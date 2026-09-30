"""指标角色绑定与派生公式模板（P2）—— 自由填报指标的唯一计算口径。

职责边界（与 industry_pack / analytics 的分工）：
  - industry_pack：指标定义的「加载与规范化」（pack.json metrics 段，三级回退）；
  - metrics（本模块）：指标定义的「消费口径」——角色 → 实际列名解析、
    派生公式模板注册与可用性判定（requires 角色声明，缺角色自动关闭）、
    自定义指标入参解析（复用 validators）；
  - analytics：用本模块的解析结果做统计聚合（不直接理解行业包结构）。

设计约定（与既有模块一致）：
  1. 纯函数 + 只读包访问，不触碰网络与 DB（毛利率的产品成本证据由调用方
     以 has_product_cost 显式传入，保持本模块可脱离 DB 单测）；
  2. 包缺角色 → 相关能力自动关闭（返回不可用标记），绝不伪造数据；
  3. 自定义指标格式非法抛 validators.ParseError，由路由层转 400；
     范围/必填问题返回错误文案，由路由层 flash 拦截（与既有 F13/F14 分工一致）。
"""
from src import industry_pack
from src.validators import check_range, parse_float

# ── 业务角色（骨架角色必有，扩展角色按包声明）──
ROLE_DIMENSION = 'dimension'    # 主记录维度（产品/服务项目/SKU）
ROLE_QUANTITY = 'quantity'      # 销量
ROLE_AMOUNT = 'amount'          # 金额
ROLE_STOCK = 'stock'            # 库存/剩余
ROLE_WASTE = 'waste'            # 损耗/报废
ROLE_COST = 'cost'              # 成本（金额类，派生毛利率用）
ROLE_NOTE = 'note'              # 备注（文本）
ROLE_OTHER = 'other'            # 其他自定义

# 骨架角色：对应 DailySales 既有 typed 列，任何包都应声明（pack.json 已保证）
SKELETON_ROLES = (ROLE_DIMENSION, ROLE_QUANTITY, ROLE_AMOUNT)

# 数值型取值类型（text 之外的都按数值解析）
NUMERIC_TYPES = ('number', 'integer')

# 自定义指标入参上限（与 validators 既有上限同量级，防误填污染统计）
MAX_METRIC_VALUE = 100000.0     # 自定义数值指标上限
MAX_TEXT_LEN = 500              # 自定义文本指标长度上限

# DailySales 既有 typed 列（legacy_column 映射目标的全集；
# 不在此列的指标键视为「自定义指标」，存 DailySales.metrics_json）
LEGACY_COLUMNS = (
    'product_name',
    'product_sales_qty',
    'total_revenue',
    'raw_material_remaining',
    'raw_material_waste',
    'abnormal_notes',
)


# ─────────────────────────── 指标定义访问 ───────────────────────────
def metric_defs():
    """当前包指标定义列表（industry_pack.pack_metrics() 直通，已规范化）。"""
    return industry_pack.pack_metrics()


def metric_by_key(key):
    """按填报键取指标定义；不存在返回 None。"""
    for d in metric_defs():
        if d['key'] == key:
            return d
    return None


def metric_by_role(role):
    """按业务角色取指标定义；包未声明该角色返回 None。"""
    for d in metric_defs():
        if d['role'] == role:
            return d
    return None


def role_key(role):
    """角色 → 实际填报键（legacy_column 优先，其次自定义 key）。

    奶茶包解析回既有列名（raw_material_remaining 等），保证零回归；
    自定义包返回 metrics_json 键。
    """
    d = metric_by_role(role)
    if d is None:
        return None
    return d['legacy_column'] or d['key']


def has_roles(*roles):
    """当前包是否声明了全部指定角色。"""
    declared = {d['role'] for d in metric_defs()}
    return all(r in declared for r in roles)


