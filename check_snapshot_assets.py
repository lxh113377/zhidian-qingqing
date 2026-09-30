#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""演示快照「被引资源完整性」守卫 —— 质量门禁第 23 项（BLOCK）。

为什么守这个量（2026-09-25 走查第 23 轮的真实事故）：快照 25 页按需加载
`/static/js/echarts.custom.js`，而镜像只部署了 html，线上该资源 HTTP 404、
图表静默空白。当时的「断链」判据只比 html 内容、不比被引资源，所以全绿。
形态与门禁第 10/19/21 项同族：**产物之间一致 ≠ 产物能跑**。

三类出口一起管（只修被点名的那一处不算修完）：
  1) 属性位引用（src/href/data-src）—— 页面间跳转与 <link> 样式；
  2) 脚本字符串字面量（"/static/js/xxx.js?v=..."）—— 真实事故正是这一形态，
     只按属性解析必然漏；
  3) CSS url() —— 内联样式表里的字体/图片。
外加两条边界：引用越出站点根（../）、资源存在但 0 字节。

判据纪律（本仓反复踩过的坑，逐条对应）：
  - 空输入不判通过：站点目录不存在 / 无 html / 引用集为空 ⇒ rc=2「没测到」，
    绝不打 PASS（对齐 `check_mirror_freshness.py` 的 rc=2 语义）。
  - 每条规则必须在 `--selftest` 夹具上真有命中，否则视为死码（第 16 轮教训）。
  - 控制台在中文 Windows 默认 GBK 时打印判据字符会崩栈（第 23 轮教训），
    故入口处 reconfigure 为 utf-8。

退出码：0=通过 / 1=有缺失或越界 / 2=没测到（或 --online 下网络不可达）/ 4=用法错。
"""

import io
import os
import re
import sys
import tempfile
import shutil

RC_OK, RC_FAIL, RC_NOTEST, RC_USAGE = 0, 1, 2, 4

# 属性位：只认标签内部的 src/href/data-src，避免把 JS 里的 `SRC="..."` 赋值
# 当属性（第 14 轮实测过这条假阳性：re.I 全文匹配会多计 24 条）。
_ATTR_RE = re.compile(r'<[a-zA-Z][^>]*?\b(?:src|href|data-src)\s*=\s*"([^"]+)"')
# 脚本字符串字面量：形如 "/static/js/echarts.custom.js?v=1789668034"
_JSSTR_RE = re.compile(r'''["'`](/[^"'`\s]*?\.(?:js|css|svg|png|jpg|jpeg|gif|webp|woff2?))(?:\?[^"'`]*)?["'`]''')
_CSSURL_RE = re.compile(r'url\(\s*(["\']?)([^)"\']+)\1\s*\)')
_EXTERNAL_RE = re.compile(r'^(?:https?:|data:|mailto:|tel:|javascript:|blob:|//)', re.I)


def strip_locator(ref: str) -> str:
    """去掉 ?query 与 #fragment，返回可解析的文件定位符。"""
    base = ref.split('#', 1)[0].split('?', 1)[0].strip()
    return base


def extract_refs(text: str):
    """从单页正文抽取 (kind, ref) 列表。三种形态独立来源，缺一即漏判。"""
    found = []
    found.extend(('attr', r) for r in _ATTR_RE.findall(text))
    found.extend(('jsstr', r) for r in _JSSTR_RE.findall(text))
    for _q, r in _CSSURL_RE.findall(text):
        found.append(('cssurl', r))
    return found


def resolve_target(site_root: str, page_name: str, ref: str):
    """把引用解析为站点内的绝对路径。

    返回 (绝对路径 或 None, 错误类别 或 None)。
    外部协议返回 (None, None)；越出站点根返回 (None, 'escape')。
    """
    base = strip_locator(ref)
    if not base or _EXTERNAL_RE.match(base):
        return None, None
    site_abs = os.path.abspath(site_root)
    if base.startswith('/'):
        target = os.path.join(site_abs, base.lstrip('/'))
    else:
        target = os.path.join(site_abs, os.path.dirname(page_name), base)
    target = os.path.normpath(target)
    if os.path.commonpath([target, site_abs]) != site_abs:
        return None, 'escape'
    return target, None


