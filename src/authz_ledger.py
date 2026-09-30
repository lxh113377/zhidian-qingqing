"""端点授权名册（对标第 27 轮 H63）——「谁能到达哪个端点」的单一声明源。

动因（2026-09-27 当场实测，权威副本 `src/`）：`app.url_map` 86 条规则里 **16 条不带任何视图级
守卫装饰器**，盘上没有任何「公开端点名册」（`PUBLIC_*` 全仓 grep 0 命中），L4 全文只有 2 处
`url_map` 使用且都不是授权断言。⇒ 新增端点忘挂装饰器 = **静默公开**，且没有任何判据会红。

上游规格（同轮 `gh api` 取回原文，缓存在 `D:/tmp/bench27/`，行号当场 grep）：
- Odoo 20.0 `odoo/addons/base/models/ir_http.py:392` ——
  `getattr(cls, f'_auth_method_{routing['auth']}')(routing)`：鉴权模式是路由声明里的**必填项**，
  由框架单点派发；四种模式实现齐备于 `:315` bearer / `:358` user / `:364` none / `:369` public。
  `:376` 连 CORS 预检都要显式改写成 `auth='none'`，不存在「没声明就是开放」。
- InvenTree master `src/backend/InvenTree/InvenTree/settings.py:521-522` ——
  `REST_FRAMEWORK['DEFAULT_PERMISSION_CLASSES'] = ['rest_framework.permissions.IsAuthenticated']`：
  默认拒绝，公开要显式撤销默认。
- Frappe develop `frappe/__init__.py:581 def whitelist(allow_guest=False, ...)`（`:619` 判定、
  `:1477` 是显式 opt-in 形态）：RPC 出口默认不对游客开放，开放是**声明出来的参数**。
- Saleor main `saleor/graphql/decorators.py` 的 `permission_required`：权限要求挂在声明位上，可枚举。

三方对账口径（本模块的实现选择）：
1. **声明侧** = `EXEMPT`（显式豁免档，逐条带 reason）；
2. **实现侧** = 对 `src/**/*.py` 做 AST 索引，取每个路由函数的守卫装饰器与函数体调用；
3. **运行侧** = `app.url_map.iter_rules()`。
「在 url_map 里、既不在豁免档、也没有守卫装饰器」= 会静默公开的端点 ⇒ 门禁判红 + 运行期 403。
不用 `inspect.getsource`：`functools.wraps` 之后取到的是包装体的源码，装饰器行是否可见取决于
各装饰器实现，判据不该建立在第三方包装语义上 —— 这是第 26 轮「接线只认 Call 节点」的同族教训。
"""
from __future__ import annotations

import ast
import os
import sys

MODE_PUBLIC = 'public'
MODE_BEARER = 'bearer'
MODE_INLINE = 'inline'

#: 允许的鉴权档（档位写错就是名册写错，不许自造）
MODES = (MODE_PUBLIC, MODE_BEARER, MODE_INLINE)

#: 视图级守卫装饰器白名单。命中任一即视为「到达该端点需要身份/角色」，无需进豁免档；
#: 反之必须逐条声明理由，否则运行期默认拒绝。
GUARD_DECORATORS = frozenset({
    'login_required', 'role_required', 'admin_required', 'staff_required',
    'business_admin_required', 'staff_or_admin_required',
})

#: 令牌鉴权入口（`/api/v1` 数据档）。bearer 档端点的函数体必须命中其中之一。
BEARER_CALLS = frozenset({'_guard', '_authenticate'})

#: scope 校验入口
SCOPE_CALLS = frozenset({'_require_scope', 'require_scope'})

#: 会话内联鉴权入口（`/api/v1/manage/*`：`@login_required` + 函数体内再校验归属与角色）
INLINE_OWNER_CALLS = frozenset({'_session_owner_merchant_id'})

_HTTP_METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE'})
_ROUTE_KEYS = frozenset({'route', 'get', 'post', 'put', 'patch', 'delete'})

