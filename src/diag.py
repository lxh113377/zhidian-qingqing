"""请求级观测核（对标第 19 轮 H36）—— 把「逐条 WARN」升级为「可回答影响面」。

为什么要这一层
──────────────
改造前的现状（当场实测，非推断）：
- `src/performance.py` 已有慢请求/慢查询 WARN，但**只打点不聚合**：故障后无法回答
  「这一小时有多少请求超阈值、集中在哪个路径」，只能人肉 grep 5MB×3 轮转日志。
- `src/ai_trace.py` 已把每次 AI 出站耗时写进 `data/ai_call_log.jsonl`，但记录里
  **没有 request_id**，与访问日志之间无法关联 —— 实测 `GET /admin/` 7249ms，
  同刻两条 AI 留痕 4040ms + 2982ms = 7022ms（占 97%）。也就是说「慢在 AI 还是慢在
  自己的代码」这个问题**数据早就有，只是串不起来**。
- `/healthz` 的 `status` 恒为 `'ok'`，与 `db='error'` 同时出现（字段自相矛盾）。

阈值依据（R236 补注③：判据阈值必须先量两侧边界）
──────────────────────────────────────────────
2026-09-26 用真实演示库（7 店 / 2100 流水）+ 规则版（`LLM_PROVIDER=none`）实测 14 条
路由 × 5 次 = 70 条采样：业务侧最慢 `max=157.3ms`（`/admin/`），中位最高 89.7ms；
在线 AI 侧最慢 `7249ms`（PH5 在线证据 P95 5625.4ms 同量级）。两侧区间不重叠，
`SLOW_REQUEST_MS=500` 距业务上界有 3.2 倍余量、距 AI 下界差一个数量级 ⇒ 阈值保留，
归属判定改由 request_id 关联完成（**不是靠调阈值掩盖「无法归因」这个机制缺陷**）。

口径
────
1. **有界**：路径聚合上限 `MAX_PATH_KEYS`，超限按最小计数淘汰并累计 `evictions`
   （淘汰必须可见，静默丢计数与没测等价）。
2. **fail-open**：观测不得让业务链路变红；取用失败只丢计数，不抛给调用方。
3. **不泄露内部情报**：`/healthz` 只出聚合整数，**不出路径名**（沿用第 10 轮 L22
   「schema 表名不进公开端点」的同一口径）。
4. **request_id 单一真相源**：ContextVar 而非 `flask.g` —— 线程池内（决策驾驶舱
   `ThreadPoolExecutor`）默认不继承上下文，必须由调用方显式 `copy_context().run()`
   传播，否则 AI 留痕又回到串不起来。
"""
import contextvars
import logging
import re
import threading
import time
from datetime import UTC, datetime

# 路径聚合上限：64 条 × 每条约 80B，量级 KB，免费层单进程可接受
MAX_PATH_KEYS = 64
# 异常指纹上限：同类异常聚合计数（对标 frappe `error_log.get_fingerprint_stats`），
# 只留类名，32 个类名足以覆盖真实故障面，超限即淘汰最小计数
MAX_KIND_KEYS = 32
# 慢查询按**模板**聚合（M58）：不同参数不得各占一格，否则上限形同虚设
MAX_SQL_KEYS = 32
SQL_TEMPLATE_LEN = 120
TOP_PATH_LIMIT = 10

_request_id_var = contextvars.ContextVar('ican_request_id', default='')
_lock = threading.Lock()

_state = {
    'requests': 0,
    'by_class': {},
    'errors_4xx': 0,
    'errors_5xx': 0,
    'slow': 0,
    'max_duration_ms': 0.0,
    'by_path': {},
    'error_kinds': {},
    'slow_queries': {},
    'evictions': 0,
    'dropped': 0,
    'started_at': time.time(),
}

__all__ = [
    'MAX_KIND_KEYS',
    'MAX_PATH_KEYS',
    'TOP_PATH_LIMIT',
    'clear_request_id',
    'get_request_id',
    'healthz_stats',
    'norm_path',
    'norm_sql',
    'note_exception',
    'note_query',
    'observe',
    'reset',
    'set_request_id',
    'snapshot',
    'submit',
    'utc_iso_z',
    'utc_now_naive',
]


def set_request_id(value):
    """写入本次请求的追踪 id（由 before_request 调用）。"""
    _request_id_var.set(value or '')


def get_request_id():
    """读取当前上下文的追踪 id；无上下文（CLI/池线程未传播）返回空串。"""
    return _request_id_var.get()


def clear_request_id():
    """请求结束时清除，防止 waitress 复用线程时把上一个请求的 id 带给下一个。"""
    _request_id_var.set('')


