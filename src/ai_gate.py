"""AI 出站调用并发闸（H5-lite，2026-09-24 第 7 轮对标）—— 无 broker 场景下的唯一可行解。

为什么不是"上 Celery/RQ"
────────────────────────
部署红线是 PythonAnywhere 免费层单 web 进程、无 Redis、无 worker、不引新依赖，
所以标准答案（异步任务队列）在本项目**不可用**。

上游是怎么处理"没有 broker"的（2026-09-24 直接拉源码核实，只写有行号证据的部分）：
- Frappe `frappe/utils/background_jobs.py:206-208`
  `call_directly = now or (not is_async and not frappe.in_test)` → `frappe.call(...)`：
  同步执行是**调用方显式选择**出来的，不是隐式发生。
- 同文件 `:212-216`：Redis `ConnectionError` 时，只有 `in_migrate` 分支才
  打印一行 "Redis queue is unreachable: Executing … synchronously" 并同步跑，
  否则直接 `raise`。即成熟框架把"无 broker"当作**受限且必须显式承认的状态**，
  绝不当通用兜底路径。
- n8n `packages/cli/src/config/types.ts:77` 的 `queue.bull.redis` 配置面印证
  "同一套代码、两种执行模式由配置切换"。
- （Superset 的异步查询回落路径本轮未完成核实，故不引用。）

对本项目的结论：既然永远没有 worker，正确姿势不是伪造异步，而是把同步执行
变成**有界且可降级**——并发有上界、拿不到槽位立即回规则版。本模块即此上界。

要防的失效模式
──────────────
`deliverables/engineering-assurance/incident-postmortem-dify-thread-starvation-2026-08-11.md`
（**注意：该文档是桌面推演 INC-2026-0811-01，不是真实生产事故**，但它对根因的量化成立）：
Dify 进入「TCP 可连、HTTP 不回」的半死态时，看板串行两次 30s 同步 AI 调用即可打满
waitress 默认 4 线程，与 AI 零依赖的 `POST /h5/submit` 被饿死 —— 触碰
「AI 只做建议、不可拖垮记录类功能」产品红线。
现状：`DIFY_TIMEOUT` 默认仍为 30s，`decision_cockpit._run_isolated_roles` 每次跑 2 个并行
模型调用（红线要求"只允许并行网络推理"），N 个并发请求即 2N 个各可挂 30s 的线程，
**出站调用至今没有并发上界**。既有 `CircuitBreaker` 只治"连续失败后快速断"，
不治并发数，两者不等价。

口径
────
1. **非阻塞取位**：拿不到槽位立即失败（由调用方回退规则版），绝不排队堆积 ——
   与 `src/webhooks.py` 的 `acquire(blocking=False)` + 溢出留痕同一范式。
2. **不污染熔断计数**：饱和是背压，不是上游故障，绝不能记 `on_failure()`。
3. **只包网络段**：取位/放位只围住 `requests.post`；配额记账、缓存、JSONL
   落盘仍在主请求线程（PH5 并发边界红线）。
4. **可观测**：暴露 `inflight()/limit()/rejected`，供健康端点与测试断言取用。
"""
import logging
import os
import threading

logger = logging.getLogger(__name__)

# 闸容量取 2 的依据（当场实测）：全仓无任何 `threads=` 配置（grep 零命中），
# waitress 走默认 **4 线程**；而一次完整驾驶舱恰好需要 2 个并行槽位（两角色，
# 红线要求真并行）。若上界设 4，AI 就能吃光整个工作线程池 —— 正是那份推演里
# 填报链路被饿死的成因。取 2 = 「AI 至多占一半池子」，保证
# `POST /h5/submit` 等记录类链路恒有线程可用。
DEFAULT_LIMIT = 2


_state = {
    'sem': None,
    'limit': 0,
    'inflight': 0,
    'rejected': 0,
}
_meta_lock = threading.Lock()


def _limit():
    raw = os.environ.get('AI_MAX_INFLIGHT')
    try:
        return max(1, int(raw)) if raw not in (None, '') else DEFAULT_LIMIT
    except (TypeError, ValueError):
        logger.warning('AI_MAX_INFLIGHT 非整数（%r），回退默认 %d', raw, DEFAULT_LIMIT)
        return DEFAULT_LIMIT


def _gate():
    """惰性建闸；环境变量变更后需显式 `rebuild()`（测试与运维用）。"""
    if _state['sem'] is None:
        with _meta_lock:
            if _state['sem'] is None:
                limit = _limit()
                _state['sem'] = threading.BoundedSemaphore(limit)
                _state['limit'] = limit
    return _state['sem']


def acquire():
    """非阻塞占一个出站槽位。

    Returns:
        bool: True=已占位（调用方**必须**配对 `release()`）；False=已满，应立即降级。
    """
    sem = _gate()
    if not sem.acquire(blocking=False):
        with _meta_lock:
            _state['rejected'] += 1
        logger.warning('AI 出站并发已满（limit=%d inflight=%d），本次降级为规则版',
                       _state['limit'], _state['inflight'])
        return False
    with _meta_lock:
        _state['inflight'] += 1
    return True


def release():
    """释放槽位；重复释放会由 BoundedSemaphore 抛错，属调用方 bug，不静默掩盖。

    先放信号量再改计数：若 `release()` 因超发抛错，计数不会被错误地减掉。
    """
    _gate().release()
    with _meta_lock:
        _state['inflight'] -= 1


def inflight():
    """当前占用中的出站调用数（观测用）。"""
    with _meta_lock:
        return _state['inflight']


def limit():
    """当前闸容量。"""
    _gate()
    with _meta_lock:
        return _state['limit']


def rejected():
    """累计因饱和被拒的次数（观测用）。"""
    with _meta_lock:
        return _state['rejected']


def stats():
    """健康端点/测试可直接取的三元组。"""
    _gate()
    with _meta_lock:
        return {'ai_inflight': _state['inflight'], 'ai_limit': _state['limit'],
                'ai_rejected': _state['rejected']}


def rebuild():
    """按当前环境变量重建闸门（仅测试与显式运维动作使用）。

    就地改 dict 内容而不是重绑模块变量：全仓「dict 容器替代 global」范式，
    且 `from src.ai_gate import acquire` 这类既有绑定不会指向旧对象。
    """
    with _meta_lock:
        limit_n = _limit()
        _state['sem'] = threading.BoundedSemaphore(limit_n)
        _state['limit'] = limit_n
        _state['inflight'] = 0
        _state['rejected'] = 0
    return limit_n


__all__ = [
    'DEFAULT_LIMIT',
    'acquire',
    'inflight',
    'limit',
    'rebuild',
    'rejected',
    'release',
    'stats',
]
