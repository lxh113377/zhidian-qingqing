"""行业包契约校验（schema_version: 1）。

PH2-1 满配契约：36 题 / 12 情景 / 15 产品 / 4 篇知识（每篇约 2-3KB）。
本模块只读包目录，产出：
  - 结构错误（坏 JSON / 缺文件 / schema_version 不符）
  - 重复项（题干、产品名、同义词、知识域名）
  - 非法指标（角色/类型/聚合越界、必需角色缺失、键重复）
  - 完整性评分（0-100）与各分项统计

CLI：``python -m src.pack_contract [--json] [--warn-only]``
退出码：0 = 无结构错误；1 = 存在结构错误或未达满配；``--warn-only`` 时恒为 0。
"""

from __future__ import annotations

import json
import os
import sys

from .industry_pack import PACK_SCHEMA_VERSION as SCHEMA_VERSION

TARGET_QUESTIONS = 36
TARGET_SCENARIOS = 12
TARGET_PRODUCTS = 15
TARGET_KNOWLEDGE = 4
KNOWLEDGE_MIN_BYTES = 1800
KNOWLEDGE_MAX_BYTES = 3400

# 角色/取值类型的唯一真相源 = src/metrics.py（禁止在本模块另立枚举，防双源漂移）
try:
    from .metrics import (
        NUMERIC_TYPES,
        ROLE_AMOUNT,
        ROLE_COST,
        ROLE_DIMENSION,
        ROLE_NOTE,
        ROLE_OTHER,
        ROLE_QUANTITY,
        ROLE_STOCK,
        ROLE_WASTE,
    )
except ImportError:  # pragma: no cover - 独立运行兜底
    ROLE_DIMENSION, ROLE_QUANTITY, ROLE_AMOUNT = 'dimension', 'quantity', 'amount'
    ROLE_STOCK, ROLE_WASTE, ROLE_COST = 'stock', 'waste', 'cost'
    ROLE_NOTE, ROLE_OTHER = 'note', 'other'
    NUMERIC_TYPES = ('number', 'integer')

REQUIRED_ROLES = (ROLE_DIMENSION, ROLE_QUANTITY, ROLE_AMOUNT)
VALID_ROLES = frozenset({
    ROLE_DIMENSION, ROLE_QUANTITY, ROLE_AMOUNT,
    ROLE_STOCK, ROLE_WASTE, ROLE_COST, ROLE_NOTE, ROLE_OTHER,
})
VALID_VALUE_TYPES = frozenset({'text', *NUMERIC_TYPES})
VALID_AGG = frozenset({'sum', 'count', 'avg', 'none'})
REQUIRED_TERM_KEYS = (
    'store', 'record_dimension', 'quantity_unit', 'material_unit',
    'assistant_persona', 'analyst_persona', 'trainer_persona',
)

_PACK_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'industry_packs')
_KB = 1024.0


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, f'文件缺失: {os.path.basename(path)}'
    except json.JSONDecodeError as exc:
        return None, f'JSON 损坏: {os.path.basename(path)} ({exc.msg} @line {exc.lineno})'
    except OSError as exc:
        return None, f'读取失败: {os.path.basename(path)} ({exc.__class__.__name__})'


def _duplicates(values):
    seen, dup = set(), []
    for item in values:
        if item in seen and item not in dup:
            dup.append(item)
        seen.add(item)
    return dup


def list_pack_ids():
    """枚举含 pack.json 的包目录（排序稳定）。"""
    if not os.path.isdir(_PACK_ROOT):
        return []
    return sorted(
        name for name in os.listdir(_PACK_ROOT)
        if os.path.isfile(os.path.join(_PACK_ROOT, name, 'pack.json'))
    )


def _ratio(actual, target):
    if target <= 0:
        return 1.0
    return max(0.0, min(1.0, actual / float(target)))


def _check_metrics(metrics):  # noqa: PLR0912
    errors, warnings = [], []
    keys, roles = [], []
    if not isinstance(metrics, list) or not metrics:
        return ['metrics 段缺失或非数组'], [], {}
    for idx, item in enumerate(metrics):
        if not isinstance(item, dict):
            errors.append(f'metrics[{idx}] 非对象')
            continue
        key = (item.get('key') or '').strip()
        if not key:
            errors.append(f'metrics[{idx}] 缺 key')
        else:
            keys.append(key)
        if item.get('role') not in VALID_ROLES:
            errors.append(f'metrics[{idx}] 非法 role: {item.get("role")!r}')
        else:
            roles.append(item['role'])
        if item.get('value_type') not in VALID_VALUE_TYPES:
            errors.append(f'metrics[{idx}] 非法 value_type: {item.get("value_type")!r}')
        if item.get('agg') not in VALID_AGG:
            errors.append(f'metrics[{idx}] 非法 agg: {item.get("agg")!r}')
        if not item.get('label'):
            warnings.append(f'metrics[{idx}] 缺 label')
    for key in _duplicates(keys):
        errors.append(f'metrics 重复 key: {key}')
    for role in REQUIRED_ROLES:
        if role not in roles:
            errors.append(f'metrics 缺必需角色: {role}')
    return errors, warnings, {'metric_count': len(metrics), 'roles': sorted(set(roles))}


