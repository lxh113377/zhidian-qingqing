#!/usr/bin/env python3
# 生成「演示快照集」：用真实 app + test_client 抓取各页面，内联 CSS、按需内联 echarts，
# 剥离服务端 main.js，输出为一套自包含静态 HTML（双击即可看，便于比赛演示/截图）。
# V2：站内路由链接重写为快照文件名（12 页互连闭环）；导航页接暗色设计体系。
# V3：CI 可移植——ROOT 允许 ICAN_ROOT 覆盖，输出目录允许 ICAN_SNAPSHOT_OUT 覆盖
#     （本地默认 junction 权威路径；CI 上 GitHub Actions 注入临时输出目录再由 workflow 推送）。
# V4（2026-09-09 审查修复）：权威目录迁移后默认路径推导失效——脚本现居 代码仓库/
#     根（与 src 同级），旧逻辑「上溯一层」会把 ROOT 指到 代码仓库 之外，
#     CSS 读取崩溃、OUTDIR 落到 代码仓库/iCAN-参赛交付物/… 生成嵌套垃圾（已清理）。
#     现改为：脚本目录含 src/ 即认作 ROOT；否则按旧布局上溯一层。输出目录默认
#     ROOT/演示快照集（环境变量仍最优先，CI 不受影响）。
# V5（2026-09-10）：配置了 Dify/DeepSeek Key（dify_client.enabled=True）时，
#     AI 聊天页快照预调用真实 AI 生成 3-5 条问答并渲染进快照 HTML（capture_ai_chat）；
#     无 Key 或 AI 调用失败自动回退规则版整页抓取，快照构建永不中断（CI 稳定）。
import io, os, re, sys
from datetime import date, timedelta

# 快照构建会在数十秒内反复登录多个角色；登录限流属于生产防护，
# 不应把 CI 抓取序列打成 429/403。测试进程显式关闭限流。
os.environ.setdefault("DISABLE_RATE_LIMIT", "1")

# 项目根：优先环境变量（CI）；缺省时「脚本所在目录含 src/」→ 权威布局（脚本在 代码仓库/），
# 否则回退旧布局「脚本在 archive/ 下」上溯一层
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ICAN_ROOT") or (
    _HERE if os.path.isdir(os.path.join(_HERE, "src"))
    else os.path.abspath(os.path.join(_HERE, "..")))
# 快照输出目录：优先环境变量（CI），缺省 ROOT/演示快照集
OUTDIR = os.environ.get("ICAN_SNAPSHOT_OUT") or os.path.join(ROOT, "演示快照集")
CSS = os.path.join(ROOT, "src", "static", "css", "style.css")
CSS_CRITICAL = os.path.join(ROOT, "src", "static", "css", "critical.css")
ECHARTS = os.path.join(ROOT, "src", "static", "js", "echarts.custom.js")
sys.path.insert(0, ROOT)

# ── M8（2026-09-24 第 9 轮）：快照集「对外交付副本」自动镜像 ──────────────────
# 生成器只写 ROOT/演示快照集（源码包内那份）。对外提交包里还有
# 工程佐证材料/01-演示快照集 一份，此前只能靠人工覆盖（第 8 轮手工做过）——
# 漏一步就是「提交包里的快照静默陈旧」，且没有任何守卫会报错。现由本脚本负责，
# 并在镜像后逐文件比字节；不一致直接非零退出（宁可红，不要假装成功）。
import hashlib
import shutil
from src import business_calendar as cal


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _find_mirror_dir(root):
    """向上最多 8 层找 交付物/*/工程佐证材料/01-演示快照集；找不到返回 None。

    CI / 独立检出里没有交付目录属正常，调用方按「跳过」处理而非报错。
    """
    env = os.environ.get('ICAN_SNAPSHOT_MIRROR')
    if env:
        return env if os.path.isdir(env) else None
    cur = os.path.abspath(root)
    for _ in range(8):
        cur = os.path.dirname(cur)
        probe = os.path.join(cur, '交付物')
        if not os.path.isdir(probe):
            continue
        for sub in sorted(os.listdir(probe)):
            cand = os.path.join(probe, sub, '工程佐证材料', '01-演示快照集')
            if os.path.isdir(cand):
                return cand
    return None


def mirror_snapshot_copy(src, dst):
    """整份镜像 + 逐文件字节复核。返回 (退出码, 摘要行列表)。"""
    lines = []
    if not os.path.isdir(src):
        return 0, ['MIRROR SKIP：本副本无快照集（' + src + '）']
    src_files = sorted(f for f in os.listdir(src) if os.path.isfile(os.path.join(src, f)))
    if not src_files:
        return 1, ['MIRROR FAIL：' + src + ' 为空，拒绝把空集镜像到交付副本']
    if not os.path.isdir(dst):
        return 0, ['MIRROR SKIP：交付副本不存在（' + dst + '）']
    bad = []
    for name in src_files:
        a, b = os.path.join(src, name), os.path.join(dst, name)
        if not os.path.exists(b) or _sha(a) != _sha(b):
            shutil.copy2(a, b)
            if not os.path.exists(b) or _sha(a) != _sha(b):
                bad.append(name)
    stale = [f for f in sorted(os.listdir(dst))
             if f.endswith('.html') and f not in src_files]
    for name in stale:                      # 只清 .html 残留，别动人工放的其它材料
        os.remove(os.path.join(dst, name))
    if bad:
        return 1, ['MIRROR FAIL：复核不一致 ' + '、'.join(bad[:5])]
    return 0, ['MIRROR OK：%d 文件已镜像并逐文件字节复核通过' % len(src_files)
               + ('；清出过期页 %d 个' % len(stale) if stale else '')]


