"""在线 LLM 统一集成客户端（步骤4 核心；第七轮起多后端可插拔）

把门店业务 AI 能力统一封装：
  - 看板备货建议 / 损耗提醒（AC-OBS-07/08）
  - 问答模式出题 / 点评（AC-OBS-11）
  - 情景对话（AI 扮顾客）（AC-OBS-12）
  - 上岗考核判定 / 薄弱点统计（AC-OBS-13/14）

后端可插拔（LLM_PROVIDER）：
  - 'auto'（默认）：配了 DIFY_*_KEY 走 Dify，否则配了 DEEPSEEK_API_KEY
    走 DeepSeek 云端 API（OpenAI 兼容 /chat/completions）；
  - 'dify' / 'deepseek'：强制指定后端；
  - 'none'：强制离线（规则版）。
DIFY_ENABLED=False 为总开关，压住一切后端（离线演示/CI 稳定）。

未启用或调用失败时，所有方法自动回退到「规则版」或降级提示，
返回 content + source='rule'/'dify-error'，保证应用离线也能完整演示。

接入方式（DeepSeek 最简路径）：.env 设置 DEEPSEEK_API_KEY 即可，
source 自动变为 'dify'（统一出口标记），无需改任何业务代码；
私有化 Dify 路线保留为生产演进选项（部署/README.md）。

防腐层约定（F7）：
  1. 所有对外网/内网 LLM 的调用异常，统一由 _fail() 收口：
     记录 logging.exception 到服务端日志，对上层只返回
     '[DIFY_ERROR] …' 标记串，绝不把堆栈、内网 IP、Key 透传到前端；
  2. 上层用 is_dify_error() 判断是否失败，失败一律不得当作有效 AI 结果；
  3. enabled 判断只在本模块内做（单一真相源，F16），路由层不再重复判断。
"""
import json
import logging
import math
import os
import re
import time

try:
    import requests
except ImportError:
    requests = None

from src import ai_gate, ai_trace, industry_pack, metrics, negotiation

# RAG 知识库检索层（轻量，纯标准库；供业务方法与 prompt 注入检索上下文）
from src.ai_explain import explain_business, summarize_confidence

# P7-E3-B3 / M-11：意图词表单一真相源（路由分支与轨迹共用，禁再内联副本）。
# 模糊路由数据（示例句/锚词/阈值）亦收敛于此，本模块仅保留算法（依赖判分归一文本工具）。
from src.intent_rules import (
    INTENT_ANCHORS,
    INTENT_EXEMPLARS,
    INTENT_FUZZY_THRESHOLD,
    match_intent,
)
from src.knowledge_base import (
    DIFFICULTY_HINTS,
    build_context,
    focus_domain,
    rule_answer,
    topic_suggestion,
)
from src.knowledge_base import (
    search as kb_search,
)

# 规则版文案与经营数据小工具（Q2 切片：纯搬运，详见 src/rule_copy.py）
from src.rule_copy import (
    _compare_text,
    _forecast_text,
    _product_lookup,
    _rule_business_report,
    _trend_desc,
    _waste_products,
    _weekday_text,
)

logger = logging.getLogger(__name__)

# 连接阶段超时上限（秒）：与读取上限分开给，避免标量 timeout 把同一数值同时用于
# connect 与 read，导致最坏墙钟变成 2 倍（见 DifyClient.timeouts 文档）。
# 允许用环境变量收紧，但不允许超过读取上限（取 min，见 timeouts 属性）。
CONNECT_TIMEOUT_CEILING = float(os.environ.get('DIFY_CONNECT_TIMEOUT', '5'))


class CircuitBreaker:
    """轻量熔断器（D3）：连续失败达到阈值后进入 OPEN 状态，在冷却窗口内快速失败，
    避免对故障 Dify 反复打请求拖垮应用；冷却结束后半开试探，成功则复位。

    Attributes:
        threshold: 连续失败达到此数即熔断。
        cooldown: 熔断后多久（秒）才允许半开试探。
    """

    def __init__(self, threshold=3, cooldown=30.0):
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._opened_at = 0.0

    def allow(self):
        """是否允许本次请求。

        Returns:
            bool: 未熔断或已过冷却窗口返回 True；熔断窗口内返回 False。
        """
        if self._failures >= self.threshold:
            if time.time() - self._opened_at < self.cooldown:
                return False
            # 冷却结束，半开试探：复位失败计数，放行一次请求
            self._failures = 0
        return True

    def on_success(self):
        self._failures = 0
        self._opened_at = 0.0

    def on_failure(self):
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = time.time()

# 统一失败标记前缀：上层（路由/模板）据此区分「AI 建议」与「AI 异常」
DIFY_ERROR_PREFIX = '[DIFY_ERROR]'
# 并发闸饱和专用前缀（H8，第 9 轮）：仍是「失败」（走同一降级链路），但原因可归因 ——
# 上游故障与自家限流保护对用户是两回事，混在一起用户无从判断该等一会还是该报错。
AI_SATURATED_PREFIX = '[AI_SATURATED]'
# 历史版本失败串前缀，保留识别能力以兼容旧会话/旧留痕数据
LEGACY_ERROR_PREFIX = '[Dify 调用失败'
# 对用户可见的统一降级文案（不含任何技术细节）
DIFY_UNAVAILABLE_TEXT = 'AI 服务暂不可用，已切换为演示模式，请稍后重试。'
# 同一文案的短标签形态（徽章用）：由长文案派生，杜绝第二份字面量（P7-E3-B2 / M-12）。
# 模板一律经路由注入（`degraded_text` / `degraded_badge`），不再内联副本 ——
# 此前 4 处内联「AI 服务暂不可用，请稍后重试。」少了「已切换为演示模式」半句。
DIFY_UNAVAILABLE_BADGE = DIFY_UNAVAILABLE_TEXT.split('，')[0]
# 并发闸饱和的用户可见文案（H8）：说清「是本机限流保护、稍后再试即可」，
# 与上游故障的「AI 服务暂不可用」区分开。中文、无技术细节、不含队列内部参数。
SATURATED_TEXT = 'AI 分析请求较多，本次已切换为规则版结果；稍等片刻重试即可获得大模型分析。'


def is_dify_error(text):
    """判断一段文本是否为 AI 调用失败标记串（含并发闸饱和）。

    Args:
        text: 待判断的文本，允许 None。

    Returns:
        bool: 命中 [DIFY_ERROR] / [AI_SATURATED] 或历史 [Dify 调用失败 前缀时为 True。
            饱和同样算「失败」，好让既有降级链路（规则版回退、脱敏文案）零改动继续生效；
            要单独识别饱和请用 `is_ai_saturated()`。
    """
    if not isinstance(text, str):
        return False
    return text.lstrip().startswith((DIFY_ERROR_PREFIX, AI_SATURATED_PREFIX, LEGACY_ERROR_PREFIX))


def is_ai_saturated(text):
    """是否属于「并发闸饱和」这一可归因降级（而非上游故障）。

    上层（驾驶舱、路由文案）据此把「等一会再试」与「服务挂了」区分开。
    """
    return isinstance(text, str) and text.lstrip().startswith(AI_SATURATED_PREFIX)


def _saturated(scene):
    """并发闸已满的失败出口：这是背压不是上游故障，故绝不调 breaker.on_failure()。

    用独立前缀而非复用 `[DIFY_ERROR]`，是为了让「为什么变简」可归因（H8）：
    限流保护下用户该等一会儿，上游故障下该去查配置——两者给同一句话都是误导。
    `is_dify_error()` 同时认这两个前缀，故既有降级链路（规则版回退）零改动生效。

    Args:
        scene: 场景标识（'deepseek' / 'chat' / 'completion'），仅写服务端日志。

    Returns:
        str: 形如 '[AI_SATURATED] …' 的脱敏标记串（不含密钥、内网地址或队列细节）。
    """
    logger.warning('AI 并发闸已满 scene=%s，本次直接降级（不排队、不占线程）', scene)
    return f'{AI_SATURATED_PREFIX} {SATURATED_TEXT}'



def _fail(scene, exc):
    """统一失败出口：服务端记日志，前端只拿到脱敏标记串。

    Args:
        scene: 失败场景标识，用于日志定位（如 'chat' / 'completion'）。
        exc: 捕获到的异常对象。

    Returns:
        str: 形如 '[DIFY_ERROR] AI 服务暂不可用…' 的脱敏标记串。
    """
    logger.exception('Dify 调用失败 scene=%s type=%s', scene, type(exc).__name__)
    return f'{DIFY_ERROR_PREFIX} {DIFY_UNAVAILABLE_TEXT}'


# ─────────────────────────────────────────────────────────────
# 规则版知识（无 Dify 时的回退内容，与 RAG 知识库同源）
# ─────────────────────────────────────────────────────────────
KNOWLEDGE_QA = [
    # —— 原料规范域 ——
    {
        "q": "本店招牌杨枝甘露的标准配料是什么？",
        "a": "西米、芒果、椰浆、西柚粒。芒果需选成熟金煌芒，西米煮至透明无白芯，椰浆比例 1:3 兑水。",
        "weak": "原料规范",
    },
    {
        "q": "珍珠（粉圆）煮好后最佳保存时长是？",
        "a": "煮好过冰水后用糖水浸泡，常温 4 小时内、冷藏不超过 24 小时；"
             "超过时间发硬发酸必须丢弃，禁止售卖。",
        "weak": "原料规范",
    },
    {
        "q": "鲜奶开封后在冷藏 2-6℃ 条件下的最长使用时间是？",
        "a": "开封后 48 小时内用完；超过效期或出现异味、胀包立即停用并上报，"
             "严禁继续使用。",
        "weak": "原料规范",
    },
    {
        "q": "每日盘点的损耗率超过多少视为异常，需查明原因？",
        "a": "损耗率 = 损耗量 /（剩余量 + 损耗量），超过 15% 即视为异常，"
             "需查明是备货过多、储存不当还是操作浪费，并少备勤补易损鲜果、鲜奶。",
        "weak": "原料规范",
    },
    # —— 异议处理域 ——
    {
        "q": "顾客嫌价格贵要砍价，正确话术是？",
        "a": "先共情不硬拒，强调真材实料的价值；给替代而非降价"
             "（推荐柠檬水等低价走量品）；再引导会员优惠（周三第二杯半价），不轻易破价。",
        "weak": "异议处理",
    },
    {
        "q": "顾客催单说「怎么这么慢」，你该怎么回应？",
        "a": "致歉 + 说明 + 安抚：说明现做饮品为保证口感需要约 2 分钟，马上好；"
             "高峰提前告知预计时长管理预期，绝不催促。",
        "weak": "异议处理",
    },
    {
        "q": "顾客质疑卫生（「干净吗」），你怎么回答？",
        "a": "出示操作规范：原料每日清点和效期管理、操作台每班次消毒，可以放心；"
             "同时主动提供冰量调整等选项。",
        "weak": "异议处理",
    },
    {
        "q": "顾客说「上一杯太甜了」，本杯怎么处理？",
        "a": "先致歉不辩解，立刻重做一杯（调整糖度）或换等价产品，原杯不收费；"
             "记录口味偏好，下次默认调整。",
        "weak": "异议处理",
    },
    # —— 套餐价格域 ——
    {
        "q": "套餐「双人欢享」的具体内容是什么？",
        "a": "2 杯杨枝甘露 = ¥32（单买 ¥36 省 ¥4）；学生特惠全场 9 折不与套餐同享；"
             "会员日每周三第二杯半价。",
        "weak": "套餐价格",
    },
    {
        "q": "顾客问饮品价时，报价话术要点是什么？",
        "a": "先报主产品价，再自然带出「加 ¥2 可加珍珠/波波、加 ¥3 可加芋泥」等增值选项；"
             "提及优惠必须先说门槛，避免事后纠纷。",
        "weak": "套餐价格",
    },
    {
        "q": "杯型升级或换鲜奶的差价怎么算？",
        "a": "加珍珠/波波 +¥2、加芋泥 +¥3、换鲜奶 +¥2；升级杯型/换奶先报差价再操作，避免结账争议。",
        "weak": "套餐价格",
    },
    {
        "q": "顾客问「有没有会员价以外的便宜方式」怎么答？",
        "a": "学生特惠全场 9 折（不与套餐同享）、午后搭档任意奶茶+柠檬水立减 ¥3；"
             "强调优惠先告知门槛再推荐，明码标价不临场改价。",
        "weak": "套餐价格",
    },
    # —— 服务话术域 ——
    {
        "q": "点单确认订单时，标准服务话术是什么？",
        "a": "复述订单确认「您点的是 1 杯珍珠奶茶、1 杯柠檬水，对吗？」；"
             "主动询问甜度冰量（默认七分糖、少冰）；关联推荐加料更丰富。",
        "weak": "服务话术",
    },
    {
        "q": "杨枝甘露交付给顾客时应提醒什么？",
        "a": "提醒小心烫/小心洒，并提示含芒果，过敏顾客请留意；"
             "过敏体质顾客点单时主动告知。",
        "weak": "服务话术",
    },
    {
        "q": "高峰排队时如何安抚等待顾客？",
        "a": "主动告知预计等待时长管理预期；制作中间顾客示意「马上好」；"
             "不催促、不敷衍，出错先致歉再补救。",
        "weak": "服务话术",
    },
    {
        "q": "顾客点完单后还有哪些提升体验的动作？",
        "a": "复述订单确认、关联推荐加料、交付提醒「小心烫/小心洒」、"
             "送客引导会员与下次光临，形成迎客到送客的完整服务闭环。",
        "weak": "服务话术",
    },
]

