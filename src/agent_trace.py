"""AI 决策轨迹（通用化改造 P6）——把「AI 怎么得出结论」变成可展示的推理链路

对外暴露一条显式流水线：意图解析 → 工具调用（真实数据 / 知识库 / 预测 / 归因）
→ 证据汇总 → 结论。每一步落 `{step,name,input_summary,output_summary,latency_ms,
source,ok}`，供管理端渲染时间线（来源徽章区分「大模型生成/规则版」），
同时追加一行 JSONL 到 `data/agent_trace.jsonl` 作为参赛自证材料。

设计约束（与既有 `ai_trace` 同范式，避免技术债）：
  1. **失败静默**：落盘失败一律吞掉（OSError），绝不影响业务主流程；
     单个步骤抛异常也只标记 ok=False，流水线继续走完（不伪造输出）。
  2. **不进 Cookie**：轨迹只在当次请求内渲染，不写入 session（签名 Cookie ~4KB）。
  3. **零第三方依赖、不改既有签名**：工具只读既有模块的产出，不新增查询范式；
     统计沿用路由层已算好的 `stats`（单次全量聚合），不重复落库。
  4. **懒加载依赖**：`analytics`/`dify_client`/`knowledge_base` 在函数内导入，
     避免与防腐层形成循环导入，也保证本模块可单独单测。
"""
import json
import logging
import os
import threading
import time

# P7-E3-B3 / M-11：意图词表收敛到 `src/intent_rules.py` 单一真相源
# （路由分支、轨迹、考核问询三处共用；此处 re-export 保持既有引用名可用）。
from src.intent_rules import (
    INTENT_EXCLUDES,
    INTENT_LABELS,
    INTENT_RULES,
    first_keyword,
    match_intent,
)

__all__ = [
    'INTENT_EXCLUDES', 'INTENT_LABELS', 'INTENT_RULES',  # re-export（M-11 单一真相源）
    'TraceRun', 'classify_intent', 'first_keyword', 'match_intent',
    'run_analysis', 'run_assistant', 'source_label',
]

# 与 src 同级项目根下的 data/（开发 SQLite 同目录；已 gitignore）
TRACE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'agent_trace.jsonl')

# PF-6：并发追加加锁，按大小轮转，避免磁盘无界增长与 Windows 多进程静默丢写。
TRACE_MAX_BYTES = 2 * 1024 * 1024
TRACE_BACKUPS = 3
_TRACE_LOCK = threading.Lock()
logger = logging.getLogger(__name__)

# 单步摘要上限：既要能在时间线里一眼读完，又不能把 JSONL 撑爆
SUMMARY_MAX = 160

# 来源 → 徽章文案（规则版与在线版共用一套，「离线可演示」是硬要求）
SOURCE_LABELS = {
    'deepseek': '大模型生成',
    'dify': '大模型生成',
    'rule': '规则版',
    'dify-error': '回退规则版',
    'tool': '工具调用',
    'local': '本地计算',
}
DEFAULT_SOURCE = 'local'


def _cap(text, limit=SUMMARY_MAX):
    """截断摘要，超长补省略号（None → 空串）。"""
    s = '' if text is None else str(text)
    s = ' '.join(s.split())          # 折叠换行/多空格，时间线单行展示
    return s if len(s) <= limit else s[:limit - 1] + '…'


def source_label(source):
    """来源 → 徽章文案（未知来源按规则版处理，不臆造「大模型生成」）。"""
    return SOURCE_LABELS.get(source or '', SOURCE_LABELS['rule'])


def _auto_summary(value):
    """未显式给出摘要时按类型兜底生成（dict 取结论正文，list 取条数）。"""
    if isinstance(value, dict):
        return value.get('reply') or value.get('content') or f'{len(value)} 项字段'
    if isinstance(value, (list, tuple)):
        return f'{len(value)} 项'
    return value


