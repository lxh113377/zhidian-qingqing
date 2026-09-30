"""门店任务闭环（PH4-2）：创建、分配、状态流转、逾期与效果回看。"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from src import business_calendar as cal

from src.models import (
    ROLE_MANAGER,
    ROLE_OWNER,
    ROLE_STAFF,
    Admin,
    AdminStoreAccess,
    Staff,
    Store,
    StoreTask,
    db,
)

STATUS_PENDING = 'pending'
STATUS_IN_PROGRESS = 'in_progress'
STATUS_DONE = 'done'
STATUS_CANCELLED = 'cancelled'

SOURCE_MANUAL = 'manual'
SOURCE_AI = 'ai_analysis'
SOURCE_AI_DECISION = 'ai_decision'

ALLOWED_SOURCES = frozenset({SOURCE_MANUAL, SOURCE_AI, SOURCE_AI_DECISION})

STATUS_LABELS = {
    STATUS_PENDING: '待处理',
    STATUS_IN_PROGRESS: '进行中',
    STATUS_DONE: '已完成',
    STATUS_CANCELLED: '已取消',
}

ROLE_LABELS = {
    ROLE_OWNER: '老板',
    ROLE_MANAGER: '店长',
    ROLE_STAFF: '店员',
}

TERMINAL_STATUSES = {STATUS_DONE, STATUS_CANCELLED}
ACTIVE_STATUSES = {STATUS_PENDING, STATUS_IN_PROGRESS}

TRANSITIONS = {
    STATUS_PENDING: {STATUS_IN_PROGRESS, STATUS_CANCELLED},
    STATUS_IN_PROGRESS: {STATUS_DONE, STATUS_CANCELLED},
    STATUS_DONE: {STATUS_IN_PROGRESS},
    STATUS_CANCELLED: set(),
}


def status_label(status):
    """返回状态中文名；未知状态原样返回，避免模板渲染失败。"""
    return STATUS_LABELS.get(status, status or '')


def is_overdue(task, today=None):
    """任务是否逾期：终态不计，截止日早于今天即逾期。"""
    if task is None or task.status not in ACTIVE_STATUSES:
        return False
    return task.due_date < (today or cal.business_today())


def can_transition(current, new):
    """状态机是否允许从 current 流转到 new。"""
    return new in TRANSITIONS.get(current, set())


def parse_due_date(raw):
    """解析 ISO 日期；空值/非法值拒绝，避免静默写成今天。"""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw or '').strip())
    except ValueError as exc:
        raise ValueError('截止时间必须是 YYYY-MM-DD 格式') from exc


def resolve_assignee(raw):
    """解析表单值 `admin:<id>` / `staff:<id>`，返回 (role, id)。"""
    text = str(raw or '').strip()
    if ':' not in text:
        raise ValueError('请选择负责人')
    prefix, raw_id = text.split(':', 1)
    try:
        assignee_id = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise ValueError('负责人参数不合法') from exc
    if prefix == 'admin':
        admin = db.session.get(Admin, assignee_id)
        if admin is None:
            raise ValueError('负责人不存在')
        return ((admin.role or ROLE_OWNER), admin.id)
    if prefix == 'staff':
        return ROLE_STAFF, assignee_id
    raise ValueError('负责人类型不合法')


def _json_text(value):
    if value in (None, '', []):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return json.dumps({'note': value}, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _validate_assignee(merchant_id, store_id, role, assignee_id):
    """校验负责人属于该商家，并且对该门店有实际责任范围。"""
    if role in (ROLE_OWNER, ROLE_MANAGER):
        admin = db.session.get(Admin, assignee_id)
        if (admin is None or admin.merchant_id != merchant_id
                or (admin.role or ROLE_OWNER) != role):
            raise ValueError('负责人不存在或不属于当前商家')
        if role == ROLE_MANAGER:
            allowed = AdminStoreAccess.query.filter_by(
                admin_id=admin.id, store_id=store_id).first()
            if allowed is None:
                raise ValueError('店长未被授权负责该门店')
        return {'assignee_admin_id': admin.id, 'assignee_staff_id': None}
    if role == ROLE_STAFF:
        staff = db.session.get(Staff, assignee_id)
        if (staff is None or staff.merchant_id != merchant_id
                or staff.store_id != store_id or not staff.is_active):
            raise ValueError('店员不存在、已停用或不属于该门店')
        return {'assignee_admin_id': None, 'assignee_staff_id': staff.id}
    raise ValueError('负责人角色不合法')


def create_task(*, merchant_id, store_id, title, due_date, assignee_role,  # noqa: PLR0913
                assignee_id, detail=None, source=SOURCE_MANUAL, evidence=None,
                created_by_admin_id=None):
    """创建任务，返回 StoreTask；任何权限/归属不匹配直接拒绝。"""
    store = Store.query.filter_by(id=store_id, merchant_id=merchant_id).first()
    if store is None:
        raise ValueError('门店不存在或不属于当前商家')
    clean_title = str(title or '').strip()
    if not clean_title:
        raise ValueError('任务标题不能为空')
    if len(clean_title) > 120:
        raise ValueError('任务标题不能超过 120 个字符')
    due = parse_due_date(due_date)
    if due < cal.business_today():
        raise ValueError('截止时间不能早于今天')
    scope = _validate_assignee(merchant_id, store_id, assignee_role, assignee_id)
    clean_source = (source or SOURCE_MANUAL)
    if clean_source not in ALLOWED_SOURCES:
        raise ValueError('任务来源不合法')
    task = StoreTask(
        merchant_id=merchant_id,
        store_id=store_id,
        title=clean_title,
        detail=(str(detail).strip() if detail else None),
        source=clean_source,
        evidence_json=_json_text(evidence),
        assignee_role=assignee_role,
        due_date=due,
        status=STATUS_PENDING,
        created_by_admin_id=created_by_admin_id,
        **scope,
    )
    db.session.add(task)
    db.session.commit()
    return task


def store_options(store_ids):
    """任务表单用门店选项。"""
    if not store_ids:
        return []
    rows = Store.query.filter(Store.id.in_(list(store_ids))).order_by(Store.id).all()
    return [{'id': s.id, 'name': s.name} for s in rows]


def assignee_options(merchant_id, store_ids):
    """返回可分配负责人；服务端创建时仍会再次校验门店范围。"""
    if not merchant_id or not store_ids:
        return []
    ids = {int(s) for s in store_ids}
    options = []
    admins = Admin.query.filter(
        Admin.merchant_id == merchant_id,
        Admin.role.in_([ROLE_OWNER, ROLE_MANAGER]),
    ).order_by(Admin.id).all()
    for admin in admins:
        role = admin.role or ROLE_OWNER
        if role == ROLE_MANAGER:
            allowed = {r.store_id for r in AdminStoreAccess.query.filter_by(
                admin_id=admin.id).all()}
            if not (allowed & ids):
                continue
        options.append({
            'value': f'admin:{admin.id}',
            'label': f'{ROLE_LABELS.get(role, role)} · {admin.username}',
            'role': role,
        })
    staff = Staff.query.filter(
        Staff.merchant_id == merchant_id,
        Staff.store_id.in_(list(ids)),
        Staff.is_active.is_(True),
    ).order_by(Staff.id).all()
    for row in staff:
        options.append({
            'value': f'staff:{row.id}',
            'label': f'店员 · {row.display_name}（{row.store_name}）',
            'role': ROLE_STAFF,
        })
    return options


def list_tasks(store_ids, status=None, overdue=False, today=None):
    """按可见门店列任务，支持状态和逾期过滤。"""
    ids = list(store_ids or [])
    if not ids:
        return []
    query = StoreTask.query.filter(StoreTask.store_id.in_(ids))
    if status in STATUS_LABELS:
        query = query.filter(StoreTask.status == status)
    if overdue:
        query = query.filter(
            StoreTask.status.in_(list(ACTIVE_STATUSES)),
            StoreTask.due_date < (today or cal.business_today()),
        )
    return query.order_by(StoreTask.due_date.asc(), StoreTask.id.desc()).all()


def task_stats(store_ids, today=None):
    """任务概览：未完成 / 逾期 / 已完成。"""
    rows = list_tasks(store_ids, today=today)
    active = [t for t in rows if t.status in ACTIVE_STATUSES]
    return {
        'total': len(rows),
        'active': len(active),
        'overdue': len([t for t in active if is_overdue(t, today)]),
        'done': len([t for t in rows if t.status == STATUS_DONE]),
    }


def assignee_label(task):
    """任务负责人展示名。"""
    if task is None:
        return ''
    if task.assignee_admin_id:
        admin = db.session.get(Admin, task.assignee_admin_id)
        if admin is not None:
            role = admin.role or ROLE_OWNER
            return f'{ROLE_LABELS.get(role, role)} · {admin.username}'
    if task.assignee_staff_id:
        staff = db.session.get(Staff, task.assignee_staff_id)
        if staff is not None:
            return f'店员 · {staff.display_name}'
    return '未分配'


def task_view(task, today=None):
    """模板用任务摘要，避免模板层写查询逻辑。"""
    store = db.session.get(Store, task.store_id)
    return {
        'task': task,
        'store_name': store.name if store else f'门店{task.store_id}',
        'assignee': assignee_label(task),
        'overdue': is_overdue(task, today),
        'status_label': status_label(task.status),
    }


def transition_task(task, new_status):
    """执行状态流转；非法跃迁抛 ValueError。"""
    if task is None:
        raise ValueError('任务不存在')
    if new_status not in STATUS_LABELS:
        raise ValueError('任务状态不合法')
    if not can_transition(task.status, new_status):
        raise ValueError(
            f'任务不能从「{status_label(task.status)}」直接变为'
            f'「{status_label(new_status)}」')
    task.status = new_status
    if new_status == STATUS_DONE:
        task.completed_at = datetime.now(UTC).replace(tzinfo=None)
    elif task.status == STATUS_IN_PROGRESS:
        task.completed_at = None
    db.session.commit()
    # OPT-A Webhook：任务状态变更落库后触发外发（内部吞异常且默认关闭，绝不阻断主链路）
    if getattr(task, 'merchant_id', None):
        from src import api_open
        api_open.notify_hook('task.status_changed', task.merchant_id, {
            'task_id': task.id, 'store_id': task.store_id,
            'status': task.status, 'title': task.title})
    return task


TAKEOVER_REASON_MAX = 200


def _assignee_key(task):
    """负责人身份键：('admin'|'staff', id)；未分配返回 (None, None)。"""
    if task.assignee_admin_id:
        return ('admin', task.assignee_admin_id)
    if task.assignee_staff_id:
        return ('staff', task.assignee_staff_id)
    return (None, None)


def is_takeover_eligible(task, today=None):
    """停滞定义：ACTIVE 状态且已逾期。终态/未逾期一律不可接管（无新状态，不动状态机）。"""
    if task is None or task.status not in ACTIVE_STATUSES:
        return False
    return is_overdue(task, today)


def takeover_task(task, *, claimant_role, claimant_id, expected=None,  # noqa: PLR0913
                  reason=None, actor_admin_id=None):
    """遗留任务接管：只换负责人，不动状态（上下文 detail/evidence/review 全保留）。

    接管条件：任务 ACTIVE 且已逾期（停滞）；终态与未逾期拒绝。
    权限：claimant 须为同商家老板/店长（店长限其授权门店），且不能是当前负责人。
    防重领：expected 为接管发起时看到的当前负责人键，对不上即有人先手 → 拒绝
    （"任务已被他人接管，请刷新后重试"）。
    记录：行上换归属 + takeover_json 记 {from,to,by,at,reason} 当次交接；
    历次交接靠 change_log 对 takeover_json/assignee 列的 auto-diff 回溯。
    """
    if task is None:
        raise ValueError('任务不存在')
    if task.status in TERMINAL_STATUSES:
        raise ValueError('已完结任务不可接管')
    if not is_takeover_eligible(task):
        raise ValueError('仅逾期停滞任务可被接管')
    if claimant_role not in (ROLE_OWNER, ROLE_MANAGER):
        raise ValueError('仅老板/店长可接管任务')
    # 身份键统一用存储形态 ('admin'|'staff', id)：老板/店长同属 admin 表，
    # 若拿 resolve 的 ('manager', id) 与 ('admin', id) 比对，自领永远判不出。
    current = _assignee_key(task)
    claimant = ('admin', claimant_id)
    if claimant == current:
        raise ValueError('不能接管自己负责的任务')
    if expected is not None and tuple(expected) != tuple(current):
        raise ValueError('任务已被他人接管，请刷新后重试')
    clean_reason = str(reason or '').strip()
    if not clean_reason:
        raise ValueError('接管必须注明原因')
    if len(clean_reason) > TAKEOVER_REASON_MAX:
        raise ValueError(f'接管原因不能超过 {TAKEOVER_REASON_MAX} 字')
    scope = _validate_assignee(task.merchant_id, task.store_id, claimant_role, claimant_id)
    task.assignee_role = claimant_role
    task.assignee_admin_id = scope['assignee_admin_id']
    task.assignee_staff_id = scope['assignee_staff_id']
    task.takeover_json = json.dumps({
        'from': {'kind': current[0], 'id': current[1]},
        'to': {'kind': 'admin', 'id': claimant_id, 'role': claimant_role},
        'by_admin_id': actor_admin_id,
        'at': datetime.now(UTC).replace(tzinfo=None).isoformat(timespec='seconds'),
        'reason': clean_reason,
    }, ensure_ascii=False, sort_keys=True)
    db.session.commit()
    return task


def save_review(task, note, before=None, after=None):
    """保存效果回看；只允许已完成任务回看，不把完成当因果证明。"""
    if task is None:
        raise ValueError('任务不存在')
    if task.status != STATUS_DONE:
        raise ValueError('只有已完成任务可以填写效果回看')
    clean_note = str(note or '').strip()
    if not clean_note:
        raise ValueError('效果回看备注不能为空')
    task.review_note = clean_note
    task.before_json = _json_text(before)
    task.after_json = _json_text(after)
    db.session.commit()
    return task


__all__ = [
    'ACTIVE_STATUSES',
    'ROLE_LABELS',
    'SOURCE_AI',
    'SOURCE_AI_DECISION',
    'SOURCE_MANUAL',
    'STATUS_CANCELLED',
    'STATUS_DONE',
    'STATUS_IN_PROGRESS',
    'STATUS_LABELS',
    'STATUS_PENDING',
    'TAKEOVER_REASON_MAX',
    'TERMINAL_STATUSES',
    'TRANSITIONS',
    'assignee_label',
    'assignee_options',
    'can_transition',
    'create_task',
    'is_overdue',
    'is_takeover_eligible',
    'list_tasks',
    'parse_due_date',
    'resolve_assignee',
    'save_review',
    'status_label',
    'store_options',
    'takeover_task',
    'task_stats',
    'task_view',
    'transition_task',
]
