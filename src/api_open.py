"""OPT-A 只读开放 API —— 带商家 API Token 的 REST 只读端点 + 令牌/订阅管理。

对标 Odoo XML-RPC / bigcapital webhook 的开发者集成能力（差距 G3），并为「老板
小程序拿到 API 权限后自动导入订单」红线预留兑现路径。

硬约束（对应任务卡第 4 节）
──────────────────────────
- **只读**：数据端点全部 GET，绝不写任何业务表；不建任务、不改状态。
- **独立认证**：`Authorization: Bearer <token>`，令牌仅存哈希（见 models.ApiToken）；
  与 session 登录链路、CSRF 行为完全解耦（现有 HTML 路由零变化）。
- **租户隔离**：令牌绑定商家，数据范围恒等于该商家的活跃门店；请求参数里出现
  非本商家门店 → 403；按 id 取到越权资源 → 404（不泄露资源是否存在）。
- **限流**：复用 rate_limit 原子计数（按 IP 计），不引入新存储。
- **无 N+1**：多店聚合用单条 in_ 查询 + Python 侧一次汇总，绝不在循环里查库。

管理端点（签发令牌 / 订阅 webhook）走既有 session 登录 + owner 角色，CSRF 照常生效；
供后台或未来的配置页调用，本模块不触碰任何模板与前端资源。
"""
import hashlib
import os
import secrets
import time
from datetime import datetime, timedelta

from flask import Blueprint, abort, current_app, g, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import func

from src import api_errors, diag, outbound_guard, webhooks
from src.models import (
    ROLE_OWNER,
    Admin,
    ApiToken,
    DailySales,
    Merchant,
    Store,
    StoreTask,
    WebhookSubscription,
    db,
)
from src.rate_limit import check_limit, window_usage
from src import business_calendar as cal

open_api_bp = Blueprint('api_v1', __name__, url_prefix='/api/v1')

TOKEN_PREFIX = 'ican_'  # noqa: S105  理由：令牌前缀字面量而非口令；随机段由 secrets.token_hex 生成（见本文件 :126）
TOKEN_BYTES = 20            # 明文令牌随机部分字节数（hex 40 位）
DEFAULT_DAYS = 30
MAX_DAYS = 365
MAX_PAGE = 500
DEFAULT_PAGE_SIZE = 100     # limit 缺省值：一屏流水约 15 行 × 7 店，100 足够且不撑爆响应体

# ── 令牌作用域（H1b，2026-09-24 第 6 轮对标）──────────────────────
# 四类只读作用域对应四组数据端点；全部以 `:read` 结尾，为未来 `:write` 留出命名空间。
SCOPE_STORES = 'stores:read'
SCOPE_DASHBOARD = 'dashboard:read'
SCOPE_SALES = 'sales:read'
SCOPE_TASKS = 'tasks:read'
DATA_SCOPES = (SCOPE_STORES, SCOPE_DASHBOARD, SCOPE_SALES, SCOPE_TASKS)
# 旧口径：OPT-A 首发时 scopes 恒为字面量 'read'（models.ApiToken 注释即写「预留」）。
# 已签发给商家的存量令牌全是这个值，必须继续等于「全部只读」，否则升级即静默断流。
LEGACY_ALL_READ = 'read'

# last_used_at 写入节流：只读 API 每次请求都落一次写，既违背「只读」直觉，
# 也会在免费层放大 WAL/锁竞争。进程内记录每枚令牌上次落库时刻，超阈值才写。
LAST_USED_MIN_INTERVAL = int(os.environ.get('API_LAST_USED_MIN_INTERVAL', '3600'))
_last_used_written = {}


