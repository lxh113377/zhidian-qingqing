"""SQLAlchemy ORM 模型 — 数据表（P2 起含指标定义表 metric_defs）"""
from datetime import UTC, datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

# ─── 多租户角色常量（PH2-2，唯一真相源）─────────────────────
ROLE_PLATFORM = 'platform'   # 平台管理员：只看商家/门店/账号/系统元数据
ROLE_OWNER = 'owner'         # 商家老板：看本商家全部门店
ROLE_MANAGER = 'manager'     # 店长：只看被授权门店（AdminStoreAccess）
ROLE_STAFF = 'staff'         # 店员：只看所属门店与自己的记录
ADMIN_ROLES = (ROLE_PLATFORM, ROLE_OWNER, ROLE_MANAGER)


# ─── 商家表（多租户顶层，PH2-2）─────────────────────────────
class Merchant(db.Model):
    """商家（租户）。一个账号只属于一个商家；老板可拥有多家门店。"""
    __tablename__ = 'merchants'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    contact_email = db.Column(db.String(120), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))

    stores = db.relationship('Store', backref='merchant', lazy=True)

    def __repr__(self):
        return f'<Merchant {self.id} {self.name}>'


# ─── 门店表（PH2-2）─────────────────────────────────────────
class Store(db.Model):
    """门店。一个门店归属一个商家；一店一当前行业包（激活态仍由行业包层负责）。"""
    __tablename__ = 'stores'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('merchants.id'),
                            nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(50), nullable=False)
    address = db.Column(db.String(200), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # PH2-6：该门店当前使用的行业包（NULL = 跟随全局激活包）。
    # 「一店一当前包」：多门店可各用各的包，历史流水 pack_id 不受影响。
    active_pack_id = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))

    __table_args__ = (
        db.UniqueConstraint('merchant_id', 'code', name='uq_store_merchant_code'),
    )

    def __repr__(self):
        return f'<Store {self.id} {self.name}>'


# ─── 店长门店授权表（PH2-2）──────────────────────────────────
class AdminStoreAccess(db.Model):
    """店长（Admin.role=manager）的门店授权；老板不需要授权行。"""
    __tablename__ = 'admin_store_access'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=False, index=True)
    store_id = db.Column(db.Integer, db.ForeignKey('stores.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))

    __table_args__ = (
        db.UniqueConstraint('admin_id', 'store_id', name='uq_admin_store'),
    )

    def __repr__(self):
        return f'<AdminStoreAccess admin={self.admin_id} store={self.store_id}>'


# ─── 邮箱验证码表（PH2-3 开放注册）────────────────────────────
class EmailVerification(db.Model):
    """邮箱验证码：只存哈希、限时有效、限尝试次数与重发频率。"""
    __tablename__ = 'email_verifications'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    purpose = db.Column(db.String(30), nullable=False, default='signup')
    code_hash = db.Column(db.String(128), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    sent_count = db.Column(db.Integer, nullable=False, default=1)
    last_sent_at = db.Column(db.DateTime, nullable=True)
    consumed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))

    def __repr__(self):
        return f'<EmailVerification {self.email} purpose={self.purpose}>'


# ─── AI 用量账本（PH2-4/P5 商家级配额）───────────────────────
class AIUsageLedger(db.Model):
    """AI 调用计量：按商家（可下钻门店）统计造包/分析次数与 Token。"""
    __tablename__ = 'ai_usage_ledger'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('merchants.id'),
                            nullable=False, index=True)
    store_id = db.Column(db.Integer, db.ForeignKey('stores.id'), nullable=True, index=True)
    kind = db.Column(db.String(30), nullable=False, default='analysis')
    tokens = db.Column(db.Integer, nullable=False, default=0)
    ok = db.Column(db.Boolean, nullable=False, default=True)
    detail_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))

    def __repr__(self):
        return f'<AIUsageLedger m={self.merchant_id} {self.kind} tokens={self.tokens}>'


# ─── 管理员表 ───────────────────────────────────────────────
class Admin(UserMixin, db.Model):
    __tablename__ = 'admin'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(50), nullable=False, unique=True)
    password = db.Column(db.String(255), nullable=False)
    # PH2-2：角色与商家归属（platform 平台管理员不绑定商家；owner/manager 绑定）
    role = db.Column(db.String(20), nullable=False, default=ROLE_OWNER, index=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('merchants.id'),
                            nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))

    def set_password(self, raw):
        self.password = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password, raw)

    def get_id(self):
        return f'admin-{self.id}'

    def __repr__(self):
        return f'<Admin {self.username}>'


