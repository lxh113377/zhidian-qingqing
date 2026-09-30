"""多租户与多门店访问层（PH2-2）。

职责：
  1. 默认商家/默认门店兜底：存量单店数据自动归位（不丢历史记录）。
  2. `current_store()` / `set_current_store()`：请求级快照 + 全局兜底，
     与 `industry_pack.active_pack_id()` 同范式（避免会话 Cookie 膨胀）。
  3. `store_scope_clause()`：带门店维度列的过滤条件唯一实现。
  4. `visible_store_ids()`：按角色计算可见门店（平台管理员不看业务明细）。
  5. `merchant_scope_clause()` / `in_clause()` / `staff_visible()`：商家维度
     收口与单条记录可见性判定（2026-09-21 越权修复：补 store 维度之外的
     merchant 维度，并堵住按 id 直接取记录的 IDOR）。
  """
from __future__ import annotations

import os
import threading

from flask import has_request_context, request

from src.models import (
    ROLE_MANAGER,
    ROLE_OWNER,
    ROLE_PLATFORM,
    ROLE_STAFF,
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

DEFAULT_MERCHANT_NAME = '默认商家'
DEFAULT_MERCHANT_CODE = 'default'
DEFAULT_STORE_NAME = '默认门店'
DEFAULT_STORE_CODE = 'default'

STORE_ID_ENV = 'ICAN_STORE_ID'
_REQUEST_STORE_ENV_KEY = '_ican_store_id'

_LOCK = threading.RLock()
_HOOKS = {'store_id': None, 'revision': 0}


def set_current_store(store_id):
    """设置全局当前门店（测试/CLI 用；请求内请用请求级快照语义）。"""
    with _LOCK:
        _HOOKS['store_id'] = int(store_id) if store_id else None
        _HOOKS['revision'] += 1


def _configured_store_id():
    with _LOCK:
        configured = _HOOKS['store_id']
    for candidate in (configured, os.environ.get(STORE_ID_ENV)):
        if candidate:
            try:
                return int(candidate)
            except (TypeError, ValueError):
                continue
    return None


def default_merchant():
    """默认商家（不存在返回 None；不隐式建库，避免只读场景写盘）。"""
    return Merchant.query.filter_by(name=DEFAULT_MERCHANT_NAME).first()


def default_store():
    """默认门店（不存在返回 None）。"""
    merchant = default_merchant()
    if not merchant:
        return None
    return Store.query.filter_by(merchant_id=merchant.id, code=DEFAULT_STORE_CODE).first()


def ensure_default_tenant():
    """确保默认商家/默认门店存在，并把存量 NULL 归属行回填到默认门店。

    PH3-0 起：若历史默认名不存在，但库中已有商家/门店（如七门店演示租户），
    复用最早一条作为兜底，避免 `--if-empty` 启动时额外造出第八家门店。

    Returns:
        dict: {'merchant': Merchant, 'store': Store, 'backfilled': {表名: 行数}}
    """
    merchant = default_merchant()
    if merchant is None:
        merchant = Merchant.query.order_by(Merchant.id).first()
    if merchant is None:
        merchant = Merchant(name=DEFAULT_MERCHANT_NAME,
                            contact_email=None, is_active=True)
        db.session.add(merchant)
        db.session.flush()
    store = Store.query.filter_by(merchant_id=merchant.id,
                                  code=DEFAULT_STORE_CODE).first()
    if store is None:
        store = Store.query.filter_by(merchant_id=merchant.id).order_by(Store.id).first()
    if store is None:
        store = Store(merchant_id=merchant.id, name=DEFAULT_STORE_NAME,
                      code=DEFAULT_STORE_CODE, is_active=True)
        db.session.add(store)
        db.session.flush()

    backfilled = {}
    # 账号归属：存量管理员 = 商家老板；存量店员 = 默认门店店员
    backfilled['admin_merchant'] = Admin.query.filter(
        Admin.merchant_id.is_(None),
        db.or_(Admin.role.is_(None), Admin.role != ROLE_PLATFORM),
    ).update({'merchant_id': merchant.id}, synchronize_session=False)
    backfilled['admin_role'] = Admin.query.filter(
        db.or_(Admin.role.is_(None), Admin.role == '')).update(
        {'role': ROLE_OWNER}, synchronize_session=False)
    backfilled['staff_merchant'] = Staff.query.filter(Staff.merchant_id.is_(None)).update(
        {'merchant_id': merchant.id}, synchronize_session=False)
    backfilled['staff_store'] = Staff.query.filter(Staff.store_id.is_(None)).update(
        {'store_id': store.id}, synchronize_session=False)
    # 业务数据归属：产品/流水/考核/指标定义
    for name, model in (('products', Product), ('daily_sales', DailySales),
                        ('quiz_records', QuizRecord), ('metric_defs', MetricDef)):
        backfilled[name] = model.query.filter(model.store_id.is_(None)).update(
            {'store_id': store.id}, synchronize_session=False)
    db.session.commit()
    return {'merchant': merchant, 'store': store, 'backfilled': backfilled}


def _role_default_store_id(user):
    """账号角色默认门店：staff→绑定门店；manager→首个授权门店；owner→本商家首店。"""
    role = user_role(user)
    if role == ROLE_STAFF:
        return getattr(user, 'store_id', None)
    if role == ROLE_MANAGER:
        row = (AdminStoreAccess.query.filter_by(admin_id=user.id)
               .order_by(AdminStoreAccess.id).first())
        return row.store_id if row else None
    if role == ROLE_OWNER:
        merchant_id = getattr(user, 'merchant_id', None)
        if merchant_id is None:
            return None
        row = (Store.query.filter_by(merchant_id=merchant_id)
               .order_by(Store.id).first())
        return row.id if row else None
    return None


def allowed_store_ids(user):
    """账号可选门店集合：staff=绑定门店；manager=授权门店；owner=本商家门店。

    门店切换、会话校验与上下文解析共用此唯一口径，避免各写一份判断。
    """
    if user is None:
        return []
    if user_role(user) == ROLE_STAFF:
        sid = getattr(user, 'store_id', None)
        return [sid] if sid else []
    return visible_store_ids(user)


def store_options_for(user):
    """当前账号可选门店摘要（供后台门店选择器渲染）。"""
    ids = allowed_store_ids(user)
    if not ids:
        return []
    rows = Store.query.filter(Store.id.in_(ids)).order_by(Store.id).all()
    return [{'id': s.id, 'name': s.name, 'code': s.code} for s in rows]


def _session_store_id(user):
    """会话中显式选择的门店（须在账号可选集合内；无权/已删则忽略）。"""
    try:
        from flask import session
        raw = session.get('store_id')
    except Exception:
        return None
    if not raw:
        return None
    try:
        store_id = int(raw)
    except (TypeError, ValueError):
        return None
    return store_id if store_id in allowed_store_ids(user) else None


def current_store_id():
    """当前门店 id：请求快照 → 显式配置（钩子/环境变量）→ 登录账号角色默认门店。

    请求外（CLI/离线脚本）无登录态时返回 None，调用方的门店过滤随之退化为
    零过滤（`store_scope_clause` 返回 db.true()），保证既有离线链路零回归。
    PH2-3D：登录态下优先取会话中显式切换的门店（经权限校验），否则回落角色默认门店。
    """
    if has_request_context():
        cached = request.environ.get(_REQUEST_STORE_ENV_KEY)
        if cached:
            try:
                return int(cached)
            except (TypeError, ValueError):
                pass
    store_id = _configured_store_id()
    if store_id is None and has_request_context():
        from flask_login import current_user
        if getattr(current_user, 'is_authenticated', False):
            store_id = _session_store_id(current_user)
            if store_id is None:
                store_id = _role_default_store_id(current_user)
    if store_id is not None and has_request_context():
        request.environ[_REQUEST_STORE_ENV_KEY] = store_id
    return store_id


def current_store():
    """当前门店对象（未设置/不存在返回 None）。"""
    store_id = current_store_id()
    if store_id is None:
        return None
    try:
        return db.session.get(Store, store_id)
    except Exception:  # 无应用上下文等只读场景
        return None


def store_scope_clause(column):
    """按当前门店过滤 ORM 列的条件（唯一实现）。

    与 `models.active_pack_clause` 同语义：当前门店为默认门店时放行 NULL
    （PH2-2 之前的历史数据）；切换到他店后严格相等。

    安全兜底（PH2-3B）：**已登录但无门店归属**的账号返回 `db.false()`
    而不是零过滤 —— 否则新建老板在开第一家门店前会看到全平台数据。
    请求外的离线/CLI 场景仍返回零过滤，保持既有脚本行为。
    """
    store_id = current_store_id()
    if store_id is None:
        if has_request_context():
            from flask_login import current_user
            if getattr(current_user, 'is_authenticated', False):
                return db.false()
        return db.true()
    store = current_store()
    if store is not None and store.code == DEFAULT_STORE_CODE:
        return db.or_(column.is_(None), column == store_id)
    return column == store_id


def visible_store_ids(admin):
    """按角色返回可见门店 id 列表（唯一实现）。

    - 平台管理员：[]（不看业务明细，只由路由层放行商家/门店/账号元数据）
    - 老板：本商家全部门店
    - 店长：AdminStoreAccess 授权的门店
    - 店员：所属门店（由调用方传 staff 的 store_id，不在此处查询）
    """
    if admin is None:
        return []
    role = getattr(admin, 'role', None) or ROLE_OWNER
    if role == ROLE_PLATFORM:
        return []
    merchant_id = getattr(admin, 'merchant_id', None)
    if role == ROLE_OWNER:
        if merchant_id is None:
            return []
        return [s.id for s in Store.query.filter_by(merchant_id=merchant_id).all()]
    if role == ROLE_MANAGER:
        rows = AdminStoreAccess.query.filter_by(admin_id=admin.id).all()
        return [r.store_id for r in rows]
    return []


def current_admin():
    """请求内当前登录的管理员账号；非请求上下文 / 未登录 / 店员登录 → None。"""
    if not has_request_context():
        return None
    from flask_login import current_user

    if not getattr(current_user, 'is_authenticated', False):
        return None
    return current_user if isinstance(current_user, Admin) else None


def visible_merchant_id(admin=None):
    """当前账号所属商家 id；平台管理员与无归属账号返回 None（无业务可见范围）。

    与 `visible_store_ids()` 同口径：平台管理员不看业务明细，只由路由层放行
    商家 / 门店 / 账号元数据。
    """
    if admin is None:
        admin = current_admin()
    if admin is None:
        return None
    if (getattr(admin, 'role', None) or ROLE_OWNER) == ROLE_PLATFORM:
        return None
    return getattr(admin, 'merchant_id', None)


def merchant_scope_clause(column):
    """按当前账号所属商家过滤 ORM 列（越权修复，唯一实现）。

    补足 `store_scope_clause()` 缺失的 merchant 维度：平台管理员与无归属账号
    返回 `db.false()`（不放行任何业务行）；请求外的 CLI / 离线脚本返回
    `db.true()`，保持既有脚本行为。

    NULL 归属行（PH2-2 之前的历史数据）按「归位到当前商家」放行，与
    `store_scope_clause()` 对默认门店的既有口径一致 —— 否则孤儿行谁都管不了。
    """
    if not has_request_context():
        return db.true()
    merchant_id = visible_merchant_id()
    if merchant_id is None:
        return db.false()
    return db.or_(column.is_(None), column == merchant_id)


def in_clause(column, values):
    """按 id 列表过滤；空列表返回 `db.false()`（不放行，兼避空 IN 告警）。"""
    ids = [int(v) for v in (values or []) if v is not None]
    if not ids:
        return db.false()
    return column.in_(ids)


def store_visible_clause(column, store_ids):
    """按可见门店过滤门店维度列；NULL 归属行按历史数据放行。

    与 `merchant_scope_clause()` 配套使用：可见门店为空时只放行 NULL 行，
    不放行任何有明确归属的门店数据；**请求外**（CLI / 离线脚本 / 单测直调）
    返回 `db.true()`，与 `store_scope_clause()` 的既有口径一致。
    """
    if not has_request_context():
        return db.true()
    ids = [int(v) for v in (store_ids or []) if v is not None]
    if not ids:
        return column.is_(None)
    return db.or_(column.is_(None), column.in_(ids))


def visible_staff_ids():
    """当前账号可见店员 id 列表（越权修复，唯一实现）。

    Returns:
        list[int]: 可见店员 id；空列表 = 确无可见范围。
        None: 请求外（CLI / 离线脚本），调用方应保持既有零过滤行为。
    """
    if not has_request_context():
        return None
    admin = current_admin()
    merchant_id = visible_merchant_id(admin)
    if merchant_id is None:
        return []
    rows = Staff.query.filter(
        merchant_scope_clause(Staff.merchant_id),
        store_visible_clause(Staff.store_id, visible_store_ids(admin)),
    ).with_entities(Staff.id).all()
    return [r[0] for r in rows]


def staff_visible(staff):
    """单个店员是否落在当前账号可见范围内（IDOR 收口，唯一实现）。

    同时校验 merchant 与 store 两个维度：merchant 明确不等于当前商家即拒绝；
    store 明确不属于可见门店即拒绝；NULL 归属按历史行归位放行。
    用于堵住 `db.get_or_404(Staff, staff_id)` 改 id 直接读他人数据。
    """
    if staff is None:
        return False
    if not has_request_context():
        # 请求外（CLI / 离线脚本）不收口，保持既有脚本行为
        return True
    admin = current_admin()
    merchant_id = visible_merchant_id(admin)
    store_ids = set(visible_store_ids(admin))
    if merchant_id is None:
        return False
    staff_merchant = getattr(staff, 'merchant_id', None)
    if staff_merchant is not None and staff_merchant != merchant_id:
        return False
    staff_store = getattr(staff, 'store_id', None)
    if staff_store is not None and staff_store not in store_ids:
        return False
    return True


def staff_store_id(staff):
    """店员所属门店 id（无归属返回 None）。"""
    return getattr(staff, 'store_id', None)


def user_role(user):
    """账号角色：Admin → role 字段（缺省 owner）；Staff → staff；其他 → None。"""
    if isinstance(user, Admin):
        return getattr(user, 'role', None) or ROLE_OWNER
    if isinstance(user, Staff):
        return ROLE_STAFF
    return None


def resolve_store_id(user):
    """解析账号的默认门店 id（写路径落 store_id 的唯一入口）。

    优先级：显式配置（全局钩子/环境变量）→ 店员绑定门店 →
    店长首个授权门店 → 老板本商家首个门店 → 平台管理员 None。
    """
    explicit = _configured_store_id()
    if explicit is not None:
        return explicit
    return _role_default_store_id(user)


__all__ = [
    'DEFAULT_MERCHANT_CODE',
    'DEFAULT_MERCHANT_NAME',
    'DEFAULT_STORE_CODE',
    'DEFAULT_STORE_NAME',
    'ROLE_MANAGER',
    'ROLE_OWNER',
    'ROLE_PLATFORM',
    'ROLE_STAFF',
    'allowed_store_ids',
    'current_admin',
    'current_store',
    'current_store_id',
    'default_merchant',
    'default_store',
    'ensure_default_tenant',
    'in_clause',
    'merchant_scope_clause',
    'resolve_store_id',
    'set_current_store',
    'staff_store_id',
    'staff_visible',
    'store_options_for',
    'store_scope_clause',
    'store_visible_clause',
    'user_role',
    'visible_merchant_id',
    'visible_staff_ids',
    'visible_store_ids',
]