MIRROR_SRC = OUTDIR
MIRROR_DST = _find_mirror_dir(ROOT)
if '--mirror-only' in sys.argv:            # 只镜像不重建：不建库、不抓页、不碰 data/
    _code, _lines = mirror_snapshot_copy(MIRROR_SRC, MIRROR_DST) if MIRROR_DST         else (0, ['MIRROR SKIP：未找到交付副本目录（01-演示快照集）'])
    for _l in _lines:
        print(_l)
    sys.exit(_code)

# 快照翻页导航样式（追加进每页内联 <style>，仅非 index 页使用；亮/暗双主题适配）
SNAP_NAV_CSS = """
/* 快照翻页导航（仅演示快照集页面） */
.snap-nav{position:fixed;bottom:12px;left:50%;transform:translateX(-50%);display:flex;gap:4px;
  background:var(--card-bg);border:1px solid var(--border);border-radius:999px;padding:4px 6px 4px 10px;
  box-shadow:var(--shadow-lg);z-index:300;font-size:12px;white-space:nowrap}
.snap-nav a{color:var(--primary);text-decoration:none;padding:4px 10px;border-radius:999px;display:inline-flex;align-items:center}
.snap-nav a:hover{background:var(--hover-bg)}
.snap-nav a.disabled{pointer-events:none;color:var(--text-light);opacity:.5}
/* 静态只读角标（U4）：与可点击导航区分，避免「按钮坏了」误读 */
.snap-hint{position:fixed;left:12px;bottom:12px;z-index:300;background:var(--card-bg);
  color:var(--text-light);border:1px solid var(--border);border-radius:999px;
  padding:4px 10px;font-size:12px;box-shadow:var(--shadow-sm);max-width:60vw;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
"""

# 静态演示模式脚本（V4 注入每页 </head> 前，先于页内脚本执行）：
# 快照剥离 main.js 且无后端，页内 fetch/批量操作/问答提交必然失败，
# 原先表现为弹「网络错误」告警——演示观感差且易被误读为「跳转坏了」。
# 统一拦截为友好提示：fetch 返回固定 JSON（页内 .json() 消费方原样兼容），
# alert 静默、confirm 拒绝（写操作本就不应发生在静态快照里）。
# 真实系统不经过本生成器，完全不受影响。
SNAPSHOT_MODE_JS = """
<script>
/* 演示快照静态模式：拦截联网/写交互，统一友好提示 */
(function () {
    window.__SNAPSHOT__ = true;
    var MSG = '演示快照为静态页面，写入与联网交互请在真实系统中体验';
    function fakeResponse() {
        if (typeof Response === 'function') {
            return new Response(JSON.stringify({ok: false, reply: MSG, message: MSG}),
                {status: 200, headers: {'Content-Type': 'application/json'}});
        }
        return { json: function () { return Promise.resolve({ok: false, reply: MSG, message: MSG}); } };
    }
    window.fetch = function () { return Promise.resolve(fakeResponse()); };
    window.alert = function () {};
    window.confirm = function () { return false; };
    // U4：加静态只读角标，明确「本页仅浏览、写入为模拟」，消除交互按钮「坏了」的误读
    function addHint() {
        if (!document.body || document.querySelector('.snap-hint')) { return; }
        var chip = document.createElement('div');
        chip.className = 'snap-hint';
        chip.textContent = '演示快照（静态只读）：交互按钮已模拟';
        document.body.appendChild(chip);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', addHint);
    } else {
        addHint();
    }
})();
</script>
"""

# 1) 重置并灌入演示数据
from init_db import init_db
init_db()

# 2) 起 app（默认 DIFY_ENABLED=False -> 规则版，无需 Key）
from src.app import create_app
app = create_app("development")
# 真实 AI 问答预生成（V5）读取同一客户端单例的 enabled 开关（配 Key 即在线）
from src.dify_client import dify_client
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False  # 快照抓取走 test_client，关闭 CSRF（页面 token 不影响静态渲染）
client = app.test_client()

# PH5/P0：免登录只读演示入口（/demo）需要绑定一个合成演示商家才能渲染 200。
# 这里用与真实系统同一个环境变量，让快照页与线上入口的渲染结果完全一致
# （未绑定 → /demo 返回 404，快照页会变成错误页，故必须显式绑定）。
from src.models import Admin as _AdminSnap  # noqa: E402

with app.app_context():
    _snap_owner = _AdminSnap.query.filter_by(username='owner_demo').first()
    if _snap_owner and _snap_owner.merchant_id:
        os.environ['DEMO_MERCHANT_ID'] = str(_snap_owner.merchant_id)
        print('演示商家已绑定 /demo：merchant_id =', _snap_owner.merchant_id)
    else:
        print('警告：未找到 owner_demo 的商家归属，/demo 快照页将渲染为 404')


