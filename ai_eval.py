"""AI 证据化评测 CLI —— 用合成场景证明「AI 用得深」且「失败可控」。

设计目标（对应交付材料中的技术实现证据）：
  1. **离线可复现**：不依赖网络与数据库即可跑完全部场景，任何机器 clone 后可复现。
  2. **故障注入**：按角色注入非法 JSON、超时、运行时异常、越界证据、超配额等，
     验证每条降级路径都真实生效，而不是「写在文档里的承诺」。
  3. **固定输出契约**：JSON 字段固定为
     场景 / 结构解析 / 证据合法率 / 越界丢弃数 / 角色隔离 / 延迟 / Token / 降级状态 / 结论。
  4. **零副作用**：全部场景以 write_trace=False 运行，不写决策轨迹、不写数据库。

用法：
    python ai_eval.py                    # 离线全量（20 场景）
    python ai_eval.py --online 3         # 追加在线双角色延迟复测（需 DEEPSEEK_API_KEY）
    python ai_eval.py --out-dir engineering-assurance

退出码：0 = 全部场景通过且验收线全绿；1 = 存在不通过项。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ── 验收线（来自复赛冲刺计划的 AI 验收标准）──
TARGET_P95_MS = 15000          # 在线双角色 P95 延迟上限
JSON_SUCCESS_FLOOR = 0.95      # 正常场景 JSON 契约成功率下限
EXPECTED_ROLE_COUNT = 4        # 四角色必须齐全

REQUIRED_PATHS = (
    '离线/未接入在线 AI',
    '配额超限',
    '模型超时',
    '非法 JSON / 契约不符',
    '单角色失败（另一角色仍为模型）',
    '越界证据全部被拒 → 降级',
    '无数据（区间内无有效营业记录）',
)

# ── 合成数据集（纯合成，不含任何真实客户或个人数据）──
BASE_ROWS = (
    # (store_id, 店名, 营收, 记录数, 损耗率, 培训均分)   损耗率 None = 该店无损耗维度
    (101, '智电轻擎·中心店', 128000.0, 310, 0.11, 86.5),
    (102, '智电轻擎·东湖店', 96500.0, 268, 0.13, 82.0),
    (103, '智电轻擎·南湖店', 74200.0, 240, 0.22, 74.5),
    (104, '智电轻擎·北城店', 51800.0, 205, 0.09, 88.0),
    (105, '智电轻擎·西园店', 38900.0, 176, 0.16, 68.0),
    (106, '智电轻擎·湖畔店', 0.0, 0, None, None),
    (107, '智电轻擎·城南店', 87400.0, 251, 0.12, 91.0),
)
BASE_ALERTS = (
    {'kind': 'waste_rate', 'store_id': 103, 'store_name': '智电轻擎·南湖店',
     'value': 0.22, 'threshold': 0.15,
     'evidence': [{'metric': 'waste_rate', 'value': 0.22}]},
    {'kind': 'revenue_drop_3d', 'store_id': 105, 'store_name': '智电轻擎·西园店',
     'value': 0.35, 'threshold': 0.2,
     'evidence': [{'metric': 'revenue_drop_3d', 'value': 0.35}]},
)


def _row_tuple(item):
    sid, name, revenue, records, waste, quiz = item
    return {'store_id': sid, 'store_name': name, 'revenue': revenue,
            'records': records, 'waste_rate': waste, 'quiz_avg': quiz,
            'has_data': records > 0}


def build_snapshot(rows, alerts, days=30):
    """按 ``decision_cockpit.build_snapshot`` 同构规则构建合成快照（不查数据库）。

    证据目录的编号顺序必须与生产实现一致：先按排行逐店写入 revenue / records /
    waste_rate / quiz_avg，再写入异常项，同 (门店, 指标) 去重。

    Args:
        rows: 门店排行行（已含 has_data）。
        alerts: 异常项。
        days: 统计区间。

    Returns:
        tuple: ``(snapshot, id_map)``；id_map 为 ``{(指标, 门店): 证据 id}``。
    """
    catalog, lookup = [], {}
    id_map = {}

    def add(store_id, metric, value):
        try:
            sid = int(store_id)
        except (TypeError, ValueError):
            return None
        key = (sid, str(metric))
        if key in lookup:
            return lookup[key]
        evidence_id = f'ev-{len(catalog) + 1}'
        lookup[key] = evidence_id
        id_map[(str(metric), sid)] = evidence_id
        catalog.append({'id': evidence_id, 'store_id': sid, 'metric': str(metric),
                        'value': value, 'period': f'近{days}天'})
        return evidence_id

    for row in rows:
        add(row['store_id'], 'revenue', row.get('revenue'))
        add(row['store_id'], 'records', row.get('records'))
        if row.get('waste_rate') is not None:
            add(row['store_id'], 'waste_rate', row.get('waste_rate'))
        if row.get('quiz_avg') is not None:
            add(row['store_id'], 'quiz_avg', row.get('quiz_avg'))
    for alert in alerts:
        evidence = (alert.get('evidence') or [{}])[0]
        add(alert.get('store_id'),
            evidence.get('metric') or alert.get('kind'),
            evidence.get('value', alert.get('value')))

    snapshot = {'store_ids': sorted({int(r['store_id']) for r in rows}),
                'days': int(days), 'ranking': rows, 'alerts': alerts,
                'evidence': catalog}
    return snapshot, id_map


class QuotaStub:
    """替换 ``decision_cockpit.ai_quota``：完全进程内，不触碰数据库。"""

    KIND_DECISION = 'decision'

    def __init__(self, ok=True, reason='ok'):
        self.ok = bool(ok)
        self.reason = str(reason)
        self.recorded = []
        self._cache = {}

    def check_quota(self, merchant_id, kind=None):
        return {'ok': self.ok, 'reason': self.reason,
                'used': 0 if self.ok else 10, 'limit': 10,
                'tokens_used': 0, 'token_limit': 120000}

    def cache_get(self, merchant_id, kind, key):
        return self._cache.get((int(merchant_id), str(kind), str(key)))

    def cache_put(self, merchant_id, kind, key, value):
        self._cache[(int(merchant_id), str(kind), str(key))] = value
        return value

    def record_usage(self, merchant_id, kind=None, **kwargs):
        self.recorded.append({'merchant_id': merchant_id, 'kind': kind, **kwargs})
        return len(self.recorded)


def _contract_of(prompt):
    """从提示词识别角色契约（两个角色的输出结构必须不同）。"""
    if '"proposals"' in prompt:
        return 'proposals'
    if '"risks"' in prompt:
        return 'risks'
    return 'unknown'


class Recorder:
    """记录模型调用（角色 / 线程 / 契约），用于验证「两个隔离调用」。"""

    def __init__(self, cite, behaviors, tokens=120):
        self.calls = []
        self.cite = cite or {}
        self.behaviors = behaviors or {}
        self.tokens = int(tokens)

    def __call__(self, prompt, role):
        self.calls.append({'role': role, 'thread': threading.current_thread().name,
                           'contract': _contract_of(prompt)})
        behavior = self.behaviors.get(role, 'valid')
        if behavior == 'timeout':
            raise TimeoutError('injected_timeout')
        if behavior == 'runtime_error':
            raise RuntimeError('injected_role_failure')
        if behavior == 'not_ok':
            return {'ok': False, 'content': '', 'source': 'error', 'tokens': 0}
        if behavior == 'invalid_json':
            return self._wrap('not-a-json-payload{')
        if behavior == 'wrong_shape':
            return self._wrap(json.dumps({'foo': 'bar'}, ensure_ascii=False))
        if behavior == 'wrong_type':
            return self._wrap(json.dumps({'proposals': 'oops'}, ensure_ascii=False))
        cite = list(self.cite.get(role) or [])
        if behavior == 'extra_scope':
            cite = cite + ['ev-9999']
        elif behavior == 'all_scope':
            cite = ['ev-9999', 'ev-8888']
        elif behavior == 'no_evidence':
            cite = []
        if behavior == 'slow_valid':
            time.sleep(0.2)
        return self._wrap(json.dumps(self._payload(role, cite), ensure_ascii=False))

    def _wrap(self, content):
        return {'ok': True, 'content': content, 'source': 'deepseek',
                'tokens': self.tokens}

    @staticmethod
    def _payload(role, cite):
        if role == 'advisor':
            return {'proposals': [{'action': '复盘高损耗门店的备货与储存流程',
                                   'rationale': '损耗率高于行业包阈值，需先核查备货量与储存条件。',
                                   'evidence_ids': list(cite),
                                   'priority': 'high'}]}
        return {'risks': [{'risk': '损耗可能继续扩大',
                           'severity': 'high',
                           'evidence_ids': list(cite),
                           'recommendation': '先核查库存与储存条件，再决定是否调整备货。'}]}


def _scan_evidence(result):
    """统计结果中引用的证据 id，返回 ``(全部引用, 越界引用)``。"""
    catalog = {item['id'] for item in result.get('evidence') or []}
    referenced, illegal = [], []
    for role in result.get('roles') or []:
        items = (role.get('claims') or role.get('proposals')
                 or role.get('risks') or [])
        for item in items:
            for evidence_id in item.get('evidence_ids') or []:
                referenced.append(evidence_id)
                if evidence_id not in catalog:
                    illegal.append(evidence_id)
    return referenced, illegal


def _role_snapshot(result):
    """整理四角色的降级状态，供固定 JSON 字段「降级状态」使用。"""
    states = {}
    for role in result.get('roles') or []:
        states[role.get('label') or role.get('role')] = {
            '来源': role.get('source'),
            '状态': role.get('status'),
            '错误': role.get('error'),
            '丢弃越界证据': int(role.get('discarded_evidence') or 0),
            'Token': int(role.get('tokens') or 0),
        }
    return states


def build_scenarios(datasets):
    """构造 20 个合成场景。

    Args:
        datasets: ``{名称: (snapshot, id_map)}``。

    Returns:
        list[dict]: 场景定义列表。
    """
    base, bid = datasets['base']
    nodata, _ = datasets['nodata']
    empty, _ = datasets['empty']

    def cite(dataset_id, metric, store_id):
        return dataset_id[(metric, store_id)]

    waste103 = cite(bid, 'waste_rate', 103)
    rev101 = cite(bid, 'revenue', 101)
    quiz105 = cite(bid, 'quiz_avg', 105)

    def scenario(sid, name, *, dataset='base', allow_ai=True, quota_ok=True,
                 behavior=None, citations=None, benign=False, fault=True,
                 expect=None, second_run=False):
        return {'id': sid, 'name': name, 'dataset': dataset, 'allow_ai': allow_ai,
                'quota_ok': quota_ok, 'responses': behavior or {},
                'cite': citations or {}, 'benign': benign, 'fault': fault,
                'expect': expect or {}, 'second_run': second_run}

    return [
        scenario('S01', '基线：两个角色合法输出 + 四角色完整', benign=True, fault=False,
                 behavior={'advisor': 'valid', 'auditor': 'valid'},
                 citations={'advisor': [rev101], 'auditor': [waste103]},
                 expect={'llm_calls': 2, 'advisor_source': 'ai', 'auditor_source': 'ai',
                         'advisor_status': 'ok', 'auditor_status': 'ok',
                         'discarded': 0, 'decision_status': 'consensus'}),
        scenario('S02', '分歧：建议与高风险共享同一组证据', benign=True, fault=False,
                 behavior={'advisor': 'valid', 'auditor': 'valid'},
                 citations={'advisor': [waste103], 'auditor': [waste103]},
                 expect={'llm_calls': 2, 'decision_status': 'disputed', 'discarded': 0}),
        scenario('S03', '共识：建议证据与高风险证据不相交', benign=True, fault=False,
                 behavior={'advisor': 'valid', 'auditor': 'valid'},
                 citations={'advisor': [rev101], 'auditor': [waste103]},
                 expect={'llm_calls': 2, 'decision_status': 'consensus'}),
        scenario('S04', '非法 JSON：经营参谋返回不可解析内容', fault=True,
                 behavior={'advisor': 'invalid_json', 'auditor': 'valid'},
                 citations={'auditor': [waste103]},
                 expect={'llm_calls': 2, 'advisor_source': 'fallback',
                         'advisor_status': 'error', 'auditor_source': 'ai',
                         'path': '非法 JSON / 契约不符'}),
        scenario('S05', '非法 JSON：风险审计员返回结构不符', fault=True,
                 behavior={'advisor': 'valid', 'auditor': 'wrong_type'},
                 citations={'advisor': [rev101]},
                 expect={'llm_calls': 2, 'auditor_source': 'fallback',
                         'auditor_status': 'error', 'advisor_source': 'ai',
                         'path': '非法 JSON / 契约不符'}),
        scenario('S06', '超时：经营参谋超时（另一角色保持模型结果）', fault=True,
                 behavior={'advisor': 'timeout', 'auditor': 'valid'},
                 citations={'auditor': [waste103]},
                 expect={'llm_calls': 2, 'advisor_status': 'timeout',
                         'auditor_source': 'ai', 'path': '模型超时'}),
        scenario('S07', '超时：风险审计员超时', fault=True,
                 behavior={'advisor': 'valid', 'auditor': 'timeout'},
                 citations={'advisor': [rev101]},
                 expect={'llm_calls': 2, 'auditor_status': 'timeout',
                         'advisor_source': 'ai', 'path': '模型超时'}),
        scenario('S08', '单角色失败：接口返回 ok=false', fault=True,
                 behavior={'advisor': 'not_ok', 'auditor': 'valid'},
                 citations={'auditor': [waste103]},
                 expect={'llm_calls': 2, 'advisor_source': 'fallback',
                         'auditor_source': 'ai',
                         'path': '单角色失败（另一角色仍为模型）'}),
        scenario('S09', '越界证据：部分越界被丢弃，合法结论保留', benign=True, fault=False,
                 behavior={'advisor': 'extra_scope', 'auditor': 'valid'},
                 citations={'advisor': [rev101], 'auditor': [waste103]},
                 expect={'llm_calls': 2, 'advisor_source': 'ai', 'discarded': 1}),
        scenario('S10', '越界证据：全部越界 → 降级且丢弃数仍可见', fault=True,
                 behavior={'advisor': 'all_scope', 'auditor': 'valid'},
                 citations={'auditor': [waste103]},
                 expect={'llm_calls': 2, 'advisor_source': 'fallback',
                         'discarded': 2, 'path': '越界证据全部被拒 → 降级'}),
        scenario('S11', '契约边界：证据列表为空 → 该条建议被丢弃', fault=True,
                 behavior={'advisor': 'no_evidence', 'auditor': 'valid'},
                 citations={'auditor': [waste103]},
                 expect={'llm_calls': 2, 'advisor_source': 'fallback',
                         'path': '非法 JSON / 契约不符'}),
        scenario('S12', '契约不符：缺少 proposals 字段', fault=True,
                 behavior={'advisor': 'wrong_shape', 'auditor': 'valid'},
                 citations={'auditor': [waste103]},
                 expect={'llm_calls': 2, 'advisor_source': 'fallback',
                         'path': '非法 JSON / 契约不符'}),
        scenario('S13', '无数据：授权门店在区间内无有效营业记录',
                 dataset='nodata', allow_ai=False, fault=False, benign=True,
                 expect={'llm_calls': 0, 'decision_status': 'no_action',
                         'advisor_source': 'fallback',
                         'path': '无数据（区间内无有效营业记录）'}),
        scenario('S14', '离线：未接入在线 AI（规则版全流程）', allow_ai=False,
                 fault=False, benign=True,
                 expect={'llm_calls': 0, 'advisor_source': 'fallback',
                         'auditor_source': 'fallback', 'analyst_source': 'rule',
                         'path': '离线/未接入在线 AI'}),
        scenario('S15', '配额耗尽：不再调用模型，回落规则版', quota_ok=False,
                 fault=True,
                 behavior={'advisor': 'valid', 'auditor': 'valid'},
                 citations={'advisor': [rev101], 'auditor': [waste103]},
                 expect={'llm_calls': 0, 'advisor_source': 'fallback',
                         'auditor_source': 'fallback', 'path': '配额超限'}),
        scenario('S16', '缓存命中：10 分钟内第二次不重复调用模型', benign=True,
                 fault=False, second_run=True,
                 behavior={'advisor': 'valid', 'auditor': 'valid'},
                 citations={'advisor': [rev101], 'auditor': [waste103]},
                 expect={'llm_calls': 2, 'second_run_calls': 0}),
        scenario('S17', '边界：授权门店集合为空', dataset='empty', allow_ai=False,
                 fault=False, benign=True,
                 expect={'llm_calls': 0, 'decision_status': 'no_action',
                         'path': '无数据（区间内无有效营业记录）'}),
        scenario('S18', '越界证据：风险审计员全部越界', fault=True,
                 behavior={'advisor': 'valid', 'auditor': 'all_scope'},
                 citations={'advisor': [rev101]},
                 expect={'llm_calls': 2, 'auditor_source': 'fallback',
                         'discarded': 2, 'path': '越界证据全部被拒 → 降级'}),
        scenario('S19', '延迟采样：单角色 200ms 后正常返回', benign=True, fault=False,
                 behavior={'advisor': 'slow_valid', 'auditor': 'valid'},
                 citations={'advisor': [quiz105], 'auditor': [waste103]},
                 expect={'llm_calls': 2, 'advisor_source': 'ai'}),
        scenario('S20', '全量证据（27 条）下证据合法率与角色隔离复核', benign=True,
                 fault=False,
                 behavior={'advisor': 'valid', 'auditor': 'valid'},
                 citations={'advisor': [rev101, waste103], 'auditor': [waste103, quiz105]},
                 expect={'llm_calls': 2, 'discarded': 0, 'advisor_source': 'ai',
                         'auditor_source': 'ai'}),
    ]


def run_scenarios(dcp, datasets, scenarios):
    """执行全部场景，返回 ``(场景结果列表, 汇总统计)``。"""
    records = []
    for spec in scenarios:
        snapshot, _ = datasets[spec['dataset']]
        quota = QuotaStub(ok=spec['quota_ok'],
                          reason='ok' if spec['quota_ok'] else 'quota_exceeded')
        original_quota = dcp.ai_quota
        original_snapshot = dcp.build_snapshot
        dcp.ai_quota = quota
        dcp.build_snapshot = lambda store_ids, days=30, today=None, _s=snapshot: _s
        recorder = Recorder(spec['cite'], spec['responses'])
        dcp.set_llm(recorder if spec['allow_ai'] else None)
        try:
            started = time.perf_counter()
            result = dcp.run_decision(1, snapshot['store_ids'], days=30,
                                      refresh=True, allow_ai=spec['allow_ai'],
                                      write_trace=False)
            elapsed_ms = (time.perf_counter() - started) * 1000
            second_calls = None
            if spec['second_run']:
                before = len(recorder.calls)
                dcp.run_decision(1, snapshot['store_ids'], days=30, refresh=False,
                                 allow_ai=True, write_trace=False)
                second_calls = len(recorder.calls) - before
        finally:
            dcp.set_llm(None)
            dcp.ai_quota = original_quota
            dcp.build_snapshot = original_snapshot

        referenced, illegal = _scan_evidence(result)
        legal_rate = ((len(referenced) - len(illegal)) / len(referenced)
                      if referenced else None)
        states = _role_snapshot(result)
        roles_called = sorted({c['role'] for c in recorder.calls})
        threads = sorted({c['thread'] for c in recorder.calls})
        contracts = sorted({c['contract'] for c in recorder.calls})

        metrics = {
            'llm_calls': len(recorder.calls),
            'second_run_calls': second_calls,
            'roles_called': roles_called,
            'threads': threads,
            'contracts': contracts,
            'referenced': referenced,
            'illegal': illegal,
            'legal_rate': legal_rate,
            'discarded': sum(v['丢弃越界证据'] for v in states.values()),
            'tokens': sum(v['Token'] for v in states.values()),
            'elapsed_ms': round(elapsed_ms, 3),
            'states': states,
            'decision_status': (result.get('decision') or {}).get('status'),
            'sources': result.get('sources') or {},
            'role_count': len(result.get('roles') or []),
        }
        failures = _check(spec, metrics)
        records.append({'spec': spec, 'metrics': metrics, 'failures': failures})
    return records


def _check(spec, metrics):
    """按场景预期逐条断言，返回失败说明列表。"""
    exp = spec['expect']
    failures = []
    if exp.get('llm_calls') is not None and metrics['llm_calls'] != exp['llm_calls']:
        failures.append(f"模型调用次数 {metrics['llm_calls']} ≠ 预期 {exp['llm_calls']}")
    if (exp.get('second_run_calls') is not None
            and metrics['second_run_calls'] != exp['second_run_calls']):
        failures.append(f"二次运行调用 {metrics['second_run_calls']} "
                        f"≠ 预期 {exp['second_run_calls']}（缓存未生效）")
    for key, role in (('advisor_source', 'advisor'), ('auditor_source', 'auditor'),
                      ('analyst_source', 'data_analyst')):
        want = exp.get(key)
        if want is not None and metrics['sources'].get(role) != want:
            failures.append(f'{role} 来源 {metrics["sources"].get(role)} ≠ 预期 {want}')
    for key, label in (('advisor_status', '经营参谋'), ('auditor_status', '风险审计员')):
        want = exp.get(key)
        if want is not None and (metrics['states'].get(label) or {}).get('状态') != want:
            failures.append(f'{label} 状态 {(metrics["states"].get(label) or {}).get("状态")}'
                            f' ≠ 预期 {want}')
    if exp.get('discarded') is not None and metrics['discarded'] != exp['discarded']:
        failures.append(f"越界丢弃数 {metrics['discarded']} ≠ 预期 {exp['discarded']}")
    if exp.get('decision_status') is not None:
        if metrics['decision_status'] != exp['decision_status']:
            failures.append(f"仲裁状态 {metrics['decision_status']} "
                            f"≠ 预期 {exp['decision_status']}")
    if metrics['role_count'] != EXPECTED_ROLE_COUNT:
        failures.append(f"角色数 {metrics['role_count']} ≠ {EXPECTED_ROLE_COUNT}")
    if metrics['illegal']:
        failures.append(f'结果中保留了越界证据：{metrics["illegal"]}')
    if metrics['llm_calls'] == 2:
        if len(metrics['roles_called']) != 2:
            failures.append(f'未触发两个角色各自的调用：{metrics["roles_called"]}')
        if len(metrics['contracts']) != 2:
            failures.append(f'两个角色契约未区分（疑似同一输出拆分）：{metrics["contracts"]}')
    return failures


def online_probe(dcp, snapshot, rounds):
    """在线双角色延迟复测：真实模型调用，测量 P95 延迟。

    Args:
        dcp: decision_cockpit 模块。
        snapshot: 合成快照（只用其提示词，不写库、不写轨迹）。
        rounds: 重复轮数。

    Returns:
        dict: ``{'可用': bool, '原因'/'延迟毫秒': ..., '模型': ...}``。
    """
    try:
        from flask import Flask
        from src.config import config_map
        from src.dify_client import dify_client
    except Exception as exc:  # noqa: BLE001
        return {'可用': False, '原因': f'依赖导入失败：{exc.__class__.__name__}'}

    app = Flask(__name__)
    app.config.from_object(config_map['development'])
    dify_client.init_app(app)
    if not dify_client.enabled:
        return {'可用': False, '原因': '未配置在线模型 Key（DIFY_ENABLED=False）'}

    prompts = dcp.role_prompts(snapshot)
    latencies, tokens, successes = [], 0, 0
    for _ in range(max(1, int(rounds))):
        for role, prompt in prompts.items():
            client = dify_client.fork()
            started = time.perf_counter()
            payload = client.decision_completion(prompt, role)
            latencies.append((time.perf_counter() - started) * 1000)
            if isinstance(payload, dict) and payload.get('ok'):
                successes += 1
                tokens += int(payload.get('tokens') or 0)
    return {'可用': True, '模型': app.config.get('DEEPSEEK_MODEL'),
            '轮数': int(rounds), '调用次数': len(latencies),
            '成功次数': successes, 'Token': tokens,
            '延迟P95毫秒': percentile(latencies, 0.95),
            '延迟中位数毫秒': round(statistics.median(latencies), 1)}


def percentile(values, quantile):
    """最近秩法（nearest-rank）取分位数，样本少时不会插值出虚假精度。

    Args:
        values: 数值序列（内部会排序，不修改入参）。
        quantile: 分位，取值 (0, 1]。

    Returns:
        float: 分位数值；空序列返回 0.0。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1,
                       int(round(quantile * len(ordered))) - 1))
    return round(ordered[index], 1)


