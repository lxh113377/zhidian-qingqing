"""数据分析模块 — Pandas 统计 + 看板数据聚合 + AI 经营智能算法层

从 daily_sales 拉取近 365 天数据，用 Pandas 做聚合，输出前端 ECharts 可直接消费的 dict。
所有金额单位：元（CNY）。

AI 经营智能（第四轮 D2）：在描述统计之上叠加四类推断能力——
  - 离群检测：z-score + IQR 双判据自动识别异常日（营收/销量/损耗率）
  - 分产品需求预测：近 7 日均 × 趋势系数 + 波动稳定性置信度
  - 安全库存备货：需求均值 + 服务水平 z 值 × 波动 σ − 现有剩余
  - 购物篮关联：同日共现组合的 support / confidence / lift

P2 角色化改造：原料/备注列不再硬编码——经 src.metrics.role_key() 按当前
行业包解析（奶茶包解析回既有列名，行为零变化）；包未声明 stock/waste
角色时损耗族能力（预警/归因/备货/派生卡）整体自动关闭，绝不伪造数据。
自定义指标（DailySales.metrics_json）在取数时展开为宽表列。
"""
import json
import logging
import math
from datetime import date, timedelta
from itertools import combinations
from statistics import mean, pstdev

import pandas as pd
from flask import g, has_app_context, has_request_context

from src import business_calendar as cal
from src import industry_pack, metrics, tenant
from src.models import DailySales, Product, active_pack_clause

logger = logging.getLogger(__name__)

# 原料损耗率预警阈值（F17：提取为模块常量，避免在函数内散落魔法值）
# 损耗率 = 损耗量 / (剩余量 + 损耗量)；超过该比例即触发预警（AC-OBS-08）
# P7-E3-B / M-4：默认值唯一真相源 = 内置兜底包 formula_params，此处为兼容别名。
WASTE_RATE_THRESHOLD = industry_pack.DEFAULT_WASTE_RATE_THRESHOLD


def _waste_threshold():
    """损耗率预警阈值：当前包 formula_params 声明优先，回退模块常量。

    奶茶包声明 0.15（与常量一致，行为零变化）；包未声明/非法值回退 0.15。
    P7-E3-B / M-4：解析下沉 `industry_pack.pack_waste_rate_threshold()`，
    使看板判定与 AI 规则文案共用同一入口（此前 AI 文案硬编码 15%）。
    """
    return industry_pack.pack_waste_rate_threshold()


def _resolve_waste_columns(df):
    """按当前包解析损耗族列（stock/waste/note）在 df 中的可用性。

    Returns:
        tuple: (stock_col, waste_col, note_col)；列不在 df 中按 None 处理。
    """
    def _col(role):
        name = metrics.role_key(role)
        return name if name and name in df.columns else None
    return _col(metrics.ROLE_STOCK), _col(metrics.ROLE_WASTE), _col(metrics.ROLE_NOTE)


def _waste_frame(df: pd.DataFrame, threshold: float):
    """一次构造损耗角色帧，供提醒与根因归因共同复用（PF-14）。"""
    stock_col, waste_col, note_col = _resolve_waste_columns(df)
    if not (stock_col and waste_col):
        return None
    remain_num = pd.to_numeric(df[stock_col], errors='coerce').fillna(0)
    waste_num = pd.to_numeric(df[waste_col], errors='coerce').fillna(0)
    total_raw = remain_num + waste_num
    loss_rate = (waste_num / total_raw).where(total_raw > 0, 0).fillna(0)
    if note_col:
        note_flag = df[note_col].notna() & (df[note_col].astype(str).str.strip() != '')
    else:
        note_flag = pd.Series(False, index=df.index)
    review_df = df.assign(_loss_rate=loss_rate)
    return {
        'stock_col': stock_col,
        'waste_col': waste_col,
        'note_col': note_col,
        'remain_num': remain_num,
        'waste_num': waste_num,
        'loss_rate': loss_rate,
        'note_flag': note_flag,
        'review_df': review_df,
        'flagged': review_df[(loss_rate > threshold) | note_flag],
    }


def _waste_review(df: pd.DataFrame, threshold: float) -> dict:
    """损耗族统计与派生指标（P2 角色化，供 _compute_stats 复用）。

    损耗率 = 损耗量 / (剩余量 + 损耗量)；超过阈值或存在异常备注均触发预警
    （AC-OBS-08）。列名经 metrics.role_key 按当前包解析（奶茶包解析回既有
    列名，行为零变化）；包未声明 stock/waste 角色时整组能力自动关闭。

    Returns:
        dict: {'ready', 'note_ready', 'alerts', 'total_remain', 'total_waste',
               'derived'}；derived 为按可用角色注入的派生指标卡列表。
    """
    stock_col, waste_col, note_col = _resolve_waste_columns(df)
    review = {'ready': bool(stock_col and waste_col), 'note_ready': bool(note_col),
              'alerts': [], 'alert_total': 0, 'total_remain': 0.0,
              'total_waste': 0.0, 'derived': []}
    if not review['ready']:
        return review

    frame = _waste_frame(df, threshold)
    remain_num = frame['remain_num']
    waste_num = frame['waste_num']
    note_col = frame['note_col']
    review['total_remain'] = float(remain_num.sum())
    review['total_waste'] = float(waste_num.sum())
    df = frame['review_df']
    waste_df = frame['flagged']
    if not waste_df.empty:
        g = waste_df.groupby(['record_date', 'product_name']).first().reset_index()
        for _, row in g.iterrows():
            lr = row['_loss_rate']
            if pd.notna(lr) and lr > threshold:
                note = f"原料损耗率 {lr * 100:.1f}% 超过预警阈值 {threshold * 100:.0f}%"
            elif note_col:
                note = (row[note_col] or "").strip() or "存在异常备注"
            else:
                note = "存在异常数据"
            review['alerts'].append({
                'date': row['record_date'].strftime('%Y-%m-%d'),
                'product': row['product_name'],
                'note': note,
            })
    review['alerts'].sort(key=lambda x: x['date'], reverse=True)
    review['alert_total'] = len(review['alerts'])
    review['alerts'] = review['alerts'][:MAX_WASTE_ALERTS]
    review['_frame'] = frame

    # 派生指标卡（requires 角色齐备才注入，缺角色自动关闭，不伪造）
    total_remain = review['total_remain']
    total_waste = review['total_waste']
    if (total_remain + total_waste) > 0:
        rate = total_waste / (total_remain + total_waste)
        review['derived'].append({
            'key': 'waste_rate', 'label': '损耗率', 'unit': '%',
            'value': round(rate * 100, 1), 'alert': rate > threshold,
            'desc': f'损耗÷（剩余+损耗），预警线 {threshold * 100:.0f}%',
        })
    if total_remain > 0:
        qty_col = metrics.role_key(metrics.ROLE_QUANTITY) or 'product_sales_qty'
        window_qty = int(df[qty_col].sum()) if qty_col in df.columns else 0
        if window_qty > 0:
            review['derived'].append({
                'key': 'sell_through', 'label': '动销率', 'unit': '%',
                'value': round(window_qty / (window_qty + total_remain) * 100, 1),
                'alert': False, 'desc': '销量÷（销量+剩余）',
            })
            review['derived'].append({
                'key': 'stock_turnover', 'label': '库存周转', 'unit': '次',
                'value': round(window_qty / total_remain, 2),
                'alert': False, 'desc': '窗口累计销量÷剩余量',
            })
    return review

