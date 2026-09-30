"""OPT-A Webhook 事件外发 —— 填报提交 / 任务状态变更事件推送到商家配置的 URL。

对标 bigcapital 的 webhook 集成能力（差距清单 G3），为「老板小程序自动导入订单」
红线预留兑现路径。

设计约束（逐条对应任务卡第 4 节与 PH5 并发边界）
────────────────────────────────────────────────
1. **绝不阻断主链路**：主请求线程只做一次「本商家是否有活跃订阅」的索引查询；
   无订阅（含 WEBHOOK_ENABLED 关闭时）直接返回，零副作用、零额外线程。
2. **不增加 >50ms**：命中订阅后以 fire-and-forget 守护线程完成出站 HTTPS，
   主线程立即返回；并发在途数受信号量约束，溢出即丢弃并留痕，不排队堆积。
3. **禁止跨线程复用 Flask 会话**：线程内不再触碰请求线程的 db.session；
   订阅信息在请求线程内读成普通 dict 后传入，签名密钥在请求线程内派生。
4. **失败可见、可复查、有界重试（第 30 轮改造）**：出站目标一律先过 `src/outbound_guard.py`
   （SSRF 面：环回/私网/链路本地元数据/内网名/userinfo/非白名单端口一律拒），
   每次尝试写一行 `webhook_delivery`（attempt / 状态码 / 错误类 / 是否被守卫拦下 / 下次重试时间）。
   仍**不阻断主链路**：留痕写库失败只计数不抛出。重试用固定退避表，
   连续失败达阈值的订阅自动 `is_active=False`（并被 change_log 记成一次字段级变更）。
5. **签名密钥不落库明文**：出站 HMAC-SHA256 密钥由 SECRET_KEY 与订阅随机盐派生，
   数据库单独泄露也无法伪造合法签名。
6. **不泄露业务明细**：留痕只记事件类型 / 商家 id / 订阅 id / HTTP 状态 / 错误类名 /
   payload 的 SHA256，绝不写入 payload 正文、签名密钥或令牌（安全与盲审红线）。
   投递目标是商家提供的 URL ⇒ 它同时也是**服务端出站点**：`allow_redirects=False`，
   http 目标按"已判定的解析结果 IP 直连 + 原 Host 头"发送，掐掉
   「创建时解析到公网、发送时解析到内网」的 DNS-rebinding 时差（Discourse
   `lib/final_destination.rb:267-276` 同法）。https 不钉 IP（会破坏证书主机名校验），
   改为临发前重新解析并按结果判，留痕里 `pinned=False` 如实记录这条腿弱在哪。
"""
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# 已支持的事件类型（唯一真相源，供订阅校验与文档）
SUPPORTED_EVENTS = ('sales.submitted', 'task.status_changed')

SIGNATURE_HEADER = 'X-ICAN-Signature'
EVENT_HEADER = 'X-ICAN-Event'
DELIVERY_HEADER = 'X-ICAN-Delivery'
SIGNATURE_SCHEME = 'sha256'
# 时间窗：±300s。太紧会让慢链路（商家侧 HTTPS 重试、NTP 偏差几分钟）误判合法签名，
# 太松则重放窗口等于窗口本身；300s 是 Stripe/Sentry 采用的同一量级。
SIGNATURE_TOLERANCE_SECONDS = 300
# 出站超时的**兜底值**：与 `src/config.py` 的 `_env_int('WEBHOOK_TIMEOUT', 3)` 同数。
# 两处兜底不一致就是「配置没说时到底几秒」的第二个真相源（本轮我自己先写成了 4）。
DEFAULT_TIMEOUT_SECONDS = 3

# 有界出站留痕（沿用 src/rate_limit.py 的 JSONL + 轮转模式，免费层不引新存储依赖）
TRACE_PATH = os.environ.get('WEBHOOK_TRACE_PATH') or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'webhook_events.jsonl')
TRACE_MAX_BYTES = 1 * 1024 * 1024
TRACE_BACKUPS = 3

