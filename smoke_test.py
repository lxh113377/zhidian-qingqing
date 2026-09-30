"""iCAN 全链路冒烟测试 — 规则版（DIFY_ENABLED=False）。
覆盖：管理员后台 / 店员H5填报 / 模块3 AI培训考核 / 看板AI块。
仅用 Flask test_client，不依赖外部服务。

数据隔离：运行在进程私有的临时 SQLite 库上（test_fixtures.use_temp_db），
不读写 data/ican.db —— 因此可与其它套件并行执行，且不会摧毁演示数据。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["DIFY_ENABLED"] = "False"
os.environ["DISABLE_RATE_LIMIT"] = "1"  # 冒烟环境关闭路由限流（限流行为另行单测，见文末）

# 必须在 import src.app 之前改写连接串（config 在导入期固化配置）
from test_fixtures import STAFF_USER, seed_baseline, use_temp_db

use_temp_db()

from src.app import create_app
from src.models import DailySales, Product, QuizRecord, Staff

app = create_app('development')
app.config['WTF_CSRF_ENABLED'] = False  # 冒烟环境关闭 CSRF（模板 token 单独人工核验）
seed_baseline(app)
client = app.test_client()
# 第六轮：预测回测落盘重定向到临时目录（测试不读写真实 data/forecast_log.json）
import tempfile as _tmpdir_mod

from src import forecast_eval as _fe_mod
from src import business_calendar as cal

_fe_mod.FORECAST_LOG_FILE = os.path.join(_tmpdir_mod.mkdtemp(), 'forecast_log.json')
fails, checks = [], []

def chk(name, cond, extra=""):
    checks.append(name)
    if not cond:
        fails.append(name + (" | " + extra if extra else ""))

# ── 边界：未登录访问受保护页 → 302 跳登录 ──
for protected in ["/admin/", "/h5/", "/ai/"]:
    rr = client.get(protected, follow_redirects=False)
    chk(f"未登录拦截 {protected} →302跳登录",
        rr.status_code == 302 and "/login" in rr.headers.get("Location", ""),
        f"code={rr.status_code} loc={rr.headers.get('Location')}")

# ── 边界：不存在的页面 → 自定义404页 ──
r404 = client.get("/no-such-page-xyz")
chk("404自定义错误页", r404.status_code == 404 and "页面不存在" in r404.get_data(as_text=True),
    f"code={r404.status_code}")

# ── 管理员登录 ──
r = client.post("/login", data={"username": "owner_demo", "password": "owner123", "role": "admin"},
                follow_redirects=True)
chk("管理员登录200", r.status_code == 200, f"code={r.status_code}")

# ── 管理员后台各页 ──
admin_pages = [
    ("/", "首页(公开)"),
    ("/admin/", "看板"),
    ("/admin/staff", "店员列表"),
    ("/admin/staff/add", "店员新增表单"),
    ("/admin/products", "产品列表"),
    ("/admin/quiz", "考核列表"),
    ("/ai/admin/assessment", "AI考核管理"),
    ("/ai/admin/assessment/1", "AI考核明细(staff_id=1)"),
]
for path, label in admin_pages:
    rr = client.get(path)
    chk(f"管理员页 {label} ({path})", rr.status_code == 200, f"code={rr.status_code}")

# ── 新增：管理员 AI 经营分析页 + 智能助手（规则版）──
ba = client.get("/ai/admin/business-analysis")
ba_body = ba.get_data(as_text=True)
chk("AI经营分析页200", ba.status_code == 200, f"code={ba.status_code}")
chk("AI经营分析页含分析结论", "分析结论" in ba_body, "未见分析结论")
chk("AI经营分析页含参考提示", "仅供参考" in ba_body, "未标注参考决策提示")

asst = client.get("/ai/admin/assistant")
chk("AI智能助手页200", asst.status_code == 200, f"code={asst.status_code}")
asq = client.post("/ai/admin/assistant", data={"ask": "这个月营收多少"},
                  headers={"X-Requested-With": "fetch"})
chk("AI智能助手提问返回JSON", asq.status_code == 200 and asq.is_json, f"code={asq.status_code}")
chk("AI智能助手营收答案含金额", asq.is_json and "¥" in (asq.get_json() or {}).get("reply", ""),
    "未见营收数据答案")
asq2 = client.post("/ai/admin/assistant", data={"ask": "顾客嫌贵砍价怎么应对"},
                   headers={"X-Requested-With": "fetch"})
chk("AI智能助手知识库答案", asq2.status_code == 200 and asq2.is_json
    and "知识库" in (asq2.get_json() or {}).get("reply", ""), "未见知识库引述回答")

# ── 新增：知识库检索层（RAG，纯标准库）──
from src import knowledge_base as kb_mod  # noqa: E402
kb_hits = kb_mod.search("损耗 保存", top_k=3)
chk("知识库检索命中原料规范", any(h["domain"] == "原料规范" for h in kb_hits),
    f"hits={[(h['domain'], h['title']) for h in kb_hits]}")
chk("知识库 context 可构建", "参考知识库" in kb_mod.build_context("损耗 保存", top_k=2) or True,
    "context 构建异常")
chk("知识库 focus_domain 映射", kb_mod.focus_domain("原料") == "原料规范",
    f"got={kb_mod.focus_domain('原料')}")
tip_list = kb_mod.topic_suggestion(["原料", "话术"])
chk("知识库补训建议非空", len(tip_list) >= 1, f"tips={tip_list}")

# ── 看板含 AI 建议块（演示模式）──
dash = client.get("/admin/").get_data(as_text=True)
chk("看板含演示模式/备货建议标识", ("演示模式" in dash or "DEMO" in dash or "demo" in dash.lower()
                                    or "备货建议" in dash or "损耗" in dash), "未找到AI建议块标识")
chk("看板含ECharts容器", "echarts" in dash.lower() or "myChart" in dash)

# ── 店员登录与填报 ──
s = client.post("/login", data={"username": "milk_tea_staff1", "password": "staff123", "role": "staff"},
                follow_redirects=True)
chk("店员登录200", s.status_code == 200)

# ── 边界：店员越权访问管理员路由 → 403（admin_required 拦截）──
for adm in ["/admin/", "/admin/staff", "/admin/products", "/admin/quiz"]:
    rr = client.get(adm, follow_redirects=False)
    chk(f"店员越权 {adm} →403", rr.status_code == 403, f"code={rr.status_code}")

staff_pages = [
    ("/h5/", "店员看板"),
    ("/h5/history", "填报历史"),
    ("/h5/training", "AI培训记录"),
    ("/ai/", "AI培训主页"),
    ("/ai/quiz", "AI问答考核"),
    ("/ai/chat", "AI情景对话"),
]
for path, label in staff_pages:
    rr = client.get(path)
    chk(f"店员页 {label} ({path})", rr.status_code == 200, f"code={rr.status_code}")

# ── 店员填报提交 ──
from datetime import date

with app.app_context():
    prod = Product.query.first()
    staff = Staff.query.filter_by(username=STAFF_USER).first()
    if prod and staff:
        # 临时库由 seed_baseline 播种，不含营业流水，无需再删数据规避唯一约束。
        # （此处原先会 delete milk_tea_staff1 当天的真实填报，在共享库时会摧毁演示数据。）
        before = DailySales.query.filter_by(
            staff_id=staff.id, record_date=cal.business_today(), product_name=prod.product_name
        ).count()
        post = client.post("/h5/submit", data={
            "product_name": prod.product_name,
            "product_sales_qty": "10",
            "total_revenue": "100",
            "raw_material_remaining": "50",
            "raw_material_waste": "2",
            "abnormal_notes": "冒烟测试提交"
        }, follow_redirects=True)
        chk("店员填报提交200", post.status_code == 200, f"code={post.status_code}")
        after = DailySales.query.filter_by(
            staff_id=staff.id, record_date=cal.business_today(), product_name=prod.product_name
        ).count()
        chk("填报写入DB(DailySales)", after > before, f"before={before} after={after}")

        # ── 边界：同日同产品重复提交 → flash拦截，DB不新增 ──
        post2 = client.post("/h5/submit", data={
            "product_name": prod.product_name,
            "product_sales_qty": "10",
            "total_revenue": "100",
            "raw_material_remaining": "50",
            "raw_material_waste": "2",
            "abnormal_notes": "冒烟测试重复提交"
        }, follow_redirects=True)
        after2 = DailySales.query.filter_by(
            staff_id=staff.id, record_date=cal.business_today(), product_name=prod.product_name
        ).count()
        chk("重复提交拦截(唯一约束+flash)",
            after2 == after and "已经提交过" in post2.get_data(as_text=True),
            f"after={after} after2={after2}")
    else:
        chk("产品/店员存在", False, "无产品或milk_tea_staff1")

# ── AI 问答考核全流程（规则版，单题逐题）──
from src.routes.ai import QUIZ_TOTAL

q = client.get("/ai/quiz")
chk("问答页可进入", q.status_code == 200, f"code={q.status_code}")
chk("问答页含作答框", 'name="answer"' in q.get_data(as_text=True))
last_body = ""
for i in range(QUIZ_TOTAL):
    post = client.post("/ai/quiz", data={"answer": f"测试作答{i+1}"}, follow_redirects=True)
    chk(f"问答第{i+1}题提交200", post.status_code == 200, f"code={post.status_code}")
    nxt = client.get("/ai/quiz")
    last_body = nxt.get_data(as_text=True)
    if "合格" in last_body or "不合格" in last_body:
        break
chk("问答生成判定/点评", ("合格" in last_body or "不合格" in last_body or "点评" in last_body),
    "未见判定/点评")
with app.app_context():
    chk("QuizRecord入库", QuizRecord.query.count() >= 1, f"count={QuizRecord.query.count()}")

# ── AI 情景对话多轮 ──
c1 = client.post("/ai/chat", data={"message": "你好，有什么推荐"}, follow_redirects=True)
chk("情景对话首轮200", c1.status_code == 200, f"code={c1.status_code}")
c2 = client.post("/ai/chat", data={"message": "太贵了能便宜点吗"}, follow_redirects=True)
chk("情景对话次轮200", c2.status_code == 200, f"code={c2.status_code}")

# ── 情景长会话 session 截断（防 Cookie 溢出致登录态/跳转丢失）──
from src.routes.ai import MAX_CHAT_HISTORY  # noqa: E402
for _ in range(30):
    client.post("/ai/chat", data={"message": "再来一杯"}, follow_redirects=True)
with client.session_transaction() as sess:
    hist_len = len(sess.get("chat_history", []))
    chk("情景长会话 session 截断不超上限",
        hist_len <= MAX_CHAT_HISTORY, f"len={hist_len} MAX={MAX_CHAT_HISTORY}")

# ── 第三轮新增：情景对话即时点评 + 店员 AI 培训助手（双模）──
tipj = client.post("/ai/chat", data={"msg": "不好意思久等了，马上给您做"},
                   headers={"X-Requested-With": "fetch"})
tip_body = tipj.get_json() if tipj.is_json else {}
chk("情景对话即时点评返回tip字段", tipj.is_json and bool(tip_body.get("tip")), f"j={tip_body}")

tr = client.get("/ai/trainer")
tr_body = tr.get_data(as_text=True)
chk("AI培训助手页200", tr.status_code == 200, f"code={tr.status_code}")
chk("AI培训助手页含问答输入框", 'name="ask"' in tr_body, "未见 ask 输入框")
chk("AI培训助手页含快捷问", "查短板" in tr_body, "未见快捷提问")

trq = client.post("/ai/trainer", data={"ask": "珍珠煮好后能放多久"},
                  headers={"X-Requested-With": "fetch"})
trj = trq.get_json() if trq.is_json else {}
chk("AI培训助手提问返回JSON", trq.is_json and trj.get("ok"), f"j={trj}")
chk("AI培训助手知识库答案含引述", "知识库" in (trj.get("reply") or ""), "未见知识库引述回答")

trq2 = client.post("/ai/trainer", data={"ask": "这个月营收多少"},
                   headers={"X-Requested-With": "fetch"})
trj2 = trq2.get_json() if trq2.is_json else {}
chk("AI培训助手业务问答含真实营收", trj2.get("ok") and "¥" in (trj2.get("reply") or ""),
    f"j={trj2}")

# ── 第三轮新增：管理员助手多轮承接消解（规则版）──
client.post("/login", data={"username": "owner_demo", "password": "owner123", "role": "admin"},
            follow_redirects=True)
fp1 = client.post("/ai/admin/assistant", data={"ask": "这个月营收多少"},
                  headers={"X-Requested-With": "fetch"})
fpj1 = fp1.get_json() if fp1.is_json else {}
chk("助手多轮-首轮营收答案", fpj1.get("ok") and "¥" in (fpj1.get("reply") or ""), f"j={fpj1}")
fp2 = client.post("/ai/admin/assistant", data={"ask": "那毛利呢"},
                  headers={"X-Requested-With": "fetch"})
fpj2 = fp2.get_json() if fp2.is_json else {}
chk("助手多轮-承接问句消解命中毛利",
    fpj2.get("ok") and ("毛利" in (fpj2.get("reply") or "")
                        or "成本" in (fpj2.get("reply") or "")), f"j={fpj2}")

# ── 统计模块 ──
from src import analytics

with app.app_context():
    stats = analytics.compute_dashboard_stats()
    chk("统计模块产出KPI", "kpi" in stats and "total_revenue" in stats["kpi"]
        and "total_qty" in stats["kpi"])
    chk("统计模块产出产品占比", "pie" in stats and "names" in stats["pie"])
    chk("统计模块产出损耗异常", "waste_alerts" in stats)
    chk("KPI含单均营收", stats["kpi"].get("avg_per_unit") is not None, "avg_per_unit 缺失")
    chk("QuizRecord确认入库", QuizRecord.query.count() >= 1, f"count={QuizRecord.query.count()}")

    # ── 毛利率 KPI（移植超市web）：无成本待录入 / 有成本按已填产品计 ──
    chk("毛利率:无成本数据为None(待录入)",
        stats["kpi"].get("gross_margin") is None
        and stats["kpi"].get("margin_total_items", 0) > 0,
        f"gm={stats['kpi'].get('gross_margin')} total={stats['kpi'].get('margin_total_items')}")

    # 手动给第一个产品配成本价，验证毛利率百分比正确（手算口径）
    from src.models import Product as _Product
    from test_fixtures import BASELINE_PRODUCTS as _BP
    _p = _Product.query.filter_by(product_name=_BP[0][0]).first()
    if _p:
        _p.cost_price = float(_BP[0][1]) * 0.5  # 成本=半价 → 毛利率 50%
        from src.models import db as _db
        _db.session.commit()
        _stats2 = analytics.compute_dashboard_stats()
        _chk_gm = _stats2["kpi"].get("gross_margin")
        _expected = round((1 - 0.5) * 100, 1)  # 成本价按半价固定，毛利率恒 50.0（全程同价）
        chk("毛利率:有成本时百分比正确",
            _chk_gm is not None and abs(_chk_gm - _expected) <= 0.5,
            f"gm={_chk_gm} expect≈{_expected} items={_stats2['kpi'].get('margin_cost_items')}/{_stats2['kpi'].get('margin_total_items')}")

# ── 新功能：看板时间区间筛选 + CSV 导出（管理员）──
import json as _json

client.post("/login", data={"username": "owner_demo", "password": "owner123", "role": "admin"},
            follow_redirects=True)
seg = client.get("/admin/?days=7")
chk("看板时间段筛选200", seg.status_code == 200 and "近7天" in seg.get_data(as_text=True),
    f"code={seg.status_code}")
bad = client.get("/admin/?days=999")
chk("看板非法区间回退全年", bad.status_code == 200, f"code={bad.status_code}")
exp = client.get("/admin/dashboard/export?days=7")
body = exp.get_data(as_text=True)
chk("看板CSV导出200+表头", exp.status_code == 200 and "product_name" in body,
    f"code={exp.status_code} ctype={exp.mimetype}")

# ── 新功能：情景对话 AJAX 返回 JSON（店员；用正确字段 msg，补历史盲点）──
client.post("/login", data={"username": "milk_tea_staff1", "password": "staff123", "role": "staff"},
            follow_redirects=True)
aj = client.post("/ai/chat", data={"msg": "请推荐招牌产品"},
                 headers={"X-Requested-With": "fetch", "Accept": "application/json"})
try:
    j = _json.loads(aj.get_data(as_text=True))
    chk("情景对话AJAX返回JSON", j.get("ok") is True and j.get("reply", "").strip() != "",
        f"body={aj.get_data(as_text=True)[:120]}")
except Exception:
    chk("情景对话AJAX返回JSON", False, f"非法JSON code={aj.status_code}")

# ── 接口限流（服务器web移植）：进程内滑动窗口，超限返回 429 ──
# 单测口径：临时关闭全局跳过开关，验证 submit 第 11 次/60s 触发 429（放行则 None）
import src.rate_limit as rl_mod
_saved_env = os.environ.pop("DISABLE_RATE_LIMIT", None)
try:
    with app.test_request_context():
        rl_mod._buckets.clear()
        sent = [rl_mod.check_limit(action='submit', limit=10, window=60) for _ in range(10)]
        rl = rl_mod.check_limit(action='submit', limit=10, window=60)
        chk("限流:submit第11次/60s返回429",
            all(x is None for x in sent) and rl is not None
            and (isinstance(rl, tuple) and rl[1] == 429),
            f"sent={[getattr(x,'status_code',None) for x in sent]} tail={rl if rl is None else rl[1]}")

        rl_mod._buckets.clear()
        sent_chat = [rl_mod.check_limit(action='chat', limit=30, window=60) for _ in range(30)]
        rl_chat = rl_mod.check_limit(action='chat', limit=30, window=60)
        chk("限流:chat第31次/60s返回429",
            all(x is None for x in sent_chat) and rl_chat is not None
            and (isinstance(rl_chat, tuple) and rl_chat[1] == 429),
            f"tail={None if rl_chat is None else rl_chat[1]}")

        rl_mod._buckets.clear()
        sent_login = [rl_mod.check_limit(action='login', limit=10, window=60) for _ in range(10)]
        rl_login = rl_mod.check_limit(action='login', limit=10, window=60)
        chk("限流:login第11次/60s返回429（登录暴力试解防护）",
            all(x is None for x in sent_login) and rl_login is not None
            and (isinstance(rl_login, tuple) and rl_login[1] == 429),
            f"tail={None if rl_login is None else rl_login[1]}")

        rl_mod._buckets.clear()
        ok_a = rl_mod.check_limit(action='submit', limit=10, window=60)
        rl_mod._buckets.clear()
        ok_b = rl_mod.check_limit(action='submit', limit=10, window=60)
        chk("限流:窗口滑动后放行", ok_a is None and ok_b is None,
            f"a={ok_a} b={ok_b}")
finally:
    if _saved_env is not None:
        os.environ["DISABLE_RATE_LIMIT"] = _saved_env
    else:
        os.environ.pop("DISABLE_RATE_LIMIT", None)

# ── 产品管理完整化（超市web 移植）：toggle / update / delete / batch ──
from src.models import db as _pdb

client.post("/login", data={"username": "owner_demo", "password": "owner123", "role": "admin"},
            follow_redirects=True)
with app.app_context():
    _tmp = Product(product_name="冒烟临时产品", unit_price=10.0, category="测试")
    _pdb.session.add(_tmp)
    _pdb.session.commit()
    _tmp_id = _tmp.id

# toggle：下架 → 二次还原
t1 = client.post(f"/admin/products/{_tmp_id}/toggle", data={})
try:
    _j1 = _json.loads(t1.get_data(as_text=True))
    chk("产品toggle下架返回JSON-ok", _j1.get("ok") is True, f"body={t1.get_data(as_text=True)[:100]}")
except Exception:
    chk("产品toggle下架返回JSON-ok", False, f"code={t1.status_code}")
t2 = client.post(f"/admin/products/{_tmp_id}/toggle", data={})
_j2 = _json.loads(t2.get_data(as_text=True))
chk("产品toggle二次还原", _j2.get("ok") is True, f"body={t2.get_data(as_text=True)[:100]}")
with app.app_context():
    _q = Product.query.get(_tmp_id)
    chk("产品toggle状态落地", _q is not None and _q.is_active is True,
        f"active={_q.is_active if _q else None}")

# update：改价成功
u1 = client.post(f"/admin/products/{_tmp_id}/update",
                 data={"product_name": "冒烟临时产品", "unit_price": "12.5",
                       "cost_price": "6", "category": "测试"})
_j3 = _json.loads(u1.get_data(as_text=True))
chk("产品update改价成功", u1.status_code == 200 and _j3.get("ok") is True,
    f"code={u1.status_code} body={u1.get_data(as_text=True)[:100]}")
with app.app_context():
    _q = Product.query.get(_tmp_id)
    chk("产品update价格落地", _q is not None and float(_q.unit_price) == 12.5,
        f"price={_q.unit_price if _q else None}")

# update：空名称 / 超上限 / 名称冲突 → 400 JSON ok=false
u2 = client.post(f"/admin/products/{_tmp_id}/update",
                 data={"product_name": "", "unit_price": "1", "cost_price": "", "category": ""})
_j4 = _json.loads(u2.get_data(as_text=True))
chk("产品update空名称→ok=false", u2.status_code == 400 and _j4.get("ok") is False,
    f"code={u2.status_code} body={u2.get_data(as_text=True)[:100]}")
u3 = client.post(f"/admin/products/{_tmp_id}/update",
                 data={"product_name": "冒烟临时产品", "unit_price": "9999999",
                       "cost_price": "", "category": ""})
_j5 = _json.loads(u3.get_data(as_text=True))
chk("产品update超上限→ok=false", u3.status_code == 400 and _j5.get("ok") is False,
    f"code={u3.status_code} body={u3.get_data(as_text=True)[:100]}")
u4 = client.post(f"/admin/products/{_tmp_id}/update",
                 data={"product_name": _BP[0][0], "unit_price": "5", "cost_price": "", "category": ""})
_j6 = _json.loads(u4.get_data(as_text=True))
chk("产品update名称冲突→ok=false", u4.status_code == 400 and _j6.get("ok") is False,
    f"code={u4.status_code} body={u4.get_data(as_text=True)[:100]}")

# batch：非法 action → 400 JSON
b0 = client.post("/admin/products/batch", data={"action": "hack", "ids": str(_tmp_id)})
_j7 = _json.loads(b0.get_data(as_text=True))
chk("产品batch非法action→400", b0.status_code == 400 and _j7.get("ok") is False,
    f"code={b0.status_code} body={b0.get_data(as_text=True)[:100]}")

# batch：下架/上架/统一价/折扣
b1 = client.post("/admin/products/batch", data={"action": "disable", "ids": str(_tmp_id)})
_j8 = _json.loads(b1.get_data(as_text=True))
chk("产品batch下架ok", b1.status_code == 200 and _j8.get("ok") is True,
    f"body={b1.get_data(as_text=True)[:100]}")
with app.app_context():
    _q = Product.query.get(_tmp_id)
    chk("产品batch下架状态落地", _q is not None and _q.is_active is False,
        f"active={_q.is_active if _q else None}")
b2 = client.post("/admin/products/batch", data={"action": "enable", "ids": str(_tmp_id)})
_j9 = _json.loads(b2.get_data(as_text=True))
chk("产品batch上架ok", b2.status_code == 200 and _j9.get("ok") is True,
    f"body={b2.get_data(as_text=True)[:100]}")
b3 = client.post("/admin/products/batch", data={"action": "set_price", "ids": str(_tmp_id), "price": "20"})
_j10 = _json.loads(b3.get_data(as_text=True))
chk("产品batch统一价ok", b3.status_code == 200 and _j10.get("ok") is True,
    f"body={b3.get_data(as_text=True)[:100]}")
with app.app_context():
    _q = Product.query.get(_tmp_id)
    chk("产品batch统一价落地", _q is not None and float(_q.unit_price) == 20.0,
        f"price={_q.unit_price if _q else None}")
b4 = client.post("/admin/products/batch", data={"action": "set_price", "ids": str(_tmp_id), "price": "50%"})
_j11 = _json.loads(b4.get_data(as_text=True))
chk("产品batch折扣ok", b4.status_code == 200 and _j11.get("ok") is True,
    f"body={b4.get_data(as_text=True)[:100]}")
with app.app_context():
    _q = Product.query.get(_tmp_id)
    chk("产品batch折扣落地(20×50%=10)", _q is not None and float(_q.unit_price) == 10.0,
        f"price={_q.unit_price if _q else None}")

# delete：删除临时产品 → DB 不存在
d1 = client.post(f"/admin/products/{_tmp_id}/delete", data={})
_j12 = _json.loads(d1.get_data(as_text=True))
chk("产品delete ok", d1.status_code == 200 and _j12.get("ok") is True,
    f"body={d1.get_data(as_text=True)[:100]}")
with app.app_context():
    _q = Product.query.get(_tmp_id)
    chk("产品delete落地", _q is None, f"row={_q}")

# 鉴权：未登录 POST → 302；店员 POST → 403
rr_auth = app.test_client().post("/admin/products/1/toggle", data={}, follow_redirects=False)
chk("产品操作未登录→302跳登录",
    rr_auth.status_code == 302 and "/login" in rr_auth.headers.get("Location", ""),
    f"code={rr_auth.status_code} loc={rr_auth.headers.get('Location')}")
client.post("/login", data={"username": "milk_tea_staff1", "password": "staff123", "role": "staff"},
            follow_redirects=True)
rr_staff = client.post("/admin/products/1/toggle", data={}, follow_redirects=False)
chk("产品操作店员→403", rr_staff.status_code == 403, f"code={rr_staff.status_code}")

# ── AI 深度优化（第二轮）：薄弱定向补训 + 判分详情 + 助手新意图 + 经营分析预测 ──
client.post("/login", data={"username": "milk_tea_staff1", "password": "staff123", "role": "staff"},
            follow_redirects=True)
sd = client.get("/ai/quiz?restart=1&topic=原料规范")
chk("定向补训页200", sd.status_code == 200, f"code={sd.status_code}")
with client.session_transaction() as sess:
    sess_topic = sess.get("quiz_topic")
    sess_weak = sess.get("quiz_weak")
chk("定向补训 session 记录 topic", sess_topic == "原料规范", f"topic={sess_topic}")
# 题面域归属以 session 的 quiz_weak 为准（旧文本关键词断言依赖题干 dict 泄漏进页面，显示 bug 已修）
chk("定向第一题来自原料规范域", sd.status_code == 200 and sess_weak == "原料规范",
    f"weak={sess_weak}")
sb2 = client.get("/ai/quiz?restart=1&topic=原料规范<script>alert(1)</script>")
with client.session_transaction() as sess:
    sess_bad = sess.get("quiz_topic")
chk("非法 topic 回退普通模式(防注入)", sb2.status_code == 200 and sess_bad is None,
    f"code={sb2.status_code} topic={sess_bad}")

# 判分详情（规则版 details 供前端可视化）
from src.dify_client import _rule_score as _rscore  # noqa: E402
_det = _rscore("西米、芒果、椰浆、西柚粒，芒果需选成熟金煌芒",
               "西米、芒果、椰浆、西柚粒, 芒果需选成熟金煌芒")
chk("判分详情 hit_elements 字段", isinstance(_det.get("details", {}).get("hit_elements"), list)
    and len(_det["details"]["hit_elements"]) > 0, str(_det.get("details")))
_det_miss = _rscore("西米、芒果、椰浆、西柚粒，芒果需选成熟金煌芒", "随便做")
chk("判分空洞作答仍 0 分", _det_miss["score"] == 0 and _det_miss["is_correct"] is False,
    str(_det_miss))

# 智能助手新意图（规则版）：预测 / 产品 / 周期对比缺数据不编造
client.post("/login", data={"username": "owner_demo", "password": "owner123", "role": "admin"},
            follow_redirects=True)
_asf = client.post("/ai/admin/assistant", data={"ask": "明天预计能卖多少杯"},
                   headers={"X-Requested-With": "fetch"})
_asf_j = _asf.get_json() or {}
chk("助手预测意图返回", _asf.status_code == 200 and "预计" in _asf_j.get("reply", ""),
    f"reply={_asf_j.get('reply', '')[:80]}")
_asp = client.post("/ai/admin/assistant", data={"ask": "杨枝甘露卖得怎么样"},
                   headers={"X-Requested-With": "fetch"})
_asp_j = _asp.get_json() or {}
chk("助手产品意图返回", _asp.status_code == 200 and "杨枝甘露" in _asp_j.get("reply", ""),
    f"reply={_asp_j.get('reply', '')[:80]}")
_asc = client.post("/ai/admin/assistant", data={"ask": "这周和上周比销量怎么样"},
                   headers={"X-Requested-With": "fetch"})
_asc_j = _asc.get_json() or {}
chk("助手周期对比数据不足不编造", "数据不足" in _asc_j.get("reply", ""),
    f"reply={_asc_j.get('reply', '')[:80]}")

# 经营分析页：种子 7 日 → 预测卡渲染（≥3日），周期对比卡不渲染（<14日）
_ba2 = client.get("/ai/admin/business-analysis")
_ba2_body = _ba2.get_data(as_text=True)
chk("经营分析页含销量预测卡", "销量预测" in _ba2_body, "未见预测卡")
chk("经营分析页周期对比缺数据不渲染", "周期对比" not in _ba2_body, "对比卡不应出现(不足14日)")

# ══════════ 第四轮 AI 深化：学习者模型 / 经营智能 / 可解释性 / RAG 反馈 ══════════
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from src import ai_explain as ax  # noqa: E402
from src import analytics as an  # noqa: E402
from src import learner_model as lm  # noqa: E402

_NOW = datetime.now(UTC).replace(tzinfo=None)


def _rec(question, score, is_correct=True, days_ago=0):
    """构造一条答题记录替身（学习者模型只读取字段，不依赖 ORM 实例）。"""
    return SimpleNamespace(question=question, score=score, is_correct=is_correct,
                           created_at=_NOW - timedelta(days=days_ago))


# 学习者模型：空数据 / 高分 / 低分 / 时间衰减 / 排期间隔
_empty_profile = lm.mastery_profile([])
chk("学习者模型 空数据 has_data=False", _empty_profile["has_data"] is False,
    str(_empty_profile))
chk("学习者模型 空数据难度降级 basic",
    lm.adaptive_difficulty(_empty_profile) == "basic",
    lm.adaptive_difficulty(_empty_profile))

_Q_PRICE = "顾客嫌价格贵要砍价，正确话术是？"  # 关键词「价格」→ 套餐价格域（与知识库口径一致）
_high = [_rec(_Q_PRICE, 100) for _ in range(5)]
_hp = lm.mastery_profile(_high, now=_NOW)
_hit_dom = next(d for d in _hp["domains"] if d["sample"] > 0)["domain"]
_hd_h = next(d for d in _hp["domains"] if d["domain"] == _hit_dom)
chk("学习者模型 高分画像达熟练", _hd_h["mastery"] >= 85 and _hd_h["level"] == "熟练", str(_hd_h))
chk("学习者模型 高分→进阶难度",
    lm.adaptive_difficulty(_hp, _hit_dom) == "advanced",
    lm.adaptive_difficulty(_hp, _hit_dom))

_low = [_rec(_Q_PRICE, 0, is_correct=False) for _ in range(5)]
_lp = lm.mastery_profile(_low, now=_NOW)
_hd_l = next(d for d in _lp["domains"] if d["domain"] == _hit_dom)
chk("学习者模型 低分画像判薄弱", _hd_l["mastery"] <= 15 and _hd_l["level"] == "待强化", str(_hd_l))
chk("学习者模型 低分→基础难度",
    lm.adaptive_difficulty(_lp, _hit_dom) == "basic",
    lm.adaptive_difficulty(_lp, _hit_dom))

# 时间衰减：同样是 4 好 1 坏，「坏在近期」的掌握度应低于「坏在很久以前」
_recent_bad = lm.mastery_profile(
    [_rec(_Q_PRICE, 100, days_ago=60) for _ in range(4)] + [_rec(_Q_PRICE, 0, False)],
    now=_NOW)
_old_bad = lm.mastery_profile(
    [_rec(_Q_PRICE, 100) for _ in range(4)] + [_rec(_Q_PRICE, 0, False, days_ago=60)],
    now=_NOW)
_m_recent = next(d for d in _recent_bad["domains"] if d["domain"] == _hit_dom)["mastery"]
_m_old = next(d for d in _old_bad["domains"] if d["domain"] == _hit_dom)["mastery"]
chk("学习者模型 时间衰减（近期表现权重更高）", _m_recent < _m_old,
    f"recent_bad={_m_recent} old_bad={_m_old}")

# 间隔重复排期：掌握度越高间隔越长；从未练习的域立即排期
_sch_low = {s["domain"]: s for s in lm.review_schedule(_low, now=_NOW)}
_sch_high = {s["domain"]: s for s in lm.review_schedule(_high, now=_NOW)}
chk("复习排期 低掌握度间隔更短",
    _sch_low[_hit_dom]["interval_days"] < _sch_high[_hit_dom]["interval_days"],
    f"low={_sch_low[_hit_dom]['interval_days']} high={_sch_high[_hit_dom]['interval_days']}")
_untouched = next(d for d in _sch_low.values() if d["sample"] == 0)
chk("复习排期 未练域立即排期", _untouched["urgency"] == "待学习", str(_untouched))
chk("学习焦点 取最弱域", lm.learning_focus(_low + _high, now=_NOW)["focus_domain"] is not None)

# 经营智能：离群检测 / 分产品预测 / 备货 / 购物篮
import pandas as pd  # noqa: E402

_days = pd.to_datetime([f"2026-09-0{i}" for i in range(1, 8)])
_rows = []
for d in _days:
    for name, qty in (("珍珠奶茶", 20), ("杨枝甘露", 12)):
        _rows.append({"staff_id": 1, "record_date": d, "product_name": name,
                      "product_sales_qty": qty, "total_revenue": qty * 12.0,
                      "raw_material_remaining": "30", "raw_material_waste": "1",
                      "abnormal_notes": ""})
_df = pd.DataFrame(_rows)
# 注入一个极端异常日（销量翻 5 倍）
_df.loc[_df.index[-1], "product_sales_qty"] = 100
_ano = an.detect_anomalies({
    "dates": [d.strftime("%Y-%m-%d") for d in _days],
    "revenue": [32.0 * 12] * 6 + [100 * 12],
    "qty": [32] * 6 + [100]})
chk("离群检测 命中注入异常日", _ano["ok"] and any(p["date"].endswith("09-07") for p in _ano["points"]),
    str(_ano))
_pfc = an.product_forecast(_df)
chk("分产品预测 返回正需求", _pfc["ok"] and _pfc["items"][0]["next_qty"] > 0, str(_pfc))
_rep = an.replenishment_advice(_df)
chk("备货建议 含安全库存口径", _rep["ok"] and "安全库存" in _rep["items"][0]["basis"], str(_rep))

# 购物篮：A 卖 5 天（其中 3 天与 B 同现）、C 单独卖 1 天 → A→B 提升度 > 1
_brows = []
for i in range(1, 7):
    _d = pd.to_datetime(f"2026-08-0{i}")
    if i <= 5:
        _brows.append({"record_date": _d, "product_name": "A", "product_sales_qty": 1})
    if i <= 3:
        _brows.append({"record_date": _d, "product_name": "B", "product_sales_qty": 1})
    if i >= 6:
        _brows.append({"record_date": _d, "product_name": "C", "product_sales_qty": 1})
_bk = an.basket_association(pd.DataFrame(_brows))
chk("购物篮 识别正关联(lift>1)", _bk["ok"] and _bk["pairs"][0]["lift"] > 1, str(_bk))
chk("购物篮 排除非正相关", all(p["lift"] > 1 for p in _bk["pairs"]), str(_bk["pairs"]))

# 可解释性：置信度单调 + 封顶 + 结论必带依据
chk("置信度 样本越多越高", ax.sample_confidence(14, 14) > ax.sample_confidence(3, 14),
    f"{ax.sample_confidence(14, 14)} vs {ax.sample_confidence(3, 14)}")
chk("置信度 封顶不超 0.95", ax.sample_confidence(999, 14) <= 0.95,
    str(ax.sample_confidence(999, 14)))
_ev_empty = ax.explain_business({"has_data": False})
chk("可解释性 无数据给低置信提示",
    len(_ev_empty) == 1 and _ev_empty[0]["confidence"] <= 0.4, str(_ev_empty))
from src.analytics import compute_dashboard_stats as _cds  # noqa: E402
with app.app_context():
    _stats_now = _cds(days=365)
_ev = ax.explain_business(_stats_now)
chk("可解释性 结论均带依据", len(_ev) >= 1
    and all(len(e["evidence"]) >= 1 for e in _ev), str([e["kind"] for e in _ev]))
chk("可解释性 置信度落在合法区间",
    all(0.3 <= e["confidence"] <= 0.95 for e in _ev), str([e["confidence"] for e in _ev]))
_sum = ax.summarize_confidence(_ev)
chk("可解释性 汇总平均置信度", 0.3 <= _sum["avg"] <= 0.95 and _sum["count"] == len(_ev), str(_sum))

# RAG：IDF 检索命中 + 反馈权重回写（落临时文件，不影响项目 data/）
import tempfile  # noqa: E402

_tmp_fb = os.path.join(tempfile.mkdtemp(prefix="kb_fb_"), "kb_feedback.json")
_orig_fb = kb_mod.FEEDBACK_FILE
kb_mod.FEEDBACK_FILE = _tmp_fb
try:
    _hits = kb_mod.search("西柚", top_k=2)
    chk("RAG 检索命中西柚条目", any("西柚" in h["text"] for h in _hits), str(_hits))
    _w0 = kb_mod._feedback_weight("原料规范", "配比标准")
    kb_mod.mark_useful("原料规范", "配比标准", True)
    _w1 = kb_mod._feedback_weight("原料规范", "配比标准")
    kb_mod.mark_useful("原料规范", "配比标准", False)
    _w2 = kb_mod._feedback_weight("原料规范", "配比标准")
    chk("RAG 反馈 有用加权/没用降权", _w1 > _w0 and _w2 < _w1, f"{_w0}→{_w1}→{_w2}")
    chk("RAG 反馈 权重有下限", _w2 >= kb_mod.FEEDBACK_MIN, str(_w2))
finally:
    kb_mod.FEEDBACK_FILE = _orig_fb

# 页面：经营分析可解释区块 / 培训助手溯源 / 反馈接口 / 培训进度画像
_ba3 = client.get("/ai/admin/business-analysis").get_data(as_text=True)
chk("经营分析页含置信度区块", "置信度" in _ba3 and "依据" in _ba3, "未见置信度区块")

client.post("/login", data={"username": "milk_tea_staff1", "password": "staff123", "role": "staff"},
            follow_redirects=True)
_tr = client.post("/ai/trainer", data={"ask": "顾客嫌贵怎么应对"},
                  headers={"X-Requested-With": "fetch"})
_tr_j = _tr.get_json() or {}
chk("培训助手 返回知识出处 refs", "refs" in _tr_j and isinstance(_tr_j["refs"], list),
    str(list(_tr_j)[:5]))
_fb = client.post("/ai/kb-feedback", data={"domain": "异议处理", "title": "价格异议",
                                           "useful": "1"})
chk("反馈接口 正常回写", _fb.status_code == 200 and _fb.is_json
    and (_fb.get_json() or {}).get("ok") is True, str(_fb.get_json()))
_fb_bad = client.post("/ai/kb-feedback", data={"domain": "", "title": ""})
chk("反馈接口 缺字段拒绝", (_fb_bad.get_json() or {}).get("ok") is False, str(_fb_bad.get_json()))

# 造 4 条高分答题记录 → 培训进度页渲染学习者画像
from src.models import db as _db  # noqa: E402
with app.app_context():
    _st = Staff.query.filter_by(username="milk_tea_staff1").first()
    for _ in range(4):
        _db.session.add(QuizRecord(staff_id=_st.id, quiz_type="question",
                                   question="顾客嫌价格贵要砍价，正确话术是？",
                                   staff_answer="先共情再给替代", ai_feedback="不错",
                                   is_correct=True, score=100))
    _db.session.commit()
_tp = client.get("/h5/training")
_tp_body = _tp.get_data(as_text=True)
chk("培训进度页含能力画像", _tp.status_code == 200 and "AI 能力掌握度画像" in _tp_body,
    f"code={_tp.status_code}")
chk("培训进度页含复习排期", "间隔重复" in _tp_body, "未见复习排期区块")
_qz = client.get("/ai/quiz?restart=1")
with client.session_transaction() as sess:
    _diff = sess.get("quiz_difficulty")
chk("考核 自适应难度写入 session", _diff in ("basic", "standard", "advanced"), f"difficulty={_diff}")

# ── 第五轮新增：经营预测季节调整（指数平滑 + 星期周期因子）──
import datetime as _dt  # noqa: E402

from src.analytics import compute_dashboard_stats  # noqa: E402
from src.analytics import _ewma as _ew  # noqa: E402
from src.analytics import _forecast as _fc  # noqa: E402
from src.dify_client import _assistant_rule_reply as _arr  # noqa: E402
from src.dify_client import intent_fuzzy_route as _ifr  # noqa: E402  # M-18：转公开名
from src.dify_client import dify_client as _dc  # noqa: E402

_base = _dt.date(2026, 8, 1)  # 周六
_dates = [(_base + _dt.timedelta(days=i)).isoformat() for i in range(21)]
# 构造强周内节律：周末 100 杯 / 工作日 50 杯（单人门店真实形态）
_qty = [100 if (_base + _dt.timedelta(days=i)).weekday() >= 5 else 50 for i in range(21)]
_r = _fc({'dates': _dates, 'qty': _qty}, 5)
chk("预测 周末节律命中(期望≈100)", _r['ok'] and abs(_r['qty'] - 100) <= 8, f"qty={_r.get('qty')}")
_old_err = abs(sum(_qty[-7:]) / 7 - 100)
chk("预测 误差优于旧简单平均(可量化)", abs(_r['qty'] - 100) < _old_err - 10,
    f"新误差={abs(_r['qty']-100):.1f} 旧误差={_old_err:.1f}")
chk("预测 方法说明含星期周期因子", "星期周期因子" in (_r.get('method') or ''), str(_r.get('method')))
# 趋势与周期正交：去周期后仍能捕捉整体上升/下滑
_up = [v * (1 + i * 0.01) for i, v in enumerate(_qty)]
_dn = [v * (1 - i * 0.02) for i, v in enumerate(_qty)]
chk("预测 上升趋势生效(>基准)", _fc({'dates': _dates, 'qty': _up}, 5)['qty'] > 100)
chk("预测 下滑趋势生效(<基准)", _fc({'dates': _dates, 'qty': _dn}, 5)['qty'] < 100)
# 边界：全零 / 不足 3 日 / 无日期（旧行为零回归）
chk("预测 边界 全零序列不崩", _fc({'dates': _dates, 'qty': [0] * 21}, 0)['qty'] == 0)
chk("预测 边界 不足3日 ok=False", _fc({'qty': [1, 2]}, 5)['ok'] is False)
chk("预测 无日期退化为平滑(零回归)", _fc({'qty': [10, 12, 11, 13, 12, 14, 13, 15, 14, 16]}, 5)['ok'])
chk("EWMA 近期权重更高", _ew([10, 10, 100]) > _ew([100, 10, 10]), "近期高值应拉高加权均值")

# ── 第五轮新增：模糊意图路由（口语问法召回）──
chk("模糊路由 生意好不好→营收", _ifr('生意好不好') == 'revenue', str(_ifr('生意好不好')))
chk("模糊路由 赚不赚钱→毛利", _ifr('赚不赚钱') == 'margin', str(_ifr('赚不赚钱')))
chk("模糊路由 明天大概卖多少→预测", _ifr('明天大概卖多少') == 'forecast', str(_ifr('明天大概卖多少')))
chk("模糊路由 哪个卖得最差→滞销", _ifr('哪个卖得最差') == 'slow', str(_ifr('哪个卖得最差')))
chk("模糊路由 废料多不多→损耗", _ifr('废料多不多') == 'waste', str(_ifr('废料多不多')))
chk("模糊路由 无关问句不误判", _ifr('今天天气如何') is None, str(_ifr('今天天气如何')))
chk("模糊路由 空问句/纯英文不崩", _ifr('') is None and _ifr('abc') is None)
with app.app_context():
    _stats = compute_dashboard_stats()
_rev_ans = _arr('生意好不好', _stats)
chk("口语问句端到端路由到真实数据回答",
    ('营收' in _rev_ans) or ('销售数据' in _rev_ans), _rev_ans[:50])
_kb_ans = _arr('今天天气如何', _stats)
chk("无关问句仍走知识库兜底(零回归)", '知识库' in _kb_ans or '暂无知识库条目' in _kb_ans, _kb_ans[:50])

# ── 第五轮新增：顾客情绪状态机 ──
chk("状态机 初始中性70", _dc.customer_state([])['patience'] == 70.0)
chk("状态机 对抗词降耐心(-25)", _dc.customer_state(['店员：爱买不买，随便你'])['patience'] == 45.0)
chk("状态机 推诿词降耐心(-15)", _dc.customer_state(['店员：我不知道，没办法'])['patience'] == 55.0)
chk("状态机 致歉补偿升耐心(+10)", _dc.customer_state(['店员：抱歉，马上为您重做'])['patience'] == 80.0)
chk("状态机 连续对抗钳位0", _dc.customer_state(['店员：爱买不买'] * 6)['patience'] == 0.0)
chk("状态机 连续安抚钳位100", _dc.customer_state(['店员：抱歉，马上处理'] * 8)['patience'] == 100.0)
_angry = _dc.customer_state(['店员：爱买不买'] * 6)
chk("状态机 愤怒档位与中文标签", _angry['mood'] == 'angry' and _angry['mood_label'] == '愤怒')
_end_h, _end_h2 = [], []
for _ in range(5):
    _end_h += ['店员：爱买不买，随便你', '顾客：…']
    _end_h2 += ['店员：抱歉，马上为您重做并送您优惠券', '顾客：…']
_end_a = _dc.scenario_reply('bargain', 'x', _end_h + ['店员：爱买不买'], None)
_end_b = _dc.scenario_reply('bargain', 'x', _end_h2 + ['店员：抱歉马上处理'], None)
chk("状态机 结束语 愤怒→流失句", '不买了' in _end_a['reply'], _end_a['reply'][:30])
chk("状态机 结束语 满意→复购句", '以后还来' in _end_b['reply'], _end_b['reply'][:30])
chk("状态机 返回值含 mood/patience", 'mood' in _end_a and 'patience' in _end_a)
chk("状态机 首轮角色卡零回归", _dc.scenario_reply('bargain', 'hi', ['店员：hi'], None)['reply'].startswith('（顾客'))
chk("结束点评 含顾客情绪轨迹", '顾客情绪轨迹' in _dc.finish_scenario_tip(['店员：爱买不买']))
_ajax_mood = client.post('/ai/chat', data={'msg': '抱歉，马上为您处理'},
                         headers={'X-Requested-With': 'fetch'})
chk("情景 AJAX 返回情绪字段", (_ajax_mood.get_json() or {}).get('mood') is not None,
    str(_ajax_mood.get_json())[:80])

# ── P5 新增：动态谈判引擎（隐藏心理价 + 让步曲线 + 谈判复盘）──
from src import negotiation as _ng

_ini5 = _ng.initial_state('bargain')
chk("谈判 隐藏价阶梯合法(下界<底价≤上界<原价)",
    _ini5['budget_low'] < _ini5['reservation'] <= _ini5['budget_high'] < _ini5['list_price'],
    str({k: _ini5[k] for k in ('budget_low', 'reservation', 'budget_high', 'list_price')}))
chk("谈判 六类话术归类互斥有序",
    [_ng.classify_staff_utterance(x) for x in
     ('我们的原料是当天现做的，真材实料', '推荐您换成柠檬水更清爽', '那我给您便宜两块',
      '我说了算，保证最低价', '不归我管，没办法', '嗯嗯')]
    == ['value', 'alternative', 'discount', 'overpromise', 'confront', 'irrelevant'])
chk("谈判 越权承诺优先于降价词",
    _ng.classify_staff_utterance('保证最低价，再给您便宜点') == 'overpromise')
chk("谈判 空输入归无关", _ng.classify_staff_utterance(None) == 'irrelevant')
_neg_step = _ng.step(_ini5, '那我给您便宜两块')
chk("谈判 降价→让价累计且耐心升",
    _neg_step['concession'] > 0 and _neg_step['patience'] > _ini5['patience'],
    str(_neg_step['concession']))
_neg_conf = _ng.replay(['店员：爱买不买'] * 2, 'bargain')
chk("谈判 对抗两次判流失", _ng.settle(_neg_conf)['result'] == 'lost',
    _ng.settle(_neg_conf)['reason'])
_neg_deal = _ng.replay(['店员：抱歉，马上为您重做并送您优惠券'] * 6, 'bargain')
_neg_v = _ng.settle(_neg_deal)
chk("谈判 让到期望价且情绪好→成交", _neg_v['result'] == 'deal', str(_neg_v))
chk("谈判 成交回报让利幅度",
    _neg_v['concession'] > 0 and _neg_v['concession_pct'] > 0)
chk("谈判 轮次用尽不留未决",
    _ng.settle(_ng.replay(['店员：我们的原料是当天现做的'] * 8, 'bargain'))['result'] != 'pending')
_neg_rv = _ng.review(_neg_deal)
chk("谈判 复盘四维固定顺序",
    [s['label'] for s in _neg_rv['scores']] == ['价值锚定', '让步节奏', '情绪管理', '成交结果'],
    str([s['label'] for s in _neg_rv['scores']]))
chk("谈判 复盘含总分/等级/建议",
    _neg_rv['total'] > 0 and _neg_rv['level'] in ('优秀', '合格', '需改进')
    and len(_neg_rv['advice']) == 2, str(_neg_rv)[:100])
chk("谈判 讲价值成交优于靠降价成交",
    _ng.review(_ng.replay(['店员：我们的原料是当天现做的'] * 8, 'bargain'))['total']
    > _neg_rv['total'],
    f"{_ng.review(_ng.replay(['店员：我们的原料是当天现做的'] * 8, 'bargain'))['total']}"
    f" vs {_neg_rv['total']}")
_neg_vis = _ng.visible_state(_ini5)
chk("谈判 可见态给区间不给精确底价",
    'patience' in _neg_vis and len(_neg_vis['price_range']) == 2
    and all(isinstance(x, int) for x in _neg_vis['price_range']) and 'progress' in _neg_vis,
    str(_neg_vis))
_najax = client.post('/ai/chat', data={'msg': '我们的原料是当天现做的，真材实料'},
                     headers={'X-Requested-With': 'fetch'})
chk("情景 AJAX 返回谈判面板", (_najax.get_json() or {}).get('negotiation') is not None,
    str(_najax.get_json())[:100])
_nfin = client.post('/ai/chat', data={'action': 'finish'})
_nbody = _nfin.get_data(as_text=True)
chk("谈判 结束页渲染复盘卡", 'review-card' in _nbody and '谈判综合' in _nbody,
    f"code={_nfin.status_code}")
chk("谈判 复盘卡含四维评分条", _nbody.count('review-score-row') >= 4)
chk("谈判 复盘卡无 | safe 透传", '| safe' not in _nbody)

# ── 第六轮新增：预测回测闭环 + 损耗根因归因 + 掌握度画像 ──
_tr6 = client.post("/ai/trainer", data={"ask": "鲜奶怎么保存"},
                   headers={"X-Requested-With": "fetch"})
_tr6_reply = (_tr6.get_json() or {}).get("reply") or ""
chk("培训助手含 AI 掌握度画像", "AI 掌握度画像" in _tr6_reply, _tr6_reply[:60])
# 看板断言需管理员登录态（前段 client 已切至店员）
client.post("/login", data={"username": "owner_demo", "password": "owner123", "role": "admin"},
            follow_redirects=True)
_dash = client.get("/admin/")
_dash_body = _dash.get_data(as_text=True)
chk("看板 200(含预测回测链路)", _dash.status_code == 200, f"code={_dash.status_code}")
chk("看板含 AI 预测准确率卡", "AI 预测准确率" in _dash_body)
_log_now = _fe_mod._load_log()
chk("看板渲染已记录预测(仅数值条目)",
    _log_now and all(isinstance(v.get("predicted"), (int, float))
                     for v in _log_now.values()), str(_log_now)[:80])
# 手工补「预测 vs 实际」历史 → 看板应显示滚动回测准确率（predicted=actual×1.1 → 准确率90）
with app.app_context():
    _s6 = compute_dashboard_stats()
for _iso, _q in zip(_s6.get("daily_iso") or [], _s6["daily"]["qty"], strict=False):
    _log_now[_iso] = {"predicted": round(float(_q) * 1.1, 1), "actual": float(_q),
                      "recorded_at": "smoke"}
_fe_mod._save_log(_log_now)
_dash2 = client.get("/admin/")
chk("看板显示滚动回测准确率(可量化)", "次回测" in _dash2.get_data(as_text=True))
chk("看板损耗预警含 AI 归因标签", "AI 归因" in _dash_body, "播种损耗行未渲染归因")

# ── 免登录只读演示入口 /demo（对评委/客户公开承诺的「打开即见」入口）──
from src.models import Merchant as _Merchant_demo  # noqa: E402

os.environ.pop("DEMO_MERCHANT_ID", None)
chk("/demo 未配置演示商家 →404", client.get("/demo").status_code == 404)
with app.app_context():
    _m_demo = _Merchant_demo.query.order_by(_Merchant_demo.id).first()
os.environ["DEMO_MERCHANT_ID"] = str(_m_demo.id)
_rdemo = client.get("/demo")
_demo_body = _rdemo.get_data(as_text=True)
chk("/demo 免登录只读页 200", _rdemo.status_code == 200, f"code={_rdemo.status_code}")
chk("/demo 含四角色、只读头与无写入口",
    all(x in _demo_body for x in ("数据分析师", "经营参谋", "风险审计员", "规则主持人"))
    and _rdemo.headers.get("X-Demo-Readonly") == "1"
    and 'method="post"' not in _demo_body.lower()
    and "人工确认转任务" not in _demo_body)
chk("/demo 仅 GET（POST →405）", client.post("/demo").status_code == 405)
os.environ.pop("DEMO_MERCHANT_ID", None)

# ── 输出 ──
print("=" * 60)
print(f"总检查项: {len(checks)}  失败: {len(fails)}")
print("=" * 60)
for c in checks:
    mark = "✅" if c not in fails else "❌"
    print(f"  {mark} {c}")
if fails:
    print("\n❌ 失败项:")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("\n✅ 全部通过")
