#!/usr/bin/env python
"""依赖已知漏洞审计（第 32 轮）：**声明面 + 环境面两面都扫**，工具/网络缺席不得静默绿。

动因（2026-09-27 当场实测，不是预防性堆砌）：
1. 全仓与既有 45 项门禁对 `pip-audit|osv|GHSA|CVE-|dependabot|renovate` **0 命中** ——
   第 26 项只管 action 钉 SHA、第 27 项只管 vendored 资产哈希、第 28 项只管 ruff 规则集；
   **"钉在哪个版本上、那个版本有没有已知漏洞"从来没人看过**（钉版 ≠ 安全）。
2. 实跑 `pip-audit` 的两侧读数不同：`-r requirements.txt`（声明面，解析 87 包）= **0 条**，
   而**本 venv 环境面** = **10 条 / 2 包**（`pip 26.1.2` → PYSEC-2026-3721/CVE-2026-13346；
   `pypdf 6.14.2` → PYSEC-2026-3655/3656/3910/3912 等 8 条）。只扫一面必然漏另一面，
   而 `pypdf` 是**未声明、无人 import、无包依赖它**的孤儿包 —— 只有环境面看得见它。
3. 上游同轮实取：superset/airflow/InvenTree/posthog **4/4** 在仓库根带 `.github/dependabot.yml`
   （superset `:1-13` daily + `groups.security.applies-to: security-updates` + `cooldown`；
   InvenTree `:1-19` weekly + cooldown + groups）。

退出码是**类别码**（不是严重度）：
  0 两面都扫过且无未豁免漏洞 ／ 1 存在未豁免漏洞 ／ 2 测量工具不可用（UNVERIFIED）
  3 网络/索引不可达 ／ 4 用法或豁免台账不合法
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
RECEIPT = os.path.join(ROOT, 'DEPENDENCY-AUDIT.json')
LEDGER = os.path.join(ROOT, 'DEPENDENCY-AUDIT-EXEMPTIONS.json')
REQUIREMENTS = os.path.join(ROOT, 'requirements.txt')
FACES = ('declared', 'environment')
TOOL_PIN = 'pip-audit==2.10.1'
_ID_RE = re.compile(r'^(PYSEC|GHSA|CVE)-[0-9A-Za-z-]+$')
_REASON_MIN = 20


def _now() -> datetime:
    return datetime.now(UTC)


def _ids_of(entry: dict) -> list:
    """一条漏洞的 id 与其别名（GHSA/CVE/PYSEC 互指）合并去重，供豁免台账按任一 id 命中。"""
    vulns = entry.get('vulns') or []
    return sorted({v.get('id') for v in vulns if v.get('id')}
                  | {a for v in vulns for a in (v.get('aliases') or [])})


def _run_audit(extra: list) -> dict:
    """跑一次 pip-audit，返回 {status, vulns:[{name,version,ids}], error}。

    status ∈ ok / no-tool / unreachable —— 「没扫到」与「扫了没有问题」必须可区分。
    """
    cmd = [sys.executable, '-m', 'pip_audit', '--progress-spinner', 'off',
           '--format', 'json', *extra]      # argv 全部由本文件字面量拼成，不接外部输入
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)  # noqa: S603 理由：命令由本文件字面量拼装，唯一入参是仓库内 requirements 路径，无外部可控输入
    except FileNotFoundError:
        return {'status': 'no-tool', 'vulns': [], 'error': 'python 解释器不存在'}
    except subprocess.TimeoutExpired:
        return {'status': 'unreachable', 'vulns': [], 'error': 'pip-audit 超时（900s）'}
    out = proc.stdout or ''
    err = (proc.stderr or '').strip()
    if 'No module named' in err or 'No module named' in out:
        return {'status': 'no-tool', 'vulns': [], 'error': err.splitlines()[-1][:120]}
    if re.search(r'Connection|NetworkFileUnavailable|Max retries|Failed to establish|timed out', err, re.I):
        return {'status': 'unreachable', 'vulns': [], 'error': err.splitlines()[-1][:120]}
    try:
        data = json.loads(out)
    except ValueError:
        return {'status': 'unreachable', 'vulns': [],
                'error': (err or out or '无 JSON 输出').splitlines()[:1][0][:120]}
    deps = data.get('dependencies') or []
    vulns = [{'name': d.get('name'), 'version': d.get('version'),
              'ids': sorted({v.get('id') for v in (d.get('vulns') or []) if v.get('id')}
                            | {a for v in (d.get('vulns') or []) for a in (v.get('aliases') or [])})}
             for d in deps if (d.get('vulns') or d.get('vulnerabilities'))]
    return {'status': 'ok', 'vulns': vulns, 'error': '', 'packages': len(deps)}


def load_ledger() -> tuple:
    """读豁免台账：返回 (条目 dict, 问题列表)。形状/理由/到期三类不合者一律列入问题。"""
    if not os.path.isfile(LEDGER):
        return {}, [f'缺豁免台账 {os.path.basename(LEDGER)}（没有台账 = 任何人可以随意放行漏洞）']
    try:
        doc = json.loads(open(LEDGER, encoding='utf-8').read())
    except ValueError as exc:
        return {}, [f'豁免台账不是合法 JSON：{exc}']
    items = doc.get('exemptions') or []
    out, problems = {}, []
    today = _now().date().isoformat()
    for it in items:
        vid = str(it.get('id') or '').strip()
        if not _ID_RE.match(vid):
            problems.append(f'豁免 id 不合 PYSEC/GHSA/CVE 形状：{vid!r}（幽灵豁免=对空气放行）')
            continue
        if vid in out:
            problems.append(f'豁免 id 重复：{vid}')
            continue
        reason = str(it.get('reason') or '').strip()
        expires = str(it.get('expires') or '').strip()
        if len(reason) < _REASON_MIN:
            problems.append(f'豁免 {vid} 的理由不足 {_REASON_MIN} 字（豁免是一次决定，必须留下为什么）')
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', expires):
            problems.append(f'豁免 {vid} 缺 ISO 到期日（永久豁免 = 没人再回头看）')
        elif expires < today:
            problems.append(f'豁免 {vid} 已于 {expires} 过期（到期即红，逼一次重判）')
        out[vid] = it
    return out, problems


def _unresolved(face_vulns: list, ledger: dict) -> list:
    """一条漏洞的所有 id（含 alias）都在有效豁免台账里才算被豁免；否则原样返回。"""
    left = []
    for row in face_vulns:
        ids = set(row.get('ids') or [])
        if ids and ids <= set(ledger):
            continue
        left.append(row)
    return left


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='依赖已知漏洞审计（声明面 + 环境面）')
    ap.add_argument('--skip-env', action='store_true',
                    help='只扫声明面（离线复现用；回执会把 environment 标成 skipped）')
    ap.add_argument('--json', action='store_true', help='只打 JSON，不打人读汇总行')
    ap.add_argument('--no-write', action='store_true', help='不落 DEPENDENCY-AUDIT.json（只判不改盘）')
    args = ap.parse_args(argv)

    if not os.path.isfile(REQUIREMENTS):
        print(f'用法错：找不到 {REQUIREMENTS}', file=sys.stderr)
        return 4
    ledger, ledger_problems = load_ledger()
    faces = {'declared': _run_audit(['-r', REQUIREMENTS]),
             'environment': {'status': 'skipped', 'vulns': [], 'error': '--skip-env'}
             if args.skip_env else _run_audit([])}
    open_ids = {i for f in faces.values() for r in f['vulns'] for i in (r.get('ids') or [])}
    ghosts = sorted(set(ledger) - open_ids)
    receipt = {'schema': 'ican-dependency-audit-v1',
               'generated_at': _now().isoformat(timespec='seconds'),
               'tool_pin': TOOL_PIN,
               'faces': {k: {'status': v['status'], 'packages': v.get('packages', 0),
                             'vulnerable_packages': len(v['vulns']),
                             'vulns': v['vulns'], 'error': v.get('error', '')} for k, v in faces.items()},
               'exemptions_applied': sorted(set(ledger) & open_ids),
               'ledger_problems': ledger_problems,
               'ghost_exemptions': ghosts}
    if not args.no_write:
        with open(RECEIPT, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(receipt, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write('\n')
    # 注意：JSON 只能由 _finish 打一次。这里再打一次会让 `--json` 通道输出两个文档，
    # 下游 json.load 直接报 Extra data（本轮自己踩到的形态）。
    left = {k: _unresolved(v['vulns'], ledger) for k, v in faces.items()}
    return _finish({'faces': faces, 'left': left, 'receipt': receipt,
                    'ledger_problems': ledger_problems, 'ghosts': ghosts,
                    'as_json': bool(args.json)})


def _print_human(ctx):
    """人读面：一条总账 + 每条未豁免漏洞 + 台账问题 + 未验证面的可抄命令。"""
    faces, left, receipt = ctx['faces'], ctx['left'], ctx['receipt']
    print(f'AUDIT declared={faces["declared"]["status"]}/{len(left["declared"])} 包待处置 '
          f'environment={faces["environment"]["status"]}/{len(left["environment"])} 包待处置 '
          f'exemptions={len(receipt["exemptions_applied"])} '
          f'台账问题={len(ctx["ledger_problems"])} 幽灵豁免={len(ctx["ghosts"])}')
    for kind, rows in left.items():
        for row in rows:
            print(f'  🔴 {kind}: {row["name"]}=={row["version"]} -> {row["ids"][:3]}')
    for item in ctx['ledger_problems'][:6]:
        print(f'  🔴 台账：{item}')
    if ctx['ghosts']:
        print(f'  🔴 幽灵豁免（当前两面都没有这些 id）：{ctx["ghosts"][:6]}')
    for kind, face in faces.items():
        if face['status'] in ('no-tool', 'unreachable'):
            print(f'  ⚠️ {kind} 面未验证（{face["status"]}: {face["error"]}）⇒ 本轮不判通过，'
                  f'先装工具/接网：python -m pip install "{TOOL_PIN}"')


def _finish(ctx):
    """给类别退出码；人读/JSON 输出由 `_print_human` 负责。拆细是因为 main 原本 13 个分支。

    超预算时的正解是拆函数，不是抬基线（后者被门禁第 37 项与项目铁律同时禁止）。
    """
    statuses = [v['status'] for v in ctx['faces'].values()]
    if ctx['as_json']:
        print(json.dumps(ctx['receipt'], ensure_ascii=False, sort_keys=True))
    else:
        _print_human(ctx)
    n_left = sum(len(x) for x in ctx['left'].values())
    if ctx['ledger_problems'] or ctx['ghosts']:
        return 4
    if 'no-tool' in statuses:
        return 2
    if 'unreachable' in statuses:
        return 3
    return 1 if n_left else 0


if __name__ == '__main__':
    sys.exit(main())
