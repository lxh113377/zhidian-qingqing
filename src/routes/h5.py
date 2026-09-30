"""店员H5功能页面 — 数据填报"""
import json
import logging
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from src import industry_pack, metrics, pack_store, task_flow, tenant
from src.decorators import staff_required
from src.knowledge_base import topic_suggestion, weak_domain_counts
from src.learner_model import DIFFICULTY_LABELS, learning_focus
from src.models import DailySales, Product, QuizRecord, StoreTask, active_pack_clause, db
from src.rate_limit import rate_limit
from src import business_calendar as cal
from src.validators import (
    MAX_MATERIAL,
    MAX_REVENUE,
    MAX_SALES_QTY,
    ParseError,
    check_range,
    parse_float,
    parse_int,
)

h5_bp = Blueprint('h5', __name__, url_prefix='/h5')
logger = logging.getLogger(__name__)

# 培训进度页薄弱方向的中文映射（与模板共用同一份口径，F18）
QUIZ_TYPE_LABELS = {
    'question': '问答',
    'scenario': '情景',
}


def _emit_submit_webhook(user, record, product_name, sales_qty, total_revenue):
    """填报落库后触发 OPT-A Webhook（默认关闭；内部吞异常，绝不阻断主链路）。

    单独成函数是为把外发细节从 submit 视图剥离，避免 submit 语句数继续膨胀；
    仅当店员账号带商家归属时才触发。
    """
    merchant_id = getattr(user, 'merchant_id', None)
    if not merchant_id:
        return
    from src import api_open
    api_open.notify_hook('sales.submitted', merchant_id, {
        'record_id': record.id, 'store_id': record.store_id,
        'date': record.record_date.isoformat(), 'product_name': product_name,
        'quantity': sales_qty, 'revenue': float(total_revenue or 0)})


@h5_bp.route('/')
@login_required
@staff_required
def dashboard():
    """店员主页：今日填报 + 历史记录"""
    today = cal.business_today()
    # P7-B2：下拉清单按当前激活行业包过滤（默认包放行 NULL = 历史数据）
    products = Product.query.filter(
        Product.is_active.is_(True), active_pack_clause(Product.pack_id),
        tenant.store_scope_clause(Product.store_id)).all()
    today_records = DailySales.query.filter_by(
        staff_id=current_user.id, record_date=today
    ).all()
    return render_template(
        'h5/dashboard.html',
        products=products,
        today_records=today_records,
        today=today,
        form=metrics.form_skeleton(),
        # H4 离线队列的归属四元组（店员×门店×包×包协议版本）。刻意取 `tenant.current_store_id()`
        # 而不是 `current_user.store_id`：前者是服务端真正归属记录的单一真相源（含会话内显式切店），
        # 取后者会让「队列里的门店」与「服务端落库的门店」在切店场景下不是同一个量。
        # 店员位是第 49 轮探针逼出来的：同店两账号当日重放，条目会以**当前登录者**身份入账
        # （实测 B 名下流水 0→1），只按门店判归属对这一形态零判定力。
        scope_staff_id=current_user.id,
        scope_store_id=tenant.current_store_id(),
        scope_pack_id=pack_store.active_pack_id(),
        scope_pack_ver=industry_pack.PACK_SCHEMA_VERSION,
    )


