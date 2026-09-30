"""iCAN Dify 接入态场景回归测试 — 「规则版恒绿、接入 Dify 必炸」的落差用例。

为什么需要这一套
────────────────
现有 smoke_test.py / test_vuln_regression.py 在头部都写死
`os.environ["DIFY_ENABLED"] = "False"`，整条 HTTP 链路跑的是规则版：
  - 规则版点评约 20 字 → session cookie 永远不会溢出；
  - 规则版判分永远成功 → 判分降级分支永远不执行；
  - 规则版零网络 IO → 超时/半死/并发饥饿永远不发生。
于是这三类缺陷在本地 100% 绿灯，接到真机 Dify 当天必现。

本套件用 monkeypatch 把 dify_client 切到 `enabled=True` 并桩掉 `_chat` /
`_completion`，在**不发任何真实网络请求**的前提下复现接入态行为。

用例分组
────────
  D1 会话体积：5 轮 Dify 长点评写入 session，cookie 是否越过 4093 字节红线
  D2 判分降级留痕污染：AI 挂掉时仍写 0 分 QuizRecord，且题号照常推进
  D3 半死状态：超时参数形态、首调失败后是否熔断、部署并发度
  D4 情景对话留痕缺口：chat 从不写 QuizRecord，培训页「情景」分类恒为 0

两类断言（重要）
────────────────
  chk()         —— 强断言。失败 = 真实回归，退出码 1，可直接进质量门禁。
  chk_pending() —— 待修复断言。当前**预期失败**，只报 ⚠️ 不阻断；
                   一旦转为通过会打印「已修复」提示，提醒把它升级成 chk()。
这样本套件可以**立刻**进 quality_gate 而不会把门禁刷红，同时又不会让
已知缺陷悄悄躺平——修好了会主动提示收口。

运行：python test_dify_integration.py

门禁状态（M1 覆盖透明化）：**有意豁免**——接入态场景回归（network-only /
monkeypatch 真机形态），不进 quality_gate 的 _ran 清单；需要时手动运行。
"""
import contextlib
import os
import random
import sys
import traceback
import warnings

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["DIFY_ENABLED"] = "False"  # 基础 app 仍建为规则版，接入态由用例内部临时切换

# 必须在 import src.app 之前改写连接串（config 在导入期固化配置）
from test_fixtures import seed_baseline, use_temp_db

use_temp_db()

from flask import url_for

from src import dify_client as dc
from src.app import create_app
from src.dify_client import DEFAULT_SCENARIO, DIFY_ERROR_PREFIX
from src.models import QuizRecord, db

app = create_app('development')
app.config['WTF_CSRF_ENABLED'] = False  # CSRF 由 test_vuln_regression / qa_verify 负责
IDS = seed_baseline(app)
STAFF_ID = IDS['staff_id']

ROOT = os.path.dirname(os.path.abspath(__file__))
# Flask 默认 cookie 上限，与 werkzeug MAX_COOKIE_SIZE 一致
MAX_COOKIE_SIZE = app.config.get('MAX_COOKIE_SIZE', 4093)

with app.test_request_context():
    URL_QUIZ = url_for('ai.quiz')
    URL_CHAT = url_for('ai.chat')
    URL_TRAINING = url_for('h5.training')
    URL_ASSESSMENT = url_for('ai.assessment')
    URL_DASHBOARD = url_for('admin.dashboard')

# ── 断言收集：用 dict 承载，避免 smoke_test/test_vuln_regression 里
#    `c not in fails` 的逐条标记 bug（带 extra 的失败项会被打成 ✅）──
results = []          # [(name, ok, pending, extra)]
_ctx = {"fn": "-"}


def _record(name, ok, pending, extra):
    results.append((f"[{_ctx['fn']}] {name}", bool(ok), pending, extra or ""))


def chk(name, cond, extra=""):
    """强断言：失败即回归，阻断门禁。"""
    _record(name, cond, False, extra)


def chk_pending(name, cond, extra=""):
    """待修复断言：当前预期失败，不阻断；通过则提示可升级为强断言。"""
    _record(name, cond, True, extra)


def run(fn):
    _ctx["fn"] = fn.__name__
    try:
        fn()
    except Exception:
        chk("用例执行未抛异常", False, traceback.format_exc().strip().splitlines()[-1])
    finally:
        _ctx["fn"] = "-"


