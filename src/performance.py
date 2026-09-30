"""性能监控中间件 —— 慢请求告警 + SQLAlchemy 慢查询日志 + 内存快照端点。

设计约束
────────
1. **零新依赖**：只用标准库 + Flask/SQLAlchemy 已有钩子。
2. **不阻断启动与请求**：注册与回调内所有异常静默吞掉；监控不得让业务链路变红。
3. **注册期不得触碰 app-context 绑定对象**：`db.engine` 是 Flask-SQLAlchemy 3.x 的
   context-bound property，在 create_app() 阶段访问会抛 `Working outside of
   application context` 直接压垮 WSGI 加载。故慢查询监听按仓内 query_guard 范式挂
   在 **`Engine` 类**上（类级 listener 无需上下文）。
4. **可配置阈值**：慢请求 SLOW_REQUEST_MS（默认 500）/ 慢查询 SLOW_QUERY_MS（默认 200）。
5. **端点默认关闭 + 鉴权**：内存快照泄露进程内部信息，默认不注册，需显式
   `MEMORY_ENDPOINT_ENABLED=true` 且仅管理员可访问。
"""
import logging
import os
import time
import tracemalloc

from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

SLOW_REQUEST_MS = int(os.environ.get('SLOW_REQUEST_MS', '500'))
SLOW_QUERY_MS = int(os.environ.get('SLOW_QUERY_MS', '200'))
MEMORY_SNAPSHOT_TOP = int(os.environ.get('MEMORY_SNAPSHOT_TOP', '10'))
MEMORY_ENDPOINT_ENABLED = os.environ.get('MEMORY_ENDPOINT_ENABLED', 'false').lower() == 'true'

# Engine 类级 listener 是进程全局的：create_app 在测试里会被多次调用，
# 不加闸会重复注册导致同一条慢查询告警刷屏。用模块级标志保证只装一次。
_state = {'sqlalchemy_installed': False}


def init_performance_monitor(app):
    """注册性能监控钩子到 Flask 应用。

    Args:
        app: 已完成 db.init_app() 的 Flask 应用实例。
    """
    _register_slow_request_hook(app)
    _register_sqlalchemy_monitor()
    _register_error_fingerprint(app)
    if MEMORY_ENDPOINT_ENABLED:
        _register_memory_endpoint(app)


def _register_slow_request_hook(app):
    """记录请求耗时：超阈值输出 WARNING，并把每次请求计入 `src/diag.py` 聚合计数。

    自带计时（`g._perf_start`），不依赖 logging_config 的钩子 —— 后者只在生产态注册，
    若复用其 `_request_start`，开发态的慢请求告警会静默失效。

    阈值 500ms 的标定依据（2026-09-26 实测，禁抄旧数）：真实演示库 14 条路由 × 5 次
    = 70 条采样，规则版下业务最慢 157.3ms（`/admin/`）、中位最高 89.7ms；在线 AI 页
    7249ms。两侧不重叠 ⇒ 阈值不动，「慢在 AI 还是慢在自己」改由 request_id 关联判定
    （见 `log_triage.py`），禁止用调阈值掩盖归因缺口。
    """
    from flask import g

    from src import diag

    @app.before_request
    def _perf_start():
        g._perf_start = time.perf_counter()

    @app.after_request
    def _check_slow_request(response):
        from flask import request
        start = getattr(g, '_perf_start', None)
        if start is None:
            return response
        duration_ms = (time.perf_counter() - start) * 1000
        slow = duration_ms > SLOW_REQUEST_MS
        diag.observe(request.path, request.method, response.status_code,
                     duration_ms, slow)
        if slow:
            try:
                logger.warning('[慢请求] %s %s 耗时 %.0fms（阈值 %dms）',
                               request.method, request.path, duration_ms, SLOW_REQUEST_MS)
            except Exception:  # 监控异常绝不影响响应
                logger.debug('慢请求告警写日志失败（忽略）', exc_info=True)
        return response


def _register_sqlalchemy_monitor():
    """在 `Engine` **类**上挂慢查询监听（进程级、只装一次）。

    不取 `db.engine` 实例：那需要应用上下文，见模块头约束 3。
    """
    if _state['sqlalchemy_installed']:
        return
    _state['sqlalchemy_installed'] = True

    # 签名必须是 SQLAlchemy 规定的完整 6 参：实测少传即 TypeError
    # （「按监听器参数个数截断传参」对 before/after_cursor_execute 并不成立），
    # 故此处形参数量由框架决定，用定点 noqa 而不是硬凑成 5 参以内。
    @event.listens_for(Engine, 'before_cursor_execute')
    def _ican_before_execute(conn, cursor, statement,  # noqa: PLR0913, PLR0917
                             parameters, context, executemany):
        conn.info['ican_query_start'] = time.perf_counter()

    @event.listens_for(Engine, 'after_cursor_execute')
    def _ican_after_execute(conn, cursor, statement,  # noqa: PLR0913, PLR0917
                            parameters, context, executemany):
        start = conn.info.pop('ican_query_start', None)
        if start is None:
            return
        duration_ms = (time.perf_counter() - start) * 1000
        if duration_ms > SLOW_QUERY_MS:
            from src import diag
            diag.note_query(statement)
            logger.warning('[慢查询] %.0fms（阈值 %dms）SQL: %s',
                           duration_ms, SLOW_QUERY_MS, statement[:200].replace('\n', ' '))


def _register_error_fingerprint(app):
    """按异常类名聚合未捕获异常（对标 frappe `error_log.get_fingerprint_stats`）。

    挂在 Flask 的 `got_request_exception` 信号上而不是逐个 except 里计数：
    信号只对**未被业务捕获**的异常触发，正是需要聚合的那一类；自己写 try/except
    计数会漏掉最要紧的「没想到要处理的那一个」。
    """
    from flask import got_request_exception

    from src import diag

    @got_request_exception.connect_via(app)
    def _note_exception(_sender, exception=None, **_extra):
        diag.note_exception(type(exception).__name__)


def _register_memory_endpoint(app):
    """注册内存快照与观测诊断端点（同一把 opt-in 闸 + 同一份 admin_required）。

    - `GET /admin/performance/memory`：tracemalloc 快照；
    - `GET /admin/performance/diag`：`src/diag.py` 的完整快照（含**路径名与异常类名**）。

    两者都只在 `MEMORY_ENDPOINT_ENABLED=true` 时注册，且要求管理员会话：
    进程内部信息不得对匿名或店员开放。诊断计数为什么单开一个管理员端点而不塞进
    `/healthz`——公开探活端点按第 19 轮口径只出聚合整数（路径名/表名属内部情报），
    而运维真正要看的"哪条路径在慢、哪类异常在涨"必须留在鉴权面内（对标第 19 轮 M59）。
    """
    from flask import jsonify

    from src.decorators import admin_required

    @app.route('/admin/performance/memory')
    @admin_required
    def _memory_snapshot():
        if not tracemalloc.is_tracing():
            tracemalloc.start()
            return jsonify(ok=True, message='tracemalloc 已启动，下次请求可见快照')
        current, peak = tracemalloc.get_traced_memory()
        top_stats = tracemalloc.take_snapshot().statistics('lineno')[:MEMORY_SNAPSHOT_TOP]
        return jsonify(ok=True, current_bytes=current, peak_bytes=peak,
                       top_allocations=[{'location': str(s.traceback),
                                         'size_bytes': s.size,
                                         'count': s.count} for s in top_stats])

    @app.route('/admin/performance/diag')
    @admin_required
    def _diag_snapshot():
        from src import diag
        return jsonify(ok=True, **diag.snapshot())