def seed_snapshot_task():
    """为静态快照补一条可见的责任任务，覆盖老板端与店员端页面。"""
    from src import task_flow
    from src.models import Admin, Staff, Store, StoreTask
    with app.app_context():
        owner = Admin.query.filter_by(username='owner_demo').first()
        staff = Staff.query.filter_by(username='milk_tea_staff1').first()
        store = Store.query.filter_by(code='milk_tea').first()
        if not (owner and staff and store):
            print('快照任务未创建：演示账号或门店缺失')
            return
        # 旧快照任务用 source='snapshot'，自 85880b9 起该值已不在 task_flow.ALLOWED_SOURCES
        # 白名单内（create_task 直接抛「任务来源不合法」，快照链断裂）。这条任务的语义就是
        # 「跨店分析证据 → 老板确认转任务」，改用正规来源，而不是反过来放宽白名单。
        (StoreTask.query
         .filter(StoreTask.source.in_(['snapshot', task_flow.SOURCE_AI_DECISION]))
         .delete(synchronize_session=False))
        task_flow.create_task(
            merchant_id=owner.merchant_id,
            store_id=store.id,
            title='核查奶茶示范店损耗异常',
            detail='对照跨店分析证据，检查备货量与储存条件，完成后填写效果回看。',
            due_date=cal.business_today() + timedelta(days=2),
            assignee_role='staff',
            assignee_id=staff.id,
            source=task_flow.SOURCE_AI_DECISION,
            evidence=[{'store_id': store.id, 'metric': 'waste_rate', 'value': 0.2}],
            created_by_admin_id=owner.id,
        )
        print('快照任务已创建：核查奶茶示范店损耗异常')


seed_snapshot_task()

with io.open(CSS, encoding="utf-8") as f:
    css = f.read()
with io.open(CSS_CRITICAL, encoding="utf-8") as f:
    css_critical = f.read()
with io.open(ECHARTS, encoding="utf-8") as f:
    echarts = f.read()


def login(username, password, role):
    if not username:
        # public：免登录入口，需**确保匿名**（test_client 会保留上一个角色的会话，
        # 不退登就抓不到真实的匿名渲染结果）
        client.post("/logout", follow_redirects=True)
        return
    client.post("/login", data={"username": username, "password": password, "role": role},
                follow_redirects=True)


DEMO_LOGIN = {
    "admin": ("owner_demo", "owner123", "admin"),
    "staff": ("milk_tea_staff1", "staff123", "staff"),
    "platform": ("platform_demo", "platform123", "admin"),
    "public": (None, None, None),  # 免登录只读演示入口（匿名抓取）
}


# ── 站内路由 → 快照文件名映射（静态闭环：页内按钮互跳不断链）──
# 精确 href 替换按 key 长度降序（长前缀优先）；带引号匹配不会误伤前缀路由
# 路由 → 快照文件名「只有一份表」：页面表 PAGES。此前这里手抄了 26 条，于是第 31 轮
# 给 PAGES 加一页（25_变更归因）时 LINK_MAP 并不知道 ⇒ 快照里 href=/admin/changes 没被重写，
# 门禁第 23 项按被引资源判红（同一个判断两处实现，漂移只是时间问题）。
# 这里只保留「不是页面的路由」：登录/登出/根路径都降级到导航首页。
_NON_PAGE_LINKS = {
    "/login": "index.html",
    "/logout": "index.html",
    "/admin/industry/": "17_建包工作台.html",
    "/": "index.html",
}
LINK_MAP = {}


def rewrite_links(html):
    """把快照页内的服务端路由 href 重写为快照文件名，形成静态互跳闭环。
    覆盖带查询串变体（如 /ai/quiz?restart=1）；POST 表单 action 由 demote_forms 处理。

    2026-09-15：补齐 PH2 二期页面的参数化路由。此前 build_snapshot_set 只覆盖
    第一阶段 16 页的静态路由，`/admin/stores/`（顶部导航「管理」）、`/admin/industry/`、
    `/admin/diy/export/<pack>`、`/admin/multi/store/<id>` 等 23 个目标**没有被重写**，
    静态快照里点了就是 file:// 死链（实测 35 处）。这里按「同一功能页就近降级」补齐。
    """
    # 任务详情动态路由统一回到任务台账页
    html = re.sub(r'href="/admin/tasks/\d+"', 'href="22_责任任务.html"', html)
    html = re.sub(r'href="/admin/tasks/?"', 'href="22_责任任务.html"', html)
    # 考核明细带数字参数（/ai/admin/assessment/1、/2…）统一映射到 06 页
    html = re.sub(r'href="/ai/admin/assessment/\d+"', 'href="06_考核明细.html"', html)
    # 店员启停为写操作，静态快照不可执行 → 降级自跳回本页（不断链）
    html = re.sub(r'href="/admin/staff/\d+/toggle"', 'href="02_店员管理.html"', html)
    # 看板区间/LINK_MAP 之外的新增静态路由：时间区间与 CSV 导出在快照里均降级跳回看板页（不断链）
    html = re.sub(r'href="/admin/dashboard/export\??[^"]*"', 'href="01_管理员看板.html"', html)
    html = re.sub(r'href="/admin/dashboard(\?[^"]*)?"', 'href="01_管理员看板.html"', html)
    # ── 参数化 / 多值路由：静态快照无法逐参数页面化，就近降级到对应功能页 ──
    # 必须在下方 LINK_MAP 循环**之前**执行（这些路径不会命中带引号锚定的精确规则）
    html = re.sub(r'href="/admin/diy/(?:export|restore)/[^"]*"',
                  'href="17_建包工作台.html"', html)
    html = re.sub(r'href="/admin/industry/[^"]*"', 'href="17_建包工作台.html"', html)
    html = re.sub(r'href="/admin/multi/store/\d+[^"]*"',
                  'href="18_多门店经营大脑.html"', html)
    html = re.sub(r'href="/admin/stores/\d+[^"]*"', 'href="19_门店与成员.html"', html)
    # 顶部栏「导入」按钮（base.html → admin.data_import_view）：快照集里没有导入页，
    # 根绝对路径 /admin/import 在 Pages/CloudBase 上会解析到**站点根**（不在 /ican-demo/ 下）
    # ⇒ 13 页全 404（2026-09-25 三视口回归当场抓到，逐条探状态实测）。按同一约定就近降级回看板页。
    html = re.sub(r'href="/admin/import(\?[^"]*)?"', 'href="01_管理员看板.html"', html)
    for route in sorted(LINK_MAP, key=len, reverse=True):
        # 匹配 href="/route" 或 href="/route?..."；route="/" 时只匹配 href="/" 本身
        html = re.sub(r'href="' + re.escape(route) + r'(\?[^"]*)?"',
                      'href="' + LINK_MAP[route] + '"', html)
    return html


