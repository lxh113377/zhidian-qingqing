"""轻量知识库检索层（RAG 能力，纯标准库实现，F0 零第三方依赖）

为门店业务 AI 提供「知识库驱动」的检索增强能力：
  - 数据源：`部署/knowledge_base/` 下 4 篇 md（异议处理/原料规范/套餐价格/服务话术）；
    任何环境缺失外部文件时，自动回退到内嵌同源种子（保证权威目录/CI 亦可检索）。
  - 检索：中文 Bigram 加权打分 + 目标域聚焦，无分词库依赖。
  - 用途：
      * Dify 版：`build_context()` 把检索结果注入 prompt（RAG 增强）
      * 规则版：`rule_answer()` 引述原始知识库内容生成回答（知识库驱动而非猜）
      * 考核补训：`topic_suggestion()` 按薄弱点映射复习建议

设计约定：
  1. 所有对外接口只接收/返回纯数据（str / list / dict），不触碰网络与 DB；
  2. 检索失败/空库一律返回空结果，绝不让检索异常扩散到业务层；
  3. 知识域标签与业务侧弱项标签（src/routes/ai.py 的 weak）保持对齐。
"""
import json
import math
import os
import re
import threading

from src import industry_pack

# ─────────────────────────────────────────────────────────────
# 检索反馈（第四轮 D4）：把「这条知识有没有帮上忙」沉淀为权重
# 落盘的轻量 JSON（不加数据库表），检索时对命中的知识块做加权/降权。
# ─────────────────────────────────────────────────────────────
FEEDBACK_FILE = os.environ.get('KB_FEEDBACK_FILE') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'kb_feedback.json')
FEEDBACK_STEP = 0.10      # 单条反馈的权重步长
FEEDBACK_MIN = 0.70       # 权重下限（被踩再多也不彻底屏蔽）
FEEDBACK_MAX = 1.50       # 权重上限

# IDF 缓存：键为语料指纹，值为 {bigram: idf}
_IDF_CACHE = {'key': None, 'map': {}}
# 知识库与反馈缓存（PF-2）：同一包/同一目录指纹下不重复读 md、不按块重复读反馈。
_KB_CACHE = {'key': None, 'data': None}
_KB_LOCK = threading.RLock()

