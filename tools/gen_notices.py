#!/usr/bin/env python
"""生成 `THIRD-PARTY-NOTICES.md`（第 18 轮 M50，对标 Airflow/Zulip 的第三方声明清单）。

纯标准库：读 `requirements.txt` 的声明名 → 用 importlib.metadata 取**实际安装**的
版本与许可证标识 → 输出清单。刻意不联网、不猜版本号。
覆盖判据由 L4 校验：清单里必须出现每一条已安装的声明包（漏一条=清单不完整）。
"""
import os
import sys
from importlib import metadata

DEV = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 台账落在副本内（与 requirements.txt 同目录）：这样它能进 ROOT_DELIVERED 被第 10 项
# 三副本一致校验，且 L4 能在任一份副本里以同一路径断言覆盖率——放仓库根就成了无人守的第四份拷贝。
OUT = os.path.join(DEV, 'THIRD-PARTY-NOTICES.md')


def declared():
    """requirements.txt 的 (声明名, 声明约束)，保留声明顺序。

    刻意**复用门禁第 46 项的解析器**而不是再写一份正则：第 32 轮实测到本清单的
    "声明约束"列停在加下界之前的旧值（`coverage>=7.0` 而契约已是 `>=7.0,<9`），
    根因就是"同一个事实两份语法"——生成方与判据方各自漂移，谁都发现不了。
    """
    if DEV not in sys.path:
        sys.path.insert(0, DEV)
    import quality_gate as _qg
    with open(os.path.join(DEV, 'requirements.txt'), encoding='utf-8') as fh:
        return _qg._s46_declared_names(fh.read())


def meta_of(name):
    try:
        d = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None
    lic = (d.metadata.get('License') or '').strip()
    cls = [v.split(':')[-1].strip() for k, v in d.metadata.items()
           if k == 'Classifier' and v.strip().startswith('License ::')]
    short = cls[-1] if cls else (lic.splitlines()[0][:60] if lic else '（元数据未标注许可证）')
    url = (d.metadata.get('Home-page') or d.metadata.get('Project-URL') or '').strip()
    return {'version': d.version, 'license': short, 'url': url.split(',')[-1].strip()}


def previous_rows():
    """读旧清单里**已实测过**的行：{声明名小写: (版本, 许可证, 上游)}。

    为什么必须保留：本工具跑在哪个解释器，就只有那个解释器的已装集。部署用的包
    （pymysql/waitress）在开发机上没装过，一重新生成就把上次在部署环境实测到的版本行
    擦成"—"——那是**证据退化**，不是"重新测量"。缺装时沿用旧行并标出来，比抹掉更诚实。
    """
    if not os.path.isfile(OUT):
        return {}
    keep = {}
    for ln in open(OUT, encoding='utf-8').read().splitlines():
        cells = [c.strip() for c in ln.strip().strip('|').split('|')]
        if len(cells) != 5 or not cells[0].startswith('`'):
            continue
        name = cells[0].strip('` ')
        ver = cells[2].strip('` ')
        if not ver or '未安装' in ver:
            continue
        keep[name.lower()] = (ver, cells[3], cells[4])
    return keep


def main():
    lines = ['# THIRD-PARTY-NOTICES — 第三方依赖声明',
             '',
             '> 本文件由 `tools/gen_notices.py` 生成（第 18 轮 M50，对标 Airflow/Zulip 的第三方声明清单）。',
             '> 版本与许可证取自**当前解释器的已安装元数据**（importlib.metadata），不猜、不手写；',
             '> 本环境未装的项**沿用上一次实测记录并标注出处**（缺装不等于依赖不存在，抹掉旧行＝证据退化）。',
             '> 覆盖完整性由 `test_l4_assertions.py` 的 S18 断言校验（漏一条即红）。',
             '',
             '| 声明名 | 声明约束 | 实际安装版本 | 许可证（元数据） | 上游 |',
             '|---|---|---|---|---|']
    missing, carried = [], []
    prev = previous_rows()
    for name, spec in declared():
        m = meta_of(name)
        if not m:
            missing.append(name)
            old = prev.get(name.lower())
            if old:
                carried.append(name)
                lines.append(f"| `{name}` | `{spec or '（未写约束）'}` | {old[0]}"
                             f"（沿用上次实测） | {old[1]} | {old[2]} |")
            else:
                lines.append(f'| `{name}` | `{spec or "（未写约束）"}` | （本环境未安装，'
                             f'且无历史读数） | — | — |')
            continue
        lines.append(f"| `{name}` | `{spec or '（未写约束）'}` | {m['version']} | "
                     f"{m['license']} | {m['url'] or '—'} |")
    lines += ['', '## 随仓第三方前端产物（vendored）',
              '',
              '| 文件 | 上游 | 版本 | 许可证 | 完整性 |',
              '|---|---|---|---|---|']
    vj = os.path.join(DEV, 'VENDOR-ASSETS.json')
    if os.path.isfile(vj):
        import json
        for a in json.load(open(vj, encoding='utf-8')).get('assets', []):
            lines.append(f"| `{a['path']}` | {a.get('upstream', '—')} | {a.get('version', '—')} | "
                         f"{a.get('upstream_license', '—')} | SHA256 `{str(a.get('sha256'))[:16]}…`"
                         f"（门禁第 27 项逐条核对） |")
    lines += ['', '## 未决事项', '',
              '- 本仓库**根 LICENSE 文件仍未选定**（属授权条款决策，须由维护者决定，不由工具代选）。',
              '- CI 里 `pip install "ruff==0.16.5"` 属开发期工具，非运行时依赖，故不进上表；'
              '其引用固定由门禁第 26 项校验。']
    if missing:
        lines += ['', f'> ⚠️ 本环境未安装的声明项：{", ".join(missing)}'
                      '（不代表依赖不存在；'
                      + (f'其中 {", ".join(carried)} 沿用上一次在别处实测到的版本行，' if carried else '')
                      + '本工具只标"沿用上次实测"，不猜、不手写，也不删旧行）']
    with open(OUT, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines) + '\n')
    print(f'✅ 已生成 {OUT}：声明 {len(declared())} 条 / 未安装 {len(missing)} 条')
    return 0


if __name__ == '__main__':
    sys.path.insert(0, DEV)
    raise SystemExit(main())
