#!/usr/bin/env python
"""限流留痕断言（2026-09-18 双向迭代 S5）

覆盖三条边界：
  ① 触发即落盘，字段齐全；
  ② 只落指纹，绝不落原始 IP / 用户 id（隐私底线）；
  ③ 磁盘不可写时不得影响限流主流程（可观测性优先级低于可用性）。

用法：python test_rate_limit_trace.py   （退出码 0 = 全部通过）

门禁状态（M1 覆盖透明化）：**已纳管**——quality_gate 第 14 项「限流留痕」。
"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix='rate-trace-')
os.environ['RATE_TRACE_PATH'] = os.path.join(TMP, 'data', 'rate_limit_events.jsonl')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.rate_limit import (  # noqa: E402
    log_rate_limit_event,
    recent_rate_limit_events,
    RATE_TRACE_PATH,
)

passed = 0


def check(name, cond):
    global passed
    if cond:
        passed += 1
        print(f'  ✅ {name}')
    else:
        print(f'  ❌ {name}')
        sys.exit(1)


# ① 写入与字段
rec = log_rate_limit_event('login', 'login:anon:203.0.113.9', 10, 60)
check('留痕写入成功', rec is not None)
check('字段齐全（ts/action/key_fp/limit/window）',
      set(rec or {}) == {'ts', 'action', 'key_fp', 'limit', 'window'})
check('action/limit/window 正确', rec['action'] == 'login' and rec['limit'] == 10 and rec['window'] == 60)
check('落盘文件已生成', os.path.exists(RATE_TRACE_PATH))

# ② 脱敏
raw = open(RATE_TRACE_PATH, encoding='utf-8').read()
check('绝不落原始 IP', '203.0.113.9' not in raw)
check('key_fp 为 8 位指纹', len(rec['key_fp']) == 8)
log_rate_limit_event('admin_write', 'admin_write:admin-1:198.51.100.7', 60, 60)
raw2 = open(RATE_TRACE_PATH, encoding='utf-8').read()
check('绝不落用户 id（admin-1）', 'admin-1' not in raw2)

# ③ 读取与异常兜底
items = recent_rate_limit_events(10)
check('可读取最近留痕（2 条）', len(items) == 2)
check('读取内容为合法 JSON', all(isinstance(x, dict) and 'key_fp' in x for x in items))

# 指向一个已存在的目录（而非文件）→ open() 必然失败，用于验证兜底不抛异常
os.environ['RATE_TRACE_PATH'] = TMP
try:
    log_rate_limit_event('chat', 'chat:anon:1.2.3.4', 30, 60)
    check('写入不可用时静默兜底、不抛异常（限流主流程不受影响）', True)
except Exception:  # noqa: BLE001
    check('磁盘不可写时不抛异常（限流主流程不受影响）', False)

print(f'断言通过 {passed} 项')