# 在途投递并发闸：首次读取配置时惰性建闸（信号量容量固定，够免费层演示并发）。
# 用可变容器承载惰性单例，规避 global 语句（沿用仓内「dict 容器替代 global」范式）。
_GATE = {'sem': None}
_GATE_LOCK = threading.Lock()

# 退避重试（第 30 轮 H82，对标 Frappe `webhook.py:198/216-225`：`next_retry` 是**盘上物件**，
# 到期由作业扫 `status=Failed ∧ next_retry<=now` 重投 —— 不是内存里的计数器，重启即失忆）。
# 索引 = 已发生的尝试次数（1 起）；表外 ⇒ 放弃并留下 next_retry=NULL 的终态行。
RETRY_BACKOFF_MINUTES = {1: 5, 2: 30}
ATTEMPT_LIMIT = 3
#: 连续失败达到这个条数 ⇒ 停用该订阅（Stripe 对持续失败端点同一姿态）。计数取自
#: `webhook_delivery` 里"每个 delivery 的最新一次尝试"，不看总失败数 —— 否则一个长期
#: 稳定但偶发失败的端点会被历史累计误杀。
DISABLE_AFTER_CONSECUTIVE = 5
# 留痕写库失败计数：吞异常是必要的（不得因审计打断业务），但**不得无痕**（stats 可见）。
_STATS = {'record_failed': 0, 'blocked': 0, 'retried': 0, 'disabled': 0}


def stats():
    """出站投递面的自读数（供 /healthz 与门禁回执取用，只有聚合整数）。"""
    return dict(_STATS)


def _inflight_gate(max_inflight):
    """返回（必要时创建）在途投递信号量；容量取首次生效值。"""
    if _GATE['sem'] is None:
        with _GATE_LOCK:
            if _GATE['sem'] is None:
                _GATE['sem'] = threading.BoundedSemaphore(max(1, int(max_inflight)))
    return _GATE['sem']


def signing_secret(secret_key, salt):
    """由服务端 SECRET_KEY 与订阅随机盐派生出站签名密钥（不落库明文）。

    Args:
        secret_key: 应用 SECRET_KEY（服务器侧机密）。
        salt: 订阅创建时生成的随机十六进制盐。

    Returns:
        str: 派生签名密钥（hex）。相同入参恒定，供签名与校验两端一致复算。
    """
    msg = ('ican-webhook:' + (salt or '')).encode('utf-8')
    return hmac.new(str(secret_key).encode('utf-8'), msg, hashlib.sha256).hexdigest()


def _digest(secret, timestamp, body_bytes):
    """签名串唯一口径：`"<t>." + body` —— 签名与校验两端都只经此函数，不分叉。"""
    payload = (f'{timestamp}.'.encode('ascii') + body_bytes)
    return hmac.new(str(secret).encode('utf-8'), payload, hashlib.sha256).hexdigest()


def sign_body(secret, body_bytes, timestamp=None):
    """计算带时间戳的签名头值：`t=<unix>,sha256=<hex>`。

    时间戳必须进签名串：只签 body 时，攻击者抓到任意一次合法请求就能无限重放
    同一份签名（对标 Stripe/Sentry/Shopify：签名串一律为 "<t>.<body>"）。

    Args:
        secret: signing_secret() 派生的签名密钥。
        body_bytes: 序列化后的 JSON 请求体字节。
        timestamp: 覆盖时间戳（仅供测试造过期样本；生产不传）。

    Returns:
        str: 形如 't=1760000000,sha256=<hex>' 的签名头值。
    """
    ts = int(time.time() if timestamp is None else timestamp)
    return f't={ts},{SIGNATURE_SCHEME}={_digest(secret, ts, body_bytes)}'


