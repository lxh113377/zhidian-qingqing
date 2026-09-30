#!/usr/bin/env python3
# 静态镜像「重传即校验」通道（对标第 13 轮，用户 2026-09-25 指令：
# 「Pages / CloudBase 镜像重传无人接管 —— 建议并入 9/25 批次并加一条"重传即校验"的通道，
#   否则每次改前端都会重新落后」）。
#
# 现场证据（本轮实测）：交付副本 演示快照集/ 生成于 2026-09-24 22:06，而 Pages 仓
# lxh113377/ican-demo 最后一次提交是 2026-09-16 02:30 ⇒ 落后 8 天、24/25 页签名不一致。
# 更要命的是**仓库里没有任何推送通道**：`.github/workflows/` 全量 grep `ican-demo`
# 只命中 deploy-pa.yml 的一次探活；`api.github.com` / `git/refs` 全仓零命中。
# 旧提交说明写着「自动部署演示快照集（GitHub Actions）」，而那个 workflow 已不在仓内
# ⇒ 文档与提交说明双双宣称"CI 自动"，实际是手工推的（走查第 11 轮 P1，见 delivery-chain §10.3）。
#
# 本脚本就是把那条断掉的通道补回来，并把「重传」和「校验」焊在同一次调用里：
# 推完轮询线上签名，直到一致或超时——不拿到 MIRROR OK 就不算重传成功。
#
# 用法：
#   python push_snapshot_mirror.py                      # 推 Pages 并复验
#   python push_snapshot_mirror.py --dry-run            # 只看会改哪些文件，不写不外发
#   python push_snapshot_mirror.py --verify-only        # 只复验（CloudBase 等手工重传后也用这条）
#
# 退出码（表同 check_mirror_freshness.py；两侧合并走 merge_rc()，
#            **按"最该动手的那条"取，不按数值取大**——第 22/23 轮两次踩同一类）：
#   0 = 已重传且线上签名一致（**含 rc=3 那种"只剩当天日期差"**——内容一致就算重传成功，
#       否则会对着一个已经好的镜像白轮询到 --wait 超时）
#   1 = 推完了但线上签名仍不一致（CDN 未刷新，或归一化漏了某种动态字段）
#   2 = 不可达（网络/站点问题，判不了内容，别当成"没推上去"）
#   4 = 校验器报用法/输入错误（缺地址、快照目录不存在、目录里没有 .html）——
#       单独一档是因为把它混进 1 会让"命令写错了"看起来像"部署延迟"，越重试越偏
import argparse
import os
import re
import shutil
import subprocess
import sys
# 中文 Windows 控制台默认 GBK：打印 ✅/🔴/⚠️ 会抛 UnicodeEncodeError，
# 于是**通过的检查反而以 rc=1 崩掉**（走查第 23 轮 F6 在姊妹工具上实测）。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (OSError, ValueError):
            pass
import time

MIRROR_URL = 'https://lxh113377.github.io/ican-demo'
MIRROR_REPO = 'https://github.com/lxh113377/ican-demo.git'
# CloudBase 静态托管：CLI 已实测（3.8.3 已登录，体验版到期 2027-03-14）。
# 注意这个桶是**共享**的——实测根目录里还混着另一个项目的 17 个文件，
# 所以只允许"上传本地产物"，绝不允许按"远端有本地无即删"去整目录镜像。
CB_ENV = 'qwer-d4gf2r76o8829463b'
CB_URL = 'https://qwer-d4gf2r76o8829463b-1458054906.tcloudbaseapp.com'
HERE = os.path.dirname(os.path.abspath(__file__))


def run(argv, cwd=None, check=True):
    r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding='utf-8')
    if check and r.returncode != 0:
        sys.stdout.write((r.stdout or '') + (r.stderr or ''))
        raise SystemExit('命令失败（rc=%d）：%s' % (r.returncode, ' '.join(argv)))
    return r