# ─── 员工/店员表 ────────────────────────────────────────────
class Staff(UserMixin, db.Model):
    __tablename__ = 'staff'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(50), nullable=False, unique=True)
    password = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(50), nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    store_name = db.Column(db.String(100), nullable=False, default='南昌·红谷滩店')
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # PH2-2：店员绑定商家与门店（历史 NULL 行由 ensure_default_tenant 回填）
    merchant_id = db.Column(db.Integer, db.ForeignKey('merchants.id'),
                            nullable=True, index=True)
    store_id = db.Column(db.Integer, db.ForeignKey('stores.id'),
                         nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))

    sales_records = db.relationship('DailySales', backref='staff', lazy=True)
    quiz_records = db.relationship('QuizRecord', backref='staff', lazy=True)

    def set_password(self, raw):
        self.password = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password, raw)

    def get_id(self):
        return f'staff-{self.id}'

    def __repr__(self):
        return f'<Staff {self.display_name}({self.username})>'


# ─── 产品表 ─────────────────────────────────────────────────
class Product(db.Model):
    __tablename__ = 'products'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # PH2-2：唯一性收敛到门店范围（跨店同名产品可共存），见 __table_args__
    product_name = db.Column(db.String(100), nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    cost_price = db.Column(db.Numeric(10, 2), nullable=True)  # 可空=待录入；看板毛利率依赖
    category = db.Column(db.String(50), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # 所属行业包（P7-B2 落列，P7-B2 起按激活包过滤）：NULL = 默认包/历史数据。
    # 已知限制：product_name 仍为全局唯一（跨包同名需先改约束，见 06-constraints P7-C 同族议题）。
    pack_id = db.Column(db.String(50), nullable=True, index=True)
    # PH2-2：门店归属（NULL = 历史数据，回填默认门店）；跨店同名产品靠门店维度区分
    store_id = db.Column(db.Integer, db.ForeignKey('stores.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))

    __table_args__ = (
        # PH2-2：同名产品按门店隔离（跨店可重名）
        db.UniqueConstraint('store_id', 'product_name', name='uq_product_store_name'),
    )

    def __repr__(self):
        return f'<Product {self.product_name} ¥{self.unit_price}>'


def active_pack_clause(column):
    """按当前激活行业包过滤的数据范围条件（P7-B2，唯一实现）。

    隔离语义与 `analytics._pack_record_filter` 一致：默认包放行 NULL
    （P2 之前的历史数据），切换到其他包后严格相等。产品清单、日销售
    流水等带 `pack_id` 的表统一复用它，避免多份口径漂移。

    Args:
        column: 带 pack_id 语义的 ORM 列（如 `Product.pack_id`）。

    Returns:
        SQLAlchemy 布尔表达式，可直接放进 `query.filter(...)`。
    """
    # 函数级导入：models 保持「只定义 ORM」的轻量职责，且规避潜在循环导入
    from src import industry_pack
    pack_id = industry_pack.active_pack_id()
    if pack_id == industry_pack.DEFAULT_PACK_ID:
        return db.or_(column.is_(None), column == pack_id)
    return column == pack_id


# ─── 每日营业数据表（核心表）───────────────────────────────────
class DailySales(db.Model):
    __tablename__ = 'daily_sales'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False, index=True)
    record_date = db.Column(db.Date, nullable=False, index=True)
    product_name = db.Column(db.String(100), nullable=False)
    product_sales_qty = db.Column(db.Integer, nullable=False, default=0)
    total_revenue = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    # H60 成本快照（第 26 轮）：毛利率的**历史**成本价取自本行，不再回查
    # `products.cost_price` 的当前值。此前 `_compute_gross_margin()` 按产品名把
    # 当前成本映射到历史流水上，实测「删一个产品 / 改一次名 / 改一次成本价」
    # 会让历史毛利率静默移动 -0.2255 / +1.5072 / +6.4935 个百分点（营收分文未动）。
    product_cost = db.Column(db.Numeric(10, 2), nullable=True)
    # snapshot = 提交当时按到的成本价；backfill = p005 补丁回填（用回填时刻的
    # 当前值补的历史行，**不得当成当日真实成本**，口径沿用第 25 轮 origin=backfill
    # 不得追认的判决）；none = 当时产品没有成本价（毛利率如实不计这一行）。
    # `server_default` 不是冗余：p002 的表重建是「按旧表实际列做 INSERT ... SELECT」，
    # 只写 ORM 的 `default=`（Python 侧）时新列在建表 DDL 里没有默认值，
    # 复制语句就会以 NOT NULL 失败 —— L4 在补丁链上实测到这条。
    cost_basis = db.Column(db.String(12), nullable=False,
                           default='none', server_default='none')
    raw_material_remaining = db.Column(db.Text, nullable=True)
    raw_material_waste = db.Column(db.Text, nullable=True)
    abnormal_notes = db.Column(db.Text, nullable=True)
    # 自定义指标 JSON（P2）：键 = 当前包自定义指标 key，值为数值/文本；
    # 仅承载 typed 列之外的自定义指标，骨架字段仍走 typed 列（零回归）。
    metrics_json = db.Column(db.Text, nullable=True)
    # 所属行业包（P2 落列，P3 起按激活包过滤）：NULL = 默认包/历史数据
    pack_id = db.Column(db.String(50), nullable=True, index=True)
    # PH2-2：门店归属（多门店隔离的写路径落点）
    store_id = db.Column(db.Integer, db.ForeignKey('stores.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))

    __table_args__ = (
        # P7-C：纳入 pack_id —— 切包后同日同名产品可再次填报，与「统计按包
        # 隔离」自洽；约束名保持不变以兼容既有引用。pack_id 为 NULL 的 P2 前
        # 历史行不受约束保护（SQL 视 NULL 互异），由 H5 提交预检查
        # （active_pack_clause 默认包直通 NULL）兜底。
        # PH2-2：纳入 store_id —— 跨店同日同名产品可共存；约束名保持不变
        # 以兼容既有引用（迁移探测按列集合判定，不依赖约束名）。
        db.UniqueConstraint('store_id', 'staff_id', 'record_date', 'pack_id',
                            'product_name', name='uq_staff_date_product'),
    )

    def __repr__(self):
        return (f'<DailySales {self.record_date} '
                f'{self.product_name} x{self.product_sales_qty}>')


# ─── 员工答题记录表 ──────────────────────────────────────────
class QuizRecord(db.Model):
    __tablename__ = 'quiz_records'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False, index=True)
    quiz_type = db.Column(db.String(20), nullable=False, default='question')
    session_id = db.Column(db.String(50), nullable=True)
    question = db.Column(db.Text, nullable=False)
    staff_answer = db.Column(db.Text, nullable=True)
    ai_feedback = db.Column(db.Text, nullable=True)
    is_correct = db.Column(db.Boolean, nullable=True)
    score = db.Column(db.Numeric(5, 2), nullable=True)
    # 所属行业包（P2 落列，P3 起按激活包过滤）：NULL = 默认包/历史数据
    pack_id = db.Column(db.String(50), nullable=True, index=True)
    # PH2-2：门店归属（考核记录按门店隔离）
    store_id = db.Column(db.Integer, db.ForeignKey('stores.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))

    def __repr__(self):
        return (f'<QuizRecord {self.quiz_type} '
                f'staff={self.staff_id} score={self.score}>')


# ─── 指标定义表（P2）─────────────────────────────────────────
class MetricDef(db.Model):
    """行业包填报指标定义（每个包一份，P4 起由配置后台在线编辑）。

    P2 阶段：init_db 按当前激活文件包播种（结构就绪 + 演示数据）；
    运行时指标定义的唯一访问器仍是 industry_pack.pack_metrics()
    （三级回退：DB 覆盖 → 文件包 → 内置兜底，P4 注册 DB 覆盖层后本表生效）。
    """
    __tablename__ = 'metric_defs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    pack_id = db.Column(db.String(50), nullable=False, index=True)
    # PH2-2：门店归属（跨店同名指标靠门店维度区分；NULL = 历史数据）
    store_id = db.Column(db.Integer, db.ForeignKey('stores.id'), nullable=True, index=True)
    key = db.Column(db.String(50), nullable=False)          # 填报键/宽表列名
    label = db.Column(db.String(50), nullable=False)        # 表单与看板展示名
    value_type = db.Column(db.String(10), nullable=False, default='number')  # number/integer/text
    unit = db.Column(db.String(20), nullable=False, default='')
    role = db.Column(db.String(20), nullable=False, default='other')         # 业务角色（metrics.py 消费）
    agg = db.Column(db.String(10), nullable=False, default='sum')            # sum/avg/count/none
    required = db.Column(db.Boolean, nullable=False, default=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    sort = db.Column(db.Integer, nullable=False, default=0)
    options_json = db.Column(db.Text, nullable=True)        # 预留（下拉选项等，P4 编辑用）

    __table_args__ = (
        # PH2-2：指标键唯一性收敛到门店范围（跨店同名指标可共存）
        db.UniqueConstraint('store_id', 'pack_id', 'key',
                            name='uq_metric_def_store_pack_key'),
    )

    def __repr__(self):
        return f'<MetricDef {self.pack_id}.{self.key}({self.role})>'


# ─── 行业包表（P4 配置后台）────────────────────────────────────
class IndustryPack(db.Model):
    """行业包元信息 + 激活态（DB 覆盖层的包登记处）。

    仅承载「后台新建/改名/激活」的元信息；内容段（词表/题库/情景等）
    存 PackItem，指标定义存 MetricDef。文件包（src/industry_packs/）
    无需登记即天然可用，登记后其内容段可被 DB 覆盖。
    """
    __tablename__ = 'industry_packs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    pack_id = db.Column(db.String(50), nullable=False, unique=True)
    name = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(200), nullable=False, default='')
    is_active = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))
    updated_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(UTC).replace(tzinfo=None),
                           onupdate=lambda: datetime.now(UTC).replace(tzinfo=None))

    def __repr__(self):
        return f'<IndustryPack {self.pack_id} active={self.is_active}>'


# ─── 包内容项表（P4 配置后台，DB 覆盖层）──────────────────────────
class PackItem(db.Model):
    """行业包内容段覆盖项（section 级覆盖文件包）。

    section 取值：terms / domains / weak_keywords / synonyms /
    formula_params / knowledge / questions / scenarios / products。
    knowledge 段按域存（key = 域名，payload = md 全文 JSON 字符串）；
    其余段 key 为空串。payload 一律为 JSON 编码文本。
    """
    __tablename__ = 'pack_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    pack_id = db.Column(db.String(50), nullable=False, index=True)
    section = db.Column(db.String(30), nullable=False)
    key = db.Column(db.String(50), nullable=False, default='')
    payload = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))
    updated_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(UTC).replace(tzinfo=None),
                           onupdate=lambda: datetime.now(UTC).replace(tzinfo=None))

    __table_args__ = (
        db.UniqueConstraint('pack_id', 'section', 'key', name='uq_pack_item'),
    )

    def __repr__(self):
        return f'<PackItem {self.pack_id}.{self.section}({self.key})>'