def classify_intent(question):
    """判定管理员问句的意图（轨迹专用，与规则版路由分支同源同序）。

    Args:
        question: 管理员提问文本。

    Returns:
        tuple: (intent, detail)；detail 为命中的关键词/产品名，未命中为空串。
    """
    from src import dify_client  # 懒加载：避免与防腐层循环导入

    q = (question or '').strip()
    if not q:
        return 'empty', ''
    for name in dify_client.pack_product_names():
        if name and name in q:
            return 'product', name
    for intent, _keywords in INTENT_RULES:
        if match_intent(q, intent):
            return intent, first_keyword(q, intent)
    # 关键词全 miss：复用第五轮模糊意图路由（Bigram 相似度），命中即真实路由目标
    fuzzy = dify_client.intent_fuzzy_route(q)  # M-11/M-18：公开入口，避免维护第二套语料
    if fuzzy:
        return fuzzy, '模糊匹配'
    return 'knowledge', ''


def _intent_step(question):
    """意图解析步骤：返回 ((intent, detail), 摘要)。"""
    intent, detail = classify_intent(question)
    return (intent, detail), f'{INTENT_LABELS.get(intent, intent)}（{detail or "无关键词命中"}）'


def _stats_summary(stats):
    """统计摘要（KPI 三件套），轨迹里作为「取到了什么数据」的证据。"""
    kpi = (stats or {}).get('kpi', {}) or {}
    has = (stats or {}).get('has_data', False)
    if not has:
        return '无经营数据（空态）'
    return (f"总营收 ¥{kpi.get('total_revenue', 0)}、总销量 {kpi.get('total_qty', 0)}、"
            f"日均 ¥{kpi.get('avg_daily_revenue', 0)}")


def _tool_analytics(stats=None):
    """工具：门店真实经营统计（stats 为空时才真正聚合一次）。"""
    if stats is None:
        from src.analytics import compute_dashboard_stats
        stats = compute_dashboard_stats()
    return stats, _stats_summary(stats)


def _tool_knowledge(question):
    """工具：知识库检索（RAG 溯源）。"""
    from src.knowledge_base import focus_domain, search
    hits = search(question, top_k=2, domain=focus_domain(question))
    names = '、'.join(f"{h.get('title', '')}（{h.get('domain', '')}）" for h in hits)
    return hits, names or '未命中知识条目，走通用兜底'


def _tool_forecast(stats):
    """工具：销量预测（数据不足时如实报「不可用」）。"""
    from src import metrics  # 懒加载：单位随包（B2b），与规则版/看板同源

    fc = (stats or {}).get('forecast') or {}
    if not fc.get('ok'):
        return fc, '数据不足 3 日，预测不可用'
    text = f"明日约 {fc.get('qty')} {metrics.dashboard_labels()['unit']}"
    if fc.get('revenue'):
        text += f"，营收约 ¥{fc.get('revenue')}"
    return fc, text


def _tool_waste(stats):
    """工具：损耗根因归因（包未声明 stock/waste 角色时自动关闭）。"""
    alerts = (stats or {}).get('waste_alerts') or []
    if not alerts:
        return alerts, '无损耗预警'
    names = '、'.join(str(a.get('product', '')) for a in alerts[:3])
    return alerts, f'{len(alerts)} 条预警：{names}'


def _tool_quiz():
    """工具：考核记录查询（管理员问「谁考得好」时的真实数据源）。

    越权修复（2026-09-22）：只统计当前账号可见范围内的考核记录，禁止把
    全平台条数回显给任意登录者；请求外（离线评测 / CLI）保持既有零过滤口径。
    """
    from src import tenant
    from src.models import QuizRecord

    staff_ids = tenant.visible_staff_ids()
    if staff_ids is None:
        total = QuizRecord.query.count()
    else:
        total = QuizRecord.query.filter(
            tenant.in_clause(QuizRecord.staff_id, staff_ids)).count()
    return total, f'共 {total} 条考核记录'


