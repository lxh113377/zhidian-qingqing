"""变更归因链（H59：谁、在什么时候、把哪条记录从什么改成了什么）。

为什么是一个监听器而不是散点调用
--------------------------------
第 26 轮当场实测：17 张表里只有 3 张带 `created_by_admin_id`、3 张带 `updated_at`，
**0 张**带「谁改的」；`src/routes/admin.py` 全文件只有 1 处 logger 调用。也就是说
「管理员改了主数据」这件事在盘上不留任何痕迹 —— 新增一个写点就得记得调一次审计，
这正是前 25 轮反复踩的「同类出口只修一半」。

对标口径（只抄判据不抄机制；文件与行号取 2026-09-27 `gh api` 实盘原文）：
- Odoo `odoo/orm/models.py:304-313`：`create_uid/create_date/write_uid/write_date` 四列由
  **metaclass `__new__` 自动注入**，且 `:4273` 把客户端传入的同名字段一律丢弃、`:3866`
  写入期服务端 `setdefault('write_uid', env.uid)` ⇒ 归因不靠自觉、也不可伪造；
- Frappe `frappe/database/database.py:102` `DEFAULT_COLUMNS` 含 `owner/modified_by`，
  `:867-870` 每次 UPDATE 默认打 `modified_by`，前像由 `document.py:1873 load_doc_before_save()`
  提供，字段级 diff 落在 Version doctype（`version.py:104-118` 的
  `{"changed": [[field, old, new]], ...}`）⇒ 本模块的 `TRACKED` 名册同一形态，
  门禁第 40 项按名册分母对账（漏登记会红）；
- Frappe `frappe/model/delete_doc.py:240-258`：默认 `delete_permanently=False`，
  删除把**整行前像 JSON + 操作者**写进 `Deleted Document` 回收表 ⇒ 对应 `action='delete'`
  必须携带前像；InvenTree `part/models.py:584-604` 的 `Part.delete()` 双门禁
  （仍 active 禁删、被装配引用禁删）⇒ 对应 `routes/admin.py` 的「有历史流水先归档、不许删」。
- Saleor `saleor/order/events.py:190-199` + `order/models.py:868-905`：事件表 append-only、
  与可变实体分表 ⇒ `change_log` 只增不改，代码里不存在 UPDATE/DELETE 出口（门禁 AST 判）。

注：曾被引用的 InvenTree `AuditMixin` / `StatusMixin` / `InvenTree/audit.py`
（`delete_server_side`、`log_event`）**在 master 已不存在** —— 2026-09-27 全树 2664 路径
`grep -i audit` 0 命中、1.0.0/0.19.5/0.18.1/0.17.0/0.16.0 逐个 tag 验过均 404。
机制改以 Odoo metaclass 与 Frappe DEFAULT_COLUMNS 为据。

脱敏边界（安全红线：商家业务明细不得暴露给平台管理员）
--------------------------------------------------
名册分两档：**主数据**（产品/门店/账号/包定义）记字段级前像与后像；
**业务明细**（流水/考核/AI 台账）只记「谁在什么时候动了哪张表的哪条 id」，
值一律不入库（`REDACTED_TABLES`）。这条边界本身是门禁判据，不只是注释。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import event, inspect, insert, select
from sqlalchemy.orm import Session

from src import diag

# ─── 声明层：跟踪谁、记哪些字段（唯一真相源，门禁第 40 项按此对账分母）───
# 字段名一律取实库列名（`validate_registry()` 会把写错的名字判红，不靠 getattr 兜底沉默）。
TRACKED = {
    'products': ('product_name', 'unit_price', 'cost_price', 'category',
                 'is_active', 'pack_id', 'store_id'),
    'stores': ('name', 'code', 'address', 'phone', 'is_active',
               'merchant_id', 'active_pack_id'),
    'staff': ('username', 'display_name', 'phone', 'store_name', 'is_active',
              'merchant_id', 'store_id'),
    'merchants': ('name', 'contact_email', 'is_active'),
    'admin': ('username', 'role', 'merchant_id'),
    'industry_packs': ('name', 'description', 'is_active'),
    'metric_defs': ('key', 'label', 'value_type', 'unit', 'role', 'agg',
                    'required', 'enabled', 'sort', 'pack_id', 'store_id'),
    'pack_items': ('section', 'key', 'payload', 'pack_id'),
    'store_tasks': ('title', 'detail', 'source', 'assignee_staff_id', 'due_date',
                    'status', 'review_note', 'store_id', 'merchant_id',
                    'assignee_admin_id', 'assignee_role', 'takeover_json'),
    'api_tokens': ('name', 'scopes', 'is_active'),
    'webhook_subscriptions': ('url', 'event_types', 'is_active'),
}

# 凭据类与业务明细类列永不得进审计值（即使被误写进 TRACKED，validate_registry() 也会判红）。
# pack_items.payload 不在其列：它装的是行业包定义（主数据），不是商家营业明细。
FORBIDDEN_FIELDS = ('password', 'token_hash', 'secret_salt', 'code_hash',
                    'evidence_json', 'before_json', 'after_json', 'metrics_json',
                    'raw_material_remaining', 'raw_material_waste', 'abnormal_notes',
                    'total_revenue', 'product_sales_qty', 'question', 'staff_answer',
                    'ai_feedback', 'detail_json', 'value_json')

# 业务明细：只记动作与主键，字段值一律不落（安全红线，非风格偏好）
REDACTED_TABLES = ('daily_sales', 'quiz_records', 'ai_usage_ledger')

# 显式不跟踪 + 理由。门禁判据要求「不跟踪」也必须登记，禁对空气豁免。
UNTRACKED = {
    'change_log': '本表即审计载体，自记会无限递归',
    'schema_patch_log': '迁移账本由 init_db 幂等写入，非业务变更',
    'admin_store_access': '授权关系由门店页整表重建，字段级 diff 无意义',
    'email_verifications': '一次性 nonce，consumed 即删，留值等于留攻击面',
    'webhook_delivery': '出站投递留痕本身就是逐次可查的审计物件，再记一层 diff 是自我复制',
}

MAX_FIELDS = 40
MAX_VALUE_LEN = 180
# 'conflict_rejected' = 第 31 轮：写被并发守卫拒下（**没有落库**），但必须看得见被拒过。
ACTIONS = ('create', 'update', 'delete', 'archive', 'bulk_delete', 'bulk_seed',
           'conflict_rejected')

# 显式静默档：演示数据播种这类「一次上万行的平台级动作」逐行入审计只会把有用的记录
# 埋掉。对标 Frappe 的 `frappe.flags.in_install` 与 Odoo 的 `flush_mode` 同一姿态 ——
# 允许静默，但必须是**点名登记的动作**，不是默认行为（调用点由门禁第 40 项 AST 枚举，
# 名单外的 `silenced()` 会红）。
_SILENCE = threading.local()

_counters = {'written': 0, 'failed': 0, 'skipped_untracked': 0, 'redacted': 0,
             'empty_diff': 0, 'mark_failed': 0, 'disabled': 0}

logger = logging.getLogger(__name__)


def stats():
    """供 /healthz 与门禁读取的聚合整数（不含任何实体信息）。"""
    return dict(_counters)


def reset_counters():
    for key in _counters:
        _counters[key] = 0


def tracked_tables():
    """全量纳入归因的名册（含脱敏档）——门禁与 L4 的分母都从这一个函数取。"""
    return tuple(sorted(set(TRACKED) | set(REDACTED_TABLES)))


def is_silenced():
    return getattr(_SILENCE, 'depth', 0) > 0


@contextmanager
def silenced():
    """点名关闭逐行归因（嵌套安全）。退出后自动恢复；关闭期间的写入**一行都不记**。"""
    _SILENCE.depth = getattr(_SILENCE, 'depth', 0) + 1
    try:
        yield
    finally:
        _SILENCE.depth -= 1


def note_bulk(entity, action, rows, merchant_id=None, store_id=None, detail=''):
    """核心批量 DELETE/INSERT 的**聚合**归因。

    为什么必须有它：`Model.query...delete()` 走的是 Core DELETE，绕开 ORM 单元工作，
    监听器天然看不见它 —— 不补这一条，「批量删了什么」就还是只有 stdout 里一行字。
    `entity_pk=0` 是聚合行的约定值（0 不可能是任何真实主键），diff 里只给条数与来源，
    不给任何业务值。
    """
    if action not in ('bulk_delete', 'bulk_seed'):
        raise ValueError('note_bulk 只接受聚合动作')
    try:
        from src.models import ChangeLog

        kind, actor_id, role = _actor()
        session = db_session()
        session.execute(insert(ChangeLog.__table__), [{
            'entity': entity, 'entity_pk': 0, 'action': action,
            'merchant_id': merchant_id, 'store_id': store_id,
            'actor_kind': kind, 'actor_id': actor_id, 'actor_role': role,
            'request_id': diag.get_request_id() or '',
            'source': 'system' if kind == 'system' else 'web',
            'diff_json': json.dumps({'bulk': True, 'rows': int(rows),
                                     'detail': (detail or '')[:MAX_VALUE_LEN]},
                                    ensure_ascii=False, sort_keys=True),
            'changed_at': datetime.now(UTC).replace(tzinfo=None),
        }])
        _counters['written'] += 1
        return True
    except Exception:                      # noqa: BLE001 聚合归因失败同样不得打断业务
        _counters['failed'] += 1
        return False


def note_denied(entity, entity_pk, fields, merchant_id=None, store_id=None):
    """被守卫拒下的单行写入也要留痕：只记**字段名与主键**，绝不记业务值。

    为什么不用 note_bulk：那条是聚合行的专用口（`entity_pk=0` + 动作白名单），
    把"某一行被拒"塞进聚合口会同时丢两件事 —— 真实主键与"这次没落库"的语义。
    实测：第一版就是这么写的，`note_bulk` 自己抛 `ValueError('只接受聚合动作')` 拦下来。

    为什么这条要**自己提交**：被拒的请求根本不会走到业务 commit，
    把审计行留在同一个未提交事务里 = 随请求一起被回滚掉（实测第一版就是"409 发了、行没了"）。
    审计写自身失败只计数，绝不影响对外的 409。
    """
    try:
        from src.models import ChangeLog

        kind, actor_id, role = _actor()
        session = db_session()
        session.execute(insert(ChangeLog.__table__), [{
            'entity': entity, 'entity_pk': int(entity_pk or 0),
            'action': 'conflict_rejected',
            'merchant_id': merchant_id, 'store_id': store_id,
            'actor_kind': kind, 'actor_id': actor_id, 'actor_role': role,
            'request_id': diag.get_request_id() or '',
            'source': 'system' if kind == 'system' else 'web',
            'diff_json': json.dumps({'denied': True,
                                     'fields': [str(f)[:40] for f in list(fields)[:MAX_FIELDS]]},
                                    ensure_ascii=False, sort_keys=True),
            'changed_at': datetime.now(UTC).replace(tzinfo=None),
        }])
        session.commit()
        _counters['written'] += 1
        return True
    except Exception:                      # noqa: BLE001 审计不得打断业务写路径
        try:
            db_session().rollback()
        except Exception as exc2:          # noqa: BLE001 回滚本身失败也不能穿出，但要留现场
            logger.warning('被拒写留痕的回滚也失败了（事务状态需人工复核）: %s', exc2)
        _counters['failed'] += 1
        return False


def purge(model, **filters):
    """批量删除的唯一入口：删 + 聚合留痕一步做完，调用方不可能只做到一半。

    为什么不直接写 `Model.query.filter_by(...).delete()`：那条路绕开 ORM 单元工作，
    before_flush 监听器看不见 ⇒ 删了多少行、谁删的，盘上一行都不留。
    第 26 轮把全仓 7 处裸批量删换成这个入口，门禁第 40 项再按「含批量删的函数必须
    引用 change_log」拦住以后的第 8 处。
    """
    table = getattr(model, '__tablename__', str(model))
    query = db_session().query(model)
    if filters:
        query = query.filter_by(**filters)
    rows = query.delete(synchronize_session=False)
    store_id = filters.get('store_id')
    note_bulk(table, 'bulk_delete', rows, merchant_id=filters.get('merchant_id'),
              store_id=store_id, detail=' '.join(f'{k}={v}' for k, v in filters.items()))
    return rows


def db_session():
    from src.models import db

    return db.session


def _jsonable(value):
    """值归一为可入 JSON 的短标量。Decimal/datetime/bytes 都走字符串形态。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    return text[:MAX_VALUE_LEN] + '…' if len(text) > MAX_VALUE_LEN else text