def normalize_scopes(scopes):
    """把入参作用域归一化为排序去重后的规范字符串；非法作用域直接抛错。

    Args:
        scopes: 逗号分隔字符串、可迭代集合，或 None/'' （回落为 LEGACY_ALL_READ）。

    Returns:
        str: 如 'dashboard:read,sales:read'；入参含 'read' 时恒返回 'read'。

    Raises:
        ValueError: 出现 DATA_SCOPES / 'read' 之外的值。
    """
    if scopes in (None, ''):
        return LEGACY_ALL_READ
    items = ([t.strip() for t in str(scopes).split(',') if t.strip()]
             if isinstance(scopes, str)
             else [str(t).strip() for t in scopes if str(t).strip()])
    if not items:
        return LEGACY_ALL_READ
    for item in items:
        if item != LEGACY_ALL_READ and item not in DATA_SCOPES:
            raise ValueError(f'不支持的令牌作用域：{item}')
    if LEGACY_ALL_READ in items:
        return LEGACY_ALL_READ
    return ','.join(sorted(set(items)))


def effective_scopes(raw):
    """把库内 scopes 展开为「实际放行的作用域集合」。"""
    if not raw or raw.strip() == LEGACY_ALL_READ:
        return set(DATA_SCOPES)
    return {s.strip() for s in raw.split(',') if s.strip()}


def _hash_token(raw):
    """明文令牌 → SHA-256 十六进制哈希（库内唯一落库形态）。"""
    return hashlib.sha256((raw or '').encode('utf-8')).hexdigest()


# ── 令牌 / 订阅的领域服务（可脱离 HTTP 直接调用，供后台与测试复用）──────
def issue_token(merchant_id, name='', created_by_admin_id=None, scopes=LEGACY_ALL_READ):
    """为商家签发一枚只读 API 令牌，返回 (record, plaintext)。

    明文只在本函数返回值中出现一次；库内仅存哈希，之后不可再导出。

    Args:
        merchant_id: 目标商家 id。
        name: 用途备注名。
        created_by_admin_id: 操作管理员 id（可空）。
        scopes: 作用域（H1b）。逗号分隔的 `*:read` 子集；默认 'read' = 全部只读，
            以保持 OPT-A 首发以来的签发口径不变。非法值抛 ValueError。

    Returns:
        tuple[ApiToken, str]: 落库记录与一次性明文令牌。

    Raises:
        ValueError: scopes 含 DATA_SCOPES / 'read' 之外的值。
    """
    plaintext = TOKEN_PREFIX + secrets.token_hex(TOKEN_BYTES)
    record = ApiToken(merchant_id=int(merchant_id), name=str(name or '')[:80],
                      token_hash=_hash_token(plaintext), scopes=normalize_scopes(scopes),
                      is_active=True, created_by_admin_id=created_by_admin_id)
    db.session.add(record)
    db.session.commit()
    return record, plaintext


def revoke_token(token_id, merchant_id):
    """吊销令牌（限定归属商家，避免跨商家吊销）。返回是否命中。"""
    record = db.session.get(ApiToken, int(token_id))
    if record is None or record.merchant_id != int(merchant_id):
        return False
    record.is_active = False
    db.session.commit()
    return True


def subscribe_webhook(merchant_id, url, event_types='*', created_by_admin_id=None, resolver=None):
    """新增 Webhook 订阅，返回 (record, signing_secret)。

    派生签名密钥随返回值一次性交付商家用于验签；库内只存随机盐，密钥不落库明文。
    `resolver` 只为测试注入而存在（喂假 DNS），**不是**"允许私网"的开关：
    开那种开关等于把一个洞留给生产环境。
    """
    clean_url = str(url or '').strip()
    if not clean_url.startswith(('http://', 'https://')):
        raise ValueError('回调 URL 必须以 http:// 或 https:// 开头')
    if len(clean_url) > 500:
        raise ValueError('回调 URL 过长')
    # 第 30 轮 H81：这个 URL 是**服务端出站点**，不是普通字符串字段。
    # 只校前缀 ⇒ 已认证商家可让服务器去请求回环/私网/云元数据地址（SSRF）。
    # 创建时早失败给可读原因（Frappe `validate_request_url`），投递时再判一次（防事后改库
    # 与 DNS 变化），两处都走同一个 `outbound_guard`，不各写一份判定。
    verdict = outbound_guard.check_target(clean_url, resolver=resolver)
    if not verdict['ok']:
        raise ValueError(f'回调 URL 被出站守卫拒绝：{verdict["reason"]}')
    types = _normalize_event_types(event_types)
    salt = secrets.token_hex(16)
    record = WebhookSubscription(merchant_id=int(merchant_id), url=clean_url,
                                 event_types=types, secret_salt=salt, is_active=True,
                                 created_by_admin_id=created_by_admin_id)
    db.session.add(record)
    db.session.commit()
    secret = webhooks.signing_secret(current_app.config.get('SECRET_KEY'), salt)
    return record, secret