def login(role='staff'):
    """新建独立 test_client 并登录。"""
    c = app.test_client()
    cred = ({"username": "owner_demo", "password": "owner123", "role": "admin"}
            if role == "admin"
            else {"username": "milk_tea_staff1", "password": "staff123", "role": "staff"})
    r = c.post("/login", data=cred, follow_redirects=True)
    assert r.status_code == 200, f"{role} 登录失败 code={r.status_code}"
    return c


def quiz_count(staff_id=STAFF_ID, quiz_type=None):
    with app.app_context():
        q = QuizRecord.query.filter_by(staff_id=staff_id)
        if quiz_type:
            q = q.filter_by(quiz_type=quiz_type)
        return q.count()


def latest_quiz(staff_id=STAFF_ID):
    with app.app_context():
        return QuizRecord.query.filter_by(staff_id=staff_id).order_by(
            QuizRecord.id.desc()).first()


def wipe_quiz(staff_id=STAFF_ID):
    with app.app_context():
        QuizRecord.query.filter_by(staff_id=staff_id).delete(synchronize_session=False)
        db.session.commit()


# ── 接入态桩：把 dify_client 切成 enabled=True，但不发任何真实请求 ──
@contextlib.contextmanager
def dify_enabled(chat=None, chat_fail=False, completion=None, completion_fail=False,
                 on_completion=None):
    """临时把全局 dify_client 切到「已接入」并桩掉底层调用。

    Args:
        chat: str 或 callable(query)->str，_chat 的返回内容。
        chat_fail: True 时 _chat 返回防腐层失败标记串。
        completion: str，_completion 的返回内容。
        completion_fail: True 时 _completion 返回失败标记串。
        on_completion: 每次 _completion 被调用时触发的回调（用于计数）。
    """
    client = dc.dify_client
    saved = {
        'enabled': client.enabled,
        '_chat': client.__dict__.get('_chat', None),
        '_completion': client.__dict__.get('_completion', None),
    }
    client.enabled = True

    def _chat_stub(query, user, conversation_id=None, api_key=None):
        if chat_fail:
            return f"{DIFY_ERROR_PREFIX} chat 调用失败(stub)", conversation_id
        text = chat(query) if callable(chat) else (chat or '（桩回答）得分:80')
        return text, (conversation_id or 'stub-cid')

    def _completion_stub(query, user, api_key=None):
        if on_completion:
            on_completion(query)
        if completion_fail:
            return f"{DIFY_ERROR_PREFIX} completion 调用失败(stub)"
        return completion or '（桩建议）'

    client._chat = _chat_stub
    client._completion = _completion_stub
    try:
        yield client
    finally:
        client.enabled = saved['enabled']
        for attr in ('_chat', '_completion'):
            if saved[attr] is None:
                client.__dict__.pop(attr, None)   # 还原为类上的真实方法
            else:
                client.__dict__[attr] = saved[attr]


# ══════════════════════ D1 会话体积：Dify 长点评撑爆 cookie ══════════════════════
# 语料用「低压缩率」的随机汉字：session 序列化会走 zlib，
# 重复文本能压到极小，用重复串测会严重低估真实体积。
_CORPUS = ('顾客异议处理服务话术原料规范价格套餐卫生安全库存损耗盘点培训考核店员'
           '管理沟通共情解释价值引导成交复购满意度投诉响应标准流程节点记录核对'
           '称量保存效期温控陈列补货预估波动客单转化留存反馈改进闭环')


def _long_feedback(n_chars=320, seed=0):
    """构造一条长度贴近真实 Dify 点评的、难压缩的中文反馈。"""
    rng = random.Random(seed)
    body = ''.join(rng.choice(_CORPUS) for _ in range(n_chars))
    return f"点评：{body}。得分:80"


def _session_payload_bytes(payload):
    """按 Flask 真实序列化器测算 session cookie 的 value 字节数。"""
    s = app.session_interface.get_signing_serializer(app)
    return len(s.dumps(payload).encode('utf-8'))


def _set_cookie_size(resp):
    """取本次响应中 session cookie 的整条 Set-Cookie 字节数；无则返回 0。"""
    for raw in resp.headers.getlist('Set-Cookie'):
        if raw.startswith('session='):
            return len(raw.encode('utf-8'))
    return 0