def _fields_for(table):
    """返回要记值的字段；脱敏档返回空元组；未登记档返回 None。"""
    if table in REDACTED_TABLES:
        return ()
    return TRACKED.get(table)


def _values(obj, fields):
    return {name: _jsonable(getattr(obj, name, None))
            for name in fields[:MAX_FIELDS]}


def _history_diff(obj, fields):
    """UPDATE 的字段级前像/后像。只用 SQLAlchemy 属性历史，不按 mtime 猜。"""
    state = inspect(obj)
    diff = {}
    for name in fields[:MAX_FIELDS]:
        hist = getattr(state.attrs, name).history
        if not hist.has_changes():
            continue
        before = hist.deleted[0] if hist.deleted else None
        after = hist.added[0] if hist.added else None
        diff[name] = [_jsonable(before), _jsonable(after)]
    return diff


def _actor():
    """请求内的操作者；CLI/迁移/测试上下文返回 system（宁可不归因到人，也不崩写路径）。"""
    try:
        from flask import has_request_context

        if not has_request_context():
            return 'system', None, None
        from flask_login import current_user

        if not current_user or not current_user.is_authenticated:
            return 'anonymous', None, None
        kind = 'staff' if type(current_user).__name__ == 'Staff' else 'admin'
        return kind, current_user.id, getattr(current_user, 'role', None)
    except Exception:                      # noqa: BLE001 审计不得打断业务写入
        return 'system', None, None


