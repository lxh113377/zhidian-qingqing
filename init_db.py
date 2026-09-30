"""数据库初始化脚本 — 建表 + 插入初始测试数据

⚠️ 默认模式会 drop_all() 清库重建，生产环境默认禁跑。
生产确需初始化时显式确认：ICAN_INIT_CONFIRM=YES python init_db.py

幂等模式（--if-empty）：仅当业务表不存在时才建表播种；已初始化则直接跳过。
免费托管/容器每次启动都会调用，用于杜绝「重启即清库」的数据丢失：
    python init_db.py --if-empty
"""
import contextlib
import json
import os
import sys

# 生产环境守卫：未显式确认时拒绝执行（防误跑清库）
if (os.environ.get('FLASK_ENV') == 'production'
        and os.environ.get('ICAN_INIT_CONFIRM') != 'YES'):
    print('❌ 生产环境默认禁跑 init_db.py（drop_all 会清空全部数据）。')
    print('   确认要初始化请执行: ICAN_INIT_CONFIRM=YES python init_db.py')
    sys.exit(1)

# 将项目根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.app import create_app
from src.models import MetricDef, Product, db


def _table_exists(name):
    """目标库是否已存在指定表（--if-empty 幂等初始化判断用）。

    Args:
        name: 表名。

    Returns:
        bool: 表存在返回 True。
    """
    from sqlalchemy import inspect
    return inspect(db.engine).has_table(name)


# P2 新增列（旧库轻量迁移清单）：表名 -> [(列名, DDL 类型)]
_ENSURE_COLUMNS = {
    # PH2-2：多租户与多门店落列（NULL = 历史数据，由 ensure_default_tenant 回填）
    'admin': [
        ('role', "VARCHAR(20) NOT NULL DEFAULT 'owner'"),
        ('merchant_id', 'INTEGER'),
    ],
    'staff': [
        ('merchant_id', 'INTEGER'),
        ('store_id', 'INTEGER'),
    ],
    'daily_sales': [
        ('metrics_json', 'TEXT'),
        ('pack_id', 'VARCHAR(50)'),
        ('store_id', 'INTEGER'),
    ],
    'quiz_records': [
        ('pack_id', 'VARCHAR(50)'),
        ('store_id', 'INTEGER'),
    ],
    # P7-B2：产品清单随包（NULL = 默认包/历史数据）
    'products': [
        ('pack_id', 'VARCHAR(50)'),
        ('store_id', 'INTEGER'),
    ],
    'metric_defs': [
        ('store_id', 'INTEGER'),
    ],
    # PH2-6：一店一当前包（NULL = 跟随全局激活包）
    'stores': [
        ('active_pack_id', 'VARCHAR(50)'),
    ],
}

# 内置兜底产品清单（奶茶演示基线）——仅在文件包 products.json 不可得时兜底，
# 保证演示流水/白名单不会指向不存在的产品（与 industry_packs/milk_tea/products.json 同值）
_LEGACY_MILK_TEA_PRODUCTS = (
    ('珍珠奶茶', 12.00, 4.50, '奶茶'),
    ('杨枝甘露', 18.00, 6.80, '果茶'),
    ('烧仙草', 15.00, 5.60, '奶茶'),
    ('柠檬水', 8.00, None, '果茶'),
    ('芋泥波波', 16.00, 6.00, '奶茶'),
)