# ── POST 表单静态化降级 ──
# file:// 协议下 <form method="POST" action="/ai/quiz"> 的 action 被浏览器解析为
# 本地盘路径（file:///C:/ai/quiz）→ 点提交直接 ERR_FILE_NOT_FOUND。
# 降级方案：method 改 GET、action 重写为「提交后合理去向」的快照页，
# 演示时点提交 = 跳到结果页（表单字段作为查询串附加，静态页自动忽略）。
ACTION_MAP = {
    "/h5/submit": "08_填报历史.html",          # 提交填报 → 查看填报历史（模拟提交成功闭环）
    "/ai/quiz": "12_我的培训进度.html",         # 提交作答 → 查看培训进度（模拟考核留痕）
    "/ai/chat": "12_我的培训进度.html",         # 结束点评 → 查看培训进度
    "/ai/trainer": "16_AI培训助手.html",        # 提交提问 → 刷新培训助手页
    "/ai/admin/assistant": "15_AI助手.html",    # 提交提问 → 刷新 AI 助手页
    "/admin/staff/add": "02_店员管理.html",     # 创建店员成功 → 店员列表
    "/admin/products/add": "03_产品管理.html",  # 添加产品成功 → 产品列表
    "/admin/platform/demo-reset": "21_演示数据重建.html",  # 静态快照只读 → 本页
    # ── 2026-09-15 补齐：顶部常驻导航与二期页面的表单 ──
    "/admin/industry/activate": "01_管理员看板.html",   # 行业包切换 → 看板（换包后看板即变）
    "/admin/stores/switch": "19_门店与成员.html",        # 门店切换 → 门店与成员页
    "/admin/stores/add": "19_门店与成员.html",
    "/logout": "index.html",                             # 退出 → 回快照总览
    "/register": "20_商家注册.html",
    "/admin/tasks/create": "22_责任任务.html",
    "/admin/multi/analyze": "18_多门店经营大脑.html",
    "/admin/diy/copy": "17_建包工作台.html",
    "/admin/diy/draft": "17_建包工作台.html",
    "/admin/diy/import": "17_建包工作台.html",
}

# 参数化表单 action（带 id / 动作后缀）→ 「就近功能页」。按顺序先匹配先替换。
FORM_ACTION_PATTERNS = [
    (r'/admin/stores/\d+/(?:pack|toggle|members/add)', "19_门店与成员.html"),
    (r'/admin/diy/restore/[^"]*', "17_建包工作台.html"),
    (r'/admin/industry/[^"]*', "17_建包工作台.html"),
]


def demote_forms(html):
    """把快照页内 POST 表单降级为静态可跳转的 GET 表单（不断链、不 404）。

    2026-09-15 修复：原实现只匹配大写 `method="POST"`，而 base.html / 二期模板
    统一写小写 `method="post"`，导致**顶部常驻导航与二期页面的表单从未被降级**
    （实测 93 处 action 在 file:// 下指向死链）。现改为大小写不敏感，
    并在最后加一条兜底：任何仍未映射的服务端 action 一律指向快照总览页。
    """
    # M = 任意大小写的 method="post"（模板里大小写混用，必须一并覆盖）
    m = r'method="[Pp][Oo][Ss][Tt]"'
    html = re.sub(r'<form ' + m + r' action="/admin/tasks/\d+/(?:status|review)"',
                  '<form method="GET" action="22_责任任务.html"', html)
    html = re.sub(r'<form ' + m + r' action="/h5/tasks/\d+/status"',
                  '<form method="GET" action="23_我的任务.html"', html)
    # 店员启停/重置密码带数字 id：一律跳回店员管理页
    html = re.sub(r'<form ' + m + r' action="/admin/staff/\d+/(?:toggle|reset-password)"',
                  '<form method="GET" action="02_店员管理.html"', html)
    for pattern, target in FORM_ACTION_PATTERNS:
        html = re.sub(r'<form ' + m + r' action="' + pattern + r'"',
                      '<form method="GET" action="' + target + '"', html)
    for route in sorted(ACTION_MAP, key=len, reverse=True):
        html = re.sub(r'<form ' + m + r' action="' + re.escape(route) + r'"',
                      '<form method="GET" action="' + ACTION_MAP[route] + '"', html)
    # 兜底：仍指向服务端绝对路径的表单（含 formaction、多行 <form> 标签与后续新增路由）
    # → 一律改成 GET + 快照总览，保证「静态快照里没有一条会打到 file:///C:/… 的死链」
    # 这一硬约束长期成立（不依赖每次新增路由都记得补 ACTION_MAP）。
    html = re.sub(r'<form\b[^>]*>', _demote_form_tag, html)
    html = re.sub(r'formaction="/[^"]*"', 'formaction="index.html"', html)
    return html


