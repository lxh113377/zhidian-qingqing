"""门店与成员管理（PH2-3C）：老板建门店、邀请店长/店员加入。

权限：老板可建门店与邀请成员；店长只读被授权门店；平台管理员在本蓝图
一律 403（`role_required(ROLE_OWNER, ROLE_MANAGER)`）。
跨商家门店 ID 一律 404（所有查询先按 `current_user.merchant_id` 收敛）。
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from src import conflict_guard, industry_pack, pack_store, tenant
from src.decorators import role_required
from src.models import (
    ROLE_MANAGER,
    ROLE_OWNER,
    Admin,
    AdminStoreAccess,
    Staff,
    Store,
    db,
)
from src.rate_limit import rate_limit_post
from src.validators import validate_password_strength

stores_bp = Blueprint('stores', __name__, url_prefix='/admin/stores')


def _my_store_or_404(store_id):
    """当前商家范围内的门店；跨商家 ID 一律 404。"""
    store = Store.query.filter_by(id=store_id,
                                  merchant_id=current_user.merchant_id).first()
    if store is None:
        abort(404)
    return store


def _username_taken(username):
    return bool(Admin.query.filter_by(username=username).first()
                or Staff.query.filter_by(username=username).first())


@stores_bp.route('/')
@login_required
@role_required(ROLE_OWNER, ROLE_MANAGER)
def store_list():
    """门店列表：老板看本商家全部；店长看被授权门店。"""
    if (getattr(current_user, 'role', None) or ROLE_OWNER) == ROLE_OWNER:
        stores = (Store.query.filter_by(merchant_id=current_user.merchant_id)
                  .order_by(Store.id).all())
    else:
        ids = tenant.visible_store_ids(current_user) or [0]
        stores = Store.query.filter(Store.id.in_(ids)).order_by(Store.id).all()

    managers = Admin.query.filter_by(merchant_id=current_user.merchant_id,
                                     role=ROLE_MANAGER).all()
    manager_access = {
        m.id: [r.store_id for r in
               AdminStoreAccess.query.filter_by(admin_id=m.id).all()]
        for m in managers
    }
    staffs = Staff.query.filter_by(merchant_id=current_user.merchant_id).all()
    current = tenant.current_store_id()
    return render_template('admin/store_list.html', stores=stores,
                           managers=managers, manager_access=manager_access,
                           staffs=staffs, can_manage=True,
                           current_store_id=current,
                           packs=industry_pack.list_packs())


@stores_bp.route('/<int:store_id>/pack', methods=['POST'])
@login_required
@role_required(ROLE_OWNER)
@rate_limit_post('admin_write')
def set_store_pack(store_id):
    """设置门店当前行业包（一店一当前包；空值 = 跟随全局）。"""
    store = _my_store_or_404(store_id)
    pack_id = (request.form.get('pack_id') or '').strip()
    ok, message = pack_store.set_store_pack(store.id, pack_id)
    flash(message, 'success' if ok else 'danger')
    return redirect(url_for('stores.store_list'))


@stores_bp.route('/switch', methods=['POST'])
@login_required
@role_required(ROLE_OWNER, ROLE_MANAGER)
@rate_limit_post('admin_write')
def switch_store():
    """切换当前门店（PH2-3D）：仅允许切到账号可选集合内的门店。"""
    raw = request.form.get('store_id')
    try:
        store_id = int(raw)
    except (TypeError, ValueError):
        abort(400, description='缺少有效的门店标识。')
    store = _my_store_or_404(store_id)
    if store_id not in tenant.allowed_store_ids(current_user):
        abort(403, description='无权切换到该门店。')
    session['store_id'] = store.id
    flash(f'已切换到门店「{store.name}」', 'success')
    return redirect(url_for('stores.store_list'))


@stores_bp.route('/add', methods=['POST'])
@login_required
@role_required(ROLE_OWNER)
@rate_limit_post('admin_write')
def add_store():
    """老板新建门店（同商家内编码唯一）。"""
    name = (request.form.get('name') or '').strip()
    code = (request.form.get('code') or '').strip()
    address = (request.form.get('address') or '').strip() or None
    phone = (request.form.get('phone') or '').strip() or None
    error = None
    if not name or not code:
        error = '门店名称与门店编码必填'
    elif len(code) > 50 or len(name) > 100:
        error = '门店名称或编码过长'
    elif Store.query.filter_by(merchant_id=current_user.merchant_id,
                               code=code).first():
        error = f'门店编码「{code}」已存在，请更换'
    if error:
        flash(error, 'danger')
        return redirect(url_for('stores.store_list'))
    db.session.add(Store(merchant_id=current_user.merchant_id, name=name,
                         code=code, address=address, phone=phone, is_active=True))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        conflict_guard.note_integrity_reject()
        flash(f'门店编码「{code}」刚被其他请求占用，请换一个', 'danger')
        return redirect(url_for('stores.store_list'))
    flash(f'门店「{name}」已创建', 'success')
    return redirect(url_for('stores.store_list'))


@stores_bp.route('/<int:store_id>/members/add', methods=['POST'])
@login_required
@role_required(ROLE_OWNER)
@rate_limit_post('admin_write')
def add_member(store_id):
    """邀请成员：店长（写 AdminStoreAccess 授权）或店员（绑定该门店）。"""
    store = _my_store_or_404(store_id)
    member_role = (request.form.get('role') or 'staff').strip()
    username = (request.form.get('username') or '').strip()
    display_name = (request.form.get('display_name') or '').strip()
    password = request.form.get('password') or ''
    error = None
    # Q1：与注册 / 后台建号同口径（F9），堵住「纯数字/纯字母弱口令」旁路
    pwd_error = validate_password_strength(password)
    if not username:
        error = '账号必填'
    elif pwd_error:
        error = pwd_error
    elif member_role not in (ROLE_MANAGER, 'staff'):
        error = '成员角色不合法'
    elif _username_taken(username):
        error = f'账号「{username}」已被占用，请更换'
    if error:
        flash(error, 'danger')
        return redirect(url_for('stores.store_list'))

    if member_role == ROLE_MANAGER:
        member = Admin(username=username, role=ROLE_MANAGER,
                       merchant_id=current_user.merchant_id)
        member.set_password(password)
        db.session.add(member)
        db.session.flush()
        db.session.add(AdminStoreAccess(admin_id=member.id, store_id=store.id))
        role_label = '店长'
    else:
        member = Staff(username=username,
                       display_name=display_name or username,
                       merchant_id=current_user.merchant_id,
                       store_id=store.id, store_name=store.name)
        member.set_password(password)
        db.session.add(member)
        role_label = '店员'
    db.session.commit()
    flash(f'{role_label}「{username}」已加入门店「{store.name}」', 'success')
    return redirect(url_for('stores.store_list'))


@stores_bp.route('/<int:store_id>/toggle', methods=['POST'])
@login_required
@role_required(ROLE_OWNER)
@rate_limit_post('admin_write')
def toggle_store(store_id):
    """启用/停用门店（跨商家 404）。"""
    store = _my_store_or_404(store_id)
    st, new_active = conflict_guard.cas_flip(
        Store, store_id, 'is_active',
        conflict_guard.claim_value(request.form.get('expect_active')))
    if st == 'conflict':
        conflict_guard.note_cas_conflict(Store.__tablename__, store_id, 'is_active',
                                         merchant_id=getattr(current_user, 'merchant_id', None))
        flash('该门店状态刚被别人改过，请刷新后重试', 'warning')
        return redirect(url_for('stores.store_list'))
    if new_active is None:
        store.is_active = not store.is_active
        db.session.commit()
        new_active = store.is_active
    flash(f'门店「{store.name}」已{"启用" if new_active else "停用"}', 'success')
    return redirect(url_for('stores.store_list'))