# ══════════════════════ 豁免档（显式声明「不需要视图级守卫」）══════════════════════
# 每条都是**决策**而不是事实：删掉一条该端点即默认拒绝；新增一条必须写 reason。
# `methods` 是声明侧的收口上界，实侧越界即判红（防「只声明 GET 的端点被挂上 POST」）。
EXEMPT = {
    # ── 匿名可达的产品出口 ──
    'index': {
        'mode': MODE_PUBLIC, 'methods': frozenset({'GET'}),
        'reason': '3D 产品叙事首屏，登录前必须可达'},
    'portal': {
        'mode': MODE_PUBLIC, 'methods': frozenset({'GET'}),
        'reason': '产品门户页，登录/注册入口所在'},
    'static': {
        'mode': MODE_PUBLIC, 'methods': frozenset({'GET'}),
        'reason': 'Flask 内置静态资源出口（css/js/img），不含业务数据'},
    'narrative_assets': {
        'mode': MODE_PUBLIC, 'methods': frozenset({'GET'}),
        'reason': '首屏叙事页随包静态资源（模型/贴图），与 static 同性质'},
    'narrative_favicon': {
        'mode': MODE_PUBLIC, 'methods': frozenset({'GET'}),
        'reason': '站点图标，浏览器匿名请求'},
    'healthz': {
        'mode': MODE_PUBLIC, 'methods': frozenset({'GET'}),
        'reason': '托管平台探活入口，仅回结构状态不回商家业务明细'},
    'demo.index': {
        'mode': MODE_PUBLIC, 'methods': frozenset({'GET'}),
        'reason': '免登录只读演示入口；`DEMO_MERCHANT_ID` 未配时本身 404，'
                  '响应带 `X-Demo-Readonly: 1` 且无任何写入口'},
    # ── 认证自身的出入口（挂 login_required 会自锁）──
    'auth.login': {
        'mode': MODE_PUBLIC, 'methods': frozenset({'GET', 'POST'}),
        'reason': '登录页与登录提交，鉴权链起点'},
    'auth.register': {
        'mode': MODE_PUBLIC, 'methods': frozenset({'GET', 'POST'}),
        'reason': '开放注册页，限流与邮箱验证在视图内收口'},
    'auth.register_verify': {
        'mode': MODE_PUBLIC, 'methods': frozenset({'GET', 'POST'}),
        'reason': '注册邮箱验证回跳，点击时用户尚无会话'},
    # ── 令牌鉴权档：`/api/v1` 只读数据出口 ──
    # Bearer 令牌与 Flask-Login 会话是两套身份，故不挂会话装饰器；但**必须**在函数体内
    # 命中 `BEARER_CALLS`，漏挂即跨商家可读 ⇒ 名册、门禁、L4 三处都钉它。
    'api_v1.api_meta': {
        'mode': MODE_BEARER, 'methods': frozenset({'GET'}), 'scopes': (),
        'reason': '开放 API 探活：Bearer 令牌鉴权，回显本令牌可见范围'},
    'api_v1.api_stores': {
        'mode': MODE_BEARER, 'methods': frozenset({'GET'}), 'scopes': ('stores:read',),
        'reason': '开放 API 门店列表：令牌鉴权 + scope 校验 + 商家范围守卫'},
    'api_v1.api_dashboard': {
        'mode': MODE_BEARER, 'methods': frozenset({'GET'}), 'scopes': ('dashboard:read',),
        'reason': '开放 API 看板聚合：令牌鉴权 + scope 校验 + 商家范围守卫'},
    'api_v1.api_sales_daily': {
        'mode': MODE_BEARER, 'methods': frozenset({'GET'}), 'scopes': ('sales:read',),
        'reason': '开放 API 日流水：令牌鉴权 + scope 校验 + 商家范围守卫'},
    'api_v1.api_sales_history': {
        'mode': MODE_BEARER, 'methods': frozenset({'GET'}), 'scopes': ('sales:read',),
        'reason': '开放 API 流水明细（游标翻页）：令牌鉴权 + scope 校验 + 商家范围守卫'},
    'api_v1.api_tasks': {
        'mode': MODE_BEARER, 'methods': frozenset({'GET'}), 'scopes': ('tasks:read',),
        'reason': '开放 API 任务列表：令牌鉴权 + scope 校验 + 商家范围守卫'},
    'api_v1.api_task_detail': {
        'mode': MODE_BEARER, 'methods': frozenset({'GET'}), 'scopes': ('tasks:read',),
        'reason': '开放 API 任务详情：令牌鉴权 + scope 校验，越权 id 回 404 不泄露存在性'},
    # ── 会话内联档：`/api/v1/manage/*` 凭据管理 ──
    # 带 `@login_required`，但角色判定写在函数体里（非管理员 403）。单独一档钉住内联校验，
    # 防止日后删掉内联校验而装饰器仍绿。
    'api_v1.manage_create_token': {
        'mode': MODE_INLINE, 'methods': frozenset({'POST'}),
        'reason': '签发开放 API 令牌：会话鉴权 + 函数体内商家归属与管理员角色校验'},
    'api_v1.manage_revoke_token': {
        'mode': MODE_INLINE, 'methods': frozenset({'POST'}),
        'reason': '吊销令牌：会话鉴权 + 内联归属校验，跨商家 id 回 404'},
    'api_v1.manage_subscribe_webhook': {
        'mode': MODE_INLINE, 'methods': frozenset({'POST'}),
        'reason': '新增 Webhook 订阅：会话鉴权 + 内联归属校验（出站推送的授权起点）'},
    'api_v1.manage_unsubscribe_webhook': {
        'mode': MODE_INLINE, 'methods': frozenset({'POST'}),
        'reason': '停用 Webhook 订阅：会话鉴权 + 内联归属校验'},
}