# ─── 门店任务表（PH4-2 AI 建议转任务闭环）──────────────────────
class StoreTask(db.Model):
    """门店责任任务：AI 建议或人工创建的负责人 / 截止时间 / 状态闭环。"""
    __tablename__ = 'store_tasks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('merchants.id'),
                            nullable=False, index=True)
    store_id = db.Column(db.Integer, db.ForeignKey('stores.id'),
                         nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    detail = db.Column(db.Text, nullable=True)
    source = db.Column(db.String(20), nullable=False, default='manual')
    evidence_json = db.Column(db.Text, nullable=True)
    assignee_role = db.Column(db.String(20), nullable=False, default='manager')
    assignee_admin_id = db.Column(db.Integer, db.ForeignKey('admin.id'),
                                  nullable=True, index=True)
    assignee_staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'),
                                  nullable=True, index=True)
    due_date = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    review_note = db.Column(db.Text, nullable=True)
    takeover_json = db.Column(db.Text, nullable=True)
    before_json = db.Column(db.Text, nullable=True)
    after_json = db.Column(db.Text, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_by_admin_id = db.Column(db.Integer, db.ForeignKey('admin.id'),
                                    nullable=True)
    created_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(UTC).replace(tzinfo=None))
    updated_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(UTC).replace(tzinfo=None),
                           onupdate=lambda: datetime.now(UTC).replace(tzinfo=None))

    def __repr__(self):
        return f'<StoreTask {self.id} store={self.store_id} {self.status}>'


