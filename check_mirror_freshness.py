#!/usr/bin/env python3
# 静态镜像「重传即校验」检查器（对标第 13 轮，用户 2026-09-25 指令：给 Pages/CloudBase 加一条
# "重传即校验"的通道，否则每次改前端都会重新落后）。
#
# 为什么不能逐字节比：演示快照每页含一次性 CSRF token 与生成时间，重建后字节必变，
# 归一化后业务内容才是不变的量（这条已在 delivery-chain §3 记过，但当时只写了结论没写工具）。
# 所以比的是**签名**：剥掉动态字段后的 sha256 前 16 位，逐页对照本地产物与线上页面。
#
# 为什么不是门禁 BLOCK 项：网络可达性既受本机 GFW/代理影响也受镜像方限流影响，
# 拿它当红线只会训练人点重试（与第 18 项"毫秒不进红线"同一判断）。
# 定位 = 重传之后立刻自证的一等命令，以及 CI 部署后的非阻断提示。
#
# 退出码：0 = 全部一致；1 = 有漂移/缺页（需要重传）；2 = 不可达（网络或站点问题，不能判内容）；
#         3 = 只剩「页面里的日期是重建当天之前的」这一类差异（STALE-DATE，内容没坏、线上确实旧一天）；
#         4 = **用法/输入错误**（缺镜像地址、找不到本地快照目录、目录里没有 .html）。
#         五种必须区分开：1 要重传、2 要换网络、3 只要当天重建一次、4 是命令本身写错了。
#         尤其 4 不能并入 1 —— 自动化按"1 = 重传"处置时会对着一个配错的命令无限重传。
#         `--selftest` 与 `--list` 不走网络，沿用同一张表（实测 selftest 全中 → rc=0）。
import argparse
import hashlib
import os
import re
import sys
# 中文 Windows 控制台默认 GBK：打印 ✅/🔴/⚠️ 会抛 UnicodeEncodeError，
# 于是**通过的检查反而以 rc=1 崩掉**（走查第 23 轮 F6 在姊妹工具上实测）。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (OSError, ValueError):
            pass
import urllib.error
import urllib.request

# 全角冒号「：」= UTF-8 三字节 \xef\xbc\x9a。**bytes 模式里不能写成字符类 `[：:]`**——
# 那会被编译成"单字节集合"，于是"生成时间："永远匹配不上，整条归一化规则变死码。
# 走查第 13 轮的实测教训：分钟级那条就是这么"看着有、其实一条没中"，
# 而验证脚本用了同样的写法 ⇒ 自己骗过了自己。必须写成三字节交替。
_FW_COLON = rb'(?:\xef\xbc\x9a|:)'
_TS_BODY = rb'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?'