# ── AI 经营智能常量 ──
ANOMALY_Z_THRESHOLD = 2.0      # z-score 离群阈值（约 95% 置信）
ANOMALY_IQR_FACTOR = 1.5       # IQR 离群系数（Tukey 栅栏）
SERVICE_LEVEL_Z = 1.65         # 95% 服务水平对应的安全库存 z 值
MIN_FORECAST_DAYS = 3          # 分产品预测所需的最少有量天数
MIN_PAIR_DAYS = 2              # 购物篮组合的最小共现天数
TOP_BASKET_PAIRS = 5           # 关联推荐返回的组合数上限
MAX_WASTE_ALERTS = 30          # PF-10：看板损耗提醒渲染上限
MAX_WASTE_CAUSES = 30          # PF-10：根因明细渲染上限


def _safe_load_metrics_json(text):
    """解析 DailySales.metrics_json（P2 自定义指标存储）。

    损坏/非对象一律按空 dict 处理 + WARNING（不向统计链路扩散异常）。
    """
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        logger.warning('[analytics] metrics_json 解析失败按空处理: %s', exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pack_record_filter():
    """当前激活包 + 当前门店的数据隔离过滤条件（多条件 AND）。

    激活包为默认包时放行 pack_id 为 NULL（P2 之前的历史数据）与默认包
    显式值；切换到其他包后仅统计该包数据，杜绝跨行业串数。
    P7-B2 起口径收敛到 `models.active_pack_clause()`（与产品清单同源）。
    PH2-2 起叠加门店维度（`tenant.store_scope_clause()`，默认门店放行 NULL
    历史数据）；无门店上下文（CLI/离线）时门店条件为零过滤。
    """
    return (active_pack_clause(DailySales.pack_id)
            & tenant.store_scope_clause(DailySales.store_id))


def get_sales_dataframe(days: int = 365):
    """从 daily_sales 拉取最近 days 天数据到 DataFrame（F20：避免全表扫描）。

    P2：自定义指标 metrics_json 展开为宽表列（键取自当前包自定义指标定义，
    定义外的键忽略）；空结果时同样补齐自定义列，保证下游角色解析稳定。
    P3：按当前激活包过滤 pack_id（NULL 视为默认包历史数据），切包即隔离。

    Args:
        days: 统计时间窗（天），默认 365。

    Returns:
        pd.DataFrame: 含 record_date(DateTime) / product_name / product_sales_qty /
        total_revenue / 自定义指标列 等列
    """
    custom_keys = [d['key'] for d in metrics.custom_metric_defs()]
    cutoff = cal.business_today() - timedelta(days=days)
    rows = DailySales.query.filter(
        DailySales.record_date >= cutoff, _pack_record_filter()).all()
    if not rows:
        return pd.DataFrame(columns=[
            'staff_id', 'record_date', 'product_name',
            'product_sales_qty', 'total_revenue', 'product_cost', 'cost_basis',
            'raw_material_remaining', 'raw_material_waste', 'abnormal_notes',
            'pack_id', *custom_keys,
        ])

    data = []
    for r in rows:
        row = {
            'staff_id': r.staff_id,
            'record_date': r.record_date,
            'product_name': r.product_name,
            'product_sales_qty': r.product_sales_qty,
            'total_revenue': float(r.total_revenue),
            # H60 成本快照随行带出（None = 提交当时该产品没有成本价）
            'product_cost': (float(r.product_cost) if r.product_cost is not None else None),
            'cost_basis': r.cost_basis or 'none',
            'raw_material_remaining': r.raw_material_remaining,
            'raw_material_waste': r.raw_material_waste,
            'abnormal_notes': r.abnormal_notes,
            'pack_id': r.pack_id,
        }
        # 自定义指标展开（白名单：仅当前包定义的键进入宽表）
        if custom_keys:
            payload = _safe_load_metrics_json(r.metrics_json)
            for key in custom_keys:
                row[key] = payload.get(key)
        data.append(row)

    df = pd.DataFrame(data)
    df['record_date'] = pd.to_datetime(df['record_date'])
    return df


def _empty_stats():
    """无数据时的占位统计，保证前端不报错。"""
    return {
        'has_data': False,
        'kpi': {
            'total_revenue': 0,
            'total_qty': 0,
            'report_days': 0,
            'staff_count': 0,
            'avg_daily_revenue': 0,
            'avg_per_unit': 0,
            'gross_margin': None,
            'margin_cost_items': 0,
            'margin_total_items': 0,
        },
        'daily': {'dates': [], 'revenue': [], 'qty': []},
        'pie': {'names': [], 'values': []},
        'top': {'names': [], 'values': []},
        'waste_alerts': [],
        'waste_alert_total': 0,
        # AI 深化：周期对比 / 销量预测 / 周末规律（数据不足时 ok=False，AI 侧不得编造）
        'compare': {'ok': False, 'revenue_pct': None, 'qty_pct': None},
        'forecast': {'ok': False, 'qty': None, 'revenue': None},
        'weekday': {'ok': False, 'weekday_avg': None, 'weekend_avg': None},
        # AI 经营智能（D2）：离群检测 / 分产品预测 / 备货建议 / 购物篮关联
        'anomalies': {'ok': False, 'points': [], 'checked': 0},
        'product_forecast': {'ok': False, 'items': []},
        'replenishment': {'ok': False, 'items': []},
        'basket': {'ok': False, 'pairs': [], 'days': 0},
        # 损耗根因归因（第六轮 C）：无数据时 ok=False，AI 侧不得编造
        'waste_causes': {'ok': False, 'items': [], 'summary': {}, 'total': 0},
        # P2 角色化：派生指标卡与能力开关（无数据/缺角色时为空/关闭）
        'derived_metrics': [],
        'capabilities': {'waste': False, 'note': False, 'custom_metrics': []},
    }


def _period_compare(daily: dict) -> dict:
    """近 7 日 vs 前 7 日营收/销量涨跌幅（AI 周期对比用）。

    Args:
        daily: {'revenue': [...], 'qty': [...]} 按日期升序的序列。

    Returns:
        dict: {'ok': bool, 'revenue_pct', 'qty_pct'}，不足 14 个交易日 ok=False。
    """
    revenue = list(daily.get('revenue') or [])
    qty = list(daily.get('qty') or [])
    if len(qty) < 14 or len(revenue) < 14:
        return {'ok': False, 'revenue_pct': None, 'qty_pct': None}

    def avg(seq, lo, hi):
        # 注意不能写 seq[-hi:-lo]：lo=0 时 -lo==0 导致切片恒为空
        seg = seq[len(seq) - hi:] if lo == 0 else seq[len(seq) - hi:len(seq) - lo]
        return sum(seg) / len(seg) if seg else 0

    rev_r, rev_p = avg(revenue, 0, 7), avg(revenue, 7, 14)
    qty_r, qty_p = avg(qty, 0, 7), avg(qty, 7, 14)
    return {
        'ok': True,
        'revenue_pct': round((rev_r / rev_p - 1) * 100, 1) if rev_p else None,
        'qty_pct': round((qty_r / qty_p - 1) * 100, 1) if qty_p else None,
    }


def _ewma(values: list, half_life: float = 3.0) -> float:
    """指数平滑加权均值：近期数据权重更高（权重 = 0.5^(age/半衰期)）。

    相比简单平均：简单平均把 14 天前的销量与昨天等权看待，对需求变化
    反应迟钝；指数平滑让最近一天权重 1.0、3 天前 0.5、6 天前 0.25，
    既保留长窗口的稳定性又快速响应趋势拐点。

    Args:
        values: 数值序列（升序时间序，末尾为最近）。
        half_life: 权重半衰期（天），越小越重视近期。

    Returns:
        float: 加权均值；空序列返回 0.0。
    """
    if not values:
        return 0.0
    m = len(values)
    num = 0.0
    den = 0.0
    for i, v in enumerate(values):
        w = 0.5 ** ((m - 1 - i) / max(half_life, 0.1))
        num += w * float(v or 0)
        den += w
    return num / den if den else 0.0


def _dow_factor_map(dates: list, qty: list) -> dict:
    """星期因子映射：每个星期几的历史均值 ÷ 全期均值。

    单人门店有明显的周内节律（周五/周末高、周一/周二低）。因子按星期
    分组统计相对倍率（限幅 0.6~1.8，防小样本极端值主导）。

    Args:
        dates: ISO 日期字符串序列（与 qty 等长、按日升序）。
        qty: 日销量序列。

    Returns:
        dict: {weekday(0=周一): 因子}；数据不足 14 天/日期不对齐返回空 dict。
    """
    n = len(qty)
    if n < 14 or len(dates) != n:
        return {}
    groups: dict = {}
    for d, v in zip(dates, qty, strict=False):
        try:
            dt = date.fromisoformat(str(d)[:10])
        except ValueError:
            continue
        groups.setdefault(dt.weekday(), []).append(float(v or 0))
    overall = sum(float(v or 0) for v in qty) / n
    if overall <= 0:
        return {}
    out = {}
    for dow, vals in groups.items():
        avg = sum(vals) / len(vals)
        if avg > 0:
            out[dow] = min(1.8, max(0.6, avg / overall))
    return out


def _forecast(daily: dict, avg_per_unit: float) -> dict:
    """明日销量/营收预测（AI 预测用，纯标准库）。

    第五轮升级为「季节调整 + 双因子」复合预测（可解释、可量化）：
      1. 星期周期因子：≥14 天时按星期几历史节律建因子表（限幅 0.6~1.8）；
      2. 季节调整：平滑与趋势均在「去周期序列」上计算——否则「近 3 日
         恰为工作日」会被趋势系数误判为下滑、与周期因子相互抵消；
      3. 指数平滑加权（半衰期 3 天，近期拐点响应更快）× 趋势系数
         （近3日均/前4日均，clamp 0.7~1.3），最后乘「明天的星期因子」。

    Args:
        daily: {'dates': [...](可选), 'qty': [...], 'revenue': [...]} 按日期升序。
        avg_per_unit: 客单价（可能为 0/空）。

    Returns:
        dict: {'ok': bool, 'qty', 'revenue', 'method'}；不足 3 日 ok=False。
        method 为可读的方法说明（评审/前端可解释性用），旧消费方不受影响。
    """
    qty = list(daily.get('qty') or [])
    dates = list(daily.get('dates') or [])
    n = len(qty)
    if n < 3:
        return {'ok': False, 'qty': None, 'revenue': None, 'method': '',
                'target_date': ''}
    # 星期因子表 + 去周期化序列（无日期/数据不足时因子恒 1，行为同旧版）
    dow_map = _dow_factor_map(dates, qty)
    if dow_map:
        def _factor_at(d):
            try:
                return dow_map.get(date.fromisoformat(str(d)[:10]).weekday(), 1.0)
            except ValueError:
                return 1.0
        adjusted = [float(v or 0) / _factor_at(d) for d, v in zip(dates, qty, strict=False)]
    else:
        adjusted = [float(v or 0) for v in qty]

    smooth = _ewma(adjusted[-14:])
    method = f'指数平滑加权（近 {min(14, n)} 日，半衰期 3 天）'
    ratio = 1.0
    if n >= 7:
        recent3 = sum(adjusted[-3:]) / 3
        prev4 = sum(adjusted[-7:-3]) / 4
        if prev4 > 0:
            ratio = min(1.3, max(0.7, recent3 / prev4))
            if ratio != 1.0:
                method += f' × 趋势系数 {ratio:.2f}（近3日均÷前4日均，限幅0.7~1.3）'
    est = smooth * ratio
    if dow_map:
        try:
            tomorrow = date.fromisoformat(str(dates[-1])[:10]) + timedelta(days=1)
        except ValueError:
            tomorrow = None
        f = dow_map.get(tomorrow.weekday(), 1.0) if tomorrow else 1.0
        if f != 1.0:
            wd = '一二三四五六日'[tomorrow.weekday()]
            method += (f' × 星期周期因子 {f:.2f}'
                       f'（明天周{wd}，按历史周内节律季节调整）')
        est *= f
    q = round(max(est, 0.0), 1)
    rev = round(q * (avg_per_unit or 0), 0) if avg_per_unit else None
    # 目标日期（预测的是哪一天，回测对账用；无日期输入时留空）
    target_date = ''
    if dates:
        try:
            last_day = date.fromisoformat(str(dates[-1])[:10])
            target_date = (last_day + timedelta(days=1)).isoformat()
        except ValueError:
            target_date = ''
    return {'ok': True, 'qty': q, 'revenue': rev, 'method': method,
            'target_date': target_date}


def _weekday_avg(df: pd.DataFrame) -> dict:
    """工作日（周一~周五） vs 周末（周六/日）日均销量对比（AI 备货规律用）。

    Args:
        df: 含 record_date / product_sales_qty 列的销售 DataFrame。

    Returns:
        dict: {'ok': bool, 'weekday_avg', 'weekend_avg'}；任一侧无数据 ok=False。
    """
    if df.empty:
        return {'ok': False, 'weekday_avg': None, 'weekend_avg': None}
    df2 = df.assign(_wd=df['record_date'].dt.weekday)
    weekday = df2[df2['_wd'] <= 4]
    weekend = df2[df2['_wd'] >= 5]

    def day_avg(sub):
        if sub.empty:
            return None
        perf = sub.groupby(sub['record_date'].dt.date)['product_sales_qty'].sum()
        return round(float(perf.mean()), 1) if len(perf) else None

    wa, we = day_avg(weekday), day_avg(weekend)
    if wa is None or we is None:
        return {'ok': False, 'weekday_avg': wa, 'weekend_avg': we}
    return {'ok': True, 'weekday_avg': wa, 'weekend_avg': we}


def _compute_gross_margin(df: pd.DataFrame):
    """毛利率（可选项）：营收 − 成本×销量，仅按有成本的产品计。

    成本取值的优先级（H60，第 26 轮）
    --------------------------------
    1. **行内快照优先**：`daily_sales.product_cost` 在 `cost_basis` 为 snapshot/backfill
       时就是这一行自己的成本，毛利率与产品目录的当前状态**彻底解耦**；
    2. 提交当时该产品没有成本价（`cost_basis='none'`）→ 才回退到目录当前值，
       让老板「事后补录成本 → 看板出毛利率」这条既有行为不丢（冒烟用例依赖它）；
       这类行的条数单独回报，在看板上如实标注「按当前成本补算」，不混进快照口径。

    为什么必须有第 1 档：此前只有第 2 档，实测「删一个产品 / 改一次名 / 改一次成本价」
    会让**历史**毛利率静默移动 -0.2255 / +1.5072 / +6.4935 个百分点，而营收分文未动。
    对标口径：Odoo/ERPNext 的成本随单据走（transaction-time），目录是主数据不是历史。

    无任何成本行 → gross_margin=None（前端显示「待录入」）。
    仅在应用上下文内查成本表（支持 L4 测试「传 df 脱离 DB」的调用方式）。

    Returns:
        (gross_margin, margin_cost_items, margin_total_items, margin_snapshot_items)
    """
    snapshot_costed = 0
    if 'product_cost' in df.columns:
        basis = (df['cost_basis'] if 'cost_basis' in df.columns
                 else pd.Series(['none'] * len(df), index=df.index))
        snap_mask = df['product_cost'].notna() & basis.isin(['snapshot', 'backfill'])
        snapshot_costed = int(snap_mask.sum())
    else:
        snap_mask = pd.Series([False] * len(df), index=df.index)

    cost_map = {}
    if has_app_context() and len(df) and snapshot_costed < len(df):
        # P7-E3 / PF-5：回退档的成本价按当前激活包取（此前 Product.query.all() 跨包串价）。
        # 只取两列，避免整行 ORM 物化。
        rows = Product.query.filter(
            active_pack_clause(Product.pack_id)
            & tenant.store_scope_clause(Product.store_id)
        ).with_entities(Product.product_name, Product.cost_price).all()
        cost_map = {name: cost for name, cost in rows if cost is not None}

    df = df.assign(_cost=df['product_cost'] if 'product_cost' in df.columns
                   else pd.Series([None] * len(df), index=df.index))
    if cost_map:
        df['_cost'] = df['_cost'].where(snap_mask, df['product_name'].map(cost_map))
    costed = df[df['_cost'].notna()]
    if costed.empty:
        return None, 0, len(df), snapshot_costed
    total_cost = float((costed['_cost'] * costed['product_sales_qty']).sum())
    margin_revenue = float(costed['total_revenue'].sum())
    gross_margin = (margin_revenue - total_cost) / margin_revenue * 100 if margin_revenue else 0.0
    return gross_margin, len(costed), len(df), snapshot_costed


def _z_outliers(values: list, labels: list, z_threshold: float = ANOMALY_Z_THRESHOLD):
    """z-score 离群点（样本 <3 或 σ=0 时无离群）。

    Args:
        values: 数值序列。
        labels: 与 values 等长的标签序列（日期）。
        z_threshold: |z| 超过该值判为离群。

    Returns:
        list[dict]: [{'label','value','expected','z'}]。
    """
    n = len(values)
    if n < 3:
        return []
    mu = mean(values)
    sd = pstdev(values)
    if sd <= 0:
        return []
    out = []
    for label, val in zip(labels, values, strict=False):
        z = (val - mu) / sd
        if abs(z) >= z_threshold:
            out.append({'label': label, 'value': round(float(val), 2),
                        'expected': round(float(mu), 2), 'z': round(float(z), 2)})
    return out


def _iqr_outliers(values: list, labels: list, factor: float = ANOMALY_IQR_FACTOR):
    """IQR（Tukey 栅栏）离群点，对极端值不敏感，用作 z-score 的互补判据。

    Args:
        values: 数值序列。
        labels: 与 values 等长的标签序列。
        factor: 栅栏系数（默认 1.5）。

    Returns:
        list[dict]: [{'label','value','expected','z'}]（z 字段复用于偏离倍数）。
    """
    ordered = sorted(values)
    n = len(ordered)
    if n < 4:
        return []

    def quantile(sorted_vals, q):
        pos = (len(sorted_vals) - 1) * q
        lo = math.floor(pos)
        hi = math.ceil(pos)
        if lo == hi:
            return float(sorted_vals[lo])
        return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo))

    q1, q3 = quantile(ordered, 0.25), quantile(ordered, 0.75)
    iqr = q3 - q1
    if iqr <= 0:
        return []
    lo_f, hi_f = q1 - factor * iqr, q3 + factor * iqr
    out = []
    for label, val in zip(labels, values, strict=False):
        if val <= lo_f or val >= hi_f:
            out.append({'label': label, 'value': round(float(val), 2),
                        'expected': round(float((q1 + q3) / 2), 2), 'z': None})
    return out


