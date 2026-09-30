"""学习者模型（AI 深化第四轮 D1）— 掌握度画像 / 间隔重复复习排期 / 自适应出题难度

把「答题记录」升级为「AI 学习者模型」，让培训从被动记录走向主动适应：

  - **掌握度画像**：按四大知识域聚合 QuizRecord，用「时间衰减加权正确率」
    刻画当前真实水平，样本不足时向中性值收缩（避免过度自信的画像）。
  - **间隔重复排期**：仿 SM-2 的简化间隔——掌握度越低复习间隔越短，
    到期即提醒，把复习从「一次性补训」变成「长期记忆曲线」。
  - **自适应难度**：按当前掌握度动态选择 basic / standard / advanced 题面要求，
    熟练者加码（要求答出例外），薄弱者降阶（只答关键要点）。

设计约定（与既有模块保持一致）：
  1. **不加表、不迁库**：全部基于 QuizRecord 实时聚合，纯函数、可单测；
  2. **不触碰网络与 DB**：只接收 record 序列，返回纯数据（dict/list），
     查询由路由层完成（沿用 dify_client 防腐层约定）；
  3. **任何异常不扩散**：记录缺失字段/None 一律按中性处理，绝不抛异常；
  4. 知识域口径与 `src/knowledge_base.py` 的 WEAK_DOMAIN_MAP 对齐。
"""
from datetime import UTC, datetime

from src import industry_pack
from src.knowledge_base import DIFFICULTY_HINTS, focus_domain

# 内嵌兜底知识域（运行时以当前行业包域表为准；顺序即能力雷达的轴顺序）
FALLBACK_DOMAINS = ('原料规范', '异议处理', '套餐价格', '服务话术')

# 兼容别名（历史常量名保留：部分测试与调用方按此名引用）
DOMAINS = FALLBACK_DOMAINS

# 题干关键词兜底（P7-E3-B / M-1：与 knowledge_base.WEAK_KEYWORDS 同源，
# 唯一真相源 = 内置兜底包；运行时应优先取 industry_pack.pack_weak_keywords()）
_FALLBACK_DOMAIN_KEYWORDS = industry_pack.FALLBACK_WEAK_KEYWORDS


def domains() -> tuple:
    """当前行业包的知识域列表（N 轴动态能力雷达的轴来源）。"""
    return tuple(industry_pack.pack_domains() or FALLBACK_DOMAINS)


def _domain_keywords() -> tuple:
    """题干关键词表：优先行业包弱项关键词，回退内嵌词表。"""
    return tuple(industry_pack.pack_weak_keywords() or _FALLBACK_DOMAIN_KEYWORDS)

# 掌握度记忆半衰期（天）：超过该天数的一次作答权重衰减为一半
HALF_LIFE_DAYS = 14.0
# 达到该样本量即视为置信度封顶；不足时向中性值收缩
FULL_CONFIDENCE_SAMPLE = 5
# 样本不足时的收缩锚点（中性掌握度）
NEUTRAL_MASTERY = 50.0

# 掌握度 → 等级标签（阈值降序，首个命中即返回）
MASTERY_LEVELS = (
    (85.0, '熟练'),
    (70.0, '良好'),
    (55.0, '合格'),
    (35.0, '薄弱'),
    (0.0, '待强化'),
)

# 掌握度 → 复习间隔（天）：越低越短，形成间隔重复曲线
REVIEW_INTERVALS = (
    (85.0, 14),
    (70.0, 7),
    (55.0, 4),
    (35.0, 2),
    (0.0, 1),
)

# 掌握度 → 出题难度
DIFFICULTY_LEVELS = (
    (85.0, 'advanced'),
    (60.0, 'standard'),
    (0.0, 'basic'),
)
DIFFICULTY_LABELS = {'basic': '基础', 'standard': '标准', 'advanced': '进阶'}

