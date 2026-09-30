"""中性「通用门店」空白骨架（PH2-4A）。

背景：早期新建空白包不写任何内容段，`_normalize_pack()` 的三级回退便把
**奶茶内置兜底**（FALLBACK_PACK）塞进新包 —— 表现为「新建的便利店包里
出现珍珠奶茶题库」。这里提供显式中性骨架，让空白包不再依赖回退，
同时把指标收敛到三个必需角色（维度/数量/金额），不含 stock/waste/note。
"""
from __future__ import annotations

NEUTRAL_METRICS = (
    {
        'key': 'product_name', 'label': '项目', 'value_type': 'text', 'unit': '',
        'role': 'dimension', 'agg': 'count', 'required': True, 'sort': 1,
        'legacy_column': 'product_name',
    },
    {
        'key': 'product_sales_qty', 'label': '数量', 'value_type': 'number', 'unit': '单',
        'role': 'quantity', 'agg': 'sum', 'required': True, 'sort': 2,
        'legacy_column': 'product_sales_qty',
    },
    {
        'key': 'total_revenue', 'label': '金额', 'value_type': 'number', 'unit': '元',
        'role': 'amount', 'agg': 'sum', 'required': True, 'sort': 3,
        'legacy_column': 'total_revenue',
    },
)

NEUTRAL_TERMS = {
    'store': '门店',
    'record_dimension': '项目',
    'record_dimension_plural': '项目',
    'quantity_unit': '单',
    'material_group': '库存情况',
    'material_remaining': '库存剩余',
    'material_waste': '损耗数量',
    'material_unit': '份',
    'staff_title': '店员',
    'customer_title': '顾客',
    'assistant_persona': '你是门店经营助手',
    'analyst_persona': '你是门店经营分析师',
    'trainer_persona': '你是门店培训教练',
    'scenario_customer': '门店顾客',
    'report_footer': '门店经营参考',
    'sample_dimension': '主营项目',
    'sample_shelf_question': '这个项目的标准流程是什么？',
}

_DOMAIN_SPECS = (
    ('商品与服务', '明确主营项目、规格与定价，保证交付一致',
     ['商品', '服务', '规格', '定价']),
    ('服务流程', '固化接待到交付的标准流程，减少返工与投诉',
     ['流程', '接待', '交付', '标准']),
    ('库存与损耗', '掌握进货、盘点与损耗控制的基本方法',
     ['库存', '盘点', '损耗', '进货']),
    ('门店运营', '做好排班、卫生、安全与顾客关系维护',
     ['运营', '排班', '卫生', '安全']),
)

_KNOWLEDGE_TEMPLATES = {
    '商品与服务': (
        '# 商品与服务\n\n## 一、主营项目\n\n- 列出本店主营项目与规格，明确单价与交付形式。\n'
        '- 项目变更需同步更新价目与员工培训。\n\n## 二、质量标准\n\n'
        '- 每个项目写明验收标准（外观/规格/时长）。\n- 不达标不出品，按流程返工或报废。\n\n'
        '## 三、定价与说明\n\n- 明码标价，先报价再操作。\n- 优惠门槛提前说明，避免事后争议。\n'
    ),
    '服务流程': (
        '# 服务流程\n\n## 一、标准动作\n\n1. 接待：主动问候，确认需求。\n'
        '2. 确认：复述项目、数量与价格。\n3. 执行：按标准流程操作。\n'
        '4. 交付：当面复检并说明注意事项。\n\n## 二、异常处理\n\n'
        '- 出错先致歉再补救，超权限请示负责人。\n- 每笔异常登记，供复盘。\n'
    ),
    '库存与损耗': (
        '# 库存与损耗\n\n## 一、盘点\n\n- 按日/周盘点，记录剩余与报废数量。\n'
        '- 账实不符当日查明，不跨期挂账。\n\n## 二、损耗控制\n\n'
        '- 损耗率 = 损耗量 ÷（剩余量 + 损耗量）。\n- 连续超标需回溯备货、储存与操作环节。\n\n'
        '## 三、红线\n\n- 变质/过期物料一律停用报废，禁止降价复用。\n'
    ),
    '门店运营': (
        '# 门店运营\n\n## 一、排班与交接\n\n- 按客流安排班次，交接班核对现金与待办。\n'
        '- 交接双方签字确认，异常当班上报。\n\n## 二、卫生与安全\n\n'
        '- 营业前后清洁，通道无堆物，地面无积水。\n- 发现隐患立即处理并登记。\n\n'
        '## 三、顾客关系\n\n- 记录顾客偏好与投诉，用于改进服务。\n- 不承诺无权限的优惠与赔偿。\n'
    ),
}