def _tool_compare(stats):
    """工具：周期对比（近 7 日 vs 前 7 日）。"""
    cp = (stats or {}).get('compare') or {}
    if not cp.get('ok'):
        return cp, '数据不足 14 日，周期对比不可用'
    return cp, f"销量 {cp.get('qty_pct')}% / 营收 {cp.get('revenue_pct')}%"


def _tool_anomaly(stats):
    """工具：离群检测（z-score + IQR 双检测）。"""
    an = (stats or {}).get('anomalies') or {}
    if not an.get('ok'):
        return an, '未检出异常日'
    return an, f"检出 {len(an.get('points') or [])} 个异常日"


def _tool_basket(stats):
    """工具：购物篮关联分析。"""
    bk = (stats or {}).get('basket') or {}
    pairs = bk.get('pairs') or []
    if not bk.get('ok') or not pairs:
        return bk, '暂无有效组合'
    top = pairs[0]
    return bk, f"「{top.get('a')}」+「{top.get('b')}」提升度 {top.get('lift')}"


class TraceRun:
    """一次 AI 决策的轨迹容器：逐步计时、逐步留痕、整体落盘。"""

    def __init__(self, scene, question=''):
        self.scene = scene
        self.question = _cap(question)
        self.steps = []
        self.source = DEFAULT_SOURCE
        self.ts = time.strftime('%Y-%m-%dT%H:%M:%S')

    def step(self, name, fn, input_summary='', source=''):
        """执行一个流水线步骤并留痕。

        Args:
            name: 步骤名（如 'intent.parse' / 'analytics.stats'）。
            fn: 无参可调用；返回 `(value, summary)` 或裸 value。
            input_summary: 输入摘要。
            source: 来源标记（空则取当前 run 默认来源）。

        Returns:
            步骤返回值；步骤抛异常时返回 None 且 ok=False（不中断流水线）。
        """
        t0 = time.perf_counter()
        try:
            res = fn()
            if isinstance(res, tuple) and len(res) == 2:
                value, summary = res
            else:
                value, summary = res, None
            ok = True
        except Exception as exc:  # 宽捕获：任一步骤失败不得打断主流程
            value, summary, ok = None, f'{type(exc).__name__}：本步失败已降级', False
        latency = int((time.perf_counter() - t0) * 1000)
        text = _cap(summary if summary is not None else _auto_summary(value))
        src = source or self.source
        self.steps.append({
            'step': len(self.steps) + 1,
            'name': name,
            'input_summary': _cap(input_summary),
            'output_summary': text,
            'latency_ms': latency,
            'source': src,
            'source_label': source_label(src),
            'ok': ok,
        })
        return value

    def finish(self, source=''):
        """标记整轮结论来源（规则版 / 大模型生成）。"""
        if source:
            self.source = source
        return self

    def total_ms(self):
        return sum(s['latency_ms'] for s in self.steps)

    def to_dict(self):
        """序列化为模板可渲染的字典（含每步来源徽章文案）。"""
        return {
            'scene': self.scene,
            'question': self.question,
            'ts': self.ts,
            'source': self.source,
            'source_label': source_label(self.source),
            'total_ms': self.total_ms(),
            'steps': list(self.steps),
        }

    def persist(self):
        """追加一行 JSONL 留痕（失败静默，绝不影响业务主流程）。"""
        try:
            os.makedirs(os.path.dirname(TRACE_PATH), exist_ok=True)
            line = json.dumps(self.to_dict(), ensure_ascii=False) + '\n'
            with _TRACE_LOCK:
                _rotate_trace_if_needed()
                with open(TRACE_PATH, 'a', encoding='utf-8') as f:
                    f.write(line)
        except OSError:
            logger.debug('agent trace persist skipped', exc_info=True)
        return self