#: 地板：豁免档条目数下限。清空名册能让「未登记」判据永不命中，故名册自身带下限，
#: 失守即判红（与第 40 项「禁录列地板」同形态）。
EXEMPT_FLOOR = 20

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _iter_py_files():
    for dirpath, dirnames, filenames in os.walk(_PKG_DIR):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for fn in sorted(filenames):
            if fn.endswith('.py'):
                yield os.path.join(dirpath, fn)


def _deco_name(node):
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ''


def _methods_of(call):
    for kw in call.keywords:
        if kw.arg == 'methods' and isinstance(kw.value, (ast.List, ast.Tuple)):
            return frozenset(e.value for e in kw.value.elts if isinstance(e, ast.Constant))
    return frozenset({'GET'})


def _call_names(func_node):
    out = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                out.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                out.add(node.func.attr)
    return frozenset(out)


def _py_files(base):
    """索引取数面：`src/` 下全部 .py（含 app.py，去重、跳过 `__pycache__`）。"""
    out = []
    for path in (os.path.join(base, 'app.py'), *_iter_py_files()):
        if path not in out and os.path.isfile(path):
            out.append(path)
    return out


def _blueprint_vars(tree):
    """`X = Blueprint('name', ...)` → {变量名: 蓝图名}，用于还原 Flask 的端点 id。"""
    out = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        f = node.value.func
        if not (isinstance(f, ast.Name) and f.id == 'Blueprint' and node.value.args):
            continue
        first = node.value.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                out[t.id] = first.value
    return out


def _route_endpoint(func_node, deco_call, deco_target, bp_vars):
    """把一个 route 装饰器解析成 Flask 端点 id；解析不出（动态注册）返回 None。"""
    owner = deco_target.value.id if isinstance(deco_target.value, ast.Name) else ''
    if owner in ('app', 'application'):
        return func_node.name
    if owner in bp_vars:
        return f'{bp_vars[owner]}.{func_node.name}'
    return None