DYN_PATTERNS = [
    # 表单里的 CSRF 域：只掏 value，保留字段名（字段名本身是结构信息）
    (re.compile(rb'(name="csrf_token"[^>]*?value=")[^"]*(")'), rb'\1TOKEN\2'),
    (re.compile(rb'(<input[^>]*?name="[^"]*token[^"]*?"[^>]*?value=")[^"]*(")'), rb'\1TOKEN\2'),
    # 一次性 nonce：**按子串认**，别写死 name="nonce"。走查第 13 轮实测漏网：
    # 平台重置页用的是 `name="reset_nonce"`，写死的版本让它每次重建都算漂移。
    (re.compile(rb'(name="[^"]*nonce[^"]*"[^>]*?value=")[^"]*(")', re.I), rb'\1NONCE\2'),
    # meta 形态的同一枚 token（`<meta name="csrf-token" content="…">`）：
    # 首轮实测就是它漏网——只写 input 的 value= 判据时，页面每次重建都算"漂移"（假报）。
    (re.compile(rb'(<meta\b[^>]*?name="csrf[-_]token"[^>]*?content=")[^"]*(")'), rb'\1TOKEN\2'),
    # 第三种摆放形态 `data-csrf-token="…"`（走查第 12 轮实测抓到）。
    # **按属性名认**，不靠形状：形状兜底要求首段 ≥20 字符，一旦哪天换成更短的签名格式就会漏。
    (re.compile(rb'(data-[a-z_-]*token[a-z_-]*=")[^"]*(")', re.I), rb'\1TOKEN\2'),
    # 「生成时间」**只按标签锚定**，秒级与分钟级两种精度一起吃掉。
    # 走查第 13 轮 F3 实测：原先另有一条**不锚标签**的 `YYYY-MM-DD HH:MM:SS` 通配规则，
    # 结果连业务时间戳也一起抹掉（往考核明细里追加 09:12:59 与 09:12:58 两版，签名一模一样）
    # ——那是**假一致**，比假漂移更危险。所以宁可漏一条动态字段（会红、会被发现），
    # 也不能多抹一条业务数据（会绿、永远没人知道）。
    # bytes 模式里**不能**把全角标点写进字符类：`[：:]` 编码成 `[\xef\xbc\x9a:]` 后是
    # "按单字节匹配 ef/bc/9a/: 之一"，而 `：` 在文本里是三字节的整体 ⇒ 这条规则永远不命中，
    # 且表面看不出问题（走查第 13 轮的分钟级归一化就是这么"写对了但一直是死码"）。
    # 正确写法是把整体放进交替，且**直接用上面那个常量**（走查第 17 轮 F8：本条规则原先在
    # 这里内联重拼了一份同义交替，于是 `_FW_COLON` 变成**从没被用过的死常量**——
    # 一条"防止写错"的对策自己却没人调用，改它不会有任何测试变红）。
    (re.compile(rb'(' + '生成时间'.encode('utf-8') + _FW_COLON + rb'\s*)' + _TS_BODY),
     rb'\1<TS>'),
]


def normalize(raw):
    """剥掉每次重建必然变化的字段，留下可比的业务内容。"""
    body = raw.replace(b'\r\n', b'\n')
    for pat, rep in DYN_PATTERNS:
        body = pat.sub(rep, body)
    return body


# ── 「日期差」单独分类，不塞进归一化 ────────────────────────────────
# 有些页含 `date.today()` 渲染出来的当天日期。**几页几处不写死在这里**——
# 走查第 18 轮 F9 实测：原注释抄的"25 页里 5 页含当天日期（01 六处、14 三处、18 一处…）"
# 其实**一个都没错**，错的是只抄了数字没抄口径：当场跑 `--date-report` 会同时给三种口径
# （A 只数 `生成时间：<今天>`、B 数所有 ISO 今天串含数据区、C 数 `今天年月日` 标题），
# 原注释那组数正是**口径 B**（25 页时 5 页、01:6 / 07:1 / 14:3 / 18:1 / 24:1），
# 而按口径 A 只有 3 页 —— 同一份产物、两个都对、结论差一倍。
# ⇒ 规矩：**引用当天日期分布必须写口径**，当场数以 `python check_mirror_freshness.py --date-report` 为准。
# 把日期也归一化掉=**为了不再假红而主动变瞎**：真有业务日期改动时再也测不出来。# 把它们也归一化掉=**为了不再假红而主动变瞎**：真有业务日期改动时再也测不出来。
# 所以主签名照旧敏感，只额外算一个"日期无关"签名用于**归类**：
# 报 DRIFT 时告诉人"这几页只差日期"，而不是替人下结论说没问题。
_DATE_PATTERNS = [
    re.compile(('\\d{4}' + '年' + '\\d{1,2}' + '月' + '\\d{1,2}' + '日').encode('utf-8')),
    re.compile(rb'\d{4}-\d{2}-\d{2}'),
]


def dateless(raw):
    body = normalize(raw)
    for pat in _DATE_PATTERNS:
        body = pat.sub(b'<D>', body)
    return hashlib.sha256(body).hexdigest()[:16]


def signature(raw):
    return hashlib.sha256(normalize(raw)).hexdigest()[:16]


def local_pages(snap_dir):
    if not os.path.isdir(snap_dir):
        return None
    return sorted(f for f in os.listdir(snap_dir) if f.endswith('.html'))