def _rotate_trace_if_needed():
    """按文件大小轮转 JSONL；轮转失败仍继续写当前文件（不阻断业务）。"""
    try:
        if not os.path.exists(TRACE_PATH) \
                or os.path.getsize(TRACE_PATH) < TRACE_MAX_BYTES:
            return
        for idx in range(TRACE_BACKUPS, 0, -1):
            src = TRACE_PATH if idx == 1 else f'{TRACE_PATH}.{idx - 1}'
            dst = f'{TRACE_PATH}.{idx}'
            if not os.path.exists(src):
                continue
            if os.path.exists(dst):
                os.remove(dst)
            os.replace(src, dst)
    except OSError:
        logger.debug('agent trace rotation skipped', exc_info=True)


def run_assistant(question, stats=None, conclude=None):
    """管理员智能助手流水线：意图 → 工具 → 结论。

    Args:
        question: 管理员提问（已过承接消解）。
        stats: 已聚合的统计结果；为 None 时由 `analytics.stats` 步骤现算。
        conclude: 结论函数 `(question, stats) -> dict`，需含 'reply' 与 'source'。

    Returns:
        tuple: (结论 dict, 轨迹 dict)；conclude 为 None 时结论为规则版空态文案。
    """
    run = TraceRun('admin-assistant', question)
    intent, _detail = run.step('intent.parse', lambda: _intent_step(question),
                               input_summary=question)
    label = INTENT_LABELS.get(intent, intent)

    if intent == 'assessment':
        run.step('quiz.records', _tool_quiz, input_summary=question)
    else:
        # 与既有路由同口径：除考核问询外一律先取真实统计（结论层需要它）
        stats = run.step('analytics.stats', lambda: _tool_analytics(stats),
                         input_summary='读取门店真实经营数据')
        if intent == 'knowledge':
            run.step('knowledge.search', lambda: _tool_knowledge(question),
                     input_summary=question)
        elif intent == 'forecast':
            run.step('forecast.eval', lambda: _tool_forecast(stats))
        elif intent == 'compare':
            run.step('period.compare', lambda: _tool_compare(stats))
        elif intent == 'waste':
            run.step('waste.root_cause', lambda: _tool_waste(stats))
    if conclude is None:
        result = {'source': 'rule', 'reply': '请输入你的问题。'}
    else:
        result = run.step('conclusion', lambda: conclude(question, stats),
                          input_summary=f'{label} → 结论生成')
        if not isinstance(result, dict):
            result = {'source': 'rule', 'reply': str(result or '')}
    run.finish(result.get('source', 'rule'))
    run.persist()
    return result, run.to_dict()


def run_analysis(stats, conclude):
    """AI 经营分析流水线：统计 → 各路 AI 智能工具 → 结论 → 依据汇总。

    Args:
        stats: 已聚合的统计结果。
        conclude: 结论函数 `(stats) -> dict`，需含 'content'/'source'/'evidence'。

    Returns:
        tuple: (分析结果 dict, 轨迹 dict)。
    """
    run = TraceRun('admin-analysis', '生成门店经营分析报告')
    run.step('analytics.stats', lambda: _tool_analytics(stats),
             input_summary='读取门店真实经营数据')
    for flag, name, fn in (
        ('forecast', 'forecast.eval', _tool_forecast),
        ('compare', 'period.compare', _tool_compare),
        ('anomalies', 'anomaly.detect', _tool_anomaly),
        ('basket', 'basket.assoc', _tool_basket),
    ):
        if (stats or {}).get(flag, {}).get('ok'):
            run.step(name, lambda fn=fn: fn(stats))
    result = run.step('conclusion', lambda: conclude(stats),
                      input_summary='汇总数据 → 生成分析报告')
    if not isinstance(result, dict):
        result = {'source': 'rule', 'content': str(result or ''), 'evidence': []}
    evidence = result.get('evidence') or []
    conf = result.get('confidence') or {}
    run.step('evidence.summary', lambda: (
        evidence,
        (f'{len(evidence)} 条结论，平均置信度 {round(float(conf.get("avg") or 0) * 100)}%'
         if evidence else '无置信度结论（数据不足）')),
             input_summary='逐条结论的置信度与数据依据')
    run.finish(result.get('source', 'rule'))
    run.persist()
    return result, run.to_dict()