# ─────────────────────────────────────────────────────────────
# 知识库内嵌同源种子（与 部署/knowledge_base/*.md 内容一致）
# 外部 md 缺失时兜底使用，保证任何环境都可检索演示。
# ─────────────────────────────────────────────────────────────
KB_SEED = {
    '异议处理': [
        ('价格异议',
         '顾客嫌贵：先认同「理解，这杯用的都是真材实料」，再强调价值'
         '（新鲜芒果+西柚+椰浆、份量足），给替代而非降价（柠檬水 ¥8），'
         '最后引导会员（周三第二杯半价）。'),
        ('等待异议',
         '顾客催单：致歉+说明+安抚，「不好意思让您久等了，'
         '现做饮品为了保证口感，大约还要 2 分钟，马上好」；'
         '高峰提前告知预计时长管理预期。'),
        ('品质安全异议',
         '卫生质疑：展示操作规范，「原料每日清点和效期管理，操作台每班次消毒」；'
         '冰量异议主动提供「少冰/去冰」选项。'),
        ('口味异议',
         '先倾听不辩解，「抱歉没调到您喜欢的口味」；'
         '补救：重做一杯（调整糖度）或换等价产品，优先保住复购。'),
        ('处理原则',
         '先处理情绪，再处理事情；不出具过期/变质原料、不虚假宣传；'
         '重大投诉升级店长，不做超权限承诺。'),
    ],
    '原料规范': [
        ('原料储存',
         '鲜奶冷藏 2-6℃ 开封后 48h；芒果/西柚冷藏 2-3 天切后果肉 4h 内用完；'
         '珍珠保温 60℃+ 4h 超时报废；芋泥冷藏 24h；茶底冷藏当日不过夜；'
         '柠檬冷藏 5 天切片后 4h 用完。'),
        ('配比标准',
         '珍珠奶茶茶底:奶=3:1、珍珠 50g/杯；杨枝甘露芒果 120g+西柚 30g+西米 40g+椰浆 30ml；'
         '烧仙草仙草冻 150g+鲜奶 200ml；芋泥波波芋泥 80g+鲜奶 200ml+波波 40g。'),
        ('损耗控制',
         '按近 7 天销量动态备货，易损原料（鲜果/鲜奶）少备勤补；'
         '每日盘点剩余量与损耗量，损耗率=损耗量/(剩余量+损耗量)；'
         '损耗率超 15% 视为异常，需查明原因（备货过多/储存不当/操作浪费）。'),
        ('卫生操作',
         '操作前洗手消毒、佩戴口罩帽；生熟分开刀具砧板专用；操作台每班次清洁消毒；'
         '含芒果、芋泥、奶制品的饮品主动告知顾客过敏提示。'),
    ],
    '套餐价格': [
        ('产品单价',
         '珍珠奶茶 ¥12、杨枝甘露 ¥18、烧仙草 ¥15、柠檬水 ¥8、芋泥波波 ¥16；'
         '果茶价低（¥8-18），奶茶为主力产品。'),
        ('套餐优惠',
         '午后搭档：任意奶茶+柠檬水立减 ¥3；双人欢享：2 杯杨枝甘露 ¥32（省 ¥4）；'
         '学生特惠全场 9 折不同享；会员日每周三第二杯半价。'),
        ('加料价目', '加珍珠/波波 +¥2、加芋泥 +¥3、换鲜奶 +¥2。'),
        ('价格话术',
         '先报主产品价再带增值选项「加 ¥X 可升级/加料」；'
         '提及优惠必须先说门槛（满 2 杯/周三）避免纠纷；明码标价不在收银台临时改价。'),
    ],
    '服务话术': [
        ('迎客',
         '「欢迎光临！今天想喝点什么？」顾客犹豫时推荐招牌：珍珠奶茶和杨枝甘露。'),
        ('点单与推荐',
         '确认订单复述品名；主动询问「甜度和冰量有要求吗？默认七分糖少冰」；'
         '关联推荐「这款可以加珍珠，口感更丰富」。'),
        ('制作与交付',
         '制作中「稍等一下，马上为您做」；交付提醒「小心烫/小心洒」；'
         '杨枝甘露含芒果，过敏顾客主动提醒。'),
        ('送客与会员',
         '「慢走，欢迎下次光临」；引导会员「加个微信，每周三会员日有第二杯半价」。'),
        ('服务禁忌',
         '不催促、不贬低竞品、不对顾客外貌或消费能力评头论足；'
         '出错先致歉再补救；遇投诉先倾听不辩解。'),
    ],
}

# 业务弱项标签 → 知识库域 映射（与 src/routes/ai.py 的 weak 标签对齐）
WEAK_DOMAIN_MAP = {
    '原料': '原料规范',
    '损耗': '原料规范',
    '保存': '原料规范',
    '价格': '套餐价格',
    '套餐': '套餐价格',
    '话术': '服务话术',
    '服务': '服务话术',
    '异议': '异议处理',
    '安全': '异议处理',
    '卫生': '异议处理',
}

# 域 → 复习建议模板
DOMAIN_TIP = {
    '异议处理': '巩固「先处理情绪再处理事情」原则，重点练习价格异议与催单场景的应答',
    '原料规范': '熟记各原料储存条件与效期，强化「损耗率超 15% 即异常」的排查意识',
    '套餐价格': '牢记在售产品单价与套餐门槛，练习先报主价再带优惠增值选项的报价',
    '服务话术': '打磨迎客/点单/交付/送客全流程话术，坚持「出错先致歉再补救」原则',
}

