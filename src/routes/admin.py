"""管理员后台路由 — 看板 + 账号管理 + 考核查看"""
import secrets
from datetime import date, timedelta

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from src import conflict_guard, decision_cockpit, exit_audit, industry_pack, metrics, task_flow, tenant
from src.analytics import compute_dashboard_stats, get_sales_dataframe
from src.decorators import admin_required, business_admin_required, role_required
from src.dify_client import (
    DIFY_UNAVAILABLE_BADGE,
    DIFY_UNAVAILABLE_TEXT,
    dify_client,
)
from src.forecast_eval import record_prediction
from src.models import ROLE_PLATFORM, Product, QuizRecord, Staff, active_pack_clause, db
from src.rate_limit import rate_limit_post
from src import business_calendar as cal
from src.validators import (
    MAX_UNIT_PRICE,
    ParseError,
    check_range,
    parse_float,
    validate_password_strength,
)

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

# PH2-2C：平台管理员只看商家/门店/账号/系统元数据，门店业务明细一律 403。
# 集中一处 before_request 而非逐路由挂装饰器 —— 新增业务页不会漏挂。
# 反向风险由门禁第 41 项 ⑧ 盯住：**这条兜底同样会拦死平台自己的入口**。第 28 轮新增
# `/admin/platform/merchants/<id>/erase` 时挂了 `@role_required(ROLE_PLATFORM)`，
# 却没进这份名单 ⇒ 唯一受众（平台管理员）打进来永远 403，而所有既有判据只判
# 「该拒的拒了没」，没有一条判「该放的放得进吗」，于是整条能力静默不可用。
_PLATFORM_ALLOWED_ENDPOINTS = frozenset({
    'admin.staff_list', 'admin.add_staff',
    'admin.toggle_staff', 'admin.reset_staff_password',
    'admin.platform_demo_reset', 'admin.platform_merchant_erase',
})


@admin_bp.before_request
def _guard_platform_business_access():
    """平台管理员访问门店业务明细接口时返回 403（PH2-2C）。"""
    if not getattr(current_user, 'is_authenticated', False):
        return None
    if getattr(current_user, 'role', None) != ROLE_PLATFORM:
        return None
    if request.endpoint in _PLATFORM_ALLOWED_ENDPOINTS:
        return None
    abort(403, description='平台管理员仅可查看商家、门店与账号，无权访问门店业务明细。')

# 看板时间区间白名单：非法/缺省一律回退 365，防任意入参
DASH_DAYS = (7, 30, 90, 365)
DASH_DAYS_DEFAULT = 365


def _parse_range_days():
    """从查询串解析看板时间窗，非法入参回退默认 365。"""
    try:
        days = int(request.args.get('days', DASH_DAYS_DEFAULT))
    except (TypeError, ValueError):
        return DASH_DAYS_DEFAULT
    return days if days in DASH_DAYS else DASH_DAYS_DEFAULT


@admin_bp.route('/platform/demo-reset', methods=['GET', 'POST'])
@login_required
@role_required(ROLE_PLATFORM)
@rate_limit_post('admin_write')
def platform_demo_reset():
    """平台管理员全库重建入口：备份成功后才允许清库播种。"""
    from src import pack_demo

    if request.method == 'POST':
        phrase = (request.form.get('confirm_phrase') or '').strip()
        checked = (request.form.get('confirm_checkbox') or '').lower() in {
            '1', 'true', 'on', 'yes',
        }
        nonce = (request.form.get('reset_nonce') or '').strip()
        expected = session.pop('demo_reset_nonce', None)
        if phrase != pack_demo.RESET_CONFIRM_PHRASE or not checked:
            abort(400, description='确认短语或二次确认未完成，已拒绝重建。')
        if not expected or nonce != expected:
            abort(400, description='重置表单已失效，请刷新页面后重新确认。')
        try:
            report = pack_demo.reset_demo_database(
                days=pack_demo.DEMO_DAYS,
                backup=True,
                keep=pack_demo.BACKUP_KEEP,
            )
        except Exception as exc:
            # exc_info 必带：本页 500 时只有一行 %s 的话，运维拿不到栈，归因只能靠猜
            # （2026-09-26 实测：DetachedInstanceError 被上层捕获后只剩一句消息）
            current_app.logger.error('演示数据重建失败（数据库未修改）: %s', exc, exc_info=True)
            return render_template(
                '500.html',
                message='演示数据重建前备份失败，数据库未修改，请检查备份目录权限。',
            ), 500
        from src import auth_epoch
        auth_epoch.bind_session()
        flash(
            f'演示库已重建：{report["stores"]} 门店 / {report["sales"]} 流水 / '
            f'{report["quiz"]} 培训 / {report["abnormal"]} 异常案例。'
            f'备份：{report.get("backup_path") or "无（原库不存在）"}',
            'success',
        )
        return redirect(url_for('admin.platform_demo_reset'))

    session['demo_reset_nonce'] = secrets.token_urlsafe(18)
    return render_template(
        'admin/platform_demo_reset.html',
        stats=pack_demo.data_stats(),
        accounts=pack_demo.demo_accounts(),
        stores=pack_demo.DEMO_STORE_SPECS,
        confirm_phrase=pack_demo.RESET_CONFIRM_PHRASE,
        reset_nonce=session['demo_reset_nonce'],
    )