def test_D1_five_rounds_dify_feedback_cookie_within_limit():
    """5 轮 Dify 长点评后，session cookie 不得越过 4093 字节。

    注意：**不能**断言「第 5 轮 session 还在」——Werkzeug 测试 cookie jar
    不执行浏览器的 4093 限制，那条断言在 test_client 下恒真，是假阴性陷阱。
    真正能抓到问题的只有「实际下发的 Set-Cookie 字节数」。
    """
    wipe_quiz()
    sizes = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with dify_enabled(chat=lambda q, _c=[0]: _long_feedback(seed=_c.append(1) or len(_c))):
            c = login('staff')
            c.get(f"{URL_QUIZ}?restart=1")
            for i in range(5):
                r = c.post(URL_QUIZ, data={'answer': f'第{i + 1}轮作答：先共情再解释产品价值'})
                sizes.append(_set_cookie_size(r))
                c.get(URL_QUIZ)  # 取下一题
    biggest = max(sizes) if sizes else 0
    chk_pending(
        f"5 轮 Dify 点评后 session cookie 不超过 {MAX_COOKIE_SIZE} 字节",
        biggest and biggest <= MAX_COOKIE_SIZE,
        f"实测各轮 Set-Cookie 字节数={sizes}，峰值={biggest}；"
        f"超限后浏览器静默丢弃整个 cookie → 用户被登出、考核进度清零",
    )
    overflow_warns = [w for w in caught if 'cookie is too large' in str(w.message).lower()]
    chk_pending(
        "Werkzeug 未发出 cookie 超限告警",
        not overflow_warns,
        f"捕获 {len(overflow_warns)} 条超限告警：{str(overflow_warns[0].message)[:160]}"
        if overflow_warns else "",
    )
    wipe_quiz()


def test_D1_session_payload_headroom_is_measured():
    """直接测算序列化体积：给出「几轮点评撑爆」的确切拐点，便于定容量方案。"""
    records, breach_at = [], None
    for i in range(1, 6):
        records.append({
            'question': _long_feedback(60, seed=100 + i),
            'answer': '店员作答示例文本，长度贴近真实填写',
            'weak': '通用',
            'is_correct': True,
            'score': 80,
            'feedback': _long_feedback(320, seed=200 + i),
        })
        size = _session_payload_bytes({'quiz_records': records, 'quiz_idx': i + 1,
                                       'quiz_score_sum': 80 * i, '_user_id': '1'})
        if size > MAX_COOKIE_SIZE and breach_at is None:
            breach_at = (i, size)
    chk_pending(
        "5 轮 Dify 点评的 session 序列化体积在 cookie 上限内",
        breach_at is None,
        f"第 {breach_at[0]} 轮即达 {breach_at[1]} 字节，越过 {MAX_COOKIE_SIZE} 上限"
        if breach_at else "",
    )


def test_D1_server_side_session_configured():
    """根因断言：quiz_records 属于服务端状态，不该放进客户端签名 cookie。"""
    chk_pending(
        "已配置服务端 session（SESSION_TYPE / Flask-Session）",
        bool(app.config.get('SESSION_TYPE')),
        "未配置 SESSION_TYPE，考核过程全量落客户端 cookie，容量上限 4093 字节",
    )


# ══════════════════ D2 判分降级留痕污染：AI 挂了仍写 0 分并推进题号 ══════════════════
def test_D2_degraded_grade_not_persisted_as_zero():
    """Dify 判分失败（fail-close 0 分）时，不应把 0 分写进 QuizRecord。"""
    wipe_quiz()
    before = quiz_count()
    with dify_enabled(chat_fail=True):
        c = login('staff')
        c.get(f"{URL_QUIZ}?restart=1")   # 出题：quiz_question 失败会自动回退规则题库
        r = c.post(URL_QUIZ, data={'answer': '先共情，再解释产品价值和用料'})
        chk("降级判分页面仍正常渲染（不 500）", r.status_code == 200, f"code={r.status_code}")
    after = quiz_count()
    rec = latest_quiz()
    chk_pending(
        "AI 判分失败时不写入 0 分留痕",
        after == before,
        f"新增 {after - before} 条；最新留痕 score={getattr(rec, 'score', None)} "
        f"is_correct={getattr(rec, 'is_correct', None)} "
        f"feedback={(getattr(rec, 'ai_feedback', '') or '')[:40]!r}",
    )
    wipe_quiz()


