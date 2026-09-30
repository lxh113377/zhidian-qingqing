"""迁移补丁注册表（声明侧）+ 执行账本查询（第 17 轮 H27/H28）。

为什么声明放这里、执行体放 `init_db.py`
--------------------------------------
`/healthz` 必须能回答「这台实例的库跟到第几步」，而 `init_db.py` 在 **import 期**带生产守卫
（`FLASK_ENV=production` 且无 `ICAN_INIT_CONFIRM` 时直接 `sys.exit(1)`）——从请求里 import 它
等于把探活端点变成自杀端点。故本模块只承载**声明与查询**（无执行体、无 import 副作用），
执行体由 `init_db._PATCH_IMPLS` 按 `pid` 绑定；绑定缺失即 `KeyError`（宁可响，不静默少跑一个补丁）。
门禁第 24 项再双向对账「声明名单 ⇄ 实现名单 ⇄ 实库账本 ⇄ UPGRADE.md」，
防"声明了没实现"与"实现了没声明"两种漂移。

对标口径（只抄判据不抄机制）
----------------------------
- Ghost `migrations` + `migrations_lock`：跑过什么可查，且要求补丁本身幂等（跑两次安全）；
- Metabase `DATABASECHANGELOG`：changeSet 缺列时用 `preConditions: onFail: MARK_RAN`
  把"已满足"记成已跑，而不是报错 —— 本模块的 `mode='noop'` 同一语义；
- Medusa `mikro_orm_migrations`：`getExecutedMigrations()` / `getPendingMigrations()` 成对 API
  ⇒ 本模块的 `status()` 一次给全 applied / pending / orphans 三个面；
- Appwrite：迁移只认代码里的版本常量，不认 tag ⇒ 版本字面量收在 `src/version.py` 一处。
"""
from dataclasses import dataclass


# 可逆性三档（第 35 轮 H127）。刻意不给默认值：新增补丁漏写 rev 就是 TypeError，
# 在 import 期响，而不是等门禁发现"有一条补丁没人想过怎么撤"。
REV_VALUES = ('auto', 'manual', 'none')
REV_MIN_NOTE = 12


@dataclass(frozen=True)
class PatchRecord:
    """一条迁移补丁的**声明**（不含执行体）。

    `pid` 一旦发布不得改名：它是 `schema_patch_log` 的主键，改名等于把存量库的
    历史记录变成孤儿账本、并把该补丁在新库上重新跑一遍。

    `rev` 是这一条**能不能撤**的声明，不是愿望：
    · `auto`   = `init_db._PATCH_REVERSERS` 里有对应反向动作，回滚命令真能跑；
    · `manual` = 只能先把库回退到备份点（第 33 轮 `restore_db.py` 那条链），不能单撤这一条；
    · `none`   = 语义上无法反推（如"把 NULL 归属回填成默认商家"——撤回去也不知道原本是谁）。
    对标：`django/db/migrations/operations/special.py:205-206` 缺 `reverse_code` 时
    直接 `raise NotImplementedError('You cannot reverse this operation')`；
    `alembic/templates/generic/script.py.mako:21/:26` 每个迁移模板都成对写 upgrade/downgrade。
    两家共同点：**反向是一等公民，不写就报错**，而不是"回滚以后再说"。
    """

    pid: str
    title: str
    kind: str      # ddl = 改结构；data = 只动数据行
    rev: str       # ∈ REV_VALUES
    rev_note: str  # 为什么是这个等级：须点名代价或前置动作（≥REV_MIN_NOTE 字）


# 顺序即执行顺序（结构补丁必须先于数据补丁：回填依赖新列已存在）
PATCHES = (
    PatchRecord('p001_ensure_columns', '旧库补齐多租户/随包新增列', 'ddl',
                'manual', '删列即丢列内数据，只能整库回退到备份点'),
    PatchRecord('p002_store_scoped_uq', '唯一约束收敛到门店范围（表重建）', 'ddl',
                'manual', '表重建期已合并过重复行，撤回旧约束会重新制造重复'),
    # 第 26 轮 S17 判据要求「结构补丁一律先于数据补丁」（回填依赖新列已存在），
    # 所以 p005/p007 排在 p003/p004/p006 之前；pid 是账本主键、一经发布不得改名，
    # 位置由本元组顺序决定，编号只表示"第几轮引入"，不承担排序职责。
    # 把 p007 按编号追加到末尾的写法第一轮就被 S17 判红 —— 新 ddl 必须插进 ddl 段。
    PatchRecord('p005_cost_snapshot', '流水行补成本快照列 + 建变更归因表', 'ddl',
                'manual', '删 change_log 表会连带丢掉变更归因历史，不可局部撤'),
    PatchRecord('p007_webhook_delivery', '建出站投递留痕表 webhook_delivery（SSRF 面与重试的账）', 'ddl',
                'manual', '留痕表是安全账，撤它等于销毁证据，须整库回退'),
    PatchRecord('p008_task_takeover', '任务表补交接备注列 takeover_json（遗留任务接管链）', 'ddl',
                'manual', '删列即丢列内交接记录，只能整库回退到备份点'),
    PatchRecord('p003_default_tenant', '建默认商家/门店并回填存量 NULL 归属', 'data',
                'none', '回填前的归属是 NULL（无主），撤不回"本来属于谁"'),
    PatchRecord('p004_pack_products', '按行业包补齐产品清单', 'data',
                'none', '补录后用户可能已改名/发生过交易，删除会连带毁数据'),
    PatchRecord('p006_cost_backfill', '存量流水回填成本快照（标 backfill）', 'data',
                'auto', '回填行带 backfill 标记，退回补丁前的 none 即精确撤'),
)