def detect_anomalies(daily: dict) -> dict:
    """经营序列离群检测（AI 自动发现异常日，而非只看固定阈值）。

    双判据互补：z-score 抓均值偏离（对极端值敏感），IQR 抓分布栅栏外
    （对极端值稳健）；任一命中即记为异常，避免单一判据漏报。

    Args:
        daily: {'dates': [...], 'revenue': [...], 'qty': [...]} 按日期升序。

    Returns:
        dict: {'ok','points':[{'date','metric','value','expected',
              'deviation_pct','method'}], 'checked'}。
    """
    dates = list(daily.get('dates') or [])
    series_pairs = (('营收', daily.get('revenue') or []), ('销量', daily.get('qty') or []))
    points = []
    checked = 0
    for name, raw in series_pairs:
        series = [float(v) for v in raw if v is not None]
        if len(series) < 4 or len(series) != len(dates):
            continue
        checked += len(series)
        seen = {}
        for hit in _z_outliers(series, dates):
            seen[hit['label']] = (hit, 'z-score')
        for hit in _iqr_outliers(series, dates):
            # z-score 已命中则标注双判据，提高可信度
            if hit['label'] in seen:
                prev, _ = seen[hit['label']]
                seen[hit['label']] = (prev, 'z-score+IQR')
            else:
                seen[hit['label']] = (hit, 'IQR')
        for label, (hit, method) in seen.items():
            expected = hit['expected']
            dev = ((hit['value'] / expected - 1) * 100) if expected else 0
            points.append({
                'date': label,
                'metric': name,
                'value': hit['value'],
                'expected': expected,
                'deviation_pct': round(dev, 1),
                'method': method,
            })
    points.sort(key=lambda p: (p['date'], p['metric']))
    return {'ok': bool(points), 'points': points, 'checked': checked}