def submit(pool, fn, *args):
    """在**复制后的请求上下文**里向线程池提交任务，返回 Future。

    每个任务单独 `copy_context()`：同一个 Context 对象被两个线程同时进入会抛
    `RuntimeError: cannot enter context: ctx is already entered`（实测）。驾驶舱
    一次并行两个模型角色，若只复制一份传两次，第二个角色直接崩、只留下一个角色结论
    —— 那比不带 request_id 更糟。
    """
    return pool.submit(contextvars.copy_context().run, fn, *args)


def utc_iso_z():
    """UTC 时刻的 ISO-8601 字符串（带 Z、保留微秒）。

    为什么要有单一入口：改造前同一份留痕里有三种时刻口径 —— `datetime.utcnow()`
    （3.12 起已弃用）、`now(UTC).isoformat()+'Z'`（得到 `+00:00Z` 这种畸形尾巴）、
    `time.strftime` 的**本地**时间。第 19 轮要做的「访问日志 ⇄ AI 留痕」关联，
    若按时间戳对，本地 UTC+8 与 UTC 之间会差 8 小时 ⇒ 归因必然错。
    保留微秒与旧输出格式一致，不改对外字段形状。
    """
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def utc_now_naive():
    """naive UTC —— 给 `db.DateTime`（无 tz 列）写入用，与旧 `utcnow()` 逐字节等值。"""
    return datetime.now(UTC).replace(tzinfo=None)


def norm_path(path):
    """把路径归一成低基数聚合键，防 id 爆炸撑破上限。

    归一规则（顺序敏感）：去查询串 → 数字段与长十六进制段一律换成 `:id` →
    只保留前 3 段 → 限长 60。`/admin/staff/12/edit` 与 `/admin/staff/7/edit`
    必须落同一个键，否则 64 条上限形同虚设。
    """
    base = (path or '/').split('?', 1)[0]
    segs = [s for s in base.split('/') if s]
    out = []
    for seg in segs[:3]:
        low = seg.lower()
        if seg.isdigit():
            out.append(':id')
        elif len(seg) >= 16 and all(ch in '0123456789abcdef' for ch in low):
            out.append(':id')
        else:
            out.append(low)
    return ('/' + '/'.join(out))[:60] if out else '/'


def observe(path, method, status, duration_ms, slow):
    """累计一次请求。任何异常都不得冒泡到响应链路。

    Args:
        path: 原始 request.path（本函数内部才做归一，便于日志侧保留原值）。
        method: HTTP 方法，仅用于同键不同方法的粗粒度区分。
        status: 响应状态码整数。
        duration_ms: 本次请求处理耗时（毫秒，浮点）。
        slow: 调用方按阈值判定后的布尔值。
    """
    try:
        key = norm_path(path)
        cls = f'{int(status) // 100}xx'
        with _lock:
            _state['requests'] += 1
            _state['by_class'][cls] = _state['by_class'].get(cls, 0) + 1
            if 400 <= int(status) < 500:
                _state['errors_4xx'] += 1
            elif int(status) >= 500:
                _state['errors_5xx'] += 1
            if slow:
                _state['slow'] += 1
            if duration_ms > _state['max_duration_ms']:
                _state['max_duration_ms'] = round(float(duration_ms), 1)
            row = _state['by_path'].get(key)
            if row is None:
                if len(_state['by_path']) >= MAX_PATH_KEYS:
                    _evict_locked()
                _state['by_path'][key] = {
                    'count': 1, 'max_ms': round(float(duration_ms), 1),
                    'slow': 1 if slow else 0, 'method': (method or '').upper(),
                }
            else:
                row['count'] += 1
                row['max_ms'] = max(row['max_ms'], round(float(duration_ms), 1))
                if slow:
                    row['slow'] += 1
    except Exception as exc:  # 观测失败只丢计数、绝不改响应，但必须留下"丢了几个"的证据
        with _lock:
            _state['dropped'] += 1
        logging.getLogger(__name__).debug('观测计数丢弃：%s', type(exc).__name__)


def _evict_locked():
    """路径表满员时淘汰计数最小的一条（调用方须已持锁），并累计淘汰数。"""
    if not _state['by_path']:
        return
    worst = min(_state['by_path'].items(), key=lambda kv: (kv[1]['count'], kv[0]))[0]
    _state['by_path'].pop(worst, None)
    _state['evictions'] += 1