_STORE_MERCHANT_MAX = 256
_store_merchant_cache = {}


def _store_merchant(session, store_id):
    """store→merchant 的进程级小缓存。

    为什么要有它：门禁第 18 项按**语句数**管热路径 —— 每次填报落库都多发一条
    `SELECT merchant_id FROM stores`，单条提交的语句数从 3 顶到 5。缓存后同一门店只在
    首次解析时查一次，代价从"每写一条流水一次查询"降回"每门店一次"（不是 N+1）。
    满了整体丢空：门店数远小于 256，不需要 LRU 的复杂度，丢空重取也只是多查一次。
    """
    if store_id in _store_merchant_cache:
        return _store_merchant_cache[store_id]
    from src.models import Store

    merchant_id = session.execute(
        select(Store.__table__.c.merchant_id).where(Store.__table__.c.id == store_id)
    ).scalar()
    if len(_store_merchant_cache) >= _STORE_MERCHANT_MAX:
        _store_merchant_cache.clear()
    _store_merchant_cache[store_id] = merchant_id
    return merchant_id


def _merchant_of(obj, table, session, cache):
    """解析商家归属。Product 一类只有 store_id，需要 store→merchant 一跳。

    这一跳必须走 **Core SELECT**（直接打 `stores` 表对象），不能用 `session.query(Store)`：
    在 flush 内部做 ORM 加载会动到 identity map —— 实测抛
    `SAWarning: Identity map already had an identity for (Store, (2,), None) ...
    Are there load operations occurring inside of an event handler within the flush?`
    （第 26 轮 L4 当场报出）。读原始列值不进 identity map，也不触发级联。
    调用方另用 `no_autoflush` 兜住"这次读引发新一轮 flush"的递归。
    """
    direct = getattr(obj, 'merchant_id', None)
    if direct is not None:
        return direct
    if table == 'merchants':
        return obj.id
    store_id = getattr(obj, 'store_id', None)
    if store_id is None:
        return None
    if store_id in cache:
        return cache[store_id]
    merchant_id = _store_merchant(session, store_id)
    cache[store_id] = merchant_id
    return merchant_id