PATCH_IDS = tuple(p.pid for p in PATCHES)


def ledger_available():
    """账本表是否真在库里（"可查且为空"与"根本没法查"必须分开，否则空账本会被读成一致）。"""
    from sqlalchemy import inspect as _inspect

    from src.models import db as _db

    try:
        return bool(_inspect(_db.engine).has_table('schema_patch_log'))
    except Exception:      # noqa: BLE001  引擎不可用属"无法判定"，不是"没问题"
        return False


def logged_rows():
    """账本行 → {pid: {kind, mode, title, applied_at}}；查不到时返回空 dict。"""
    from src.models import SchemaPatchLog, db as _db

    if not ledger_available():
        return {}
    try:
        return {r.patch_id: {'kind': r.kind, 'mode': r.mode, 'title': r.title,
                             'applied_at': r.applied_at.strftime('%Y-%m-%dT%H:%M:%S')
                             if r.applied_at else ''}
                for r in SchemaPatchLog.query.all()}
    except Exception:      # noqa: BLE001  读失败要把会话回滚干净，别把脏状态留给请求后续
        _db.session.rollback()
        return {}


def status():
    """「这个库升到第几步」的一次性答案，供 `/healthz`、门禁与部署后校验共用同一实现。

    `orphans`（账本里有、声明里没有）与 `pending`（声明里有、账本里没有）必须**同时**存在：
    只报 pending 就看不见"补丁被改名/被删后留下的幽灵记录"，只报 orphans 就看不见没跑完。
    """
    rows = logged_rows()
    pending = [p for p in PATCH_IDS if p not in rows]
    return {'declared': len(PATCH_IDS), 'applied': len([p for p in PATCH_IDS if p in rows]),
            'pending': pending, 'logged': sorted(rows),
            'orphans': sorted(set(rows) - set(PATCH_IDS)),
            'ledger_table': ledger_available()}


# mode 三取值（写入方只有两处：init_db 的补丁运行器、全新库的整册补记）
MODES = ('applied', 'noop', 'adopted')


def record_pid(pid, title, kind, mode, detail=''):
    """记一条补丁账（merge 语义：同一 pid 重复记不炸，最后一次为准）。"""
    from src.models import SchemaPatchLog, db as _db

    if mode not in MODES:
        raise ValueError(f'非法账本 mode {mode!r}，取值 {MODES}')
    _db.session.merge(SchemaPatchLog(patch_id=pid, title=title, kind=kind,
                                     mode=mode, detail=str(detail)[:200]))
    _db.session.commit()


def record_all(mode='adopted', detail='全新库：create_all + 播种直接构造为最新结构'):
    """整册补记 —— 只给"结构天然最新"的全新库用。

    为什么必须有这条：CI 与冷启动解压走的是**播种分支**（`reset_demo_database`），
    那里 `create_all()` 一次建出的就是最新版 schema，压根不需要跑任何补丁。
    若不为它补记，门禁会把"刚建好的干净库"判成"4 个补丁都没跑"，
    于是每一次 CI 冷启动都红 —— 那是判据错了，不是库错了。
    """
    for rec in PATCHES:
        record_pid(rec.pid, rec.title, rec.kind, mode, detail)


def rev_problems(patches=None):
    """声明侧的自洽检查（**纯函数**：吃一份 PatchRecord 列表，吐违例清单，不落盘不连库）。

    拆成纯函数的原因：门禁要判的是"声明合不合法"，而合法性与"这台机器的库里有没有这张表"无关；
    若把两件事写在一个函数里，测试就只能靠造真库来打反例（第 34 轮 ②-b 那族坑的同一形状）。
    """
    recs = PATCHES if patches is None else patches
    problems = []
    for rec in recs:
        pid = getattr(rec, 'pid', '?')
        if getattr(rec, 'rev', None) not in REV_VALUES:
            problems.append(f'{pid}：rev={getattr(rec, "rev", None)!r} 不在值域 {REV_VALUES}')
        note = str(getattr(rec, 'rev_note', '') or '')
        if len(note) < REV_MIN_NOTE:
            problems.append(f'{pid}：rev_note 只有 {len(note)} 字（下限 {REV_MIN_NOTE}）'
                            '⇒ 写了等级没写代价，等于没声明')
    return problems


def unrecord_pid(pid):
    """把一条补丁从账本里摘掉 —— `record_pid` 的反向动作，回滚成功后必须调它。

    为什么是"删行"而不是"记一条 reverted"：账本语义是「这个库还欠着哪些补丁」，
    撤掉补丁 = 重新欠着 ⇒ 下一次 `--if-empty` 会再跑一遍（幂等闭环，也是回滚的正解验证）。
    加一列状态反而制造"两个真相源"（欠不欠 = applied 列 与 reverted 列 的差），
    且要开一条新补丁来给账本加列，本末倒置。
    返回被摘掉的行（dict，供调用方写进回滚清单）；账本里没有该 pid 时返回 None（不报错——
    "没跑过的补丁无需撤"是合法态，不是失败）。
    """
    from src.models import SchemaPatchLog, db as _db

    row = SchemaPatchLog.query.get(pid)
    if row is None:
        return None
    snap = {'patch_id': row.patch_id, 'title': row.title, 'kind': row.kind,
            'mode': row.mode, 'applied_at': row.applied_at.strftime('%Y-%m-%dT%H:%M:%S')
            if row.applied_at else ''}
    _db.session.delete(row)
    _db.session.commit()
    return snap
