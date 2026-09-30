"""通用装饰器 — 角色权限控制"""
from functools import wraps

from flask import abort
from flask_login import current_user

from src.models import ROLE_MANAGER, ROLE_OWNER, Admin, Staff


def role_required(*roles):
    """仅允许指定角色的管理员(Admin)访问（PH2-2 多租户权限）。

    - 未登录：由外层 @login_required 拦截跳 /login
    - 店员(Staff)或匿名对象：abort(403)
    - Admin.role 不在 roles 内：abort(403)（平台管理员默认不在业务角色内）
    """
    def _decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated or not isinstance(current_user, Admin):
                abort(403, description='该页面仅管理员账号可访问。')
            role = getattr(current_user, 'role', None) or ROLE_OWNER
            if roles and role not in roles:
                abort(403, description='当前角色无权访问该页面。')
            return view(*args, **kwargs)
        return wrapped
    return _decorator


def business_admin_required(view):
    """业务页（营收/产品/考核/分析）：仅老板与店长；平台管理员 403。"""
    return role_required(ROLE_OWNER, ROLE_MANAGER)(view)


def admin_required(view):
    """仅允许管理员(Admin)访问，店员或未登录者拒绝。

    - 未登录：由外层 @login_required 拦截跳 /login
    - 已登录但非 Admin（如店员）：abort(403) 明确拒绝
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not isinstance(current_user, Admin):
            abort(403, description='该页面仅管理员可访问。')
        return view(*args, **kwargs)
    return wrapped


def staff_required(view):
    """仅允许店员(Staff)访问，管理员或未登录者拒绝。

    - 未登录：由外层 @login_required 拦截跳 /login
    - 已登录但非 Staff（如管理员）：abort(403) 明确拒绝，
      避免以 Admin.id 写入 staff 外键产生脏数据
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not isinstance(current_user, Staff):
            abort(403, description='该页面仅店员账号可访问。')
        return view(*args, **kwargs)
    return wrapped


def staff_or_admin_required(view):
    """允许店员(Staff)或管理员(Admin)访问，其他一律拒绝（F6）。

    用于 AI 培训入口这类「员工角色共用」的页面：管理员需要能进入
    查看培训内容，但任何非员工身份（含伪造 session 的匿名对象）都必须挡住。

    - 未登录：由外层 @login_required 拦截跳 /login
    - 已登录但既不是 Staff 也不是 Admin：abort(403)
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or \
                not isinstance(current_user, (Admin, Staff)):
            abort(403, description='该页面仅门店员工账号可访问。')
        return view(*args, **kwargs)
    return wrapped