def _pending_action(obj):
    """行上的动作提示（归档语义用，见 mark_archive）。state.info 是官方留的挂点。"""
    try:
        return inspect(obj).info.get('change_action')
    except Exception:                      # noqa: BLE001
        return None


def mark_archive(obj):
    """调用方把「删除改归档」标出来，让审计里看得见 archive 而不是含糊的 update。"""
    try:
        inspect(obj).info['change_action'] = 'archive'
    except Exception:                      # noqa: BLE001 标记失败不影响业务写
        # 静默失败也要留指纹：归档会退化成一条 update 行（动作名不准，但值仍然对）
        _counters['mark_failed'] += 1
        logger.warning('[change_log] 归档标记失败 entity=%s pk=%s',
                       getattr(obj, '__tablename__', '?'), getattr(obj, 'id', '?'))


def _diff_for(obj, action, fields):
    if fields == ():
        _counters['redacted'] += 1
        return {}
    if action == 'update':
        diff = _history_diff(obj, fields)
        if not diff:
            _counters['empty_diff'] += 1
        return diff
    if action == 'archive':
        return {'is_active': [True, False]}
    values = _values(obj, fields)
    if action == 'delete':
        return {name: [val, None] for name, val in values.items()}
    return {name: [None, val] for name, val in values.items()}