def validate_pack(pack_id):  # noqa: PLR0912, PLR0915
    """校验单个包，返回结构化报告（不抛异常）。"""
    base = os.path.join(_PACK_ROOT, pack_id)
    report = {
        'pack_id': pack_id, 'ok': False, 'score': 0, 'errors': [], 'warnings': [],
        'stats': {}, 'defects': {},
    }
    if not os.path.isdir(base):
        report['errors'].append(f'包目录不存在: {pack_id}')
        return report

    pack, err = _read_json(os.path.join(base, 'pack.json'))
    if err:
        report['errors'].append(err)
        return report

    errors, warnings = report['errors'], report['warnings']
    if pack.get('schema_version') != SCHEMA_VERSION:
        errors.append(
            f'schema_version 期望 {SCHEMA_VERSION}，实际 {pack.get("schema_version")!r}'
        )
    for field in ('id', 'name', 'version', 'terms', 'domains', 'metrics', 'files'):
        if not pack.get(field):
            errors.append(f'pack.json 缺字段: {field}')
    if pack.get('id') != pack_id:
        errors.append(f'pack.id({pack.get("id")!r}) 与目录名({pack_id!r}) 不一致')

    terms = pack.get('terms') if isinstance(pack.get('terms'), dict) else {}
    missing_terms = [k for k in REQUIRED_TERM_KEYS if not terms.get(k)]
    if missing_terms:
        errors.append(f'terms 缺键: {",".join(missing_terms)}')
    if not pack.get('synonyms'):
        errors.append('synonyms 空')
    if not pack.get('weak_keywords'):
        errors.append('weak_keywords 空')
    if not isinstance(pack.get('formula_params'), dict) or not pack['formula_params']:
        errors.append('formula_params 空')

    metric_errors, metric_warnings, metric_stats = _check_metrics(pack.get('metrics'))
    errors.extend(metric_errors)
    warnings.extend(metric_warnings)
    report['stats'].update(metric_stats)

    domains = pack.get('domains') if isinstance(pack.get('domains'), list) else []
    domain_names = [d.get('name', '') for d in domains if isinstance(d, dict)]
    for name in _duplicates(domain_names):
        errors.append(f'domains 重名: {name}')
    if len(domains) != TARGET_KNOWLEDGE:
        warnings.append(f'知识域 {len(domains)} 个，目标 {TARGET_KNOWLEDGE} 个')

    kdir = os.path.join(base, 'knowledge')
    knowledge = {}
    for item in domains:
        fname = (item.get('knowledge_file') or '').strip() if isinstance(item, dict) else ''
        if not fname:
            errors.append(f'domain {item.get("name")!r} 缺 knowledge_file')
            continue
        fpath = os.path.join(kdir, fname)
        if not os.path.isfile(fpath):
            errors.append(f'知识文件缺失: {fname}')
            continue
        size = os.path.getsize(fpath)
        knowledge[fname] = size
        if size < KNOWLEDGE_MIN_BYTES or size > KNOWLEDGE_MAX_BYTES:
            warnings.append(
                f'知识 {fname} {size / _KB:.1f}KB 超出 2-3KB 目标区间'
            )

    questions, q_err = _read_json(os.path.join(base, 'questions.json'))
    if q_err:
        errors.append(q_err)
        questions = []
    if not isinstance(questions, list):
        errors.append('questions.json 顶层非数组')
        questions = []
    for idx, item in enumerate(questions):
        if not isinstance(item, dict):
            errors.append(f'questions[{idx}] 非对象')
            continue
        if not (item.get('q') or '').strip():
            errors.append(f'questions[{idx}] 缺题干')
        if not (item.get('a') or '').strip():
            errors.append(f'questions[{idx}] 缺答案')
        weak = (item.get('weak') or '').strip()
        if not weak:
            warnings.append(f'questions[{idx}] 缺弱项标签')
        elif domain_names and weak not in domain_names:
            warnings.append(f'questions[{idx}] 弱项标签 {weak!r} 不在知识域内')
    for q in _duplicates([(x.get('q') or '').strip() for x in questions if isinstance(x, dict)]):
        if q:
            errors.append(f'题干重复: {q[:30]}')

    scenarios_raw, s_err = _read_json(os.path.join(base, 'scenarios.json'))
    if s_err:
        errors.append(s_err)
        scenarios_raw = {}
    pool = scenarios_raw.get('pool') if isinstance(scenarios_raw, dict) else None
    pool = pool if isinstance(pool, dict) else {}
    scen_types = len([k for k, v in pool.items() if isinstance(v, list) and v])

    products, p_err = _read_json(os.path.join(base, 'products.json'))
    if p_err:
        errors.append(p_err)
        products = []
    if not isinstance(products, list):
        errors.append('products.json 顶层非数组')
        products = []
    for idx, item in enumerate(products):
        if not isinstance(item, dict):
            errors.append(f'products[{idx}] 非对象')
            continue
        if not (item.get('product_name') or '').strip():
            errors.append(f'products[{idx}] 缺 product_name')
        price = item.get('unit_price')
        if not isinstance(price, (int, float)) or price <= 0:
            errors.append(f'products[{idx}] 非法 unit_price: {price!r}')
    for name in _duplicates([(x.get('product_name') or '').strip() for x in products if isinstance(x, dict)]):
        if name:
            errors.append(f'产品重复: {name}')

    dup_syn = _duplicates([tuple(s) if isinstance(s, list) else s for s in pack.get('synonyms', [])])
    for item in dup_syn:
        warnings.append(f'同义词重复: {item}')

    report['defects'] = {
        'bad_json': [e for e in errors if 'JSON 损坏' in e],
        'duplicates': [e for e in errors + warnings if '重复' in e or '重名' in e],
        'invalid_metrics': [e for e in errors if 'metrics' in e],
    }
    report['stats'].update({
        'questions': len(questions), 'scenarios': scen_types, 'products': len(products),
        'knowledge_docs': len(knowledge), 'knowledge_bytes': knowledge,
    })

    structure = 30.0
    if pack.get('schema_version') != SCHEMA_VERSION:
        structure -= 10
    structure -= min(15.0, 3.0 * len(report['defects']['duplicates']))
    structure -= min(15.0, 3.0 * len(report['defects']['invalid_metrics']))
    content = 40.0 * (
        0.4 * _ratio(len(questions), TARGET_QUESTIONS)
        + 0.2 * _ratio(scen_types, TARGET_SCENARIOS)
        + 0.2 * _ratio(len(products), TARGET_PRODUCTS)
        + 0.2 * _ratio(len(knowledge), TARGET_KNOWLEDGE)
    )
    quality = 30.0
    quality -= min(15.0, 2.0 * len([e for e in errors if 'questions' in e]))
    quality -= min(15.0, 2.0 * len([e for e in errors if 'products' in e]))
    report['score'] = max(0, min(100, round(structure + content + quality)))
    report['ok'] = not errors and all(
        report['stats'][key] >= target for key, target in (
            ('questions', TARGET_QUESTIONS), ('scenarios', TARGET_SCENARIOS),
            ('products', TARGET_PRODUCTS), ('knowledge_docs', TARGET_KNOWLEDGE),
        )
    )
    return report