def product_forecast(df: pd.DataFrame) -> dict:
    """分产品需求预测：指数平滑加权均值 × 趋势系数，并给出置信度。

    第五轮升级：均值由「近 7 日简单平均」改为「近 14 日指数平滑加权」
    （半衰期 3 天），近期需求拐点响应更快。
    置信度 = 数据天数充分性 × 波动稳定性（变异系数越小越可信），
    用于 AI 侧明示「这个预测有多可靠」（D3 可解释性输入）。

    Args:
        df: 含 record_date / product_name / product_sales_qty 的 DataFrame。

    Returns:
        dict: {'ok','items':[{'product','avg7','trend','next_qty','confidence'}]}。
    """
    if df is None or df.empty:
        return {'ok': False, 'items': []}
    items = []
    for name, grp in df.groupby('product_name'):
        series = grp.groupby(grp['record_date'].dt.date)['product_sales_qty'].sum().sort_index()
        vals = [float(v) for v in series.tolist()]
        if len(vals) < MIN_FORECAST_DAYS:
            continue
        # 第五轮升级：avg7 由简单平均改为指数平滑加权（半衰期 3 天），
        # 近期需求变化响应更快；字段名保留 avg7 兼容旧消费方。
        avg7 = _ewma(vals[-14:])
        trend = 1.0
        if len(vals) >= 7:
            recent3 = sum(vals[-3:]) / 3
            prev4 = sum(vals[-7:-3]) / 4
            if prev4 > 0:
                trend = min(1.3, max(0.7, recent3 / prev4))
        mu = mean(vals)
        sd = pstdev(vals) if len(vals) > 1 else 0.0
        cv = (sd / mu) if mu else 0.0
        stability = max(0.0, 1 - min(cv, 1.0) * 0.5)
        adequacy = min(1.0, len(vals) / 7.0)
        items.append({
            'product': name,
            'avg7': round(avg7, 1),
            'trend': round(trend, 2),
            'next_qty': round(avg7 * trend, 1),
            'confidence': round(adequacy * stability, 2),
        })
    items.sort(key=lambda x: -x['next_qty'])
    return {'ok': bool(items), 'items': items}


