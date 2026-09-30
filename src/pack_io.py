"""行业包 JSON 导入导出（PH2-4C）。

固定格式：`schema_version + meta + terms + metrics + knowledge + questions +
scenarios + products + formula_params + domains + weak_keywords + synonyms`。

安全边界：
  1. **未知版本拒绝**：`schema_version` 不等于当前版本直接拒收。
  2. **恶意外键拒绝**：包标识走白名单正则；知识键必须是安全文件名
     （禁路径分隔符 / `..` / 绝对路径），杜绝写出包目录。
  3. **超长导入拒绝**：整体字节数、单篇知识、题量/情景/产品/同义词量均有上限。
  4. **冲突显式化**：目标包已存在且未声明覆盖时，返回冲突清单且不写库。
"""
from __future__ import annotations

import json

from src import change_log, industry_pack, pack_contract, pack_manifest, pack_store, pack_wizard
from src.models import IndustryPack, MetricDef, PackItem, db

MAX_IMPORT_BYTES = 512 * 1024
MAX_KNOWLEDGE_DOCS = pack_contract.TARGET_KNOWLEDGE * 3
MAX_QUESTIONS = pack_store.MAX_QUESTIONS
MAX_PRODUCTS = 200
MAX_DOMAINS = industry_pack.MAX_DOMAINS
MAX_METRICS = industry_pack.MAX_METRICS
MAX_SYNONYMS = pack_store.MAX_SYNONYMS
MAX_SCENARIO_TYPES = pack_store.MAX_SCENARIOS

EXPORT_SECTIONS = ('terms', 'domains', 'weak_keywords', 'synonyms', 'formula_params',
                   'metrics', 'knowledge', 'questions', 'scenarios', 'products')


def _knowledge_of(pack, pack_id):
    inline = pack.get('knowledge')
    if isinstance(inline, dict) and inline:
        return dict(inline)
    return dict(industry_pack.file_knowledge(pack_id) or {})


def export_pack(pack_id):
    """把任意包（文件包/DB 覆盖层）导出为固定结构 dict。"""
    pack = industry_pack.get_pack(pack_id)
    if not pack or pack.get('id') != pack_id:
        return None
    return {
        'schema_version': pack_contract.SCHEMA_VERSION,
        'meta': {
            'id': pack_id,
            'name': pack.get('name') or pack_id,
            'version': pack.get('version') or '1.0.0',
            'description': pack.get('description') or '',
            'source': pack.get('source') or 'file',
        },
        'terms': dict(pack.get('terms') or {}),
        'domains': [dict(d) for d in (pack.get('domains') or [])],
        'weak_keywords': list(pack.get('weak_keywords') or []),
        'synonyms': [list(s) for s in (pack.get('synonyms') or [])],
        'formula_params': dict(pack.get('formula_params') or {}),
        'metrics': [dict(m) for m in (pack.get('metrics') or [])],
        'knowledge': _knowledge_of(pack, pack_id),
        'questions': [dict(x) for x in (pack.get('questions') or [])],
        'scenarios': json.loads(json.dumps(pack.get('scenarios') or {}, ensure_ascii=False)),
        'products': [dict(x) for x in (pack.get('products') or [])],
        # H3 溯源清单：导出即声明版本区间/能力依赖，导入端据此判兼容。
        # 刻意不带文件层校验和——导出载荷是「有效内容」（含 DB 覆盖层），
        # 与文件层字节不同生命周期，带上只会在对端误判篡改。
        'manifest': pack_manifest.build_export_manifest(pack_id, pack),
    }


def _file_sections(pack_id):
    """文件包段读取（questions/scenarios/products 可能不在归一化结果里）。"""
    out = {}
    for name in ('questions', 'scenarios', 'products'):
        rows = industry_pack.load_pack_file_json(pack_id, f'{name}.json')
        if rows is not None:
            out[name] = rows
    return out


def export_pack_full(pack_id):
    """导出并对缺段做文件回填（用于往返一致性校验）。"""
    payload = export_pack(pack_id)
    if payload is None:
        return None
    missing = [k for k in ('questions', 'scenarios', 'products') if not payload.get(k)]
    if missing:
        for name, rows in _file_sections(pack_id).items():
            if name in missing:
                payload[name] = rows
    return payload


def dumps_pack(pack_id, indent=2):
    payload = export_pack_full(pack_id)
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, indent=indent)


def parse_payload(raw):
    """把 JSON 文本/dict 解析为 payload；失败返回 (None, 原因)。"""
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return None, '导入内容必须是 JSON 文本或对象'
    if len(raw.encode('utf-8')) > MAX_IMPORT_BYTES:
        return None, f'导入内容超过上限（{MAX_IMPORT_BYTES // 1024}KB）'
    try:
        return json.loads(raw), None
    except (ValueError, TypeError) as exc:
        return None, f'JSON 解析失败：{exc.__class__.__name__}'


