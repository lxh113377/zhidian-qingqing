"""多门店聚合与规则版经营分析（PH2-5）。

职责边界：
  · 只做**门店维度聚合**与**规则版结论**（AI 版在路由层经 ai_quota 调用后覆盖）；
  · 每条结论都带 `evidence`（门店 + 指标 + 数值），供页面与轨迹展示；
  · 门店集合由调用方传入（来自 `tenant.allowed_store_ids`），本模块不做权限判断。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from src import ai_quota, industry_pack
from src.models import DailySales, QuizRecord, Store
from src import business_calendar as cal

DEFAULT_DAYS = 14
REVENUE_DROP_RATIO = 0.3  # 近 3 天较前 3 天跌幅超过 30% 视为异常

# LLM 注入口（默认未接入 → 直接规则版，不消耗配额）
_LLM = {'fn': None}


def set_llm(fn):
    """注入 LLM 调用器（`fn(prompt) -> dict | str | None`）。"""
    _LLM['fn'] = fn


def llm_configured():
    return _LLM['fn'] is not None


def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _pack_clause():
    """多店汇总不按包过滤：门店历史可能跨包，门店视角看整体经营。"""
    return DailySales.store_id.isnot(None)


def store_metrics(store_ids, days=DEFAULT_DAYS, today=None):
    """逐店聚合：营收 / 销量 / 损耗率 / 记录数 / 培训均分。"""
    if not store_ids:
        return []
    today = today or cal.business_today()
    cutoff = today - timedelta(days=days - 1)
    names = {s.id: s.name for s in Store.query.filter(Store.id.in_(list(store_ids))).all()}
    rows = DailySales.query.filter(
        DailySales.store_id.in_(list(store_ids)),
        DailySales.record_date >= cutoff,
        _pack_clause(),
    ).all()
    agg = {}
    for r in rows:
        item = agg.setdefault(r.store_id, {'revenue': 0.0, 'qty': 0,
                                           'remain': 0.0, 'waste': 0.0, 'records': 0})
        item['revenue'] += float(r.total_revenue or 0)
        item['qty'] += int(r.product_sales_qty or 0)
        item['remain'] += _num(r.raw_material_remaining)
        item['waste'] += _num(r.raw_material_waste)
        item['records'] += 1

    quiz = {}
    for q in QuizRecord.query.filter(QuizRecord.store_id.in_(list(store_ids))).all():
        bucket = quiz.setdefault(q.store_id, [])
        if q.score is not None:
            bucket.append(float(q.score))

    out = []
    for sid in list(store_ids):
        item = agg.get(sid, {'revenue': 0.0, 'qty': 0, 'remain': 0.0,
                             'waste': 0.0, 'records': 0})
        total_material = item['remain'] + item['waste']
        scores = quiz.get(sid) or []
        out.append({
            'store_id': sid,
            'store_name': names.get(sid) or f'门店{sid}',
            'revenue': round(item['revenue'], 2),
            'quantity': item['qty'],
            'records': item['records'],
            'waste_rate': (item['waste'] / total_material) if total_material else None,
            'quiz_avg': (sum(scores) / len(scores)) if scores else None,
            'quiz_count': len(scores),
        })
    return out


def store_ranking(store_ids, days=DEFAULT_DAYS, today=None, metrics=None):
    """按营收降序排名（无数据门店排在末尾并标注）。

    metrics: 调用方已有 store_metrics 结果时直接传入复用（P2：同请求内
    不重复聚合），传 None 则内部现算，两种方式返回值完全一致。
    """
    rows = metrics if metrics is not None else store_metrics(store_ids, days=days, today=today)
    rows.sort(key=lambda x: (x['records'] > 0, x['revenue']), reverse=True)
    for idx, row in enumerate(rows, start=1):
        row['rank'] = idx
        row['has_data'] = row['records'] > 0
    return rows


def cross_store_trend(store_ids, days=DEFAULT_DAYS, today=None):
    """跨店按日汇总：[{'date','revenue','quantity'}]（按日期升序）。"""
    if not store_ids:
        return []
    today = today or cal.business_today()
    cutoff = today - timedelta(days=days - 1)
    rows = DailySales.query.filter(
        DailySales.store_id.in_(list(store_ids)),
        DailySales.record_date >= cutoff,
        _pack_clause(),
    ).all()
    daily = {}
    for r in rows:
        key = r.record_date.isoformat()
        item = daily.setdefault(key, {'date': key, 'revenue': 0.0, 'quantity': 0})
        item['revenue'] = round(item['revenue'] + float(r.total_revenue or 0), 2)
        item['quantity'] += int(r.product_sales_qty or 0)
    return [daily[k] for k in sorted(daily)]


def abnormal_stores(store_ids, days=DEFAULT_DAYS, today=None, metrics=None):
    """异常门店：损耗率超阈值 或 近 3 日营收较前 3 日跌幅超 30%。

    metrics: 调用方已有 store_metrics 结果时直接传入复用（P2，同上）。
    """
    rows = metrics if metrics is not None else store_metrics(store_ids, days=days, today=today)
    threshold = industry_pack.pack_waste_rate_threshold()
    today = today or cal.business_today()
    # P1（N+1 收敛）：营收跌幅一次性批量查询，不再逐店各发一条
    drops = _revenue_drops([row['store_id'] for row in rows], today)
    alerts = []
    for row in rows:
        if row['waste_rate'] is not None and row['waste_rate'] > threshold:
            alerts.append({
                'store_id': row['store_id'], 'store_name': row['store_name'],
                'kind': 'waste_rate', 'value': round(row['waste_rate'], 4),
                'threshold': threshold,
                'evidence': [{'store_id': row['store_id'],
                              'metric': 'waste_rate',
                              'value': round(row['waste_rate'], 4)}],
            })
        drop = drops.get(row['store_id'])
        if drop is not None and drop > REVENUE_DROP_RATIO:
            alerts.append({
                'store_id': row['store_id'], 'store_name': row['store_name'],
                'kind': 'revenue_drop', 'value': round(drop, 4),
                'threshold': REVENUE_DROP_RATIO,
                'evidence': [{'store_id': row['store_id'],
                              'metric': 'revenue_drop_3d',
                              'value': round(drop, 4)}],
            })
    return alerts


def _revenue_drops(store_ids, today):
    """批量「近 3 天较前 3 天营收跌幅」（P1：一次查询覆盖全部门店）。

    Returns:
        dict: {store_id: drop}；仅含「前 3 天营收 > 0」的门店（其余无跌幅可言）。
    """
    ids = list(store_ids)
    if not ids:
        return {}
    recent_start = today - timedelta(days=2)
    prior_start = today - timedelta(days=5)
    rows = DailySales.query.filter(
        DailySales.store_id.in_(ids),
        DailySales.record_date >= prior_start,
        _pack_clause(),
    ).all()
    recent = defaultdict(float)
    prior = defaultdict(float)
    for r in rows:
        rev = float(r.total_revenue or 0)
        if r.record_date >= recent_start:
            recent[r.store_id] += rev
        elif r.record_date >= prior_start:
            prior[r.store_id] += rev
    out = {}
    for sid in ids:
        base = prior.get(sid, 0.0)
        if base <= 0:
            continue
        out[sid] = max(0.0, (base - recent.get(sid, 0.0)) / base)
    return out


def _revenue_drop(store_id, today):
    """单店跌幅（保留单店入口；内部委托批量版，行为一致）。"""
    return _revenue_drops([store_id], today).get(store_id)


def drill_down(store_id, days=DEFAULT_DAYS, today=None):
    """单店下钻：指标 + 日趋势 + 培训概览。"""
    metrics = store_metrics([store_id], days=days, today=today)
    if not metrics:
        return None
    today = today or cal.business_today()
    cutoff = today - timedelta(days=days - 1)
    rows = DailySales.query.filter(
        DailySales.store_id == store_id,
        DailySales.record_date >= cutoff,
        _pack_clause(),
    ).all()
    daily, products = {}, {}
    for r in rows:
        key = r.record_date.isoformat()
        item = daily.setdefault(key, {'date': key, 'revenue': 0.0, 'quantity': 0})
        item['revenue'] = round(item['revenue'] + float(r.total_revenue or 0), 2)
        item['quantity'] += int(r.product_sales_qty or 0)
        products[r.product_name] = products.get(r.product_name, 0) + int(
            r.product_sales_qty or 0)
    top = sorted(products.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {'metrics': metrics[0], 'trend': [daily[k] for k in sorted(daily)],
            'top_products': [{'name': n, 'quantity': q} for n, q in top]}


def rule_analysis(store_ids, days=DEFAULT_DAYS, today=None):
    """规则版跨店分析：结论 + 证据引用（AI 不可用时的降级目标）。"""
    # P2：ranking 与 alerts 共享一次 store_metrics 聚合，不再各算各的
    metrics = store_metrics(store_ids, days=days, today=today)
    ranking = store_ranking(store_ids, days=days, today=today, metrics=metrics)
    alerts = abnormal_stores(store_ids, days=days, today=today, metrics=metrics)
    findings = []
    active = [r for r in ranking if r['has_data']]
    if active:
        top = active[0]
        findings.append({
            'kind': 'summary',
            'text': (f'本期营收最高门店为「{top["store_name"]}」'
                     f'（{top["revenue"]:.2f} 元，共 {top["records"]} 条记录）。'),
            'evidence': [{'store_id': top['store_id'], 'metric': 'revenue',
                          'value': top['revenue']}],
        })
    low = [r for r in active if r['revenue'] < (active[0]['revenue'] * 0.6)]
    for row in low[:2]:
        findings.append({
            'kind': 'gap',
            'text': (f'「{row["store_name"]}」营收仅为最高门店的'
                     f'{row["revenue"] / max(active[0]["revenue"], 1e-9):.0%}，建议核查排班与备货。'),
            'evidence': [{'store_id': row['store_id'], 'metric': 'revenue',
                          'value': row['revenue']}],
        })
    for alert in alerts[:4]:
        if alert['kind'] == 'waste_rate':
            findings.append({
                'kind': 'root_cause',
                'text': (f'「{alert["store_name"]}」损耗率 {alert["value"]:.1%} 超过阈值 '
                         f'{alert["threshold"]:.0%}，优先核查备货量与储存条件。'),
                'evidence': alert['evidence'],
            })
        else:
            findings.append({
                'kind': 'root_cause',
                'text': (f'「{alert["store_name"]}」近 3 日营收较前 3 日下滑 '
                         f'{alert["value"]:.0%}，建议核查客流与竞争环境。'),
                'evidence': alert['evidence'],
            })
    training = [r for r in active if r['quiz_avg'] is not None and r['quiz_avg'] < 80]
    for row in training[:2]:
        findings.append({
            'kind': 'training',
            'text': (f'「{row["store_name"]}」培训均分 {row["quiz_avg"]:.1f}，'
                     f'建议安排薄弱知识域复训。'),
            'evidence': [{'store_id': row['store_id'], 'metric': 'quiz_avg',
                          'value': round(row['quiz_avg'], 2)}],
        })
    if not findings:
        findings.append({'kind': 'summary', 'text': '当前门店数据不足，暂无可分析结论。',
                         'evidence': []})
    return {'source': 'rule', 'days': days, 'ranking': ranking, 'alerts': alerts,
            'findings': findings}


def _trace_step(step, detail, ok=True):
    return {'step': step, 'detail': detail, 'ok': ok}


def _build_prompt(ranking, alerts, days):
    lines = [
        f'你是多门店经营分析师。基于以下 {days} 天门店数据，输出 JSON：',
        '{"findings":[{"kind":"summary|gap|root_cause|stocking|training",'
        '"text":"结论","evidence":[{"store_id":<int>,"metric":"<名称>","value":<数值>}]}]}',
        '要求：每条结论必须带 evidence，且 store_id 只能取自下面列出的门店；不要输出营销文案。',
        '门店数据：',
    ]
    for row in ranking:
        lines.append(
            f"- store_id={row['store_id']} {row['store_name']}：营收 {row['revenue']}，"
            f"记录 {row['records']}，损耗率 {row['waste_rate']}，培训均分 {row['quiz_avg']}")
    if alerts:
        lines.append('已识别异常：' + '；'.join(
            f"{a['store_name']}({a['kind']}={a['value']})" for a in alerts))
    return '\n'.join(lines)


def _valid_findings(raw, allowed_ids):  # noqa: PLR0911
    """校验 AI 结论结构：findings 列表 + 每条含 text 与集合内 evidence。"""
    if not isinstance(raw, dict):
        return None
    findings = raw.get('findings')
    if not isinstance(findings, list) or not findings:
        return None
    out = []
    for item in findings:
        if not isinstance(item, dict):
            return None
        text = str(item.get('text') or '').strip()
        evidence = item.get('evidence')
        if not text or not isinstance(evidence, list) or not evidence:
            return None
        clean = []
        for ev in evidence:
            if not isinstance(ev, dict):
                return None
            try:
                sid = int(ev.get('store_id'))
            except (TypeError, ValueError):
                return None
            if sid not in allowed_ids or not str(ev.get('metric') or '').strip():
                return None
            clean.append({'store_id': sid, 'metric': str(ev['metric']),
                          'value': ev.get('value')})
        out.append({'kind': str(item.get('kind') or 'summary'), 'text': text,
                    'evidence': clean})
    return out


def analyze_cross_store(merchant_id, store_ids, days=DEFAULT_DAYS, today=None,  # noqa: PLR0913, PLR0917
                        store_id=None, cache_key=None):
    """跨店分析：规则基线 + 可选 AI 覆盖（配额/缓存/超时/降级统一走 ai_quota）。

    Returns:
        dict: {'source', 'findings', 'trace', 'fallback_reason',
               'ranking', 'alerts', 'days'}
    """
    base = rule_analysis(store_ids, days=days, today=today)
    _total_records = sum(r['records'] for r in base['ranking'])
    trace = [_trace_step('收集数据',
                         f'{len(store_ids)} 家门店 / {days} 天，记录 {_total_records} 条'),
             _trace_step('规则基线', f'生成 {len(base["findings"])} 条规则结论')]
    if not llm_configured():
        trace.append(_trace_step('AI 分析', '未接入在线 AI，使用规则版', ok=False))
        return {**base, 'trace': trace, 'fallback_reason': 'llm_not_configured'}

    allowed = {int(s) for s in store_ids}

    def _producer(_timeout):
        raw = _LLM['fn'](_build_prompt(base['ranking'], base['alerts'], days))
        findings = _valid_findings(raw, allowed)
        if findings is None:
            raise ValueError('invalid_structure')
        return {'findings': findings}, 800

    run = ai_quota.run_with_quota(
        merchant_id, ai_quota.KIND_ANALYSIS, _producer,
        lambda: {'findings': base['findings']},
        cache_key=(cache_key or f'multi:{sorted(allowed)}:{days}'), store_id=store_id)
    if run['source'] in ('ai', 'cache'):
        trace.append(_trace_step(
            'AI 分析', f'来源 {run["source"]}，{len(run["value"]["findings"])} 条结论'))
        return {**base, 'source': run['source'],
                'findings': run['value']['findings'], 'trace': trace,
                'fallback_reason': None}
    trace.append(_trace_step('AI 分析', f'已降级：{run["reason"]}', ok=False))
    return {**base, 'trace': trace, 'fallback_reason': run['reason']}


__all__ = [
    'DEFAULT_DAYS',
    'REVENUE_DROP_RATIO',
    'abnormal_stores',
    'analyze_cross_store',
    'cross_store_trend',
    'drill_down',
    'llm_configured',
    'rule_analysis',
    'set_llm',
    'store_metrics',
    'store_ranking',
]
