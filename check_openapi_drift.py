"""openapi.yaml 与真实路由的漂移检查（H10，2026-09-24 第 8 轮对标）

为什么需要它
────────────
`openapi.yaml` 是手写的（第 3 轮建、第 6 轮手改 scopes）。手写规范必然随代码腐坏，
而「文档在骗人」比「没有文档」更危险：接入方照它写代码，然后线上 404。

借鉴与取舍
────────────
Superset 的做法是 CI 里重新生成 spec 再 `git diff --quiet`
（`.github/workflows/check-openapi-spec-drift.yml`，注释里还特意说明「不要用 paths
过滤，否则必需检查在不命中路径的 PR 上永远不跑、把合并卡死」）。
该机制要求存在**无损生成器**；本项目的 spec 含大量人写的中文语义描述，无法由代码
自动生成，照抄只会让检查永远红。因此取其意图（漂移即红灯）、换可行机制：
**双向集合对账** ——
① 代码注册的端点 ⇄ spec 声明的端点；② 代码承认的作用域枚举 ⇄ spec 的 enum。
新增 / 删除 / 改名而忘改文档，两类都会被抓住。

零依赖：只用标准库做定向解析（门禁整体原则是 stdlib-only，不引 PyYAML）。
退出码：0=无漂移；1=有漂移（逐条打印差异）。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(ROOT, 'openapi.yaml')

# 统一用大写比较：Werkzeug 的 rule.methods 给的是 'GET' 这类大写名，
# 而 spec 的 YAML 键是小写 get/post —— spec 侧统一 .upper() 后再比。
HTTP_METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE'})
BLUEPRINT_NAME = 'api_v1'
# 只在开放 API 蓝图中对账：HTML 路由不在 spec 范围内
BLUEPRINT_ENDPOINT_PREFIX = 'api_v1.'

SPEC_PATH_RE = re.compile(r'^  (/[^\s:]*):\s*$')
SPEC_METHOD_RE = re.compile(r'^    (get|post|put|patch|delete):\s*$')
SPEC_ENUM_INLINE_RE = re.compile(r'enum:\s*\[([^\]]*)\]')
SCOPE_VALUE_RE = re.compile(r'[a-z_]+:read')
PARAM_RE = re.compile(r'<(?:[a-zA-Z]+:)?([a-zA-Z_][a-zA-Z0-9_]*)>')


def spec_endpoints(text):
    """从 openapi.yaml 抽出 {(path, METHOD)} 与 spec 声明的作用域集合。

    作用域枚举兼容两种 YAML 写法：块式 `- stores:read` 与流式 `enum: [a, b]`
    （本 spec 用后者）。

    Args:
        text: openapi.yaml 全文。

    Returns:
        tuple[set, set]: (端点集合，作用域集合)。
    """
    endpoints = set()
    scopes = set()
    current_path = None
    for line in text.splitlines():
        pm = SPEC_PATH_RE.match(line)
        if pm:
            current_path = pm.group(1)
            continue
        mm = SPEC_METHOD_RE.match(line)
        if mm and current_path:
            endpoints.add((current_path, mm.group(1).upper()))
        stripped = line.strip()
        if stripped.startswith('- ') and stripped[2:].endswith(':read'):
            scopes.add(stripped[2:].strip())
        em = SPEC_ENUM_INLINE_RE.search(line)
        if em:
            scopes.update(v.strip() for v in SCOPE_VALUE_RE.findall(em.group(1)))
    return endpoints, scopes


def api_base_path(app):
    """开放 API 蓝图的 url 前缀（spec 把它放进 `servers.url`，故 paths 不带前缀）。

    第一版漏了这层归一化，拿 `/api/v1/meta` 去比 spec 的 `/meta`，
    把 11 个端点全误报成「文档在骗人」——检查器自身也会造出假阳性。
    """
    bp = app.blueprints.get(BLUEPRINT_NAME)
    return (getattr(bp, 'url_prefix', '') or '').rstrip('/')


def real_endpoints(app):
    """从 Flask url_map 取开放 API 的 {(spec 形态 path, METHOD)}。

    两处归一化：① 去掉蓝图 url_prefix；② `<int:task_id>` → `{task_id}`。
    """
    prefix = api_base_path(app)
    out = set()
    for rule in app.url_map.iter_rules():
        if not str(rule.endpoint).startswith(BLUEPRINT_ENDPOINT_PREFIX):
            continue
        raw = rule.rule
        if prefix and raw.startswith(prefix):
            raw = raw[len(prefix):] or '/'
        path = PARAM_RE.sub(r'{\1}', raw)
        for method in sorted(rule.methods or ()):
            if method in HTTP_METHODS:
                out.add((path, method))
    return out


def real_scopes():
    """代码里真正承认的令牌作用域枚举。"""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from src import api_open
    return set(api_open.DATA_SCOPES)


def collect_problems(spec_eps, real_eps, spec_scopes, real_sc):
    """把四个集合压成人类可读的漂移清单（顺序稳定，便于 diff 与日志核对）。"""
    problems = []
    for path, method in sorted(real_eps - spec_eps):
        problems.append(f'代码有、spec 缺：{method} {path}')
    for path, method in sorted(spec_eps - real_eps):
        problems.append(f'spec 有、代码无（文档在骗人）：{method} {path}')
    for scope in sorted(real_sc - spec_scopes):
        problems.append(f'代码承认、spec 未列的作用域：{scope}')
    for scope in sorted(spec_scopes - real_sc):
        problems.append(f'spec 声明、代码不认的作用域：{scope}')
    return problems


def main():
    if not os.path.isfile(SPEC):
        print('🔴 [openapi 漂移] 缺少 openapi.yaml（交付白名单里的规范文件被删？）')
        return 1
    with open(SPEC, encoding='utf-8') as handle:
        spec_eps, spec_scopes = spec_endpoints(handle.read())

    os.environ.setdefault('DIFY_ENABLED', 'False')
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from src.app import create_app
    app = create_app('development')
    real_eps = real_endpoints(app)
    real_sc = real_scopes()

    problems = collect_problems(spec_eps, real_eps, spec_scopes, real_sc)
    if problems:
        print('🔴 [openapi 漂移] 与真实路由/枚举不一致：')
        for item in problems:
            print(f'     ↳ {item}')
        print(f'     ↳ spec 端点 {len(spec_eps)} / 代码端点 {len(real_eps)} / '
              f'spec 作用域 {len(spec_scopes)} / 代码作用域 {len(real_sc)}')
        return 1
    print(f'✅ [openapi 漂移] spec 与代码一致：端点 {len(real_eps)} 个、'
          f'作用域 {len(real_sc)} 类')
    return 0


if __name__ == '__main__':
    sys.exit(main())
