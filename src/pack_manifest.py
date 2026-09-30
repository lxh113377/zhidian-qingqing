"""行业包清单（manifest）与兼容性闸门 —— 对标 Odoo 模块 ``__manifest__.py`` 与
InvenTree 插件注册表的 ``MIN_VERSION / MAX_VERSION + check_version()``。

治的问题（对标报告 §3.2「模块治理」差距）：行业包可跨环境流动（DIY 工作台导出
→ 另一套部署导入、AI 造包、多部署副本），而 ``industry_pack._normalize_pack()``
只认固定键 —— 更新版本导出的包一旦落到旧平台，包内新段落会被**静默忽略**，表现为
「导入成功但功能缺一半」。清单把这种隐性不兼容前移为激活/导入处的显式拒绝 + 可读原因。

三级取数（与内容段同优先级）：
  1. DB 覆盖层 ``PackItem(section='manifest')``（导入包随载荷携带）；
  2. 文件包 ``src/industry_packs/<id>/manifest.json``（独立清单文件，不改 pack.json 字节）；
  3. 合成 —— 历史包未声明时按「与当前平台兼容」处理（``declared=False``，零行为变化）。

字段：``pack_version`` / ``schema_min`` / ``schema_max`` / ``requires`` /
``vendor`` / ``exported_at`` / ``integrity``。``integrity`` 是文件层内容指纹
（覆盖包目录内除 manifest.json 自身外的全部 .json/.md），默认只告警；
``PACK_INTEGRITY_STRICT=1`` 时不匹配即拒绝激活 —— 之所以默认不做强约束，是因为本机
``core.autocrlf=true`` 且仓库无 .gitattributes，检出字节随行尾策略变化属正常现象，
硬拦会把正常换行差异误判成篡改（2026-09-24 实测）。

CLI：``python -m src.pack_manifest [--json] [--seal <pack_id> | --seal-all]``
退出码：0 = 全部兼容；1 = 存在不兼容包（``--seal`` 写回 manifest.json，幂等）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

# 清单文件名（文件层唯一声明处；pack.json 不参与，避免同一层两个真相源）
MANIFEST_FILE = 'manifest.json'
# 清单字段格式版本（与「平台包协议」PACK_SCHEMA_VERSION 不是一回事，勿混用）
MANIFEST_FORMAT = 1
_SHA_PREFIX = 'sha256:'
_SEMVER_RE = re.compile(r'^\d{1,4}(\.\d{1,4}){0,2}$')
_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')
# 上限防护（清单来自外部载荷，与包内容段同一治理口径）
MAX_REQUIRES = 12
MAX_INTEGRITY_FILES = 60
MAX_VENDOR_LEN = 24
MAX_UNKNOWN_KEYS = 12
_INTEGRITY_SUFFIXES = ('.json', '.md')
_KNOWN_KEYS = frozenset({'manifest_format', 'pack_id', 'pack_version', 'schema_min',
                         'schema_max', 'requires', 'vendor', 'exported_at', 'integrity'})

# 平台能力清单（唯一真相源）：包可声明依赖其中若干项，未列出的即判为不兼容。
# 每项对应一个已交付的平台特性，包内容能否被完整消费取决于它在目标部署上是否存在。
CAPABILITIES = frozenset({
    'free_metrics',        # P2 自由填报指标（MetricDef + 公式模板）
    'pack_products',       # P7-B2 产品随包（Product.pack_id）
    'pack_scoped_unique',  # P7-C 填报唯一约束含 pack_id
    'multi_store',         # PH2 多门店与门店级激活包
    'custom_terms',        # P7-B1 H5 骨架字段与词表随包
    'dashboard_labels',    # P7-B3 看板标签/单位随包
    'negotiation',         # P5 动态谈判情景池
    'decision_cockpit',    # PH5 四角色决策驾驶舱
})


def platform_schema_version():
    """当前平台的行业包协议版本（真相源仍是 industry_pack.PACK_SCHEMA_VERSION）。"""
    from . import industry_pack

    return industry_pack.PACK_SCHEMA_VERSION


def integrity_strict():
    """文件层指纹不匹配是否升级为拒绝（默认 False = 只告警）。"""
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            value = current_app.config.get('PACK_INTEGRITY_STRICT')
            if value is not None:
                return bool(value)
    except Exception:  # pragma: no cover  # noqa: S110  理由：CLI 无上下文即回落环境变量
        pass
    return os.environ.get('PACK_INTEGRITY_STRICT', '').strip().lower() in ('1', 'true', 'yes', 'on')


def _pos_int(raw, default, field, problems):
    """清单整数字段：缺省取默认值；非法（含 bool）记问题并回默认。"""
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        problems.append(f'{field} 必须是 ≥1 的整数（当前 {raw!r}）')
        return default
    return raw


def normalize(raw, *, version=''):  # noqa: PLR0912
    """归一化清单声明 → ``(manifest, problems)``。

    manifest 恒返回（未声明时合成兼容态），problems 为结构非法项（由 check_pack
    计入拒绝原因）。本函数不读盘、不判平台版本，便于纯数据隔离测试。
    """
    schema = platform_schema_version()
    if not isinstance(raw, dict) or not raw:
        return {'declared': False, 'manifest_format': MANIFEST_FORMAT,
                'pack_version': str(version or ''), 'schema_min': 1, 'schema_max': schema,
                'requires': [], 'vendor': 'legacy', 'exported_at': '', 'integrity': {},
                'unknown_keys': []}, []

    problems: list[str] = []
    fmt = _pos_int(raw.get('manifest_format'), MANIFEST_FORMAT, 'manifest_format', problems)
    if fmt > MANIFEST_FORMAT:
        problems.append(f'manifest_format 不支持：v{fmt}（本平台清单格式 v{MANIFEST_FORMAT}）')
    pack_version = str(raw.get('pack_version') or version or '').strip()
    if not _SEMVER_RE.match(pack_version):
        problems.append(f'pack_version 格式非法：{pack_version!r}（应为 1.2.3 形式的数字版本号）')
    schema_min = _pos_int(raw.get('schema_min'), 1, 'schema_min', problems)
    schema_max = _pos_int(raw.get('schema_max'), schema, 'schema_max', problems)
    if schema_min > schema_max:
        problems.append(f'兼容区间倒置：schema_min {schema_min} > schema_max {schema_max}')

    requires_raw = raw.get('requires')
    if requires_raw is None:
        requires = []
    elif not isinstance(requires_raw, list) or len(requires_raw) > MAX_REQUIRES:
        problems.append(f'requires 必须是不超过 {MAX_REQUIRES} 项的列表')
        requires = []
    else:
        requires = [str(x).strip() for x in requires_raw]
        if any(not x for x in requires):
            problems.append('requires 含空项')
            requires = [x for x in requires if x]

    integrity_raw = raw.get('integrity')
    if integrity_raw is None:
        integrity = {}
    elif not isinstance(integrity_raw, dict) or len(integrity_raw) > MAX_INTEGRITY_FILES:
        problems.append(f'integrity 必须是不超过 {MAX_INTEGRITY_FILES} 项的映射')
        integrity = {}
    else:
        integrity = {}
        for path, digest in integrity_raw.items():
            rel = str(path).replace('\\', '/').strip()
            if not rel or rel.startswith('/') or '..' in rel.split('/') or not digest:
                problems.append(f'integrity 路径非法：{path!r}')
                continue
            value = str(digest).strip().lower()
            if not value.startswith(_SHA_PREFIX) or not _HEX64_RE.match(value[len(_SHA_PREFIX):]):
                problems.append(f'integrity 摘要非法（应为 sha256:<64 位十六进制>）：{rel}')
                continue
            integrity[rel] = value

    unknown = sorted(str(k) for k in raw if k not in _KNOWN_KEYS)
    if len(unknown) > MAX_UNKNOWN_KEYS:
        unknown = [*unknown[:MAX_UNKNOWN_KEYS], '…']
    return {'declared': True, 'manifest_format': fmt, 'pack_version': pack_version,
            'schema_min': schema_min, 'schema_max': schema_max, 'requires': requires,
            'vendor': str(raw.get('vendor') or '')[:MAX_VENDOR_LEN],
            'exported_at': str(raw.get('exported_at') or '')[:32],
            'integrity': integrity, 'unknown_keys': unknown}, problems


def read_declared(pack_id):
    """读文件包清单（``<pack_dir>/manifest.json``；缺失/坏 JSON/非对象 → None）。"""
    from . import industry_pack

    base = industry_pack.pack_dir(pack_id)
    if not base:
        return None
    raw = _read_json(os.path.join(base, MANIFEST_FILE))
    return raw if isinstance(raw, dict) else None


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning('[pack_manifest] 清单读取失败 %s: %s', os.path.basename(path), exc)
        return None


def file_integrity(pack_id):
    """文件层内容指纹：包目录内除 manifest.json 外的 .json/.md（含 knowledge/）。

    Returns:
        dict: ``{相对路径: 'sha256:<hex>'}``；目录不可解析时返回空 dict。
    """
    from . import industry_pack

    base = industry_pack.pack_dir(pack_id)
    if not base or not os.path.isdir(base):
        return {}
    out = {}
    for root, _dirs, files in os.walk(base):
        for fn in sorted(files):
            if not fn.endswith(_INTEGRITY_SUFFIXES) or fn == MANIFEST_FILE:
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, base).replace(os.sep, '/')
            digest = _sha256_file(path)
            if digest:
                out[rel] = _SHA_PREFIX + digest
    return dict(sorted(out.items()))


def _sha256_file(path):
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        logger.warning('[pack_manifest] 指纹计算失败 %s: %s', path, exc)
        return ''


def integrity_diff(pack_id, integrity):
    """比对声明指纹与磁盘实况 → ``{'missing':[], 'changed':[], 'extra':[], 'ok':bool}``。"""
    actual = file_integrity(pack_id)
    declared = {str(k).replace('\\', '/'): str(v).strip().lower() for k, v in (integrity or {}).items()}
    missing = sorted(p for p in declared if p not in actual)
    extra = sorted(p for p in actual if p not in declared)
    changed = sorted(p for p, d in declared.items() if p in actual and actual[p] != d)
    return {'missing': missing, 'changed': changed, 'extra': extra,
            'ok': not (missing or changed or extra)}


def check_pack(pack, *, pack_id=None):
    """兼容性闸门（激活与导入共用同一判定）。

    Returns:
        dict: ``{'ok', 'errors', 'warnings', 'manifest', 'platform_schema'}``；
        ``ok=False`` 时调用方必须拒绝（激活/导入），并把 errors 原文给管理员看。
    """
    pid = pack_id or (pack.get('id') if isinstance(pack, dict) else '') or ''
    manifest, problems = normalize(pack.get('manifest'),
                                  version=str(pack.get('version') or ''))
    errors = list(problems)
    warnings: list[str] = []
    schema = platform_schema_version()
    if manifest['declared']:
        s_min, s_max = manifest['schema_min'], manifest['schema_max']
        if schema < s_min:
            errors.append(f'该包要求平台包协议至少 v{s_min}，当前平台为 v{schema}'
                          '（包由更新版本平台导出，强行激活会静默丢失它携带的新内容）')
        if schema > s_max:
            errors.append(f'该包声明最高兼容平台包协议 v{s_max}，当前平台为 v{schema}'
                          '（包版本过旧，请在新平台重新导出或重建为新包）')
        missing_caps = [c for c in manifest['requires'] if c not in CAPABILITIES]
        if missing_caps:
            errors.append('该包声明依赖本系统未提供的能力：' + '、'.join(missing_caps))
        if manifest['unknown_keys']:
            warnings.append('清单含本版本未识别的字段（已忽略）：'
                            + '、'.join(manifest['unknown_keys']))
        if manifest['integrity'] and pack.get('source') == 'file':
            diff = integrity_diff(pid, manifest['integrity'])
            if not diff['ok']:
                detail = []
                if diff['changed']:
                    detail.append('内容与清单不一致：' + '、'.join(diff['changed'][:5]))
                if diff['missing']:
                    detail.append('清单声明的文件缺失：' + '、'.join(diff['missing'][:5]))
                if diff['extra']:
                    detail.append('清单未记录的文件：' + '、'.join(diff['extra'][:5]))
                message = '行业包文件层校验和未通过（' + '；'.join(detail) + '）'
                if integrity_strict():
                    errors.append(message + '；已开启 PACK_INTEGRITY_STRICT，拒绝激活')
                else:
                    warnings.append(message + '；如需强制拦截请设 PACK_INTEGRITY_STRICT=1')
    return {'ok': not errors, 'errors': errors, 'warnings': warnings,
            'manifest': manifest, 'platform_schema': schema}


def build_manifest(pack_id, *, vendor='builtin', base=None):
    """为文件包构建清单（含当前文件层指纹），用于导出与 --seal 写回。

    Args:
        base: 既有清单（保留人工声明过的 vendor/exported_at 等字段）。
    """
    from . import industry_pack

    pack = industry_pack.get_pack(pack_id)
    schema = platform_schema_version()
    existing = base if isinstance(base, dict) else (read_declared(pack_id) or {})
    return {'manifest_format': MANIFEST_FORMAT,
            'pack_id': pack_id,
            'pack_version': str((existing.get('pack_version') if isinstance(existing, dict) else '')
                                or pack.get('version') or '1.0.0'),
            'schema_min': 1,
            'schema_max': schema,
            'requires': sorted(CAPABILITIES),
            'vendor': str((existing.get('vendor') if isinstance(existing, dict) else '') or vendor),
            'exported_at': str(existing.get('exported_at') if isinstance(existing, dict) else '')[:32],
            'integrity': file_integrity(pack_id)}


def build_export_manifest(pack_id, pack=None):
    """导出载荷用的溯源清单：**不含**文件层校验和（理由见 pack_io 注释）。

    已声明过的字段原样带出（requires / pack_version / vendor / exported_at），
    只对未声明的历史包补当前平台默认值 —— 保证「导出→导入→再导出」字节稳定，
    不引入日期一类的非确定字段。
    """
    from . import industry_pack

    pack = pack if isinstance(pack, dict) else industry_pack.get_pack(pack_id)
    declared = pack.get('manifest') if isinstance(pack.get('manifest'), dict) else {}
    return {'manifest_format': MANIFEST_FORMAT,
            'pack_id': pack_id,
            'pack_version': str(declared.get('pack_version') or pack.get('version') or '1.0.0'),
            'schema_min': int(declared.get('schema_min') or 1),
            'schema_max': int(declared.get('schema_max') or platform_schema_version()),
            'requires': sorted(declared.get('requires') or CAPABILITIES),
            'vendor': str(declared.get('vendor') or 'exported')[:MAX_VENDOR_LEN],
            'exported_at': str(declared.get('exported_at') or '')[:32]}


def seal_pack(pack_id):
    """把清单写回 ``<pack_dir>/manifest.json``（幂等；不触碰 pack.json 与内容文件）。

    Returns:
        tuple: ``(ok, message)``。包目录不可解析或写盘失败时返回 False。
    """
    from . import industry_pack

    base = industry_pack.pack_dir(pack_id)
    if not base or not os.path.isdir(base):
        return False, f'包 {pack_id} 无文件包目录，跳过清单写回'
    payload = build_manifest(pack_id)
    target = os.path.join(base, MANIFEST_FILE)
    body = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    try:
        with open(target, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(body)
    except OSError as exc:
        logger.warning('[pack_manifest] 清单写回失败 %s: %s', target, exc)
        return False, f'清单写回失败：{exc}'
    industry_pack.invalidate()
    return True, (f'已写入 {pack_id}/{MANIFEST_FILE}'
                  f'（覆盖 {len(payload["integrity"])} 个文件的校验和）')


def manifest_view(pack):
    """供后台渲染的清单视图（已归一化 + 判定结果）。"""
    check = check_pack(pack)
    m = check['manifest']
    return {'declared': m['declared'], 'pack_version': m['pack_version'],
            'schema_min': m['schema_min'], 'schema_max': m['schema_max'],
            'requires': m['requires'], 'vendor': m['vendor'],
            'exported_at': m['exported_at'],
            'integrity_count': len(m['integrity']),
            'platform_schema': check['platform_schema'],
            'ok': check['ok'], 'errors': check['errors'], 'warnings': check['warnings']}


def main(argv=None):
    """CLI：核对全部文件包兼容性；``--seal-all`` / ``--seal <id>`` 写回清单。"""
    from . import industry_pack

    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = '--json' in argv
    ids = industry_pack.list_pack_ids()
    results = {}
    sealed = []
    for pid in ids:
        if '--seal' in argv:
            idx = argv.index('--seal')
            target = argv[idx + 1] if idx + 1 < len(argv) else ''
            if pid != target:
                continue
        elif '--seal-all' in argv:
            ok, msg = seal_pack(pid)
            sealed.append((pid, ok, msg))
        pack = industry_pack.get_pack(pid)
        results[pid] = check_pack(pack)

    if '--seal-all' in argv:
        for _pid, ok, msg in sealed:
            print(('  ✅ ' if ok else '  ❌ ') + msg)
    if as_json:
        print(json.dumps({k: {'ok': v['ok'], 'errors': v['errors'], 'warnings': v['warnings'],
                             'manifest': v['manifest']} for k, v in results.items()},
                         ensure_ascii=False, indent=2))
    else:
        for pid, res in results.items():
            m = res['manifest']
            flag = '✅' if res['ok'] else '❌'
            print(f'{flag} {pid}: 清单={"声明" if m["declared"] else "合成(legacy)"} '
                  f'v{m["pack_version"] or "-"} 兼容区间 v{m["schema_min"]}-v{m["schema_max"]} '
                  f'能力 {len(m["requires"])} 项 校验和 {len(m["integrity"])} 文件')
            for line in res['errors'] + res['warnings']:
                print(f'     · {line}')
        bad = [p for p, r in results.items() if not r['ok']]
        print(f'\n共 {len(results)} 个文件包，平台包协议 v{platform_schema_version()}，'
              f'不兼容 {len(bad)} 个' + ('：' + '、'.join(bad) if bad else ''))
    return 1 if any(not r['ok'] for r in results.values()) else 0


if __name__ == '__main__':  # pragma: no cover
    import sys

    sys.exit(main())