def parse_signature_header(header_value):
    """解签名头 → (timestamp 或 None, [digest, ...])；时间戳无法识别时返回 None。

    逗号分隔的多个 `sha256=` 是有意允许的形状：轮换签名密钥的窗口内，同一事件可带
    新旧两把密钥的签名，接收方任一匹配即认。签名侧当前不产出多签名（无生产调用方，
    不留永远跑不到的代码），但契约与校验逻辑已按可扩展形状实现。
    """
    ts = None
    digests = []
    for part in (header_value or '').split(','):
        key, sep, val = part.strip().partition('=')
        if not sep:
            continue
        if key == 't':
            try:
                ts = int(val)
            except (TypeError, ValueError):
                return None, []
        elif key == SIGNATURE_SCHEME:
            digests.append(val.strip())
    return ts, digests


def verify_signature(secret, body_bytes, header_value, tolerance_seconds=None, now=None):
    """校验签名头：格式 + 时间窗 + 常量时间比较，三者缺一即拒。

    使用常量时间比较，避免按字节早停造成的计时侧信道。

    Args:
        secret: 该订阅的签名密钥。
        body_bytes: 收到的原始请求体字节（须与发送端逐字节一致）。
        header_value: `X-ICAN-Signature` 头原值。
        tolerance_seconds: 允许的时间偏差，默认 SIGNATURE_TOLERANCE_SECONDS。
        now: 覆盖「当前时间」（仅供测试稳定断言过期分支）。
    """
    ts, digests = parse_signature_header(header_value)
    if ts is None or not digests:
        return False
    limit = SIGNATURE_TOLERANCE_SECONDS if tolerance_seconds is None else int(tolerance_seconds)
    ref = int(time.time() if now is None else now)
    if abs(ref - ts) > limit:
        return False
    expected = _digest(secret, ts, body_bytes)
    return any(hmac.compare_digest(expected, cand) for cand in digests)


def build_payload(event_type, merchant_id, data):
    """构造外发事件体（普通 dict，可 JSON 序列化）。"""
    return {
        'event': event_type,
        'merchant_id': int(merchant_id),
        'event_id': uuid.uuid4().hex,
        'occurred_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'data': data,
    }


def _rotate_trace_if_needed():
    try:
        if not os.path.exists(TRACE_PATH) or os.path.getsize(TRACE_PATH) < TRACE_MAX_BYTES:
            return
        for idx in range(TRACE_BACKUPS, 0, -1):
            src = TRACE_PATH if idx == 1 else f'{TRACE_PATH}.{idx - 1}'
            dst = f'{TRACE_PATH}.{idx}'
            if not os.path.exists(src):
                continue
            if os.path.exists(dst):
                os.remove(dst)
            os.replace(src, dst)
    except OSError as exc:
        logger.warning('Webhook 留痕轮转失败（保留原文件继续追加）: %s', exc)


