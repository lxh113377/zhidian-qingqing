"""AI 调用留痕（S4）——把「真的调用了大模型」变成可自证证据

每次在线 LLM 调用落一行 JSONL 到 `data/ai_call_log.jsonl`。留痕写入失败一律
静默吞掉（OSError），**绝不影响业务主流程**。

双重价值：
  1. 可观测性：AI 可用率 / 平均延迟 / 回退次数，服务端运维可查；
  2. 参赛自证：答辩现场可当场调取「provider=deepseek、latency、ok」证据，
     证明作品是「实际含 AI 功能」而非仅用 AI 生成代码。
"""
import json
import os
import threading

from src import diag

# 与 src 同级项目根下的 data/（开发 SQLite 同目录；已 gitignore）
LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'ai_call_log.jsonl')

LOG_MAX_BYTES = 1 * 1024 * 1024
LOG_BACKUPS = 3
_LOG_LOCK = threading.Lock()


def _rotate_log_if_needed():
    try:
        if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) < LOG_MAX_BYTES:
            return
        for idx in range(LOG_BACKUPS, 0, -1):
            src = LOG_PATH if idx == 1 else f'{LOG_PATH}.{idx - 1}'
            dst = f'{LOG_PATH}.{idx}'
            if not os.path.exists(src):
                continue
            if os.path.exists(dst):
                os.remove(dst)
            os.replace(src, dst)
    except OSError:
        pass


def record(scene, backend, latency_ms, ok, fallback=False):
    """追加一条调用记录（失败静默）。

    Args:
        scene: 调用场景标识（如 'chat'/'completion'/'quiz'）。
        backend: 后端信息 dict，含 'provider'（auto/dify/deepseek）、
            'source'（实际出口）与 'model'（模型名，可空）。
        latency_ms: 本次调用耗时（毫秒）。
        ok: 本次是否成功返回有效内容。
        fallback: 是否发生了回退（在线失败 → 降级）。
    """
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        info = backend or {}
        entry = {
            'ts': diag.utc_iso_z(),
            # 关联键：没有它，「这一页为什么慢 7 秒」只能靠时间戳猜（第 19 轮实测
            # 7249ms 的请求里两条留痕合计 7022ms，数据早就在，串不起来等于没有）
            'request_id': diag.get_request_id() or '-',
            'scene': scene,
            'provider': info.get('provider', ''),
            'source': info.get('source', ''),
            'model': info.get('model', ''),
            'latency_ms': int(latency_ms),
            'ok': bool(ok),
            'fallback': bool(fallback),
        }
        line = json.dumps(entry, ensure_ascii=False) + '\n'
        with _LOG_LOCK:
            _rotate_log_if_needed()
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line)
    except OSError:
        pass


def recent(limit=20):
    """读取最近 N 条记录（新 → 旧），供看板/答辩现场展示。

    Args:
        limit: 返回条数上限。

    Returns:
        list[dict]: 记录列表；文件不存在或损坏时返回已解析部分。
    """
    try:
        with open(LOG_PATH, 'rb') as f:
            try:
                f.seek(0, os.SEEK_END)
                tail = min(f.tell(), 64 * 1024)
                f.seek(-tail, os.SEEK_END)
            except OSError:
                f.seek(0)
            lines = f.read().decode('utf-8', errors='ignore').splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except (ValueError, TypeError):
            continue
    return out


def stats():
    """汇总可用率 / 平均延迟 / 回退次数。

    Returns:
        dict: {'ok', 'total', 'ok_rate', 'avg_latency_ms', 'fallbacks'}；
        无记录时 ok=False（调用方不得编造）。
    """
    entries = []
    try:
        with open(LOG_PATH, encoding='utf-8') as f:
            for raw in f:
                text = raw.strip()
                if not text:
                    continue
                try:
                    entries.append(json.loads(text))
                except (ValueError, TypeError):
                    continue
    except OSError:
        return {'ok': False, 'total': 0, 'ok_rate': None,
                'avg_latency_ms': None, 'fallbacks': 0}
    if not entries:
        return {'ok': False, 'total': 0, 'ok_rate': None,
                'avg_latency_ms': None, 'fallbacks': 0}
    ok_n = sum(1 for e in entries if e.get('ok'))
    lat = [int(e.get('latency_ms') or 0) for e in entries]
    return {
        'ok': True,
        'total': len(entries),
        'ok_rate': round(ok_n / len(entries) * 100, 1),
        'avg_latency_ms': round(sum(lat) / len(lat)),
        'fallbacks': sum(1 for e in entries if e.get('fallback')),
    }