# ─────────────────────────────────────────────────────────────
# 检索增强：查询侧归一（同义词/口语 → 知识库惯用词）
# 统一方向：把用户口语词替换为知识库原文常用词，使 Bigram 能交叠。
# ─────────────────────────────────────────────────────────────
SEARCH_SYNONYMS = [
    ('省钱', '优惠'), ('省多少', '优惠'), ('便宜', '优惠'), ('折扣', '优惠'),
    ('保存', '储存'), ('存放', '储存'), ('效期', '保质期'),
    ('应对', '话术'), ('回复', '话术'), ('回答', '话术'),
    ('价钱', '价格'), ('价', '价格'),
    ('怎么', '如何'), ('多少', '几'),
]
# 中文数字字 → 阿拉伯数字（1-99 简单数词归一用）
_CN_DIGIT = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
             '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}


def _cn_int(s: str):
    """中文数词（≤99，含「十」结构）转阿拉伯数字；无法解析返回 None。"""
    if not s:
        return None
    total = 0
    num = 0
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch == '十':
            total += (num or 1) * 10
            num = 0
        else:
            return None
    return total + num


def _normalize_numbers(text: str) -> str:
    """中文数字表述归一为阿拉伯数字（百分之十五→15%、三小时→3小时）。

    仅在白名单单位/结构上生效，避免误伤句中常见字（如"一起"）。
    """
    def repl_percent(m):
        n = _cn_int(m.group(1))
        return f'{n}%' if n is not None else m.group(0)

    text = re.sub(r'百分之([一二两三四五六七八九十]+)', repl_percent, text)

    def repl_units(m):
        n = _cn_int(m.group(1))
        return f'{n}{m.group(2)}' if n is not None else m.group(0)

    text = re.sub(r'([一二两三四五六七八九十]+)(小时|分钟|天|杯|元)', repl_units, text)
    return text


def _normalize_query(text: str) -> str:
    """查询侧归一：同义词替换 + 数字归一 + 全半角折叠。"""
    text = (text or '').strip()
    for src, dst in (industry_pack.pack_synonyms() or SEARCH_SYNONYMS):
        if src in text:
            text = text.replace(src, dst)
    return _normalize_numbers(text)


def _load_kb() -> dict:
    """加载知识库（P4 起 DB 覆盖层内联优先，探不到时回退内嵌种子）。

    Returns:
        dict: {域: [(标题, 内容), ...]}。
    """
    key = _kb_cache_key()
    with _KB_LOCK:
        if _KB_CACHE['key'] == key and _KB_CACHE['data'] is not None:
            return _KB_CACHE['data']

    # 0) DB 覆盖层内联知识（P4 配置后台编辑后即时生效，按域覆盖）
    inline = industry_pack.pack_knowledge_inline()
    if inline:
        merged = {}
        for dom, text in inline.items():
            blocks = _split_blocks(text or '')
            if blocks:
                merged[dom] = blocks
        if merged:
            # 未被 DB 覆盖的域仍走文件包/外部目录/种子
            other = _load_kb_from_dirs()
            for dom, blocks in other.items():
                merged.setdefault(dom, blocks)
            data = merged
        else:
            data = _load_kb_from_dirs()
    else:
        # 1) 行业包知识目录（P1 起为唯一真相源）→ 2) 外部目录 → 3) 内嵌种子
        data = _load_kb_from_dirs()

    with _KB_LOCK:
        _KB_CACHE['key'] = key
        _KB_CACHE['data'] = data
    return data