def replenishment_advice(df: pd.DataFrame) -> dict:
    """安全库存备货建议：需求 + 服务水平 z×σ − 现有剩余。

    P2 角色化：剩余量列经 metrics.role_key 按当前包解析；包未声明
    stock 角色时能力自动关闭（ok=False，AI 侧不得编造）。

    Args:
        df: 含 record_date / product_name / product_sales_qty / 剩余量列 的 DataFrame。

    Returns:
        dict: {'ok','items':[{'product','demand','sigma','safety_stock',
              'remaining','suggest','basis'}]}。
    """
    if df is None or df.empty:
        return {'ok': False, 'items': []}
    stock_col = metrics.role_key(metrics.ROLE_STOCK)
    if not stock_col or stock_col not in df.columns:
        return {'ok': False, 'items': []}
    items = []
    for name, grp in df.groupby('product_name'):
        series = grp.groupby(grp['record_date'].dt.date)['product_sales_qty'].sum().sort_index()
        vals = [float(v) for v in series.tolist()]
        if len(vals) < MIN_FORECAST_DAYS:
            continue
        demand = sum(vals[-7:]) / min(7, len(vals))
        sigma = pstdev(vals) if len(vals) > 1 else 0.0
        safety = SERVICE_LEVEL_Z * sigma
        # 现有剩余：取该产品最近一条非空的剩余量（Text 字段，容忍非数字）
        remain_series = pd.to_numeric(grp[stock_col], errors='coerce').dropna()
        remaining = float(remain_series.iloc[-1]) if len(remain_series) else None
        need = demand + safety
        suggest = max(0.0, need - (remaining or 0.0))
        items.append({
            'product': name,
            'demand': round(demand, 1),
            'sigma': round(sigma, 2),
            'safety_stock': round(safety, 1),
            'remaining': (round(remaining, 1) if remaining is not None else None),
            'suggest': round(suggest, 1),
            'basis': (f'日均需求 {demand:.1f} + 安全库存 {safety:.1f}'
                      + (f' − 现存 {remaining:.1f}' if remaining is not None else '（无现存数据）')),
        })
    items.sort(key=lambda x: -x['suggest'])
    return {'ok': bool(items), 'items': items}