def _num(value):
    """JSON 数值宽松转换（非法/缺失返回 None，不抛异常）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pack_product_rows(pack_id):
    """读某包的产品模板；默认包读不到时回退内置奶茶清单（防演示数据断链）。"""
    from src import industry_pack
    rows = industry_pack.pack_products_of(pack_id)
    if not rows and pack_id == industry_pack.DEFAULT_PACK_ID:
        print(f'⚠️  包 {pack_id} 未读到产品模板，回退内置奶茶产品清单')
        return [{'product_name': name, 'unit_price': price,
                 'cost_price': cost, 'category': cat}
                for name, price, cost, cat in _LEGACY_MILK_TEA_PRODUCTS]
    return rows


def _ensure_pack_products():
    """按行业包补齐产品清单（P7-B2，幂等）。

    ① 存量回填：`pack_id` 为 NULL 的产品归属默认包（与 analytics
       「NULL = 默认包历史数据」口径一致）；
    ② 增量补齐：每个内置包缺失的产品逐个插入，**不覆盖**既有价格/成本/上下架态。

    product_name 目前为全局唯一，跨包重名会跳过并告警（约束改造见
    06-constraints.md 的 P7-C 同族议题）。
    """
    from src import industry_pack
    from src import tenant
    default_id = industry_pack.DEFAULT_PACK_ID
    _default_store = tenant.default_store()
    default_store_id = _default_store.id if _default_store else None
    backfilled = Product.query.filter(Product.pack_id.is_(None)).update(
        {'pack_id': default_id}, synchronize_session=False)
    added, skipped = 0, 0
    for pack_id in (industry_pack.list_pack_ids() or [default_id]):
        existing = {p.product_name
                    for p in Product.query.filter_by(pack_id=pack_id).all()}
        for row in _pack_product_rows(pack_id):
            name = row['product_name']
            if name in existing:
                continue
            # PH2-2B：重名判定收敛到门店范围（跨店同名产品合法共存）
            if Product.query.filter_by(store_id=default_store_id,
                                       product_name=name).first():
                print(f'⚠️  产品「{name}」在本门店已存在，跳过包 {pack_id}')
                skipped += 1
                continue
            db.session.add(Product(
                pack_id=pack_id,
                store_id=default_store_id,
                product_name=name,
                unit_price=(_num(row['unit_price']) or 0.0),
                cost_price=_num(row['cost_price']),
                category=row['category'] or None,
            ))
            existing.add(name)
            added += 1
    db.session.commit()
    print(f'✅ 产品清单按包补齐：回填 {backfilled} 行 / 新增 {added} 行'
          + (f' / 跳过重名 {skipped} 行' if skipped else ''))
    return {'changed': bool(backfilled or added),
            'detail': f'回填 {backfilled} / 新增 {added} / 跳过 {skipped}'}


@contextlib.contextmanager
def _atomic_ddl():
    """把一批 DDL 变成**真**事务：要么全成，要么全滚。

    为什么不用 `db.engine.begin()`（2026-09-25 第 17 轮实测，写下来防下一个人重踩）：
    Python 官方 sqlite3 的 legacy 事务控制会在执行 DDL 前**隐式提交**当前事务，
    于是 SQLAlchemy 的 begin() 包不住 ALTER/CREATE/DROP ——
    实测「第二条 DDL 语法报错，第一条 ALTER 照样留在库里」（我上一版就误以为包住了，
    是 L4 的原子性断言把它抓回来的）。
    正解：把这条连接切成 AUTOCOMMIT（等价于 driver 的 `isolation_level=None`，
    不再做隐式管理），再自己发 `BEGIN IMMEDIATE` / `COMMIT`，失败时显式 ROLLBACK。

    两条硬约束（违反会得到 database is locked，而不是"慢一点"）：
    ① 开事务前先结掉 ORM 会话的事务 —— 它持有的读锁会让本事务的 COMMIT 失败；
    ② 事务内**不得再用第二条连接读结构** —— 所以本块所需的元数据必须先读齐。
    """
    db.session.commit()
    db.session.close()
    conn = db.engine.execution_options(isolation_level='AUTOCOMMIT').connect()
    try:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        yield conn
        conn.exec_driver_sql('COMMIT')
    except Exception:
        with contextlib.suppress(Exception):
            conn.exec_driver_sql('ROLLBACK')
        raise
    finally:
        conn.close()


def _ensure_columns():
    """为旧库补齐 P2 新增列（幂等；--if-empty 复用已初始化库时调用）。

    create_all 只建缺表、不补既有表的新列；免费托管/存量库走 --if-empty
    时若缺列会让 ORM SELECT 直接报错。此处逐列探测并 ALTER TABLE 补齐，
    SQLite / MySQL 语法兼容；全部允许 NULL，旧行无需回填。

    第 17 轮 H27：改为「先规划、再在 `_atomic_ddl()` 的单个事务内执行全部 ALTER」。
    旧写法逐列 `db.session.execute` + 末尾 commit，中途抛错会留下"补了一半列"的库。
    （原子性为何不能靠 engine.begin()，见 `_atomic_ddl` 文档头。）

    Returns:
        dict: {'changed': bool, 'detail': str}，供迁移账本记 mode/detail。
    """
    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    plan = []
    for table, columns in _ENSURE_COLUMNS.items():
        if not insp.has_table(table):
            continue
        existing = {c['name'] for c in insp.get_columns(table)}
        plan += [(table, col_name, ddl_type)
                 for col_name, ddl_type in columns if col_name not in existing]
    if not plan:
        return {'changed': False, 'detail': '无缺列'}
    with _atomic_ddl() as conn:
        for table, col_name, ddl_type in plan:
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col_name} {ddl_type}'))
            print(f'✅ 旧库迁移：{table} 补齐列 {col_name}')
    return {'changed': True, 'detail': f'补齐 {len(plan)} 列'}


def _adopt_orphan_rebuild(table, rebuild_name, insp):
    """把中断遗留的 `{table}_rebuild` 收编为正式表（第 10 轮 H17）。

    只在正式表已不存在、而中间态 rebuild 表带着数据留在库里时调用。
    这条分支的存在理由不是「让迁移跑成功」，而是**让已经发生的中断可见且可修**：
    此前 `if not insp.has_table(table): continue` 把「无需迁移」与
    「库处于我处理不了的状态」塌缩成同一个静默动作，实测该状态下迁移层零输出、
    应用则每次查询都抛 no such table。

    Returns:
        str: 'recovered' 已收编 / 'none' 无中间态 / 'unsafe' 有中间态但列集合
             与 ORM 不符 —— 此时**禁止自动动表**（猜错的代价是丢数据），交人工。
    """
    from sqlalchemy import text as _text

    names = set(insp.get_table_names())
    if rebuild_name not in names:
        return 'none'
    want = {c.name for c in db.metadata.tables[table].columns}
    got = {c['name'] for c in insp.get_columns(rebuild_name)}
    if got != want:
        print(f'⚠️ 中间态 {rebuild_name} 列集合与 ORM 不一致，拒绝自动收编：'
              f'仅中间表有={sorted(got - want)} 仅 ORM 有={sorted(want - got)}')
        return 'unsafe'
    with db.engine.begin() as conn:
        conn.execute(_text(f'ALTER TABLE {rebuild_name} RENAME TO {table}'))
    print(f'⚠️ 检出上次迁移中断遗留的 {rebuild_name}，已收编为 {table}'
          '（行数据未丢，缺失索引由调用方随后补齐）')
    return 'recovered'


def _rebuild_store_scoped_uq():
    """PH2-2B：唯一约束收敛到门店范围 —— SQLite 存量库表重建迁移（幂等）。

    SQLite 无法直接修改唯一约束，需「建新表 → 拷数据 → 换名」重建；DDL 由
    models 元数据编译生成（单一真相源，防两处漂移）。仅当探测到旧形状时执行：
      · products：全局 `(product_name)` → 门店内 `(store_id, product_name)`
      · metric_defs：`(pack_id, key)` → `(store_id, pack_id, key)`
      · daily_sales：原四列约束 → 纳入 `store_id`（约束名保持不变）
    新库或已迁移库幂等跳过；「换表后、建索引前」中断的中间态只补索引不重建；
    真表已失而 `{table}_rebuild` 尚存时先收编再补索引（见 `_adopt_orphan_rebuild`）。
    MySQL 存量库不受影响（生产为 SQLite）。

    Returns:
        list[dict]: 逐表处置记录，供调用方汇总打印 —— 迁移层必须能区分
            「无事可做」与「我做不下去」，禁止用静默返回值冒充成功。
    """
    from sqlalchemy import inspect
    from sqlalchemy import text as _text
    from sqlalchemy.schema import CreateTable

    def _old_product(uqs):
        return any(set(uq['column_names']) == {'product_name'} for uq in uqs)

    def _old_metric(uqs):
        return any(set(uq['column_names']) == {'pack_id', 'key'} for uq in uqs)

    def _old_sales(uqs):
        return any({'staff_id', 'record_date', 'product_name'} <= set(uq['column_names'])
                   and 'store_id' not in uq['column_names'] for uq in uqs)

    specs = (
        ('products', _old_product, '产品名唯一性 → 门店范围'),
        ('metric_defs', _old_metric, '指标键唯一性 → 门店+包'),
        ('daily_sales', _old_sales, '流水唯一约束纳入门店'),
    )
    actions = []
    for table, is_old, label in specs:
        # 每张表重取 inspector：本函数自身持续执行 DDL（建表/DROP/RENAME/建索引），
        # 复用同一 Inspector 会读到过期快照（实测：刚建的表旧对象仍报 has_table=False），
        # 收编孤儿 rebuild 表之后尤其必须重取，否则后续判定全部建立在旧结构上。
        insp = inspect(db.engine)
        rebuild_name = f'{table}_rebuild'
        if not insp.has_table(table):
            adopted = _adopt_orphan_rebuild(table, rebuild_name, insp)
            actions.append({'table': table, 'action':
                            'orphan_recovered' if adopted == 'recovered'
                            else 'orphan_unsafe' if adopted == 'unsafe'
                            else 'absent'})
            if adopted != 'recovered':
                continue
            insp = inspect(db.engine)      # 收编改变了结构，快照作废
        tbl = db.metadata.tables[table]
        expected_indexes = {ix.name: ix for ix in tbl.indexes}
        actual_indexes = {ix['name'] for ix in insp.get_indexes(table)}
        missing_indexes = [ix for name, ix in expected_indexes.items()
                           if name not in actual_indexes]
        uqs = insp.get_unique_constraints(table) or []
        if not is_old(uqs):
            if missing_indexes:
                # 上次迁移在「换表后、建索引前」中断：只补索引，不重建表。
                with _atomic_ddl() as conn:
                    for ix in missing_indexes:
                        ix.create(conn)
                print(f'✅ 旧库迁移：补齐 {table} 索引 {len(missing_indexes)} 个')
            actions.append({'table': table,
                            'action': ('indexes_repaired' if missing_indexes else 'skip'),
                            'indexes': len(missing_indexes)})
            continue
        cols = ', '.join(c['name'] for c in insp.get_columns(table))
        create_sql = str(CreateTable(tbl).compile(db.engine)).replace(
            f'CREATE TABLE {table}', f'CREATE TABLE {rebuild_name}', 1)
        # 第 17 轮 H27：本表的全部 DDL/DML 走 `_atomic_ddl()` 的一个真事务。
        # 旧写法是「session 执行到换名 → commit 释锁 → 新连接建索引」，中间那个 commit
        # 就是取证在案的「表已换、索引未建」窗口：崩在窗口里库就半新半旧，
        # 只能靠 `_adopt_orphan_rebuild` 事后收编。原子性为何不能靠 engine.begin()
        # （pysqlite 执行 DDL 前隐式提交，实测包不住），见 `_atomic_ddl` 文档头。
        with _atomic_ddl() as conn:
            conn.execute(_text(f'DROP TABLE IF EXISTS {rebuild_name}'))
            conn.execute(_text(create_sql))
            conn.execute(_text(f'INSERT INTO {rebuild_name} ({cols}) '
                               f'SELECT {cols} FROM {table}'))
            conn.execute(_text(f'DROP TABLE {table}'))
            conn.execute(_text(f'ALTER TABLE {rebuild_name} RENAME TO {table}'))
            for ix in expected_indexes.values():
                conn.execute(_text(f'DROP INDEX IF EXISTS {ix.name}'))
                ix.create(conn)
        print(f'✅ 旧库迁移：{table} {label}（表重建完成，单事务）')
        actions.append({'table': table, 'action': 'rebuilt', 'label': label})
    return actions


# 兼容旧调用名（历史代码/记忆引用）
_rebuild_daily_sales_uq = _rebuild_store_scoped_uq


# ═══════ 迁移补丁注册表 + 执行账本（第 17 轮 H27）═══════
# 对标：Ghost `migrations` + `migrations_lock`、Metabase `DATABASECHANGELOG`
# （+ `validCheckSum` 防"跑过了又被改"）、ToolJet `migrationsTransactionMode:'all'`、
# Medusa `mikro_orm_migrations` 的 getExecuted/getPending 成对 API。
# 为什么值得加：此前 4 个补丁的执行顺序由 init_db 里四处硬编码调用串决定，
# "这个库升到第几步"不可查询，崩在第几个补丁也不可知 —— 结构探测只能回答
# "现在缺不缺"，回答不了"曾经跑过没有"。账本补的正是后者。
# 明确不做（与本轮靶维度一致，避免假装有机制）：
#   · 不引入 Alembic/Liquibase：单环境单库无收益（在册结论 M46，若将来多环境则直接采用）；
#   · 不做 schema downgrade：回滚走 backups/ 文件级还原；
#   · 不加迁移锁：红线禁多 worker/多实例，SQLite 单连接写者（n8n 同源前提）。
from dataclasses import dataclass

from src import schema_ledger


@dataclass(frozen=True)
class PatchSpec:
    """注册表条目 = 声明（`src/schema_ledger.PatchRecord`）+ 执行体。

    `pid` 是**账本主键**，一旦发布不得改名（改名 = 存量库历史变孤儿账本 + 该补丁重跑）。
    """

    pid: str
    title: str
    kind: str          # ddl = 改结构；data = 只动数据行
    fn: object         # 执行体；返回 {'changed': bool, 'detail': str} 或 None（无法自证改没改）


def _patch_store_scoped_uq():
    acts = _rebuild_store_scoped_uq()
    done = [a for a in acts if a['action'] not in ('skip', 'absent')]
    for a in done:
        print(f"✅ 唯一约束迁移处置：{a['table']} → {a['action']}")
    blocked = [a['table'] for a in acts if a['action'] == 'orphan_unsafe']
    if blocked:
        print(f'🔴 中间态需人工确认（未自动动表）：{", ".join(blocked)}')
    if not done and not blocked:
        print('✅ 唯一约束迁移：三表均已是新形状，无需处置')
    return {'changed': bool(done),
            'detail': f'处置 {len(done)} 项' + (f'；待人工 {len(blocked)} 表' if blocked else '')}


def _patch_default_tenant():
    from src import tenant
    res = tenant.ensure_default_tenant()
    backfilled = res['backfilled']
    # `backfilled` 是按维度计的字典（全 0 也算"回填过 0 行"）——直接判真值会把
    # "什么都没改"记成"改过"，账本的 mode 就失去意义，故取各维度之和。
    total = sum(v for v in backfilled.values() if isinstance(v, int))
    detail = '回填 ' + ' + '.join(f'{k}={v}' for k, v in backfilled.items() if v) \
        if total else '默认商家/门店已存在，零回填'
    print(f'✅ 多租户迁移：默认商家 #{res["merchant"].id} / '
          f'默认门店 #{res["store"].id}，{detail}')
    return {'changed': bool(total), 'detail': detail}


def _patch_cost_snapshot():
    """p005（DDL）：daily_sales 补成本快照两列 + 建 change_log 表。

    `create_all()` 只建缺表、不给既有表补列，所以两列必须走 ALTER；
    `change_log` 是新表，create_all 会建，但存量库走 `--if-empty` 时不一定经过它，
    这里用 `checkfirst=True` 兜住（对标第 17 轮 H27「补丁必须幂等，跑两次安全」）。
    """
    from sqlalchemy import inspect, text

    from src.models import ChangeLog

    insp = inspect(db.engine)
    plan = []
    if insp.has_table('daily_sales'):
        existing = {c['name'] for c in insp.get_columns('daily_sales')}
        for col_name, ddl_type in (('product_cost', 'NUMERIC(10, 2)'),
                                   ('cost_basis', "VARCHAR(12) NOT NULL DEFAULT 'none'")):
            if col_name not in existing:
                plan.append((col_name, ddl_type))
        has_sales = True
    else:
        has_sales = False
    made_table = not insp.has_table('change_log')
    with _atomic_ddl() as conn:
        for col_name, ddl_type in plan:
            conn.execute(text(f'ALTER TABLE daily_sales ADD COLUMN {col_name} {ddl_type}'))
        if made_table:
            ChangeLog.__table__.create(bind=conn, checkfirst=True)
    names = [f'daily_sales.{c}' for c, _ in plan] + (['表 change_log'] if made_table else [])
    detail = '、'.join(names) or '结构与列已满足'
    if not has_sales and not made_table:
        return {'changed': False, 'detail': 'daily_sales 表不存在（空库），结构由 create_all 负责'}
    return {'changed': bool(plan or made_table), 'detail': detail}


def _patch_cost_backfill():
    """p006（DATA）：给存量流水回填成本快照，并把来源标成 backfill。

    回填值的语义必须钉死：它取的是**回填这一刻**目录里的成本价，不是当年的真实成本。
    第 25 轮已经立过同一条规矩（`origin='backfill'` 的证据不得追认为当时的核对结果），
    这里同一形态：`cost_basis='backfill'` 让看板与门禁都能把它和 'snapshot' 分开算，
    而不是让「后来补的数」冒充「当年的数」。
    """
    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    if not insp.has_table('daily_sales') or not insp.has_table('products'):
        return {'changed': False, 'detail': '表不存在，无可回填'}
    with db.engine.begin() as conn:
        rows = conn.execute(text(
            "UPDATE daily_sales SET product_cost = ("
            "  SELECT p.cost_price FROM products p"
            "   WHERE p.product_name = daily_sales.product_name"
            "     AND (p.store_id = daily_sales.store_id OR p.store_id IS NULL)"
            "   ORDER BY p.store_id DESC LIMIT 1),"
            " cost_basis = 'backfill'"
            " WHERE product_cost IS NULL"
            "   AND EXISTS (SELECT 1 FROM products p"
            "                WHERE p.product_name = daily_sales.product_name"
            "                  AND p.cost_price IS NOT NULL)")).rowcount
    detail = (f'回填 {rows} 行成本快照（来源标 backfill，不冒充当日真实成本）'
              if rows else '无待回填行（新库或已回填过）')
    return {'changed': bool(rows), 'detail': detail}


def _patch_webhook_delivery():
    """p007（DDL）：建出站投递留痕表 `webhook_delivery`（第 30 轮 H82）。

    新表用 `checkfirst=True` ⇒ 存量库首跑 `applied`、复跑 `noop`（对标第 17 轮 H27 幂等口径）。
    只建表不改任何既有列，历史数据零改写。
    """
    from sqlalchemy import inspect

    from src.models import WebhookDelivery

    insp = inspect(db.engine)
    made = not insp.has_table('webhook_delivery')
    with _atomic_ddl() as conn:
        if made:
            WebhookDelivery.__table__.create(bind=conn, checkfirst=True)
    detail = '建表 webhook_delivery（含 delivery_id / next_retry / blocked 三个索引列）' if made \
        else '表 webhook_delivery 已存在'
    return {'changed': made, 'detail': detail}


def _patch_task_takeover():
    """p008（DDL）：任务表补 ``takeover_json`` 列（遗留任务接管链，第 50 轮）。

    只加可空列、不改既有列、不回填（旧行读到 None = 无交接史）；
    存量库首跑 ``applied``、复跑 ``noop``。列级补丁走 ALTER（p001 同族），
    不做表重建 —— 重建会动唯一约束与索引，风险远大于一列之益。
    """
    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    if not insp.has_table('store_tasks'):
        return {'changed': False, 'detail': '表 store_tasks 不存在（建库路径会自带新列）'}
    existing = {c['name'] for c in insp.get_columns('store_tasks')}
    if 'takeover_json' in existing:
        return {'changed': False, 'detail': '列 store_tasks.takeover_json 已存在'}
    with _atomic_ddl() as conn:
        conn.execute(text('ALTER TABLE store_tasks ADD COLUMN takeover_json TEXT'))
    return {'changed': True, 'detail': '补齐列 store_tasks.takeover_json'}


# 名单的唯一真相源 = src/schema_ledger.PATCHES（`/healthz` 与门禁共用同一份声明，
# 见该模块文档头「为什么声明不放 init_db」）。执行体按 pid 绑定：漏绑或多绑都在
# import 期直接 KeyError（响），不允许"声明里有、实现里没有"这种要靠人眼发现的漂移。
_PATCH_IMPLS = {
    'p001_ensure_columns': _ensure_columns,
    'p002_store_scoped_uq': _patch_store_scoped_uq,
    'p003_default_tenant': _patch_default_tenant,
    'p004_pack_products': _ensure_pack_products,
    'p005_cost_snapshot': _patch_cost_snapshot,
    'p006_cost_backfill': _patch_cost_backfill,
    'p007_webhook_delivery': _patch_webhook_delivery,
    'p008_task_takeover': _patch_task_takeover,
}

_PATCHES = tuple(PatchSpec(r.pid, r.title, r.kind, _PATCH_IMPLS[r.pid])
                 for r in schema_ledger.PATCHES)

PATCH_IDS = schema_ledger.PATCH_IDS


# ── 反向动作（第 35 轮 H128）：只允许登记 rev=='auto' 的补丁 ────────────────────
def _revert_cost_backfill():
    """p006 的反向动作：把带 `backfill` 标记的行退回**补丁前的真实状态**，不多不少。

    能精确撤的唯一理由就是当初留了标记（`cost_basis='backfill'`）；
    没有标记的回填无法反查"哪些行是补的"，那类补丁只能声明 manual/none。
    这条区分就是本档与 Django `RunPython(reverse_code=...)` 的同源处。

    ⚠️ 目标值不是 NULL 而是 `'none'`：`cost_basis` 列 NOT NULL DEFAULT 'none'
    （见 `src/models.py` 的 `cost_basis` 定义与 p005 的 ALTER），补丁前的行本来就是
    `product_cost=NULL` + `cost_basis='none'`。第一版这里写 `cost_basis=NULL`，
    被 L4 S41 的真演习当场撞成 `IntegrityError: NOT NULL constraint failed: daily_sales.cost_basis`
    ⇒ **"声明 auto"的前提是反向动作真跑得通**，否则那个档位本身就是第二句假话（第 35 轮一手）。
    """
    from sqlalchemy import inspect, text

    if not inspect(db.engine).has_table('daily_sales'):
        return {'changed': False, 'detail': 'daily_sales 表不存在，无可撤销'}
    with db.engine.begin() as conn:
        rows = conn.execute(text(
            "UPDATE daily_sales SET product_cost = NULL, cost_basis = 'none'"
            " WHERE cost_basis = 'backfill'")).rowcount
    return {'changed': bool(rows),
            'detail': f"撤回填 {rows} 行（product_cost 置回 NULL、cost_basis 退回补丁前的 'none'）"}


_PATCH_REVERSERS = {
    'p006_cost_backfill': _revert_cost_backfill,
}


def rollback_patch(pid, confirmed=False, verbose=True):
    """撤一条补丁：动库前先落可核锚点，撤完把账本行摘掉（= 这个库重新欠着它）。

    三档语义对齐 `django/db/migrations/operations/special.py:205-206`：
    缺反向实现时**必须报错**，不许安静跳过。所以这里只有 `rev='auto'` 才真动手；
    `manual`/`none` 一律拒绝并打印该档的 `rev_note`，把人指向 `restore_db.py` 的整库回退，
    而不是造出"看起来撤了、其实只删了账本行"的第二种假账。

    撤成功后调 `schema_ledger.unrecord_pid`（`record_pid` 的反向），于是
    `apply_patches()` 会把它当未跑补丁重新执行一遍 —— 幂等闭环是"撤干净了"的自证。
    """
    rec = next((r for r in schema_ledger.PATCHES if r.pid == pid), None)
    if rec is None:
        raise KeyError(f'未知补丁 {pid!r}：声明名单里没有，拒绝执行')
    if not confirmed:
        # 前置在**读账本之前**：确认位是调用方契约，不该"这次恰好不用写"就免检
        # （否则同一行命令在"已跑过/没跑过"两种库上行为不同，人学到的是错的）。
        raise ValueError(f'撤 {pid} 是破坏性写库动作，必须显式带 --yes 才执行（当前未带）')
    logged = schema_ledger.logged_rows()
    if pid not in logged:
        if verbose:
            print(f'ℹ️ {pid} 账本里没有记录 ⇒ 本库未跑过它，无需撤（这是合法态，不是失败）')
        return {'pid': pid, 'action': 'not-applied', 'rev': rec.rev}
    if rec.rev != 'auto':
        raise NotImplementedError(
            f'{pid} 声明为 rev={rec.rev!r}，不可自动撤。原因：{rec.rev_note}。'
            '要回退请先用 restore_db.py 把库载回该补丁执行前的备份锚点。')
    impl = _PATCH_REVERSERS.get(pid)
    if impl is None:
        # 上面已按声明放行，这里只可能"声明 auto 但没绑实现"——门禁第 50 项也该拦住，
        # 但真跑起来时必须在这里响，不能拿 .get() 的 None 去当"没活干"。
        raise KeyError(f'{pid} 声明 rev=auto 却不在 _PATCH_REVERSERS 里（绑定漏了）')
    path = str(db.engine.url.database or '')
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f'取不到在用库文件（url={db.engine.url!r}）⇒ 不建锚点就不动手')
    import restore_db

    ts = restore_db.wall_ts()          # 业务钟面 14 位（第 36 轮：旧写法既绕过第 43 项又与账本差 8h）
    anchor, why = restore_db.pre_rollback_copy(path, ts, tag='pre-rollback', tool='init_db.rollback')
    if not anchor:
        raise RuntimeError(f'回滚锚点建不出来（{why}）⇒ 拒绝在无锚点状态下动库')
    if verbose:
        print(f'🔒 回滚锚点已建：{os.path.basename(anchor)}（含证据卡）')
    result = impl() or {}
    removed = schema_ledger.unrecord_pid(pid)
    if verbose:
        print(f"↩️ 已撤 {pid}：{result.get('detail', '（实现未回报）')}")
        print('   账本已摘除该行 ⇒ 下一次 `python init_db.py --if-empty` 会重新执行它（幂等自证）')
    return {'pid': pid, 'action': 'reverted', 'rev': rec.rev, 'detail': result.get('detail', ''),
            'changed': bool(result.get('changed')), 'anchor': anchor, 'unrecorded': removed}


def rollback_via_backup(pid, backup=None, confirmed=False, verbose=True, max_age_h=72, root=None):
    """`manual`/`none` 档的**一条命令**整库回退：挑一份早于该补丁的可用备份载回（M104）。

    为什么需要这一件（2026-09-28 第 36 轮实测）：第 35 轮把 7 条补丁的档位如实分成
    `auto 1 / manual 4 / none 2`，于是**事故现场 6/7 的情况要人自己敲两条命令**
    （先 `restore_db.py --backup <挑一份>`、再确认账本里那行没了）。"记不住就等于没有"
    是第 33 轮那条"没恢复过的备份不算备份"的续集：能力在，但出口不是一个动作。

    上游对照（本轮 `gh api` 实取，落 `D:/tmp/bench36/`）：
      discourse `lib/backup_restore/backup_store.rb:27 latest_file` = `files.first`
        —— 选择只是"读已排好形的清单的头"，判定全在造清单时做完；本函数复用
        `restore_db.pick_latest_eligible()`，不在这里重算一遍资格；
      frappe `frappe/utils/backups.py:741 is_file_old()` —— 取不到路径**按"太旧"处理**
        （缺证据即不合格），所以备份时刻或账本 `applied_at` 任一拿不到 ⇒ 拒绝而不是放行。

    三道拒绝（全部先于任何写盘）：未知 pid、未带确认位、`rev='auto'`
    （精确反向就在手边，不该拿整库回退换掉之后的全部业务数据）。
    """
    import restore_db

    from datetime import datetime          # 只在真走回滚这条路时取
    from src.business_calendar import from_utc_naive   # 账本列口径：naive UTC
    here = root or os.path.dirname(os.path.abspath(__file__))   # root 可注入 ⇒ 夹具能在临时备份面演习，不必碰真仓
    rec = next((r for r in schema_ledger.PATCHES if r.pid == pid), None)
    if rec is None:
        raise KeyError(f'未知补丁 {pid!r}：声明名单里没有，拒绝执行')
    if not confirmed:
        raise ValueError(f'--via-backup 是整库覆盖，必须显式带 --yes 才执行（当前未带）')
    if rec.rev == 'auto':
        raise NotImplementedError(
            f'{pid} 声明 rev=auto，有精确反向实现（_PATCH_REVERSERS），拒绝改用整库回退：'
            '载回旧备份会连带丢掉该补丁之后写入的全部业务数据。请用 `--rollback=%s --yes`。' % pid)
    logged = schema_ledger.logged_rows()
    if pid not in logged:
        if verbose:
            print(f'ℹ️ {pid} 账本里没有记录 ⇒ 本库未跑过它，无需回退（合法态，不是失败）')
        return {'pid': pid, 'action': 'not-applied', 'rev': rec.rev}
    applied_at = (logged[pid] or {}).get('applied_at') or ''
    try:
        applied_dt = from_utc_naive(datetime.fromisoformat(applied_at))
    except ValueError:
        raise ValueError(f'账本里 {pid} 的 applied_at={applied_at!r} 不可解析 ⇒ '
                         '无法证明"备份早于补丁"，拒绝整库回退（缺证据不等于合格）') from None

    if backup:
        chosen = os.path.join(here, 'backups', backup) if not os.path.isabs(backup) else backup
        rec_ts = restore_db.name_ts_utc(chosen)
        if rec_ts is None:
            raise ValueError(f'{os.path.basename(chosen)} 文件名里没有可信时刻 ⇒ 拒绝（无法排序就不是"那一份"）')
        if rec_ts >= applied_dt:
            raise ValueError(f'{os.path.basename(chosen)} 的时刻 {rec_ts} **不早于** {pid} 的应用时刻 '
                             f'{applied_dt} ⇒ 这份备份里已经含该补丁，载回它也撤不掉，拒绝')
        if not os.path.isfile(chosen):
            raise FileNotFoundError(f'指定的备份不存在: {chosen}')
    else:
        chosen, picked, population = restore_db.pick_latest_eligible(root=root or here, max_age_h=max_age_h)
        if chosen is None:
            heads = '; '.join('%s→%s' % (r['name'], r['state']) for r in population[:5])
            raise FileNotFoundError(
                f'没有够格的备份可自动选择（人口 {len(population)} 件，前 5 名：{heads or "人口为空"}）'
                f'⇒ 拒绝回退。要指定某一份请显式 --backup <文件名>')
        if verbose:
            print(f'🔎 自动选中 {os.path.basename(chosen)}（时刻 {picked["name_ts"]}，'
                  f'龄 {picked["age_h"]}h，integrity={picked["integrity"]}，人口 {len(population)} 件）')
        if restore_db.name_ts_utc(chosen) >= applied_dt:
            raise ValueError(f'够格的最新备份 {os.path.basename(chosen)} 仍晚于 {pid} 的应用时刻 '
                             f'{applied_dt} ⇒ 载回它撤不掉这条补丁（要撤它得找更早的一份，而更早的那些都不够格）')

    path = str(db.engine.url.database or '')
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f'取不到在用库文件（url={db.engine.url!r}）⇒ 不建锚点就不动手')
    ts = restore_db.wall_ts()
    # 动手**之前**先释放连接：会话/连接池里若还有人占着这个文件，它可能把覆盖前的页
    # 写回去（第 36 轮 L4 S43 抓到的是"载回后账本行复活"，两侧都要放才成立）。
    db.session.remove()
    db.engine.dispose()
    anchor, why = restore_db.pre_rollback_copy(path, ts, tag='pre-rollback-via-backup',
                                               tool='init_db.rollback_via_backup')
    if not anchor:
        raise RuntimeError(f'回滚锚点建不出来（{why}）⇒ 拒绝在无锚点状态下覆盖在用库')
    if verbose:
        print(f'🔒 覆盖前锚点已建：{os.path.basename(anchor)}（含证据卡）')
    rc, doc = restore_db.restore(chosen, path, force=True)
    if rc != restore_db.RC_OK:
        raise RuntimeError(f'载回失败 rc={rc}: {doc.get("error", "（无明细）")} ⇒ 在用库未被改动，锚点仍在 '
                           f'{os.path.basename(anchor)}')
    db.session.remove()                     # 会话可能还占着一条连接：它手里是覆盖前的库镜像
    db.engine.dispose()                   # 目标文件被整体换掉，旧连接不能再用来"验证成功"
    # 两条都要：只 dispose 不 remove 时，仍在会话里的连接会继续读出覆盖前的内容
    # （第 36 轮 L4 S43 真演习当场抓到：载回后账本行"复活"，实为读到了旧镜像）
    rows = schema_ledger.logged_rows()
    if pid in rows:
        # 失败面必须自带归因读数：ORM 看到的 vs 新连接直接读文件看到的，是两种完全不同的缺陷
        # （前者=连接没放干净，后者=载回根本没生效）。只报"仍有"下一轮还得再猜一次。
        import sqlite3 as _sq

        def _raw(where):
            try:
                con = _sq.connect(f'file:{where}?mode=ro', uri=True)
                try:
                    return con.execute('select count(*) from schema_patch_log where patch_id=?',
                                       (pid,)).fetchone()[0]
                finally:
                    con.close()
            except Exception as exc:                # noqa: BLE001 读数取不到也要如实带出去
                return f'读不到({type(exc).__name__})'
        raise RuntimeError(
            f'回退后账本里仍有 {pid} ⇒ 这份备份并不早于该补丁（判定被实测否证），'
            f'中止并保留锚点，禁止继续当成功收口。归因读数：ORM 侧在账 {sorted(rows)}；'
            f'新连接直读在用库={_raw(path)}；直读备份={_raw(chosen)}；'
            f'库 sha={restore_db.sha256_file(path)[:12]} 备份 sha={restore_db.sha256_file(chosen)[:12]}')
    out = {'pid': pid, 'action': 'restored-from-backup', 'rev': rec.rev,
           'backup': os.path.basename(chosen), 'backup_sha256': doc.get('backup_sha256', ''),
           'anchor': anchor, 'ledger_now': sorted(rows), 'row_total': doc.get('row_total', 0)}
    if verbose:
        print(f"↩️ 已用 {os.path.basename(chosen)} 载回在用库：{out['row_total']} 行 / "
              f'{len(out["ledger_now"])} 条补丁记录在账，{pid} 已不在账本里')
        print('   下一次 `python init_db.py --if-empty` 会从这条补丁起重放（幂等闭环自证）')
    return out


# 账本查询一律走 src/schema_ledger —— 同一判断只留一处实现
# （`/healthz`、门禁第 24 项、部署后校验读的是同一个函数，不会各算各的）


def _record(spec, mode, detail):
    """账本单独一个事务写，且**在补丁事务之后**：中间崩溃 = 补丁做了但没记账，
    下次重跑时补丁自身幂等（结构已满足 → mode=noop），账本自动补齐，不会重复改结构。
    这是 Ghost「迁移必须幂等，跑两次安全」要求的落地形态。写库动作收在
    `src/schema_ledger.record_pid`（全新库的整册补记走同一函数，防两处写法漂移）。"""
    schema_ledger.record_pid(spec.pid, spec.title, spec.kind, mode, detail)


def apply_patches(verbose=True):
    """按注册表顺序执行「账本里没有」的补丁并记账。

    Returns:
        list[dict]: 逐补丁处置（action ∈ recorded|run|failed）。
    Raises:
        Exception: 补丁本体失败时原样抛出（调用方不得吞——中断必须可见），
            抛之前先打印「停在第几个补丁」，让下一次接手的人知道从哪查。
    """
    rows = schema_ledger.logged_rows()
    out = []
    for spec in _PATCHES:
        if spec.pid in rows:
            out.append({'patch': spec.pid, 'action': 'recorded', 'mode': rows[spec.pid]['mode']})
            continue
        try:
            info = spec.fn() or {}
        except Exception as exc:      # noqa: BLE001  不吞异常，只补一条可归因信息后原样抛
            print(f"🔴 迁移中断：停在 {spec.pid}（{spec.title}）→ {type(exc).__name__}: {exc}")
            print('   账本未记该条，重跑 `python init_db.py --if-empty` 会从这一条继续。')
            out.append({'patch': spec.pid, 'action': 'failed'})
            raise
        changed = info.get('changed')
        mode = 'applied' if (changed is None or changed) else 'noop'
        _record(spec, mode, str(info.get('detail', '')))
        if verbose:
            print(f"✅ 迁移补丁 {spec.pid} → {mode}"
                  + (f"（{info.get('detail')}）" if info.get('detail') else ''))
        out.append({'patch': spec.pid, 'action': 'run', 'mode': mode})
    return out



# M20（2026-09-24 第 10 轮）：原 `_seed_metric_defs()` 已删除。
# 删除理由（当场实测）：`grep -rn "_seed_metric_defs" ` 在三副本内**只有定义、零调用点**；
# 指标定义的 DB 覆盖层实际由配置后台 `pack_store.save_metrics` 写入，运行时读取走
# `industry_pack.pack_metrics()` 三级回退。留着一个"看起来像播种正主"的死函数，
# 下一个改代码的人会以为改它就能改种子数据 —— 这是维护状态差距的实体形态。
# 需要恢复：`git show 509679b:iCAN大学生创新创业大赛/init_db.py`（删除前最后一次提交）。


def init_db(if_empty=False):
    """建表并播种演示数据。

    Args:
        if_empty: 为 True 时启用幂等模式——库中已存在 admin 表则直接返回，
            不执行 drop_all，避免每次启动清空既有业务数据（生产/免费托管用）。
    """
    app = create_app('development')
    with app.app_context():
        if if_empty and _table_exists('admin'):
            # 幂等模式：库已存在 → 先补建新表（create_all 只建缺表、不动既有表，
            # P4 的 industry_packs/pack_items 在存量库上即由此补齐），再按注册表跑补丁
            db.create_all()
            _acts = apply_patches()
            _ran = [a for a in _acts if a['action'] == 'run']
            print(f'✅ 迁移账本：声明 {len(_PATCHES)} 条 / 本次执行 {len(_ran)} 条 / '
                  f'跳过已记 {len(_acts) - len(_ran)} 条')
            print('✅ 检测到数据库已初始化（admin 表存在），幂等模式跳过重建，数据保持不变')
            return
        # 全量初始化与平台重置共用同一播种服务，避免两套数据逻辑漂移。
        from src import pack_demo
        demo = pack_demo.reset_demo_database(days=pack_demo.DEMO_DAYS)
        print(f'✅ 演示数据：{demo["stores"]} 门店 × '
              f'{demo["date_start"]}~{demo["date_end"]}，'
              f'流水 {demo["sales"]} 条 + 培训记录 {demo["quiz"]} 条'
              f'（异常案例 {demo["abnormal"]} 条）')
        print(f'✅ 演示账号：平台 {pack_demo.DEMO_PLATFORM_USER} / '
              f'老板 {pack_demo.DEMO_OWNER_USER} / 七店长 / 十四店员')
        print('🎉 数据库初始化完成！')


_USAGE = """用法:
  python init_db.py              # 默认模式：备份后重建七门店演示库
  python init_db.py --if-empty  # 幂等模式：已有库只做增量迁移
  python init_db.py --rollback=p006_cost_backfill --yes
                                # 撤一条声明为 rev=auto 的补丁（动库前先落回滚锚点）
  python init_db.py --rollback=p005_xxx --via-backup --yes
                                # rev=manual/none 档：自动挑一份**早于该补丁**的够格备份载回整库
  python init_db.py --rollback=p005_xxx --via-backup --backup=ican_20260927_035845.db --yes
                                # 同上但点名某一份（时刻不早于该补丁的应用时刻即拒）
  python init_db.py --help      # 查看帮助，不读取或修改数据库
