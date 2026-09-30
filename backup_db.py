"""数据库备份脚本 — SQLite 在线一致快照 / MySQL mysqldump，备份后必验

用法:
    python backup_db.py                    # 备份并当场验证（可恢复才判成功）
    python backup_db.py --keep 30          # 保留最近30份（默认30；非法值退出2）
    python backup_db.py --verify <文件>    # 只复验一份既有备份能否恢复（字节层 + 证据层）
    python backup_db.py --stamp <文件>     # 给历史备份补一份「回填态」证据件（不得当可恢复凭据）
    python backup_db.py --help             # 只看帮助，不写任何文件

建议加入 crontab（每日凌晨3点）:
    0 3 * * * cd /opt/ican && /usr/bin/python3 backup_db.py >> backups/backup.log 2>&1

策略:
- DATABASE_URL 含 mysql → 调 mysqldump 导出 .sql，按「尾部完成标记 + 非空」判成功
- 否则按 SQLite 处理 → sqlite3 在线备份 API。文件复制（shutil.copy2）在库正在写入时
  会拿到撕裂页，integrity_check 直接判坏，所以「复制没报错」不等于「备份能用」
- 每次备份后立即复验：PRAGMA integrity_check 为 ok，且逐表行数与原库一致；
  任一不满足即删除该副本并以非零退出，绝不留一份「看起来成功」的坏备份
- 每次备份成功后写 `<备份名>.manifest.json` 证据件（见 src/backup_manifest.py）：
  状态、行数、源⇄副本核对、sha256 都写在**物件**上，而不是只写在当时的 stdout 里
- 每次备份后自动清理超出保留份数的旧文件，并把删掉的份名记进在存件的证据卡

复验口径（第 25 轮收紧）:
    integrity_check 对 0 表 0 行的空库同样返回 ok —— 本机实测那份 4,096 B 的备份就是这样
    被判「✅ 可恢复」的。所以 `--verify` 现在分两层：字节层（integrity + 行数复算）与证据层
    （manifest ⇄ 实盘对账 + 空库守卫 + 来源必须是快照态）。回填态（--stamp 补的）只证明字节
    没动，不追认「与原库逐表一致」，因此不得当成可恢复凭据用。

日志只输出路径、体积与行数等聚合量，不含任何商家业务字段。
"""
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.backup_manifest import (ORIGIN_BACKFILL, STATUS_FAILED, STATUS_SUCCESS,
                                 STATUS_UNVERIFIABLE, empty_guard, manifest_path, record_pruned,
                                 reconcile, write_manifest)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
# 份数上限必须是**在册常数**：第 38 轮起它要能被 `restore_db.retention_channels()` 现取，
# 匿名 `return 30` 让「留 30 份」这条尺只藏在函数体里，谁也核对不了它还是不是 30。
DEFAULT_KEEP = 30
DUMP_DONE_MARKER = '-- Dump completed'

HELP = __doc__


def parse_mysql_url(url):
    """mysql+pymysql://user:pass@host:port/dbname -> dict"""
    body = url.split('://', 1)[1]
    cred, host_db = body.rsplit('@', 1)
    user, _, password = cred.partition(':')
    host_port, _, dbname = host_db.partition('/')
    host, _, port = host_port.partition(':')
    return {
        'user': user, 'password': password,
        'host': host or 'localhost', 'port': port or '3306',
        'dbname': dbname.split('?')[0],
    }


def _silent_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _table_names(conn):
    sql = ("SELECT name FROM sqlite_master WHERE type='table' "
           "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    return [row[0] for row in conn.execute(sql)]


def _table_counts(conn):
    # 表名一律取自 sqlite_master（非外部输入），双引号包裹防关键字冲突
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]  # noqa: S608
            for name in _table_names(conn)}


def check_sqlite_file(path):
    """打开副本做完整性与行数清点 → (ok, detail, counts)"""
    if not os.path.exists(path):
        return False, '文件不存在', {}
    if os.path.getsize(path) == 0:
        return False, '文件为空（0 字节）', {}
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error as exc:
        return False, f'无法打开: {exc}', {}
    try:
        rows = conn.execute('PRAGMA integrity_check').fetchall()
        verdicts = [str(r[0]) for r in rows]
        if verdicts != ['ok']:
            return False, 'integrity_check: ' + '; '.join(verdicts[:3]), {}
        return True, 'integrity_check ok', _table_counts(conn)
    except sqlite3.Error as exc:
        return False, f'清点失败: {exc}', {}
    finally:
        conn.close()


