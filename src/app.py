"""Flask 应用入口"""
import os

from dotenv import load_dotenv

# 必须在导入 src.config 之前加载 .env（F3）：
# config 模块在「导入期」就读取 os.environ 构建配置类，
# 若晚于 import 才 load_dotenv()，.env 里的配置将全部失效。
load_dotenv()

from flask import Flask, abort, redirect, render_template, request, send_from_directory, url_for  # noqa: E402
from flask_login import LoginManager, current_user  # noqa: E402
from flask_wtf.csrf import CSRFError, CSRFProtect  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

from src.config import (  # noqa: E402
    MIN_PROD_SECRET_LEN,
    PRODUCTION_NAMES,
    _env_int,
    _truthy,
    config_map,
    secret_strength_issues,
)

# 可信反向代理层数（P7-E / S-4）：默认 1（Cloud Studio / PythonAnywhere / Nginx 等单跳场景）；
# 直连部署（无反向代理）须显式设 TRUST_PROXY_HOPS=0，否则伪造的 XFF 会反向污染 remote_addr。
TRUST_PROXY_HOPS = _env_int('TRUST_PROXY_HOPS', 1)

# P7-E / S-1 过渡期逃生开关：线上（PythonAnywhere）尚未更换强密钥前设 ICAN_ALLOW_WEAK_SECRET=1，
# 弱密钥只告警不阻断；换好强密钥后必须移除该开关。
ALLOW_WEAK_SECRET = _truthy(os.environ.get('ICAN_ALLOW_WEAK_SECRET'), default=False)
from src import api_errors  # noqa: E402
from src import business_calendar, conflict_guard  # noqa: E402  # 第 29/31 轮：业务日历与写并发守卫
from src.models import ROLE_PLATFORM, Admin, Staff, db  # noqa: E402


def _error_copy(e):
    """Werkzeug 异常的中文人读文案：description 有就用，没有回落到状态码语义。"""
    desc = getattr(e, 'description', '') or ''
    return desc or api_errors.DEFAULT_COPY.get(
        api_errors.STATUS_CODE_MAP.get(getattr(e, 'code', None) or 0, ''), '请求未能完成。')

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
csrf = CSRFProtect()

# 基础 CSP：允许同源资源 + 内联脚本/样式（ECharts 初始化与进度条宽度为内联），
# 不放开任何外域，既满足私有化部署红线，也保证微信 H5 内嵌页正常渲染。
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

# CSRF 失败时的可操作提示（G1）。手机端最常见的成因是「会话 Cookie 没送达」：
# 用 http:// 链接打开（Secure Cookie 被浏览器直接丢弃）、无痕/省流模式的浏览器、
# 或站点 Cookie 被禁用。Flask-WTF 原始的英文报错对用户毫无指导意义。
CSRF_SESSION_HINT = (
    '登录会话已失效：浏览器没有把会话 Cookie 交给服务器。'
    '请改用 https:// 开头的链接重新打开本页，并确认浏览器未开启无痕/省流模式、已允许站点使用 Cookie。'
)


def resolve_config_name(config_name=None):
    """归一化运行环境名，支持 FLASK_ENV / ENV 两个变量与 prod 简写。

    Args:
        config_name: 显式传入的环境名，None 时读环境变量。

    Returns:
        str: 'production' 或 'development'。
    """
    raw = config_name or os.environ.get('FLASK_ENV') or os.environ.get('ENV') or 'development'
    normalized = str(raw).strip().lower()
    return 'production' if normalized in PRODUCTION_NAMES else 'development'


def env_explicitly_declared():
    """运行环境是否被**显式**声明（P7-E / S-2）。

    `resolve_config_name()` 在 FLASK_ENV / ENV 都未设置时会静默回落 'development'。
    本地开发与测试套件依赖该回落，不能改为拒绝启动；但回落必须**可见**，
    否则生产部署漏配环境变量时会以 DEBUG=True 运行而无人察觉。

    Returns:
        bool: FLASK_ENV 或 ENV 至少有一个非空即 True。
    """
    for name in ('FLASK_ENV', 'ENV'):
        raw = os.environ.get(name)
        if raw is not None and str(raw).strip():
            return True
    return False