@admin_bp.route('/platform/merchants/<int:merchant_id>/erase', methods=['GET', 'POST'])
@login_required
@role_required(ROLE_PLATFORM)
@rate_limit_post('admin_write')
def platform_merchant_erase(merchant_id):
    """商家数据擦除的受控出口（对标第 28 轮 H70）。

    为什么没有页面：这是平台侧低频动作，新增模板要牵动设计体系三副本同步与演示快照重建
    （成本与该动作的频次不成比例）；出口形态改为 JSON + 全套同构护栏，登记为 L 项待裁。
    护栏一条都不能少：`ROLE_PLATFORM` 角色 + CSRF + **逐字确认短语** + 二次确认勾选 +
    **一次性 nonce**（`session.pop` 取走即失效，防重放）+ **可用备份凭证**。
    GET 只回预览（零写入），POST 不齐备即 400 并说明缺哪一条 —— 绝不静默什么都不做，
    因为"200 + 什么也没删"会被读成"擦除成功"。
    """
    from src import erasure

    if request.method == 'GET':
        return jsonify(ok=True, plan=erasure.plan(merchant_id),
                       confirm_phrase=erasure.ERASE_CONFIRM_PHRASE,
                       needs=['confirm_phrase', 'confirm_checkbox', 'erase_nonce', 'backup_path'])
    phrase = (request.values.get('confirm_phrase') or '').strip()
    checked = (request.values.get('confirm_checkbox') or '').lower() in {'1', 'true', 'on', 'yes'}
    nonce = (request.values.get('erase_nonce') or '').strip()
    expected = session.pop('erase_nonce', None)
    if phrase != erasure.ERASE_CONFIRM_PHRASE or not checked:
        abort(400, description='确认短语或二次确认未完成，已拒绝擦除（数据未改动）。')
    if not expected or nonce != expected:
        abort(400, description='擦除表单已失效，请重新 GET 取预览与一次性口令。')
    session['erase_nonce'] = secrets.token_urlsafe(18)
    backup_path = (request.values.get('backup_path') or '').strip()
    try:
        report = erasure.execute(merchant_id, backup_path,
                                 emails=[e for e in (request.values.get('emails') or '').split(',') if e])
    except ValueError as exc:
        abort(400, description=f'擦除被拒绝，数据未改动：{exc}')
    flash(f'商家 {merchant_id} 已擦除：{sum(v.get("deleted", 0) for v in report["tables"].values())} '
          f'行；残留复扫 {report["residual"] or "无"}', 'warning')
    return jsonify(ok=not report['residual'], **report)


@admin_bp.route('/')
@login_required
@business_admin_required
def dashboard():
    """管理员首页：数据看板（Pandas 统计 + ECharts 可视化）"""
    days = _parse_range_days()
    stats = compute_dashboard_stats(days=days)
    decision_days = 30
    store_ids = tenant.allowed_store_ids(current_user)
    decision = decision_cockpit.initial_decision(
        current_user.merchant_id, store_ids, days=decision_days,
        store_id=tenant.current_store_id())
    # 预测回测闭环（第六轮 A）：看板每次打开 → 记录「明日预测」（首次为准）
    # → 回填已到期预测的实际销量 → 滚动计算准确率注入 stats，
    # 经营分析/智能助手文案与看板卡片共用同一指标。
    if stats.get('has_data'):
        actual_by_date = dict(zip(stats.get('daily_iso') or [],
                                  stats['daily']['qty'], strict=False))
        fc = stats.get('forecast') or {}
        if fc.get('ok') and fc.get('target_date'):
            stats['forecast_accuracy'] = record_prediction(
                fc['target_date'], fc.get('qty'), actual_by_date)
        else:
            stats['forecast_accuracy'] = {'ok': False, 'count': 0, 'mape': None,
                                          'accuracy': None, 'hit_rate': None,
                                          'last': None}
    # 步骤4：看板接入 AI 备货建议 + 损耗提醒（AC-OBS-07/08），未配 Dify 自动回退规则版
    ai_advice = dify_client.restock_advice(stats) if stats.get('has_data') else None
    ai_loss = dify_client.loss_alert(stats) if stats.get('has_data') else None
    return render_template(
        'admin/dashboard.html',
        stats=stats,
        ai_advice=ai_advice,
        ai_loss=ai_loss,
        dify_enabled=dify_client.enabled,
        range_days=days,
        range_choices=DASH_DAYS,
        labels=metrics.dashboard_labels(),  # P7-B3：看板文案随包
        # P7-E3-B2 / M-12：降级文案单入口（模板不再内联副本）
        degraded_text=DIFY_UNAVAILABLE_TEXT,
        degraded_badge=DIFY_UNAVAILABLE_BADGE,
        decision=decision,
        decision_days=decision_days,
        task_stores=task_flow.store_options(store_ids),
        task_assignees=task_flow.assignee_options(
            getattr(current_user, 'merchant_id', None), store_ids),
        default_due_date=(cal.business_today() + timedelta(days=3)).isoformat(),
    )


