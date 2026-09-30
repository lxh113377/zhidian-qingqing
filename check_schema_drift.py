#!/usr/bin/env python
"""Schema 漂移守卫：ORM 元数据 ⇄ 实际数据库结构对账（只读，不写库、不联网）。

为什么需要它（2026-09-24 第 10 轮实测）
------------------------------------------------
开发库 `data/ican.db` 实测只有 **14** 张表，交付副本同码库有 **16** 张，
差的正是 OPT-A 开放 API 的 `api_tokens` / `webhook_subscriptions`；
对该库做任何 ORM 查询直接抛 `OperationalError: no such table: api_tokens`。
**而当天质量门禁 13 项全绿** —— 既有门禁证明的是「代码之间一致」，
没有任何一项证明「代码与库一致」。

对标口径（三家共同语义，只抄判据不抄机制）
------------------------------------------
- Alembic `alembic check`：跑与 autogenerate 相同的比对，检测到差异即非零退出；
- Django `makemigrations --check` / `migrate --check`：分别检测「模型改了而迁移没写」
  与「有未应用迁移」，命中即非零退出；
- Flyway `validate`：账本与实际不一致时分类报错，而不是静默通过；
- Gitea `calcDBVersion()` 乱序直接 panic、`EnsureUpToDate()` 版本不符即报错。
⇒ 值钱的是 **「有差异就红」这条退出码约定**。
  本轮**沿用**既有探测式幂等迁移、只补判据，理由不是"不想加依赖"，而是两条具体成本：
  ① 本仓只有 1 个环境 1 个库，Alembic env.py 的 online/offline 双模式在此无收益；
  ② 回滚已有 `backup_db.py` + SQLite 文件级还原，双向 downgrade 脚本反而是新增腐化面。
  ⚠️ 2026-09-25 用户立规后重判：**"零依赖"不再是理由**，若将来出现多环境/多人改库，
  正解是直接采用 Alembic，而不是继续加人手写探测（见 07 台账 M21 重评）。

明确不做（避免假装有守卫）
--------------------------
1. **不做 ORM autogenerate 全量 diff**：Alembic 官方自承检不出表改名/列改名
   （"autogenerate is not intended to be perfect"，且检出形态是会丢数据的 add+drop），
   而列改名是本仓手写迁移里最高频的操作。
2. **不在本脚本里改库**：只读。修复一律走 `python init_db.py --if-empty`，
   检测与处置分离（同 Prometheus 告警与 runbook 的分工）。
3. **不加迁移锁**：n8n 源码注释已给出前提 —— "SQLite is single-instance, so
   concurrent migrations aren't possible"；本项目红线禁止多 worker / 多实例。

为什么不借 `create_app()` 拿引擎（本轮实测教训）
------------------------------------------------
在 L4 套件里改写 `src.config.DevelopmentConfig.SQLALCHEMY_DATABASE_URI` 之后，
`create_app()` 产出的 `app.config` **仍是测试夹具先前写入的值**（补丁进了类属性、
没进 config）⇒「隔离审计」会静默读到别的库并给出**假绿**。
漂移判定只需要 SQLAlchemy 引擎 + ORM metadata，两者都不需要应用上下文，
故直连 `create_engine`：更快（实测全量比对 5.3 ms）、且无任何进程级副作用。

分级判据
--------
- **FAIL（退出 1）**：ORM 有库缺表；ORM 有库缺列；ORM 声明的索引库里没有；
  **ORM 声明的唯一约束形状库里没有**（第 17 轮 H29 新增，见下）。
  第三条索引维度专对本仓真实事故 `bcf9fe1`（存量库迁移中断后索引缺失/重名）。
- **WARN（不阻断）**：库有 / ORM 无的表、列、索引、唯一约束形状（反向对账，第 15 轮 M29 补列与索引两级）。
  历史遗留结构判红会让人直接绕过守卫；SQLite 的列只能加不能删，孤儿列会长期存在，
  但"库里留着什么"必须可见——迁移、导出、脱敏都会撞上它。
  `sqlite_autoindex_*` 不算孤儿索引（SQLite 为 UNIQUE 自动建，不在 ORM 声明面）。
- **UNDETERMINED（退出 2）**：库文件不存在或零表 —— 「首次部署前」的正常态，
  必须与「一致」区分，否则门禁会把「没库可查」读成「查了没问题」。

为什么必须比「唯一约束形状」而不只比索引名（第 17 轮 H29 实证）
--------------------------------------------------------------
原三向差集只比 `get_columns` 与 `get_indexes` 的**名字集合**，于是有一个致命盲区：
本仓 `_rebuild_store_scoped_uq()`（init_db.py）真正改的是**列形状**
（`products` 由旧 `UNIQUE(product_name)` 改成了 `UNIQUE(store_id, product_name)`，
见 models.py:198），而 SQLite 把表级 `UNIQUE` 落成 `sqlite_autoindex_products_1`
这种**自动生成名**——名字恒定不变，形状却被改回旧版也照样在"索引名集合"里对得上。
⇒ 手工把唯一约束退回旧形状，原判据仍输出「✅ schema 与 ORM 一致」。
这正是本项目反复在案的「覆盖面按被保护对象的**结构单位**算」在数据库层的同形复发。
修法：两侧都归一化成**有序列元组集合**再比（名字不参与判定），
并只在"ORM 声明而库里没有"时判红（缺形状=迁移没跑或跑丢了，属真缺陷）；
库里多出来的唯一约束只 WARN（可能是历史业务约束，判红逼人绕守卫）。

用法
----
    python check_schema_drift.py                 # 新建库自检 + （若有）实库对账
    python check_schema_drift.py --json          # 机器可读（门禁第 17 项用这条）
    python check_schema_drift.py --db <path>     # 指定要审计的 SQLite 文件
退出码：0 一致 / 1 漂移 / 2 无实库可查且未做新建库自检
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault('ICAN_ALLOW_WEAK_SECRET', '1')

LIVE_DB = os.path.join(ROOT, 'data', 'ican.db')


def metadata():
    """ORM 元数据。import src.models 即完成全部表注册，无需 app 上下文。"""
    import src.models as _m

    return _m.db.metadata


def engine_for(db_path):
    """为目标库建一个只用于反射的引擎（不复用应用连接池，避免共享状态）。"""
    from sqlalchemy import create_engine

    return create_engine('sqlite:///' + os.path.abspath(db_path).replace(os.sep, '/'))


def _orm_unique_shapes(tbl):
    """ORM 一侧的唯一约束形状：有序列元组集合（名字一律不参与，SQLite 会把它换成自动生成名）。"""
    from sqlalchemy.schema import UniqueConstraint

    shapes = {tuple(c.name for c in uc.columns)
              for uc in tbl.constraints if isinstance(uc, UniqueConstraint)}
    shapes |= {tuple(c.name for c in ix.columns) for ix in tbl.indexes if ix.unique}
    return shapes


def _db_unique_shapes(insp, table):
    """库一侧的唯一约束形状：显式唯一索引 + 反射到的 UNIQUE 约束（含 sqlite_autoindex_*）。"""
    shapes = {tuple(ix['column_names']) for ix in insp.get_indexes(table) if ix.get('unique')}
    shapes |= {tuple(uc['column_names']) for uc in insp.get_unique_constraints(table)}
    return shapes


def snapshot(engine):
    """取「库实际结构」一侧：{表名: (列集合, 索引名集合, 唯一约束形状集合)}。"""
    from sqlalchemy import inspect as _inspect

    insp = _inspect(engine)
    return {name: ({c['name'] for c in insp.get_columns(name)},
                   {i['name'] for i in insp.get_indexes(name)},
                   _db_unique_shapes(insp, name))
            for name in insp.get_table_names()}


def snapshot_orm(meta=None):
    """取「ORM 声明」一侧，键与 `snapshot` 严格对齐。"""
    meta = meta if meta is not None else metadata()
    return {name: ({c.name for c in tbl.columns}, {ix.name for ix in tbl.indexes},
                   _orm_unique_shapes(tbl))
            for name, tbl in meta.tables.items()}


def diff(expect, actual):
    """两侧差集，返回纯数据结构（不打印、不改库）。

    `extra_tables` 只作告警：库里存在 ORM 已删的表是常见历史遗留，判红会逼人绕守卫。
    索引只比 ORM 显式声明的索引名，不比 SQLite 为 UNIQUE 约束自动生成的
    `sqlite_autoindex_*`（它不在 ORM 声明里，比了必误报）。

    反向对账（第 15 轮 M29，对标 Zulip `check-database-compatibility`）：
    `extra_columns` / `extra_indexes` 管"库里还留着、ORM 已不声明"的结构——
    SQLite 的列只能加不能删，改过模型的库会长期带着孤儿列与孤儿索引。
    它们同样**只 WARN 不阻断**：孤儿结构不影响本轮读写，判红只会让人绕守卫；
    但"没人知道库里留着什么"是真风险（迁移、导出、脱敏都会撞上它）。
    整表已进 `extra_tables` 的表不参与本项（同一事实不在三处重复报）。

    `missing_unique` / `extra_unique`（第 17 轮 H29）比的是**唯一约束的有序列形状**：
    前者判红（形状没落进库里 = 迁移没跑或跑丢了，直接对应跨店重复数据这类真缺陷），
    后者只告警（库里多出的 UNIQUE 可能是历史业务约束）。
    """
    shared = set(expect) & set(actual)
    for _side, _label in ((expect, 'ORM 声明侧'), (actual, '实库侧')):
        for _t, _tup in _side.items():
            if len(_tup) != 3:      # 旧调用方传 2 元组会在这里炸成 IndexError，排查成本极高
                raise ValueError(f'{_label} 表 {_t} 的快照必须是 (列, 索引名, 唯一形状) 三元组，'
                                 f'实测长度 {len(_tup)}')

    def _u(side, t):
        return sorted(list(x) for x in side[t][2])

    return {
        'missing_tables': sorted(set(expect) - set(actual)),
        'extra_tables': sorted(set(actual) - set(expect)),
        'missing_columns': {t: sorted(expect[t][0] - actual[t][0])
                            for t in sorted(shared) if expect[t][0] - actual[t][0]},
        'missing_indexes': {t: sorted(expect[t][1] - actual[t][1])
                            for t in sorted(shared) if expect[t][1] - actual[t][1]},
        'missing_unique': {t: _u(expect, t) for t in sorted(shared)
                           if expect[t][2] - actual[t][2]},
        'extra_columns': {t: sorted(actual[t][0] - expect[t][0])
                          for t in sorted(shared) if actual[t][0] - expect[t][0]},
        'extra_indexes': {t: sorted(c for c in actual[t][1] - expect[t][1]
                                    if not c.startswith('sqlite_autoindex_'))
                          for t in sorted(shared)
                          if any(not c.startswith('sqlite_autoindex_')
                                 for c in actual[t][1] - expect[t][1])},
        'extra_unique': {t: sorted(list(x) for x in actual[t][2] - expect[t][2])
                         for t in sorted(shared) if actual[t][2] - expect[t][2]},
        'checked_tables': len(shared),
    }


def is_drift(rep):
    return bool(rep) and bool(rep['missing_tables'] or rep['missing_columns']
                              or rep['missing_indexes'] or rep.get('missing_unique'))


def audit_db(db_path):
    """审计一个库文件。

    Args:
        db_path: SQLite 文件路径；不存在或零表 → 'no_database'（不判 ok）。

    Returns:
        (status, report): status ∈ {'ok', 'drift', 'no_database'}
    """
    if not db_path or not os.path.isfile(db_path):
        return 'no_database', {}
    try:
        eng = engine_for(db_path)
    except Exception:      # noqa: BLE001  引擎建不起来属"无法判定"，不是"没问题"
        return 'no_database', {}
    try:
        actual = snapshot(eng)
    finally:
        eng.dispose()
    if not actual:
        return 'no_database', {}
    rep = diff(snapshot_orm(), actual)
    return ('drift' if is_drift(rep) else 'ok'), rep


def audit_fresh():
    """只走 `create_all()` 建一份全新临时库再对账。

    CI 上没有既有库文件，这条给门禁一个**始终可跑**的判据；它同时证明
    `create_all()` 能建出与 metadata 完全一致的结构（含索引名），
    是本守卫的正对照——没有正对照的守卫不知道自己是不是恒绿。
    """
    fd, tmp = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    os.unlink(tmp)
    eng = engine_for(tmp)
    try:
        meta = metadata()
        meta.create_all(eng)
        return diff(snapshot_orm(meta), snapshot(eng))
    finally:
        eng.dispose()
        for suffix in ('', '-wal', '-shm'):
            try:
                if os.path.exists(tmp + suffix):
                    os.remove(tmp + suffix)
            except OSError:
                pass


def audit_ensure_list():
    """补列迁移清单 `_ENSURE_COLUMNS` 自身是否可信。

    结构为 `{表名: [(列名, DDL 类型), ...]}`（init_db.py:42 起）。
    这条判据非恒真（已用"注入错列名"的负对照证明它有鉴别力）：清单里的表名/列名
    与 models 靠人肉对齐，写错一个字符就等于那条补列迁移**永远不会生效**，
    而实库对账看不出问题——那列"本就不该在"新建库里。
    """
    import init_db as _init

    tables = metadata().tables
    problems = []
    for table, entries in getattr(_init, '_ENSURE_COLUMNS', {}).items():
        if table not in tables:
            problems.append(f'表名不存在于 ORM：{table}')
            continue
        cols = {c.name for c in tables[table].columns}
        for pair in entries:
            column = pair[0]
            if column not in cols:
                problems.append(f'列名不存在于 ORM：{table}.{column}（该条补列迁移永不生效）')
    return problems


def audit_ledger(db_path):
    """实库的**迁移账本**对账（第 17 轮 H27）：库里记过的 pid ⇄ `src/schema_ledger` 声明名单。

    结构与 `diff()` 分开的理由：`diff()` 比的是"库长什么样"，本函数比的是
    "库被推到第几步" —— 前者 create_all 新建库也必须过，后者对新建库**天然不成立**
    （刚建的库一个补丁都没跑过，属正常态，判红会让每次 CI 冷启动都红）。
    所以本函数只审计实库，新建库自检不调用它。

    `pending` 非空 = 该库没升到最新（跑 `init_db.py --if-empty`）；
    `orphans` 非空 = 有人改了已发布的 pid 名（账本主键改名 = 存量库历史变幽灵记录）。
    两者都判 FAIL：账本只有在"两个方向都不许错"时才是账本，否则只是日志。
    """
    if not db_path or not os.path.isfile(db_path):
        return None
    from sqlalchemy import inspect as _inspect
    from sqlalchemy import text as _text

    from src import schema_ledger

    eng = engine_for(db_path)
    try:
        has = bool(_inspect(eng).has_table('schema_patch_log'))
        with eng.connect() as conn:
            logged = (sorted(r[0] for r in conn.execute(
                _text('SELECT patch_id FROM schema_patch_log'))) if has else [])
    finally:
        eng.dispose()
    declared = list(schema_ledger.PATCH_IDS)
    return {'ledger_table': has, 'logged': logged,
            'pending': [p for p in declared if p not in logged],
            'orphans': sorted(set(logged) - set(declared)), 'declared': len(declared)}


def main(argv=None):
    """CLI 入口。

    Returns:
        int: 0 一致 / 1 漂移或清单有问题 / 2 无实库可查且未做新建库自检。
    """
    ap = argparse.ArgumentParser(description='ORM ⇄ 数据库 schema 漂移对账（只读）')
    ap.add_argument('--json', action='store_true', help='输出机器可读 JSON')
    ap.add_argument('--db', default=None, help='要审计的 SQLite 文件（默认 data/ican.db）')
    ap.add_argument('--live-only', action='store_true', help='跳过新建库自检')
    args = ap.parse_args(argv)

    target = args.db or (LIVE_DB if os.path.isfile(LIVE_DB) else None)
    live_status, live_rep = audit_db(target)
    fresh_rep = None if args.live_only else audit_fresh()
    ensure_problems = audit_ensure_list()
    ledger_rep = audit_ledger(target)

    ledger_problems = []
    if ledger_rep and (ledger_rep['pending'] or ledger_rep['orphans']
                       or not ledger_rep['ledger_table']):
        if not ledger_rep['ledger_table']:
            ledger_problems.append(
                f"账本表缺失：0/{ledger_rep['declared']} 补丁有记录 → "
                "跑 python init_db.py --if-empty")
        for p in ledger_rep['pending']:
            ledger_problems.append(f'待跑补丁 {p} → 跑 python init_db.py --if-empty')
        for o in ledger_rep['orphans']:
            ledger_problems.append(f'孤儿账本 {o}：声明名单里已无此 pid（禁止改名已发布补丁）')

    fail = ((live_status == 'drift') or is_drift(fresh_rep) or bool(ensure_problems)
            or bool(ledger_problems))
    undetermined = live_status == 'no_database' and args.live_only
    code = 2 if (undetermined and not fail) else (1 if fail else 0)

    if args.json:
        print(json.dumps({
            'ok': code == 0, 'exit_code': code,
            'db_audited': target or '(无实库)', 'live_status': live_status,
            'live': live_rep, 'fresh': fresh_rep,
            'ledger': ledger_rep, 'ledger_problems': ledger_problems,
            'ensure_list_problems': ensure_problems,
        }, ensure_ascii=False, indent=2))
        return code

    def say(line):
        print('↳ ' + line)

    def shape(rows):
        return ' / '.join('+'.join(cols) for cols in rows)

    say(f"实库 {live_status}：缺表 {len(live_rep.get('missing_tables') or [])} "
        f"缺列表 {len(live_rep.get('missing_columns') or {})} "
        f"缺索引表 {len(live_rep.get('missing_indexes') or {})} "
        f"缺唯一形状表 {len(live_rep.get('missing_unique') or {})} "
        f"孤儿列表 {len(live_rep.get('extra_columns') or {})} "
        f"孤儿索引表 {len(live_rep.get('extra_indexes') or {})} "
        f"孤儿唯一形状表 {len(live_rep.get('extra_unique') or {})} "
        f"已核表 {live_rep.get('checked_tables', 0)}｜库={target or '(无)'}")
    for t in live_rep.get('missing_tables') or []:
        say(f'  缺表 {t} → 修复：python init_db.py --if-empty')
    for t, cols in (live_rep.get('missing_columns') or {}).items():
        say(f'  缺列 {t}: {", ".join(cols)}')
    for t, ixs in (live_rep.get('missing_indexes') or {}).items():
        say(f'  缺索引 {t}: {", ".join(ixs)}')
    for t, shp in (live_rep.get('missing_unique') or {}).items():
        say(f'  缺唯一约束形状 {t}: ({shape(shp)}) → 修复：python init_db.py --if-empty')
    for t in live_rep.get('extra_tables') or []:
        say(f'  告警（不阻断）库内多表 {t}：ORM 未声明')
    for t, cols in (live_rep.get('extra_columns') or {}).items():
        say(f'  告警（不阻断）库内孤儿列 {t}: {", ".join(cols)}｜ORM 已不声明，迁移/导出会撞上它')
    for t, ixs in (live_rep.get('extra_indexes') or {}).items():
        say(f'  告警（不阻断）库内孤儿索引 {t}: {", ".join(ixs)}')
    for t, shp in (live_rep.get('extra_unique') or {}).items():
        say(f'  告警（不阻断）库内多余唯一约束 {t}: ({shape(shp)})｜ORM 未声明，可能挡住合法写入')
    if fresh_rep is not None:
        say(f"create_all 新建库自检：缺表 {len(fresh_rep['missing_tables'])} "
            f"缺列表 {len(fresh_rep['missing_columns'])} "
            f"缺索引表 {len(fresh_rep['missing_indexes'])} "
            f"缺唯一形状表 {len(fresh_rep.get('missing_unique') or {})} "
            f"孤儿列表 {len(fresh_rep.get('extra_columns') or {})} "
            f"孤儿索引表 {len(fresh_rep.get('extra_indexes') or {})}")
    say(f'_ENSURE_COLUMNS 清单自检：{"通过" if not ensure_problems else "不通过"}')
    for p in ensure_problems:
        say(f'  {p}')
    if ledger_rep is not None:
        say(f"迁移账本自检：已记 {len(ledger_rep['logged'])}/{ledger_rep['declared']} "
            f"待跑 {len(ledger_rep['pending'])} 孤儿 {len(ledger_rep['orphans'])}")
    for p in ledger_problems:
        say(f'  {p}')
    if code == 0:
        _warn_n = (len(live_rep.get('extra_tables') or [])
                   + len(live_rep.get('extra_columns') or {})
                   + len(live_rep.get('extra_indexes') or {})
                   + len(live_rep.get('extra_unique') or {}))
        print('✅ schema 与 ORM 一致（含唯一约束形状）'
              + (f'（另有 {_warn_n} 处孤儿结构属告警，不阻断：库里有 / ORM 已不声明）' if _warn_n else ''))
    elif code == 2:
        print('⚠️ 无实库可查（首次部署前属正常态，不得当作已通过）')
    else:
        print('❌ 检出 schema 漂移')
    return code


if __name__ == '__main__':
    sys.exit(main())