def _dir_fingerprint(path: str) -> tuple:
    """目录内文件指纹（相对路径 + mtime_ns + size）；目录不存在返回空元组。"""
    if not path or not os.path.isdir(path):
        return ()
    items = []
    for root, _dirs, files in os.walk(path):
        for fn in files:
            if not fn.endswith('.md'):
                continue
            fp = os.path.join(root, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            items.append((os.path.relpath(fp, path), st.st_mtime_ns, st.st_size))
    return tuple(sorted(items))


def _kb_cache_key() -> tuple:
    """知识库缓存键：激活包 + DB 内联知识 + 文件目录指纹。"""
    inline = industry_pack.pack_knowledge_inline()
    external = os.environ.get('KNOWLEDGE_BASE_DIR') or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', '部署', 'knowledge_base')
    return (
        industry_pack.active_pack_id(),
        tuple(sorted(inline.items())),
        _dir_fingerprint(industry_pack.pack_knowledge_dir() or ''),
        _dir_fingerprint(external),
    )


def _load_kb_from_dirs() -> dict:
    """文件层知识库加载（包知识目录 → 外部目录 → 内嵌种子）。"""
    pack_dir = industry_pack.pack_knowledge_dir()
    if pack_dir:
        parsed = _load_from_dir(pack_dir)
        if parsed:
            return parsed
    base = os.path.dirname(os.path.abspath(__file__))
    kb_dir = os.environ.get('KNOWLEDGE_BASE_DIR') or os.path.join(
        os.path.abspath(os.path.join(base, '..', '部署', 'knowledge_base')))
    if os.path.isdir(kb_dir):
        parsed = _load_from_dir(kb_dir)
        if parsed:
            return parsed
    return KB_SEED


def _load_from_dir(kb_dir: str) -> dict:
    """解析目录内 *.md 文件为 {域: 块列表}。

    Args:
        kb_dir: 知识库 markdown 目录路径。

    Returns:
        dict | None: 空目录/全部解析失败时返回 None 回退种子。
    """
    got = {}
    for fn in sorted(os.listdir(kb_dir)):
        if not fn.endswith('.md'):
            continue
        path = os.path.join(kb_dir, fn)
        try:
            with open(path, encoding='utf-8') as f:
                text = f.read()
        except OSError:
            continue
        domain = _infer_domain(text, fn)
        blocks = _split_blocks(text)
        if domain and blocks:
            got[domain] = blocks
    return got or None


def _infer_domain(text: str, filename: str) -> str:
    """从文件名/正文推断知识域（P1：以当前行业包的域表为准）。

    三级判定：包内显式声明的 knowledge_file → 文件名含域名 → 标题含域名；
    全部未命中时回退内嵌奶茶域表，保证旧目录（部署/knowledge_base）仍可用。
    """
    for d in industry_pack.pack_domain_defs():
        if d.get('knowledge_file') and d['knowledge_file'] == filename:
            return d['name']
    domains = industry_pack.pack_domains() or list(KB_SEED)
    name_hits = [d for d in domains if d in filename]
    if name_hits:
        return name_hits[0]
    title = text.splitlines()[0] if text.splitlines() else ''
    for d in domains:
        if d in title:
            return d
    for d in KB_SEED:
        if d in title:
            return d
    return ''


def _split_blocks(text: str) -> list:
    """把 md 文本切成可作为检索单元的「标题块」。

    Args:
        text: 一篇知识库 md 全文。

    Returns:
        list[tuple[str, str]]: [(小节标题, 小节内容文本), ...]。
    """
    blocks = []
    cur_title = ''
    cur_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r'^#{1,4}\s+', line):  # 新节（#/##/###）
            if cur_title or cur_lines:
                blocks.append((cur_title, ' '.join(t for t in cur_lines if t)))
            cur_title = re.sub(r'^#{1,4}\s*', '', stripped)
            cur_lines = []
        else:
            cleaned = re.sub(r'^\s*[-*•]|^\s*\d+[.、)]\s*', '', stripped)
            if '|' in cleaned:  # 表格行：取前两列（描述性文本）降噪
                cols = [c.strip() for c in cleaned.split('|') if c.strip()]
                cleaned = ' '.join(cols[:2])
            if cleaned:
                cur_lines.append(cleaned)
    if cur_title or cur_lines:
        blocks.append((cur_title, ' '.join(t for t in cur_lines if t)))
    return [(t, c) for t, c in blocks if c]


