"""登录/登出路由 — 管理员和店员共用，根据角色跳转不同主页"""
import secrets

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import login_required, login_user, logout_user
from sqlalchemy.exc import IntegrityError

from src import conflict_guard, email_verify
from src.models import ROLE_OWNER, ROLE_PLATFORM, Admin, Merchant, Staff, db
from src.rate_limit import check_limit
from src.validators import validate_password_strength

# 验证码失败原因 → 人读文案（注册第二步共用；本轮从函数体内提出来，两条 400 回包合成一条出口）
_VERIFY_COPY = {
    'invalid_input': '请输入 6 位数字验证码',
    'no_pending': '验证码不存在或已使用，请重新获取',
    'expired': '验证码已过期，请重新获取',
    'too_many_attempts': '错误次数过多，请重新获取验证码',
    'mismatch': '验证码不正确',
}

auth_bp = Blueprint('auth', __name__)


def _rotate_session():
    """登录后轮换会话，缓解会话固定攻击（F14）。

    客户端的会话 cookie 在登录后内容已变化会重新下发，这里再写入一个
    一次性登录 nonce，确保旧会话彻底失效，并便于后续审计。
    """
    saved = dict(session)
    session.clear()
    session.update(saved)
    session['_login_nonce'] = secrets.token_hex(16)
    from src import auth_epoch
    auth_epoch.bind_session()


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """账号密码登录。

    Returns:
        Response: 成功跳角色主页；账号密码错误返回登录页（200）；
        账号已被停用返回 403 并给出明确提示（F4），不再静默 200。
    """
    if request.method == 'POST':
        # 接口限流：登录尝试 10 次 / 60s（按 IP+动作计，缓解口令暴力试解；
        # 冒烟/压测环境以 DISABLE_RATE_LIMIT=1 关闭）
        limited = check_limit(action='login')
        if limited is not None:
            return limited
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        role = request.form.get('role', 'staff')  # admin / staff

        if role == 'admin':
            user = Admin.query.filter_by(username=username).first()
        else:
            # 先按用户名取出（不过滤 is_active），凭证正确但账号停用时
            # 才能给出「已停用」而非「账号或密码错误」的准确反馈
            user = Staff.query.filter_by(username=username).first()

        if user and user.check_password(password):
            # 停用账号：凭证有效但禁止登录，明确返回 403
            if isinstance(user, Staff) and not user.is_active:
                flash('该账号已被管理员停用，如需恢复请联系管理员', 'danger')
                return render_template('login.html'), 403
            login_user(user)
            # F14：登录成功后轮换会话，使旧会话失效
            _rotate_session()
            # F4：按账号真实类型决定跳转，避免「取自选 role 导致 admin 进 H5 报 403」
            if isinstance(user, Admin):
                if getattr(user, 'role', None) == ROLE_PLATFORM:
                    return redirect(url_for('admin.platform_demo_reset'))
                return redirect(url_for('admin.dashboard'))
            return redirect(url_for('h5.dashboard'))

        flash('账号或密码错误', 'danger')

    return render_template('login.html')


@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    """登出（P7-E / S-13：由 GET 改为 POST + CSRF）。

    状态变更必须走写方法：GET 时一个 `<img src="/logout">` 就能让管理员/店员
    被跨站强制登出（可用性骚扰），且与项目「写操作一律 POST + CSRF」的范式不符。
    """
    logout_user()
    return redirect(url_for('auth.login'))


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """商家老板自助注册 · 第一步：提交邮箱，发送验证码（PH2-3B）。

    Brevo 未配置时关闭自助注册并给出可操作提示（平台管理员仍可人工开户）。
    """
    enabled = email_verify.signup_enabled()
    if request.method == 'POST':
        limited = check_limit(action='register')
        if limited is not None:
            return limited
        if not enabled:
            flash('当前未开放自助注册（邮件服务未配置），请联系平台管理员人工开户', 'danger')
            return render_template('register.html', signup_enabled=False), 403
        email = email_verify.normalize_email(request.form.get('email', ''))
        result = email_verify.request_code(email, email_verify.PURPOSE_SIGNUP)
        if result['ok']:
            flash('验证码已发送，请查收邮箱（10 分钟内有效）', 'success')
            return redirect(url_for('auth.register_verify', email=email))
        reason = result.get('reason')
        if reason == 'invalid_email':
            flash('请输入有效的邮箱地址', 'danger')
        elif reason == 'too_soon':
            flash(f'发送过于频繁，请 {result.get("retry_after", 60)} 秒后再试', 'warning')
        elif reason == 'not_configured':
            flash('邮件服务未配置，自助注册暂不可用；请联系平台管理员人工开户', 'danger')
        else:
            flash('验证码发送失败，请稍后重试', 'danger')
    return render_template('register.html', signup_enabled=enabled)