def _enforce_production_guards(app):
    """生产环境启动守卫：SECRET_KEY（F2 + P7-E/S-1）与 DATABASE_URL（F1）。

    session 是**客户端签名 Cookie**，密钥即「伪造管理员会话」的钥匙，因此：
      - 缺失 → 拒启（原 F2）；
      - 命中已知弱值 / 长度不足 / 字符种类过少 → 拒启（S-1 新增，堵住
        「只比对开发默认值那一个字符串」的黑名单绕过）；
      - `ICAN_ALLOW_WEAK_SECRET=1` 时降级为醒目告警，供线上换 key 的过渡期使用。

    Args:
        app: 已加载配置类的 Flask 应用实例。

    Raises:
        RuntimeError: 任一守卫不通过（未被逃生开关放行）。
    """
    secret = app.config.get('SECRET_KEY')
    if not secret:
        raise RuntimeError(
            '生产环境必须通过环境变量显式配置 SECRET_KEY，且不得沿用开发默认值'
            '（参考 部署/.env.example）'
        )
    issues = secret_strength_issues(secret)
    if issues:
        hint = (
            '生产环境 SECRET_KEY 强度不足（{issues}）：长度需 ≥ {min_len} 位、'
            '不得是已知弱值或曾出现在仓库/文档中的值，且字符应有足够随机性。'
            '生成方式：python -c "import secrets;print(secrets.token_urlsafe(48))"'
        ).format(issues='/'.join(issues), min_len=MIN_PROD_SECRET_LEN)
        if ALLOW_WEAK_SECRET:
            app.logger.warning('[安全降级] %s（已由 ICAN_ALLOW_WEAK_SECRET=1 '
                               '放行，请尽快更换强密钥后移除该开关）', hint)
        else:
            raise RuntimeError(hint)
    # F1 安全债修复：生产环境必须显式配置 DATABASE_URL（MySQL 连接串），
    # 缺失或仍是占位 None 时拒绝启动，杜绝「漏配即连弱口令默认库」。
    if not app.config.get('SQLALCHEMY_DATABASE_URI'):
        raise RuntimeError(
            '生产环境必须通过环境变量 DATABASE_URL 配置 MySQL 连接串，'
            '不得在代码中内嵌任何默认口令（参考 部署/.env.example）'
        )


