"""动态谈判引擎（P5）—— 隐藏心理价 + 让步曲线 + 话术特征判定的纯函数状态机。

设计范式与第五轮顾客情绪状态机（`DifyClient.customer_state`）一致：
**纯函数重放** —— 状态由对话历史重算，不落盘、不进 Cookie、不依赖网络。
收益：无脏值、无签名 Cookie 膨胀风险（4KB 预算）、单测可直接构造历史序列，
规则版完全离线可跑（演示/CI 稳定）。

三层结构：
  1. `classify_staff_utterance()`：店员每轮发言归入六类之一
     （价值解释 / 替代方案 / 无理由降价 / 对抗推诿 / 越权承诺 / 无关）；
  2. `replay()` / `step()`：按类别做状态转移（耐心衰减、心理价下移、让步累计）；
  3. `settle()` / `review()`：结束时刻判定成交/流失/未决，并输出四维复盘评分。

状态分层：
  - **隐藏态**（顾客不说破，仅在复盘揭示）：预算区间 `[budget_low, budget_high]`、
    心理价底线 `reservation`；
  - **可见态**（前端渲染）：耐心值、心理价区间提示、让步进度（见 `visible_state()`）。

内容外置：场景参数可由当前行业包 `pack.json` 的 `negotiation` 段覆盖
（键为场景名，值为 5 元组），缺失/非法一律回退本模块常量，绝不抛异常。
"""
import math

from src import industry_pack

# ── 耐心与轮次基线（与 dify_client.TONE_RULES 的口径保持同一量纲 0~100）──
PATIENCE_START = 70.0
PATIENCE_MIN, PATIENCE_MAX = 0.0, 100.0
ROUND_DECAY = 2.0           # 每轮自然消耗（顾客的等待成本）
DEAL_PATIENCE = 60.0        # 达成「期望价」时的耐心门槛
MAX_ROUNDS = 8              # 轮次上限：用尽即出结论
CONFRONT_LIMIT = 2          # 对抗推诿次数上限（达到即流失）
# 金额比较容差（5 分）：每轮 round(…, 2) 会累积 1 分内的浮点漂移，
# 判定「是否已让到期望价」时用容差吸收，避免卡在小数第三位上反复横跳。
PRICE_EPS = 0.05

DEFAULT_LIST_PRICE = 15.0   # 包内无有效产品价时的兜底标的价
PRICE_FLOOR_RATIO = 0.40    # 让价下限（占标的价比例，防越权承诺算出负价）

# ── 场景参数：(标的价倍率, 预算下界, 心理价底线, 预算上界, 初始耐心) ──
# 口径：`budget_low` 是顾客心理上「再低就要怀疑质量」的临界；`budget_high`
# 是预算上限（高于此宁愿不买）；`reservation` 是顾客想砍到的期望价。
# 因此恒有 budget_low < reservation ≤ budget_high ≤ ~list_price。
SCENARIO_TUNING = {
    'bargain':  (1.00, 0.62, 0.80, 0.94, 70.0),   # 砍价：底线最低、耐心中等
    'safety':   (1.00, 0.70, 0.90, 0.98, 72.0),   # 卫生担忧：几乎不让价，重在证据
    'pick':     (1.00, 0.68, 0.88, 0.97, 58.0),   # 挑剔服务：耐心偏低
    'rush':     (1.00, 0.72, 0.92, 0.99, 52.0),   # 催单：耐心最低
    'delivery': (1.00, 0.70, 0.90, 0.98, 60.0),   # 外卖自提：中等
    'member':   (1.00, 0.65, 0.84, 0.95, 66.0),   # 会员咨询：优惠敏感
}
DEFAULT_TUNING = (1.00, 0.68, 0.86, 0.96, 68.0)

