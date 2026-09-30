"""开放 API 的错误面 —— 单一 JSON 出口 + 与浏览器路由的形态分界（对标第 22 轮 H48/H49）。

实测动因（本轮当场跑出来的，不是推测）：用 test_client 打六条真实错误腿，结果是
`/api/v1/tasks/999999`→404 **HTML**、`/api/v1/sales/daily?limit=0`→400 **HTML**、
`/api/v1/dashboard?store_id=999999`→403 **HTML**、`/api/v1/meta` 无令牌→401 **HTML**、
`/api/v1/nope`→404 **HTML**，只有代码内联 `jsonify` 的那条缺作用域 403 是 JSON。
五条 HTML 的来由是 `src/app.py` 注册的 40x handler 全部无条件 `render_template`。
而 `openapi.yaml` 的 `Error` schema 声明的是 JSON —— 接入方按文档写
`resp.json()['error']`，在 404/401/403 上会直接崩在解析 HTML。

三条设计约束
────────────────────────────────────────────────
1. **浏览器路由行为不得回退**：H5/Admin 的 403/404/500 语义化错误页照旧是 HTML，
   只有落在 `API_PREFIX` 上的请求才切 JSON。判据（门禁第 34 项 + L4 S28）双向验。
2. **机器码是唯一清单**：`ERROR_CODES` 与 `openapi.yaml` 里 `Error.error` 的 enum 逐项相等，
   多一个少一个都判红；传未登记的码直接抛 ValueError（防"先上代码、后补文档"走回老路）。
3. **错误体自带 request_id**：与响应头 `X-Request-Id`（第 19 轮 logging_config 那条腿）同源，
   报障时调用方给一个 id 就能在 JSONL 里定位，而不是"刚才那个请求挂了"。
"""
from flask import g, jsonify, request

# 开放 API 前缀。与 Blueprint 的 url_prefix 一致；两处都改时必须一起改（第 34 项看守）。
API_PREFIX = '/api/v1/'

# 错误体固定四键。门禁第 34 项要求 spec 的 Error schema 全数声明。
ERROR_SCHEMA_KEYS = ('ok', 'error', 'message', 'request_id')

# 机器可读错误码唯一清单（⇄ openapi.yaml 的 Error.error enum 双向对账）
ERROR_CODES = (
    'bad_request',
    'conflict',
    'forbidden',
    'insufficient_scope',
    'internal_error',
    'method_not_allowed',
    'not_found',
    'rate_limited',
    'unauthorized',
    'unsupported_media_type',
)

# 只透传这几条元数据头。Werkzeug 的 `HTTPException.get_headers()` 还带着旧表示的实体头
# （`Content-Type: text/html` + `Content-Length`），无脑搬过来会**把刚修好的 JSON 响应
# 内容类型又改回 HTML** —— 本轮 L4 S28 实测抓到这个回归，故改为显式白名单。
PASSTHROUGH_HEADERS = ('Allow', 'Retry-After', 'WWW-Authenticate')

# HTTP 状态 → 机器码。路由阶段与视图内 abort 共用这一张表，不分叉。
STATUS_CODE_MAP = {
    400: 'bad_request',
    401: 'unauthorized',
    403: 'forbidden',
    404: 'not_found',
    405: 'method_not_allowed',
    409: 'conflict',
    415: 'unsupported_media_type',
    429: 'rate_limited',
    500: 'internal_error',
}

# description 缺失时的中文兜底文案（人读，不参与对账）
DEFAULT_COPY = {
    'bad_request': '请求参数不合法，请检查后重新提交。',
    'conflict': '资源状态冲突，操作未执行。',
    'forbidden': '当前令牌无权访问该资源。',
    'insufficient_scope': '该令牌缺少所需作用域。',
    'internal_error': '服务内部错误，请稍后重试或凭 request_id 报障。',
    'method_not_allowed': '该路径不支持此请求方法。',
    'not_found': '资源不存在。',
    'rate_limited': '请求过于频繁，请稍后再试。',
    'unauthorized': '缺少或无效的访问令牌。',
    'unsupported_media_type': '请求体内容类型不受支持。',
}