def custom_metric_defs():
    """自定义指标定义（解析目标不是既有 typed 列的部分）。

    自定义指标按包启停：定义存在 → 表单渲染 + 入库 metrics_json + 宽表展开；
    定义不存在 → 相关字段整组消失（天然随包切换）。
    """
    return [d for d in metric_defs()
            if (d['legacy_column'] or d['key']) not in LEGACY_COLUMNS]


# ─────────────────────────── 派生公式模板 ───────────────────────────
# 每个模板声明 requires 角色：包缺任一角色 → 该公式自动关闭（不伪造数据）。
# expose: 'derived' = 由 analytics 注入 stats['derived_metrics'] 出卡；
#         'kpi'     = 复用既有 KPI 口径（不重复出卡，注册只为口径文档化）。
def _f_waste_rate(stock, waste):
    total = stock + waste
    return (waste / total) if total > 0 else None


def _f_gross_margin(amount, cost, quantity):
    if not amount or amount <= 0:
        return None
    return (amount - cost * quantity) / amount


def _f_avg_ticket(amount, quantity):
    return (amount / quantity) if quantity else None


def _f_sell_through(quantity, stock):
    total = quantity + stock
    return (quantity / total) if total > 0 else None


def _f_stock_turnover(quantity, stock):
    return (quantity / stock) if stock else None


FORMULA_TEMPLATES = (
    {
        'key': 'waste_rate', 'label': '损耗率', 'unit': '%',
        'requires': (ROLE_STOCK, ROLE_WASTE), 'expose': 'derived',
        'describe': '损耗率 = 损耗量 ÷（剩余量 + 损耗量）',
    },
    {
        'key': 'gross_margin', 'label': '毛利率', 'unit': '%',
        'requires': (ROLE_AMOUNT, ROLE_COST), 'expose': 'kpi',
        'describe': '毛利率 =（营收 − 成本×销量）÷ 营收；'
                    'cost 证据可由产品成本价表满足（既有口径）',
    },
    {
        'key': 'avg_ticket', 'label': '客单价', 'unit': '元',
        'requires': (ROLE_AMOUNT, ROLE_QUANTITY), 'expose': 'kpi',
        'describe': '客单价 = 营收 ÷ 销量（对应既有 kpi.avg_per_unit）',
    },
    {
        'key': 'sell_through', 'label': '动销率', 'unit': '%',
        'requires': (ROLE_QUANTITY, ROLE_STOCK), 'expose': 'derived',
        'describe': '动销率 = 销量 ÷（销量 + 剩余量）',
    },
    {
        'key': 'stock_turnover', 'label': '库存周转', 'unit': '次',
        'requires': (ROLE_QUANTITY, ROLE_STOCK), 'expose': 'derived',
        'describe': '库存周转 = 窗口累计销量 ÷ 剩余量',
    },
)

_FORMULA_FN = {
    'waste_rate': _f_waste_rate,
    'gross_margin': _f_gross_margin,
    'avg_ticket': _f_avg_ticket,
    'sell_through': _f_sell_through,
    'stock_turnover': _f_stock_turnover,
}


def formula_template(key):
    """按 key 取公式模板；未注册返回 None。"""
    for t in FORMULA_TEMPLATES:
        if t['key'] == key:
            return t
    return None


def compute_formula(key, **values):
    """按模板计算派生指标；分母为 0 等不可计算情形返回 None（不伪造）。"""
    fn = _FORMULA_FN.get(key)
    return fn(**values) if fn else None


def available_formulas(has_product_cost=None):
    """当前包可用的公式模板列表（requires 角色全部就绪才可用）。

    Args:
        has_product_cost: 产品表是否存在已录成本价（毛利率 cost 证据之一，
            由调用方在应用上下文传入；None=未知，仅按包角色判定）。

    Returns:
        tuple[dict]: 可用模板（保持 FORMULA_TEMPLATES 声明顺序）。
    """
    out = []
    for t in FORMULA_TEMPLATES:
        if has_roles(*t['requires']):
            out.append(t)
            continue
        # 毛利率特例：cost 证据可由「产品成本价表」满足（既有口径，护住零回归）
        if (t['key'] == 'gross_margin' and has_product_cost is True
                and has_roles(ROLE_AMOUNT)):
            out.append(t)
    return tuple(out)