# ── 话术特征词表（顺序即优先级，每轮发言只命中最靠前的一类）──
# 负面与越权优先于正向：一句「爱买不买」足以盖过同轮的「抱歉」。
CONFRONT_WORDS = (
    '爱买不买', '随便你', '快点吧', '别磨叽', '急什么', '嫌贵别买', '爱要不要',
    '不归我管', '不知道', '没办法', '自己看', '就这样', '没空',
)
OVERPROMISE_WORDS = (
    '终身', '永久', '无限', '包您满意', '我说了算', '最低价', '随便送',
    '下次免费', '一定给您', '保证最',
)
DISCOUNT_WORDS = (
    '便宜', '少收', '算你', '打折', '抹零', '降价', '优惠券', '再降', '让利',
    '给您优惠', '破价',
)
ALT_WORDS = (
    '推荐', '换成', '替代', '搭配', '套餐', '加料', '小杯', '柠檬水', '第二杯',
    '会员日', '学生', '另一款', '换一款', '少冰', '去冰',
)
VALUE_WORDS = (
    '真材实料', '新鲜', '当天', '现做', '口感', '用料', '品质', '健康', '卫生',
    '标准', '品牌', '手艺', '进口', '严格执行', '每日', '冷链',
    # 关系价值维护：致歉/安抚类发言无价格与替代信号时归入价值类，
    # 保持「六类」契约不扩张（纯致歉不是无理由降价，也不是对抗）。
    '抱歉', '不好意思', '对不起', '给您添麻烦', '请稍等', '别急', '马上', '立刻',
)
UTTERANCE_RULES = (
    ('confront', CONFRONT_WORDS),
    ('overpromise', OVERPROMISE_WORDS),
    ('discount', DISCOUNT_WORDS),
    ('alternative', ALT_WORDS),
    ('value', VALUE_WORDS),
)

CLASS_LABELS = {
    'value': '价值解释', 'alternative': '替代方案', 'discount': '无理由降价',
    'overpromise': '越权承诺', 'confront': '对抗推诿', 'irrelevant': '无关应答',
}

# 类别 → (耐心增量, 价格下移档位)。价格下移以「单档让步幅度」为单位：
# 直接降价让满 1 档；替代方案仅半档（换规格/加料满足需求而非破价）；
# 越权承诺超让 1.6 档（对顾客更爽但复盘要扣分的真实代价）；对抗不让价。
EFFECTS = {
    'value':       (6.0, 0.0),
    'alternative': (8.0, 0.5),
    'discount':    (10.0, 1.0),
    'overpromise': (12.0, 1.6),
    'confront':    (-22.0, 0.0),
    'irrelevant':  (-6.0, 0.0),
}

# 四维复盘：每轮发言在「价值锚定」维度上的单轮得分（取平均）
CLASS_ANCHOR_SCORE = {
    'value': 92, 'alternative': 84, 'discount': 35,
    'overpromise': 25, 'confront': 5, 'irrelevant': 40,
}
# 成交结果与情绪权重合计 0.45，价值锚定 0.35：靠无理由降价成交能拿到结果分，
# 但锚定分低 → 综合分显著低于「讲价值成交」，让复盘真正有区分度。
REVIEW_WEIGHTS = {'anchor': 0.35, 'pace': 0.20, 'emotion': 0.20, 'outcome': 0.25}
REVIEW_LABELS = {
    'anchor': '价值锚定', 'pace': '让步节奏', 'emotion': '情绪管理', 'outcome': '成交结果',
}
REVIEW_ADVICE = {
    'anchor': '价格锚点被过早让破：先用「真材实料 / 当日现做」讲清价值，再谈优惠。',
    'pace': '让利节奏失衡：替代方案（换规格 / 加料 / 套餐）应优先于直接降价。',
    'emotion': '情绪管理待加强：先认同诉求再给方案，避免「不知道 / 没办法」类推诿。',
    'outcome': '本次未成交：复盘顾客真实诉求（价格 / 效率 / 卫生），下次针对性应对。',
}


def classify_staff_utterance(text):
    """店员发言归类（顺序即优先级，只返回最靠前的一类）。

    Args:
        text: 店员本轮发言（允许 None / 空串）。

    Returns:
        str: 'confront' / 'overpromise' / 'discount' / 'alternative' /
        'value' / 'irrelevant'（无任何特征词时）。
    """
    msg = (text or '').strip()
    if not msg:
        return 'irrelevant'
    for name, words in UTTERANCE_RULES:
        if any(w in msg for w in words):
            return name
    return 'irrelevant'


def _parse_tuning(raw):
    """把包内自定义参数规范为 5 元组；结构或单调性非法返回 None。"""
    if not isinstance(raw, (list, tuple)) or len(raw) != 5:
        return None
    try:
        ratio, low_r, res_r, high_r, patience = (float(x) for x in raw)
    except (TypeError, ValueError):
        return None
    ok = (0 < low_r < res_r <= high_r <= 1.05) and 0 < ratio <= 2.0
    if not ok:
        return None
    return (ratio, low_r, res_r, high_r,
            min(PATIENCE_MAX, max(1.0, patience)))


def _tuning(scenario):
    """场景谈判参数：包内 `negotiation` 段优先，缺失/非法回退模块常量。

    非字符串场景名（None / 数字 / 列表 / 超长串）一律按空串处理——
    路由层已做白名单，但引擎不得因脏入参抛异常（异常不外扩契约）。
    """
    key = scenario if isinstance(scenario, str) else ''
    return _parse_tuning(industry_pack.pack_negotiation().get(key)) \
        or SCENARIO_TUNING.get(key, DEFAULT_TUNING)