# 难度加权（第六轮 B）：答对进阶题比答对基础题更能说明真实掌握水平。
# 从题干尾部的难度提示语（knowledge_base.DIFFICULTY_HINTS，单一真相源）
# 推断该题难度：advanced 答对放大 1.15、basic 打折 0.85、推断失败按
# standard 权重 1.0（对历史记录零回归）。答错（rate=0）不额外惩罚。
DIFFICULTY_WEIGHTS = {'basic': 0.85, 'standard': 1.0, 'advanced': 1.15}


def difficulty_of_question(question: str) -> str:
    """从题干尾部提示语推断该题难度档位。

    Args:
        question: 题干文本（出题时难度提示语追加在题尾）。

    Returns:
        str: 'basic' / 'standard' / 'advanced'；无提示语按 'standard'。
    """
    text = question or ''
    for level, hint in DIFFICULTY_HINTS.items():
        if hint and hint in text:
            return level
    return 'standard'


def _now():
    """当前 UTC naive 时间（与 models 里 created_at 的落库口径一致）。"""
    return datetime.now(UTC).replace(tzinfo=None)


def _age_days(ts, now=None):
    """时间戳距今天数；空值/异常一律返回 None（不参与衰减计算）。"""
    if ts is None:
        return None
    try:
        delta = (now or _now()) - ts
    except TypeError:
        return None
    days = delta.total_seconds() / 86400.0
    return days if days >= 0 else 0.0


def _decay(days):
    """时间衰减权重：半衰期 HALF_LIFE_DAYS。"""
    if days is None:
        return 1.0
    return 0.5 ** (max(days, 0.0) / HALF_LIFE_DAYS)


def question_domain(question: str) -> str:
    """从题干推断知识域（与知识库 WEAK_DOMAIN_MAP 同口径）。

    Args:
        question: 题干文本。

    Returns:
        str: 四大知识域之一；无法归因时返回空串。
    """
    text = question or ''
    domain_names, keywords, keyword_map = _domain_context()
    return _question_domain_with(text, domain_names, keywords, keyword_map)


def _domain_context():
    """一次性取当前包的域/关键词映射（PF-3：避免逐记录重复读包）。"""
    return domains(), _domain_keywords(), industry_pack.pack_domain_map()


def _question_domain_with(text, domain_names, keywords, keyword_map):
    """在已取好的域/关键词快照上归因，语义与 `question_domain` 一致。"""
    for dom in domain_names:  # 先直接命中域全名（如定向补训的题干）
        if dom in text:
            return dom
    for kw in keywords:
        if kw in text:
            dom = keyword_map.get(kw) or focus_domain(kw)
            if dom:
                return dom
    return ''


def _outcome(rec):
    """单条记录的得分率（0.0~1.0）；不可判定返回 None。"""
    score = getattr(rec, 'score', None)
    if score is not None:
        try:
            return max(0.0, min(1.0, float(score) / 100.0))
        except (TypeError, ValueError):
            pass
    flag = getattr(rec, 'is_correct', None)
    if flag is True:
        return 1.0
    if flag is False:
        return 0.0
    return None


def _level_of(mastery: float) -> str:
    """掌握度 → 等级标签。"""
    for threshold, label in MASTERY_LEVELS:
        if mastery >= threshold:
            return label
    return MASTERY_LEVELS[-1][1]


def _interval_of(mastery: float) -> int:
    """掌握度 → 复习间隔天数。"""
    for threshold, days in REVIEW_INTERVALS:
        if mastery >= threshold:
            return days
    return REVIEW_INTERVALS[-1][1]