@h5_bp.route('/submit', methods=['POST'])
@login_required
@staff_required
@rate_limit(action='submit')
def submit():  # noqa: PLR0911, PLR0915 - 校验顺序（先便宜后昂贵）与逐类拦截提示是业务本质，
               # 拆分会让「提示最贴近用户实际错误」这一既验证过的行为变得不可追溯
    """提交当日营业数据。

    字段键与文案随当前行业包（P7）：骨架字段经 `metrics.form_skeleton()` /
    `skeleton_key()` / `skeleton_label()` 解析，包未声明 stock/waste 的角色
    整块不渲染、此处也不解析（存 NULL，与既有「未填即可选」语义一致）。

    校验顺序（先便宜后昂贵，且保证提示信息最贴近用户实际错误）：
      1. 主记录维度非空；
      2. 数值字段显式解析，格式非法 → 400（F13，不再静默回退 0）；
      3. 符号与合理上限校验 → flash 拦截（F14）；
      4. 产品名白名单（取自 products 表**当前激活包**的在售产品，P7-B2）→ flash 拦截（F15）。

    Returns:
        Response: 成功/被拦截均重定向回填报页并 flash 提示；
        入参格式非法时返回 400 错误页。
    """
    record_date = cal.business_today()
    form = metrics.form_skeleton()
    dim = form['dimension'] or {'name': 'product_name', 'label': '产品'}
    note = form['note'] or {'name': 'abnormal_notes'}
    qty_key = metrics.skeleton_key(metrics.ROLE_QUANTITY)
    amt_key = metrics.skeleton_key(metrics.ROLE_AMOUNT)
    stock_key = metrics.skeleton_key(metrics.ROLE_STOCK)
    waste_key = metrics.skeleton_key(metrics.ROLE_WASTE)
    qty_label = metrics.skeleton_label(metrics.ROLE_QUANTITY, '销量')
    amt_label = metrics.skeleton_label(metrics.ROLE_AMOUNT, '营收')
    stock_label = metrics.skeleton_label(metrics.ROLE_STOCK, '原料剩余')
    waste_label = metrics.skeleton_label(metrics.ROLE_WASTE, '原料损耗')

    product_name = request.form.get(dim['name'], '').strip()
    notes = request.form.get(note['name'], '').strip() if note.get('name') else ''

    if not product_name:
        flash(f'请选择{dim["label"]}', 'danger')
        return redirect(url_for('h5.dashboard'))

    # ── 1.5 H4 离线重放日期守卫 ──
    # record_date 恒按服务端提交时刻算，隔夜补交会把昨天的数写成今天（真实数据污染）。
    # 仅当客户端显式带回 queued_for（= 本机离线队列重放）且与今天不一致时拒写；
    # 正常在线提交不带该字段，既有行为与全部既有断言完全不变。
    queued_for = (request.form.get('queued_for') or '').strip()
    if queued_for and queued_for != record_date.isoformat():
        flash(f'这条是 {queued_for} 的离线记录，已不能计入今天，请当天重新填报', 'danger')
        return redirect(url_for('h5.dashboard'))

    # ── 2. 显式类型解析：非法输入直接 400，杜绝静默回退默认值 ──
    # 键为 None = 该角色归自定义指标组（已由 parse_custom_metrics 接管）或未声明
    try:
        sales_qty = parse_int(request.form.get(qty_key), qty_label, default=0) \
            if qty_key else 0
        total_revenue = parse_float(request.form.get(amt_key), amt_label, default=0.0) \
            if amt_key else 0.0
        material_remaining = parse_float(
            request.form.get(stock_key), stock_label, default=None) if stock_key else None
        material_waste = parse_float(
            request.form.get(waste_key), waste_label, default=None) if waste_key else None
    except ParseError as exc:
        abort(400, description=str(exc))

    # ── 2.2 PF-4：骨架角色未映射到既有列时，数据只进 metrics_json，
    #         而 analytics 主 KPI 仍读 typed 列 → 已入库但看板全零且零提示。
    #         三内置包都映射了 legacy 列故不会触发，属潜伏缺陷：此处只留可见日志，
    #         不改变既有写入行为（避免影响存量回归）。
    for _role, _key in ((metrics.ROLE_QUANTITY, qty_key),
                        (metrics.ROLE_AMOUNT, amt_key)):
        if not _key:
            logger.warning(
                '[h5.submit] 骨架角色 %s 未映射既有列，填报值只写入 metrics_json，'
                '看板主 KPI 将读不到该值（pack=%s product=%s）',
                _role, industry_pack.active_pack_id(), product_name)

    # ── 2.5 自定义指标解析（P2）：按当前包定义，格式非法 → 400；
    #         范围/必填问题返回错误文案（下方 flash 拦截）；定义外的键忽略 ──
    try:
        custom_values, custom_error = metrics.parse_custom_metrics(request.form)
    except ParseError as exc:
        abort(400, description=str(exc))

    # ── 3. 符号与范围校验（骨架字段 + 自定义指标，F14/P2）──
    # 骨架字段先于自定义指标（保持既有提示优先级），任一命中即拦截
    range_errors = [
        check_range(value, label, maximum) for value, label, maximum in (
            (sales_qty, qty_label, MAX_SALES_QTY),
            (total_revenue, amt_label, MAX_REVENUE),
            (material_remaining, stock_label, MAX_MATERIAL),
            (material_waste, waste_label, MAX_MATERIAL),
        )
    ]
    first_error = next((e for e in range_errors if e), custom_error)
    if first_error:
        flash(first_error, 'danger')
        return redirect(url_for('h5.dashboard'))

    # ── 4. 产品白名单：只接受 products 表中当前激活包的在售产品名（P7-B2）──
    # H60：同一次查询顺手把成本价取出（不加查询次数），落库时写成流水行的成本快照 ——
    # 毛利率此后不再回查目录当前值，改名/删品/改价都动不了历史。
    catalog_cost = {p.product_name: p.cost_price for p in Product.query.filter(
        Product.is_active.is_(True), active_pack_clause(Product.pack_id),
        tenant.store_scope_clause(Product.store_id)).all()}
    if product_name not in catalog_cost:
        flash('所选产品不在系统在售产品列表中，请从下拉框重新选择', 'danger')
        return redirect(url_for('h5.dashboard'))

    # 检查是否已提交过同一天同一产品（P7-C：按当前激活包判定，
    # 跨包同名产品不再互相拦截；默认包经 active_pack_clause 直通 NULL 历史行）
    existing = DailySales.query.filter(
        DailySales.staff_id == current_user.id,
        DailySales.record_date == record_date,
        DailySales.product_name == product_name,
        active_pack_clause(DailySales.pack_id),
        tenant.store_scope_clause(DailySales.store_id),
    ).first()

    if existing:
        flash(f'{product_name} 今天已经提交过了，如需修改请联系管理员', 'warning')
        return redirect(url_for('h5.dashboard'))

    _cost_now = catalog_cost.get(product_name)
    record = DailySales(
        staff_id=current_user.id,
        # PH2-2：门店维度落库（唯一入口 tenant.resolve_store_id）
        store_id=tenant.resolve_store_id(current_user),
        record_date=record_date,
        product_name=product_name,
        product_sales_qty=sales_qty,
        total_revenue=total_revenue,
        product_cost=_cost_now,
        cost_basis='snapshot' if _cost_now is not None else 'none',
        raw_material_remaining=(None if material_remaining is None
                                else f'{material_remaining:g}'),
        raw_material_waste=(None if material_waste is None
                            else f'{material_waste:g}'),
        abnormal_notes=notes or None,
        metrics_json=(json.dumps(custom_values, ensure_ascii=False)
                      if custom_values else None),
        pack_id=industry_pack.active_pack_id(),
    )
    db.session.add(record)
    try:
        db.session.commit()
    except IntegrityError:
        # 并发重复提交（TOCTOU）：唯一约束触发，回滚并给友好提示，避免 500（F3）
        db.session.rollback()
        flash(f'{product_name} 今天已经提交过了，如需修改请联系管理员', 'warning')
        return redirect(url_for('h5.dashboard'))

    # OPT-A Webhook：填报成功落库后触发外发（内部已吞异常且默认关闭，绝不阻断主链路）
    _emit_submit_webhook(current_user, record, product_name, sales_qty, total_revenue)

    flash(f'{product_name} 数据提交成功！', 'success')
    return redirect(url_for('h5.dashboard'))