def _pack_list_price():
    """标的价：当前行业包在售产品均价（无有效价格时回退兜底常量）。"""
    prices = []
    for item in industry_pack.pack_products():
        try:
            val = float(item.get('unit_price'))
        except (TypeError, ValueError):
            continue
        if val > 0:
            prices.append(val)
    if not prices:
        return DEFAULT_LIST_PRICE
    return round(sum(prices) / len(prices), 2)


def initial_state(scenario):
    """构造某场景的谈判初始状态（隐藏态 + 可见态一次算好）。"""
    ratio, low_r, res_r, high_r, patience = _tuning(scenario)
    anchor = _pack_list_price() * ratio
    return {
        'scenario': scenario,
        'list_price': round(anchor, 2),
        'budget_low': round(anchor * low_r, 2),
        'budget_high': round(anchor * high_r, 2),
        'reservation': round(anchor * res_r, 2),
        'current_price': round(anchor, 2),
        'concession': 0.0,
        'concession_pct': 0.0,
        'patience': float(patience),
        'rounds': 0,
        'classes': [],
        'counts': {},
    }


def _clamp_patience(value):
    """耐心值收口到 [0, 100]。"""
    return round(max(PATIENCE_MIN, min(PATIENCE_MAX, value)), 1)


def _concession_step(state):
    """单档让步幅度 = 可让价空间 / 轮次上限（保证「每轮让一档」恰好到区间下界）。"""
    span = state['budget_high'] - state['budget_low']
    return max(0.0, span) / MAX_ROUNDS


def step(state, utterance):
    """应用一轮店员发言，返回新的状态字典（不修改入参）。

    Args:
        state: `initial_state()` 或上一次 `step()` 的结果。
        utterance: 店员本轮发言文本。

    Returns:
        dict: 新状态（含本轮类别留痕与计数，供复盘使用）。
    """
    cls = classify_staff_utterance(utterance)
    new = dict(state)
    new['classes'] = [*(state.get('classes') or []), cls]
    counts = dict(state.get('counts') or {})
    counts[cls] = counts.get(cls, 0) + 1
    new['counts'] = counts
    new['rounds'] = int(state.get('rounds') or 0) + 1

    d_patience, price_units = EFFECTS.get(cls, (0.0, 0.0))
    floor = round(new['list_price'] * PRICE_FLOOR_RATIO, 2)
    new['current_price'] = round(
        max(floor, new['current_price'] - _concession_step(new) * price_units), 2)
    new['concession'] = round(new['list_price'] - new['current_price'], 2)
    new['concession_pct'] = (
        round(new['concession'] / new['list_price'] * 100, 1)
        if new['list_price'] > 0 else 0.0)
    new['patience'] = _clamp_patience(new['patience'] + d_patience - ROUND_DECAY)
    return new


def replay(history, scenario):
    """从对话历史重放谈判状态（纯函数；历史为空返回初始态）。

    Args:
        history: 对话历史行列表，形如 ``['店员：…', '顾客：…']``（允许 None）。
        scenario: 情景类型（决定隐藏态参数）。

    Returns:
        dict: 重放后的状态字典。
    """
    state = initial_state(scenario)
    for line in history or []:
        if isinstance(line, str) and line.startswith('店员：'):
            state = step(state, line[3:])
    return state


def _verdict(state, result, reason):
    """成交结论载荷（含复盘卡需要的让利数字）。"""
    return {
        'result': result,
        'deal': result == 'deal',
        'lost': result == 'lost',
        'reason': reason,
        'list_price': state['list_price'],
        'price': state['current_price'],
        'concession': state['concession'],
        'concession_pct': state['concession_pct'],
        'patience': state['patience'],
        'rounds': state['rounds'],
    }


def settle(state, max_rounds=MAX_ROUNDS):
    """结束时刻判定：成交 / 流失 / 未决（店员可提前结束，未谈拢即流失）。

    Args:
        state: 重放后的谈判状态。
        max_rounds: 轮次上限（达到即出最终结论）。

    Returns:
        dict: {'result', 'deal', 'lost', 'reason', 'price', 'concession', ...}。
    """
    counts = state.get('counts') or {}
    if counts.get('confront', 0) >= CONFRONT_LIMIT:
        return _verdict(state, 'lost', '两次对抗推诿，顾客直接放弃')
    if state['patience'] < 20:
        # 同一分支两种措辞：归零 = 彻底耗尽，>0 = 已在临界（避免多一个 return）
        reason = ('耐心耗尽，顾客不愿再谈' if state['patience'] <= PATIENCE_MIN
                  else '顾客情绪接近临界，终止谈判')
        return _verdict(state, 'lost', reason)
    if (state['patience'] >= DEAL_PATIENCE
            and state['current_price'] <= state['reservation'] + PRICE_EPS):
        return _verdict(state, 'deal', '价格已到顾客期望区间，顾客当场下单')
    if state['rounds'] >= max_rounds:
        if state['patience'] >= 45:
            return _verdict(state, 'deal', '顾客接受当前条件，成交')
        return _verdict(state, 'lost', '轮次用尽仍未谈拢，顾客离开')
    return _verdict(state, 'pending', '谈判进行中，顾客仍在观望')