def _bucket(records, now=None, domain_names=None, domain_keywords=None,
            domain_map=None):
    """按知识域归组答题记录。

    Args:
        records: QuizRecord 序列（含 score/is_correct/created_at/question）。
        now: 可选的「当前时间」锚点（便于单测确定性）。

    Returns:
        dict: {域: [{'rate','age_days','at'}, ...]}，无法判定的记录丢弃。
    """
    domain_names = domain_names or domains()
    domain_keywords = domain_keywords or _domain_keywords()
    domain_map = domain_map or industry_pack.pack_domain_map()
    buckets = {d: [] for d in domain_names}
    for rec in records or []:
        rate = _outcome(rec)
        if rate is None:
            continue
        q_text = getattr(rec, 'question', '') or ''
        dom = _question_domain_with(q_text, domain_names, domain_keywords, domain_map)
        if not dom:
            continue
        # 难度加权（第六轮 B）：进阶题答对更值钱、基础题答对打折；
        # 仅对答对（rate>0）生效，答错不额外惩罚；权重封顶 1.0。
        weight = DIFFICULTY_WEIGHTS.get(difficulty_of_question(q_text), 1.0)
        buckets[dom].append({
            'rate': (min(1.0, rate * weight) if rate > 0 else 0.0),
            'age_days': _age_days(getattr(rec, 'created_at', None), now),
            'at': getattr(rec, 'created_at', None),
        })
    return buckets


def _domain_mastery(items):
    """单域掌握度：时间衰减加权正确率 + 样本置信度收缩。

    Args:
        items: `_bucket` 中该域的条目列表。

    Returns:
        dict: {'mastery', 'confidence', 'sample', 'last_at', 'trend'}。
    """
    sample = len(items)
    confidence = min(1.0, sample / float(FULL_CONFIDENCE_SAMPLE)) if sample else 0.0
    if not sample:
        return {'mastery': round(NEUTRAL_MASTERY, 1), 'confidence': 0.0,
                'sample': 0, 'last_at': None, 'trend': 'none'}

    num = sum(_decay(it['age_days']) * it['rate'] for it in items)
    den = sum(_decay(it['age_days']) for it in items)
    raw = (num / den * 100.0) if den else NEUTRAL_MASTERY
    mastery = confidence * raw + (1 - confidence) * NEUTRAL_MASTERY

    # 趋势：最近 3 次均值 对比 更早次均值（样本不足记 none）
    trend = 'none'
    if sample >= 4:
        ordered = sorted(items, key=lambda x: (x['at'] is None, x['at']))
        recent = ordered[-3:]
        prev = ordered[:-3]
        if prev:
            r_avg = sum(x['rate'] for x in recent) / len(recent)
            p_avg = sum(x['rate'] for x in prev) / len(prev)
            diff = r_avg - p_avg
            trend = 'up' if diff > 0.05 else ('down' if diff < -0.05 else 'flat')

    last_at = max((it['at'] for it in items if it['at'] is not None), default=None)
    return {
        'mastery': round(mastery, 1),
        'confidence': round(confidence, 2),
        'sample': sample,
        'last_at': last_at,
        'trend': trend,
    }


def mastery_profile(records, now=None) -> dict:
    """全域掌握度画像（能力雷达 + 整体水平）。

    Args:
        records: 当前店员的 QuizRecord 序列。
        now: 可选时间锚点。

    Returns:
        dict: {'domains': [...], 'overall', 'overall_level', 'radar':
              {'labels','values'}, 'sample_total', 'has_data'}。
    """
    now = now or _now()
    domain_names, keywords, domain_map = _domain_context()
    buckets = _bucket(records, now, domain_names, keywords, domain_map)
    domain_rows = []
    for dom in domain_names:
        m = _domain_mastery(buckets.get(dom, []))
        m['domain'] = dom
        m['level'] = _level_of(m['mastery'])
        domain_rows.append(m)

    sample_total = sum(d['sample'] for d in domain_rows)
    if sample_total:
        # 整体水平按各域样本量加权，避免小样本域拉偏
        overall = sum(d['mastery'] * d['sample'] for d in domain_rows) / sample_total
    else:
        overall = 0.0
    return {
        'has_data': sample_total > 0,
        'domains': domain_rows,
        'overall': round(overall, 1),
        'overall_level': _level_of(overall) if sample_total else '暂无数据',
        'sample_total': sample_total,
        'radar': {
            'labels': list(domain_names),
            'values': [d['mastery'] for d in domain_rows],
        },
    }