def collect(snap_dir):
    if not os.path.isdir(snap_dir):
        raise SystemExit('找不到本地快照目录 %s（先跑 build_snapshot_set.py）' % snap_dir)
    pages = sorted(f for f in os.listdir(snap_dir) if f.endswith('.html'))
    if not pages:
        raise SystemExit('本地快照目录为空，拒绝用它去覆盖线上（覆盖成空站是破坏性动作）')
    return pages


def sync_into(pages, snap_dir, work, dry):
    """把本地产物拷进克隆目录，返回 (新增/改动, 删除) 文件名。"""
    changed, removed = [], []
    for name in pages:
        dst = os.path.join(work, name)
        src = os.path.join(snap_dir, name)
        same = os.path.isfile(dst) and open(dst, 'rb').read() == open(src, 'rb').read()
        if not same:
            changed.append(name)
            if not dry:
                shutil.copyfile(src, dst)
    have = {f for f in os.listdir(work) if f.endswith('.html')}
    # 只删"本地产物命名形态"的页面（NN_业务页.html / index.html）。
    # 实测原因：CloudBase 那个静态托管根目录里混着**另一个项目**的 17 个文件
    # （cloud-admin/、js/three-scene.js、vendor/gsap.min.js、__auth/* 等），
    # 一旦按"远端有、本地无即删"整目录镜像，就会把别人在做的东西抹掉。
    ours = re.compile(r'^(?:\d{2}_[^/\\]+|index)\.html$')
    for extra in sorted(have - set(pages)):
        if not ours.match(extra):
            print('   · 保留非快照形态文件（不是本项目的产物，不删）：%s' % extra)
            continue
        removed.append(extra)
        if not dry:
            os.remove(os.path.join(work, extra))
    return changed, removed