def _bigrams(text: str) -> list:
    """中文连续 2 字 Bigram + 裸露数字/英文词，用作检索特征。"""
    grams = []
    cn = re.findall(r'[\u4e00-\u9fa5]{2}', text or '')
    grams.extend(cn)
    en = re.findall(r'(?<![A-Za-z])[A-Za-z]{2,}(?![A-Za-z])', text or '', re.IGNORECASE)
    grams.extend(w.lower() for w in en)
    return grams


def _corpus_blocks() -> list:
    """展开知识库全部知识块，供 IDF 统计使用。

    Returns:
        list[tuple]: [(域, 标题, 正文), ...]。
    """
    kb = _load_kb()
    return [(dom, title, text) for dom, blocks in kb.items() for title, text in blocks]


def _idf_map() -> dict:
    """语料级 IDF 权重：稀有 Bigram 权重更高（第四轮 D4）。

    纯字面共现会让「的的/一杯」这类高频字组稀释区分度，IDF 让
    真正有信息量的词（如「西柚」「效期」）主导排序。

    Returns:
        dict: {bigram: idf 值}，空语料返回空字典。
    """
    blocks = _corpus_blocks()
    # 语料指纹直接复用知识库缓存键：md 文件/DB 内联知识任一变化即重算。
    key = _kb_cache_key()
    with _KB_LOCK:
        if _IDF_CACHE['key'] == key and _IDF_CACHE['map']:
            return _IDF_CACHE['map']

    doc_freq = {}
    total = 0
    for _dom, title, text in blocks:
        total += 1
        for g in set(_bigrams(title) + _bigrams(text)):
            doc_freq[g] = doc_freq.get(g, 0) + 1
    if not total:
        return {}
    idf = {g: math.log(1 + total / df) for g, df in doc_freq.items()}
    with _KB_LOCK:
        _IDF_CACHE['key'] = key
        _IDF_CACHE['map'] = idf
    return idf


