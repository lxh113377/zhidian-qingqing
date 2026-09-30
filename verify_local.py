#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""verify_local.py — 本地等价验证入口（对标第 16 轮 H26）。

为什么要有它（两条当场实测的事实）：
1. 对标组 4/8 有本地校验入口（erpnext `.pre-commit-config.yaml`、PostHog/nocodb `.husky`、
   dify `Makefile`），我方此前**一个都没有**；
2. 2026-09-25 起 GitHub Actions 被账号额度/付款拦住（`gh run list` 最近 3 条 failure，
   annotation「The job was not started because recent account payments have failed…」，
   job 从未启动、日志 `log not found`）⇒ 依赖 CI 出证据的链条断了。
   `make` 本机未装，所以不产出跑不了的 Makefile，改做成 python 入口（跨平台、当场可跑）。

它做什么：一条命令按阶段跑完「环境指纹 → git 本地状态 → 权威门禁 → 判据抽验 → 远端/CI」，
每阶段**独立记录退出码**，聚合按类别而非取最大值，最后打出一段可直接粘进台账的证据块，
并把全文输出落盘到系统临时目录（对齐"归因第一步读工具自己的 log"）。

刻意不做的事：
- 不装 git hook。本仓常有并发会话在提交，装 hook 会把别人的提交一起挡掉（钩子属跨会话副作用，
  要装应由用户决定并显式装）。
- 不反向被 `quality_gate.py` 调用：它是门禁的**调用方**，进门禁会成环。