def check_sql_dump_file(path):
    """mysqldump 产物只有「非空 + 尾部完成标记」才算写完"""
    if not os.path.exists(path):
        return False, '文件不存在'
    size = os.path.getsize(path)
    if size == 0:
        return False, '文件为空（0 字节）'
    with open(path, 'rb') as fh:
        fh.seek(max(0, size - 4096))
        tail = fh.read().decode('utf-8', 'ignore')
    if DUMP_DONE_MARKER not in tail:
        return False, '尾部缺少完成标记（疑似中途截断）'
    return True, f'{size} 字节且含完成标记'


def verify_backup(path):
    """两层复验：字节层（integrity + 行数）+ 证据层（manifest ⇄ 实盘对账）。

    `.sql` 走 mysqldump 完成标记那条腿（无 SQLite 语义，行数不可本地复算）。
    """
    if path.endswith('.sql'):
        return check_sql_dump_file(path)
    ok, detail, counts = check_sqlite_file(path)
    if not ok:
        return ok, detail
    rok, rdetail = reconcile(path)
    if not rok:
        return False, f'字节层通过（{detail}），但证据层不过：{rdetail}'
    return True, f'{detail}；{rdetail}'


def _ro_uri(path):
    """只读 URI：备份方向一旦写反，SQLite 直接报 readonly 而不是毁掉原库。"""
    return Path(os.path.abspath(path)).as_uri() + '?mode=ro'


def _sqlite_snapshot(src, dst):
    """在线一致快照 → (ok, detail, src_counts)。

    源连接以 mode=ro 打开是硬前提：backup() 的语义是 source.backup(target)，
    写反时只读连接抛 readonly，而不是把空库灌回原库（2026-09-26 本机实测踩过）。
    """
    try:
        source = sqlite3.connect(_ro_uri(src), uri=True)
    except sqlite3.Error as exc:
        return False, f'无法以只读方式打开原库: {exc}', None
    try:
        src_counts = _table_counts(source)
        try:
            target = sqlite3.connect(dst)
        except sqlite3.Error as exc:
            return False, f'无法创建备份文件: {exc}', None
        try:
            source.backup(target)      # 方向：source → target
        except sqlite3.Error as exc:
            return False, f'在线备份失败: {exc}', None
        finally:
            target.close()
    finally:
        source.close()
    return True, '快照完成', src_counts


def backup_sqlite(ts, src=None, dst_dir=None):
    src = src or os.path.join(BASE_DIR, 'data', 'ican.db')
    dst_dir = dst_dir or BACKUP_DIR
    if not os.path.exists(src):
        print(f'❌ SQLite 库不存在: {src}')
        return None
    dst = os.path.join(dst_dir, f'ican_{ts}.db')
    src_stat = os.stat(src)

    ok, detail, src_counts = _sqlite_snapshot(src, dst)
    if not ok:
        _silent_remove(dst)
        print(f'❌ 备份失败: {detail}')
        return None
    ok, detail, dst_counts = check_sqlite_file(dst)
    if not ok:
        _silent_remove(dst)
        print(f'❌ 备份未通过完整性校验，已丢弃副本: {detail}')
        return None
    diff = {k: (src_counts.get(k), v) for k, v in dst_counts.items()
            if src_counts.get(k) != v}
    if set(src_counts) - set(dst_counts) or diff:
        _silent_remove(dst)
        print(f'❌ 备份行数与原库不一致，已丢弃副本: {diff or sorted(set(src_counts) - set(dst_counts))}')
        return None
    post_stat = os.stat(src)
    if (post_stat.st_size, post_stat.st_mtime_ns) != (src_stat.st_size, src_stat.st_mtime_ns):
        _silent_remove(dst)
        print(f'❌ 备份期间原库被写入（size {src_stat.st_size}→{post_stat.st_size}），'
              '本轮不判成功，请检查是否有并发写入')
        return None
    print(f'🔍 复验通过: {len(dst_counts)} 表 / {sum(dst_counts.values())} 行与原库一致，原库未被改写')
    mok, _mp, mdetail = write_manifest(dst, source_path=src, src_counts=src_counts,
                                       counts=dst_counts, integrity=True, tool='backup_db.py')
    if not mok:
        # 副本本身是好的，但"没有证据的备份"在这个仓里不再等于"备份成功"（第 25 轮 H55）
        print(f'❌ 证据件未写出，本轮不判成功（副本保留但不得当已留证使用）: {mdetail}')
        return None
    print(f'🧾 证据件: {mdetail}')
    return dst