def unsubscribe_webhook(sub_id, merchant_id):
    """停用订阅（限定归属商家）。返回是否命中。"""
    record = db.session.get(WebhookSubscription, int(sub_id))
    if record is None or record.merchant_id != int(merchant_id):
        return False
    record.is_active = False
    db.session.commit()
    return True


def _normalize_event_types(event_types):
    """校验并归一化事件类型列表；'*' 或合法事件子集，逗号分隔。非法即抛错。"""
    if event_types in (None, '', '*'):
        return '*'
    if isinstance(event_types, str):
        items = [t.strip() for t in event_types.split(',') if t.strip()]
    else:
        items = [str(t).strip() for t in event_types if str(t).strip()]
    for item in items:
        if item not in webhooks.SUPPORTED_EVENTS:
            raise ValueError(f'不支持的事件类型：{item}')
    return ','.join(sorted(set(items)))


def _merchant_store_ids(merchant_id):
    """商家的活跃门店 id（数据范围唯一口径）。"""
    rows = Store.query.filter_by(merchant_id=merchant_id, is_active=True).order_by(Store.id).all()
    return [row.id for row in rows]


def _authenticate():
    """Bearer 令牌认证 + 限流 + 商家有效性校验；结果写入 g.api_ctx。

    失败即 abort：缺失/非法令牌 401，商家停用 403，超限 429，功能关闭 404。
    """
    if not current_app.config.get('OPEN_API_ENABLED'):
        abort(404)
    limited = check_limit('openapi', limit=current_app.config.get('OPEN_API_RATE_LIMIT'),
                          window=current_app.config.get('OPEN_API_RATE_WINDOW'))
    if limited is not None:
        # check_limit 面向 HTML 返回 429 页；开放 API 统一回 JSON
        return api_errors.json_error(429, 'rate_limited')
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        abort(401, description='缺少 Bearer 令牌')
    raw = header[len('Bearer '):].strip()
    token = ApiToken.query.filter_by(token_hash=_hash_token(raw), is_active=True).first()
    if token is None:
        # 不区分「令牌不存在 / 已吊销」，避免枚举；也不确认商家是否存在
        abort(401, description='令牌无效或已失效')
    merchant = db.session.get(Merchant, token.merchant_id)
    if merchant is None or not merchant.is_active:
        abort(403, description='商家不存在或已停用')
    g.api_ctx = {'merchant_id': merchant.id, 'store_ids': _merchant_store_ids(merchant.id),
                 'token_id': token.id, 'scopes': sorted(effective_scopes(token.scopes))}
    _touch_last_used(token)
    return None


def _touch_last_used(token):
    """按进程内节流回写 `last_used_at`（默认同一令牌 1 小时内至多写一次）。

    写失败只记日志不影响读取：认证已成功，观测性字段不得反噬主链路。
    """
    now = time.time()
    if now - _last_used_written.get(token.id, 0) < LAST_USED_MIN_INTERVAL:
        return
    try:
        token.last_used_at = diag.utc_now_naive()
        db.session.commit()
        _last_used_written[token.id] = now
    except Exception:
        db.session.rollback()
        current_app.logger.debug('last_used_at 回写失败（忽略）', exc_info=True)


def _require_scope(scope):
    """端点级作用域闸门：令牌缺少该作用域则 403（JSON，口径与既有错误体一致）。

    Args:
        scope: DATA_SCOPES 之一。

    Returns:
        None: 放行。存量 scopes='read' 令牌经 effective_scopes() 展开为全只读。

    Raises:
        werkzeug.exceptions.HTTPException: abort(403) 并携带可读原因。
    """
    if scope not in g.api_ctx['scopes']:
        return api_errors.json_error(403, 'insufficient_scope',
                                     f'该令牌缺少作用域 {scope}',
                                     granted=g.api_ctx['scopes'], required=scope)
    return None