# ─────────────────────────────────────────────────────────────
# 规则版场景池与角色卡（无 Dify 时的回退内容，与 RAG 知识库同源）
# ─────────────────────────────────────────────────────────────
SCENARIO_POOL = {
    "bargain": [  # 砍价
        "你们家比隔壁贵三块呢，便宜点嘛，我经常来的。",
        "行吧行吧，那再送我个加料呗？",
        "那行，给我来一杯，记得糖少点啊。",
        "对了，办你们的会员卡有没有什么优惠？",
    ],
    "safety": [  # 担心安全/卫生
        "你这珍珠看着不太新鲜啊，不会放很久了吧？",
        "芒果是今天的吗？我有点怕不新鲜拉肚子。",
        "杯子干净不？我刚才看好像没消毒。",
        "你们这操作台怎么看着有点乱，卫生没问题吧？",
    ],
    "pick": [  # 挑剔服务
        "怎么这么慢，我都等十分钟了！",
        "你这杯颜色不对吧，是不是做错了？",
        "我要的无糖你给我全糖？你们培训过的吗？",
        "态度这么敷衍，下次谁还来你家！",
    ],
    "rush": [  # 催单/出餐慢
        "还有多久好啊？我这赶时间呢！",
        "前面那杯也是我的吧？做完这杯先给我吧？",
        "你们后厨是不是在偷懒啊，怎么这么慢？",
        "都没人排队，做一杯要十分钟？",
    ],
    "delivery": [  # 外卖与自提
        "我点了外卖，备注说少冰，到底做了没有？",
        "外卖单是不是漏做了我的？都超时了！",
        "自提的餐好了吗？我扫码的那个杨枝甘露。",
    ],
    "member": [  # 会员优惠咨询
        "你们会员日是什么时候？优惠力度大不大？",
        "我加会员能送什么？每周三第二杯半价是本身会员价吗？",
        "学生证能打折吗？跟会员优惠能不能叠加？",
    ],
}

SCENARIO_ROLES = {  # 顾客角色卡（情绪/意图提示，税务专属文案）
    "bargain": ("老顾客·陈姐", "想省点钱，说话带笑但立场坚定", "低价购入招牌饮品，顺便拿点加料"),
    "safety": ("谨慎妈妈·李女士", "担心食品安全，语气迟疑较真", "确认原料新鲜、操作卫生后购买"),
    "pick": ("急性子·小王", "等得不耐烦，语带不满", "拿到一杯做得对且快的饮品"),
    "rush": ("赶时间·阿龙", "上班前赶时间，语气急切", "尽可能快拿到饮品，故关注效率"),
    "delivery": ("点单用户·林子", "外卖等待中，略烦躁", "确认外卖/自提订单正确出餐"),
    "member": ("新客·小芸", "想薅羊毛，好奇优惠规则", "问清会员/学生优惠并下单"),
}

# 情景类型白名单（F11：路由层入参校验的唯一真相源）
SCENARIO_TYPES = tuple(SCENARIO_POOL.keys())
DEFAULT_SCENARIO = 'bargain'
SCENARIO_LABELS = {
    'bargain': '砍价',
    'safety': '卫生担忧',
    'pick': '挑剔服务',
    'rush': '催单/出餐慢',
    'delivery': '外卖与自提',
    'member': '会员优惠咨询',
}

# ─────────────────────────────────────────────────────────────
# 顾客情绪状态机（第五轮 D1）：店员表现驱动顾客反应
# ─────────────────────────────────────────────────────────────
# 旧版规则模式：顾客台词只按轮次顺序推进，店员说什么都一样 → 演练感弱。
# 新增状态机：耐心值（0~100）由店员发言的语气规则驱动，决定顾客情绪
# 与台词修饰/结束语，让「AI 顾客」真实感知店员的服务表现。
CUSTOMER_PATIENCE_START = 70.0   # 初始耐心（中性）
PATIENCE_MIN, PATIENCE_MAX = 0.0, 100.0

# 语气规则（顺序即优先级，每轮发言只命中最靠前的一条）：
# 负面强信号优先——一句对抗话足以盖过同轮的致歉措辞。
TONE_RULES = (
    (-25.0, ('爱买不买', '随便你', '快点吧', '别磨叽', '急什么', '你爱')),
    (-15.0, ('不知道', '不归我管', '没办法', '自己看', '就这样', '嫌贵别买')),
    (+10.0, ('抱歉', '不好意思', '对不起', '请稍等', '您别急', '给您添麻烦')),
    (+8.0, ('马上', '立刻', '重做', '换一杯', '送您', '优惠', '会员', '帮您')),
    (+5.0, ('复述', '确认', '七分糖', '少冰', '去冰', '您点的是')),
)

# 耐心值 → 情绪档位（阈值降序，首个命中即返回）
MOOD_LEVELS = (
    (85.0, 'satisfied'),
    (60.0, 'pleased'),
    (35.0, 'neutral'),
    (15.0, 'annoyed'),
    (0.0, 'angry'),
)
MOOD_LABELS = {
    'satisfied': '满意', 'pleased': '较好', 'neutral': '一般',
    'annoyed': '不满', 'angry': '愤怒',
}
# 台词尾随的情绪修饰（neutral 不加，保持原台词干净）
MOOD_DECOR = {
    'satisfied': '（顾客明显放松了，语气缓和）',
    'pleased': '（顾客神色缓和了些）',
    'neutral': '',
    'annoyed': '（顾客皱了皱眉，不太满意）',
    'angry': '（顾客明显不耐烦了，语气变差）',
}
# 台词池用尽后的结束语：按最终情绪分化（满意/中性/流失）
ENDING_LINES = {
    'satisfied': '行，就冲你这服务态度，以后还来。',
    'pleased': '嗯，那行吧，你这服务还行。',
    'neutral': '嗯，那行吧。',
    'annoyed': '算了算了，下次再说吧。',
    'angry': '不买了！你们这态度也太差了。',
}

# ─────────────────────────────────────────────────────────────
# 动态谈判结论台词（P5）：成交/流失由 negotiation 状态机判定，
# 优先于台词池推进 —— 让「谈成了没有」真正由店员话术决定。
# ─────────────────────────────────────────────────────────────
NEG_DEAL_LINES = {
    'satisfied': '行，就冲你这态度和价格，以后还来，给我来一杯。',
    'pleased': '嗯，那就按你说的来，给我来一杯吧。',
    'neutral': '好吧，就这个价，来一杯。',
    'annoyed': '……行吧，就这一次啊。',
    'angry': '算了，就按你的价来一杯，下次不一定了。',
}
NEG_LOST_LINES = {
    'satisfied': '今天先不买了，改天有空再来。',
    'pleased': '那我再想想吧。',
    'neutral': '算了，我去别家看看。',
    'annoyed': '不买了，你们这价格实在谈不下来。',
    'angry': '不买了！你们这态度也太差了。',
}

# 在售产品白名单（智能助手「具体产品」问询唯一真相源，同时防注入）
PRODUCT_NAMES = ('杨枝甘露', '珍珠奶茶', '烧仙草', '柠檬水', '芋泥波波')

# 判分及格线（与 analytics 展示口径一致）
PASS_SCORE = 60

# ─────────────────────────────────────────────────────────────
# 行业包内容桥接（P1）：内容优先取当前行业包，缺失回退内嵌常量
#   —— 只读幂等，不改变任何业务方法签名；新增行业包无需改代码
# ─────────────────────────────────────────────────────────────
_FALLBACK_PERSONA = {
    'assistant_persona': '你是奶茶店经营助手',
    'analyst_persona': '你是奶茶店经营分析师',
    'trainer_persona': '你是奶茶店培训教练',
}


def pack_persona(key):
    """按当前行业包术语表取 AI 人设前缀（缺失回退内嵌奶茶文案）。"""
    return industry_pack.pack_terms().get(key) or _FALLBACK_PERSONA.get(key, '')


def pack_questions():
    """规则题库：当前行业包优先，缺失回退内嵌 KNOWLEDGE_QA。"""
    return industry_pack.pack_questions() or KNOWLEDGE_QA


def pack_product_names():
    """在售产品白名单：当前行业包优先，缺失回退内嵌 PRODUCT_NAMES。"""
    names = tuple(p['product_name'] for p in industry_pack.pack_products())
    return names or PRODUCT_NAMES


def pack_material_domain():
    """仓储/损耗类知识域（按当前包关键词解析；空串 = 不聚焦）。"""
    return focus_domain('损耗') or focus_domain('原料') or ''


def pack_units() -> tuple:
    """当前包的（数量单位, 原料单位）——规则版文案与 prompt 随包口径（P7-E3-B2b）。

    走 `metrics.dashboard_labels()` 单一真相源（与看板/H5 表单同源）；无应用上下文
    时该函数自行回退内置兜底包，不抛异常。
    """
    labels = metrics.dashboard_labels()
    return labels['unit'], labels['material_unit']


def pack_service_domain():
    """服务/异议类知识域（按当前包关键词解析；空串 = 不聚焦）。"""
    return focus_domain('异议') or focus_domain('话术') or ''


