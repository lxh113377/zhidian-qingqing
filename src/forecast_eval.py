"""预测回测闭环（AI 深化第六轮 A）— AI 预测 vs 实际销量的滚动准确率追踪

解决的问题：AI 每天给出「明日销量预测」，但项目从未回答「这个 AI 预测到底准不准」。
本模块把每次预测落盘（首次为准，防同日多次刷新刷分），次日实际销量入库后自动回测，
滚动输出可量化的效果指标：

  - **MAPE**：滚动窗口内平均绝对百分比误差（越低越准）；
  - **准确率 accuracy** = 100 − MAPE×100（百分比口径，可直接写进申报材料）；
  - **命中率 hit_rate**：相对误差 ≤ HIT_THRESHOLD 的预测占比；
  - **last**：最近一次「预测 vs 实际」明细（偏差百分比）。

设计约定（与 knowledge_base 反馈闭环同款）：
  1. 落盘用轻量 JSON（`data/forecast_log.json`），不加表、不迁库；
  2. 读写失败一律静默降级（返回 ok=False），绝不把异常扩散到看板渲染；
  3. `evaluate_forecasts()` 为纯函数便于单测；落盘副作用只集中在
     `record_forecast()`（组合入口）；
  4. 「首次为准」：同一天重复渲染看板不覆盖已记录预测，保证回测公平。
"""
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# 落盘文件（可用环境变量重定向，测试隔离用）
FORECAST_LOG_FILE = os.environ.get('FORECAST_LOG_FILE') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'forecast_log.json')

# 滚动回测窗口：取最近 N 次已验证（有实际值）的预测计算指标
BACKTEST_WINDOW = 10
# 命中判据：相对误差 ≤ 20% 视为该次预测命中
HIT_THRESHOLD = 0.20

# PF-7：看板高频渲染不应重复写盘；并发回填也不能互相覆盖。
_FORECAST_LOCK = threading.RLock()


def _load_log() -> dict:
    """读取预测日志；文件缺失/损坏返回空字典（不抛异常）。"""
    try:
        with open(FORECAST_LOG_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_log(data: dict) -> bool:
    """原子写入预测日志（先写临时文件再替换）。"""
    try:
        os.makedirs(os.path.dirname(FORECAST_LOG_FILE), exist_ok=True)
        tmp = f'{FORECAST_LOG_FILE}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FORECAST_LOG_FILE)
        return True
    except OSError:
        return False


def record_prediction(target_date: str, predicted,
                      actual_by_date: dict | None = None) -> dict:
    """记录一次预测并回测历史，返回滚动准确率指标（组合入口）。

    流程（按 key 合并更新，保历史 actual，绝不整体重建）：
      1. 读取日志 → 回填：凡目标日期已出现实际销量的条目补上 actual；
      2. 记录本次预测：target_date 首次为准（同日已有记录不覆盖）；
      3. 回写落盘 → 用纯函数 evaluate_forecasts 计算滚动指标。

    Args:
        target_date: 预测目标日期（ISO 'YYYY-MM-DD'）。
        predicted: 预测销量（数值，容忍 None/非法）。
        actual_by_date: {ISO日期: 实际日销量} 映射（回填用，可为 None）。

    Returns:
        dict: `evaluate_forecasts()` 的指标结构；任何异常均降级
        {'ok': False, 'count': 0, 'mape': None, 'accuracy': None,
         'hit_rate': None, 'last': None}，绝不打断看板渲染。
    """
    fallback = {'ok': False, 'count': 0, 'mape': None,
                'accuracy': None, 'hit_rate': None, 'last': None}
    try:
        key = str(target_date or '')[:10]
        if not key:
            return fallback
        pred = None
        try:
            pred = round(float(predicted), 1)
        except (TypeError, ValueError):
            pred = None
        actual_by_date = actual_by_date or {}

        with _FORECAST_LOCK:
            log = _load_log()
            changed = False
            for d, rec in log.items():
                if isinstance(rec, dict) and rec.get('actual') is None \
                        and isinstance(rec.get('predicted'), (int, float)):
                    act = actual_by_date.get(d)
                    if act is not None:
                        rec['actual'] = round(float(act), 1)
                        changed = True
            # 本次预测：首次为准
            if pred is not None:
                old = log.get(key)
                if not isinstance(old, dict) or old.get('predicted') is None:
                    log[key] = {'predicted': pred,
                                'actual': actual_by_date.get(key),
                                'recorded_at': time.strftime('%Y-%m-%d %H:%M')}
                    changed = True
            # PF-7：无新预测且无 actual 回填时不写盘，看板渲染退化为纯读取。
            if changed:
                _save_log(log)
            return evaluate_forecasts(log)
    except Exception:  # 回测属增强功能，任何异常不阻断看板渲染
        logger.exception('forecast backtest failed')
        return fallback


def evaluate_forecasts(log: dict) -> dict:
    """滚动回测评估（纯函数）。

    Args:
        log: {ISO日期: {'predicted': float, 'actual': float|None}} 预测日志。

    Returns:
        dict: {'ok', 'count', 'mape', 'accuracy', 'hit_rate', 'last'}。
        count=0 时 ok=False，其余指标为 None（前端显示「预测积累中」）。
    """
    fallback = {'ok': False, 'count': 0, 'mape': None,
                'accuracy': None, 'hit_rate': None, 'last': None}
    validated = []
    for d, rec in (log or {}).items():
        if not isinstance(rec, dict):
            continue
        pred, act = rec.get('predicted'), rec.get('actual')
        if not isinstance(pred, (int, float)) or not isinstance(act, (int, float)):
            continue
        # 相对误差：actual=0 时预测>0 记 100% 偏差，预测也为 0 记命中
        abs_pct = (abs(pred - act) / act) if act > 0 else (0.0 if pred <= 0 else 1.0)
        validated.append({
            'date': d, 'predicted': round(float(pred), 1),
            'actual': round(float(act), 1),
            'abs_pct': round(abs_pct, 4),
            'hit': abs_pct <= HIT_THRESHOLD,
        })
    if not validated:
        return fallback
    validated.sort(key=lambda x: x['date'])
    window = validated[-BACKTEST_WINDOW:]
    mape = sum(w['abs_pct'] for w in window) / len(window)
    hit_rate = sum(1 for w in window if w['hit']) / len(window)
    last = window[-1]
    return {
        'ok': True,
        'count': len(window),
        'mape': round(mape * 100, 1),
        'accuracy': round(max(0.0, 1.0 - mape) * 100, 1),
        'hit_rate': round(hit_rate * 100, 1),
        'last': {'date': last['date'], 'predicted': last['predicted'],
                 'actual': last['actual'],
                 'bias_pct': round((last['predicted'] / last['actual'] - 1) * 100, 1)
                 if last['actual'] > 0 else None},
    }
