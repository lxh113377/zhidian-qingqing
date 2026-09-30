"""版本字面量对齐器（第 35 轮 M103）：一条命令列全载体、逐格核、要么全改要么不改。

为什么要有这个文件
------------------
第 34 轮收口时我跑过 `grep -rln "1\\.12\\.0"`，**输出里明明有开发副本 README**，
还是只改了 4 个载体中的 3 个 ⇒ 权威门禁第 25 项判红，且 L4 的一条变异腿
`AssertionError: 变异未命中锚点：README.md` 直接崩掉整轮 L4。
"记住有几个载体"是人的活，而且是最容易漏的一种；把它写成表 + 断言，机器就能替我记。

口径
----
· 载体的唯一真相源 = 下面的 `CARRIERS`（新增载体必须同时加一行，漏加即被 `--check` 之外的
  那条"仓里还残留别的版本字面量"判据抓到）；
· `--check` 只读：所有载体的锚点版本必须**完全一致**，且与 `src/version.py` 的 `APP_VERSION` 相同；
· `--apply NEW` 才写：每个锚点必须**恰好命中一次**，命中 0 次或多次都拒绝改该文件并退出非 0
  （"静默少改一个文件"正是本工具要消灭的失效形态）；
· 写完必须读回逐载体打印 `OK/STALE`，任一 STALE 即退出非 0。
镜像副本（`deploy-free/`、`05-源代码全文/`）不在本工具职责内 —— 它们由 `sync` 面单向同步，
本工具只保证**源**一致，避免造出第二个同步器（同一事实两处写）。
"""
import argparse
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# (相对路径, 锚点正则, 是否可缺面)
# 第 4 格 `optional=True` 的是**仓根** README：交付副本嵌在仓库深处
# （`交付物/提交包/工程佐证材料/05-源代码全文/`），对它而言 `../README.md` 指的是别处
# ——第 35 轮 L4 在权威副本里跑这条腿时就是这么红的。所以它按 git 顶层解析，
# 没有 git（ZIP 解压态）就如实记 N/A，不判红也不假装通过。
CARRIERS = (
    ('src/version.py', r"^APP_VERSION\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'", False),
    ('README.md', r"当前版本：\*\*v([0-9]+\.[0-9]+\.[0-9]+)\*\*", False),
    ('UPGRADE.md', r"当前版本：\*\*v([0-9]+\.[0-9]+\.[0-9]+)\*\*", False),
    ('REPO_ROOT_README', r"当前版本：v([0-9]+\.[0-9]+\.[0-9]+)", True),
)


def _read(path):
    with io.open(path, encoding='utf-8', newline='') as fh:
        return fh.read()


def _git_toplevel(root):
    """git 顶层目录；取不到返回 None（ZIP 解压态、非仓库面都走这条路）。"""
    try:
        r = subprocess.run(['git', '-C', root, 'rev-parse', '--show-toplevel'],
                           capture_output=True, text=True)
    except OSError:
        return None
    top = (r.stdout or '').strip()
    return top if r.returncode == 0 and top else None


def carrier_path(rel, root):
    """把载体名解析成绝对路径；`REPO_ROOT_README` 优先按 git 顶层，退回上一级目录。"""
    if rel != 'REPO_ROOT_README':
        return os.path.normpath(os.path.join(root, rel)), False
    top = _git_toplevel(root)
    if top:
        cand = os.path.join(top, 'README.md')
        if os.path.isfile(cand):
            return cand, True
    return os.path.normpath(os.path.join(root, '..', 'README.md')), True


def plan(root=ROOT):
    """纯函数：读一遍所有载体，返回逐载体读数 + 问题清单（不写任何东西）。"""
    rows, problems = [], []
    for rel, pat, optional in CARRIERS:
        path, is_root = carrier_path(rel, root)
        if not os.path.isfile(path):
            if is_root:
                rows.append((rel + '（本面无仓根 README，如 ZIP 解压态）', None))
                continue
            problems.append(f'载体文件不存在：{rel}（登记了路径却没文件 = 判据面空转）')
            rows.append((rel, None))
            continue
        hits = re.findall(pat, _read(path), re.M)
        if len(hits) != 1:
            problems.append(f'{rel}：锚点命中 {len(hits)} 次（应为 1 次；0 次=改了写法，多次=两处版本行）')
        rows.append((rel, hits[0] if hits else None))
    vals = {v for _r, v in rows if v}
    if len(vals) > 1:
        problems.append(f'载体之间版本不一致：{sorted(vals)} ⇒ 必须一次改全，不许留半套')
    return rows, problems


def _rewrite_carrier(rel, pat, new, root=ROOT):
    """把一个载体的锚点行里的版本号换成 `new`。返回 (状态, 说明)；状态非 'wrote'/'same' 一律不写。"""
    path, _opt = carrier_path(rel, root)
    lines = _read(path).split('\n')
    idx = [i for i, ln in enumerate(lines) if re.search(pat, ln)]
    if len(idx) != 1:
        return 'refused', f'定位到 {len(idx)} 行锚点（只接受恰好 1 行，不猜改哪一行）'
    line = lines[idx[0]]
    old = re.search(pat, line).group(1)
    if old == new:
        return 'same', old
    lines[idx[0]] = line.replace(old, new)
    with io.open(path, 'w', encoding='utf-8', newline='') as fh:
        fh.write('\n'.join(lines))
    return 'wrote', f'{old} -> {new}'


def apply_version(new, root=ROOT):
    """把每个载体的锚点版本改成 `new`；任一载体定位不到唯一锚点即**整体拒绝**（不做部分成功）。"""
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', new or ''):
        print(f'❌ --version 必须是 X.Y.Z 三段数字，收到 {new!r}')
        return 2
    rows, problems = plan(root)
    if problems:
        print('❌ 先修对齐再改版本（带着不一致改版本 = 把混乱再复制一遍）：')
        for p in problems:
            print('   -', p)
        return 1
    for rel, pat, _opt in CARRIERS:
        state, why = _rewrite_carrier(rel, pat, new, root)
        print(f'   {state:8} {rel}：{why}')
        if state == 'refused':
            return 1
    rows2, problems2 = plan(root)
    print('逐载体读回：')
    for rel, val in rows2:
        print(f"   {'OK   ' if val == new else 'STALE'} {rel} = {val}")
    for p in problems2:
        print('   ❌', p)
    if problems2 or any(v != new for _r, v in rows2):
        return 1
    print(f'✅ 全部载体已对齐到 v{new}（镜像副本随后由同步面单向覆盖，本工具不碰）')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='版本字面量载体枚举与对齐（第 35 轮 M103）')
    ap.add_argument('--check', action='store_true', help='只读：列逐载体读数并判一致性')
    ap.add_argument('--version', help='配合 --apply：目标版本号 X.Y.Z')
    ap.add_argument('--apply', action='store_true', help='真写入（改完全载体后逐个读回）')
    args, unknown = ap.parse_known_args(argv if argv is not None else sys.argv[1:])
    if unknown:
        print(f'❌ 未知参数：{unknown}（支持 --check / --apply --version X.Y.Z）')
        return 2
    if not args.check and not args.apply:
        print('用法见 --help：必须显式带 --check 或 --apply（不猜你想不想写文件）')
        return 2
    if args.apply:
        return apply_version(args.version)
    rows, problems = plan()
    for rel, val in rows:
        print(f"   {'READ' if val else 'MISS'} {rel} = {val}")
    for p in problems:
        print('   ❌', p)
    if problems:
        return 1
    print('✅ 载体一致（--check 通过）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