def backup_mysql(url, ts):
    cfg = parse_mysql_url(url)
    dst = os.path.join(BACKUP_DIR, f'ican_{ts}.sql')
    cmd = [
        'mysqldump',
        f'-h{cfg["host"]}', f'-P{cfg["port"]}',
        f'-u{cfg["user"]}', f'-p{cfg["password"]}',
        '--single-transaction', '--routines', '--triggers',
        cfg['dbname'],
    ]
    try:
        with open(dst, 'w', encoding='utf-8') as f:
            # argv 直调、无 shell；mysqldump 路径与凭据均来自本机 DATABASE_URL
            ret = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)  # noqa: S603
    except OSError as exc:
        _silent_remove(dst)
        print(f'❌ mysqldump 无法启动（命令不在 PATH？）: {exc}')
        return None
    if ret.returncode != 0:
        _silent_remove(dst)
        print(f'❌ mysqldump 失败: {ret.stderr.decode("utf-8", "ignore")}')
        return None
    ok, detail = check_sql_dump_file(dst)
    if not ok:
        _silent_remove(dst)
        print(f'❌ mysqldump 产物不可信，已丢弃副本: {detail}')
        return None
    print(f'🔍 复验通过: {detail}')
    return dst


def latest_backup():
    files = glob.glob(os.path.join(BACKUP_DIR, 'ican_*.db')) + \
        glob.glob(os.path.join(BACKUP_DIR, 'ican_*.sql'))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def cleanup(keep):
    files = sorted(
        glob.glob(os.path.join(BACKUP_DIR, 'ican_*.db'))
        + glob.glob(os.path.join(BACKUP_DIR, 'ican_*.sql')),
        key=os.path.getmtime,
    )
    removed, failed = [], 0
    while len(files) > keep:
        victim = files.pop(0)
        try:
            os.remove(victim)
            removed.append(os.path.basename(victim))
            if victim.endswith('.db') and os.path.isfile(manifest_path(victim)):
                os.remove(manifest_path(victim))   # 件已删，留孤儿证据只会误导下一次审计
        except OSError:
            failed += 1
    return removed, failed


def parse_keep(argv):
    if '--keep' not in argv:
        return DEFAULT_KEEP
    i = argv.index('--keep')
    if i + 1 >= len(argv):
        print('❌ --keep 需要一个整数参数')
        sys.exit(2)
    try:
        keep = int(argv[i + 1])
    except ValueError:
        print(f'❌ --keep 参数非法: {argv[i + 1]!r}')
        sys.exit(2)
    if keep < 1:
        print(f'❌ --keep 必须 ≥1（收到 {keep}），拒绝执行以免清空全部备份')
        sys.exit(2)
    return keep


def parse_known_args(argv):
    """未识别参数一律拒绝：与 init_db.py 同口径，避免 --help 之类触发真实备份"""
    known = {'--keep', '--verify', '--stamp', '--selftest', '--help', '-h'}
    valued = {'--keep', '--verify', '--stamp'}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token not in known:
            print(f'❌ 未知参数: {token}（用 --help 查看用法）')
            sys.exit(2)
        if token in valued:
            if i + 1 >= len(argv):
                print(f'❌ {token} 需要一个值（文件路径或份数）')
                sys.exit(2)
            i += 2
        else:
            i += 1


# ═══════════ 自检（--selftest，第 25 轮 H57；门禁第 38 项实跑它）═══════════
# 为什么计数由本脚本自己报：门禁第 38 项用 AST 数出下面 `_st_` 的条数，再与子进程报回的
# legs 对齐 ⇒ 任何一条被静默跳过（异常吞掉、条件短路）都会让两边不等，判红。

import tempfile

from src.backup_manifest import (CHECK_KEYS, CONTENT_KEYS, MANIFEST_KEYS, ORIGIN_SNAPSHOT,
                                 SOURCE_KEYS, STATUS_FAILED, sha256_file, table_counts)


def _st_dir(tag):
    return tempfile.mkdtemp(prefix='ican_selftest_' + tag + '_')


def _st_make_db(path, rows=3, with_data=True):
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE merchants (id INTEGER PRIMARY KEY, name TEXT)')
    con.execute('CREATE TABLE stores (id INTEGER PRIMARY KEY, merchant_id INTEGER)')
    con.execute('CREATE TABLE daily_sales (id INTEGER PRIMARY KEY, store_id INTEGER, '
                'record_date TEXT)')
    if with_data:
        con.executemany('INSERT INTO merchants (name) VALUES (?)',
                        [('m' + str(i),) for i in range(rows)])
        con.executemany('INSERT INTO stores (merchant_id) VALUES (?)',
                        [(1,) for _ in range(rows)])
        con.executemany('INSERT INTO daily_sales (store_id, record_date) VALUES (?, ?)',
                        [(1, '2026-09-' + str(i + 1).zfill(2)) for i in range(rows)])
    con.commit()
    con.close()


