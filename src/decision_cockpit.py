"""AI 决策驾驶舱：确定性分析、双角色隔离推理、证据仲裁与人工确认前置。"""
from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4

from src import ai_quota, diag, multi_store
from src import business_calendar as cal

DECISION_DAYS = 30
DECISION_RANGE_CHOICES = (7, 30, 90)

ROLE_DATA_ANALYST = 'data_analyst'
ROLE_ADVISOR = 'advisor'
ROLE_AUDITOR = 'auditor'
ROLE_MODERATOR = 'moderator'

ROLE_LABELS = {
    ROLE_DATA_ANALYST: '数据分析师',
    ROLE_ADVISOR: '经营参谋',
    ROLE_AUDITOR: '风险审计员',
    ROLE_MODERATOR: '规则主持人',
}

DECISION_TRACE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'decision_trace.jsonl')
DECISION_TRACE_MAX_BYTES = 1024 * 1024
DECISION_TRACE_BACKUPS = 3
_TRACE_LOCK = threading.Lock()
logger = logging.getLogger(__name__)

# 测试注入口：默认 None，生产使用 DifyClient.fork() 的两个隔离实例。
_LLM = {'fn': None}


def set_llm(fn):
    """注入测试用调用器：`fn(prompt, role) -> str | dict | None`。"""
    _LLM['fn'] = fn


def _now_text():
    return cal.business_now().strftime('%Y-%m-%d %H:%M:%S')


