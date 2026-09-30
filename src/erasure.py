"""商家数据擦除（对标第 28 轮 H69）——「注销一个商家」的唯一入口与归属图。

动因（2026-09-27 当场实测，开发副本 `src/`）：
- `注销|delete_account|erase` 在 `src/` **0 命中**；`routes` 里 `db.session.delete` 只有 2 处
  且都是产品行 —— **停用有开关（`is_active`），删除没有出口**；而 `/register` 在授权名册里
  是匿名公开档 ⇒ 开放注册进来的商家永远删不掉。
- 更关键的是分母：ORM 元数据枚举 18 张表，**只有 8 张挂 `merchant_id`**；
  `daily_sales`（实测 2,100 行，全库最大的业务数据表）、`quiz_records`、`products`、
  `metric_defs` 只有 `store_id`/`staff_id`/`pack_id`。⇒ **按 `merchant_id` 列名直觉去擦会
  整表漏掉营业流水**，且没有任何判据会红（`grep merchant_id` 得 14 处，与元数据的 8 张也对不上
  —— 分母不能由 grep 得出）。
- `email_verifications` 只存 `email`，而 `admin` 表**没有 email 列**（注册时填的邮箱验证完
  就成了孤儿行）⇒ 它既不能由商家归属推出，也不该被悄悄漏掉，只能显式一档并如实报残留。

上游规格（同轮 `gh api` 取回原文，缓存在 `D:/tmp/bench29/`）：
- Saleor `saleor/graphql/account/mutations/account/account_delete.py:46/:54/:105` ——
  删除是一条**声明了 webhook_events_info 的事件**（`cls.call_event(manager.account_deleted)`）
  并带 `clean_instance` 前置校验位，不是"跑一条 DELETE 就算删了"。
- PostHog `posthog/tasks/delete_persons.py:23/:31` —— 删除是**有界分块作业**：
  "Small chunks keep a retry cheap: only the chunk holding a failing person is re-run"，
  失败只重试失败者，禁止整块重跑。
- Frappe develop `frappe/core/doctype/user/user.py:381/:384/:386/:265` —— `check_enable_disable`
  守「不可停用的最小集合」，停用同时清会话。我方实测停用即吊销已由 Flask-Login 的 `is_active`
  达成（停用后同一会话 302 → `/login?next=`）⇒ 那一半不是待补功能，是**待钉住的判据**（L4 S34）。

三个不变量：
1. **归属图是唯一事实**：每张表显式登记"怎么算属于本商家"，漏登记 ⇒ 门禁第 42 项判红；
2. **不可逆动作必须有可用备份凭证**（复用第 25 轮备份证据链的 `verify_backup`；
   0 表 0 行的撕裂备份会被它拒掉 —— 那正是"备份成功"三个字的空洞形态）；
3. **擦完必须复扫自证零残留**并返回逐表读数："删了"与"证明删干净了"是两件事。
"""
from __future__ import annotations

import json
import os

MODE_MERCHANT = 'merchant_id'            # 表自带 merchant_id 列
MODE_STORE = 'store_id'                  # 经本商家门店连坐
MODE_STORE_OR_STAFF = 'store_or_staff'   # 门店或店员任一命中即属本商家
MODE_ROOT = 'root'                       # merchants 行本身
MODE_EMAIL = 'email_list'                # 只认注销请求显式给出的注册邮箱
MODE_NONE = 'declared_none'              # 不属任何商家：必须逐条写明为什么

MODES = (MODE_MERCHANT, MODE_STORE, MODE_STORE_OR_STAFF, MODE_ROOT, MODE_EMAIL, MODE_NONE)