def audit_site(site_root: str):
    """审计一个站点根。返回 (页面数, 引用数, 资源数, 问题列表, 落地资源相对路径)。"""
    pages = sorted(f for f in os.listdir(site_root) if f.lower().endswith('.html'))
    if not pages:
        return 0, 0, 0, ['站点根内无 .html 页面'], set()
    refs, targets = 0, set()
    problems = []
    for page in pages:
        with open(os.path.join(site_root, page), encoding='utf-8', errors='replace') as fh:
            text = fh.read()
        for kind, ref in extract_refs(text):
            refs += 1
            target, err = resolve_target(site_root, page, ref)
            if err == 'escape':
                problems.append(f'{page} 的 {kind} 引用越出站点根：{ref}')
                continue
            if target is None:
                continue
            rel = os.path.relpath(target, os.path.abspath(site_root)).replace(os.sep, '/')
            if not os.path.isfile(target):
                problems.append(f'{page} 的 {kind} 引用缺失资源：{ref} -> {rel}')
            elif os.path.getsize(target) == 0:
                problems.append(f'{page} 的 {kind} 引用命中 0 字节资源：{rel}')
            else:
                targets.add(rel)
    if refs == 0:
        problems.append('整站未抽到任何本地引用，判据没跑到目标（防空转）')
    return len(pages), refs, len(targets), problems, targets


def asset_url(base_url: str, rel: str) -> str:
    """拼出可请求的 URL：中文页名必须按段 percent-encode。

    实测教训（本轮第一次跑线上腿即崩）：快照页名是 `01_管理员看板.html`，
    未编码的请求行让 `http.client` 抛 `UnicodeEncodeError: 'ascii' codec can't
    encode characters`——姊妹脚本 `smoke_remote.py` 在第 21 轮踩过同一条。
    判据自己抛栈不等于判据通过，故编码与兜底都要有。
    """
    from urllib.parse import quote

    return f'{base_url.rstrip("/")}/{quote(rel)}'


def judge_fetch(rel: str, code=None, body_len=None, error=None):
    """把一次取回的结果判成三态之一：('ok'|'missing'|'unreachable', 说明)。

    单独拆成纯函数是为了让「线上腿」的判定也能在夹具上跑——不然这条判据只能靠
    真网络验证，而真网络会漂（本仓已实测过 `*.github.io` 本机不可达那一天）。
    """
    if error is not None:
        return 'unreachable', f'{rel} -> 取不到（{error}）'
    if code != 200:
        return 'missing', f'{rel} -> HTTP {code}'
    if not body_len:
        return 'missing', f'{rel} -> 200 但响应体为空'
    return 'ok', f'{rel} -> 200/{body_len}B'