def _git_commit():
    """读取当前代码提交号（无 git 时返回 'unknown'）。

    仓库根可能在脚本目录的祖先层级（交付副本是工作区根仓库里的普通子目录），
    因此逐级上溯找 `.git`，而不是只看脚本同级。
    """
    here = REPO_ROOT
    for candidate in [here, *here.parents]:
        head = candidate / '.git' / 'HEAD'
        if not head.is_file():
            continue
        try:
            raw = head.read_text(encoding='utf-8').strip()
        except OSError:
            return 'unknown'
        if raw.startswith('ref:'):
            ref = raw.split(':', 1)[1].strip()
            try:
                return (candidate / '.git' / ref).read_text(encoding='utf-8').strip()
            except OSError:
                return 'unknown'
        return raw
    return 'unknown'


def _env_candidates(explicit):
    if explicit:
        return [Path(explicit)]
    workspace = REPO_ROOT.parents[2] if len(REPO_ROOT.parents) > 2 else None
    candidates = []
    if workspace:
        candidates.append(workspace / 'iCAN大学生创新创业大赛' / '.env')
        candidates.append(workspace / '.env')
    candidates.append(REPO_ROOT / '.env')
    return candidates


def _load_env(explicit):
    """在导入 src 之前加载 .env（config 在导入期即读取环境变量）。"""
    for candidate in _env_candidates(explicit):
        if candidate and candidate.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(candidate, override=False)
            except ImportError:
                return None
            return str(candidate)
    return None


