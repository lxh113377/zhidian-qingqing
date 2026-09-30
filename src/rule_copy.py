"""规则版文案与经营分析模板（Q2：从 dify_client 纯搬运，零逻辑变更）

拆分动机
────────
`dify_client.py` 长期单文件承载「HTTP 客户端 / 判分引擎 / 规则文案 / 意图路由 /
业务门面」多类职责，任何 AI 改动都要动它，冲突与回归风险持续上升。本模块先做
**低风险切片**：收纳「经营数据小工具 + 规则版文案」两类纯函数（无网络、无 IO、
无 DB、无副作用），核心文件随之瘦身。

契约
────
1. 只依赖标准库与传入的 stats/daily dict，**不导入 dify_client**（杜绝循环导入）；
2. 迁移前后对同一输入必须输出**全等字符串**（L4 已断言报告关键段落）；
3. 命名沿用原私有名（`_` 前缀），由 dify_client 显式导入复用；
4. **数量单位随行业包**（P7-E3-B2b）：文案函数收 `unit` / `material_unit` 入参，
   默认值保持奶茶口径（`杯` / `份`）→ 不传参的旧调用零回归；dify_client 一律显式
   传当前包单位（`metrics.dashboard_labels()`），fruit 包不再输出「15 杯」。
"""
__all__ = [
    '_anomaly_text', '_basket_text', '_compare_text', '_forecast_text',
    '_product_lookup', '_replenish_text', '_rule_business_report', '_trend_desc',
    '_waste_products', '_weekday_text',
]

# 默认单位 = 奶茶口径（历史字面量，保证不传参时输出与改造前逐字一致）
DEFAULT_UNIT = '杯'
DEFAULT_MATERIAL_UNIT = '份'


# ─────────────────────────────────────────────────────────────
# 经营数据小工具（备货/损耗/经营分析共用，纯函数便于测试）
# ─────────────────────────────────────────────────────────────
def _trend_desc(daily: dict, unit: str = DEFAULT_UNIT) -> tuple:
    """近 7 日 vs 前 7 日销量趋势描述。

    Args:
        daily: analytics stats 的 daily 字段 {'dates','revenue','qty'}。
        unit: 数量单位（随行业包，默认奶茶「杯」）。

    Returns:
        tuple[str, str]: (可读的中文趋势文本, 'up'|'down'|'flat')。
    """
    qty = daily.get('qty') or []
    if len(qty) < 7:
        return '数据不足 7 日', 'flat'
    recent = qty[-7:]
    prev = qty[-14:-7] if len(qty) >= 14 else qty
    avg_recent = sum(recent) / len(recent)
    avg_prev = sum(prev) / len(prev)
    if avg_prev <= 0:
        return f'近 7 日日均销量 {avg_recent:.0f} {unit}', 'flat'
    ratio = avg_recent / avg_prev
    if ratio >= 1.05:
        trend = 'up'
        desc = f'近 7 日日均 {avg_recent:.0f} {unit}，较前 7 日增长 {((ratio - 1) * 100):.0f}%'
    elif ratio <= 0.95:
        trend = 'down'
        desc = f'近 7 日日均 {avg_recent:.0f} {unit}，较前 7 日回落 {((1 - ratio) * 100):.0f}%'
    else:
        trend = 'flat'
        desc = f'近 7 日日均 {avg_recent:.0f} {unit}，与前 7 日基本持平'
    return desc, trend


def _waste_products(alerts: list) -> list:
    """从损耗告警统计损耗高频产品名（按出现次数降序）。

    Args:
        alerts: analytics stats 的 waste_alerts 列表。

    Returns:
        list[str]: 产品名去重列表。
    """
    counter = {}
    for a in alerts or []:
        name = (a.get('product') or '').strip()
        if name:
            counter[name] = counter.get(name, 0) + 1
    return [p for p, _ in sorted(counter.items(), key=lambda x: -x[1])]


