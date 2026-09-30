"""分步建包向导 + AI 结构化草稿（PH2-4B）。

契约：
  1. **草稿不落库**：AI / 模板产出的草稿只进进程内草稿缓存（`_DRAFTS`），
     必须经「校验 → 预览 → 人工确认」后才由调用方写 PackItem / MetricDef。
  2. **AI 只能给草稿**：`generate_draft()` 返回 `source='ai'|'template'`，
     非法结构 / 调用异常 / 超限一律回落模板，并在 `warnings` 里说明原因。
  3. **单一校验口径**：结构校验复用 `pack_contract` 的指标与重复项判定，
     避免向导层另立一套规则。
"""
from __future__ import annotations

import json
import re
import secrets
import time

from src import pack_contract, pack_skeleton

DRAFT_TTL_SECONDS = 30 * 60
MAX_DRAFTS = 32
MAX_ITEMS = 12

WIZARD_STEPS = (
    {'key': 'industry_type', 'label': '行业类型', 'required': True, 'max_len': 50,
     'hint': '如：烘焙轻食店 / 宠物服务店'},
    {'key': 'main_items', 'label': '主营项目', 'required': True, 'max_len': 200,
     'hint': '用顿号或逗号分隔，如：可颂、吐司、手冲咖啡'},
    {'key': 'target_audience', 'label': '目标客群', 'required': False, 'max_len': 100,
     'hint': '如：周边上班族、亲子家庭'},
    {'key': 'focus', 'label': '经营重点', 'required': False, 'max_len': 100,
     'hint': '如：库存与损耗 / 服务体验 / 复购'},
    {'key': 'unit', 'label': '使用单位', 'required': False, 'max_len': 10,
     'hint': '如：个 / 杯 / 单 / 件'},
    {'key': 'free_text', 'label': '补充描述', 'required': False, 'max_len': 500,
     'hint': '经营场景、特殊要求等（选填）'},
)

_DRAFTS = {}


def _split_items(text):
    parts = [p.strip() for p in re.split(r'[、,，;；/\n]+', text or '') if p.strip()]
    out = []
    for p in parts:
        if p not in out:
            out.append(p)
    return out[:MAX_ITEMS]


def validate_brief(form):
    """校验向导输入，返回 (ok, errors, brief)。"""
    brief, errors = {}, []
    for step in WIZARD_STEPS:
        raw = (form.get(step['key']) or '').strip() if hasattr(form, 'get') else ''
        if step['required'] and not raw:
            errors.append(f'{step["label"]}必填')
        if len(raw) > step['max_len']:
            errors.append(f'{step["label"]}超过 {step["max_len"]} 字')
        brief[step['key']] = raw
    return (not errors), errors, brief


def _tailored_knowledge(brief):
    base = pack_skeleton.neutral_sections()['knowledge']
    industry = brief.get('industry_type') or '门店'
    focus = brief.get('focus') or ''
    out = {}
    for name, text in base.items():
        head = f'# {industry}·{name}\n\n'
        note = ''
        if focus and name in ('库存与损耗', '门店运营'):
            note = f'\n## 经营重点提示\n\n- 本店经营重点：{focus}，相关动作优先落地并留痕。\n'
        out[name] = head + text.split('\n', 1)[1] + note
    return out


def build_template_draft(brief):
    """规则模板草稿（无 AI 也可用；AI 失败时的回落目标）。"""
    sections = pack_skeleton.neutral_sections()
    industry = brief.get('industry_type') or '通用门店'
    items = _split_items(brief.get('main_items')) or ['主营项目']
    unit = (brief.get('unit') or '单').strip() or '单'
    audience = brief.get('target_audience') or ''
    focus = brief.get('focus') or ''

    terms = dict(sections['terms'])
    terms.update({
        'quantity_unit': unit,
        'assistant_persona': f'你是{industry}经营助手',
        'analyst_persona': f'你是{industry}经营分析师',
        'trainer_persona': f'你是{industry}培训教练',
        'scenario_customer': f'{industry}顾客',
        'report_footer': f'{industry}经营参考',
        'sample_dimension': items[0],
        'sample_shelf_question': f'{items[0]}的标准流程是什么？',
    })
    metrics = [dict(m) for m in sections['metrics']]
    for m in metrics:
        if m['role'] == 'quantity':
            m['unit'] = unit

    products = [
        {'product_name': name, 'unit_price': 10.0 + idx * 5, 'cost_price': None,
         'category': industry[:20]}
        for idx, name in enumerate(items[:8])
    ]
    questions = []
    for name in items[:6]:
        questions.append({
            'q': f'{name}的标准交付要求是什么？',
            'a': '按门店标准执行：规格、时长与验收标准对齐；不达标返工，具体标准由老板确认后固化。',
            'weak': sections['domains'][0]['name'],
        })
    questions.append({
        'q': f'{industry}的库存盘点与损耗控制怎么做？',
        'a': '按日/周盘点，记录剩余与报废；损耗率 = 损耗量 ÷（剩余量 + 损耗量），'
             '连续超标回溯备货与操作环节。',
        'weak': sections['domains'][2]['name'],
    })

    free = brief.get('free_text') or ''
    description = f'{industry}行业包（向导生成）'
    if audience:
        description += f'；目标客群：{audience}'
    if focus:
        description += f'；经营重点：{focus}'
    if free:
        description += f'；补充：{free[:80]}'

    return {
        'schema_version': pack_contract.SCHEMA_VERSION,
        'id': '',
        'name': industry,
        'version': '1.0.0',
        'description': description[:200],
        'terms': terms,
        'domains': [dict(d) for d in sections['domains']],
        'weak_keywords': list(sections['weak_keywords']),
        'synonyms': [list(s) for s in sections['synonyms']],
        'metrics': metrics,
        'formula_params': dict(sections['formula_params']),
        'questions': questions,
        'scenarios': json.loads(json.dumps(sections['scenarios'], ensure_ascii=False)),
        'products': products,
        'knowledge': _tailored_knowledge(brief),
    }