def summarize(records, online, isolation, faith=None):
    """汇总统计与验收结论。"""
    benign_calls = 0
    benign_ok = 0
    total_calls = 0
    total_tokens = 0
    referenced_total = 0
    illegal_total = 0
    retained_out_of_scope = 0
    latencies = []
    covered_paths = set()
    passed = 0

    for record in records:
        metrics = record['metrics']
        spec = record['spec']
        total_calls += metrics['llm_calls']
        total_tokens += metrics['tokens']
        referenced_total += len(metrics['referenced'])
        illegal_total += len(metrics['illegal'])
        retained_out_of_scope += len(metrics['illegal'])
        latencies.append(metrics['elapsed_ms'])
        if spec['benign']:
            benign_calls += metrics['llm_calls']
            benign_ok += metrics['llm_calls'] - _failed_model_responses(metrics)
        if spec['expect'].get('path'):
            covered_paths.add(spec['expect']['path'])
        if not record['failures']:
            passed += 1

    json_rate = (benign_ok / benign_calls) if benign_calls else None
    legal_rate = ((referenced_total - illegal_total) / referenced_total
                  if referenced_total else None)

    acceptance = [
        {'验收线': '证据引用非法数为 0', '要求': '= 0',
         '实测': str(illegal_total), '结论': '通过' if illegal_total == 0 else '不通过'},
        {'验收线': '越界证据保留数为 0', '要求': '= 0',
         '实测': str(retained_out_of_scope),
         '结论': '通过' if retained_out_of_scope == 0 else '不通过'},
        {'验收线': '两个模型角色使用独立客户端实例', '要求': 'fork() 互不相同',
         '实测': (f"客户端类型 {isolation.get('客户端类型')}，互不相同 "
                  f"{isolation.get('互不相同')}" if isolation.get('可用')
                  else f"未验证：{isolation.get('原因')}"),
         '结论': '通过' if isolation.get('互不相同') else '不通过'},
        {'验收线': '降级路径覆盖 100%', '要求': f'{len(REQUIRED_PATHS)}/{len(REQUIRED_PATHS)}',
         '实测': f'{len(covered_paths)}/{len(REQUIRED_PATHS)}',
         '结论': '通过' if covered_paths == set(REQUIRED_PATHS) else '不通过'},
        {'验收线': f'JSON 契约成功率 ≥ {JSON_SUCCESS_FLOOR:.0%}（正常场景）',
         '要求': f'≥ {JSON_SUCCESS_FLOOR:.0%}',
         '实测': f'{json_rate:.1%}' if json_rate is not None else '无样本',
         '结论': ('通过' if json_rate is not None and json_rate >= JSON_SUCCESS_FLOOR
                  else '不通过')},
        {'验收线': f'在线双角色 P95 延迟 ≤ {TARGET_P95_MS / 1000:.0f} 秒',
         '要求': f'≤ {TARGET_P95_MS} ms',
         '实测': (f"{online.get('延迟P95毫秒')} ms（{online.get('调用次数')} 次真实调用）"
                  if online.get('可用') else f"未复测：{online.get('原因')}"),
         '结论': ('通过' if online.get('可用') and online.get('延迟P95毫秒') <= TARGET_P95_MS
                  else ('待复测' if not online.get('可用') else '不通过'))},
        {'验收线': '语义忠实（零 Token 数字回算）',
         '要求': f'= 100% 且关系式全一致，主张数 ≥ {FAITH_MIN_CLAIMS}',
         '实测': (
             '未测到' if not faith or not faith.get('可用') else
             (f"{faith['语义忠实率']:.1%}（{faith['已核对']}/{faith['数字主张总数']} 条数字主张）"
              f"＋关系式 {faith['关系式核对项'] - len(faith['关系式不一致项'])}/"
              f"{faith['关系式核对项']}")),
         '结论': ('通过' if faith and faith.get('可用') and faith.get('通过')
                  else ('未测到' if not faith or not faith.get('可用') else '不通过'))},
        {'验收线': '场景全部通过', '要求': f'{len(records)}/{len(records)}',
         '实测': f'{passed}/{len(records)}',
         '结论': '通过' if passed == len(records) else '不通过'},
    ]
    summary = {
        '场景总数': len(records),
        '通过': passed,
        '不通过': len(records) - passed,
        '证据引用总数': referenced_total,
        '证据引用非法数': illegal_total,
        '越界证据保留数': retained_out_of_scope,
        '证据合法率': f'{legal_rate:.1%}' if legal_rate is not None else '无样本',
        '模型调用总数（离线注入）': total_calls,
        'Token 总数（离线注入）': total_tokens,
        'JSON 契约成功率（正常场景）': f'{json_rate:.1%}' if json_rate is not None else '无样本',
        '降级路径覆盖': f'{len(covered_paths)}/{len(REQUIRED_PATHS)}',
        '语义忠实率': (('未测到' if not faith or not faith.get('可用')
                       else ('—' if faith.get('语义忠实率') is None
                             else f"{faith['语义忠实率']:.1%}"))),
        '数字主张总数': (faith.get('数字主张总数') if faith and faith.get('可用') else 0),
        '关系式核对项': (f"{faith['关系式核对项'] - len(faith['关系式不一致项'])}/"
                        f"{faith['关系式核对项']}" if faith and faith.get('可用') else '未测到'),
        '离线场景延迟 P95 毫秒': percentile(latencies, 0.95),
        '客户端实例隔离': isolation,
        '在线复测': online,
    }
    return summary, acceptance, sorted(covered_paths)