退出码（沿用本仓镜像类工具的约定，类别码不是严重度）：
0 = 全部跑成且全过 / 1 = 有阶段失败 / 2 = 有阶段没跑成（网络不可达、脚本缺失、解释器不对）
3 = 结论依赖当天产物（此处不使用）/ 4 = 命令本身写错。
聚合优先级 1 > 4 > 2 > 0，与 `push_snapshot_mirror.merge_rc()` 同族：
**禁 `max()`**（"2 = 换网络"会盖掉"1 = 真失败"）。
"""

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

RC_OK, RC_FAIL, RC_NOTEST, RC_USAGE = 0, 1, 2, 4
# 类别优先级：真失败 > 命令写错 > 没跑成；0 只在"没有非零码"时才可能是结果。
CATEGORY_PRIORITY = (RC_FAIL, RC_USAGE, RC_NOTEST)

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PAGES = 'https://lxh113377.github.io/ican-demo'
DEFAULT_CLOUDBASE = 'https://qwer-d4gf2r76o8829463b-1458054906.tcloudbaseapp.com'

# 门禁第 10 项真跑过的六段证据（缺任一段 = 根本没比过，不得当通过）。
# 动因：本工具第一次真跑就把权威副本路径按脚本自身目录拼错，回落到 dev 跑门禁，
# 第 10 项打「未检测到可信工作区根 src…」却仍显示 ✅、阻断项从 20 静默变 19。
GATE_EVIDENCE = ('工作区根 src =', 'src 比对文件数', '探针', '根级交付脚本比对',
                 'deploy-free', '部署/')


def find_authoritative_copy(start=None):
    """就近找权威交付副本（含 quality_gate.py 的 05-源代码全文 目录）。找不到返回 None。"""
    cur = os.path.abspath(start or ROOT)
    for _ in range(6):
        cand = os.path.join(cur, '交付物', '提交包', '工程佐证材料', '05-源代码全文')
        if os.path.isfile(os.path.join(cand, 'quality_gate.py')):
            return cand
        if os.path.isfile(os.path.join(cur, 'check_snapshot_assets.py')) and \
                os.path.basename(cur) == '05-源代码全文':
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def gate_evidence_missing(out):
    """返回缺失的六段证据名；全在则空列表。"""
    return [name for name in GATE_EVIDENCE if name not in out]


def audit_gate_output(rec):
    """在阶段记录上追加一条门禁体检：降级跑成 / 六段证据缺失 一律判失败。"""
    out = rec.get('out', '')
    if not out:
        return rec
    missing = gate_evidence_missing(out)
    degraded = '未检测到可信工作区根' in out
    if degraded or missing:
        why = ('第 10 项被降级跳过（不在权威副本里跑，比对根本没发生）' if degraded
               else f'第 10 项六段证据缺失：{missing}')
        rec['rc'] = RC_FAIL
        rec['line'] = f'gate：门禁退出码为 0 但{why} —— 不得当作交付一致性已过'
    return rec


def merge_rc(codes):
    """按类别优先级合并，不按数值大小。表外返回码取最大非 0（宁吵不静默）。

    第一版把「0 在集合里」直接判成通过，于是 `[0, 128]` 返回 0 —— 夹具
    「表外返回码取最大非 0」当场把它判红。非零码先于 0 判定才是本函数的前提。
    """
    present = [c for c in codes if c is not None]
    if not present:
        return RC_NOTEST
    nonzero = [c for c in present if c]
    if not nonzero:
        return RC_OK
    for want in CATEGORY_PRIORITY:
        if want in nonzero:
            return want
    return max(nonzero)


def _failure_lines(out):
    """从一段输出里挑失败行 —— 与门禁**同一条尺**（`quality_gate._is_failure_line`）。

    动因（第 34 轮 §7-8）：这里原先也写 `'❌' in ln`，于是"通过项标题里引用了那个字符"
    会被当成失败证据顶到阶段摘要第一位，真红因反而看不见（同族第二次复发）。
    取不到门禁口径时，兜底必须是**同语义**的窄尺，绝不回落到旧的整行子串口径。
    """
    try:
        from quality_gate import _is_failure_line as pred
    except Exception:  # noqa: BLE001 - import 失败也不能把尺子换宽
        def pred(ln):
            s = ln.strip()
            return bool(s) and chr(10060) in s and not s.startswith(chr(9989))
    return [ln.strip() for ln in out.splitlines() if pred(ln)]


def run_stage(name, argv, cwd=None, net=False, blurb=''):
    """跑一个阶段，返回 (记录 dict)。argv 为 None 表示前置不满足，没跑。"""
    if argv is None:
        return {'name': name, 'rc': RC_NOTEST, 'ran': False, 'line': f'{name}：前置不满足，未跑'}
    try:
        r = subprocess.run(argv, cwd=cwd or ROOT, capture_output=True, text=True,
                           timeout=900 if not net else 120)
    except subprocess.TimeoutExpired:
        return {'name': name, 'rc': RC_NOTEST, 'ran': True, 'line': f'{name}：超时（120s/900s）未跑成'}
    except (OSError, ValueError) as exc:
        return {'name': name, 'rc': RC_NOTEST, 'ran': True,
                'line': f'{name}：起不来 {type(exc).__name__}: {exc}'}
    out = (r.stdout or '') + (r.stderr or '')
    rc = r.returncode
    tail = [ln.strip() for ln in out.splitlines() if ln.strip()][-1:] or ['无输出']
    if rc != 0:
        bad = _failure_lines(out)[:3]
        summary = bad or tail
    else:
        summary = tail
    return {'name': name, 'rc': rc, 'ran': True, 'out': out,
            'line': f'{name} rc={rc} {" ".join(summary)[:150]}', 'blurb': blurb}


def stage_env(py, gate_cwd):
    """环境指纹 + 一条真实前提：门禁/测试必须在**项目 venv 且依赖实测可导入**的解释器下跑。

    第 17 轮补第二条判据（观测）：原先只按**路径名** `envs/ican` 认解释器，把日常在用的
    项目内 `.venv` 误判成"未跑成"（同一子串语义的另一半没实现，属"拿环境字符串猜能力"）。
    但**名字不能整个扔掉** —— L4 的 S16 断言当场把我拦下（实测复现）：本机裸
    `C:/Program Files/Python312/python.exe` 也 import 得出 flask/sqlalchemy/pandas，
    它装的是 Flask 3.0.0，与 `requirements.txt` 的 `Flask>=3.0.3,<3.2` 不符，
    L4 的 H7 依赖契约会当场红。⇒ 正解是两条都要：命名落在项目 venv 集合内 + 依赖能导入。
    """
    low = (py or '').replace('\\', '/').lower()
    named = 'envs/ican' in low or '/.venv/' in low
    probe = subprocess.run(
        [py, '-c', 'import flask, sqlalchemy, pandas; print("deps-ok")'],
        capture_output=True, text=True)
    deps_ok = 'deps-ok' in (probe.stdout or '')
    if not named:
        return {'name': 'env', 'rc': RC_NOTEST, 'ran': True,
                'line': (f'env：解释器 {py} 不在项目 venv 集合内（envs/ican 或 .venv）——'
                         '本机裸 python 的 Flask 是 3.0.0，与 requirements 钉版区间不符，'
                         '门禁会因依赖契约假红（AGENTS 工具陷阱节）；本次判"未跑成"而非判过')}
    if not deps_ok:
        return {'name': 'env', 'rc': RC_NOTEST, 'ran': True,
                'line': (f'env：{py} 缺门禁所需依赖（flask/sqlalchemy/pandas 之一）'
                         '——门禁会因缺依赖假红；本次判"未跑成"而非判过')}
    if not gate_cwd or not os.path.isfile(os.path.join(gate_cwd, 'quality_gate.py')):
        return {'name': 'env', 'rc': (RC_NOTEST if gate_cwd is None else RC_FAIL), 'ran': True,
                'line': (f'env：找不到权威副本（05-源代码全文/quality_gate.py）——'
                         f'在开发副本里跑门禁会让第 10 项自跳过却显示 ✅，故拒绝降级执行'
                         if gate_cwd is None else f'env：权威副本路径存在但缺 quality_gate.py（{gate_cwd}）')}
    v = subprocess.run([py, '-c', 'import sys;print(sys.version.split()[0])'],
                       capture_output=True, text=True)
    return {'name': 'env', 'rc': RC_OK, 'ran': True,
            'line': f'env：解释器 {py}（python {v.stdout.strip()}，项目 venv + 依赖实测齐），权威副本就位'}


def stage_version():
    """版本契约的 **git 腿**：APP_VERSION ⇄ 最新 `v*` tag ⇄ 仓库根 README 版本行。

    为什么放这里而不是 quality_gate 第 25 项：CI 是 depth=1 浅克隆，`git describe`
    在那里取不到 tag —— 把 git 派生值当门禁判据会造出"本地绿、CI 红"的假失败。
    本阶段只在本地跑（git 与 tag 都在手边），拿不到 git 时判"未跑成"而非通过。
    """
    def _git(*a):
        try:
            r = subprocess.run(['git'] + list(a), capture_output=True, text=True)
        except OSError:
            return None
        return (r.stdout or '').strip() if r.returncode == 0 else None

    top = _git('rev-parse', '--show-toplevel')
    if top is None:
        return {'name': 'version', 'rc': RC_NOTEST, 'ran': True,
                'line': 'version：git 不可用或不在仓库内（本阶段没测到，不得判通过）'}
    vp = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src', 'version.py')
    ver = ''
    try:
        m = re.search(r"^APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]",
                      open(vp, encoding='utf-8').read(), re.M)
        ver = m.group(1) if m else ''
    except OSError:
        ver = ''
    problems = []
    if not ver:
        problems.append(f'读不到 APP_VERSION（{vp}）')
    # 取"最新已发布版本"必须按**版本序**，不能按 creatordate：同一秒内补打两个 tag 时
    # （第 39 轮真实现场：一次命令里先后补签 v1.17.0 与 v1.18.0，taggerdate 同刻）
    # git 对并列项按 refname 兜底 ⇒ `-creatordate` 会把 v1.17.0 排在前面，判据自造假红。
    tags = [t for t in (_git('tag', '-l', '--sort=-v:refname', 'v[0-9]*') or '').splitlines() if t]
    if not tags:
        problems.append('仓库无任何 v* 语义 tag（发布身份未建立）')
    elif ver and tags[0].lstrip('v') != ver:
        problems.append(f'最新 tag {tags[0]} ≠ APP_VERSION v{ver}（改版本没跟着打 tag）')
    readme = os.path.join(top, 'README.md')
    if os.path.isfile(readme) and ver:
        try:
            with open(readme, encoding='utf-8') as fh:
                if f'v{ver}' not in fh.read():
                    problems.append('仓库根 README 无当前版本行')
        except OSError:
            problems.append('仓库根 README 读不到')
    return {'name': 'version', 'rc': RC_FAIL if problems else RC_OK, 'ran': True,
            'line': ('version：' + ('；'.join(problems) if problems
                                   else f'v{ver} 与 tag/README 同源（tag 数 {len(tags)}）'))}


def stage_git_local():
    head = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True)
    dirty = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True)
    if head.returncode != 0:
        return {'name': 'git', 'rc': RC_NOTEST, 'ran': True, 'line': 'git：不在仓库内或未初始化'}
    lines = [ln for ln in (dirty.stdout or '').splitlines() if ln.strip()]
    tracked = [ln for ln in lines if not ln.startswith('?? ')]
    return {'name': 'git', 'rc': RC_OK, 'ran': True,
            'line': f'git：HEAD={head.stdout.strip()} 跟踪文件改动 {len(tracked)} 项 / 未跟踪 {len(lines) - len(tracked)} 项'
                    + (f' → {tracked[:3]}' if tracked else '')}


def stage_remote_and_ci(repo, pages, cloudbase):
    """联网阶段：远端一致性 + CI 结论 + 静态镜像资源。任一不可达只判"未跑成"。"""
    recs = []
    gh = shutil.which('gh')
    if gh:
        remote = subprocess.run(['git', 'ls-remote', 'origin', 'refs/heads/main'],
                                capture_output=True, text=True, timeout=60)
        local = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True)
        r_sha = (remote.stdout or '').split('\t')[0].strip() if remote.returncode == 0 else ''
        if r_sha:
            same = r_sha == (local.stdout or '').strip()
            recs.append({'name': 'remote', 'rc': RC_OK if same else RC_FAIL, 'ran': True,
                         'line': f'remote：本地/远端 main {"一致" if same else "不一致（未推送？）"} '
                                 f'{r_sha[:7]}'})
        else:
            recs.append({'name': 'remote', 'rc': RC_NOTEST, 'ran': True,
                         'line': 'remote：ls-remote 取不到（网络/认证），不得当作已同步'})
        runs = subprocess.run([gh, 'run', 'list', '-R', repo, '--limit', '1', '--json',
                               'headSha,conclusion,status,workflowName'],
                              capture_output=True, text=True, timeout=60)
        if runs.returncode == 0 and runs.stdout.strip():
            txt = runs.stdout.strip().replace('\n', ' ')[:160]
            bad = '"conclusion":"failure"' in txt.replace(' ', '') or 'failure' in txt
            recs.append({'name': 'ci', 'rc': RC_NOTEST if bad else RC_OK, 'ran': True,
                         'line': 'ci：' + (f'最近一次非 success → 证据链此刻只能靠本地：{txt}'
                                           if bad else f'最近一次运行：{txt}')})
        else:
            recs.append({'name': 'ci', 'rc': RC_NOTEST, 'ran': True,
                         'line': 'ci：gh 取不到运行结论（未跑成，不等于 CI 通过）'})
    else:
        recs.append({'name': 'remote', 'rc': RC_NOTEST, 'ran': True, 'line': 'remote/ci：无 gh，跳过'})
    for label, url in (('mirror-cloudbase', cloudbase), ('mirror-pages', pages)):
        if not url:
            continue
        recs.append(_stage_asset(url, label))
    return recs


def _stage_asset(url, label):
    script = os.path.join(ROOT, 'check_snapshot_assets.py')
    if not os.path.isfile(script):
        return {'name': label, 'rc': RC_NOTEST, 'ran': True, 'line': f'{label}：守卫脚本缺失'}
    r = subprocess.run([sys.executable, 'check_snapshot_assets.py', '--online', url],
                       cwd=ROOT, capture_output=True, text=True, timeout=150)
    out = (r.stdout or '') + (r.stderr or '')
    if r.returncode == RC_NOTEST:
        line = f'{label}：站点不可达或未跑成（不判正常也不判异常）'
    else:
        line = [ln.strip() for ln in out.splitlines() if ln.strip()][-1:] or [f'{label} 无输出']
        line = f'{label}：{line[0][:120]}' if isinstance(line, list) else line
    return {'name': label, 'rc': r.returncode, 'ran': True, 'line': line}


# ── md 写入卫生（对标第 19 轮 M64）──────────────────────────────
# 动因：同一族坑（shell 内联写入把「反斜杠+字母」解析成转义、把反引号当命令替换）
# 在台账/日志上已复发 4 次，前几轮收口文字都是"下次注意"。按本仓 Step 2.7 的
# 复发计数硬门槛，第 3 次起必须交付机器可判载体，故本阶段挂在执行路径上：
# `push_snapshot_mirror.preflight()` 会先跑本工具，红即拒绝外发。
# 标定（2026-09-26 实测 136 份 md）：控制字符命中 **0** ⇒ 可作硬判据；
# 「空反引号对」命中 6 处且全是合法形态（A-project-handoff 的 `- `` — ` 空占位、
# 嵌套代码示例）⇒ 两侧区间重叠，**不作品判据**，只如实登记为已知盲区。
MD_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x85\x9b]')


def md_hygiene_hits(text):
    """返回一份 markdown 里的控制字符命中（去重后按码位排序，最多报 6 个）。"""
    found = sorted({ord(m.group(0)) for m in MD_CTRL_RE.finditer(text)})
    return [f'U+{c:04X}' for c in found[:6]], len(found)


def scan_md_hygiene(paths):
    """扫描给定 markdown 文件；读不到的文件算"没测到"而不是"没问题"。"""
    problems = []
    unreadable = []
    for p in paths:
        try:
            with io.open(p, encoding='utf-8') as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append(f'{p}（{type(exc).__name__}）')
            continue
        codes, total = md_hygiene_hits(text)
        if codes:
            problems.append(f'{p} 含控制字符 {total} 种：{", ".join(codes)}')
    return problems, unreadable


def changed_md_paths(cwd=None):
    """本轮涉及 md：工作区相对 HEAD 的改动 + 未跟踪文件（`.gitignore` 已替你排除噪声）。

    取数面一律锚在 `git rev-parse --show-toplevel`：`git diff --name-only` 的输出是
    **相对 cwd** 的，而本工具会从开发副本、权威副本、pre-flight 调用点三种目录被拉起，
    按 cwd 拼路径会在子目录里拼出不存在的文件（清单为空 = 静默放行，正是 R236 的形态）。
    """
    try:
        top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=cwd,
                             capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001  git 不可用＝没测到
        return None, f'git 取数失败：{type(exc).__name__}'
    if top.returncode != 0 or not (top.stdout or '').strip():
        return None, '取不到 git 根（不在仓库里？判"没测到"而非"没问题"）'
    root = top.stdout.strip()
    paths = []
    for args, flag in ((['git', '-c', 'core.quotePath=false', 'diff', '--name-only',
                         '--diff-filter=ACMR', 'HEAD', '--', '*.md'], 'tracked'),
                       (['git', '-c', 'core.quotePath=false', 'ls-files', '--others',
                         '--exclude-standard', '--', '*.md'], 'untracked')):
        try:
            r = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=60)
        except Exception as exc:  # noqa: BLE001
            return None, f'{flag} 取数失败：{type(exc).__name__}'
        if r.returncode != 0:
            return None, f'{flag} 取数失败 rc={r.returncode}'
        paths += [line.strip() for line in (r.stdout or '').splitlines() if line.strip()]
    keep = []
    for q in paths:
        parts = [x for x in q.split('/') if x]
        # 工具自家运行时目录（.qoder*/.venv*/node_modules/.git）不是本仓的写入面，
        # 把它们算进来会让判据红在别人家的文件上
        if any(x.startswith('.') for x in parts) or 'node_modules' in parts:
            continue
        keep.append(os.path.join(root, q.replace('/', os.sep)))
    uniq = sorted(set(keep))
    return [q for q in uniq if os.path.isfile(q)], ''


def stage_sync():
    """三副本同步检查（**唯一写出口**的只读模式；漂移会在门禁之前先被点名）。

    第 37 轮接线动机：`tools/frontend_sync_check.py` 此前只覆盖 `src/ → deploy-free/src/`，
    根级交付件与 05 副本的同步一直由一个不在仓里的一次性脚本代跑 ⇒ "工具能修但没接线"= 半成品。
    现在门禁第 52 项要求写出口唯一、且本地链必须跑到它。
    """
    return run_stage('sync', [sys.executable, os.path.join('tools', 'frontend_sync_check.py')],
                     cwd=ROOT, blurb='三副本逐字节一致（MISSING/DIFF/EXTRA 三类归零）')


def stage_md_hygiene(cwd=None):
    """LOCAL-VERIFY 的一个阶段：本轮要发出去的 md 里不得有控制字符残留。"""
    paths, err = changed_md_paths(cwd)
    if paths is None:
        return {'name': 'md-hygiene', 'rc': RC_NOTEST, 'ran': True,
                'line': f'md-hygiene：{err}（取不到改动面＝没测到）'}
    if not paths:
        return {'name': 'md-hygiene', 'rc': RC_OK, 'ran': True,
                'line': 'md-hygiene：本轮 0 个 md 改动（面无内容，不是"已验过"）'}
    problems, unreadable = scan_md_hygiene(paths)
    if problems:
        return {'name': 'md-hygiene', 'rc': RC_FAIL, 'ran': True,
                'line': f'md-hygiene：{len(problems)} 个文件带控制字符（内联写入把转义吃了）'
                        f'｜{problems[0]}',
                'out': '\n'.join(problems + unreadable)}
    if unreadable:
        return {'name': 'md-hygiene', 'rc': RC_NOTEST, 'ran': True,
                'line': f'md-hygiene：{len(unreadable)} 个文件读不到，判"没测到"',
                'out': '\n'.join(unreadable)}
    return {'name': 'md-hygiene', 'rc': RC_OK, 'ran': True,
            'line': f'md-hygiene：{len(paths)} 个 md 无控制字符残留'}


def selftest():
    """聚合器与用法判据的夹具：每类都要能指名道姓地红。"""
    cases = [
        ('失败优先于"没跑成"（禁 max 掩盖）', merge_rc([RC_OK, RC_NOTEST, RC_FAIL]) == RC_FAIL),
        ('顺序无关（[2,1] 与 [1,2] 同为 1）',
         merge_rc([RC_NOTEST, RC_FAIL]) == merge_rc([RC_FAIL, RC_NOTEST]) == RC_FAIL),
        ('只有没跑成时不得判过', merge_rc([RC_OK, RC_NOTEST]) == RC_NOTEST),
        ('全过才判 0', merge_rc([RC_OK, RC_OK]) == RC_OK),
        ('空阶段清单判"没跑成"而非通过', merge_rc([]) == RC_NOTEST),
        ('表外返回码取最大非 0（宁吵不静默）', merge_rc([RC_OK, 128]) == 128),
        # ↓ 门禁降级体检（本工具第一次真跑复现出的假通过形态，必须有双向夹具）
        ('门禁降级输出被判失败',
         audit_gate_output({'rc': RC_OK, 'line': 'gate rc=0',
                            'out': '✅ [10. 交付一致性] 通过\n     ↳ 未检测到可信工作区根 src'}
                           )['rc'] == RC_FAIL),
        ('六段证据齐备的输出不误伤（收紧判据不得误杀）',
         audit_gate_output({'rc': RC_OK, 'line': 'gate rc=0',
                            'out': '工作区根 src = x\nsrc 比对文件数：权威 181 / 工作区 181\n'
                                   '探针 routes/ai.py\n根级交付脚本比对 19 个\n'
                                   'deploy-free 副本比对：通过\n部署/ 目录比对：通过'})['rc'] == RC_OK),
        ('缺一段证据也算没过（防"少一段"被放过）',
         gate_evidence_missing('工作区根 src = x\nsrc 比对文件数\n探针\n根级交付脚本比对\n部署/') == ['deploy-free']),
    ]
    # md 写入卫生（M64）：正例 + 反例成对，缺任一侧都算这条判据没验证过
    d = tempfile.mkdtemp(prefix='ican_mdhyg_')
    try:
        bad_p = os.path.join(d, 'bad.md')
        good_p = os.path.join(d, 'good.md')
        with io.open(bad_p, 'w', encoding='utf-8', newline='') as fh:
            fh.write('路径写作 \assets 时反斜杠 a 被吃掉：' + chr(7) + ' 落盘\n')
        with io.open(good_p, 'w', encoding='utf-8', newline='') as fh:
            fh.write('正常中文正文，含 `code`、``` 围栏与 U+0007 字样但**不是**控制字符\n')
        problems, _ = scan_md_hygiene([bad_p])
        codes, _ = md_hygiene_hits(io.open(bad_p, encoding='utf-8').read())
        cases.append(('md 卫生判据有牙：注入 BEL 必须点名文件与码位',
                      len(problems) == 1 and 'U+0007' in problems[0]))
        cases.append(('md 卫生不误伤：正常 markdown（含反引号与"U+0007"字样）零命中',
                      scan_md_hygiene([good_p])[0] == []))
        cases.append(('md 卫生命中去重且按码位报（同字符多次只报一次）',
                      md_hygiene_hits('a' + chr(7) + 'b' + chr(7) + chr(11))[0]
                      == ['U+0007', 'U+000B']))
        hyg = stage_md_hygiene(cwd=d)
        cases.append(('md 卫生：取不到改动面（非 git 仓）→ 判"没测到"，不判通过',
                      hyg['rc'] == RC_NOTEST and '没测到' in hyg['line']))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    bad = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(f"  {'✅' if ok else '❌'} 夹具 {n}")
    print(f'SELFTEST {"OK" if not bad else "FAIL: " + "、".join(bad)}｜{len(cases)} 条')
    return RC_OK if not bad else RC_FAIL


def _git_root():
    """当前仓根（回执要落在仓根的 .ci/ 下，而不是某个副本里）。"""
    r = subprocess.run(['git', 'rev-parse', '--show-toplevel'],
                       capture_output=True, text=True, cwd=ROOT)
    return (r.stdout or '').strip()


RECEIPT_REL = os.path.join('.ci', 'local-run-receipt.json')
RUNS_REL = os.path.join('.ci', 'local-runs.jsonl')
MAX_RUNS = 60


def _now_iso():
    """带**真实偏移**的 ISO 时刻（写死 +08:00 在 UTC 机器上就是一条假账）。"""
    from datetime import UTC, datetime
    return datetime.now(UTC).astimezone().isoformat(timespec='seconds')


def append_run_ledger(doc, root):
    """每次真跑往运行账本追一行；超上限自动裁剪（裁剪也要留痕）。

    为什么要有账：回执是"最后一面镜子"，只能照出最后一次；第 48 项为了防自指成环
    故意不裁决 verdict ⇒ "本地闸长期红/谁在红"必须靠这串时间序列回答，否则诚实的读数
    只活在一台机器的屏幕上（M93 的原意）。
    """
    path = os.path.join(root, RUNS_REL)
    rows = []
    if os.path.isfile(path):
        try:
            rows = [ln for ln in open(path, encoding='utf-8').read().splitlines() if ln.strip()]
        except OSError:
            rows = []
    entry = dict(doc)
    # seq 取"上一行的 seq + 1"而不是"行数 + 1"：裁剪到封顶后按行数会恒等于 61，
    # 一个不再递增的序号既证明不了没被中间删行，也证明不了这本账是追加出来的。
    prev = 0
    for ln in reversed(rows):
        try:
            prev = int(json.loads(ln).get('seq') or 0)
        except (ValueError, TypeError, AttributeError):
            continue
        break
    entry['seq'] = prev + 1
    rows.append(json.dumps(entry, ensure_ascii=False, sort_keys=True))
    pruned = 0
    if len(rows) > MAX_RUNS:
        pruned = len(rows) - MAX_RUNS
        rows = rows[-MAX_RUNS:]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline=chr(10)) as fh:
            fh.write(chr(10).join(rows) + chr(10))
    except OSError as exc:
        print(f'  ❌ 运行账本写不出去：{type(exc).__name__}: {exc}')
        return ''
    print(f'  运行账本已追加：{os.path.relpath(path, ROOT).replace(chr(92), chr(47))}'
          f'（现 {len(rows)} 行' + (f'，裁剪掉最早 {pruned} 行' if pruned else '') + '）')
    return path



def write_receipt(doc, root):
    """把一次运行的结论落成回执。写不出去必须显式说，不许静默。

    刻意记 `head`：第 48 项要能证明"这份回执覆盖当前提交或它的某个祖先"，
    否则一份旧回执可以永远吃老本。
    """
    path = os.path.join(root, RECEIPT_REL)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline=chr(10)) as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write(chr(10))
    except OSError as exc:
        print(f"  ❌ 回执写不出去：{type(exc).__name__}: {exc}")
        return ""
    rel = os.path.relpath(path, ROOT).replace(chr(92), chr(47))
    print('  回执已落盘：' + rel + '（head=' + str(doc.get('head'))[:7]
          + ' verdict=' + str(doc.get('verdict')) + '）')
    return path


def _emit_receipt_and_ledger(records, final, fast):
    """唯一写盘入口：回执 + 运行账本必须同源同次产出（分两处写就会各写各的）。"""
    root = _git_root()
    if not root:
        print('  ⚠️ 回执未写：找不到仓根（git rev-parse --show-toplevel 空）')
        return ''
    head = subprocess.run(['git', '-C', root, 'rev-parse', 'HEAD'],
                          capture_output=True, text=True).stdout.strip()
    doc = {'schema': 'ican-local-run-receipt-v1', 'at': _now_iso(), 'head': head,
           'mode': 'fast' if fast else 'full', 'entry': 'verify_local.py', 'rc': final,
           'verdict': {0: 'ok', 1: 'fail', 2: 'notest', 4: 'usage'}.get(final, f'rc{final}'),
           'stages': [{'name': r.get('name'), 'rc': r.get('rc')} for r in records if r.get('ran')]}
    path = write_receipt(doc, root)
    if path:
        append_run_ledger(doc, root)
    return path


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(
        description='本地等价验证入口：一条命令跑完门禁 + 判据抽验 + 远端/CI/镜像，输出可粘贴证据块')
    ap.add_argument('--fast', action='store_true', help='门禁走 --fast（跳过三套测试，只跑静态段）')
    ap.add_argument('--skip-net', action='store_true', help='跳过远端/CI/静态镜像等联网阶段')
    ap.add_argument('--selftest', action='store_true', help='只跑聚合器夹具')
    ap.add_argument('--receipt', action='store_true',
                    help='把本次结论写成 <仓根>/.ci/local-run-receipt.json —— 本地闸的运行回执，供门禁第 48 项核龄（不装 git hook 时的替代强制点）')
    ap.add_argument('--repo', default='lxh113377/ican', help='CI 查询用的 owner/repo')
    ap.add_argument('--pages', default=DEFAULT_PAGES)
    ap.add_argument('--cloudbase', default=DEFAULT_CLOUDBASE)
    try:
        args, unknown = ap.parse_known_args(argv)
    except SystemExit:
        return RC_USAGE
    if unknown:
        print(f'❌ 未知参数：{unknown}（支持 --fast / --skip-net / --selftest / --repo / --pages / --cloudbase）')
        return RC_USAGE
    if args.selftest:
        return selftest()

    started = time.strftime('%Y-%m-%d %H:%M:%S')
    log_dir = tempfile.mkdtemp(prefix='ican_verify_')
    full_log = os.path.join(log_dir, f'local-verify-{time.strftime("%Y%m%d-%H%M%S")}.log')
    # 权威副本找不到时**不回落到开发副本**：在开发副本里跑门禁会让第 10 项自跳过却显示 ✅
    gate_cwd = find_authoritative_copy()
    records = [stage_env(sys.executable, gate_cwd), stage_git_local(), stage_version(),
               stage_md_hygiene(), stage_sync()]

    if gate_cwd:
        gate_flags = ['--fast'] if args.fast else []
        records.append(audit_gate_output(
            run_stage('gate', [sys.executable, 'quality_gate.py'] + gate_flags,
                      cwd=gate_cwd, blurb='权威门禁')))
        records.append(run_stage('judge-selftest',
                                [sys.executable, 'check_snapshot_assets.py', '--selftest'],
                                cwd=gate_cwd, blurb='门禁 PASS 的抽验（判据是否真跑到目标）'))
    if not args.skip_net:
        records.extend(stage_remote_and_ci(args.repo, args.pages, args.cloudbase))
    else:
        records.append({'name': 'net', 'rc': RC_NOTEST, 'ran': False, 'line': 'net：--skip-net 显式跳过'})

    ran = [r for r in records if r.get('ran')]
    if not ran:
        print('❌ 一个阶段都没跑起来 —— 不得判通过')
        return RC_NOTEST
    final = merge_rc([r['rc'] for r in ran])
    fails = [r for r in ran if r['rc'] == RC_FAIL]
    notest = [r for r in ran if r['rc'] not in (RC_OK, RC_FAIL)]



    head = ('🔴 本地验证有失败' if final == RC_FAIL
            else '⚠️ 本地验证部分未跑成' if final == RC_NOTEST else '✅ 本地验证全过')
    print(f'═══ LOCAL-VERIFY {started}（{"fast" if args.fast else "full"}）═══')
    for r in ran:
        mark = '✅' if r['rc'] == RC_OK else ('❌' if r['rc'] == RC_FAIL else '⚠️')
        print(f'  {mark} {r["line"]}')
    print(f'LOCAL-VERIFY 总结：阶段 {len(ran)} 个 / 失败 {len(fails)} / 未跑成 {len(notest)} → rc={final}')
    print(f'  失败阶段：{[r["name"] for r in fails] or "无"}｜未跑成：{[r["name"] for r in notest] or "无"}')
    try:
        with open(full_log, 'w', encoding='utf-8') as fh:
            for r in ran:
                fh.write(f'\n════ {r["name"]} rc={r["rc"]} ════\n{r.get("out", r["line"])}\n')
        print(f'  全文输出已落盘：{full_log}')
    except OSError as exc:
        print(f'  ⚠️ 原文落盘失败（须人工留存输出）：{type(exc).__name__}: {exc}')
    if args.receipt:
        _emit_receipt_and_ledger(records, final, args.fast)
    return final


if __name__ == '__main__':
    sys.exit(main())