def _payload_for(obj, action, session, cache):
    table = getattr(obj.__table__, 'name', None)
    if table is None or table == 'change_log':
        return None
    fields = _fields_for(table)
    if fields is None:
        _counters['skipped_untracked'] += 1
        return None
    pk = getattr(obj, 'id', None)
    if pk is None:
        return None
    diff = _diff_for(obj, action, fields)
    if action == 'update' and not diff:
        return None
    kind, actor_id, role = _actor()
    return {
        'entity': table, 'entity_pk': int(pk), 'action': action,
        'merchant_id': _merchant_of(obj, table, session, cache),
        'store_id': getattr(obj, 'store_id', None),
        'actor_kind': kind, 'actor_id': actor_id, 'actor_role': role,
        'request_id': diag.get_request_id() or '',
        'source': 'system' if kind == 'system' else 'web',
        'diff_json': json.dumps(diff, ensure_ascii=False, sort_keys=True),
        # Core INSERT 不走 ORM 的列默认值，时间戳必须在这里显式给
        'changed_at': datetime.now(UTC).replace(tzinfo=None),
    }


def _collect(session, new=False):
    """一次 flush 内的审计载荷。异常向上抛出，由监听器统一吞掉并计数。

    新建行必须分两趟取，而且第二趟不能用 ORM 对象：INSERT 之前主键还没分配
    （`obj.id` 是 None），写在 before_flush 里只会「记到了动作、记不到是哪一条」；
    而放到 after_flush 里再 `session.add()` 的话，该行赶不上本次 flush、
    commit 后又不会被提交 —— 等于「审计看着写了，其实一行为零」。
    所以 after_flush 这一趟走 **Core INSERT**，与业务写同一个连接、同一个事务。
    """
    payloads = []
    cache = {}
    if is_silenced():
        return payloads
    sources = ((session.dirty, 'update'), (session.deleted, 'delete')) if not new \
        else ((session.new, 'create'),)
    for objs, default_action in sources:
        for obj in objs:
            action = None if new else _pending_action(obj)
            payload = _payload_for(obj, action or default_action, session, cache)
            if payload is not None:
                payloads.append(payload)
    return payloads


def _write(session, payloads, as_core):
    from src.models import ChangeLog

    if not payloads:
        return
    if as_core:
        session.execute(insert(ChangeLog.__table__), payloads)
    else:
        session.add_all([ChangeLog(**p) for p in payloads])
    _counters['written'] += len(payloads)


def _before_flush(session, flush_context, instances):
    # 审计写失败必须不影响业务写：吞掉 + 计数，计数经 /healthz 与门禁第 40 项可见
    try:
        with session.no_autoflush:
            payloads = _collect(session, new=False)
        _write(session, payloads, as_core=False)
    except Exception:                      # noqa: BLE001
        _counters['failed'] += 1


def _after_flush(session, flush_context):
    try:
        with session.no_autoflush:
            payloads = _collect(session, new=True)
        _write(session, payloads, as_core=True)
    except Exception:                      # noqa: BLE001
        _counters['failed'] += 1


_INSTALL = {'done': False, 'lock': threading.Lock()}


def install():
    """注册 flush 监听（幂等，Session 类级 → 覆盖全部会话）。"""
    if _INSTALL['done']:
        return True
    with _INSTALL['lock']:
        if not _INSTALL['done']:
            event.listen(Session, 'before_flush', _before_flush)
            event.listen(Session, 'after_flush', _after_flush)
            _INSTALL['done'] = True
    return _INSTALL['done']