def _isolation_text(metrics):
    """判定本条场景是否构成「两个角色各自独立调用」。

    **不把线程名当作隔离证据**：实测发现 ThreadPoolExecutor 会复用工作线程，
    两个快速返回的角色可能落在同一个 ``decision-role_0`` 线程上。
    可靠的隔离证据是「两个不同角色 + 两份互不相同的输出契约 + 各一次调用」。

    Args:
        metrics: 场景指标。

    Returns:
        str: 隔离判定说明。
    """
    if metrics['llm_calls'] == 0:
        return '不适用（未调用模型）'
    if len(metrics['roles_called']) == 2 and len(metrics['contracts']) == 2:
        return '两角色各自调用 / 契约互异'
    return '单角色调用'


def client_isolation_probe():
    """验证两个模型角色使用独立客户端实例（离线可验证）。

    Returns:
        dict: ``{'可用': bool, '互不相同': bool, ...}``。
    """
    try:
        from src.dify_client import dify_client
    except Exception as exc:  # noqa: BLE001
        return {'可用': False, '原因': f'导入失败：{exc.__class__.__name__}'}
    first, second = dify_client.fork(), dify_client.fork()
    return {'可用': True, '互不相同': first is not second,
            '客户端类型': type(first).__name__}


# ══ 语义忠实（零 Token 确定性数字回算）════════════════════════════════
# 对标实读（2026-09-30 经 `gh api` 取各仓 main 分支字节，非二手转述）：
#   · ragas `src/ragas/metrics/_faithfulness.py:34-37` 先把结论拆成陈述句，`:58-61` 逐句给 0/1
#     verdict，`:182-194` 得分 = 忠实句数 / 句数，`:191-192` **一句都没有时返回 nan 而不是通过**；
#   · ragas `collections/datacompy_score/metric.py:15,60` 是全部对标对象里唯一**不经 LLM**
#     的核对先例（参考表 vs 响应表，precision/recall/F1）；
#   · promptfoo `assertions/contextFaithfulness.ts:37-63` → `matchers/rag.ts:392-515`
#     把「没判定到的」也计入分母，防止比率被一堆 trivial 陈述句抬高。
# 本项目取「verdict 数组 → 比率」这个骨架，把判官从模型换成**同一份数据的独立复算**：
# 零 Token、可重跑、无判官方差（ragas 要靠双判官取均值压方差的那类问题在这里不存在）。
# 边界（诚实声明）：按「值」对账不做「归因」，所以一个编造的数字若恰好等于本文案里另一个
# 合法值，本判据看不见——那一类由 `consistency_checks()` 的关系式与 L4 真面腿负责。

