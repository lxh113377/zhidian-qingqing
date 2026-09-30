"""应用配置：开发环境用SQLite，生产环境切换MySQL"""
import logging
import os

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

logger = logging.getLogger(__name__)

# 开发环境默认密钥。仅供本地/离线演示使用；生产环境若仍为该值，
# create_app() 会直接抛 RuntimeError 拒绝启动（见 src/app.py，F2）。
DEV_SECRET_KEY = 'ican-dev-secret-key-change-in-production'  # noqa: S105  理由：开发占位值（名字自带 change-in-production），生产环境命中弱密钥会被 app.py 启动守卫拒绝，除非显式 ICAN_ALLOW_WEAK_SECRET

# ── 生产 SECRET_KEY 强度策略（P7-E / S-1）──
# 旧的守卫是「等值黑名单」：只挡 DEV_SECRET_KEY 这一个字符串。任何「不是那一个字符串」
# 的弱密钥都能顺利通过守卫 —— 例如部署文档里曾明文写过的 `ican-demo-secret-2026-0910`
# （见 部署/免费托管上线-2026-09-10.md）。而 Flask 的 session 是**客户端签名 Cookie**：
# 密钥一旦出现在仓库/文档里，任何人都能离线伪造一份 `admin-1` 会话，直接拿到全部管理权限。
# 因此升级为「已知弱值黑名单 + 强度下限」。
#
# 过渡期逃生开关：`ICAN_ALLOW_WEAK_SECRET=1` 时只告警不阻断（供线上换 key 前临时使用）。
WEAK_SECRET_VALUES = frozenset({
    DEV_SECRET_KEY,
    # 曾在部署文档中明文出现并用于演示/沙箱部署的值（已视为泄露，永久拉黑）
    'ican-demo-secret-2026-0910',
    # 常见占位值
    'changeme', 'change-me', 'secret', 'secret-key', 'password', 'ican', 'ican-secret',
})
MIN_PROD_SECRET_LEN = 16       # 低于此长度直接判定为弱密钥
RECOMMENDED_SECRET_LEN = 32    # 仅作建议，不阻断（避免误伤既有 24~31 位强随机密钥）


def secret_strength_issues(secret):
    """评估生产 SECRET_KEY 强度，返回问题码列表（空列表 = 合格）。

    纯函数：不读环境变量、不抛异常，便于测试直接构造用例钉死（P7-E / S-1）。

    Args:
        secret: 待评估的密钥；None / 空串视为 'empty'。

    Returns:
        list[str]: 问题码，可能包含 'empty' / 'known-weak' / 'too-short' /
            'low-entropy'。
    """
    if not secret:
        return ['empty']
    text = str(secret)
    issues = []
    if text in WEAK_SECRET_VALUES or text.strip().lower() in {
            v.lower() for v in WEAK_SECRET_VALUES}:
        issues.append('known-weak')
    if len(text) < MIN_PROD_SECRET_LEN:
        issues.append('too-short')
    elif len(set(text)) <= 4:
        # 长度够但字符种类极少（如 'aaaaaaaaaaaaaaaa'），同样视为弱密钥
        issues.append('low-entropy')
    return issues

# 生产 MySQL 连接串占位（F1 安全债修复：已删除原 root:password 弱口令默认）。
# 真实部署必须由环境变量 DATABASE_URL 显式提供；值为 None 时，create_app()
# 会在生产环境直接抛 RuntimeError 拒绝启动，杜绝「漏配即连弱口令库」。
DEFAULT_PROD_DB_URI = None


def _truthy(raw, default=False):
    """把环境变量字符串解析成布尔值。

    Args:
        raw: 环境变量原始值，可能为 None。
        default: raw 为 None / 空串时返回的默认值。

    Returns:
        bool: 解析结果。'1'/'true'/'yes'/'on' 视为 True（大小写不敏感）。
    """
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if not text:
        return default
    return text in ('1', 'true', 'yes', 'y', 'on')


