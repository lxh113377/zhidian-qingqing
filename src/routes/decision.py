"""管理员 AI 决策驾驶舱接口。"""
from datetime import date, timedelta

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from src import decision_cockpit, task_flow, tenant
from src.decorators import role_required
from src.models import ROLE_MANAGER, ROLE_OWNER
from src.rate_limit import rate_limit_post
from src import business_calendar as cal

decision_bp = Blueprint('decision', __name__, url_prefix='/admin/decision')


def _visible_ids():
    return tenant.allowed_store_ids(current_user)


def _task_context(store_ids):
    return {
        'task_stores': task_flow.store_options(store_ids),
        'task_assignees': task_flow.assignee_options(
            getattr(current_user, 'merchant_id', None), store_ids),
        'default_due_date': (cal.business_today() + timedelta(days=3)).isoformat(),
    }


@decision_bp.route('/run', methods=['POST'])
@login_required
@role_required(ROLE_OWNER, ROLE_MANAGER)
@rate_limit_post('admin_write')
def run():
    """生成一次决策驾驶舱；手动刷新显式绕过缓存。"""
    store_ids = _visible_ids()
    try:
        days = int(request.form.get('days') or decision_cockpit.DECISION_DAYS)
    except (TypeError, ValueError):
        days = decision_cockpit.DECISION_DAYS
    if days not in decision_cockpit.DECISION_RANGE_CHOICES:
        days = decision_cockpit.DECISION_DAYS
    result = decision_cockpit.run_decision(
        current_user.merchant_id,
        store_ids,
        days=days,
        refresh=request.form.get('refresh') == '1',
        allow_ai=True,
        store_id=tenant.current_store_id(),
    )
    fragment = render_template(
        'admin/_decision_cockpit_inner.html',
        decision=result,
        decision_days=days,
        **_task_context(store_ids),
    )
    return jsonify(ok=True, result=result, fragment=fragment)


__all__ = ['decision_bp']