def scenario_catalog():
    """情景剧本目录 {pool, roles, labels, default, types}；包优先、内嵌回退。"""
    pack = industry_pack.pack_scenarios()
    pool = pack.get('pool') or SCENARIO_POOL
    default = pack.get('default') or DEFAULT_SCENARIO
    if default not in pool:
        default = next(iter(pool), DEFAULT_SCENARIO)
    return {
        'pool': pool,
        'roles': pack.get('roles') or SCENARIO_ROLES,
        'labels': pack.get('labels') or SCENARIO_LABELS,
        'default': default,
        'types': tuple(pool.keys()),
    }

# 自适应难度的题面要求（第四轮 D1）：常量已收口至 src/knowledge_base.py
#（第六轮：learner_model 难度加权与出题共用同一份真相源，防止文案漂移）

# ─────────────────────────────────────────────────────────────
# 判分语义增强：业务同义词组 / 否定词 / 数字实体
# ─────────────────────────────────────────────────────────────
# 同义词组：组内全部归一为组首代表词（作答案与参考答案都归一后再比对）
SYNONYM_GROUPS = [
    ('顾客', ('客户', '客人')),
    ('便宜', ('低价', '优惠')),
    ('贵', ('价高', '昂贵')),
    ('保存', ('储存', '存放')),
    ('效期', ('保质期', '保持时间')),
    ('丢弃', ('扔掉', '报废')),
    ('停用', ('禁止', '不用')),
    ('消毒', ('清洁',)),
    ('损耗', ('浪费',)),
    ('备货', ('进货', '采购')),
    ('鲜果', ('水果',)),
    ('过敏', ('忌口',)),
    ('冷藏', ('冷冻',)),
    ('立即', ('马上',)),
    ('严禁', ('禁止',)),
    ('每日', ('每天',)),
    ('检查', ('排查',)),
]

# 否定语义词：词前 6 字内出现 → 该词极性为「否定」
NEG_WORDS = ('不', '没', '别', '勿', '禁止', '严禁', '不得', '避免', '无', '不再')

# 全角标点 → 半角（判分/数字归一共用；dict 形式免等长限制）
_FULL2HALF = str.maketrans({
    '\uFF0C': ',', '\u3002': '.', '\uFF01': '!', '\uFF1F': '?',
    '\uFF1B': ';', '\uFF1A': ':', '\u3001': ',', '\uFF08': '(',
    '\uFF09': ')', '\u3010': '[', '\u3011': ']', '\uFF05': '%',
    '\u201C': '"', '\u201D': '"', '\u2018': "'", '\u2019': "'",
})


def _normalize_terms(text: str) -> str:
    """判分归一：小写 + 全角→半角 + 业务同义词统一为组内代表词。"""
    if not text:
        return ''
    text = text.lower().translate(_FULL2HALF)
    for canon, alts in SYNONYM_GROUPS:
        for a in alts:
            if a in text:
                text = text.replace(a, canon)
    return text


def _cjk_words(text: str) -> set:
    """提取文本中的中文词特征：连续汉字段整体 + 其内 2 字滑窗（不整段吞并）。

    既保留整段核心术语（如「超过效期必须丢弃」），又保证近义改写
    （多/少一字）仍能产生交集，是判分语义化的基础特征集。
    """
    out = set()
    for chunk in re.findall(r'[\u4e00-\u9fa5]+', text or ''):
        if len(chunk) >= 2:
            out.add(chunk)
        for i in range(len(chunk) - 1):
            out.add(chunk[i:i + 2])
    return out


def _split_elements(text: str) -> list:
    """参考答案拆要素句（按 、，；。！？换行）。"""
    return [p.strip() for p in re.split(r'[,，、;；。!！?？\n]', text or '') if p.strip()]


def _around_neg(text: str, word: str) -> bool:
    """word 在 text 中出现位置前 6 字符内是否有否定词（极性判定）。"""
    idx = text.find(word)
    while idx != -1:
        pre = text[max(0, idx - 6):idx]
        if any(n in pre for n in NEG_WORDS):
            return True
        idx = text.find(word, idx + 1)
    return False


def _extract_numbers(text: str) -> set:
    """抽取数字实体（值, 单位），中文百分数已在归一前由调用方处理。"""
    out = set()
    for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(%|小时|分钟|钟|分|天|℃|度|杯|元)?', text or ''):
        out.add((float(m.group(1)), m.group(2) or ''))
    return out


def _numbers_match(ref_norm: str, ans_norm: str) -> bool:
    """参考答案含数字实体时，作答是否含等价数字（仅值匹配，单位宽松）。"""
    ref_nums = _extract_numbers(ref_norm)
    ans_nums = _extract_numbers(ans_norm)
    if not ref_nums:
        return False
    ans_values = {v for v, _ in ans_nums}
    return any(v in ans_values for v, _ in ref_nums)


def _cjk_bigrams(text: str) -> set:
    """中文连续 2 字 Bigram 特征集（仅 CJK 字符），供知识库同义句桥接判定。

    Args:
        text: 待提取文本（数字/标点/英文自动剔除）。

    Returns:
        set[str]: 文本中全部邻接两字组合（长度 <2 时为空集）。
    """
    body = re.sub(r'[^\u4e00-\u9fa5]', '', text or '')
    return {body[i:i + 2] for i in range(max(0, len(body) - 1))}


def _kb_recovery_hit(e_norm: str, ans_norm: str, ans_terms: set, kb_ctx: str,
                     neg_scope: bool) -> str | None:
    """知识库同义桥接兜底（第三轮 M4）：为「完全换说法」的极端改写补一次命中机会。

    运作逻辑：作答题若出现「参考要素句与知识库条文共有的任意 2 字 Bigram」，
    且与要素句极性一致（否定句要求作答同带否定），即视为该要素命中。
    仅当要素句原始判定未命中时才启用（miss→hit 单向放宽）：
      - 不改变 word_ratio / 满分分支，L4 good=100 / bad=0 契约保持；
      - 数字点加分、空洞作答惩罚等既有路径不受影响。

    Args:
        e_norm: 已归一化的要素句。
        ans_norm: 已归一化的作答全文。
        ans_terms: 作答的中文词特征集（_cjk_words 结果）。
        kb_ctx: build_context() 检索到的知识库上下文文本。
        neg_scope: 该要素是否否定作用域。

    Returns:
        str | None: 桥接命中的共享 2-gram（作返回元素标记）；未命中返回 None。
    """
    e_grams = _cjk_bigrams(e_norm)
    if not e_grams or not ans_terms or not kb_ctx:
        return None
    for sent in re.split(r'[，。；、\n]', kb_ctx):
        s_norm = _normalize_terms(sent)
        shared = e_grams & _cjk_bigrams(s_norm)
        if not shared:
            continue
        for g in shared:
            if g in ans_terms and g in ans_norm and _around_neg(ans_norm, g) == neg_scope:
                return g
    return None


def _rule_score(reference: str, user_answer: str, kb_ctx: str = '') -> dict:
    """规则版判分：要素覆盖 + 词命中 + 数字点 + 否定极性感知。

    相比 9/7 版（纯词覆盖），本版：
      - 参考答案拆「要素句」，逐句判定命中（覆盖度更符合人工判分直觉）；
      - 同义词组归一（顾客/客户、保存/储存、效期/保质期…）；
      - 否定极性感知：词前有否定而作答无（或有反之）→ 该词不计命中；
      - 数字实体命中额外加分；
      - 全要素 + 全词覆盖时恒 100（保持 L4 契约：good=100 / bad=0）。
      - kb_ctx（第三轮 M4）：要素原始判定未命中时，用知识库同义句桥接
        兜底一次（作答命中「要素与知识库共有 Bigram」即算命中）。
    """
    ref_elements = _split_elements(reference or '')
    if not ref_elements:
        return {'source': 'rule', 'feedback': '无参考答案，判定为合格',
                'is_correct': True, 'score': PASS_SCORE}
    ref_norm = _normalize_terms(reference or '')
    ans_norm = _normalize_terms(user_answer or '')
    ref_terms = _cjk_words(ref_norm)
    ans_terms = _cjk_words(ans_norm)

    # 要素覆盖（逐句判定）。含否定的句子取「否定词之后的否定作用域」作关键内容：
    # 只有答对同一语义方向（也带否定）才算命中，答成肯定语义即反转 → miss。
    hit_words, miss_words = [], []
    for e in ref_elements:
        e_norm = _normalize_terms(e)
        m_neg = re.search('(' + '|'.join(NEG_WORDS) + ')', e_norm)
        if m_neg:
            neg_scope = True
            e_scope = e_norm[m_neg.end():]  # 否定作用域：否定词之后的实词
        else:
            neg_scope = False
            e_scope = e_norm
        e_terms = _cjk_words(e_scope)
        hit = None
        for w in sorted(e_terms, key=len, reverse=True):
            # 极性一致：否定句要求作答同带否定；肯定句要求作答也不带否定
            if w in ans_terms and (_around_neg(ans_norm, w) == neg_scope):
                hit = w
                break
        # 极端改写兜底：原始判定未命中时，借知识库同义句桥接补一次命中机会
        if not hit:
            hit = _kb_recovery_hit(e_norm, ans_norm, ans_terms, kb_ctx, neg_scope)
        if hit:
            hit_words.append(hit)
        else:
            miss_words.append(next(iter(sorted(e_terms, key=len, reverse=True)), ''))

    element_ratio = len(hit_words) / len(ref_elements)
    word_ratio = len(ref_terms & ans_terms) / len(ref_terms) if ref_terms else 0
    num_ok = _numbers_match(ref_norm, ans_norm)

    # 全要素+全词覆盖 → 恒满分（数字点非必需）；否则加权，数字命中额外加分
    if word_ratio >= 1.0 and element_ratio >= 1.0:
        score = 100
    else:
        score = round(word_ratio * 35 + element_ratio * 45 + (15 if num_ok else 0), 1)
        score = int(min(100, max(0, score)))
        # 空洞作答惩罚：全未命中且作答过短 → 20 分封顶
        if not hit_words and len((user_answer or '').strip()) < 8:
            score = min(score, 20)
    is_correct = score >= PASS_SCORE
    total = len(ref_elements)
    fb = ('✅ 答对要点，' if is_correct else '❌ 要点有遗漏，') + \
         f"覆盖 {len(hit_words)}/{total} 个要点。" + \
         ('关键数值已答出。' if num_ok else '')
    return {'source': 'rule', 'feedback': fb, 'is_correct': is_correct, 'score': score,
            'details': {'hit_elements': hit_words, 'miss_elements': miss_words,
                        'num_ok': num_ok}}