def _trace(record):
    """落一条有界出站留痕；任何写盘异常都被吞掉（可用性优先于可观测性）。"""
    try:
        os.makedirs(os.path.dirname(TRACE_PATH), exist_ok=True)
        with _GATE_LOCK:
            _rotate_trace_if_needed()
            with open(TRACE_PATH, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as exc:  # 留痕失败绝不影响业务
        logger.debug('Webhook 留痕失败（忽略）: %s', exc)


def _utc_naive():
    """库内时间列统一口径：UTC-naive（沿用 models.py 既有形态，本轮不动列语义）。"""
    return datetime.now(UTC).replace(tzinfo=None)


def next_retry_at(attempt, now=None):
    """第 `attempt` 次尝试失败后该在什么时候重试；返回 None 表示已放弃（终态）。"""
    minutes = RETRY_BACKOFF_MINUTES.get(int(attempt))
    if minutes is None:
        return None
    return (now or _utc_naive()) + timedelta(minutes=minutes)


def _record_attempt(app, base, result, now=None):
    """把一次投递尝试落成 `webhook_delivery` 一行 —— 投递结果必须是可查的物件。

    写失败只计数（`stats().record_failed`）不抛出：审计面不得反过来打断业务出站，
    但也不许"吞了就算没事"——计数本身就是要被门禁与 /healthz 看见的读数。
    `base` 由 `_deliver_one` 组（`sub_id/merchant_id/event/delivery_id/attempt/payload_sha256`）：
    把六个入参收成一个物件不是为了绕参数上限，而是因为"漏一个/传错位"这类失效在长签名里
    是静默的 —— 本轮 `now` 就是这样被丢掉的（见 §报告 G4）。
    """
    from src.models import WebhookDelivery, db

    try:
        with app.app_context():
            try:
                db.session.add(WebhookDelivery(
                    subscription_id=int(base['sub_id']), merchant_id=base['merchant_id'],
                    delivery_id=base['delivery_id'], event=base['event'],
                    attempt=int(base['attempt']),
                    ok=bool(result.get('ok')), status_code=result.get('status_code'),
                    error_class=(result.get('error_class') or '')[:40] or None,
                    payload_sha256=base['payload_sha256'], http_ms=result.get('http_ms'),
                    blocked=bool(result.get('blocked')),
                    # 成功的那一行不得带 next_retry：否则表里同时存在「ok=True」和
                    # 「排在 5 分钟后重试」两种互相矛盾的读法，靠 `ok` 过滤才没被重投，
                    # 但把「已送达」的排程字段留成非空，任何按 next_retry 出的报表都会多算待发。
                    next_retry=None if result.get('ok') else next_retry_at(base['attempt'], now),
                    created_at=now or _utc_naive()))
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
            finally:
                db.session.remove()
        return True
    except Exception as exc:
        _STATS['record_failed'] += 1
        logger.debug('Webhook 投递留痕写库失败（忽略）: %s', exc)
        return False


#: `_deliver_one` 的注入位。**写错键名当场抛** —— 参数被静默忽略就是本轮实测到的真失效
#: （`now` 曾经是死参：签名里有、函数体里没人用，注入时钟整套断言因此测的是空气）。
_SEAMS = frozenset({'timeout', 'attempt', 'delivery_id', 'resolver', 'post', 'now'})


def _deliver_one(sub, payload_dict, app=None, seam=None):
    """单次出站投递：守卫 → 签名 → POST（禁重定向，http 按解析 IP 直连）→ 落一行留痕。

    `seam` 是注入位（`resolver` 喂假 DNS / `post` 换 HTTP 客户端 / `now` 注入时钟 /
    `attempt`+`delivery_id` 供重试续号 / `timeout`）。**不为此开"允许私网"的环境开关**：
    那等于把洞留给生产。
    """
    from src import outbound_guard

    seam = dict(seam or {})
    unknown = sorted(set(seam) - _SEAMS)
    if unknown:
        raise ValueError(f'未知注入位 {unknown}，合法键：{sorted(_SEAMS)}')
    timeout = seam.get('timeout', DEFAULT_TIMEOUT_SECONDS)
    attempt = int(seam.get('attempt', 1))
    resolver, post, now = seam.get('resolver'), seam.get('post'), seam.get('now')
    if post is None:
        import requests

        post = requests.post
    body = json.dumps(payload_dict, ensure_ascii=False).encode('utf-8')
    delivery_id = seam.get('delivery_id') or uuid.uuid4().hex
    stamp = now or _utc_naive()
    base = {'event': payload_dict['event'], 'merchant_id': payload_dict['merchant_id'],
            'sub_id': sub['id'], 'attempt': attempt, 'delivery_id': delivery_id,
            'payload_sha256': hashlib.sha256(body).hexdigest()}
    verdict = outbound_guard.check_target(sub['url'], resolver=resolver)
    if not verdict['ok']:
        _STATS['blocked'] += 1
        result = dict(base, ok=False, blocked=True, status_code=None, http_ms=None,
                      error_class=f'BlockedTarget:{verdict["reason"][:24]}')
        _trace(dict(result, reason=verdict['reason'], url_host=verdict['host']))
        if app is not None:
            _record_attempt(app, base, result, now=stamp)
        return result
    pinned = outbound_guard.pin_http_url(sub['url'], verdict['ip'])
    target, host_header = pinned if pinned else (sub['url'], '')
    headers = {
        'Content-Type': 'application/json',
        EVENT_HEADER: payload_dict['event'],
        DELIVERY_HEADER: delivery_id,
        SIGNATURE_HEADER: sign_body(sub['secret'], body),
    }
    if host_header:
        headers['Host'] = host_header
    started = time.perf_counter()
    result = dict(base, ok=False, blocked=False, status_code=None, http_ms=None, error_class=None)
    try:
        resp = post(target, data=body, headers=headers, timeout=timeout, allow_redirects=False)
        result['status_code'] = int(resp.status_code)
        result['ok'] = 200 <= result['status_code'] < 300
        if not result['ok'] and result['status_code'] in (301, 302, 303, 307, 308):
            # 重定向被禁是**策略**，不是故障：记成可区分的错误类，别混进"网络失败"里查不出方向
            result['error_class'] = 'RedirectForbidden'
        elif not result['ok']:
            result['error_class'] = f'Http{result["status_code"]}'
    except Exception as exc:
        result['error_class'] = type(exc).__name__
    result['http_ms'] = int((time.perf_counter() - started) * 1000)
    _trace({'event': result['event'], 'merchant_id': result['merchant_id'],
            'sub_id': result['sub_id'], 'attempt': attempt, 'ok': result['ok'],
            'status': result['status_code'], 'pinned': bool(pinned),
            'error': result['error_class'], 'ms': result['http_ms']})
    if app is not None:
        _record_attempt(app, base, result, now=stamp)
    return result


def consecutive_failures(sub_id, limit=DISABLE_AFTER_CONSECUTIVE):
    """该订阅最近 `limit` 次投递（按每个 delivery 的最终一次尝试）是否**全失败**。

    取"每个 delivery 的最新 attempt"而不是"所有行"：一次事件重试 3 次全败只算一次失败，
    否则一个偶发抖动就会被记成三次，把阈值悄悄压短。
    """
    from sqlalchemy import func

    from src.models import WebhookDelivery, db

    latest = (db.session.query(WebhookDelivery.subscription_id, WebhookDelivery.delivery_id,
                               func.max(WebhookDelivery.attempt).label('a'))
              .filter(WebhookDelivery.subscription_id == int(sub_id))
              .group_by(WebhookDelivery.subscription_id, WebhookDelivery.delivery_id).subquery())
    rows = (db.session.query(WebhookDelivery.ok)
            .join(latest, (WebhookDelivery.delivery_id == latest.c.delivery_id)
                  & (WebhookDelivery.attempt == latest.c.a))
            .filter(WebhookDelivery.subscription_id == int(sub_id))
            .order_by(WebhookDelivery.id.desc()).limit(limit).all())
    return len(rows) >= limit and not any(r[0] for r in rows)


def _maybe_disable(app, sub_id, now=None):
    """连续失败达阈值 ⇒ 停用订阅（字段级变更由 change_log 监听器自动留痕）。"""
    from src.models import WebhookSubscription, db

    if not consecutive_failures(sub_id):
        return False
    with app.app_context():
        try:
            row = db.session.get(WebhookSubscription, int(sub_id))
            if row is None or not row.is_active:
                return False
            row.is_active = False
            db.session.commit()
        finally:
            db.session.remove()
    _STATS['disabled'] += 1
    _trace({'event': 'webhook.disabled', 'sub_id': int(sub_id), 'ok': False,
            'error': f'consecutive_failures>={DISABLE_AFTER_CONSECUTIVE}'})
    return True


def _worker(subs, payload_dict, gate, app=None, seam=None):
    """守护线程主体：投递本事件命中的全部订阅，结束后释放并发闸。"""
    try:
        for sub in subs:
            _deliver_one(sub, payload_dict, app=app, seam=seam)
    finally:
        gate.release()


def notify(event_type, merchant_id, data):
    """业务写库成功后调用；命中活跃订阅则以受限并发守护线程外发，否则零副作用返回。

    必须在业务事务 **commit 之后** 调用：投递失败不回滚、也不影响已落库的业务数据。
    主线程只做一次索引查询与（可选）派生密钥计算，出站 HTTPS 全在工作线程完成。

    Args:
        event_type: SUPPORTED_EVENTS 之一。
        merchant_id: 事件所属商家 id。
        data: 事件明细（普通 dict，仅含本商家自有数据；不含令牌/密钥/他商家信息）。

    Returns:
        bool: True 表示已受理（已进入投递队列或已派线程）；False 表示功能关闭 / 无订阅 /
            并发溢出丢弃。本函数不抛异常。
    """
    try:
        from flask import current_app

        from src.models import WebhookSubscription
        cfg = current_app.config
        if not cfg.get('WEBHOOK_ENABLED'):
            return False
        # 请求线程内读订阅（一次索引查询），并即时派生签名密钥 —— 工作线程不再碰 DB。
        rows = (WebhookSubscription.query
                .filter_by(merchant_id=int(merchant_id), is_active=True).all())
        subs = []
        secret_key = cfg.get('SECRET_KEY')
        for row in rows:
            types = (row.event_types or '').strip()
            if types != '*' and event_type not in [t.strip() for t in types.split(',') if t.strip()]:
                continue
            subs.append({'id': row.id, 'url': row.url,
                         'secret': signing_secret(secret_key, row.secret_salt)})
        if not subs:
            return False
        payload_dict = build_payload(event_type, merchant_id, data)
        gate = _inflight_gate(cfg.get('WEBHOOK_MAX_INFLIGHT', 4))
        if not gate.acquire(blocking=False):
            # 并发溢出：丢弃而非堆积，保证主链路与既有投递不被拖慢
            _trace({'event': event_type, 'merchant_id': merchant_id,
                    'ok': False, 'error': 'dropped_max_inflight'})
            return False
        thread = threading.Thread(
            target=_worker,
            args=(subs, payload_dict, gate,
                  current_app._get_current_object(),
                  {'timeout': cfg.get('WEBHOOK_TIMEOUT', DEFAULT_TIMEOUT_SECONDS)}),
            daemon=True)
        thread.start()
        return True
    except Exception as exc:  # Webhook 任何异常都不得影响业务主链路
        logger.debug('Webhook notify 异常（忽略）: %s', exc)
        return False


def due_deliveries(now=None, limit=20):
    """到期待重试的投递行（同 delivery 的**最大 attempt** 那条才算数，避免重复排程）。

    必须在无请求上下文的调用里也可见 ⇒ 走 `query_guard.unscoped()`：
    重试是平台动作，跨全部商家扫，不能被行级租户守卫按"当前会话商家"滤成空集
    （滤空会被读成"没有要重试的"，那是最安静的一类失效）。
    """
    from sqlalchemy import func

    from src import query_guard
    from src.models import WebhookDelivery, db

    moment = now or _utc_naive()
    with query_guard.unscoped():
        latest = (db.session.query(WebhookDelivery.delivery_id,
                                   func.max(WebhookDelivery.attempt).label('a'))
                  .filter(WebhookDelivery.next_retry.isnot(None))
                  .group_by(WebhookDelivery.delivery_id).subquery())
        return (db.session.query(WebhookDelivery)
                .join(latest, (WebhookDelivery.delivery_id == latest.c.delivery_id)
                      & (WebhookDelivery.attempt == latest.c.a))
                .filter(WebhookDelivery.ok.is_(False), WebhookDelivery.next_retry <= moment)
                .filter(WebhookDelivery.blocked.is_(False))   # 被守卫拦下的不重试：那是策略判定
                .order_by(WebhookDelivery.next_retry.asc(), WebhookDelivery.id.asc())
                .limit(int(limit)).all())


def retry_due(app=None, now=None, limit=20, resolver=None, post=None):
    """跑一轮到期重试。返回读数 dict（CLI 与平台侧排障共用同一份出口）。

    免费层没有常驻 worker：PA 定时任务（每日）或运维手工 `python -m src.webhooks --retry-due`
    驱动。**刻意不自己起调度器** —— 一个没人知道的调度器比没有调度器更危险。
    """
    from flask import current_app

    from src.models import WebhookSubscription, db

    flask_app = app or current_app
    summary = {'scanned': 0, 'retried': 0, 'still_failed': 0, 'succeeded': 0, 'disabled': 0}
    with flask_app.app_context():
        try:
            rows = due_deliveries(now=now, limit=limit)
            for row in rows:
                summary['scanned'] += 1
                sub = db.session.get(WebhookSubscription, row.subscription_id)
                if sub is None or not sub.is_active:
                    row.next_retry = None
                    db.session.commit()
                    continue
                subs = {'id': sub.id, 'url': sub.url,
                        'secret': signing_secret(flask_app.config.get('SECRET_KEY'), sub.secret_salt)}
                payload = {'event': row.event, 'merchant_id': row.merchant_id,
                           'data': {'replay_of': row.delivery_id, 'attempt': row.attempt + 1}}
                attempt = int(row.attempt) + 1
                if attempt > ATTEMPT_LIMIT:
                    row.next_retry = None
                    db.session.commit()
                    continue
                result = _deliver_one(
                    subs, payload, app=flask_app,
                    seam={'timeout': flask_app.config.get('WEBHOOK_TIMEOUT', DEFAULT_TIMEOUT_SECONDS),
                          'attempt': attempt, 'delivery_id': row.delivery_id,
                          'resolver': resolver, 'post': post, 'now': now})
                _STATS['retried'] += 1
                summary['retried'] += 1
                if result['ok']:
                    summary['succeeded'] += 1
                else:
                    summary['still_failed'] += 1
                    if _maybe_disable(flask_app, sub.id, now=now):
                        summary['disabled'] += 1
            return summary
        finally:
            db.session.remove()


def main(argv=None):
    """离线出口：`python -m src.webhooks --retry-due`（有界重试一轮）/ `--stats`。

    没有 `--send-now <url>` 这类"直接帮我发一次"的入口 —— 那等于给 SSRF 留一个命令行面板。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--help' in argv or '-h' in argv:
        print(__doc__)
        return 0
    app = _bootstrap_app()
    if '--stats' in argv:
        print(f'[webhooks] stats={stats()}')
        return 0
    if '--retry-due' in argv:
        summary = retry_due(app=app)
        print(f'[webhooks] 重试一轮 {summary}')
        return 0
    print('用法：python -m src.webhooks [--retry-due|--stats|--help]', file=sys.stderr)
    return 2


def _bootstrap_app():
    from src.app import create_app

    return create_app()


__all__ = [
    'ATTEMPT_LIMIT',
    'DELIVERY_HEADER',
    'DISABLE_AFTER_CONSECUTIVE',
    'EVENT_HEADER',
    'RETRY_BACKOFF_MINUTES',
    'SIGNATURE_HEADER',
    'SIGNATURE_SCHEME',
    'SIGNATURE_TOLERANCE_SECONDS',
    'SUPPORTED_EVENTS',
    'build_payload',
    'consecutive_failures',
    'due_deliveries',
    'next_retry_at',
    'notify',
    'parse_signature_header',
    'retry_due',
    'sign_body',
    'signing_secret',
    'stats',
    'verify_signature',
]


if __name__ == '__main__':
    raise SystemExit(main())