def validate_all():
    return [validate_pack(pid) for pid in list_pack_ids()]


def _fmt(report):
    s = report['stats']
    head = '✅' if report['ok'] else ('❌' if report['errors'] else '⚠️')
    return (
        f"{head} {report['pack_id']}  评分 {report['score']}/100  "
        f"题 {s.get('questions', 0)}/{TARGET_QUESTIONS}  "
        f"情景 {s.get('scenarios', 0)}/{TARGET_SCENARIOS}  "
        f"产品 {s.get('products', 0)}/{TARGET_PRODUCTS}  "
        f"知识 {s.get('knowledge_docs', 0)}/{TARGET_KNOWLEDGE}"
    )


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = '--json' in argv
    warn_only = '--warn-only' in argv
    reports = validate_all()
    if as_json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        print(f'行业包契约校验（schema_version={SCHEMA_VERSION}）')
        for report in reports:
            print(_fmt(report))
            for item in report['errors'][:8]:
                print(f'    ✗ {item}')
            for item in report['warnings'][:5]:
                print(f'    ! {item}')
        bad = [r for r in reports if r['errors'] or not r['ok']]
        print('—' * 46)
        print(f'包数 {len(reports)} / 达标 {sum(1 for r in reports if r["ok"])} / 未达标 {len(bad)}')
    if warn_only:
        return 0
    return 0 if all(r['ok'] for r in reports) else 1


if __name__ == '__main__':
    raise SystemExit(main())