#: 表名 -> 归属档。**键集合必须等于 db.metadata 的实表集合**（门禁第 42 项按此对账）。
#: 顺序不承载语义，删除顺序见 ERASE_ORDER。
OWNING = {
    'products': {'mode': MODE_STORE},
    'metric_defs': {'mode': MODE_STORE},
    'daily_sales': {'mode': MODE_STORE_OR_STAFF},
    'quiz_records': {'mode': MODE_STORE_OR_STAFF},
    'admin_store_access': {'mode': MODE_STORE},
    'ai_usage_ledger': {'mode': MODE_MERCHANT},
    'api_tokens': {'mode': MODE_MERCHANT},
    'webhook_subscriptions': {'mode': MODE_MERCHANT},
    'webhook_delivery': {'mode': MODE_MERCHANT},
    'store_tasks': {'mode': MODE_MERCHANT},
    'staff': {'mode': MODE_MERCHANT},
    'admin': {'mode': MODE_MERCHANT},
    'change_log': {'mode': MODE_MERCHANT},
    'stores': {'mode': MODE_MERCHANT},
    'email_verifications': {'mode': MODE_EMAIL, 'reason':
        '只存 email，而 admin 表无 email 列 ⇒ 无法由商家归属推出；注销时显式给出注册邮箱才清，'
        '否则如实报残留，禁静默漏'},
    'industry_packs': {'mode': MODE_NONE, 'reason':
        'pack_id 是行业包标识不是租户键，存的是全平台预置行业内容；商家自建包走 DIY 草稿面不落此表'},
    'pack_items': {'mode': MODE_NONE, 'reason': '随 industry_packs 的包内容项，同为全平台共享'},
    'schema_patch_log': {'mode': MODE_NONE, 'reason':
        '迁移账本是全局结构事实，删它会毁掉升级幂等判据（第 17/26 轮 p00x 链靠它）'},
    'merchants': {'mode': MODE_ROOT, 'reason':
        '商家根行：连坐表全部指向它，必须最后删；提前删会留下无 merchant 可寻的孤儿流水'},
}

#: 先叶子后根：门店/流水先走，merchants 最后走。中途失败时不会出现
#: "商家行没了、流水还在"的无主数据（那种数据比不删更糟）。
ERASE_ORDER = ('products', 'metric_defs', 'admin_store_access', 'daily_sales', 'quiz_records',
               'ai_usage_ledger', 'api_tokens', 'webhook_delivery', 'webhook_subscriptions',
               'store_tasks', 'email_verifications', 'staff', 'admin', 'change_log', 'stores',
               'merchants')

BATCH_ROWS = 500        # 有界分块（PostHog 口径：失败只重试失败者）
REASON_FLOOR = 20       # 「为什么不属本商家」写成一句话才算理由
MAX_LOOPS = 400         # 单表分块硬上限：绝不因异常条件把进程挂在死循环上
NEEDS_COLUMN = {MODE_MERCHANT: 'merchant_id', MODE_STORE: 'store_id', MODE_EMAIL: 'email'}
#: 不可擦最小集合（Frappe `check_enable_disable` 同族）：平台租户与演示根行永不擦。
FORBIDDEN_NAMES = ('platform_demo', 'platform')
#: 双重确认短语（与演示重置同构）：必须逐字键入，防"手滑点到"把整个商家抹掉。
ERASE_CONFIRM_PHRASE = 'ERASE-MERCHANT-AND-ALL-DATA-FOREVER'


def orm_models():
    """表名 -> ORM 类（枚举器唯一一份，门禁与运行时同一把尺）。"""
    from src import models as m

    out = {}
    for name in dir(m):
        obj = getattr(m, name)
        tab = getattr(obj, '__tablename__', None)
        if isinstance(tab, str):
            out[tab] = obj
    return out


def metadata_columns(name):
    from src.models import db

    t = db.metadata.tables.get(name)
    return set() if t is None else {c.name for c in t.columns}


def scope_of(merchant_id):
    """这个商家有哪些门店/店员/账号 —— 连坐档的行筛选全靠它。"""
    from src.models import Admin, Staff, Store, db

    def ids(model):
        return [r.id for r in db.session.query(model).filter_by(merchant_id=merchant_id).all()]

    return {'merchant_id': merchant_id, 'stores': ids(Store), 'staff': ids(Staff),
            'admins': ids(Admin)}


