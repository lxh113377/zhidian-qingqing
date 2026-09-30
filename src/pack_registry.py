"""行业包机读能力索引（pack_registry.json）—— 对标 PostHog ``products.json``
缺失即硬失败并给出生成命令（``posthog/products.py:29-43`` 自动发现 +
``:67-79`` 注册表缺失即 ``FileNotFoundError``）。

治的问题（对标第 50 轮 G3/M36）：平台能力位（``pack_manifest.CAPABILITIES``）
只活在代码里，没有一份机读清单回答「这套部署上有哪些包、每个包声明了什么」。
后果有二：① 新包声明了未知能力，错误只在激活时按包逐个暴露，没有全局视图；
② 包文件被改（加包/删包/改 manifest），没有任何判据能说出"索引与实盘分叉"。

形态（刻意不照抄动态装载）：索引是**派生账本**不是手写配置 —— 唯一写入口是
``--write``（从已 seal 的 manifest.json 逐包生成，键排序、格式固定）；
``load_registry()`` 缺件即报缺件（退出码 2 = 不可验，**绝不塌成 0 通过**）；
``check_registry()`` 做索引 ⇄ 实盘双向对账（漏登记/幽灵条目/字段漂移即红）。

文件落点：``src/industry_packs/pack_registry.json``（随 src/** 进三副本同步面）。
"""
from __future__ import annotations

import json
import logging
import os
import sys

logger = logging.getLogger(__name__)

# 索引文件名（industry_packs 目录内唯一真相源；pack.json 不参与，避免双源）
REGISTRY_FILE = 'pack_registry.json'
# 索引格式版本（与 MANIFEST_FORMAT / PACK_SCHEMA_VERSION 均不是一回事，勿混用）
REGISTRY_FORMAT = 1
# 条目字段白名单（只收这组键，多余键即漂移；与 pack_manifest 的声明段同源）
ENTRY_KEYS = frozenset({'pack_version', 'schema_min', 'schema_max', 'requires',
                        'integrity_count', 'manifest_format', 'declared'})


def registry_path():
    """索引文件绝对路径（目录不可解析时返回空串，调用方判不可验）。"""
    try:
        from . import industry_pack

        base = os.path.dirname(os.path.abspath(industry_pack.__file__))
        packs = os.path.join(base, 'industry_packs')
    except Exception as exc:  # pragma: no cover  # noqa: S110  理由：目录失联即不可验（缺目录只判不可验，不断言包不存在）
        logger.warning('[pack_registry] 包目录定位失败: %s', exc)
        return ''
    return os.path.join(packs, REGISTRY_FILE)


def build_registry():
    """从实盘逐包生成索引 → ``{pack_id: 条目}``（键排序；不读索引自身）。"""
    from . import industry_pack
    from . import pack_manifest as pm

    out = {}
    for pid in sorted(industry_pack.list_pack_ids()):
        pack = industry_pack.get_pack(pid)
        check = pm.check_pack(pack, pack_id=pid)
        m = check['manifest']
        out[pid] = {'pack_version': m['pack_version'],
                    'schema_min': m['schema_min'],
                    'schema_max': m['schema_max'],
                    'requires': sorted(m['requires']),
                    'integrity_count': len(m['integrity']),
                    'manifest_format': m['manifest_format'],
                    'declared': bool(m['declared'])}
    return out


def dump_registry(registry):
    """确定性序列化（排序键 + 2 空格 + LF；保证 write→read→write 字节稳定）。"""
    payload = {'registry_format': REGISTRY_FORMAT, 'packs': registry}
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n'


