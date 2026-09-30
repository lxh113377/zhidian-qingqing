#!/usr/bin/env python3
# 交付副本漂移检查器（门禁第 21 项候选的独立实现；当前先挂 CI，不挤 quality_gate.py。
# 项号当场实测：18=查询预算、19=证据脚本接线、20=编号唯一性自检 ⇒ 本守卫只排队到 21。对标第 11 轮实证过：照旧项号猜编号会撞车）。
# 治两类"门禁看不见但交付包就是错的"漂移：
#   A. .github：仓库根才是 Actions 唯一生效配置，交付副本 05-源代码全文/.github 是给人读的镜像，
#      曾经长期是根迁移前的旧版（还写着"本项目尚未初始化 git 仓库"）。
#   B. 根级脚本白名单外的那批文件：quality_gate 的 ROOT_DELIVERED 名单不含它们时，
#      三副本漂移无人拦截（含被门禁第 14 项自己跑的 test_rate_limit_trace.py）。
# 退出码：0 = 无漂移；1 = 有漂移（CI 阻断）。--quiet 只打结论行。
import argparse
import hashlib
import os
import sys

MIRROR_SUBPATH = os.path.join('交付物', '提交包', '工程佐证材料', '05-源代码全文')
FREE_SUBPATH = 'deploy-free'
# 既不在 ROOT_DELIVERED 名单、又确实需要三副本一致的文件（相对各自根目录）
EXTRA_TRACKED = ('smoke_remote.py', 'test_rate_limit_trace.py', 'migrate_sqlite_to_mysql.py',
                 'check_delivery_drift.py', 'check_mirror_freshness.py', 'push_snapshot_mirror.py')
# 允许只存在于 dev 的文件（清单外的调试/临时件；出现即提示，不阻断）
# 真·调试残片：只要还在 dev 且没进交付副本就 WARN（要么删、要么转正，别留无主状态）
DEV_ONLY_HINTS = ('_v.py',)
# 有意留在 dev、不进交付包的**测试资产**：必须写理由 + 当次实测三态 + 待办号，否则就是
# 「看起来有测试、实际无人跑」的孤儿（本项目第 16/19 项反复在案的那类病）。
DEV_ONLY_ACK = {
    'qa_verify_tmp.py': (
        '第三方视角独立 QA 套件；2026-09-25 实测 194 项 / 49 失败 / 1 跳过。'
        '不进交付包、也暂不进 BLOCK 链的原因：本轮刚把它的夹具补回多租户+随包形状'
        '（老 setup() 缺 merchant_id/store_id/pack_id，提交必被判白名单外，一条根因级联出十几条红），'
        '余下红项属产品契约漂移（admin CRUD、h5 鉴权跳转、MySQL 配置串、弱口令 UI 文案）。'
        '在逐条判明"是产品退步还是断言过期"之前塞进门禁，只会诱使人改断言凑绿。'
        '修复与纳管登记为 07-next-steps 的 L30b。'),
}
# 提交包内的"同一份文档两个摆放位置"对：评委既可能看包根也可能翻佐证材料目录，
# 内容不一致就是对外自相矛盾。本轮实测抓到 `00-…` 镜像**在本次改动之前就已经落后**
# （根级文档改过、镜像没跟），而这一对不在第 10 项比对面内（第 10 项只管 05-源代码全文 子树）
# ⇒ 与 `.github` 同一形态的洞，补成 BLOCK。
DOC_MIRROR_PAIRS = (
    (os.path.join('交付物', '提交包', '可运行链接与源码说明.md'),
     os.path.join('交付物', '提交包', '工程佐证材料', '00-可运行链接与源码说明.md')),
)


def check_doc_mirrors(root, report):
    n = 0
    for a_rel, b_rel in DOC_MIRROR_PAIRS:
        a, b = os.path.join(root, a_rel), os.path.join(root, b_rel)
        if not (os.path.isfile(a) and os.path.isfile(b)):
            continue                      # 缺任一份属"交付包结构"问题，由别处判
        n += 1
        if sha256(a) != sha256(b):
            report.append(('BLOCK', '提交包文档与镜像副本不一致：%s ≠ %s'
                           % (os.path.basename(a_rel), os.path.basename(b_rel))))
    return n


