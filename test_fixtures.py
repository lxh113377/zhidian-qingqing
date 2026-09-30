"""测试夹具 — 临时库隔离 + 基线数据播种。

解决的问题
──────────
在此模块引入之前，smoke_test.py / test_vuln_regression.py / quality_gate.py
三套件共享 data/ican.db，带来两个真实故障：

  1. **并行执行互相摧毁**：quality_gate.py 内部会先跑 init_db.py 的 drop_all()
     清库重建，与另外两个套件并行时会炸出大量假失败（登录 302、staff.id 为
     None、toggle 404）。此前只能靠「必须串行」的口头约定规避。
  2. **摧毁演示数据**：冒烟测试为规避唯一约束，会删掉 milk_tea_staff1 当天的真实填报
     记录。答辩前跑一次测试，当天演示数据就没了。

本模块让每个套件在独立临时 SQLite 文件上运行，两个问题一并消除：
测试可并行、可重复、不碰业务库。

使用方式（必须在 import src.app 之前调用 use_temp_db）
────────────────────────────────────────────────────
    from test_fixtures import use_temp_db, seed_baseline
    use_temp_db()                      # 改写配置指向临时库
    from src.app import create_app     # 之后再导入 app
    app = create_app('development')
    seed_baseline(app)                 # 建表 + 播种基线数据

为什么 use_temp_db 必须先行：
    src.config 在「导入期」就把连接串固化成类属性，create_app 只是读取它。
    晚于 create_app 调用则改写无效，会静默落回 data/ican.db。
"""
import atexit
import os
import shutil
import sys
import tempfile

# 基线账号（与 init_db.py 保持一致，避免套件断言出现两套口径）
ADMIN_USER = 'owner_demo'
ADMIN_PWD = 'owner123'
STAFF_USER = 'milk_tea_staff1'
STAFF_PWD = 'staff123'
STAFF2_USER = 'milk_tea_staff2'
STAFF2_PWD = 'staff123'

# 基线产品（供 F15 产品白名单校验取值）
BASELINE_PRODUCTS = [
    ('珍珠奶茶', 12.00, '奶茶'),
    ('杨枝甘露', 18.00, '果茶'),
    ('烧仙草', 15.00, '奶茶'),
    ('柠檬水', 8.00, '果茶'),
    ('芋泥波波', 16.00, '奶茶'),
]

# 用 dict 承载模块级可变状态，避免散落的 global 语句。
_state = {'tmp_dir': None}


def use_temp_db():
    """将开发配置的连接串改写到进程私有的临时 SQLite 文件。

    必须在 `from src.app import create_app` 之前调用。
    进程退出时自动清理临时目录（atexit 注册）。

    Returns:
        str: 临时数据库文件的绝对路径。
    """
    if _state['tmp_dir'] is not None:  # 幂等：重复调用返回同一个库
        return os.path.join(_state['tmp_dir'], 'test.db')

    root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)

    _state['tmp_dir'] = tempfile.mkdtemp(prefix='ican_test_')
    db_path = os.path.join(_state['tmp_dir'], 'test.db')
    # SQLAlchemy 的 sqlite URI 统一用正斜杠，Windows 反斜杠会被解析成转义符。
    uri = 'sqlite:///' + db_path.replace('\\', '/')

    import src.config as cfg
    cfg.DevelopmentConfig.SQLALCHEMY_DATABASE_URI = uri
    # 项目当前只有 Development/Production 两个配置类；若后续新增 TestingConfig，
    # 这里一并改写，避免测试套件切换配置名后静默落回业务库。
    if hasattr(cfg, 'TestingConfig'):
        cfg.TestingConfig.SQLALCHEMY_DATABASE_URI = uri

    atexit.register(_cleanup_temp_db)
    return db_path


def _cleanup_temp_db():
    """进程退出时删除临时库目录（忽略 Windows 文件占用导致的删除失败）。"""
    tmp_dir = _state.get('tmp_dir')
    if tmp_dir and os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    _state['tmp_dir'] = None


def seed_baseline(app, with_staff2=True, with_history=True):
    """在临时库上建表并播种基线数据。

    与 init_db.py 的关键差异：营业流水是**固定值而非随机值**。
    看板 KPI / 占比 / 损耗告警类断言依赖确定的数字，随机流水会让断言时灵时不灵。

    历史流水刻意**只播种到昨天为止，不含今天**：
    冒烟测试的「店员填报提交」用例要往今天写一条记录，若这里先占位，
    会撞 (store_id, staff_id, record_date, pack_id, product_name) 唯一约束导致用例假失败。

    Args:
        app: 已创建的 Flask 应用实例。
        with_staff2: 是否播种第二个店员（部分越权用例需要两个店员对照）。
        with_history: 是否播种历史营业流水。看板的 AI 建议块依赖
            `stats['has_data']`，无流水时整块不渲染，相关断言会失败。

    Returns:
        dict: 便于调用方引用的关键对象 id，形如 {'admin_id', 'staff_id'}。
    """
    from datetime import date, timedelta

    from src import business_calendar as cal

    from src import tenant
    from src.models import ROLE_OWNER, Admin, DailySales, Product, Staff, db

    with app.app_context():
        db.create_all()
        # PH2-2：所有测试数据落在默认商家/默认门店上（与生产迁移后的形态一致），
        # 否则 store_id 为 NULL 会让 SQLite 复合唯一约束失效（NULL 互不相等）。
        _tenant = tenant.ensure_default_tenant()
        _merchant, _store = _tenant['merchant'], _tenant['store']

        admin = Admin(username=ADMIN_USER, role=ROLE_OWNER, merchant_id=_merchant.id)
        admin.set_password(ADMIN_PWD)
        db.session.add(admin)

        staff = Staff(username=STAFF_USER, display_name='小王',
                      phone='13800138001', store_name='测试门店',
                      merchant_id=_merchant.id, store_id=_store.id)
        staff.set_password(STAFF_PWD)
        db.session.add(staff)

        if with_staff2:
            staff2 = Staff(username=STAFF2_USER, display_name='小李',
                           phone='13800138002', store_name='测试门店',
                           merchant_id=_merchant.id, store_id=_store.id)
            staff2.set_password(STAFF2_PWD)
            db.session.add(staff2)

        for name, price, category in BASELINE_PRODUCTS:
            db.session.add(Product(product_name=name, unit_price=price,
                                   category=category, store_id=_store.id))

        db.session.flush()  # 取 staff.id 用于流水外键

        if with_history:
            # 从昨天往前 6 天，每天播 3 个产品的固定销量，
            # 销量随天数递减以形成稳定的产品排序（便于 top/占比类断言）。
            for offset in range(1, 7):
                day = cal.business_today() - timedelta(days=offset)
                for idx, (name, price, _) in enumerate(BASELINE_PRODUCTS[:3]):
                    qty = 30 - idx * 8 - offset  # 确定性递减，恒为正
                    db.session.add(DailySales(
                        staff_id=staff.id,
                        store_id=_store.id,
                        record_date=day,
                        product_name=name,
                        product_sales_qty=qty,
                        total_revenue=round(qty * float(price), 2),
                        raw_material_remaining='50',
                        raw_material_waste='2',
                        abnormal_notes='',
                    ))

        db.session.commit()
        return {'admin_id': admin.id, 'staff_id': staff.id}