def _env_int(name, default):
    """把环境变量解析成 int，非法值回退默认（F11：外部配置不合法不应崩启动）。

    Args:
        name: 环境变量名。
        default: 解析失败或缺省时的回退值。

    Returns:
        int: 解析结果或回退默认值。
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning('环境变量 %s 非整数（%r），已回退默认 %s', name, raw, default)
        return default


def _with_utf8mb4(uri):
    """为 MySQL 连接串补齐 charset=utf8mb4，避免中文备注乱码（F27）。

    Args:
        uri: 原始 SQLAlchemy 连接串。

    Returns:
        str: 若为 MySQL 且未显式指定 charset，则追加 charset=utf8mb4。
    """
    if not uri or not uri.startswith('mysql'):
        return uri
    if 'charset=' in uri:
        return uri
    return uri + ('&' if '?' in uri else '?') + 'charset=utf8mb4'


def _resolve_dify_enabled():
    """统一从 DIFY_ENABLED 推导在线 AI 总开关，并与各后端 Key 做一致性检查（F3）。

    契约（单一真相源；第七轮起语义扩展为「在线 AI 总开关」，不只 Dify）：
      - DIFY_ENABLED 显式设置时以它为准（缺省 False）；
      - 未显式设置时，按「配了任一后端 Key（Dify 或 DeepSeek）即启用」自动推导，
        保持既有「未配 Key 自动回退规则版」的离线演示行为不变。

    Returns:
        bool: 是否启用在线 LLM 调用。
    """
    raw = os.environ.get('DIFY_ENABLED')
    has_key = bool(os.environ.get('DIFY_API_KEY')
                   or os.environ.get('DIFY_CHAT_APP_KEY')
                   or os.environ.get('DIFY_ADVICE_APP_KEY')
                   or os.environ.get('DEEPSEEK_API_KEY'))

    if raw is None or not str(raw).strip():
        # 未显式声明：沿用「有 Key 就启用」的历史行为
        return has_key

    enabled = _truthy(raw, default=False)
    # 一致性检查：只告警不改写，避免静默偏离运维意图
    if enabled and not has_key:
        logger.warning(
            'DIFY_ENABLED=%s 但未配置任何 DIFY_*_KEY，AI 调用将全部失败并回退提示，'
            '请检查 .env 配置。', raw)
    if not enabled and has_key:
        logger.warning(
            '已配置 DIFY_*_KEY 但 DIFY_ENABLED=%s，AI 能力保持关闭（规则版），'
            '如需启用请设置 DIFY_ENABLED=True。', raw)
    return enabled


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', DEV_SECRET_KEY)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # P7-E / S-9：请求体上限。八分区编辑器单次最多 12×20000 字符（约 240KB），
    # 取 1MB 留足余量；无此限制时单个 POST 可塞入数十 MB 文本撑爆 payload 与 DB。
    MAX_CONTENT_LENGTH = 1 * 1024 * 1024

    # ── 会话 Cookie 安全（F8）──
    # HTTPONLY：禁止 JS 读取 session cookie，缩小 XSS 危害面
    # SAMESITE=Lax：跨站 POST 不带 cookie，配合 CSRFProtect 双保险
    # SECURE：仅 HTTPS 下发送；开发环境（本地 http 演示）在子类关闭
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = True

    # ── 步骤4：Dify 私有化 AI 集成（腾讯云 Docker 部署）──
    # 未配置 DIFY_API_KEY 时，应用自动回退规则版，照常运行（演示不中断）
    DIFY_BASE_URL = os.environ.get('DIFY_BASE_URL', 'http://localhost:8080').rstrip('/')
    DIFY_API_KEY = os.environ.get('DIFY_API_KEY', '')
    DIFY_CHAT_APP_KEY = os.environ.get('DIFY_CHAT_APP_KEY', '')  # 问答/情景/考核 用 Chat App
    DIFY_ADVICE_APP_KEY = os.environ.get('DIFY_ADVICE_APP_KEY', '')  # 备货建议/损耗提醒 用 Completion App
    DIFY_ENABLED = _resolve_dify_enabled()
    DIFY_TIMEOUT = _env_int('DIFY_TIMEOUT', 30)

    # ── 第七轮：AI 后端可插拔（DeepSeek 云端 API / 私有化 Dify 双模式）──
    # LLM_PROVIDER：'auto'（默认，Dify Key 优先、无则用 DeepSeek）/ 'dify' /
    #   'deepseek' / 'none'；DIFY_ENABLED=False 时无论取值都走规则版（总开关）。
    # DeepSeek 为 OpenAI 兼容接口，仅用于在线推理；业务数据仍存自有库（安全红线）。
    LLM_PROVIDER = (os.environ.get('LLM_PROVIDER') or 'auto').strip().lower()
    DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
    DEEPSEEK_BASE_URL = os.environ.get(
        'DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')
    DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-chat')
    # PH2-3：开放注册与邮箱验证（Brevo HTTPS API）。
    # 未配置 BREVO_API_KEY / BREVO_SENDER_EMAIL 时自助注册自动关闭，
    # 平台管理员仍可人工开户（见 src/routes/auth.py 注册路由）。
    BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
    BREVO_SENDER_EMAIL = os.environ.get('BREVO_SENDER_EMAIL', '')
    BREVO_SENDER_NAME = os.environ.get('BREVO_SENDER_NAME', '智电轻擎')
    # 留空 = 跟随 Brevo 配置自动判定；显式 '0'/'false' 可强制关闭自助注册
    SIGNUP_ENABLED = os.environ.get('SIGNUP_ENABLED', '')

    # ── 响应压缩与静态资源缓存（性能 P0-1，2026-09-23）──
    # 标准库 gzip，零新增依赖；文本类响应才压，见 src/compression.py。
    # 关掉只用于排查「压缩导致的显示异常」：设 COMPRESS_ENABLED=0。
    COMPRESS_ENABLED = _truthy(os.environ.get('COMPRESS_ENABLED'), default=True)
    # 小于该字节数不压（CPU 换不到带宽）
    COMPRESS_MIN_SIZE = _env_int('COMPRESS_MIN_SIZE', 500)
    # 1–9：6 是压缩率/CPU 的常用折中
    COMPRESS_LEVEL = _env_int('COMPRESS_LEVEL', 6)
    # 静态资源浏览器缓存秒数。URL 未做指纹化，故不宜过长：取 1 小时，
    # 既能覆盖「一次会话内多次跳页」，又能让重新部署后最多 1 小时自然失效。
    STATIC_CACHE_SECONDS = _env_int('STATIC_CACHE_SECONDS', 3600)

    # ── H2 查询范围守卫（2026-09-24 开源对标改进批次1：租户隔离下沉框架层）──
    # 请求内已登录非平台账号的 ORM SELECT 自动注入租户范围条件（见 src/query_guard.py），
    # 路由漏写过滤的查询由守卫兜底；请求外/平台管理员/写路径不受影响。
    # 默认开（纯收紧：合法查询本就带同口径过滤，结果不变）；设 0 可回退纯路由过滤排查问题。
    QUERY_SCOPE_GUARD_ENABLED = _truthy(os.environ.get('QUERY_SCOPE_GUARD_ENABLED'), default=True)

    # ── OPT-A 只读开放 API + Webhook（2026-09-23 对标 P0）──
    # 两个开关**默认全部关闭**（对标批准口径「保持默认关、零行为变化」）：
    #   线上部署后不暴露 /api/v1、不外发任何 webhook，需运维显式设 OPEN_API_ENABLED=1
    #   / WEBHOOK_ENABLED=1 才启用；关闭态下 /api/v1 一律 404，现有路由/session/CSRF 零变化。
    OPEN_API_ENABLED = _truthy(os.environ.get('OPEN_API_ENABLED'), default=False)
    # Webhook 出站默认**关闭**：开启后每次填报/任务变更才会尝试外发。免费层无常驻
    # worker，外发以受限并发的守护线程完成，失败静默降级、绝不阻断主链路。
    WEBHOOK_ENABLED = _truthy(os.environ.get('WEBHOOK_ENABLED'), default=False)
    # 单次出站 HTTP 超时（秒）：超时即放弃本次投递并留痕，不重试、不排队堆积。
    WEBHOOK_TIMEOUT = _env_int('WEBHOOK_TIMEOUT', 3)
    # 同时在途投递上限（守护线程数）：达到即丢弃新事件并留痕，保护免费层内存/CPU。
    WEBHOOK_MAX_INFLIGHT = _env_int('WEBHOOK_MAX_INFLIGHT', 4)
    # 开放 API 限流：窗口秒 / 次数上限（按 IP + 令牌指纹计），复用 rate_limit 原子计数。
    OPEN_API_RATE_WINDOW = _env_int('OPEN_API_RATE_WINDOW', 60)
    OPEN_API_RATE_LIMIT = _env_int('OPEN_API_RATE_LIMIT', 60)


class DevelopmentConfig(Config):
    # P7-E / S-2：DEBUG 允许被 FLASK_DEBUG 单独覆盖（缺省仍为 True，本地开发零变化）。
    # 场景：FLASK_ENV 漏配时环境会静默回落 development（DEBUG=True，异常堆栈外泄、
    # 500 友好页被绕过）。托管面板若不方便补设 FLASK_ENV，可单独设 FLASK_DEBUG=0
    # 先关掉交互式调试器；根本解法仍是显式声明 FLASK_ENV=production。
    DEBUG = _truthy(os.environ.get('FLASK_DEBUG'), default=True)
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(BASE_DIR, 'data', 'ican.db')
    # 本地/离线演示走 http，Secure cookie 会导致会话失效，故仅开发环境关闭；
    # 生产环境继承 Config.SESSION_COOKIE_SECURE = True。
    SESSION_COOKIE_SECURE = False


class ProductionConfig(Config):
    DEBUG = False
    # 生产环境必须显式配置 SECRET_KEY（不继承开发默认值，防 session 伪造）
    SECRET_KEY = os.environ.get('SECRET_KEY')
    # 静态资源缓存：仅生产启用（开发留 0，改完 CSS 立刻生效，不被浏览器缓存咬住）
    SEND_FILE_MAX_AGE_DEFAULT = Config.STATIC_CACHE_SECONDS
    # 生产环境 DB 连接串必须由 DATABASE_URL 显式提供；缺失时为 None，
    # 由 create_app() 统一抛错（见 src/app.py），不在代码里内嵌任何默认口令。
    SQLALCHEMY_DATABASE_URI = _with_utf8mb4(
        os.environ.get('DATABASE_URL') or DEFAULT_PROD_DB_URI
    ) if (os.environ.get('DATABASE_URL') or DEFAULT_PROD_DB_URI) else None


config_map = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
}

# 生产环境别名（避免 FLASK_ENV=prod 时静默回落开发配置）
PRODUCTION_NAMES = frozenset({'production', 'prod'})
