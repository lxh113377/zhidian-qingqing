"""运行期告警判定 —— 把已有聚合计数与阈值比较，产出机器可读的告警条目（第 23 轮 H51）。

动因（当场实测，不是"应该有告警"）：
- `/healthz` 已经算出 `db_ms` / `ai.inflight|limit|rejected` / `diag.errors_5xx|slow|dropped|error_kinds`，
  但**全文没有任何一处把它们与阈值比较** —— 数字在，判断不在，运维得自己肉眼比。
- 阈值只活在 `src/performance.py:25-26`（`SLOW_REQUEST_MS=500` / `SLOW_QUERY_MS=200`），
  用途是打标 + WARN，不参与健康判定。
- `grep 阈值告警|error_rate|failure_rate` 在门禁与 L4 合并只 1 命中，且那是业务损耗率
  ⇒ 整个告警面 0 判据：把判定逻辑删掉，35 项门禁与三套测试都不会红。

标定（2026-09-26 本机临时库，40 次探活 + 480 次真实请求，脚本口径见 07 本轮段落）：
`db_ms` p50 0.2 / p95 0.3 / max 0.5 ms；端到端 p50 1.2 / p95 2.3 / max 85.2 ms；
`slow`（>500ms）计数 0；`errors_5xx` 0；**而 `errors_4xx` 比例高达 0.346** ——
因为这段探测本身就打了 404 与坏令牌。⇒ **4xx 比例不得进告警**（否则每次自检脚本都在报警），
本模块只用 5xx 与未捕获异常类数。

线上为什么不用：`https://lxh123.pythonanywhere.com/healthz` 实测取不到 `db_ms`/`diag`
（PA 仍跑改前代码，07 的 M63 同条），所以 `ALERT_DB_MS` 不抄本机分位数，改取
「严格小于 `SLOW_REQUEST_MS`」这条可辩护关系：库先于整条请求报警。

计数字段是**自进程启动累计、重启清零**，所以带 0 阈值的规则触发后会一直显示到重启 ——
这正是把它做成 `alerts`（advisory）而不是并进 `status`（对外契约）的原因：
`smoke_remote.py:58` 按 `status == 'ok'` 判部署冒烟，改 status 语义等于把冒烟原地判红。

红线：输出只有聚合整数，不含路径名 / SQL 模板 / 表名（`/healthz` 匿名可达，第 10 轮 L22 口径）。
"""
import os

# 规则唯一清单。字段：code 机读码 / metric 取哪个聚合值 / env 可配阈值键（必须登记进
# 部署/.env.example，门禁第 36 项按这个对账）/ default / cmp / min_sample 比例类分母下限。
ALERT_RULES = (
    {'code': 'db_slow', 'metric': 'db_ms', 'env': 'ALERT_DB_MS', 'default': 250.0,
     'cmp': 'gt', 'label': '探活用的 SELECT 1 耗时超阈'},
    {'code': 'server_errors', 'metric': 'errors_5xx', 'env': 'ALERT_ERROR_5XX_MAX',
     'default': 0, 'cmp': 'gt', 'label': '出现 5xx（本应为 0，重启前会持续显示）'},
    {'code': 'unhandled_exception_kinds', 'metric': 'error_kinds',
     'env': 'ALERT_ERROR_KINDS_MAX', 'default': 0, 'cmp': 'gt',
     'label': '出现未捕获异常（got_request_exception 聚合到类数）'},
    {'code': 'slow_request_ratio', 'metric': 'slow_ratio', 'env': 'ALERT_SLOW_RATIO',
     'default': 0.05, 'cmp': 'gt', 'min_sample': 50, 'label': '慢请求占比超阈'},
    {'code': 'ai_backpressure', 'metric': 'ai_inflight_ratio', 'env': 'ALERT_AI_INFLIGHT_RATIO',
     'default': 0.75, 'cmp': 'gte', 'label': 'AI 在途数贴近并发闸上限'},
    {'code': 'ai_rejected', 'metric': 'ai_rejected', 'env': 'ALERT_AI_REJECTED_MAX',
     'default': 0, 'cmp': 'gt', 'label': 'AI 请求被并发闸丢弃'},
    {'code': 'observability_dropped', 'metric': 'dropped', 'env': 'ALERT_DROPPED_MAX',
     'default': 0, 'cmp': 'gt', 'label': '观测链自身丢记录（计数或留痕写失败）'},
)

ALERT_CODES = tuple(r['code'] for r in ALERT_RULES)
ALERT_ENV_KEYS = tuple(r['env'] for r in ALERT_RULES)


def threshold_for(rule):
    """阈值取环境变量优先；非法值回默认并让调用方仍能判定（不静默接受垃圾值当 0）。"""
    raw = os.environ.get(rule['env'])
    if raw in (None, ''):
        return rule['default']
    try:
        return float(raw)
    except (TypeError, ValueError):
        return rule['default']


def build_metrics(db_ms=None, diag_stats=None, ai_stats=None, audit_stats=None):
    """三个聚合器 → 判定输入。只取聚合整数，字段名一律不带进来（公开端点脱敏前提）。"""
    d = dict(diag_stats or {})
    a = dict(ai_stats or {})
    audit = dict(audit_stats or {})
    requests = int(d.get('requests') or 0)
    limit = int(a.get('ai_limit') or 0)
    return {
        'db_ms': float(db_ms or 0),
        'requests': requests,
        'errors_5xx': int(d.get('errors_5xx') or 0),
        'error_kinds': int(d.get('error_kinds') or 0),
        'slow_ratio': (int(d.get('slow') or 0) / requests) if requests else 0.0,
        'ai_inflight_ratio': (int(a.get('ai_inflight') or 0) / limit) if limit else 0.0,
        'ai_rejected': int(a.get('ai_rejected') or 0),
        # 两条丢弃计数合成一条：观测链丢数据 = 归因能力失效，与业务无关但必须可见
        'dropped': int(d.get('dropped') or 0) + int(audit.get('dropped') or 0),
    }


def evaluate(metrics=None, **kwargs):
    """按规则表判定 → 告警条目列表。**空列表才是"无告警"**，缺判定输入不会被当成通过。"""
    if metrics is None:
        metrics = build_metrics(**kwargs)
    alerts = []
    for rule in ALERT_RULES:
        value = metrics.get(rule['metric'])
        if value is None:
            continue
        min_sample = rule.get('min_sample')
        if min_sample is not None and int(metrics.get('requests') or 0) < min_sample:
            continue        # 分母不足时比例无意义：宁可不报，也不制造"1/1=100%"的假告警
        limit_value = threshold_for(rule)
        hit = value >= limit_value if rule['cmp'] == 'gte' else value > limit_value
        if hit:
            alerts.append({'code': rule['code'], 'label': rule['label'],
                           'value': round(float(value), 4), 'threshold': limit_value})
    return alerts


def healthz_fields(db_ms=None, diag_stats=None, ai_stats=None, audit_stats=None):
    """`/healthz` 的两条新键；status 判定集合不动（对外契约，见模块文档）。"""
    alerts = evaluate(build_metrics(db_ms=db_ms, diag_stats=diag_stats,
                                    ai_stats=ai_stats, audit_stats=audit_stats))
    return {'alerts': alerts, 'alert_count': len(alerts)}


__all__ = ['ALERT_CODES', 'ALERT_ENV_KEYS', 'ALERT_RULES', 'build_metrics', 'evaluate',
           'healthz_fields', 'threshold_for']