def _parse_json_grade(text: str) -> dict | None:
    """从大模型输出中解析 `{score, feedback, weak}` JSON。

    容忍大模型常见的 Markdown 代码块包裹与首尾杂讯（F1 判分可靠性）。
    解析成功返回 dict，否则返回 None（由调用方 fail-close）。

    Args:
        text: 大模型原始输出。

    Returns:
        dict | None: {'score': int, 'feedback': str, 'weak': str}。
    """
    if not text:
        return None
    body = text.strip()
    # 剥掉 ```json / ``` 代码块包装
    m = re.search(r'\{.*\}', body, re.DOTALL)
    if not m:
        return None
    candidate = m.group(0)
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, TypeError, ValueError):
        # 大模型可能输出 {中文引号} 或多余尾逗号 → 尝试修正两次
        cleaned = candidate.replace('，', ',').replace('“', '"').replace('”', '"')
        cleaned = re.sub(r',\s*}', '}', cleaned)
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    # 非 dict / 缺 score / 非法 score → 统一 fail-close
    raw_score = data.get('score') if isinstance(data, dict) else None
    if raw_score is None:
        return None
    try:
        score = int(float(raw_score))
    except (TypeError, ValueError):
        return None
    score = max(0, min(100, score))
    feedback = str(data.get('feedback') or '').strip()
    weak = str(data.get('weak') or '通用').strip() or '通用'
    return {'score': score, 'feedback': feedback, 'weak': weak}