def create_app(config_name=None):  # noqa: PLR0915
    config_name = resolve_config_name(config_name)

    app = Flask(__name__)
    app.config.from_object(config_map[config_name])

    # 第 29 轮 H77（InvenTree `settings.py:741-747` 同族）：业务时区配了就当场校验，
    # 配错必须**启动失败**，而不是静默按进程时钟跑 —— 后者每天会有 8 小时把"今天"算成昨天，
    # 且不报错、不告警，只在店主早上补交数据时表现为"昨天的数不见了"。
    _cal_problems = business_calendar.validate_config()
    if _cal_problems:
        raise ValueError('；'.join(_cal_problems))

    # 反向代理适配（G1）：Cloud Studio / EdgeOne 在 TLS 终止后以 http 回源，
    # 不信任 X-Forwarded-Proto 时 request.is_secure 恒为 False，https 判定全部失真。
    # P7-E / S-4：`x_for` 必须一并开启，否则 X-Forwarded-For 不被改写，
    # rate_limit.request_ip() 又无条件信任它 → 换一个 XFF 就是全新限流桶（登录爆破防护失效）。
    # 可信代理层数由 TRUST_PROXY_HOPS 控制；前面没有反向代理时请设为 0。
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUST_PROXY_HOPS, x_proto=1)

    if config_name == 'production':
        _enforce_production_guards(app)

        # 结构化日志（生产态 JSON + 文件轮转 + 请求级追踪）
        from src.logging_config import inject_request_id, setup_logging
        setup_logging(app, production=True)
        inject_request_id(app)

        # 生产环境强制 https（G1）：会话 Cookie 带 Secure 标记，若用户拿到的是
        # http:// 链接，浏览器会直接丢弃该 Cookie，表现为「首页能打开、一点登录就
        # 400（The CSRF session token is missing.）」。
        # 仅在反向代理明确声明「客户端走的是 http」时跳转；探测不到协议时不动作，
        # 避免误判造成 301 重定向死循环。
        @app.before_request
        def _force_https():
            proto = (request.headers.get('X-Forwarded-Proto') or '').split(',')[-1].strip().lower()
            if proto == 'http':
                return redirect(request.url.replace('http://', 'https://', 1), code=301)

        # P7-E / S-2：生产 DEBUG 二次兜底。配置类里 ProductionConfig.DEBUG 已是 False，
        # 此处再钉一次，防止后续任何人用 app.config.update / 子类覆写把调试器放出去
        # （DEBUG 打开会让 500 友好页失效、异常堆栈与内网路径直出）。
        app.config['DEBUG'] = False
        app.config['PROPAGATE_EXCEPTIONS'] = False
    elif not env_explicitly_declared():
        # P7-E / S-2：环境名来自「缺省回落」而非显式声明 —— 本地开发与测试套件依赖
        # 该回落（不能拒绝启动），但生产部署漏配 FLASK_ENV 时会静默以 DEBUG=True 运行，
        # 故在此把回落变成**可见**事件。
        app.logger.warning(
            '[环境回落] FLASK_ENV / ENV 均未显式声明，已回退 development（DEBUG=%s）。'
            '生产部署请显式设置 FLASK_ENV=production；若暂时无法设置，至少设 FLASK_DEBUG=0 '
            '关闭调试器。', app.config.get('DEBUG'))
        # 第 19 轮 H39：开发态同样要有 request_id 与结构化日志出口。
        # 改造前只有 production 分支注册 inject_request_id，于是「缺省回落」分支
        # （测试套件与漏配 FLASK_ENV 的部署都走这里）**既没有日志配置也没有追踪 id**，
        # AI 留痕与访问日志在开发态永远串不起来 —— 归因能力不该由环境变量决定。
        from src.logging_config import inject_request_id, setup_logging
        setup_logging(app, production=False)
        inject_request_id(app)
    else:
        # 开发态日志：人类可读 + 控制台
        from src.logging_config import inject_request_id, setup_logging
        setup_logging(app, production=False)
        inject_request_id(app)

    # 初始化 Dify 客户端（步骤4 AI 集成，未配置则自动规则版）
    from src.dify_client import dify_client
    dify_client.init_app(app)

    # 初始化扩展
    # 干净解压场景：源码 ZIP 不携带 data/，创建 SQLite 父目录后再交给 SQLAlchemy。
    _db_uri = app.config.get('SQLALCHEMY_DATABASE_URI') or ''
    if _db_uri.startswith('sqlite:///'):
        _db_path = _db_uri[len('sqlite:///'):]
        if _db_path and _db_path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(_db_path)), exist_ok=True)
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)  # 全站 CSRF 防护（测试可用 WTF_CSRF_ENABLED=False 关闭）

    # H2 查询范围守卫（2026-09-24 对标批次1）：租户隔离下沉为 SQLAlchemy 全局钩子，
    # 请求内漏写租户过滤的 ORM SELECT 自动兜底；平台管理员/请求外/写路径零影响。
    from src import query_guard
    query_guard.init_app(app)

    # H59 变更归因链（2026-09-27 对标第 26 轮）：before_flush 监听器统一记「谁改了哪条」，
    # 与 H2 同一姿态 —— 归因不能靠业务代码自觉调用，新增写点自动被覆盖。
    # 审计写失败只计数不打断业务写（计数经 /healthz.alerts 与门禁第 40 项可见）。
    from src import change_log
    change_log.init_app(app)

    # 响应压缩（性能 P0-1，2026-09-23）：文本类响应 gzip，标准库实现、零新增依赖。
    # 收益最大的是三处大资源：style.css 80KB、echarts.custom.js 604KB、
    # 首屏 3D 叙事页 vendor 855KB —— 均降到约 1/3.6。开关见 src/config.py。
    from src import compression
    compression.init_app(app)

    # 性能监控（慢请求告警 + SQLAlchemy 慢查询日志 + 内存快照端点）
    from src.performance import init_performance_monitor
    init_performance_monitor(app)

    # 注册蓝图
    from src.routes.admin import admin_bp
    from src.routes.ai import ai_bp
    from src.routes.auth import auth_bp
    from src.routes.decision import decision_bp
    from src.routes.demo import demo_bp
    from src.routes.diy import diy_bp
    from src.routes.h5 import h5_bp
    from src.routes.industry import industry_bp
    from src.routes.multi import multi_bp
    from src.routes.stores import stores_bp
    from src.routes.tasks import tasks_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(demo_bp)  # 免登录只读演示入口（GET /demo）
    app.register_blueprint(h5_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(decision_bp)
    app.register_blueprint(industry_bp)
    app.register_blueprint(stores_bp)
    app.register_blueprint(diy_bp)
    app.register_blueprint(multi_bp)
    app.register_blueprint(tasks_bp)

    # OPT-A 只读开放 API（Token 认证的独立认证分支，GET 只读 + session owner 管理端点）
    from src.api_open import open_api_bp
    app.register_blueprint(open_api_bp)

    # 对标第 27 轮 H64：端点授权面**默认拒绝**（deny by default）。
    # 改造前每个端点是否可达取决于「有没有人记得挂装饰器」—— 实测 url_map 86 条里 16 条不带
    # 视图级守卫，而盘上没有任何公开名册，新增端点漏挂装饰器会**静默公开**且无判据会红。
    # 上游口径：InvenTree `settings.py:521-522` 把 DRF 默认权限钉成 IsAuthenticated；
    # Odoo `ir_http.py:392` 按路由声明的 `auth` 单点派发（CORS 预检都要显式写 `auth='none'`）；
    # Frappe `whitelist(allow_guest=False)` 默认不对游客开放。⇒ 开放必须是声明出来的。
    # 本钩子不重复做鉴权（角色判定仍在装饰器与视图内），只堵「没登记就可达」这一类。
    from src import authz_ledger

    @app.before_request
    def _authz_backstop():
        endpoint = request.url_rule.endpoint if request.url_rule is not None else None
        if authz_ledger.backstop_decision(endpoint):
            return None
        app.logger.warning(
            '[授权名册] 端点 %s 既不在豁免档也无视图级守卫，已默认拒绝（请在 '
            'src/authz_ledger.py 显式声明档位与理由）', endpoint)
        abort(403, description='该入口未在授权名册中登记')

    # 行业包 DB 覆盖层注册（P4 配置后台）：注入取数函数 + 恢复持久化激活态。
    # 首次访问包内容时才建表查询；DB 不可用时 override 内部吞异常回退文件包。
    from src import industry_pack, pack_store
    industry_pack.register_db_override(pack_store.override_fn)
    with app.app_context():
        try:
            pack_store.restore_active()
        except Exception as exc:  # 启动自检失败不阻断（回退默认包链）
            app.logger.warning('[app] 行业包激活态恢复失败（按默认继续）: %s', exc)

    @app.context_processor
    def inject_admin_pack_switcher():
        """管理员顶部常驻行业包切换器（P10 可发现性修复）。"""
        try:
            user_id = current_user.get_id() or ''
        except Exception:
            return {}
        if not (current_user.is_authenticated and user_id.startswith('admin-')):
            return {}
        if getattr(current_user, 'role', None) == ROLE_PLATFORM:
            return {}
        try:
            from src import tenant
            return {
                'admin_pack_options': pack_store.all_packs(),
                'admin_active_pack': pack_store.active_pack_id(),
                # PH2-3D：门店选择器（老板/店长；平台管理员无业务门店 → 空列表）
                'admin_store_options': tenant.store_options_for(current_user),
                'admin_current_store_id': tenant.current_store_id(),
            }
        except Exception:
            return {}

    # ── 静态资产版本化（性能 P0-2，2026-09-23）────────────────────
    # url_for('static') 不带版本参数，发布后浏览器可能在缓存期内继续用旧
    # CSS/JS（新 critical + 旧 style 的错位风险）。模板改用 asset_url()，
    # 以文件 mtime 作版本戳注入 ?v=；stat 失败（文件缺失）降级为原 URL。
    _static_v_cache = {}

    def _static_version(filename):
        try:
            v = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            v = _static_v_cache.get(filename)
        if v:
            _static_v_cache[filename] = v
        return v

    @app.context_processor
    def inject_asset_url():
        def asset_url(filename):
            v = _static_version(filename)
            base = url_for('static', filename=filename)
            return f'{base}?v={v}' if v else base
        return {'asset_url': asset_url}

    # ── 安全响应头（F8）──
    # Flask 不内置安全头，统一在 after_request 补齐；用 setdefault 避免覆盖
    # 视图已显式设置的同名头。
    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault('Content-Security-Policy', CONTENT_SECURITY_POLICY)
        # 2026-09-18 双向迭代 S8：HSTS。仅 HTTPS 连接下发（本地 http 不下发，避免锁死开发环境）；
        # 部署到 HTTPS 域名后浏览器即强制加密，防降级攻击（原有头缺此项）。
        if request.is_secure or app.config.get('FORCE_HSTS'):
            response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        return response

    # 产品首页（首页面，不强制跳转登录）
    # ── 探活端点（2026-09-18 双向迭代 S1；闭环事故复盘 I-5「全仓无 /healthz」）──
    # 与超市web /_health 同构：只回显状态，绝不泄露密钥/内网地址。
    # ai.configured 用于发现「改了配置却忘重启」与「AI 静默降级为规则版」。
    @app.route('/healthz')
    def healthz():
        db_state = 'error'
        db_ms = None
        try:
            import time as _time

            from flask import jsonify
            from sqlalchemy import text as _text

            from src.models import db as _db
            _t0 = _time.perf_counter()
            _db.session.execute(_text('SELECT 1'))
            db_ms = round((_time.perf_counter() - _t0) * 1000, 1)
            db_state = 'ok'
        except Exception as exc:  # 探活只报告、不掩盖：DB 异常也必须返回 200 + db=error
            app.logger.warning('[healthz] db check failed: %s', exc)
        import os as _os
        has_key = bool(_os.environ.get('DIFY_API_KEY') or _os.environ.get('DIFY_CHAT_APP_KEY')
                       or _os.environ.get('DIFY_ADVICE_APP_KEY') or _os.environ.get('DEEPSEEK_API_KEY'))
        from src import ai_gate as _ag
        from src import diag as _diag
        from src import schema_ledger as _sl
        from src import version as _ver

        # H27/H28 的两个事实合成一次读取：status 不能再对 db=error 说 ok（第 19 轮 H38）。
        # 判定集合与 smoke_remote.check_healthz 逐条对齐，两处必须同进同退。
        schema_state = _sl.status()
        healthy = (db_state == 'ok' and schema_state.get('ledger_table')
                   and not schema_state.get('pending') and not schema_state.get('orphans'))
        # 第 23 轮 H51：把已有聚合数与阈值比较，产出机器可读告警。
        # status 的判定集合**一字不改**（它是对外契约，smoke_remote 按 status=='ok' 判部署冒烟），
        # 告警只加字段；路径名/SQL 模板/表名一律不进这里（alerts.build_metrics 只取聚合整数）。
        from src import alerts as _alerts
        from src import business_calendar as _bc
        from src import exit_audit as _ea
        alert_fields = _alerts.healthz_fields(
            db_ms=db_ms, diag_stats=_diag.healthz_stats(), ai_stats=_ag.stats(),
            audit_stats=_ea.stats())
        return jsonify({
            'status': 'ok' if healthy else 'degraded',
            # H28 版本契约：仅凭 HTTP 就能回答"这跑的是哪一版"（对标 Appwrite /v1/health/version）
            'version': _ver.payload(),
            # 第 29 轮 H77：业务日与解析路径必须在线上可见 —— 服务器时钟是 UTC 时，
            # "今天"是不是店主的那个今天要能一眼核出来；走固定偏移降级也必须看得见（禁静默）
            'calendar': _bc.describe(),
            'conflict': conflict_guard.describe(),
            # H27 迁移账本：同一问题在库这一侧的答案 —— 未跑完的补丁必须在线上可见，
            # 而不是等人去猜"这台是不是又忘了 --if-empty"。只报告不拦截（判红交部署链与门禁）。
            'schema': schema_state,
            # 依赖探测带耗时（对标 Appwrite health/DB/Get.php 的 ping 毫秒字段）：
            # 只有 ok/error 二值的探活看不出「活着但很慢」这一档故障
            'db': db_state,
            'db_ms': db_ms,
            'ai': {'configured': has_key, 'source': 'dify' if has_key else 'rule',
                   # 并发闸实况：inflight 长期贴近 limit、rejected 持续增长
                   # = AI 正在被背压降级，是「静默降级」的可见入口（闭环 I-5 同源）
                   **_ag.stats()},
            # H36 观测计数：只有聚合整数。路径名/表名一律不出现在公开端点
            # （沿用第 10 轮 L22 口径：schema 情报不进 /healthz）
            'diag': _diag.healthz_stats(),
            # 时刻口径与 AI 留痕同一个单源（UTC + Z + 微秒），否则按时间戳对表差 8 小时
            'ts': _diag.utc_iso_z(),
            # alerts/alert_count：阈值判定结果（空数组=无告警）。判定单源在 src/alerts.py，
            # 部署侧冒烟 smoke_remote.py 读同一份，两处不各写一份阈值。
            **alert_fields,
        }), 200

    # ── 首屏与产品首页（2026-09-22）──────────────────────────────
    # `/` 改为滚轮驱动的 3D 产品叙事页（打开站点第一眼即品牌叙事），
    # 原产品首页迁到 `/portal`，入口功能不丢失。
    # 产物放在 `src/narrative/`，**不可放进 `static/js/`**：门禁第 12 项对
    # `static/js` 设 700KB 预算，而 three/react/gsap 三个 vendor 合计已 855KB。
    # 产物内引用是相对路径（`./assets/*`、`./favicon.svg`），故补两条托管路由。
    narrative_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'narrative')

    @app.route('/')
    def index():
        """首屏：3D 产品叙事页；产物缺失时回退产品首页（不白屏）。"""
        if os.path.exists(os.path.join(narrative_dir, 'index.html')):
            resp = send_from_directory(narrative_dir, 'index.html')
            # 入口 HTML 必须每次回源：否则发布后拿不到新构建的带 hash 资产名。
            resp.headers['Cache-Control'] = 'no-cache'
            return resp
        return render_template('index.html')

    @app.route('/assets/<path:filename>')
    def narrative_assets(filename):
        """3D 叙事页构建产物（CSS / JS chunk）。"""
        resp = send_from_directory(os.path.join(narrative_dir, 'assets'), filename)
        # 文件名自带内容 hash（内容变 → 文件名变），可放心长缓存；入口 index 不在此列。
        resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return resp

    @app.route('/favicon.svg')
    def narrative_favicon():
        """3D 叙事页图标（主系统模板走 `/static/favicon.svg`，两者互不影响）。"""
        return send_from_directory(narrative_dir, 'favicon.svg')

    @app.route('/portal')
    def portal():
        """产品首页（原 `/`，保留登录 / 注册 / 演示等全部入口）。"""
        return render_template('index.html')

    # ── 友好错误页（替代白屏堆栈，演示/生产不丢分）──
    # 第 22 轮 H48：每条 handler 都先过 `api_errors.api_error_or` —— 落在 `/api/v1/` 上的请求
    # 回 JSON（与 openapi.yaml 的 Error schema 同形），浏览器路由照旧回 HTML 错误页。
    # 分支收在一个函数里而不是五处各写一遍，是为了让门禁第 34 项能按 AST 证明"每条 handler
    # 都经过这里"：漏一条就是一条真实的 HTML 错误腿（本轮实测 6 条里有 5 条是这么吐 HTML 的）。
    @app.errorhandler(CSRFError)
    def csrf_error(e):
        """CSRF 校验失败：写操作照旧被拒绝，但不再把英文报错甩给用户（G1）。

        Flask-WTF 的 "The CSRF session token is missing." 只可能出现在
        「表单带了 token、服务端却读不到会话」时 —— 即会话 Cookie 没送达
        （http 链接 / 无痕与省流模式 / 浏览器禁用 Cookie / 服务端换了密钥）。
        """
        app.logger.warning('CSRF 校验失败：%s %s（%s）',
                           request.method, request.path, e.description)
        return api_errors.api_error_or(
            400, e, lambda: (render_template('400.html', message=CSRF_SESSION_HINT), 400))

    @app.errorhandler(400)
    def bad_request(e):
        return api_errors.api_error_or(
            400, e,
            lambda: (render_template('400.html', message=_error_copy(e)), 400))

    @app.errorhandler(401)
    def unauthorized(e):
        return api_errors.api_error_or(
            401, e, lambda: (render_template('403.html', message=_error_copy(e)), 401))

    @app.errorhandler(403)
    def forbidden(e):
        return api_errors.api_error_or(
            403, e, lambda: (render_template('403.html', message=_error_copy(e)), 403))

    @app.errorhandler(404)
    def page_not_found(e):
        return api_errors.api_error_or(404, e, lambda: (render_template('404.html'), 404))

    @app.errorhandler(405)
    def method_not_allowed(e):
        return api_errors.api_error_or(
            405, e, lambda: (render_template('400.html', message=_error_copy(e)), 405))

    @app.errorhandler(429)
    def too_many_requests(e):
        return api_errors.api_error_or(
            429, e, lambda: (render_template('429.html', message=_error_copy(e)), 429))

    @app.errorhandler(500)
    def internal_error(e):
        # 第 19 轮 H40：错误页自带追踪编号。traceback 由 Flask 打在 app.logger 里，
        # 但用户看得见的是这张页 —— 没有编号，报障就只剩「刚才卡了一下」。
        # 响应头 X-Request-Id 由 logging_config 的 after_request 统一挂（所有响应同一条腿）。
        from flask import g
        return api_errors.api_error_or(
            500, e,
            lambda: (render_template('500.html',
                                     request_id=getattr(g, 'request_id', '')), 500))

    return app