def _register_taken(email, enabled):
    """并发同名注册被唯一约束挡下：回滚 + 计数 + 可读回包（不新增端点的返回出口）。"""
    db.session.rollback()
    conflict_guard.note_integrity_reject()
    flash('该账号刚被其他请求占用，请更换一个', 'danger')
    return render_template('register_verify.html', email=email,
                           signup_enabled=enabled), 400


def _register_field_error(email, code, merchant_name, username, password):
    """注册第二步的校验面：必填 → 密码强度 → 账号占用，全过了才消耗验证码次数。

    单独成函数是因为 `register_verify` 本身已带两类出口（未开放/GET 渲染）+ 建号事务，
    再叠四段校验会超开门禁第 37 项的复杂度预算（实测 11 > 10）。
    """
    pwd_error = validate_password_strength(password)
    if not merchant_name or not username or pwd_error:
        return pwd_error or '商家名称与账号必填'
    if Admin.query.filter_by(username=username).first():
        return '该账号已被占用，请更换一个'
    verified = email_verify.verify_code(email, code, email_verify.PURPOSE_SIGNUP)
    if not verified['ok']:
        return _VERIFY_COPY.get(verified['reason'], '验证码校验失败，请重新获取')
    return None


@auth_bp.route('/register/verify', methods=['GET', 'POST'])
def register_verify():
    """商家老板自助注册 · 第二步：校验验证码并创建商家 + 老板账号。"""
    email = email_verify.normalize_email(request.values.get('email', ''))
    enabled = email_verify.signup_enabled()
    if not enabled:
        # 未配置邮件服务：自助注册关闭（页面提示可操作路径），平台管理员仍可人工开户
        if request.method == 'POST':
            flash('当前未开放自助注册（邮件服务未配置），请联系平台管理员人工开户', 'danger')
        return render_template('register_verify.html', email=email,
                               signup_enabled=False), (403 if request.method == 'POST' else 200)
    if request.method != 'POST':
        return render_template('register_verify.html', email=email,
                               signup_enabled=True)

    limited = check_limit(action='register')
    if limited is not None:
        return limited

    code = (request.form.get('code') or '').strip()
    merchant_name = (request.form.get('merchant_name') or '').strip()
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    # 表单本身没过就不消耗验证码次数（校验面见 _register_field_error）
    form_error = _register_field_error(email, code, merchant_name, username, password)
    if form_error:                           # 表单/验证码两类不通过共用这一条出口
        flash(form_error, 'danger')
        return render_template('register_verify.html', email=email,
                               signup_enabled=enabled), 400

    merchant = Merchant(name=merchant_name, contact_email=email, is_active=True)
    db.session.add(merchant)
    db.session.flush()
    owner = Admin(username=username, role=ROLE_OWNER, merchant_id=merchant.id)
    owner.set_password(password)
    db.session.add(owner)
    try:
        db.session.commit()
    except IntegrityError:
        # 验证码窗口内的并发同名注册：回滚 + 可读提示，不冒泡 500（第 31 轮类 C 出口）
        return _register_taken(email, enabled)

    login_user(owner)
    _rotate_session()
    flash('注册成功！请先创建你的第一家门店', 'success')
    return redirect(url_for('admin.dashboard'))