def basket_association(df: pd.DataFrame) -> dict:
    """购物篮关联分析：同日共现组合的 support / confidence / lift。

    lift > 1 表示两产品正相关（搭售潜力）；按 lift 降序取 Top N。

    Args:
        df: 含 record_date / product_name / product_sales_qty 的 DataFrame。

    Returns:
        dict: {'ok','pairs':[{'a','b','days','support','confidence','lift'}],'days'}。
    """
    if df is None or df.empty:
        return {'ok': False, 'pairs': [], 'days': 0}
    active = df[pd.to_numeric(df['product_sales_qty'], errors='coerce').fillna(0) > 0]
    if active.empty:
        return {'ok': False, 'pairs': [], 'days': 0}
    days = (active['record_date'].dt.date
            if hasattr(active['record_date'], 'dt') else active['record_date'])
    by_date = (active.assign(_day=days)
               .groupby('_day')['product_name']
               .apply(lambda s: set(s.dropna().astype(str))))

    total_days = len(by_date)
    if total_days < 2:
        return {'ok': False, 'pairs': [], 'days': total_days}

    single = {}
    pair = {}
    for products in by_date:
        names = sorted(products)
        for n in names:
            single[n] = single.get(n, 0) + 1
        for a, b in combinations(names, 2):
            pair[(a, b)] = pair.get((a, b), 0) + 1

    pairs = []
    for (a, b), cnt in pair.items():
        if cnt < MIN_PAIR_DAYS:
            continue
        support = cnt / total_days
        conf_a = cnt / single[a] if single.get(a) else 0.0
        conf_b = cnt / single[b] if single.get(b) else 0.0
        # 以提升度较高的方向作为推荐方向（a→b 或 b→a）
        if conf_a >= conf_b:
            src, dst, conf = a, b, conf_a
        else:
            src, dst, conf = b, a, conf_b
        support_dst = single.get(dst, 0) / total_days
        lift = (conf / support_dst) if support_dst else 0.0
        if lift <= 1.0:  # 无正相关不推荐
            continue
        pairs.append({'a': src, 'b': dst, 'days': cnt,
                      'support': round(support, 3),
                      'confidence': round(conf, 3),
                      'lift': round(lift, 2)})
    pairs.sort(key=lambda x: (-x['lift'], -x['days']))
    return {'ok': bool(pairs), 'pairs': pairs[:TOP_BASKET_PAIRS], 'days': total_days}


# ── 损耗根因归因（第六轮 C）：备注文本分类 + 损耗率模式推断 ──
# 判据优先级：备注明确词 > 高损耗率模式 > 待排查；置信度显式标注，
# 符合「AI 只输出参考建议」红线（归因是参考，不是定责）。
WASTE_CAUSE_KEYWORDS = (
    ('storage', '储存不当', ('变质', '异味', '臭', '胀包', '过期',
                             '坏了', '馊', '发酸', '腐坏')),
    ('operation', '操作浪费', ('打翻', '洒', '做错', '倒掉', '失手',
                               '操作失误', '碰倒')),
)
# 损耗率超过该值视为「备货过多」模式（远超 15% 预警线，通常是备量问题）
WASTE_OVERSTOCK_RATE = 0.30

