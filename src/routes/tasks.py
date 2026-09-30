"""门店任务闭环路由（PH4-2）。"""
from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from src import task_flow, tenant
from src.decorators import role_required
from src.models import ROLE_MANAGER, ROLE_OWNER, StoreTask, db
from src.rate_limit import rate_limit_post
from src import business_calendar as cal

tasks_bp = Blueprint('tasks', __name__, url_prefix='/admin/tasks')


def _visible_ids():
    return tenant.allowed_store_ids(current_user)


def _visible_task_or_404(task_id):
    task = db.session.get(StoreTask, task_id)
    if task is None or task.store_id not in _visible_ids():
        abort(404, description='任务不存在或无权查看。')
    return task


@tasks_bp.route('/')
@login_required
@role_required(ROLE_OWNER, ROLE_MANAGER)
def task_list():
    """任务列表：支持状态与逾期筛选。"""
    status = request.args.get('status') or None
    overdue = request.args.get('overdue') == '1'
    raw_rows = task_flow.list_tasks(_visible_ids(), status=status, overdue=overdue)
    rows = [task_flow.task_view(t) for t in raw_rows]
    return render_template(
        'admin/tasks.html',
        tasks=rows,
        stats=task_flow.task_stats(_visible_ids()),
        status=status,
        overdue=overdue,
        status_labels=task_flow.STATUS_LABELS,
        store_options=task_flow.store_options(_visible_ids()),
        assignee_options=task_flow.assignee_options(
            getattr(current_user, 'merchant_id', None), _visible_ids()),
        default_due_date=(cal.business_today() + timedelta(days=3)).isoformat(),
    )


@tasks_bp.route('/create', methods=['POST'])
@login_required
@role_required(ROLE_OWNER, ROLE_MANAGER)
@rate_limit_post('admin_write')
def task_create():
    """从 AI 结论或人工表单创建任务。"""
    try:
        store_id = int(request.form.get('store_id') or 0)
    except (TypeError, ValueError):
        abort(400, description='门店参数不合法。')
    if store_id not in _visible_ids():
        abort(404, description='门店不存在或无权操作。')
    try:
        role, assignee_id = task_flow.resolve_assignee(request.form.get('assignee'))
        raw_source = request.form.get('source') or task_flow.SOURCE_MANUAL
        if raw_source not in task_flow.ALLOWED_SOURCES:
            flash('任务来源不合法', 'danger')
            return redirect(request.form.get('next') or url_for('tasks.task_list'))
        task = task_flow.create_task(
            merchant_id=current_user.merchant_id,
            store_id=store_id,
            title=request.form.get('title'),
            detail=request.form.get('detail'),
            due_date=request.form.get('due_date'),
            assignee_role=role,
            assignee_id=assignee_id,
            source=raw_source,
            evidence=request.form.get('evidence_json'),
            created_by_admin_id=current_user.id,
        )
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(request.form.get('next') or url_for('tasks.task_list'))
    flash(f'任务已创建：{task.title}', 'success')
    return redirect(url_for('tasks.task_detail', task_id=task.id))


@tasks_bp.route('/<int:task_id>')
@login_required
@role_required(ROLE_OWNER, ROLE_MANAGER)
def task_detail(task_id):
    """任务详情：状态流转与效果回看。"""
    task = _visible_task_or_404(task_id)
    if task.assignee_admin_id:
        current_key = f'admin:{task.assignee_admin_id}'
    elif task.assignee_staff_id:
        current_key = f'staff:{task.assignee_staff_id}'
    else:
        current_key = ''
    return render_template(
        'admin/task_detail.html',
        task=task,
        store_name=task_flow.task_view(task)['store_name'],
        assignee=task_flow.assignee_label(task),
        overdue=task_flow.is_overdue(task),
        status_labels=task_flow.STATUS_LABELS,
        transitions=task_flow.TRANSITIONS.get(task.status, set()),
        takeover_eligible=task_flow.is_takeover_eligible(task),
        current_key=current_key,
        assignee_options=task_flow.assignee_options(
            getattr(current_user, 'merchant_id', None), [task.store_id]),
    )


@tasks_bp.route('/<int:task_id>/status', methods=['POST'])
@login_required
@role_required(ROLE_OWNER, ROLE_MANAGER)
@rate_limit_post('admin_write')
def task_status(task_id):
    """状态流转；非法流转由状态机拒绝。"""
    task = _visible_task_or_404(task_id)
    try:
        task_flow.transition_task(task, request.form.get('status'))
    except ValueError as exc:
        flash(str(exc), 'danger')
    else:
        flash(f'任务状态已更新为「{task_flow.status_label(task.status)}」', 'success')
    return redirect(url_for('tasks.task_detail', task_id=task.id))


@tasks_bp.route('/<int:task_id>/review', methods=['POST'])
@login_required
@role_required(ROLE_OWNER, ROLE_MANAGER)
@rate_limit_post('admin_write')
def task_review(task_id):
    """效果回看：记录观察值和备注，不宣称因果。"""
    task = _visible_task_or_404(task_id)
    before = {}
    after = {}
    if request.form.get('before_metric'):
        before = {
            'metric': request.form.get('before_metric'),
            'value': request.form.get('before_value'),
        }
    if request.form.get('after_metric'):
        after = {
            'metric': request.form.get('after_metric'),
            'value': request.form.get('after_value'),
        }
    try:
        task_flow.save_review(
            task,
            request.form.get('review_note'),
            before=before or None,
            after=after or None,
        )
    except ValueError as exc:
        flash(str(exc), 'danger')
    else:
        flash('效果回看已保存', 'success')
    return redirect(url_for('tasks.task_detail', task_id=task.id))


@tasks_bp.route('/<int:task_id>/takeover', methods=['POST'])
@login_required
@role_required(ROLE_OWNER, ROLE_MANAGER)
@rate_limit_post('admin_write')
def task_takeover(task_id):
    """遗留任务接管：仅逾期停滞任务可被他人接走；expected 防多人重复认领。"""
    task = _visible_task_or_404(task_id)
    try:
        role, assignee_id = task_flow.resolve_assignee(request.form.get('assignee'))
        raw_expected = (request.form.get('expected') or '').strip()
        expected = None
        if raw_expected and ':' in raw_expected:
            prefix, raw_id = raw_expected.split(':', 1)
            if prefix in ('admin', 'staff') and raw_id.strip().isdigit():
                expected = (prefix, int(raw_id.strip()))
        task_flow.takeover_task(
            task,
            claimant_role=role,
            claimant_id=assignee_id,
            expected=expected,
            reason=request.form.get('reason'),
            actor_admin_id=current_user.id,
        )
    except ValueError as exc:
        flash(str(exc), 'danger')
    else:
        flash(f'任务已交接给{task_flow.assignee_label(task)}，原上下文完整保留', 'success')
    return redirect(url_for('tasks.task_detail', task_id=task.id))


__all__ = ['tasks_bp']