def test_D2_quizrecord_can_distinguish_degraded():
    """schema 层根因：QuizRecord 无任何字段能区分「真答错」与「AI 挂了」。"""
    has_marker = any(hasattr(QuizRecord, f) for f in ('source', 'is_graded', 'graded_by'))
    chk_pending(
        "QuizRecord 具备降级标记字段（source / is_graded / graded_by）",
        has_marker,
        "缺字段 → 0 分留痕不可回溯，考核结果页、培训通过率、薄弱点统计三处同时被污染",
    )


def test_D2_degraded_round_does_not_advance():
    """判分失败等于本题没判成，题号不该推进——否则店员白丢一题。"""
    wipe_quiz()
    with dify_enabled(chat_fail=True):
        c = login('staff')
        c.get(f"{URL_QUIZ}?restart=1")
        with c.session_transaction() as s:
            idx_before = s.get('quiz_idx')
        c.post(URL_QUIZ, data={'answer': '先共情，再解释产品价值和用料'})
        with c.session_transaction() as s:
            idx_after = s.get('quiz_idx')
    chk_pending(
        "AI 判分失败时题号不推进",
        idx_after == idx_before,
        f"题号 {idx_before} → {idx_after}，本题未判分却被消耗",
    )
    wipe_quiz()


def test_D2_downstream_pages_survive_degraded_records():
    """强断言（真实回归）：存在降级留痕时，考核结果页 / 培训页不得报错。"""
    wipe_quiz()
    with dify_enabled(chat_fail=True):
        c = login('staff')
        c.get(f"{URL_QUIZ}?restart=1")
        c.post(URL_QUIZ, data={'answer': '共情并解释价值'})
        rt = c.get(URL_TRAINING)
        chk("含降级留痕时培训页仍 200", rt.status_code == 200, f"code={rt.status_code}")
        a = login('admin')
        ra = a.get(URL_ASSESSMENT)
        chk("含降级留痕时考核结果页仍 200", ra.status_code == 200, f"code={ra.status_code}")
        chk("降级文案不把 [DIFY_ERROR] 标记串透传到页面",
            DIFY_ERROR_PREFIX not in ra.get_data(as_text=True),
            "考核结果页出现原始失败标记串")
    wipe_quiz()


# ══════════════════ D3 半死状态：超时形态 / 熔断 / 部署并发度 ══════════════════
class _CapturingRequests:
    """桩掉 dify_client 模块级 requests，捕获调用参数后立即抛错。"""

    def __init__(self, store):
        self.store = store

    def post(self, url, **kw):
        self.store['timeout'] = kw.get('timeout')
        self.store['url'] = url
        raise RuntimeError('stub: 不发真实请求')


@contextlib.contextmanager
def _capture_requests():
    store = {}
    saved_requests, saved_enabled = dc.requests, dc.dify_client.enabled
    dc.requests = _CapturingRequests(store)
    dc.dify_client.enabled = True
    # 防腐层 _fail() 会 logging.exception 打整段堆栈，这里是预期失败，静音以免污染门禁输出
    dc.logger.disabled = True
    try:
        yield store
    finally:
        dc.logger.disabled = False
        dc.requests = saved_requests
        dc.dify_client.enabled = saved_enabled


def test_D3_timeout_is_connect_read_tuple():
    """DIFY_TIMEOUT 是标量 → requests 对 connect 和 read 各计一次，最坏 2×。"""
    with _capture_requests() as store:
        dc.dify_client._chat('探针', 'probe')
    t = store.get('timeout')
    chk("_chat 确实向 requests 传了 timeout", t is not None, "未传 timeout，可能永久挂起")
    chk_pending(
        "timeout 应为 (connect, read) 二元组",
        isinstance(t, (tuple, list)) and len(t) == 2,
        f"当前为标量 {t!r}：connect 与 read 各独立计时，单次调用最坏耗时 {2 * (t or 0)}s；"
        f"看板一次请求串行发起 2 次 Dify 调用 → 最坏 {4 * (t or 0)}s",
    )