# ── 检索反馈：读写轻量 JSON，失败一律不影响检索主流程 ──
def _load_feedback() -> dict:
    """读取反馈计数；文件缺失/损坏返回空字典。"""
    try:
        with open(FEEDBACK_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_feedback(data: dict) -> bool:
    """原子写入反馈计数（先写临时文件再替换）。

    Args:
        data: 反馈字典。

    Returns:
        bool: 写入成功为 True；目录不可写等异常返回 False。
    """
    try:
        os.makedirs(os.path.dirname(FEEDBACK_FILE), exist_ok=True)
        tmp = f'{FEEDBACK_FILE}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FEEDBACK_FILE)
        return True
    except OSError:
        return False


def feedback_key(domain: str, title: str) -> str:
    """知识块的反馈主键。"""
    return f'{domain}|{title}'


def mark_useful(domain: str, title: str, useful: bool = True) -> dict:
    """记录一次检索反馈（有用/没用），用于后续检索加权。

    Args:
        domain: 知识域。
        title: 知识块标题。
        useful: True 记「有用」，False 记「没用」。

    Returns:
        dict: {'ok': bool, 'weight': float, 'useful': int, 'useless': int}。
    """
    key = feedback_key(domain or '', title or '')
    data = _load_feedback()
    item = data.get(key) or {'useful': 0, 'useless': 0}
    item['useful'] = int(item.get('useful', 0)) + (1 if useful else 0)
    item['useless'] = int(item.get('useless', 0)) + (0 if useful else 1)
    data[key] = item
    ok = _save_feedback(data)
    return {'ok': ok, 'weight': _feedback_weight(domain, title),
            'useful': item['useful'], 'useless': item['useless']}


def _feedback_weight(domain: str, title: str) -> float:
    """知识块的反馈权重（1.0 为中性）。"""
    return _feedback_weight_from_data(_load_feedback(), domain, title)


def _feedback_weight_from_data(data: dict, domain: str, title: str) -> float:
    """从已加载反馈字典计算权重，避免检索循环内按块重复读盘。"""
    item = (data or {}).get(feedback_key(domain or '', title or ''))
    if not item:
        return 1.0
    delta = int(item.get('useful', 0)) - int(item.get('useless', 0))
    return max(FEEDBACK_MIN, min(FEEDBACK_MAX, 1.0 + delta * FEEDBACK_STEP))


def _score(text: str, query_grams: list, domain_boost: str = '',
           idf: dict | None = None, query: str = '') -> float:
    """对片段文本打分：IDF 加权共现 + 查询覆盖率 + 短语命中 + 反馈权重。

    相比纯共现计数（旧版），本版三点增强：
      1. **IDF 加权**：稀有词贡献更大，抑制高频字组稀释；
      2. **覆盖率归一**：命中查询词越全分越高，避免「只蹭到一个词」上位；
      3. **短语命中**：查询原文（≥3 字）整段出现时额外加成。

    Args:
        text: 待评分片段文本。
        query_grams: 查询的 Bigram 特征列表。
        domain_boost: 已聚焦的知识域（该域片段额外加权）。
        idf: 预计算的 IDF 权重表；为空退化为等权共现。
        query: 归一化后的查询原文（短语命中判定用）。

    Returns:
        float: 分数（越高越相关）。
    """
    grams = _bigrams(text)
    if not grams or not query_grams:
        return 0.0
    idf = idf or {}
    score = 0.0
    hit_grams = set()
    for qg in query_grams:
        cnt = grams.count(qg)
        if cnt:
            hit_grams.add(qg)
            score += cnt * idf.get(qg, 1.0)
    if not hit_grams:
        return 0.0
    # 长文本偏差修正：按片段长度归一（经验系数，避免大块恒赢）
    score = score / (1 + len(grams) / 60.0)
    # 覆盖率：命中查询词占比越高越可信（保留 0.4 底分，避免长查询被过度惩罚）
    coverage = len(hit_grams) / len(set(query_grams))
    score *= (0.4 + 0.6 * coverage)
    # 短语命中：查询原文整段出现在片段中，说明高度相关
    if query and len(query) >= 3 and query in text:
        score *= 1.25
    return score


def dedupe(items, key):
    seen, out = set(), []
    for it in items:
        k = key(it)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def search(query: str, top_k: int = 3, domain: str = '') -> list:
    """全库检索最相关知识块。

    Args:
        query: 待检索的查询文本（如经营问题描述）。
        top_k: 返回条数上限。
        domain: 可选知识域聚焦（取自 WEAK_DOMAIN_MAP 的域全名）。

    Returns:
        list[dict]: [{'domain','title','text','score'}]，按相关度降序。
    """
    query = _normalize_query(query)
    if not query:
        return []
    grams = _bigrams(query)
    if not grams:
        return []
    kb = _load_kb()
    idf = _idf_map()
    feedback = _load_feedback()
    scored = []
    for dom, blocks in kb.items():
        focus = (dom == domain)
        for title, text in blocks:
            # 标题加权：正文命中 + 标题命中 ×0.5（小标题更能定位知识点）
            s = (_score(text, grams, idf=idf, query=query)
                 + _score(title, grams, idf=idf, query=query) * 0.5)
            if s <= 0:
                continue
            if focus:
                s *= 1.5
            # 检索反馈权重：被标记「有用」的知识块后续更易命中
            weight = _feedback_weight_from_data(feedback, dom, title)
            s *= weight
            scored.append({'domain': dom, 'title': title, 'text': text,
                           'score': round(s, 3), 'weight': round(weight, 2)})
    scored.sort(key=lambda d: -d['score'])
    return scored[:top_k]


def build_context(query: str, top_k: int = 3, domain: str = '') -> str:
    """把检索结果拼成可直接注入 prompt 的上下文文本（RAG 增强用）。

    Args:
        query: 查询文本。
        top_k: 检索条数。
        domain: 域聚焦。

    Returns:
        str: 「【参考知识库】…原文引用…」上下文；无命中返回空串。
    """
    hits = search(query, top_k=top_k, domain=domain)
    if not hits:
        return ''
    parts = ['以下门店知识库原文，仅可引用其中事实作答：']
    for h in hits:
        parts.append(f"-《{h['domain']}·{h['title']}》{h['text']}")
    return '\n'.join(parts)


def rule_answer(query: str, top_k: int = 2, domain: str = '') -> str:
    """规则版：引述知识库原文生成回答（知识库驱动，杜绝凭空捏造）。

    Args:
        query: 用户问题。
        top_k: 引述条数。
        domain: 域聚焦。

    Returns:
        str: 回答文案；无命中返回提示。
    """
    hits = search(query, top_k=top_k, domain=domain)
    if not hits:
        return '这个问题暂无知识库条目，建议询问店长。'
    lines = ['已为您调取门店标准知识库：']
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. [{h['domain']}] {h['title']}：{h['text']}")
    return '\n'.join(lines)


def focus_domain(weak_label: str) -> str:
    """业务弱项标签 → 知识库域。

    Args:
        weak_label: 页面/题库使用的弱项标签（如 '原料'）。

    Returns:
        str: 知识库域全名；无映射返回空串。
    """
    for kw, dom in (industry_pack.pack_domain_map() or WEAK_DOMAIN_MAP).items():
        if kw in (weak_label or ''):
            return dom
    for dom in (industry_pack.pack_domains() or list(KB_SEED)):
        if weak_label and weak_label in dom:
            return dom
    return ''


# 自适应难度的题面要求（第六轮从 dify_client 收口至此：难度提示语是题面
# 语义的一部分，出题（dify_client）与掌握度难度加权（learner_model）
# 共用同一份真相源，防两处文案漂移导致加权失配）
DIFFICULTY_HINTS = {
    'basic': '（提示：答出关键要点即可）',
    'standard': '',
    'advanced': '（要求：请一并说明操作要点与例外情况）',
}

# 薄弱点归因关键词兜底（业务侧共用口径，F18）：
# h5 培训进度 / AI 考核统计 / AI 个人画像共用同一份词表与「首词命中即归因」规则；
# 顺序即优先级：题干含多个关键词时按本序首个命中归因。
# P7-E3-B / M-1：字面量收敛到内置兜底包（`industry_pack.FALLBACK_WEAK_KEYWORDS`），
# 运行时应优先取 `industry_pack.pack_weak_keywords()`，此处仅作包不可用时的最后防线。
WEAK_KEYWORDS = industry_pack.FALLBACK_WEAK_KEYWORDS


def weak_domain_counts(questions) -> dict:
    """题干关键词 → 知识域计数（薄弱点归因唯一实现）。

    Args:
        questions: 题干文本序列（含 None 容忍）。

    Returns:
        dict: {知识域全名: 命中次数}，仅统计能映射到知识库域的关键词。
    """
    counts: dict = {}
    for q in questions or []:
        text = q or ''
        for kw in (industry_pack.pack_weak_keywords() or WEAK_KEYWORDS):
            if kw in text:
                dom = focus_domain(kw)
                if dom:
                    counts[dom] = counts.get(dom, 0) + 1
                break
    return counts


def topic_suggestion(weak_labels) -> list:
    """薄弱知识点 → 复习建议（上岗考核补训用）。

    Args:
        weak_labels: 薄弱点标签列表。

    Returns:
        list[str]: 去重后的复习建议文案。
    """
    tip_map = industry_pack.pack_domain_tips() or DOMAIN_TIP
    tips = []
    for label in weak_labels or []:
        dom = focus_domain(label)
        if dom and dom in tip_map:
            tips.append(tip_map[dom])
    return dedupe(tips, key=str)