def review_schedule(records, now=None, profile=None) -> list:
    """间隔重复复习排期（SM-2 简化版）。

    掌握度越低 → 间隔越短；到期/逾期优先；从未练过的域立即安排。

    Args:
        records: 当前店员的 QuizRecord 序列。
        now: 可选时间锚点。

    Returns:
        list[dict]: 每域一条 {'domain','mastery','level','interval_days',
                    'next_review','due_days','urgency','sample'}，
                    按紧急度（due_days 升序）排序。
    """
    now = now or _now()
    profile = profile or mastery_profile(records, now=now)
    schedule = []
    for d in profile['domains']:
        interval = _interval_of(d['mastery'])
        last_at = d['last_at']
        if last_at is None:
            due_days = 0
            next_review = now
        else:
            elapsed = _age_days(last_at, now) or 0.0
            due_days = round(interval - elapsed)
            next_review = last_at
        if due_days <= 0:
            urgency = '待学习' if last_at is None else '已到期'
        elif due_days == 1:
            urgency = '明日复习'
        elif due_days <= 3:
            urgency = '临近复习'
        else:
            urgency = '已排期'
        schedule.append({
            'domain': d['domain'],
            'mastery': d['mastery'],
            'level': d['level'],
            'sample': d['sample'],
            'interval_days': interval,
            'next_review': (next_review.strftime('%Y-%m-%d') if next_review else ''),
            'due_days': max(due_days, 0),
            'urgency': urgency,
        })
    schedule.sort(key=lambda x: (x['due_days'], x['mastery']))
    return schedule


def adaptive_difficulty(value, domain: str = '') -> str:
    """按掌握度给出自适应出题难度。

    Args:
        value: 掌握度数值（float）或 `mastery_profile()` 的返回值。
        domain: 可选域名；value 为 profile 时按该域取掌握度。

    Returns:
        str: 'basic' / 'standard' / 'advanced'。
    """
    if isinstance(value, dict):
        doms = value.get('domains') or []
        if domain:
            hit = next((d for d in doms if d.get('domain') == domain), None)
            mastery = hit['mastery'] if hit else value.get('overall', 0.0)
        else:
            mastery = value.get('overall', 0.0)
    else:
        try:
            mastery = float(value)
        except (TypeError, ValueError):
            mastery = 0.0
    for threshold, level in DIFFICULTY_LEVELS:
        if mastery >= threshold:
            return level
    return 'basic'


def learning_focus(records, now=None) -> dict:
    """学习优先级：最该补的域 + 复习排期 + 一句自然语言建议。

    Args:
        records: 当前店员的 QuizRecord 序列。
        now: 可选时间锚点。

    Returns:
        dict: {'profile', 'schedule', 'focus_domain', 'difficulty', 'advice'}。
    """
    now = now or _now()
    profile = mastery_profile(records, now=now)
    schedule = review_schedule(records, now=now, profile=profile)
    # 优先级：已到期且掌握度最低的域
    due = [s for s in schedule if s['due_days'] <= 0]
    pool = due or schedule
    focus = min(pool, key=lambda x: (x['mastery'], x['due_days'])) if pool else None
    focus_domain_name = focus['domain'] if focus else ''
    difficulty = adaptive_difficulty(profile, focus_domain_name)

    if not profile['has_data']:
        advice = '练习数据还不足，先完成一轮上岗考核，AI 才能画出你的能力画像。'
    else:
        advice = (f"当前整体掌握度 {profile['overall']} 分（{profile['overall_level']}）。"
                  f"优先补强「{focus_domain_name}」（掌握度 {focus['mastery']}、"
                  f"{focus['level']}），建议 {focus['interval_days']} 天内复习一次。")
    return {
        'profile': profile,
        'schedule': schedule,
        'focus_domain': focus_domain_name,
        'difficulty': difficulty,
        'advice': advice,
    }