def _anchor_score(state):
    """价值锚定维度：各轮类别单轮得分取平均（无发言按 0）。"""
    classes = state.get('classes') or []
    if not classes:
        return 0
    total = sum(CLASS_ANCHOR_SCORE.get(c, 40) for c in classes)
    return round(total / len(classes))


def _pace_score(state):
    """让步节奏维度：让利恰好到顾客期望价得满分，超额让利重扣、让不够轻扣。"""
    ideal = state['list_price'] - state['reservation']
    if ideal <= 0:
        return 60
    actual = state['concession']
    over = max(0.0, actual - ideal) / ideal
    short = max(0.0, ideal - actual) / ideal
    return round(max(0.0, min(100.0, 100 - over * 60 - short * 20)))


def _emotion_score(state):
    """情绪管理维度：终值耐心为主，每次对抗推诿额外扣分。"""
    confront_n = (state.get('counts') or {}).get('confront', 0)
    return round(max(0.0, min(100.0, state['patience'] - confront_n * 15)))


def _outcome_score(verdict):
    """成交结果维度：成交 100 / 未决 50 / 流失 0。"""
    return {'deal': 100, 'pending': 50}.get(verdict['result'], 0)


def review(state, verdict=None):
    """输出四维复盘评分与改进建议。

    Args:
        state: 重放后的谈判状态。
        verdict: `settle()` 的结果；缺省时内部即时判定。

    Returns:
        dict: {'deal', 'concession_pct', 'scores': [{'key','label','score'}],
               'total', 'level', 'advice': [str], 'classes': [str]}。
        scores 顺序固定为 价值锚定 / 让步节奏 / 情绪管理 / 成交结果（前端按序渲染）。
    """
    v = verdict or settle(state)
    raw = {
        'anchor': _anchor_score(state),
        'pace': _pace_score(state),
        'emotion': _emotion_score(state),
        'outcome': _outcome_score(v),
    }
    total = round(sum(raw[k] * REVIEW_WEIGHTS[k] for k in REVIEW_WEIGHTS))
    scores = [{'key': k, 'label': REVIEW_LABELS[k], 'score': raw[k]}
              for k in REVIEW_WEIGHTS]
    # 改进建议：取最低两个维度（成交结果优先，未成交时最该复盘）
    order = sorted(raw, key=lambda k: (raw[k], k != 'outcome'))
    advice = [REVIEW_ADVICE[k] for k in order[:2]]
    level = '优秀' if total >= 85 else ('合格' if total >= 60 else '需改进')
    return {
        'deal': v['deal'],
        'result': v['result'],
        'reason': v['reason'],
        'concession_pct': v['concession_pct'],
        'scores': scores,
        'total': total,
        'level': level,
        'advice': advice,
        'classes': [CLASS_LABELS.get(c, c) for c in (state.get('classes') or [])],
    }


def visible_state(state, verdict=None):
    """对外可见状态（前端谈判面板渲染用）。

    隐藏态只给「区间提示」不给精确底线：既让店员有判断依据，
    又不泄露顾客真实心理价（真实谈判中不存在读心术）。

    Args:
        state: 谈判状态。
        verdict: 可选的 `settle()` 结果（提供时一并回传结论）。

    Returns:
        dict: {'patience', 'price_range': [低, 高], 'progress', 'concession_pct',
               'rounds', 'status', ...}；数值均为可直接 JSON 序列化的基础类型。
    """
    out = {
        'patience': state['patience'],
        'price_range': [math.floor(state['budget_low']),
                        math.ceil(state['budget_high'])],
        'current_price': state['current_price'],
        'list_price': state['list_price'],
        'concession_pct': state['concession_pct'],
        'rounds': state['rounds'],
    }
    span = state['list_price'] - state['budget_low']
    out['progress'] = round(min(100.0, state['concession'] / span * 100)) if span > 0 else 0
    if verdict is not None:
        out['verdict'] = verdict
    return out
