#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把备份**载回并断言** —— 备份只有被恢复过，才不只是"一个曾经成功写出的文件"。

为什么现在补这一件（2026-09-27 第 33 轮当场实测，取数面 = `src/*.py` + `src/routes/*.py` + 根级脚本）：

    grep "def restore"  →  只有 `src/pack_io.py:restore_builtin` / `src/pack_store.py:restore_active`
                           两处，且都是**行业包**的恢复，与数据库无关
    grep "iterdump"     →  0 处
    grep "ATTACH"       →  2 处（都在别处，不用于恢复）
    backups/*.db        →  11 份，其中 8 份连证据件都没有；**0 份被载回过**

⇒ 门禁第 38 项自己写着"回填态不得当可恢复凭据"，但全仓没有任何一条把备份载回的路径 ——
那句话至今是一张空头支票：真出事时第一次执行恢复的人，会在恢复当场才知道行不行。

上游对照（本轮 `gh api` 实取原文，落 `D:/tmp/bench34/`）：
  discourse `lib/backup_restore.rb:19 def self.restore!` + `:23 rollback!`
    —— 恢复与回滚是**成对**的两个出口，不是一个函数；
  同文件 `:24 raise BackupRestore::OperationRunningError if is_operation_running?`
    —— 恢复期挂**互斥旗标**，禁止与备份/其它恢复并发；
  同文件 `:59 allow_restore: Rails.env.development? || SiteSetting.allow_restore`
    —— **恢复默认关闭，须显式开关**（生产端覆盖在用库必须点名）；
  同文件 `:68 current_version` / `:72 postgresql_major_version`
    —— 版本核对通过才允许搬表（本仓对偶 = 备份清单里的 `content.rows_by_table` 复算对账）。

退出码是**类别码**（"没测到"绝不塌成 0）：
  0 成功 / 1 校验或对账不过 / 2 输入缺失（备份不存在、空文件、非 SQLite）/
  3 目标已存在且未显式 `--force`（拒绝覆盖 = 正确行为，不是失败）/ 4 证据件写不出去
"""
import argparse
import ast
import contextlib
import fnmatch
import json
import os
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from src.backup_manifest import (  # noqa: E402  复用同一把尺：sha/行数/integrity/证据卡命名不另立第二套
    KEY_TABLES,
    reconcile,
    _portable_path,
    empty_guard,
    integrity_ok,
    read_manifest,
    sha256_file,
    table_counts,
    write_manifest,
)

RC_OK, RC_MISMATCH, RC_NO_INPUT, RC_TARGET_EXISTS, RC_EVIDENCE = 0, 1, 2, 3, 4
EVIDENCE_FILE = 'RESTORE-EVIDENCE.json'
EVIDENCE_SCHEMA = 'ican-restore-evidence-v1'
MAX_ENTRIES = 20


def _now_iso():
    """带**真实偏移**的 ISO 时刻。

    写死 `+08:00` 在本机看着对，换到 UTC 的 CI/别人机器上就是一条假账 ——
    门禁第 47 项要按这条算新鲜度，所以时刻必须是量出来的而不是抄来的。
    """
    from datetime import UTC, datetime
    return datetime.now(UTC).astimezone().isoformat(timespec='seconds')


def _ro(path):
    """只读打开源：方向写反时（`target.backup(source)`）只读连接当场抛 readonly，
    而不是把空库灌回真库 —— 2026-09-26 本机实测踩过，故源必须 mode=ro。"""
    return sqlite3.connect(f'file:{os.path.abspath(path)}?mode=ro', uri=True)


def snapshot_into(backup, target):
    """把 backup 的字节快照到 target（target 可为已存在的空串路径）。返回 (ok, detail)。"""
    try:
        source = _ro(backup)
    except sqlite3.Error as exc:
        return False, f'无法只读打开备份: {type(exc).__name__}: {exc}'
    try:
        try:
            dst = sqlite3.connect(target)
        except sqlite3.Error as exc:
            return False, f'无法打开恢复目标: {type(exc).__name__}: {exc}'
        try:
            source.backup(dst)          # 方向：source(备份) → dst(恢复目标)
        finally:
            dst.close()
    except sqlite3.Error as exc:
        return False, f'在线恢复失败: {type(exc).__name__}: {exc}'
    finally:
        source.close()
    return True, '载回完成'


def inspect(path, label):
    """一个库的可信读数：integrity + 逐表行数 + 空库守卫。任一步失败 ⇒ (None, 原因)。"""
    if not os.path.isfile(path):
        return None, f'{label} 不存在: {path}'
    if os.path.getsize(path) == 0:
        return None, f'{label} 是 0 字节空文件（零输入不得判通过）: {path}'
    # `integrity_ok` 内部不吞异常：损坏/非 SQLite 文件会让 PRAGMA 直接抛
    # （本轮篡改实测：截断 4096B 后 traceback 从判据里穿出来）。
    # 失败路径本身必须能失败**得体面** —— 转成 rc=2 + 点名原因，而不是崩给调用方看。
    try:
        ok, why = integrity_ok(path)
    except sqlite3.Error as exc:
        return None, f'{label} 连 integrity_check 都跑不动: {type(exc).__name__}: {exc}'
    if not ok:
        return None, f'{label} integrity_check 非 ok: {why}'
    try:
        counts = table_counts(path)
    except sqlite3.Error as exc:
        return None, f'{label} 行数复算失败: {type(exc).__name__}: {exc}'
    eg_ok, eg_why = empty_guard(counts)
    if not eg_ok:
        return None, f'{label} 触发空库守卫: {eg_why}'
    return {'integrity': 'ok', 'tables': len(counts), 'row_total': sum(counts.values()),
            'rows_by_table': dict(sorted(counts.items()))}, ''


def manifest_expectation(backup):
    """备份证据卡里声明的行数（有则对账，无则如实标 UNVERIFIED —— 不猜、不算通过）。

    命名与读取一律走 `src.backup_manifest`（证据件真名是 `<备份>.manifest.json`，
    本轮首版按 `备份 + ".json"` 拼路径 ⇒ 连唯一那份在存证据卡都看不见，等于对账恒 UNVERIFIED）。
    """
    doc, err = read_manifest(backup)
    if doc is None:
        return None, err or '证据卡不可读'
    rows = ((doc.get('content') or {}).get('rows_by_table') or {})
    return (rows or None), ('与证据卡对账' if rows else '证据卡没有 rows_by_table 段')


def pre_rollback_copy(target, ts, tag='pre-restore', tool='restore_db.py'):
    """覆盖在用库之前先把目标存一份"回滚锚点"（对偶 discourse 的 `rollback!`）。

    锚点同样要写证据卡：门禁第 38 项的不变式是"写快照的出口必须留证据"，
    一份没有证据卡的锚点出事时只是"一个看起来像库的文件"——那正是本项目反复踩的
    "没恢复过的备份不算备份"。

    `tag`/`tool` 从第 35 轮起可传：补丁回滚（`init_db.rollback_patch`）走的是**同一条不变式**
    （动手前先有一份可核副本），所以复用这个唯一出口，而不是另建第二套锚点写法；
    但证据卡里的 `tool` 必须如实写是谁建的，否则事后按产出方归因会指错人。
    """
    anchor = f'{target}.{tag}-{ts}.bak'
    ok, why = snapshot_into(target, anchor)
    if not ok:
        return '', why
    try:
        # src_counts + counts 两个都要给：`write_manifest` 只在两者齐备时才记 `row_parity`，
        # 只给 source_path 会让卡上 `row_parity=None` ⇒ 第 51 项深核判它"卡不可信"。
        # 真生产者 backup_db.py:202 一直是两个都给的，锚点这里此前漏了一个参数。
        write_manifest(anchor, source_path=target,
                       src_counts=table_counts(target), counts=table_counts(anchor),
                       integrity=True, tool=tool)
    except Exception as exc:                      # 证据卡写失败必须说出来，但不毁掉已经成功的锚点
        return anchor, f'锚点已建但证据卡失败: {type(exc).__name__}: {exc}'
    return anchor, ''


def write_evidence(entry, root=None):
    """证据件追加写（保留最近 MAX_ENTRIES 条）。写不出去 ⇒ RC_EVIDENCE，不许静默。"""
    path = os.path.join(root or BASE_DIR, EVIDENCE_FILE)
    doc = {'schema_version': EVIDENCE_SCHEMA, 'entries': []}
    if os.path.isfile(path):
        try:
            loaded = json.loads(open(path, encoding='utf-8').read())
            if isinstance(loaded.get('entries'), list):
                doc = loaded
        except (OSError, ValueError):
            pass                                    # 坏件按空账继续，但下面如实记一行
    doc['entries'] = (doc.get('entries') or [])[-(MAX_ENTRIES - 1):] + [entry]
    try:
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write('\n')
    except OSError as exc:
        return None, f'证据件写不出去: {type(exc).__name__}: {exc}'
    return path, ''


def restore(backup, into, force=False, live=None):
    """一次可核的恢复：载回 → 目标复算 → 与源/清单三方对账 → 落证据。返回 (rc, doc)。"""
    src, why = inspect(backup, '备份')
    if src is None:
        return RC_NO_INPUT, {'error': why, 'backup': backup}
    expected, exp_note = manifest_expectation(backup)
    if expected is not None and expected != src['rows_by_table']:
        diff = {k: (expected.get(k), src['rows_by_table'].get(k))
                for k in set(expected) | set(src['rows_by_table'])
                if expected.get(k) != src['rows_by_table'].get(k)}
        return RC_MISMATCH, {'error': f'备份与自身清单对不上（这份 .db 不可信）：{diff}',
                             'backup': backup}
    ts = time.strftime('%Y%m%d_%H%M%S')
    forced, anchor = False, ''
    if os.path.isfile(into) and os.path.getsize(into) > 0:
        if not force:
            return RC_TARGET_EXISTS, {'error': f'恢复目标已存在且非空: {into}（要覆盖请加 --force；'
                                               '这不算失败，算拒覆盖）', 'target': into}
        forced = True
        anchor, why = pre_rollback_copy(into, ts)
        if not anchor:
            return RC_MISMATCH, {'error': f'覆盖前无法建回滚锚点，中止（绝不裸覆盖）: {why}'}
        anchor_note = why                                       # 证据卡失败也要留痕，不静默
    ok, why = snapshot_into(backup, into)
    if not ok:
        return RC_MISMATCH, {'error': why, 'backup': backup, 'target': into}
    dst, why = inspect(into, '恢复目标')
    if dst is None:
        return RC_MISMATCH, {'error': f'载回后目标不可信: {why}', 'target': into}
    if dst['rows_by_table'] != src['rows_by_table']:
        return RC_MISMATCH, {'error': '恢复后行数与备份不一致（复制被截断/表缺失）',
                             'src': src['rows_by_table'], 'dst': dst['rows_by_table']}
    doc = {'at': _now_iso(), 'face': _portable_path(BASE_DIR), 'backup': os.path.basename(backup),
           'backup_sha256': sha256_file(backup), 'size_bytes': os.path.getsize(backup),
           'backup_path': _portable_path(backup),
           'target': os.path.basename(into), 'tables': dst['tables'],
           'row_total': dst['row_total'], 'key_rows': {t: dst['rows_by_table'].get(t, 0)
                                                       for t in KEY_TABLES},
           'integrity': dst['integrity'], 'reconcile': exp_note, 'forced': forced, 'anchor_note': anchor_note if forced else '',
           'rollback_anchor': os.path.basename(anchor) if anchor else '', 'rc': RC_OK}
    return RC_OK, doc


def selftest_roundtrip(tmpdir=None):
    """造一个含关键表的小库 → 备份 → 恢复到新文件 → 断言逐表行数相等。
    门禁第 47 项与本脚本 `--selftest` 共用这一个函数（一个事实一处判）。"""
    tmpdir = tmpdir or os.path.join(BASE_DIR, 'data')
    os.makedirs(tmpdir, exist_ok=True)
    src_path = os.path.join(tmpdir, '_rt_src.db')
    bak_path = os.path.join(tmpdir, '_rt_bak.db')
    dst_path = os.path.join(tmpdir, '_rt_dst.db')
    for p in (src_path, bak_path, dst_path):
        if os.path.isfile(p):
            os.remove(p)
    conn = sqlite3.connect(src_path)
    try:
        conn.execute('CREATE TABLE merchants (id INTEGER PRIMARY KEY, name TEXT)')
        conn.execute('CREATE TABLE stores (id INTEGER PRIMARY KEY, merchant_id INTEGER)')
        conn.executemany('INSERT INTO merchants (name) VALUES (?)', [('甲',), ('乙',)])
        conn.executemany('INSERT INTO stores (merchant_id) VALUES (?)', [(1,), (1,), (2,)])
        conn.commit()
    finally:
        conn.close()
    ok, why = snapshot_into(src_path, bak_path)
    if not ok:
        return False, f'往返第一步（备份）就失败: {why}'
    rc, doc = restore(bak_path, dst_path)
    legs = [
        ('备份能被载回且退出码为 0', rc == RC_OK, str(doc)[:120]),
        ('恢复目标逐表行数等于源（不是"非空"）',
         (doc.get('key_rows') or {}).get('stores') == 3 and (doc.get('key_rows') or {}).get('merchants') == 2,
         str(doc.get('key_rows'))),
        ('拒覆盖：目标已存在且无 --force ⇒ RC_TARGET_EXISTS',
         restore(bak_path, dst_path)[0] == RC_TARGET_EXISTS, '第二次无 force 恢复的 rc'),
        ('零输入判红：空文件不当作可恢复对象',
         inspect(_mk_empty(tmpdir, '_rt_zero.db'), '备份')[0] is None, '空文件必须被拒'),
    ]
    bad = [f'{n}（{ev}）' for n, passed, ev in legs if not passed]
    for p in (src_path, bak_path, dst_path, os.path.join(tmpdir, '_rt_zero.db')):
        if os.path.isfile(p):
            os.remove(p)
    return not bad, (f'{len(legs)} 腿全过' if not bad else '；'.join(bad))


def _mk_empty(dirpath, name):
    path = os.path.join(dirpath, name)
    with open(path, 'wb'):
        pass
    return path


def latest_evidence(root=None):
    path = os.path.join(root or BASE_DIR, EVIDENCE_FILE)
    if not os.path.isfile(path):
        return None, f'缺 {EVIDENCE_FILE}：一次恢复都没留过痕'
    try:
        doc = json.loads(open(path, encoding='utf-8').read())
    except (OSError, ValueError) as exc:
        return None, f'{EVIDENCE_FILE} 不是合法 JSON: {type(exc).__name__}'
    return doc, ''


# ══════════════ 备份"挑哪一份"的资格判定面（第 36 轮 H134）══════════════
# 来历：`restore_db.py` 只能吃显式 `--backup <路径>`，全仓**没有任何"挑一份可用的"出口**，
# 而 `latest_evidence()`（:268）读的是恢复史台账、不是备份人口 —— 两者被当作一回事过。
# 实测（2026-09-28 当场）：`backups/*.db` 14 件、带证据卡的 1 件，且**最新那份没有证据卡**。
# ⇒ "出事时挑最近一份"这句话里，"最近"可由文件名算出来，"可用"此前无处可查。
#
# 上游对照（本轮 `gh api` 实取原文，落 `D:/tmp/bench36/`）：
#   frappe `frappe/utils/backups.py:303 backup_time()` —— 排序取的是**文件名里的时间戳**，
#     不是 mtime（名字才是"这份库属于哪一次"的凭据，mtime 会被复制/同步改掉）；
#   同文件 `:314 old_enough()` + `:741 is_file_old()` —— **路径不存在直接算"太旧"**（取不到=不合格，
#     不是"没有反证所以合格"）；
#   discourse `lib/backup_restore/backup_store.rb:22 files` / `:27 latest_file` —— 列表按
#     `-last_modified` 排好，`latest_file` 只是 `files.first`（选择=读已排好形的清单，不另算一套）；
#   同目录 `local_backup_store.rb:53 unsorted_files` —— 人口按**扩展名白名单**取（`*.{gz,tgz}`），
#     `:70 create_file_from_path` —— 每个条目是带 filename/size/last_modified 的**类型化记录**，
#     不是一条裸路径。本仓对偶 = 下面的 `backup_records()` 返回 dict 列表。
#   restic `internal/repository/checker.go:217/:227/:237` —— 声明⇄实测的差集分**三类**各出一行
#     （missing / size 不符 / orphaned），不折叠成一个"对不上"。本仓对偶 = `manifest_orphan`
#     （有 db 无卡）与 `manifest_ghost`（有卡无 db）**分开记**。
NAME_TS_RE = re.compile(r'(20\d{6}_\d{6})')
BACKUP_EXTS = ('.db',)
ELIG_STATES = ('eligible', 'nameless', 'no_manifest', 'stale', 'manifest_mismatch',
               'integrity_fail', 'unreadable')
REFUSAL_STATES = ELIG_STATES[1:]
MAX_AGE_H = 72                     # 与门禁第 49 项的覆盖率同一档（同一把尺不另立）


def name_ts(name):
    """文件名里的时刻（本地时间，与 `backup_db.py` 的 strftime 同口径）。取不到 ⇒ None。

    为什么不用 mtime：备份会被同步盘/复制/解压改动时间戳，而名字是写它的那个人/工具给的"哪一次"。
    frappe 同样按名字排（`backups.py:303`），这不是巧合而是同一威胁模型。
    """
    m = NAME_TS_RE.search(os.path.basename(str(name)))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), '%Y%m%d_%H%M%S')
    except ValueError:                                   # 2099 月日之类"看着像但不是"
        return None


def _ts_iso(dt):
    """时刻 → 可打印串；取不到给**空串**而不是把 None 混进字段（None 会让台账/判据两侧不同形）。"""
    return dt.isoformat(timespec='seconds') if dt else ''


def name_ts_utc(name):
    """文件名里的时刻 → **aware UTC**（要与账本比的时候只能用这个）。取不到 ⇒ None ⇒ 调用方必须拒。

    第 36 轮实测的坑（同一件事两个基准）：账本 `applied_at` 是 naive UTC
    （`src/models.py:535-536`），而 `ican_before_p007_20260927_094600.db` 这个名字是
    人眼看到的钟面（+08）⇒ 直接比会差整 8 小时：实测同一次 p007 应用，账本写
    `2026-09-27 01:46:09`、文件名写 `094600`。基准必须由一处定义，所以两个换算都走
    `src/business_calendar`（`from_wall_clock` / `from_utc_naive`），不在这里各算各的。
    """
    dt = name_ts(name)
    if dt is None:
        return None
    from src.business_calendar import from_wall_clock
    return from_wall_clock(dt)


def wall_ts():
    """14 位业务钟面戳 —— 备份名/回滚锚点名的唯一合法来源（防两处各写各的 strftime）。"""
    from src.business_calendar import wall_stamp
    return wall_stamp()


def eligibility(name, has_manifest, age_h, max_age_h=MAX_AGE_H, integrity='unchecked',
              card='unchecked'):
    """一份备份**够不够格被自动选中** —— 纯函数：只吃读数，不碰盘（契约①：fn(input)->判定）。

    顺序即语义，且必须按"便宜的先跑"：
      1. 名字里没有可信时刻 ⇒ `nameless`（无法排序 ⇒ 无法声称"最近"，后面都不必问）
      2. 没有证据卡 ⇒ `no_manifest`（第 33 轮立的不变式：没被核过的备份只是一个文件）
      3. 超过 `max_age_h` ⇒ `stale`
      4. 真跑过 `integrity_check` 且非 ok ⇒ `integrity_fail`；连打开都失败 ⇒ `unreadable`
    `card` 三态：`unchecked`（浅扫，没跟实盘对过账）/ `ok` / `mismatch`
    （卡里的 sha256·size·status·origin·row_parity 至少一项对不上）。
    **"旁边有个同名 .manifest.json" 不等于"这张卡说的是这个文件"** ——
    本轮实测：给一份 8KB 全零件写一张属于别的库的卡，浅扫下它被判 eligible 并进过选择结果。
    对账本身复用 `src.backup_manifest.reconcile()`（第 38 项的那把尺），不在此另写一套。

    `integrity='unchecked'` 与 `card='unchecked'` 都是**未核**而不是"核过没问题"，
    所以它们落 `eligible` 时调用方必须把"未核"一起印出来 —— 禁止把没测到读成测过了。
    """
    if name_ts(name) is None:
        return 'nameless', '文件名里没有 20YYYYMMDD_HHMMSS 时刻，无法判定"最近"'
    if not has_manifest:
        return 'no_manifest', '缺 <备份>.manifest.json 证据卡（没核过的备份不算可自动选用的备份）'
    if age_h is None:
        return 'stale', '拿不到文件时间戳'
    if age_h > max_age_h:
        return 'stale', f'距now {age_h:.1f}h 超过上限 {max_age_h}h'
    if integrity == 'fail':
        return 'integrity_fail', 'integrity_check 非 ok'
    if integrity == 'error':
        return 'unreadable', '连 integrity_check 都跑不动'
    # 卡对不上排在"文件本身能读"之后：一件打不开的库，"卡与字节不符"只是它的下游症状，
    # 报在后面会让人以为文件是可用的。
    if card == 'mismatch':
        return 'manifest_mismatch', '证据卡与这份文件对不上账（sha/size/status/origin/row_parity 之一）'
    return 'eligible', ''


def _state_from_inspect(path):
    """把 `inspect()` 的读数压成 eligibility 需要的三态之一（ok/fail/error/unchecked）。"""
    reading, why = inspect(path, '备份')
    if reading is not None:
        return 'ok', ''
    if 'integrity_check 非 ok' in why:
        return 'fail', why
    return 'error', why


def backup_records(root=None, now=None, max_age_h=MAX_AGE_H, deep=False):
    """备份人口 → 类型化记录列表（按名字里的时刻**新→旧**排；`deep` 才逐个跑完整性）。

    `deep=False` 是默认：`--list-backups` 要能秒回，而"未核"会原样印在 `integrity` 字段上。
    清单同时收**有卡无件**的幽灵记录（restic 的 orphaned/missing 不得折叠成一句"对不上"）。
    """
    root = root or BASE_DIR
    now = now or datetime.now(UTC)
    bdir = os.path.join(root, 'backups')
    names = set()
    if os.path.isdir(bdir):
        for fn in sorted(os.listdir(bdir)):
            if fn.endswith(BACKUP_EXTS) and not fn.endswith(('.bak', '.tmp')):
                names.add(fn)
    records = []
    for fn in sorted(names, key=lambda x: name_ts(x) or datetime.min, reverse=True):
        path = os.path.join(bdir, fn)
        try:
            age_h = (now - datetime.fromtimestamp(os.path.getmtime(path), UTC)).total_seconds() / 3600.0
        except OSError:
            age_h = None
        has_manifest = read_manifest(path)[0] is not None
        integ = _state_from_inspect(path)[0] if deep else 'unchecked'
        card = 'unchecked'
        why_card = ''
        if deep and has_manifest:
            trust_ok, why_card = reconcile(path)       # 复用第 38 项那把尺（一个事实一处判）
            card = 'ok' if trust_ok else 'mismatch'
        state, why = eligibility(fn, has_manifest, age_h, max_age_h, integ, card)
        if card == 'mismatch':
            why = f'{why}：{why_card}' if why_card else why
        records.append({'name': fn, 'path': path, 'name_ts': _ts_iso(name_ts(fn)),
                        'age_h': None if age_h is None else round(age_h, 2),
                        'bytes': os.path.getsize(path) if os.path.isfile(path) else 0,
                        'manifest': has_manifest, 'integrity': integ, 'state': state, 'why': why})
    for path in sorted(os.listdir(bdir) if os.path.isdir(bdir) else []):
        if not path.endswith('.manifest.json'):
            continue
        twin = path[:-len('.manifest.json')]
        if twin not in names and twin.endswith(BACKUP_EXTS):
            records.append({'name': twin, 'path': '', 'name_ts': _ts_iso(name_ts(twin)),
                            'age_h': None, 'bytes': 0, 'manifest': True, 'integrity': 'absent',
                            'state': 'manifest_ghost', 'why': '有证据卡但没有对应的 .db（清单成了孤儿 ⇒ 这份备份实际不存在）'})
    return records


def pick_latest_eligible(root=None, now=None, max_age_h=MAX_AGE_H, deep=True):
    """自动选择唯一出口：从排好形的清单里取**第一个 eligible**，否则给拒绝原因。

    与 discourse `backup_store.rb:27 latest_file = files.first` 同形 —— 选择只是读清单的头，
    判定全部在造清单时做完；这样"挑中的那份"与"清单上写的那份"不可能不一致。
    `deep=True`：真恢复前必须把完整性跑实（frappe `is_file_old` 的取向：取不到即不合格）。
    """
    records = backup_records(root=root, now=now, max_age_h=max_age_h, deep=deep)
    for rec in records:
        if rec['state'] == 'eligible':
            return rec['path'], rec, records
    return None, None, records


# ══════════ 保留体检：哪条通道管哪一族、三把尺对得上吗（第 38 轮 H151；只读，绝不删）══════════
# 动因（2026-09-28 当场实测，人口仍由 backup_records() 这一把尺给出，本模块不另写 glob）：
#   全人口 14 件 = `ican_*` 6 / `demo-reset_*` 3 / **族外 5**（`PH2-*` 4 + `ican-db-before-cisim-*` 1），
#   最老件 1631.57h ≈ 68 天；两条淘汰通道各自 keep=30 / keep=10 ⇒ 至今谁都没触发过淘汰；
#   而资格面第三把尺 `MAX_AGE_H=72`（本模块）与那两个"份数上限"从不对话 ⇒
#   "账面留 30 份"与"够格载回的只有 1 件"同时为真，且没人把这两件事放在一起看过。
# 上游对照（本轮 `gh api` 取回原文落 D:/tmp/bench38/，行号逐行读过）：
#   discourse/discourse `lib/backup_restore/backup_store.rb` 把**两把尺写在同一个类里且各自带守卫**——
#     `:36-43 delete_old` 计数尺（`SiteSetting.maximum_backups`），先 `:37 return unless cleanup_allowed?`；
#     `:45-53 delete_prior_to_n_days` 时间尺（`remove_older_backups`），`:47` 先验窗口是正数、
#     `:48` 再过同一守卫；`:100-102 used_bytes` 让存储自报足迹。
#   frappe/frappe `frappe/utils/backups.py:630 delete_temp_backups(older_than=23)` /
#     `:644 is_file_old(file_path, older_than=24)` —— 按**小时**而非按份数；
#     `:276-277` 从文件名 `%Y%m%d_%H%M%S` 解析时刻（与本模块 `name_ts()` 同一取向，不用 mtime）。


def _read_src(path):
    """读源码文本。第 38 轮补：上一版这里直接写 read(path)，那是 quality_gate 的助手，
    restore_db 里没有这个名字 ⇒ NameError。与上一轮"判据腿调一个从未实现的函数"同族，
    只是方向反过来：这次是在被测脚本里用了门禁模块的名字。故块内自由名一律先静态核。"""
    with open(path, encoding='utf-8') as f:
        return f.read()


class RetentionError(RuntimeError):
    """射程读不出来 = 没测。绝不允许退化成"族外为零 ⇒ 保留策略健康"。"""


def _ast_module_literal(tree, name):
    """模块级字面量（`Assign` 与 `AnnAssign` 都认；非字面量与查无此名一律 RetentionError）。"""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for tgt in targets:
            if isinstance(tgt, ast.Name) and tgt.id == name:
                try:
                    return ast.literal_eval(node.value)
                except ValueError as exc:
                    raise RetentionError(
                        f'{name} 不是字面量（{type(exc).__name__}）⇒ 射程不可核对') from None
    raise RetentionError(f'模块级找不到 {name} ⇒ 射程不可核对')


def _glob_patterns(tree):
    """该模块真传给 `glob.glob(...)` 的**通配字面量**（按行为取；改代码即改读数，注释不算）。"""
    pats = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, 'attr', '') == 'glob'):
            continue
        for arg in node.args:
            for lit in re.findall(r"'([^']*\*[^']*)'|\"([^\"]*\*[^\"]*)\"", ast.unparse(arg)):
                m = next(x for x in lit if x)
                base = os.path.basename(m.replace('\\', '/'))
                if '*' in base:
                    pats.append(base)
    return sorted(set(pats))


def _prune_shape(tree, func_name, expect_name):
    """从 `_prune_backups` 里现取"前缀变量名 + 后缀字面量"，据此推出该通道真正吃的形态。

    只认 `name.startswith(f'<expect_name>_')` 这一种写法：换成别的变量名就 RetentionError，
    免得我按 BACKUP_PREFIX 推出来的族与它实际删的族悄悄分家（那又是一条恒绿判据）。
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == func_name):
            continue
        prefix_var, suffix = None, None
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Call) and getattr(sub.func, 'attr', '') in ('startswith', 'endswith')
                    and sub.args):
                continue
            try:
                val = ast.literal_eval(sub.args[0])
            except ValueError:
                val = None
            if isinstance(val, str) and val.startswith('.') and sub.func.attr == 'endswith':
                suffix = val
            elif sub.func.attr == 'startswith':
                names = {n.id for n in ast.walk(sub.args[0]) if isinstance(n, ast.Name)}
                if names:
                    prefix_var = next(iter(names))
        if prefix_var is None or suffix is None:
            raise RetentionError(f'{func_name} 里前缀或后缀读不出来 ⇒ 该通道射程不可核对')
        if prefix_var != expect_name:
            raise RetentionError(
                f'{func_name} 用 {prefix_var} 划族，而我只在册 {expect_name} ⇒ 请同步登记，别猜')
        return suffix
    raise RetentionError(f'找不到函数 {func_name} ⇒ 通道未在册')


def retention_channels(root=None):
    """淘汰通道清单：模式与 keep **一律从生产者源码现取**（新增生产者未在册 ⇒ 门禁第 53 项判红）。"""
    root = root or os.path.dirname(os.path.abspath(__file__))
    chans = []
    for rel, keeper, prefix_name, prune_fn in (
            ('backup_db.py', 'DEFAULT_KEEP', None, None),
            ('src' + os.sep + 'pack_demo.py', 'BACKUP_KEEP', 'BACKUP_PREFIX', '_prune_backups')):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            raise RetentionError(f'取不到生产者 {rel} ⇒ 无法核对淘汰通道')
        tree = ast.parse(_read_src(path))
        keep = int(_ast_module_literal(tree, keeper))
        if keep < 1:
            raise RetentionError(f'{rel} 的 {keeper}={keep} 非法 ⇒ 射程为零不是健康')
        if prune_fn:
            suffix = _prune_shape(tree, prune_fn, prefix_name)
            pats = [f'{_ast_module_literal(tree, prefix_name)}_*{suffix}']
        else:
            pats = _glob_patterns(tree)
            if not pats:
                raise RetentionError(f'{rel} 里没有含通配符的 glob.glob 字面量 ⇒ 射程不可核对')
        chans.append({'channel': rel.replace('\\', '/'), 'producer': rel.replace('\\', '/'),
                      'patterns': pats, 'keep': keep, 'knob': keeper})
    return chans


def _family_of(name, chans):
    base = os.path.basename(str(name))
    for ch in chans:
        if any(fnmatch.fnmatch(base, p) for p in ch['patterns']):
            return ch['channel']
    return None


def retention_reconciliation(keeps, max_age_h, hours_per_backup=24.0):
    """把"份数上限"换算成"最老可存件小时数"，再与资格窗口对账（两把尺必须见面，不许各说各话）。"""
    if not keeps or not max_age_h:
        return {'verdict': 'unverified', 'reason': '份数尺或资格窗口取不到 ⇒ 不对账（不判健康）',
                'kept_hours_estimate': None}
    kept = max(keeps) * float(hours_per_backup)
    return {
        'verdict': 'aligned' if kept <= max_age_h else 'gap:keep_outlives_eligibility',
        'kept_hours_estimate': kept,
        'max_age_h': max_age_h,
        'estimate_basis': f'每通道最多一日一份（假设值 {hours_per_backup}h/份，未实测生产节奏）',
        'reason': (f'最大份数尺 {max(keeps)} 份 ≈ {kept:.0f}h，'
                   f'{"落在" if kept <= max_age_h else "远超"}资格窗口 {max_age_h}h ⇒ '
                   + ('留量与可载回窗口同档' if kept <= max_age_h else
                      '账面留量再大也不等于有得载：淘汰按份数、资格按小时，二者从不对账')),
    }


def retention_report(root=None, now=None, records=None, hours_per_backup=24.0):
    """只读的跨族保留体检：人口 / 每族件数与最老件 / 族外具名清单 / 三把尺对账结论。"""
    chans = retention_channels(root)
    recs = list(records) if records is not None else list(
        backup_records(root=root, now=now))
    fam = {c['channel']: {'channel': c['channel'], 'patterns': c['patterns'], 'keep': c['keep'],
                          'count': 0, 'oldest_h': None, 'eligible': 0} for c in chans}
    unmanaged = []
    for r in recs:
        owner = _family_of(r['name'], chans)
        if owner:
            cell = fam[owner]
            cell['count'] += 1
            cell['eligible'] += 1 if r['state'] == 'eligible' else 0
            if r['age_h'] is not None and (cell['oldest_h'] is None or r['age_h'] > cell['oldest_h']):
                cell['oldest_h'] = r['age_h']
        else:
            unmanaged.append({'name': r['name'], 'age_h': r['age_h'], 'state': r['state']})
    unmanaged.sort(key=lambda x: -(x['age_h'] or 0))
    eligible = sum(1 for r in recs if r['state'] == 'eligible')
    rep = {
        'population': len(recs),
        'eligible': eligible,
        'families': [fam[k] for k in sorted(fam)],
        'unmanaged': unmanaged,
        'unmanaged_count': len(unmanaged),
        'channels': chans,
        'knobs': {c['channel']: c['keep'] for c in chans} | {'max_age_h': MAX_AGE_H},
        'reconciliation': retention_reconciliation([c['keep'] for c in chans], MAX_AGE_H,
                                                  hours_per_backup=hours_per_backup),
        'status': 'ok' if recs else 'unverified',
    }
    if not recs:
        rep['reconciliation'] = {'verdict': 'unverified', 'reason': '人口为零：`backups/` 是 gitignore 的本机物，'
                                                                   '量不到不等于健康', 'kept_hours_estimate': None}
    return rep


def print_retention(rep):
    """体检读数一律走 stdout 的固定形状（门禁与人都按同一形状读，禁散文）。"""
    print(f'保留体检：人口 {rep["population"]} 件 / 够格 {rep["eligible"]} 件 / '
          f'族外 {rep["unmanaged_count"]} 件（status={rep["status"]}）')
    for f in rep['families']:
        old = '-' if f['oldest_h'] is None else f'{f["oldest_h"]:.1f}h'
        print(f'   通道 {f["channel"]} 模式={f["patterns"]} keep={f["keep"]} ⇒ '
              f'在册 {f["count"]} 件 / 最老 {old} / 够格 {f["eligible"]}')
    for u in rep['unmanaged'][:10]:
        print(f'   族外无主 {u["name"]} 龄={u["age_h"]:.1f}h 资格态={u["state"]}')
    rec = rep['reconciliation']
    print(f'   三把尺 {rep["knobs"]} ⇒ verdict={rec["verdict"]}；{rec["reason"]}')
    return 0 if rep['population'] else 1


# ══════════ 复核后处置通道（第 39 轮 H161-H166）：体检只报，这里才是"人看过、确认删"的那个出口 ══════════
# 动因（在册 M115/M117，第 37—38 轮登记）：`retention_report()` 是**只读**的，EXTRA/族外件"只报不删"
#   已被写成判据（第 53 项 + L4 S45），但全仓没有任何出口承接"人已复核、确认该删"——
#   于是那 5 件族外无主件（`PH2-*` 4 + `ican-db-before-cisim-*` 1，开发面实测最老 1631.9h ≈ 68 天）
#   永久停在原地，而"永久停着"和"有人看过并决定留它"在盘上长得一模一样，后人无法区分。
# 上游对照（本轮 `gh api` 取回原文，取数时刻 2026-09-28 08:11—08:19 +0800，peer 一律 owner/name）：
#   `pre-commit/pre-commit` `pre_commit/commands/gc.py:18-23/:36-45` —— 候选集先建全集，再逐项
#     **证明它被引用**才从可疑名单里 discard；证明不了的（`:36` 没克隆、`:40` 清单读失败）直接 return，
#     ⇒ 不删证明不了的东西。`:86-91` 出口按 key 逐条 `DELETE`+`rmtree`，`:93` 的计数只用于事后播报。
#   `borgbackup/borg` `src/borg/archiver/repo_delete_cmd.py:32-38` —— `looks_like_borg_store()` 不过
#     就 raise「so borg does not destroy it」；`:86-96` 必须逐字输入 `YES` 且 `retry=False`。
#     `prune_cmd.py:352-367` 没有 keep 策略即 CommandError（入口硬门禁，不"反正少删点"）。
#   `restic/restic` `cmd/restic/cmd_forget.go:243-252` —— 无策略拒；要删全部必须先给非空 filter
#     作为**作用域证明**（`--unsafe-allow-remove-all` 单独命名）。`cmd_prune.go:185-191` 危险动作
#     要求把仓库身份 ID 原样回显。`:317-347` 按 ID 逐个删，单个失败进 failedSnIDs 不回滚整体。
#   `cli/cli` `pkg/cmd/repo/delete/delete.go:60-67` —— **对象没被明确指认时连 `--yes` 都作废**；
#     `internal/prompter/huh_prompter.go:210-227` 确认凭据就是 `owner/name` 全名逐字输入。
#   `git/git` `builtin/prune.c:172` 不给 `--expire` 时 `expire = TIME_MAX`（默认永不过期，必须显式给龄期）；
#     `:84-108` 可达性 + 龄期两重前置通过，才 unlink **那一个**对象；dry-run 走同一函数体只少最后一步。
# 设计取向：入口只收"体检看过的名字"（孤儿入参一律拒）；出口一次一件、按 ticket 绑定字节，
#   删除前重取 sha256 对账（TOCTOU）；台账写在 backups/ **之外**且分两阶段（claimed→deleted，
#   对齐 borg 的 soft-delete→compact），因为"删了没记录"比"没删"更难发现。

DISPOSITION_SCHEMA = 'ican-backup-disposition-v1'
DISPOSITION_LEDGER_NAME = 'BACKUP-DISPOSITION.jsonl'
DISPOSITION_PENDING_NAME = 'BACKUP-DISPOSITION-PENDING.jsonl'
DISPOSITION_MAX_ENTRIES = 200
TICKET_TTL_S = 900          # 复核与执行不得隔任意久（git `prune` 的 TIME_MAX 反面：这里必须限时）
# token 只绑**对象身份**、不含 issued_ts：绑了就等于人拿到凭据也无法把它带回删除出口。
# 时效单独由 pending 记录上的 issued_ts 管 —— 身份轴与时效轴分开，才各自可核。
TOKEN_FIELDS = ('schema', 'name', 'sha256', 'bytes', 'age_h', 'state', 'channel', 'patterns')
CLAIM_FIELDS = ('claim_by', 'claim_reason')
TICKET_FIELDS = TOKEN_FIELDS + ('issued_ts', 'ttl_s', 'token') + CLAIM_FIELDS
MIN_REASON_CHARS = 20       # 与本仓豁免台账同一把尺：一句话不算复核
LOCK_NAME = 'BACKUP-DISPOSITION.lock'
HOLDER_NAME = 'BACKUP-DISPOSITION.holder'
# 被拒过清理的非法侧件**原文**归档处（第 42 轮 M133）：清掉一个没人认领的归因件，
# 本身也是不可逆动作 ⇒ 必须先留证再删，且留证落在被处置面之外。
HELD_NAME = 'BACKUP-DISPOSITION-HELD.jsonl'
# 处置通道的文件种类（门禁第 55 项按这张名册取路径并双向对账，不再写死"四类"）
DISPOSITION_PATH_KINDS = ('lock', 'holder', 'ledger', 'pending', 'held')
LOCK_STALE_S = 600          # 归因侧件超过这个龄期才算"陈旧"（与凭据 TTL 同量级，两条轴各留各的数）
LOCK_WAIT_S = 0.0           # 默认不等待：拿不到就拒，不排队把别人的删除等到自己头上
_LOCKS: dict = {}           # 路径 -> (FileLock, 重入深度)；实测同进程不同实例会互撞，故缓存单例


class DispositionError(RuntimeError):
    """凭据不成立 ⇒ 一个字节都不动。与 RetentionError 同一取向：读不出就是没测到，不是通过。"""


def _face(root):
    return os.path.basename(os.path.abspath(root or BASE_DIR))


def _outside_root(root, name):
    """复核件必须落在被处置面之外，否则这个通道会擦掉自己的账。

    一手教训（第 28 轮账号注销）：不可逆动作的留痕记在被处置主体名下，而该表自身在删除范围内
    ⇒ 实测 0 条留痕。台账若写进 `backups/`，族外件删完顺带把"谁授权删的"一起带走。
    不变量两条，第 39 轮 S46 首跑实测出第一条**永不相等**（等于一条不可达的死守卫）⇒ 补第二条：
      1) 留痕路径不得位于本面 `<root>/backups/` 之下（数学上这条在纯 dirname 推导下不会命中，
         留着当兜底）；
      2) **root 自己不许嵌在某个 `backups/` 里层** —— 那才是真会发生的越界：把面指进
         `backups/nested` 时，留痕就落在被处置面内部，删完连"谁授权的"一起带走。
    """
    absroot = os.path.abspath(root)
    bdir = os.path.join(absroot, 'backups')
    path = os.path.abspath(os.path.join(os.path.dirname(bdir), name))
    # normcase 只用于**比较**，绝不能当返回值（Windows 下它会把盘符与目录整体小写化 ——
    # 第 39 轮 S46 实测返回了 'c:\\users\\...'，调用方拿到的是一个大小写被改写过的路径）
    if os.path.normcase(path).startswith(os.path.normcase(bdir) + os.path.sep):
        raise DispositionError(f'{name} 解析进了被处置面 {bdir} 内 ⇒ 拒绝（留痕不得与被删对象同目录）')
    parts = os.path.normcase(absroot).split(os.path.sep)
    if 'backups' in parts[:-1]:
        raise DispositionError(f'面 {absroot} 自己嵌在 backups/ 里层 ⇒ 留痕会落在被处置面内，'
                               f'拒绝把 {name} 写到那里')
    return path


def disposition_ledger_path(root=None):
    return _outside_root(root or BASE_DIR, DISPOSITION_LEDGER_NAME)


def disposition_pending_path(root=None):
    return _outside_root(root or BASE_DIR, DISPOSITION_PENDING_NAME)


def disposition_held_path(root=None):
    return _outside_root(root or BASE_DIR, HELD_NAME)


def read_jsonl(path):
    """逐行读；坏行单独计数，不静默丢（append-only 面一定会遇到半行）。"""
    if not os.path.isfile(path):
        return [], 0
    rows, broken = [], 0
    with open(path, encoding='utf-8', newline='') as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            try:
                rows.append(json.loads(s))
            except ValueError:
                broken += 1
    return rows, broken


def append_jsonl(path, entry):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + '\n')
        f.flush()
        os.fsync(f.fileno())
    return path


def cap_jsonl(path, limit=DISPOSITION_MAX_ENTRIES):
    """封顶：claimed（未消费的凭据）永不先掉，只淘汰最老的终态行。"""
    rows, _b = read_jsonl(path)
    if len(rows) <= limit:
        return 0
    pinned = [r for r in rows if r.get('outcome') == 'claimed']
    free = [r for r in rows if r.get('outcome') != 'claimed']
    room = max(0, limit - len(pinned))
    dropped = max(0, len(free) - room)
    if dropped:
        # 留**最新** room 条：写 free[room:] 会反过来保住最老的一批（第 39 轮 S46 首跑实测
        # 252 行未被压回 —— 方向写反，与第 33 轮 backup(源,目标) 写反同族）
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            for r in (free[-room:] if room else []) + pinned:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n')
    return dropped


def _canonical_identity(ticket):
    body = {k: ticket[k] for k in TOKEN_FIELDS if k in ticket}
    return json.dumps(body, ensure_ascii=False, sort_keys=True).encode('utf-8')


def ticket_token(ticket):
    import hashlib
    return hashlib.sha256(_canonical_identity(ticket)).hexdigest()[:16]


def _norm_name(name):
    """入站形状守卫：只收裸 basename。带分隔符 / `..` / 绝对路径 = **孤儿入参**，一律拒。"""
    s = str(name or '')
    if (not s or s != os.path.basename(s) or os.path.sep in s
            or (os.path.altsep and os.path.altsep in s) or '..' in s):
        return None
    return s


def _find_pending(root, name):
    """按名字取**最后一条** pending；状态机是 claimed → consumed/aborted，末条才算现状。

    旧写法"从后往前找第一条 outcome=claimed"会让已消费的老凭据被重新翻出来（S46 实测二次同名
    处置走的是 gone_now 而不是"无未消费凭据"，等于状态机没闭合）。
    """
    rows, _b = read_jsonl(disposition_pending_path(root))
    for r in reversed(rows):
        if r.get('name') == name:
            return r if r.get('outcome') == 'claimed' else None
    return None


def make_ticket(name, root=None, now=None, records=None, chans=None, issue=True):
    """只读产一张复核凭据；`issue=True` 同时落一张 pending（"人看过"这件事必须在盘上有形）。

    四类拒绝（入口硬门禁，对偶 `borg repo_delete_cmd.py:32-38` 与 `restic cmd_forget.go:243-252`）：
      孤儿入参       —— 指认不出是一个裸备份名
      不在体检人口里 —— `backup_records()` 没见过它 ⇒ 未经复核的对象不得进本通道
      幽灵记录       —— 有卡无件，那属证据卡面，不在本通道
      仍够格可载回   —— 它是恢复退路，删它等于自己把退路当垃圾
    """
    root = root or BASE_DIR
    now = now or datetime.now(UTC)
    bare = _norm_name(name)
    if bare is None or not bare.endswith(BACKUP_EXTS):
        raise DispositionError(f'孤儿入参：{name!r} 不是可指认的裸备份文件名 ⇒ 一个字节都不动')
    chans = chans if chans is not None else retention_channels(root)
    recs = list(records) if records is not None else list(backup_records(root=root, now=now))
    rec = next((r for r in recs if r['name'] == bare), None)
    if rec is None:
        raise DispositionError(f'{bare} 不在体检人口里 ⇒ 未经复核的对象不得进本通道')
    if rec['state'] == 'manifest_ghost':
        raise DispositionError(f'{bare} 是有卡无件的幽灵记录 ⇒ 先处理证据卡面，本通道不接')
    if rec['state'] == 'eligible':
        raise DispositionError(f'{bare} 仍够格可载回（{rec["why"] or "eligibility=ok"}）⇒ 拒，它是退路')
    if not os.path.isfile(rec['path']):
        raise DispositionError(f'{bare} 人口在册但取不到文件 ⇒ 判没测到')
    try:
        sha = sha256_file(rec['path'])
    except OSError as exc:
        raise DispositionError(f'{bare} 取 sha256 失败：{type(exc).__name__} ⇒ 判没测到') from None
    owner = _family_of(bare, chans)
    fam = next((c for c in chans if c['channel'] == owner), None)
    ticket = {'schema': DISPOSITION_SCHEMA, 'name': bare, 'sha256': sha, 'bytes': rec['bytes'],
              'age_h': rec['age_h'], 'state': rec['state'], 'channel': owner,
              'patterns': list(fam['patterns']) if fam else [],
              'issued_ts': now.isoformat(), 'ttl_s': TICKET_TTL_S,
              'claim_by': '', 'claim_reason': ''}
    ticket['token'] = ticket_token(ticket)
    if owner is None:
        ticket['note'] = ('族外无主：没有任何生产者声明过这个形态 ⇒ 处置必须由人署名认领'
                          '（对偶 restic 要删全部前必须先给非空作用域证明）')
    if issue:
        with disposition_lock(root=root):
            body = {k: ticket[k] for k in TICKET_FIELDS if k in ticket}
            append_jsonl(disposition_pending_path(root), body | {'outcome': 'claimed',
                                                                 'face': _face(root)})
            cap_jsonl(disposition_pending_path(root))
            write_holder(root=root, token=ticket['token'], kind='ticket')
    return ticket


def verify_ticket(ticket, root=None, now=None):
    """四核：形状 / token 与身份自洽 / 时效 / 实盘字节对账（TOCTOU）。返回 (ok, kind, why)。

    认领人与理由**不进 token**（那是人这一侧的输入，在 dispose 入口单独验非空与长度）。
    """
    root = root or BASE_DIR
    now = now or datetime.now(UTC)
    if not isinstance(ticket, dict):
        return False, 'shape', '凭据必须是单个对象：批量处置不在本通道（按名字删，不按计数删）'
    missing = [k for k in TICKET_FIELDS if k not in ticket]
    if missing:
        return False, 'shape', f'凭据缺字段 {missing}'
    if ticket['schema'] != DISPOSITION_SCHEMA:
        return False, 'shape', f"schema 不是 {DISPOSITION_SCHEMA}：{ticket['schema']!r}"
    bare = _norm_name(ticket['name'])
    if bare is None:
        return False, 'orphan_arg', f"名字 {ticket['name']!r} 不是裸 basename"
    if ticket_token(ticket) != ticket['token']:
        return False, 'token_mismatch', 'token 与身份字段不符 ⇒ 凭据被动过，或对象身份已变'
    if len(str(ticket.get('claim_reason') or '')) < MIN_REASON_CHARS:
        return False, 'reason_too_short', f'认领理由不足 {MIN_REASON_CHARS} 字：一句话不算复核'
    if not str(ticket.get('claim_by') or '').strip():
        return False, 'reason_too_short', '认领人为空 ⇒ 无主的删除等于没人负责'
    try:
        issued = datetime.fromisoformat(str(ticket['issued_ts']))
    except ValueError:
        return False, 'shape', f"issued_ts 不可解析：{ticket['issued_ts']!r}"
    held_s = (now - issued).total_seconds()
    if held_s > float(ticket['ttl_s']):
        return False, 'expired', f'凭据已持有 {held_s:.0f}s > TTL {ticket["ttl_s"]}s ⇒ 重新体检'
    path = os.path.join(os.path.abspath(root), 'backups', bare)
    if not os.path.isfile(path):
        return False, 'gone_now', f'{bare} 现在不存在 ⇒ 不把"已经没了"报成处置成功'
    try:
        if sha256_file(path) != ticket['sha256']:
            return False, 'token_mismatch', '实盘字节与凭据不符 ⇒ 复核对象已被替换'
    except OSError as exc:
        return False, 'unreadable', f'取 sha256 失败：{type(exc).__name__}'
    return True, 'ok', (f'{bare} sha={ticket["sha256"][:12]} 龄={ticket["age_h"]}h '
                        f'归属={ticket["channel"] or "族外无主"}')


def _stamp(ticket, outcome, extra=None):
    e = {k: ticket.get(k) for k in TOKEN_FIELDS}
    e.update({'outcome': outcome, 'ts': datetime.now(UTC).isoformat(),
              'issued_ts': ticket.get('issued_ts'), 'token': ticket.get('token'),
              'claim_by': ticket.get('claim_by'), 'claim_reason': ticket.get('claim_reason'),
              'face': ticket.get('_face') or _face(BASE_DIR)})
    if extra:
        e.update(extra)
    return e


def _dispose_probe(root, bare, confirm, claim_by, claim_reason, now):
    """`dispose(dry_run=True)` 的实现体：**不取锁、不落账、不删**，但判定与真跑同源。

    锁态取自 `stale_lock_report`（它会瞬时试拿再放，不是长期持锁），所以撞锁现场这一档照样出结论
    —— 这正是 M128 要补的出口：以前撞锁只剩"等"或"破别人的锁"两个动作。
    """
    bdir = os.path.join(os.path.abspath(root), 'backups')
    led_p = disposition_ledger_path(root)
    pen_p = disposition_pending_path(root)

    def _names():
        return sorted(f for f in os.listdir(bdir) if f.endswith(BACKUP_EXTS)) \
            if os.path.isdir(bdir) else []

    def _counts():
        return (len(read_jsonl(led_p)[0]), len(read_jsonl(pen_p)[0]))

    rep = stale_lock_report(root=root, now=now)
    before, led_before = _names(), _counts()
    out = {'dry_run': True, 'name': bare, 'removed': 0, 'lock': rep['verdict'],
           'lock_token': rep['token'], 'holder_age_s': rep['holder_age_s'],
           'population_before': len(before), 'ledger_before': led_before,
           'would_free_bytes': 0, 'face': _face(root)}
    pending = _find_pending(root, bare)
    if pending is None:
        out.update(verdict='refuse:no_ticket',
                   why='没有未消费的复核凭据 ⇒ 真跑也会拒；先 --ticket 取一张，人看过再来')
    else:
        ticket = dict(pending)
        ticket['claim_by'] = claim_by or ''
        ticket['claim_reason'] = claim_reason or ''
        ok, kind, why = verify_ticket(ticket, root=root, now=now)
        if not ok:
            out.update(verdict=f'refuse:{kind}', why=why)
        elif str(confirm) != ticket['token']:
            out.update(verdict='refuse:confirm_mismatch',
                       why='confirm 与 token 不符 ⇒ 真跑会判"未复核"，这里如实报拒')
        else:
            out.update(verdict='would_dispose', why='', sha256=ticket['sha256'],
                       channel=ticket['channel'], age_h=ticket['age_h'],
                       would_free_bytes=ticket['bytes'])
    after, led_after = _names(), _counts()
    if after != before or led_after != led_before:
        raise DispositionError(f'dry-run 却动了盘：名字 {len(before)}→{len(after)}，'
                               f'台账/意图 {led_before}→{led_after} ⇒ 判缺陷，不判通过')
    out.update(population_after=len(after), ledger_after=led_after)
    return out


def dispose(name, confirm=None, claim_by=None, claim_reason=None, root=None, now=None,
            dry_run=False):
    """唯一真删出口：一次一件，删的是**凭据绑定的那一个名字**；先记意图再删，两态都落账。

    `confirm` 必须逐字等于 pending 那张凭据的 token（`cli/cli` `huh_prompter.go:210-227` 的
    `requiredValue` 取向：确认凭据就是对象标识本身，不是"我知道"）。没有 `--yes` 这类无条件跳过
    （`delete.go:60-67`：对象没被明确指认时，连显式授权都作废）。

    `dry_run=True` 是撞锁时的只读出口（第 41 轮 M128；restic 只允许 `--no-lock` 配 `--dry-run`，
    我方没有 `--no-lock`，于是补上正向半边：**想知道"这一件会不会删成"不必先拿到锁**）。
    只读档不是免检档——每一条拒因照跑；差别只在不取锁、不落账、不删，且返回前按
    "名字集合 + 台账行数 + pending 行数"三面对账，动了盘就判缺陷。
    """
    root = root or BASE_DIR
    now = now or datetime.now(UTC)
    if isinstance(name, (list, tuple)):
        raise DispositionError(f'一次只处置一件，收到 {len(name)} 件 ⇒ 拒（按名字不按计数）')
    if confirm is None:
        raise DispositionError('缺 --confirm：本通道没有"默认删"，非交互环境也不隐式降级')
    bare = _norm_name(name)
    if bare is None:
        raise DispositionError(f'孤儿入参：{name!r} ⇒ 一个字节都不动')
    if dry_run:
        return _dispose_probe(root, bare, confirm, claim_by, claim_reason, now)
    # 锁必须裹住"读 pending → 核凭据 → 删 → 落账"整段（对偶 pre-commit `gc.py:62` 的
    # with 范围）。原先先读 pending 再取锁：他人持锁时报的是"没有未消费的凭据"，
    # 把竞态伪装成凭据问题 —— 本轮 S47 争锁腿实测抓到并据此换位。
    with disposition_lock(root=root, wait_s=LOCK_WAIT_S):
        pending = _find_pending(root, bare)
        if pending is None:
            raise DispositionError(f'{bare} 没有未消费的复核凭据 ⇒ 先 --ticket 取一张，人看过再来')
        ticket = dict(pending)
        ticket['claim_by'] = claim_by or ''
        ticket['claim_reason'] = claim_reason or ''
        ok, kind, why = verify_ticket(ticket, root=root, now=now)
        if not ok:
            raise DispositionError(f'凭据不成立（{kind}）：{why} ⇒ 一个字节都不动')
        if str(confirm) != ticket['token']:
            raise DispositionError('confirm 与 token 不符 ⇒ 视为未复核，拒绝执行')
        bdir = os.path.join(os.path.abspath(root), 'backups')
        ticket['_face'] = _face(root)
        write_holder(root=root, token=ticket['token'], kind='dispose')
        before = sorted(f for f in os.listdir(bdir) if f.endswith(BACKUP_EXTS))
        expect_rest = [f for f in before if f != bare]
        append_jsonl(disposition_ledger_path(root), _stamp(ticket, 'approved'))
        err = None
        try:
            os.remove(os.path.join(bdir, bare))
        except OSError as exc:                                # 失败也必须落账，不许静默吞
            err = f'{type(exc).__name__}: {exc}'
        after = sorted(f for f in os.listdir(bdir) if f.endswith(BACKUP_EXTS))
        if err is None and after != expect_rest:
            err = f'删后人口 != "只少那一个"（期望 {len(expect_rest)} 实得 {len(after)}）'
        append_jsonl(disposition_ledger_path(root),
                     _stamp(ticket, 'deleted' if err is None else 'failed',
                            {'error': err, 'population_before': len(before),
                             'population_after': len(after)}))
        body = {k: ticket[k] for k in TICKET_FIELDS if k in ticket}
        append_jsonl(disposition_pending_path(root),
                     body | {'outcome': 'consumed' if err is None else 'aborted',
                             'face': _face(root)})
        cap_jsonl(disposition_ledger_path(root))
        cap_jsonl(disposition_pending_path(root))
    if err is not None:
        raise DispositionError(f'处置失败：{err}')
    return {'name': bare, 'sha256': ticket['sha256'], 'channel': ticket['channel'],
            'population_before': len(before), 'population_after': len(after),
            'removed': 1, 'claim_by': ticket['claim_by'],
            'ledger': disposition_ledger_path(root)}


def plan_disposal(root=None, now=None, records=None):
    """dry-run：可指认与不可处置两张清单都摆出来（对齐 borg `--list-pruned` 与 `--list-kept` 分列）。

    与 `dispose()` 的差别只在最后一步 —— 本函数**不删、也不发凭据**（第 54 项按 AST 钉这条只读承诺）。
    """
    root = root or BASE_DIR
    now = now or datetime.now(UTC)
    chans = retention_channels(root)
    recs = list(records) if records is not None else list(backup_records(root=root, now=now))
    cands, refused = [], []
    for r in recs:
        if r['state'] in ('eligible', 'manifest_ghost'):
            refused.append({'name': r['name'], 'kind': r['state'], 'why': r['why']})
            continue
        cands.append({'name': r['name'], 'age_h': r['age_h'], 'bytes': r['bytes'],
                      'state': r['state'], 'channel': _family_of(r['name'], chans)})
    cands.sort(key=lambda x: -(x['age_h'] or 0))
    pend, _b = read_jsonl(disposition_pending_path(root))
    held = [p for p in pend if p.get('outcome') == 'claimed' and p.get('face') == _face(root)]
    return {'population': len(recs), 'candidates': len(cands), 'refused': len(refused),
            'orphans': sum(1 for c in cands if c['channel'] is None),
            'bytes_candidates': sum(int(c['bytes'] or 0) for c in cands),
            'pending_tickets': len(held), 'ttl_s': TICKET_TTL_S,
            'candidate_list': cands, 'refused_list': refused,
            'status': 'ok' if recs else 'unverified'}


def print_disposal_plan(plan):
    print(f'处置体检（dry-run，未删任何件、未发凭据）：人口 {plan["population"]} / 可指认 '
          f'{plan["candidates"]}（族外无主 {plan["orphans"]}，合计 {plan["bytes_candidates"]} B）'
          f'/ 不可处置 {plan["refused"]} / 未消费凭据 {plan["pending_tickets"]} 张'
          f'（TTL {plan["ttl_s"]}s，status={plan["status"]}）')
    for c in plan['candidate_list'][:10]:
        old = '-' if c['age_h'] is None else f'{c["age_h"]:.1f}h'
        print(f'   候选 {c["name"]} 龄={old} 归属={c["channel"] or "族外无主"} 资格态={c["state"]}')
    for r in plan['refused_list'][:10]:
        print(f'   不处置 {r["name"]} 因={r["kind"]}｜{r["why"]}')
    print(f'   要真删：--ticket <名字> 取凭据 → 人看过 → --dispose <名字> --confirm <token>'
          f' --claim-by <谁> --claim-reason "<≥{MIN_REASON_CHARS} 字>"（一次一件，不按计数）')
    return 0 if plan['population'] else 1


# ══════════ 处置通道的独占锁（第 40 轮 H171-H177 / 在册 M120）══════════
# 动因（本轮开场实测，取数面 = restore_db.py 全文 grep "filelock|msvcrt|flock|acquire_lock|LOCK"）：
#   命中 **0 处** ⇒ 第 39 轮那条通道的"签发 → 复核 → 删除"三段之间没有任何跨进程互斥。
#   真实暴露面：签发凭据后 TTL 900s 内，另一会话（本机确实并存）可 `--plan-disposal` 之后
#   对同一名字再签一张，或更糟——在我复核期间跑 `backup_db.cleanup()` 把它淘汰掉；
#   第 39 轮的 sha 对账只拦得住"内容被换"，拦不住"被合法淘汰后重名同内容"这类竞态。
# 上游对照（本轮 `gh api` 当日实取，取数时刻见报告 §7；peer 一律 owner/name）：
#   `pre-commit/pre-commit` `pre_commit/commands/gc.py:62`
#     `with store.exclusive_lock(), store.connect() as db:` —— 回收全程持独占锁，且是 `with`
#     （异常路径必然释放），不是手调 acquire/release。
#   `restic/restic` `cmd/restic/cmd_forget.go:189-191`
#     `if gopts.NoLock && !opts.DryRun { return errors.Fatal("--no-lock is only applicable in
#     combination with --dry-run") }`；`cmd_prune.go:179` `openWithExclusiveLock(...)`
#     —— **免锁只允许发生在只读态**，这条形状区分正是我要的对偶判据。
#   `borgbackup/borg` `src/borg/archiver/repo_delete_cmd.py:47-96`
#     破确认要求逐字回显身份；本处的"残留锁"同构 ⇒ 必须回交 holder 的 pid+token 才允许清。
# 本机实测（filelock 4.0.4，取数 2026-09-28 12:4x +0800，四条都影响设计，不是文档转述）：
#   ① 跨进程互斥成立：子进程持锁时父进程 `acquire(timeout=0.5)` ⇒ BLOCKED；
#   ② 进程被 kill 后 OS 侧锁自动释放（再 acquire ⇒ ACQUIRED），且锁文件被清掉；
#   ③ 但**归因侧件残留**（holder.txt 里仍是死者 pid 24548）⇒ 归因不能靠锁文件本身；
#   ④ `filelock.Timeout` **没有 `locked_by` 属性** ⇒ 想从异常里读持锁者 pid 是行不通的，
#      谁持锁只能由我自己写侧文件。（这条是探针量出来的：先写 `e.locked_by.pid` 直接
#      AttributeError，与"判据第一版的输出是待读样本"同族——不探就会照着文档假设写腿。）


def disposition_lock_path(root=None):
    """锁文件路径：与被删面、与台账、与 pending 三者互不相同（同 `_outside_root` 的守卫）。"""
    return _outside_root(root or BASE_DIR, LOCK_NAME)


def disposition_holder_path(root=None):
    """归因侧件：`Timeout` 给不出持锁者，所以"谁在删"必须自己落盘（③ 的直接后果）。"""
    return _outside_root(root or BASE_DIR, HOLDER_NAME)


def _lock_singleton(path, timeout_s):
    """一个路径一个 FileLock 实例 + 深度计数。

    实测 ① 的另一半：同一实例可重入，但**同进程不同实例会互相对撞**（先前探针
    `b.acquire(timeout=0.3)` 在 a 持有时抛 Timeout）⇒ 若 `dispose()` 里再套一层取锁，
    会自己把自己锁死到超时。所以这里缓存实例并计深度，只在 0 层真释放。
    """
    import filelock
    ent = _LOCKS.get(path)
    if ent is None:
        ent = (filelock.FileLock(path, timeout=0), 0)
        _LOCKS[path] = ent
    return ent


def _holder_doc(pid=None, token=None, kind=None):
    return {'pid': pid if pid is not None else os.getpid(), 'token': token or '',
            'kind': kind, 'ts': datetime.now(UTC).isoformat(), 'face': _face(BASE_DIR)}


# 归因侧件的名册（第 41 轮）：**只收真在发的种**——`_s55_holder_kinds` 按调用点双向对账，
# 多登记一个没人发的种＝对空气豁免（第 40 轮 `write_holder(kind='disposal')` 默认值就是这么个死字）。
HOLDER_KINDS = frozenset({'ticket', 'dispose'})
TOKEN_HEX_LEN = 16            # `ticket_token` 的复算长度，改一处须同改此数


def validate_holder_doc(doc):
    """归因侧件的字段级校验。形状借 `kopia/kopia` `repo/format/upgrade_lock_intent.go:64-79`
    的 `Validate()`：身份非空 ∧ 时间已设 ∧ 枚举在册，三条各自点名——
    一份"解析得开但字段是垃圾"的侧件，比一份坏 JSON 更危险，因为它会被当成可清的残局。
    """
    if not isinstance(doc, dict):
        return ['侧件不是对象 ⇒ 无法逐字段核']
    if 'unreadable' in doc:
        return ['侧件读不出（JSON 层就坏了）']
    probs = []
    pid = doc.get('pid')
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        probs.append(f'pid 不是正整数：{pid!r}')
    tok = doc.get('token')
    if not (isinstance(tok, str) and len(tok) == TOKEN_HEX_LEN
            and all(c in '0123456789abcdef' for c in tok.lower()) and tok == tok.lower()):
        probs.append(f'token 不是 {TOKEN_HEX_LEN} 位小写十六进制：{tok!r}')
    kind = doc.get('kind')
    if kind not in HOLDER_KINDS:
        probs.append(f'kind={kind!r} 不在名册 {sorted(HOLDER_KINDS)} ⇒ 未知写入者，不替它定性质')
    ts = doc.get('ts')
    try:
        parsed = datetime.fromisoformat(str(ts))
        if parsed.tzinfo is None:
            probs.append(f'ts 不带时区：{ts!r} ⇒ 龄期与陈旧阈值不可比')
    except (TypeError, ValueError):
        probs.append(f'ts 解析失败：{ts!r}')
    return probs


def validate_lock_config(wait_s=None, stale_s=None):
    """两个锁预算必须**成对**成立（借 kopia `:77-79` 的 `status-poll-interval <= io-drain-timeout`）：
    等待窗口长于陈旧阈值 ⇒ 工具会去等一把自己判为"陈旧"的锁，两条轴互相打脸。
    """
    wait_s = LOCK_WAIT_S if wait_s is None else wait_s
    stale_s = LOCK_STALE_S if stale_s is None else stale_s
    probs = []
    if not isinstance(wait_s, (int, float)) or wait_s < 0:
        probs.append(f'LOCK_WAIT_S 不是非负数：{wait_s!r}')
    if not isinstance(stale_s, (int, float)) or stale_s <= 0:
        probs.append(f'LOCK_STALE_S 不是正数：{stale_s!r}')
    if wait_s > stale_s:
        probs.append(f'LOCK_WAIT_S={wait_s} > LOCK_STALE_S={stale_s} ⇒ 会等一把自己判陈旧的锁')
    return probs



def write_holder(root=None, token='', kind=None):
    """持锁期间写归因侧件；写失败或**字段不合格** ⇒ 拒绝继续（无归因的删除等于没人负责）。"""
    path = disposition_holder_path(root)
    doc = _holder_doc(token=token, kind=kind)
    bad = validate_holder_doc(doc)
    if bad:
        raise DispositionError('归因侧件不合格 ⇒ 拒绝在无有效归因下继续：'
                               '；'.join(bad))
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    body = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(body + '\n')
        f.flush()
        os.fsync(f.fileno())
    return path


def read_holder(root=None):
    """读归因侧件。缺/坏一律如实区分：`None` = 没有，`{'unreadable':...}` = 读不出。"""
    path = disposition_holder_path(root)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.loads(f.read().strip() or '{}')
    except (ValueError, OSError) as exc:
        return {'unreadable': f'{type(exc).__name__}', 'path': path}


def clear_holder(root=None):
    path = disposition_holder_path(root)
    if os.path.isfile(path):
        try:
            os.remove(path)
            return True
        except OSError:
            return False
    return False


@contextlib.contextmanager
def disposition_lock(root=None, wait_s=0.0):
    """处置期间的跨进程独占锁（对偶 `pre-commit gc.py:62` 的 `with store.exclusive_lock()`）。

    拿不到 ⇒ `DispositionError` 且**点名持锁者的 pid/token**（来自归因侧件，不来自异常），
    绝不降级成"没锁也继续"。`with` 形态本身保证失败路径也释放（第 55 项按 AST 钉）。
    """
    root = root or BASE_DIR
    path = disposition_lock_path(root)
    try:
        import filelock  # 延迟导入：restore_db 会被 init_db.py 在函数体内 import，
    except ImportError as exc:  # 顶层导入会把新依赖拖进 PythonAnywhere 的启动路径
        raise DispositionError(
            f'缺 filelock（{type(exc).__name__}）⇒ 拒绝无锁处置。装它或明确改设计，'
            '禁止退化成"没有锁也照删"') from None
    lk, depth = _lock_singleton(path, LOCK_WAIT_S)
    if depth > 0:                                   # 同实例可重入（实测），只加深度
        _LOCKS[path] = (lk, depth + 1)
        yield lk
        _LOCKS[path] = (lk, depth)
        return
    try:
        acquired = lk.acquire(timeout=wait_s)
    except filelock.Timeout:
        raise DispositionError(
            f'处置锁被占用（{path}）：持锁者 '
            f'{json.dumps(read_holder(root) or {"pid": "未知"}, ensure_ascii=False)}'
            ' ⇒ 等它结束；只想确认"这一件会不会删成"就加 --dry-run（不取锁的只读档）；'
            '确认对方已死且侧件残留，才用 --break-lock 逐字回交其 token；本轮不动任何字节') from None
    _LOCKS[path] = (lk, 1)
    if not acquired:                                # 库允许返回假值而不抛 ⇒ 不认"当拿到了"
        _LOCKS[path] = (lk, 0)
        raise DispositionError(f'acquire() 未抛错也没返回真值 ⇒ 判没测到，拒绝执行')
    try:
        yield lk
    finally:
        lk.release()
        _LOCKS[path] = (lk, 0)
        clear_holder(root)


def stale_lock_report(root=None, now=None):
    """残留归因读数：锁**能拿到**但侧件还在 ⇒ 上一次处置半途崩了（实测 ②③ 的组合）。"""
    root = root or BASE_DIR
    now = now or datetime.now(UTC)
    doc = read_holder(root)
    held_path = disposition_lock_path(root)
    if not doc:
        return {'holder': None, 'holder_age_s': None, 'lock_file_present': os.path.isfile(held_path),
                'verdict': 'clean', 'token': ''}
    if 'unreadable' in doc:
        return {'holder': doc, 'holder_age_s': None, 'lock_file_present': os.path.isfile(held_path),
                'verdict': 'unreadable', 'token': ''}
    bad = validate_holder_doc(doc)
    try:
        held_s = (now - datetime.fromisoformat(str(doc['ts']))).total_seconds()
    except (ValueError, KeyError, TypeError):
        held_s = None
    lk, _d = _lock_singleton(held_path, LOCK_WAIT_S)
    try:
        got = lk.acquire(timeout=0.0)
        lk.release()
    except Exception:
        got = False
    if not got:
        # 拿不到锁 ⇒ **有人正在临界区里**，诚实报 live，哪怕侧件字段是垃圾
        # （"谁在删"这个信号绝不能被侧件质量问题盖掉；但把 holder_problems 一并带出，
        #  归因缺陷不许因为状态正确就隐身）
        return {'holder': doc, 'holder_problems': bad, 'holder_age_s': None if held_s is None
                else round(held_s, 1), 'lock_file_present': os.path.isfile(held_path),
                'verdict': 'live', 'token': '' if bad else lock_break_token(doc)}
    if bad:
        # 第六态（第 41 轮）：OS 已放锁但侧件字段非法。此前它落到 `stale`（因 `ts` 取不到 ⇒ held_s=None），
        # 而 `stale` 是**允许破锁**的档 ⇒ 一份垃圾侧件等于一把无人认领却能被清掉的锁。
        return {'holder': doc, 'holder_problems': bad, 'holder_age_s': None,
                'lock_file_present': os.path.isfile(held_path), 'verdict': 'malformed', 'token': ''}
    v = 'stale' if (held_s is None or held_s > LOCK_STALE_S) else 'residual-recent'
    return {'holder': doc, 'holder_problems': [], 'holder_age_s': None if held_s is None
            else round(held_s, 1), 'lock_file_present': os.path.isfile(held_path),
            'verdict': v, 'token': lock_break_token(doc)}


def lock_break_token(holder):
    """破锁凭据 = 持锁者身份的复算值（对齐 restic 要求回显仓库 ID、gh 要求逐字 `owner/name`）。"""
    body = json.dumps({k: (holder or {}).get(k) for k in ('pid', 'token', 'kind', 'ts')},
                      ensure_ascii=False, sort_keys=True)
    import hashlib
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:12]


def break_stale_lock(root=None, confirm=None, now=None):
    """清残留归因侧件：必须逐字回交 holder token，且只允许在锁**已被 OS 释放**时做。

    锁还被人拿着 ⇒ 拒（那是在抢别人的现场，不是清自己的残局）。
    """
    root = root or BASE_DIR
    rep = stale_lock_report(root=root, now=now)
    if rep['verdict'] == 'clean':
        raise DispositionError('没有残留归因可清 ⇒ 不把"无事可做"报成成功')
    if rep['verdict'] == 'live':
        raise DispositionError('锁仍在被持有 ⇒ 拒绝破别人的现场（只允许清 OS 已释放的残留）')
    if rep['verdict'] == 'unreadable':
        raise DispositionError('归因侧件读不出 ⇒ 判没测到，不替你猜一个 token')
    if rep['verdict'] == 'malformed':
        raise DispositionError('归因侧件字段非法 ⇒ 拒破锁：'
                               f'{"；".join(rep.get("holder_problems") or [])}'
                               '。本工具不给它算 token、也不代删该文件：'
                               '先人工核对这份侧件是谁写的，确认无主后再按"人工处置"处理（该缺口在册）')
    if confirm is None:
        raise DispositionError('缺 --confirm：破锁要逐字回交持锁者 token，不接受无条件跳过')
    if str(confirm) != rep['token']:
        raise DispositionError(f'confirm 与 holder token 不符（期望 {rep["token"]}）⇒ 视为未复核')
    ok = clear_holder(root)
    return {'cleared': ok, 'was': rep['verdict'], 'holder_age_s': rep['holder_age_s'],
            'token': rep['token']}

def disposition_paths(root=None):
    """处置通道的五类文件路径（按名册取，不写死数量 ⇒ 加一类就必须在名册登记）。"""
    funcs = {'lock': disposition_lock_path, 'holder': disposition_holder_path,
             'ledger': disposition_ledger_path, 'pending': disposition_pending_path,
             'held': disposition_held_path}
    if set(funcs) != set(DISPOSITION_PATH_KINDS):
        raise DispositionError(f'路径名册与实现不符：名册={sorted(DISPOSITION_PATH_KINDS)} '
                               f'实现={sorted(funcs)} ⇒ 少一类是漏登记，多一类是死名册')
    return {k: funcs[k](root) for k in DISPOSITION_PATH_KINDS}


def read_holder_raw(root=None):
    """取侧件**原始字节**。malformed 的字段本就不可信，所以受控出口的凭据只能绑原文。"""
    path = disposition_holder_path(root)
    if not os.path.isfile(path):
        return None, 'absent'
    try:
        with open(path, 'rb') as f:
            return f.read(), 'present'
    except OSError as exc:
        return None, f'unreadable:{type(exc).__name__}'


def malformed_token(raw, root=None):
    """清非法侧件的凭据 = (面, 原文 sha256, 原文长度) 的复算值。

    与 `lock_break_token`（从 holder **字段**复算）刻意不同：字段非法正是本通道的入口条件，
    拿被污染的东西去算"我确认它被污染了"的凭据是循环论证 ⇒ 只能绑字节。
    """
    import hashlib
    body = json.dumps({'face': _face(root or BASE_DIR),
                       'sha256': hashlib.sha256(raw or b'').hexdigest(),
                       'bytes': len(raw or b'')}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:TOKEN_HEX_LEN]


def clear_malformed_holder(root=None, confirm=None, reason=None, now=None):
    """受控清理 malformed 归因侧件：**先归档原文，再删那一个文件**，不碰被处置面。

    四道拒因（对齐处置通道的取向，不做成 `--force`）：
      ① 现状必须是 `malformed`（clean 无事可做 / live 不许破别人现场 / stale 与 residual-recent
         走 `--break-lock` / unreadable 交人工核对）；
      ② `confirm` 必须逐字等于由**原文**复算的 token；
      ③ 理由长度 ≥ `MIN_REASON_CHARS`；
      ④ 写态必须在处置锁内，且锁内重读原文必须与签发凭据时一字不差。
    """
    import hashlib
    root = root or BASE_DIR
    rep = stale_lock_report(root=root, now=now)
    if rep['verdict'] != 'malformed':
        raise DispositionError(
            f"本通道只处理 malformed，现状是 {rep['verdict']!r} ⇒ 拒"
            '（各态各有出口：stale/residual-recent 用 --break-lock，live 不许动别人的现场）')
    raw, state = read_holder_raw(root)
    if raw is None:
        raise DispositionError(f'侧件原文取不到（{state}）⇒ 判没测到，不替你猜一个 token')
    token = malformed_token(raw, root)
    if confirm is None:
        raise DispositionError('缺 --confirm：清 malformed 侧件要逐字回交**由原文派生**的 token')
    if str(confirm) != token:
        raise DispositionError(f'confirm 与原文 token 不符（期望 {token}）⇒ 视为未复核，一个字节都不动')
    if not reason or len(str(reason).strip()) < MIN_REASON_CHARS:
        raise DispositionError(f'理由不足 {MIN_REASON_CHARS} 字 ⇒ 一句话不算复核（与处置通道同一把尺）')
    path = disposition_holder_path(root)
    with disposition_lock(root=root, wait_s=LOCK_WAIT_S):
        raw2, state2 = read_holder_raw(root)
        if state2 != 'present' or raw2 != raw:
            raise DispositionError(f'锁内重读发现侧件已变（{state2} 或字节不同）⇒ 现场不是刚才那一个，'
                                   '本轮不动任何字节')
        rep2 = stale_lock_report(root=root, now=now)
        if rep2['verdict'] != 'malformed':
            raise DispositionError(f'锁内重读现状为 {rep2["verdict"]!r} ⇒ 拒（签发与执行之间状态变了）')
        bdir = os.path.join(os.path.abspath(root), 'backups')
        before = sorted(f for f in os.listdir(bdir) if f.endswith(BACKUP_EXTS)) \
            if os.path.isdir(bdir) else []
        held_entry = {'schema': DISPOSITION_SCHEMA, 'mode': 'held-clear',
                      'face': _face(root), 'raw_sha256': hashlib.sha256(raw).hexdigest(),
                      'raw_bytes': len(raw), 'raw_text': raw.decode('utf-8', 'replace'),
                      'problems': rep2['holder_problems'], 'token': token,
                      'reason': str(reason).strip(), 'at': (now or datetime.now(UTC)).isoformat()}
        append_jsonl(disposition_held_path(root), held_entry)
        cap_jsonl(disposition_held_path(root))
        err = None
        try:
            os.remove(path)
        except OSError as exc:
            err = f'{type(exc).__name__}: {exc}'
        after = sorted(f for f in os.listdir(bdir) if f.endswith(BACKUP_EXTS)) \
            if os.path.isdir(bdir) else []
        gone = not os.path.isfile(path)
        append_jsonl(disposition_held_path(root),
                     dict(held_entry, mode='held-cleared',
                          error=err, holder_gone=gone,
                          population_before=len(before), population_after=len(after)))
        cap_jsonl(disposition_held_path(root))
        if err or not gone:
            raise DispositionError(f'清理失败：{err or "删后侧件仍在盘上"} ⇒ 如实报失败，不判通过')
        if after != before:
            raise DispositionError(f'被处置面出现变化（{len(before)}→{len(after)}）⇒ 本通道不该碰它')
    return {'cleared': True, 'was': 'malformed', 'token': token, 'raw_sha256': held_entry['raw_sha256'],
            'raw_bytes': len(raw), 'problems': rep2['holder_problems'], 'reason': held_entry['reason'],
            'held_ledger': disposition_held_path(root),
            'population': len(after)}


def main(argv=None):
    ap = argparse.ArgumentParser(description='把备份载回并断言（默认恢复到一个新的检查点文件，不碰在用库）')
    ap.add_argument('--backup', help='要恢复的 backups/*.db；--selftest 时可省')
    ap.add_argument('--into', help='恢复目标（缺省 = data/restore_check_<ts>.db）')
    ap.add_argument('--force', action='store_true', help='显式允许覆盖已存在的目标（覆盖前先建回滚锚点）')
    ap.add_argument('--check-only', action='store_true', help='只校验备份可读回，不落证据件')
    ap.add_argument('--list-backups', action='store_true',
                    help='只出备份人口清单（逐件判定 + 状态），不恢复；深核加 --deep')
    ap.add_argument('--latest-eligible', action='store_true',
                    help='不手写路径：从清单里取第一份**够格**的备份（够格=名字有时刻∧有证据卡∧没过期∧完整性过）')
    ap.add_argument('--retention', action='store_true',
                    help='只读打印跨族保留体检（人口/各族件数与最老件/族外具名/三把尺对账）')
    ap.add_argument('--plan-disposal', action='store_true',
                    help='只读 dry-run：列出可指认件与不可处置件（**不删、也不发凭据**）')
    ap.add_argument('--ticket', metavar='NAME',
                    help='对某个备份名产一张复核凭据（含 token），并落一条 pending 意图')
    ap.add_argument('--dispose', metavar='NAME',
                    help='唯一真删出口：一次只处置这一件，必须配 --confirm <token>')
    ap.add_argument('--dry-run', action='store_true',
                    help='与 --dispose 配：判定照跑但**不取锁、不落账、不删**'
                         '（撞锁时的只读出口；看全景清单请用 --plan-disposal）')
    ap.add_argument('--confirm', metavar='TOKEN',
                    help='逐字回交凭据里的 token（确认凭据=对象标识本身，不是"我知道"）')
    ap.add_argument('--claim-by', help='认领人（族外无主件的署名，必填才能删）')
    ap.add_argument('--claim-reason', help=f'认领理由（≥{20} 字，一句话不算复核）')
    ap.add_argument('--lock-status', action='store_true',
                    help='只读打印处置锁与归因侧件的现状（live/stale/residual-recent/unreadable/clean）')
    ap.add_argument('--break-lock', metavar='HOLDER_TOKEN',
                    help='清掉 OS 已释放但归因侧件仍残留的锁；须逐字回交 holder token')
    ap.add_argument('--malformed-token', action='store_true',
                    help='只读：打印清理 malformed 侧件所需的 token（由**原文字节**派生，含原文与问题清单）')
    ap.add_argument('--clear-malformed', action='store_true',
                    help='受控清理 malformed 侧件：须配 --confirm <原文 token> 与 --reason（≥20 字），'
                         '先把原文归档到 HELD.jsonl 再删那一个文件')
    ap.add_argument('--reason', metavar='TEXT',
                    help=f'配合 --clear-malformed 的复核理由（≥{MIN_REASON_CHARS} 字）')
    ap.add_argument('--max-age-h', type=float, default=MAX_AGE_H,
                    help=f'自动选择的时效上限（小时，默认 {MAX_AGE_H}）')
    ap.add_argument('--deep', action='store_true', help='清单也逐件跑 integrity_check（慢，但"未核"不再混在通过里）')
    ap.add_argument('--no-ledger', action='store_true',
                    help='跑真恢复但不写台账：夹具/演练用，防把临时面的备份混进真账（第 33 轮 L4 实发）')
    ap.add_argument('--root',
                    help='备份面所在仓根（默认本脚本所在仓；异地备份盘与 L4 演习指别处，这样"零人口"这一态能被真跑出来而不是只靠推理）')
    ap.add_argument('--selftest', action='store_true', help='跑内置往返四腿（供门禁与 CI 复跑）')
    ap.add_argument('--json', action='store_true', help='只打一个 JSON 文档到 stdout')
    ns = ap.parse_args(argv)
    if ns.selftest:
        ok, detail = selftest_roundtrip()
        print(json.dumps({'ok': ok, 'detail': detail}, ensure_ascii=False) if ns.json
              else f'{"✅" if ok else "❌"} restore_db 自检：{detail}')
        return RC_OK if ok else RC_MISMATCH
    if ns.list_backups:
        recs = backup_records(root=ns.root, max_age_h=ns.max_age_h, deep=ns.deep)
        if ns.json:
            print(json.dumps({'schema': 'ican-backup-inventory-v1', 'deep': ns.deep,
                              'max_age_h': ns.max_age_h, 'records': recs}, ensure_ascii=False))
            return RC_OK
        ok_n = sum(1 for r in recs if r['state'] == 'eligible')
        print(f'备份人口 {len(recs)} 件｜够格 {ok_n} 件｜时效上限 {ns.max_age_h}h｜'
              f'{"深核：完整性 + 证据卡对账逐件真跑" if ns.deep else "浅扫：完整性未核，且证据卡未与实盘对账（--deep 才做）"}')
        for r in recs:
            print(f'  {"✅" if r["state"] == "eligible" else "⛔"} {r["name"]} '
                  f'bytes={r["bytes"]} age_h={r["age_h"]} 证据卡={r["manifest"]} 状态={r["state"]}'
                  + (f'｜{r["why"]}' if r['why'] else ''))
        if not ok_n:
            print('❌ 一件都不够格：自动选择会拒绝执行（显式 --backup 仍可，但那是在替工具担判断）')
            return RC_NO_INPUT
        return RC_OK
    if ns.retention:
        try:
            rep = retention_report(root=ns.root)
        except RetentionError as exc:
            print(f'❌ 保留体检无法完成：{exc}')
            return 2
        if ns.json:
            print(json.dumps({'schema': 'ican-backup-retention-v1', **rep}, ensure_ascii=False))
            return RC_OK if rep['population'] else RC_NO_INPUT
        return print_retention(rep)
    if ns.lock_status:
        rep40 = stale_lock_report(root=ns.root)
        if ns.json:
            print(json.dumps({'schema': DISPOSITION_SCHEMA, 'mode': 'lock', **rep40},
                             ensure_ascii=False))
        else:
            age40 = rep40['holder_age_s']
            print(f'处置锁现状：verdict={rep40["verdict"]} '
                  f'归因龄={"-" if age40 is None else f"{age40}s"} '
                  f'锁文件在={rep40["lock_file_present"]} holder_token={rep40["token"] or "-"}')
        return RC_OK
    if ns.break_lock is not None:      # 空串也要进这里：它表达的是"我要破锁但没交 token"，
        try:                           # 按真值判会让它掉进"没给参数"的兜底提示，绕开拒绝理由
            r40 = break_stale_lock(root=ns.root, confirm=ns.break_lock)
        except DispositionError as exc:
            print(f'❌ 破锁被拒：{exc}')
            return RC_NO_INPUT
        age41 = r40['holder_age_s']
        print(f'✅ 已清残留归因（原态 {r40["was"]}，龄 '
              f'{"-" if age41 is None else f"{age41}s"}）：'
              f'{disposition_holder_path(ns.root)}')
        return RC_OK
    if ns.malformed_token:
        rep42 = stale_lock_report(root=ns.root)
        raw42, state42 = read_holder_raw(ns.root)
        if state42 != 'present':
            print(f'❌ 侧件原文取不到（{state42}）⇒ 无 token 可给，判没测到')
            return RC_NO_INPUT
        doc42 = {'schema': DISPOSITION_SCHEMA, 'mode': 'malformed-token',
                 'verdict': rep42['verdict'], 'face': _face(ns.root or BASE_DIR),
                 'raw_bytes': len(raw42),
                 'raw_sha256': __import__('hashlib').sha256(raw42).hexdigest(),
                 'token': malformed_token(raw42, ns.root),
                 'problems': rep42.get('holder_problems') or [],
                 'raw_text': raw42.decode('utf-8', 'replace').rstrip()}
        if ns.json:
            print(json.dumps(doc42, ensure_ascii=False))
        else:
            print(f'malformed 清理凭据：verdict={doc42["verdict"]} token={doc42["token"]} '
                  f'原文 {doc42["raw_bytes"]}B sha={doc42["raw_sha256"][:12]}…')
            for p42 in doc42['problems'] or ['（字段校验无问题 ⇒ 本通道不该动它）']:
                print(f'   ! {p42}')
            print(f'   原文：{doc42["raw_text"][:160]}')
        return RC_OK if rep42['verdict'] == 'malformed' else RC_NO_INPUT
    if ns.clear_malformed:
        try:
            r42 = clear_malformed_holder(root=ns.root, confirm=ns.confirm, reason=ns.reason)
        except DispositionError as exc:
            print(f'❌ 清理被拒：{exc}')
            return RC_NO_INPUT
        print(f'✅ 已受控清理 malformed 归因侧件（原文 {r42["raw_bytes"]}B sha={r42["raw_sha256"][:12]}… '
              f'已归档）；留痕 {r42["held_ledger"]}（在被处置面之外），'
              f'被处置面人口仍 {r42["population"]} 件')
        return RC_OK
    if ns.plan_disposal:
        plan = plan_disposal(root=ns.root)
        if ns.json:
            print(json.dumps({'schema': DISPOSITION_SCHEMA, 'mode': 'plan', **plan},
                             ensure_ascii=False))
            return RC_OK if plan['population'] else RC_NO_INPUT
        return print_disposal_plan(plan)
    if ns.ticket:
        try:
            t = make_ticket(ns.ticket, root=ns.root)
        except DispositionError as exc:
            print(f'❌ 凭据不予签发：{exc}')
            return RC_NO_INPUT
        print(json.dumps({'schema': DISPOSITION_SCHEMA, 'mode': 'ticket', **t},
                         ensure_ascii=False) if ns.json
              else f'凭据已签发：{t["name"]} token={t["token"]} 归属={t["channel"] or "族外无主"} '
                   f'sha={t["sha256"][:12]} 龄={t["age_h"]}h（TTL {t["ttl_s"]}s，过期须重取）')
        return RC_OK
    if ns.dry_run and not ns.dispose:
        print('❌ --dry-run 只与 --dispose 组合有意义：它答"这一件真跑会不会删成"；'
              '要看全景清单请用 --plan-disposal（同为只读，两者不互相顶替）')
        return RC_NO_INPUT
    if ns.dispose:
        try:
            r = dispose(ns.dispose, confirm=ns.confirm, claim_by=ns.claim_by,
                        claim_reason=ns.claim_reason, root=ns.root, dry_run=ns.dry_run)
        except DispositionError as exc:
            print(f'❌ 处置被拒：{exc}')
            return RC_NO_INPUT
        if r.get('dry_run'):
            print(json.dumps({'schema': DISPOSITION_SCHEMA, 'mode': 'probe', **r},
                             ensure_ascii=False) if ns.json
                  else f'🧪 dry-run｜verdict={r["verdict"]}｜锁={r["lock"]}'
                       f'｜人口 {r["population_before"]}→{r["population_after"]}（未动盘）'
                       f'｜{r["why"] or "真跑会删掉这一个"}')
            # rc 语义（当场实测取数，不是推的）：0 = "真跑会删成"（would_dispose），
            # 2 = "真跑也会拒"（refuse:*，与 --list-backups 无够格件同一档 RC_NO_INPUT）。
            # 只读档的返回码必须能区分这两件事，否则它退化成"跑通了"，把拒绝读成成功。
            return RC_OK if r['verdict'] == 'would_dispose' else RC_NO_INPUT
            return RC_OK if r['verdict'] == 'would_dispose' else RC_NO_INPUT
        print(f'✅ 已处置 {r["name"]}（人口 {r["population_before"]} → {r["population_after"]}，'
              f'认领人 {r["claim_by"]}）；台账 {r["ledger"]}（在被删面之外）')
        return RC_OK
    if ns.latest_eligible:
        if ns.backup:
            print('❌ --latest-eligible 与 --backup 互斥：要么点名一份，要么让工具判，两个都给等于没判')
            return RC_NO_INPUT
        path, rec, recs = pick_latest_eligible(root=ns.root, max_age_h=ns.max_age_h)
        if path is None:
            heads = '; '.join(f'{r["name"]}→{r["state"]}' for r in recs[:5])
            print(f'❌ 没有够格的备份（人口 {len(recs)} 件，前 5 名：{heads or "人口为空"}）'
                  '⇒ 拒绝恢复。要强行指定请显式 --backup <路径>（后果由指定人承担，工具不背书）')
            return RC_NO_INPUT
        ns.backup = path
        print(f'ℹ️ 自动选中 {rec["name"]}（时刻 {rec["name_ts"]}，龄 {rec["age_h"]}h，'
              f'integrity={rec["integrity"]}），人口 {len(recs)} 件里第一个够格的')
    if not ns.backup:
        print('❌ 必须给 --backup / --latest-eligible / --list-backups / --retention'
              ' / --plan-disposal / --ticket / --dispose / --lock-status / --break-lock'
              ' / --malformed-token / --clear-malformed（或 --selftest）')
        return RC_NO_INPUT
    into = ns.into or os.path.join(BASE_DIR, 'data', f'restore_check_{int(time.time())}.db')
    if os.path.realpath(ns.backup) == os.path.realpath(into):
        print(f'❌ 备份与恢复目标是同一个文件（{ns.backup}）⇒ 拒。'
              '自恢复会先把目标打开再读源，等于拿"正在被写的自己"当源（第 33 轮方向写反的同族）')
        return RC_NO_INPUT
    os.makedirs(os.path.dirname(into) or BASE_DIR, exist_ok=True)
    rc, doc = restore(ns.backup, into, force=ns.force)
    doc['mode'] = 'check-only' if ns.check_only else 'restore'
    if rc == RC_OK and not ns.check_only and not ns.no_ledger:
        path, why = write_evidence(doc)
        if path is None:
            print(f'❌ {why}')
            return RC_EVIDENCE
        doc['evidence_file'] = os.path.basename(path)
    if ns.json:
        print(json.dumps(doc, ensure_ascii=False))
    else:
        head = '✅ 恢复可核' if rc == RC_OK else ('⚠️ 拒覆盖' if rc == RC_TARGET_EXISTS else '❌ 恢复不可核')
        print(f'{head} rc={rc} ' + json.dumps(doc, ensure_ascii=False)[:400])
    return rc


if __name__ == '__main__':
    sys.exit(main())