# ─────────────────── 骨架字段提交键与表单骨架（P7） ───────────────────
# 角色 → DailySales 既有列名兜底：包未声明该角色时，`role_key()` 返回 None，
# 而业务层（h5 填报 / analytics）仍需要稳定的提交键与列名。
# 有兜底 = 老表单、老测试（直接 POST 既有列名）继续可用（零回归）；
# 扩展角色（cost/other）无内置列 → 返回 None，由调用方按「未声明」处理。
SKELETON_FALLBACK_KEYS = {
    ROLE_DIMENSION: 'product_name',
    ROLE_QUANTITY: 'product_sales_qty',
    ROLE_AMOUNT: 'total_revenue',
    ROLE_STOCK: 'raw_material_remaining',
    ROLE_WASTE: 'raw_material_waste',
    ROLE_NOTE: 'abnormal_notes',
}


def skeleton_key(role):
    """骨架字段（映射到 DailySales 既有列）的提交键。

    三种情形：
      - 包声明该角色且映射既有列 → 返回列名（如 stock → raw_material_remaining）；
      - 包声明该角色但未映射既有列 → 返回 None（该值归自定义指标组，
        由 `parse_custom_metrics()` 存 metrics_json，避免同一字段双写）；
      - 包完全未声明该角色 → 回退内置列名（老表单/老测试直接 POST 既有列名仍可用）。

    Args:
        role: 业务角色常量。

    Returns:
        str | None: 表单提交键（= 解析目标既有列名）；归自定义组或扩展角色时为 None。
    """
    defn = metric_by_role(role)
    if defn is None:
        return SKELETON_FALLBACK_KEYS.get(role)
    return defn['legacy_column'] or None


def _field(defn):
    """指标定义 → 模板渲染用的字段投影（name 即表单提交键）。

    Args:
        defn: `metric_by_role()` 返回的指标定义，或 None。

    Returns:
        dict | None: `{key, name, label, unit, value_type, required, role}`。
    """
    if not defn:
        return None
    return {
        'key': defn['key'],
        'name': defn['legacy_column'] or defn['key'],
        'label': defn['label'],
        'unit': defn['unit'],
        'value_type': defn['value_type'],
        'required': defn['required'],
        'role': defn['role'],
        # 数值步长：金额类允许两位小数，其余按整数录入
        # （与 `parse_int` 的严格解析对齐，避免浏览器放行小数后 400）
        'step': '0.01' if defn['role'] == ROLE_AMOUNT else '1',
    }


def skeleton_label(role, default):
    """骨架字段的中文名（包未声明该角色时用 default，供报错文案与列表单位用）。"""
    defn = metric_by_role(role)
    return defn['label'] if defn else default


def skeleton_field(role):
    """骨架字段投影（仅限映射既有列的角色；其余归自定义指标组）。"""
    defn = metric_by_role(role)
    if defn is None or not defn['legacy_column']:
        return None
    return _field(defn)


def _skeleton_fields(*roles):
    """按角色批量投影（跳过未声明/未映射既有列的角色，保持声明顺序）。"""
    return [f for f in (skeleton_field(r) for r in roles) if f]


def form_skeleton():
    """H5 填报表单骨架（随行业包变化；模板只渲染，不做角色判断）。

    分组规则：
      - 销售组：主记录维度（下拉）+ 销量 + 金额；
      - 库存组：stock / waste —— **包未声明任一角色则整组消失**（如便利店包）；
      - 自定义指标组：非既有列的扩展指标（既有能力，随包启停）；
      - 备注组：note 角色。

    Returns:
        dict: {
            'dimension': 字段投影 | None,
            'sales': [字段投影, ...],
            'material_group': str,
            'material': [字段投影, ...],
            'note': 字段投影 | None,
            'custom': [指标定义, ...],
        }
    """
    terms = industry_pack.pack_terms()
    return {
        'dimension': skeleton_field(ROLE_DIMENSION),
        'sales': _skeleton_fields(ROLE_QUANTITY, ROLE_AMOUNT),
        'material_group': terms.get('material_group') or '库存情况',
        'material': _skeleton_fields(ROLE_STOCK, ROLE_WASTE),
        'note': skeleton_field(ROLE_NOTE),
        'custom': custom_metric_defs(),
        # 列表/徽章用的数量单位（术语优先，其次指标定义，最后中性兜底）
        'quantity_unit': (terms.get('quantity_unit')
                          or skeleton_label_unit(ROLE_QUANTITY) or '份'),
    }