def _resolve_store_ids():
    """解析可选 store_id 过滤参数，并强制落在本商家可见集合内（否则 403）。"""
    scope = g.api_ctx['store_ids']
    raw = request.args.get('store_id')
    if raw in (None, ''):
        return scope
    try:
        store_id = int(raw)
    except (TypeError, ValueError):
        abort(400, description='store_id 必须为整数')
    if store_id not in scope:
        abort(403, description='该门店不属于当前令牌所属商家')
    return [store_id]


def _parse_days(default=DEFAULT_DAYS):
    raw = request.args.get('days')
    if raw in (None, ''):
        return default
    try:
        days = int(raw)
    except (TypeError, ValueError):
        abort(400, description='days 必须为整数')
    if days < 1 or days > MAX_DAYS:
        abort(400, description=f'days 需在 1~{MAX_DAYS} 之间')
    return days


def _parse_date(raw, field='date'):
    if raw in (None, ''):
        return cal.business_today()
    try:
        return datetime.strptime(str(raw), '%Y-%m-%d').date()
    except ValueError:
        abort(400, description=f'{field} 需为 YYYY-MM-DD')


def _parse_limit():
    """页大小：缺省 DEFAULT_PAGE_SIZE，上限 MAX_PAGE；越界一律 400 而非静默夹取。

    静默夹取会让调用方以为拿到了自己要求的量 —— 越界必须是显式失败。
    """
    raw = request.args.get('limit')
    if raw in (None, ''):
        return DEFAULT_PAGE_SIZE
    try:
        size = int(raw)
    except (TypeError, ValueError):
        abort(400, description='limit 必须为整数')
    if size < 1 or size > MAX_PAGE:
        abort(400, description=f'limit 需在 1~{MAX_PAGE} 之间')
    return size


def _parse_cursor():
    """游标 = 上一页最后一条记录的 id；非法输入 400，缺省表示从头开始。"""
    raw = request.args.get('cursor')
    if raw in (None, ''):
        return None
    try:
        cursor = int(raw)
    except (TypeError, ValueError):
        abort(400, description='cursor 需为上一页 next_cursor 返回的整数')
    if cursor < 0:
        abort(400, description='cursor 不能为负')
    return cursor


def _page_shape(rows, size):
    """把「多取一条」的探测结果收敛成 (本页行, has_more, next_cursor)。

    多取一条比 COUNT(*) 便宜，且不受并发写入导致的 offset 漂移影响。
    """
    has_more = len(rows) > size
    page = rows[:size]
    return page, has_more, (page[-1].id if has_more and page else None)


@open_api_bp.after_request
def _attach_rate_headers(response):
    """给每个 /api/v1 响应挂配额头；429 额外挂 Retry-After。"""
    usage = window_usage('openapi', limit=current_app.config.get('OPEN_API_RATE_LIMIT'),
                         window=current_app.config.get('OPEN_API_RATE_WINDOW'))
    if usage is None:
        return response
    response.headers['X-RateLimit-Limit'] = str(usage['limit'])
    response.headers['X-RateLimit-Remaining'] = str(usage['remaining'])
    response.headers['X-RateLimit-Reset'] = str(usage['reset_in'])
    if response.status_code == 429:
        response.headers['Retry-After'] = str(max(1, usage['reset_in']))
    return response


def _guard():
    """统一前置：认证/限流；返回非 None 即为需要提前返回的响应（如 429）。"""
    return _authenticate()


# ── 数据端点（只读 GET）────────────────────────────────────────
@open_api_bp.route('/meta', methods=['GET'])
def api_meta():
    """探活并回显当前令牌的可见范围（不含任何跨商家信息）。"""
    early = _guard()
    if early is not None:
        return early
    return jsonify(ok=True, merchant_id=g.api_ctx['merchant_id'],
                   store_count=len(g.api_ctx['store_ids']), scopes=g.api_ctx['scopes'],
                   server_time=diag.utc_iso_z())