def _demote_form_tag(match):
    """把单个 <form> 开标签降级为静态可跳转形式（无服务端 action 时原样返回）。

    Args:
        match: `re.sub(r'<form\\b[^>]*>', ...)` 传入的匹配对象。

    Returns:
        str: 改写后的 <form> 开标签。
    """
    tag = match.group(0)
    action = re.search(r'action="(/[^"]*)"', tag)
    if not action:
        return tag
    if re.search(r'method="[A-Za-z]+"', tag):
        tag = re.sub(r'method="[A-Za-z]+"', 'method="GET"', tag, count=1)
    else:
        tag = '<form method="GET"' + tag[len('<form'):]
    return tag.replace(action.group(0), 'action="index.html"')


# ── V5：AI 问答预生成（真实 Dify/DeepSeek）──
# 配置了 Dify/DeepSeek Key（dify_client.enabled=True）时，构建快照前用真实路由
# 连续提交 3-5 问，让真实 AI 生成问答并渲染进聊天页快照（演示更有说服力）；
# 未配置 Key 或 AI 调用失败时自动回退规则版整页抓取，快照构建永不中断（CI 稳定）。
# 提问刻意避开「考核/成绩」等走路由层直答的关键词，保证全部落到真实 AI 问答链路。
AI_QA_PAGES = {
    "15_AI助手.html": {
        "post_url": "/ai/admin/assistant",
        "marker": 'id="assistantLog"',
        "questions": [
            "本周门店营收和毛利情况怎么样？",
            "明天备货建议是什么？",
            "近期有损耗异常吗，怎么改进？",
            "杨枝甘露卖得怎么样？",
        ],
    },
    "16_AI培训助手.html": {
        "post_url": "/ai/trainer",
        "marker": 'id="trainerLog"',
        "questions": [
            "顾客嫌贵砍价，我该怎么应对？",
            "珍珠煮好后能保存多久？",
            "我最近哪块比较弱，先复习什么？",
            "杨枝甘露怎么保存？",
        ],
    },
}


def capture_ai_chat(url, post_url, questions, marker):
    """预生成真实 AI 问答后抓取聊天页快照。

    通过真实路由连续 POST：中间轮带 fetch 标记拿 JSON（问答同时写入 session
    历史并校验 AI 可用性），最后一轮整页提交，返回含全部问答气泡的页面 HTML。
    任一环节 AI 不可用/异常 → 回退规则版整页抓取（快照不产出失败文案）。
    """
    if not dify_client.enabled:
        return capture(url)
    for q in questions[:-1]:
        try:
            j = client.post(post_url, data={"ask": q},
                            headers={"X-Requested-With": "fetch"},
                            follow_redirects=False).get_json()
        except Exception:
            j = None
        if not (isinstance(j, dict) and j.get("ok")):
            print("AI 问答不可用，回退规则版整页抓取:", post_url)
            return capture(url)
    r = client.post(post_url, data={"ask": questions[-1]}, follow_redirects=False)
    html = r.get_data(as_text=True)
    if marker not in html:  # 意外 302/429/错误页 → 回退规则版
        print("AI 问答末轮响应异常，回退规则版整页抓取:", post_url)
        return capture(url)
    return _transform(html)


def capture(url):
    r = client.get(url, follow_redirects=True)
    return _transform(r.get_data(as_text=True))


def _transform(html):
    """对抓取到的页面 HTML 做快照化变换（内联 CSS/静态模式脚本/链接重写等）。

    GET 抓取与 AI 问答预生成（capture_ai_chat）共用，保证两种来源的
    页面 HTML 输出格式完全一致。
    """
    # 内联 CSS（性能 P0-3 分层后为 critical + style 两层；asset_url 版本化后
    # URL 带 ?v= 版本戳，改按前缀正则匹配而非整串比对）
    html = re.sub(r'<link rel="stylesheet" href="/static/css/critical\.css[^"]*">',
                  "<style>\n" + css_critical + "\n</style>", html, count=1)
    html = re.sub(r'<link rel="preload" as="style" href="/static/css/style\.css[^"]*"[^>]*>',
                  "<style>\n" + css + "\n</style>", html, count=1)
    # 追加快照翻页导航样式（首个 </style> 即内联样式块尾部）
    html = html.replace("</style>", SNAP_NAV_CSS + "</style>", 1)
    # 静态演示模式脚本注入（</head> 前，先于页内脚本执行）
    html = html.replace("</head>", SNAPSHOT_MODE_JS + "</head>", 1)
    # 按需内联 echarts（base.html 按需加载器改造后页面不再外链引擎，含图表页
    # 才有 withEcharts( 调用；在 </head> 前注入引擎并完成 {default: api} 解包，
    # 使 withEcharts() 同步命中「引擎已就绪」分支，快照页不发网络请求）
    if "withEcharts(" in html:
        engine_block = ("<script>\n" + echarts
                        + "\nif (window.echarts && !window.echarts.init"
                          " && window.echarts.default) { window.echarts = window.echarts.default; }\n</script>")
        html = html.replace("</head>", engine_block + "</head>", 1)
    # 剥离服务端 main.js（主题切换已内联 base.html，不受影响）
    html = html.replace('<script src="/static/js/main.js"></script>',
                        "<!-- main.js 为服务端路由资源，静态快照已移除 -->")
    # 兜底：清掉任何残留 /static 引用，避免断链
    html = re.sub(r'<link[^>]*href="/static/[^"]*"[^>]*>', "", html)
    html = re.sub(r'<script[^>]*src="/static/[^"]*"[^>]*></script>', "", html)
    # 站内链接闭环重写
    html = rewrite_links(html)
    # POST 表单静态化降级（file:// 下防提交死链）
    html = demote_forms(html)
    return html


