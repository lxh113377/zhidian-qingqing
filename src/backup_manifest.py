"""备份证据件（manifest）的唯一写入与复算入口 —— 第 25 轮 H55。

动因（当场实测，非推测）
────────────────────────
1. 负对照实跑：磁盘上那份 4,096 B 的 `demo-reset_20260922_001606_340670.db`（**0 表 0 行**）
   经 `backup_db.py --verify` 判为「✅ 可恢复 … integrity_check ok，0 表 / 0 行」且 rc=0
   —— `PRAGMA integrity_check` 对空库同样返回 ok，所以"字节层完好"从来不等于"里面有数据"。
2. 备份普查（只读 URI 逐份开）：开发副本 12 份 + 交付副本 10 份 = **22 份** `.db`，
   表数从 5/8/13/14/16/17 各不相同，而证据件 **0 份**；`grep manifest|sidecar` 在门禁全文 **0 命中**。
3. 两条生产者只做完整性检查：`src/pack_demo.create_database_backup()` 连行数核对都没有，
   且源库以读写方式打开（`backup_db` 侧第 21 轮已立的三条守卫没跟着过来 = 同类出口漏修）。

对标规格（同轮 `gh api contents` 取回原文）
──────────────────────────────────────────
- barman `src/barman/infofile.py:499 class BackupInfo(FieldListFile)`：每份备份一张卡，卡上带
  `status` 与 `error` 字段 —— 成败写在**物件**上，不是写在当时的 stdout 里。
- pgbackrest `src/info/manifest/manifest.h:137 struct ManifestFile`：`size / sizeOriginal / sizeRepo`
  分开记 —— 因此能回答"落盘之后有没有被动过"。
- restic `internal/checker/checker.go`：`Structure()` 与 `TreeError{ID, Errors}`/`SnapshotError{ID}`
  —— 复算失败必须**点名到具体对象**，不接受一句"检查失败"。

本模块只记聚合量（表名、行数、字节数、哈希），**绝不写任何业务明细字段**（守商家数据红线）。
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
SUFFIX = '.manifest.json'

STATUS_SUCCESS = 'SUCCESS'
STATUS_FAILED = 'FAILED'
STATUS_UNVERIFIABLE = 'UNVERIFIABLE'

# 证据的来源：备份当时写的才叫快照态；事后补算的只能算回填态（不得追认"与原库一致"）
ORIGIN_SNAPSHOT = 'snapshot'
ORIGIN_BACKFILL = 'backfill'

# 业务骨架表：全为 0 行即视为"这份备份里没有业务数据"（表名取自 models 的实测复数形态）
KEY_TABLES = ('merchants', 'stores', 'staff', 'admin', 'products', 'daily_sales')

MANIFEST_KEYS = frozenset({
    'schema_version', 'tool', 'app_version', 'origin', 'backup_file', 'sha256', 'size_bytes',
    'created_at', 'status', 'error', 'source', 'content', 'checks', 'pruned',
})
SOURCE_KEYS = frozenset({'path', 'sha256', 'size_bytes', 'mtime_ns'})
CONTENT_KEYS = frozenset({'tables', 'row_total', 'rows_by_table', 'key_rows'})
CHECK_KEYS = frozenset({'integrity', 'row_parity', 'empty_guard'})


def ro_uri(path):
    """只读 URI：复算工具绝不该有写被证物的能力（写反方向时它先报错，而不是毁掉原件）。"""
    return Path(os.path.abspath(path)).as_uri() + '?mode=ro'


def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(chunk), b''):
            h.update(block)
    return h.hexdigest()


def table_counts(path):
    """按表名字典序列出每表行数；读不到（非 SQLite / 损坏）时抛异常，由调用方定成败。"""
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            'ORDER BY name')]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]   # noqa: S608  理由：表名取自 sqlite_master 而非外部输入，双引号包裹防关键字冲突
              for n in names}
    finally:
        conn.close()


def integrity_ok(path):
    conn = sqlite3.connect(ro_uri(path), uri=True)
    try:
        rows = conn.execute('PRAGMA integrity_check').fetchall()
    finally:
        conn.close()
    verdicts = [str(r[0]) for r in rows]
    return len(verdicts) == 1 and verdicts[0] == 'ok', '; '.join(verdicts[:3])


def empty_guard(counts):
    """空库守卫：0 表，或关键表全部 0 行 ⇒ False（这正是本轮负对照要拦的那一类）。"""
    if not counts:
        return False, '0 张表（空库备份，integrity_check 对它同样返回 ok）'
    key_rows = {t: counts.get(t, 0) for t in KEY_TABLES if t in counts}
    if key_rows and all(v == 0 for v in key_rows.values()):
        return False, f'关键表全部 0 行：{sorted(key_rows)}'
    return True, ''


def manifest_path(backup_path):
    return backup_path + SUFFIX


def _app_version():
    try:
        from src.version import APP_VERSION
        return APP_VERSION
    except Exception:
        return 'unknown'


def _portable_path(path):
    """证据件里的源路径一律写成**可移植形态**：仓内→相对仓根；仓外→只留文件名。

    为什么不写绝对路径（实测代价）：交付副本里的 `backups/*.manifest.json` 记过
    `C:/Users/<name>/Desktop/workspace/...`，被门禁第 5 项「盲审红线（疑似本机绝对路径）」判红；
    而且这个路径到别人机器上本来就是假的——它既不描述对方的机器，也不描述仓库。
    """
    if not path:
        return ''
    text = os.path.abspath(str(path)).replace(os.sep, '/')
    root = os.path.dirname(os.path.abspath(__file__)).replace(os.sep, '/')
    root = root[:root.rindex('/')]                      # src/ 的上一级 = 项目根
    if text.startswith(root + '/'):
        return text[len(root) + 1:]
    return os.path.basename(text)


def write_manifest(backup_path, *, source_path=None, src_counts=None, counts=None,
                   integrity=None, status=None, error=None, origin=ORIGIN_SNAPSHOT,
                   tool='backup_db.py', pruned=None):
    """写 `<备份名>.manifest.json`（LF / UTF-8 / 键序固定，可 diff 可跨机复算）。

    行数缺失时现算（只读），`status` 缺失时由 integrity + 空库守卫推得——
    但**绝不把"没测到"写成 SUCCESS**：拿不到行计数即 UNVERIFIABLE。
    """
    if not os.path.isfile(backup_path):
        return False, None, f'备份件不存在，拒绝写证据件：{backup_path}'
    if counts is None:
        try:
            counts = table_counts(backup_path)
        except Exception as exc:
            counts = None
            status = status or STATUS_UNVERIFIABLE
            error = error or f'行数复算失败：{exc}'
    eg_ok, eg_why = (True, '') if counts is None else empty_guard(counts)
    if integrity is None:
        try:
            integrity = integrity_ok(backup_path)[0]
        except Exception as exc:
            integrity = False
            error = error or f'integrity 复算失败：{exc}'
    parity = None
    if counts is not None and src_counts is not None:
        parity = (counts == src_counts)
    if status is None:
        status = STATUS_SUCCESS if (integrity and eg_ok and parity is not False) else STATUS_FAILED
        if status == STATUS_FAILED and not error:
            error = eg_why or 'integrity_check 非 ok'
    doc = {
        'schema_version': SCHEMA_VERSION,
        'tool': tool,
        'app_version': _app_version(),
        'origin': origin,
        'backup_file': os.path.basename(backup_path),
        'sha256': sha256_file(backup_path),
        'size_bytes': os.path.getsize(backup_path),
        'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'status': status,
        'error': error or '',
        'source': ({
            'path': _portable_path(source_path),
            'sha256': sha256_file(source_path) if source_path and os.path.isfile(source_path) else '',
            'size_bytes': os.path.getsize(source_path) if source_path and os.path.isfile(source_path) else -1,
            'mtime_ns': (os.stat(source_path).st_mtime_ns
                         if source_path and os.path.isfile(source_path) else -1),
        } if source_path else {'path': '', 'sha256': '', 'size_bytes': -1, 'mtime_ns': -1}),
        'content': ({
            'tables': len(counts),
            'row_total': sum(counts.values()),
            'rows_by_table': dict(sorted(counts.items())),
            'key_rows': {t: counts.get(t, 0) for t in KEY_TABLES if t in counts},
        } if counts is not None else {'tables': -1, 'row_total': -1,
                                      'rows_by_table': {}, 'key_rows': {}}),
        'checks': {'integrity': bool(integrity),
                   'row_parity': (None if parity is None else bool(parity)),
                   'empty_guard': (None if counts is None else bool(eg_ok))},
        'pruned': list(pruned or []),
    }
    path = manifest_path(backup_path)
    try:
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write('\n')
    except OSError as exc:
        return False, None, f'证据件写入失败（备份件保留，但这份备份不得判"已留证"）：{exc}'
    return True, path, f'{os.path.basename(path)} status={status} ' \
                       f'{doc["content"]["tables"]} 表 / {doc["content"]["row_total"]} 行'


def read_manifest(backup_path):
    path = manifest_path(backup_path)
    if not os.path.isfile(path):
        return None, f'缺证据件：{os.path.basename(path)}'
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f'证据件不可读：{os.path.basename(path)} — {exc}'


def record_pruned(backup_path, removed_names):
    """把"删掉了哪些旧备份"记进在存件的证据卡上（保留动作本身也要有证据）。"""
    doc, err = read_manifest(backup_path)
    if err:
        return False, err
    doc['pruned'] = sorted(set(doc.get('pruned') or []) | set(removed_names or []))
    with open(manifest_path(backup_path), 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write('\n')
    return True, f'已记录删除 {len(doc["pruned"])} 份旧备份'


def _card_problems(doc, counts, disk_sha, disk_size):
    """证据卡 ⇄ 实盘的逐条不符项（restic 的形态：点名到具体一项，不是一句"检查失败"）。

    拆成独立函数是因为 `reconcile` 本体一旦超过 mccabe 10 就会进 C901 基线，
    而第 37 项的基线只降不升 —— 新写的判据代码不该去抬高历史欠账。
    """
    if doc.get('sha256') != disk_sha:
        return [f"证据件记 sha256={str(doc.get('sha256'))[:12]}… 实盘={disk_sha[:12]}… "
                '⇒ 备份件在留证之后被改写过']
    if doc.get('size_bytes') != disk_size:
        return [f"证据件记 size={doc.get('size_bytes')} 实盘={disk_size}"]
    if dict(sorted(counts.items())) != doc.get('content', {}).get('rows_by_table'):
        return [f"证据件记 {doc.get('content', {}).get('tables')} 表 / "
                f"{doc.get('content', {}).get('row_total')} 行，"
                f'实盘 {len(counts)} 表 / {sum(counts.values())} 行']
    eg_ok, eg_why = empty_guard(counts)
    if not eg_ok:
        return [f'空库守卫拦截：{eg_why}']
    if doc.get('status') != STATUS_SUCCESS:
        return [f"证据件状态是 {doc.get('status')}（error={doc.get('error') or '无'}）"]
    if doc.get('origin') != ORIGIN_SNAPSHOT:
        return [f"证据件来源是 {doc.get('origin')}（回填态）：它只能证明字节与内容当时如此，"
                '不能追认"与原库逐表一致"——请重新做一次备份']
    checks = doc.get('checks') or {}
    if set(checks) - CHECK_KEYS:
        return [f'证据件 checks 多出未知键：{sorted(set(checks) - CHECK_KEYS)}']
    if checks.get('row_parity') is not True:
        return [f"证据件 row_parity={checks.get('row_parity')}"
                '（非 True：当时就没做或没做成源⇄副本核对）']
    return []


def reconcile(backup_path):
    """实盘 ⇄ 证据件双向对账。返回 (是否判"可用", 说明)；回填态只证明字节没动。"""
    if not os.path.isfile(backup_path):
        return False, f'备份件不存在：{backup_path}'
    doc, err = read_manifest(backup_path)
    if err:
        return False, f'{err}（判"没测到"不判通过；旧件可用 `--stamp` 补成回填态，但回填态不得当快照态用）'
    try:
        counts = table_counts(backup_path)
    except Exception as exc:
        return False, f'行数复算失败：{exc}'
    problems = _card_problems(doc, counts, sha256_file(backup_path), os.path.getsize(backup_path))
    if problems:
        return False, problems[0]
    return True, (f"证据件对账通过：{len(counts)} 表 / {sum(counts.values())} 行，"
                  f"sha256 与 size 均与实盘一致（origin={doc['origin']}）")