def write_registry():
    """唯一写入口：重算全量索引并落盘（幂等；写失败返回 False）。"""
    target = registry_path()
    if not target:
        return False, '包目录不可解析，索引不可写（不可验，不得记通过）'
    body = dump_registry(build_registry())
    try:
        with open(target, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(body)
    except OSError as exc:
        logger.warning('[pack_registry] 索引写回失败 %s: %s', target, exc)
        return False, f'索引写回失败：{exc}'
    return True, f'已写入 {os.path.basename(target)}'


def load_registry():
    """读索引 → ``(registry|None, problems)``。

    缺件/坏 JSON/非对象一律 problems 非空（调用方必须判红或不可验，
    禁止把"读不到"当成"无漂移"通过）。
    """
    target = registry_path()
    if not target:
        return None, ['包目录不可解析，索引不可验']
    if not os.path.isfile(target):
        return None, [f'能力索引缺失：{REGISTRY_FILE} 不存在，'
                       '请跑 python -m src.pack_registry --write 生成（缺件=不可验，不得记通过）']
    try:
        with open(target, encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f'能力索引解析失败：{exc}（请重跑 --write）']
    if not isinstance(raw, dict) or not isinstance(raw.get('packs'), dict):
        return None, ['能力索引形状非法（应为 {"packs": {...}}，请重跑 --write）']
    if raw.get('registry_format') != REGISTRY_FORMAT:
        return None, [f'能力索引格式 v{raw.get("registry_format")} 与本平台 v{REGISTRY_FORMAT} 不一致'
                       '（请重跑 --write）']
    return raw['packs'], []


def check_registry(indexed=None):
    """索引 ⇄ 实盘双向对账 → ``{'ok', 'errors', 'entries', 'packs'}``。

    三类红因：① 实盘有包而索引无条目（漏登记）；② 索引有条目而实盘无包（幽灵）；
    ③ 字段漂移（版本/兼容区间/requires/指纹数/格式/声明态任一不等）。

    ``indexed`` 为测试缝：传 dict 即跳过落盘读取，直接对账（线上路径恒传 None）。
    """
    live = build_registry()
    if indexed is None:
        indexed, problems = load_registry()
    else:
        problems = []
    errors = list(problems)
    if indexed is None:
        return {'ok': False, 'errors': errors, 'entries': 0, 'packs': sorted(live)}
    for pid in sorted(set(live) | set(indexed)):
        if pid not in indexed:
            errors.append(f'索引漏登记实盘包：{pid}（跑 --write 重算）')
        elif pid not in live:
            errors.append(f'索引含幽灵条目：{pid}（实盘无此包，跑 --write 重算）')
            continue
        else:
            want = live[pid]
            got = indexed[pid]
            if not isinstance(got, dict):
                errors.append(f'索引条目形状非法：{pid}（应为对象，跑 --write 重算）')
                continue
            extra = sorted(k for k in got if k not in ENTRY_KEYS)
            if extra:
                errors.append(f'索引条目含未知键：{pid} 多出 {extra}')
            for key in sorted(ENTRY_KEYS):
                if got.get(key) != want.get(key):
                    errors.append(f'索引与实盘分叉：{pid}.{key} 索引={got.get(key)!r} 实盘={want.get(key)!r}')
                    break
    return {'ok': not errors, 'errors': errors,
            'entries': len(indexed), 'packs': sorted(live)}


def main(argv=None):
    """CLI：``--write`` 重算落盘；``--check`` 对账；``--json`` 机器读数。

    退出码：0 = 通过；1 = 失配/漂移；2 = 缺件不可验（不是通过）。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = '--json' in argv
    if '--write' in argv:
        ok, msg = write_registry()
        print(('✅ ' if ok else '❌ ') + msg)
        return 0 if ok else 2
    if not os.path.isfile(registry_path()):
        msg = f'能力索引缺失（{REGISTRY_FILE}），跑 python -m src.pack_registry --write 生成'
        if as_json:
            print(json.dumps({'ok': False, 'errors': [msg]}, ensure_ascii=False))
        else:
            print('❌ ' + msg)
        return 2
    res = check_registry()
    if as_json:
        print(json.dumps({'ok': res['ok'], 'errors': res['errors'],
                          'entries': res['entries'], 'packs': res['packs']},
                         ensure_ascii=False, indent=2))
    else:
        print(('✅ ' if res['ok'] else '❌ ')
              + f'能力索引条目 {res["entries"]} / 实盘包 {len(res["packs"])}')
        for line in res['errors']:
            print(f'     · {line}')
    return 0 if res['ok'] else 1


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
