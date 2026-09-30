"""多门店经营大脑（PH2-5C）：老板/店长的多店视图、下钻与跨店 AI 分析。

权限：老板看本商家全部门店；店长只看被授权门店（`tenant.allowed_store_ids`）；
店员与平台管理员 403。下钻越界门店一律 404（不泄露门店是否存在）。
"""
from datetime import date, timedelta

from flask import Blueprint, abort, render_template, request
from flask_login import current_user, login_required

from src import ai_quota, multi_store, task_flow, tenant
from src.decorators import role_required
from src.models import ROLE_MANAGER, ROLE_OWNER
from src.rate_limit import rate_limit_post
from src import business_calendar as cal

multi_bp = Blueprint('multi', __name__, url_prefix='/admin/multi')

DEFAULT_DAYS = multi_store.DEFAULT_DAYS


def _visible_ids():
    return tenant.allowed_store_ids(current_user)


def _visible_or_404(store_id):
    if store_id not in _visible_ids():
        abort(404, description='门店不存在或无权查看。')
    return store_id


def _task_context(store_ids):
    """跨店分析页共用的任务表单选项。"""
    return {
        'task_stores': task_flow.store_options(store_ids),
        'task_assignees': task_flow.assignee_options(
            getattr(current_user, 'merchant_id', None), store_ids),
        'default_due_date': (cal.business_today() + timedelta(days=3)).isoformat(),
    }


@multi_bp.route('/')
@login_required
@role_required(ROLE_OWNER, ROLE_MANAGER)
def multi_dashboard():
    """多店视图：汇总 / 排行 / 趋势 / 异常门店提示。"""
    ids = _visible_ids()
    days = DEFAULT_DAYS
    # P2：一次聚合供排行与异常巡检共用，单请求不再重复查询
    metrics = multi_store.store_metrics(ids, days=days)
    ranking = multi_store.store_ranking(ids, days=days, metrics=metrics)
    trend = multi_store.cross_store_trend(ids, days=days)
    alerts = multi_store.abnormal_stores(ids, days=days, metrics=metrics)
    summary = {
        'store_count': len(ids),
        'active_count': len([r for r in ranking if r['has_data']]),
        'revenue': round(sum(r['revenue'] for r in ranking), 2),
        'records': sum(r['records'] for r in ranking),
    }
    quota = ai_quota.usage_today(current_user.merchant_id, ai_quota.KIND_ANALYSIS)
    return render_template('admin/multi_dashboard.html', ranking=ranking,
                           trend=trend, alerts=alerts, summary=summary, days=days,
                           analysis=None,
                           **_task_context(ids),
                           quota={'used': quota['calls'],
                                  'limit': ai_quota.ANALYSIS_DAILY_LIMIT,
                                  'tokens': quota['tokens'],
                                  'token_limit': ai_quota.TOKEN_DAILY_LIMIT})


@multi_bp.route('/analyze', methods=['POST'])
@login_required
@role_required(ROLE_OWNER, ROLE_MANAGER)
@rate_limit_post('admin_write')
def multi_analyze():
    """跨店 AI 分析（配额/缓存/超时/降级由 ai_quota 统一处置）。"""
    ids = _visible_ids()
    days = DEFAULT_DAYS
    analysis = multi_store.analyze_cross_store(
        current_user.merchant_id, ids, days=days,
        store_id=tenant.current_store_id())
    ranking = analysis['ranking']
    summary = {
        'store_count': len(ids),
        'active_count': len([r for r in ranking if r['has_data']]),
        'revenue': round(sum(r['revenue'] for r in ranking), 2),
        'records': sum(r['records'] for r in ranking),
    }
    quota = ai_quota.usage_today(current_user.merchant_id, ai_quota.KIND_ANALYSIS)
    return render_template('admin/multi_dashboard.html', ranking=ranking,
                           trend=multi_store.cross_store_trend(ids, days=days),
                           alerts=analysis['alerts'], summary=summary, days=days,
                           analysis=analysis,
                           **_task_context(ids),
                           quota={'used': quota['calls'],
                                  'limit': ai_quota.ANALYSIS_DAILY_LIMIT,
                                  'tokens': quota['tokens'],
                                  'token_limit': ai_quota.TOKEN_DAILY_LIMIT})


@multi_bp.route('/store/<int:store_id>')
@login_required
@role_required(ROLE_OWNER, ROLE_MANAGER)
def multi_store_detail(store_id):
    """单店下钻（越界 404）。"""
    _visible_or_404(store_id)
    detail = multi_store.drill_down(store_id, days=DEFAULT_DAYS)
    if detail is None:
        abort(404, description='门店不存在。')
    return render_template('admin/multi_store.html', detail=detail,
                           days=DEFAULT_DAYS,
                           requested_store=request.args.get('name') or '')


__all__ = ['DEFAULT_DAYS', 'multi_bp']