@admin_bp.route('/dashboard/export')
@login_required
@admin_required
def dashboard_export():
    """按选定区间导出原始填报数据为 CSV（utf-8-sig BOM，Excel 打开不乱码）。
    导出属「数据看板」能力，不触碰范围红线。

    两道出口护栏（对标第 20 轮 H42/H43）：
    ① **公式注入中和**：`product_name` / `abnormal_notes` 是用户自由文本，以
       ``=`` ``+`` ``-`` ``@`` 开头的值会被 Excel 求值 —— 转义只在此处做，
       DataFrame 保持原值，统计侧不受影响（`src/csv_guard.py` 有完整理由与取舍说明）；
    ② **行数上限**：窗口参数已被 `DASH_DAYS` 白名单夹住，但**数据量面**没有上限
       （`.all()` 全量入内存），单进程免费层上一个管理员点一次导出即可能拖死整站，
       故越界直接 413 并给出"缩短区间分批导出"的处置口径。
    """
    from src import csv_guard

    days = _parse_range_days()
    df = get_sales_dataframe(days)
    actor = f'admin-{getattr(current_user, "id", 0)}'
    if csv_guard.rows_exceed(len(df)):
        # 拒绝也要留痕：这是"谁在试图一次性取走多少行"的唯一记录点
        exit_audit.record('export_rejected', actor, {'rows': len(df)}, allowed=False)
        return Response(
            f'导出行数 {len(df)} 超过单次上限 {csv_guard.max_export_rows()} 行，'
            f'请缩短时间窗（可选 7/30/90 天）分批导出。',
            status=413, mimetype='text/plain; charset=utf-8',
        )
    csv = csv_guard.escape_dataframe(df).to_csv(index=False)
    fname = f"sales_{cal.business_today():%Y%m%d}_days{days}.csv"
    # 只记行数与字节数：**不记产品名/备注**（安全红线对日志同样成立，见 src/exit_audit.py）
    exit_audit.record('export_csv', actor,
                      {'rows': len(df), 'bytes': len(csv.encode('utf-8'))})
    return Response(
        csv,
        mimetype='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'},
    )


@admin_bp.route('/staff')
@login_required
@admin_required
def staff_list():
    """店员账号管理"""
    # 越权修复（2026-09-21）：只列本商家可见门店内的店员，禁止跨商家回显。
    store_ids = tenant.visible_store_ids(tenant.current_admin())
    staffs = Staff.query.filter(
        tenant.merchant_scope_clause(Staff.merchant_id),
        tenant.store_visible_clause(Staff.store_id, store_ids),
    ).order_by(Staff.created_at.desc()).all()
    return render_template('admin/staff_list.html', staffs=staffs)