@login_manager.user_loader
def load_user(user_id):
    """从 session 加载用户（先查 Admin，再查 Staff）。

    Args:
        user_id: session 中存储的用户标识，统一格式 "admin-1" / "staff-1"。

    Returns:
        Admin | Staff | None: 解析失败或用户不存在时返回 None（跳登录），
        绝不因坏 session 抛异常导致 500（F10）。
    """
    if not user_id or '-' not in user_id:
        return None
    role, raw_uid = user_id.split('-', 1)
    try:
        uid = int(raw_uid)
    except (TypeError, ValueError):
        # 坏 session（被篡改 / 旧格式残留）→ fail-safe 视为未登录
        return None
    from src import auth_epoch
    user = None
    if role == 'admin':
        user = db.session.get(Admin, uid)
        if user is not None:
            if not auth_epoch.session_epoch_valid():
                user = None
    elif role == 'staff' and auth_epoch.session_epoch_valid():
        user = db.session.get(Staff, uid)
    return user


# 命令行入口
if __name__ == '__main__':
    app = create_app()
    # debug 跟随环境：生产环境（FLASK_ENV=production）强制关闭
    # 生产部署请改用 waitress（见 部署/README.md），本入口仅供开发/演示
    app.run(host='0.0.0.0', port=5000,  # noqa: S104  理由：仅 __main__ 开发/演示入口（手机联调需局域网可达）；生产按 部署/README 走 waitress，且 app.py 有生产密钥守卫
            debug=(resolve_config_name() != 'production'))
