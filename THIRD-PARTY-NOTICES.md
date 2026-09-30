# THIRD-PARTY-NOTICES — 第三方依赖声明

> 本文件由 `tools/gen_notices.py` 生成（第 18 轮 M50，对标 Airflow/Zulip 的第三方声明清单）。
> 版本与许可证取自**当前解释器的已安装元数据**（importlib.metadata），不猜、不手写；
> 本环境未装的项**沿用上一次实测记录并标注出处**（缺装不等于依赖不存在，抹掉旧行＝证据退化）。
> 覆盖完整性由 `test_l4_assertions.py` 的 S18 断言校验（漏一条即红）。

| 声明名 | 声明约束 | 实际安装版本 | 许可证（元数据） | 上游 |
|---|---|---|---|---|
| `Flask` | `>=3.0.3,<3.2` | 3.1.3 | （元数据未标注许可证） | https://flask.palletsprojects.com/page/changes/ |
| `Flask-SQLAlchemy` | `==3.1.1` | 3.1.1 | BSD License | https://flask-sqlalchemy.palletsprojects.com/changes/ |
| `Flask-Login` | `==0.6.3` | 0.6.3 | MIT License | https://github.com/maxcountryman/flask-login |
| `Flask-WTF` | `>=1.2.1,<1.4` | 1.3.0 | BSD License | https://flask-wtf.readthedocs.io/ |
| `WTForms` | `>=3.1.1,<4` | 3.2.2 | BSD License | https://wtforms.readthedocs.io |
| `pandas` | `>=2.2,<4` | 3.0.3 | BSD License | https://pandas.pydata.org |
| `requests` | `>=2.31,<3` | 2.34.2 | Apache Software License | https://requests.readthedocs.io |
| `python-dotenv` | `>=1.0,<2` | 1.2.2 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| `pymysql` | `>=1.1,<2` | 1.2.3（沿用上次实测）（沿用上次实测）（沿用上次实测） | （元数据未标注许可证） | https://github.com/PyMySQL/PyMySQL |
| `waitress` | `>=3.0,<4` | 3.0.2（沿用上次实测）（沿用上次实测）（沿用上次实测） | Zope Public License | https://github.com/Pylons/waitress |
| `coverage` | `>=7.0,<9` | 7.16.1 | Apache-2.0 | https://github.com/coveragepy/coveragepy |
| `tzdata` | `>=2024.1,<2027` | 2026.3 | Apache-2.0 | https://github.com/python/tzdata |
| `filelock` | `>=4.0,<5` | 4.0.4 | MIT License | https://py-filelock.readthedocs.io |
| `PyYAML` | `>=6.0,<7` | 6.0.3 | MIT License | https://pyyaml.org/ |

## 随仓第三方前端产物（vendored）

| 文件 | 上游 | 版本 | 许可证 | 完整性 |
|---|---|---|---|---|
| `src/static/js/echarts.custom.js` | Apache ECharts | 5.6.1（产物内 var RD="5.6.1"；同文件另有 zrender 版本串 5.6.0，故构建期依赖版本一并记在此） | Apache-2.0 | SHA256 `9783385d1eaf2d31…`（门禁第 27 项逐条核对） |

## 未决事项

- 本仓库**根 LICENSE 文件仍未选定**（属授权条款决策，须由维护者决定，不由工具代选）。
- CI 里 `pip install "ruff==0.16.5"` 属开发期工具，非运行时依赖，故不进上表；其引用固定由门禁第 26 项校验。

> ⚠️ 本环境未安装的声明项：pymysql, waitress（不代表依赖不存在；其中 pymysql, waitress 沿用上一次在别处实测到的版本行，本工具只标"沿用上次实测"，不猜、不手写，也不删旧行）