FAITHFUL_RATIO_FLOOR = 1.0     # 地板取满分：结论全部由模板生成，不存在「合理误差」区间
# 分母下限：夹具实测 41 条数字主张，取 30 为地板（留 27% 余量）。
# 没有这条下限，「模板缩水到只剩一个数字」也会被判 100% —— 比率满分不等于覆盖满分。
FAITH_MIN_CLAIMS = 30
NUMBER_TOKEN_RX = re.compile(r'\d[\d,]*(?:\.\d+)?')

# 模板写死、与业务数据无关的量。**逐个给理由**，禁止用通配豁免（豁免一旦成串，
# 分母就被掏空，比率虚高正是 ragas/promptfoo 都在防的那个失败模式）。
STRUCTURAL_LITERALS = {
    7.0: '「近 7 日」窗口长度（_trend_desc / _compare_text / _forecast_text 固定话术）',
    14.0: '「前 7 日」需要 14 日回看',
    3.0: 'TOP3 / 损耗产品前 3 截断',
    4.0: '销售结构取饼图前 4',
    2.0: '备货建议取前 2 项',
    95.0: '安全库存服务水平 95%（_replenish_text 固定话术）',
    1.1: '备货倍率下限（模板尾句固定话术）',
    1.2: '备货倍率上限（模板尾句固定话术）',
}