# ─────────────────────────────────────────────────────────────
# 规则版 AI 辅助：经营分析模板 + 助手意图路由（纯函数便于测试）
# ─────────────────────────────────────────────────────────────
def _rule_business_report(stats: dict, unit: str = DEFAULT_UNIT,
                          material_unit: str = DEFAULT_MATERIAL_UNIT) -> str:
    """规则版经营分析报告：全部数值来自真实 stats，标注演示模式。

    Args:
        stats: compute_dashboard_stats() 的完整统计结果。
        unit: 数量单位（随行业包，默认奶茶「杯」）。
        material_unit: 原料/备货计量单位（默认「份」）。

    Returns:
        str: 结构化报告正文。
    """
    kpi = stats.get('kpi', {})
    top = stats.get('top', {}) or {}
    pie = stats.get('pie', {}) or {}
    waste = stats.get('waste_alerts', [])
    has_data = stats.get('has_data', False)
    if not has_data:
        return ('【经营概况】暂无销售数据，待店员提交后生成经营分析。\n'
                '（规则版分析，配置 Dify 后由大模型生成深度洞察）')
    ratio_parts = []
    for n, v in zip((pie.get('names') or [])[:4], (pie.get('values') or [])[:4],
                    strict=False):  # 长度不同的空序列以短者为准
        ratio_parts.append(f"{n}({v}{unit})")
    trend, _ = _trend_desc(stats.get('daily', {}), unit)
    lines = [
        '【① 经营概况】',
        f"统计期总营收 ¥{kpi.get('total_revenue', 0):,.0f}，总销量 {kpi.get('total_qty', 0)} {unit}，"
        f"日均营收 ¥{kpi.get('avg_daily_revenue', 0):,.0f}，客单价 ¥{kpi.get('avg_per_unit', 0):,.0f}。",
        '【② 趋势与结构】',
        f"销量{trend}。热销 TOP：{'、'.join((top.get('names') or [])[:3])}；"
        f"销售结构：{'、'.join(ratio_parts) if ratio_parts else '暂无'}。",
    ]
    cmp_text = _compare_text(stats)
    if cmp_text:
        lines.append(cmp_text)
    wd_text = _weekday_text(stats, unit)
    if wd_text:
        lines.append(wd_text)
    if kpi.get('gross_margin') is not None:
        lines.append(f"综合毛利率 {kpi.get('gross_margin')}%（按已录成本口径，"
                     f"{kpi.get('margin_cost_items')}/{kpi.get('margin_total_items')} 条填报核入）。")
    lines.append('【③ 风险提示】')
    if waste:
        names = '、'.join(_waste_products(waste)[:3])
        lines.append(f"近期 {len(waste)} 条原料损耗预警，集中在 {names}，建议按《损耗控制》逐项排查。")
    else:
        lines.append('当前无原料损耗异常，原料管理与备货相对健康。')
    ano_text = _anomaly_text(stats)
    if ano_text:
        lines.append(ano_text + '建议核对当日异常备注与天气/活动因素。')
    lines.append('【④ 优化建议】')
    fc_text = _forecast_text(stats, unit)
    if fc_text:
        lines.append(fc_text + '，建议按此量级预备原料与人力。')
    rep_text = _replenish_text(stats, material_unit)
    if rep_text:
        lines.append(rep_text + '。')
    bk_text = _basket_text(stats)
    if bk_text:
        lines.append(bk_text)
    lines.append('建议保持热销品按 1.1-1.2 倍预备、易损鲜果少备勤补，并持续录入成本价以支撑毛利分析。'
                 '（规则版分析基于真实数据模板生成，配置 Dify 后由大模型输出深度洞察）')
    return '\n'.join(lines)


def _product_lookup(stats: dict, name: str) -> tuple:
    """按产品名查销量（pie）与营收（top）。缺失返回 (None, None)。"""
    pie = stats.get('pie') or {}
    top = stats.get('top') or {}
    pie_names, pie_vals = (pie.get('names') or []), (pie.get('values') or [])
    top_names, top_vals = (top.get('names') or []), (top.get('values') or [])
    qty = next((pie_vals[i] for i, n in enumerate(pie_names) if n == name), None)
    rev = next((top_vals[i] for i, n in enumerate(top_names) if n == name), None)
    return qty, rev