def _st_empty_db(path):
    # 真 0 表库：只留页头（本机那份 4,096 B 的案发备份就是这个形态）
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE tmp_probe (x INTEGER)')
    con.execute('DROP TABLE tmp_probe')
    con.commit()
    con.close()


def _st_snapshot_writes_manifest():
    d = _st_dir('snap')
    src = os.path.join(d, 'src.db')
    _st_make_db(src, rows=4)
    dst = backup_sqlite('st1', src=src, dst_dir=d)
    if not dst:
        return False, 'backup_sqlite 返回 None'
    ok = os.path.isfile(manifest_path(dst))
    doc = json.load(open(manifest_path(dst), encoding='utf-8')) if ok else {}
    return ok and doc.get('status') == STATUS_SUCCESS, 'status=' + str(doc.get('status'))


def _st_manifest_shape_exact():
    d = _st_dir('shape')
    src = os.path.join(d, 'src.db')
    _st_make_db(src)
    dst = backup_sqlite('st2', src=src, dst_dir=d)
    doc = json.load(open(manifest_path(dst), encoding='utf-8'))
    bad = sorted((set(doc) - MANIFEST_KEYS) | (MANIFEST_KEYS - set(doc)))
    sub = (set(doc['source']) != SOURCE_KEYS or set(doc['content']) != CONTENT_KEYS
           or set(doc['checks']) != CHECK_KEYS)
    unsorted = list(doc['content']['rows_by_table']) != sorted(doc['content']['rows_by_table'])
    return (not bad and not sub and not unsorted), '键差=' + str(bad) + ' 子键漂移=' + str(sub)


def _st_row_parity_recorded():
    d = _st_dir('parity')
    src = os.path.join(d, 'src.db')
    _st_make_db(src, rows=5)
    dst = backup_sqlite('st3', src=src, dst_dir=d)
    doc = json.load(open(manifest_path(dst), encoding='utf-8'))
    same = doc['content']['rows_by_table'] == dict(sorted(table_counts(src).items()))
    return doc['checks']['row_parity'] is True and same, 'parity=' + str(doc['checks']['row_parity'])


def _st_source_not_mutated():
    d = _st_dir('ro')
    src = os.path.join(d, 'src.db')
    _st_make_db(src)
    before = sha256_file(src)
    backup_sqlite('st4', src=src, dst_dir=d)
    return sha256_file(src) == before, '源库字节在备份前后必须逐字节相同'


def _st_verify_requires_manifest():
    d = _st_dir('nomanifest')
    src = os.path.join(d, 'src.db')
    _st_make_db(src)
    dst = os.path.join(d, 'legacy.db')
    shutil.copyfile(src, dst)
    ok, detail = verify_backup(dst)
    return (not ok) and '缺证据件' in detail, 'ok=' + str(ok) + ' ' + detail[:70]


def _st_empty_table_guard():
    d = _st_dir('empty')
    e = os.path.join(d, 'empty.db')
    _st_empty_db(e)
    ok, _detail = verify_backup(e)
    write_manifest(e, origin=ORIGIN_BACKFILL, tool='selftest')
    doc = json.load(open(manifest_path(e), encoding='utf-8'))
    return ((not ok) and doc['status'] == STATUS_FAILED
            and doc['checks']['empty_guard'] is False), 'verify_ok=' + str(ok)


def _st_zero_rows_guard():
    d = _st_dir('zerorows')
    z = os.path.join(d, 'z.db')
    _st_make_db(z, with_data=False)
    ok, why = empty_guard(table_counts(z))
    return (not ok) and '0 行' in why, 'ok=' + str(ok) + ' ' + why[:60]


def _st_backfill_not_accepted():
    d = _st_dir('backfill')
    src = os.path.join(d, 'src.db')
    _st_make_db(src)
    mok, _mp, _dt = write_manifest(src, origin=ORIGIN_BACKFILL, tool='selftest')
    ok, detail = reconcile(src)
    return mok and (not ok) and '回填态' in detail, 'ok=' + str(ok) + ' ' + detail[:70]


def _st_tamper_detected():
    d = _st_dir('tamper')
    src = os.path.join(d, 'src.db')
    _st_make_db(src)
    dst = backup_sqlite('st5', src=src, dst_dir=d)
    with sqlite3.connect(dst) as tc:
        tc.execute('CREATE TABLE injected (x INTEGER)')
    ok, detail = reconcile(dst)
    return (not ok) and 'sha256' in detail, 'ok=' + str(ok) + ' ' + detail[:70]


