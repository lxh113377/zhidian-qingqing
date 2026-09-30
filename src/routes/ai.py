"""模块3 路由 — AI-RAG 员工培训考核（步骤4）

店员侧：问答模式 / 情景对话 / 上岗考核（H5 入口）
管理员侧：考核结果查看（错题 + 薄弱知识点统计）

所有 AI 调用经 src.dify_client，未配置 Dify 时自动走规则版，页面照常可用。
AI 只输出参考建议，是否采纳由老板/店长判断；AI 异常一律降级展示，
绝不把异常串当正常建议渲染。
"""
import json

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required

from src import industry_pack, metrics, negotiation, tenant
from src.agent_trace import run_analysis, run_assistant
from src.analytics import compute_dashboard_stats
from src.decorators import admin_required, staff_or_admin_required, staff_required
from src.dify_client import (
    DIFY_UNAVAILABLE_BADGE,
    DIFY_UNAVAILABLE_TEXT,
    dify_client,
    scenario_catalog,
)
from src.intent_rules import match_intent
from src.knowledge_base import WEAK_KEYWORDS, mark_useful, weak_domain_counts
from src.learner_model import adaptive_difficulty, mastery_profile
from src.models import QuizRecord, Staff, db
from src.rate_limit import check_limit

ai_bp = Blueprint('ai', __name__, url_prefix='/ai')

QUIZ_TOTAL = 5  # 每次考核题数

# 薄弱定向补训的知识域白名单兜底（F11：?topic 入参校验唯一真相源，非法一律回退全题库）
# P1：运行时以当前行业包域表为准（见 _valid_topics()），此处保留内嵌兜底。
QUIZ_TOPICS = ('原料规范', '异议处理', '套餐价格', '服务话术')


def _valid_topics() -> tuple:
    """当前行业包允许定向补训的知识域（包域表缺失时回退内嵌白名单）。"""
    return tuple(industry_pack.pack_domains() or QUIZ_TOPICS)

# 一轮考核的全部 session 键（restart 时需整组清空，避免残留串题）
QUIZ_SESSION_KEYS = (
    'quiz_idx', 'quiz_score_sum', 'quiz_records',
    'quiz_q', 'quiz_ref', 'quiz_weak',
)

# ── 会话 Cookie 预算（三重收口：行数 + 单行字节 + 总量字节）──
# Flask 默认会话是客户端签名 Cookie（werkzeug 在 4093 字节告警，超限浏览器
# 可能整体丢弃）：后端读不到 session 表现为登录态静默丢失、页面跳回登录页。
# 08-25 首修（行数）、09-09 补修（单行长度），但两版都按「字符数」收口——
# 而会话序列化是 json.dumps(ensure_ascii=True) + urlsafe base64 两级膨胀，
# 一个中文字符 ≈ 8 个 Cookie 字节（\uXXXX 6 字节 → base64 ×4/3）。
# 09-10 快照构建实测：DeepSeek 长回复把 session 撑到 8024 字节（超限 2 倍），
# 证明字符口径对中文场景无效。以下常量一律改为「Cookie 字节成本」口径。
MAX_CHAT_HISTORY = 12           # 行数硬上限（防无界累加，支持无限轮次练习）
MAX_HISTORY_LINE_BYTES = 600    # 单行 Cookie 成本上限（≈200 个中文字符）
MAX_HISTORY_BYTES = 1500        # 单个历史键总成本预算（超预算从最旧行丢弃）
MAX_SESSION_COOKIE_BYTES = 3500  # PF-11：整个序列化会话的 Cookie 字节预算
# quiz_records 不再往 session 塞全文（09-10 探针实测：Dify 长题干+点评
# 单轮即 +3020B，5 轮 15090B，是 Cookie 溢出第一元凶）。改为「ID 留痕」：
# session 只存 {id, score, is_correct, weak}（≈80B/条），题干/作答/点评全文
# 在 QuizRecord 库里，结果页按 ID 读回——与 test_dify_integration D1 的
# 根因结论一致（服务端状态不该进客户端签名 Cookie），且零新依赖。


