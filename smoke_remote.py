#!/usr/bin/env python
"""部署后远程冒烟（2026-09-18 双向迭代 S1）。

用法：
    python smoke_remote.py https://<your-username>.pythonanywhere.com
    python smoke_remote.py https://<host> --expect-ai   # 期望 AI 在线（配置了 Key 时）

设计：
  借鉴超市web ci.yml 的「部署后冒烟 → 失败即回滚」三要素，按 PythonAnywhere 形态重写：
  ① 只依赖标准库（无需 requests，任何机器可跑）；
  ② 判定 = 探活 JSON 结构 + db=ok + 关键页面 200，任一不满足即 exit 1（供部署脚本据此回滚）；
  ③ 不打印任何密钥/内网信息，失败只输出状态码与 URL 路径。

退出码：0 = 冒烟通过；1 = 冒烟失败（调用方应触发回滚）。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 15
PAGE_PATHS = ['/', '/login']
# 参与「部署是否落后于仓库」比对的静态资产（相对站点根 = 相对源码副本目录，两侧同路径可直接比）
ASSET_PATHS = ['/static/css/style.css', '/static/css/critical.css']


def fetch(url):
    # 快照镜像的页面名含中文（01_管理员看板.html），urllib 直接发会 UnicodeEncodeError ⇒ 必须百分号编码
    req = urllib.request.Request(urllib.parse.quote(url, safe=":/?=&#"),
                                 headers={'User-Agent': 'ican-smoke/1.0'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read().decode('utf-8', 'replace')


def check_healthz(base, failures, expect_ai, allow_alerts=False):
    """探活：结构 + db + 可选 AI 配置态。"""
    try:
        status, body = fetch(f'{base}/healthz')
    except urllib.error.HTTPError as e:
        failures.append(f'/healthz HTTP {e.code}')
        return
    except Exception as e:
        failures.append(f'/healthz 请求异常：{type(e).__name__}')
        return

    if status != 200:
        failures.append(f'/healthz HTTP {status}')
        return
    try:
        data = json.loads(body or '{}')
    except json.JSONDecodeError:
        failures.append('/healthz 返回非 JSON')
        return
    print(f"  /healthz → {data}")
    if data.get('status') != 'ok':
        failures.append(f"/healthz status={data.get('status')}")
    if data.get('db') != 'ok':
        failures.append(f"/healthz db={data.get('db')}（数据库不可用）")
    if expect_ai and not (data.get('ai') or {}).get('configured'):
        failures.append('/healthz ai.configured=false（期望已配置 AI 后端）')

    # 第 23 轮 H51：/healthz 的 alerts 是阈值判定结果，判定单源在 src/alerts.py。
    # 缺字段 = 线上仍是加告警面之前的旧版（按 H27/H28 同一口径：缺字段判失败，不当"没测到"）。
    alerts = data.get('alerts')
    if not isinstance(alerts, list):
        failures.append('/healthz 无 alerts 段（线上未部署告警面，不得判"已观测"）')
    elif alerts and not allow_alerts:
        codes = [str(a.get('code')) for a in alerts if isinstance(a, dict)]
        failures.append(f'/healthz 有 {len(alerts)} 条告警：{codes}'
                        '（确认是可接受的一次性累计可加 --allow-alerts）')
    elif alerts:
        print(f'  ℹ️ 已按 --allow-alerts 放行 {len(alerts)} 条告警（只报不判红）')

    # 第 17 轮 H27/H28：部署后必须能证明"版本已上新、迁移已跑完"。
    # 缺字段一律判失败而不是跳过 —— 旧版线上（改这一版之前部署的）恰恰就是没这两个字段，
    # 若"没有就当没测到"，这条腿等于没有（对标 Metabase /api/health 的 initializing 语义）。
    ver = data.get('version') or {}
    if not ver.get('app'):
        failures.append('/healthz 无 version.app（线上仍是 H28 之前的旧版，不得判"已升级"）')
    local = local_version()
    if local and ver.get('app') and ver['app'] != local:
        failures.append(f"/healthz version.app={ver['app']} ≠ 本次部署期望 {local}（部署没生效）")
    sch = data.get('schema')
    if not isinstance(sch, dict):
        failures.append('/healthz 无 schema 段（旧版未部署，无法判断迁移是否跑完）')
    else:
        if not sch.get('ledger_table'):
            failures.append('/healthz schema.ledger_table=false（迁移账本表不存在）')
        if sch.get('pending'):
            failures.append(f"/healthz 有未应用的迁移补丁：{sch['pending']}"
                            ' → 线上执行 init_db.py --if-empty')
        if sch.get('orphans'):
            failures.append(f"/healthz 迁移账本有孤儿记录：{sch['orphans']}（已发布 pid 被改过名）")


def local_version():
    """读同副本 `src/version.py` 的 APP_VERSION（本次要部署成的样子）。"""
    import os
    import re

    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src', 'version.py')
    try:
        with open(p, encoding='utf-8') as fh:
            m = re.search(r"^APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]", fh.read(), re.M)
        return m.group(1) if m else ''
    except OSError:
        return ''


def check_pages(base, failures, pages):
    """页面可达（登录页 302 会被 urllib 跟随，非 200/3xx 才失败）。页面清单由调用方给：
    真系统与静态快照镜像可探的页面不同（镜像里没有 /login 与 /demo），写死会把"本就没有"判成失败。"""
    for path in pages:
        try:
            status, _ = fetch(f'{base}{path}')
        except urllib.error.HTTPError as e:
            if e.code in (301, 302):
                print(f'  {path} → {e.code}（重定向，视为通过）')
                continue
            failures.append(f'{path} HTTP {e.code}')
            continue
        except Exception as e:
            failures.append(f'{path} 请求异常：{type(e).__name__}')
            continue
        if status != 200:
            failures.append(f'{path} HTTP {status}')
        else:
            print(f'  {path} → 200')


def check_freshness(base, local_src, failures, strict):
    """线上静态资产 ⇄ 本地权威副本逐字节比对：不一致 = 部署落后于仓库。

    2026-09-24 实测踩点：PA 线上 style.css 61,104B / 仓库 64,483B 且缺当轮新增类，
    `git status` 全绿、页面 200、`/healthz` ok —— 三种常规判据全都看不出来。
    默认只告警：本脚本被部署回滚链调用，镜像陈旧不该触发回滚；`--strict-fresh` 才判失败。
    """
    import hashlib
    for path in ASSET_PATHS:
        rel = path.lstrip('/')
        # 站点根映射到 src/（Flask static 指向 src/static），副本里可能是 <副本>/src/static 或 <副本>/static
        candidates = [os.path.join(local_src, 'src', rel), os.path.join(local_src, rel)]
        local = next((c for c in candidates if os.path.isfile(c)), None)
        if local is None:
            print(f'  {path} → 本地两处候选均无，未比对')
            failures.append(f'{path} 本地副本缺失 ⇒ 新鲜度根本没比（判失败，不给假绿）')
            continue
        try:
            status, body = fetch(f'{base}{path}')
        except Exception as e:
            failures.append(f'{path} 新鲜度比对取不到：{type(e).__name__}') if strict else \
                print(f'  {path} → 取不到（{type(e).__name__}），未比对')
            continue
        remote = body.encode('utf-8', 'replace')
        with open(local, 'rb') as fh:
            local_bytes = fh.read()
        # 换行归一后再比：Windows 工作树常是 CRLF（core.autocrlf=true），而仓库 blob / 站点服务的是
        # LF —— 直接比原始字节会在"其实一致"时报落后（2026-09-25 本地实测：live 62,983B ⇄ 本地 64,483B）。
        norm = lambda b: b.replace(b'\r\n', b'\n')
        r_n, l_n = norm(remote), norm(local_bytes)
        rh, lh = hashlib.sha256(r_n).hexdigest()[:12], hashlib.sha256(l_n).hexdigest()[:12]
        crlf_note = '' if remote == local_bytes else '（已忽略 CRLF/LF 差异）'
        if rh == lh:
            print(f'  {path} → 与仓库一致 {rh}{crlf_note}')
            continue
        msg = (f'{path} 落后于仓库：线上 {len(r_n)}B/{rh} vs 仓库 {len(l_n)}B/{lh}')
        if strict:
            failures.append(msg)
        else:
            print(f'  ⚠️ {msg}（默认不判失败，--strict-fresh 可强制）')


def check_marker(base, marker, failures, pages):
    """镜像新鲜度：所探页面中**至少一页**含该标记（快照站把 CSS 内联进每页，index 页不含业务样式，
    所以按"逐页必含"判会把一个完全新鲜的镜像误判成陈旧）。全部页面都不含 = 该镜像早于引入此标记的构建。"""
    hits = {}
    for path in pages:
        try:
            _, body = fetch(f'{base}{path}')
        except Exception as e:
            hits[path] = None
            print(f'  {path} 取不到（{type(e).__name__}），该页不计入标记比对')
            continue
        hits[path] = marker in body
        print(f'  {path} 含「{marker}」= {hits[path]}')
    if not any(hits.values()):
        failures.append(f'所探 {len(pages)} 页无一含标记「{marker}」⇒ 该副本早于引入该标记的构建（或页面清单不对）')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('base', help='站点根地址，如 https://xxx.pythonanywhere.com')
    ap.add_argument('--expect-ai', action='store_true', help='期望 AI 后端已配置（未配置即判定失败）')
    ap.add_argument('--static', action='store_true',
                    help='静态镜像模式（Pages/CloudBase 快照站）：跳过 /healthz，只判各页 200')
    ap.add_argument('--local-src', metavar='DIR',
                    help='本地权威副本目录（如 …/05-源代码全文），比对 CSS 资产字节以判断部署是否落后')
    ap.add_argument('--strict-fresh', action='store_true', help='新鲜度不一致即判失败（默认只告警）')
    ap.add_argument('--allow-alerts', action='store_true',
                    help='放行 /healthz 的阈值告警（累计计数重启前不会自己消失，确认无害时用）')
    ap.add_argument('--expect-marker', metavar='STR', help='各页面必须含有的标记串（判陈旧）')
    ap.add_argument('--pages', metavar='P1,P2',
                    help='标记比对页面清单；缺省：真系统 /,/login,/demo，静态镜像 /,/index.html')
    args = ap.parse_args()

    base = args.base.rstrip('/')
    # 页面清单只解析一次，可达性与标记比对共用（镜像与真系统可探页面不同，写死即误判）
    if args.pages:
        pages = [p.strip() for p in args.pages.split(',') if p.strip()]
    else:
        pages = (['/', '/index.html', '/01_管理员看板.html'] if args.static
                 else ['/', '/login', '/demo'])
    failures = []
    if not args.static:
        check_healthz(base, failures, args.expect_ai, args.allow_alerts)
    check_pages(base, failures, pages)
    if args.local_src:
        check_freshness(base, args.local_src, failures, args.strict_fresh)
    if args.expect_marker:
        check_marker(base, args.expect_marker, failures, pages)

    if failures:
        print('❌ 冒烟失败：')
        for f in failures:
            print(f'   · {f}')
        return 1
    print('✅ 冒烟通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
