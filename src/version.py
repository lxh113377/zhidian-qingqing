"""应用版本单一真相源（第 17 轮 H28，对标 Appwrite `APP_VERSION_STABLE` / Chatwoot `VERSION_CW`）。

为什么要有这个文件
------------------
此前全仓**没有任何运行时可见的应用版本号**：`v1.6.0` 只活在 git tag、GitHub Release 与
README 的文字里。结果是线上出问题时，仅凭 HTTP 无法回答两个最基本的问题
——「这台跑的是哪一版」与「它的库跟到了第几步」。第二个问题由 `src/schema_ledger.py`
（H27 迁移账本）回答，本文件回答第一个。

三条约定（门禁第 25 项逐条对账，违约即红）
----------------------------------------
1. `APP_VERSION` 是**唯一**版本字面量，其余各处一律引用它，禁止再抄一份；
2. 版本号遵循仓内既有约定（`README.md` §版本与发布）：major=1 / minor=阶段(PH) / patch=轮次；
3. 该值必须与根 `README.md` 的版本行、`UPGRADE.md` 的「当前版本」行逐字一致 ——
   文档写版本而代码不写，是"看起来有版本契约"的假形态。

git commit 不进本文件：CI 浅克隆（depth=1）下 `git describe` 会失败，
把 git 派生值当判据会造成"本地绿、CI 红"，故 commit 只经环境变量注入、仅用于展示。
"""
import os
import re

# 语义版本（不含 v 前缀）。改这一处即改全局。
APP_VERSION = '1.28.0'

VERSION_RE = re.compile(r'^\d+\.\d+\.\d+$')


def is_valid_version(value=None):
    """版本字面量是否合法（供门禁与自测复用，避免第二处写正则）。"""
    return bool(VERSION_RE.match(value if value is not None else APP_VERSION))


def git_commit():
    """部署注入的 commit 短号；未注入则空串（不猜、不跑 git 子进程）。"""
    return (os.environ.get('ICAN_GIT_COMMIT') or '').strip()[:40]


def payload():
    """`/healthz` 的 version 段。"""
    return {'app': APP_VERSION, 'commit': git_commit()}