@open_api_bp.route('/stores', methods=['GET'])
def api_stores():
    """商家门店列表。"""
    early = _guard()
    if early is not None:
        return early
    denied = _require_scope(SCOPE_STORES)
    if denied is not None:
        return denied
    rows = (Store.query.filter_by(merchant_id=g.api_ctx['merchant_id'], is_active=True)
            .order_by(Store.id).all())
    return jsonify(ok=True, stores=[
        {'id': s.id, 'name': s.name, 'code': s.code, 'active_pack_id': s.active_pack_id}
        for s in rows])


@open_api_bp.route('/dashboard', methods=['GET'])
def api_dashboard():
    """看板聚合：范围内逐店营收/销量/记录数 + 总量（DB 侧 GROUP BY 单查询）。

    用聚合下推（SUM/COUNT + GROUP BY store_id）替代「拉全表 ORM 对象再 Python 汇总」，
    既避免逐店 N+1，也避免物化成千上万行实体 —— 同数据量下 P95 对齐既有看板路径。
    不按激活包过滤，返回商家完整数据。
    """
    early = _guard()
    if early is not None:
        return early
    denied = _require_scope(SCOPE_DASHBOARD)
    if denied is not None:
        return denied
    days = _parse_days()
    ids = _resolve_store_ids()
    cutoff = cal.business_today() - timedelta(days=days - 1)
    scope = ids or [-1]
    names = {s.id: s.name for s in Store.query.filter(Store.id.in_(scope)).all()}
    rows = (db.session.query(
                DailySales.store_id,
                func.coalesce(func.sum(DailySales.total_revenue), 0),
                func.coalesce(func.sum(DailySales.product_sales_qty), 0),
                func.count(DailySales.id))
            .filter(DailySales.store_id.in_(scope), DailySales.record_date >= cutoff)
            .group_by(DailySales.store_id).all())
    stores_out = [{'store_id': sid, 'store_name': names.get(sid, f'门店{sid}'),
                   'revenue': round(float(rev), 2), 'quantity': int(qty), 'records': int(cnt)}
                  for sid, rev, qty, cnt in rows]
    stores_out.sort(key=lambda x: x['revenue'], reverse=True)
    total_rev = round(sum(x['revenue'] for x in stores_out), 2)
    total_qty = sum(x['quantity'] for x in stores_out)
    record_count = sum(x['records'] for x in stores_out)
    return jsonify(ok=True, days=days, total_revenue=total_rev,
                   total_quantity=total_qty, record_count=record_count,
                   stores=stores_out)


@open_api_bp.route('/sales/daily', methods=['GET'])
def api_sales_daily():
    """指定日期（默认今天）的流水明细，按 id 游标翻页（只取所需列）。

    旧形态是「无上限一次性全给」：单店单日最多几百行时无所谓，但免费层遇到批量导入
    后的千行日子会把整个响应体堆进内存，调用方也无从续取。现按 limit+cursor 翻页，
    上限恒 ≤ MAX_PAGE，绝不一次吐全表。
    """
    early = _guard()
    if early is not None:
        return early
    denied = _require_scope(SCOPE_SALES)
    if denied is not None:
        return denied
    day = _parse_date(request.args.get('date'))
    ids = _resolve_store_ids()
    size = _parse_limit()
    cursor = _parse_cursor()
    query = (db.session.query(
                DailySales.id, DailySales.store_id, DailySales.product_name,
                DailySales.product_sales_qty, DailySales.total_revenue, DailySales.pack_id)
             .filter(DailySales.store_id.in_(ids or [-1]), DailySales.record_date == day))
    if cursor is not None:
        query = query.filter(DailySales.id > cursor)
    rows = query.order_by(DailySales.id).limit(size + 1).all()
    page, has_more, next_cursor = _page_shape(rows, size)
    return jsonify(ok=True, date=day.isoformat(), count=len(page), limit=size,
                   has_more=has_more, next_cursor=next_cursor, records=[
        {'id': r.id, 'store_id': r.store_id, 'product_name': r.product_name,
         'quantity': r.product_sales_qty, 'revenue': float(r.total_revenue or 0),
         'pack_id': r.pack_id} for r in page])


