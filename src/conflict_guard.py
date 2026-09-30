"""写路径的并发正确性 —— 「我改的是不是我看到的那一行」必须有凭据（第 31 轮 H95）。

为什么必须有它（2026-09-27 当场实测，`D:/tmp/bench32/measure2.py`）：
① 两个真实会话先后编辑同一产品：A 提交 `unit_price=11.00` 得 200，B 的表单在 A 之前加载、
   提交 `unit_price=12.00` 也得 200 ⇒ 终值 12.0，**A 的编辑无声消失**；归因链两次都记了，
   但"带冲突痕迹的归因行 = 0" —— 记了 diff 不等于知道自己在覆盖别人；
② 双 ORM 会话交错：A 写 99 → B（手里仍是旧值）写 77 ⇒ 终值 77，99 无声丢失；
③ `version_id_col|for_update|with_for_update|optimistic|冲突检测|已被他人` 在
   `src/*.py + src/routes/*.py + quality_gate.py` 共 **8 组 0 命中** —— 全仓对"丢失更新"零判据。

上游口径（同轮 `gh api` 取回原文逐行核过，缓存在 `D:/tmp/bench32/`）：
- Frappe `frappe/model/document.py:1387` `check_if_latest()` —— 保存前把**客户端带回来的
  `modified`** 与库内值比，`:1409` 抛
  `"Error: {name} ({doctype}) has been modified after you have opened it"`；
  该函数在 `:734/:844/:1813`（save / rename / delete）都被调用 ⇒ 乐观并发的凭据**随表单出、回来验**；
- Saleor `AGENTS.md:13-14`「Assume every operation runs concurrently with itself. Use atomic DB
  operations (`F()`, `update_or_create`, `select_for_update`) **instead of check-then-act**」、
  `:352` Avoid Check-Then-Act Race Conditions、`:376` Row-Level Locking；
  `saleor/app/lock_objects.py:6-7` `App.objects.order_by("pk").select_for_update(of=["self"])`；
- PostHog `posthog/api/services/llm_prompt.py:208/:302` 发布/复制带版本语义的编辑时用
  `select_for_update()`。

**为什么我方不照抄行锁**：SQLite/PythonAnywhere 免费层没有可用的行级锁
（`SELECT … FOR UPDATE` 在 SQLite 上是空操作），所以走 Frappe 那一侧 —— **凭据在表单里**，
服务端不保存任何跨请求状态 ⇒ 多 worker / 重启都不失效，也不会把"我看到的值"变成第二个真相源。

刻意不做的事：
- **不开"跳过冲突检查"的环境开关**（那等于给并发洞留后门）；缺凭据的请求按"放行 + 计数"处理，
  计数在 `/healthz` 与门禁回执可见，等误报率量清楚了再接线（与既有告警口径一致）；
- 不把行的**值**写进留痕（只写字段名与指纹前缀），与第 26/28 轮红线同口径。
"""

import hashlib
import json

__all__ = ['CONFLICT_TOKEN_LEN', 'FORM_FIELD', 'ConflictError', 'cas_flip', 'check',
           'claim_value', 'describe', 'fingerprint', 'note_cas_conflict',
           'note_integrity_reject', 'reset_stats', 'stats']

#: 随表单出发、回来再验的隐藏字段名（门禁第 45 项按这个名字核"两端都在"）。
#: 命名刻意不带 token/secret 字样 —— ruff S105 会把 `*_TOKEN = '字面量'` 判成硬编码口令，
#: 这里存的本来也只是表单字段名，不是凭据本身。
FORM_FIELD = '_occ'
CONFLICT_TOKEN_LEN = 16
#: 留痕里允许出现的形态：字段名 + 指纹前缀，绝不写业务值
_PREFIX = 8

_STATS = {'checked': 0, 'conflict_rejected': 0, 'missing_token': 0, 'missing_claim': 0,
          'cas_conflicts': 0, 'integrity_rejected': 0, 'applied': 0}


class ConflictError(ValueError):
    """这一行在我看到它之后被别人改过。带**字段名**（不带值），供端点回显与留痕。"""

    def __init__(self, table, changed_fields, obj=None):
        fields = list(changed_fields)
        super().__init__(f'{table} 记录已被他人修改（字段：{", ".join(fields) or "未知"}）')
        self.table = table
        self.changed_fields = fields
        self.obj_id = getattr(obj, 'id', None)
        self.store_id = getattr(obj, 'store_id', None)