def nav_block(prev, nxt):
    """生成快照翻页导航条（首/尾页对缺失方向禁用）"""
    prev_a = f'<a href="{prev}">← 上一页</a>' if prev else '<a class="disabled" aria-disabled="true">← 上一页</a>'
    nxt_a = f'<a href="{nxt}">下一页 →</a>' if nxt else '<a class="disabled" aria-disabled="true">下一页 →</a>'
    # 快照集内的 index.html 已经是唯一总览入口，不再生成历史外部回链。
    back = ''
    return f'<nav class="snap-nav" aria-label="页面导航">{prev_a}{back}<a href="index.html">返回总览</a>{nxt_a}</nav>'


PAGES = [
    # (role, url, filename, title, desc)
    ("admin", "/admin/",              "01_管理员看板.html",   "管理员看板",     "数据总览：KPI + 日营收销量折线 + 产品占比饼图 + 热销排行 + 损耗异常提醒"),
    ("admin", "/admin/staff",         "02_店员管理.html",     "店员管理",       "店员列表（小王/小李，状态启用），可新增/停用"),
    ("admin", "/admin/staff/add",     "13_新增店员.html",     "新增店员",       "店员账号创建表单（AC-OBS-01 证据页：管理员建号→店员登录）"),
    ("admin", "/admin/products",      "03_产品管理.html",     "产品管理",       "5 个在售产品及单价，可增删"),
    ("admin", "/admin/quiz",          "04_考核查看.html",     "考核查看",       "店员填报/考核记录入口"),
    ("admin", "/ai/admin/assessment", "05_考核结果统计.html", "AI考核结果统计", "每人错题数 + 平均得分 + 薄弱知识点分布"),
    ("admin", "/ai/admin/assessment/1", "06_考核明细.html",   "AI考核明细",     "店员(小王)逐条考核记录与对错"),
    ("admin", "/ai/admin/business-analysis", "14_经营分析.html", "AI经营分析", " 营收/趋势/结构/损耗 综合报告（AI 生成，规则版演示）"),
    ("admin", "/ai/admin/assistant", "15_AI助手.html", "AI智能助手", " 管理员业务问答（营收/销量/毛利/备货/知识库，多轮记忆）"),
    ("staff", "/h5/",                 "07_店员看板.html",     "店员看板(H5)",   "店员端数据总览，微信内打开"),
    ("staff", "/h5/history",          "08_填报历史.html",     "填报历史(H5)",   "本人历史填报记录，按日可追溯"),
    ("staff", "/ai/",                 "09_AI培训主页.html",   "AI培训主页",     "问答考核 / 情景对话 / 上岗考核 三入口"),
    ("staff", "/ai/quiz",             "10_AI问答考核.html",   "AI问答考核",     "AI出题→店员作答→AI点评（第1题示例）"),
    ("staff", "/ai/chat",             "11_情景对话.html",     "AI情景对话",     "AI扮演顾客练习（砍价/卫生/服务场景）"),
    ("staff", "/h5/training",         "12_我的培训进度.html", "我的培训进度(H5)", "店员个人通过率+分类统计(问答/情景)+薄弱方向+错题集（AC-OBS-10/13 店员视角闭环）"),
    ("staff", "/ai/trainer",          "16_AI培训助手.html",   "AI培训助手",     "店员业务问答 + 个人错题针对性学习提示（聊天式）"),
    # ── 二期页面（PH2-8A 补录：此前的快照集只覆盖第一阶段 16 页）──
    ("admin", "/admin/diy/",          "17_建包工作台.html",   "全行业建包工作台", "六步向导建包 → AI/模板草稿 → 预览校验 → 人工确认落库；含导入导出/复制/恢复内置"),
    ("admin", "/admin/multi/",        "18_多门店经营大脑.html", "多门店经营大脑", "多店汇总/门店排行/跨店趋势/异常门店提示 + 跨店 AI 分析（证据引用 + 决策轨迹 + 来源标注）"),
    ("admin", "/admin/stores/",       "19_门店与成员.html",   "门店与成员",     "老板建门店、邀请店长/店员、设置本店行业包（一店一当前包）"),
    ("admin", "/register",            "20_商家注册.html",     "商家注册",       "邮箱验证码自助注册入口（未配置邮服时显示关闭提示与人工开户指引）"),
    ("platform", "/admin/platform/demo-reset", "21_演示数据重建.html", "演示数据重建",
     "平台管理员备份后全库重建七门店 30 天演示租户（双重确认 + 自动备份 + 账号清单）"),
    ("admin", "/admin/tasks/", "22_责任任务.html", "责任任务",
     "多店异常转责任任务：负责人、截止时间、状态、逾期与效果回看"),
    ("admin", "/admin/changes", "25_变更归因.html", "变更归因",
     "谁在什么时候改了哪一行的哪个字段：按实体/门店筛选的变更流水（只出字段名与前后值，不给平台业务明细）"),
    ("staff", "/h5/tasks", "23_我的任务.html", "我的任务",
     "店员微信 H5 查看本人任务并更新开始/完成状态（无需安装 App）"),
    # ── PH5 只读演示入口（免登录；评委/客户打开链接即见，无需任何账号）──
    ("public", "/demo", "24_只读演示入口.html", "只读演示入口",
     "免登录只读 AI 决策驾驶舱：四角色结论 / 证据目录 / 分歧与仲裁；不调用大模型、不写数据库、不创建任务"),
]