def _email_clause(model, emails):
    """email 档：按 lower(email) 匹配。存量行是注册时按输入原样落的（实测有
    'S34-KEEP@Example.COM' 这种混合大小写），SQLite 的 IN 区分大小写，
    用原值匹配会"看起来删了、其实一行没动"。"""
    low = [str(e).strip().lower() for e in (emails or ()) if e]
    if not low:
        return None
    from sqlalchemy import func

    return [func.lower(model.email).in_(low)]


def _clause(model, mode, sc, emails):
    """按归属档造筛选条件。**一律返回条件列表或 None**，绝不回裸表达式。

    为什么钉这一条（本轮一手实踩）：首版 merchant/root 两档直接 `return model.x == id`，
    而消费侧写的是 `if not conds: 跳过` —— SQLAlchemy 的 `BinaryExpression.__bool__`
    对 `col == 值` 返回 False，于是"有归属依据"被静默读成"本轮无归属依据，跳过"，
    **商家根行与全部 merchant_id 档永远删不掉**（连坐档因返回 list 而正常，所以只有它绿）。
    与第 27 轮 `for x in <dict>` 取到键、②-b 反例没进谓词同族：**形状不对，判据就恒假。**
    消费侧一律 `is None` 判缺依据，不用真值判断。
    """
    if mode == MODE_MERCHANT:
        return [model.merchant_id == sc['merchant_id']]
    if mode == MODE_ROOT:
        return [model.id == sc['merchant_id']]
    if mode == MODE_STORE:
        return [model.store_id.in_(sc['stores'])] if sc['stores'] else None
    if mode == MODE_STORE_OR_STAFF:
        conds = []
        if sc['stores']:
            conds.append(model.store_id.in_(sc['stores']))
        if sc['staff']:
            conds.append(model.staff_id.in_(sc['staff']))
        return conds or None
    if mode == MODE_EMAIL:
        return _email_clause(model, emails)
    return None


def _count(models, name, spec, sc, emails):
    """该表按归属档命中多少行；-1 = ORM 类找不到（对账层面是缺陷，不是 0）。"""
    from sqlalchemy import or_

    from src.models import db

    model = models.get(name)
    if model is None:
        return -1
    conds = _clause(model, spec['mode'], sc, emails)
    if conds is None:
        return 0
    clause = or_(*conds) if len(conds) > 1 else conds[0]
    return db.session.query(model).filter(clause).count()


def plan(merchant_id, emails=()):
    """只读预览：逐表**将要**删多少行。双重确认的第一屏靠它，不靠人记。"""
    from src.models import Merchant, db

    models = orm_models()
    sc = scope_of(int(merchant_id))
    per = {n: _count(models, n, s, sc, emails) for n, s in OWNING.items()
           if s['mode'] != MODE_NONE}
    root = db.session.get(Merchant, int(merchant_id))
    return {'merchant_id': int(merchant_id), 'merchant_name': getattr(root, 'name', ''),
            'is_active': getattr(root, 'is_active', None),
            'scope': {'stores': len(sc['stores']), 'staff': len(sc['staff']),
                      'admins': len(sc['admins'])},
            'per_table': per, 'total_rows': sum(v for v in per.values() if v > 0),
            'unscoped': sorted(n for n, s in OWNING.items() if s['mode'] == MODE_NONE)}