def _index_file(path, index):
    """索引单个文件里的全部路由端点（守卫装饰器、函数体调用、声明方法集、路由串）。"""
    try:
        tree = ast.parse(open(path, encoding='utf-8').read())
    except (SyntaxError, UnicodeDecodeError) as exc:
        index[f'__parse_error__{os.path.basename(path)}'] = {
            'guards': frozenset(), 'calls': frozenset(), 'methods': frozenset(),
            'rule': '', 'file': path, 'line': 0, 'error': str(exc)}
        return
    bp_vars = _blueprint_vars(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in node.decorator_list:
            call = d if isinstance(d, ast.Call) else None
            tgt = call.func if call else (d if isinstance(d, ast.Attribute) else None)
            if not isinstance(tgt, ast.Attribute) or tgt.attr not in _ROUTE_KEYS:
                continue
            endpoint = _route_endpoint(node, call, tgt, bp_vars)
            if endpoint is None:
                continue
            rule = ''
            if call and call.args and isinstance(call.args[0], ast.Constant):
                rule = str(call.args[0].value)
            index[endpoint] = {
                'guards': frozenset(_deco_name(x) for x in node.decorator_list
                                    if _deco_name(x) in GUARD_DECORATORS),
                'calls': _call_names(node),
                'methods': _methods_of(call) if call else frozenset({'GET'}),
                'rule': rule,
                'file': path,
                'line': node.lineno,
            }


def build_index(pkg_dir=None):
    """AST 索引：endpoint -> {guards, calls, methods, rule, file, line}。

    蓝图端点 id = `<Blueprint 首参名>.<函数名>`，app 级端点 id = `<函数名>` —— 与 Flask 的
    `url_map` 端点命名同构，故声明侧、实现侧、运行侧三方可以按同一把尺对账。
    """
    base = pkg_dir or _PKG_DIR
    index = {}
    for path in _py_files(base):
        _index_file(path, index)
    return index


_IDX = {'cache': None}


def get_index(force=False):
    if _IDX['cache'] is None or force:
        _IDX['cache'] = build_index()
    return _IDX['cache']


def url_endpoints(app):
    return {rule.endpoint for rule in app.url_map.iter_rules()}


def url_method_map(app):
    out = {}
    for rule in app.url_map.iter_rules():
        out[rule.endpoint] = frozenset(
            m for m in (rule.methods or set()) if m not in ('HEAD', 'OPTIONS'))
    return out


def guarded_endpoints():
    return {ep for ep, v in get_index().items() if v['guards']}


def allowed_endpoints(app=None):
    """默认拒绝的放行集 = 豁免档 ∪ 实侧带守卫端点。

    传入 app 时只放行「确实在 url_map 里」的端点，避免名册陈旧项反噬运行侧。
    """
    allow = set(EXEMPT) | guarded_endpoints()
    if app is not None:
        allow &= url_endpoints(app)
    return allow


def backstop_decision(endpoint):
    """默认拒绝判定：放行 True；未登记 False（调用侧 abort 403 并留痕）。

    判定**不依赖 app 实例**：放行集只由「声明侧 EXEMPT + 源码 AST 实现侧」决定。
    早先按 `id(app)` 缓存过放行集 —— 测试里 app 被回收后 id 可被新实例复用，
    缓存会把「上一个 app 的形状」套到下一个 app 上；而这里的输入本来就不需要 app，
    去掉缓存同时去掉了失效条件（少一个状态就少一类 bug）。

    没有开关可关 —— 「能不能关掉默认拒绝」本身就该是一个需要声明的决策，而关掉它等于
    把本轮修的洞重新挖开（对标第 26 轮「可配却无人知道可配的假开关」同族，S21 判据实测）。
    """
    if endpoint is None:
        return True
    return endpoint in EXEMPT or endpoint in guarded_endpoints()


def _shape_problems(index, actual):
    """名册自身的形状判据：解析面完整、地板未失守、无陈旧声明、无未登记端点。"""
    problems = []
    parse_errs = sorted(k for k in index if k.startswith('__parse_error__'))
    if parse_errs:
        problems.append(f'这些源码解析失败（索引范围不完整，不许静默少算）：{parse_errs[:5]}')
    if len(EXEMPT) < EXEMPT_FLOOR:
        problems.append(f'豁免档条目数 {len(EXEMPT)} 低于地板 {EXEMPT_FLOOR}（名册被清空=红线判据永不命中）')
    stale = sorted(set(EXEMPT) - actual)
    if stale:
        problems.append(f'名册登记了已不存在的端点（陈旧声明，须删）：{stale[:8]}')
    guarded = {ep for ep, v in index.items() if v['guards']}
    undeclared = sorted(ep for ep in actual if ep not in EXEMPT and ep not in guarded)
    if undeclared:
        problems.append(f'这些端点既不在豁免档也无视图级守卫（会静默公开，默认拒绝已拦）：{undeclared[:8]}')
    return problems, parse_errs, guarded


def _ghost_problems(index, actual, parse_errs):
    """「源码里有、url_map 里没有」分两类判（一刀切会把 opt-in 路由误判成缺陷，
    反过来又会放过真漏网）：
    - 带守卫的幽灵 = opt-in 路由（如 `MEMORY_ENDPOINT_ENABLED` 开时才注册），启用后必进
      url_map 且仍带守卫 ⇒ 只作证据行；
    - 无守卫又不在豁免档的幽灵 = 代码里有路由、一旦注册即静默公开 ⇒ 判红。
    """
    ghost = sorted((set(index) | set(EXEMPT)) - actual - set(parse_errs))
    bare = sorted(g for g in ghost if not index.get(g, {}).get('guards'))
    guarded_ghost = sorted(g for g in ghost if index.get(g, {}).get('guards'))
    problems = []
    if bare:
        problems.append(f'这些端点在源码里声明了路由却不在 url_map，且既无守卫也不在豁免档'
                        f'（一旦注册即静默公开）：{bare[:8]}')
    return problems, guarded_ghost


def _bearer_problems(ep, spec, calls, decos):
    """bearer 档：令牌鉴权必须真接上，且不得与会话身份混用。"""
    problems = []
    if not (calls & BEARER_CALLS):
        problems.append(f'{ep} 声明为 bearer 但函数体未命中 `_guard()/_authenticate()`（跨商家可读）')
    if decos:
        problems.append(f'{ep} bearer 档不应再挂会话守卫装饰器（两套身份混用）：{sorted(decos)}')
    scopes = spec.get('scopes') or ()
    if scopes and not (calls & SCOPE_CALLS):
        problems.append(f'{ep} 声明了 scope {list(scopes)} 却无 scope 校验调用')
    return problems


def _inline_problems(ep, calls, decos):
    """inline 档：会话装饰器 + 函数体内归属校验，二者缺一即判红。"""
    problems = []
    if not (calls & INLINE_OWNER_CALLS):
        problems.append(f'{ep} 声明为 inline 但函数体未命中商家归属/角色校验调用')
    if 'login_required' not in decos:
        problems.append(f'{ep} inline 档仍必须带 login_required（不能只靠内联校验）')
    return problems


def _entry_problems(ep, spec, mmap, index):
    """逐条豁免档的档位/理由/方法集/接线判据（声明与实现必须同时对）。"""
    problems = []
    mode = spec.get('mode')
    if mode not in MODES:
        problems.append(f'{ep} 档位非法：{mode!r}（只允许 {MODES}）')
    if not str(spec.get('reason') or '').strip():
        problems.append(f'{ep} 缺 reason（豁免必须写明为什么，否则等于没有决策）')
    extra = mmap.get(ep, frozenset()) - spec.get('methods', frozenset())
    if extra:
        problems.append(f'{ep} 运行侧方法集越界：{sorted(extra)}'
                        f'（声明为 {sorted(spec["methods"])}）')
    entry = index.get(ep, {})
    calls = entry.get('calls', frozenset())
    decos = entry.get('guards', frozenset())
    if mode == MODE_BEARER:
        problems += _bearer_problems(ep, spec, calls, decos)
    elif mode == MODE_INLINE:
        problems += _inline_problems(ep, calls, decos)
    elif mode == MODE_PUBLIC and decos:
        problems.append(f'{ep} 声明为 public 却挂了守卫装饰器（声明与实现脱节）：{sorted(decos)}')
    return problems


def validate_registry(app):
    """名册 ⇄ AST 实现侧 ⇄ url_map 运行侧 三方对账。返回 (problems, evidence)。

    每条都设计成**会红**：漏登记、登记已删端点、方法集越界、档位写法错、reason 空、
    bearer/inline 档断线、public 档却挂守卫、名册被清空、AST 解析失败。
    """
    index = get_index()
    actual = set(url_endpoints(app))
    mmap = url_method_map(app)

    problems, parse_errs, guarded = _shape_problems(index, actual)
    p, guarded_ghost = _ghost_problems(index, actual, parse_errs)
    problems += p
    for ep in sorted(set(EXEMPT) & actual):
        problems += _entry_problems(ep, EXEMPT[ep], mmap, index)

    evidence = [
        f'url_map 规则数：{len(actual)}',
        f'豁免档声明数：{len(EXEMPT)}（地板 {EXEMPT_FLOOR}）',
        f'AST 索引到端点数：{len(index) - len(parse_errs)}，其中带视图级守卫 {len(guarded)}',
        f'放行集合：{len(allowed_endpoints(app))} / 实侧 {len(actual)}',
        f'opt-in 路由（当前未注册、带守卫）：{guarded_ghost or "无"}',
    ]
    return problems, evidence


def dump_matrix(app):
    """把三方对账结果打成可读矩阵（CLI 出口，非 Web 面）。"""
    index = get_index()
    lines = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.endpoint):
        ep = rule.endpoint
        spec = EXEMPT.get(ep)
        decos = sorted(index.get(ep, {}).get('guards', ()))
        mode = spec['mode'] if spec else ('guarded' if decos else 'UNDECLARED')
        lines.append(f'{rule!s:<38} {mode:<11} '
                     f'{",".join(sorted(decos)) or "-":<30} '
                     f'{(spec or {}).get("reason", "")[:38]}')
    return lines


def main(argv=None):
    """离线出口：`python -m src.authz_ledger [--check|--dump]`。

    第 26 轮教训「能力存在但无出口 = 半成品」：名册必须能在不开 Web 的情况下被人读、被 CI 用。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    from src.app import create_app
    app = create_app('development')
    problems, evidence = validate_registry(app)
    for line in evidence:
        print('  ·', line)
    if '--dump' in argv:
        print('\n'.join(dump_matrix(app)))
    if problems:
        print(f'[GATE:authz-fail] 对账问题 {len(problems)} 项：')
        for p in problems:
            print('  ✗', p)
        return 1
    print('[GATE:authz-pass] 名册 ⇄ 实现 ⇄ url_map 三方一致')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
