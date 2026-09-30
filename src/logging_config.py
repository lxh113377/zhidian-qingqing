"""结构化日志配置 —— 开发态人类可读，生产态 JSON 可解析。

设计约束
────────
1. **零新依赖**：只用标准库 logging / json / datetime，不引入 structlog 等第三方。
2. **不破坏既有 logger**：各模块已用 `logging.getLogger(__name__)` 或 `app.logger`，
   本模块只配置 root logger 的 handler 与 formatter，不改模块侧调用。
3. **请求级追踪**：生产态自动注入 request_id（UUID4 前 8 位），供跨模块关联。
4. **有界输出**：日志文件轮转（默认 5MB × 3 备份），避免免费层磁盘被打满。
5. **敏感字段脱敏**：formatter 层对 password / secret / token 等键值做掩码处理。
"""
import json
import logging  # noqa: E402  # 业务日历紧随其后（第 29 轮）
import logging.handlers
import os
import uuid
from datetime import UTC, datetime

from src import diag
from src import business_calendar as cal

LOG_DIR = os.environ.get('LOG_DIR') or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
LOG_FILE = os.path.join(LOG_DIR, 'app.log')
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3

SENSITIVE_KEYS = frozenset({
    'password', 'secret', 'token', 'api_key', 'secret_key', 'secret_salt',
    'token_hash', 'csrf_token',
})


def _mask_record(record):
    """对 dict 中的敏感键值做掩码（递归处理嵌套 dict）。"""
    if not isinstance(record, dict):
        return record
    masked = {}
    for key, value in record.items():
        if key.lower() in SENSITIVE_KEYS and isinstance(value, str):
            masked[key] = value[:4] + '****' if len(value) > 4 else '****'
        elif isinstance(value, dict):
            masked[key] = _mask_record(value)
        else:
            masked[key] = value
    return masked


class JsonFormatter(logging.Formatter):
    """生产态 JSON 格式化器 —— 每条日志一行 JSON，便于日志采集系统解析。"""

    def format(self, record):
        log_entry = {
            'timestamp': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
        }
        if hasattr(record, 'request_id'):
            log_entry['request_id'] = record.request_id
        if record.exc_info:
            log_entry['exception'] = self.formatException(record.exc_info)
        if hasattr(record, 'extra_data'):
            log_entry['data'] = _mask_record(record.extra_data)
        return json.dumps(log_entry, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """开发态人类可读格式化器 —— 带时间戳、级别、追踪 id、模块名。"""

    def format(self, record):
        ts = cal.business_now().strftime('%H:%M:%S')
        msg = record.getMessage()
        rid = getattr(record, 'request_id', '-')
        base = f'{ts} [{record.levelname:>5s}] {rid} {record.name}: {msg}'
        if record.exc_info:
            base += '\n' + self.formatException(record.exc_info)
        return base


def _ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


class RequestIdFilter(logging.Filter):
    """把当前请求的追踪 id 挂到**所有**经过 handler 的记录上。

    为什么用 filter 而不是在每个调用点传 `extra`：Flask 自己打的异常日志
    （`app.logger.error` 里的 traceback）与 urllib3 / SQLAlchemy 等第三方 logger
    的记录都不会经过业务代码，靠调用点传参永远覆盖不全。第 19 轮实测到的正是这个洞：
    访问行带 request_id、traceback 不带，一次 500 的两半证据串不到一起。
    """

    def filter(self, record):
        if not hasattr(record, 'request_id'):
            record.request_id = diag.get_request_id() or '-'
        return True


_request_id_filter = RequestIdFilter()


def setup_logging(app=None, production=False):
    """配置日志系统。

    Args:
        app: Flask 应用实例（可选）。传入时会替换 app.logger 的 handler。
        production: True 则使用 JSON 格式化 + 文件轮转；False 则人类可读 + 控制台。
    """
    root = logging.getLogger()
    root.handlers.clear()

    if production:
        _ensure_log_dir()
        formatter = JsonFormatter()
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
            encoding='utf-8')
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_request_id_filter)
        root.addHandler(file_handler)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.addFilter(_request_id_filter)
        root.addHandler(console_handler)
        root.setLevel(logging.INFO)
    else:
        formatter = HumanFormatter()
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.addFilter(_request_id_filter)
        root.addHandler(console_handler)
        root.setLevel(logging.DEBUG)

    if app is not None:
        app.logger.handlers.clear()
        for handler in root.handlers:
            app.logger.addHandler(handler)
        app.logger.setLevel(root.level)


def inject_request_id(app):
    """注册请求级追踪钩子：`g` + ContextVar 双写，并保证请求结束即清除。

    用法：在 create_app() 中调用 inject_request_id(app)。

    为什么双写：`g` 供模板与视图直接取用；ContextVar 供**日志过滤器与 AI 留痕**取用
    —— 后者必须能在 `ThreadPoolExecutor` 工作线程里读到（决策驾驶舱并行两个模型角色），
    而线程默认不继承上下文，所以池侧要显式用 `diag.context_for_pool()` 传播。
    为什么必须清除：waitress 复用线程，不清会把上一个请求的 id 盖到下一个请求上，
    表现为「日志里的 id 对不上真实请求」——比没有 id 更坏。
    """
    from flask import g

    @app.before_request
    def _set_request_id():
        g.request_id = uuid.uuid4().hex[:8]
        diag.set_request_id(g.request_id)

    @app.teardown_request
    def _clear_request_id(_exc):
        diag.clear_request_id()

    @app.after_request
    def _log_request(response):
        from flask import request
        req_id = getattr(g, 'request_id', '-')
        duration_ms = 0
        if hasattr(g, '_request_start'):
            duration_ms = round((datetime.now(UTC) - g._request_start).total_seconds() * 1000, 1)
        app.logger.info(
            '%s %s %s %d %.1fms',
            req_id, request.method, request.path,
            response.status_code, duration_ms,
            extra={'request_id': req_id}
        )
        # 报障闭环：把追踪号回给客户端，用户/店长报一句编号就能对上服务端那行
        # traceback 与 AI 留痕。id 是不透明随机串，不含任何业务信息。
        if req_id and req_id != '-':
            response.headers.setdefault('X-Request-Id', req_id)
        return response

    @app.before_request
    def _record_start():
        g._request_start = datetime.now(UTC)
