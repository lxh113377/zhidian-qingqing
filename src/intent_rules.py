"""意图关键词表单一真相源（通用化改造 P7-E3-B3 / 审查项 M-11）

改造前意图词表存在 3 份且已漂移：`dify_client._assistant_rule_reply` 的分支
条件内联词、`agent_trace.INTENT_RULES`、`routes/ai._answer_assessment_query`
的考核词各写一套，导致「决策轨迹说 A 意图、真实路由答 B」（与 P6 的
「轨迹意图 = 真实路由分支」承诺相悖）。

本模块收敛为一张表：
  - `INTENT_RULES`：顺序与 `_assistant_rule_reply` 的路由分支一致（考核问询
    在路由层直答、不在规则版分支内，但列在表首供轨迹与路由层共用）；
  - `INTENT_EXCLUDES`：命中即在当前意图上判否的例外词（备货类问法走备货分支）；
  - `INTENT_LABELS`：轨迹时间线展示的意图中文名；
  - `INTENT_EXEMPLARS` / `INTENT_ANCHORS` / `INTENT_FUZZY_THRESHOLD`：
    第五轮模糊意图路由的数据（算法仍在 `dify_client`，因其依赖判分归一文本工具）。

消费方（只读，禁再复制词表）：`dify_client` / `agent_trace` / `routes.ai`。
词表内容为三份旧表的**并集**（消除漂移），分支顺序与既有字面量语义不变。
"""

# 意图关键词表：顺序与 `_assistant_rule_reply` 的路由分支一致，
# 保证轨迹里的意图就是真实路由到的分支（不是第二套判定逻辑）。
INTENT_RULES = (
    ('assessment', ('考核', '成绩', '通过率', '谁考', '考得好', '答题情况', '学得怎么样')),
    ('forecast', ('明天', '预计', '预估', '大概会卖', '能卖多少')),
    ('compare', ('这周', '上周', '环比', '比上周', '涨了还是跌', '跟上周比')),
    ('slow', ('卖得最差', '垫底', '滞销', '卖得少', '哪些卖得', '不畅销')),
    ('margin', ('毛利', '利润', '赚多少', '成本')),
    ('revenue', ('营收', '收入', '营业额', '业绩', '卖了多少')),
    ('qty', ('销量', '卖几杯', '多少杯', '库存')),
    ('waste', ('损耗', '废料', '浪费', '异常')),
    ('restock', ('备货', '进多少', '采购', '进货', '明天')),
)

# 例外词：命中即在当前意图上判否（与规则版「备货类问法走备货分支」同逻辑）
INTENT_EXCLUDES = {
    'forecast': ('备货', '进货', '采购', '进多少'),
}

INTENT_LABELS = {
    'assessment': '考核问询', 'product': '产品问询', 'forecast': '销量预测',
    'compare': '周期对比', 'slow': '滞销榜', 'margin': '毛利核算',
    'revenue': '营收问询', 'qty': '销量问询', 'waste': '损耗归因',
    'restock': '备货建议', 'knowledge': '知识库问答', 'empty': '空问句',
}

# ── 模糊意图路由数据（第五轮；算法在 dify_client，数据在此收敛）──
INTENT_FUZZY_THRESHOLD = 0.34       # 相似度下限（低于此值视为无关问句）

INTENT_EXEMPLARS = {
    'revenue': ('生意好不好', '今天生意怎么样', '最近收入怎么样', '今天赚了多少', '业绩怎么样'),
    'margin': ('赚不赚钱', '利润怎么样', '毛利大概多少', '成本收回来了吗', '还能挣多少'),
    'forecast': ('明天大概卖多少', '预计明天销量', '明天能卖几杯', '预估一下明天'),
    'compare': ('这周比上周怎么样', '销量是涨了还是跌了', '跟上周比生意如何', '这两周对比一下'),
    'slow': ('哪个卖得最差', '有没有卖不动的', '哪些不太畅销', '什么产品销量垫底'),
    'qty': ('今天卖了多少杯', '销量咋样', '库存还剩多少', '卖出去几杯'),
    'waste': ('有没有浪费很多', '废料多不多', '损耗情况怎么样', '报废的原料多吗'),
    'restock': ('明天要进多少货', '要不要多采购点', '给个备货建议', '需要囤多少原料'),
}

# 意图 → 锚关键词（注入后必然命中既有关键词分支，保证递归深度恒为 1）
INTENT_ANCHORS = {
    'revenue': '营收',
    'margin': '毛利',
    'forecast': '预计',
    'compare': '环比',
    'slow': '滞销',
    'qty': '销量',
    'waste': '损耗',
    'restock': '备货',
}

# 意图 → 关键词元组（match/first 的查找表，由 INTENT_RULES 派生）
_INTENT_KEYWORDS = dict(INTENT_RULES)


def match_intent(question: str, intent: str) -> bool:
    """问句是否命中指定意图（关键词命中且不含该意图的例外词）。

    Args:
        question: 已 strip 的问句文本。
        intent: `INTENT_RULES` 中的意图名；未知意图恒 False。

    Returns:
        bool: True = 路由到该意图分支。
    """
    keywords = _INTENT_KEYWORDS.get(intent, ())
    if not any(k in question for k in keywords):
        return False
    return not any(k in question for k in INTENT_EXCLUDES.get(intent, ()))


def first_keyword(question: str, intent: str) -> str:
    """该意图在问句中命中的第一个关键词（供轨迹展示判定依据，未命中空串）。"""
    for k in _INTENT_KEYWORDS.get(intent, ()):
        if k in question:
            return k
    return ''