def fetch(url, timeout):
    req = urllib.request.Request(url, headers={'User-Agent': 'mirror-freshness-check/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def quote_url(base, name):
    from urllib.parse import quote
    return base.rstrip('/') + '/' + quote(name)


def check_mirror(base, pages, snap_dir, timeout):
    """返回 (状态, 命中数, 明细)。状态 ∈ OK / DRIFT / UNREACHABLE。"""
    drift, dateonly, miss, ok = [], [], [], 0
    unreachable = None
    for name in pages:
        url = quote_url(base, name)
        try:
            raw = fetch(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                miss.append(name)
            else:
                unreachable = '%s → HTTP %d' % (name, exc.code)
                break
            continue
        except Exception as exc:                       # 网络层异常：整站判不可达，不猜内容
            unreachable = '%s: %s: %s' % (name, type(exc).__name__, exc)
            break
        with open(os.path.join(snap_dir, name), 'rb') as fh:
            local = fh.read()
        if signature(raw) == signature(local):
            ok += 1
        elif dateless(raw) == dateless(local):
            # 只差"当天日期"：重建与线上不同日，属**待重传**而非内容不一致——单独归类，别混进漂移
            dateonly.append(name)
        else:
            drift.append('%s（线上 %s ≠ 本地 %s）' % (name, signature(raw), signature(local)))
    if unreachable is not None:
        return 'UNREACHABLE', ok, [unreachable]
    detail = []
    if dateonly:
        detail.append('仅日期差 %d 页（`date.today()` 渲染，跨天重建所致，内容一致）：%s'
                      % (len(dateonly), '、'.join(dateonly[:6])))
    if drift or miss:
        if miss:
            detail.append('缺页 %d：%s' % (len(miss), '、'.join(miss[:6])))
        return 'DRIFT', ok, ['内容漂移 %d：%s' % (len(drift), '、'.join(drift[:6]))] + detail
    if dateonly:
        return 'STALE-DATE', ok, detail
    return 'OK', ok, detail


FIXTURE = (
    # token 段长一律照**真实产物**取（Flask 签名是 64.6.33 形态）：
    # 自检第一版把首段写成 4 个字符，结果"按形状兜底"那条规则被误判成死码——
    # 夹具不真实，判据自检就会假红（与"文档抄错输出"同一类错误）。
    '<html><head><meta name="csrf-token" content="IjY2ZGYxMGJlNTBkYzQ2Zjg5MzBjZDZiZDQ2NWYwZDk0NTNhZjBhYmMi.aqmOsw.tDCaQrnzI-KkXN2D0hZolzcO03Q">'
    '<script src="/static/js/app.js"></script></head><body>'
    '<form><input type="hidden" name="csrf_token" value="IjY2ZGYxMGJlNTBkYzQ2Zjg5MzBjZDZiZDQ2NWYwZDk0NTNhZjBhYmMi.aqmOsw.tDCaQrnzI-KkXN2D0hZolzcO03Q">'
    '<input type="hidden" name="reset_nonce" value="HdPKkYUhNgfXnjSGrPmdOnEe"></form>'
    '<button data-csrf-token="IjY2ZGYxMGJlNTBkYzQ2Zjg5MzBjZDZiZDQ2NWYwZDk0NTNhZjBhYmMi.aqmOsw.tDCaQrnzI-KkXN2D0hZolzcO03Q">确认</button>'
    '<p class="note">生成时间：2026-09-25 03:48（规则版·演示）</p>'
    '<p>生成时间：2026-09-24 22:06:58</p>'
    # 业务数据：含秒级的考核时刻，**必须**参与签名（被抹掉就是假一致）
    '<div class="cord-detail mt-4">2026-08-21 09:12:59</div>'
    '<td>芝士奶盖 3 杯</td></body></html>').encode('utf-8')


def selftest():
    """归一化自检——专治"规则写了却一条没匹配上"（走查第 13 轮实证：
    bytes 模式里 `生成时间[：:]` 被编译成单字节集合，分钟级规则永远不命中，
    而验证脚本用了同样的写法，于是假漂移被判成"已修好"）。"""
    fails = []
    for idx, (pat, _rep) in enumerate(DYN_PATTERNS):
        if not pat.search(FIXTURE):
            fails.append('第 %d 条归一化规则在夹具上零命中（可能是死码，如全角标点写进了字符类）' % idx)
    mut = FIXTURE
    mut = re.sub(rb'content="[^"]*"', b'content="ZZZZ.ZZZZZZZZ.ZZZZZZZZZZZZZZZZZZ"', mut)
    mut = re.sub(rb'name="csrf_token" value="[^"]*"', b'name="csrf_token" value="XXXX.YYYYY.YYYYYYYYYYYYYYYYYYY"', mut)
    mut = re.sub(rb'name="reset_nonce" value="[^"]*"', b'name="reset_nonce" value="YYYYYYYYYYYYYYYYYYYYYYYY"', mut)
    mut = re.sub(rb'data-csrf-token="[^"]*"', b'data-csrf-token="QQQQ.QQQQQQQQ.QQQQQQQQQQQQQQQQQQ"', mut)
    mut = mut.replace('2026-09-25 03:48'.encode(), '2030-01-01 00:00'.encode())
    mut = mut.replace('2026-09-24 22:06:58'.encode(), '2030-01-02 10:20:30'.encode())
    if signature(FIXTURE) != signature(mut):
        fails.append('动态字段整批替换后签名仍变 ⇒ 归一化漏字段')
    if signature(FIXTURE) == signature(FIXTURE.replace('09:12:59'.encode(), '09:12:58'.encode())):
        fails.append('改一个**业务**秒级时间戳签名不变 ⇒ 归一化过度，把真实改动也抹掉了（假一致）')
    if signature(FIXTURE) == signature(FIXTURE.replace('芝士奶盖'.encode(), '椰云拿铁'.encode())):
        fails.append('改业务品名签名不变 ⇒ 判据无牙')
    if dateless(FIXTURE) != dateless(FIXTURE.replace('2026-08-21'.encode(), '2026-08-22'.encode())):
        fails.append('改日期后"日期无关签名"也变了 ⇒ 分类签名没起作用（跨天重建会被误判成内容漂移）')
    if signature(FIXTURE) == signature(FIXTURE.replace('2026-08-21'.encode(), '2026-08-22'.encode())):
        fails.append('主签名对日期改动无感 ⇒ 归一化过度（宁可假红也不能假绿）')
    for line in fails:
        print('  🔴 %s' % line)
    print('归一化自检：%d 条规则全部活、%s' % (len(DYN_PATTERNS), '通过' if not fails else '不通过'))
    return 1 if fails else 0


def date_report(snap_dir):
    """逐页数「当天日期落在几页几处」，并把**口径本身**写进输出。

    为什么要有这条（走查第 18 轮 F9）：文件头注释原先直接抄"25 页里 5 页含当天日期
    （01 六处、14 三处…）"，而同一份产物按 `生成时间：` 按口径 A 只数到 3 页，按 ISO 串全量数
    又是另一个值 —— **数字没绑定义时，两种说法都能"实测成立"**。所以这里一次跑三种口径，
    引用者必须说明用的是哪一条。不访问网络，退出码 0=有数据可报、4=目录都没有。
    """
    import datetime
    pages = local_pages(snap_dir)
    if pages is None:
        print('FAIL：找不到快照目录 %s（先跑 build_snapshot_set.py 重建）' % snap_dir)
        return 4
    today = datetime.date.today().strftime('%Y-%m-%d')
    ymd = re.compile(r'\d{4}' + '年' + r'\d{1,2}' + '月' + r'\d{1,2}' + '日')
    n_gen = n_iso = n_ymd = 0
    hits = []
    for name in pages:
        body = open(os.path.join(snap_dir, name), 'rb').read().decode('utf-8', 'replace')
        g = body.count('生成时间：' + today)
        i = body.count(today)
        y = len(ymd.findall(body))
        if g or i or y:
            hits.append((name, g, i, y))
        n_gen += bool(g)
        n_iso += bool(i)
        n_ymd += bool(y)
    print('口径 A｜含「生成时间：<今天>」的页数：%d' % n_gen)
    print('口径 B｜含 ISO 今天串（含数据区日期）的页数：%d' % n_iso)
    print('口径 C｜含「今天年月日」标题形态的页数：%d' % n_ymd)
    print('总页数 %d；当天日期串 %s' % (len(pages), today))
    for name, g, i, y in hits:
        print('   %-34s 生成时间:%d  ISO:%d  年月日:%d' % (name, g, i, y))
    print('⚠️ 引用本报告时必须写清用的是哪一种口径；'
          '只抄数字不抄口径，就是本文件头那段注释被订正的原因。')
    return 0


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(
        description='静态镜像新鲜度：本地演示快照 ⇄ Pages/CloudBase 线上页面逐页签名比对')
    ap.add_argument('mirror', nargs='?', help='镜像根地址，如 https://lxh113377.github.io/ican-demo')
    ap.add_argument('--selftest', action='store_true',
                    help='只跑归一化自检（每条规则必须真的命中、业务改动必须改变签名），不访问网络')
    ap.add_argument('--snap-dir', default=os.path.join(here, '演示快照集'),
                    help='本地快照目录（默认取脚本同目录 演示快照集/）')
    ap.add_argument('--timeout', type=float, default=25.0)
    ap.add_argument('--list', action='store_true', help='只列本地页名与签名，不访问网络')
    ap.add_argument('--date-report', action='store_true',
                    help='只报本地快照里当天日期落在几页几处（三种口径各报一遍），不访问网络')

    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.date_report:
        return date_report(args.snap_dir)
    # `--list` **不需要镜像地址**（它只算本地签名）。此前缺地址的检查排在它前面，
    # 于是"报错提示让你改用 --list，而 --list 自己也报同一个错"——
    # 走查第 16 轮量退出码时实测到这条自相矛盾的分支。
    if not args.mirror and not args.list:
        print('FAIL：缺少镜像地址（或改用 --selftest / --list）')
        return 4                                    # 用法错误，不是内容漂移（见文件头退出码表）
    pages = local_pages(args.snap_dir)
    if pages is None:
        print('FAIL：找不到本地快照目录 %s（先跑 build_snapshot_set.py 重建）' % args.snap_dir)
        return 4
    if not pages:
        print('FAIL：本地快照目录里一个 .html 都没有，无页可比（不得当通过）')
        return 4
    if args.list:
        for name in pages:
            with open(os.path.join(args.snap_dir, name), 'rb') as fh:
                print('%s %s' % (signature(fh.read()), name))
        print('本地 %d 页' % len(pages))
        return 0

    status, ok, detail = check_mirror(args.mirror, pages, args.snap_dir, args.timeout)
    for line in detail:
        print('  · %s' % line)
    icon = {'OK': '✅', 'DRIFT': '🔴', 'UNREACHABLE': '⚠️', 'STALE-DATE': '🟡'}[status]
    print('%s MIRROR %s %s｜%d/%d 页签名一致' % (icon, status, args.mirror, ok, len(pages)))
    if status == 'DRIFT':
        print('   ↳ 重传命令见 references/delivery-chain.md §10.3；重传后复跑本行即可自证')
    if status == 'STALE-DATE':
        print('   ↳ 内容与签名一致、只差当天日期：要么当天重传，要么接受（**不是**判据坏了）。'
              '退出码 3 专门留给这种情况，方便 CI/脚本区分"要重传"与"线上有问题"')
    if status == 'UNREACHABLE':
        print('   ↳ 不可达只说明"这次没测到"，既不能判落后也不能判一致（一次超时分不出网络与宕机）')
    return {'OK': 0, 'DRIFT': 1, 'UNREACHABLE': 2, 'STALE-DATE': 3}[status]


if __name__ == '__main__':
    sys.exit(main())