def export_shape_of(draft, pack_id=None):
    """把草稿 dict 包装为导入格式（向导确认落库复用同一条校验/写入链路）。"""
    return {
        'schema_version': pack_contract.SCHEMA_VERSION,
        'meta': {
            'id': (pack_id or draft.get('id') or '').strip(),
            'name': draft.get('name') or '未命名行业包',
            'version': draft.get('version') or '1.0.0',
            'description': draft.get('description') or '',
        },
        'terms': draft.get('terms') or {},
        'domains': draft.get('domains') or [],
        'weak_keywords': draft.get('weak_keywords') or [],
        'synonyms': draft.get('synonyms') or [],
        'formula_params': draft.get('formula_params') or {},
        'metrics': draft.get('metrics') or [],
        'knowledge': draft.get('knowledge') or {},
        'questions': draft.get('questions') or [],
        'scenarios': draft.get('scenarios') or {},
        'products': draft.get('products') or [],
    }


def _safe_knowledge_key(key):
    text = str(key or '').strip()
    if not text or len(text) > 100:
        return False
    if any(sep in text for sep in ('/', '\\', '..', ':')) or text.startswith('.'):
        return False
    return True


def validate_import(raw):
    """四层校验：版本 → meta/外键 → 体积/数量 → 结构。

    Returns:
        dict: {'ok', 'errors', 'warnings', 'score', 'payload', 'normalized'}
    """
    errors, warnings = [], []
    payload, err = parse_payload(raw)
    if err:
        return {'ok': False, 'errors': [err], 'warnings': [], 'score': 0,
                'payload': None, 'normalized': None}
    if not isinstance(payload, dict):
        return {'ok': False, 'errors': ['导入内容必须是 JSON 对象'], 'warnings': [],
                'score': 0, 'payload': None, 'normalized': None}

    version = payload.get('schema_version')
    if version != pack_contract.SCHEMA_VERSION:
        return {'ok': False,
                'errors': [f'不支持的 schema_version：{version!r}'
                           f'（当前支持 {pack_contract.SCHEMA_VERSION}）'],
                'warnings': [], 'score': 0, 'payload': payload, 'normalized': None}

    meta = payload.get('meta') if isinstance(payload.get('meta'), dict) else {}
    pack_id = str(meta.get('id') or payload.get('id') or '').strip()
    name = str(meta.get('name') or payload.get('name') or '').strip()
    if not industry_pack.PACK_ID_RE.match(pack_id):
        errors.append('包标识非法（只允许小写字母开头 + 字母/数字/下划线，≤50 位）')
    if not name or len(name) > 50:
        errors.append('包名称必填且 ≤50 字')

    # H3 同一天花板：导入处就把「新平台导出的包落到旧平台」挡掉，
    # 否则包内新段落会被 _normalize_pack 静默忽略（导入成功但功能缺一半）。
    gate = pack_manifest.check_pack({'id': pack_id, 'version': str(meta.get('version') or '1.0.0'),
                                     'source': 'db', 'manifest': payload.get('manifest')},
                                    pack_id=pack_id)
    errors.extend(gate['errors'])
    warnings.extend(gate['warnings'])

    knowledge = payload.get('knowledge') if isinstance(payload.get('knowledge'), dict) else {}
    if len(knowledge) > MAX_KNOWLEDGE_DOCS:
        errors.append(f'知识文档数量超上限（{len(knowledge)}/{MAX_KNOWLEDGE_DOCS}）')
    for key, text in knowledge.items():
        if not _safe_knowledge_key(key):
            errors.append(f'知识键非法（疑似路径/外键）：{key!r}')
            continue
        if not isinstance(text, str) or len(text) > pack_store.MAX_KNOWLEDGE_LEN:
            errors.append(f'知识正文超上限：{key}')

    def _len_of(key):
        val = payload.get(key)
        return len(val) if isinstance(val, (list, dict)) else 0

    for key, limit in (('questions', MAX_QUESTIONS), ('products', MAX_PRODUCTS),
                       ('domains', MAX_DOMAINS), ('metrics', MAX_METRICS),
                       ('synonyms', MAX_SYNONYMS)):
        if _len_of(key) > limit:
            errors.append(f'{key} 数量超上限（{_len_of(key)}/{limit}）')
    scen_pool = (payload.get('scenarios') or {}).get('pool') \
        if isinstance(payload.get('scenarios'), dict) else None
    if isinstance(scen_pool, dict) and len(scen_pool) > MAX_SCENARIO_TYPES:
        errors.append(f'scenarios 类型超上限（{len(scen_pool)}/{MAX_SCENARIO_TYPES}）')

    normalized = {
        'schema_version': pack_contract.SCHEMA_VERSION,
        'id': pack_id,
        'name': name,
        'version': str(meta.get('version') or '1.0.0')[:20],
        'description': str(meta.get('description') or '')[:200],
        'terms': payload.get('terms') or {},
        'domains': payload.get('domains') or [],
        'weak_keywords': payload.get('weak_keywords') or [],
        'synonyms': payload.get('synonyms') or [],
        'formula_params': payload.get('formula_params') or {},
        'metrics': payload.get('metrics') or [],
        'knowledge': knowledge,
        'questions': payload.get('questions') or [],
        'scenarios': payload.get('scenarios') or {},
        'products': payload.get('products') or [],
        'manifest': payload.get('manifest') if isinstance(payload.get('manifest'), dict) else None,
    }
    struct = pack_wizard.validate_draft(normalized)
    errors.extend(struct['errors'])
    warnings.extend(struct['warnings'])
    return {'ok': not errors, 'errors': errors, 'warnings': warnings,
            'score': struct['score'], 'payload': payload, 'normalized': normalized}