def norm_sql(statement):
    """把一条 SQL 归一成低基数模板：字符串字面量与数字换 `?`、空白折叠、截 120。

    刻意**不动带数字的标识符**（`t1`、`store_2` 仍是原样）：那是不同表/别名，
    归并成一个模板会把两类不同的慢查询算成一条（本项目反复犯的过度归一形态）。
    """
    text = ' '.join((statement or '').split())
    text = re.sub(r"'[^']*'", '?', text)
    text = re.sub(r'"[^"]*"', '?', text)
    text = re.sub('(?<![0-9A-Za-z_])[0-9]+([.][0-9]+)?(?![0-9A-Za-z_])', '?', text)
    return text[:SQL_TEMPLATE_LEN]


def note_query(statement):
    """记一次慢查询（按模板聚合）。异常类名同理：只计数，不存原文 SQL 参数。"""
    try:
        tpl = norm_sql(statement)
        if not tpl:
            return
        with _lock:
            rows = _state['slow_queries']
            if tpl not in rows and len(rows) >= MAX_SQL_KEYS:
                worst = min(rows.items(), key=lambda kv: (kv[1]['count'], kv[0]))[0]
                rows.pop(worst, None)
                _state['evictions'] += 1
            row = rows.get(tpl)
            if row is None:
                rows[tpl] = {'count': 1}
            else:
                row['count'] += 1
    except Exception as exc:  # 同上：丢弃必须可见，不做静默 pass
        with _lock:
            _state['dropped'] += 1
        logging.getLogger(__name__).debug('慢查询聚合丢弃：%s', type(exc).__name__)


def note_exception(kind):
    """按异常类名聚合计数（不存堆栈、不存路径，避免把内部情报留在内存里）。"""
    try:
        name = (kind or '').strip()[:60]
        if not name:
            return
        with _lock:
            kinds = _state['error_kinds']
            if name not in kinds and len(kinds) >= MAX_KIND_KEYS:
                worst = min(kinds.items(), key=lambda kv: (kv[1], kv[0]))[0]
                kinds.pop(worst, None)
                _state['evictions'] += 1
            kinds[name] = kinds.get(name, 0) + 1
    except Exception as exc:  # 同上：丢弃可见，不做静默 pass
        with _lock:
            _state['dropped'] += 1
        logging.getLogger(__name__).debug('异常指纹丢弃：%s', type(exc).__name__)


def snapshot():
    """完整诊断快照（含路径名）—— 只给管理员侧与离线工具，不进公开端点。"""
    with _lock:
        rows = sorted(_state['by_path'].items(),
                      key=lambda kv: (-kv[1]['max_ms'], -kv[1]['count']))
        return {
            'requests': _state['requests'],
            'by_class': dict(_state['by_class']),
            'errors_4xx': _state['errors_4xx'],
            'errors_5xx': _state['errors_5xx'],
            'slow': _state['slow'],
            'max_duration_ms': _state['max_duration_ms'],
            'path_keys': len(_state['by_path']),
            'path_cap': MAX_PATH_KEYS,
            'error_kinds': dict(_state['error_kinds']),
            'slow_queries': sorted(({'template': k, **v} for k, v in _state['slow_queries'].items()),
                                   key=lambda r: -r['count']),
            'sql_cap': MAX_SQL_KEYS,
            'evictions': _state['evictions'],
            'dropped': _state['dropped'],
            'uptime_s': int(time.time() - _state['started_at']),
            'top_paths': [{'path': k, **v} for k, v in rows[:TOP_PATH_LIMIT]],
        }


def healthz_stats():
    """`/healthz` 专用：只有聚合整数，无任何路径名/表名/文件名。"""
    with _lock:
        return {
            'requests': _state['requests'],
            'errors_4xx': _state['errors_4xx'],
            'errors_5xx': _state['errors_5xx'],
            'slow': _state['slow'],
            # 只出「有几种异常」这一整数，类名留在 snapshot（公开端点不泄内部结构）
            'error_kinds': len(_state['error_kinds']),
            'evictions': _state['evictions'],
            'dropped': _state['dropped'],
            'uptime_s': int(time.time() - _state['started_at']),
        }


def reset():
    """清空计数（仅测试用）；started_at 一并重置，避免 uptime 断言跨用例漂移。"""
    with _lock:
        _state['requests'] = 0
        _state['by_class'] = {}
        _state['errors_4xx'] = 0
        _state['errors_5xx'] = 0
        _state['slow'] = 0
        _state['max_duration_ms'] = 0.0
        _state['by_path'] = {}
        _state['error_kinds'] = {}
        _state['slow_queries'] = {}
        _state['evictions'] = 0
        _state['dropped'] = 0
        _state['started_at'] = time.time()