def test_D3_dashboard_circuit_breaks_after_first_failure():
    """看板串行发起 restock_advice + loss_alert；首调失败后应熔断，不再打第二次。"""
    calls = []
    with dify_enabled(completion_fail=True, on_completion=lambda q: calls.append(q)):
        a = login('admin')      # 登录后会 follow_redirects 到看板，先渲染一次
        calls.clear()           # 只统计下面这一次显式请求，避免把登录跳转算进来
        r = a.get(URL_DASHBOARD)
        chk("Dify 全挂时看板仍 200", r.status_code == 200, f"code={r.status_code}")
    chk_pending(
        "首个 Dify 调用失败后熔断，本次请求不再发起后续调用",
        len(calls) <= 1,
        f"单次看板请求实际发起 {len(calls)} 次 completion（restock_advice + loss_alert 串行）；"
        f"半死状态下每次卡满超时 → 单请求耗时 ×{len(calls)}，是并发饥饿的主要放大器",
    )


def test_D3_waitress_thread_count_pinned():
    """部署命令未显式指定 --threads，waitress 默认仅 4 线程。"""
    path = os.path.join(ROOT, '部署', 'README.md')
    if not os.path.isfile(path):
        chk("部署文档存在", False, path)
        return
    with open(path, encoding='utf-8') as f:
        text = f.read()
    lines = [ln.strip() for ln in text.splitlines() if 'waitress-serve' in ln]
    chk("部署文档包含 waitress-serve 启动命令", bool(lines), "未找到启动命令")
    chk_pending(
        "waitress 启动命令显式指定 --threads",
        bool(lines) and any('--threads' in ln for ln in lines),
        f"未指定 → 默认 4 线程；命令：{lines[0][:110] if lines else 'N/A'}。"
        f"答辩现场 5 人同时点看板即排队，第 5 个人开始等前面的 Dify 超时",
    )


# ══════════════════ D4 情景对话留痕缺口 ══════════════════
def test_D4_scenario_chat_creates_no_quiz_record():
    """现状固化：情景对话全程不写 QuizRecord（含 scenario 类型）。"""
    wipe_quiz()
    before = quiz_count()
    with dify_enabled(chat='哎呀这个也太贵了吧，能不能便宜点？'):
        c = login('staff')
        c.get(f"{URL_CHAT}?scenario={DEFAULT_SCENARIO}")
        r = c.post(URL_CHAT, data={'msg': '这款用的是鲜奶和当日茶底，成本确实高一些'})
        chk("情景对话正常渲染", r.status_code == 200, f"code={r.status_code}")
    chk("情景对话不产生任何 QuizRecord（现状）", quiz_count() == before,
        f"新增 {quiz_count() - before} 条")
    chk("库中不存在 quiz_type='scenario' 的留痕（现状）",
        quiz_count(quiz_type='scenario') == 0, "存在 scenario 留痕，与写入路径不符")
    wipe_quiz()


def test_D4_training_scenario_category_consistency():
    """培训页有「情景」分类，但生产路径永不写入 → 该分类恒为 0。

    这是产品口径不一致：要么 chat 落留痕，要么培训页去掉该分类。
    二选一之前保持 pending，不擅自用测试把某一侧固化成规格。
    """
    wipe_quiz()
    with dify_enabled(chat='顾客桩回复'):
        c = login('staff')
        c.get(f"{URL_CHAT}?scenario={DEFAULT_SCENARIO}")
        c.post(URL_CHAT, data={'msg': '这款用的是鲜奶和当日茶底'})
        r = c.get(URL_TRAINING)
    chk("培训页可访问", r.status_code == 200, f"code={r.status_code}")
    scenario_written = quiz_count(quiz_type='scenario') > 0
    page_has_category = '情景' in r.get_data(as_text=True)
    chk_pending(
        "培训页「情景」分类与实际写入路径一致",
        (not page_has_category) or scenario_written,
        "培训页展示「情景」分类但 chat 从不写 scenario 留痕 → 该项恒为 0%，"
        "需产品决策：chat 落留痕，或培训页移除该分类",
    )
    wipe_quiz()