def audit_online(base_url: str, assets):
    """线上腿：逐条被引资源真取一次，非 200 即判缺失。

    为什么必须单独有这条腿：第 23 轮那次 404 发生在**部署面**（镜像只推了 html，
    被按需加载的 echarts 资源没推），本地离线腿只证「文件在仓库里」，证不了
    「文件在站点上」。判据强度必须落在用户实际取到的那个位置上。
    """
    import urllib.error
    import urllib.request

    problems = []
    checked = 0
    base = base_url.rstrip('/')
    for rel in sorted(assets):
        url = asset_url(base, rel)
        try:
            req = urllib.request.Request(url, method='GET',
                                         headers={'User-Agent': 'ican-asset-guard/1.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                code = getattr(resp, 'status', None) or resp.getcode()
                body_len = len(resp.read())
            verdict, msg = judge_fetch(rel, code=code, body_len=body_len)
        except urllib.error.HTTPError as exc:
            verdict, msg = judge_fetch(rel, code=exc.code, body_len=0)
        # ValueError 覆盖 UnicodeEncodeError（未编码的中文请求行）；兜底成 unreachable
        # 而不是让判据抛栈——抛栈会被读成"跑过了没报错"。
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            verdict, msg = judge_fetch(rel, error=f'{type(exc).__name__}: {exc}')
        checked += 1
        if verdict == 'unreachable':
            return [msg], checked, RC_NOTEST
        if verdict == 'missing':
            problems.append(msg)
    return problems, checked, RC_OK


def find_snapshot_dirs(start: str):
    """就近找快照站点根，令三副本任一位置运行都能测到目标。

    直接子目录：`演示快照集`（权威副本内）；同级：`01-演示快照集`（交付面）；
    再往上按交付布局找 `工程佐证材料/01-演示快照集` 与 `交付物/.../01-演示快照集`
    ——开发工作区里快照不落在本目录，若只查直接子目录，dev 侧运行会退化成
    「没测到」（第 13 轮 CI 教训的反向形态：判据只在某一台机器/某个目录成立）。
    """
    hits = []
    cur = os.path.abspath(start)
    probes = ('演示快照集',
              '01-演示快照集',
              os.path.join('工程佐证材料', '01-演示快照集'),
              os.path.join('交付物', '提交包', '工程佐证材料', '01-演示快照集'))
    for _ in range(5):
        for parts in probes:
            # 注意：这里必须整体 join。曾经写成 join(cur, *parts)，把路径串按字符
            # 展开后中间的 '\\' 会让 ntpath.join 重置整条路径（isabs('\\') 为真），
            # 于是探测恒落空、守卫在开发副本上退化成「没测到」。
            cand = os.path.normpath(os.path.join(cur, parts))
            if os.path.isdir(cand) and cand not in hits:
                hits.append(cand)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return hits


# ───────────────────────── 负对照夹具（每条规则各钉一个现场） ─────────────────────────
def _fixture_site(tmp: str) -> str:
    site = os.path.join(tmp, 'site')
    os.makedirs(os.path.join(site, 'static', 'js'))
    with open(os.path.join(site, 'static', 'js', 'ok.js'), 'w', encoding='utf-8') as fh:
        fh.write('window.OK=1;')
    with open(os.path.join(site, 'static', 'js', 'empty.js'), 'w', encoding='utf-8') as fh:
        fh.write('')
    page = '\n'.join([
        '<html><head>',
        '<link rel="stylesheet" href="https://cdn.example/x.css">',
        '<link rel="icon" href="data:image/svg+xml;base64,AAA">',
        '</head><body>',
        '<a href="index.html">self</a><img src="/static/js/ok.js?v=123">',
        '<script>SRC="/static/js/notloaded.js";</script>',
        '<script>var cssUrl = "/static/js/empty.js?cb=1";</script>',
        '<style>.bg{background:url(/static/js/ok.js)}</style>',
        '<a href="../outside/index.html">escape</a>',
        '<script>var SRC="/static/js/attr_trap.js";</script>',
        '</body></html>',
    ])
    with open(os.path.join(site, 'index.html'), 'w', encoding='utf-8') as fh:
        fh.write(page)
    return site


def selftest() -> int:
    """每条判据分支必须有夹具真命中；缺命中即判红（防死码冒充判据）。"""
    tmp = tempfile.mkdtemp(prefix=f'snapself-{os.getpid()}-')
    try:
        site = _fixture_site(tmp)
        pages, refs, assets, problems, landed = audit_site(site)
        cases = []
        cases.append(('缺失资源被点名', any('notloaded.js' in p for p in problems)))
        cases.append(('0 字节资源被点名', any('0 字节' in p and 'empty.js' in p for p in problems)))
        cases.append(('越出站点根被点名', any('越出站点根' in p for p in problems)))
        cases.append(('外链与 data: 不误报', not any('cdn.example' in p or 'data:image' in p for p in problems)))
        cases.append(('query 串被正确剥离后命中真文件', assets >= 1))
        cases.append(('三种引用形态都被抽到', refs >= 5))
        # JS 里的 `SRC="..."` 赋值必须由 jsstr 规则抓到（真实事故形态），且不得被
        # 属性位规则重复认领（第 14 轮实测过那条假阳性会多计 24 条）。
        trap_attr = [p for p in problems if 'attr_trap.js' in p and '的 attr ' in p]
        trap_jsstr = [p for p in problems if 'attr_trap.js' in p and '的 jsstr ' in p]
        cases.append(('JS 赋值形态归 jsstr 而非 attr', not trap_attr and bool(trap_jsstr)))
        # 空站点：无 html 必须判「没测到」而不是通过
        empty_site = os.path.join(tmp, 'empty')
        os.makedirs(empty_site)
        _, _, _, ep, _ = audit_site(empty_site)
        cases.append(('无页面时如实报错而非静默通过', bool(ep)))
        cases.append(('落地资源集合非空（正样本，防空判）', bool(landed)))
        # 线上腿不靠真网络自证：三态判定各给一个夹具（网络会漂，判定逻辑不该跟着漂）
        cases.append(('线上腿 200/404/不可达 三态各有命中',
                      judge_fetch('a.js', code=200, body_len=10)[0] == 'ok'
                      and judge_fetch('b.js', code=404, body_len=0)[0] == 'missing'
                      and judge_fetch('c.js', error='URLError: timed out')[0] == 'unreachable'))
        cases.append(('200 但空响应体判缺失',
                      judge_fetch('d.js', code=200, body_len=0)[0] == 'missing'))
        _u = asset_url('https://host/', '01_管理员看板.html')
        cases.append(('中文页名拼 URL 时被 percent-encode（否则 ascii 请求行直接抛栈）',
                      _u.startswith('https://host/01_%') and _u.isascii(), _u[:48]))
        # 走一遍真取回路径：连不上的端口必须归成"未跑成"，而不是让判据抛栈
        _op, _oc, _orc = audit_online('http://127.0.0.1:1', ['01_管理员看板.html'])
        cases.append(('取不到时返回"未跑成"而非抛栈（抛栈会被读成跑过且没报错）',
                      _orc == RC_NOTEST and _oc == 1 and bool(_op), f'{_orc}/{_oc}'))
        bad = [c[0] for c in cases if not c[1]]
        for c in cases:
            # 失败时把实测值一起打出来：只说"这条没过"的判据，下次还得重新查一遍
            tail = '' if c[1] or len(c) < 3 else f'｜实测 {c[2]}'
            print(f"  {'✅' if c[1] else '❌'} 夹具 {c[0]}{tail}")
        print(f'SELFTEST {"OK" if not bad else "FAIL: ' + '、'.join(bad) + '"}'
              f'｜{len(cases)} 条，页面 {pages}，引用 {refs}，资源 {assets}，问题 {len(problems)} 条')
        return RC_OK if not bad else RC_FAIL
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding='utf-8', errors='replace')
    if '--selftest' in argv:
        return selftest()

    base_url = None
    if '--online' in argv:
        idx = argv.index('--online')
        if idx + 1 >= len(argv) or argv[idx + 1].startswith('--'):
            print('❌ --online 需要一个站点根地址作为取值（用法：--online https://host/）')
            return RC_USAGE
        base_url = argv[idx + 1]
    unknown = [a for a in argv if a.startswith('--') and a != '--online']
    if unknown:
        print(f'❌ 未知参数：{unknown}（本脚本仅支持 --selftest / --online <URL>）；'
              '判据错用不得判通过')
        return RC_USAGE

    start = os.path.dirname(os.path.abspath(__file__))
    roots = find_snapshot_dirs(start)
    if not roots:
        print('❌ 未找到快照站点根（演示快照集 / 01-演示快照集）——没测到不等于通过')
        return RC_NOTEST

    total_pages = total_refs = total_assets = 0
    all_problems = []
    landed = set()
    for site in roots:
        pages, refs, assets, problems, site_landed = audit_site(site)
        rel = os.path.relpath(site, os.path.abspath(start)).replace(os.sep, '/')
        print(f"{'✅' if not problems else '❌'} 站点 {rel}：页面 {pages} / 引用 {refs} / "
              f'落地资源 {assets} / 问题 {len(problems)}')
        for p in problems:
            print(f'   ↳ {p}')
        total_pages += pages
        total_refs += refs
        total_assets += assets
        all_problems.extend(problems)
        landed |= site_landed

    rc = RC_FAIL if all_problems else RC_OK
    if all_problems:
        print(f'❌ 快照被引资源不完整：{len(all_problems)} 个问题（页面 {total_pages} / 引用 {total_refs}）')
        return rc
    print(f'✅ ASSET-OK：{len(roots)} 个站点根 / 页面 {total_pages} / 引用 {total_refs} 条 / '
          f'落地资源 {total_assets} 个，无缺失、无越界、无 0 字节')
    if not base_url:
        return RC_OK

    problems, checked, code = audit_online(base_url, landed)
    for p in problems:
        print(f'   ↳ {p}')
    if code == RC_NOTEST:
        print(f'⚠️ 线上腿未跑成（不可达），本次不判资源正常也不判异常：{checked} 条中止')
        return RC_NOTEST
    if problems:
        print(f'❌ 线上被引资源不完整：{len(problems)}/{checked} 条未取到（{base_url}）')
        return RC_FAIL
    print(f'✅ ONLINE-ASSET-OK：{checked} 条被引资源在 {base_url} 全部 200 且非空')
    return RC_OK


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