def enabled_by_config(app=None):
    """开关读法（第 23 轮 S21 的教训：登记了却不参与判定的键就是假开关）。

    `CHANGE_LOG_ENABLED=0` 时 `install()` 直接不挂监听 —— 本模块因此可被 A/B：
    关掉归因链跑一遍，任何"只有开着才出现"的现象都能当场归因到它。
    """
    if app is not None and 'CHANGE_LOG_ENABLED' in app.config:
        return bool(app.config.get('CHANGE_LOG_ENABLED'))
    return os.environ.get('CHANGE_LOG_ENABLED', '1').strip().lower() not in ('0', 'false', 'off')


def init_app(app):
    if enabled_by_config(app):
        install()
    else:
        _counters['disabled'] = 1
        logger.warning('[change_log] CHANGE_LOG_ENABLED=0，变更归因链未挂载（业务写不受影响）')
    return app


def _mapped_tables():
    import src.models as m

    return {cls.__tablename__: cls for cls in vars(m).values()
            if isinstance(cls, type) and hasattr(cls, '__tablename__')}


def _value_fields_problems(by_table):
    """值档：表必须在、列必须在、列不得是凭据/明细列。"""
    problems = []
    for table, fields in TRACKED.items():
        cls = by_table.get(table)
        if cls is None:
            problems.append(f'表不存在于映射: {table}')
            continue
        cols = {c.name for c in cls.__table__.columns}
        problems += [f'列不存在: {table}.{name}' for name in fields if name not in cols]
        problems += [f'禁录列被声明跟踪: {table}.{name}'
                     for name in fields if name in cols and name in FORBIDDEN_FIELDS]
    return problems


def _membership_problems(by_table):
    """三档名册必须互为补集：脱敏/不跟踪档指到空表 = 对空气豁免，漏登记 = 静默不记。"""
    problems = []
    for tables, label in ((REDACTED_TABLES, '脱敏档'), (UNTRACKED, '不跟踪档')):
        problems += [f'{label}表不存在: {t}' for t in tables if t not in by_table]
    registered = set(TRACKED) | set(REDACTED_TABLES) | set(UNTRACKED)
    problems += [f'表未登记任何一档（既没跟踪也没写明不跟踪）: {t}'
                 for t in by_table if t not in registered]
    return problems


def validate_registry():
    """名册自证：声明的表名与列名必须在 ORM 映射里真的存在，且没踩禁录列。

    为什么必须单独有这一条：`_values()` 用 `getattr(obj, name, None)`，写错名字不会报错，
    只会把该字段永远记成 null —— 审计「看起来在记、其实记的是空气」。
    返回问题清单（空 = 名册可信），由 L4 与门禁判红，不在运行期抛异常打断请求。

    拆成 `_value_fields_problems` / `_membership_problems` 不是美化：第 37 项的 C901
    基线是**只降不升**的棘轮，合在一起复杂度 12 会把基线从 17 抬到 18。
    按第 25 轮的口径 —— 棘轮拦到自己时改代码，不抬基线、不加豁免。
    """
    by_table = _mapped_tables()
    return _value_fields_problems(by_table) + _membership_problems(by_table)


def recent(limit=100, merchant_id=None, store_ids=None, entity=None, action=None,
           merchant_is_null=False):
    """商家侧变更记录视图查询。

    严格商家归属：`merchant_id` 为空的行不对商家侧展示（安全红线）。
    「看不见」不等于「安全」—— 所以平台侧走 `merchant_is_null=True` 单独取那一档，
    两边各自有明确出口，而不是共用一个「不过滤就等于全给」的默认值。
    """
    from src.models import ChangeLog

    query = ChangeLog.query
    if merchant_is_null:
        query = query.filter(ChangeLog.merchant_id.is_(None))
    if merchant_id is not None:
        query = query.filter(ChangeLog.merchant_id == merchant_id)
    if store_ids is not None:
        query = query.filter(ChangeLog.store_id.in_(tuple(store_ids)))
    if entity:
        query = query.filter(ChangeLog.entity == entity)
    if action:
        query = query.filter(ChangeLog.action == action)
    return query.order_by(ChangeLog.changed_at.desc(), ChangeLog.id.desc()).limit(limit).all()


def parse_diff(row):
    """审计行的 diff 反序列化；坏 JSON 一律回空 dict 并由调用方显示「不可解析」。"""
    try:
        loaded = json.loads(row.diff_json or '{}')
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