WASTE_CAUSE_LABELS = {
    'storage': '储存不当',
    'operation': '操作浪费',
    'overstock': '备货过多',
    'unknown': '待排查',
}

WASTE_CAUSE_ADVICE = {
    'storage': '按效期管理整改：易损鲜果/鲜奶少备勤补，冷藏原料每日盘点效期，'
               '变色异味胀包立即停用上报。',
    'operation': '规范操作流程：开封原料标注时间，出品按标准配比，减少制作与搬运环节洒漏。',
    'overstock': '按近 7 日销量动态备货：热销品 1.1-1.2 倍、易损品少备勤补，避免一次囤多。',
    'unknown': '建议连续观察 2-3 日损耗构成，定位属于备货、储存还是操作环节问题。',
}


def waste_root_cause(df: pd.DataFrame, frame: dict | None = None) -> dict:
    """损耗根因归因：把「损耗率超标/有异常备注」的记录归类到可行动的根因。

    规则（顺序即优先级，首条命中即归因）：
      1. 原始备注命中「储存不当」关键词（变质/异味/胀包…）→ storage（置信 中）；
      2. 原始备注命中「操作浪费」关键词（打翻/洒/倒掉…）→ operation（置信 中）；
      3. 无备注但损耗率 > 30%（远超预警线）→ 备货过多模式推断（置信 中）；
      4. 其余 → 待排查（置信 低，绝不拍脑袋下结论）。

    Args:
        df: `get_sales_dataframe()` 的 DataFrame（含剩余量/损耗量/备注列，
            列名经当前包角色解析）。

    Returns:
        dict: {'ok','items':[{'date','product','rate','note','cause',
              'cause_label','confidence','advice'}],
              'summary':{'labels':{标签:次数},'main':主归因标签|None}}。
    """
    if df is None or df.empty:
        return {'ok': False, 'items': [], 'summary': {}}
    frame = frame or _waste_frame(df, _waste_threshold())
    if frame is None:
        # 包未声明 stock/waste 角色 → 归因能力自动关闭（不伪造）
        return {'ok': False, 'items': [], 'summary': {}, 'total': 0}
    note_col = frame['note_col']
    loss_rate = frame['loss_rate']
    flagged = frame['flagged']
    if flagged.empty:
        return {'ok': False, 'items': [], 'summary': {}, 'total': 0}

    items = []
    label_count: dict = {}
    rows = flagged.assign(_lr=loss_rate[flagged.index])
    for _, row in rows.groupby(['record_date', 'product_name']).first().reset_index().iterrows():
        # NaN（无备注）在 pandas 中是 float，须先转空串再 strip
        raw_note = row[note_col] if note_col else ''
        note = ('' if pd.isna(raw_note) else str(raw_note)).strip()
        rate = float(row['_lr'])
        cause = 'unknown'
        for key, _label, words in WASTE_CAUSE_KEYWORDS:
            if any(w in note for w in words):
                cause = key
                break
        else:
            if rate > WASTE_OVERSTOCK_RATE:
                cause = 'overstock'
        label = WASTE_CAUSE_LABELS[cause]
        items.append({
            'date': row['record_date'].strftime('%Y-%m-%d'),
            'product': row['product_name'],
            'rate': round(rate * 100, 1),
            'note': note or '（无备注，按损耗率模式推断）',
            'cause': cause,
            'cause_label': label,
            'confidence': '低' if cause == 'unknown' else '中',
            'advice': WASTE_CAUSE_ADVICE[cause],
        })
        label_count[label] = label_count.get(label, 0) + 1
    items.sort(key=lambda x: (x['date'], x['rate']), reverse=True)
    total = len(items)
    items = items[:MAX_WASTE_CAUSES]
    main = max(label_count.items(), key=lambda x: x[1])[0] if label_count else None
    return {'ok': True, 'items': items,
            'summary': {'labels': label_count, 'main': main},
            'total': total}


# 请求级缓存键（PF1）：同一请求内多处调用复用同一份 stats，避免看板 + AI 建议 +
# 助手在单次请求里重复做「365 天 Pandas 全量重算」。仅缓存「从 DB 自动拉取」的路径，
# 显式传 df 的测试路径不缓存（保持纯函数语义），且**不跨请求**——保持「提交后看板
# 立即可见」的演示语义与既有冒烟/L4 断言零影响。
_REQUEST_CACHE_ATTR = '_ican_dashboard_stats_cache'


def _cache_get(days: int):
    """读取请求级 stats 缓存（非请求上下文或未命中返回 None）。

    仅在同一 HTTP 请求内生效：`g` 实际绑定的是 **app 上下文**，而 app 上下文
    可比单个请求更长（测试里的 `with app.app_context()` 会跨多次逻辑操作复用），
    若不额外要求 request 上下文，就会读到陈旧 stats——实测曾令冒烟「毛利率」
    断言失败（先写入成本价再重算，仍拿到缓存里的旧 None）。故以 request 上下文为准。
    """
    if not has_request_context():
        return None
    cache = getattr(g, _REQUEST_CACHE_ATTR, None) or {}
    return cache.get(days)


def _cache_set(days: int, stats: dict):
    """写入请求级 stats 缓存（非请求上下文时静默跳过）。"""
    if not has_request_context():
        return
    cache = getattr(g, _REQUEST_CACHE_ATTR, None)
    if cache is None:
        cache = {}
        setattr(g, _REQUEST_CACHE_ATTR, cache)
    cache[days] = stats