def validate_draft(draft, *, require_full=False):  # noqa: PLR0912
    """草稿结构校验 + 完整性评分（不读文件、不写库）。

    Returns:
        dict: {'ok': bool, 'errors': list, 'warnings': list, 'score': int}
    """
    errors, warnings = [], []
    if not isinstance(draft, dict):
        return {'ok': False, 'errors': ['草稿不是对象'], 'warnings': [], 'score': 0}
    if draft.get('schema_version') != pack_contract.SCHEMA_VERSION:
        errors.append(f'schema_version 必须为 {pack_contract.SCHEMA_VERSION}')
    for field in ('name', 'terms', 'domains', 'metrics'):
        if not draft.get(field):
            errors.append(f'缺少字段: {field}')
    for field in ('questions', 'products'):
        if not isinstance(draft.get(field), list) or not draft.get(field):
            errors.append(f'{field} 不能为空数组')
    scen = draft.get('scenarios')
    if not isinstance(scen, dict) or not isinstance(scen.get('pool'), dict) or not scen['pool']:
        errors.append('scenarios.pool 不能为空')

    metric_errors, metric_warnings, _ = pack_contract._check_metrics(draft.get('metrics'))
    errors.extend(metric_errors)
    warnings.extend(metric_warnings)

    qs = [x for x in (draft.get('questions') or []) if isinstance(x, dict)]
    for idx, x in enumerate(qs):
        if not (x.get('q') or '').strip() or not (x.get('a') or '').strip():
            errors.append(f'questions[{idx}] 缺题干或答案')
    for dup in pack_contract._duplicates([(x.get('q') or '').strip() for x in qs]):
        if dup:
            errors.append(f'题干重复: {dup[:30]}')

    prods = [x for x in (draft.get('products') or []) if isinstance(x, dict)]
    for idx, x in enumerate(prods):
        if not (x.get('product_name') or '').strip():
            errors.append(f'products[{idx}] 缺名称')
    for dup in pack_contract._duplicates(
            [(x.get('product_name') or '').strip() for x in prods]):
        if dup:
            errors.append(f'产品重复: {dup}')

    kb = draft.get('knowledge') or {}
    if not isinstance(kb, dict) or not kb:
        errors.append('knowledge 不能为空')
    domain_names = [d.get('name') for d in (draft.get('domains') or [])
                    if isinstance(d, dict)]
    for name in domain_names:
        if name and name not in kb:
            warnings.append(f'知识域「{name}」缺少对应知识正文')

    if require_full:
        stats = {'questions': len(qs), 'products': len(prods),
                 'scenarios': len((scen or {}).get('pool') or {}),
                 'knowledge': len(kb)}
        if stats['questions'] < pack_contract.TARGET_QUESTIONS:
            errors.append(f'题量不足：{stats["questions"]}/{pack_contract.TARGET_QUESTIONS}')
        if stats['products'] < pack_contract.TARGET_PRODUCTS:
            errors.append(f'产品不足：{stats["products"]}/{pack_contract.TARGET_PRODUCTS}')
        if stats['scenarios'] < pack_contract.TARGET_SCENARIOS:
            errors.append(f'情景不足：{stats["scenarios"]}/{pack_contract.TARGET_SCENARIOS}')
        if stats['knowledge'] < pack_contract.TARGET_KNOWLEDGE:
            errors.append(f'知识不足：{stats["knowledge"]}/{pack_contract.TARGET_KNOWLEDGE}')

    coverage = min(1.0, len(qs) / 8.0) * 0.5 + min(1.0, len(prods) / 5.0) * 0.5
    score = max(0, min(100, round(60 * (1.0 if not errors else 0.4) + 40 * coverage)))
    return {'ok': not errors, 'errors': errors, 'warnings': warnings, 'score': score}