# ─── 开放 API 令牌表（OPT-A，只读集成）──────────────────────────
class ApiToken(db.Model):
    """商家只读开放 API 访问令牌。

    安全口径：库内**仅存 SHA-256 哈希**，明文令牌只在创建瞬间返回一次，落库/日志/
    交付物均不含可重放的原文（对齐「密钥不入库面」安全红线）。令牌绑定单个商家，
    其可见数据范围恒等于该商家的活跃门店，无法跨商家读取。
    """
    __tablename__ = 'api_tokens'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('merchants.id'),
                            nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False, default='')     # 用途备注（如「小程序导入」）
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    # 逗号分隔的 *:read 子集；'read' = 全部只读（存量签发口径，见 api_open.effective_scopes）
    scopes = db.Column(db.String(120), nullable=False, default='read')
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    last_used_at = db.Column(db.DateTime, nullable=True)
    created_by_admin_id = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=True)
    created_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(UTC).replace(tzinfo=None))

    def __repr__(self):
        return f'<ApiToken {self.id} merchant={self.merchant_id} active={self.is_active}>'


# ─── Webhook 订阅表（OPT-A，事件外发）───────────────────────────
class WebhookSubscription(db.Model):
    """商家 Webhook 订阅：填报提交 / 任务状态变更事件外发到商家配置的 URL。

    签名密钥不落库明文：只存一个随机 `secret_salt`，出站签名密钥由
    `HMAC(SECRET_KEY, 'ican-webhook:' + salt)` 派生（见 src/webhooks.py）。
    因此即便数据库整表泄露，在没有服务端 SECRET_KEY 时也无法伪造合法签名。
    """
    __tablename__ = 'webhook_subscriptions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('merchants.id'),
                            nullable=False, index=True)
    url = db.Column(db.String(500), nullable=False)
    # 逗号分隔的事件类型；'*' 表示订阅全部已支持事件（见 webhooks.SUPPORTED_EVENTS）
    event_types = db.Column(db.String(200), nullable=False, default='*')
    secret_salt = db.Column(db.String(64), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by_admin_id = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=True)
    created_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(UTC).replace(tzinfo=None))

    def __repr__(self):
        return f'<WebhookSubscription {self.id} merchant={self.merchant_id} active={self.is_active}>'