def compute_dashboard_stats(df: pd.DataFrame = None, days: int = 365):
    """计算看板所需的全部统计（带请求级缓存）。

    Args:
        df: 可选，预拉取的 sales DataFrame；为 None 时自动拉取。
        days: 统计时间窗（天），当 df 为 None 时决定拉取范围。

    Returns:
        dict: 含 kpi / daily / pie / top / waste_alerts 的看板数据。

    Note:
        df 为 None 时走「本请求内缓存」：同一 Flask 请求里看板、AI 建议、AI 助手
        多次调用只算一次（PF1）；不跨请求，保持提交后看板立即可见的演示语义。
    """
    if df is not None:
        return _compute_stats(df)
    cached = _cache_get(days)
    if cached is not None:
        return cached
    stats = _compute_stats(get_sales_dataframe(days))
    _cache_set(days, stats)
    return stats


def _compute_stats(df: pd.DataFrame):
    """纯计算入口（无缓存、无拉取），df 必传；供上述包装层复用。"""
    if df.empty:
        return _empty_stats()

    # ── KPI ──
    total_revenue = float(df['total_revenue'].sum())
    total_qty = int(df['product_sales_qty'].sum())
    report_days = int(df['record_date'].dt.date.nunique())
    staff_count = int(df['staff_id'].nunique())
    avg_daily_revenue = total_revenue / report_days if report_days else 0

    # ── 日销量折线（按日期聚合）──
    daily = df.groupby(df['record_date'].dt.date).agg(
        revenue=('total_revenue', 'sum'),
        qty=('product_sales_qty', 'sum'),
    ).sort_index()
    daily_dates = [d.strftime('%m-%d') for d in daily.index]
    daily_revenue = [round(float(v), 2) for v in daily['revenue']]
    daily_qty = [int(v) for v in daily['qty']]

    # ── 产品占比饼图（按销量）──
    product_qty = df.groupby('product_name')['product_sales_qty'].sum().sort_values(ascending=False)
    pie_names = list(product_qty.index)
    pie_values = [int(v) for v in product_qty.values]

    # ── 热销排行（按营收）──
    product_rev = df.groupby('product_name')['total_revenue'].sum().sort_values(ascending=False)
    top_names = list(product_rev.index)
    top_values = [round(float(v), 2) for v in product_rev.values]

    # ── 原料损耗异常提醒（按损耗率阈值判定，AC-OBS-08；P2 角色化）──
    # 预警/窗口总量/派生卡统一由 _waste_review 产出（角色解析 + 能力关闭）。
    threshold = _waste_threshold()
    waste_review = _waste_review(df, threshold)
    waste_alerts = waste_review['alerts']
    waste_frame = waste_review.pop('_frame', None)

    # ── 毛利率（可选项）：营收 − 成本价×销量，仅按已填成本的产品计 ──
    (gross_margin, margin_cost_items, margin_total_items,
     margin_snapshot_items) = _compute_gross_margin(df)

    # ── AI 深化数据：周期对比 / 销量预测 / 周末规律（数据不足 ok=False，AI 侧不得编造）──
    iso_dates = [d.strftime('%Y-%m-%d') for d in daily.index]
    compare = _period_compare({'revenue': daily_revenue, 'qty': daily_qty})
    # 第六轮 A 修复：此前调用漏传 dates，第五轮的星期周期因子在主链路
    # 实际未生效（dow_map 因日期缺失恒为空，因子恒 1）；传入 ISO 日期后
    # 季节调整真正生效，target_date 供回测对账。
    fc = _forecast({'dates': iso_dates, 'qty': daily_qty},
                   (total_revenue / total_qty) if total_qty else 0)
    weekday = _weekday_avg(df)

    # ── AI 经营智能（D2）：离群检测 / 分产品预测 / 备货建议 / 购物篮关联 ──
    anomalies = detect_anomalies({'dates': iso_dates,
                                  'revenue': daily_revenue,
                                  'qty': daily_qty})
    pfc = product_forecast(df)
    replen = replenishment_advice(df)
    basket = basket_association(df)

    # ── 损耗根因归因（第六轮 C）：按原始备注文本 + 损耗率模式推断 ──
    waste_causes = waste_root_cause(df, frame=waste_frame)
    cause_map = {(it['date'], it['product']): it
                 for it in waste_causes.get('items') or []}
    for a in waste_alerts:
        hit = cause_map.get((a['date'], a['product']))
        if hit:
            a['cause'] = hit['cause']
            a['cause_label'] = hit['cause_label']

    # ── 派生指标与能力开关（P2）：缺角色自动关闭（不伪造）──
    derived_metrics = waste_review['derived']
    capabilities = {
        'waste': waste_review['ready'],
        'note': waste_review['note_ready'],
        'custom_metrics': [d['label'] for d in metrics.custom_metric_defs()],
    }

    return {
        'has_data': True,
        'kpi': {
            'total_revenue': round(total_revenue, 2),
            'total_qty': total_qty,
            'report_days': report_days,
            'staff_count': staff_count,
            'avg_daily_revenue': round(avg_daily_revenue, 2),
            'avg_per_unit': round(total_revenue / total_qty, 2) if total_qty else 0,
            'gross_margin': round(gross_margin, 1) if gross_margin is not None else None,
            'margin_cost_items': margin_cost_items,
            'margin_total_items': margin_total_items,
        },
        'daily': {'dates': daily_dates, 'revenue': daily_revenue, 'qty': daily_qty},
        'daily_iso': iso_dates,
        'pie': {'names': pie_names, 'values': pie_values},
        'top': {'names': top_names, 'values': top_values},
        'waste_alerts': waste_alerts,
        'waste_alert_total': waste_review.get('alert_total', len(waste_alerts)),
        'waste_causes': waste_causes,
        'compare': compare,
        'forecast': fc,
        'weekday': weekday,
        'anomalies': anomalies,
        'product_forecast': pfc,
        'replenishment': replen,
        'basket': basket,
        'derived_metrics': derived_metrics,
        'capabilities': capabilities,
    }