def numeric_tokens(text):
    """抽取文案里的每个数字主张（分母由文案本身得出，不由人工清单抄录）。"""
    out = []
    for m in NUMBER_TOKEN_RX.finditer(text or ''):
        raw = m.group()
        try:
            value = float(raw.replace(',', ''))
        except ValueError:
            continue
        out.append({'原文': raw, '值': value, '位置': m.start()})
    return out


def _date_parts(stats):
    """把文案会原样打印的日期串拆成数字分量（日期是数据标识，不是业务量）。"""
    parts = []
    for d in ((stats.get('daily') or {}).get('dates') or []):
        for token in re.split(r'[^0-9]+', str(d)):
            if token:
                parts.append(('日期分量(daily.dates)', float(token)))
    for p in ((stats.get('anomalies') or {}).get('points') or []):
        for token in re.split(r'[^0-9]+', str(p.get('date') or '')):
            if token:
                parts.append(('日期分量(anomalies.points)', float(token)))
    return parts


def allowed_numeric_values(stats):
    """由 stats 独立得出「允许出现在文案里的数字」集合：直传字段 + 派生复算 + 结构常量。"""
    kpi = stats.get('kpi') or {}
    allowed = [('kpi.total_revenue', kpi.get('total_revenue')),
               ('kpi.total_qty', kpi.get('total_qty')),
               ('kpi.report_days', kpi.get('report_days')),
               ('kpi.avg_daily_revenue', kpi.get('avg_daily_revenue')),
               ('kpi.avg_per_unit', kpi.get('avg_per_unit')),
               ('kpi.gross_margin', kpi.get('gross_margin')),
               ('kpi.margin_cost_items', kpi.get('margin_cost_items')),
               ('kpi.margin_total_items', kpi.get('margin_total_items')),
               ('len(waste_alerts)', len(stats.get('waste_alerts') or []))]
    daily = stats.get('daily') or {}
    allowed += [(f'daily.revenue[{i}]', v) for i, v in enumerate(daily.get('revenue') or [])]
    allowed += [(f'daily.qty[{i}]', v) for i, v in enumerate(daily.get('qty') or [])]
    allowed += [(f'pie.values[{i}]', v) for i, v in enumerate((stats.get('pie') or {}).get('values') or [])]
    allowed += [(f'top.values[{i}]', v) for i, v in enumerate((stats.get('top') or {}).get('values') or [])]
    cmp_ = stats.get('compare') or {}
    allowed += [('compare.qty_pct', abs(cmp_.get('qty_pct') or 0) if cmp_.get('ok') else None),
                ('compare.revenue_pct', abs(cmp_.get('revenue_pct') or 0) if cmp_.get('ok') else None)]
    fc = stats.get('forecast') or {}
    allowed += [('forecast.qty', fc.get('qty') if fc.get('ok') else None),
                ('forecast.revenue', fc.get('revenue') if fc.get('ok') else None)]
    acc = stats.get('forecast_accuracy') or {}
    allowed += [('forecast_accuracy.count', acc.get('count') if acc.get('ok') else None),
                ('forecast_accuracy.accuracy', acc.get('accuracy') if acc.get('ok') else None),
                ('forecast_accuracy.hit_rate', acc.get('hit_rate') if acc.get('ok') else None)]
    wd = stats.get('weekday') or {}
    allowed += [('weekday.weekday_avg', wd.get('weekday_avg') if wd.get('ok') else None),
                ('weekday.weekend_avg', wd.get('weekend_avg') if wd.get('ok') else None)]
    ano = stats.get('anomalies') or {}
    pts = (ano.get('points') or []) if ano.get('ok') else []
    allowed += [('len(anomalies.points)', len(pts))]
    for p in pts:
        allowed += [(f'anomalies.points[{p.get("metric")}]', p.get('value')),
                    ('anomalies.deviation_pct', p.get('deviation_pct'))]
    rep = stats.get('replenishment') or {}
    allowed += [(f'replenishment[{it.get("product")}]', it.get('suggest'))
                for it in ((rep.get('items') or [])[:2] if rep.get('ok') else [])]
    bask = stats.get('basket') or {}
    for pair in ((bask.get('pairs') or [])[:1] if bask.get('ok') else []):
        allowed += [('basket.lift', pair.get('lift')), ('basket.days', pair.get('days'))]
    allowed += _date_parts(stats)
    # 派生复算：趋势句里的「近 7 日日均」与「增长/回落 %」由 daily.qty 独立算出，
    # 不读 stats 里的任何文案字段——这一路是真正能抓producer 算错的腿。
    qty = daily.get('qty') or []
    if len(qty) >= 7:
        recent = qty[-7:]
        prev = qty[-14:-7] if len(qty) >= 14 else qty
        avg_recent = sum(recent) / len(recent)
        avg_prev = sum(prev) / len(prev)
        allowed.append(('派生:近7日日均销量', avg_recent))
        if avg_prev > 0:
            ratio = avg_recent / avg_prev
            if ratio >= 1.05:
                allowed.append(('派生:环比增长%', (ratio - 1) * 100))
            elif ratio <= 0.95:
                allowed.append(('派生:环比回落%', (1 - ratio) * 100))
    for value, reason in STRUCTURAL_LITERALS.items():
        allowed.append((f'结构常量:{reason}', value))
    return [(label, float(v)) for label, v in allowed if isinstance(v, (int, float)) and not isinstance(v, bool)]


def _tolerance_for(raw):
    """文案的格式化精度决定容差：``:,.0f`` 半位、``:.1f`` 两位分之一、整数写法不吃小数。"""
    if '.' not in raw:
        return 0.5
    decimals = len(raw.split('.', 1)[1])
    return 0.5 / (10 ** decimals)


def ground_numeric_claims(stats, text):
    """逐条判定文案里的数字能否由同一份数据复算（verdict 数组 → 比率）。"""
    claims = numeric_tokens(text)
    allowed = allowed_numeric_values(stats)
    ungrounded = []
    for claim in claims:
        tol = _tolerance_for(claim['原文'])
        hit = next((label for label, v in allowed if abs(claim['值'] - v) <= tol), None)
        if hit is None:
            start = max(0, claim['位置'] - 26)
            ungrounded.append({'原文': claim['原文'],
                               '上下文': (text or '')[start:claim['位置'] + len(claim['原文']) + 6]})
    ratio = ((len(claims) - len(ungrounded)) / len(claims)) if claims else None
    return {'数字主张总数': len(claims), '已核对': len(claims) - len(ungrounded),
            '未核对项': ungrounded, '语义忠实率': ratio}