def skeleton_label_unit(role):
    """骨架字段单位（未声明角色时返回空串）。"""
    defn = metric_by_role(role)
    return defn['unit'] if defn else ''


def dashboard_labels():
    """看板/经营分析页的随包文案（P7-B3：模板只渲染不判断）。

    汇聚维度词、数量单位、损耗字段名、库存组标题与快捷提问样例，管理员侧页面与
    H5 填报表单共用同一套包术语，杜绝「表单已随包、看板仍是奶茶口径」的漂移。

    Returns:
        dict: ``{dimension, unit, material_unit, waste, material_group,
        sample_dimension, sample_shelf_question}``，均带中性兜底值（包缺该角色/术语时
        页面仍可渲染，不伪造数据）。
    """
    terms = industry_pack.pack_terms()
    dimension = skeleton_field(ROLE_DIMENSION) or {}
    waste = skeleton_field(ROLE_WASTE) or {}
    dimension_label = (dimension.get('label') or terms.get('record_dimension')
                       or '产品')
    return {
        'dimension': dimension_label,
        'unit': (terms.get('quantity_unit')
                 or skeleton_label_unit(ROLE_QUANTITY) or '份'),
        # P7-E3-B2b：原料/备货计量单位（规则版文案与 H5 原料字段共用同一口径）
        'material_unit': (terms.get('material_unit')
                          or skeleton_label_unit(ROLE_STOCK) or '份'),
        'waste': (waste.get('label') or '损耗'),
        'material_group': (terms.get('material_group') or '库存情况'),
        # M-9（P7-E3-B2）：模板快捷提问的行业样例（包未声明时用中性措辞兜底，
        # 不回落奶茶样例，避免切包后又显示「杨枝甘露」）
        'sample_dimension': (terms.get('sample_dimension')
                             or f'这款{dimension_label}'),
        'sample_shelf_question': (terms.get('sample_shelf_question')
                                  or f'这款{dimension_label}一般怎么保存？'),
    }


# ─────────────────────── 自定义指标入参解析 ───────────────────────
def parse_custom_metrics(form):
    """按当前包自定义指标定义解析表单值（F13/F14 分工：格式→400，范围/必填→flash）。

    Args:
        form: request.form（或任意 Mapping）。

    Returns:
        tuple: (values, error)
            values: {key: 值}，数值为 float/int、文本为 str；无有效提交为 {}；
            error: 范围/必填错误文案；None 表示通过。

    Raises:
        ParseError: 值非空但不是合法数字（路由层应转 400）。
    """
    defs = custom_metric_defs()
    if not defs:
        return {}, None
    values = {}
    for d in defs:
        raw = form.get(d['key'])
        text = (raw or '').strip() if isinstance(raw, str) else raw
        label = d['label']
        if text is None or text == '':
            if d.get('required'):
                return values, f'请填写「{label}」'
            continue
        if d['value_type'] == 'text':
            s = str(text)
            if len(s) > MAX_TEXT_LEN:
                return values, f'「{label}」不能超过 {MAX_TEXT_LEN} 字'
            values[d['key']] = s
            continue
        value = parse_float(text, label)  # 格式非法 → ParseError → 400
        error = check_range(value, label, MAX_METRIC_VALUE)
        if error:
            return values, error
        values[d['key']] = int(value) if d['value_type'] == 'integer' else value
    return values, None