@admin_bp.route('/staff/add', methods=['GET', 'POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def staff_add():
    """新增店员账号（含口令复杂度校验，F9）"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        display_name = request.form.get('display_name', '').strip()
        phone = request.form.get('phone', '').strip()
        store_name = request.form.get('store_name', '南昌·红谷滩店').strip()

        if not all([username, password, display_name]):
            flash('用户名、密码、显示名不能为空', 'danger')
            return render_template('admin/staff_form.html')

        pwd_error = validate_password_strength(password)
        if pwd_error:
            flash(pwd_error, 'danger')
            return render_template('admin/staff_form.html')

        exists = Staff.query.filter_by(username=username).first()
        if exists:
            flash('用户名已存在', 'danger')
            return render_template('admin/staff_form.html')

        staff = Staff(
            username=username,
            display_name=display_name,
            phone=phone or None,
            store_name=store_name,
            merchant_id=getattr(tenant.current_admin(), 'merchant_id', None),
            store_id=tenant.resolve_store_id(tenant.current_admin()),
        )
        staff.set_password(password)
        db.session.add(staff)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()   # 先查后插的窗口：并发同名不得冒泡成 500（与 PF-12 同口径）
            conflict_guard.note_integrity_reject()
            flash(f'账号「{username}」刚被其他请求占用，请换一个', 'danger')
            return redirect(url_for('admin.staff_list'))
        flash(f'店员 {display_name} 添加成功', 'success')
        return redirect(url_for('admin.staff_list'))

    return render_template('admin/staff_form.html')


@admin_bp.route('/staff/<int:staff_id>/toggle', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def staff_toggle(staff_id):
    """启用/停用店员。

    状态变更属写操作，只接受 POST 并由全站 CSRFProtect 校验 token（F4），
    杜绝「诱导管理员点一个 <a href> 就静默停用账号」的 CSRF 风险。
    """
    staff = db.get_or_404(Staff, staff_id)
    # IDOR 收口（2026-09-21）：改 id / 构造 POST 也改不动本商家之外的账号。
    if not tenant.staff_visible(staff):
        abort(403)
    st, new_active = conflict_guard.cas_flip(
        Staff, staff_id, 'is_active',
        conflict_guard.claim_value(request.form.get('expect_active')))
    if st == 'conflict':
        conflict_guard.note_cas_conflict(Staff.__tablename__, staff_id, 'is_active',
                                         merchant_id=getattr(current_user, 'merchant_id', None))
        flash('该账号状态刚被别人改过（可能已被启用/停用），请刷新后重试', 'warning')
        return redirect(url_for('admin.staff_list'))
    if new_active is None:            # no_claim 兜底：保持旧行为，计数在 /healthz 可见
        staff.is_active = not staff.is_active
        db.session.commit()
        new_active = staff.is_active
    status = '启用' if new_active else '停用'
    flash(f'{staff.display_name} 已{status}', 'success')
    return redirect(url_for('admin.staff_list'))


@admin_bp.route('/staff/<int:staff_id>/reset-password', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def staff_reset_password(staff_id):
    """管理员重置店员密码（同样受口令复杂度约束，F9）"""
    staff = db.get_or_404(Staff, staff_id)
    if not tenant.staff_visible(staff):
        abort(403)
    password = request.form.get('password', '').strip()
    pwd_error = validate_password_strength(password)
    if pwd_error:
        flash(pwd_error, 'danger')
        return redirect(url_for('admin.staff_list'))
    staff.set_password(password)
    db.session.commit()
    flash(f'{staff.display_name} 的密码已重置', 'success')
    return redirect(url_for('admin.staff_list'))


def _scoped_product_or_404(product_id):
    """取当前激活包 + 当前门店范围内的产品；跨包/跨店 ID 一律 404。

    Args:
        product_id: 产品主键。

    Returns:
        Product: 命中的产品对象。
    """
    product = Product.query.filter(
        Product.id == product_id,
        active_pack_clause(Product.pack_id),
        tenant.store_scope_clause(Product.store_id),
    ).first()
    if product is None:
        abort(404)
    return product


@admin_bp.route('/changes')
@login_required
@admin_required
def change_log_view():
    """变更记录（H59 的出口）：谁在什么时候把哪条主数据从什么改成什么。

    可见范围按安全红线切：老板/店长/管理员只看**本商家**的行；平台管理员只看
    `merchant_id IS NULL` 的平台级行（行业包、商家、管理员账号），商家侧一行都不给。
    业务明细档（流水/考核）本来就只记动作与主键、不记值，这里同样只出动作。
    """
    from src import change_log

    admin = tenant.current_admin()
    role = getattr(admin, 'role', None)
    if role == ROLE_PLATFORM:
        raw = change_log.recent(limit=200, merchant_is_null=True)
        scope_note = '平台级变更（行业包 / 商家 / 管理员账号）；商家侧记录不在此展示'
    else:
        raw = change_log.recent(limit=200, merchant_id=getattr(admin, 'merchant_id', None))
        scope_note = '本商家全部变更记录（店长侧由查询守卫收敛到被授权门店）'
    return render_template('admin/change_log.html',
                           rows=[_change_row_view(r) for r in raw],
                           stats=change_log.stats(), scope_note=scope_note,
                           registry_problems=change_log.validate_registry())


def _change_row_view(row):
    """审计行 → 视图模型。JSON 解析留在路由侧，模板不做反序列化（也杜绝 | safe 面）。"""
    from src import change_log

    diff = change_log.parse_diff(row)
    bulk = bool(diff.get('bulk'))
    return {
        'changed_at': row.changed_at.strftime('%m-%d %H:%M') if row.changed_at else '—',
        'actor': f'{row.actor_kind}#{row.actor_id if row.actor_id is not None else "—"}',
        'actor_role': row.actor_role or '',
        'entity': row.entity, 'entity_pk': row.entity_pk, 'action': row.action,
        'source': row.source,
        'bulk': bulk,
        'rows': diff.get('rows') if bulk else None,
        'detail': diff.get('detail', '') if bulk else '',
        'diff': [{'field': k,
                  'before': '—' if v[0] is None else v[0],
                  'after': '—' if (len(v) < 2 or v[1] is None) else v[1]}
                 for k, v in sorted(diff.items()) if not bulk and isinstance(v, list)],
    }


@admin_bp.route('/products')
@login_required
@admin_required
def product_list():
    """产品管理（P7-B2：按当前激活行业包展示，新增自动归属该包）"""
    products = Product.query.filter(
        active_pack_clause(Product.pack_id),
        tenant.store_scope_clause(Product.store_id)
    ).order_by(Product.category, Product.product_name).all()
    # 行指纹随表单出发（Frappe check_if_latest 同形态）：服务端不留跨请求状态 ⇒ 多 worker 也不失效
    return render_template('admin/product_list.html', products=products,
                           pack=industry_pack.current_pack(),
                           occ={p.id: conflict_guard.fingerprint(p) for p in products})


@admin_bp.route('/products/add', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def product_add():
    """新增产品（单价显式解析 + 范围校验，F13/F14；归属当前激活包，P7-B2）"""
    name = request.form.get('product_name', '').strip()
    category = request.form.get('category', '').strip()

    if not name:
        flash('产品名称不能为空', 'danger')
        return redirect(url_for('admin.product_list'))

    try:
        price = parse_float(request.form.get('unit_price'), '产品单价', default=0.0)
        # 成本价可选：空串/None → 待录入（毛利率不参与计算）
        raw_cost = (request.form.get('cost_price') or '').strip()
        cost_price = (parse_float(raw_cost, '成本价', default=None)
                      if raw_cost else None)
    except ParseError as exc:
        abort(400, description=str(exc))

    range_error = check_range(price, '产品单价', MAX_UNIT_PRICE)
    if range_error:
        flash(range_error, 'danger')
        return redirect(url_for('admin.product_list'))
    if cost_price is not None:
        cost_range_error = check_range(cost_price, '成本价', MAX_UNIT_PRICE)
        if cost_range_error:
            flash(cost_range_error, 'danger')
            return redirect(url_for('admin.product_list'))

    # PH2-2：产品名唯一性收敛到门店范围（跨店同名合法，同店不可重名）
    _store_id = tenant.resolve_store_id(current_user)
    exists = Product.query.filter_by(product_name=name, store_id=_store_id).first()
    if exists:
        if exists.pack_id == industry_pack.active_pack_id():
            flash(f'当前门店当前行业包已有产品「{name}」', 'warning')
        else:
            flash(f'产品名「{name}」在当前门店已被其他行业包占用，请换个名称', 'warning')
        return redirect(url_for('admin.product_list'))

    product = Product(product_name=name, unit_price=price, category=category or None,
                      cost_price=cost_price,
                      pack_id=industry_pack.active_pack_id(),
                      store_id=_store_id)
    db.session.add(product)
    try:
        db.session.commit()
    except IntegrityError:
        # P7-E3 / PF-12：并发同名提交（唯一性先查后插的 TOCTOU）→ 回滚 + 友好提示，
        # 避免 IntegrityError 冒泡成 500（对比 h5.submit 已按此处理）。
        db.session.rollback()
        flash(f'产品名「{name}」刚被其他请求占用，请换个名称重试', 'warning')
        return redirect(url_for('admin.product_list'))
    flash(f'产品 {name} 添加成功', 'success')
    return redirect(url_for('admin.product_list'))


# ── 产品管理：内联编辑/上下架/删除/批量（超市web 移植，fetch AJAX 消费）──
# 全部 POST + JSON，CSRF 由请求头 X-CSRFToken 校验（Flask-WTF 原生支持 header）。


def _product_json_ok(message="", **extra):
    """构造统一 JSON 成功响应（fetch 消费，含可选的额外字段）。"""
    payload = {"ok": True, "message": message}
    payload.update(extra)
    return jsonify(payload)


def _note_conflict(exc, merchant_id=None):
    """冲突被拒也要留痕：只记表/主键/字段名，绝不记业务值（第 26/28 轮红线同口径）。"""
    from src import change_log

    change_log.note_denied(exc.table, exc.obj_id, exc.changed_fields,
                           merchant_id=merchant_id, store_id=exc.store_id)


@admin_bp.errorhandler(conflict_guard.ConflictError)
def _on_write_conflict(exc):
    """整行覆盖被拦下的统一回包：fetch 端点拿 409 JSON，行内红条显示同一句话。"""
    _note_conflict(exc, getattr(current_user, "merchant_id", None))
    return jsonify({"ok": False, "error": "conflict", "fields": list(exc.changed_fields),
                    "message": "这一行在你打开之后已被他人修改（字段："
                               + "、".join(exc.changed_fields)
                               + "），请刷新后重改，别直接覆盖别人的值"}), 409


@admin_bp.route('/products/<int:product_id>/toggle', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def product_toggle(product_id):
    """上架/下架产品。H5 填报下拉依赖产品列表，前端已 confirm 防误触。"""
    product = _scoped_product_or_404(product_id)
    # 单列读改写走 CAS：期望值必须由客户端主张（Saleor AGENTS.md 反对 check-then-act）
    st, new_active = conflict_guard.cas_flip(
        Product, product_id, 'is_active',
        conflict_guard.claim_value(request.form.get('expect_active')))
    if st == 'missing':
        abort(404)
    if st == 'conflict':
        conflict_guard.note_cas_conflict(Product.__tablename__, product_id, 'is_active',
                                         store_id=product.store_id,
                                         merchant_id=getattr(current_user, 'merchant_id', None))
        return jsonify({'ok': False, 'error': 'conflict',
                        'message': '该产品的上下架状态刚被别人改过，请刷新后再操作'}), 409
    if new_active is None:            # no_claim：老页面没带期望值 ⇒ 退化成原行为并计数
        product.is_active = not product.is_active
        db.session.commit()
        new_active = product.is_active
    label = '在售' if new_active else '下架'
    return _product_json_ok(f'{product.product_name} 已{label}')


@admin_bp.route('/products/<int:product_id>/update', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def product_update(product_id):
    """内联编辑产品。参数错误返回 400 JSON（不走 abort HTML 错误页，保证 fetch 可解析）。"""
    product = _scoped_product_or_404(product_id)
    name = (request.form.get('product_name') or '').strip()
    if not name:
        return jsonify({"ok": False, "message": "产品名称不能为空"}), 400
    try:
        price = parse_float(request.form.get('unit_price'), '产品单价', default=0.0)
        raw_cost = (request.form.get('cost_price') or '').strip()
        cost_price = (parse_float(raw_cost, '成本价', default=None) if raw_cost else None)
    except ParseError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    range_error = check_range(price, '产品单价', MAX_UNIT_PRICE)
    cost_range_error = (check_range(cost_price, '成本价', MAX_UNIT_PRICE)
                        if cost_price is not None else None)
    combined_range_error = range_error or cost_range_error
    if combined_range_error:
        return jsonify({"ok": False, "message": combined_range_error}), 400
    dup = Product.query.filter(Product.product_name == name, Product.id != product.id).first()
    if dup:
        return jsonify({"ok": False,
                        "message": "产品名已存在"}), 400
    # 冲突交给蓝图级 handler 回 409：端点自身的返回出口数量不因守卫而膨胀
    conflict_guard.check(product, request.form.get(conflict_guard.FORM_FIELD))
    product.product_name = name
    product.unit_price = price
    product.cost_price = cost_price
    product.category = (request.form.get('category') or '').strip() or None
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"ok": False, "message": "产品名冲突，请换个名称重试"}), 400
    return _product_json_ok(f'{name} 已更新')


@admin_bp.route('/products/<int:product_id>/delete', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def product_delete(product_id):
    """删除产品：有历史流水引用时**归档**，无引用才物理删（H60，第 26 轮）。

    旧 docstring 写「硬删除：DailySales 存产品名字符串，无外键风险」—— 这句话只对外键
    成立，对**按名字回查成本价**的毛利率不成立。实测：删掉一个产品在用后，同一块看板上
    「产品营收占比」的分母仍是 4851.0，而毛利率口径的分母掉到 2943.0（差 1908.0，39.3%），
    且历史毛利率随之静默移动。对标 InvenTree `part/models.py:584-604`（仍 active 禁删、
    被引用禁删）与 Frappe `delete_doc.py:240-258`（默认归档 + 存整行前像）。

    物理删除仍然允许（新种的产品不该被永久锁住），但删除动作现在必定留下一条
    带前像的 change_log 行 —— 「删了查不回来」这一档从此不存在。
    """
    from src import change_log

    product = _scoped_product_or_404(product_id)
    name = product.product_name
    referenced = _sales_referrer_count(product)
    if referenced:
        product.is_active = False
        change_log.mark_archive(product)
        db.session.commit()
        return _product_json_ok(
            f'{name} 有 {referenced} 条历史流水在用，已归档下架（不删目录、不动历史）')
    db.session.delete(product)
    db.session.commit()
    return _product_json_ok(f'{name} 已删除')


def _sales_referrer_count(product):
    """该产品名在本店本包下还有多少条历史流水（含 pack_id 为 NULL 的 P2 前历史行）。

    判据宁宽勿松：只要有任何一条流水引用，就不许把目录行删掉 —— 少算一条就等于
    把本轮实测抓到的那个「分母静默缩水」重新放出去。
    """
    from src.models import DailySales

    pack_id = product.pack_id or industry_pack.DEFAULT_PACK_ID
    return DailySales.query.filter(
        DailySales.product_name == product.product_name,
        DailySales.store_id == product.store_id,
        db.or_(DailySales.pack_id == pack_id, DailySales.pack_id.is_(None)),
    ).count()


_BATCH_ACTIONS = ('enable', 'disable', 'delete', 'set_price')


def _parse_batch_ids():
    """解析批量产品 ID（逗号分隔）。返回 (ids, 错误信息)，错误时 ids 为 None。"""
    ids = []
    for chunk in (request.form.get('ids') or '').split(','):
        piece = chunk.strip()
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except ValueError:
            return None, '产品ID不合法'
    if not ids:
        return None, '请先选择产品'
    return ids, None


def _apply_simple_action(products, action):
    """enable / disable / delete 三种纯状态批量操作。

    Returns:
        str | None: 成功返回消息文案；未知 action 返回 None（交给调用方处理）。
    """
    if action == 'enable':
        for p in products:
            p.is_active = True
        db.session.commit()
        return f'已批量上架 {len(products)} 项'
    if action == 'disable':
        for p in products:
            p.is_active = False
        db.session.commit()
        return f'已批量下架 {len(products)} 项'
    if action == 'delete':
        from src import change_log

        archived = deleted = 0
        for p in products:
            if _sales_referrer_count(p):
                p.is_active = False
                change_log.mark_archive(p)
                archived += 1
                continue
            db.session.delete(p)
            deleted += 1
        db.session.commit()
        # 与单条删除同一条规则：有历史引用的一律归档，绝不让目录行凭空消失
        if archived and deleted:
            return f'已归档 {archived} 项（历史流水仍在用）、删除 {deleted} 项'
        if archived:
            return f'已归档 {archived} 项（历史流水仍在用，目录不删、历史不动）'
        return f'已批量删除 {len(products)} 项'
    return None


def _apply_batch_discount(products, raw_price):
    """折扣批量改价（NN% → 原价×NN/100，保留 2 位）。返回错误信息或 None。

    改价后逐项复检 MAX_UNIT_PRICE 上限（P3-1：原实现直接写库，可把单价
    抬到合理范围之外污染看板口径）。"""
    discount = parse_float(raw_price[:-1], '折扣', default=None)
    if discount is None:
        return '折扣格式非法'
    if not 0 < discount < 1000:
        return '折扣需在 0-1000 之间'
    for p in products:
        new_price = round(float(p.unit_price) * discount / 100, 2)
        range_error = check_range(new_price, f'{p.product_name} 改后单价', MAX_UNIT_PRICE)
        if range_error:
            return range_error
        p.unit_price = new_price
    return None


def _apply_batch_flat(products, raw_price):
    """统一价批量改价。返回错误信息或 None。"""
    price = parse_float(raw_price, '统一价', default=None)
    if price is None:
        return '统一价格式非法'
    range_error = check_range(price, '产品单价', MAX_UNIT_PRICE)
    if range_error:
        return range_error
    for p in products:
        p.unit_price = price
    return None


def _apply_batch_price(products, raw_price):
    """批量改价：统一价（纯数字）或 NN% 折扣。返回错误信息或 None。"""
    raw_price = (raw_price or '').strip()
    if not raw_price:
        return '请填写统一价或折扣'
    try:
        if raw_price.endswith('%'):
            return _apply_batch_discount(products, raw_price)
        return _apply_batch_flat(products, raw_price)
    except ParseError as exc:
        return str(exc)


@admin_bp.route('/products/batch', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def product_batch():
    """批量操作：enable / disable / delete / set_price（统一价 或 NN% 折扣）。"""
    action = request.form.get('action', '').strip()
    if action not in _BATCH_ACTIONS:
        return jsonify({"ok": False, "message": "未知批量操作"}), 400
    ids, err = _parse_batch_ids()
    if err:
        return jsonify({"ok": False, "message": err}), 400
    # P7-B2：批量操作限定在当前激活包范围内，跨包 ID 直接拒绝（防越权改他包）
    products = Product.query.filter(Product.id.in_(ids),
                                    active_pack_clause(Product.pack_id),
                                    tenant.store_scope_clause(Product.store_id)).all()
    if len(products) != len(set(ids)):
        return jsonify({"ok": False,
                        "message": "所选产品包含其他行业包的条目，请刷新页面重试"}), 400
    msg = _apply_simple_action(products, action)
    if msg:
        return _product_json_ok(msg, count=len(products))
    price_err = _apply_batch_price(products, request.form.get('price'))
    if price_err:
        return jsonify({"ok": False, "message": price_err}), 400
    db.session.commit()
    return _product_json_ok(f'已批量改价 {len(products)} 项', count=len(products))


@admin_bp.route('/quiz')
@login_required
@admin_required
def quiz_list():
    """查看员工考核情况"""
    store_ids = tenant.visible_store_ids(tenant.current_admin())
    staffs = Staff.query.filter(
        tenant.merchant_scope_clause(Staff.merchant_id),
        tenant.store_visible_clause(Staff.store_id, store_ids),
    ).with_entities(Staff.id).all()
    records = QuizRecord.query.filter(
        tenant.in_clause(QuizRecord.staff_id, [s[0] for s in staffs]),
    ).order_by(QuizRecord.created_at.desc()).limit(50).all()
    return render_template('admin/quiz_list.html', records=records)


# ══ H5 数据批量导入（2026-09-24 对标批次1：已有收银系统数据一次性迁移）══
@admin_bp.route('/import', methods=['GET', 'POST'])
@login_required
@business_admin_required
@rate_limit_post('admin_write')
def data_import_view():
    """CSV 流水批量导入：预览（校验报告 + 失败行回执）→ 确认落库。

    无状态两段式：preview 与 commit 都从同一上传文件重新解析（session 有
    3500B Cookie 预算约束，不缓存预览），commit 幂等可重放（重复行自动跳过）。
    门店选择限定当前账号可见门店（店长仅授权门店）；产品白名单与店员解析
    全部限定在该门店 + 该门店激活包内，与店员手工填报受同一套约束。
    """
    import os as _os
    from src import data_import as _di
    from src.models import Store as _Store

    admin = tenant.current_admin()
    stores = tenant.store_options_for(admin)
    if not stores:
        flash('当前账号没有可导入数据的门店', 'warning')
        return redirect(url_for('admin.dashboard'))
    # 该门店可选默认店员（行内店员列缺省时使用）
    def _staff_options(store_id):
        return Staff.query.filter(
            Staff.is_active.is_(True), Staff.store_id == store_id
        ).order_by(Staff.id).all()

    if request.method == 'GET':
        return render_template('admin/data_import.html', stores=stores,
                               staff_options={s['id']: _staff_options(s['id'])
                                              for s in stores})

    upload = request.files.get('file')
    mode = request.form.get('mode', 'preview')
    if mode not in ('preview', 'commit'):
        abort(400, description='未知导入模式')
    if upload is None or not upload.filename:
        flash('请选择要导入的 CSV 文件', 'danger')
        return redirect(url_for('admin.data_import_view'))
    if _os.path.splitext(upload.filename)[1].lower() in ('.xlsx', '.xls'):
        flash('暂不支持 Excel 直读，请在 Excel 中「另存为 CSV(UTF-8)」后重新上传',
              'warning')
        return redirect(url_for('admin.data_import_view'))

    # 门店越权校验：store_id 必须在当前账号可选门店集合内
    try:
        store_id = int(request.form.get('store_id', ''))
    except ValueError:
        abort(400, description='门店参数非法')
    if store_id not in {s['id'] for s in stores}:
        # 越权尝试必须留痕（比成功更值得记）：只记 id 与被拒事实。
        # 用 content_length 而不是 upload.read() —— 读一次就会把流消耗掉，
        # 下面 build_report 再读到的就是空文件（自己造缺陷的那种"顺手一写"）。
        exit_audit.record('import_csv', f'admin-{getattr(admin, "id", 0)}',
                          {'mode': mode, 'store_id': store_id,
                           'bytes': upload.content_length or 0}, allowed=False)
        abort(400, description='所选门店不在当前账号可用范围内')
    store = db.session.get(_Store, store_id)

    try:
        default_staff_id = int(request.form.get('default_staff_id', '') or 0)
    except ValueError:
        default_staff_id = 0
    if default_staff_id not in {s.id for s in _staff_options(store_id)}:
        default_staff_id = (_staff_options(store_id)[0].id
                            if _staff_options(store_id) else None)
    if default_staff_id is None:
        flash('该门店还没有在售店员账号，请先在「店员管理」创建店员再导入', 'danger')
        return redirect(url_for('admin.data_import_view'))

    payload = upload.read()
    _actor = f'admin-{getattr(admin, "id", 0)}'
    try:
        report = _di.build_report(admin, store, payload,
                                  default_staff_id, commit=(mode == 'commit'))
    except _di.ImportError_ as exc:
        exit_audit.record('import_rejected', _actor,
                          {'mode': mode, 'store_id': store_id, 'bytes': len(payload)})
        flash(str(exc), 'danger')
        return redirect(url_for('admin.data_import_view'))
    except IntegrityError:
        exit_audit.record('import_conflict', _actor,
                          {'mode': mode, 'store_id': store_id, 'bytes': len(payload)})
        db.session.rollback()
        flash('导入过程中发生数据冲突，本次未写入任何行，请检查重复数据后重试', 'danger')
        return redirect(url_for('admin.data_import_view'))

    # 只记计数与 id：产品名/备注等业务明细一律不入日志（安全红线对日志同样成立）
    exit_audit.record('import_csv', _actor,
                      {'mode': mode, 'store_id': store_id, 'rows': report['total'],
                       'inserted': report['inserted'], 'duplicates': report['duplicates'],
                       'failed': len(report['errors']), 'bytes': len(payload)})

    if mode == 'commit':
        flash('导入完成：新增 %d 行 / 跳过重复 %d 行 / 失败 %d 行'
              % (report['inserted'], report['duplicates'], len(report['errors'])),
              'success' if not report['errors'] else 'warning')
    return render_template('admin/data_import.html', stores=stores,
                           staff_options={s['id']: _staff_options(s['id'])
                                          for s in stores},
                           report=report, mode=mode,
                           store_id=store_id,
                           default_staff_id=default_staff_id)