_QUESTIONS = (
    ('本店主营项目有哪些，各自的标准是什么？',
     '按门店实际填写：项目名称 + 规格 + 验收标准，并保持价目表同步。', '商品与服务'),
    ('顾客下单后，标准服务流程分几步？',
     '接待 → 确认（项目/数量/价格）→ 执行 → 交付复检，四步缺一不可。', '服务流程'),
    ('库存盘点发现账实不符，应该怎么处理？',
     '当日复查同规格串号、调拨与报废记录；确认差异后登记原因并修正账目。', '库存与损耗'),
    ('门店出现顾客投诉，第一步做什么？',
     '先致歉不辩解，完整听取诉求并记录，再给可行方案；超权限请示负责人。', '门店运营'),
)

_SCENARIOS = {
    'pool': {
        'inquiry': [
            '你们这个项目具体包含什么？多少钱？',
            '这个和另一个有什么区别，我该选哪个？',
            '能便宜点吗？别家好像更划算。',
        ],
        'complaint': [
            '你们这次做得和上次不一样，怎么回事？',
            '等太久了，我赶时间。',
            '这个效果我不太满意，能重做吗？',
        ],
    },
    'roles': {
        'inquiry': ['新顾客·小林', '对项目不了解，边问边比较', '弄明白差异后下单'],
        'complaint': ['不满顾客·周女士', '带着情绪，关注处理态度', '得到合理解释与补救'],
    },
    'labels': {'inquiry': '项目咨询', 'complaint': '顾客投诉'},
    'default': 'inquiry',
}

_PRODUCTS = (
    {'product_name': '主营项目A', 'unit_price': 10.0, 'cost_price': None,
     'category': '通用'},
    {'product_name': '主营项目B', 'unit_price': 20.0, 'cost_price': None,
     'category': '通用'},
    {'product_name': '主营项目C', 'unit_price': 30.0, 'cost_price': None,
     'category': '通用'},
)


def neutral_sections():
    """返回中性骨架的各内容段（新建空白包的起点，全部为通用口径）。"""
    domains = [
        {'name': name, 'knowledge_file': f'{name}.md', 'tip': tip, 'keywords': list(keywords)}
        for name, tip, keywords in _DOMAIN_SPECS
    ]
    return {
        'terms': dict(NEUTRAL_TERMS),
        'domains': domains,
        'weak_keywords': [kw for _, _, kws in _DOMAIN_SPECS for kw in kws],
        'synonyms': [['便宜', '优惠'], ['多少钱', '价格'], ['保存', '储存']],
        # M3：0.1 = 新建空白包的「中性起点值」，语义是「新包从这里开始调」，
        # 不是兜底值——运行期兜底 0.15 在 industry_pack.FALLBACK_PACK /
        # DEFAULT_WASTE_RATE_THRESHOLD，两处数字不同是设计而非漂移。
        'formula_params': {'waste_rate_threshold': 0.1},
        'metrics': [dict(m) for m in NEUTRAL_METRICS],
        'questions': [{'q': q, 'a': a, 'weak': weak} for q, a, weak in _QUESTIONS],
        'scenarios': {
            'pool': {k: list(v) for k, v in _SCENARIOS['pool'].items()},
            'roles': {k: list(v) for k, v in _SCENARIOS['roles'].items()},
            'labels': dict(_SCENARIOS['labels']),
            'default': _SCENARIOS['default'],
        },
        'products': [dict(p) for p in _PRODUCTS],
        'knowledge': dict(_KNOWLEDGE_TEMPLATES),
    }


__all__ = ['NEUTRAL_METRICS', 'NEUTRAL_TERMS', 'neutral_sections']