def _cookie_cost(text: str) -> int:
    """估算一段文本写入签名 Cookie 后占用的字节数。

    口径与 Flask 会话序列化一致：json.dumps（默认 ensure_ascii=True，
    非 ASCII 转 \\uXXXX）→ urlsafe base64（×4/3 膨胀）。直接对整串取
    json 长度再乘 4/3 上取整，比逐字符分支更准、且无需随编码规则维护。

    Args:
        text: 待估算文本（None 按空串计）。

    Returns:
        int: 预估 Cookie 字节数（含 json 引号开销，略保守）。
    """
    raw = len(json.dumps(text or ''))
    return (raw * 4 + 2) // 3


def _cap_line(text: str, budget_bytes: int = MAX_HISTORY_LINE_BYTES) -> str:
    """单行历史截断：按 Cookie 字节成本截断，超长以省略号收尾。

    截断只影响 session 内的历史留痕与整页回显——AJAX 主路径本次回复
    完整返回，店员实际看到的内容不受损。

    Args:
        text: 原始文本（允许 None）。
        budget_bytes: 该行的 Cookie 成本上限（默认聊天历史口径，quiz 记录传更紧值）。

    Returns:
        str: 成本 ≤ budget_bytes 的文本。
    """
    text = text or ''
    if _cookie_cost(text) <= budget_bytes:
        return text
    budget = budget_bytes - _cookie_cost('…')
    # 成本随前缀长度单调递增：先按实测成本比例取近似切片，再线性收缩微调
    cut = text[:max(1, int(len(text) * budget / _cookie_cost(text)))]
    while len(cut) > 1 and _cookie_cost(cut) > budget:
        cut = cut[:-max(1, len(cut) // 10)]
    return cut + '…'


def _fit_history(history: list) -> list:
    """把对话历史压回 Cookie 预算：行数、单行字节、总量字节三重收口。

    总量超预算时从最旧行开始丢弃（保留最近上下文）；至少保留 1 行——
    单行已由 _cap_line 限长，留 1 行必定不超单行上限。

    Args:
        history: 历史行列表（允许 None）。

    Returns:
        list: 满足三重预算的新列表。
    """
    kept = [_cap_line(h) for h in (history or [])][-MAX_CHAT_HISTORY:]
    total = sum(_cookie_cost(h) for h in kept)
    while len(kept) > 1 and total > MAX_HISTORY_BYTES:
        total -= _cookie_cost(kept.pop(0))
    return kept


def _append_history(session_key: str, lines: list):
    """向 session 中的对话历史追加若干行（问/答），统一走三重预算收口。"""
    history = session.get(session_key, [])
    session[session_key] = _fit_history(list(history) + list(lines))


def _session_cookie_bytes() -> int:
    """当前会话按 Flask 实际签名序列化后的 Cookie 字节数。"""
    serializer = current_app.session_interface.get_signing_serializer(current_app)
    return len(serializer.dumps(dict(session))) if serializer else 0


def _enforce_session_budget():  # noqa: PLR0912
    """整会话 Cookie 总预算（PF-11）。

    三个历史键各自的 1500B 预算叠加后仍可能越过 4093B。本函数按实际签名
    序列化结果收口：先按成本从最大的历史键丢最旧行，再压缩考核题面，最后
    丢弃可重建的 conversation_id / 临时题面；登录身份与 CSRF 键永不删除。
    """
    history_keys = ('chat_history', 'trainer_history', 'assistant_history')
    optional_keys = (
        'chat_cid', 'trainer_cid', 'assistant_cid',
        'quiz_ref', 'quiz_q', 'quiz_weak', 'quiz_topic',
    )
    try:
        if _session_cookie_bytes() <= MAX_SESSION_COOKIE_BYTES:
            return

        while _session_cookie_bytes() > MAX_SESSION_COOKIE_BYTES:
            costs = []
            for key in history_keys:
                rows = session.get(key) or []
                if rows:
                    costs.append((sum(_cookie_cost(x) for x in rows), key))
            if not costs:
                break
            _cost, key = max(costs)
            session[key] = list(session.get(key) or [])[1:]

        if _session_cookie_bytes() > MAX_SESSION_COOKIE_BYTES:
            if session.get('quiz_q'):
                session['quiz_q'] = _cap_line(session['quiz_q'], 400)
            if session.get('quiz_ref'):
                session['quiz_ref'] = _cap_line(session['quiz_ref'], 900)

        for key in optional_keys:
            if _session_cookie_bytes() <= MAX_SESSION_COOKIE_BYTES:
                break
            session.pop(key, None)

        if _session_cookie_bytes() > MAX_SESSION_COOKIE_BYTES:
            for key in history_keys:
                session[key] = []
    except Exception as exc:
        # 预算收口属保护层，序列化异常不得影响主响应；但根因必须可观测（Q2）。
        try:
            current_app.logger.warning('session budget enforce skipped: %s', exc)
        except Exception:  # noqa: S110  理由：会话预算 enforcement 失败不得影响 AI 应答（上面已记 warning），轨迹与埋点属尽力而为
            pass
        return


@ai_bp.after_request
def _apply_session_budget(response):
    """所有 AI 页面/接口返回前统一执行整会话 Cookie 预算。"""
    _enforce_session_budget()
    return response


def _reset_quiz_session():
    """清空一轮考核的全部 session 状态。"""
    for key in QUIZ_SESSION_KEYS:
        session.pop(key, None)


def _load_quiz_records(light_records: list) -> list:
    """按 session 轻量留痕中的 ID 从 DB 读回完整作答（结果页回顾用）。

    只取当前店员的记录（防越权读取他人作答）；DB 中已不存在的 ID 跳过。
    返回 dict 列表，键名与模板 ai_quiz.html 消费口径一致。

    Args:
        light_records: [{'id', 'score', 'is_correct', 'weak'}, ...]。

    Returns:
        list[dict]: 含 question/answer/feedback 全文的记录列表（按考核顺序）。
    """
    ids = [r['id'] for r in light_records if r.get('id') is not None]
    if not ids:
        return []
    by_id = {r.id: r for r in QuizRecord.query.filter(
        QuizRecord.id.in_(ids), QuizRecord.staff_id == current_user.id).all()}
    out = []
    for r in light_records:
        rec = by_id.get(r.get('id'))
        if rec is None:
            continue
        out.append({'question': rec.question, 'answer': rec.staff_answer,
                    'weak': r.get('weak', '通用'), 'is_correct': rec.is_correct,
                    'score': r.get('score'), 'feedback': rec.ai_feedback})
    return out


def _current_difficulty(topic=None) -> str:
    """按学习者模型实时推导本次考核的出题难度（第四轮 D1）。

    掌握度高的店员拿到进阶题面（要求答出例外），薄弱者降阶只答要点；
    无答题记录时默认 standard，行为与旧版一致（零回归）。

    Args:
        topic: 可选的定向补训知识域。

    Returns:
        str: 'basic' / 'standard' / 'advanced'。
    """
    recs = QuizRecord.query.filter_by(staff_id=current_user.id).all()
    return adaptive_difficulty(mastery_profile(recs), topic or '')


def _answer_assessment_query(question: str):
    """管理员考核问询（谁考得好/考核成绩/通过率）。

    防腐层约定：DifyClient 不查 DB，故考核统计在此路由层用 ORM 查后直答，
    全部来自真实 QuizRecord 数据。命中考核类关键词返回文案，否则返回 None。

    P7-E3-B3 / M-11：考核词表走 `intent_rules` 单一真相源，与轨迹判定同源。
    """
    if not match_intent(question, 'assessment'):
        return None
    # 越权修复（2026-09-21）：只统计当前账号可见商家 + 门店范围内的店员与考核
    # 记录，禁止全表统计后把全平台店员账号名与成绩回显给提问者。
    store_ids = tenant.visible_store_ids(tenant.current_admin())
    staffs = Staff.query.filter(
        tenant.merchant_scope_clause(Staff.merchant_id),
        tenant.store_visible_clause(Staff.store_id, store_ids),
    ).all()
    if not staffs:
        return '当前账号没有可见的门店范围，无法统计考核成绩。'
    # F16：消除 N+1——单次全量查询后按 staff_id 分组，而非每店员各查一次库
    # （与同文件 assessment() 的既有模式对齐；固定 2 次查询，O(N+M)）。
    recs_by_staff = {}
    for _r in QuizRecord.query.filter(
            tenant.in_clause(QuizRecord.staff_id, [s.id for s in staffs])).all():
        recs_by_staff.setdefault(_r.staff_id, []).append(_r)
    recs_by = {}
    for s in staffs:
        q_recs = recs_by_staff.get(s.id)
        if not q_recs:
            continue
        scores = [r.score for r in q_recs if r.score is not None]
        avg = (sum(scores) / len(scores)) if scores else 0
        pass_n = sum(1 for r in q_recs if r.is_correct is True)
        recs_by[s.username] = (round(avg, 1), pass_n, len(q_recs))
    if not recs_by:
        return '暂无可统计的考核记录，店员完成问答考核后即可查看。'
    ranking = sorted(recs_by.items(), key=lambda x: -x[1][0])
    lines = [f'全员考核成绩（共 {len(ranking)} 名店员）：']
    for name, (avg, pn, total) in ranking:
        lines.append(f'· {name}：平均 {avg} 分，答对 {pn}/{total} 题')
    return '\n'.join(lines)


# 多轮承接词（助手对话消解用）：问句过短且以此开头 → 结合上一问改写
FOLLOWUP_PREFIXES = ('那', '再', '还有', '然后', '它', '这个', '那个', '该')


def _personal_ctx():
    """组装当前店员的学习画像（只读 QuizRecord 聚合，供 AI 培训助手个性化）。

    Returns:
        dict: {'weak_labels': list[str], 'wrong_count': int,
               'recent_wrong_questions': list[str],
               'mastery_summary': str}，无错题时 weak_labels 为空。
        mastery_summary（第六轮 B）：整体掌握度 + 最弱域一句话画像，
        把培训助手从「你错了 N 题」升级为「你当前掌握多少」。
    """
    recs = QuizRecord.query.filter_by(staff_id=current_user.id).order_by(
        QuizRecord.created_at.desc()).all()
    wrong_qs = []
    wrong_count = 0
    for r in recs:
        if r.is_correct is not False:
            continue
        wrong_count += 1
        if len(wrong_qs) < 3:
            wrong_qs.append(r.question)
    # 薄弱域归因：共用 knowledge_base.weak_domain_counts（F18 口径唯一）
    domain_count = weak_domain_counts(r.question for r in recs if r.is_correct is False)
    weak_labels = [k for k, _ in sorted(domain_count.items(), key=lambda x: -x[1])]
    # 掌握度画像（第六轮 B）：时间衰减 + 难度加权的真实水平
    profile = mastery_profile(recs)
    mastery_summary = ''
    if profile.get('has_data'):
        scored = [d for d in profile['domains'] if d['sample'] > 0]
        weakest = min(scored, key=lambda d: d['mastery'], default=None)
        mastery_summary = (f"整体掌握度 {profile['overall']} 分"
                           f"（{profile['overall_level']}）"
                           + (f"，最薄弱：「{weakest['domain']}」"
                              f"{weakest['mastery']} 分（{weakest['level']}）"
                              if weakest else ''))
    return {'weak_labels': weak_labels, 'wrong_count': wrong_count,
            'recent_wrong_questions': wrong_qs,
            'mastery_summary': mastery_summary}


def _last_question(history) -> str:
    """取出会话历史里最近一条「问：」内容（多轮承接消解用）。"""
    for line in reversed(history or []):
        if line.startswith('问：'):
            return line[2:]
    return ''


def _followup_disambiguate(prev_q: str, question: str) -> str:
    """规则版多轮承接消解：问句过短且以承接词开头时，结合上一问改写后路由。

    仅改写路由输入，不影响答而复述历史，回答本身不回显改写内容。
    """
    q = (question or '').strip()
    if not q:
        return q
    if prev_q and len(q) <= 8 and q.startswith(FOLLOWUP_PREFIXES):
        return f'结合上一问「{prev_q}」，{q}'
    return q


# ───────────────────────── 店员侧 ─────────────────────────
@ai_bp.route('/')
@login_required
@staff_or_admin_required
def hub():
    """AI 培训主页：三种模式入口。

    店员与管理员均可进入（管理员需查看培训内容），
    但必须是已登录的门店员工角色（F6）。
    """
    return render_template('h5/ai.html')


@ai_bp.route('/quiz', methods=['GET', 'POST'])
@login_required
@staff_required
def quiz():
    """问答模式 / 上岗考核（出题→作答→点评→判定）"""
    # 开始新的一轮
    if request.method == 'GET' and request.args.get('restart'):
        _reset_quiz_session()
        # 薄弱定向练习：?topic=知识域全名，白名单校验（非法一律回退普通模式，防注入）
        topic = request.args.get('topic')
        if topic:
            session['quiz_topic'] = topic if topic in _valid_topics() else None
        else:
            session.pop('quiz_topic', None)
        # 自适应难度：开轮时按学习者模型掌握度定档，本轮保持一致
        session['quiz_difficulty'] = _current_difficulty(session.get('quiz_topic'))

    idx = session.get('quiz_idx', 1)
    score_sum = session.get('quiz_score_sum', 0)
    records = session.get('quiz_records', [])

    # 提交答案 → 点评
    if request.method == 'POST' and request.form.get('answer') is not None:
        # P7-E / S-5：DEFAULT_LIMITS 里早就为考核预留了 'quiz': (60, 30)，
        # 但全仓从未挂载 —— 这是唯一「店员可无限次写库 + 触发在线 AI 计费」的入口。
        limited = check_limit(action='quiz')
        if limited is not None:
            return limited
        q = (session.get('quiz_q') or '').strip()
        ref = session.get('quiz_ref') or ''
        weak = session.get('quiz_weak', '通用')
        answer = request.form.get('answer', '').strip()

        # 题干为空说明会话过期/被清理，若继续入库会命中 question NOT NULL 约束报 500（F12）
        if not q:
            _reset_quiz_session()
            flash('本次考核会话已失效，请重新开始答题', 'warning')
            return redirect(url_for('ai.quiz', restart='1'))

        if not answer:
            flash('请先填写你的作答', 'warning')
            return redirect(url_for('ai.quiz'))

        g = dify_client.grade_answer(q, ref, answer)
        score_sum += g['score']
        # 保存本题到数据库（考核留痕，全文只存 DB）
        rec = QuizRecord(
            staff_id=current_user.id, quiz_type='question',
            question=q, staff_answer=answer, ai_feedback=g['feedback'],
            is_correct=g['is_correct'], score=g['score'],
        )
        db.session.add(rec)
        db.session.commit()
        # session 只存轻量留痕（id + 判分标量，≈80B/条）：题干/作答/点评全文
        # 在 QuizRecord 库里，结果页按 ID 读回。旧版把 5 轮全文塞进签名
        # Cookie，Dify 长点评下第 5 轮实测 4722B 越过 4093 上限（浏览器
        # 静默丢弃整个 cookie → 被登出、考核进度清零）。
        records.append({'id': rec.id, 'score': g['score'],
                        'is_correct': g['is_correct'], 'weak': weak})
        session['quiz_records'] = records
        session['quiz_score_sum'] = score_sum
        session['quiz_idx'] = idx + 1
        return render_template('h5/ai_quiz.html', mode='feedback',
                               idx=idx, total=QUIZ_TOTAL, grade=g,
                               question=q, reference=ref,
                               has_next=idx < QUIZ_TOTAL)

    # 一轮结束 → 判定
    if idx > QUIZ_TOTAL:
        verdict = dify_client.assessment_verdict(records)
        # 结果页「本次作答回顾」需要全文：按 session 里的 ID 从 DB 读回
        # （限定当前店员，防越权读取他人作答）
        full_records = _load_quiz_records(records)
        return render_template('h5/ai_quiz.html', mode='result',
                               total=QUIZ_TOTAL, verdict=verdict, records=full_records,
                               quiz_topic=session.get('quiz_topic'))

    # 出下一题（薄弱定向模式下按知识域出题；难度按学习者模型自适应）
    difficulty = session.get('quiz_difficulty')
    if difficulty not in ('basic', 'standard', 'advanced'):
        difficulty = _current_difficulty(session.get('quiz_topic'))
        session['quiz_difficulty'] = difficulty
    q = dify_client.quiz_question(idx, QUIZ_TOTAL, topic=session.get('quiz_topic'),
                                  difficulty=difficulty)
    session['quiz_idx'] = idx
    session['quiz_q'] = q['question']
    session['quiz_ref'] = q.get('reference', '')
    session['quiz_weak'] = q.get('weak', '通用')
    return render_template('h5/ai_quiz.html', mode='ask',
                           idx=idx, total=QUIZ_TOTAL, question=q['question'],
                           source=q['source'])


@ai_bp.route('/chat', methods=['GET', 'POST'])
@login_required
@staff_required
def chat():
    """情景对话模式（AI 扮顾客：砍价/担心卫生/挑剔服务）。

    scenario 直接进入 prompt，必须走白名单校验，
    防止任意文本注入大模型指令（F11）。
    """
    catalog = scenario_catalog()
    requested = request.args.get('scenario')
    if requested is not None and requested not in catalog['types']:
        flash('不支持的情景类型，已为你切换到默认情景', 'warning')
        return redirect(url_for('ai.chat', scenario=catalog['default']))

    scenario = requested or session.get('scenario') or catalog['default']
    if scenario not in catalog['types']:  # session 中的历史脏值同样兜底
        scenario = catalog['default']
    session['scenario'] = scenario

    if request.method == 'POST':
        # 接口限流：情景对话 30 次 / 60s（get/post 混合路由，仅 POST 分支检查）
        limited = check_limit(action='chat')
        if limited is not None:
            return limited
        action = request.form.get('action')
        history = session.get('chat_history', [])
        cid = session.get('chat_cid')

        if action == 'finish':
            # 结束点评：enabled 判断收敛在 dify_client 内部（F16），路由不再重复判断
            tip = dify_client.finish_scenario_tip(history)
            # P5 谈判复盘：成交/流失 + 让利幅度 + 四维评分（纯函数，离线可跑）
            review = negotiation.review(negotiation.replay(history, scenario))
            return render_template('h5/ai_chat.html', scenario=scenario,
                                   scenario_labels=catalog['labels'],
                                   history=history, finished=True, tip=tip,
                                   review=review,
                                   mood=dify_client.customer_state(history))

        user_msg = request.form.get('msg', '').strip()
        mood = None
        reply_full = ''
        panel = None
        if user_msg:
            history.append(f'店员：{user_msg}')
            r = dify_client.scenario_reply(scenario, user_msg, history, cid)
            history.append(f'顾客：{r["reply"]}')
            reply_full = r['reply']
            session['chat_cid'] = r.get('conversation_id')
            # 顾客情绪状态机（第五轮）：规则版返回 mood/patience，Dify 版无则置空
            if r.get('mood'):
                mood = {'mood': r.get('mood'), 'label': r.get('mood_label', ''),
                        'patience': r.get('patience', 0)}
            # 谈判面板（P5）：心理价区间/当前谈价/让步进度，规则版与在线版均产出
            panel = r.get('negotiation')
        # 三重 Cookie 预算收口（行数/单行字节/总量字节，见常量注释）：
        # 只压缩 session 留痕，AJAX 分支下方仍返回本轮完整回复。
        history = _fit_history(history)
        session['chat_history'] = history

        # AJAX 情景对话：前端 fetch 携带 fetch 标记，返回 JSON 供无刷新渲染
        if request.headers.get('X-Requested-With') == 'fetch':
            ok = bool(user_msg and reply_full)
            tip = dify_client.scenario_instant_tip(user_msg) if user_msg else ''
            return jsonify(ok=ok, reply=(reply_full if ok else '输入为空或对话已重置，请重试'),
                           tip=tip, mood=mood, negotiation=panel)

        return render_template('h5/ai_chat.html', scenario=scenario,
                               scenario_labels=catalog['labels'], history=history,
                               mood=mood, negotiation=panel, review=None)

    # GET：重置并开始
    session['chat_history'] = []
    session['chat_cid'] = None
    return render_template('h5/ai_chat.html', scenario=scenario,
                           scenario_labels=catalog['labels'], history=[], mood=None,
                           negotiation=None, review=None)


@ai_bp.route('/trainer', methods=['GET', 'POST'])
@login_required
@staff_required
def trainer():
    """店员 AI 培训助手：业务/知识问答 + 个人错题针对性提示（聊天式，第三轮 M1）。

    - GET：进入对话页（重置历史，展示个人薄弱点徽章）
    - POST AJAX：`ask` 字段 → 规则版意图路由/知识库 + 个人画像个性化（无刷新渲染）

    个性化画像只读当前店员 QuizRecord 聚合，AI 侧只消费不查库。
    """
    history = session.get('trainer_history', [])
    if request.method == 'POST':
        # 接口限流：培训助手 30 次 / 60s（防刷，与情景对话同口径）
        limited = check_limit(action='chat')
        if limited is not None:
            return limited
        question = request.form.get('ask', '').strip()
        if not question:
            return jsonify(ok=False, reply='请输入你的问题。')
        personal = _personal_ctx()
        r = dify_client.trainer_ask(
            question, compute_dashboard_stats(), personal_ctx=personal,
            conversation_id=session.get('trainer_cid'))
        session['trainer_cid'] = r.get('conversation_id')
        # 三重 Cookie 预算收口：AJAX 分支下方仍返回本轮完整回复
        _append_history('trainer_history', [f'问：{question}', f'答：{r["reply"]}'])
        history = session.get('trainer_history', [])
        refs = r.get('refs') or []
        if request.headers.get('X-Requested-With') == 'fetch':
            return jsonify(ok=not r['source'].startswith('dify-error'),
                           reply=r['reply'], refs=refs)
        return render_template('h5/ai_trainer.html', history=history,
                               personal=personal, refs=refs,
                               labels=metrics.dashboard_labels(),
                               degraded_text=DIFY_UNAVAILABLE_TEXT)
    # GET：重置历史并开始
    session['trainer_history'] = []
    session['trainer_cid'] = None
    return render_template('h5/ai_trainer.html', history=[],
                           personal=_personal_ctx(), refs=[],
                           labels=metrics.dashboard_labels(),
                           degraded_text=DIFY_UNAVAILABLE_TEXT)


@ai_bp.route('/kb-feedback', methods=['POST'])
@login_required
@staff_or_admin_required
def kb_feedback():
    """知识库检索反馈（第四轮 D4）：把「有用/没用」回写为检索权重。

    仅接受知识域 + 条目标题两个字段（均需非空），非法输入直接拒绝，
    不做任何 DB 写入；反馈落轻量 JSON，刷新后影响后续检索排序。
    """
    limited = check_limit(action='chat')
    if limited is not None:
        return limited
    domain = request.form.get('domain', '').strip()
    title = request.form.get('title', '').strip()
    if not domain or not title:
        return jsonify(ok=False, msg='缺少知识条目标识')
    # P7-E / S-12：反馈键此前无白名单也无长度上限 —— 键空间开放 + 文件无界增长。
    # domain 必须命中当前包域表，title 限长 100（与后台 MAX_FIELD_LEN 同族口径）。
    if domain not in set(industry_pack.pack_domains()):
        return jsonify(ok=False, msg='知识域不合法')
    if len(title) > 100:
        return jsonify(ok=False, msg='知识条目标识过长')
    useful = request.form.get('useful', '1') not in ('0', 'false', 'no')
    res = mark_useful(domain, title, useful)
    return jsonify(ok=res['ok'], weight=res['weight'])


# ───────────────────────── 管理员侧 ─────────────────────────
@ai_bp.route('/admin/assessment')
@login_required
@admin_required
def assessment():
    """考核结果查看：每人错题 + 薄弱知识点统计（AC-OBS-14）"""
    # 越权修复（2026-09-21）：只统计本商家可见门店内的店员及其考核记录。
    store_ids = tenant.visible_store_ids(tenant.current_admin())
    staffs = Staff.query.filter(
        tenant.merchant_scope_clause(Staff.merchant_id),
        tenant.store_visible_clause(Staff.store_id, store_ids),
    ).order_by(Staff.created_at.desc()).all()
    # F16：消除 N+1——单次全量查询后按 staff_id 分组，而非每店员各查一次库
    recs_all = QuizRecord.query.filter(
        tenant.in_clause(QuizRecord.staff_id, [s.id for s in staffs]),
    ).order_by(QuizRecord.created_at.desc()).all()
    recs_by_staff = {}
    for _r in recs_all:
        recs_by_staff.setdefault(_r.staff_id, []).append(_r)
    result = []
    # P7-E3-B / M-2：归因词表随当前行业包（此前恒用奶茶内嵌词表 → 切包后
    # 本页与店员侧归因口径不一致，用户可见）；包缺失时回退 knowledge_base 兜底。
    weak_kw = industry_pack.pack_weak_keywords() or WEAK_KEYWORDS
    for s in staffs:
        recs = recs_by_staff.get(s.id, [])[:20]
        wrong = [r for r in recs if not r.is_correct]
        weak_map = {}
        for r in wrong:
            tag = '通用'
            for kw in weak_kw:
                if kw in (r.question or ''):
                    tag = kw
                    break
            weak_map[tag] = weak_map.get(tag, 0) + 1
        scored = [float(r.score) for r in recs if r.score is not None]
        avg = sum(scored) / len(scored) if scored else 0
        result.append({
            'staff': s, 'total': len(recs), 'wrong': len(wrong),
            'avg': round(avg, 1),
            'weak': sorted(weak_map.items(), key=lambda x: -x[1]),
            'latest': recs[0] if recs else None,
        })
    return render_template('admin/assessment.html', result=result)


@ai_bp.route('/admin/assessment/<int:staff_id>')
@login_required
@admin_required
def assessment_detail(staff_id):
    """单个店员考核明细"""
    staff = db.get_or_404(Staff, staff_id)
    # IDOR 收口（2026-09-21）：改 URL 里的 staff_id 也读不到本商家之外的人。
    if not tenant.staff_visible(staff):
        abort(403)
    recs = QuizRecord.query.filter_by(staff_id=staff_id).order_by(
        QuizRecord.created_at.desc()).all()
    return render_template('admin/assessment_detail.html', staff=staff, recs=recs)


# ───────────────────────── 新增：管理员 AI 经营分析 ─────────────────────────
@ai_bp.route('/admin/business-analysis')
@login_required
@admin_required
def business_analysis():
    """AI 经营分析（管理员侧）：营收/趋势/结构/损耗 综合报告。

    Dify 版由大模型生成深度洞察，规则版基于真实 stats 数据模板化生成；
    结果只作参考决策建议（决策红线），异常一律降级展示。

    P6：整条分析过程经 `agent_trace` 流水线产出决策轨迹（统计 → 各路 AI 智能
    工具 → 结论 → 依据汇总），与结论一同渲染；规则版同样产出轨迹（离线可演示）。
    """
    stats = compute_dashboard_stats()
    analysis, trace = run_analysis(stats, dify_client.business_analysis)
    return render_template('admin/business_analysis.html',
                           stats=stats,
                           analysis=analysis,
                           trace=trace,
                           dify_enabled=dify_client.enabled,
                           labels=metrics.dashboard_labels(),  # P7-B3：文案随包
                           # P7-E3-B2 / M-12：降级文案单入口（模板不再内联副本）
                           degraded_text=DIFY_UNAVAILABLE_TEXT,
                           degraded_badge=DIFY_UNAVAILABLE_BADGE)


# ───────────────────────── 新增：管理员 AI 智能助手 ─────────────────────────
@ai_bp.route('/admin/assistant', methods=['GET', 'POST'])
@login_required
@admin_required
def assistant():
    """AI 智能助手（管理员侧业务问答）：营收/销量/毛利/损耗/备货/知识库。

    - GET：进入对话页（历史放 session，跨页面保持最近几轮）
    - POST AJAX：`ask` 字段 → 规则版/知识库/Dify 回答（无刷新渲染）

    P6：回答过程经 `agent_trace` 流水线产出决策轨迹（意图 → 工具 → 结论），
    随本次响应渲染时间线；轨迹只活在当次请求，**不进 session**（Cookie 预算）。
    """
    history = session.get('assistant_history', [])
    if request.method == 'POST':
        # 接口限流：智能助手 30 次 / 60s（管理员高频问答防刷）
        limited = check_limit(action='chat')
        if limited is not None:
            return limited
        question = request.form.get('ask', '').strip()
        if not question:
            return jsonify(ok=False, reply='请输入你的问题。')
        # 规则版多轮承接消解：过短的承接问句结合上一问改写后再路由（第三轮 M2）
        q_for_ask = _followup_disambiguate(_last_question(history), question)

        def _conclude(q, stats):
            """结论步骤：考核问询走真实 QuizRecord 直答，其余交 AI 后端。"""
            # 考核问询（谁考得好/考核成绩/通过率）→ 路由层查真实 QuizRecord 直答
            assess_ans = _answer_assessment_query(q)
            if assess_ans:
                return {'source': 'rule', 'reply': assess_ans}
            r = dify_client.assistant_ask(q, stats,
                                          conversation_id=session.get('assistant_cid'))
            session['assistant_cid'] = r.get('conversation_id')
            return r

        r, trace = run_assistant(q_for_ask, None, _conclude)
        _append_history('assistant_history',
                        [f'问：{question}', f'答：{r["reply"]}'])
        history = session.get('assistant_history', [])
        if request.headers.get('X-Requested-With') == 'fetch':
            return jsonify(
                ok=not r['source'].startswith('dify-error'),
                reply=r['reply'],
                trace_html=render_template('admin/_agent_trace.html',
                                           trace=trace, standalone=True))
        return render_template('admin/assistant.html', history=history, trace=trace,
                               labels=metrics.dashboard_labels(),
                               degraded_text=DIFY_UNAVAILABLE_TEXT)
    # GET：重置历史并开始
    session['assistant_history'] = []
    return render_template('admin/assistant.html', history=[],
                           labels=metrics.dashboard_labels(),
                           degraded_text=DIFY_UNAVAILABLE_TEXT)