def sha256(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def rel_files(root, rel_dir):
    """列出一棵子树里的文件（相对 rel_dir），不存在则返回空集。"""
    base = os.path.join(root, rel_dir)
    out = {}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for f in filenames:
            full = os.path.join(dirpath, f)
            out[os.path.relpath(full, base).replace('\\', '/')] = full
    return out


def check_github(root, report):
    a = rel_files(root, '.github')
    b = rel_files(root, os.path.join(MIRROR_SUBPATH, '.github'))
    for key in sorted(set(a) | set(b)):
        if key not in b:
            report.append(('BLOCK', f'.github 镜像缺文件：05-源代码全文/.github/{key}'))
        elif key not in a:
            report.append(('BLOCK', f'.github 镜像多文件（根上不存在＝宣称不存在的 posture）：{key}'))
        elif sha256(a[key]) != sha256(b[key]):
            report.append(('BLOCK', f'.github 镜像内容漂移：{key}'))
    return len(a), len(b)


def check_extra_tracked(root, report):
    n = 0
    for rel in EXTRA_TRACKED:
        paths = [os.path.join(root, 'iCAN大学生创新创业大赛', rel),
                 os.path.join(root, MIRROR_SUBPATH, rel),
                 os.path.join(root, FREE_SUBPATH, rel)]
        missing = [p for p in paths if not os.path.isfile(p)]
        if len(missing) == len(paths):
            continue                      # 三处都没有 = 该文件不存在，不报
        if missing:
            for p in missing:
                report.append(('BLOCK', f'三副本缺文件：{os.path.relpath(p, root).replace(chr(92), "/")}'))
            n += 1
            continue
        digests = {sha256(p) for p in paths}
        if len(digests) > 1:
            report.append(('BLOCK', f'三副本内容漂移：{rel}（{sorted(d[:12] for d in digests)}）'))
        n += 1
    return n


def check_dev_only(root, report):
    for rel in DEV_ONLY_HINTS:
        p = os.path.join(root, 'iCAN大学生创新创业大赛', rel)
        if os.path.isfile(p) and not os.path.isfile(os.path.join(root, MIRROR_SUBPATH, rel)):
            report.append(('WARN', f'dev 独有文件未进交付副本（确认是交付物还是调试残片）：{rel}'))
    for rel, why in DEV_ONLY_ACK.items():
        p = os.path.join(root, 'iCAN大学生创新创业大赛', rel)
        if not (os.path.isfile(p) and not os.path.isfile(os.path.join(root, MIRROR_SUBPATH, rel))):
            continue
        # 登记也要有牙：理由太短＝敷衍，照样提示（对齐门禁第 19 项的 ≥12 字理由口径，本处取 40）
        if len(why) < 40:
            report.append(('WARN', f'dev-only 测试资产登记理由不足（{len(why)} 字 <40）：{rel}'))
        else:
            print(f'   ↳ 已登记 dev-only 测试资产（非残片，纳管前挂账）：{rel}')


def main(argv=None):
    ap = argparse.ArgumentParser(description='交付副本漂移检查：.github 镜像 ⇄ 仓库根、名单外根级脚本三副本一致性')
    ap.add_argument('--root', default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help='工作区根（含 iCAN大学生创新创业大赛 / 交付物 / deploy-free）')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    if not os.path.isdir(os.path.join(root, MIRROR_SUBPATH)):
        print('FAIL：找不到交付副本目录（%s）——本检查只在完整工作区里有意义' % MIRROR_SUBPATH)
        return 1
    report = []
    n_root, n_mirror = check_github(root, report)
    n_extra = check_extra_tracked(root, report)
    n_pairs = check_doc_mirrors(root, report)
    check_dev_only(root, report)
    blocks = [m for lvl, m in report if lvl == 'BLOCK']
    warns = [m for lvl, m in report if lvl == 'WARN']
    for lvl, msg in report:
        print(('  [%s] %s' % (lvl, msg)))
    print('交付漂移检查：%d BLOCK / %d WARN ｜ 比对 .github 根 %d 文件 ⇄ 镜像 %d 文件，'
          '名单外根级脚本 %d 个，提交包文档镜像对 %d 组'
          % (len(blocks), len(warns), n_root, n_mirror, n_extra, n_pairs))
    if not blocks:
        print('✅ 交付副本与仓库根一致（.github + 名单外根级脚本）')
    return 1 if blocks else 0


if __name__ == '__main__':
    sys.exit(main())
