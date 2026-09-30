"""数据出入口留痕（对标第 20 轮 M68）—— 让"谁把数据取走了/谁灌进来的"可回答。

## 为什么单独有这一个文件

导出与导入是**数据的两次位移**，出纠纷时（"这份报表谁改的""这批流水怎么进来的"）
需要能回答 who / when / how many。此前全仓三处 JSONL 留痕（AI 调用、决策轨迹、
webhook 事件）都在**AI 侧**，而业务数据出口一处留痕都没有 —— 观测面偏了。

本模块与第 19 轮的 `request_id` 直接对接：留痕行带 `request_id`，
运维拿 `python log_triage.py --rid <id>` 就能把"这次导出"与"这段时间的服务端日志"
串起来，不需要另建关联机制。

## 三条硬约束（都来自本项目既有红线，不是新发明）

1. **不落业务明细**：只记计数、门店/账号 id、模式与结果，**不记 CSV 内容与产品名**。
   安全红线「商家业务明细不得暴露」对日志文件同样成立 —— 日志常被复制进工单，
   把业务数据写进日志等于开了第二条泄露通道。
2. **写失败静默但必须可数**：捕获 OSError 后记 `dropped` 计数（与 `src/diag.py`
   "丢弃不得静默"同口径），业务主流程绝不受影响：留痕是旁证，不是前置条件。
3. **有界**：单文件超限即轮转备份，防"日志把免费层磁盘吃满"（免费层唯一硬约束是磁盘）。
"""
import json
import os
import threading

from src import diag

LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'exit_audit.jsonl')
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUPS = 2
_LOCK = threading.Lock()
_counters = {'written': 0, 'dropped': 0}


def _rotate_if_needed():
    """轮转备份。**必须在调用方已持锁的前提下调用**，自己绝不再取锁 ——
    `threading.Lock` 不可重入，若在 `record()` 持锁时进来再抢锁就会自锁，
    而这条路径恰好在"日志写满、并发导出请求最多"时触发（自查出来的真缺陷，不是洁癖）。

    Returns:
        bool: False 表示轮转失败（由调用方计入 dropped，绝不静默）。
    """
    try:
        if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) < LOG_MAX_BYTES:
            return True
        for idx in range(LOG_BACKUPS, 0, -1):
            src = LOG_PATH if idx == 1 else f'{LOG_PATH}.{idx - 1}'
            dst = f'{LOG_PATH}.{idx}'
            if not os.path.exists(src):
                continue
            if os.path.exists(dst):
                os.remove(dst)
            os.replace(src, dst)
        return True
    except OSError:
        return False


def record(action, actor, stats=None, allowed=True):
    """追加一条出入口留痕。

    Args:
        action: `export_csv` / `import_csv` / `export_rejected` / `import_conflict` 等动作名。
        actor: 操作者标识，形如 `admin-1`；**由调用方给**，本模块不碰请求上下文，
            这样离线脚本与测试都能复用同一个函数（不依赖 current_user 的行为）。
        stats: 计数袋，允许键 `mode / store_id / rows / inserted / duplicates / failed / bytes`；
            缺省按 0/空处理，`inserted` 与 `duplicates` 未给时保持 None（表示本次不涉及落库，
            与"这次落了 0 行"是两种语义，不能混成一格）。
            刻意收成一个袋而不是摊成 8 个形参：摊开就撞 lint 的参数上限，
            而"改一行日志格式"本来就该只动一处。
        allowed: 越权校验是否通过（False 表示这是一次被拒绝的尝试，同样要留痕）。

    Returns:
        bool: True=已落盘；False=被丢弃（计数已累加，调用方可选择提示）。
    """
    bag = dict(stats or {})
    line = {
        'ts': diag.utc_iso_z(),
        'request_id': diag.get_request_id() or '-',
        'action': action,
        'mode': str(bag.get('mode') or ''),
        'actor': actor or '',
        'store_id': bag.get('store_id'),
        'rows': int(bag.get('rows') or 0),
        'inserted': None if bag.get('inserted') is None else int(bag['inserted']),
        'duplicates': None if bag.get('duplicates') is None else int(bag['duplicates']),
        'failed': int(bag.get('failed') or 0),
        'bytes': int(bag.get('bytes') or 0),
        'allowed': bool(allowed),
    }
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        payload = json.dumps(line, ensure_ascii=False, separators=(',', ':')) + os.linesep
        with _LOCK:
            rotated = _rotate_if_needed()
            with open(LOG_PATH, 'a', encoding='utf-8') as fh:
                fh.write(payload)
            if rotated:
                _counters['written'] += 1
            else:
                # 轮转失败：这行仍写进了主文件，但"有界"这一半已经失效 ⇒ 计丢弃并让判据可见
                _counters['dropped'] += 1
        return rotated
    except (OSError, TypeError, ValueError):
        with _LOCK:
            _counters['dropped'] += 1
        return False


def stats():
    """留痕自身健康度（写成功 / 丢弃计数），供管理员诊断端点与测试取用。"""
    with _LOCK:
        return dict(_counters)


def reset_counters():
    """测试专用：归零计数（不清文件，避免误删真实留痕）。"""
    with _LOCK:
        _counters['written'] = 0
        _counters['dropped'] = 0


def tail_lines(limit=20):
    """读最近若干行（管理员侧排障用）；文件不存在返回空列表而非报错。

    必须先丢掉尾随空行：写入用的是 `os.linesep`，文件末尾天然有一个空"行"，
    而 `splitlines()[-1:]` 恰好只取到它 ⇒ json.loads 失败被跳过 ⇒ 明明有数据却报空
    （实测：`limit=1` 时取不到任何行，`limit=3` 才碰巧读到 —— 这个差异就是当场定位的线索）。
    """
    try:
        with open(LOG_PATH, encoding='utf-8') as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        return []
    out = []
    for raw in lines[-limit:]:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue                     # 半行（轮转竞态）跳过，不炸调用方
    return out


__all__ = ['LOG_BACKUPS', 'LOG_MAX_BYTES', 'LOG_PATH', 'record',
           'reset_counters', 'stats', 'tail_lines']