def consistency_checks(stats):
    """关系式核对：KPI 必须等于基础数组的复算值（这一层不看文案，抓的是「数据自相矛盾」）。"""
    kpi = stats.get('kpi') or {}
    daily = stats.get('daily') or {}
    qty = daily.get('qty') or []
    rev = daily.get('revenue') or []
    pie_vals = (stats.get('pie') or {}).get('values') or []
    top_vals = (stats.get('top') or {}).get('values') or []
    checks = []

    def add(name, expected, actual, tol):
        ok = expected is not None and actual is not None and abs(float(expected) - float(actual)) <= tol
        checks.append({'核对项': name, '复算值': (round(float(expected), 2) if expected is not None else None),
                       '实测值': (round(float(actual), 2) if actual is not None else None),
                       '容差': tol, '结论': '一致' if ok else '不一致'})

    add('总销量 = Σ 日销量', sum(qty) if qty else None, kpi.get('total_qty'), 0)
    add('总营收 = Σ 日营收', sum(rev) if rev else None, kpi.get('total_revenue'), 0.01 * max(len(rev), 1))
    add('日均营收 = 总营收 / 天数',
        (kpi.get('total_revenue') / len(daily.get('dates') or [])) if daily.get('dates') else None,
        kpi.get('avg_daily_revenue'), 0.01)
    add('客单价 = 总营收 / 总销量',
        (kpi.get('total_revenue') / kpi['total_qty']) if kpi.get('total_qty') else None,
        kpi.get('avg_per_unit'), 0.01)
    add('饼图合计 = 总销量', sum(pie_vals) if pie_vals else None, kpi.get('total_qty'), 0)
    add('热销合计 = 总营收', sum(top_vals) if top_vals else None, kpi.get('total_revenue'),
        0.01 * max(len(top_vals), 1))
    if len(qty) >= 14:
        recent, prev = qty[-7:], qty[-14:-7]
        add('环比% = 近7日均/前7日均-1',
            (sum(recent) / len(recent)) / (sum(prev) / len(prev)) * 100 - 100,
            (stats.get('compare') or {}).get('qty_pct'), 0.1)
    return checks


def build_faithfulness_fixture():
    """合成 stats 夹具（只给基础数组，KPI 由夹具内部按公式算出，保证「不是我手抄的数」）。

    注意：本夹具用于**离线**复现链；真面证据在 L4 S59（那里喂的是
    ``analytics.compute_dashboard_stats()`` 的生产输出，数字不由我编写）。
    """
    dates = [f'09-{d:02d}' for d in range(16, 30)]
    rev = [1000.0] * 7 + [1200.0] * 7
    qty = [100] * 7 + [120] * 7
    total_rev = round(sum(rev), 2)
    total_qty = sum(qty)
    pie_names = ['杨枝甘露', '双皮奶', '芒果西米露', '珍珠奶茶']
    pie_values = [600, 460, 280, 200]
    top_values = [6000.0, 4600.0, 2800.0, 2000.0]
    return {
        'has_data': True,
        'kpi': {'total_revenue': total_rev, 'total_qty': total_qty,
                'report_days': len(dates), 'staff_count': 3,
                'avg_daily_revenue': round(total_rev / len(dates), 2),
                'avg_per_unit': round(total_rev / total_qty, 2),
                'gross_margin': 38.5, 'margin_cost_items': 9, 'margin_total_items': 14},
        'daily': {'dates': dates, 'revenue': rev, 'qty': qty},
        'pie': {'names': pie_names, 'values': pie_values},
        'top': {'names': pie_names, 'values': top_values},
        'waste_alerts': [{'product': '芒果西米露'}, {'product': '双皮奶'}, {'product': '芒果西米露'}],
        'compare': {'ok': True, 'qty_pct': 20.0, 'revenue_pct': 20.0},
        'forecast': {'ok': True, 'qty': 130, 'revenue': 1430.0},
        'forecast_accuracy': {'ok': True, 'count': 8, 'accuracy': 75.0, 'hit_rate': 62.5},
        'weekday': {'ok': True, 'weekday_avg': 100.0, 'weekend_avg': 120.0},
        'anomalies': {'ok': True, 'points': [{'date': '2026-09-28', 'metric': 'revenue',
                                              'value': 3800.0, 'deviation_pct': 45.0}]},
        'replenishment': {'ok': True, 'items': [{'product': '杨枝甘露', 'suggest': 25},
                                                 {'product': '双皮奶', 'suggest': 18}]},
        'basket': {'ok': True, 'pairs': [{'a': '杨枝甘露', 'b': '双皮奶', 'lift': 1.8, 'days': 6}]},
    }


def faithfulness_probe(stats=None):
    """跑一遍「模板产结论 → 逐数字回算 → 关系式核对」，返回可入报告的结论块。"""
    try:
        from src.rule_copy import _rule_business_report
    except Exception as exc:  # noqa: BLE001
        return {'可用': False, '原因': f'规则版模板导入失败：{exc.__class__.__name__}'}
    data = stats if stats is not None else build_faithfulness_fixture()
    text = _rule_business_report(data, '杯', '份')
    ground = ground_numeric_claims(data, text)
    checks = consistency_checks(data)
    bad = [c for c in checks if c['结论'] != '一致']
    ratio = ground['语义忠实率']
    claims = ground['数字主张总数']
    return {'可用': True, '数字主张总数': claims,
            '已核对': ground['已核对'], '未核对项': ground['未核对项'],
            '语义忠实率': ratio, '关系式核对项': len(checks),
            '关系式不一致项': bad, 'checks': checks,
            '通过': (ratio is not None and ratio >= FAITHFUL_RATIO_FLOOR
                     and not bad and claims >= FAITH_MIN_CLAIMS)}


def _failed_model_responses(metrics):
    """正常场景中「模型给出了响应但被判失败」的条数。"""
    failed = 0
    for state in metrics['states'].values():
        if state['状态'] in ('error', 'timeout'):
            failed += 1
    return failed