def generate_draft(brief, llm=None, timeout_note=None):
    """生成草稿：AI 优先，非法/异常/未接入则回落模板。

    Args:
        brief: 向导输入（validate_brief 的 brief 输出）。
        llm: 可选可调用对象 `fn(prompt) -> dict | str | None`（测试桩 / 真实模型）。
        timeout_note: 上游超限/超时的原因说明（有值时直接回落模板）。

    Returns:
        dict: {'draft': dict, 'source': 'ai'|'template', 'warnings': list,
               'validation': dict}
    """
    warnings = []
    template = build_template_draft(brief)
    if timeout_note:
        warnings.append(timeout_note)
        return {'draft': template, 'source': 'template', 'warnings': warnings,
                'validation': validate_draft(template)}
    if llm is None:
        warnings.append('未接入 AI，使用规则模板草稿')
        return {'draft': template, 'source': 'template', 'warnings': warnings,
                'validation': validate_draft(template)}

    prompt = _build_prompt(brief)
    try:
        raw = llm(prompt)
    except Exception as exc:
        warnings.append(f'AI 调用失败（{exc.__class__.__name__}），已回落模板草稿')
        return {'draft': template, 'source': 'template', 'warnings': warnings,
                'validation': validate_draft(template)}

    candidate = raw
    if isinstance(raw, str):
        try:
            candidate = json.loads(raw)
        except (ValueError, TypeError):
            candidate = None
    if not isinstance(candidate, dict):
        warnings.append('AI 返回结构非法，已回落模板草稿')
        return {'draft': template, 'source': 'template', 'warnings': warnings,
                'validation': validate_draft(template)}

    merged = dict(template)
    merged.update({k: v for k, v in candidate.items() if v not in (None, '', [], {})})
    merged['schema_version'] = pack_contract.SCHEMA_VERSION
    merged['name'] = (candidate.get('name') or template['name'])[:50]
    result = validate_draft(merged)
    if not result['ok']:
        warnings.append('AI 草稿未通过校验（' + '；'.join(result['errors'][:2]) + '），已回落模板草稿')
        return {'draft': template, 'source': 'template', 'warnings': warnings,
                'validation': validate_draft(template)}
    return {'draft': merged, 'source': 'ai', 'warnings': warnings, 'validation': result}


def _build_prompt(brief):
    lines = ['请按门店经营场景生成行业包草稿，输出 JSON（schema_version/name/terms/domains/'
             'knowledge/questions/scenarios/products/synonyms/metrics/formula_params）。']
    for step in WIZARD_STEPS:
        val = brief.get(step['key']) or ''
        if val:
            lines.append(f'- {step["label"]}：{val}')
    lines.append('要求：术语贴合该行业；指标必须包含 dimension/quantity/amount 三个角色；'
                 '不要输出营销文案。')
    return '\n'.join(lines)


def _purge_expired(now=None):
    now = now or time.time()
    for token in [t for t, v in _DRAFTS.items()
                  if now - v['created_at'] > DRAFT_TTL_SECONDS]:
        _DRAFTS.pop(token, None)


def put_draft(draft, brief=None, source='template'):
    """把草稿放进进程内草稿缓存（不落库），返回一次性 token。"""
    _purge_expired()
    while len(_DRAFTS) >= MAX_DRAFTS:
        _DRAFTS.pop(next(iter(_DRAFTS)), None)
    token = secrets.token_urlsafe(16)
    _DRAFTS[token] = {'draft': draft, 'brief': brief or {},
                      'source': source, 'created_at': time.time()}
    return token


def peek_draft(token):
    """读取草稿（不移除），不存在/过期返回 None。"""
    _purge_expired()
    return _DRAFTS.get(token)


def take_draft(token):
    """取出并移除草稿（人工确认后调用），不存在/过期返回 None。"""
    _purge_expired()
    return _DRAFTS.pop(token, None)


def clear_drafts():
    """清空草稿缓存（测试/维护用）。"""
    _DRAFTS.clear()


__all__ = [
    'DRAFT_TTL_SECONDS',
    'MAX_ITEMS',
    'WIZARD_STEPS',
    'build_template_draft',
    'clear_drafts',
    'generate_draft',
    'peek_draft',
    'put_draft',
    'take_draft',
    'validate_brief',
    'validate_draft',
]