LINK_MAP.update({url: fname for _role, url, fname, _t, _d in PAGES})
LINK_MAP.update(_NON_PAGE_LINKS)
_unmapped = [f for _r, _u, f, _t, _d in PAGES if LINK_MAP.get(_u) != f]
assert not _unmapped, "页面表里有路由没进重写映射（快照必断链）：" + str(_unmapped)
print("LINK MAP: ", len(LINK_MAP),
      "条路由→页面映射（派生自 PAGES + 4 条非页面路由）")

os.makedirs(OUTDIR, exist_ok=True)
written = []
order = [fname for _, _, fname, _, _ in PAGES]
for i, (role, url, fname, title, desc) in enumerate(PAGES):
    login(*DEMO_LOGIN[role])
    # V5：AI 聊天页先经真实路由预生成 3-5 条真实问答再抓取（无 Key 自动回退规则版）
    qa = AI_QA_PAGES.get(fname)
    if qa:
        html = capture_ai_chat(url, qa["post_url"], qa["questions"], qa["marker"])
    else:
        html = capture(url)
    # 注入翻页导航（按 index 卡片顺序 prev/next，闭环可回总览）
    html = html.replace("</body>", nav_block(
        order[i - 1] if i > 0 else None,
        order[i + 1] if i < len(order) - 1 else None) + "</body>")
    path = os.path.join(OUTDIR, fname)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    written.append((fname, title, desc, role, os.path.getsize(path)))
    print("captured:", fname, os.path.getsize(path), "bytes")

# ── 生成导航首页（自包含，接暗色设计体系 + 浮动主题切换）──
cards = "\n".join(
    f'      <a class="card" href="{fname}">\n'
    f'        <div class="role role-{role}">{ {"admin": "管理员", "staff": "店员H5", "platform": "平台管理员", "public": "公开入口"}[role] }</div>\n'
    f'        <div class="t">{title}</div>\n'
    f'        <div class="d">{desc}</div>\n'
    f'      </a>' for fname, title, desc, role, _ in written
)
index_html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>iCAN 演示快照集</title>
<script>
// 主题初始化（渲染前执行，防闪烁）：手动选择优先，缺省默认「墨绿黑暗主题」（雾山青暗室）
(function () {{
    var t = null;
    try {{ t = localStorage.getItem('ican-theme'); }} catch (e) {{}}
    if (t === 'dark' || t === 'light') {{ document.documentElement.dataset.theme = t; }}
    else {{ document.documentElement.dataset.theme = 'dark'; }}
}})();
</script>
<style>
  :root {{
    --bg:#F5F7F6; --card:#FFFFFF; --text:#16211F; --text-light:#5B6B67; --border:#E3E8E6;
    --primary:#0F766E; --soft:rgba(15,118,110,.09); --shadow-sm:0 1px 2px rgba(16,33,31,.05);
    --shadow-lg:0 12px 34px -8px rgba(16,33,31,.18);
    color-scheme: light;
  }}
  html[data-theme="dark"] {{
    --bg:#0A1513; --card:#12211F; --text:#E4EBE9; --text-light:#8CA09B; --border:#1E3A34;
    --primary:#2DD4BF; --soft:rgba(45,212,191,.12); --shadow-sm:0 1px 2px rgba(0,0,0,.5);
    --shadow-lg:0 14px 34px -8px rgba(0,0,0,.62);
    color-scheme: dark;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family:"DIN Alternate","Bahnschrift","PingFang SC","HarmonyOS Sans SC","MiSans","Segoe UI","Microsoft YaHei",sans-serif;
    background:var(--bg); color:var(--text); margin:0; padding:32px 24px 56px;
    -webkit-font-smoothing:antialiased; line-height:1.65;
    transition: background-color .2s ease, color .2s ease;
  }}
  .wrap {{ max-width:960px; margin:0 auto; }}
  .head {{ display:flex; align-items:center; gap:10px; margin:0 0 6px; }}
  .mark {{
    display:inline-flex; align-items:center; justify-content:center;
    width:32px; height:32px; border-radius:9px; flex:none;
    background:linear-gradient(120deg,#0A4841,#0F766E 55%,#14B8A6); color:#fff;
  }}
  h1 {{ font-size:22px; letter-spacing:-.01em; margin:0; }}
  .sub {{ color:var(--text-light); font-size:13px; margin:0 0 14px 42px; }}
  .guide {{
    margin:0 0 22px 42px; padding:10px 14px; font-size:13px; line-height:1.6;
    color:var(--primary); background:var(--soft); border:1px solid var(--primary);
    border-radius:12px;
  }}
  .guide b {{ font-weight:650; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(248px,1fr)); gap:14px; }}
  .card {{
    background:var(--card); border:1px solid var(--border); border-radius:16px;
    padding:16px; text-decoration:none; color:inherit;
    transition:transform .12s cubic-bezier(.22,.61,.36,1), box-shadow .12s ease, border-color .12s ease;
    display:block;
  }}
  .card:hover {{ transform:translateY(-2px); box-shadow:var(--shadow-lg); border-color:var(--primary); }}
  .card:active {{ transform:scale(.98); }}
  .card:focus-visible {{ outline:2px solid var(--primary); outline-offset:2px; }}
  .role {{ display:inline-block; font-size:11px; font-weight:600; padding:2px 9px; border-radius:999px; margin-bottom:9px; }}
  .role-admin {{ background:var(--soft); color:var(--primary); border:1px solid var(--primary); }}
  .role-platform {{ background:var(--soft); color:var(--primary); border:1px solid var(--primary); }}
  .role-staff {{ background:var(--soft); color:var(--primary); border:1px dashed var(--primary); }}
  .role-public {{ background:var(--soft); color:var(--primary); border:1px solid var(--primary); }}
  .t {{ font-size:15px; font-weight:650; margin-bottom:6px; }}
  .d {{ font-size:12px; color:var(--text-light); line-height:1.55; }}
  .fab {{
    position:fixed; right:16px; bottom:16px; z-index:200;
    width:40px; height:40px; border-radius:50%; font-size:18px; cursor:pointer;
    display:inline-flex; align-items:center; justify-content:center;
    background:var(--card); color:var(--text); border:1px solid var(--border);
    box-shadow:var(--shadow-lg); transition:all .2s;
  }}
  .fab:hover {{ transform:rotate(15deg) scale(1.08); }}
  /* 主题按钮线性图标：默认墨绿黑暗显示太阳（点击切亮），亮色显示月亮（点击切暗） */
  .fab .icon-sun {{ display:block; }}
  .fab .icon-moon {{ display:none; }}
  html[data-theme="light"] .fab .icon-sun {{ display:none; }}
  html[data-theme="light"] .fab .icon-moon {{ display:block; }}
