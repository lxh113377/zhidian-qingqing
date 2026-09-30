"""AI 可解释性层（第四轮 D3）— 建议置信度 + 数据依据溯源

解决的问题：AI 给出建议不难，难的是让店主知道「凭什么信它」。
本模块为每条 AI 结论附上：

  - **置信度 confidence**：由样本量与数据充分度推导，样本越少置信越低，
    并显式标注等级（高/中/偏低/低），杜绝「数据只有 3 天也敢下结论」；
  - **依据 evidence**：支撑该结论的具体数据点（数值 + 口径），可逐条核对；
  - **局限 caveat**：结论不适用的边界（如「不含促销日」「未剔除异常日」）。

设计约定：
  1. 纯函数、零第三方依赖、不查 DB、不发网络请求；
  2. 只接收 `analytics.compute_dashboard_stats()` 的 stats 结构；
  3. 数据不足时一律输出低置信 + 明确 caveat，绝不编造依据。
"""
from src import industry_pack, metrics  # 损耗阈值随包（M-4）+ 数量单位随包（B2b）；轻依赖，不引入 pandas / DB

# 置信度等级（阈值降序，首个命中即返回）
CONFIDENCE_LEVELS = (
    (0.80, '高'),
    (0.60, '中'),
    (0.40, '偏低'),
    (0.00, '低'),
)

# 各项结论判定为「样本充分」所需的天数/样本量
FULL_SAMPLE_DAYS = 14      # 周期对比类
FULL_SAMPLE_PRODUCT = 7    # 分产品预测类
FULL_SAMPLE_BASKET = 10    # 购物篮关联类（需要足够多的营业日）
CONFIDENCE_CAP = 0.95      # 置信度上限（任何统计结论都不宣称 100% 确定）
CONFIDENCE_FLOOR = 0.30    # 置信度下限（有结论即至少给出最低可信度）


def sample_confidence(sample, full: int) -> float:
    """样本量 → 置信度（线性充分度，封顶 0.95）。

    Args:
        sample: 实际样本量（天数/题数/记录数）。
        full: 视为「充分」的样本量。

    Returns:
        float: 0.0 ~ 0.95 的置信度。
    """
    try:
        n = float(sample)
    except (TypeError, ValueError):
        return 0.0
    if n <= 0:
        return 0.0
    return round(min(1.0, n / float(full)) * CONFIDENCE_CAP, 2)


def level_of(confidence: float) -> str:
    """置信度 → 等级标签。"""
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return CONFIDENCE_LEVELS[-1][1]
    for threshold, label in CONFIDENCE_LEVELS:
        if c >= threshold:
            return label
    return CONFIDENCE_LEVELS[-1][1]


def _clamp(confidence: float) -> float:
    """把置信度收敛到 [FLOOR, CAP] 区间（有结论即不低于下限）。"""
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return CONFIDENCE_FLOOR
    return round(max(CONFIDENCE_FLOOR, min(CONFIDENCE_CAP, c)), 2)


def build_advice(kind: str, claim: str, evidence, sample=0, full=1,  # noqa: PLR0913, PLR0917
                 caveat: str = '') -> dict:
    """组装一条「可解释的 AI 建议」。

    Args:
        kind: 建议类别（如 '周期对比' / '备货建议'）。
        claim: 结论文案。
        evidence: 依据条目列表（字符串）。
        sample: 支撑样本量。
        full: 样本充分阈值。
        caveat: 局限说明。

    Returns:
        dict: {'kind','claim','confidence','level','evidence','sample','caveat'}。
    """
    conf = _clamp(sample_confidence(sample, full))
    return {
        'kind': kind,
        'claim': claim,
        'confidence': conf,
        'level': level_of(conf),
        'evidence': [e for e in (evidence or []) if e],
        'sample': sample,
        'caveat': caveat,
    }