@h5_bp.route('/history')
@login_required
@staff_required
def history():
    """历史填报记录（分页参数显式校验，非法 → 400 而非静默回退第 1 页）"""
    try:
        page = parse_int(request.args.get('page'), '页码', default=1)
    except ParseError as exc:
        abort(400, description=str(exc))
    if page < 1:
        abort(400, description='页码必须是大于 0 的整数')

    # P7-E3 / PF-5：只列当前激活包的流水（此前仅按 staff 过滤，切包后仍显示他包记录）
    pagination = DailySales.query.filter(
        DailySales.staff_id == current_user.id,
        active_pack_clause(DailySales.pack_id),
        tenant.store_scope_clause(DailySales.store_id),
    ).order_by(
        DailySales.record_date.desc(),
        DailySales.id.desc()
    ).paginate(page=page, per_page=20, error_out=False)

    # P7-E3-B2 / M-9：历史列表数量单位随包（此前恒显示「份」）
    return render_template('h5/history.html', pagination=pagination,
                           labels=metrics.dashboard_labels())


@h5_bp.route('/training')
@login_required
@staff_required
def training():
    """店员个人培训进度：通过率、分类统计、薄弱方向、错题集（AC-OBS-10/13 店员视角闭环）"""
    records = QuizRecord.query.filter_by(
        staff_id=current_user.id
    ).order_by(QuizRecord.created_at.desc()).all()

    total = len(records)
    correct = sum(1 for r in records if r.is_correct is True)
    pass_rate = (correct / total * 100) if total else 0

    # 分类通过率（问答 / 情景）
    by_type = {}
    for t in ('question', 'scenario'):
        t_recs = [r for r in records if r.quiz_type == t]
        t_total = len(t_recs)
        t_correct = sum(1 for r in t_recs if r.is_correct is True)
        by_type[t] = {
            'total': t_total,
            'correct': t_correct,
            'pass_rate': (t_correct / t_total * 100) if t_total else 0,
        }

    # 薄弱方向：有数据且通过率最低的类型
    candidates = {k: v for k, v in by_type.items() if v['total'] > 0}
    weak = min(candidates, key=lambda k: candidates[k]['pass_rate']) if candidates else None

    wrong_list = [r for r in records if r.is_correct is False][:10]

    # AI 补训建议：从近期错题题干关键词推断薄弱知识域 → 给出复习建议（知识库驱动）
    # 薄弱域归因共用 knowledge_base.weak_domain_counts（F18 口径唯一）
    wd_count = weak_domain_counts(r.question for r in wrong_list)
    suggestions = topic_suggestion(
        [d for d, _ in sorted(wd_count.items(), key=lambda x: -x[1])])
    # 薄弱点定向补训按钮所需：错题归因次数最高的知识域（无错题时 None）
    weak_domain = (sorted(wd_count.items(), key=lambda x: -x[1])[0][0]
                   if wd_count else None)

    # 学习者模型（第四轮 D1）：掌握度画像 + 间隔重复排期 + 自适应难度
    learner = learning_focus(records)

    return render_template(
        'h5/training.html',
        total=total,
        correct=correct,
        pass_rate=pass_rate,
        by_type=by_type,
        weak=weak,
        quiz_type_labels=QUIZ_TYPE_LABELS,
        wrong_list=wrong_list,
        suggestions=suggestions,
        weak_domain=weak_domain,
        mastery=learner['profile'],
        schedule=learner['schedule'],
        focus_domain=learner['focus_domain'],
        difficulty=learner['difficulty'],
        difficulty_label=DIFFICULTY_LABELS.get(learner['difficulty'], '标准'),
        learning_advice=learner['advice'],
    )


@h5_bp.route('/tasks')
@login_required
@staff_required
def tasks():
    """店员任务清单：只显示分配给本人的任务。"""
    rows = StoreTask.query.filter_by(
        assignee_staff_id=current_user.id
    ).order_by(StoreTask.due_date.asc(), StoreTask.id.desc()).all()
    return render_template(
        'h5/tasks.html',
        tasks=[task_flow.task_view(row, today=cal.business_today()) for row in rows],
        status_labels=task_flow.STATUS_LABELS,
        task_flow=task_flow,
        today=cal.business_today(),
    )


@h5_bp.route('/tasks/<int:task_id>/status', methods=['POST'])
@login_required
@staff_required
@rate_limit(action='submit')
def task_status(task_id):
    """店员更新本人任务状态，不能操作他人的任务。"""
    task = StoreTask.query.filter_by(
        id=task_id, assignee_staff_id=current_user.id).first()
    if task is None:
        abort(404, description='任务不存在或无权操作。')
    try:
        task_flow.transition_task(task, request.form.get('status'))
    except ValueError as exc:
        flash(str(exc), 'danger')
    else:
        flash(f'任务状态已更新为「{task_flow.status_label(task.status)}」', 'success')
    return redirect(url_for('h5.tasks'))