def _compare_text(stats: dict) -> str:
    """周期对比文案（compare 字段，数据不足返回空串）。"""
    cmp = stats.get('compare') or {}
    if not cmp.get('ok'):
        return ''
    parts = []
    if cmp.get('qty_pct') is not None:
        d = cmp['qty_pct']
        parts.append(f"销量{('上涨' if d >= 0 else '回落')}{abs(d):.1f}%")
    if cmp.get('revenue_pct') is not None:
        d = cmp['revenue_pct']
        parts.append(f"营收{('上涨' if d >= 0 else '回落')}{abs(d):.1f}%")
    return f"近 7 日较前 7 日：{'、'.join(parts)}。" if parts else ''


def _forecast_text(stats: dict, unit: str = DEFAULT_UNIT) -> str:
    """销量预测文案（forecast 字段，数据不足返回空串）。

    第六轮 A：stats 注入 forecast_accuracy（路由层回测指标）时，
    附上滚动回测的量化准确率——让「预测」本身可被验证。
    """
    fc = stats.get('forecast') or {}
    if not fc.get('ok') or fc.get('qty') is None:
        return ''
    line = f"预计明日销量约 {fc['qty']} {unit}"
    if fc.get('revenue'):
        line += f"、营收约 ¥{fc['revenue']:,.0f}"
    line += '（基于近 7 日趋势外推，仅供参考）'
    acc = stats.get('forecast_accuracy') or {}
    if acc.get('ok'):
        line += (f"。历史回测：近 {acc['count']} 次预测平均准确率 "
                 f"{acc['accuracy']}%（命中 {acc['hit_rate']}%）")
    return line


def _anomaly_text(stats: dict) -> str:
    """异常经营日文案（anomalies 字段，无异常返回空串）。"""
    ano = stats.get('anomalies') or {}
    if not ano.get('ok'):
        return ''
    pts = ano.get('points') or []
    last = pts[-1]
    return (f"AI 离群检测发现 {len(pts)} 个异常经营日，最新为 {last.get('date')} "
            f"（{last.get('metric')} {last.get('value')}，偏离常态 {last.get('deviation_pct')}%）。")


def _replenish_text(stats: dict, material_unit: str = DEFAULT_MATERIAL_UNIT) -> str:
    """备货建议文案（replenishment 字段，数据不足返回空串）。"""
    rep = stats.get('replenishment') or {}
    if not rep.get('ok'):
        return ''
    items = (rep.get('items') or [])[:2]
    parts = [f"{it.get('product')} 约 {it.get('suggest')} {material_unit}" for it in items]
    return '安全库存模型测算需补货：' + '、'.join(parts) + '（按 95% 服务水平）'


def _basket_text(stats: dict) -> str:
    """购物篮组合推荐文案（basket 字段，无正相关返回空串）。"""
    basket = stats.get('basket') or {}
    if not basket.get('ok'):
        return ''
    pair = (basket.get('pairs') or [None])[0]
    if not pair:
        return ''
    return (f"购物篮分析显示「{pair.get('a')}」与「{pair.get('b')}」正相关"
            f"（提升度 {pair.get('lift')}，共现 {pair.get('days')} 天），"
            f"建议做搭售或相邻陈列。")


def _weekday_text(stats: dict, unit: str = DEFAULT_UNIT) -> str:
    """工作日/周末销量规律文案（weekday 字段，数据不足返回空串）。"""
    wd = stats.get('weekday') or {}
    if not wd.get('ok'):
        return ''
    wa, we = wd.get('weekday_avg'), wd.get('weekend_avg')
    if wa is None or we is None:
        return ''
    if we > wa * 1.05:
        return f'周末日均销量 {we} {unit}高于工作日 {wa} {unit}，周末可适当加量备货。'
    if wa > we * 1.05:
        return f'工作日日均销量 {wa} {unit}高于周末 {we} {unit}，工作日下午高峰期需备足。'
    return f'工作日与周末日均销量接近（{wa}/{we} {unit}），备货节奏可保持平稳。'
