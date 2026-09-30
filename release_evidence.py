#!/usr/bin/env python
"""发布制品校验和台账生成器（第 18 轮 H34，对标 Airflow 的 release `.sha512`/`.asc`）。

背景：AGENTS / 07 / `提交清单与验收状态.md` 三处写着"唯一提交包 SHA256 台账"，
而第 18 轮实测 `find -iname "*sha256*" -o "*台账*" -o "*checksum*"` 全仓 0 命中 ——
声称先于实体。本工具把它变成磁盘上可复算的实体，并被门禁**第 29 项**双向校验
（台账里每条须与实盘一致；实盘每个在范围内的文件也必须在台账里）。

复用门禁的排除规则（`SKIP_DIRS`/`SKIP_EXTS` 来自 quality_gate）而不是再抄一份：
两处规则迟早会漂移，漂移了就又造出"门禁说一致、台账说另一套"。

用法：
    python release_evidence.py            # ⚠️ 必须在权威副本 05-源代码全文/ 内跑：
                                          #    台账是交付物，开发副本含 memory/ 与
                                          #    3d-narrative/ 等非交付文件（实测 263 → 3594 条）
    python release_evidence.py --json     # 机器可读汇总
退出码：0 生成成功 / 1 无法定位发布目录或版本 / 4 参数非法
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

USAGE = __doc__.split('用法：', 1)[-1]


def app_version():
    p = os.path.join(ROOT, 'src', 'version.py')
    m = re.search(r"^APP_VERSION\s*=\s*['\"]([^'\"]+)", open(p, encoding='utf-8').read(), re.M)
    return m.group(1) if m else ''


def collect(skip_dirs, skip_exts, skip_names=frozenset()):
    """{副本内相对路径: sha256}，路径一律正斜杠且不含绝对路径（台账要进盲审扫描面）。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if fn.endswith(skip_exts) or fn in skip_names:
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, ROOT).replace(os.sep, '/')
            out[rel] = hashlib.sha256(open(p, 'rb').read()).hexdigest()
    return out


def aggregate(digests):
    blob = ''.join(f'{k} {digests[k]}\n' for k in sorted(digests))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description='生成发布制品 SHA256 台账（门禁第 29 项消费）')
    ap.add_argument('--json', action='store_true', help='输出机器可读汇总')
    ap.add_argument('--check', action='store_true', help='只校验既有台账与实盘是否一致，不写盘')
    args, unknown = ap.parse_known_args(argv)
    if unknown:
        print(f'❌ 未知参数：{unknown}\n{USAGE}')
        return 4

    import quality_gate as qg      # 排除规则的唯一来源

    ver = app_version()
    if not ver:
        print('❌ 读不到 APP_VERSION（src/version.py），拒绝生成无版本锚点的台账')
        return 1
    out_dir = os.path.normpath(os.path.join(ROOT, '..', '09-发布校验'))
    parent = os.path.dirname(out_dir)
    if not os.path.isdir(parent):
        print(f'❌ 找不到发布证据父目录：{parent}（本工具须在交付副本内跑）')
        return 1
    # 台账只该落在「工程佐证材料/09-发布校验」。在开发副本里跑时 ROOT 的上一级是工作区
    # 根，父目录照样存在 —— 于是静默写出一份同名的越权台账（实测造出过 3,605 行的那一份，
    # 把整个工作区而非交付集装进台账）。判据按父目录名钉死，而不是按"目录在不在"。
    if os.path.basename(parent) != '工程佐证材料':
        print(f'❌ 当前根目录的上一级是「{os.path.basename(parent)}」，不是「工程佐证材料」：'
              f'\n   台账会写到 {out_dir}\n   请在交付副本内跑（例：cd 交付物/提交包/工程佐证材料'
              f'/05-源代码全文 && python release_evidence.py）')
        return 2
    digests = collect(qg.LEDGER_SKIP_DIRS, qg.LEDGER_SKIP_EXTS,
                        getattr(qg, 'LEDGER_SKIP_NAMES', frozenset()))
    agg = aggregate(digests)
    path = os.path.join(out_dir, f'SHA256SUMS-v{ver}.txt')

    if args.check:
        if not os.path.isfile(path):
            print(f'❌ 台账不存在：{path}')
            return 1
        listed = {}
        for ln in open(path, encoding='utf-8').read().splitlines():
            parts = ln.split()
            if len(parts) == 2 and re.match(r'^[0-9a-f]{64}$', parts[0]):
                listed[parts[1]] = parts[0]
        bad = [k for k, v in digests.items() if listed.get(k) != v]
        extra = sorted(set(listed) - set(digests))
        print(json.dumps({'ok': not (bad or extra), 'version': ver, 'files': len(digests),
                          'stale_or_missing': bad[:10], 'orphan_entries': extra[:10],
                          'aggregate': agg}, ensure_ascii=False))
        return 0 if not (bad or extra) else 1

    os.makedirs(out_dir, exist_ok=True)
    # 台账生成戳走**显式 UTC**：这份清单要在别的机器上复算核对，本地时区的 awareness 戳
    # 会让"同一份内容的两次生成"跨机不可比（第 29 轮：技术戳一律显式带时区，不吃进程时钟）
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    lines = [f'# VERSION v{ver}', f'# GENERATED {stamp}',
             f'# FILES {len(digests)}', f'# AGGREGATE {agg}',
             '# scope=本目录（权威源码副本）内除 data/__pycache__/.venv/演示快照集/*.pyc|db|log 外的全部文件']
    lines += [f'{digests[k]}  {k}' for k in sorted(digests)]
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines) + '\n')
    print(json.dumps({'written': path, 'files': len(digests), 'aggregate': agg,
                      'version': ver}, ensure_ascii=False) if args.json
          else f'✅ 台账已生成：{path}\n   {len(digests)} 个文件 / 总哈希 {agg[:16]}… / 版本 v{ver}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