def _roster(obj):
    """取该表要参与指纹的字段名册 —— 直接复用归因链的权威源，不另抄一份名单。"""
    from src import change_log

    table = obj.__tablename__
    fields = change_log._fields_for(table)
    if not fields:
        raise ValueError(f'{table} 不在归因链值档名册内（无法取指纹字段；判"没配好"而不是判"没冲突"）')
    return table, tuple(fields)


def fingerprint(obj, fields=None):
    """行指纹：对**名册字段**的归一化值做 sha256（值本身不出库、不落日志）。"""
    from src import change_log

    if fields is None:
        _table, fields = _roster(obj)
    snap = {name: change_log._jsonable(getattr(obj, name, None)) for name in fields}
    blob = json.dumps(snap, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:CONFLICT_TOKEN_LEN]


def check(obj, token):
    """校验表单带回来的指纹；不一致抛 `ConflictError`，缺凭据则计数后放行（不静默）。"""
    table, fields = _roster(obj)
    _STATS['checked'] += 1
    given = str(token or '').strip()
    if len(given) < CONFLICT_TOKEN_LEN:
        # 老页面/缓存/直接 API 调用：放行并计数。**不**在此开环境开关 —— 开关就是后门。
        _STATS['missing_token'] += 1
        return
    current = fingerprint(obj, fields)
    if given[:CONFLICT_TOKEN_LEN] == current:
        _STATS['applied'] += 1
        return
    _STATS['conflict_rejected'] += 1
    raise ConflictError(table, fields, obj)


def cas_flip(model, pk, column, expect, **scope):
    """比较并交换式翻转：`UPDATE … SET col=NOT :expect WHERE id=pk AND col=:expect`。

    **`expect` 必须是客户端看到的那个值**（表单/JS 带回来），不能在服务端重新读 ——
    自己刚读的值当期望，WHERE 恒真，等于把"读—改—写"三步原样保留，什么也没防住
    （本模块第一版就是那样写的，自检时才发现）。

    返回 `(状态, 新值)`，状态 ∈ {'applied','conflict','missing','no_claim'}：
    `conflict` = 这一行在我看到之后已被他人改成别的值 ⇒ 拒绝而不是覆盖。
    `no_claim` = 客户端没带期望值（老页面）⇒ 计数后放行，与指纹缺凭据同一口径，不静默。
    """
    from src.models import db

    if expect is None:
        _STATS['missing_claim'] += 1
        return 'no_claim', None
    expect = bool(expect)
    q = db.session.query(model).filter(model.id == pk)
    for col, val in scope.items():
        q = q.filter(getattr(model, col) == val)
    exists = db.session.get(model, pk) is not None
    got = int(q.filter(getattr(model, column) == expect)
              .update({column: not expect}, synchronize_session=False))
    db.session.commit()
    if got == 1:
        _STATS['applied'] += 1
        return 'applied', not expect
    _STATS['cas_conflicts'] += 1
    return ('conflict' if exists else 'missing'), None


def claim_value(raw):
    """客户端带回来的布尔期望值转 bool；空/缺 ⇒ None（没主张，另计一类，不静默）。"""
    text = str(raw if raw is not None else '').strip().lower()
    if not text:
        return None
    return text in ('1', 'true', 'on', 'yes')


def note_cas_conflict(entity, pk, field, store_id=None, merchant_id=None):
    """比较并交换撞车也走同一条留痕口（动作名共用 conflict_rejected，别造第二套词汇）。"""
    from src import change_log

    return change_log.note_denied(entity, pk, [field], merchant_id=merchant_id,
                                store_id=store_id)


def note_integrity_reject():
    """路由层计数：先查后插被唯一约束挡下 —— 既不是 500，也不算成功。"""
    _STATS['integrity_rejected'] += 1


def stats():
    """进程内读数（聚合整数，不含任何业务值）。"""
    return dict(_STATS)


def reset_stats():
    for k in _STATS:
        _STATS[k] = 0


def describe():
    """`/healthz` 的 conflict 段：只出聚合整数，与既有告警面同口径。"""
    s = stats()
    return {'checked': s['checked'], 'rejected': s['conflict_rejected'],
            'missing_token': s['missing_token'], 'cas_conflicts': s['cas_conflicts'],
           'integrity_rejected': s['integrity_rejected']}