def verify(script, snap_dir, timeout, url=MIRROR_URL):
    """跑同目录的 check_mirror_freshness.py，透传其退出码。

    五种：0=一致 / 1=仍漂移（要重传）/ 2=不可达（判不了内容）/ 3=STALE-DATE（只差当天日期）/
    4=用法或输入错（命令写错了，**不是**线上漂移）。语义表以 `check_mirror_freshness.py`
    文件头为唯一真相源，这里只列形状不复述处置办法（复述过一次结果漏了 3/4 两档，变假描述）。
    """
    if not os.path.isfile(script):
        print('🔴 校验器不存在：%s —— 这是**工具坏了**，不是线上状态' % script)
        return 5
    r = subprocess.run([sys.executable, script, url, '--snap-dir', snap_dir],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    for ln in [l for l in (r.stdout or '').splitlines() if 'MIRROR' in l]:
        print('   ', ln)
    return classify_rc(r.returncode, (r.stdout or '') + (r.stderr or ''))


def push_cloudbase(snap_dir, dry):
    """CloudBase 静态托管上传（只做"把本地产物推上去"，删远端交给人工）。"""
    tcb = shutil.which('tcb') or shutil.which('tcb.cmd')
    if not tcb:
        print('⚠️ 找不到 tcb CLI（CloudBase 侧未上传；Pages 侧不受影响）。'
              '装好后手工执行：tcb hosting deploy 演示快照集 -e %s' % CB_ENV)
        return 2
    if dry:
        print('--dry-run：跳过 tcb hosting deploy')
        return 0
    r = subprocess.run([tcb, 'hosting', 'deploy', snap_dir, '-e', CB_ENV],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    tail = [ln for ln in (r.stdout or '').splitlines() if ln.strip()][-3:]
    for ln in tail:
        print('   ', ln.strip()[:110])
    if r.returncode != 0:
        print('🔴 tcb 上传失败 rc=%d（提示付费/需登录时**停手**，不改环境、不换桶）' % r.returncode)
    return r.returncode


# 退出码合并：类别码不是严重度。取 max 会让「2 = 换网络」盖掉「1 = 要重传」，
# 自动化按 2 处置就永远不去重传真漂移的那一侧（对标第 22 轮 F11）。
# 优先级写在 merge_rc() 里：1 要重传 > 4 命令写错 > 2 不可达 > 表外非 0 码 > 3 只差当天日期 > 0 一致。


def classify_rc(rc, blob):
    """把"校验器自己的故障"和"校验器给出的结论"分开——第 23 轮 F7：
    校验器不存在 / 崩溃时，rc 恰好撞上结论表里的 1 或 2，于是把**工具坏了**
    报成"网络不可达"或"CDN 未刷新"，自动化就往不存在的问题上重试。

    5 = 校验器故障（不是线上状态，且**优先级高于 2**：工具坏了不能被说成"换网络"）。判据：退出码不在 0..4 表内，或 rc!=0 且输出里没有
    任何 MIRROR 结论行（真崩溃只有 traceback）。
    """
    if rc not in (0, 1, 2, 3, 4):
        return 5
    if rc and rc != 4 and 'MIRROR' not in blob:
        # 4 = 用法/输入错，检查器本来就不打 MIRROR 行，不能当成"工具崩了"
        return 5
    return rc


def merge_rc(codes):
    """合并多路退出码：**类别码不是严重度**，按"最该动手的那一条"取。

    顺序 1 要重传 > 4 命令写错 > 5 校验器故障 > 2 不可达 > 表外非 0 码 > 3 只差当天日期 > 0 一致。
    走查第 23 轮 F2b：上一版把表外码排在 3 之后 ⇒ `merge_rc([128, 3])` 返回 3，
    与自家注释「表外码取大、宁吵不静默」相反；本函数由 `--selftest` 逐条钉住。
    """
    for want in (1, 4, 5, 2):
        if want in codes:
            return want
    other = [c for c in codes if c not in (0, 3)]
    if other:
        return max(other)
    return 3 if 3 in codes else 0


def selftest_merge_rc():
    """merge_rc 的成对夹具：正例 + 反例，任何一条不符返回 1（`--selftest`）。"""
    cases = [((2, 1), 1), ((1, 2), 1), ((4, 2, 1), 1), ((2, 3), 2), ((128, 3), 128),
             ((3, 0), 3), ((0, 0), 0), ((), 0), ((2,), 2), ((1, 128), 1),
             ((5, 2), 5), ((1, 5), 1)]
    # classify_rc：结论表内的码原样透传；表外码与"崩溃但没结论行"必须抬成 5
    cases2 = [((1, 'x MIRROR DRIFT y'), 1), ((2, 'MIRROR UNREACHABLE'), 2),
              ((1, 'Traceback (most recent call last)\n'), 5), ((7, ''), 5),
              ((0, 'MIRROR OK'), 0), ((4, '缺少镜像地址'), 4)]
    bad = []
    for codes, want in cases:
        got = merge_rc(list(codes))
        flag = 'ok' if got == want else 'MISMATCH'
        print('  %-14s -> %-4s 期望 %-4s %s' % (str(codes), got, want, flag))
        if got != want:
            bad.append(codes)
    for (rc, blob), want in cases2:
        got = classify_rc(rc, blob)
        flag = 'ok' if got == want else 'MISMATCH'
        print('  classify_rc(%-2d, %-34r) -> %-2s 期望 %-2s %s' % (rc, blob[:24], got, want, flag))
        if got != want:
            bad.append(('classify', rc, blob))
    print('merge_rc/classify_rc 自检：%d + %d 组，%s'
          % (len(cases), len(cases2), '全对' if not bad else '失败 ' + str(bad)))
    return 1 if bad else 0


def preflight():
    """重传前置闸（第 17 轮 M43）：先把本地等价验证链跑一遍，没证据就不许外发。

    根因：本脚本把**当前快照目录**原样推到 Pages/CloudBase，而快照是 build 产物。
    源码改了、快照没重建、门禁没跑就推 ⇒ 线上变成"看起来交付了"的假交付
    （第 16 轮实测：降级执行会伪装成通过）。第 16 轮做出 `verify_local.py` 却没接进
    任何阻断链 = 半成品（在册教训"工具没进阻断链=半成品"），本函数就是那根接线。

    返回 0 才放行。**"没跑成"(rc=2) 同样拦** —— 拿不到证据时放行等于没有闸；
    连 verify_local.py 都不在（三副本没同步）也拦，并说清缺的是哪个文件。
    """
    script = os.path.join(HERE, 'verify_local.py')
    if not os.path.isfile(script):
        print(f'🔴 前置闸：{script} 不存在 —— 本地验证链丢失，拒绝重传')
        return 4
    print('— 前置闸：verify_local.py --fast --skip-net（静态段门禁 + 判据自测 + 版本/环境）')
    r = subprocess.run([sys.executable, script, '--fast', '--skip-net'],
                       cwd=HERE, capture_output=True, text=True)
    out = ((r.stdout or '') + (r.stderr or '')).strip().splitlines()
    for ln in out[-8:]:
        print('   ' + ln)
    if r.returncode != 0:
        tag = {1: '有阶段判红', 2: '有阶段没跑成（无证据）', 4: '前置命令写错'}.get(
            r.returncode, f'退出码 {r.returncode}')
        print(f'🔴 前置闸未通过（{tag}）——拒绝重传；先修红因再推，禁止 --dry-run 之外的绕行')
    return r.returncode


def main(argv=None):
    ap = argparse.ArgumentParser(description='演示快照集 → Pages / CloudBase 镜像重传，并在同一次调用里复验')
    ap.add_argument('--snap-dir', default=os.path.join(HERE, '演示快照集'))
    ap.add_argument('--work', default='', help='克隆目录（默认 %TEMP% 下临时目录）')
    ap.add_argument('--target', choices=('pages', 'cloudbase', 'both'), default='both',
                    help='重传目标；默认 both（CloudBase 缺 tcb 或需登录时只影响该侧，Pages 照常）')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--verify-only', action='store_true')
    ap.add_argument('--wait', type=int, default=240, help='推完等 CDN/Pages 刷新的秒数上限')
    ap.add_argument('--selftest', action='store_true',
                    help='只跑 merge_rc 成对夹具（不访问网络、不外发），全对返回 0')
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest_merge_rc()
    script = os.path.join(HERE, 'check_mirror_freshness.py')
    targets = {'pages': [(MIRROR_URL, 'Pages')], 'cloudbase': [(CB_URL, 'CloudBase')],
               'both': [(MIRROR_URL, 'Pages'), (CB_URL, 'CloudBase')]}[args.target]
    if args.verify_only:
        codes = []
        for url, tag in targets:
            print('— %s' % tag)
            codes.append(verify(script, args.snap_dir, args.wait, url))
        return merge_rc(codes)

    pages = collect(args.snap_dir)
    if not args.dry_run:                      # --dry-run 不外发，无需前置闸
        _pre = preflight()
        if _pre:
            return _pre
    overall = 0
    if args.target in ('pages', 'both'):
        overall = push_pages(pages, args, script)
    if args.target in ('cloudbase', 'both'):
        print('— CloudBase（tcb hosting deploy）')
        crc = push_cloudbase(args.snap_dir, args.dry_run)
        if crc == 0 and not args.dry_run:
            crc = poll_verify(script, args.snap_dir, args.wait, CB_URL)
        elif crc == 2:
            print('   ⚠️ CloudBase 侧未完成（见上），Pages 侧结论不受影响')
        overall = merge_rc([overall, crc])
    return overall


def poll_verify(script, snap_dir, wait, url):
    deadline = time.time() + wait
    tried = 0
    while True:
        tried += 1
        rc = verify(script, snap_dir, wait, url)
        if rc == 0:
            print('✅ %s 重传已校验通过（第 %d 次探测）' % (url, tried))
            return 0
        if rc == 3:
            # 3 = STALE-DATE：业务内容签名已一致，只差当天日期（`date.today()` 渲染）
            # ⇒ 重传本身成功了，别再白轮询到超时；日期差要么当天重传，要么接受
            print('✅ %s 内容已一致（仅日期差，rc=3）——重传有效，第 %d 次探测' % (url, tried))
            return 0
        if rc == 2:
            print('⚠️ 不可达，无法判定；恢复后复跑：python %s --verify-only' % os.path.basename(script))
            return 2
        if rc == 4:
            # 4 = 校验器**用法/输入错误**（缺地址、快照目录不存在、目录里没有 .html）。
            # 不特殊处理会一路轮询到超时，然后把"命令写错了"报成"CDN 未刷新"——
            # 一个把配置缺陷伪装成部署延迟的诊断，比红更糟。
            print('🔴 校验器报用法/输入错误（rc=4），不是线上漂移：见上面 FAIL 行，修命令再重跑')
            return 4
        if time.time() >= deadline:
            break
        time.sleep(20)
    print('🔴 已推送但线上签名仍不一致（超时）——要么 CDN 未刷新，要么归一化漏了动态字段')
    return 1


def push_pages(pages, args, script):
    work = args.work or os.path.join(os.environ.get('TEMP', '/tmp'), 'ican-demo-push-work')
    if not os.path.isdir(os.path.join(work, '.git')):
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(os.path.dirname(work), exist_ok=True)
        run(['git', 'clone', MIRROR_REPO, work])
    else:
        run(['git', 'fetch', 'origin'], cwd=work)
        run(['git', 'reset', '--hard', 'origin/main'], cwd=work)
    before = run(['git', 'rev-parse', 'HEAD'], cwd=work).stdout.strip()

    changed, removed = sync_into(pages, args.snap_dir, work, args.dry_run)
    print('— Pages：克隆基线 %s' % before[:12])
    print('   %d 页待更新 / %d 页删除' % (len(changed), len(removed)))
    for n in changed[:6]:
        print('   · 改 %s' % n)
    for n in removed[:6]:
        print('   · 删 %s' % n)
    if args.dry_run:
        print('   --dry-run：未写入、未推送')
        return 0
    if not changed and not removed:
        print('   仓库文件已与本地逐字节一致，跳过推送（仓库一致 ≠ 站点已更新，仍需复验）')

    run(['git', 'add', '-A'], cwd=work)
    src_head = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=HERE,
                              capture_output=True, text=True).stdout.strip() or 'unknown'
    msg = ('chore(demo): 重传演示快照集（%d 页，源仓 %s）\n\n'
           '由 push_snapshot_mirror.py 生成；推后按 check_mirror_freshness.py 逐页签名复验。'
           % (len(pages), src_head))
    r = subprocess.run(['git', 'commit', '-m', msg], cwd=work, capture_output=True, text=True,
                       encoding='utf-8')
    if r.returncode != 0 and 'nothing to commit' not in (r.stdout or ''):
        sys.stdout.write((r.stdout or '') + (r.stderr or ''))
        return 1
    if 'nothing to commit' in (r.stdout or ''):
        print('   （无新提交，直接复验）')
    else:
        run(['git', 'push', 'origin', 'HEAD:main'], cwd=work)
        print('   已推送 → 轮询线上（Pages 构建需要时间，上限 %ds）' % args.wait)

    rc = poll_verify(script, args.snap_dir, args.wait, MIRROR_URL)
    if rc == 1:
        print('   回滚用克隆基线：git -C %s push --force-with-lease origin %s:main（需人工确认）'
              % (work, before[:12]))
    return rc


if __name__ == '__main__':
    sys.exit(main())