class WebhookDelivery(db.Model):
    """一次出站投递的**一条尝试**留痕（第 30 轮 H82，对标 Frappe `Webhook Request Log`）。

    为什么必须落库而不是只写 JSONL：轮转型日志文件回答的是"刚才那次打印了什么"，
    而商家与排障要回答的是"这个事件发过几次、每次什么结果、下一次什么时候重试"——
    日志一轮转就把答案带走了（`WEBHOOK_TRACE_MAX_*` 有界），且无法按订阅聚合判失败率。
    ⇒ 投递结果是**物件**：可查、可复盘、可作为"连续失败即停用"的判据输入。

    刻意**不存 payload 正文**，只存 `payload_sha256`：商家业务明细不得外泄进审计面
    （第 26/28 轮同一条红线），但要能证明"发出去的就是这一份"。
    """
    __tablename__ = 'webhook_delivery'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('webhook_subscriptions.id'),
                                nullable=False, index=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('merchants.id'),
                            nullable=False, index=True)
    # 同一事件的多条订阅共享一个 delivery_id（对标 Saleor EventDelivery ↔ attempts 的一对多）
    delivery_id = db.Column(db.String(32), nullable=False, index=True)
    event = db.Column(db.String(60), nullable=False)
    attempt = db.Column(db.Integer, nullable=False, default=1)
    ok = db.Column(db.Boolean, nullable=False, default=False)
    status_code = db.Column(db.Integer, nullable=True)
    error_class = db.Column(db.String(40), nullable=True)
    payload_sha256 = db.Column(db.String(64), nullable=False, default='')
    http_ms = db.Column(db.Integer, nullable=True)
    # 目标被守卫拦下时记 'blocked'，与"网络失败"分开：前者是策略，后者是故障，混在一起就查不出方向
    blocked = db.Column(db.Boolean, nullable=False, default=False, index=True)
    next_retry = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, index=True,
                           default=lambda: datetime.now(UTC).replace(tzinfo=None))

    def __repr__(self):
        return f'<WebhookDelivery {self.id} sub={self.subscription_id} attempt={self.attempt} ok={self.ok}>'


