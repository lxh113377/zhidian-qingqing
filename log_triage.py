#!/usr/bin/env python
"""离线日志取证（对标第 19 轮 H38）—— 把「翻 5MB 轮转日志」变成一条命令。

为什么要有这个工具（全部当场实测，非推断）
──────────────────────────────────────────
改造前的归因链路是断的：
- `data/app.log` 有访问行（含耗时）与 `[慢请求]` / `[慢查询]` WARN；
- `data/ai_call_log.jsonl` 有每次 AI 出站的 `latency_ms`；
- 两份数据**没有任何公共键**，只能靠时间戳猜 —— 而 ai_trace 的 `ts` 改造前是
  **本地时间**、访问日志生产态是 **UTC**，相差 8 小时，按时间戳对表必然错。
第 19 轮把 `request_id` 写进两边（`src/ai_trace.py` + `src/logging_config.py`），
本工具负责消费：一次请求的「总耗时 = AI 等待 + 自身代码」当场拆开。

实测标定样本（2026-09-26）：`GET /admin/` 7249ms，同一 request 的两条 AI 留痕
4040 + 2982 = 7022ms ⇒ 97% 是 AI 等待，业务侧只有约 227ms（仍在 157ms 量级内）。

行格式前置（混合格式必须全枚举，R219d）：本工具识别三种行 ——
① JSON 行（生产态 JsonFormatter）② `HH:MM:SS [ INFO] rid logger: msg` 人类行（开发态）
③ 其它（werkzeug 自带行、异常 traceback 续行）一律计入 `unparsed`，**不静默丢弃**。

用法：
    python log_triage.py                      # 汇总（默认读 ./data）
    python log_triage.py --top 15             # 慢请求榜（含 AI 归因）
    python log_triage.py --rid 4a8e14e9       # 单次请求全链路
    python log_triage.py --json               # 机器可读
    python log_triage.py --selftest           # 夹具自证：每条解析腿都必须真有命中
退出码：0 有数据且无 5xx / 1 出现 5xx 或夹具自证不通过 / 2 没读到任何日志行（没测到 ≠ 通过）/ 4 用法错
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

from src import diag  # noqa: E402

# 开发态访问行：`11:58:53 [ INFO] 4a8e14e9 src.app: GET /admin/ 200 7248.3ms`
HUMAN_RE = re.compile(
    r'^(?P<ts>\d{2}:\d{2}:\d{2})\s+\[\s*(?P<level>[A-Z]+)\s*\]\s+'
    r'(?P<rid>[0-9a-f]{8}|-)\s+(?P<logger>[\w.]+):\s+(?P<msg>.*)$')
ACCESS_MSG_RE = re.compile(r'(?P<method>GET|POST|PUT|PATCH|DELETE)\s+'
                           r'(?P<path>\S+)\s+(?P<status>\d{3})\s+(?P<ms>[\d.]+)ms')
SLOW_REQ_RE = re.compile(r'\[慢请求\]\s+(?P<method>\w+)\s+(?P<path>\S+)\s+耗时\s+(?P<ms>\d+)ms')
SLOW_QUERY_RE = re.compile(r'\[慢查询\]\s+(?P<ms>\d+)ms')

# 三种行格式各自的命中计数：任一为 0 说明那条解析腿是死的（第 18 轮抓到过
# 「违例正则匹配不上 concise 输出 = 永久绿判据」，同一族）
_FORMATS = ('json', 'human', 'unparsed')


def log_files(data_dir):
    """按「旧 → 新」返回日志文件序列（含轮转件 app.log.3 … app.log）。"""
    base = os.path.join(data_dir, 'app.log')
    paths = [f'{base}.{n}' for n in range(3, 0, -1) if os.path.exists(f'{base}.{n}')]
    if os.path.exists(base):
        paths.append(base)
    return paths


def _read_lines(path):
    """容忍 BOM / CRLF / 非法字节：取证工具不得因为一行坏字节就整本读不下去。"""
    with open(path, 'rb') as fh:
        raw = fh.read()
    text = raw.decode('utf-8-sig', errors='replace')
    return text.replace('\r\n', '\n').split('\n')


def _pick(text, rid, level, logger):
    """把一行正文归成访问行 / 慢请求 WARN / 慢查询 WARN / None（忽略）。

    `status=None` 表示那是 WARN 行而不是访问行 —— 慢请求 WARN 与它的访问行是同一
    请求的两条记录，去重交给 `attribute()`，这里不做合并（合并会把两种形状的
    字段语义搅在一起，反而看不清丢了哪一条）。
    """
    acc = ACCESS_MSG_RE.search(text)
    if acc:
        return {'rid': rid, 'method': acc.group('method'), 'path': acc.group('path'),
                'status': int(acc.group('status')), 'ms': float(acc.group('ms')),
                'level': level, 'logger': logger, 'slow': False, 'kind': 'request'}
    slow = SLOW_REQ_RE.search(text)
    if slow:
        return {'rid': rid, 'method': slow.group('method'), 'path': slow.group('path'),
                'status': None, 'ms': float(slow.group('ms')),
                'level': level, 'logger': logger, 'slow': True, 'kind': 'slow_request'}
    q = SLOW_QUERY_RE.search(text)
    if q:
        return {'rid': rid, 'method': '', 'path': '(sql)', 'status': None,
                'ms': float(q.group('ms')), 'level': level, 'logger': logger,
                'slow': True, 'kind': 'query'}
    return None


def parse_access_lines(data_dir):
    """抽取访问行，返回 (rows, format_hits)。

    Args:
        data_dir: 含 app.log 的目录。

    Returns:
        tuple: rows 为 dict 列表，format_hits 为 json/human/unparsed 三种行各自的命中数。
    """
    rows = []
    hits = dict.fromkeys(_FORMATS, 0)
    for path in log_files(data_dir):
        for raw in _read_lines(path):
            text = raw.strip()
            if not text:
                continue
            if text.startswith('{'):
                try:
                    obj = json.loads(text)
                except ValueError:
                    hits['unparsed'] += 1
                    continue
                hits['json'] += 1
            else:
                mh = HUMAN_RE.match(text)
                if not mh:
                    hits['unparsed'] += 1
                    continue
                hits['human'] += 1
                obj = {'request_id': mh.group('rid'), 'level': mh.group('level').strip(),
                       'logger': mh.group('logger'), 'message': mh.group('msg')}
            row = _pick(obj.get('message', ''), obj.get('request_id') or '',
                        obj.get('level', ''), obj.get('logger', ''))
            if row:
                rows.append(row)
    return rows, hits


def parse_ai_rows(ai_log_path):
    """按 request_id 汇总 AI 出站耗时；无 id 的旧行单独归到空串键。"""
    by_rid = {}
    if not os.path.exists(ai_log_path):
        return by_rid
    for raw in _read_lines(ai_log_path):
        text = raw.strip()
        if not text.startswith('{'):
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        rid = obj.get('request_id') or ''
        row = by_rid.setdefault(rid, {'calls': 0, 'ai_ms': 0, 'max_ms': 0,
                                      'fallback': 0, 'failed': 0, 'sources': set()})
        lat = int(obj.get('latency_ms') or 0)
        row['calls'] += 1
        row['ai_ms'] += lat
        row['max_ms'] = max(row['max_ms'], lat)
        row['fallback'] += 1 if obj.get('fallback') else 0
        row['failed'] += 0 if obj.get('ok') else 1
        if obj.get('source'):
            row['sources'].add(str(obj.get('source')))
    for row in by_rid.values():
        row['sources'] = sorted(row['sources'])
    return by_rid


def attribute(rows, ai_by_rid):
    """给访问行补上 AI 归因：business_ms = 总耗时 - AI 等待；并判并行度。

    改造前落盘的旧行（rid 为空或短横）不猜时间戳，直接标 `legacy`；
    同一 rid 没有任何 AI 行 ⇒ `none`（业务耗时即总耗时）。

    `ai_pattern` 是 M57 的落点：驾驶舱红线要求「两个模型角色真并行」，
    而 PH5 收口时那只能靠读代码确认。判据是纯算术 —— 总耗时 ≈ 各次之和 ⇒ 串行，
    总耗时 ≈ 最慢一次 ⇒ 并行（容差各 10% / 15%，取"宁误报不放过"的方向）。
    """
    out = []
    for row in rows:
        if row['kind'] == 'query':
            continue
        total = row['ms'] or 0.0
        if row['rid'] in ('', '-'):
            kind, ai_ms, calls, mx, pattern = 'legacy', 0, 0, 0, ''
        else:
            ai = ai_by_rid.get(row['rid'])
            if not ai:
                kind, ai_ms, calls, mx, pattern = 'none', 0, 0, 0, ''
            else:
                kind, ai_ms, calls, mx = 'ai', ai['ai_ms'], ai['calls'], ai['max_ms']
                if calls < 2:
                    pattern = 'single'
                elif total and total >= 0.9 * ai_ms:
                    pattern = 'serial'
                elif total and total <= 1.15 * mx:
                    pattern = 'parallel'
                else:
                    pattern = 'partial'
        item = dict(row)
        item.update({'attribution': kind, 'ai_ms': ai_ms, 'ai_calls': calls, 'ai_max_ms': mx,
                     'ai_pattern': pattern,
                     'business_ms': round(max(0.0, total - ai_ms), 1),
                     'ai_share': round(ai_ms / total * 100, 1) if total and ai_ms else 0.0})
        out.append(item)
    return out


def build_report(data_dir, top=10):
    """汇总报告：按类别计数、5xx 清单、慢请求榜（含归因）、AI 侧统计。"""
    rows, hits = parse_access_lines(data_dir)
    ai_by_rid = parse_ai_rows(os.path.join(data_dir, 'ai_call_log.jsonl'))
    attributed = attribute(rows, ai_by_rid)
    reqs = [r for r in attributed if r['status'] is not None]
    by_class = {}
    for r in reqs:
        key = f'{r["status"] // 100}xx'
        by_class[key] = by_class.get(key, 0) + 1
    return {
        'files': log_files(data_dir),
        'format_hits': hits,
        'lines_parsed': len(rows),
        'requests': len(reqs),
        'request_rows': attributed,
        'by_class': by_class,
        'errors_5xx': [r for r in reqs if r['status'] >= 500],
        'slow_requests': len([r for r in attributed if r['slow']]),
        'slow_queries': len([r for r in rows if r['kind'] == 'query']),
        'slowest': sorted((r for r in attributed if r['ms']), key=lambda r: -r['ms'])[:top],
        'ai': {'requests_with_ai': len([k for k in ai_by_rid if k]),
               'calls': sum(v['calls'] for v in ai_by_rid.values()),
               'total_ms': sum(v['ai_ms'] for v in ai_by_rid.values()),
               'fallback': sum(v['fallback'] for v in ai_by_rid.values()),
               'failed': sum(v['failed'] for v in ai_by_rid.values()),
               'legacy_rows_without_rid': ai_by_rid.get('', {}).get('calls', 0)},
    }


def chain_of(rid, data_dir):
    """单个请求的全链路：访问行 + WARN + AI 出站，按原序返回。"""
    rows, _ = parse_access_lines(data_dir)
    ai = parse_ai_rows(os.path.join(data_dir, 'ai_call_log.jsonl')).get(rid) or {}
    return {'rid': rid, 'access': [r for r in rows if r['rid'] == rid], 'ai': ai}


def _fixtures():
    """造一份含三种行格式的日志夹具，返回其目录（建在系统临时目录，不落受管根）。"""
    d = tempfile.mkdtemp(prefix='ican_triage_')
    human = (
        '11:58:45 [ INFO] c6fdc87c src.app: GET /portal 200 97.0ms\n'
        '11:58:53 [ INFO] 4a8e14e9 src.app: GET /admin/ 200 7248.3ms\n'
        '11:58:53 [WARNING] 4a8e14e9 src.performance: [慢请求] GET /admin/ 耗时 7249ms（阈值 500ms）\n'
        '11:58:54 [WARNING] 9999aaaa src.performance: [慢查询] 260ms（阈值 200ms）SQL: SELECT 1\n'
        '11:58:55 [ INFO] - src.app: GET /legacy 200 5.0ms\n'
        '[2026-09-26 11:58:44,799] WARNING in app: 不是本工具该解析的行\n')
    js = ''.join(json.dumps(o, ensure_ascii=False) + '\n' for o in (
        {'timestamp': 'x', 'level': 'INFO', 'logger': 'src.app',
         'message': 'GET /h5/ 403 9.0ms', 'request_id': 'beefcafe'},
        {'timestamp': 'y', 'level': 'ERROR', 'logger': 'src.app',
         'message': 'POST /boom 500 12.3ms', 'request_id': 'deadbe01'},
        # M57 夹具的第二侧：同一 rid 两次 AI 调用、总耗时 ≈ 最慢一次 ⇒ 判并行
        {'timestamp': 'z', 'level': 'INFO', 'logger': 'src.app',
         'message': 'GET /admin/tasks/ 200 2100.0ms', 'request_id': '11223344'},
        # M57 夹具的第三侧：单次调用（total 也满足"≥0.9×和"），只有 calls<2 守卫能判对
        {'timestamp': 'w', 'level': 'INFO', 'logger': 'src.app',
         'message': 'GET /ai/chat 200 320.0ms', 'request_id': '5a5a5a5a'}))
    with open(os.path.join(d, 'app.log'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(human + js)
    ai = [
        {'ts': diag.utc_iso_z(), 'request_id': '4a8e14e9', 'scene': 'completion',
         'source': 'deepseek', 'latency_ms': 4040, 'ok': True, 'fallback': False},
        {'ts': diag.utc_iso_z(), 'request_id': '4a8e14e9', 'scene': 'completion',
         'source': 'deepseek', 'latency_ms': 2982, 'ok': True, 'fallback': False},
        {'ts': diag.utc_iso_z(), 'request_id': '11223344', 'scene': 'completion',
         'source': 'deepseek', 'latency_ms': 2000, 'ok': True, 'fallback': False},
        {'ts': diag.utc_iso_z(), 'request_id': '11223344', 'scene': 'completion',
         'source': 'deepseek', 'latency_ms': 1900, 'ok': True, 'fallback': False},
        {'ts': diag.utc_iso_z(), 'request_id': '5a5a5a5a', 'scene': 'chat',
         'source': 'deepseek', 'latency_ms': 300, 'ok': True, 'fallback': False},
        {'ts': 'x', 'request_id': '', 'scene': 'chat', 'source': 'dify',
         'latency_ms': 100, 'ok': False, 'fallback': True},
    ]
    with open(os.path.join(d, 'ai_call_log.jsonl'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.writelines(json.dumps(x, ensure_ascii=False) + '\n' for x in ai)
    return d


def _check_counts(rep, problems):
    """夹具的「计数形状」期望：三条解析腿各自命中数 + 状态分类 + 旧行计数。"""
    hits = rep['format_hits']
    if hits['human'] != 5:
        problems.append(f'human 腿命中 {hits["human"]} != 5（开发态格式化器改了？）')
    if hits['json'] != 4:
        problems.append(f'json 腿命中 {hits["json"]} != 4（生产态格式化器改了？）')
    if hits['unparsed'] != 1:
        problems.append(f'未识别行命中 {hits["unparsed"]} != 1（werkzeug 行被静默吞了？）')
    if rep['requests'] != 7:
        problems.append(f"访问行 {rep['requests']} != 7")
    if (rep['by_class'].get('5xx') != 1 or rep['by_class'].get('4xx') != 1
            or rep['by_class'].get('2xx') != 5):
        problems.append(f'状态分类错：{rep["by_class"]}')
    if rep['slow_queries'] != 1:
        problems.append(f"慢查询腿命中 {rep['slow_queries']} != 1")
    if rep['ai']['legacy_rows_without_rid'] != 1:
        problems.append('旧 AI 行（无 rid）未被单独计数')


def _check_rows(rep, problems):
    """夹具的「行内容」期望：慢请求归因、业务耗时差值、legacy 行判定。"""
    admin = [r for r in rep['slowest'] if r['path'] == '/admin/' and r['status'] == 200]
    if not admin:
        problems.append('没抓到 /admin/ 的访问行（慢请求归因无从谈起）')
        return
    row = admin[0]
    if row['ai_ms'] != 7022 or row['attribution'] != 'ai':
        problems.append(f"AI 归因错：ai_ms={row['ai_ms']} kind={row['attribution']}")
    if row['business_ms'] != 226.3:
        problems.append(f"业务耗时算错：{row['business_ms']} != 226.3")
    legacy = [r for r in rep['request_rows'] if r['attribution'] == 'legacy']
    if len(legacy) != 1:
        problems.append(f"legacy（rid 为短横的改造前行）判定 {len(legacy)} != 1")


def _check_patterns(rep, problems):
    """M57 双向判据：同是两次 AI 调用，总耗时贴着和判串行、贴着最慢一次判并行。

    拆成独立函数是为了 selftest 自己不沦为分支堆叠（PLR0912），而不是为了好看——
    判据函数一旦读不动，下一轮就没人敢改它。
    """
    by_rid = {r['rid']: r for r in rep['request_rows']}
    serial = by_rid.get('4a8e14e9', {})
    parallel = by_rid.get('11223344', {})
    if serial.get('ai_pattern') != 'serial':
        problems.append(f"串行样本判成 {serial.get('ai_pattern')!r} 应为 serial；"
                        f"total={serial.get('ms')} sum={serial.get('ai_ms')} "
                        f"max={serial.get('ai_max_ms')}")
    if parallel.get('ai_pattern') != 'parallel':
        problems.append(f"并行样本判成 {parallel.get('ai_pattern')!r} 应为 parallel；"
                        f"total={parallel.get('ms')} sum={parallel.get('ai_ms')} "
                        f"max={parallel.get('ai_max_ms')}")
    if serial.get('ai_calls') != 2 or parallel.get('ai_calls') != 2:
        problems.append('AI 调用次数没按 rid 聚合对（两例都应为 2 次）')
    # 第三侧：单次调用同样满足"总耗时 ≥ 0.9×和"，只有 calls<2 分支能把它判成 single
    single = by_rid.get('5a5a5a5a', {})
    if single.get('ai_pattern') != 'single':
        problems.append(f"单次调用样本判成 {single.get('ai_pattern')!r} 应为 single；"
                        f"total={single.get('ms')} sum={single.get('ai_ms')}")


def _check_rep(rep, problems):
    """夹具期望总入口（三段各自不超分支上限，缺一即失配）。"""
    _check_counts(rep, problems)
    _check_rows(rep, problems)
    _check_patterns(rep, problems)


def selftest():
    """夹具自证：证明三条解析腿与归因算法真的活着，而不是"跑完没报错"。"""
    problems = []
    d = _fixtures()
    try:
        _check_rep(build_report(d, top=5), problems)
        chain = chain_of('4a8e14e9', d)
        if len(chain['access']) != 2 or chain['ai'].get('calls') != 2:
            problems.append('--rid 链路不完整（访问行或 AI 行没串上）')
        empty = tempfile.mkdtemp(prefix='ican_triage_empty_')
        try:
            rep2 = build_report(empty)
            if rep2['lines_parsed'] != 0 or rep2['requests'] != 0:
                problems.append('空目录竟解析出内容（夹具目录泄漏？）')
        finally:
            shutil.rmtree(empty, ignore_errors=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return problems


def _print_summary(rep, top):
    print(f'日志文件 {len(rep["files"])} 个 / 解析行 {rep["lines_parsed"]} / '
          f'格式命中 json={rep["format_hits"]["json"]} human={rep["format_hits"]["human"]} '
          f'未识别={rep["format_hits"]["unparsed"]}')
    print(f'请求 {rep["requests"]} 条，状态分布 {rep["by_class"]}，'
          f'慢请求标记 {rep["slow_requests"]}，慢查询 {rep["slow_queries"]}')
    print(f'AI 侧：带 rid 的请求 {rep["ai"]["requests_with_ai"]}，调用 {rep["ai"]["calls"]}，'
          f'合计 {rep["ai"]["total_ms"]}ms，回退 {rep["ai"]["fallback"]}，'
          f'失败 {rep["ai"]["failed"]}')
    if rep['errors_5xx']:
        print(f'⚠️ 5xx {len(rep["errors_5xx"])} 条：')
        for r in rep['errors_5xx'][:10]:
            print(f"   rid={r['rid']} {r['method']} {r['path']} {r['status']} {r['ms']}ms")
    print(f'最慢 {min(top, len(rep["slowest"]))} 条（归因 ai=含出站调用，'
          f'none=纯业务，legacy=改造前无 rid 的旧行）：')
    for r in rep['slowest']:
        print(f"   {r['ms']:9.1f}ms  业务 {r['business_ms']:8.1f}ms  "
              f"AI {r['ai_ms']:7d}ms ({r['ai_share']:5.1f}%)  [{r['attribution']}] "
              f"{r['method']} {r['path']} {r['status']} rid={r['rid']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description='离线日志取证（app.log + ai_call_log.jsonl）')
    ap.add_argument('--dir', default=os.path.join(ROOT, 'data'), help='日志目录（默认 ./data）')
    ap.add_argument('--top', type=int, default=10, help='慢请求榜条数')
    ap.add_argument('--rid', help='只看某一个请求 id 的全链路')
    ap.add_argument('--json', action='store_true', help='机器可读输出')
    ap.add_argument('--selftest', action='store_true', help='夹具自证（不落任何产物）')
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        # argparse 用法错必须占独立档：1 已被"有 5xx"占用，2 是"没测到"，
        # 混用会让自动化对着一条写错的命令无限重试（第 17 轮同一族坑）
        return 0 if exc.code in (0, None) else 4
    return dispatch(args)


def dispatch(args):
    if args.top < 1:
        print('❌ --top 至少为 1')
        return 4
    if args.selftest:
        problems = selftest()
        for p in problems:
            print('❌ ' + p)
        print('log_triage selftest: ' + ('全对' if not problems else f'{len(problems)} 项不对'))
        return 0 if not problems else 1
    if not os.path.isdir(args.dir):
        print(f'❌ 日志目录不存在：{args.dir}（没读到内容不等于没有异常）')
        return 2
    if args.rid:
        chain = chain_of(args.rid, args.dir)
        print(json.dumps(chain, ensure_ascii=False, indent=1, default=str))
        return 0 if chain['access'] or chain['ai'] else 2
    rep = build_report(args.dir, top=args.top)
    if rep['lines_parsed'] == 0:
        print(f'❌ 未读到任何日志行：{args.dir}（判"没测到"，不判"通过"）')
        return 2
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    else:
        _print_summary(rep, args.top)
    return 1 if rep['errors_5xx'] else 0


if __name__ == '__main__':
    sys.exit(main())