def _erase_one(name, ctx):
    """单表擦除：有界分块 + 每块一条聚合留痕（不留业务值，第 26 轮红线同口径）。"""
    from sqlalchemy import or_

    from src import change_log
    from src.models import db

    model = ctx['models'].get(name)
    stats = ctx['stats']
    if model is None:
        stats[name] = {'deleted': 0, 'error': 'ORM 类缺失'}
        return
    conds = _clause(model, OWNING[name]['mode'], ctx['scope'], ctx['emails'])
    if conds is None:
        stats[name] = {'deleted': 0, 'note': '本轮无归属依据，跳过'}
        return
    clause = or_(*conds) if len(conds) > 1 else conds[0]
    pk = getattr(model, model.__mapper__.primary_key[0].name)
    moved = 0
    batch = 0
    for _ in range(MAX_LOOPS):
        ids = [r[0] for r in db.session.query(pk).filter(clause).limit(BATCH_ROWS).all()]
        if not ids:
            break
        got = db.session.query(model).filter(pk.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        # 留痕必须落在**被擦范围之外**：`change_log` 自身在归属图里按 merchant_id 归档，
        # 若按被擦商家记账，擦除动作会把自己的证据一起扫掉（L4 首跑实测 0 条），
        # 「谁在什么时候擦了这个商家」在最需要它的那一行上恰恰无从回答。
        # PostHog 把这点写进结构里：`PersonTombstone` 独立成表，`LOG_ACTIVITY` 归入
        # STEPS_AFTER_DELETION（bulk_deletions.py:53-64），删完的人不可能再被 resolve，
        # 所以证据绝不能随人被删。⇒ 聚合行记 merchant_id=NULL（平台侧档，只有平台管理员可见），
        # 目标商家只作为 detail 里的一个 id 出现（不含任何业务值，第 26 轮红线同口径）。
        batch += 1
        change_log.note_bulk(name, 'bulk_delete', got, merchant_id=None,
                             detail=f'erase merchant={ctx["merchant_id"]} batch={batch} '
                                    f'ids={ids[0]}..{ids[-1]}')
        moved += got
        if got == 0:
            stats[name] = {'deleted': moved, 'error': '删 0 行仍查到残留 ⇒ 停止，交人工'}
            return
        if len(ids) < BATCH_ROWS:
            break
    stats[name] = {'deleted': moved}


def backup_proof(path):
    """把"这份备份到底能不能用"算成可复算凭证（第 25 轮口径，全部走包内原语）。

    为什么不用"文件存在 + 复制没报错"当凭证：`integrity_check` 对 0 表 0 行的空库照样返回
    ok —— 本机实测那份 4,096 B 的备份就是这样被判「✅ 可恢复」的（`backup_manifest` 文件头
    原话）。所以三道都要过：文件在 ∧ integrity ok ∧ 非空（`empty_guard`），并回抄 sha256 与
    逐表行数，让人能在屏上核到自己那个商家还在不在。
    """
    from src import backup_manifest as BM

    p = str(path)
    if not os.path.isfile(p):
        return {'ok': False, 'path': p, 'detail': '备份文件不存在'}
    ok, why = BM.integrity_ok(p)
    counts = BM.table_counts(p)
    non_empty, empty_why = BM.empty_guard(counts)
    if not ok:
        return {'ok': False, 'path': p, 'detail': f'integrity_check 未过：{why}'}
    if not non_empty:
        return {'ok': False, 'path': p, 'detail': f'空库备份，拒绝当凭证：{empty_why}'}
    has_manifest, _err = BM.read_manifest(p)
    card = '在' if has_manifest else '缺（该备份未经 backup_db 盖章，只过字节层，未经第 25 轮证据层）'
    return {'ok': True, 'path': p, 'sha256': BM.sha256_file(p)[:16],
            'tables': len(counts), 'rows': sum(counts.values()),
            'evidence_card': bool(has_manifest),
            'detail': f'integrity ok，{len(counts)} 表 / {sum(counts.values())} 行；证据件{card}'}


def _protected_ids():
    """受保护商家 id：来自配置，而非"演示库里 1 号是平台"这种巧合。

    首版写成 `FORBIDDEN_MERCHANT_IDS = (1,)`，在全新临时库里第一个商家就是 id=1
    ⇒ 普通商家被误判成"平台根行"拒绝擦除（本轮独立复现抓到）。id 是播种顺序的产物，
    不能当身份用。
    """
    from flask import current_app

    raw = ''
    try:
        raw = str(current_app.config.get('ERASURE_PROTECTED_MERCHANT_IDS') or '')
    except RuntimeError:      # 无应用上下文（脚本/离线）时不猜测，交角色判据兜底
        raw = ''
    return {int(x) for x in raw.split(',') if x.strip().isdigit()}


def _has_platform_admin(merchant_id):
    from src.models import ROLE_PLATFORM, Admin, db

    return db.session.query(Admin).filter_by(merchant_id=int(merchant_id),
                                             role=ROLE_PLATFORM).count() > 0


def _guard_root(root, merchant_id):
    """不可擦最小集合：命中即抛，绝不"静默什么都不做"（那会被读成删除成功）。

    三条判据都取**数据事实**：有平台管理员在册 / 配置显式保护 / 商家名在禁删名册。
    """
    if root is None:
        raise ValueError('商家不存在，拒绝擦除（不创建、不猜测）')
    if (_has_platform_admin(merchant_id) or int(merchant_id) in _protected_ids()
            or getattr(root, 'name', '') in FORBIDDEN_NAMES):
        raise ValueError(f'商家 {merchant_id} 属不可擦最小集合（平台在册/配置保护/禁删名），拒绝擦除')


def execute(merchant_id, backup_path, emails=()):
    """擦除的唯一执行入口。**没有可用备份凭证就不动一行盘**。

    `backup_path` 走第 25 轮 `backup_db.verify_backup` 复验：字节层（integrity_check）+
    证据层（逐表行数）。0 表 0 行的撕裂备份会被拒 —— 那正是"备份成功"三个字的空洞形态。
    """
    from src.models import Merchant, db

    root = db.session.get(Merchant, int(merchant_id))
    _guard_root(root, merchant_id)
    proof = backup_proof(backup_path)
    if not proof['ok']:
        raise ValueError('备份凭证不可用，拒绝擦除：' + proof['detail'])
    stats = {'backup': proof, 'tables': {}, 'residual': {}}
    from src import query_guard

    ctx = {'models': orm_models(), 'scope': scope_of(int(merchant_id)), 'emails': emails,
           'merchant_id': int(merchant_id), 'stats': stats['tables']}
    # 跨租户删除必须显式逃逸行级守卫：不逃逸时 `db.session.query(Staff)` 会被附加
    # `merchant_id = <当前会话商家>`，于是"删了 0 行"与"这个商家本来就没数据"同形
    # （L4 首跑实测正是这样：连坐表删掉了、staff 与 merchants 一行没动）。
    # 刻意不套 change_log.silenced() —— 擦除的留痕就是它存在的证据。
    with query_guard.unscoped():
        for name in ERASE_ORDER:
            if name not in OWNING:
                stats['tables'][name] = {'deleted': 0, 'error': '不在归属图（门禁第 42 项会红）'}
                continue
            _erase_one(name, ctx)
        stats['residual'] = verify(int(merchant_id), emails=emails)
    return stats


def verify(merchant_id, emails=()):
    """复扫：逐表**残留**行数。只回非零项 —— 空 dict 才叫擦干净，且不合并成布尔。"""
    models = orm_models()
    sc = scope_of(int(merchant_id))
    out = {}
    for name, spec in OWNING.items():
        if spec['mode'] == MODE_NONE:
            continue
        if spec['mode'] == MODE_EMAIL and not [e for e in (emails or ()) if e]:
            # 「没给邮箱」不等于「没有残留」：这是未判定，必须独立成状态而不是被读成 0
            out[name] = 'undetermined:未提供注册邮箱，无法按 email 匹配'
            continue
        n = _count(models, name, spec, sc, emails)
        if n:
            out[name] = n
    return out


def _reason_problems(name, spec):
    if len(str(spec.get('reason') or '').strip()) < REASON_FLOOR:
        return [f'{name} 必须写 ≥{REASON_FLOOR} 字的理由（不可擦/无主是要留痕的决策）']
    return []


def _column_problems(name, spec):
    mode, cols = spec['mode'], metadata_columns(name)
    problems = []
    need = NEEDS_COLUMN.get(mode)
    if need and cols and need not in cols:
        problems.append(f'{name} 声明为 {mode} 却没有 {need} 列（谓词会抛或恒假）')
    if mode == MODE_STORE_OR_STAFF and cols and not {'store_id', 'staff_id'} & cols:
        problems.append(f'{name} 声明为 store_or_staff 但两个归属列都没有')
    return problems


def _entry_problems(name, spec):
    """单表声明自证：档位合法 → 归属列 → 理由地板。"""
    mode = spec.get('mode')
    if mode not in MODES:
        return [f'{name} 归属档非法：{mode!r}']
    problems = _column_problems(name, spec)
    if mode in (MODE_NONE, MODE_ROOT, MODE_EMAIL):
        problems += _reason_problems(name, spec)
    return problems


def validate_registry():
    """归属图 ⇄ ORM 元数据 双向对账。返回 (problems, evidence)。

    会红的形态：漏登记实表、登记不存在的表、归属档写错、直取档缺 `merchant_id` 列、
    连坐档缺归属列、email 档缺 `email` 列、ROOT/NONE/EMAIL 档缺理由或理由过短、
    删除顺序漏表、根行不在最后、归属图为空。
    """
    from src.models import db

    problems = []
    actual = {t.name for t in db.metadata.sorted_tables}
    if not OWNING:
        return ['归属图为空（零登记一律判红，不得读成「没有要擦的表」）'], []
    miss = sorted(actual - set(OWNING))
    ghost = sorted(set(OWNING) - actual)
    if miss:
        problems.append(f'这些实表不在归属图（擦除会静默漏表）：{miss[:8]}')
    if ghost:
        problems.append(f'归属图登记了不存在的表（陈旧声明）：{ghost[:8]}')
    order_miss = sorted(set(OWNING) - set(ERASE_ORDER)
                        - {n for n, s in OWNING.items() if s['mode'] == MODE_NONE})
    if order_miss:
        problems.append(f'可擦档的表不在删除顺序里：{order_miss[:8]}')
    for name, spec in sorted(OWNING.items()):
        problems += _entry_problems(name, spec)
    roots = [n for n, s in OWNING.items() if s['mode'] == MODE_ROOT]
    if len(roots) != 1:
        problems.append(f'根行档必须恰好一张表，现在 {len(roots)} 张：{roots}')
    elif 'id' not in metadata_columns(roots[0]):
        problems.append(f'根行档 {roots[0]} 没有 id 列，谓词无法构造')
    if roots and ERASE_ORDER and ERASE_ORDER[-1] not in roots:
        problems.append(f'删除顺序最后一步必须是根行（现在最后是 {ERASE_ORDER[-1]}）')

    def _n(mode):
        return sum(1 for v in OWNING.values() if v['mode'] == mode)

    evidence = [f'实表 {len(actual)} 张，归属图 {len(OWNING)} 条（直取 {_n(MODE_MERCHANT)} / '
                f'连坐 {_n(MODE_STORE) + _n(MODE_STORE_OR_STAFF)} / 邮箱 {_n(MODE_EMAIL)} / '
                f'无主 {_n(MODE_NONE)}）',
                f'删除顺序 {len(ERASE_ORDER)} 步，最后一步 {ERASE_ORDER[-1]}']
    return problems, evidence


def main(argv=None):
    """离线出口：`python -m src.erasure [--plan <id>|--verify <id>]`。

    **这里没有 `--execute`** —— 不可逆动作只允许走带双重确认与备份凭证的受控入口；
    CLI 只读，否则等于给批量误删开一条无门槛旁路。
    """
    import sys

    from src.app import create_app

    argv = list(sys.argv[1:] if argv is None else argv)
    app = create_app('development')
    problems, evidence = validate_registry()
    with app.app_context():
        for line in evidence:
            print('  ·', line)
        for flag in ('--plan', '--verify'):
            if flag in argv and len(argv) > argv.index(flag) + 1:
                mid = int(argv[argv.index(flag) + 1])
                fn = plan if flag == '--plan' else verify
                print(json.dumps(fn(mid), ensure_ascii=False, indent=1, default=str))
    if problems:
        print(f'[GATE:erasure-fail] 对账问题 {len(problems)} 项：')
        for p in problems:
            print('  ✗', p)
        return 1
    print('[GATE:erasure-pass] 归属图 ⇄ ORM 元数据双向一致')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
