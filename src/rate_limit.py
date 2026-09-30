"""接口级限流 — 进程内滑动窗口（轻量防刷/防高频）

单店场景够用：waitress 多线程共享进程内状态。
生产多进程部署需换 Redis（不引入，超范围；本模块注释注明即可）。
哨兵键 = 「用户身份 + 动作」：登录用户用 staff_id/admin_id，匿名用 IP。
"""
import hashlib
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from functools import wraps

from flask import jsonify, render_template, request
from flask_login import current_user

logger = logging.getLogger(__name__)

# 动作 → (窗口秒数, 次数上限)
DEFAULT_LIMITS = {
    'submit': (60, 10),   # 报表提交：10 次 / 60s
    'chat': (60, 30),     # 情景对话/助手问答：30 次 / 60s
    'quiz': (60, 30),     # 考核作答入库：30 次 / 60s（防灌库，正常答题远低于此频）
    'login': (60, 10),    # 登录尝试：10 次 / 60s（缓解口令暴力试解）
    # P7-E / S-6：管理端写接口此前完全无限流（industry 7 条 + admin 8 条裸奔），
    # 单次保存可提交 12×20000 字符且每次触发全局缓存失效，需削峰。
    # 取 60 次 / 60s：既挡脚本化高频写入，又给批量操作留足余量。
    'admin_write': (60, 60),
    # PH2-3B：注册发码/验证码校验（防邮件轰炸与验证码爆破）
    'register': (60, 5),
}

# 内存滑动窗口：key -> deque[(timestamp,)]；maxlen 由窗口容量推算
_buckets = defaultdict(deque)
_BUCKET_LOCK = threading.Lock()


def rate_limit(action, limit=None, window=None):
    """装饰器：限流超限返回 429 错误页（复用 429.html）。

    Args:
        action: 动作名（'submit'/'chat'），默认限值取 DEFAULT_LIMITS[action]；
        limit/window: 可覆盖默认值（测试用可缩小窗口验证）。
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            check_result = check_limit(action, limit, window)
            if check_result is not None:
                return check_result
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def rate_limit_post(action, limit=None, window=None):
    """装饰器：只对**写方法**限流（P7-E / S-6）。

    管理端多为 GET/POST 同址路由（列表页 + 提交），GET 不应消耗写额度；
    且未登录请求应先被 `login_required / admin_required` 拦下（302/403），
    故本装饰器放在权限装饰器**下方**，只对通过权限校验的写请求计数。
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                limited = check_limit(action, limit, window)
                if limited is not None:
                    return limited
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def check_limit(action, limit=None, window=None):
    """函数内限流检查（适合 GET/POST 混合路由只在 POST 分支触发）。

    返回 None = 放行；返回 Response = 429（HTML 或 JSON，按请求类型）。
    环境变量 DISABLE_RATE_LIMIT=1 时跳过（冒烟/压测环境使用）。
    """
    if os.environ.get('DISABLE_RATE_LIMIT') == '1':
        return None
    default_window, default_limit = DEFAULT_LIMITS.get(action, (60, 10))
    win = window or default_window
    lim = limit or default_limit

    key = _bucket_key(action)
    now = time.time()
    with _BUCKET_LOCK:
        bucket = _buckets[key]
        # 全局桶数量兜底（防长驻进程下匿名 IP 键无界增长拖内存）：
        # 超过 1024 个键时，清理「最新记录已超出最大窗口」的过期桶
        if len(_buckets) > 1024:
            stale = [k for k, v in _buckets.items()
                     if not v or now - v[-1][0] > 60]
            for k in stale:
                _buckets.pop(k, None)
        # 清理过期记录（滑动窗口）
        while bucket and now - bucket[0][0] > win:
            bucket.popleft()
        if len(bucket) >= lim:
            over_limit = True
        else:
            over_limit = False
            bucket.append((now,))
    if over_limit:
        # S5：触发即留痕（此前限流是否生效完全不可见，只能靠猜）
        log_rate_limit_event(action, key, lim, win)
        if request_wants_json():
            return jsonify(ok=False, reply='操作太频繁啦，请稍后再试'), 429
        return render_template('429.html', message='操作太频繁啦，请歇一会儿再试。'), 429
    return None


def window_usage(action, limit=None, window=None):
    """只读回报当前窗口用量，供开放 API 回写 `X-RateLimit-*` 头。

    与 check_limit 的区别：本函数**不占用配额**（不 append、不 popleft 落盘），
    因此可以在 after_request 里对每个响应安全调用。DISABLE_RATE_LIMIT=1 时返回
    None —— 限流都关了还回头寸等于撒谎，宁可不发头。

    Args:
        action: 与 check_limit 同一个动作名。
        limit: 覆盖默认上限（取 DEFAULT_LIMITS[action]）。
        window: 覆盖默认窗口秒数。

    Returns:
        dict | None: {'limit': int, 'remaining': int, 'reset_in': int}。
    """
    if os.environ.get('DISABLE_RATE_LIMIT') == '1':
        return None
    default_window, default_limit = DEFAULT_LIMITS.get(action, (60, 10))
    win = window or default_window
    lim = int(limit or default_limit)
    now = time.time()
    with _BUCKET_LOCK:
        bucket = _buckets.get(_bucket_key(action))
        stamps = [ts for (ts,) in (bucket or []) if now - ts <= win]
    used = len(stamps)
    reset_in = int(max(0, win - (now - min(stamps)))) if stamps else int(win)
    return {'limit': lim, 'remaining': max(0, lim - used), 'reset_in': reset_in}