def _clean_text(value, limit=240):
    text = ' '.join(str(value or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _claim(text, evidence_ids=None, kind='observation'):
    return {
        'kind': str(kind or 'observation'),
        'text': _clean_text(text),
        'evidence_ids': list(evidence_ids or []),
    }


def _add_evidence(catalog, lookup, store_id, metric, value, period,  # noqa: PLR0913, PLR0917
                  allowed):
    try:
        sid = int(store_id)
    except (TypeError, ValueError):
        return None
    if sid not in allowed:
        return None
    key = (sid, str(metric))
    if key in lookup:
        return lookup[key]
    evidence_id = f'ev-{len(catalog) + 1}'
    lookup[key] = evidence_id
    catalog.append({
        'id': evidence_id,
        'store_id': sid,
        'metric': str(metric),
        'value': value,
        'period': str(period),
    })
    return evidence_id


def build_snapshot(store_ids, days=DECISION_DAYS, today=None):
    """在主线程构建租户隔离快照，返回四角色共享的证据目录。"""
    allowed = {int(s) for s in store_ids or []}
    safe_ids = sorted(allowed)
    # P2：排行与异常巡检共享一次聚合（主线程构建，四角色共享证据目录）
    _metrics = multi_store.store_metrics(safe_ids, days=days, today=today)
    ranking = multi_store.store_ranking(safe_ids, days=days, today=today, metrics=_metrics)
    alerts = multi_store.abnormal_stores(safe_ids, days=days, today=today, metrics=_metrics)
    catalog = []
    lookup = {}
    period = f'近{days}天'
    for row in ranking:
        sid = row.get('store_id')
        _add_evidence(catalog, lookup, sid, 'revenue', row.get('revenue'), period, allowed)
        _add_evidence(catalog, lookup, sid, 'records', row.get('records'), period, allowed)
        if row.get('waste_rate') is not None:
            _add_evidence(catalog, lookup, sid, 'waste_rate',
                          row.get('waste_rate'), period, allowed)
        if row.get('quiz_avg') is not None:
            _add_evidence(catalog, lookup, sid, 'quiz_avg',
                          row.get('quiz_avg'), period, allowed)
    for alert in alerts:
        evidence = (alert.get('evidence') or [{}])[0]
        _add_evidence(catalog, lookup, alert.get('store_id'),
                      evidence.get('metric') or alert.get('kind'),
                      evidence.get('value', alert.get('value')),
                      period, allowed)
    return {
        'store_ids': safe_ids,
        'days': int(days),
        'ranking': ranking,
        'alerts': alerts,
        'evidence': catalog,
    }


def _evidence_id(snapshot, store_id, metric):
    for item in snapshot.get('evidence') or []:
        if item.get('store_id') == int(store_id) and item.get('metric') == metric:
            return item.get('id')
    return None


def _analyst_result(snapshot):
    rows = snapshot.get('ranking') or []
    active = [row for row in rows if row.get('has_data')]
    claims = []
    if not active:
        claims.append(_claim('当前授权门店在统计区间内没有有效营业记录。', kind='no_data'))
        summary = '暂无可分析的有效经营数据。'
    else:
        top = active[0]
        top_ev = _evidence_id(snapshot, top['store_id'], 'revenue')
        claims.append(_claim(
            f'营收最高门店为「{top["store_name"]}」，本期营收 {top["revenue"]:.2f} 元。',
            [top_ev] if top_ev else [], 'ranking'))
        low = [row for row in active
               if row['revenue'] < max(top['revenue'] * 0.6, 1e-9)]
        for row in low[:2]:
            ev_id = _evidence_id(snapshot, row['store_id'], 'revenue')
            claims.append(_claim(
                f'「{row["store_name"]}」营收仅为最高门店的 '
                f'{row["revenue"] / max(top["revenue"], 1e-9):.0%}。',
                [ev_id] if ev_id else [], 'gap'))
        for alert in (snapshot.get('alerts') or [])[:4]:
            sid = alert.get('store_id')
            if alert.get('kind') == 'waste_rate':
                ev_id = _evidence_id(snapshot, sid, 'waste_rate')
                text = (f'「{alert["store_name"]}」损耗率 {alert["value"]:.1%}，'
                        f'高于阈值 {alert["threshold"]:.0%}。')
            else:
                ev_id = _evidence_id(snapshot, sid, 'revenue_drop_3d')
                text = (f'「{alert["store_name"]}」近 3 日营收下滑 '
                        f'{alert["value"]:.0%}。')
            claims.append(_claim(text, [ev_id] if ev_id else [], 'risk'))
        training = [row for row in active
                    if row.get('quiz_avg') is not None and row['quiz_avg'] < 80]
        for row in training[:2]:
            ev_id = _evidence_id(snapshot, row['store_id'], 'quiz_avg')
            claims.append(_claim(
                f'「{row["store_name"]}」培训均分 {row["quiz_avg"]:.1f}，'
                '建议安排薄弱知识域复训。',
                [ev_id] if ev_id else [], 'training'))
        summary = f'覆盖 {len(active)} 家有效门店，识别 {len(claims)} 条分析结论。'
    return _role_result(ROLE_DATA_ANALYST, 'rule', summary, claims)


def _role_result(role, source, summary, items, *, status='ok', error=None,  # noqa: PLR0913
                 discarded_evidence=0, tokens=0):
    key = {
        ROLE_DATA_ANALYST: 'claims',
        ROLE_ADVISOR: 'proposals',
        ROLE_AUDITOR: 'risks',
        ROLE_MODERATOR: 'claims',
    }[role]
    return {
        'role': role,
        'label': ROLE_LABELS[role],
        'source': source,
        'status': status,
        'summary': _clean_text(summary),
        key: list(items or []),
        'error': error,
        'discarded_evidence': int(discarded_evidence or 0),
        'tokens': int(tokens or 0),
    }


def _advisor_fallback(snapshot):
    proposals = []
    for alert in (snapshot.get('alerts') or [])[:3]:
        sid = alert.get('store_id')
        if alert.get('kind') == 'waste_rate':
            ev_id = _evidence_id(snapshot, sid, 'waste_rate')
            proposals.append({
                'action': f'复盘「{alert["store_name"]}」备货和储存',
                'rationale': '损耗率超过行业包阈值，需要先核查备货量与储存条件。',
                'evidence_ids': [ev_id] if ev_id else [],
                'priority': 'high',
            })
        else:
            ev_id = _evidence_id(snapshot, sid, 'revenue_drop_3d')
            proposals.append({
                'action': f'核查「{alert["store_name"]}」客流变化',
                'rationale': '近三日营收明显下滑，建议复核排班、竞品和缺货情况。',
                'evidence_ids': [ev_id] if ev_id else [],
                'priority': 'high',
            })
    if not proposals:
        active = [row for row in snapshot.get('ranking') or [] if row.get('has_data')]
        if active:
            sid = active[0].get('store_id')
            ev_id = _evidence_id(snapshot, sid, 'revenue')
            proposals.append({
                'action': '保持当前经营监控节奏',
                'rationale': '当前未识别到超过规则阈值的经营异常。',
                'evidence_ids': [ev_id] if ev_id else [],
                'priority': 'medium',
            })
        else:
            proposals.append({
                'action': '补齐门店营业数据后复核',
                'rationale': '当前没有可用经营记录，无法形成可靠经营建议。',
                'evidence_ids': [],
                'priority': 'low',
            })
    return _role_result(
        ROLE_ADVISOR, 'fallback', f'规则版形成 {len(proposals)} 条建议。', proposals,
        status='fallback')


def _auditor_fallback(snapshot):
    risks = []
    for alert in (snapshot.get('alerts') or [])[:4]:
        sid = alert.get('store_id')
        if alert.get('kind') == 'waste_rate':
            ev_id = _evidence_id(snapshot, sid, 'waste_rate')
            risks.append({
                'risk': f'「{alert["store_name"]}」损耗继续扩大',
                'severity': 'high',
                'evidence_ids': [ev_id] if ev_id else [],
                'recommendation': '先核查库存和储存条件，再决定是否调整备货。',
            })
        else:
            ev_id = _evidence_id(snapshot, sid, 'revenue_drop_3d')
            risks.append({
                'risk': f'「{alert["store_name"]}」营收下滑未止',
                'severity': 'high',
                'evidence_ids': [ev_id] if ev_id else [],
                'recommendation': '需要补充客流和缺货记录后再执行经营动作。',
            })
    if not risks:
        risks.append({
            'risk': '数据覆盖范围有限，结论可能遗漏未观测因素',
            'severity': 'low',
            'evidence_ids': [],
            'recommendation': '按周复核数据完整性，不把单次结论当因果证明。',
        })
    return _role_result(
        ROLE_AUDITOR, 'fallback', f'规则版识别 {len(risks)} 项风险。', risks,
        status='fallback')


def _extract_json(text):
    raw = str(text or '').strip()
    if raw.startswith('```'):
        raw = raw.strip('`')
        if raw.lower().startswith('json'):
            raw = raw[4:].strip()
    start, end = raw.find('{'), raw.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('invalid_json')
    value = json.loads(raw[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError('invalid_json')
    return value


def _normalize_evidence_ids(raw_ids, allowed_ids):
    clean, discarded = [], 0
    for raw_id in raw_ids or []:
        evidence_id = str(raw_id or '').strip()
        if evidence_id in allowed_ids:
            if evidence_id not in clean:
                clean.append(evidence_id)
        elif evidence_id:
            discarded += 1
    return clean, discarded


def _normalize_advisor(raw, allowed_ids):
    items = raw.get('proposals')
    if not isinstance(items, list) or not items:
        raise ValueError('invalid_advisor')
    proposals, discarded = [], 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('invalid_advisor')
        action = _clean_text(item.get('action'), 120)
        rationale = _clean_text(item.get('rationale'), 240)
        if not action or not rationale:
            raise ValueError('invalid_advisor')
        evidence_ids, dropped = _normalize_evidence_ids(
            item.get('evidence_ids'), allowed_ids)
        discarded += dropped
        if not evidence_ids:
            continue
        proposals.append({
            'action': action,
            'rationale': rationale,
            'evidence_ids': evidence_ids,
            'priority': _clean_text(item.get('priority') or 'medium', 16),
        })
    return proposals, discarded


def _normalize_auditor(raw, allowed_ids):
    items = raw.get('risks')
    if not isinstance(items, list) or not items:
        raise ValueError('invalid_auditor')
    risks, discarded = [], 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('invalid_auditor')
        risk = _clean_text(item.get('risk'), 180)
        recommendation = _clean_text(item.get('recommendation'), 240)
        severity = _clean_text(item.get('severity') or 'medium', 16).lower()
        if severity not in ('high', 'medium', 'low'):
            severity = 'medium'
        if not risk or not recommendation:
            raise ValueError('invalid_auditor')
        evidence_ids, dropped = _normalize_evidence_ids(
            item.get('evidence_ids'), allowed_ids)
        discarded += dropped
        if not evidence_ids:
            continue
        risks.append({
            'risk': risk,
            'severity': severity,
            'evidence_ids': evidence_ids,
            'recommendation': recommendation,
        })
    return risks, discarded


def _contract_error(code, discarded=0):
    """构造带「已丢弃越界证据数」的契约异常。

    角色输出若全部越界，会被判定为 ``no_valid_evidence`` 并降级为规则版。若异常上
    不携带丢弃数，降级结果就会丢掉这条安全观测值——**越界行为变得不可见**。
    因此用属性附带计数；异常类型仍保持 ``ValueError``，不改变既有 ``error`` 字段口径。

    Args:
        code: 失败原因码，写入异常消息。
        discarded: 本次已丢弃的越界 evidence id 数量。

    Returns:
        ValueError: 附有 ``discarded`` 属性的异常实例。
    """
    exc = ValueError(str(code))
    exc.discarded = int(discarded or 0)
    return exc


def _advisor_prompt(snapshot):
    return (
        '你是小微连锁门店的经营参谋。只能使用下列 evidence，'
        '输出严格 JSON：{"proposals":[{"action":"...",'
        '"rationale":"...","evidence_ids":["ev-1"],"priority":"high|medium|low"}]}。'
        '不得创造新 evidence id，不得输出营销文案。\n'
        f'门店排行={json.dumps(snapshot["ranking"], ensure_ascii=False)}\n'
        f'异常={json.dumps(snapshot["alerts"], ensure_ascii=False)}\n'
        f'evidence={json.dumps(snapshot["evidence"], ensure_ascii=False)}'
    )


def _auditor_prompt(snapshot):
    return (
        '你是风险审计员。只使用下列 evidence，审查经营建议的风险与证据缺口，'
        '输出严格 JSON：{"risks":[{"risk":"...","severity":"high|medium|low",'
        '"evidence_ids":["ev-1"],"recommendation":"..."}]}。'
        '不得创造新 evidence id，不得把相关性写成因果。\n'
        f'门店排行={json.dumps(snapshot["ranking"], ensure_ascii=False)}\n'
        f'异常={json.dumps(snapshot["alerts"], ensure_ascii=False)}\n'
        f'evidence={json.dumps(snapshot["evidence"], ensure_ascii=False)}'
    )


def role_prompts(snapshot):
    """返回两个模型角色的提示词（单一拼装入口）。

    对外暴露的目的：AI 评测 CLI 与在线延迟复测需要在**不经过完整编排**的情况下
    直接调用模型，若在外部重复拼装 Prompt，就会产生第二份契约文本，
    一旦主流程修改契约，评测结论将不再代表真实行为。

    Args:
        snapshot: 与 ``run_decision`` 内部同构的快照字典。

    Returns:
        dict: ``{ROLE_ADVISOR: str, ROLE_AUDITOR: str}``。
    """
    return {
        ROLE_ADVISOR: _advisor_prompt(snapshot),
        ROLE_AUDITOR: _auditor_prompt(snapshot),
    }


def _call_role(role, snapshot, client, fallback):
    prompt = _advisor_prompt(snapshot) if role == ROLE_ADVISOR else _auditor_prompt(snapshot)
    try:
        if _LLM['fn'] is not None:
            raw = _LLM['fn'](prompt, role)
        elif client is not None and client.enabled:
            raw = client.decision_completion(prompt, role)
        else:
            return fallback(snapshot), 0, 'not_configured'
        if isinstance(raw, dict):
            if not raw.get('ok'):
                raise RuntimeError(raw.get('source') or 'role_failed')
            content = raw.get('content') or ''
            source = raw.get('source') or 'ai'
            tokens = int(raw.get('tokens') or 0)
        else:
            content = raw or ''
            source = 'ai'
            tokens = max(1, (len(prompt) + len(content)) // 4)
        payload = _extract_json(content)
        allowed = {item['id'] for item in snapshot.get('evidence') or []}
        if role == ROLE_ADVISOR:
            items, discarded = _normalize_advisor(payload, allowed)
            if not items:
                raise _contract_error('no_valid_evidence', discarded)
            result = _role_result(
                role, source, f'形成 {len(items)} 条带证据建议。', items,
                discarded_evidence=discarded, tokens=tokens)
        else:
            items, discarded = _normalize_auditor(payload, allowed)
            if not items:
                raise _contract_error('no_valid_evidence', discarded)
            result = _role_result(
                role, source, f'识别 {len(items)} 项带证据风险。', items,
                discarded_evidence=discarded, tokens=tokens)
        return result, tokens, 'ok'
    except TimeoutError:
        result = fallback(snapshot)
        result.update({'status': 'timeout', 'error': 'timeout'})
        return result, 0, 'timeout'
    except Exception as exc:
        result = fallback(snapshot)
        result.update({'status': 'error', 'error': exc.__class__.__name__,
                       'discarded_evidence': getattr(exc, 'discarded', 0)})
        # 第三个返回值是可归因原因码（供 UI 说明「为什么变简」），
        # status/error 两个字段维持原口径不变，既有断言与留痕不受影响。
        return result, 0, degrade_reason_from(exc)


def _run_isolated_roles(snapshot):
    from src.dify_client import dify_client

    advisor_client = None if _LLM['fn'] is not None else dify_client.fork()
    auditor_client = None if _LLM['fn'] is not None else dify_client.fork()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='decision-role') as pool:
        # diag.submit：把请求上下文复制进池线程，两个角色的 AI 留痕才带得上
        # request_id（线程默认不继承 contextvars，见 src/diag.py 口径 4）
        advisor_future = diag.submit(
            pool, _call_role, ROLE_ADVISOR, snapshot, advisor_client, _advisor_fallback)
        auditor_future = diag.submit(
            pool, _call_role, ROLE_AUDITOR, snapshot, auditor_client, _auditor_fallback)
        advisor, advisor_tokens, advisor_state = advisor_future.result()
        auditor, auditor_tokens, auditor_state = auditor_future.result()
    return advisor, auditor, advisor_tokens, auditor_tokens, advisor_state, auditor_state


def _moderate(analyst, advisor, auditor):
    proposals = advisor.get('proposals') or []
    supported = [item for item in proposals if item.get('evidence_ids')]
    risks = auditor.get('risks') or []
    high_evidence = set()
    for risk in risks:
        if risk.get('severity') == 'high':
            high_evidence.update(risk.get('evidence_ids') or [])
    accepted, conflicts = [], []
    for proposal in supported:
        overlap = high_evidence & set(proposal.get('evidence_ids') or [])
        if overlap:
            conflicts.append({
                'proposal': proposal,
                'risk': next(
                    (risk for risk in risks
                     if risk.get('severity') == 'high'
                     and overlap & set(risk.get('evidence_ids') or [])),
                    None),
                'evidence_ids': sorted(overlap),
            })
        else:
            accepted.append(proposal)
    if conflicts:
        summary = f'存在 {len(conflicts)} 项高风险分歧，需人工核查后再决定。'
        claims = [_claim(
            f'经营建议「{item["proposal"]["action"]}」与高风险「{item["risk"]["risk"]}」'
            '共享同一组证据，暂不自动执行。',
            item['evidence_ids'], 'disagreement') for item in conflicts]
        if not accepted:
            accepted.append({
                'action': '先补齐受影响门店的客流与库存记录',
                'rationale': '高风险冲突共享同一组证据，先补充记录再决定经营动作。',
                'evidence_ids': sorted(high_evidence),
                'priority': 'high',
            })
    elif not accepted:
        summary = '没有形成带证据的可执行建议，本轮不创建 AI 来源任务。'
        claims = [_claim('建议和风险均缺少可用证据。', kind='no_action')]
    else:
        summary = f'风险审计未阻断 {len(accepted)} 条建议，可进入人工确认。'
        claims = [_claim(
            '经营建议与风险审计在当前证据下没有高风险冲突。',
            sorted({ev for item in accepted for ev in item.get('evidence_ids') or []}),
            'consensus')]
    return _role_result(ROLE_MODERATOR, 'rule', summary, claims), {
        'status': 'disputed' if conflicts else ('consensus' if accepted else 'no_action'),
        'summary': summary,
        'selected_actions': accepted[:3],
        'disagreements': conflicts,
    }


# ── 降级可归因（H8，第 9 轮）────────────────────────────────────
# 「为什么这次变简单了」必须是可回答的：红线要求 AI 只给参考建议，而参考建议的
# 可信度取决于它到底出自大模型还是规则兜底。四种成因的用户动作完全不同
# （等一会 / 查配置 / 换门店数据 / 无需动作），混成一句「已降级」等于没告知。
# 文案单一真相源在本表，模板只取值渲染（沿用 P7-E3-B2 收口口径，不再内联副本）。
DEGRADE_REASONS = {
    'saturated': '本机 AI 并发已满，本次为规则版结果；稍等片刻重试即可。',
    'timeout': '大模型响应超时，本次回退规则版；可稍后手动刷新重试。',
    'upstream_error': '大模型服务调用失败，本次回退规则版；请检查 AI 配置或服务状态。',
    'not_configured': '未配置大模型服务，当前为离线规则版结果。',
    # 首屏走 initial_decision（设计上不占 AI 额度、不等模型），但配了大模型的老板
    # 只看到「规则版」会以为 AI 坏了 —— 这条状态必须单独说清「点刷新即可」。
    'not_generated': '首屏为规则版结果（不占用 AI 额度）；点右上角「刷新决策」可生成大模型分析。',
    'quota_exceeded': '今日 AI 决策次数已用完，本次为规则版结果；明日额度重置后可再生成。',
    'contract_rejected': '大模型输出缺少可核验的门店证据，已按安全规则丢弃并回退规则版。',
}
# 规则版/确定性角色不是「降级」，而是设计内的正常输出，不参与可归因提示
_NON_DEGRADED_STATES = frozenset({'ok', 'rule', 'deterministic'})


def _skip_reason_code(skip_reason, ai_available):
    """调用方未允许调模型时的归因码，三态分开（H8 可归因降级）。

    ① ``skip_reason`` 为 None ⇒ 调用方主动只读（只读演示入口），是设计内输出，
       不算降级，故不给任何提示；
    ② 首屏快路径且 AI 可用 ⇒ 用调用方给的码（'not_generated'），告知「点刷新即得」；
    ③ 首屏快路径但 AI 没配 ⇒ 如实说「未配置」，不用 not_generated 误导。

    Args:
        skip_reason: 调用方声明的跳过成因，None 表示主动只读。
        ai_available: 大模型是否配置可用（与本次是否允许调用无关）。

    Returns:
        str: 原因码，必属 DEGRADE_REASONS 的键或 _NON_DEGRADED_STATES。
    """
    if skip_reason is None:
        return 'deterministic'
    return skip_reason if ai_available else 'not_configured'


def degrade_reason_from(exc):
    """把角色调用异常映射为可归因原因码。

    契约异常本身就是 `ValueError(code)`（见 `_contract_error`），消息即原因码；
    `_call_role` 里由 `decision_completion` 抛出的 RuntimeError 消息则是上游 `source`
    值（'saturated' / 'error'）。因此这里按消息精确匹配，未知一律归 upstream_error。
    """
    code = str(exc).strip()
    if code == 'saturated':
        return 'saturated'
    if code in ('no_valid_evidence', 'contract_empty', 'bad_payload'):
        return 'contract_rejected'
    return 'upstream_error'


def _source_for_result(result):
    if result.get('source') in ('deepseek', 'dify', 'ai'):
        return 'ai'
    if result.get('source') in ('rule', 'not_configured'):
        return 'rule'
    return 'fallback'


def _task_draft(decision, evidence):
    selected = decision.get('selected_actions') or []
    primary = selected[0] if selected else None
    title = (primary or {}).get('action') or '复核 AI 经营决策'
    detail_parts = []
    if primary:
        detail_parts.append(primary.get('rationale') or '')
    detail_parts.append(decision.get('summary') or '')
    if decision.get('disagreements'):
        detail_parts.append('存在分歧，已保留证据供人工复核。')
    return {
        'title': _clean_text(title, 120),
        'detail': _clean_text(' '.join(part for part in detail_parts if part), 600),
        'evidence': evidence,
    }


def _append_trace(payload):
    try:
        os.makedirs(os.path.dirname(DECISION_TRACE_PATH), exist_ok=True)
        with _TRACE_LOCK:
            if (os.path.exists(DECISION_TRACE_PATH)
                    and os.path.getsize(DECISION_TRACE_PATH) >= DECISION_TRACE_MAX_BYTES):
                for idx in range(DECISION_TRACE_BACKUPS, 0, -1):
                    src = DECISION_TRACE_PATH if idx == 1 else f'{DECISION_TRACE_PATH}.{idx - 1}'
                    dst = f'{DECISION_TRACE_PATH}.{idx}'
                    if os.path.exists(src):
                        os.replace(src, dst)
            with open(DECISION_TRACE_PATH, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + '\n')
    except OSError:
        logger.debug('decision trace write skipped', exc_info=True)


def _cache_key(merchant_id, store_ids, days):
    ids = ','.join(str(s) for s in sorted(int(s) for s in store_ids or []))
    return f'decision:{int(merchant_id)}:{ids}:{int(days)}'


def _build_result(snapshot, analyst, advisor, auditor, moderator, decision, *,  # noqa: PLR0913, PLR0917
                  quota, cache_state, configured, degrade=None):
    roles = [analyst, advisor, auditor, moderator]
    sources = {
        ROLE_DATA_ANALYST: _source_for_result(analyst),
        ROLE_ADVISOR: _source_for_result(advisor),
        ROLE_AUDITOR: _source_for_result(auditor),
        ROLE_MODERATOR: _source_for_result(moderator),
    }
    # 只保留真正「比预期简单」的成因；ok / 规则版 / 确定性演示输出不进提示
    degrade_map = {
        role: code for role, code in (degrade or {}).items()
        if code and code not in _NON_DEGRADED_STATES
    }
    evidence = list(snapshot.get('evidence') or [])
    return {
        'run_id': uuid4().hex[:12],
        'generated_at': _now_text(),
        'days': snapshot.get('days') or DECISION_DAYS,
        'store_ids': snapshot.get('store_ids') or [],
        'evidence': evidence,
        'roles': roles,
        'sources': sources,
        # H8 可归因降级：degrade 是原因码（机器可读，留痕/断言用），
        # degrade_text 是给用户看的中文说明（单一真相源 = DEGRADE_REASONS）
        'degrade': degrade_map,
        'degrade_text': {r: DEGRADE_REASONS.get(c, c) for r, c in degrade_map.items()},
        'degraded': bool(degrade_map),
        'decision': decision,
        'task_draft': _task_draft(decision, evidence),
        'quota': quota,
        'cache': cache_state,
        'configured': bool(configured),
    }


def run_decision(merchant_id, store_ids, days=DECISION_DAYS, *, refresh=False,  # noqa: PLR0913
                 allow_ai=True, store_id=None, today=None, write_trace=True,
                 skip_reason=None):
    """生成一次完整驾驶舱结果；网络推理并行，其余操作全部在主线程。

    Args:
        merchant_id: 商家主键，配额与缓存的作用域。
        store_ids: 本次允许的证据门店集合（越界门店不会进入快照）。
        days: 统计区间，仅允许 7 / 30 / 90，其余回退默认。
        refresh: 为 True 时绕过 10 分钟缓存强制重算。
        allow_ai: 为 False 时不检查模型配置、不产生任何模型调用（只读演示入口用）。
        store_id: 当前门店上下文，仅用于配额明细标注。
        skip_reason: allow_ai=False 时的归因码（H8）。默认 None = 「设计内的确定性输出」
            （只读演示入口用，不算降级）；首屏传 'not_generated' 以告知用户「点刷新可得大模型」。
        today: 统计截止日，测试注入用。
        write_trace: 为 False 时跳过决策轨迹 JSONL 落盘（评测与只读演示用，
            它们不得产生任何文件副作用）。

    Returns:
        dict: 驾驶舱完整结果（roles / sources / evidence / decision / task_draft / quota）。
    """
    allowed = sorted({int(s) for s in store_ids or []})
    days = int(days) if int(days) in DECISION_RANGE_CHOICES else DECISION_DAYS
    key = _cache_key(merchant_id, allowed, days)
    if not refresh:
        cached = ai_quota.cache_get(merchant_id, ai_quota.KIND_DECISION, key)
        if cached is not None:
            result = dict(cached)
            result['cache'] = 'hit'
            return result

    snapshot = build_snapshot(allowed, days=days, today=today)
    analyst = _analyst_result(snapshot)
    quota = ai_quota.check_quota(merchant_id, ai_quota.KIND_DECISION)
    # 「AI 是否可用」与「本次是否允许调 AI」是两件独立的事，必须分开求值：
    # 原写法只在 allow_ai=True 时才求 configured，于是首屏（恒 allow_ai=False）
    # 永远拿不到「已配置但本次没调」这个成因，只能误报成「未配置」。
    ai_available = _LLM['fn'] is not None
    if not ai_available:
        from src.dify_client import dify_client
        ai_available = bool(dify_client.enabled)
    configured = bool(allow_ai and ai_available)

    if configured and quota.get('ok'):
        advisor, auditor, advisor_tokens, auditor_tokens, adv_state, aud_state = \
            _run_isolated_roles(snapshot)
        tokens = int(advisor_tokens or 0) + int(auditor_tokens or 0)
        detail = json.dumps({
            'advisor': advisor.get('status'),
            'auditor': auditor.get('status'),
        }, ensure_ascii=False)
        ai_called = advisor.get('source') in ('ai', 'deepseek', 'dify') or \
            auditor.get('source') in ('ai', 'deepseek', 'dify')
        if ai_called:
            ai_quota.record_usage(
                merchant_id, ai_quota.KIND_DECISION, tokens=tokens,
                store_id=store_id, ok=True, detail=detail)
        quota = ai_quota.check_quota(merchant_id, ai_quota.KIND_DECISION)
    else:
        advisor = _advisor_fallback(snapshot)
        auditor = _auditor_fallback(snapshot)
        # 三种「没调大模型」的成因必须分开告知：配额用完、压根没配、以及只读演示
        # （后者是设计内行为，不算降级）。此前三者渲染成同一句「规则版」，
        # 老板无法判断是该明天再来、该配 Key、还是本来就不该给写入口。
        if not allow_ai:
            adv_state = aud_state = _skip_reason_code(skip_reason, ai_available)
        elif configured:
            adv_state = aud_state = 'quota_exceeded'
        else:
            adv_state = aud_state = 'not_configured'

    degrade = {ROLE_ADVISOR: adv_state, ROLE_AUDITOR: aud_state}
    moderator, decision = _moderate(analyst, advisor, auditor)
    result = _build_result(
        snapshot, analyst, advisor, auditor, moderator, decision,
        quota=quota, cache_state='miss', configured=configured, degrade=degrade)
    ai_quota.cache_put(merchant_id, ai_quota.KIND_DECISION, key, result)
    if write_trace:
        _append_trace({
            'run_id': result['run_id'],
            'generated_at': result['generated_at'],
            'merchant_id': int(merchant_id),
            'store_ids': allowed,
            'days': days,
            'sources': result['sources'],
            'decision_status': decision.get('status'),
            'disagreements': len(decision.get('disagreements') or []),
        })
    return result


def initial_decision(merchant_id, store_ids, days=DECISION_DAYS, store_id=None,
                     write_trace=True):
    """首屏只读取缓存或构建规则版，不触发模型推理。"""
    return run_decision(
        merchant_id, store_ids, days=days, refresh=False,
        allow_ai=False, store_id=store_id, write_trace=write_trace,
        skip_reason='not_generated')


__all__ = [
    'DECISION_DAYS',
    'DECISION_RANGE_CHOICES',
    'ROLE_ADVISOR',
    'ROLE_AUDITOR',
    'ROLE_DATA_ANALYST',
    'ROLE_LABELS',
    'ROLE_MODERATOR',
    'build_snapshot',
    'initial_decision',
    'role_prompts',
    'run_decision',
    'set_llm',
]
