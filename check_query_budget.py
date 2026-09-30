#!/usr/bin/env python
"""查询预算守卫：热路径 SQL 语句数的整数上界断言（只读、机器速度无关）。

为什么守语句数、不守毫秒（第 11 轮对标结论）
------------------------------------------------
业界拿来做「红」的判据不是 wall-clock：

- **Apache Superset** `tests/integration_tests/deletion_retention/purge_performance_tests.py`
  自称 "Deterministic query-count guard"：`CHART_PURGE_BASELINE_STATEMENTS=35` 这类
  **整数基线** + `MAX_STATEMENT_REGRESSION=0.10` ⇒ `statement_budget = ceil(base*1.10)`，
  断言形式是 `assert statement_count <= statement_budget(...)`；
  同文件里中位耗时**只 print 相对 delta，不断言毫秒绝对值**。
- **github-action-benchmark** 的 bundle-size 类守卫默认 `fail-on-alert: false`（超阈值只告警），
  且基线由**独立的 nightly 工作流**刷新，不是每次 PR 重建。
- **Gitea / Matomo**：CI 里根本没有性能回归 job（Gitea 只有手工 `go test -bench` 的微基准文件）。

本项目为什么更该走这条路：单进程 + 1 个 SQLite + 2100 行固定演示数据 + 零流量 +
免费层共享 CPU。**毫秒在这里既无 SLA 含义、又是全场最容易 flaky 的量**，
把它写进红线只会训练人去看一眼就点重试。
⇒ 结论：**整数、确定、与 CPU 快慢无关**的语句数进红线；毫秒进报告不进红线（本脚本照此分工）。

防的是什么事故
--------------
N+1 复活。本仓 P7-E4「性能收口」轮做过的事就是把这些路径批量化
（购物篮 groupby 化、包列表消除 meta N+1、知识库/IDF 单次加载）。
**但没有任何东西阻止下一次改动把它们改回去** —— 那轮留下的 `controlled_benchmark.py`
至今 `grep benchmark quality_gate.py .github` **零命中**，即"有测量、无守卫"
（与第 9 轮 `ai_eval.py` 的失守形态完全相同，这是同一个病的第三次发作）。

预算怎么来的（不是拍脑袋）
--------------------------
每条预算 = `ceil(实测中位数 × 1.10)`，实测值与样本数写在本文件 `BASELINES` 里，
且本脚本带 `--rebaseline` 重新打印建议预算。预算表与实现同文件，改代码的人当场看得见。

用法
----
    python check_query_budget.py                # 门禁调用形态；退出码 0/1
    python check_query_budget.py --json         # 机器可读
    python check_query_budget.py --rebaseline   # 只测量并打印建议预算，不断言
退出码：0 全部在预算内 / 1 有路径超预算 / 2 无法测量（播种失败等）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from datetime import date
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault('ICAN_ALLOW_WEAK_SECRET', '1')
os.environ.setdefault('FLASK_ENV', 'development')

# 与 Superset 的 MAX_STATEMENT_REGRESSION 同值：给 10% 容差吸收无关小改动，
# 但 N+1（几十倍）一定越界。
REGRESSION_RATIO = 1.10
RUNS = 5            # 每条路径重复次数；语句数应恒定，取众数并可检测非确定性
WARMUP = 1

# 路径注册表：名称 → (说明, 在 app context 内可调用的函数, 实测基线, 预算)
# 基线为 2026-09-25 建表、2026-09-27 第 26 轮按 `--rebaseline` 实测重标 single_insert。
BASELINES = {
    # (说明, 2026-09-25 实测语句数, 预算 = ceil(实测 x 1.10))
    'dashboard_stats':   ('管理员看板聚合（P7-E4 收口对象）', 2, 3),
    'store_metrics':     ('跨店指标一次批量取（防每店一查）', 3, 4),
    'multi_page':        ('多店经营大脑整页四函数', 5, 6),
    'knowledge_search':  ('RAG 检索必须零 SQL（纯文件语料+IDF 缓存）', 0, 0),
    'learner_profile':   ('答题记录到掌握度画像（routes/ai.py 真实调用点）', 1, 2),
    'decision_snapshot': ('驾驶舱四角色共享证据快照', 4, 5),
    # M28 写路径与 AI 链路。选 50 行而非 5 行：这条判据的价值就在**规模不变量**上，
    # 谁把批量改回逐行，语句数随行数线性涨，预算立刻兜不住。
    'import_commit_50': ('批量导入 50 行落库（幂等重跑）', 5, 6),
    # 第 26 轮 H59 归因链：一条流水提交多发 **1 条** change_log INSERT（3 → 4）。
    # 门店归属解析已加进程级缓存，否则每写一行还多一条 SELECT（实测顶到 5）。
    # 预算按 ceil(4×1.10)=5 重标，来源当场 --rebaseline 实测，不是抬阈值求绿。
    'single_insert':    ('单条填报落库（H5 提交核心）', 4, 5),
    'decision_ai_path': ('驾驶舱四角色全链路（AI 关闭走规则版）', 6, 7),
}


def _install_counter():
    """挂 SQL 语句计数器。返回 (计数读取器, 清零器)。

    复用 SQLAlchemy `before_cursor_execute`，与 src/performance.py 的慢查询探针同一挂点；
    这里刻意自建而不是 import performance —— 守卫不能依赖监控模块是否被启用
    （performance.py 的端点默认关闭，若复用它会让预算检查随配置开关漂移）。
    """
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    state = {'n': 0}

    @event.listens_for(Engine, 'before_cursor_execute')
    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: E301,PLR0913,PLR0917
        state['n'] += 1

    return (lambda: state['n'], lambda: state.update(n=0))


def _seed_app():
    """播种一份**确定性**语料后返回 app。

    为什么不用开发库 data/ican.db：它会被人手工改演示数据，预算会随无关改动漂红；
    语料必须是固定基准（pack_demo 固定基准日期播种），这样"语句数变了"才只可能
    意味着代码变了 —— 这正是这条判据的全部价值所在。
    """
    from test_fixtures import use_temp_db

    use_temp_db()
    from src.app import create_app
    from src import pack_demo

    app = create_app('development')
    with app.app_context():
        pack_demo.reset_demo_database(days=pack_demo.DEMO_DAYS)
    return app


def build_paths(app):
    """构造路径注册表：{名称: callable}，每条都是**路由里真实存在的那条读库路径**。

    写这个表时踩到并已纠正的坑（留在此处，因为它是这类守卫最常见的死法）：
    第一版把 `initial_decision(store_ids[0], …)` 当探针，实测 **0 语句 / 0.01 ms**——
    因为第一个位置参数是 `merchant_id` 而不是 store_id，租户解析成空 → 什么都没查，
    守卫却"全绿"。同理 `learner_profile` 用了 `hasattr` 兜底，函数不存在时退化成
    纯计算探针。**"跑通"不等于"跑到"，每条路径都必须用语句数 > 0 自证**
    （见 main() 里的 _SELF_ASSERT 非零下限）。
    """
    from src import analytics, decision_cockpit, knowledge_base, learner_model, multi_store
    from src.models import QuizRecord, Staff, Store

    with app.app_context():
        from src.models import db
        store_ids = [s.id for s in Store.query.order_by(Store.id).limit(7).all()]
        staff_id = Staff.query.order_by(Staff.id).limit(1)[0].id
        merchant_id = Store.query.order_by(Store.id).limit(1).first().merchant_id
    _uniq = {'i': 0}
    if len(store_ids) < 2:
        raise RuntimeError('演示语料未就位：门店数 < 2')

    def dashboard():
        with app.app_context():
            analytics.compute_dashboard_stats(days=30)

    def metrics():
        with app.app_context():
            multi_store.store_metrics(store_ids, days=30)

    def multipage():
        with app.app_context():
            m = multi_store.store_metrics(store_ids, days=30)
            multi_store.store_ranking(store_ids, days=30, metrics=m)
            multi_store.cross_store_trend(store_ids, days=30)
            multi_store.abnormal_stores(store_ids, days=30, metrics=m)

    def kb():
        """检索路径的不变量是**零 SQL**：语料与 IDF 只从文件加载一次并缓存。
        预算 0 是最强形态——将来谁在 search() 里加一句 DB 读就红。"""
        with app.app_context():
            knowledge_base.search('芒果', top_k=3)

    def profile():
        with app.app_context():
            recs = QuizRecord.query.filter_by(staff_id=staff_id).all()
            learner_model.mastery_profile(recs)
            learner_model.review_schedule(recs)

    def snapshot():
        with app.app_context():
            decision_cockpit.build_snapshot(store_ids, today=None)

    # ---- M28 写路径与 AI 链路：注册在读取类之后，因为它们会改动语料 ----
    def _csv(rows):
        head = '日期,产品,销量,营收' + chr(10)
        body = ''.join('2026-08-%02d,导入产品%d,%d,%d.0' % (d, d, d, d * 10) + chr(10)
                       for d in rows)
        return head.encode('utf-8') + body.encode('utf-8')

    def import_rows():
        with app.app_context():
            from src.data_import import build_report
            from src.models import Admin, Store as _S

            adm = Admin.query.order_by(Admin.id).limit(1).first()
            st = _S.query.order_by(_S.id).limit(1).first()
            build_report(adm, st, _csv(range(1, 51)), adm.id, commit=True)

    def single_insert():
        with app.app_context():
            from src.models import DailySales, Staff as _St, Store as _S, db

            st = _S.query.order_by(_S.id).limit(1).first()
            sf = _St.query.order_by(_St.id).limit(1).first()
            row = DailySales(staff_id=sf.id, store_id=st.id, record_date=date(2026, 8, 26),
                             product_name='预算探针产品' + str(_uniq['i']),
                             product_sales_qty=1, total_revenue=10.0, pack_id='milk_tea')
            _uniq['i'] += 1
            db.session.add(row)
            db.session.commit()

    def decision_ai():
        with app.app_context():
            from src import decision_cockpit

            # 每轮先清缓存：不清则第 2..N 次全走 10 分钟缓存，测到的是"命中有多便宜"，
            # 而不是本条要守的不变量 ——四角色共享一次快照（PH5 红线）。
            decision_cockpit.ai_quota.clear_cache()
            decision_cockpit.run_decision(merchant_id, store_ids, allow_ai=False,
                                          write_trace=False, refresh=True)

    return {
        'dashboard_stats': dashboard,
        'store_metrics': metrics,
        'multi_page': multipage,
        'knowledge_search': kb,
        'learner_profile': profile,
        'decision_snapshot': snapshot,
        'import_commit_50': import_rows,
        'single_insert': single_insert,
        'decision_ai_path': decision_ai,
    }


# 自证下限：这些路径**必须**产生 SQL，否则探针是空的（knowledge_search 设计上应恒为 0，
# 它的不变量方向相反，故不列在这里）。
_SELF_ASSERT = {'dashboard_stats': 1, 'store_metrics': 1, 'multi_page': 2,
                'learner_profile': 1, 'decision_snapshot': 1,
                'import_commit_50': 3, 'single_insert': 2, 'decision_ai_path': 3}


def measure(fn, reader, resetter):
    """重复测量一条路径，返回 (语句数众数, 毫秒中位数, 是否非确定)。"""
    counts, ms_list = [], []
    for i in range(WARMUP + RUNS):
        resetter()
        t0 = time.perf_counter()
        fn()
        ms_list.append((time.perf_counter() - t0) * 1000)
        counts.append(reader())
    core = counts[WARMUP:]
    mode = statistics.mode(core)
    nondeterministic = len(set(core)) > 1
    return mode, statistics.median(ms_list), nondeterministic


def main(argv=None):
    ap = argparse.ArgumentParser(description='热路径 SQL 语句数预算守卫（只读）')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--rebaseline', action='store_true',
                    help='只测量并打印建议预算，不做断言')
    args = ap.parse_args(argv)

    app = _seed_app()
    reader, resetter = _install_counter()
    paths = build_paths(app)

    rows, bad, flaky, empty, stale = [], [], [], [], []
    for name, fn in paths.items():
        label, base, budget = BASELINES[name]
        n, ms, nondet = measure(fn, reader, resetter)
        suggested = math.ceil(n * REGRESSION_RATIO)
        rows.append({'name': name, 'label': label, 'measured': n, 'baseline': base,
                     'budget': budget, 'suggested_budget': suggested,
                     'ms_median': round(ms, 2), 'nondeterministic': nondet})
        if nondet:
            flaky.append(name)
        floor = _SELF_ASSERT.get(name, 0)
        if n < floor:
            empty.append((name, n, floor))
        if not args.rebaseline and n > budget:
            bad.append(name)

    if args.json:
        print(json.dumps({'ok': not bad and not flaky and not empty, 'rows': rows,
                          'over_budget': bad, 'nondeterministic': flaky,
                          'empty_probe': [e[0] for e in empty]},
                         ensure_ascii=False, indent=2))
        return 1 if (bad or flaky or empty) else 0

    def say(line):
        print('↳ ' + line)

    say(f'语料：固定基准演示库（{WARMUP} 次预热 + {RUNS} 次测量，取语句数众数）'
        f'｜容差 {int((REGRESSION_RATIO - 1) * 100)}%')
    for r in rows:
        flag = ('❌' if r['name'] in bad
                else '❌空探针' if r['name'] in [e[0] for e in empty]
                else ('⚠️非确定' if r['name'] in flaky else '✅'))
        # 声明基线只用于"预算怎么来的"可审计；它自己也会过期。
        # L4 那条算术断言只校验 budget==ceil(baseline*1.10)，**看不见 baseline 与实际脱节**，
        # 故此处实测值与声明值不等就点名（变多变少都报：变少说明有人优化了却没重标）。
        if r['measured'] != r['baseline']:
            stale.append((r['name'], r['baseline'], r['measured']))
        say(f"{flag} {r['name']:20s} 语句 {r['measured']:3d} / 预算 {r['budget']:3d} "
            f"（历史基线 {r['baseline']:3d}）｜{r['label']}｜ms(仅参考)={r['ms_median']}")
        if r['name'] in bad:
            say(f"   ↳ 超预算：{r['measured']} > {r['budget']}；"
                f"新增 {r['measured'] - r['baseline']} 条查询属 N+1 级变化还是有意为之？")
    say('— 毫秒仅作趋势参考，**不进红线**（共享 runner / SQLite 页缓存冷热会让它抖动；'
        '对标 Superset 的 print-not-assert）')
    if args.rebaseline:
        say('--rebaseline 模式：下表为按实测重算的建议预算')
        for r in rows:
            say(f"   '{r['name']}': (…, {r['measured']}, {r['suggested_budget']}),")
        return 0
    for name, decl, got in stale:
        say(f'⚠️ 声明基线过期：{name} 表里写 {decl}，实测 {got} ⇒ '
            '跑 --rebaseline 重标（预算本身仍按实测判定，不因此红）')
    if flaky:
        say(f'❌ 语句数非确定（同库同码两次跑出的数不同）：{", ".join(flaky)}'
            '——这条判据的前提被破坏，必须查明后再生效')
    for name, n, floor in empty:
        say(f'❌ 空探针：{name} 实测语句 {n} < 自证下限 {floor}'
            '——探针没跑到目标路径，绿了也是假绿（对标第 9 轮"恒真守卫"教训）')
    return 1 if (bad or flaky or empty) else 0


if __name__ == '__main__':
    sys.exit(main())