def conflict_report(pack_id):
    """目标包是否已存在（文件包或 DB 覆盖层都算占用）。"""
    rows = []
    if pack_id in (industry_pack.list_pack_ids() or []):
        rows.append({'kind': 'file_pack', 'id': pack_id})
    if IndustryPack.query.filter_by(pack_id=pack_id).first():
        rows.append({'kind': 'db_pack', 'id': pack_id})
    return rows


def _write_sections(pack_id, normalized):
    change_log.purge(PackItem, pack_id=pack_id)
    change_log.purge(MetricDef, pack_id=pack_id)
    for section in ('terms', 'domains', 'weak_keywords', 'synonyms',
                    'formula_params', 'questions', 'scenarios', 'products'):
        db.session.add(PackItem(pack_id=pack_id, section=section, key='',
                                payload=json.dumps(normalized.get(section),
                                                   ensure_ascii=False)))
    if normalized.get('manifest'):
        # 清单随包落 DB：激活闸门与后台徽章读的是同一份声明，不另立真相源
        db.session.add(PackItem(pack_id=pack_id, section='manifest', key='',
                                payload=json.dumps(normalized['manifest'], ensure_ascii=False)))
    for name, text in (normalized.get('knowledge') or {}).items():
        db.session.add(PackItem(pack_id=pack_id, section='knowledge', key=name,
                                payload=json.dumps(text, ensure_ascii=False)))
    for d in normalized.get('metrics') or []:
        db.session.add(MetricDef(
            pack_id=pack_id, key=d['key'], label=d['label'],
            value_type=d['value_type'], unit=d['unit'], role=d['role'],
            agg=d['agg'], required=d['required'], enabled=True, sort=d['sort'],
            options_json=(json.dumps({'legacy_column': d['legacy_column']})
                          if d.get('legacy_column') else None),
        ))


def import_pack(raw, overwrite=False, target_id=None):
    """校验通过后写库；冲突未声明覆盖时不写。返回 (ok, message, report)。"""
    report = validate_import(raw)
    if not report['ok']:
        return False, '导入校验未通过', report
    normalized = report['normalized']
    pack_id = target_id or normalized['id']
    if not industry_pack.PACK_ID_RE.match(pack_id or ''):
        report['errors'].append('目标包标识非法')
        return False, '目标包标识非法', report
    normalized['id'] = pack_id

    conflicts = conflict_report(pack_id)
    if conflicts and not overwrite:
        return False, f'包 {pack_id} 已存在，需显式声明覆盖', {
            **report, 'conflicts': conflicts}

    meta = IndustryPack.query.filter_by(pack_id=pack_id).first()
    if meta is None:
        meta = IndustryPack(pack_id=pack_id, name=normalized['name'])
        db.session.add(meta)
    meta.name = normalized['name']
    meta.description = normalized['description']
    _write_sections(pack_id, normalized)
    _ok, _err = pack_store._commit('导入包')
    if not _ok:
        return False, _err, report
    industry_pack.invalidate()
    return True, f'行业包「{normalized["name"]}」已导入', {**report, 'conflicts': conflicts}


def restore_builtin(pack_id):
    """恢复内置示例：删除 DB 覆盖层，让该包回落到文件包/内置版本。"""
    if not industry_pack.PACK_ID_RE.match(pack_id or ''):
        return False, '包标识非法'
    removed = change_log.purge(PackItem, pack_id=pack_id)
    removed_metrics = change_log.purge(MetricDef, pack_id=pack_id)
    change_log.purge(IndustryPack, pack_id=pack_id)
    db.session.commit()
    industry_pack.invalidate()
    if pack_id not in (industry_pack.list_pack_ids() or []):
        return True, (f'已清除 {pack_id} 的 DB 覆盖层'
                      f'（{removed} 段 / {removed_metrics} 指标）；该包无文件包版本')
    return True, f'已恢复内置示例「{pack_id}」（清除 {removed} 段 / {removed_metrics} 指标）'


def copy_pack(src_id, new_id, name=None, description=None):
    """复制包：以来源包为起点创建新包（沿用 pack_store.create_pack）。"""
    if not industry_pack.PACK_ID_RE.match(src_id or ''):
        return False, '来源包标识非法'
    if not pack_store.pack_exists(src_id):
        return False, f'来源包 {src_id} 不存在'
    return pack_store.create_pack(new_id, name or f'{src_id}-副本',
                                  description or f'复制自 {src_id}',
                                  copy_from=src_id)


__all__ = [
    'EXPORT_SECTIONS',
    'MAX_IMPORT_BYTES',
    'conflict_report',
    'copy_pack',
    'dumps_pack',
    'export_pack',
    'export_pack_full',
    'export_shape_of',
    'import_pack',
    'parse_payload',
    'restore_builtin',
    'validate_import',
]