def explain_business(stats: dict) -> list:
    """把看板统计数据翻译为「带置信度与依据」的 AI 结论清单。

    Args:
        stats: `compute_dashboard_stats()` 的返回值。

    Returns:
        list[dict]: `build_advice()` 结构的结论列表；无数据时返回单条
                    「数据不足」的低置信提示。
    """
    if not stats or not stats.get('has_data'):
        return [build_advice(
            '数据充分性', '当前营业数据不足，AI 暂不给出经营结论。',
            ['尚未录入任何营业数据，或统计窗口内无记录'],
            sample=0, full=FULL_SAMPLE_DAYS,
            caveat='请先由店员在 H5 工作台录入每日营业数据后再查看分析')]

    out = []
    kpi = stats.get('kpi') or {}
    # P7-E3-B2b：数量单位随包（与规则版文案/看板同源；M-4 已有读包先例）
    _labels = metrics.dashboard_labels()
    unit, material_unit = _labels['unit'], _labels['material_unit']

    # 1) 周期对比
    cmp_info = stats.get('compare') or {}
    if cmp_info.get('ok'):
        pct = cmp_info.get('qty_pct')
        rev_pct = cmp_info.get('revenue_pct')
        direction = '上升' if (pct or 0) >= 0 else '下滑'
        rev_direction = '上升' if (rev_pct or 0) >= 0 else '下滑'
        out.append(build_advice(
            '周期对比',
            f'近 7 日销量较前 7 日{direction} {abs(pct) if pct is not None else 0}%，'
            f'营收{rev_direction} {abs(rev_pct) if rev_pct is not None else 0}%。',
            ['对比窗口：近 7 日 vs 前 7 日（共 14 个统计日）',
             f'销量变动 {pct}%，营收变动 {rev_pct}%'],
            sample=14, full=FULL_SAMPLE_DAYS,
            caveat='未剔除节假日与促销活动的结构性影响'))

    # 2) 明日销量预测
    fc = stats.get('forecast') or {}
    if fc.get('ok'):
        out.append(build_advice(
            '销量预测',
            f'预计明日销量约 {fc.get("qty")} {unit}'
            + (f'，营收约 ¥{int(fc.get("revenue") or 0)}' if fc.get('revenue') else '') + '。',
            [f'方法：{fc.get("method") or "指数平滑加权 × 趋势系数 × 星期周期因子"}'],
            sample=min(7, int(kpi.get('report_days') or 0)), full=7,
            caveat='趋势外推不适用于突变场景（天气骤变、临时活动、停业）'))

    # 3) 分产品需求预测
    pfc = stats.get('product_forecast') or {}
    top = (pfc.get('items') or [None])[0]
    if pfc.get('ok') and top:
        conf = float(top.get('confidence') or 0)
        out.append(build_advice(
            '分产品预测',
            f'「{top.get("product")}」明日需求约 {top.get("next_qty")} {material_unit}，'
            f'为当前需求最高的单品。',
            [f'近 14 日指数平滑加权均值 {top.get("avg7")} {material_unit}'
             f'（半衰期 3 天） × 趋势系数 {top.get("trend")}',
             f'模型自评稳定度 {conf}（含波动率惩罚）'],
            sample=max(1, round(conf * FULL_SAMPLE_PRODUCT)),
            full=FULL_SAMPLE_PRODUCT,
            caveat='单品样本天数少于 3 天时不输出预测'))

    # 4) 备货建议
    rep = stats.get('replenishment') or {}
    rep_top = (rep.get('items') or [None])[0]
    if rep.get('ok') and rep_top:
        out.append(build_advice(
            '备货建议',
            f'建议为「{rep_top.get("product")}」补 {rep_top.get("suggest")} {material_unit}原料/成品。',
            [f'计算口径：{rep_top.get("basis")}',
             f'服务水平 95%（安全库存 = 1.65 × 波动 σ={rep_top.get("sigma")}）'],
            sample=min(7, int(kpi.get('report_days') or 0)), full=7,
            caveat='未考虑在途订货与临期原料，实际下单请结合盘点结果'))

    # 5) 购物篮关联推荐
    basket = stats.get('basket') or {}
    pair = (basket.get('pairs') or [None])[0]
    if basket.get('ok') and pair:
        out.append(build_advice(
            '组合推荐',
            f'「{pair.get("a")}」与「{pair.get("b")}」存在正向关联（提升度 {pair.get("lift")}），'
            f'建议做搭售或相邻陈列。',
            [f'共现天数 {pair.get("days")} / 总营业日 {basket.get("days")}'
             f'（支持度 {pair.get("support")}）',
             f'置信度 {pair.get("confidence")} · 提升度 {pair.get("lift")}（>1 为正相关）'],
            sample=int(basket.get('days') or 0), full=FULL_SAMPLE_BASKET,
            caveat='相关性不等于因果，搭售效果需小范围试点验证'))

    # 6) 离群检测
    ano = stats.get('anomalies') or {}
    if ano.get('ok'):
        pts = ano.get('points') or []
        last = pts[-1]
        out.append(build_advice(
            '异常识别',
            f'检测到 {len(pts)} 个异常经营日，最新为 {last.get("date")} '
            f'（{last.get("metric")}偏离 {last.get("deviation_pct")}%）。',
            ['判据：z-score（|z|≥2）与 IQR（1.5×四分位距）双检测',
             f'该日实际 {last.get("value")} vs 期望 {last.get("expected")}'
             f'（{last.get("method")}）'],
            sample=int(ano.get('checked') or 0), full=FULL_SAMPLE_DAYS,
            caveat='异常不等于事故，需结合异常备注与当日实际情况复核'))

    # 7) 损耗预警
    alerts = stats.get('waste_alerts') or []
    if alerts:
        out.append(build_advice(
            '损耗预警',
            f'存在 {len(alerts)} 条原料损耗/异常记录，建议优先排查。',
            [f'最新：{alerts[0].get("date")} · {alerts[0].get("product")} · {alerts[0].get("note")}',
             f'判定规则：损耗率 > {industry_pack.pack_waste_rate_threshold() * 100:.0f}% 或存在异常备注'],
            sample=len(alerts), full=5,
            caveat='损耗率为按行填报口径估算，未区分原料品类'))

    return out


def summarize_confidence(items: list) -> dict:
    """对一组结论做整体可信度汇总（供页面顶部展示）。

    Args:
        items: `explain_business()` 的结论列表。

    Returns:
        dict: {'count','avg','min','level'}。
    """
    vals = [float(it.get('confidence') or 0) for it in (items or [])]
    if not vals:
        return {'count': 0, 'avg': 0.0, 'min': 0.0, 'level': level_of(0.0)}
    avg = sum(vals) / len(vals)
    return {'count': len(vals), 'avg': round(avg, 2),
            'min': round(min(vals), 2), 'level': level_of(avg)}