class DifyClient:
    """在线 LLM 统一客户端（第七轮起多后端可插拔）。

    provider 取值（LLM_PROVIDER，缺省 auto）：
      - 'auto'：Dify Key 优先，无则用 DeepSeek API（OpenAI 兼容）；
      - 'dify' / 'deepseek'：强制指定后端；
      - 'none'：强制离线（规则版）。
    DIFY_ENABLED=False 为总开关，压住一切后端（离线演示/CI 稳定）。
    """

    def __init__(self, app=None):
        if app is not None:
            self.init_app(app)
        else:
            self.base_url = os.environ.get('DIFY_BASE_URL', 'http://localhost:8080').rstrip('/')
            self.chat_key = os.environ.get('DIFY_CHAT_APP_KEY', '')
            self.advice_key = os.environ.get('DIFY_ADVICE_APP_KEY', '')
            self.timeout = int(os.environ.get('DIFY_TIMEOUT', '30'))
            self.provider = (os.environ.get('LLM_PROVIDER') or 'auto').strip().lower()
            self.deepseek_base = os.environ.get(
                'DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')
            self.deepseek_key = os.environ.get('DEEPSEEK_API_KEY', '')
            self.deepseek_model = os.environ.get('DEEPSEEK_MODEL', 'deepseek-chat')
            # env 直构路径同样尊重 DIFY_ENABLED 总开关（False 压住一切后端）
            raw = os.environ.get('DIFY_ENABLED')
            master = (None if raw is None or not str(raw).strip()
                      else str(raw).strip().lower() in ('1', 'true', 'yes', 'y', 'on'))
            self.enabled = self._resolve_enabled(master=master)
        # 熔断器实例（D3）：每个客户端独立计数，避免多 App 互相影响
        self.breaker = CircuitBreaker()

    def init_app(self, app):
        self.base_url = app.config['DIFY_BASE_URL'].rstrip('/')
        self.chat_key = app.config['DIFY_CHAT_APP_KEY']
        self.advice_key = app.config['DIFY_ADVICE_APP_KEY']
        self.timeout = app.config['DIFY_TIMEOUT']
        self.provider = (app.config.get('LLM_PROVIDER') or 'auto').strip().lower()
        self.deepseek_base = app.config.get(
            'DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')
        self.deepseek_key = app.config.get('DEEPSEEK_API_KEY', '')
        self.deepseek_model = app.config.get('DEEPSEEK_MODEL', 'deepseek-chat')
        # DIFY_ENABLED 是在线 AI 总开关（含「未设时按 Key 自动推导」的解析结果），
        # 必须先于 provider 路由生效——否则配了 DeepSeek Key 的离线演示/CI
        # 会意外发起真实 API 调用（第七轮冒烟实测教训）。
        self.enabled = self._resolve_enabled(master=app.config.get('DIFY_ENABLED'))
        self.breaker = CircuitBreaker()
        # 启动自检（S5）：一行脱敏指纹，现场可秒确认「在线 AI 是否真的生效」
        self._log_backend_fingerprint()

    @staticmethod
    def _mask_key(key: str) -> str:
        """密钥脱敏：仅显示末 4 位，绝不打印完整 Key（安全红线）。"""
        key = key or ''
        if not key:
            return '(未配置)'
        return f'***{key[-4:]}' if len(key) > 4 else '***'

    def _log_backend_fingerprint(self):
        """启动自检日志：输出当前 AI 后端 + 脱敏 Key（S5，仅服务端日志）。"""
        if not self.enabled:
            logger.info('AI 后端自检：离线规则版（总开关关闭或未配置任何 Key）')
            return
        backend = 'deepseek' if self._use_deepseek() else 'dify'
        key = self.deepseek_key if backend == 'deepseek' else (self.chat_key or self.advice_key)
        target = self.deepseek_model if backend == 'deepseek' else self.base_url
        logger.info('AI 后端自检：provider=%s backend=%s target=%s key=%s',
                    self.provider, backend, target, self._mask_key(key))

    def fork(self):
        """复制一个状态隔离的客户端，供并发角色调用使用。

        两个并发角色不得共享 CircuitBreaker 的可变计数；复制后的实例仍读取
        同一份后端配置，但不安装 Flask 扩展，也不保留原实例的熔断状态。
        """
        clone = DifyClient()
        clone.base_url = self.base_url
        clone.chat_key = self.chat_key
        clone.advice_key = self.advice_key
        clone.timeout = self.timeout
        clone.provider = self.provider
        clone.deepseek_base = self.deepseek_base
        clone.deepseek_key = self.deepseek_key
        clone.deepseek_model = self.deepseek_model
        clone.enabled = self.enabled
        clone.breaker = CircuitBreaker()
        return clone

    def _trace(self, scene, backend, started, answer):
        """记录一次在线调用留痕（S4；写失败静默，绝不影响主流程）。"""
        ok = bool(answer) and not is_dify_error(answer)
        ai_trace.record(
            scene,
            {'provider': self.provider,
             'source': backend,
             'model': self.deepseek_model if backend == 'deepseek' else ''},
            (time.time() - started) * 1000, ok, fallback=not ok)

    def _resolve_enabled(self, master=None) -> bool:
        """按总开关 + provider + 各后端 Key 推导在线 AI 开关。

        Args:
            master: DIFY_ENABLED 总开关解析值；False 一票压住（离线/CI），
                None 表示未显式设置（按 provider 与 Key 推导）。

        Returns:
            bool: 'none' 恒 False；'dify'/'deepseek' 只认各自 Key；
            'auto' 任一后端有 Key 即 True。
        """
        if master is False:
            return False
        if self.provider == 'none':
            return False
        if self.provider == 'dify':
            return bool(self.chat_key or self.advice_key)
        if self.provider == 'deepseek':
            return bool(self.deepseek_key)
        return bool(self.chat_key or self.advice_key or self.deepseek_key)

    # ── 底层 API ──
    def _chat(self, query, user, conversation_id=None, api_key=None):
        """在线 LLM Chat 出口：按 provider 路由到 Dify 或 DeepSeek。

        Args:
            query: 发送给大模型的完整 prompt（自含上下文与知识库检索结果）。
            user: Dify 侧用户标识（DeepSeek 后端忽略）。
            conversation_id: Dify 多轮会话 ID，首轮为 None。DeepSeek 无服务端
                会话，该值原样透传返回——多轮上下文由 prompt 自带历史承载
                （与 Dify 版 role_prompt+对话历史的用法一致）。
            api_key: 覆盖默认 chat_key 的 Dify 应用密钥。

        Returns:
            tuple[str | None, str | None]: (回答或失败标记串, 会话ID)。
            未启用时返回 (None, conversation_id)。
        """
        if not self.enabled or not requests:
            return None, conversation_id
        started = time.time()
        if self._use_deepseek():
            ans, cid = self._deepseek_chat(query, conversation_id)
            backend = 'deepseek'
        else:
            ans, cid = self._dify_chat(query, user, conversation_id, api_key)
            backend = 'dify'
        self._trace('chat', backend, started, ans)
        return ans, cid

    def _use_deepseek(self) -> bool:
        """本次调用是否走 DeepSeek 后端（auto：Dify Key 优先，无则 DeepSeek）。

        Returns:
            bool: 显式 'deepseek' 恒 True；'auto' 在无任何 Dify Key 且有
            DeepSeek Key 时 True；显式 'dify' 恒 False。
        """
        if self.provider == 'deepseek':
            return True
        if self.provider == 'auto':
            return bool(self.deepseek_key) and not (self.chat_key or self.advice_key)
        return False

    def _deepseek_chat(self, query, conversation_id=None):
        """DeepSeek Chat（OpenAI 兼容 /chat/completions），熔断器与收口复用。

        Args:
            query: 完整 prompt（自含上下文）。
            conversation_id: 透传返回（DeepSeek 无服务端会话概念）。

        Returns:
            tuple[str | None, str | None]: (回答或失败标记串, 会话ID)。
        """
        if not self.deepseek_key:
            return None, conversation_id
        # D3：熔断窗口内直接快速失败，与 Dify 后端共享同一熔断计数
        if not self.breaker.allow():
            return _fail('breaker', RuntimeError('circuit breaker open')), conversation_id
        if not ai_gate.acquire():
            return _saturated('deepseek'), conversation_id
        try:
            resp = requests.post(
                f'{self.deepseek_base}/chat/completions',
                headers={'Authorization': f'Bearer {self.deepseek_key}',
                         'Content-Type': 'application/json'},
                json={
                    'model': self.deepseek_model,
                    'messages': [{'role': 'user', 'content': query}],
                    'stream': False,
                },
                timeout=self.timeouts,
            )
            resp.raise_for_status()
            data = resp.json()
            self.breaker.on_success()
            choices = data.get('choices') or [{}]
            content = (choices[0].get('message') or {}).get('content', '')
            return content, conversation_id
        except Exception as exc:  # 含 requests 全族异常 + JSON 解析异常
            self.breaker.on_failure()
            return _fail('deepseek', exc), conversation_id
        finally:
            ai_gate.release()

    @property
    def timeouts(self):
        """requests 超时参数，恒以 `(connect, read)` 元组下发。

        为什么不用标量：urllib3 的 `Timeout(total=None, connect=N, read=N)` 会把同一个
        N 同时用作连接与读取上限（本机读 urllib3 2.7.0 源码核实），即 `timeout=30`
        的最坏墙钟是 30s 连接 + 30s 读取 = 60s，而非直觉的 30s。驾驶舱的角色调用
        跑在 worker 线程里，signal/SIGALRM 类超时在非主线程不可用（Superset
        `utils/core.py:797-804` 正是这么退化的），socket 元组是唯一真实约束。
        连接阶段本就不该等：5s 内建不起 TCP 说明网络已不可用，立即失败让并发闸与
        规则版降级及时接管。

        Returns:
            tuple[float, float]: (连接秒数, 读取秒数)。
        """
        cap = self.timeout if self.timeout < CONNECT_TIMEOUT_CEILING else CONNECT_TIMEOUT_CEILING
        return (cap, self.timeout)

    def _dify_chat(self, query, user, conversation_id=None, api_key=None):
        """Dify Chat App：多轮对话（问答/情景/考核用）。"""
        if not self.breaker.allow():
            return _fail('breaker', RuntimeError('circuit breaker open')), conversation_id
        api_key = api_key or self.chat_key
        if not ai_gate.acquire():
            return _saturated('chat'), conversation_id
        try:
            resp = requests.post(
                f'{self.base_url}/v1/chat-messages',
                headers={'Authorization': f'Bearer {api_key}',
                         'Content-Type': 'application/json'},
                json={
                    'inputs': {},
                    'query': query,
                    'response_mode': 'blocking',
                    'conversation_id': conversation_id or '',
                    'user': user,
                },
                timeout=self.timeouts,
            )
            resp.raise_for_status()
            data = resp.json()
            self.breaker.on_success()
            return data.get('answer', ''), data.get('conversation_id', conversation_id)
        except Exception as exc:  # 含 requests 全族异常 + JSON 解析异常
            self.breaker.on_failure()
            return _fail('chat', exc), conversation_id
        finally:
            ai_gate.release()

    def _completion(self, query, user, api_key=None, record_trace=True):
        """在线 LLM 单次生成出口：Dify Completion App 或 DeepSeek Chat。

        DeepSeek 无 completion 型 API，统一走 chat/completions（自含上下文
        的单轮 prompt 与 completion 语义等价）。

        Args:
            query: 发送给大模型的完整 prompt。
            user: Dify 侧用户标识。
            api_key: 覆盖默认 advice_key/chat_key 的应用密钥。

        Returns:
            str | None: 生成内容或失败标记串；未启用时返回 None。
        """
        if not self.enabled or not requests:
            return None
        started = time.time()
        if self._use_deepseek():
            ans, _ = self._deepseek_chat(query)
            backend = 'deepseek'
        else:
            ans = self._dify_completion(query, user, api_key)
            backend = 'dify'
        if record_trace:
            self._trace('completion', backend, started, ans)
        return ans

    def decision_completion(self, prompt, role):
        """决策驾驶舱的隔离单轮调用，不在工作线程写任何轨迹。

        Returns:
            dict: {'content', 'source', 'ok', 'tokens'}。
        """
        if not self.enabled:
            return {'content': '', 'source': 'rule', 'ok': False, 'tokens': 0}
        ans = self._completion(prompt, f'decision-{role}', record_trace=False)
        if is_ai_saturated(ans):
            # 单独回传 'saturated'，让驾驶舱能把「本机限流」与「上游挂了」分开归因
            return {'content': '', 'source': 'saturated', 'ok': False, 'tokens': 0}
        if not ans or is_dify_error(ans):
            return {'content': '', 'source': 'error', 'ok': False, 'tokens': 0}
        backend = 'deepseek' if self._use_deepseek() else 'dify'
        tokens = max(1, (len(str(prompt)) + len(str(ans))) // 4)
        return {'content': ans, 'source': backend, 'ok': True, 'tokens': tokens}

    def _dify_completion(self, query, user, api_key=None):
        """Dify Completion App 原调用（备货建议/损耗提醒用）。"""
        if not self.breaker.allow():
            return _fail('breaker', RuntimeError('circuit breaker open'))
        api_key = api_key or self.advice_key or self.chat_key
        if not ai_gate.acquire():
            return _saturated('completion')
        try:
            resp = requests.post(
                f'{self.base_url}/v1/completion-messages',
                headers={'Authorization': f'Bearer {api_key}',
                         'Content-Type': 'application/json'},
                json={'inputs': {'query': query}, 'response_mode': 'blocking', 'user': user},
                timeout=self.timeouts,
            )
            resp.raise_for_status()
            self.breaker.on_success()
            return resp.json().get('answer', '')
        except Exception as exc:
            self.breaker.on_failure()
            return _fail('completion', exc)
        finally:
            ai_gate.release()

    @staticmethod
    def customer_state(history) -> dict:
        """顾客情绪状态：从对话历史重放店员发言推导耐心值与情绪档位。

        设计为「纯函数重放」而非 session 持久化：history 已是唯一状态载体
        （最多 12 行、单行 240 字），重放天然无脏值、无 Cookie 膨胀风险，
        且便于单测。每轮店员发言按 TONE_RULES 命中最靠前的一条计分。

        Args:
            history: 对话历史行列表，形如 ['店员：…', '顾客：…']（允许 None）。

        Returns:
            dict: {'patience': float, 'mood': str, 'mood_label': str}。
        """
        patience = CUSTOMER_PATIENCE_START
        for line in history or []:
            if not isinstance(line, str) or not line.startswith('店员：'):
                continue
            msg = line[3:]
            for delta, words in TONE_RULES:
                if any(w in msg for w in words):
                    patience += delta
                    break  # 一轮只计一次（规则顺序即优先级）
        patience = max(PATIENCE_MIN, min(PATIENCE_MAX, patience))
        mood = next((m for t, m in MOOD_LEVELS if patience >= t), MOOD_LEVELS[-1][1])
        return {'patience': round(patience, 1), 'mood': mood,
                'mood_label': MOOD_LABELS.get(mood, '一般')}

    def finish_scenario_tip(self, history):
        """情景对话结束点评。

        单一真相源（F16）：enabled 判断收敛在客户端内部，
        路由层直接调用即可，无需再判 dify_client.enabled。

        Args:
            history: 对话历史行列表，形如 ['店员：…', '顾客：…']。

        Returns:
            str: 教练点评文案；未启用或调用失败时返回演示模式文案。
        """
        if not self.enabled:
            # 规则版：引知识库话术给「亮点/改进点」清单
            lines = [h for h in (history or []) if h.startswith('店员：')]
            if not lines:
                return '✅ 演示模式：本轮对话无店员发言，真实部署后 AI 会在此给出针对性服务改进建议。'
            kb = rule_answer('服务话术 异议处理 致歉', top_k=2,
                             domain=pack_service_domain())
            quote = ''
            for line in kb.splitlines()[1:]:  # 去掉「已为您调取…」引导行
                quote += line + '\n'
            # 情绪轨迹：把整轮对话的耐心值变化翻译为可量化的服务表现评价
            state = self.customer_state(history)
            mood_txt = (f"【顾客情绪轨迹】本轮结束顾客情绪「{state['mood_label']}」"
                        f"（耐心值 {state['patience']}/100）。")
            if state['patience'] >= 85:
                mood_txt += '应对到位：致歉与补救及时，顾客被成功留住。'
            elif state['patience'] >= 60:
                mood_txt += '应对合格：基本稳住顾客情绪，可再主动给出补偿选项。'
            elif state['patience'] >= 35:
                mood_txt += '应对一般：顾客情绪中性，缺少主动安抚或解决方案。'
            else:
                mood_txt += '应对欠妥：出现推诿/对抗表述，顾客有流失风险，须按知识库话术重练。'
            return ('✅ 演示模式：以下结合门店知识库为你点评本轮应对。\n'
                    f'【亮点建议】共 {len(lines)} 轮店员发言，注意「错误先致歉再补救、'
                    '诉求先认同再引导」的礼貌闭环。\n'
                    f'{mood_txt}\n'
                    f'【知识库参考】\n{quote.strip()}\n'
                    '（真实部署后由大模型逐轮拆解点评。）')
        prompt = ("你是培训教练，基于以下店员与顾客的对话给店员一段改进点评：\n"
                  + '\n'.join(history or []))
        ans = self._completion(prompt, 'staff-scenario-tip')
        if not ans or is_dify_error(ans):
            return f'⚠️ {DIFY_UNAVAILABLE_TEXT}本次点评未生成，请稍后重试。'
        return ans

    # ── 业务方法 1：备货建议（AC-OBS-07）──
    def restock_advice(self, stats):
        """生成次日备货建议。

        Args:
            stats: analytics.compute_dashboard_stats() 的统计结果。

        Returns:
            dict: {'source': 'rule'|'dify'|'dify-error', 'content': str}。
            source='dify-error' 表示 AI 不可用，前端必须降级展示（F38）。
        """
        top = stats.get('top', {})
        kpi = stats.get('kpi', {})
        top_names = top.get('names', [])
        top_values = top.get('values', [])
        # 近 7 日销量趋势（涨/跌/稳），供 prompt 与规则版共同使用（单位随包 B2b）
        trend_txt, trend = _trend_desc(stats.get('daily', {}), pack_units()[0])
        # 损耗高频品名（备货建议需剔除/少备）
        waste_products = _waste_products(stats.get('waste_alerts', []))
        restock_ctx = build_context('损耗 备货 易损 保存', top_k=2,
                                    domain=pack_material_domain())

        if not self.enabled:
            if not top_names:
                return {'source': 'rule', 'content': '暂无销售数据，待店员提交后生成备货建议。'}
            lines = ['📦 次日备货建议（规则版，基于近期真实销售）']
            # 1. 热销优先级 + 趋势修正
            if trend in ('up', 'down'):
                lines.append(f"1. 近 7 日销量整体{('上涨' if trend == 'up' else '回落')}"
                             f"（{trend_txt}），热销 TOP3：{'、'.join(top_names[:3])}，"
                             f"建议{('按平日 1.2 倍加量' if trend == 'up' else '按 0.9 倍微降')}预备。")
            else:
                lines.append(f"1. 热销 TOP3：{'、'.join(top_names[:3])}，按平日 1.1 倍预备。")
            # 2. 损耗件 → 少备勤补
            if waste_products:
                lines.append(f"2. {'、'.join(waste_products[:3])} 近期损耗偏高，按「少备勤补」降低批量，"
                             f"易损鲜果（芒果/西柚/鲜奶）当日按需采购。")
            else:
                lines.append("2. 近期无损耗异常，易损鲜果（芒果/西柚/鲜奶）仍按需当日采购。")
            lines.append("3. 配置 Dify 后可结合天气、节假日与库存效期做更精准预测（当前为规则演示版）。")
            return {'source': 'rule', 'content': '\n'.join(lines)}

        waste_names = [a.get('product') for a in stats.get('waste_alerts', [])]
        prompt = (
            f"{pack_persona('assistant_persona')}。门店近况：热销TOP={top_names}（营收={top_values}），"
            f"近7日销量趋势={trend_txt or '暂无'}，损耗预警={waste_names}"
            f"，KPI={kpi}。\n"
            f"{restock_ctx}\n"
            "请基于以上数据给店主一份次日备货建议：原料+数量级，说明依据日与风险点，简洁有条理。"
        )
        ans = self._completion(prompt, 'admin')
        if not ans or is_dify_error(ans):
            return {'source': 'dify-error', 'content': DIFY_UNAVAILABLE_TEXT}
        return {'source': 'dify', 'content': ans}

    # ── 业务方法 2：损耗提醒（AC-OBS-08，AI 版汇总）──
    def loss_alert(self, stats):
        """生成原料损耗风险提醒。

        Args:
            stats: analytics.compute_dashboard_stats() 的统计结果。

        Returns:
            dict: {'source': 'rule'|'dify'|'dify-error', 'content': str}。
        """
        alerts = stats.get('waste_alerts', [])
        kb_ctx = build_context('损耗 保存 效期', top_k=2,
                               domain=pack_material_domain())

        if not self.enabled:
            if not alerts:
                return {'source': 'rule', 'content': '✅ 当前无原料损耗异常。'}
            prods = {}
            for a in alerts:
                prods[a['product']] = prods.get(a['product'], 0) + 1
            detail = '、'.join(f"{k}({v}次)" for k, v in sorted(prods.items(), key=lambda x: -x[1]))
            lines = [f"⚠️ 检测到 {len(alerts)} 条损耗异常，集中在：{detail}。"]
            # 根因归因（第六轮 C）：读 analytics.waste_root_cause 的归因结果，
            # 输出主因分布 + 可行动对策；无归因数据时回退通用排查清单（零回归）。
            causes = stats.get('waste_causes') or {}
            if causes.get('ok'):
                labels = (causes.get('summary') or {}).get('labels') or {}
                main = (causes.get('summary') or {}).get('main')
                dist = '、'.join(f'{k}({v}条)' for k, v in labels.items())
                lines.append(f"AI 归因分布（仅供参考）：{dist}。")
                advice = next((it.get('advice') for it in causes.get('items') or []
                               if it.get('cause_label') == main and it.get('advice')), '')
                if main and advice:
                    lines.append(f"主因「{main}」对策：{advice}")
            else:
                # P7-E3-B / M-4：阈值随当前包（此前硬编码 15%，fruit 包看板 8% 时口径不一）
                _thr_txt = f'{industry_pack.pack_waste_rate_threshold() * 100:.0f}%'
                lines += [
                    f"建议：① 按《损耗控制》超过 {_thr_txt} 即异常的标准排查归因"
                    "（备货过多/储存不当/操作浪费）；",
                    "② 易损鲜果/鲜奶「少备勤补」，每日盘点；",
                    "③ 原料变色、异味、胀包立即停用上报，禁止使用。",
                ]
            return {'source': 'rule', 'content': '\n'.join(lines)}
        causes = stats.get('waste_causes') or {}
        cause_txt = (f"AI 规则归因参考（仅供分析，非定责）："
                     f"{json.dumps(causes.get('summary'), ensure_ascii=False)}。\n"
                     if causes.get('ok') else '')
        prompt = (
            f"门店损耗异常记录：{json.dumps(alerts, ensure_ascii=False)}。\n"
            f"{cause_txt}"
            f"{kb_ctx}\n"
            "请结合知识库的损耗控制规范，总结主要风险、归因与改进建议，简洁有条理。"
        )
        ans = self._completion(prompt, 'admin')
        if not ans or is_dify_error(ans):
            return {'source': 'dify-error', 'content': DIFY_UNAVAILABLE_TEXT}
        return {'source': 'dify', 'content': ans}

    # ── 业务方法 3：问答模式出题（AC-OBS-11，第四轮加自适应难度）──
    def quiz_question(self, round_idx, total, topic=None, difficulty='standard'):
        """出一道门店业务题（难度按学习者模型自适应）。

        Args:
            round_idx: 当前题号（从 1 开始）。
            total: 本轮总题数。
            topic: 可选的知识点标签（如 '原料规范'/'异议处理'），为空时顺序轮转四域。
            difficulty: 'basic' / 'standard' / 'advanced'，由学习者模型给出；
                非法值一律回退 'standard'。

        Returns:
            dict: {'source', 'question', 'reference', 'weak', 'difficulty'}。
            Dify 失败时自动回退规则题库，绝不把错误串当题目展示。
        """
        level = difficulty if difficulty in DIFFICULTY_HINTS else 'standard'
        hint = DIFFICULTY_HINTS[level]
        if self.enabled:
            dom = focus_domain(topic) if topic else ''
            domain_hint = dom or '/'.join(industry_pack.pack_domains())
            prompt = (f"你是门店培训考官。请出第 {round_idx}/{total} 道门店业务题"
                      f"（领域：{domain_hint}），"
                      f"难度要求：{hint}，只给题目，不要给答案。")
            ans, _ = self._chat(prompt, f'staff-quiz-{round_idx}')
            # Dify 调用失败/空返回时回退规则版题库，避免把错误串当题目展示
            if ans and not is_dify_error(ans):
                return {'source': 'dify', 'question': ans, 'reference': '',
                        'weak': topic or '通用', 'difficulty': level}
        pool = self._rule_questions(topic, level)
        item = pool[(round_idx - 1) % len(pool)]
        question = item['q']
        if hint:  # 难度要求直接写在题面，店员明确知道答到什么程度
            question = f'{question}{hint}'
        return {'source': 'rule', 'question': question,
                'reference': item['a'], 'weak': item['weak'], 'difficulty': level}

    @staticmethod
    def _rule_questions(topic, difficulty='standard'):
        """按知识点 + 难度过滤规则题库；无匹配时按域/全库回退。

        难度由题目在域内的序号稳定派生（每域 4 题：基础/标准/进阶/标准），
        不引入新题面即可让同一知识点出现不同考查深度。

        Args:
            topic: 知识点标签。
            difficulty: 'basic' / 'standard' / 'advanced'。

        Returns:
            list[dict]: 过滤后的题库（无匹配时回退）。
        """
        pool = pack_questions()
        if topic:
            dom = focus_domain(topic)
            filtered = [it for it in pool if it['weak'] == dom] if dom else []
            if filtered:
                pool = filtered
        if difficulty in ('basic', 'advanced'):
            # 域内序号 → 难度：0=基础、1=标准、2=进阶、3=标准
            leveled = [it for i, it in enumerate(pool)
                       if (i % 4 == 0 if difficulty == 'basic' else i % 4 == 2)]
            if leveled:
                return leveled
        return pool

    # ── 业务方法 4：答案点评 + 判分（AC-OBS-11）──
    def grade_answer(self, question, reference, user_answer):
        """对店员作答判分并给出点评。

        判分 fail-close（F1）：Dify 调用失败、返回空、或点评里解析不到
        有效分数时，一律判 0 分且 is_correct=False，
        绝不因解析失败默认给及格分。

        Args:
            question: 题目文本。
            reference: 参考答案（规则版判分依据）。
            user_answer: 店员作答。

        Returns:
            dict: {'source', 'feedback', 'is_correct', 'score'}。
        """
        q = (question or '').strip()
        ref = reference or ''
        ans = (user_answer or '').strip()
        if self.enabled:
            prompt = (f"题目：{q}\n标准答案：{ref}\n店员作答：{ans}\n"
                      "请严格只输出 JSON，不要输出其他文字："
                      "{\"score\": 0-100的整数, \"feedback\": \"简短点评\", \"weak\": \"薄弱点标签\"}")
            raw, _ = self._chat(prompt, 'staff-quiz-grade')
            # 失败/空返回：不计成绩，不得默认判对
            if not raw or is_dify_error(raw):
                return {'source': 'dify-error',
                        'feedback': 'AI 调用失败，本次作答不计入成绩，请稍后重试',
                        'is_correct': False, 'score': 0}
            parsed = _parse_json_grade(raw)
            if parsed:
                score = max(0, min(100, int(parsed['score'])))
                return {'source': 'dify', 'feedback': parsed['feedback'] or raw,
                        'is_correct': score >= PASS_SCORE, 'score': score,
                        'weak': parsed.get('weak', '通用') if parsed.get('weak') else '通用'}
            # JSON 解析失败 → 正则（得分:X）二次兜底
            m = re.search(r'得分[:：]\s*(\d+)', raw)
            if not m:
                # 解析不到分数 = 判分不可信 → fail-close 判 0 分
                return {'source': 'dify-error',
                        'feedback': 'AI 点评未给出有效分数，本次作答不计入成绩，请稍后重试',
                        'is_correct': False, 'score': 0}
            score = max(0, min(100, int(m.group(1))))
            return {'source': 'dify', 'feedback': raw,
                    'is_correct': score >= PASS_SCORE, 'score': score}
        # 规则版：语义判分 + 知识库同义扩展（极端改写兜底，第三轮 M4）
        kb = build_context(ref, top_k=2) if ref else ''
        return _rule_score(ref, ans, kb_ctx=kb)

    # ── 业务方法 5：情景对话（AI 扮顾客）（AC-OBS-12）──
    def scenario_reply(self, scenario_type, user_msg, history, conversation_id):
        """AI 扮演顾客生成一轮回复。

        规则版：首轮注入顾客角色卡（姓名/情绪/意图），随后按场景池台词推进；
        Dify 版：prompt 携带角色卡 + 历史轮次，让大模型在角色内连贯应答。

        Args:
            scenario_type: 情景类型，须为 SCENARIO_TYPES 之一。
            user_msg: 店员本轮发言。
            history: 历史对话行列表。
            conversation_id: Dify 多轮会话 ID。

        Returns:
            dict: {'source', 'reply', 'conversation_id'}。
            source='dify-error' 时 reply 为脱敏降级文案（F38）。
        """
        catalog = scenario_catalog()
        if scenario_type not in catalog['pool']:
            scenario_type = catalog['default']
        role_name, role_mood, role_intent = catalog['roles'].get(
            scenario_type, catalog['roles'].get(catalog['default'], ('顾客', '', '')))
        # P5 谈判引擎：隐藏态（预算/心理价/让步曲线）由历史重放得出，
        # 结论（成交/流失/未决）优先于台词池推进 —— 说辞决定生意成不成。
        neg = negotiation.replay(history, scenario_type)
        verdict = negotiation.settle(neg)
        panel = negotiation.visible_state(neg, verdict)
        if self.enabled:
            customer = industry_pack.pack_terms().get('scenario_customer') or '顾客'
            role_prompt = (f"你扮演一位{customer}「{role_name}」，"
                           f"场景：{catalog['labels'].get(scenario_type, '')}。"
                           f"性格/情绪：{role_mood}，意图：{role_intent}。"
                           f"用口语化、带情绪的话回复店员，每轮 1-2 句，不要破角色，也别急着结束话题。")
            # P5：把谈判隐藏态摘要注入 prompt，约束顾客让步行为（数字底线不明说）
            neg_txt = (
                f"【内部谈判状态，不得向店员直接报出数字底线】"
                f"顾客可接受价格区间约 ¥{panel['price_range'][0]}~¥{panel['price_range'][1]}，"
                f"当前谈判价 ¥{panel['current_price']}（原价 ¥{panel['list_price']}），"
                f"顾客耐心 {panel['patience']}/100，已进行 {panel['rounds']} 轮，"
                f"系统判定：{verdict['reason']}。请据此决定松动、坚持还是收尾，"
                "保持角色内一致性，不要复述本段状态说明。")
            ctx = '\n'.join(history[-6:]) if history else ''
            query = f"{role_prompt}\n{neg_txt}\n对话历史：\n{ctx}\n店员：{user_msg}\n顾客："
            ans, cid = self._chat(query, 'staff-scenario', conversation_id)
            if not ans or is_dify_error(ans):
                return {'source': 'dify-error',
                        'reply': f'（{DIFY_UNAVAILABLE_TEXT}本轮回复未生成）',
                        'conversation_id': conversation_id, 'negotiation': panel}
            return {'source': 'dify', 'reply': ans, 'conversation_id': cid,
                    'negotiation': panel}
        # 规则版：首轮现身（角色卡开头），随后按轮次取场景池台词
        # 第五轮：叠加情绪状态机——耐心/情绪由店员表现驱动，台词与结束语随之分化
        # P5：叠加谈判状态机——成交/流失结论优先于台词池推进
        pool = catalog['pool'][scenario_type]
        cust_turns = len([h for h in history if h.startswith('顾客')])
        state = self.customer_state(history)
        if verdict['deal']:
            reply = NEG_DEAL_LINES.get(state['mood'], NEG_DEAL_LINES['neutral'])
        elif verdict['lost']:
            reply = NEG_LOST_LINES.get(state['mood'], NEG_LOST_LINES['neutral'])
        elif cust_turns == 0:
            reply = f"（顾客「{role_name}」{role_mood}。{role_intent}）{pool[0]}"
        else:
            idx = cust_turns % len(pool)
            if cust_turns < len(pool):
                # 台词主体按场景推进（叙事完整），情绪只做语气修饰不改写台词
                reply = pool[idx] + MOOD_DECOR.get(state['mood'], '')
            else:
                reply = ENDING_LINES.get(state['mood'], ENDING_LINES['neutral'])
        return {'source': 'rule', 'reply': reply, 'conversation_id': conversation_id,
                'mood': state['mood'], 'mood_label': state['mood_label'],
                'patience': state['patience'], 'negotiation': panel}

    # ── 业务方法 6：上岗考核判定（AC-OBS-13/14）──
    def assessment_verdict(self, records):
        """汇总一轮考核结果。

        Args:
            records: list of {score, weak, is_correct}。

        Returns:
            dict: {'pass', 'avg', 'weak', 'summary', 'suggestion'}。
        """
        if not records:
            return {'pass': False, 'avg': 0, 'weak': [], 'summary': '暂无考核记录。',
                    'suggestion': []}
        scores = [r['score'] for r in records]
        avg = sum(scores) / len(scores)
        weak_count = {}
        for r in records:
            if not r.get('is_correct'):
                w = r.get('weak', '通用')
                weak_count[w] = weak_count.get(w, 0) + 1
        weak = sorted(weak_count.items(), key=lambda x: -x[1])
        passed = avg >= PASS_SCORE  # 平均分达标即合格
        summary = (f"平均分 {avg:.0f}/100，{'合格' if passed else '不合格'}。"
                   + (f"薄弱点：{('、'.join(f'{k}({v})' for k, v in weak))}。" if weak else "知识掌握均衡。"))
        # 补训建议：按薄弱点映射知识库对应篇目（规则版离线可用；Dify 版由大模型生成）
        suggestion = topic_suggestion([w for w, _ in weak])
        return {'pass': passed, 'avg': round(avg, 1),
                'weak': [w for w, _ in weak], 'summary': summary,
                'suggestion': suggestion}

    # ── 业务方法 7：AI 经营分析（管理员侧）──
    def business_analysis(self, stats):
        """生成门店经营分析报告（营收/趋势/结构/损耗/建议）。

        规则版：基于真实 stats 数据模板化生成，标注演示模式；
        Dify 版：数据 + 知识库上下文交给大模型生成结构化洞察。

        Args:
            stats: analytics.compute_dashboard_stats() 的完整统计结果。

        Returns:
            dict: {'source', 'content', 'generated_at', 'evidence',
                   'confidence'}，evidence 为每条结论的置信度与数据依据（D3）。
        """
        # 可解释层与大模型无关：无论哪条链路都附上「凭什么信」的依据
        evidence = explain_business(stats)
        confidence = summarize_confidence(evidence)
        if not self.enabled:
            _unit, _mu = pack_units()  # B2b：规则版报告单位随包
            return {'source': 'rule',
                    'content': _rule_business_report(stats, _unit, _mu),
                    'generated_at': time.strftime('%Y-%m-%d %H:%M'),
                    'evidence': evidence, 'confidence': confidence}
        prompt = self._business_prompt(stats)
        ans = self._completion(prompt, 'admin-analysis')
        if not ans or is_dify_error(ans):
            return {'source': 'dify-error', 'content': DIFY_UNAVAILABLE_TEXT,
                    'generated_at': time.strftime('%Y-%m-%d %H:%M'),
                    'evidence': evidence, 'confidence': confidence}
        return {'source': 'dify', 'content': ans,
                'generated_at': time.strftime('%Y-%m-%d %H:%M'),
                'evidence': evidence, 'confidence': confidence}

    @staticmethod
    def _business_prompt(stats):
        """拼装经营分析 prompt：真实数据 + 知识库上下文。"""
        kpi = stats.get('kpi', {})
        top = stats.get('top', {}) or {}
        pie = stats.get('pie', {}) or {}
        waste = stats.get('waste_alerts', [])
        _unit = pack_units()[0]  # B2b：prompt 里的单位与规则版同源
        trend, _ = _trend_desc(stats.get('daily', {}), _unit)
        ctx = build_context('损耗 备货 保存', top_k=2,
                            domain=pack_material_domain())
        extra = []
        if _compare_text(stats):
            extra.append(f"周期对比：{_compare_text(stats)}")
        if _forecast_text(stats, _unit):
            extra.append(f"销量预测：{_forecast_text(stats, _unit)}")
        if _weekday_text(stats, _unit):
            extra.append(f"工作日/周末规律：{_weekday_text(stats, _unit)}")
        return (
            f"{pack_persona('analyst_persona')}。请基于以下真实门店数据，输出一份 4 段经营分析报告："
            "① 经营概况 ② 趋势与结构 ③ 风险提示 ④ 优化建议。要求数据准确、结论可落地、语气客观。\n"
            f"KPI={json.dumps(kpi, ensure_ascii=False)}\n"
            f"销量趋势={trend}\n"
            f"热销TOP={top.get('names')}（营收={top.get('values')}）\n"
            f"销售结构占比{pie.get('names')}={pie.get('values')}\n"
            f"损耗预警={json.dumps(waste, ensure_ascii=False)}\n"
            + ('\n'.join(extra) + '\n' if extra else '')
            + f"{ctx}\n"
            "输出报告正文即可，不要输出 JSON。"
        )

    # ── 业务方法 8：AI 智能助手（管理员侧业务问答，支持多轮）──
    def assistant_ask(self, question, stats, conversation_id=None):
        """管理员业务问答：销量/营收/毛利/损耗/备货/知识库。

        规则版：意图关键词路由 + 真实数据/知识库回答（承接消解在路由层）；
        Dify 版：问题 + 数据摘要 + 检索上下文交给大模型，多轮
        conversation_id 透传保持上下文。

        Args:
            question: 管理员提问文本。
            stats: analytics.compute_dashboard_stats() 的完整统计结果。
            conversation_id: Dify 多轮会话 ID（首轮为 None）。

        Returns:
            dict: {'source', 'reply', 'conversation_id'}。
        """
        q = (question or '').strip()
        if not q:
            return {'source': 'rule', 'reply': '请输入你的问题。', 'conversation_id': conversation_id}
        if not self.enabled:
            reply = _assistant_rule_reply(q, stats)
            return {'source': 'rule', 'reply': reply, 'conversation_id': conversation_id}

        kpi = stats.get('kpi', {})
        _unit = pack_units()[0]  # B2b：prompt 里的单位与规则版同源
        trend, _ = _trend_desc(stats.get('daily', {}), _unit)
        waste = stats.get('waste_alerts', [])
        top = stats.get('top', {}) or {}
        ctx = build_context(q, top_k=3)
        extra = []
        if _compare_text(stats):
            extra.append(f"周期对比：{_compare_text(stats)}")
        if _forecast_text(stats, _unit):
            extra.append(f"销量预测：{_forecast_text(stats, _unit)}")
        prompt = (
            f"{pack_persona('assistant_persona')}，管理员问：「{q}」。请你根据以下门店真实数据回答，"
            "只输出答案正文，不要复述背景。数据不足时如实说明。\n"
            f"KPI（营收/日均/客单/毛利）={json.dumps(kpi, ensure_ascii=False)}\n"
            f"近期趋势={trend}，热销TOP={top.get('names')}\n"
            f"损耗预警={json.dumps(waste, ensure_ascii=False)}\n"
            + ('\n'.join(extra) + '\n' if extra else '')
            + f"{ctx}\n"
        )
        ans, cid = self._chat(prompt, 'admin-assistant', conversation_id)
        if not ans or is_dify_error(ans):
            return {'source': 'dify-error', 'reply': DIFY_UNAVAILABLE_TEXT,
                    'conversation_id': conversation_id}
        return {'source': 'dify', 'reply': ans, 'conversation_id': cid}

    # ── 业务方法 9：店员 AI 培训助手（个人错题 + 知识库多轮问答，第三轮 M1）──
    def trainer_ask(self, question, stats, personal_ctx=None, conversation_id=None):
        """店员侧 AI 培训助手：业务/知识问答 + 个人错题针对性提示。

        个人画像（personal_ctx）由路由层从当前店员 QuizRecord 聚合（只读），
        本方法不触碰 DB（防腐层约定）：
          - 规则版：先走业务意图路由/知识库问答取主答案，再按个人画像附加
            「你的薄弱点〈域〉：已错 N 题，建议复习…」个性化区块；
          - Dify 版：问题 + 数据摘要 + RAG 上下文 + 个人画像注入大模型，
            多轮 conversation_id 透传；调用失败降级规则版主答案（绝不空回）。

        Args:
            question: 店员提问文本。
            stats: analytics.compute_dashboard_stats() 的完整统计结果。
            personal_ctx: {'weak_labels': list, 'wrong_count': int,
                           'recent_wrong_questions': list}，可为空 dict。
            conversation_id: Dify 多轮会话 ID（首轮为 None）。

        Returns:
            dict: {'source', 'reply', 'conversation_id', 'refs'}，
            refs 为本次检索命中的知识库出处（RAG 溯源 + 反馈闭环用，D4）。
        """
        q = (question or '').strip()
        if not q:
            return {'source': 'rule', 'reply': '请输入你的问题。', 'conversation_id': conversation_id,
                    'refs': []}
        ctx = personal_ctx or {}
        weak_labels = ctx.get('weak_labels') or []
        # 检索出处：无论走哪条链路都本地检索一次，让店员看到答案来自哪条知识
        refs = [{'domain': h['domain'], 'title': h['title'], 'score': h['score']}
                for h in kb_search(q, top_k=2)]
        if self.enabled:
            weak_txt = (f"该学员薄弱知识域：{'、'.join(weak_labels[:3])}。"
                        if weak_labels else '无薄弱点记录。')
            # 掌握度画像（第六轮 B）：整体分 + 最弱域，供大模型个性化表述
            mastery_txt = ctx.get('mastery_summary') or ''
            if mastery_txt:
                weak_txt += f' 掌握度画像：{mastery_txt}。'
            wrong_txt = ''
            if ctx.get('recent_wrong_questions'):
                wrong_txt = '其近期错题摘要：' + '；'.join(ctx['recent_wrong_questions'][:3]) + '。'
            prompt = (
                f"{pack_persona('trainer_persona')}，店员问：「{q}」。\n"
                f"个人学习画像（仅用于个性化提示）：{weak_txt}{wrong_txt}\n"
                f"门店数据摘要：KPI={json.dumps(stats.get('kpi', {}), ensure_ascii=False)}\n"
                f"{build_context(q, top_k=3)}\n"
                "回答应结合门店标准知识库与真实数据，先给结论再给依据；"
                "若该店员有薄弱域，末尾加一句「你的薄弱点〈域〉建议复习：…」。"
                "只输出正文，不要复述背景。"
            )
            ans, cid = self._chat(prompt, 'staff-trainer', conversation_id)
            if ans and not is_dify_error(ans):
                return {'source': 'dify', 'reply': ans, 'conversation_id': cid, 'refs': refs}
        # 规则版主答案（业务问法 / 知识库兜底），再按个人画像附加补训提示
        main = _assistant_rule_reply(q, stats)
        # 掌握度画像（第六轮 B）：把「错了几题」升级为「当前掌握多少」
        mastery_txt = ctx.get('mastery_summary') or ''
        if mastery_txt:
            main += f'\n\n【AI 掌握度画像】{mastery_txt}。'
        tips = topic_suggestion(weak_labels[:3])
        if weak_labels and tips:
            main += ('\n\n【你的薄弱点提示】' + weak_labels[0] + f"方向已错 "
                     f"{ctx.get('wrong_count', 0)} 题，建议：{'；'.join(tips)}")
        return {'source': 'rule', 'reply': main, 'conversation_id': conversation_id, 'refs': refs}

    # ── 业务方法 10：情景对话即时点评（第三轮 M3，每轮陪练提示）──
    def scenario_instant_tip(self, user_msg):
        """对店员本轮发言给一句即时点评。

        规则版：先致歉/对抗词检测，否则引知识库最相关话术给正向提示；
        Dify 版：短 prompt 生成 ≤40 字点评。失败/无匹配返回空串（前端不渲染）。

        Args:
            user_msg: 店员本轮发言。

        Returns:
            str: 点评文案；无匹配或调用失败返回 ''。
        """
        msg = (user_msg or '').strip()
        if not msg:
            return ''
        if self.enabled:
            prompt = ('你是情景演练陪练教练。店员刚说：' + msg +
                      '。给一句 ≤40 字的即时点评，正面为主，最多 1 个改进点，只说点评本身。')
            ans = self._completion(prompt, 'staff-scenario-tip')
            if ans and not is_dify_error(ans):
                return ans.strip()
        apol = ('抱歉', '不好意思', '对不起', '真抱歉', '致歉')
        if any(k in msg for k in apol):
            return '✓ 已先致歉，继续安抚并给出解决方案即可。'
        confront = ('你爱买不买', '随便你', '快点吧', '别磨叽', '爱要不要')
        if any(k in msg for k in confront):
            return '✗ 避免对抗语气，先认同顾客情绪再引导。'
        quote = rule_answer(msg, top_k=1)
        for line in quote.splitlines()[1:]:
            if line and '：' in line:
                return '✓ 本轮知识库要点：' + line.split('：', 1)[1]
        return ''


# 单例（在 app 启动时 init_app）
dify_client = DifyClient()


# ─────────────────────────────────────────────────────────────
# 第五轮：模糊意图路由（关键词 miss 后的语义兜底）
# ─────────────────────────────────────────────────────────────
# 纯关键词路由对口语问法漏召回（"生意好不好""这周比上周咋样"不含任一关键词，
# 直接掉进知识库兜底 → 答非所问）。本层用「意图示例句」做 IDF 加权 Bigram
# 相似度匹配：命中意图后注入该意图的锚关键词重走关键词分支，
# 复用全部既有真实数据回答逻辑，不新增第二套答复实现。
# P7-E3-B3 / M-11：示例句、锚词、阈值等数据已收敛到 `src/intent_rules.py`
# （见文件头 import 的 re-export）；此处仅保留依赖判分归一文本工具的匹配算法。
# 示例语料 IDF 缓存（语料为模块常量，首次计算后常驻）
_INTENT_IDF_CACHE = {}


def _intent_gram_idf() -> dict:
    """示例语料级 IDF：出现在少数示例句中的 Bigram 区分度更高。

    Returns:
        dict: {bigram: idf}；语料为空时返回空字典。
    """
    if _INTENT_IDF_CACHE:
        return _INTENT_IDF_CACHE
    docs = []
    for exemplars in INTENT_EXEMPLARS.values():
        for ex in exemplars:
            grams = _cjk_bigrams(_normalize_terms(ex))
            if grams:
                docs.append(grams)
    total = len(docs)
    if not total:
        return {}
    doc_freq = {}
    for grams in docs:
        for g in grams:
            doc_freq[g] = doc_freq.get(g, 0) + 1
    _INTENT_IDF_CACHE.update(
        {g: math.log(1 + total / df) for g, df in doc_freq.items()})
    return _INTENT_IDF_CACHE


def _gram_similarity(a: set, b: set, idf: dict) -> float:
    """IDF 加权 Jaccard 相似度（交集权重 / 并集权重）。"""
    if not a or not b:
        return 0.0
    inter = sum(idf.get(g, 1.0) for g in a & b)
    union = sum(idf.get(g, 1.0) for g in a | b)
    return inter / union if union else 0.0


def intent_fuzzy_route(question: str) -> str | None:
    """模糊意图路由：口语问句 → 最相似的业务意图（未达阈值返回 None）。

    M-18：原私有名 `_intent_fuzzy_route` 转公开——`agent_trace` 轨迹层是本函数
    的正式消费方，跨模块调用不应依赖私有函数。

    Args:
        question: 用户问句（已去空格，允许空/纯英文/超长）。

    Returns:
        str | None: INTENT_EXEMPLARS 的意图名；无中文特征或相似度低于
        阈值时返回 None（调用方走知识库兜底，行为与旧版一致）。
    """
    q_grams = _cjk_bigrams(_normalize_terms(question or ''))
    if not q_grams:
        return None
    idf = _intent_gram_idf()
    best, best_score = None, 0.0
    for intent, exemplars in INTENT_EXEMPLARS.items():
        for ex in exemplars:
            score = _gram_similarity(q_grams, _cjk_bigrams(_normalize_terms(ex)), idf)
            if score > best_score:
                best, best_score = intent, score
    return best if best_score >= INTENT_FUZZY_THRESHOLD else None


def _assistant_rule_reply(question: str, stats: dict) -> str:  # noqa: PLR0911, PLR0912, PLR0915
    """规则版智能助手：意图关键词路由 + 真实数据/知识库回答。

    路由顺序：具体产品 / 预测 / 周期对比 / 滞销榜 / 毛利 / 营收 / 销量 /
    损耗 / 备货 / **模糊意图路由（第五轮）** / 知识库兜底。

    Args:
        question: 管理员提问。
        stats: compute_dashboard_stats() 的完整统计结果。

    Returns:
        str: 回答文案。
    """
    q = question.strip()
    kpi = stats.get('kpi', {})
    top = stats.get('top', {}) or {}
    waste = stats.get('waste_alerts', [])
    has = stats.get('has_data', False)
    unit = pack_units()[0]  # B2b：规则版答复的数量单位随包

    def _no_data():
        return '当前还没有销售数据，待店员提交首日填报后即可分析。'

    # 具体产品问询（最优先：命中产品名即答该产品数据）
    hit_product = [p for p in pack_product_names() if p in q]
    if hit_product:
        if not has:
            return _no_data()
        answers = []
        for name in hit_product:
            qty, rev = _product_lookup(stats, name)
            if qty is None and rev is None:
                answers.append(f'{name}：暂无可统计的销售数据')
            else:
                parts = []
                if qty is not None:
                    parts.append(f'销量 {qty} {unit}')
                if rev is not None:
                    parts.append(f'营收 ¥{rev:,.0f}')
                answers.append(f'{name}：{"、".join(parts)}')
        return '、'.join(answers) + '（统计期内）。'
    # 销量预测（明天/预计/预估；「备货」类问法走备货建议分支）
    # P7-E3-B3 / M-11：词表走 intent_rules 单一真相源，与轨迹判定同源同序
    if match_intent(q, 'forecast'):
        if not has:
            return _no_data()
        f_text = _forecast_text(stats, unit)
        if not f_text:
            return '当前经营数据不足 3 日，暂无法给出销量预测。'
        return f_text
    # 周期对比（这周/上周/环比/涨跌）
    if match_intent(q, 'compare'):
        if not has:
            return _no_data()
        c_text = _compare_text(stats)
        if not c_text:
            return '当前经营数据不足 14 日，暂无法做周期对比。'
        return c_text
    # 滞销榜（最差/垫底/滞销/卖得少）
    if match_intent(q, 'slow'):
        if not has:
            return _no_data()
        pie = stats.get('pie') or {}
        names, values = (pie.get('names') or []), (pie.get('values') or [])
        if not names:
            return '暂无销售结构数据。'
        least = sorted(zip(names, values, strict=False), key=lambda x: x[1])[:3]
        least_txt = '、'.join(f'{n}({v}{unit})' for n, v in least if isinstance(v, int))
        return (f'销量最低的依次为：{least_txt}。建议排查是新品冷启动还是定价/出餐问题，'
                '切忌砍掉后无替代品可用。')
    # 毛利率
    if match_intent(q, 'margin'):
        if not has:
            return _no_data()
        gm = kpi.get('gross_margin')
        if gm is None:
            return ('当前尚未录入产品成本价，暂无法核算毛利率。'
                    '建议先在「产品管理」中为产品补录成本价，即可自动核出毛利。')
        return (f'统计期综合毛利率约 {gm}%（按已录成本口径，'
                f'共 {kpi.get("margin_cost_items")}/{kpi.get("margin_total_items")} 条填报核入）。'
                '成本价未录的产品暂未计入。')
    # 营收
    if match_intent(q, 'revenue'):
        if not has:
            return _no_data()
        top3 = '、'.join((top.get('names') or [])[:3])
        return (f'统计期总营收 ¥{kpi.get("total_revenue", 0):,.0f}，'
                f'日均约 ¥{kpi.get("avg_daily_revenue", 0):,.0f}，客单价 ¥{kpi.get("avg_per_unit", 0):,.0f}。'
                f'热销前三为 {top3}。')
    # 销量
    if match_intent(q, 'qty'):
        if not has:
            return _no_data()
        top3 = '、'.join((top.get('names') or [])[:3])
        trend0 = _trend_desc(stats.get('daily', {}), unit)[0]
        return (f'统计期总销量 {kpi.get("total_qty", 0)} {unit}。热销 TOP：{top3}。'
                f'销量趋势：{trend0}。')
    # 损耗
    if match_intent(q, 'waste'):
        if not has:
            return _no_data()
        if not waste:
            return '当前无原料损耗异常，原料管理较健康。注意按《损耗控制》保持每日盘点。'
        names = '、'.join(_waste_products(waste)[:3])
        # P7-E3-B / M-4：阈值随当前包（与看板判定同源）
        thr_txt = f'{industry_pack.pack_waste_rate_threshold() * 100:.0f}%'
        return (f'近期检测到 {len(waste)} 条损耗预警，集中在 {names}。'
                f'建议：① 按 {thr_txt} 阈值排查归因（备货过多/储存不当/操作浪费）；'
                '② 易损鲜果/鲜奶少备勤补；③ 发现变色异味立即停用上报。')
    # 备货
    if match_intent(q, 'restock'):
        if not has:
            return _no_data()
        return dify_client.restock_advice(stats).get('content', '')
    # 第五轮：模糊意图路由——关键词全部 miss 时按口语相似度补一次路由；
    # 命中则注入该意图锚关键词递归一次（锚词必落对应分支，递归深度恒为 1；
    # 锚词已在句中时不再递归，防任何情况下的死循环）
    fuzzy = intent_fuzzy_route(q)
    if fuzzy and INTENT_ANCHORS[fuzzy] not in q:
        return _assistant_rule_reply(f'{INTENT_ANCHORS[fuzzy]} {q}', stats)
    # 知识库兜底（话术/价格/原料/服务等）
    return rule_answer(q, top_k=2, domain=focus_domain(q))