def _st_pruned_recorded():
    d = _st_dir('pruned')
    src = os.path.join(d, 'src.db')
    _st_make_db(src)
    dst = backup_sqlite('st6', src=src, dst_dir=d)
    record_pruned(dst, ['ican_old1.db', 'ican_old2.db'])
    doc = json.load(open(manifest_path(dst), encoding='utf-8'))
    rok, _rd = reconcile(dst)
    return doc['pruned'] == ['ican_old1.db', 'ican_old2.db'] and rok, str(doc['pruned'])[:60]


SELFTESTS = (_st_snapshot_writes_manifest, _st_manifest_shape_exact, _st_row_parity_recorded,
             _st_source_not_mutated, _st_verify_requires_manifest, _st_empty_table_guard,
             _st_zero_rows_guard, _st_backfill_not_accepted, _st_tamper_detected,
             _st_pruned_recorded)


def run_selftest():
    """跑 SELFTESTS 全部腿；每条腿出一行机器可读回执，结论行由门禁第 38 项对账。

    回执形状固定为 `[<腿名>] PASS|FAIL <说明>` —— 门禁要数它，所以分母不许只由本脚本自报：
    自报 legs=10 而回执只有 7 行 ⇒ 判"有腿被静默跳过"。
    """
    passed = 0
    for fn in SELFTESTS:
        try:
            ok, detail = fn()
        except Exception as exc:                       # 异常算失败，不算跳过
            ok, detail = False, type(exc).__name__ + ': ' + str(exc)
        passed += 1 if ok else 0
        print(f'[{fn.__name__}] {"PASS" if ok else "FAIL"} {str(detail)[:140]}')
    good = passed == len(SELFTESTS)
    print('SELFTEST ' + ('ok' if good else 'FAILED')
          + ' legs=' + str(passed) + '/' + str(len(SELFTESTS)))
    return 0 if good else 1


def main():
    argv = sys.argv[1:]
    if '--selftest' in argv:
        sys.exit(run_selftest())

    if '--help' in argv or '-h' in argv:
        print(HELP)
        sys.exit(0)
    parse_known_args(argv)

    if '--verify' in argv:
        path = argv[argv.index('--verify') + 1]
        ok, detail = verify_backup(path)
        print(f'{"✅ 可恢复" if ok else "❌ 不可恢复"}: {path} — {detail}')
        sys.exit(0 if ok else 1)

    if '--stamp' in argv:
        path = argv[argv.index('--stamp') + 1]
        if not os.path.isfile(path):
            print(f'❌ 文件不存在，拒绝补证据件: {path}')
            sys.exit(1)
        counts = _table_counts(sqlite3.connect(_ro_uri(path), uri=True))
        eg_ok, eg_why = empty_guard(counts)
        status = STATUS_UNVERIFIABLE if eg_ok else STATUS_FAILED
        mok, mpath, mdetail = write_manifest(
            path, origin=ORIGIN_BACKFILL, counts=counts, status=status,
            error=('' if eg_ok else eg_why) or '回填态：创建当时未观测源库，不得追认「与原库逐表一致」',
            tool='backup_db.py --stamp')
        print(f'{"🧾 已补回填态证据件" if mok else "❌ 补写失败"}: {mdetail}')
        if mok:
            print(f'   ⚠️ {os.path.basename(path)} 的证据件 origin={ORIGIN_BACKFILL} '
                  f'status={status}：用于清点可以，用于"能恢复"不行 —— 请重新做一次备份')
        # 补写成功 ≠ 备份可用：空库/损坏这类 FAILED 卡必须让脚本侧读到非零
        sys.exit(0 if mok and status != STATUS_FAILED else 1)

    keep = parse_keep(argv)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')  # 备份名走 UTC：跨机可比
    db_url = os.environ.get('DATABASE_URL', '')

    if 'mysql' in db_url:
        out = backup_mysql(db_url, ts)
    else:
        out = backup_sqlite(ts)

    if not out:
        print('❌ 本轮备份失败，未做任何清理（旧备份保持原样）')
        sys.exit(1)

    removed, failed = cleanup(keep)
    if removed:
        pok, pdetail = record_pruned(out, removed)
        print(('🧾 删除动作已记入证据卡: ' if pok else '⚠️ 删除动作未能记入证据卡: ') + pdetail)
    size_kb = os.path.getsize(out) / 1024
    print(f'✅ 备份完成、已验证并留证: {out} ({size_kb:.1f} KB)，清理旧备份 {len(removed)} 份')
    if failed:
        print(f'⚠️ {failed} 份旧备份删除失败（不影响本轮备份有效性）')


if __name__ == '__main__':
    main()
