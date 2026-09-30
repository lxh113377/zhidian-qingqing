"""演示租户数据播种、全库重置与单包重置。

核心约束（PH3-0）：
  - 一个商家、七家门店，每店绑定一个行业包。
  - 每店 1 名店长、2 名店员；账号使用行业语义命名。
  - 默认重建执行日往前 29 天至执行日，共 30 个自然日。
  - 每店每天 10 条产品流水（两名店员各 5 条），共 2100 条。
  - 15 个产品在 30 天内各出现 20 天；每店 10 条培训记录。
  - 最后两天每店各 1 条异常损耗，共 14 条。

CLI：
    python -m src.pack_demo --reset-all --days 30 --json
    python -m src.pack_demo --pack bakery --reset
    python -m src.pack_demo --pack bakery
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sqlite3
import sys
import zlib
from datetime import date, datetime, timedelta

from src import industry_pack
from src import business_calendar as cal
from src.backup_manifest import (STATUS_SUCCESS, STATUS_UNVERIFIABLE,
                                 empty_guard, manifest_path, ro_uri, write_manifest)
from src.models import (
    ROLE_MANAGER,
    ROLE_OWNER,
    ROLE_PLATFORM,
    Admin,
    AdminStoreAccess,
    DailySales,
    Merchant,
    MetricDef,
    Product,
    QuizRecord,
    Staff,
    Store,
    db,
)

DEMO_DAYS = 30
DEMO_SALES_PER_STAFF = 5
DEMO_QUIZ_PER_STORE = 10
DEMO_ABNORMAL_DAYS = 2
RESET_CONFIRM_PHRASE = 'REBUILD-DEMO-DB'
BACKUP_KEEP = 10
BACKUP_PREFIX = 'demo-reset'

DEMO_MERCHANT_NAME = '智电轻擎演示商家'
DEMO_OWNER_USER = 'owner_demo'
DEMO_PLATFORM_USER = 'platform_demo'

# 门店顺序即产品、账号、快照与验收清单的稳定顺序。
DEMO_STORE_SPECS = (
    {
        'pack_id': 'milk_tea',
        'store_name': '奶茶示范店',
        'store_code': 'milk_tea',
        'display_name': '奶茶示范店',
    },
    {
        'pack_id': 'fruit',
        'store_name': '水果生鲜示范店',
        'store_code': 'fruit',
        'display_name': '水果生鲜示范店',
    },
    {
        'pack_id': 'convenience',
        'store_name': '便利店示范店',
        'store_code': 'convenience',
        'display_name': '便利店示范店',
    },
    {
        'pack_id': 'bakery',
        'store_name': '烘焙轻食示范店',
        'store_code': 'bakery',
        'display_name': '烘焙轻食示范店',
    },
    {
        'pack_id': 'beauty',
        'store_name': '美业示范店',
        'store_code': 'beauty',
        'display_name': '美业示范店',
    },
    {
        'pack_id': 'pet',
        'store_name': '宠物服务示范店',
        'store_code': 'pet',
        'display_name': '宠物服务示范店',
    },
    {
        'pack_id': 'apparel',
        'store_name': '服装零售示范店',
        'store_code': 'apparel',
        'display_name': '服装零售示范店',
    },
)

_ABNORMAL_REMAIN = 30
_ABNORMAL_WASTE = 8


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_date(value):
    if value is None:
        return cal.business_today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), '%Y-%m-%d').date()


def _pack_questions(pack):
    inline = pack.get('questions')
    if isinstance(inline, list):
        return [x for x in inline if isinstance(x, dict) and x.get('q')]
    pack_id = pack.get('id')
    rows = industry_pack.load_pack_file_json(pack_id, 'questions.json') if pack_id else None
    return [x for x in rows if isinstance(x, dict) and x.get('q')] if isinstance(rows, list) else []


def _product_plan(day_index, product_count, slots=10):
    """返回当天产品索引；30 天内每个产品恰好出现 20 天。"""
    start = (day_index * slots) % product_count
    return [(start + idx) % product_count for idx in range(slots)]


def _stable_rng(*parts):
    material = '|'.join(str(part) for part in parts)
    return random.Random(zlib.crc32(material.encode('utf-8')))  # noqa: S311  理由：演示数据确定性播种（可复现是设计目标），非密钥路径；真实令牌与盐用 secrets.token_hex


def _store_account_names(pack_id):
    return {
        'manager': f'{pack_id}_manager',
        'staff1': f'{pack_id}_staff1',
        'staff2': f'{pack_id}_staff2',
    }


def demo_accounts():
    """返回演示账号清单（密码仅用于演示环境页面展示）。"""
    rows = [
        {'role': ROLE_PLATFORM, 'username': DEMO_PLATFORM_USER,
         'password': 'platform123', 'store': '—'},
        {'role': ROLE_OWNER, 'username': DEMO_OWNER_USER,
         'password': 'owner123', 'store': '全部 7 家门店'},
    ]
    for spec in DEMO_STORE_SPECS:
        names = _store_account_names(spec['pack_id'])
        rows.extend([
            {'role': ROLE_MANAGER, 'username': names['manager'],
             'password': 'manager123', 'store': spec['store_name']},
            {'role': 'staff', 'username': names['staff1'],
             'password': 'staff123', 'store': spec['store_name']},
            {'role': 'staff', 'username': names['staff2'],
             'password': 'staff123', 'store': spec['store_name']},
        ])
    return rows


def _conn_table_counts(conn):
    """按 sqlite_master 取每表行数（表名非外部输入；只读连接上调用）。"""
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        'ORDER BY name')]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]   # noqa: S608  理由：表名取自 sqlite_master 而非外部输入，双引号包裹防关键字冲突
          for n in names}


def _snapshot_readonly(source, target):
    """在线一致快照 + 源侧行数，返回 src_counts。源库全程只读打开（写反方向会先报错）。"""
    src_conn = sqlite3.connect(ro_uri(source), uri=True)
    try:
        src_counts = _conn_table_counts(src_conn)
        dst_conn = sqlite3.connect(target)
        try:
            src_conn.backup(dst_conn)          # 方向：source → target
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return src_counts


def _verify_copy(target):
    """只读复验副本：返回 (integrity 结果, 副本行数)。"""
    conn = sqlite3.connect(ro_uri(target), uri=True)
    try:
        return conn.execute('PRAGMA integrity_check').fetchone(), _conn_table_counts(conn)
    finally:
        conn.close()


def _sqlite_source_path():
    """当前库的 SQLite 绝对路径；非 SQLite / 无库文件时返回 None（调用方据此跳过备份）。"""
    url = db.engine.url
    if url.get_backend_name() != 'sqlite':
        raise RuntimeError('当前全库演示重建仅支持 SQLite 备份')
    source = url.database
    if not source:
        return None
    source = os.path.abspath(source)
    return source if os.path.isfile(source) else None


def _resolve_backup_dir(source, backup_dir):
    if backup_dir:
        return backup_dir
    db_dir = os.path.dirname(source)
    project_dir = os.path.dirname(db_dir) if os.path.basename(db_dir) == 'data' else db_dir
    return os.path.join(project_dir, 'backups')


def _reject_copy(target, why):
    """副本不可信 ⇒ 删除并拒绝重置（一条口径一个出口，不在三处各写一遍删除逻辑）。"""
    if os.path.exists(target):
        os.remove(target)
    raise RuntimeError(why)


def _seal_backup_evidence(target, source, src_counts, dst_counts, target_dir, keep):
    """第 25 轮：备份成功后必须留证据件；留不下证据就不许往下重置（与"备份失败即拒绝重置"同口径）。"""
    eg_ok, eg_why = empty_guard(dst_counts)
    pruned = _prune_backups(target_dir, keep)
    mok, _mp, mdetail = write_manifest(
        target, source_path=source, src_counts=src_counts, counts=dst_counts, integrity=True,
        status=STATUS_SUCCESS if eg_ok else STATUS_UNVERIFIABLE,
        error='' if eg_ok else (eg_why + '；本次重置无数据可保护，证据件按回填口径记为不可追认'),
        tool='src/pack_demo.py', pruned=pruned)
    if not mok:
        raise RuntimeError(f'备份证据件未写出，已拒绝重置: {mdetail}')
    if not eg_ok:
        logging.getLogger(__name__).warning('[pack_demo] 备份证据件标注：%s', mdetail)


def create_database_backup(backup_dir=None, keep=BACKUP_KEEP):
    """备份当前 SQLite 数据库并校验完整性；无源库时返回 None。

    第 25 轮补的第二条出口守卫：这条路径此前只查 integrity，
    一份 0 表空库照样"备份成功"（本机实测那份 4,096 B 的 demo-reset 备份就是这么留下的）。
    """
    source = _sqlite_source_path()
    if not source:
        return None
    target_dir = _resolve_backup_dir(source, backup_dir)
    os.makedirs(target_dir, exist_ok=True)
    stamp = cal.business_now().strftime('%Y%m%d_%H%M%S_%f')
    target = os.path.join(target_dir, f'{BACKUP_PREFIX}_{stamp}.db')
    try:
        src_counts = _snapshot_readonly(source, target)
    except Exception as exc:
        _reject_copy(target, f'数据库备份失败，已拒绝重置: {exc}')
    result, dst_counts = _verify_copy(target)
    if not result or result[0] != 'ok':
        _reject_copy(target, f'数据库备份完整性校验失败，已拒绝重置: {result}')
    if dst_counts != src_counts:
        bad = sorted({k for k in set(src_counts) | set(dst_counts)
                      if src_counts.get(k) != dst_counts.get(k)})
        _reject_copy(target, f'备份行数与原库不一致，已拒绝重置: {bad[:6]}')
    _seal_backup_evidence(target, source, src_counts, dst_counts, target_dir, keep)
    return target


def _prune_backups(backup_dir, keep=BACKUP_KEEP):
    """仅保留最近 N 份演示重置备份；返回被删份名（保留动作本身要能被追责）。"""
    removed = []
    try:
        keep = max(1, int(keep))
        files = sorted(
            (os.path.join(backup_dir, name) for name in os.listdir(backup_dir)
             if name.startswith(f'{BACKUP_PREFIX}_') and name.endswith('.db')),
            key=os.path.getmtime,
        )
        while len(files) > keep:
            victim = files.pop(0)
            os.remove(victim)
            removed.append(os.path.basename(victim))
            pair = manifest_path(victim)
            if os.path.isfile(pair):
                os.remove(pair)               # 件已删，孤儿证据只会让下一次审计误判
    except OSError:
        return removed
    return removed


def data_stats():
    """当前库关键计数（平台重置页只读概览）。"""
    abnormal = DailySales.query.filter(
        DailySales.abnormal_notes.isnot(None),
        DailySales.abnormal_notes != '',
    ).count()
    return {
        'merchants': Merchant.query.count(),
        'stores': Store.query.count(),
        'accounts': Admin.query.count() + Staff.query.count(),
        'products': Product.query.count(),
        'metrics': MetricDef.query.count(),
        'sales': DailySales.query.count(),
        'quiz': QuizRecord.query.count(),
        'abnormal': abnormal,
    }


def _seed_catalog(pack, store):
    products = industry_pack.pack_products_of(pack['id'])
    for row in products:
        db.session.add(Product(
            product_name=row['product_name'],
            unit_price=_num(row.get('unit_price')),
            cost_price=(
                _num(row.get('cost_price'))
                if row.get('cost_price') not in (None, '') else None
            ),
            category=row.get('category') or None,
            pack_id=pack['id'],
            store_id=store.id,
            is_active=True,
        ))
    for item in pack.get('metrics') or []:
        db.session.add(MetricDef(
            pack_id=pack['id'],
            store_id=store.id,
            key=item['key'],
            label=item['label'],
            value_type=item['value_type'],
            unit=item['unit'],
            role=item['role'],
            agg=item['agg'],
            required=item['required'],
            enabled=True,
            sort=item['sort'],
            options_json=(
                json.dumps({'legacy_column': item['legacy_column']})
                if item.get('legacy_column') else None
            ),
        ))
    return len(products)


def _seed_store_sales(store, pack, staff_rows, days, end_date):
    products = industry_pack.pack_products_of(pack['id'])
    names = [p['product_name'] for p in products]
    prices = {p['product_name']: _num(p.get('unit_price')) for p in products}
    if len(names) != 15:
        raise RuntimeError(f'{pack["id"]} 产品数不是 15，无法按契约播种')
    if days < DEMO_ABNORMAL_DAYS or len(staff_rows) != 2:
        raise RuntimeError('演示播种需要至少 2 天且每店 2 名店员')

    start_date = end_date - timedelta(days=days - 1)
    sales = 0
    abnormal = 0
    for day_index in range(days):
        day = start_date + timedelta(days=day_index)
        picked = _product_plan(day_index, len(names), slots=10)
        for staff_index, staff in enumerate(staff_rows):
            assigned = picked[
                staff_index * DEMO_SALES_PER_STAFF:
                (staff_index + 1) * DEMO_SALES_PER_STAFF
            ]
            for slot, product_index in enumerate(assigned):
                product_name = names[product_index]
                rng = _stable_rng(
                    pack['id'], end_date.isoformat(), store.code, day.isoformat(),
                    staff.username, product_name,
                )
                qty = rng.randint(10, 50)
                remain = rng.randint(30, 80)
                waste = rng.randint(0, 2)
                note = ''
                abnormal_day = day_index >= days - DEMO_ABNORMAL_DAYS
                if abnormal_day and staff_index == 0 and slot == 0:
                    remain, waste = _ABNORMAL_REMAIN, _ABNORMAL_WASTE
                    note = '损耗率异常，打烊复盘'
                    abnormal += 1
                db.session.add(DailySales(
                    staff_id=staff.id,
                    store_id=store.id,
                    record_date=day,
                    pack_id=pack['id'],
                    product_name=product_name,
                    product_sales_qty=qty,
                    total_revenue=round(qty * prices[product_name], 2),
                    raw_material_remaining=str(remain),
                    raw_material_waste=str(waste),
                    abnormal_notes=note,
                ))
                sales += 1
    return sales, abnormal


def _seed_store_quiz(store, pack, staff_rows, end_date):
    questions = _pack_questions(pack)
    if len(questions) < DEMO_QUIZ_PER_STORE:
        raise RuntimeError(f'{pack["id"]} 题库不足 {DEMO_QUIZ_PER_STORE} 题')
    count = 0
    for idx in range(DEMO_QUIZ_PER_STORE):
        item = questions[idx]
        staff = staff_rows[idx % len(staff_rows)]
        weak = (item.get('weak') or '综合').strip()
        ok = idx % 3 != 2
        created = datetime.combine(
            end_date - timedelta(days=DEMO_QUIZ_PER_STORE - 1 - idx), datetime.min.time()
        ) + timedelta(hours=9)
        db.session.add(QuizRecord(
            staff_id=staff.id,
            store_id=store.id,
            pack_id=pack['id'],
            quiz_type='question',
            question=f'【{weak}】{item["q"]}',
            staff_answer=item.get('a') if ok else '（答错示例）',
            ai_feedback=('回答准确，要点完整。' if ok else f'参考答案：{item.get("a")}'),
            is_correct=ok,
            score=100.0 if ok else 0.0,
            created_at=created,
        ))
        count += 1
    return count


def _seed_demo_database(days, end_date):
    """在空表上创建固定演示租户，返回播种计数。"""
    merchant = Merchant(name=DEMO_MERCHANT_NAME, contact_email=None, is_active=True)
    db.session.add(merchant)
    db.session.flush()

    platform = Admin(username=DEMO_PLATFORM_USER, role=ROLE_PLATFORM,
                     merchant_id=None)
    platform.set_password('platform123')
    db.session.add(platform)
    db.session.flush()
    if platform.id != 1:
        raise RuntimeError('平台管理员必须为 Admin.id=1，以保证平台会话重置后仍有效')

    owner = Admin(username=DEMO_OWNER_USER, role=ROLE_OWNER,
                  merchant_id=merchant.id)
    owner.set_password('owner123')
    db.session.add(owner)

    products = sales = quiz = abnormal = 0
    for spec in DEMO_STORE_SPECS:
        pack_id = spec['pack_id']
        pack = industry_pack.get_pack(pack_id)
        if pack.get('id') != pack_id:
            raise RuntimeError(f'行业包不存在或不可用: {pack_id}')
        store = Store(
            merchant_id=merchant.id,
            name=spec['store_name'],
            code=spec['store_code'],
            address='演示数据门店',
            phone=None,
            is_active=True,
            active_pack_id=pack_id,
        )
        db.session.add(store)
        db.session.flush()

        names = _store_account_names(pack_id)
        manager = Admin(username=names['manager'], role=ROLE_MANAGER,
                        merchant_id=merchant.id)
        manager.set_password('manager123')
        db.session.add(manager)
        db.session.flush()
        db.session.add(AdminStoreAccess(admin_id=manager.id, store_id=store.id))

        staff_rows = []
        for idx, username in enumerate((names['staff1'], names['staff2']), start=1):
            staff = Staff(
                username=username,
                display_name=f'{spec["display_name"]}店员{idx}',
                phone=None,
                store_name=store.name,
                merchant_id=merchant.id,
                store_id=store.id,
                is_active=True,
            )
            staff.set_password('staff123')
            db.session.add(staff)
            staff_rows.append(staff)
        db.session.flush()

        products += _seed_catalog(pack, store)
        store_sales, store_abnormal = _seed_store_sales(
            store, pack, staff_rows, days, end_date
        )
        sales += store_sales
        abnormal += store_abnormal
        quiz += _seed_store_quiz(store, pack, staff_rows, end_date)

    return {
        'stores': len(DEMO_STORE_SPECS),
        'accounts': 2 + len(DEMO_STORE_SPECS) * 3,
        'products': products,
        'metrics': MetricDef.query.count(),
        'sales': sales,
        'quiz': quiz,
        'abnormal': abnormal,
    }


def reset_demo_database(days=DEMO_DAYS, end_date=None, backup=True,
                        backup_dir=None, keep=BACKUP_KEEP):
    """备份后清空全库，重建七门店演示租户。"""
    days = int(days)
    if days <= 0:
        raise ValueError('days 必须为正整数')
    end_date = _as_date(end_date)
    db.session.commit()
    backup_path = create_database_backup(backup_dir=backup_dir, keep=keep) if backup else None
    # 全库重建是平台级维护动作：必须在 unscoped() 里做。
    # 否则 `db.session.remove()` 会把当前请求的 current_user 变成游离实例，
    # 播种期间的任意 SELECT 都会走进租户守卫去读它的 role/merchant_id，
    # 抛 DetachedInstanceError 让重置页 500（2026-09-26 由 test_ph3_demo 实测抓到）。
    from src import query_guard
    # H59 静默档：整库重建是平台级动作，2100+ 行播种逐条入审计只会把真正的商家变更埋掉。
    # 关闭必须点名（门禁第 40 项 AST 枚举 silenced() 调用点，名单外会红），
    # 且关闭期间发生的事由下面这一条聚合行留痕，不是"什么都不写"。
    from src import change_log
    with query_guard.unscoped(), change_log.silenced():
        db.session.remove()
        db.drop_all()
        db.create_all()
        counts = _seed_demo_database(days, end_date)
        expected = {
            'stores': len(DEMO_STORE_SPECS),
            'accounts': 2 + len(DEMO_STORE_SPECS) * 3,
            'products': len(DEMO_STORE_SPECS) * 15,
            'metrics': 42,
            'sales': len(DEMO_STORE_SPECS) * days * 2 * DEMO_SALES_PER_STAFF,
            'quiz': len(DEMO_STORE_SPECS) * DEMO_QUIZ_PER_STORE,
            'abnormal': len(DEMO_STORE_SPECS) * min(days, DEMO_ABNORMAL_DAYS),
        }
        if counts != expected:
            db.session.rollback()
            raise RuntimeError(f'演示数据计数不符：expected={expected} actual={counts}')
        db.session.commit()
        industry_pack.invalidate()
        # 第 17 轮 H27：全新库（drop_all + create_all + 播种）结构天然就是最新版，
        # 一个补丁都不需要跑 —— 但**必须整册补记**，否则门禁会把刚建好的干净库
        # 判成"4 个补丁都没应用"，每次 CI 冷启动与平台重置都会无故变红。
        from src import schema_ledger
        schema_ledger.record_all()
        from src import auth_epoch
        auth_epoch.rotate_epoch()
        change_log.note_bulk('daily_sales', 'bulk_seed', counts['sales'],
                             merchant_id=None,
                             detail='platform demo reset: 全库重建 + 七门店演示播种')
    counts.update({
        'ok': True,
        'date_start': (end_date - timedelta(days=days - 1)).isoformat(),
        'date_end': end_date.isoformat(),
        'backup_path': backup_path,
    })
    return counts


def _store_for_pack(pack_id):
    store = Store.query.filter_by(active_pack_id=pack_id).order_by(Store.id).first()
    if store is not None:
        return store
    from src import tenant
    return tenant.default_store()


def _delete_pack_rows(pack_id):
    from src import change_log

    conditions = [DailySales.pack_id == pack_id]
    quiz_conditions = [QuizRecord.pack_id == pack_id]
    if pack_id == industry_pack.DEFAULT_PACK_ID:
        conditions.append(DailySales.pack_id.is_(None))
        quiz_conditions.append(QuizRecord.pack_id.is_(None))
    store = _store_for_pack(pack_id)
    store_id = store.id if store is not None else None
    sales = DailySales.query.filter(*conditions).delete(synchronize_session=False)
    quiz = QuizRecord.query.filter(*quiz_conditions).delete(synchronize_session=False)
    # 核心批量 DELETE 绕开 ORM 单元工作，监听器天然看不见 —— 聚合行是它唯一的留痕
    change_log.note_bulk('daily_sales', 'bulk_delete', sales, store_id=store_id,
                         detail=f'pack={pack_id}')
    change_log.note_bulk('quiz_records', 'bulk_delete', quiz, store_id=store_id,
                         detail=f'pack={pack_id}')
    return sales, quiz


def seed_pack(pack_id, force=False, days=DEMO_DAYS, end_date=None):
    """播种单包演示数据；保留旧 CLI 兼容。"""
    pack = industry_pack.get_pack(pack_id)
    if not pack or pack.get('id') != pack_id:
        return {'pack_id': pack_id, 'ok': False, 'reason': '包不存在或回退默认包'}
    existing = DailySales.query.filter(DailySales.pack_id == pack_id).count()
    if pack_id == industry_pack.DEFAULT_PACK_ID:
        existing += DailySales.query.filter(DailySales.pack_id.is_(None)).count()
    if existing and not force:
        return {'pack_id': pack_id, 'ok': True, 'skipped': True,
                'sales': existing, 'quiz': 0, 'abnormal': 0}
    if force:
        _delete_pack_rows(pack_id)

    store = _store_for_pack(pack_id)
    if store is None:
        return {'pack_id': pack_id, 'ok': False, 'reason': '没有可用门店'}
    staff_rows = Staff.query.filter_by(store_id=store.id).order_by(Staff.id).limit(2).all()
    if len(staff_rows) < 2:
        return {'pack_id': pack_id, 'ok': False, 'reason': '门店店员不足 2 名'}
    end_date = _as_date(end_date)
    # 演示播种同属平台级动作，逐行入审计是噪声不是归因（调用点由门禁第 40 项枚举）
    from src import change_log
    with change_log.silenced():
        sales, abnormal = _seed_store_sales(store, pack, staff_rows, int(days), end_date)
        quiz = _seed_store_quiz(store, pack, staff_rows, end_date)
        db.session.flush()
        change_log.note_bulk('daily_sales', 'bulk_seed', sales, store_id=store.id,
                             merchant_id=store.merchant_id, detail=f'pack={pack_id}')
    return {
        'pack_id': pack_id, 'ok': True, 'skipped': False,
        'sales': sales, 'quiz': quiz, 'abnormal': abnormal,
        'products': len(industry_pack.pack_products_of(pack_id)),
    }


def seed_all(force=False):
    """七包全量播种（旧入口，按门店绑定包逐包执行）。"""
    reports = [seed_pack(spec['pack_id'], force=force) for spec in DEMO_STORE_SPECS]
    return {
        'packs': len(reports),
        'days': DEMO_DAYS,
        'sales': sum(r.get('sales', 0) for r in reports if not r.get('skipped')),
        'quiz': sum(r.get('quiz', 0) for r in reports),
        'abnormal': sum(r.get('abnormal', 0) for r in reports),
        'details': reports,
    }


def reset_pack(pack_id):
    """单包一键重置。"""
    return seed_pack(pack_id, force=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='行业包演示数据播种 / 全库重建')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--pack', help='指定包 id')
    group.add_argument('--reset-all', action='store_true', help='备份后重建七门店全库')
    parser.add_argument('--reset', action='store_true', help='配合 --pack：先删该包再重建')
    parser.add_argument('--days', type=int, default=DEMO_DAYS, help='重置天数（默认 30）')
    parser.add_argument('--end-date', help='结束日期 YYYY-MM-DD（默认今天）')
    parser.add_argument('--json', action='store_true', help='输出 JSON')
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.reset and not args.pack:
        parser.error('--reset 仅可与 --pack 一起使用')

    from src.app import create_app
    app = create_app('development')
    try:
        with app.app_context():
            if args.reset_all:
                report = reset_demo_database(days=args.days, end_date=args.end_date)
            elif args.pack:
                report = (reset_pack(args.pack) if args.reset
                          else seed_pack(args.pack, days=args.days, end_date=args.end_date))
            else:
                report = seed_all(force=args.reset)
    except Exception as exc:
        report = {'ok': False, 'error': str(exc)}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('演示数据操作完成：' + json.dumps(report, ensure_ascii=False))
    return 0 if report.get('ok', True) else 1


if __name__ == '__main__':
    raise SystemExit(main())