def targets_api(path=None):
    """请求是否落在开放 API 前缀上（含路由阶段的 404/405，那时 blueprint 尚未接管）。

    连 `/api/v1`（无尾斜杠，本身不是任何端点）也算：它只会以 404 收场，而 404 恰好是
    本模块存在的理由 —— 按前缀排除它，就等于给接入方留一条吐 HTML 的错误腿。
    """
    raw = request.path if path is None else str(path)
    return raw.startswith(API_PREFIX) or raw.rstrip('/') == API_PREFIX.rstrip('/')


def current_request_id():
    """与 `X-Request-Id` 响应头同源的追踪编号；取不到就返回空串而不是伪造一个。"""
    return str(getattr(g, 'request_id', '') or '')


def json_error(status, code, message=None, headers=None, **extra):
    """统一错误出口 → `(Response, status)`，体形固定为四键 + extra。

    Args:
        status: HTTP 状态码（必须与 code 同属一张表，否则判错用）。
        code: `ERROR_CODES` 之一。
        message: 人读文案；缺省取 `DEFAULT_COPY[code]`。
        headers: 需要透传响应头（如 405 的 `Allow`，见 `api_error_or`）。
        extra: 附加键（如 `granted`/`required`），必须在 spec 里声明为可选。

    Raises:
        ValueError: 传了未登记的机器码 —— 先加清单与 spec，再上代码。
    """
    if code not in ERROR_CODES:
        raise ValueError(f'未登记的错误码 {code!r}：请先加入 api_errors.ERROR_CODES '
                         '与 openapi.yaml 的 Error.error enum（门禁第 34 项会双向对账）')
    body = {
        'ok': False,
        'error': code,
        'message': message or DEFAULT_COPY[code],
        'request_id': current_request_id(),
    }
    body.update(extra)
    response = jsonify(body)
    # 405 的 Allow 由 Werkzeug 挂在异常上（RFC 9110：405 响应必须带 Allow 列出允许的方法）。
    # 自建 JSON 响应会把它丢掉 —— 本轮实测就是这个形态，所以这里显式透传。
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response, status


def api_error_or(status, e, html_renderer):
    """app 级 handler 的共用分叉：API 前缀回 JSON，其余交回原 HTML 渲染闭包。

    把"分支"收进一个函数而不是散在五个 handler 里，是为了让第 34 项能按 AST 证明
    每个 handler 都经过这里 —— 漏一个就是一个真实的 HTML 错误腿。
    """
    if targets_api():
        code = STATUS_CODE_MAP.get(status, 'internal_error' if status >= 500 else 'bad_request')
        desc = getattr(e, 'description', '') or None
        passthrough = {}
        getter = getattr(e, 'get_headers', None)
        if callable(getter):
            try:
                # 只透传"与方法/认证/重试有关"的元数据头。Werkzeug 的 get_headers() 里还带着
                # Content-Type: text/html 与 Content-Length —— 那是**旧表示**的实体头，
                # 原样搬到 JSON 响应上就等于把刚修好的形态又改回 HTML（本轮 L4 S28 实测抓到）。
                for key, value in getter():
                    if key in PASSTHROUGH_HEADERS:
                        passthrough[key] = value
            except (TypeError, ValueError):
                passthrough = {}
        return json_error(status, code, desc, headers=passthrough)
    return html_renderer()


__all__ = ['API_PREFIX', 'DEFAULT_COPY', 'ERROR_CODES', 'ERROR_SCHEMA_KEYS',
           'PASSTHROUGH_HEADERS', 'STATUS_CODE_MAP', 'api_error_or', 'current_request_id',
           'json_error', 'targets_api']