def render_markdown(report):
    """渲染 Markdown 报告。"""
    summary = report['汇总']
    lines = [
        f"# AI 证据化评测报告（{report['报告']['日期']}）",
        '',
        f"> 工具：`ai_eval.py` ｜ 代码提交：`{report['报告']['代码提交'][:12]}` "
        f"｜ 模型：`{report['报告']['模型']}`（模式：{report['报告']['模型模式']}）",
        '> 数据：全部为脚本内合成数据，不含任何真实客户或个人数据。',
        '',
        '## 一、验收线',
        '',
        '| 验收线 | 要求 | 实测 | 结论 |',
        '|---|---|---|---|',
    ]
    for row in report['验收']:
        lines.append(f"| {row['验收线']} | {row['要求']} | {row['实测']} | {row['结论']} |")
    lines += [
        '',
        '## 二、汇总',
        '',
        '| 指标 | 值 |',
        '|---|---|',
    ]
    for key, value in summary.items():
        if isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f'| {key} | {value} |')
    lines += [
        '',
        '## 三、场景明细',
        '',
        '| 场景 | 结构解析 | 证据合法率 | 越界丢弃数 | 角色隔离 | 延迟(ms) | Token | 降级状态 | 结论 |',
        '|---|---|---|---|---|---|---|---|---|',
    ]
    for item in report['场景']:
        structure = item['结构解析']
        isolation = item['角色隔离']
        states = '; '.join(f"{k}={v['来源']}/{v['状态']}" for k, v in item['降级状态'].items())
        lines.append(
            f"| {item['场景']} | 调用 {structure['模型调用次数']} / 合法 "
            f"{structure['契约合法返回']} | {item['证据合法率']} | {item['越界丢弃数']} "
            f"| {isolation} | {item['延迟毫秒']} | {item['Token']} | {states} "
            f"| {item['结论']} |")
    lines += ['', '## 四、降级路径覆盖', '']
    for path in REQUIRED_PATHS:
        mark = '✅' if path in report['覆盖的降级路径'] else '❌'
        lines.append(f'- {mark} {path}')
    lines += [
        '',
        '## 五、成本口径',
        '',
        f"- 离线注入场景 Token 合计：**{summary['Token 总数（离线注入）']}**（注入值，非真实计费）。",
        '- 真实成本 = Σ(输入 Token × 输入单价 + 输出 Token × 输出单价)。',
        '- **单价未在本报告内假设**：须取自模型服务商公开价目表后填算，禁止凭空估值。',
        '',
        '## 六、局限',
        '',
        '- 离线场景使用测试注入口（`decision_cockpit.set_llm`）注入可控返回，'
        '验证的是**编排、契约校验、证据准入与降级**逻辑，不是模型本身质量。',
        '- `在线复测` 需真实 Key；未复测时该项验收结论为「待复测」，不得视为通过。',
        '- **不把线程名当隔离证据**：实测发现线程池会复用工作线程，两个快速返回的角色'
        '可能落在同一个 `decision-role_0` 线程上；因此隔离由「两个角色各自调用 + 输出契约互异 '
        '+ 独立客户端实例」共同证明。',
        '- 本报告不含真实经营效果数据，不作为经营改善证据。',
    ]
    return '\n'.join(lines) + '\n'


def main():
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description='AI 证据化评测（离线可复现）')
    parser.add_argument('--out-dir', default='engineering-assurance',
                        help='报告输出目录（相对仓库根）')
    parser.add_argument('--online', type=int, default=0,
                        help='在线双角色延迟复测轮数（0 = 不复测）')
    parser.add_argument('--env-file', default='',
                        help='在线复测使用的 .env 路径')
    args = parser.parse_args()

    env_file = _load_env(args.env_file) if args.online else None
    from src import decision_cockpit as dcp

    datasets = {
        'base': build_snapshot([_row_tuple(r) for r in BASE_ROWS], list(BASE_ALERTS), 30),
        'nodata': build_snapshot(
            [_row_tuple((sid, name, 0.0, 0, None, None))
             for sid, name, *_ in BASE_ROWS], [], 30),
        'empty': build_snapshot([], [], 30),
    }
    scenarios = build_scenarios(datasets)
    records = run_scenarios(dcp, datasets, scenarios)

    isolation = client_isolation_probe()
    online = {'可用': False, '原因': '未启用 --online（本机未做真实网络复测）'}
    if args.online:
        online = online_probe(dcp, datasets['base'][0], args.online)
        # 2026-09-16 安全修正：报告不得携带本机绝对路径（曾把 .env 全路径写进
        # ai-eval 报告并被源码 ZIP 带走）。只输出「已加载（文件名）/未提供」。
        online['env文件'] = (f'已加载（{os.path.basename(env_file)}）'
                             if env_file else '未提供')

    summary, acceptance, covered = summarize(records, online, isolation, faith)
    report = {
        '报告': {
            '工具': 'ai_eval.py',
            '生成时间': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
            '日期': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            '代码提交': _git_commit(),
            '模型': (online.get('模型') if online.get('可用')
                     else os.environ.get('DEEPSEEK_MODEL') or 'deepseek-chat（配置值）'),
            '模型模式': ('在线真实调用' if online.get('可用') else '离线注入（未调用真实模型）'),
            '数据集': '脚本内合成数据（7 门店 / 27 条证据，无真实客户数据）',
        },
        '汇总': summary,
        '验收': acceptance,
        '覆盖的降级路径': covered,
        '场景': [{
            '场景': f"{rec['spec']['id']} {rec['spec']['name']}",
            '结构解析': {
                '模型调用次数': rec['metrics']['llm_calls'],
                '契约合法返回': (rec['metrics']['llm_calls']
                                 - _failed_model_responses(rec['metrics'])),
                '角色': rec['metrics']['roles_called'],
            },
            '证据合法率': ('—' if rec['metrics']['legal_rate'] is None
                      else f"{rec['metrics']['legal_rate']:.1%}"),
            '越界丢弃数': rec['metrics']['discarded'],
            '角色隔离': _isolation_text(rec['metrics']),
            '延迟毫秒': rec['metrics']['elapsed_ms'],
            'Token': rec['metrics']['tokens'],
            '降级状态': rec['metrics']['states'],
            '结论': '通过' if not rec['failures'] else '不通过：' + '；'.join(rec['failures']),
        } for rec in records],
    }

    out_dir = REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report['报告']['日期']
    json_path = out_dir / f'ai-eval-{stamp}.json'
    md_path = out_dir / f'ai-eval-{stamp}.md'
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                         encoding='utf-8')
    md_path.write_text(render_markdown(report), encoding='utf-8')

    print('═' * 62)
    print('  AI 证据化评测')
    print('═' * 62)
    for row in acceptance:
        mark = '✅' if row['结论'] == '通过' else ('⏳' if row['结论'] == '待复测' else '🔴')
        print(f"{mark} {row['验收线']}：{row['实测']}（要求 {row['要求']}）")
    print('─' * 62)
    for rec in records:
        status = '✅' if not rec['failures'] else '🔴'
        print(f"{status} {rec['spec']['id']} {rec['spec']['name']}"
              + ('' if not rec['failures'] else '  → ' + '；'.join(rec['failures'])))
    print('─' * 62)
    print(f"场景 {summary['通过']}/{summary['场景总数']} 通过 ｜ "
          f"越界保留 {summary['越界证据保留数']} ｜ 非法证据引用 {summary['证据引用非法数']} ｜ "
          f"降级路径 {summary['降级路径覆盖']}")
    print(f'JSON 报告：{json_path}')
    print(f'Markdown ：{md_path}')

    ok = (summary['通过'] == summary['场景总数']
          and all(row['结论'] != '不通过' for row in acceptance))
    print('═' * 62)
    print('🎉 评测通过' if ok else '🔴 评测未通过')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
