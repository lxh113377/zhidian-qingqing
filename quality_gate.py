"""
质量门禁脚本 — 一键执行《代码审查标准与流程.md》中的 🔴 阻断项自动化检查

用法：
    python quality_gate.py            # 全量检查（含冒烟 + 漏洞回归）
    python quality_gate.py --fast     # 仅静态检查，跳过测试（提交前快检）

设计原则：
  1. 无构建步骤、不联网：仅用标准库，任何机器 clone 下来即可跑（选标准库是为"装了才能跑"
     这个具体属性，不是因为依赖本身邪恶）
  2. 每一项检查都对应审查标准里的一条具体规则，可追溯
  3. 有 ruff 就跑 ruff，没有则跳过，不阻断

退出码：0 = 全部通过；1 = 存在阻断项
"""
import ast
import compileall
import contextlib
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, 'src')
TPL = os.path.join(SRC, 'templates')
PY = sys.executable

results = []  # (级别, 检查名, 是否通过, 详情列表)
# 第 56 项的账（第 47 轮 M152）：每个检查项经 `_guarded` 跑一遍，抛出异常的记在这里。
# 形状是 (函数名, '类型: 消息', traceback 末三行) —— 不是布尔，因为"崩了"必须能被指认。
CRASHED_CHECKS: list = []


def _guarded(fn, *args, **kwargs):
    """跑一个门禁项；**它自己崩了只算这一项没测到**，不得带走后面的所有项。

    动因（第 46 轮自曝 ①，本机实测）：我在 L4 正在重新 read 被测源码的时候分步改文件，
    一条判据腿读到半截源码抛出 `NameError`，整个套件直接崩掉 —— 那天的形态不是"某项红"，
    而是"所有项一起消失"，摘要行根本没印出来，也就没人知道另外 51 项跑没跑。
    上游对照三种既有形状：
      · golang/go `src/testing/testing.go:2039 tRunner` + `:2057 err := recover()`：
        用例跑在自己的 goroutine 里，fuzzing 模式下 panic 被转成一次普通失败
        （`:2088` `t.Errorf("%s%s\\n%s\\n", prefix, err, debug.Stack())`）并把 `finished` 置真，
        注释原文 `:2054` "It's important that the process keeps running"；
      · pre-commit `pre_commit/commands/run.py:293-302`：`_run_single_hook` 返回
        `current_retval`，主循环 `retval |= current_retval` 逐项累加，只有显式 `fail_fast` 才 break
        —— "要不要一红就停"是**调用方的选择**，不是异常处理的默认；
      · 本仓 `quality_gate.py:1788` 与 `:3700`：行为核早就各自裹了一层，唯独 `main()` 里
        那 48 个裸调用没裹 ⇒ 抗崩只覆盖"跑测试的子进程"，不覆盖"跑检查的本进程"。
    只捕 `Exception`：`SystemExit` / `KeyboardInterrupt` 是**有意退出**，本门禁末尾就靠
    `sys.exit(0/1)` 表达结论，把它吞掉会把"判红"变成"跑完了但没结论"（假绿，比崩更坏）。
    """
    name = getattr(fn, '__name__', repr(fn))
    try:
        return fn(*args, **kwargs)
    except Exception as exc:                      # noqa: BLE001  一条腿崩不得带走整套
        tail = traceback.format_exc().strip().splitlines()[-3:]
        CRASHED_CHECKS.append((name, f'{type(exc).__name__}: {exc}', ' | '.join(tail)))
        # 立刻打印，不等摘要：后面的项若也崩，这条证据不能只存在于永远印不出来的汇总里
        print(f'🔴 [56. 门禁项自身异常] {name}() 抛出 {type(exc).__name__}: {exc}'
              ' ⇒ 本项判"没测到"，其余项继续跑')
        for line in tail:
            print(f'     ↳ {line[:260]}')
        return None
    # 刻意**不写** `except BaseException`：SystemExit / KeyboardInterrupt 是有意退出，
    # 本门禁末尾就靠 sys.exit(0/1) 表达结论，吞掉它会把"判红"变成"跑完了但没结论"。
    # L4 S55 有一条对偶腿专量这个方向（只测"该拦的拦住了"会造出永久假绿）。


def _crashed_names():
    """本次运行里"抛异常没跑完"的项函数名（第 56 项与 main 末尾的退出面共用）。"""
    return [c[0] for c in CRASHED_CHECKS]


def _count_or_missing(value):
    """测试套件的自报计数读数：**崩了** 与 **量为 0** 必须分形（0 是测过了，崩是没测）。"""
    return '没测到（该腿抛异常，见第 56 项）' if value is None else f'{value} 项'


def record(level, name, ok, details=None):
    results.append((level, name, ok, details or []))
    icon = '✅' if ok else ('🔴' if level == 'BLOCK' else '🟡')
    print(f'{icon} [{name}] {"通过" if ok else "未通过"}')
    for d in (details or [])[:15]:
        print(f'     ↳ {d}')
    if details and len(details) > 15:
        print(f'     ↳ …另有 {len(details) - 15} 处')


def iter_files(base, exts):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for fn in filenames:
            if os.path.splitext(fn)[1] in exts:
                yield os.path.join(dirpath, fn)


def iter_rel(base):
    """遍历 base 下全部真实文件（跳过 __pycache__/pyc），产出 (绝对路径, 相对路径)。"""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for fn in filenames:
            if fn.endswith('.pyc'):
                continue
            p = os.path.join(dirpath, fn)
            yield p, os.path.relpath(p, base)


def rel(p):
    return os.path.relpath(p, ROOT).replace('\\', '/')


def read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


# ══════════════ 1. 语法编译（BLOCK）══════════════
def check_compile():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        ok = compileall.compile_dir(SRC, quiet=2, force=True)
    record('BLOCK', '1. Python 语法编译', bool(ok),
           [] if ok else [buf.getvalue().strip()[:500]])


# ══════════════ 2. 模板 XSS 面：| safe（BLOCK）══════════════
def check_safe_filter():
    hits = []
    for p in iter_files(TPL, {'.html'}):
        for i, line in enumerate(read(p).splitlines(), 1):
            if re.search(r'\|\s*safe\b', line):
                hits.append(f'{rel(p)}:{i} 使用 | safe，应改用 | tojson 或去掉')
    record('BLOCK', '2. 模板无 | safe 透传（XSS 面）', not hits, hits)


# ══════════════ 3. 内联静态样式（BLOCK）══════════════
# 规则：style="..." 只允许含 Jinja 表达式（动态计算值，如进度条宽度）；
#      纯静态样式必须迁到 style.css，否则破坏主题切换。
def check_inline_style():
    hits = []
    for p in iter_files(TPL, {'.html'}):
        for i, line in enumerate(read(p).splitlines(), 1):
            for m in re.finditer(r'style="([^"]*)"', line):
                val = m.group(1)
                if '{{' not in val and '{%' not in val:
                    hits.append(f'{rel(p)}:{i} 静态内联样式 style="{val[:60]}" 应迁入 style.css')
    record('BLOCK', '3. 模板无静态内联样式（主题一致性）', not hits, hits)


# ══════════════ 4. 跨模块调用私有方法（BLOCK）══════════════
def check_private_call():
    hits = []
    for p in iter_files(os.path.join(SRC, 'routes'), {'.py'}):
        for i, line in enumerate(read(p).splitlines(), 1):
            if line.lstrip().startswith('#'):
                continue
            for m in re.finditer(r'\b(\w+)\._([a-zA-Z]\w*)\s*\(', line):
                if m.group(1) not in ('self', 'cls'):
                    hits.append(f'{rel(p)}:{i} 调用私有方法 {m.group(1)}._{m.group(2)}()，应加公开封装')
    record('BLOCK', '4. 路由层不调用私有方法（封装性）', not hits, hits)


# ══════════════ 5. 盲审红线关键词（BLOCK）══════════════
BLIND_WORDS = ['大学', '学院', '校徽', '指导老师', '指导教师', '学校名称']
# 第三方 bundle 排除名单：与 VENDOR-ASSETS.json 的台账项交叉核对（第 27 项），
# 防「文件改名后过滤器变死码却无人发现」（实测踩过：echarts.min.js → echarts.custom.js）。
BLIND_EXCLUDES = ('echarts.custom.js',)


def check_blind_review():
    hits = []
    for p in list(iter_files(SRC, {'.py', '.html', '.css', '.js'})):
        if any(b in p for b in BLIND_EXCLUDES):  # 第三方 bundle 不扫（台账见第 27 项）
            continue
        for i, line in enumerate(read(p).splitlines(), 1):
            for w in BLIND_WORDS:
                if w in line:
                    hits.append(f'{rel(p)}:{i} 命中盲审关键词「{w}」：{line.strip()[:60]}')
    evidence = []
    pkg_hits, pkg_evidence = _scan_submit_package()
    hits.extend(pkg_hits)
    evidence.extend(pkg_evidence)
    record('BLOCK', '5. 盲审红线（无学校/老师/单位信息）', not hits, hits + evidence)


# ── 2026-09-16 扩展：盲审扫描范围从 src/ 扩展到最终交付包及 ZIP 内容 ──
# 背景：ai-eval 报告曾把本机 .env 绝对路径写进 JSON/MD 并随源码 ZIP 上传，
# src/ 内扫描对此不可见。覆盖 html/csv/json/md/txt/log 的绝对路径、密钥、口径词泄漏。
PKG_EXTS = {'.json', '.md', '.txt', '.log', '.html', '.csv'}
PKG_ABS_PATH = re.compile(
    r'[A-Za-z]:[\\/](?:Users|Windows)|/Users/|/c/Users|Desktop[\\/](?:workspace|Workspace)')
PKG_SECRET = re.compile(r'sk-[A-Za-z0-9]{28,}|API_KEY\s*=\s*["\']?[A-Za-z0-9]{16,}')
PKG_WORDS = ('指导老师', '指导教师', '校徽', '省一等奖', '中国大学生创新创业大赛',
             '大学生创业组', 'Latifa', 'Yeya', '刘君豪')
# 「禁止事项/检查表」语境（含否定词）中的关键词引用不判违规
PKG_NEGATION = ('不要', '禁止', '零「', '零', '不得', '勿', '不出现', '不允许', '无',
                '自检', '自查')


def find_submit_package():
    """从权威仓库向上有限层查找唯一上传目录；CI/纯净环境返回 None（跳过）。"""
    d = ROOT
    for _ in range(4):
        d = os.path.dirname(d)
        for name in ('提交包', '提交包-2026-09-30'):
            cand = os.path.join(d, '交付物', name)
            if os.path.isdir(cand):
                return cand
    return None


def _scan_pkg_text(text, tag, hits):
    """对一段交付包文本执行三条泄漏规则，命中写入 hits。"""
    for i, line in enumerate(text.splitlines(), 1):
        if PKG_ABS_PATH.search(line):
            hits.append(f'{tag}:{i} 疑似本机绝对路径：{line.strip()[:70]}')
        if PKG_SECRET.search(line):
            hits.append(f'{tag}:{i} 疑似密钥泄漏：{line.strip()[:70]}')
        word = next((w for w in PKG_WORDS if w in line), None)
        if word and not any(n in line for n in PKG_NEGATION):
            hits.append(f'{tag}:{i} 命中口径词「{word}」：{line.strip()[:70]}')


def _scan_submit_package():
    """扫描唯一上传目录内的 html/csv/json/md/txt/log 及 ZIP 同类成员。

    Returns:
        tuple: (违规命中列表, PASS 时的可复核证据列表)。
    """
    pkg = find_submit_package()
    if pkg is None:
        return [], ['交付包扩展扫描：未检测到 交付物/提交包（CI/纯净环境跳过）']
    hits, n_files, n_zip = [], 0, 0
    for dirpath, _dirnames, filenames in os.walk(pkg):
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in PKG_EXTS:
                n_files += 1
                p = os.path.join(dirpath, fn)
                with open(p, encoding='utf-8', errors='ignore') as f:
                    _scan_pkg_text(f.read(), os.path.relpath(p, pkg).replace('\\', '/'), hits)
    zpath = next((os.path.join(pkg, f) for f in sorted(os.listdir(pkg))
                  if f.endswith('.zip')), None)
    if zpath:
        import zipfile
        try:
            with zipfile.ZipFile(zpath) as z:
                for name in z.namelist():
                    if os.path.splitext(name)[1].lower() in PKG_EXTS:
                        n_zip += 1
                        _scan_pkg_text(z.read(name).decode('utf-8', errors='ignore'),
                                       f'[zip]{name}', hits)
        except zipfile.BadZipFile:
            hits.append(f'{os.path.basename(zpath)} 无法解析（BadZipFile）')
    scanned = '/'.join(sorted(ext.lstrip('.') for ext in PKG_EXTS))
    evidence = [f'交付包扩展扫描：{os.path.basename(pkg)} 内文档 {n_files} 个 + '
                f'ZIP 内文档 {n_zip} 个（{scanned}），0 命中']
    return hits, evidence


# ══════════════ 6. 硬编码颜色（WARN）══════════════
# 颜色应走 CSS 变量，便于暗色/亮色双主题；此项仅告警不阻断。
def check_hardcoded_color():
    hits = []
    css = os.path.join(SRC, 'static', 'css', 'style.css')
    if os.path.exists(css):
        for i, line in enumerate(read(css).splitlines(), 1):
            s = line.strip()
            # 豁免 1：CSS 自定义属性（--xxx: #hex）本身就是设计令牌的定义源头
            # 豁免 2：注释行
            if s.startswith(('--', '/*', '*')):
                continue
            if re.search(r':\s*#[0-9a-fA-F]{3,8}\b', s) and 'var(' not in s:
                hits.append(f'{rel(css)}:{i} 未走变量的硬编码颜色：{s[:60]}')
    record('WARN', '6. CSS 颜色走变量（主题可切换）', not hits, hits)


# ══════════════ 7. ruff（WARN）══════════════
# 探测顺序：当前解释器所在 Scripts/bin → PATH → python -m ruff。
# 三者皆无时记为 WARN 未通过（而非静默跳过），避免静态告警长期不可见。
def find_ruff():
    """真实探测 ruff 可执行文件。

    Returns:
        list[str] | None: 可直接拼接子命令的调用前缀，未探测到时为 None。
    """
    scripts_dir = os.path.dirname(os.path.abspath(PY))
    for path in (shutil.which('ruff', path=scripts_dir), shutil.which('ruff')):
        if path:
            return [path]
    probe = subprocess.run([PY, '-m', 'ruff', '--version'],
                           capture_output=True, text=True)
    if probe.returncode == 0:
        return [PY, '-m', 'ruff']
    return None


def check_ruff():
    cmd = find_ruff()
    if cmd is None:
        record('WARN', '7. ruff 静态检查', False, [
            '未探测到 ruff 可执行文件：'
            f'已查找 venv 目录 {os.path.dirname(os.path.abspath(PY))}、系统 PATH 及 python -m ruff',
            '静态检查未执行（告警项被跳过 = 风险不可见），请在当前 venv 执行：'
            f'"{PY}" -m pip install ruff',
        ])
        return
    r = subprocess.run([*cmd, 'check', 'src'],
                       cwd=ROOT, capture_output=True, text=True)
    problems = (r.stdout.strip().splitlines()[:15] if r.returncode else [])
    # 根级脚本此前**不在任何 lint 面里**（`ruff check src` 只覆盖 src/）——
    # 第 33 轮一手代价：本地验证入口被插入语句切断后 `main()` 提前返回，
    # 整轮跑完零输出还退出 0，两处未定义名只有 ruff 能当场抓到。
    # 取数面**从盘上派生**（根目录 *.py），不手抄名单：名单会漏、会把新脚本挡在门外，
    # 而且在这里写出入口文件名会撞上本仓"门禁不得反向引用本地验证入口"那条防成环判据。
    roots = sorted(f for f in os.listdir(ROOT) if f.endswith('.py'))
    r2 = subprocess.run([*cmd, 'check', '--select', 'F821,E9', '--output-format', 'concise'] + roots,
                        cwd=ROOT, capture_output=True, text=True)
    if r2.returncode:
        problems += ['根级脚本有未定义名/语法错：'] + (r2.stdout.strip().splitlines()[:10])
    record('WARN', f'7. ruff 静态检查（{cmd[0]}）',
           r.returncode == 0 and r2.returncode == 0,
           problems or [f'src 面 rc={r.returncode}、根级脚本面 rc={r2.returncode}'
                        f'（根级扫 {len(roots)} 个文件，判据 F821/E9）'])


# ══════════════ 10. 交付一致性（BLOCK）══════════════
# 防漏同步红线（P0，2026-08-25 教训）：只改工作区根 src 而不同步权威 = 未交付。
# 对比「工作区根 src」与「权威代码仓库 src」的 SHA256 哈希，任一文件不一致即阻断。
# 仅在本机权威环境校验；CI（检出 GitHub 仓库、无工作区根）自动跳过，不误报。
def _real_nested(a: str, b: str) -> bool:
    """realpath 后 a 与 b 是否同一目录或存在祖先/子孙嵌套（防自比用）。

    Returns:
        bool: 存在自比关系返回 True；无法判定（OSError/跨盘）也按
        危险处理返回 True，宁可跳过候选也不冒险自比。
    """
    try:
        ra, rb = os.path.realpath(a), os.path.realpath(b)
        return ra == rb or os.path.commonpath([ra, rb]) in (ra, rb)
    except (OSError, ValueError):
        return True


def find_ws_src():
    """有限层上溯搜索工作区根 src（防自比守卫版，2026-09-10）。

    假通过根因（R220 实证）：旧版 os.path.isdir 命中即比对——
    若命中权威同树内的 src 副本，或 junction/软链指向权威自身
    （isdir 会跟随链接），哈希比对变成「自己比自己」恒 PASS，
    且门禁输出无任何线索可发现。守卫：候选与 SRC 同 inode
    （samefile，覆盖 junction/软链）或 realpath 后同目录/嵌套
    → 跳过该候选继续上溯，并记录供证据输出。

    Returns:
        tuple: (工作区 src 路径 | None, 被守卫跳过的候选列表)。
    """
    d = ROOT
    candidates = []
    for _ in range(6):
        d = os.path.dirname(d)
        candidates.append(os.path.join(d, 'iCAN大学生创新创业大赛', 'src'))
        candidates.append(os.path.join(d, 'src'))
    skipped = []
    for cand in candidates:
        try:
            st = os.lstat(cand)
        except OSError:
            continue  # 不存在/不可达
        if getattr(st, 'st_file_attributes', 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            # junction/软链：跟随即可能指回权威造成自比（实测 WinError 448，
            # Python 3.13 isdir 对不受信装入点返回 False，旧版会跟随），一律拒绝并留痕
            skipped.append(f'{cand}（reparse point/链接，拒绝跟随以防自比）')
            continue
        if not os.path.isdir(cand):
            continue
        try:
            same = os.path.samefile(cand, SRC)
        except OSError:
            same = False
        if same or _real_nested(cand, SRC):
            skipped.append(f'{cand}（与权威同树/同文件）')
            continue
        return cand, skipped
    return None, skipped


def _sha256_file(p):
    """文件 SHA256（十六进制）。"""
    with open(p, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def _tree_diffs(dir_a: str, dir_b: str, a_label: str, b_label: str):
    """逐文件比对两个目录树，返回差异列表（含空集保护）。

    Args:
        dir_a: 基准目录（权威）。
        dir_b: 待比对目录（工作区/部署副本）。
        a_label / b_label: 差异文案里的可读标签。

    Returns:
        list[str]: 差异描述；空列表表示一致。任一侧为空集时返回阻断项，
        杜绝「空对空 = 一致」的假通过。
    """
    a_files = {r: p for p, r in iter_rel(dir_a)}
    b_files = {r: p for p, r in iter_rel(dir_b)}
    diffs = []
    for rel, bp in b_files.items():
        ap = a_files.get(rel)
        if ap is None:
            diffs.append(f'{rel} {b_label}独有，{a_label}缺失')
        elif _sha256_file(ap) != _sha256_file(bp):
            diffs.append(f'{rel} 哈希不一致（{a_label}↔{b_label}）')
    for rel in a_files:
        if rel not in b_files:
            diffs.append(f'{rel} {a_label}独有，{b_label}缺失')
    if not a_files or not b_files:
        diffs.append(f'比对集为空（{a_label} {len(a_files)} / {b_label} '
                     f'{len(b_files)} 个文件），无法确认一致性')
    return diffs


# 根级「必同步」交付脚本/配置（2026-09-10 扩展：此前仅比 src，根级漂移不可见）
ROOT_DELIVERED = (
    'init_db.py', 'smoke_test.py', 'test_vuln_regression.py', 'test_l4_assertions.py',
    'test_fixtures.py', 'test_ph3_demo.py', 'quality_gate.py', 'backup_db.py',
    'build_snapshot_set.py',
    'test_dify_integration.py', 'requirements.txt', 'ruff.toml',
    # 2026-09-15 新增：AI 证据化评测 CLI（离线可复现的 AI 能力证据），
    # 与其它交付脚本同等待遇，只改权威不同步工作区即阻断。
    'ai_eval.py',
    # 2026-09-24 第 8 轮新增：开放 API 规范漂移守卫（门禁第 15 项依赖它）
    'check_openapi_drift.py',
    # 2026-09-24 第 10 轮新增：Schema 漂移守卫（门禁第 17 项依赖它）
    'check_schema_drift.py',
    # 2026-09-25 第 11 轮新增：查询预算守卫（门禁第 18 项依赖它）
    'check_query_budget.py',
    # 2026-09-25 第 14 轮新增：快照被引资源完整性守卫（门禁第 23 项依赖它）
    'check_snapshot_assets.py',
    # 2026-09-27 第 32 轮新增：依赖已知漏洞审计（门禁第 46 项读它的回执）
    'check_dependency_audit.py',
    # 2026-09-27 第 33 轮新增：备份恢复出口（门禁第 47 项依赖它）
    'restore_db.py',
    # 2026-09-27 第 35 轮新增（M103）：版本字面量载体枚举器 —— 第 25 项核"码/README/UPGRADE 一致"，
    # 但"有哪几个载体"这件事本身过去靠人 grep 记忆，第 34 轮实测漏改一处 ⇒ 交给这条命令记。
    'set_version.py',
    'DEPENDENCY-AUDIT.json', 'DEPENDENCY-AUDIT-EXEMPTIONS.json',
    # 2026-09-25 第 16 轮新增：本地等价验证入口（CI 被账号额度拦停期间的唯一证据生产者；
    # 它调用门禁而非被门禁调用，故只登记白名单、不加门禁项，防成环）
    'verify_local.py',
    # 2026-09-26 第 19 轮新增：离线日志取证工具（门禁第 31 项实跑其 --selftest）
    'log_triage.py',
    # 2026-09-24 新增：覆盖率报告生成器与配置
    'run_coverage.py', '.coveragerc',
    # 2026-09-25 第 18 轮新增：第三方资产台账（第 27 项读它）+ 发布校验和台账生成器（第 29 项验它）
    # 两者都进三副本比对面——"只改权威不同步工作区"正是第 10 项要拦的形态。
    'VENDOR-ASSETS.json', 'release_evidence.py', 'THIRD-PARTY-NOTICES.md',
    # 2026-09-25 第 18 轮 H35：行尾策略随副本走一份——源码 ZIP 解压后没有 .git，
    # 门禁第 30 项要靠副本自带的这份才能继续判（不是靠仓库根那一份）。
    '.gitattributes',
    # 2026-09-26 第 24 轮新增：可维护性预算表（第 37 项的唯一输入）。它是交付物不是本机缓存——
    # 不进三副本比对面，就可能出现「权威副本的表比代码旧」而第 37 项照样判绿（表与代码分叉）。
    'MAINTENANCE_BUDGET.json',
)

# 发布台账的范围规则：唯一实现在此，`release_evidence.py` 反向 import 本模块复用，
# 避免"门禁认为该收录的文件"与"生成器实际收录的文件"两套口径（同类病：判据写两份）。
LEDGER_SKIP_DIRS = {'data', '__pycache__', '.venv', '.venv-test', '演示快照集', '.pytest_cache',
                      '.ruff_cache', 'instance', 'backups', '.git', 'htmlcov'}
# `.coverage` 无扩展名且由覆盖率步骤现生成，按文件名排除（否则第 29 项会被本次运行自己判红）
LEDGER_SKIP_NAMES = {'.coverage', '.coverage.tmp',
                       '.coverage.smoke', '.coverage.vuln', '.coverage.l4'}
LEDGER_SKIP_EXTS = ('.pyc', '.db', '.log')


def check_delivery_consistency():
    ws_src, skipped = find_ws_src()
    if ws_src is None:
        note = f'未检测到可信工作区根 src（{ROOT} 上层无匹配）——CI/纯净环境跳过本地同步校验'
        if skipped:
            note += f'；已跳过 {len(skipped)} 个与权威同树/同文件的危险候选（防自比守卫）'
        record('WARN', '10. 交付一致性（src + 根级脚本 + 部署副本）', True, [note])
        return

    ws_root = os.path.dirname(ws_src)
    diffs = _tree_diffs(SRC, ws_src, '权威', '工作区根')
    auth_n = len({r for _, r in iter_rel(SRC)})
    ws_n = len({r for _, r in iter_rel(ws_src)})
    # PASS 也必须携带可复核证据（R220：门禁 PASS 亦需抽验实证）：
    # 实际比对的工作区路径 + 文件计数 + 探针文件双端哈希，供人工核对「比对的是谁」
    evidence = [f'工作区根 src = {ws_src}',
                f'src 比对文件数：权威 {auth_n} / 工作区 {ws_n}']
    probe = os.path.join('routes', 'ai.py')
    sp_a, sp_w = os.path.join(SRC, probe), os.path.join(ws_src, probe)
    if os.path.exists(sp_a) and os.path.exists(sp_w):
        evidence.append(f'探针 {probe}：权威 {_sha256_file(sp_a)[:12]} / '
                        f'工作区 {_sha256_file(sp_w)[:12]}')

    # ── 根级交付脚本/配置逐文件比对（修根级漂移不可见的盲区）──
    root_checked = 0
    for name in ROOT_DELIVERED:
        ap, wp = os.path.join(ROOT, name), os.path.join(ws_root, name)
        if not os.path.exists(ap):
            continue
        root_checked += 1
        if not os.path.exists(wp):
            diffs.append(f'{name} 权威独有，工作区根缺失')
        elif _sha256_file(ap) != _sha256_file(wp):
            diffs.append(f'{name} 哈希不一致（工作区根↔权威）')
    evidence.append(f'根级交付脚本比对 {root_checked} 个')

    # ── 部署包 deploy-free/src（第三副本，此前无守卫，是最危险的漂移面）──
    project_root = os.path.dirname(ws_root)
    dep_src = os.path.join(project_root, 'deploy-free', 'src')
    if os.path.isdir(dep_src):
        dep_diffs = _tree_diffs(SRC, dep_src, '权威', 'deploy-free')
        diffs.extend(f'[deploy-free] {d}' for d in dep_diffs)
        evidence.append('deploy-free 副本比对：'
                        + ('通过' if not dep_diffs else f'{len(dep_diffs)} 处差异'))
    else:
        evidence.append('未发现 deploy-free/src（跳过第三副本比对）')

    # ── 同级「部署/」交付目录（Docker-Dify / pythonanywhere 上线物料）──
    auth_deploy = os.path.join(os.path.dirname(ROOT), '部署')
    ws_deploy = os.path.join(ws_root, '部署')
    if os.path.isdir(auth_deploy) and os.path.isdir(ws_deploy):
        deploy_diffs = _tree_diffs(auth_deploy, ws_deploy, '权威', '工作区')
        diffs.extend(f'[部署] {x}' for x in deploy_diffs)
        evidence.append('部署/ 目录比对：'
                        + ('通过' if not deploy_diffs else f'{len(deploy_diffs)} 处差异'))
    else:
        evidence.append('未发现 部署/ 目录（跳过物料比对）')

    evidence += [f'防自比守卫跳过候选：{p}' for p in skipped]
    record('BLOCK', '10. 交付一致性（src + 根级脚本 + 部署副本）', not diffs,
           diffs if diffs else evidence)


# ══════════════ 12. 静态资源体积预算（WARN）══════════════
# 来源：双向迭代 A2B-S2（借鉴超市web echarts 深路径按需裁剪经验）。
# 实测基线（2026-09-18）：static/js 合计约 1098.8KB，其中 echarts.min.js 1000.7KB（占 91%）。
# 当前先以「防继续膨胀」为目标的宽松上限纳入告警（不阻断、保门禁全绿），
# 完成按需裁剪后把 STATIC_JS_BUDGET_KB 收紧到 600KB，即转为实质约束。
STATIC_JS_BUDGET_KB = 700   # 裁剪后收紧（原 1200 防膨胀档）；全量 echarts 时代为 1018KB
STATIC_JS_TARGET_KB = 620   # 当前实测约 621.7KB（echarts.custom.js 604.4 + 业务 JS 约 17）


def check_static_budget():
    js_dir = os.path.join(SRC, 'static', 'js')
    if not os.path.isdir(js_dir):
        record('WARN', '12. 静态资源体积预算（防膨胀）', True, ['未找到 src/static/js，跳过'])
        return
    items = [(os.path.getsize(p), rel(p)) for p in iter_files(js_dir, {'.js'})]
    total_kb = sum(s for s, _ in items) / 1024
    ok = total_kb <= STATIC_JS_BUDGET_KB
    detail = [f'static/js 合计 {total_kb:.1f}KB / 上限 {STATIC_JS_BUDGET_KB}KB'
              f'（裁剪目标 {STATIC_JS_TARGET_KB}KB）']
    detail += [f'{n} {s / 1024:.1f}KB' for s, n in sorted(items, reverse=True)[:3]]
    record('WARN', '12. 静态资源体积预算（防膨胀）', ok, detail)


# ══════════════ 13. 探活端点存在（BLOCK）══════════════
# 双向迭代 S1（2026-09-18）：闭环事故复盘 I-5「全仓无 /healthz」。
# 有探活端点，「部署后冒烟 → 失败自动回滚」才有判定依据；没有则整条安全网无从落地。
def check_health_endpoint():
    hits = []
    app_py = os.path.join(SRC, 'app.py')
    if not os.path.isfile(app_py):
        record('BLOCK', '13. 探活端点 /healthz 存在（部署安全网前提）', False, ['未找到 src/app.py'])
        return
    if '/healthz' not in read(app_py):
        hits.append('src/app.py 未定义 /healthz 路由——部署后冒烟与回滚失去判定依据')
    record('BLOCK', '13. 探活端点 /healthz 存在（部署安全网前提）', not hits, hits)


# ══════════════ 15. 开放 API 规范漂移（BLOCK）══════════════
def check_openapi_drift():
    """跑 check_openapi_drift.py：openapi.yaml 必须与真实路由/作用域枚举一致。

    为什么是 BLOCK 而非 WARN：手写规范没有生成器兜底，漂移只能靠这道闸拦；
    「文档在骗人」比「没有文档」更危险——接入方照 404 的文档写完代码才发现。
    借鉴 Superset 的 check-openapi-spec-drift 工作流意图（漂移即红灯），
    机制改为集合对账（本仓 spec 含人工语义描述，无法无损再生成，见脚本头注释）。
    """
    script = os.path.join(ROOT, 'check_openapi_drift.py')
    if not os.path.isfile(script):
        record('BLOCK', '15. 开放 API 规范漂移（spec ⇄ 路由/作用域对账）', False,
               ['未找到 check_openapi_drift.py（漂移守卫丢失）'])
        return
    r = subprocess.run([PY, 'check_openapi_drift.py'], cwd=ROOT,
                       capture_output=True, text=True)
    out = ((r.stdout or '') + (r.stderr or '')).strip()
    # 子脚本自身用 ↳ 打头，record() 会再加一次 → 这里剥掉避免「↳ ↳」双箭头
    details = [ln.strip().lstrip('↳').strip() for ln in out.splitlines() if '↳' in ln][:15]
    if r.returncode != 0 and not details:
        details = [out[:400] or '检查脚本无输出（可能导入失败）']
    record('BLOCK', '15. 开放 API 规范漂移（spec ⇄ 路由/作用域对账）',
           r.returncode == 0, details or ['spec 端点与代码端点、作用域枚举逐项一致'])


# ══════════════ 16. AI 证据化评测通过率地板（BLOCK）══════════════
def check_ai_eval_floor():
    """跑 ai_eval.py 离线 20 场景，任一不过即红（第 9 轮 H13）。

    对标 promptfoo 的 `PROMPTFOO_PASS_RATE_THRESHOLD`（`src/node/doEval.ts:1269-1285`
    用整体通过率决定 exitCode）——成熟项目把「AI 质量回退」当 CI 门禁而不是事后报告。
    本项目地板取**满分**而非百分比：ai_eval 自身已把「越界保留」「非法证据引用」
    定为零容忍，降级路径要求 7/7，故任何 <100% 都是真回退，没有可接受区间。
    离线跑 0.9s、零网络、零 token 成本 ⇒ 放进门禁不构成负担。
    报告产物指向临时目录，避免每次门禁都脏化工作区。
    """
    import tempfile

    script = os.path.join(ROOT, 'ai_eval.py')
    if not os.path.isfile(script):
        record('BLOCK', '16. AI 证据化评测通过率地板（离线 20 场景）', False,
               ['未找到 ai_eval.py（AI 质量守卫丢失）'])
        return
    with tempfile.TemporaryDirectory(prefix='ican_eval_') as tmp:
        r = subprocess.run([PY, 'ai_eval.py', '--out-dir', tmp],
                           cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout or '') + (r.stderr or '')
    line = [ln.strip() for ln in out.splitlines() if '场景' in ln and '/' in ln]
    details = line[-2:-1] or line[:1] or ['无场景汇总行（可能提前异常退出）']
    if r.returncode != 0:
        tail = [ln.strip() for ln in out.splitlines()
                if ln.strip().startswith(('❌', '✗', 'Traceback'))][:8]
        record('BLOCK', '16. AI 证据化评测通过率地板（离线 20 场景）', False,
               details + tail)
    else:
        record('BLOCK', '16. AI 证据化评测通过率地板（离线 20 场景）', True, details)


# ══════════════ 17. Schema 漂移（ORM ⇄ 数据库，BLOCK）══════════════
def check_schema_drift():
    """跑 check_schema_drift.py：库结构必须与 ORM 声明一致。

    为什么是 BLOCK（2026-09-24 第 10 轮实证）：开发库实测比代码**少两张表**
    （api_tokens / webhook_subscriptions），对它的 ORM 查询直接抛
    `OperationalError: no such table`，而当天其余 12 项阻断项全绿——
    因为既有门禁只证明「代码之间一致」，没有任何一项证明「代码与库一致」。
    判据取业界三家共识（Alembic `check` / Django `makemigrations --check` /
    Flyway `validate` 共同点都是「有差异即非零退出」），机制不抄：本仓单进程
    SQLite 单环境，探测式幂等迁移在此处的收益高于版本式（理由不是"不加依赖"本身）。
    缺索引一条专对本仓真实事故 `bcf9fe1`（存量库迁移中断后索引缺失/重名）。
    """
    script = os.path.join(ROOT, 'check_schema_drift.py')
    if not os.path.isfile(script):
        record('BLOCK', '17. Schema 漂移（ORM ⇄ 数据库结构对账）', False,
               ['未找到 check_schema_drift.py（漂移守卫丢失）'])
        return
    r = subprocess.run([PY, 'check_schema_drift.py', '--json'], cwd=ROOT,
                       capture_output=True, text=True)
    out = (r.stdout or '') + (r.stderr or '')
    try:
        data = json.loads(out[out.index('{'):out.rindex('}') + 1])
    except (ValueError, json.JSONDecodeError):
        record('BLOCK', '17. Schema 漂移（ORM ⇄ 数据库结构对账）', False,
               [f'守卫无合法 JSON 输出（rc={r.returncode}）：'
                f'{(r.stderr or out)[:300] or "无输出——静默失败不得当通过"}'])
        return
    live = data.get('live') or {}
    fresh = data.get('fresh') or {}
    details = [
        f"实库 {data.get('live_status')}：缺表 {len(live.get('missing_tables') or [])} "
        f"缺列表 {len(live.get('missing_columns') or {})} "
        f"缺索引表 {len(live.get('missing_indexes') or {})} "
        f"已核 {live.get('checked_tables', 0)} 表",
    ]
    if data.get('live_status') == 'no_database':
        details.append('⚠️ 本机无实库（CI/纯净检出属正常），本项降级为仅「新建库自检」——'
                       '不得据此认定存量库已跟上代码')
    for t in (live.get('missing_tables') or [])[:6]:
        details.append(f'缺表 {t} → 修复：python init_db.py --if-empty')
    for t, cols in (live.get('missing_columns') or {}).items():
        details.append(f'缺列 {t}: {", ".join(cols)}')
    for t, ixs in (live.get('missing_indexes') or {}).items():
        details.append(f'缺索引 {t}: {", ".join(ixs)}')
    details.append(f"create_all 自检：缺表 {len(fresh.get('missing_tables') or [])} "
                   f"缺列表 {len(fresh.get('missing_columns') or {})} "
                   f"缺索引表 {len(fresh.get('missing_indexes') or {})}")
    for p in (data.get('ensure_list_problems') or [])[:6]:
        details.append(f'_ENSURE_COLUMNS 清单：{p}')
    record('BLOCK', '17. Schema 漂移（ORM ⇄ 数据库结构对账）',
           r.returncode == 0, details)


# ══════════════ 18. 查询预算 / N+1 回归（BLOCK）══════════════
def check_query_budget():
    """跑 check_query_budget.py：热路径 SQL 语句数不得越过整数上界。

    为什么守语句数而不是毫秒（第 11 轮对标）：
    Apache Superset 的 `purge_performance_tests.py` 自称 "Deterministic query-count guard"
    （整数基线 + MAX_STATEMENT_REGRESSION=0.10 → statement_budget=ceil(base*1.10)），
    而它**同文件里的中位耗时只 print 相对 delta、绝不断言毫秒绝对值**；
    github-action-benchmark 类 bundle 守卫默认 `fail-on-alert: false` 且基线由独立 nightly 刷新；
    Gitea / Matomo 的 CI 里根本没有性能回归 job。
    ⇒ 业界拿来做「红」的判据是**与机器速度无关的整数**，wall-clock 只作参考。
    本项目更是如此：单进程 + 1 个 SQLite + 固定演示语料 + 免费层共享 CPU，
    毫秒既无 SLA 含义又最易 flaky，写进红线只会训练人点重试。

    本轮实测这条判据有牙：把 store_metrics 探针从「7 店一次批量」改成「每店一查」，
    语句数 3 → 21，预算 4 ⇒ 立刻判红（负对照见 L4 S11 断言与报告 §6）。
    """
    script = os.path.join(ROOT, 'check_query_budget.py')
    if not os.path.isfile(script):
        record('BLOCK', '18. 查询预算（热路径 SQL 语句数上界）', False,
               ['未找到 check_query_budget.py（N+1 回归守卫丢失）'])
        return
    r = subprocess.run([PY, 'check_query_budget.py'], cwd=ROOT,
                       capture_output=True, text=True)
    out = (r.stdout or '') + (r.stderr or '')
    details = [ln.strip().lstrip('↳').strip() for ln in out.splitlines() if '↳' in ln][:14]
    if r.returncode != 0 and not details:
        details = [out[:400] or '预算脚本无输出（可能播种失败或导入异常）——不得当通过']
    record('BLOCK', '18. 查询预算（热路径 SQL 语句数上界）',
           r.returncode == 0, details or ['6 条热路径语句数均在预算内'])


_EVIDENCE_EXEMPT = {
    # 文件名 → 「为什么不进门禁」的**必须写理由**（空理由视为未豁免，判红）。
    'controlled_benchmark.py':
        '人工逐行路径 vs 聚合决策的对照实验，产物是参赛材料证据（PH6-3 已入库 '
        'controlled-benchmark-2026-09-16.md），非回归判据；口径变更时手工重跑',
    'test_dify_integration.py': '依赖外部 Dify 服务与 --network，离线 CI 不可跑（文件头有豁免声明）',
}


# 死代码里的执行形调用（第 18 轮 F2 新增的可见面）：不算接线，但要在明细里点名，
# 否则"收窄判据"同时又"静默丢弃被收窄的东西"，就只是把假绿换成无信息。
_WIRE_UNREACHABLE = []


_EXEC_FUNCS = {'run', 'Popen', 'call', 'check_call', 'check_output', 'getoutput',
               'getstatusoutput', 'system', 'popen', 'run_test'}


def _annotate_call_owner(tree):
    """给每个 Call 节点挂上它最近的**外层函数名**（模块级挂 `<module>`）。

    `ast.walk` 是广度优先，外层函数先写、内层后写 ⇒ 最终留的是最内层，正是我要的"谁包的这段"。
    直接挂在节点上而不是用 `id()` 建表：节点活着 id 才稳定，跨一次遍历就把表传过去更容易错。
    """
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Call):
                    sub._qg_owner = fn.name
    for sub in ast.walk(tree):
        if isinstance(sub, ast.Call) and not hasattr(sub, '_qg_owner'):
            sub._qg_owner = '<module>'


def _reachable_func_names(tree):
    """从"模块级真会执行的那批调用"出发，沿函数名调用关系求可达函数名闭包。

    为什么需要（走查第 18 轮 F2）：上一版只要"执行形调用的参数位出现文件名"就算接线，
    于是往门禁里塞一个**谁都不调用**的函数也能转绿：
        def _zz_dead_helper():            # 全文件没有任何地方调用它
            subprocess.run([PY, "check_zz.py"])
    实测 `check_zz.py` 进了 `called` 集合、第 19 项打印"走『门禁执行形调用』"——
    **写在死代码里的守卫不是守卫**，与本项目反复抓的"写了没接线"同一形态，只是躲过了上一道判据。
    保守性说明：本闭包只做**收窄**，真接线的脚本必然在 main 的调用链上，不会被误判成未接线。

    高阶包裹（第 48 轮补，动因是第 47 轮那次「逐项抗崩」包裹把本判据喂成了假红）：
    `main()` 里 48 个检查项从 `check_x()` 改成 `_guarded(check_x)` 之后，出边只剩
    `main → _guarded`，而 `_guarded` 体内调的是形参 `fn`（不在 `defs` 里）⇒ 闭包当场把
    所有被包裹的项判成"写在无人调用的函数里"，第 19 项一口气点名 10 个证据脚本。
    修法取「跟随**真会调用形参**的包裹器」：先求出 invoker 集合（体内存在
    `Call(func=Name(自己的参数名))` 或 `Call(func=Attribute(value=Name(自己的参数名)))`
    的函数），只有被调名落在该集合里时，才把实参位上的函数引用也算作一条出边。
    已知边界（刻意不跟随，宁可误红也不放宽）：① 回调先塞进容器再取出来调
    （`DISPATCH['k']()`、`self.hooks.pop()()`）不跟随；② 非 invoker 的普通调用
    （`lst.append(dead_fn)`）不跟随 —— 否则「把死函数名当数据传一下」就算接线，
    本判据就此失去收窄能力。两类各由 S56 的一条变异腿钉住。
    """
    defs = {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _own_params(fn):
        a = fn.args
        names = {p.arg for grp in (a.posonlyargs, a.args, a.kwonlyargs) for p in grp}
        if a.vararg is not None:
            names.add(a.vararg.arg)
        if a.kwarg is not None:
            names.add(a.kwarg.arg)
        return names

    def _invoker_names():
        out = set()
        for fn in defs.values():
            params = _own_params(fn)
            if not params:
                continue
            for sub in ast.walk(fn):
                if not isinstance(sub, ast.Call):
                    continue
                f = sub.func
                if isinstance(f, ast.Name) and f.id in params:
                    out.add(fn.name)
                    break
                if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                        and f.value.id in params):
                    out.add(fn.name)
                    break
        return out

    INVOKER = _invoker_names()

    def called_names(node):
        out = set()
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            callee = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if callee:
                out.add(callee)
            if callee in INVOKER:
                for arg in list(sub.args) + [kw.value for kw in sub.keywords]:
                    if isinstance(arg, ast.Name) and arg.id in defs:
                        out.add(arg.id)
        return out

    # 根：模块级语句（含 `if __name__ == "__main__":` 块）里直接出现的调用
    top_stmts = list(tree.body)
    for stmt in tree.body:
        if isinstance(stmt, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            for fld in (getattr(stmt, "body", []), getattr(stmt, "orelse", []),
                        getattr(stmt, "finalbody", []),
                        [h for h in getattr(stmt, "handlers", [])]):
                if isinstance(fld, list):
                    top_stmts.extend(fld)
    roots = set()
    for stmt in top_stmts:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            roots |= called_names(stmt.value)
    reach, frontier = set(), {r for r in roots if r in defs}
    while frontier:
        name = frontier.pop()
        if name in reach:
            continue
        reach.add(name)
        frontier |= {n for n in called_names(defs[name]) if n in defs and n not in reach}
    return reach


def _gate_called_strings(src_text):
    """取「出现在**可达的执行形**调用参数位置上的字符串常量」集合。

    执行形 = 被调用的函数名落在 `_EXEC_FUNCS`（`subprocess.run` / `os.system` /
    本仓自己的 `run_test` 等）。三层收紧一层比一层细，各有实测事故：
      · 第 13 轮 F4：不能算"任意调用参数"—— `os.path.join(ROOT, "check_x.py")` 是**读**不是**跑**；
      · 第 16 轮 F5：路径拼装踢掉后，仍留下"只拼路径 + isfile 看一眼"的口子，一并踢；
      · 第 18 轮 F2：执行形调用本身还必须**跑得到**——写在没人调用的函数里等于没写。
    文档字符串、赋值字面量、`record(...)` 的项名同样不算。
    解析失败时返回 None，由调用方回落旧口径并在明细里显式说明（不静默放宽）。
    """
    try:
        tree = ast.parse(src_text)
    except SyntaxError:
        return None
    reach = _reachable_func_names(tree)
    _annotate_call_owner(tree)
    found = set()
    unreachable = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else '')
        if fname not in _EXEC_FUNCS:
            continue
        own = getattr(node, '_qg_owner', '<module>')
        if own != '<module>' and own not in reach:
            # 记下来但不放行：死代码里的调用要在明细里显式点名，否则又变成一次静默放宽
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                        and sub.value.strip().endswith('.py'):
                    unreachable.add('%s（写在无人调用的 %s() 里）' % (sub.value.strip(), own))
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                found.add(sub.value.strip())
    globals()['_WIRE_UNREACHABLE'] = sorted(unreachable)
    return found


def _wf_run_lines(path):
    """CI 工作流里**真正会执行**的行（剥掉 YAML 注释行），用于判"被 CI 调用"。"""
    return chr(10).join(ln for ln in read(path).split(chr(10)) if not ln.lstrip().startswith('#'))


def check_evidence_scripts_wired():
    """扫描"能测量"的脚本，逐个确认它被门禁或 CI 调用，或登记了豁免理由。"""
    import glob

    here = os.path.abspath(__file__)
    # 直接把**完整源码**交给 AST：豁免表是赋值字面量、文档字符串是表达式语句，两者都不在
    # 任何 Call 的参数位置上，天然不会被判成"接线"。
    # （上一版为此专门写了个"从源码里切掉豁免表"的字符串手术，结果切完的源码语法不通，
    #  AST 每次解析都失败 ⇒ 静默回落到子串匹配，等于新判据一行都没生效。负对照实测到这一点：
    # **在"改写文本"的产物上做语法解析，本身就是一种自伤**。）
    called = _gate_called_strings(read(here))
    ast_note = ''
    if called is None:
        # 门禁自身语法不通时不静默降级：照旧用"代码行子串"判，但把降级事实打进明细
        called = set()
        ast_note = '⚠️ 门禁源码 AST 解析失败，接线判定回落到"剥注释后子串匹配"（更宽，可能漏判）'
        raw_match = chr(10).join(ln for ln in read(here).split(chr(10))
                                 if not ln.lstrip().startswith('#'))
    else:
        raw_match = ''
    wf_dirs = [os.path.join(ROOT, '.github', 'workflows')]
    ws = find_ws_root()
    if ws:
        # 仓库根那一份才是**真正在跑 Actions** 的配置；交付副本里的 .github 是给人读的镜像。
        # 只扫本副本时会出现"CI 明明调用了它，本地门禁却判未接线"的假红（走查第 13 轮 F4
        # 修完后当场暴露：check_mirror_freshness.py 此前靠注释里的提及被判成已接线）。
        _root_wf = os.path.join(ws, '.github', 'workflows')
        if _root_wf not in wf_dirs:
            wf_dirs.append(_root_wf)
    wf_src = ''.join(_wf_run_lines(f) for d in wf_dirs
                     for f in glob.glob(os.path.join(d, '*.yml')))
    candidates = []
    for pattern in ('check_*.py', '*eval*.py', '*benchmark*.py'):
        for base in (ROOT, os.path.join(ROOT, 'engineering-assurance')):
            candidates.extend(sorted(glob.glob(os.path.join(base, pattern))))
    unwired, unexplained, channels = [], [], []
    for path in dict.fromkeys(candidates):           # 去重且保序
        name = os.path.basename(path)
        if name == os.path.basename(here):
            continue
        via = []
        if name in called or (raw_match and name in raw_match):
            via.append('门禁执行形调用')
        if name in wf_src:
            via.append('CI 执行行')
        if via:
            channels.append(f'{name}={"+".join(via)}')
            continue
        reason = _EVIDENCE_EXEMPT.get(name, '')
        if reason and len(reason) >= 12:
            unexplained.append(f'{name}（已豁免：{reason[:40]}…）')
        else:
            unwired.append(name)
    hits = [f'未接线证据脚本：{n}（要么接进门禁/CI，要么在 _EVIDENCE_EXEMPT 写理由）'
            for n in unwired]
    if ast_note:
        hits.append(ast_note)
    # PASS 明细**逐候选点名走的是哪条通道**，并把豁免项单列（走查第 14 轮 F3：
    # 上一版统一写"全部出现在门禁调用参数或 CI 执行行里"，而 controlled_benchmark.py
    # 其实走的是第三条通道 `_EVIDENCE_EXEMPT` —— 一句过度断言把豁免机制藏掉了，
    # 读者据此以为"所有测量脚本都进门禁"，与事实相反）。
    detail = [f'扫描 {len(set(candidates))} 个测量类脚本：'
              f'接线 {len(channels)} / 登记豁免 {len(unexplained)} / 未接线 0']
    detail += ['  走『' + c + '』' for c in channels]
    detail += ['  『' + u + '』' for u in unexplained]
    # 死代码里的执行形调用：**不放行，但必须点名**（第 18 轮 F2）。
    # 只把它算进"未接线"是不够的——人会把消息复制去改一行 import 蒙过去；
    # 单独列一行是为了让"有人写了个不跑的电闸"这件事在日志里留痕。
    # ⚠️ 必须**红与绿两条路径都打**（走查第 19 轮 P2）：上一版只在 detail 里加，
    #    而 detail 只在 `not hits` 时被采用 ⇒ 一旦死调用正是"未接线"的原因，
    #    这条最有价值的诊断反而消失，只留下一句"要么接进门禁/CI，要么写理由"，
    #    把人往"那我加个 subprocess.run 吧"的方向推 —— 而那正是本判据要拦的动作。
    disclose = []
    if _WIRE_UNREACHABLE:
        disclose = ['⚠️ 写在无人调用的函数里、因此**不算**接线的调用：'
                    + '；'.join(_WIRE_UNREACHABLE)]
    record('BLOCK', '19. 证据脚本必须被接线（防"有评测能力、无评测守卫"）',
           not hits, (hits or detail) + disclose)


# ══════════════ 20. 门禁自身编号唯一性（BLOCK）══════════════
def _strip_comments_and_docstrings(raw):
    """剥掉**整行注释**与**文档字符串覆盖的行**，只留"代码行"。

    为什么两样都要剥（走查第 19 轮 F-P3）：第 20 项的静态枚举原先只剥 `#` 注释，
    于是**文档字符串**里举的反例（例如 `record('BLOCK', '15. …')` 这种教学用的假项名）
    会被当成一次真记录 ⇒ 凭空报出「编号重复：15」，或在 missed 里冒出幽灵号。
    注释与文档串都是**叙述面**不是调用面 —— 与第 19 项"文档串提及不算接线"同一条口径。
    解析失败时退回"只剥注释"继续工作：宁可不剥文档串，也不因为读不懂就整体放行。
    """
    _nl = chr(10)
    lines = raw.split(_nl)
    drop = set()
    for i, ln in enumerate(lines, 1):
        if ln.lstrip().startswith('#'):
            drop.add(i)
    try:
        tree = ast.parse(raw)
    except SyntaxError:
        return _nl.join(ln for k, ln in enumerate(lines, 1) if k not in drop)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = getattr(node, 'body', None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            for k in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                drop.add(k)
    return _nl.join(ln for k, ln in enumerate(lines, 1) if k not in drop)

def _label_head(label):
    """项名的**可比头**：截到第一个 `{` 前，并去掉尾随的括号/标点与空白。

    为什么需要（第 20 项跨维度判据上线当次实测）：第 7 项有两种写法并存 ——
    `record('WARN', '7. ruff 静态检查', …)`（脚本缺失分支）与
    `record('WARN', f'7. ruff 静态检查（{chr(99)+chr(109)+chr(100)[0]}）', …)`（正常分支把解释器路径打进项名）。
    不归一就是**同一编号"挂两个名称"**，自检会把自己误红；归一后跨维度比对才可比。
    代价（诚实声明）：真把两项命名成"X"与"X（另一种）"会被并成一个头而漏判撞号——
    所以本函数只用于**比对**，`record()` 里的原始项名照旧原样打印，报告可读性不受影响。
    """
    return re.sub(r'[（(\s：:，,]+$', '', label.split('{', 1)[0].strip())


def check_gate_numbering(fast=False):
    """门禁自检：编号必须**连续、唯一、且每条记录都带号**，任一不满足即红。

    为什么需要（第 11 轮实证）：并发会话在 `check_delivery_drift.py` 文件头自称
    「第 18 项候选」，而我本轮已把第 18 项＝查询预算、第 19 项＝证据脚本接线提交进门禁
    ⇒ 编号撞车。人写编号会撞、会跳、会在删除一项后留洞，**而编号是报告的引用锚**
    （台账/CI 日志/材料都按「第 N 项」指代），一旦重复，"第 18 项通过"就没有确定含义。

    两个判据维度，缺一不可（第 21 项上线时当场自证）：
      · 静态扫源码取「写了什么号」——只看字面串项名，且必须先剥注释，否则注释里举的
        假例子会被算成一个真项（本函数原版就举过这样一个假例子，差点自撞第 15 项）；
      · 运行期扫 results 取「实际记了什么号」。
    只留静态会漏这一类：项名用变量或 f-string 拼，静态正则抓不到 ⇒ 该项对自检**隐形**，
    撞号检不出。实测：第 21 项初版就是这么写的，门禁当场打印了第 21 项，
    而自检仍报「1..20」——正是本函数要抓的形态，靠自己抓到了自己。
    """
    raw = read(os.path.abspath(__file__))
    # 先剥整行注释：本函数文档里就举了 `record('BLOCK', '15. …')` 当例子，
    # 不剥的话注释里的假编号会被当成真项（本轮实测被抓：'15. …' 命中成"同编号挂两名"）。
    src = _strip_comments_and_docstrings(raw)
    # 编号有两个来源，只扫其一就会自己误红（本轮实测）：
    #   ① record('BLOCK', '15. …')  —— 静态检查类
    #   ② run_test('冒烟测试', 'smoke_test.py', '8.', …) —— 测试类（8/9/11/14）
    # 漏掉 ② 时 holes 会把这四项报成"编号空洞"，一个自己先误红的守卫，下一轮必被绕过。
    # 第 47 轮 M152 之后 ② 的实形是 `_guarded(run_test, '冒烟测试', …)`：正则同时吃
    # `run_test(` 与 `run_test, ` 两种，只认前者会让这五项从静态枚举里**整体消失**
    # （--fast 下 test_nums 是它们的合法豁免名单，名单空了就把正常跳过判成漏跑）。
    nums = [(m.group(1), m.group(2), m.group(3)) for m in re.finditer(
        r"record\('(BLOCK|WARN)',\s*f?'(\d+)\. ([^']*)'", src)]
    # 项名允许 f-string（第 7 项 `f'7. ruff 静态检查（{cmd[0]}）'` 把解释器路径写进项名）。
    # 静态侧只能看到 `{` 之前的**字面头**，运行期侧看到插值完成后的全名 ⇒
    # 跨维度比对一律**先取字面头再比**，否则第 7 项必被误报成"项名跨维度不一致"
    # （走查第 14 轮 F4 上线当次实测到的第一个假阳）。字面头**为空**（整个项名都是变量）的那种
    # 不在此列 —— 它由下面 `label_escape` 里的 `heads` 分支显式判红。
    # ⚠️ 这里原先还有一行 `static_dynamic = {...}` 自称"另走 dynamic 通道"，但**全文件没人用它**，
    #    而那条"通道"其实不存在（走查第 18 轮 F1 实测：纯变量项名会因 `startswith('')` 恒真而静默放行）。
    #    死变量 + 假注释比没写更坏，故删除，判据统一走 `heads` 一处。
    pairs = [(int(n), _label_head(lbl)) for _lvl, n, lbl in nums]
    static_levels = {}
    for lvl, n, _lbl in nums:
        static_levels.setdefault(int(n), set()).add(lvl)
    test_pairs = [(int(m.group(2)), m.group(1)) for m in re.finditer(
        r"run_test[,(]\s*'([^']*)',\s*'[^']*',\s*'(\d+)\.'", src)]
    test_nums = {n for n, _ in test_pairs}      # 经 run_test() 发出的号（--fast 会合法跳过它们）
    pairs += test_pairs
    # 同一编号常在多条出口各 record() 一次（脚本缺失分支 + 正常分支），
    # 按"出现次数"判重会把每个多项分支的检查都误报成撞号（本轮实测踩过）。
    # 真正的撞号是：**同一编号挂了两个不同的名称**。
    by_num = {}
    for n, label in pairs:
        by_num.setdefault(n, set()).add(label)
    dups = sorted(n for n, labels in by_num.items() if len(labels) > 1)
    holes = sorted(set(range(1, max(by_num) + 1)) - set(by_num)) if by_num else []
    hits = []
    if dups:
        hits.append('编号重复（并发会话撞号或误加一项）：' + ', '.join(str(n) for n in dups))
    if holes:
        hits.append('编号有空洞（删项后未收敛，引用会指到不存在的项）：'
                    + ', '.join(str(n) for n in holes))
    # ── 运行期维度：以「实际记录」为准（本函数必须在 main() 里最后调用才有意义）
    runtime = {}
    runtime_levels = {}
    anonymous = []
    for _lvl, name, _ok, _det in results:
        m = re.match(r'\s*(\d+)\.\s*(.+?)\s*$', name or '')
        if m:
            runtime.setdefault(int(m.group(1)), set()).add(m.group(2))
            runtime_levels.setdefault(int(m.group(1)), set()).add(_lvl)
        else:
            anonymous.append((name or '(空名)')[:40])
    if anonymous:
        hits.append('有 %d 条记录不带项号，对引用锚不可见：%s' % (len(anonymous), '、'.join(anonymous)))
    ghost = sorted(set(runtime) - set(by_num))
    if ghost:
        hits.append('运行期记到了、静态枚举看不见的项号（项名是变量拼的，自检会长期失明）：'
                    + ', '.join(str(n) for n in ghost))
    # 「写了号却没执行」两种模式下都要查：--fast 只合法豁免 run_test() 发的那几个号
    # （8/9/11/14），静态检查项在 fast 下同样必须执行，漏跑就是真漏跑。
    # 上一版用 `if not fast` 整块关掉，实测把 check_health_endpoint() 删调用后
    # --fast 依然全绿 ⇒ 豁免过宽会把自己最有牙的一条判据在常用模式下变成空判据。
    _self = re.search(r"record\('BLOCK', '(\d+)\. 门禁编号唯一性", src)
    self_no = int(_self.group(1)) if _self else None
    allow_absent = test_nums if fast else set()
    missed = sorted(set(by_num) - set(runtime) - allow_absent
                    - ({self_no} if self_no else set()))
    if missed:
        # 一条腿抛异常时"该项没记到结果"是**下游后果**，真因在第 56 项。两条红必须互相指认，
        # 否则下一轮有人来"修第 20 项"（第 46 轮自曝 ④：红是对的但理由不对，一样要返工）。
        _tail = ('（其中因抛异常而没跑完的项见第 56 项：'
                 + '、'.join(_crashed_names()) + '）') if CRASHED_CHECKS else ''
        hits.append('源码里写了项号、本次却没执行到（检查函数被删调用或被 if 旁路）：'
                    + ', '.join(str(n) for n in missed) + _tail)
    # 撞号必须**按实际记录再判一遍**：只看静态字面量的话，"同一编号的第二条出口用变量拼名"
    # 就完全检不出（走查第 12 轮实测：注入 record('BLOCK', '6. 变量拼的第二个名字') 之后
    # 静态 dups 仍为空 ⇒ 本函数自称能抓的那类撞号其实是漏的）。
    rt_dups = sorted(n for n, labels in runtime.items() if len(labels) > 1)
    if rt_dups:
        hits.append('实际记录里同一编号挂了两个名称（引用锚失效）：' + '；'.join(
            '%d → %s' % (n, ' / '.join(sorted(runtime[n]))) for n in rt_dups))
    # ── 跨维度对账（走查第 14 轮 F4）：上面每条判据都只在**单一维度内**比（静态⇄静态、
    # 运行期⇄运行期），于是留了两类逃逸：
    #   ① 同一项号在源码里叫 A、实际记出来叫 B —— 两个维度各自"唯一"，撞号却跨维度存在；
    #   ② 源码只写过 `record('BLOCK', …)`，运行期却以 WARN 出现（BLOCK 被静默降级成"看着红不拦"）。
    # 判据方向是**运行期 ⊆ 静态**（不是相等）：一个编号在源码里常有 BLOCK/WARN 两条出口、
    # 或多条不同措辞的出口，本次只走到其中一条是正常形态；反过来"跑出来的东西源码里根本没有"
    # 才是缺陷。实测反向（要求相等）会把第 6/10/12 项各误报一条。
    label_escape = []
    blind_names = []
    for n in sorted(set(runtime) & set(by_num)):
        # 项名的可比头为空 ⇒ 源码里那一项的名字**整个**是变量（`f'{no}. {narr}'`），
        # 静态侧没有任何字面可比。上一版在这种情况下仍然走 `lbl.startswith(head)`，
        # 而 `startswith('')` 恒真 ⇒ 这一维对纯变量项名**静默失效**，
        # 却照样打印"三维度一致"（走查第 18 轮 F1：注入 `record('BLOCK', f'22. {narr}')`
        # 后端上打印一个源码里根本不存在的名，第 20 项零告警）。
        # 现在改成：**不假装通过**，显式列进"静态不可核"清单，让 PASS 文案说实话。
        heads = {h for h in by_num[n] if h}
        if not heads:
            blind_names.append('%d → %s' % (n, ' / '.join(sorted(runtime[n]))))
            continue
        unknown = sorted(lbl for lbl in runtime[n]
                         if not any(lbl == head or lbl.startswith(head) for head in heads))
        if unknown:
            label_escape.append('%d → %s（源码里没有这个措辞）' % (n, ' / '.join(unknown)))
    if blind_names:
        hits.append('有 %d 项的项名在源码里完全是变量、静态无法核对，第 20 项对它只能查号与级别：%s'
                    '（要恢复可核：把字面量写回 f-string 前段）' % (len(blind_names), '；'.join(blind_names)))
    if label_escape:
        hits.append('实际项名与源码枚举项名跨维度不一致：' + '；'.join(label_escape))
    level_escape = []
    for n in sorted(set(runtime_levels) & set(static_levels)):
        demoted = sorted(runtime_levels[n] - static_levels[n])
        if demoted:
            level_escape.append('%d 运行期=%s 但源码只写过 %s' % (
                n, '/'.join(demoted), '/'.join(sorted(static_levels[n]))))
    if level_escape:
        hits.append('有项的检查级别在源码里没有出处（BLOCK 被静默降级成 WARN 就藏在这里）：'
                    + '；'.join(level_escape))
    # 同一项名允许多条出口各记一次；名称只比"编号 → 名称集合"，同上避免按次数误报
    record('BLOCK', '20. 门禁编号唯一性（静态枚举 ⇄ 实际记录 双向对账）', not hits,
           hits or [f'{len(by_num)} 个编号连续唯一：1..{max(by_num) if by_num else 0}，'
                    f'且实际记录 {len(runtime)} 项在**项号/项名/级别**三个维度上与源码一致'])


# ══════════════ 22. 门禁自身不得有死定义、重复定义（BLOCK）══════════════
# 来历（走查第 19 轮 P1，本循环至今最重的一条）：第 18 轮我给第 19 项加"可达性"判据，
# 收口说明里写着"用节点标注而不是 id() 建表"。但那次补丁**贴了两遍**，结果是：
#   · `_reachable_func_names()` 被定义两次（第一份被第二份静默遮蔽）；
#   · 新写的 `_annotate_call_owner()` 从没被调用，活路径仍在用旧的 `_call_owner()`
#     —— 也就是收口说明点名批评的那种写法；
#   · 另留着一个 V4.16 时代遗留、彻底没人调的 `_gate_src_without_exempt_table()`。
# **修「死代码守卫」的那个补丁自己带着三处死代码**，而当时全部 21 项检查没有一项能发现它。
# ⇒ 第 19 项管"测量脚本有没有被接线"；本项把同一条纪律掉转枪口指向门禁自己：
#   顶层定义必须真的被用到、不得重名。豁免必须写理由（≥12 字），与第 19 项同形态。
_SELF_DEAD_EXEMPT = {
    # 函数名 → 「为什么允许它待着不动」的必须写理由（空理由视为未豁免，判红）
}


def check_gate_item_isolation():
    """56. 逐项抗崩：一条腿崩只算**这一项**没测到，不得带走后面的项（第 47 轮 M152）。

    为什么单独立项而不是把 `main()` 改成 try 包一次就完：一次整链包裹（原版形态）抓不住
    "第 12 项崩了 ⇒ 第 13..55 项全没跑"这件事，因为它只在**最外层**红一条，
    而报告看上去仍是"门禁跑过了"。本项断言的是三件事，缺一不成立：
      ① 顶层每个 `check_*` 都经 `_guarded` 进入 `main()`（在册名字 ≠ 在跑机制：
         只要还剩一处裸调用，那个出口就仍然是"一条腿带走整套"）；
      ② 包裹器捕的是 `Exception`（换成 `except ValueError` 等于没裹），
         且**没有**捕 `BaseException`（捕了会把末尾的 `sys.exit` 吞成"没结论"）；
      ③ 本次运行零崩溃；有崩溃则逐条点名（函数名 + 异常 + traceback 末三行）。
    ①② 取的是**本文件源码的结构**，不取"我印象里改了几处"——第 46 轮自曝 ② 的教训：
    打在已 import 的模块对象上的行为核对"源码被改坏"完全隐形。
    项名一律写字面量、两次 record 各写一遍：首版把项名存进 `name` 变量复用，第 20 项的
    静态侧就枚举不到 56 号（实测报"运行期记到了、静态枚举看不见的项号：56"）——
    正是本文件第 1005 行注释里那条"纯变量项名会让自检长期失明"的第三次复发。
    """
    try:
        tree = ast.parse(read(os.path.abspath(__file__)))
    except SyntaxError as exc:
        record('BLOCK', '56. 逐项抗崩（顶层 check_* 全经包裹 ∧ 处理器捕 Exception ∧ 本次零崩溃）',
               False, [f'门禁源码解析不通，自查无法进行：{exc}'])
        return
    top = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name.startswith('check_')]
    names = {n.name for n in top}
    tmain = next((n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == 'main'), None)
    wrapped, bare = set(), set()
    if tmain is not None:
        for node in ast.walk(tmain):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id == '_guarded':
                for a in node.args:
                    if isinstance(a, ast.Name):
                        wrapped.add(a.id)
            elif node.func.id in names:
                bare.add(node.func.id)
    problems = []
    if tmain is None:
        problems.append('门禁源码里没有 main() ⇒ 无法判包裹面')
    else:
        unguarded = sorted(names - wrapped)
        if unguarded:
            problems.append(f'{len(unguarded)}/{len(names)} 个顶层检查项没经 _guarded 进入 main()'
                            '（一处裸调用=那条腿崩时后面的项全部隐身）：'
                            + '、'.join(unguarded[:8]) + ('…' if len(unguarded) > 8 else ''))
        if bare:
            problems.append('main() 里仍存在对 check_* 的**直接**调用（绕开包裹的第二个出口）：'
                            + '、'.join(sorted(bare)[:8]))
    gd = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == '_guarded'), None)
    if gd is None:
        problems.append('没有 _guarded ⇒ 逐项隔离这个机制根本不存在')
    else:
        handlers = [h for h in ast.walk(gd) if isinstance(h, ast.ExceptHandler)]
        broad = [h for h in handlers
                 if isinstance(h.type, ast.Name) and h.type.id == 'Exception']
        swallowing = [h for h in handlers
                      if (isinstance(h.type, ast.Name) and h.type.id == 'BaseException')
                      or (isinstance(h.type, ast.Tuple)
                          and any(getattr(e, 'id', '') == 'SystemExit' for e in h.type.elts))]
        if not broad:
            problems.append('_guarded 的处理器没捕 Exception ⇒ 窄捕获等于没裹（换个异常类型就漏）')
        if swallowing:
            problems.append('_guarded 捕了 BaseException/SystemExit ⇒ 会把门禁末尾的'
                            ' sys.exit 结论吞成"跑完了但没结论"（假绿，比崩更坏）')
    if CRASHED_CHECKS:
        problems.append(f'本次运行有 {len(CRASHED_CHECKS)} 个检查项抛出异常未完成：'
                        + '；'.join(f'{n} → {e}' for n, e, _t in CRASHED_CHECKS[:6]))
    details = ([f'{p}' for p in problems] or
               [f'顶层检查项 {len(names)} 个全部经 _guarded 进入 main()，裸调用 0 处',
                f'包裹器捕 Exception（{len(broad)} 个处理器），未捕 BaseException',
                f'本次运行崩溃 {len(CRASHED_CHECKS)} 项，results 累计 {len(results)} 条记录'])
    record('BLOCK', '56. 逐项抗崩（顶层 check_* 全经包裹 ∧ 处理器捕 Exception ∧ 本次零崩溃）',
           not problems, details)


def check_gate_self_dead_code():
    """门禁自查：顶层函数/类不得重复定义，也不得定义了却从没人用。"""
    path = os.path.abspath(__file__)
    raw = read(path)
    try:
        tree = ast.parse(raw)
    except SyntaxError as exc:
        record('BLOCK', '22. 门禁自身无死定义/重复定义（守卫自查）', False,
               ['门禁源码语法不通，自查无法进行：%s' % exc])
        return
    # 走查第 20 轮 P1：只看 tree.body 会漏掉**条件块里的定义**，于是
    #     if 1 + 1 == 2:
    #         def _zz(): ...
    #     else:
    #         def _zz(): ...
    # 这种重名遮蔽完全不红（评审在真实副本实测：注入后第 22 项照绿、计数不变）。
    # 所以往下钻 If/Try/With/For/While，但不进 FunctionDef/ClassDef 内部
    # （函数里的嵌套函数是另一回事，不该按顶层要求它被模块级调用）。
    def _module_scope_defs(node):
        out = []
        for child in getattr(node, 'body', []) or []:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.append(child)
            elif isinstance(child, (ast.If, ast.Try, ast.With, ast.AsyncWith,
                                    ast.For, ast.AsyncFor, ast.While)):
                out.extend(_module_scope_defs(child))
                for handler in getattr(child, 'handlers', []) or []:
                    out.extend(_module_scope_defs(handler))
                for fld in ('orelse', 'finalbody'):
                    blk = getattr(child, fld, None)
                    if blk:
                        out.extend(_module_scope_defs(
                            ast.Module(body=blk, type_ignores=[])))
        return out

    top = _module_scope_defs(tree)
    by_name = {}
    for n in top:
        by_name.setdefault(n.name, []).append(n.lineno)
    dups = {k: v for k, v in by_name.items() if len(v) > 1}
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            used.add(f.id if isinstance(f, ast.Name) else getattr(f, 'attr', ''))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            # 当一等函数被引用（传回调、放进列表、getattr 取）也算"用到了"
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    never = sorted(k for k in by_name
                   if k not in used and k not in _SELF_DEAD_EXEMPT
                   and not k.startswith('__'))
    unexplained = sorted(k for k, why in _SELF_DEAD_EXEMPT.items()
                         if not why or len(why) < 12)
    # 走查第 20 轮 P3：豁免一个**根本不存在**的名同样是判据空转 —— 删掉函数的人只要
    # 顺手加一行豁免就能永远压绿。所以孤儿豁免判红。
    orphan = sorted(k for k in _SELF_DEAD_EXEMPT if k not in by_name)
    hits = []
    if dups:
        hits.append('顶层定义重名（后一份会静默遮蔽前一份，读代码的人以为跑的是前者）：'
                    + '；'.join('%s @ 行%s' % (k, v) for k, v in sorted(dups.items())))
    if never:
        hits.append('定义了却从没人调用（要么接上、要么删掉、要么登记理由）：' + '、'.join(never))
    if unexplained:
        hits.append('豁免表里理由缺失或少于 12 字：' + '、'.join(unexplained))
    if orphan:
        hits.append('豁免表里登记了不存在的名字（对空气豁免＝判据空转）：' + '、'.join(orphan))
    record('BLOCK', '22. 门禁自身无死定义/重复定义（守卫自查）', not hits,
           hits or ['模块级定义 %d 个（含 if/try 块内）：无重名、无死定义（豁免 %d 个，须写理由且名字须存在）'
                    % (len(by_name), len(_SELF_DEAD_EXEMPT))])

# ══════════════ 21. 交付副本与仓库根一致（BLOCK）══════════════
# 第 10 项比的是 `src` 整树 + 根级白名单脚本，**`.github/` 完全不在它的面内**
# （`_tree_diffs` 只作用于 src，`ROOT_DELIVERED` 是枚举名单）。于是出现过一次真实事故：
# 交付副本 `05-源代码全文/.github/workflows/quality-gate.yml` 长期停在仓库根迁移前的旧版，
# 里面还写着「本项目当前尚未初始化 git 仓库」，而门禁一路全绿（对标第 5 轮实测）。
# `check_delivery_drift.py` 之前只挂在 CI 一个 step 上 ⇒ 本地跑门禁看不到这项，
# 改完 workflow 直接提交才会红。升成 BLOCK 后，本地与 CI 判据同源。
_WS_MIRROR_REL = os.path.join('交付物', '提交包', '工程佐证材料', '05-源代码全文')


def find_ws_root():
    """上溯找「同时含开发副本与交付副本」的工作区根；找不到返回 None。

    必须按**两个**目录共同判定：只判 `iCAN大学生创新创业大赛` 会在仓库根本身命中
    （根下没有交付副本时，漂移检查比的是不存在的镜像，恒绿＝假通过）。
    """
    d = ROOT
    for _ in range(8):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
        if (os.path.isdir(os.path.join(d, 'iCAN大学生创新创业大赛'))
                and os.path.isdir(os.path.join(d, _WS_MIRROR_REL))):
            return d
    return None


def check_delivery_drift():
    # 名称一律写成 record() 调用里的**字面串**：第 20 项编号自检的正则只认字面串，
    # 用变量传 label 会让本项对自检隐形（首次实跑即复现：输出停在「1..20」，
    # 而门禁当场打印了第 21 项 ⇒ 编号撞车时自检会漏报）。
    ws = find_ws_root()
    if ws is None:
        record('BLOCK', '21. 交付副本与仓库根一致（.github 镜像 + 名单外根级脚本三副本）', False,
               ['找不到工作区根（需同时含 iCAN大学生创新创业大赛/ 与 05-源代码全文/）'
                '——漂移检查**没有跑过**，不得当通过'])
        return
    script = os.path.join(ROOT, 'check_delivery_drift.py')
    if not os.path.isfile(script):
        record('BLOCK', '21. 交付副本与仓库根一致（.github 镜像 + 名单外根级脚本三副本）', False,
               ['未找到 check_delivery_drift.py（守卫本体丢失）'])
        return
    r = subprocess.run([PY, 'check_delivery_drift.py', '--root', ws], cwd=ROOT,
                       capture_output=True, text=True)
    out = (r.stdout or '') + (r.stderr or '')
    lines = out.splitlines()
    details = [ln.strip() for ln in lines if '[BLOCK]' in ln or '[WARN]' in ln][:12]
    details += [ln for ln in lines if ln.startswith('交付漂移检查')]
    if r.returncode != 0 and not details:
        details = [out[:400] or '漂移脚本无输出（可能导入异常）——不得当通过']
    record('BLOCK', '21. 交付副本与仓库根一致（.github 镜像 + 名单外根级脚本三副本）',
           r.returncode == 0, details or ['漂移脚本通过但无输出——不得当通过'])


# ══════════════ 23. 快照被引资源完整性（BLOCK）══════════════
def check_snapshot_assets():
    """跑 check_snapshot_assets.py：快照每页引用的 js/css/图片必须真在站点根内。

    动因是走查第 23 轮的真实事故：快照按需加载 `/static/js/echarts.custom.js`，
    镜像只推了 html ⇒ 线上该资源 404、图表静默空白，而当时门禁 19 项全绿。
    同族缺陷的共同根因是「只比产物之间一致，不比产物能不能跑」——本项补那条腿。
    离线腿在本项（仓库 ⇄ 两份交付快照目录）；线上腿由 `--online <站点根>` 按需调用，
    刻意不塞进门禁：门禁必须在无网络环境（CI 干净检出）下结论稳定。
    """
    script = os.path.join(ROOT, 'check_snapshot_assets.py')
    if not os.path.isfile(script):
        record('BLOCK', '23. 快照被引资源完整性（页面引用的资源必须真在站点根内）', False,
               ['未找到 check_snapshot_assets.py（资源完整性守卫丢失）——不得当通过'])
        return
    r = subprocess.run([PY, 'check_snapshot_assets.py'], cwd=ROOT,
                       capture_output=True, text=True)
    out = (r.stdout or '') + (r.stderr or '')
    lines = out.splitlines()
    # 子脚本自身用 ↳ 打头，record() 会再加一次 → 先剥掉避免「↳ ↳」双箭头
    details = [ln.strip().lstrip('↳').strip() for ln in lines if '↳' in ln][:12]
    details += [ln.strip() for ln in lines
                if 'ASSET-OK' in ln or '不完整' in ln or '没测到' in ln]
    if r.returncode != 0 and not details:
        details = [out[:400] or '资源守卫无输出（可能导入失败）——不得当通过']
    record('BLOCK', '23. 快照被引资源完整性（页面引用的资源必须真在站点根内）',
           r.returncode == 0, details or ['资源守卫通过但无输出——不得当通过'])


# ══════════════ 24. 迁移补丁四方对账（BLOCK）══════════════
def _s24_registry_section(text):
    """只取 UPGRADE.md 里「补丁注册表」那一节的正文（找不到返回 None）。

    收紧的理由是本轮实测抓出来的：原口径全文扫 `` `pNNN_x` ``，
    第 35 轮新加的「回滚」章把 7 条 pid 又点了一遍名 ⇒ S17 那条
    "注册表抽掉一行必须判红"的变异腿**当场变绿**（文档另一处的顺带提及把缺陷喂饱了）。
    不变式是「注册表表格里逐条列了这个补丁」，别处提到不算数。
    """
    m = re.search(r'^##\s*补丁注册表[^\n]*\n', text or '', re.M)
    if not m:
        return None
    rest = text[m.end():]
    nxt = re.search(r'^##\s', rest, re.M)
    return rest if nxt is None else rest[:nxt.start()]


def check_migration_ledger():
    """补丁「声明 ⇄ 执行体绑定 ⇄ UPGRADE.md 名单 ⇄ 账本表声明」四方必须同源。

    动因（2026-09-25 第 17 轮对标）：7 个同类仓 7/7 有迁移账本，我方 0；
    而补一个账本最容易造的第二个缺陷是"注册表与文档各写一份"——本项目已有
    三处同族前科（`_ENSURE_COLUMNS` 清单自检、openapi 手写规范、门禁编号对账）。
    所以本项不查"账本有没有"（那由 check_schema_drift 的实库腿查），
    只查**四个名字列表是否还是同一份**：漏一处即红。
    判据面自身也带防空转：解析不到 PatchRecord / 找不到绑定表 一律算 FAIL。
    """
    problems = []
    led_src = read(os.path.join(SRC, 'schema_ledger.py'))
    init_src = read(os.path.join(ROOT, 'init_db.py'))
    declared = re.findall(r"PatchRecord\(\s*'([^']+)'", led_src)
    if not led_src or not declared:
        problems.append('src/schema_ledger.py 缺失或解析不到任何 PatchRecord'
                        '（判据面无输入，不得记通过）')
    dup = sorted({p for p in declared if declared.count(p) > 1})
    if dup:
        problems.append(f'注册表 pid 重复声明：{dup}')
    bad = [p for p in declared if not re.fullmatch(r'p\d{3}_[a-z0-9_]+', p)]
    if bad:
        problems.append(f'pid 命名不合 pNNN_snake_case（改名会制造孤儿账本）：{bad}')
    bound_blk = re.search(r'_PATCH_IMPLS\s*=\s*\{(.*?)\n\}', init_src, re.S)
    if not bound_blk:
        problems.append('init_db.py 里找不到 _PATCH_IMPLS 绑定表——补丁等于没接线')
    else:
        bound = re.findall(r"['\"](p\d{3}_[a-z0-9_]+)['\"]\s*:", bound_blk.group(1))
        problems += [f'声明了却没有执行体（永不执行）：{p}' for p in sorted(set(declared) - set(bound))]
        problems += [f'有执行体却没进声明（永不执行）：{p}' for p in sorted(set(bound) - set(declared))]
    up_path = os.path.join(ROOT, 'UPGRADE.md')
    if not os.path.isfile(up_path):
        problems.append('UPGRADE.md 缺失：存量库升级只能靠口口相传')
    else:
        seg = _s24_registry_section(read(up_path))
        if seg is None:
            problems.append('UPGRADE.md 找不到「补丁注册表」这一节 ⇒ 名单无处对账（判"没测到"，不判过）')
        else:
            named = set(re.findall(r'`(p\d{3}_[a-z0-9_]+)`', seg))
            problems += [f'UPGRADE.md 注册表漏写补丁：{p}' for p in sorted(set(declared) - named)]
            problems += [f'UPGRADE.md 注册表写了不存在的补丁：{p}' for p in sorted(named - set(declared))]
    if 'schema_patch_log' not in read(os.path.join(SRC, 'models.py')):
        problems.append('models.py 未声明 schema_patch_log 账本表（create_all 建不出账本）')
    if 'record_all' not in read(os.path.join(SRC, 'pack_demo.py')):
        problems.append('播种分支未整册补记 → CI 冷启动会把刚建好的干净库判成"没升级"')
    record('BLOCK', '24. 迁移补丁四方对账（声明 ⇄ 实现 ⇄ UPGRADE 文档 ⇄ 账本表）', not problems,
           problems or [f'{len(declared)} 条补丁四方同源，账本表与整册补记均已接线'])


# ══════════════ 25. 版本契约一致性（BLOCK）══════════════
def check_version_contract():
    """应用版本号必须有一个代码内真相源，且运行时可见。

    动因（第 17 轮 H28 取证）：改本仓之前**全仓没有任何运行时可见的版本号**——
    `v1.6.0` 只活在 git tag、GitHub Release 与 README 文字里，仅凭 HTTP 回答不了
    "这台跑的是哪一版"。对标 Appwrite（`APP_VERSION_STABLE` + `/v1/health/version`）
    与 Chatwoot（根文件 `VERSION_CW`）。
    本项刻意只查**仓内可判**的四条：真相源存在且唯一、格式合法、UPGRADE.md 同版本、
    `/healthz` 暴露 version 与 schema。git tag ⇄ APP_VERSION 不在本项查——
    CI 是 depth=1 浅克隆，`git describe` 在那里会失败，把 git 派生值当判据
    会造出"本地绿、CI 红"（那条腿归第 16 轮立的本地验证入口，只在本地跑）。
    """
    problems = []
    ver_src = read(os.path.join(SRC, 'version.py'))
    m = re.search(r"^APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]", ver_src, re.M)
    if not m:
        problems.append('src/version.py 缺 APP_VERSION 字面量（版本契约没有真相源）')
        record('BLOCK', '25. 版本契约（版本单一真相源 + 运行时可见）', False, problems)
        return
    ver = m.group(1)
    if not re.fullmatch(r'\d+\.\d+\.\d+', ver):
        problems.append(f'APP_VERSION 不合 x.y.z：{ver}')
    second = [f for f in sorted(os.listdir(SRC))
              if f.endswith('.py') and f != 'version.py'
              and re.search(r'^APP_VERSION\s*=', read(os.path.join(SRC, f)), re.M)]
    if second:
        problems.append(f'版本字面量出现第二处赋值（改一处忘一处）：{second}')
    up_path = os.path.join(ROOT, 'UPGRADE.md')
    if not os.path.isfile(up_path) or f'v{ver}' not in read(up_path):
        problems.append(f'UPGRADE.md 未出现当前版本 v{ver}（文档版本与代码脱钩）')
    rd_path = os.path.join(ROOT, 'README.md')
    if not os.path.isfile(rd_path) or f'v{ver}' not in read(rd_path):
        problems.append(f'README.md 未出现当前版本 v{ver}（版本契约的 README 腿缺失——'
                        '本条判据第 30 轮才补上：第 17 轮的 docstring 早就这么承诺了，'
                        '但代码里一直没查，属于"文档写了、判据没写"的假契约）')
    app_src = read(os.path.join(SRC, 'app.py'))
    for frag in ("'version'", "'schema'", 'schema_ledger'):
        if frag not in app_src:
            problems.append(f'/healthz 未暴露 {frag}（线上无法判断版本与迁移进度）')
    record('BLOCK', '25. 版本契约（版本单一真相源 + 运行时可见）', not problems,
           problems or [f'v{ver} 单源、UPGRADE 同版、/healthz 已暴露 version+schema'])



# ══════════════ 26. CI 供应链固定（BLOCK）══════════════
_LOCAL_USES = ('./',)
_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
_USES_RE = re.compile(r'^\s*(?:-\s*)?uses:\s*(\S+)')
_PIP_RE = re.compile(r'pip install (.+)')


def check_supply_chain_pins():
    """workflow 里每个第三方 action 必须钉 40 位 commit SHA，且每个文件有显式 permissions。

    动因（2026-09-25 第 18 轮对标）：同类 7 仓实测 5 家 **100% 钉 SHA**
    （sentry 153/153、airflow 245/245、zulip 35/35、inventree 96/96、appwrite 110/110），
    我方改前 **0/9 全用可变标签**。上游把 `@v4` 标签挪一下，我们所有门禁与部署
    **执行的内容**就跟着变，而"门禁全绿"是这条链上唯一的裁判——裁判自己踩在别人的 tag 上。
    顺带两条同源：CI 里 `pip install ruff` 不钉版（工具漂=尺子漂）、workflow 不写
    `permissions:` 就走 GITHUB_TOKEN 默认权限（部署线还持 PA Token）。
    """
    wf_dir = os.path.join(ROOT, '.github', 'workflows')
    if not os.path.isdir(wf_dir):
        record('BLOCK', '26. CI 供应链固定（action 钉 SHA + 最小 permissions + 工具钉版）', False,
               [f'未找到 {wf_dir}（判据面无输入，不得记通过；权威副本应含 .github 镜像）'])
        return
    files = sorted(f for f in os.listdir(wf_dir) if f.endswith(('.yml', '.yaml')))
    if not files:
        record('BLOCK', '26. CI 供应链固定（action 钉 SHA + 最小 permissions + 工具钉版）', False,
               [f'{wf_dir} 下没有任何 workflow 文件（CI 不存在却报通过=假绿）'])
        return
    problems, n_uses = [], 0
    for fn in files:
        text = read(os.path.join(wf_dir, fn))
        if not re.search(r'^permissions:', text, re.M):
            problems.append(f'{fn}: 无顶层 permissions（走 GITHUB_TOKEN 默认权限）')
        for ln in text.splitlines():
            m = _USES_RE.match(ln)
            if not m:
                continue
            ref = m.group(1)
            if ref.startswith(_LOCAL_USES):
                continue
            n_uses += 1
            tail = ref.rsplit('@', 1)
            if len(tail) != 2 or not _SHA_RE.match(tail[1]):
                problems.append(f'{fn}: `uses: {ref}` 未钉 40 位 SHA（可变 tag/branch）')
        for m in _PIP_RE.finditer(text):
            arg = m.group(1).strip()
            if arg.startswith('-r ') or '-r ' in arg:
                continue
            if arg == '--upgrade pip':
                # 唯一豁免：引导 pip 自身（不装任何第三方包）。除此之外一律要钉版。
                continue
            if '==' not in arg:
                problems.append(f'{fn}: `pip install {arg}` 未钉版本（工具漂移=尺子漂移）')
    record('BLOCK', '26. CI 供应链固定（action 钉 SHA + 最小 permissions + 工具钉版）',
           not problems,
           problems or [f'{len(files)} 个 workflow / {n_uses} 条第三方 uses 全部钉 SHA，'
                        f'permissions 与 pip 钉版齐备'])


# ══════════════ 27. 第三方 vendored 资产台账（BLOCK）══════════════
def check_vendor_assets():
    """vendored 第三方产物必须有版本/来源/哈希台账，且台账与实盘逐条对得上。

    动因（第 18 轮）：619 KB 的 `echarts.custom.js` 入仓，**无版本、无哈希、无 lock**，
    其构建目录只有两个 .mjs；更糟的是第 5 项盲审的第三方排除过滤器还写着旧名
    `echarts.min.js`（改名后成死码），所以"它被逐行扫"这件事也没人知道。
    本项同时堵三件事：哈希漂移、台账指向不存在的文件、排除名单里的名字在 src 下不存在。
    """
    path = os.path.join(ROOT, 'VENDOR-ASSETS.json')
    if not os.path.isfile(path):
        record('BLOCK', '27. 第三方 vendored 资产台账（版本/来源/SHA256 与实盘一致）', False,
               ['未找到 VENDOR-ASSETS.json —— vendored 第三方产物失去来源与完整性记录'])
        return
    try:
        data = json.loads(read(path))
    except (ValueError, OSError) as exc:
        record('BLOCK', '27. 第三方 vendored 资产台账（版本/来源/SHA256 与实盘一致）', False,
               [f'台账 JSON 解析失败：{type(exc).__name__}: {exc}'])
        return
    items = data.get('assets') or []
    problems, need = [], ('path', 'size_bytes', 'sha256', 'upstream', 'version',
                          'upstream_license', 'how_built')
    for e in items:
        miss = [k for k in need if not str(e.get(k) or '').strip()]
        if miss:
            problems.append(f"{e.get('path') or '?'}: 台账缺字段 {miss}")
            continue
        p = os.path.join(ROOT, e['path'].replace('/', os.sep))
        if not os.path.isfile(p):
            problems.append(f"台账指向不存在的文件：{e['path']}")
            continue
        if os.path.getsize(p) != e['size_bytes']:
            problems.append(f"{e['path']}: 大小变了（台账 {e['size_bytes']} vs 实盘 {os.path.getsize(p)}）")
        got = hashlib.sha256(open(p, 'rb').read()).hexdigest()
        if got != e['sha256']:
            problems.append(f"{e['path']}: SHA256 与台账不一致（改了产物没更台账=来源失控）")
        base = os.path.basename(e['path'])
        if base not in {os.path.basename(x) for x in iter_files(SRC, {'.js'})}:
            problems.append(f"{base}: 台账里有、src 下没有（第 5 项排除过滤器会变死码）")
    orphans = [n for n in BLIND_EXCLUDES
               if n not in {os.path.basename(e['path']) for e in items if e.get('path')}]
    problems += [f'排除名单指向不存在的第三方台账项：{n}' for n in orphans]
    record('BLOCK', '27. 第三方 vendored 资产台账（版本/来源/SHA256 与实盘一致）', not problems,
           problems or [f'{len(items)} 项 vendored 资产逐条哈希核对通过'])


# ══════════════ 28. 依赖安全规则集（BLOCK）══════════════
_NOQA_S_RE = re.compile(r'#\s*noqa:\s*S\d+')


def check_security_lint():
    """ruff 安全规则集（S / flake8-bandit）必须 0 违例；每条豁免必须带 ≥12 字理由。

    动因（第 18 轮）：改前 `select` 里没有 `S` ⇒ `ruff check src --select S` 实测 9 条违例
    （S110×4 静默吞异常、S104×2 绑 0.0.0.0、S105×2 名字像口令、S311×1 伪随机）**今日全部隐形**。
    靶群只有 zulip 开了 `S`，airflow 用 bandit `--severity-level high` —— 说明"能开"而非"该开就行"，
    关键是**开了之后不许用空 noqa 把判据关掉**，所以豁免本身也被本项判。
    刻意不动第 7 项（风格类 WARN）：把 22 条风格债和本项混在一起只会逼人为凑绿改数据。
    """
    prefix = find_ruff()
    if not prefix:
        record('BLOCK', '28. 依赖安全规则集（ruff S 零违例 + 豁免带理由）', False,
               ['未探测到 ruff 可执行文件 —— 安全静态面失明不得记通过（与第 7 项的 WARN 分级不同：'
                '本项是安全面，工具缺席=盲区）'])
        return
    r = subprocess.run(prefix + ['check', 'src', '--select', 'S', '--output-format', 'concise'],
                       cwd=ROOT, capture_output=True, text=True)
    out = ((r.stdout or '') + (r.stderr or '')).strip()
    # concise 格式是 `src\app.py:1:12: S105 说明文字`——代码后是**空格**不是冒号。
    # 这里曾写成 `: S\d{3}:`（要求尾冒号），于是违例恒计 0、本判据变成永远通过的假判据；
    # 是 L4 的 S18 注入变异体（写一条真 S105 进去）把它抓出来的。
    viol = [ln for ln in out.splitlines() if re.search(r':\s+S\d{3}\s', ln)]
    problems = [f'安全规则违例 {v}' for v in viol[:12]]
    bare = []
    for p in iter_files(SRC, {'.py'}):
        for i, ln in enumerate(read(p).splitlines(), 1):
            if _NOQA_S_RE.search(ln):
                tail = ln.split('noqa', 1)[1]
                if '理由' not in tail or len(tail.split('理由')[-1].strip(' ：:')) < 12:
                    bare.append(f'{rel(p)}:{i}')
    if bare:
        problems.append(f'豁免未写 ≥12 字理由（用 noqa 关判据却不交代为什么）：{bare[:6]}')
    record('BLOCK', '28. 依赖安全规则集（ruff S 零违例 + 豁免带理由）', not problems,
           problems or [f'S 规则零违例；{sum(1 for p in iter_files(SRC, {".py"}) for ln in read(p).splitlines() if _NOQA_S_RE.search(ln))} 条豁免全部带理由'])


# ══════════════ 29. 发布制品校验和台账（BLOCK）══════════════
def check_release_ledger():
    """对外发布必须留一份可复算的校验和台账，且与当前权威副本逐文件对得上。

    动因（第 18 轮）：AGENTS/07/提交清单三处写着"唯一提交包 SHA256 台账"，实测
    `find -iname "*sha256*" -o "*台账*" -o "*checksum*"` 全仓 **0 命中** —— 声称先于实体；
    唯一 Release v1.6.0 的 `assets: []`。对照 Airflow（release 附 .sha512+.asc）、
    InvenTree（SPDX + attest-build-provenance）。生成器 = `release_evidence.py`（根级交付件，
    **必须在权威副本里跑** —— 台账落点按 `ROOT/../09-发布校验` 解析，副本与开发副本不是同一格）。
    双向校验：台账里的每条哈希必须与实盘一致（防改了文件没更台账），
    实盘每个在范围内的文件也必须出现在台账里（防台账漏项冒充完整）。
    """
    ver = ''
    vp = os.path.join(SRC, 'version.py')
    if os.path.isfile(vp):
        m = re.search(r"^APP_VERSION\s*=\s*['\"]([^'\"]+)", read(vp), re.M)
        ver = m.group(1) if m else ''
    led_dir = os.path.normpath(os.path.join(ROOT, '..', '09-发布校验'))
    led = os.path.join(led_dir, f'SHA256SUMS-v{ver}.txt') if ver else ''
    if not ver or not os.path.isfile(led):
        record('BLOCK', '29. 发布制品校验和台账（可复算且与副本逐文件一致）', False,
               [f'台账缺失：{led or "（APP_VERSION 读不到，无法定位台账）"}'
                ' → 在权威副本里跑 python release_evidence.py'])
        return
    lines = [ln for ln in read(led).splitlines() if ln.strip()]
    listed, agg = {}, ''
    for ln in lines:
        if ln.startswith('# AGGREGATE'):
            agg = ln.split()[-1]
        elif len(ln.split()) == 2 and re.match(r'^[0-9a-f]{64}$', ln.split()[0]):
            listed[ln.split()[1]] = ln.split()[0]
    problems = []
    if f'v{ver}' not in read(led):
        problems.append(f'台账头部未标版本 v{ver}（与 APP_VERSION 脱钩）')
    actual = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in LEDGER_SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(LEDGER_SKIP_EXTS) or fn in LEDGER_SKIP_NAMES:
                continue
            p = os.path.join(dirpath, fn)
            relp = os.path.relpath(p, ROOT).replace(os.sep, '/')
            actual[relp] = hashlib.sha256(open(p, 'rb').read()).hexdigest()
    for k, v in sorted(actual.items()):
        if listed.get(k) != v:
            problems.append(f'{k}: 台账哈希与实盘不符/缺条目')
    for k in sorted(set(listed) - set(actual)):
        problems.append(f'台账含已不存在的条目：{k}')
    expect_agg = hashlib.sha256(
        ''.join(f'{k} {actual[k]}\n' for k in sorted(actual)).encode('utf-8')).hexdigest()
    if agg and agg != expect_agg:
        problems.append('AGGREGATE 总哈希与逐条重算不一致（台账被局部改过）')
    record('BLOCK', '29. 发布制品校验和台账（可复算且与副本逐文件一致）', not problems,
           problems or [f'{len(actual)} 个文件逐条核对通过；总哈希 {expect_agg[:12]}…'])


# ── 第 57 项的豁免名册：版本 → 「为什么这一侧缺件是既有事实，而不是漏做」──────────
# 判据取向（第 48 轮，对标 odoo/InvenTree/getodk 的发布面一致性后立）：
#   · 理由必须**可复算**（≥12 字，且带提交短码这类可指认锚点）；空理由视为未豁免；
#   · **当前 APP_VERSION 一律不得出现在这里** —— 否则下次漏打 tag 会被历史名单顺手放行
#     （与第 19 项"对空气豁免＝判据空转"同族）；
#   · 不追补 tag/台账：给历史版本造 tag = 往对外发布面写一个错误的锚点，比漂移更坏。
RELEASE_IDENTITY_EXEMPT = {
    'ledger_only': {
        'v1.6.2': 'tag 纪律起点是 v1.12.0；本件由第 27 轮 a77a7a3 补生成，内容对应补做当轮的工作树而非 v1.6.2 自身提交，追补 tag 会造出错误对外锚点',
        'v1.7.0': '同上（tag 纪律起点 v1.12.0 之前无 set_version/tag 步骤）：台账由第 28 轮 ce32f55 补生成，不追补 tag',
        'v1.8.0': '同上：台账由第 29 轮 a77eb15 补生成，其字节对应第 29 轮工作树而非 v1.8.0 自身提交',
        'v1.9.0': '同上：台账由第 30 轮 27fd06f 补生成，其字节对应第 30 轮工作树而非 v1.9.0 自身提交',
        'v1.10.0': '同上：台账由第 31 轮 489c746 补生成，其字节对应第 31 轮工作树而非 v1.10.0 自身提交',
        'v1.11.0': '同上：台账由第 32 轮 e214624 补生成，其字节对应第 32 轮工作树而非 v1.11.0 自身提交；自 v1.12.0 起两侧同批生成',
    },
    'tag_only': {
        'v1.6.0': '台账生成器 release_evidence.py 于第 18 轮 490dcf5 才入库，而 v1.6.0 的 annotated tag 指向 9b7b471（第 15 轮）早于该提交 ⇒ 仓内不可能有它的台账，其发布面是 GitHub Release',
    },
}
SEMVER_TAG_RE = re.compile(r'^v\d+\.\d+\.\d+$')


def _release_identity_reconcile(ledger_versions, tag_versions, odd_names, exempt, current_v):
    """纯函数：台账版本集 ⇄ tag 版本集的双向差集 + 名册核检。

    返回 `(problems, readings)`。拆成纯函数是为了让 L4 能在内存里把两侧都喂反例
    （只测"该红的红了"会造出永久假绿，本项目已为此立过规）：
      差集方向、死豁免、短理由、当前版本入册 四类各要正反两侧。
    """
    ledgers = set(ledger_versions)
    tags = set(tag_versions)
    led_only = sorted(ledgers - tags)
    tag_only = sorted(tags - ledgers)
    problems = []
    for name in sorted(odd_names):
        problems.append(f'台账目录里有非 `SHA256SUMS-vX.Y.Z.txt` 形态的文件名：{name}'
                        '（版本集取不出来＝该件对三层对账不可见）')
    for face, diff in (('ledger_only', led_only), ('tag_only', tag_only)):
        ex = exempt.get(face, {})
        for v in diff:
            if v not in ex:
                problems.append(f'{face} 侧缺件未登记：{v}'
                                '（要么补 tag/台账，要么写明为什么不追补、进名册）')
        for name, reason in sorted(ex.items()):
            if name not in diff:
                problems.append(f'死豁免：{face} 名单里的 {name} 其实不在本次差集里'
                                '（对空气豁免＝判据空转，与第 19 项同族）')
            elif len(str(reason).strip()) < 12:
                problems.append(f'{face} 名单 {name} 的理由不足 12 字（空理由视为未豁免）')
    if current_v:
        for face, ex in exempt.items():
            if current_v in ex:
                problems.append(f'当前版本 {current_v} 出现在 {face} 豁免名单 ⇒ '
                                '名册不得吞掉本轮（本轮漏打 tag/漏出台账必须红）')
    readings = {'ledger': len(ledgers), 'tag': len(tags), 'both': len(ledgers & tags),
                'ledger_only': led_only, 'tag_only': tag_only,
                'exempted': len(led_only) + len(tag_only)}
    return problems, readings


def check_release_identity():
    """57. 发布身份三层对账 —— 仓内校验和台账的版本集合 ⇄ git tag 的版本集合（BLOCK）。

    动因（第 48 轮对标当日实测）：台账 23 个版本、tag 17 个，差集里
    `v1.6.2…v1.11.0` **有台账无 tag**、`v1.6.0` **有 tag 无台账**。对标群
    （getodk/InvenTree/frappe，180 天 10/20/88 次 release）三处发布身份一律一一对应，
    这是外部一眼就能验的硬伤，而当时**没有任何一条判据看这件事**。

    与既有两条的分工（同一事实只许一处判，故这里刻意不重复）：
      · 第 29 项判「当前版本的台账存在且逐文件对得上实盘」；
      · L4 S46 判「最新 tag == APP_VERSION」；
      · 本项判「**版本集合**双向对账」——两侧都要量，只补一侧不算修完。
    取数面不具条件时一律判红并给出处置（与第 29 项"台账缺失→跑 release_evidence.py"同口径），
    **不得**把"读不到"记成"没漂移"。
    """
    led_dir = os.path.normpath(os.path.join(ROOT, '..', '09-发布校验'))
    # 项名**每条出口都写字面量**，不存进变量复用：第 20 项的静态侧只认
    # `record('BLOCK', '57. …')` 这种字面串，用变量会让 57 号对自检隐形
    # （第 47 轮为 56 号踩过同一条，注释就在本文件里 —— 本轮首版又复现一次）。
    if not os.path.isdir(led_dir):
        record('BLOCK', '57. 发布身份三层对账（台账版本集 ⇄ git tag 双向差集 ∧ 豁免须有可指认理由 ∧ 当前版本禁入名册）', False,
               [f'取不到台账目录：{led_dir} ⇒ 判"没测到"而不是"没漂移"；'
                '门禁须在权威 `05-源代码全文/` 面跑（与第 29 项同前提）'])
        return
    ledgers, odd = [], []
    for fn in sorted(os.listdir(led_dir)):
        m = re.match(r'^SHA256SUMS-(v\d+\.\d+\.\d+)\.txt$', fn)
        if m:
            ledgers.append(m.group(1))
        elif not fn.startswith('.'):
            odd.append(fn)
    if not ledgers:
        record('BLOCK', '57. 发布身份三层对账（台账版本集 ⇄ git tag 双向差集 ∧ 豁免须有可指认理由 ∧ 当前版本禁入名册）', False,
               [f'台账目录在但一份合法台账都没有（目录内 {len(odd)} 个异常名）⇒ 零输入不得记通过'])
        return
    top = _git_toplevel()
    if not top:
        record('BLOCK', '57. 发布身份三层对账（台账版本集 ⇄ git tag 双向差集 ∧ 豁免须有可指认理由 ∧ 当前版本禁入名册）', False,
               ['取不到 git 根 ⇒ tag 面无法读取，判"没测到"（不判"没漂移"）；'
                '解压副本/无 git 环境属此态，请在有 git 的权威面跑'])
        return
    r = subprocess.run(['git', 'tag', '-l', 'v*'], cwd=top,
                       capture_output=True, text=True)
    if r.returncode != 0:
        record('BLOCK', '57. 发布身份三层对账（台账版本集 ⇄ git tag 双向差集 ∧ 豁免须有可指认理由 ∧ 当前版本禁入名册）', False,
               [f'`git tag -l v*` 取数失败 rc={r.returncode}：{r.stderr.strip()[:140]}'])
        return
    tags = [t for t in r.stdout.split() if SEMVER_TAG_RE.match(t)]
    if not tags:
        record('BLOCK', '57. 发布身份三层对账（台账版本集 ⇄ git tag 双向差集 ∧ 豁免须有可指认理由 ∧ 当前版本禁入名册）', False,
               ['`git tag` 返回 0 个 semver tag ⇒ 浅克隆/未取 tag 的检出（Actions 默认 checkout '
                'depth=1 即此态）：tag 面无数据，不作对账也不记通过；处置=fetch --tags'])
        return
    ver = ''
    vp = os.path.join(SRC, 'version.py')
    if os.path.isfile(vp):
        m = re.search(r"^APP_VERSION\s*=\s*['\"]([^'\"]+)", read(vp), re.M)
        ver = f'v{m.group(1)}' if m else ''
    problems, rd = _release_identity_reconcile(ledgers, tags, odd,
                                               RELEASE_IDENTITY_EXEMPT, ver)
    record('BLOCK', '57. 发布身份三层对账（台账版本集 ⇄ git tag 双向差集 ∧ 豁免须有可指认理由 ∧ 当前版本禁入名册）',
           not problems,
           problems or
           [f'台账 {rd["ledger"]} 个 / tag {rd["tag"]} 个 / 两侧都在 {rd["both"]} 个 / '
            f'已豁免 {rd["exempted"]} 个（台账独有 {len(rd["ledger_only"])}、tag 独有 {len(rd["tag_only"])}）',
            f'当前版本 {ver or "（读不到）"} 两侧齐全：台账={"有" if ver in ledgers else "无"} / '
            f'tag={"有" if ver in tags else "无"}'])


OFFLINE_SCOPE_REQUIRED = {
    'staff': 'data-scope-staff',
    'store': 'data-scope-store',
    'pack': 'data-scope-pack',
    'ver': 'data-scope-packver',
}


def _offline_scope_wiring(submit_js, scope_js, template_html, route_py):
    """离线队列「条目归属」三方对账的纯函数：模板注入 ⇄ 归属库 ⇄ 重放路径。

    返回 `(problems, readings)`。写成纯函数（而不是只在真面上读一把）是为了让 L4
    能拿**变异后的字节**驱动它：把守卫删掉、把脚本顺序颠倒，都必须让它变红，
    否则"我接线了"只是我的一句宣称。

    动因（第 49 轮探针实测）：同店两店员、当日重放一条甲未提交过的产品，
    服务端 `提交成功=True` 且乙名下流水 0→1 ⇒ 隔夜那半早被 `queued_for` 拦住，
    真漏的是**同日换账号**，而归属键只按门店判对它零判定力 —— 所以 staff 位必须在键里。
    """
    problems = []
    readings = {}
    if not submit_js or not scope_js or not template_html or not route_py:
        problems.append('四面之一读不到（JS/归属库/模板/路由）⇒ 判"没测到"，不判"没接线"')
        return problems, readings

    # ① 归属库必须是零 DOM 的纯函数面，否则 node 驱动不了、判据只能靠读文本
    for fn in ('scopeKey', 'scopeOfElement', 'itemMatchesScope', 'fieldsDigest'):
        if f'function {fn}(' not in scope_js:
            problems.append(f'归属库缺导出函数 {fn}()（L4 的 node 腿会打不到）')
    if 'document.' in scope_js:
        problems.append('归属库里出现 document. —— 它必须能被无浏览器执行面驱动')

    # ② 模板必须真把四个身份位注进表单，且归属库先于主模块加载
    for key, attr in OFFLINE_SCOPE_REQUIRED.items():
        if template_html.count(attr) < 1:
            problems.append(f'模板未注入 {attr}（归属键的 {key} 位恒空 ⇒ fail-closed 全挂起）')
    i_scope = template_html.find('js/offline_scope.js')
    i_main = template_html.find('js/offline_submit.js')
    if not (0 <= i_scope < i_main):
        problems.append(f'脚本顺序错：offline_scope.js@{i_scope} 必须先于 offline_submit.js@{i_main}'
                        '（颠倒 ⇒ 主模块取不到 ICANOfflineScope，所有条目被静默挂起）')

    # ③ 主模块必须升到 v2 键、写归属、并在**唯一发送出口**上拦（顺序判，不看有没有提）
    if "'ican_h5_offline_v2'" not in submit_js:
        problems.append('队列键仍是 v1：不带归属的旧条目没有归属化路径')
    if 'LEGACY_KEY' not in submit_js or 'migrateLegacy' not in submit_js:
        problems.append('没有 v1→v2 迁移出口（老条目要么被静默丢弃，要么被静默发送）')
    if 'scope:' not in submit_js or 'fields_digest:' not in submit_js:
        problems.append('makeItem 未写 scope / fields_digest ⇒ 条目仍不可归属')
    guard_calls = submit_js.count('Scope.itemMatchesScope(')
    if guard_calls < 2:
        problems.append(f'归属闸只出现 {guard_calls} 处（挑选队列与唯一发送出口各需一处，'
                        '少一处=绕过挑选直接调 playOne 时无人拦）')
    send_at = submit_js.find('xhr.send(')
    play_at = submit_js.find('function playOne(')
    # 不变量是「**发送之前**有守卫」，不是「文件里最后一处守卫在发送之前」——
    # 后者是本项首版的写法，实跑被真面否证：render() 里第三处守卫排在 send 之后，
    # rfind 于是抓到它并误判"守卫没挡住发送"。取数面必须落在 playOne 这一段里。
    in_play = submit_js.count('Scope.itemMatchesScope(', play_at, send_at) if play_at >= 0 and send_at > play_at else 0
    if send_at < 0:
        problems.append('找不到 xhr.send( 出口（取数面变了，本判据需同步而非默认通过）')
    elif play_at < 0:
        problems.append('找不到 function playOne(（唯一发送出口改名/拆分，本判据的取数面已失效）')
    elif in_play < 1:
        problems.append(f'playOne 内、xhr.send 之前没有归属闸（该段 {in_play} 处）'
                        ' ⇒ 守卫写在别处不等于拦住发送')
    elif guard_calls - in_play < 1:
        problems.append('挑选队列一侧（nextPlayable）没有归属闸 ⇒ 点「立即重传」清掉 parked 后无人拦')
    # ④ 路由必须从服务端单一真相源取值，而不是让 JS 自己猜
    #    （本段曾在一次补丁里被误留在 `return` 之后 ⇒ 整段永不执行，路由面的变异当场
    #     翻不出红，实测报"0 条红"。取数面若在 return 之后 = 假守卫，见第 19 项同族。）
    for param in ('scope_staff_id=', 'scope_store_id=', 'scope_pack_id=', 'scope_pack_ver='):
        if param not in route_py:
            problems.append(f'dashboard 未传 {param}（前端无处可注入该身份位）')
    m_staff = re.search(r"scope_staff_id\s*=\s*([^,\n]+)", route_py)
    if not m_staff or m_staff.group(1).strip() != 'current_user.id':
        problems.append('scope_staff_id 的右值不是 current_user.id'
                        f'（实测={m_staff.group(1).strip() if m_staff else "取不到赋值"}）'
                        ' ⇒ 换成别的来源就不是服务端真正归属记录的那个量')
    readings = {'guards': guard_calls, 'in_playOne': in_play, 'send_at': send_at,
                'scope_attrs': sum(1 for a in OFFLINE_SCOPE_REQUIRED.values() if a in template_html)}
    return problems, readings


def check_offline_queue_scope():
    """58. 离线队列条目归属（模板注入 ⇄ 归属库 ⇄ 唯一发送出口 ⇄ 路由取值 四方对账）。

    对标出处：ODK Briefcase 把 `formId + versionString + manifestUrl` 作为条目一等身份
    （`getodk/briefcase/src/org/opendatakit/briefcase/model/RemoteFormDefinition.java:23-37`），
    归属不符时是**拒绝重放**而不是"用当前凭据发出去"。我方原来只有服务端日期守卫
    （`queued_for`），同日换账号那一半没人守。
    """
    paths = {
        'submit_js': os.path.join(SRC, 'static', 'js', 'offline_submit.js'),
        'scope_js': os.path.join(SRC, 'static', 'js', 'offline_scope.js'),
        'tpl': os.path.join(SRC, 'templates', 'h5', 'dashboard.html'),
        'route': os.path.join(SRC, 'routes', 'h5.py'),
    }
    texts = {}
    missing = []
    for k, p in paths.items():
        if os.path.isfile(p):
            texts[k] = read(p)
        else:
            texts[k] = ''
            missing.append(rel(p))
    if missing:
        record('BLOCK', '58. 离线队列条目归属（模板注入 ⇄ 归属库 ⇄ 唯一发送出口 ⇄ 路由取值 四方对账）', False,
               [f'取不到文件：{"、".join(missing)} ⇒ 判"没测到"，不得记通过'])
        return
    problems, rd = _offline_scope_wiring(texts['submit_js'], texts['scope_js'],
                                         texts['tpl'], texts['route'])
    record('BLOCK', '58. 离线队列条目归属（模板注入 ⇄ 归属库 ⇄ 唯一发送出口 ⇄ 路由取值 四方对账）',
           not problems,
           problems or
           [f'归属模板位 {rd["scope_attrs"]}/4 · 归属闸共 {rd["guards"]} 处，'
            f'其中 playOne 内且先于 xhr.send 的 {rd["in_playOne"]} 处（send@{rd["send_at"]}）· '
            'v2 键与 v1 迁移出口均在'])


# 行尾/二进制判定表：门禁第 27/29/30 项与 L4 夹具共用同一份常量（两处各写一套必然漂移）
EOL_BINARY_EXTS = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.ico', '.pdf', '.docx',
                             '.xlsx', '.pptx', '.zip', '.gz', '.db', '.sqlite', '.bundle',
                             '.mp4', '.mov', '.webm', '.mp3', '.wav', '.pyc', '.whl',
                             '.ttf', '.woff', '.woff2'})
# Windows 侧要能直接双击跑的交付脚本：仓库内存 LF，检出必须是 CRLF（.gitattributes 已声明）
EOL_CRLF_REQUIRED_EXTS = frozenset({'.cmd', '.bat'})


def _git_toplevel(cwd=None):
    """git 仓库根；不可用时返回 ''（调用方须判红，不得当"没有漂移"）。"""
    r = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=cwd or ROOT,
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ''


def check_line_ending_policy():
    """工作树字节必须等于「别人 clone 后拿到的字节」，否则一切哈希类判据只在单机成立。

    动因（第 18 轮 H35，实测）：本机 `core.autocrlf=true` 而 CI（Linux）默认 false，
    仓库又**没有 `.gitattributes`** ⇒ 权威副本 82 个跟踪文件是"工作树 CRLF / blob LF"，
    第 29 项台账记的是工作树哈希，CI 检出的是 LF ⇒ 台账在 CI 必红，
    外人 `sha256sum` 也永远复算不出我们公布的那串。修法是 `.gitattributes`
    钉死策略 + 归一工作树；本项负责让它不再悄悄漂回去，三件事一起判：
      ① 策略文件在位（优先仓库根，副本内自带一份以便"解压 ZIP 复现门禁"也能判）；
      ② 副本内文本文件不得出现 CRLF；
      ③ `.cmd/.bat` 交付件必须是 CRLF（②的另一半，漏判即等于放行坏脚本）。
    无 git 环境（评委解压源码 ZIP）时退化为"遍历副本自身"，判据不减、只是不分跟踪态。
    """
    top = _git_toplevel()
    ga = ''
    for cand in ([os.path.join(top, '.gitattributes')] if top else []) + \
                [os.path.join(ROOT, '.gitattributes')]:
        if os.path.isfile(cand):
            ga = cand
            break
    problems = []
    if not ga:
        problems.append('既无仓库根也无副本内的 .gitattributes → 行尾随平台与 git 配置而变，'
                        '台账/资产哈希不可跨机复算（此项在解压态也要求副本内自带一份）')
        body = ''
    else:
        body = read(ga)
        if 'eol=lf' not in body or not re.search(r'^\*\s+text=auto', body, re.M):
            problems.append('.gitattributes 未声明 `* text=auto eol=lf`（策略没覆盖全部文本）')
        if ' binary' not in body:
            problems.append('.gitattributes 无二进制名单（图片/PDF 会被行尾规则误伤）')
    files, src_note = [], ''
    if top:
        # pathspec 用正斜杠 + 输出用 --full-name（相对**仓库根**而非相对 cwd）：
        # Windows 下反斜杠会被当转义符、默认相对 cwd 的输出拼出来全不存在，
        # 两个坑都实测过——它们会让本项退化成"永远零输入"，靠下面的守卫兜住。
        ls = subprocess.run(['git', 'ls-files', '--full-name', '-z', '--',
                             ROOT.replace(os.sep, '/')], cwd=top, capture_output=True)
        files = [os.path.join(top, x.decode('utf-8', 'replace').replace('/', os.sep))
                 for x in ls.stdout.split(b'\0') if x]
        src_note = f'git 跟踪集（根 {top}）'
        if not files:
            # 上溯到了某个 .git，但该仓的索引里没有本副本 ⇒ 这是"解压到别人仓库里"
            # 的形态，git 面必然空。改走遍历腿，而不是让本项以"零输入"红在一次
            # 与交付无关的父级仓库上。
            files = []
            src_note = ''
    if not src_note:
        # 无 git（源码 ZIP 解压态）⇒ 改用与第 29 项**同一份**排除规则遍历副本：
        # 判据强度不变，只是不分"是否跟踪"。用目录名集合精确匹配，不用子串 in
        # （子串匹配会把 `data_backup2/` 这类目录一起放过，属同族误判）。
        files = [os.path.join(dp, fn) for dp, _dns, fns in os.walk(ROOT) for fn in fns
                 if not set(dp.replace(os.sep, '/').split('/')) & LEDGER_SKIP_DIRS]
        src_note = '遍历副本自身（无 git 或该 git 不含本副本；排除规则与第 29 项同源）'
    n_text = n_crlf_req = 0
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext in EOL_BINARY_EXTS or not os.path.isfile(f):
            continue
        has_crlf = b'\r\n' in open(f, 'rb').read()
        if ext in EOL_CRLF_REQUIRED_EXTS:
            n_crlf_req += 1
            if not has_crlf:
                problems.append(f'{os.path.relpath(f, ROOT)} 应为 CRLF（Windows 交付脚本）')
        else:
            n_text += 1
            if has_crlf:
                problems.append(f'{os.path.relpath(f, ROOT)} 工作树带 CRLF，'
                                f'与 blob/检出字节不同 → 台账哈希不可跨机复算')
    if not n_text:
        problems.append('未扫到任何文本文件——零输入不记通过')
    record('BLOCK', '30. 行尾确定性（工作树 ⇄ 检出同形，哈希可跨机复算）', not problems,
           problems[:12] or [f'{n_text} 个文本文件无 CRLF 残留；{n_crlf_req} 个 .cmd/.bat 为 CRLF；'
                             f'策略文件 {ga or "无"}；取数面：{src_note}'])




# ══════════════ 31. 观测与归因链路（BLOCK）══════════════
def check_observability_chain():
    """请求级观测链必须端到端接线：计数有喂、状态联动、trace 可关联、取证工具有牙。

    动因（第 19 轮实测）：改造前 `src/performance.py` 已有慢请求/慢查询 WARN，但
    `grep -n 'slow_request|SLOW_REQUEST|performance'` 在 smoke / 漏洞 / L4 / 本门禁
    四份文件里只命中一行注释 —— **观测层自己没被观测**。靶群 8/8 都有可观测性自身的
    测试且断言的是字段（zulip `zerver/tests/test_middleware.py:29-55`、frappe
    `frappe/tests/test_monitor.py:32-38`）。本项把这条链的六段一次钉住，拆任一段即红。
    """
    problems = []
    evidence = []
    def _chain_inputs():
        """按"最小取用面"读第 31 项需要的源码，并把崩溃变成可诊断的红色结论。

        M62 的来由：首跑在 `read(test_l4_assertions.py)`（实测 291,100 字符）处抛
        `MemoryError`，进程内什么都没留下，事后无法归因。两处改动：
        ① 那份 291 KB 的文件只需要回答"有没有 S20 段、有没有引用 diag"——
           改成**流式扫描 + 命中即停**，不再把整份读进内存（降自己的峰值）；
        ② 其余小文件仍整份读，但一旦再遇 MemoryError，不再让异常穿出门禁：
           带上当场字节数与"已读到第几个"的现场信息判红（崩溃≠通过）。
        """
        small = {
            'app.py': os.path.join(SRC, 'app.py'),
            'performance.py': os.path.join(SRC, 'performance.py'),
            'logging_config.py': os.path.join(SRC, 'logging_config.py'),
            'ai_trace.py': os.path.join(SRC, 'ai_trace.py'),
            'diag.py': os.path.join(SRC, 'diag.py'),
        }
        got = {}
        try:
            for key, path in small.items():
                got[key] = read(path)
        except MemoryError:
            done = ', '.join(sorted(got)) or '无'
            sizes = '; '.join(f'{k}={os.path.getsize(v)}B' for k, v in small.items()
                              if os.path.isfile(v))
            raise MemoryError(f'第 31 项读取阶段内存不足；已读到：{done}｜现场字节数：{sizes}')
        has_s20 = has_diag = False
        test_path = os.path.join(ROOT, 'test_l4_assertions.py')
        try:
            with io.open(test_path, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    if not has_s20 and 'S20' in line:
                        has_s20 = True
                    if not has_diag and 'diag' in line:
                        has_diag = True
                    if has_s20 and has_diag:
                        break
        except MemoryError:
            sizes = '; '.join(f'{k}={os.path.getsize(v)}B' for k, v in small.items()
                              if os.path.isfile(v))
            raise MemoryError(f'第 31 项流式扫描测试文件时内存不足（该文件 '
                              f'{os.path.getsize(test_path) if os.path.isfile(test_path) else 0}B）'
                              f'｜小文件现场字节数：{sizes}')
        got['test_l4_assertions.py'] = (
            ('S20 ' if has_s20 else '') + ('import diag ' if has_diag else '')) or '未命中'
        return got

    try:
        parts = _chain_inputs()
    except MemoryError as exc:
        record('BLOCK', '31. 观测与归因链路（计数接线 + 状态联动 + trace 可关联 + 取证工具有牙）',
               False, [f'MemoryError（崩溃不得记通过）：{exc}'])
        return
    empty = [k for k, v in parts.items() if not v]
    if empty:
        record('BLOCK', '31. 观测与归因链路（计数接线 + 状态联动 + trace 可关联 + 取证工具有牙）',
               False, [f'读不到或为空：{p}——零输入不记通过' for p in empty])
        return
    app_src, perf_src = parts['app.py'], parts['performance.py']
    log_src, trace_src = parts['logging_config.py'], parts['ai_trace.py']
    diag_src, test_src = parts['diag.py'], parts['test_l4_assertions.py']

    # 段 1：监控必须挂上应用
    if 'init_performance_monitor(app)' not in app_src:
        problems.append('src/app.py 未调用 init_performance_monitor(app)（慢请求腿根本没挂）')
    # 段 2：开发态/回落态也必须注入 request_id。按「setup_logging 之后 6 行内必须出现
    # inject_request_id」逐分支核，而不是数全局出现次数——数次数会让"只在生产分支挂一次"
    # 蒙过去，而那正是改造前的真实形态（测试与漏配 FLASK_ENV 的部署拿不到追踪 id）。
    lines = app_src.splitlines()
    dev_branches = [i for i, ln in enumerate(lines) if 'setup_logging(app, production=False)' in ln]
    if not dev_branches:
        problems.append('未找到任何 setup_logging(production=False) 分支（日志初始化被移走了？）')
    for i in dev_branches:
        window = '\n'.join(lines[i:i + 6])
        if 'inject_request_id(app)' not in window:
            problems.append(f'src/app.py 第 {i + 1} 行的开发态分支只调 setup_logging '
                            '而未调 inject_request_id(app)（开发/回落态没有追踪 id）')
    evidence.append(f'开发态日志分支 {len(dev_branches)} 处均已注入 request_id')
    # 段 3：慢请求腿必须真的喂计数器
    if 'diag.observe(' not in perf_src:
        problems.append('src/performance.py 的慢请求钩子未调用 diag.observe()（有 WARN 无聚合）')
    if '_register_error_fingerprint(app)' not in perf_src:
        problems.append('src/performance.py 未注册异常指纹聚合（5xx 只有总数、没有成因）')
    # 段 4：日志必须全域带 id，并把 id 回给客户端
    n_filter = log_src.count('addFilter(_request_id_filter)')
    if n_filter < 3:
        problems.append(f'request_id 过滤器只装了 {n_filter} 个 handler（应为 3：生产文件/'
                        '生产控制台/开发控制台；装少了等于那一路日志永远没有 id）')
    if 'X-Request-Id' not in log_src:
        problems.append('响应头未回写 X-Request-Id（用户报障没有可引用的编号，证据链断在浏览器侧）')
    if 'teardown_request' not in log_src or 'clear_request_id()' not in log_src:
        problems.append('未注册 teardown_request 清除追踪 id（waitress 复用线程会把上一个请求的 id 串到下一个）')
    # 段 5：AI 留痕必须带关联键
    if "'request_id'" not in trace_src or 'diag.get_request_id()' not in trace_src:
        problems.append('src/ai_trace.py 的留痕行没有 request_id（访问日志与 AI 耗时串不起来）')
    # 段 6：/healthz 的 status 必须与子检查联动，且不得把路径名泄到公开端点
    if re.search(r"'status':\s*'ok'\s*,", app_src):
        problems.append('/healthz 的 status 又写回常量 ok（db=error 时会说谎；判定必须来自子检查汇总）')
    for frag, why in (("'diag': _diag.healthz_stats()", '公开端点缺观测计数聚合段'),
                      ("'db_ms'", '探活没有 DB 往返耗时（"活着但很慢"这一档看不见）')):
        if frag not in app_src:
            problems.append(f'/healthz 未包含 {frag}：{why}')
    hs_start = diag_src.find('def healthz_stats')
    if hs_start < 0:
        problems.append('src/diag.py 没有 healthz_stats()（公开端点的聚合出口被删）')
    else:
        hs_end = diag_src.find(chr(10) + 'def ', hs_start + 1)
        hs = diag_src[hs_start:hs_end if hs_end > 0 else len(diag_src)]
        if "'top_paths'" in hs or "'by_path'" in hs or "'error_kinds': dict" in hs:
            problems.append('healthz_stats() 输出了路径名/异常类名（内部情报不得进公开端点，'
                            '口径同第 10 轮 L22）')
    # 段 7：观测模块必须被断言覆盖（防止"写了没测"，同第 19 项形态，但对象是运行时模块）
    if 'S20' not in test_src or 'import diag' not in test_src.replace('from src import diag', 'import diag'):
        problems.append('test_l4_assertions.py 没有 S20 观测链断言段（观测层重新变成没被观测的那一层）')
    # 段 8：离线取证工具必须自带夹具自证，且真跑一次
    triage = os.path.join(ROOT, 'log_triage.py')
    if not os.path.isfile(triage):
        problems.append('根级 log_triage.py 缺失（慢请求归因只剩人工翻日志）')
    else:
        try:
            r = subprocess.run([PY, 'log_triage.py', '--selftest'], cwd=ROOT,
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0 or '全对' not in (r.stdout or ''):
                problems.append('log_triage.py --selftest 未通过（rc=%s）：%s'
                                % (r.returncode, (r.stdout or r.stderr or '').strip()[-300:]))
            else:
                evidence.append('log_triage --selftest rc=0（三条解析腿在夹具上均有命中）')
        except Exception as exc:  # noqa: BLE001  跑不动＝没验证过，不得记通过
            problems.append(f'log_triage.py --selftest 无法执行：{type(exc).__name__}: {exc}')

    record('BLOCK', '31. 观测与归因链路（计数接线 + 状态联动 + trace 可关联 + 取证工具有牙）',
           not problems, problems or evidence)


# ══════════════ 32. 数据出口与入口防护接线（BLOCK）══════════════
def _ast_calls(tree_or_fn, suffix):
    """返回对某函数的**调用节点**列表（不看文档与注释）。

    为什么特意写这一条：本仓立过规矩「判据只能匹配行为，不能匹配描述」，
    而第 32 项第一版用 `子串 in ast.unparse(函数)` 判接线，结果 `unescape_cell`
    只出现在 _parse_rows 的文档字符串里也照样判通过（变异反证当场抓红）。
    """
    out = []
    for node in ast.walk(tree_or_fn):
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            if name == suffix or name.endswith('.' + suffix):
                out.append(node)
    return out


def _dict_key_literals(tree):
    """所有字典字面量的字符串键（回执字段必须以"键"存在，不是以词存在）。"""
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return keys


def _fold_int(node):
    """把 `512 * 1024` 这类纯数字表达式折成整数；含名字/调用则返回 None。

    `ast.literal_eval` 不折 `*`，所以第一版把 `LOG_MAX_BYTES = 512 * 1024` 判成
    "不是整数字面量"——基线自红，被变异 runner 的基线检查当场抓到（探针失能 ≠ 判据失败）。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _fold_int(node.operand)
        return None if inner is None else -inner
    if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Mult, ast.Add, ast.Sub, ast.FloorDiv, ast.Pow)):
        left, right = _fold_int(node.left), _fold_int(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.FloorDiv):
            return None if right == 0 else left // right
        return left ** right
    return None


def _int_constant(tree, name):
    """模块级 `NAME = <整数表达式>` 的值；取不到返回 None。"""
    for node in getattr(tree, 'body', []):
        if isinstance(node, ast.Assign) and any(
                isinstance(tg, ast.Name) and tg.id == name for tg in node.targets):
            value = _fold_int(node.value)
            if value is not None:
                return value
    return None


def check_exit_guardrails():
    """电子表格出口必须"转义 + 上限 + 留痕"三件套齐备，入口必须对称还原。

    动因（对标第 20 轮实测）：看板导出把用户自由文本（产品名、异常备注）原样写进 CSV，
    而 Excel/WPS 会对以 =+-@ 开头的单元格求值 —— 这是**数据面的跨信任边界位移**，
    第 28 项（ruff S）看代码反模式、第 5 项（盲审）看材料字面串、三套测试只看 HTTP 200，
    全链无人看守。上游对照（逐行核实）：frappe `frappe/utils/csvutils.py:14/17/24/121`
    同时提供转义与还原（只做一半会把标记烘进回灌数据）；`superset/config.py:787`
    `ALLOW_FULL_CSV_EXPORT: False` 与 `:1744` `SQL_MAX_ROW` 用上限拒绝"一次全量导出"。
    本项按 AST 枚举"哪些函数在往外发电子表格"，逐个要求三件套，而不是点名字符串。
    """
    problems, evidence = [], []
    targets = ['src/csv_guard.py', 'src/data_import.py', 'src/exit_audit.py',
               'src/routes/admin.py']
    srcs, trees = {}, {}
    for rel in targets:
        path = os.path.join(ROOT, *rel.split('/'))
        if not os.path.isfile(path):
            problems.append('%s 不存在（出口防护面缺输入，不得记通过）' % rel)
            continue
        with open(path, encoding='utf-8') as fh:
            srcs[rel] = fh.read()
    if len(srcs) == len(targets):
        for rel in targets:
            try:
                trees[rel] = ast.parse(srcs[rel])
            except SyntaxError as exc:
                problems.append('%s 语法错误：%s' % (rel, exc))
    if len(srcs) != len(targets) or len(trees) != len(targets):
        record('BLOCK', '32. 数据出口与入口防护接线（转义 + 上限 + 留痕 + 对称还原）', False, problems)
        return

    # ① 触发字符表：单一真相源 + 六个字符一个不能少（少一个就是一类可执行入口没堵）
    guard_tree = trees['src/csv_guard.py']
    trig = None
    for node in guard_tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(tg, ast.Name) and tg.id == 'FORMULA_TRIGGER_CHARS'
                for tg in node.targets):
            trig = ast.literal_eval(node.value) if isinstance(node.value, ast.Tuple) else None
    if not trig:
        problems.append('csv_guard 未以元组字面量定义 FORMULA_TRIGGER_CHARS'
                        '（出入口两侧必须共用这张表，运行时拼装迟早漂移）')
    else:
        lack = [c for c in ('=', '+', '-', '@', chr(9), chr(13)) if c not in trig]
        if lack:
            problems.append('触发字符表缺 %s（少一个＝留一类可执行入口没堵）'
                            % ' '.join(repr(c) for c in lack))
        else:
            evidence.append('触发字符表六项齐备：%s' % ' '.join(repr(c) for c in trig))
    for fn in ('escape_cell', 'unescape_cell', 'escape_dataframe', 'rows_exceed',
               'max_export_rows'):
        if not any(isinstance(n, ast.FunctionDef) and n.name == fn for n in ast.walk(guard_tree)):
            problems.append('csv_guard 缺 %s()（转义与还原必须成对，只做一半会污染回灌数据）' % fn)

    # ② 出口面：按"返回带 text/csv 或 Content-Disposition 的 Response"这一调用结构枚举
    admin_tree = trees['src/routes/admin.py']
    outlets = []
    for fn in ast.walk(admin_tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        sheets, responses = 0, 0
        for call in _ast_calls(fn, 'Response'):
            responses += 1
            blob = ast.unparse(call)
            if 'text/csv' in blob or 'Content-Disposition' in blob:
                sheets += 1
        if sheets:
            outlets.append((fn, sheets, responses))
    if not outlets:
        problems.append('没枚举到任何电子表格出口（判据无输入 ≠ 通过；'
                        '导出路由改名或被移走时，本项要先跟着改）')
    for fn, _sheets, responses in outlets:
        lack = set()
        for name, why in (('escape_dataframe', '转义'), ('rows_exceed', '上限')):
            if not _ast_calls(fn, name):
                lack.add('%s（%s）' % (name, why))
        records = _ast_calls(fn, 'record')
        # 留痕按"返回路径数"要求：出口有几条返回路径，就该有几条留痕，
        # 否则就是某一条路径静默出去（"拆掉成功路径留痕"这类回归的真实形态）。
        if len(records) < responses:
            lack.add('留痕（%d 条留痕 / %d 条返回路径）' % (len(records), responses))
        if lack:
            problems.append('出口 %s() 缺：%s（漏一件＝该出口裸奔）'
                            % (fn.name, '、'.join(sorted(lack))))
        else:
            evidence.append('出口 %s() 三件套齐备（转义 + 上限 + 留痕 ×%d）'
                            % (fn.name, len(records)))
    if not _ast_calls(admin_tree, 'max_export_rows'):
        problems.append('路由未调用 csv_guard.max_export_rows() 取上限'
                        '（写死字面量就会与 .env.example 的登记值漂移）')

    # ③ 入口面：_parse_rows 必须**调用**还原；不得自带第二张表；回执键必须真是字典键
    imp_tree = trees['src/data_import.py']
    parse_fn = next((n for n in ast.walk(imp_tree)
                     if isinstance(n, ast.FunctionDef) and n.name == '_parse_rows'), None)
    if parse_fn is None:
        problems.append('data_import 找不到 _parse_rows()（入口面被改名，本项需同步）')
    elif not _ast_calls(parse_fn, 'unescape_cell'):
        problems.append('data_import._parse_rows 未调用 unescape_cell（不还原标记，'
                        '自家导出文件导回去就把引号烘进业务数据）')
    else:
        evidence.append('入口 _parse_rows 逐格还原转义标记（与出口共用一张表）')
    if any(isinstance(n, ast.Assign) and any(
            isinstance(tg, ast.Name) and tg.id == 'FORMULA_TRIGGER_CHARS' for tg in n.targets)
            for n in ast.walk(imp_tree)):
        problems.append('data_import 自带第二张触发字符表（必须单一真相源，两处迟早漂移）')
    if 'ignored_columns' not in _dict_key_literals(imp_tree):
        problems.append('导入报告字典里没有 ignored_columns 键'
                        '（未识别列静默丢弃＝用户以为导回来了）')

    # ④ 留痕面：有界（上限须是可处置量级）+ 失败可数 + 不写业务明细
    aud_tree = trees['src/exit_audit.py']
    for fn in ('record', '_rotate_if_needed', 'tail_lines', 'stats'):
        if not any(isinstance(n, ast.FunctionDef) and n.name == fn for n in ast.walk(aud_tree)):
            problems.append('exit_audit 缺 %s()' % fn)
    cap = _int_constant(aud_tree, 'LOG_MAX_BYTES')
    if cap is None:
        problems.append('exit_audit.LOG_MAX_BYTES 不是整数字面量（有界性无从核验）')
    elif not 1024 <= cap <= 8 * 1024 * 1024:
        problems.append('exit_audit.LOG_MAX_BYTES=%d 超出可处置量级'
                        '（免费层磁盘是唯一硬约束，抬到天文数字等于取消有界）' % cap)
    else:
        evidence.append('留痕单文件上限 %d KB，超限轮转' % (cap // 1024))
    rotate_fn = next((n for n in ast.walk(aud_tree)
                      if isinstance(n, ast.FunctionDef) and n.name == '_rotate_if_needed'), None)
    if rotate_fn is None:
        problems.append('exit_audit 找不到 _rotate_if_needed()')
    else:
        # `_LOCK` 是在 `with` 语句里被"持有"而不是被"调用"，所以必须按 ast.With 取——
        # 第一版用 _ast_calls 找它，等于这条腿永远不触发（判据自己空转，比没有更糟）。
        inner_locks = [ast.unparse(w) for w in ast.walk(rotate_fn)
                       if isinstance(w, ast.With) and '_LOCK' in ast.unparse(w.items)]
        if inner_locks:
            problems.append('_rotate_if_needed 内部又取 _LOCK（threading.Lock 不可重入，'
                            '持锁时轮转失败即自锁，而失败恰好在并发导出时发生）')
        else:
            evidence.append('轮转函数不自取锁（由调用方持锁），无自锁路径')
    for ban in ('product_name', 'abnormal_notes', 'metrics_json'):
        if ban in srcs['src/exit_audit.py']:
            problems.append('exit_audit 出现业务字段 %s（安全红线：日志不得记业务明细）' % ban)

    # ⑤ 生成物纪律：留痕文件必须在 .gitignore 内（本项目红线：新落盘文件需 gitignore）
    gi_path, gi_text = None, ''
    probe_dir = ROOT
    for _ in range(6):
        cand = os.path.join(probe_dir, '.gitignore')
        if os.path.isfile(cand):
            gi_path, gi_text = cand, open(cand, encoding='utf-8').read()
            break
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            break
        probe_dir = parent
    if gi_path is None:
        problems.append('找不到 .gitignore（无法核验留痕是否会入库，判"没测到"不判通过）')
    elif 'exit_audit.jsonl' not in gi_text:
        problems.append('data/exit_audit.jsonl 未 gitignore（留痕会随提交外泄）')
    else:
        evidence.append('留痕文件已在 .gitignore 内')

    record('BLOCK', '32. 数据出口与入口防护接线（转义 + 上限 + 留痕 + 对称还原）', not problems, problems or evidence)


# ══════════════ 33. 开放 API 契约面双向对账（BLOCK）══════════════
def _s33_load_yaml(text):
    """用「重复键即报错」的加载器解析 spec；返回 (doc, 错误串或 None)。

    PyYAML 默认后键覆盖前键：同一路径里写两份 `'429':` 不报错，读到的那份可能正好是
    对的那份，于是漂移被静默吃掉（本轮批量插 429 时实测造出过一例重复键）。
    加载器继承 SafeLoader，不引入 python/object 标签，只多一道重复键检测。
    """
    try:
        import yaml
    except ImportError:
        return None, 'PyYAML 不可用：无法严格解析 openapi.yaml（判"没测到"不判通过）'

    class _NoDupLoader(yaml.SafeLoader):
        pass

    def _construct_mapping(loader, node, deep=False):
        seen = {}
        for k_node, _v_node in node.value:
            key = loader.construct_object(k_node, deep=deep)
            if key in seen:
                raise ValueError(f'重复键 {key!r}（第 {seen[key]} 行与第 '
                                 f'{k_node.start_mark.line + 1} 行）')
            seen[key] = k_node.start_mark.line + 1
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    _NoDupLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                                 _construct_mapping)
    try:
        return yaml.load(text, _NoDupLoader), None
    except yaml.YAMLError as exc:
        return None, f'openapi.yaml 解析失败：{exc}'
    except ValueError as exc:
        return None, f'openapi.yaml {exc}（重复键会被后写覆盖，判据看不见被盖掉的那份）'


def _s33_query_params(tree):
    """AST 枚举代码真实读取的查询参数名（request.args.get('NAME')）。"""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != 'get' or not isinstance(node.func.value, ast.Attribute):
            continue
        base = node.func.value
        if base.attr != 'args' or not isinstance(base.value, ast.Name):
            continue
        if base.value.id != 'request':
            continue
        if node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            names.add(node.args[0].value)
    return names


def _s33_response_headers(tree):
    """AST 枚举代码真实写出的响应头名（<resp>.headers['X'] 赋值/删除）。"""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript) or not isinstance(node.value, ast.Attribute):
            continue
        attr = node.value
        if attr.attr != 'headers' or not isinstance(attr.value, ast.Name):
            continue
        if attr.value.id not in ('resp', 'response'):
            continue
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.add(key.value)
    return names


def _s33_route_paths(tree):
    """AST 枚举 blueprint 上注册的路径字面量。"""
    paths = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                        and dec.func.attr == 'route' and dec.args:
                    first = dec.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        paths.add(first.value)
    return paths


def _s33_paginated_views(tree):
    """代码里同时调用 _parse_limit 与 _parse_cursor 的视图 = 分页端点（不靠人写清单）。"""
    views = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        called = {c.func.id for c in ast.walk(node)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        if '_parse_limit' not in called or '_parse_cursor' not in called:
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                    and dec.func.attr == 'route' and dec.args \
                    and isinstance(dec.args[0], ast.Constant):
                views[str(dec.args[0].value)] = node.name
    return views


def _s33_param_names(op, doc):
    """单个 operation 的查询参数名集合（$ref 就地展开；全局与逐端点共用此口径）。"""
    pool = ((doc.get('components') or {}).get('parameters') or {})
    names = set()
    for prm in (op.get('parameters') or []):
        if not isinstance(prm, dict):
            continue
        ref = prm.get('$ref')
        if isinstance(ref, str) and ref.startswith('#/components/parameters/'):
            resolved = pool.get(ref.rsplit('/', 1)[-1]) or {}
            if resolved.get('in') == 'query' and resolved.get('name'):
                names.add(resolved['name'])
        elif prm.get('in') == 'query' and prm.get('name'):
            names.add(prm['name'])
    return names


def _s33_spec_query_params(doc):
    """spec 侧全部查询参数名（含 components.parameters 的 $ref 展开）。"""
    names = set()
    for item_op in (doc.get('paths') or {}).values():
        for op in (item_op or {}).values():
            if isinstance(op, dict):
                names |= _s33_param_names(op, doc)
    return names


def _s33_spec_headers(doc):
    """spec 侧声明的响应头名：只认「某个响应真的挂了这条头」。

    口径说明：components.headers 下的键是定义名（RetryAfter），不是 HTTP 头名
    （Retry-After）。把定义名当声明会造出假绿 —— 定义了却没挂到任何响应上，
    等于文档没写。所以这里只统计响应级 headers 映射的键。
    """
    names = set()
    for item_op in (doc.get('paths') or {}).values():
        for op in (item_op or {}).values():
            if not isinstance(op, dict):
                continue
            for resp in (op.get('responses') or {}).values():
                if isinstance(resp, dict):
                    names |= set((resp.get('headers') or {}).keys())
    for resp in ((doc.get('components') or {}).get('responses') or {}).values():
        if isinstance(resp, dict):
            names |= set((resp.get('headers') or {}).keys())
    return names


def check_api_contract_surface():
    """openapi.yaml 的参数/响应头 ⇄ 代码实际读写面双向对账（第 21 轮 M71）。

    为什么第 15 项不够：第 15 项只对「端点集合 + 作用域枚举」。本轮把游标翻页
    （limit/cursor/has_more/next_cursor）与配额头（X-RateLimit-* / Retry-After）
    写进代码后，若 spec 漏写或写错名字，第 15 项照样全绿 —— 而接入方照文档写代码
    会拿到「参数被静默忽略」的响应，比 404 更难查。判据形状借鉴 Stripe 的
    openapi spec 即事实源：文档说了代码没有、代码做了文档没说，两头都算漂移。
    """
    problems, evidence = [], []
    code_path = os.path.join(ROOT, 'src', 'api_open.py')
    spec_path = os.path.join(ROOT, 'openapi.yaml')
    for rel, path in (('src/api_open.py', code_path), ('openapi.yaml', spec_path)):
        if not os.path.isfile(path):
            problems.append(f'{rel} 不存在（契约对账缺输入，不得记通过）')
    if problems:
        record('BLOCK', '33. 开放 API 契约面双向对账（查询参数 + 响应头，spec ⇄ 代码）', False, problems)
        return

    with open(code_path, encoding='utf-8') as fh:
        code_src = fh.read()
    try:
        code_tree = ast.parse(code_src)
    except SyntaxError as exc:
        record('BLOCK', '33. 开放 API 契约面双向对账（查询参数 + 响应头，spec ⇄ 代码）', False,
               [f'src/api_open.py 语法错误：{exc}'])
        return
    with open(spec_path, encoding='utf-8') as fh:
        spec_text = fh.read()
    spec_doc, yaml_error = _s33_load_yaml(spec_text)
    if yaml_error:
        record('BLOCK', '33. 开放 API 契约面双向对账（查询参数 + 响应头，spec ⇄ 代码）', False,
               [yaml_error])
        return
    if not isinstance(spec_doc, dict):
        record('BLOCK', '33. 开放 API 契约面双向对账（查询参数 + 响应头，spec ⇄ 代码）', False,
               ['openapi.yaml 顶层不是映射（取不到 paths/components，判没测到）'])
        return

    code_params = _s33_query_params(code_tree)
    spec_params = _s33_spec_query_params(spec_doc)
    code_headers = _s33_response_headers(code_tree)
    spec_hdrs = _s33_spec_headers(spec_doc)
    api_paths = {p for p in _s33_route_paths(code_tree) if '{' not in p}
    paginated = _s33_paginated_views(code_tree)

    if not code_params or not spec_params:
        problems.append(f'枚举器空转：代码参数 {len(code_params)} 个 / spec 参数 {len(spec_params)} 个'
                        '（任一侧为 0 说明对账根本没发生，不得记通过）')
    else:
        missing_doc = sorted(code_params - spec_params)
        ghost_doc = sorted(spec_params - code_params)
        if missing_doc:
            problems.append(f'代码读了但 spec 未声明的查询参数：{missing_doc}')
        if ghost_doc:
            problems.append(f'spec 声明但代码从不读的查询参数：{ghost_doc}（文档在承诺代码没做的事）')
        evidence.append(f'查询参数双向一致：代码 {len(code_params)} ⇄ spec {len(spec_params)}')

    if not code_headers:
        problems.append('代码侧未枚举到任何响应头写出点（配额头守卫丢失或被改名，判没测到）')
    elif not spec_hdrs:
        problems.append(f'spec 未声明任何响应头，但代码写出 {sorted(code_headers)}')
    else:
        h_missing = sorted(code_headers - spec_hdrs)
        h_ghost = sorted(n for n in spec_hdrs - code_headers if n.startswith(('X-Rate', 'Retry-')))
        if h_missing:
            problems.append(f'代码写出但 spec 未声明的响应头：{h_missing}')
        if h_ghost:
            problems.append(f'spec 声明但代码不写的配额/重试类响应头：{h_ghost}')
        if not h_missing and not h_ghost:
            evidence.append(f'响应头双向一致：代码 {len(code_headers)} ⇄ spec {len(spec_hdrs)}')

    # 翻页契约逐端点核对：代码里翻页的端点，spec 必须同时声明 limit+cursor 参数
    # 且 200 响应体里有 has_more+next_cursor —— 只写一半的文档会让调用方只能截断不能续取
    paths_doc = spec_doc.get('paths') or {}
    if not paginated:
        problems.append('代码侧未枚举到任何分页端点（_parse_limit/_parse_cursor 改名或摘线，判没测到）')
    for path, view in sorted(paginated.items()):
        op = ((paths_doc.get(path) or {}).get('get')
              or (paths_doc.get('/' + path.strip('/')) or {}).get('get'))
        if not isinstance(op, dict):
            problems.append(f'分页端点 {path}（{view}）在 spec 里没有 get 段')
            continue
        declared = _s33_param_names(op, spec_doc)
        for need in ('limit', 'cursor'):
            if need not in declared:
                problems.append(f'{path} 代码按 {need} 翻页，spec 未声明该参数')
        schema = ((((op.get('responses') or {}).get('200') or {}).get('content') or {})
                  .get('application/json') or {}).get('schema') or {}
        props = set((schema.get('properties') or {}).keys())
        for need in ('has_more', 'next_cursor'):
            if need not in props:
                problems.append(f'{path} 响应会回 {need}，spec 的 200 schema 里没有该字段')
        if not problems:
            evidence.append(f'翻页契约齐：{path} 参数 {sorted(declared)} + 响应游标字段')

    if not api_paths:
        problems.append('代码侧未枚举到任何 API 路径（装饰器形状变了，判没测到）')
    else:
        spec_paths = {('/' + p.strip('/')) if not p.startswith('/') else p
                      for p in (spec_doc.get('paths') or {})}
        evidence.append(f'代码路径 {len(api_paths)} 条 ⇄ spec 路径 {len(spec_paths)} 条'
                        '（端点集合由第 15 项看守，本项只看参数与响应头）')

    record('BLOCK', '33. 开放 API 契约面双向对账（查询参数 + 响应头，spec ⇄ 代码）',
           not problems, problems or evidence)


# ══════════════ 34. 对外错误契约（JSON 错误面接线与 spec 码表对账，BLOCK）══════════════
def _s34_tuple_literal(tree, name):
    """取模块级元组字面量（`ERROR_CODES = (...)`）；不是元组字面量一律视为取不到。"""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            if isinstance(node.value, (ast.Tuple, ast.List)):
                return [ast.literal_eval(e) for e in node.value.elts]
    return None


def _s34_json_error_codes(tree):
    """枚举 `api_errors.json_error(status, 'code', ...)` 调用点上的字面码（AST，不信注释）。"""
    codes, calls = set(), 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == 'json_error'
                and isinstance(f.value, ast.Name) and f.value.id == 'api_errors'):
            continue
        calls += 1
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            codes.add(node.args[1].value)
    return codes, calls


def _s34_error_handlers(tree):
    """app.py 上的 `@app.errorhandler(X)` → {X: 是否经过 JSON 分叉}。"""
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == 'errorhandler'):
                continue
            key = 'CSRFError'
            if dec.args and isinstance(dec.args[0], ast.Constant):
                key = dec.args[0].value
            wired = any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr in ('api_error_or', 'json_error')
                for c in ast.walk(node))
            out[key] = out.get(key, False) or wired
    return out


def check_api_error_contract():
    """`/api/v1` 的错误面必须是 JSON，且机读码与 spec 逐项相等（第 22 轮 H50）。

    动因（本轮当场实测）：用 test_client 打六条真实错误腿，`/api/v1/tasks/999999`→404、
    `/api/v1/sales/daily?limit=0`→400、`/api/v1/dashboard?store_id=999999`→403、
    `/api/v1/meta` 无令牌→401、`/api/v1/nope`→404 **五条都吐 `text/html`**，
    只有代码内联 `jsonify` 的那条 403 是 JSON；根因是 `src/app.py` 的 40x handler
    全部无条件 `render_template`，而 `openapi.yaml` 声明的是 JSON `Error` 体。
    接入方按文档写 `resp.json()['error']`，会在每个非 2xx 上崩在解析 HTML。
    上游对照：Stripe 的错误体是 `{type, code, message, request_log_url}` 固定形状，
    且 `request_*` 追踪串与响应头同源；GitHub API 同样承诺 `application/problem+json`。
    本项只做静态接线对账（handler 是否分叉、码表是否与 spec 相等、枚举为空即红），
    行为面（真发请求验 content-type）由 L4 S28 承担 —— 两层缺一不可。
    """
    problems, evidence = [], []
    paths = {'src/api_errors.py': os.path.join(ROOT, 'src', 'api_errors.py'),
             'src/api_open.py': os.path.join(ROOT, 'src', 'api_open.py'),
             'src/app.py': os.path.join(ROOT, 'src', 'app.py'),
             'openapi.yaml': os.path.join(ROOT, 'openapi.yaml')}
    texts = {}
    for rel, path in paths.items():
        if not os.path.isfile(path):
            problems.append(f'{rel} 不存在（错误面对账缺输入，不得记通过）')
        else:
            texts[rel] = read(path)
    if problems:
        record('BLOCK', '34. 对外错误契约（JSON 错误面接线 + 机读码与 spec 双向对账）', False, problems)
        return

    trees = {}
    for rel in ('src/api_errors.py', 'src/api_open.py', 'src/app.py'):
        try:
            trees[rel] = ast.parse(texts[rel])
        except SyntaxError as exc:
            problems.append(f'{rel} 语法错误：{exc}')
    if problems:
        record('BLOCK', '34. 对外错误契约（JSON 错误面接线 + 机读码与 spec 双向对账）', False, problems)
        return

    codes = _s34_tuple_literal(trees['src/api_errors.py'], 'ERROR_CODES')
    schema_keys = _s34_tuple_literal(trees['src/api_errors.py'], 'ERROR_SCHEMA_KEYS')
    if not codes:
        problems.append('取不到 ERROR_CODES 元组字面量（改成推导式/外部来源即视为枚举器空转）')
        codes = []
    if not schema_keys:
        problems.append('取不到 ERROR_SCHEMA_KEYS 元组字面量')
        schema_keys = []

    # ① 代码侧调用点：每个 json_error 的字面码必须在清单内，且调用点不得为 0
    used, calls = _s34_json_error_codes(trees['src/api_open.py'])
    if calls == 0:
        problems.append('src/api_open.py 里 0 处 api_errors.json_error() 调用'
                        '（错误出口又回到各写一份的 jsonify，判没测到）')
    else:
        unknown = sorted(c for c in used if c not in codes)
        if unknown:
            problems.append(f'代码里出现未登记的机读码（先加 ERROR_CODES 与 spec enum 再上代码）：{unknown}')
        evidence.append(f'json_error 调用点 {calls} 处，字面码 {sorted(used)}')

    # ② handler 分叉：app.py 每条 errorhandler 都必须经过 JSON 分叉函数
    handlers = _s34_error_handlers(trees['src/app.py'])
    if not handlers:
        problems.append('src/app.py 未枚举到任何 @app.errorhandler（注册方式变了即视为漏测）')
    else:
        unwired = sorted(str(k) for k, wired in handlers.items() if not wired)
        if unwired:
            problems.append(f'这些错误 handler 没有 JSON 分叉，落在 /api/v1 上会吐 HTML：{unwired}')
        need = {400, 401, 403, 404, 405, 429, 500}
        missing = sorted(need - set(handlers))
        if missing:
            problems.append(f'缺少这些状态码的 handler（API 上它们会回 Werkzeug 默认 HTML 页）：{missing}')
        if not unwired:
            evidence.append(f'errorhandler {len(handlers)} 条，全部经 api_error_or/json_error 分叉')
        else:
            evidence.append(f'errorhandler {len(handlers)} 条（有未分叉的已在上面判红）')

    # ③ spec ⇄ 代码：enum 与码表逐项相等；Error 属性覆盖四键；429 必须复用同一 schema
    spec_doc, yaml_err = _s33_load_yaml(texts['openapi.yaml'])
    if yaml_err:
        problems.append(yaml_err)
        record('BLOCK', '34. 对外错误契约（JSON 错误面接线 + 机读码与 spec 双向对账）', False, problems)
        return
    schemas = (spec_doc.get('components') or {}).get('schemas') or {}
    err_schema = schemas.get('Error') or {}
    props = (err_schema.get('properties') or {})
    enum = (props.get('error') or {}).get('enum')
    if not isinstance(enum, list) or not enum:
        problems.append('spec 的 Error.error 没有非空 enum（无清单可对账 = 本项失去意义，判没测到）')
    elif codes:
        miss_doc = sorted(set(codes) - set(enum))
        ghost_doc = sorted(set(enum) - set(codes))
        if miss_doc:
            problems.append(f'代码有、spec enum 未声明的错误码：{miss_doc}')
        if ghost_doc:
            problems.append(f'spec enum 声明了、代码永不返回的错误码：{ghost_doc}')
        if not miss_doc and not ghost_doc:
            evidence.append(f'机读码双向相等：代码 {len(codes)} ⇄ spec enum {len(enum)}')
    lack_keys = [k for k in schema_keys if k not in props]
    if lack_keys:
        problems.append(f'Error schema 缺这些恒定键的声明：{lack_keys}')
    # 组件级错误响应必须复用同一份 Error schema。判据按**整组**枚举而不是只点 429 ——
    # 本轮变异实测：只盯 RateLimited 时，把 Forbidden 换成内联副本仍判绿，
    # 说明"点名单例"的判据天生漏同类出口（同一契约两处各写一份）。
    comps = ((spec_doc.get('components') or {}).get('responses') or {})
    inline = []
    for rname in sorted(comps):
        sch = ((((comps[rname] or {}).get('content') or {})
                .get('application/json') or {}).get('schema')) or {}
        if sch.get('$ref') != '#/components/schemas/Error':
            inline.append(f'{rname}({sorted(sch) or "空"})')
    if inline:
        problems.append('这些组件级错误响应没有复用统一 Error schema（内联副本=各写一份）：'
                        + ', '.join(inline))
    elif comps:
        evidence.append(f'{len(comps)} 条组件级错误响应全部复用同一 Error schema')
    else:
        problems.append('spec 里没有 components.responses（错误体无处声明，判没测到）')
    m = re.search(r"^API_PREFIX\s*=\s*'([^']+)'", texts['src/api_errors.py'], re.M)
    bp = re.search(r"url_prefix\s*=\s*'([^']+)'", texts['src/api_open.py'])
    if not m or not bp:
        problems.append('取不到 API_PREFIX 或 Blueprint url_prefix（前缀分界的两条腿断了一条）')
    elif m.group(1).rstrip('/') != bp.group(1).rstrip('/'):
        problems.append(f'API_PREFIX={m.group(1)!r} 与 blueprint url_prefix={bp.group(1)!r} '
                        '不一致（改了前缀没改分界，所有 API 错误都会退回 HTML）')
    else:
        evidence.append(f'前缀分界一致：{m.group(1)} ⇄ url_prefix {bp.group(1)}')

    record('BLOCK', '34. 对外错误契约（JSON 错误面接线 + 机读码与 spec 双向对账）',
           not problems, problems or evidence)


# ══════════════ 37. 可维护性预算：规模与复杂度双向棘轮（BLOCK）══════════════
BUDGET_FILE = 'MAINTENANCE_BUDGET.json'
# 预算允许的上浮余量：预算 ∈ [实测, 实测×(1+SLACK)]。
# 只设下界会把判据变成"每次改代码都得改预算"的噪声闸；只认"不超实测"又会逼出无意义抖动，
# 10% 是"允许正常增删、不允许悄悄长大"的折中，写死并在此项输出里显式回显。
BUDGET_SLACK = 0.10


def _s37_py_files():
    """枚举纳入预算的范围：`src/**.py` + 根级交付脚本（与第 10/19 项同源，不另起一把尺）。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(SRC, '.')):
        dirnames[:] = [d for d in dirnames if d not in ('__pycache__', 'static', 'templates',
                                                       'industry_packs', 'narrative')]
        for fn in filenames:
            if fn.endswith('.py'):
                full = os.path.join(dirpath, fn)
                out.append(os.path.relpath(full, ROOT).replace(os.sep, '/'))
    for name in ROOT_DELIVERED:
        p = os.path.join(ROOT, name)
        if name.endswith('.py') and os.path.isfile(p):
            out.append(name)
    return sorted(set(out))


def _s37_c901_count():
    """用 ruff 现量 C901（ mccabe 复杂度 >10）违规数；ruff 不可用返回 None（该项判红，不跳过）。"""
    cmd = find_ruff()
    if not cmd:
        return None, '未探测到 ruff'
    r = subprocess.run([*cmd, 'check', '--isolated', '--select', 'C901', '--output-format', 'concise',
                        '--config', 'lint.mccabe.max-complexity=10', 'src'],
                       cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout or '') + (r.stderr or '')
    n = len([ln for ln in out.splitlines() if re.search(r'\bC901\b', ln)])
    return n, None


def write_budget(reason=''):
    """`--write-budget` 的唯一写入口：按实测生成预算表，只允许收紧或持平。

    放宽必须带 reason（人工决定，把"悄悄长大"变成一次显式动作），这正是棘轮的成本可见化。
    """
    path = os.path.join(ROOT, BUDGET_FILE)
    old = {}
    if os.path.isfile(path):
        try:
            old = json.loads(read(path)).get('files', {})
        except json.JSONDecodeError:
            old = {}
    files = {}
    loosened = []
    for rel in _s37_py_files():
        actual = len(read(os.path.join(ROOT, *rel.split('/'))).splitlines())
        cap = int(math.ceil(actual * (1 + BUDGET_SLACK)))
        prev = old.get(rel)
        if prev is not None and cap > prev:
            if not reason:
                # 无 reason 不得放宽：沿用旧预算，让第 37 项在真实超长处判红——
                # 放宽是一次人工决定，不是一个默认行为。
                loosened.append(f'{rel}: {prev}→{cap}')
                cap = prev
        files[rel] = cap
    c901, err = _s37_c901_count()
    doc = {
        'generated_by': 'quality_gate.py --write-budget',
        'slack': BUDGET_SLACK,
        'files': files,
        'complexity': {'c901_baseline': c901 if c901 is not None else 0,
                       'ruff_probe': ('unavailable' if err else 'ok'),
                       'reason': reason},
    }
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write('\n')
    print(f'✅ 预算表已生成：{path}（{len(files)} 个文件，C901 基线 {c901}）')
    if loosened:
        print('⚠️ 以下文件实测比旧预算更大，未带 --reason 故按旧预算保留（会判红，属预期）：'
              + ', '.join(loosened[:6]))
    return 0


def check_maintenance_budget():
    """规模与复杂度只准变小、不准悄悄长大（第 24 轮 H53/H54）。

    动因（当场实测三条）：
    1. `grep 行数|规模预算|complexity|C901` 在本门禁全文 **0 命中** —— 36 项里没有任何一项看文件尺寸；
    2. `wc -l`：`test_l4_assertions.py` **7,810 行**、`quality_gate.py` 2,882、`src/dify_client.py` 1,893；
       本会话已在该巨石上付过一次代价（`ruff --select I001 --fix` 一次改 39 处 / +357 行 churn）；
    3. `ruff --isolated --select C901 --config lint.mccabe.max-complexity=10 src` = **17 个函数超限**，
       而第 7 项 ruff 是 WARN 级、只 lint `src`，src 里数百条历史告警无人清 ⇒ 复杂度看得见但不拦。
    上游对照：Airflow 的 lint exceptions 清单只减不增、Superset/Airflow 把覆盖率与规模门槛放进 CI、
    PostHog 按 app 拆测试文件。本轮**不做**巨石拆分（一次移动 7.8k 行会同时打穿第 10 项字节比对、
    第 29 项台账与 L4 计数口径，PH6 冻结窗口不冒这个险），只立"不许再长"的棘轮把拆分变成可分期工程。
    """
    problems, evidence = [], []
    path = os.path.join(ROOT, BUDGET_FILE)
    if not os.path.isfile(path):
        record('BLOCK', '37. 可维护性预算（单文件规模 + 复杂度只降不升）', False,
               [f'缺 {BUDGET_FILE}：跑 `python quality_gate.py --write-budget` 生成'
                '（判"没测到"不判通过）'])
        return
    try:
        doc = json.loads(read(path))
    except json.JSONDecodeError as exc:
        record('BLOCK', '37. 可维护性预算（单文件规模 + 复杂度只降不升）', False,
               [f'{BUDGET_FILE} 不是合法 JSON：{exc}'])
        return
    budget = doc.get('files') or {}
    actual = {}
    for rel in _s37_py_files():
        actual[rel] = len(read(os.path.join(ROOT, *rel.split('/'))).splitlines())
    if not budget or not actual:
        problems.append(f'枚举器空转：预算 {len(budget)} 条 / 实测 {len(actual)} 个文件')
    else:
        unlisted = sorted(set(actual) - set(budget))
        ghosts = sorted(set(budget) - set(actual))
        if unlisted:
            problems.append(f'这些 .py 没有预算条目（新增文件即免检，判据立刻失效）：{unlisted[:6]}')
        if ghosts:
            problems.append(f'预算表里有已不存在的文件（表没随代码更新）：{ghosts[:6]}')
        grew = [f'{rel}(实测 {actual[rel]} > 预算 {budget[rel]})'
                for rel in sorted(set(actual) & set(budget)) if actual[rel] > budget[rel]]
        too_loose = [f'{rel}(预算 {budget[rel]} > 实测 {actual[rel]}×1.1)'
                     for rel in sorted(set(actual) & set(budget))
                     if budget[rel] > int(math.ceil(actual[rel] * (1 + BUDGET_SLACK)))]
        if grew:
            problems.append('超出预算（只准拆分或带 --reason 显式上调，不许默认放行）：'
                            + '; '.join(grew[:6]))
        if too_loose:
            problems.append('预算比实测宽松超过 10%（把预算写成天文数字 = 买通判据）：'
                            + '; '.join(too_loose[:6]))
        if not grew and not too_loose:
            evidence.append(f'{len(actual)} 个文件全部在预算内；余量上限 10%')

    c901, err = _s37_c901_count()
    cx = doc.get('complexity') or {}
    base = cx.get('c901_baseline')
    if err:
        problems.append(f'ruff 不可用（{err}）⇒ 复杂度腿无法复算，判红不判通过')
    elif base is None:
        problems.append('预算表缺 complexity.c901_baseline（历史欠账没有账本 = 永远不清）')
    else:
        if c901 > base:
            problems.append(f'C901 超限函数从基线 {base} 涨到 {c901}（新增复杂函数必须拆，'
                            '或把老的先清掉再改基线）')
        elif c901 < base:
            problems.append(f'C901 已降到 {c901}，但基线仍写 {base} ⇒ 台账失真：'
                            '重跑 --write-budget 下调基线（只降不升）')
        else:
            evidence.append(f'C901 超限函数 {c901} 个 = 基线（重构掉必须同步下调）')

    record('BLOCK', '37. 可维护性预算（单文件规模 + 复杂度只降不升）',
           not problems, problems or evidence)


# ══════════════ 38. 备份证据链（生产者接线 + 复算只读 + 自检腿数对账）══════════════
def _s38_trees():
    """解析纳入范围的 .py（跳过项显式列出，绝不静默少算）。"""
    trees, skipped = {}, []
    for rel in _s37_py_files():
        try:
            trees[rel] = ast.parse(read(os.path.join(ROOT, *rel.split('/'))))
        except SyntaxError as exc:
            skipped.append(f'{rel}:{exc.lineno}')
    return trees, skipped


def _s38_backup_producers(trees):
    """AST 枚举「调用 SQLite 在线备份 API」的模块 —— 新加一条出口会自动纳入，不靠手抄名单。

    谓词按**语义**取而不是按变量名形状取：`conn.backup(target)` 与 `source.backup(target)`
    是同一条 API 的两次调用，谁都必须留证。上一版本要求接收者名字以 `conn` 结尾，
    结果只数到 1 条出口（`source.backup(target)` 被漏掉）——这条判据的 `≥2` 下限当场把它逮住了。
    """
    out = set()
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'backup' and len(node.args) == 1
                    and not node.keywords):
                out.add(rel)
    return out


def _s38_manifest_writers(trees):
    """AST 枚举「调用 write_manifest」的模块（同样不看注释与字符串）。"""
    out = set()
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (isinstance(f, ast.Name) and f.id == 'write_manifest') or \
               (isinstance(f, ast.Attribute) and f.attr == 'write_manifest'):
                out.add(rel)
    return out


def _s38_st_legs(tree):
    """从模块级 `SELFTESTS = (...)` 里取自检腿名单（语义枚举，不按 `_st_` 前缀猜形状）。

    前缀匹配会和夹具函数（`_st_make_db` / `_st_dir` / `_st_empty_db`）混在一起，
    于是"改个名就不再是腿"这种漂移看不见；名单住在一个赋值里，就照那个赋值取。
    """
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    declared = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'SELFTESTS' for t in node.targets):
            if isinstance(node.value, (ast.Tuple, ast.List)):
                declared = [e.id for e in node.value.elts if isinstance(e, ast.Name)]
    return sorted(defined), sorted(declared)


# 按名字钉死的下限：删掉任何一条腿都必须显式在这里删，而这里删了就判红
# （计数下限挡不住"连函数带元组一起删"——两边会自洽地一起变少）
S38_REQUIRED_LEGS = (
    '_st_snapshot_writes_manifest',   # 备份产出证据件
    '_st_manifest_shape_exact',       # 形状＝声明的键集（定义⇄声明对账）
    '_st_row_parity_recorded',        # 源⇄副本行数核对被如实记录
    '_st_source_not_mutated',         # 复算/备份不得改写源库
    '_st_verify_requires_manifest',   # 无证据件 ⇒ 判不可恢复
    '_st_empty_table_guard',          # 0 表 ⇒ 拦（本轮案发形态）
    '_st_zero_rows_guard',            # 有表但关键表全 0 行 ⇒ 拦
    '_st_backfill_not_accepted',      # 回填态不得追认
    '_st_tamper_detected',            # 留证后被改写 ⇒ 拦
    '_st_pruned_recorded',            # 保留动作本身可追责
)


def check_backup_evidence_chain():
    """备份必须留下可复算的证据件；复算工具自己不得持有写权限（第 25 轮 H57/H58）。

    动因（当场实测三条，不引用旧结论）：
    1. 负对照：`python backup_db.py --verify` 对磁盘那份 **4,096 B / 0 表 / 0 行** 的
       `demo-reset_20260922_001606_340670.db` 输出「✅ 可恢复 … integrity_check ok」且 rc=0
       —— `PRAGMA integrity_check` 对空库照样返回 ok，字节层完好从来不等价于"里面有数据"；
    2. 只读普查：开发副本 12 份 + 交付副本 10 份 = **22 份 `.db`**，表数 5/8/13/14/16/17 各不相同，
       而证据件 **0 份**；`grep manifest|sidecar` 在本门禁全文 **0 命中** ⇒ 36 项里没一项看备份目录；
    3. 两条出口口径不一：`backup_db` 第 21 轮已立"只读源 + 行数核对 + 原库未变守卫"，
       而 `src/pack_demo.create_database_backup()`（平台重置页那条）**只查 integrity**、
       源库还是读写打开 ⇒ 同类出口漏修。
    上游规格（同轮 `gh api contents` 取回）：barman `BackupInfo` 把 `status/error` 写在每张备份卡上
    （成败落在**物件**上而不是 stdout）；pgbackrest `ManifestFile` 分记 `size/sizeOriginal/sizeRepo`
    （因此能回答"留证之后有没有被动过"）；restic `checker.go` 的 `TreeError{ID,...}` 要求错误点名对象。
    """
    problems, evidence = [], []
    trees, skipped = _s38_trees()
    if skipped:
        problems.append(f'这些 .py 解析失败（枚举范围不完整，不许静默少算）：{skipped[:5]}')

    # ① 生产者接线：任何用在线备份 API 的模块，都必须是 write_manifest 的调用方
    producers = _s38_backup_producers(trees)
    writers = _s38_manifest_writers(trees)
    if not producers:
        problems.append('一条备份出口都没枚举到（备份 API 改名或被删？判"没测到"不判通过）')
    else:
        unwired = sorted(producers - writers)
        if unwired:
            problems.append(f'这些模块会做库备份却不写证据件（同类出口只修一半）：{unwired}')
        else:
            evidence.append(f'备份出口 {len(producers)} 条全部接线证据件写入：{sorted(producers)}')
        if len(producers) < 2:
            problems.append(f'备份出口只找到 {len(producers)} 条（本机实装有 backup_db 与 pack_demo 两条，'
                            '少一条=枚举器或文件范围被改动）')

    # ② 复算工具只读：backup_manifest 里每个 sqlite3.connect 都必须带 uri=True
    bm_rel = 'src/backup_manifest.py'
    if bm_rel not in trees:
        problems.append(f'{bm_rel} 不在解析范围内或已不存在（判据失去输入）')
    else:
        rw = []
        for node in ast.walk(trees[bm_rel]):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'connect'):
                uri_true = any(k.arg == 'uri' and isinstance(k.value, ast.Constant)
                               and k.value.value is True for k in node.keywords)
                if not uri_true:
                    rw.append(f'行{node.lineno}')
        if rw:
            problems.append('复算工具出现可写的 sqlite3.connect（复验不该有写被证物的能力）：'
                            + ', '.join(rw[:6]))
        else:
            evidence.append(f'{bm_rel} 内全部 connect 均为 uri=True 只读')
        consts = {t.id for n in trees[bm_rel].body if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Name)}
        need = {'MANIFEST_KEYS', 'SOURCE_KEYS', 'CONTENT_KEYS', 'CHECK_KEYS', 'KEY_TABLES'}
        if need - consts:
            problems.append(f'形状声明缺失：{sorted(need - consts)}')
        else:
            evidence.append('证据件形状四键集 + 关键表名单均已声明')

    # ③ 自检腿对账：门禁从 SELFTESTS 取的名单 ⇄ 被测脚本自己报回的总数（双向都要咬得上）
    if 'backup_db.py' not in trees:
        problems.append('backup_db.py 不在范围内，自检腿对账无从进行')
        defined, declared = [], []
    else:
        defined, declared = _s38_st_legs(trees['backup_db.py'])
    if not declared:
        problems.append('backup_db.py 里没有 SELFTESTS 名单（腿的权威清单住在被删的那一行 ⇒ 判据空转）')
    dangling = sorted(set(declared) - set(defined))
    if dangling:
        problems.append(f'SELFTESTS 引用了不存在的腿（删函数留名单）：{dangling[:5]}')
    unlisted = sorted(n for n in defined if n.startswith('_st_') and n not in declared
                      and n not in ('_st_make_db', '_st_dir', '_st_empty_db'))
    if unlisted:
        problems.append(f'这些自检腿定义了却没进名单（写了不跑=没有）：{unlisted[:5]}')
    lost = sorted(set(S38_REQUIRED_LEGS) - set(declared))
    if lost:
        problems.append(f'必守的自检腿被删除（删腿=拆判据，须连同这里的名单一起显式删）：{lost[:5]}')
    r = subprocess.run([PY, 'backup_db.py', '--selftest'], cwd=ROOT,
                       capture_output=True, text=True)
    m = re.search(r'SELFTEST (\w+) legs=(\d+)/(\d+)', r.stdout or '')
    if not m:
        problems.append('backup_db.py --selftest 没有输出结论行（rc=' + str(r.returncode)
                        + '，stderr 尾：' + (r.stderr or '')[-120:] + '）')
    else:
        verdict, got, total = m.group(1), int(m.group(2)), int(m.group(3))
        if verdict != 'ok' or got != total:
            problems.append(f'自检未全过：legs={got}/{total} verdict={verdict}')
        if total != len(declared):
            problems.append(f'自检腿数与门禁 AST 数出不一致：脚本报 {total}，名单 {len(declared)} 条'
                            '⇒ 有腿被改名/删除/短路')
        if total < 8:
            problems.append(f'自检腿仅 {total} 条（下限 8）：判据覆盖面被削')
        else:
            evidence.append(f'自检 {total} 腿全过（与 SELFTESTS 名单一致，必守 {len(S38_REQUIRED_LEGS)} 条在册）')
        # 逐腿回执：分母不许只由脚本自报，必须数出每条腿各自的通过行（防"总数对、腿没跑"）
        printed = re.findall(r'^\[([A-Za-z0-9_]+)\] (PASS|FAIL|OK|SKIP)', r.stdout or '', re.M)
        names = [n for n, _ in printed]
        if len(names) != total:
            problems.append(f'自报 legs={total} 但逐腿回执只有 {len(names)} 条 ⇒ 有腿被静默跳过')
        missing_receipt = sorted(set(declared) - set(names))
        if missing_receipt:
            problems.append(f'这些腿没有任何回执（写了却没跑）：{missing_receipt[:5]}')
        skipped = sorted({n for n, s in printed if s != 'PASS'})
        if skipped:
            problems.append(f'这些腿的回执不是 PASS（SKIP 不算通过）：{skipped[:5]}')
        if r.returncode != 0:
            problems.append(f'--selftest 退出码非零（rc={r.returncode}）—— 结论行不许代替 rc')

    # ④ 本机备份目录只做可见性报告（历史件无证据件会立刻 22 红 ⇒ 先量误报率再接线）
    real_dir = os.path.join(ROOT, 'backups')
    if os.path.isdir(real_dir):
        dbs = [f for f in os.listdir(real_dir) if f.endswith('.db')]
        nomanifest = [f for f in dbs if not os.path.isfile(os.path.join(real_dir, f + '.manifest.json'))]
        empty_ok = [f for f in nomanifest
                    if os.path.getsize(os.path.join(real_dir, f)) <= 8192]
        evidence.append(f'本机 backups/ 共 {len(dbs)} 份 .db，其中 {len(nomanifest)} 份无证据件'
                        f'（≤8 KB 的 0 表形态 {len(empty_ok)} 份）—— 只报告不阻断，'
                        '补证用 `python backup_db.py --stamp <文件>`，回填态不得当可恢复凭据')

    record('BLOCK', '38. 备份证据链（生产者接线 + 复算只读 + 自检腿数对账）',
           not problems, problems or evidence)


# ══════════════ 40. 变更归因链与破坏性写入纳管（BLOCK）══════════════
_S40_MODULE = 'src/change_log.py'
# 扫描面 = 生产写路径。测试脚本删的是自己进程私有临时库里的行，快照脚本写的是演示库，
# 而 quality_gate.py 自己内含 '.silenced()' 这个**字符串字面量** —— 度量工具不得量自己
# （第 24 轮 S30 的「按语义集合求交时排除分母自身」同一族）。排除不是豁免：名单在本项
# 回执里逐条打印，扩面时看得见。
_S40_IN_SCOPE = ('init_db.py', 'backup_db.py')


def _s40_scope(trees):
    inside, outside = {}, []
    for rel, tree in trees.items():
        if rel.startswith('src/') or rel in _S40_IN_SCOPE:
            inside[rel] = tree
        else:
            outside.append(rel)
    return inside, outside


def _s40_class_columns(tree):
    """{表名: {列名}}：只认 `X = db.Column(...)` 形态，其他成员一律不算列。"""
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        table = None
        cols = set()
        for st in node.body:
            if not isinstance(st, ast.Assign):
                continue
            names = [t.id for t in st.targets if isinstance(t, ast.Name)]
            if '__tablename__' in names and isinstance(st.value, ast.Constant)                     and isinstance(st.value.value, str):
                table = st.value.value
            if isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Attribute)                     and st.value.func.attr == 'Column':
                cols.update(names)
        if table is not None:
            out[table] = cols
    return out


def _s40_roster(tree):
    """取 src/change_log.py 的三档名册。只认顶层赋值，不按变量命名前缀猜形状。"""
    roster = {'TRACKED': {}, 'REDACTED_TABLES': [], 'UNTRACKED': [], 'FORBIDDEN_FIELDS': []}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if 'TRACKED' in names and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and isinstance(v, (ast.Tuple, ast.List)):
                    roster['TRACKED'][k.value] = [
                        e.value for e in v.elts if isinstance(e, ast.Constant)]
        elif names & set(roster):
            key = sorted(names & set(roster))[0]
            if isinstance(node.value, ast.Dict):      # UNTRACKED：键名即表名，值是不跟踪理由
                roster[key] = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
            elif isinstance(node.value, (ast.Tuple, ast.List)):
                roster[key] = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    return roster


def _s40_call_is_bulk_delete(node):
    """识别 `Model.query...delete()` / `session.query(Model)...delete()` 这类绕开监听器的删。"""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr != 'delete':
        return False
    return '.query' in ast.unparse(node.func.value)


def _s40_class_tables(tree):
    """{ORM 类名: __tablename__}：把 `DailySales.query...delete()` 映射到 `daily_sales`。"""
    out = {}
    if tree is None:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for st in node.body:
            if not isinstance(st, ast.Assign):
                continue
            names = [t.id for t in st.targets if isinstance(t, ast.Name)]
            if '__tablename__' in names and isinstance(st.value, ast.Constant) \
                    and isinstance(st.value.value, str):
                out[node.name] = st.value.value
    return out


def _s40_delete_site_entities(node):
    """该函数内**每个**批量删除站点的目标实体（ORM 类名；解析不到记 None）。

    量纲是「删除站点」而不是「函数」：一个函数里删两张表，就必须留两条痕 ——
    按函数做子串匹配时，只要还剩任意一条 `change_log.` 字样就判绿，
    第 26 轮的这条判据因此对「拆掉其中一条配对留痕」永久不红（第 28 轮 L4 实测）。
    """
    sites = []
    for sub in ast.walk(node):
        if not _s40_call_is_bulk_delete(sub):
            continue
        head = ast.unparse(sub.func.value).split('.', 1)[0].strip()
        sites.append(head if head[:1].isupper() else None)
    return sites


def _s40_note_entities(node):
    """该函数内聚合留痕声明的实体：`note_bulk('tbl', …)` / `purge(Model, …)`。

    返回 (实体集合, 是否含动态实体)。动态实体（`note_bulk(name, …)`）无法按字面核对，
    调用侧退化成条数对账，不因此判绿也不因此判红。
    """
    ents, dynamic = set(), False
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fname = _gate_deco_name(sub.func)
        if fname == 'note_bulk' and sub.args:
            first = sub.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                ents.add(first.value)
            else:
                dynamic = True
        elif fname == 'purge' and sub.args:
            first = sub.args[0]
            if isinstance(first, ast.Name):
                ents.add(first.id)
            else:
                dynamic = True
    return ents, dynamic


def _s40_pairing_problems(func, rel, table_map):
    """逐站点配对核：删了哪张表，就得有那张表的聚合痕。"""
    sites = _s40_delete_site_entities(func)
    if not sites:
        return []
    ents, dynamic = _s40_note_entities(func)
    noted = {table_map.get(e, e) for e in ents}
    problems = []
    literal = [s for s in sites if s]
    for site in literal:
        if table_map.get(site, site.lower()) not in noted:
            problems.append(f'{rel}:{func.name} 删了 {site} 却没有该表的聚合留痕')
    if len(literal) != len(sites):                       # 有解析不出实体的站点
        note_calls = len(ents) + (1 if dynamic else 0)
        if note_calls < len(sites):
            problems.append(f'{rel}:{func.name} 批量删 {len(sites)} 处只配 {note_calls} 条聚合痕'
                            '（条数对不上，动态实体站点无法逐张核对）')
    return problems


def _s40_bulk_delete_funcs(tree, path):
    """返回 (函数名, 文件) 清单：含批量删除的函数。模块级（非函数内）的算 '__module__'。"""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            body = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
            if any(_s40_call_is_bulk_delete(c) for c in body) and 'purge' not in ast.unparse(node):
                hits.append((path, node.name, ast.unparse(node)))
    return hits


def _s40_silenced_funcs(tree, path):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            src = ast.unparse(node)
            if '.silenced()' in src:
                out.append((path, node.name, src))
    return out


def _s40_cost_binding(expr):
    """`df.assign(_cost=A if C else B)` 里真正读的那一列 = 三元式的 **body 分支**。

    整个表达式一起做子串匹配会被条件本身骗过：把 `_cost=df['product_cost']` 改成
    `_cost=df['total_revenue'] if 'product_cost' in df.columns` 之后，"product_cost"
    这几个字还在，取值却已经换列了 —— 只看子串就判绿（本轮变异实测到）。
    """
    node = expr
    while isinstance(node, ast.IfExp):
        node = node.body
    return ast.unparse(node)


def _s40_wiring_problems(trees):
    problems = []
    # 接线判据只认 **Call 节点**：`ast.unparse` 会保留 docstring 里的字面量，
    # 用整文件子串匹配就会被一句说明文字骗成"已装配"（第 20/25 轮同款教训）。
    app_tree = trees.get('src/app.py')
    wired = bool(app_tree) and any(
        isinstance(n, ast.Call) and ast.unparse(n).strip() == 'change_log.init_app(app)'
        for n in ast.walk(app_tree))
    if not wired:
        problems.append('src/app.py 未装配 change_log.init_app(app) —— 监听器没挂上，'
                        '名册再全也是一行不写')
    guard = trees.get('src/query_guard.py')
    if guard is None:
        problems.append('src/query_guard.py 读不到，无法核对 ChangeLog 是否纳管')
    else:
        store_models = ''
        for node in ast.walk(guard):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == 'store_dim_models' for t in node.targets):
                store_models = ast.unparse(node.value)
        if 'm.ChangeLog' not in store_models:
            problems.append('ChangeLog 不在 query_guard 的 store_dim_models 里 —— '
                            '审计表带着商家/门店列却不受守卫 = 跨租户可读')
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ('delete', 'update') \
                    and 'ChangeLog' in ast.unparse(node.func.value):
                problems.append(f'{rel} 出现对 change_log 的 {"删除" if node.func.attr == "delete" else "改写"}'
                                ' 调用 —— 审计表必须 append-only（Saleor events 同一姿态）')
    h5 = trees.get('src/routes/h5.py')
    if h5 is None:
        problems.append('src/routes/h5.py 读不到，无法核对成本快照写入点')
    else:
        writes = [ast.unparse(n) for n in ast.walk(h5)
                  if isinstance(n, ast.Call) and ast.unparse(n.func).strip() == 'DailySales']
        if not writes:
            problems.append('h5 里找不到 DailySales(...) 构造点（改名了？判"没测到"不判通过）')
        else:
            missing = [kw for kw in ('product_cost=', 'cost_basis=') if kw not in writes[0]]
            if missing:
                problems.append(f'流水写入点缺成本快照参数 {missing} —— 新写点不带快照，毛利率又会回查当前值')
    an = trees.get('src/analytics.py')
    if an is not None:
        fn = next((n for n in ast.walk(an)
                   if isinstance(n, ast.FunctionDef) and n.name == '_compute_gross_margin'), None)
        if fn is None:
            problems.append('src/analytics.py 里找不到 _compute_gross_margin（改名会让本判据空转）')
        else:
            body = ast.unparse(fn)
            # 只查"字还在"会被 `df['product_cost']` 留在别处、而 `_cost` 实际改取
            # total_revenue 的变异放过 —— 必须钉在**那个赋值**上：
            # `df.assign(_cost=<这里必须是 product_cost>)`。
            bound = ''
            for call in ast.walk(fn):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)                         and call.func.attr == 'assign':
                    for kw in call.keywords:
                        if kw.arg == '_cost':
                            bound = _s40_cost_binding(kw.value)
            if not bound:
                problems.append('_compute_gross_margin 里找不到 df.assign(_cost=...) —— '
                                '改写法会让本判据空转，宁可可红不可假绿')
            elif "df['product_cost']" not in bound or 'cost_basis' not in body:
                problems.append(f'_compute_gross_margin 的 _cost 不再是行内成本快照'
                                f'（实得 {bound[:60]}）—— 退回"按产品名回查目录当前值"，'
                                '实测一次改名就能把历史毛利率挪 +1.51 个百分点')
    else:
        problems.append('src/analytics.py 读不到')
    return problems


def _s40_delete_problems(trees):
    """破坏性写入出口逐条核：批量删必须留聚合痕（**按删除站点逐条**），静默档必须配对聚合行。"""
    problems, evidence = [], []
    bulk, silenced = [], []
    scoped, outside = _s40_scope(trees)
    table_map = _s40_class_tables(trees.get('src/models.py'))
    paired = []
    for rel, tree in sorted(scoped.items()):
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if _s40_delete_site_entities(func):
                paired += _s40_pairing_problems(func, rel, table_map)
        bulk += _s40_bulk_delete_funcs(tree, rel)
        silenced += _s40_silenced_funcs(tree, rel)
    if not bulk and not silenced:
        problems.append('一处批量删除都没枚举到（全仓不可能零批量删 —— 判"没测到"，不判通过）')
    if not table_map:
        problems.append('src/models.py 读不到 ORM 类名⇄表名映射 ⇒ 逐站点配对退化为条数对账，'
                        '按"判据空转"处理不判过')
    problems += sorted(set(paired))
    unpaired = [f'{p}:{name}' for p, name, src in bulk if 'change_log.' not in src]
    if unpaired:
        problems.append(f'这些批量删除不经 change_log.purge()/留聚合痕（绕开监听器=删了无人知道）：{unpaired}')
    mute = [f'{p}:{name}' for p, name, src in silenced
            if 'note_bulk' not in src and 'purge' not in src]
    if mute:
        problems.append(f'这些函数关掉了逐行归因却不配对冲聚合行（静默 = 审计黑洞）：{mute}')
    evidence.append(f'扫描面：生产写路径 {len(scoped)} 个文件；批量删除函数 {len(bulk)} 处'
                    f'（按删除站点逐条配对，站点级违例 {len(set(paired))} 条）、'
                    f'silenced() 静默档 {len(silenced)} 处，配对检查'
                    f'{"通过" if not (unpaired or mute or paired) else "未通过"}；'
                    f'面外文件 {len(outside)} 个（测试/取证/门禁自身，不静默省略）：'
                    f'{sorted(outside)[:6]}')
    return problems, evidence


def _s40_roster_problems(trees):
    problems, evidence = [], []
    cl = trees.get(_S40_MODULE)
    md = trees.get('src/models.py')
    if cl is None:
        return [f'{_S40_MODULE} 读不到或解析失败 —— 归因链整体缺席，不许按通过处理'], evidence
    if md is None:
        return ['src/models.py 读不到，无法核对名册分母'], evidence
    tables = _s40_class_columns(md)
    roster = _s40_roster(cl)
    covered = set(roster['TRACKED']) | set(roster['REDACTED_TABLES']) | set(roster['UNTRACKED'])
    if not tables:
        problems.append('models.py 一张表都没枚举到（分母为空 = 判据不会红，比漏登记更危险）')
    if not roster['TRACKED']:
        problems.append('change_log.TRACKED 名册为空 —— 归因链看着在，其实什么都不记')
    missing = sorted(set(tables) - covered)
    if missing:
        problems.append(f'这些表既没登记跟踪/脱敏，也没写"不跟踪+理由"（漏登记=静默不记）：{missing}')
    ghost = sorted(covered - set(tables))
    if ghost:
        problems.append(f'名册里的表在 models.py 不存在（对空气豁免）：{ghost}')
    banned = [f'{t}.{c}' for t, cols in roster['TRACKED'].items()
              for c in cols if c in roster['FORBIDDEN_FIELDS']]
    if banned:
        problems.append(f'凭据/业务明细列被声明记值（安全红线）：{banned}')
    # 红线清单本身要有地板：清空 FORBIDDEN_FIELDS 会让上面这条永远不命中 = 判据被买通
    _floor = {'password', 'token_hash', 'secret_salt', 'code_hash'}
    if not _floor <= set(roster['FORBIDDEN_FIELDS']):
        problems.append(f'禁录列地板失守（凭据列名从 FORBIDDEN_FIELDS 里消失，'
                        f'红线判据退化成空转）：缺 {sorted(_floor - set(roster["FORBIDDEN_FIELDS"]))}')
    ghost_cols = [f'{t}.{c}' for t, cols in roster['TRACKED'].items()
                  for c in cols if t in tables and c not in tables[t]]
    if ghost_cols:
        problems.append(f'名册里的列在 ORM 不存在（getattr 会静默记成 null）：{ghost_cols}')
    evidence.append(f'表 {len(tables)} 张，名册覆盖 {len(tables) - len(missing)} 张'
                    f'（值档 {len(roster["TRACKED"])}、脱敏档 {len(roster["REDACTED_TABLES"])}、'
                    f'不跟踪档 {len(roster["UNTRACKED"])}）')
    return problems, evidence


def _s42_val(node, names):
    """取字面值：常量直取；MODE_* 名字查表；隐式相邻字符串拼接已由 AST 合成一个 Constant。"""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple)):
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None


def _s42_model_tables():
    """静态取 models.py 里 `__tablename__` 与各类的列名（门禁自己数一遍，不借 erasure 的尺）。"""
    try:
        tree = ast.parse(read(os.path.join(ROOT, 'src', 'models.py')))
    except (SyntaxError, MemoryError):
        return {}, True
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        tab = None
        cols = []
        for st in node.body:
            if isinstance(st, ast.Assign) and isinstance(st.targets[0], ast.Name):
                nm = st.targets[0].id
                if nm == '__tablename__' and isinstance(st.value, ast.Constant):
                    tab = st.value.value
                elif isinstance(st.value, ast.Call) and nm != 'metadata':
                    cols.append(nm)
        if tab:
            out[tab] = set(cols)
    return out, False


def _s42_registry_ast():
    """取 erasure.py 的 OWNING / ERASE_ORDER 字面量（含 reason），不执行该模块。"""
    try:
        tree = ast.parse(read(os.path.join(ROOT, 'src', 'erasure.py')))
    except (SyntaxError, MemoryError):
        return None, None, None
    owning = order = phrase = None
    names = {}        # MODE_* 这类常量名 -> 字面值（归属档写的是名字不是字符串，不解析就全是 None）
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Name):
            continue
        nm = node.targets[0].id
        if isinstance(node.value, ast.Constant) and nm.isupper():
            names[nm] = node.value.value
        if nm == 'OWNING' and isinstance(node.value, ast.Dict):
            owning = {}
            for k, v in zip(node.value.keys, node.value.values):
                spec = {}
                if isinstance(v, ast.Dict):
                    for kk, vv in zip(v.keys, v.values):
                        if isinstance(kk, ast.Constant):
                            spec[kk.value] = _s42_val(vv, names)
                if isinstance(k, ast.Constant):
                    owning[k.value] = spec
        elif nm == 'ERASE_ORDER' and isinstance(node.value, (ast.Tuple, ast.List)):
            order = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
        elif nm == 'ERASE_CONFIRM_PHRASE' and isinstance(node.value, ast.Constant):
            phrase = node.value.value
    return owning, order, phrase


# ══════════════ 43. 业务日切与时区（BLOCK）══════════════
# 裸进程时钟的形态：这几种写法都"跟着服务器时区走"，在 UTC 机器上每天会有 8 小时算错业务日。
# 只认**尾部两段** ⇒ `date.today()` 与 `datetime.date.today()` 同判（合成样本实测：只看整串
# 相等时，别名/限定写法会整片漏判，判据看起来绿而分母是空的）。
_S43_NAIVE_TAIL = frozenset({'date.today()', 'datetime.now()', 'datetime.today()',
                             'time.localtime()', 'datetime.datetime.now()',
                             'datetime.datetime.today()'})


def _s43_chain(node):
    """把 `a.b.c()` 的函数链还原成 'a.b.c'。

    第 36 轮加固：base 不是 Name 时**照样返回尾段属性**。旧实现直接 `return ''`，
    于是 `__import__('datetime').datetime.now()`（本机第 35 轮真实写过的一行）整形逃逸 ——
    拿到模块对象的写法千奇百怪（`__import__` / `importlib.import_module` / 函数返回值 / 下标），
    逐个列名单是追不完的；而"裸进程时钟"的签名在**尾部那两个属性**上，与 base 是谁无关。
    """
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return '.'.join(reversed(parts))


def _s43_is_naive(node):
    """裸进程时钟调用 = **无参**且函数链尾部命中形台账。

    只按文本判会两头出错：`datetime.now(UTC).isoformat()` 整串尾部像 `datetime.now()`
    会被误判成裸调用（其实已显式 UTC），而 `datetime.date.today()` 这种别名写法早先按
    整串相等会整片漏判。⇒ 判定按 AST 节点（带参即非裸），报告用文本尾串。
    """
    if not isinstance(node, ast.Call) or node.args or node.keywords:
        return False
    chain = _s43_chain(node.func)
    if not chain:
        return False
    return '.'.join(chain.split('.')[-2:]) + '()' in _S43_NAIVE_TAIL


_S43_CAL_FILE = 'src/business_calendar.py'
_S43_DEPLOY_SCRIPT = '部署/pythonanywhere/one_click_deploy.py'
_S43_ENV_NAME = 'ICAN_BUSINESS_TZ'
# 反向地板：防"把 33 处业务日调用全删掉"来买通「裸点=0」。删调用比改调用更容易假装完成。
_S43_BUSINESS_FLOOR = 25
_S43_EXPLICIT_UTC_FLOOR = 40
# 允许保留的裸时钟点：键 = `文件::函数::调用`，值 = 为什么这里**应该**跟进程/系统时钟。
# 当前为空 —— 加一条就是声明一次决策，必须带 ≥20 字理由。
_S43_NAIVE_ALLOWED = {}


def _s43_trees():
    """取数面：与第 37/40 项同一份 py 清单，另把部署脚本纳进来（它写证据文件名时间戳）。"""
    rels = _s37_py_files()
    if os.path.isfile(os.path.join(ROOT, *_S43_DEPLOY_SCRIPT.split('/'))) and \
            _S43_DEPLOY_SCRIPT not in rels:
        rels = sorted(set(rels) | {_S43_DEPLOY_SCRIPT})
    trees, broken = {}, []
    for rel in rels:
        try:
            trees[rel] = ast.parse(read(os.path.join(ROOT, *rel.split('/'))))
        except (SyntaxError, MemoryError, UnicodeDecodeError) as exc:
            broken.append(f'{rel}:{type(exc).__name__}')
    return trees, broken


def _s43_fn_of(tree, lineno):
    """找某行所属的最内层函数名（模块级记 `<module>`）。"""
    best = '<module>'
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                getattr(node, 'lineno', 0) <= lineno <= getattr(node, 'end_lineno', 0):
            best = node.name
    return best


def _s43_clock_shapes(trees):
    """同时枚举三类时钟口径：裸进程时钟 / 业务日历调用 / 显式 UTC。缺任一类都说明尺没量到东西。"""
    naive, business, explicit = [], 0, 0
    for rel, tree in sorted(trees.items()):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            try:
                txt = ast.unparse(node)
            except Exception:                        # noqa: BLE001 unparse 失败按形态不计
                continue
            if _s43_is_naive(node):
                naive.append((rel, node.lineno, _s43_fn_of(tree, node.lineno), txt))
            elif txt.startswith('cal.business_today()') or txt.startswith('cal.business_now()') \
                    or txt.startswith('business_calendar.business_'):
                business += 1
            elif 'datetime.now(' in txt and ('UTC' in txt or 'timezone.utc' in txt):
                explicit += 1
    return naive, business, explicit


def _s43_shape_problems(trees):
    """业务日历自身的形状：函数齐、兜底表存在、describe 必须把"走了哪条路"回读出来。"""
    problems = []
    tree = trees.get(_S43_CAL_FILE)
    if tree is None:
        return [f'{_S43_CAL_FILE} 读不到或解析失败 —— 业务日历整体缺席，不许按通过处理'], []
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    need = {'business_today', 'business_now', 'describe', 'validate_config', 'tz_name'}
    if need - names:
        problems.append(f'业务日历缺必需函数：{sorted(need - names)}')
    tops = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets
            if isinstance(t, ast.Name)}
    if 'FIXED_OFFSET_FALLBACK' not in tops:
        problems.append('缺 FIXED_OFFSET_FALLBACK 声明表 ⇒ tz 数据库缺失时的降级无处声明（只能静默失败）')
    blob = ast.unparse(tree)
    for key in ('resolved_via', 'business_date'):
        if f"'{key}'" not in blob:
            problems.append(f'describe() 未回读 {key} ⇒ 线上问不到"今天算给哪个时区/走的哪条路"')
    if 'raise ValueError' not in blob:
        problems.append('业务日历未在配置非法处 raise ⇒ 配错会静默按进程时钟跑（InvenTree 同族判据失效）')
    return problems, [f'业务日历函数 {len(names & need)}/{len(need)} 齐备，降级表与可见性字段在册']


# ══════════════ 52. 三副本同步出口唯一 + 面来自声明 + 本地链已接线（BLOCK）══════════════
# 动因（2026-09-28 第 37 轮当场实测，且**否证了上一轮自己登记的 M112**）：
#   上一轮我写"仓内没有同步工具，收编 D:/tmp 那个一次性脚本"。实测发现仓内**早就有**
#   `tools/frontend_sync_check.py`（被跟踪、06-constraints 里写着"改静态资源后必须跑"），
#   只是它只覆盖 `src/ → deploy-free/src/` 一格；根级交付件与 05 副本它完全不看，
#   于是第 31—36 轮的交付链一直靠一个**不在仓里**的脚本补另一半。
#   同轮还抓到第二个错位：只读检查器 `check_delivery_drift.py` 已经在管"白名单外那批文件
#   三副本必须一致"（EXTRA_TRACKED 6 项），而可同步侧完全不知道它 —— **查得到、修不了**。
#
# 上游对照（本轮 `gh api` 实取原文，落 D:/tmp/bench37/）：
#   pre-commit `pre_commit/commands/install_uninstall.py:77 shutil.move(hook_path, legacy_path)`
#     + `:102-:108` 单一写出口 + `:111 打印落地路径` —— **换出口时旧出口被显式处置**，
#     不允许两个实现同时往一个位置写；本项的对偶就是"写者必须只有一个"。
#   InvenTree `tasks.py:1933 setup_test()` → `:2013-:2016 shutil.copytree(src, dst, dirs_exist_ok=True)`
#     之后紧跟 `validate_files` 校验 —— **复制完必须重新验**，不拿"我刚才复制了"当结论；
#     同一批动作全集中在 invoke task 里（`:680/:689/:710 …` 40+ 个 @task），没有第二处手工复制。
#   discourse `lib/backup_restore/backup_store.rb:36 delete_old` 前置 `:96 cleanup_allowed?`
#     —— 破坏性清理前要问"现在能不能删"（本仓 EXTRA 一律只报不删，同一取向）。

_S52_WRITER = 'tools/frontend_sync_check.py'
# 同步面的删除点名册（第 43 轮起，第 46 轮把**取数面**从"写死两个函数名"改成"全模块函数"）：
# `<外层函数>.<接收者>.<方法>` 形状，判据按调用点实扫双向对账——
# 漏登记＝旁路（新加删除点无人知晓），多登记＝对空气放行（豁免一个不存在的动作）。
# 一手（第 46 轮自曝）：旧枚举只走 `for fn in ('drop_extra',)`，于是第 44 轮就存在的
# `sync_lock.holder.unlink` **从未进过分母** —— 名册一直自称穷尽，其实只穷尽了它点名的那两个函数。
S52_DELETE_ROSTER = {
    'drop_extra.target.unlink': '唯一真删 EXTRA 的出口：一次一个，删的是凭据绑定的那一份副本',
    # 第 47 轮 M149：侧件的释放动作随 `sync_lock` 一起搬进了 `SyncLease`，键名跟着结构走
    # （名册锚的是"哪个函数删哪个对象"，不是"当年那个函数叫什么"）。
    'close.self.holder_path.unlink': '只删自己刚写的归因侧件（租约释放路径），不触被同步面',
    'renew.tmp.unlink': '心跳重写侧件失败时清掉**自己刚建的** .beat 半途件；原件与侧件都不动'
                        '（半途件不落盘 = 归档面不会长出无人认领的 .part）',
    'break_lock.stale_path.unlink': '唯一破锁出口：只删"归属查得清 + 持锁进程已死 + 锁龄到点"的锁与侧件',
    'prune_archive.target.unlink': '归档件唯一清理出口：过了保留窗口且台账查得回来历才删那一个',
}
_S52_MARKS = ('deploy-free', '05-源代码全文')
_S52_COPY = ('copy2', 'copyfile', 'copy', 'copytree')
_S52_ABS_RE = re.compile(r'^([A-Za-z]:[\\/]|/[A-Za-z0-9_.\-]+/)')
_S52_SEP_RE = re.compile(r'[\\/]')


def _s52_recv(node):
    """接收者标签：`x.unlink()` → `x`；`self.holder.unlink()` → `self.holder`。

    第 47 轮 M149 把侧件的释放动作从 `sync_lock` 搬进 `SyncLease.close()` 后，旧写法
    `getattr(call.func.value, 'id', '?')` 把 `self.holder_path.unlink()` 塌成了一个 `'?'`
    —— 名册的键形状是 `<函数>.<接收者>.<方法>`，接收者塌成问号等于**两处不同的删除点在名册里
    共用一个键**（今天 close 与 tomorrow 任何 `self.foo.unlink()` 都会撞在一起），
    可归因性就此丢掉。取不到的（如 `foo().unlink()`）显式标 `<unresolvable>` 而不是 '?'，
    让名册必须为它单独写一条理由。
    """
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return '.'.join(reversed(parts))
    return '<unresolvable>'


def _s52_delete_sites(tree):
    """同步工具里的删除点 = `<外层函数>.<接收者>.<方法>`，**分母取全模块的函数**。

    第 46 轮改这里的原因（自曝）：旧写法是 `for fn in ('drop_extra',)`，
    等于把"有哪些删除点"这个事实的分母交给**上一轮手写的函数名清单** ——
    于是 `sync_lock.holder.unlink`（第 44 轮就在）从未进过分母，名册自称穷尽而实际不穷尽。
    判据的取数面必须由被审对象自身结构给出，不接受手抄名单（同类：第 44 轮"写出口枚举只能打在模块级"）。
    第 47 轮再补一刀：接收者按**点号链**取，属性链接收者不得塌成同一个 '?'（见 `_s52_recv`）。
    """
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr in ('unlink', 'remove', 'rmtree')):
                out.add(f'{node.name}.{_s52_recv(call.func.value)}.{call.func.attr}')
    return sorted(out)


def _s52_docstring_nodes(tree):
    """把 Module/Class/Function 的首个字符串表达式（docstring）挑出来 —— 那是**描述**，不是输入。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, 'body', None)
            if body and isinstance(body[0], ast.Expr):
                val = body[0].value
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    out.add(id(val))
    return out



def _s52_trees():
    """取数面 = 第 43 项那份（src/** + 根级交付件 + 部署脚本）**并上 `tools/` 下的 *.py**。

    必须显式加 tools/：`_s37_py_files()` 历来只覆盖 src 与根级交付件，
    而被判的唯一合法写者恰好住在 `tools/` 里 —— 不加进来就是"面里没有合法写者，只有违规者"，
    判据会红得莫名其妙（本轮实测正是这样先红了一次）。
    """
    trees, broken = _s43_trees()
    tdir = os.path.join(ROOT, 'tools')
    for fn in sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []:
        rel = 'tools/' + fn
        if not fn.endswith('.py') or rel in trees:
            continue
        try:
            trees[rel] = ast.parse(read(os.path.join(ROOT, *rel.split('/'))))
        except (OSError, SyntaxError) as exc:
            broken.append(f'{rel}:{type(exc).__name__}')
    return trees, sorted(set(broken))


def _s52_writers(trees):
    """实测分母（本轮量得：全仓 copy* 调用 30 处，但**没有一处**在调用参数里字面出现镜像标记 ⇒
    "看调用点字符串"会写成一条恒绿判据。真正可判的形态是"该模块声明了镜像根 ∧ 它会复制"。）"""
    out = []
    for rel, tree in sorted(trees.items()):
        if rel.startswith('test_') or rel.startswith('memory/'):
            continue                                  # 测试夹具要在临时面里造镜像树，合法
        strs, has_copy = set(), False
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, 'attr', '') in _S52_COPY:
                has_copy = True
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                if any(m in n.value for m in _S52_MARKS):
                    strs.add(n.value[:40])
        if has_copy and strs:
            out.append((rel, sorted(strs)[:2]))
    return out


def _s52_offrepo_problems(src):
    """本地闸源码里的**绝对路径字面量**（真代码，不含 docstring）= 仓外依赖。

    判据只能匹配行为，不能匹配描述：本文件的注释里就写着 `D:/tmp/bench37/`，
    而 `verify_local` 的 docstring 也提到"不在仓里的一次性脚本" —— 那些都不算。
    只有落到 AST 常量节点（会被真传进 argv）的路径才判红。
    实测误报率：7 个根级在册脚本 0 命中；正例见 L4 S44 的合成样本。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [f'本地闸源码解析失败（{type(exc).__name__}）⇒ 仓外面无法判，不判通过']
    skip = _s52_docstring_nodes(tree)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip or not _S52_ABS_RE.match(node.value):
            continue
        if len([s for s in _S52_SEP_RE.split(node.value.strip('\\/')) if s]) >= 2:
            bad.append(f':{node.lineno} {node.value[:60]}')
    if bad:
        return [f'本地闸里出现仓外绝对路径（换机/CI 必断，且不受版本控制）：{bad[:4]}']
    return []


def _s52_face_problems(tool_path, gate_path, drift_path, carrier_path, declared):
    """面 ⇄ 三处声明 的包含关系（**按行为判**：真调用工具的 declared_files() 取实际面）。"""
    problems = []
    missing_files = [p for p in (tool_path, gate_path, drift_path, carrier_path) if not os.path.isfile(p)]
    if missing_files:
        return [f'判据面无输入：缺 {missing_files[:2]} ⇒ 判"没测到"，不判通过'], []
    try:
        root_decl = ast.literal_eval(next(
            n.value for n in ast.parse(open(gate_path, encoding='utf-8').read()).body
            if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') == 'ROOT_DELIVERED'))
        extra_decl = ast.literal_eval(next(
            n.value for n in ast.parse(open(drift_path, encoding='utf-8').read()).body
            if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') == 'EXTRA_TRACKED'))
        carriers = ast.literal_eval(next(
            n.value for n in ast.parse(open(carrier_path, encoding='utf-8').read()).body
            if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') == 'CARRIERS'))
    except (StopIteration, ValueError, SyntaxError) as exc:
        return [f'三处声明之一读不出来（{type(exc).__name__}）⇒ 面无法核对，判红不判通过'], []
    md = {str(rel) for rel, *_ in carriers if str(rel).endswith(('.md',)) and not str(rel).startswith('..')}
    face = set(declared or ())
    gaps = []
    for label, want in (('ROOT_DELIVERED', {str(x) for x in root_decl}),
                        ('EXTRA_TRACKED', {str(x) for x in extra_decl}),
                        ('md 载体', md)):
        miss = sorted(want - face)
        if miss:
            gaps.append(f'{label} 有 {len(miss)} 项不在同步面内：{miss[:4]}')
    if gaps:
        problems += gaps
    if len(face) < 30:
        problems.append(f'同步面只剩 {len(face)} 项（三处声明并集的正常量级 39）⇒ 判某处声明被改坏')
    return problems, [f'同步面 {len(face)} 项 ⊇ 三处声明并集'
                      f'（ROOT_DELIVERED {len(root_decl)} ∪ EXTRA_TRACKED {len(extra_decl)} ∪ md {len(md)}）']


def check_sync_egress_single_entry():
    """第 52 项：三副本同步的**写出口唯一** + **覆盖面来自声明并集** + **本地链已接线**（BLOCK）。"""
    problems, evidence = [], []
    trees, broken = _s52_trees()
    if broken:
        problems.append(f'这些 .py 解析失败（出口枚举不完整，不许静默少算）：{broken[:4]}')
    writers = _s52_writers(trees)
    names = [w[0] for w in writers]
    if names != [_S52_WRITER]:
        problems.append(f'往交付副本复制源码的写出口必须只有一个（应={_S52_WRITER}），实测：{writers or "零个 —— 那就是没测到"}')
    tool_src = read(os.path.join(ROOT, 'tools', 'frontend_sync_check.py'))
    if '--fix' not in tool_src or 'copy2' not in tool_src:
        problems.append('同步工具缺 --fix 或复制实现 ⇒ 它能查不能修，等于没接线')
    # 面 ⇄ 声明：真调用工具的 declared_files()，不读它对自身的描述
    declared = []
    mod = None                                            # 取不到模块 ⇒ 后续行为核一律判"没测到"
    try:
        import importlib.util as _ilu

        spec = _ilu.spec_from_file_location('ican_sync_tool', os.path.join(ROOT, 'tools', 'frontend_sync_check.py'))
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        declared = list(mod.declared_files())
        orig_dev, orig_mirrors = mod.DEV, mod.MIRROR_DIRS   # 面闸行为核要临时改全局，改完必须回原
    except Exception as exc:                        # noqa: BLE001 取不到面就是"没测到"
        problems.append(f'调用工具的 declared_files() 失败：{type(exc).__name__}: {exc}')
    fp, fe = _s52_face_problems(os.path.join(ROOT, 'tools', 'frontend_sync_check.py'),
                                os.path.join(ROOT, 'quality_gate.py'),
                                os.path.join(ROOT, 'check_delivery_drift.py'),
                                os.path.join(ROOT, 'set_version.py'), declared)
    problems += fp
    evidence += fe
    # 接线：本地闸必须有一阶段跑它，且阶段脚本必须解析在仓内（禁止 D:/tmp 那类仓外一次性脚本）
    vl = os.path.join(ROOT, 'verify_local.py')
    if not os.path.isfile(vl):
        problems.append('取不到 verify_local.py ⇒ 无法证明已接线，判没测到')
    else:
        vsrc = read(vl)
        if 'frontend_sync_check.py' not in vsrc:
            problems.append('本地闸没跑同步检查 ⇒ 工具是孤儿（能修没接=半成品）')
        problems += _s52_offrepo_problems(vsrc)
    # 写者必须**主张自己站在哪一面**（第 41 轮 M129）：本工具的根按 `__file__` 上溯，
    # 复制到镜像面跑就会把镜像当权威源——第 40 轮实测它报 MISSING 472 假红，而照处方跑 --fix
    # 会用 mkdir(parents=True) 往 `交付物/提交包/` 里造 472 个副本。所以"能不能写"之外，
    # 还要判"它在哪儿被允许写"。按结构判（分支退 2 排在任何 mkdir/copy 之前），不按函数名判。
    try:
        tool_tree = ast.parse(tool_src)
        tmain = next((n for n in tool_tree.body
                      if isinstance(n, ast.FunctionDef) and n.name == 'main'), None)
        funcs = {n.name: n for n in ast.walk(tool_tree) if isinstance(n, ast.FunctionDef)}
        # 面闸必须"由一次真计算喂进来"：只查 `if probs: return 2` 的形状会被 `probs = []` 直接骗过去
        # （本轮注入实测：改成空表判据照绿 ⇒ 断言了事实的名字而不是事实本身）。
        fed = set()
        for n in ast.walk(tmain):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) \
                    and getattr(n.value.func, 'id', '') in funcs:
                callee = funcs[n.value.func.id]
                reads = {getattr(x, 'id', '') for x in ast.walk(callee) if isinstance(x, ast.Name)}
                if {'DEV', 'MIRROR_DIRS'} <= reads:
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            fed.add(t.id)
        guard_ln = min((n.lineno for n in ast.walk(tmain) if isinstance(n, ast.If)
                        and any(isinstance(x, ast.Name) and x.id in fed for x in ast.walk(n.test))
                        and any(isinstance(r, ast.Return) and getattr(r.value, 'value', None) == 2
                                for r in ast.walk(n))), default=None)
        wln = [n.lineno for n in ast.walk(tmain)
               if isinstance(n, ast.Attribute) and n.attr in ('mkdir', 'copy2')]
        if tmain is None:
            problems.append('同步工具里没有 main() ⇒ 无法判面闸位置')
        elif not fed:
            problems.append('main() 没有把"读 DEV/MIRROR_DIRS 的函数结果"喂给任何判据 ⇒ 面闸不存在或是死的')
        elif guard_ln is None:
            problems.append('面闸算出来的量没有进入"退 2"分支 ⇒ 写了不用，等于没闸')
        elif wln and guard_ln > min(wln):
            problems.append(f'面闸在第 {guard_ln} 行、写盘在第 {min(wln)} 行之后 ⇒ 拦不住污染交付树那一次')
        else:
            # 行为面双向核：**用刚读到的同一份源码 exec 出独立命名空间**，不复用已 import 的模块对象。
            # 原因（本轮实测）：判据读的是源码、行为核却打在 `importlib` 载入的模块上，
            # 那么"源码被改坏但模块没变"的形态对判据完全隐形 —— 一条只查形状的判据会被这样糊过去。
            # 形状必须**合成**，不能拿本面还原后的 DEV 当源面正对照：门禁在 05 副本里跑时
            # 那本来就是镜像面，face_problems() 如实报问题（第 39 轮"期望须随所在面变"的第三次复发）。
            tmp41 = tempfile.mkdtemp(prefix='gate52-face-')
            try:
                ptype = type(mod.DEV)
                src_like = os.path.join(tmp41, str(mod.DEV_FACE_NAME))
                os.makedirs(src_like)
                mir_a = os.path.join(src_like, 'a-mirror')
                mir_b = os.path.join(src_like, 'b-mirror')
                os.makedirs(mir_a)
                os.makedirs(mir_b)
                probe_ns = {'__file__': os.path.join(ROOT, 'tools', 'frontend_sync_check.py'),
                            '__name__': 'gate52_probe', '__builtins__': __builtins__}
                exec(compile(tool_src, probe_ns['__file__'], 'exec'), probe_ns)
                readouts = {}
                for dev_v, mir_v, key in (
                        (src_like, {'deploy-free': mir_a, '05-源代码全文': mir_b}, 'ok'),
                        (src_like, {'deploy-free': os.path.join(tmp41, 'nope'),
                                    '05-源代码全文': mir_b}, 'gone'),
                        (mir_a, {'deploy-free': mir_a, '05-源代码全文': mir_b}, 'same')):
                    probe_ns['DEV'] = ptype(dev_v)
                    probe_ns['MIRROR_DIRS'] = {k: ptype(v) for k, v in mir_v.items()}
                    readouts[key] = probe_ns['face_problems']()
                ok_face, gone_face, same_face = readouts['ok'], readouts['gone'], readouts['same']
            finally:
                try:
                    shutil.rmtree(tmp41, ignore_errors=True)
                except OSError:
                    pass
            if ok_face:
                problems.append(f'源面形状被 face_problems() 误判：{ok_face[:2]} ⇒ 闸恒红，正常提交会被拦')
            if not any('不存在' in p for p in gone_face):
                problems.append('镜像缺失未被 face_problems() 点名 ⇒ 认不出"跑错面"这一形态')
            if not any('同一个面' in p for p in same_face):
                problems.append('镜像与权威源同一面时未报警 ⇒ 自己比自己必然全等，会伪装成"已同步"')
            if not problems:
                evidence.append(f'写出口自带面闸：退 2 排在 mkdir/copy（第 {min(wln)} 行）之前，'
                                f'合成三面形状分别报 {len(ok_face)}/{len(gone_face)}/'
                                f'{len(same_face)} 条')
    except (SyntaxError, OSError, StopIteration, AttributeError) as exc:
        problems.append(f'面闸结构判据取不到输入：{type(exc).__name__}: {exc} ⇒ 判没测到')
    # ⑪ EXTRA 出口（第 43 轮 M115）：同步面第 37 轮起"只报不删"，本轮补上"人看过、确认删这一个"的合法出口。
    #   判四件事：删除点必须登记进名册、tracked 件必须拒（那是漏同步不是孤儿）、台账必须落在两份镜像之外、
    #   --fix 与 --drop-extra 必须互斥（一次调用只允许一种动作）。
    try:
        tool_tree43 = tool_tree
    except NameError:
        tool_tree43 = ast.parse(tool_src)
    tfns = {n.name: n for n in ast.walk(tool_tree43) if isinstance(n, ast.FunctionDef)}
    de43 = tfns.get('drop_extra')
    if 'drop_extra' not in tfns or 'scan_extra' not in tfns or 'extra_ledger_path' not in tfns:
        problems.append('同步工具缺 EXTRA 出口三件套（scan_extra / extra_ledger_path / drop_extra）'
                        ' ⇒ EXTRA 仍只能报不能处置，M115 未闭')
    else:
        dels43 = _s52_delete_sites(tool_tree43)
        if set(dels43) != set(S52_DELETE_ROSTER):
            problems.append(f'同步面删除点名册⇄实扫不等值：实扫={dels43} 名册={sorted(S52_DELETE_ROSTER)}'
                            ' ⇒ 漏登记＝旁路，多登记＝对空气放行')
        cons43 = {getattr(x, 'value', None) for x in ast.walk(de43) if isinstance(x, ast.Constant)}
        if 'untracked' not in cons43:
            problems.append('drop_extra() 没有按 git 身份放行（缺 untracked 判定）⇒ '
                            '已跟踪件会被当孤儿删掉，那是毁他人在途件')
        if 'tracked' not in cons43:
            problems.append('drop_extra() 里没有 tracked 的专门拒因 ⇒ 只有一条泛化拒绝，'
                            '漏同步与孤儿在日志里分不开')
        if not {'confirm', 'reason'} <= {a.arg for a in de43.args.args}:
            problems.append('drop_extra() 缺 confirm/reason 形参 ⇒ 出口没有复核入口')
        elp = tfns.get('extra_ledger_path')
        if elp is None or not any(isinstance(n, ast.Raise) for n in ast.walk(elp)):
            problems.append('extra_ledger_path() 没有"落点在镜像内即拒"的守卫 ⇒ '
                            '留证可能记在被处置面名下，被自己的动作清掉（第 39 轮一手坑）')
        if not any(isinstance(n, ast.If) and {'fix', 'drop_extra'} <=
                   {getattr(x, 'attr', '') for x in ast.walk(n.test) if isinstance(x, ast.Attribute)
                    or isinstance(x, ast.Name) and x.id == 'args'}
                   for n in ast.walk(tfns.get('main') or de43)):
            problems.append('--fix 与 --drop-extra 未见互斥判定 ⇒ 一次调用两种动作，删没删看不出来')
        # 行为核（纯内存改模块全局 + 临时目录，不写本仓任何在途件）：台账落点越界必须抛
        mod43 = mod
        if mod43 is None:
            problems.append('EXTRA 行为核拿不到已载入的工具模块（前面 importlib 那步就失败了）'
                            ' ⇒ 判没测到，不当作"结构判据过了就算过"')
        else:
            try:
                tmp43 = tempfile.mkdtemp(prefix='gate52-extra-')
                try:
                    inside = os.path.join(tmp43, 'mir')
                    os.makedirs(inside, exist_ok=True)
                    ptype43 = type(mod43.DEV)
                    mod43.DEV = ptype43(inside)
                    mod43.MIRROR_DIRS = {'mir': ptype43(inside)}
                    escaped = False
                    try:
                        mod43.extra_ledger_path()
                    except getattr(mod43, 'ExtraError', RuntimeError):
                        escaped = True
                    mod43.DEV = ptype43(tmp43)
                    good43 = mod43.extra_ledger_path()
                finally:
                    mod43.DEV, mod43.MIRROR_DIRS = orig_dev, orig_mirrors
                    shutil.rmtree(tmp43, ignore_errors=True)
                if not escaped:
                    problems.append('DEV 落在镜像内时 extra_ledger_path() 仍给出路径 ⇒ 越界守卫是死的')
                elif os.path.normcase(os.path.abspath(good43)).startswith(
                        os.path.normcase(os.path.abspath(str(orig_mirrors.get('deploy-free', ''))))):
                    problems.append(f'正常情形下台账落进镜像内 ⇒ {good43}')
                else:
                    evidence.append('EXTRA 出口在册：删除点已登记、tracked 拒、台账落点在镜像外（越界即抛）')
            except (OSError, NameError, AttributeError, TypeError, RuntimeError) as exc:
                # 被测件自己崩（如缺 import）不能把整个门禁带崩，也不能被咽掉：
                # 点名判红 = "判据自己崩了是没测，不是过"。
                problems.append(f'EXTRA 行为核跑不起来：{type(exc).__name__}: {exc}'
                                ' ⇒ 判没测到并点名，不让它冒充通过')
    # ⑫ 写态同锁（第 44 轮 M141）：`--fix`（批量复制）与 `--drop-extra`（复核后删一个）动的是同一个面，
    #   两处必须在**同一把**锁内；只读档（不加 --fix 的比对、--list-extra）不得取锁 —— 排队的是要动手的人，
    #   不是来看一眼的人。形状借 restic `internal/repository/lock.go:Lock()`（锁"我在写这个面"这件事本身，
    #   锁内容 = pid+action+时刻的归因侧件）；跨进程真争锁由 L4 S52 段用子进程验，这里验形状与拒因。
    sl = tfns.get('sync_lock')
    if sl is None or tfns.get('sync_lock_path') is None:
        problems.append('同步工具没有 sync_lock/sync_lock_path ⇒ --fix 与 --drop-extra 可以同时动同一个面，'
                        '「只少那一个」的人口断言会把别人的动作记成本轮的（第 44 轮 M141）')
    else:
        def _s52_with_covered(fn, marks):
            """函数里是否存在「With 取 sync_lock 且体内出现 marks 里的调用」。"""
            for w in ast.walk(fn):
                if not isinstance(w, ast.With):
                    continue
                if not any(isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'sync_lock'
                           for n in ast.walk(w)):
                    continue
                got = {f'{getattr(n.func.value, "id", "?")}.{n.func.attr}'
                       for n in ast.walk(w) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
                if got & set(marks):
                    return True
            return False

        if not _s52_with_covered(de43, ('target.unlink',)):
            problems.append('drop_extra() 的删除动作不在 sync_lock 的 With 体内 ⇒ 锁外取的人口快照与锁内动作之间有窗口')
        if not _s52_with_covered(tfns.get('main') or de43, ('shutil.copy2',)):
            problems.append('main() 的 --fix 复制不在 sync_lock 的 With 体内 ⇒ 两条写态各写各的，锁等于没上')
        for ro in ('list_extra', 'scan_extra', 'print_extra', 'report'):
            fn = tfns.get(ro)
            if fn is not None and any(isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'sync_lock'
                                      for n in ast.walk(fn)):
                problems.append(f'只读档 {ro}() 取了写锁 ⇒ 看一眼也要排队；只读面必须不取锁（对偶腿）')
        def _s52_raised(r):
            # `raise SyncLockError(...)` 的 Raise.exc 是 Call 而不是 Name ⇒ 名字要从 func 上取，
            # 直接判 isinstance(exc, ast.Name) 会让这条守卫恒红（本轮实测踩过一次）。
            f = r.exc
            return getattr(f.func if isinstance(f, ast.Call) else f, 'id', '')

        if not any(isinstance(n, ast.ExceptHandler) and getattr(n.type, 'id', '') == 'Timeout'
                   and any(isinstance(m, ast.Raise) and _s52_raised(m) == 'SyncLockError'
                           for m in ast.walk(n))
                   for n in ast.walk(sl)):
            problems.append('sync_lock() 没有"取不到锁即 raise SyncLockError"的 Timeout 分支 ⇒ '
                            '争锁时降级成没锁也照写（换个别的异常也不行：CLI 侧按 SyncLockError 退 2）')
        slp = tfns.get('sync_lock_path')
        if not any(isinstance(n, ast.Raise) for n in ast.walk(slp)):
            problems.append('sync_lock_path() 没有"锁落进镜像即拒"的守卫 ⇒ 锁文件会被同步动作自己搬走')
        mod44 = mod
        if mod44 is None:
            problems.append('⑫ 行为核拿不到已载入的工具模块（前面 importlib 那步就失败了）⇒ 判没测到')
        else:
            try:
                import types as _tp44
                orig_dev44, orig_mir44 = mod.DEV, mod.MIRROR_DIRS
                orig_fl44 = sys.modules.get('filelock')
                tmp44 = tempfile.mkdtemp(prefix='gate52-lock-')
                try:
                    inside44 = os.path.join(tmp44, 'mir')
                    os.makedirs(inside44, exist_ok=True)
                    ptype44 = type(mod.DEV)
                    mod.DEV, mod.MIRROR_DIRS = ptype44(tmp44), {'mir': ptype44(inside44)}
                    esc44 = False
                    try:
                        mod.sync_lock_path(ptype44(inside44))
                    except getattr(mod, 'SyncLockError', RuntimeError):
                        esc44 = True
                    if not esc44:
                        problems.append('锁文件落在镜像内时 sync_lock_path() 仍给出路径 ⇒ 锁会被自己搬走，守卫是死的')
                    fake44 = _tp44.ModuleType('filelock')

                    class _Timeout44(Exception):
                        pass

                    class _FileLock44:
                        def __init__(self, path, timeout=None):
                            self.path = path

                        def acquire(self, *a, **kw):
                            raise _Timeout44('held elsewhere')

                        def release(self):
                            pass

                    fake44.Timeout, fake44.FileLock = _Timeout44, _FileLock44
                    sys.modules['filelock'] = fake44
                    refused = ''
                    try:
                        with mod.sync_lock('fix'):
                            pass
                    except getattr(mod, 'SyncLockError', RuntimeError) as exc:
                        refused = str(exc)
                    if '写态锁被占用' not in refused:
                        problems.append(f'他人持锁时 sync_lock 未按预期拒（实测：{refused[:70] or "没拒，照写"}）')
                    elif 'pid=' not in refused:
                        problems.append('争锁拒因没点名持锁者（缺 pid/action/ts 归因）⇒ 只说"忙"，不说谁忙')
                    sys.modules['filelock'] = None
                    miss = ''
                    try:
                        with mod.sync_lock('drop-extra'):
                            pass
                    except getattr(mod, 'SyncLockError', RuntimeError) as exc:
                        miss = str(exc)
                    if 'filelock 不可用' not in miss:
                        problems.append(f'filelock 缺失时写态没 fail-closed（实测：{miss[:70] or "照写"}）'
                                        ' ⇒ 把"锁库没了"降级成"没锁也写"')
                finally:
                    mod.DEV, mod.MIRROR_DIRS = orig_dev44, orig_mir44
                    if orig_fl44 is not None:
                        sys.modules['filelock'] = orig_fl44
                    else:
                        sys.modules.pop('filelock', None)
                    shutil.rmtree(tmp44, ignore_errors=True)
                evidence.append('⑫ 写态同锁在册：两处写态都裹锁、只读档不取锁、Timeout 即拒且点名持锁者、'
                             '锁落进镜像即拒、filelock 缺失 fail-closed')
            except (OSError, AttributeError, TypeError, ValueError, KeyError, IndexError, RuntimeError) as exc:
                problems.append(f'⑫ 行为核跑不起来：{type(exc).__name__}: {exc} ⇒ 判没测到')

    # ⑬ 删除半径与证据半径（第 45 轮 M140/M142/M143）：
    #   ① 归档必须**先于** unlink（对标 rclone `fs/sync/sync.go:608 DeleteFilesWithBackupDir`），且落点在镜像外；
    #   ② 凭据只能发给字段合法的件（对标 mutagen `core/conflict.go:11 EnsureValid`）⇒ 校验前置于 extra_token；
    #   ③ 封顶只削可再生成的中间行，终态证据永不削；终态行本身超上限 ⇒ 回报 overflow 而不是删证据。
    arc = tfns.get('archive_extra')
    vli = tfns.get('validate_extra_item')
    ead = tfns.get('extra_archive_dir')

    def _s53_first(fn, needle):
        """函数体内**第一个**名为 needle 的调用行号（没有则 -1）；⑬ 与 ⑭ 共用同一把尺。"""
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                nm = getattr(f, 'attr', '') or getattr(f, 'id', '')
                if nm == needle:
                    return getattr(n, 'lineno', -1)
        return -1

    if arc is None or vli is None or ead is None:
        problems.append(f'同步工具缺 ⑬ 三件套（archive_extra={arc is not None} / '
                        f'validate_extra_item={vli is not None} / extra_archive_dir={ead is not None}）'
                        ' ⇒ 删除仍不可回滚、坏字段仍能领凭据')
    else:
        a_line, u_line = _s53_first(de43, 'archive_extra') if de43 else -1,             _s53_first(de43, 'unlink') if de43 else -1
        if de43 is None:
            problems.append('drop_extra() 不存在 ⇒ ⑬ 无从判"归档是否早于删除"')
        elif not (0 < a_line < u_line):
            problems.append(f'drop_extra() 里 archive_extra 的行号 {a_line} 不早于 unlink 的 {u_line} ⇒ '
                            '顺序反了：先删后归档等于没归档（第 42 轮"归档 lineno 早于删除"同一条尺）')
        term53 = [n for n in ast.walk(de43) if isinstance(n, ast.Call)
                  and getattr(n.func, 'id', '') == 'dict' and n.args
                  and isinstance(n.args[0], ast.Name) and n.args[0].id == 'entry']
        miss53 = [i for i, c in enumerate(term53)
                  if not {k.arg for k in c.keywords} >= {'outcome', 'archive_path'}]
        if not term53 or miss53:
            problems.append(f'drop_extra() 的终态台账行有 {len(miss53) or len(term53)} 处缺 '
                            f'outcome/archive_path（共 {len(term53)} 处终态行调用）⇒ 删完之后查不到字节去了哪里')
        et = tfns.get('extra_token')
        if et is None or _s53_first(et, 'validate_extra_item') <= 0:
            problems.append('extra_token() 没有先跑 validate_extra_item ⇒ 坏字段照样发凭据（形状借 mutagen EnsureValid）')
        if not any(isinstance(n, ast.Raise) for n in ast.walk(ead)):
            problems.append('extra_archive_dir() 没有"归档落点进镜像即拒"的守卫 ⇒ 归档件会被 --fix 复制回权威面')
        cap = tfns.get('_cap_rows')
        cap_src = {getattr(x, 'value', None) for x in ast.walk(cap) if isinstance(x, ast.Constant)} if cap else set()
        if 'intent' not in cap_src:
            problems.append('_cap_rows() 不再按 outcome 分档 ⇒ 封顶会削掉终态证据（第 43 轮自挂 M143 的原缺陷形态）')
        if {'failed', 'unparsable'} & cap_src:
            problems.append('_cap_rows() 的保护名单又变回**白名单枚举**（函数体内出现被点名的终态值）⇒ '
                            '新增 outcome 忘扩展就会被悄悄削掉；正解是"非 intent 即证据"的排除式（第 46 轮 M149）')
        if 'overflow' not in cap_src:
            problems.append('_cap_rows() 没有 overflow 通道 ⇒ 终态行超过上限时只能靠删证据来满足封顶')
        mod53 = mod
        if mod53 is None:
            problems.append('⑬ 行为核拿不到已载入的工具模块 ⇒ 判没测到')
        else:
            try:
                tmp53 = tempfile.mkdtemp(prefix='gate52-cap-')
                try:
                    rows53 = [json.dumps({'n': i,
                                          'outcome': 'dropped' if i % 2 else 'intent',
                                          'token': 'T%03d' % i}, ensure_ascii=False)
                              for i in range(210)]
                    led53 = os.path.join(tmp53, 'cap210.jsonl')
                    with open(led53, 'w', encoding='utf-8', newline='\n') as fh53:
                        fh53.write('\n'.join(rows53) + '\n')
                    r53 = mod._cap_rows(led53, 200)
                    left53 = [json.loads(x) for x in open(led53, encoding='utf-8').read().splitlines()]
                    term53 = sum(1 for x in left53 if x['outcome'] == 'dropped')
                    if r53['dropped'] != 10 or term53 != 105 or r53.get('overflow'):
                        problems.append(f'封顶把终态证据也削了（应保留 105 条终态、实得 {term53}，'
                                        f'返回值 {r53}）⇒ M143 未闭')
                    rows53b = [json.dumps({'n': i, 'outcome': 'dropped'}) for i in range(30)]
                    led53b = os.path.join(tmp53, 'cap30.jsonl')
                    with open(led53b, 'w', encoding='utf-8', newline='\n') as fh53b:
                        fh53b.write('\n'.join(rows53b) + '\n')
                    r53b = mod._cap_rows(led53b, 20)
                    left53b = [x for x in open(led53b, encoding='utf-8').read().splitlines() if x.strip()]
                    if r53b['dropped'] or len(left53b) != 30 or not r53b.get('overflow'):
                        problems.append(f'终态行超上限时删了证据（应 dropped=0 且 overflow=True，'
                                        f'实得 {r53b} / 剩 {len(left53b)} 行）')
                    bad53 = []
                    try:
                        mod.validate_extra_item({'schema': 'bogus', 'sha256': 'zz', 'bytes': -1,
                                                 'git': 'weird', 'rel': '../x', 'src_absent': 'yes'})
                    except getattr(mod, 'ExtraError', ValueError) as exc:
                        bad53 = [str(exc)]
                    if not bad53 or '不合规' not in bad53[0]:
                        problems.append('validate_extra_item 放行了六个字段全坏的件 ⇒ 契约是摆设')
                    elif 'sha256' not in bad53[0]:
                        problems.append('字段契约的拒因没点名 sha256 ⇒ 不可归因（判据面的老要求）')
                finally:
                    shutil.rmtree(tmp53, ignore_errors=True)
                evidence.append('⑬ 删除半径在册：archive_extra 早于 unlink、台账带 archive_path、'
                                '凭据前置于字段校验、封顶只削 intent 且终态超限回报 overflow')
            except (OSError, AttributeError, TypeError, ValueError, KeyError, IndexError, RuntimeError) as exc:
                problems.append(f'⑬ 行为核跑不起来：{type(exc).__name__}: {exc} ⇒ 判没测到')

    # ⑭ 锁的残件出口 + 归档面回读（第 46 轮 M145/M148）。
    #   对标 `borgbackup/borg` `src/borg/archiver/lock_cmds.py:34 do_break_lock`（只破 dead process 留下的锁）
    #   与 `restic/restic` `cmd/restic/cmd_unlock.go:21/47/57-60`（默认 stale-only，`--remove-all` 才连活锁清）。
    #   本仓**不开**那个 escalation：锁文件在本机、pid 活性判得出 ⇒ 活锁永远拒（少一个能毁在途写的开关）。
    ls_fn, bl_fn = tfns.get('lock_state'), tfns.get('break_lock')
    ai_fn, pa_fn = tfns.get('archive_inventory'), tfns.get('prune_archive')
    missing46 = [n for n, f in (('lock_state', ls_fn), ('break_lock', bl_fn),
                                ('archive_inventory', ai_fn), ('prune_archive', pa_fn)) if f is None]
    if missing46:
        problems.append(f'同步工具缺 ⑭ 出口（{"、".join(missing46)}）⇒ '
                        '残件锁仍只能人工 rm（删了不留证）、归档面仍是只写不读')
    else:
        bl_calls = {getattr(n.func, 'id', '') or getattr(n.func, 'attr', '')
                    for n in ast.walk(bl_fn) if isinstance(n, ast.Call)}
        if 'sync_lock' in bl_calls:
            problems.append('break_lock() 里去取那把写态锁 ⇒ 锁正被一个死进程占着，取它只会等到超时，'
                            '破锁出口永远跑不到（borg/restic 的 break-lock 都不取锁）')
        if 'lock_state' not in bl_calls:
            problems.append('break_lock() 没调用 lock_state() ⇒ 状态在别处重算了一份，判定与执行会漂移')
        a46, u46 = _s53_first(bl_fn, 'archive_lock_file'), _s53_first(bl_fn, 'unlink')
        if not (0 < a46 < u46):
            problems.append(f'break_lock() 里归档（{a46}）不早于删除（{u46}）⇒ 破完就查不到当时谁持有')
        term46 = [n for n in ast.walk(bl_fn) if isinstance(n, ast.Call)
                  and getattr(n.func, 'id', '') == 'dict' and n.args
                  and isinstance(n.args[0], ast.Name) and n.args[0].id == 'base'
                  and any(k.arg == 'outcome' and getattr(k.value, 'value', None) != 'intent'
                          for k in n.keywords)]
        miss46 = [i for i, c in enumerate(term46)
                  if not {k.arg for k in c.keywords} >= {'outcome', 'archive_path', 'sidecar_archive_path'}]
        if not term46 or miss46:
            problems.append(f'break_lock() 的**终态**台账行有 {len(miss46) or len(term46)} 处没带齐 '
                            f'archive_path/sidecar_archive_path（共 {len(term46)} 处终态行，intent 行不计）⇒ '
                            '锁与侧件去了哪里查不到')
        bl_const = {getattr(x, 'value', None) for x in ast.walk(bl_fn) if isinstance(x, ast.Constant)}
        if not {'absent', 'held-living', 'unattributable', 'too-young'} <= bl_const:
            problems.append('break_lock() 的拒因没有逐态点名（缺 absent/held-living/unattributable/too-young 之一）⇒ '
                            '"没得破"与"不能破"在日志里分不开')
        if not any(isinstance(n, ast.Raise) for n in ast.walk(bl_fn)):
            problems.append('break_lock() 一条 Raise 都没有 ⇒ 五态全走"照样破"')
        ai_const = {getattr(x, 'value', None) for x in ast.walk(ai_fn) if isinstance(x, ast.Constant)}
        if not {'files_balanced', 'refs_balanced', 'orphan_files', 'dangling_refs'} <= ai_const:
            problems.append('archive_inventory() 没给出双向恒等式或异常分类 ⇒ 漏计数会被读成"归档面干净"')
        if not {'sha_checked', 'sha_mismatch', 'sha_unrecorded'} <= ai_const:
            problems.append('archive_inventory() 没有把台账 archive_sha 拿去和归档件实算比对 ⇒ '
                            '"删了还能捞回来"只是字符串上的承诺（M144 未闭）')
        # 路径⇄字节 的成对字段名册 ⇄ 终态行实际写了哪些归档字段：双向差集，缺一边都判红。
        # 名册从**源码 AST** 取而不是从已 import 的模块取：判据读的是源码，
        # 若从模块取，变异体改了源码那一行而模块没变 ⇒ 这条腿永远翻不出红（第 41 轮"侧件字段校验"同形坑）。
        pair_keys = set()
        for _n46 in tool_tree43.body:
            if isinstance(_n46, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == 'ARCHIVE_SHA_PAIRS' for t in _n46.targets):
                try:
                    pair_keys = {p[0] for p in ast.literal_eval(_n46.value)}
                except (ValueError, TypeError, SyntaxError):
                    problems.append('ARCHIVE_SHA_PAIRS 不是可解析的字面量 ⇒ 名册读不出，判没测到')
        used_keys: set[str] = set()
        for _fn46 in (de43, bl_fn):
            for _n46 in ast.walk(_fn46) if _fn46 else []:
                if isinstance(_n46, ast.Call) and getattr(_n46.func, 'id', '') == 'dict':
                    used_keys |= {k.arg for k in _n46.keywords
                                  if k.arg and k.arg.endswith('archive_path')}
        if used_keys != pair_keys:
            problems.append(f'归档字段名册⇄终态行实写不等值：实写={sorted(used_keys)} '
                            f'名册={sorted(pair_keys)} ⇒ 新增归档字段没人核字节（漏一边=静默放行）')
        pa_const = {getattr(x, 'value', None) for x in ast.walk(pa_fn) if isinstance(x, ast.Constant)}
        if not {'archive-pruned', 'referenced_by'} <= pa_const:
            problems.append('prune_archive() 终态行没有 referenced_by ⇒ 字节没了之后无法证明它曾走过复核')
        if not tool_src:
            problems.append('⑭ 行为核拿不到同步工具源码 ⇒ 判没测到')
        else:
            try:
                from pathlib import Path as _P46
                tmp46 = tempfile.mkdtemp(prefix='gate52-lock-')
                # 行为核必须打在**这份源码 exec 出来的命名空间**上，不是已 import 的模块对象：
                # 否则"源码被改坏但模块没变"的变异体对本项完全隐形（第 21 轮面闸腿已为此换过一次实现，
                # 本轮 L4 实测又踩：七支变异腿里四支翻不出红，红的理由是"根本没跑到那段代码"）。
                ns46 = {'__file__': os.path.join(ROOT, 'tools', 'frontend_sync_check.py'),
                        '__name__': 'gate52_probe46', '__builtins__': __builtins__}
                exec(compile(tool_src, ns46['__file__'], 'exec'), ns46)

                class _Ns46:
                    """把 exec 出来的命名空间包成属性对象：`probe.DEV = x` 写回的就是同一个全局，
                    函数体里读到的也是它 —— 直接给代理对象设属性不会传递到 exec 的作用域。"""

                    def __getattr__(self, k):
                        return ns46[k]

                    def __setattr__(self, k, v):
                        ns46[k] = v

                mod46 = _Ns46()
                try:
                    dev46 = _P46(tmp46)
                    mod46.DEV = dev46
                    mod46.MIRROR_DIRS = {'a-mirror': dev46 / 'a', 'b-mirror': dev46 / 'b'}
                    good46 = mod46.validate_holder({'pid': 1, 'action': 'fix',
                                                    'ts': '2026-01-01T00:00:00+08:00'})
                    bad46 = mod46.validate_holder({'pid': 'x', 'action': '', 'ts': '1'})
                    if good46:
                        problems.append(f'合法 holder 被判不合法（{good46}）⇒ 契约误伤，破锁出口对真残件失灵')
                    if not bad46 or not any('pid' in b for b in bad46):
                        problems.append(f'holder 的 pid/action/ts 契约没有逐项点名（实得 {bad46}）⇒ '
                                        '坏侧件照样领得到凭据，破的是"不知道谁持有"的写态')
                    lk46, hd46 = dev46 / mod46.SYNC_LOCK_NAME, dev46 / mod46.SYNC_HOLDER_NAME
                    st0 = mod46.lock_state()
                    if st0['state'] != 'absent' or st0['breakable']:
                        problems.append(f'无锁时 lock_state 报 {st0["state"]} ⇒ 判不了"没有锁可破"')
                    try:
                        mod46.break_lock(confirm='a' * 16, reason='门禁 ⑭ 行为核的无锁对照用满二十字')
                        problems.append('没有锁时 break_lock 居然走下去了 ⇒ "没得破"被写成"破完了"')
                    except mod46.SyncLockError as exc:
                        if '没有锁可破' not in str(exc):
                            problems.append(f'无锁拒因不点名：{str(exc)[:60]}')
                    lk46.write_text('lock-bytes\n', encoding='utf-8')
                    hd46.write_text(json.dumps({'pid': os.getpid(), 'action': 'fix',
                                                'ts': '2026-01-01T00:00:00+08:00'}), encoding='utf-8')
                    st1 = mod46.lock_state()
                    if st1['state'] != 'held-living':
                        problems.append(f'持锁进程是自己却报 {st1["state"]} ⇒ 活性判定不可信（破活锁＝杀掉在写的进程）')
                    try:
                        mod46.break_lock(confirm='b' * 16, reason='门禁 ⑭ 行为核的活锁对照用满二十字')
                        problems.append('活锁被破了 ⇒ restic 那个 --remove-all 在本仓也不该开')
                    except mod46.SyncLockError as exc:
                        if '活锁拒破' not in str(exc):
                            problems.append(f'活锁拒因不点名：{str(exc)[:60]}')
                    hd46.write_text(json.dumps({'pid': 999999, 'action': 'fix',
                                                'ts': '2026-01-01T00:00:00+08:00'}), encoding='utf-8')
                    st2 = mod46.lock_state()
                    if st2['state'] != 'too-young':
                        problems.append(f'死锁但锁龄未到却报 {st2["state"]} ⇒ 刚被正常回收的锁会被当成残件')
                    old46 = time.time() - 400.0
                    os.utime(str(lk46), (old46, old46))
                    st3 = mod46.lock_state()
                    if st3['state'] != 'stale-residue' or not st3['breakable']:
                        problems.append(f'死锁 + 锁龄 400s 应判残件，实得 {st3["state"]} ⇒ 破锁出口对真残件失灵')
                    tok46 = mod46.lock_break_token(st3)
                    hd46.write_bytes(hd46.read_bytes() + b' ')
                    try:
                        mod46.break_lock(confirm=tok46, reason='门禁 ⑭ 行为核：侧件被改一个字节后旧凭据必须失效')
                        problems.append('侧件多写一个字节后旧 token 仍可用 ⇒ 凭据没绑字节（第 45 轮同一条尺）')
                    except mod46.SyncLockError as exc:
                        if 'confirm' not in str(exc):
                            problems.append(f'凭据失效路径没点名：{str(exc)[:60]}')
                    st4 = mod46.lock_state()
                    res46 = mod46.break_lock(confirm=mod46.lock_break_token(st4),
                                           reason='门禁 ⑭ 行为核：合成残件锁的一次真破（锁与侧件都先归档）')
                    rows46 = mod46._ledger_rows(mod46.extra_ledger_path())
                    adir46 = mod46.extra_archive_dir()
                    archived46 = sorted(p.name for p in adir46.iterdir() if p.is_file())
                    if res46['broken'] != 2 or lk46.exists() or hd46.exists():
                        problems.append(f'破锁后残留 {lk46.exists()} / {hd46.exists()}（broken={res46["broken"]}）')
                    if len(archived46) != 2 or len(rows46) != 2:
                        problems.append(f'破锁应留 2 件归档 + 2 行台账，实得 {len(archived46)} / {len(rows46)}')
                    if [r.get('outcome') for r in rows46] != ['intent', 'lock-broken']:
                        problems.append(f'破锁台账形状应为 intent→lock-broken，实得 '
                                        f'{[r.get("outcome") for r in rows46]}')
                    inv46 = mod46.archive_inventory()
                    if (inv46['files'], inv46['referenced'], inv46['orphan_files'],
                            inv46['dangling_refs'], inv46['anomalies']) != (2, 2, 0, 0, []):
                        problems.append(f'破锁产生的两份归档件应全部被台账指到，实得 {inv46["files"]}/'
                                        f'{inv46["referenced"]}/orphan={inv46["orphan_files"]}/'
                                        f'dangling={inv46["dangling_refs"]}')
                    led46 = mod46.extra_ledger_path()
                    term_rows46 = [r for r in rows46
                                   if r.get('outcome') in ('lock-broken', 'lock-break-failed')
                                   and isinstance(r.get('archive_sha'), str)]
                    if not term_rows46:
                        problems.append('台账里挑不出带 archive_sha 的终态行 ⇒ 篡改腿没有现场（判没测到，不判过）')
                    else:
                        old_sha46 = term_rows46[-1]['archive_sha']
                        raw46 = open(led46, encoding='utf-8').read()
                        flip46 = raw46.replace(old_sha46,
                                               ('1' if old_sha46[0] != '1' else '2') + old_sha46[1:], 1)
                        if flip46 == raw46:
                            problems.append('台账里的 archive_sha 改不动 ⇒ 这条变异腿没构造出现场（判据没测到）')
                        else:
                            with open(led46, 'w', encoding='utf-8', newline='\n') as fh46t:
                                fh46t.write(flip46)
                            inv46t = mod46.archive_inventory()
                            with open(led46, 'w', encoding='utf-8', newline='\n') as fh46t:
                                fh46t.write(raw46)
                            if inv46t['sha_mismatch'] != 1 or not any(
                                    '实算' in a for a in inv46t['anomalies']):
                                problems.append(f'手改台账 archive_sha 后仍报 mismatch={inv46t["sha_mismatch"]} ⇒ '
                                                '账载字节从没被拿去和归档件比对（M144 未闭）')
                            if inv46t['files'] != inv46['files']:
                                problems.append('篡改台账字节把归档人口也改了 ⇒ 回读面与处置面互相污染')
                    inv46b = mod46.archive_inventory()
                    if inv46b['sha_mismatch'] != 0 or inv46b['sha_checked'] != 2:
                        problems.append(f'还原台账后应 sha_checked=2 / mismatch=0，实得 '
                                        f'{inv46b["sha_checked"]}/{inv46b["sha_mismatch"]} ⇒ 还原没还原干净')
                    stray = adir46 / 'stray-no-bookkeeping.txt'
                    stray.write_text('没有任何台账指向这件\n', encoding='utf-8')
                    old47 = time.time() - 400.0
                    os.utime(str(stray), (old47, old47))    # 先把龄期做出窗口，否则先撞 TTL 拒因、测不到"来历"那条
                    inv47 = mod46.archive_inventory()
                    if inv47['orphan_files'] != 1 or inv47['anomalies'] != ['stray-no-bookkeeping.txt']:
                        problems.append(f'孤儿归档件没被点名（orphan={inv47["orphan_files"]} '
                                        f'anomalies={inv47["anomalies"][:3]}）⇒ 归档面仍是只写不读')
                    try:
                        mod46.prune_archive('stray-no-bookkeeping.txt', confirm='c' * 16,
                                          reason='门禁 ⑭ 行为核：孤儿件必须先补账再谈清理',
                                          ttl_s=1.0)
                        problems.append('来历查不到的孤儿件被清了 ⇒ 把"没记账"变成"没痕迹"，正是要防的形态')
                    except mod46.ExtraError as exc:
                        if '终态行' not in str(exc):
                            problems.append(f'孤儿件拒因不点名：{str(exc)[:60]}')
                    nm46 = archived46[0]
                    sha46 = hashlib.sha256((adir46 / nm46).read_bytes()).hexdigest()
                    try:
                        mod46.prune_archive(nm46, confirm=mod46.archive_token(nm46, sha46),
                                          reason='门禁 ⑭ 行为核：仍在保留窗口内的归档件不许清',
                                          ttl_s=3600.0)
                        problems.append('未过保留窗口就清了归档件 ⇒ "删了还能捞回来"的承诺被悄悄缩到 1 小时')
                    except mod46.ExtraError as exc:
                        if '保留窗口' not in str(exc):
                            problems.append(f'未到期拒因不点名：{str(exc)[:60]}')
                    old48 = time.time() - 400.0
                    os.utime(str(adir46 / nm46), (old48, old48))
                    # 前置守卫：变异腿会重跑**整个**判定函数，别的腿改坏 `_cap_rows` 时，
                    # 本腿依赖的终态行可能已被削掉。这时必须"报红 + 说明前置塌了"，
                    # 不能让它以未捕获异常冲出——一条腿崩 = 后面所有腿与整个套件一起瞎（第 46 轮一手）。
                    if not [r for r in mod46._ledger_rows(mod46.extra_ledger_path())
                            if r.get('outcome') == 'lock-broken']:
                        problems.append('⑭ 清理腿的前置塌了（台账里已无 lock-broken 终态行）⇒ 判没测到')
                    else:
                        pr46 = mod46.prune_archive(nm46, confirm=mod46.archive_token(nm46, sha46),
                                                   reason='门禁 ⑭ 行为核：过了窗口的归档件按凭据清一件并留下 sha',
                                                   ttl_s=1.0)
                        inv48 = mod46.archive_inventory()
                        if pr46['pruned'] != 1 or (inv48['pruned_explained'], inv48['dangling_refs']) != (1, 0):
                            problems.append(f'清理后应记为 pruned_explained=1 / dangling=0，实得 '
                                            f'{inv48["pruned_explained"]}/{inv48["dangling_refs"]}')
                        if inv48['files'] != 2 or inv48['orphan_files'] != 1:
                            problems.append(f'清理只应动被点名的那一件（清完应剩 2 件=另一份归档+孤儿件，'
                                            f'其中 orphan=1；实得 files={inv48["files"]} orphan='
                                            f'{inv48["orphan_files"]}）')
                    # 对账不平**造得出现场**才配说"会红"：一件同时落在 partial 与 referenced
                    # 两个分类（崩在半路又被补了账）⇒ 分类合计 > files ⇒ 必须抛，不能只报"平"。
                    overlap46 = adir46 / 'overlap-case.lock.part'
                    overlap46.write_text('半成品\n', encoding='utf-8')
                    mod46._append_row(mod46.extra_ledger_path(),
                                      {'schema': mod46.EXTRA_SCHEMA, 'outcome': 'dropped',
                                       'token': 'OVERLAP46', 'archive_path': overlap46.as_posix(),
                                       'archive_sha': '0' * 64})
                    try:
                        ov46 = mod46.archive_inventory()
                        problems.append(f'分类重叠（{ov46["referenced"]}+{ov46["partial_writes"]} 含同一件）'
                                        '却没被判不平 ⇒ 恒等式是死的，漏计会被读成"归档面干净"')
                    except mod46.ExtraError as exc:
                        if '对账不平' not in str(exc):
                            problems.append(f'归档面重叠的拒因不点名：{str(exc)[:60]}')
                    overlap46.unlink()
                    led49 = os.path.join(tmp46, 'cap-exotic.jsonl')
                    with open(led49, 'w', encoding='utf-8', newline='\n') as fh46:
                        fh46.write('\n'.join([json.dumps({'n': i, 'outcome': 'intent' if i % 2 else 'some-future-kind'})
                                              for i in range(210)]) + '\n')
                    r46 = mod46._cap_rows(led49, 200)
                    left46 = [json.loads(x) for x in open(led49, encoding='utf-8').read().splitlines()]
                    if sum(1 for x in left46 if x['outcome'] == 'some-future-kind') != 105:
                        problems.append('封顶把**没见过的新终态**也削了 ⇒ 保护名单又是白名单式枚举（M149 未闭）')
                    if r46['dropped'] != 10 or r46.get('overflow'):
                        problems.append(f'排除式封顶的 intent 侧算错（{r46}）⇒ 新机制自己先坏')
                finally:
                    # 探针命名空间随本腿一起丢弃 ⇒ 不需要"复原全局"。
                    # （第 45 轮及之前是改**已 import 的共享模块**的 DEV/MIRROR_DIRS，
                    #   那等于把判据对象当夹具用：并发腿会互相看见对方的 DEV。）
                    shutil.rmtree(tmp46, ignore_errors=True)
                evidence.append('⑭ 残件锁出口与归档面回读在册：五态各有拒因、破锁不取那把锁、'
                                '两份字节先归档再删、凭据绑侧件字节、归档件字节⇄账载 sha 实算比对、'
                                '孤儿件拒清、未到期拒清、封顶改为"非 intent 即证据"')
            except (OSError, AttributeError, TypeError, ValueError, KeyError, IndexError, RuntimeError) as exc:
                problems.append(f'⑭ 行为核跑不起来：{type(exc).__name__}: {exc} ⇒ 判没测到')

    record('BLOCK', '52. 三副本同步出口唯一（写者=1 ∧ 面=三处声明并集 ∧ 本地链已接线且无仓外路径 '
                    '∧ 写者自证所在面 ∧ EXTRA 有复核出口 ∧ 两处写态同一把锁 ∧ 删除前先归档 ∧ 封顶不削证据 '
                    '∧ 残件锁有显式破锁出口 ∧ 归档面双向对账）',
           not problems, problems or (evidence + [f'写出口枚举：{names}']))



def check_business_calendar():
    """第 43 项：业务日切与时区 —— 裸进程时钟清零、业务日历成唯一入口、降级必须可见。

    动因（2026-09-27 当场实测）：全仓 AST 数到 **45 处**裸 `date.today()`/`datetime.now()`
    （grep 口径 67 行，含注释与带参形态 —— 分母按调用点而不是按命中行）。PythonAnywhere
    免费层服务器是 UTC ⇒ 每天北京时间 00:00—08:00 这 8 小时内服务端的"今天"是昨天：
    早上补交写成前一天、看板"今日"空白、`uq(staff,date,pack,product)` 判重判错日子。
    而时区/日切方向在既有 42 项门禁与三套测试里 **0 条判据**。`src/ai_quota.py:53`
    的注释早已自认同一形态会算错当天用量 —— 上一轮只修了那一个出口。
    上游（同轮 `gh api` 取回原文）：Django `django/utils/timezone.py:187-198` 的
    `localdate()` 是"先转当前时区再取 date"；Frappe `frappe/utils/data.py:388-390` 的
    业务时区取**系统设置**而非进程时钟；InvenTree `settings.py:741-747` 配了 `TIME_ZONE`
    就当场 `ZoneInfo()` 校验、非法即 ValueError ⇒ "配错"必须是启动失败。
    """
    problems, evidence = [], []
    trees, broken = _s43_trees()
    if broken:
        problems.append(f'这些 .py 解析失败（时钟面枚举不完整，不许静默少算）：{broken[:5]}')
    if not trees:
        record('BLOCK', '43. 业务日切与时区（裸时钟清零 + 业务日历唯一入口 + 降级可见）',
               False, ['一个 .py 都没解析成功 —— 判"没测到"，不判通过'])
        return
    naive, business, explicit = _s43_clock_shapes(trees)
    if not (naive or business or explicit):
        problems.append('三类时钟口径全部为 0（不可能：全仓有 ORM 默认值与看板窗口）⇒ 判枚举器失效')
    outside = [f'{rel}:{ln}({fn})={txt}' for rel, ln, fn, txt in naive
               if f'{rel}::{fn}::{txt}' not in _S43_NAIVE_ALLOWED]
    if outside:
        problems.append(f'这些裸进程时钟调用点未经业务日历（UTC 服务器上会把"今天"算错 8 小时）：'
                        f'{outside[:6]}')
    stale = [k for k in _S43_NAIVE_ALLOWED
             if not any(f'{rel}::{fn}::{txt}' == k for rel, ln, fn, txt in naive)]
    if stale:
        problems.append(f'白名单里的豁免点已不存在（陈旧豁免=名册失真）：{stale[:5]}')
    for k, why in sorted(_S43_NAIVE_ALLOWED.items()):
        if len(str(why).strip()) < 20:
            problems.append(f'豁免 {k} 的理由不足 20 字（豁免是决策，必须留下为什么）')
    if business < _S43_BUSINESS_FLOOR:
        problems.append(f'业务日历调用点 {business} 处低于地板 {_S43_BUSINESS_FLOOR} '
                        '（删调用比改调用更容易假装完成 ⇒ 反向地板守这条）')
    if explicit < _S43_EXPLICIT_UTC_FLOOR:
        problems.append(f'显式 UTC 时钟读数 {explicit} 处低于地板 {_S43_EXPLICIT_UTC_FLOOR} '
                        '（技术戳那半边必须仍显式带时区，不能倒回裸 now）')
    shape_problems, shape_ev = _s43_shape_problems(trees)
    problems += shape_problems
    evidence += shape_ev
    # 配置面与接线：假开关判据（S21 同族）——可配就必须写进 .env.example；healthz 必须真调
    env_txt = read(os.path.join(ROOT, '部署', '.env.example')) if os.path.isfile(
        os.path.join(ROOT, '部署', '.env.example')) else ''
    if _S43_ENV_NAME not in env_txt:
        problems.append(f'{_S43_ENV_NAME} 未登记进 部署/.env.example ⇒ 可配却无人知道可配（假开关）')
    req = read(os.path.join(ROOT, 'requirements.txt'))
    if not re.search(r'^tzdata\b', req, re.M):
        problems.append('requirements.txt 缺 tzdata ⇒ 生产端 zoneinfo 主路径走不通，只能长期靠降级跑')
    app_src = read(os.path.join(ROOT, 'src', 'app.py'))
    hooked = re.search(r'business_calendar\.validate_config\(\)', app_src)
    if not hooked:
        problems.append('src/app.py 未调用 business_calendar.validate_config() ⇒ 配错不会启动失败')
    described = False
    try:
        atree = ast.parse(app_src)
        for node in ast.walk(atree):
            # `ast.unparse(node.func)` 给出 `_bc.describe`（不带括号）——早先按 `describe()`
            # 匹配必然落空，把已接线的出口判成未接线（假红）。同时要求被调对象确实是业务日历，
            # 别的项目里的 `describe()` 不算（接线判据也要有鉴别力）。
            f = node.func if isinstance(node, ast.Call) else None
            if isinstance(f, ast.Attribute) and f.attr == 'describe' and \
                    re.search(r'(business_calendar|_bc)$', ast.unparse(f.value)):
                described = True
    except (SyntaxError, MemoryError) as exc:
        problems.append(f'src/app.py 解析失败，无法核验 /healthz 接线：{exc}')
    if not described:
        problems.append('/healthz 未调用 business_calendar.describe() ⇒ 线上问不到业务日与解析路径')
    evidence.append(f'取数面：{len(trees)} 个 .py（含部署脚本）；裸进程时钟 {len(naive)} 处'
                    f'（豁免 {len(_S43_NAIVE_ALLOWED)} 条）/ 业务日历调用 {business} 处'
                    f'（地板 {_S43_BUSINESS_FLOOR}）/ 显式 UTC {explicit} 处（地板 {_S43_EXPLICIT_UTC_FLOOR}）')
    record('BLOCK', '43. 业务日切与时区（裸时钟清零 + 业务日历唯一入口 + 降级可见）',
           not problems, problems or evidence)


# ══════════════ 44. 出站投递面（SSRF 守卫 + 投递留痕 + 跨轮名册联动）（BLOCK）══════════════
_S44_GUARD_FILE = 'src/outbound_guard.py'
_S44_HOOKS_FILE = 'src/webhooks.py'
_S44_APIOPEN_FILE = 'src/api_open.py'
_S44_OPS_SCRIPT = '部署/pythonanywhere/one_click_deploy.py'
_S44_TABLE = 'webhook_delivery'
_S44_HTTP_MODULES = ('requests', 'httpx', 'aiohttp')
_S44_HTTP_METHODS = ('get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'request')
#: 为可测性开私网后门 = 把洞留给生产。这几个名字一旦出现在源码里即红。
_S44_BACKDOOR_WORDS = ('ALLOW_PRIVATE', 'SSRF_BYPASS', 'DISABLE_OUTBOUND_GUARD', 'ALLOW_LOOPBACK')
_S44_DELIVER_FUNC = '_deliver_one'
_S44_RETRY_FUNCS = frozenset({'retry_due', 'due_deliveries'})
_S44_NOTE = []
# 出站点豁免：键 `文件::函数`，值 = 为什么这里允许不经 outbound_guard（≥20 字，门禁逐条判）。
# 三条理由都指向同一件事：目标不是**租户可控输入**，而是运维给定的常量/配置；
# 收紧它们需要一次口径决定（见报告 L68），不是本轮顺手改 —— 顺手改会把本地 Dify 打挂。
_S44_OUTBOUND_ALLOWED = {
    'src/dify_client.py::_deepseek_chat':
        '目标来自运维配置 DIFY_BASE_URL（默认 http://localhost:8080 本地 Dify），'
        '不是租户可控输入；按公网守卫会打断离线开发',
    'src/dify_client.py::_dify_chat':
        '同上：Dify 平台地址由运维 env 给定，商家不可控；默认值即本地回环，公网守卫会打死开发链',
    'src/dify_client.py::_dify_completion':
        '同上：Dify 补全端点目标来自 env 配置而非请求入参，收紧属另行口径决定（报告 L68）',
    'src/email_verify.py::send_code_email':
        '发信目标是 Brevo 常量 API 域（模块内写死），不接受任何外部入参 ⇒ 无 SSRF 面可走',
}


def _s44_func_of(tree, lineno):
    """最内层包含该行的函数名（模块级记 `<module>`）。"""
    best = '<module>'
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                getattr(node, 'lineno', 0) <= lineno <= getattr(node, 'end_lineno', 0):
            best = node.name
    return best


def _s44_outbound_calls(trees, outside=()):
    """枚举出站 HTTP 调用点与"函数内是否调过 check_target"。

    【面】= 应用自身的出站写路径。`部署/pythonanywhere/one_click_deploy.py` 是运维手工脚本
    （目标是 PA API 主机常量 + 运维 Token，且不在 Web 进程里跑）⇒ 整份按"面外文件"排除，
    并把排除名单印在回执里 —— 比给它的 9 个函数各写一条豁免诚实：豁免名册该记**决策**，
    不是把一份脚本的每个调用点抄一遍（抄来的名单第一个过期，且没人会去核对）。"""
    sites, guarded_funcs = [], set()
    for rel, tree in sorted(trees.items()):
        if tree is None or rel in outside:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(isinstance(c, ast.Call) and ast.unparse(c.func).endswith('check_target')
                       for c in ast.walk(node)):
                    guarded_funcs.add((rel, node.name))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            base = ast.unparse(node.func.value)
            if base.split('.')[0] not in _S44_HTTP_MODULES:
                continue
            if node.func.attr not in _S44_HTTP_METHODS:
                continue
            sites.append((rel, node.lineno, _s44_func_of(tree, node.lineno),
                          f'{base}.{node.func.attr}()'))
    return sites, guarded_funcs


def _s44_shape_problems(trees):
    """守卫自身与投递实现的四条形状判据（少任一条都是"看着有、其实不成立"）。"""
    problems = []
    guard = trees.get(_S44_GUARD_FILE)
    if guard is None:
        return [f'{_S44_GUARD_FILE} 读不到或解析失败 —— 出站守卫整体缺席'], []
    names = {n.name for n in ast.walk(guard)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    need = {'check_target', 'pin_http_url', 'describe_host'}
    if need - names:
        problems.append(f'出站守卫缺必需函数：{sorted(need - names)}')
    tops = {t.id for n in ast.walk(guard) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)}
    if 'OUTBOUND_PORTS' not in tops:
        problems.append('缺 OUTBOUND_PORTS 端口白名单 ⇒ 「打到自家任意端口」这条面无人守')
    if 'OUTBOUND_SCHEMES' not in tops:
        problems.append('缺 OUTBOUND_SCHEMES ⇒ 协议白名单没有单一真相源')
    deliver_ev = []
    hooks = trees.get(_S44_HOOKS_FILE)
    if hooks is None:
        problems.append(f'{_S44_HOOKS_FILE} 读不到，无法核验出站点是否真的过守卫')
    else:
        # 投递用的是可注入的 `post` 形参（为测试留的缝），所以**不能**按 `requests.post(` 的字面
        # 形状找调用点 —— 第一版就是这么找的，结果看不见自己那行真代码，判出一条假红。
        # 改为函数级：`_deliver_one` 内必须出现 `allow_redirects=<Constant(False)>`。
        redirect_ok = False
        deliver_guarded = False
        for node in ast.walk(hooks):
            if not isinstance(node, ast.FunctionDef) or node.name != _S44_DELIVER_FUNC:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and ast.unparse(sub.func).endswith('check_target'):
                    deliver_guarded = True
                if isinstance(sub, ast.Call) and any(
                        k.arg == 'allow_redirects' and isinstance(k.value, ast.Constant)
                        and k.value.value is False for k in sub.keywords):
                    redirect_ok = True
        if not deliver_guarded:
            problems.append(f'{_S44_HOOKS_FILE}::{_S44_DELIVER_FUNC} 不调 check_target ⇒ 守卫只在创建时'
                            '跑一次；库里那行 URL 之后可被改写、DNS 也可被换，投递时无人复判'
                            '（这一处的 post 是注入形参，出站调用点枚举按模块名匹配看不见它）')
        if not redirect_ok:
            problems.append(f'{_S44_HOOKS_FILE}::{_S44_DELIVER_FUNC} 没有 allow_redirects=False '
                            '⇒ 「校验 A、重定向落到内网 B」')
        if deliver_guarded and redirect_ok:
            deliver_ev.append(f'投递出站点按函数点名：{_S44_HOOKS_FILE}::{_S44_DELIVER_FUNC} '
                              '既复判守卫又禁重定向（该处的 post 是注入形参，模块名枚举看不见它）')
    api = trees.get(_S44_APIOPEN_FILE)
    if api is None:
        problems.append(f'{_S44_APIOPEN_FILE} 读不到，无法核验创建时的早失败')
    else:
        created = False
        for node in ast.walk(api):
            if isinstance(node, ast.FunctionDef) and node.name == 'subscribe_webhook':
                created = any(isinstance(c, ast.Call) and ast.unparse(c.func).endswith('check_target')
                              for c in ast.walk(node))
        if not created:
            problems.append('subscribe_webhook 未调 check_target ⇒ 创建时不早失败，'
                            '商家只在投递失败后看到"发不出去"（真因是 URL 违规）')
    return problems, [f'守卫函数 {len(names & need)}/{len(need)} 齐备，端口/协议白名单与'
                      f'重定向禁令、创建时早失败均在册', *deliver_ev]


def _s44_registry_problems(trees):
    """跨轮名册联动：新表必须同时进归因/租户/擦除三张名册，漏一张就是静默越权面。"""
    problems, evidence = [], []
    md = trees.get('src/models.py')
    if md is None:
        return ['src/models.py 读不到，无法核对投递表形状'], evidence
    cols, payload_like = set(), []
    for node in ast.walk(md):
        if isinstance(node, ast.ClassDef) and node.name == 'WebhookDelivery':
            for st in node.body:
                if isinstance(st, ast.Assign):
                    for t in st.targets:
                        if isinstance(t, ast.Name):
                            cols.add(t.id)
    need_cols = {'subscription_id', 'merchant_id', 'delivery_id', 'event', 'attempt', 'ok',
                 'status_code', 'error_class', 'payload_sha256', 'next_retry', 'blocked', 'created_at'}
    if not cols:
        problems.append('models.py 里找不到 WebhookDelivery（投递留痕不是物件 ⇒ 重试计划无处存）')
    elif need_cols - cols:
        problems.append(f'webhook_delivery 缺列：{sorted(need_cols - cols)}')
    payload_like = [c for c in cols if c.startswith('payload') and c != 'payload_sha256']
    if payload_like:
        problems.append(f'留痕表出现业务正文字段 {payload_like} —— 商家业务明细不得进审计面'
                        '（第 26/28 轮同一条红线），只准存摘要 sha256')
    er = trees.get('src/erasure.py')
    if er is not None and _S44_TABLE not in ast.unparse(er):
        problems.append(f'{_S44_TABLE} 未进 erasure.OWNING ⇒ 商家注销会留下无主投递账，'
                        '而第 42 项的分母对账也会红')
    qg_tree = trees.get('src/query_guard.py')
    if qg_tree is not None and 'WebhookDelivery' not in ast.unparse(qg_tree):
        problems.append('WebhookDelivery 未进 query_guard 租户名单 ⇒ 跨商家可读投递记录（越权面）')
    cl = trees.get('src/change_log.py')
    if cl is not None and _S44_TABLE not in ast.unparse(cl):
        problems.append(f'{_S44_TABLE} 未在三方名册（change_log 三档）里登记 ⇒ "不跟踪"也要点名')
    evidence.append(f'留痕表 {len(cols)} 列、跨轮名册三处联动已核（归因/租户/擦除）')
    return problems, evidence


def _s44_env_string_constants(tree):
    """取"真的被当环境变量名去读"的字符串常量：os.environ.get('X') / _env_int('X') 的首参。

    词表若拿全文子串去扫，会扫到门禁自己的词表与守卫文档串里的反例（自指假命中，
    本仓第 26/28 轮栽过三次）。判"有没有开后门"要看**行为**：有没有代码去读这个名字的环境变量。
    """
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = ast.unparse(node.func)
        if not (fname.endswith('environ.get') or fname.split('.')[-1] == 'get'
                or fname.startswith('_env_')):
            continue
        for arg in node.args[:1]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.add(arg.value)
    return out


def _s44_backdoor_problems(trees):
    """源码读任何名字里含守卫后门词的环境变量 ⇒ 红（为可测性开私网口子=把洞留给生产）。"""
    hits, skipped = [], []
    for rel, tree in sorted(trees.items()):
        if tree is None:
            continue
        if rel == 'quality_gate.py':
            skipped.append(rel)          # 度量工具不量自己：本文件里那份词表不是开关
            continue
        for name in sorted(_s44_env_string_constants(tree)):
            upper = name.upper()
            for word in _S44_BACKDOOR_WORDS:
                if word in upper:
                    hits.append(f'{rel}:{name}')
    if skipped:
        _S44_NOTE.append(f'后门子判据面外文件（度量工具不量自己）：{skipped}')
    return [f'源码读到了守卫后门开关环境变量（禁）：{hits}'] if hits else []


# ══════════════ 45. 写路径并发正确性（丢失更新 / 单列读改写 / 先查重后插入）══════════════
_S45_TOKEN_FIELD = '_occ'
_S45_GUARD_CALLS = ('conflict_guard.check', 'conflict_guard.cas_flip')
_S45_FORM_SRC = ('request.form', 'request.json', 'get_json', 'parse_float', 'request.args')
#: 动作名必须在 change_log.ACTIONS 里，且留痕写入器要真存在（第一版误用聚合口，运行时才炸）
_S45_ACTION = 'conflict_rejected'
_S45_WRITERS = ('note_denied',)
_S45_BRIDGES = ('note_cas_conflict',)
#: 未接守卫的写出口豁免：键 `文件::函数`，值 = 为什么这里可以没有并发凭据（≥20 字，逐条判）。
# 当前为空 —— 三条候选豁免都被自己的「陈旧豁免」腿打回了（批量上下架是「设为常量」、
# 删除核的是存在性、成员绑定是增行，都不是 A/B/C 的样本）。留空 = 全部真接线，
# 而不是「用名册把没接的出口洗白」。
_S45_ALLOWED = {}


def _s45_model_words(trees):
    """models.py 里的 ORM 类名 —— 类 C 要靠它认出"探针打的是不是被插入的那个模型"。"""
    md = trees.get('src/models.py')
    if md is None:
        return ()
    return tuple(n.name for n in md.body if isinstance(n, ast.ClassDef))


def _s45_tracked_columns(trees):
    """TRACKED 值档的列名词表：从 change_log 的权威名册取，绝不在门禁里再抄一份名单。"""
    cl = trees.get('src/change_log.py')
    if cl is None:
        return set(), []
    cols = set()
    for node in ast.walk(cl):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == 'TRACKED' and isinstance(node.value, ast.Dict):
                for val in node.value.values:
                    if isinstance(val, (ast.Tuple, ast.List)):
                        cols |= {k.value for k in val.elts if isinstance(k, ast.Constant)}
    actions = set()
    for node in ast.walk(cl):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'ACTIONS'
                                               for t in node.targets) \
                and isinstance(node.value, (ast.Tuple, ast.List)):
            actions = {k.value for k in node.value.elts if isinstance(k, ast.Constant)}
    return cols, sorted(actions)


def _s45_form_vars(fn):
    """函数内「值来自表单」的局部变量名（`v = request.form.get(...)` 一类）。"""
    out = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and any(k in ast.unparse(node.value) for k in _S45_FORM_SRC)):
            out.add(node.targets[0].id)
    return out


def _s45_rhs_from_form(rhs, form_vars):
    """右值是否取自表单：直接写 request.form，或取自那个表单变量。"""
    if any(k in rhs for k in _S45_FORM_SRC):
        return True
    return any(rhs == v or rhs.startswith((v + '.', v + '[', v + '(')) for v in form_vars)


def _s45_negates_self(node, name, attr):
    """`x.flag = not x.flag` —— 读—改—写三步的形状（SQLite 没有可用的行锁）。"""
    return (isinstance(node.value, ast.UnaryOp) and isinstance(node.value.op, ast.Not)
            and ast.unparse(node.value.operand) == f'{name}.{attr}')


def _s45_check_then_add(src, model_words):
    """「先查重后插」的竞态只在**同一个模型**上：探针打的模型必须与被插入的模型一致。"""
    added = {m for m in model_words if f'db.session.add({m}(' in src or f'{m}(' in src}
    return [m for m in added if f'{m}.query' in src and '.filter_by(' in src
            and ('.first()' in src or 'exists(' in src)]


def _s45_attr_writes(fn, cols):
    """函数内的属性写：返回「按对象聚合的表单写入列」与「单列取反（读—改—写）」。"""
    fvars, per, rmw = _s45_form_vars(fn), {}, []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        t = node.targets[0]
        if not (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)) or t.attr not in cols:
            continue
        if _s45_rhs_from_form(ast.unparse(node.value), fvars):
            per.setdefault(t.value.id, set()).add(t.attr)
        if _s45_negates_self(node, t.value.id, t.attr):
            rmw.append(t.attr)
    return per, rmw


def _s45_func_sites(tree, rel, cols, model_words):
    """一个文件里的三类写出口：A 整行覆盖 / B 单列读改写 / C 先查重后插入无兜底。"""
    a, b, c = [], [], []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        deco = ' '.join(ast.unparse(d) for d in fn.decorator_list)
        is_route = 'route' in deco and ('POST' in deco or 'PUT' in deco)
        guarded = any(isinstance(s, ast.Call)
                      and ast.unparse(s.func).startswith(_S45_GUARD_CALLS)
                      for s in ast.walk(fn))
        per, rmw = _s45_attr_writes(fn, cols)
        src = ast.unparse(fn)
        for obj, names in per.items():
            if len(names) >= 2 and is_route:
                a.append((f'{rel}::{fn.name}', len(names), guarded))
        if rmw:
            b.append((f'{rel}::{fn.name}', rmw, guarded))
        probe = _s45_check_then_add(src, model_words)
        if probe and 'IntegrityError' not in src:
            c.append((f'{rel}::{fn.name}', probe[0], None))
    return a, b, c


def _s45_sites(trees, cols, model_words):
    """全量枚举三类写出口（面 = 路由层 + 四个服务模块；不含模板与 CLI）。"""
    pool = [t for t in sorted(trees)
            if t.startswith('src/routes/') or t in ('src/api_open.py', 'src/task_flow.py',
                                                    'src/data_import.py', 'src/pack_store.py')]
    out = [[], [], []]
    for rel in pool:
        if trees[rel] is None:
            continue
        for i, got in enumerate(_s45_func_sites(trees[rel], rel, cols, model_words)):
            out[i].extend(got)
    return (out[0], out[1], out[2])


def _s45_site_problems(sites_a, sites_b, sites_c):
    """三类出口逐条核账：没接守卫 / 没走 CAS / 无唯一约束兜底，外加名册失真与理由长度。"""
    problems = []
    for name, _n, guarded in sites_a:
        if not guarded and name not in _S45_ALLOWED:
            problems.append(f'整行表单覆盖出口未接冲突凭据（会无声覆盖他人编辑）：{name}')
    for name, fields, guarded in sites_b:
        if not guarded and name not in _S45_ALLOWED:
            problems.append(f'单列读改写（{"、".join(fields)}）没走比较并交换：{name}')
    for name, _q, _x in sites_c:
        # sites_c 的定义已排除"函数内有 IntegrityError"的：剩下的都是无兜底的先查后插出口
        if name not in _S45_ALLOWED:
            problems.append(f'先查重后插入且无唯一约束兜底（并发下冒泡 500）：{name}')
    seen = ({n for n, _a, _b in sites_a} | {n for n, _f, _c in sites_b}
            | {n for n, _q, _x in sites_c})
    stale = [k for k in _S45_ALLOWED if k not in seen]
    if stale:
        problems.append(f'豁免名册里的出口已不存在（陈旧豁免=名册失真）：{stale[:5]}')
    for key, why in sorted(_S45_ALLOWED.items()):
        if len(str(why).strip()) < 20:
            problems.append(f'豁免 {key} 的理由不足 20 字（豁免是决策，必须留下为什么）')
    return problems


def _s45_audit_problems(trees, actions):
    """留痕侧联动：动作名在册、写入器与桥接函数真存在（"会留痕"不能只写在注释里）。"""
    if _S45_ACTION not in actions:
        return [f'`change_log.ACTIONS` 缺动作 {_S45_ACTION} ⇒ 冲突留痕写不进归因链']
    cl_tree, cg_tree = trees.get('src/change_log.py'), trees.get('src/conflict_guard.py')
    if cl_tree is None or cg_tree is None:
        return ['change_log 或 conflict_guard 读不到（留痕与守卫本体缺席）']
    names = {n.name for t in (cl_tree, cg_tree) for n in ast.walk(t)
             if isinstance(n, ast.FunctionDef)}
    miss = [f for f in list(_S45_WRITERS) + list(_S45_BRIDGES) if f not in names]
    return [f'冲突留痕写入器/桥接函数缺失：{miss}'] if miss else []


def _s45_both_ends(trees):
    """两端都在：服务端读凭据字段 + 模板真发出该字段（只有一端 = 永久 fail-open 的假守卫）。"""
    app_src = trees.get('src/routes/admin.py')
    admin_src = '' if app_src is None else ast.unparse(app_src)
    # 字面量与常量引用两种写法都认（本轮代码走 FORM_FIELD，按字面量找会假红）
    server_side = (_S45_TOKEN_FIELD in admin_src) or ('FORM_FIELD' in admin_src)
    tpl = os.path.join(TPL, 'admin', 'product_list.html')
    client_side, problems = False, []
    try:
        client_side = f'name="{_S45_TOKEN_FIELD}"' in read(tpl)
    except OSError as exc:
        problems.append(f'读不到产品列表模板，无法核验凭据是否真的发出：{exc}')
    if server_side and not client_side:
        problems.append(f'模板没发出 `{_S45_TOKEN_FIELD}` ⇒ 服务端校验永远走"缺凭据放行"分支（假守卫）')
    if not server_side:
        problems.append(f'admin 路由不再读 `{_S45_TOKEN_FIELD}`（凭据校验被拆）')
    return server_side, client_side, problems


def check_write_conflict():
    """第 45 项：写路径的并发正确性 —— 谁先改的必须有凭据可判，不是"最后写的算赢"。

    动因（2026-09-27 当场实测，`D:/tmp/bench32/measure2.py`）：
    ① 两个真实会话编辑同一产品：A 提交 `unit_price=11.00` 得 200，B 的表单在 A 之前加载、
       提交 12.00 也得 200 ⇒ 终值 12.0，**A 的编辑无声消失**；归因链记了两行，但
       "带冲突痕迹的归因行 = 0" —— 记了 diff ≠ 知道自己在覆盖别人；
    ② `product/staff/store` 的上下架是 `x.is_active = not x.is_active`（读—改—写三步，
       SQLite 无行锁可用）；
    ③ `version_id_col|for_update|optimistic|冲突检测|已被他人` 在 `src/*.py`+`src/routes/*.py`
       +`quality_gate.py` **8 组 0 命中** ⇒ 这一整面此前无人看守。
    上游（同轮 `gh api` 实取）：Frappe `document.py:1387`（保存前比对客户端带回的 `modified`，
    `:1409` 抛"has been modified after you have opened it"）；Saleor `AGENTS.md:13-14`
    「用原子操作代替 check-then-act」+ `saleor/app/lock_objects.py:6-7`；
    PostHog `posthog/api/services/llm_prompt.py:208/:302`。
    """
    problems, evidence = [], []
    trees, broken = _s43_trees()
    if broken:
        problems.append(f'这些 .py 解析失败，并发面枚举不完整（不许静默少算）：{broken[:5]}')
    if not trees:
        record('BLOCK', '45. 写路径并发正确性（丢失更新 / 读改写 / 先查重后插入）',
               False, ['一个 .py 都没解析成功 —— 判"没测到"，不判通过'])
        return
    cols, actions = _s45_tracked_columns(trees)
    model_words = _s45_model_words(trees)
    if not model_words:
        problems.append('读不到 models.py 的模型名册（类 C 无法判断探针打的是哪个模型）')
    if not cols:
        problems.append('读不到 change_log.TRACKED 的列名册（谓词失去词表 ⇒ 三类形态一律数不清）')
    sites_a, sites_b, sites_c = _s45_sites(trees, cols, model_words)
    problems += _s45_site_problems(sites_a, sites_b, sites_c)
    problems += _s45_audit_problems(trees, actions)
    server_side, client_side, ends = _s45_both_ends(trees)
    problems += ends
    if not sites_a:
        problems.append('一处"整行表单覆盖"出口都没枚举到（本仓有产品内联编辑 ⇒ 判枚举器失效）')
    if not sites_b:
        problems.append('一处"单列读改写"出口都没枚举到（本仓有 3 处上下架 toggle ⇒ 判枚举器失效）')
    n_a_guarded = len([x for x in sites_a if x[2]])
    n_a_exempt = len([x for x in sites_a if not x[2] and x[0] in _S45_ALLOWED])
    n_b_guarded = len([x for x in sites_b if x[2]])
    n_c_left = len([x for x in sites_c if x[0] not in _S45_ALLOWED])
    evidence.append(f'类 A 整行覆盖 {len(sites_a)} 处（接守卫 {n_a_guarded} 处、豁免 {n_a_exempt} 处）；'
                    f'类 B 单列读改写 {len(sites_b)} 处（接 CAS {n_b_guarded} 处）；'
                    f'类 C 先查重后插 {len(sites_c)} 处（无兜底且未豁免 {n_c_left} 处）')
    evidence.append(f'凭据字段 `{_S45_TOKEN_FIELD}`：服务端读={'是' if server_side else '否'} / '
                    f'模板发={'是' if client_side else '否'}；留痕动作 {_S45_ACTION} 在册='
                    f'{_S45_ACTION in actions}')
    record('BLOCK', '45. 写路径并发正确性（丢失更新 / 读改写 / 先查重后插入）',
           not problems, problems or evidence)


# ══════════════ 46. 依赖供应链与已知漏洞（BLOCK）══════════════
_S46_DB_YML = os.path.join('.github', 'dependabot.yml')
_S46_RECEIPT = 'DEPENDENCY-AUDIT.json'
_S46_LEDGER = 'DEPENDENCY-AUDIT-EXEMPTIONS.json'
_S46_AUDIT_SCRIPT = 'check_dependency_audit.py'
_S46_MAX_AGE_DAYS = 14          # 上游是周更；两周一扫是"能力还在动"的下界
_S46_ID_RE = re.compile(r'^(PYSEC|GHSA|CVE)-[0-9A-Za-z-]+$')
_S46_REASON_MIN = 20


def _s46_github_dir():
    """定位 **真正会被 GitHub 读到** 的那份 `.github/`：从本副本上溯到含 workflows/quality-gate.yml 的目录。

    三个交付副本里只有仓库根那份生效（`iCAN…/.github/` 与 `05…/.github/` 都是镜像/历史摆放），
    所以判据必须显式往上找，而不是拿 ROOT 拼一条路径——那正是本轮案发现场的形态：
    维护者在子目录里放了 dependabot 并以为它在跑。
    """
    cur = ROOT
    for _ in range(6):
        cand = os.path.join(cur, '.github')
        if os.path.isfile(os.path.join(cand, 'workflows', 'quality-gate.yml')):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None



def _s46_dep_dirs(requirements_root):
    """本副本里所有"带 requirements.txt 的在册目录"（相对仓库根的 posix 路径）。"""
    out = []
    for base, dirs, files in os.walk(requirements_root):
        dirs[:] = [d for d in dirs
                   if d not in {'.venv', '.venv-test', '__pycache__', 'node_modules', '.git',
                                'site-packages', '演示快照集', 'backups', 'data', 'archive',
                                '交付物'}   # 交付副本由同步脚本维护，不让 dependabot 直接改镜像
                   and not d.startswith('_') and not d.startswith('退役')]
        if 'requirements.txt' in files:
            rel = os.path.relpath(base, requirements_root).replace(os.sep, '/')
            out.append('/' + (rel if rel != '.' else ''))
    return sorted(set(out))


def _s46_declared_dirs(doc):
    """从 dependabot.yml 取 pip 生态声明的目录（`directory` 与 `directories` 两种写法都认）。"""
    out = []
    for item in (doc.get('updates') or []):
        if str(item.get('package-ecosystem')) != 'pip':
            continue
        if item.get('directory'):
            out.append(str(item['directory']))
        for extra in (item.get('directories') or []):
            out.append(str(extra))
    return out


def _s46_update_problems(item):
    """一条 update 声明自身的接线判据：目录对不对、有没有 schedule、有没有降噪开关。"""
    eco = str(item.get('package-ecosystem'))
    problems = []
    if str(item.get('package-ecosystem')) == 'github-actions' \
            and str(item.get('directory') or '') not in ('/', ''):
        # 该生态的 "/" 指仓库根本身（workflow 在 .github/workflows/ 下），不能按文件存在性判
        problems.append(f'github-actions 的 directory={item.get("directory")!r}，'
                        '而生效配置只在仓库根的 .github/ 里 ⇒ 这条永不命中')
    if not (item.get('schedule') or {}).get('interval'):
        problems.append(f'生态 {eco} 无 schedule.interval（有配置不等于在跑）')
    if not any(k in item for k in ('cooldown', 'groups', 'open-pull-requests-limit')):
        problems.append(f'生态 {eco} 既无 cooldown/groups 又无 PR 上限'
                        ' ⇒ 与"每周开一堆无人看的 PR"只隔一步')
    return problems


def _s46_dependabot_problems(github_dir=None):
    """①② dependabot 配置面：在**仓库根**、严格解析、两类生态齐、声明目录与真实 requirements 对账。

    取数面去掉 ROOT 拼路径：三个副本里只有仓库根的 `.github/` 真的被 GitHub 读到，
    按副本自身目录找会把“镜像里有一份”读成“能力在跑”——正是本轮案发现场。

    `github_dir` 可注入（L4 S38 用它在一个合成面上跑目录对账）：默认路径下镜像副本没有
    `.git`，那个分支根本走不到，反例夹具若建在真实副本上就会"环境巧合判绿"（判据交付契约 ②-b）。
    """
    github_dir = github_dir or _s46_github_dir()
    if github_dir is None:
        return (['上溯 6 层找不到含 workflows/quality-gate.yml 的 .github/ ⇒ 无法确定生效配置在哪'
                '（判“没测到”，不判通过）'], []), []
    root = os.path.dirname(github_dir)
    path = os.path.join(github_dir, 'dependabot.yml')
    if not os.path.isfile(path):
        return ([f'缺 {_S46_DB_YML}：GitHub 只读**仓库根**的 .github/，历史那份一直躺在子目录里 '
                 f'⇒ 从未生效过（git 取数：三条 dependabot 路径全在子目录/归档件）；'
                 f'能力缺口不能被"删掉宣称"掩盖'], []), []
    doc, err = _s33_load_yaml(read(path))
    if err:
        return ([f'{_S46_DB_YML} 解析失败（重复键即抛，与第 33 项同一把尺）：{err}'], []), []
    if not isinstance(doc, dict):
        return ([f'{_S46_DB_YML} 顶层不是映射，读不出 ecosystem'], []), []
    problems, eco = [], {str(i.get('package-ecosystem')) for i in (doc.get('updates') or [])}
    if 'pip' not in eco:
        problems.append('dependabot 没有 pip 生态：生产依赖永不自动更新，已知漏洞只能等下一次人工审计')
    if 'github-actions' not in eco:
        problems.append('dependabot 没有 github-actions 生态：CI 自身的供应链无人管'
                        '（第 26 项只钉 SHA，不升 SHA）')
    for item in (doc.get('updates') or []):
        problems += _s46_update_problems(item)
    want, got = _s46_dep_dirs(root), sorted({d.rstrip('/') + '/' for d in _s46_declared_dirs(doc)})
    if os.path.isdir(os.path.join(root, '.git')):
        # 只有**生效面**（旁边有 .git 的那份）才做目录对账：交付镜像里没有 `/iCAN…` 与 `/deploy-free`
        # 这两个目录，硬对它判红是假阳；镜像的正确保证是"与仓库根逐字节相同"（第 21 项在管）。
        missing = [d for d in want if (d if d.endswith('/') else d + '/') not in got]
        ghost = [d for d in got if d not in {w if w.endswith('/') else w + '/' for w in want}]
        if missing:
            problems.append('这些目录带 requirements.txt 却没被 dependabot 覆盖'
                            f'（该面的依赖永不更新）：{missing}')
        if ghost:
            problems.append('dependabot 声明了不存在 requirements 的目录'
                            f'（幽灵覆盖＝看起来全面其实没扫）：{ghost}')
        scope_ev = f'实测带 requirements 的目录 {len(want)} 个'
    else:
        scope_ev = '本副本是交付镜像（无 .git）⇒ 目录对账交由第 21 项的字节一致性保证'
    evidence = [f'{_S46_DB_YML} 生态={sorted(eco)}；pip 覆盖目录 {got or ["(无)"]}；{scope_ev}']
    return problems, evidence


def _s46_pin_problems(root):
    """③ 钉版纪律：每条声明必须带区间上界或精确钉；CI 里的审计工具自身也要钉版。"""
    problems = []
    req = os.path.join(root, 'requirements.txt')
    if not os.path.isfile(req):
        return ['本副本没有 requirements.txt（钉版面失明，判"没测到"）'], []
    rows = _s46_declared_names(read(req))          # 与 gen_notices / S18 同一个解析器
    loose = [f'{n}{spec}' for n, spec in rows if '<' not in spec and '==' not in spec]
    if loose:
        problems.append(f'requirements 无上界声明 {len(loose)} 条（某次重建会静默跳 major；'
                        f'pandas 2→3 已实测打穿 CSV 护栏）：{loose[:4]}')
    wf = os.path.join(_s46_github_dir() or '', 'workflows', 'quality-gate.yml')
    # 生效的 workflow 只在仓库根的 .github/ 下；按本副本拼路径会永远"找不到 ⇒ 跳过"，
    # 那等于把这条接线判据静默关掉（L4 S38 第三条负对照当场抓出的第二条）。
    if _s46_github_dir() is None:
        problems.append('上溯找不到含 workflows/quality-gate.yml 的 .github/ ⇒ 无法核验审计是否接进 CI')
    # 只认**没被注释掉**的执行行：把步骤整行前加 `#` 就等于把审计停用，
    # 而按「脚本名出现在文件里」判会照样绿（L4 S38 负对照当场抓出的第一条）。
    live = [ln for ln in (read(wf).splitlines() if os.path.isfile(wf) else [])
            if _S46_AUDIT_SCRIPT in ln and not ln.strip().startswith('#')]
    if os.path.isfile(wf) and not live:
        problems.append(f'CI 未执行 {_S46_AUDIT_SCRIPT} ⇒ 审计只在这台机器上跑过，不算接进阻断链'
                        '（注释掉的步骤行不算）')
    evidence = [f'requirements 声明 {len(rows)} 条全部带上界/精确钉']
    return problems, evidence


def _s46_face_vulns(name, face, applied):
    """一个面上的读数：0 包判空转 + 逐条漏洞是否都被台账豁免。返回 (problems, 点到的 id)。"""
    problems, ids = [], set()
    if not face.get('packages'):
        problems.append(f'{name} 面解析到 0 个包 ⇒ 判审计空转，不判通过')
    for row in (face.get('vulns') or []):
        row_ids = set(row.get('ids') or [])
        ids |= row_ids
        if not row_ids <= applied:
            problems.append(f'{name} 面存在未豁免漏洞：{row.get("name")}=={row.get("version")} '
                            f'-> {sorted(row_ids)[:3]}')
    return problems, ids


def _s46_receipt_problems(root):
    """④⑤ 审计回执：两面都真扫过、结果干净、台账无幽灵/无过期、且足够新鲜。"""
    path = os.path.join(root, _S46_RECEIPT)
    if not os.path.isfile(path):
        return [f'缺 {_S46_RECEIPT}：跑一次 `python {_S46_AUDIT_SCRIPT}` '
                '（它同时扫声明面与环境面，工具缺失记 UNVERIFIED 而不是绿）'], []
    if not os.path.isfile(os.path.join(root, _S46_LEDGER)):
        return [f'缺 {_S46_LEDGER}：没有豁免台账 = 任何人都能悄悄放行漏洞'], []
    try:
        doc = json.loads(read(path))
    except ValueError as exc:
        return [f'{_S46_RECEIPT} 不是合法 JSON：{exc}'], []
    faces = doc.get('faces') or {}
    problems = []
    if len(faces) < 2:
        problems.append(f'回执只含 {len(faces)} 面，本项要求声明面 + 环境面两面都在'
                        '（只扫一面必然漏：本轮环境面抓到 10 条而声明面 0 条）')
    open_ids = set()
    applied = set(doc.get('exemptions_applied') or [])
    for name, face in sorted(faces.items()):
        status = face.get('status')
        if status != 'ok':
            problems.append(f'{name} 面 status={status}（未验证不是"没有问题"）：'
                            f'{face.get("error", "")[:80]}')
            continue
        face_p, face_ids = _s46_face_vulns(name, face, applied)
        problems += face_p
        open_ids |= face_ids
    for p in (doc.get('ledger_problems') or []):
        problems.append(f'回执自带的台账读数：{p}')
    problems += _s46_ledger_problems(root, open_ids)
    if any(not _S46_ID_RE.match(str(i)) for i in applied):
        problems.append('豁免 id 形状不合法（PYSEC/GHSA/CVE 之外一律不放行）')
    age = _s46_age_days(doc.get('generated_at'))
    if age is None:
        problems.append('回执缺可读的 generated_at（无时刻 = 不可判断新鲜度）')
    elif age > _S46_MAX_AGE_DAYS:
        problems.append(f'回执已 {int(age)} 天未更新（上限 {_S46_MAX_AGE_DAYS} 天）'
                        f'⇒ 依赖面无人复扫：python {_S46_AUDIT_SCRIPT}')
    summary = ' · '.join(f'{k}={v.get("status")}/{v.get("packages")}包'
                         for k, v in sorted(faces.items())) or '(无面)'
    evidence = [f'审计 {doc.get("generated_at")}（{int(age) if age is not None else -1} 天前）{summary}；'
                f'已豁免 {len(doc.get("exemptions_applied") or [])} 条，'
                f'台账问题 {len(doc.get("ledger_problems") or [])} 条']
    return problems, evidence


def _s46_age_days(stamp):
    """回执距今多少天；不可解析 ⇒ None（由调用方判红，不猜）。"""
    from datetime import UTC
    from datetime import datetime as _dt
    if not stamp:
        return None
    try:
        then = _dt.fromisoformat(str(stamp))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (_dt.now(UTC) - then).total_seconds() / 86400.0


def _s46_ledger_problems(root, open_ids):
    """门禁**自己**读豁免台账并按同一把尺判：形状 / 重复 / 理由长度 / 到期日 / 幽灵豁免。
    """
    path = os.path.join(root, _S46_LEDGER)
    if not os.path.isfile(path):
        return [f'缺 {_S46_LEDGER}：没有台账 = 任何人都能悄悄放行漏洞']
    try:
        doc = json.loads(read(path))
    except ValueError as exc:
        return [f'{_S46_LEDGER} 不是合法 JSON：{exc}']
    problems, seen = [], set()
    today = _s46_today()
    for it in (doc.get('exemptions') or []):
        vid = str(it.get('id') or '').strip()
        if not _S46_ID_RE.match(vid):
            problems.append(f'豁免 id 不合 PYSEC/GHSA/CVE 形状：{vid!r}（幽灵豁免=对空气放行）')
            continue
        if vid in seen:
            problems.append(f'豁免 id 重复：{vid}')
            continue
        seen.add(vid)
        reason = str(it.get('reason') or '').strip()
        expires = str(it.get('expires') or '').strip()
        if len(reason) < _S46_REASON_MIN:
            problems.append(f'豁免 {vid} 的理由不足 {_S46_REASON_MIN} 字'
                            '（豁免是一次决定，必须留下为什么）')
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', expires):
            problems.append(f'豁免 {vid} 缺 ISO 到期日（永久豁免 = 没人再回头看）')
        elif expires < today:
            problems.append(f'豁免 {vid} 已于 {expires} 过期（到期即红，逼一次重判）')
    ghosts = sorted(seen - set(open_ids))
    if ghosts:
        problems.append(f'幽灵豁免（回执的两面里都没有这些 id）：{ghosts[:4]}')
    return problems


def _s46_today():
    from datetime import UTC
    from datetime import datetime as _dt
    return _dt.now(UTC).date().isoformat()


_S46_MODULE_TO_DIST = {'yaml': 'PyYAML'}      # import 名 ⇄ 发行包名不一致的在册映射
_S46_LOCAL_MODULES = {'src', 'quality_gate'}  # 本仓自有包，不是依赖


def _local_module_names():
    """本仓自己提供的顶层可导入名 —— **从盘上派生，不手写名单**。

    手抄那份的名字是"隐性冻结的分母"：第 33 轮新写了 `restore_db.py`，
    当天 ⑤ 就把自家模块判成"未声明的第三方依赖"⇒ 判据红得很自信，红的是假事。
    """
    local = set(_S46_LOCAL_MODULES)
    with contextlib.suppress(OSError):
        local |= {f[:-3] for f in os.listdir(ROOT) if f.endswith('.py')}
    pkg = os.path.join(ROOT, 'src')
    with contextlib.suppress(OSError):
        local |= {d for d in os.listdir(pkg) if os.path.isfile(os.path.join(pkg, d, '__init__.py'))}
    return local


def _s46_third_party_imports(src_text, local=None):
    """源码里 import 到的第三方顶层模块名（按 sys.stdlib_module_names 减掉标准库与本仓自名）。"""
    mods = set()
    for node in ast.walk(ast.parse(src_text)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            mods.add(node.module.split('.')[0])
    std = set(sys.stdlib_module_names)
    return {m for m in mods if m not in std and m not in (local or _local_module_names())}


def _s46_declared_names(req_text):
    """requirements.txt 的 (声明名, 声明约束) 有序表 —— 全仓**唯一**一个 requirements 解析器。

    `tools/gen_notices.py` 与 L4 S18 都改为调它（第 32 轮实测：三处各写一份正则，
    结果是清单里的"声明约束"列停在加下界之前的旧值而无人判红）。
    """
    rows = []
    for raw in req_text.splitlines():
        line = raw.split('#')[0].strip()
        if not line or line.startswith('-'):
            continue
        # extras 先剥掉：否则整行不匹配 ⇒ 这条依赖从分母里消失
        line = re.sub(r'\[[^\]]*\]', '', line, count=1)
        m = re.match(r'^([A-Za-z][A-Za-z0-9._-]*)\s*([<>=!~].*)?$', line)
        if m:
            rows.append((m.group(1), (m.group(2) or '').strip()))
    return rows


def _s46_declared_dists(req_text):
    """requirements 里声明到的发行包名（小写视图，用于与 import 名对账）。"""
    return {n.lower() for n, _s in _s46_declared_names(req_text)}


def _s46_missing_gate_dists(need, have):
    """import 名 → 发行包名后仍未被声明的那些（纯函数，L4 用合成输入打反例）。"""
    return sorted(_S46_MODULE_TO_DIST.get(m, m) for m in need
                  if _S46_MODULE_TO_DIST.get(m, m).lower() not in have)


def _s46_gate_import_problems(root):
    """⑤ 门禁自身的可跑性：门禁 import 的第三方包必须写进依赖契约，不许靠某台机器恰好装了。

    本轮实测的案发形态：本机 `.venv` 里没有 PyYAML，而 `requirements.txt` 从未声明它
    ⇒ 第 33/34/46 项一起退化成"判没测到"（fail-closed 是对的，但三次门禁运行当场作废）。
    CI 侧同一条从未暴露 —— 因为守卫作业拿不到 runner（M42，第 17 次复发），
    所以「判据在自己的机器上跑过」不等于「换一个解释器还跑得动」。
    """
    gate_src = os.path.abspath(__file__)
    req = os.path.join(root, 'requirements.txt')
    if not os.path.isfile(req):
        return ['缺 requirements.txt ⇒ 无法核对门禁自身依赖（判"没测到"）'], []
    need = _s46_third_party_imports(read(gate_src))
    have = _s46_declared_dists(read(req))
    missing = _s46_missing_gate_dists(need, have)
    if missing:
        return ([f'门禁自身 import 的第三方包未声明 {missing}：换解释器/CI 干净检出时 '
                 f'相关判据会整批退化成"没测到"（本轮 .venv 无 PyYAML 即此形态）'], [])
    return [], [f'门禁自身第三方 import {len(need)} 个全部在依赖契约内'
                + (f'（{sorted(need)}）' if need else '')]


def _portable_root(path):
    """把副本目录化成可移植面名（仓内相对 posix 路径；仓外退回 basename）。"""
    try:
        import restore_db as _rd
        return _rd._portable_path(path)
    except Exception:                          # 副本里没有 restore_db 时不猜，直接判"面名未知"
        return ''


_S47_RESTORE_SCRIPT = 'restore_db.py'
_S47_EVIDENCE = 'RESTORE-EVIDENCE.json'
_S47_MAX_AGE_DAYS = 30          # 恢复演练期限：比依赖审计宽，但不许"曾经演练过一次"永久吃老本
_S47_RC_CLASSES = ('RC_OK', 'RC_MISMATCH', 'RC_NO_INPUT', 'RC_TARGET_EXISTS', 'RC_EVIDENCE')


def _s47_egress_problems(src_text):
    """恢复出口的三条形状判据：方向对、源只读、退出码分档直传。

    为什么按字面/AST 判而不按"跑一下看看"：方向写反（`target.backup(source)`）在目标可写时
    会把空库灌回真库（2026-09-26 本机实测），跑一遍恰好看不出来；而 rc 折叠（把 3 折成 1）
    会让"拒覆盖"与"恢复失败"同形，运维读到只会当成又坏了。
    """
    problems = []
    if 'source.backup(' not in src_text:
        problems.append('恢复出口里没有 `source.backup(dst)` —— 快照方向写反时只读源会抛，'
                        '而写回真库的形态看不出来（与第 38 项同族的"方向敏感"）')
    if 'mode=ro' not in src_text:
        problems.append('恢复的源没有以 `mode=ro` 打开：源必须只读是硬前提（防把恢复目标当源）')
    if not all(tok in src_text for tok in _S47_RC_CLASSES):
        missing = [t for t in _S47_RC_CLASSES if t not in src_text]
        problems.append(f'退出码类别不齐（缺 {missing}）⇒ "没测到 / 拒覆盖 / 写不出去"会塌成一个红或塌成 0')
    if 'sys.exit(main())' not in src_text:
        problems.append('main() 的 rc 没有直传 `sys.exit(main())` ⇒ 类别码在出口处被吞')
    return problems


def _s47_evidence_problems(root):
    """恢复台账：≥1 条**真恢复**（自检腿不算），且每条的备份 sha256 可在盘上复算。"""
    path = os.path.join(root, _S47_EVIDENCE)
    if not os.path.isfile(path):
        return [f'缺 {_S47_EVIDENCE}：一次恢复都没留过痕 ⇒ 备份的可恢复性未被证明过'], '', 0
    try:
        doc = json.loads(read(path))
    except ValueError as exc:
        return [f'{_S47_EVIDENCE} 不是合法 JSON：{type(exc).__name__}'], '', 0
    entries = [e for e in (doc.get('entries') or []) if isinstance(e, dict)]
    real = [e for e in entries if str(e.get('mode')) == 'restore']
    problems = []
    if not real:
        problems.append(f'台账 {len(entries)} 条里没有一条 mode=restore'
                        '（自检腿不能充当"真恢复过备份"，否则等于把"能力存在"当成"能力用过"）')
    # 生效面 vs 镜像面用同一把尺（与第 46 项目录对账那条同源）：**有 .git 的那份才是生效面**。
    # 两个交付镜像里的 backups/ 是另一批历史库，拿生效面今天产出的台账去镜像里要求同名文件在盘，
    # 是假阳（本轮实测：05 副本 11 份 .db 全是 demo-reset 系列，不含台账点名的两份）。
    # 但镜像面也不许自称已复算 —— 范围必须印出来，让读 PASS 的人知道这条账在谁身上。
    # 台账必须**自述是哪个副本产的**（`face`）：三个副本的 `backups/` 装的是不同批次的库，
    # 拿生效面今天的账去镜像里要求同名文件在盘上是假阳；靠"有没有 .git"猜也不成立
    # ——开发副本本身就在仓里。本轮实测两轮才收敛到这个口径。
    this_face = _portable_root(root)
    bad, newest, external = [], '', 0
    for e in real:
        face = str(e.get('face') or '')
        if face and face != this_face:
            external += 1                     # 别的副本产的账：只报面，不在本副本复算
            continue
        if not face:
            bad.append('该条账没有 face，无法判定它属于哪个副本（新台账必填）')
            continue
        name = str(e.get('backup') or '')
        # 路径按台账里记的**可移植路径**解析（仓内相对／仓外绝对），回落到 `backups/<名>`：
        # 只按 backups/ 拼会让"恢复过别处的备份"这件真事永远复算不出来。
        stored = str(e.get('backup_path') or '').strip()
        bak = (stored if os.path.isabs(stored) else os.path.join(root, stored)) if stored             else os.path.join(root, 'backups', name)
        if not os.path.isfile(bak):
            bad.append(f'{name} 已不在盘上')
            continue
        elif _sha256_file(bak) != e.get('backup_sha256'):
            bad.append(f'{name} 的字节与台账 sha 不一致')
        if int(e.get('row_total') or 0) <= 0:
            bad.append(f'{name} row_total={e.get("row_total")}（零输入不得记账）')
        if e.get('forced') and not e.get('rollback_anchor'):
            bad.append(f'{name} 用 --force 覆盖却没留回滚锚点名')
        newest = max(newest, str(e.get('at') or ''))
    if bad:
        problems.append(f'恢复台账有 {len(bad)} 条不可复算：{bad[:3]}')
    age = 0
    if newest:
        from datetime import UTC, datetime as _dt
        try:
            then = _dt.fromisoformat(newest)
            # 混合时刻是这里最容易翻车的地方：台账里的 at 可能带偏移也可能不带，
            # aware 减 naive 直接 TypeError（本轮实测）。统一归一到 UTC 再比。
            then = then.replace(tzinfo=UTC) if then.tzinfo is None else then
            age = (_dt.now(UTC) - then).days
        except ValueError:
            problems.append(f'最新一条的 at 不是可读 ISO 时刻：{newest!r}')
            newest = ''
    if age > _S47_MAX_AGE_DAYS and newest:
        problems.append(f'最近一次恢复已在 {age} 天前（上限 {_S47_MAX_AGE_DAYS} 天）'
                        f'⇒ 重跑：python {_S47_RESTORE_SCRIPT} --backup backups/<最新>.db')
    scope = (f'本面（{this_face or "仓根"}）逐条复算'
             + (f'；另有 {external} 条属别的副本，此处不复算' if external else ''))
    shown = f'{newest}（{age} 天前）' if newest else f'本面无自有账目（{external} 条都在别的副本）'
    return problems, f'{shown}｜{scope}', len(real)


def _s47_roundtrip():
    """真跑一次往返 + 一次受控坏输入：门禁必须同时拿到绿的一侧和红的一侧。"""
    try:
        import restore_db as rd
    except ImportError as exc:
        return [f'{_S47_RESTORE_SCRIPT} 导入失败：{type(exc).__name__}: {exc}'], []
    with tempfile.TemporaryDirectory(prefix='gate47_') as tmp:
        ok, detail = rd.selftest_roundtrip(tmp)
        if not ok:
            return [f'往返自检失败（备份载不回 / 行数不等 / 未拒覆盖）：{detail}'], []
        src = os.path.join(tmp, 'bad.db')
        with open(src, 'wb') as fh:
            fh.write(b'\x00' * 8192)                    # 全零 8KB：合法字节数、非法库
        verdict, why = rd.inspect(src, '备份')
        if verdict is not None:
            return ['坏输入样本（全零 8KB）被判成可恢复对象 ⇒ 空/坏守卫没牙'], []
        return [], [f'往返实测 {detail}；受控坏输入当场被拒（{str(why)[:46]}）']


_S50_REV_VALUES = ('auto', 'manual', 'none')
_S50_MIN_NOTE = 12
_S50_ROLLBACK_HEADING = '## 回滚'


def _s50_parse_decls(led_src):
    """从 `schema_ledger.py` **源码文本**取声明五元组（不 import：该模块属 src 包，
    门禁在交付副本里跑时 import 它会牵出 Flask 上下文依赖）。"""
    out = []
    for m in re.finditer(r"PatchRecord\(\s*'([^']+)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,"
                         r"\s*'([^']*)'\s*,\s*'([^']*)'\s*\)", led_src or ''):
        pid, _title, kind, rev, note = m.groups()
        out.append({'pid': pid, 'kind': kind, 'rev': rev, 'note': note})
    return out


def _s50_decl_problems(recs):
    """声明侧自洽（**纯函数**，L4 用合成名单打反例，不需要真库也不需要改盘上文件）。"""
    problems = []
    if not recs:
        return ['一条补丁声明都解析不到 ⇒ 判据面无输入，不得记通过']
    for rec in recs:
        pid = rec['pid']
        if rec['rev'] not in _S50_REV_VALUES:
            problems.append(f"{pid}：rev={rec['rev']!r} 不在值域 {_S50_REV_VALUES}")
        if len(rec['note']) < _S50_MIN_NOTE:
            problems.append(f"{pid}：rev_note {len(rec['note'])} 字 < 下限 {_S50_MIN_NOTE}"
                            '⇒ 只写等级不写代价，等于没声明')
    # pid 重复/命名形状归第 24 项管（同一事实只许一处判；两处都判会互为替身，
    # 撤掉任一处另一处仍绿 ⇒ 变异测试恒不翻红）。
    return problems


def _s50_reverser_keys(init_src):
    """取 `init_db._PATCH_REVERSERS` 的键集合；找不到绑定表返回 None（区别于"表在但为空"）。"""
    blk = re.search(r'_PATCH_REVERSERS\s*=\s*\{(.*?)\n\}', init_src or '', re.S)
    if blk is None:
        return None
    return set(re.findall(r"['\"](p\d{3}_[a-z0-9_]+)['\"]\s*:", blk.group(1)))


def _s50_doc_problems(upgrade_text, auto_pids):
    """UPGRADE.md 必须有「回滚」这一节，且把每一条 `auto` 与"撤不了怎么办"都写在里面。

    第 24 项已经逐条对过「UPGRADE.md 有没有写这个 pid」，这里**不重复那把尺**，
    只多要两件它测不到的东西：回滚这一节存在、且节内点名了每一条可自动撤的补丁与整库回退的去处。
    """
    problems = []
    if _S50_ROLLBACK_HEADING not in (upgrade_text or ''):
        return [f'UPGRADE.md 缺「回滚」章（{chr(96)}{_S50_ROLLBACK_HEADING}{chr(96)} 起头）'
                '⇒ 补丁只有正向路径，出事时没人知道往回怎么走']
    seg = upgrade_text.split(_S50_ROLLBACK_HEADING, 1)[1]
    nxt = seg.find('\n## ')
    seg = seg if nxt < 0 else seg[:nxt]
    missing = [p for p in sorted(auto_pids) if p not in seg]
    if missing:
        problems.append(f'回滚章没点名这些可自动撤的补丁：{missing}')
    if 'restore_db' not in seg:
        problems.append('回滚章没指路 restore_db（manual/none 档唯一的整库回退去处）')
    return problems


def check_patch_reversibility():
    """第 50 项：迁移补丁的**可逆性面** —— 声明 ⇄ 反向实现 ⇄ 文档三方同源。

    动因（2026-09-27 第 35 轮当场实测）：`PatchRecord` 只有 `pid/title/kind` 三格，
    `_PATCH_IMPLS` 只有正向执行体，`revert`/`user_version` 在 src+init_db 里 **0 命中**
    ⇒ "这条补丁能不能撤、撤的话代价是什么"在盘上没有任何一处答案。
    上游对照（本轮 `gh api` 实取原文）：
    · `django/django django/db/migrations/operations/special.py:151` 声明
      `reverse_code=None`，`:205-206` 在往回跑时 `raise NotImplementedError`
      —— **缺反向是一等错误，不是静默跳过**；
    · `sqlalchemy/alembic alembic/templates/generic/script.py.mako:21/:26`
      模板里 `upgrade()` / `downgrade()` 成对存在 ⇒ 每个迁移天生要写反向；
    · `InvenTree/InvenTree .github/workflows/migration_tests.yaml:76/:79` 把迁移测试做成
      独立作业且产物上传带 `if: always()` ⇒ 跑失败也留证据。
    本项目不引 Alembic（红线：免费层单进程、存量库已在跑，换机制的风险大于收益），
    只借它的**判据形状**：每一条补丁都必须自证"我这一档可逆性是什么、代价写在哪儿、反向动作是谁"。
    """
    problems, evidence = [], []
    led_src = read(os.path.join(SRC, 'schema_ledger.py'))
    init_src = read(os.path.join(ROOT, 'init_db.py'))
    recs = _s50_parse_decls(led_src)
    problems += _s50_decl_problems(recs)

    auto = {r['pid'] for r in recs if r['rev'] == 'auto'}
    keys = _s50_reverser_keys(init_src)
    if keys is None:
        problems.append('init_db.py 里找不到 _PATCH_REVERSERS 绑定表 ⇒ 无人能撤任何补丁（判"没接线"）')
    else:
        for p in sorted(auto - keys):
            problems.append(f'{p}：声明 rev=auto 但没有反向实现 ⇒ 回滚命令会在这里响，'
                            '但"能撤"这句已经写进声明了（骗人的声明比没声明更糟）')
        for p in sorted(keys - auto):
            problems.append(f'{p}：有反向实现却声明非 auto ⇒ 两条真相源（谁能撤 vs 声明说什么）')
        if auto:
            evidence.append(f'可自动撤 {len(auto)}/{len(recs)} 条：' + '、'.join(sorted(auto)))
        else:
            evidence.append('可自动撤 0 条 ⇒ 全部依赖整库回退（读数，不判红）')

    if 'def unrecord_pid' not in led_src:
        problems.append('schema_ledger 缺 unrecord_pid ⇒ 撤完补丁账本还记着 applied，'
                        '下一次迁移不会重跑（撤不干净）')
    if 'def pre_rollback_copy' not in read(os.path.join(ROOT, 'restore_db.py')):
        problems.append('缺 pre_rollback_copy 这个唯一锚点出口 ⇒ 回滚会各自造副本写法')
    up_path = os.path.join(ROOT, 'UPGRADE.md')
    if not os.path.isfile(up_path):
        problems.append('UPGRADE.md 缺失')
    else:
        problems += _s50_doc_problems(read(up_path), auto)
    kinds = {}
    for r in recs:
        kinds[r['rev']] = kinds.get(r['rev'], 0) + 1
    evidence.insert(0, f'声明 {len(recs)} 条，档位分布 {kinds}（值域 {"/".join(_S50_REV_VALUES)}）')
    record('BLOCK', '50. 迁移补丁可逆性（声明 ⇄ 反向实现 ⇄ 回滚文档 三方同源）',
           not problems, problems or evidence)


def check_restore_reversibility():
    """第 47 项：备份的**可恢复性** —— 写得出备份不等于恢复得了。

    动因（2026-09-27 第 33 轮当场实测，取数面 = `src/*.py` + `src/routes/*.py` + 根级脚本）：
    ① `def restore` 只有 `src/pack_io.py:restore_builtin` / `src/pack_store.py:restore_active`
       两处且都属行业包；`iterdump` 0 处、恢复用途的 `ATTACH` 0 处 ⇒ **数据库从来没有载回路径**；
    ② `backups/` 11 份 .db：有证据卡 1 份、被恢复过 0 份 ⇒ 第 38 项那句
       "回填态不得当可恢复凭据"至今没有兑现件；
    ③ 上游（本轮 `gh api` 实取原文）：discourse `lib/backup_restore.rb:19 restore!` 与 `:23 rollback!`
       成对出现、`:24 raise OperationRunningError if is_operation_running?`（恢复期互斥旗标）、
       `:59 allow_restore: Rails.env.development? || SiteSetting.allow_restore`
       （**恢复默认关，生产覆盖须显式开关**）、`:68 current_version`（版本核对通过才搬表）。
    """
    problems, evidence = [], []
    script = os.path.join(ROOT, _S47_RESTORE_SCRIPT)
    if not os.path.isfile(script):
        record('BLOCK', '47. 备份的可恢复性（载回 + 断言 + 回滚锚点 + 台账复算）',
               False, [f'缺 {_S47_RESTORE_SCRIPT} ⇒ 恢复出口不存在（判"没做过"，不判通过）'])
        return
    rt_p, rt_e = _s47_roundtrip()
    problems += rt_p
    evidence += rt_e
    problems += _s47_egress_problems(read(script))
    ev_p, stamp, n_real = _s47_evidence_problems(ROOT)
    problems += ev_p
    if n_real:
        evidence.append(f'恢复台账 {n_real} 条真恢复，最近一次 {stamp}（sha256 + row_total + 锚点按面复算）')
    record('BLOCK', '47. 备份的可恢复性（载回 + 断言 + 回滚锚点 + 台账复算）',
           not problems, problems or evidence)


_S48_CONTRACT = os.path.join('.ci', 'contract.json')
_S48_SKIP_LEDGER = os.path.join('.ci', 'prepush-skips.jsonl')
_S48_HOOK_CANDIDATES = (os.path.join('.git', 'hooks', 'pre-push'),
                        os.path.join('.githooks', 'pre-push'))


def _s48_repo_root():
    """从本副本上溯找 `.git`（交付镜像里也找得到真仓根；找不到即如实判"没测到"）。"""
    cur = ROOT
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, '.git')) or os.path.isfile(os.path.join(cur, '.git')):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _s48_contract_problems(root):
    """契约本身：在库、被跟踪、字段齐、声明的门禁命令文件真存在。

    刻意不复用技能仓的 `greencheck.py` —— 交付副本必须能在一台没装技能的机器上自证，
    否则这条判据的输入面就落在仓外（第 32 轮 ⑥ 那条立规的同族）。
    """
    path = os.path.join(root, _S48_CONTRACT)
    if not os.path.isfile(path):
        return [f'缺 {_S48_CONTRACT} ⇒ 没有"谁在拦推送"的声明，'
                f'补闸：python <A-project-handoff>/scripts/greencheck.py bootstrap '
                f'--repo-dir "{root}" --apply --install-hook'], None
    try:
        doc = json.loads(read(path))
    except ValueError as exc:
        return [f'{_S48_CONTRACT} 不是合法 JSON：{type(exc).__name__}'], None
    problems = []
    checks = doc.get('checks')
    if not isinstance(checks, list) or not checks:
        problems.append('契约 checks 为空 ⇒ 无对象可判，不得算绿')
        checks = []
    for c in checks:
        if not isinstance(c, dict) or not c.get('name') or not c.get('cmd'):
            problems.append(f'契约有检查项缺 name/cmd：{c!r}')
    if not isinstance(doc.get('refresh_days'), int) or doc.get('refresh_days', 0) <= 0:
        problems.append('契约缺合法 refresh_days（保质期要写在产物里，不回落代码常量）')
    named = [c.get('cmd', '') for c in checks if isinstance(c, dict)]
    if not any('quality_gate.py' in str(cmd) for cmd in named):
        problems.append('契约里没有一项声明跑 quality_gate.py ⇒ "门禁接进阻断链"是空话')
    tracked = subprocess.run(['git', '-C', root, 'ls-files', '--', _S48_CONTRACT],
                             capture_output=True, text=True)
    if not (tracked.stdout or '').strip():
        problems.append(f'{_S48_CONTRACT} 未被 git 跟踪 ⇒ 契约即代码，不入库就等于"只在这台机器上有效"')
    return problems, doc


def _s48_hook_problems(root):
    """在役钩子：存在、按**脚本路径**且按**未被注释的行**引用门禁、且不是无条件 exit 0。"""
    hook = next((os.path.join(root, h) for h in _S48_HOOK_CANDIDATES
                 if os.path.isfile(os.path.join(root, h))), None)
    if hook is None:
        return ['pre-push 钩子不在位 ⇒ 推送无人拦（CI 拿不到 runner 时，本地是唯一强制点）'
                '；补闸：python <A-project-handoff>/scripts/greencheck.py bootstrap '
                f'--repo-dir "{root}" --apply --install-hook'], None
    text = read(hook)
    # 「这行里有 quality_gate.py」不等于「这行会执行」：整行前加 `#` 就等于把闸拆了，
    # 而按子串判照样绿 —— 第 32 轮在 CI 接线那条上已经栽过一次，本轮在新判据里复发。
    live = [ln for ln in text.splitlines()
            if 'quality_gate.py' in ln and not ln.strip().startswith('#')]
    problems = []
    if not live:
        problems.append('pre-push 没有按路径**执行**门禁（注释掉的行不算接线；'
                        '在册不等于在跑）')
    if re.search(r'^\s*(exit 0|true)\s*$', text, re.M) and not live:
        problems.append('pre-push 只有一句无条件 exit 0 ⇒ 是个空钩子')
    return problems, os.path.relpath(hook, root)


def _s48_skip_problems(root):
    """逃逸台账：每次跳过必须留 reason + 时刻 + 谁；空头跳过即红。"""
    path = os.path.join(root, _S48_SKIP_LEDGER)
    if not os.path.isfile(path):
        return [], 0
    problems, rows = [], 0
    for ln in read(path).splitlines():
        ln = ln.strip()
        if not ln:
            continue
        rows += 1
        try:
            doc = json.loads(ln)
        except ValueError:
            problems.append(f'{_S48_SKIP_LEDGER} 有不可解析行（混合格式必须逐行判）')
            continue
        if not str(doc.get('reason') or '').strip():
            problems.append(f'第 {rows} 条跳过没有 reason ⇒ 逃生门必须留下为什么')
        if not str(doc.get('ts') or '').strip():
            problems.append(f'第 {rows} 条跳过没有 ts（无时刻的账无法与推送对账）')
    return problems, rows


def _s48_head_is_ancestor(root, older, newer):
    """older 是否为 newer 的祖先（含相等）。取不到数 ⇒ None（判"没测到"，不判通过）。"""
    if not older or not newer:
        return None
    if older == newer:
        return True
    r = subprocess.run(['git', '-C', root, 'merge-base', '--is-ancestor', older, newer],
                       capture_output=True, text=True)
    if r.returncode not in (0, 1):
        return None
    return r.returncode == 0


def _s48_receipt_problems(root, enf):
    """不装钩子时的替代强制点：新鲜的本地运行回执，且回执覆盖当前提交（HEAD 是祖先）。"""
    rel = str(enf.get('receipt') or os.path.join('.ci', 'local-run-receipt.json'))
    verdict = ''
    path = os.path.join(root, rel.replace('/', os.sep))
    max_days = enf.get('receipt_max_days')
    if not isinstance(max_days, int) or max_days <= 0:
        return [f'契约 enforcement.receipt_max_days 非法（{max_days!r}）⇒ 回执保质期不许回落成常量'], ''
    if not os.path.isfile(path):
        return [f'缺本地运行回执 {rel}：无钩子又无回执 ⇒ 没有任何东西被证明跑过'
                '；处置：按契约 enforcement.receipt_cmd 跑一次（命令是数据，写在小数里，'
                '不硬编码进门禁源码 —— 防成环那条 S16 同一把尺）'], ''
    try:
        doc = json.loads(read(path))
    except ValueError as exc:
        return [f'{rel} 不是合法 JSON：{type(exc).__name__}'], ''
    problems = []
    at = str(doc.get('at') or '')
    try:
        from datetime import UTC, datetime as _dt
        then = _dt.fromisoformat(at)
        then = then.replace(tzinfo=UTC) if then.tzinfo is None else then
        age = (_dt.now(UTC) - then).days
    except ValueError:
        return [f'{rel} 的 at 不可解析（{at!r}）⇒ 无时刻的回执等于没有回执'], ''
    if age > max_days:
        problems.append(f'本地回执已 {age} 天未刷新（上限 {max_days} 天）⇒ 按契约 enforcement.receipt_cmd 重跑并带 --receipt')
    # 回执的**结论**不参与本项裁决，只作为读数印出来：
    # 回执是"跑过这条链"的产物，而这条链里就含本项 —— 拿它的 verdict 当裁决会自指成环
    # （无回执⇒红⇒跑一次⇒回执=fail⇒仍红，永远出不来绿）。本轮 L4 抓到后改的这条。
    verdict = str(doc.get('verdict') or '')
    if verdict in ('usage', 'notest'):
        problems.append(f'最近一次本地闸根本没跑成（verdict={verdict!r}）⇒ 未执行的运行不构成闸')
    st = [s for s in (doc.get('stages') or []) if isinstance(s, dict)]
    verdict = str(doc.get('verdict') or '')
    if not st:
        problems.append(f'{rel} 里没有任何阶段读数 ⇒ 空回执不得算跑过（零输入不判绿）')
    cur = subprocess.run(['git', '-C', root, 'rev-parse', 'HEAD'],
                         capture_output=True, text=True).stdout.strip()
    anc = _s48_head_is_ancestor(root, str(doc.get('head') or ''), cur)
    if anc is None:
        problems.append(f'回执 HEAD 与当前 HEAD 的祖先关系判不了（git 取不到数）⇒ 判"没测到"')
    elif not anc:
        problems.append(f'回执 HEAD {str(doc.get("head"))[:7]} 不是当前 {cur[:7]} 的祖先'
                        ' ⇒ 这份回执不覆盖现在要推的东西（吃老本）')
    return problems, verdict


_S48_REASON_MIN = 20


def _s48_enforcement_problems(root, contract):
    """阻断链的两种合法形状：① pre-push 钩子按路径**执行**门禁；
    ② 契约里写清"为什么不装钩子"（≥20 字）+ 一张新鲜的本地运行回执。

    为什么必须两种都收（本仓一手）：仓内本地验证入口头部第 18—19 行的既有裁定是
    "不装 git hook —— 本仓常有并发会话在提交，装了会把别人的提交一起挡住；要装应由用户决定并显式装"。
    若判据只认钩子，诚实回答会被判红、而随手 `exit 0` 的假钩子能过 ⇒ 判据在逼虚报。
    另外 GitHub Actions 因账号额度结构性不可用（job 108580122204 startedAt=None/steps=0），
    所以"远端会拦"这句假话不能再用一次。
    """
    hook_p, hook_rel = _s48_hook_problems(root)
    if hook_rel is not None:
        # 钩子在位：只看形状。"存在"与"不在位"必须是两种可分辨的形，
        # 否则一份没有钩子的仓会被报成"钩子坏了"（本轮 L4 当场抓出）。
        if hook_p:
            return ['pre-push 存在但形状不合规：' + '；'.join(hook_p)], []
        return [], [f'阻断链形状=pre-push 钩子（{hook_rel}，按路径执行门禁）']
    enf = (contract or {}).get('enforcement') or {}
    reason = str(enf.get('no_hook_reason') or '').strip()
    problems = []
    if len(reason) < _S48_REASON_MIN:
        problems.append(f'契约 enforcement.no_hook_reason 不足 {_S48_REASON_MIN} 字或缺失'
                        f'（现状 {reason[:24]!r}）⇒ 不装钩子必须留下为什么，'
                        '且不得与"钩子坏了"同形')
    rp, verdict = _s48_receipt_problems(root, enf)
    problems += rp
    ev = [] if problems else ['阻断链形状=本地回执（无钩子，理由已入契约）']
    if verdict:
        ev.append(f'本地回执结论={verdict}（结论只报不重复定罪：回执里就含本项，拿它裁决会自指成环）')
    return problems, ev


def check_commit_block_chain():
    """第 48 项：提交/推送的阻断链是否真的在位 —— 门禁跑得快，但没人被它拦住过。

    动因（2026-09-27 第 33 轮当场实测）：
    ① `gh run view --json conclusion` 实测最近三次推送（含本轮前一提交）全部 `failure`，
       job `108580122204` 的 `startedAt=None / steps=0`，check-run 注解原文：
       "The job was not started because recent account payments have failed or your
        spending limit needs to be increased." ⇒ 远端闸**结构性不可用**（M42 第 18 次）；
    ② 而本仓 `git config core.hooksPath` 未设、`.git/hooks/pre-push` 与 `.ci/contract.json`
       都不存在 ⇒ 远端不可用时，本地也没有第二道闸 ⇒ 47 项判据全部只在"有人记得跑"时生效；
    ③ 上游对照（本轮 `gh api` 实取）：superset `.pre-commit-config.yaml:63-67`
       （`repo: local` + `entry: ./scripts/oxfmt.sh` —— 仓自带脚本挂进本地钩子）、
       `:53-55`（check-docstring-first / debug-statements / end-of-file-fixer 这类**便宜的结构检查**
       放本地，重的放 CI）；airflow `.pre-commit-config.yaml` 1556 行同构；
       InvenTree 直接把迁移测试做成独立作业 `.github/workflows/migration_tests.yaml:53`（SQLite）
       与 `:86`（MySQL）—— 本地/CI 同一入口，不靠人记。
    """
    problems, evidence = [], []
    root = _s48_repo_root()
    if root is None:
        record('BLOCK', '48. 提交阻断链（契约 + 钩子或本地回执 + 逃逸台账）',
               False, ['上溯 6 层找不到 .git ⇒ 无法判定阻断链是否在位（判"没测到"）'])
        return
    cp, cdoc = _s48_contract_problems(root)
    problems += cp
    if cdoc:
        evidence.append(f'契约 {len(cdoc.get("checks") or [])} 项检查 / refresh_days='
                        f'{cdoc.get("refresh_days")}（仓根 {os.path.basename(root)}）')
    ep, ee = _s48_enforcement_problems(root, cdoc)
    problems += ep
    evidence += ee
    sp, n_skip = _s48_skip_problems(root)
    problems += sp
    evidence.append(f'逃逸台账 {n_skip} 条（每条须带 reason+ts，缺即红）')
    record('BLOCK', '48. 提交阻断链（契约 + 钩子或本地回执 + 逃逸台账）',
           not problems, problems or evidence)


_S49_RUNS = os.path.join('.ci', 'local-runs.jsonl')
_S49_RECEIPT = os.path.join('.ci', 'local-run-receipt.json')
_S49_MAX_ROWS = 60
_S49_KEYS = ('head', 'at', 'verdict', 'rc', 'stages', 'seq')


def _s49_rows(text):
    """账本文本 → 合法行列表（纯函数，L4 用合成文本打反例）。返回 (rows, problems)。"""
    rows, problems = [], []
    prev_seq = 0
    for ln in (text or '').splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            doc = json.loads(ln)
        except ValueError:
            problems.append('运行账本有不可解析行（混合格式必须逐行判，不许当作没发生）')
            continue
        if not isinstance(doc, dict):
            problems.append(f'账本某行不是对象：{ln[:40]!r}')
            continue
        lack = [k for k in _S49_KEYS if k not in doc]
        if lack:
            problems.append(f'账本行缺键 {lack}（每行必须自证 head/at/verdict/rc/stages/seq）')
            continue
        if not isinstance(doc.get('stages'), list) or not doc['stages']:
            problems.append('账本行的 stages 为空 ⇒ 一次什么都没测的运行不得记账')
            continue
        try:
            seq = int(doc['seq'])
        except (TypeError, ValueError):
            problems.append(f"账本行 seq={doc['seq']!r} 不是整数 ⇒ 无法判递增")
            continue
        if seq <= prev_seq:
            problems.append(f'账本 seq 未递增（{seq} 接在 {prev_seq} 之后）'
                            '⇒ 要么被重排/手改，要么写盘方绕过追加通道')
            continue
        prev_seq = seq
        rows.append(doc)
    return rows, problems


def _s49_consistency(rows, receipt):
    """账本末行必须与回执同源同次（分两处写、或事后手改回执，都会在这里露馅）。"""
    if not rows or not isinstance(receipt, dict):
        return ['账本或回执缺一 ⇒ 无法证明二者同源'], ''
    tail = rows[-1]
    bad = [k for k in ('head', 'at', 'verdict', 'rc') if tail.get(k) != receipt.get(k)]
    if bad:
        return [f'账本末行与回执在 {bad} 上不一致 ⇒ 要么旁路写盘、要么事后手改'], ''
    return [], (f"末行 head={str(tail.get('head'))[:7]} verdict={tail.get('verdict')} "
                f"mode={tail.get('mode')} 阶段 {len(tail.get('stages') or [])} 个")


def _s49_coverage(rows, head, root=None, max_age_h=72):
    """覆盖读数（**只报不拦**）：全量本地验证约 8 分钟，拦在每次提交会连坐并发会话。

    第 35 轮 M97 改成三态而不是"精确匹配一个数"。旧口径只数 `head == 当前 HEAD` 的行，
    于是提交一落地读数立刻归零（本轮实测：savepoint 提交后 covered 从 1/6 变 0/6），
    而"0"同时可能是三种完全不同的处境 —— 把它读成"从没跑过"就是错的。
    三态：命中当前 HEAD（真新鲜）/ 在当前 HEAD 的祖先线上（跑过，但没验过**这次**提交）/
    离线（那行属于别_branch/改写过的历史，不得拿来给当前提交背书）。
    `root=None` 时退化为"只报精确匹配"，让纯函数在没 git 的面上也可测（不猜、不判红）。
    """
    if not rows:
        return '账本为空 ⇒ 覆盖未验证（UNVERIFIED）', {}
    cur = str(head)
    covered, ancestor, offline, unknown = [], 0, 0, 0
    for r in rows:
        if str(r.get('head')) == cur:
            covered.append(r)
            continue
        if root is None:
            unknown += 1
            continue
        state = _s48_head_is_ancestor(root, str(r.get('head')), cur)
        if state is None:
            unknown += 1
        elif state:
            ancestor += 1
        else:
            offline += 1
    try:
        from datetime import UTC, datetime as _dt
        then = _dt.fromisoformat(str(rows[-1].get('at')))
        then = then.replace(tzinfo=UTC) if then.tzinfo is None else then
        age_h = (_dt.now(UTC) - then).total_seconds() / 3600.0
    except ValueError:
        age_h = -1.0
    tally = {}
    for r in rows:
        tally[str(r.get('verdict'))] = tally.get(str(r.get('verdict')), 0) + 1
    fresh = '新鲜' if 0 <= age_h <= max_age_h else (f'已 {age_h:.0f}h 未刷新' if age_h >= 0 else '时刻不可解析')
    gap = ''
    if not covered and root is not None and rows:
        # 距离按**传入的那个 head** 算，不按仓的 HEAD 算：调用方给谁就答谁（临时仓夹具里两者
        # 不总是一致，写死 HEAD 会让夹具测到的是"仓库此刻"而不是"被测对象"）
        r = subprocess.run(['git', '-C', root, 'rev-list', '--count',
                            '{}..{}'.format(rows[-1].get('head'), cur)],
                           capture_output=True, text=True)
        n = (r.stdout or '').strip()
        gap = f'｜当前 HEAD 距上次真跑 {n} 个提交' if r.returncode == 0 and n.isdigit() else '｜提交距离取不到'
    extra = (f'｜祖先线 {ancestor} 行' if root is not None else '') + \
            (f'｜离线 {offline} 行' if offline else '') + \
            (f'｜无法判定 {unknown} 行' if unknown else '')
    return (f'覆盖 {len(covered)}/{len(rows)} 行命中当前 HEAD（{fresh}，拦不拦另说）'
            f'{extra}{gap}｜结论分布 {tally}'), \
           {'age_h': age_h, 'covered': len(covered), 'ancestor': ancestor,
            'offline': offline, 'unknown': unknown}


def _s51_segment(src, name):
    """取一个模块级函数的源码段（从 `def name(` 到下一个 `^def ` / `^@` / 文件尾）。

    取不到 ⇒ 返回 None，调用方**必须**把它当成"这一维没测到"而不是"没有违规"。
    """
    m = re.search(r'^def %s\(.*?(?=^def |^@|\Z)' % re.escape(name), src, re.S | re.M)
    return m.group(0) if m else None


def _s51_state_contract(rd_seg, elig_seg):
    """资格状态枚举 ⇄ `eligibility()` 实际返回的字面量 双向对账（纯字符串面，可在任何机器上判）。

    要防的两类漂移：① 枚举里写了一个状态而实现从不返回（L4 夹具会为它造一条"永远不可能出现"的
    反例，判据永久绿）；② 实现返回了一个枚举里没有的状态（下游按名单分档时会静默落到"未知"）。
    `REFUSAL_STATES` 必须是 `ELIG_STATES[1:]` 的派生写法 —— 手抄一份就是第二套真相源。
    """
    problems = []
    if rd_seg is None or elig_seg is None:
        return ['看不见 restore_db 里的 ELIG_STATES 或 eligibility() ⇒ 本维没测到，不判通过']
    m = re.search(r'^ELIG_STATES\s*=\s*\(([^)]*)\)', rd_seg, re.M)
    if not m:
        return ['ELIG_STATES 不是元组字面量 ⇒ 状态名单失去了唯一真相源']
    declared = [x.strip().strip("'\"") for x in m.group(1).split(',') if x.strip()]
    if not declared or declared[0] != 'eligible':
        problems.append(f'ELIG_STATES 首项必须是 eligible（"够格"是判定的默认出口），实测 {declared[:1]}')
    returned = set(re.findall(r"return\s+'([a-z_]+)'", elig_seg))
    ghost = sorted(set(declared) - returned - {'eligible'})
    unlisted = sorted(returned - set(declared))
    if ghost:
        problems.append(f'枚举里声明了却从不返回的状态（夹具会为它造出永不成立的反例）：{ghost}')
    if unlisted:
        problems.append(f'return 出来的状态不在枚举名单里：{unlisted}')
    if 'eligible' not in returned:
        problems.append('eligibility() 没有任何一条 return eligible ⇒ 全仓没有一份备份能被自动选中')
    if not re.search(r'^REFUSAL_STATES\s*=\s*ELIG_STATES\[1:\]', rd_seg, re.M):
        problems.append('REFUSAL_STATES 未由 ELIG_STATES 派生（手抄名单必然滞后于人口）')
    return problems


def _s51_wiring(rd_seg, init_seg):
    """选择出口只允许一处实现：`pick_latest_eligible` 读已排形的清单头，调用方不得重算资格。"""
    problems = []
    if rd_seg is None:
        return ['看不见 restore_db.backup_records/pick_latest_eligible 所在源码 ⇒ 本维没测到']
    if 'def pick_latest_eligible(' not in rd_seg:
        problems.append('restore_db 里没有 pick_latest_eligible ⇒ "挑哪一份"又回到人记命令')
    if init_seg is None:
        return problems + ['看不见 init_db.rollback_via_backup ⇒ 本维没测到']
    if 'pick_latest_eligible(' not in init_seg:
        problems.append('rollback_via_backup 未调用 pick_latest_eligible ⇒ 它在自己那侧另算了一套资格（两处判定必漂移）')
    if re.search(r'\beligibility\(', init_seg):
        problems.append('init_db 侧直接调 eligibility() ⇒ 同一事实第二处判（应只经 pick_latest_eligible 消费）')
    # 方向守卫：载回"不早于补丁"的备份撤不掉补丁，所以拒绝条件必须是 >=（含相等）。
    # 写成 < / <= 的那一版看着同样像自卫，实际把"同一秒的备份"放行。
    if not re.search(r'>?=\s*applied_dt|name_ts\([a-z_]+\)\s*>=\s*applied_dt', init_seg):
        problems.append('rollback_via_backup 缺少"备份时刻 >= 补丁应用时刻即拒"的方向守卫')
    if re.search(r'<=\s*applied_dt|<\s*applied_dt', init_seg):
        problems.append('rollback_via_backup 里出现 <= / < 与 applied_dt 比较（方向反了：会放行已含该补丁的备份）')
    return problems


def _s51_population_problems(records):
    """读盘后的**形状自证**：只判"代码把自己算错了"，不判"这台机器有没有备份"。

    人口本身是 gitignore 的机器本地物（实测 `backups/` 跟踪数 0），所以"零人口"在这台机器上
    合法 —— 它只能记成 UNVERIFIED，绝不能判红（一次正常提交变不了绿的判据没资格当闸，
    而且 CI 上永远没有这些 .db）。但下面这些是**谓词自身不一致**，在任何人口上都该成立。
    """
    problems = []
    for r in records:
        st, man = r.get('state'), r.get('manifest')
        if st == 'eligible' and not man:
            problems.append(f'{r.get("name")}：判 eligible 却没有证据卡（谓词与实现不一致）')
        if st == 'no_manifest' and man:
            problems.append(f'{r.get("name")}：判 no_manifest 但证据卡读到了（read_manifest 口径漂移）')
        if st == 'nameless' and r.get('name_ts'):
            problems.append(f'{r.get("name")}：判 nameless 却解析出了时刻（两处取数用了不同的尺）')
    ts = [r.get('name_ts') for r in records if r.get('name_ts')]
    if ts != sorted(ts, reverse=True):
        problems.append('清单未按"名字里的时刻 新→旧"排序 ⇒ latest 就不是最新的那份')
    return problems


def check_backup_eligibility():
    """第 51 项：备份**资格判定面** —— 枚举 ⇄ 实现 ⇄ 调用点三方同源 + 读盘形状自证（BLOCK）。

    动因（2026-09-28 第 36 轮当场实测）：`restore_db.py` 全仓**没有"挑一份可用的"出口**，
    而 `latest_evidence()`(:268) 读的是恢复史台账、不是备份人口 —— 此前被当作一回事。
    实测人口 `backups/*.db` 14 件 / 带证据卡 1 件，且**最新那份没有证据卡**
    （`ican_before_p007_20260927_094600.db` → no_manifest，够格的是 20h 前那份）。
    ⇒ "出事时挑最近一份"这句话里，"最近"能算，"可用"此前无处可查。

    上游对照（本轮 `gh api` 实取原文行号，落 D:/tmp/bench36/）：
      discourse `lib/backup_restore/backup_store.rb:27 latest_file = files.first`
        —— 选择=读排好形的清单头，判定在造清单时做完；
      同目录 `local_backup_store.rb:53 unsorted_files`（扩展名白名单）+ `:70 create_file_from_path`
        （每条是 filename/size/last_modified 的类型化记录，不是裸路径）；
      frappe `frappe/utils/backups.py:303 backup_time`（**按文件名里的时刻排序**，不按 mtime）
        + `:741 is_file_old`（路径不存在按"太旧"处理 = 缺证据即不合格）；
      restic `internal/repository/checker.go:217/:227/:237`
        —— 声明⇄实测的差集分 missing / size 不符 / orphaned **三类各出一行**，不折叠。
    """
    problems, notes = [], []
    try:
        rd_src = open(os.path.join(ROOT, 'restore_db.py'), encoding='utf-8').read()
    except OSError as exc:
        rd_src = ''
        problems.append(f'读不到 restore_db.py：{type(exc).__name__}')
    try:
        init_src = open(os.path.join(ROOT, 'init_db.py'), encoding='utf-8').read()
    except OSError as exc:
        init_src = ''
        problems.append(f'读不到 init_db.py：{type(exc).__name__}')
    problems += _s51_state_contract(rd_src, _s51_segment(rd_src, 'eligibility'))
    problems += _s51_wiring(rd_src, _s51_segment(init_src, 'rollback_via_backup'))
    records = []
    if rd_src and not problems:
        try:
            if ROOT not in sys.path:
                sys.path.insert(0, ROOT)
            import restore_db
            records = restore_db.backup_records(root=ROOT)
        except Exception as exc:              # noqa: BLE001 读盘失败要把原因说出来，不是当没备份
            problems.append(f'读备份人口失败：{type(exc).__name__}: {exc}')
    problems += _s51_population_problems(records)
    ok_n = sum(1 for r in records if r['state'] == 'eligible')
    if records:
        notes.append(f'人口 {len(records)} 件 / 够格 {ok_n} 件（证据卡覆盖率 {ok_n}/{len(records)}）')
    else:
        notes.append('人口 0 件 ⇒ 本面记 UNVERIFIED（backups/ 是 gitignore 的机器本地物，'
                     'CI 上没有；所以这条只判谓词一致性，不判这台机器存不存在备份）')
    detail = problems + notes
    record('BLOCK', '51. 备份资格判定面（状态枚举 ⇄ eligibility 实现 ⇄ 调用点 三方同源 + 读盘形状自证）',
           not problems, detail or ['资格名单与实现一致，选择出口唯一，方向守卫在位'])


def check_backup_retention():
    """第 53 项：备份**保留体检** —— 族从生产者源码现取 + 生产者⇄通道对账 + 三把尺必须见面（BLOCK）。

    动因（2026-09-28 第 38 轮当场实测，人口仍由第 51 项那把尺 `backup_records()` 给出）：
      盘上 14 件里 `ican_*` 6 件（`backup_db.cleanup` 射程，keep=30）、`demo-reset_*` 3 件
      （`pack_demo._prune_backups` 射程，BACKUP_KEEP=10）、**族外 5 件无主**
      （`PH2-*` 4 + `ican-db-before-cisim-*` 1）；两条通道各自都没触发过淘汰，
      而资格面第三把尺 `MAX_AGE_H=72` 与两个"份数上限"从不对账 ⇒
      「账面留 30 份」与「够格载回只有 1 件」同时为真，此前没有任何一处把这两件事放在一起。
      上一轮我登记的 M114 原文写的是"射程只认一种命名"——本轮实测**否证了一半**：
      每条通道各自认自己的族，真缺口是**没有跨族视角、也没有份数尺与小时尺的对账**。

    上游对照（本轮 `gh api` 取回原文落 D:/tmp/bench38/，行号逐行读过）：
      discourse/discourse `lib/backup_restore/backup_store.rb:36-43`（计数尺 maximum_backups，
        先 `:37 return unless cleanup_allowed?`）与 `:45-53 delete_prior_to_n_days`
        （时间尺 remove_older_backups，`:47` 先验窗口为正、`:48` 再过守卫）——
        **两把尺写在同一个类里，各自带守卫**；`:100-102 used_bytes` 让存储自报足迹。
      frappe/frappe `frappe/utils/backups.py:630 delete_temp_backups(older_than=23)` /
        `:644 is_file_old(…, older_than=24)` 按小时；`:276-277` 从文件名解析
        `%Y%m%d_%H%M%S` ⇒ 与 `restore_db.name_ts()` 同一取向（不靠 mtime）。
    """
    problems, evidence = [], []
    try:
        import restore_db as rd
    except Exception as exc:                        # noqa: BLE001 取不到模块就是没测到
        record('BLOCK', '53. 备份保留体检（族⇄生产者对账 + 三把尺对账 + 只读承诺在册）', False,
               [f'import restore_db 失败：{type(exc).__name__}: {exc} ⇒ 判没测到，不判通过'])
        return
    for fn in ('retention_channels', 'retention_report', 'retention_reconciliation',
               'print_retention', '_read_src'):
        if not callable(getattr(rd, fn, None)):
            problems.append(f'restore_db 缺 {fn}() ⇒ 体检链断了（别把"没实现"写成"已接"）')
    if problems:
        record('BLOCK', '53. 备份保留体检（族⇄生产者对账 + 三把尺对账 + 只读承诺在册）',
               False, problems)
        return

    # ① 通道必须从生产者源码现取，且每条都带 patterns + 在册的份数尺名
    try:
        chans = rd.retention_channels(root=ROOT)
    except rd.RetentionError as exc:
        record('BLOCK', '53. 备份保留体检（族⇄生产者对账 + 三把尺对账 + 只读承诺在册）',
               False, [f'射程无法核对：{exc} ⇒ 禁止退化成"族外为零=健康"'])
        return
    if len(chans) < 2:
        problems.append(f'淘汰通道只数到 {len(chans)} 条（实测至少 backup_db 与 pack_demo 两条）'
                        '⇒ 判枚举器失明，不判"就一条"')
    for c in chans:
        if not c.get('patterns') or not any('*' in p for p in c['patterns']):
            problems.append(f"通道 {c['channel']} 没有通配模式 {c.get('patterns')} ⇒ 它不划族，分母会漏")
        if not isinstance(c.get('keep'), int) or c['keep'] < 1:
            problems.append(f"通道 {c['channel']} 的份数尺非法：{c.get('knob')}={c.get('keep')}")
        if not c.get('knob'):
            problems.append(f"通道 {c['channel']} 的 keep 没有常数名 ⇒ 匿名常数无法核对（本轮已把 "
                            "backup_db 的 `return 30` 升成 DEFAULT_KEEP）")

    # ② 生产者 ⇄ 通道：任何"往 backups/ 写"的模块都必须在册（新增未登记 ⇒ 红，可自愈）
    producers = _s53_producers()
    if not producers:
        problems.append('一个往 backups/ 写文件的模块都没数到 ⇒ 判生产者枚举器失明（不得记通过）')
    known = {c['producer'] for c in chans}
    ghost = sorted(producers - known)
    if ghost:
        problems.append(f'这些生产者往备份目录写文件却没有淘汰通道：{ghost} ⇒ 要么给通道，'
                        '要么在 retention_channels 里显式登记（族外=无人管）')
    orphan = sorted(known - producers)
    if orphan:
        problems.append(f'通道在册但它对应的生产者已不再写备份目录：{orphan} ⇒ 名单比现实胖，会掩盖新生产者')

    # ③ 三把尺必须见面：给出对账读数，且 verdict 必须是三态之一
    try:
        rep = rd.retention_report(root=ROOT)
    except rd.RetentionError as exc:
        record('BLOCK', '53. 备份保留体检（族⇄生产者对账 + 三把尺对账 + 只读承诺在册）',
               False, [f'retention_report() 取不到读数：{exc}'])
        return
    rec = rep.get('reconciliation') or {}
    if rec.get('verdict') not in ('aligned', 'gap:keep_outlives_eligibility', 'unverified'):
        problems.append(f"对账结论非法：{rec.get('verdict')!r} ⇒ 三态之外一律视为没判")
    kept = rec.get('kept_hours_estimate')
    if rep['population'] and not isinstance(kept, (int, float)):
        problems.append(f'份数尺换算出的小时数不是数值：{kept!r} ⇒ "两把尺见面"仍是句空话')
    evidence.append(f"三把尺 {rep.get('knobs')} ⇒ verdict={rec.get('verdict')}"
                    f"（kept≈{rec.get('kept_hours_estimate')}h vs 资格窗口 {rd.MAX_AGE_H}h）")

    # ④ 接线：CLI 必须真把 retention_report 接上（按 AST 行为判，不读描述）
    if not _s53_cli_wired(rd):
        problems.append('`restore_db.py` 没有把 `--retention` 接到 retention_report() ⇒ '
                        '体检只有函数没有出口（半成品）')

    # ⑤ 只读承诺：体检链上不得出现任何删除能力（下一步最容易顺手加删）
    deletes = _s53_delete_shapes()
    if deletes:
        problems.append(f'保留体检链上出现删除动作 {deletes} ⇒ 本项的职责是"看见"，'
                        '删除须走人工复核的专用通道（M115 未闭环前一律禁止）')

    # ⑥ 人口读数只报不拦：backups/ 是 gitignore 的本机物，CI 上恒 0
    if not rep['population']:
        evidence.append('人口 0 件：本机没有备份 ⇒ 记 unverified，不判红也不判健康')
    else:
        evidence.append(f"人口 {rep['population']} 件 / 够格 {rep['eligible']} 件 / "
                        f"族外无主 {rep['unmanaged_count']} 件"
                        f"（{[u['name'] for u in rep['unmanaged'][:3]] or '无'}）")
    for f in rep['families']:
        evidence.append(f"通道 {f['channel']} 模式={f['patterns']} keep={f['keep']} ⇒ "
                        f"在册 {f['count']} 件 / 最老 "
                        f"{'-' if f['oldest_h'] is None else format(f['oldest_h'], '.1f')}h / "
                        f"够格 {f['eligible']}")
    record('BLOCK', '53. 备份保留体检（族⇄生产者对账 + 三把尺对账 + 只读承诺在册）',
           not problems, problems or evidence)


def _s54_func(tree, name):
    """按名字取模块级函数节点（判据的作用域必须锁在函数体内，不能全文扫）。"""
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def _s54_delete_sites(tree):
    """restore_db.py 里 `os./shutil.` 的 remove / unlink / rmtree 调用点。

    标签取 `外层函数.接收者.方法` —— 接收者必须在标签里，否则第 54 项拿 `dispose.os.remove`
    当期望值去比一个永不产出的形状（本轮首跑实测：标签写成 `dispose.remove` ⇒ 该项恒红）。
    归属按**外层函数**取，不按 `n.func.id`（那是被调方，第 49 项刚为这个踩过恒绿判据）。
    """
    out = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for c in ast.walk(fn):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                    and c.func.attr in ('remove', 'unlink', 'rmtree') \
                    and isinstance(c.func.value, ast.Name) \
                    and c.func.value.id in ('os', 'shutil'):
                out.append(f'{fn.name}.{c.func.value.id}.{c.func.attr}')
    return sorted(set(out))


def _s54_count_shapes(fn):
    """计数式删除的三种形态：while 循环 / len() 参与的大小比较 / 切片下标。"""
    bad = []
    for n in ast.walk(fn):
        if isinstance(n, ast.While):
            bad.append('while 循环')
        elif isinstance(n, ast.Compare) and any(
                isinstance(op, (ast.Gt, ast.Lt, ast.GtE, ast.LtE))
                and any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                        and x.func.id == 'len' for x in [n.left, *n.comparators])
                for op in n.ops):
            bad.append('len() 参与大小比较')
        elif isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Slice):
            bad.append('切片截断')
    return sorted(set(bad))


def _s54_cli_flags(tree):
    fn = _s54_func(tree, 'main')
    if fn is None:
        return set()
    flags = set()
    for c in ast.walk(fn):
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                and c.func.attr == 'add_argument':
            flags |= {a.value for a in c.args if isinstance(a, ast.Constant)
                      and isinstance(a.value, str)}
    return flags


# 第 54 项的删除点名册：`外层函数.接收者.方法` → 为什么它不算旁路。
# 双向判据：名册外的新删除点 ⇒ 红（旁路）；名册里却已不存在的 ⇒ 也红（对空气豁免＝判据空转）。
S54_DELETE_ROSTER = {
    'dispose.os.remove': '唯一真删出口：只删复核凭据绑定的那一个备份名',
    'selftest_roundtrip.os.remove': '只清本函数自己在临时目录里造的桩件，不触 backups/ 面',
    # 第 40 轮加：锁的归因侧件也走 os.remove，但它删的是自己写的 holder（在 backups/ 之外），
    # 且只可能从 with 锁的退出路径调用 —— 由第 55 项判它的调用形态，不在此处放行任意删除。
    'clear_holder.os.remove': '只删自写归因侧件（backups/ 之外），不触被处置面',
    # 第 42 轮加：malformed 侧件的受控清理。它删的也是 backups/ 之外的自写件，
    # 但与 clear_holder 不同——那是"自己用完自己收"，这是**清掉一份没人认领的旧现场**，
    # 所以必须先由 `--malformed-token` 出凭据、由原文字节绑 token、把原文归档进 HELD.jsonl 才许动。
    'clear_malformed_holder.os.remove': '只删**已归档原文**的非法归因侧件（HELD.jsonl 先落，再删那一个）',
}


def check_disposition_channel():
    """第 54 项：复核后处置通道——真删出口唯一、只读面对偶、台账在被删面之外、禁计数式删除。"""
    problems, notes = [], []
    try:
        import restore_db as rd
    except Exception as exc:
        record('BLOCK', '54. 复核后处置通道（真删出口唯一 ∧ 只读面对偶 ∧ 台账独立于被删面'
                        ' ∧ 禁计数式删除）', False,
               [f'import restore_db 失败：{type(exc).__name__}: {exc} ⇒ 判没测到，不判通过'])
        return
    need = ('make_ticket', 'verify_ticket', 'dispose', 'plan_disposal', 'print_disposal_plan',
            'disposition_ledger_path', 'disposition_pending_path', '_find_pending',
            '_outside_root', 'ticket_token', 'cap_jsonl', 'read_jsonl', 'append_jsonl')
    for fn in need:
        if not callable(getattr(rd, fn, None)):
            problems.append(f'restore_db 缺 {fn}() ⇒ 通道不完整（别把"没实现"写成"已接"）')
    if problems:
        record('BLOCK', '54. 复核后处置通道（真删出口唯一 ∧ 只读面对偶 ∧ 台账独立于被删面'
                        ' ∧ 禁计数式删除）', False, problems)
        return
    try:
        tree = ast.parse(read(os.path.join(ROOT, 'restore_db.py')))
    except (OSError, SyntaxError) as exc:
        record('BLOCK', '54. 复核后处置通道（真删出口唯一 ∧ 只读面对偶 ∧ 台账独立于被删面'
                        ' ∧ 禁计数式删除）', False,
               [f'读不到 restore_db.py 源码：{type(exc).__name__} ⇒ 无法判删除面，不判通过'])
        return
    # ① 删除点必须逐个有名册理由：既不许有旁路，也不许留幽灵豁免
    sites = _s54_delete_sites(tree)
    unlisted = [s for s in sites if s not in S54_DELETE_ROSTER]
    ghost = [s for s in S54_DELETE_ROSTER if s not in sites]
    if unlisted:
        problems.append(f'这些删除点不在名册里：{unlisted} ⇒ 复核通道被旁路；要么走 dispose，'
                        '要么在 S54_DELETE_ROSTER 写清它为何不算旁路')
    if ghost:
        problems.append(f'名册里的删除点已不存在：{ghost} ⇒ 对空气豁免＝判据空转')
    if not [s for s in sites if s.startswith('dispose.')]:
        problems.append(f'dispose() 里没有任何删除调用（实扫={sites or "空"}）'
                        ' ⇒ "唯一出口"是句空话，比多出口更糟')
    notes.append(f'删除点 {len(sites)} 处（名册 {len(S54_DELETE_ROSTER)} 条，均带理由）：{sites}')
    plan_fn = _s54_func(tree, 'plan_disposal')
    if plan_fn is not None:
        called = {c.func.id for c in ast.walk(plan_fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        if 'dispose' in called:
            problems.append('plan_disposal() 直接调用 dispose ⇒ dry-run 会偷偷执行，两态合一')
        if _s54_delete_sites(ast.Module(body=[plan_fn], type_ignores=[])):
            problems.append('plan_disposal() 体内有删除调用 ⇒ 违反只读承诺')
    # ② 禁计数式删除：dispose 一次只处置凭据绑定的那一个名字
    dp = _s54_func(tree, 'dispose')
    if dp is None:
        problems.append('找不到 dispose() ⇒ 本项后半段无被测对象')
    else:
        shapes = _s54_count_shapes(dp)
        if shapes:
            problems.append(f'dispose() 里出现计数式删除形态：{shapes} ⇒ 本通道按名字删不按计数')
        if 'confirm' not in [a.arg for a in dp.args.args]:
            problems.append('dispose() 参数表没有 confirm ⇒ 人复核这一步没有落点')
    # ③ 台账必须落在被处置面之外（留痕不得记在被删主体名下——第 28 轮实测过 0 条留痕）
    try:
        led = rd.disposition_ledger_path(root=ROOT)
        pend = rd.disposition_pending_path(root=ROOT)
        bdir = os.path.normcase(os.path.abspath(os.path.join(ROOT, 'backups'))) + os.path.sep
        for label, path in (('台账', led), ('pending', pend)):
            if os.path.normcase(os.path.abspath(path)).startswith(bdir):
                problems.append(f'{label} 落在被处置面 {bdir} 内 ⇒ 删完会把授权记录一起带走')
        if os.path.normcase(os.path.abspath(led)) == os.path.normcase(os.path.abspath(pend)):
            problems.append('台账与 pending 同名 ⇒ 两态混在一处，claimed 与 deleted 分不开')
        notes.append(f'台账={os.path.basename(led)} / pending={os.path.basename(pend)}'
                     f'（均在 backups/ 之外）')
    except rd.DispositionError as exc:
        problems.append(f'路径解析失败：{exc}')
    # ④ CLI 接线：六个旗标在册，且 main() 真调三个出口
    flags = _s54_cli_flags(tree)
    want = {'--plan-disposal', '--ticket', '--dispose', '--confirm', '--claim-by', '--claim-reason'}
    if not want <= flags:
        problems.append(f'CLI 缺旗标 {sorted(want - flags)} ⇒ 能力在源码里但没人能触发')
    main_fn = _s54_func(tree, 'main')
    called_main = ({c.func.id for c in ast.walk(main_fn)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
                   if main_fn else set())
    for fn in ('plan_disposal', 'make_ticket', 'dispose'):
        if fn not in called_main:
            problems.append(f'main() 未调用 {fn}() ⇒ 只写函数不接 CLI 算半成品')
    # ⑤ 时效与理由长度是在册常数，不是散文里的承诺
    if not (isinstance(rd.TICKET_TTL_S, int) and rd.TICKET_TTL_S > 0):
        problems.append(f'TICKET_TTL_S={rd.TICKET_TTL_S!r} ⇒ 凭据不过期等于复核永不过期')
    if not (isinstance(rd.MIN_REASON_CHARS, int) and rd.MIN_REASON_CHARS >= 20):
        problems.append(f'MIN_REASON_CHARS={rd.MIN_REASON_CHARS!r} < 20 ⇒ 一句话就能删别人的备份')
    notes.append(f'TTL={rd.TICKET_TTL_S}s 理由下限={rd.MIN_REASON_CHARS}字 CLI 旗标 {len(flags)} 个')
    # ⑥ 真跑 dry-run 一条（只读：不发凭据、不删件）
    rc, out = _s54_run_plan_json()
    if rc != 0 or '"mode": "plan"' not in out:
        problems.append(f'--plan-disposal --json 真跑不过：rc={rc} 输出 {out[:90]!r}'
                        ' ⇒ 门面读不到数就是没测到')
    else:
        notes.append('CLI 真跑 rc=0，mode=plan')
    record('BLOCK', '54. 复核后处置通道（真删出口唯一 ∧ 只读面对偶 ∧ 台账独立于被删面'
                    ' ∧ 禁计数式删除）', not problems, problems or notes)
def _s55_dispatch_is_none_checked(tree):
    """`if ns.break_lock is not None:` 才算数；`if ns.break_lock:` 是空串漏洞（本轮实测踩到）。"""
    for n in ast.walk(tree):
        t = getattr(n, 'test', None)
        if isinstance(n, ast.If) and isinstance(t, ast.Compare) \
                and isinstance(t.left, ast.Attribute) and t.left.attr == 'break_lock' \
                and any(isinstance(op, ast.IsNot) for op in t.ops):
            return True
    return False


def _s55_calls(fn, names):
    """某函数体内被直接调用的顶层函数名集合（按 `Name.id` 取，不按子串扫源码）。"""
    return {c.func.id for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in names}


def _s55_with_lock(tree, fname):
    """该函数是否**以 with 形态**持有处置锁（对偶 `pre-commit gc.py:62` 的 with 独占锁）。

    只认 `With` 节点里调了 `disposition_lock(...)` 的那种；同时禁止函数体内出现裸
    `acquire()` / `release()`——那意味着异常路径可以把锁漏在外面。
    """
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fname), None)
    if fn is None:
        return 'missing'
    has_with = any(isinstance(w, ast.With)
                   and any(isinstance(it.context_expr, ast.Call)
                           and isinstance(it.context_expr.func, ast.Name)
                           and it.context_expr.func.id == 'disposition_lock' for it in w.items)
                   for w in ast.walk(fn))
    raw = [f'{c.func.attr}' for c in ast.walk(fn)
           if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
           and c.func.attr in ('acquire', 'release')]
    if not has_with:
        return 'no-with'
    return f'raw:{raw}' if raw else 'ok'


def check_disposition_lock():
    """第 55 项：处置锁——写态必须 with 独占、只读态必须不取锁、破锁必须回交 holder token。"""
    problems, notes = [], []
    try:
        import restore_db as rd
    except Exception as exc:
        record('BLOCK', '55. 处置锁（写态 with 独占 ∧ 只读态不取锁 ∧ 破锁回交 holder token '
                        '∧ filelock 已声明且延迟导入 ∧ 只读档排在取锁前 ∧ 侧件字段级校验）', False,
               [f'import restore_db 失败：{type(exc).__name__}: {exc} ⇒ 判没测到，不判通过'])
        return
    try:
        tree = ast.parse(read(os.path.join(ROOT, 'restore_db.py')))
    except (OSError, SyntaxError) as exc:
        record('BLOCK', '55. 处置锁（写态 with 独占 ∧ 只读态不取锁 ∧ 破锁回交 holder token '
                        '∧ filelock 已声明且延迟导入 ∧ 只读档排在取锁前 ∧ 侧件字段级校验）', False,
               [f'读不到 restore_db.py 源码：{type(exc).__name__} ⇒ 无法判锁形态，不判通过'])
        return
    # ① 写态两个入口必须 with 取锁（缺一个就是半条链裸奔）
    for fn in ('dispose', 'make_ticket'):
        got = _s55_with_lock(tree, fn)
        if got != 'ok':
            problems.append(f'{fn}() 的锁形态 = {got}（期望 with disposition_lock(...) 且无裸 '
                            'acquire/release）⇒ 异常路径可能把锁漏在外面')
    # ② 只读态一律不取锁（对偶 restic `cmd_forget.go:189-191`：免锁只配 dry-run）
    for fn in ('plan_disposal', 'stale_lock_report', 'print_disposal_plan', 'retention_report',
               'backup_records'):
        node = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn), None)
        if node is None:
            problems.append(f'找不到 {fn}() ⇒ 只读面少了一个被测对象')
        elif 'disposition_lock' in _s55_calls(node, {'disposition_lock'}):
            problems.append(f'只读函数 {fn}() 取了处置锁 ⇒ 只读态与写态混同（dry-run 不该排队等锁）')
    # ③ 处置通道的文件必须**按名册取**、互不相同且都在被删面之外（第 42 轮：不再写死"四类"）
    try:
        bdir = os.path.normcase(os.path.abspath(os.path.join(ROOT, 'backups'))) + os.path.sep
        paths = rd.disposition_paths(ROOT)
        if set(paths) != set(rd.DISPOSITION_PATH_KINDS):
            problems.append(f'路径名册与实现不符：名册={sorted(rd.DISPOSITION_PATH_KINDS)} '
                            f'实际={sorted(paths)}')
        for k, v in paths.items():
            if os.path.normcase(os.path.abspath(v)).startswith(bdir):
                problems.append(f'{k} 落在被处置面内 ⇒ {v}')
        if len({os.path.normcase(os.path.abspath(v)) for v in paths.values()}) != len(paths):
            problems.append(f'{len(paths)} 类路径出现重名 ⇒ 角色混在一起：{sorted(paths.values())}')
        notes.append(f'锁/归因/台账/pending/归档 {len(paths)} 路分离且均在 backups/ 之外')
    except rd.DispositionError as exc:
        problems.append(f'路径解析失败：{exc}')
    # ④ 破锁三态可读、且"无事可做"不得报成功
    try:
        rep = rd.stale_lock_report(root=ROOT)
        if rep['verdict'] not in ('clean', 'live', 'stale', 'residual-recent', 'unreadable',
                                  'malformed'):
            problems.append(f"stale_lock_report 给出表外状态 {rep['verdict']!r} ⇒ 状态枚举漂移")
        else:
            notes.append(f"现状 verdict={rep['verdict']} 龄={rep['holder_age_s']}"
                         f" token={rep['token'] or '-'}")
    except Exception as exc:
        problems.append(f'stale_lock_report 真跑失败：{type(exc).__name__}: {exc}')
    for bad in ('', '0' * 12):
        try:
            rd.break_stale_lock(root=ROOT, confirm=bad)
            problems.append(f'confirm={bad!r} 竟被接受 ⇒ 破锁无需回交身份')
        except rd.DispositionError as exc:
            if '拒绝' not in str(exc) and '不符' not in str(exc) and '无事可做' not in str(exc):
                problems.append(f'破锁被拒但理由不可核：{str(exc)[:60]}')
    # ⑤ 新依赖必须"已声明 + 延迟导入"两条都成立（顶层导入会把依赖拖进 PA 的启动路径）
    req = os.path.join(ROOT, 'requirements.txt')
    body = read(req) if os.path.isfile(req) else ''
    if 'filelock' not in body:
        problems.append('requirements.txt 未声明 filelock ⇒ 用了没登记（第 46 项同族，'
                        '第 32 轮 PyYAML 那次就是这里塌的）')
    top = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    top |= {n.module for n in tree.body if isinstance(n, ast.ImportFrom) and n.level == 0}
    if 'filelock' in top:
        problems.append('restore_db.py 在**模块顶层** import filelock ⇒ init_db.py 函数体内 '
                        'import restore_db 时会连带加载它，把新依赖拖进线上启动路径')
    notes.append(f'filelock 已声明={"filelock" in body} 顶层导入={"filelock" in top}'
                 f'（应为 True / False）')
    # ⑥ CLI 在册 + main 真调两个出口 + 空 token 不得降级成"没给参数"
    flags = _s54_cli_flags(tree)
    want = {'--lock-status', '--break-lock', '--dry-run'}
    if not want <= flags:
        problems.append(f'CLI 缺旗标 {sorted(want - flags)} ⇒ 能力在源码里但没人能触发')
    main_fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main'), None)
    called = _s55_calls(main_fn, {'stale_lock_report', 'break_stale_lock'}) if main_fn else set()
    for fn in ('stale_lock_report', 'break_stale_lock'):
        if fn not in called:
            problems.append(f'main() 未调用 {fn}() ⇒ 只写函数不接 CLI 算半成品')
    if not _s55_dispatch_is_none_checked(tree):
        problems.append('破锁分支按真值分派 ⇒ 空 token 会掉进"没给参数"的兜底提示，'
                        '绕开"未复核即拒"（本轮 `--break-lock ""` 实测过这个降级）')
    # ⑦ 真跑一条锁状态（门面必须带读数，不接受只回"通过"）
    rc, out = _s55_run_lock_status()
    if rc != 0 or 'verdict=' not in out:
        problems.append(f'--lock-status 真跑不过：rc={rc} 输出 {out[:80]!r}')
    else:
        notes.append('CLI 真跑 rc=0 且门面行带 verdict 读数')
    # ⑧ 只读档（M128）：`dry_run` 必须排在取锁之前，且 `_dispose_probe` 体内一个写动作都不许有
    disp_fn = next((n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == 'dispose'), None)
    probe_fn = next((n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == '_dispose_probe'), None)
    if disp_fn is None or probe_fn is None:
        problems.append('缺 dispose() 或 _dispose_probe() ⇒ 只读档不在册（写了能力却没出口＝半成品）')
    else:
        args55 = [a.arg for a in disp_fn.args.args] + [a.arg for a in disp_fn.args.kwonlyargs]
        if 'dry_run' not in args55:
            problems.append('dispose() 没有 dry_run 形参 ⇒ CLI 的 --dry-run 无处可去')
        dry_ln = min((n.lineno for n in ast.walk(disp_fn) if isinstance(n, ast.If)
                      and any(isinstance(x, ast.Name) and x.id == 'dry_run'
                             for x in ast.walk(n.test))), default=None)
        with_ln = min((it.context_expr.lineno for n in ast.walk(disp_fn) if isinstance(n, ast.With)
                       for it in n.items if isinstance(it.context_expr, ast.Call)
                       and getattr(it.context_expr.func, 'id', '') == 'disposition_lock'),
                      default=None)
        if dry_ln is None or with_ln is None:
            problems.append(f'dispose() 里找不到 dry_run 分支或取锁点（dry={dry_ln} with={with_ln}）')
        elif dry_ln > with_ln:
            problems.append(f'dry_run 分支在第 {dry_ln} 行、排在取锁（第 {with_ln} 行）之后 '
                            '⇒ 撞锁时只读档一样进不了门，M128 等于没补')
        writes55 = sorted({getattr(c.func, 'attr', '') or getattr(c.func, 'id', '')
                           for c in ast.walk(probe_fn) if isinstance(c, ast.Call)}
                          & {'remove', 'append_jsonl', 'write_holder', 'cap_jsonl', '_stamp'})
        if writes55:
            problems.append(f'_dispose_probe() 体内出现写动作 {writes55} ⇒ 名为只读、实会动盘')
        if '--dry-run' not in flags:
            problems.append('CLI 缺 --dry-run ⇒ 只读档只能被代码调用，人用不上')
        else:
            notes.append(f'只读档 dry_run@{dry_ln} 排在取锁@{with_ln} 之前，probe 体内零写动作')
    # ⑨ 侧件字段级校验（借 kopia `upgrade_lock_intent.go:64-79` 的 Validate() 交叉不等式形状）
    try:
        good55 = rd._holder_doc(token='a' * rd.TOKEN_HEX_LEN, kind='dispose')
        gp = rd.validate_holder_doc(good55)
        if gp:
            problems.append(f'合法侧件被拒 ⇒ 校验把正常路径也杀了：{gp}')
        for label, patch in (('pid 为 0', {'pid': 0}), ('pid 为字符串', {'pid': 'abc'}),
                             ('token 过短', {'token': 'abc'}),
                             ('kind 不在册', {'kind': 'who-knows'}),
                             ('ts 无时区', {'ts': '2026-01-01T00:00:00'}),
                             ('ts 解析失败', {'ts': 'not-a-time'})):
            doc55 = dict(good55)
            doc55.update(patch)
            if not rd.validate_holder_doc(doc55):
                problems.append(f'{label} 竟被接受 ⇒ validate_holder_doc 恒过，第六态永不可达')
        if rd.validate_lock_config():
            problems.append(f'当前锁预算不成对：{rd.validate_lock_config()}')
        if not rd.validate_lock_config(wait_s=rd.LOCK_STALE_S + 1, stale_s=rd.LOCK_STALE_S):
            problems.append('等待窗口长于陈旧阈值竟被放行 ⇒ 交叉不等式没落地（会等一把自己判陈旧的锁）')
        kinds55 = _s55_holder_kinds(tree)
        if kinds55 != set(rd.HOLDER_KINDS):
            problems.append(f'HOLDER_KINDS 名册与调用点不符：名册={sorted(rd.HOLDER_KINDS)} '
                            f'实发={sorted(kinds55)} ⇒ 死豁免或漏登记')
        notes.append(f'侧件校验双向（合法放行 / 6 类非法各判红），名册⇄调用点等值 {sorted(kinds55)}')
    except AttributeError as exc:
        problems.append(f'侧件校验件缺失：{exc} ⇒ 判没测到')
    # ⑩ 受控清理通道（第 42 轮 M133）：写态 with、凭据绑字节、归档先于删除、非 malformed 即拒
    cm = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == 'clear_malformed_holder'), None)
    mt = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == 'malformed_token'), None)
    if cm is None or mt is None:
        problems.append('缺 clear_malformed_holder() 或 malformed_token() ⇒ malformed 仍无合法收尾出口')
    else:
        if _s55_with_lock(tree, 'clear_malformed_holder') != 'ok':
            problems.append(f'clear_malformed_holder() 的锁形态 = '
                            f'{_s55_with_lock(tree, "clear_malformed_holder")}（期望 with '
                            'disposition_lock 且无裸 acquire/release）⇒ 清理可与正在进行的处置并发')
        fields42 = {getattr(x, 'value', None) for x in ast.walk(cm) if isinstance(x, ast.Constant)}
        if "'malformed'" not in [repr(f) for f in fields42] and 'malformed' not in fields42:
            problems.append('clear_malformed_holder() 里没有"非 malformed 即拒"的判据 ⇒ 通道谁都能走')
        # 归档必须**归到 HELD 台账**：只查"有没有 append_jsonl/cap_jsonl"会被 cap_jsonl 单独满足，
        # 把真正留证那一行删掉判据照样绿（本轮变异腿实测恒绿 ⇒ 断言了动作的名字，没断它的对象）。
        arch_nodes = [n for n in ast.walk(cm) if isinstance(n, ast.Call)
                      and getattr(n.func, 'id', '') == 'append_jsonl' and n.args
                      and isinstance(n.args[0], ast.Call)
                      and getattr(n.args[0].func, 'id', '') == 'disposition_held_path']
        arch = min((n.lineno for n in arch_nodes), default=None)
        dele = min((n.lineno for n in ast.walk(cm) if isinstance(n, ast.Attribute)
                    and n.attr == 'remove'), default=None)
        if arch is None:
            problems.append('clear_malformed_holder() 里没有"写进 HELD 台账"的动作 ⇒ 删了没留证，'
                            '比没删更难发现')
        elif dele is None:
            problems.append(f'清理通道没有删除动作（归档在第 {arch} 行）⇒ 通道存在但什么都不做')
        elif arch > dele:
            problems.append(f'归档在第 {arch} 行、删除在第 {dele} 行之后 ⇒ 删了没留证，比没删更难发现')
        mt_fields = {getattr(x, 'value', None) for x in ast.walk(mt) if isinstance(x, ast.Constant)}
        tainted = sorted(f for f in ('pid', 'kind', 'ts') if f in mt_fields)
        if tainted:
            problems.append(f'malformed_token() 引用了侧件字段 {tainted} ⇒ 凭据绑在被污染的输入上')
    if not {'--malformed-token', '--clear-malformed'} <= flags:
        problems.append(f'CLI 缺受控出口旗标：{sorted({"--malformed-token", "--clear-malformed"} - flags)}')
    called42 = _s55_calls(main_fn, {'clear_malformed_holder'}) if main_fn else set()
    if 'clear_malformed_holder' not in called42:
        problems.append('main() 未调用 clear_malformed_holder() ⇒ 只写函数不接 CLI 算半成品')
    # 行为核：临时面真跑（不碰本仓任何已跟踪件）——改一个字节 token 必须变，旧 token 必须清不动
    try:
        tmp42 = tempfile.mkdtemp(prefix='gate55-malformed-')
        try:
            os.makedirs(os.path.join(tmp42, 'backups'), exist_ok=True)
            hp42 = rd.disposition_holder_path(tmp42)
            open(hp42, 'w', encoding='utf-8', newline='\n').write(
                json.dumps({'pid': 'x', 'token': 'zz', 'kind': 'ghost', 'ts': 'nope'},
                           ensure_ascii=False) + '\n')
            raw42 = open(hp42, 'rb').read()
            tok42 = rd.malformed_token(raw42, tmp42)
            tok42b = rd.malformed_token(raw42 + b' ', tmp42)
            refused = ''
            try:
                rd.clear_malformed_holder(root=tmp42, confirm='0' * 16,
                                          reason='门禁行为核：旧 token 不该清得动这份侧件')
            except rd.DispositionError as exc:
                refused = str(exc)
            still = os.path.isfile(hp42)
            done42 = rd.clear_malformed_holder(
                root=tmp42, confirm=tok42, reason='门禁行为核：以原文派生 token 完成一次受控清理')
            held_rows = len(rd.read_jsonl(rd.disposition_held_path(tmp42))[0])
        finally:
            shutil.rmtree(tmp42, ignore_errors=True)
        if tok42 == tok42b:
            problems.append('侧件多一个空格 token 不变 ⇒ 凭据没真绑住原文')
        if not refused or 'confirm' not in refused or not still:
            problems.append('旧/错 token 没能拦住清理，或拦下后侧件已被动 ⇒ 出口不是 fail-closed')
        if not done42.get('cleared') or held_rows != 2:
            problems.append(f'正确 token 的受控清理未留两条账（approved+cleared，实得 {held_rows} 行）')
        if done42.get('population') != 0:
            problems.append('清理动作触碰了被处置面人口 ⇒ 本通道只该动侧件本身')
        else:
            notes.append(f'受控出口行为核通过：token {tok42[:6]}… 绑原文（+1 字节即变），'
                         f'错 token 拒且侧件仍在，归档 {held_rows} 行')
    except (OSError, rd.DispositionError) as exc:
        problems.append(f'受控出口行为核跑不起来：{type(exc).__name__}: {exc} ⇒ 判没测到')
    record('BLOCK', '55. 处置锁（写态 with 独占 ∧ 只读态不取锁 ∧ 破锁回交 holder token '
                    '∧ filelock 已声明且延迟导入 ∧ 只读档排在取锁前 ∧ 侧件字段级校验）',
           not problems, problems or notes)


def _s55_holder_kinds(tree):
    """从调用点取 `write_holder(kind=...)` 实发的字面量（不读名册自述，按行为对账）。"""
    out = set()
    for c in ast.walk(tree):
        if isinstance(c, ast.Call) and getattr(c.func, 'id', '') == 'write_holder':
            for kw in c.keywords:
                if kw.arg == 'kind' and isinstance(kw.value, ast.Constant):
                    out.add(str(kw.value.value))
    return out


def _s55_run_lock_status():
    import subprocess
    try:
        p = subprocess.run([sys.executable, os.path.join(ROOT, 'restore_db.py'), '--lock-status'],
                           capture_output=True, text=True, timeout=180, cwd=ROOT)
        return p.returncode, (p.stdout or '')[:2000]
    except (OSError, subprocess.SubprocessError) as exc:
        return 99, f'{type(exc).__name__}: {exc}'


def _s54_run_plan_json():
    import subprocess
    try:
        p = subprocess.run([sys.executable, os.path.join(ROOT, 'restore_db.py'),
                            '--plan-disposal', '--json'],
                           capture_output=True, text=True, timeout=180, cwd=ROOT)
        return p.returncode, (p.stdout or '')[:4000]
    except (OSError, subprocess.SubprocessError) as exc:
        return 99, f'{type(exc).__name__}: {exc}'


def _s53_producers():
    """生产者 = 在模块级**声明了一份"留几份"的尺**的模块：名字按 '_' 切词后含 `KEEP` 的常数。

    为什么不按"往 backups/ 写文件"判（本轮实测过两版都错）：
      ① 含 'backups' 字面量 ∧ 有写/复制动作 ⇒ 数到 6 个，`init_db.py`（只是解析 --backup 路径）、
         `quality_gate.py`（第 51 项读目录）、`test_l4_assertions.py`（夹具）全是假阳；
      ② 再放宽到"路径拼出 backups"⇒ 仍 5 个假阳面；按函数名子串 `_prune` 判 ⇒ 连
         `src/backup_manifest.record_pruned` 都被算成通道（同族老坑：子串匹配会被同名族骗过）。
      真正可判、且与"淘汰通道"一一对等的不变量是：**做了保留决定的模块必须留得下名字**。
      新增一处 KEEP 常数而没进 `retention_channels()` ⇒ 本项当场红（一次正常提交即可变绿）。
    """
    out = set()
    for rel, tree in _s43_trees()[0].items():
        for node in tree.body:
            if isinstance(node, ast.Assign):
                tgts = node.targets
            elif isinstance(node, ast.AnnAssign):
                tgts = [node.target]
            else:
                continue
            for t in tgts:
                if isinstance(t, ast.Name) and 'KEEP' in t.id.split('_'):
                    out.add(rel.replace('\\', '/'))
    return out


def _s53_cli_wired(rd):
    """`main()` 里有一条分支：读 `ns.retention` 且调用 `retention_report`（两者同一函数体内才算接上）。"""
    try:
        tree = ast.parse(rd._read_src(os.path.join(ROOT, 'restore_db.py')))
    except (OSError, SyntaxError):
        return False
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == 'main'):
            continue
        blob = ast.unparse(fn)
        if 'ns.retention' in blob and 'retention_report' in blob:
            return True
    return False


def _s53_delete_shapes():
    """体检链上的删除形态（os.remove / unlink / rmtree / unlink_manifest）——应当为空。"""
    bad = []
    for rel in ('restore_db.py',):
        try:
            tree = ast.parse(read(os.path.join(ROOT, rel)))
        except (OSError, SyntaxError) as exc:
            bad.append(f'{rel}:{type(exc).__name__}(读不到=无法判只读)')
            continue
        funcs = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name.startswith(('retention', 'print_retention'))}
        for fn in ast.walk(tree):
            if not (isinstance(fn, ast.FunctionDef) and fn.name in funcs):
                continue
            for call in ast.walk(fn):
                if isinstance(call, ast.Attribute) and call.attr in ('remove', 'unlink', 'rmtree'):
                    bad.append(f'{rel}:{fn.name}:{call.attr}')
    return sorted(set(bad))


def check_local_gate_ledger():
    """第 49 项：本地闸的运行台账 —— 让"闸跑没跑过、跑了多少次、红在哪个阶段"可回答。

    动因（2026-09-27 第 34 轮当场实测）：第 48 项为了防自指成环，刻意**不裁决**回执的
    `verdict`（无回执⇒红⇒跑一次⇒回执=fail⇒仍红），代价是"本地闸长期红"这件事
    只活在一台机器的屏幕上：回执只有一面镜子（最后一次），既看不见历史，也看不见有没有被手改。
    上游对照（本轮 `gh api` 实取）：InvenTree 把前端/迁移各自做成独立作业
    （`.github/workflows/frontend.yaml`、`migration_tests.yaml:53/:86`）⇒ 运行史在平台侧可查；
    我们没有可用的平台（M42 第 19 次：job `108597535679` 注解原文
    "The job was not started because recent account payments have failed or your spending limit
    needs to be increased."），所以运行史必须自己落盘。
    """
    problems, evidence = [], []
    root = _s48_repo_root()
    if root is None:
        record('BLOCK', '49. 本地闸运行台账（同源自证 + 唯一写盘入口 + 覆盖读数）',
               False, ['上溯找不到 .git ⇒ 无法定位仓根账本（判"没测到"）'])
        return
    lp, rp = os.path.join(root, _S49_RUNS), os.path.join(root, _S49_RECEIPT)
    if not os.path.isfile(lp):
        problems.append(f'缺 {_S49_RUNS}：本地闸没有运行史 ⇒ "跑过"这句话不可核')
        rows = []
    else:
        rows, rp_ = _s49_rows(read(lp))
        problems += rp_
        if len(rows) > _S49_MAX_ROWS:
            problems.append(f'账本 {len(rows)} 行 > 封顶 {_S49_MAX_ROWS} ⇒ 裁剪通道失效（无限膨胀的账本没人看）')
        evidence.append(f'运行账本 {len(rows)} 行（封顶 {_S49_MAX_ROWS}）')
    receipt = None
    if os.path.isfile(rp):
        try:
            receipt = json.loads(read(rp))
        except ValueError as exc:
            problems.append(f'{_S49_RECEIPT} 不可解析：{type(exc).__name__}')
    else:
        problems.append(f'缺 {_S49_RECEIPT}：第 48 项的替代强制点没有落点')
    con_p, con_e = _s49_consistency(rows, receipt)
    problems += con_p
    if con_e:
        evidence.append('账本末行 ⇄ 回执同源自证：' + con_e)
    cur = subprocess.run(['git', '-C', root, 'rev-parse', 'HEAD'],
                         capture_output=True, text=True).stdout.strip()
    if rows:
        line, _ = _s49_coverage(rows, cur, root)
        evidence.append(line)
    entry_p = _s49_entry_problems(root)
    problems += entry_p
    record('BLOCK', '49. 本地闸运行台账（同源自证 + 唯一写盘入口 + 覆盖读数）',
           not problems, problems or evidence)


def _s49_entry_script(root):
    """入口脚本路径**从契约里取**：命令是数据，写进门禁源码就成环（S16 同一把尺）。

    返回 (abs_path 或 None, problems)。
    """
    cp = os.path.join(root, _S48_CONTRACT)
    if not os.path.isfile(cp):
        return None, [f'缺 {cp} ⇒ 无法从 enforcement.receipt_cmd 取到本地闸入口（判"没测到"）']
    try:
        contract = json.loads(read(cp))
    except (ValueError, OSError) as exc:
        return None, [f'契约不可解析：{type(exc).__name__} ⇒ 入口取不到（判"没测到"）']
    cmd = str(((contract.get('enforcement') or {}).get('receipt_cmd')) or '')
    for tok in cmd.split():
        if tok.endswith('.py') and os.path.isfile(os.path.join(root, tok.replace('\\', os.sep))):
            return os.path.join(root, tok.replace('\\', os.sep)), []
    return None, [f'enforcement.receipt_cmd={cmd!r} 里没有一个 .py 落在盘上 ⇒ 入口脚本取不到（判"没测到"）']


def _s49_entry_problems(root):
    """唯一写盘入口：账本只能由本地验证脚本里的**一个**发射函数追加，且它同时写回执。

    旁路（别的脚本也写、或回执与账本分两处写）= 时间序列可以被伪造，那这本账就只是装饰。
    注意「调用方」必须按**外层函数名**归属：写成 `n.func.id` 取到的是被调方自己，
    任何样本都只会得到一个元素 ⇒ 判据永不翻红（本轮由 L4 S40 两条有牙腿实测抓到）。
    """
    vp, probs = _s49_entry_script(root)
    if probs:
        return probs
    tree = ast.parse(read(vp))
    top_funcs = {f.name for f in tree.body if isinstance(f, ast.FunctionDef)}
    if 'append_run_ledger' not in top_funcs:
        return ['入口脚本里没有 append_run_ledger ⇒ 账本没有生产者，判"没做过"']
    callers, loose = set(), 0
    for node in tree.body:
        hits = [n for n in ast.walk(node)
                if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'append_run_ledger']
        if isinstance(node, ast.FunctionDef):
            if node.name != 'append_run_ledger' and hits:
                callers.add(node.name)
        elif hits:
            loose += len(hits)
    if loose:
        return [f'append_run_ledger 有 {loose} 处在任何函数之外被调用 ⇒ import 该脚本即可伪造账本']
    if len(callers) != 1 or 'main' in callers:
        return [f'append_run_ledger 的调用方必须只有一个且不是 main（现在 {sorted(callers)}）'
                '⇒ 分两处写就能写出互相矛盾的两件产物']
    emitter = next(iter(callers))
    fn = next(f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == emitter)
    inner = {getattr(n.func, 'id', '') for n in ast.walk(fn) if isinstance(n, ast.Call)}
    if 'write_receipt' not in inner:
        return [f'唯一写盘入口 {emitter}() 内没有 write_receipt ⇒ 回执与账本分两处写就会各写各的']
    return []


def check_dependency_supply_chain():
    """第 46 项：依赖供应链 —— 谁在更新依赖、更新到哪一版、那一版有没有已知漏洞，三问都要有答案。

    动因（2026-09-27 当场实测，`D:/tmp/bench33/`）：
    ① 全仓与既有 45 项门禁对 `pip-audit|osv|dependabot|GHSA|CVE-` **0 命中**；
    ② `git log --all --name-only | grep dependabot` 三条路径**全在子目录/归档件**
       （`iCAN大学生创新创业大赛/.github/`、`05-源代码全文/.github/`、
        `archive/ci-pre-root-move-20260924/`）⇒ GitHub 只读仓库根的 `.github/`，
       2026-09-18 那份从未生效；09-24 的处置（`9172ab7`/`c80f3e6`）是**删掉宣称**而不是补能力，
       而当时没有任何判据能区分"配置已退役"与"配置从未生效"；
    ③ 实跑 pip-audit：声明面（requirements.txt，27 包）**0 条**，环境面（本 venv，86→87 包）
       **10 条 / 2 包**（pip 26.1.2 → PYSEC-2026-3721/CVE-2026-13346；
       pypdf 6.14.2 → PYSEC-2026-3655/3656/3910/3912 等 8 条，且 pypdf 未声明、无人 import、
       无包依赖它 = 孤儿包）⇒ 只扫一面必漏一面。
    上游（本轮 `gh api` 实取）：superset `.github/dependabot.yml:1-13`（daily +
    `groups.security.applies-to: security-updates` + `cooldown.default-days: 7`）、
    InvenTree `:1-19`（weekly + cooldown + groups）；airflow/posthog 同在仓库根 ⇒ 4/4 有。
    """
    problems, evidence = [], []
    for sub in (_s46_dependabot_problems, lambda: _s46_pin_problems(ROOT),
                lambda: _s46_receipt_problems(ROOT), lambda: _s46_gate_import_problems(ROOT)):
        p, e = sub()
        problems += p
        evidence += e
    record('BLOCK', '46. 依赖供应链（dependabot 生效面 + 钉版纪律 + 两面漏洞审计回执）',
           not problems, problems or evidence)


def check_outbound_surface():
    """第 44 项：出站投递的 SSRF 守卫、留痕物件化与跨轮名册联动。

    动因（2026-09-27 当场实测）：`api_open.py:152-155` 对回调 URL 只校 `http(s)://` 前缀与长度，
    `webhooks.py:202` 把库里的 URL 原样交给 `requests.post`（`allow_redirects` 默认 True）
    ⇒ 已认证商家可指挥本服务端去请求 `http://127.0.0.1:5000/…`、`http://169.254.169.254/…`、
    `http://2130706433/`（十进制回环）；而全仓与既有 43 项门禁里 `localhost`/`is_private`/
    `ip_address` 类判断 **0 条**。投递本身"一次都不重试、失败只进会轮转的 JSONL"
    ⇒ 「这个事件到底发出去没有」在轮转后不可回答。
    上游（同轮 `gh api` 取回原文）：Saleor `webhook/models.py:12-15` 把校验器挂在 URLField 上
    （任何写路径都跑不掉）；Frappe `webhook.py:198/:216-225` 把 `next_retry` 落成盘上物件并由
    到期作业重投（不是内存计数器）；Discourse `final_destination.rb:267-276` 解析后**直连该 IP**，
    掐掉「校验时一个 IP、连接时另一个 IP」的 rebinding 时差。
    """
    problems, evidence = [], []
    trees, broken = _s43_trees()
    if broken:
        problems.append(f'这些 .py 解析失败（出站面枚举不完整，不许静默少算）：{broken[:5]}')
    if not trees:
        record('BLOCK', '44. 出站投递面（SSRF 守卫 + 投递留痕 + 跨轮名册联动）',
               False, ['一个 .py 都没解析成功 —— 判"没测到"，不判通过'])
        return
    _s44_outside = (_S44_OPS_SCRIPT,)
    sites, guarded = _s44_outbound_calls(trees, outside=_s44_outside)
    if not sites:
        problems.append('一处出站 HTTP 调用都没枚举到（本仓有 Webhook 投递与 AI 外呼 ⇒ '
                        '判枚举器失效，不得读成"没有要管的出站点"）')
    unguarded = [f'{rel}:{ln}({fn})={txt}' for rel, ln, fn, txt in sites
                 if (rel, fn) not in guarded and f'{rel}::{fn}' not in _S44_OUTBOUND_ALLOWED]
    if unguarded:
        problems.append(f'这些出站调用点没先过 outbound_guard.check_target（SSRF 面）：{unguarded[:6]}')
    stale = [k for k in _S44_OUTBOUND_ALLOWED
             if not any(f'{rel}::{fn}' == k for rel, _ln, fn, _txt in sites)]
    if stale:
        problems.append(f'出站点豁免已不存在（陈旧豁免=名册失真）：{stale[:5]}')
    for key, why in sorted(_S44_OUTBOUND_ALLOWED.items()):
        if len(str(why).strip()) < 20:
            problems.append(f'豁免 {key} 的理由不足 20 字（豁免是决策，必须留下为什么）')
    shape_problems, shape_ev = _s44_shape_problems(trees)
    problems += shape_problems
    evidence += shape_ev
    reg_problems, reg_ev = _s44_registry_problems(trees)
    problems += reg_problems
    evidence += reg_ev
    retry_scoped = ''
    hooks = trees.get(_S44_HOOKS_FILE)
    if hooks is not None:
        for node in ast.walk(hooks):
            if not isinstance(node, ast.FunctionDef) or node.name not in _S44_RETRY_FUNCS:
                continue
            if any(isinstance(c, ast.Call) and 'unscoped' in ast.unparse(c.func)
                   for c in ast.walk(node)):
                retry_scoped = node.name
    if hooks is not None and not retry_scoped:
        problems.append(f'{_S44_HOOKS_FILE} 里 {sorted(_S44_RETRY_FUNCS)} 没有一个走 '
                        'query_guard.unscoped() ⇒ 跨商家扫描会被行级守卫滤成空集，'
                        '而"没有要重试的"与"滤空了"同形')
    if retry_scoped:
        evidence.append(f'重试扫描的逃逸面：{retry_scoped}()（点名到函数，不承认"整文件里有 unscoped"）')
    _S44_NOTE.clear()
    problems += _s44_backdoor_problems(trees)
    evidence.append(f'取数面 {len(trees)} 个 .py；出站调用点 {len(sites)} 处'
                    f'（未过守卫 {len(unguarded)} 处、豁免 {len(_S44_OUTBOUND_ALLOWED)} 条带理由）')
    evidence.append(f'出站面外文件（运维手工脚本，不在 Web 进程内跑）：{list(_s44_outside)}；'
                    f'豁免名册只记决策，不抄调用点')
    evidence += list(_S44_NOTE)
    record('BLOCK', '44. 出站投递面（SSRF 守卫 + 投递留痕 + 跨轮名册联动）',
           not problems, problems or evidence)


def check_erasure_surface():
    """第 42 项：商家数据擦除的归属图分母、出口接线与不可逆动作护栏。

    动因（2026-09-27 当场实测）：`注销|delete_account|erase` 在 `src/` **0 命中**，
    而 `/register` 是匿名公开档 ⇒ 开放注册进来的商家永久删不掉。更关键的是分母：
    ORM 元数据枚举 18 张表，**只有 8 张挂 `merchant_id`** —— `daily_sales`（2,100 行，
    全库最大业务表）只有 `store_id`/`staff_id`/`pack_id`，**按 merchant_id 直觉擦会整表漏掉
    营业流水**，且没有任何既有判据会红。上游（同轮 `gh api` 实取）：Saleor
    `account_delete.py:46/:54/:105` 把删除做成声明式事件；PostHog `delete_persons.py:23/:31`
    是**有界分块、失败只重试失败者**；Frappe `user.py:381-386` 守「不可擦最小集合」。
    """
    problems, evidence = [], []
    tables, broken = _s42_model_tables()
    if broken:
        problems.append('src/models.py 解析失败（实表分母取不到，不得按通过处理）')
    owning, order, phrase = _s42_registry_ast()
    if owning is None:
        record('BLOCK', '42. 商家数据擦除归属图与出口护栏（分母 + 接线 + 备份凭证）',
               False, ['src/erasure.py 缺失或解析失败（擦除能力的唯一声明源不存在）'])
        return
    if not owning:
        problems.append('归属图为空（零登记一律判红，不得读成"没有要擦的表"）')
    if not order:
        problems.append('ERASE_ORDER 缺失或为空（无删除顺序 ⇒ 可能出现无主孤儿行）')
    if not phrase or len(str(phrase)) < 12:
        problems.append('确认短语缺失或过短（不可逆动作必须逐字键入）')

    miss = sorted(set(tables) - set(owning))
    ghost = sorted(set(owning) - set(tables))
    if miss:
        problems.append(f'这些实表不在归属图（擦除会静默漏表）：{miss[:8]}')
    if ghost:
        problems.append(f'归属图登记了不存在的表（陈旧声明）：{ghost[:8]}')

    erasable = set(order or [])
    not_in_order = sorted((set(owning) - erasable)
                          - {n for n, s in owning.items()
                             if isinstance(s, dict) and s.get('mode') == 'declared_none'})
    if not_in_order:
        problems.append(f'可擦档的表不在删除顺序里：{not_in_order[:8]}')
    roots = [n for n, s in owning.items()
             if isinstance(s, dict) and s.get('mode') == 'root']
    if order and roots and order[-1] not in roots:
        problems.append(f'删除顺序最后一步必须是根行（现在最后是 {order[-1]}）')

    need_col = {'merchant_id': 'merchant_id', 'store_id': 'store_id', 'email_list': 'email'}
    for name, spec in sorted(owning.items()):
        if not isinstance(spec, dict):
            problems.append(f'{name} 的归属档解析不出（写法超出门禁可静态识别的形状）')
            continue
        mode = spec.get('mode')
        cols = tables.get(name, set())
        want = need_col.get(mode)
        if want and cols and want not in cols:
            problems.append(f'{name} 声明为 {mode} 但 models.py 里没有 {want} 列（谓词会抛或恒假）')
        if mode == 'store_or_staff' and cols and not {'store_id', 'staff_id'} & cols:
            problems.append(f'{name} 声明为 store_or_staff 但两个归属列都没有')
        if mode in ('declared_none', 'root', 'email_list'):
            if len(str(spec.get('reason') or '').strip()) < 20:
                problems.append(f'{name} 必须写 ≥20 字的理由（不可擦/无主是要留痕的决策）')

    # 出口必须真的接上：只认 Call 节点，且不得被常量短路（第 41 项同口径）
    wired = False
    try:
        atree = ast.parse(read(os.path.join(ROOT, 'src', 'routes', 'admin.py')))
    except (SyntaxError, MemoryError) as exc:
        atree = None
        problems.append(f'src/routes/admin.py 解析失败，无法核验擦除出口：{exc}')
    if atree is not None:
        for node in ast.walk(atree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(node)
                     if isinstance(n, ast.Call) and ast.unparse(n.func).endswith('erasure.execute')]
            if not calls:
                continue
            wired = True
            decos = set()
            for d in node.decorator_list:
                tgt = d.func if isinstance(d, ast.Call) else d
                decos.add(tgt.attr if isinstance(tgt, ast.Attribute)
                          else (tgt.id if isinstance(tgt, ast.Name) else ''))
            if 'role_required' not in decos or 'login_required' not in decos:
                problems.append('擦除出口缺 login_required/role_required（不可逆动作不得只靠短语拦）')
            for b in ast.walk(node):
                if isinstance(b, ast.BoolOp) and \
                        any(isinstance(o, ast.Constant) and o.value is True for o in b.values) and \
                        any(isinstance(o, ast.Call)
                            and ast.unparse(o.func).endswith('backup_proof') for o in b.values):
                    problems.append('备份凭证校验被常量短路（凭证还在判但永远为真 = 假护栏）')
        if not wired:
            problems.append('没有任何路由调用 erasure.execute（能力存在但无出口 = 半成品）')

    # 无门槛旁路：CLI 里出现 --execute 等于给批量误删开后门。
    # 只看**代码位**的字符串常量，排除模块/函数/类文档串 —— 首版我用裸子串扫全文，
    # 当场被自己 docstring 里那句「这里没有 --execute」骗成假红（第 26 轮「子串匹配会被
    # docstring 骗过」的同族，这次骗的是我自己写的判据）。
    try:
        etree = ast.parse(read(os.path.join(ROOT, 'src', 'erasure.py')))
    except (SyntaxError, MemoryError):
        etree = None
        problems.append('src/erasure.py 解析失败，无法核验 CLI 旁路')
    if etree is not None:
        docids = set()
        for node in ast.walk(etree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = node.body[0] if getattr(node, 'body', None) else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)                         and isinstance(first.value.value, str):
                    docids.add(id(first.value))
        flags = [n.value for n in ast.walk(etree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and id(n) not in docids and '--execute' in n.value]
        if flags:
            problems.append('src/erasure.py 的 CLI 暴露了 --execute（不可逆动作必须走带确认与凭证的受控入口）')

    evidence.append(f'实表 {len(tables)} 张 ⇄ 归属图 {len(owning)} 条；删除顺序 {len(order or [])} 步')
    evidence.append(f'模式分布：' + json.dumps(
        {m: sum(1 for s in owning.values() if isinstance(s, dict) and s.get('mode') == m)
         for m in sorted({s.get('mode') for s in owning.values() if isinstance(s, dict)})},
        ensure_ascii=False))
    evidence.append('出口接线：' + ('已调用 erasure.execute' if wired else '未接线'))
    record('BLOCK', '42. 商家数据擦除归属图与出口护栏（分母 + 接线 + 备份凭证）',
           not problems, problems or evidence)


def check_authz_surface():
    """第 41 项：端点授权面的名册分母、默认拒绝接线与公开档红线。

    动因（2026-09-27 当场实测）：`app.url_map` 86 条规则里 **16 条不带任何视图级守卫装饰器**，
    盘上没有任何「公开端点名册」（`PUBLIC_*` 全仓 grep 0 命中），L4 全文只有 2 处 `url_map`
    使用且都不是授权断言 ⇒ 新增端点忘挂装饰器 = 静默公开，**没有任何判据会红**。
    上游规格（同轮 `gh api` 取回原文）：InvenTree `settings.py:521-522` 把 DRF 默认权限钉成
    `IsAuthenticated`；Odoo `ir_http.py:392` 按路由声明的 `auth` 单点派发（`:376` 连 CORS 预检
    都要显式写 `auth='none'`）；Frappe `__init__.py:581 whitelist(allow_guest=False)` 默认不开放。
    ⇒ 开放必须是**声明出来的**，而不是「没人拦」。

    本项只做静态对账（分母、接线、红线）；需要 `url_map` 的三方运行对账在 L4 S33 段，
    两侧互为对照，避免门禁拿被测模块自己的枚举器当唯一尺（定义 ≠ 声明）。
    """
    problems = []
    evidence = []
    ledger_rel = 'src/authz_ledger.py'
    ledger_path = os.path.join(ROOT, *ledger_rel.split('/'))
    if not os.path.isfile(ledger_path):
        record('BLOCK', '41. 端点授权面默认拒绝与名册对账（分母 + 接线 + 公开档红线）',
               False, [f'{ledger_rel} 不存在（授权名册整体缺失）'])
        return

    # ① 独立分母：门禁自己数一遍「带 route 装饰器的函数」，不借用名册的枚举器。
    #    按**函数**去重而不是按装饰器计数 —— 一个函数挂两条路由时端点 id 相同，
    #    名册侧必然合成一条，按装饰器数会比出假差（分母要按同一把尺的量纲来）。
    raw_routes = 0
    app_src = read(os.path.join(ROOT, 'src', 'app.py'))
    for rel in _s37_py_files():
        if not rel.startswith('src/') or not rel.endswith('.py'):
            continue
        try:
            tree = ast.parse(read(os.path.join(ROOT, *rel.split('/'))))
        except (SyntaxError, MemoryError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            hit = False
            for d in node.decorator_list:
                tgt = d.func if isinstance(d, ast.Call) else d
                if isinstance(tgt, ast.Attribute) and tgt.attr in (
                        'route', 'get', 'post', 'put', 'patch', 'delete'):
                    hit = True
                    break
            if hit:
                raw_routes += 1
    evidence.append(f'门禁独立数到的路由装饰器：{raw_routes} 处')

    # ② 借用名册枚举器，要求两侧分母一致（枚举器漏文件即红）
    import importlib
    import sys as _sys
    if ROOT not in _sys.path:
        _sys.path.insert(0, ROOT)
    try:
        az = importlib.import_module('src.authz_ledger')
        index = az.build_index()
    except Exception as exc:  # 名册加载崩了就是名册的问题，不得静默跳过
        record('BLOCK', '41. 端点授权面默认拒绝与名册对账（分母 + 接线 + 公开档红线）',
               False, [f'src/authz_ledger.py 导入/索引失败：{type(exc).__name__}: {exc}'])
        return
    if len(index) != raw_routes:
        problems.append(f'名册枚举器只索引到 {len(index)} 个端点，独立计数为 {raw_routes} '
                        f'（枚举面漏了，判据分母不可信）')
    if not index:
        problems.append('枚举器产出为空（零枚举一律判红，不得读成「没有要管的端点」）')

    # ③ 豁免档形状：地板、档位合法、reason 非空、methods 非空
    exempt = az.EXEMPT
    if len(exempt) < az.EXEMPT_FLOOR:
        problems.append(f'豁免档 {len(exempt)} 条低于地板 {az.EXEMPT_FLOOR}（清空名册=红线永不命中）')
    for ep, spec in sorted(exempt.items()):
        if spec.get('mode') not in az.MODES:
            problems.append(f'{ep} 档位非法：{spec.get("mode")!r}')
        if not str(spec.get('reason') or '').strip():
            problems.append(f'{ep} 缺 reason（豁免是决策，必须留下为什么）')
        if not spec.get('methods'):
            problems.append(f'{ep} 方法集为空（越界判定失去上界）')

    # ④ 公开档红线：管理/填报面不得被声明成匿名可达
    for ep, spec in sorted(exempt.items()):
        rule = index.get(ep, {}).get('rule', '')
        if spec.get('mode') == az.MODE_PUBLIC and (rule.startswith('/admin')
                                                   or rule.startswith('/h5')):
            problems.append(f'{ep}（{rule}）被声明为 public —— 管理/填报面不得匿名可达')

    # ⑤ bearer 档接线：函数体必须命中令牌鉴权调用，且不得与会话装饰器混用
    for ep, spec in sorted(exempt.items()):
        if spec.get('mode') != az.MODE_BEARER:
            continue
        entry = index.get(ep)
        if entry is None:
            problems.append(f'{ep} 在豁免档但枚举器找不到（端点名写错或已删）')
            continue
        calls = entry.get('calls', frozenset())
        if not (calls & az.BEARER_CALLS):
            problems.append(f'{ep} bearer 档未命中 `_guard()/_authenticate()`（跨商家可读）')
        if entry.get('guards'):
            problems.append(f'{ep} bearer 档混挂会话守卫装饰器：{sorted(entry["guards"])}')

    # ⑦ 静态兜底：枚举器看到「有路由、无守卫、又不在豁免档」= 静默公开，不依赖 url_map 也能红
    #    （否则把 healthz 从名册里删掉，本项会因「只剩 url_map 侧才知道漏登记」而漏判）
    for ep, entry in sorted(index.items()):
        if not entry.get('guards') and ep not in exempt and not ep.startswith('__parse_error__'):
            problems.append(f'{ep}（{entry.get("rule", "")}）有路由装饰器、无视图级守卫，'
                            f'也不在豁免档 —— 会被默认拒绝，且属于漏登记')

    # ⑥ 默认拒绝必须真的接在请求链上：只认 Call 节点，docstring 里的字样不算装配
    wired = False
    try:
        tree = ast.parse(app_src)
    except (SyntaxError, MemoryError) as exc:
        tree = None
        problems.append(f'src/app.py 解析失败，无法核验默认拒绝接线：{exc}')
    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            hooked = any(_gate_deco_name(d) == 'before_request' for d in node.decorator_list)
            if not hooked:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    txt = ast.unparse(sub.func)
                    if txt.endswith('backstop_decision'):
                        wired = True
            # 「Call 节点在」不等于「判定真的生效」：`True or backstop_decision(...)` 保留了
            # 节点却把判定变成恒真。只认接线不认短路，判据就等于给自我催眠留了后门。
            for sub in ast.walk(node):
                if not isinstance(sub, ast.BoolOp):
                    continue
                has_true = any(isinstance(o, ast.Constant) and o.value is True for o in sub.values)
                has_call = any(isinstance(o, ast.Call)
                               and ast.unparse(o.func).endswith('backstop_decision')
                               for o in sub.values)
                if has_true and has_call:
                    problems.append('默认拒绝判定被常量短路（`True or backstop_decision(...)` '
                                    '= 接线还在但永远放行，属假装配）')
        if not wired:
            problems.append('src/app.py 里没有任何 `@app.before_request` 调用 '
                            'authz_ledger.backstop_decision（名册存在但没进请求链 = 半成品）')

    evidence.append(f'豁免档：{len(exempt)} 条（public {sum(1 for s in exempt.values() if s["mode"] == az.MODE_PUBLIC)} / '
                    f'bearer {sum(1 for s in exempt.values() if s["mode"] == az.MODE_BEARER)} / '
                    f'inline {sum(1 for s in exempt.values() if s["mode"] == az.MODE_INLINE)}）')
    evidence.append(f'带视图级守卫端点：{len(az.guarded_endpoints())} / 枚举 {len(index)}')
    evidence.append('默认拒绝接线：' + ('已挂在 before_request 链上' if wired else '未接线'))
    # ⑧ 可达面（第 28 轮）：前七条全在判「该拒的有没有拒」，没有一条判「该放的放得进吗」。
    reach_p, reach_e = _s41_platform_reach_from_file()
    problems += reach_p
    evidence += reach_e
    record('BLOCK', '41. 端点授权面默认拒绝与名册对账（分母 + 接线 + 公开档红线）',
           not problems, problems or evidence)


_S41_ADMIN_REL = 'src/routes/admin.py'
_S41_ALLOW_NAME = '_PLATFORM_ALLOWED_ENDPOINTS'


def _s41_str_frozenset(tree, name):
    """取模块级 `NAME = frozenset({...})` / 集合字面量的字符串成员。读不到返回 None。"""
    for node in getattr(tree, 'body', []):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        val = node.value
        if isinstance(val, ast.Call) and _gate_deco_name(val) == 'frozenset' and val.args:
            val = val.args[0]
        if isinstance(val, (ast.Set, ast.List, ast.Tuple)):
            return {e.value for e in val.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        return None
    return None


def _s41_blueprint_prefix(tree, fallback):
    """从 `Blueprint('admin', ...)` 取端点前缀，避免把 'admin' 写死进判据。"""
    for node in getattr(tree, 'body', []):
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        if isinstance(val, ast.Call) and _gate_deco_name(val) == 'Blueprint' and val.args:
            first = val.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
    return fallback


def _s41_platform_reach(tree, prefix):
    """纯函数：`@role_required(ROLE_PLATFORM)` 端点 ⇄ 蓝图级平台白名单 的差集。

    动因（2026-09-27 第 28 轮 L4 当场实测）：新增的 `/admin/platform/merchants/<id>/erase`
    挂了平台角色装饰器，却被 `admin.py` 集中式 `before_request`（平台管理员只看元数据、
    白名单外一律 403）拦死 —— GET 实测 403。能力写完、护栏齐全，**唯一受众却进不去**，
    而门禁前七条子判据全在判"该拒的有没有拒"，无一条会红。
    上游把可达性做成路由声明的一部分（Odoo `ir_http.py:392` 按声明的 `auth` 单点派发），
    而不是"没人拦"。⇒ 平台端点名单与蓝图白名单必须逐条对账，缺一即红。
    """
    problems, evidence = [], []
    allowed = _s41_str_frozenset(tree, _S41_ALLOW_NAME)
    if allowed is None:
        return ([f'{_S41_ADMIN_REL} 读不到 `{_S41_ALLOW_NAME}` 的字符串集合字面量'
                 '（改名/改成运行时拼装 ⇒ 本判据空转，宁可可红不可假绿）'], evidence)
    plat = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and _gate_deco_name(dec) == 'role_required'):
                continue
            if any(isinstance(a, ast.Name) and a.id == 'ROLE_PLATFORM' for a in dec.args):
                plat.add(f'{prefix}.{node.name}')
    if not plat:
        problems.append(f'一处 `role_required(ROLE_PLATFORM)` 都没枚举到'
                        f'（全仓不可能零平台端点 ⇒ 判"没测到"，不判通过）')
    if not allowed:
        problems.append(f'`{_S41_ALLOW_NAME}` 为空 —— 蓝图级兜底会把所有平台端点拦死')
    blocked = sorted(plat - allowed)
    if blocked:
        problems.append(f'这些平台专属端点不在蓝图级平台白名单里，对它唯一的受众永远 403：{blocked}')
    evidence.append(f'可达面对账：平台端点 {len(plat)} / 白名单 {len(allowed)} / '
                    f'被拦死 {len(blocked)}（名单外即 403）')
    return problems, evidence


def _s41_platform_reach_from_file():
    path = os.path.join(ROOT, *_S41_ADMIN_REL.split('/'))
    if not os.path.isfile(path):
        return [f'{_S41_ADMIN_REL} 不存在，无法核验平台端点可达面'], []
    try:
        tree = ast.parse(read(path))
    except (SyntaxError, MemoryError, UnicodeDecodeError) as exc:
        return [f'{_S41_ADMIN_REL} 解析失败，无法核验平台端点可达面：{exc}'], []
    return _s41_platform_reach(tree, _s41_blueprint_prefix(tree, 'admin'))


def _gate_deco_name(node):
    """取装饰器末段名（`app.before_request` → `before_request`）。"""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ''


def check_change_trail():
    """第 40 项：变更归因链的接线、名册分母、破坏性出口配对与快照写点。

    动因（2026-09-27 当场实测）：17 张表 0 张有「谁改的」列、`src/routes/admin.py` 全文件
    只有 1 处 logger、17 处删除里 2 处是老板点的物理删除且无前像；
    `_compute_gross_margin()` 用目录当前值算历史毛利率 —— 删一个产品 / 改一次名 /
    改一次成本价，营收分文未动，毛利率静默移动 -0.2255 / +1.5072 / +6.4935 个百分点。
    上游规格（同轮 `gh api` 取回原文）：Odoo `odoo/orm/models.py:304-313` 由 metaclass
    自动注入 create_uid/write_uid；Frappe `database.py:102` 的 DEFAULT_COLUMNS 含
    modified_by、`delete_doc.py:240-258` 默认归档并存整行前像；
    InvenTree `part/models.py:584-604` 被引用即禁删。
    """
    problems, evidence = [], []
    trees, skipped = {}, []
    for rel in _s37_py_files():
        try:
            trees[rel] = ast.parse(read(os.path.join(ROOT, *rel.split('/'))))
        except SyntaxError as exc:
            skipped.append(f'{rel}:{exc.lineno}')
    if skipped:
        problems.append(f'这些 .py 解析失败（枚举范围不完整，不许静默少算）：{skipped[:5]}')
    p, e = _s40_roster_problems(trees)
    problems += p
    evidence += e
    p, e = _s40_delete_problems(trees)
    problems += p
    evidence += e
    problems += _s40_wiring_problems(trees)
    record('BLOCK', '40. 变更归因链与破坏性写入纳管（名册分母 + 出口配对 + 快照写点）',
           not problems, problems or evidence)


# ══════════════ 36. 运行期告警面与留痕有界性（BLOCK）══════════════
def _s36_dict_keys(node):
    """取一个 dict 字面量的顶层键名（用于枚举 build_metrics 的产出面）。"""
    if isinstance(node, ast.Dict):
        return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return set()


def _s36_append_jsonl_writers(trees):
    """AST 枚举「以追加方式打开 *.jsonl」的模块 —— 不看 grep 命中数，注释里的文件名不算写入者。"""
    writers = set()
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            if not (isinstance(fn, ast.Name) and fn.id == 'open'):
                continue
            mode = None
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for kw in node.keywords:
                if kw.arg == 'mode' and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if not mode or 'a' not in mode:
                continue
            blob = ast.unparse(node)
            if 'jsonl' in blob or _s36_jsonl_const_referenced(tree, blob):
                writers.add(rel)
    return writers


def _s36_jsonl_const_referenced(tree, blob):
    """open(PATH_CONST, 'a') 这种写法：路径是常量名，展开后再看是不是 .jsonl。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in blob:
                    v = ast.unparse(node.value)
                    if 'jsonl' in v:
                        return True
    return False


def check_alerting_and_trace_bounds():
    """运行期"数字→判定→出口"这条链必须齐备，且留痕有界性由判据而不是自觉保证。

    动因（第 23 轮当场实测）：`/healthz` 早就在算 `db_ms`、`ai.inflight/limit/rejected`、
    `diag.errors_5xx/slow/dropped/error_kinds`，但**没有一处把它们与阈值比较** —— 数字在、判断不在；
    阈值只活在 `src/performance.py:25-26` 的打标逻辑里。而 `错误率|阈值告警` 在门禁+L4 的合并 grep
    只有 1 命中，且那条是**业务损耗率** ⇒ 把告警面整块删掉，35 项门禁与三套测试都不会红。
    上游对照：Grafana/Uptime Kuma 的「阈值→规则→通知」三段、frappe `monitor` 的 ErrorLog 聚合。
    留痕半边：6 个模块以 append 写 `data/*.jsonl`，各自有 `*_MAX_BYTES`/`*_BACKUPS`，
    但第 32 项只管 `exit_audit` 一个 —— 新加一个留痕文件忘了上限，没人会红。
    """
    problems, evidence = [], []
    paths = ['src/alerts.py', 'src/app.py', 'src/rate_limit.py', 'src/agent_trace.py',
             'src/ai_trace.py', 'src/decision_cockpit.py', 'src/exit_audit.py', 'src/webhooks.py',
             'src/diag.py', 'src/logging_config.py', 'smoke_remote.py', '部署/.env.example']
    texts = {}
    for rel in paths:
        fp = os.path.join(ROOT, *rel.split('/'))
        if os.path.isfile(fp):
            texts[rel] = read(fp)
        else:
            problems.append(f'{rel} 不存在（告警面对账缺输入，不得记通过）')
    if problems:
        record('BLOCK', '36. 运行期告警面与留痕有界性（阈值 ⇄ 出口 ⇄ 上限）', False, problems)
        return

    try:
        trees = {rel: ast.parse(texts[rel]) for rel in texts if rel.endswith('.py')}
    except SyntaxError as exc:
        record('BLOCK', '36. 运行期告警面与留痕有界性（阈值 ⇄ 出口 ⇄ 上限）', False,
               [f'解析失败：{exc}'])
        return

    # ① 规则表：非空、code 唯一、metric 必须真在 build_metrics 的产出面里
    rules = None
    metrics_keys = set()
    for node in trees['src/alerts.py'].body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'ALERT_RULES' for t in node.targets):
            rules = node.value
        if isinstance(node, ast.FunctionDef) and node.name == 'build_metrics':
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return):
                    metrics_keys |= _s36_dict_keys(sub.value)
    items = list(getattr(rules, 'elts', []) or [])
    if not items or not metrics_keys:
        problems.append(f'枚举器空转：ALERT_RULES {len(items)} 条 / build_metrics 产出键 '
                        f'{len(metrics_keys)} 个（任意为 0 说明对账没发生）')
    else:
        codes, envs, bad_metric = [], [], []
        for it in items:
            kv = {}
            if isinstance(it, ast.Dict):
                for k, v in zip(it.keys, it.values):
                    if isinstance(k, ast.Constant):
                        kv[k.value] = v
            code = kv.get('code')
            env = kv.get('env')
            metric = kv.get('metric')
            if isinstance(code, ast.Constant):
                codes.append(code.value)
            if isinstance(env, ast.Constant):
                envs.append(env.value)
            if isinstance(metric, ast.Constant) and metric.value not in metrics_keys:
                bad_metric.append(str(metric.value))
        if len(set(codes)) != len(codes):
            problems.append(f'告警 code 有重复：{codes}')
        if bad_metric:
            problems.append(f'规则引用了 build_metrics 不存在的聚合字段（判据会静默不触发）：{bad_metric}')
        missing_env = [e for e in envs if f'{e}=' not in texts['部署/.env.example']
                       and f'# {e}=' not in texts['部署/.env.example']]
        if missing_env:
            problems.append(f'阈值键未登记进 部署/.env.example（可配却无人知道可配）：{missing_env}')
        evidence.append(f'规则 {len(items)} 条、code {len(set(codes))} 个、阈值键 {len(set(envs))} 个均已登记')

    # ② 出口：/healthz 必须真的调用判定并并入响应；smoke_remote 必须读同一份（两处同进同退）
    healthz_fn = next((f for f in ast.walk(trees['src/app.py'])
                       if isinstance(f, ast.FunctionDef) and f.name == 'healthz'), None)
    if healthz_fn is None:
        problems.append('src/app.py 里找不到 healthz 视图（改名即视为漏测）')
    else:
        called = any(isinstance(c, ast.Call) and ast.unparse(c.func).endswith('healthz_fields')
                     for c in ast.walk(healthz_fn))
        # dict 里的 `**alert_fields` 在 AST 中是 key=None 的 Dict 项，不是 Starred 节点
        spread = any(isinstance(d, ast.Dict) and any(
            k is None and 'alert_fields' in ast.unparse(v) for k, v in zip(d.keys, d.values))
            for d in ast.walk(healthz_fn))
        if not called:
            problems.append('/healthz 未调用 alerts.healthz_fields（规则表成了死代码）')
        if not spread:
            problems.append('/healthz 未把 alerts 并进响应体（算了不出口 = 运维仍看不到）')
        if not (called and spread):
            return record('BLOCK', '36. 运行期告警面与留痕有界性（阈值 ⇄ 出口 ⇄ 上限）', False, problems)
    if "'alerts'" not in texts['smoke_remote.py'] and '"alerts"' not in texts['smoke_remote.py']:
        problems.append('smoke_remote.py 不读 alerts（部署侧冒烟与线上判定不同源，等于只有一半边）')

    # ③ 留痕有界性：每个"追加写 JSONL"的模块都要同时有上限常量与备份份数常量
    writers = _s36_append_jsonl_writers(trees)
    if not writers:
        problems.append('未枚举到任何以追加方式写 .jsonl 的模块（枚举器失效，不判通过）')
    else:
        unbounded = []
        for rel in sorted(writers):
            # "定义了上限"不算，必须**被引用**：改名成 _DISABLED_BACKUPS 这种"留着常量、
            # 断掉使用"的形态本轮变异实测能骗过后缀匹配（后缀仍在），只有查 Load 引用才拦得住。
            defined, loaded = {}, {}
            for node in ast.walk(trees[rel]):
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            defined[tgt.id] = node
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    loaded[node.id] = loaded.get(node.id, 0) + 1
            def _live(suffix):
                # 定义处是 Store 上下文，所以 Load 计数 ≥1 就等于"真在被使用"；
                # （写 ≥2 是我自己造的假门槛：LOG_MAX_BYTES 全文只在一处比较里出现）
                return any(n.endswith(suffix) and loaded.get(n, 0) >= 1 for n in defined)
            has_bytes, has_backups = _live('MAX_BYTES'), _live('BACKUPS') or _live('backupCount')
            if not (has_bytes and has_backups):
                unbounded.append(f'{rel}(上限被引用={has_bytes}, 备份份数被引用={has_backups})')
        if unbounded:
            problems.append('这些留痕写入者缺上限或备份份数常量（磁盘无界增长）：' + '; '.join(unbounded))
        else:
            evidence.append(f'{len(writers)} 个 JSONL 追加写入者全部带 MAX_BYTES + BACKUPS 上限')

    # ④ 磁盘实测有界：任何留痕文件不得超过其模块上限的 2 倍（轮转失效的可移植探测）
    data_dir = os.path.join(ROOT, 'data')
    caps = []
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id.endswith('MAX_BYTES'):
                        val = _fold_int(node.value)
                        if val:
                            caps.append((tgt.id, val))
    ceiling = max([v for _, v in caps] + [0])
    seen = []
    if os.path.isdir(data_dir):
        for fn in sorted(os.listdir(data_dir)):
            if fn.endswith('.jsonl') or '.jsonl.' in fn:
                seen.append(fn)
                size = os.path.getsize(os.path.join(data_dir, fn))
                if ceiling and size > ceiling * 2:
                    problems.append(f'留痕文件 {fn} 实测 {size}B 超过最大上限 {ceiling}B 的 2 倍'
                                    '（轮转疑似失效）')
    if not seen:
        evidence.append('本次 data/ 下无留痕文件（未跑过流量），磁盘腿记 UNVERIFIED 不记通过')
    elif ceiling:
        evidence.append(f'磁盘实测 {len(seen)} 个留痕文件，最大上限 {ceiling}B，全部在 2 倍界内')

    record('BLOCK', '36. 运行期告警面与留痕有界性（阈值 ⇄ 出口 ⇄ 上限）',
           not problems, problems or evidence)


# ══════════════ 35. 覆盖率地板在册且可执行（BLOCK）══════════════
def check_coverage_floor():
    """覆盖率工具必须「装得上、有地板、地板被执行」，否则它只是一个看起来存在的文件。

    动因（第 22 轮当场实测）：`run_coverage.py` 与 `.coveragerc` 三副本都在、
    `requirements.txt:41` 声明了 `coverage>=7.0`，但项目解释器里根本没装 coverage ⇒
    跑一次直接 rc=1「未安装 coverage 包」；而 `.coveragerc` 全文 **没有 fail_under**，
    门禁 33 项里 coverage 只出现在「交付名单」和「台账排除名单」两处，**没有任何一处调用它**。
    也就是说：这条防护网既没跑过、也没有阈值、也没人知道它坏了。
    上游对照：Airflow/Superset/Posthog 都在 CI 里带 `--cov-fail-under`（数值随基线漂移有记录），
    frappe 的 coverage 报告是 setup 流程的一部分。
    """
    problems, evidence = [], []
    rc_path = os.path.join(ROOT, '.coveragerc')
    runner = os.path.join(ROOT, 'run_coverage.py')
    if not os.path.isfile(rc_path) or not os.path.isfile(runner):
        problems.append('缺 .coveragerc 或 run_coverage.py（工具丢失不得记通过）')
        record('BLOCK', '35. 覆盖率地板在册且可执行（fail_under ⇄ 实测基线）', False, problems)
        return
    # 用 configparser 读，不用正则：正则会把注释掉的那行也算"在册"
    # （本轮变异实测：`# fail_under = 80` 在正则判据下仍判绿 ⇒ 注释一下就等于删掉，
    #   这不是"地板在不在"的判据，是"字符串在不在"的判据）
    import configparser

    cp = configparser.ConfigParser()
    try:
        cp.read(rc_path, encoding='utf-8')
    except configparser.Error as exc:
        problems.append(f'.coveragerc 解析失败：{exc}')
        record('BLOCK', '35. 覆盖率地板在册且可执行（fail_under ⇄ 实测基线）', False, problems)
        return
    floor_raw = (cp['report'].get('fail_under') if cp.has_section('report') else None)
    if floor_raw is None:
        problems.append('.coveragerc 的 [report] 段没有 fail_under'
                        '（工具能跑但永远不会"不够"，等于没有地板；注释掉同样算没有）')
        record('BLOCK', '35. 覆盖率地板在册且可执行（fail_under ⇄ 实测基线）', False, problems)
        return
    try:
        floor = float(floor_raw)
    except ValueError:
        problems.append(f'fail_under={floor_raw!r} 不是数字')
        record('BLOCK', '35. 覆盖率地板在册且可执行（fail_under ⇄ 实测基线）', False, problems)
        return
    if not (0 < floor < 100):
        problems.append(f'fail_under={floor} 不是有效地板（0 等于没有，100 等于冻结）')

    # 地板必须"低于实测基线"并写明基线从哪来（零余量地板＝冻结增长，见 MEMORY 同名条）
    base = re.search(r'COVERAGE_BASELINE\s*=\s*(\d+(?:\.\d+)?)', read(runner))
    if not base:
        problems.append('run_coverage.py 未记录 COVERAGE_BASELINE（地板无出处，改天数无人知）')
    else:
        bfloat = float(base.group(1))
        if floor >= bfloat:
            problems.append(f'地板 {floor} ≥ 实测基线 {bfloat}：抬地板必须同时抬实测，否则每次门禁都红')
        else:
            evidence.append(f'地板 {floor} < 实测基线 {bfloat}%（余量 {round(bfloat - floor, 1)} 个点）')

    # 可执行性：解释器里真能 import coverage
    probe = subprocess.run([PY, '-c', 'import coverage; print(coverage.__version__)'],
                           capture_output=True, text=True, cwd=ROOT)
    if probe.returncode != 0:
        problems.append('当前解释器导不进 coverage（' +
                        (probe.stderr or '').strip().splitlines()[-1][:80] +
                        f'）⇒ requirements 声明了但本机/CI 装不上，工具实际跑不起来：{PY}')
    else:
        evidence.append(f'coverage {probe.stdout.strip()} 可在门禁解释器内导入')

    # 真跑一次"地板生效"的端到端证据：用临时小模块证明低于地板会退出非零
    tmp = tempfile.mkdtemp(prefix='ican_cov_floor_')
    try:
        mod = os.path.join(tmp, 'tiny_target.py')
        with open(mod, 'w', encoding='utf-8') as fh:
            fh.write('def used():\n    return 1\n\n\ndef never_called():\n    return 2\n'
                     'def also_never():\n    return 3\n')
        env = dict(os.environ, COVERAGE_FILE=os.path.join(tmp, '.cov'))
        run = subprocess.run([PY, '-m', 'coverage', 'run', '--data-file', env['COVERAGE_FILE'], mod],
                             capture_output=True, text=True, cwd=tmp, env=env)
        rep = subprocess.run([PY, '-m', 'coverage', 'report',
                              '--data-file', env['COVERAGE_FILE'],
                              '--fail-under=100', mod],
                             capture_output=True, text=True, cwd=tmp, env=env)
        if run.returncode != 0:
            problems.append(f'地板探针没能跑起来：{(run.stderr or "").strip()[:100]}')
        elif rep.returncode == 0:
            problems.append('探针：未覆盖的模块在 `--fail-under=100` 下仍退出 0 ⇒ 地板不会被执行')
        else:
            evidence.append('地板可执行性已证：未达标时 coverage 以非零退出（探针 tiny_target）')
    except OSError as exc:
        problems.append(f'覆盖率探针建不起来（判没测到，不判通过）：{exc}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ④ 工具自己的传播链：`coverage report` 的退出码必须一路走到 sys.exit
    #    （本轮实测 `run_coverage.py` 里 `_report()` 丢返回值、末尾只按套件结果 exit 0，
    #     于是"地板不达标"也会被印成「总体：全部通过」——地板写在配置里却没人执行。）
    runner_tree = None
    try:
        runner_tree = ast.parse(read(runner))
    except SyntaxError as exc:
        problems.append(f'run_coverage.py 语法错误：{exc}')
    if runner_tree is not None:
        report_returns = False
        for fn in ast.walk(runner_tree):
            if isinstance(fn, ast.FunctionDef) and fn.name == '_report':
                for node in ast.walk(fn):
                    if isinstance(node, ast.Return) and node.value is not None:
                        report_returns = True
        if not report_returns:
            problems.append('run_coverage.py 的 _report() 不返回 coverage 退出码'
                            '（地板被丢弃，不达标也会印「全部通过」）')
        main_fn = next((f for f in runner_tree.body
                        if isinstance(f, ast.FunctionDef) and f.name == 'main'), None)
        exits_with_floor = False
        if main_fn:
            for node in ast.walk(main_fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'exit' and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == 'sys'):
                    src = ast.unparse(node)
                    if 'floor' in src:
                        exits_with_floor = True
        if not exits_with_floor:
            problems.append('main() 的 sys.exit 表达式里没有引用地板结果'
                            '（退出码与覆盖率达标与否脱钩）')
        if report_returns and exits_with_floor:
            evidence.append('地板退出码传播链完整：_report() 返回 → main 的 sys.exit 引用它')

    record('BLOCK', '35. 覆盖率地板在册且可执行（fail_under ⇄ 实测基线）',
           not problems, problems or evidence)


# ══════════════ 8/9/11. 测试（BLOCK）══════════════
def _is_failure_line(ln):
    """一行输出算不算失败证据 —— 只看**非通过行上的裸失败标记**，别的一律交给返回码。

    动因（2026-09-27 第 34 轮，同一族两次实测）：旧口径是整段输出做子串
    `'❌' not in out`，于是 ① 夹具让被测函数打到 stdout 的诊断行、
    ② **通过项标题里引用了那个字符本身**（标题写着"不往 stdout 印 ❌"）
    都能把 1384 项 0 失败的一套测试记成阻断项失败，而摘录位上摆着的正是那条噪声。

    本函数的口径是**用真实日志量出来的**（本轮 L4 全量输出 1390 行）：
    · 整行含 `❌` = 1 行，且那一行以 ✅ 开头 ⇒ 旧口径的唯一命中就是假红；
    · 含"失败/FAILED + 数字"的 ✅ 说明行 = 4 行（"失败路径返回 503"、
      "commit：4 失败 + 2 新增"、"非 2xx 记为一次失败并排到 5 分钟后重试"…）；
    · `Traceback (most recent call last):` = 3 行，而该轮失败项数为 0（错误路径夹具的正当产物）。
    ⇒ 把"结构词"或 Traceback 接进来会再造一批假红；崩溃由 `returncode != 0` 兜住，
      计数由脚本自报汇总行（`self_report` 正则）兜住，这里只留**行为**不认**描述**。
    """
    s = ln.strip()
    return bool(s) and '❌' in s and not s.startswith('✅')


def _failure_details(out, script):
    """失败时的可归因明细：❌ 行 + 紧随其后的条目行 + 原文落盘路径。

    动因（2026-09-25 第 15 轮实测）：漏洞回归在门禁链内偶发失败、单跑全绿，
    而本函数旧版只保留含 ❌ 的行——三套测试的失败清单都打成
    `❌ 失败项:` + 若干 `  - <条目名>`，条目行不含 ❌ 被全部丢弃，
    于是"红了但不知道为什么红"。落盘路径写系统临时目录，
    不往交付副本树里塞日志（生成物纪律）。
    """
    lines = out.splitlines()
    details = [ln for ln in lines if _is_failure_line(ln)][:12]
    for i, ln in enumerate(lines):
        if '失败项' in ln:
            details += [x.strip() for x in lines[i + 1:i + 12] if x.strip()][:12]
            break
    seen, kept = set(), []
    for d in details:
        if d not in seen:
            seen.add(d)
            kept.append(d)
    if kept:
        # 只取 basename：script 可能是绝对路径（L4 探针套件就是），旧写法把整条路径嵌进文件名
        # ⇒ Windows `Invalid argument`，于是"给失败留证据"这一步自己先失败（第 34 轮端到端腿实测）。
        safe = re.sub(r'[^0-9A-Za-z_.-]', '_', os.path.basename(script).replace('.py', ''))
        path = os.path.join(tempfile.gettempdir(),
                            f'ican_gate_fail_{safe}_{int(time.time())}.log')
        try:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(out)
            kept.append(f'原文已落盘：{path}')
        except OSError as exc:   # 落不下去也要如实说，不能静默丢掉证据
            kept.append(f'原文落盘失败（须人工留存输出）：{type(exc).__name__}: {exc}')
    return kept[:16] or ['脚本无 ❌ 输出但返回码非 0（可能提前异常退出）——不得当通过']


def run_test(name, script, label, self_report=None):
    # P7-D 计数口径改造：优先解析脚本自报汇总行（单一真相源），
    # 消除「smoke/vuln 汇总行含 ✅ 而 L4 不含」导致的 -1 补偿口径漂移（258 vs 259）。
    r = subprocess.run([PY, script], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout or '') + (r.stderr or '')
    n = None
    if self_report:
        _m = re.search(self_report, out)
        if _m:
            n = int(_m.group(1))
    if n is None:
        # 回退口径：✅ 字符计数。汇总行格式漂移时才会走到这里，显式告警不静默。
        n = out.count('✅')
        print(f'     ⚠️ {script} 未匹配自报计数正则，回退 ✅ 字符计数（请核对脚本汇总行格式）')
    ok = r.returncode == 0 and not any(_is_failure_line(ln) for ln in out.splitlines())
    record('BLOCK', f'{label} {name}', ok, [] if ok else _failure_details(out, script))
    return n


def main():
    fast = '--fast' in sys.argv
    print('═' * 62)
    print('  质量门禁 — 依据《代码审查标准与流程.md》自动化阻断项检查')
    print('═' * 62)

    # 每一项都经 `_guarded` 进入（第 47 轮 M152）：任何一项自己崩 ⇒ 只有那一项判"没测到"，
    # 后面的项照常跑、摘要行照常印。此前这里是 48 个裸调用，形态是"一条腿崩 = 全套件瞎"。
    _guarded(check_compile)
    _guarded(check_safe_filter)
    _guarded(check_inline_style)
    _guarded(check_private_call)
    _guarded(check_blind_review)
    _guarded(check_hardcoded_color)
    _guarded(check_ruff)
    _guarded(check_static_budget)
    _guarded(check_health_endpoint)
    _guarded(check_delivery_consistency)
    _guarded(check_openapi_drift)
    _guarded(check_ai_eval_floor)
    _guarded(check_schema_drift)
    _guarded(check_query_budget)
    _guarded(check_evidence_scripts_wired)
    _guarded(check_delivery_drift)
    _guarded(check_snapshot_assets)
    _guarded(check_migration_ledger)
    _guarded(check_version_contract)
    _guarded(check_supply_chain_pins)
    _guarded(check_vendor_assets)
    _guarded(check_security_lint)
    _guarded(check_release_ledger)
    _guarded(check_release_identity)
    _guarded(check_offline_queue_scope)
    _guarded(check_line_ending_policy)
    _guarded(check_observability_chain)
    _guarded(check_exit_guardrails)
    _guarded(check_api_contract_surface)
    _guarded(check_api_error_contract)
    _guarded(check_coverage_floor)
    _guarded(check_alerting_and_trace_bounds)
    _guarded(check_maintenance_budget)
    _guarded(check_backup_evidence_chain)
    _guarded(check_change_trail)
    _guarded(check_authz_surface)
    _guarded(check_erasure_surface)
    _guarded(check_outbound_surface)
    _guarded(check_write_conflict)
    _guarded(check_dependency_supply_chain)
    _guarded(check_restore_reversibility)
    _guarded(check_commit_block_chain)
    _guarded(check_local_gate_ledger)
    _guarded(check_patch_reversibility)
    _guarded(check_backup_eligibility)
    _guarded(check_sync_egress_single_entry)
    _guarded(check_backup_retention)
    _guarded(check_disposition_channel)
    _guarded(check_disposition_lock)
    _guarded(check_business_calendar)

    if not fast:
        print('─' * 62)
        # 此处原先会先跑 init_db.py 准备数据，而 init_db 内部是 drop_all() 全库
        # 重建——等于每跑一次门禁就清空一次业务库（答辩演示数据一并没掉）。
        # 现在 smoke_test / test_vuln_regression 各自运行在 test_fixtures 提供的
        # 进程私有临时库上，自带建表与播种，不再需要外部准备数据，故移除。
        n1 = _guarded(run_test, '冒烟测试', 'smoke_test.py', '8.', r'总检查项[:：]\s*(\d+)')
        n2 = _guarded(run_test, '漏洞回归测试', 'test_vuln_regression.py', '9.', r'检查项[:：]\s*(\d+)')
        n3 = _guarded(run_test, 'L4 业务正确性', 'test_l4_assertions.py', '11.', r'断言通过\s+(\d+)\s+项')
        n4 = _guarded(run_test, '限流留痕', 'test_rate_limit_trace.py', '14.', r'断言通过\s+(\d+)\s+项')
        # 第 25 轮 M86：PH3 演示重建回归接进阻断链。它此前挂在豁免表里"手工触发"，
        # 而 2026-09-26 正是它抓到 `/admin/platform/demo-reset` 的 500（租户守卫读游离
        # current_user ⇒ DetachedInstanceError）；HEAD 上它已经红了却没人知道，
        # 这就是"豁免 = 隐身"的实测代价。整文件 28s，可接受。
        n5 = _guarded(run_test, 'PH3 演示重建与权限回归', 'test_ph3_demo.py', '39.', r'全部通过 (\d+) 项')
        print(f'     ↳ 冒烟断言 {_count_or_missing(n1)} / 漏洞回归断言 {_count_or_missing(n2)} / '
              f'L4 业务断言 {_count_or_missing(n3)} / '
              f'限流留痕断言 {_count_or_missing(n4)} / PH3 检查项 {_count_or_missing(n5)}')
        # 覆盖透明化：列出仓库根存在但未纳入门禁的测试文件，避免「薛定谔的测试」
        # （看起来覆盖很全、实际无人运行）长期不可见。
        _ran = {'smoke_test.py', 'test_vuln_regression.py', 'test_l4_assertions.py',
                'test_fixtures.py', 'test_rate_limit_trace.py', 'test_ph3_demo.py'}
        # 有意不进门禁的测试（文件头均有豁免声明）：Dify 接入态 network-only /
        # 依赖 PH3 演示数据重建，按 --network 或手动触发。
        _exempt = {'test_dify_integration.py'}
        _other = [f for f in sorted(os.listdir(ROOT))
                  if f.startswith('test') and f.endswith('.py')
                  and f not in _ran and f not in _exempt]
        if _other:
            print('     ↳ 未纳入门禁的测试文件（需人工确认仍有效或按 --network 手动触发）：'
                  + '、'.join(_other))
    else:
        print('⏭️  已跳过测试（--fast 模式）')

    _guarded(check_gate_self_dead_code)

    # 第 56 项排在这里：它要读 CRASHED_CHECKS 的累计账，所以必须在全部检查项之后；
    # 但它又必须**早于**第 20 项，否则第 20 项会把"56 写了号却没记录"判成编号空洞。
    # 晚于本项才发生的崩溃（第 20/22 项自己崩）由下面的退出码复核兜住 —— 两把尺各管一段。
    _guarded(check_gate_item_isolation)

    # 第 20 项**必须**排在所有 record() 之后：它要做的是「静态枚举 ⇄ 实际记录」双向对账，
    # 提前调用就等于拿半截 results 对账（项号 8/9/11/14 会被漏计）。打印顺序因此不按项号排。
    _guarded(check_gate_numbering, fast=fast)

    print('═' * 62)
    blocked = [r for r in results if r[0] == 'BLOCK' and not r[2]]
    warned = [r for r in results if r[0] == 'WARN' and not r[2]]
    if blocked:
        print(f'🔴 门禁未通过：{len(blocked)} 项阻断项失败')
        for _, name, _, _ in blocked:
            print(f'   · {name}')
    if CRASHED_CHECKS:
        # 退出面独立于 blocked 再判一次：第 20/22 项自己崩时，第 56 项**已经**记过"零崩溃"，
        # 只看 blocked 就会把"有腿崩了"印成 🎉（那正是 M152 要堵的假绿形态）。
        print(f'🔴 有 {len(CRASHED_CHECKS)} 个检查项抛出异常未完成：'
              + '、'.join(_crashed_names()))
    if blocked or CRASHED_CHECKS:
        sys.exit(1)
    print(f'🎉 质量门禁全部通过（阻断项 {len([r for r in results if r[0] == "BLOCK"])} 项全绿'
          + f'，包裹器捕获崩溃 {len(CRASHED_CHECKS)} 项'
          + (f'，另有 {len(warned)} 项告警待优化）' if warned else '）'))
    sys.exit(0)


if __name__ == '__main__':
    if '--write-budget' in sys.argv:
        _i = sys.argv.index('--write-budget')
        _reason = ''
        for _a in sys.argv[_i + 1:]:
            if _a.startswith('--reason='):
                _reason = _a.split('=', 1)[1].strip()
        if not _reason and any(a.startswith('--reason') for a in sys.argv[_i + 1:]):
            print('❌ --reason 必须带值（放宽预算必须留下"为什么"）')
            sys.exit(2)
        sys.exit(write_budget(_reason))
    main()
