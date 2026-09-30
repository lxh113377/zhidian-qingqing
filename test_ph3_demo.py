"""PH3-0 七门店 30 天演示数据契约与重置安全验收。

门禁状态（第 25 轮 M86 起）：**已纳入阻断链，门禁第 39 项每次全量跑都会执行本文件**。
原豁免理由（"不适合在每次门禁中重建"）实测不成立：整文件 28s，且正是它抓到
`/admin/platform/demo-reset` 的 500（租户守卫读游离 current_user）——豁免等于隐身。
"""
import hashlib
import os
import re
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_fixtures import use_temp_db  # noqa: E402

use_temp_db()

from src import industry_pack, pack_demo, tenant  # noqa: E402
from src.app import create_app  # noqa: E402
from src.models import (  # noqa: E402
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

app = create_app('development')
app.config['WTF_CSRF_ENABLED'] = False
END_DATE = date(2026, 9, 14)
failures = []
checks = []


def chk(name, condition, extra=''):
    checks.append(name)
    if not condition:
        failures.append(f'{name} | {extra}')


def csrf(client, path):
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert match, path
    return match.group(1), html


def login(client, username, password, role='admin'):
    token, _ = csrf(client, '/login')
    return client.post(
        '/login',
        data={'username': username, 'password': password, 'role': role,
              'csrf_token': token},
        follow_redirects=False,
    )


def daily_checksum():
    digest = hashlib.sha256()
    for row in DailySales.query.order_by(DailySales.id).all():
        digest.update((
            f'{row.store_id}|{row.staff_id}|{row.record_date}|{row.product_name}|'
            f'{row.product_sales_qty}|{row.total_revenue}|'
            f'{row.raw_material_remaining}|{row.raw_material_waste}|'
            f'{row.abnormal_notes}\n'
        ).encode())
    return digest.hexdigest()


with app.app_context():
    report = pack_demo.reset_demo_database(
        days=30, end_date=END_DATE.isoformat(), backup=False
    )
    counts = {model.__name__: model.query.count() for model in (
        Merchant, Store, Admin, Staff, Product, DailySales, QuizRecord,
        MetricDef, AdminStoreAccess,
    )}
    expected_counts = {
        'Merchant': 1, 'Store': 7, 'Admin': 9, 'Staff': 14, 'Product': 105,
        'DailySales': 2100, 'QuizRecord': 70, 'MetricDef': 42,
        'AdminStoreAccess': 7,
    }
    chk('重置报告核心计数', all(report[key] == value for key, value in {
        'stores': 7, 'accounts': 23, 'products': 105, 'metrics': 42,
        'sales': 2100, 'quiz': 70, 'abnormal': 14,
    }.items()), str(report))
    chk('数据库 ORM 计数契约', counts == expected_counts,
        f'{counts} != {expected_counts}')

    store_ids = [row.id for row in Store.query.order_by(Store.id).all()]
    per_day = Counter()
    per_day_staff = Counter()
    per_day_products = defaultdict(set)
    per_store_staff = defaultdict(set)
    product_days = defaultdict(set)
    abnormalities = []
    for row in DailySales.query.all():
        per_day[(row.store_id, row.record_date)] += 1
        per_day_staff[(row.store_id, row.record_date, row.staff_id)] += 1
        per_day_products[(row.store_id, row.record_date)].add(row.product_name)
        per_store_staff[row.store_id].add(row.staff_id)
        product_days[(row.store_id, row.product_name)].add(row.record_date)
        if row.abnormal_notes:
            abnormalities.append(row)

    date_contract = all(
        per_day[(store_id, END_DATE - timedelta(days=offset))] == 10
        for store_id in store_ids for offset in range(30)
    )
    staff_contract = all(
        per_day_staff[(store_id, END_DATE - timedelta(days=offset), staff_id)] == 5
        for store_id in store_ids for offset in range(30)
        for staff_id in per_store_staff[store_id]
    )
    product_contract = all(
        len(per_day_products[(store_id, END_DATE - timedelta(days=offset))]) == 10
        for store_id in store_ids for offset in range(30)
    )
    coverage_contract = all(len(days) == 20 for days in product_days.values())
    chk('每店 30 天每天 10 条流水', date_contract)
    chk('每店员每天 5 条流水', staff_contract)
    chk('每天 10 个不同产品', product_contract)
    chk('每产品 30 天覆盖 20 天', coverage_contract)

    quiz_by_store = Counter(row.store_id for row in QuizRecord.query.all())
    chk('每店 10 条培训记录',
        all(quiz_by_store[store_id] == 10 for store_id in store_ids),
        str(dict(quiz_by_store)))
    tagged = []
    for store in Store.query.all():
        domains = {d['name'] for d in industry_pack.get_pack(store.active_pack_id)['domains']}
        rows = QuizRecord.query.filter_by(store_id=store.id).all()
        tagged.append(all(any(f'【{domain}】' in row.question for domain in domains)
                          for row in rows))
    chk('培训题干带行业知识域标签', all(tagged))

    abnormal_days = {END_DATE, END_DATE - timedelta(days=1)}
    chk('异常案例 14 条且落在最后两天',
        len(abnormalities) == 14
        and all(row.record_date in abnormal_days for row in abnormalities),
        f'count={len(abnormalities)}')
    chk('平台管理员 ID=1', db.session.get(Admin, 1).username == 'platform_demo')
    tenant.ensure_default_tenant()
    chk('默认租户迁移不绑定平台账号且不增门店',
        db.session.get(Admin, 1).merchant_id is None
        and Store.query.count() == 7)
    chk('七门店绑定七包',
        sorted(row.active_pack_id for row in Store.query.all()) == sorted(
            spec['pack_id'] for spec in pack_demo.DEMO_STORE_SPECS))
    first_checksum = daily_checksum()

    second = pack_demo.reset_demo_database(
        days=30, end_date=END_DATE.isoformat(), backup=False
    )
    chk('同日期重复重置计数一致', second['sales'] == report['sales'],
        str(second))
    chk('同日期重复重置流水校验和一致',
        daily_checksum() == first_checksum)

    backup_dir = tempfile.mkdtemp(prefix='ph3_backup_')
    backup_path = pack_demo.create_database_backup(backup_dir=backup_dir, keep=3)
    conn = sqlite3.connect(backup_path)
    try:
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
    finally:
        conn.close()
    chk('备份 integrity_check=ok', integrity == 'ok', integrity)

    for idx in range(5):
        open(os.path.join(
            backup_dir, f'demo-reset_2000010{idx}_000000_000000.db'
        ), 'w').close()
    pack_demo._prune_backups(backup_dir, keep=3)
    remaining = [
        name for name in os.listdir(backup_dir)
        if name.startswith('demo-reset_') and name.endswith('.db')
    ]
    chk('备份最多保留 3 份（测试参数）', len(remaining) == 3, str(remaining))

platform_client = app.test_client()
owner_client = app.test_client()
manager_client = app.test_client()
staff_client = app.test_client()
anon = app.test_client()

chk('未登录访问重置页跳登录',
    anon.get('/admin/platform/demo-reset', follow_redirects=False).status_code == 302)
chk('平台登录跳重置页',
    login(platform_client, 'platform_demo', 'platform123').headers.get('Location')
    == '/admin/platform/demo-reset')
chk('老板访问重置页 403',
    login(owner_client, 'owner_demo', 'owner123').status_code == 302
    and owner_client.get('/admin/platform/demo-reset').status_code == 403)
chk('店长访问重置页 403',
    login(manager_client, 'milk_tea_manager', 'manager123').status_code == 302
    and manager_client.get('/admin/platform/demo-reset').status_code == 403)
chk('店员访问重置页 403',
    login(staff_client, 'milk_tea_staff1', 'staff123', 'staff').status_code == 302
    and staff_client.get('/admin/platform/demo-reset').status_code == 403)
chk('平台访问重置页 200',
    platform_client.get('/admin/platform/demo-reset').status_code == 200)

with app.app_context():
    all_store_names = [row.name for row in Store.query.all()]
    own_store_name = Store.query.filter_by(active_pack_id='milk_tea').first().name
owner_page = owner_client.get('/admin/multi/').get_data(as_text=True)
manager_page = manager_client.get('/admin/multi/').get_data(as_text=True)
staff_multi_status = staff_client.get('/admin/multi/').status_code
chk('老板多店视图含 7 家门店',
    all(name in owner_page for name in all_store_names))
chk('店长多店视图仅包含授权门店',
    own_store_name in manager_page
    and all(name not in manager_page for name in all_store_names
            if name != own_store_name))
chk('店员访问多店视图 403', staff_multi_status == 403,
    f'status={staff_multi_status}')
with app.app_context():
    staff_row = Staff.query.filter_by(username='milk_tea_staff1').first()
    own_rows = DailySales.query.filter_by(staff_id=staff_row.id).all()
    chk('店员流水全部归属本店本人',
        own_rows and all(row.store_id == staff_row.store_id for row in own_rows))

page = platform_client.get('/admin/platform/demo-reset').get_data(as_text=True)
nonce = re.search(r'name="reset_nonce"[^>]*value="([^"]+)"', page).group(1)
csrf_token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
with app.app_context():
    before_bad = DailySales.query.count()
    bad = platform_client.post('/admin/platform/demo-reset', data={
        'csrf_token': csrf_token, 'reset_nonce': nonce,
        'confirm_phrase': 'WRONG', 'confirm_checkbox': '1',
    })
    after_bad = DailySales.query.count()
chk('错误确认短语 400 且不改库',
    bad.status_code == 400 and after_bad == before_bad)

csrf_token, page = csrf(platform_client, '/admin/platform/demo-reset')
nonce = re.search(r'name="reset_nonce"[^>]*value="([^"]+)"', page).group(1)
original_backup = pack_demo.create_database_backup
pack_demo.create_database_backup = lambda *args, **kwargs: backup_path
valid = platform_client.post('/admin/platform/demo-reset', data={
    'csrf_token': csrf_token, 'reset_nonce': nonce,
    'confirm_phrase': pack_demo.RESET_CONFIRM_PHRASE,
    'confirm_checkbox': '1',
})
pack_demo.create_database_backup = original_backup
with app.app_context():
    after_valid = DailySales.query.count()
chk('有效确认执行重置并保留平台会话',
    valid.status_code == 302
    and platform_client.get('/admin/platform/demo-reset').status_code == 200
    and after_valid == 2100)
chk('重置后老板/店长/店员旧会话失效',
    owner_client.get('/admin/', follow_redirects=False).status_code == 302
    and manager_client.get('/admin/multi/', follow_redirects=False).status_code == 302
    and staff_client.get('/h5/', follow_redirects=False).status_code == 302)

repeat = platform_client.post('/admin/platform/demo-reset', data={
    'csrf_token': csrf_token, 'reset_nonce': nonce,
    'confirm_phrase': pack_demo.RESET_CONFIRM_PHRASE,
    'confirm_checkbox': '1',
})
with app.app_context():
    after_repeat = DailySales.query.count()
chk('重复提交不二次改库',
    repeat.status_code == 400 and after_repeat == after_valid)

csrf_token, page = csrf(platform_client, '/admin/platform/demo-reset')
nonce = re.search(r'name="reset_nonce"[^>]*value="([^"]+)"', page).group(1)
pack_demo.create_database_backup = (
    lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('forced backup fail'))
)
backup_fail = platform_client.post('/admin/platform/demo-reset', data={
    'csrf_token': csrf_token, 'reset_nonce': nonce,
    'confirm_phrase': pack_demo.RESET_CONFIRM_PHRASE,
    'confirm_checkbox': '1',
})
pack_demo.create_database_backup = original_backup
with app.app_context():
    after_backup_fail = DailySales.query.count()
chk('备份失败拒绝重置且不改库',
    backup_fail.status_code == 500 and after_backup_fail == after_valid)

app.config['WTF_CSRF_ENABLED'] = True
csrf_client = app.test_client()
login_token, _ = csrf(csrf_client, '/login')
csrf_client.post('/login', data={
    'username': 'platform_demo', 'password': 'platform123',
    'role': 'admin', 'csrf_token': login_token,
})
_, reset_page = csrf(csrf_client, '/admin/platform/demo-reset')
with app.app_context():
    before_csrf = DailySales.query.count()
csrf_fail = csrf_client.post('/admin/platform/demo-reset', data={
    'confirm_phrase': pack_demo.RESET_CONFIRM_PHRASE,
    'confirm_checkbox': '1',
})
with app.app_context():
    after_csrf = DailySales.query.count()
chk('CSRF 无效 400 且不改库',
    csrf_fail.status_code == 400 and after_csrf == before_csrf)

print(f'PH3-0 检查项：{len(checks)}')
if failures:
    print('❌ 失败项：')
    for item in failures:
        print('  -', item)
    raise SystemExit(1)
print(f'✅ 全部通过 {len(checks)} 项')
