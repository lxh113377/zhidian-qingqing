"""商家级 AI 配额、超时、结果缓存与降级（PH2-4D / P5 复用）。

配额（每商家每日）：
  · 造包 pack_gen：3 次
  · 经营分析 analysis：30 次
  · Token 总量：120,000
单次调用超时 30 秒；结果缓存 10 分钟；超限/超时/异常一律**回落规则版**，
并把真实原因写进返回值（页面据此标注来源），不静默降级。
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from src.models import AIUsageLedger, db

KIND_PACK_GEN = 'pack_gen'
KIND_ANALYSIS = 'analysis'
KIND_DECISION = 'decision'

PACK_GEN_DAILY_LIMIT = 3
ANALYSIS_DAILY_LIMIT = 30
DECISION_DAILY_LIMIT = 10
TOKEN_DAILY_LIMIT = 120_000

AI_TIMEOUT_SECONDS = 30
CACHE_TTL_SECONDS = 10 * 60

_KIND_LIMITS = {
    KIND_PACK_GEN: PACK_GEN_DAILY_LIMIT,
    KIND_ANALYSIS: ANALYSIS_DAILY_LIMIT,
    KIND_DECISION: DECISION_DAILY_LIMIT,
}
_CACHE = {}
_CACHE_MAX_ENTRIES = 1000


def _cache_evict_if_needed(now=None):
    now = now or time.time()
    if len(_CACHE) < _CACHE_MAX_ENTRIES:
        return
    expired = [k for k, (_, exp) in _CACHE.items() if now >= exp]
    for k in expired:
        _CACHE.pop(k, None)
    while len(_CACHE) >= _CACHE_MAX_ENTRIES:
        _CACHE.pop(next(iter(_CACHE)), None)


def _today_start():
    """当日零点（**UTC 口径**）。

    账本 `created_at` 由 `datetime.now(UTC)` 生成（UTC-naive）；若这里用本地
    `date.today()`，在 UTC+8 的凌晨时段（本地今天 / UTC 昨天）会把当天用量
    算成 0 —— 配额整条失效。故统一取 UTC 当日零点。
    """
    return datetime.now(UTC).replace(
        tzinfo=None, hour=0, minute=0, second=0, microsecond=0)


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def kind_limit(kind):
    """该类型的每日次数上限（未知类型按分析口径）。"""
    return _KIND_LIMITS.get(kind, ANALYSIS_DAILY_LIMIT)


def usage_today(merchant_id, kind=None):
    """当日用量：{'calls': n, 'tokens': n}（按商家；kind=None 时含全部类型）。"""
    from sqlalchemy import func as _func
    query = db.session.query(
        _func.count(AIUsageLedger.id),
        _func.coalesce(_func.sum(AIUsageLedger.tokens), 0),
    ).filter(AIUsageLedger.merchant_id == merchant_id,
             AIUsageLedger.created_at >= _today_start())
    if kind:
        query = query.filter(AIUsageLedger.kind == kind)
    calls, tokens = query.one()
    return {'calls': int(calls or 0), 'tokens': int(tokens or 0)}


def check_quota(merchant_id, kind=KIND_ANALYSIS):
    """配额检查（次数 + Token 双闸门）。

    Returns:
        dict: {'ok', 'reason', 'used', 'limit', 'tokens_used', 'token_limit'}
        reason 取值：ok / quota_exceeded / token_exceeded
    """
    used = usage_today(merchant_id, kind)
    total = usage_today(merchant_id)
    limit = kind_limit(kind)
    if used['calls'] >= limit:
        return {'ok': False, 'reason': 'quota_exceeded', 'used': used['calls'],
                'limit': limit, 'tokens_used': total['tokens'],
                'token_limit': TOKEN_DAILY_LIMIT}
    if total['tokens'] >= TOKEN_DAILY_LIMIT:
        return {'ok': False, 'reason': 'token_exceeded', 'used': used['calls'],
                'limit': limit, 'tokens_used': total['tokens'],
                'token_limit': TOKEN_DAILY_LIMIT}
    return {'ok': True, 'reason': 'ok', 'used': used['calls'], 'limit': limit,
            'tokens_used': total['tokens'], 'token_limit': TOKEN_DAILY_LIMIT}


def record_usage(merchant_id, kind=KIND_ANALYSIS, tokens=0, store_id=None,  # noqa: PLR0913, PLR0917
                 ok=True, detail=None):
    """写一条 AI 用量账本（每次调用一行，含失败）。"""
    row = AIUsageLedger(merchant_id=merchant_id, store_id=store_id, kind=kind,
                        tokens=max(0, int(tokens or 0)), ok=bool(ok),
                        detail_json=(str(detail)[:200] if detail else None))
    db.session.add(row)
    db.session.commit()
    return row.id


def cache_key_of(merchant_id, kind, key):
    return (int(merchant_id), str(kind), str(key or ''))


def cache_get(merchant_id, kind, key, now=None):
    """读缓存（过期即清）；未命中返回 None。"""
    now = now or time.time()
    ck = cache_key_of(merchant_id, kind, key)
    hit = _CACHE.get(ck)
    if not hit:
        return None
    value, expires_at = hit
    if now >= expires_at:
        _CACHE.pop(ck, None)
        return None
    return value


def cache_put(merchant_id, kind, key, value, now=None):
    now = now or time.time()
    _cache_evict_if_needed(now)
    _CACHE[cache_key_of(merchant_id, kind, key)] = (value, now + CACHE_TTL_SECONDS)
    return value


def clear_cache():
    _CACHE.clear()


def run_with_quota(merchant_id, kind, producer, fallback, *, cache_key=None,  # noqa: PLR0913
                   store_id=None, now=None):
    """配额 + 缓存 + 超时 + 降级的唯一编排入口。

    Args:
        producer: `fn(timeout) -> value | (value, tokens)`；可抛 TimeoutError/异常。
        fallback: 无参可调用，返回规则版结果（超限/超时/异常时使用）。

    Returns:
        dict: {'ok', 'source', 'value', 'reason', 'quota'}
        source 取值：cache / ai / fallback
    """
    if cache_key is not None:
        cached = cache_get(merchant_id, kind, cache_key, now=now)
        if cached is not None:
            return {'ok': True, 'source': 'cache', 'value': cached,
                    'reason': 'cache_hit', 'quota': check_quota(merchant_id, kind)}

    quota = check_quota(merchant_id, kind)
    if not quota['ok']:
        return {'ok': False, 'source': 'fallback', 'value': fallback(),
                'reason': quota['reason'], 'quota': quota}

    try:
        produced = producer(AI_TIMEOUT_SECONDS)
    except TimeoutError:
        record_usage(merchant_id, kind, tokens=0, store_id=store_id, ok=False,
                     detail='timeout')
        return {'ok': False, 'source': 'fallback', 'value': fallback(),
                'reason': 'timeout', 'quota': quota}
    except Exception as exc:
        record_usage(merchant_id, kind, tokens=0, store_id=store_id, ok=False,
                     detail=f'error:{exc.__class__.__name__}')
        return {'ok': False, 'source': 'fallback', 'value': fallback(),
                'reason': 'error', 'quota': quota}

    if isinstance(produced, tuple):
        value, tokens = produced[0], produced[1]
    else:
        value, tokens = produced, 0
    record_usage(merchant_id, kind, tokens=tokens, store_id=store_id, ok=True)
    if cache_key is not None:
        cache_put(merchant_id, kind, cache_key, value, now=now)
    return {'ok': True, 'source': 'ai', 'value': value, 'reason': 'ok', 'quota': quota}


__all__ = [
    'AI_TIMEOUT_SECONDS',
    'ANALYSIS_DAILY_LIMIT',
    'CACHE_TTL_SECONDS',
    'DECISION_DAILY_LIMIT',
    'KIND_ANALYSIS',
    'KIND_DECISION',
    'KIND_PACK_GEN',
    'PACK_GEN_DAILY_LIMIT',
    'TOKEN_DAILY_LIMIT',
    'cache_get',
    'cache_put',
    'check_quota',
    'clear_cache',
    'kind_limit',
    'record_usage',
    'run_with_quota',
    'usage_today',
]