@open_api_bp.route('/sales/history', methods=['GET'])
def api_sales_history():
    """近 N 天按日营收/销量汇总（DB 侧 GROUP BY record_date 单查询）。"""
    early = _guard()
    if early is not None:
        return early
    denied = _require_scope(SCOPE_SALES)
    if denied is not None:
        return denied
    days = _parse_days()
    ids = _resolve_store_ids()
    cutoff = cal.business_today() - timedelta(days=days - 1)
    rows = (db.session.query(
                DailySales.record_date,
                func.coalesce(func.sum(DailySales.total_revenue), 0),
                func.coalesce(func.sum(DailySales.product_sales_qty), 0))
            .filter(DailySales.store_id.in_(ids or [-1]), DailySales.record_date >= cutoff)
            .group_by(DailySales.record_date).order_by(DailySales.record_date).all())
    return jsonify(ok=True, days=days, series=[
        {'date': d.record_date.isoformat(), 'revenue': round(float(d[1]), 2),
         'quantity': int(d[2])} for d in rows])


@open_api_bp.route('/tasks', methods=['GET'])
def api_tasks():
    """任务状态列表（支持 status / store_id 过滤，按 id 游标翻页）。

    排序由「due_date asc, id desc」改为「id asc」：游标翻页要求一个与游标同向的
    全序，按到期日排序翻页会在中途插入新任务时漏行/重行。需要按到期日看的调用方
    自行对返回页排序（任务量级很小）。
    """
    early = _guard()
    if early is not None:
        return early
    denied = _require_scope(SCOPE_TASKS)
    if denied is not None:
        return denied
    ids = _resolve_store_ids()
    size = _parse_limit()
    cursor = _parse_cursor()
    status = request.args.get('status')
    query = StoreTask.query.filter(StoreTask.merchant_id == g.api_ctx['merchant_id'],
                                   StoreTask.store_id.in_(ids or [-1]))
    if status:
        query = query.filter(StoreTask.status == status)
    if cursor is not None:
        query = query.filter(StoreTask.id > cursor)
    rows = query.order_by(StoreTask.id.asc()).limit(size + 1).all()
    page, has_more, next_cursor = _page_shape(rows, size)
    return jsonify(ok=True, count=len(page), limit=size, has_more=has_more,
                   next_cursor=next_cursor, tasks=[
        {'id': t.id, 'store_id': t.store_id, 'title': t.title, 'status': t.status,
         'source': t.source, 'due_date': t.due_date.isoformat() if t.due_date else None}
        for t in page])


@open_api_bp.route('/tasks/<int:task_id>', methods=['GET'])
def api_task_detail(task_id):
    """单任务详情；非本商家任务返回 404（不泄露资源是否存在）。"""
    early = _guard()
    if early is not None:
        return early
    denied = _require_scope(SCOPE_TASKS)
    if denied is not None:
        return denied
    task = db.session.get(StoreTask, task_id)
    if task is None or task.merchant_id != g.api_ctx['merchant_id']:
        abort(404)
    return jsonify(ok=True, task={
        'id': task.id, 'store_id': task.store_id, 'title': task.title,
        'status': task.status, 'source': task.source,
        'due_date': task.due_date.isoformat() if task.due_date else None,
        'assignee_role': task.assignee_role})