"""

_RC_USAGE, RC_OK, RC_REFUSED = 2, 0, 1


def _rollback_cli(spec, confirmed, app_factory=None, via_backup=False, backup=None):
    """`--rollback=<pid>` 入口：起 app context 后调 `rollback_patch`，失败要说清是谁拒的。

    拒绝（rev 非 auto / 缺 --yes / 未知 pid / 锚点建不出来）**必须非零退出且不写库**，
    而不是打一行字然后返回 0 —— 那是"没做成但看起来做成了"的形态。

    `app_factory` 是可注入的接缝：`create_app('development')` 的库路径写死指向
    `data/ican.db`，测试若直接调本函数就会去碰开发库；留一个缝，测试可以带临时库的 app 进来。
    `via_backup` 走第 36 轮的整库回退出口（`manual`/`none` 档才有意义，见 `rollback_via_backup`）。
    """
    pid = spec.split('=', 1)[1].strip() if '=' in spec else ''
    if not pid:
        print('❌ --rollback= 后面必须给补丁 pid（形如 --rollback=p006_cost_backfill）')
        print(_USAGE)
        return _RC_USAGE
    app = (app_factory or create_app)('development')
    with app.app_context():
        try:
            if via_backup:
                rollback_via_backup(pid, backup=backup, confirmed=confirmed)
            else:
                rollback_patch(pid, confirmed=confirmed)
        except (KeyError, ValueError, NotImplementedError, FileNotFoundError,
                RuntimeError) as exc:
            head = exc.args[0] if exc.args else type(exc).__name__
            print(f'❌ 回滚未执行：{type(exc).__name__}: {head}')
            return RC_REFUSED
    return RC_OK


def main(argv=None):
    """命令行入口：显式处理帮助和未知参数，防止误触默认重建。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ('-h', '--help') for arg in args):
        print(_USAGE)
        return 0
    roll = [a for a in args if a.startswith('--rollback')]
    via_backup = '--via-backup' in args
    bname = [a for a in args if a.startswith('--backup=')]
    unknown = [a for a in args
               if a not in ('--if-empty', '--yes', '--via-backup')
               and not a.startswith('--rollback=') and not a.startswith('--backup=')]
    if unknown:
        print(f'❌ 未知参数：{" ".join(unknown)}')
        print(_USAGE)
        return _RC_USAGE
    if len(bname) > 1:
        print(f'❌ --backup= 只准给一个（收到 {len(bname)} 个）：一份备份就是一个明确的回退点')
        return _RC_USAGE
    if bname and not via_backup:
        print('❌ --backup= 只在 --via-backup 这条路上有意义（两个都缺就是没说要回退什么）')
        return _RC_USAGE
    if via_backup and not roll:
        print('❌ --via-backup 必须与 --rollback=<pid> 同用（整库回退也得先说要撤哪一条，才有"早于谁"的判据）')
        return _RC_USAGE
    if roll:
        if len(roll) > 1:
            print(f'❌ 一次只准撤一条补丁（收到 {len(roll)} 条）')
            return _RC_USAGE
        if '--if-empty' in args:
            print('❌ --rollback 与 --if-empty 互斥：一个是撤，一个是补')
            return _RC_USAGE
        return _rollback_cli(roll[0], '--yes' in args, via_backup=via_backup,
                             backup=bname[0].split('=', 1)[1].strip() if bname else None)
    init_db(if_empty='--if-empty' in args)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