# ─── 迁移账本（第 17 轮 H27，对标 Ghost migrations / Metabase DATABASECHANGELOG）──
class SchemaPatchLog(db.Model):
    """存量库轻量迁移补丁的执行账本。

    存在的理由不是"记录日志"，而是让**「这个库升到第几步」变成一个可查询状态**：
    此前 4 个补丁全靠结构探测推断该不该跑，跑没跑过无从回答，中断后也无法定位
    停在第几步（对标口径见 check_schema_drift.py 文档头）。

    租户守卫豁免：本表是**库自身**的元数据，不归属任何商家，跨租户可见。
    """
    __tablename__ = 'schema_patch_log'

    patch_id = db.Column(db.String(40), primary_key=True)
    title = db.Column(db.String(120), nullable=False, default='')
    # ddl = 改结构（表/列/约束）；data = 只动数据行。对标 ToolJet 把两者拆成两个 datasource
    kind = db.Column(db.String(8), nullable=False, default='ddl')
    # applied = 本次真改了东西；noop = 跑过但结构/数据已满足（幂等自证）
    mode = db.Column(db.String(8), nullable=False, default='applied')
    detail = db.Column(db.String(200), nullable=False, default='')
    applied_at = db.Column(db.DateTime, nullable=False,
                           default=lambda: datetime.now(UTC).replace(tzinfo=None))

    def __repr__(self):
        return f'<SchemaPatchLog {self.patch_id} {self.mode}>'


# ─── 变更归因链（H59，第 26 轮）─────────────────────────────
class ChangeLog(db.Model):
    """业务主数据的变更记录：谁、什么时候、把哪条从什么改成什么。

    append-only：代码里不存在 UPDATE/DELETE 出口（门禁第 40 项 AST 判）。
    写入点唯一 —— `src/change_log.py` 的 before_flush 监听器，业务代码不调用它，
    这是对标 Odoo `log_access` 的核心取舍：**归因不能靠自觉**。

    值的粒度由名册分两档（见 change_log.TRACKED / REDACTED_TABLES）：主数据记字段级
    前像后像；营业流水/考核只记动作与主键，值不落库（安全红线：商家业务明细不得
    暴露给平台管理员）。

    归属：`store_id` 为 NULL 的行是商家级/平台级对象（商家、行业包、管理员账号），
    商家侧视图靠 `merchant_id` 严格等值过滤，NULL 归属行不对商家侧展示。
    """
    __tablename__ = 'change_log'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    entity = db.Column(db.String(40), nullable=False, index=True)
    entity_pk = db.Column(db.Integer, nullable=False)
    # create / update / delete / archive
    action = db.Column(db.String(12), nullable=False, index=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('merchants.id'),
                            nullable=True, index=True)
    store_id = db.Column(db.Integer, db.ForeignKey('stores.id'),
                         nullable=True, index=True)
    # actor_id 跨 admin/staff 两张表，故意不建外键（建了会把店员侧写成悬空引用）
    actor_kind = db.Column(db.String(12), nullable=False, default='system',
                           server_default='system')
    actor_id = db.Column(db.Integer, nullable=True)
    actor_role = db.Column(db.String(20), nullable=True)
    request_id = db.Column(db.String(36), nullable=False, default='', server_default='')
    # web = 请求内产生；system = CLI/迁移/无请求上下文
    source = db.Column(db.String(12), nullable=False, default='web', server_default='web')
    diff_json = db.Column(db.Text, nullable=False, default='{}', server_default='{}')
    changed_at = db.Column(db.DateTime, nullable=False, index=True,
                           default=lambda: datetime.now(UTC).replace(tzinfo=None))

    __table_args__ = (
        db.Index('ix_change_log_merchant_time', 'merchant_id', 'changed_at'),
    )

    def __repr__(self):
        return f'<ChangeLog {self.entity}#{self.entity_pk} {self.action}>'