def _bucket_key(action):
    """哨兵键：登录用户用 id，匿名用 IP，动作前缀隔离.
    无请求上下文（如纯函数单测）时退化为匿名+空 IP，保证可测性。
    """
    from flask import has_request_context, session
    if has_request_context():
        prefix = 'anon'
        if current_user.is_authenticated:
            # P7-E / S-11：Admin.id 与 Staff.id 是两张表各自自增，只用 id 会让
            # admin-1 与 staff-1 撞进同一个桶（正常用户被互相挤占配额 → 误 429）。
            # session['_user_id'] 形如 'admin-1'（见 app.load_user），天然带角色。
            prefix = session.get('_user_id') or str(current_user.id)
        remote = request_ip()
    else:
        prefix = 'anon'
        remote = 'test'
    return f'{action}:{prefix}:{remote}'


def request_ip():
    """客户端 IP（P7-E / S-4）：一律以 ProxyFix 校正后的 `remote_addr` 为准。

    旧实现无条件信任 `X-Forwarded-For` 首值，而 app.py 的 ProxyFix 只开了
    `x_proto`（协议头）、未开 `x_for` —— XFF 从未被代理改写，攻击者每次请求
    换一个 XFF 就是全新桶，`/login` 10 次/60s 形同虚设。

    现由 `ProxyFix(x_for=TRUST_PROXY_HOPS)` 统一校正（默认 1 跳），本函数只读结果；
    **直连部署（前面没有反向代理）请把 TRUST_PROXY_HOPS 设为 0**，否则伪造的
    XFF 会反向污染 remote_addr。
    """
    return request.remote_addr or '0.0.0.0'  # noqa: S104  理由：这是取不到客户端 IP 时的**键名兜底字符串**，不是监听地址，未做任何 bind


# ── 限流留痕（2026-09-18 双向迭代 S5）──
# 沿用本项目 src/ai_trace.py 的 JSONL 落盘模式：单进程免费层不引入新存储依赖，
# 触发后「限流到底有没有生效、谁在被限」才可被举证（原本触发零可见性）。
# 只落指纹不落原始 IP/身份；任何写入异常都不得影响限流主流程。
RATE_TRACE_PATH = os.environ.get('RATE_TRACE_PATH') or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'rate_limit_events.jsonl'
)
RATE_TRACE_MAX_BYTES = 1 * 1024 * 1024
RATE_TRACE_BACKUPS = 3


def _rotate_rate_trace_if_needed():
    try:
        if not os.path.exists(RATE_TRACE_PATH) \
                or os.path.getsize(RATE_TRACE_PATH) < RATE_TRACE_MAX_BYTES:
            return
        for idx in range(RATE_TRACE_BACKUPS, 0, -1):
            src = RATE_TRACE_PATH if idx == 1 else f'{RATE_TRACE_PATH}.{idx - 1}'
            dst = f'{RATE_TRACE_PATH}.{idx}'
            if not os.path.exists(src):
                continue
            if os.path.exists(dst):
                os.remove(dst)
            os.replace(src, dst)
    except OSError as exc:
        # Q3：轮转失败不得影响限流主流程，但必须可观测（否则 jsonl 无界增长不可见）
        logger.warning('限流留痕轮转失败（保留原文件继续追加）: %s', exc)


def _fingerprint(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()[:8]


def log_rate_limit_event(action, bucket_key, limit, window):
    try:
        os.makedirs(os.path.dirname(RATE_TRACE_PATH), exist_ok=True)
        rec = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'action': action,
            'key_fp': _fingerprint(bucket_key),  # 脱敏：绝不落原始 IP / 用户 id
            'limit': limit,
            'window': window,
        }
        with _BUCKET_LOCK:
            _rotate_rate_trace_if_needed()
            with open(RATE_TRACE_PATH, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        return rec
    except Exception as exc:  # 可观测性优先级低于可用性
        print(f'[rate_limit] 留痕失败（不影响限流）: {exc}')
        return None


def recent_rate_limit_events(n=20):
    """读取最近 n 条限流留痕（供排查/面板使用；文件不存在返回空列表）。"""
    try:
        with open(RATE_TRACE_PATH, encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def request_wants_json():
    """前端 AJAX fetch 请求判定（与 ai.py 的 X-Requested-With: fetch 口径一致）。"""
    from flask import has_request_context
    if not has_request_context():
        return False
    return request.headers.get('X-Requested-With') == 'fetch'