def test_D4_init_db_does_not_seed_scenario_records():
    """init_db 播种 scenario 留痕会让培训页「情景」分类出现假阳性。"""
    path = os.path.join(ROOT, 'init_db.py')
    with open(path, encoding='utf-8') as f:
        lines = f.read().splitlines()
    # 不能只搜 `quiz_type='scenario'`：init_db 是用元组 ('scenario', …) + quiz_type=qt
    # 播种的，字面量搜索会漏（这正是 quality_gate.py 字符串嗅探式判定的通病）。
    # init_db 中 'scenario' 字面量只有播种一处用途，直接搜字面量。
    hits = [f"L{i}" for i, ln in enumerate(lines, 1) if "'scenario'" in ln or '"scenario"' in ln]
    chk_pending(
        "init_db 不播种 quiz_type='scenario' 留痕",
        not hits,
        f"init_db {','.join(hits)} 播种了 scenario 留痕，但生产路径"
        f"（ai.py:92 硬编码 quiz_type='question'）永不写入；"
        f"拿种子库验收会误判「情景分类正常」",
    )


CASES = [
    # D1 会话体积
    test_D1_five_rounds_dify_feedback_cookie_within_limit,
    test_D1_session_payload_headroom_is_measured,
    test_D1_server_side_session_configured,
    # D2 判分降级留痕污染
    test_D2_degraded_grade_not_persisted_as_zero,
    test_D2_quizrecord_can_distinguish_degraded,
    test_D2_degraded_round_does_not_advance,
    test_D2_downstream_pages_survive_degraded_records,
    # D3 半死状态
    test_D3_timeout_is_connect_read_tuple,
    test_D3_dashboard_circuit_breaks_after_first_failure,
    test_D3_waitress_thread_count_pinned,
    # D4 情景对话留痕缺口
    test_D4_scenario_chat_creates_no_quiz_record,
    test_D4_training_scenario_category_consistency,
    test_D4_init_db_does_not_seed_scenario_records,
]

# 无法在进程内自动化、必须人工执行的项（打印提醒，不做假断言）
MANUAL_NOTES = [
    "并发饥饿实测：waitress 起服务后，用 `ab -n 20 -c 8 /admin/dashboard` 或 20 个"
    "并发浏览器标签打看板，同时把 Dify 断网（iptables DROP 或改错 BASE_URL）；"
    "观察第 5 个及以后的请求是否排队等满超时。test_client 无线程池，进程内测不出来。",
    "真机 cookie 溢出终验：用真实浏览器（非 test_client）跑完 5 轮 Dify 考核，"
    "看 DevTools → Application → Cookies 里 session 是否被丢弃、是否被登出。",
]


def _summary():
    total = len(results)
    hard_fail = [r for r in results if not r[1] and not r[2]]
    pending_fail = [r for r in results if not r[1] and r[2]]
    fixed = [r for r in results if r[1] and r[2]]

    print("=" * 74)
    print(f"Dify 接入态场景测试  用例数: {len(CASES)}  检查项: {total}  "
          f"硬失败: {len(hard_fail)}  待修复: {len(pending_fail)}  已修复待收口: {len(fixed)}")
    print("=" * 74)
    for name, ok, pending, extra in results:
        mark = "✅" if ok and not pending else ("🎉" if ok else ("⚠️" if pending else "❌"))
        print(f"  {mark} {name}")
        if not ok and extra:
            print(f"      └─ {extra}")

    if fixed:
        print("\n🎉 以下待修复项现已通过，请把 chk_pending 升级为 chk（防止再次退化）：")
        for name, *_ in fixed:
            print("   -", name)
    if pending_fail:
        print(f"\n⚠️  待修复缺陷 {len(pending_fail)} 项（已知，不阻断门禁）：")
        for name, _ok, _p, extra in pending_fail:
            print(f"   - {name}\n     {extra}")
    if hard_fail:
        print(f"\n❌ 硬失败 {len(hard_fail)} 项（真实回归，必须修）：")
        for name, _ok, _p, extra in hard_fail:
            print(f"   - {name} | {extra}")

    print("\n📋 需人工执行的补充验证：")
    for i, n in enumerate(MANUAL_NOTES, 1):
        print(f"   {i}. {n}")

    return 1 if hard_fail else 0


if __name__ == "__main__":
    for case in CASES:
        run(case)
    sys.exit(_summary())