</style></head>
<body>
  <div class="wrap">
    <div class="head">
      <span class="mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="20" height="20"><line x1="4" y1="20" x2="20" y2="20"/><line x1="7" y1="16" x2="7" y2="11"/><line x1="12" y1="16" x2="12" y2="6"/><line x1="17" y1="16" x2="17" y2="9"/></svg></span>
      <h1>iCAN 演示快照集</h1>
    </div>
    <div class="sub">全行业多门店轻量化 AI 管理智能体 · 共 {len(written)} 页 · 页内按钮可互跳 · 右下角按钮可切暗色（离线双击即用）</div>
    <div class="guide" role="status"><b>推荐演示路径</b>：⓪ 只读演示入口（免登录，打开即见四角色驾驶舱）→ ① 管理员看板（KPI + 三图 + 四角色驾驶舱）→ ② AI 经营分析 / AI 助手 → ③ 责任任务与效果回看 → ④ 店员 H5 填报 → ⑤ AI 培训考核 → ⑥ 我的培训进度（数据→洞察→执行→成长闭环）</div>
    <div class="grid">
{cards}
    </div>
  </div>
  <button type="button" class="fab" id="themeToggle" title="切换深色/浅色" aria-label="切换深色/浅色">
    <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="20" height="20" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>
    <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="20" height="20" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
  </button>
  <script>
  (function () {{
      var btn = document.getElementById('themeToggle');
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      function isDark() {{
          var t = document.documentElement.dataset.theme;
          return t ? t === 'dark' : true; // 缺省默认墨绿黑暗主题
      }}
      function syncIcon() {{
          // 图标语义 = 点击后切换到的目标主题：暗色显示太阳（切亮），亮色显示月亮（切暗）
          var dark = isDark();
          var sun = btn.querySelector('.icon-sun');
          var moon = btn.querySelector('.icon-moon');
          if (sun) sun.style.display = dark ? 'block' : 'none';
          if (moon) moon.style.display = dark ? 'none' : 'block';
      }}
      syncIcon();
      btn.addEventListener('click', function () {{
          var next = isDark() ? 'light' : 'dark';
          try {{ localStorage.setItem('ican-theme', next); }} catch (e) {{}}
          document.documentElement.dataset.theme = next;
          syncIcon();
      }});
      if (mq.addEventListener) mq.addEventListener('change', syncIcon);
  }})();
  </script>
</body></html>"""
with io.open(os.path.join(OUTDIR, "index.html"), "w", encoding="utf-8", newline="\n") as f:
    f.write(index_html)
print("index.html written")
print("DONE:", len(written), "pages + index")

# 重建完成后立刻镜像对外交付副本（失败即非零退出，避免「快照建好了但提交包里还是旧的」）
if MIRROR_DST:
    _code, _lines = mirror_snapshot_copy(MIRROR_SRC, MIRROR_DST)
    for _l in _lines:
        print(_l)
    if _code:
        sys.exit(_code)
else:
    print('MIRROR SKIP：未找到交付副本目录（01-演示快照集）')