# ── 管理端点（session 登录 + owner 角色；CSRF 由全站保护照常生效）──────
def _session_owner_merchant_id():
    """管理端点归属校验：仅本商家老板可管理其令牌/订阅；否则 403。

    注意：Staff 账号没有 role 字段，若只按 `getattr(role) or OWNER` 判定会被误放行，
    因此必须先按类型收口为 Admin，再校验 role=owner（店员/店长/平台管理员一律 403）。
    """
    if not getattr(current_user, 'is_authenticated', False):
        abort(401)
    if not isinstance(current_user, Admin):
        abort(403, description='仅管理员账号可管理开放 API 凭证')
    role = getattr(current_user, 'role', None) or ROLE_OWNER
    if role != ROLE_OWNER:
        abort(403, description='仅商家老板可管理开放 API 凭证')
    merchant_id = getattr(current_user, 'merchant_id', None)
    if merchant_id is None:
        abort(403, description='当前账号无归属商家')
    return merchant_id


@open_api_bp.route('/manage/tokens', methods=['POST'])
@login_required
def manage_create_token():
    """为当前老板商家签发一枚只读令牌，明文仅本次返回。

    可选 `scopes`（H1b）：逗号分隔的 `stores:read / dashboard:read / sales:read /
    tasks:read` 子集，用于给不同接入方发最小权限令牌；省略或传 `read` 即全部只读
    （与 OPT-A 首发口径一致）。非法作用域回 400，不签发出一个永不放行的令牌。
    """
    merchant_id = _session_owner_merchant_id()
    payload = request.get_json(silent=True) or request.form
    try:
        record, plaintext = issue_token(merchant_id, name=payload.get('name', ''),
                                        created_by_admin_id=current_user.id,
                                        scopes=payload.get('scopes') or LEGACY_ALL_READ)
    except ValueError as exc:
        return api_errors.json_error(400, 'bad_request', str(exc))
    return jsonify(ok=True, token_id=record.id, token=plaintext, scopes=record.scopes,
                   note='明文令牌仅此一次返回，库内仅存哈希，请妥善保存'), 201


@open_api_bp.route('/manage/tokens/<int:token_id>/revoke', methods=['POST'])
@login_required
def manage_revoke_token(token_id):
    """吊销当前老板商家的一枚令牌。"""
    merchant_id = _session_owner_merchant_id()
    if not revoke_token(token_id, merchant_id):
        abort(404)
    return jsonify(ok=True, revoked=token_id)


@open_api_bp.route('/manage/webhooks', methods=['POST'])
@login_required
def manage_subscribe_webhook():
    """为当前老板商家新增 Webhook 订阅，返回一次性派生签名密钥。"""
    merchant_id = _session_owner_merchant_id()
    payload = request.get_json(silent=True) or request.form
    try:
        record, secret = subscribe_webhook(
            merchant_id, url=payload.get('url', ''),
            event_types=payload.get('event_types', '*'),
            created_by_admin_id=current_user.id)
    except ValueError as exc:
        return api_errors.json_error(400, 'bad_request', str(exc))
    return jsonify(ok=True, webhook_id=record.id, signing_secret=secret,
                   note='签名密钥仅此一次返回；出站事件带 X-ICAN-Signature: '
                        't=<unix>,sha256=HMAC("<t>." + body)，时间窗 ±'
                        f'{webhooks.SIGNATURE_TOLERANCE_SECONDS}s'), 201


@open_api_bp.route('/manage/webhooks/<int:sub_id>/revoke', methods=['POST'])
@login_required
def manage_unsubscribe_webhook(sub_id):
    """停用当前老板商家的一条 Webhook 订阅。"""
    merchant_id = _session_owner_merchant_id()
    if not unsubscribe_webhook(sub_id, merchant_id):
        abort(404)
    return jsonify(ok=True, revoked=sub_id)


__all__ = [
    'DATA_SCOPES',
    'SCOPE_DASHBOARD',
    'SCOPE_SALES',
    'SCOPE_STORES',
    'SCOPE_TASKS',
    'ApiToken',
    'WebhookSubscription',
    'effective_scopes',
    'issue_token',
    'normalize_scopes',
    'notify_hook',
    'open_api_bp',
    'revoke_token',
    'subscribe_webhook',
    'unsubscribe_webhook',
]


def notify_hook(event_type, merchant_id, data):
    """业务写库成功后对外暴露的 Webhook 触发口（薄封装，异常已在 webhooks.notify 内吞掉）。"""
    return webhooks.notify(event_type, merchant_id, data)
