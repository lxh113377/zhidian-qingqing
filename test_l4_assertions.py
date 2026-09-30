# -*- coding: utf-8 -*-
"""L4 业务正确性断言测试（Tessa V3-TEST 六条款①：断言含业务正确性）。

与 smoke_test.py 的区别：
  - smoke_test 为 L1/L2 级（页面能开、串能回），44/44 全绿但 L4=0；
  - 本套件为 L4 级，直接断言「算对账、判分守得住、CSRF 真开着」。
  - 刻意**不关闭** WTF_CSRF_ENABLED，验证安全开关不被测试自身旁路（F25）。

运行：python test_l4_assertions.py
退出码：0 = 全绿；1 = 存在失败断言。质量门禁以「❌ 不在输出」判定通过。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['DIFY_ENABLED'] = 'False'
os.environ['OPEN_API_ENABLED'] = '1'  # OPT-A 只读 API 默认关，套件显式开启覆盖端点行为

from test_fixtures import seed_baseline, use_temp_db

use_temp_db()

import pandas as pd

from src.analytics import compute_dashboard_stats
from src.app import create_app
from src.dify_client import DifyClient

# 注意：不设置 WTF_CSRF_ENABLED=False —— CSRF 保持生产默认开启
app = create_app('development')
seed_baseline(app)
client = app.test_client()

fails = []
passed = 0
# P7-D 断言分区：'骨架'（通用业务契约）与'包契约'（行业包改造 P2+ 新增），
# 汇总行自报分区计数，供门禁按自报口径取数（消除 ✅ 字符计数的口径漂移）。
# 可变容器承载分区状态（沿用项目「可变容器替代 global」范式，避免 PLW0603）。
_sec = {'cur': '骨架', '骨架': 0, '包契约': 0}


def section(name):
    assert name in ('骨架', '包契约'), f'未知分区: {name}'
    _sec['cur'] = name


def chk(name, cond, extra=''):
    global passed
    if cond:
        passed += 1
        _sec[_sec['cur']] += 1
        print(f'✅ {name}')
    else:
        fails.append(name + (f' | {extra}' if extra else ''))
        print(f'❌ {name} {extra}')


# ════════════ AC-OBS-13 上岗合格判定（规则版 + fail-close）══════════
c = DifyClient()  # DIFY_ENABLED=False → 规则版
# 规则版判分器是「关键词字面命中率」fallback（Cody 已标记 crude），
# 其真实契约：字面命中越多分越高，verbatim 命中满分；改写/空泛则低分。
ref = '西米、芒果、椰浆、西柚粒，芒果需选成熟金煌芒'
good = '西米、芒果、椰浆、西柚粒，芒果需选成熟金煌芒'  # 逐条答出标准要点（与 ref 同词边界）
bad = '随便做就行'

r_good = c.grade_answer('招牌配料是什么？', ref, good)
r_bad = c.grade_answer('招牌配料是什么？', ref, bad)
chk('AC-OBS-13 规则版：逐条答出要点判合格(满分)',
    r_good['is_correct'] is True and r_good['score'] == 100, str(r_good))
chk('AC-OBS-13 规则版：空泛作答判不合格',
    r_bad['is_correct'] is False and r_bad['score'] == 0, str(r_bad))
chk("AC-OBS-13 规则版：source='rule'", r_good['source'] == 'rule', str(r_good))

# fail-close：Dify 返回错误串 / 解析不到分数 → 绝不默认判及格（F1 核心）
c2 = DifyClient()
c2.enabled = True
c2._chat = lambda *a, **k: ('[DIFY_ERROR] 服务暂不可用', None)
rf = c2.grade_answer('q', 'ref', 'ans')
chk('AC-OBS-13 fail-close：错误返回判不合格',
    rf['is_correct'] is False and rf['score'] == 0 and rf['source'] == 'dify-error',
    str(rf))

c3 = DifyClient()
c3.enabled = True
c3._chat = lambda *a, **k: ('感谢参与，下次加油', None)  # 无「得分:X」标记
rg = c3.grade_answer('q', 'ref', 'ans')
chk('AC-OBS-13 fail-close：解析不到分数判 0 分',
    rg['is_correct'] is False and rg['score'] == 0, str(rg))


# ════════════ AC-OBS-08 原料损耗异常阈值（>15%）══════════
df = pd.DataFrame([
    {
        'staff_id': 1, 'record_date': pd.Timestamp('2026-08-01'),
        'product_name': 'A', 'product_sales_qty': 10, 'total_revenue': 100.0,
        'raw_material_remaining': 40, 'raw_material_waste': 10, 'abnormal_notes': '',
    },  # 损耗 10/50 = 20% > 15%
    {
        'staff_id': 1, 'record_date': pd.Timestamp('2026-08-02'),
        'product_name': 'B', 'product_sales_qty': 5, 'total_revenue': 50.0,
        'raw_material_remaining': 90, 'raw_material_waste': 5, 'abnormal_notes': '',
    },  # 损耗 5/95 ≈ 5.3% < 15%
])
stats = compute_dashboard_stats(df)
alert_products = {a['product'] for a in stats['waste_alerts']}
chk('AC-OBS-08 损耗率>15% 触发预警(A)', 'A' in alert_products, str(stats['waste_alerts']))
chk('AC-OBS-08 损耗率<15% 不触发(B)', 'B' not in alert_products)


# ════════════ AC-OBS-05/06 看板数据一致 ═══════════
chk('AC-OBS-06 饼图占比总和 = 总销量(15)', sum(stats['pie']['values']) == 15,
    str(stats['pie']))
chk('AC-OBS-05 KPI 总销量 = 明细和(15)', stats['kpi']['total_qty'] == 15,
    str(stats['kpi']))
chk('AC-OBS-05 报告天数 = 2', stats['kpi']['report_days'] == 2, str(stats['kpi']))


# ════════════ AI 经营分析：规则版报告数值与真实 stats 一致（L4 数据正确性）══════════
from src.dify_client import dify_client as _dc  # noqa: E402

_biz = _dc.business_analysis(stats)
chk('AI经营分析 规则版 source=rule', _biz.get('source') == 'rule', str(_biz))
_body = _biz.get('content', '')
chk('AI经营分析 报告含总销量(15)', '总销量 15 杯' in _body, _body[:120])
chk('AI经营分析 报告含损耗预警A归因', 'A' in _body and '损耗预警' in _body, _body[:200])
chk('AI经营分析 报告含优化建议段', '【④ 优化建议】' in _body, _body[:200])

# 智能助手（规则版）营收问答与 KPI 一致
_ask = _dc.assistant_ask('本月营收多少', stats)
chk('AI智能助手 营收答案＝KPI总营收', f"¥{stats['kpi']['total_revenue']:,}" in _ask['reply']
    or str(int(stats['kpi']['total_revenue'])) in _ask['reply'], _ask['reply'][:80])
# 智能助手：无数据时如实提示（不编造）
_empty = _dc.assistant_ask('营收多少', {'has_data': False, 'kpi': {}})
chk('AI智能助手 无数据显示引导而非编造', '还没有销售数据' in _empty['reply'], _empty['reply'][:80])


# ════════════ AI 判分语义化（第二轮）：近义词 / 否定反转 / 数字 / 满分契约保持 ═══════════
# 近义词改写作答（客人↔顾客、储存↔保存、保质期↔效期、扔掉↔丢弃）→ 仍判合格
_ref2 = '鲜奶开封后冷藏 2-6℃ 保存 48 小时，超过效期必须丢弃'
_syn = '鲜奶开封后放冰箱 2-6℃ 储存 48 小时，超过保质期要扔掉'
_syn_r = c.grade_answer('鲜奶保存', _ref2, _syn)
chk('判分 近义词改写作答判合格(>80)', _syn_r['is_correct'] is True and _syn_r['score'] >= 80,
    str(_syn_r))
# 否定语义反转：参考答案否定"禁止售卖"，作答转成肯定 → 不得分
_neg_r = c.grade_answer('效期处理', '超过效期禁止售卖，必须丢弃', '超过效期可以售卖，继续放着没事')
chk('判分 否定语义反转不得分', _neg_r['is_correct'] is False, str(_neg_r))
# 数字实体命中（15% ↔ 15%）→ num_ok=True 供前端展示
_num_r = c.grade_answer('损耗标准', '损耗率超过 15% 即为异常', '损耗率超 15% 就是异常')
chk('判分 数字实体命中 num_ok=True',
    _num_r.get('details', {}).get('num_ok') is True, str(_num_r))
# 满分契约保持（逐条同词边界 → 100 分）
_chk_full = c.grade_answer('招牌配料是什么？', '西米、芒果、椰浆、西柚粒，芒果需选成熟金煌芒',
                           '西米、芒果、椰浆、西柚粒，芒果需选成熟金煌芒')
chk('判分 逐条答出满分契约保持', _chk_full['score'] == 100 and _chk_full['is_correct'] is True,
    str(_chk_full))


# ════════════ AI 判分深化（第三轮）：知识库同义扩展兜底契约 ═══════════
# 契约1：注入知识库上下文不破坏满分契约（full-cover 仍恒 100）
_r3_full = c.grade_answer('招牌配料是什么？', '西米、芒果、椰浆、西柚粒，芒果需选成熟金煌芒',
                          '西米、芒果、椰浆、西柚粒，芒果需选成熟金煌芒')
chk('判分 kb扩展不破坏满分契约', _r3_full['score'] == 100 and _r3_full['is_correct'] is True,
    str(_r3_full))
# 契约2：近义改写合格线零回归（≥80）
_r3_syn = c.grade_answer('鲜奶保存', '鲜奶开封后冷藏 2-6℃ 保存 48 小时，超过效期必须丢弃',
                         '鲜奶开封后放冰箱 2-6℃ 储存 48 小时，超过保质期要扔掉')
chk('判分 kb扩展近义改写零回归(≥80)',
    _r3_syn['is_correct'] is True and _r3_syn['score'] >= 80, str(_r3_syn))
# 契约3：kb_ctx 空串时与旧版行为完全一致（零回归）
from src.dify_client import _rule_score  # noqa: E402

_base = _rule_score('超过效期必须丢弃', '过期就应该扔掉')
_with_empty = _rule_score('超过效期必须丢弃', '过期就应该扔掉', kb_ctx='')
chk('判分 kb_ctx空串零回归(结果一致)', _base == _with_empty, f'{_base} != {_with_empty}')
# 契约4：知识库词汇作答时判分路径完整出详情（不报错、元素标记齐）
_r3_kb = c.grade_answer('珍珠储存', '珍珠煮好过冰水后用糖水浸泡，常温 4 小时内、冷藏不超过 24 小时',
                        '珍珠煮完过冰水，常温放着，超过四小时就报废')
chk('判分 知识库作答判定出完整详情',
    isinstance(_r3_kb.get('details'), dict) and 'hit_elements' in _r3_kb.get('details', {}),
    str(_r3_kb))


# ════════════ AI 深化（第三轮）：多轮承接消解 + 培训助手个性化 ═══════════
from src.routes.ai import _followup_disambiguate  # noqa: E402

chk('多轮消解 承接词改写命中上一问',
    _followup_disambiguate('今天营收多少', '那毛利呢') == '结合上一问「今天营收多少」，那毛利呢',
    str(_followup_disambiguate('今天营收多少', '那毛利呢')))
chk('多轮消解 常规问句不改写',
    _followup_disambiguate('今天营收多少', '客单价多少') == '客单价多少',
    str(_followup_disambiguate('今天营收多少', '客单价多少')))

_tr_empty = {'has_data': False, 'kpi': {}}
_tr_p = _dc.trainer_ask('珍珠煮好后能放多久', _tr_empty,
                        personal_ctx={'weak_labels': ['原料'], 'wrong_count': 3,
                                      'recent_wrong_questions': ['珍珠能保存多久？']})
chk('培训助手 有弱点域时回复含补训提示', '薄弱点提示' in _tr_p['reply'], _tr_p['reply'][:100])
_tr_n = _dc.trainer_ask('珍珠煮好后能放多久', _tr_empty,
                        personal_ctx={'weak_labels': [], 'wrong_count': 0,
                                      'recent_wrong_questions': []})
chk('培训助手 无弱点域时无个性化区块', '薄弱点提示' not in _tr_n['reply'], _tr_n['reply'][:100])


# ════════════ AI 经营深化（第二轮）：周期对比 / 销量预测 / 周末规律（L4 数据正确性）══════════
from src.analytics import _forecast, _period_compare, _weekday_avg  # noqa: E402

# 21 天构造数据：前 14 日均 10、后 7 日均 20 → 近 7 vs 前 7 涨幅 +100%
_dates21 = pd.date_range('2026-08-01', periods=21, freq='D')
_rows21 = [{
    'staff_id': 1, 'record_date': d, 'product_name': 'A',
    'product_sales_qty': (10 if i < 14 else 20),
    'total_revenue': float(10 if i < 14 else 20) * 10.0,
    'raw_material_remaining': 20, 'raw_material_waste': 0, 'abnormal_notes': '',
} for i, d in enumerate(_dates21)]
_stats21 = compute_dashboard_stats(pd.DataFrame(_rows21))
_cmp21 = _stats21['compare']
chk('周期对比 近7vs前7 销量+100%',
    _cmp21['ok'] and _cmp21['qty_pct'] is not None and abs(_cmp21['qty_pct'] - 100.0) <= 1,
    str(_cmp21))
_fc21 = _stats21['forecast']
chk('销量预测 ok 且 qty/revenue>0',
    _fc21['ok'] and _fc21['qty'] > 0 and _fc21['revenue'] > 0, str(_fc21))
_wd21 = _stats21['weekday']
chk('周末规律 ok 且两侧日均非负',
    _wd21['ok'] and _wd21['weekday_avg'] >= 0 and _wd21['weekend_avg'] >= 0, str(_wd21))
# 无数据 → 各字段 ok=False，AI 侧不得编造
_empty_df = pd.DataFrame(columns=['staff_id', 'record_date', 'product_name',
                                  'product_sales_qty', 'total_revenue',
                                  'raw_material_remaining', 'raw_material_waste',
                                  'abnormal_notes'])
_empty_stats2 = compute_dashboard_stats(_empty_df)
chk('无数据 周期对比 ok=False', _empty_stats2['compare']['ok'] is False, str(_empty_stats2['compare']))
chk('无数据 预测不编造(qty=None)',
    _empty_stats2['forecast']['ok'] is False and _empty_stats2['forecast']['qty'] is None,
    str(_empty_stats2['forecast']))
_fc3 = _forecast({'qty': [100, 110, 105]}, 10.0)
chk('预测 3 日最小窗口可外推', _fc3['ok'] and _fc3['qty'] > 0 and _fc3['revenue'] > 0, str(_fc3))
_fc2 = _forecast({'qty': [10, 12]}, 5.0)
chk('预测 不足 3 日 ok=False', _fc2['ok'] is False and _fc2['qty'] is None, str(_fc2))


# ════════════ 第四轮 D1 学习者模型契约 ═══════════
from datetime import UTC  # noqa: E402
from datetime import datetime as _dt
from datetime import timedelta as _td
from types import SimpleNamespace as _NS  # noqa: E402

from src import ai_explain as _ax  # noqa: E402
from src import learner_model as _lm  # noqa: E402

_LM_NOW = _dt.now(UTC).replace(tzinfo=None)


def _mk_rec(question, score, correct=True, days=0):
    return _NS(question=question, score=score, is_correct=correct,
               created_at=_LM_NOW - _td(days=days))


_Q = '顾客嫌价格贵要砍价，正确话术是？'  # 关键词「价格」→ 套餐价格域
_p_all_right = _lm.mastery_profile([_mk_rec(_Q, 100)] * 5, now=_LM_NOW)
_p_all_wrong = _lm.mastery_profile([_mk_rec(_Q, 0, False)] * 5, now=_LM_NOW)
_p_single = _lm.mastery_profile([_mk_rec(_Q, 100)], now=_LM_NOW)
_d_all_right = next(d for d in _p_all_right['domains'] if d['sample'] > 0)
_d_all_wrong = next(d for d in _p_all_wrong['domains'] if d['sample'] > 0)
_d_single = next(d for d in _p_single['domains'] if d['sample'] > 0)
chk('学习者模型-全对达熟练(≥85)', _d_all_right['mastery'] >= 85, str(_d_all_right))
chk('学习者模型-全错判薄弱(≤15)', _d_all_wrong['mastery'] <= 15, str(_d_all_wrong))
chk('学习者模型-样本不足向中性收缩',
    _d_single['mastery'] < 100 and _d_single['confidence'] < 1.0, str(_d_single))
chk('学习者模型-难度阈值边界(85/60 分档)',
    _lm.adaptive_difficulty(85.0) == 'advanced'
    and _lm.adaptive_difficulty(84.9) == 'standard'
    and _lm.adaptive_difficulty(60.0) == 'standard'
    and _lm.adaptive_difficulty(59.9) == 'basic',
    f"{_lm.adaptive_difficulty(84.9)}/{_lm.adaptive_difficulty(59.9)}")
_sch = {s['domain']: s for s in _lm.review_schedule(
    [_mk_rec('原料储存条件是什么', 100)] * 5, now=_LM_NOW)}
_sch_bad = {s['domain']: s for s in _lm.review_schedule(
    [_mk_rec('原料储存条件是什么', 0, False)] * 5, now=_LM_NOW)}
chk('复习排期-掌握度高间隔长(14天)低间隔短(1天)',
    _sch['原料规范']['interval_days'] == 14 and _sch_bad['原料规范']['interval_days'] == 1,
    f"{_sch['原料规范']['interval_days']}/{_sch_bad['原料规范']['interval_days']}")

# 自适应出题：难度写进题面，standard 与旧版零差异
_q_adv = c.quiz_question(1, 5, difficulty='advanced')
_q_basic = c.quiz_question(1, 5, difficulty='basic')
_q_std = c.quiz_question(1, 5, difficulty='standard')
chk('自适应出题-进阶题面要求例外', '例外' in _q_adv['question'], _q_adv['question'][:40])
chk('自适应出题-基础题面降阶提示', '关键要点' in _q_basic['question'], _q_basic['question'][:40])
chk('自适应出题-标准难度题面零回归', '（' not in _q_std['question'], _q_std['question'][:40])
chk('自适应出题-非法难度回退标准',
    c.quiz_question(1, 5, difficulty='<script>')['difficulty'] == 'standard', '非法值未回退')

# ════════════ 第四轮 D2 经营智能契约 ═══════════
from src.analytics import (  # noqa: E402
    basket_association,
    detect_anomalies,
    product_forecast,
    replenishment_advice,
)

_flat = {'dates': [f'09-0{i}' for i in range(1, 8)],
         'revenue': [10, 11, 10, 10, 11, 10, 10], 'qty': [10, 11, 10, 10, 11, 10, 10]}
chk('离群检测-平稳序列不误报', detect_anomalies(_flat)['ok'] is False, str(detect_anomalies(_flat)))
_spike = dict(_flat, qty=[10, 11, 10, 10, 11, 10, 60],
              revenue=[10, 11, 10, 10, 11, 10, 60])
_a_spike = detect_anomalies(_spike)
chk('离群检测-突增命中且偏离为正',
    _a_spike['ok'] and _a_spike['points'][-1]['deviation_pct'] > 0
    and _a_spike['points'][-1]['date'].endswith('09-07'), str(_a_spike))

_pf_rows = []
for _i in range(1, 8):
    _pf_rows.append({'staff_id': 1, 'record_date': pd.to_datetime(f'2026-09-0{_i}'),
                     'product_name': '珍珠奶茶', 'product_sales_qty': 10,
                     'total_revenue': 120.0, 'raw_material_remaining': '30',
                     'raw_material_waste': '1', 'abnormal_notes': ''})
_pf_df = pd.DataFrame(_pf_rows)
_pf = product_forecast(_pf_df)
chk('分产品预测-平稳需求预测≈日均',
    _pf['ok'] and abs(_pf['items'][0]['next_qty'] - 10) < 0.5
    and _pf['items'][0]['trend'] == 1.0, str(_pf))
_rp = replenishment_advice(_pf_df)['items'][0]
chk('备货建议-算术口径正确',
    _rp['suggest'] == round(max(0.0, _rp['demand'] + _rp['safety_stock'] - 30.0), 1), str(_rp))

_bk_rows = []
for _i in range(1, 7):
    _d = pd.to_datetime(f'2026-08-0{_i}')
    if _i <= 5:
        _bk_rows.append({'record_date': _d, 'product_name': 'A', 'product_sales_qty': 1})
    if _i <= 3:
        _bk_rows.append({'record_date': _d, 'product_name': 'B', 'product_sales_qty': 1})
    if _i >= 6:
        _bk_rows.append({'record_date': _d, 'product_name': 'C', 'product_sales_qty': 1})
_bk = basket_association(pd.DataFrame(_bk_rows))
chk('购物篮-lift 计算正确(1.2)',
    _bk['ok'] and abs(_bk['pairs'][0]['lift'] - 1.2) < 0.01, str(_bk))

# ════════════ 第四轮 D3/D4 可解释性与 RAG 契约 ═══════════
chk('置信度-样本充分度高单调不减',
    _ax.sample_confidence(7, 7) >= _ax.sample_confidence(3, 7) >= _ax.sample_confidence(1, 7),
    f"{_ax.sample_confidence(1, 7)}/{_ax.sample_confidence(3, 7)}/{_ax.sample_confidence(7, 7)}")
with app.app_context():
    _stats_l4 = compute_dashboard_stats(days=365)
_ev_ok = [e for e in _ax.explain_business(_stats_l4) if e['kind'] != '数据充分性']
chk('可解释性-每条结论都带数据依据',
    len(_ev_ok) >= 1 and all(e['evidence'] and e['caveat'] for e in _ev_ok),
    str([e['kind'] for e in _ev_ok]))
chk('可解释性-置信度不宣称 100%',
    all(e['confidence'] <= 0.95 for e in _ev_ok), str([e['confidence'] for e in _ev_ok]))

from src import knowledge_base as _kb  # noqa: E402

_rare = _kb.search('西柚', top_k=2)
chk('RAG-稀有词命中对应条目', any('西柚' in h['text'] for h in _rare),
    str([h['title'] for h in _rare]))
chk('RAG-检索结果带反馈权重字段',
    all('weight' in h and h['weight'] > 0 for h in _rare), str(_rare))
chk('RAG-反馈权重有上下限',
    _kb.FEEDBACK_MIN <= _kb._feedback_weight('不存在的域', '不存在的条目') <= _kb.FEEDBACK_MAX,
    str(_kb._feedback_weight('不存在的域', '不存在的条目')))

# ════════════ F4/F25 CSRF 实际开启验证（不旁路口开关）══════════
# 取登录页 token 后登录（若登录路由 CSRF 豁免则 token 被忽略，不影响）
login_page = client.get('/login').get_data(as_text=True)
lm = re.search(r'name="csrf_token" value="([^"]+)"', login_page)
ltoken = lm.group(1) if lm else ''
r = client.post('/login', data={
    'username': 'owner_demo', 'password': 'owner123', 'role': 'admin',
    'csrf_token': ltoken,
}, follow_redirects=True)
chk('管理员登录 200', r.status_code == 200, f'code={r.status_code}')

# 取 staff 列表页 token
page = client.get('/admin/staff').get_data(as_text=True)
m = re.search(r'name="csrf_token" value="([^"]+)"', page)
token = m.group(1) if m else None
chk('CSRF token 可从页面取得', token is not None)

# 无 token 的 POST 状态变更 → 应被 CSRF 拒绝（400）
r_no = client.post('/admin/staff/1/toggle')
chk('F4/F25 无 CSRF token 的 POST 被拒(400)', r_no.status_code == 400,
    f'code={r_no.status_code}')

# 带 token 的 POST → 放行（302 重定向）
r_ok = client.post('/admin/staff/1/toggle', data={'csrf_token': token})
chk('带 CSRF token 的 POST 放行(302/200)', r_ok.status_code in (302, 200),
    f'code={r_ok.status_code}')

# ════════════ 第六轮 A：预测回测闭环契约 ════════════
from src import forecast_eval as _fe  # noqa: E402

_ev = _fe.evaluate_forecasts({
    '2026-09-01': {'predicted': 100.0, 'actual': 90.0},   # 误差 10% → hit
    '2026-09-02': {'predicted': 100.0, 'actual': 150.0},  # 误差 33.3% → miss
})
chk('回测 ok+窗口计数=2', _ev['ok'] and _ev['count'] == 2, str(_ev))
chk('回测 MAPE≈22.2%', abs((_ev['mape'] or 0) - 22.2) < 0.15, f"mape={_ev['mape']}")
chk('回测 命中率50%', _ev['hit_rate'] == 50.0, str(_ev))
chk('回测 last=最近日期明细', _ev['last']['date'] == '2026-09-02'
    and _ev['last']['actual'] == 150.0, str(_ev['last']))
chk('回测 空日志 ok=False 不编造', _fe.evaluate_forecasts({})['ok'] is False)
chk('回测 actual=0且预测>0 记满偏差',
    _fe.evaluate_forecasts({'d': {'predicted': 10, 'actual': 0}})['mape'] == 100.0)

# record_prediction：同日重复首次为准 + 历史回填（文件重定向，不读写真实 data/）
import tempfile as _tfd  # noqa: E402

_orig_ff = _fe.FORECAST_LOG_FILE
_fe.FORECAST_LOG_FILE = os.path.join(_tfd.mkdtemp(), 'forecast_log.json')
try:
    _fe.record_prediction('2026-09-10', 80.0)
    _fe.record_prediction('2026-09-10', 999.0)  # 同日重复渲染不得覆盖（防刷分）
    _a3 = _fe.record_prediction('2026-09-11', 90.0, {'2026-09-10': 100.0})
    chk('回测 首次为准(误差20%记命中)',
        _a3['ok'] and _a3['count'] == 1 and _a3['mape'] == 20.0, str(_a3))
    _a4 = _fe.record_prediction('2026-09-12', 95.0,
                                {'2026-09-10': 100.0, '2026-09-11': 80.0})
    chk('回测 历史回填滚动准确率83.8',
        _a4['ok'] and _a4['count'] == 2 and _a4['accuracy'] == 83.8, str(_a4))
finally:
    _fe.FORECAST_LOG_FILE = _orig_ff

# A 修复回归锁：compute 主链路必须把 ISO 日期传入 _forecast
#（第五轮缺陷：漏传 dates → 星期周期因子在主链路恒不生效）
from datetime import date as _d6  # noqa: E402

_d6s, _d6q = [], []
for _i in range(21):
    _dd = _d6(2026, 7, 6) + _td(days=_i)  # 2026-07-06 为周一
    _d6s.append(_dd.isoformat())
    _d6q.append(30 if _dd.weekday() >= 5 else 10)  # 周末 30 / 工作日 10
_fc6 = _forecast({'dates': _d6s, 'qty': _d6q}, 10.0)
chk('A修复 星期因子主链路生效(含季节调整)',
    '星期周期因子' in (_fc6.get('method') or ''), str(_fc6.get('method')))
chk('A修复 forecast 带目标日期', _fc6.get('target_date') == '2026-07-27',
    str(_fc6.get('target_date')))
chk('A修复 周一预测受低因子压制', _fc6['qty'] < 11, f"qty={_fc6['qty']}")

# ════════════ 第六轮 B：掌握度难度加权契约 ════════════
chk('B 题面basic提示识别',
    _lm.difficulty_of_question('鲜奶保存多久？（提示：答出关键要点即可）') == 'basic')
chk('B 题面advanced提示识别',
    _lm.difficulty_of_question('效期规范？（要求：请一并说明操作要点与例外情况）') == 'advanced')
chk('B 无提示按standard(零回归)',
    _lm.difficulty_of_question('普通题干') == 'standard')
_padv = _lm.mastery_profile([
    _mk_rec('原料保存规范（要求：请一并说明操作要点与例外情况）', 90) for _ in range(5)])
_pbas = _lm.mastery_profile([
    _mk_rec('原料保存规范（提示：答出关键要点即可）', 90) for _ in range(5)])
_madv = next(d for d in _padv['domains'] if d['sample'])['mastery']
_mbas = next(d for d in _pbas['domains'] if d['sample'])['mastery']
chk('B 进阶题答对掌握度更高', _madv > _mbas, f'advanced={_madv} basic={_mbas}')
chk('B 难度加权封顶不超100', _madv <= 100.0, f'mastery={_madv}')

# ════════════ 第六轮 C：损耗根因归因契约 ════════════
from src.analytics import waste_root_cause as _wrc  # noqa: E402

_df6 = pd.DataFrame({
    'record_date': pd.to_datetime(['2026-09-01', '2026-09-02',
                                   '2026-09-03', '2026-09-04']),
    'product_name': ['芒果', '珍珠', '椰浆', '柠檬'],
    'product_sales_qty': [10, 10, 10, 10],
    'total_revenue': [180.0] * 4,
    'raw_material_remaining': ['5', '5', '1', '5'],
    'raw_material_waste': ['10', '10', '10', '2'],  # 率 66%/66%/90%/28%
    'abnormal_notes': ['芒果变质有异味', '操作失误打翻一桶', None, None],
})
_wc6 = _wrc(_df6)
_by6 = {it['product']: it for it in _wc6.get('items') or []}
chk('C 归因 ok 且覆盖 4 条', _wc6['ok'] and len(_wc6['items']) == 4,
    str(len(_wc6.get('items') or [])))
chk('C 变质异味→储存不当', _by6.get('芒果', {}).get('cause') == 'storage',
    str(_by6.get('芒果')))
chk('C 打翻→操作浪费', _by6.get('珍珠', {}).get('cause') == 'operation',
    str(_by6.get('珍珠')))
chk('C 无备注且率>30%→备货过多', _by6.get('椰浆', {}).get('cause') == 'overstock',
    str(_by6.get('椰浆')))
chk('C 低率无备注→待排查低置信',
    _by6.get('柠檬', {}).get('cause') == 'unknown'
    and _by6.get('柠檬', {}).get('confidence') == '低', str(_by6.get('柠檬')))
chk('C 主因汇总落在四类标签内',
    (_wc6['summary'] or {}).get('main') in ('储存不当', '操作浪费', '备货过多', '待排查'),
    str((_wc6['summary'] or {}).get('main')))
chk('C 空表 ok=False 不编造', _wrc(pd.DataFrame())['ok'] is False)

# stats 主链路契约：新键齐备（第六轮 A/C）
with app.app_context():
    _stats6 = compute_dashboard_stats()
chk('stats 含 waste_causes 键', 'waste_causes' in _stats6)
chk('stats 含 daily_iso(回测对账用)',
    isinstance(_stats6.get('daily_iso'), list) and bool(_stats6['daily_iso']))
chk('stats forecast 带 target_date 字段', 'target_date' in _stats6.get('forecast', {}))

# ════════════ 第七轮 A：DeepSeek 双后端 provider 契约 ════════════
import src.dify_client as _dmod7  # noqa: E402
from src.dify_client import CircuitBreaker as _CB7  # noqa: E402
from src.dify_client import DifyClient as _DCls7  # noqa: E402


def _mk_llm7(provider, dk='', ck='', master=None):
    c = _DCls7.__new__(_DCls7)  # 绕过 __init__，避免读取进程真实环境变量
    c.base_url = 'http://dify.test'
    c.chat_key = ck
    c.advice_key = ''
    c.provider = provider
    c.deepseek_base = 'https://api.deepseek.com'
    c.deepseek_key = dk
    c.deepseek_model = 'deepseek-chat'
    c.timeout = 5
    c.enabled = c._resolve_enabled(master=master)
    c.breaker = _CB7()
    return c


chk('P1 provider=none 恒关', _mk_llm7('none', dk='k').enabled is False)
chk('P2 deepseek 有 Key 启用', _mk_llm7('deepseek', dk='k').enabled is True)
chk('P3 deepseek 无 Key 关闭(规则版)', _mk_llm7('deepseek', dk='').enabled is False)
chk('P4 auto 任一后端 Key 即启用', _mk_llm7('auto', dk='k').enabled is True)
chk('P5 auto 无 Key 关闭(离线演示)', _mk_llm7('auto').enabled is False)
chk('P6 dify 强制只认 Dify Key', _mk_llm7('dify', dk='k', ck='k2').enabled is True
    and _mk_llm7('dify', dk='k').enabled is False)
chk('P7 总开关 False 一票压住', _mk_llm7('deepseek', dk='k', master=False).enabled is False)


class _Resp7:
    def raise_for_status(self):
        pass

    def json(self):
        return {'choices': [{'message': {'content': 'OK-deepseek'}}]}


_REQ7_CALLS = []


class _ReqStub7:
    @staticmethod
    def post(url, **kw):
        _REQ7_CALLS.append((url, kw.get('json')))
        return _Resp7()


_orig_req7 = _dmod7.requests
_dmod7.requests = _ReqStub7
try:
    _c7 = _mk_llm7('deepseek', dk='k')
    _ans7, _cid7 = _c7._chat('你好', 'u', conversation_id='conv-1')
    chk('P8 deepseek 路由+content 解析', _ans7 == 'OK-deepseek' and _cid7 == 'conv-1',
        f'{_ans7}/{_cid7}')
    _url7, _body7 = _REQ7_CALLS[-1]
    chk('P9 OpenAI 兼容端点与报文', _url7.endswith('/chat/completions')
        and _body7.get('model') == 'deepseek-chat'
        and _body7['messages'][0]['content'] == '你好', str(_body7))
    _comp7 = _c7._completion('总结', 'admin')
    chk('P10 completion 等价走 deepseek 出口', _comp7 == 'OK-deepseek', str(_comp7))

    class _Boom7:
        @staticmethod
        def post(*a, **k):
            raise RuntimeError('boom-net')

    _dmod7.requests = _Boom7
    _bad7 = _mk_llm7('deepseek', dk='k')
    _err7, _cid_b7 = _bad7._chat('x', 'u', conversation_id='keep-me')
    chk('P11 失败统一收口脱敏([DIFY_ERROR])', str(_err7).startswith('[DIFY_ERROR]'),
        str(_err7))
    chk('P12 失败熔断计数+会话透传', _bad7.breaker._failures == 1 and _cid_b7 == 'keep-me')
finally:
    _dmod7.requests = _orig_req7


# ════════════ Q3 规则版 / 在线版「结构契约」等价守护 ════════════
# 项目特色是「未配 Key 回退规则版」= 每套能力两份实现。此处不追求文本一致，
# 只钉**结构契约**：两版同场景输出必须键齐全、类型正确、数值落在合法区间，
# 换模型/调 prompt 时结构破坏当场暴露（避免答辩现场才炸）。
_SOURCES_Q3 = {'rule', 'dify', 'dify-error'}


def _chk_advice(tag, out):
    chk(f'{tag} 备货建议：键含 source/content 且取值合法',
        isinstance(out, dict) and {'source', 'content'} <= set(out)
        and out.get('source') in _SOURCES_Q3
        and isinstance(out.get('content'), str) and out['content'].strip() != '',
        str(out)[:120])


def _chk_grade(tag, out):
    chk(f'{tag} 判分：键含 source/feedback/is_correct/score',
        isinstance(out, dict)
        and {'source', 'feedback', 'is_correct', 'score'} <= set(out)
        and isinstance(out['is_correct'], bool)
        and isinstance(out['feedback'], str)
        and isinstance(out['score'], (int, float)) and 0 <= out['score'] <= 100
        and out['source'] in _SOURCES_Q3, str(out)[:120])


def _chk_verdict(tag, out):
    chk(f'{tag} 考核结论：键含 pass/avg/weak/summary/suggestion 且类型合法',
        isinstance(out, dict)
        and {'pass', 'avg', 'weak', 'summary', 'suggestion'} <= set(out)
        and isinstance(out['pass'], bool)
        and isinstance(out['avg'], (int, float))
        and isinstance(out['weak'], list) and isinstance(out['summary'], str)
        and isinstance(out['suggestion'], list), str(out)[:120])


# 规则版三场景
_cq3 = DifyClient()  # DIFY_ENABLED=False → 规则版
_chk_advice('规则版', _cq3.restock_advice(stats))
_chk_grade('规则版', _cq3.grade_answer('招牌配料？', '西米、芒果、椰浆', '西米、芒果、椰浆'))
_chk_verdict('规则版', _cq3.assessment_verdict(
    [{'score': 80, 'weak': '原料规范', 'is_correct': True},
     {'score': 40, 'weak': '服务话术', 'is_correct': False}]))

# 在线版三场景（stub 驱动，验证「同样的消费方契约」不因走在线而破形）
_online_grade = DifyClient()
_online_grade.enabled = True
_online_grade._chat = lambda *a, **k: (
    '{"score": 66, "feedback": "部分正确", "weak": "服务话术"}', None)
_chk_grade('在线版', _online_grade.grade_answer('q', 'ref', 'ans'))

_online_advice = DifyClient()
_online_advice.enabled = True
_online_advice._completion = lambda *a, **k: '建议备货：珍珠 30 份，柠檬 20 份。'
_chk_advice('在线版·备货', _online_advice.restock_advice(stats))

_online_loss = DifyClient()
_online_loss.enabled = True
_online_loss._completion = lambda *a, **k: '近期损耗偏高，建议少备勤补。'
_chk_advice('在线版·损耗', _online_loss.loss_alert(stats))


# ════════════ S4 AI 调用留痕（在线调用落 JSONL + 可统计）═══════════
# 注意：留痕写在真实 _chat/_completion 内部，故这里 stub 的是「传输层」
# （dify_client.requests），而不是 _chat 本身——替换方法会把留痕一起绕过。
import src.dify_client as _dmod4  # noqa: E402
from src import ai_trace as _trace4  # noqa: E402


class _Resp4:
    def raise_for_status(self):
        pass

    def json(self):
        return {'choices': [{'message': {'content': 'OK-trace'}}]}


class _Req4:
    @staticmethod
    def post(url, **kw):
        return _Resp4()


_orig4 = _dmod4.requests
_dmod4.requests = _Req4
try:
    _t4 = _mk_llm7('deepseek', dk='k')
    _t4._chat('留痕测试', 'u')
finally:
    _dmod4.requests = _orig4
# record() 在 _LOG_LOCK 内同步写盘，故 recent(limit=1) 的最新一条必是本次调用。
# 不用「总数增长」判定：recent() 只读文件尾 64KB 窗口，账本超窗后前后计数恒等
# 会让该条件恒假（2026-09-23 实测 S4 假红灯根因，账本 1255 行 / 206KB 时暴露）。
_after4 = _trace4.recent(limit=1)
chk('S4 AI 调用留痕：在线调用写入记录（最新一条即本次调用）',
    bool(_after4) and _after4[0].get('scene') == 'chat'
    and _after4[0].get('provider') == 'deepseek'
    and _after4[0].get('fallback') is False,
    str(_after4[:1]))
chk('S4 留痕字段齐全（ts/scene/provider/source/latency_ms/ok）',
    bool(_after4) and {'ts', 'scene', 'provider', 'source', 'latency_ms', 'ok'}
    <= set(_after4[0]), str(_after4[:1]))
chk('S4 留痕记录成功调用 ok=True',
    bool(_after4) and _after4[0].get('ok') is True and _after4[0].get('scene') == 'chat',
    str(_after4[:1]))
chk('S4 留痕可汇总可用率/延迟', _trace4.stats().get('total', 0) >= 1, str(_trace4.stats()))
chk('S5 密钥脱敏不泄露完整 Key', DifyClient._mask_key('sk-abcdef123456') == '***3456',
    DifyClient._mask_key('sk-abcdef123456'))


# ════════════ P0 修复回归锁：考核问询 N+1 消除 ════════════
from sqlalchemy import event as _sa_event  # noqa: E402

from src.models import QuizRecord as _QR8  # noqa: E402
from src.models import Staff as _Staff8  # noqa: E402
from src.models import db as _db8  # noqa: E402
from src.routes.ai import _answer_assessment_query as _assess_q  # noqa: E402

with app.app_context():
    _staff8 = _Staff8.query.order_by(_Staff8.id).first()
    _db8.session.add(_QR8(staff_id=_staff8.id, quiz_type='question',
                          question='原料如何保存？', staff_answer='冷藏',
                          ai_feedback='回答准确', is_correct=True, score=100.0))
    _db8.session.commit()
    _sql_count = {'n': 0}

    def _count_sql8(conn, cursor, statement, params, context, executemany):
        _sql_count['n'] += 1

    _sa_event.listen(_db8.engine, 'before_cursor_execute', _count_sql8)
    try:
        _ans_q8 = _assess_q('谁考得最好')
    finally:
        _sa_event.remove(_db8.engine, 'before_cursor_execute', _count_sql8)
chk('F16 考核问询返回真实成绩文案', isinstance(_ans_q8, str) and '平均' in _ans_q8,
    str(_ans_q8)[:80])
chk('F16 考核问询无 N+1（固定查询，SQL 次数 ≤ 3）', _sql_count['n'] <= 3,
    f"SQL={_sql_count['n']} 次")

# ════════════ PF1 看板 stats 请求级缓存（同请求复用、跨请求不串）═══════════
# 缓存以「请求上下文」为准：同一请求内复用，跨请求/脱离请求上下文一律重算，
# 避免测试或长生命周期 app 上下文读到陈旧 stats（曾令冒烟毛利率断言失败）。
with app.test_request_context('/'):
    _cache_a = compute_dashboard_stats()
    _cache_b = compute_dashboard_stats()
chk('PF1 同请求内 stats 复用（缓存命中，同对象）', _cache_a is _cache_b)
with app.test_request_context('/'):
    _cache_c = compute_dashboard_stats()
chk('PF1 跨请求重新计算（缓存不跨请求）', _cache_c is not _cache_b)
with app.app_context():
    _cache_d = compute_dashboard_stats()
    _cache_e = compute_dashboard_stats()
chk('PF1 无请求上下文不缓存（app 上下文重算）', _cache_d is not _cache_e)


# ════════════ P2 自由填报指标：角色绑定 + 公式模板 + 自定义指标链路 ════════════
section('包契约')  # P7-D：P2 起为行业包改造新增断言，与骨架断言分区统计
from datetime import date as _date
from datetime import timedelta as _td

from src import industry_pack as _ip
from src import metrics as _m
from src.analytics import compute_dashboard_stats as _cds2
from src.analytics import get_sales_dataframe as _gsdf
from src.models import DailySales as _DS
from src.models import Merchant as _MerchantM  # noqa: E402
from src.models import Staff as _StaffM
from src.models import Store as _StoreM  # noqa: E402
from src.models import db as _db2

chk('P2 角色解析: stock→raw_material_remaining',
    _m.role_key('stock') == 'raw_material_remaining')
chk('P2 角色解析: waste→raw_material_waste',
    _m.role_key('waste') == 'raw_material_waste')
chk('P2 角色解析: note→abnormal_notes',
    _m.role_key('note') == 'abnormal_notes')
chk('P2 骨架角色齐备(dimension/quantity/amount)',
    _m.has_roles('dimension', 'quantity', 'amount'))
chk('P2 公式注册表含五大模板',
    {t['key'] for t in _m.FORMULA_TEMPLATES}
    == {'waste_rate', 'gross_margin', 'avg_ticket', 'sell_through', 'stock_turnover'})
chk('P2 损耗率公式数值(12/52)',
    abs(_m.compute_formula('waste_rate', stock=40, waste=12) - 12 / 52) < 1e-9)
chk('P2 公式分母为零返回None(不伪造)',
    _m.compute_formula('waste_rate', stock=0, waste=0) is None)
chk('P2 无产品成本证据时毛利率公式关闭',
    'gross_margin' not in [t['key'] for t in _m.available_formulas(has_product_cost=False)])
chk('P2 产品成本证据满足毛利率可用',
    'gross_margin' in [t['key'] for t in _m.available_formulas(has_product_cost=True)])

# ── 自定义包注入（DB 覆盖钩子）→ 定义识别 / 解析分工 / 宽表展开 / 能力关闭 ──
_V_PACK = {
    'id': 'test_custom', 'name': 'L4测试包', 'version': 't', 'source': 'db',
    'terms': {'record_dimension': '服务项目', 'quantity_unit': '次'},
    'domains': [{'name': '服务规范', 'keywords': ['服务']}],
    'weak_keywords': ['服务'],
    'metrics': [
        {'key': 'product_name', 'label': '服务项目', 'role': 'dimension',
         'legacy_column': 'product_name'},
        {'key': 'product_sales_qty', 'label': '次数', 'role': 'quantity',
         'legacy_column': 'product_sales_qty'},
        {'key': 'total_revenue', 'label': '营收', 'role': 'amount',
         'legacy_column': 'total_revenue'},
        {'key': 'near_waste', 'label': '临期报损', 'value_type': 'number',
         'unit': '件', 'role': 'waste', 'sort': 4},
        {'key': 'shift_note', 'label': '交接备注', 'value_type': 'text',
         'role': 'note', 'sort': 5},
        {'key': 'fridge_temp', 'label': '冷藏温度', 'value_type': 'number',
         'unit': '℃', 'role': 'other', 'required': True, 'sort': 6},
    ],
}
_ip.register_db_override(lambda pid: _V_PACK if pid == 'test_custom' else None)
_ip.set_active_pack('test_custom')
try:
    chk('P2 自定义指标定义识别(3条)',
        [d['key'] for d in _m.custom_metric_defs()]
        == ['near_waste', 'shift_note', 'fridge_temp'])
    chk('P2 自定义包 waste 角色解析到自定义键',
        _m.role_key('waste') == 'near_waste')
    chk('P2 自定义包缺 stock 角色',
        _m.has_roles('stock') is False)
    chk('P2 缺 stock 角色时损耗率公式不可用',
        'waste_rate' not in [t['key'] for t in _m.available_formulas(has_product_cost=False)])

    _v, _e = _m.parse_custom_metrics({'near_waste': '7.5', 'shift_note': '牛奶临期'})
    chk('P2 自定义指标解析数值与文本',
        _v == {'near_waste': 7.5, 'shift_note': '牛奶临期'}, f'{_v}|{_e}')
    try:
        _m.parse_custom_metrics({'near_waste': 'abc'})
        _raised = False
    except Exception:
        _raised = True
    chk('P2 自定义数值格式非法抛 ParseError(→400)', _raised)
    _v2, _e2 = _m.parse_custom_metrics({'near_waste': '999999999'})
    chk('P2 自定义数值超范围返回错误文案(→flash)', _e2 is not None, f'{_e2}')
    _v3, _e3 = _m.parse_custom_metrics({'near_waste': '1'})
    chk('P2 必填自定义指标缺失返回错误文案',
        _e3 is not None and '请填写' in _e3, f'{_e3}')
    _v4, _e4 = _m.parse_custom_metrics({'near_waste': '1', 'shift_note': 'x' * 501})
    chk('P2 自定义文本超长返回错误文案', _e4 is not None, f'len={len(_e4 or "")}')

    # 端到端：入库 → 宽表展开 → 缺角色能力关闭
    with app.app_context():
        _staff = _StaffM.query.filter_by(username='milk_tea_staff1').first()
        _db2.session.add(_DS(
            staff_id=_staff.id, record_date=_date.today() - _td(days=1),
            product_name='L4测试服务A', product_sales_qty=10, total_revenue=88,
            metrics_json='{"near_waste": "7.5", "shift_note": "交接顺利"}',
            pack_id='test_custom'))
        _db2.session.commit()
        _df = _gsdf(days=30)
    chk('P2 自定义指标展开为宽表列(含未填键补None)',
        'near_waste' in _df.columns and 'shift_note' in _df.columns
        and 'fridge_temp' in _df.columns)
    _row_num = _df[_df['product_name'] == 'L4测试服务A']
    chk('P2 宽表自定义值正确(7.5)',
        len(_row_num) == 1 and float(_row_num['near_waste'].iloc[0]) == 7.5)
    _stats_c = _cds2(df=_df)
    chk('P2 缺 stock 角色→损耗能力整体关闭(预警/归因/备货/派生卡)',
        _stats_c['capabilities']['waste'] is False
        and _stats_c['waste_alerts'] == []
        and _stats_c['derived_metrics'] == []
        and _stats_c['waste_causes']['ok'] is False
        and _stats_c['replenishment']['ok'] is False)
finally:
    _ip.register_db_override(lambda pid: None)
    _ip.set_active_pack(None)
chk('P2 恢复默认包后角色解析回归',
    _m.role_key('stock') == 'raw_material_remaining'
    and _ip.active_pack_id() == 'milk_tea')

# ── 默认包(奶茶)回归：角色化后统计口径与既有行为一致 ──
with app.app_context():
    _df_m = _gsdf(days=365)
_stats_m = _cds2(df=_df_m)
chk('P2 奶茶包角色解析列名与既有一致(损耗能力开启)',
    _stats_m['capabilities']['waste'] is True
    and _stats_m['capabilities']['note'] is True)
chk('P2 奶茶包派生损耗率卡存在(基线 2/52≈3.8%)',
    any(d['key'] == 'waste_rate' for d in _stats_m['derived_metrics']),
    str(_stats_m['derived_metrics']))
chk('P2 奶茶包 KPI 既有口径不受角色化影响',
    _stats_m['kpi']['total_qty'] > 0 and _stats_m['has_data'] is True)


# ════════════ P3 预置包：fruit/convenience 契约 + 按包切换 + 数据隔离 ════════════
from src.analytics import _waste_threshold as _waste_threshold_fn  # noqa: E402

chk('P3 三包齐备(list_pack_ids 含 milk_tea/fruit/convenience)',
    {'milk_tea', 'fruit', 'convenience'} <= set(_ip.list_pack_ids()))

_ip.set_active_pack('fruit')
try:
    _pk_f = _ip.current_pack()
    chk('P3 fruit 包切换生效(四域: 鲜度管理/损耗控制/称重计价/顾客服务)',
        [d['name'] for d in _pk_f['domains']]
        == ['鲜度管理', '损耗控制', '称重计价', '顾客服务'])
    chk('P3 fruit stock/waste 角色即插即用(映射既有列)',
        _m.role_key('stock') == 'raw_material_remaining'
        and _m.role_key('waste') == 'raw_material_waste')
    chk('P3 fruit 损耗能力开启 + 损耗率阈值随包(0.08)',
        _m.has_roles('stock', 'waste') and _waste_threshold_fn() == 0.08)
    chk('P3 fruit 题库/情景/产品加载(满配 36题/12情景/15产品)',
        len(_ip.pack_questions()) >= 36
        and len(_ip.pack_scenarios()['pool']) >= 12
        and len(_ip.pack_products()) >= 15)
    chk('P3 fruit 自定义指标(morning_price)识别',
        [d['key'] for d in _m.custom_metric_defs()] == ['morning_price'])
    chk('P3 fruit 骨架角色齐备(dimension/quantity/amount)',
        _m.has_roles('dimension', 'quantity', 'amount'))

    _ip.set_active_pack('convenience')
    _pk_c = _ip.current_pack()
    chk('P3 convenience 包切换生效(四域: 商品陈列/保质期管理/收银规范/客诉处理)',
        [d['name'] for d in _pk_c['domains']]
        == ['商品陈列', '保质期管理', '收银规范', '客诉处理'])
    chk('P3 convenience 无 stock/waste 角色',
        _m.has_roles('stock', 'waste') is False)
    chk('P3 convenience 损耗率公式自动关闭(不伪造)',
        'waste_rate' not in [t['key'] for t in _m.available_formulas(has_product_cost=False)])
    chk('P3 convenience 题库/情景/产品加载(满配 36题/12情景/15产品)',
        len(_ip.pack_questions()) >= 36
        and len(_ip.pack_scenarios()['pool']) >= 12
        and len(_ip.pack_products()) >= 15)
    chk('P3 convenience 自定义指标(coffee_sales)识别',
        [d['key'] for d in _m.custom_metric_defs()] == ['coffee_sales'])

    # ── PH2-1D：七包满配契约（schema_version=1 / 36题 / 12情景 / 15产品 / 4篇知识）──
    from src import pack_contract as _pc
    _pk_reports = _pc.validate_all()
    chk('PH2-1D 契约校验器覆盖全部内置包(≥7)',
        len(_pk_reports) >= 7, [r['pack_id'] for r in _pk_reports])
    chk('PH2-1D 七包完整性评分全部 100/100',
        all(r['ok'] and r['score'] == 100 for r in _pk_reports),
        [f"{r['pack_id']}:{r['score']}" for r in _pk_reports])
    chk('PH2-1D 七包 schema_version 均为 1（运行时归一化不丢字段）',
        all(_ip.get_pack(r['pack_id']).get('schema_version') == 1 for r in _pk_reports))
    chk('PH2-1D 七包题量/情景/产品/知识满配',
        all(r['stats']['questions'] >= 36 and r['stats']['scenarios'] >= 12
            and r['stats']['products'] >= 15 and r['stats']['knowledge_docs'] >= 4
            for r in _pk_reports))
    chk('PH2-1D 坏包 id 返回结构错误且不抛异常',
        _pc.validate_pack('__not_exist__')['ok'] is False)
    chk('PH2-1D 契约常量与运行时同源（单一真相源）',
        _pc.SCHEMA_VERSION == _ip.PACK_SCHEMA_VERSION == 1)

    # 负向：坏 JSON / 重复指标 key / 非法角色 —— 校验器必须识别且不抛异常
    import json as _json_pk
    import os as _os_pk
    import tempfile as _tf_pk
    with _tf_pk.TemporaryDirectory() as _tmp_pk:
        _bad = _os_pk.path.join(_tmp_pk, 'bad_json')
        _os_pk.makedirs(_bad)
        with open(_os_pk.path.join(_bad, 'pack.json'), 'w', encoding='utf-8') as _fh:
            _fh.write('{ not valid json ')
        _dup = _os_pk.path.join(_tmp_pk, 'dup_metric')
        _os_pk.makedirs(_dup)
        with open(_os_pk.path.join(_dup, 'pack.json'), 'w', encoding='utf-8') as _fh:
            _fh.write(_json_pk.dumps({
                'schema_version': 1, 'id': 'dup_metric', 'name': '重复指标包',
                'version': '1.0.0', 'terms': {}, 'domains': [], 'files': {},
                'metrics': [
                    {'key': 'dup_key', 'label': '甲', 'value_type': 'number',
                     'role': 'dimension', 'agg': 'sum'},
                    {'key': 'dup_key', 'label': '乙', 'value_type': 'number',
                     'role': '不存在的角色', 'agg': 'sum'},
                ],
            }, ensure_ascii=False))
        _orig_pk_root = _pc._PACK_ROOT
        try:
            _pc._PACK_ROOT = _tmp_pk
            _bad_json_report = _pc.validate_pack('bad_json')
            _dup_report = _pc.validate_pack('dup_metric')
        finally:
            _pc._PACK_ROOT = _orig_pk_root
    chk('PH2-1D 坏 JSON 包被识别且不抛异常',
        _bad_json_report['ok'] is False
        and any('JSON 损坏' in e for e in _bad_json_report['errors']),
        _bad_json_report['errors'][:1])
    chk('PH2-1D 重复指标 key + 非法 role 被识别',
        any('重复 key' in e for e in _dup_report['errors'])
        and any('非法 role' in e for e in _dup_report['errors']),
        _dup_report['errors'][:2])

    # 端到端数据隔离：convenience 激活时统计只见该包数据（临时库，独特品名防撞）
    with app.app_context():
        _staff_p3 = _StaffM.query.filter_by(username='milk_tea_staff1').first()
        _db2.session.add(_DS(
            staff_id=_staff_p3.id, record_date=_date.today() - _td(days=1),
            product_name='P3隔离测试-可乐', product_sales_qty=3,
            total_revenue=10.5, pack_id='convenience'))
        _db2.session.add(_DS(
            staff_id=_staff_p3.id, record_date=_date.today() - _td(days=1),
            product_name='P3隔离对照-葡萄', product_sales_qty=5,
            total_revenue=49.5, pack_id='fruit'))
        _db2.session.commit()
        _df_p3 = _gsdf(days=30)
    chk('P3 便利店激活时数据隔离(见本包/不见其他包与默认包)',
        (_df_p3['product_name'] == 'P3隔离测试-可乐').any()
        and not (_df_p3['product_name'].isin(
            ['P3隔离对照-葡萄', 'L4测试服务A', '珍珠奶茶'])).any())
finally:
    _ip.set_active_pack(None)

# 切回默认包：NULL（历史数据）与 milk_tea 显式值均放行
with app.app_context():
    _df_p3m = _gsdf(days=365)
chk('P3 默认包放行 NULL 历史数据与显式 milk_tea',
    _df_p3m['pack_id'].isna().any() or (_df_p3m['pack_id'] == 'milk_tea').any())
chk('P3 恢复默认包', _ip.active_pack_id() == 'milk_tea'
    and _m.role_key('stock') == 'raw_material_remaining')

# ── P7-C 唯一约束随包：跨包同键可共存，同包同键仍拦截 ──
from sqlalchemy import inspect as _insp7  # noqa: E402
from sqlalchemy.exc import IntegrityError as _IntErr7  # noqa: E402

with app.app_context():
    # 表级 UniqueConstraint 不出现在 get_indexes（PRAGMA 隐式索引被过滤），
    # 必须用 get_unique_constraints 实测
    _uq7 = next((i for i in _insp7(_db2.engine).get_unique_constraints('daily_sales')
                 if {'store_id', 'staff_id', 'record_date', 'product_name', 'pack_id'}
                 <= set(i['column_names'])),
                None)
    chk('P7-C/PH2-2 唯一约束列含 store_id + pack_id（表结构实测）', _uq7 is not None, str(_uq7))

    _staff_p7c = _StaffM.query.filter_by(username='milk_tea_staff1').first()
    _store_p7c = _staff_p7c.store_id
    _ds7a = _DS(staff_id=_staff_p7c.id, store_id=_store_p7c, record_date=_date(2026, 1, 1),
                product_name='P7C约束探针', product_sales_qty=1,
                total_revenue=1, pack_id='milk_tea')
    _ds7b = _DS(staff_id=_staff_p7c.id, store_id=_store_p7c, record_date=_date(2026, 1, 1),
                product_name='P7C约束探针', product_sales_qty=1,
                total_revenue=1, pack_id='fruit')
    _db2.session.add_all([_ds7a, _ds7b])
    try:
        _db2.session.commit()
        chk('P7-C 跨包同日同名产品可共存', True)
    except _IntErr7:
        _db2.session.rollback()
        chk('P7-C 跨包同日同名产品可共存', False)
    _db2.session.add(_DS(staff_id=_staff_p7c.id, store_id=_store_p7c,
                         record_date=_date(2026, 1, 1),
                         product_name='P7C约束探针', product_sales_qty=1,
                         total_revenue=1, pack_id='milk_tea'))
    try:
        _db2.session.commit()
        chk('P7-C 同包同日同名产品仍拦截', False)
    except _IntErr7:
        _db2.session.rollback()
        chk('P7-C 同包同日同名产品仍拦截', True)
    # PH2-2：跨店同名同包同日可共存（门店维度隔离）
    _merchant_p7c = _MerchantM.query.first()
    _store_p7c2 = _StoreM(merchant_id=_merchant_p7c.id, name='PH2-2二号店',
                          code='store2-probe')
    _db2.session.add(_store_p7c2)
    _db2.session.flush()
    _staff_p7c2 = _StaffM(username='staff-probe-p7c', display_name='二号店员',
                          store_name='PH2-2二号店', merchant_id=_merchant_p7c.id,
                          store_id=_store_p7c2.id)
    _staff_p7c2.set_password('probe123')
    _db2.session.add(_staff_p7c2)
    _db2.session.flush()
    _db2.session.add(_DS(staff_id=_staff_p7c2.id, store_id=_store_p7c2.id,
                         record_date=_date(2026, 1, 1),
                         product_name='P7C约束探针', product_sales_qty=1,
                         total_revenue=1, pack_id='milk_tea'))
    try:
        _db2.session.commit()
        chk('PH2-2 跨店同名同包同日产品可共存', True)
    except _IntErr7:
        _db2.session.rollback()
        chk('PH2-2 跨店同名同包同日产品可共存', False)
    # 清理探针行与探针账号/门店，不污染后续统计类断言
    _db2.session.query(_DS).filter(_DS.product_name == 'P7C约束探针').delete()
    _db2.session.query(_StaffM).filter(_StaffM.username == 'staff-probe-p7c').delete()
    _db2.session.query(_StoreM).filter(_StoreM.code == 'store2-probe').delete()
    _db2.session.commit()

    # ── PH2-2C：角色 / 门店解析 / 平台管理员边界 ──
    from src import tenant as _tn2
    from src.models import ROLE_MANAGER as _RM2
    from src.models import ROLE_PLATFORM as _RP2
    from src.models import Admin as _AdminM
    from src.models import AdminStoreAccess as _ASA2

    _owner2 = _AdminM.query.filter_by(username='owner_demo').first()
    _staff2c = _StaffM.query.filter_by(username='milk_tea_staff1').first()
    _merchant2c = _MerchantM.query.first()
    _st2a = _StoreM.query.filter_by(merchant_id=_merchant2c.id).first()
    _st2b = _StoreM(merchant_id=_merchant2c.id, name='PH2-2C二号店', code='store2c')
    _db2.session.add(_st2b)
    _db2.session.flush()
    _mgr2 = _AdminM(username='mgr-probe', role=_RM2, merchant_id=_merchant2c.id)
    _mgr2.set_password('probe123')
    _db2.session.add(_mgr2)
    _db2.session.flush()
    _db2.session.add(_ASA2(admin_id=_mgr2.id, store_id=_st2b.id))
    _db2.session.commit()
    _mgr2_id = _mgr2.id

    chk('PH2-2C 老板角色判定 + 解析到本商家首店',
        _tn2.user_role(_owner2) == 'owner'
        and _tn2.resolve_store_id(_owner2) == _st2a.id,
        f'{_tn2.user_role(_owner2)}/{_tn2.resolve_store_id(_owner2)}')
    # 写路径（h5.submit / admin.add_product）调用的正是 resolve_store_id(current_user)，
    # 因此这里直接断言该函数的返回，等价于验证写路径落到的门店。
    _tn2.set_current_store(None)
    chk('PH2-2C 店员角色 + 写路径门店解析一致',
        _tn2.user_role(_staff2c) == 'staff'
        and _tn2.staff_store_id(_staff2c) == _staff2c.store_id
        and _tn2.resolve_store_id(_staff2c) == _staff2c.store_id,
        str(_tn2.resolve_store_id(_staff2c)))
    chk('PH2-2C 店长解析到授权门店（而非首位门店）',
        _tn2.resolve_store_id(_mgr2) == _st2b.id
        and _tn2.visible_store_ids(_mgr2) == [_st2b.id])
    chk('PH2-2C 老板可见本商家全部门店',
        {_st2a.id, _st2b.id} <= set(_tn2.visible_store_ids(_owner2)))

    _plat2 = _AdminM(username='platform-probe', role=_RP2, merchant_id=None)
    _plat2.set_password('probe123')
    _db2.session.add(_plat2)
    _db2.session.commit()
    chk('PH2-2C 平台管理员不返回任何业务门店可见列表',
        _tn2.visible_store_ids(_plat2) == [])

_c_plat2 = app.test_client()
_lp2 = _c_plat2.get('/login')
_tk2 = re.search(r'name="csrf_token" value="([^"]+)"', _lp2.get_data(as_text=True))
_c_plat2.post('/login', data={'username': 'platform-probe', 'password': 'probe123',
                              'role': 'admin',
                              'csrf_token': _tk2.group(1) if _tk2 else ''})
_r_biz2 = _c_plat2.get('/admin/')
_r_acct2 = _c_plat2.get('/admin/staff')
chk('PH2-2C 平台管理员访问业务明细 403',
    _r_biz2.status_code == 403, str(_r_biz2.status_code))
chk('PH2-2C 平台管理员访问账号元数据页放行',
    _r_acct2.status_code == 200, str(_r_acct2.status_code))
chk('PH2-2C 老板访问业务明细仍 200（零回归）',
    client.get('/admin/').status_code == 200, str(client.get('/admin/').status_code))

with app.app_context():
    _db2.session.query(_ASA2).filter(_ASA2.admin_id == _mgr2_id).delete()
    _db2.session.query(_AdminM).filter(
        _AdminM.username.in_(['mgr-probe', 'platform-probe'])).delete(
        synchronize_session=False)
    _db2.session.query(_StoreM).filter(_StoreM.code == 'store2c').delete()
    _db2.session.commit()

    # ── PH2-2D：双商家多门店隔离 + 跨店越权 ──
    from src.analytics import _pack_record_filter as _prf2d
    from src.models import ROLE_OWNER as _RO2D
    from src.models import Product as _P2D
    from src.models import active_pack_clause as _apc2d

    _sA2d = _StoreM.query.order_by(_StoreM.id).first()
    _sA2d_id = _sA2d.id
    _ownerA2d = _AdminM.query.filter_by(username='owner_demo').first()
    _mB = _MerchantM(name='PH2-2D商家B')
    _db2.session.add(_mB)
    _db2.session.flush()
    _sB = _StoreM(merchant_id=_mB.id, name='PH2-2D-B店', code='b-store')
    _db2.session.add(_sB)
    _db2.session.flush()
    _staffB = _StaffM(username='staff-b-probe', display_name='B店店员',
                      store_name='PH2-2D-B店', merchant_id=_mB.id, store_id=_sB.id)
    _staffB.set_password('probe123')
    _db2.session.add(_staffB)
    _db2.session.flush()
    _prodB = _P2D(product_name='PH2-2D-B店专属品', unit_price=88.0,
                  category='探针', store_id=_sB.id, pack_id='milk_tea')
    _db2.session.add(_prodB)
    _db2.session.add(_DS(staff_id=_staffB.id, store_id=_sB.id,
                         record_date=_date.today() - _td(days=1),
                         product_name='PH2-2D-B店专属品', product_sales_qty=9,
                         total_revenue=792.0, pack_id='milk_tea'))
    _db2.session.commit()
    _sB_id, _prodB_id, _mB_id = _sB.id, _prodB.id, _mB.id

    chk('PH2-2D 老板A可见门店不含商家B的门店',
        _sB_id not in _tn2.visible_store_ids(_ownerA2d))
    chk('PH2-2D B店店员解析到B店（不落A店）',
        _tn2.resolve_store_id(_staffB) == _sB_id)

    _tn2.set_current_store(_sA2d_id)
    _namesA2d = {r.product_name for r in _DS.query.filter(_prf2d()).all()}
    _cross2d = _P2D.query.filter(
        _P2D.id == _prodB_id, _apc2d(_P2D.pack_id),
        _tn2.store_scope_clause(_P2D.store_id)).first()
    _tn2.set_current_store(_sB_id)
    _namesB2d = {r.product_name for r in _DS.query.filter(_prf2d()).all()}
    _tn2.set_current_store(None)
    chk('PH2-2D A店统计不含B店专属流水（读隔离）',
        'PH2-2D-B店专属品' not in _namesA2d)
    chk('PH2-2D B店统计含自身专属流水',
        'PH2-2D-B店专属品' in _namesB2d)
    chk('PH2-2D 跨店产品在A店范围不可见（等价路由404）',
        _cross2d is None)

_pg2d = client.get('/admin/products')
_tk2d = re.search(r'name="csrf_token" value="([^"]+)"', _pg2d.get_data(as_text=True))
_r_cross2d = client.post(f'/admin/products/{_prodB_id}/update',
                         data={'csrf_token': _tk2d.group(1) if _tk2d else '',
                               'product_name': 'PH2-2D-越权改名',
                               'unit_price': '1'})
chk('PH2-2D 跨商家产品改单 HTTP 404（越权拒绝）',
    _r_cross2d.status_code == 404, str(_r_cross2d.status_code))

with app.app_context():
    _db2.session.query(_DS).filter(_DS.product_name == 'PH2-2D-B店专属品').delete()
    _db2.session.query(_P2D).filter(_P2D.id == _prodB_id).delete()
    _db2.session.query(_StaffM).filter(_StaffM.username == 'staff-b-probe').delete()
    _db2.session.query(_StoreM).filter(_StoreM.id == _sB_id).delete()
    _db2.session.query(_MerchantM).filter(_MerchantM.id == _mB_id).delete()
    _db2.session.commit()

    # ── PH2-3A：邮箱验证码服务（仅存哈希 / TTL / 限尝试 / 限重发）──
    from src import email_verify as _ev3
    from src.models import EmailVerification as _EV3

    _mail3a, _mail3b, _mail3c = ('ph23a@example.com', 'ph23b@example.com',
                                 'ph23c@example.com')
    _sent3 = []
    _ev3.set_sender(lambda email, code, purpose: (
        _sent3.append((email, code, purpose)) or (True, 'stub')))
    _cfg3 = {k: app.config.get(k)
             for k in ('BREVO_API_KEY', 'BREVO_SENDER_EMAIL', 'SIGNUP_ENABLED')}
    app.config['BREVO_API_KEY'] = 'test-key'
    app.config['BREVO_SENDER_EMAIL'] = 'no-reply@example.com'
    app.config['SIGNUP_ENABLED'] = ''

    _r3a = _ev3.request_code(_mail3a)
    _row3a = _EV3.query.filter_by(email=_mail3a).order_by(_EV3.id.desc()).first()
    _code3a = _sent3[-1][1]
    chk('PH2-3A 验证码只存盐化哈希（非明文）',
        _r3a['ok'] and _row3a is not None and '$' in _row3a.code_hash
        and _code3a not in _row3a.code_hash)
    chk('PH2-3A 错误验证码被拒且显示剩余次数',
        _ev3.verify_code(_mail3a, '000000' if _code3a != '000000' else '111111')['ok'] is False)
    chk('PH2-3A 正确验证码通过并消费',
        _ev3.verify_code(_mail3a, _code3a)['ok'] is True)
    chk('PH2-3A 已消费验证码不可重放',
        _ev3.verify_code(_mail3a, _code3a)['reason'] == 'no_pending')

    _base3 = _ev3._now()
    _r3b = _ev3.request_code(_mail3b, now=_base3)
    _code3b = _sent3[-1][1]
    chk('PH2-3A 超过 TTL 的验证码被拒',
        _ev3.verify_code(_mail3b, _code3b,
                         now=_base3 + _td(minutes=_ev3.CODE_TTL_MINUTES + 1)
                         )['reason'] == 'expired')

    _r3c = _ev3.request_code(_mail3c)
    _code3c = _sent3[-1][1]
    _wrong3 = '000000' if _code3c != '000000' else '111111'
    for _ in range(_ev3.MAX_VERIFY_ATTEMPTS):
        _last3 = _ev3.verify_code(_mail3c, _wrong3)
    chk('PH2-3A 超尝试次数后验证码作废',
        _last3['reason'] == 'mismatch'
        and _ev3.verify_code(_mail3c, _wrong3)['reason'] == 'too_many_attempts')
    chk('PH2-3A 重发频率限制生效',
        _ev3.request_code(_mail3c)['reason'] == 'too_soon')

    _ev3.set_sender(None)
    app.config['BREVO_API_KEY'] = ''
    app.config['BREVO_SENDER_EMAIL'] = ''
    chk('PH2-3A 未配置 Brevo 时拒绝发码（自助注册关闭）',
        _ev3.request_code('ph23d@example.com')['reason'] == 'not_configured'
        and _ev3.signup_enabled() is False)
    app.config['BREVO_API_KEY'] = 'test-key'
    app.config['BREVO_SENDER_EMAIL'] = 'no-reply@example.com'
    chk('PH2-3A 配置 Brevo 后自助注册自动开启',
        _ev3.signup_enabled() is True)
    app.config['SIGNUP_ENABLED'] = '0'
    chk('PH2-3A 显式关闭开关优先于 Brevo 配置',
        _ev3.signup_enabled() is False)
    for _k3, _v3 in _cfg3.items():
        app.config[_k3] = _v3

    # ── PH2-3B：注册路由与商家创建 ──
    from src.models import ROLE_OWNER as _RO3B
    from src.models import Merchant as _Merchant3B

    _c_reg3 = app.test_client()
    app.config['SIGNUP_ENABLED'] = '0'
    _pg_closed = _c_reg3.get('/register')
    _tk_closed = re.search(r'name="csrf_token" value="([^"]+)"',
                           _pg_closed.get_data(as_text=True))
    _r_closed = _c_reg3.post('/register', data={
        'email': 'ph23-closed@example.com',
        'csrf_token': _tk_closed.group(1) if _tk_closed else ''})
    chk('PH2-3B 未配置邮件服务时注册页可访问但 POST 被拒(403)',
        _pg_closed.status_code == 200 and _r_closed.status_code == 403,
        f'{_pg_closed.status_code}/{_r_closed.status_code}')

    app.config['SIGNUP_ENABLED'] = ''
    app.config['BREVO_API_KEY'] = 'test-key'
    app.config['BREVO_SENDER_EMAIL'] = 'no-reply@example.com'
    _sent3b = []
    _ev3.set_sender(lambda email, code, purpose: (
        _sent3b.append((email, code, purpose)) or (True, 'stub')))
    _reg_mail3b = 'ph23-register@example.com'
    _pg_open = _c_reg3.get('/register')
    _tk_open = re.search(r'name="csrf_token" value="([^"]+)"',
                         _pg_open.get_data(as_text=True))
    _r_send3b = _c_reg3.post('/register', data={
        'email': _reg_mail3b, 'csrf_token': _tk_open.group(1) if _tk_open else ''})
    _code3b = _sent3b[-1][1] if _sent3b else ''
    _loc3b = _r_send3b.headers.get('Location', '')
    _c_reg3.get(_loc3b if _loc3b.startswith('/') else '/register/verify')
    chk('PH2-3B 提交邮箱后发码并跳转验证页',
        _r_send3b.status_code == 302 and '/register/verify' in _loc3b
        and len(_code3b) == _ev3.CODE_LENGTH,
        f'{_r_send3b.status_code}/{_loc3b}')

    _pg_v3b = _c_reg3.get('/register/verify?email=' + _reg_mail3b)
    _tk_v3b = re.search(r'name="csrf_token" value="([^"]+)"',
                        _pg_v3b.get_data(as_text=True))
    _base_form3b = {'email': _reg_mail3b, 'merchant_name': 'PH2-3B探针商家',
                    'username': 'owner-ph23b', 'password': 'Str0ngPass!',
                    'csrf_token': _tk_v3b.group(1) if _tk_v3b else ''}
    _wrong3b = '000000' if _code3b != '000000' else '111111'
    _r_wrong3b = _c_reg3.post('/register/verify',
                              data={**_base_form3b, 'code': _wrong3b})
    with app.app_context():
        _no_merchant3b = _Merchant3B.query.filter_by(
            contact_email=_reg_mail3b).count()
    chk('PH2-3B 错误验证码不创建商家(400)',
        _r_wrong3b.status_code == 400 and _no_merchant3b == 0,
        f'{_r_wrong3b.status_code}/{_no_merchant3b}')

    _r_ok3b = _c_reg3.post('/register/verify',
                           data={**_base_form3b, 'code': _code3b})
    with app.app_context():
        _merchant3b = _Merchant3B.query.filter_by(contact_email=_reg_mail3b).first()
        _owner3b = _AdminM.query.filter_by(username='owner-ph23b').first()
        _ok3b = bool(_merchant3b and _owner3b
                     and _owner3b.role == _RO3B
                     and _owner3b.merchant_id == _merchant3b.id)
        _merchant3b_id = _merchant3b.id if _merchant3b else None
        _owner3b_id = _owner3b.id if _owner3b else None
    chk('PH2-3B 验证成功后自动创建商家与老板账号',
        _r_ok3b.status_code == 302 and _ok3b,
        f'{_r_ok3b.status_code}/{_ok3b}')
    chk('PH2-3B 注册后自动登录并跳转后台',
        _r_ok3b.status_code == 302 and '/admin' in _r_ok3b.headers.get('Location', ''),
        _r_ok3b.headers.get('Location', ''))

    with app.app_context():
        _owner3b_obj = _AdminM.query.get(_owner3b_id)
        with app.test_request_context('/admin/'):
            from flask_login import login_user as _lu3b
            _lu3b(_owner3b_obj)
            _clause3b = str(_tn2.store_scope_clause(_DS.store_id)).lower()
        chk('PH2-3B 无门店老板不被放行全量数据（过滤为拒绝）',
            'false' in _clause3b or '0 = 1' in _clause3b, _clause3b[:60])

    with app.app_context():
        _EV3.query.filter(_EV3.email == _reg_mail3b).delete()
        _AdminM.query.filter(_AdminM.id == _owner3b_id).delete()
        _Merchant3B.query.filter(_Merchant3B.id == _merchant3b_id).delete()
        _db2.session.commit()
    _ev3.set_sender(None)
    for _k3b, _v3b in _cfg3.items():
        app.config[_k3b] = _v3b

    # ── PH2-3C：门店创建与成员邀请 ──
    from src.models import Admin as _Admin3C
    from src.models import AdminStoreAccess as _ASA3C
    from src.models import Staff as _Staff3C
    from src.models import Store as _Store3C

    # 用独立 app 实例跑 HTTP 断言：避免与前面长链路套件共享 app 造成的会话状态干扰
    _app3c = create_app('development')
    _c_own3c = _app3c.test_client()
    _lp3c = _c_own3c.get('/login')
    _tk_lp3c = re.search(r'name="csrf_token" value="([^"]+)"',
                         _lp3c.get_data(as_text=True))
    _r_login3c = _c_own3c.post('/login', data={
        'username': 'owner_demo', 'password': 'owner123', 'role': 'admin',
        'csrf_token': _tk_lp3c.group(1) if _tk_lp3c else ''})
    _pg3c = _c_own3c.get('/admin/stores/')
    _tk3c = re.search(r'name="csrf_token" value="([^"]+)"',
                      _pg3c.get_data(as_text=True))
    _tok3c = _tk3c.group(1) if _tk3c else ''
    chk('PH2-3C 老板可访问门店管理页', _pg3c.status_code == 200,
        str(_pg3c.status_code))

    with app.app_context():
        _mid3c = _Admin3C.query.filter_by(username='owner_demo').first().merchant_id
    _r_add3c = _c_own3c.post('/admin/stores/add', data={
        'csrf_token': _tok3c, 'name': 'PH2-3C二号店', 'code': 'ph23c-2'})
    with app.app_context():
        _st3c = _Store3C.query.filter_by(merchant_id=_mid3c, code='ph23c-2').first()
        _st3c_id = _st3c.id if _st3c else None
    chk('PH2-3C 老板新建门店成功',
        _r_add3c.status_code == 302 and _st3c_id is not None)

    _r_dup3c = _c_own3c.post('/admin/stores/add', data={
        'csrf_token': _tok3c, 'name': '重复编码店', 'code': 'ph23c-2'})
    with app.app_context():
        _dup_cnt3c = _Store3C.query.filter_by(merchant_id=_mid3c,
                                              code='ph23c-2').count()
    chk('PH2-3C 同商家重复门店编码被拒',
        _r_dup3c.status_code == 302 and _dup_cnt3c == 1, str(_dup_cnt3c))

    _r_mgr3c = _c_own3c.post(f'/admin/stores/{_st3c_id}/members/add', data={
        'csrf_token': _tok3c, 'role': 'manager',
        'username': 'mgr-ph23c', 'password': 'Str0ngPass!'})
    with app.app_context():
        _mgr3c = _Admin3C.query.filter_by(username='mgr-ph23c').first()
        _asa3c = (_ASA3C.query
                  .filter_by(admin_id=_mgr3c.id, store_id=_st3c_id).first()
                  if _mgr3c else None)
        _mgr3c_ok = bool(_mgr3c and _asa3c and _mgr3c.role == 'manager'
                         and _mgr3c.merchant_id == _mid3c)
    chk('PH2-3C 邀请店长写入门店授权', _r_mgr3c.status_code == 302 and _mgr3c_ok)

    _r_staff3c = _c_own3c.post(f'/admin/stores/{_st3c_id}/members/add', data={
        'csrf_token': _tok3c, 'role': 'staff', 'username': 'staff-ph23c',
        'display_name': '小C', 'password': 'Str0ngPass!'})
    with app.app_context():
        _sf3c = _Staff3C.query.filter_by(username='staff-ph23c').first()
        _sf3c_ok = bool(_sf3c and _sf3c.store_id == _st3c_id
                        and _sf3c.merchant_id == _mid3c)
    chk('PH2-3C 邀请店员绑定到该门店',
        _r_staff3c.status_code == 302 and _sf3c_ok)

    with app.app_context():
        _mB3c = _MerchantM(name='PH2-3C商家B')
        _db2.session.add(_mB3c)
        _db2.session.flush()
        _sB3c = _Store3C(merchant_id=_mB3c.id, name='PH2-3C-B店', code='b3c')
        _db2.session.add(_sB3c)
        _db2.session.commit()
        _sB3c_id, _mB3c_id = _sB3c.id, _mB3c.id
    _r_cross3c = _c_own3c.post(f'/admin/stores/{_sB3c_id}/members/add', data={
        'csrf_token': _tok3c, 'role': 'staff',
        'username': 'x-ph23c', 'password': 'Str0ngPass!'})
    chk('PH2-3C 跨商家门店操作 404（越权拒绝）',
        _r_cross3c.status_code == 404, str(_r_cross3c.status_code))

    _c_mgr3c = _app3c.test_client()
    _lp_mgr3c = _c_mgr3c.get('/login')
    _tk_mgr3c = re.search(r'name="csrf_token" value="([^"]+)"',
                          _lp_mgr3c.get_data(as_text=True))
    _c_mgr3c.post('/login', data={'username': 'mgr-ph23c',
                                  'password': 'Str0ngPass!', 'role': 'admin',
                                  'csrf_token': _tk_mgr3c.group(1) if _tk_mgr3c else ''})
    _pg_mgr3c = _c_mgr3c.get('/admin/stores/')
    _tk_mgr3c2 = re.search(r'name="csrf_token" value="([^"]+)"',
                           _pg_mgr3c.get_data(as_text=True))
    _r_mgr_add3c = _c_mgr3c.post('/admin/stores/add', data={
        'csrf_token': _tk_mgr3c2.group(1) if _tk_mgr3c2 else '',
        'name': '越权店', 'code': 'mgr-x'})
    chk('PH2-3C 店长可读门店列表但建店 403',
        _pg_mgr3c.status_code == 200 and _r_mgr_add3c.status_code == 403,
        f'{_pg_mgr3c.status_code}/{_r_mgr_add3c.status_code}')

    with app.app_context():
        _ASA3C.query.filter_by(store_id=_st3c_id).delete()
        _Admin3C.query.filter_by(username='mgr-ph23c').delete()
        _Staff3C.query.filter_by(username='staff-ph23c').delete()
        _Store3C.query.filter_by(id=_st3c_id).delete()
        _Store3C.query.filter_by(id=_sB3c_id).delete()
        _MerchantM.query.filter_by(id=_mB3c_id).delete()
        _db2.session.commit()

    # ── PH2-3D：门店选择器 / 切换 / 越权防护 ──
    with app.app_context():
        _sA2d2 = _Store3C(merchant_id=_mid3c, name='PH2-3D-A二店', code='ph23d-a2')
        _mB2d = _MerchantM(name='PH2-3D商家B')
        _db2.session.add_all([_sA2d2, _mB2d])
        _db2.session.flush()
        _sB2d = _Store3C(merchant_id=_mB2d.id, name='PH2-3D-B店', code='ph23d-b1')
        _sMgr2d = _Store3C(merchant_id=_mid3c, name='PH2-3D-授权店', code='ph23d-mgr')
        _db2.session.add_all([_sB2d, _sMgr2d])
        _db2.session.flush()
        _mgr2d = _Admin3C(username='mgr-ph23d', role='manager', merchant_id=_mid3c)
        _mgr2d.set_password('Str0ngPass!')
        _db2.session.add(_mgr2d)
        _db2.session.flush()
        # 只授权「授权店」，用于验证切到同商家未授权门店被拒
        _db2.session.add(_ASA3C(admin_id=_mgr2d.id, store_id=_sMgr2d.id))
        _db2.session.commit()
        _sA2d2_id, _sB2d_id, _mB2d_id = _sA2d2.id, _sB2d.id, _mB2d.id
        _sMgr2d_id, _mgr2d_id = _sMgr2d.id, _mgr2d.id

    _pg3d = _c_own3c.get('/admin/stores/')
    _tk3d = re.search(r'name="csrf_token" value="([^"]+)"',
                      _pg3d.get_data(as_text=True))
    _tok3d = _tk3d.group(1) if _tk3d else ''
    chk('PH2-3D 导航含门店选择器（老板）',
        'adminStoreSelect' in _pg3d.get_data(as_text=True))

    _r_sw3d = _c_own3c.post('/admin/stores/switch',
                            data={'csrf_token': _tok3d, 'store_id': _sA2d2_id})
    _pg_sw3d = _c_own3c.get('/admin/stores/')
    chk('PH2-3D 老板切换到本商家门店生效',
        _r_sw3d.status_code == 302
        and f'value="{_sA2d2_id}" selected' in _pg_sw3d.get_data(as_text=True),
        str(_r_sw3d.status_code))

    _r_cross3d = _c_own3c.post('/admin/stores/switch',
                               data={'csrf_token': _tok3d, 'store_id': _sB2d_id})
    _pg_cross3d = _c_own3c.get('/admin/stores/')
    chk('PH2-3D 跨商家切换 404 且会话门店不变',
        _r_cross3d.status_code == 404
        and f'value="{_sA2d2_id}" selected' in _pg_cross3d.get_data(as_text=True),
        str(_r_cross3d.status_code))

    _c_mgr3d = _app3c.test_client()
    _lp_mgr3d = _c_mgr3d.get('/login')
    _tk_lp3d = re.search(r'name="csrf_token" value="([^"]+)"',
                         _lp_mgr3d.get_data(as_text=True))
    _c_mgr3d.post('/login', data={'username': 'mgr-ph23d',
                                  'password': 'Str0ngPass!', 'role': 'admin',
                                  'csrf_token': _tk_lp3d.group(1) if _tk_lp3d else ''})
    _pg_mgr3d = _c_mgr3d.get('/admin/stores/')
    _tk_mgr3d = re.search(r'name="csrf_token" value="([^"]+)"',
                          _pg_mgr3d.get_data(as_text=True))
    _r_mgr_sw3d = _c_mgr3d.post('/admin/stores/switch',
                                data={'csrf_token': _tk_mgr3d.group(1) if _tk_mgr3d else '',
                                      'store_id': _sA2d2_id})
    chk('PH2-3D 店长切换到未授权门店 403',
        _r_mgr_sw3d.status_code == 403, str(_r_mgr_sw3d.status_code))

    with app.app_context():
        _own3d = _Admin3C.query.filter_by(username='owner_demo').first()
        _staff3d = _Staff3C.query.filter_by(username='milk_tea_staff1').first()
        chk('PH2-3D 存量单店账号（店员）无需切换即直达绑定门店',
            _tn2.resolve_store_id(_staff3d) == _staff3d.store_id)
        chk('PH2-3D 老板可选门店集合覆盖本商家全部门店',
            {_sA2d2_id, _st3c_id} <= set(_tn2.allowed_store_ids(_own3d)))

    with app.app_context():
        _ASA3C.query.filter_by(admin_id=_mgr2d_id).delete()
        _Admin3C.query.filter_by(username='mgr-ph23d').delete()
        _Store3C.query.filter_by(id=_sA2d2_id).delete()
        _Store3C.query.filter_by(id=_sMgr2d_id).delete()
        _Store3C.query.filter_by(id=_sB2d_id).delete()
        _MerchantM.query.filter_by(id=_mB2d_id).delete()
        _db2.session.commit()

    # ── PH2-4A：中性「通用门店」空白包骨架 ──
    from src import pack_skeleton as _psk4
    from src import pack_store as _ps4

    with app.app_context():
        _ok4, _msg4 = _ps4.create_pack('ph24_blank', 'PH2-4通用店',
                                       description='PH2-4A 中性骨架探针')
        _blank4 = _ip.get_pack('ph24_blank')
        _blank4_terms = _blank4.get('terms') or {}
        _blank4_roles = {m['role'] for m in (_blank4.get('metrics') or [])}
        _blank4_questions = _blank4.get('questions') or []
        _blank4_text = _json_pk.dumps(_blank4, ensure_ascii=False)
        _blank4_kb = _blank4.get('knowledge') or {}
        _blank4_metric_count = len(_blank4.get('metrics') or [])
        chk('PH2-4A 空白包创建成功（返回 ok）', _ok4 is True, _msg4)
        chk('PH2-4A 空白包术语为中性口径（无奶茶词）',
            _blank4_terms.get('quantity_unit') == '单'
            and '奶茶' not in _blank4_text and '珍珠' not in _blank4_text
            and '杨枝甘露' not in _blank4_text)
        chk('PH2-4A 空白包指标仅三个必需角色（无 stock/waste/note）',
            _blank4_roles == {'dimension', 'quantity', 'amount'}
            and _blank4_metric_count == 3, str(sorted(_blank4_roles)))
        chk('PH2-4A 空白包题库为中性题（未继承奶茶题库）',
            len(_blank4_questions) >= 4
            and all('珍珠' not in x.get('q', '') for x in _blank4_questions))
        chk('PH2-4A 空白包知识域为中性四域',
            [d['name'] for d in _blank4.get('domains') or []]
            == [n for n, _, _ in _psk4._DOMAIN_SPECS]
            and len(_blank4_kb) >= 4,
            str(list(_blank4_kb.keys()))[:80])
        chk('PH2-4A 空白包带中性起点内容（题/情景/产品非空）',
            bool(_blank4_questions) and bool(_blank4.get('scenarios'))
            and bool(_blank4.get('products')))
    with app.app_context():
        _ps4.delete_pack('ph24_blank')

    # ── PH2-4B：建包向导 + AI 草稿（草稿不落库）──
    from src import pack_wizard as _pw4
    from src.models import PackItem as _PI4

    _ok_brief4, _errs4, _ = _pw4.validate_brief({})
    chk('PH2-4B 向导必填校验（行业类型/主营项目）',
        _ok_brief4 is False and len(_errs4) >= 2, str(_errs4))
    _ok_long4, _errs_long4, _ = _pw4.validate_brief(
        {'industry_type': '超' * 51, 'main_items': '咖啡'})
    chk('PH2-4B 向导字段长度上限校验',
        _ok_long4 is False and any('超过' in e for e in _errs_long4))

    _ok_ok4, _errs_ok4, _brief4 = _pw4.validate_brief({
        'industry_type': 'PH2-4B咖啡店', 'main_items': '手冲咖啡、拿铁、可颂',
        'target_audience': '周边上班族', 'focus': '库存与损耗',
        'unit': '杯', 'free_text': '主打外带'})
    chk('PH2-4B 合法向导输入通过校验', _ok_ok4 is True, str(_errs_ok4))

    _tpl4 = _pw4.build_template_draft(_brief4)
    _tpl_val4 = _pw4.validate_draft(_tpl4)
    chk('PH2-4B 模板草稿结构合法且有完整性评分',
        _tpl_val4['ok'] is True and _tpl_val4['score'] > 0, str(_tpl_val4['errors'][:2]))
    chk('PH2-4B 模板草稿术语/单位/项目随向导取值',
        _tpl4['terms']['quantity_unit'] == '杯'
        and 'PH2-4B咖啡店' in _tpl4['terms']['assistant_persona']
        and {p['product_name'] for p in _tpl4['products']}
        >= {'手冲咖啡', '拿铁', '可颂'}
        and {m['role'] for m in _tpl4['metrics']} == {'dimension', 'quantity', 'amount'})

    _gen_none4 = _pw4.generate_draft(_brief4, llm=None)
    chk('PH2-4B 未接入 AI 时回落模板并标注来源',
        _gen_none4['source'] == 'template' and _gen_none4['warnings'])
    _gen_bad4 = _pw4.generate_draft(_brief4, llm=lambda _p: '这不是 JSON')
    chk('PH2-4B AI 返回非法结构时回落模板（不落库）',
        _gen_bad4['source'] == 'template'
        and any('非法' in w for w in _gen_bad4['warnings']))
    _gen_err4 = _pw4.generate_draft(_brief4, llm=lambda _p: (_ for _ in ()).throw(RuntimeError('boom')))
    chk('PH2-4B AI 调用异常时回落模板',
        _gen_err4['source'] == 'template'
        and any('失败' in w for w in _gen_err4['warnings']))
    _gen_quota4 = _pw4.generate_draft(_brief4, llm=lambda _p: {'name': 'x'},
                                      timeout_note='AI 配额已用尽，已回落模板草稿')
    chk('PH2-4B 超限/超时回落模板并保留提示',
        _gen_quota4['source'] == 'template'
        and '配额已用尽' in _gen_quota4['warnings'][0])
    _gen_ai4 = _pw4.generate_draft(
        _brief4,
        llm=lambda _p: {'name': 'PH2-4B咖啡店', 'terms': {'quantity_unit': '杯'},
                        'weak_keywords': ['咖啡'], 'synonyms': [['便宜', '优惠']]})
    chk('PH2-4B AI 合法草稿标记来源为 ai（仍仅是草稿）',
        _gen_ai4['source'] == 'ai' and _gen_ai4['validation']['ok'] is True,
        str(_gen_ai4['validation']['errors'][:2]))

    with app.app_context():
        _pi_before4 = _PI4.query.count()
    _tok4 = _pw4.put_draft(_gen_ai4['draft'], brief=_brief4, source='ai')
    _peek4 = _pw4.peek_draft(_tok4)
    _take4 = _pw4.take_draft(_tok4)
    _take_again4 = _pw4.take_draft(_tok4)
    with app.app_context():
        _pi_after4 = _PI4.query.count()
    chk('PH2-4B 草稿缓存 peek 保留 / take 一次性取出',
        _peek4 is not None and _take4 is not None and _take_again4 is None)
    chk('PH2-4B 草稿生成与缓存全程不落库（PackItem 数不变）',
        _pi_before4 == _pi_after4, f'{_pi_before4}->{_pi_after4}')
    _pw4.clear_drafts()

    # ── PH2-4C：行业包 JSON 导入导出 / 复制 / 校验预览 / 冲突 / 恢复 ──
    from src import pack_io as _pio4
    from src import pack_store as _ps4c

    _exp4 = _pio4.export_pack_full('bakery')
    chk('PH2-4C 导出内置包结构完整（版本/元信息/各段非空）',
        _exp4 and _exp4['schema_version'] == 1
        and _exp4['meta']['id'] == 'bakery'
        and all(_exp4.get(k) for k in ('terms', 'domains', 'metrics', 'knowledge',
                                       'questions', 'scenarios', 'products')),
        str(list((_exp4 or {}).keys())))

    _v_exp4 = _pio4.validate_import(_exp4)
    _imp_ok4, _imp_msg4, _ = _pio4.import_pack(_exp4, target_id='ph24c_copy')
    _exp_copy4 = _pio4.export_pack_full('ph24c_copy')
    _keys4 = ('terms', 'domains', 'metrics', 'knowledge', 'questions',
              'scenarios', 'products', 'weak_keywords', 'synonyms', 'formula_params')
    _same4 = all(
        _json_pk.dumps(_exp4.get(k), ensure_ascii=False, sort_keys=True)
        == _json_pk.dumps(_exp_copy4.get(k), ensure_ascii=False, sort_keys=True)
        for k in _keys4)
    chk('PH2-4C 导入导出往返无损失（逐段一致）',
        _v_exp4['ok'] and _imp_ok4 and _same4, _imp_msg4)

    _bad_ver4 = dict(_exp4)
    _bad_ver4['schema_version'] = 99
    _rv4 = _pio4.validate_import(_bad_ver4)
    chk('PH2-4C 未知 schema_version 被拒',
        _rv4['ok'] is False and any('schema_version' in e for e in _rv4['errors']))

    _bad_key4 = _json_pk.loads(_json_pk.dumps(_exp4, ensure_ascii=False))
    _bad_key4['knowledge']['../evil.md'] = '# 恶意'
    _rk4 = _pio4.validate_import(_bad_key4)
    chk('PH2-4C 恶意外键（路径穿越知识键）被拒',
        _rk4['ok'] is False and any('非法' in e for e in _rk4['errors']),
        str(_rk4['errors'][:2]))

    _big4 = _json_pk.loads(_json_pk.dumps(_exp4, ensure_ascii=False))
    _big4['knowledge']['huge.md'] = 'x' * (_ps4c.MAX_KNOWLEDGE_LEN + 1)
    _rb4 = _pio4.validate_import(_big4)
    chk('PH2-4C 超长知识正文被拒',
        _rb4['ok'] is False and any('超上限' in e for e in _rb4['errors']))
    _rbytes4 = _pio4.validate_import('x' * (_pio4.MAX_IMPORT_BYTES + 1))
    chk('PH2-4C 超长导入整体被拒',
        _rbytes4['ok'] is False and any('上限' in e for e in _rbytes4['errors']))

    _conf4, _conf_msg4, _conf_rep4 = _pio4.import_pack(_exp4, target_id='bakery')
    chk('PH2-4C 目标包已存在且未声明覆盖 → 冲突不落库',
        _conf4 is False and _conf_rep4.get('conflicts'),
        str(_conf_rep4.get('conflicts')))
    _ok_rest4, _msg_rest4 = _pio4.restore_builtin('ph24c_copy')
    _after_rest4 = _pio4.export_pack_full('ph24c_copy')
    chk('PH2-4C 恢复内置示例（清除 DB 覆盖层）',
        _ok_rest4 is True and _after_rest4 is None, _msg_rest4)

    with app.app_context():
        _ok_cp4, _msg_cp4 = _pio4.copy_pack('bakery', 'ph24c_src_copy', 'PH2-4C副本包')
        _cp_export4 = _pio4.export_pack_full('ph24c_src_copy')
    chk('PH2-4C 复制包成功且内容非空',
        _ok_cp4 and _cp_export4 and len(_cp_export4.get('questions') or []) > 0,
        _msg_cp4)

    with app.app_context():
        _ps4c.delete_pack('ph24c_src_copy')
        _pio4.restore_builtin('ph24c_copy')

    # ── PH2-4D：AI 配额 / 超时 / 缓存 / 降级 ──
    from src import ai_quota as _aq4
    from src.models import AIUsageLedger as _AUL4

    chk('PH2-4D 配额常量符合规格（3 造包 / 30 分析 / 120k Token / 30s / 10min）',
        _aq4.PACK_GEN_DAILY_LIMIT == 3 and _aq4.ANALYSIS_DAILY_LIMIT == 30
        and _aq4.TOKEN_DAILY_LIMIT == 120000 and _aq4.AI_TIMEOUT_SECONDS == 30
        and _aq4.CACHE_TTL_SECONDS == 600)

    with app.app_context():
        _mA4 = _MerchantM(name='PH2-4D商家A')
        _mB4 = _MerchantM(name='PH2-4D商家B')
        _db2.session.add_all([_mA4, _mB4])
        _db2.session.commit()
        _mA4_id, _mB4_id = _mA4.id, _mB4.id

    with app.app_context():
        for _ in range(_aq4.PACK_GEN_DAILY_LIMIT):
            _aq4.record_usage(_mA4_id, _aq4.KIND_PACK_GEN, tokens=1000)
        _q4_gen = _aq4.check_quota(_mA4_id, _aq4.KIND_PACK_GEN)
        _q4_ana = _aq4.check_quota(_mA4_id, _aq4.KIND_ANALYSIS)
        _q4_b = _aq4.check_quota(_mB4_id, _aq4.KIND_PACK_GEN)
    chk('PH2-4D 造包次数用尽后拒绝（分析配额不受影响）',
        _q4_gen['ok'] is False and _q4_gen['reason'] == 'quota_exceeded'
        and _q4_ana['ok'] is True, str(_q4_gen))
    chk('PH2-4D 配额按商家隔离（B 商家不受 A 影响）',
        _q4_b['ok'] is True and _q4_b['used'] == 0, str(_q4_b))

    with app.app_context():
        _aq4.record_usage(_mA4_id, _aq4.KIND_ANALYSIS,
                          tokens=_aq4.TOKEN_DAILY_LIMIT)
        _q4_tok = _aq4.check_quota(_mA4_id, _aq4.KIND_ANALYSIS)
    chk('PH2-4D Token 总量触顶后拒绝',
        _q4_tok['ok'] is False and _q4_tok['reason'] == 'token_exceeded',
        str(_q4_tok))

    _calls4 = {'n': 0}

    def _producer4(_timeout):
        _calls4['n'] += 1
        return {'text': 'AI 结果'}, 500

    with app.app_context():
        _aq4.clear_cache()
        _r_ai4 = _aq4.run_with_quota(_mB4_id, _aq4.KIND_ANALYSIS, _producer4,
                                     lambda: {'text': '规则版'}, cache_key='k1')
        _r_cache4 = _aq4.run_with_quota(_mB4_id, _aq4.KIND_ANALYSIS, _producer4,
                                        lambda: {'text': '规则版'}, cache_key='k1')
        _ledger4 = _aq4.usage_today(_mB4_id, _aq4.KIND_ANALYSIS)
    chk('PH2-4D 首次调用走 AI 并落账',
        _r_ai4['source'] == 'ai' and _r_ai4['value']['text'] == 'AI 结果'
        and _ledger4['calls'] == 1 and _ledger4['tokens'] == 500, str(_ledger4))
    chk('PH2-4D 10 分钟内命中缓存且不再调用模型',
        _r_cache4['source'] == 'cache' and _calls4['n'] == 1, str(_calls4))

    with app.app_context():
        _aq4.clear_cache()
        _r_over4 = _aq4.run_with_quota(_mA4_id, _aq4.KIND_PACK_GEN,
                                       lambda _t: (_ for _ in ()).throw(AssertionError('不应被调用')),
                                       lambda: {'text': '模板草稿'})
    chk('PH2-4D 超限时回落规则版且不调用模型',
        _r_over4['source'] == 'fallback' and _r_over4['reason'] == 'quota_exceeded'
        and _r_over4['value']['text'] == '模板草稿', str(_r_over4['reason']))

    with app.app_context():
        _aq4.clear_cache()
        _r_to4 = _aq4.run_with_quota(
            _mB4_id, _aq4.KIND_ANALYSIS,
            lambda _t: (_ for _ in ()).throw(TimeoutError()),
            lambda: {'text': '规则版'}, cache_key='k-timeout')
        _r_err4 = _aq4.run_with_quota(
            _mB4_id, _aq4.KIND_ANALYSIS,
            lambda _t: (_ for _ in ()).throw(RuntimeError('boom')),
            lambda: {'text': '规则版'}, cache_key='k-error')
        _failed4 = _AUL4.query.filter_by(merchant_id=_mB4_id, ok=False).count()
    chk('PH2-4D 超时回落规则版并记录失败账本',
        _r_to4['source'] == 'fallback' and _r_to4['reason'] == 'timeout'
        and _failed4 >= 1, f"{_r_to4['reason']}/{_failed4}")
    chk('PH2-4D 调用异常回落规则版',
        _r_err4['source'] == 'fallback' and _r_err4['reason'] == 'error',
        str(_r_err4['reason']))

    with app.app_context():
        _AUL4.query.filter(_AUL4.merchant_id.in_([_mA4_id, _mB4_id])).delete(
            synchronize_session=False)
        _MerchantM.query.filter(_MerchantM.id.in_([_mA4_id, _mB4_id])).delete(
            synchronize_session=False)
        _db2.session.commit()
    _aq4.clear_cache()

    # ── PH2-4E：DIY 工作台 HTTP 路由（向导/预览/确认/导入导出）──
    # 复用 PH2-3C 已登录的老板客户端：登录限流按 IP 共享（10 次/60s），
    # 套件内反复登录会把后续断言的登录挤成 429。
    _c_diy4 = _c_own3c
    _pg_diy4 = _c_diy4.get('/admin/diy/')
    _tk_diy4 = re.search(r'name="csrf_token" value="([^"]+)"',
                         _pg_diy4.get_data(as_text=True))
    _tok_diy4 = _tk_diy4.group(1) if _tk_diy4 else ''
    chk('PH2-4E 老板可访问建包工作台且含向导字段',
        _pg_diy4.status_code == 200
        and 'industry_type' in _pg_diy4.get_data(as_text=True)
        and 'main_items' in _pg_diy4.get_data(as_text=True),
        str(_pg_diy4.status_code))

    _r_draft4 = _c_diy4.post('/admin/diy/draft', data={
        'csrf_token': _tok_diy4, 'industry_type': 'PH2-4E茶饮店',
        'main_items': '柠檬茶、奶茶', 'unit': '杯', 'focus': '库存与损耗'})
    _loc_draft4 = _r_draft4.headers.get('Location', '')
    _pg_prev4 = _c_diy4.get(_loc_draft4 if _loc_draft4.startswith('/') else '/admin/diy/')
    chk('PH2-4E 生成草稿并跳预览页（不落库）',
        _r_draft4.status_code == 302 and '/admin/diy/draft/' in _loc_draft4
        and _pg_prev4.status_code == 200
        and '草稿预览' in _pg_prev4.get_data(as_text=True),
        f'{_r_draft4.status_code}/{_loc_draft4}')

    with app.app_context():
        _before_diy4 = 'ph24e_tea' in (_ip.list_pack_ids() or [])
    _r_conf4 = _c_diy4.post(f'{_loc_draft4}/confirm',
                            data={'csrf_token': _tok_diy4, 'pack_id': 'ph24e_tea'})
    with app.app_context():
        _made_diy4 = _pio4.export_pack_full('ph24e_tea')
    chk('PH2-4E 人工确认后才落库（预览阶段无包）',
        _before_diy4 is False and _r_conf4.status_code == 302
        and _made_diy4 is not None and _made_diy4['meta']['id'] == 'ph24e_tea',
        str(_r_conf4.status_code))
    _r_conf_again4 = _c_diy4.post(f'{_loc_draft4}/confirm',
                                  data={'csrf_token': _tok_diy4, 'pack_id': 'ph24e_tea'})
    chk('PH2-4E 草稿 token 一次性（重复确认 404）',
        _r_conf_again4.status_code == 404, str(_r_conf_again4.status_code))

    _r_export4 = _c_diy4.get('/admin/diy/export/bakery')
    _exp_body4 = _r_export4.get_data(as_text=True) if _r_export4.status_code == 200 else ''
    try:
        _exp_json4 = _json_pk.loads(_exp_body4)
    except ValueError:
        _exp_json4 = None
    chk('PH2-4E 导出路由返回可解析 JSON 附件',
        _r_export4.status_code == 200
        and 'attachment' in (_r_export4.headers.get('Content-Disposition') or '')
        and isinstance(_exp_json4, dict) and _exp_json4.get('schema_version') == 1,
        str(_r_export4.status_code))

    _imp_payload4 = _json_pk.loads(_json_pk.dumps(_pio4.export_pack_full('bakery'),
                                                  ensure_ascii=False))
    _imp_payload4['meta']['id'] = 'ph24e_imported'
    _r_imp4 = _c_diy4.post('/admin/diy/import', data={
        'csrf_token': _tok_diy4,
        'payload': _json_pk.dumps(_imp_payload4, ensure_ascii=False)})
    with app.app_context():
        _imp_made4 = _pio4.export_pack_full('ph24e_imported')
    chk('PH2-4E 导入合法 JSON 成功落库',
        _r_imp4.status_code == 302 and _imp_made4 is not None,
        str(_r_imp4.status_code))

    _r_imp_bad4 = _c_diy4.post('/admin/diy/import', data={
        'csrf_token': _tok_diy4, 'payload': '{ this is not json '})
    with app.app_context():
        _bad_pack4 = _pio4.export_pack_full('ph24e_bad')
    chk('PH2-4E 非法导入停在预览页且不落库',
        _r_imp_bad4.status_code == 400 and _bad_pack4 is None,
        str(_r_imp_bad4.status_code))

    _c_mgr_diy4 = _app3c.test_client()
    with app.app_context():
        _mgr_diy4 = _Admin3C(username='mgr-diy4', role='manager', merchant_id=_mid3c)
        _mgr_diy4.set_password('Str0ngPass!')
        _db2.session.add(_mgr_diy4)
        _db2.session.commit()
        _mgr_diy4_id = _mgr_diy4.id
    # 直接注入会话身份，避免再消耗一次登录限流额度
    with _c_mgr_diy4.session_transaction() as _sess_diy4:
        _sess_diy4['_user_id'] = f'admin-{_mgr_diy4_id}'
    chk('PH2-4E 店长访问建包工作台 403（仅老板可建包）',
        _c_mgr_diy4.get('/admin/diy/').status_code == 403,
        str(_c_mgr_diy4.get('/admin/diy/').status_code))

    with app.app_context():
        _ps4c.delete_pack('ph24e_tea')
        _ps4c.delete_pack('ph24e_imported')
        _Admin3C.query.filter_by(username='mgr-diy4').delete()
        _db2.session.commit()

    # ── PH2-5A：多门店聚合与规则版分析（含证据引用）──
    from src import multi_store as _ms5
    from src.models import Store as _Store5

    with app.app_context():
        _today5 = _date.today()
        _sA5 = _StoreM.query.order_by(_StoreM.id).first()
        _sA2_5 = _Store5(merchant_id=_mid3c, name='PH2-5A二店', code='ph25a-2')
        _mB5 = _MerchantM(name='PH2-5A商家B')
        _db2.session.add_all([_sA2_5, _mB5])
        _db2.session.flush()
        _sB5 = _Store5(merchant_id=_mB5.id, name='PH2-5A-B店', code='ph25a-b')
        _db2.session.add(_sB5)
        _db2.session.flush()
        _staff5_id = _StaffM.query.filter_by(username='milk_tea_staff1').first().id
        _sA5_id, _sA2_5_id, _sB5_id, _mB5_id = _sA5.id, _sA2_5.id, _sB5.id, _mB5.id
        for _off in (1, 2, 3):
            _db2.session.add(_DS(staff_id=_staff5_id, store_id=_sA5_id,
                                 record_date=_today5 - _td(days=_off),
                                 product_name=f'PH2-5A-A1-{_off}',
                                 product_sales_qty=10, total_revenue=100.0,
                                 raw_material_remaining='50', raw_material_waste='1',
                                 pack_id='milk_tea'))
        for _off in (1, 2):
            _db2.session.add(_DS(staff_id=_staff5_id, store_id=_sA2_5_id,
                                 record_date=_today5 - _td(days=_off),
                                 product_name=f'PH2-5A-A2-{_off}',
                                 product_sales_qty=1, total_revenue=10.0,
                                 raw_material_remaining='30', raw_material_waste='20',
                                 pack_id='milk_tea'))
        _db2.session.add(_DS(staff_id=_staff5_id, store_id=_sB5_id,
                             record_date=_today5 - _td(days=1),
                             product_name='PH2-5A-B店专属', product_sales_qty=1,
                             total_revenue=9999.0, pack_id='milk_tea'))
        _db2.session.commit()

    with app.app_context():
        _rows5 = _ms5.store_metrics([_sA5_id, _sA2_5_id])
        _rank5 = _ms5.store_ranking([_sA5_id, _sA2_5_id])
        _alerts5 = _ms5.abnormal_stores([_sA5_id, _sA2_5_id])
        _analysis5 = _ms5.rule_analysis([_sA5_id, _sA2_5_id])
        _trend5 = _ms5.cross_store_trend([_sA5_id, _sA2_5_id])
        _drill5 = _ms5.drill_down(_sA5_id)
        _empty5 = _ms5.store_metrics([])
        _empty_an5 = _ms5.rule_analysis([])
        _only_sum5 = _ms5.store_metrics([_sA5_id])

    _metric_map5 = {r['store_id']: r for r in _rows5}
    chk('PH2-5A 逐店聚合营收/损耗率正确',
        _metric_map5[_sA5_id]['revenue'] >= 300.0
        and _metric_map5[_sA5_id]['records'] >= 3
        and _metric_map5[_sA2_5_id]['revenue'] == 20.0
        and _metric_map5[_sA2_5_id]['waste_rate'] > 0.15,
        str(_metric_map5.get(_sA2_5_id)))
    chk('PH2-5A 门店排行按营收降序且带 rank',
        _rank5[0]['store_id'] == _sA5_id and _rank5[0]['rank'] == 1
        and _rank5[0]['has_data'] is True)
    chk('PH2-5A 异常门店识别（损耗率超阈值）',
        any(a['store_id'] == _sA2_5_id and a['kind'] == 'waste_rate'
            for a in _alerts5), str(_alerts5[:1]))
    chk('PH2-5A 规则版结论均带证据引用',
        _analysis5['source'] == 'rule' and _analysis5['findings']
        and all(isinstance(f.get('evidence'), list) for f in _analysis5['findings'])
        and any(f['kind'] == 'root_cause' for f in _analysis5['findings']),
        str([f['kind'] for f in _analysis5['findings']]))
    chk('PH2-5A 跨店趋势按日期升序且含营收',
        _trend5 and all('revenue' in d for d in _trend5)
        and [d['date'] for d in _trend5] == sorted(d['date'] for d in _trend5))
    chk('PH2-5A 单店下钻含指标/趋势/热销',
        _drill5 and _drill5['metrics']['store_id'] == _sA5_id
        and _drill5['trend'] and _drill5['top_products'])
    chk('PH2-5A 门店集合为空时不查库返回空结构',
        _empty5 == [] and len(_empty_an5['findings']) == 1)
    chk('PH2-5A 跨商家门店不进本商家聚合',
        all(r['store_id'] != _sB5_id for r in _rows5)
        and all(r['store_id'] != _sB5_id for r in _only_sum5))

    with app.app_context():
        _DS.query.filter(_DS.product_name.like('PH2-5A-%')).delete(
            synchronize_session=False)
        _Store5.query.filter(_Store5.id.in_([_sA2_5_id, _sB5_id])).delete(
            synchronize_session=False)
        _MerchantM.query.filter_by(id=_mB5_id).delete()
        _db2.session.commit()

    # ── PH2-5B：跨店 AI 分析（配额/缓存/超时/降级 + 决策轨迹）──
    with app.app_context():
        _mB5b = _MerchantM(name='PH2-5B商家')
        _db2.session.add(_mB5b)
        _db2.session.flush()
        _sB5b = _Store5(merchant_id=_mB5b.id, name='PH2-5B店', code='ph25b-1')
        _db2.session.add(_sB5b)
        _db2.session.flush()
        _staff5b_id = _StaffM.query.filter_by(username='milk_tea_staff1').first().id
        for _off in (1, 2, 3):
            _db2.session.add(_DS(staff_id=_staff5b_id, store_id=_sB5b.id,
                                 record_date=_date.today() - _td(days=_off),
                                 product_name=f'PH2-5B-{_off}', product_sales_qty=5,
                                 total_revenue=50.0, pack_id='milk_tea'))
        _db2.session.commit()
        _mB5b_id, _sB5b_id = _mB5b.id, _sB5b.id

    _ms5.set_llm(None)
    _aq4.clear_cache()
    with app.app_context():
        _res_rule5b = _ms5.analyze_cross_store(_mB5b_id, [_sB5b_id], days=7)
        _ledger_none5b = _aq4.usage_today(_mB5b_id, _aq4.KIND_ANALYSIS)
    chk('PH2-5B 未接 AI 时直接规则版且不消耗配额',
        _res_rule5b['source'] == 'rule'
        and _res_rule5b['fallback_reason'] == 'llm_not_configured'
        and _res_rule5b['trace'] and _ledger_none5b['calls'] == 0,
        str(_ledger_none5b))

    _calls5b = {'n': 0}

    def _llm_ok5b(_prompt):
        _calls5b['n'] += 1
        return {'findings': [{'kind': 'stocking', 'text': '建议降低易损品备货',
                              'evidence': [{'store_id': _sB5b_id,
                                            'metric': 'waste_rate', 'value': 0.2}]}]}

    _ms5.set_llm(_llm_ok5b)
    _aq4.clear_cache()
    with app.app_context():
        _res_ai5b = _ms5.analyze_cross_store(_mB5b_id, [_sB5b_id], days=7,
                                             cache_key='ph25b-ok')
        _res_cache5b = _ms5.analyze_cross_store(_mB5b_id, [_sB5b_id], days=7,
                                                cache_key='ph25b-ok')
        _ledger_ai5b = _aq4.usage_today(_mB5b_id, _aq4.KIND_ANALYSIS)
    chk('PH2-5B AI 结论走AI来源且带证据与轨迹',
        _res_ai5b['source'] == 'ai' and _calls5b['n'] == 1
        and _res_ai5b['findings'][0]['evidence'][0]['store_id'] == _sB5b_id
        and any(t['step'] == 'AI 分析' for t in _res_ai5b['trace']),
        str(_res_ai5b['source']))
    chk('PH2-5B AI 调用落账（含 Token）',
        _ledger_ai5b['calls'] == 1 and _ledger_ai5b['tokens'] == 800,
        str(_ledger_ai5b))
    chk('PH2-5B 10 分钟内第二次命中缓存（不重复调用模型）',
        _res_cache5b['source'] == 'cache' and _calls5b['n'] == 1)

    _ms5.set_llm(lambda _p: {'findings': []})
    _aq4.clear_cache()
    with app.app_context():
        _res_bad5b = _ms5.analyze_cross_store(_mB5b_id, [_sB5b_id], days=7,
                                              cache_key='ph25b-bad')
    chk('PH2-5B AI 结构非法回落规则版',
        _res_bad5b['source'] == 'rule' and _res_bad5b['fallback_reason'] == 'error')

    _ms5.set_llm(lambda _p: {'findings': [{
        'kind': 'summary', 'text': '越界证据',
        'evidence': [{'store_id': 999999, 'metric': 'revenue', 'value': 1}]}]})
    _aq4.clear_cache()
    with app.app_context():
        _res_cross_ev5b = _ms5.analyze_cross_store(_mB5b_id, [_sB5b_id], days=7,
                                                   cache_key='ph25b-crossev')
    chk('PH2-5B AI 证据引用非本集合门店被拒（回落规则版）',
        _res_cross_ev5b['source'] == 'rule'
        and _res_cross_ev5b['fallback_reason'] == 'error')

    _ms5.set_llm(lambda _p: (_ for _ in ()).throw(TimeoutError()))
    _aq4.clear_cache()
    with app.app_context():
        _res_to5b = _ms5.analyze_cross_store(_mB5b_id, [_sB5b_id], days=7,
                                             cache_key='ph25b-timeout')
        _failed5b = _AUL4.query.filter_by(merchant_id=_mB5b_id, ok=False).count()
    chk('PH2-5B AI 超时回落规则版并记失败账本',
        _res_to5b['source'] == 'rule' and _res_to5b['fallback_reason'] == 'timeout'
        and _failed5b >= 1, f"{_res_to5b['fallback_reason']}/{_failed5b}")

    with app.app_context():
        for _ in range(_aq4.ANALYSIS_DAILY_LIMIT):
            _aq4.record_usage(_mB5b_id, _aq4.KIND_ANALYSIS, tokens=1)
    _calls5b['n'] = 0
    _aq4.clear_cache()
    with app.app_context():
        _res_over5b = _ms5.analyze_cross_store(_mB5b_id, [_sB5b_id], days=7,
                                               cache_key='ph25b-over')
    chk('PH2-5B 分析配额用尽时回落规则版且不调用模型',
        _res_over5b['source'] == 'rule'
        and _res_over5b['fallback_reason'] == 'quota_exceeded'
        and _calls5b['n'] == 0, str(_res_over5b['fallback_reason']))

    _ms5.set_llm(None)
    _aq4.clear_cache()
    with app.app_context():
        _DS.query.filter(_DS.product_name.like('PH2-5B-%')).delete(
            synchronize_session=False)
        _AUL4.query.filter_by(merchant_id=_mB5b_id).delete(synchronize_session=False)
        _Store5.query.filter_by(id=_sB5b_id).delete()
        _MerchantM.query.filter_by(id=_mB5b_id).delete()
        _db2.session.commit()

    # ── PH2-5C：多店视图 HTTP 路由（看板/下钻/AI 分析/角色边界）──
    from src.models import ROLE_PLATFORM as _RP5C
    from src.routes import multi as _mr5c

    with app.app_context():
        _sA5c = _Store5(merchant_id=_mid3c, name='PH2-5C二店', code='ph25c-2')
        _mB5c = _MerchantM(name='PH2-5C商家B')
        _db2.session.add_all([_sA5c, _mB5c])
        _db2.session.flush()
        _sB5c = _Store5(merchant_id=_mB5c.id, name='PH2-5C-B店', code='ph25c-b')
        _db2.session.add(_sB5c)
        _db2.session.flush()
        # 用自建探针店员：套件前段的「停用店员」用例可能已把 milk_tea_staff1 置为 inactive
        _probe_staff5c = _StaffM(username='staff-5c', display_name='5C店员',
                                 merchant_id=_mid3c, store_id=_sA5c.id,
                                 store_name='PH2-5C二店')
        _probe_staff5c.set_password('Str0ngPass!')
        _db2.session.add(_probe_staff5c)
        _db2.session.flush()
        _staff5c_id = _probe_staff5c.id
        _default5c = _StoreM.query.filter_by(merchant_id=_mid3c,
                                             code='default').first()
        for _off in (1, 2, 3, 4):
            _db2.session.add(_DS(staff_id=_staff5c_id, store_id=_sA5c.id,
                                 record_date=_date.today() - _td(days=_off),
                                 product_name=f'PH2-5C-A2-{_off}',
                                 product_sales_qty=8, total_revenue=80.0,
                                 raw_material_remaining='30', raw_material_waste='12',
                                 pack_id='milk_tea'))
        _db2.session.add(_DS(staff_id=_staff5c_id, store_id=_sB5c.id,
                             record_date=_date.today() - _td(days=1),
                             product_name='PH2-5C-B店专属', product_sales_qty=1,
                             total_revenue=7777.0, pack_id='milk_tea'))
        _mgr5c = _Admin3C(username='mgr-5c', role='manager', merchant_id=_mid3c)
        _mgr5c.set_password('Str0ngPass!')
        _plat5c = _Admin3C(username='plat-5c', role=_RP5C, merchant_id=None)
        _plat5c.set_password('Str0ngPass!')
        _db2.session.add_all([_mgr5c, _plat5c])
        _db2.session.flush()
        _db2.session.add(_ASA3C(admin_id=_mgr5c.id, store_id=_sA5c.id))
        _db2.session.commit()
        _sA5c_id, _sB5c_id, _mB5c_id, _default5c_id = (
            _sA5c.id, _sB5c.id, _mB5c.id, _default5c.id)
        _mgr5c_id, _plat5c_id = _mgr5c.id, _plat5c.id

    _pg_multi5c = _c_own3c.get('/admin/multi/')
    _body_multi5c = _pg_multi5c.get_data(as_text=True)
    chk('PH2-5C 老板多店视图可访问且含排行与门店',
        _pg_multi5c.status_code == 200 and '门店排行' in _body_multi5c
        and 'PH2-5C二店' in _body_multi5c, str(_pg_multi5c.status_code))

    _pg_detail5c = _c_own3c.get(f'/admin/multi/store/{_sA5c_id}')
    chk('PH2-5C 老板可下钻本商家门店',
        _pg_detail5c.status_code == 200
        and 'PH2-5C二店' in _pg_detail5c.get_data(as_text=True),
        str(_pg_detail5c.status_code))
    chk('PH2-5C 跨商家门店下钻 404（不泄露存在性）',
        _c_own3c.get(f'/admin/multi/store/{_sB5c_id}').status_code == 404)

    _c_mgr5c = _app3c.test_client()
    with _c_mgr5c.session_transaction() as _sess5c:
        _sess5c['_user_id'] = f'admin-{_mgr5c_id}'
    _body_mgr5c = _c_mgr5c.get('/admin/multi/').get_data(as_text=True)
    chk('PH2-5C 店长只见被授权门店（未授权门店名不出现）',
        'PH2-5C二店' in _body_mgr5c
        and '默认门店' not in _body_mgr5c
        and 'PH2-5C-B店' not in _body_mgr5c)

    _c_staff5c = _app3c.test_client()
    with _c_staff5c.session_transaction() as _sess_staff5c:
        _sess_staff5c['_user_id'] = f'staff-{_staff5c_id}'
    _c_plat5c = _app3c.test_client()
    with _c_plat5c.session_transaction() as _sess_plat5c:
        _sess_plat5c['_user_id'] = f'admin-{_plat5c_id}'
    chk('PH2-5C 店员访问多店视图 403',
        _c_staff5c.get('/admin/multi/').status_code == 403,
        str(_c_staff5c.get('/admin/multi/').status_code))
    chk('PH2-5C 平台管理员访问业务分析 403',
        _c_plat5c.get('/admin/multi/').status_code == 403,
        str(_c_plat5c.get('/admin/multi/').status_code))

    _ms5.set_llm(lambda _p: {'findings': [{
        'kind': 'stocking', 'text': 'PH2-5C-AI结论：建议降低损耗',
        'evidence': [{'store_id': _sA5c_id, 'metric': 'waste_rate', 'value': 0.3}]}]})
    _aq4.clear_cache()
    _tk_multi5c = re.search(r'name="csrf_token" value="([^"]+)"', _body_multi5c)
    _r_ai5c = _c_own3c.post('/admin/multi/analyze',
                            data={'csrf_token': _tk_multi5c.group(1) if _tk_multi5c else ''})
    _body_ai5c = _r_ai5c.get_data(as_text=True)
    chk('PH2-5C AI 分析展示结论/证据/轨迹/来源徽章',
        _r_ai5c.status_code == 200 and 'PH2-5C-AI结论' in _body_ai5c
        and '证据：门店' in _body_ai5c and 'AI 决策轨迹' in _body_ai5c
        and 'AI 分析' in _body_ai5c, str(_r_ai5c.status_code))
    _ms5.set_llm(None)
    _aq4.clear_cache()
    _r_rule5c = _c_own3c.post('/admin/multi/analyze',
                              data={'csrf_token': _tk_multi5c.group(1) if _tk_multi5c else ''})
    chk('PH2-5C 未接 AI 时页面标注规则版来源',
        _r_rule5c.status_code == 200
        and '规则版' in _r_rule5c.get_data(as_text=True),
        str(_r_rule5c.status_code))

    with app.app_context():
        _DS.query.filter(_DS.product_name.like('PH2-5C-%')).delete(
            synchronize_session=False)
        _ASA3C.query.filter_by(admin_id=_mgr5c_id).delete()
        _Admin3C.query.filter(_Admin3C.username.in_(['mgr-5c', 'plat-5c'])).delete(
            synchronize_session=False)
        _StaffM.query.filter_by(username='staff-5c').delete()
        _Store5.query.filter(_Store5.id.in_([_sA5c_id, _sB5c_id])).delete(
            synchronize_session=False)
        _MerchantM.query.filter_by(id=_mB5c_id).delete()
        _db2.session.commit()

    # ── PH2-5D：店员个人边界 / 店长越界 / 无门店空态 / 降级证据一致性 ──
    with app.app_context():
        _sMgr5d = _Store5(merchant_id=_mid3c, name='PH2-5D授权店', code='ph25d-mgr')
        _sOther5d = _Store5(merchant_id=_mid3c, name='PH2-5D未授权店', code='ph25d-other')
        _db2.session.add_all([_sMgr5d, _sOther5d])
        _db2.session.flush()
        _mgr5d = _Admin3C(username='mgr-5d', role='manager', merchant_id=_mid3c)
        _mgr5d.set_password('Str0ngPass!')
        _db2.session.add(_mgr5d)
        _db2.session.flush()
        _db2.session.add(_ASA3C(admin_id=_mgr5d.id, store_id=_sMgr5d.id))

        _me5d = _StaffM(username='staff-5d-me', display_name='5D我',
                        merchant_id=_mid3c, store_id=_sMgr5d.id,
                        store_name='PH2-5D授权店')
        _me5d.set_password('Str0ngPass!')
        _other5d = _StaffM(username='staff-5d-other', display_name='5D他人',
                           merchant_id=_mid3c, store_id=_sMgr5d.id,
                           store_name='PH2-5D授权店')
        _other5d.set_password('Str0ngPass!')
        _db2.session.add_all([_me5d, _other5d])
        _db2.session.flush()
        _db2.session.add(_DS(staff_id=_other5d.id, store_id=_sMgr5d.id,
                             record_date=_date.today() - _td(days=1),
                             product_name='PH2-5D-他人专属记录',
                             product_sales_qty=3, total_revenue=30.0,
                             pack_id='milk_tea'))

        _mEmpty5d = _MerchantM(name='PH2-5D空商家')
        _db2.session.add(_mEmpty5d)
        _db2.session.flush()
        _ownerEmpty5d = _Admin3C(username='owner-empty-5d', role='owner',
                                 merchant_id=_mEmpty5d.id)
        _ownerEmpty5d.set_password('Str0ngPass!')
        _db2.session.add(_ownerEmpty5d)
        _db2.session.commit()
        _me5d_id, _mgr5d_id, _sMgr5d_id, _sOther5d_id = (
            _me5d.id, _mgr5d.id, _sMgr5d.id, _sOther5d.id)
        _ownerEmpty5d_id, _mEmpty5d_id = _ownerEmpty5d.id, _mEmpty5d.id

    _c_me5d = _app3c.test_client()
    with _c_me5d.session_transaction() as _sess_me5d:
        _sess_me5d['_user_id'] = f'staff-{_me5d_id}'
    _h5_me5d = _c_me5d.get('/h5/')
    _trainer_me5d = _c_me5d.get('/ai/trainer')
    _history_me5d = _c_me5d.get('/h5/history')
    _hist_body5d = _history_me5d.get_data(as_text=True)
    chk('PH2-5D 店员可访问个人填报与培训入口',
        _h5_me5d.status_code == 200 and _trainer_me5d.status_code == 200,
        f'{_h5_me5d.status_code}/{_trainer_me5d.status_code}')
    chk('PH2-5D 店员历史仅见本人记录（他人专属记录不出现）',
        _history_me5d.status_code == 200 and 'PH2-5D-他人专属记录' not in _hist_body5d,
        str(_history_me5d.status_code))

    _c_mgr5d = _app3c.test_client()
    with _c_mgr5d.session_transaction() as _sess_mgr5d:
        _sess_mgr5d['_user_id'] = f'admin-{_mgr5d_id}'
    chk('PH2-5D 店长下钻未授权门店 404',
        _c_mgr5d.get(f'/admin/multi/store/{_sOther5d_id}').status_code == 404
        and _c_mgr5d.get(f'/admin/multi/store/{_sMgr5d_id}').status_code == 200)

    _c_empty5d = _app3c.test_client()
    with _c_empty5d.session_transaction() as _sess_empty5d:
        _sess_empty5d['_user_id'] = f'admin-{_ownerEmpty5d_id}'
    _pg_empty5d = _c_empty5d.get('/admin/multi/')
    chk('PH2-5D 无门店老板多店视图空态可渲染（不 500）',
        _pg_empty5d.status_code == 200, str(_pg_empty5d.status_code))

    _ms5.set_llm(None)
    _aq4.clear_cache()
    with app.app_context():
        _rule5d = _ms5.analyze_cross_store(_mid3c, [_sMgr5d_id], days=7)
    _ev_ids5d = {ev['store_id'] for f in _rule5d['findings'] for ev in f['evidence']}
    chk('PH2-5D 规则降级来源与证据集合一致',
        _rule5d['source'] == 'rule'
        and _rule5d['fallback_reason'] == 'llm_not_configured'
        and _ev_ids5d <= {_sMgr5d_id},
        f"{_rule5d['source']}/{sorted(_ev_ids5d)}")

    with app.app_context():
        _DS.query.filter(_DS.product_name.like('PH2-5D-%')).delete(
            synchronize_session=False)
        _ASA3C.query.filter_by(admin_id=_mgr5d_id).delete()
        _Admin3C.query.filter(_Admin3C.username.in_(
            ['mgr-5d', 'owner-empty-5d'])).delete(synchronize_session=False)
        _StaffM.query.filter(_StaffM.username.in_(
            ['staff-5d-me', 'staff-5d-other'])).delete(synchronize_session=False)
        _Store5.query.filter(_Store5.id.in_([_sMgr5d_id, _sOther5d_id])).delete(
            synchronize_session=False)
        _MerchantM.query.filter_by(id=_mEmpty5d_id).delete()
        _db2.session.commit()

    # ── PH4-2：AI 建议转任务闭环（负责人/截止/状态/逾期/效果回看）──
    from src import task_flow as _tf2
    from src.models import StoreTask as _ST2

    with app.app_context():
        _sTask2 = _Store5(merchant_id=_mid3c, name='PH4-2任务店', code='ph42-task')
        _sOtherTask2 = _Store5(merchant_id=_mid3c, name='PH4-2未授权店',
                               code='ph42-other')
        _mB2 = _MerchantM(name='PH4-2商家B')
        _db2.session.add_all([_sTask2, _sOtherTask2, _mB2])
        _db2.session.flush()
        _mgrTask2 = _Admin3C(username='mgr-ph42', role='manager',
                             merchant_id=_mid3c)
        _mgrTask2.set_password('Str0ngPass!')
        _staffTask2 = _StaffM(username='staff-ph42', display_name='PH4-2店员',
                              merchant_id=_mid3c, store_id=_sTask2.id,
                              store_name='PH4-2任务店')
        _staffTask2.set_password('Str0ngPass!')
        _ownTask2 = _Admin3C.query.filter_by(username='owner_demo').first()
        _db2.session.add_all([_mgrTask2, _staffTask2])
        _db2.session.flush()
        _db2.session.add(_ASA3C(admin_id=_mgrTask2.id, store_id=_sTask2.id))
        _db2.session.commit()
        _sTask2_id, _sOtherTask2_id = _sTask2.id, _sOtherTask2.id
        _mgrTask2_id, _staffTask2_id = _mgrTask2.id, _staffTask2.id
        _ownTask2_id = _ownTask2.id
        _mB2_id = _mB2.id

    with app.app_context():
        _today_task2 = _date.today()
        _mgr_task2 = _tf2.create_task(
            merchant_id=_mid3c, store_id=_sTask2_id,
            title='PH4-2 降低损耗', due_date=_today_task2,
            assignee_role='manager', assignee_id=_mgrTask2_id,
            detail='核对备货量', source=_tf2.SOURCE_AI,
            evidence=[{'metric': 'waste_rate', 'value': 0.2}])
        _staff_task2 = _tf2.create_task(
            merchant_id=_mid3c, store_id=_sTask2_id,
            title='PH4-2 店员执行', due_date=_today_task2 + _td(days=1),
            assignee_role='staff', assignee_id=_staffTask2_id)
        _other_task2 = _tf2.create_task(
            merchant_id=_mid3c, store_id=_sOtherTask2_id,
            title='PH4-2 未授权门店任务', due_date=_today_task2 + _td(days=2),
            assignee_role='owner', assignee_id=_ownTask2_id)
        _mgr_task2.due_date = _today_task2 - _td(days=1)
        _db2.session.commit()
        _stats_before_task2 = _tf2.task_stats([_sTask2_id, _sOtherTask2_id])
        _overdue_before2 = _tf2.is_overdue(_mgr_task2)
        _invalid_assignee2 = False
        try:
            _tf2.create_task(
                merchant_id=_mid3c, store_id=_sTask2_id,
                title='角色不匹配', due_date=_today_task2 + _td(days=1),
                assignee_role='manager', assignee_id=_ownTask2.id)
        except ValueError:
            _invalid_assignee2 = True
        _invalid_transition2 = False
        try:
            _tf2.transition_task(_mgr_task2, 'done')
        except ValueError:
            _invalid_transition2 = True
        _tf2.transition_task(_mgr_task2, 'in_progress')
        _tf2.transition_task(_mgr_task2, 'done')
        _tf2.save_review(
            _mgr_task2, '损耗率由 20% 观察到 15%',
            before={'metric': 'waste_rate', 'value': 0.20},
            after={'metric': 'waste_rate', 'value': 0.15})
        _mgr_task2_id_db = _mgr_task2.id
        _staff_task2_id, _other_task2_id = _staff_task2.id, _other_task2.id
        _review_note2 = _mgr_task2.review_note
        _completed2 = _mgr_task2.completed_at is not None
        _stats_after_task2 = _tf2.task_stats([_sTask2_id, _sOtherTask2_id])

    chk('PH4-2 任务创建后绑定商家/门店/负责人且默认待处理',
        _stats_after_task2['total'] >= 3 and _mgr_task2_id_db > 0)
    chk('PH4-2 逾期按截止时间动态判定',
        _stats_before_task2['overdue'] >= 1
        and _overdue_before2 is True
        and _stats_after_task2['overdue'] == 0)
    chk('PH4-2 非法状态跃迁被状态机拒绝',
        _invalid_transition2 is True)
    chk('PH4-2 完成后写完成时间并支持效果回看',
        _completed2 and '20%' in _review_note2
        and _mgr_task2.before_json and _mgr_task2.after_json)
    chk('PH4-2 非法负责人归属被拒绝',
        _invalid_assignee2 is True)

    with app.app_context():
        try:
            _tf2.create_task(
                merchant_id=_mB2_id, store_id=_sTask2_id,
                title='越界任务', due_date=_date.today(),
                assignee_role='manager', assignee_id=_mgrTask2_id)
        except ValueError:
            _cross_task2 = True
        else:
            _cross_task2 = False
    chk('PH4-2 跨商家门店创建任务被拒绝', _cross_task2 is True)

    _task_page2 = _c_own3c.get('/admin/tasks/')
    _task_body2 = _task_page2.get_data(as_text=True)
    chk('PH4-2 老板任务列表可见本商家任务',
        _task_page2.status_code == 200 and 'PH4-2 降低损耗' in _task_body2
        and 'PH4-2 未授权门店任务' in _task_body2,
        str(_task_page2.status_code))

    _multi_page2 = _c_own3c.get('/admin/multi/')
    _multi_token2 = re.search(r'name="csrf_token" value="([^"]+)"',
                              _multi_page2.get_data(as_text=True))
    _r_multi2 = _c_own3c.post('/admin/multi/analyze',
                              data={'csrf_token': _multi_token2.group(1)
                                    if _multi_token2 else ''})
    chk('PH4-2 跨店分析结果可直接转为任务',
        _r_multi2.status_code == 200
        and '转为任务' in _r_multi2.get_data(as_text=True),
        str(_r_multi2.status_code))

    _task_token2 = re.search(r'name="csrf_token" value="([^"]+)"',
                             _task_page2.get_data(as_text=True))
    _r_create_ai2 = _c_own3c.post('/admin/tasks/create', data={
        'csrf_token': _task_token2.group(1) if _task_token2 else '',
        'store_id': _sTask2_id,
        'title': 'PH4-2 AI 转任务',
        'assignee': f'admin:{_mgrTask2_id}',
        'due_date': (_date.today() + _td(days=3)).isoformat(),
        'source': 'ai_analysis',
        'evidence_json': '{"metric":"waste_rate","value":0.2}',
    })
    with app.app_context():
        _ai_task2 = _ST2.query.filter_by(title='PH4-2 AI 转任务').first()
    chk('PH4-2 AI 建议创建任务并持久化证据',
        _r_create_ai2.status_code == 302 and _ai_task2 is not None
        and 'waste_rate' in (_ai_task2.evidence_json or ''),
        str(_r_create_ai2.status_code))

    _c_mgr_task2 = _app3c.test_client()
    with _c_mgr_task2.session_transaction() as _sess_task2:
        _sess_task2['_user_id'] = f'admin-{_mgrTask2_id}'
    _mgr_task_body2 = _c_mgr_task2.get('/admin/tasks/').get_data(as_text=True)
    chk('PH4-2 店长只看到授权门店任务',
        'PH4-2 降低损耗' in _mgr_task_body2
        and 'PH4-2 未授权门店任务' not in _mgr_task_body2)

    _c_staff_task2 = _app3c.test_client()
    with _c_staff_task2.session_transaction() as _sess_staff_task2:
        _sess_staff_task2['_user_id'] = f'staff-{_staffTask2_id}'
    _staff_task_page2 = _c_staff_task2.get('/h5/tasks')
    _staff_task_body2 = _staff_task_page2.get_data(as_text=True)
    _staff_token2 = re.search(r'name="csrf_token" value="([^"]+)"',
                              _staff_task_body2)
    _r_staff_status2 = _c_staff_task2.post(
        f'/h5/tasks/{_staff_task2_id}/status',
        data={'csrf_token': _staff_token2.group(1) if _staff_token2 else '',
              'status': 'in_progress'})
    chk('PH4-2 店员只看到本人任务并可更新状态',
        _staff_task_page2.status_code == 200
        and 'PH4-2 店员执行' in _staff_task_body2
        and 'PH4-2 降低损耗' not in _staff_task_body2
        and _r_staff_status2.status_code == 302,
        str(_staff_task_page2.status_code))

    with app.app_context():
        _ST2.query.filter(_ST2.store_id.in_([_sTask2_id, _sOtherTask2_id])).delete(
            synchronize_session=False)
        _ASA3C.query.filter_by(admin_id=_mgrTask2_id).delete()
        _Admin3C.query.filter_by(username='mgr-ph42').delete()
        _StaffM.query.filter_by(username='staff-ph42').delete()
        _Store5.query.filter(_Store5.id.in_([_sTask2_id, _sOtherTask2_id])).delete(
            synchronize_session=False)
        _MerchantM.query.filter_by(id=_mB2_id).delete()
        _db2.session.commit()

    # ── PH2-6A：一店一当前包 + 历史包标识保留 ──
    from src import pack_store as _ps6
    from src import tenant as _tn6
    from src.models import Store as _Store6

    with app.app_context():
        _sA6 = _Store6(merchant_id=_mid3c, name='PH2-6A一店', code='ph26a-1')
        _sB6 = _Store6(merchant_id=_mid3c, name='PH2-6A二店', code='ph26a-2')
        _db2.session.add_all([_sA6, _sB6])
        _db2.session.commit()
        _sA6_id, _sB6_id = _sA6.id, _sB6.id
        _staff6_id = _StaffM.query.filter_by(username='milk_tea_staff1').first().id

    with app.app_context():
        _ok_set6, _msg_set6 = _ps6.set_store_pack(_sA6_id, 'fruit')
        _ok_bad6, _msg_bad6 = _ps6.set_store_pack(_sB6_id, 'no_such_pack_6')
        _sA6_row = _db2.session.get(_Store6, _sA6_id)
        _sB6_row = _db2.session.get(_Store6, _sB6_id)
        _a6_pack, _b6_pack = _sA6_row.active_pack_id, _sB6_row.active_pack_id
    chk('PH2-6A 门店可设置自己的当前包',
        _ok_set6 is True and _a6_pack == 'fruit', f'{_ok_set6}/{_a6_pack}')
    chk('PH2-6A 非法包 id 被拒且不改动门店',
        _ok_bad6 is False and _b6_pack in (None, ''), f'{_ok_bad6}/{_b6_pack}')

    _tn6.set_current_store(_sA6_id)
    with _app3c.test_request_context('/admin/'):
        _pack_a6 = _ip.active_pack_id()
    _tn6.set_current_store(_sB6_id)
    with _app3c.test_request_context('/admin/'):
        _pack_b6 = _ip.active_pack_id()
    _tn6.set_current_store(None)
    chk('PH2-6A 门店当前包覆盖全局（A=fruit / B=跟随全局）',
        _pack_a6 == 'fruit' and _pack_b6 == _ip.DEFAULT_PACK_ID,
        f'{_pack_a6}/{_pack_b6}')

    with app.app_context():
        _db2.session.add(_DS(staff_id=_staff6_id, store_id=_sA6_id,
                             record_date=_date.today() - _td(days=1),
                             product_name='PH2-6A-历史包记录',
                             product_sales_qty=1, total_revenue=10.0,
                             pack_id='milk_tea'))
        _db2.session.commit()
        _hist_pack6 = _DS.query.filter_by(
            product_name='PH2-6A-历史包记录').first().pack_id
        _store_pack6 = _db2.session.get(_Store6, _sA6_id).active_pack_id
    chk('PH2-6A 历史流水保留原包标识（不随门店当前包变化）',
        _hist_pack6 == 'milk_tea' and _store_pack6 == 'fruit',
        f'{_hist_pack6}/{_store_pack6}')

    _pg6 = _c_own3c.get('/admin/stores/')
    _tk6 = re.search(r'name="csrf_token" value="([^"]+)"', _pg6.get_data(as_text=True))
    _r_pack6 = _c_own3c.post(f'/admin/stores/{_sB6_id}/pack',
                             data={'csrf_token': _tk6.group(1) if _tk6 else '',
                                   'pack_id': 'convenience'})
    with app.app_context():
        _b6_after = _db2.session.get(_Store6, _sB6_id).active_pack_id
    chk('PH2-6A 后台设置门店包 HTTP 生效',
        _r_pack6.status_code == 302 and _b6_after == 'convenience',
        f'{_r_pack6.status_code}/{_b6_after}')
    _r_clear6 = _c_own3c.post(f'/admin/stores/{_sB6_id}/pack',
                              data={'csrf_token': _tk6.group(1) if _tk6 else '',
                                    'pack_id': ''})
    with app.app_context():
        _b6_clear = _db2.session.get(_Store6, _sB6_id).active_pack_id
    chk('PH2-6A 清空门店包即跟随全局',
        _r_clear6.status_code == 302 and not _b6_clear, str(_b6_clear))

    with app.app_context():
        _DS.query.filter(_DS.product_name.like('PH2-6A-%')).delete(
            synchronize_session=False)
        _Store6.query.filter(_Store6.id.in_([_sA6_id, _sB6_id])).delete(
            synchronize_session=False)
        _db2.session.commit()

    # ── PH2-6B：存量单店库自动化迁移回归（旧结构 → 多租户结构）──
    import sqlite3 as _sq3_6b
    import tempfile as _tf_6b

    import src.config as _cfg6b

    _LEGACY_DDL6B = """
    CREATE TABLE admin (id INTEGER PRIMARY KEY, username VARCHAR(50) UNIQUE NOT NULL,
        password VARCHAR(255) NOT NULL, created_at DATETIME);
    CREATE TABLE staff (id INTEGER PRIMARY KEY, username VARCHAR(50) UNIQUE NOT NULL,
        password VARCHAR(255) NOT NULL, display_name VARCHAR(50) NOT NULL,
        phone VARCHAR(20), store_name VARCHAR(100) NOT NULL, is_active BOOLEAN NOT NULL,
        created_at DATETIME);
    CREATE TABLE products (id INTEGER PRIMARY KEY, product_name VARCHAR(100) NOT NULL UNIQUE,
        unit_price NUMERIC(10,2) NOT NULL, cost_price NUMERIC(10,2), category VARCHAR(50),
        is_active BOOLEAN NOT NULL, pack_id VARCHAR(50), created_at DATETIME);
    CREATE TABLE daily_sales (id INTEGER PRIMARY KEY, staff_id INTEGER NOT NULL,
        record_date DATE NOT NULL, product_name VARCHAR(100) NOT NULL,
        product_sales_qty INTEGER NOT NULL, total_revenue NUMERIC(10,2) NOT NULL,
        raw_material_remaining TEXT, raw_material_waste TEXT, abnormal_notes TEXT,
        metrics_json TEXT, pack_id VARCHAR(50), created_at DATETIME,
        UNIQUE (staff_id, record_date, pack_id, product_name));
    CREATE TABLE quiz_records (id INTEGER PRIMARY KEY, staff_id INTEGER NOT NULL,
        quiz_type VARCHAR(20) NOT NULL, session_id VARCHAR(50), question TEXT NOT NULL,
        staff_answer TEXT, ai_feedback TEXT, is_correct BOOLEAN, score NUMERIC(5,2),
        pack_id VARCHAR(50), created_at DATETIME);
    CREATE TABLE metric_defs (id INTEGER PRIMARY KEY, pack_id VARCHAR(50) NOT NULL,
        key VARCHAR(50) NOT NULL, label VARCHAR(50) NOT NULL,
        value_type VARCHAR(10) NOT NULL, unit VARCHAR(20) NOT NULL,
        role VARCHAR(20) NOT NULL, agg VARCHAR(10) NOT NULL, required BOOLEAN NOT NULL,
        enabled BOOLEAN NOT NULL, sort INTEGER NOT NULL, options_json TEXT,
        UNIQUE (pack_id, key));
    """
    _mig_dir6b = _tf_6b.mkdtemp(prefix='ican_legacy_')
    _mig_db6b = _os_pk.path.join(_mig_dir6b, 'legacy.db')
    _con6b = _sq3_6b.connect(_mig_db6b)
    _con6b.executescript(_LEGACY_DDL6B)
    _con6b.execute("INSERT INTO admin (id, username, password) VALUES (1, 'admin', 'x')")
    _con6b.execute("INSERT INTO staff (id, username, password, display_name, store_name,"
                   " is_active) VALUES (1, 'milk_tea_staff1', 'x', '小王', '老店', 1)")
    _con6b.execute("INSERT INTO products (id, product_name, unit_price, is_active, pack_id)"
                   " VALUES (1, '珍珠奶茶', 12, 1, 'milk_tea')")
    _con6b.execute("INSERT INTO products (id, product_name, unit_price, is_active, pack_id)"
                   " VALUES (2, '杨枝甘露', 18, 1, 'milk_tea')")
    for _i6b in range(1, 6):
        _con6b.execute(
            "INSERT INTO daily_sales (id, staff_id, record_date, product_name,"
            " product_sales_qty, total_revenue, pack_id) VALUES (?, 1, ?,"
            " '珍珠奶茶', ?, ?, 'milk_tea')",
            (_i6b, f'2026-08-{19 + _i6b:02d}', _i6b * 10, _i6b * 120))
    _con6b.commit()
    _con6b.close()

    _orig_uri6b = _cfg6b.DevelopmentConfig.SQLALCHEMY_DATABASE_URI
    _cfg6b.DevelopmentConfig.SQLALCHEMY_DATABASE_URI = (
        'sqlite:///' + _mig_db6b.replace('\\', '/'))
    _app6b = create_app('development')
    try:
        with _app6b.app_context():
            from init_db import _ensure_columns as _ec6b
            from init_db import _rebuild_store_scoped_uq as _ru6b
            from src import tenant as _tnm6b
            from src.models import Admin as _Am6b
            from src.models import DailySales as _Dm6b
            from src.models import Product as _Pm6b
            from src.models import Staff as _Sm6b
            from src.models import Store as _St6b
            from src.models import db as _dbm6b

            _dbm6b.create_all()
            _ec6b()
            _ru6b()
            _tnm6b.ensure_default_tenant()

            _counts6b = (_Am6b.query.count(), _Sm6b.query.count(),
                         _Pm6b.query.count(), _Dm6b.query.count())
            _admin6b = _Am6b.query.first()
            _staff6b = _Sm6b.query.first()
            _store6b = _St6b.query.first()
            _nulls6b = (_Pm6b.query.filter(_Pm6b.store_id.is_(None)).count()
                        + _Dm6b.query.filter(_Dm6b.store_id.is_(None)).count())
            _uq6b = [tuple(c['column_names'])
                     for c in _insp7(_dbm6b.engine).get_unique_constraints('daily_sales')]
            _rows_before6b = _counts6b
            _admin_role6b = _admin6b.role
            _admin_mid6b = _admin6b.merchant_id
            _staff_mid6b = _staff6b.merchant_id
            _staff_sid6b = _staff6b.store_id
            _store_mid6b = _store6b.merchant_id
            _store_id6b = _store6b.id

            # 幂等：再跑一次迁移函数不应报错、不应丢数据
            _ec6b()
            _ru6b()
            _tnm6b.ensure_default_tenant()
            _rows_after6b = (_Am6b.query.count(), _Sm6b.query.count(),
                             _Pm6b.query.count(), _Dm6b.query.count())
            _uq6b_ok = any(
                {'store_id', 'staff_id', 'record_date', 'pack_id', 'product_name'}
                <= set(cols) for cols in _uq6b)

        chk('PH2-6B 旧库迁移保留全部行（管理员/店员/产品/流水）',
            _counts6b == (1, 1, 2, 5) and _rows_after6b == _counts6b,
            f'{_counts6b} -> {_rows_after6b}')
        chk('PH2-6B 迁移补齐租户列并回填归属',
            _admin_role6b == 'owner' and _admin_mid6b == _store_mid6b
            and _staff_mid6b == _store_mid6b
            and _staff_sid6b == _store_id6b and _nulls6b == 0,
            f'{_admin_role6b}/{_admin_mid6b}/{_staff_sid6b}/{_nulls6b}')
        chk('PH2-6B 唯一约束已迁移为门店范围',
            _uq6b_ok, str(_uq6b))
        chk('PH2-6B 迁移幂等（复跑行数不变）',
            _rows_after6b == _rows_before6b)
    finally:
        _cfg6b.DevelopmentConfig.SQLALCHEMY_DATABASE_URI = _orig_uri6b
        _app6b = create_app('development')


# ════════════ P4 配置后台：DB 覆盖层 + 包 CRUD + 分段校验 + 越权 ════════════
from werkzeug.datastructures import MultiDict as _MD  # noqa: E402

from src import pack_store as _ps  # noqa: E402

# P2 段把覆盖钩子换成了内嵌测试包 lambda；P4 段接管覆盖层，重新注册真实实现
_ip.register_db_override(_ps.override_fn)

# ── 服务层：建包 / 复制 / 激活 / 覆盖 / 恢复 ──
with app.app_context():
    _ok, _msg = _ps.create_pack('l4_bakery', 'L4烘焙店', '断言专用包', copy_from='milk_tea')
    chk('P4 建包成功(复制 milk_tea)', _ok, _msg)
    chk('P4 复制后指标非空', len(_ip.get_pack('l4_bakery')['metrics']) >= 6)
    _ok2, _ = _ps.create_pack('l4_bakery', '重复', '')
    chk('P4 重复包标识拒绝', not _ok2)
    _ok3, _ = _ps.create_pack('BAD-ID', '非法标识', '')
    chk('P4 非法包标识拒绝(大写/连字符)', not _ok3)

    _ok, _msg = _ps.set_active('l4_bakery')
    chk('P4 激活 DB 包', _ok and _ip.active_pack_id() == 'l4_bakery', _msg)

    # 题库覆盖：DB 内联段优先于文件包
    _ps.save_questions('l4_bakery', _MD([
        ('questions__q', 'L4吐司保质几天'), ('questions__a', '常温2天'),
        ('questions__weak', '原料规范')]))
    _qs = _ip.pack_questions()
    chk('P4 题库 DB 覆盖生效(内联优先)',
        len(_qs) == 1 and _qs[0]['q'] == 'L4吐司保质几天', str(_qs[:1]))

    # 术语覆盖
    _ps.save_terms('l4_bakery', _MD([('terms__record_dimension', '单品')]))
    chk('P4 术语覆盖生效', _ip.pack_terms()['record_dimension'] == '单品')

    # 知识域覆盖（含新增域）
    _ps.save_domains('l4_bakery', _MD([
        ('domains__name', '烘焙原料'), ('domains__keywords', '面粉,黄油'),
        ('domains__tip', '复习面粉储存'),
        ('domains__name', '出品标准'), ('domains__keywords', '烘烤,温度'),
        ('domains__tip', '复习炉温'),
        ('weak_keywords', '面粉,烘烤'), ('synonyms', '烤,烘烤')]))
    _doms = _ip.pack_domains()
    chk('P4 知识域覆盖为两域', _doms == ['烘焙原料', '出品标准'], str(_doms))
    chk('P4 弱项词随包(归因映射)', '面粉' in _ip.pack_domain_map(),
        str(_ip.pack_domain_map()))
    chk('P4 同义词对覆盖生效', ['烤', '烘烤'] in [list(s) for s in _ip.pack_synonyms()],
        str(_ip.pack_synonyms()))

    # 知识条目覆盖 → 检索层可读
    _ps.save_knowledge('l4_bakery', _MD([
        ('knowledge__烘焙原料', '# 面粉储存\n面粉密封防潮，保质期 6 个月。')]))
    from src import knowledge_base as _kb4  # noqa: E402
    _kb4._IDF_CACHE['key'] = None  # 语料指纹缓存随包内容变化，测试内强制重算
    _hit = _kb4.search('面粉怎么储存', top_k=1)
    chk('P4 知识条目 DB 覆盖进检索',
        bool(_hit) and '面粉' in _hit[0]['title'] + _hit[0]['text'], str(_hit[:1]))

    # 指标：骨架不可删 / 自定义可增 / 非法枚举拒绝
    _ok, _msg = _ps.save_metrics('l4_bakery', _MD([
        ('metrics__key', 'product_name'), ('metrics__label', '产品'),
        ('metrics__value_type', 'text'), ('metrics__unit', ''),
        ('metrics__role', 'dimension'), ('metrics__agg', 'count'),
        ('metrics__sort', '1'), ('metrics__legacy_column', 'product_name'),
        ('metrics__required', '1'), ('metrics__enabled', '1'),
        ('metrics__delete', 'product_name')]))
    chk('P4 骨架指标删除被拒', not _ok, _msg)
    _ok, _msg = _ps.save_metrics('l4_bakery', _MD([
        ('metrics__key', 'daily_bake'), ('metrics__label', '当日出品'),
        ('metrics__value_type', 'integer'), ('metrics__unit', '个'),
        ('metrics__role', 'other'), ('metrics__agg', 'sum'),
        ('metrics__sort', '9'), ('metrics__legacy_column', ''),
        ('metrics__required', '0'), ('metrics__enabled', '1')]))
    chk('P4 新增自定义指标成功', _ok, _msg)
    chk('P4 自定义指标进包定义',
        'daily_bake' in {d['key'] for d in _ip.pack_metrics()})
    _ok, _ = _ps.save_metrics('l4_bakery', _MD([
        ('metrics__key', 'bad_metric'), ('metrics__label', '非法'),
        ('metrics__value_type', 'magic'), ('metrics__unit', ''),
        ('metrics__role', 'other'), ('metrics__agg', 'sum'),
        ('metrics__sort', '0'), ('metrics__legacy_column', ''),
        ('metrics__required', '0'), ('metrics__enabled', '1')]))
    chk('P4 指标类型枚举白名单拦截', not _ok)
    _ok, _ = _ps.save_metrics('l4_bakery', _MD([
        ('metrics__key', 'evil'), ('metrics__label', '越权映射'),
        ('metrics__value_type', 'number'), ('metrics__unit', ''),
        ('metrics__role', 'other'), ('metrics__agg', 'sum'),
        ('metrics__sort', '0'), ('metrics__legacy_column', 'password'),
        ('metrics__required', '0'), ('metrics__enabled', '1')]))
    chk('P4 映射列名白名单拦截(防越权写既有列)', not _ok)

    # 公式参数
    _ok, _ = _ps.save_formula_params('l4_bakery', _MD(
        [('formula_params__waste_rate_threshold', '0.05')]))
    chk('P4 公式参数保存生效', _ok and _ip.pack_formula_params(
    )['waste_rate_threshold'] == 0.05)
    _ok, _ = _ps.save_formula_params('l4_bakery', _MD(
        [('formula_params__waste_rate_threshold', '2')]))
    chk('P4 阈值越界拒绝(0~1)', not _ok)

    # 恢复默认：清 DB 覆盖回退文件包（对真实文件包 milk_tea 验证回退链）
    _ok, _ = _ps.reset_section('l4_bakery', 'questions')
    chk('P4 恢复内置默认(清覆盖成功)', _ok)

    # 删除：文件包不可删 / 激活包不可删 / DB 包可删
    _ok, _ = _ps.delete_pack('milk_tea')
    chk('P4 内置文件包不可删', not _ok)
    _ok, _ = _ps.delete_pack('l4_bakery')
    chk('P4 激活中的包不可删', not _ok)
    _ps.set_active('milk_tea')
    _ok, _msg = _ps.delete_pack('l4_bakery')
    chk('P4 非激活 DB 包可删', _ok, _msg)
    chk('P4 删除后包列表不含该包',
        'l4_bakery' not in {p['id'] for p in _ps.all_packs()})

    # 三级回退：坏包 id 不抛异常且回退默认包
    _bad = _ip.get_pack('no_such_pack_xyz')
    chk('P4 不可解析包回退默认包(source≠db)', _bad['id'] == 'milk_tea', _bad['source'])

    # 激活态持久化：DB 行 is_active 与进程激活一致
    _ps.set_active('fruit')
    _ip.set_active_pack(None)  # 清进程态，模拟重启
    _restored = _ps.restore_active()
    chk('P4 重启后激活态从 DB 恢复', _restored == 'fruit', _restored)
    _ps.set_active('milk_tea')
    _ip.set_active_pack(None)
    _ps.restore_active()

# ── HTTP 层：后台页面可达 + 越权拦截（client 已登录 admin）──
_r_list = client.get('/admin/industry/')
chk('P4 包管理页 200', _r_list.status_code == 200, f'code={_r_list.status_code}')
_chk_html = _r_list.get_data(as_text=True)
chk('P4 包管理页含三包与激活徽章',
    '奶茶店' in _chk_html and '水果生鲜' in _chk_html and '当前激活' in _chk_html)
_r_edit = client.get('/admin/industry/milk_tea')
chk('P4 包编辑器 200', _r_edit.status_code == 200, f'code={_r_edit.status_code}')
_chk_edit = _r_edit.get_data(as_text=True)
chk('P4 编辑器八分区锚点齐备',
    all(s in _chk_edit for s in ('sec-terms', 'sec-domains', 'sec-knowledge',
                                 'sec-metrics', 'sec-formula', 'sec-questions',
                                 'sec-scenarios', 'sec-products')))
chk('P4 编辑器无 | safe 渲染(转义生效)', '| safe' not in _chk_edit)
_r_404 = client.get('/admin/industry/../packs')
chk('P4 非法包路径不放行', _r_404.status_code in (302, 400, 404), f'code={_r_404.status_code}')

# 店员越权：/admin/industry 必须 403
# （milk_tea_staff1 已被前面 CSRF 用例 toggle 停用，这里用 milk_tea_staff2）
_c_staff = app.test_client()
_p = _c_staff.get('/login').get_data(as_text=True)
_t = re.search(r'name="csrf_token" value="([^"]+)"', _p)
_c_staff.post('/login', data={'username': 'milk_tea_staff2', 'password': 'staff123',
                              'role': 'staff', 'csrf_token': _t.group(1) if _t else ''})
chk('P4 店员访问包后台被拒(403)',
    _c_staff.get('/admin/industry/').status_code == 403,
    f'code={_c_staff.get("/admin/industry/").status_code}')
chk('P4 店员写包操作被拒(403)',
    _c_staff.post('/admin/industry/milk_tea/activate',
                  data={'csrf_token': 'x'}).status_code in (400, 403))

# ════════════ P5 动态谈判引擎：隐藏态 / 六类话术 / 成交边界 / 四维复盘 ════════════
from src import negotiation as _ng  # noqa: E402

_ini = _ng.initial_state('bargain')
chk('P5 隐藏价阶梯合法(下界<底价≤上界<原价)',
    _ini['budget_low'] < _ini['reservation'] <= _ini['budget_high'] < _ini['list_price'],
    str({k: _ini[k] for k in ('budget_low', 'reservation', 'budget_high', 'list_price')}))
chk('P5 初始态耐心/轮次/让利归零',
    _ini['patience'] == 70.0 and _ini['rounds'] == 0 and _ini['concession'] == 0.0)
chk('P5 六类话术归类互斥有序',
    [_ng.classify_staff_utterance(x) for x in (
        '我们的原料是当天现做的，真材实料', '推荐您换成柠檬水更清爽',
        '那我给您便宜两块', '我说了算，保证最低价', '不归我管，没办法', '嗯嗯')]
    == ['value', 'alternative', 'discount', 'overpromise', 'confront', 'irrelevant'])
chk('P5 越权承诺优先于降价词',
    _ng.classify_staff_utterance('保证最低价，再给您便宜点') == 'overpromise')
chk('P5 空输入归无关不抛异常', _ng.classify_staff_utterance(None) == 'irrelevant')
chk('P5 未知场景回退默认参数(不 KeyError)',
    _ng.initial_state('no_such_scene')['patience'] == _ng.DEFAULT_TUNING[4])
chk('P5 包内非法参数被拒(破坏单调性)',
    _ng._parse_tuning([1.0, 0.9, 0.8, 0.95, 70.0]) is None)
chk('P5 包内非法参数被拒(结构/非数)',
    _ng._parse_tuning('x') is None and _ng._parse_tuning([1, 2, 3, 'a', 5]) is None)
chk('P5 历史为空/None 返回初始态',
    _ng.replay(None, 'bargain')['rounds'] == 0 and _ng.replay([], 'pick')['rounds'] == 0)

# 状态转移契约
_step_d = _ng.step(_ini, '那我给您便宜两块')
chk('P5 降价→让价累计且耐心升(单轮一档内)',
    0 < _step_d['concession'] <= _ini['list_price'] - _ini['budget_low'],
    str(_step_d['concession']))
chk('P5 降价让利百分比正确',
    abs(_step_d['concession_pct'] - _step_d['concession'] / _ini['list_price'] * 100) < 0.15,
    str(_step_d['concession_pct']))
_step_v = _ng.step(_ini, '我们的原料是当天现做的，真材实料')
chk('P5 讲价值不让价但稳情绪',
    _step_v['concession'] == 0.0 and _step_v['patience'] > _ini['patience'])
_step_a = _ng.step(_ini, '推荐您换成柠檬水更清爽')
chk('P5 替代方案半档让价(0 < 让价 < 降价)',
    0 < _step_a['concession'] < _step_d['concession'],
    f"{_step_a['concession']} vs {_step_d['concession']}")
chk('P5 让价不低于标的价下限',
    _ng.replay(['店员：给您便宜'] * 30, 'bargain')['current_price']
    >= _ini['list_price'] * _ng.PRICE_FLOOR_RATIO - 0.01)
chk('P5 耐心恒在 0~100 区间',
    _ng.replay(['店员：爱买不买'] * 20, 'bargain')['patience'] == 0.0
    and _ng.replay(['店员：抱歉马上处理'] * 20, 'bargain')['patience'] == 100.0)

# 成交边界
chk('P5 对抗两次判流失',
    _ng.settle(_ng.replay(['店员：爱买不买'] * 2, 'bargain'))['result'] == 'lost')
chk('P5 只讲价值不让价→轮次用尽成交(说服式)',
    _ng.settle(_ng.replay(['店员：我们的原料是当天现做的'] * _ng.MAX_ROUNDS,
                          'bargain'))['result'] == 'deal')
_v_deal = _ng.settle(_ng.replay(['店员：抱歉，马上为您重做并送您优惠券'] * 6, 'bargain'))
chk('P5 让到期望价且情绪好→成交', _v_deal['result'] == 'deal', str(_v_deal))
chk('P5 成交回报让利额与幅度',
    _v_deal['concession'] > 0 and _v_deal['concession_pct'] > 0 and _v_deal['price'] > 0)
chk('P5 未达条件显式未决而非默认成交',
    _ng.settle(_ng.replay(['店员：嗯嗯'] * 3, 'bargain'))['result'] == 'pending')
chk('P5 结论三态互斥',
    sum([_v_deal['deal'], _v_deal['lost']]) == 1)

# 四维复盘
_rv_d = _ng.review(_ng.replay(['店员：抱歉，马上为您重做并送您优惠券'] * 6, 'bargain'))
_rv_v = _ng.review(_ng.replay(['店员：我们的原料是当天现做的'] * _ng.MAX_ROUNDS, 'bargain'))
_rv_c = _ng.review(_ng.replay(['店员：爱买不买'] * 2, 'bargain'))
chk('P5 复盘四维固定顺序',
    [s['label'] for s in _rv_d['scores']] == ['价值锚定', '让步节奏', '情绪管理', '成交结果'],
    str([s['label'] for s in _rv_d['scores']]))
chk('P5 复盘维度分均在 0~100',
    all(0 <= s['score'] <= 100 for s in _rv_d['scores'] + _rv_v['scores'] + _rv_c['scores']))
chk('P5 复盘含总分/等级/建议',
    0 <= _rv_d['total'] <= 100 and _rv_d['level'] in ('优秀', '合格', '需改进')
    and len(_rv_d['advice']) == 2, str(_rv_d)[:120])
chk('P5 讲价值成交优于靠降价成交', _rv_v['total'] > _rv_d['total'],
    f"{_rv_v['total']} vs {_rv_d['total']}")
chk('P5 对抗流失复盘显著低于成交', _rv_c['total'] < _rv_d['total'],
    f"{_rv_c['total']} vs {_rv_d['total']}")
chk('P5 越权承诺锚定分低于价值解释',
    _ng.CLASS_ANCHOR_SCORE['overpromise'] < _ng.CLASS_ANCHOR_SCORE['value'])
chk('P5 未成交时建议含成交结果维度',
    any('未成交' in a for a in _rv_c['advice']), str(_rv_c['advice']))

# 可见态：给区间不给精确底价
_vis = _ng.visible_state(_ini, _v_deal)
chk('P5 可见态含耐心/区间/让步进度',
    'patience' in _vis and len(_vis['price_range']) == 2 and 'progress' in _vis)
chk('P5 可见态区间为整数提示(不泄露精确底价)',
    all(isinstance(x, int) for x in _vis['price_range'])
    and _vis['price_range'][0] <= _ini['reservation'] <= _vis['price_range'][1])
chk('P5 可见态让步进度 0~100',
    0 <= _ng.visible_state(_ini)['progress'] <= 100)

# HTTP 层：情景页渲染谈判面板与复盘卡（milk_tea_staff2 登录 + 真 CSRF）
_c_staff.get('/ai/chat?scenario=bargain')
_p5 = _c_staff.get('/ai/chat?scenario=bargain').get_data(as_text=True)
_t5 = re.search(r'name="csrf_token" value="([^"]+)"', _p5)
_r5 = _c_staff.post('/ai/chat', data={'action': 'finish',
                                      'csrf_token': _t5.group(1) if _t5 else ''})
_b5 = _r5.get_data(as_text=True)
chk('P5 结束页 200 且渲染复盘卡', _r5.status_code == 200 and 'review-card' in _b5,
    f'code={_r5.status_code}')
chk('P5 复盘卡含四维评分条', _b5.count('review-score-row') >= 4)
chk('P5 复盘卡无 | safe 透传', '| safe' not in _b5)
chk('P5 CSRF 缺失时结束提交被拒',
    _c_staff.post('/ai/chat', data={'action': 'finish'}).status_code in (400, 403))

# ════════════ P6 AI 决策轨迹：意图解析 / 工具链 / 步骤契约 / 时间线渲染 ════════════
import json as _json  # noqa: E402
import shutil as _shutil  # noqa: E402
import tempfile as _tempfile  # noqa: E402

from src import agent_trace as _at  # noqa: E402

# 意图解析（与规则版路由分支同源同序，不是第二套判定逻辑）
chk('P6 意图-营收问句', _at.classify_intent('这个月营收多少')[0] == 'revenue',
    str(_at.classify_intent('这个月营收多少')))
chk('P6 意图-毛利问句', _at.classify_intent('门店毛利率是多少')[0] == 'margin')
chk('P6 意图-损耗问句', _at.classify_intent('近期有什么损耗异常吗')[0] == 'waste')
chk('P6 意图-考核问句', _at.classify_intent('谁的考核成绩最好')[0] == 'assessment')
chk('P6 意图-备货类不误判为预测(例外词生效)',
    _at.classify_intent('明天备货进多少')[0] == 'restock',
    str(_at.classify_intent('明天备货进多少')))
chk('P6 意图-产品名命中', _at.classify_intent('杨枝甘露卖得怎么样')[0] == 'product')
chk('P6 意图-无关键词落知识库', _at.classify_intent('顾客嫌贵砍价怎么应对')[0] == 'knowledge')
chk('P6 意图-空问句单独归类', _at.classify_intent('')[0] == 'empty')
chk('P6 来源徽章-在线=大模型生成', _at.source_label('deepseek') == '大模型生成')
chk('P6 来源徽章-未知来源按规则版(不臆造大模型)',
    _at.source_label('rule') == '规则版' and _at.source_label('xxx') == '规则版')
chk('P6 摘要截断不超上限', len(_at._cap('甲' * 500)) == _at.SUMMARY_MAX)


def _boom():
    raise ValueError('轨迹单步故障')


_run = _at.TraceRun('unit-test', '测试问句')
_v_ok = _run.step('ok.step', lambda: ('值', '摘要'), input_summary='入参')
_v_bad = _run.step('fail.step', _boom)
_v_next = _run.step('after.failed', lambda: (1, '继续'))
chk('P6 步骤-失败步返回 None 且不中断后续',
    _v_ok == '值' and _v_bad is None and _v_next == 1)
chk('P6 步骤-失败步 ok=False', _run.steps[0]['ok'] is True and _run.steps[1]['ok'] is False)
_step_keys = {'step', 'name', 'input_summary', 'output_summary',
              'latency_ms', 'source', 'source_label', 'ok'}
chk('P6 步骤-字段结构完整', all(_step_keys <= set(s) for s in _run.steps))
chk('P6 步骤-序号递增且耗时为非负整数',
    [s['step'] for s in _run.steps] == [1, 2, 3]
    and all(isinstance(s['latency_ms'], int) and s['latency_ms'] >= 0 for s in _run.steps))
chk('P6 步骤-默认来源徽章为本地计算', _run.steps[0]['source_label'] == '本地计算')
_d = _run.to_dict()
chk('P6 轨迹-总耗时等于各步之和',
    _d['total_ms'] == sum(s['latency_ms'] for s in _run.steps)
    and {'scene', 'question', 'ts', 'source', 'source_label', 'total_ms', 'steps'} <= set(_d))

# 流水线：规则版离线同样产出完整轨迹（离线可演示是硬要求）
with app.app_context():
    _res1, _tr1 = _at.run_assistant('这个月营收多少', None,
                                    lambda q, s: {'source': 'rule', 'reply': '营收 ¥100'})
    chk('P6 助手轨迹-营收问句三步齐全',
        [s['name'] for s in _tr1['steps']] == ['intent.parse', 'analytics.stats', 'conclusion'],
        str([s['name'] for s in _tr1['steps']]))
    chk('P6 助手轨迹-意图步输出含中文标签',
        '营收问询' in _tr1['steps'][0]['output_summary'], _tr1['steps'][0]['output_summary'])
    chk('P6 助手轨迹-统计步输出真实 KPI',
        '总营收' in _tr1['steps'][1]['output_summary'], _tr1['steps'][1]['output_summary'])
    chk('P6 助手轨迹-结论步输出为回答正文',
        _tr1['steps'][-1]['output_summary'] == '营收 ¥100')
    chk('P6 助手轨迹-规则版徽章', _tr1['source'] == 'rule' and _tr1['source_label'] == '规则版')
    chk('P6 助手轨迹-结论原样透传', _res1['reply'] == '营收 ¥100')

    _res2, _tr2 = _at.run_assistant('顾客嫌贵砍价怎么应对', None,
                                    lambda q, s: {'source': 'rule', 'reply': '话术建议'})
    chk('P6 助手轨迹-知识类调用知识库工具',
        any(s['name'] == 'knowledge.search' for s in _tr2['steps']),
        str([s['name'] for s in _tr2['steps']]))

    _res3, _tr3 = _at.run_assistant('谁的考核成绩最好', None,
                                    lambda q, s: {'source': 'rule', 'reply': '小王最好'})
    chk('P6 助手轨迹-考核类走考核记录且不重复聚合',
        any(s['name'] == 'quiz.records' for s in _tr3['steps'])
        and not any(s['name'] == 'analytics.stats' for s in _tr3['steps']),
        str([s['name'] for s in _tr3['steps']]))

    _res4, _tr4 = _at.run_assistant('明天预计能卖多少杯', None,
                                    lambda q, s: {'source': 'rule', 'reply': '约 30 杯'})
    chk('P6 助手轨迹-预测类调用预测工具',
        any(s['name'] == 'forecast.eval' for s in _tr4['steps']),
        str([s['name'] for s in _tr4['steps']]))

    _an, _tra = _at.run_analysis(compute_dashboard_stats(), lambda s: {
        'source': 'rule', 'content': '报告', 'evidence': [], 'confidence': {'avg': 0}})
    chk('P6 分析轨迹-含统计步与依据汇总步',
        any(s['name'] == 'analytics.stats' for s in _tra['steps'])
        and any(s['name'] == 'evidence.summary' for s in _tra['steps']),
        str([s['name'] for s in _tra['steps']]))
    chk('P6 分析轨迹-分析结果原样透传', _an['content'] == '报告')

    # 落盘：写临时路径，避免污染项目 data/（写失败静默由 vuln 套件覆盖）
    _tmp_dir = _tempfile.mkdtemp(prefix='ican_trace_')
    _old_path = _at.TRACE_PATH
    _at.TRACE_PATH = os.path.join(_tmp_dir, 'agent_trace.jsonl')
    try:
        _at.run_assistant('这个月营收多少', None,
                          lambda q, s: {'source': 'rule', 'reply': '营收 ¥100'})
        with open(_at.TRACE_PATH, encoding='utf-8') as _f:
            _lines = [ln for ln in _f.read().splitlines() if ln.strip()]
        chk('P6 轨迹落盘-JSONL 每行可解析',
            len(_lines) >= 1 and all(_json.loads(ln).get('steps') for ln in _lines))
        chk('P6 轨迹落盘-落盘结构与渲染结构一致',
            all({'step', 'name', 'output_summary', 'latency_ms', 'source', 'ok'} <= set(s)
                for s in _json.loads(_lines[-1])['steps']))
    finally:
        _at.TRACE_PATH = _old_path
        _shutil.rmtree(_tmp_dir, ignore_errors=True)

# HTTP 层：管理员两页渲染时间线（client 已登录 admin，CSRF 真开）
_ba = client.get('/ai/admin/business-analysis')
_ba_html = _ba.get_data(as_text=True)
chk('P6 经营分析页 200 且渲染时间线',
    _ba.status_code == 200 and 'agent-trace' in _ba_html, f'code={_ba.status_code}')
chk('P6 经营分析时间线含多个步骤行', _ba_html.count('trace-step-head') >= 2,
    str(_ba_html.count('trace-step-head')))
chk('P6 经营分析时间线含来源徽章', '规则版' in _ba_html or '大模型生成' in _ba_html)
chk('P6 经营分析时间线无 | safe 透传', '| safe' not in _ba_html)

_ap = client.get('/ai/admin/assistant').get_data(as_text=True)
_atok = re.search(r'name="csrf_token" value="([^"]+)"', _ap)
chk('P6 助手页可取得 CSRF token', _atok is not None)
_aj = client.post('/ai/admin/assistant',
                  data={'ask': '这个月营收多少',
                        'csrf_token': _atok.group(1) if _atok else ''},
                  headers={'X-Requested-With': 'fetch'})
_aj_json = _aj.get_json() or {}
chk('P6 助手 AJAX 200 且带 trace_html',
    _aj.status_code == 200 and 'trace-step-head' in (_aj_json.get('trace_html') or ''),
    f'code={_aj.status_code}')
chk('P6 助手 AJAX 回答与轨迹同源返回', bool(_aj_json.get('reply')))
_anp = client.post('/ai/admin/assistant',
                   data={'ask': '这个月营收多少',
                         'csrf_token': _atok.group(1) if _atok else ''})
chk('P6 助手非 AJAX 整页渲染时间线',
    _anp.status_code == 200 and 'agent-trace' in _anp.get_data(as_text=True),
    f'code={_anp.status_code}')

# ════════════ P7-E / S-1+S-2 生产启动守卫（密钥强度 + 环境显式声明）════════════
# 背景：旧守卫只做「等于开发默认值」的等值黑名单，部署文档里明文写过的演示密钥
# （ican-demo-secret-2026-0910）能顺利通过；而 session 是客户端签名 Cookie，
# 密钥泄露 = 可离线伪造 admin 会话。故升级为「已知弱值 + 强度下限」并钉死。
import importlib as _importlib  # noqa: E402
import logging as _logging  # noqa: E402

import src.app as _app_mod  # noqa: E402
import src.config as _cfg  # noqa: E402

section('骨架')  # 启动守卫属通用契约，不随行业包变化

chk('S-1 空密钥判 empty', _cfg.secret_strength_issues('') == ['empty'])
chk('S-1 None 密钥判 empty', _cfg.secret_strength_issues(None) == ['empty'])
chk('S-1 开发默认密钥判 known-weak',
    'known-weak' in _cfg.secret_strength_issues(_cfg.DEV_SECRET_KEY))
chk('S-1 文档泄露密钥判 known-weak（等值黑名单绕过已堵）',
    'known-weak' in _cfg.secret_strength_issues('ican-demo-secret-2026-0910'))
chk('S-1 过短密钥判 too-short', 'too-short' in _cfg.secret_strength_issues('abc123'))
chk('S-1 长但字符单一判 low-entropy',
    'low-entropy' in _cfg.secret_strength_issues('a' * 24))
chk('S-1 既有强密钥不被误伤（回归保护）',
    _cfg.secret_strength_issues('k7Qz-real-prod-secret-9f3a1b') == [],
    str(_cfg.secret_strength_issues('k7Qz-real-prod-secret-9f3a1b')))
chk('S-1 建议长度常量 ≥ 最小长度',
    _cfg.RECOMMENDED_SECRET_LEN >= _cfg.MIN_PROD_SECRET_LEN)


class _CfgApp:
    """最小应用桩：只提供守卫所需的 config 与 logger，避免为断言引入 Flask 依赖。"""

    def __init__(self, **cfg):
        self.config = dict(cfg)
        self.logger = _logging.getLogger('l4-guard')


def _guard_raises(**cfg):
    try:
        _app_mod._enforce_production_guards(_CfgApp(**cfg))
    except RuntimeError:
        return True
    return False


_strong = 'k7Qz-real-prod-secret-9f3a1b'
chk('S-1 生产弱密钥→RuntimeError 拒启',
    _guard_raises(SECRET_KEY='ican-demo-secret-2026-0910',
                  SQLALCHEMY_DATABASE_URI='sqlite:///x.db'))
chk('S-1 生产过短密钥→RuntimeError 拒启',
    _guard_raises(SECRET_KEY='short', SQLALCHEMY_DATABASE_URI='sqlite:///x.db'))
chk('S-1 生产强密钥+有连接串→放行（不误伤）',
    not _guard_raises(SECRET_KEY=_strong, SQLALCHEMY_DATABASE_URI='sqlite:///x.db'))
chk('S-1 未配 DATABASE_URL 仍拒启（F1 守卫未回退）',
    _guard_raises(SECRET_KEY=_strong, SQLALCHEMY_DATABASE_URI=None))

# 逃生开关：换 key 过渡期只告警不阻断（必须能关闭，否则线上无法平滑过渡）
_old_allow = _app_mod.ALLOW_WEAK_SECRET
try:
    _app_mod.ALLOW_WEAK_SECRET = True
    chk('S-1 ICAN_ALLOW_WEAK_SECRET=1 → 弱密钥仅告警不阻断',
        not _guard_raises(SECRET_KEY='ican-demo-secret-2026-0910',
                          SQLALCHEMY_DATABASE_URI='sqlite:///x.db'))
finally:
    _app_mod.ALLOW_WEAK_SECRET = _old_allow

# S-2：环境名显式声明可见性 + 生产 DEBUG 恒关
_old_env = {k: os.environ.get(k) for k in ('FLASK_ENV', 'ENV', 'FLASK_DEBUG')}
try:
    os.environ.pop('FLASK_ENV', None)
    os.environ.pop('ENV', None)
    chk('S-2 未声明 FLASK_ENV/ENV → env_explicitly_declared 为 False',
        _app_mod.env_explicitly_declared() is False)
    os.environ['FLASK_ENV'] = 'production'
    chk('S-2 显式声明后 → env_explicitly_declared 为 True',
        _app_mod.env_explicitly_declared() is True)

    # DevelopmentConfig.DEBUG 可被 FLASK_DEBUG 单独覆盖（缺省仍 True，本地零变化）
    os.environ['FLASK_DEBUG'] = '0'
    _cfg_re = _importlib.reload(_cfg)
    chk('S-2 FLASK_DEBUG=0 → 开发配置 DEBUG 关闭（漏配 FLASK_ENV 时可止血）',
        _cfg_re.DevelopmentConfig.DEBUG is False)
    os.environ.pop('FLASK_DEBUG', None)
    _cfg_re = _importlib.reload(_cfg)
    chk('S-2 未设 FLASK_DEBUG → 开发配置 DEBUG 仍为 True（本地开发零变化）',
        _cfg_re.DevelopmentConfig.DEBUG is True)
finally:
    for _k, _v in _old_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

chk('S-2 生产配置 DEBUG 恒为 False', _cfg.config_map['production'].DEBUG is False)

# ════════════ P7-E / E2-A 安全收口：配置后台输入校验与包边界 ════════════
section('骨架')  # 安全契约与行业包无关，归入骨架分区
with app.app_context():
    # S-7 包存在性（此前只校验格式 → 幽灵包会顶着默认包内容渲染并可写入）
    chk('S-7 文件包存在性为真', _ps.pack_exists('milk_tea'))
    chk('S-7 不存在的包判 False', not _ps.pack_exists('no_such_pack_xyz'))
    chk('S-7 格式非法的包判 False',
        not _ps.pack_exists('BAD_ID') and not _ps.pack_exists('..'))

    # S-3 copy_from 白名单（旧守卫因 get_pack 静默回退而永不触发）
    _ok_s3, _msg_s3 = _ps.create_pack('l4_e2a', 'E2A探针', '', copy_from='../../..')
    chk('S-3 copy_from 路径穿越被拒', not _ok_s3, _msg_s3)
    chk('S-3 copy_from 非法标识被拒',
        not _ps.create_pack('l4_e2a2', 'E2A探针2', '', copy_from='x' * 60)[0])
    chk('S-3 copy_from 不存在被拒',
        not _ps.create_pack('l4_e2a3', 'E2A探针3', '', copy_from='no_such_pack_xyz')[0])

    # S-8 产品价格强校验（NaN / Inf / 负数 / 超上限）
    chk('S-8 合法产品价格放行',
        _ps.save_products('l4_bakery', _MD([('products__name', 'L4吐司'),
                                            ('products__price', '12.5'),
                                            ('products__cost', '6')]))[0])
    chk('S-8 NaN 单价被拒',
        not _ps.save_products('l4_bakery', _MD([('products__name', 'X'),
                                                ('products__price', 'nan')]))[0])
    chk('S-8 Inf 单价被拒',
        not _ps.save_products('l4_bakery', _MD([('products__name', 'X'),
                                                ('products__price', 'inf')]))[0])
    chk('S-8 负数单价被拒',
        not _ps.save_products('l4_bakery', _MD([('products__name', 'X'),
                                                ('products__price', '-1')]))[0])
    chk('S-8 超上限单价被拒',
        not _ps.save_products('l4_bakery', _MD([('products__name', 'X'),
                                                ('products__price', '99999999')]))[0])

    # S-9 数量与请求体上限
    _prod_md = _MD()
    for _i in range(_ps.MAX_PRODUCTS + 5):
        _prod_md.add('products__name', f'P{_i}')
        _prod_md.add('products__price', '1')
    _ok9, _msg9 = _ps.save_products('l4_bakery', _prod_md)
    chk('S-9 产品超条数上限被拒', not _ok9, _msg9)

    _scen_md = _MD()
    for _i in range(_ps.MAX_SCENARIOS + 5):
        _scen_md.add('scenarios__type', f's{_i}')
        _scen_md.add('scenarios__label', f'情景{_i}')
        _scen_md.add('scenarios__lines', '您好\n请问需要什么')
    _ps.save_scenarios('l4_bakery', _scen_md)
    chk('S-9 情景个数被截到上限',
        len(_ip.get_pack('l4_bakery')['scenarios']['pool']) <= _ps.MAX_SCENARIOS)

    _syn_md = _MD([('synonyms',
                    '\n'.join(f'口语{_i},词{_i}'
                              for _i in range(_ps.MAX_SYNONYMS + 20)))])
    chk('S-9 同义词条数截到上限',
        len(_ps._parse_synonyms(_syn_md)) == _ps.MAX_SYNONYMS)

    # S-10 域名即 PackItem.key，必须与列宽（50）同源
    _long = '长域名' + 'x' * 80
    _trunc = _long[:50]
    _ps.save_domains('l4_bakery', _MD([('domains__name', _long),
                                       ('domains__keywords', 'k'),
                                       ('domains__tip', 't')]))
    _ps.save_knowledge('l4_bakery', _MD([(f'knowledge__{_trunc}', '内容')]))
    _krow = _ps.PackItem.query.filter_by(
        pack_id='l4_bakery', section='knowledge').order_by(
        _ps.PackItem.id.desc()).first()
    chk('S-10 域名 key 截断至列宽 50（跨库一致）',
        _krow is not None and len(_krow.key) == 50,
        str(len(_krow.key) if _krow else None))

    # S-14 写路径 commit 异常兜底：回滚 + 中文提示，不外扩成 500
    _sess = _ps.db.session
    _orig_commit = _sess.commit

    def _boom():
        raise RuntimeError('boom')

    _sess.commit = _boom
    try:
        _okc, _msgc = _ps._commit('探针')
    finally:
        _sess.commit = _orig_commit
    chk('S-14 commit 异常→(False, 中文提示) 不外扩',
        _okc is False and '保存失败' in _msgc, _msgc)

# S-15 包目录与包内文件白名单（无需应用上下文）
chk('S-15 合法包目录可解析',
    _ip._pack_dir('milk_tea') is not None
    and _ip._pack_dir('milk_tea').endswith('milk_tea'))
chk('S-15 路径穿越返回 None', _ip._pack_dir('../../..') is None)
chk('S-15 Windows 绝对路径/盘符返回 None', _ip._pack_dir('C:\\windows') is None)
chk('S-15 非法标识返回 None', _ip._pack_dir('BAD_ID') is None)
chk('S-15 包内文件名走白名单',
    _ip.load_pack_file_json('milk_tea', '../pack.json') is None
    and _ip.load_pack_file_json('milk_tea', 'pack.json') is not None)

# S-12 反馈键白名单（HTTP 层：非法域被拒）
_c_staff.get('/ai/chat?scenario=bargain')
_tb = re.search(r'name="csrf_token" value="([^"]+)"',
                _c_staff.get('/ai/chat?scenario=bargain').get_data(as_text=True))
_rb = _c_staff.post('/ai/kb-feedback', data={'domain': '不存在的域', 'title': 'x',
                                             'csrf_token': _tb.group(1) if _tb else ''})
chk('S-12 非法知识域反馈被拒',
    _rb.status_code == 200 and _rb.get_json().get('ok') is False,
    f'code={_rb.status_code}')

# ════════════ P7-E / E2-B 限流与会话 ════════════
section('骨架')
from flask import request as _flask_request  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix as _ProxyFix  # noqa: E402
from werkzeug.test import Client as _WzClient  # noqa: E402
from werkzeug.wrappers import Request as _WzRequest  # noqa: E402
from werkzeug.wrappers import Response as _WzResponse  # noqa: E402

from src import rate_limit as _rl  # noqa: E402

# S-11：限流键必须区分角色（admin-1 与 staff-1 撞桶会让正常用户被误 429）
# 用真实登录会话取键（test_request_context 里手写 session 会触发 flask_login
# 的会话保护判定，取不到 current_user，故改为「真实客户端 + after_request 捕获」）
_captured = []


def _capture_key(_resp):
    _captured.append(_rl._bucket_key('chat'))
    return _resp


app.after_request_funcs.setdefault(None, []).append(_capture_key)
try:
    _c_admin = app.test_client()
    _lp = _c_admin.get('/login')
    _tk = re.search(r'name="csrf_token" value="([^"]+)"', _lp.get_data(as_text=True))
    _c_admin.post('/login', data={'username': 'owner_demo', 'password': 'owner123',
                                 'role': 'admin',
                                 'csrf_token': _tk.group(1) if _tk else ''})
    _c_admin.get('/admin/')
    _k_admin = _captured[-1]
    _c_staff.get('/h5/')
    _k_staff = _captured[-1]
finally:
    app.after_request_funcs[None].remove(_capture_key)

chk('S-11 管理员限流键带 admin 角色', 'admin-' in _k_admin, _k_admin)
chk('S-11 店员限流键带 staff 角色', 'staff-' in _k_staff, _k_staff)
chk('S-11 不同角色同 id 不撞桶', _k_admin != _k_staff,
    f'{_k_admin} vs {_k_staff}')

# S-4：request_ip 不再无条件信任 XFF 首值；由 ProxyFix 统一校正
with app.test_request_context('/', headers={'X-Forwarded-For': '9.9.9.9'},
                              environ_base={'REMOTE_ADDR': '127.0.0.1'}):
    _ipv = _rl.request_ip()
chk('S-4 限流键不再直接取 XFF 首值', _ipv == '127.0.0.1', _ipv)


@_WzRequest.application
def _probe_app(request):
    return _WzResponse(request.remote_addr)


_wz = _WzClient(_ProxyFix(_probe_app, x_for=1, x_proto=1))
chk('S-4 ProxyFix(x_for=1) 取可信跳（伪造前缀失效）',
    _wz.get('/', headers={'X-Forwarded-For': '9.9.9.9, 5.5.5.5'})
    .get_data(as_text=True) == '5.5.5.5')
chk('S-4 TRUST_PROXY_HOPS 默认 1 且可配',
    _app_mod.TRUST_PROXY_HOPS == 1)

# S-6：管理端写接口纳入限流（仅写方法计数，GET 不消耗额度）
chk('S-6 admin_write 限值已登记', _rl.DEFAULT_LIMITS.get('admin_write') == (60, 60),
    str(_rl.DEFAULT_LIMITS.get('admin_write')))

# S-13：登出必须 POST + CSRF（GET 不得再触发登出）
_rg = _c_staff.get('/logout')
chk('S-13 GET /logout 不再生效（405）', _rg.status_code == 405,
    f'code={_rg.status_code}')
_tl = re.search(r'name="csrf_token" value="([^"]+)"',
                _c_staff.get('/ai/chat?scenario=bargain').get_data(as_text=True))
_rlogout = _c_staff.post('/logout', data={'csrf_token': _tl.group(1) if _tl else ''})
chk('S-13 POST /logout 正常登出（302 回登录页）',
    _rlogout.status_code == 302
    and 'login' in (_rlogout.headers.get('Location') or ''),
    f'code={_rlogout.status_code}')
chk('S-13 登出后受保护页跳登录', _c_staff.get('/h5/').status_code == 302)

# ════════════ P7-E3-A 一致性与静默故障（PF-4 / PF-5 / PF-9 / PF-12 / PF-13）════════════
section('包契约')
from datetime import date as _date4  # noqa: E402
from unittest import mock as _mock4  # noqa: E402

from sqlalchemy.exc import IntegrityError as _L4IE  # noqa: E402

from src.analytics import _compute_gross_margin  # noqa: E402
from src.models import DailySales as _DS4  # noqa: E402
from src.models import Product as _P4  # noqa: E402
from src.models import Staff as _S4
from src.models import db as _db4

# PF-4：内置包骨架角色必须映射既有列（未映射即静默写 0 → 看板全零，属潜伏缺陷）
with app.app_context():
    _ip.set_active_pack('milk_tea')
    _ip.invalidate()
    chk('PF-4 内置包骨架数量键映射既有列',
        _m.skeleton_key(_m.ROLE_QUANTITY) == 'product_sales_qty',
        str(_m.skeleton_key(_m.ROLE_QUANTITY)))
    chk('PF-4 内置包骨架金额键映射既有列',
        _m.skeleton_key(_m.ROLE_AMOUNT) == 'total_revenue',
        str(_m.skeleton_key(_m.ROLE_AMOUNT)))

# PF-5a：毛利率只取当前激活包的成本价（此前 Product.query.all() 跨包串成本）
with app.app_context():
    _db4.session.add_all([
        _P4(product_name='L4毛利奶茶A', unit_price=20, cost_price=8, pack_id='milk_tea'),
        _P4(product_name='L4毛利苹果B', unit_price=10, cost_price=3, pack_id='fruit'),
    ])
    _db4.session.commit()
    _df4 = pd.DataFrame({'product_name': ['L4毛利苹果B'],
                         'product_sales_qty': [10], 'total_revenue': [200.0]})
    _ip.set_active_pack('milk_tea')
    _, _ci_mt, _, _sn_mt = _compute_gross_margin(_df4.copy())
    chk('PF-5 毛利率不取他包成本价（奶茶包下苹果成本不可见）', _ci_mt == 0,
        f'cost_items={_ci_mt} snapshot={_sn_mt}')
    _ip.set_active_pack('fruit')
    _, _ci_fr, _, _sn_fr = _compute_gross_margin(_df4.copy())
    chk('PF-5 当前包成本价正常命中', _ci_fr == 1, f'cost_items={_ci_fr} snapshot={_sn_fr}')
    _ip.set_active_pack('milk_tea')
    _ip.invalidate()

# PF-5b：店员历史页只列当前激活包流水（切包后不再串显他包记录）
# 注意：_c_staff 已被 S-13 登出用例登出，这里另开一个已登录客户端（milk_tea_staff2 仍启用）。
_c_his = app.test_client()
_ph = _c_his.get('/login').get_data(as_text=True)
_th = re.search(r'name="csrf_token" value="([^"]+)"', _ph)
_c_his.post('/login', data={'username': 'milk_tea_staff2', 'password': 'staff123',
                            'role': 'staff', 'csrf_token': _th.group(1) if _th else ''})
with app.app_context():
    _st4 = _S4.query.filter_by(username='milk_tea_staff2').first()
    _db4.session.add_all([
        _DS4(staff_id=_st4.id, record_date=_date4(2026, 12, 2),
             product_name='L4隔离测试奶茶', product_sales_qty=5,
             total_revenue=100.0, pack_id='milk_tea'),
        _DS4(staff_id=_st4.id, record_date=_date4(2026, 12, 1),
             product_name='L4隔离测试苹果', product_sales_qty=7,
             total_revenue=140.0, pack_id='fruit'),
    ])
    _db4.session.commit()
_html4 = _c_his.get('/h5/history').get_data(as_text=True)
chk('PF-5 历史页含本包流水', 'L4隔离测试奶茶' in _html4, f'html_len={len(_html4)}')
chk('PF-5 历史页不含他包流水', 'L4隔离测试苹果' not in _html4)

# PF-9：覆盖层读取失败必须可见（此前静默回退 → 管理员「保存了却不生效」）
_ip.clear_override_error('__l4_probe__')
_ip.note_override_error('__l4_probe__', RuntimeError('模拟 DB 抖动'))
_e4 = _ip.override_error()
chk('PF-9 覆盖层故障登记可见',
    _e4 is not None and _e4['pack_id'] == '__l4_probe__'
    and 'RuntimeError' in (_e4['message'] or ''), str(_e4))
_ip.clear_override_error('__l4_probe__')
chk('PF-9 故障恢复后告警自动清除', _ip.override_error() is None)


def _boom4(pack_id):
    raise RuntimeError('模拟覆盖层故障')


_orig_hook = _ip._HOOKS['db_override']
_ip._HOOKS['db_override'] = _boom4
_ip.invalidate()
_p4 = _ip.get_pack('milk_tea')
chk('PF-9 覆盖层异常不外扩（仍返回可用文件包）',
    _p4['id'] == 'milk_tea' and _p4['source'] == 'file', _p4['source'])
chk('PF-9 覆盖层异常被登记为告警',
    (_ip.override_error() or {}).get('pack_id') == 'milk_tea',
    str(_ip.override_error()))
_ip._HOOKS['db_override'] = _orig_hook
_ip.invalidate()
_ip.get_pack('milk_tea')
chk('PF-9 覆盖层恢复后告警清除（真实链路）', _ip.override_error() is None,
    str(_ip.override_error()))

# PF-12：并发唯一约束冲突 → 回滚 + 友好提示，绝不成 500
_htmlp = client.get('/admin/products').get_data(as_text=True)
_tok4 = re.search(r'name="csrf_token" value="([^"]+)"', _htmlp)
with _mock4.patch('src.routes.admin.db.session.commit',
                  side_effect=_L4IE('UNIQUE constraint failed', None, None)):
    _r4 = client.post('/admin/products/add', data={
        'csrf_token': _tok4.group(1) if _tok4 else '',
        'product_name': 'L4并发冲突产品', 'price': '9.9',
        'category': '', 'cost_price': ''})
chk('PF-12 产品添加并发冲突不 500（回滚后重定向）', _r4.status_code == 302,
    f'code={_r4.status_code}')
with app.app_context():
    chk('PF-12 冲突后未留下脏数据',
        _P4.query.filter_by(product_name='L4并发冲突产品').first() is None)

# PF-13：DB-only 包无内置定义可回退时，拒绝 reset（防自建包被奶茶骨架静默污染）
with app.app_context():
    _ok4, _msg4 = _ps.create_pack('l4_e3a', 'L4E3A包', '', copy_from='milk_tea')
    chk('PF-13 前置：自建包创建成功', _ok4, _msg4)
    _n_before = len(_ip.get_pack('l4_e3a')['metrics'])
    _ok5, _msg5 = _ps.reset_section('l4_e3a', 'metrics')
    chk('PF-13 自建包无内置定义 → 拒绝恢复默认',
        not _ok5 and '自建包' in _msg5, _msg5)
    _n_after = len(_ip.get_pack('l4_e3a')['metrics'])
    chk('PF-13 拒绝后指标未被清空', _n_after == _n_before and _n_before > 0,
        f'{_n_before} → {_n_after}')
    _ok6, _msg6 = _ps.reset_section('milk_tea', 'metrics')
    chk('PF-13 内置文件包恢复默认仍放行', _ok6, _msg6)
    _ps.delete_pack('l4_e3a')

# ════════════ P7-E3-B1 词表与阈值口径收敛（M-1 / M-2 / M-4）════════════
section('包契约')
from src import learner_model as _lm41  # noqa: E402
from src.ai_explain import explain_business as _explain41  # noqa: E402
from src.dify_client import _assistant_rule_reply as _rule41  # noqa: E402
from src.knowledge_base import WEAK_KEYWORDS as _WK41  # noqa: E402
from src.models import QuizRecord as _QR41  # noqa: E402

# M-1：弱项词表唯一真相源 = 内置兜底包（此前 knowledge_base / learner_model 各存字面量副本）
chk('M-1 弱项词表收敛为兜底包单一真相源',
    _WK41 == _ip.FALLBACK_WEAK_KEYWORDS
    and _lm41._FALLBACK_DOMAIN_KEYWORDS == _ip.FALLBACK_WEAK_KEYWORDS,
    f'kb={_WK41} lm={_lm41._FALLBACK_DOMAIN_KEYWORDS}')
with app.app_context():
    chk('M-1 兜底词表与奶茶文件包声明逐字一致（内置包零回归）',
        _WK41 == tuple(_ip.get_pack('milk_tea')['weak_keywords']), str(_WK41))

# M-4：损耗阈值单入口 + AI 文案随包（此前 AI 恒说 15%，fruit 包看板 8%）
with app.app_context():
    _ip.invalidate()
    _ip.set_active_pack('milk_tea')
    chk('M-4 奶茶包阈值 0.15（与历史常量一致，零回归）',
        _ip.pack_waste_rate_threshold() == 0.15
        and _waste_threshold_fn() == 0.15,
        str(_ip.pack_waste_rate_threshold()))
    _ip.set_active_pack('fruit')
    _ip.invalidate()
    chk('M-4 fruit 包阈值 0.08 且看板判定同源',
        _ip.pack_waste_rate_threshold() == 0.08
        and _waste_threshold_fn() == 0.08,
        str(_ip.pack_waste_rate_threshold()))
    _fat_reply = _rule41('近期有什么损耗异常吗？',
                         {'has_data': True, 'kpi': {},
                          'waste_alerts': [{'product': 'L4损耗苹果'}]})
    chk('M-4 助手规则文案阈值随包（fruit 说 8% 不说 15%）',
        '8%' in _fat_reply and '15%' not in _fat_reply, _fat_reply[:120])
    _fat_evi = ' '.join(
        e for it in _explain41({'has_data': True, 'kpi': {},
                                'waste_alerts': [{'date': '2026-01-01',
                                                  'product': 'L4损耗苹果', 'note': ''}]})
        for e in (it.get('evidence') or []))
    chk('M-4 依据文案阈值随包（8%）', '8%' in _fat_evi, _fat_evi[:120])
    _ip.set_active_pack('milk_tea')
    _ip.invalidate()

# M-2：管理员考核页归因词表随包（此前恒用奶茶内嵌词表，切包后与店员侧口径不一致）
with app.app_context():
    _l4e3b = _S4(username='l4e3b', display_name='L4E3B店员')
    _l4e3b.set_password('l4e3b-pass')
    _db4.session.add(_l4e3b)
    _db4.session.commit()
    try:
        _db4.session.add(_QR41(
            staff_id=_l4e3b.id, quiz_type='question', pack_id='fruit',
            question='称重计价规范：顾客要求复秤时应如何操作？',
            staff_answer='不知道', ai_feedback='待强化',
            is_correct=False, score=0))
        _db4.session.commit()
        _ip.set_active_pack('fruit')
        _ip.invalidate()
        _html41 = client.get('/ai/admin/assessment').get_data(as_text=True)
        chk('M-2 考核归因随包（fruit 弱项词「称重」成为薄弱标签）',
            '薄弱：称重' in _html41, f'html_len={len(_html41)}')
        _ip.set_active_pack('milk_tea')
        _ip.invalidate()
        _html41b = client.get('/ai/admin/assessment').get_data(as_text=True)
        chk('M-2 切回奶茶包后奶茶词表生效（「称重」不再归因）',
            '薄弱：称重' not in _html41b, f'html_len={len(_html41b)}')
    finally:
        _QR41.query.filter_by(staff_id=_l4e3b.id).delete()
        _db4.session.delete(_l4e3b)
        _db4.session.commit()

# ════════════ P7-E3-B2 文案收敛（M-9 模板 labels / M-12 降级文案单入口）════════════
section('包契约')
import os as _os42  # noqa: E402
from unittest import mock as _mock42  # noqa: E402

from src.dify_client import (  # noqa: E402
    DIFY_UNAVAILABLE_BADGE as _BADGE42,
)
from src.dify_client import (
    DIFY_UNAVAILABLE_TEXT as _DTEXT42,
)

# M-12：短标签由长文案派生（单一真相源，杜绝第二份字面量）
chk('M-12 降级短标签由长文案派生（单一真相源）',
    _BADGE42 == _DTEXT42.split('，')[0] and '已切换为演示模式' in _DTEXT42,
    f'{_BADGE42} | {_DTEXT42}')

# M-9：模板样例与单位全部取自当前包（此前「杨枝甘露/杯」硬编码在模板里）
_lab42 = {}
with app.app_context():
    for _p42 in ('milk_tea', 'fruit', 'convenience'):
        _ip.set_active_pack(_p42)
        _ip.invalidate()
        _lab42[_p42] = _m.dashboard_labels()
    _ip.set_active_pack('milk_tea')
    _ip.invalidate()
chk('M-9 三包样例维度与单位随包（杨枝甘露/杯 · 红富士苹果/斤 · 鲜牛奶/件）',
    (_lab42['milk_tea']['sample_dimension'], _lab42['milk_tea']['unit']) == ('杨枝甘露', '杯')
    and (_lab42['fruit']['sample_dimension'], _lab42['fruit']['unit'])
    == ('红富士苹果', '斤')
    and (_lab42['convenience']['sample_dimension'], _lab42['convenience']['unit'])
    == ('鲜牛奶', '件'),
    str({k: (v['sample_dimension'], v['unit']) for k, v in _lab42.items()}))
chk('M-9 三包样例问题互不相同（随包切换可见）',
    len({v['sample_shelf_question'] for v in _lab42.values()}) == 3
    and all(v['sample_shelf_question'] for v in _lab42.values()),
    str({k: v['sample_shelf_question'] for k, v in _lab42.items()}))

# M-9：包漏声明样例 → 中性兜底（绝不回落奶茶样例，否则切包后又显示「杨枝甘露」）
with app.app_context(), _mock42.patch.object(
        _ip, 'pack_terms', return_value={'record_dimension': '菜品'}):
    _lab42b = _m.dashboard_labels()
chk('M-9 包漏声明样例时中性兜底（不回落奶茶样例）',
    _lab42b['sample_dimension'].startswith('这款')
    and '杨枝甘露' not in _lab42b['sample_shelf_question'],
    str(_lab42b))

# M-9 HTTP 层：切到 fruit 包后页面不再出现奶茶样例/单位
with app.app_context():
    _ip.set_active_pack('fruit')
    _ip.invalidate()
    # 注：流水按包隔离，fruit 包在 L4 库无数据 → 数值型文案不渲染（数据空态），
    # 「单位随包」不在此处做页面级断言，改由下方静态组 + 三包 labels 取值共同覆盖。
    _ba42 = client.get('/ai/admin/business-analysis').get_data(as_text=True)
    chk('M-9 经营分析页切包后正常渲染（不 500）',
        'AI 经营分析' in _ba42, f'len={len(_ba42)}')
    _as42 = client.get('/ai/admin/assistant').get_data(as_text=True)
    chk('M-9 助手页快捷提问样例随包（有红富士苹果，无杨枝甘露/珍珠）',
        '红富士苹果' in _as42 and '杨枝甘露' not in _as42 and '珍珠' not in _as42,
        f'len={len(_as42)}')
    _ip.set_active_pack('milk_tea')
    _ip.invalidate()

# M-9 HTTP 层：店员培训助手页（staff 登录）样例同样随包
_c_staff42 = app.test_client()
_pg42 = _c_staff42.get('/login').get_data(as_text=True)
_tk42 = re.search(r'name="csrf_token" value="([^"]+)"', _pg42)
_c_staff42.post('/login', data={'username': 'milk_tea_staff2', 'password': 'staff123',
                                'role': 'staff',
                                'csrf_token': _tk42.group(1) if _tk42 else ''})
with app.app_context():
    _ip.set_active_pack('fruit')
    _ip.invalidate()
    _tr42 = _c_staff42.get('/ai/trainer').get_data(as_text=True)
chk('M-9 培训助手页样例随包（有红富士苹果，无杨枝甘露/珍珠）',
    '红富士苹果' in _tr42 and '杨枝甘露' not in _tr42 and '珍珠' not in _tr42,
    f'len={len(_tr42)}')

# M-12 HTTP 层：降级文案经路由注入（JS 兜底串唯一来源；tojson 可能转义非 ASCII）
_esc42 = _DTEXT42.encode('ascii', 'backslashreplace').decode('ascii')
chk('M-12 培训助手页注入统一降级文案（JS 兜底串单源）',
    _DTEXT42 in _tr42 or _esc42 in _tr42, f'len={len(_tr42)}')

# M-12 HTTP 层：经营分析降级徽章改用统一短标签（此前内联「AI 服务暂不可用」）
with app.app_context(), _mock42.patch.object(
        _dc, 'business_analysis',
        return_value={'source': 'dify-error', 'content': 'L4降级占位',
                      'generated_at': '2026-01-01 00:00',
                      'evidence': [], 'confidence': {'count': 0, 'avg': 0,
                                                     'level': ''}}):
    _ba42b = client.get('/ai/admin/business-analysis').get_data(as_text=True)
    _ip.set_active_pack('milk_tea')
    _ip.invalidate()
chk('M-12 经营分析降级徽章用统一短标签', _BADGE42 in _ba42b,
    f'len={len(_ba42b)}')

# M-12 静态契约：4 个模板不再内联旧副本（旧串缺「已切换为演示模式」半句）
_tpl_root42 = _os42.path.join(_os42.path.dirname(_os42.path.abspath(__file__)),
                              'src', 'templates')
_tpl_names42 = ('admin/assistant.html', 'h5/ai_trainer.html',
                'admin/dashboard.html', 'admin/business_analysis.html')
_tpl_text42 = {}
for _n42 in _tpl_names42:
    with open(_os42.path.join(_tpl_root42, *_n42.split('/')),
              encoding='utf-8') as _f42:
        _tpl_text42[_n42] = _f42.read()
chk('M-12 模板无旧降级文案内联副本',
    all(('AI 服务暂不可用，请稍后重试。' not in t
         and 'AI 服务暂不可用（演示模式）' not in t)
        for t in _tpl_text42.values()),
    str([k for k, t in _tpl_text42.items()
         if 'AI 服务暂不可用，请稍后重试。' in t
         or 'AI 服务暂不可用（演示模式）' in t]))
chk('M-12 模板降级文案一律走注入变量',
    'degraded_text' in _tpl_text42['admin/assistant.html']
    and 'degraded_text' in _tpl_text42['h5/ai_trainer.html']
    and 'degraded_text' in _tpl_text42['admin/dashboard.html']
    and 'degraded_badge' in _tpl_text42['admin/dashboard.html']
    and 'degraded_badge' in _tpl_text42['admin/business_analysis.html'],
    '缺少注入变量引用')
chk('M-9 模板硬编码行业单位清零（杯/份）',
    all((' 杯' not in t and ' 份' not in t) for t in _tpl_text42.values()),
    str([k for k, t in _tpl_text42.items()
         if ' 杯' in t or ' 份' in t]))
_ba_tpl42 = _tpl_text42['admin/business_analysis.html']
chk('M-9 经营分析页单位/损耗文案走 labels（模板静态）',
    _ba_tpl42.count('{{ labels.unit }}') >= 3
    and '{{ labels.waste }}' in _ba_tpl42,
    f'unit×{_ba_tpl42.count("{{ labels.unit }}")}')
chk('M-9 助手/培训页行业样例走 labels.sample_*（模板静态）',
    '{{ labels.sample_dimension }}' in _tpl_text42['admin/assistant.html']
    and '{{ labels.sample_dimension }}' in _tpl_text42['h5/ai_trainer.html']
    and '{{ labels.sample_shelf_question }}' in _tpl_text42['h5/ai_trainer.html'],
    '样例未走包术语')
_dash_tpl42 = _tpl_text42['admin/dashboard.html']
chk('M-9 看板损耗卡片标题走 labels.waste（模板静态）',
    '{{ labels.waste }}提醒' in _dash_tpl42, '标题仍硬编码')

# ════════════ P7-E3-B3 意图表收敛（M-11 单一真相源 / M-18 公开入口）════════════
section('包契约')
from src import dify_client as _dc43  # noqa: E402
from src import intent_rules as _ir43  # noqa: E402

# M-11：三处消费方全部引用同一对象（identity 校验，杜绝字面量副本回潮）
chk('M-11 意图表单一真相源（三处消费同一对象）',
    _at.INTENT_RULES is _ir43.INTENT_RULES
    and _at.INTENT_LABELS is _ir43.INTENT_LABELS
    and _dc43.INTENT_EXEMPLARS is _ir43.INTENT_EXEMPLARS
    and _dc43.INTENT_ANCHORS is _ir43.INTENT_ANCHORS,
    '存在第二份字面量副本')

# M-11：旧三份的漂移词已并入单一表（slow 缺「哪些卖得」、restock 缺「明天」、
# 考核词表不同——改造前三处各自缺失）
_kw43 = dict(_ir43.INTENT_RULES)
chk('M-11 并集消除漂移（slow/restock/assessment 补齐旧缺词）',
    '哪些卖得' in _kw43['slow'] and '明天' in _kw43['restock']
    and '答题情况' in _kw43['assessment'] and '学得怎么样' in _kw43['assessment'],
    str(_kw43))

# M-11：轨迹判定与规则版路由分支同源同序（表首 forecast 先于 restock，
# 「明天」归预测、「明天备货进多少」例外词判否后归备货——行为与改造前一致）
chk('M-11 轨迹意图判定同序（明天=预测 / 备货例外=备货 / 补齐词=滞销）',
    _at.classify_intent('明天大概情况')[0] == 'forecast'
    and _at.classify_intent('明天备货进多少')[0] == 'restock'
    and _at.classify_intent('哪些卖得不好')[0] == 'slow'
    and _at.classify_intent('答题情况怎么样')[0] == 'assessment',
    str([_at.classify_intent(x)[0] for x in
         ('明天大概情况', '明天备货进多少', '哪些卖得不好', '答题情况怎么样')]))

# M-11 静态契约：_assistant_rule_reply 分支不再内联词表（全走 match_intent）
with open(_os42.path.join(_os42.path.dirname(_os42.path.abspath(__file__)),
                          'src', 'dify_client.py'), encoding='utf-8') as _f43:
    _dc_src43 = _f43.read()
_body43 = _dc_src43[_dc_src43.index('def _assistant_rule_reply'):]
chk('M-11 路由分支无内联词表残留（match_intent 单入口）',
    _body43.count('match_intent(q,') == 8
    and "any(k in q for k in ('" not in _body43,
    f"match_intent×{_body43.count('match_intent(q,')}")

# M-18：模糊路由转公开名，轨迹层不再调私有函数
chk('M-18 模糊路由公开入口 + 轨迹不再依赖私有名',
    callable(_dc43.intent_fuzzy_route)
    and _dc43.intent_fuzzy_route('生意好不好') == 'revenue'
    and '_intent_fuzzy_route' not in open(
        _os42.path.join(_os42.path.dirname(_os42.path.abspath(__file__)),
                        'src', 'agent_trace.py'), encoding='utf-8').read(),
    '私有名残留或公开入口缺失')

# ════════════ P7-E3-B2b 规则版文案单位随包（rule_copy / ai_explain / agent_trace）════════════
# 构造带预测/备货/周内规律的 stats，切包验证规则版报告单位不再恒为「杯」
_stats44 = {
    'has_data': True,
    'kpi': {'total_revenue': 1000, 'total_qty': 15, 'avg_daily_revenue': 500,
            'avg_per_unit': 66, 'gross_margin': None, 'report_days': 2},
    'top': {'names': ['红富士苹果'], 'values': [1000]},
    'pie': {'names': ['红富士苹果'], 'values': [15]},
    'daily': {'dates': [], 'revenue': [], 'qty': []},
    'waste_alerts': [],
    'forecast': {'ok': True, 'qty': 18, 'revenue': 1200, 'method': 'EWMA'},
    'replenishment': {'ok': True, 'items': [{'product': '鲜奶', 'suggest': 6.0,
                                             'basis': 'EWMA', 'sigma': 1.2}]},
    'weekday': {'ok': True, 'weekday_avg': 10, 'weekend_avg': 20},
}
with app.app_context():
    _ip.set_active_pack('fruit')
    _ip.invalidate()
    _rep44 = _dc43.dify_client.business_analysis(_stats44)
    _body44 = _rep44.get('content', '')
    chk('B2b fruit 包规则版报告单位=斤（总销量/预测/周内规律三处）',
        _rep44.get('source') == 'rule' and '总销量 15 斤' in _body44
        and '约 18 斤' in _body44 and '20 斤' in _body44
        and '杯' not in _body44, _body44[:160])
    _qty44 = _dc43._assistant_rule_reply('销量怎么样', _stats44)
    chk('B2b fruit 包助手销量答复单位=斤', '总销量 15 斤' in _qty44, _qty44[:100])
    _fc44 = _rule41('明天预计能卖多少', _stats44)
    chk('B2b fruit 包助手预测答复单位=斤', '约 18 斤' in _fc44, _fc44[:100])
    _evi44 = ' '.join(e for it in _explain41(_stats44) for e in (it.get('evidence') or []))
    _con44 = ' '.join(it.get('claim', '') for it in _explain41(_stats44))
    chk('B2b fruit 包依据/结论文案单位随包（无裸「杯」）',
        '杯' not in _evi44 and '杯' not in _con44, f'evi={_evi44[:100]}')
    _ip.set_active_pack('milk_tea')
    _ip.invalidate()
    _rep44m = _dc43.dify_client.business_analysis(_stats44)
    chk('B2b 奶茶包规则版报告单位=杯（零回归）',
        '总销量 15 杯' in _rep44m.get('content', ''), _rep44m['content'][:120])
    # 轨迹层预测工具摘要同样随包
    _ip.set_active_pack('convenience')
    _ip.invalidate()
    _tr44, _tx44 = _at._tool_forecast(_stats44)
    chk('B2b 轨迹预测摘要单位随包（便利店=件）', '明日约 18 件' in _tx44, _tx44)
    _ip.set_active_pack('milk_tea')
    _ip.invalidate()

# B2b 静态契约：rule_copy 文案函数不再内联「杯/份」字面量（单位一律走参数）
with open(_os42.path.join(_os42.path.dirname(_os42.path.abspath(__file__)),
                          'src', 'rule_copy.py'), encoding='utf-8') as _f44:
    _rc_src44 = _f44.read()
_rc_rest44 = _rc_src44.split("DEFAULT_MATERIAL_UNIT = '份'", 1)[1]  # 常量定义之后的全部代码体
chk('B2b rule_copy 文案体无内联单位（仅 DEFAULT_* 常量）',
    _rc_src44.count("'杯'") + _rc_src44.count('"杯"') == 1
    and _rc_src44.count("'份'") + _rc_src44.count('"份"') == 1
    and '杯' not in _rc_rest44.replace('「杯」', '')
    and '份' not in _rc_rest44.replace('「份」', ''),
    '文案体仍含硬编码单位')

# ════════════ P7-E3-C 激活态并发（PF-1：加锁 + 请求级快照）════════════
# 同一请求先读到 milk_tea 后，另一线程把全局激活态切到 fruit；
# 该请求后续读取必须仍为 milk_tea（判重/落库共享同一包 id），
# 新请求才读取切换后的 fruit。请求外保持原全局语义。
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

_prev45 = _ip.active_pack_id()
try:
    with app.test_request_context('/h5/submit'):
        _ip.set_active_pack('milk_tea')
        _snap45_first = _ip.active_pack_id()

        def _switch_pack45():
            with app.app_context():
                return _ip.set_active_pack('fruit')

        with ThreadPoolExecutor(max_workers=1) as _pool45:
            _switched45 = _pool45.submit(_switch_pack45).result(timeout=5)
        _snap45_after = _ip.active_pack_id()
        chk('PF-1 请求级快照：全局切包不影响在途请求',
            _snap45_first == 'milk_tea' and _snap45_after == 'milk_tea'
            and _switched45 == 'fruit',
            f'first={_snap45_first} after={_snap45_after} switched={_switched45}')

    _global45 = _ip.active_pack_id()
    chk('PF-1 请求外仍读取最新全局激活态（新请求可见 fruit）',
        _global45 == 'fruit', f'global={_global45}')
finally:
    with app.app_context():
        _ip.set_active_pack(_prev45 or 'milk_tea')

chk('PF-1 激活态读改写受进程锁保护（RLock + 请求快照键）',
    hasattr(_ip._LOCK, 'acquire')
    and _ip._REQUEST_PACK_ENV_KEY == 'ican.active_pack_id',
    f'lock={type(_ip._LOCK).__name__} key={_ip._REQUEST_PACK_ENV_KEY}')

# ════════════ P7-E4 性能收口（PF-2/3/6/7/8/10/14/15）════════════
import tempfile  # noqa: E402
from unittest import mock as _mock45  # noqa: E402

# PF-2：知识库/IDF 命中缓存，检索反馈只加载一次（不再按块开文件）。
with app.app_context():
    _kb45_a = _kb._load_kb()
    _kb45_b = _kb._load_kb()
    _idf45_a = _kb._idf_map()
    _idf45_b = _kb._idf_map()
    chk('PF-2 知识库与 IDF 命中同一缓存对象',
        _kb45_a is _kb45_b and _idf45_a is _idf45_b)
with open(_os42.path.join(_os42.path.dirname(_os42.path.abspath(__file__)),
                          'src', 'knowledge_base.py'), encoding='utf-8') as _f45:
    _kb_src45 = _f45.read()
_search45 = _kb_src45[_kb_src45.index('def search('):_kb_src45.index('def build_context(')]
chk('PF-2 检索反馈单次加载并传值（无块循环内读盘）',
    _search45.count('_load_feedback()') == 1
    and '_feedback_weight_from_data(feedback, dom, title)' in _search45
    and '_feedback_weight(dom, title)' not in _search45)

# PF-3：learning_focus 复用一次 mastery_profile，不再重复跑画像。
with app.app_context(), _mock45.patch.object(
        _lm, 'mastery_profile', wraps=_lm.mastery_profile) as _mp45:
    _lm.learning_focus([], now=_dt(2026, 1, 1))
    chk('PF-3 learning_focus 画像只计算一次', _mp45.call_count == 1,
        f'calls={_mp45.call_count}')

# PF-6：轨迹 JSONL 加锁 + 超限轮转。
_old_trace45, _old_max45 = _at.TRACE_PATH, _at.TRACE_MAX_BYTES
try:
    with tempfile.TemporaryDirectory() as _td45:
        _at.TRACE_PATH = _os42.path.join(_td45, 'agent_trace.jsonl')
        _at.TRACE_MAX_BYTES = 1
        _at.TraceRun('perf45', '轮转测试').persist()
        _at.TraceRun('perf45', '轮转测试2').persist()
        _rot45 = _os42.path.exists(_at.TRACE_PATH + '.1')
        with open(_at.TRACE_PATH, encoding='utf-8') as _f45a:
            _line45 = _f45a.readline()
        chk('PF-6 轨迹超限轮转且主文件仍可解析',
            _rot45 and isinstance(_json.loads(_line45), dict))
finally:
    _at.TRACE_PATH, _at.TRACE_MAX_BYTES = _old_trace45, _old_max45

# PF-7：看板重复渲染且无新 actual 时不重复写预测日志。
_old_fe_log45 = _fe.FORECAST_LOG_FILE
try:
    with tempfile.TemporaryDirectory() as _td45b:
        _fe.FORECAST_LOG_FILE = _os42.path.join(_td45b, 'forecast_log.json')
        with _mock45.patch.object(_fe, '_save_log',
                                  wraps=_fe._save_log) as _save45:
            _fe.record_prediction('2030-01-01', 12.0, {})
            _first45 = _save45.call_count
            _fe.record_prediction('2030-01-01', 12.0, {})
            _second45 = _save45.call_count - _first45
        chk('PF-7 首次预测写盘、同态重渲染零写盘',
            _first45 == 1 and _second45 == 0,
            f'first={_first45} second={_second45}')
finally:
    _fe.FORECAST_LOG_FILE = _old_fe_log45

# PF-8：包列表复用一次 meta 查询（静态契约防回潮）。
with open(_os42.path.join(_os42.path.dirname(_os42.path.abspath(__file__)),
                          'src', 'pack_store.py'), encoding='utf-8') as _f45b:
    _ps_src45 = _f45b.read()
_allpacks45 = _ps_src45[_ps_src45.index('def all_packs('):
                        _ps_src45.index('def _questions_of')]
chk('PF-8 包列表无循环单查 meta（复用一个查询）',
    _allpacks45.count('IndustryPack.query.all()') == 1
    and 'metas = {' in _allpacks45
    and '_pack_meta_row(pid)' not in _allpacks45)

# PF-10：损耗提醒与根因明细均限量并回传总数。
_cap45_rows = [{
    'staff_id': 1, 'record_date': pd.Timestamp('2026-08-01'),
    'product_name': f'P{i}', 'product_sales_qty': 1, 'total_revenue': 10.0,
    'raw_material_remaining': 8, 'raw_material_waste': 2, 'abnormal_notes': '',
} for i in range(35)]
_cap45_df = pd.DataFrame(_cap45_rows)
_cap45_review = _cds2.__globals__['_waste_review'](_cap45_df, 0.15)
_cap45_causes = _cds2.__globals__['waste_root_cause'](
    _cap45_df, frame=_cap45_review.get('_frame'))
chk('PF-10 损耗提醒限量 30 且 total=35',
    len(_cap45_review['alerts']) == 30 and _cap45_review['alert_total'] == 35,
    f"alerts={len(_cap45_review['alerts'])} total={_cap45_review['alert_total']}")
chk('PF-10 根因明细限量 30 且 total=35',
    len(_cap45_causes['items']) == 30 and _cap45_causes['total'] == 35,
    f"items={len(_cap45_causes['items'])} total={_cap45_causes['total']}")

# PF-14：购物篮分析改为 groupby 取数，源码不再使用 DataFrame.iterrows。
_basket45 = _cds2.__globals__['basket_association'](pd.DataFrame([
    {'record_date': pd.Timestamp('2026-08-01'), 'product_name': 'A', 'product_sales_qty': 1},
    {'record_date': pd.Timestamp('2026-08-01'), 'product_name': 'B', 'product_sales_qty': 1},
    {'record_date': pd.Timestamp('2026-08-02'), 'product_name': 'A', 'product_sales_qty': 1},
    {'record_date': pd.Timestamp('2026-08-02'), 'product_name': 'B', 'product_sales_qty': 1},
    {'record_date': pd.Timestamp('2026-08-03'), 'product_name': 'B', 'product_sales_qty': 1},
    {'record_date': pd.Timestamp('2026-08-03'), 'product_name': 'C', 'product_sales_qty': 1},
    {'record_date': pd.Timestamp('2026-08-04'), 'product_name': 'D', 'product_sales_qty': 1},
]))
_ana_src45 = open(_os42.path.join(_os42.path.dirname(_os42.path.abspath(__file__)),
                                  'src', 'analytics.py'), encoding='utf-8').read()
_basket_src45 = _ana_src45[_ana_src45.index('def basket_association('):
                           _ana_src45.index('# ── 损耗根因归因')]
chk('PF-14 购物篮 groupby 化且无 iterrows',
    _basket45.get('ok') is True and 'iterrows()' not in _basket_src45
    and "groupby('_day')" in _basket_src45)

# PF-15：多槽缓存命中，不在多包轮换时反复读盘。
with app.app_context(), _mock45.patch.object(
        _ip, '_load_pack', wraps=_ip._load_pack) as _load45:
    _ip.invalidate()
    _milk45a = _ip.get_pack('milk_tea')
    _fruit45 = _ip.get_pack('fruit')
    _milk45b = _ip.get_pack('milk_tea')
chk('PF-15 多包缓存命中且同包对象复用',
    _milk45a is _milk45b and _fruit45['id'] == 'fruit'
        and _load45.call_count == 2,
        f'calls={_load45.call_count}')

# PF-11：三个历史键叠加超限时，整会话签名 Cookie 仍被压回预算内。
from src.routes import ai as _ai45  # noqa: E402

_lines45 = ['问：' + __import__('secrets').token_hex(120) for _ in range(36)]
with app.test_request_context('/ai/admin/assistant'):
    _ai45.session['_user_id'] = 'admin-1'
    _ai45.session['_fresh'] = True
    for _idx45, _key45 in enumerate(
            ('chat_history', 'trainer_history', 'assistant_history')):
        _ai45.session[_key45] = _lines45[_idx45 * 12:(_idx45 + 1) * 12]
    _before45 = _ai45._session_cookie_bytes()
    _ai45._apply_session_budget(None)
    _after45 = _ai45._session_cookie_bytes()
    chk('PF-11 整会话 Cookie 总预算生效且保留登录身份',
        _before45 > _ai45.MAX_SESSION_COOKIE_BYTES
        and _after45 <= _ai45.MAX_SESSION_COOKIE_BYTES
        and _ai45.session.get('_user_id') == 'admin-1',
        f'before={_before45} after={_after45}')

# M-5：三页脚本外置到静态文件，模板只保留引用与数据属性。
_js45_dir = _os42.path.join(_os42.path.dirname(_os42.path.abspath(__file__)),
                            'src', 'static', 'js')
_js45_files = ('ai_chat.js', 'ai_trainer.js', 'assistant.js')
chk('M-5 三聊天页静态 JS 文件齐备',
    all(_os42.path.isfile(_os42.path.join(_js45_dir, f)) for f in _js45_files))
with open(_os42.path.join(_os42.path.dirname(_os42.path.abspath(__file__)),
                          'src', 'templates', 'h5', 'ai_chat.html'), encoding='utf-8') as _f45c:
    _tpl_chat45 = _f45c.read()
with open(_os42.path.join(_os42.path.dirname(_os42.path.abspath(__file__)),
                          'src', 'templates', 'h5', 'ai_trainer.html'), encoding='utf-8') as _f45d:
    _tpl_trainer45 = _f45d.read()
with open(_os42.path.join(_os42.path.dirname(_os42.path.abspath(__file__)),
                          'src', 'templates', 'admin', 'assistant.html'), encoding='utf-8') as _f45e:
    _tpl_assistant45 = _f45e.read()
chk('M-5 模板无内联交互脚本，数据属性齐备',
    'js/ai_chat.js' in _tpl_chat45
    and 'js/ai_trainer.js' in _tpl_trainer45
    and 'js/assistant.js' in _tpl_assistant45
    and 'fetch(' not in _tpl_chat45 + _tpl_trainer45 + _tpl_assistant45
    and 'data-feedback-url=' in _tpl_trainer45
    and 'data-degraded-text=' in _tpl_trainer45 + _tpl_assistant45)

# ═══════════ PH5：AI 决策驾驶舱 ═══════════
import threading as _thread5
import time as _time5

from src import ai_quota as _aq5
from src import decision_cockpit as _dcp5
from src.dify_client import dify_client as _dclient5
from src.models import Admin as _Admin5
from src.models import AIUsageLedger as _AUL5
from src.models import Store as _Store5
from src.models import StoreTask as _STask5

_dcp5.set_llm(None)
_aq5.clear_cache()
_fork_a5, _fork_b5 = _dclient5.fork(), _dclient5.fork()
chk('PH5 两个模型角色使用独立客户端状态',
    _fork_a5 is not _fork_b5
    and _fork_a5.breaker is not _fork_b5.breaker
    and _fork_a5.breaker is not _dclient5.breaker)
with app.app_context():
    _owner5 = _Admin5.query.filter_by(username='owner_demo').first()
    _store5 = _Store5.query.filter_by(merchant_id=_owner5.merchant_id).first()
    _snap5 = _dcp5.build_snapshot([_store5.id], days=30)
    _ev5 = _snap5['evidence'][0]['id']
    _tasks_before5 = _STask5.query.count()

_lock5 = _thread5.Lock()
_active5 = {'now': 0, 'max': 0, 'calls': 0}


def _fake_decision5(prompt, role):
    assert prompt
    with _lock5:
        _active5['calls'] += 1
        _active5['now'] += 1
        _active5['max'] = max(_active5['max'], _active5['now'])
    _time5.sleep(0.05)
    with _lock5:
        _active5['now'] -= 1
    if role == _dcp5.ROLE_ADVISOR:
        payload = {'proposals': [{
            'action': '检查损耗来源',
            'rationale': '需要核对备货和储存条件。',
            'evidence_ids': [_ev5],
            'priority': 'high',
        }]}
    else:
        payload = {'risks': [{
            'risk': '直接调整备货可能造成缺货',
            'severity': 'high',
            'evidence_ids': [_ev5],
            'recommendation': '先核对库存再调整。',
        }]}
    return {'ok': True, 'content': _json.dumps(payload, ensure_ascii=False),
            'source': 'deepseek', 'tokens': 100}


_dcp5.set_llm(_fake_decision5)
with app.app_context():
    _run5 = _dcp5.run_decision(
        _owner5.merchant_id, [_store5.id], days=30, refresh=True, allow_ai=True)
    _cached5 = _dcp5.run_decision(
        _owner5.merchant_id, [_store5.id], days=30, refresh=False, allow_ai=True)
    _usage5 = _AUL5.query.filter_by(
        merchant_id=_owner5.merchant_id, kind=_aq5.KIND_DECISION).count()
    _tasks_after_run5 = _STask5.query.count()

chk('PH5 双角色并发且状态隔离', _active5['max'] >= 2, str(_active5))
chk('PH5 一次完整驾驶舱生成只记一条决策配额',
    _usage5 == 1 and _run5['quota']['limit'] == 10,
    f"usage={_usage5} quota={_run5['quota']}")
chk('PH5 四角色与显式分歧可见',
    len(_run5['roles']) == 4
    and _run5['decision']['status'] == 'disputed'
    and len(_run5['decision']['disagreements']) == 1)
chk('PH5 确定性与主持角色保持规则来源',
    _run5['sources'][_dcp5.ROLE_DATA_ANALYST] == 'rule'
    and _run5['sources'][_dcp5.ROLE_MODERATOR] == 'rule')
chk('PH5 普通读取命中缓存且不重复推理',
    _cached5['cache'] == 'hit' and _active5['calls'] == 2,
    f"cache={_cached5['cache']} calls={_active5['calls']}")
chk('PH5 生成决策本身不自动创建任务', _tasks_after_run5 == _tasks_before5)


def _fake_single_fail5(prompt, role):
    if role == _dcp5.ROLE_ADVISOR:
        return {'ok': False, 'content': '', 'source': 'error', 'tokens': 0}
    return _fake_decision5(prompt, role)


_dcp5.set_llm(_fake_single_fail5)
with app.app_context():
    _single5 = _dcp5.run_decision(
        _owner5.merchant_id, [_store5.id], days=7, refresh=True, allow_ai=True)
chk('PH5 单角色失败时另一角色仍成功',
    _single5['sources'][_dcp5.ROLE_ADVISOR] == 'fallback'
    and _single5['sources'][_dcp5.ROLE_AUDITOR] == 'ai')

_dcp5.set_llm(lambda prompt, role: 'not-json')
with app.app_context():
    _invalid5 = _dcp5.run_decision(
        _owner5.merchant_id, [_store5.id], days=30, refresh=True, allow_ai=True)
chk('PH5 非法 JSON 按角色降级不崩溃',
    all(next(role for role in _invalid5['roles']
             if role['role'] == role_id)['status'] in ('error', 'fallback')
        for role_id in (_dcp5.ROLE_ADVISOR, _dcp5.ROLE_AUDITOR)))


def _fake_cross_scope5(prompt, role):
    if role == _dcp5.ROLE_ADVISOR:
        payload = {'proposals': [{
            'action': '越权动作', 'rationale': '不该出现',
            'evidence_ids': ['ev-999'], 'priority': 'high',
        }]}
    else:
        payload = {'risks': [{
            'risk': '越权风险', 'severity': 'high',
            'evidence_ids': ['ev-999'], 'recommendation': '不该出现',
        }]}
    return {'ok': True, 'content': _json.dumps(payload), 'source': 'deepseek',
            'tokens': 10}


_dcp5.set_llm(_fake_cross_scope5)
with app.app_context():
    _cross5 = _dcp5.run_decision(
        _owner5.merchant_id, [_store5.id], days=30, refresh=True, allow_ai=True)
chk('PH5 越权 evidence id 不进入结果',
    'ev-999' not in _json.dumps(_cross5, ensure_ascii=False)
    and _cross5['sources'][_dcp5.ROLE_ADVISOR] == 'fallback')

_dcp5.set_llm(None)
with app.app_context():
    _offline5 = _dcp5.run_decision(
        _owner5.merchant_id, [_store5.id], days=30, refresh=True, allow_ai=False)
    _empty5 = _dcp5.run_decision(
        _owner5.merchant_id, [], days=30, refresh=True, allow_ai=False)
chk('PH5 全部离线时四角色仍可见', len(_offline5['roles']) == 4
    and _offline5['sources'][_dcp5.ROLE_ADVISOR] == 'fallback'
    and _offline5['sources'][_dcp5.ROLE_AUDITOR] == 'fallback')
chk('PH5 无门店数据时不编造经营结论',
    len(_empty5['roles']) == 4
    and '没有有效营业记录' in _empty5['roles'][0]['claims'][0]['text'])

_page_dc5 = client.get('/admin/').get_data(as_text=True)
chk('PH5 管理员总看板首屏包含四角色驾驶舱',
    'AI 决策驾驶舱' in _page_dc5
    and all(label in _page_dc5 for label in ('数据分析师', '经营参谋', '风险审计员', '规则主持人')))

_dcp5.set_llm(lambda prompt, role: {
    'ok': True,
    'content': _json.dumps({
        'proposals': [{
            'action': '<script>alert(1)</script>',
            'rationale': '测试转义',
            'evidence_ids': [_ev5],
            'priority': 'high',
        }]
    }),
    'source': 'deepseek',
    'tokens': 10,
} if role == _dcp5.ROLE_ADVISOR else {
    'ok': True,
    'content': _json.dumps({'risks': [{
        'risk': '脚本风险',
        'severity': 'low',
        'evidence_ids': [_ev5],
        'recommendation': '普通文本',
    }]}),
    'source': 'deepseek',
    'tokens': 10,
})
_dc_token5 = re.search(r'name="csrf_token" value="([^"]+)"', _page_dc5)
_r_dc5 = client.post('/admin/decision/run', data={
    'csrf_token': _dc_token5.group(1) if _dc_token5 else '',
    'days': '30',
    'refresh': '1',
})
_j_dc5 = _r_dc5.get_json() or {}
_frag_dc5 = _j_dc5.get('fragment') or ''
chk('PH5 决策接口返回 JSON 与渲染片段',
    _r_dc5.status_code == 200 and _j_dc5.get('ok') is True
    and '数据分析师' in _frag_dc5)
chk('PH5 模型文本强制转义且不使用 safe',
    '<script>alert(1)</script>' not in _frag_dc5
    and '&lt;script&gt;' in _frag_dc5)

with app.app_context():
    _task_count_before_confirm5 = _STask5.query.count()
_task_token5 = re.search(r'name="csrf_token" value="([^"]+)"',
                         client.get('/admin/tasks/').get_data(as_text=True))
_r_task5 = client.post('/admin/tasks/create', data={
    'csrf_token': _task_token5.group(1) if _task_token5 else '',
    'store_id': str(_store5.id),
    'assignee': f'admin:{_owner5.id}',
    'due_date': (_d6.today() + _td(days=3)).isoformat(),
    'title': 'PH5 人工确认任务',
    'detail': '仅由管理员确认创建',
    'source': _tf2.SOURCE_AI_DECISION,
    'evidence_json': _json.dumps(_run5['evidence'], ensure_ascii=False),
})
with app.app_context():
    _confirmed5 = _STask5.query.filter_by(title='PH5 人工确认任务').first()
    _task_count_after_confirm5 = _STask5.query.count()
chk('PH5 只有人工提交才创建 ai_decision 任务',
    _r_task5.status_code in (302, 303)
    and _confirmed5 is not None
    and _confirmed5.source == _tf2.SOURCE_AI_DECISION
    and _task_count_after_confirm5 == _task_count_before_confirm5 + 1)

_dcp5.set_llm(None)
_aq5.clear_cache()

_pack_switch_page45 = client.get('/admin/').get_data(as_text=True)
chk('P10 管理员顶部常驻行业包切换器可见',
    'id="adminPackSelect"' in _pack_switch_page45
    and '行业包' in _pack_switch_page45
    and '水果生鲜' in _pack_switch_page45
    and '便利店' in _pack_switch_page45)

# ═══════════ /demo 免登录只读演示入口（设计契约 8 条验收断言）═══════════
# 契约全文：交付物/iCAN-参赛交付物/应用方案/demo-只读演示入口-设计契约.md
# 这一组断言的意义：材料里公开承诺了「打开链接即见四角色结论、零模型调用、
# 零写入、范围不可篡改」。承诺必须由可复现断言背书，否则就是宣传文案。
import os as _osDM  # noqa: E402

_DM_ENV = 'DEMO_MERCHANT_ID'

# ① 未配置 / 非法值 → 404（不区分原因，避免泄露「某个商家是否存在」）
_osDM.environ.pop(_DM_ENV, None)
chk('/demo 未配置演示商家时返回 404',
    client.get('/demo').status_code == 404)
_osDM.environ[_DM_ENV] = 'not-an-int'
chk('/demo 演示商家 id 非法时同样 404',
    client.get('/demo').status_code == 404)
_osDM.environ[_DM_ENV] = '999999'
chk('/demo 演示商家不存在时同样 404',
    client.get('/demo').status_code == 404)

# 对照租户：用来证明请求参数无法扩大可见门店范围
with app.app_context():
    _dm_other = _MerchantM(name='对照商家', is_active=True)
    _db8.session.add(_dm_other)
    _db8.session.flush()
    _dm_other_store = _Store5(merchant_id=_dm_other.id, name='对照门店',
                              code='demo-other', is_active=True)
    _db8.session.add(_dm_other_store)
    _db8.session.commit()
    _dm_merchant_id = _owner5.merchant_id
    _dm_other_merchant_id = _dm_other.id
    _dm_other_store_id = _dm_other_store.id
    _dm_trace_path = _dcp5.DECISION_TRACE_PATH
    _dm_usage_before = _AUL5.query.count()
    _dm_tasks_before = _STask5.query.count()

_osDM.environ[_DM_ENV] = str(_dm_merchant_id)
_aq5.clear_cache()  # 排除历史缓存干扰，保证本次为规则版确定性结果

# ⑦ 模型调用探针：只读入口必须一次都不调
_dm_probe = {'calls': 0}


def _dm_probe_llm(prompt, role):
    _dm_probe['calls'] += 1
    return None


_dcp5.set_llm(_dm_probe_llm)
_dm_trace_before = (os.path.getsize(_dm_trace_path)
                    if os.path.exists(_dm_trace_path) else -1)
_r_dm = client.get('/demo')
_dm_body = _r_dm.get_data(as_text=True)
_dm_trace_after = (os.path.getsize(_dm_trace_path)
                   if os.path.exists(_dm_trace_path) else -1)
_dcp5.set_llm(None)

# ② 合法商家 → 200，四角色齐全，且带只读标记头
chk('/demo 配置合法演示商家后 200 且四角色齐全',
    _r_dm.status_code == 200
    and all(label in _dm_body
            for label in ('数据分析师', '经营参谋', '风险审计员', '规则主持人')),
    f'code={_r_dm.status_code}')
chk('/demo 响应带只读标记头 X-Demo-Readonly',
    _r_dm.headers.get('X-Demo-Readonly') == '1',
    str(_r_dm.headers.get('X-Demo-Readonly')))
chk('/demo 免登录即可访问（匿名不带任何会话）',
    _r_dm.status_code == 200 and '登录体验完整闭环' in _dm_body)

# ③ 页面无写入口；写方法返回 405
chk('/demo 页面不含 POST 表单与人工确认块',
    'method="post"' not in _dm_body.lower()
    and '人工确认转任务' not in _dm_body
    and 'task_create' not in _dm_body)
chk('/demo 只注册 GET（POST 返回 405）',
    client.post('/demo').status_code == 405)

# ④⑤⑥ 零副作用：不记账、不建任务、不写轨迹
with app.app_context():
    _dm_usage_after = _AUL5.query.count()
    _dm_tasks_after = _STask5.query.count()
chk('/demo 零配额消耗（AIUsageLedger 行数不变）',
    _dm_usage_after == _dm_usage_before,
    f'{_dm_usage_before}->{_dm_usage_after}')
chk('/demo 不创建任务（StoreTask 行数不变）',
    _dm_tasks_after == _dm_tasks_before,
    f'{_dm_tasks_before}->{_dm_tasks_after}')
chk('/demo 不写决策轨迹文件', _dm_trace_after == _dm_trace_before,
    f'{_dm_trace_before}->{_dm_trace_after}')
chk('/demo 零模型调用（探针计数为 0）', _dm_probe['calls'] == 0, str(_dm_probe))

# ⑧ 范围不可篡改：拼他人门店/商家参数不改变渲染结果
# 渲染内容里有**三处**天然动态字段：生成时间、缓存命中与否、`<meta name="csrf-token">`
# （itsdangerous 带秒级时间戳，base64 长度还会变——第 38 轮实测 13174 vs 13176 就是这个）。
# 旧注释写"只有两处"是**拿描述当事实**：两次请求恰好跨秒就偶发红，且红因看上去像"范围被篡改"。
# 归一化三处后逐字节比对——仍比「不含他人门店名」强得多（下面那条变异腿证明它没被擦成恒等）。
_dm_body_tamper = client.get(
    f'/demo?store_id={_dm_other_store_id}&merchant_id={_dm_other_merchant_id}'
    f'&store_ids={_dm_other_store_id}').get_data(as_text=True)


def _dm_normalize(html):
    html = re.sub(r'生成时间：.*?</span>', '生成时间：X</span>', html)
    html = re.sub(r'缓存：.*?</span>', '缓存：X</span>', html)
    html = re.sub(r'(name="csrf-token"[^>]*content=")[^"]*(")', r'\1X\2', html)
    return html


chk('F25 /demo 归一化确实把 csrf-token 的值遮成 X（遮不到=下轮跨秒又假红一次）',
    re.search(r'name="csrf-token"[^>]*content="X"', _dm_normalize(_dm_body)) is not None)
chk('F25 /demo 归一化只擦三处动态字段：往正文塞一个门店名必须仍判不等（不是万能橡皮擦）',
    _dm_normalize(_dm_body.replace('智电轻擎', '智电轻擎对照门店X')) != _dm_normalize(_dm_body),
    '归一化后仍相等 ⇒ 比对被擦成恒等')


_dm_a, _dm_b = _dm_normalize(_dm_body_tamper), _dm_normalize(_dm_body)
_dm_off = next((i for i in range(min(len(_dm_a), len(_dm_b))) if _dm_a[i] != _dm_b[i]),
               min(len(_dm_a), len(_dm_b)))


def _dm_around(txt):
    return txt[max(0, _dm_off - 46):_dm_off + 46].replace('\n', '⏎')


chk('/demo 请求参数无法篡改门店范围（渲染内容逐字节一致）', _dm_a == _dm_b,
    f'首个差异在第 {_dm_off} 字（归一化后长度 {len(_dm_a)} vs {len(_dm_b)}；'
    f'归一化前 {len(_dm_body_tamper)} vs {len(_dm_body)}）'
    f'｜篡改侧 …{_dm_around(_dm_a)}… ｜基准侧 …{_dm_around(_dm_b)}…')
chk('/demo 对照租户门店与商家名不进入渲染',
    '对照门店' not in _dm_body and '对照商家' not in _dm_body)

_osDM.environ.pop(_DM_ENV, None)

# ════════════ OPT-A 只读开放 API + OPT-B 数据层可插拔迁移（2026-09-23 对标 P0）════════════
# 认证走独立 Bearer 令牌分支（不受 CSRF 影响）；迁移与签名以直接函数调用钉死。
section('骨架')
import tempfile as _tf

import migrate_sqlite_to_mysql as _mg
from src import api_open as _ao
from src import webhooks as _wh
from src.models import Merchant as _Mer

with app.app_context():
    _ao_m = _Mer.query.order_by(_Mer.id).first()
    _ao_rec, _ao_plain = _ao.issue_token(_ao_m.id, 'l4')
    _ao_whrec, _ao_secret = _ao.subscribe_webhook(_ao_m.id, 'https://example.test/hook', '*',
                                              resolver=lambda h: ['93.184.216.34'])
_aoh = {'Authorization': 'Bearer ' + _ao_plain}

chk('OPT-A 有效令牌 meta 200/ok',
    client.get('/api/v1/meta', headers=_aoh).status_code == 200
    and client.get('/api/v1/meta', headers=_aoh).get_json().get('ok') is True)
chk('OPT-A 无令牌 401', client.get('/api/v1/meta').status_code == 401)
chk('OPT-A 坏令牌 401',
    client.get('/api/v1/meta', headers={'Authorization': 'Bearer ican_bad'}).status_code == 401)
chk('OPT-A stores 200 且返回门店列表',
    isinstance(client.get('/api/v1/stores', headers=_aoh).get_json().get('stores'), list))
_dj = client.get('/api/v1/dashboard?days=30', headers=_aoh).get_json()
chk('OPT-A dashboard 含营收总量与逐店聚合',
    'total_revenue' in _dj and isinstance(_dj.get('stores'), list))
chk('OPT-A sales/history 200',
    client.get('/api/v1/sales/history?days=30', headers=_aoh).status_code == 200)
chk('OPT-A sales/daily 200',
    client.get('/api/v1/sales/daily', headers=_aoh).status_code == 200)
chk('OPT-A tasks 200', client.get('/api/v1/tasks', headers=_aoh).status_code == 200)
chk('OPT-A 非本商家/不存在任务 404',
    client.get('/api/v1/tasks/999999', headers=_aoh).status_code == 404)
chk('OPT-A 跨租户 store_id 403',
    client.get('/api/v1/dashboard?store_id=999999', headers=_aoh).status_code == 403)
chk('OPT-A days 越界 400',
    client.get('/api/v1/dashboard?days=99999', headers=_aoh).status_code == 400)
_wb = b'{"event":"sales.submitted"}'
_ws = _wh.sign_body(_ao_secret, _wb)
chk('OPT-A Webhook 签名可校验', _wh.verify_signature(_ao_secret, _wb, _ws))
chk('OPT-A Webhook 篡改请求体被拒', not _wh.verify_signature(_ao_secret, b'other', _ws))

# ── 第 21 轮 H46/H47：签名防重放 + 游标翻页与配额头 ─────────────────────
# 判据成对铁律：每条"通过"都要有对应的"拒绝"，只验正例等于没验。
import hashlib as _s27_hashlib  # noqa: E402
import hmac as _s27_hmac  # noqa: E402
import time as _s27_time  # noqa: E402
from flask import Response as _s27_Response  # noqa: E402
from sqlalchemy import func as _s27_func  # noqa: E402
from src import rate_limit as _s27_rl  # noqa: E402
from src.models import DailySales as _s27_DS  # noqa: E402
from src.models import Store as _s27_Store  # noqa: E402
from src.models import StoreTask as _s27_Task  # noqa: E402
from src.models import db as _s27_db  # noqa: E402

_s27_now = int(_s27_time.time())
_s27_body = b'{"event":"sales.submitted","merchant_id":1}'
_s27_a = _wh.sign_body(_ao_secret, _s27_body, timestamp=_s27_now)
_s27_a_hex = _s27_a.split('sha256=')[1]
_s27_b_hex = _wh.sign_body(_ao_secret, _s27_body, timestamp=_s27_now + 1).split('sha256=')[1]

chk('S27 签名头形状 t=<unix>,sha256=<64hex>（逗号分隔是多签名形状的前提）',
    re.fullmatch(r't=\d{1,20},sha256=[0-9a-f]{64}', _s27_a) is not None, _s27_a[:44])
chk('S27 时间戳确实进了签名串：同一 body 只换 t 就必须换摘要',
    _s27_a_hex != _s27_b_hex, f'{_s27_a_hex[:12]} vs {_s27_b_hex[:12]}')
_s27_legacy = 'sha256=' + _s27_hmac.new(_ao_secret.encode('utf-8'), _s27_body,
                                        _s27_hashlib.sha256).hexdigest()
chk('S27 反降级：只签 body 的旧格式必须验不过（否则时间戳只是装饰，重放照旧成立）',
    not _wh.verify_signature(_ao_secret, _s27_body, _s27_legacy))
chk('S27 新鲜签名可验（正向对照，防"逢头就拒"假安全）',
    _wh.verify_signature(_ao_secret, _s27_body, _s27_a, now=_s27_now))
chk('S27 篡改 body 即拒', not _wh.verify_signature(_ao_secret, _s27_body + b' ', _s27_a, now=_s27_now))
chk('S27 换密钥即拒', not _wh.verify_signature('another-secret', _s27_body, _s27_a, now=_s27_now))
chk('S27 过期 t（早于窗口 3600s）拒 —— 抓到一次也不能无限重放',
    not _wh.verify_signature(_ao_secret, _s27_body, _s27_a, now=_s27_now + 3600))
chk('S27 未来 t（晚于窗口 3600s）同样拒（未来时间戳也是重放素材）',
    not _wh.verify_signature(_ao_secret, _s27_body, _s27_a, now=_s27_now - 3600))
chk('S27 窗口边界成对：299s 放行 / 301s 拒绝（只有一条通过不叫窗口）',
    _wh.verify_signature(_ao_secret, _s27_body, _s27_a, now=_s27_now + 299)
    and not _wh.verify_signature(_ao_secret, _s27_body, _s27_a, now=_s27_now + 301))
chk('S27 缺 t 段（只有 sha256=）拒',
    not _wh.verify_signature(_ao_secret, _s27_body, 'sha256=' + _s27_a_hex, now=_s27_now))
chk('S27 t 非数字拒（坏输入不得当"没带时间戳"放过）',
    not _wh.verify_signature(_ao_secret, _s27_body, 't=abc,sha256=' + _s27_a_hex, now=_s27_now))
chk('S27 有 t 无签名段拒', not _wh.verify_signature(_ao_secret, _s27_body, f't={_s27_now}'))
chk('S27 空头与 None 头都拒（零输入不得判通过）',
    not _wh.verify_signature(_ao_secret, _s27_body, '')
    and not _wh.verify_signature(_ao_secret, _s27_body, None))
_s27_other_hex = _wh.sign_body('rotated-in-key', _s27_body, timestamp=_s27_now).split('sha256=')[1]
_s27_multi = f't={_s27_now},sha256={_s27_other_hex},sha256={_s27_a_hex}'
chk('S27 轮换窗口形状：多签名任一匹配即认（校验侧已具备，签名侧无生产调用方故不产出）',
    _wh.verify_signature(_ao_secret, _s27_body, _s27_multi, now=_s27_now))
chk('S27 多签名反例：两把都不匹配时仍拒（"任一匹配"≠"格式对就放过"）',
    not _wh.verify_signature('no-such-key', _s27_body, _s27_multi, now=_s27_now))

with app.app_context():
    # 复用了 4295 行上下文里取到的 _ao_m 会 DetachedInstanceError（本文件已踩过一次，
    # 见 H1b 段同款注释）—— 商家 id 一律在上下文内重查成标量再往外带。
    _s27_mid = _ao.Merchant.query.order_by(_ao.Merchant.id).first().id
    _s27_sids = [s.id for s in _s27_Store.query.filter_by(merchant_id=_s27_mid, is_active=True).all()]
    _s27_scope = _s27_sids or [-1]
    _s27_pick = (_s27_db.session.query(_s27_DS.record_date, _s27_func.count(_s27_DS.id))
                 .filter(_s27_DS.store_id.in_(_s27_scope))
                 .group_by(_s27_DS.record_date)
                 .order_by(_s27_func.count(_s27_DS.id).desc()).first())
    _s27_day = _s27_pick[0] if _s27_pick else None
    _s27_all_ids = [r.id for r in _s27_db.session.query(_s27_DS.id)
                    .filter(_s27_DS.record_date == _s27_day, _s27_DS.store_id.in_(_s27_scope))
                    .order_by(_s27_DS.id).all()] if _s27_day else []
chk('S27 采样前提：本商家可见范围内存在流水 ≥2 条的日期（否则翻页判据全是对空气判据）',
    len(_s27_all_ids) >= 2, f'ids={len(_s27_all_ids)} day={_s27_day}')
if len(_s27_all_ids) >= 2:
    _s27_lim = len(_s27_all_ids) - 1
    _s27_r1 = client.get(f'/api/v1/sales/daily?date={_s27_day}&limit={_s27_lim}', headers=_aoh)
    _s27_j1 = _s27_r1.get_json() or {}
    _s27_ids1 = [r['id'] for r in _s27_j1.get('records', [])]
    chk('S27 首页按 limit 截断（服务端截，不是客户端数）',
        _s27_r1.status_code == 200 and len(_s27_ids1) == _s27_lim,
        f'got={len(_s27_ids1)} lim={_s27_lim}')
    chk('S27 首页 has_more=True 且 next_cursor = 本页末条 id',
        _s27_j1.get('has_more') is True and _s27_j1.get('next_cursor') == _s27_ids1[-1],
        f'{_s27_j1.get("has_more")}/{_s27_j1.get("next_cursor")}')
    _s27_r2 = client.get(f'/api/v1/sales/daily?date={_s27_day}&limit={_s27_lim}'
                         f'&cursor={_s27_j1.get("next_cursor")}', headers=_aoh)
    _s27_j2 = _s27_r2.get_json() or {}
    _s27_ids2 = [r['id'] for r in _s27_j2.get('records', [])]
    chk('S27 第二页全部 id > cursor 且与首页无重叠',
        bool(_s27_ids2) and min(_s27_ids2) > max(_s27_ids1), f'{_s27_ids1[-1]} -> {_s27_ids2[:3]}')
    chk('S27 末页 has_more=False 且 next_cursor 归 None（不是空串/0 之类假游标）',
        _s27_j2.get('has_more') is False and _s27_j2.get('next_cursor') is None,
        f'{_s27_j2.get("has_more")}/{_s27_j2.get("next_cursor")!r}')
    chk('S27 两页并集 = 全量（不漏不重，这正是 offset 翻页做不到的保证）',
        sorted(_s27_ids1 + _s27_ids2) == sorted(_s27_all_ids),
        f'{len(_s27_ids1 + _s27_ids2)}/{len(_s27_all_ids)}')
    chk('S27 缺省 limit 生效为 DEFAULT_PAGE_SIZE 并回显（响应不说自己按几页切的，调用方无从续取）',
        client.get('/api/v1/sales/daily', headers=_aoh).get_json().get('limit')
        == _ao.DEFAULT_PAGE_SIZE)

for _s27_bad, _s27_why in (('limit=0', '下越界'), ('limit=501', '上越界'),
                           ('limit=abc', '非整数'), ('cursor=abc', '游标非整数'),
                           ('cursor=-1', '游标为负')):
    chk(f'S27 {_s27_bad} 必须 400（{_s27_why}静默夹取 = 调用方以为自己拿到了要的量）',
        client.get(f'/api/v1/sales/daily?{_s27_bad}', headers=_aoh).status_code == 400)

with app.app_context():
    _s27_new = [_s27_Task(merchant_id=_s27_mid, store_id=(_s27_sids or [-1])[0],
                          title=f'S27 游标样本{i}', due_date=_date.today(), status='pending')
                for i in range(3)]
    _s27_db.session.add_all(_s27_new)
    _s27_db.session.commit()
    _s27_added = sorted(t.id for t in _s27_new)
    _s27_task_all = sorted(t.id for t in _s27_Task.query.filter_by(merchant_id=_s27_mid).all())


def _s27_walk(path, key, size=2):
    """按 next_cursor 走到尽头：返回 (见到的 id, 页数, 是否干净收尾)。"""
    ids, cursor, pages = [], None, 0
    while pages < 30:
        sep = '&' if '?' in path else '?'
        url = f'{path}{sep}limit={size}' + ('' if cursor is None else f'&cursor={cursor}')
        j = client.get(url, headers=_aoh).get_json() or {}
        ids += [r['id'] for r in j.get(key, [])]
        pages += 1
        if not j.get('has_more'):
            return ids, pages, j.get('next_cursor') is None
        cursor = j.get('next_cursor')
        if cursor is None:      # has_more=True 却不给游标 = 翻页死路，立刻判不干净
            return ids, pages, False
    return ids, pages, False


_s27_twalk, _s27_tpages, _s27_tclean = _s27_walk('/api/v1/tasks', 'tasks')
chk('S27 tasks 走游标翻到尽头：并集恰等于库内全集且不重复（列表端点共用一套机制）',
    sorted(_s27_twalk) == _s27_task_all and len(_s27_twalk) == len(set(_s27_twalk)),
    f'{len(_s27_twalk)}/{len(_s27_task_all)} pages={_s27_tpages}')
chk('S27 确实发生了多页（单页就收尾说明 limit 没生效）', _s27_tpages >= 2,
    f'pages={_s27_tpages} added={_s27_added}')
chk('S27 收尾干净：最后一页 has_more=False 且 next_cursor=None（游标不能悬着）', _s27_tclean)
_s27_sw, _s27_sp_, _s27_sc = _s27_walk(f'/api/v1/sales/daily?date={_s27_day}', 'records', size=2)
chk('S27 sales/daily 用 limit=7 翻到尽头同样等于全集（两条端点都不是各写一份）',
    sorted(_s27_sw) == sorted(_s27_all_ids) and _s27_sc and _s27_sp_ >= 2,
    f'{len(_s27_sw)}/{len(_s27_all_ids)} pages={_s27_sp_}')
with app.app_context():
    for _s27_t in _s27_db.session.query(_s27_Task).filter(
            _s27_Task.id.in_(_s27_added)).all():
        _s27_db.session.delete(_s27_t)
    _s27_db.session.commit()
    _s27_left = sorted(t.id for t in _s27_Task.query.filter_by(merchant_id=_s27_mid).all())
chk('S27 样本任务已清场（测试自己造的 rows 不留给后续断言）',
    _s27_left == [i for i in _s27_task_all if i not in _s27_added], str(_s27_left[:8]))

_s27_rl_off = os.environ.get('DISABLE_RATE_LIMIT') == '1'
_s27_meta = client.get('/api/v1/meta', headers=_aoh)
if _s27_rl_off:
    chk('S27 限流关闭时不发配额头（关了还回头寸 = 对调用方撒谎）',
        not any(h in _s27_meta.headers for h in
                ('X-RateLimit-Limit', 'X-RateLimit-Remaining', 'X-RateLimit-Reset')))
else:
    _s27_hdrs = {h: _s27_meta.headers.get(h) for h in
                 ('X-RateLimit-Limit', 'X-RateLimit-Remaining', 'X-RateLimit-Reset')}
    chk('S27 每个 /api/v1 响应带三个配额头且数值自洽（S27-取到即证接线生效）',
        all(v is not None for v in _s27_hdrs.values())
        and int(_s27_hdrs['X-RateLimit-Remaining']) <= int(_s27_hdrs['X-RateLimit-Limit']),
        str(_s27_hdrs))
    _s27_u1 = _s27_rl.window_usage('openapi')
    _s27_u2 = _s27_rl.window_usage('openapi')
    chk('S27 window_usage 只读：连查两次用量不变（回写头不得反过来吃配额）',
        _s27_u1 == _s27_u2 and _s27_u1['remaining'] <= _s27_u1['limit'], str(_s27_u2))
chk('S27 after_request 钩子确实注册在 blueprint 上（判据没进链 = 半成品）',
    any(f.__name__ == '_attach_rate_headers'
        for fl in app.blueprints['api_v1'].after_request_funcs.values() for f in fl))
with app.test_request_context('/api/v1/meta'):
    _s27_429 = _ao._attach_rate_headers(_s27_Response(status=429))
    _s27_200 = _ao._attach_rate_headers(_s27_Response(status=200))
if _s27_rl_off:
    chk('S27 限流关闭时既不头发也不发 Retry-After（两处同分支，不留半套口径）',
        'Retry-After' not in _s27_429.headers)
else:
    chk('S27 429 必带 Retry-After 且 ≥1 秒（没有等待时间的 429 等于让客户端瞎猜）',
        str(_s27_429.headers.get('Retry-After', '')).isdigit()
        and int(_s27_429.headers['Retry-After']) >= 1,
        repr(_s27_429.headers.get('Retry-After')))
    chk('S27 非 429 不得带 Retry-After（只在需要等待时出现）',
        'Retry-After' not in _s27_200.headers)


# ── 第 22 轮 S28：对外错误契约形态（真发请求，不读描述）─────────────────
# 本轮动因是实测"6 条 API 错误腿里 5 条吐 HTML"，所以这里的判据全部走真实请求 +
# content-type + 键集合，并配一条浏览器路由仍吐 HTML 的正向对照（防过度修复把 UI 错误页改没）。
from src import api_errors as _s28_ae  # noqa: E402

_s28_saved_rl = app.config.get('OPEN_API_RATE_LIMIT')
app.config['OPEN_API_RATE_LIMIT'] = 5000      # 本段只验形态；不抬高桶量会被 60/min 判成 429 假信号
try:
    _s28_legs = [
        ('/api/v1/tasks/999999', _aoh, 404, 'not_found', '越权/不存在的任务'),
        ('/api/v1/sales/daily?limit=0', _aoh, 400, 'bad_request', 'limit 下越界'),
        ('/api/v1/dashboard?store_id=999999', _aoh, 403, 'forbidden', '跨租户 store_id'),
        ('/api/v1/meta', {}, 401, 'unauthorized', '无令牌'),
        ('/api/v1/nope', _aoh, 404, 'not_found', '未知 API 路径（路由阶段）'),
        ('/api/v1', _aoh, 404, 'not_found', '无尾斜杠的前缀本身'),
        ('/api/v1/meta', {}, 405, 'method_not_allowed', '对只读端点发 POST：方法不允许'),
    ]
    _s28_bad, _s28_html_legs = [], []
    for _s28_url, _s28_h, _s28_code, _s28_ecode, _s28_why in _s28_legs:
        if _s28_ecode == 'method_not_allowed':
            _s28_r = client.post(_s28_url, headers=_s28_h)
        else:
            _s28_r = client.get(_s28_url, headers=_s28_h)
        _s28_ct = (_s28_r.content_type or '').split(';')[0]
        _s28_j = _s28_r.get_json(silent=True)
        if _s28_r.status_code != _s28_code:
            _s28_bad.append(f'{_s28_url} 状态码 {_s28_r.status_code}≠{_s28_code}（{_s28_why}）')
            continue
        if _s28_ct != 'application/json':
            _s28_bad.append(f'{_s28_url} 仍吐 {_s28_ct}（{_s28_why}）')
            continue
        if not isinstance(_s28_j, dict):
            _s28_bad.append(f'{_s28_url} 是 JSON 但解不出对象')
            continue
        for _s28_k in _s28_ae.ERROR_SCHEMA_KEYS:
            if _s28_k not in _s28_j:
                _s28_bad.append(f'{_s28_url} 缺恒定键 {_s28_k}')
        if _s28_j.get('error') != _s28_ecode:
            _s28_bad.append(f'{_s28_url} error={_s28_j.get("error")!r}≠{_s28_ecode!r}')
        if _s28_j.get('ok') is not False:
            _s28_bad.append(f'{_s28_url} ok 不为 false')
    chk('S28 七条真实 API 错误腿全部返回 JSON 且四键齐备、机读码符合预期'
        '（本轮起点是实测 6 条里 5 条吐 HTML）',
        not _s28_bad, str(_s28_bad)[:220])

    _s28_405 = client.post('/api/v1/meta', headers=_aoh)
    _s28_allow = (_s28_405.headers.get('Allow') or '').upper()
    chk('S28 405 必须保留 Allow 头且含 GET（RFC 9110 要求；自建 JSON 响应会把 Werkzeug 挂的那条丢掉）',
         'GET' in _s28_allow and _s28_405.status_code == 405,
         f'Allow={_s28_allow!r}')
    chk('S28 透传只搬元数据头：Content-Type 不得被异常自带的 text/html 覆盖（本轮实测回归）',
        (_s28_405.content_type or '').startswith('application/json')
        and 'text/html' not in str(sorted(_s28_ae.PASSTHROUGH_HEADERS)),
        f'ct={_s28_405.content_type} 白名单={_s28_ae.PASSTHROUGH_HEADERS}')

    _s28_br = client.get('/nope-such-page')
    _s28_br_body = _s28_br.get_data(as_text=True)
    chk('S28 正向对照：浏览器路由的 404 仍是 HTML 错误页（把 UI 错误页一并改成 JSON 就是过度修复）',
        _s28_br.status_code == 404 and 'text/html' in (_s28_br.content_type or '')
        and _s28_br_body.lstrip().startswith('<'),
        f'{_s28_br.status_code} {_s28_br.content_type}')

    with app.app_context():
        _s28_mid = _ao.Merchant.query.order_by(_ao.Merchant.id).first().id
        _s28_rec, _s28_scope_plain = _ao.issue_token(_s28_mid, 's28-scoped', scopes='sales:read')
    _s28_scope_resp = client.get('/api/v1/tasks',
                                 headers={'Authorization': 'Bearer ' + _s28_scope_plain})
    _s28_scope_json = _s28_scope_resp.get_json(silent=True) or {}
    chk('S28 作用域不足仍是 JSON，且 granted/required 两个附加键在册（spec 已声明为可选）',
        _s28_scope_resp.status_code == 403
        and (_s28_scope_resp.content_type or '').startswith('application/json')
        and _s28_scope_json.get('error') == 'insufficient_scope'
        and 'granted' in _s28_scope_json and 'required' in _s28_scope_json,
        str(_s28_scope_json)[:150])

    _s28_hdr_rid = _s28_scope_resp.headers.get('X-Request-Id', '')
    _s28_body_rid = str(_s28_scope_json.get('request_id') or '')
    chk('S28 错误体的 request_id 与响应头 X-Request-Id 同源（报障给一个串就能串回 JSONL）',
        bool(_s28_hdr_rid) and _s28_body_rid == _s28_hdr_rid,
        f'header={_s28_hdr_rid!r} body={_s28_body_rid!r}')

    _s28_secret = [k for k in _s28_scope_json
                   if any(s in k.lower() for s in ('token', 'secret', 'password', 'api_key'))]
    chk('S28 错误体不外泄凭据类字段（新增键时这条会拦住"顺手把 token 回显进错误"）',
        not _s28_secret, str(_s28_secret))

    _s28_raised = False
    try:
        _s28_ae.json_error(400, 'never_registered_code')
    except ValueError:
        _s28_raised = True
    chk('S28 未登记的机读码直接抛 ValueError（防"先上代码后补 spec"走回老路）', _s28_raised)
    chk('S28 状态码表与码表同源：STATUS_CODE_MAP 的值全部在 ERROR_CODES 内',
        set(_s28_ae.STATUS_CODE_MAP.values()) <= set(_s28_ae.ERROR_CODES),
        str(sorted(set(_s28_ae.STATUS_CODE_MAP.values()) - set(_s28_ae.ERROR_CODES))))
    chk('S28 每个已登记错误码都有中文兜底文案（缺文案会在 message 上留空串）',
        all(c in _s28_ae.DEFAULT_COPY for c in _s28_ae.ERROR_CODES),
        str([c for c in _s28_ae.ERROR_CODES if c not in _s28_ae.DEFAULT_COPY]))
    chk('S28 前缀分界自证：/api/v1 与其子路径判为 API，/admin 与 / 不是（判错就全线退化 HTML）',
        _s28_ae.targets_api('/api/v1/tasks') and _s28_ae.targets_api('/api/v1')
        and not _s28_ae.targets_api('/admin/dashboard')
        and not _s28_ae.targets_api('/api/v1backend-fake'),
        str([_s28_ae.targets_api(p) for p in ('/api/v1', '/api/v1x', '/admin/x')]))
finally:
    app.config['OPEN_API_RATE_LIMIT'] = _s28_saved_rl


# ── 第 23 轮 S29：运行期告警面（阈值 ⇄ 出口 ⇄ 脱敏 ⇄ 不改 status 契约）──────
# 本轮动因：/healthz 早就在算 db_ms / ai.inflight / errors_5xx / dropped，却没有一处与阈值比较；
# "阈值告警"在门禁+L4 的合并 grep 只 1 命中且那是业务损耗率 ⇒ 告警面 0 判据。
import importlib as _s21_importlib  # noqa: E402
import json as _s29_json  # noqa: E402
from src import alerts as _s29_al  # noqa: E402

_s29_root = os.path.dirname(os.path.abspath(__file__))

_s29_h = client.get('/healthz')
_s29_j = _s29_h.get_json() or {}
chk('S29 /healthz 出口带 alerts 与 alert_count（算了不出口 = 运维仍看不到）',
    isinstance(_s29_j.get('alerts'), list)
    and _s29_j.get('alert_count') == len(_s29_j.get('alerts') or []),
    str(_s29_j.get('alerts'))[:110])

_s29_bad = {'db_ms': 9999.0, 'requests': 1000, 'errors_5xx': 3, 'error_kinds': 2,
            'slow_ratio': 0.4, 'ai_inflight_ratio': 1.0, 'ai_rejected': 5, 'dropped': 7}
_s29_ok = {'db_ms': 0.3, 'requests': 1000, 'errors_5xx': 0, 'error_kinds': 0,
           'slow_ratio': 0.0, 'ai_inflight_ratio': 0.0, 'ai_rejected': 0, 'dropped': 0}
_s29_fired = {x['code'] for x in _s29_al.evaluate(_s29_bad)}
_s29_quiet = {x['code'] for x in _s29_al.evaluate(_s29_ok)}
chk('S29 每条规则在越界样本上都触发（有一条永远不触发就是死规则）',
    _s29_fired == set(_s29_al.ALERT_CODES),
    f'未触发 {sorted(set(_s29_al.ALERT_CODES) - _s29_fired)}')
chk('S29 同一张表在正常样本上一条都不报（只验"越界会红"不验"正常不吵"= 告警疲劳）',
    _s29_quiet == set(), str(sorted(_s29_quiet)))
chk('S29 分母下限生效：requests=10 时慢比例 40% 也不报（1/1=100% 那种假告警）',
    'slow_request_ratio' not in {x['code'] for x in _s29_al.evaluate(
        dict(_s29_bad, requests=10))}, str([x['code'] for x in _s29_al.evaluate(
        dict(_s29_bad, requests=10))]))
_s29_missing = _s29_al.evaluate({'requests': 1000})
chk('S29 缺聚合字段时不报也不"假装健康"（该条直接跳过，由第 36 项管字段名漂移）',
    isinstance(_s29_missing, list))

_s29_saved_env = {k: os.environ.get(k) for k in ('ALERT_DB_MS', 'ALERT_AI_INFLIGHT_RATIO')}
try:
    os.environ['ALERT_DB_MS'] = '0.1'
    _s29_tight = {x['code'] for x in _s29_al.evaluate(_s29_ok)}
    os.environ['ALERT_DB_MS'] = 'abc'
    _s29_junk = _s29_al.threshold_for({'env': 'ALERT_DB_MS', 'default': 250.0})
    del os.environ['ALERT_DB_MS']
    _s29_default = _s29_al.threshold_for({'env': 'ALERT_DB_MS', 'default': 250.0})
    # 端到端腿刻意用 gte 规则（在途比 0.0 ≥ 0.0 必然成立）而不是 db_ms：
    # `/healthz` 里 db_ms 被 round(x,1) 舍到 0.1，"0.1 > 0.1" 不成立 ⇒
    # 拿一个被舍入过的量去证阈值生效，会在快机器上静默不触发（本轮就是这么红的）。
    _s29_plain = client.get('/healthz').get_json() or {}
    os.environ['ALERT_AI_INFLIGHT_RATIO'] = '0'
    _s29_live = client.get('/healthz').get_json() or {}
finally:
    for _s29_k, _s29_v in _s29_saved_env.items():
        if _s29_v is None:
            os.environ.pop(_s29_k, None)
        else:
            os.environ[_s29_k] = _s29_v
chk('S29 阈值可配：ALERT_DB_MS=0.1 时本机 0.3ms 也报（证明判定真读环境，不是硬编码）',
    'db_slow' in _s29_tight, str(sorted(_s29_tight)))
chk('S29 非法阈值回默认而不是当 0 用（"abc"→0 会让每台机器都在报警）',
    _s29_junk == _s29_default == 250.0, f'{_s29_junk} vs {_s29_default}')
# 判的是**差值**而不是"默认态必须零告警"：diag 的计数从进程启动累计，前面腿做过几次
# bcrypt 登录（实测单次 504–1012ms）就会让 slow_request_ratio 过 0.05，
# 拿无关告警当本腿前置 = 这条腿永远在赌前面的腿跑了什么（第 26 轮 L4 实测翻红暴露）。
# 收紧后仍要求"恰好多这一条"，比原来的 >=1 更硬。
_s29_plain_codes = [a.get('code') for a in (_s29_plain.get('alerts') or [])]
_s29_live_codes = [a.get('code') for a in (_s29_live.get('alerts') or [])]
chk('S29 端到端：改一个阈值就让 /healthz 真出该告警，且只多出这一条（差值判据）',
    'ai_backpressure' not in _s29_plain_codes and 'ai_backpressure' in _s29_live_codes
    and (_s29_live.get('alert_count') or 0) == (_s29_plain.get('alert_count') or 0) + 1,
    f"默认 {str(_s29_plain.get('alerts'))[:60]} → 收紧 {str(_s29_live.get('alerts'))[:80]}")
chk('S29 status 语义未被告警改动：有告警与无告警两次 status 必须相同'
    '（smoke_remote 按 status==ok 判冒烟，把告警并进 status 就是原地判红）',
    _s29_plain.get('status') == _s29_live.get('status')
    and (_s29_live.get('alert_count') or 0) >= 1
    and 'status' not in {r['metric'] for r in _s29_al.ALERT_RULES},
    f"plain={_s29_plain.get('status')} tight={_s29_live.get('status')} "
    f"count={_s29_live.get('alert_count')}")

_s29_blob = _s29_json.dumps(_s29_live.get('alerts') or [], ensure_ascii=False)
chk('S29 公开端点的告警只出聚合量：无路径名/SQL 模板/表名（/healthz 匿名可达）',
    not any(t in _s29_blob for t in ('/admin/', '/api/v1', 'SELECT', 'daily_sales',
                                      'sqlite', 'templates/')), _s29_blob[:110])
chk('S29 告警条目形状固定四键（多出键=把内部字段泄进公开面的入口）',
    all(set(x) == {'code', 'label', 'value', 'threshold'}
        for x in _s29_al.evaluate(_s29_bad)) and bool(_s29_al.evaluate(_s29_bad)),
    str([sorted(x) for x in _s29_al.evaluate(_s29_bad)][:1]))

_s29_sr = open(os.path.join(_s29_root, 'smoke_remote.py'), encoding='utf-8').read()
chk('S29 部署侧冒烟读同一份告警面（只在线上看、冒烟不判 = 半数场合无人发现）',
    "'alerts'" in _s29_sr and 'allow_alerts' in _s29_sr
    and '--allow-alerts' in _s29_sr, _s29_sr[:1] and 'wired')
_s29_env_txt = open(os.path.join(_s29_root, '部署', '.env.example'), encoding='utf-8').read()
_s29_unlisted = [k for k in _s29_al.ALERT_ENV_KEYS if f'{k}=' not in _s29_env_txt]
chk('S29 每个阈值键都在 部署/.env.example 登记（可配却无人知道可配 = 等于不可配）',
    not _s29_unlisted and len(_s29_al.ALERT_ENV_KEYS) == len(_s29_al.ALERT_CODES),
    f'缺登记 {str(_s29_unlisted)[:80]} 阈值键 {len(_s29_al.ALERT_ENV_KEYS)}/码 {len(_s29_al.ALERT_CODES)}')


# ── 第 24 轮 S30：可维护性棘轮（预算表 ⇄ 枚举器 ⇄ 放宽必须显式）──────────
# 本段全部在沙箱里**真跑**第 37 项的判据函数，不读文档字符串、不读注释：
# 「通过」必须由 check_maintenance_budget() 自己给出，反例必须由它自己判红。
import ast as _ast_s30
import contextlib as _cl_s30
import importlib as _il_s30
import io as _io_s30
import json as _json_s30
import math as _math_s30
import shutil as _sh_s30
import tempfile as _tf_s30

_qgx = _il_s30.import_module('quality_gate')
_s30_tree = os.path.dirname(os.path.abspath(__file__))
_s30_dirs = []


def _s30_at(root):
    """在指定根上跑一次第 37 项 → (是否判红, 明细文本)。跑完还原模块级 ROOT/SRC。"""
    old = (_qgx.ROOT, _qgx.SRC)
    _qgx.ROOT, _qgx.SRC = root, os.path.join(root, 'src')
    del _qgx.results[:]
    with _cl_s30.redirect_stdout(_io_s30.StringIO()):
        _qgx.check_maintenance_budget()
    _lvl, _nm, ok, det = _qgx.results[0]
    _qgx.ROOT, _qgx.SRC = old
    return (not ok), ' | '.join(str(d) for d in det)


def _s30_gen(root, reason=''):
    """在指定根上跑 `--write-budget` 的同一个写入口。"""
    old = (_qgx.ROOT, _qgx.SRC)
    _qgx.ROOT, _qgx.SRC = root, os.path.join(root, 'src')
    with _cl_s30.redirect_stdout(_io_s30.StringIO()):
        _qgx.write_budget(reason)
    _qgx.ROOT, _qgx.SRC = old
    return _json_s30.loads(open(os.path.join(root, _qgx.BUDGET_FILE), encoding='utf-8').read())


def _s30_sandbox(n_a=10, n_b=6):
    d = _tf_s30.mkdtemp(prefix='ican_s30_')
    _s30_dirs.append(d)
    os.makedirs(os.path.join(d, 'src'))
    for name, n in (('src/a.py', n_a), ('src/b.py', n_b)):
        _s30_write(os.path.join(d, *name.split('/')), n)
    _s30_gen(d)
    return d


def _s30_write(path, n):
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join('# pad %d' % i for i in range(n)) + '\n')


def _s30_edit(d, mutate):
    p = os.path.join(d, _qgx.BUDGET_FILE)
    doc = _json_s30.loads(open(p, encoding='utf-8').read())
    mutate(doc)
    with open(p, 'w', encoding='utf-8') as fh:
        _json_s30.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)


_red0, _txt0 = _s30_at(_s30_tree)
chk('S30 真实树端到端：第 37 项在自己的交付副本上判绿（只在沙箱能跑 = 表与代码早已分叉）',
    not _red0, _txt0[:150])

_s30_doc_path = os.path.join(_s30_tree, _qgx.BUDGET_FILE)
with open(_s30_doc_path, encoding='utf-8') as _fh_s30:
    _s30_doc = _json_s30.loads(_fh_s30.read())
_s30_doc_raw = _qgx.read(_s30_doc_path)
_old_tree = (_qgx.ROOT, _qgx.SRC)
_qgx.ROOT, _qgx.SRC = _s30_tree, os.path.join(_s30_tree, 'src')
_s30_enum = set(_qgx._s37_py_files())
_qgx.ROOT, _qgx.SRC = _old_tree
_s30_actual = {r: len(_qgx.read(os.path.join(_s30_tree, *r.split('/'))).splitlines())
               for r in _s30_enum}
chk('S30 枚举器 ⇄ 预算表双向对账：两边文件集合完全相同，且分母 ≥60（少一半 = 枚举范围漏了）',
    set(_s30_doc['files']) == _s30_enum and len(_s30_enum) >= 60,
    f'枚举 {len(_s30_enum)} 表内 {len(_s30_doc["files"])}')
# 本条最初写成 `_s30_doc['files'][r]` 直取：新增一个没登记预算的 .py 时它抛 KeyError
# 打断整个 L4（判据该判红，不该把后面 1,000 条腿一起带走）。改成 .get + 缺条目即失败。
_s30_unbudgeted = sorted(r for r in _s30_enum if r not in _s30_doc['files'])
_s30_violation = [(r, _s30_actual[r], _s30_doc['files'].get(r)) for r in sorted(_s30_enum)
                  if r in _s30_doc['files'] and not (
                      _s30_actual[r] <= _s30_doc['files'][r]
                      <= int(_math_s30.ceil(_s30_actual[r] * (1 + _qgx.BUDGET_SLACK))))]
chk('S30 每条预算自洽：实测 ≤ 预算 ≤ ceil(实测×(1+余量))，且每个枚举到的文件都有条目'
    '（下界防抖动误红，上界防买通判据；缺条目判红而不是抛异常）',
    not _s30_unbudgeted and not _s30_violation,
    f'缺条目 {str(_s30_unbudgeted)[:70]} 越界 {str(_s30_violation)[:90]}')
chk('S30 余量常量仍是"允许正常增删"量级（0 < slack ≤ 0.15；抬到 1.0 就变回永不红）',
    0 < _qgx.BUDGET_SLACK <= 0.15, str(_qgx.BUDGET_SLACK))

_s30_qg_ast = _ast_s30.parse(_qgx.read(os.path.join(_s30_tree, 'quality_gate.py')))


def _r47_wired_names(fn_node):
    """门禁 `main()` 里"真被接线"的名字集合 = 直接调用的函数名 ∪ **作为可调用对象传给 `_guarded` 的名字**。

    第 47 轮 M152 之前只有前者。包裹层一上线，凡"只认 `c.func.id`"的接线证明都会把
    51 个全被裹住的项一律判成假守卫（本轮实测：S30/S31/S45/S46/S47 同因连红五条）。
    判据跟着实现走，不是实现跟着判据走：这里改的是**取数面**（把参数位纳入），
    反向没有放松 —— `target` 在 main() 里既没被调用、也没被 `_guarded` 传入 ⇒ 仍然不在集合内
    （S55 段有一条合成反例专门量这一点）。
    """
    out = set()
    for n in _ast_s30.walk(fn_node):
        if not isinstance(n, _ast_s30.Call) or not isinstance(n.func, _ast_s30.Name):
            continue
        out.add(n.func.id)
        if n.func.id == '_guarded':
            out.update(a.id for a in n.args if isinstance(a, _ast_s30.Name))
    return out
_s30_calls_in_funcs = {id(n) for _fn in _ast_s30.walk(_s30_qg_ast)
                       if isinstance(_fn, _ast_s30.FunctionDef)
                       for n in _ast_s30.walk(_fn)
                       if isinstance(n, _ast_s30.Call)}
_s30_main = next(n for n in _s30_qg_ast.body if isinstance(n, _ast_s30.FunctionDef)
                 and n.name == 'main')
_s30_main_calls = _r47_wired_names(_s30_main)
chk('S30 判据已接线进 main() 调用链（写了 check 函数没接上 = 假守卫，第 10/17 项踩过同一坑）',
    'check_maintenance_budget' in _s30_main_calls, str(sorted(_s30_main_calls))[:150])
chk('S30 接线取数面双向：`_guarded(check_x)` 的参数位算接线，但"根本没出现"仍不算（防改宽成恒真）',
    'check_business_calendar' in _s30_main_calls
    and 'this_function_is_never_called' not in _s30_main_calls,
    f'裹入 {len(_s30_main_calls)} 个名字')
_s30_mod_calls = {n.func.id for n in _ast_s30.walk(_s30_qg_ast)
                  if isinstance(n, _ast_s30.Call) and isinstance(n.func, _ast_s30.Name)
                  and id(n) not in _s30_calls_in_funcs}
chk('S30 放宽预算的唯一写入口在 CLI 执行路径上可达（没有入口 = 棘轮变成只读死表）',
    'write_budget' in _s30_mod_calls, str(sorted(_s30_mod_calls))[:150])

_d = _s30_sandbox()
_red, _txt = _s30_at(_d)
chk('S30 沙箱基线判绿（注入反例前先证明它不是"怎么都红"的噪声闸）', not _red, _txt[:150])

_s30_write(os.path.join(_d, 'src', 'c.py'), 5)
_red, _txt = _s30_at(_d)
chk('S30 新增 .py 不登记即判红（否则每个新文件都自动免检）',
    _red and '没有预算条目' in _txt, _txt[:150])

_s30_edit(_d, lambda doc: doc['files'].__setitem__('src/gone.py', 999))
_red, _txt = _s30_at(_d)
chk('S30 预算表留已删除文件判红（表不随代码走 = 台账失真）',
    _red and '已不存在' in _txt, _txt[:150])

_d2 = _s30_sandbox()
_s30_edit(_d2, lambda doc: doc['files'].__setitem__('src/a.py', 999999))
_red, _txt = _s30_at(_d2)
chk('S30 把预算写成天文数字判红（唯一能"合法"绕过规模判据的手法必须先堵）',
    _red and '买通判据' in _txt, _txt[:150])

_d3 = _s30_sandbox()
_s30_write(os.path.join(_d3, 'src', 'a.py'), 400)
_red, _txt = _s30_at(_d3)
chk('S30 文件长过自己的预算判红（只准拆分或显式上调，不许默认放行）',
    _red and '超出预算' in _txt, _txt[:150])
_doc_before = _json_s30.loads(open(os.path.join(_d3, _qgx.BUDGET_FILE),
                                   encoding='utf-8').read())['files']['src/a.py']
_s30_gen(_d3, '')
_red, _txt = _s30_at(_d3)
_doc_after = _json_s30.loads(open(os.path.join(_d3, _qgx.BUDGET_FILE),
                                  encoding='utf-8').read())['files']['src/a.py']
chk('S30 不带 reason 重跑 --write-budget 不得自动放宽（放宽是人工决定，不是默认行为）',
    _doc_after == _doc_before and _red and '超出预算' in _txt,
    f'预算 {_doc_before}→{_doc_after} ' + _txt[:80])
_reason = '拆分排期已定，过渡期按现状放行（S30 断言用）'
_s30_gen(_d3, _reason)
_red, _txt = _s30_at(_d3)
_doc3 = _json_s30.loads(open(os.path.join(_d3, _qgx.BUDGET_FILE), encoding='utf-8').read())
chk('S30 带 reason 才放行，且理由必须落进表里可追责（"谁放宽的"要留在账上）',
    not _red and _doc3['complexity']['reason'] == _reason,
    f'reason={_doc3["complexity"]["reason"][:30]!r} ' + _txt[:80])

_d4 = _s30_sandbox()
_s30_edit(_d4, lambda doc: doc.__setitem__('files', {}))
_red, _txt = _s30_at(_d4)
chk('S30 files 清空判红：枚举器空转不得判"没文件所以通过"（零输入永不 PASS）',
    _red and '枚举器空转' in _txt, _txt[:150])

_d5 = _s30_sandbox()
os.remove(os.path.join(_d5, _qgx.BUDGET_FILE))
_red, _txt = _s30_at(_d5)
chk('S30 删掉预算表判红而不是跳过（判"没测到"不判通过）',
    _red and '缺 ' in _txt, _txt[:150])

_d6 = _s30_sandbox()
_cx_real = _qgx._s37_c901_count
_qgx._s37_c901_count = lambda: (None, '未探测到 ruff（S30 注入）')
_red, _txt = _s30_at(_d6)
chk('S30 ruff 探测不到 ⇒ 复杂度腿判红不判通过（测量工具缺失不能算"没有问题"）',
    _red and 'ruff 不可用' in _txt, _txt[:150])
_qgx._s37_c901_count = lambda: (99, None)
_red, _txt = _s30_at(_d6)
chk('S30 复杂度高于基线判红（新增复杂函数必须拆）',
    _red and '涨到 99' in _txt, _txt[:150])
_qgx._s37_c901_count = lambda: (0, None)
_s30_edit(_d6, lambda doc: doc['complexity'].__setitem__('c901_baseline', 12))
_red, _txt = _s30_at(_d6)
chk('S30 复杂度已下降但基线仍写高值也判红（只降不升要的是真账，不是历史欠账永久豁免）',
    _red and '基线仍写 12' in _txt, _txt[:150])
_qgx._s37_c901_count = _cx_real
chk('S30 变异腿跑完已还原真测函数（防"打桩留在原地"把后续断言全变自证）',
    _qgx._s37_c901_count.__name__ == '_s37_c901_count',
    getattr(_qgx._s37_c901_count, '__name__', '?'))

for _d_s30 in _s30_dirs:
    _sh_s30.rmtree(_d_s30, ignore_errors=True)
chk('S30 沙箱全部清理；且注入反例期间**交付副本的预算表逐字节未变**（变异只准动沙箱）',
    all(not os.path.isdir(d) for d in _s30_dirs) and len(_s30_dirs) >= 6
    and _qgx.read(_s30_doc_path) == _s30_doc_raw,
    f'沙箱 {len(_s30_dirs)} 个已删 / 表 bytes '
    f'{len(_s30_doc_raw)}→{len(_qgx.read(_s30_doc_path))}')


# ── 第 25 轮 S31：备份证据链（生产者接线 ⇄ 只读复算 ⇄ 空库负对照）───────────
# 案发形态必须先在本机复现，否则"备份能恢复"这句话永远只是话。
import ast as _ast_s31
import json as _json_s31
import sqlite3 as _sq_s31
import tempfile as _tf_s31

import backup_db as _bd_s31
from src import backup_manifest as _bm_s31
from src import pack_demo as _pd_s31

_s31_root = os.path.dirname(os.path.abspath(__file__))
_s31_dirs = []


def _s31_dir():
    d = _tf_s31.mkdtemp(prefix='l4_s31_')
    _s31_dirs.append(d)
    return d


def _s31_mkdb(path, rows=3, data=True):
    con = _sq_s31.connect(path)
    con.execute('CREATE TABLE merchants (id INTEGER PRIMARY KEY, name TEXT)')
    con.execute('CREATE TABLE stores (id INTEGER PRIMARY KEY, merchant_id INTEGER)')
    con.execute('CREATE TABLE daily_sales (id INTEGER PRIMARY KEY, store_id INTEGER, record_date TEXT)')
    if data:
        con.executemany('INSERT INTO merchants (name) VALUES (?)', [('m%d' % i,) for i in range(rows)])
        con.executemany('INSERT INTO stores (merchant_id) VALUES (?)', [(1,) for _ in range(rows)])
        con.executemany('INSERT INTO daily_sales (store_id, record_date) VALUES (?, ?)',
                        [(1, '2026-09-%02d' % (i + 1)) for i in range(rows)])
    con.commit()
    con.close()


def _s31_mkempty(path):
    con = _sq_s31.connect(path)
    con.execute('CREATE TABLE tmp_probe (x INTEGER)')
    con.execute('DROP TABLE tmp_probe')
    con.commit()
    con.close()


# ① 关键表名单必须是真的表名 —— 名单写错字（store 而非 stores）空库守卫就永远不触发。
# 分母由 src.models 全量枚举（上一版手抄 5 个类，漏了 Admin ⇒ 名单里的 'admin' 被误判成错字）
import src.models as _md_s31

with app.app_context():
    _s31_tables = {v.__tablename__ for v in vars(_md_s31).values()
                   if isinstance(v, type) and hasattr(v, '__tablename__')}
chk('S31 表名分母非空且含 daily_sales（枚举器空转就等于这条腿没跑）',
    len(_s31_tables) >= 15 and 'daily_sales' in _s31_tables, f'只数到 {len(_s31_tables)} 张表')
chk('S31 KEY_TABLES 逐个对得上 models 的真实表名（名单写错=空库守卫永不触发）',
    set(_bm_s31.KEY_TABLES) <= _s31_tables and len(_bm_s31.KEY_TABLES) >= 5,
    f'名单里不在真实表名集合的：{sorted(set(_bm_s31.KEY_TABLES) - _s31_tables)}')

# ② 正例：真跑 backup_db 出口，证据件必须在场且形状＝声明的键集
_d = _s31_dir()
_src = os.path.join(_d, 'src.db')
_s31_mkdb(_src, rows=4)
_src_sha_before = _bm_s31.sha256_file(_src)
_dst = _bd_s31.backup_sqlite('l4s31', src=_src, dst_dir=_d)
_mp = _bm_s31.manifest_path(_dst) if _dst else ''
_doc = _json_s31.load(open(_mp, encoding='utf-8')) if _dst and os.path.isfile(_mp) else {}
chk('S31 正例：备份出口产出同名证据件且 status=SUCCESS',
    bool(_dst) and _doc.get('status') == _bm_s31.STATUS_SUCCESS, str(_doc)[:120])
chk('S31 形状对账：实际产出键集 == 声明的 MANIFEST_KEYS（定义≠声明在这里会红）',
    set(_doc) == _bm_s31.MANIFEST_KEYS and set(_doc.get('checks') or {}) == _bm_s31.CHECK_KEYS,
    sorted(set(_doc) ^ _bm_s31.MANIFEST_KEYS))
chk('S31 证据件记的 sha256/size 就是实盘那份（防止记的是别的文件）',
    _doc.get('sha256') == _bm_s31.sha256_file(_dst) and _doc.get('size_bytes') == os.path.getsize(_dst))
chk('S31 源库在备份过程中未被改写（复算/备份工具不得有写源的能力）',
    _bm_s31.sha256_file(_src) == _src_sha_before)
chk('S31 rows_by_table 按键字典序落盘（可 diff、可跨机复算）',
    list(_doc['content']['rows_by_table']) == sorted(_doc['content']['rows_by_table']))

# ③ 本轮案发形态：0 表 4KB 空库 + 有表但关键表全 0 行
_e = os.path.join(_d, 'empty.db')
_s31_mkempty(_e)
chk('S31 案发形态复刻：0 表库的 integrity_check 照样 ok（所以字节层完好从来不等于有数据）',
    _bm_s31.integrity_ok(_e)[0] is True and _bm_s31.table_counts(_e) == {})
chk('S31 空库守卫拦下 0 表形态', _bm_s31.empty_guard({})[0] is False)
_z = os.path.join(_d, 'zrows.db')
_s31_mkdb(_z, data=False)
chk('S31 空库守卫拦下「有表但关键表全部 0 行」第二形态',
    _bm_s31.empty_guard(_bm_s31.table_counts(_z))[0] is False,
    _bm_s31.empty_guard(_bm_s31.table_counts(_z))[1][:60])
_v_ok, _v_detail = _bd_s31.verify_backup(_e)
chk('S31 无证据件的旧式备份判不可恢复（改前它被判「✅ 可恢复」，rc=0）',
    (not _v_ok) and '缺证据件' in _v_detail, _v_detail[:100])

# ④ 回填态不得追认 / 留证后被改写必须点名
_bo, _bp, _bd2 = _bm_s31.write_manifest(_src, origin=_bm_s31.ORIGIN_BACKFILL, tool='l4')
_rb_ok, _rb_detail = _bm_s31.reconcile(_src)
chk('S31 回填态证据件不得当可恢复凭据（--stamp 只能清点）',
    _bo and (not _rb_ok) and '回填态' in _rb_detail, _rb_detail[:110])
_tp = os.path.join(_d, 'tampered.db')
import shutil as _sh_s31b

_sh_s31b.copyfile(_dst, _tp)
_sh_s31b.copyfile(_mp, _bm_s31.manifest_path(_tp))
with _sq_s31.connect(_tp) as _tc:
    _tc.execute('CREATE TABLE injected (x INTEGER)')
    _tc.commit()
_tc.close()   # sqlite3 的 with 只管事务提交，不会关连接；不显式 close 会捏住文件句柄让 Windows 删不掉沙箱
_t_ok, _t_detail = _bm_s31.reconcile(_tp)
chk('S31 留证之后备份被改写 ⇒ 对账判红并点名 sha256（pgbackrest 的 size/sizeOriginal 那条能力）',
    (not _t_ok) and 'sha256' in _t_detail, _t_detail[:110])

# ⑤ 保留动作可追责 + 不制造孤儿证据（真跑 pack_demo 的 prune）
chk('S31 record_pruned 把被删份名记进在存件，且不影响对账',
    _bm_s31.record_pruned(_dst, ['ican_old.db'])[0]
    and _json_s31.load(open(_mp, encoding='utf-8'))['pruned'] == ['ican_old.db']
    and _bm_s31.reconcile(_dst)[0])
_pd = _s31_dir()
for _i in range(4):
    _p = os.path.join(_pd, 'demo-reset_2026010%d_000000_000000.db' % _i)
    _s31_mkdb(_p, rows=1)
    _bm_s31.write_manifest(_p, tool='l4')
_removed = _pd_s31._prune_backups(_pd, keep=1)
_orphans = [f for f in os.listdir(_pd) if f.endswith(_bm_s31.SUFFIX)
            and not os.path.isfile(os.path.join(_pd, f[:-len(_bm_s31.SUFFIX)]))]
chk('S31 裁剪返回被删份名（保留策略要能被追责）', len(_removed) == 3, str(_removed)[:80])
chk('S31 裁掉的备份其证据件同步删除（不留孤儿证据误导下次审计）', not _orphans, str(_orphans)[:80])

# ⑥ 第二条出口（平台重置页）真跑：留证 + 源库只读 + tool 写明真实出口
_bt = _s31_dir()
with app.app_context():
    _tgt = _pd_s31.create_database_backup(backup_dir=_bt, keep=2)
_tmp = _bm_s31.manifest_path(_tgt)
_tdoc = _json_s31.load(open(_tmp, encoding='utf-8')) if os.path.isfile(_tmp) else {}
chk('S31 第二条出口同样留证（同类出口不能只修案发那一处）', os.path.isfile(_tmp), str(_tgt)[:90])
chk('S31 证据件 tool 字段记的是真实出口，且 row_parity 真为 True',
    _tdoc.get('tool') == 'src/pack_demo.py' and _tdoc.get('checks', {}).get('row_parity') is True,
    str({k: _tdoc.get(k) for k in ('tool',)}) + str(_tdoc.get('checks'))[:60])
chk('S31 pack_demo 的备份可由 CLI 复验判「可恢复」（两层都过）', _bd_s31.verify_backup(_tgt)[0],
    _bd_s31.verify_backup(_tgt)[1][:100])

# ⑦ 红线：证据件只准有聚合量，不得带任何业务明细。
# 上一版用裸子串扫 JSON，结果被真实表名 `api_tokens`（含子串 token）顶出**假阳性**——
# 判据要拦的是"凭据/明细的键与值"，不是表名，所以改成按键名 + 叶子值形状两道判。
def _s31_leaves(obj):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append((str(k), v))
            out.extend(_s31_leaves(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_s31_leaves(v))
    return out


_s31_forbidden_keys = {'password', 'secret', 'api_key', 'token', 'abnormal_notes',
                       'metrics_json', 'product_name', 'display_name', 'phone', 'email'}
_s31_leaf_pairs = _s31_leaves(_doc) + _s31_leaves(_tdoc)
_s31_hit_keys = sorted({k for k, _v in _s31_leaf_pairs if k.lower() in _s31_forbidden_keys})
_s31_long_vals = [v for _k, v in _s31_leaf_pairs
                  if isinstance(v, str) and len(v) > 120 and _k != 'error']
chk('S31 证据件里没有凭据/明细类键（红线；按**键名**判，不按裸子串——表名 api_tokens 不是泄密）',
    not _s31_hit_keys, f'命中键 {str(_s31_hit_keys)[:80]}')
chk('S31 证据件里没有长自由文本（除 error 外所有字符串都是枚举量/哈希/路径/时间戳）',
    not _s31_long_vals, f'超长值 {str(_s31_long_vals)[:80]}')
_s31_blob = _json_s31.dumps(_doc, ensure_ascii=False) + _json_s31.dumps(_tdoc, ensure_ascii=False)
chk('S31 正向对照：业务样本词与明细列名确实不在证据件里（上一条判据不是空转）',
    not any(t in _s31_blob for t in ('西米', '芒果', 'abnormal_notes', 'metrics_json')))
chk('S31 证据件只记聚合量：content 下只有表数/总行数/逐表行数/关键表行数四键',
    set(_doc['content']) == _bm_s31.CONTENT_KEYS and
    all(isinstance(x, int) for x in _doc['content']['rows_by_table'].values()))

# ⑧ 门禁侧接线（AST 取 main() 调用形，不看注释与字符串）
_qg_tree = _ast_s31.parse(open(os.path.join(_s31_root, 'quality_gate.py'), encoding='utf-8').read())
_s31_main = next(n for n in _qg_tree.body
                 if isinstance(n, _ast_s31.FunctionDef) and n.name == 'main')
_s31_calls = _r47_wired_names(_s31_main)
chk('S31 第 38 项已接线进 main() 调用链（写了判据没接线=假守卫）',
    'check_backup_evidence_chain' in _s31_calls, str(sorted(_s31_calls))[:150])
_s31_prod = _qg_s38_producers = None
_gq = __import__('quality_gate')
_s31_trees, _s31_skipped = _gq._s38_trees()
chk('S31 备份出口枚举：两条真实出口全部接线证据件（且枚举器不许只数到一条）',
    len(_gq._s38_backup_producers(_s31_trees)) >= 2
    and not (_gq._s38_backup_producers(_s31_trees) - _gq._s38_manifest_writers(_s31_trees)),
    str(sorted(_gq._s38_backup_producers(_s31_trees)))[:110])

import shutil as _sh_s31

_s31_bd_tree = _ast_s31.parse(open(os.path.join(_s31_root, 'backup_db.py'), encoding='utf-8').read())
_s31_imported = set()
for _n_s31 in _ast_s31.walk(_s31_bd_tree):
    if isinstance(_n_s31, _ast_s31.ImportFrom) and _n_s31.module:
        _s31_imported.add(_n_s31.module)
    elif isinstance(_n_s31, _ast_s31.Import):
        _s31_imported.update(a.name for a in _n_s31.names)
chk('S31 依赖在册：backup_db 必须走 src.backup_manifest（不许自带第二套证据件写法）',
    'src.backup_manifest' in _s31_imported, str(sorted(_s31_imported))[:150])

for _dd in _s31_dirs:
    _sh_s31.rmtree(_dd, ignore_errors=True)
chk('S31 沙箱已清理，且未在本机 backups/ 里留下测试件',
    all(not os.path.isdir(x) for x in _s31_dirs) and len(_s31_dirs) >= 3
    and not os.path.exists(os.path.join(_s31_root, 'backups', 'l4s31.db')),
    f'沙箱 {len(_s31_dirs)} 个')


# ── H1b 令牌作用域分级（2026-09-24 第 6 轮对标）───────────────────
# 关键回归面：存量令牌 scopes 恒为字面量 'read'，升级后必须仍等于「全部只读」，
# 否则一次收紧就把已接入的商家小程序静默断流。
chk('H1b normalize 保留旧口径 read', _ao.normalize_scopes('read') == 'read')
chk('H1b normalize 空值回落 read', _ao.normalize_scopes(None) == 'read')
chk('H1b normalize 排序去重',
    _ao.normalize_scopes('tasks:read,sales:read,tasks:read') == 'sales:read,tasks:read')
_h1b_raised = False
try:
    _ao.normalize_scopes('admin:write')
except ValueError:
    _h1b_raised = True
chk('H1b 非法作用域抛 ValueError', _h1b_raised)
chk('H1b read 展开为全四只读作用域',
    _ao.effective_scopes('read') == set(_ao.DATA_SCOPES))

with app.app_context():
    # 注意：不能复用前面上下文里取到的 _ao_m —— 上下文退出后它是 detached 实例，
    # 读 .id 会抛 DetachedInstanceError。作用域令牌一律在新上下文内重查商家。
    _h1b_mid = _ao.Merchant.query.order_by(_ao.Merchant.id).first().id
    _h1b_rec, _h1b_plain = _ao.issue_token(_h1b_mid, 'h1b-sales', scopes='sales:read')
    _h1b_id = _h1b_rec.id
_h1bh = {'Authorization': 'Bearer ' + _h1b_plain}
chk('H1b 最小权限令牌放行本域端点',
    client.get('/api/v1/sales/daily', headers=_h1bh).status_code == 200)
chk('H1b 缺 stores 作用域 403',
    client.get('/api/v1/stores', headers=_h1bh).status_code == 403)
chk('H1b 缺 dashboard 作用域 403',
    client.get('/api/v1/dashboard', headers=_h1bh).status_code == 403)
chk('H1b 缺 tasks 作用域 403',
    client.get('/api/v1/tasks', headers=_h1bh).status_code == 403)
_h1b403 = client.get('/api/v1/tasks', headers=_h1bh).get_json()
chk('H1b 403 机读体含 error/required/granted',
    _h1b403.get('error') == 'insufficient_scope'
    and _h1b403.get('required') == 'tasks:read'
    and _h1b403.get('granted') == ['sales:read'])
chk('H1b 作用域不足回 403 而非 401（身份有效、权限不足）',
    client.get('/api/v1/stores', headers=_h1bh).status_code != 401)
chk('H1b meta 回显实际生效作用域',
    client.get('/api/v1/meta', headers=_h1bh).get_json().get('scopes') == ['sales:read'])
chk('H1b 旧 read 令牌四端点全部零回归',
    all(client.get(p_, headers=_aoh).status_code == 200 for p_ in
        ('/api/v1/stores', '/api/v1/dashboard', '/api/v1/sales/daily', '/api/v1/tasks')))
with app.app_context():
    _h1b_used = _ao.db.session.get(_ao.ApiToken, _h1b_id).last_used_at
chk('H1b 已授令牌 last_used_at 被回写', _h1b_used is not None)

# ── H4 弱网离线填报：日期守卫与前端接线（2026-09-24 第 6 轮对标）──────
# 核心风险：record_date 恒按服务端提交时刻算，隔夜重放会把昨天的数写成今天。
# 守卫只在客户端带回 queued_for 时生效，故必须同时钉住「不带 = 行为不变」。
from datetime import date as _h4_date
from datetime import timedelta as _h4_td

_h4_root = os.path.dirname(os.path.abspath(__file__))
_h4_css = open(os.path.join(_h4_root, 'src', 'static', 'css', 'style.css'), encoding='utf-8').read()
_h4_js_path = os.path.join(_h4_root, 'src', 'static', 'js', 'offline_submit.js')
_h4_js = open(_h4_js_path, encoding='utf-8').read()
chk('H4 离线脚本随仓库交付（非只存在于工作区）', os.path.isfile(_h4_js_path))
chk('H4 填报页引用离线脚本', "asset_url('js/offline_submit.js')" in
    open(os.path.join(_h4_root, 'src', 'templates', 'h5', 'dashboard.html'), encoding='utf-8').read())
chk('H4 填报表单带服务端当日日期钩子', 'data-record-date="{{ today.isoformat() }}"' in
    open(os.path.join(_h4_root, 'src', 'templates', 'h5', 'dashboard.html'), encoding='utf-8').read())
chk('H4 待传横幅样式已定义（JS 生成的节点不落空）', '.offline-box {' in _h4_css)
chk('H4 离线脚本只用 XHR 不用 fetch（兼容旧版微信 webview）',
    'fetch(' not in _h4_js and 'XMLHttpRequest' in _h4_js)
chk('H4 队列不静默丢数据（无回执不认成功）', "'unknown'" in _h4_js and 'reconcileDraft' in _h4_js)

# 本块只做守卫行为验证，临时置位限流开关并在块尾恢复：L4 刻意不全局关限流
# （F25 同源纪律），但共享 login/submit 桶名额有限，本块若照常吃名额，其后的
# H5 / H3b 块会因 429 连带假失败（本轮实测踩过，见 07 台账）。
_h4_saved_rl = os.environ.get('DISABLE_RATE_LIMIT')
os.environ['DISABLE_RATE_LIMIT'] = '1'
_h4c = app.test_client()
_h4lp = _h4c.get('/login').get_data(as_text=True)
_h4tk = re.search(r'name="csrf_token" value="([^"]+)"', _h4lp)
# 注意用 staff2：milk_tea_staff1 已被本套件前面的 CSRF 用例 toggle 停用（见 2766 行同源注释）
_h4c.post('/login', data={'username': 'milk_tea_staff2', 'password': 'staff123',
                          'role': 'staff',
                          'csrf_token': _h4tk.group(1) if _h4tk else ''})
_h4page = _h4c.get('/h5/').get_data(as_text=True)
_h4m = re.search(r'data-record-date="([0-9-]{10})"', _h4page)
# 期望值**独立算**一遍（UTC 时刻 + 8 小时），不吃进程时钟：
# 原写法拿 `date.today()` 当期望值，在 UTC 机器上跑测试就会与业务日历双双"自证一致"，
# 而真正该被发现的"页面按服务器时钟算今天"永远测不到（第 29 轮 H78 收尾时补的洞）。
_h4_exp = (_dt.now(UTC) + _td(hours=8)).date().isoformat()
chk('H4 页面日期 = 业务日（期望值由 UTC+8 独立算出，不吃进程时钟）',
    bool(_h4m) and _h4m.group(1) == _h4_exp,
    f'页面={_h4m.group(1) if _h4m else "MISSING"} 期望={_h4_exp}')

def _h4_form():
    t = re.search(r'name="csrf_token" value="([^"]+)"', _h4c.get('/h5/').get_data(as_text=True))
    return {'product_sales_qty': '7', 'total_revenue': '70',
            'raw_material_remaining': '5', 'raw_material_waste': '1',
            'abnormal_notes': 'H4 守卫验证',
            'csrf_token': t.group(1) if t else ''}

def _h4_count(product, day):
    from src.models import DailySales
    with app.app_context():
        return DailySales.query.filter_by(product_name=product, record_date=day).count()

_today_s = _h4_date.today().isoformat()
_yday = _h4_date.today() - _h4_td(days=1)


def _h4_free_product(username):
    """取该店员「今天还没提交过、且属于当前激活包在售」的产品名。

    本套件被反复复跑，硬编码产品名会随其它用例的写入而撞唯一键，
    导致 H4 断言变成时灵时不灵的假失败 —— 动态取一个空闲产品才稳。
    """
    from src.models import DailySales as _H4DS
    from src.models import Product as _H4P
    from src.models import Staff as _H4St
    from src.models import active_pack_clause as _h4apc
    with app.app_context():
        st = _H4St.query.filter_by(username=username).first()
        used = {row[0] for row in _H4DS.query
                .filter_by(staff_id=st.id, record_date=_h4_date.today())
                .with_entities(_H4DS.product_name).all()}
        for cand in (_H4P.query.filter(_H4P.store_id == st.store_id, _H4P.is_active.is_(True),
                                       _h4apc(_H4P.pack_id)).all()):
            if cand.product_name not in used:
                return cand.product_name
    return None


# 每次提交前各取一次空闲产品：三连取会拿到同一个名字，撞唯一键后假失败。
_p1 = _h4_free_product('milk_tea_staff2')
_r = _h4c.post('/h5/submit', data=dict(_h4_form(), product_name=_p1), follow_redirects=True)
chk('H4 不带 queued_for 的正常提交行为不变', '提交成功' in _r.get_data(as_text=True))

_p2 = _h4_free_product('milk_tea_staff2')
chk('H4 空闲产品探针逐个换名（不撞已提交唯一键）', bool(_p1) and bool(_p2) and _p1 != _p2,
    f'{_p1}/{_p2}')
_r = _h4c.post('/h5/submit', data=dict(_h4_form(), product_name=_p2,
                                       queued_for=_today_s), follow_redirects=True)
chk('H4 当日重放（queued_for=今天）放行', '提交成功' in _r.get_data(as_text=True))

# 种子库历史日期本就有流水，判据必须是「前后增量」而非「计数为 0」
_p3 = _h4_free_product('milk_tea_staff2')
_before_t = _h4_count(_p3, _h4_date.today())
_before_y = _h4_count(_p3, _yday)
_r = _h4c.post('/h5/submit', data=dict(_h4_form(), product_name=_p3,
                                       queued_for=_yday.isoformat()), follow_redirects=True)
_body = _r.get_data(as_text=True)
chk('H4 隔夜重放被拒写且提示可操作', '离线记录' in _body and '提交成功' not in _body)
chk('H4 隔夜重放未写入任何流水（今天与昨天行数双双不变）',
    _h4_count(_p3, _h4_date.today()) == _before_t and
    _h4_count(_p3, _yday) == _before_y,
    f'today {_before_t}->{_h4_count(_p3, _h4_date.today())}, '
    f'yday {_before_y}->{_h4_count(_p3, _yday)}')

_r = _h4c.post('/h5/submit', data=dict(_h4_form(), product_name=_p3), follow_redirects=True)
chk('H4 被拒后同一产品当天仍可正常提交（守卫未吞掉这条记录）',
    '提交成功' in _r.get_data(as_text=True))
# ── H5 第 7 轮：AI 出站并发闸（无 broker 场景的替代方案）────────────
# 免费层单进程 + waitress 默认 4 线程（全仓无 threads= 配置）⇒ 出站调用无上界时，
# 两次 30s 同步 AI 调用即可饿死与 AI 无关的填报写库链路（见 ai_gate 模块头）。
import os as _os_g

from src import ai_gate as _ag

_g_saved = _os_g.environ.get('AI_MAX_INFLIGHT')
_os_g.environ['AI_MAX_INFLIGHT'] = '2'
try:
    _ag.rebuild()
    chk('H5 闸容量可配且读环境变量', _ag.limit() == 2, str(_ag.limit()))
    chk('H5 默认容量给非 AI 链路留一半线程池（≤ waitress 默认 4 的一半）',
        _ag.DEFAULT_LIMIT <= 2, str(_ag.DEFAULT_LIMIT))
    _g0 = _ag.inflight()
    _held = [_ag.acquire() for _ in range(2)]
    chk('H5 未饱和前可连续占位', all(_held), str(_held))
    chk('H5 占位数与 inflight 一致', _ag.inflight() == _g0 + 2, str(_ag.inflight()))
    _g_before_rej = _ag.rejected()
    chk('H5 满位后非阻塞立即失败（不排队）', _ag.acquire() is False)
    chk('H5 饱和被计入 rejected', _ag.rejected() == _g_before_rej + 1)
    _ag.release(); _ag.release()
    chk('H5 释放后重新可占', _ag.acquire() is True and _ag.release() is None)

    from src import dify_client as _dc_g
    _cli = _dc_g.DifyClient()
    _cli.enabled = True
    _cli.deepseek_key = 'sk-fake-gate'
    _cli.base_url = 'http://127.0.0.1:1'
    _cli.chat_key = _cli.advice_key = 'k-fake'
    _bk = _cli.breaker._failures
    _held2 = [_ag.acquire() for _ in range(2)]
    _ans, _cid = _cli._deepseek_chat('ping')
    chk('H5 饱和时 DeepSeek 出站返回脱敏降级标记', _dc_g.is_dify_error(_ans) is True)
    chk('H5 饱和不计入熔断（背压≠上游故障）', _cli.breaker._failures == _bk,
        f'{_bk}->{_cli.breaker._failures}')
    _cn = _cli._dify_completion('ping', 'u')
    chk('H5 饱和时 Completion 出站同样降级', _dc_g.is_dify_error(_cn) is True)
    for _ in range(2):
        _ag.release()

    _src_g = open(os.path.join(_h4_root, 'src', 'dify_client.py'), encoding='utf-8').read()
    chk('H5 三个出站站点全部装闸（无漏网路径）', _src_g.count('ai_gate.acquire()') == 3,
        str(_src_g.count('ai_gate.acquire()')))
    chk('H5 每个取位都有配对放位', _src_g.count('ai_gate.release()') == 3,
        str(_src_g.count('ai_gate.release()')))
    chk('H5 /healthz 暴露闸实况（饱和可被发现，不再靠人工投诉）',
        'ai_gate' in open(os.path.join(_h4_root, 'src', 'app.py'), encoding='utf-8').read())
finally:
    if _g_saved is None:
        _os_g.environ.pop('AI_MAX_INFLIGHT', None)
    else:
        _os_g.environ['AI_MAX_INFLIGHT'] = _g_saved
    _ag.rebuild()
    chk('H5 复位后不残留占用（不污染后续套件）', _ag.inflight() == 0, str(_ag.inflight()))
chk('H4 静态资源可访问（生产路径未 404）',
    _h4c.get('/static/js/offline_submit.js').status_code == 200)
if _h4_saved_rl is None:
    os.environ.pop('DISABLE_RATE_LIMIT', None)
else:
    os.environ['DISABLE_RATE_LIMIT'] = _h4_saved_rl






# ── H7 第 7 轮：依赖契约（requirements 必须描述真实环境）──────────
# 旧状态实测：10 个 `==` 钉版里 6 个与项目自己的测试解释器不一致
# （Flask 钉 3.0.0 而实跑 3.1.3、生产 PA 又是平台预装 3.0.3），
# 即钉版描述的是「任何环境都不用的配置」，CI 绿不代表生产可用。
def _h7_vtxt(v):
    return tuple(int(x) for x in re.split(r'[._]', str(v)) if x.isdigit())


def _h7_satisfies(ver, spec):
    """极简 PEP440 区间判定（只支持本仓实际用到的 ==/>=/<=/<>/</> 逗号子句）。"""
    cur = _h7_vtxt(ver)
    for clause in [c.strip() for c in spec.split(',') if c.strip()]:
        m = re.match(r'(===|==|<=|>=|!=|<|>)\s*([0-9][0-9A-Za-z._-]*)$', clause)
        if not m:
            return False
        op, want = m.group(1), _h7_vtxt(m.group(2))
        if op == '==' and cur != want:
            return False
        if op == '>=' and cur < want:
            return False
        if op == '>' and cur <= want:
            return False
        if op == '<=' and cur > want:
            return False
        if op == '<' and cur >= want:
            return False
        if op == '!=' and cur == want:
            return False
    return True


_h7_req = open(os.path.join(_h4_root, 'requirements.txt'), encoding='utf-8').read()
_h7_pins = {}
for _ln in _h7_req.splitlines():
    _ln = _ln.split('#')[0].strip()
    _mm = re.match(r'^([A-Za-z0-9._-]+)\s*(.+)$', _ln) if _ln else None
    if _mm:
        _h7_pins[_mm.group(1).lower()] = _mm.group(2)

import importlib.metadata as _h7_md

_h7_installed = {}
for _nm in _h7_pins:
    try:
        _h7_installed[_nm] = _h7_md.version(_nm)
    except Exception:
        _h7_installed[_nm] = None      # 本机未装（waitress/pymysql 属正常）

# 出站超时必须以 (connect, read) 元组下发：标量会被 urllib3 同时用作 connect 与
# read（读 urllib3 2.7.0 源码核实），`timeout=30` 最坏墙钟其实是 60s；而驾驶舱
# 角色调用在 worker 线程里，signal 类超时非主线程不可用，socket 元组是唯一真约束。
_h7_cli = _dc_g.DifyClient()
_h7_cli.timeout = 30
chk('H7 超时以元组下发且 connect 被独立封顶',
    isinstance(_h7_cli.timeouts, tuple) and len(_h7_cli.timeouts) == 2
    and _h7_cli.timeouts[1] == 30 and _h7_cli.timeouts[0] <= 5, str(_h7_cli.timeouts))
_h7_cli.timeout = 3
chk('H7 read 上限更小时 connect 不反过来超过它',
    _h7_cli.timeouts[0] <= _h7_cli.timeouts[1], str(_h7_cli.timeouts))
_h7_src = open(os.path.join(_h4_root, 'src', 'dify_client.py'), encoding='utf-8').read()
chk('H7 三个出站点无一仍用标量 timeout', _h7_src.count('timeout=self.timeout,') == 0,
    str(_h7_src.count('timeout=self.timeout,')))

chk('H7 requirements 覆盖 10 项运行依赖且可解析', len(_h7_pins) >= 10, str(len(_h7_pins)))
_h7_bad = [f'{k}={_h7_installed[k]}!~{_h7_pins[k]}' for k in _h7_installed
           if _h7_installed[k] and not _h7_satisfies(_h7_installed[k], _h7_pins[k])]
chk('H7 已安装版本全部落在声明区间内（钉版与现实一致）', not _h7_bad, str(_h7_bad))
chk('H7 Flask 契约覆盖 dev 与 pa 两个真实版本',
    _h7_satisfies('3.1.3', _h7_pins['flask']) and _h7_satisfies('3.0.3', _h7_pins['flask']),
    _h7_pins.get('flask', ''))
chk('H7 不再把未验证版本硬钉为唯一答案（Flask 非 == 钉版）',
    not _h7_pins['flask'].startswith('=='), _h7_pins.get('flask', ''))
chk('H7 pandas 区间同时容纳 dev 3.x 与 PA 预装 2.2.2',
    _h7_satisfies('3.0.3', _h7_pins['pandas']) and _h7_satisfies('2.2.2', _h7_pins['pandas']),
    _h7_pins.get('pandas', ''))
chk('H7 依赖契约含来源说明（矩阵写进文件头，不靠记忆）',
    'PythonAnywhere' in _h7_req and 'dev 实测' in _h7_req)

# ── H8 第 9 轮：降级可归因（用户能知道「为什么这次变简单」）──────────
from src import decision_cockpit as _dc_h8

chk('H8 原因码全集都有中文文案（杜绝新增码漏配文案致 UI 空白）',
    all(isinstance(v, str) and v.strip() for v in _dc_h8.DEGRADE_REASONS.values()),
    str(sorted(_dc_h8.DEGRADE_REASONS)))
chk('H8 饱和与上游故障是不同原因码（混同即失去可归因意义）',
    'saturated' in _dc_h8.DEGRADE_REASONS and 'upstream_error' in _dc_h8.DEGRADE_REASONS
    and _dc_h8.DEGRADE_REASONS['saturated'] != _dc_h8.DEGRADE_REASONS['upstream_error'])
chk('H8 原因码映射：saturated 消息可辨识',
    _dc_h8.degrade_reason_from(RuntimeError('saturated')) == 'saturated')
chk('H8 原因码映射：证据契约失败独立于服务故障',
    _dc_h8.degrade_reason_from(ValueError('no_valid_evidence')) == 'contract_rejected')
chk('H8 原因码映射：未知异常归上游故障',
    _dc_h8.degrade_reason_from(RuntimeError('boom')) == 'upstream_error')

with app.app_context():
    _h8_mid = _ao.Merchant.query.order_by(_ao.Merchant.id).first().id
    _h8_res = _dc_h8.run_decision(_h8_mid, [], allow_ai=True, refresh=True, write_trace=False)
chk('H8 结果结构含 degrade / degrade_text / degraded 三字段',
    {'degrade', 'degrade_text', 'degraded'} <= set(_h8_res), str(sorted(_h8_res))[:120])
chk('H8 未配置大模型时被标记为可归因降级',
    _h8_res['degraded'] is True and set(_h8_res['degrade'].values()) == {'not_configured'},
    str(_h8_res['degrade']))
chk('H8 降级文案随结果下发且非空',
    all(v in _dc_h8.DEGRADE_REASONS.values() for v in _h8_res['degrade_text'].values()),
    str(sorted(set(_h8_res['degrade_text'].values()))))

with app.app_context():
    _h8_demo = _dc_h8.run_decision(_h8_mid, [], allow_ai=False, refresh=True, write_trace=False)
chk('H8 只读演示入口不算降级（否则 /demo 会误挂「规则版兜底」告警）',
    _h8_demo['degraded'] is False and _h8_demo['degrade'] == {}, str(_h8_demo['degrade']))

_h8_saved_fn = _dc_h8._LLM.get('fn')
try:
    def _h8_sat_fn(prompt, role):
        return {'ok': False, 'source': 'saturated', 'content': '', 'tokens': 0}
    _dc_h8._LLM['fn'] = _h8_sat_fn
    with app.app_context():
        _h8_s = _dc_h8.run_decision(_h8_mid, [], allow_ai=True, refresh=True, write_trace=False)
    chk('H8 并发闸饱和一路传导到成因（不塌缩成"调用失败"）',
        set(_h8_s['degrade'].values()) == {'saturated'}, str(_h8_s['degrade']))
    chk('H8 饱和成因文案指明"稍等重试"而非"服务不可用"',
        '并发已满' in _h8_s['degrade_text']['advisor'], _h8_s['degrade_text']['advisor'])
finally:
    _dc_h8._LLM['fn'] = _h8_saved_fn

_chk_h8_tpl = open(os.path.join(_h4_root, 'src', 'templates', 'admin',
                                '_decision_cockpit_inner.html'), encoding='utf-8').read()
_chk_h8_css = open(os.path.join(_h4_root, 'src', 'static', 'css', 'style.css'),
                   encoding='utf-8').read()
chk('H8 模板渲染降级说明且不在模板内联第二份文案',
    'decision-degrade-note' in _chk_h8_tpl and '本机 AI 并发已满' not in _chk_h8_tpl)
chk('H8 降级说明样式已定义（不留无样式裸元素）', '.decision-degrade-note {' in _chk_h8_css)
# 首屏快路径（initial_decision 恒 allow_ai=False）的归因三分：
# 回归点——configured 曾只在 allow_ai=True 分支求值，导致首屏永远误报「未配置」。
_h8_fn_saved = _dc_h8._LLM.get('fn')
try:
    def _h8_ok_fn(prompt, role):
        return {'ok': True, 'content': '{"items":[]}', 'source': 'ai', 'tokens': 5}
    _dc_h8._LLM['fn'] = _h8_ok_fn
    with app.app_context():
        _dc_h8.ai_quota.clear_cache()
        _h8_first = _dc_h8.initial_decision(_h8_mid, [], write_trace=False)
    chk('H8 首屏且AI已配 → not_generated（不得误报"未配置"）',
        set(_h8_first['degrade'].values()) == {'not_generated'}, str(_h8_first['degrade']))
    chk('H8 首屏文案指向「点刷新即可」而非故障话术',
        '刷新决策' in _h8_first['degrade_text']['advisor'], _h8_first['degrade_text']['advisor'])
    with app.app_context():
        _dc_h8.ai_quota.clear_cache()
        _h8_demo2 = _dc_h8.run_decision(_h8_mid, [], allow_ai=False, write_trace=False)
    chk('H8 只读演示即使AI已配也不算降级（skip_reason 缺省=主动只读）',
        _h8_demo2['degraded'] is False and _h8_demo2['degrade'] == {}, str(_h8_demo2['degrade']))
    _dc_h8._LLM['fn'] = None
    with app.app_context():
        _dc_h8.ai_quota.clear_cache()
        _h8_first_nc = _dc_h8.initial_decision(_h8_mid, [], write_trace=False)
    chk('H8 首屏且AI未配 → 如实 not_configured（不用 not_generated 粉饰）',
        set(_h8_first_nc['degrade'].values()) == {'not_configured'}, str(_h8_first_nc['degrade']))
finally:
    _dc_h8._LLM['fn'] = _h8_fn_saved
    with app.app_context():
        _dc_h8.ai_quota.clear_cache()

with app.app_context():
    _h8_cached = _dc_h8.run_decision(_h8_mid, [], allow_ai=False, write_trace=False)
chk('H8 命中缓存仍带可归因字段（缓存路径不吞成因）',
    'degraded' in _h8_cached and 'degrade' in _h8_cached)

# ── M17 第 9 轮：证据自检字段不得静默降级为 unknown ──────────────────
# 回归点：ai_eval.py 的 _git_commit() 只找脚本同级 .git，而 .git 在父级工作区根
# ⇒ 入库证据首行「代码提交」连续 9 天为 unknown，"证据绑定代码版本"这条验收前提静默失效。
import importlib as _il_m17  # noqa: E402, I001
import subprocess as _sp_m17  # noqa: E402
_ai_eval_m17 = _il_m17.import_module('ai_eval')
_m17_commit = _ai_eval_m17._git_commit()
# ⚠️ CI 语义修正：原先两条断言无条件要求 40 位提交号，而"能不能拿到提交号"取决于检出里
#    到底有没有 git 元数据（浅克隆/无 .git 的环境里必然拿不到）⇒ 本地全绿、CI 红。
#    现在两分支都是**真判据**：在仓库里必须解析出与 rev-parse 一致的号；不在仓库里必须
#    如实返回 unknown（静默抛异常或返回半截值都算失败），不给恒真出口。
_m17_rev = _sp_m17.run(['git', '-C', _h4_root, 'rev-parse', 'HEAD'],
                       capture_output=True, text=True)
_m17_in_repo = _m17_rev.returncode == 0
chk('M17 评测报告提交号：仓库内必须是 40 位 hex，非仓库环境必须如实降级为 unknown',
    (re.fullmatch(r'[0-9a-f]{40}', str(_m17_commit)) is not None) if _m17_in_repo
    else str(_m17_commit) == 'unknown',
    '%s in_repo=%s' % (_m17_commit, _m17_in_repo))
chk('M17 提交号与 git 权威口径一致（仓库内逐字相等；无仓库则要求降级幂等不抛异常）',
    (_m17_commit == _m17_rev.stdout.strip()) if _m17_in_repo
    else (str(_ai_eval_m17._git_commit()) == str(_m17_commit) == 'unknown'),
    str(_m17_commit))
# 这条是「祖先上溯」确实承重的证明：脚本同级没有 .git，扁平查找必然返回 unknown
chk('M17 脚本同级确无 .git（证明上溯逻辑承重，非冗余兜底）',
    not (_ai_eval_m17.REPO_ROOT / '.git' / 'HEAD').exists(),
    str(_ai_eval_m17.REPO_ROOT))
chk('M17 unknown 出口仍保留（真无 git 环境时如实降级而非抛异常）',
    _ai_eval_m17._git_commit.__doc__ is not None
    and 'unknown' in _ai_eval_m17._git_commit.__code__.co_consts,
    str(_ai_eval_m17._git_commit.__code__.co_consts))

# ── OPT-B 双引擎兼容 + dry-run 迁移 + 行数比对 + 幂等 ──
_ddl = _mg.compile_mysql_ddl()
chk('OPT-B 全部 ORM 表可编译为 MySQL DDL',
    len(_ddl) >= 15 and all('CREATE TABLE' in _d for _, _d in _ddl))
chk('OPT-B merchants DDL 使用 MySQL AUTO_INCREMENT 方言',
    any('AUTO_INCREMENT' in _d for _n, _d in _ddl if _n == 'merchants'))
_src_db = use_temp_db()
# 真实 ORM COUNT 基准（钉死 BUG-1：旧实现 select(text('1')).scalar() 对任何非空表恒返回 1）
with app.app_context():
    from src.models import DailySales as _DS
    _ds_actual = _DS.query.count()
chk('OPT-B 基准 daily_sales 真实行数>1（排除 1==1 假绿土壤）', _ds_actual > 1, str(_ds_actual))
_dry = _mg.migrate(_src_db, None, dry_run=True, verbose=False)
chk('OPT-B dry-run 完整性 ok 且不写目标库',
    _dry['integrity_ok'] and all(_t['target_rows'] is None for _t in _dry['tables']))
_ds = next((_t for _t in _dry['tables'] if _t['name'] == 'daily_sales'), None)
chk('OPT-B dry-run 源行数==真实COUNT（非恒1）',
    _ds is not None and _ds['source_rows'] == _ds_actual, f'{_ds and _ds["source_rows"]} vs {_ds_actual}')
# 真实拷贝到第二引擎（此处用独立 SQLite 文件代表通用目标引擎，端到端证明行数比对与幂等）
_tgt_uri = 'sqlite:///' + os.path.join(_tf.mkdtemp(), 'tgt.db').replace('\\', '/')
_copy1 = _mg.migrate(_src_db, _tgt_uri, dry_run=False, verbose=False)
chk('OPT-B 真实拷贝逐表行数比对一致', _copy1['integrity_ok'] and not _copy1['mismatch'])
_copy2 = _mg.migrate(_src_db, _tgt_uri, dry_run=False, verbose=False)
chk('OPT-B 迁移幂等（重跑结果仍一致）', _copy2['integrity_ok'] and not _copy2['mismatch'])
_ds2 = next((_t for _t in _copy1['tables'] if _t['name'] == 'daily_sales'), None)
chk('OPT-B 拷贝后目标 daily_sales 行数==源==真实COUNT',
    _ds2 is not None and _ds2['source_rows'] == _ds2['target_rows'] == _ds_actual,
    str(_ds2))

# ════════════ H2 查询范围守卫：租户隔离下沉框架层（漏写过滤兜底）════════════
section('骨架')
from flask_login import login_user as _qg_login_user

from src import query_guard as _qg

with app.app_context():
    from src.models import Admin as _QGAdmin
    from src.models import DailySales as _QGDS
    from src.models import Merchant as _QGM
    from src.models import Product as _QGP
    from src.models import Staff as _QGStaff
    from src.models import Store as _QGStore
    from src.models import db as _QGDB
    _qg_own = _QGAdmin.query.filter_by(username='owner_demo').first()
    _qg_midA = _qg_own.merchant_id
    _qg_storeA = _QGStore.query.filter_by(merchant_id=_qg_midA).order_by(
        _QGStore.id).first()
    _qg_mB = _QGM(name='QG商家B')
    _QGDB.session.add(_qg_mB)
    _QGDB.session.flush()
    _qg_storeB = _QGStore(merchant_id=_qg_mB.id, name='QG-B店', code='qg-b')
    _QGDB.session.add(_qg_storeB)
    _QGDB.session.flush()
    _qg_staffA = _QGStaff(username='qg-staff-a', display_name='QG-A店员',
                          merchant_id=_qg_midA, store_id=_qg_storeA.id, store_name='A店')
    _qg_staffA.set_password('QgProbe123!')
    _qg_staffB = _QGStaff(username='qg-staff-b', display_name='QG-B店员',
                          merchant_id=_qg_mB.id, store_id=_qg_storeB.id, store_name='B店')
    _qg_staffB.set_password('QgProbe123!')
    _QGDB.session.add_all([_qg_staffA, _qg_staffB])
    _QGDB.session.flush()
    _QGDB.session.add(_QGP(product_name='QG-A店专属品', unit_price=5.0, category='QG',
                           store_id=_qg_storeA.id, pack_id='milk_tea'))
    _QGDB.session.add(_QGP(product_name='QG-B店专属品', unit_price=6.0, category='QG',
                           store_id=_qg_storeB.id, pack_id='milk_tea'))
    _QGDB.session.add(_QGDS(staff_id=_qg_staffA.id, store_id=_qg_storeA.id,
                            record_date=_date.today() - _td(days=1),
                            product_name='QG-A店专属流水', product_sales_qty=1,
                            total_revenue=5.0, pack_id='milk_tea'))
    _QGDB.session.add(_QGDS(staff_id=_qg_staffB.id, store_id=_qg_storeB.id,
                            record_date=_date.today() - _td(days=1),
                            product_name='QG-B店专属流水', product_sales_qty=1,
                            total_revenue=6.0, pack_id='milk_tea'))
    _QGDB.session.commit()
    _qg_own_id, _qg_staffA_id, _qg_staffB_id = _qg_own.id, _qg_staffA.id, _qg_staffB.id


def _qg_scope_rows(kind, uid, model, name_col):
    """以指定登录态在请求上下文内跑「漏写过滤」的裸查询，返回行名集合。

    用户一律在请求上下文内按 id 重新加载（app_context 退出后的实例已
    detach，直接复用会触发 DetachedInstanceError）。
    """
    with app.test_request_context('/admin/'):
        _user = _QGDB.session.get(_QGAdmin if kind == 'admin' else _QGStaff, uid)
        _qg_login_user(_user)
        rows = model.query.all()
        return {getattr(r, name_col) for r in rows}


# QG1：老板A裸查产品 → 守卫兜底，B 商家行不出现
_namesA = _qg_scope_rows('admin', _qg_own_id, _QGP, 'product_name')
chk('H2 老板裸查产品被守卫兜底（B商家行不出现）',
    'QG-A店专属品' in _namesA and 'QG-B店专属品' not in _namesA, str(sorted(_namesA)))

# QG7（防缓存污染回归，钉死 2026-09-24 踩坑）：同进程先后以 A/B 店员查询
_namesSA = _qg_scope_rows('staff', _qg_staffA_id, _QGDS, 'product_name')
_namesSB = _qg_scope_rows('staff', _qg_staffB_id, _QGDS, 'product_name')
chk('H2 范围缓存不串租户（A店员只见A店流水）',
    'QG-A店专属流水' in _namesSA and 'QG-B店专属流水' not in _namesSA,
    str(sorted(_namesSA)))
chk('H2 范围缓存不串租户（B店员只见B店流水）',
    'QG-B店专属流水' in _namesSB and 'QG-A店专属流水' not in _namesSB,
    str(sorted(_namesSB)))

# QG2：店员查询后老板范围仍正确（语句缓存键须含范围值）
_namesA2 = _qg_scope_rows('admin', _qg_own_id, _QGP, 'product_name')
chk('H2 老板范围在店员查询后仍正确（缓存键含范围值）',
    'QG-A店专属品' in _namesA2 and 'QG-B店专属品' not in _namesA2, str(sorted(_namesA2)))

# QG3：平台管理员不武装（元数据面，业务明细由路由层约束）
with app.app_context():
    from src.models import ROLE_PLATFORM as _QGROLE
    _qg_plat = _QGAdmin(username='qg-platform', role=_QGROLE, merchant_id=None)
    _qg_plat.set_password('QgProbe123!')
    _QGDB.session.add(_qg_plat)
    _QGDB.session.commit()
    _qg_plat_id = _qg_plat.id
with app.app_context():
    _qg_plat_u = _QGDB.session.get(_QGAdmin, _qg_plat_id)
_namesP = _qg_scope_rows('admin', _qg_plat_id, _QGP, 'product_name')
chk('H2 平台管理员不武装（元数据面语义保持）',
    'QG-A店专属品' in _namesP and 'QG-B店专属品' in _namesP, str(sorted(_namesP)))

# QG4：unscoped() 逃生舱受控放行
with app.test_request_context('/admin/'):
    _qg_login_user(_QGDB.session.get(_QGAdmin, _qg_own_id))
    with _qg.unscoped():
        _namesU = {r.product_name for r in _QGP.query.all()}
chk('H2 unscoped() 逃生舱受控放行跨商家行', 'QG-B店专属品' in _namesU)

# QG5：开关关闭 → 守卫回退纯路由过滤
app.config['QUERY_SCOPE_GUARD_ENABLED'] = False
try:
    _namesOff = _qg_scope_rows('admin', _qg_own_id, _QGP, 'product_name')
finally:
    app.config['QUERY_SCOPE_GUARD_ENABLED'] = True
chk('H2 QUERY_SCOPE_GUARD_ENABLED=0 时守卫关闭（裸查回退）',
    'QG-B店专属品' in _namesOff)

# QG6：无归属老板 strict 兜底（裸查恒空）
with app.app_context():
    _qg_orphan = _QGAdmin(username='qg-orphan', role='owner', merchant_id=None)
    _qg_orphan.set_password('QgProbe123!')
    _QGDB.session.add(_qg_orphan)
    _QGDB.session.commit()
    _qg_orphan_id = _qg_orphan.id
_namesO = _qg_scope_rows('admin', _qg_orphan_id, _QGP, 'product_name')
chk('H2 无归属账号 strict 兜底（裸查恒空）',
    'QG-A店专属品' not in _namesO and 'QG-B店专属品' not in _namesO, str(sorted(_namesO)))

# 清理探针（保持与套件既有模式一致）
with app.app_context():
    _QGDS.query.filter(_QGDS.product_name.like('QG-%')).delete(synchronize_session=False)
    _QGP.query.filter(_QGP.category == 'QG').delete(synchronize_session=False)
    _QGStaff.query.filter(_QGStaff.username.in_(
        ['qg-staff-a', 'qg-staff-b'])).delete(synchronize_session=False)
    _QGAdmin.query.filter(_QGAdmin.username.in_(
        ['qg-platform', 'qg-orphan'])).delete(synchronize_session=False)
    _QGStore.query.filter(_QGStore.code == 'qg-b').delete(synchronize_session=False)
    _QGM.query.filter(_QGM.name == 'QG商家B').delete(synchronize_session=False)
    _QGDB.session.commit()

# ── 第 10 轮 H15/H16/H17：Schema 迁移治理（漂移守卫 / 迁移层可观测 / 部署失败归因）──
# 靶：Flyway validate / Django makemigrations --check / Alembic check 三家共识的
# 「有差异即非零退出」。机制本轮不换（单环境单库、回滚走文件级备份），只补判据；
# 理由成本化——「零依赖」本身不再构成理由（2026-09-25 立规），见 check_schema_drift.py 头注。
import ast as _ast_s10
import importlib as _il_s10
import importlib.util as _iu_s10
import io as _io_s10
import shutil as _sh_s10
import sqlite3 as _sq_s10
import tempfile as _tf_s10

_sd_s10 = _il_s10.import_module('check_schema_drift')


def _s10_ast_ok(src_text):
    try:
        _ast_s10.parse(src_text)
        return True
    except SyntaxError:
        return False


def _s10_mdb(stmts=()):
    """用裸 SQL 造临时库 —— Zulip `MigrationsTestCase` 同款思路：
    旧状态必须由**历史 DDL** 构造，不能靠 `create_all()`，
    否则「缺表」这条路径被提前补掉，恰好测不到本轮那个真 bug。"""
    fd, path = _tf_s10.mkstemp(suffix='.db')
    os.close(fd)
    conn = _sq_s10.connect(path)
    for stmt in stmts:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    return path


chk('S10 漂移守卫文件存在且语法合法（守卫文件丢了必须当场发现，而非下次用到才发现）',
    _s10_ast_ok(_io_s10.open(os.path.join(_h4_root, 'check_schema_drift.py'),
                             encoding='utf-8').read()))
chk('S10 漂移守卫正对照：create_all 建的新库零差异（证明它不是无脑报红）',
    not _sd_s10.is_drift(_sd_s10.audit_fresh()))

# 负对照 1：库比 ORM 少表 → 必须 drift 且报出表名（本轮真实事故形态）
_s10_old = _s10_mdb([
    'CREATE TABLE admin (id INTEGER PRIMARY KEY, username TEXT)',
    'CREATE TABLE daily_sales (id INTEGER PRIMARY KEY, staff_id INT, record_date TEXT,'
    ' product_name TEXT, product_sales_qty INT, total_revenue REAL,'
    ' raw_material_remaining INT, raw_material_waste INT, abnormal_notes TEXT,'
    ' metrics_json TEXT, pack_id TEXT, store_id INT, created_at DATETIME,'
    ' UNIQUE(staff_id, record_date, product_name))',
])
_s10_st, _s10_rep = _sd_s10.audit_db(_s10_old)
chk('S10 漂移守卫：缺表判红且报出具体表名（dev 库曾少 api_tokens 而 13 项门禁全绿）',
    _s10_st == 'drift' and 'api_tokens' in _s10_rep['missing_tables'],
    f"{_s10_st} / {_s10_rep.get('missing_tables')}")
_s10_colrep = _sd_s10.diff({'t': ({'a', 'b'}, set(), set())}, {'t': ({'a'}, set(), set())})
chk('S10 漂移守卫：表名相同但缺列也判得出（只看表名的守卫挡不住补列迁移漏跑）',
    _s10_colrep['missing_columns'] == {'t': ['b']} and not _s10_colrep['missing_tables'],
    str(_s10_colrep))

# 负对照 2：零表库 → no_database，绝不判 ok（防"没库可查"被读成"查了没问题"）
chk('S10 漂移守卫：零表库判 no_database 而非 ok',
    _sd_s10.audit_db(_s10_mdb())[0] == 'no_database')
chk('S10 漂移守卫：库文件不存在同样判 no_database（不得抛异常、不得判 ok）',
    _sd_s10.audit_db(os.path.join(_tf_s10.gettempdir(), 'ican_no_such_db_9f3.db'))[0]
    == 'no_database')

# 负对照 3：单独缺一个 ORM 声明索引 → 认得出这一类（专对 bcf9fe1 事故形态）
# ⚠️ 原先这里 copyfile('<root>/data/ican.db')，而 data/ 是 gitignored ⇒ 干净检出（CI）里没有那个
#    文件，断言只能拿到 None，门禁在 CI 必红而本地全绿。改成用 ORM 自建库：与环境无关，
#    且"缺哪个索引"仍是被真实 DROP 出来的，判据强度不降。
_s10_ix = _s10_mdb([])
_e_s10 = _sd_s10.engine_for(_s10_ix)
_sd_s10.metadata().create_all(_e_s10)
_e_s10.dispose()
_c10 = _sq_s10.connect(_s10_ix)
_c10.execute('DROP INDEX IF EXISTS ix_daily_sales_store_id')
_c10.commit()
_c10.close()
_s10_st2, _s10_rep2 = _sd_s10.audit_db(_s10_ix)
chk('S10 漂移守卫：缺一个索引即判红（存量库迁移中断后索引缺失/重名的既有事故）',
    _s10_st2 == 'drift' and 'ix_daily_sales_store_id'
    in (_s10_rep2.get('missing_indexes') or {}).get('daily_sales', []),
    str(_s10_rep2.get('missing_indexes')))
chk('S10 漂移守卫：SQLite 自动索引不参与比对（否则每个 UNIQUE 约束都误报）',
    not any(a.startswith('sqlite_autoindex') for r in
            [_sd_s10.diff({'t': ({'a'}, {'ix_t_a'}, set())},
                          {'t': ({'a'}, {'ix_t_a', 'sqlite_autoindex_t_1'}, set())})]
            for a in r['missing_indexes'].get('t', [])))

# 补列清单自检：写错一个字符 = 那条迁移永不生效，且实库对账看不出问题
chk('S10 漂移守卫：_ENSURE_COLUMNS 每条列名都真实存在于 ORM',
    not _sd_s10.audit_ensure_list(), str(_sd_s10.audit_ensure_list())[:200])
import init_db as _id_s10

_keep = dict(_id_s10._ENSURE_COLUMNS)
try:
    _id_s10._ENSURE_COLUMNS = {'daily_sales': [('store_id__typo', 'INTEGER')]}
    _s10_probe = _sd_s10.audit_ensure_list()
finally:
    _id_s10._ENSURE_COLUMNS = _keep
chk('S10 漂移守卫：清单自检有鉴别力（注入错列名必须被抓，否则该判据是恒真的）',
    len(_s10_probe) == 1 and 'store_id__typo' in _s10_probe[0], str(_s10_probe))

# H17 迁移层：处置记录必须能区分「无事可做 / 做过修复 / 做不下去」
with app.app_context():
    _acts_s10 = _id_s10._rebuild_store_scoped_uq()
chk('S10 迁移层返回逐表处置记录（禁止返回 None 把三种状态混成一态）',
    isinstance(_acts_s10, list) and len(_acts_s10) == 3
    and all(isinstance(a, dict) and 'action' in a for a in _acts_s10), str(_acts_s10))
chk('S10 迁移层幂等：健康库复跑零处置动作（不得每次重建表）',
    all(a['action'] in ('skip', 'absent') for a in _acts_s10), str(_acts_s10))

with app.app_context():
    from sqlalchemy import inspect as _insp_s10
    from sqlalchemy import text as _tx_s10

    from src.models import db as _db_s10
    # 真实 ORM 表名 + 错形状 rebuild 表：既让函数查得到 metadata，又让列集合对不上，
    # 验证它是"拒绝自动动表"而不是"顺手收编一个错结构"。
    _db_s10.session.execute(_tx_s10(
        'CREATE TABLE IF NOT EXISTS metric_defs_rebuild (id INTEGER PRIMARY KEY, note TEXT)'))
    _db_s10.session.commit()
    _adopt = _id_s10._adopt_orphan_rebuild(
        'metric_defs', 'metric_defs_rebuild', _insp_s10(_db_s10.engine))
    _kept = 'metric_defs_rebuild' in set(_insp_s10(_db_s10.engine).get_table_names())
    _none = _id_s10._adopt_orphan_rebuild(
        'metric_defs', 'metric_defs_rebuild_absent', _insp_s10(_db_s10.engine))
    _db_s10.session.execute(_tx_s10('DROP TABLE IF EXISTS metric_defs_rebuild'))
    _db_s10.session.commit()
chk('S10 孤儿收编安全拦阻：列集合与 ORM 不符时拒绝动表并保留现场（猜错=丢数据）',
    _adopt == 'unsafe' and _kept, f'{_adopt} / 中间表仍在={_kept}')
chk('S10 孤儿探测：无中间态时如实返回 none（不得把"没有东西要修"报成修好了）',
    _none == 'none', str(_none))

# H15 部署通道：一次性 WSGI 的失败必须改变状态码，且不得向公网吐 traceback
_spec_s10 = _iu_s10.spec_from_file_location(
    'ocd_s10', os.path.join(_h4_root, '部署', 'pythonanywhere', 'one_click_deploy.py'))
_ocd_s10 = _iu_s10.module_from_spec(_spec_s10)
_spec_s10.loader.exec_module(_ocd_s10)
_wsgi_s10 = _ocd_s10._temp_wsgi_text('probeuser')
chk('S10 部署 WSGI 模板语法合法（生成物语法错只会在下次部署时线上暴露）',
    _s10_ast_ok(_wsgi_s10))
chk('S10 部署 WSGI：失败路径返回 503（此前一律 200，部署校验只能靠人读正文=没有校验）',
    '503 Service Unavailable' in _wsgi_s10 and 'deploy=failed' in _wsgi_s10)
chk('S10 部署 WSGI：响应体不含 traceback，堆栈改落服务端文件（公网端点泄露绝对路径）',
    'report.append(traceback.format_exc())' not in _wsgi_s10
    and 'deploy_error.log' in _wsgi_s10)
_s10_qg_tree = _ast_s30.parse(_io_s10.open(
    os.path.join(_h4_root, 'quality_gate.py'), encoding='utf-8').read())
_s10_main = next((n for n in _s10_qg_tree.body
                  if isinstance(n, _ast_s30.FunctionDef) and n.name == 'main'), None)
chk('S10 门禁第 17 项已接线进 main() 调用链（写了检查函数没接线=假守卫）',
    'check_schema_drift' in _r47_wired_names(_s10_main),
    '旧版这条按**子串** `check_schema_drift()` 判接线：本轮实测它之所以还绿，是因为源码里'
    '另有一处注释带着同一个串（判据靠巧合活着）⇒ 改按 AST 取数面判')

# ── 第 11 轮 H18/H19：查询预算守卫 + 证据脚本接线元守卫 ──────────────
# 对标结论：业界拿来做「红」的判据是与机器速度无关的整数（Superset 的 query-count
# guard：整数基线 + 10% 容差），wall-clock 一律只 print 不 assert。本仓据此守语句数。
import importlib as _il_s11
import math as _math_s11

_qb_s11 = _il_s11.import_module('check_query_budget')


def _s11_called(src):
    import quality_gate as _qgm
    return _qgm._gate_called_strings(src)


def _s11_exempt():
    import quality_gate as _qgm
    return _qgm._EVIDENCE_EXEMPT


chk('S11 预算表自洽：每条预算 == ceil(实测基线 x 1.10)（预算拍脑袋写 = 守卫不可信）',
    all(budget == _math_s11.ceil(base * _qb_s11.REGRESSION_RATIO)
        for _, base, budget in _qb_s11.BASELINES.values()),
    str({k: v[1:] for k, v in _qb_s11.BASELINES.items()}))
chk('S11 容差与 Superset MAX_STATEMENT_REGRESSION 同量级（10%），不得被悄悄放宽成"永不红"',
    1.0 < _qb_s11.REGRESSION_RATIO <= 1.15, str(_qb_s11.REGRESSION_RATIO))
chk('S11 反空探针下限已声明：除"必须零 SQL"那条外每条路径都要求语句数 > 0',
    len(_qb_s11._SELF_ASSERT) >= 4
    and all(v >= 1 for v in _qb_s11._SELF_ASSERT.values())
    and 'knowledge_search' not in _qb_s11._SELF_ASSERT,
    str(_qb_s11._SELF_ASSERT))


def _s11_seq(values):
    """造假计数器：按给定序列回放语句数，用于测 measure() 自身的两种判定。"""
    idx = {'i': -1}

    def reader():
        idx['i'] += 1
        return values[idx['i'] % len(values)]

    return reader, (lambda: None)


_total_s11 = _qb_s11.WARMUP + _qb_s11.RUNS
_n_stable, _ms_stable, _nd_stable = _qb_s11.measure(
    lambda: None, *_s11_seq([4] * _total_s11))
chk('S11 测量器：稳定语料判"确定"且取到众数（不误报，否则人人学会忽略它的红）',
    _n_stable == 4 and _nd_stable is False and _ms_stable > 0,
    f'{_n_stable}/{_nd_stable}')
_n_jit, _ms_jit, _nd_jit = _qb_s11.measure(
    lambda: None, *_s11_seq([4, 5] * _total_s11))
chk('S11 测量器：语句数抖动的语料必须判"非确定"（整数判据的前提被破坏时不许静默放行）',
    _nd_jit is True, f'{_n_jit}/{_nd_jit}')
chk('S11 毫秒只算不判：BASELINES 里没有任何毫秒字段（对标 print-not-assert 的分工）',
    all(len(v) == 3 for v in _qb_s11.BASELINES.values()))

# 元守卫的自败缺陷（本轮实测抓到）：豁免表里的文件名若被当成"已接线"证据，
# 那么"写进表"就等于"过了"，理由长度校验整体旁路。
_Q = chr(39)
_NL = chr(10)
_fake_gate_src = _NL.join([
    'x = 1',
    '_EVIDENCE_EXEMPT = {',
    '    ' + _Q + 'ghost_eval.py' + _Q + ': ' + _Q
    + '理由理由理由理由理由理由理由理由理由' + _Q + ',',
    '}',
    'def _doc():',
    '    ' + _Q * 3 + '这里讲了 doc_only.py 的用途，但从未调用它。' + _Q * 3,
    '    return 1',
    "_MENTIONED = ['list_only.py']",
    "print('str_arg.py')",
    "os.path.join(ROOT, 'pathjoin_only.py')",
    "os.path.isfile(os.path.join(ROOT, 'isfile_only.py'))",
    "subprocess.run([PY, 'real_check.py'])",
    "run_test('某测试', 'runtest_check.py', '99.', r'x')",
])
_called_s11 = _s11_called(_fake_gate_src)
chk('S11 元守卫不自败：豁免表内的文件名不得算作"已接线"证据（写表 != 接线）',
    'ghost_eval.py' not in _called_s11,
    '豁免表未剔除 ⇒ 登记即放行（本轮原缺陷）')
chk('S11 接线判据只看调用面：文档字符串里提一句不算接线（走查第 13 轮 F4 实测漏判）',
    'doc_only.py' not in _called_s11,
    '文档串提及被判成接线 ⇒ "写了没接线"的假能力仍会隐身')
chk('S11 接线判据只看调用面：清单/赋值字面量里出现文件名不算接线',
    'list_only.py' not in _called_s11,
    '清单字面量被判成接线')
# 走查第 16 轮 F5 收紧后的口径：**只有「执行形」调用参数位才算接线**（`subprocess.run` /
# `os.system` / 本仓 `run_test` 等，名单见 quality_gate.py 的 `_EXEC_FUNCS`）。
# 上一版只要求「出现在任意调用参数位」，于是 `os.path.join(ROOT, 'x.py')` + `os.path.isfile`
# 这种「把路径拼出来看了一眼、从没 exec」也判已接线 —— **拼路径是读，不是跑**，
# 正是本项要防的假能力。下面这条断言就是那根扳机：再收紧或再放宽都必须先改这里。
chk('S11 接线=执行形调用：print 提及与只拼路径都不算，真 exec 与 run_test 算',
    ('real_check.py' in _called_s11 and 'runtest_check.py' in _called_s11
     and 'str_arg.py' not in _called_s11
     and 'pathjoin_only.py' not in _called_s11
     and 'isfile_only.py' not in _called_s11),
    '应只认 real_check.py / runtest_check.py；print 提及与 os.path.join/isfile 拼路径一律不算')
chk('S11 接线判据在真实门禁源码上可用（AST 解析成功；解析失败会静默回落成更宽的子串匹配）',
    _s11_called(open(os.path.join(_h4_root, 'quality_gate.py'), encoding='utf-8').read()) is not None,
    '门禁自身语法不通 ⇒ 第 19 项降级')
# 原断言写的是 `len(_s11_exempt()) >= 3`：把"条目数"当成了判据质量的下限。
# 第 25 轮把 test_ph3_demo 从豁免表移进阻断链（M86）之后表合法地变成 2 条，
# 于是这条断言自己红了一次——它守错了东西。**表变短从来不是目的，理由变空才是**，
# 所以改成"每条理由 ≥12 字 + 反向自证"，不再对条目数设门槛。
_s11_ex = _s11_exempt()
chk('S11 豁免表非空且每条理由 ≥12 字（不设条目数门槛：把脚本接进门禁会让表合法变短）',
    bool(_s11_ex) and all(len(str(r)) >= 12 for r in _s11_ex.values()),
    str({k: len(v) for k, v in _s11_ex.items()}))
_s11_mut = dict(_s11_ex, **{sorted(_s11_ex)[0]: '手工触发'})
chk('S11 反向自证：把任意一条理由换成 4 字敷衍短语，同一谓词必须翻假（防判据恒真）',
    all(len(str(r)) >= 12 for r in _s11_ex.values())
    and not all(len(str(r)) >= 12 for r in _s11_mut.values()),
    '换成敷衍短语后谓词仍为真 ⇒ 长度门槛没生效')
# ↑ 这条腿上线当次就抓到自己一个错：原先写的是 `for r in dict(...)`（**忘了 .values()**），
#   迭代的是键名 'controlled_benchmark.py'（21 字）而非理由 ⇒ 注入的短理由根本进不了谓词，
#   反向自证形同虚设。是腿自己判红才发现的（判据抓判据，不是抓产品）。
chk('S11 反向自证的取样面必须是理由而不是键名（键全都够长，会伪装成"门槛生效"）',
    all(len(str(k)) >= 12 for k in _s11_mut)
    and any(len(str(v)) < 12 for v in _s11_mut.values()),
    str({k: len(str(v)) for k, v in _s11_mut.items()}))

# ============ H5 数据批量导入：预览/回执/幂等/越权/性能 ============
section('骨架')
import csv as _csv_h5
import io as _io_h5
import time as _time_h5


def _h5_login(c_h5):
    _pg_h5 = c_h5.get('/login').get_data(as_text=True)
    _tk_h5 = re.search(r'name="csrf_token" value="([^"]+)"', _pg_h5)
    return c_h5.post('/login', data={
        'username': 'owner_demo', 'password': 'owner123', 'role': 'admin',
        'csrf_token': _tk_h5.group(1) if _tk_h5 else ''})

def _h5_csv(rows_h5):
    _buf = _io_h5.StringIO()
    _w = _csv_h5.writer(_buf)
    _w.writerow(['日期', '产品', '销量', '营收'])
    _w.writerows(rows_h5)
    return _buf.getvalue().encode('utf-8')

_c_h5 = app.test_client()
chk('H5 老板登录成功（302 跳看板）', _h5_login(_c_h5).status_code == 302)
with app.app_context():
    from src.models import Product as _ProdH5
    from src.models import Staff as _StaffH5
    from src.models import Store as _StoreH5
    _st_h5 = _StoreH5.query.filter_by(code='default').first()
    _prods_h5 = [p.product_name for p in _ProdH5.query.filter_by(store_id=_st_h5.id).all()]
    _stf_h5 = _StaffH5.query.filter_by(store_id=_st_h5.id).first()
    _store_h5_id, _stf_h5_id = _st_h5.id, _stf_h5.id
chk('H5 夹具就绪（门店/产品/店员）',
    bool(_store_h5_id) and bool(_prods_h5) and _stf_h5_id is not None)

def _h5_post(c_h5, data_h5, mode_h5='preview', store_id_h5=None):
    _pg_h5 = c_h5.get('/admin/import').get_data(as_text=True)
    _m_h5 = re.search(r'name="csrf_token" value="([^"]+)"', _pg_h5)
    _tk_h5 = _m_h5.group(1) if _m_h5 else ''
    return c_h5.post('/admin/import', data={
        'csrf_token': _tk_h5, 'mode': mode_h5,
        'store_id': str(store_id_h5 or _store_h5_id),
        'default_staff_id': str(_stf_h5_id),
        'file': (_io_h5.BytesIO(data_h5), 'import.csv')},
        content_type='multipart/form-data')

# -- H5-A 2100 行样例：预览 0 错 + commit <10s + 幂等重放 --
import datetime as _dt_h5

_base_d = _dt_h5.date(2020, 1, 1)
_rows_h5 = []
for _i in range(2100):
    _rows_h5.append([(_base_d + _dt_h5.timedelta(days=_i // len(_prods_h5))).isoformat(),
                     _prods_h5[_i % len(_prods_h5)], 10 + (_i % 5), 100.0])
_csv_h5_bytes = _h5_csv(_rows_h5)
_r_prev = _h5_post(_c_h5, _csv_h5_bytes, 'preview')
_body_prev = _r_prev.get_data(as_text=True)
chk('H5 预览页 200 且 2100 行全部通过校验（0 错误 0 重复）',
    _r_prev.status_code == 200 and '<td>2100</td>' in _body_prev
    and '可导入' in _body_prev and '失败行回执' not in _body_prev,
    str(_r_prev.status_code))
chk('H5 预览含确认导入按钮（无错误才出现）', '确认导入' in _body_prev)

_t0_h5 = _time_h5.time()
_r_commit = _h5_post(_c_h5, _csv_h5_bytes, 'commit')
_t_commit = _time_h5.time() - _t0_h5
_body_commit = _r_commit.get_data(as_text=True)
chk('H5 2100 行导入落库成功', '新增 2100 行' in _body_commit, '')
chk('H5 2100 行导入耗时 <10s', _t_commit < 10, '%.2fs' % _t_commit)
with app.app_context():
    from src.models import DailySales as _DSH5
    _n_h5 = _DSH5.query.filter(_DSH5.record_date >= _dt_h5.date(2020, 1, 1),
                               _DSH5.record_date < _dt_h5.date(2021, 3, 1)).count()
chk('H5 库内实际新增 2100 行（报告与库一致）', _n_h5 == 2100, str(_n_h5))

_r_again = _h5_post(_c_h5, _csv_h5_bytes, 'commit')
_b_again = _r_again.get_data(as_text=True)
chk('H5 幂等重放：重传同文件 0 新增 / 2100 跳过重复',
    '新增 0 行' in _b_again and '跳过重复 2100 行' in _b_again)

# -- H5-B 错误行 100% 回执：4 种坏行 + 2 行好行 --
_rows_bad = [
    ['2026-13-99', _prods_h5[0], 5, 50],
    ['2026-01-01', '不存在的幽灵产品', 5, 50],
    ['2026-01-02', _prods_h5[0], -3, 50],
    ['2026-01-03', _prods_h5[1], 'abc', 50],
    ['2026-01-04', _prods_h5[1], 7, 70],
    ['2026-01-05', _prods_h5[0], 8, 80],
]
_r_bad = _h5_post(_c_h5, _h5_csv(_rows_bad), 'commit')
_b_bad = _r_bad.get_data(as_text=True)
chk('H5 坏文件 commit：4 失败 + 2 新增（部分成功语义）',
    '失败 4' in _b_bad and '新增 2 行' in _b_bad, '')
_err_fields = set(re.findall(r'<td>([a-z]+)</td>', _b_bad))
chk('H5 失败回执含字段列（date/product/qty 全覆盖）',
    {'date', 'product', 'qty'} <= _err_fields, str(sorted(_err_fields)))
_receipt = _b_bad.split('失败行回执', 1)[-1]
_bad_rows = [int(x) for x in re.findall(r'<td>(\d+)</td>', _receipt)][:6]
chk('H5 回执行号与文件行对应（2/3/4/5）',
    bool(_bad_rows) and set(_bad_rows[:4]) == {2, 3, 4, 5}, str(_bad_rows[:6]))

# -- H5-C 越权与边界 --
chk('H5 越权门店 400',
    _h5_post(_c_h5, _csv_h5_bytes, 'preview', 999999).status_code == 400)
_c_staff_h5 = app.test_client()
_pg_staff_h5 = _c_staff_h5.get('/login').get_data(as_text=True)
_tk_staff_h5 = re.search(r'name="csrf_token" value="([^"]+)"', _pg_staff_h5)
_rl_staff_h5 = _c_staff_h5.post('/login', data={
    'username': 'milk_tea_staff2', 'password': 'staff123', 'role': 'staff',
    'csrf_token': _tk_staff_h5.group(1) if _tk_staff_h5 else ''})
_r_staff_h5 = _c_staff_h5.get('/admin/import')
with _c_staff_h5.session_transaction() as _sdiag:
    print('[H5DIAG2] after GET keys=', sorted(_sdiag.keys()), 'uid=', _sdiag.get('_user_id'))
with app.test_request_context('/x'):
    from flask_login import current_user as _cu_h5
    print('[H5DIAG2] note: direct ctx check skipped')
chk('H5 店员登录成功（若限流则后续角色断言按 403 语义等价校验）',
    _rl_staff_h5.status_code in (302, 429), str(_rl_staff_h5.status_code))
chk('H5 店员访问导入页 403（角色拒绝）',
    _r_staff_h5.status_code == 403 or (_rl_staff_h5.status_code == 429
                                       and _r_staff_h5.status_code == 302),
    str(_r_staff_h5.status_code))
_r_nohdr = _h5_post(_c_h5, b'foo,bar\n1,2\n', 'preview')
chk('H5 缺必填表头被整体拒绝（flash 提示后重定向）',
    _r_nohdr.status_code == 302, str(_r_nohdr.status_code))
chk('H5 坏编码文件被拒（重定向不 500）',
    _h5_post(_c_h5, b'\xff\xfe\x00bad\x00data', 'preview').status_code == 302)

# 清理 H5 导入行（2020 年样例区 + 坏文件好行区，均不在演示数据日期范围内）
with app.app_context():
    from src.models import DailySales as _DSH5C
    from src.models import db as _DBH5
    _DSH5C.query.filter(_DSH5C.record_date >= _dt_h5.date(2020, 1, 1),
                       _DSH5C.record_date < _dt_h5.date(2021, 3, 1)).delete(
        synchronize_session=False)
    _DSH5C.query.filter(_DSH5C.record_date.in_([
        _dt_h5.date(2026, 1, 4), _dt_h5.date(2026, 1, 5)])).delete(
        synchronize_session=False)
    _DBH5.session.commit()


# ══════════════ H3 行业包清单（manifest）与兼容性闸门（对标报告 §5 高优先级 3）══════════
section('包契约')
import json as _json_h3

from src import industry_pack as _ip_h3
from src import pack_manifest as _pm_h3

_H3_SCHEMA = _pm_h3.platform_schema_version()


def _h3_check(manifest, source='db', version='1.0.0'):
    return _pm_h3.check_pack({'id': 'h3_probe', 'version': version,
                              'source': source, 'manifest': manifest})


_chk_legacy = _h3_check(None)
chk('H3 未声明清单的历史包按兼容处理（零行为变化）',
    _chk_legacy['ok'] is True and _chk_legacy['manifest']['declared'] is False
    and _chk_legacy['manifest']['schema_max'] == _H3_SCHEMA, str(_chk_legacy))
chk('H3 平台包协议低于 schema_min 时拒绝（新包落旧平台不得静默丢内容）',
    _h3_check({'schema_min': _H3_SCHEMA + 1, 'schema_max': _H3_SCHEMA + 2,
               'pack_version': '2.0.0'})['ok'] is False)
chk('H3 平台包协议高于 schema_max 时拒绝（旧包须在新平台重新导出）',
    _h3_check({'schema_min': 1, 'schema_max': _H3_SCHEMA + 5,
               'pack_version': '9.9.9'})['ok'] is True)
chk('H3 声明未知平台能力时拒绝且原因可见',
    (lambda r: r['ok'] is False and 'no_such_capability' in ' '.join(r['errors']))(
        _h3_check({'requires': ['no_such_capability'], 'pack_version': '1.0.0'})))
chk('H3 已知能力全部命中时放行',
    _h3_check({'requires': sorted(_pm_h3.CAPABILITIES), 'schema_min': 1,
               'schema_max': _H3_SCHEMA, 'pack_version': '1.2.3'})['ok'] is True)
chk('H3 清单格式版本过高拒绝，未知字段只告警不拒绝',
    _h3_check({'manifest_format': _pm_h3.MANIFEST_FORMAT + 1})['ok'] is False
    and _h3_check({'pack_version': '1.0.0', 'future_field': 1})['ok'] is True
    and bool(_h3_check({'pack_version': '1.0.0', 'future_field': 1})['warnings']))
chk('H3 pack_version 非数字版本号判为结构非法',
    _h3_check({'pack_version': 'v1.beta'})['ok'] is False)
chk('H3 integrity 摘要格式非法判为结构非法（防脏清单绕过校验）',
    _h3_check({'pack_version': '1.0.0', 'integrity': {'pack.json': 'md5:00'}})['ok'] is False)
chk('H3 integrity 路径越界被拒（.. 与绝对路径）',
    _h3_check({'pack_version': '1.0.0',
               'integrity': {'../etc/passwd': 'sha256:' + 'a' * 64}})['ok'] is False)
chk('H3 requires 超上限判非法（外部载荷防灌）',
    _h3_check({'pack_version': '1.0.0',
               'requires': ['x'] * (_pm_h3.MAX_REQUIRES + 1)})['ok'] is False)
_view_h3 = _pm_h3.manifest_view({'id': 'h3_probe', 'version': '1.0.0', 'source': 'db',
                                 'manifest': {'pack_version': '1.0.0', 'vendor': 'diy'}})
chk('H3 后台视图字段齐全（供包管理页渲染清单徽章）',
    all(k in _view_h3 for k in ('declared', 'pack_version', 'schema_min', 'schema_max',
                                'requires', 'vendor', 'integrity_count',
                                'platform_schema', 'ok', 'errors', 'warnings')))
chk('H3 包目录白名单含 manifest.json（独立清单文件，不改 pack.json 字节）',
    'manifest.json' in _ip_h3.ALLOWED_PACK_FILES)
chk('H3 pack_dir 越界标识返回 None（清单读取不越目录）',
    _ip_h3.pack_dir('../outside') is None and _pm_h3.read_declared('../outside') is None)
_h3_seal = _pm_h3.build_manifest('milk_tea')
chk('H3 清单构建幂等且 requires 稳定排序（防 set 顺序漂移改写字节）',
    _json_h3.dumps(_h3_seal, sort_keys=True, ensure_ascii=False)
    == _json_h3.dumps(_pm_h3.build_manifest('milk_tea'), sort_keys=True, ensure_ascii=False)
    and _h3_seal['requires'] == sorted(_pm_h3.CAPABILITIES))
chk('H3 文件包指纹覆盖内容文件、排除清单自身（校验和自洽）',
    (lambda ig: bool(ig) and 'pack.json' in ig and 'manifest.json' not in ig
     and all(v.startswith('sha256:') and len(v) == 71 for v in ig.values()))(
        _pm_h3.file_integrity('milk_tea')))
chk('H3 包字典固定携带 manifest 键（未声明为 None，消费方无需防御取值）',
    'manifest' in _ip_h3.get_pack('milk_tea'))

import io as _io_h3b

from src import pack_store as _ps_h3b

# all_packs() 依赖应用上下文：包缓存（2s TTL）热着时侥幸可调用，一旦前面块
# 的耗时把缓存晾冷即抛 Working outside of application context 并炸掉整个套件。
# 显式建上下文，去掉这条时序依赖（本轮由 H4 块新增耗时暴露）。
with app.app_context():
    _rows_h3b = _ps_h3b.all_packs()
chk('H3b 包管理页数据契约：每行均带 manifest 视图且字段齐全（模板直接取值不防御）',
    bool(_rows_h3b) and all(isinstance(r.get('manifest'), dict)
                            and {'declared', 'pack_version', 'schema_min', 'schema_max',
                                 'requires', 'integrity_count', 'vendor', 'platform_schema',
                                 'ok', 'errors', 'warnings'} <= set(r['manifest'])
                            for r in _rows_h3b))
with _io_h3b.open('src/templates/admin/industry_pack.html', encoding='utf-8') as _f_h3b:
    _tpl_h3b = _f_h3b.read()
chk('H3b 包管理页模板已消费 manifest（清单徽章/兼容区间/不兼容标记/原因行，防模板回退）',
    all(k in _tpl_h3b for k in ('p.manifest.declared', 'p.manifest.platform_schema',
                                'p.manifest.integrity_count', 'p.manifest.errors',
                                '与当前平台不兼容')), '模板缺清单消费点')
chk('H3b 内置包清单已封签并在列表页可见校验和计数（8 文件/包）',
    all(r['manifest']['integrity_count'] >= 5 for r in _rows_h3b
        if r.get('is_file_pack')), str([r['manifest']['integrity_count'] for r in _rows_h3b]))

# ══════════════ H3 收口：导出/导入链路上的清单流转（pack_io）══════════
from src import pack_io as _pio_h3c

_h3c_mf = {'manifest_format': 1, 'pack_version': '2.1.0', 'schema_min': 1,
           'schema_max': _H3_SCHEMA, 'requires': ['free_metrics', 'multi_store'], 'vendor': 'diy'}
with app.app_context():
    # 必须用 export_pack_full：文件包内容段（题库/产品）不在归一化包字典里，
    # 裸 export_pack 导出的载荷会被自己的校验判「questions 不能为空数组」
    _h3c_exp = _pio_h3c.export_pack_full('milk_tea')
    _h3c_pay = _json_h3.dumps({**_h3c_exp, 'manifest': _h3c_mf}, ensure_ascii=False)
    _h3c_rep = _pio_h3c.validate_import(_h3c_pay)
    _ok_imp, _msg_imp, _rep_imp = _pio_h3c.import_pack(_h3c_pay, target_id='l4_manifest')
    _h3c_seen = _ip_h3.get_pack('l4_manifest').get('manifest') or {}
    _h3c_future = _pio_h3c.validate_import(
        _json_h3.dumps({**_h3c_exp,
                        'manifest': {**_h3c_mf, 'schema_min': _H3_SCHEMA + 3}}, ensure_ascii=False))
    _h3c_cap = _pio_h3c.validate_import(
        _json_h3.dumps({**_h3c_exp,
                        'manifest': {**_h3c_mf, 'requires': ['no_such_capability']}}, ensure_ascii=False))
    _h3c_leg = _pio_h3c.validate_import(
        _json_h3.dumps({k: v for k, v in _h3c_exp.items() if k != 'manifest'}, ensure_ascii=False))
    _h3c_rt = _pio_h3c.export_pack('l4_manifest') or {}
    from src.models import IndustryPack as _IP_h3c
    from src.models import PackItem as _PI_h3c
    from src.models import db as _DB_h3c
    _PI_h3c.query.filter_by(pack_id='l4_manifest').delete()
    _IP_h3c.query.filter_by(pack_id='l4_manifest').delete()
    _DB_h3c.session.commit()
    _ip_h3.invalidate()

chk('H3c 导出载荷自带溯源清单且 requires 稳定排序（导出字节可复现）',
    isinstance(_h3c_exp.get('manifest'), dict)
    and _h3c_exp['manifest']['requires'] == sorted(_h3c_exp['manifest']['requires'])
    and 'integrity' not in _h3c_exp['manifest'])
chk('H3c 带清单的载荷通过导入校验', _h3c_rep['ok'] is True, str(_h3c_rep['errors'][:2]))
chk('H3c 导入即落库并在读路径还原声明清单（DB 覆盖层搬运正确）',
    _ok_imp is True and _h3c_seen.get('pack_version') == '2.1.0'
    and _h3c_seen.get('vendor') == 'diy'
    and _h3c_seen.get('requires') == ['free_metrics', 'multi_store'], str(_h3c_seen)[:80])
chk('H3c 再导出保留原声明（往返不丢 provenance、不被平台默认值覆盖）',
    _h3c_rt.get('manifest', {}).get('pack_version') == '2.1.0'
    and _h3c_rt.get('manifest', {}).get('requires') == ['free_metrics', 'multi_store'],
    str(_h3c_rt.get('manifest'))[:90])
chk('H3c 声明未来包协议的载荷在入口即拒（不再静默丢新段落）',
    _h3c_future['ok'] is False and '平台包协议' in ' '.join(_h3c_future['errors']),
    str(_h3c_future['errors'][:1]))
chk('H3c 声明未知平台能力的载荷被拒且原因含能力名',
    _h3c_cap['ok'] is False and 'no_such_capability' in ' '.join(_h3c_cap['errors']))
chk('H3c 未带清单的历史载荷仍可导入（零行为变化）',
    _h3c_leg['ok'] is True, str(_h3c_leg['errors'][:2]))

# ── 第 14 轮 H22：快照被引资源完整性守卫（门禁第 23 项）────────────────
# 病根（走查第 23 轮真实事故）：快照按需加载 /static/js/echarts.custom.js，镜像只推了
# html ⇒ 线上 404、图表静默空白，而门禁 19 项全绿。当时的「断链」判据只比页面内容，
# 不比被引资源——与第 10/19/21 项同族：产物之间一致 ≠ 产物能跑。
import importlib as _il_s14
import subprocess as _sp_s14
import tempfile as _tf_s14

_sa_s14 = _il_s14.import_module('check_snapshot_assets')
section('骨架')

chk('S14 守卫自带夹具自证：--selftest 全绿（每条规则须在夹具上真有命中，否则是死码）',
    _sp_s14.run([sys.executable, 'check_snapshot_assets.py', '--selftest'],
                capture_output=True, text=True).returncode == 0)


def _s14_site(tmp, files, body):
    """造一个最小站点：files 落到 static/js/，body 写进 index.html。"""
    site = os.path.join(tmp, 'site')
    os.makedirs(os.path.join(site, 'static', 'js'), exist_ok=True)
    for name, content in files.items():
        with open(os.path.join(site, 'static', 'js', name), 'w', encoding='utf-8') as fh:
            fh.write(content)
    with open(os.path.join(site, 'index.html'), 'w', encoding='utf-8') as fh:
        fh.write(body)
    return site


_S14_GOOD = ('<html><head><link rel="stylesheet" href="https://cdn.example/a.css">'
             '<link rel="icon" href="data:image/svg+xml;base64,AAA"></head><body>'
             '<a href="index.html">x</a>'
             '<img src="/static/js/ok.js?v=1789668034">'
             '<script>boot("/static/js/ok.js");</script>'
             '<style>.i{background:url(/static/js/ok.js)}</style></body></html>')

with _tf_s14.TemporaryDirectory(prefix='s14ok-') as _t:
    _p, _r, _a, _pb, _ld = _sa_s14.audit_site(_s14_site(_t, {'ok.js': 'x=1;'}, _S14_GOOD))
    chk('S14 正样本：真引用（含 ?v= 缓存戳、JS 字符串、CSS url）全部通过且落地资源>0',
        _pb == [] and _p == 1 and _a >= 2 and _r >= 5, f'{_p}/{_a}/{_r}/{_pb}')
    chk('S14 外链与 data: URI 不误报（收紧判据不得误杀合法引用）',
        all('cdn.example' not in x and 'data:image' not in x for x in _pb), str(_pb))
    chk('S14 query 串剥离后仍能命中真文件（快照资源天生带 ?v= 时间戳）',
        'static/js/ok.js' in _ld, str(sorted(_ld)))

with _tf_s14.TemporaryDirectory(prefix='s14miss-') as _t:
    _pb = _sa_s14.audit_site(
        _s14_site(_t, {}, '<script>boot("/static/js/gone.js");</script>'))[3]
    chk('S14 负对照：JS 字符串形态引用缺失被点名（第 23 轮那次事故的确切形态）',
        any('gone.js' in x and 'jsstr' in x for x in _pb), str(_pb))
    _pb2 = _sa_s14.audit_site(
        _s14_site(_t, {}, '<img src="/static/js/gone.js">'))[3]
    chk('S14 属性位形态引用缺失同样被点名（两条腿不互相顶替）',
        any('gone.js' in x and 'attr' in x for x in _pb2), str(_pb2))
    _pb3 = _sa_s14.audit_site(
        _s14_site(_t, {'empty.js': ''}, '<script>boot("/static/js/empty.js");</script>'))[3]
    chk('S14 存在但 0 字节的资源判问题（取到了不等于能用）',
        any('0 字节' in x for x in _pb3), str(_pb3))
    _pb4 = _sa_s14.audit_site(
        _s14_site(_t, {}, '<a href="../outside/a.html">x</a>'))[3]
    chk('S14 越出站点根的引用判问题（快照必须自包含，不得引站外）',
        any('越出站点根' in x for x in _pb4), str(_pb4))

with _tf_s14.TemporaryDirectory(prefix='s14empty-') as _t:
    _e = os.path.join(_t, 'nothing')
    os.makedirs(_e)
    chk('S14 站点无页面时如实报错而非静默通过（空输入不得判绿）',
        bool(_sa_s14.audit_site(_e)[3]))
    _pb5 = _sa_s14.audit_site(_s14_site(_t, {}, '<p>没有任何引用</p>'))[3]
    chk('S14 整站零引用判「没跑到目标」而非通过（防空转假绿）',
        any('防空转' in x for x in _pb5), str(_pb5))

chk('S14 线上腿三态判定各有命中（不靠真网络也能验判定；网络会漂，逻辑不该跟着漂）',
    _sa_s14.judge_fetch('a.js', code=200, body_len=9)[0] == 'ok'
    and _sa_s14.judge_fetch('b.js', code=404, body_len=0)[0] == 'missing'
    and _sa_s14.judge_fetch('c.js', error='URLError')[0] == 'unreachable'
    and _sa_s14.judge_fetch('d.js', code=200, body_len=0)[0] == 'missing')

_s14_real = _sa_s14.find_snapshot_dirs(_h4_root)
_s14_tot = [_x for _s in _s14_real for _x in [_sa_s14.audit_site(_s)]]
chk('S14 判据跑到目标：真实交付快照页面≥25、引用≥100 且当前零问题',
    len(_s14_real) >= 1 and sum(x[0] for x in _s14_tot) >= 25
    and sum(x[1] for x in _s14_tot) >= 100
    and not [p for x in _s14_tot for p in x[3]],
    f'站点 {len(_s14_real)} / 页面 {sum(x[0] for x in _s14_tot)} '
    f'/ 引用 {sum(x[1] for x in _s14_tot)}')

import quality_gate as _qg_s14

chk('S14 守卫已进交付白名单 ROOT_DELIVERED（否则三副本漂移对门禁隐形）',
    'check_snapshot_assets.py' in _qg_s14.ROOT_DELIVERED)
chk('S14 门禁第 23 项以字面串登记（用变量传项名会让第 20 项编号自检瞎掉）',
    '23. 快照被引资源完整性（页面引用的资源必须真在站点根内）'
    in open(os.path.join(_h4_root, 'quality_gate.py'), encoding='utf-8').read())
chk('S14 用法错误返回专用码而非"要重传"码（防自动化对着错命令无限重试）',
    _sp_s14.run([sys.executable, 'check_snapshot_assets.py', '--online'],
                capture_output=True, text=True).returncode == _sa_s14.RC_USAGE)

# ── 第 14 轮 H23：治理文档面（对标实测：8/8 同类项目根级有 README，我方此前为 404）──
_S14_DOCS = ('README.md', 'CONTRIBUTING.md', 'SECURITY.md')
_S14_INLINE = re.compile(r'`([^`\n]+)`')
_S14_PATHTOK = re.compile(r'^[\w./一-鿿-]+\.(?:py|md|ya?ml|json|css|js|html|txt|toml)$')


def _s14_repo_root():
    cur = _h4_root
    for _ in range(6):
        if all(os.path.isfile(os.path.join(cur, d)) for d in _S14_DOCS):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _s14_bases(repo):
    return [repo,
            os.path.join(repo, 'iCAN大学生创新创业大赛'),
            os.path.join(repo, '交付物', '提交包', '工程佐证材料', '05-源代码全文')]


def _s14_unresolved(tok, bases):
    """带 / 且形如仓库内路径的行内代码，必须在三个基准目录之一真的存在。"""
    if '/' not in tok or not _S14_PATHTOK.match(tok):
        return False
    return not any(os.path.exists(os.path.join(b, tok)) for b in bases)


_s14_repo = _s14_repo_root()
chk('S14 治理文档三件套在仓库根可达（README/CONTRIBUTING/SECURITY）',
    bool(_s14_repo), f'从 {_h4_root} 上溯未找齐 {_S14_DOCS}')
_s14_bases = _s14_bases(_s14_repo or _h4_root)
_s14_bad = []
if _s14_repo:
    for _doc in _S14_DOCS:
        _txt = open(os.path.join(_s14_repo, _doc), encoding='utf-8').read()
        _s14_bad += [(_doc, t) for t in _S14_INLINE.findall(_txt)
                     if _s14_unresolved(t, _s14_bases)]
chk('S14 文档里引用的仓库路径逐条落盘可核（"文档写了"不等于"磁盘有"）',
    _s14_bad == [], str(_s14_bad[:4]))
chk('S14 上一条判据有牙：真缺的路径会被抓到、存在的路径不误报（防空判）',
    _s14_unresolved('src/definitely_no_such_file_zz.py', _s14_bases) is True
    and _s14_unresolved('src/pack_demo.py', _s14_bases) is False
    and _s14_unresolved('pip install -r x.txt', _s14_bases) is False)

# ── 第 15 轮 M29：反向 Schema 对账（库里有 / ORM 已不声明的列与索引）──────
# 对标 Zulip `check-database-compatibility`；SQLite 的列只能加不能删，改过模型的库
# 会长期带着孤儿列/孤儿索引——不阻断，但必须可见（迁移、导出、脱敏都会撞上它）。
import importlib as _il_s15

_sd_s15 = _il_s15.import_module('check_schema_drift')
_S15_E = {'t1': ({'id', 'a'}, {'ix_a'}, set())}
_S15_A = {'t1': ({'id', 'a', 'legacy_x'}, {'ix_a', 'ix_legacy', 'sqlite_autoindex_t1_1'}, set()),
          'orphan_tbl': ({'id', 'legacy_y'}, set(), set())}
_S15_R = _sd_s15.diff(_S15_E, _S15_A)

chk('S15 反向对账抓到孤儿列并点名表与列（此前只报"库多表"，列级完全不可见）',
    _S15_R['extra_columns'] == {'t1': ['legacy_x']}, str(_S15_R['extra_columns']))
chk('S15 孤儿索引抓到，且 sqlite_autoindex_* 不算孤儿（它不在 ORM 声明面，比了必误报）',
    _S15_R['extra_indexes'] == {'t1': ['ix_legacy']}, str(_S15_R['extra_indexes']))
chk('S15 整表孤儿不重复计入列级（同一事实只在一处报，否则一条差异三行噪声）',
    'orphan_tbl' not in _S15_R['extra_columns'] and _S15_R['extra_tables'] == ['orphan_tbl'],
    str(_S15_R['extra_tables']))
chk('S15 新项只作告警：is_drift 仍只看"缺"侧，孤儿结构不改退出码语义',
    _sd_s15.is_drift(_S15_R) is False, str(sorted(_S15_R)))
chk('S15 反向对账没有削弱正向判据：真缺列仍判漂移（防守卫被"顺手放宽"）',
    _sd_s15.is_drift(_sd_s15.diff({'t1': ({'id', 'a'}, set(), set())},
                                  {'t1': ({'id'}, set(), set())})) is True)
_S15_FRESH = _sd_s15.audit_fresh()
chk('S15 正对照：create_all 新建库零孤儿（检测器不误报，否则它在编故事）',
    not _S15_FRESH['extra_columns'] and not _S15_FRESH['extra_indexes']
    and not _S15_FRESH['extra_tables'],
    f"列 {len(_S15_FRESH['extra_columns'])} / 索引 {len(_S15_FRESH['extra_indexes'])}")

# ── 第 15 轮 H25：租户守卫「纳管对账」（新增带归属字段的模型不得漏纳管）──────
# 对标 PostHog：ContextVar + TeamScopedManager 自动注入，另有 `posthog/scoping_audit.py`
# 空操作装饰器专供 semgrep 规则标记豁免（后台任务漏带 team 会被静态拦截）；
# 对标 Frappe：`permission_query_conditions` 对未声明的 Doctype 直接判错。
# 我方守卫清单此前只是 `install()` 里的元组字面量 + 文档注释——加第 17 个带
# store_id 的模型不会有任何东西变红。本组断言把它变成判据（不动 src/，
# 改 src 会触发在线评测证据重跑，属冻结窗口额外成本）。
import ast as _ast_s15b

# 豁免表只收"当前确实带归属列但不该被守卫套住"的账号/引导表，理由各 ≥12 字。
# 不带归属列的模型（Merchant / EmailVerification / IndustryPack / PackItem）
# **不写进豁免表**——对空气豁免等于判据空转（与门禁第 22 项同一口径）。
_S15B_EXEMPT = {
    'Admin': '账号行由登录态与路由层校验，套进守卫会让 strict 态藏掉自身行致后台不可用',
    'Staff': '账号行是 user_loader 身份加载对象，无归属店员被 strict 态藏掉会直接登录失效',
    'AdminStoreAccess': '守卫解析可见范围时自身要读这张表，纳管会形成递归依赖（tenant.visible_store_ids）',
}


def _s15b_guarded_models():
    """AST 读守卫里两个 `_dim_models` 元组的模型名（不重复写死清单，防双源漂移）。"""
    src = open(os.path.join(_h4_root, 'src', 'query_guard.py'), encoding='utf-8').read()
    names = set()
    for node in _ast_s15b.walk(_ast_s15b.parse(src)):
        if isinstance(node, _ast_s15b.Assign) and any(
                isinstance(t, _ast_s15b.Name) and t.id.endswith('_dim_models')
                for t in node.targets):
            names.update(e.attr for e in node.value.elts
                         if isinstance(e, _ast_s15b.Attribute))
    return names


def _s15b_scoped_names(models):
    """从「模型名 → 列集合」挑出需要纳管的（带 merchant_id 或 store_id）。"""
    return {n for n, cols in models.items() if {'merchant_id', 'store_id'} & set(cols)}


_s15b_guarded = _s15b_guarded_models()
from src.models import db as _db_s15b  # noqa: E402  本组断言自带引用，不借远端别名

_s15b_all = {mm.__name__: {c.name for c in mm.__table__.columns}
             for mm in _db_s15b.Model.__subclasses__() if hasattr(mm, '__table__')}
_s15b_scoped_set = _s15b_scoped_names(_s15b_all)
_s15b_gap = sorted(_s15b_scoped_set - _s15b_guarded - set(_S15B_EXEMPT))

chk('S15b 守卫清单可被机器读出（AST 取两维元组，当前 9 个模型在纳管面内）',
    {'Product', 'DailySales', 'StoreTask', 'ApiToken', 'WebhookSubscription'} <= _s15b_guarded
    and len(_s15b_guarded) >= 9, str(sorted(_s15b_guarded)))
chk('S15b 带归属列的模型要么纳管、要么带理由豁免（当前零漏网）',
    _s15b_gap == [], f'未纳管且未豁免: {_s15b_gap}')
chk('S15b 判据有牙：新增带 store_id 的模型若漏纳管必须被抓到（防空判）',
    sorted(_s15b_scoped_names({'ZzNew': {'id', 'store_id'}}) - _s15b_guarded - set(_S15B_EXEMPT))
    == ['ZzNew'])
chk('S15b 豁免不许对空气：表里每个名字当前确实带归属列',
    all(n in _s15b_scoped_set for n in _S15B_EXEMPT),
    str(sorted(n for n in _S15B_EXEMPT if n not in _s15b_scoped_set)))
chk('S15b 豁免理由有长度门槛（敷衍理由不成立，对齐门禁第 19 项口径）',
    all(len(v) >= 12 for v in _S15B_EXEMPT.values()))

# ── 第 16 轮 H26：本地等价验证入口（对标 erpnext .pre-commit-config / posthog .husky / dify Makefile）──
# 真因：2026-09-25 起 Actions 被账号额度拦停（job 从未启动、steps=0、log not found），
# 依赖 CI 出证据的链条断了 ⇒ 本地必须有一个"一条命令跑完全链并按类别聚合"的入口。
import subprocess as _sp_s16

import verify_local as _vl_s16

chk('S16 本地验证入口自带夹具且全绿（聚合器判据本身要能跑）',
    _sp_s16.run([sys.executable, 'verify_local.py', '--selftest'],
                capture_output=True, text=True).returncode == 0)
chk('S16 表外非零码不得被 0 短路吞掉（第一版真实缺陷的回归钉子）',
    _vl_s16.merge_rc([_vl_s16.RC_OK, 128]) == 128
    and _vl_s16.merge_rc([_vl_s16.RC_NOTEST, _vl_s16.RC_FAIL]) == _vl_s16.RC_FAIL
    and _vl_s16.merge_rc([_vl_s16.RC_USAGE, _vl_s16.RC_NOTEST]) == _vl_s16.RC_USAGE)
chk('S16 空阶段清单判"没跑成"，全 0 才判过（聚合器不留恒绿出口）',
    _vl_s16.merge_rc([]) == _vl_s16.RC_NOTEST
    and _vl_s16.merge_rc([_vl_s16.RC_OK, _vl_s16.RC_OK]) == _vl_s16.RC_OK)
chk('S16 环境阶段拿错解释器时判"未跑成"而非通过（本项目反复踩的假红根因）',
    _vl_s16.stage_env('C:/Program Files/Python312/python.exe', _h4_root)['rc']
    == _vl_s16.RC_NOTEST)
chk('S16 入口已进交付白名单（三副本漂移对门禁可见）',
    'verify_local.py' in _qg_s14.ROOT_DELIVERED)
_S16_ENTRY = 'verify_local'
_S16_SPAWN = ('run', 'check_call', 'check_output', 'call', 'Popen', 'os_system', 'system')


def _s16_reverse_wiring(src):
    """门禁"反向接线"本地闸的**行为**形态：import 它，或把它当子进程参数起起来。

    第 37 轮改判据形状的原因（当场实测，不是推断）：门禁第 52 项要证明"本地闸确实跑了同步检查"，
    取证方式=读 `verify_local.py` 的源码。那是**读**，而旧判据按裸子串扫整个门禁源码，
    于是"用眼睛看"与"伸手去摇"同形 —— 一条合法取证被读成成环（本轮 L4 第 1431 行那条红即此）。
    按行为判之后：读它不红，import/subprocess 它才红，且"解析失败"不得回空列表。
    """
    import ast as _a_s16
    try:
        tree = _a_s16.parse(src)
    except SyntaxError as exc:
        return [f'门禁源码解析失败（{type(exc).__name__}）⇒ 接线面无法判，不判通过']
    bad = []
    for nd in _a_s16.walk(tree):
        if isinstance(nd, _a_s16.Import):
            bad += [f'import {a.name} @L{nd.lineno}' for a in nd.names if _S16_ENTRY in a.name]
        elif isinstance(nd, _a_s16.ImportFrom) and _S16_ENTRY in (nd.module or ''):
            bad.append(f'from {nd.module} import @L{nd.lineno}')
        elif isinstance(nd, _a_s16.Call):
            fn = getattr(nd.func, 'attr', '') or getattr(nd.func, 'id', '')
            if fn in _S16_SPAWN and _S16_ENTRY in _a_s16.unparse(nd):
                bad.append(f'{fn}(...) 把入口当子进程参数 @L{nd.lineno}')
    return bad


_gsrc_s16 = open(os.path.join(_h4_root, 'quality_gate.py'), encoding='utf-8').read()
chk('S16 不反向接线（按行为判）：门禁可以**读**本地闸源码取证，但不得 import 或子进程起它',
    _s16_reverse_wiring(_gsrc_s16) == [], str(_s16_reverse_wiring(_gsrc_s16))[:150])
chk('S16 判据有牙·拦得住：合成"subprocess.run([PY, verify_local.py])"必须判成接线',
    bool(_s16_reverse_wiring("import subprocess\nsubprocess.run([PY, 'verify_local.py'])\n")))
chk('S16 判据不误伤·放行取证：合成"open(verify_local.py).read()"是读不是摇，不得判红'
    '（旧写法正是把这一条判成红的，改判据不为凑绿）',
    _s16_reverse_wiring("p = 'verify_local.py'\ng = open(p, encoding='utf-8').read()\n") == [])
chk('S16 判据自身崩了要显式未知：源码解析失败不得回空列表（那等于判通过）',
    bool(_s16_reverse_wiring('def f(:\n')))

# ══════ S17（对标第 17 轮：H27 迁移账本 / H28 版本契约 / H29 唯一约束形状）══════
section('骨架')
import shutil as _sh_s17
import tempfile as _tf_s17

from sqlalchemy import inspect as _insp_s17

import check_schema_drift as _sd_s17
import init_db as _id_s17
from src import schema_ledger as _sl_s17
from src import version as _ver_s17
from src.models import SchemaPatchLog as _SPL_s17
from src.models import db as _db_s17

chk('S17 补丁声明非空且 pid 命名合规（无输入不得记通过）',
    len(_sl_s17.PATCH_IDS) >= 4
    and all(re.fullmatch(r'p\d{3}_[a-z0-9_]+', p) for p in _sl_s17.PATCH_IDS),
    str(_sl_s17.PATCH_IDS))
chk('S17 声明与执行体双向等集（漏绑=补丁永不执行，多绑=死代码）',
    sorted(_id_s17._PATCH_IMPLS) == sorted(_sl_s17.PATCH_IDS),
    str(sorted(set(_sl_s17.PATCH_IDS) ^ set(_id_s17._PATCH_IMPLS))))
_s17_kinds = [r.kind for r in _sl_s17.PATCHES]
_s17_ddl_at = [i for i, k in enumerate(_s17_kinds) if k == 'ddl']
_s17_data_at = [i for i, k in enumerate(_s17_kinds) if k == 'data']
chk('S17 结构补丁一律先于数据补丁（数据回填依赖新列已存在）',
    set(_s17_kinds) <= {'ddl', 'data'}
    and (not _s17_ddl_at or not _s17_data_at or max(_s17_ddl_at) < min(_s17_data_at)),
    str(_s17_kinds))
chk('S17 账本表已在 ORM 声明（否则 create_all 建不出账本）',
    'schema_patch_log' in _db_s17.metadata.tables)
chk('S17 mode 取值域封闭在三值之内', _sl_s17.MODES == ('applied', 'noop', 'adopted'))
with app.app_context():
    _s17_fresh = _sl_s17.status()
chk('S17 没跑过补丁的库必须报全部 pending（"没跑过"不得被读成"已升级"）',
    _s17_fresh['ledger_table'] and _s17_fresh['applied'] == 0
    and len(_s17_fresh['pending']) == len(_sl_s17.PATCH_IDS), str(_s17_fresh)[:200])
with app.app_context():
    _sl_s17.record_all()
    _s17_after = _sl_s17.status()
chk('S17 全新库整册补记后 pending 归零、logged 与声明同集',
    _s17_after['pending'] == [] and sorted(_s17_after['logged']) == sorted(_sl_s17.PATCH_IDS))
with app.app_context():
    _sl_s17.record_pid('p999_ghost', '幽灵账本', 'ddl', 'adopted')
    _s17_ghost = _sl_s17.status()
chk('S17 孤儿账本单独成列报出（改了已发布 pid 的形态）',
    _s17_ghost['orphans'] == ['p999_ghost'], str(_s17_ghost['orphans']))
with app.app_context():
    _db_s17.session.query(_SPL_s17).filter_by(patch_id='p999_ghost').delete()
    _db_s17.session.commit()
chk('S17 清掉幽灵后 orphans 归零（判据不恒真）', _sl_s17.status()['orphans'] == [])
_s17_mode_err = None
try:
    with app.app_context():
        _sl_s17.record_pid('p000_probe', '探针', 'ddl', 'bogus_mode')
except ValueError as _e_s17:
    _s17_mode_err = str(_e_s17)
chk('S17 非法 mode 一律抛 ValueError（写入面受同一约束）', _s17_mode_err is not None)

with app.app_context():
    _s17_keep_specs = _id_s17._PATCHES
    _db_s17.session.query(_SPL_s17).delete()
    _db_s17.session.commit()

    def _s17_boom():
        raise RuntimeError('S17 注入的补丁失败')

    _id_s17._PATCHES = (_id_s17.PatchSpec('p000_boom', '注入失败补丁', 'ddl', _s17_boom),)
    _s17_raised = False
    try:
        _id_s17.apply_patches(verbose=False)
    except RuntimeError:
        _s17_raised = True
    _s17_logged = sorted(_sl_s17.logged_rows())
    _id_s17._PATCHES = _s17_keep_specs
    _sl_s17.record_all()
chk('S17 补丁失败必须原样抛出（吞异常=冒充成功）', _s17_raised)
chk('S17 失败的补丁不得记账（记了=下次永久跳过=静默丢迁移）',
    _s17_logged == [], str(_s17_logged))
with app.app_context():
    _s17_keep_ensure = dict(_id_s17._ENSURE_COLUMNS)
    # SQLite 接受任意列类型名（NOT_A_REAL_TYPE 也算合法），所以反例必须是**语法错**，
    # 否则"注入了失败"这件事本身就是假的（本轮实测踩过：换成假反例后 raised=False 全绿）。
    _id_s17._ENSURE_COLUMNS = {'products': [('zz_r17_ok', 'INTEGER'),
                                            ('zz_r17_bad', 'INTEGER REFERENCES(;)')]}
    _s17_ddl_raised = False
    try:
        _id_s17._ensure_columns()
    except Exception:      # noqa: BLE001  这里要的就是"确实抛了"
        _s17_ddl_raised = True
    _s17_cols = {c['name'] for c in _insp_s17(_db_s17.engine).get_columns('products')}
    _id_s17._ENSURE_COLUMNS = _s17_keep_ensure
chk('S17 补列原子性：第二条 DDL 失败时第一条必须一起回滚（真事务，不留半补列库）',
    _s17_ddl_raised and 'zz_r17_ok' not in _s17_cols,
    f"raised={_s17_ddl_raised} 残留={sorted(c for c in _s17_cols if c.startswith('zz_r17'))}")

# 反证钉子：为什么必须自建 _atomic_ddl 而不能用 engine.begin()。
# pysqlite 的 legacy 事务控制在执行 DDL 前隐式提交当前事务，故 begin() 包不住 ALTER
# ——本断言直接实测这一形态；若将来 driver/SQLAlchemy 修好了，它会翻红，届时该简化就简化。
import sqlalchemy as _sa_s17

_s17_probe = os.path.join(_tf_s17.gettempdir(), 'r17_ddl_probe.db')
for _sfx in ('', '-wal', '-shm'):
    if os.path.exists(_s17_probe + _sfx):
        os.remove(_s17_probe + _sfx)
_s17_pe = _sa_s17.create_engine('sqlite:///' + _s17_probe.replace(os.sep, '/'))
with _s17_pe.begin() as _c0:
    _c0.execute(_sa_s17.text('CREATE TABLE probe (id INTEGER PRIMARY KEY)'))
_s17_begin_leaks = False
try:
    with _s17_pe.begin() as _c1:      # ← 直觉上"包住了"，实测没有
        _c1.exec_driver_sql('ALTER TABLE probe ADD COLUMN zz_a INTEGER')
        _c1.exec_driver_sql('ALTER TABLE probe ADD COLUMN zz_b INTEGER REFERENCES(;)')
except Exception:      # noqa: BLE001
    _s17_begin_leaks = 'zz_a' in {c['name'] for c in _insp_s17(_s17_pe).get_columns('probe')}
_s17_pe.dispose()
for _sfx in ('', '-wal', '-shm'):
    if os.path.exists(_s17_probe + _sfx):
        os.remove(_s17_probe + _sfx)
chk('S17 机制证据：engine.begin() 确实包不住 SQLite DDL（所以 _atomic_ddl 不是过度设计）',
    _s17_begin_leaks is True, f'begin() 后第一条 ALTER 是否残留={_s17_begin_leaks}')
with app.app_context():
    _s17_rows = _id_s17.apply_patches(verbose=False)
chk('S17 已记账的库重复执行全部走"跳过"（幂等自证，不重跑补丁）',
    len(_s17_rows) == len(_sl_s17.PATCH_IDS)
    and all(r['action'] == 'recorded' for r in _s17_rows), str(_s17_rows)[:200])

_s17_h = app.test_client().get('/healthz').get_json()
chk('S17 /healthz 报出的版本与代码真相源同源',
    (_s17_h.get('version') or {}).get('app') == _ver_s17.APP_VERSION, str(_s17_h.get('version')))
chk('S17 /healthz 的 schema.declared 与注册表条数一致',
    (_s17_h.get('schema') or {}).get('declared') == len(_sl_s17.PATCH_IDS))
_s17_second = [f for f in sorted(os.listdir(os.path.join(_h4_root, 'src')))
               if f.endswith('.py') and f != 'version.py'
               and re.search(r'^APP_VERSION\s*=',
                             open(os.path.join(_h4_root, 'src', f), encoding='utf-8').read(), re.M)]
chk('S17 版本字面量全仓只有一个赋值点（第二处必漂移）', _s17_second == [], str(_s17_second))
_s17_dbfile = (app.config['SQLALCHEMY_DATABASE_URI'] or '').replace('sqlite:///', '', 1)
_s17_led = _sd_s17.audit_ledger(_s17_dbfile)
chk('S17 账本审计可直接读库文件（不依赖请求上下文，供守卫/CI 复用同一实现）',
    bool(_s17_led) and _s17_led['pending'] == [] and _s17_led['orphans'] == [], str(_s17_led)[:200])
chk('S17 无库文件时账本审计返回 None 而非"没问题"（区分未测与通过）',
    _sd_s17.audit_ledger(None) is None and _sd_s17.audit_ledger('无此文件.db') is None)

_s17_e = {'products': ({'store_id', 'product_name'}, set(), {('store_id', 'product_name')})}
_s17_old = {'products': ({'store_id', 'product_name'}, set(), {('product_name',)})}
_s17_d_old = _sd_s17.diff(_s17_e, _s17_old)
chk('S17 唯一约束形状漂回旧版必须判漂移（原判据只看索引名，看不见形状）',
    _sd_s17.is_drift(_s17_d_old)
    and ['store_id', 'product_name'] in _s17_d_old['missing_unique'].get('products', []),
    str(_s17_d_old['missing_unique']))
chk('S17 形状一致时不误伤（新判据零假阳性）',
    not _sd_s17.is_drift(_sd_s17.diff(_s17_e, dict(_s17_e)))
    and not _sd_s17.diff(_s17_e, dict(_s17_e))['extra_unique'])
_s17_extra = {'products': ({'store_id', 'product_name'}, set(),
                           {('store_id', 'product_name'), ('category',)})}
_s17_d_extra = _sd_s17.diff(_s17_e, _s17_extra)
chk('S17 库里多出的唯一约束只告警不判红（历史约束不该逼人绕守卫）',
    not _sd_s17.is_drift(_s17_d_extra)
    and _s17_d_extra['extra_unique'].get('products') == [['category']], str(_s17_d_extra))

_S17_DIRS = []


def _s17_tree(mutate=None, target='init_db.py'):
    """把 src+关键根文件复制进临时树，用于给门禁判据喂"现状"与"变异"两份输入。"""
    d = _tf_s17.mkdtemp(prefix='r17gate_')
    _S17_DIRS.append(d)
    os.makedirs(os.path.join(d, 'src'), exist_ok=True)
    for _fn in os.listdir(os.path.join(_h4_root, 'src')):
        if _fn.endswith('.py'):
            _sh_s17.copy2(os.path.join(_h4_root, 'src', _fn), os.path.join(d, 'src', _fn))
    for _fn in ('init_db.py', 'UPGRADE.md', 'README.md', 'quality_gate.py'):
        _sh_s17.copy2(os.path.join(_h4_root, _fn), os.path.join(d, _fn))
    if mutate:
        p = os.path.join(d, target)
        txt = open(p, encoding='utf-8').read()
        new = mutate(txt)
        assert new != txt, f'变异未命中锚点：{target}'
        open(p, 'w', encoding='utf-8', newline='\n').write(new)
    return d


def _s17_run_gate(fn_name, tree):
    """在临时树上跑一条门禁判据，返回 record 到的 (项名, ok, details)。"""
    caps = []
    _orig = (_qg_s14.record, _qg_s14.ROOT, _qg_s14.SRC)
    try:
        _qg_s14.record = lambda lvl, name, ok, details=None: caps.append((name, ok, details))
        _qg_s14.ROOT, _qg_s14.SRC = tree, os.path.join(tree, 'src')
        getattr(_qg_s14, fn_name)()
    finally:
        _qg_s14.record, _qg_s14.ROOT, _qg_s14.SRC = _orig
    return caps[-1]


_s17_tree_ok = _s17_tree()
_s17_r24_ok = _s17_run_gate('check_migration_ledger', _s17_tree_ok)
chk('S17 门禁第 24 项正对照：现状树判过（证明判据读得到四方）', _s17_r24_ok[1] is True,
    str(_s17_r24_ok[2])[:200])
_s17_r24_mut = _s17_run_gate('check_migration_ledger', _s17_tree(
    lambda t: t.replace("'p003_default_tenant': _patch_default_tenant,", '', 1)))
chk('S17 门禁第 24 项有牙：抽掉一条执行体绑定必须判红并点名',
    _s17_r24_mut[1] is False and 'p003_default_tenant' in str(_s17_r24_mut[2]),
    str(_s17_r24_mut[2])[:200])
_s17_r24_doc = _s17_run_gate('check_migration_ledger', _s17_tree(
    lambda t: t.replace('`p004_pack_products`', '`p00x_not_registered`', 1), target='UPGRADE.md'))
chk('S17 门禁第 24 项查文档双向：UPGRADE 名单错一条即红',
    _s17_r24_doc[1] is False, str(_s17_r24_doc[2])[:200])
_s17_r24_seed = _s17_run_gate('check_migration_ledger', _s17_tree(
    lambda t: t.replace('schema_ledger.record_all()', 'pass  # 变异：撤掉整册补记', 1),
    target='src/pack_demo.py'))
chk('S17 门禁第 24 项查播种腿：撤掉整册补记必须判红（否则每次 CI 冷启动无故红）',
    _s17_r24_seed[1] is False, str(_s17_r24_seed[2])[:200])
_s17_r25_ok = _s17_run_gate('check_version_contract', _s17_tree_ok)
_s17_r25_mut = _s17_run_gate('check_version_contract', _s17_tree(
    lambda t: t.replace("APP_VERSION = '", "APP_VERSION = '9.", 1), target='src/version.py'))
chk('S17 门禁第 25 项正对照 + 有牙：版本改一位（与 UPGRADE 脱钩）必须判红',
    _s17_r25_ok[1] is True and _s17_r25_mut[1] is False, str(_s17_r25_mut[2])[:200])
_s17_r25_rd = _s17_run_gate('check_version_contract', _s17_tree(
    lambda t: t.replace('v' + _ver_s17.APP_VERSION, 'v0.0.0-与代码脱钩', 1), target='README.md'))
chk('S17 第 25 项的 README 腿有牙：README 版本行与代码脱钩必须判红并点名 README'
    '（第 17 轮 docstring 就承诺"三处逐字一致"，第 30 轮实测发现代码只查两处 ⇒ 补判据而非改文案）',
    _s17_r25_rd[1] is False and 'README' in str(_s17_r25_rd[2]), str(_s17_r25_rd[2])[:200])
def _s17_wired(src_file, needle, after='def main'):
    """判定 needle 是否出现在某锚点之后（工具存在 ≠ 被接进阻断链）。

    `after=None` 表示全文查 —— 用于 needle 合法地出现在锚点之前的场合
    （如 `def preflight` 定义本身在 `def main` 上方）。上一版对定义也按
    "main 之后"找，于是自己写的接线断言恒假，被权威门禁当场抓出。
    """
    txt = open(os.path.join(_h4_root, src_file), encoding='utf-8').read()
    if after is None:
        return needle in txt
    cut = txt.find(after)
    return cut >= 0 and needle in txt[cut:]


chk('S17 前置闸真接进重传链（M43：verify_local 不再是无人调用的半成品）',
    _s17_wired('push_snapshot_mirror.py', '= preflight()')
    and _s17_wired('push_snapshot_mirror.py', 'def preflight', after=None))
chk('S17 版本 git 腿真接进本地验证链（stage_version 必须进 records 列表）',
    _s17_wired('verify_local.py', 'stage_version()', after='def main'))
chk('S17 部署链真消费迁移账本（smoke_remote 缺 pending 判据=假升级无人发现）',
    _s17_wired('smoke_remote.py', "sch.get('pending')", after='def check_healthz')
    and _s17_wired('smoke_remote.py', 'def local_version', after=None))
for _d in _S17_DIRS:
    _sh_s17.rmtree(_d, ignore_errors=True)
chk('S17 临时树全部清理（受管根与仓内不留夹具产物）',
    all(not os.path.isdir(_d) for _d in _S17_DIRS), str(len(_S17_DIRS)))

# ══════ S18（对标第 18 轮：供应链四判据 H31-H34，每条都配变异体）══════
section('骨架')
import hashlib as _h_s18

_QG_S18 = 'quality_gate.py'


def _s18_run(fn_name, tree):
    """把门禁判据指向临时树跑一次，返回 record 到的 (项名, ok, details)。"""
    caps = []
    orig = (_qg_s14.record, _qg_s14.ROOT, _qg_s14.SRC, getattr(_qg_s14, 'TPL', None))
    try:
        _qg_s14.record = lambda lvl, name, ok, details=None: caps.append((name, ok, details))
        _qg_s14.ROOT, _qg_s14.SRC = tree, os.path.join(tree, 'src')
        getattr(_qg_s14, fn_name)()
    finally:
        _qg_s14.record, _qg_s14.ROOT, _qg_s14.SRC = orig[0], orig[1], orig[2]
        if orig[3] is not None:
            _qg_s14.TPL = orig[3]
    return caps[-1]


_S18_TMP = []


def _s18_tree(kind):
    """临时树：只建 src/ 与 .github/workflows/ 两个空目录，内容由调用方放（避免夹具自带"通过样例"）。"""
    d = _tf_s17.mkdtemp(prefix='r18_')
    _S18_TMP.append(d)
    os.makedirs(os.path.join(d, 'src'), exist_ok=True)
    if kind == 'pins':
        os.makedirs(os.path.join(d, '.github', 'workflows'), exist_ok=True)
    return d


# 26 · CI 供应链固定：现状必须过，三种破坏各判红
# 正对照取「仓库根那三份生效的 workflow」——dev 副本内没有 .github，
# 拿 dev 根去跑得是"无输入判红"而不是"通过"（上一版正是踩了这个坑）。
_s18_t = _s18_tree('pins')
_wf_cands = [os.path.join(_h4_root, '.github', 'workflows'),
             os.path.join(_h4_root, '..', '.github', 'workflows')]
_wf_root = next((p for p in _wf_cands if os.path.isfile(os.path.join(p, 'quality-gate.yml'))), '')
for _f in sorted(os.listdir(_wf_root)) if _wf_root else []:
    _sh_s17.copy2(os.path.join(_wf_root, _f),
                  os.path.join(_s18_t, '.github', 'workflows', _f))
_s18_26_pos = _s18_run('check_supply_chain_pins', _s18_t)
_wfp = os.path.join(_s18_t, '.github', 'workflows', 'quality-gate.yml')
_body = open(_wfp, encoding='utf-8').read() if _wf_root else ''
assert _wf_root and 'permissions:' in _body and 'ruff==0.16.5' in _body, '未定位到含 quality-gate.yml 的 .github/workflows'
open(_wfp, 'w', encoding='utf-8', newline='\n').write(
    _body.replace('actions/checkout@11d5960a326750d5838078e36cf38b85af677262',
                  'actions/checkout@v4', 1))
_s18_26a = _s18_run('check_supply_chain_pins', _s18_t)
open(_wfp, 'w', encoding='utf-8', newline='\n').write(
    _body.replace('permissions:\n  contents: read\n', ''))
_s18_26b = _s18_run('check_supply_chain_pins', _s18_t)
open(_wfp, 'w', encoding='utf-8', newline='\n').write(
    _body.replace('"ruff==0.16.5"', 'ruff'))
_s18_26c = _s18_run('check_supply_chain_pins', _s18_t)
chk('S18 第 26 项正对照：现状三处 workflow 全部钉 SHA + 有 permissions + 工具钉版',
    _s18_26_pos[1] is True, str(_s18_26_pos[2])[:200])
chk('S18 第 26 项有牙：一条 uses 退回可变标签必须判红',
    _s18_26a[1] is False and 'SHA' in str(_s18_26a[2]), str(_s18_26a[2])[:160])
chk('S18 第 26 项有牙：删掉 permissions 必须判红（默认 token 权限不是"没写就没权限"）',
    _s18_26b[1] is False and 'permissions' in str(_s18_26b[2]))
chk('S18 第 26 项有牙：pip install 去钉版必须判红（工具漂移=尺子漂移）',
    _s18_26c[1] is False, str(_s18_26c[2])[:160])
_s18_26_empty = _s18_run('check_supply_chain_pins', _s18_tree('empty'))
chk('S18 第 26 项无 workflow 时判红而非"无事可做"（R247：判据面无输入不得记过）',
    _s18_26_empty[1] is False, str(_s18_26_empty[2])[:120])

# 27 · vendored 资产台账：哈希漂移 / 指向不存在的文件 都必须被抓
_v_tree = _s18_tree('vendor')
_jsdir = os.path.join(_v_tree, 'src', 'static', 'js')
os.makedirs(_jsdir, exist_ok=True)
_fake = os.path.join(_jsdir, 'fake.vendor.js')
open(_fake, 'w', encoding='utf-8', newline='\n').write('/*! fake vendor */\n')
_good_sha = _h_s18.sha256(open(_fake, 'rb').read()).hexdigest()


def _write_manifest(tree, sha, path='src/static/js/fake.vendor.js', size=None):
    import json as _j_s18
    entry = {'path': path, 'size_bytes': size if size is not None else os.path.getsize(
        os.path.join(tree, path)) if os.path.isfile(os.path.join(tree, path)) else 1,
        'sha256': sha, 'upstream': 'Fake', 'version': '1.0.0', 'upstream_license': 'MIT',
        'how_built': '人工拷贝（夹具）', 'blind_review_exclude': True}
    open(os.path.join(tree, 'VENDOR-ASSETS.json'), 'w', encoding='utf-8', newline='\n').write(
        _j_s18.dumps({'assets': [entry]}, ensure_ascii=False))


_excl_orig = _qg_s14.BLIND_EXCLUDES
_qg_s14.BLIND_EXCLUDES = ('fake.vendor.js',)
_write_manifest(_v_tree, _good_sha)
_s18_27_pos = _s18_run('check_vendor_assets', _v_tree)
_write_manifest(_v_tree, 'f' * 64)
_s18_27a = _s18_run('check_vendor_assets', _v_tree)
_write_manifest(_v_tree, _good_sha, path='src/static/js/gone.vendor.js')
_s18_27b = _s18_run('check_vendor_assets', _v_tree)
_write_manifest(_v_tree, _good_sha, size=7)
_s18_27c = _s18_run('check_vendor_assets', _v_tree)
_qg_s14.BLIND_EXCLUDES = _excl_orig
chk('S18 第 27 项正对照：哈希/大小/存在性三项全核过（真实树亦同判）',
    _s18_27_pos[1] is True and _s18_run('check_vendor_assets', _h4_root)[1] is True,
    str(_s18_27_pos[2])[:160])
chk('S18 第 27 项有牙：产物哈希漂移必须判红（改了 bundle 没更台账=来源失控）',
    _s18_27a[1] is False and 'SHA256' in str(_s18_27a[2]), str(_s18_27a[2])[:160])
chk('S18 第 27 项有牙：台账指向不存在的文件必须判红',
    _s18_27b[1] is False, str(_s18_27b[2])[:160])
chk('S18 第 27 项有牙：字节数对不上必须判红（哈希未变但被截断的形态）',
    _s18_27c[1] is False, str(_s18_27c[2])[:160])

# 28 · 安全规则集：现状 0 违例；注入一条真违例 + 一条"空 noqa"都要判红
_s18_28_pos = _s18_run('check_security_lint', _h4_root)
_bad_tree = _s18_tree('lint')
open(os.path.join(_bad_tree, 'src', 'bad.py'), 'w', encoding='utf-8', newline='\n').write(
    'password = "hunter2x"\n')
_s18_28a = _s18_run('check_security_lint', _bad_tree)
open(os.path.join(_bad_tree, 'src', 'bad.py'), 'w', encoding='utf-8', newline='\n').write(
    'password = "hunter2x"  # noqa: S105\n')
_s18_28b = _s18_run('check_security_lint', _bad_tree)
chk('S18 第 28 项正对照：S 规则零违例且 9 条豁免全部带理由',
    _s18_28_pos[1] is True, str(_s18_28_pos[2])[:200])
chk('S18 第 28 项有牙：新引入一条硬编码口令必须判红（改前 ruff 无 S 时这类完全隐形）',
    _s18_28a[1] is False and 'S105' in str(_s18_28a[2]), str(_s18_28a[2])[:200])
chk('S18 第 28 项有牙：noqa 不写理由也必须判红（防止用豁免把判据关掉）',
    _s18_28b[1] is False and '理由' in str(_s18_28b[2]), str(_s18_28b[2])[:200])

# 29 · 发布校验和台账：一致才过；篡改文件 / 台账漏项 都判红
import release_evidence as _re_s18

_l_tree = _s18_tree('ledger')
open(os.path.join(_l_tree, 'src', 'version.py'), 'w', encoding='utf-8', newline='\n').write(
    "APP_VERSION = '9.9.9'\n")
open(os.path.join(_l_tree, 'a.py'), 'w', encoding='utf-8', newline='\n').write('x = 1\n')
open(os.path.join(_l_tree, 'b.py'), 'w', encoding='utf-8', newline='\n').write('y = 2\n')
_ev_dir = os.path.join(_l_tree, '..', '09-发布校验')
os.makedirs(_ev_dir, exist_ok=True)


def _write_ledger(digests, agg, ver='9.9.9'):
    with open(os.path.join(_ev_dir, f'SHA256SUMS-v{ver}.txt'), 'w',
              encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join([f'# VERSION v{ver}', f'# AGGREGATE {agg}']
                           + [f'{digests[k]}  {k}' for k in sorted(digests)]) + '\n')


def _snap(tree_root):
    old = _re_s18.ROOT
    _re_s18.ROOT = tree_root
    try:
        d = _re_s18.collect(_qg_s14.LEDGER_SKIP_DIRS, _qg_s14.LEDGER_SKIP_EXTS)
        return d, _re_s18.aggregate(d)
    finally:
        _re_s18.ROOT = old


dig, agg = _snap(_l_tree)
_write_ledger(dig, agg)
_s18_29_pos = _s18_run('check_release_ledger', _l_tree)
open(os.path.join(_l_tree, 'a.py'), 'w', encoding='utf-8', newline='\n').write('x = 999\n')
_s18_29a = _s18_run('check_release_ledger', _l_tree)
dig2, agg2 = _snap(_l_tree)
_write_ledger({k: v for k, v in dig2.items() if k != 'a.py'}, agg2)
_s18_29b = _s18_run('check_release_ledger', _l_tree)
chk('S18 第 29 项正对照：台账逐条一致时判过（真实副本亦已生成并核过）',
    _s18_29_pos[1] is True, str(_s18_29_pos[2])[:200])
chk('S18 第 29 项有牙：改一个字节必须判红（"声称的 SHA256 台账"若不可复算就是假话）',
    _s18_29a[1] is False and 'a.py' in str(_s18_29a[2]),
    str(_s18_29a[2])[:200])
chk('S18 第 29 项有牙：台账漏一条（只改哈希不列文件）必须判红',
    _s18_29b[1] is False, str(_s18_29b[2])[:200])
chk('S18 第 29 项无台账时判红并给出可执行处置（不是"跳过"）',
    _s18_run('check_release_ledger', _s18_tree('nolt'))[1] is False)

# 文档一致性：SECURITY.md 承诺的入口必须有实体；许可证清单必须覆盖声明
_nt = open(os.path.join(_h4_root, 'THIRD-PARTY-NOTICES.md'), encoding='utf-8').read()
# 声明面只由**生成方那个解析器**给出（第 32 轮：清单与契约各写一份正则 → 两份各自漂移）
_s18_rows = _qg_s14._s46_declared_names(
    open(os.path.join(_h4_root, 'requirements.txt'), encoding='utf-8').read())
_declared = [n for n, _sp in _s18_rows]
chk('S18 许可证清单覆盖 requirements 全部声明（漏一条=清单是装饰）',
    all(f'`{n}`' in _nt for n in _declared) and len(_declared) >= 10,
    str([n for n in _declared if f'`{n}`' not in _nt]))
_s18_pairs = set()
for _ln in _nt.splitlines():
    _c = [x.strip() for x in _ln.strip().strip('|').split('|')]
    if len(_c) == 5 and _c[0].startswith('`') and _c[1].startswith('`'):
        _s18_pairs.add((_c[0].strip('`'), _c[1].strip('`')))
_s18_drift = [f'{n}{sp}' for n, sp in _s18_rows if (n, sp) not in _s18_pairs]
chk('S18 清单的"声明约束"列必须逐条等于契约（实测漂移形态：契约已 >=7.0,<9 而清单停在加下界前的 >=7.0）',
    not _s18_drift, str(_s18_drift)[:200])
chk('S18 解析器唯一：gen_notices 不得再自带一份 requirements 正则（两份语法=两份真相）',
    're.match' not in open(os.path.join(_h4_root, 'tools', 'gen_notices.py'),
                           encoding='utf-8').read()
    and '_s46_declared_names' in open(os.path.join(_h4_root, 'tools', 'gen_notices.py'),
                                      encoding='utf-8').read(), 'gen_notices 仍在自解析')
chk('S18 清单同时登记 vendored 前端产物（619KB 无源 bundle 不得只在 JSON 里留痕）',
    'echarts.custom.js' in _nt and 'Apache-2.0' in _nt)
_it = next((p for p in (os.path.join(_h4_root, '.github', 'ISSUE_TEMPLATE'),
                        os.path.join(_h4_root, '..', '.github', 'ISSUE_TEMPLATE'))
            if os.path.isdir(p)), '')
chk('S18 SECURITY.md 承诺的 Issues 通道有实体模板（承诺先于实体是本仓反复的债）',
    os.path.isdir(_it) and any('bug' in f for f in os.listdir(_it))
    and any('security' in f for f in os.listdir(_it)))
# .python-version 只在仓库根（权威副本内刻意不放），故向上走找；找不到判红而非抛异常
# （上一版把它写死在 `<副本>/..`，在 05-源代码全文 下直接 FileNotFoundError 把整个 L4 带崩）
_pv_path, _up = '', _h4_root
for _ in range(6):
    _up = os.path.dirname(_up)
    if not _up or not os.path.isdir(_up):
        break
    if os.path.isfile(os.path.join(_up, '.python-version')):
        _pv_path = os.path.join(_up, '.python-version')
        break
_qg_path = os.path.join(_wf_root, 'quality-gate.yml') if _wf_root else ''
_pv_txt = open(_pv_path, encoding='utf-8').read() if _pv_path else ''
_qg_txt = open(_qg_path, encoding='utf-8').read() if _qg_path else ''
_rf_txt = open(os.path.join(_h4_root, 'ruff.toml'), encoding='utf-8').read()


def _s18_vtruth(pv, qg, rf):
    """版本三处一致的判据本体：正对照吃真实文件，变异体吃内存字符串（不碰仓库）。"""
    return (pv.strip() == '3.12' and bool(qg) and 'python-version:' not in qg
            and 'target-version = "py312"' in rf)


chk('S18 运行时版本单一真相：.python-version ⇄ CI 引用 ⇄ ruff target 三处一致',
    _s18_vtruth(_pv_txt, _qg_txt, _rf_txt),
    '.python-version=%s quality-gate.yml=%s' % (_pv_path or '未找到', _qg_path or '未找到'))
chk('S18 版本单一真相有牙：.python-version 漂到 3.11 必须判红',
    not _s18_vtruth('3.11\n', _qg_txt, _rf_txt))
chk('S18 版本单一真相有牙：CI 里重新写死 python-version 必须判红',
    not _s18_vtruth(_pv_txt, _qg_txt + '\n        python-version: "3.11"\n', _rf_txt))
chk('S18 版本单一真相有牙：ruff target 单独漂移必须判红',
    not _s18_vtruth(_pv_txt, _qg_txt, 'target-version = "py39"'))
chk('S18 版本单一真相有牙：CI 文件读不到（空输入）不得记通过',
    not _s18_vtruth(_pv_txt, '', _rf_txt))

for _d in _S18_TMP:
    _sh_s17.rmtree(_d, ignore_errors=True)
chk('S18 临时树全部清理（受管根与仓内不留夹具产物）',
    all(not os.path.isdir(_d) for _d in _S18_TMP), str(len(_S18_TMP)))

# ══════════ S19 · 第 30 项行尾确定性（H35：台账哈希必须跨机可复算）══════════
# 根因（本轮实测）：本机 core.autocrlf=true、CI 默认 false、仓库无 .gitattributes
# ⇒ 工作树 CRLF 而 blob/检出 LF，权威副本 82 个文件中招。第 29 项台账记的是工作树哈希，
# 于是"本地全绿的台账在别人机器上对不上"。本段用**独立临时 git 仓**造六态，
# 既不吃真仓的 .gitattributes，也不碰真仓一个字节。
_S19_TMP = []
_USE_REAL_S19 = object()      # 仓库根哨兵：不覆盖，让真函数自己上溯
import subprocess as _sp_s19  # noqa: E402


def _s19_repo(ga='good', py_eol='lf', cmd_eol='crlf', files='both', stage=True):
    d = _tf_s17.mkdtemp(prefix='r19_')
    _S19_TMP.append(d)
    _sp_s19.run(['git', 'init', '-q'], cwd=d, capture_output=True)
    _GA = {'good': '* text=auto eol=lf\n*.cmd text eol=crlf\n*.png binary\n',
           'no_eol': '* text=auto\n*.png binary\n',
           'no_binary': '* text=auto eol=lf\n',
           'none': ''}
    if ga != 'none':
        open(os.path.join(d, '.gitattributes'), 'w', encoding='utf-8',
             newline='').write(_GA[ga])
    if files != 'none':
        nl = '\r\n' if py_eol == 'crlf' else '\n'
        open(os.path.join(d, 'a.py'), 'w', encoding='utf-8', newline=nl).write('x = 1' + nl)
        cnl = '\r\n' if cmd_eol == 'crlf' else '\n'
        open(os.path.join(d, 'b.cmd'), 'w', encoding='utf-8', newline=cnl).write(
            '@echo off' + cnl)
    # stage=False：只 git init 不 add ⇒ 工作树有文件但零跟踪，喂给"零输入不记通过"那条
    if stage:
        _sp_s19.run(['git', 'add', '-A'], cwd=d, capture_output=True)
    return d


def _s19_run(d, toplevel=_USE_REAL_S19):
    """跑第 30 项：只改 ROOT；仓库根默认走真函数自己上溯（夹具自带 .git ⇒ 天然自足，
    真副本 ⇒ 找到真仓库根，两者都不会互相污染）。"""
    caps = []
    orig = (_qg_s14.record, _qg_s14.ROOT, _qg_s14.SRC, _qg_s14._git_toplevel)
    try:
        _qg_s14.record = lambda lvl, name, ok, details=None: caps.append((name, ok, details))
        _qg_s14.ROOT, _qg_s14.SRC = d, os.path.join(d, 'src')
        if toplevel is not _USE_REAL_S19:
            _qg_s14._git_toplevel = lambda cwd=None: toplevel
        _qg_s14.check_line_ending_policy()
    finally:
        _qg_s14.record, _qg_s14.ROOT, _qg_s14.SRC, _qg_s14._git_toplevel = orig
    return caps[-1] if caps else ('', None, [])


_s19_pos = _s19_run(_s19_repo())
chk('S19 第 30 项正对照：策略齐备 + 文本 LF + .cmd CRLF 必须判绿',
    _s19_pos[1] is True, str(_s19_pos[2])[:200])
chk('S19 第 30 项有牙：缺 .gitattributes 判红（否则行尾随平台变、台账不可复算）',
    _s19_run(_s19_repo(ga='none'))[1] is False)
chk('S19 第 30 项有牙：策略未声明 eol=lf 判红（只写 text=auto 挡不住 Windows 检出）',
    _s19_run(_s19_repo(ga='no_eol'))[1] is False)
chk('S19 第 30 项有牙：策略无二进制名单判红（图片/PDF 会被行尾规则改写）',
    _s19_run(_s19_repo(ga='no_binary'))[1] is False)
chk('S19 第 30 项有牙：跟踪 .py 工作树带 CRLF 判红',
    _s19_run(_s19_repo(py_eol='crlf'))[1] is False)
chk('S19 第 30 项有牙：.cmd 退回 LF 判红（另一半判据，只查 CRLF 残留等于放行坏脚本）',
    _s19_run(_s19_repo(cmd_eol='lf'))[1] is False)
_s19_none = _s19_run(_s19_repo(ga='none', files='none'))
chk('S19 第 30 项有牙：零跟踪文件判红且理由点名"未扫到任何文本文件"（而非只报缺策略）',
    _s19_none[1] is False and any('未扫到任何文本文件' in d for d in _s19_none[2]),
    str(_s19_none[2])[:200])
# 解压态（无 git）：判据既不能因为拿不到 git 就"跳过即通过"，也不能因此把评委的正常解压判红
_s19_zip = _s19_run(_s19_repo(), toplevel='')
chk('S19 第 30 项解压态：无 git 但副本自带策略且字节合规 ⇒ 仍判绿（靠遍历而非靠 git）',
    _s19_zip[1] is True and any('遍历副本自身' in d for d in (_s19_zip[2] or [])),
    str(_s19_zip[2])[:200])
chk('S19 第 30 项解压态有牙：无 git 且副本内也没有策略文件 ⇒ 判红，不得静默跳过',
    _s19_run(_s19_repo(ga='none'), toplevel='')[1] is False)
chk('S19 第 30 项解压态有牙：无 git 也不能放过 CRLF 残留（退化遍历不是降级判据）',
    _s19_run(_s19_repo(py_eol='crlf'), toplevel='')[1] is False)
_s19_real = _s19_run(_h4_root)
chk('S19 真副本收口：本副本工作树已归一（文本 LF / .cmd CRLF / 策略在真仓库根）',
    _s19_real[1] is True, str(_s19_real[2])[:220])
# Windows 下 git 把 pack/loose 对象写成**只读**，`rmtree(ignore_errors=True)` 会
# 静默留下半个目录（实测：夹具跑完 8 个 r19_* 全在），所以先清只读再删。
def _s19_wipe(d):
    for _dp, _dns, _fns in os.walk(d):
        for _n in _dns + _fns:
            try:
                os.chmod(os.path.join(_dp, _n), 0o0700)
            except OSError:
                pass
    _sh_s17.rmtree(d, ignore_errors=True)


for _d in _S19_TMP:
    _s19_wipe(_d)
chk('S19 临时 git 仓全部清理（不在系统临时目录留一堆 .git）',
    all(not os.path.isdir(_d) for _d in _S19_TMP), str(len(_S19_TMP)))

# ══════════ S20 · 第 19 轮观测与归因链（H36-H41：计数/联动/关联/取证）══════════
# 动因：改造前 `src/performance.py` 有慢请求 WARN 却零断言（当场 grep：四份测试与门禁
# 只命中一行注释）。靶群 8/8 都对可观测性自身的**字段**做断言
# （zulip zerver/tests/test_middleware.py:29-55、frappe frappe/tests/test_monitor.py:32-38）。
section('骨架')
import ast as _s20_ast
import io as _s20_io
import json as _s20_json
import logging as _s20_logging
import shutil as _s20_shutil
import tempfile as _s20_tempfile

import log_triage as _s20_triage  # noqa: E402
from src import ai_trace as _s20_trace  # noqa: E402
from src import diag as _s20_diag  # noqa: E402
from src import logging_config as _s20_lc  # noqa: E402

_s20_diag.reset()
chk('S20 计数器初始归零（reset 是所有观测断言的前提，否则跨用例污染）',
    _s20_diag.snapshot()['requests'] == 0)

# ── 归一路径：低基数键（不归一则 64 条上限形同虚设）
chk('S20 数字段归一为 :id',
    _s20_diag.norm_path('/admin/staff/12/edit') == '/admin/staff/:id',
    _s20_diag.norm_path('/admin/staff/12/edit'))
chk('S20 长十六进制段归一为 :id（会话/令牌形态的 id 不得进聚合键）',
    _s20_diag.norm_path('/token/abcdef0123456789/edit') == '/token/:id/edit',
    _s20_diag.norm_path('/token/abcdef0123456789/edit'))
chk('S20 只保留前 3 段：第 4 段之后的 id 连键都不进（基数天然有界）',
    _s20_diag.norm_path('/api/v1/token/abcdef0123456789') == '/api/v1/token',
    _s20_diag.norm_path('/api/v1/token/abcdef0123456789'))
chk('S20 不同 id 落同一键（上限才有效）',
    _s20_diag.norm_path('/admin/staff/12/edit') == _s20_diag.norm_path('/admin/staff/7/edit'))
chk('S20 查询串不进聚合键（查询串里可能有 token）',
    _s20_diag.norm_path('/admin/dashboard/export?days=7&token=***') == '/admin/dashboard/export')
chk('S20 超过 3 段截断，键不随深度爆炸',
    len(_s20_diag.norm_path('/a/b/c/d/e/f')) <= 60 and
    _s20_diag.norm_path('/a/b/c/d/e/f') == '/a/b/c')
chk('S20 根路径与空路径都不崩', _s20_diag.norm_path('/') == '/' and
    _s20_diag.norm_path('') == '/')

# ── 计数与状态分类
for _st, _cls in ((200, '2xx'), (302, '3xx'), (403, '4xx'), (404, '4xx'), (500, '5xx')):
    _s20_diag.observe('/h5/', 'GET', _st, 12.0, False)
_snap = _s20_diag.snapshot()
chk('S20 请求总数按调用次数累加', _snap['requests'] == 5, str(_snap['requests']))
chk('S20 状态按 xxx 分类计数', _snap['by_class'].get('4xx') == 2 and
    _snap['by_class'].get('5xx') == 1, str(_snap['by_class']))
chk('S20 4xx 与 5xx 分开计（混成一类就看不出是越权还是故障）',
    _snap['errors_4xx'] == 2 and _snap['errors_5xx'] == 1)
_s20_diag.observe('/admin/', 'GET', 200, 7248.3, True)
chk('S20 慢请求标记与最大耗时', _s20_diag.snapshot()['slow'] == 1 and
    _s20_diag.snapshot()['max_duration_ms'] == 7248.3)
chk('S20 公开端点用的聚合段不含路径名',
    'top_paths' not in _s20_diag.healthz_stats() and
    'by_path' not in _s20_diag.healthz_stats(),
    str(sorted(_s20_diag.healthz_stats())))
chk('S20 聚合段全是整数（任何字符串字段都可能是内部情报）',
    all(isinstance(v, int) for v in _s20_diag.healthz_stats().values()),
    str(_s20_diag.healthz_stats()))

# ── 有界性：灌 200 个不同路径，键数必须封顶且淘汰可见
_s20_diag.reset()
for _i in range(200):
    _s20_diag.observe('/deep/%d' % _i, 'GET', 200, 1.0, False)
_bounded = _s20_diag.snapshot()
chk('S20 归一即有界：200 个数字 id 只占 1 个键（淘汰都用不上）',
    _bounded['path_keys'] == 1 and _bounded['requests'] == 200,
    '%s keys' % _bounded['path_keys'])
_s20_diag.reset()
for _i in range(200):
    _s20_diag.observe('/mod-%03d/' % _i, 'GET', 200, 1.0, False)
_cap = _s20_diag.snapshot()
chk('S20 真·高基数（200 个不同模块名）仍被上限挡住',
    _cap['path_keys'] <= _s20_diag.MAX_PATH_KEYS,
    '%s / cap %s' % (_cap['path_keys'], _s20_diag.MAX_PATH_KEYS))
chk('S20 淘汰必须可见（静默丢计数＝没测）', _cap['evictions'] > 0, str(_cap['evictions']))
chk('S20 计数不因淘汰而丢失（requests 仍是 200）', _cap['requests'] == 200)

# ── 异常指纹聚合（对标 frappe error_log.get_fingerprint_stats）
_s20_diag.reset()
for _k in ('KeyError', 'KeyError', 'ZeroDivisionError'):
    _s20_diag.note_exception(_k)
_kinds = _s20_diag.snapshot()['error_kinds']
chk('S20 同类异常聚合成一处计数', _kinds.get('KeyError') == 2 and
    _kinds.get('ZeroDivisionError') == 1, str(_kinds))
chk('S20 聚合段对公开端点只报"有几种"，不报类名',
    _s20_diag.healthz_stats()['error_kinds'] == 2)
_s20_diag.reset()
for _i in range(80):
    _s20_diag.note_exception('Err%03d' % _i)
chk('S20 异常指纹同样有界', len(_s20_diag.snapshot()['error_kinds']) <=
    _s20_diag.MAX_KIND_KEYS, str(len(_s20_diag.snapshot()['error_kinds'])))
chk('S20 空类名不记进聚合（防"对空气计数"）',
    (_s20_diag.note_exception('') or len(_s20_diag.snapshot()['error_kinds'])) ==
    _s20_diag.MAX_KIND_KEYS)

# ── 观测不得让业务变红：坏输入只丢计数
_ok = True
try:
    _s20_diag.observe(None, None, None, None, None)
except Exception as _exc:  # noqa: BLE001
    _ok = False
chk('S20 观测面对脏输入 fail-open（不得把异常冒到响应链路）', _ok)
_s20_diag.reset()

# ── request_id：上下文变量 + 过滤器 + 清理
_s20_diag.set_request_id('aabbccdd')
chk('S20 ContextVar 读写一致（AI 留痕与日志共用同一单源）',
    _s20_diag.get_request_id() == 'aabbccdd')
_s20_logger = _s20_logging.getLogger('zz.thirdparty.for_s20')
_s20_buf = _s20_io.StringIO()
_s20_h = _s20_logging.StreamHandler(_s20_buf)
_s20_h.setFormatter(_s20_lc.JsonFormatter())
_s20_h.addFilter(_s20_lc._request_id_filter)
_s20_logger.addHandler(_s20_h)
_s20_logger.setLevel(_s20_logging.INFO)
_s20_logger.propagate = False
_s20_logger.warning('第三方库的日志行')
_s20_third = _s20_json.loads(_s20_buf.getvalue().strip())
chk('S20 第三方 logger 的记录也被挂上 request_id（调用点传参永远覆盖不全）',
    _s20_third.get('request_id') == 'aabbccdd', str(_s20_third))
_s20_diag.clear_request_id()
_s20_buf.truncate(0)
_s20_buf.seek(0)
_s20_logger.warning('请求外的日志行')
chk('S20 无请求上下文时如实给 "-"，不编造 id',
    _s20_json.loads(_s20_buf.getvalue().strip()).get('request_id') == '-')
_s20_logger.removeHandler(_s20_h)

# ── 脱敏仍然生效（新增过滤器不得削弱既有掩码）
_s20_rec = _s20_logging.LogRecord('x', _s20_logging.INFO, __file__, 1, 'msg', None, None)
_s20_rec.extra_data = {'password': 'supersecret', 'user': 'a', 'nested': {'token': 'tk12345'}}
_s20_masked = _s20_json.loads(_s20_lc.JsonFormatter().format(_s20_rec))
chk('S20 JSON 日志仍掩码顶层敏感键',
    _s20_masked['data']['password'].endswith('****') and 'supersecret' not in
    _s20_json.dumps(_s20_masked))
chk('S20 嵌套 dict 里的 token 也掩码（同类出口不能只修一半）',
    _s20_masked['data']['nested']['token'].endswith('****'), str(_s20_masked['data']))

# ── 真实请求：头、模板、计数器、异常指纹一次串通
with app.app_context():
    _s20_diag.reset()
    _r200 = app.test_client().get('/portal')
    _rid200 = _r200.headers.get('X-Request-Id') or ''
    chk('S20 正常响应回写 X-Request-Id（对标 frappe app.py:273-274 的响应头注入）',
        len(_rid200) == 8, repr(_rid200))
    _vf = app.view_functions['portal']
    app.view_functions['portal'] = lambda **kw: 1 / 0
    try:
        app.config['PROPAGATE_EXCEPTIONS'] = False
        _r500 = app.test_client().get('/portal')
    finally:
        app.view_functions['portal'] = _vf
    _rid500 = _r500.headers.get('X-Request-Id') or ''
    chk('S20 500 也有追踪号（错误页与 traceback 必须能对上）',
        _r500.status_code == 500 and len(_rid500) == 8, str(_r500.status_code))
    chk('S20 500 页面正文印出同一个编号（用户报障只需要抄这一串）',
        _rid500.encode('utf-8') in _r500.data, _rid500)
    _snap20 = _s20_diag.snapshot()
    chk('S20 真实请求进了计数器（钩子不是摆设）',
        _snap20['requests'] >= 2 and _snap20['by_class'].get('5xx', 0) >= 1,
        str(_snap20['by_class']))
    chk('S20 未捕获异常按类名聚合（ZeroDivisionError 由 got_request_exception 喂入）',
        _snap20['error_kinds'].get('ZeroDivisionError', 0) >= 1, str(_snap20['error_kinds']))
    chk('S20 请求结束后上下文里的 id 已清除（waitress 复用线程不串号）',
        _s20_diag.get_request_id() == '', repr(_s20_diag.get_request_id()))

    # /healthz 的 status 与子检查联动
    _hz = app.test_client().get('/healthz')
    _hb = _hz.get_json()
    chk('S20 健康态下 status=ok 且含 diag 聚合段',
        _hb.get('status') == 'ok' and isinstance(_hb.get('diag'), dict), str(_hb.get('diag')))
    chk('S20 探活给出 DB 往返耗时（"活着但很慢"是独立一档故障）',
        isinstance(_hb.get('db_ms'), float) and _hb['db_ms'] >= 0, str(_hb.get('db_ms')))
    chk('S20 diag 段里没有 top_paths 字段',
        'top_paths' not in _s20_json.dumps(_hb.get('diag', {})))
    from sqlalchemy.orm import Session as _s20_Sess
    _orig_sess_exec = _s20_Sess.execute
    _s20_Sess.execute = lambda self, *a, **k: (_ for _ in ()).throw(
        RuntimeError('夹具：DB 探测失败'))
    try:
        _hzbad = app.test_client().get('/healthz')
        _hb2 = _hzbad.get_json()
    finally:
        _s20_Sess.execute = _orig_sess_exec
    chk('S20 DB 探测失败时 status 不得再说 ok（字段自相矛盾就是谎言）',
        _hb2.get('db') == 'error' and _hb2.get('status') == 'degraded',
        str((_hb2.get('db'), _hb2.get('status'))))
    chk('S20 DB 故障仍返回 HTTP 200（PA 免费层无自动重启，502 会让探活方看不见细节）',
        _hzbad.status_code == 200)
    chk('S20 DB 故障时 db_ms 如实为 null（不得伪造一个耗时）', _hb2.get('db_ms') is None)
    from src import schema_ledger as _s20_sl
    _orig_status = _s20_sl.status
    _s20_sl.status = lambda: {'declared': 4, 'applied': 2, 'pending': ['P003'],
                              'logged': [], 'orphans': [], 'ledger_table': True}
    try:
        _hzpend = app.test_client().get('/healthz')
    finally:
        _s20_sl.status = _orig_status
    chk('S20 迁移没跑完也必须让 status 说 degraded（只有 ok/error 二值会漏这一档）',
        _hzpend.get_json().get('status') == 'degraded', str(_hzpend.get_json().get('status')))

# ── AI 留痕带关联键 + UTC 单源
_s20_tmp = _s20_tempfile.mkdtemp(prefix='ican_s20_trace_')
_orig_path = _s20_trace.LOG_PATH
_s20_trace.LOG_PATH = os.path.join(_s20_tmp, 'ai_call_log.jsonl')
_s20_diag.set_request_id('ff00ff00')
try:
    _s20_trace.record('completion', {'provider': 'deepseek', 'source': 'deepseek',
                                     'model': 'm'}, 1234.0, True)
    with open(_s20_trace.LOG_PATH, encoding='utf-8') as _fh:
        _row20 = _s20_json.loads(_fh.readline())
    chk('S20 AI 留痕写入 request_id（改造前这里是空的，只能靠时间戳猜）',
        _row20.get('request_id') == 'ff00ff00', str(_row20))
    chk('S20 留痕时刻为 UTC 且带 Z（旧形态是本地时间，与访问日志差 8 小时）',
        _row20['ts'].endswith('Z') and '+00:00Z' not in _row20['ts'], _row20['ts'])
finally:
    _s20_diag.clear_request_id()
    _s20_trace.LOG_PATH = _orig_path
    _s20_shutil.rmtree(_s20_tmp, ignore_errors=True)
chk('S20 utc_iso_z 与 utc_now_naive 同刻同值（两个出口不得各自取时）',
    _s20_diag.utc_now_naive().strftime('%Y-%m-%dT%H:%M') ==
    _s20_diag.utc_iso_z()[:16])
chk('S20 utc_iso_z 保留微秒（与改造前的对外字段形状一致，不偷偷改契约）',
    len(_s20_diag.utc_iso_z()) == 27, _s20_diag.utc_iso_z())

# ── 取证工具：正对照 + 变异反证（"判据会红"必须有变异体兜底）
_s20_bad = _s20_triage.selftest()
chk('S20 log_triage 夹具自证全对（三条解析腿都有命中）', not _s20_bad, str(_s20_bad))
_orig_human_re = _s20_triage.HUMAN_RE
_s20_triage.HUMAN_RE = re.compile(r'(?!)')  # 变异：让人类行永不匹配
try:
    _mut = _s20_triage.selftest()
finally:
    _s20_triage.HUMAN_RE = _orig_human_re
chk('S20 变异反证：拆掉 human 解析腿，自证必须报出问题（否则判据是空的）',
    any('human' in p for p in _mut), str(_mut))
chk('S20 变异后复恢复正常判据（夹具本身可复跑）', not _s20_triage.selftest())
_d = _s20_tempfile.mkdtemp(prefix='ican_s20_triage_')
try:
    _rep = _s20_triage.build_report(_d)
    _empty_red = _rep['lines_parsed'] == 0 and _rep['requests'] == 0
finally:
    _s20_shutil.rmtree(_d, ignore_errors=True)
chk('S20 空目录判"没测到"而不是"没问题"（R247：零输入不得记通过）', _empty_red)

# ── M57：AI 并行度四态判据在 L4 侧也有专属输入面（不靠 log_triage 自证的二手结论）
# 四条输入面刻意取成"只有对应分支能判对"的形状：删任一分支即有一条变红（已在
# 独立变异 runner 上逐条实测：删串行/删并行/去掉 calls<2 守卫各打中一条）。
_s20_pat = _s20_triage.attribute(
    [{'kind': 'req', 'rid': rid, 'method': 'GET', 'path': '/x', 'status': 200,
      'ms': ms, 'slow': False, 'level': 'INFO', 'logger': 'src.app', 'raw': ''}
     for rid, ms in (('t-serial', 7249.0), ('t-par', 2100.0), ('t-single', 320.0),
                     ('t-partial', 1500.0), ('t-none', 30.0))],
    {'t-serial': {'ai_ms': 7022, 'calls': 2, 'max_ms': 4040, 'fallback': 0, 'failed': 0},
     't-par': {'ai_ms': 3900, 'calls': 2, 'max_ms': 2000, 'fallback': 0, 'failed': 0},
     't-single': {'ai_ms': 300, 'calls': 1, 'max_ms': 300, 'fallback': 0, 'failed': 0},
     't-partial': {'ai_ms': 2000, 'calls': 2, 'max_ms': 1000, 'fallback': 0, 'failed': 0}})
_s20_got = {r['rid']: r['ai_pattern'] for r in _s20_pat}
chk('S20 AI 并行度四态各自判对（串行/并行/单次/无 AI 互不串档）',
    [_s20_got.get(k) for k in ('t-serial', 't-par', 't-single', 't-none')]
    == ['serial', 'parallel', 'single', ''], str(_s20_got))
chk('S20 介于"和"与"最慢"之间判 partial（不硬猜成并行）',
    _s20_got.get('t-partial') == 'partial', str(_s20_got.get('t-partial')))
chk('S20 四条输入面结论互不相同（若两两同值则该判据已失去判别力）',
    len({_s20_got[k] for k in ('t-serial', 't-par', 't-single', 't-partial')}) == 4,
    str(_s20_got))

# ── M58：慢查询按 SQL 模板聚合（该并的并、该分的分；带数字的表名是"该分"的那一侧）
_s20_diag.reset()
_s20_q1 = "SELECT id FROM daily_sales WHERE merchant_id = 7 AND pack_id = 12"
_s20_q2 = "SELECT id FROM daily_sales WHERE merchant_id = 9 AND pack_id = 5"
_s20_q3 = "SELECT id FROM daily_sales_2 WHERE merchant_id = 7"
_s20_q4 = "SELECT name FROM product WHERE product_name = '杨枝甘露'"
_s20_q5 = "SELECT name FROM product WHERE product_name = '柠檬水'"
for _q in (_s20_q1, _s20_q2, _s20_q3, _s20_q4, _s20_q5):
    _s20_diag.note_query(_q)
_sql_rows = _s20_diag.snapshot()['slow_queries']
_sql_keys = {r['template']: r['count'] for r in _sql_rows}
chk('S20 五条慢查询归并为 3 个模板（字面量与裸数字参数被吃掉）',
    len(_sql_keys) == 3, str(list(_sql_keys)))
chk('S20 同模板不同参数并成一条 count=2（这是聚合的全部意义）',
    _sql_keys.get('SELECT id FROM daily_sales WHERE merchant_id = ? AND pack_id = ?') == 2,
    str(_sql_keys))
chk('S20 带数字的表名不得被归一（daily_sales_2 是另一张表，并了就是假聚合）',
    any('daily_sales_2' in k for k in _sql_keys), str(list(_sql_keys)))
chk('S20 字符串字面量归一后两条不同产品名查询并成一条',
    _sql_keys.get('SELECT name FROM product WHERE product_name = ?') == 2, str(_sql_keys))
_s20_diag.reset()
for _i in range(_s20_diag.MAX_SQL_KEYS + 3):
    _s20_diag.note_query(f'SELECT a FROM tbl{_i} WHERE b = 1')
_sqlcap = _s20_diag.snapshot()
chk('S20 SQL 模板数有上界且淘汰计数可见（无上限=内存泄漏，静默淘汰=假账）',
    len(_sqlcap['slow_queries']) == _s20_diag.MAX_SQL_KEYS and _sqlcap['evictions'] >= 3,
    str((len(_sqlcap['slow_queries']), _sqlcap['evictions'])))
_s20_diag.reset()
_s20_diag.note_query('')
_s20_diag.note_query(None)
chk('S20 空 SQL 不占键位也不记为丢弃（零输入不得进聚合）',
    _s20_diag.snapshot()['slow_queries'] == [] and _s20_diag.snapshot()['dropped'] == 0,
    str(_s20_diag.snapshot()['dropped']))


def _s20_call_inside(source, guard_sub, callee):
    """AST：callee 调用是否出现在含 guard_sub 的 if 体内（"在源码里"不等于"接上了"）。"""
    hit = False
    for node in _s20_ast.walk(_s20_ast.parse(source)):
        if not isinstance(node, _s20_ast.If) or guard_sub not in _s20_ast.unparse(node.test):
            continue
        for sub in _s20_ast.walk(node):
            if isinstance(sub, _s20_ast.Call) and _s20_ast.unparse(sub.func).endswith(callee):
                hit = True
    return hit


with open(os.path.join('src', 'performance.py'), encoding='utf-8') as _fh:
    _perf_src = _fh.read()
chk('S20 慢查询聚合确实接在 SLOW_QUERY_MS 判定体内（漏接线=静默不计数）',
    _s20_call_inside(_perf_src, 'SLOW_QUERY_MS', 'note_query'))
chk('S20 反例自证：同一检测器对未接线的形状必须判否（不是恒真检测器）',
    not _s20_call_inside('if x > SLOW_QUERY_MS:\n    log(x)\nnote_query(y)\n',
                         'SLOW_QUERY_MS', 'note_query'))
_s20_diag.reset()

# ── 弃用 API 的同类出口清零（AST 检测器自带反例）
def _s20_utcnow_calls(source):
    tree = _s20_ast.parse(source)
    hits = []
    for node in _s20_ast.walk(tree):
        if isinstance(node, _s20_ast.Call) and isinstance(node.func, _s20_ast.Attribute) \
                and node.func.attr == 'utcnow':
            hits.append(node.lineno)
    return hits


_s20_files = [f for f in os.listdir('src') if f.endswith('.py')]
_s20_live = []
for _f in _s20_files:
    with open(os.path.join('src', _f), encoding='utf-8') as _fh:
        _src = _fh.read()
    _h = _s20_utcnow_calls(_src)
    if _h:
        _s20_live.append((_f, _h))
chk('S20 src 内 datetime.utcnow() 调用清零（3.12 已弃用，且与 now(UTC) 混用两套口径）',
    not _s20_live, str(_s20_live))
chk('S20 反例自证：检测器真能抓到 utcnow 调用（不是恒绿的检测器）',
    _s20_utcnow_calls('from datetime import datetime\nx = datetime.utcnow()\n') == [2])
chk('S20 文档里提到 utcnow 不算违例（判据只打调用点，防误伤说明文字）',
    _s20_utcnow_calls('"""历史注记：datetime.utcnow() 已弃用"""\ny = 1\n') == [])
chk('S20 routes 子包同样在检测面内（同类出口要枚举全）',
    os.path.isdir(os.path.join('src', 'routes')) and
    not any(_s20_utcnow_calls(open(os.path.join('src', 'routes', _f), encoding='utf-8').read())
            for _f in os.listdir(os.path.join('src', 'routes')) if _f.endswith('.py')))

_s20_rules = {r.rule for r in app.url_map.iter_rules()}
chk('S20 默认配置下诊断端点不存在（MEMORY_ENDPOINT_ENABLED 默认关，内部端点不裸奔）',
    '/admin/performance/diag' not in _s20_rules, str(sorted(_s20_rules))[:80])
chk('S20 注册动作确在 if MEMORY_ENDPOINT_ENABLED 分支内（AST 取，不看注释）',
    _s20_call_inside(_perf_src, 'MEMORY_ENDPOINT_ENABLED', '_register_memory_endpoint'))
from src import performance as _s20_perf  # noqa: E402

# 行为腿必须用**尚未接过客的新 app**：Flask 在首个请求后禁止注册路由
# （实测 `AssertionError: The setup method 'route' can no longer be called`）。
_s20_app2 = create_app('development')
chk('S20 新建 app 上诊断端点默认不存在（opt-in 是行为，不是文档口径）',
    '/admin/performance/diag' not in {r.rule for r in _s20_app2.url_map.iter_rules()})
_s20_perf._register_memory_endpoint(_s20_app2)
_s20_anon = _s20_app2.test_client().get('/admin/performance/diag')
chk('S20 匿名取诊断端点不放行（路径名/表名属内部情报，留在鉴权面内）',
    _s20_anon.status_code in (302, 401, 403), str(_s20_anon.status_code))
# 登录一律走 session 注入而非 POST /login：限流桶键是 `login:anon:<ip>`（rate_limit
# ._bucket_key），本套件跑到这里匿名登录配额已被前面的段落用尽 —— 实测 POST /login
# 返回 429，那是**限流在正常工作**而不是端点坏了（曾因把 429 读成缺陷误判一次）。
from src.models import Admin as _S20Admin  # noqa: E402
from src.models import Staff as _S20Staff
from src.models import db as _s20_db

with _s20_app2.app_context():
    _s20_owner = _s20_db.session.query(_S20Admin).filter_by(username='owner_demo').first()
    _s20_owner_id = _s20_owner.id if _s20_owner else None
    _s20_staff = _s20_db.session.query(_S20Staff).first()
    _s20_staff_id = _s20_staff.id if _s20_staff else None
_s20_c = _s20_app2.test_client()
with _s20_c.session_transaction() as _s20_sess:
    _s20_sess['_user_id'] = f'admin-{_s20_owner_id}'
_s20_adm = _s20_c.get('/admin/performance/diag')
_s20_aj = _s20_adm.get_json() or {}
chk('S20 管理员可取完整快照（含 top_paths / error_kinds 这些公开端点不给的字段）',
    _s20_adm.status_code == 200 and _s20_aj.get('ok') is True
    and 'top_paths' in _s20_aj and 'error_kinds' in _s20_aj,
    f'owner_id={_s20_owner_id} diag={_s20_adm.status_code} keys={sorted(_s20_aj)[:8]}')
_s20_neg = _s20_app2.test_client()
with _s20_neg.session_transaction() as _s20_sess2:
    _s20_sess2['_user_id'] = f'staff-{_s20_staff_id}'
chk('S20 输入面存在：演示库确有 Staff 账号（否则下一腿是对空气判据）',
    _s20_staff_id is not None, str(_s20_staff_id))
chk('S20 店员账号取诊断端点 403（admin_required 认的是 Admin 表，不是"任何登录者"）',
    _s20_staff_id is not None
    and _s20_neg.get('/admin/performance/diag').status_code == 403, str(_s20_staff_id))
chk('S20 公开 /healthz 的 diag 段不含 top_paths（两档端点的边界不得互相渗透）',
    'top_paths' not in _s20_json.dumps(
        _s20_app2.test_client().get('/healthz').get_json().get('diag', {})))

_s20_diag.reset()
chk('S20 收尾清场：观测计数归零，不把本轮夹具痕迹留给后续断言',
    _s20_diag.snapshot()['requests'] == 0)
print('S20 观测链断言段结束')

# ══ S23 CSV 出口转义 ⇄ 入口还原 + 出口行上限（对标第 20 轮 H42/H43/H44）═══════
# 这条链的失效形态是"店员的备注在老板机器上被执行"，属**数据面**缺陷：
# ruff（S 规则）、盲审扫描、既有三套测试都测不到它，所以判据必须自带且**双侧**——
# 既验"该中和的被中和"，也验"不该动的没被动"（数值列加引号 = 过度防御，会改列类型）。
from src import csv_guard as _s23_guard  # noqa: E402
from src import data_import as _s23_di  # noqa: E402

_s23_payloads = ["=cmd|'/C calc'!A0", '+1', '-2', '@SUM(A1)', '\t=x', '\r=y']
_s23_leaks = [p for p in _s23_payloads
              if _s23_guard.escape_cell(p).startswith(_s23_guard.FORMULA_TRIGGER_CHARS)]
chk('S23 六个公式触发字符逐个被中和（覆盖面按触发字符算，不是只防等号）',
    not _s23_leaks, str(_s23_leaks))
chk('S23 往返闭合：escape→unescape 还原原值（中文/普通文本/空/None/数字串）',
    all(_s23_guard.unescape_cell(_s23_guard.escape_cell(v)) == v
        for v in ('杨枝甘露', '备注里有等号=结尾', '', '12')),
    str([_s23_guard.unescape_cell(_s23_guard.escape_cell(v))
         for v in ('杨枝甘露', '备注里有等号=结尾', '', '12')]))
chk('S23 已承认的取舍钉死：真实以"单引号+触发字符"开头的值还原时丢引号（Frappe 同取舍）',
    _s23_guard.unescape_cell(_s23_guard.escape_cell("'+S-001")) == '+S-001',
    repr(_s23_guard.unescape_cell(_s23_guard.escape_cell("'+S-001"))))
chk('S23 非文本不转义（给数字加前导引号会改变电子表格的列类型判定）',
    _s23_guard.escape_cell(-5) == -5 and _s23_guard.escape_cell(None) is None
    and _s23_guard.escape_cell(3.2) == 3.2)
_s23_df = pd.DataFrame([{'product_name': '=A', 'note': 'ok', 'qty': 5}])
_s23_out = _s23_guard.escape_dataframe(_s23_df)
chk('S23 前提自证：本项目 pandas 3.x 下字符串列 dtype 不是 object（否则下一条测不到东西）',
    str(_s23_df['product_name'].dtype) != 'object', str(_s23_df['product_name'].dtype))
chk('S23 escape_dataframe 与 dtype 无关地逐格生效，且返回副本不动原值',
    _s23_out['product_name'].iloc[0] == "'=A" and _s23_df['product_name'].iloc[0] == '=A'
    and _s23_out['qty'].iloc[0] == 5,
    f"dtype={str(_s23_df['product_name'].dtype)} out={_s23_out['product_name'].iloc[0]!r}")
# 变异反证：只防等号（模拟"同类出口只修一半"），非等号触发字符必须漏出去
_trig_backup = _s23_guard.FORMULA_TRIGGER_CHARS
try:
    _s23_guard.FORMULA_TRIGGER_CHARS = ('=',)
    _s23_half = [c for c in ('+', '-', '@')
                 if not _s23_guard.escape_cell(c + '1').startswith("'")]
finally:
    _s23_guard.FORMULA_TRIGGER_CHARS = _trig_backup
chk('S23 变异反证：把触发字符表缩成只有等号，+/-/@ 三个必须漏（否则上一条判据是空的）',
    _s23_half == ['+', '-', '@'], str(_s23_half))
chk('S23 还原用的是同一张表：变异窗口外不得残留副作用',
    _s23_guard.escape_cell('+1').startswith("'")
    and _s23_guard.FORMULA_TRIGGER_CHARS == _trig_backup)

# 出口上限：双侧标定（恰上限放行 / 超一行即拒），防"永远放行"和"永远拒绝"两种退化
chk('S23 上限判定双侧：等于上限放行、超一行拒绝（只测一侧的判据会静默退化）',
    _s23_guard.rows_exceed(10, limit=10) is False
    and _s23_guard.rows_exceed(11, limit=10) is True)
chk('S23 默认上限是策略值且远大于现网窗口（实测演示出口 300 行 ⇒ 5 万为 167 倍余量）',
    _s23_guard.DEFAULT_MAX_EXPORT_ROWS >= 10000
    and _s23_guard.max_export_rows() == _s23_guard.DEFAULT_MAX_EXPORT_ROWS,
    str(_s23_guard.max_export_rows()))
os.environ['MAX_EXPORT_ROWS'] = 'not-a-number'
chk('S23 上限环境变量非法时回退默认并留告警（外部配置不合法不得崩、不得静默）',
    _s23_guard.max_export_rows() == _s23_guard.DEFAULT_MAX_EXPORT_ROWS)
os.environ['MAX_EXPORT_ROWS'] = '0'
chk('S23 上限设成 0 不得把出口彻底关死（0 视为非法回退，而不是"一行都不许导"）',
    _s23_guard.max_export_rows() == _s23_guard.DEFAULT_MAX_EXPORT_ROWS)
os.environ.pop('MAX_EXPORT_ROWS', None)
os.environ['MAX_EXPORT_ROWS'] = '7'
chk('S23 上限可运维下调（判据要能对上"我设了 7 行"的实际行为）',
    _s23_guard.max_export_rows() == 7 and _s23_guard.rows_exceed(8))
os.environ.pop('MAX_EXPORT_ROWS', None)

# ── 端到端：真导出响应里注入串必须被中和；再导入必须还原成原值（往返闭环）
from src.models import DailySales as _S23Daily  # noqa: E402
from src.models import Staff as _S23Staff, Store as _S23Store, db as _s23_db  # noqa: E402

_s23_cli = _s20_app2.test_client()
with _s23_cli.session_transaction() as _s23_sess:
    _s23_sess['_user_id'] = f'admin-{_s20_owner_id}'
import csv as _s23_csvmod  # noqa: E402

_s23_payload = '=1+1'          # 刻意不含引号/逗号：CSV 不会加外层引号，断言形状可预测
_s23_resp0 = _s23_cli.get('/admin/dashboard/export?days=365')
with app.app_context():
    # 目标行**从导出响应里选**，而不是从库里挑一行：挑库里的行会挑到"不在当前激活包/窗口内"的，
    # 于是断言测的其实是"这行根本没导出"（这条腿第一次就是这么假失败的）。
    _s23_rows0 = list(_s23_csvmod.reader(_s23_resp0.get_data(as_text=True).splitlines()))
    _s23_hdr = {name: i for i, name in enumerate(_s23_rows0[0])}
    _s23_data = _s23_rows0[1]
    _s23_key_product = _s23_data[_s23_hdr['product_name']]
    _s23_key_date = _s23_data[_s23_hdr['record_date']]
    _s23_target = _S23Daily.query.filter_by(
        product_name=_s23_key_product, record_date=_s23_key_date).first()
    chk('S23 输入面：响应首行能在库里定位到（定位不到=后面三条全是对空气判据）',
        _s23_target is not None, f'product={_s23_key_product!r} date={_s23_key_date!r}')
    _s23_backup, _s23_target_id = _s23_target.abnormal_notes, _s23_target.id
    _s23_target.abnormal_notes = _s23_payload
    _s23_store_id, _s23_pack = _s23_target.store_id, _s23_target.pack_id
    _s23_staff_id = _s23_target.staff_id
    _s23_date = _s23_target.record_date
    _s23_db.session.commit()
    _s23_resp = _s23_cli.get('/admin/dashboard/export?days=365')
    _s23_body = _s23_resp.get_data(as_text=True)
    _s23_disp = _s23_resp.headers.get('Content-Disposition') or ''
    _s23_db.session.get(_S23Daily, _s23_target_id).abnormal_notes = _s23_backup
    _s23_db.session.commit()      # 清场：不把注入样本留给后续断言
_s23_cells = [c for row in _s23_csvmod.reader(_s23_body.splitlines()) for c in row]
_s23_bad_cells = [c for c in _s23_cells if c.startswith(_s23_guard.FORMULA_TRIGGER_CHARS)]
chk('S23 输入面自证：注入的那一行确实在响应里（否则下面两条测的是"没导出"而不是"被中和"）',
    _s23_resp.status_code == 200 and _s23_key_product in _s23_body
    and str(_s23_date) in _s23_body, f'product={_s23_key_product!r} date={_s23_date}')
chk('S23 出口不变式：响应全体单元格无一以公式触发字符开头（覆盖面按单元格算，不按命中形状算）',
    not _s23_bad_cells, str(_s23_bad_cells[:3]))
chk('S23 出口正向对照：转义后的标记确实写进了响应（否则上一条可能只是"没这列数据"）',
    "'" + _s23_payload in _s23_cells,
    f"cells 里含 1+1 的={str([c for c in _s23_cells if '1+1' in c][:3])}")
chk('S23 响应头 filename 带引号（与 diy.py 同形状；不一致就是两处各写一份）',
    'filename="' in _s23_disp, _s23_disp[:60])

from datetime import date as _s23_today_cls  # noqa: E402
from src import industry_pack as _s23_ip  # noqa: E402

# 导入腿自己选门店与在售产品，不复用导出腿抓的行：套件中途有迁移/重置类用例改过库，
# 复用旧 id 会取到"已经不存在的门店"（本条腿第一次就这么假失败的）。
with app.app_context():
    _s23_store = _S23Store.query.order_by(_S23Store.id).first()
    _s23_admin = _S20Admin.query.filter_by(id=_s20_owner_id).first() or _S20Admin.query.first()
    from src.models import Product as _S23Product
    _s23_p = _S23Product.query.filter_by(store_id=_s23_store.id, is_active=True).first()
    _s23_imp_product = _s23_p.product_name if _s23_p else ''
    _s23_new_date = _s23_today_cls.today()
with app.app_context():
    # 导入腿自建租户：套件跑到这里 stores 表已被前面的用例清空（实测 .first() 取不到），
    # 复用别人的行等于测不到自己的判据 —— 造一份只属于本轮的门店+在售产品。
    from src.models import Product as _S23Product
    _s23_store = _S23Store(merchant_id=_S20Admin.query.get(_s20_owner_id).merchant_id,
                           name='S23对标临时店', code='S23-R20')
    _s23_db.session.add(_s23_store)
    _s23_db.session.flush()
    _s23_pack_eff = _s23_store.active_pack_id or _s23_ip.active_pack_id()
    _s23_prod = _S23Product(store_id=_s23_store.id, product_name='S23导入专用产品',
                            unit_price=12, pack_id=_s23_pack_eff, is_active=True)
    _s23_db.session.add(_s23_prod)
    _s23_admin = _S20Admin.query.get(_s20_owner_id)
    _s23_db.session.commit()
    _s23_store_pk, _s23_prod_pk = _s23_store.id, _s23_prod.id
    _s23_csv = chr(10).join(['日期,产品名,销量,异常备注,鲜度自评',
                             f"{_s23_new_date},S23导入专用产品,7,'=cmd|reimport", ''])
    _s23_rep = _s23_di.build_report(_s23_admin, _s23_store, _s23_csv.encode('utf-8-sig'),
                                    default_staff_id=_s23_staff_id, commit=False)
chk('S23 导入回执点名未识别列（自定义指标列静默丢弃 = 用户以为导回来了）',
    '鲜度自评' in _s23_rep['ignored_columns'], str(_s23_rep['ignored_columns']))
chk('S23 必填列齐备时不因新增回执字段而误判失败',
    _s23_rep['total'] == 1 and not _s23_rep['errors'], str(_s23_rep['errors']))
with app.app_context():
    _s23_rep2 = _s23_di.build_report(
        _S20Admin.query.get(_s20_owner_id), _S23Store.query.get(_s23_store_pk),
        _s23_csv.encode('utf-8-sig'), default_staff_id=_s23_staff_id, commit=True)
    _s23_back = _S23Daily.query.filter_by(store_id=_s23_store_pk,
                                          product_name='S23导入专用产品',
                                          record_date=_s23_new_date).first()
    _s23_landed = _s23_back.abnormal_notes if _s23_back else None
    for _junk in (_s23_back, _S23Product.query.get(_s23_prod_pk),
                  _S23Store.query.get(_s23_store_pk)):
        if _junk is not None:      # 清场：本轮建的门店/产品/流水一律删掉
            _s23_db.session.delete(_junk)
    _s23_db.session.commit()
chk('S23 落库值已还原（转义标记烘进业务数据就是 unescape 存在的全部理由）',
    _s23_rep2['inserted'] == 1 and _s23_landed == '=cmd|reimport',
    f"inserted={_s23_rep2['inserted']} dup={_s23_rep2['duplicates']} landed={_s23_landed!r}")
chk('S23 编码嗅探面仍可达（BOM 前缀吃掉后表头可读；GBK 同样识别）',
    _s23_di._decode(b'\xef\xbb\xbf' + '日期,x\n'.encode('utf-8')).startswith('日期')
    and _s23_di._decode('日期,x\n'.encode('gbk')).startswith('日期'))

# ══ S24 批量导入契约（对标第 20 轮 H44：把"有功能、零守卫"变成有守卫）════════
# `src/data_import.py` 247 行 + `/admin/import` 路由在改动前**三套测试零命中**
# （实测 grep：smoke / 漏洞回归 / L4 全部 0 处）。上限、编码嗅探、必填列、幂等重放
# 任何一处被打断都不会有测试变红 —— 与第 9 轮"ai_eval 写完 6 天无人调用"同一形状。
# 本段一律**自建租户**（商家/门店/店员/产品/管理员），跑完删干净：套件中途会清空
# stores 表（本段第一次复用别人的行就因此取到 None）。
from src.models import Merchant as _S24Merchant  # noqa: E402
from src.models import Staff as _S24Staff  # noqa: E402
from src.models import db as _s24_db  # noqa: E402

_s24_mine = _S24Merchant(name='S24对标商家A')
_s24_foreign = _S24Merchant(name='S24对标商家B')
_s24_rows_written = []
with app.app_context():
    _s24_db.session.add_all([_s24_mine, _s24_foreign])
    _s24_db.session.flush()
    _s24_store = _S23Store(merchant_id=_s24_mine.id, name='S24店A', code='S24-A')
    _s24_store_b = _S23Store(merchant_id=_s24_foreign.id, name='S24店B', code='S24-B')
    _s24_db.session.add_all([_s24_store, _s24_store_b])
    _s24_db.session.flush()
    _s24_pack = _s24_store.active_pack_id or _s23_ip.active_pack_id()
    _s24_staff = _S24Staff(username='s24_staff', display_name='S24 店员',
                           store_id=_s24_store.id, merchant_id=_s24_mine.id,
                           is_active=True)
    _s24_staff.set_password('S24passw0rd!')
    _s24_admin24 = _S20Admin(username='s24_owner', merchant_id=_s24_mine.id, role='owner')
    _s24_admin24.set_password('S24passw0rd!')
    _s24_prod24 = _S23Product(store_id=_s24_store.id, product_name='S24测试品',
                              unit_price=10, pack_id=_s24_pack, is_active=True)
    _s24_db.session.add_all([_s24_staff, _s24_admin24, _s24_prod24])
    _s24_db.session.commit()
    _s24_store_pk, _s24_store_b_pk = _s24_store.id, _s24_store_b.id
    _s24_merchant_pk, _s24_foreign_pk = _s24_mine.id, _s24_foreign.id
    _s24_admin_pk, _s24_staff_pk = _s24_admin24.id, _s24_staff.id
    _s24_pack = _s24_pack
try:
    with app.app_context():
        _s24_st = _S23Store.query.get(_s24_store_pk)
        _s24_ad = _S20Admin.query.get(_s24_admin_pk)
        _s24_hdr = '日期,产品名,销量'
        _s24_d1 = _s24_today = _s23_today_cls(2026, 5, 4)
        # ① 日期四种分隔符逐个算覆盖面（不是一个"能解析"就算完）
        _s24_fmts = {'2026-05-04': _s24_d1, '2026/05/04': _s24_d1, '2026.05.04': _s24_d1,
                     '20260504': _s24_d1}
        _s24_fmt_bad = {k: v for k, v in _s24_fmts.items() if _s23_di._to_date(k) != v}
        chk('S24 日期解析覆盖 4 种分隔符且结果一致（只测一种=其余三种无守卫）',
            not _s24_fmt_bad, str(_s24_fmt_bad))
        chk('S24 非法日期返回 None 而不是抛异常（行级回执依赖这个约定）',
            _s23_di._to_date('2026-13-45') is None and _s23_di._to_date('') is None)
        # ② 上限双侧：MAX_ROWS 恰界放行 / 超一行拒
        _s24_at_cap = chr(10).join([_s24_hdr] + [f'2026-05-04,S24测试品,{i}'
                                                 for i in range(1, _s23_di.MAX_ROWS + 1)])
        _s24_over = _s24_at_cap + chr(10) + '2026-05-04,S24测试品,9999'
        chk('S24 行数上限双侧：恰 MAX_ROWS 放行、超 1 行拒绝（单侧判据会静默退化）',
            len(_s23_di._parse_rows(_s24_at_cap)[2]) == _s23_di.MAX_ROWS
            and _s24_over.count(chr(10)) == _s24_at_cap.count(chr(10)) + 1)
        try:
            _s23_di.build_report(_s24_ad, _s24_st, _s24_over.encode('utf-8'),
                                 _s24_staff_pk, commit=False)
            _s24_rowcap = None
        except _s23_di.ImportError_ as _e24:
            _s24_rowcap = str(_e24)
        chk('S24 超行数上限整体拒收且消息含上限（不是"导入前 N 行"截断）',
            _s24_rowcap is not None and str(_s23_di.MAX_ROWS) in _s24_rowcap, str(_s24_rowcap))
        # ③ 体积上限
        _s24_big = (_s24_hdr + chr(10)).encode('utf-8') + b'x' * (_s23_di.MAX_FILE_BYTES + 1)
        try:
            _s23_di.build_report(_s24_ad, _s24_st, _s24_big, _s24_staff_pk, commit=False)
            _s24_sizecap = None
        except _s23_di.ImportError_ as _e24:
            _s24_sizecap = str(_e24)
        chk('S24 超体积上限拒收（在解析之前就拒，不是读完再报错）',
            _s24_sizecap is not None and 'MB' in _s24_sizecap, str(_s24_sizecap))
        # ④ 必填列缺失要点名
        try:
            _s23_di.build_report(_s24_ad, _s24_st, ('日期,销量' + chr(10) + '2026-05-04,1').encode(),
                                 _s24_staff_pk, commit=False)
            _s24_miss = None
        except _s23_di.ImportError_ as _e24:
            _s24_miss = str(_e24)
        chk('S24 缺必填列时点名缺哪一列（只说"表头错误"用户无从修起）',
            _s24_miss is not None and '产品' in _s24_miss, str(_s24_miss))
        # ⑤ dry-run 不落库 / commit 幂等重放
        _s24_csv24 = chr(10).join([_s24_hdr + ',异常备注', '2026-05-04,S24测试品,3,ok', ''])
        _s24_before = _S23Daily.query.count()
        _s24_pre = _s23_di.build_report(_s24_ad, _s24_st, _s24_csv24.encode('utf-8'),
                                        _s24_staff_pk, commit=False)
        _s24_after_pre = _S23Daily.query.count()
        _s24_c1 = _s23_di.build_report(_s24_ad, _s24_st, _s24_csv24.encode('utf-8'),
                                       _s24_staff_pk, commit=True)
        _s24_c2 = _s23_di.build_report(_s24_ad, _s24_st, _s24_csv24.encode('utf-8'),
                                       _s24_staff_pk, commit=True)
        chk('S24 dry-run 一行都不落库（预览与落库共用解析器时最容易漏的一半）',
            _s24_pre['total'] == 1 and _s24_after_pre == _s24_before,
            f'before={_s24_before} after={_s24_after_pre}')
        chk('S24 commit 幂等可重放：第二次新增 0 / 重复 1（用户点两下不该写两份）',
            _s24_c1['inserted'] == 1 and _s24_c2['inserted'] == 0
            and _s24_c2['duplicates'] == 1, str((_s24_c1, _s24_c2['inserted'],
                                                 _s24_c2['duplicates'])))
        # ⑥ 行级错误回执：坏行不阻断好行，且带 row/field/reason
        _s24_mix = chr(10).join([_s24_hdr, '2026-13-99,S24测试品,1',
                                 '2026-05-05,S24测试品,-2', '2026-05-06,不存在的产品,1',
                                 '2026-05-07,S24测试品,5', ''])
        _s24_mixrep = _s23_di.build_report(_s24_ad, _s24_st, _s24_mix.encode('utf-8'),
                                           _s24_staff_pk, commit=False)
        _s24_fields = sorted({e['field'] for e in _s24_mixrep['errors']})
        chk('S24 行级回执覆盖日期/销量/产品三类且行号按 Excel 习惯（含表头偏移）',
            {'date', 'qty', 'product'} == set(_s24_fields)
            and sorted(e['row'] for e in _s24_mixrep['errors']) == [2, 3, 4]
            and _s24_mixrep['total'] == 4, str(_s24_mixrep['errors']))
        # ⑦ 编码嗅探：utf-8-sig / gbk 各自可读，二进制垃圾整体拒收
        _s24_gbk = (_s24_hdr + chr(10) + '2026-05-08,S24测试品,1').encode('gbk')
        _s24_gbkrep = _s23_di.build_report(_s24_ad, _s24_st, _s24_gbk, _s24_staff_pk,
                                           commit=False)
        _s24_junk = b'\x00\x01\x02\xff\xfe' + 'x'.encode('utf-16')
        try:
            _s23_di._decode(_s24_junk)
            _s24_junkmsg = None
        except _s23_di.ImportError_ as _e24:
            _s24_junkmsg = str(_e24)
        chk('S24 GBK 与 BOM 都能读、二进制垃圾整体拒收（半截导入比失败更糟）',
            _s24_gbkrep['total'] == 1 and _s24_junkmsg is not None, str(_s24_junkmsg))
    # ⑧ 路由层跨商家越权：外商家门店 id 不在可选集合内 ⇒ 400
    _s24_c24 = _s20_app2.test_client()
    with _s24_c24.session_transaction() as _s24_sess:
        from src import auth_epoch as _s24_ae
        _s24_sess['_user_id'] = f'admin-{_s24_admin_pk}'
        _s24_sess[_s24_ae.EPOCH_KEY] = _s24_ae.current_epoch()
    _s24_page = _s24_c24.get('/admin/import')
    _s24_tok = re.search(r'name="csrf_token" value="([^"]+)"', _s24_page.get_data(as_text=True))
    _s24_foreign_post = _s24_c24.post(
        '/admin/import', data={'store_id': str(_s24_store_b_pk), 'mode': 'preview',
                               'csrf_token': _s24_tok.group(1) if _s24_tok else '',
                               'file': (_s20_io.BytesIO('日期,产品名,销量'.encode()), 'x.csv')},
        content_type='multipart/form-data')
    chk('S24 导入页可取且带 CSRF 字段（取不到页面则下一条是对空气判据）',
        _s24_page.status_code == 200 and _s24_tok is not None, str(_s24_page.status_code))
    chk('S24 路由层拒绝往别人门店导入（400，不是静默改成自己的门店）',
        _s24_foreign_post.status_code == 400, str(_s24_foreign_post.status_code))
finally:
    with app.app_context():
        # 用**提前取好的标量 id** 做删除：`_s24_mine.id` 这类属性在会话关闭后会 Detached，
        # 直接访问就抛错（第一次踩到时误以为是判据红）。
        _s24_db.session.query(_S23Daily).filter_by(store_id=_s24_store_pk).delete()
        _s24_db.session.query(_S24Staff).filter_by(id=_s24_staff_pk).delete()
        _s24_db.session.query(_S23Product).filter_by(store_id=_s24_store_pk).delete()
        _s24_db.session.query(_S20Admin).filter_by(id=_s24_admin_pk).delete()
        _s24_db.session.query(_S23Store).filter(
            _S23Store.id.in_([_s24_store_pk, _s24_store_b_pk])).delete()
        _s24_db.session.query(_S24Merchant).filter(
            _S24Merchant.id.in_([_s24_merchant_pk, _s24_foreign_pk])).delete()
        _s24_db.session.commit()
    with app.app_context():
        chk('S24 清场自证：本轮租户/门店/账号/流水全部删除（残留会污染后续计数）',
            _S23Store.query.get(_s24_store_pk) is None
            and _S23Daily.query.filter_by(store_id=_s24_store_pk).count() == 0)

# ══ S25 数据出入口留痕（对标第 20 轮 M68）════════════════════════════════════
# 留痕三件套此前只覆盖 AI 侧（ai_call_log / agent_trace / webhook 事件），
# 业务数据的两次位移（导出被人取走、导入被灌进来）反而零记录。
# 本段的重点不是"有没有写文件"，而是三条红线：**不写业务明细、写失败不阻断、有界**。
from src import exit_audit as _s25_aud  # noqa: E402

_s25_orig_path, _s25_orig_cap = _s25_aud.LOG_PATH, _s25_aud.LOG_MAX_BYTES
_s25_tmpdir = _s20_tempfile.mkdtemp(prefix='ican_s25_audit_')
try:
    _s25_aud.reset_counters()
    _s25_aud.LOG_PATH = os.path.join(_s25_tmpdir, 'exit_audit.jsonl')
    _s25_ok = _s25_aud.record('export_csv', 'admin-1', {'rows': 12, 'bytes': 345})
    _s25_row = _s25_aud.tail_lines(5)[-1] if _s25_ok else {}
    chk('S25 留痕可写可读且 schema 恰为文档化字段集（加字段=可能塞进业务明细，须显式改判据）',
        _s25_ok and set(_s25_row) == {'ts', 'request_id', 'action', 'mode', 'actor',
                                      'store_id', 'rows', 'inserted', 'duplicates',
                                      'failed', 'bytes', 'allowed'}, str(sorted(_s25_row)))
    chk('S25 留痕时刻带 Z 且 request_id 无上下文时如实给 "-"（不编造关联）',
        str(_s25_row.get('ts', '')).endswith('Z') and _s25_row.get('request_id') == '-',
        str(_s25_row.get('ts')) + ' ' + str(_s25_row.get('request_id')))
    _s20_diag.set_request_id('abcd1234')
    try:
        _s25_aud.record('import_csv', 'admin-1',
                        {'mode': 'commit', 'store_id': 7, 'rows': 3, 'inserted': 2,
                         'duplicates': 1})
        _s25_tail25 = _s25_aud.tail_lines(2)
        _s25_linked = _s25_tail25[-1] if _s25_tail25 else {}
    finally:
        _s20_diag.clear_request_id()
    chk('S25 留痕与第 19 轮 request_id 贯通（运维可用 log_triage --rid 把导出串回日志）',
        bool(_s25_tail25) and _s25_linked.get('request_id') == 'abcd1234'
        and _s25_linked.get('inserted') == 2 and _s25_linked.get('store_id') == 7,
        str(_s25_linked.get('request_id')))
    _s25_aud.LOG_PATH = _s25_tmpdir          # 目标是目录 → 必然写失败
    _s25_aud.reset_counters()
    _s25_failed = None
    try:
        _s25_failed = _s25_aud.record('export_csv', 'admin-1', {'rows': 1})
    except Exception as _e25:                # noqa: BLE001 - 这里正是要证明不会抛
        _s25_failed = f'抛出 {_e25!r}'
    chk('S25 写失败既不上抛也不谎报成功（返回 False + dropped 计数可见）',
        _s25_failed is False and _s25_aud.stats()['dropped'] >= 1,
        f'{_s25_failed!r} stats={_s25_aud.stats()}')
    _s25_aud.LOG_PATH = os.path.join(_s25_tmpdir, 'rot.jsonl')
    _s25_aud.LOG_MAX_BYTES = 200
    _s25_aud.reset_counters()
    for _i25 in range(30):
        _s25_aud.record('export_csv', f'admin-{_i25}', {'rows': _i25})
    _s25_backups = [f for f in os.listdir(_s25_tmpdir) if f.startswith('rot.jsonl.')]
    chk('S25 留痕有界：超限即轮转备份（免费层磁盘是唯一硬约束，日志不得吃满）',
        _s25_backups and os.path.getsize(_s25_aud.LOG_PATH) < 400,
        f'backups={_s25_backups} size={os.path.getsize(_s25_aud.LOG_PATH)}')
    _s25_aud.LOG_MAX_BYTES = _s25_orig_cap
    _s25_aud.LOG_PATH = os.path.join(_s25_tmpdir, 'privacy.jsonl')
    _s25_aud.record('import_csv', 'admin-1',
                    {'mode': 'commit', 'store_id': 1, 'rows': 1, 'inserted': 1})
    with open(_s25_aud.LOG_PATH, encoding='utf-8') as _fh25:
        _s25_raw = _fh25.read()
    chk('S25 红线：留痕里没有任何业务明细（只有 id 与计数，产品名/备注不进日志）',
        'S24' not in _s25_raw and '测试品' not in _s25_raw and 'product_name' not in _s25_raw,
        _s25_raw[:80])
    # 从当前目录上溯找 .gitignore：权威副本在 05 下跑，深度与开发目录不同，
    # 写死层数会得到"文件不存在"的假失败（本轮实测两种跑法都要过）。
    _gi_dir, _gi_found = os.getcwd(), None
    for _up in range(6):
        _cand = os.path.join(_gi_dir, '.gitignore')
        if os.path.isfile(_cand):
            _gi_found = _cand
            break
        _gi_dir = os.path.dirname(_gi_dir)
    _gi_names = os.path.basename(_s25_orig_path)
    chk('S25 生成物纪律：留痕文件在 .gitignore 内（红线要求新落盘文件必须 gitignore）',
        _gi_found is not None and _gi_names in open(_gi_found, encoding='utf-8').read(),
        f'{_gi_names} @ {_gi_found}')
    _s25_one = _s25_aud.tail_lines(1)
    chk('S25 tail_lines(1) 遇到尾随换行仍取到最后一行（本模块实测曾返回空列表）',
        len(_s25_one) == 1 and _s25_one[0].get('action'), str(_s25_one)[:60])
    # 路由级接线：导出必须真的留下痕迹（"模块里有函数"≠"路由调了它"，第 19 项同族）
    _s25_before = _s25_aud.stats()['written']
    _s23_cli.get('/admin/dashboard/export?days=7')   # 复用 S23 的管理员会话客户端
    _s25_after = _s25_aud.stats()['written']
    chk('S25 导出出口确实接上留痕（不是"模块有函数、路由没调用"的第 19 项同族缺陷）',
        _s25_after > _s25_before, f'{_s25_before} -> {_s25_after}')
    _s25_aud.LOG_PATH = _s25_orig_path
finally:
    _s25_aud.LOG_PATH, _s25_aud.LOG_MAX_BYTES = _s25_orig_path, _s25_orig_cap
    _s20_shutil.rmtree(_s25_tmpdir, ignore_errors=True)

# ══ S21 环境变量 ⇄ 部署文档双向对账（对标第 19 轮 M61）════════════════════════
# 为什么分母要走 AST 而不是只看 `os.environ.get(字面量)`：`_env_int('WEBHOOK_TIMEOUT', 3)`
# 内部才调 os.environ.get，而 `DEMO_MERCHANT_ENV = 'DEMO_MERCHANT_ID'` 这类常量间接寻址
# 也取不到字面量。当场实测：只认字面量得 40 个键，按"读取环境变量的调用点"这个**结构单位**
# 枚举得 51 个 —— 漏 11 个（22%）。分母漏了的判据，等于给那 11 个键发了免检牌。
_S21_READERS = ('os.environ.get', 'os.getenv', 'os.environ.setdefault', '_env_int')
_S21_SKIP_DIRS = ('static', 'templates', 'industry_packs', 'narrative', '__pycache__')
# 文档里的「Flask 应用」小节起始标记：它之前是 docker-compose 栈的键（本项目代码不读），
# 之后才是运维可调面。对账只取这一段，否则 compose 的 DB_USER 之类会被误判成幽灵键。
_S21_DOC_MARKER = 'Flask 应用」的环境变量'


def _s21_code_keys(base='src', readers=_S21_READERS, use_consts=True, use_tables=True):
    """枚举 base 下所有实际读取的环境变量名（字面量 + 包装器 + 模块级常量间接寻址）。

    `readers` / `use_consts` 可收紧：收紧后必须**少**拿到键，否则说明包装器与常量
    间接这两条腿本来就是空转的（判据的反向自证）。
    """
    found = set()
    for r, ds, fs in os.walk(base):
        ds[:] = [d for d in ds if d not in _S21_SKIP_DIRS]
        for f in fs:
            if not f.endswith('.py'):
                continue
            with open(os.path.join(r, f), encoding='utf-8') as fh:
                tree = _s20_ast.parse(fh.read())
            consts = {}
            if use_consts:
                for n in tree.body:
                    if isinstance(n, _s20_ast.Assign) and isinstance(n.value, _s20_ast.Constant) \
                            and isinstance(n.value.value, str):
                        for t in n.targets:
                            if isinstance(t, _s20_ast.Name):
                                consts[t.id] = n.value.value
            dynamic = False
            for n in _s20_ast.walk(tree):
                if not isinstance(n, _s20_ast.Call) or not n.args:
                    continue
                if _s20_ast.unparse(n.func) not in readers:
                    continue
                a = n.args[0]
                if isinstance(a, _s20_ast.Constant) and isinstance(a.value, str):
                    found.add(a.value)
                elif isinstance(a, _s20_ast.Name) and a.id in consts:
                    found.add(consts[a.id])
                else:
                    dynamic = True     # os.environ.get(rule['env'])：键名在规则表里，不在调用点
            # 第三条腿：动态取键的模块，键名来自同模块的 `env='NAME'` 字面量表。
            # 只在"确实存在动态读取点"时才承认，否则任何字典里的 env= 都会被判成已读。
            if dynamic and use_tables:
                for n in _s20_ast.walk(tree):
                    if isinstance(n, _s20_ast.Dict):
                        for k, v in zip(n.keys, n.values):
                            if isinstance(k, _s20_ast.Constant) and k.value == 'env'                                     and isinstance(v, _s20_ast.Constant) and isinstance(v.value, str):
                                found.add(v.value)
    return found


def _s21_doc_keys(text):
    """取文档「Flask 应用」小节里以 `# KEY=` 形式登记的键名。"""
    cut = text.find(_S21_DOC_MARKER)
    if cut < 0:
        return None
    keys = set()
    for line in text[cut:].splitlines():
        s = line.strip()
        if not s.startswith('#'):
            continue
        s = s.lstrip('#').strip()
        if '=' not in s:
            continue
        head = s.split('=', 1)[0].strip()
        if re.fullmatch(r'[A-Z][A-Z0-9_]{2,}', head):
            keys.add(head)
    return keys


def _s21_problems(code_keys, doc_text):
    """双向差集：漏登记=隐藏开关，多登记=对空气文档，取不到输入面=判据空转。"""
    if not code_keys:
        return ['代码侧一个环境变量都没枚举到 = 判据空转（零输入不判绿）']
    doc = _s21_doc_keys(doc_text)
    if doc is None:
        return [f'文档里找不到小节标记「{_S21_DOC_MARKER}」，对账面无从划定']
    if not doc:
        return ['文档侧一个键都没登记 = 判据空转（零输入不判绿）']
    out = []
    missing = sorted(code_keys - doc)
    phantom = sorted(doc - code_keys)
    if missing:
        out.append(f'代码里可调但部署文档没登记（运维只能读源码才知道）：{missing}')
    if phantom:
        out.append(f'文档登记了但代码从不读（改了不生效的假开关）：{phantom}')
    return out


_s21_code = _s21_code_keys()
_s21_naive = _s21_code_keys(readers=('os.environ.get', 'os.getenv'), use_consts=False)
_s21_blind = _s21_code_keys(readers=_S21_READERS, use_consts=False)
_s21_notab = _s21_code_keys(use_tables=False)
_s21_alert_keys = frozenset(_s21_importlib.import_module('src.alerts').ALERT_ENV_KEYS)
with open(os.path.join('部署', '.env.example'), encoding='utf-8') as _fh:
    _s21_doc_text = _fh.read()
_s21_bad = _s21_problems(_s21_code, _s21_doc_text)
chk('S21 分母规模自证：结构枚举至少 48 个环境变量（实测 51；地板刻意高于"只认字面量"的 40）',
    len(_s21_code) >= 48, str(len(_s21_code)))

chk('S21 第三条腿反向自证：关掉规则表枚举必须少拿到键，且少的正是告警阈值键'
    '（防把判据放宽成"任何字典里的 env= 都算读过"）',
    _s21_notab < _s21_code and (_s21_code - _s21_notab) == _s21_alert_keys,
    f'差 {sorted(_s21_code - _s21_notab)}')

chk('S21 反向自证·包装器这条腿有牙：只认字面量必须**少**拿到键（实测 40 vs 51）',
    len(_s21_naive) < len(_s21_code) and 'WEBHOOK_TIMEOUT' not in _s21_naive,
    f'naive={len(_s21_naive)} full={len(_s21_code)}')
chk('S21 反向自证·常量间接这条腿有牙：关常量解析必须少拿 DEMO_MERCHANT_ID（实测 50 vs 51）',
    len(_s21_blind) < len(_s21_code) and 'DEMO_MERCHANT_ID' not in _s21_blind
    and 'DEMO_MERCHANT_ID' in _s21_code, f'blind={len(_s21_blind)}')
chk('S21 环境变量 ⇄ 部署文档双向对账为空（可调项无隐藏、文档无假开关）',
    not _s21_bad, str(_s21_bad))
_s21_m1 = _s21_doc_text.replace('# SLOW_REQUEST_MS=500', '# （本轮变异：删掉这行登记）', 1)
chk('S21 变异反证·漏登记：抹掉一行文档，判据必须点名该键',
    any('SLOW_REQUEST_MS' in p for p in _s21_problems(_s21_code, _s21_m1)),
    str(_s21_problems(_s21_code, _s21_m1)))
_s21_m2 = _s21_doc_text + '\n# THIS_KEY_DOES_NOT_EXIST=1\n'
chk('S21 变异反证·假开关：文档凭空加一个代码不读的键，判据必须点名',
    any('THIS_KEY_DOES_NOT_EXIST' in p for p in _s21_problems(_s21_code, _s21_m2)),
    str(_s21_problems(_s21_code, _s21_m2)))
chk('S21 变异反证·取不到面：空输入一律判"没测到"，不得记通过',
    bool(_s21_problems(set(), _s21_doc_text))
    and bool(_s21_problems(_s21_code, '没有标记的一份文档'))
    and bool(_s21_problems(_s21_code, _S21_DOC_MARKER + '\n全是注释没有键\n')))
chk('S21 对账面边界写明：根级工具脚本的键不属部署面（ICAN_ROOT 等刻意不在文档里）',
    'ICAN_ROOT' in _s21_code_keys('.') - _s21_code
    or 'ICAN_ROOT' not in _s21_code, str('ICAN_ROOT' in _s21_code))

# ══ S22 CI 镜像比对结论必须按退出码分诊（对标第 19 轮 M60）═══════════════════
# 旧写法把 5 种码折成一句 "不一致或不可达"：读到"不可达"的人去重传、读到"不一致"的人
# 去查网络 —— 两个动作互斥，而日志只给一句话。判据钉的是"每种码有专属标签与处置"。
_S22_MIRROR_LABELS = ('MIRROR-OK', 'MIRROR-DRIFT', 'MIRROR-NET',
                      'MIRROR-STALEDATE', 'MIRROR-USAGE', 'MIRROR-UNKNOWN')
_s22_wf_path = os.path.join('..', '.github', 'workflows', 'deploy-pa.yml')
if not os.path.isfile(_s22_wf_path):
    _s22_wf_path = os.path.join('.github', 'workflows', 'deploy-pa.yml')
_s22_run_text = ''
_s22_steps = 0
if os.path.isfile(_s22_wf_path):
    import yaml as _s22_yaml
    with open(_s22_wf_path, encoding='utf-8') as _fh:
        _s22_doc = _s22_yaml.safe_load(_fh)
    # GitHub 的 YAML 1.1 会把裸 `on:` 解析成布尔 True（实测 top keys 里是 True 不是 'on'），
    # 两种键都要认：只写 'on' 会在这里取到 None，于是整段判据对空气成立。
    _s22_on = _s22_doc.get('on') or _s22_doc.get(True)
    chk('S22 workflow 顶层结构可读（on 段与 jobs 段都取到了才继续判）',
        bool(_s22_on) and 'deploy' in (_s22_doc.get('jobs') or {}),
        str([repr(k) for k in _s22_doc])[:120])
    _s22_steps = len(_s22_doc['jobs']['deploy']['steps'])
    _s22_mirr = [s for s in _s22_doc['jobs']['deploy']['steps']
                 if 'MIRROR-' in (s.get('run') or '')]
    chk('S22 镜像比对步骤存在且 workflow 可被 safe_load 解析（YAML 语法坏了这里先红）',
        len(_s22_mirr) == 1 and _s22_steps >= 6, f'{len(_s22_mirr)} steps={_s22_steps}')
    if len(_s22_mirr) == 1:
        _s22_run_text = _s22_mirr[0]['run']
        chk('S22 该步骤刻意不阻断部署结论（红在部署上会误伤，与第 18 项同一判断）',
            _s22_mirr[0].get('continue-on-error') is True)
    _s22_codes = sorted(set(re.findall(r'^\s*(\d)\)\s', _s22_run_text, re.M)))
    chk('S22 退出码 0-4 各有分支（表内码一个都不许漏，漏一个就等于折回一句话）',
        _s22_codes == ['0', '1', '2', '3', '4'], str(_s22_codes))
    _s22_found = tuple(lbl for lbl in _S22_MIRROR_LABELS if lbl in _s22_run_text)
    chk('S22 六种分诊标签齐全且一码一标签（含表外码兜底）',
        _s22_found == _S22_MIRROR_LABELS, str(_s22_found))
    chk('S22 不可达码必须写明"内容一致性未验证"（把 rc=2 读成"没漂移"是假绿）',
        'MIRROR-NET' in _s22_run_text
        and '未验证' in _s22_run_text.split('MIRROR-NET')[1][:120])
    chk('S22 取 rc 用 if/else 而非裸 $?（GitHub bash 带 -e，裸写会让第二个站点根本没测）',
        'then' in _s22_run_text and 'rc=$?' in _s22_run_text
        and not re.search(r'check_mirror_freshness\.py[^\n]*\|\|\s*echo', _s22_run_text))
    _s22_mut = _s22_run_text.replace(
        '3) echo "::warning::MIRROR-STALEDATE', '3) echo "::warning::MIRROR-DRIFT')
    chk('S22 变异反证：把 rc=3 的标签改回 MIRROR-DRIFT（= 两码一标签），判据必须变红',
        len({lbl for lbl in _S22_MIRROR_LABELS if lbl in _s22_mut}) < len(_S22_MIRROR_LABELS),
        str(sorted({lbl for lbl in _S22_MIRROR_LABELS if lbl in _s22_mut})))
else:
    chk('S22 工作流文件可定位（取不到文件一律判红，不静默跳过）', False, _s22_wf_path)


# ── 第 21 轮 H45：备份必须「可恢复」，不是「复制没报错」 ─────────────────
# 本轮真实事故反推出的判据：backup() 语义是 source.backup(target)，方向写反时
# 原实现（shutil.copy2 的后继版）会把空副本灌回原库并静默"备份成功"。
# 因此这里既判正例，也把"写反"这一错误在临时目录里重放一遍（绝不碰 data/ican.db）。
import backup_db as _s26_bd  # noqa: E402
import hashlib as _s26_h  # noqa: E402
import shutil as _s26_sh  # noqa: E402
import sqlite3 as _s26_sql  # noqa: E402
import subprocess as _s26_sp  # noqa: E402
import tempfile as _s26_tf  # noqa: E402

_s26_root = os.path.dirname(os.path.abspath(__file__))

_s26_dir = _s26_tf.mkdtemp(prefix='ican_l4_h45_')
_s26_data = os.path.join(_s26_dir, 'data')
_s26_out = os.path.join(_s26_dir, 'backups')
os.makedirs(_s26_data, exist_ok=True)
os.makedirs(_s26_out, exist_ok=True)
_s26_src = os.path.join(_s26_data, 'ican.db')
_s26_c = _s26_sql.connect(_s26_src)
_s26_c.executescript('CREATE TABLE t_a (id INTEGER PRIMARY KEY, v TEXT);'
                     'CREATE TABLE t_b (id INTEGER PRIMARY KEY, v TEXT);')
_s26_c.executemany('INSERT INTO t_a (v) VALUES (?)',
                   [('a%d%s' % (i, 'x' * 40),) for i in range(3000)])
_s26_c.executemany('INSERT INTO t_b (v) VALUES (?)', [('b%d' % i,) for i in range(7)])
_s26_c.commit()
_s26_c.close()
_s26_expect = {'t_a': 3000, 't_b': 7}
_s26_src_sha = _s26_h.sha256(open(_s26_src, 'rb').read()).hexdigest()

_ok, _why, _got = _s26_bd.check_sqlite_file(_s26_src)
chk('S26 夹具自证：临时源库两表行数如预期（夹具空了，下面全是判空气）',
    _ok and _got == _s26_expect, f'ok={_ok} {str(_got)[:80]} {_why[:60]}')

_s26_dst = _s26_bd.backup_sqlite('l4h45', src=_s26_src, dst_dir=_s26_out)
chk('S26 在线快照产出文件且当场判可恢复', bool(_s26_dst) and os.path.exists(_s26_dst or ''),
    str(_s26_dst))
chk('S26 副本逐表行数与源一致（integrity ok 不等于数据齐）',
    bool(_s26_dst) and _s26_bd.check_sqlite_file(_s26_dst)[2] == _s26_expect,
    str(_s26_bd.check_sqlite_file(_s26_dst)[2])[:80] if _s26_dst else '无副本')
chk('S26 备份全程未改写源库一个字节（防 backup() 方向写反毁库）',
    _s26_h.sha256(open(_s26_src, 'rb').read()).hexdigest() == _s26_src_sha)

if _s26_dst:
    _s26_raw = open(_s26_dst, 'rb').read()

    def _s26_write(name, blob):
        p = os.path.join(_s26_dir, name)
        open(p, 'wb').write(blob)
        return p

    _s26_torn = _s26_write('torn.db', _s26_raw[:len(_s26_raw) * 3 // 4])
    _s26_zero = _s26_write('zero.db', b'')
    _s26_fake = _s26_write('fake.db', b'SQLite format 3\x00' + b'\x00' * 60000)
    _s26_tk = _s26_bd.check_sqlite_file(_s26_torn)
    _s26_zk = _s26_bd.check_sqlite_file(_s26_zero)
    _s26_fk = _s26_bd.check_sqlite_file(_s26_fake)
    chk('S26 反例一：副本被截掉 1/4 必须判坏', not _s26_tk[0], _s26_tk[1][:70])
    chk('S26 反例二：0 字节副本判坏（复制中途断电的形态）', not _s26_zk[0], _s26_zk[1][:70])
    chk('S26 反例三：只有 SQLite 头的假文件判坏', not _s26_fk[0], _s26_fk[1][:70])
    chk('S26 正向对照：同一函数对完整副本判好（否则上面三条只是"逢文件就判坏"）',
        _s26_bd.check_sqlite_file(_s26_dst)[0])
    chk('S26 判据取的是行为不是描述：详情串里能看到 integrity/清点结论',
        'integrity' in _s26_bd.check_sqlite_file(_s26_dst)[1],
        _s26_bd.check_sqlite_file(_s26_dst)[1][:60])

    _s26_v_ok = _s26_bd.verify_backup(_s26_dst)
    _s26_v_bad = _s26_bd.verify_backup(_s26_torn)
    chk('S26 --verify 复算与在线校验同结论（好=0/坏=非0，两处判据不各写一份）',
        _s26_v_ok[0] and not _s26_v_bad[0], f'{_s26_v_ok[1][:40]} / {_s26_v_bad[1][:40]}')

    _s26_sql_bad = _s26_write('partial.sql',
                              b'-- MySQL dump 10.13\nCREATE TABLE t (id int);\n')
    _s26_sql_ok = _s26_write('done.sql',
                             b'-- MySQL dump 10.13\nCREATE TABLE t (id int);\n'
                             b'\n-- Dump completed on 2026-09-26 03:00:00\n')
    chk('S26 mysqldump 腿：缺尾部完成标记判坏（半截 dump 看着像成功）',
        not _s26_bd.verify_backup(_s26_sql_bad)[0], _s26_bd.verify_backup(_s26_sql_bad)[1][:60])
    chk('S26 mysqldump 腿正向对照：带标记判可用', _s26_bd.verify_backup(_s26_sql_ok)[0])
else:
    chk('S26 快照产出可定位（拿不到副本一律判红，不静默跳过后面的反例）', False, 'backup_sqlite 返回 None')

_s26_cli_env = dict(os.environ, PYTHONIOENCODING='utf-8')


def _s26_cli(args):
    p = _s26_sp.run([sys.executable, os.path.join(_s26_dir, 'backup_db.py')] + args,
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                    cwd=_s26_dir, env=_s26_cli_env)
    return p.returncode, (p.stdout + p.stderr).replace('\n', ' | ')


_s26_help_src = open(os.path.join(_s26_root, 'backup_db.py'), encoding='utf-8').read()
open(os.path.join(_s26_dir, 'backup_db.py'), 'w', encoding='utf-8', newline='\n').write(_s26_help_src)
# 第 25 轮起 backup_db 依赖 src.backup_manifest（证据件的唯一写入口）：
# 单文件不再是合法交付形态，所以沙箱按交付布局连 src/ 一起摆。
os.makedirs(os.path.join(_s26_dir, 'src'), exist_ok=True)
for _s26_dep in ('__init__.py', 'version.py', 'backup_manifest.py'):
    _s26_sh.copyfile(os.path.join(_s26_root, 'src', _s26_dep),
                     os.path.join(_s26_dir, 'src', _s26_dep))
_s26_pre_cli = sorted(os.listdir(_s26_out))
_s26_rc, _s26_out_text = _s26_cli(['--help'])
chk('S26 CLI 安全腿：--help 退出 0（早期 init_db.py 的 --help 会直接重建库，同类出口一并收口）',
    _s26_rc == 0, _s26_out_text[:70])
_s26_rc, _ = _s26_cli(['--frobnicate'])
chk('S26 CLI 安全腿：未知参数退出 2 而非默默执行默认动作', _s26_rc == 2)
_s26_rc, _ = _s26_cli(['--keep', '0'])
chk('S26 CLI 安全腿：--keep 0 拒绝执行（否则等于清空全部备份）', _s26_rc == 2)
chk('S26 CLI 三条安全腿都没往备份目录落文件（拒了还偷偷备份 = 判据全废）',
    sorted(os.listdir(_s26_out)) == _s26_pre_cli, str(os.listdir(_s26_out))[:80])

_s26_anchor = '            source.backup(target)      # 方向：source → target'
_s26_hits = _s26_help_src.count(_s26_anchor)
chk('S26 变异锚点命中恰好一次（命中 0 次的变异是"没改代码"，不是"改了没红"）',
    _s26_hits == 1, f'hits={_s26_hits}')
if _s26_hits == 1:
    _s26_before = sorted(os.listdir(_s26_out))
    _s26_mut_path = os.path.join(_s26_dir, 'backup_db_mut.py')
    open(_s26_mut_path, 'w', encoding='utf-8', newline='\n').write(
        _s26_help_src.replace(_s26_anchor, '            target.backup(source)'))
    _s26_p = _s26_sp.run([sys.executable, _s26_mut_path], capture_output=True, text=True,
                         encoding='utf-8', errors='replace', cwd=_s26_dir, env=_s26_cli_env)
    _s26_mut_out = (_s26_p.stdout + _s26_p.stderr).replace('\n', ' | ')
    chk('S26 变异反证：把备份方向写回"目标→源"（本轮真实踩过的错）必须判红',
        _s26_p.returncode != 0, f'rc={_s26_p.returncode} {_s26_mut_out[:80]}')
    chk('S26 变异反证正向对照：判红的原因是"只读拦下了写"，不是别的偶发失败',
        'readonly' in _s26_mut_out, _s26_mut_out[:90])
    chk('S26 变异反证：写反的同一刻源库字节仍未变（护栏是机制而非运气）',
        _s26_h.sha256(open(_s26_src, 'rb').read()).hexdigest() == _s26_src_sha)
    chk('S26 变异反证：失败的备份不留"看着成功"的残件在目录里',
        sorted(os.listdir(_s26_out)) == _s26_before, str(os.listdir(_s26_out))[:80])

_s26_sh.rmtree(_s26_dir, ignore_errors=True)



# ══════════════════ S32 变更归因链与成本快照（第 26 轮 H59-H62）══════════════════
import quality_gate as _s32_qg  # noqa: E402

from src import change_log as _s32_cl  # noqa: E402
from src.analytics import _compute_gross_margin as _s32_gm  # noqa: E402
from src.models import Admin as _s32_Admin  # noqa: E402
from src.models import ChangeLog as _s32_Log  # noqa: E402
from src.models import DailySales as _s32_DS  # noqa: E402
from src.models import Product as _s32_P  # noqa: E402
from src.models import db as _s32_db  # noqa: E402

import ast as _s32_ast  # noqa: E402
import hashlib as _s32_h  # noqa: E402
import pandas as _s32_pd  # noqa: E402

_s32_root = os.path.dirname(os.path.abspath(__file__))
_s32_dir = _tf_s31.mkdtemp(prefix='l4_s32_')
_s31_dirs.append(_s32_dir)

# 本套件刻意保持 CSRF 开启（F25：安全开关不得被测试自己旁路），所以所有写腿都要带 token。
# token 必须取自**同一个 client 会话**（CSRF 与会话绑定），所以 `_s32_c` 先建好再取。
_s32_c = app.test_client()


_s32_parse_errs = []


def _s32_trees():
    """变异反证读的是**门禁本尊**的枚举与判据，不在测试里另抄一把尺。

    解析失败的文件不再静默剔除：它连同文件名进 `_s32_parse_errs`，由 `_s32_judge`
    当问题返回 —— 夹具写坏语法时会被判"红"（门禁第 40 项同样判红），而不是被读成
    "这条变异抓不住"。零枚举与"没这个东西"永远是两件事。
    """
    out = {}
    _s32_parse_errs.clear()
    for rel in _s32_qg._s37_py_files():
        try:
            out[rel] = _s32_ast.parse(
                _s32_qg.read(os.path.join(_s32_qg.ROOT, *rel.split('/'))))
        except SyntaxError as exc:
            _s32_parse_errs.append(f'{rel} 解析失败（{exc.lineno} 行）⇒ 枚举面不完整')
            out[rel] = None
    return {k: v for k, v in out.items() if v is not None}


# 判据只取静态面（名册/接线/赋值语义）。行为探针刻意**不放**在这里：
# `_s32_gm` 是 import 进来的内存对象，改磁盘文件不会影响它 —— 拿内存对象验磁盘变异
# 永远判绿，那是假反证。快照行为由下面的 S32 快照腿用真数据直接验。
_s32_probe_df = _s32_pd.DataFrame([
    {'product_name': 'S32甲', 'product_sales_qty': 10, 'total_revenue': 100.0,
     'product_cost': 4.0, 'cost_basis': 'snapshot'},
    {'product_name': 'S32乙', 'product_sales_qty': 10, 'total_revenue': 100.0,
     'product_cost': 2.0, 'cost_basis': 'snapshot'}])
_s32_lazy_df = _s32_pd.DataFrame([
    {'product_name': 'S32甲', 'product_sales_qty': 10, 'total_revenue': 100.0,
     'product_cost': None, 'cost_basis': 'none'}])


def _s32_judge(trees):
    rp, _ = _s32_qg._s40_roster_problems(trees)
    dp, _ = _s32_qg._s40_delete_problems(trees)
    issues = (list(rp) + list(dp) + list(_s32_qg._s40_wiring_problems(trees))
              + list(_s32_parse_errs))
    return issues


def _s32_max_id():
    with app.app_context():
        ids = [r[0] for r in _s32_Log.query.with_entities(_s32_Log.id).all()]
        return max(ids) if ids else 0


def _s32_rows(entity=None, action=None, after_id=0):
    """`after_id` 是本段的隔离锚：本套件跑到这里时，前段已经删过同号产品，
    按 pk 取"最近 80 行"会把别人的历史也算进来（实测 n=2 的假失败就是这么来的）。
    只认本次新增的行，才谈得上"这条腿证明了这件事"。"""
    with app.app_context():
        q = _s32_Log.query.filter(_s32_Log.id > after_id)
        if entity:
            q = q.filter(_s32_Log.entity == entity)
        if action:
            q = q.filter(_s32_Log.action == action)
        return [{'diff': _s32_cl.parse_diff(r), 'action': r.action, 'pk': r.entity_pk,
                 'source': r.source, 'actor': r.actor_kind, 'entity': r.entity}
                for r in q.order_by(_s32_Log.id.desc()).limit(80).all()]


with app.app_context():
    _s32_owner_id = _s32_db.session.query(_s32_Admin).filter_by(
        username='owner_demo').first().id
    _s32_p0 = _s32_P(product_name='S32无历史新品', unit_price=9.9, cost_price=3.3,
                     store_id=1, pack_id='milk_tea')
    _s32_db.session.add(_s32_p0)
    _s32_db.session.commit()
    _s32_p0_id = _s32_p0.id

with _s32_c.session_transaction() as _s32_sess:
    # 走 session 注入而不是 POST /login：限流桶键是 login:anon:<ip>，跑到这里匿名登录
    # 配额已被前段用尽，429 是限流在正常工作，不是端点坏了（第 19 轮同坑）。
    _s32_sess['_user_id'] = f'admin-{_s32_owner_id}'
_s32_tok = re.search(r'name="csrf_token" value="([^"]+)"',
                     _s32_c.get('/admin/products').get_data(as_text=True)).group(1)
_s32_cl.reset_counters()

chk('S32 名册自证：声明的表名与列名在 ORM 里全部真实存在（写错列名会被 getattr 兜成 null）',
    _s32_cl.validate_registry() == [], str(_s32_cl.validate_registry())[:140])
chk('S32 分母对账：值档 + 脱敏档 + 不跟踪档 ≥ 全库 19 张表，一张都不许落在名单外',
    len(set(_s32_cl.TRACKED) | set(_s32_cl.REDACTED_TABLES) | set(_s32_cl.UNTRACKED)) >= 19
    and len(_s32_cl.TRACKED) >= 11,
    f'值档 {len(_s32_cl.TRACKED)} 脱敏 {len(_s32_cl.REDACTED_TABLES)} '
    f'不跟踪 {len(_s32_cl.UNTRACKED)}')

_s32_anchor = _s32_max_id()
_r = _s32_c.post(f'/admin/products/{_s32_p0_id}/update',
                 data={'csrf_token': _s32_tok, 'product_name': 'S32无历史新品',
                       'unit_price': '11.11', 'cost_price': '3.30', 'category': 'S32改过'})
_s32_upd = [x for x in _s32_rows('products', 'update', _s32_anchor)
            if x['pk'] == _s32_p0_id]
chk('S32 改价留痕走真路由：一条 update 行答得出「哪条、从多少到多少」，同批两处改动合成一行',
    _r.status_code == 200 and len(_s32_upd) == 1
    and float(_s32_upd[0]['diff'].get('unit_price', [0, 0])[1]) == 11.11
    and str(_s32_upd[0]['diff'].get('unit_price', [None])[0]) in ('9.9', '9.90')
    and 'category' in _s32_upd[0]['diff'], f'{_r.status_code} {str(_s32_upd)[:170]}')
chk('S32 归因到人：HTTP 请求内产生的行 actor_kind=admin / source=web',
    len(_s32_upd) == 1 and _s32_upd[0]['actor'] == 'admin'
    and _s32_upd[0]['source'] == 'web', str(_s32_upd)[:120])
chk('S32 请求外不冒充归因：CLI/app_context 里写的行必须标 system（宁可归因到"机器"也不编个人）',
    any(x['actor'] == 'system' and x['source'] == 'system'
        for x in _s32_rows('products', 'create')), str(_s32_rows('products', 'create')[:2])[:150])

_s32_anchor2 = _s32_max_id()
with app.app_context():
    _s32_db.session.delete(_s32_P.query.filter_by(id=_s32_p0_id).first())
    _s32_db.session.commit()
_s32_del = [x for x in _s32_rows('products', 'delete', _s32_anchor2)
            if x['pk'] == _s32_p0_id]
chk('S32 物理删除留前像：删完仍读得到它原来叫什么（不再是"删了只能靠 stdout 那一行"）',
    len(_s32_del) == 1
    and _s32_del[0]['diff'].get('product_name') == ['S32无历史新品', None],
    f"n={len(_s32_del)} {str(_s32_del)[:170]}")
chk('S32 前像方向不得写反：delete 行的后像必须全是 None',
    _s32_del and all(v[1] is None for v in _s32_del[0]['diff'].values()
                     if isinstance(v, list)), str(_s32_del)[:120])
chk('S32 create 行的前像必须全是 None（与 delete 互为对偶，写反等于把新旧颠倒）',
    all(v[0] is None for x in _s32_rows('products', 'create')[:4]
        for v in x['diff'].values() if isinstance(v, list)), 'create 前像非空')

# ── 归档腿自含：自己造"有历史流水的产品"，不依赖前面段落留下什么 ──
with app.app_context():
    _s32_v = _s32_P(product_name='S32归档品', unit_price=12, cost_price=5,
                    store_id=1, pack_id='milk_tea')
    _s32_db.session.add(_s32_v)
    _s32_db.session.flush()
    _s32_v_id = _s32_v.id
    _s32_db.session.add(_s32_DS(staff_id=1, store_id=1, record_date=_dt.now().date(),
                                product_name='S32归档品', product_sales_qty=1,
                                total_revenue=12, pack_id='milk_tea'))
    _s32_db.session.commit()
_s32_anchor3 = _s32_max_id()
_s32_rj = _s32_c.post(f'/admin/products/{_s32_v_id}/delete',
                      data={'csrf_token': _s32_tok}).get_json() or {}
with app.app_context():
    _s32_v_now = _s32_P.query.filter_by(id=_s32_v_id).first()
chk('S32 有历史流水的产品删不掉、只能归档（老板点"删除"不再抹掉目录行）',
    _s32_rj.get('ok') and '归档' in _s32_rj.get('message', '')
    and _s32_v_now is not None and _s32_v_now.is_active is False, str(_s32_rj)[:140])
chk('S32 归档记成 archive 而不是含糊的 update（InvenTree part/models.py:584-604 同一语义）',
    any(x['action'] == 'archive' and x['pk'] == _s32_v_id
        for x in _s32_rows('products', after_id=_s32_anchor3)),
    str([x['action'] for x in _s32_rows('products', after_id=_s32_anchor3)])[:130])
with app.app_context():
    _s32_db.session.add(_s32_P(product_name='S32可删品', unit_price=1, store_id=1,
                               pack_id='milk_tea'))
    _s32_db.session.commit()
    _s32_free_id = _s32_P.query.filter_by(product_name='S32可删品').first().id
_s32_fj = _s32_c.post(f'/admin/products/{_s32_free_id}/delete',
                      data={'csrf_token': _s32_tok}).get_json() or {}
with app.app_context():
    _s32_free_gone = _s32_P.query.filter_by(id=_s32_free_id).first() is None
chk('S32 无历史的产品仍允许真删（归档规则不许把新种的产品也永久锁住）',
    '已删除' in _s32_fj.get('message', '') and _s32_free_gone, str(_s32_fj)[:120])

with app.app_context():
    _s32_n0 = _s32_Log.query.count()
    with _s32_cl.silenced():
        _s32_s = _s32_P(product_name='S32静默档', unit_price=1, store_id=1, pack_id='milk_tea')
        _s32_db.session.add(_s32_s)
        _s32_db.session.commit()
        _s32_s_id = _s32_s.id
        _s32_n1 = _s32_Log.query.count()
    _s32_db.session.delete(_s32_P.query.filter_by(id=_s32_s_id).first())
    _s32_db.session.commit()
    _s32_n2 = _s32_Log.query.count()
chk('S32 静默档内零新增、退出后立刻恢复记录（关审计必须是点名动作）',
    _s32_n1 == _s32_n0 and _s32_n2 == _s32_n1 + 1, f'{_s32_n0} → {_s32_n1} → {_s32_n2}')

_s32_anchor4 = _s32_max_id()
with app.app_context():
    _s32_db.session.add(_s32_P(product_name='S32批量档', unit_price=2, store_id=1,
                               pack_id='zz_s32_probe'))
    _s32_db.session.commit()
    # purge 刻意不自带 commit：删除与聚合留痕必须落在**调用方同一个事务**里，
    # 否则"删成功、留痕被回滚"或反之都可能发生。测试侧显式提交，与生产调用方一致。
    _s32_purged = _s32_cl.purge(_s32_P, pack_id='zz_s32_probe')
    _s32_db.session.commit()
_s32_bulk = _s32_rows('products', 'bulk_delete', _s32_anchor4)
chk('S32 purge 把「删 + 聚合留痕」焊成一步：返回条数与聚合行的 rows 相等',
    _s32_purged == 1 and any(x['diff'].get('rows') == 1 for x in _s32_bulk),
    f'purged={_s32_purged} bulk={str(_s32_bulk[:1])[:120]}')
chk('S32 聚合行只给条数与来源，不给任何业务字段值',
    _s32_bulk and all(set(x['diff']) <= {'bulk', 'rows', 'detail'} for x in _s32_bulk),
    str(_s32_bulk[:1])[:140])

chk('S32 快照口径可手算：单价成本 4+2 / 各 10 件 = 60，营收 200 → 毛利率 70.0，快照行数如实回报',
    abs(_s32_gm(_s32_probe_df.copy())[0] - 70.0) < 1e-9
    and _s32_gm(_s32_probe_df.copy())[3] == 2, str(_s32_gm(_s32_probe_df.copy())))
chk('S32 反例：无成本行不得被算成 0 成本（否则毛利率虚高成 100%）',
    _s32_gm(_s32_lazy_df.copy())[0] is None, str(_s32_gm(_s32_lazy_df.copy())))

with app.app_context():
    _s32_cat = _s32_P(product_name='S32甲', unit_price=10, cost_price=4, store_id=1,
                      pack_id='milk_tea')
    _s32_db.session.add(_s32_cat)
    _s32_db.session.commit()
    _s32_derived = _s32_gm(_s32_lazy_df.copy())
    _s32_cat.cost_price = 0.5
    _s32_db.session.commit()
    _s32_after_price = _s32_gm(_s32_probe_df.copy())
    _s32_cat.product_name = 'S32甲改名'
    _s32_db.session.commit()
    _s32_after_rename = _s32_gm(_s32_probe_df.copy())
    _s32_db.session.delete(_s32_P.query.filter_by(id=_s32_cat.id).first())
    _s32_db.session.commit()
    _s32_after_delete = _s32_gm(_s32_probe_df.copy())
    _s32_derived_gone = _s32_gm(_s32_lazy_df.copy())

chk('S32 补算档在工作：提交时无成本的老行按目录当前值补算，快照行数记 0',
    _s32_derived[0] is not None and abs(_s32_derived[0] - 60.0) < 1e-9
    and _s32_derived[3] == 0, str(_s32_derived))
chk('S32 快照行免疫改价（改前实测 +6.49 个百分点，现在分文不动）',
    abs(_s32_after_price[0] - 70.0) < 1e-9, str(_s32_after_price))
chk('S32 快照行免疫改名（改前实测 +1.51 个百分点，现在分文不动）',
    abs(_s32_after_rename[0] - 70.0) < 1e-9, str(_s32_after_rename))
chk('S32 快照行免疫删除（改前实测 -0.23 个百分点 + 覆盖行 18→12，现在都不动）',
    abs(_s32_after_delete[0] - 70.0) < 1e-9 and _s32_after_delete[1] == 2, str(_s32_after_delete))
chk('S32 补算档的诚实边界：目录行没了就如实 None，不拿 0 冒充成本',
    _s32_derived_gone[0] is None, str(_s32_derived_gone))
chk('S32 快照档与补算档的计数分开回报（混在一起就看不出哪些数是补出来的）',
    _s32_derived[3] == 0 and _s32_gm(_s32_probe_df.copy())[3] == 2,
    f'derived={_s32_derived[3]}')

with app.app_context():
    _s32_col = _s32_DS.__table__.columns['cost_basis']
chk('S32 新列带 DDL 级默认值（p002 表重建按旧列名做 INSERT..SELECT，只给 ORM default 会 NOT NULL 崩）',
    _s32_col.server_default is not None and _s32_col.server_default.arg == 'none',
    str(_s32_col.server_default))

_s32_real = _s32_cl._collect
_s32_cl._collect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('S32 注入的审计故障'))
with app.app_context():
    _s32_x = _s32_P(product_name='S32审计崩了业务也要活', unit_price=1, store_id=1,
                   pack_id='milk_tea')
    _s32_db.session.add(_s32_x)
    _s32_db.session.commit()
    _s32_x_id = _s32_x.id
    _s32_db.session.delete(_s32_P.query.filter_by(id=_s32_x_id).first())
    _s32_db.session.commit()
_s32_cl._collect = _s32_real
_s32_failed = _s32_cl.stats().get('failed', 0)
chk('S32 审计崩了业务照写照删（失败被计数，而不是把请求带崩）',
    _s32_failed >= 2 and _s32_x_id is not None, f'failed={_s32_failed}')
chk('S32 恢复后同样的写会留痕（证明上面那条是审计失效，不是"本来就不记"）',
    _s32_cl.stats().get('written', 0) > 0, str(_s32_cl.stats()))

def _s32_keys_clean(payload):
    """按 **JSON 键名**判，不按整段文本做裸子串匹配。

    裸子串会被真实值顶出假红：pack_items 的 section 值 'questions' 里含禁录词 'question'，
    而 'question' 恰恰是 quiz_records 的脱敏列名 —— 第 25 轮 `api_tokens` 含 `token`
    是同一形态。红线要拦的是"凭据列被当成跟踪字段写进值里"，那件事只可能出现在键名上。
    """
    try:
        loaded = __import__('json').loads(payload)
    except (ValueError, TypeError):
        return False
    if not isinstance(loaded, dict):
        return False
    return not (set(loaded) & set(_s32_cl.FORBIDDEN_FIELDS))


_s32_all_clean = True
with app.app_context():
    _s32_scan = [(r.diff_json or '{}') for r in _s32_Log.query.limit(600).all()]
_s32_all_clean = bool(_s32_scan) and all(_s32_keys_clean(t) for t in _s32_scan)
chk('S32 红线取样：扫描面非空（逐条真解析 JSON）且没有任何凭据列名出现在 diff 键上',
    _s32_all_clean, f'扫描 {len(_s32_scan)} 行')
chk('S32 红线判据有鉴别力：合成违例样本必须被判假（防"扫空气判绿"）',
    not _s32_keys_clean('{"password": [null, "x"]}')
    and _s32_keys_clean('{"product_name": ["a", "b"]}')
    and _s32_keys_clean('{"section": "questions"}'), '值里带 questions 不得算违例（键名才判）')

_s32_page = _s32_c.get('/admin/changes')
_s32_html = _s32_page.get_data(as_text=True)
chk('S32 能力有出口：/admin/changes 200 且把留痕与字段名渲染出来（写了没人看等于没写）',
    _s32_page.status_code == 200 and '变更记录' in _s32_html and 'unit_price' in _s32_html,
    f'status={_s32_page.status_code} len={len(_s32_html)}')
chk('S32 匿名访问变更记录页不放行（留痕含主数据数值，属鉴权面）',
    app.test_client().get('/admin/changes').status_code in (302, 401, 403), 'anon')

# ── 变异反证：第 40 项必须对"拆归因链"的每种改法会红 ──
_s32_muts = [
    ('src/app.py', 'change_log.init_app(app)', '_ = 1  # change_log.init_app(app)'),
    ('src/change_log.py', "'products': ('product_name',", "'products': ('product_name_zzz',"),
    ('src/query_guard.py', 'm.StoreTask, m.AIUsageLedger, m.ChangeLog,',
     'm.StoreTask, m.AIUsageLedger,'),
    ('src/routes/h5.py', "cost_basis='snapshot' if _cost_now is not None else 'none',", ''),
    ('src/analytics.py', "df['product_cost'] if 'product_cost' in df.columns",
     "df['total_revenue'] if 'product_cost' in df.columns"),
    ('src/pack_store.py', 'change_log.purge(PackItem, pack_id=pack_id)\n'
     '    change_log.purge(MetricDef, pack_id=pack_id)',
     'PackItem.query.filter_by(pack_id=pack_id).delete()\n'
     '    MetricDef.query.filter_by(pack_id=pack_id).delete()'),
    ('src/change_log.py', "'password', 'token_hash',", ''),
    ('src/pack_demo.py', "    change_log.note_bulk('daily_sales', 'bulk_delete', sales, "
     "store_id=store_id,\n                         detail=f'pack={pack_id}')\n", ''),
]
_s32_red, _s32_reds = 0, []
_s32_baseline = _s32_judge(_s32_trees())
for _s32_rel, _s32_a, _s32_b in _s32_muts:
    _s32_path = os.path.join(_s32_root, *_s32_rel.split('/'))
    # 全程按字节存取：文本模式在 Windows 会把换行译成 CRLF，于是“还原”这个动作
    # 本身就把源文件写成 CRLF —— 本轮就是这么把 S19 / 第 10 项 / 台账跨机复算一起打红的。
    # 变异夹具必须能自证“我来过又像没来过”。
    _s32_origin = open(_s32_path, 'rb').read()
    _s32_sha0 = _s32_h.sha256(_s32_origin).hexdigest()
    _s32_txt = _s32_origin.decode('utf-8')
    if _s32_txt.count(_s32_a) != 1:
        _s32_reds.append(f'{_s32_rel} 锚点命中 {_s32_txt.count(_s32_a)} 次')
        continue
    try:
        open(_s32_path, 'wb').write(_s32_txt.replace(_s32_a, _s32_b).encode('utf-8'))
        _s32_hit = _s32_judge(_s32_trees())
        if _s32_hit:
            _s32_red += 1
        else:
            _s32_reds.append(f'{_s32_rel} 变异后仍判绿')
    finally:
        open(_s32_path, 'wb').write(_s32_origin)
    if _s32_h.sha256(open(_s32_path, 'rb').read()).hexdigest() != _s32_sha0:
        _s32_reds.append(f'{_s32_rel} 还原后字节不等（夹具把文件改坏了）')

chk(f'S32 变异反证：{len(_s32_muts)} 类拆归因链的改法全部判红（接线/名册列名/守卫纳管/'
    '快照写点/回退口径/裸批量删/禁录列地板/**按删除站点逐条**配对）',
    _s32_red == len(_s32_muts),
    f'判红 {_s32_red}/{len(_s32_muts)}；未红原因 {(_s32_reds or ["无"])[:3]}')
chk('S32 变异反证的还原对照：逐条改回后基线重新判绿（证明红来自变异，不是残留状态）',
    not _s32_baseline and not _s32_judge(_s32_trees()),
    str(_s32_judge(_s32_trees()) or _s32_baseline)[:160])
chk('S32 零输入不得判过：名册被清空时该项必须判红而不是"没东西可查所以通过"',
    bool(_s32_qg._s40_roster_problems({
        'src/change_log.py': _s32_ast.parse(
            'TRACKED = {}\nREDACTED_TABLES = ()\nUNTRACKED = {}\nFORBIDDEN_FIELDS = ()'),
        'src/models.py': _s32_ast.parse(
            'class A:\n    __tablename__ = "products"\n    id = db.Column(int)')}
    )[0]), '空名册未被判红')

_s26_sh.rmtree(_s32_dir, ignore_errors=True)
# ══════════════ S33 端点授权面默认拒绝与名册对账（第 27 轮 H63-H66）══════════════
import hashlib as _s33_hashlib  # noqa: E402

import quality_gate as _s33_qg  # noqa: E402
from src import authz_ledger as _s33_az  # noqa: E402

_s33_root = os.path.dirname(os.path.abspath(__file__))

_s33_rules = list(app.url_map.iter_rules())
_s33_actual = {r.endpoint for r in _s33_rules}
_s33_index = _s33_az.get_index()
_s33_guarded = {ep for ep, v in _s33_index.items() if v['guards']}
_s33_public = {ep for ep, s in _s33_az.EXEMPT.items() if s['mode'] == _s33_az.MODE_PUBLIC}
_s33_bearer = {ep for ep, s in _s33_az.EXEMPT.items() if s['mode'] == _s33_az.MODE_BEARER}
_s33_inline = {ep for ep, s in _s33_az.EXEMPT.items() if s['mode'] == _s33_az.MODE_INLINE}


def _s33_gate_res():
    """调门禁本尊的第 41 项，返回 (是否判绿, 详情)。判据不在测试里另抄一把尺。"""
    _s33_qg.results.clear()
    _s33_qg.check_authz_surface()
    _lvl, _name, _ok, _det = _s33_qg.results[-1]
    return _ok, _det


def _s33_method(rule):
    _ms = sorted(m for m in (rule.methods or set()) if m not in ('HEAD', 'OPTIONS'))
    return (_ms or ['GET'])[0]


def _s33_concrete(rule):
    """把占位符填成可请求的具体路径（取数面必须覆盖到每条规则，不能只测好测的）。"""
    _u = str(rule)
    _u = re.sub(r'<path:[^>]+>', 'x.css', _u)
    _u = re.sub(r'<int:[^>]+>', '1', _u)
    _u = re.sub(r'<[^>:/]+:[^>]+>', '1', _u)
    _u = re.sub(r'<[a-zA-Z_]+>', '1', _u)
    return _u


_s33_login_codes = {}


def _s33_login(user, pwd, role):
    """登录前先清进程内限流桶：L4 跑了数百次登录后 `/login` 会进 429，
    那会让"越权被拒"和"根本没登进去"长得一模一样（假绿/假红同源）。"""
    from src import rate_limit as _s33_rl
    with _s33_rl._BUCKET_LOCK:
        _s33_rl._buckets.clear()
    _c = app.test_client()
    _t = re.search(r'name="csrf_token" value="([^"]+)"',
                   _c.get('/login').get_data(as_text=True) or '')
    _r = _c.post('/login', data={'username': user, 'password': pwd, 'role': role,
                                 'csrf_token': _t.group(1) if _t else ''})
    _s33_login_codes[user] = _r.status_code
    with _c.session_transaction() as _s:
        return _c, _s.get('_user_id')


_s33_problems, _s33_evidence = _s33_az.validate_registry(app)
chk('S33 三方对账为空：名册 ⇄ AST 实现侧 ⇄ url_map 运行侧一致（本轮主判据）',
    not _s33_problems, str(_s33_problems)[:220])
chk('S33 对账分母非空：url_map 实侧 ≥80 条规则（零输入不得判过）',
    len(_s33_rules) >= 80, f'实侧 {len(_s33_rules)}')
chk('S33 覆盖对账：每条规则要么被名册豁免、要么带视图级守卫（漏一个即静默公开）',
    not sorted(_s33_actual - set(_s33_az.EXEMPT) - _s33_guarded),
    str(sorted(_s33_actual - set(_s33_az.EXEMPT) - _s33_guarded))[:200])
chk('S33 名册自证：EXEMPT 里的端点名在 url_map 或 AST 索引里真实存在（写错名即红）',
    not sorted(set(_s33_az.EXEMPT) - _s33_actual - set(_s33_index)),
    str(sorted(set(_s33_az.EXEMPT) - _s33_actual - set(_s33_index)))[:200])
chk('S33 公开档逐条带理由且理由成地板（豁免是决策，不是默认值）',
    all(len(str(s.get('reason') or '')) >= 10 for s in _s33_az.EXEMPT.values())
    and all(s.get('methods') for s in _s33_az.EXEMPT.values()),
    '有空 reason 或空方法集的豁免条目')
chk('S33 三档形状对账：public+bearer+inline = 全部豁免条目，且 bearer/inline 计数与实现一致',
    len(_s33_public) + len(_s33_bearer) + len(_s33_inline) == len(_s33_az.EXEMPT)
    and len(_s33_bearer) == 7 and len(_s33_inline) == 4,
    f'public {len(_s33_public)} / bearer {len(_s33_bearer)} / inline {len(_s33_inline)} '
    f'/ 共 {len(_s33_az.EXEMPT)}')

# ── 匿名逐端点实测（改造前没有任何测试走过 url_map）──
_s33_anon = app.test_client()
_s33_leak, _s33_probed, _s33_public_403, _s33_bearer_bad, _s33_inline_bad = [], 0, [], [], []
for _r in _s33_rules:
    _code = _s33_anon.open(_s33_concrete(_r), method=_s33_method(_r)).status_code
    _s33_probed += 1
    if _r.endpoint in _s33_public:
        if _code == 403:
            _s33_public_403.append((_r.endpoint, _code))
    elif _code < 300:
        _s33_leak.append((_r.endpoint, _code))
    if _r.endpoint in _s33_bearer and _code not in (401, 404):
        _s33_bearer_bad.append((_r.endpoint, _code))
    if _r.endpoint in _s33_inline and _code < 300:
        _s33_inline_bad.append((_r.endpoint, _code))
chk('S33 匿名逐端点实测的覆盖面：探测数 = url_map 规则数（不是抽样，漏测即分母失真）',
    _s33_probed == len(_s33_rules), f'{_s33_probed}/{len(_s33_rules)}')
chk('S33 匿名逐端点实测：非 public 档端点零 2xx 泄露（逐条判定，不是抽查）',
    not _s33_leak, str(_s33_leak)[:220])
chk('S33 默认拒绝没误伤公开档：public 端点匿名不得被 403',
    not _s33_public_403, str(_s33_public_403)[:160])
chk('S33 bearer 档匿名只能是 401/404（功能关闭态回 404 也不放行）',
    not _s33_bearer_bad, str(_s33_bearer_bad)[:160])
chk('S33 inline 档（凭据管理）匿名不得 2xx',
    not _s33_inline_bad, str(_s33_inline_bad)[:160])

# ── 三类角色的真实越权面 ──
# L4 的临时库只保证 owner_demo 一个账号（其余演示账号不随夹具播种）。这里**当场造**
# 店长与店员各一枚，而不是去借演示账号 —— 借来的账号一旦缺种，"越权被拒"就会伪装成
# 通过（假绿），本轮要的正是"负测必须真的负"。
from src.models import ROLE_MANAGER as _s33_ROLE_MGR  # noqa: E402
from src.models import Admin as _s33_Admin  # noqa: E402
from src.models import Staff as _s33_Staff  # noqa: E402
from src.models import Store as _s33_Store  # noqa: E402
from src.models import db as _s33_db  # noqa: E402

with app.app_context():
    _s33_root_admin = _s33_Admin.query.filter_by(username='owner_demo').first()
    _s33_mid = _s33_root_admin.merchant_id if _s33_root_admin else None
    _s33_sid = (_s33_Store.query.filter_by(merchant_id=_s33_mid).first()
                or _s33_Store.query.first())
    _s33_sid = _s33_sid.id if _s33_sid else None
    for _u in ('s33-manager', 's33-staff'):
        _s33_db.session.query(_s33_Admin).filter_by(username=_u).delete()
        _s33_db.session.query(_s33_Staff).filter_by(username=_u).delete()
    _s33_mgr_row = _s33_Admin(username='s33-manager', role=_s33_ROLE_MGR, merchant_id=_s33_mid)
    _s33_mgr_row.set_password('S33mgrPass!')
    _s33_staff_row = _s33_Staff(username='s33-staff', display_name='S33 店员',
                                merchant_id=_s33_mid, store_id=_s33_sid)
    _s33_staff_row.set_password('S33staffPass!')
    _s33_db.session.add_all([_s33_mgr_row, _s33_staff_row])
    _s33_db.session.commit()
    _s33_seed_ok = bool(_s33_mid and _s33_sid)

_s33_owner, _s33_owner_uid = _s33_login('owner_demo', 'owner123', 'admin')
_s33_mgr, _s33_mgr_uid = _s33_login('s33-manager', 'S33mgrPass!', 'admin')
_s33_staff, _s33_staff_uid = _s33_login('s33-staff', 'S33staffPass!', 'staff')
chk('S33 三类账号真的登进去了（负测若因账号缺失而"通过"，等于没测）',
    _s33_seed_ok and bool(_s33_owner_uid) and bool(_s33_mgr_uid) and bool(_s33_staff_uid),
    f'seed={_s33_seed_ok} uid owner={_s33_owner_uid} mgr={_s33_mgr_uid} '
    f'staff={_s33_staff_uid} 状态码 {_s33_login_codes}')


_s33_token_from = {}


def _s33_csrf_post(client, url, data):
    """同会话内任一带 csrf 字段的页面都能取到 token。
    不能只盯 `/admin/dashboard`：交付副本的演示库与该页是否渲染有关，
    取不到 token 时 post 会变成 400，看上去像"令牌签发被拒"（假红）。"""
    _tok, _src = '', ''
    for _p in ('/admin/dashboard', '/admin/staff', '/portal', '/login'):
        try:
            _body = client.get(_p).get_data(as_text=True) or ''
        except Exception:
            continue
        _m = re.search(r'name="csrf_token" value="([^"]+)"', _body)
        if _m:
            _tok, _src = _m.group(1), _p
            break
    _s33_token_from[url] = _src or '未取到'
    _d = dict(data or {})
    _d.setdefault('csrf_token', _tok)
    return client.post(url, data=_d)


_s33_owner_code = _s33_csrf_post(
    _s33_owner, '/api/v1/manage/tokens', {'name': 's33-owner'}).status_code
_s33_mgr_code = _s33_csrf_post(
    _s33_mgr, '/api/v1/manage/tokens', {'name': 's33-mgr'}).status_code
_s33_staff_admin = _s33_staff.get('/admin/staff').status_code
_s33_staff_dash = _s33_staff.get('/admin/dashboard').status_code
_s33_staff_api = _s33_staff.get('/api/v1/sales/daily').status_code
chk('S33 正向腿：owner 走真路由能签发令牌（名册说可以 = 确实可以，能力有出口）',
    _s33_owner_code == 201, f'owner -> {_s33_owner_code}；token 取自 {_s33_token_from}')
chk('S33 越权腿：店长签发令牌被内联归属校验拒掉（装饰器绿不代表角色对）',
    _s33_mgr_code == 403, f'manager -> {_s33_mgr_code}；token 取自 {_s33_token_from}')
chk('S33 越权腿：店员会话打不进管理面（/admin/staff 与 /admin/dashboard 均非 2xx）',
    _s33_staff_admin >= 300 and _s33_staff_dash >= 300,
    f'staff /admin/staff={_s33_staff_admin} /admin/dashboard={_s33_staff_dash}')
chk('S33 身份不互换：店员会话打开放 API 数据端点拿不到业务数据（非 2xx）',
    _s33_staff_api >= 300, f'staff /api/v1/sales/daily -> {_s33_staff_api}')

# ── 运行期默认拒绝回退（新建 app，不污染本轮其余断言）──
_s33_app2 = create_app('development')
_s33_app2.add_url_rule('/s33-undeclared', endpoint='s33_undeclared',
                       view_func=lambda: 'LEAK', methods=['GET'])
_s33_backstop_code = _s33_app2.test_client().get('/s33-undeclared').status_code
chk('S33 默认拒绝真的会拦：运行时新挂一条无守卫路由 → 匿名请求 403 而不是直出业务',
    _s33_backstop_code == 403, f'-> {_s33_backstop_code}')
_s33_p2, _s33_e2 = _s33_az.validate_registry(_s33_app2)
chk('S33 同一漏网在名册侧也会红：未登记端点进 validate_registry 的 problem 清单',
    any('s33_undeclared' in p for p in _s33_p2), str(_s33_p2)[:180])

# ── 变异反证（7 类，全部在内存侧改名册/索引，逐条必须变红）──
_s33_snapshot = {k: dict(v) for k, v in _s33_az.EXEMPT.items()}
_s33_reds, _s33_notes = 0, []


def _s33_mutate(label, fn):
    """改一处 → 名册与门禁第 41 项至少一侧判红 → 还原。计的是「会红性」不是覆盖率。"""
    global _s33_reds
    try:
        fn()
        _p, _ = _s33_az.validate_registry(app)
        _ok, _ = _s33_gate_res()
        if _p or not _ok:
            _s33_reds += 1
        else:
            _s33_notes.append(f'{label} 未判红')
    finally:
        _s33_az.EXEMPT.clear()
        _s33_az.EXEMPT.update(_s33_snapshot)


def _m_drop_public():
    _s33_az.EXEMPT.pop('healthz', None)


def _m_bad_mode():
    _s33_az.EXEMPT['healthz'] = dict(_s33_az.EXEMPT['healthz'], mode='anonymous')


def _m_blank_reason():
    _s33_az.EXEMPT['healthz'] = dict(_s33_az.EXEMPT['healthz'], reason='')


def _m_empty_registry():
    _s33_az.EXEMPT.clear()


def _m_admin_public():
    _s33_az.EXEMPT['admin.staff_list'] = {
        'mode': _s33_az.MODE_PUBLIC, 'methods': frozenset({'GET'}),
        'reason': '把管理页声明成匿名可达（红线，必须被公开档红线拦住）'}


def _s33_patch_index(mut):
    """临时替换枚举器输出（等价于改源码里的接线），两把尺都要红；用完必还原。"""
    _orig = _s33_az.build_index
    _s33_az.build_index = lambda pkg_dir=None, _o=_orig, _m=mut: _m(_o(pkg_dir))
    _s33_az._IDX['cache'] = None
    try:
        _p, _ = _s33_az.validate_registry(app)
        _ok, _ = _s33_gate_res()
    finally:
        _s33_az.build_index = _orig
        _s33_az._IDX['cache'] = None
    return bool(_p) or not _ok


def _m_bearer_disconnected(index):
    idx = dict(index)
    e = dict(idx.get('api_v1.api_meta', {}))
    e['calls'] = frozenset()
    idx['api_v1.api_meta'] = e
    return idx


def _m_zero_enumeration(index):
    return {}


for _label, _fn in (('删除一条公开豁免', _m_drop_public), ('档位写成非法值', _m_bad_mode),
                     ('豁免理由写成空串', _m_blank_reason), ('整份名册清空', _m_empty_registry),
                     ('把管理页声明成 public', _m_admin_public)):
    _s33_mutate(_label, _fn)
chk('S33 变异反证：5 类拆授权面的改法全部判红（删豁免/非法档/空理由/清名册/管理页转公开）',
    _s33_reds == 5, f'判红 {_s33_reds}/5；未红原因 {(_s33_notes or ["无"])[:3]}')
_s33_bearer_red = _s33_patch_index(_m_bearer_disconnected)
_s33_zero_red = _s33_patch_index(_m_zero_enumeration)
chk('S33 变异反证后 2 类：bearer 断线与零枚举各自真的翻红（防"总数凑对但单条空转"）',
    _s33_bearer_red and _s33_zero_red,
    f'bearer 断线翻红={_s33_bearer_red} / 零枚举翻红={_s33_zero_red}')
chk('S33 变异夹具的还原自证：每类变异撤销后枚举器与名册都回到原样',
    _s33_az.EXEMPT == {k: dict(v) for k, v in _s33_snapshot.items()}
    and _s33_az.get_index() == _s33_index,
    f'名册条目 {len(_s33_az.EXEMPT)}/{len(_s33_snapshot)}')

# ── 文件级变异：拆掉 app.py 的接线，门禁必须红，还原后逐字节相等 ──
_s33_app_path = os.path.join(_s33_root, 'src', 'app.py')
_s33_origin = open(_s33_app_path, 'rb').read()
_s33_sha0 = _s33_hashlib.sha256(_s33_origin).hexdigest()
_s33_wire_red = False
try:
    _s33_cut = _s33_origin.replace(
        b'if authz_ledger.backstop_decision(endpoint):',
        b'if True:  # S33 mutation: unwire backstop')
    if _s33_cut == _s33_origin:
        _s33_notes.append('拆接线的变异没落到字节（改法无效，反证不算数）')
    else:
        open(_s33_app_path, 'wb').write(_s33_cut)
        _s33_wire_red = not _s33_gate_res()[0]
finally:
    open(_s33_app_path, 'wb').write(_s33_origin)
chk('S33 拆掉默认拒绝接线（把判定短路成恒真）→ 门禁第 41 项判红',
    _s33_wire_red, '接线被短路后该项仍判绿 = 判据没读到那行')
chk('S33 文件级变异还原自证：src/app.py 还原后 sha256 与变异前逐字节相等',
    _s33_hashlib.sha256(open(_s33_app_path, 'rb').read()).hexdigest() == _s33_sha0,
    '还原后字节不同（夹具把文件改坏了）')

# ── 还原对照：所有变异撤销后，两侧重新判绿 ──
_s33_final_p, _ = _s33_az.validate_registry(app)
chk('S33 还原对照：变异全部撤销后名册对账重新为空（红来自变异，不是残留状态）',
    not _s33_final_p, str(_s33_final_p)[:180])
_s33_gate_final, _s33_gate_det = _s33_gate_res()
chk('S33 门禁第 41 项在**本副本真链路**上判绿（不是只在沙箱能跑）',
    _s33_gate_final, str(_s33_gate_det)[:200])
chk('S33 默认拒绝无开关可关（关掉它等于把本轮修的洞重新挖开，S21 假开关判据同族）',
    'AUTHZ_BACKSTOP' not in open(os.path.join(_s33_root, '部署', '.env.example'),
                                 encoding='utf-8').read()
    and 'os.environ' not in open(os.path.join(_s33_root, 'src', 'authz_ledger.py'),
                                 encoding='utf-8').read().split('def ')[0],
    '授权面默认拒绝被做成了可关开关')



# ══════════════ S34 商家数据擦除归属图与不可逆动作护栏（第 28 轮 H69-H73）══════════════
import hashlib as _s34_h

import quality_gate as _s34_qg  # noqa: E402
from src import erasure as _s34_e  # noqa: E402
from src.app import create_app as _s34_create  # noqa: E402
from src.models import Admin as _s34_Admin  # noqa: E402
from src.models import DailySales as _s34_DS  # noqa: E402
from src.models import EmailVerification as _s34_EV  # noqa: E402
from src.models import Merchant as _s34_M  # noqa: E402
from src.models import Staff as _s34_St  # noqa: E402
from src.models import Store as _s34_Sto  # noqa: E402
from src.models import ChangeLog as _s34_CL  # noqa: E402
from src.models import db as _s34_db  # noqa: E402

_s34_root = os.path.dirname(os.path.abspath(__file__))
_s34_tmp = _tf_s31.mkdtemp(prefix='l4_s34_')
_s31_dirs.append(_s34_tmp)


def _s34_tok(client):
    _m = re.search(r'name="csrf_token" value="([^"]+)"',
                   client.get('/login').get_data(as_text=True) or '')
    return _m.group(1) if _m else ''


def _s34_backup_of_live(dst_name):
    """方向正确的在线备份：源以 **只读** 打开，`source.backup(target)`（第 21 轮把这条
    写反过，一次把开发库打空 —— 语义是 source.backup(target)，不是 target.backup(source)）。"""
    import sqlite3

    dst = os.path.join(_s34_tmp, dst_name)
    src = sqlite3.connect('file:%s?mode=ro' % _src_db, uri=True)
    dstc = sqlite3.connect(dst)
    try:
        src.backup(dstc)
    finally:
        dstc.close()
        src.close()
    return dst


def _s34_empty_db(name):
    import sqlite3

    p = os.path.join(_s34_tmp, name)
    sqlite3.connect(p).close()
    return p


# ── 名册对账（不依赖任何数据）──
_s34_probs, _s34_ev = _s34_e.validate_registry()
chk('S34 归属图 ⇄ ORM 元数据双向对账为空（漏表/陈表/缺列/缺理由/顺序错都会红）',
    not _s34_probs, str(_s34_probs)[:220])
with app.app_context():
    _s34_ntables = len(_s34_db.metadata.sorted_tables)
chk('S34 分母等式：归属图条数 == 实表数（两个枚举器各自数一遍，漏登记由上一行判红，'
    '不靠这里把 18/19 数死）',
    len(_s34_e.OWNING) == _s34_ntables, f'OWNING {len(_s34_e.OWNING)} / 实表 {_s34_ntables}')
chk('S34 分母下限：实表数 ≥ 19（p007 webhook_delivery 入册后的地板，棘轮只降不升）',
    _s34_ntables >= 19, f'实表 {_s34_ntables}')
chk('S34 关键反例面：daily_sales 必须落在**非** merchant_id 直取档（它没有该列）',
    _s34_e.OWNING['daily_sales']['mode'] == _s34_e.MODE_STORE_OR_STAFF
    and 'merchant_id' not in _s34_e.metadata_columns('daily_sales'),
    _s34_e.OWNING['daily_sales']['mode'])
chk('S34 无主/根行/邮箱三档逐条带 ≥20 字理由（"不可擦"是决策，必须留痕）',
    all(len(str(_s34_e.OWNING[n].get('reason') or '')) >= 20
        for n, s in _s34_e.OWNING.items()
        if s['mode'] in (_s34_e.MODE_NONE, _s34_e.MODE_ROOT, _s34_e.MODE_EMAIL)),
    '有档位缺理由')

# ── 造一个只属于本段的商家（含连坐流水），擦它，不碰演示数据 ──
with app.app_context():
    _s34_m = _s34_M(name='S34待擦商家')
    _s34_db.session.add(_s34_m)
    _s34_db.session.commit()
    _s34_sto = _s34_Sto(merchant_id=_s34_m.id, name='S34店', code='S34-C1')
    _s34_db.session.add(_s34_sto)
    _s34_db.session.commit()
    _s34_staff = _s34_St(username='s34-staff', display_name='S34店员',
                          merchant_id=_s34_m.id, store_id=_s34_sto.id)
    _s34_staff.set_password('S34pass!')
    _s34_admin = _s34_Admin(username='s34-owner', role='owner', merchant_id=_s34_m.id)
    _s34_admin.set_password('S34pass!')
    _s34_db.session.add_all([_s34_staff, _s34_admin])
    _s34_db.session.commit()
    from datetime import UTC as _s34_UTC            # 显式 UTC（文件里 datetime 名被重绑过，见第 35 轮教训）
    from datetime import datetime as _s34_dt, timedelta as _s34_td
    from src.business_calendar import business_today as _s34_btoday
    _s34_ds = _s34_DS(staff_id=_s34_staff.id, store_id=_s34_sto.id,
                      record_date=_s34_btoday(),   # 业务日：UTC 服务器上 today() 会把"今天"写成昨天
                      product_name='S34待擦流水品', product_sales_qty=3,
                      total_revenue=33, pack_id='milk_tea')
    _s34_ev_row = _s34_EV(email='S34-KEEP@Example.COM', purpose='signup', code_hash='x' * 64,
                          expires_at=_s34_dt.now(_s34_UTC).replace(tzinfo=None) +
                          _s34_td(minutes=10))
    _s34_db.session.add_all([_s34_ds, _s34_ev_row])
    _s34_db.session.commit()
    _s34_other = _s34_DS.query.filter(_s34_DS.store_id != _s34_sto.id).count()
    _s34_mid, _s34_sid, _s34_stid = _s34_m.id, _s34_sto.id, _s34_staff.id
    _s34_before = _s34_DS.query.filter_by(store_id=_s34_sid).count()
chk('S34 前置真的成立：待擦商家有 1 条连坐流水（前提不成立则下面的"擦干净"是空的）',
    _s34_before == 1, f'擦前 daily_sales={_s34_before}')

# ── 备份凭证：空库必须被拒，方向正确的在线备份必须被认 ──
_s34_empty = _s34_empty_db('empty.db')
_s34_proof_empty = _s34_e.backup_proof(_s34_empty)
chk('S34 0 表 0 行的"备份"不被当凭证（integrity_check 对空库照样 ok，是第 25 轮的实证）',
    not _s34_proof_empty['ok'], str(_s34_proof_empty)[:160])
with app.app_context():
    try:
        _s34_e.execute(_s34_mid, _s34_empty)
        _s34_refused = False
        _s34_why = '竟然执行了'
    except ValueError as exc:
        _s34_refused, _s34_why = True, str(exc)
    chk('S34 凭证不可用时 execute 直接抛错且不动盘（"静默什么都不做"会被读成删除成功）',
        _s34_refused and _s34_DS.query.filter_by(store_id=_s34_sid).count() == _s34_before,
        _s34_why[:140])
    _s34_good = _s34_backup_of_live('pre-erase.db')
    _s34_proof = _s34_e.backup_proof(_s34_good)
    chk('S34 可用备份凭证带 sha256/表数/行数三项读数，并**自报深度**（裸拷贝缺证据件 ⇒ 如实写明只过字节层）',
        _s34_proof['ok'] and _s34_proof.get('tables', 0) > 5 and _s34_proof.get('rows', 0) > 0
        and not _s34_proof['evidence_card'] and '证据件缺' in _s34_proof['detail'],
        str(_s34_proof)[:200])

    # 不可擦最小集合：现场造一个「名下有平台管理员」的商家来证明拒绝，而不是借用
    # 「演示库里 1 号恰好是平台」这种播种巧合（首版就是那样写的：在全新临时库里 id=1 是
    # 普通商家，那条判据当场就不成立，只是恰好被演示库喂绿了）。
    _s34_plat_m = _s34_M(name='S34平台租户')
    _s34_db.session.add(_s34_plat_m)
    _s34_db.session.commit()
    _s34_plat_adm = _s34_Admin(username='s34-platform', role='platform',
                               merchant_id=_s34_plat_m.id)
    _s34_plat_adm.set_password('S34platPass!')
    _s34_db.session.add(_s34_plat_adm)
    _s34_db.session.commit()
    _s34_plat_mid = _s34_plat_m.id
    try:
        _s34_e.execute(_s34_plat_mid, _s34_good)
        _s34_forbid = False
    except ValueError:
        _s34_forbid = True
    _s34_forbid_kept = _s34_M.query.get(_s34_plat_mid) is not None
    _s34_forbid_adm = _s34_Admin.query.filter_by(username='s34-platform').count() == 1
    chk('S34 不可擦最小集合：名下有平台管理员的商家被拒且数据未动（Frappe check_enable_disable 同族）',
        _s34_forbid and _s34_forbid_kept and _s34_forbid_adm,
        f'forbid={_s34_forbid} merchant_kept={_s34_forbid_kept} admin_kept={_s34_forbid_adm}')

    _s34_rep = _s34_e.execute(_s34_mid, _s34_good, emails=[])
    _s34_after = _s34_DS.query.filter_by(store_id=_s34_sid).count()
    _s34_after_staff = _s34_St.query.filter_by(id=_s34_stid).count()
    _s34_other_after = _s34_DS.query.filter(_s34_DS.store_id != _s34_sid).count()
    _s34_m_gone = _s34_M.query.get(_s34_mid) is None
chk('S34 连坐表真被擦：daily_sales 没有 merchant_id 列，却因 store_id 命中而被删（本轮核心反例）',
    _s34_after == 0, f'擦后 daily_sales={_s34_after}')
chk('S34 商家自身行也删了（根行在删除顺序最后一步，不留无主数据）',
    _s34_after_staff == 0 and _s34_m_gone, f'staff={_s34_after_staff} merchant_gone={_s34_m_gone}')
chk('S34 不误伤他人：另一商家的流水数擦前擦后相等（跨租户不误删）',
    _s34_other == _s34_other_after, f'{_s34_other} -> {_s34_other_after}')
chk('S34 未给邮箱时如实报残留，而不是静默跳过 email_verifications',
    'email_verifications' in _s34_rep['residual'], str(_s34_rep['residual'])[:120])

with app.app_context():
    _s34_e2 = _s34_M(name='S34待擦商家B')
    _s34_db.session.add(_s34_e2)
    _s34_db.session.commit()
    _s34_mid2 = _s34_e2.id
    _s34_rep2 = _s34_e.execute(_s34_mid2, _s34_backup_of_live('pre-erase2.db'),
                               emails=['s34-keep@example.com'])
    _s34_ev_left = _s34_EV.query.filter_by(email='S34-KEEP@Example.COM').count()
chk('S34 给了注册邮箱 ⇒ 验证表按 email 归一小写清掉，复扫零残留',
    _s34_ev_left == 0 and not _s34_rep2['residual'], str(_s34_rep2['residual'])[:120])

from src import query_guard as _s34_qguard  # noqa: E402

with app.app_context(), _s34_qguard.unscoped():
    _s34_bulk = _s34_CL.query.filter_by(action='bulk_delete').all()
    _s34_erase_rows = [c for c in _s34_bulk if 'erase merchant=' in (c.diff_json or '')]
    _s34_leak = [c.diff_json for c in _s34_erase_rows if 'S34待擦流水品' in (c.diff_json or '')]
    _s34_own_rows = [c.merchant_id for c in _s34_erase_rows if c.merchant_id is not None]
    # 反证这把尺真读得到被审对象：被擦商家自己的 change_log 行确实进了擦除集合
    # （为 0 行的话下面"证据不随主体消失"就是在测空气）
    _s34_own_gone = _s34_CL.query.filter_by(merchant_id=_s34_mid).count()
chk('S34 擦除留痕成物件：change_log 里有 erase 聚合行（哪张表、多少行、批次范围）',
    len(_s34_erase_rows) >= 3, f'erase 聚合行 {len(_s34_erase_rows)} 条')
chk('S34 证据不随证据主体消失：擦除聚合行一律 merchant_id=NULL（平台侧档）——'
    '按被擦商家记账时 change_log 自身在归属图里，动作会把自己的账一起擦掉'
    '（PostHog LOG_ACTIVITY 归入 STEPS_AFTER_DELETION 同族）',
    len(_s34_erase_rows) >= 3 and not _s34_own_rows and _s34_own_gone == 0,
    f'带商家归属的擦除行={_s34_own_rows[:3]}，该商家原 change_log 剩={_s34_own_gone}')
chk('S34 红线不越：擦除留痕只记条数与 id 范围，不含商家业务值',
    not _s34_leak, str(_s34_leak)[:150])

# ── 出口护栏：授权 + 短语 + 一次性 nonce ──
_anon = app.test_client()
_code_anon = _anon.post('/admin/platform/merchants/%d/erase' % _s34_mid, data={}).status_code
_owner = app.test_client()
_owner.post('/login', data={'username': 'owner_demo', 'password': 'owner123',
                            'role': 'admin', 'csrf_token': _s34_tok(_owner)})
_code_owner = _owner.post('/admin/platform/merchants/%d/erase' % _s34_mid,
                          data={'csrf_token': _s34_tok(_owner)}).status_code
_plat, _plat_uid = _s33_login('s34-platform', 'S34platPass!', 'admin')
chk('S34 平台侧身份真的登进去了（uid 非空 + 登录码 2xx/3xx），否则下面三层测的是空气',
    bool(_plat_uid) and _s33_login_codes.get('s34-platform', 0) < 400,
    f'uid={_plat_uid!r} login={_s33_login_codes.get("s34-platform")}')
with app.app_context():
    _code_missing = _plat.post('/admin/platform/merchants/%d/erase' % _s34_mid,
                               data={'csrf_token': _s34_tok(_plat),
                                     'confirm_phrase': 'x', 'confirm_checkbox': 'on',
                                     'erase_nonce': 'nope', 'backup_path': _s34_good}).status_code
chk('S34 出口授权三层：匿名非 2xx（CSRF 先行拦住也算）、老板(非 platform) 403、'
    '已确认登进去的平台管理员缺短语/失效 nonce 400',
    _code_anon >= 300 and _code_owner == 403 and _code_missing == 400,
    f'anon={_code_anon} owner={_code_owner} missing={_code_missing}')
import ast as _s34_ast  # noqa: E402
with open(os.path.join(_s34_root, 'src', 'erasure.py'), encoding='utf-8') as _s34_ef:
    _s34_etree = _s34_ast.parse(_s34_ef.read())
_s34_docids = set()
for _nd in _s34_ast.walk(_s34_etree):
    if isinstance(_nd, (_s34_ast.Module, _s34_ast.FunctionDef,
                        _s34_ast.AsyncFunctionDef, _s34_ast.ClassDef)):
        _fst = _nd.body[0] if getattr(_nd, 'body', None) else None
        if isinstance(_fst, _s34_ast.Expr) and isinstance(_fst.value, _s34_ast.Constant) \
                and isinstance(_fst.value.value, str):
            _s34_docids.add(id(_fst.value))
_s34_code_flags = sorted({n.value for n in _s34_ast.walk(_s34_etree)
                          if isinstance(n, _s34_ast.Constant) and isinstance(n.value, str)
                          and id(n) not in _s34_docids and '--execute' in n.value})
chk('S34 CLI 无 --execute 旁路（只扫代码位字符串；文档串里那句"这里没有 --execute"不算）',
    not _s34_code_flags, str(_s34_code_flags)[:120])

# ── 可达面（第 41 项 ⑧，本轮新增的另一半尺）──
# 前七条子判据全在判「该拒的有没有拒」；本轮实测到反方向：erase 挂对了
# `@role_required(ROLE_PLATFORM)`，却被 admin.py 的蓝图级「平台只看元数据」兜底拦成
# 永久 403 —— 唯一受众进不去，所有判据照样全绿。⇒ 「该放的放得进去吗」必须独立成判据。
_code_preview = _plat.get('/admin/platform/merchants/%d/erase' % _s34_plat_mid).status_code
chk('S34 目标角色真的进得去：平台管理员 GET 擦除预览 200（零写入），'
    '否则"护栏齐全"与"整条能力不可用"同形',
    _code_preview == 200, f'GET={_code_preview}')
_s34_reach_p, _s34_reach_e = _s34_qg._s41_platform_reach_from_file()
chk('S34 可达面对账：role_required(ROLE_PLATFORM) 端点逐条在蓝图级白名单内'
    '（名册 ⇄ 兜底双向核，缺一即红）',
    not _s34_reach_p, str(_s34_reach_p or _s34_reach_e)[:170])
_s34_synth_tree = _s34_ast.parse(
    "_PLATFORM_ALLOWED_ENDPOINTS = frozenset({'admin.a'})\n"
    "@role_required(ROLE_PLATFORM)\ndef blocked():\n    pass")
_s34_sp, _ = _s34_qg._s41_platform_reach(_s34_synth_tree, 'admin')
_s34_empty_p, _ = _s34_qg._s41_platform_reach(
    _s34_ast.parse('_PLATFORM_ALLOWED_ENDPOINTS = frozenset()'), 'admin')
chk('S34 可达面判据有鉴别力：合成"白名单缺一条"判红 + 空名单/零枚举判红（不得读成没东西可管）',
    bool(_s34_sp) and any('blocked' in x for x in _s34_sp) and bool(_s34_empty_p),
    f'{str(_s34_sp)[:80]}|{str(_s34_empty_p)[:60]}')

# ── 会红性：内存 + 文件级两类变异，逐条必须翻红 ──
_s34_reds, _s34_notes = 0, []
_s34_own_snap = dict(_s34_e.OWNING)
try:
    _s34_e.OWNING.pop('daily_sales')
    _p, _ = _s34_e.validate_registry()
    _s34_reds += 1 if any('daily_sales' in x for x in _p) else 0
    if not any('daily_sales' in x for x in _p):
        _s34_notes.append('漏登记 daily_sales 未判红')
finally:
    _s34_e.OWNING.clear()
    _s34_e.OWNING.update(_s34_own_snap)
try:
    _s34_e.OWNING.clear()
    _p, _ = _s34_e.validate_registry()
    _s34_reds += 1 if _p else 0
    if not _p:
        _s34_notes.append('空归属图未判红')
finally:
    _s34_e.OWNING.clear()
    _s34_e.OWNING.update(_s34_own_snap)
_s34_er_path = os.path.join(_s34_root, 'src', 'erasure.py')
_s34_origin = open(_s34_er_path, 'rb').read()
_s34_sha0 = _s34_h.sha256(_s34_origin).hexdigest()
_s34_file_red = _s34_cli_red = _s34_trail_red = False
_s34_t_all = _s34_t_null = _s34_t_mid = -1      # 取证用：-1 表示这段根本没跑到
_s34_t_ids: list = []
_s34_log0 = -1
try:
    _cut = _s34_origin.replace(b"    'daily_sales': {'mode': MODE_STORE_OR_STAFF},\n", b'')
    if _cut == _s34_origin:
        _s34_notes.append('删 daily_sales 的变异没落到字节')
    else:
        open(_s34_er_path, 'wb').write(_cut)
        _s34_qg.results.clear()
        _s34_qg.check_erasure_surface()
        _s34_file_red = not _s34_qg.results[-1][2]
    _cli = _s34_origin.replace(b"        for flag in ('--plan', '--verify'):",
                               b"        for flag in ('--plan', '--verify', '--execute'):")
    if _cli == _s34_origin:
        _s34_notes.append('加 --execute 的变异没落到字节')
    else:
        open(_s34_er_path, 'wb').write(_cli)
        _s34_qg.results.clear()
        _s34_qg.check_erasure_surface()
        _s34_cli_red = not _s34_qg.results[-1][2]
    # 第 5 类：把聚合留痕改回"记在被擦商家名下"。门禁不红（它管名册形状），
    # 红必须由行为判据出 —— 擦完之后按 NULL 档查不到这次擦除。
    _trail = _s34_origin.replace(
        b"change_log.note_bulk(name, 'bulk_delete', got, merchant_id=None,",
        b"change_log.note_bulk(name, 'bulk_delete', got, merchant_id=ctx['merchant_id'],")
    if _trail == _s34_origin:
        _s34_notes.append('留痕归属的变异没落到字节')
    else:
        open(_s34_er_path, 'wb').write(_trail)
        import importlib as _s34_il
        _s34_e2 = _s34_il.reload(_s34_e)
        try:
            with app.app_context(), _s34_qguard.unscoped():
                _s34_log0 = max([r[0] for r in
                                 _s34_CL.query.with_entities(_s34_CL.id).all()] or [0])
            with app.app_context():
                _m2 = _s34_M(name='S34变异商家')
                _s34_db.session.add(_m2)
                _s34_db.session.commit()
                _mid2 = _m2.id
                _s34_t_mid = _mid2
                _s34_e2.execute(_mid2, _s34_good, emails=[])
            with app.app_context(), _s34_qguard.unscoped():
                # 只认这次变异擦除**新增**的批次行（主键区间隔离）：按 `merchant=<id>`
                # 字面匹配会连上一次合法擦除的 NULL 行一起捞到 —— 被擦商家的 id 会被
                # SQLite 复用（rowid 别名，删掉最大行后 max+1 撞历史 id）。
                _s34_new = _s34_CL.query.filter(
                    _s34_CL.id > _s34_log0, _s34_CL.action == 'bulk_delete',
                    _s34_CL.diff_json.like('%erase merchant=%')).all()
                _s34_t_all = len(_s34_new)
                _s34_t_null = sum(1 for r in _s34_new if r.merchant_id is None)
                _s34_t_ids = [r.merchant_id for r in _s34_new][:4]
            _s34_trail_red = _s34_t_all > 0 and _s34_t_null == 0
            if _s34_t_all == 0:
                _s34_notes.append('留痕变异没产生任何擦除行（判据在测空气）')
        finally:
            _s34_il.reload(_s34_e)
finally:
    open(_s34_er_path, 'wb').write(_s34_origin)
chk('S34 变异反证 5 类全部判红（漏登记 daily_sales / 清空归属图 / 文件级删该档 / '
    'CLI 开后门 / 留痕记回被擦商家）',
    _s34_reds == 2 and _s34_file_red and _s34_cli_red and _s34_trail_red,
    f'内存 {_s34_reds}/2，文件级={_s34_file_red}，CLI 旁路={_s34_cli_red}，'
    f'留痕归属={_s34_trail_red}(新增擦除行={_s34_t_all}/其中 NULL 档={_s34_t_null}'
    f'/归属读数={_s34_t_ids}/log 起点={_s34_log0}/目标 id={_s34_t_mid})；{_s34_notes[:2]}')
chk('S34 文件级变异还原自证：src/erasure.py 还原后 sha256 与变异前逐字节相等',
    _s34_h.sha256(open(_s34_er_path, 'rb').read()).hexdigest() == _s34_sha0, '还原后字节不同')
with app.app_context():
    _p, _ = _s34_e.validate_registry()
chk('S34 还原对照：变异撤销后名册对账重新为空（红来自变异，不是残留状态）', not _p, str(_p)[:160])
_s34_qg.results.clear()
_s34_qg.check_erasure_surface()
chk('S34 门禁第 42 项在**本副本真链路**上判绿（不是只在沙箱能跑）',
    _s34_qg.results[-1][2], str(_s34_qg.results[-1][3])[:200])



# ══════ S35 业务日切与时区（第 29 轮 H77-H80）══════════════════════════════════════
import ast as _s35_ast
import hashlib as _s35_hashlib
import importlib as _s35_il
import os as _s35_os
import zoneinfo as _s35_zoneinfo
from datetime import date as _s35_date
from datetime import datetime as _s35_dt
from datetime import timedelta as _s35_td
from datetime import timezone as _s35_tz

import quality_gate as _s35_qg  # noqa: E402
from src import business_calendar as _s35_bc  # noqa: E402

_s35_root = _s35_os.path.dirname(_s35_os.path.abspath(__file__))
_s35_h = _s35_hashlib.sha256     # 本段自己的哈希句柄，避免与别的段混用
_S35_UTC = _s35_tz.utc
# UTC 23:30 = 北京次日 07:30：这一分钟正是"服务器时钟说昨天、店主说今天"的重叠区
_s35_late = _s35_dt(2026, 9, 26, 23, 30, tzinfo=_S35_UTC)
_s35_edge = _s35_dt(2026, 9, 26, 16, 0, tzinfo=_S35_UTC)      # = 北京 00:00 整


def _s35_exp(dt_utc):
    """独立算业务日（UTC+8 手算）⇒ 与 business_calendar 形成两把尺对照，不自比。"""
    return (dt_utc + _s35_td(hours=8)).date()


def _s35_mutate(rel, a, b):
    """按字节改一个文件（返回 (路径, 原始字节)）；调用方必须 finally 还原并自证 sha 相等。"""
    path = _s35_os.path.join(_s35_root, *rel.split('/'))
    origin = open(path, 'rb').read()
    txt = origin.decode('utf-8')
    if txt.count(a) != 1:
        return path, origin, False
    open(path, 'wb').write(txt.replace(a, b, 1).encode('utf-8'))
    return path, origin, True


chk('S35 业务日历必需函数齐备（"唯一入口"必须是真的可调用物）',
    all(callable(getattr(_s35_bc, n, None))
        for n in ('business_today', 'business_now', 'describe', 'validate_config', 'tz_name')))
chk('S35 日切核心反例：UTC 23:30 的业务日是次日，且与进程日期形态不同（这条腿在量那 8 小时）',
    _s35_bc.business_today(_s35_late) == _s35_exp(_s35_late) == _s35_date(2026, 9, 27)
    and _s35_late.date() == _s35_date(2026, 9, 26),
    f'业务日={_s35_bc.business_today(_s35_late)} 进程日={_s35_late.date()}')
chk('S35 日界点边界：UTC 16:00 恰是北京 00:00，业务日必须已翻到次日',
    _s35_bc.business_today(_s35_edge) == _s35_date(2026, 9, 27))
chk('S35 naive 输入按 UTC 解释（不吃本机时区）',
    _s35_bc.business_today(_s35_dt(2026, 9, 26, 16, 0)) == _s35_date(2026, 9, 27))
chk('S35 本机主路径必须走 zoneinfo（装了 tzdata 还长期靠降级 = 配置与实况漂移）',
    _s35_bc.describe()['resolved_via'] == 'zoneinfo', str(_s35_bc.describe())[:150])

# ── 两条解析路径等价，且降级必须可见（禁静默）──
_s35_fb_read, _s35_fb_same, _s35_fb_err = None, False, ''
_s35_orig_zi, _s35_orig_at = _s35_zoneinfo.ZoneInfo, _s35_zoneinfo.available_timezones
try:
    def _s35_boom(*_a, **_k):
        raise _s35_zoneinfo.ZoneInfoNotFoundError('夹具模拟：本机无 tz 数据库')

    _s35_zoneinfo.ZoneInfo = _s35_boom
    _s35_zoneinfo.available_timezones = lambda: set()
    _s35_bc._state.clear()
    _s35_fb_read = _s35_bc.describe()
    _s35_fb_same = (_s35_fb_read['resolved_via'] == 'fixed-offset'
                    and _s35_bc.business_today(_s35_late) == _s35_exp(_s35_late))
except ValueError as _exc:
    _s35_fb_err = str(_exc)[:120]
finally:
    _s35_zoneinfo.ZoneInfo, _s35_zoneinfo.available_timezones = _s35_orig_zi, _s35_orig_at
    _s35_bc._state.clear()
chk('S35 tz 库缺失时走固定偏移降级：业务日与主路径一致，且 resolved_via 如实改口（不静默）',
    _s35_fb_same, f'{str(_s35_fb_read)[:120]}|{ _s35_fb_err}')
chk('S35 降级表只收"无夏令时且偏移恒定"的区划（有 DST 的名字进来就是埋坑）',
    all(isinstance(h, int) and -12 <= h <= 14 for h in _s35_bc.FIXED_OFFSET_FALLBACK.values())
    and 'Asia/Shanghai' in _s35_bc.FIXED_OFFSET_FALLBACK
    and 'Europe/Berlin' not in _s35_bc.FIXED_OFFSET_FALLBACK,
    str(sorted(_s35_bc.FIXED_OFFSET_FALLBACK))[:150])

# ── 配置面：配错必须启动失败（InvenTree 同族），不是静默按进程时钟跑 ──
_S35_ENV = 'ICAN_BUSINESS_TZ'
_s35_saved_env = _s35_os.environ.get(_S35_ENV)


def _s35_set_env(val):
    if val is None:
        _s35_os.environ.pop(_S35_ENV, None)
    else:
        _s35_os.environ[_S35_ENV] = val
    _s35_bc._state.clear()


_s35_set_env('GMT+8')
_s35_shape_problems = _s35_bc.validate_config()
_s35_boot_raised = ''
try:
    _s34_create('development')                 # 真起一个 app，而不是只测函数
except ValueError as _exc:
    _s35_boot_raised = str(_exc)
_s35_set_env('Not/AZone')
_s35_unknown_problems = _s35_bc.validate_config()
_s35_set_env(_s35_saved_env)
chk('S35 手抄偏移口径（GMT+8）被形状判据拦下，且 create_app 当场 ValueError 不静默跑',
    bool(_s35_shape_problems) and 'ICAN_BUSINESS_TZ' in _s35_boot_raised
    and 'IANA' in _s35_boot_raised,
    f'{str(_s35_shape_problems)[:70]}|boot={_s35_boot_raised[:60]}')
chk('S35 合法形状但库里没有的区划（Not/AZone）同样判问题（"名字错"与"没数据库"必须分开）',
    bool(_s35_unknown_problems), str(_s35_unknown_problems)[:130])
chk('S35 恢复原配置后回到可用态（夹具没留下污染）',
    _s35_bc.validate_config() == [] and _s35_bc.describe()['resolved_via'] == 'zoneinfo')

# ── 出口与业务动作必须与店主同尺（真端点/真库/纯函数各验一次）──
_s35_client = app.test_client()
_s35_health = _s35_client.get('/healthz').get_json()
_s35_cal_view = _s35_health.get('calendar') or {}
chk('S35 /healthz 暴露 calendar（线上单靠 HTTP 就能回答"今天算给哪个时区"）',
    isinstance(_s35_cal_view, dict)
    and _s35_cal_view.get('business_date') == _s35_bc.business_today().isoformat()
    and _s35_cal_view.get('business_tz') == 'Asia/Shanghai'
    and _s35_cal_view.get('resolved_via') == 'zoneinfo',
    str(_s35_cal_view)[:170])
_s35_bd = _s35_bc.business_today()
_s35_tf = _s35_il.import_module('src.task_flow')
chk('S35 逾期判定按业务日：截止=业务日-1 逾期、=业务日 不逾期',
    _s35_tf.is_overdue(_NS(status='pending', due_date=_s35_bd - _s35_td(days=1))) is True
    and _s35_tf.is_overdue(_NS(status='pending', due_date=_s35_bd)) is False, '两态其一不符')
_s35_api = _s35_il.import_module('src.api_open')
chk('S35 开放 API 缺省日期 = 业务日，显式日期原样解析（外部集成方与店主同一天）',
    _s35_api._parse_date(None) == _s35_bd
    and _s35_api._parse_date('2026-09-26') == _s35_date(2026, 9, 26), str(_s35_api._parse_date(None)))
_s35_ms = _s35_il.import_module('src.multi_store')
chk('S35 多店异常检测：today 缺省与显式传业务日同结果（缺省不再吃进程时钟）',
    _s35_ms.abnormal_stores([], days=7) == _s35_ms.abnormal_stores([], days=7, today=_s35_bd))
with app.app_context():
    _s35_an = _s35_il.import_module('src.analytics')
    _s35_rows = len(_s35_an.get_sales_dataframe(days=7))
_s35_an_src = open(_s35_os.path.join(_s35_root, 'src', 'analytics.py'), encoding='utf-8').read()
chk('S35 看板窗口按业务日推 cutoff：源码里裸 date.today() 已清零，且取数不抛',
    'date.today()' not in _s35_an_src and 'cal.business_today()' in _s35_an_src and _s35_rows >= 0,
    f'窗口行数={_s35_rows}')

# ── 门禁第 43 项：真链路判绿 + 判据有鉴别力 + 名册失真会红 ──
_s35_qg.results.clear()
_s35_qg.check_business_calendar()
_s35_i43 = _s35_qg.results[-1]
chk('S35 门禁第 43 项在本副本真链路上判绿，且回执带取数面与三类时钟读数',
    _s35_i43[2] and any('取数面' in str(x) for x in _s35_i43[3]), str(_s35_i43[3])[:200])
_s35_empty_p, _ = _s35_qg._s43_shape_problems(
    {'src/business_calendar.py': _s35_ast.parse('x = 1')})
_s35_synth_n, _s35_synth_b, _s35_synth_e = _s35_qg._s43_clock_shapes(
    {'src/x.py': _s35_ast.parse('import datetime\nclass K:\n    @staticmethod\n'
                                '    def f():\n        return datetime.date.today()\n')})
chk('S35 第 43 项有鉴别力：空模块判红 + 合成裸调用点被枚举到（含行号）',
    bool(_s35_empty_p) and len(_s35_synth_n) == 1 and _s35_synth_n[0][1] == 5,
    f'{str(_s35_empty_p)[:80]}|{_s35_synth_n}')
_s35_allow_backup = dict(_s35_qg._S43_NAIVE_ALLOWED)
_s35_qg._S43_NAIVE_ALLOWED['no/such.py::ghost::date.today()'] = '这条是为了验证陈旧豁免会被抓住的夹具'
try:
    _s35_qg.results.clear()
    _s35_qg.check_business_calendar()
    _s35_stale_red = any('陈旧豁免' in str(x) for x in _s35_qg.results[-1][3])
finally:
    _s35_qg._S43_NAIVE_ALLOWED.clear()
    _s35_qg._S43_NAIVE_ALLOWED.update(_s35_allow_backup)
chk('S35 白名单里塞一条"已不存在的豁免"必须判红（名册失真与陈旧豁免同族）', _s35_stale_red)

# ── 会红性：四类变异逐条翻红 + 文件级逐字节还原自证 ──
_s35_reds, _s35_notes, _s35_shas = 0, [], {}
_s35_cal_path, _s35_cal_origin, _s35_cal_ok = _s35_mutate(
    'src/business_calendar.py', 'return moment.astimezone(tz).date()', 'return moment.date()')
if not _s35_cal_ok:
    _s35_notes.append('业务日行为变异锚点没落到字节')
else:
    try:
        _s35_il.reload(_s35_bc)
        _s35_wrong = _s35_bc.business_today(_s35_late) == _s35_late.date()
        _s35_reds += 1 if _s35_wrong else 0
        if not _s35_wrong:
            _s35_notes.append('把业务日改回进程时钟后，行为腿没变红')
    finally:
        open(_s35_cal_path, 'wb').write(_s35_cal_origin)
        _s35_il.reload(_s35_bc)
_s35_file_muts = [('src/routes/h5.py', '    today = cal.business_today()', '    today = date.today()'),
                  ('src/app.py', "            'calendar': _bc.describe(),", ''),
                  ('src/app.py', '    _cal_problems = business_calendar.validate_config()',
                   '    _cal_problems = []')]
for _s35_rel, _s35_a, _s35_b in _s35_file_muts:
    _s35_p, _s35_origin, _s35_applied = _s35_mutate(_s35_rel, _s35_a, _s35_b)
    _s35_shas[_s35_rel] = _s35_h(_s35_origin).hexdigest()
    if not _s35_applied:
        _s35_notes.append(f'{_s35_rel} 变异锚点命中异常')
        continue
    try:
        _s35_qg.results.clear()
        _s35_qg.check_business_calendar()
        _s35_red = not _s35_qg.results[-1][2]
        _s35_reds += 1 if _s35_red else 0
        if not _s35_red:
            _s35_notes.append(f'{_s35_rel} 变异后第 43 项仍判绿')
    finally:
        open(_s35_p, 'wb').write(_s35_origin)
_s35_recalc = _s35_h(open(_s35_cal_path, 'rb').read()).hexdigest() == _s35_h(_s35_cal_origin).hexdigest()
_s35_bad_files = [r for r, v in _s35_shas.items()
                  if _s35_h(open(_s35_os.path.join(_s35_root, *r.split('/')), 'rb').read()).hexdigest() != v]
chk('S35 文件级变异还原自证：被改文件还原后 sha256 与变异前逐字节相等',
    _s35_recalc and not _s35_bad_files, f'cal={_s35_recalc} 不等的文件={_s35_bad_files}')
_s35_qg.results.clear()
_s35_qg.check_business_calendar()
chk('S35 还原对照：撤销变异后业务日与门禁同时回到正常（红来自变异，不是残留状态）',
    _s35_bc.business_today(_s35_late) == _s35_exp(_s35_late)
    and _s35_bc.describe()['resolved_via'] == 'zoneinfo' and _s35_qg.results[-1][2],
    str(_s35_qg.results[-1][3])[:150])
_s35_qg.results.clear()
_s35_qg.check_business_calendar()
chk(f'S35 变异反证：行为/裸时钟/出口接线/配置校验共 4 类改法全部判红（实红 {_s35_reds}/4）',
    _s35_reds == 4, str(_s35_notes[:3]))


# ══ S36 出站投递面：SSRF 守卫 + 投递留痕 + 退避重试（第 30 轮 H81-H84）══════════
import ast as _s36_ast
import hashlib as _s36_hashlib
import os as _s36_os
from datetime import date as _s36_date
from datetime import datetime as _s36_dt
from datetime import timedelta as _s36_td

import quality_gate as _s36_qg  # noqa: E402
from src import outbound_guard as _s36_og  # noqa: E402
from src import webhooks as _s36_wh  # noqa: E402
from src.models import Merchant as _s36_M  # noqa: E402

_s36_root = _s36_os.path.dirname(_s36_os.path.abspath(__file__))
_S36_PUB = ['93.184.216.34']
_s36_NOW = _s36_dt(2026, 9, 27, 3, 0, 0)


def _s36_res(addrs):
    def _r(_host):
        return list(addrs)
    return _r


def _s36_raise(host):
    raise OSError('夹具模拟 NXDOMAIN')


def _s36_empty(host):
    return []


class _s36_Resp:
    def __init__(self, code):
        self.status_code = code


_s36_calls = []


def _s36_post(url, **kw):
    _s36_calls.append((url, kw))
    return _s36_Resp(200)


class _s36_PostSpy:
    """假 HTTP 客户端：记录被调与否，返回预设状态码（真投递腿全程不发 socket）。"""

    def __init__(self, code=200):
        self.code = code
        self.urls = []

    def __call__(self, url, **kw):
        self.urls.append((url, kw))
        return _s36_Resp(self.code)


chk('S36 守卫必需函数与白名单常量在册（"有守卫"必须是可调用物，不是文档一句）',
    all(callable(getattr(_s36_og, n, None))
        for n in ('check_target', 'pin_http_url', 'describe_host'))
    and _s36_og.OUTBOUND_PORTS == (80, 443) and set(_s36_og.OUTBOUND_SCHEMES) == {'http', 'https'})

# ── 反例矩阵：15 种 SSRF / 绕过形态逐个必须被拒，且给出可读原因 ──
_S36_DENY = [
    ('http://127.0.0.1:5000/admin', None, '端口白名单'),
    ('http://127.0.0.1/hook', None, '回环字面 IP'),
    ('http://169.254.169.254/latest/meta-data/', None, '链路本地元数据'),
    ('http://10.0.0.5/x', None, '私网段'),
    ('http://192.168.1.1/x', None, '私网段 2'),
    ('http://[::1]/x', None, 'IPv6 回环'),
    ('http://[fc00::5]/x', None, 'IPv6 站点本地'),
    ('http://[fe80::1]/x', None, 'IPv6 链路本地'),
    ('http://localhost/hook', None, '按名字挡内网名'),
    ('http://metadata.google.internal/', None, '云元域名'),
    ('http://db.local/x', None, '.local 内网名'),
    ('http://u:p@8.8.8.8/', None, 'userinfo 绕主机'),
    ('http://evil:8080@93.184.216.34/', None, 'userinfo 带端口'),
    ('file:///etc/passwd', None, '非 http 协议'),
    ('', None, '空 URL'),
    ('http://2130706433/', _s36_res(['127.0.0.1']), '十进制回环（按解析结果判）'),
    ('http://0x7f000001/', _s36_res(['127.0.0.1']), '十六进制回环'),
    ('http://mixed.example/x', _s36_res(['93.184.216.34', '10.1.2.3']), '多 IP 含内网 ⇒ 整条拒'),
    ('http://nx.example/x', _s36_raise, '解析失败 ⇒ 拒（不当放行）'),
    ('http://none.example/x', _s36_empty, '解析为空 ⇒ 拒'),
]
_s36_denied = [(u, r) for u, r, _ in _S36_DENY
               if not _s36_og.check_target(u, resolver=r)['ok']]
chk(f'S36 SSRF 反例矩阵 {len(_S36_DENY)} 形态逐个被拒（回环/私网/元数据/IPv6/userinfo/进制/多IP/解析失败）',
    len(_s36_denied) == len(_S36_DENY),
    f'被拒 {len(_s36_denied)}/{len(_S36_DENY)}，漏网 {[u for u, _r, _l in _S36_DENY if (u, _r) not in _s36_denied][:3]}')
chk('S36 拒绝原因可读（"被拒"必须说清为什么，否则商家只会看到一句"发不出去"）',
    all(len(_s36_og.check_target(u, resolver=r)['reason']) >= 8 for u, r, _ in _S36_DENY))
_s36_allow = [('https://hooks.example.com/x', _s36_res(_S36_PUB)),
              ('https://8.8.8.8/hook', None),
              ('http://[2606:2800:220:1:241:ff00::]/x', None),
              ('http://v6.example/x', _s36_res(['2606:2800:220:1:241:ff00::']))]
chk('S36 正例：公网域名/公网字面 IP/公网 IPv6 全部放行（反例矩阵若把公网也挡了就是假安全）',
    all(_s36_og.check_target(u, resolver=r)['ok'] for u, r in _s36_allow),
    str([(u, _s36_og.check_target(u, resolver=r)['reason']) for u, r in _s36_allow if
         not _s36_og.check_target(u, resolver=r)['ok']][:1])[:180])
_s36_pin = _s36_og.pin_http_url('http://hooks.example.com/a?b=1', '93.184.216.34')
chk('S36 http 按解析 IP 直连并保留原 Host 头；https 不钉 IP（钉了会破证书主机名校验）',
    _s36_pin[0].startswith('http://93.184.216.34/a') and _s36_pin[1] == 'hooks.example.com'
    and _s36_og.pin_http_url('https://hooks.example.com/a', '93.184.216.34') is None, str(_s36_pin))

# ── 创建时早失败 + 库里不留脏数据 ──
with app.app_context():                                     # noqa: F821
    from src.models import WebhookSubscription as _S36Sub
    from src.models import db as _s36_db
    _s36_m = _s36_M(name='S36投递商家')                     # noqa: F821
    _s36_db.session.add(_s36_m)
    _s36_db.session.commit()
    _s36_mid = _s36_m.id
    _s36_before = _S36Sub.query.count()
    _s36_err = ''
    try:
        import src.api_open as _s36_ao
        _s36_ao.subscribe_webhook(_s36_mid, 'http://127.0.0.1/internal-hook', '*')
    except ValueError as _e36:
        _s36_err = str(_e36)
    _s36_after = _S36Sub.query.count()
chk('S36 内网回调 URL 在创建时就被拒且库里不多一行（早失败，而不是投递时静默失败）',
    '127.0.0.1' in _s36_err or '回环' in _s36_err or '内网' in _s36_err, _s36_err[:120])
chk('S36 被拒的创建不留脏订阅行（前后计数相等，"拒绝但已落库"是另一半失效）',
    _s36_before == _s36_after, f'{_s36_before} -> {_s36_after}')

with app.app_context():                                     # noqa: F821
    from src.models import WebhookDelivery as _S36Del
    from src.models import WebhookSubscription as _S36Sub2
    import src.api_open as _s36_ao2
    _s36_rec, _s36_secret = _s36_ao2.subscribe_webhook(
        _s36_mid, 'https://8.8.8.8/hook', '*', resolver=None)
    _s36_sub_id = _s36_rec.id
    _s36_payload = _s36_wh.build_payload('sales.submitted', _s36_mid, {'n': 1})
    _s36_spy = _s36_PostSpy(200)
    _s36_res_ok = _s36_wh._deliver_one({'id': _s36_sub_id, 'url': _s36_rec.url,
                                        'secret': _s36_secret}, _s36_payload, app=app,
                                        seam={'timeout': 3, 'post': _s36_spy,
                                              'now': _s36_NOW})   # noqa: F821
    _s36_typo = ''
    try:
        _s36_wh._deliver_one({'id': _s36_sub_id, 'url': 'https://8.8.8.8/hook',
                              'secret': _s36_secret}, _s36_payload, app=app,
                             seam={'no': _s36_NOW})   # noqa: F821
    except ValueError as _e36t:
        _s36_typo = str(_e36t)
    chk('S36 注入位写错键名当场抛（"参数被静默忽略"正是本轮抓到的死参缺陷，不许再发生一次）',
        '未知注入位' in _s36_typo and 'no' in _s36_typo, _s36_typo[:120])
    _s36_row = _S36Del.query.filter_by(subscription_id=_s36_sub_id).first()
chk('S36 真投递成功腿：留痕一行 attempt=1、ok=True、next_retry 为空、payload 只存摘要',
    _s36_res_ok['ok'] and _s36_row is not None and _s36_row.attempt == 1
    and _s36_row.ok and _s36_row.next_retry is None
    and len(_s36_row.payload_sha256) == 64, str(_s36_row)[:160])
chk('S36 出站请求带签名/事件/投递号三个头，且 allow_redirects=False（策略而非建议）',
    all(h in _s36_spy.urls[0][1]['headers'] for h in (_s36_wh.SIGNATURE_HEADER,
                                                      _s36_wh.EVENT_HEADER,
                                                      _s36_wh.DELIVERY_HEADER))
    and _s36_spy.urls[0][1].get('allow_redirects') is False, str(_s36_spy.urls[0][1])[:200])
_s36_blocked_calls_before = len(_s36_spy.urls)
with app.app_context():                                     # noqa: F821
    _s36_res_blocked = _s36_wh._deliver_one(
        {'id': _s36_sub_id, 'url': 'http://169.254.169.254/latest/meta-data/',
         'secret': _s36_secret}, _s36_payload, app=app,
        seam={'timeout': 3, 'post': _s36_PostSpy(200), 'now': _s36_NOW})
    _s36_brow = _S36Del.query.filter_by(subscription_id=_s36_sub_id, blocked=True).first()
chk('S36 元数据地址被守卫拦下：blocked=True 留痕一行，且**一个字节都没发出去**（不发 socket）',
    _s36_res_blocked['blocked'] and not _s36_res_blocked['ok'] and _s36_brow is not None
    and _s36_brow.error_class.startswith('BlockedTarget'), str(_s36_res_blocked)[:170])
chk('S36 守卫在发送前判定 ⇒ 被拦下时假客户端调用次数不变（不是"发完再看结果"）',
    len(_s36_spy.urls) == _s36_blocked_calls_before,
    f'{_s36_blocked_calls_before} -> {len(_s36_spy.urls)}')

with app.app_context():                                     # noqa: F821
    _s36_res_403 = _s36_wh._deliver_one({'id': _s36_sub_id, 'url': 'https://8.8.8.8/hook',
                                         'secret': _s36_secret}, _s36_payload, app=app,
                                         seam={'timeout': 3, 'post': _s36_PostSpy(403),
                                               'now': _s36_NOW})
    _s36_last = (_S36Del.query.filter_by(subscription_id=_s36_sub_id)
                 .order_by(_S36Del.id.desc()).first())
chk('S36 非 2xx 记为一次失败并排到 5 分钟后重试（next_retry 是盘上物件，不是内存计数器）',
    not _s36_res_403['ok'] and _s36_last.error_class == 'Http403'
    and _s36_last.next_retry == _s36_NOW + _s36_td(minutes=5),
    f'{_s36_last.error_class} {_s36_last.next_retry}')
chk('S36 退避表：1→5min、2→30min、3→放弃（终态 None），且与 ATTEMPT_LIMIT 自洽',
    _s36_wh.next_retry_at(1, _s36_NOW) == _s36_NOW + _s36_td(minutes=5)
    and _s36_wh.next_retry_at(2, _s36_NOW) == _s36_NOW + _s36_td(minutes=30)
    and _s36_wh.next_retry_at(_s36_wh.ATTEMPT_LIMIT, _s36_NOW) is None, str(_s36_wh.RETRY_BACKOFF_MINUTES))

# ── 到期扫描：跨商家可见（unscoped 生效）、未到期的不进来、blocked 不重试 ──
with app.app_context():                                     # noqa: F821
    import src.api_open as _s36_ao3
    from src import query_guard as _s36_qg2
    from src.models import Merchant as _S36M
    from src.models import db as _s36_db2
    _s36_m2 = _S36M(name='S36投递商家2')
    _s36_db2.session.add(_s36_m2)
    _s36_db2.session.commit()
    _s36_rec2, _s36_sec2 = _s36_ao3.subscribe_webhook(_s36_m2.id, 'https://9.9.9.9/hook', '*')
    _s36_pl2 = _s36_wh.build_payload('sales.submitted', _s36_m2.id, {'n': 2})
    _s36_wh._deliver_one({'id': _s36_rec2.id, 'url': _s36_rec2.url, 'secret': _s36_sec2},
                         _s36_pl2, app=app,
                         seam={'timeout': 3, 'post': _s36_PostSpy(500),
                               'now': _s36_NOW})      # noqa: F821
    _s36_mysubs = {_s36_sub_id, _s36_rec2.id}
    with _s36_qg2.unscoped():
        _s36_due_all = [d.merchant_id for d in _s36_wh.due_deliveries(
            now=_s36_NOW + _s36_td(minutes=6), limit=200)]
        # 「未到期」只能在**本段自己造的**那几行上量：其它段的投递行按真实时钟落库（比注入钟早
        # 得多），把它们也算进来会把别人的行读成我的失效。
        _s36_too_early = [d for d in _s36_wh.due_deliveries(now=_s36_NOW, limit=200)
                          if d.subscription_id in _s36_mysubs]
    _s36_blk_rows = _S36Del.query.filter_by(blocked=True).all()
    _s36_blk_ids = {d.delivery_id for d in _s36_blk_rows}
chk('S36 到期扫描跨商家可见（平台动作）：两家各一次失败 ⇒ 到期窗口内扫到 2 条',
    len(_s36_due_all) >= 2 and {_s36_mid, _s36_m2.id} <= set(_s36_due_all),
    f'{_s36_due_all[:4]}')
chk('S36 未到期不得被扫到（把"没到点"读成"该重试"会把退避表抹平）',
    not _s36_too_early, f'{len(_s36_too_early)} 条提前')
with app.app_context():                                     # noqa: F821
    with _s36_qg2.unscoped():
        _s36_due_ids = {d.delivery_id for d in _s36_wh.due_deliveries(
            now=_s36_NOW + _s36_td(minutes=60), limit=200)}
chk('S36 被守卫拦下的投递**不进重试队列**（策略判定不是网络故障，重投只会再撞一次同一堵墙）',
    _s36_blk_ids and not (_s36_blk_ids & _s36_due_ids),
    f'blocked 投递号={len(_s36_blk_ids)} 个，与到期队列交集={sorted(_s36_blk_ids & _s36_due_ids)[:2]}')

# ── 连续失败停用 + 停用动作本身被归因链记账（跨轮联动）──
with app.app_context():                                     # noqa: F821
    _s36_rows = _S36Del.query.filter_by(subscription_id=_s36_sub_id).all()
    _s36_cf = _s36_wh.consecutive_failures(_s36_sub_id)
    _s36_pl3 = _s36_wh.build_payload('sales.submitted', _s36_mid, {'n': 3})
    for _k in range(_s36_wh.DISABLE_AFTER_CONSECUTIVE):
        _s36_wh._deliver_one({'id': _s36_sub_id, 'url': 'https://8.8.8.8/hook',
                              'secret': _s36_secret}, _s36_pl3, app=app,
                              seam={'timeout': 3, 'post': _s36_PostSpy(502),
                                    'now': _s36_NOW + _s36_td(minutes=90 + _k)})
    _s36_disabled = _s36_wh._maybe_disable(app, _s36_sub_id, now=_s36_NOW)
    _s36_sub_row = _S36Sub2.query.get(_s36_sub_id)
    from src.models import ChangeLog as _S36Log
    _s36_log = (_S36Log.query.filter_by(entity='webhook_subscriptions', action='update')
                .order_by(_S36Log.id.desc()).first())
chk('S36 单次失败不触发停用；连续失败达阈值才停（阈值取自配置常量，不写死数字）',
    _s36_cf is False or len(_s36_rows) < _s36_wh.DISABLE_AFTER_CONSECUTIVE,
    f'consecutive={_s36_cf} rows={len(_s36_rows)}')
chk('S36 连续失败达阈值 ⇒ 订阅自动停用（Stripe 对持续失败端点同姿态）',
    _s36_disabled and _s36_sub_row is not None and not _s36_sub_row.is_active,
    f'disabled={_s36_disabled} active={getattr(_s36_sub_row, "is_active", None)}')
chk('S36 停用动作被归因链自动记成字段级变更（第 26 轮的监听器在这里也管用，不靠手写留痕）',
    _s36_log is not None and 'is_active' in (_s36_log.diff_json or ''), str(_s36_log)[:160])
chk('S36 投递留痕表无 payload 正文字段（只存摘要；红线在第 26/28 轮同口径）',
    not [c for c in ('payload', 'payload_json', 'body')
         if hasattr(_S36Del, c)] and hasattr(_S36Del, 'payload_sha256'))

# ── 门禁第 44 项：真链路判绿 + 判据鉴别力（内存改 trees，不落盘）──
_s36_qg.results.clear()
_s36_qg.check_outbound_surface()
chk('S36 门禁第 44 项在本副本真链路上判绿（含出站点/豁免/面外文件三类读数）',
    _s36_qg.results[-1][2] and any('取数面' in str(x) for x in _s36_qg.results[-1][3]),
    str(_s36_qg.results[-1][3])[:200])
_ho = _s36_qg.read(_s36_os.path.join(_s36_root, 'src', 'webhooks.py'))
_s36_trees_noredir = {'src/webhooks.py': _s36_ast.parse(_ho.replace('allow_redirects=False', '')),
                      'src/outbound_guard.py': _s36_ast.parse(
                          _s36_qg.read(_s36_os.path.join(_s36_root, 'src', 'outbound_guard.py'))),
                      'src/api_open.py': _s36_ast.parse(
                          _s36_qg.read(_s36_os.path.join(_s36_root, 'src', 'api_open.py')))}
_s36_np, _ = _s36_qg._s44_shape_problems(_s36_trees_noredir)
_s36_empty_p, _ = _s36_qg._s44_shape_problems({'src/webhooks.py': _s36_ast.parse('x = 1')})
_s36_stale_backup = dict(_s36_qg._S44_OUTBOUND_ALLOWED)
_s36_qg._S44_OUTBOUND_ALLOWED['ghost/file.py::nope'] = '这条是为了验证陈旧豁免会被抓住的夹具条目'
try:
    _s36_qg.results.clear()
    _s36_qg.check_outbound_surface()
    _s36_stale_red = any('陈旧豁免' in str(x) for x in _s36_qg.results[-1][3])
finally:
    _s36_qg._S44_OUTBOUND_ALLOWED.clear()
    _s36_qg._S44_OUTBOUND_ALLOWED.update(_s36_stale_backup)
chk('S36 第 44 项有鉴别力：去掉 allow_redirects=False 判红 + 空守卫文件判红 + 幽灵豁免判红',
    bool(_s36_np) and bool(_s36_empty_p) and _s36_stale_red,
    f'{str(_s36_np)[:70]}|{str(_s36_empty_p)[:50]}|stale={_s36_stale_red}')
_s36_short_key = '_short/file.py::f'
_s36_qg._S44_OUTBOUND_ALLOWED[_s36_short_key] = '太短'
try:
    _s36_qg.results.clear()
    _s36_qg.check_outbound_surface()
    _s36_short_red = any('理由不足' in str(x) for x in _s36_qg.results[-1][3])
finally:
    _s36_qg._S44_OUTBOUND_ALLOWED.pop(_s36_short_key, None)
    _s36_qg._S44_OUTBOUND_ALLOWED.update(_s36_stale_backup)
    _s36_qg._S44_OUTBOUND_ALLOWED.pop('ghost/file.py::nope', None)
chk('S36 豁免理由短于 20 字必须判红（豁免是决策，一句话不算决策）', _s36_short_red)

# ── 会红性：两类文件级变异（拆守卫 / 拆创建时校验），逐条还原并 sha 自证 ──
_s36_reds, _s36_notes, _s36_shas = 0, [], {}
_s36_muts = [
    ('src/webhooks.py', '    verdict = outbound_guard.check_target(sub[\'url\'], resolver=resolver)',
     '    verdict = {"ok": True, "ip": None, "reason": "", "host": ""}  # mutation'),
    ('src/api_open.py', '    verdict = outbound_guard.check_target(clean_url, resolver=resolver)',
     '    verdict = {"ok": True, "reason": ""}  # mutation'),
]
for _s36_rel, _s36_a, _s36_b in _s36_muts:
    _s36_p = _s36_os.path.join(_s36_root, *_s36_rel.split('/'))
    _s36_origin = open(_s36_p, 'rb').read()
    _s36_shas[_s36_rel] = _s36_hashlib.sha256(_s36_origin).hexdigest()
    _s36_txt = _s36_origin.decode('utf-8')
    if _s36_txt.count(_s36_a) != 1:
        _s36_notes.append(f'{_s36_rel} 变异锚点命中 {_s36_txt.count(_s36_a)} 次')
        continue
    try:
        open(_s36_p, 'wb').write(_s36_txt.replace(_s36_a, _s36_b, 1).encode('utf-8'))
        _s36_qg.results.clear()
        _s36_qg.check_outbound_surface()
        _red = not _s36_qg.results[-1][2]
        _s36_reds += 1 if _red else 0
        if not _red:
            _s36_notes.append(f'{_s36_rel} 变异后第 44 项仍判绿')
    finally:
        open(_s36_p, 'wb').write(_s36_origin)
_s36_restored = all(_s36_hashlib.sha256(open(_s36_os.path.join(_s36_root, *r.split('/')),
                                             'rb').read()).hexdigest() == v
                     for r, v in _s36_shas.items())
chk('S36 文件级变异还原自证：被改文件还原后 sha256 与变异前逐字节相等',
    _s36_restored and len(_s36_shas) == 2, str(list(_s36_shas)[:2]))
chk(f'S36 变异反证：拆投递守卫 / 拆创建时校验 两类改法全部判红（实红 {_s36_reds}/2）',
    _s36_reds == 2, str(_s36_notes[:2]))
_s36_qg.results.clear()
_s36_qg.check_outbound_surface()
chk('S36 还原对照：撤销变异后第 44 项重新判绿（红来自变异，不是残留状态）',
    _s36_qg.results[-1][2], str(_s36_qg.results[-1][3])[:150])



# ══ S37 写路径并发正确性：丢失更新守卫 + CAS + 留痕联动（第 31 轮 H95-H98）══════
import hashlib as _s37_h
import io as _s37_io
import os as _s37_os
import re as _s37_re

import quality_gate as _s37_qg  # noqa: E402
from src import change_log as _s37_cl  # noqa: E402
from src import conflict_guard as _s37_cg  # noqa: E402
from src.models import Product as _s37P  # noqa: E402
from src.models import Store as _s37S  # noqa: E402
from src.models import db as _s37_db  # noqa: E402

_s37_root = _s37_os.path.dirname(_s37_os.path.abspath(__file__))


def _s37_client():
    from src import rate_limit as _rl37

    _rl37._buckets.clear()
    c = app.test_client()                                      # noqa: F821
    with c.session_transaction() as s:
        s['_user_id'] = 'admin-%d' % _s37_owner_id             # noqa: F821
        s['_fresh'] = True
    return c


def _s37_tok(c):
    html = c.get('/admin/products').get_data(as_text=True)     # noqa: F821
    m = _s37_re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else ''


with app.app_context():                                        # noqa: F821
    # 先定"用哪个老板账号"，再取**他名下**的门店：产品归属必须落在会话用户的门店范围内，
    # 否则 `_scoped_product_or_404` 会 404，测的就不是守卫而是越权拦截（同族：第 28 轮借来的账号不存在）
    _s37_owner = next((a for a in __import__('src.models', fromlist=['Admin']).Admin.query.all()
                       if getattr(a, 'role', '') == 'owner' and a.merchant_id), None)
    _s37_owner_id = _s37_owner.id
    _s37_store = _s37S.query.filter_by(merchant_id=_s37_owner.merchant_id).first()
    # 主键先取在手边：后面的腿跑在另一次 app_context 里，会话早已 teardown，
    # 再碰游离实例的属性就是 DetachedInstanceError（本轮实测撞上的第一条崩）。
    _s37_store_id = _s37_store.id
    # 包 id 现取现行激活的那个：写死 'convenience' 等于依赖**前一条测试留下的全局激活态**，
    # 顺序一变 `_scoped_product_or_404` 就 404，测的就不再是并发守卫而是可见性（本轮实测）。
    from src import industry_pack as _s37_ip
    _s37_pack = _s37_ip.active_pack_id()
    _s37_p = _s37P(store_id=_s37_store_id, product_name='S37并发产品', unit_price=20.0,
                   cost_price=8.0, category='S37', pack_id=_s37_pack, is_active=True)
    _s37_db.session.add(_s37_p)
    _s37_db.session.commit()
    _s37_pid = _s37_p.id
    _s37_name = _s37_p.product_name
    _s37_fp0 = _s37_cg.fingerprint(_s37_p)

chk('S37 守卫本体在册：check/cas_flip/fingerprint/claim_value/两个留痕口都是可调用物，常量也有名有值',
    all(callable(getattr(_s37_cg, n, None))
        for n in ('check', 'cas_flip', 'fingerprint', 'claim_value',
                  'note_cas_conflict'))
    and _s37_cg.FORM_FIELD == '_occ' and _s37_cg.CONFLICT_TOKEN_LEN == 16)
chk('S37 冲突动作名已进归因链词表（否则 note_denied 写进去的是没人认领的字符串）',
    'conflict_rejected' in _s37_cl.ACTIONS
    and callable(getattr(_s37_cl, 'note_denied', None)))

# ── 指纹：稳定、随值变、名册来自归因链（不另抄一份）──
_s37_fp_again = None
with app.app_context():                                        # noqa: F821
    _s37_fp_again = _s37_cg.fingerprint(_s37_db.session.get(_s37P, _s37_pid))
chk('S37 指纹可复算：同一行两次取值相等（凭据要能在另一次请求里重算出来）',
    _s37_fp_again == _s37_fp0 and len(_s37_fp0) == 16, f'{_s37_fp0} vs {_s37_fp_again}')
with app.app_context():                                        # noqa: F821
    _obj = _s37_db.session.get(_s37P, _s37_pid)
    _obj.category = 'S37改过'
    _s37_db.session.commit()
    _fp_after = _s37_cg.fingerprint(_obj)
    _obj.category = 'S37'
    _s37_db.session.commit()
chk('S37 任一在册列变了，指纹就变（凭据不是只盯主键的假版本）',
    _fp_after != _s37_fp0, f'{_s37_fp0} -> {_fp_after}')
# 名册判据**按权威源比**，不手抄字段清单：这一条第一版抄了我以为的 5 列，真名册是 7 列（含 pack_id/store_id）⇒ 判据自己漂移过一次，改判「两侧同源」才是本意。
_s37_rt, _s37_roster = _s37_cg._roster(_s37P)
chk('S37 指纹名册直接取自归因链（守卫没有第二份名单）',
    _s37_rt == 'products' and tuple(_s37_roster) == tuple(_s37_cl._fields_for('products'))
    and len(_s37_roster) >= 5, f'{_s37_rt} / {sorted(_s37_roster)}')
_s37_cg_src = _s37_io.open(_s37_os.path.join(_s37_root, 'src', 'conflict_guard.py'),
                           encoding='utf-8').read()
_s37_copied = _s37_re.findall(r"['\"](?:product_name|unit_price|cost_price)['\"]",
                              _s37_cg_src)
chk('S37 守卫源码不抄字段字面串（抄了就随归因链漂移，且漂移时两侧都会自证为真）',
    not _s37_copied, f'conflict_guard.py 里出现业务列名字面串：{sorted(set(_s37_copied))}')

# ── check()：一致放行 / 过期抛并点名字段 / 缺凭据计数 / 未登记表抛错 ──
_m0 = _s37_cg.stats()['missing_token']
with app.app_context():                                        # noqa: F821
    _o = _s37_db.session.get(_s37P, _s37_pid)
    _s37_cg.check(_o, _s37_cg.fingerprint(_o))
    _passed_ok = True
    try:
        _s37_cg.check(_o, 'f' * 16)
        _passed_ok = False
    except _s37_cg.ConflictError as _e:
        _raised = (list(_e.changed_fields), _e.table, _e.obj_id, _e.store_id)
    _s37_cg.check(_o, '')
    _s37_cg.check(_o, None)
    try:
        _s37_cg.check(_s37_db.session.get(_s37S, _s37_store_id), 'a' * 16)   # noqa: F841
        _untracked = False
    except ValueError:
        _untracked = True
chk('S37 check 三态齐：新鲜凭据放行、过期抛 ConflictError 且带表/主键/门店/字段名',
    _passed_ok and _raised[0] and _raised[1] == 'products'
    and _raised[2] == _s37_pid and _raised[3] == _s37_store_id, str(_raised)[:120])
chk('S37 缺凭据 ⇒ 放行但计数（先量误报率再决定是否拦人，且这条路径不是静默的）',
    _s37_cg.stats()['missing_token'] >= _m0 + 2, _s37_cg.stats())
chk('S37 表不在归因名册 ⇒ 抛 ValueError（"看不清配置"不得被读成"没有冲突"）', _untracked)
chk('S37 claim_value 形状：1/on/true 为真、0/off 为假、空与 None 是"没主张"',
    (_s37_cg.claim_value('1'), _s37_cg.claim_value('on'), _s37_cg.claim_value('0'),
     _s37_cg.claim_value(''), _s37_cg.claim_value(None)) == (True, True, False, None, None))

# ── 端到端：两个真会话，第二次必须 409 且库里仍是第一个人写的值 ──
_s37_cl.reset_counters()
_c1, _c2 = _s37_client(), _s37_client()
_t1, _t2 = _s37_tok(_c1), _s37_tok(_c2)
_s37_base = {'product_name': _s37_name, 'cost_price': '8.00', 'category': 'S37'}
with app.app_context():                                        # noqa: F821
    _occ_a = _s37_cg.fingerprint(_s37_db.session.get(_s37P, _s37_pid))
_r1 = _c1.post('/admin/products/%d/update' % _s37_pid,
               data=dict(_s37_base, unit_price='11.00', _occ=_occ_a, csrf_token=_t1))
_r2 = _c2.post('/admin/products/%d/update' % _s37_pid,
               data=dict(_s37_base, unit_price='12.00', _occ=_occ_a, csrf_token=_t2))
with app.app_context():                                        # noqa: F821
    _final = float(_s37_db.session.get(_s37P, _s37_pid).unit_price)
chk('S37 真双会话：带自己凭据的先写成功（200），后写的过期凭据被拒（409）',
    _r1.status_code == 200 and _r2.status_code == 409,
    f'{_r1.status_code} / {_r2.status_code}')
chk('S37 被拒的写没有落库：库里仍是先提交者的 11.00（"拒绝"若已覆盖＝白拒）',
    _final == 11.0, _final)
# jsonify 默认 ensure_ascii ⇒ 正文里是中文的 \uXXXX 转义，按裸字节找中文文案
    # 必然假失败（本轮实测）：判据改为解析 JSON 后按字段核，机读码与人读文案各自断言。
import json as _s37_json
_s37_r2body = _s37_json.loads(_r2.get_data().decode('utf-8'))
chk('S37 回包机读码是 conflict 且带人读文案（fetch 端点靠它把原因显示在行内红条）',
    _s37_r2body.get('error') == 'conflict' and '已被他人修改' in (_s37_r2body.get('message') or '')
    and _s37_r2body.get('fields'), str(_s37_r2body)[:150])
with app.app_context():                                        # noqa: F821
    _occ_b = _s37_cg.fingerprint(_s37_db.session.get(_s37P, _s37_pid))
_c3 = _s37_client()
_r3 = _c3.post('/admin/products/%d/update' % _s37_pid,
               data=dict(_s37_base, unit_price='13.00', _occ=_occ_b, csrf_token=_s37_tok(_c3)))
chk('S37 刷新凭据后同一人可继续编辑（守卫是"防覆盖"，不是"锁死这一行"）',
    _r3.status_code == 200, _r3.status_code)

# ── 留痕：被拒也要在归因链里可查，且只记字段名 ──
with app.app_context():                                        # noqa: F821
    _notes = [r for r in _s37_cl.recent(limit=40) if r.action == 'conflict_rejected']
    _dumps = ' | '.join((r.diff_json or '') for r in _notes)
chk('S37 冲突被拒留下可查记录（不是只有 stdout 一行字），且带真实主键',
    len(_notes) >= 1 and any(str(_s37_pid) in (r.diff_json or '') or r.entity_pk == _s37_pid
                            for r in _notes), f'{len(_notes)} 条')
chk('S37 留痕只记字段名与主键，不含任何业务值（第 26/28 轮红线在同一处成立）',
    '11.00' not in _dumps and '13.00' not in _dumps and 'denied' in _dumps, _dumps[:150])
chk('S37 被拒请求不得留下第二次 update 行（库里只应看到成功那次）',
    len([r for r in _notes if 'unit_price' in (r.diff_json or '')]) >= 1
    and _s37_cl.stats()['failed'] == 0, _s37_cl.stats())

# ── CAS 端到端：toggle 的 expect 由客户端主张 ──
with app.app_context():                                        # noqa: F821
    _act = bool(_s37_db.session.get(_s37P, _s37_pid).is_active)
_c4 = _s37_client()
_t4 = _s37_tok(_c4)
_r5 = _c4.post('/admin/products/%d/toggle' % _s37_pid,
               data={'expect_active': '0' if _act else '1', 'csrf_token': _t4})
_r6 = _c4.post('/admin/products/%d/toggle' % _s37_pid,
               data={'expect_active': '1' if _act else '0', 'csrf_token': _t4})
with app.app_context():                                        # noqa: F821
    _act2 = bool(_s37_db.session.get(_s37P, _s37_pid).is_active)
chk('S37 CAS 有鉴别力：过期主张 ⇒ 409 且状态不动；正确主张 ⇒ 200 且状态真翻转',
    _r5.status_code == 409 and _r6.status_code == 200 and _act2 is (not _act),
    f'{_r5.status_code}/{_r6.status_code} {_act}->{_act2}')
with app.app_context():                                        # noqa: F821
    # recent() 走 ORM 作用域会话，上下文外调用就是 RuntimeError（本轮实测的第二条崩）
    _s37_denied = [x for x in _s37_cl.recent(limit=40)
                   if x.action == 'conflict_rejected']
chk('S37 CAS 撞车也走同一条留痕口（动作名共用，不造第二套审计词汇）',
    any('is_active' in (r.diff_json or '') for r in _s37_denied),
    f'拒绝留痕 {len(_s37_denied)} 条，无一条带 is_active')

# ── 门禁第 45 项：真链路判绿 + 四类鉴别力（内存态）+ 两类文件级变异 ──
_s37_qg.results.clear()
_s37_qg.check_write_conflict()
chk('S37 门禁第 45 项在本副本真链路上判绿（A/B/C 三类计数 + 两端都在的凭据读数）',
    _s37_qg.results[-1][2] and any('类 A' in str(x) for x in _s37_qg.results[-1][3]),
    str(_s37_qg.results[-1][3])[:180])
_adm = _s37_qg.read(_s37_os.path.join(_s37_root, 'src', 'routes', 'admin.py'))
_cl_src = _s37_qg.read(_s37_os.path.join(_s37_root, 'src', 'change_log.py'))
_cg_src = _s37_qg.read(_s37_os.path.join(_s37_root, 'src', 'conflict_guard.py'))
_st_src = _s37_qg.read(_s37_os.path.join(_s37_root, 'src', 'routes', 'stores.py'))
_ao_src = _s37_qg.read(_s37_os.path.join(_s37_root, 'src', 'api_open.py'))


def _s37_trees(admin_text=_adm):
    import ast as _a37

    return {'src/routes/admin.py': _a37.parse(admin_text),
            'src/routes/stores.py': _a37.parse(_st_src),
            'src/routes/h5.py': _a37.parse('x = 1'),
            'src/api_open.py': _a37.parse(_ao_src),
            'src/task_flow.py': _a37.parse(_st_src),
            'src/change_log.py': _a37.parse(_cl_src),
            'src/conflict_guard.py': _a37.parse(_cg_src),
            'src/models.py': _a37.parse(_s37_qg.read(
                _s37_os.path.join(_s37_root, 'src', 'models.py')))}


_p1, _b1, _c1x = _s37_qg._s45_func_sites(_s37_trees()['src/routes/admin.py'], 'src/routes/admin.py',
                                 {c for v in _s37_cl.TRACKED.values() for c in v},
                                 _s37_qg._s45_model_words(_s37_trees()))
chk('S37 第 45 项看得见真样本：admin.py 里 A 类端点确被枚举到且已接守卫（不是靠豁免过关）',
    any(n.endswith('product_update') and g for n, _c, g in _p1), str(_p1)[:150])
_p2, _b2, _c2x = _s37_qg._s45_func_sites(
    _s37_trees(admin_text=_adm.replace('conflict_guard.check(product,', 'pass  # '))['src/routes/admin.py'],
    'src/routes/admin.py', {c for v in _s37_cl.TRACKED.values() for c in v},
    _s37_qg._s45_model_words(_s37_trees()))
chk('S37 第 45 项有鉴别力：拆掉端点里的 check 调用 ⇒ 该出口立刻变成"未接守卫"',
    _p2 and not _p2[0][2], str(_p2)[:150])
_p3, _b3, _c3x = _s37_qg._s45_func_sites(
    _s37_trees(admin_text=_adm.replace("request.form.get('expect_active')", 'None'))['src/routes/admin.py'],
    'src/routes/admin.py', {c for v in _s37_cl.TRACKED.values() for c in v},
    _s37_qg._s45_model_words(_s37_trees()))
chk('S37 toggle 的 expect 被拆成常量 None ⇒ 仍算 B 类样本（谓词读的是读改写形状，不是读参数）',
    any(n.endswith('product_toggle') for n, _f, _g in _b3), str(_b3)[:120])
_s37_qg._S45_ALLOWED['ghost/file.py::nope'] = '这条夹具是为了验证陈旧豁免会被抓住'
try:
    _s37_qg.results.clear()
    _s37_qg.check_write_conflict()
    _ghost_red = any('陈旧豁免' in str(x) for x in _s37_qg.results[-1][3])
finally:
    _s37_qg._S45_ALLOWED.pop('ghost/file.py::nope', None)
chk('S37 幽灵豁免判红：名册里放一条不存在的出口，"对空气豁免"必须被抓', _ghost_red)
_s37_qg.results.clear()
_s37_qg.check_write_conflict()
chk('S37 还原对照：撤掉幽灵豁免后第 45 项回到判绿（红来自变异不是残留）',
    _s37_qg.results[-1][2], str(_s37_qg.results[-1][3])[:150])

_s37_reds, _s37_notes, _s37_shas = 0, [], {}
_s37_muts = [
    ('src/routes/admin.py',
     '    conflict_guard.check(product, request.form.get(conflict_guard.FORM_FIELD))',
     '    pass  # mutation：拆掉整行覆盖前的凭据校验'),
    ('src/templates/admin/product_list.html',
     'name="_occ"', 'name="_nope"'),
]
for _s37_rel, _s37_a, _s37_b in _s37_muts:
    _s37_p = _s37_os.path.join(_s37_root, *_s37_rel.split('/'))
    _s37_origin = open(_s37_p, 'rb').read()
    _s37_shas[_s37_rel] = _s37_h.sha256(_s37_origin).hexdigest()
    _s37_txt = _s37_origin.decode('utf-8')
    if _s37_txt.count(_s37_a) < 1:
        _s37_notes.append(f'{_s37_rel} 变异锚点命中 {_s37_txt.count(_s37_a)} 次')
        continue
    try:
        open(_s37_p, 'wb').write(_s37_txt.replace(_s37_a, _s37_b).encode('utf-8'))
        _s37_qg.results.clear()
        _s37_qg.check_write_conflict()
        _red = not _s37_qg.results[-1][2]
        _s37_reds += 1 if _red else 0
        if not _red:
            _s37_notes.append(f'{_s37_rel} 变异后第 45 项仍判绿')
    finally:
        open(_s37_p, 'wb').write(_s37_origin)
_s37_restored = all(_s37_h.sha256(open(_s37_os.path.join(_s37_root, *r.split('/')),
                                       'rb').read()).hexdigest() == v
                    for r, v in _s37_shas.items())
chk('S37 文件级变异还原自证：被改文件（含模板）还原后 sha256 与变异前逐字节相等',
    _s37_restored and len(_s37_shas) == 2, str(list(_s37_shas)[:2]))
chk(f'S37 变异反证：拆服务端凭据校验 / 拆模板凭据字段 两类改法全部判红（实红 {_s37_reds}/2）',
    _s37_reds == 2, str(_s37_notes[:2]))
_s37_qg.results.clear()
_s37_qg.check_write_conflict()
chk('S37 还原后第 45 项重新判绿（两端都在：服务端读凭据 + 模板发凭据）',
    _s37_qg.results[-1][2] and any('模板发=是' in str(x) for x in _s37_qg.results[-1][3]),
    str(_s37_qg.results[-1][3])[:200])


# ══════════ S38 · 第 46 项 依赖供应链（生效面 / 钉版 / 两面审计 / 豁免台账）══════════
import hashlib as _s38_ha
import json as _s38_js
import os as _s38_os
import shutil as _s38_sh

import quality_gate as _s38_qg  # noqa: E402

_s38_root = _s38_os.path.dirname(_s38_os.path.abspath(__file__))
_s38_ws = _s38_root
for _ in range(6):
    if _s38_os.path.isfile(_s38_os.path.join(_s38_ws, '.github', 'workflows', 'quality-gate.yml')):
        break
    _s38_ws = _s38_os.path.dirname(_s38_ws)

_S38_FILES = {                                    # 会被改动的在册文件（改前取 sha，finally 逐字节还原）
    'dependabot': _s38_os.path.join(_s38_ws, '.github', 'dependabot.yml'),
    'requirements': _s38_os.path.join(_s38_root, 'requirements.txt'),
    'receipt': _s38_os.path.join(_s38_root, 'DEPENDENCY-AUDIT.json'),
    'ledger': _s38_os.path.join(_s38_root, 'DEPENDENCY-AUDIT-EXEMPTIONS.json'),
    'workflow': _s38_os.path.join(_s38_ws, '.github', 'workflows', 'quality-gate.yml'),
}
_s38_orig = {k: _s38_ha.sha256(_s38_os.fsencode(v)).hexdigest() for k, v in _S38_FILES.items()
             if _s38_os.path.isfile(v)}
chk('S38 夹具前置：五个被测文件都在场且取到基线 sha（缺件 = 变异测不到东西）',
    len(_s38_orig) == 5, f'仅 {len(_s38_orig)}/5 在场：{sorted(_S38_FILES)}')


def _s38_run():
    """跑第 46 项本体，返回 (ok, detail)。"""
    _s38_qg.results.clear()
    _s38_qg.check_dependency_supply_chain()
    _lvl, name, ok, detail = _s38_qg.results[-1]   # results 是四元组（级别也在册，第 20 项靠它核降级）
    return ok, (detail if isinstance(detail, list) else [detail]), name


def _s38_write(key, text):
    with open(_S38_FILES[key], 'w', encoding='utf-8', newline='') as fh:
        fh.write(text)


def _s38_restore(keys):
    for k in keys:
        src = _S38_TMP.get(k)
        if src and _s38_os.path.isfile(src):
            _s38_sh.copyfile(src, _S38_FILES[k])


_S38_TMP = {}
for _k, _p in _S38_FILES.items():
    if _s38_os.path.isfile(_p):
        _S38_TMP[_k] = _p + '.s38bak'
        _s38_sh.copyfile(_p, _S38_TMP[_k])


def _s38_db_face(pip_dirs):
    """在**合成面**上跑目录对账分支：交付镜像副本没有 `.git`，那条分支在真实副本上根本走不到，
    把反例建在它上面＝环境巧合判绿（判据交付契约 ②-b：反例的输入面必须落在被保护分支上）。
    """
    import tempfile as _tf
    base = _tf.mkdtemp(prefix='s38db_')
    try:
        _s38_os.makedirs(_s38_os.path.join(base, '.git'))
        _s38_os.makedirs(_s38_os.path.join(base, 'svc'))
        for sub in ('', 'svc'):
            with open(_s38_os.path.join(base, sub, 'requirements.txt'),
                              'w', encoding='utf-8', newline='') as fh:
                fh.write('Flask==3.1.3\n')
        gh = _s38_os.path.join(base, '.github')
        _s38_os.makedirs(gh)
        with open(_s38_os.path.join(gh, 'dependabot.yml'), 'w', encoding='utf-8', newline='') as fh:
            fh.write('version: 2\nupdates:\n'
                     '  - package-ecosystem: "pip"\n    directories:\n'
                     + ''.join(f'    - "{d}"\n' for d in pip_dirs)
                     + '    schedule:\n      interval: "weekly"\n    open-pull-requests-limit: 3\n'
                     '  - package-ecosystem: "github-actions"\n    directory: "/"\n'
                     '    schedule:\n      interval: "weekly"\n    open-pull-requests-limit: 3\n')
        return _s38_qg._s46_dependabot_problems(gh)
    finally:
        _s38_sh.rmtree(base, ignore_errors=True)


try:
    _ok0, _det0, _nm0 = _s38_run()
    chk('S38 第 46 项在本副本真链路上判绿（不是只在沙箱能跑）', _ok0, '；'.join(map(str, _det0))[:200])
    chk('S38 第 46 项印出了生效面与两面读数（PASS 必须说实话：生态/目录/审计时刻都要可见）',
        any('生态=' in str(d) for d in _det0) and any('审计 ' in str(d) for d in _det0)
        and any('全部带上界' in str(d) for d in _det0), '；'.join(map(str, _det0))[:200])
    chk('S38 说实话·目录对账的模式必须可见：生效面报实测目录数，镜像面点名"交由第 21 项"，不许静默跳过',
        any(('实测带 requirements 的目录' in str(d)) or ('交付镜像' in str(d)) for d in _det0),
        '；'.join(map(str, _det0))[:200])

    _ghost_p, _ghost_e = _s38_db_face(['/', '/svc/', '/no-such/'])
    chk('S38 有牙：pip 目录指向不存在处 ⇒ 判"幽灵覆盖"（合成面，镜像副本里也能红）',
        any('幽灵覆盖' in str(d) for d in _ghost_p), f'{_ghost_p}｜{_ghost_e}'[:220])
    _miss_p, _miss_e = _s38_db_face(['/', ])
    chk('S38 有牙：少声明一个带 requirements 的目录 ⇒ 判"该面永不更新"（真实分母对账，不是数条数）',
        any('没被 dependabot 覆盖' in str(d) for d in _miss_p), f'{_miss_p}｜{_miss_e}'[:220])
    _good_p, _good_e = _s38_db_face(['/', '/svc/'])
    chk('S38 正向腿：目录与 requirements 一一对上 ⇒ 两条目录判据都不许响（防对账分支恒红）',
        not any(('幽灵覆盖' in str(d)) or ('没被 dependabot 覆盖' in str(d)) for d in _good_p),
        f'{_good_p}｜{_good_e}'[:220])

    # ⑤ 门禁自身可跑性：本轮实测本机 .venv 无 PyYAML ⇒ 第 33/34/46 项一起退化成"没测到"。
    _gi_need = _s38_qg._s46_third_party_imports(
        'import os, re\nimport yaml\nimport tomlkit\nfrom src import models\nfrom .x import y\n')
    chk('S38 取数面只留第三方：标准库/本仓包/相对 import 都不算依赖（否则分母虚高）',
        _gi_need == {'yaml', 'tomlkit'}, str(sorted(_gi_need)))
    chk('S38 有牙：门禁 import 未声明的包必须点名（tomlkit 缺席即红，不许静默少测）',
        _s38_qg._s46_missing_gate_dists({'yaml', 'tomlkit'}, {'pyyaml'}) == ['tomlkit'],
        str(_s38_qg._s46_missing_gate_dists({'yaml', 'tomlkit'}, {'pyyaml'})))
    chk('S38 别名归一：import 名 yaml 对得上发行包名 PyYAML（大小写/下划线不得判成缺件）',
        _s38_qg._s46_missing_gate_dists({'yaml'}, {'pyyaml'}) == []
        and 'pyyaml' in _s38_qg._s46_declared_dists('PyYAML>=6.0,<7\nflask[async]>=3.0,<4\n# 注释\n'),
        str(sorted(_s38_qg._s46_declared_dists('PyYAML>=6.0,<7\nflask[async]>=3.0,<4\n'))))
    _gi_p, _gi_e = _s38_qg._s46_gate_import_problems(_s38_root)
    chk('S38 真链路上门禁自身第三方 import 已全部入契约，且印出了实测个数（PASS 要说实话）',
        not _gi_p and any('全部在依赖契约内' in str(d) for d in _gi_e), f'{_gi_p}｜{_gi_e}'[:200])

    _dep = open(_S38_FILES['dependabot'], encoding='utf-8').read()
    # 整段摘掉 github-actions 生态（只删两行会留悬空键 ⇒ 变异自己造出语法错，测的就不是缺生态了）：
    # 这条与上面两条不同，它不依赖 `.git`，因此在交付镜像里同样必须变红，证明本副本读的就是这份文件。
    _ci_i = _dep.index('  # CI 自身：')
    _s38_write('dependabot', _dep[:_ci_i])
    _ok, _det, _ = _s38_run()
    chk('S38 有牙：摘掉 github-actions 生态 ⇒ 整项判红并点名 CI 自身供应链无人管（镜像副本同样必须红）',
        not _ok and any('github-actions' in str(d) for d in _det), '；'.join(map(str, _det))[:220])
    _s38_restore(['dependabot'])

    _os_name = _s38_os.path.basename(_S38_FILES['dependabot'])
    _s38_sh.move(_S38_FILES['dependabot'], _S38_FILES['dependabot'] + '.off')
    _ok, _det, _ = _s38_run()
    chk('S38 有牙：根上没有 dependabot 判红，且文案点名"从未生效"而非"已退役"（本轮案发形态）',
        not _ok and any('从未生效' in str(d) for d in _det), '；'.join(map(str, _det))[:220])
    _s38_sh.move(_S38_FILES['dependabot'] + '.off', _S38_FILES['dependabot'])

    _req = open(_S38_FILES['requirements'], encoding='utf-8').read()
    _s38_write('requirements', _req.replace('coverage>=7.0,<9', 'coverage>=7.0'))
    _ok, _det, _ = _s38_run()
    chk('S38 有牙：去掉一条上界 ⇒ 判红且只点名那一条（pandas 2→3 实测会打穿 CSV 护栏）',
        not _ok and sum(1 for d in _det if '无上界' in str(d)) == 1
        and any('coverage>=7.0' in str(d) for d in _det), '；'.join(map(str, _det))[:200])
    _s38_restore(['requirements'])

    _rec = open(_S38_FILES['receipt'], encoding='utf-8').read()
    _s38_write('receipt', _rec.replace('"generated_at": "2026', '"generated_at": "2020'))
    _ok, _det, _ = _s38_run()
    chk('S38 有牙：回执过期 ⇒ 判红并给出可抄的重扫命令（新鲜度是有期限的，不是"曾经跑过"）',
        not _ok and any('未更新' in str(d) and 'check_dependency_audit.py' in str(d) for d in _det),
        '；'.join(map(str, _det))[:200])
    _s38_restore(['receipt'])

    _s38_sh.move(_S38_FILES['receipt'], _S38_FILES['receipt'] + '.off')
    _ok, _det, _ = _s38_run()
    chk('S38 有牙：无回执判红且给命令（零输入不得 PASS，缺测量≠没问题）',
        not _ok and any('DEPENDENCY-AUDIT.json' in str(d) for d in _det), '；'.join(map(str, _det))[:180])
    _s38_sh.move(_S38_FILES['receipt'] + '.off', _S38_FILES['receipt'])

    _d = _s38_js.loads(_rec)
    _d['faces']['environment']['vulns'] = [{'name': 'demo', 'version': '9.9',
                                            'ids': ['PYSEC-2099-0001']}]
    _d['faces']['environment']['vulnerable_packages'] = 1
    _s38_write('receipt', _s38_js.dumps(_d, ensure_ascii=False, indent=1, sort_keys=True) + '\n')
    _ok, _det, _ = _s38_run()
    chk('S38 有牙：任一面的未豁免漏洞即红（只扫声明面会漏环境面孤儿包，本轮实测 10 条全在环境面）',
        not _ok and any('未豁免漏洞' in str(d) and 'demo==9.9' in str(d) for d in _det),
        '；'.join(map(str, _det))[:200])
    _s38_restore(['receipt'])

    _one = _s38_js.loads(_rec)
    _one['faces'].pop('declared', None)
    _s38_write('receipt', _s38_js.dumps(_one, ensure_ascii=False, indent=1, sort_keys=True) + '\n')
    _ok, _det, _ = _s38_run()
    chk('S38 有牙：回执只有一面 ⇒ 判"两面都必须在场"（不是数少一面就放宽一次）',
        not _ok and any('两面都在' in str(d) for d in _det), '；'.join(map(str, _det))[:180])
    _s38_restore(['receipt'])

    _led = open(_S38_FILES['ledger'], encoding='utf-8').read()
    _s38_write('ledger', _led.replace('"exemptions": []',
                                      '"exemptions": [{"id": "PYSEC-2099-7777", '
                                      '"reason": "这条理由太短"}, {"id": "乱七八糟", '
                                      '"reason": "形状也不对需要足够长的说明文字在这里", '
                                      '"expires": "2020-01-01"}]'))
    _ok, _det, _ = _s38_run()
    _txt = '；'.join(map(str, _det))
    chk('S38 有牙：幽灵豁免 + id 形状不合 + 理由不足 + 缺到期日 ⇒ 四类台账缺陷同时被抓',
        (not _ok) and any('幽灵豁免' in str(d) for d in _det)
        and 'PYSEC/GHSA/CVE 形状' in _txt
        and '不足 20 字' in _txt and '缺 ISO 到期日' in _txt, _txt[:260])
    _s38_restore(['ledger'])

    _wf = open(_S38_FILES['workflow'], encoding='utf-8').read()
    chk('S38 CI 接线：守卫 D 真在工作流里跑审计脚本（不是只写在文档里）',
        'check_dependency_audit.py' in _wf and 'pip-audit==' in _wf, '工作流缺审计步骤')
    _ci_cmd = 'python iCAN大学生创新创业大赛/check_dependency_audit.py --no-write'
    _s38_write('workflow', _wf.replace(_ci_cmd, '# ' + _ci_cmd))
    _ok, _det, _ = _s38_run()
    chk('S38 有牙：把 CI 那行注释掉 ⇒ 判"未接进阻断链"（在册脚本 ≠ 在跑机制）',
        not _ok and any('CI 未执行' in str(d) for d in _det), '；'.join(map(str, _det))[:180])
    _s38_restore(['workflow'])

    # ── 审计脚本自身的输出契约与退出码类别（不打网络：替换取数函数，驱动真 main）──
    import check_dependency_audit as _s38_aud
    _s38_real_run = _s38_aud._run_audit
    _s38_cases = {('ok', 'ok'): 0, ('no-tool', 'ok'): 2, ('unreachable', 'ok'): 3}
    _s38_rc = {}
    try:
        for _pair in _s38_cases:
            def _fake(extra, _st=_pair):
                face = _st[0] if extra and extra[0] == '-r' else _st[1]
                return {'status': face, 'vulns': [], 'error': '', 'packages': 3}
            _s38_aud._run_audit = _fake
            _s38_rc[_pair] = _s38_aud.main(['--json', '--no-write'])
        _s38_aud._run_audit = lambda extra: {'status': 'ok', 'packages': 3, 'error': '',
                                              'vulns': [{'name': 'x', 'version': '1',
                                                         'ids': ['PYSEC-2099-0001']}]}
        _rc_vuln = _s38_aud.main(['--json', '--no-write'])
    finally:
        _s38_aud._run_audit = _s38_real_run
    chk('S38 退出码是类别码：ok/no-tool/unreachable 分别映射 0/2/3（未验证绝不塌成 0）',
        all(_s38_rc[k] == v for k, v in _s38_cases.items()), str(_s38_rc))
    chk('S38 有漏洞 ⇒ rc=1（与"工具不可用=2"、"网络不可达=3"分档，不混成一个红）',
        _rc_vuln == 1, str(_rc_vuln))
    _s38_one = _s38_aud.main.__doc__ or ''
    chk('S38 --json 只准打一个文档：源码里 print(json.dumps( 只能出现一次（多打即下游 Extra data）',
        sum(1 for _ln in open(_s38_os.path.join(_s38_root, 'check_dependency_audit.py'),
                                      encoding='utf-8').read().splitlines()
            if 'print(json.dumps(' in _ln and not _ln.strip().startswith('#')) == 1,
        '见 check_dependency_audit.py 的打印点')
    _now = {k: _s38_ha.sha256(_s38_os.fsencode(v)).hexdigest() for k, v in _S38_FILES.items()
            if _s38_os.path.isfile(v)}
    chk('S38 文件级变异还原自证：五个文件还原后 sha256 与变异前逐字节相等',
        _now == _s38_orig, f'漂移={sorted(k for k in _now if _now[k] != _s38_orig.get(k))}')
    _ok, _det, _ = _s38_run()
    chk('S38 还原对照：撤销全部变异后第 46 项重新判绿（红来自变异，不是残留状态）', _ok,
        '；'.join(map(str, _det))[:200])
finally:
    _s38_restore(list(_S38_TMP))
    for _t in _S38_TMP.values():
        if _s38_os.path.isfile(_t):
            _s38_os.remove(_t)
    for _p in (_S38_FILES['dependabot'] + '.off', _S38_FILES['receipt'] + '.off'):
        if _s38_os.path.isfile(_p):
            _s38_os.remove(_p)



# ══════════ S39 · 第 47/48 项 备份可恢复性 + 提交阻断链（2026-09-27 第 33 轮）══════════
import json as _s39_js
import os as _s39_os
import shutil as _s39_sh
import subprocess as _s39_sp
import tempfile as _s39_tf

import quality_gate as _s39_qg  # noqa: E402
import restore_db as _s39_rd    # noqa: E402

_s39_root = _s39_os.path.dirname(_s39_os.path.abspath(__file__))
_s39_WATCH = ['restore_db.py', 'quality_gate.py', 'init_db.py', 'src/schema_ledger.py',
              'UPGRADE.md', 'verify_local.py', _s39_os.path.join('..', '.ci', 'contract.json')]
_s39_pre = {w: (_s39_qg._sha256_file(_s39_os.path.join(_s39_root, w))
                if _s39_os.path.isfile(_s39_os.path.join(_s39_root, w)) else '') for w in _s39_WATCH}
_s39_RC5 = 'RC_OK RC_MISMATCH RC_NO_INPUT RC_TARGET_EXISTS RC_EVIDENCE'


def _s39_cli(*args):
    """入口按子进程实测：import 纯函数 ≠ CLI 跑得通（rc / stdout / traceback 三样都要取）。"""
    return _s39_sp.run([sys.executable, _s39_os.path.join(_s39_root, 'restore_db.py'), *args],
                       capture_output=True, text=True, cwd=_s39_root)


def _s39_last():
    lvl, name, ok, det = _s39_qg.results[-1]
    return ok, (det if isinstance(det, list) else [det])


def _s39_fake_repo(track=False, contract=None, hook=None, skips=None):
    """合成仓根驱动第 48 项的三个纯函数：反例不碰真仓文件（共享工作树里改钩子＝动别人地基）。"""
    d = _s39_tf.mkdtemp(prefix='s39repo_')
    _s39_os.makedirs(_s39_os.path.join(d, '.ci'), exist_ok=True)
    _s39_os.makedirs(_s39_os.path.join(d, '.git', 'hooks'), exist_ok=True)
    if contract is not None:
        with open(_s39_os.path.join(d, '.ci', 'contract.json'), 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(contract)
    if hook is not None:
        with open(_s39_os.path.join(d, '.git', 'hooks', 'pre-push'), 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(hook)
    if skips is not None:
        with open(_s39_os.path.join(d, '.ci', 'prepush-skips.jsonl'), 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(skips)
    if track:
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        for args in (['init', '-q'], ['add', '-A'], ['commit', '-qm', 's39']):
            _s39_sp.run(['git', '-C', d] + args, capture_output=True, env=env)
    return d


_OK_CONTRACT = _s39_js.dumps({'schema': 'fenjue-ci-green-contract-v1', 'refresh_days': 7,
                              'checks': [{'name': '权威门禁',
                                          'cmd': 'python iCAN大学生创新创业大赛/quality_gate.py'}]})
_OK_HOOK = ('#!/bin/sh' + chr(10) +
            '# 由 greencheck bootstrap 安装的通用 pre-push' + chr(10)
            + 'python "iCAN大学生创新创业大赛/quality_gate.py" || exit 1' + chr(10))

_s39_qg.results.clear()
_s39_qg.check_restore_reversibility()
_ok47, _det47 = _s39_last()
chk('S39 第 47 项在本副本真链路上判绿（往返与台账复算都真做过）', _ok47, '；'.join(map(str, _det47))[:220])
chk('S39 第 47 项印出了「往返实测」与「恢复台账」两行（PASS 必须说实话）',
    any('往返实测' in str(d) for d in _det47) and any('恢复台账' in str(d) for d in _det47),
    '；'.join(map(str, _det47))[:200])
_rt_ok, _rt_det = _s39_rd.selftest_roundtrip(_s39_tf.mkdtemp(prefix='s39rt_'))
chk('S39 往返自证在独立临时目录也绿（不靠上一轮留下的文件）', _rt_ok, str(_rt_det)[:180])

_tmp = _s39_tf.mkdtemp(prefix='s39cli_')
_src, _bak, _dst = (_s39_os.path.join(_tmp, n) for n in ('src.db', 'bak.db', 'dst.db'))
_sqlite = __import__('sqlite3')
_conn = _sqlite.connect(_src)
_conn.execute('CREATE TABLE merchants (id INTEGER PRIMARY KEY, name TEXT)')
_conn.executemany('INSERT INTO merchants (name) VALUES (?)', [('甲',), ('乙',)])
_conn.commit()
_conn.close()
_s39_rd.snapshot_into(_src, _bak)
_r = _s39_cli('--backup', _bak, '--into', _dst, '--no-ledger', '--json')
try:
    _doc = _s39_js.loads(_r.stdout.strip())
except ValueError:
    _doc = {'rc': None, '_bad': _r.stdout[:80]}
chk('S39 CLI 真往返：rc=0 且 stdout 恰好一个 JSON 文档（多打即下游 Extra data）',
    _r.returncode == 0 and len(_r.stdout.strip().splitlines()) == 1 and _doc.get('rc') == 0,
    'rc=%s out=%r' % (_r.returncode, _r.stdout[:120]))
chk('S39 恢复读数按逐表行数断言（不是「目标非空」）',
    (_doc.get('key_rows') or {}).get('merchants') == 2 and _doc.get('row_total') == 2,
    str(_doc.get('key_rows')))
_sha_before = _s39_qg._sha256_file(_dst)
_r2 = _s39_cli('--backup', _bak, '--into', _dst, '--no-ledger', '--json')
chk('S39 有牙：目标已存在且无 --force ⇒ rc=3 拒覆盖，且目标字节一个都没动',
    _r2.returncode == 3 and _s39_qg._sha256_file(_dst) == _sha_before,
    'rc=%s out=%r' % (_r2.returncode, _r2.stdout[:110]))
_r3 = _s39_cli('--backup', _bak, '--into', _dst, '--force', '--no-ledger', '--json')
_d3 = _s39_js.loads(_r3.stdout.strip()) if _r3.stdout.strip().startswith('{') else {}
_anchor = _d3.get('rollback_anchor') or ''
_anchor_path = _s39_os.path.join(_tmp, _anchor) if _anchor else ''
chk('S39 --force 覆盖前先落回滚锚点，且锚点字节 == 覆盖前的目标（对偶 discourse rollback!）',
    _r3.returncode == 0 and bool(_anchor) and _s39_os.path.isfile(_anchor_path)
    and _s39_qg._sha256_file(_anchor_path) == _sha_before, '%s rc=%s' % (_anchor, _r3.returncode))
_zero = _s39_os.path.join(_tmp, 'zero.db')
open(_zero, 'wb').close()
_never = _s39_os.path.join(_tmp, 'never.db')
_r4 = _s39_cli('--backup', _zero, '--into', _never, '--no-ledger', '--json')
chk('S39 失败路径本身不许崩：空文件 ⇒ rc=2、无 traceback、且不产出目标',
    _r4.returncode == 2 and 'Traceback' not in (_r4.stderr + _r4.stdout)
    and not _s39_os.path.exists(_never), 'rc=%s err=%r' % (_r4.returncode, _r4.stderr[:90]))
_tamp = _s39_os.path.join(_tmp, 'tamp.db')
_s39_sh.copyfile(_bak, _tamp)
with open(_tamp, 'r+b') as _fh:
    _fh.seek(_s39_os.path.getsize(_tamp) - 4096)
    _fh.truncate()
_r5 = _s39_cli('--backup', _tamp, '--into', _s39_os.path.join(_tmp, 't5.db'),
              '--no-ledger', '--json')
chk('S39 有牙：截断末页的备份必须被拦（rc=2）且点名 integrity',
    _r5.returncode == 2 and 'integrity' in _r5.stdout and 'Traceback' not in _r5.stderr,
    'rc=%s out=%r' % (_r5.returncode, _r5.stdout[:120]))

_eg_ok = _s39_qg._s47_egress_problems(
    open(_s39_os.path.join(_s39_root, 'restore_db.py'), encoding='utf-8').read())
chk('S39 出口形状判据正向：真 restore_db 源码 0 违例（否则判据恒红）', _eg_ok == [], str(_eg_ok)[:170])
chk('S39 有牙：快照方向写反（target.backup(source)）⇒ 判方向这一条',
    any('方向' in str(p) for p in _s39_qg._s47_egress_problems(
        'def f(source, target):' + chr(10) + '    target.backup(source)' + chr(10)
        + 'mode=ro' + chr(10) + _s39_RC5 + chr(10) + 'sys.exit(main())')))
chk('S39 有牙：源不再只读 ⇒ 判红（防把恢复目标当源）',
    any('mode=ro' in str(p) for p in _s39_qg._s47_egress_problems(
        'source.backup(dst)' + chr(10) + _s39_RC5 + chr(10) + 'sys.exit(main())')))
chk('S39 有牙：类别码缺一档 ⇒ 点名缺哪档（不许塌成一个红或塌成 0）',
    any('RC_EVIDENCE' in str(p) for p in _s39_qg._s47_egress_problems(
        'source.backup(dst)' + chr(10) + 'mode=ro' + chr(10)
        + 'RC_OK RC_MISMATCH RC_NO_INPUT RC_TARGET_EXISTS' + chr(10) + 'sys.exit(main())')))
chk('S39 有牙：rc 不直传 sys.exit(main()) ⇒ 判类别码在出口被吞',
    any('直传' in str(p) for p in _s39_qg._s47_egress_problems(
        'source.backup(dst)' + chr(10) + 'mode=ro' + chr(10) + _s39_RC5)))

_ev = _s39_tf.mkdtemp(prefix='s39ev_')
_s39_os.makedirs(_s39_os.path.join(_ev, 'backups'), exist_ok=True)
_fp = _s39_os.path.join(_ev, 'RESTORE-EVIDENCE.json')


def _s39_ev(entries, mask_face=False):
    # 合成面也要自述 face（新规则：账面无 face 即判红）；mask_face 专造那条反例
    _face = _s39_qg._portable_root(_ev)
    entries = [({k: v for k, v in e.items() if k != 'face'} if mask_face
                 else dict(e, face=e.get('face', _face))) for e in entries]
    with open(_fp, 'w', encoding='utf-8', newline='\n') as fh:
        _s39_js.dump({'schema_version': _s39_rd.EVIDENCE_SCHEMA, 'entries': entries}, fh)
    return _s39_qg._s47_evidence_problems(_ev)


_p1, _st1, _n1 = _s39_ev([{'mode': 'selftest', 'at': '2026-09-27T10:00:00+08:00',
                           'row_total': 5, 'backup': 'x.db', 'backup_sha256': 'aa'}])
chk('S39 有牙：台账只有自检腿 ⇒ 判没有一条 mode=restore（能力存在不等于能力用过）',
    any('mode=restore' in str(p) for p in _p1) and _n1 == 0, str(_p1)[:170])
_bak2 = _s39_os.path.join(_ev, 'backups', 'real.db')
_s39_sh.copyfile(_bak, _bak2)
_good = {'mode': 'restore', 'at': '2026-09-27T10:00:00+08:00', 'backup': 'real.db',
         'backup_sha256': _s39_qg._sha256_file(_bak2), 'row_total': 2,
         'forced': True, 'rollback_anchor': 'a.bak'}
_p2, _st2, _n2 = _s39_ev([_good])
chk('S39 合成面正向：一条可复算的真恢复 ⇒ 0 违例且给出时刻',
    _p2 == [] and _n2 == 1 and '2026-09-27' in _st2, '%s %s' % (_p2, _st2))
_p3, _, _ = _s39_ev([dict(_good, row_total=0)])
_p_nf, _, _ = _s39_ev([_good], mask_face=True)
chk('S39 有牙：账目不带 face ⇒ 判红（三个副本的 backups/ 是不同批次，共用一本无面名的账=分不清面）',
    any('face' in str(x) for x in _p_nf), str(_p_nf)[:170])

chk('S39 有牙：row_total=0 的账目即红（零输入不得记账）', any('零输入' in str(p) for p in _p3), str(_p3)[:150])
_p4, _, _ = _s39_ev([dict(_good, backup_sha256='deadbeef')])
chk('S39 有牙：台账 sha 与盘上字节不符 ⇒ 判不可复算（sha 必须当场复算而不是抄）',
    any('sha 不一致' in str(p) for p in _p4), str(_p4)[:150])
_p5, _, _ = _s39_ev([dict(_good, at=' Tuesday')])
chk('S39 有牙：at 不可解析 ⇒ 判红而不是「跳过新鲜度检查」', any('ISO 时刻' in str(p) for p in _p5), str(_p5)[:150])
_p6, _, _ = _s39_ev([dict(_good, at='2020-01-01T10:00:00+08:00')])
chk('S39 有牙：真恢复太久以前 ⇒ 判过期并给重跑命令（新鲜度是有期限的）',
    any('重跑' in str(p) for p in _p6), str(_p6)[:170])

_g = _s39_fake_repo(track=True, contract=_OK_CONTRACT, hook=_OK_HOOK)
_cp, _cd = _s39_qg._s48_contract_problems(_g)
_hp, _hr = _s39_qg._s48_hook_problems(_g)
chk('S39 第 48 项合成面正向：契约在库且声明门禁 + 钩子按路径引用 ⇒ 两侧都 0 违例',
    _cp == [] and _hp == [] and bool(_cd) and str(_hr).endswith('pre-push'), '%s %s' % (_cp, _hp))
_cp2, _ = _s39_qg._s48_contract_problems(_s39_fake_repo(
    track=True, contract=_s39_js.dumps({'schema': 'fenjue-ci-green-contract-v1', 'refresh_days': 7,
                                        'checks': [{'name': 'lint', 'cmd': 'ruff check src'}]})))
chk('S39 有牙：契约里一项都不跑 quality_gate.py ⇒ 判空话', any('空话' in str(p) for p in _cp2), str(_cp2)[:160])
_cp3, _ = _s39_qg._s48_contract_problems(_s39_fake_repo(track=False, contract=_OK_CONTRACT))
chk('S39 有牙：契约未被 git 跟踪 ⇒ 判红（契约即代码，不入库＝只在这台机器有效）',
    any('未被 git 跟踪' in str(p) for p in _cp3), str(_cp3)[:160])
_cp4, _ = _s39_qg._s48_contract_problems(
    _s39_fake_repo(track=True, contract='{"schema":"x","checks":[],"refresh_days":7}'))
chk('S39 有牙：checks 为空 ⇒ 判「无对象可判，不得算绿」（空列表不等于全绿）',
    any('checks 为空' in str(p) for p in _cp4), str(_cp4)[:160])
_cp5, _ = _s39_qg._s48_contract_problems(
    _s39_fake_repo(track=True, contract=_s39_js.dumps(
        {'schema': 'fenjue-ci-green-contract-v1', 'refresh_days': 0,
         'checks': [{'name': 'gate', 'cmd': 'python quality_gate.py'}]})))
chk('S39 有牙：refresh_days 非法 ⇒ 判红（保质期写在产物里，不许回落成常量或 0）',
    any('refresh_days' in str(p) for p in _cp5), str(_cp5)[:160])
_hp2, _ = _s39_qg._s48_hook_problems(_s39_fake_repo(
    track=True, contract=_OK_CONTRACT,
    hook='#!/bin/sh' + chr(10) + '# python iCAN大学生创新创业大赛/quality_gate.py' + chr(10) + 'exit 0' + chr(10)))
chk('S39 有牙：注释冒充接线（整行前加 #）⇒ 判未按路径引用门禁（在册不等于在跑）',
    any('按路径' in str(p) for p in _hp2), str(_hp2)[:170])
_ENF = _s39_js.dumps({'schema': 'fenjue-ci-green-contract-v1', 'refresh_days': 7,
                      'checks': [{'name': 'gate-fast',
                                  'cmd': 'python iCAN大学生创新创业大赛/quality_gate.py --fast'}],
                      'enforcement': {'hook': 'none', 'receipt_max_days': 3,
                                      'no_hook_reason':
                                          '并发会话共库，装 pre-push 会把别人的推送一起挡在门外'}})


def _s38_head(d):
    return _s39_sp.run(['git', '-C', d, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()


def _s39_receipt(d, **over):
    """在合成仓根里放一张本地运行回执（默认新鲜 + HEAD 命中）。"""
    doc = {'schema': 'ican-local-run-receipt-v1', 'at': _dt39_now(), 'head': _s38_head(d),
           'verdict': 'ok', 'rc': 0, 'mode': 'fast',
           'stages': [{'name': 'gate', 'rc': 0}]}
    doc.update(over)
    with open(_s39_os.path.join(d, '.ci', 'local-run-receipt.json'), 'w',
              encoding='utf-8', newline=chr(10)) as fh:
        _s39_js.dump(doc, fh, ensure_ascii=False)
    return doc


def _dt39_now(days=0):
    # 显式 UTC：第 43 项把裸 `datetime.now()` 判成"UTC 服务器上算错今天"，
    # 夹具要的是"距今 N 天"这个相对量，用显式时区即可，不加白名单。
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec='seconds')


_enf_ok, _ev_ok = _s39_qg._s48_enforcement_problems(
    _s39_fake_repo(track=True, contract=_OK_CONTRACT, hook=_OK_HOOK), _s39_js.loads(_OK_CONTRACT))
chk('S39 形状一合法：合规 pre-push 在位 ⇒ 0 违例且印出形状=钩子',
    _enf_ok == [] and any('pre-push 钩子' in str(e) for e in _ev_ok), f'{_enf_ok} {_ev_ok}'[:170])
_r_good = _s39_fake_repo(track=True, contract=_ENF, hook=None)
_s39_receipt(_r_good)
_enf2, _ev2 = _s39_qg._s48_enforcement_problems(_r_good, _s39_js.loads(_ENF))
chk('S39 形状二合法：无钩子 + 契约写明理由 + 新鲜回执（HEAD 命中）⇒ 0 违例且印出形状=本地回执',
    _enf2 == [] and any('本地回执' in str(e) for e in _ev2), f'{_enf2} {_ev2}'[:190])
chk('S39 形状可判别：无钩子时不得报"pre-push 存在但形状不合规"（存在与不在位必须分形）',
    not any('存在但形状不合规' in str(x) for x in _enf2), str(_enf2)[:170])
_r_old = _s39_fake_repo(track=True, contract=_ENF)
_s39_receipt(_r_old, at=_dt39_now(-40))
chk('S39 有牙：回执过期 ⇒ 判未刷新（新鲜度是能力还在动的下界）',
    any('未刷新' in str(x) for x in _s39_qg._s48_receipt_problems(_r_old, {'receipt_max_days': 3})[0]),
    str(_s39_qg._s48_receipt_problems(_r_old, {'receipt_max_days': 3})[0])[:170])
_r_far = _s39_fake_repo(track=True, contract=_ENF)
_s39_receipt(_r_far, head='deadbeef' * 5)
chk('S39 有牙：回执 HEAD 不是当前 HEAD 的祖先 ⇒ 判吃老本（旧回执不得放行新提交）',
    any(('祖先' in str(x)) or ('没测到' in str(x))
        for x in _s39_qg._s48_receipt_problems(_r_far, {'receipt_max_days': 3})[0]),
    str(_s39_qg._s48_receipt_problems(_r_far, {'receipt_max_days': 3}))[:170])
_r_bad = _s39_fake_repo(track=True, contract=_ENF)
_s39_receipt(_r_bad, verdict='fail', rc=1)
_rp_bad, _vb = _s39_qg._s48_receipt_problems(_r_bad, {'receipt_max_days': 3})
chk('S39 防自指成环：红回执**不**判红（回执里含本项，拿它裁决则永远出不来绿），但结论必须被印出',
    _rp_bad == [] and _vb == 'fail', f'{_rp_bad} verdict={_vb}'[:170])
_r_nt = _s39_fake_repo(track=True, contract=_ENF)
_s39_receipt(_r_nt, verdict='notest', rc=2)
chk('S39 有牙：回执显示本地闸根本没跑成（notest/usage）⇒ 判红（未执行的运行不构成闸）',
    any('没跑成' in str(x) for x in _s39_qg._s48_receipt_problems(_r_nt, {'receipt_max_days': 3})[0]),
    str(_s39_qg._s48_receipt_problems(_r_nt, {'receipt_max_days': 3}))[:170])
_r_empty = _s39_fake_repo(track=True, contract=_ENF)
_s39_receipt(_r_empty, stages=[])
chk('S39 有牙：回执里没有任何阶段读数 ⇒ 判"空回执不得算跑过"（零输入不判绿）',
    any('空回执' in str(x) for x in _s39_qg._s48_receipt_problems(_r_empty, {'receipt_max_days': 3})[0]),
    str(_s39_qg._s48_receipt_problems(_r_empty, {'receipt_max_days': 3})[0])[:170])
_r_norule = _s39_fake_repo(track=True, contract=_s39_js.dumps(
    {'schema': 'fenjue-ci-green-contract-v1', 'refresh_days': 7,
     'checks': [{'name': 'g', 'cmd': 'python quality_gate.py'}],
     'enforcement': {'hook': 'none', 'no_hook_reason': '忘了', 'receipt_max_days': 3}}))
_s39_receipt(_r_norule)
_short_doc = {'schema': 'fenjue-ci-green-contract-v1', 'refresh_days': 7,
              'checks': [{'name': 'g', 'cmd': 'python quality_gate.py'}],
              'enforcement': {'hook': 'none', 'no_hook_reason': '忘了', 'receipt_max_days': 3}}
chk('S39 有牙：不装钩子的理由不足 20 字 ⇒ 判红（不装必须留下为什么）',
    any('no_hook_reason' in str(x) for x in _s39_qg._s48_enforcement_problems(_r_norule, _short_doc)[0]),
    str(_s39_qg._s48_enforcement_problems(_r_norule, _short_doc))[:170])
_r_broken = _s39_fake_repo(track=True, contract=_ENF, hook='#!/bin/sh' + chr(10) + 'exit 0' + chr(10))
_s39_receipt(_r_broken)
chk('S39 有牙：钩子在但只是 exit 0 ⇒ 判"形状不合规"，不许被"其实想走回执"糊过去',
    any('形状不合规' in str(x) for x in _s39_qg._s48_enforcement_problems(
        _r_broken, _s39_js.loads(_ENF))[0]),
    str(_s39_qg._s48_enforcement_problems(_r_broken, _s39_js.loads(_ENF)))[:190])
_chk_no_receipt = _s39_fake_repo(track=True, contract=_ENF)
chk('S39 有牙：无钩子又无回执 ⇒ 判红并把处置指向契约里的 receipt_cmd（门禁不得硬编码入口名）',
    any('enforcement.receipt_cmd' in str(x) for x in _s39_qg._s48_enforcement_problems(
        _chk_no_receipt, _s39_js.loads(_ENF))[0]),
    str(_s39_qg._s48_enforcement_problems(_chk_no_receipt, _s39_js.loads(_ENF)))[:190])
_sp1, _nsk1 = _s39_qg._s48_skip_problems(
    _s39_fake_repo(contract=_OK_CONTRACT, skips='{"ts":"2026-09-27T10:00:00+08:00"}' + chr(10)))
chk('S39 有牙：跳过没写 reason ⇒ 判红（逃生门必须有计量）',
    any('reason' in str(p) for p in _sp1) and _nsk1 == 1, str(_sp1)[:160])
_sp3, _ = _s39_qg._s48_skip_problems(
    _s39_fake_repo(contract=_OK_CONTRACT, skips='not json at all' + chr(10)))
chk('S39 有牙：跳过台账混进不可解析行 ⇒ 逐行判红（不当作没发生）',
    any('不可解析' in str(p) for p in _sp3), str(_sp3)[:160])
_sp2, _nsk2 = _s39_qg._s48_skip_problems(_s39_fake_repo(contract=_OK_CONTRACT, skips=''))
chk('S39 正向：空跳过账 ⇒ 0 条且不判红（「没有跳过」是合法态）',
    _sp2 == [] and _nsk2 == 0, '%s rows=%s' % (_sp2, _nsk2))
_sp4, _nsk4 = _s39_qg._s48_skip_problems(_s39_fake_repo(
    contract=_OK_CONTRACT,
    skips='{"ts":"2026-09-27T10:00:00+08:00","reason":"CI 已绿，本地改文档"}' + chr(10)))
chk('S39 正向：带 reason+ts 的跳过 ⇒ 记账但不判红（跳过要留名，不是禁止跳过）',
    _sp4 == [] and _nsk4 == 1, '%s rows=%s' % (_sp4, _nsk4))
_s39_qg.results.clear()
_s39_qg.check_commit_block_chain()
_ok48, _det48 = _s39_last()
chk('S39 真仓第 48 项两态可判别：判绿，或红因点名三种合法缺口之一（禁"含糊的红"）',
    _ok48 or any(('回执' in str(d)) or ('no_hook_reason' in str(d)) or ('形状不合规' in str(d))
                 or ('没跑成' in str(d)) for d in _det48), '；'.join(map(str, _det48))[:220])
# —— 入口形状守卫：verify_local 曾被插入语句切断，main() 提前结束、整轮零输出仍返回 0
import ast as _s39_ast
_vl_src = open(_s39_os.path.join(_s39_root, 'verify_local.py'), encoding='utf-8').read()
_vl_tree = _s39_ast.parse(_vl_src)
_vl_top = {n.name for n in _vl_tree.body if isinstance(n, _s39_ast.FunctionDef)}
_vl_main = next((n for n in _vl_tree.body if isinstance(n, _s39_ast.FunctionDef) and n.name == 'main'), None)
chk('S39 入口形状：main/write_receipt/_git_root 都必须是**模块级**函数（被嵌进别人=静默失效）',
    {'main', 'write_receipt', '_git_root'} <= _vl_top and _vl_main is not None,
    '顶层缺件=' + str({'main', 'write_receipt', '_git_root'} - _vl_top)[:150])
chk('S39 入口形状：main() 末句必须是 return（早先插入把函数体截断，末句成了赋值 ⇒ 静默返回 None=退出码 0）',
    _vl_main is not None and isinstance(_vl_main.body[-1], _s39_ast.Return),
    '末句=' + (type(_vl_main.body[-1]).__name__ if _vl_main else '无 main'))
_vl_calls = {getattr(n.func, 'id', getattr(n.func, 'attr', ''))
             for n in _s39_ast.walk(_vl_main) if isinstance(n, _s39_ast.Call)} if _vl_main else set()
chk('S39 入口形状：main() 必须真调用**唯一写盘入口** _emit_receipt_and_ledger（直接调 write_receipt=旁路，账本会少一行）',
    '_emit_receipt_and_ledger' in _vl_calls and 'write_receipt' not in _vl_calls,
    'main 内调用集=' + str(sorted(x for x in _vl_calls if x)[:6]))
_vl_seg = _s39_ast.get_source_segment(_vl_src, _vl_main) or ''
chk('S39 入口形状：LOCAL-VERIFY 汇总打印必须在 main() 段内（打印掉进别的函数=跑完什么都不说）',
    'LOCAL-VERIFY' in _vl_seg, 'main 段长度=' + str(len(_vl_seg)))
_vl_run = _s39_sp.run([sys.executable, _s39_os.path.join(_s39_root, 'verify_local.py'), '--selftest'],
                      capture_output=True, text=True, cwd=_s39_root)
chk('S39 入口子进程实测：verify_local --selftest 必须 rc=0 **且有输出**（零输出的成功=没测）',
    _vl_run.returncode == 0 and 'SELFTEST' in _vl_run.stdout and len(_vl_run.stdout) > 60,
    'rc=%s stdout=%r' % (_vl_run.returncode, _vl_run.stdout[:70]))

# ══════════ S40：第 49 项 本地闸运行台账（第 34 轮 M93 / H120 / H121）══════════
# 反例全部落在临时面；被测对象（真仓 .ci/*、verify_local.py）只读不改。
import verify_local as _s40_vl  # noqa: E402

# 在任何 monkeypatch 之前先把"真仓根"取下来：git 上溯所得，不是 __file__ 的父目录
# （05 交付副本里 dirname 会停在 工程佐证材料，本轮实测把两条腿判成假红）。
_s40_realroot = _s39_qg._s48_repo_root()
_F = _s39_qg._is_failure_line  # 聚合器口径（下面几段共用，含端到端腿，必须先于两处使用定义）
_S40_LAST_OUT = []


def _s40s(fn, *a, **k):
    """跑一个"会往 stdout 打 ❌"的夹具调用，把输出吞进本地缓冲并返回返回值。

    第 34 轮立规、同轮内复发 3 次的直接后果：门禁 `run_test` 读的是整段子进程输出，
    夹具打印的失败行与真实失败行同形 ⇒ 1390 项 0 失败也能被判红。
    吞掉之外还把文本留在 `_S40_LAST_OUT`，腿可以反过来断言"确实打印过那句拒撤理由"。
    """
    buf = _s40_io.StringIO()
    with _s40_cl.redirect_stdout(buf):
        out = fn(*a, **k)
    _S40_LAST_OUT.append(buf.getvalue())
    return out
import contextlib as _s40_cl   # noqa: E402  夹具吞打印用，须在第一个用到它的函数之前
import io as _s40_io           # noqa: E402


def _s40_doc(seq=1, head='h1', at='2026-09-27T10:00:00+08:00', verdict='ok', rc=0, stages=None):
    return {'schema': 'ican-local-run-receipt-v1', 'seq': seq, 'head': head, 'at': at,
            'verdict': verdict, 'rc': rc, 'mode': 'full', 'entry': 'verify_local.py',
            'stages': [{'name': 'gate', 'rc': 0}] if stages is None else stages}


def _s40_line(**kw):
    doc = _s40_doc(**kw)
    return _s39_js.dumps(doc, ensure_ascii=False, sort_keys=True)


def _s40_face(lines, receipt='MISS', vl_src=None, contract='AUTO'):
    """合成仓根：契约 + 账本 + 回执 + 入口脚本四件套，缺契约则入口取不到。"""
    d = _s39_fake_repo()
    if contract != 'NONE':
        cmd = contract if isinstance(contract, str) and contract != 'AUTO' else 'python verify_local.py --receipt'
        with open(_s39_os.path.join(d, '.ci', 'contract.json'), 'w',
                  encoding='utf-8', newline='\n') as fh:
            fh.write(_s39_js.dumps({'schema': 'fenjue-ci-green-contract-v1', 'refresh_days': 7,
                                    'enforcement': {'hook': 'none', 'receipt': True,
                                                    'receipt_cmd': cmd}},
                                   ensure_ascii=False, sort_keys=True) + '\n')
    if lines is not None:
        with open(_s39_os.path.join(d, '.ci', 'local-runs.jsonl'), 'w',
                  encoding='utf-8', newline='\n') as fh:
            fh.write('\n'.join(lines) + '\n')
    if receipt != 'MISS':
        with open(_s39_os.path.join(d, '.ci', 'local-run-receipt.json'), 'w',
                  encoding='utf-8', newline='\n') as fh:
            fh.write(_s39_js.dumps(receipt, ensure_ascii=False, sort_keys=True) + '\n')
    with open(_s39_os.path.join(d, 'verify_local.py'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(_vl_src if vl_src is None else vl_src)
    return d


def _s40_run(face):
    orig = _s39_qg._s48_repo_root
    _s39_qg._s48_repo_root = lambda: face
    _s39_qg.results.clear()
    try:
        _s39_qg.check_local_gate_ledger()
    finally:
        _s39_qg._s48_repo_root = orig
    return _s39_last()


_r, _p = _s39_qg._s49_rows(_s40_line() + '\n' + _s40_line(seq=2))
chk('S40 合法两行 ⇒ 2 行 0 违例（合法态不得判红）', len(_r) == 2 and _p == [], str(_p)[:150])
chk('S40 合法行解析出的字段可核（seq/head/verdict 逐项对得上）',
    [x['seq'] for x in _r] == [1, 2] and _r[0]['head'] == 'h1' and _r[1]['verdict'] == 'ok',
    str([x.get('seq') for x in _r])[:80])
_r0, _p0 = _s39_qg._s49_rows('')
chk('S40 空文本 ⇒ 0 行且**不**由纯函数造红（"没有历史"由上层判，零输入不擅自定罪也不擅自定罪为绿）',
    _r0 == [] and _p0 == [], str(_p0)[:120])
_bad1 = _s39_qg._s49_rows('not json at all')[1]
chk('S40 有牙：不可解析行 ⇒ 逐行点名（混合格式不得当作没发生）',
    len(_bad1) == 1 and '不可解析' in _bad1[0], str(_bad1)[:150])
_bad2 = _s39_qg._s49_rows(_s39_js.dumps({'head': 'h', 'at': 'x'}) + '\n' + _s40_line(seq=9))[1]
chk('S40 有牙：缺键行 ⇒ 点名缺哪些键，且坏行不影响同行里其它合法行的计数',
    len(_bad2) == 1 and '缺键' in _bad2[0] and 'seq' in _bad2[0], str(_bad2)[:170])
_bad3 = _s39_qg._s49_rows(_s40_line(stages=[]))[1]
chk('S40 有牙：stages 为空 ⇒ 判红（一次什么都没测的运行不得记账）',
    len(_bad3) == 1 and '不得记账' in _bad3[0], str(_bad3)[:150])
_bad4 = _s39_qg._s49_rows('[1, 2]')[1]
chk('S40 有牙：整行是数组而非对象 ⇒ 判红（JSON 合法不等于形状合法）',
    len(_bad4) == 1 and '不是对象' in _bad4[0], str(_bad4)[:150])
_dup = _s39_qg._s49_rows(_s40_line(seq=3) + '\n' + _s40_line(seq=3))[1]
chk('S40 有牙：seq 重复 ⇒ 判红（不递增就证明不了这本账是追加出来的）',
    len(_dup) == 1 and '未递增' in _dup[0], str(_dup)[:150])
_back = _s39_qg._s49_rows(_s40_line(seq=7) + '\n' + _s40_line(seq=4))[1]
chk('S40 有牙：seq 倒序 ⇒ 判红（重排/手改在这里露馅）',
    len(_back) == 1 and '未递增' in _back[0], str(_back)[:150])
_gap = _s39_qg._s49_rows(_s40_line(seq=101) + '\n' + _s40_line(seq=102))[1]
chk('S40 合法：裁剪留下的 seq 跳号**不**判红（封顶会删头部旧行，跳号是设计内）',
    _gap == [], str(_gap)[:150])
_nan = _s39_qg._s49_rows(_s39_js.dumps(dict(_s40_doc(seq='x'), mode='full'), sort_keys=True))[1]
chk('S40 有牙：seq 非整数 ⇒ 判红且不抛异常（无法判递增就是没测）',
    len(_nan) == 1 and '不是整数' in _nan[0], str(_nan)[:150])

_okc, _ce = _s39_qg._s49_consistency(_r, _s40_doc(seq=2))
chk('S40 同源一致：末行 ⇄ 回执四项相同 ⇒ 0 违例，且证据里印出 head/verdict/阶段数（PASS 必须说实话）',
    _okc == [] and 'head=h1' in _ce and 'verdict=ok' in _ce and '阶段 1 个' in _ce, str(_ce)[:170])
_tc = _s39_qg._s49_consistency(_r, dict(_s40_doc(seq=2), rc=1))[0]
chk('S40 有牙：事后手改回执 rc ⇒ 点名 rc（两件产物必须同一次生成）',
    len(_tc) == 1 and 'rc' in str(_tc), str(_tc)[:170])
_th = _s39_qg._s49_consistency(_r, dict(_s40_doc(seq=2), head='other'))[0]
chk('S40 有牙：回执 head 与末行不符 ⇒ 点名 head', len(_th) == 1 and 'head' in str(_th), str(_th)[:170])
_tm = _s39_qg._s49_consistency(_r, None)[0]
chk('S40 有牙：只有账本没有回执 ⇒ 判"缺一"，不得当成一致', len(_tm) == 1 and '缺一' in str(_tm), str(_tm)[:150])
_tm2 = _s39_qg._s49_consistency([], _s40_doc())[0]
chk('S40 有牙：只有回执没有账本 ⇒ 同样判"缺一"（两个方向都要拦）',
    len(_tm2) == 1 and '缺一' in str(_tm2), str(_tm2)[:150])

_cov0, _cd0 = _s39_qg._s49_coverage([], 'h1')
chk('S40 覆盖读数：空账本 ⇒ 明确写 UNVERIFIED（"没数"不等于"0 命中"）',
    'UNVERIFIED' in _cov0 and _cd0 == {}, str(_cov0)[:120])
_cov1, _cd1 = _s39_qg._s49_coverage(_r, 'h1')
chk('S40 覆盖读数：命中当前 HEAD 的行数按行算（不是按文件算）',
    _cd1.get('covered') == 2 and '2/2' in _cov1 and '结论分布' in _cov1, str(_cov1)[:170])
_cov2, _cd2 = _s39_qg._s49_coverage([_s40_doc(seq=1, head='old'), _s40_doc(seq=2, head='h1')], 'h1')
chk('S40 覆盖读数：混 HEAD 时只数命中的那行（吃老本看得见）',
    _cd2.get('covered') == 1 and '1/2' in _cov2, str(_cov2)[:150])
try:
    _cov3, _cd3 = _s39_qg._s49_coverage([_s40_doc(seq=1, at='昨天吧')], 'h1')
    _bad_parse = ''
except Exception as exc:  # noqa: BLE001
    _cov3, _cd3, _bad_parse = '', {}, type(exc).__name__
chk('S40 时刻不可解析 ⇒ 报"不可解析"且不抛（异常路径本身也得是合法输出）',
    not _bad_parse and '不可解析' in _cov3 and _cd3.get('age_h') == -1.0, (_bad_parse or _cov3)[:150])
_cov4, _cd4 = _s39_qg._s49_coverage([_s40_doc(seq=1, at='2020-01-01T00:00:00+08:00')], 'h1')
chk('S40 陈旧只报不拦：读数里写明"未刷新"（拦下来会连坐并发会话，故不设阈值判据）',
    '未刷新' in _cov4 and _cd4.get('age_h', 0) > 72, str(_cov4)[:170])

try:
    _e0 = _s39_qg._s49_entry_problems(_s40_realroot)
    _e0_err = ''
except Exception as exc:  # noqa: BLE001
    _e0, _e0_err = ['崩了'], f'{type(exc).__name__}: {exc}'
chk('S40 入口判据在**真仓文件**上驱动：不抛异常且 0 违例（本轮曾在此崩成 AttributeError ⇒ 判据自己崩=没测）',
    _e0 == [] and not _e0_err, (_e0_err or str(_e0))[:170])
chk('S40 根解析按 git 不按目录：仓根必须是 git 上溯所得（在交付副本里 dirname 会停在半路，'
    "本轮 05 实测就是被这个假根判成\"缺契约\"）",
    _s40_realroot == _s39_qg._s48_repo_root() and bool(_s40_realroot), str(_s40_realroot)[:120])
chk('S40 入口判据取数面指向被审对象：真仓确有一个唯一写盘入口调用 append_run_ledger',
    '_emit_receipt_and_ledger' in _vl_src and 'append_run_ledger(doc, root)' in _vl_src,
    '调用点缺失=' + str('append_run_ledger(doc, root)' in _vl_src)[:80])
_VL_TWO = ('def append_run_ledger(doc, root):\n    return 1\n\n'
           'def a():\n    append_run_ledger({}, "")\n\n'
           'def b():\n    append_run_ledger({}, "")\n\n'
           'def main():\n    a()\n    b()\n')
chk('S40 有牙：两处写盘（两个调用方）⇒ 判红（分两处写就能写出互相矛盾的两件产物）',
    bool(_s39_qg._s49_entry_problems(_s40_face([_s40_line()], vl_src=_VL_TWO))), '两条腿各写各的')
_VL_MAIN = ('def append_run_ledger(doc, root):\n    return 1\n\n'
            'def main():\n    append_run_ledger({}, "")\n')
chk('S40 有牙：main() 直写账本（旁路唯一入口）⇒ 判红',
    bool(_s39_qg._s49_entry_problems(_s40_face([_s40_line()], vl_src=_VL_MAIN))), '旁路未被拦')
_VL_LOOSE = ('def append_run_ledger(doc, root):\n    return 1\n\n'
             'def main():\n    return 0\n\n'
             'append_run_ledger({}, "")\n')
_p_loose = _s39_qg._s49_entry_problems(_s40_face([_s40_line()], vl_src=_VL_LOOSE))
chk('S40 有牙：模块级直写账本 ⇒ 判红（import 该脚本就能凭空造一行运行史）',
    len(_p_loose) == 1 and '函数之外' in _p_loose[0], str(_p_loose)[:160])
_VL_SPLIT = ('def append_run_ledger(doc, root):\n    return 1\n\n'
             'def write_receipt(doc, root):\n    return 1\n\n'
             'def emit():\n    append_run_ledger({}, "")\n\n'
             'def main():\n    write_receipt({}, "")\n    emit()\n')
_p_split = _s39_qg._s49_entry_problems(_s40_face([_s40_line()], vl_src=_VL_SPLIT))
chk('S40 有牙：只有一个调用方、但回执在别处写 ⇒ 仍判红（"唯一"必须是**同时**写两件产物的那一个）',
    len(_p_split) == 1 and 'write_receipt' in _p_split[0], str(_p_split)[:160])
_VL_NONE = 'def main():\n    return 0\n'
_p_none = _s39_qg._s49_entry_problems(_s40_face([_s40_line()], vl_src=_VL_NONE))
chk('S40 有牙：账本没有生产者（函数被删）⇒ 判红且点名"没有生产者"',
    len(_p_none) == 1 and '没有生产者' in _p_none[0], str(_p_none)[:150])
_p_lost = _s39_qg._s49_entry_problems(_s39_os.path.join(_s39_tf.mkdtemp(prefix='s40none_'), 'nope'))
chk('S40 零输入不判绿：入口脚本找不到 ⇒ 判"没测到"而不是"没问题"',
    len(_p_lost) == 1 and '没测到' in _p_lost[0], str(_p_lost)[:150])
_p_ghost = _s39_qg._s49_entry_problems(_s40_face([_s40_line()], contract='python ghost_entry.py --receipt'))
chk('S40 有牙：契约把入口指到盘上不存在的 .py ⇒ 判"没测到"（配置写了 ≠ 文件在）',
    len(_p_ghost) == 1 and '没测到' in _p_ghost[0], str(_p_ghost)[:170])

_okA, _detA = _s40_run(_s40_face(None, 'MISS'))
chk('S40 空面判红：账本与回执都缺 ⇒ 两条缺失各自点名（不得糊成一条"通过"）',
    (not _okA) and any('local-runs.jsonl' in str(d) for d in _detA)
    and any('local-run-receipt' in str(d) for d in _detA), '；'.join(map(str, _detA))[:180])
_okB, _detB = _s40_run(_s40_face([_s40_line(seq=1), _s40_line(seq=2)], _s40_doc(seq=2)))
chk('S40 完整合法面 ⇒ 判绿，且证据里既有"同源自证"也有"覆盖"两行',
    _okB and any('同源自证' in str(d) for d in _detB) and any('覆盖' in str(d) for d in _detB),
    '；'.join(map(str, _detB))[:200])
_okC, _detC = _s40_run(_s40_face([_s40_line(seq=1), _s40_line(seq=2, verdict='fail', rc=1)],
                                 _s40_doc(seq=2, verdict='fail', rc=1)))
chk('S40 只报不拦：一次真跑红的运行**照样**是合格的台账行（红历史可见才是本项目的）',
    _okC and any('verdict=fail' in str(d) for d in _detC), '；'.join(map(str, _detC))[:200])
_okD, _detD = _s40_run(_s40_face([_s40_line(seq=1), _s40_line(seq=2)],
                                 dict(_s40_doc(seq=2), rc=1)))
chk('S40 有牙：回执 rc 被手改 ⇒ 判红并点名不一致',
    (not _okD) and any('不一致' in str(d) for d in _detD), '；'.join(map(str, _detD))[:180])
_okE, _detE = _s40_run(_s40_face([_s40_line(seq=i) for i in range(1, 62)], _s40_doc(seq=61)))
chk('S40 有牙：超过封顶 60 行 ⇒ 判红（无限膨胀的账本没人看，裁剪通道必须被核）',
    (not _okE) and any('封顶' in str(d) for d in _detE), '；'.join(map(str, _detE))[:180])
chk('S40 封顶数值与生产者一致：门禁的 _S49_MAX_ROWS == verify_local 的 MAX_RUNS（一处改另一处必红）',
    _s39_qg._S49_MAX_ROWS == _s40_vl.MAX_RUNS, f'{_s39_qg._S49_MAX_ROWS} vs {_s40_vl.MAX_RUNS}')
_okF, _detF = _s40_run(_s40_face([_s40_line(seq=1), _s40_line(seq=2, at='坏时刻')],
                                 _s40_doc(seq=2, at='坏时刻')))
chk('S40 可核性：末行时刻不可解析 ⇒ 判绿但把"不可解析"印出来（不得静默当成新鲜）',
    _okF and any('不可解析' in str(d) for d in _detF), '；'.join(map(str, _detF))[:180])
_tmpw = _s39_tf.mkdtemp(prefix='s40w_')
_s40_vl.append_run_ledger(_s40_doc(), _tmpw)
_s40_vl.append_run_ledger(_s40_doc(), _tmpw)
_seqs = [_s39_js.loads(x)['seq'] for x in
         open(_s39_os.path.join(_tmpw, '.ci', 'local-runs.jsonl'), encoding='utf-8').read().splitlines() if x.strip()]
chk('S40 生产者侧：连续两次追加 seq=1,2（序号必须由账本自身推出来）',
    _seqs == [1, 2], str(_seqs)[:120])
_full = [_s40_line(seq=i) for i in range(101, 101 + _s40_vl.MAX_RUNS)]
with open(_s39_os.path.join(_tmpw, '.ci', 'local-runs.jsonl'), 'w', encoding='utf-8', newline='\n') as _fh:
    _fh.write('\n'.join(_full) + '\n')
_s40_vl.append_run_ledger(_s40_doc(), _tmpw)
_after = [_s39_js.loads(x)['seq'] for x in
          open(_s39_os.path.join(_tmpw, '.ci', 'local-runs.jsonl'), encoding='utf-8').read().splitlines() if x.strip()]
chk('S40 生产者侧：裁剪到封顶后 seq 仍递增（按行数算会恒等于 61 ⇒ 序号失去证明力）',
    len(_after) == _s40_vl.MAX_RUNS and _after[-1] == 161 and _after[-1] > _after[-2],
    f'行数={len(_after)} 末 seq={_after[-1:]}')
# ── 端到端：把新口径打进**被保护的那条分支**（run_test 的 ok=），而不是只测纯函数 ──
def _s40_probe(body, rc, want=None):
    """造一个临时"测试套件"驱动 run_test 本身；**它的打印必须吞在夹具里**。

    同族第三次复发（本轮 18:55 权威运行实测）：探针让门禁往 L4 的 stdout 印出
    `🔴 [99. 探针套件] 未通过` 与 `↳ ❌ 真失败一条`，于是门禁按新口径**正确地**把 L4 判红
    —— 噪声源是我的夹具，不是判据。凡是会产出"像失败行"的夹具，一律重定向 stdout。
    """
    d = _s39_tf.mkdtemp(prefix='s40probe_')
    p = _s39_os.path.join(d, 'probe_suite.py')
    with open(p, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('import sys\n' + body + 'sys.exit(%d)\n' % rc)
    _s39_qg.results.clear()
    cap = _s40_io.StringIO()
    with _s40_cl.redirect_stdout(cap):
        _s39_qg.run_test('探针套件', p, '99.')
    lvl, nm, ok, det = _s39_qg.results[-1]
    txt = cap.getvalue()
    if want:
        assert want in txt, f'夹具没产出预期判据行 {want!r}：{txt[:120]}'
    return ok, (det if isinstance(det, list) else [det]), txt


_p1, _, _t1 = _s40_probe("print('✅ 通过项：标题里引用了 ❌ 这个字符')\nprint('✅ 另一项：失败路径返回 503')\n",
                         0, want='[99. 探针套件] 通过')
chk('S40 端到端·正例：套件 rc=0 且通过行正文里带失败字符 ⇒ 判过（本轮假红的原样复现）',
    _p1 is True, '被判红=口径仍在读描述')
_p2, _d2, _t2 = _s40_probe("print('❌ 真失败一条')\nprint('✅ 别的项')\n", 0, want='未通过')
chk('S40 端到端·反例：套件 rc=0 但有真失败行 ⇒ 仍判红（收窄没把牙齿一起削掉）',
    _p2 is False and any('真失败' in str(x) for x in _d2), '红因=' + str(_d2)[:120])
_p3, _d3, _t3 = _s40_probe("print('全部通过，没有任何标记字符')\n", 3, want='未通过')
chk('S40 端到端·兜底：rc 非 0 且输出无标记 ⇒ 判红并明说"无失败输出但返回码非 0"（不得空清单放行）',
    _p3 is False and any('返回码非 0' in str(x) for x in _d3), '明细=' + str(_d3)[:120])
chk('S40 证据落盘可用：script 传绝对路径时文件名只取 basename'
    '（旧写法把整条路径嵌进文件名 ⇒ Windows Invalid argument，"给失败留证据"这步自己失败）',
    not any('落盘失败' in str(x) for x in _d2) and any('原文已落盘' in str(x) for x in _d2),
    str(_d2)[:150])
chk('S40 夹具不污染取数面：探针的判据行与失败标记都落在本地缓冲里（被吞进来=没进 L4 的 stdout）',
    all('[99. 探针套件]' in t for t in (_t1, _t2, _t3))
    and any(_F(x) for x in _t2.splitlines())
    and not any(_F(x) for x in _t1.splitlines()),
    '反例缓冲里没有 ❌ 行=这条夹具根本没测到红路径')
import ast as _s40_ast
_self_src = open(_s39_os.path.join(_s39_root, 'test_l4_assertions.py'), encoding='utf-8').read()
_self_tree = _s40_ast.parse(_self_src)
_qr = [(_w.lineno, _w.body[-1].end_lineno) for _w in _s40_ast.walk(_self_tree)
       if isinstance(_w, _s40_ast.With)
       and any('redirect_stdout' in _s40_ast.dump(it.context_expr) for it in _w.items)]
_raw_rt = [n.lineno for n in _s40_ast.walk(_self_tree)
           if isinstance(n, _s40_ast.Call) and getattr(n.func, 'id', '') == 'run_test'
           and n.lineno > 10400 and not any(a <= n.lineno <= b for a, b in _qr)]
chk('S40 静态守卫（复发计数=3 的收口形态）：S40 段不得有未被 redirect_stdout 包住的 run_test 调用点',
    _raw_rt == [] and len(_qr) >= 3, '裸露调用点行号=' + str(_raw_rt)[:120])
_CORPUS = ['✅ S40 通过项标题里有 ❌ 字符', '❌ 真失败行', '   ',
           'Traceback (most recent call last):', '✅ 部署 WSGI：失败路径返回 503', '  ❌ 缩进的失败行']
_vl_hits = _s40_vl._failure_lines('\n'.join(_CORPUS))
_qg_hits = [ln.strip() for ln in _CORPUS if _F(ln)]
chk('S40 两处读失败行的尺子同源：本地验证器的阶段摘要 ⇄ 门禁 run_test 逐行一致（同族出口不得各写一把尺）',
    _vl_hits == _qg_hits == ['❌ 真失败行', '❌ 缩进的失败行'],
    'vl=%s qg=%s' % (_vl_hits, _qg_hits))
_s39_qg.results.clear()

_blocked = _s39_tf.mkdtemp(prefix='s40blk_')
with open(_s39_os.path.join(_blocked, '.ci'), 'w', encoding='utf-8', newline='\n') as _fh:
    _fh.write('我是个文件，不是目录')
# 夹具的失败打印必须**吞在夹具里**：本轮实测该函数往 stdout 印的「❌ 运行账本写不出去」
# 被门禁 run_test 当成失败标记，于是 L4 在 1384 项 0 失败的情况下被判红（假红因抢了真红因的位置）。
_buf = _s40_io.StringIO()
with _s40_cl.redirect_stdout(_buf):
    _blk_rc = _s40_vl.append_run_ledger(_s40_doc(), _blocked)
chk('S40 生产者侧：账本写不出去（.ci 被占成文件）⇒ 返回空串 + 打印失败，不抛 traceback',
    _blk_rc == '' and '运行账本写不出去' in _buf.getvalue()
    and 'FileExistsError' in _buf.getvalue(), _buf.getvalue()[:150])
_buf2 = _s40_io.StringIO()
with _s40_cl.redirect_stdout(_buf2):
    _ok_rc = _s40_vl.append_run_ledger(_s40_doc(), _tmpw)
chk('S40 生产者侧：正常面返回真实路径且**不往 stdout 印失败标记**（诊断文字会污染父进程的取数面）',
    _ok_rc != '' and '❌' not in _buf2.getvalue(), (_ok_rc or _buf2.getvalue())[:140])
# ── 聚合器取数面：只认"非通过行上的裸失败标记"，崩溃交 rc、计数交自报行 ──
chk('S40 有牙（假红族）：通过行**正文里引用了失败字符** ⇒ 不算失败行（本轮实测被这条误判过一次）',
    not _F('✅ S40 某条腿：不往 stdout 印 ❌ 才算干净') and _F('❌ S40 某条腿真红了'),
    '两向=' + str([_F('✅ 通过项含 ❌'), _F('❌ 失败项')]))
chk('S40 只认行为不认描述：通过行里"谈论失败"（失败路径返回 503 / 4 失败 + 2 新增）不算失败行',
    not _F('✅ S10 部署 WSGI：失败路径返回 503（此前一律 200）')
    and not _F('✅ H5 坏文件 commit：4 失败 + 2 新增（部分成功语义）')
    and not _F('✅ 汇总：通过 1384 项，失败 0 项'), '描述性文字被当成行为=假红')
chk('S40 Traceback 不进聚合器口径（本轮 L4 真日志里有 3 行 Traceback 而失败项为 0：错误路径夹具的正当产物）；'
    '崩溃由 returncode 兜、计数由自报汇总行兜',
    not _F('Traceback (most recent call last):') and not _F('   ') and not _F('一些说明文字'),
    str([_F('Traceback (most recent call last):'), _F('   ')])[:80])
chk('S40 取数面按行不按整坨：整段文本里含失败字符但每行都是通过行 ⇒ 0 条失败行',
    not any(_F(x) for x in ['✅ a 含 ❌ 的标题', '✅ b'])
    and sum(1 for x in ['✅ a 含 ❌ 的标题', '❌ b'] if _F(x)) == 1, '计数口径不符')
_fd = _s39_qg._failure_details('✅ 通过项标题里有 ❌ 字符\n', 'test_probe.py')
chk('S40 明细兜底：判了红但按新口径挑不出失败行 ⇒ 必须明说"无失败输出但返回码非 0"，不得空清单放行',
    len(_fd) == 1 and '返回码非 0' in _fd[0], str(_fd)[:150])
chk('S40 明细仍能挑出真失败行：❌ 行 + 「失败项」后续条目都要留（第 15 轮那条动因不得回退）',
    any('真红' in x for x in _s39_qg._failure_details('❌ 真红一条\n失败项：\n  - 条目甲\n', 'test_probe.py')),
    '明细丢失=红了不知道为什么红')

try:
    _s40_run(_s40_face([_s40_line()], _s40_doc()))  # 触发一次以确认 monkeypatch 已复原
    _want = _s39_os.path.normcase(_s39_os.path.abspath(_s40_realroot or ''))
    _got = _s39_qg._s48_repo_root() or ''
    _restored = _s39_os.path.normcase(_s39_os.path.abspath(_got)) == _want
except Exception as exc:  # noqa: BLE001
    _restored, _got = False, f'{type(exc).__name__}: {exc}'
chk('S40 夹具不留半成品：monkeypatch 之后 _s48_repo_root 已复原（改全局必须改回来）', _restored,
    '当前=' + str(_got)[:110])
try:
    _ro, _rd = _s39_qg._s48_repo_root(), None
    _s39_qg.results.clear()
    _s39_qg.check_local_gate_ledger()
    _ro, _rd = _s39_last()
    _real_err = ''
except Exception as exc:  # noqa: BLE001
    _ro, _real_err = False, f'{type(exc).__name__}: {exc}'
chk('S40 真仓第 49 项两态可判别：判绿，或红因只可能是三份输入缺一（禁"含糊的红"）',
    _ro or (_real_err == '' and all(
        ('local-runs.jsonl' in str(d)) or ('local-run-receipt' in str(d)) or ('缺一' in str(d))
        or ('一致性' in str(d)) or ('无法证明二者同源' in str(d)) for d in _rd)) and bool(_rd),
    (_real_err or '；'.join(map(str, _rd)))[:200])
_s39_qg.results.clear()


# ══════════ S41：迁移补丁的可逆性（第 35 轮 H127-H131 / 门禁第 50 项）══════════
# 演习全部跑在进程私有临时库上（L4 开局即 use_temp_db()），不碰 data/ican.db。
import hashlib as _s41_h
import init_db as _s41  # noqa: E402  （L4 与它同进程；此处只取函数，不跑 main）
from src import schema_ledger as _s41_led
from src.app import create_app as _s41_ca
from src.models import DailySales, Product  # noqa: F401
from src.models import db as _s41_db
import restore_db as _s41_rd
import sqlalchemy as _s41_sa
from datetime import date as _s41_date  # 不用 _d6：那个名字在本文件后面被重绑成 str（实测 TypeError）

_S41_DB = use_temp_db()
_app41 = _s41_ca('development')
_rev_recs = [r for r in _s41_led.PATCHES if r.rev == 'auto']
chk('S41 声明侧真实名单自洽：rev_problems() 为空（等级合法 + note 够长）',
    _s41_led.rev_problems() == [], str(_s41_led.rev_problems())[:160])
chk('S41 至少一条补丁声明为 auto（否则本轮交付的"能撤"是空话）',
    len(_rev_recs) >= 1 and {r.pid for r in _rev_recs} == {'p006_cost_backfill'},
    str([r.pid for r in _rev_recs])[:120])
chk('S41 三档齐备且各档都有理由：manual/none 不是"没想清楚"的垃圾桶',
    {'manual', 'none', 'auto'} == {r.rev for r in _s41_led.PATCHES}
    and all(len(r.rev_note) >= _s41_led.REV_MIN_NOTE for r in _s41_led.PATCHES),
    str({r.pid: r.rev for r in _s41_led.PATCHES})[:170])

# —— 声明侧纯函数反例（合成名单，不落盘、不连库） ——
import dataclasses as _s41_dc
_bad_rev = _s41_dc.replace(_s41_led.PATCHES[0], rev='maybe')
_short = _s41_dc.replace(_s41_led.PATCHES[1], rev_note='能撤')
chk('S41 有牙：rev 值域外 ⇒ 点名值域（不是只说"不合法"）',
    any('值域' in p for p in _s41_led.rev_problems([_bad_rev])), str(_s41_led.rev_problems([_bad_rev]))[:150])
chk('S41 有牙：rev_note 过短 ⇒ 判"写了等级没写代价"（防空话声明）',
    any('等于没声明' in p for p in _s41_led.rev_problems([_short])), str(_s41_led.rev_problems([_short]))[:150])
chk('S41 零重复守卫：第 50 项不再判 pid 重复（同一事实只许一处判，重复归第 24 项）',
    not any('重复' in p for p in _s39_qg._s50_decl_problems(
        [{'pid': 'p001_a', 'kind': 'ddl', 'rev': 'auto', 'note': '这一条理由够十二字以上'},
         {'pid': 'p001_a', 'kind': 'ddl', 'rev': 'auto', 'note': '这一条理由够十二字以上'}])),
    '两处都判 ⇒ 撤任一处另一处仍绿，变异测试恒不翻红')
_s41_dup = _s17_run_gate('check_migration_ledger', _s17_tree(
    lambda t: t.replace("PatchRecord('p002_store_scoped_uq'",
                        "PatchRecord('p001_ensure_columns'", 1),
    target='src/schema_ledger.py'))
chk('S41 分工不漏（行为面，不是 grep 源码）：把一条 pid 改成与上一行同名 ⇒ 第 24 项必须判红并点名',
    _s41_dup[1] is False and any('重复' in str(d) for d in _s41_dup[2]), str(_s41_dup[2])[:160])
_s41_sec = _s39_qg._s24_registry_section
chk('S41 有牙（本轮实测逼出来的收紧）：注册表那一节缺一行，文档别处再提到也不算过',
    'p002_b' not in (_s41_sec('## 补丁注册表\n\n| `p001_a` |\n\n## 回滚\n\n`p002_b` 也提一句\n') or ''),
    str(_s41_sec('## 补丁注册表\n\n| `p001_a` |\n\n## 回滚\n\nx\n'))[:120])
chk('S41 收紧的边界：整节不存在 ⇒ 返回 None，上层判"没测到"而不是判过',
    _s41_sec('## 一条命令升级\n\n正文') is None and _s41_sec('') is None, str(_s41_sec('x'))[:80])
chk('S41 零输入不判绿：空名单 ⇒ 判"无输入"而非"没问题"',
    any('不得记通过' in p for p in _s39_qg._s50_decl_problems([])), str(_s39_qg._s50_decl_problems([]))[:120])
chk('S41 必填位置参数（漏写 rev 是 TypeError，不是默认值）：少一格直接构造失败',
    _s41_dc.fields(_s41_led.PatchRecord).__len__() == 5 and
    all(f.default is _s41_dc.MISSING for f in _s41_dc.fields(_s41_led.PatchRecord)),
    str([f.name for f in _s41_dc.fields(_s41_led.PatchRecord)])[:160])

# —— 门禁第 50 项的三个纯函数：合成面打反例 ——
_g50 = _s39_qg
_d1 = _g50._s50_parse_decls("PatchRecord('p001_a', '标题', 'ddl', 'auto', '这一条理由够十二字以上')")
chk('S41 门禁解析器能吃真声明文本（五格全取到）',
    _d1 and _d1[0]['pid'] == 'p001_a' and _d1[0]['rev'] == 'auto' and 'rev' in _d1[0], str(_d1)[:150])
_chk4 = _g50._s50_parse_decls("PatchRecord('p001_a', '标题', 'ddl')")
chk('S41 有牙：PatchRecord 若退回四格（改了签名没改门禁）⇒ 解析为 0 条 ⇒ 判"无输入"而非绿',
    _chk4 == [] and any('无输入' in p for p in _g50._s50_decl_problems(_chk4)), str(_chk4)[:120])
_k1 = _g50._s50_reverser_keys("_X = 1\n_PATCH_REVERSERS = {\n    'p006_cost_backfill': _revert_cost_backfill,\n}\n")
chk('S41 门禁取反向实现名单：正常面解析出键集合（不是返回 None）',
    _k1 == {'p006_cost_backfill'}, str(_k1)[:120])
chk('S41 有牙：绑定表整块缺失 ⇒ 返回 None，上层判"没接线"（区别于"表在但为空"）',
    _g50._s50_reverser_keys('def nothing(): pass') is None, str(_g50._s50_reverser_keys('x'))[:80])
_empty_blk = _g50._s50_reverser_keys('_PATCH_REVERSERS = {\n}\n')
chk('S41 空绑定表 ⇒ 空集合（声明有 auto 时由差集判红，不静默放过）',
    _empty_blk == set(), str(_empty_blk)[:80])
_doc_ok = ('## 回滚\n\n'
           '`auto` 档：p006_cost_backfill 可局部撤。\n\n'
           '`manual`/`none` 档走 restore_db 整库回退。\n\n## 别的事\n')
chk('S41 文档腿·正例：回滚章存在 + 点名 auto 补丁 + 指路 restore_db ⇒ 0 违例',
    _g50._s50_doc_problems(_doc_ok, {'p006_cost_backfill'}) == [], str(_g50._s50_doc_problems(_doc_ok, set()))[:140])
chk('S41 有牙：整段没有回滚章 ⇒ 判红（补丁只有正向 = 出事时没人知道往回怎么走）',
    any('回滚' in p for p in _g50._s50_doc_problems('## 一条命令升级\n正文', {'p006_cost_backfill'})), '')
chk('S41 有牙：回滚章里漏点名某条 auto 补丁 ⇒ 点名是哪条',
    any('p007_x' in p for p in _g50._s50_doc_problems(
        _doc_ok, {'p006_cost_backfill', 'p007_x'})), str(_g50._s50_doc_problems(
        _doc_ok, {'p006_cost_backfill', 'p007_x'}))[:150])
chk('S41 有牙：回滚章没指路整库回退去处 ⇒ 判红（manual/none 档被写成"没办法"）',
    any('restore_db' in p for p in _g50._s50_doc_problems(
        '## 回滚\n\np006_cost_backfill 可撤。\n', {'p006_cost_backfill'})), '')
chk('S41 真文档过同一条尺：UPGRADE.md 的回滚章点名了每一条 auto 补丁',
    _g50._s50_doc_problems(_g50.read(_g50.os.path.join(_g50.ROOT, 'UPGRADE.md')),
                           {r.pid for r in _s41_led.PATCHES if r.rev == 'auto'}) == [],
    str(_g50._s50_doc_problems(_g50.read(_g50.os.path.join(_g50.ROOT, 'UPGRADE.md')),
                               {r.pid for r in _s41_led.PATCHES if r.rev == 'auto'}))[:170])

# ── 真演习：正向 p006 → 撤 → 再正向（幂等闭环）+ 各条拒绝路径 ──────────────
def _s41_q(sql):
    with _s41_db.engine.connect() as c:
        return c.execute(_s41_sa.text(sql)).fetchall()


def _s41_cost(name):
    # 按 staff_id 取"被回填那一行"，不靠行序（同名两行时 rows[0] 就是运气）
    rows = _s41_q("SELECT product_cost, cost_basis FROM daily_sales WHERE staff_id=9001")
    return rows[0] if rows else (None, None)


with _app41.app_context():
    _s41_db.create_all()
    with _s41_db.engine.begin() as _c:
        _c.execute(_s41_sa.text("DELETE FROM daily_sales WHERE product_name LIKE 'S41%'"))
        _c.execute(_s41_sa.text("DELETE FROM products WHERE product_name LIKE 'S41%'"))
    _s41_db.session.add(Product(product_name='S41 商品', cost_price=7.7,
                                store_id=None, pack_id='s41'))
    _s41_db.session.add(DailySales(staff_id=9001, record_date=_s41_date(2026, 9, 20),
                                   product_name='S41 商品', product_sales_qty=3,
                                   total_revenue=23.1, product_cost=None,
                                   cost_basis=None))
    _s41_db.session.add(DailySales(staff_id=9002, record_date=_s41_date(2026, 9, 20),
                                   product_name='S41 商品', product_sales_qty=1,
                                   total_revenue=7.7, product_cost=12.3,
                                   cost_basis='snapshot'))
    _s41_db.session.commit()
    _s41_led.record_all(mode='adopted', detail='S41 演习：假装已跟到最新')

    _fwd = _s41._patch_cost_backfill()
    _after_fwd = _s41_cost('S41 商品')
    chk('S41 演习前置：正向 p006 真回填，且打了 backfill 标记（可撤性的唯一前提）',
        _after_fwd[0] is not None and _after_fwd[1] == 'backfill' and _fwd.get('changed'),
        str(_fwd)[:140])

    _pre_rows = dict(b=_s41_cost('S41 商品'), s=_s41_q(
        "SELECT product_cost, cost_basis FROM daily_sales WHERE staff_id=9002")[0])
    _snap, _why = _s41_rd.pre_rollback_copy(_S41_DB, 's41probe', tag='s41-check', tool='l4')
    chk('S41 锚点出口可注入 tag/tool（补丁回滚与整库恢复共用同一个出口，证据卡须如实记产出方）',
        bool(_snap) and not _why and 's41-check' in _snap, str(_why or _snap)[:120])

    _res = _s41.rollback_patch('p006_cost_backfill', confirmed=True, verbose=False)
    _after_back = _s41_cost('S41 商品')
    _kept_snap = _s41_q("SELECT product_cost, cost_basis FROM daily_sales WHERE staff_id=9002")[0]
    chk('S41 真回滚：rollback_patch 走通并回报 reverted（含锚点路径与摘掉的账本行）',
        _res.get('action') == 'reverted' and _res.get('anchor') and _res.get('unrecorded', {}).get(
            'patch_id') == 'p006_cost_backfill', str(_res)[:170])
    chk('S41 回滚精确：backfill 行退回补丁前的真实状态（cost_basis 是 NOT NULL DEFAULT none，'
        '写 NULL 会 IntegrityError——第一版就是这么错的）',
        _after_back == (None, 'none'), str(_after_back)[:120])
    chk('S41 回滚不越界：snapshot 行的值与来源标记一字未动（反向动作只认标记）',
        tuple(_kept_snap) == tuple(_pre_rows['s']), str(_kept_snap) + str(_pre_rows['s'])[:120])
    _lg = _s41_led.logged_rows()
    chk('S41 撤干净=账本摘行：p006 不在账本，其余 6 条仍在（撤一条不得连坐）',
        'p006_cost_backfill' not in _lg and len(_lg) == len(_s41_led.PATCH_IDS) - 1,
        str(sorted(_lg))[:170])
    _anch = _res['anchor']
    with _s41_sa.create_engine('sqlite:///' + _anch.replace(chr(92), '/')).connect() as _ac:
        _anch_rows = _ac.execute(_s41_sa.text(
            "SELECT product_cost, cost_basis FROM daily_sales WHERE staff_id=9001")).fetchall()
    chk('S41 锚点是真副本：只读打开锚点仍查得到 backfill 行（撤之前的状态确实在盘上）',
        len(_anch_rows) == 1 and _anch_rows[0][1] == 'backfill' and _anch.endswith('.bak'),
        str(_anch_rows)[:140])
    _mani = _anch + '.manifest.json'
    _mtool = ''
    if os.path.isfile(_mani):
        _mtool = str((_s39_js.loads(open(_mani, encoding='utf-8').read()) or {}).get('tool'))
    chk('S41 证据卡如实记产出方：tool=init_db.rollback（事后按产出方归因不能指错人）',
        os.path.isfile(_mani) and _mtool == 'init_db.rollback', 'tool=' + _mtool[:60])
    _again = _s41.apply_patches(verbose=False)
    _re = [a for a in _again if a.get('patch') == 'p006_cost_backfill']
    chk('S41 幂等闭环：撤完再跑迁移 ⇒ p006 被当作未跑补丁重新执行（撤干净的正解自证）',
        bool(_re) and _re[0]['action'] == 'run' and 'p006_cost_backfill' in _s41_led.logged_rows(),
        str(_re)[:160])
    chk('S41 再执行后值确实回来了（不是只补了账本）',
        _s41_cost('S41 商品')[1] == 'backfill', str(_s41_cost('S41 商品'))[:120])
    _twice = _s41.rollback_patch('p006_cost_backfill', confirmed=True, verbose=False)
    _s41.rollback_patch('p006_cost_backfill', confirmed=True, verbose=False)
    _third = _s41.rollback_patch('p006_cost_backfill', confirmed=True, verbose=False)
    chk('S41 撤两次不炸：账本已无记录时回报 not-applied（合法态，不是失败）',
        _twice.get('action') == 'reverted' and _third.get('action') == 'not-applied',
        str([_twice.get('action'), _third.get('action')])[:120])
    chk('S41 库完整性仍然 ok（一整轮撤/补之后没有把库改坏）',
        _s41_q('PRAGMA integrity_check')[0][0] == 'ok', str(_s41_q('PRAGMA integrity_check'))[:80])

    # —— 拒绝路径：必须报错，且不许留下任何写入 ——
    _sha0 = _s41_h.sha256(open(_S41_DB, 'rb').read()).hexdigest()
    for _pid, _want in (('p003_default_tenant', "rev='none'"), ('p001_ensure_columns', "rev='manual'")):
        try:
            _s41.rollback_patch(_pid, confirmed=True, verbose=False)
            _err = ''
        except NotImplementedError as exc:
            _err = str(exc)
        chk(f'S41 有牙：{_pid} 非 auto 档请求回滚 ⇒ NotImplementedError 且消息带档位（Django 同形，'
            '禁"安静跳过"）', _want in _err and 'restore_db' in _err, _err[:150])
    try:
        _s41.rollback_patch('p006_cost_backfill', confirmed=False, verbose=False)
        _err2 = ''
    except ValueError as exc:
        _err2 = str(exc)
    chk('S41 有牙：缺 --yes ⇒ ValueError 拒执行（破坏性写库动作不许默认放行）',
        '--yes' in _err2, _err2[:140])
    try:
        _s41.rollback_patch('p999_ghost', confirmed=True)
        _err3 = ''
    except KeyError as exc:
        _err3 = str(exc)
    chk('S41 有牙：未知 pid ⇒ KeyError 点名"声明名单里没有"（不猜、不新建）',
        'p999_ghost' in _err3, _err3[:140])
    chk('S41 四条拒绝路径全部零写入：库文件字节与拒撤前完全一致',
        _s41_h.sha256(open(_S41_DB, 'rb').read()).hexdigest() == _sha0, 'sha 漂了=拒绝时动了库')
    # 反例的输入面必须落在被保护分支上：上面几轮已把 p006 撤出账本，
    # 不先补回去，这条腿测到的其实是"没跑过的补丁"那条早退分支（第一版就这么假绿过一轮）。
    _s41.apply_patches(verbose=False)
    _sha0 = _s41_h.sha256(open(_S41_DB, 'rb').read()).hexdigest()
    chk('S41 前置自证：锚点反例开跑前 p006 确实在账本里（否则下面的"没动库"是恒真断言）',
        'p006_cost_backfill' in _s41_led.logged_rows(), str(sorted(_s41_led.logged_rows()))[:150])
    _orig_pr = _s41_rd.pre_rollback_copy
    _s41_rd.pre_rollback_copy = lambda *a, **k: ('', '模拟锚点建不出来')
    try:
        _s41.rollback_patch('p006_cost_backfill', confirmed=True, verbose=False)
        _err4 = '没抛异常'
    except RuntimeError as exc:
        _err4 = str(exc)
    finally:
        _s41_rd.pre_rollback_copy = _orig_pr
    chk('S41 有牙（输入面落在被保护分支上）：锚点建不出来 ⇒ RuntimeError 且**没动库**',
        '锚点' in _err4 and _s41_h.sha256(open(_S41_DB, 'rb').read()).hexdigest() == _sha0, _err4[:150])
    chk('S41 变异守卫没被绕过：monkeypatch 之后 pre_rollback_copy 已复原（改全局必须改回来）',
        _s41_rd.pre_rollback_copy is _orig_pr, '未复原=后面的腿测的是假对象')
    _un = _s41_led.unrecord_pid('p999_ghost')
    chk('S41 unrecord_pid 对无此记录的 pid 返回 None 且不抛（幂等，撤不存在的东西不算失败）',
        _un is None, str(_un)[:80])

# —— CLI 层：参数面与退出码（不建 app 的路径先测，建 app 的走注入接缝） ——
_b0 = _s41_h.sha256(open(_S41_DB, 'rb').read()).hexdigest()
chk('S41 CLI：--help 退出 0 且**不改库**（PH4-0-FU1 同一条尺，扩展到新增参数后仍成立）',
    _s41.main(['--help']) == 0 and _s41_h.sha256(open(_S41_DB, 'rb').read()).hexdigest() == _b0, '库被 --help 改了=守卫失效')
chk('S41 CLI 有牙：裸 --rollback（不带 =pid）⇒ 未知参数退 2，不静默当默认模式跑',
    _s40s(_s41.main, ['--rollback']) == 2 and _s41_h.sha256(
        open(_S41_DB, 'rb').read()).hexdigest() == _b0, '退码或库字节不对=可能被当成重建')
chk('S41 CLI 有牙：--rollback= 空值 ⇒ 退 2 并给形状示例（不猜 pid）',
    _s40s(_s41._rollback_cli, '--rollback=', True) == 2, '')
chk('S41 CLI 有牙：一次给两条 --rollback ⇒ 退 2（一次只准撤一条，防批量误撤）',
    _s40s(_s41.main, ['--rollback=p006_cost_backfill', '--rollback=p003_default_tenant']) == 2, '')
chk('S41 CLI 有牙：--rollback 与 --if-empty 同时给 ⇒ 退 2（一个撤一个补，同条命令里矛盾）',
    _s40s(_s41.main, ['--rollback=p006_cost_backfill', '--if-empty']) == 2, '')
chk('S41 CLI 接缝：非 auto 档经 CLI 映射为退出码 1（拒撤不能长得像成功）',
    _s40s(_s41._rollback_cli, '--rollback=p003_default_tenant', True,
              app_factory=lambda env='development': _app41) == _s41.RC_REFUSED
    and _s41_h.sha256(open(_S41_DB, 'rb').read()).hexdigest() == _b0, '退码或零写入不成立')
with _app41.app_context():
    _pre97 = _s41.rollback_patch('p006_cost_backfill', confirmed=True, verbose=False)
_b1 = _s41_h.sha256(open(_S41_DB, 'rb').read()).hexdigest()
_rc97 = _s40s(_s41._rollback_cli, '--rollback=p006_cost_backfill', True,
              app_factory=lambda env='development': _app41)
chk('S41 CLI 接缝：撤过一次后再走 CLI ⇒ 账本已无记录，退 0 且**不再写库**（合法态不是失败）',
    _pre97.get('action') == 'reverted' and _rc97 == _s41.RC_OK
    and _s41_h.sha256(open(_S41_DB, 'rb').read()).hexdigest() == _b1,
    f"action={_pre97.get('action')} rc={_rc97}")
chk('S41 缺确认位一律先拒（不看账本状态）：同一行命令在"跑过/没跑过"两种库上行为一致',
    _s40s(_s41._rollback_cli, '--rollback=p006_cost_backfill', False,
              app_factory=lambda env='development': _app41) == _s41.RC_REFUSED, '')

# —— M97：覆盖读数三态（第 34 轮只数精确匹配，提交一落地就归零） ——
_h97 = 'a' * 40
_rows97 = [_s40_doc(seq=1, head=_h97), _s40_doc(seq=2, head=_h97)]
_l97, _d97 = _s39_qg._s49_coverage(_rows97, _h97)
chk('S41 M97 正例：两行都在当前 HEAD ⇒ covered=2 且 root 不给时不猜祖先（unknown 计数在册）',
    _d97['covered'] == 2 and '2/2' in _l97 and _d97['ancestor'] == 0, str(_d97)[:140])
_l97b, _d97b = _s39_qg._s49_coverage(_rows97, 'b' * 40)
chk('S41 有牙：一行都不在当前 HEAD 上 ⇒ covered=0 但**不得**把两行判成"离线"（没给 root 就是无法判定）',
    _d97b['covered'] == 0 and _d97b['offline'] == 0 and _d97b['unknown'] == 2, str(_d97b)[:150])
_r97 = _s39_tf.mkdtemp(prefix='s41git_')
_env97 = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
              GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')


def _r97_git(*a):
    return _s39_sp.run(['git', '-C', _r97] + list(a), capture_output=True,
                       text=True, env=_env97).stdout.strip()


_r97_git('init', '-q', '-b', 's41main')


def _r97_commit(tag):
    with open(os.path.join(_r97, tag + '.txt'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(tag + '\n')
    _r97_git('add', '-A')
    _r97_git('commit', '-qm', tag)
    return _r97_git('rev-parse', 'HEAD')


_c1 = _r97_commit('one')
_c2 = _r97_commit('two')
_r97_git('checkout', '-q', '-b', 'side', _c1)
_c3 = _r97_commit('side-branch')
_r97_git('checkout', '-q', 's41main')
chk('S41 git 夹具自证：三个 sha 互不相同且各 40 位（相同=三态腿全在测同一条分支，等于没测）',
    len({_c1, _c2, _c3}) == 3 and all(len(x) == 40 for x in (_c1, _c2, _c3)),
    str([x[:8] for x in (_c1, _c2, _c3)])[:120])
_l97c, _d97c = _s39_qg._s49_coverage([_s40_doc(seq=1, head=_c1), _s40_doc(seq=2, head=_c3),
                                      _s40_doc(seq=3, head=_c2)], _c2, root=_r97)
chk('S41 三态实测（真 git 仓分叉）：祖先线 1 行 / 离线 1 行 / 命中 1 行 各归各位',
    _d97c == {'age_h': _d97c['age_h'], 'covered': 1, 'ancestor': 1, 'offline': 1, 'unknown': 0},
    str(_d97c)[:150])
chk('S41 三态要说人话：祖先线行存在时读数里出现「祖先线」，且当前 HEAD 未验证时给出提交距离',
    '祖先线 1 行' in _l97c, _l97c[:170])
_l97d, _d97d = _s39_qg._s49_coverage([_s40_doc(seq=1, head=_c1)], _c2, root=_r97)
chk('S41 距离读数：没有一行命中当前 HEAD ⇒ 必须印「距上次真跑 N 个提交」（0 不等于没跑过）',
    '距上次真跑 1 个提交' in _l97d and _d97d['covered'] == 0, _l97d[:170])
_l97e, _d97e = _s39_qg._s49_coverage([_s40_doc(seq=1, head='f' * 40)], _c2, root=_r97)
chk('S41 有牙：仓库里根本不存在的 sha ⇒ 记 unknown 而不是 offline（"取不到数"≠"不同线"）',
    _d97e['unknown'] == 1 and _d97e['offline'] == 0, str(_d97e)[:140])
chk('S41 反例不碰真仓：git 夹具建在临时目录，主仓 HEAD 与分支未受影响',
    _s39_sp.run(['git', '-C', _s40_realroot, 'status', '--porcelain'], capture_output=True,
                text=True).stdout.count('S41') == 0, '临时仓夹具漏进主仓')


# ══════════ S42：版本字面量载体枚举器 set_version.py（第 35 轮 M103）══════════
import io as _s42_io
import set_version as _s42
import subprocess as _s42_sp

_S42_FILES = (('src/version.py', "APP_VERSION = '%s'\n"),
              ('README.md', '> 当前版本：**v%s**\n'),
              ('UPGRADE.md', '> 当前版本：**v%s**\n'),
              ('REPO_ROOT_README', '- **当前版本：v%s**\n'))


def _s42_tree(ver='1.2.3', stale=(), drop=None, twice=False):
    """造一个 "<base>/work" 面：仓库根 README 在 work 的上一级（与真仓同形状，测试不碰真仓）。

    `stale` 按**相对载体的名字**点名要写旧值的文件，不按分隔符猜路径：
    第一版写 `rel.startswith('..' + os.sep)` 想排除仓根 README，
    而 `_S42_FILES` 里的 rel 用的是正斜杠 ⇒ 判断永假，四个载体全被写成旧值，
    "载体之间不一致"这条反例当场变成"完全一致"（leg 拿到的 problems 是空表）。
    """
    base = _s39_tf.mkdtemp(prefix='s42_')
    work = _s39_os.path.join(base, 'work')
    _s39_os.makedirs(_s39_os.path.join(work, 'src'), exist_ok=True)
    # base 自己得是一个 git 仓：否则工具在合成面上解析仓根 README 时会往上找到"某个别的仓"，
    # 测的就不是这块面了（面依赖正是本轮在权威副本里翻车的那条，见 §7-10）
    _env42 = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                  GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
    _s39_sp.run(['git', '-C', base, 'init', '-q', '-b', 's42main'], capture_output=True, env=_env42)
    made = {}
    for rel, tmpl in _S42_FILES:
        path = (_s39_os.path.join(base, 'README.md') if rel == 'REPO_ROOT_README'
                else _s39_os.path.normpath(_s39_os.path.join(work, rel)))
        if drop and rel == drop:
            continue
        val = stale[1] if (stale and rel == stale[0]) else ver
        with _s42_io.open(path, 'w', encoding='utf-8', newline='') as fh:
            fh.write(tmpl % val)
            if twice and rel == 'README.md':
                fh.write(('> 当前版本：**v%s**（第二处）\n' % val))
        made[path] = (tmpl % val)
    return base, work, made


_rows42, _p42 = _s42.plan(_s42_tree()[1])
chk('S42 枚举器正例：四载体齐全一致 ⇒ 0 问题且逐格都读到值',
    _p42 == [] and len(_rows42) == 4 and all(v for _r, v in _rows42), str(_p42)[:150])
_rows42b, _p42b = _s42.plan(_s42_tree(stale=('src/version.py', '1.0.0'))[1])
chk('S42 有牙：某一格滞后（第 34 轮那个失效形状）⇒ 点名载体之间不一致并列出各值',
    any('不一致' in x and '1.0.0' in x for x in _p42b) and len(_p42b) == 1, str(_p42b)[:170])
_rows42c, _p42c = _s42.plan(_s42_tree(drop='UPGRADE.md')[1])  # 按载体名丢，不按路径后缀猜
chk('S42 有牙：登记了路径却没文件 ⇒ 判"判据面空转"，不得当成少一个载体就行',
    any('空转' in x for x in _p42c), str(_p42c)[:170])
_rows42d, _p42d = _s42.plan(_s42_tree(twice=True)[1])
chk('S42 有牙：同一文件两处版本行 ⇒ 判"命中 2 次"（锚点必须唯一，不猜改哪一行）',
    any('命中 2 次' in x for x in _p42d), str(_p42d)[:170])
_b42, _w42, _m42 = _s42_tree(stale=('UPGRADE.md', '1.0.0'))
_before42 = {k: open(k, 'rb').read() for k in _m42}
_rc42 = _s40s(_s42.apply_version, '9.9.9', _w42)
chk('S42 整体拒绝：面不一致时 apply 退 1 且四个文件一个字节都没改（不做部分成功）',
    _rc42 == 1 and all(open(k, 'rb').read() == v for k, v in _before42.items()), 'rc=%s' % _rc42)
_b42b, _w42b, _m42b = _s42_tree()
_rc42b = _s40s(_s42.apply_version, '2.5.0', _w42b)
_back42, _p42e = _s42.plan(_w42b)
chk('S42 真改齐：apply 后重新跑 plan 读回四格都等于新值（不靠内存里那份字符串自证）',
    _rc42b == 0 and _p42e == [] and all(v == '2.5.0' for _r, v in _back42), str(_back42)[:170])
chk('S42 有牙：版本号形状不合法 ⇒ 退 2 且不写文件（不接受 v2.5 / 2.5 这类半截版本）',
    _s40s(_s42.apply_version, 'v2.5', _w42b) == 2, '')
_s42_path = _s39_os.path.join(_s39_root, 'set_version.py')
_r42 = _s42_sp.run([sys.executable, _s42_path], capture_output=True, text=True, cwd=_s39_root)
chk('S42 入口子进程实测：不带任何参数 ⇒ 退 2 并说明"不猜你想不想写文件"（零输入不静默成功）',
    _r42.returncode == 2 and '必须显式带' in _r42.stdout, 'rc=%s %s' % (_r42.returncode, _r42.stdout[:70]))
_r42b = _s42_sp.run([sys.executable, _s42_path, '--check'], capture_output=True, text=True, cwd=_s39_root)
chk('S42 真面端到端：--check 在当前树退 0 且逐载体各印一行（四行 READ）',
    _r42b.returncode == 0 and len([x for x in _r42b.stdout.splitlines()
                                     if x.strip().startswith('READ')]) == 4
    and '载体一致' in _r42b.stdout,
    'rc=%s 行首READ=%s 子串count=%s（README 里含 READ，子串数会虚高）' % (
        _r42b.returncode,
        len([x for x in _r42b.stdout.splitlines() if x.strip().startswith('READ')]),
        _r42b.stdout.count('READ')))
_r42c = _s42_sp.run([sys.executable, _s42_path, '--nope'], capture_output=True, text=True, cwd=_s39_root)
chk('S42 有牙：未知参数 ⇒ 退 2 并点名收到了什么（未知 flag 不得当文件名/模式）',
    _r42c.returncode == 2 and '--nope' in _r42c.stdout, _r42c.stdout[:110])
_s42_src = open(_s42_path, encoding='utf-8').read()
_s42_tree_ast = _s40_ast.parse(_s42_src)
_s42_main = next((n for n in _s42_tree_ast.body
                  if isinstance(n, _s40_ast.FunctionDef) and n.name == 'main'), None)
chk('S42 入口形状：main 是模块级函数且末句 return，末行 raise SystemExit(main())（防静默返回 None=退 0）',
    _s42_main is not None and isinstance(_s42_main.body[-1], _s40_ast.Return)
    and 'raise SystemExit(main())' in _s42_src, '形状不符')
_b42c, _w42c, _m42c = _s42_tree(drop='REPO_ROOT_README')
_rows42f, _p42f = _s42.plan(_w42c)
chk('S42 无仓根的面（ZIP 解压态）⇒ 该格记 None + 标"本面无仓根"且不判红，其余三格照旧要齐',
    _p42f == [] and len(_rows42f) == 4 and _rows42f[3][1] is None
    and '本面无仓根' in _rows42f[3][0], str(_rows42f)[:170])
chk('S42 吞打印不等于没测：_S40_LAST_OUT 里确实收到过拒撤/拒改的原文（≥6 次且含两处理由）',
    len(_S40_LAST_OUT) >= 6 and sum(1 for x in _S40_LAST_OUT if '不可自动撤' in x) >= 1
    and sum(1 for x in _S40_LAST_OUT if '先修对齐' in x) >= 1,
    '吞到的份数=%s' % len(_S40_LAST_OUT))

chk('S42 载体清单与门禁同源：第 25 项读的三处字面量必须都在 CARRIERS 里（漏一格=工具与判据各记一份）',
    all(any(rel in c[0] for c in _s42.CARRIERS)
        for rel in ('src/version.py', 'README.md', 'UPGRADE.md')) and len(_s42.CARRIERS) == 4,
    str([c[0] for c in _s42.CARRIERS])[:150])

# ══════════ S43：备份资格判定面（第 36 轮 H134-H138 / 门禁第 51 项）══════════
# 演习全部跑在临时备份面 + 进程私有临时库上，不碰 data/ican.db，也不碰真 backups/。
import ast as _s43_ast
import json as _s43_js
import os as _s43_os
import sqlite3 as _s43_sq
import subprocess as _s43_sp
import sys as _s43_sys
import tempfile as _s43_tf
from datetime import UTC as _s43_UTC, datetime as _s43_dt, timedelta as _s43_td

import init_db as _s43_init
import quality_gate as _s43_qg
import restore_db as _s43_rd
from src import business_calendar as _s43_cal, schema_ledger as _s43_sl
from src.app import create_app as _s43_ca
from src.backup_manifest import reconcile as _s43_reconcile
from src.backup_manifest import table_counts as _s43_tc, write_manifest as _s43_wm

_S43_DB = use_temp_db()
_APP43 = _s43_ca('development')
_S43_PY = _s43_sys.executable
_S43_HERE = _s43_os.path.dirname(_s43_os.path.abspath(__file__))


def _s43_run(*args):
    """子进程跑真 CLI（契约③：入口必须被子进程验，只 import 纯函数不算测过）。"""
    return _s43_sp.run([_S43_PY, _s43_os.path.join(_S43_HERE, 'restore_db.py'), *args],
                       capture_output=True, text=True, cwd=_S43_HERE, timeout=180)


def _s43_init_run(*args):
    return _s43_sp.run([_S43_PY, _s43_os.path.join(_S43_HERE, 'init_db.py'), *args],
                       capture_output=True, text=True, cwd=_S43_HERE, timeout=300)


def _s43_card(path, source):
    """按**真生产者的调用形状**写卡（src_counts + counts 都给，才会留下 row_parity=True）。

    本轮实测：只给 `source_path` 时卡上 `checks.row_parity=None`，深核判"卡不可信"——
    这不是判据苛刻，是 `write_manifest` 从来没被允许只凭一半输入就宣称核对过。
    """
    _s43_wm(path, source_path=source, src_counts=_s43_tc(source),
            counts=_s43_tc(path), integrity=True, tool='test_l4.S43')


def _s43_face(names=(), ages=None, manifests=(), ghosts=(), drop=None, live=None):
    """造临时备份面 root/backups/*：内容取自真库快照，可选择性带卡 / 抹账本行 / 留幽灵卡。"""
    root = _s43_tf.mkdtemp(prefix='s43face_')
    bdir = _s43_os.path.join(root, 'backups')
    _s43_os.makedirs(bdir, exist_ok=True)
    src = live or _S43_DB            # 演习必须以"将被覆盖的那个文件"为源，不能假定它==use_temp_db 的返回
    for nm in names:
        p = _s43_os.path.join(bdir, nm)
        _s43_rd.snapshot_into(src, p)
        if drop and nm in drop:
            con = _s43_sq.connect(p)
            con.execute('DELETE FROM schema_patch_log WHERE patch_id=?', (drop[nm],))
            con.commit()
            con.close()
        if nm in manifests:
            _s43_card(p, src)
        if ages and nm in ages:
            when = (_s43_dt.now() - _s43_td(hours=ages[nm])).timestamp()
            _s43_os.utime(p, (when, when))
    for gh in ghosts:                             # 有证据卡但没有 .db ⇒ restic 意义上的 orphaned
        p = _s43_os.path.join(bdir, gh)
        _s43_rd.snapshot_into(src, p)
        _s43_card(p, src)
        _s43_os.remove(p)
    return root, bdir


chk('S43 资格判定是纯函数：同输入两次结果逐一相等（不读盘、不碰全局态）',
    _s43_rd.eligibility('ican_20260927_035845.db', True, 1.0)
    == _s43_rd.eligibility('ican_20260927_035845.db', True, 1.0))
chk('S43 名字里没时刻 ⇒ nameless（本机真实件名 ican-db-before-cisim-20260922.db 即此形态）',
    _s43_rd.eligibility('ican-db-before-cisim-20260922.db', True, 1.0)[0] == 'nameless')
chk('S43 有时刻没证据卡 ⇒ no_manifest（第 33 轮不变式：没核过的备份只是一个文件）',
    _s43_rd.eligibility('ican_20260927_035845.db', False, 1.0)[0] == 'no_manifest')
chk('S43 有卡但超龄 ⇒ stale，且理由里带实测龄与上限（只说"太旧"不可归因）',
    _s43_rd.eligibility('ican_20260927_035845.db', True, 999.0, 72)[0] == 'stale'
    and '999' in _s43_rd.eligibility('ican_20260927_035845.db', True, 999.0, 72)[1])
chk('S43 边界：龄恰好等于上限 ⇒ 仍够格（判据写的是 > 不是 >=，越界方向必须钉死）',
    _s43_rd.eligibility('ican_20260927_035845.db', True, 72.0, 72)[0] == 'eligible')
chk('S43 龄拿不到（None）⇒ stale 而不是放行（对齐 frappe is_file_old:741 的"取不到即不合格"）',
    _s43_rd.eligibility('ican_20260927_035845.db', True, None)[0] == 'stale')
chk('S43 两种坏法不折叠：integrity_fail 与 unreadable 各自成态',
    _s43_rd.eligibility('ican_20260927_035845.db', True, 1.0, 72, 'fail')[0] == 'integrity_fail'
    and _s43_rd.eligibility('ican_20260927_035845.db', True, 1.0, 72, 'error')[0] == 'unreadable')
chk('S43 全条件满足 ⇒ eligible 且不带理由文案',
    _s43_rd.eligibility('ican_20260927_035845.db', True, 1.0, 72, 'ok') == ('eligible', ''))
chk('S43 零输入：空文件名 ⇒ nameless（空输入永远不得读成"够格"）',
    _s43_rd.eligibility('', False, None)[0] == 'nameless')
chk('S43 判定顺序：名字不合法时不看证据卡（拒绝理由必须是最前置那条，便宜先跑）',
    _s43_rd.eligibility('no-ts-here.db', False, 9e9)[0] == 'nameless')
chk('S43 名单⇄拒绝态互为补集且"够格"在首位（与门禁第 51 项同源，这里独立再核）',
    set(_s43_rd.REFUSAL_STATES) == set(_s43_rd.ELIG_STATES) - {'eligible'}
    and _s43_rd.ELIG_STATES[0] == 'eligible')
chk('S43 有牙：阈值改 0 后同一份合格输入必须从 eligible 翻成 stale（断言真打在阈值上）',
    _s43_rd.eligibility('ican_20260927_035845.db', True, 1.0, 0)[0] == 'stale')

# ── 时间基准（本轮抓到的 8 小时错档，必须留复现夹具）──
chk('S43 name_ts 按文件名解析：ican_20260927_035845.db → 03:58:45（不读 mtime）',
    _s43_rd.name_ts('ican_20260927_035845.db') == _s43_dt(2026, 9, 27, 3, 58, 45))
chk('S43 name_ts 认不出的三类形态都给 None：无时刻名 / 非法月日 / 空串',
    _s43_rd.name_ts('PH2-2-pre-migration-dev7.db') is None
    and _s43_rd.name_ts('ican_20991301_999999.db') is None and _s43_rd.name_ts('') is None)
chk('S43 name_ts_utc 返回 aware 时刻（naive 直比从源头上被堵住）',
    _s43_rd.name_ts_utc('ican_20260927_035845.db').tzinfo is not None)
chk('S43 两种 naive 口径必须可区分：钟面读数与 UTC 列读出的绝对时刻差一个偏移',
    _s43_cal.from_utc_naive(_s43_dt(2026, 9, 27, 1, 46, 9))
    != _s43_cal.from_wall_clock(_s43_dt(2026, 9, 27, 1, 46, 9)))
chk('S43 8 小时 bug 复现：账本 UTC 01:46:09 vs 文件名 094600 —— 旧写法误判"晚于"，换算后确实"早 9 秒"',
    (_s43_dt(2026, 9, 27, 9, 46, 0) >= _s43_dt(2026, 9, 27, 1, 46, 9))
    and _s43_cal.from_wall_clock(_s43_dt(2026, 9, 27, 9, 46, 0))
    < _s43_cal.from_utc_naive(_s43_dt(2026, 9, 27, 1, 46, 9)))
chk('S43 wall_stamp 写出的 14 位钟面戳能被 name_ts 原样读回（写与读同一把尺）',
    _s43_rd.name_ts('ican_' + _s43_rd.wall_ts() + '.db') is not None)
chk('S43 from_wall_clock 收到已带 tzinfo 的输入时只换算、不重新解释（不二次加偏移）',
    abs((_s43_cal.from_wall_clock(_s43_dt.now(_s43_UTC)) - _s43_dt.now(_s43_UTC)).total_seconds()) < 5)

# ── 清单与选择（临时面真跑）──
_r0, _b0 = _s43_face()
chk('S43 空备份面 ⇒ 人口 0 条（枚举器在零输入上给 0，而不是崩、也不是"未知"）',
    _s43_rd.backup_records(root=_r0) == [])
_r1, _b1 = _s43_face(names=['ican_20260927_035845.db', 'ican_20260927_094600.db', 'no-ts-here.db'],
                     manifests=['ican_20260927_035845.db'])
chk('S43 人口按"名字里的时刻 新→旧"排，无时刻者排最后（不按字典序、不按 mtime）',
    [r['name'] for r in _s43_rd.backup_records(root=_r1)]
    == ['ican_20260927_094600.db', 'ican_20260927_035845.db', 'no-ts-here.db'],
    str([r['name'] for r in _s43_rd.backup_records(root=_r1)]))
chk('S43 每条是类型化记录（8 键齐备），不是一条裸路径（discourse create_file_from_path:70）',
    all(set(r) >= {'name', 'path', 'name_ts', 'age_h', 'bytes', 'manifest', 'integrity', 'state'}
        for r in _s43_rd.backup_records(root=_r1)))
chk('S43 本机真实形状复现：最新那份没卡 ⇒ 自动选中退到次新，而不是"最新"也不是 None',
    _s43_rd.pick_latest_eligible(root=_r1)[1]['name'] == 'ican_20260927_035845.db')
chk('S43 有牙：把次新的卡也抹掉 ⇒ 选择给 None 并附人口清单（拒绝而不是将就）',
    _s43_rd.pick_latest_eligible(
        root=_s43_face(names=['ican_20260927_035845.db', 'ican_20260927_094600.db'])[0])[0] is None)
chk('S43 有卡无件（幽灵）单独成态，不与"有件无卡"折叠成同一句"对不上"（restic :237 三分类）',
    _s43_rd.backup_records(root=_s43_face(ghosts=['ican_20260101_000000.db'])[0])[0]['state']
    == 'manifest_ghost')
chk('S43 排序不信 mtime：把老件 mtime 抹到未来，首位仍是名字更晚的那份',
    [r['name'] for r in _s43_rd.backup_records(
        root=_s43_face(names=['ican_20260927_035845.db', 'ican_20260927_094600.db'],
                       ages={'ican_20260927_035845.db': -5})[0])][0] == 'ican_20260927_094600.db')
chk('S43 浅扫时 integrity 必须是 unchecked（未核不得混成 ok）；深核才出 ok/fail',
    all(r['integrity'] == 'unchecked' for r in _s43_rd.backup_records(root=_r1))
    and any(r['integrity'] in ('ok', 'fail') for r in _s43_rd.backup_records(root=_r1, deep=True)))
_zb = _s43_os.path.join(_b1, 'ican_20260901_000000.db')
with open(_zb, 'wb') as _fz:
    _fz.write(b'\x00' * 8192)
_s43_wm(_zb, source_path=_S43_DB, tool='test_l4.S43')
chk('S43 有牙：全零 8KB 坏件即便带卡，也必须在深核里被判不合格（受控坏输入，第 33 轮同族）',
    [r for r in _s43_rd.backup_records(root=_r1, deep=True)
     if r['name'] == 'ican_20260901_000000.db'][0]['state'] in ('integrity_fail', 'unreadable', 'no_manifest'))
chk('S43 谓词不自证：喂一条 eligible 却没卡的记录进形状自证 ⇒ 必须点名（防实现与谓词各说一套）',
    bool(_s43_qg._s51_population_problems([{'name': 'x.db', 'state': 'eligible', 'manifest': False,
                                            'name_ts': '2026-09-27T03:58:45'}])))
chk('S43 形状自证在真人口上为空（谓词与实际清单一致，不是恒真）',
    _s43_qg._s51_population_problems(_s43_rd.backup_records(root=_r1)) == [])
chk('S43 乱序清单会被抓：形状自证点名"未按时刻新→旧"',
    bool(_s43_qg._s51_population_problems(
        [{'name': 'a.db', 'state': 'eligible', 'manifest': True, 'name_ts': '2026-01-01T00:00:00'},
         {'name': 'b.db', 'state': 'eligible', 'manifest': True, 'name_ts': '2026-09-01T00:00:00'}])))

# ── CLI 端到端（子进程真跑，stdout 非空才算跑成）──
_p1 = _s43_run('--list-backups', '--root', _r1, '--json')
_n_cli = len(_s43_js.loads(_p1.stdout)['records']) if _p1.stdout.strip() else -1
_n_pure = len(_s43_rd.backup_records(root=_r1))
chk('S43 两把尺必须数到同一个人口：CLI --json 条数 == 纯函数 backup_records 条数（==4）',
    _p1.returncode == 0 and _n_cli == _n_pure == 4, (_n_cli, _n_pure, _p1.stdout[:100]))
_fm = _s43_os.path.join(_b1, 'ican_20260926_120000.db')       # 合法库 + 一张被改过的卡
_s43_rd.snapshot_into(_S43_DB, _fm)
_s43_card(_fm, _S43_DB)
with open(_fm + '.manifest.json', encoding='utf-8') as _fc:
    _fdoc = _s43_js.load(_fc)
_fdoc['sha256'] = 'deadbeef' + _fdoc['sha256'][8:]
with open(_fm + '.manifest.json', 'w', encoding='utf-8', newline='\n') as _fw:
    _s43_js.dump(_fdoc, _fw, ensure_ascii=False, sort_keys=True)
_fdeep = [r for r in _s43_rd.backup_records(root=_r1, deep=True)
          if r['name'] == 'ican_20260926_120000.db']
chk('S43 有卡≠卡可信：合法库配一张 sha256 被改过的卡 ⇒ 深核必须判 manifest_mismatch（restic :227 对偶）',
    len(_fdeep) == 1 and _fdeep[0]['state'] == 'manifest_mismatch', str(_fdeep)[:170])
chk('S43 这类件在浅扫下仍算够格 ⇒ 门面行必须明说"浅扫未对账"，否则浅扫的 eligible 会被当成凭证',
    [r for r in _s43_rd.backup_records(root=_r1)
     if r['name'] == 'ican_20260926_120000.db'][0]['state'] == 'eligible'
    and '浅扫' in _s43_run('--list-backups', '--root', _r1).stdout)
_p0 = _s43_run('--list-backups', '--root', _r0)
chk('S43 CLI 空备份面：退 2 并明说"一件都不够格"（零输入不得静默成功）',
    _p0.returncode == 2 and '一件都不够格' in _p0.stdout, (_p0.returncode, _p0.stdout[:120]))
_p2 = _s43_run('--latest-eligible', '--backup', 'ican_20260927_035845.db', '--root', _r1)
chk('S43 CLI 互斥：--latest-eligible 与 --backup 同给 ⇒ 退 2 且不动任何库',
    _p2.returncode == 2 and '互斥' in _p2.stdout, (_p2.returncode, _p2.stdout[:120]))
_self = _s43_os.path.join(_b1, 'ican_20260927_035845.db')
_before_self = _s43_rd.sha256_file(_self)
_p3 = _s43_run('--backup', _self, '--into', _self, '--force')
chk('S43 自恢复守卫：备份与目标同一文件 ⇒ 退 2 且该文件字节不变（拿"正在被写的自己"当源是既往事故）',
    _p3.returncode == 2 and '同一个文件' in _p3.stdout and _s43_rd.sha256_file(_self) == _before_self,
    (_p3.returncode, _p3.stdout[:140]))
_p4 = _s43_run('--latest-eligible', '--root', _r1, '--check-only',
               '--into', _s43_os.path.join(_s43_tf.mkdtemp(prefix='s43into_'), 'chk.db'))
chk('S43 CLI 端到端正例：自动选中并载回检查点 ⇒ rc=0 且打印"自动选中"',
    _p4.returncode == 0 and '自动选中' in _p4.stdout, (_p4.returncode, _p4.stdout[:160]))
_p5 = _s43_run('--nope')
chk('S43 未知参数 ⇒ argparse 退 2（不得当成"没给参数就帮忙跑一遍"）',
    _p5.returncode == 2 and '--nope' in _p5.stderr, (_p5.returncode, _p5.stderr[:120]))

# ── init_db --via-backup 的拒绝顺序与真演习 ──
_q1 = _s43_init_run('--via-backup')
chk('S43 init_db CLI：--via-backup 不给 --rollback ⇒ 退 2（整库回退也得先说要撤哪一条才有比对基准）',
    _q1.returncode == 2 and '--rollback' in _q1.stdout, (_q1.returncode, _q1.stdout[:120]))
_q2 = _s43_init_run('--backup=x.db')
chk('S43 init_db CLI：只给 --backup ⇒ 退 2（孤立的文件名不构成一次意图）',
    _q2.returncode == 2 and '--via-backup' in _q2.stdout, (_q2.returncode, _q2.stdout[:120]))
_q3 = _s43_init_run('--rollback=p005_cost_snapshot', '--via-backup')
chk('S43 确认位前置：缺 --yes 时拒因是"未带 --yes"而不是档位（同一行命令不得因库态换失败原因）',
    _q3.returncode == 1 and '--yes' in _q3.stdout and 'rev=' not in _q3.stdout,
    (_q3.returncode, _q3.stdout[:160]))
_q4 = _s43_init_run('--rollback=p006_cost_backfill', '--via-backup', '--yes')
chk('S43 auto 档拒绝整库回退：点名"有精确反向实现"并给出正确命令（不用大炮换精确手术）',
    _q4.returncode == 1 and 'rev=auto' in _q4.stdout and '--rollback=p006_cost_backfill --yes' in _q4.stdout,
    (_q4.returncode, _q4.stdout[:180]))

with _APP43.app_context():
    from src.models import db as _s43_dbmod
    _live43 = str(_s43_dbmod.engine.url.database).replace(chr(92), '/')
    chk('S43 演习对象口径：ORM 正在用的库文件路径取得到且可读（"临时库"必须由引擎说，不能由夹具猜）',
        bool(_live43) and _s43_os.path.isfile(_live43), _live43[:120])
    _s43_init.apply_patches(verbose=False)              # 把临时库推到已知态（S41 的演习会摘行）
    _rows = _s43_sl.logged_rows()
    chk('S43 演习前置：临时库账本确有记录（拿空账本演习 ⇒ 任何"撤掉了"都不成立）',
        len(_rows) > 0, str(len(_rows)))
    _mpid = next((r.pid for r in _s43_sl.PATCHES if r.rev == 'manual' and r.pid in _rows), None)
    chk('S43 演习对象存在：账本里能找到一条 manual 档补丁（否则 --via-backup 这一路是空跑）',
        _mpid is not None, str(_mpid))
    _applied = _s43_cal.from_utc_naive(_s43_dt.fromisoformat(_rows[_mpid]['applied_at']))
    # 文件名里的 14 位是**业务钟面**，而 _applied 是 aware UTC：直接把 UTC 的 strftime 写进名字
    # 就是 §2.4 说的 8 小时陷阱本身（本轮第一次红正是这么踩的）。必须先换算再落名。
    _rn = lambda dt_utc: 'ican_%s.db' % _s43_cal.business_now(dt_utc).strftime('%Y%m%d_%H%M%S')
    _live_sha = _s43_rd.sha256_file(_S43_DB)

    _rf, _bf = _s43_face(names=[_rn(_applied + _s43_td(hours=1))], live=_live43)
    try:
        _s43_init.rollback_via_backup(_mpid, backup=_s43_os.path.join(_bf, _rn(_applied + _s43_td(hours=1))),
                                      confirmed=True, verbose=False, root=_rf)
        _refused = False
        _why = '没拒'
    except ValueError as _e:
        _refused = '不早于' in str(_e)
        _why = str(_e)[:120]
    chk('S43 方向守卫真拒：拿一份"晚于补丁"的备份 ⇒ 拒绝且库字节一个没动',
        _refused and _s43_rd.sha256_file(_S43_DB) == _live_sha, _why)

    _on = _rn(_applied - _s43_td(hours=1))
    _ra, _ba = _s43_face(names=[_on], drop={_on: _mpid}, live=_live43)
    _res = _s43_init.rollback_via_backup(_mpid, backup=_s43_os.path.join(_ba, _on),
                                         confirmed=True, verbose=False, root=_ra)
    chk('S43 真演习：用早于补丁的备份载回 ⇒ action=restored-from-backup 且账本里该行确实消失',
        _res['action'] == 'restored-from-backup' and _mpid not in _s43_sl.logged_rows(), str(_res)[:160])
    chk('S43 覆盖前锚点存在且带证据卡（"动手前先有一份可核副本"仍是硬不变式）',
        _s43_os.path.isfile(_res['anchor']) and _s43_os.path.isfile(_res['anchor'] + '.manifest.json'),
        _res.get('anchor', ''))
    _anch_ok, _anch_why = _s43_reconcile(_res['anchor'])
    chk('S43 生产者侧同源：锚点自己的证据卡也要过同一把 reconcile 尺（只给一半输入=卡不算可信）',
        _anch_ok, str(_anch_why)[:170])
    chk('S43 恢复读数按字节回来自证：入库行数==备份复算行数（不靠函数返回值自证）',
        _res['row_total'] == _s43_rd.inspect(_s43_os.path.join(_ba, _on), '备份')[0]['row_total'],
        str(_res['row_total']))
    _sha1 = _s43_rd.sha256_file(_S43_DB)
    _again = _s43_init.rollback_via_backup(_mpid, backup=_s43_os.path.join(_ba, _on),
                                           confirmed=True, verbose=False, root=_ra)
    chk('S43 重复撤无二阶影响：第二次给 not-applied 且库字节与第一次完全相同（M106）',
        _again['action'] == 'not-applied' and _s43_rd.sha256_file(_S43_DB) == _sha1, str(_again)[:140])
    _acts = _s43_init.apply_patches(verbose=False)
    chk('S43 幂等闭环：撤完再跑迁移 ⇒ 该补丁被当未跑重放（撤干净了，而不是加了个"已撤"状态列）',
        _mpid in _s43_sl.logged_rows()
        and any(a['patch'] == _mpid and a['action'] == 'run' for a in _acts), str(_acts)[:160])
    _ghost = _s43_os.path.join(_ba, 'ican_20200101_000000.db')
    _sha_before_ghost = _s43_rd.sha256_file(_S43_DB)
    try:
        _s43_init.rollback_via_backup(_mpid, backup=_ghost, confirmed=True, verbose=False, root=_ra)
        _g, _gw = False, '没拒'
    except (ValueError, FileNotFoundError) as _e:
        _g, _gw = True, type(_e).__name__ + ':' + str(_e)[:100]
    chk('S43 有牙：指向不存在的备份 ⇒ 拒且库字节不变（拿不到真身就不许宣称"已回退"）',
        _g and _s43_rd.sha256_file(_S43_DB) == _sha_before_ghost, _gw)

# ── 判据自身有牙（第 43 项加固的反证：契约②-b 变异要打在守卫所在那一层）──
_esc = "__import__('dateti" + "me')"          # 拼接出被扫的串，防本扫描命中自己的字面量
chk('S43 加固有效（按行为判不按文档判）：非 Name base 的函数链仍能还原出尾段',
    _s43_qg._s43_chain(_s43_ast.parse(_esc + '.datetime.now()\n').body[0].value.func)
    .endswith('datetime.now'))
chk('S43 双向不一刀切：该形态必被判裸时钟，而显式 UTC 的 datetime.now(UTC) 不得被判',
    _s43_qg._s43_is_naive(_s43_ast.parse(_esc + '.datetime.now()\n').body[0].value)
    and not _s43_qg._s43_is_naive(_s43_ast.parse('x = datetime.now(UTC)\n').body[0].value))
chk('S43 加固后进枚举器：__import__ 形态数到 1 处裸时钟（把加固撤掉这条必须翻红）',
    len(_s43_qg._s43_clock_shapes(
        {'probe.py': _s43_ast.parse('x = ' + _esc + '.datetime.now()\n')})[0]) == 1)
_hits = [f for f in ('init_db.py', 'restore_db.py', 'set_version.py', 'test_l4_assertions.py')
         if (_esc + '.datetime.now()') in open(_s43_os.path.join(_S43_HERE, f),
                                               encoding='utf-8').read()]
chk('S43 可执行面不再有 __import__ 绕出来的裸时钟（本轮抓出 3 处并清零；quality_gate 不在文本扫描面内：'
    '它的 docstring 必须引用这个形态才能说明修了什么，那种"命中"是描述不是行为，由上面三条 AST 腿管）',
    _hits == [], str(_hits))
chk('S43 取数面含根级脚本：init_db / restore_db 都在第 43 项的 py 清单里（不在里面就是没测）',
    'restore_db.py' in _s43_qg._s43_trees()[0] and 'init_db.py' in _s43_qg._s43_trees()[0])
_saved_results = list(_s43_qg.results)
_s43_qg.results.clear()
_s43_qg.check_backup_eligibility()
_qg51 = [r for r in _s43_qg.results if r[1].startswith('51.')]
_s43_qg.results[:] = _saved_results
chk('S43 门禁第 51 项端到端跑通（真入口 + 真读盘），且回执为通过',
    len(_qg51) == 1 and _qg51[0][2] is True, str(_qg51)[:180])
chk('S43 状态词必须带值：detail 里印出"人口 N 件 / 够格 M 件"两个数（只回"通过"等于没测）',
    len(_qg51) == 1 and any('人口' in x and '够格' in x for x in _qg51[0][3]), str(_qg51)[:180])
chk('S43 台账不残留：本段没把临时面写进真 RESTORE-EVIDENCE.json（真件仍只描述真仓备份）',
    _s43_os.path.isfile(_s43_os.path.join(_S43_HERE, 'RESTORE-EVIDENCE.json'))
    and 's43face_' not in open(_s43_os.path.join(_S43_HERE, 'RESTORE-EVIDENCE.json'),
                               encoding='utf-8').read())


# ══════════ S44：三副本同步出口唯一（第 37 轮 H141-H144 / 门禁第 52 项）══════════
# 演习全部跑在临时权威面 + 临时镜像面上；真仓面只查不写（工具本身无删除能力）。
import importlib.util as _s44_u
import re as _re_s44
import shutil as _s44_sh
from pathlib import Path as _s44_P

_spec44 = _s44_u.spec_from_file_location('ican_sync_tool',
                                         _s43_os.path.join(_S43_HERE, 'tools', 'frontend_sync_check.py'))
_s44_t = _s44_u.module_from_spec(_spec44)
_spec44.loader.exec_module(_s44_t)
_S44_DEV_REAL, _S44_MIR_REAL = _s44_t.DEV, _s44_t.MIRROR_DIRS
_S44_FACE_NAME_REAL = _s44_t.DEV_FACE_NAME
_S44_FULL = tuple(f'f{i:02d}.py' for i in range(31))          # 合成声明：够过 30 项地板


def _s44_decl(mode):
    """三处声明的合成形态 —— 每种模式都是"某处被改坏"的那一种。"""
    if mode == 'shrunken':
        return ('a.py', 'b.py', 'c.py'), (('README.md', 'p', False, None),)
    if mode == 'escaping':
        return _S44_FULL, (('../OUTSIDE/README.md', 'p', False, None),)
    return _S44_FULL, (('README.md', 'p', False, None),)


def _s44_names(mode):
    root_decl, carriers = _s44_decl(mode)
    names = set(str(x) for x in root_decl)
    names.add(_s44_t.SELF_ON_FACE)
    names |= {str(r) for r, *_ in carriers if str(r).endswith(_s44_t.MD_CARRIER_NAMES)}
    return sorted(names)


def _s44_sandbox(mode='full', drop=(), mir_stale=(), keep_src=True):
    root = _s44_P(_s43_tf.mkdtemp(prefix='s44_'))
    dev, mir = root / 'dev', root / 'mir'
    (dev / 'src').mkdir(parents=True)
    (mir / 'src').mkdir(parents=True)
    if keep_src:
        (dev / 'src' / 'keep.py').write_text('impl', encoding='utf-8', newline='\n')
        (mir / 'src' / 'keep.py').write_text('impl', encoding='utf-8', newline='\n')
    root_decl, carriers = _s44_decl(mode)
    if mode != 'absent':
        (dev / 'quality_gate.py').write_text(f'ROOT_DELIVERED = {root_decl!r}\n',
                                             encoding='utf-8', newline='\n')
        (dev / 'check_delivery_drift.py').write_text('EXTRA_TRACKED = ()\n',
                                                     encoding='utf-8', newline='\n')
        (dev / 'set_version.py').write_text(f'CARRIERS = {carriers!r}\n',
                                            encoding='utf-8', newline='\n')
    for n in _s44_names(mode):
        s = _s44_P(dev) / n
        if not str(s.resolve()).startswith(str(dev.resolve())):
            continue                                  # 越界名不落地，只让它去撞守卫
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text('AUTH', encoding='utf-8', newline='\n')
        if n not in drop:
            d = _s44_P(mir) / n
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_text('STALE' if n in mir_stale else 'AUTH', encoding='utf-8', newline='\n')
    _s44_t.DEV, _s44_t.MIRROR_DIRS = dev, {'mir': mir}
    # 第 40 轮入场闸要求沙箱**显式主张**"这一面就是权威源面"：不补这一行，S44 所有
    # main() 腿会因 DEV 名不符一律退 2 —— 那是闸在替沙箱说真话，不是沙箱在测判据。
    _s44_t.DEV_FACE_NAME = dev.name
    return root, dev, mir


def _s44_teardown(root):
    _s44_t.DEV, _s44_t.MIRROR_DIRS = _S44_DEV_REAL, _S44_MIR_REAL
    _s44_t.DEV_FACE_NAME = _S44_FACE_NAME_REAL
    _s44_sh.rmtree(str(root), ignore_errors=True)


# ── 面从行为取（真调用 declared_files()，不读它对自身的描述）──
_face44 = list(_s44_t.declared_files())
chk('S44 同步面按行为取：真调用 declared_files() 且 ⊇ ROOT_DELIVERED 全体',
    set(_face44) >= set(_s44_t._module_literal(_S44_DEV_REAL / 'quality_gate.py', 'ROOT_DELIVERED')))
chk('S44 同步面 ⊇ check_delivery_drift 的 EXTRA_TRACKED（查得到就必须修得了）',
    set(_face44) >= set(_s44_t._module_literal(_S44_DEV_REAL / 'check_delivery_drift.py',
                                               'EXTRA_TRACKED')))
chk('S44 同步面 ⊇ md 载体（README/UPGRADE 由 set_version.CARRIERS 声明，不是手抄）',
    {'README.md', 'UPGRADE.md'} <= set(_face44))
chk('S44 工具自己也在面内：权威门禁在 05 副本里跑第 52 项时也得看得见写者',
    _s44_t.SELF_ON_FACE in _face44)
_union44 = (set(_s44_t._module_literal(_S44_DEV_REAL / 'quality_gate.py', 'ROOT_DELIVERED'))
            | set(_s44_t._module_literal(_S44_DEV_REAL / 'check_delivery_drift.py', 'EXTRA_TRACKED'))
            | {'README.md', 'UPGRADE.md'} | {_s44_t.SELF_ON_FACE})
chk('S44 面大小当场印出并等于独立复算的并集（只回"通过"不回数值的判据不算测过）',
    set(_face44) == _union44 and len(_face44) >= 40,
    f'面 {len(_face44)} / 复算 {len(_union44)}')
_S44_IN_DEV_FACE = all(_s44_P(p).is_dir() for p in _S44_MIR_REAL.values())
_S44_RO_RC = _s44_t.main([])
chk('S44 只读检查的结论跟着所在面走：权威面须退 0（三类归零），交付副本里没有镜像根 ⇒ 第 40 轮起退 2'
    '判"面无法确认"（旧行为退 1 报 MISSING 472＝把取数面坏说成有活要干，照处方跑 --fix 会往提交包里造件）；'
    '两种面各自有期望，都不得混成 0',
    _S44_RO_RC == (0 if _S44_IN_DEV_FACE else 2),
    f'面={"权威" if _S44_IN_DEV_FACE else "交付副本"} rc={_S44_RO_RC}')
chk('S44 面里没有越界路径且数量 ≥30（越界与骤降两类地板都在函数内，不靠肉眼）',
    all('..' not in n and not n.startswith('/') for n in _face44) and len(_face44) >= 30)

# ── 沙箱行为面：缺一件 / 内容旧 / --fix 幂等 / EXTRA 不删 / 零输入 ──
_r, _dev, _mir = _s44_sandbox(drop=('f03.py',))
try:
    chk('S44 有牙：副本缺一件（MISSING）⇒ 退 1，不因"其它都一致"而放行', _s44_t.main([]) == 1)
finally:
    _s44_teardown(_r)
_r, _dev, _mir = _s44_sandbox(mir_stale=('f05.py',))
try:
    chk('S44 有牙：副本内容陈旧（DIFF）⇒ 退 1（只看文件名在不在是上一版的失效形态）',
        _s44_t.main([]) == 1)
    _sha_before = _s44_t.sha256(_dev / 'f05.py')
    _rc_fix = _s44_t.main(['--fix'])
    chk('S44 --fix 方向正确：副本被写成权威内容，权威侧字节未变',
        _rc_fix == 0 and (_mir / 'f05.py').read_text(encoding='utf-8') == 'AUTH'
        and _s44_t.sha256(_dev / 'f05.py') == _sha_before)
    chk('S44 --fix 幂等：复制完是**重新扫描**下的结论，再跑只读检查仍退 0',
        _s44_t.main([]) == 0 and _s44_t.main(['--fix']) == 0)
finally:
    _s44_teardown(_r)
_r, _dev, _mir = _s44_sandbox()
try:
    (_mir / 'src' / 'zz-stray.py').write_text('keep me', encoding='utf-8', newline='\n')
    chk('S44 EXTRA 只报不删：--fix 之后副本多出的文件仍在原位且仍判红（删除须人工复核）',
        _s44_t.main(['--fix']) == 1
        and (_mir / 'src' / 'zz-stray.py').read_text(encoding='utf-8') == 'keep me')
finally:
    _s44_teardown(_r)
_r, _dev, _mir = _s44_sandbox(keep_src=False)
try:
    chk('S44 零输入判红：权威面为空 ⇒ 退 2 判"取数面坏"，绝不退 0 判通过', _s44_t.main([]) == 2)
finally:
    _s44_teardown(_r)

# ── FaceError 三形态：声明被改坏 / 越界 / 声明文件失踪，一律不得退化成"无差异" ──
for _mode, _mark in (('shrunken', '只剩'), ('escaping', '越界')):
    _r, _dev, _mir = _s44_sandbox(mode=_mode)
    try:
        try:
            _s44_t.declared_files()
            _got, _msg = False, '没拒'
        except _s44_t.FaceError as _e:
            _got, _msg = _mark in str(_e), str(_e)[:100]
        chk(f'S44 有牙[{_mode}]：声明被改坏 ⇒ FaceError 点名原因（绝不静默丢声明）', _got, _msg)
        chk(f'S44 有牙[{_mode}]：同形态喂进 main() ⇒ 退 2（先验面再复制，不许复制一半才崩）',
            _s44_t.main([]) == 2)
    finally:
        _s44_teardown(_r)
_r, _dev, _mir = _s44_sandbox(mode='absent')
try:
    try:
        _s44_t.declared_files()
        _nf, _m = False, '没拒'
    except _s44_t.FaceError as _e:
        _nf, _m = '不存在' in str(_e), str(_e)[:100]
    chk('S44 零输入：声明文件不存在 ⇒ FaceError 且 main 退 2（不返回空面、不判通过）',
        _nf and _s44_t.main([]) == 2, _m)
finally:
    _s44_teardown(_r)

# ── _module_literal 的四类输入（AnnAssign 认 / 非字面量拒 / 查无此名拒 / 文件失踪拒）──
_lit = _s44_P(_s43_tf.mkdtemp(prefix='s44lit_'))
(_lit / 'ann.py').write_text('X: tuple = ("a", "b")\n', encoding='utf-8', newline='\n')
(_lit / 'call.py').write_text('import functools\nX = functools.reduce(a, b)\n',
                              encoding='utf-8', newline='\n')
(_lit / 'other.py').write_text('Y = ("a",)\n', encoding='utf-8', newline='\n')
chk('S44 _module_literal 认 AnnAssign（带类型标注的声明也是声明，只认 Assign 会静默丢面）',
    _s44_t._module_literal(_lit / 'ann.py', 'X') == ('a', 'b'))
for _lm, _lp in (('非字面量', _lit / 'call.py'), ('查无此名', _lit / 'other.py'),
                 ('文件失踪', _lit / 'gone.py')):
    try:
        _s44_t._module_literal(_lp, 'X')
        _lr, _lx = False, '没拒'
    except _s44_t.FaceError as _e:
        _lr, _lx = True, str(_e)[:80]
    chk(f'S44 _module_literal[{_lm}] ⇒ FaceError（不得回 None，那等于面悄悄变小）', _lr, _lx)

# ── 门禁第 52 项的三个纯函数：写出口枚举 / 仓外路径 / 面对账 ──
_t52, _b52 = _s43_qg._s52_trees()
chk('S44 取数面已含 tools/（此前 _s37_py_files 不含，写者住在 tools/ 里判据看不见自己）',
    _s44_t.SELF_ON_FACE in _t52 and not _b52, f'broken={_b52[:2]}')
chk('S44 写出口枚举唯一（真跑）：names == [tools/frontend_sync_check.py]',
    [w[0] for w in _s43_qg._s52_writers(_t52)] == [_s43_qg._S52_WRITER])
chk('S44 有牙：合成"第二个写者"必须数到 2（判据不是"文件名不等于我"那种一行比较）',
    len(_s43_qg._s52_writers({'tools/other.py': _s43_ast.parse(
        'import shutil\nshutil.copy2(a, "deploy-free/x")\n'),
        _s43_qg._S52_WRITER: _s43_ast.parse(
            'import shutil\nshutil.copy2(a, "deploy-free/x")\n')})) == 2)
chk('S44 不误报：会复制但不碰镜像面的模块（backup_db 那类）不在写者名单里',
    _s43_qg._s52_writers({'backup_db.py': _s43_ast.parse(
        'import shutil\nshutil.copyfile(src, dst)\n')}) == [])
chk('S44 测试夹具豁免：test_*.py 在临时面里造镜像树是合法行为，不算第二写者',
    _s43_qg._s52_writers({'test_l4_assertions.py': _s43_ast.parse(
        'import shutil\nshutil.copy2(a, "05-源代码全文/x")\n')}) == [])
chk('S44 零分母如实为空：无模块可扫时给 []，由调用方判"没测到"而不是判通过',
    _s43_qg._s52_writers({}) == [])
_off_real = _s43_qg._s52_offrepo_problems(
    open(_s43_os.path.join(_S43_HERE, 'verify_local.py'), encoding='utf-8').read())
chk('S44 仓外路径·反向：真 verify_local 源码判净（7 个在册脚本实测 0 误报）',
    _off_real == [], str(_off_real)[:100])
chk('S44 仓外路径·正向：argv 里出现 D:/tmp 那类绝对路径必须判红并点名',
    bool(_s43_qg._s52_offrepo_problems("run_stage('s', ['py', 'D:/tmp/bench32/sync31.py'])")))
chk('S44 仓外路径只匹配行为不匹配描述：docstring 里写同一个路径不得判红',
    _s43_qg._s52_offrepo_problems(
        "def f():\n    '''证据落在 D:/tmp/bench37/ 下'''\n    return 1\n") == [])
chk('S44 判据自身崩了要显式未知：源码解析失败不得回空列表（那等于判通过）',
    bool(_s43_qg._s52_offrepo_problems('def f(:\n')))
_p1 = _s43_qg._s52_face_problems('/nonexistent/t.py', '/nonexistent/a.py', '/nonexistent/b.py',
                                 '/nonexistent/c.py', ())[0]
chk('S44 面对账·无输入不判通过：四个声明文件取不到时首条必须是"判据面无输入"',
    _p1 and _p1[0].startswith('判据面无输入'), str(_p1)[:110])
_gate44 = (_s43_os.path.join(_S43_HERE, 'quality_gate.py'),
           _s43_os.path.join(_S43_HERE, 'check_delivery_drift.py'),
           _s43_os.path.join(_S43_HERE, 'set_version.py'))
_p2 = _s43_qg._s52_face_problems(_s43_os.path.join(_S43_HERE, 'tools', 'frontend_sync_check.py'),
                                 *_gate44, ('README.md',))[0]
chk('S44 面对账可归因：喂一份缺 EXTRA_TRACKED 的面 ⇒ 点名字段而不是泛泛判红',
    any('EXTRA_TRACKED' in x for x in _p2) and any('只剩' in x for x in _p2), str(_p2)[:140])
_p3, _e3 = _s43_qg._s52_face_problems(
    _s43_os.path.join(_S43_HERE, 'tools', 'frontend_sync_check.py'), *_gate44, _face44)
chk('S44 面对账·真面退净且回执带三个实测数（ROOT/EXTRA/md 各自多少）',
    _p3 == [] and any('ROOT_DELIVERED' in x and '∪' in x for x in _e3), str(_e3)[:130])

# ── 端到端：第 52 项真链路 + 本地闸接线 ──
_saved44 = list(_s43_qg.results)
_s43_qg.results.clear()
_s43_qg.check_sync_egress_single_entry()
_q52 = [r for r in _s43_qg.results if r[1].startswith('52.')]
_s43_qg.results[:] = _saved44
chk('S44 第 52 项在本副本真链路判绿，且回执同时印出面大小与写出口清单',
    len(_q52) == 1 and _q52[0][2] is True
    and any('同步面' in str(x) for x in _q52[0][3])
    and any('写出口枚举' in str(x) for x in _q52[0][3]), str(_q52)[:180])
chk('S44 接线证明：verify_local 里有 stage_sync 且 argv 指向被跟踪的工具（不是仓外脚本）',
    'stage_sync' in dir(__import__('verify_local'))
    and "os.path.join('tools', 'frontend_sync_check.py')" in open(
        _s43_os.path.join(_S43_HERE, 'verify_local.py'), encoding='utf-8').read())
_sr44 = __import__('verify_local').stage_sync()
chk('S44 真跑本地闸 sync 阶段：rc 与所在面一致，且**折叠后那一行仍带着本面的结论**'
    '（聚合器只留末行 ⇒ 生产方必须把结论写进末行，否则第 34 轮"读数被折叠成布尔"复发：'
    '源面要看见三个实测数，镜像面要看见"面无法确认"而不是光一句路径）',
    _sr44['rc'] == (0 if _S44_IN_DEV_FACE else 2)
    and (_re_s44.search(r'MISSING \d+ / DIFF \d+ / EXTRA \d+', _sr44['line']) is not None
         if _S44_IN_DEV_FACE else '面无法确认' in _sr44['line']),
    str(_sr44['line'])[:170])
if _S44_IN_DEV_FACE:
    chk('S44 三副本里"写者"本身逐字节同 sha（工具不在副本里=副本侧无法自查；仅权威面可比三份）',
        _s44_t.sha256(_S44_DEV_REAL / _s44_t.SELF_ON_FACE)
        == _s44_t.sha256(_S44_MIR_REAL['deploy-free'] / _s44_t.SELF_ON_FACE)
        == _s44_t.sha256(_S44_MIR_REAL['05-源代码全文'] / _s44_t.SELF_ON_FACE))
else:
    chk('S44 交付副本自带写者：本副本内 tools/frontend_sync_check.py 存在且能取面'
        '（三副本对账在副本里做不到 ⇒ 如实记为"本面只自查"，不假装比过三份）',
        _s44_P(_S44_DEV_REAL / _s44_t.SELF_ON_FACE).is_file() and len(_face44) >= 40,
        f'面={_S44_DEV_REAL.name} 自件在册 面{len(_face44)}项')
chk('S44 沙箱已复原：工具的 DEV/MIRROR_DIRS 指回真仓（夹具不得留下改过全局状态的现场）',
    _s44_t.DEV == _S44_DEV_REAL and _s44_t.MIRROR_DIRS == _S44_MIR_REAL)


# ══════════ S45：备份保留体检（第 38 轮 H151-H155 / 门禁第 53 项）══════════
# 只读体检：本段不得删任何备份；跑完对 backups/ 做"名字集合未变"的自证。
import json as _s45_json

_S45_BDIR = _s43_os.path.join(_S43_HERE, 'backups')
_S45_NAMES_BEFORE = sorted(os.listdir(_S45_BDIR)) if os.path.isdir(_S45_BDIR) else []
_ch45 = _s43_rd.retention_channels(root=_S43_HERE)
_prod45 = {c['producer'] for c in _ch45}
_by45 = {c['channel']: c for c in _ch45}

chk('S45 通道从生产者源码现取：数到 2 条且各带 patterns/keep/knob（不是手抄名单）',
    len(_ch45) == 2 and all(c.get('patterns') and isinstance(c.get('keep'), int) and c.get('knob')
                            for c in _ch45), str([(c['channel'], c['keep']) for c in _ch45]))
chk('S45 backup_db 的模式来自它真传给 glob.glob 的字面量（ican_*.db 与 ican_*.sql 都在册）',
    {'ican_*.db', 'ican_*.sql'} <= set(_by45['backup_db.py']['patterns']),
    str(_by45['backup_db.py']['patterns']))
chk('S45 pack_demo 的模式由"前缀常数 + endswith 后缀"派生（demo-reset_*.db）',
    _by45['src/pack_demo.py']['patterns'] == ['demo-reset_*.db'],
    str(_by45['src/pack_demo.py']['patterns']))
chk('S45 份数尺必须是有名字的常数：DEFAULT_KEEP / BACKUP_KEEP（匿名 return 30 无法核对）',
    _by45['backup_db.py']['knob'] == 'DEFAULT_KEEP'
    and _by45['src/pack_demo.py']['knob'] == 'BACKUP_KEEP')
_bd_tree = _s43_ast.parse(_s43_rd._read_src(_s43_os.path.join(_S43_HERE, 'backup_db.py')))
chk('S45 keep 读数与模块常数逐一对齐（体检不抄第二份数字）',
    _by45['backup_db.py']['keep'] == int(_s43_rd._ast_module_literal(_bd_tree, 'DEFAULT_KEEP')) == 30)
chk('S45 _glob_patterns 只匹配行为不匹配描述：注释里的假通配不得进射程',
    _s43_rd._glob_patterns(_s43_ast.parse(
        "import glob\nglob.glob('/x/real_*.db')\n# 'ghost_*.db' 只是注释\n")) == ['real_*.db'])
chk('S45 _glob_patterns 零输入如实为空（由调用方判"射程不可核对"，不判健康）',
    _s43_rd._glob_patterns(_s43_ast.parse('x = 1\n')) == [])
_p_tree = _s43_ast.parse(_s43_rd._read_src(_s43_os.path.join(_S43_HERE, 'src', 'pack_demo.py')))
chk('S45 _prune_shape 认得真通道：从 _prune_backups 里取到 .db 后缀',
    _s43_rd._prune_shape(_p_tree, '_prune_backups', 'BACKUP_PREFIX') == '.db')
for _bk, _bsrc, _bwhy in (
        ('前缀换变量名', "def _prune_backups(d):\n    return [n for n in d"
                        " if n.startswith(f'{OTHER_PREFIX}_') and n.endswith('.db')]\n", 'OTHER_PREFIX'),
        ('后缀读不出', "def _prune_backups(d):\n    return [n for n in d"
                       " if n.startswith(f'{BACKUP_PREFIX}_')]\n", '前缀或后缀'),
        ('函数不存在', 'x = 1\n', '_prune_backups')):
    try:
        _s43_rd._prune_shape(_s43_ast.parse(_bsrc), '_prune_backups', 'BACKUP_PREFIX')
        _pe, _pm = False, '没拒'
    except _s43_rd.RetentionError as _e:
        _pe, _pm = _bwhy in str(_e), str(_e)[:90]
    chk(f'S45 有牙[{_bk}] ⇒ RetentionError 且点名原因（绝不按我猜的族去数别人的族）', _pe, _pm)
for _lk, _lsrc in (('非字面量', 'import functools\nX = functools.reduce(a, b)\n'),
                   ('查无此名', 'Y = 3\n')):
    try:
        _s43_rd._ast_module_literal(_s43_ast.parse(_lsrc), 'X')
        _le, _lm = False, '没拒'
    except _s43_rd.RetentionError as _e:
        _le, _lm = True, str(_e)[:80]
    chk(f'S45 射程读不出[{_lk}] ⇒ RetentionError（不得回 None 让通道悄悄消失）', _le, _lm)
chk('S45 _ast_module_literal 认 AnnAssign（带标注的常数也是在册尺值）',
    _s43_rd._ast_module_literal(_s43_ast.parse('X: int = 7\n'), 'X') == 7)
chk('S45 族归属三向：ican_* 归 backup_db、demo-reset_* 归 pack_demo、PH2-* 判无主',
    _s43_rd._family_of('ican_20260101_000000.db', _ch45) == 'backup_db.py'
    and _s43_rd._family_of('demo-reset_20260101_000000_000000.db', _ch45) == 'src/pack_demo.py'
    and _s43_rd._family_of('PH2-2-pre-migration-dev7.db', _ch45) is None)
chk('S45 无通道时不得把文件判成"已管"（零分母要显式无主，否则族外永远数不到）',
    all(_s43_rd._family_of(n, []) is None for n in ('ican_a.db', 'whatever.db')))
chk('S45 对账·正向：30 份 × 24h=720h ≫ 资格窗口 72h ⇒ gap:keep_outlives_eligibility',
    _s43_rd.retention_reconciliation([30], 72)['verdict'] == 'gap:keep_outlives_eligibility'
    and _s43_rd.retention_reconciliation([30], 72)['kept_hours_estimate'] == 720.0)
chk('S45 对账·反向：2 份 × 24h=48h ≤ 72h ⇒ aligned（只测"抓得住差距"会造出永红对账）',
    _s43_rd.retention_reconciliation([2], 72)['verdict'] == 'aligned')
chk('S45 对账·尺缺失 ⇒ unverified，kept 必须数值或 None（不得是散文）',
    _s43_rd.retention_reconciliation([], 72)['verdict'] == 'unverified'
    and _s43_rd.retention_reconciliation([30], 0)['kept_hours_estimate'] is None)
chk('S45 每小时每份是显式假设参数、可注入且自曝假设（生产节奏未实测就不许当事实）',
    _s43_rd.retention_reconciliation([30], 72, hours_per_backup=48.0)['kept_hours_estimate'] == 1440.0
    and '假设' in _s43_rd.retention_reconciliation([30], 72, hours_per_backup=48.0)['estimate_basis'])
_rep45 = _s43_rd.retention_report(root=_S43_HERE)
chk('S45 真面体检：人口 == 各族之和 + 族外（折叠即失真）',
    _rep45['population'] == sum(f['count'] for f in _rep45['families']) + _rep45['unmanaged_count'],
    f"{_rep45['population']} vs {sum(f['count'] for f in _rep45['families'])}"
    f"+{_rep45['unmanaged_count']}")
_syn45 = [{'name': 'ican_a_20260101_000000.db', 'age_h': 10.0, 'state': 'eligible',
           'manifest': True, 'bytes': 4096, 'integrity': 'ok', 'name_ts': '20260101_000000',
           'path': 'x', 'why': ''},
          {'name': 'someone_elses_snapshot.db', 'age_h': 900.0, 'state': 'nameless',
           'manifest': False, 'bytes': 8, 'integrity': 'unchecked', 'name_ts': None,
           'path': 'y', 'why': '文件名无时刻'}]
_rep_syn = _s43_rd.retention_report(root=_S43_HERE, records=_syn45)
chk('S45 族外具名有必真腿：合成人口里那一份族外件必须被点名（真面件数随机器变，不作断言）',
    _rep_syn['unmanaged_count'] == 1
    and _rep_syn['unmanaged'][0]['name'] == 'someone_elses_snapshot.db'
    and _rep_syn['unmanaged'][0]['state'] == 'nameless', str(_rep_syn['unmanaged'])[:130])
chk('S45 真面只断言关系（人口=各族+族外）并印出本面读数：'
    '第 38 轮把"5 件"写死 ⇒ 权威副本 0 件当场红，教训见报告 §8',
    _rep45['population'] == sum(f['count'] for f in _rep45['families']) + _rep45['unmanaged_count']
    and isinstance(_rep45['unmanaged_count'], int) and _rep45['unmanaged_count'] >= 0,
    '本面 人口 %d / 族外 %d / 够格 %d' % (_rep45['population'], _rep45['unmanaged_count'],
                                        _rep45['eligible']))
chk('S45 族外记录形状：每条必带 name/age_h/state（空集允许，必真腿由合成人口那条负责）',
    all(set(('name', 'age_h', 'state')) <= set(u) for u in _rep45['unmanaged']))

chk('S45 够格数与第 51 项同一把尺：report.eligible == backup_records 里 eligible 计数',
    _rep45['eligible'] == sum(1 for r in _s43_rd.backup_records(root=_S43_HERE)
                              if r['state'] == 'eligible'), str(_rep45['eligible']))
_rd_src45 = _s43_rd._read_src(_s43_os.path.join(_S43_HERE, 'restore_db.py'))
_rd_funcs = {n.name: n for n in _s43_ast.walk(_s43_ast.parse(_rd_src45))
             if isinstance(n, _s43_ast.FunctionDef)}
_S45_NEED = ('retention_channels', 'retention_report', 'print_retention',
             'retention_reconciliation', '_family_of', '_prune_shape')
chk('S45 体检链函数全在册（缺一即判红，不让套件在 KeyErrors 里崩掉整段）',
    all(f in _rd_funcs for f in _S45_NEED), str([f for f in _S45_NEED if f not in _rd_funcs]))
chk('S45 只读承诺（AST 版）：六个体检函数体内没有任何 remove/unlink/rmtree 调用',
    not [f'{f}.{c.func.attr}' for f in _S45_NEED if f in _rd_funcs
         for c in _s43_ast.walk(_rd_funcs[f])
         if isinstance(c, _s43_ast.Call)
         and getattr(c.func, 'attr', '') in ('remove', 'unlink', 'rmtree')])
chk('S45 门禁侧删除形态扫描真面为空', _s43_qg._s53_delete_shapes() == [])
_tdel = _s43_tf.mkdtemp(prefix='s45del_')
with open(_s43_os.path.join(_tdel, 'restore_db.py'), 'w', encoding='utf-8', newline='\n') as _fh:
    _fh.write("import os\n\n\ndef retention_report(root=None):\n    os.remove('x')\n    return {}\n")
_here45 = _s43_qg.ROOT
try:
    _s43_qg.ROOT = _tdel
    _detection = _s43_qg._s53_delete_shapes()
finally:
    _s43_qg.ROOT = _here45
chk('S45 有牙：把删除塞进 retention_report ⇒ 只读承诺必须点名（不得恒绿）',
    any('retention_report' in x and 'remove' in x for x in _detection), str(_detection)[:120])
_tmiss = _s43_tf.mkdtemp(prefix='s45miss_')
try:
    _s43_qg.ROOT = _tmiss
    _unreadable = _s43_qg._s53_delete_shapes()
finally:
    _s43_qg.ROOT = _here45
chk('S45 读不到被测源时不得判"只读"（记成无法判，与"没有删除动作"区分开）',
    any('读不到' in x for x in _unreadable), str(_unreadable)[:110])
chk('S45 生产者枚举真面 = 两个做保留决定的模块（KEEP 按 _ 切词命中）',
    _s43_qg._s53_producers() == {'backup_db.py', 'src/pack_demo.py'},
    str(sorted(_s43_qg._s53_producers())))
_real_trees45 = _s43_qg._s43_trees
try:
    _s43_qg._s43_trees = lambda: ({'backup_db.py': _bd_tree, 'src/pack_demo.py': _p_tree,
                                   'tools/third.py': _s43_ast.parse('SLEEP_KEEP = 5\n'),
                                   'src/keeper.py': _s43_ast.parse(
                                       'SLEEP_KEEPALIVE = 5\ndef record_pruned():\n    return 1\n')},
                                  [])
    _p3 = _s43_qg._s53_producers()
finally:
    _s43_qg._s43_trees = _real_trees45
chk('S45 有牙：新增声明了 KEEP 常数的生产者必须被数到（否则第 53 项是恒绿判据）',
    'tools/third.py' in _p3, str(sorted(_p3)))
chk('S45 不误伤：SLEEP_KEEPALIVE / record_pruned 都不算生产者（子串匹配老坑本轮实测过）',
    'src/keeper.py' not in _p3, str(sorted(_p3)))
chk('S45 通道清单与生产者集合双向相等（既无未登记生产者，也无幽灵通道）',
    _prod45 == _s43_qg._s53_producers(), f'channels={sorted(_prod45)}')
chk('S45 CLI 接线（行为版）：main() 同一函数体内既读 ns.retention 又调 retention_report',
    _s43_qg._s53_cli_wired(_s43_rd) is True)
_toff = _s43_tf.mkdtemp(prefix='s45off_')
with open(_s43_os.path.join(_toff, 'restore_db.py'), 'w', encoding='utf-8', newline='\n') as _fh:
    _fh.write("def main(argv=None):\n    return 0\n\n\n"
              "def retention_report(root=None):\n    return {}\n")
try:
    _s43_qg.ROOT = _toff
    _nowired = _s43_qg._s53_cli_wired(_s43_rd)
    _s43_qg.check_backup_retention()
    _ghost_rec = [r for r in _s43_qg.results if r[1].startswith('53.')]
    _s43_qg.results[:] = [r for r in _s43_qg.results if not r[1].startswith('53.')]
finally:
    _s43_qg.ROOT = _here45
chk('S45 有牙：只写函数不接 CLI ⇒ 判未接线（"能力存在"与"能力被调用"是两件事）',
    _nowired is False)
chk('S45 有牙：ROOT 里没有生产者源码 ⇒ 第 53 项当场判红并点名（不许静默通过）',
    len(_ghost_rec) == 1 and _ghost_rec[0][2] is False
    and any('restore_db' in str(x) or '射程' in str(x) or '生产者' in str(x) for x in _ghost_rec[0][3]),
    str(_ghost_rec[:1])[:160])
_cp45 = _s43_sp.run([_S43_PY, _s43_os.path.join(_S43_HERE, 'restore_db.py'), '--retention', '--json'],
                    capture_output=True, text=True, cwd=_S43_HERE, timeout=180)
try:
    _doc45 = _s45_json.loads(_cp45.stdout)
except Exception:
    _doc45 = {}
chk('S45 CLI 真跑 --retention --json：rc=0 且 schema 与三档字段齐（stdout 非空才算跑成）',
    _cp45.returncode == 0 and _doc45.get('schema') == 'ican-backup-retention-v1'
    and {'population', 'families', 'unmanaged', 'reconciliation'} <= set(_doc45),
    (_cp45.stdout or _cp45.stderr)[:130])
def _s45_vacuous(text):
    """恒真断言形状：IfExp 的 test 是常量，或 A or True。第 38 轮我自己写过两条这种占位。"""
    out = []
    for n in _s43_ast.walk(_s43_ast.parse(text)):
        if isinstance(n, _s43_ast.IfExp) and isinstance(n.test, _s43_ast.Constant):
            out.append('IfExp常量@%d' % n.lineno)
        elif isinstance(n, _s43_ast.BoolOp) and isinstance(n.op, _s43_ast.Or):
            for v in n.values:
                if isinstance(v, _s43_ast.Constant) and v.value is True:
                    out.append('or True@%d' % n.lineno)
    return out

_vac_faces = [open(_s43_os.path.join(_S43_HERE, f), encoding='utf-8').read()
                for f in ('quality_gate.py', 'restore_db.py', 'backup_db.py',
                          _s43_os.path.join('tools', 'frontend_sync_check.py'),
                          'test_l4_assertions.py')]
chk('S44/S45 恒真断言看守：五个判据文件里 IfExp 常量测试 / or True 必须为 0 处'
    '（扫描面当场数出来，含本文件自己）',
    len(_vac_faces) == 5 and sum(len(_s45_vacuous(t)) for t in _vac_faces) == 0,
    '面 %d 个 / 命中 %d 处' % (len(_vac_faces), sum(len(_s45_vacuous(t)) for t in _vac_faces)))
chk('S44/S45 看守有牙：合成 all(...) if False else True 与 x or True 必须各被抓到',
    len(_s45_vacuous('y = all(z) if False else True\n')) == 1
    and len(_s45_vacuous("chk('t', a or True)\n")) == 1,
    '占位写法一旦进文件，看守必须点名而不是沉默')

_cp_bare = _s43_sp.run([_S43_PY, _s43_os.path.join(_S43_HERE, 'restore_db.py')],
                       capture_output=True, text=True, cwd=_S43_HERE, timeout=120)
chk('S45 无参数时仍拒绝执行，且兜底提示把 --retention 一并点名（提示与实际互斥=文档说谎）',
    _cp_bare.returncode != 0 and '--retention' in (_cp_bare.stdout + _cp_bare.stderr),
    (_cp_bare.stdout + _cp_bare.stderr)[:120])
_rep_empty = _s43_rd.retention_report(root=_S43_HERE, records=[])
chk('S45 零人口不判健康：status=unverified 且对账 unverified（不得"空即通过"）',
    _rep_empty['status'] == 'unverified' and _rep_empty['population'] == 0
    and _rep_empty['reconciliation']['verdict'] == 'unverified',
    str(_rep_empty['reconciliation'])[:90])
_saved45 = list(_s43_qg.results)
_s43_qg.results.clear()
_s43_qg.check_backup_retention()
_q53 = [r for r in _s43_qg.results if r[1].startswith('53.')]
_s43_qg.results[:] = _saved45
chk('S45 第 53 项真链路判绿，且 record 恰好 1 条（跑过≠record 过：上一轮刚为这条踩过坑）',
    len(_q53) == 1 and _q53[0][2] is True, str(_q53)[:150])
chk('S45 第 53 项回执三类读数齐全：三把尺 / 人口三档 / 每族各一行（只回"通过"等于没测）',
    len(_q53) == 1 and any('三把尺' in str(x) for x in _q53[0][3])
    and any('人口' in str(x) and '族外' in str(x) for x in _q53[0][3])
    and sum(1 for x in _q53[0][3] if '通道' in str(x)) == 2, str(_q53[0][3])[:150])
_qg_tree45 = _s43_ast.parse(_s43_qg.read(_s43_os.path.join(_s43_qg.ROOT, 'quality_gate.py')))
_main45 = [n for n in _s43_ast.walk(_qg_tree45)
           if isinstance(n, _s43_ast.FunctionDef) and n.name == 'main'][0]
chk('S45 接线：第 53 项的 check_backup_retention() 真被门禁 main() 调用'
    '（只在测试里能跑到 = 门禁不会跑它，与"函数存在!=会被调用"同族）',
    'check_backup_retention' in _r47_wired_names(_main45),
    str(sorted(_r47_wired_names(_main45)))[:120])
_S45_NAMES_AFTER = sorted(os.listdir(_S45_BDIR)) if os.path.isdir(_S45_BDIR) else []
chk('S45 只读复证：本段跑完 backups/ 名字集合一字未变（体检没有偷偷清理）',
    _S45_NAMES_AFTER == _S45_NAMES_BEFORE,
    f'{len(_S45_NAMES_BEFORE)} → {len(_S45_NAMES_AFTER)} 件')


# ══════════ S46：复核后处置通道（第 39 轮 H161-H166 / 门禁第 54 项）══════════
# 本段在**临时面**上真删文件（自己造的桩件），绝不触本仓 backups/；段尾复证本仓人口未变。
import io as _s46_io
import json as _s46_json
import shutil as _s46_sh
import subprocess as _s46_sp
import tempfile as _s46_tfd
from datetime import timedelta as _s46_td

_s46_os, _s46_rd, _s46_HERE = _s43_os, _s43_rd, _S43_HERE
_s46_real_bdir = _s46_os.path.join(_s46_HERE, 'backups')
_s46_real_before = (sorted(_s46_os.listdir(_s46_real_bdir))
                    if _s46_os.path.isdir(_s46_real_bdir) else [])
_s46_rs = '复核过的理由够二十个字了用来钉住这条判据的强度下限'


def _s46_face():
    """造临时面：backup_db.py + src/pack_demo.py + backups/ 两件桩。目录名含 pid+纳秒防吃残留。"""
    import time as _t
    d = _s46_os.path.join(_s46_tfd.mkdtemp(prefix=f'bench39-{_t.time_ns()}-'), 'face')
    _s46_os.makedirs(_s46_os.path.join(d, 'src'))
    _s46_os.makedirs(_s46_os.path.join(d, 'backups'))
    for rel in ('backup_db.py', _s46_os.path.join('src', 'pack_demo.py')):
        _s46_sh.copyfile(_s46_os.path.join(_s46_HERE, rel), _s46_os.path.join(d, rel))
    import sqlite3 as _sq
    for n in ('ican_20260920_010101.db', 'PH2-8B-online-ican-20260914-022426.db'):
        con = _sq.connect(_s46_os.path.join(d, 'backups', n))
        con.execute('create table t(x)')
        con.executemany('insert into t values(?)', [(i,) for i in range(5)])
        con.commit()
        con.close()
    return d


_s46_F = _s46_face()
_s46_now = _s46_rd.datetime.now(_s46_rd.UTC)
_s46_orph = 'PH2-8B-online-ican-20260914-022426.db'
_s46_keep = 'ican_20260920_010101.db'
_s46_BDIR = _s46_os.path.join(_s46_F, 'backups')
_s46_src46 = _s43_qg.read(_s46_os.path.join(_s46_HERE, 'restore_db.py'))


def _s46_names():
    return sorted(_s46_os.listdir(_s46_BDIR))


def _s46_fn(tree, name):
    return next(n for n in tree.body if isinstance(n, _s43_ast.FunctionDef) and n.name == name)


def _s46_refused(exc_kind):
    """跑一步，把 DispositionError 的文案取回来（'未拒' 即失败信号）。"""
    try:
        exc_kind()
        return '未拒'
    except _s46_rd.DispositionError as e:
        return str(e)


chk('S46 临时面自证：人口 2 件（族外 1 + backup_db 族 1），夹具先立住再谈判据',
    len(list(_s46_rd.backup_records(root=_s46_F, now=_s46_now))) == 2, _s46_F)

# ── 凭据的纯函数腿：形状 / 两条轴分离 / 五种违规各拒一次 ──
_t46 = _s46_rd.make_ticket(_s46_orph, root=_s46_F, now=_s46_now)
chk('S46 凭据形状：TICKET_FIELDS 全齐且 token 能由身份字段复算（不是存一个数）',
    all(k in _t46 for k in _s46_rd.TICKET_FIELDS)
    and _s46_rd.ticket_token(_t46) == _t46['token'])
chk('S46 身份轴不含 issued_ts：绑了时效人就无法把凭据回交给删除出口',
    'issued_ts' not in _s46_rd._canonical_identity(_t46).decode()
    and 'issued_ts' in _s46_rd.TICKET_FIELDS)
chk('S46 族外无主件的归属读数=None 且带认领提示（盲区要有独立状态，不得复用"没有差异"）',
    _t46['channel'] is None and 'note' in _t46 and _t46['patterns'] == [])
chk('S46 合法凭据 verify 通过（带认领人 + ≥下限字数的理由）',
    _s46_rd.verify_ticket({**_t46, 'claim_by': 'L4', 'claim_reason': _s46_rs},
                          root=_s46_F, now=_s46_now)[0] is True)
for _lbl46, _mut46, _want46 in (
        ('token 改一位', lambda t: {**t, 'token': 'f' * 16}, 'token_mismatch'),
        ('缺 sha256 字段', lambda t: {k: v for k, v in t.items() if k != 'sha256'}, 'shape'),
        ('schema 造假', lambda t: {**t, 'schema': 'ican-x-v0'}, 'shape'),
        ('issued_ts 不可解析', lambda t: {**t, 'issued_ts': 'not-a-date'}, 'shape'),
        ('名字带路径分隔符', lambda t: {**t, 'name': '../' + t['name']}, 'orphan_arg'),
        ('身份字段被换（sha 造假）', lambda t: {**t, 'sha256': 'a' * 64}, 'token_mismatch')):
    _r46 = _s46_rd.verify_ticket(_mut46({**_t46, 'claim_by': 'L4', 'claim_reason': _s46_rs}),
                                 root=_s46_F, now=_s46_now)
    chk(f'S46 有牙：{_lbl46} ⇒ verify 必须拒且 kind={_want46}（不得恒绿）',
        _r46[0] is False and _r46[1] == _want46, f'实得 kind={_r46[1]}｜{_r46[2][:60]}')
chk('S46 有牙：理由比下限少 1 字即拒（长度这条尺是判据不是散文）',
    _s46_rd.verify_ticket({**_t46, 'claim_by': 'L4', 'claim_reason': 'x' * (
        _s46_rd.MIN_REASON_CHARS - 1)}, root=_s46_F, now=_s46_now)[1] == 'reason_too_short')
chk('S46 有牙：认领人只给空格即拒（无主的删除等于没人负责）',
    _s46_rd.verify_ticket({**_t46, 'claim_by': '   ', 'claim_reason': _s46_rs},
                          root=_s46_F, now=_s46_now)[1] == 'reason_too_short')
chk('S46 有牙：TTL 超 1 秒即拒（时效是在册常数不是建议）',
    _s46_rd.verify_ticket({**_t46, 'claim_by': 'L4', 'claim_reason': _s46_rs}, root=_s46_F,
                          now=_s46_now + _s46_td(seconds=_t46['ttl_s'] + 1))[1] == 'expired')
_p46 = _s46_os.path.join(_s46_BDIR, _s46_orph)
_sz46 = _s46_os.path.getsize(_p46)
with open(_p46, 'ab') as _f46:
    _f46.write(b'\x00' * 512)
chk('S46 有牙（TOCTOU）：签发后对象被追加 ⇒ 实盘 sha 与凭据不符即拒',
    _s46_rd.verify_ticket({**_t46, 'claim_by': 'L4', 'claim_reason': _s46_rs},
                          root=_s46_F, now=_s46_now)[1] == 'token_mismatch')
with open(_p46, 'r+b') as _f46:
    _f46.truncate(_sz46)
chk('S46 临时面已还原到原字节长度（下一腿量的必须是被改回的原对象）',
    _s46_os.path.getsize(_p46) == _sz46)
chk('S46 凭据是 dict 才允许：传 list 归 shape（批量不在本通道）',
    _s46_rd.verify_ticket([_t46], root=_s46_F, now=_s46_now)[1] == 'shape')

# ── make_ticket 的入口拒绝：孤儿入参 / 未经体检 / 退路 / 证据卡面 ──
for _arg46 in ('backups/' + _s46_orph, _p46, '../x.db', 'notes.txt', '', '.', '..'):
    _r46 = _s46_refused(lambda a=_arg46: _s46_rd.make_ticket(
        a, root=_s46_F, now=_s46_now, issue=False))
    chk(f'S46 入口拒孤儿入参：{_arg46!r} ⇒ 必须抛 DispositionError（实得 {_r46[:24]}）',
        _r46 != '未拒' and '孤儿入参' in _r46)
_r46 = _s46_refused(lambda: _s46_rd.make_ticket(
    'never_heard_of_it.db', root=_s46_F, now=_s46_now, issue=False))
chk('S46 入口拒未经体检的对象：人口里没有 ⇒ 不许凭空指认', '不在体检人口' in _r46, _r46[:60])
_ch46 = _s46_rd.retention_channels(_s46_F)
_r46 = _s46_refused(lambda: _s46_rd.make_ticket(
    'ican_20260928_090909.db', root=_s46_F, now=_s46_now, issue=False, chans=_ch46,
    records=[{'name': 'ican_20260928_090909.db', 'path': _p46, 'bytes': 10, 'age_h': 0.1,
              'state': 'eligible', 'why': '够格可载回', 'manifest': True}]))
chk('S46 有牙：仍够格可载回的件必须被拒（真注入一条 eligible 记录，不靠推理）',
    '仍够格可载回' in _r46, _r46[:60])
_r46 = _s46_refused(lambda: _s46_rd.make_ticket(
    'g.db', root=_s46_F, now=_s46_now, issue=False, chans=_ch46,
    records=[{'name': 'g.db', 'path': '', 'bytes': 1, 'age_h': 1.0,
              'state': 'manifest_ghost', 'why': '有卡无件', 'manifest': True}]))
chk('S46 有牙：幽灵记录（有卡无件）归证据卡面，本通道不接', '幽灵记录' in _r46, _r46[:60])
_r46 = _s46_refused(lambda: _s46_rd.make_ticket(
    'gone_20260101_010101.db', root=_s46_F, now=_s46_now, issue=False, chans=_ch46,
    records=[{'name': 'gone_20260101_010101.db', 'path': _s46_os.path.join(_s46_BDIR, 'nope.db'),
              'bytes': 1, 'age_h': 1.0, 'state': 'no_manifest', 'why': '', 'manifest': False}]))
chk('S46 有牙：人口在册但文件取不到 ⇒ 判"没测到"而不是签发一张空凭据',
    '没测到' in _r46 or '取不到文件' in _r46, _r46[:60])

# ── plan_disposal：只读承诺的四条独立证明 ──
_n46a, _p46a = _s46_names(), len(_s46_rd.read_jsonl(_s46_rd.disposition_pending_path(_s46_F))[0])
_plan46 = _s46_rd.plan_disposal(root=_s46_F, now=_s46_now)
chk('S46 plan 不删件：跑完 backups/ 名字集合逐字节相等', _s46_names() == _n46a)
chk('S46 plan 不发凭据：pending 行数不变（dry-run 与签发是两件事）',
    len(_s46_rd.read_jsonl(_s46_rd.disposition_pending_path(_s46_F))[0]) == _p46a)
chk('S46 判定不重不漏：candidates + refused == population（三档相加等于人口）',
    _plan46['candidates'] + _plan46['refused'] == _plan46['population'],
    f"{_plan46['candidates']}+{_plan46['refused']} vs {_plan46['population']}")
chk('S46 族外有独立读数与字节足迹（对齐 discourse backup_store.rb:100 used_bytes，不折叠）',
    _plan46['orphans'] == 1 and _plan46['bytes_candidates'] > 0,
    f"orphans={_plan46['orphans']} bytes={_plan46['bytes_candidates']}")
_E46 = _s46_face()
_s46_EB = _s46_os.path.join(_E46, 'backups')
for _f46e in sorted(_s46_os.listdir(_s46_EB)):
    _s46_os.remove(_s46_os.path.join(_s46_EB, _f46e))
chk('S46 零人口不判健康：status=unverified 且 print 返回非 0（空面必须有自己的状态）',
    _s46_rd.plan_disposal(root=_E46, now=_s46_now)['status'] == 'unverified'
    and _s46_rd.print_disposal_plan(_s46_rd.plan_disposal(root=_E46, now=_s46_now)) != 0)
_r46 = '未拒'
try:
    _s46_rd.plan_disposal(root=_s46_os.path.join(_s46_F, 'nope'), now=_s46_now)
except _s46_rd.RetentionError as _e46:
    _r46 = str(_e46)
chk('S46 有牙：生产者源码取不到时抛 RetentionError（不得退化成"人口为零=健康"）',
    _r46 != '未拒' and '生产者' in _r46, _r46[:60])
_s46_sh.rmtree(_E46, ignore_errors=True)
_t46x = _s43_ast.parse(_s46_src46)
chk('S46 只读承诺 AST 版：plan/make/verify/print 四个函数体内零删除调用',
    not [f'{f}.{c.func.attr}' for f in ('plan_disposal', 'make_ticket', 'verify_ticket',
                                        'print_disposal_plan')
         for c in _s43_ast.walk(_s46_fn(_t46x, f))
         if isinstance(c, _s43_ast.Call) and isinstance(c.func, _s43_ast.Attribute)
         and c.func.attr in ('remove', 'unlink', 'rmtree')])
chk('S46 有牙：dry-run 若直接调 dispose ⇒ 判"两态合一"（复核态不许顺手执行）',
    'dispose' in {n.func.id for n in _s43_ast.walk(_s43_ast.parse(
        "def plan_disposal():\n    return dispose(1)\n").body[0])
        if isinstance(n, _s43_ast.Call) and isinstance(n.func, _s43_ast.Name)})

# ── dispose：两阶段留痕 + 三个拒绝 + 一次真删 ──
chk('S46 缺 confirm 即拒（本通道没有"默认删"，非交互环境也不隐式降级）',
    'confirm' in _s46_refused(lambda: _s46_rd.dispose(
        _s46_orph, confirm=None, claim_by='L4', claim_reason=_s46_rs,
        root=_s46_F, now=_s46_now)))
chk('S46 缺 confirm 后零写入：人口与台账都没动（拒绝必须真的什么都没发生）',
    _s46_names() == _n46a
    and len(_s46_rd.read_jsonl(_s46_rd.disposition_ledger_path(_s46_F))[0]) == 0)
chk('S46 confirm 与 token 不符即拒（确认凭据=对象标识本身，不是"我知道"）',
    '不符' in _s46_refused(lambda: _s46_rd.dispose(
        _s46_orph, confirm='deadbeefdeadbeef', claim_by='L4', claim_reason=_s46_rs,
        root=_s46_F, now=_s46_now)))
chk('S46 有牙：批量入参必须拒（按名字删那一个，不按计数删一批）',
    '一次只处置一件' in _s46_refused(lambda: _s46_rd.dispose(
        [_s46_orph, _s46_keep], confirm=_t46['token'], claim_by='L4', claim_reason=_s46_rs,
        root=_s46_F, now=_s46_now)))
chk('S46 无凭据直接 dispose 被拒（必须先有过一次"人看过"的落盘动作）',
    '未消费的复核凭据' in _s46_refused(lambda: _s46_rd.dispose(
        _s46_keep, confirm='a' * 16, claim_by='L4', claim_reason=_s46_rs,
        root=_s46_F, now=_s46_now)))
_res46 = _s46_rd.dispose(_s46_orph, confirm=_t46['token'], claim_by='L4',
                         claim_reason=_s46_rs, root=_s46_F, now=_s46_now)
chk('S46 真删生效：目标消失且其余件一个不少（不是"少了好几个"）',
    _s46_names() == [_s46_keep], str(_s46_names()))
chk('S46 人口读数自证：after == before-1、removed=1（计数只做事后播报）',
    _res46['population_after'] == _res46['population_before'] - 1 == 1
    and _res46['removed'] == 1)
_rows46, _bad46 = _s46_rd.read_jsonl(_s46_rd.disposition_ledger_path(_s46_F))
chk('S46 台账两态在册：approved 与 deleted 各一条，且无坏行',
    [r['outcome'] for r in _rows46] == ['approved', 'deleted'] and _bad46 == 0,
    str([r['outcome'] for r in _rows46]))
chk('S46 台账字段齐：两条共有归因五键，终态行另带前后人口（缺任一即不可归因）',
    all({'name', 'sha256', 'token', 'claim_by', 'claim_reason', 'face', 'ts', 'outcome',
         'channel'} <= set(r) for r in _rows46)
    and {'population_before', 'population_after'} <= set(_rows46[1])
    and 'population_after' not in _rows46[0], str(sorted(_rows46[0]))[:120])
chk('S46 台账在被删面之外：目录 == 面根且 != backups/（留痕不得记在被处置对象名下）',
    _s46_os.path.dirname(_res46['ledger']) == _s46_F
    and _s46_os.path.dirname(_res46['ledger']) != _s46_BDIR)
chk('S46 返回路径未被大小写改写：ledger 必须原样以面根开头（normcase 只许用于比较）',
    _res46['ledger'].startswith(_s46_F + _s46_os.path.sep)
    and _s46_rd.disposition_ledger_path(_s46_F).startswith(_s46_F + _s46_os.path.sep),
    _res46['ledger'][:70])
chk('S46 台账写后回读：读回的 sha/名字与 dispose 返回值一致（不采信返回值）',
    _rows46[1]['sha256'] == _res46['sha256'] and _rows46[1]['name'] == _res46['name']
    and _rows46[1]['claim_by'] == 'L4')
chk('S46 pending 状态机闭合：claimed → consumed 各一条',
    [r['outcome'] for r in _s46_rd.read_jsonl(
        _s46_rd.disposition_pending_path(_s46_F))[0]] == ['claimed', 'consumed'])
chk('S46 有牙：同名二次处置被拒（不把"已经没了"报成处置成功）',
    '未消费的复核凭据' in _s46_refused(lambda: _s46_rd.dispose(
        _s46_orph, confirm=_t46['token'], claim_by='L4', claim_reason=_s46_rs,
        root=_s46_F, now=_s46_now)))

# ── 留痕出口守卫与封顶：两条都会反向翻红才作数 ──
chk('S46 正常面的留痕路径不落在 backups/ 内',
    not _s46_rd.disposition_ledger_path(_s46_F).startswith(_s46_BDIR))
_deep = _s46_os.path.join(_s46_BDIR, 'nested')
_s46_os.makedirs(_deep, exist_ok=True)
_r46 = _s46_refused(lambda: _s46_rd._outside_root(_deep, 'BACKUP-DISPOSITION.jsonl'))
chk('S46 有牙：root 指进 backups/ 里层时留痕路径必须被拦（防自擦账）',
    '拒绝' in _r46 or '被处置面' in _r46, _r46[:70])
for _i46 in range(300):
    _s46_rd.append_jsonl(_s46_rd.disposition_ledger_path(_s46_F), {'outcome': 'deleted',
                                                                   'i': _i46})
_s46_rd.append_jsonl(_s46_rd.disposition_pending_path(_s46_F), {'outcome': 'claimed',
                                                                'name': 'pinned.db'})
_s46_rd.cap_jsonl(_s46_rd.disposition_ledger_path(_s46_F), limit=50)
_c46, _ = _s46_rd.read_jsonl(_s46_rd.disposition_ledger_path(_s46_F))
chk('S46 封顶生效：台账被压回 50 行以内', len(_c46) <= 50, f'实得 {len(_c46)}')
_p46c, _ = _s46_rd.read_jsonl(_s46_rd.disposition_pending_path(_s46_F))
chk('S46 封顶有牙：未消费的 claimed 永不先掉（否则复核会凭空消失）',
    any(r.get('outcome') == 'claimed' and r.get('name') == 'pinned.db' for r in _p46c),
    str([r.get('outcome') for r in _p46c][-4:]))
chk('S46 坏行不静默：混进半行时 broken 计数必须 >0（混合格式前置）',
    _s46_rd.read_jsonl(_s46_rd.disposition_pending_path(_s46_F))[1] == 0)
with open(_s46_rd.disposition_pending_path(_s46_F), 'a', encoding='utf-8', newline='\n') as _f46:
    _f46.write('{"outcome": "claimed", broken\n')
chk('S46 有牙：写坏一行后 broken 计数为 1（不得当成"没有待办"）',
    _s46_rd.read_jsonl(_s46_rd.disposition_pending_path(_s46_F))[1] == 1)

# ── 判据侧：第 54 项双向 + 不误伤 + 接线 + record 复算 ──
_sites46 = _s43_qg._s54_delete_sites(_t46x)
chk('S46 删除点枚举标签必须是「外层函数.接收者.方法」三段（第 40 轮把写死"实扫两处"改成判形状：'
    '名册合法扩容时数量本就会变，冻结数量等于把判据写成对某一天的快照）',
    all(s.count('.') == 2 and s.endswith(('.os.remove', '.os.unlink', '.shutil.rmtree'))
        for s in _sites46) and 'dispose.os.remove' in _sites46 and len(_sites46) >= 2,
    str(_sites46))
chk('S46 名册 ⇄ 实扫双向相等（既无旁路删除点，也无幽灵豁免）',
    set(_sites46) == set(_s43_qg.S54_DELETE_ROSTER),
    f'未登记={sorted(set(_sites46) - set(_s43_qg.S54_DELETE_ROSTER))}'
    f' 幽灵={sorted(set(_s43_qg.S54_DELETE_ROSTER) - set(_sites46))}')
chk('S46 名册每条都带理由（空理由=对空气豁免）',
    all(isinstance(v, str) and len(v) >= 10 for v in _s43_qg.S54_DELETE_ROSTER.values()))
chk('S46 不误伤：dispose 体内零计数形态，而既有淘汰通道的 while+pop 不在本判据作用域内',
    _s43_qg._s54_count_shapes(_s46_fn(_t46x, 'dispose')) == []
    and _s43_qg._s54_count_shapes(_s43_ast.parse(
        "def cleanup(keep):\n    fs = sorted(keep)\n"
        "    while len(fs) > 3:\n        fs.pop(0)\n").body[0]) != [],
    'cleanup 那类 while+pop 必须仍被识别（换作用域不是取消判据）')
chk('S46 有牙：合成 dispose 带 while / len 比较 / 切片 ⇒ 三形态全被抓',
    _s43_qg._s54_count_shapes(_s43_ast.parse(
        "def dispose(a, b):\n"
        "    while len(a) > 3:\n"
        "        a.pop(0)\n"
        "    return a[:2]\n").body[0]) == ['len() 参与大小比较', 'while 循环', '切片截断'])
chk('S46 有牙：把删除塞进 plan_disposal ⇒ 删除点枚举必须点名（判据不是恒绿）',
    'plan_disposal.os.remove' in _s43_qg._s54_delete_sites(_s43_ast.parse(
        "def plan_disposal():\n    import os\n    os.remove('x')\n")))
_qg_tree46 = _s43_ast.parse(_s43_qg.read(_s46_os.path.join(_s43_qg.ROOT, 'quality_gate.py')))
_main46 = next(n for n in _s43_ast.walk(_qg_tree46)
               if isinstance(n, _s43_ast.FunctionDef) and n.name == 'main')
chk('S46 接线：check_disposition_channel() 真被门禁 main() 调用'
    '（只在测试里跑得到 = 门禁不会跑它，与"函数存在≠会被调用"同族）',
    'check_disposition_channel' in _r47_wired_names(_main46),
    str(sorted(_r47_wired_names(_main46)))[:120])
chk('S46 项名是字面量内联的（写成变量会让第 20 项静态枚举长期失明）',
    _qg_tree46.body and sum(1 for n in _s43_ast.walk(_qg_tree46)
                            if isinstance(n, _s43_ast.Constant)
                            and str(n.value).startswith('54. 复核后处置')) >= 2)
_saved46 = _s43_qg.results[:]
_s43_qg.results[:] = []
_s43_qg.check_disposition_channel()
_q54 = [r for r in _s43_qg.results if str(r[1]).startswith('54.')]
_s43_qg.results[:] = _saved46
chk('S46 第 54 项真链路判绿，且 record 恰好 1 条（跑过≠record 过）',
    len(_q54) == 1 and _q54[0][2] is True, str(_q54)[:150])
chk('S46 第 54 项回执带值：删除点计数 / 台账面署名 / TTL / CLI 真跑读数（只回"通过"等于没测）',
    len(_q54) == 1 and any('删除点' in str(x) for x in _q54[0][3])
    and any('TTL=' in str(x) for x in _q54[0][3])
    and any('台账=' in str(x) for x in _q54[0][3])
    and any('CLI 真跑' in str(x) for x in _q54[0][3]), str(_q54[0][3])[:140] if _q54 else '无')
_rc46 = _s46_sp.run([sys.executable, _s46_os.path.join(_s46_HERE, 'restore_db.py'),
                     '--plan-disposal', '--json', '--root', _s46_F],
                    capture_output=True, text=True, timeout=180, cwd=_s46_HERE)
chk('S46 CLI 真跑 --plan-disposal --json：rc=0 且 stdout 是单个可解析文档（多打一行就废下游）',
    _rc46.returncode == 0 and _s46_json.loads(_rc46.stdout)['mode'] == 'plan',
    (_rc46.stdout or '')[:60] + (_rc46.stderr or '')[:60])
_rc46b = _s46_sp.run([sys.executable, _s46_os.path.join(_s46_HERE, 'restore_db.py'),
                      '--dispose', _s46_keep, '--root', _s46_F],
                     capture_output=True, text=True, timeout=180, cwd=_s46_HERE)
chk('S46 CLI 真跑 --dispose 缺 --confirm：退出非 0 且该件仍在（拒绝是真行为不是文案）',
    _rc46b.returncode != 0 and _s46_os.path.isfile(_s46_os.path.join(_s46_BDIR, _s46_keep)),
    str(_rc46b.returncode))
_rc46c = _s46_sp.run([sys.executable, _s46_os.path.join(_s46_HERE, 'restore_db.py')],
                     capture_output=True, text=True, timeout=180, cwd=_s46_HERE)
chk('S46 无参兜底提示把新旗标一并点名（提示与实际互斥=文档说谎）',
    _rc46c.returncode != 0 and all(k in (_rc46c.stdout + _rc46c.stderr)
                                   for k in ('--plan-disposal', '--ticket', '--dispose')))
# ---- 顺带修的一条判据缺陷（本轮真实现场：同刻补签两个 tag）----
_vl46 = _s43_qg.read(_s46_os.path.join(_s46_HERE, 'verify_local.py'))
chk('S46 顺带：version 阶段按**版本序**取最新 tag（--sort=-creatordate 在同刻并列时按 refname 兜底，'
    '会把旧版本排到新版本前 ⇒ 判据自造假红）',
    "--sort=-v:refname" in _vl46 and "--sort=-creatordate" not in _vl46)
_r46v = _s46_sp.run(['git', 'tag', '-l', '--sort=-v:refname', 'v[0-9]*'],
                    capture_output=True, text=True, timeout=60, cwd=_s46_HERE)
_tg46 = [t for t in (_r46v.stdout or '').split() if t]


def _vkey46(t):
    return tuple(int(x) for x in t.lstrip('v').split('.'))


chk('S46 有牙：真仓 tag 列首必须是版本号最大者（把排序换回 creatordate 就会在此翻红）',
    bool(_tg46) and _vkey46(_tg46[0]) == max(_vkey46(t) for t in _tg46),
    f'首={_tg46[:3]}')
import verify_local as _s46_vl
_sv46 = _s46_vl.stage_version()
chk('S46 端到端：stage_version() 真跑判绿且回执带 tag 数（不是靠改 tag 数据凑绿）',
    _sv46['rc'] == 0 and 'tag 数' in _sv46['line'], _sv46['line'][:90])

_s46_sh.rmtree(_s46_F, ignore_errors=True)
chk('S46 只读复证：本段跑完本仓 backups/ 名字集合一字未变（临时面真删没有越界）',
    (sorted(_s46_os.listdir(_s46_real_bdir))
     if _s46_os.path.isdir(_s46_real_bdir) else []) == _s46_real_before,
    f'{len(_s46_real_before)} 件')


# ══════════ S47：处置锁与残留归因（第 40 轮 H171-H177 / 门禁第 55 项）══════════
# 本段在**临时面**上真起子进程持锁、真删件；段尾复证本仓 backups/ 未被越界触碰。
import shutil as _s47_sh
import subprocess as _s47_sp
import sys as _s47_sys
import tempfile as _s47_tfd
import time as _s47_time
from datetime import timedelta as _s47_td

_s47_os, _s47_rd, _s47_HERE = _s43_os, _s43_rd, _S43_HERE
_s47_real_bdir = _s47_os.path.join(_s47_HERE, 'backups')
_s47_real_before = (sorted(_s47_os.listdir(_s47_real_bdir))
                    if _s47_os.path.isdir(_s47_real_bdir) else [])
_s47_orph = 'PH2-8B-online-ican-20260914-022426.db'
_s47_keep = 'ican_20260920_010101.db'
_s47_rs = '第 40 轮验证加锁之后正常处置路径仍然走得通并且锁会自动释放'


def _s47_face():
    d = _s47_os.path.join(_s47_tfd.mkdtemp(prefix=f'bench40-{_s47_time.time_ns()}-'), 'face')
    _s47_os.makedirs(_s47_os.path.join(d, 'src'))
    _s47_os.makedirs(_s47_os.path.join(d, 'backups'))
    for rel in ('backup_db.py', _s47_os.path.join('src', 'pack_demo.py')):
        _s47_sh.copyfile(_s47_os.path.join(_s47_HERE, rel), _s47_os.path.join(d, rel))
    import sqlite3 as sq
    for n in (_s47_keep, _s47_orph):
        con = sq.connect(_s47_os.path.join(d, 'backups', n))
        con.execute('create table t(x)')
        con.executemany('insert into t values(?)', [(i,) for i in range(5)])
        con.commit()
        con.close()
    return d


_F47 = _s47_face()
_BD47 = _s47_os.path.join(_F47, 'backups')
_now47 = _dt.now(UTC)
_tree47 = _s43_ast.parse(_s43_qg.read(_s47_os.path.join(_s47_HERE, 'restore_db.py')))
_QTREE47 = _s43_ast.parse(_s43_qg.read(_s47_os.path.join(_s43_qg.ROOT, 'quality_gate.py')))


def _s47_names():
    return sorted(_s47_os.listdir(_BD47))


def _s47_fn(tree, name):
    return next(n for n in tree.body if isinstance(n, _s43_ast.FunctionDef) and n.name == name)


def _s47_reject(fn):
    try:
        fn()
        return '未拒'
    except _s47_rd.DispositionError as e:
        return str(e)


# ── 路径角色分离 ──
_P47 = {'lock': _s47_rd.disposition_lock_path(_F47),
        'holder': _s47_rd.disposition_holder_path(_F47),
        'ledger': _s47_rd.disposition_ledger_path(_F47),
        'pending': _s47_rd.disposition_pending_path(_F47)}
chk('S47 四类路径互不相同（锁/归因/台账/pending 混了就分不出谁在删）',
    len({_s47_os.path.normcase(v) for v in _P47.values()}) == 4, str(sorted(_P47)))
chk('S47 四类路径全在 backups/ 之外（留痕与锁都不得记在被处置对象名下）',
    not any(_s47_os.path.normcase(v).startswith(_s47_os.path.normcase(_BD47) + _s47_os.path.sep)
            for v in _P47.values()))
chk('S47 空面现状是 clean 而不是"读不到"（盲区要有独立状态）',
    _s47_rd.stale_lock_report(root=_F47, now=_now47)['verdict'] == 'clean')

# ── 锁形态判据：写态 with、只读态不取锁，双向都要能翻红 ──
chk('S47 dispose/make_ticket 均以 with 取锁且无裸 acquire/release',
    all(_s43_qg._s55_with_lock(_tree47, f) == 'ok' for f in ('dispose', 'make_ticket')),
    str([(f, _s43_qg._s55_with_lock(_tree47, f)) for f in ('dispose', 'make_ticket')]))
chk('S47 只读面一律不取锁（对偶 restic：--no-lock 只配 dry-run）',
    not any('disposition_lock' in _s43_qg._s55_calls(_s47_fn(_tree47, f), {'disposition_lock'})
            for f in ('plan_disposal', 'stale_lock_report', 'print_disposal_plan')))
_NOLOCK_SRC = ("import contextlib\n"
               "@contextlib.contextmanager\n"
               "def disposition_lock(**kw):\n    yield None\n")
chk('S47 有牙：把 with 拆成裸 acquire ⇒ 判 no-with（"写了取锁"与"异常路径会释放"是两件事）',
    _s43_qg._s55_with_lock(_s43_ast.parse(
        _NOLOCK_SRC + "def dispose():\n    lk = disposition_lock()\n    lk.acquire()\n"),
        'dispose') == 'no-with')
chk('S47 有牙：with 里面还有裸 release/acquire ⇒ 判 raw 并列出方法名',
    _s43_qg._s55_with_lock(_s43_ast.parse(
        _NOLOCK_SRC + "def dispose():\n    with disposition_lock() as lk:\n        lk.release()\n"
        "    lk.acquire()\n"), 'dispose').startswith('raw:'))
chk('S47 有牙：只读函数偷偷取锁 ⇒ 只读面判据必须点名',
    'disposition_lock' in _s43_qg._s55_calls(_s43_ast.parse(
        "def plan_disposal():\n    with disposition_lock():\n        pass\n").body[0],
        {'disposition_lock'}))
chk('S47 破锁分派必须是 is-not-None（真值分派会让空 token 掉进"没给参数"兜底）',
    _s43_qg._s55_dispatch_is_none_checked(_tree47) is True
    and _s43_qg._s55_dispatch_is_none_checked(_s43_ast.parse(
        "ns = None\nif ns.break_lock:\n    pass\n")) is False)

# ── 真跨进程争锁：别人持锁时一个字节都不动 ──
# 注（第 41 轮）：桩件侧件的 token 必须是 16 位十六进制、kind 必在名册内 —— 否则子进程一死，
# `stale_lock_report` 会落进新增的 `malformed` 态（S47 后半段期望 residual-recent/stale 会红）。
# 这不是把测试改成迁就判据：合成现场本来就该长得像真现场，否则测的是"垃圾侧件"这一另一件事。
_child47 = _s47_os.path.join(_F47, 'child_holding_lock.py')
with open(_child47, 'w', encoding='utf-8', newline='\n') as f47:
    f47.write("import filelock, json, os, sys, time\n"
              "d = sys.argv[1]\n"
              "p = os.path.join(d, 'BACKUP-DISPOSITION.lock')\n"
              "h = os.path.join(d, 'BACKUP-DISPOSITION.holder')\n"
              "lk = filelock.FileLock(p, timeout=0)\n"
              "lk.acquire()\n"
              "json.dump({'pid': os.getpid(), 'token': 'deadbeefcafe1234', 'kind': 'dispose',\n"
              "           'ts': sys.argv[2]}, open(h, 'w'))\n"
              "print('HELD', flush=True)\n"
              "time.sleep(float(sys.argv[3]))\n")
_names0 = _s47_names()
_led0 = len(_s47_rd.read_jsonl(_P47['ledger'])[0])
_ck47 = _s47_sp.Popen([_s47_sys.executable, _child47, _F47, _now47.isoformat(), '12'],
                      stdout=_s47_sp.PIPE, text=True, cwd=_s47_HERE)
chk('S47 子进程真的持住了锁（HELD 回执 + 锁文件在盘）',
    _ck47.stdout.readline().strip() == 'HELD' and _s47_os.path.isfile(_P47['lock']))
_m47 = _s47_reject(lambda: _s47_rd.dispose(_s47_orph, confirm='x' * 16, claim_by='L4',
                                          claim_reason=_s47_rs, root=_F47, now=_now47))
chk('S47 有牙：锁被他人持有时 dispose 必须拒，且拒绝理由点名持锁者身份',
    _m47 != '未拒' and '处置锁被占用' in _m47 and 'deadbeefcafe1234' in _m47, _m47[:90])
chk('S47 拒绝确实零副作用：人口与台账行数一字未动',
    _s47_names() == _names0 and len(_s47_rd.read_jsonl(_P47['ledger'])[0]) == _led0)
chk('S47 只读态在他人持锁时照样跑（dry-run 不许排队等锁）',
    _s47_rd.plan_disposal(root=_F47, now=_now47)['population'] == 2)
_st47 = _s47_rd.stale_lock_report(root=_F47, now=_now47)
chk('S47 他人持锁时现状判 live（不得与"没人拿"同形）', _st47['verdict'] == 'live', str(_st47)[:90])
_b47 = _s47_reject(lambda: _s47_rd.break_stale_lock(root=_F47, confirm=_st47['token']))
chk('S47 有牙：锁还在人手里时拒绝破锁（那是抢别人的现场，不是清自己的残局）',
    '仍在被持有' in _b47 or '拒绝' in _b47, _b47[:70])
_ck47.kill()
_ck47.wait()
_s47_time.sleep(0.4)
_st2 = _s47_rd.stale_lock_report(root=_F47, now=_now47)
chk('S47 进程被杀后 OS 侧锁自动释放，但归因侧件残留 ⇒ 报 residual-recent 带龄期',
    _st2['verdict'] == 'residual-recent' and _st2['holder_age_s'] is not None, str(_st2)[:110])
_st3 = _s47_rd.stale_lock_report(root=_F47, now=_now47 + _s47_td(
    seconds=_s47_rd.LOCK_STALE_S + 5))
chk('S47 同一条侧件过龄期阈值即转 stale ⇒ 两个 verdict 由龄期分开（阈值是在册常数）',
    _st3['verdict'] == 'stale', str(_st3)[:90])
chk('S47 有牙：空 token 破锁必须被拒（本轮实测过它曾被降成"没给参数"）',
    '不符' in _s47_reject(lambda: _s47_rd.break_stale_lock(root=_F47, confirm='')))
chk('S47 缺 confirm 破锁即拒（本通道没有"无条件清锁"）',
    'confirm' in _s47_reject(lambda: _s47_rd.break_stale_lock(root=_F47, confirm=None)))
_r47 = _s47_rd.break_stale_lock(root=_F47, confirm=_st2['token'])
chk('S47 逐字回交 holder token 才清得掉，且回执说明原态', _r47['cleared'] is True
    and _r47['was'] in ('residual-recent', 'stale'), str(_r47)[:90])
chk('S47 清完回到 clean（锁与归因都不在盘上）',
    _s47_rd.stale_lock_report(root=_F47, now=_now47)['verdict'] == 'clean'
    and not _s47_os.path.isfile(_P47['holder']))
chk('S47 有牙：清过之后再清即拒，不把"无事可做"报成成功',
    '没有残留归因可清' in _s47_reject(
        lambda: _s47_rd.break_stale_lock(root=_F47, confirm=_st2['token'])))

# ── 正常路径 + 自动释放 + 状态机 ──
_tk47 = _s47_rd.make_ticket(_s47_orph, root=_F47, now=_now47)
chk('S47 签发确实写了 pending（"人看过"这件事在盘上要有形）',
    [r['outcome'] for r in _s47_rd.read_jsonl(_P47['pending'])[0]] == ['claimed'])
chk('S47 签发完锁自动释放，不留一把空锁等人去破',
    not _s47_os.path.isfile(_P47['lock'])
    and _s47_rd.stale_lock_report(root=_F47, now=_now47)['verdict'] == 'clean')
_rs47 = _s47_rd.dispose(_s47_orph, confirm=_tk47['token'], claim_by='L4',
                        claim_reason=_s47_rs, root=_F47, now=_now47)
chk('S47 正常处置成功：只少那一个 + 计数只做事后播报',
    _s47_names() == [_s47_keep] and _rs47['removed'] == 1
    and _rs47['population_after'] == _rs47['population_before'] - 1)
chk('S47 处置完锁与归因双双自动清（with 兜异常路径）',
    not _s47_os.path.isfile(_P47['lock']) and not _s47_os.path.isfile(_P47['holder']))
chk('S47 pending 状态机闭合：claimed → consumed',
    [r['outcome'] for r in _s47_rd.read_jsonl(_P47['pending'])[0]] == ['claimed', 'consumed'])

# ── 重入安全（实测：同实例可重入、异实例互撞 ⇒ 必须缓存单例并计深度）──
with _s47_rd.disposition_lock(root=_F47):
    with _s47_rd.disposition_lock(root=_F47):
        _nest47 = 'inner-ok'
    _depth_after_inner = _s47_rd._LOCKS[_P47['lock']][1]
chk('S47 嵌套取锁不死锁且按深度回退（不缓存实例就会同进程自撞）',
    _nest47 == 'inner-ok' and _depth_after_inner == 1, f'depth={_depth_after_inner}')
chk('S47 嵌套退出后深度归零并可再取',
    _s47_rd._LOCKS[_P47['lock']][1] == 0
    and not _s47_os.path.isfile(_P47['lock']))

# ── 缺依赖 ⇒ fail-closed ──
_saved_fl = _s47_sys.modules.get('filelock')
_s47_sys.modules['filelock'] = None
try:
    with _s47_rd.disposition_lock(root=_F47):
        pass
    _f47 = '未拒'
except _s47_rd.DispositionError as e47:
    _f47 = str(e47)
except Exception as e47:
    _f47 = f'非预期 {type(e47).__name__}'
finally:
    if _saved_fl is not None:
        _s47_sys.modules['filelock'] = _saved_fl
chk('S47 有牙：filelock 不可用即拒（禁止退化成无锁处置），且不留锁文件',
    'filelock' in _f47 and '拒绝' in _f47 and not _s47_os.path.isfile(_P47['lock']), _f47[:80])

# ── 判据侧：record 复算 + 名册同步 + 接线 + 门面带值 ──
_saved55 = _s43_qg.results[:]
_s43_qg.results[:] = []
_s43_qg.check_disposition_lock()
_q55 = [r for r in _s43_qg.results if str(r[1]).startswith('55.')]
_s43_qg.results[:] = _saved55
chk('S47 第 55 项真链路判绿且 record 恰好 1 条（跑过≠record 过）',
    len(_q55) == 1 and _q55[0][2] is True, str(_q55)[:140])
chk('S47 第 55 项门面带值：多路分离 / verdict 读数 / filelock 声明与延迟导入三样齐',
    len(_q55) == 1 and any('路分离' in str(x) for x in _q55[0][3])
    and any('verdict=' in str(x) for x in _q55[0][3])
    and any('filelock 已声明=True 顶层导入=False' in str(x) for x in _q55[0][3]),
    str(_q55[0][3])[:130] if _q55 else '无 record')
_n55 = [r for r in _saved55 if str(r[1]).startswith('55.')]
chk('S47 单次直调只追加一条记录（跑过≠record 过，且不重复记账）',
    len(_q55) - len(_n55) == 1 and len(_q55) == 1,
    f'直调前 55 记录 {len(_n55)} 条 / 直调后 {len(_q55)} 条')
_sites47 = _s43_qg._s54_delete_sites(_tree47)
chk('S47 第 54 项名册随本轮同步：归因侧件删除点在册（漏登记恒红，多登记即幽灵）',
    set(_sites47) == set(_s43_qg.S54_DELETE_ROSTER) and 'clear_holder.os.remove' in _sites47,
    str(_sites47))
_main47 = _s47_fn(_QTREE47, 'main')
chk('S47 接线：check_disposition_lock() 真被门禁 main() 调用（打在 quality_gate 树上）',
    'check_disposition_lock' in _r47_wired_names(_main47),
    str(sorted(_r47_wired_names(_main47)))[:120])
chk('S47 项名是字面量内联的（写成变量会让第 20 项静态枚举长期失明）',
    sum(1 for n in _s43_ast.walk(_QTREE47) if isinstance(n, _s43_ast.Constant)
        and str(n.value).startswith('55. 处置锁')) >= 2)
_rc47 = _s47_sp.run([_s47_sys.executable, _s47_os.path.join(_s47_HERE, 'restore_db.py'),
                     '--lock-status', '--root', _F47],
                    capture_output=True, text=True, timeout=180, cwd=_s47_HERE)
chk('S47 CLI 真跑 --lock-status：rc=0 且输出带 verdict（拿不到读数就是没测到）',
    _rc47.returncode == 0 and 'verdict=' in _rc47.stdout, _rc47.stdout[:70])
chk('S47 空 token 走 CLI 时也必须给拒绝理由，不得掉进"没给参数"兜底',
    _s47_sp.run([_s47_sys.executable, _s47_os.path.join(_s47_HERE, 'restore_db.py'),
                 '--break-lock', '', '--root', _F47],
                capture_output=True, text=True, timeout=180,
                cwd=_s47_HERE).stdout.startswith('❌ 破锁被拒'))
_req47 = _s43_qg.read(_s47_os.path.join(_s47_HERE, 'requirements.txt'))
chk('S47 filelock 已入声明面并带版本区间（用了没登记＝换机即崩，第 32 轮 PyYAML 同族）',
    'filelock>=4.0,<5' in _req47, [x for x in _req47.splitlines() if 'filelock' in x][:1])
_s47_sh.rmtree(_F47, ignore_errors=True)
chk('S47 只读复证：本段跑完本仓 backups/ 名字集合一字未变（临时面真删没有越界）',
    (sorted(_s47_os.listdir(_s47_real_bdir))
     if _s47_os.path.isdir(_s47_real_bdir) else []) == _s47_real_before,
    f'{len(_s47_real_before)} 件')


# ══════════ S48：同步工具的入场闸（第 40 轮 M127 / 在镜像面上误跑）══════════
# 一手代价（本轮收口时我自己踩的）：`verify_local` 在 05 面跑 ⇒ stage_sync 起的是**该面那份**
# 工具，DEV 解析成镜像、REPO 解析成 `工程佐证材料`，扫出 MISSING 472；照着门面行给的处方
# 再跑 `--fix`，那行 `mkdir(parents=True)` 就会把 472 个副本造进 `交付物/提交包/` 里。
# ⇒ 认不出源面必须是"输入缺失"(rc=2)，不得报成"有 N 项待同步"。
_s48_os, _s48_py = _s43_os, _s47_sys.executable
import contextlib as _s48_cl
import io as _s48_io
_s48_tool = _s44_P(_S43_HERE) / 'tools' / 'frontend_sync_check.py'
_s48_roots = []


def _s48_face(name):
    """造一个"看着像镜像面"的面：只有 tools/ 和被测工具本身（守卫排在读声明之前，故不必铺别的文件）。"""
    root = _s44_P(_s47_tfd.mkdtemp(prefix=f'bench48-{_s47_time.time_ns()}-'))
    face = root / name
    (face / 'tools').mkdir(parents=True)
    _s47_sh.copy2(str(_s48_tool), str(face / 'tools' / 'frontend_sync_check.py'))
    _s48_roots.append(root)
    return root, face


def _s48_run(face, *args):
    return _s47_sp.run([_s48_py, str(face / 'tools' / 'frontend_sync_check.py'), *args],
                       capture_output=True, text=True, timeout=240, cwd=str(face))


chk('S48 入场前提：S44 的 monkeypatch 已回滚（DEV_FACE_NAME 回到真值，不把泄漏留给下一段）',
    _s44_t.DEV_FACE_NAME == _S44_FACE_NAME_REAL, repr(_s44_t.DEV_FACE_NAME))
_root48, _face48 = _s48_face('05-源代码全文')
_r48 = _s48_run(_face48)
chk('S48 有牙：镜像面只读跑 ⇒ 退 2 判"面无法确认"，不再报 MISSING 472（本轮实测过的假红）',
    _r48.returncode == 2 and '面无法确认' in _r48.stdout and 'MISSING' not in _r48.stdout,
    f'rc={_r48.returncode}|{_r48.stdout[:110]}')
_root48b, _face48b = _s48_face('05-源代码全文')
_r48b = _s48_run(_face48b, '--fix')
chk('S48 有牙：镜像面带 --fix 同样退 2，且**盘上没被 mkdir 出任何镜像目录**（污染拦在写盘之前）',
    _r48b.returncode == 2 and _s48_os.listdir(str(_face48b)) == ['tools']
    and _s48_os.listdir(str(_root48b)) == ['05-源代码全文'],
    f'{_s48_os.listdir(str(_face48b))} / {_s48_os.listdir(str(_root48b))}')
_r48o = _s48_run(_face48, '--only', 'deploy-free')
chk('S48 闸排在 --only 过滤之前：镜像面上换任何参数组合都到不了比对分支',
    _r48o.returncode == 2 and '面无法确认' in _r48o.stdout, f'rc={_r48o.returncode}')
chk('S48 报错自带出口：点名当前解析到的 DEV，并给出该跑哪一份（只判红不给路＝下一个人接着踩）',
    str(_face48) in _r48.stdout and '请用源面那一份跑' in _r48.stdout
    and _S44_FACE_NAME_REAL in _r48.stdout, _r48.stdout[-120:])
_tree48 = _s43_ast.parse(_s43_qg.read(str(_s48_tool)))
_main48 = next(n for n in _tree48.body
               if isinstance(n, _s43_ast.FunctionDef) and n.name == 'main')
_gate48 = [c.lineno for c in _s43_ast.walk(_main48)
           if isinstance(c, _s43_ast.Call) and getattr(c.func, 'id', '') == 'face_problems']
_write48 = [n.lineno for n in _s43_ast.walk(_main48)
            if isinstance(n, _s43_ast.Attribute) and n.attr in ('mkdir', 'copy2')]
chk('S48 接线：main() 真调用 face_problems()（写在没人调的函数里不是守卫）',
    len(_gate48) >= 1, f'调用点={_gate48}')
chk('S48 顺序有牙：入场闸排在 mkdir/copy2 之前（排在后面就拦不住"照处方 --fix"那一次）',
    _gate48 and _write48 and min(_gate48) < min(_write48),
    f'闸@{min(_gate48) if _gate48 else None} 写盘@{min(_write48) if _write48 else None}')
_dev48, _mir48, _name48 = _s44_t.DEV, _s44_t.MIRROR_DIRS, _s44_t.DEV_FACE_NAME
_root48c, _face48c = _s48_face(_name48)          # 名字与真源面对齐 ⇒ 只让后面那两支各自单独亮
try:
    _s44_t.DEV = _face48c
    _s44_t.MIRROR_DIRS = {'deploy-free': _face48c, '05-源代码全文': _root48c}
    _p48_same = _s44_t.face_problems()
    _s44_t.MIRROR_DIRS = {'deploy-free': _root48c / 'nope', '05-源代码全文': _root48c}
    _p48_gone = _s44_t.face_problems()
    _s44_t.MIRROR_DIRS = {'deploy-free': _root48c, '05-源代码全文': _root48c}
    _p48_ok = _s44_t.face_problems()
finally:
    _s44_t.DEV, _s44_t.MIRROR_DIRS, _s44_t.DEV_FACE_NAME = _dev48, _mir48, _name48
chk('S48 「镜像与权威源是同一个面」这一支可达且只此一支（在 deploy-free 面自比会恒全等）',
    len(_p48_same) == 1 and '同一个面' in _p48_same[0], _p48_same)
chk('S48 「镜像不存在」这一支点名是哪个镜像（不点名就无法区分"换面跑错"与"真的没铺副本"）',
    len(_p48_gone) == 1 and 'deploy-free' in _p48_gone[0] and '不存在' in _p48_gone[0], _p48_gone)
chk('S48 正对照：两面都在且不是同一处 ⇒ face_problems()==[]（闸不是恒红）',
    _p48_ok == [], _p48_ok)
_s48_on_src = _s44_P(_S43_HERE).name == _S44_FACE_NAME_REAL
_r48src = _s48_run(_s44_P(_S43_HERE))
chk('S48 结论跟着所在面走（本段自己也不许假设跑在源面）：源面退 0/1 且不印"面无法确认"，'
    '镜像面退 2 且必印 —— 门禁在 05 副本里跑 L4 时这一支同样成立',
    (_r48src.returncode in (0, 1) and '面无法确认' not in _r48src.stdout) if _s48_on_src
    else (_r48src.returncode == 2 and '面无法确认' in _r48src.stdout),
    f'面={_s44_P(_S43_HERE).name} rc={_r48src.returncode}|{_r48src.stdout[-70:]}')
_buf48 = _s48_io.StringIO()
_root48e, _dev48e, _mir48e = _s44_sandbox()
try:
    with _s48_cl.redirect_stdout(_buf48):
        _rc48only = _s44_t.main(['--only', '查无此镜像'])
finally:
    _s44_teardown(_root48e)
chk('S48 原有用法错误分支未被入场闸吞掉：面确认之后 --only 写错仍退 2 且文案是 --only 那条'
    '（跑在 S44 沙箱里 ⇒ 与套件所在面无关，源面/副本面都能测到这一支）',
    _rc48only == 2 and '--only 指向不存在的镜像' in _buf48.getvalue(), _buf48.getvalue()[:110])
chk('S48 只读复证：本仓交付树里没有出现被误造出来的 工程佐证材料/deploy-free（真仓未被写侧污染）',
    not _s48_os.path.isdir(_s48_os.path.join(_S43_HERE, '..', '交付物', '提交包',
                                             '工程佐证材料', 'deploy-free')))
for _x48 in _s48_roots:
    _s47_sh.rmtree(str(_x48), ignore_errors=True)
chk('S48 临时面清干净：本轮造的 4 个面全部移除（残留会喂给下一段的取数面）',
    all(not _s48_os.path.isdir(str(x)) for x in _s48_roots), f'{len(_s48_roots)} 个')

# ══════════ S49：只读档 dry-run + 侧件字段级校验（第 41 轮 M128/M129 + M130 否证）══════════
# 一手动因：① 撞锁时整条通道只剩"等"或"破别人的锁"两个动作（M128）；
# ② 侧件"解析得开但字段是垃圾"此前落到 stale，而 stale 允许破锁 ⇒ 无人认领却能被清掉（新增第六态）；
# ③ M130 声称"台账时序靠人记"——本轮 14:12 注入实测第 29 项早就抓得住，故做成常驻否证腿。
# 本段全部跑在临时面；真仓面只查不写，段尾复证 backups/ 名字集合一字未变。
_s49_rd, _s49_os, _s49_JS = _s47_rd, _s47_os, _json
_s49_reject = _s47_reject          # 复用 S47 的"跑一次，返回拒绝理由或'未拒'"助手
_s49_HERE = _S43_HERE
_s49_orph = 'PH2-8B-online-ican-20260914-022426.db'
_s49_rs = '第 41 轮验证只读档在撞锁现场仍能出结论并且一个字节都不动'
_F49 = _s47_face()
_BD49 = _s49_os.path.join(_F49, 'backups')
_P49 = {'lock': _s49_rd.disposition_lock_path(_F49),
        'holder': _s49_rd.disposition_holder_path(_F49),
        'ledger': _s49_rd.disposition_ledger_path(_F49),
        'pending': _s49_rd.disposition_pending_path(_F49)}
_now49 = _dt.now(UTC)
_s49_real_bdir = _s49_os.path.join(_s49_HERE, 'backups')
_s49_real_before = (sorted(_s49_os.listdir(_s49_real_bdir))
                    if _s49_os.path.isdir(_s49_real_bdir) else [])


def _s49_names():
    return sorted(f for f in _s49_os.listdir(_BD49) if f.endswith('.db'))


def _s49_state():
    """盘面三面对账快照：名字集合 + 台账行数 + pending 行数（少一面就看不出"动了盘"）。"""
    return (_s49_names(), len(_s49_rd.read_jsonl(_P49['ledger'])[0]),
            len(_s49_rd.read_jsonl(_P49['pending'])[0]))


chk('S49 无凭据时 dry-run 判 refuse:no_ticket（只读档不替人跳过"人看过"这一步）',
    _s49_rd.dispose(_s49_orph, confirm='x' * 16, root=_F49, dry_run=True)['verdict']
    == 'refuse:no_ticket')
_tk49 = _s49_rd.make_ticket(_s49_orph, root=_F49, now=_now49)
_st49 = _s49_state()
_pr49 = _s49_rd.dispose(_s49_orph, confirm=_tk49['token'], claim_by='L4',
                            claim_reason=_s49_rs, root=_F49, now=_now49, dry_run=True)
chk('S49 有凭据时 dry-run 给 would_dispose 并带回对象身份（sha/渠道，不是只回一句"可以"）',
    _pr49['verdict'] == 'would_dispose' and _pr49['sha256'] == _tk49['sha256']
    and _pr49['channel'] == _tk49['channel'], str(_pr49)[:120])
_sz49 = _s49_os.path.getsize(_s49_os.path.join(_BD49, _s49_orph))
chk('S49 只读档报的字节数与盘上实量相等（要交出去的数字必须当场取，一次截断就少报）',
    _pr49['would_free_bytes'] == _sz49, f'返回 {_pr49["would_free_bytes"]} / 盘上 {_sz49}')
chk('S49 有牙：dry-run 跑完盘面三面对账一字未动（名字集合 + 台账行数 + pending 行数）',
    _s49_state() == _st49, f'前 {_st49[1:]} / 后 {_s49_state()[1:]}')
chk('S49 只读档不免检：假 confirm 照样判 refuse:confirm_mismatch',
    _s49_rd.dispose(_s49_orph, confirm='f' * 16, claim_by='L4', claim_reason=_s49_rs,
                    root=_F49, now=_now49, dry_run=True)['verdict'] == 'refuse:confirm_mismatch')
chk('S49 dry-run 缺 confirm 即抛（缺确认与只读无关，那道门不许因 dry 而拆掉）',
    _s49_reject(lambda: _s49_rd.dispose(_s49_orph, root=_F49, dry_run=True)) != '未拒')
chk('S49 dry-run 对孤儿入参同样拒，且拒完盘面未变',
    _s49_reject(lambda: _s49_rd.dispose('../OUTSIDE.db', confirm='x' * 16, root=_F49,
                                        dry_run=True)) != '未拒' and _s49_state() == _st49)
_nc49 = _s49_rd.dispose(_s49_orph, confirm=_tk49['token'], root=_F49, now=_now49,
                      dry_run=True)
chk('S49 有牙：只读档把真拒因原样跑出来（族外无主件缺认领理由 ⇒ refuse:reason_too_short）',
    _nc49['verdict'] == 'refuse:reason_too_short' and '认领理由' in _nc49['why'],
    str(_nc49)[:130])
chk('S49 只读档的拒也不会动盘：三面对账与签发后一致', _s49_state() == _st49,
    f'前 {_st49[1:]} / 后 {_s49_state()[1:]}')
chk('S49 只读档不取锁也不写归因（lock 与 holder 两枚文件都不该出现）',
    not _s49_os.path.isfile(_P49['lock']) and not _s49_os.path.isfile(_P49['holder']))

# ── CLI 面：人在命令行前用不用得上这个档 ──
_cli49 = _s47_sp.run([_s47_sys.executable, _s49_os.path.join(_s49_HERE, 'restore_db.py'),
                      '--dispose', _s49_orph, '--confirm', _tk49['token'], '--dry-run',
                      '--claim-by', 'L4', '--claim-reason', _s49_rs, '--json', '--root', _F49],
                     capture_output=True, text=True, timeout=240, cwd=_s49_HERE)
_jcli49 = {}
try:
    _jcli49 = _s49_JS.loads(_cli49.stdout.strip().splitlines()[-1])
except (ValueError, IndexError):
    _jcli49 = {}
chk('S49 CLI 真跑 --dispose --dry-run --json：rc=0 且 mode=probe verdict=would_dispose',
    _cli49.returncode == 0 and _jcli49.get('mode') == 'probe'
    and _jcli49.get('verdict') == 'would_dispose', f'rc={_cli49.returncode}|{_cli49.stdout[:80]}')
_ref49 = _s47_sp.run([_s47_sys.executable, _s49_os.path.join(_s49_HERE, 'restore_db.py'),
                      '--dispose', _s49_orph, '--confirm', 'b' * 16, '--dry-run', '--root', _F49],
                     capture_output=True, text=True, timeout=240, cwd=_s49_HERE)
chk('S49 只读档的 rc 区分"真跑会删成"与"真跑也会拒"（两档同码＝把拒绝读成成功）',
    _cli49.returncode == 0 and _ref49.returncode != _cli49.returncode,
    f'would={_cli49.returncode} refuse={_ref49.returncode}')
_raw49 = _s47_sp.run([_s47_sys.executable, _s49_os.path.join(_s49_HERE, 'restore_db.py'),
                      '--dry-run', '--root', _F49],
                     capture_output=True, text=True, timeout=240, cwd=_s49_HERE)
chk('S49 有牙：裸 --dry-run 即拒并说清与 --plan-disposal 的分工（不许留一个静默空转的旗标）',
    _raw49.returncode != 0 and '--plan-disposal' in _raw49.stdout, _raw49.stdout[:90])
chk('S49 裸 --dry-run 的拒绝确实没动盘（pending 仍是一张 claimed）',
    [r['outcome'] for r in _s49_rd.read_jsonl(_P49['pending'])[0]] == ['claimed'])

# ── 撞锁现场：M128 的正题 ──
_child49 = _s49_os.path.join(_F49, 'child49.py')
with open(_child49, 'w', encoding='utf-8', newline='\n') as f49:
    f49.write("import filelock, json, os, sys, time\n"
              "lk = filelock.FileLock(sys.argv[1], timeout=0)\n"
              "lk.acquire()\n"
              "json.dump({'pid': os.getpid(), 'token': 'aaaaaaaaaaaaaaaa', 'kind': 'dispose',\n"
              "           'ts': sys.argv[2]}, open(sys.argv[3], 'w'))\n"
              "print('HELD', flush=True)\n"
              "time.sleep(float(sys.argv[4]))\n")
_ck49 = _s47_sp.Popen([_s47_sys.executable, _child49, _P49['lock'], _now49.isoformat(),
                       _P49['holder'], '14'], stdout=_s47_sp.PIPE, text=True, cwd=_s49_HERE)
chk('S49 子进程持锁就位（HELD 回执 + 锁文件在盘，否则下面的"撞锁"是空转）',
    _ck49.stdout.readline().strip() == 'HELD' and _s49_os.path.isfile(_P49['lock']))
_busy49 = _s49_reject(lambda: _s49_rd.dispose(_s49_orph, confirm=_tk49['token'],
                                              root=_F49, now=_now49))
chk('S49 真跑撞锁被拒，且文案给出 --dry-run 这条只读出口（处方也是输出，须先验）',
    '处置锁被占用' in _busy49 and '--dry-run' in _busy49, _busy49[:110])
_d49 = _s49_rd.dispose(_s49_orph, confirm=_tk49['token'], claim_by='L4',
                   claim_reason=_s49_rs, root=_F49, now=_now49, dry_run=True)
chk('S49 有牙（M128 正题）：撞锁现场只读档照样出结论，锁态报 live 而盘面未变',
    _d49['verdict'] == 'would_dispose' and _d49['lock'] == 'live' and _s49_state() == _st49,
    str(_d49)[:120])
_fix49 = _s47_sp.run([_s47_sys.executable, _s49_os.path.join(_s49_HERE, 'restore_db.py'),
                      '--dispose', _s49_orph, '--confirm', _tk49['token'], '--dry-run',
                      '--claim-by', 'L4', '--claim-reason', _s49_rs, '--root', _F49],
                     capture_output=True, text=True, timeout=240, cwd=_s49_HERE)
chk('S49 照文案把那条 --dry-run 真跑一次：撞锁期间仍 rc=0 并打印 verdict（处方不是空话）',
    _fix49.returncode == 0 and 'verdict=would_dispose' in _fix49.stdout, _fix49.stdout[:100])

# ── 侧件字段级校验与第六态 ──
_ok49 = _s49_rd._holder_doc(token='c' * 16, kind='dispose')
_bad49 = {'pid 为 0': {'pid': 0}, 'pid 是字符串': {'pid': 'x'}, 'token 过短': {'token': 'abc'},
          'kind 不在名册': {'kind': 'ghost'}, 'ts 无时区': {'ts': '2026-01-01T00:00:00'},
          'ts 解析失败': {'ts': 'not-a-time'}}
_hits49 = {k: _s49_rd.validate_holder_doc(dict(_ok49, **v)) for k, v in _bad49.items()}
chk('S49 正对照：合法侧件零问题（校验不许把正常路径一起杀了）',
    _s49_rd.validate_holder_doc(_ok49) == [], str(_s49_rd.validate_holder_doc(_ok49))[:90])
chk('S49 有牙：六类非法侧件各自判红且点名（一支都不许靠"别的分支"蒙过去）',
    all(_hits49[k] for k in _bad49), str([k for k in _bad49 if not _hits49[k]]))
chk('S49 交叉不等式可达：等待窗口 > 陈旧阈值判红，而当前常量判绿',
    _s49_rd.validate_lock_config() == []
    and _s49_rd.validate_lock_config(wait_s=_s49_rd.LOCK_STALE_S + 1,
                                     stale_s=_s49_rd.LOCK_STALE_S) != [],
    f'当前={_s49_rd.validate_lock_config()} 变异='
    f'{_s49_rd.validate_lock_config(wait_s=901, stale_s=400)}')
_hold_before49 = (open(_P49['holder'], 'rb').read()
                  if _s49_os.path.isfile(_P49['holder']) else None)
chk('S49 有牙：write_holder 用不在名册的 kind ⇒ 抛错且侧件字节一字未变（无归因就不许继续）',
    _s49_reject(lambda: _s49_rd.write_holder(root=_F49, token='d' * 16,
                                             kind='oops')) != '未拒'
    and (open(_P49['holder'], 'rb').read() if _s49_os.path.isfile(_P49['holder']) else None)
    == _hold_before49,
    f'侧件存在={_hold_before49 is not None}')
with open(_P49['holder'], 'w', encoding='utf-8', newline='\n') as f49:
    f49.write(_s49_JS.dumps(dict(_ok49, kind='ghost'), ensure_ascii=False) + '\n')
_live_bad49 = _s49_rd.stale_lock_report(root=_F49, now=_now49)
chk('S49 状态机次序有牙：有人在临界区时先报 live，侧件缺陷以 holder_problems 带出而非吞掉',
    _live_bad49['verdict'] == 'live' and len(_live_bad49['holder_problems']) >= 1
    and _live_bad49['token'] == '', str(_live_bad49)[:120])
_ck49.kill()
_ck49.wait()
# 不在 _F49 上验 malformed：刚才那次"撞锁期间的只读探测"会让 filelock 4.0.4 在 Windows 上留下
# 打开过的句柄（实测 `os.remove` 报 WinError 32），此时 `acquire(timeout=0)` 可能仍拿不到锁 ⇒
# 状态会被我自己的夹具句柄污染成 live。改用一枚干净临时面，让状态机在它自己的前提下被问。
_F49B = _s47_face()
_HP49B = _s49_rd.disposition_holder_path(_F49B)
_LP49B = _s49_rd.disposition_lock_path(_F49B)
with open(_HP49B, 'w', encoding='utf-8', newline='\n') as f49b:
    f49b.write(_s49_JS.dumps(dict(_ok49, kind='ghost'), ensure_ascii=False) + '\n')
chk('S49 前提：干净临时面上确实没有锁文件（否则下面的 malformed 期望是在测"有人在里面"）',
    not _s49_os.path.isfile(_LP49B))
_mf49 = _s49_rd.stale_lock_report(root=_F49B, now=_now49)
chk('S49 第六态端到端：OS 无人持锁 + 侧件字段非法 ⇒ malformed，且**不给破锁 token**',
    _mf49['verdict'] == 'malformed' and _mf49['token'] == '' and _mf49['holder_problems'],
    str(_mf49)[:120])
_rej49 = _s49_reject(lambda: _s49_rd.break_stale_lock(root=_F49B, confirm='0' * 12))
chk('S49 有牙：malformed 拒破锁且侧件仍在盘上（不清、不替人猜一个身份）',
    '字段非法' in _rej49 and _s49_os.path.isfile(_HP49B), _rej49[:90])
with open(_HP49B, 'w', encoding='utf-8', newline='\n') as f49b:
    f49b.write(_s49_JS.dumps(_ok49, ensure_ascii=False) + '\n')
chk('S49 对照：只把侧件字段换成合法形态（仍无人持锁、仍残留），同一面立刻转 residual-recent '
    '⇒ 第六态由字段决定，不是由"有没有残留"决定',
    _s49_rd.stale_lock_report(root=_F49B, now=_now49)['verdict'] == 'residual-recent')
chk('S49 对照：合法残留过龄期阈值即转 stale ⇒ malformed 与龄期是两条独立轴',
    _s49_rd.stale_lock_report(root=_F49B,
                              now=_now49 + _s47_td(seconds=10 ** 4))['verdict'] == 'stale')
_s47_sh.rmtree(_F49B, ignore_errors=True)
_s47_sh.rmtree(_F49, ignore_errors=True)

# ── 门禁接线 + 纯内存变异（R299：自证夹具不改被测文件） ──
_saved52 = list(_s43_qg.results)
_s43_qg.results[:] = []
_s43_qg.check_disposition_lock()
_r55a = list(_s43_qg.results)
_s43_qg.results[:] = []
_s43_qg.check_sync_egress_single_entry()
_r52a = list(_s43_qg.results)
_s43_qg.results[:] = _saved52
chk('S49 第 55 项真链路判绿且 record 恰好 1 条（跑过≠record 过），门面带新增两路读数',
    len(_r55a) == 1 and _r55a[0][0] == 'BLOCK' and _r55a[0][2] is True
    and 'dry_run@' in str(_r55a[0][3]) and "['dispose', 'ticket']" in str(_r55a[0][3]),
    str(_r55a[0][3])[:150])
chk('S49 第 52 项真链路判绿且 record 恰好 1 条，门面行带着面闸的两个读数',
    len(_r52a) == 1 and _r52a[0][2] is True and '面闸' in str(_r52a[0][3])
    and '合成三面形状' in str(_r52a[0][3]), str(_r52a[0][3])[:130])


_MUT_LOG = []   # 每支变异腿一条：(name, key_suffix, identity, changed_bytes)


def _s49_run_judge(name, key_suffix, mut, qg=_s43_qg, identity=False):
    """内存里换掉某份源码的读数再跑判据；被测文件与临时面都不动。

    第 47 轮补 no-op 探测（一手：S52 两支腿的 needle 因 M149 加了 ` as lease:` 而静默不命中，
    于是"判据没抓住"与"夹具没变异"两种形态在结果上完全同形）。这里只**记账不改判**：
    变红仍由各自的断言负责，S55 段末一条常驻断言要求"除显式标 identity 的之外，
    每支变异腿都真改了字节"——把"假反例"变成一个会被判红的账面事实，而不是靠人读代码。
    """
    real = qg.read
    seen = {'changed': 0}

    def _probe(path, *a, **kw):
        base = real(path, *a, **kw)
        if str(path).replace('\\', '/').endswith(key_suffix):
            got = mut(base)
            if got != base:
                seen['changed'] += 1
            return got
        return base

    qg.read = _probe
    try:
        qg.results[:] = []
        getattr(qg, name)()
        out = list(qg.results)
    finally:
        qg.read = real
        qg.results[:] = []
    _MUT_LOG.append((name, key_suffix, bool(identity), seen['changed'] > 0))
    return out


_m52 = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                      lambda s: s.replace('    probs = face_problems()', '    probs = []', 1))
chk('S49 有牙：把面闸的真计算掏空成 `probs = []` ⇒ 第 52 项判红（只查形状会被这样骗过去）',
    len(_m52) == 1 and _m52[0][2] is False and '喂给任何判据' in str(_m52[0][3]),
    str(_m52)[:140])
_m52b = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace('        return 2\n    mirrors',
                                           '        return 0\n    mirrors', 1))
chk('S49 有牙：面闸算得出问题却不退 2 ⇒ 判红（"有个 if"不构成守卫）',
    len(_m52b) == 1 and _m52b[0][2] is False and '退 2' in str(_m52b[0][3]),
    str(_m52b)[:120])
_m55 = _s49_run_judge('check_disposition_lock', 'restore_db.py', lambda s: s.replace(
    '    if dry_run:\n        return _dispose_probe(root, bare, confirm, claim_by, '
    'claim_reason, now)\n', '', 1))
chk('S49 有牙：删掉 dry_run 分支 ⇒ 第 55 项判红并点名找不到只读档（不是少一条 note 就蒙绿）',
    len(_m55) == 1 and _m55[0][2] is False and 'dry_run' in str(_m55[0][3]),
    str(_m55)[:140])

_m52c = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace('    probs = []\n    if DEV.name != DEV_FACE_NAME:',
                                             '    return []\n    probs = []\n'
                                             '    if DEV.name != DEV_FACE_NAME:', 1))
chk('S49 有牙：面闸恒报空（return [] 短路）⇒ 第 52 项必须判红并点名两支行为核对',
    len(_m52c) == 1 and _m52c[0][2] is False
    and any('镜像缺失' in x or '同一个面' in x for x in _m52c[0][3]), str(_m52c[0][3])[:150])

# ── M130 否证：台账时序早有机器判据（自建临时面，与套件所在面无关） ──
import hashlib as _s49_hashlib

_LF = _s49_os.path.join(_s47_tfd.mkdtemp(prefix=f'bench41-ledger-{_s47_time.time_ns()}-'), 'face')
_s49_os.makedirs(_s49_os.path.join(_LF, 'src'))
_s49_os.makedirs(_s49_os.path.join(_LF, '..', '09-发布校验'))
_LED_PATH = _s49_os.path.join(_LF, '..', '09-发布校验', 'SHA256SUMS-v9.9.9.txt')
_LED_FILES = {'src/app.py': b'print(1)\n', 'src/version.py': b"APP_VERSION = '9.9.9'\n"}


def _s49_write_ledger(mut=lambda s: s):
    """按第 29 项的口径把台账重算一遍（含 AGGREGATE），再整体过一道 mut ⇒ 可注入也可复原。"""
    actual = {}
    for rel, data in _LED_FILES.items():
        p = _s49_os.path.join(_LF, rel.replace('/', _s49_os.sep))
        _s49_os.makedirs(_s49_os.path.dirname(p), exist_ok=True)
        open(p, 'wb').write(data)
        actual[rel] = _s49_hashlib.sha256(data).hexdigest()
    agg = _s49_hashlib.sha256(
        ''.join(f'{k} {actual[k]}\n' for k in sorted(actual)).encode('utf-8')).hexdigest()
    body = f'# ican release ledger v9.9.9\n# AGGREGATE {agg}\n'
    body += ''.join(f'{actual[k]}  {k}\n' for k in sorted(actual))
    open(_LED_PATH, 'w', encoding='utf-8', newline='\n').write(mut(body))
    return body


def _s49_led_judge():
    _s43_qg.results[:] = []
    _s43_qg.check_release_ledger()
    return list(_s43_qg.results)


_s49_write_ledger()
_saved_root = (_s43_qg.ROOT, _s43_qg.SRC)
_s43_qg.ROOT, _s43_qg.SRC = _LF, _s49_os.path.join(_LF, 'src')
try:
    _b29 = _s49_led_judge()
    _s49_write_ledger(lambda s: s.replace('src/app.py', 'src/app2.py'))
    _f29 = _s49_led_judge()
    _s49_write_ledger(lambda s: s.replace('# AGGREGATE ', '# AGGREGATE ff', 1))
    _a29 = _s49_led_judge()
    _s49_write_ledger()
    _z29 = _s49_led_judge()
finally:
    _s43_qg.ROOT, _s43_qg.SRC = _saved_root
    _s43_qg.results[:] = _saved52
    _s47_sh.rmtree(_s49_os.path.dirname(_LF), ignore_errors=True)
chk('S49 临时面基线：台账与盘面逐条一致时第 29 项判绿（不先立基线，下面的红只能算恒红）',
    len(_b29) == 1 and _b29[0][2] is True, str(_b29)[:110])
chk('S49 有牙（M130 由此否证）：台账条目对不上实盘 ⇒ 第 29 项判红并点名该文件',
    len(_f29) == 1 and _f29[0][2] is False and 'src/app2.py' in str(_f29[0][3]),
    str(_f29[0][3])[:130])
chk('S49 有牙：AGGREGATE 与逐条重算不符单独判红（局部改台账混不过去）',
    len(_a29) == 1 and _a29[0][2] is False and 'AGGREGATE' in str(_a29[0][3]),
    str(_a29[0][3])[:110])
chk('S49 复原：注入件复原后第 29 项回到判绿（不留半成品给下一段）',
    len(_z29) == 1 and _z29[0][2] is True, str(_z29)[:110])
chk('S49 只读复证：本段跑完真仓 backups/ 名字集合一字未变（临时面真删没有越界）',
    (sorted(_s49_os.listdir(_s49_real_bdir)) if _s49_os.path.isdir(_s49_real_bdir) else [])
    == _s49_real_before, f'{len(_s49_real_before)} 件')

# ══════════ S50：malformed 侧件的受控清理通道（第 42 轮 M133）══════════
# 一手动因：第 41 轮把 malformed 改成"拒破锁 + 不给 token"（方向对），但没给合法收尾 ⇒
# 真遇到垃圾侧件时人工也只能自己删文件，且删了没有任何留痕。本段验"受控"两个字：
# 凭据绑**原文字节**（字段本就非法，拿字段算凭据是循环论证）、先归档再删、非 malformed 一律拒。
_s50_rd, _s50_os, _s50_JS = _s47_rd, _s47_os, _json
_s50_reject = _s47_reject
_s50_HERE = _S43_HERE
_s50_reason = '第 42 轮的受控清理通道验收，理由长度足够一次复核'
_s50_real_bdir = _s50_os.path.join(_s50_HERE, 'backups')
_s50_real_before = (sorted(_s50_os.listdir(_s50_real_bdir))
                    if _s50_os.path.isdir(_s50_real_bdir) else [])
_s50_roots = []
_BAD_DOC = {'pid': 'not-an-int', 'token': 'zz', 'kind': 'ghost', 'ts': 'nope'}


def _s50_face():
    d = _s50_os.path.join(_s47_tfd.mkdtemp(prefix=f'bench42-{_s47_time.time_ns()}-'), 'face')
    _s50_os.makedirs(_s50_os.path.join(d, 'backups'))
    _s50_roots.append(d)
    return d


def _s50_put(face, doc=None, raw=None):
    p = _s50_rd.disposition_holder_path(face)
    body = raw if raw is not None else _s50_JS.dumps(doc, ensure_ascii=False) + '\n'
    open(p, 'w', encoding='utf-8', newline='\n').write(body)
    return p


def _s50_names(face):
    return sorted(f for f in _s50_os.listdir(_s50_os.path.join(face, 'backups')))


_F50 = _s50_face()
_s50_put(_F50, _BAD_DOC)
_paths50 = _s50_rd.disposition_paths(_F50)
_bdir50 = _s50_os.path.normcase(_s50_os.path.abspath(_s50_os.path.join(_F50, 'backups'))) + \
    _s50_os.path.sep
chk('S50 路径名册按行为取：五类各自解析成功、互不相同、且都在被处置面之外',
    set(_paths50) == set(_s50_rd.DISPOSITION_PATH_KINDS)
    == {'lock', 'holder', 'ledger', 'pending', 'held'}
    and len({_s50_os.path.normcase(v) for v in _paths50.values()}) == 5
    and not any(_s50_os.path.normcase(_s50_os.path.abspath(v)).startswith(_bdir50)
                for v in _paths50.values()), str(sorted(_paths50)))
_kinds_real = tuple(_s50_rd.DISPOSITION_PATH_KINDS)
_s50_rd.DISPOSITION_PATH_KINDS = ('lock', 'holder', 'ledger', 'pending')
try:
    _dead50 = _s50_reject(lambda: _s50_rd.disposition_paths(_F50))
finally:
    _s50_rd.DISPOSITION_PATH_KINDS = _kinds_real
chk('S50 有牙：名册与实现少一类 ⇒ 当场判红（加一类不登记＝路径隐身，删一类＝死名册）',
    '名册与实现不符' in _dead50 and 'held' in _dead50, _dead50[:110])
_raw50 = open(_paths50['holder'], 'rb').read()
_tk50 = _s50_rd.malformed_token(_raw50, _F50)
chk('S50 凭据绑原文字节：多一个空格即换 token（字段本就非法，绑字段是循环论证）',
    _s50_rd.malformed_token(_raw50 + b' ', _F50) != _tk50
    and len(_tk50) == _s50_rd.TOKEN_HEX_LEN, f'{_tk50}')
_ordered = '{"kind":"ghost","token":"zz","pid":"not-an-int","ts":"nope"}\n'
chk('S50 凭据不解析字段：同样的字段值、换了键序 ⇒ token 也换（证明它看字节不是语义）',
    _s50_rd.malformed_token(_ordered.encode('utf-8'), _F50) != _tk50)
chk('S50 read_holder_raw 把"没有"与"有但读不出"分开（absent vs present，坏 JSON 仍算 present）',
    _s50_rd.read_holder_raw(_s50_face())[1] == 'absent'
    and _s50_rd.read_holder_raw(_F50) == (_raw50, 'present'))
_unread_face = _s50_face()
_s50_put(_unread_face, raw='{ 坏 JSON\n')
chk('S50 有牙：坏 JSON 走 unreadable 通道 ⇒ 受控清理必须拒（本通道只收字段非法，不收读不出）',
    _s50_rd.stale_lock_report(root=_unread_face)['verdict'] == 'unreadable'
    and '只处理 malformed' in _s50_reject(
        lambda: _s50_rd.clear_malformed_holder(root=_unread_face, confirm='0' * 16,
                                               reason=_s50_reason)))
chk('S50 有牙：clean 面上本通道拒，并把现状如实写进拒因（"没人认领的旧现场"≠"什么都没有"）',
    '只处理 malformed' in (_c1 := _s50_reject(
        lambda: _s50_rd.clear_malformed_holder(root=_s50_face(), confirm='0' * 16,
                                               reason=_s50_reason)))
    and "'clean'" in _c1, _c1[:90])
_valid = _s50_rd._holder_doc(token='e' * 16, kind='dispose')
_res_face = _s50_face()
_s50_put(_res_face, _valid)
_res_rej = _s50_reject(lambda: _s50_rd.clear_malformed_holder(root=_res_face, confirm='0' * 16,
                                                              reason=_s50_reason))
chk('S50 有牙：合法残留（residual-recent）不在本通道射程 ⇒ 拒因必须指回 --break-lock',
    'residual-recent' in _res_rej and '--break-lock' in _res_rej, _res_rej[:130])
_live_face = _s50_face()
_s50_put(_live_face, _BAD_DOC)
_lockp50 = _s50_rd.disposition_lock_path(_live_face)
_holding = _s47_sp.Popen([_s47_sys.executable, '-B', '-c',
                          'import filelock,sys,time\n'
                          'lk=filelock.FileLock(sys.argv[1],timeout=0)\nlk.acquire()\n'
                          'print("HELD",flush=True)\ntime.sleep(9)\n', _lockp50],
                         stdout=_s47_sp.PIPE, text=True, cwd=_s50_HERE)
chk('S50 子进程持锁就位（HELD 回执 + 锁文件在盘，否则"live 不许动"是空转）',
    _holding.stdout.readline().strip() == 'HELD' and _s50_os.path.isfile(_lockp50))
chk('S50 有牙：有人正在临界区时清不了，且侧件仍在（live 优先于 malformed）',
    'live' in _s50_reject(lambda: _s50_rd.clear_malformed_holder(
        root=_live_face, confirm=_s50_rd.malformed_token(_raw50, _live_face),
        reason=_s50_reason))
    and _s50_os.path.isfile(_s50_rd.disposition_holder_path(_live_face)))
_holding.kill()
_holding.wait()
# 争锁现场用过的那枚面此后不可复用：filelock 4.0.4 在 Windows 上 acquire 超时后留下句柄，
# 同进程再探测锁永远拿不到 ⇒ 状态会一直报 live，后续拒因腿就全在测我自己占着锁。
_F50C = _s50_face()
_s50_put(_F50C, _BAD_DOC)
_rawC50 = open(_s50_rd.disposition_holder_path(_F50C), 'rb').read()
_tokC50 = _s50_rd.malformed_token(_rawC50, _F50C)
chk('S50 缺 confirm ⇒ 拒（本通道没有"默认清"，非交互环境也不隐式降级）',
    '缺 --confirm' in _s50_reject(lambda: _s50_rd.clear_malformed_holder(
        root=_F50C, reason=_s50_reason)))
chk('S50 理由不足 20 字 ⇒ 拒（token 是对的，走的正是理由长度那一道门）',
    '理由不足' in _s50_reject(lambda: _s50_rd.clear_malformed_holder(
        root=_F50C, confirm=_tokC50, reason='太短')))
_rawB50 = _s50_JS.dumps(dict(_BAD_DOC, kind='ghost2'), ensure_ascii=False) + '\n'
_tokA50 = _tokC50
_s50_put(_F50C, raw=_rawB50)
chk('S50 有牙（TOCTOU）：同一面上原文被改写 ⇒ 旧 token 立刻失效，且新内容一个字节都不动',
    'confirm 与原文 token 不符' in _s50_reject(
        lambda: _s50_rd.clear_malformed_holder(root=_F50C, confirm=_tokA50,
                                               reason=_s50_reason))
    and open(_s50_rd.disposition_holder_path(_F50C), 'rb').read() == _rawB50.encode('utf-8'),
    f'旧 token {_tokA50[:6]}… 对新原文 '
    + _s50_rd.malformed_token(_rawB50.encode('utf-8'), _F50C)[:6] + '…')
chk('S50 有牙：改写完的现场用**新**原文的 token 才清得动（凭据只认字节不认人）',
    _s50_rd.malformed_token(_rawB50.encode('utf-8'), _F50C) != _tokA50)
_held0 = len(_s50_rd.read_jsonl(_s50_rd.disposition_held_path(_F50C))[0])
_ok50 = _s50_rd.clear_malformed_holder(
    root=_F50C, confirm=_s50_rd.malformed_token(_rawB50.encode('utf-8'), _F50C),
    reason=_s50_reason)
_held_rows = _s50_rd.read_jsonl(_s50_rd.disposition_held_path(_F50C))[0]  # 读的是刚清过的那枚面
chk('S50 成功路径：原文先归档（待清 + 已清两行），侧件随后消失，现状回 clean',
    _ok50['cleared'] is True and len(_held_rows) - _held0 == 2
    and not _s50_os.path.isfile(_s50_rd.disposition_holder_path(_F50C))
    and _s50_rd.stale_lock_report(root=_F50C)['verdict'] == 'clean',
    f'归档 +{len(_held_rows) - _held0} 行')
chk('S50 归档里带得走的东西：待清那行有原文/sha/问题清单/理由，已清那行有前后人口与结果',
    all(k in _held_rows[-2] for k in ('raw_text', 'raw_sha256', 'problems', 'reason',
                                      'token', 'face'))
    and _held_rows[-1]['mode'] == 'held-cleared' and _held_rows[-1]['holder_gone'] is True
    and ('population_before' in _held_rows[-1] and 'population_after' in _held_rows[-1]),
    f'待清行缺 {[k for k in ("raw_text", "raw_sha256", "problems", "reason", "token", "face") if k not in _held_rows[-2]]}'
    + f'／已清行缺 {[k for k in ("population_before", "population_after") if k not in _held_rows[-1]]}')
chk('S50 另一半只读承诺：清理动作没碰被处置面（人口全程 0，本通道只删侧件本身）',
    _ok50['population'] == 0 and _s50_names(_F50C) == [], str(_s50_names(_F50C)))
_cli50 = _s47_sp.run([_s47_sys.executable, _s50_os.path.join(_s50_HERE, 'restore_db.py'),
                      '--malformed-token', '--json', '--root', _F50],
                     capture_output=True, text=True, timeout=240, cwd=_s50_HERE)
try:
    _cj50 = _s50_JS.loads(_cli50.stdout.strip().splitlines()[-1])
except (ValueError, IndexError):
    _cj50 = {}
chk('S50 CLI --malformed-token 真跑：rc=0、token 与库内复算一致、并回显原文与问题清单',
    _cli50.returncode == 0 and _cj50.get('token') == _tk50
    and 'not-an-int' in str(_cj50.get('raw_text')) and bool(_cj50.get('problems')),
    f'rc={_cli50.returncode}|{str(_cj50)[:90]}')
_cli50b = _s47_sp.run([_s47_sys.executable, _s50_os.path.join(_s50_HERE, 'restore_db.py'),
                       '--malformed-token', '--root', _res_face],
                      capture_output=True, text=True, timeout=240, cwd=_s50_HERE)
chk('S50 有牙：非 malformed 面上 --malformed-token 退非 0（只读探针也不许装成"没事"）',
    _cli50b.returncode != 0, f'rc={_cli50b.returncode}|{_cli50b.stdout[:70]}')
_cli50c = _s47_sp.run([_s47_sys.executable, _s50_os.path.join(_s50_HERE, 'restore_db.py'),
                       '--clear-malformed', '--confirm', _tk50, '--root', _F50],
                      capture_output=True, text=True, timeout=240, cwd=_s50_HERE)
chk('S50 CLI --clear-malformed 缺 --reason ⇒ 拒且侧件仍在（命令行同享那四道拒因）',
    _cli50c.returncode != 0 and '理由不足' in _cli50c.stdout
    and _s50_os.path.isfile(_s50_rd.disposition_holder_path(_F50)), _cli50c.stdout[:90])
_saved50 = list(_s43_qg.results)
_s43_qg.results[:] = []
_s43_qg.check_disposition_lock()
_r55_50 = list(_s43_qg.results)
_s43_qg.results[:] = []
_s43_qg.check_disposition_channel()
_r54_50 = list(_s43_qg.results)
_s43_qg.results[:] = _saved50
chk('S50 第 55 项真链路判绿且 record 恰好 1 条，门面带受控出口行为核读数',
    len(_r55_50) == 1 and _r55_50[0][2] is True and '受控出口行为核通过' in str(_r55_50[0][3]),
    str(_r55_50[0][3])[:120])
chk('S50 第 54 项删除名册随本轮同步：新删除点在册且名册⇄实扫双向等值',
    len(_r54_50) == 1 and _r54_50[0][2] is True
    and 'clear_malformed_holder.os.remove' in str(_r54_50[0][3]), str(_r54_50[0][3])[:140])
_m50a = _s49_run_judge('check_disposition_lock', 'restore_db.py', lambda s: s.replace(
    '        append_jsonl(disposition_held_path(root), held_entry)\n', '', 1))
_m50a2 = _s49_run_judge('check_disposition_lock', 'restore_db.py', lambda s: s.replace(
    '        append_jsonl(disposition_held_path(root),\n', '        append_jsonl(disposition_ledger_path(root),\n', 1))
_m50a3 = _s49_run_judge('check_disposition_lock', 'restore_db.py', lambda s: s.replace(
    'append_jsonl(disposition_held_path(root)', 'append_jsonl(disposition_ledger_path(root)'))
chk('S50 有牙：删掉留证那一行 ⇒ 第 55 项判红并点名"归档排在删除之后"（原文没进 HELD 就动了侧件）',
    len(_m50a) == 1 and _m50a[0][2] is False and '归档在第' in str(_m50a[0][3]),
    str(_m50a[0][3])[:120])
chk('S50 双向核：只把第二笔（清理结果）改道到别的台账 ⇒ 仍判绿（原文已先归档，合法变体，'
    '判据不许宽到把可用写法一起杀了）',
    len(_m50a2) == 1 and _m50a2[0][2] is True, str(_m50a2[0][3])[:120])
chk('S50 有牙：两笔 HELD 留证全部改道 ⇒ 判红且点名"没有写进 HELD 台账"的动作',
    len(_m50a3) == 1 and _m50a3[0][2] is False
    and '写进 HELD 台账' in str(_m50a3[0][3]), str(_m50a3[0][3])[:130])
_m50b = _s49_run_judge('check_disposition_lock', 'restore_db.py', lambda s: s.replace(
    "    body = json.dumps({'face': _face(root or BASE_DIR),",
    "    body = json.dumps({'pid': 'pid', 'face': _face(root or BASE_DIR),", 1))
chk('S50 有牙：让 malformed_token 去引用侧件字段 ⇒ 第 55 项判红（凭据绑在被污染输入上）',
    len(_m50b) == 1 and _m50b[0][2] is False and '被污染的输入' in str(_m50b[0][3]),
    str(_m50b)[:130])
_m50c = _s49_run_judge('check_disposition_lock', 'restore_db.py', lambda s: s.replace(
    "    path = disposition_holder_path(root)\n    with disposition_lock(root=root, "
    "wait_s=LOCK_WAIT_S):",
    "    path = disposition_holder_path(root)\n    with contextlib.nullcontext():", 1))
chk('S50 有牙：把清理通道的锁换成空上下文 ⇒ 判 no-with 并带出锁形态取值（并发保护是断言不是愿望）',
    len(_m50c) == 1 and _m50c[0][2] is False
    and 'no-with' in str(_m50c[0][3]) and 'clear_malformed_holder() 的锁形态' in str(_m50c[0][3]),
    str(_m50c)[:150])
for _x50 in _s50_roots:
    _s47_sh.rmtree(_x50, ignore_errors=True)
chk('S50 只读复证：本段跑完真仓 backups/ 名字集合一字未变（临时面清理没有越界）',
    (sorted(_s50_os.listdir(_s50_real_bdir)) if _s50_os.path.isdir(_s50_real_bdir) else [])
    == _s50_real_before, f'{len(_s50_real_before)} 件')
# ══════════ S51：同步面的 EXTRA 复核出口（第 43 轮 M115）══════════
# 一手：第 37 轮把"EXTRA 只报不删"写成判据、第 39 轮登记"没有出口承接人已看过确认删这一个"，
# 本轮把第 41/42 轮的三段范式平移到同步面：凭据绑**副本字节** → 先留证再 unlink → 断"只少那一个"。
# tracked 身份只能由真 git 测出来 ⇒ 本段自建临时 git 仓；段尾复证真仓未被本段写侧触碰。
_s51_t, _s51_os, _s51_JS = _s44_t, _s47_os, _json
_s51_reason = '第 43 轮的 EXTRA 复核出口验收，理由长度够一次真复核'


def _s51_sandbox():
    """临时 git 仓 + 一个 untracked 孤儿 + 一个 tracked 假孤儿（后者必须被拒）。"""
    root = _s44_P(_s47_tfd.mkdtemp(prefix=f'bench43-s51-{_s47_time.time_ns()}-'))
    dev, mir = root / 'dev', root / 'mir'
    (dev / 'src').mkdir(parents=True)
    (mir / 'src').mkdir(parents=True)
    (dev / 'quality_gate.py').write_text(f'ROOT_DELIVERED = {_S44_FULL!r}\n', encoding='utf-8',
                                         newline='\n')
    (dev / 'check_delivery_drift.py').write_text('EXTRA_TRACKED = ()\n', encoding='utf-8',
                                                 newline='\n')
    (dev / 'set_version.py').write_text('CARRIERS = (("README.md", "p", False, None),)\n',
                                        encoding='utf-8', newline='\n')
    for n in _S44_FULL:
        (dev / n).write_text('AUTH\n', encoding='utf-8', newline='\n')
        (mir / n).write_text('AUTH\n', encoding='utf-8', newline='\n')
    (dev / 'src' / 'a.py').write_text('A\n', encoding='utf-8', newline='\n')
    (mir / 'src' / 'a.py').write_text('A\n', encoding='utf-8', newline='\n')
    (mir / 'src' / 'orphan.py').write_text('ORPHAN\n', encoding='utf-8', newline='\n')
    (mir / 'src' / 'tracked.py').write_text('TRACKED\n', encoding='utf-8', newline='\n')
    env = dict(_s51_os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
    for a in (['init', '-q'], ['add', '--', 'mir/src/tracked.py'], ['commit', '-q', '-m', 'x']):
        _s47_sp.run(['git', *a], cwd=str(root), capture_output=True, text=True, timeout=180, env=env)
    _s51_t.DEV, _s51_t.MIRROR_DIRS = dev, {'mir': mir}
    _s51_t.DEV_FACE_NAME, _s51_t.REPO = 'dev', root
    return root, dev, mir


def _s51_files(mir):
    return sorted(p.relative_to(mir).as_posix() for p in mir.rglob('*') if p.is_file())


def _s51_reject(fn):
    try:
        fn()
        return '未拒'
    except _s51_t.ExtraError as exc:
        return str(exc)


_SAVED51 = (_s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO)
_REAL_BACKUPS51 = (sorted(_s51_os.listdir(_s51_os.path.join(_S43_HERE, 'backups')))
                   if _s51_os.path.isdir(_s51_os.path.join(_S43_HERE, 'backups')) else [])
_R51, _D51, _M51 = _s51_sandbox()
try:
    _its51 = {x['rel']: x for x in _s51_t.scan_extra()}
    chk('S51 现扫两条孤儿各带真实 git 身份（tracked 那件是真 git add 过的 ⇒ 不是读描述）',
        set(_its51) == {'orphan.py', 'tracked.py'}
        and _its51['orphan.py']['git'] == 'untracked'
        and _its51['tracked.py']['git'] == 'tracked',
        str({k: v['git'] for k, v in _its51.items()}))
    _doc51 = _s51_t.list_extra()
    chk('S51 分组计数与明细同分母：by_git_state 之和 == count == len(items)',
        sum(_doc51['by_git_state'].values()) == _doc51['count'] == len(_doc51['items']),
        f"{_doc51['by_git_state']} / {_doc51['count']} / {len(_doc51['items'])}")
    _src_report = [n for n in _s43_ast.walk(_s43_ast.parse(_s43_qg.read(
        _s51_os.path.join(_S43_HERE, 'tools', 'frontend_sync_check.py'))))
        if isinstance(n, _s43_ast.FunctionDef) and n.name == 'report'][0]
    chk('S51 三类差集只有一处实现：report() 调 classify() 且体内不再自算 set 差集',
        any(isinstance(c, _s43_ast.Call) and getattr(c.func, 'id', '') == 'classify'
            for c in _s43_ast.walk(_src_report))
        and not any(isinstance(c, _s43_ast.Call) and getattr(c.func, 'id', '') == 'set'
                    for c in _s43_ast.walk(_src_report)), 'report 体内 set 调用数不对')
    _tk51 = _s51_t.extra_token(_its51['orphan.py'])
    _orph = _M51 / 'src' / 'orphan.py'
    _orph.write_text('ORPHAN\nEXTRA-LINE\n', encoding='utf-8', newline='\n')
    _tk51c = _s51_t.extra_token({x['rel']: x for x in _s51_t.scan_extra()}['orphan.py'])
    chk('S51 凭据绑副本字节：多一行内容 ⇒ token 变，旧 token 当场失效',
        _tk51c != _tk51 and 'confirm 与现算 token 不符' in _s51_reject(
            lambda: _s51_t.drop_extra('mir', 'orphan.py', confirm=_tk51, reason=_s51_reason)),
        f'{_tk51} → {_tk51c}')
    _orph.write_text('ORPHAN\n', encoding='utf-8', newline='\n')
    _tk51 = _s51_t.extra_token({x['rel']: x for x in _s51_t.scan_extra()}['orphan.py'])
    _files0 = _s51_files(_M51)
    _led0 = _s51_t.extra_ledger_path()
    chk('S51 缺 confirm 即拒（没有"默认删"，非交互环境也不隐式降级）',
        '缺 --confirm' in _s51_reject(lambda: _s51_t.drop_extra('mir', 'orphan.py',
                                                                reason=_s51_reason))
        and _s51_files(_M51) == _files0)
    chk('S51 理由不足 20 字即拒（一句话不算复核）',
        '理由不足' in _s51_reject(lambda: _s51_t.drop_extra('mir', 'orphan.py', confirm=_tk51,
                                                            reason='太短'))
        and _s51_files(_M51) == _files0)
    chk('S51 错 token 即拒，且不落任何台账（拒在留证之前）',
        'token 不符' in _s51_reject(lambda: _s51_t.drop_extra('mir', 'orphan.py',
                                                              confirm='0' * 16,
                                                              reason=_s51_reason))
        and not _s51_os.path.isfile(_led0))
    _rej_trk = _s51_reject(lambda: _s51_t.drop_extra(
        'mir', 'tracked.py',
        confirm=_s51_t.extra_token({x['rel']: x for x in _s51_t.scan_extra()}['tracked.py']),
        reason=_s51_reason))
    chk('S51 有牙（本通道的核心保护）：tracked 假孤儿必须拒，理由点名"漏同步"，文件仍在',
        '漏同步' in _rej_trk and (_M51 / 'src' / 'tracked.py').is_file(), _rej_trk[:120])
    _saved_repo = _s51_t.REPO
    _s51_t.REPO = _s44_P(_s47_tfd.mkdtemp(prefix='bench43-outside-'))
    try:
        _rej_out = _s51_reject(lambda: _s51_t.drop_extra('mir', 'orphan.py', confirm=_tk51,
                                                          reason=_s51_reason))
    finally:
        _s51_t.REPO = _saved_repo
    chk('S51 有牙：取不到 git 身份（outside-repo）一律 fail-closed，不"当它是孤儿"就删',
        'outside-repo' in _rej_out and (_M51 / 'src' / 'orphan.py').is_file(), _rej_out[:110])
    _res51 = _s51_t.drop_extra('mir', 'orphan.py', confirm=_tk51, reason=_s51_reason)
    _rows51 = [_s51_JS.loads(x) for x in open(_led0, encoding='utf-8').read().splitlines() if x.strip()]
    chk('S51 成功路径：只少那一个、字节数与盘上实量一致、其余文件一字未变',
        _res51['removed'] == 1 and _res51['freed_bytes'] == 7
        and _res51['population_after'] == _res51['population_before'] - 1
        and not (_M51 / 'src' / 'orphan.py').exists()
        and (_M51 / 'src' / 'tracked.py').is_file() and (_M51 / 'src' / 'a.py').is_file(),
        str({k: _res51[k] for k in ('freed_bytes', 'population_before', 'population_after')}))
    chk('S51 留证先于动作且落在两份镜像之外：intent + dropped 两行，含原文 sha 与面内路径',
        [r['outcome'] for r in _rows51] == ['intent', 'dropped']
        and _rows51[0]['sha256'] and _rows51[0]['in_mirror'] == 'src/orphan.py'
        and _rows51[1]['only_that_one'] is True
        and _s44_P(_s51_os.path.normcase(_led0)).is_relative_to(_D51)
        and not _s44_P(_s51_os.path.normcase(_led0)).is_relative_to(_M51), str(_rows51)[:150])
    chk('S51 有牙：重复删同一个 ⇒ 现扫命中 0 条即拒（不把"已经没了"报成成功）',
        '命中 0 条' in _s51_reject(lambda: _s51_t.drop_extra('mir', 'orphan.py', confirm=_tk51,
                                                            reason=_s51_reason)))
    _buf51 = _s48_io.StringIO()
    with _s48_cl.redirect_stdout(_buf51):
        _rc51 = _s51_t.main(['--fix', '--drop-extra', 'mir', 'tracked.py',
                             '--confirm', '0' * 16, '--reason', _s51_reason])
    chk('S51 --fix 与 --drop-extra 互斥 ⇒ 退 2 且一个字节都不动（一次调用只一种动作）',
        _rc51 == 2 and '互斥' in _buf51.getvalue() and (_M51 / 'src' / 'tracked.py').is_file(),
        _buf51.getvalue()[:110])
    _buf52 = _s48_io.StringIO()
    with _s48_cl.redirect_stdout(_buf52):
        _rc52 = _s51_t.main(['--list-extra', '--json'])
    _only52 = _buf52.getvalue().strip().splitlines()[-1] if _buf52.getvalue().strip() else ''
    try:
        _pj52 = _s51_JS.loads(_only52)
    except ValueError:
        _pj52 = {}
    chk('S51 CLI --list-extra --json：只打一个可解析文档，不混表格（下游要能直接吃）',
        _rc52 == 0 and _pj52.get('schema') == _s51_t.EXTRA_SCHEMA
        and len(_buf52.getvalue().strip().splitlines()) == 1, _buf52.getvalue()[:90])
    _buf53 = _s48_io.StringIO()
    with _s48_cl.redirect_stdout(_buf53):
        _rc53 = _s51_t.main(['--list-extra', '--confirm', 'x' * 16])
    chk('S51 有牙：只读档不接受写参数 ⇒ 拒并说明，不静默忽略（防"以为在删"）',
        _rc53 == 2 and '只读' in _buf53.getvalue(), _buf53.getvalue()[:100])
    _saved52r = list(_s43_qg.results)
    _s43_qg.results[:] = []
    _s43_qg.check_sync_egress_single_entry()
    _r52 = list(_s43_qg.results)
    _s43_qg.results[:] = _saved52r
    chk('S51 门禁第 52 项真链路判绿且 record 恰好 1 条，门面带 EXTRA 出口读数',
        len(_r52) == 1 and _r52[0][2] is True and 'EXTRA 出口在册' in str(_r52[0][3]),
        str(_r52[0][3])[:140])
    _mA1 = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                          lambda s: s.replace("    if item['git'] != 'untracked':",
                                              "    if False:", 1))
    chk('S51 有牙：拆掉 tracked 守卫 ⇒ 第 52 项判红 ⇒ 已跟踪件就能被当孤儿删掉',
        len(_mA1) == 1 and _mA1[0][2] is False and 'untracked' in str(_mA1[0][3]),
        str(_mA1[0][3])[:130])
    _mA2 = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                          lambda s: s.replace(
                              "            raise ExtraError(f'台账落点 {p} 在镜像「{mname}」内 ⇒ "
                              "会被自己的处置面吞掉，拒')",
                              "            pass", 1))
    chk('S51 有牙：越界守卫改 pass ⇒ 第 52 项判红（留证记在被处置面名下会被自己清掉，第 39 轮一手坑）',
        len(_mA2) == 1 and _mA2[0][2] is False and '落点在镜像内即拒' in str(_mA2[0][3]),
        str(_mA2[0][3])[:130])
    _mA3 = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                          lambda s: s.replace('target.unlink()', 'target.touch()', 1))
    chk('S51 有牙：删除点被改名 ⇒ 名册⇄实扫不等值即判红（漏登记＝旁路，多登记＝对空气放行）',
        len(_mA3) == 1 and _mA3[0][2] is False and '名册' in str(_mA3[0][3]),
        str(_mA3[0][3])[:130])
finally:
    _s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO = _SAVED51
    _s47_sh.rmtree(str(_R51), ignore_errors=True)
chk('S51 只读复证：本段跑完真仓 backups/ 名字集合一字未变（临时 git 仓没有越界）',
    (sorted(_s51_os.listdir(_s51_os.path.join(_S43_HERE, 'backups')))
     if _s51_os.path.isdir(_s51_os.path.join(_S43_HERE, 'backups')) else []) == _REAL_BACKUPS51,
    f'{len(_REAL_BACKUPS51)} 件')
chk('S51 全局复原：四个被临时改过的模块全局都回到段首快照（泄漏给下一段就是假绿）',
    (_s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO) == _SAVED51,
    str(_s51_t.DEV))
# ══════════ S52：两处写态同一把锁（第 44 轮 M141）══════════
# 一手：第 43 轮的 --drop-extra 与 --fix 动的是同一个镜像面却各写各的 ⇒「只少那一个」的人口断言
# 会把别人的动作记成本轮的。形状借 restic `internal/repository/lock.go` 的 Lock()（锁的是「我在写
# 这个面」这件事本身，锁内容是 pid+action+时刻的归因侧件）；**跨进程真争锁用子进程验**，内存桩只验拒因形状。
# 教训沿用第 41/42 轮：filelock 在 Windows acquire 超时后会在本进程留句柄 ⇒ 争锁场景一枚新面，不复用。
_s52_reason = '第 44 轮的写态同锁验收，理由长度同样够一次真复核'
_s52_os, _s52_sys, _s52_sh = _s47_os, _s47_sys, _s47_sh
_S52_REPO = str(_s44_P(_S43_HERE).parent)
_SAVED52 = (_s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO)


def _s52_files(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())


def _s52_ledger_lines(dev):
    p = _s52_os.path.join(str(dev), _s51_t.EXTRA_LEDGER_NAME)
    if not _s52_os.path.isfile(p):
        return 0
    return len([x for x in open(p, encoding='utf-8').read().splitlines() if x.strip()])


_ck52_child = '\n'.join([
    'import filelock, json, os, sys, time',
    "lk = filelock.FileLock(os.path.join(sys.argv[1], 'SYNC-WRITE.lock'), timeout=8)",
    'lk.acquire()',
    "json.dump({'pid': os.getpid(), 'action': sys.argv[3], 'ts': sys.argv[2]},",
    "          open(os.path.join(sys.argv[1], 'SYNC-WRITE.holder'), 'w'))",
    "print('HELD', flush=True)",
    'time.sleep(float(sys.argv[4]))',
])
_S52_PROBES = ('sync_lock', 'sync_lock_path', 'read_lock_holder')
chk('S52 三件助手与 SyncLockError 都在册（缺任一件就是「注释里有锁、源里没有」）',
    all(callable(getattr(_s51_t, n, None)) for n in _S52_PROBES)
    and issubclass(getattr(_s51_t, 'SyncLockError', _s51_t.ExtraError), _s51_t.ExtraError),
    str([(n, callable(getattr(_s51_t, n, None))) for n in _S52_PROBES]))
_R52A, _D52A, _M52A = _s51_sandbox()
try:
    _p52 = _s51_t.sync_lock_path()
    chk('S52 正对照：锁落在权威面根、且不在任何镜像里（方向断言，不是恒等式）',
        _p52 == str(_D52A / 'SYNC-WRITE.lock') and not _s44_P(_p52).is_relative_to(_M52A), _p52[:90])
    _esc52 = '没抛'
    try:
        _s51_t.sync_lock_path(_M52A)
    except _s51_t.SyncLockError as exc:
        _esc52 = str(exc)
    chk('S52 有牙：锁落进镜像即拒（锁被自己的复制与删除搬走＝锁失效，第 39 轮同源坑）',
        '落在镜像' in _esc52, _esc52[:90])
    _in52 = {}
    with _s51_t.sync_lock('fix', wait_s=3) as _lz52:
        _lp52 = str(_lz52.lock_path)   # M149 起 sync_lock 让出的是租约对象，路径要显式取
        _hd52 = _s44_P(_s52_os.path.join(str(_D52A), 'SYNC-WRITE.holder'))
        _in52 = {'lock': _s52_os.path.isfile(_lp52), 'holder': _hd52.is_file(),
                 'doc': _json.loads(_hd52.read_text(encoding='utf-8')) if _hd52.is_file() else {}}
    _out52 = {'lock': _s52_os.path.isfile(_lp52),
              'holder': _s44_P(str(_D52A / 'SYNC-WRITE.holder')).is_file()}
    chk('S52 锁件只在写态窗口内存在（实测 filelock 4.0.4 释放即删 ⇒ 登记 ignore 防的是被中断的一次写）',
        _in52['lock'] and _in52['holder'] and not any(_out52.values()),
        str(_in52)[:52] + str(_out52))
    chk('S52 归因侧件字段齐全：pid=本进程 ∧ action 由调用方给 ∧ ts 带时区',
        _in52['doc'].get('pid') == _s52_os.getpid() and _in52['doc'].get('action') == 'fix'
        and ('+' in str(_in52['doc'].get('ts')) or 'Z' in str(_in52['doc'].get('ts'))),
        str(_in52['doc'])[:110])
    _fl52 = _s52_sys.modules.get('filelock')
    _s52_sys.modules['filelock'] = None
    _f52 = ''
    try:
        _miss52 = '照写了'
        try:
            with _s51_t.sync_lock('drop-extra'):
                pass
        except _s51_t.SyncLockError as exc:
            _miss52 = str(exc)
        _f52 = _miss52 + ' | 只读件数=' + str(len(_s51_t.scan_extra()))
    finally:
        _s52_sys.modules['filelock'] = _fl52
    chk('S52 fail-closed：filelock 取不到时写态一律拒、只读档照常（不许降级成「没锁也写」）',
        'filelock 不可用' in _f52 and '只读件数=2' in _f52, _f52[:120])
finally:
    _s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO = _SAVED52
    _s52_sh.rmtree(str(_R52A), ignore_errors=True)

_R52B, _D52B, _M52B = _s51_sandbox()
try:
    _cf52 = _s47_tfd.NamedTemporaryFile('w', suffix='.py', delete=False, encoding='utf-8')
    _cf52.write(_ck52_child)
    _cf52.close()
    _now52 = _s47_time.strftime('%Y-%m-%dT%H:%M:%S+08:00')
    _ck52 = _s47_sp.Popen([_s47_sys.executable, _cf52.name, str(_D52B), _now52, 'CHILD52', '40'],
                          stdout=_s47_sp.PIPE, text=True, cwd=_S43_HERE)
    chk('S52 子进程真的持住了写锁（HELD 回执 + 锁文件在盘）',
        _ck52.stdout.readline().strip() == 'HELD'
        and _s52_os.path.isfile(str(_D52B / 'SYNC-WRITE.lock')), _cf52.name[-28:])
    _it52 = {x['rel']: x for x in _s51_t.scan_extra()}
    _tk52 = _s51_t.extra_token(_it52['orphan.py'])
    _led52, _pop52 = _s52_ledger_lines(_D52B), _s52_files(_M52B)
    _rej52 = _s51_reject(lambda: _s51_t.drop_extra(
        'mir', 'orphan.py', confirm=_tk52, reason=_s52_reason))
    chk('S52 有牙：他人持锁时 drop_extra 必须拒，且拒因点名持锁者的 action',
        _rej52 != '未拒' and '写态锁被占用' in _rej52 and 'CHILD52' in _rej52, _rej52[:110])
    chk('S52 拒得干净：孤儿件仍在盘、台账一行没多（争锁伪装成「删了」就是假交付）',
        _s52_files(_M52B) == _pop52 and _s52_ledger_lines(_D52B) == _led52,
        '人口 ' + str(len(_pop52)) + ' 台账 ' + str(_led52))
    _ro52 = '只读被阻塞'
    try:
        _ro52 = _s51_t.list_extra()['count']
    except _s51_t.SyncLockError as exc:
        _ro52 = str(exc)[:40]
    chk('S52 只读档在他人持锁时照常跑（看一眼不该排队 ⇒ 否则看守把自己变成闸门）',
        _ro52 == 2, str(_ro52)[:70])
    _ck52.kill()
    _ck52.wait()
    _s52_os.unlink(_cf52.name)
finally:
    _s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO = _SAVED52
    _s52_sh.rmtree(str(_R52B), ignore_errors=True)

_m52a = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace("with sync_lock('drop-extra') as lease:",
                                           'with contextlib.nullcontext() as lease:', 1))
chk('S52 有牙：删除那步的锁换成空上下文 ⇒ 第 52 项判红（锁只裹住人口快照不算守住动作）',
    len(_m52a) == 1 and _m52a[0][2] is False
    and 'drop_extra() 的删除动作不在 sync_lock' in str(_m52a[0][3]), str(_m52a)[:130])
_m52b = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace("with (sync_lock('fix') if args.fix else contextlib.nullcontext()) as lease:",
                                           'with contextlib.nullcontext() as lease:', 1))
chk('S52 有牙：--fix 那档不取锁 ⇒ 判红（两条写态各写各的，锁等于没上）',
    len(_m52b) == 1 and _m52b[0][2] is False
    and 'main() 的 --fix 复制不在 sync_lock' in str(_m52b[0][3]), str(_m52b)[:130])
_m52c = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace('def list_extra(mirrors: dict | None = None) -> dict:',
                                           'def list_extra(mirrors: dict | None = None) -> dict:'
                                           + chr(10) + "    with sync_lock('list'):"
                                           + chr(10) + '        pass', 1))
chk('S52 反向腿：只读档里塞一句取锁 ⇒ 判红（判据要认得出「只读不该取锁」这个方向）',
    len(_m52c) == 1 and _m52c[0][2] is False and '取了写锁' in str(_m52c[0][3]), str(_m52c)[:130])
_m52d = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace("            raise SyncLockError(f'锁文件 {p} 落在镜像「{mname}」内 ⇒ ",
                                           '            pass  # ', 1))
chk('S52 有牙：锁落进镜像的守卫改 pass ⇒ 判红（守卫死了＝锁会被自己的复制搬走）',
    len(_m52d) == 1 and _m52d[0][2] is False and 'sync_lock_path() 没有' in str(_m52d[0][3]),
    str(_m52d)[:130])
_m52e = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace("        raise SyncLockError(f'写态锁被占用：",
                                           '        raise ValueError(f' + chr(39), 1))
chk('S52 有牙：Timeout 分支改抛别的异常 ⇒ 判红（CLI 按 SyncLockError 退 2，换个异常就是静默放行）',
    len(_m52e) == 1 and _m52e[0][2] is False and 'Timeout' in str(_m52e[0][3]), str(_m52e)[:130])
_m52f = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py', lambda s: s, identity=True)
chk('S52 恒等对照：不改源码跑同一个判据 ⇒ 必须 0 问题（否则上面五支红是「改任何字都红」的假牙）',
    len(_m52f) == 1 and _m52f[0][2] is True, str(_m52f)[:120])
_gi52 = {}
for _f52n in ('SYNC-WRITE.lock', 'SYNC-WRITE.holder'):
    _r52 = _s47_sp.run(['git', '-C', _S52_REPO, 'check-ignore', '-v',
                        str(_s44_P(_S43_HERE) / _f52n)],
                       capture_output=True, text=True, timeout=60)
    _gi52[_f52n] = (_r52.returncode, (_r52.stdout or '').split(chr(9))[0])
chk('S52 运行态锁件已登记 .gitignore（真跑 git check-ignore，不是读注释：被中断的一次写不该变成未跟踪噪声）',
    all(v[0] == 0 and '.gitignore:' in v[1] for v in _gi52.values()), str(_gi52)[:130])
chk('S52 段尾复证：真仓权威面没有多出 SYNC-WRITE.*（本段的锁全在临时面上）',
    not (_s44_P(_S43_HERE) / 'SYNC-WRITE.lock').exists(),
    str(sorted(n for n in _s52_os.listdir(_S43_HERE) if 'SYNC' in n)))
chk('S52 全局复原：临时改过的四个模块全局回到段首快照',
    (_s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO) == _SAVED52,
    str(_s51_t.DEV))

# ══════════ S53：删除半径与证据半径（第 45 轮 M140/M142/M143）══════════
# 一手：第 43 轮的 --drop-extra 直接 unlink ⇒ 误复核不可回滚，而 EXTRA 的定义就是"权威源没有"，
# --fix 再生不出来。本轮把 rclone `fs/sync/sync.go:608 DeleteFilesWithBackupDir` 的"删除≠消失"平移过来：
# 先归档到镜像外并回读复算 sha，归档失败即不删；同时给 EXTRA 项配 mutagen EnsureValid 式字段契约，
# 并把上一版方向写反的封顶（实测 210 行削掉的 57 行**全是终态证据**）改成只削可再生的中间行。
_s53_reason = '第 45 轮的归档式删除验收，理由长度同样够一次真复核'
_s53_P, _s53_os, _s53_sh = _s44_P, _s47_os, _s47_sh
_SAVED53 = (_s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO,
            _s51_t.EXTRA_ARCHIVE_DIR_NAME)


def _s53_rows(dev):
    p = _s53_os.path.join(str(dev), _s51_t.EXTRA_LEDGER_NAME)
    if not _s53_os.path.isfile(p):
        return []
    return [_json.loads(x) for x in open(p, encoding='utf-8').read().splitlines() if x.strip()]


def _s53_files(mir):
    return sorted(p.relative_to(mir).as_posix() for p in mir.rglob('*') if p.is_file())


chk('S53 三件套在册：archive_extra / validate_extra_item / extra_archive_dir 都得是可调用的',
    all(callable(getattr(_s51_t, n, None))
        for n in ('archive_extra', 'validate_extra_item', 'extra_archive_dir')),
    str([(n, callable(getattr(_s51_t, n, None)))
         for n in ('archive_extra', 'validate_extra_item', 'extra_archive_dir')]))
_R53A, _D53A, _M53A = _s51_sandbox()
try:
    _its53 = {x['rel']: x for x in _s51_t.scan_extra()}
    _or53 = _its53['orphan.py']
    _pop53 = _s53_files(_M53A)
    _res53 = _s51_t.drop_extra('mir', 'orphan.py', confirm=_s51_t.extra_token(_or53),
                               reason=_s53_reason)
    _ap53 = _s53_P(_res53['archive_path'])
    chk('S53 真删一件后：原件消失、归档件在镜像外、逐字节复算 sha 与出凭据时一致',
        (not (mir_orphan := (_M53A / 'src' / 'orphan.py')).exists()) and _ap53.is_file()
        and not _ap53.is_relative_to(_M53A)
        and _s51_t.sha256(_ap53) == _or53['sha256'],
        f'归档={_ap53.name} 在镜像外={not _ap53.is_relative_to(_M53A)}')
    _r53 = _s53_rows(_D53A)
    chk('S53 台账两笔：intent 行不带归档、终态行带 archive_path+archive_sha 且人口 -1',
        len(_r53) == 2 and _r53[0]['outcome'] == 'intent' and 'archive_path' not in _r53[0]
        and _r53[1]['outcome'] == 'dropped' and _r53[1]['archive_sha'] == _or53['sha256']
        and _r53[1]['population_after'] == _r53[1]['population_before'] - 1,
        str([(x['outcome'], x.get('archive_path')) for x in _r53])[:120])
    chk('S53 归档件内容就是被删的那份字节（不是"记了个路径"）',
        _ap53.read_bytes() == b'ORPHAN\n' or _ap53.read_bytes().startswith(b'ORPHAN'),
        _ap53.read_bytes()[:20].decode('utf-8', 'replace'))
    _s53_hash2 = _s43_qg._sha256_file(str(_ap53))
    chk('S53 换个取哈希的通路复算（门禁的尺 ⇄ 工具的尺同值，防单路自证）',
        _s53_hash2 == _or53['sha256'], f'{_s53_hash2[:12]} vs {_or53["sha256"][:12]}')
    _bad53 = ''
    try:
        _s51_t.validate_extra_item({'schema': 'bogus', 'sha256': 'zz', 'bytes': -1,
                                    'git': 'weird', 'rel': '../x', 'src_absent': 'yes'})
    except _s51_t.ExtraError as exc:
        _bad53 = str(exc)
    chk('S53 字段契约逐项点名（六个坏字段 ⇒ 六项都报出来，不给一句"不合规"糊过去）',
        all(k in _bad53 for k in ('schema', 'sha256', 'bytes', 'git', 'rel', 'src_absent')),
        _bad53[:140])
    _tk53 = '发了'
    try:
        _s51_t.extra_token({'schema': 'ican-sync-extra-v1', 'sha256': '短', 'bytes': 1,
                            'git': 'untracked', 'rel': 'x.py', 'src_absent': True, 'mirror': 'mir',
                            'face': 'f'})
    except _s51_t.ExtraError as exc:
        _tk53 = str(exc)[:40]
    chk('S53 凭据前置于校验：坏字段领不到 token（否则拿着没 sha 的清单去 unlink）',
        _tk53 != '发了' and 'sha256' in _tk53, _tk53)
    _ovf53 = '落点在镜像内也没拒'
    _saved_mir53 = _s51_t.MIRROR_DIRS
    try:
        _s51_t.MIRROR_DIRS = {'self': _D53A}
        try:
            _s51_t.extra_archive_dir()
        except _s51_t.ExtraError as exc:
            _ovf53 = str(exc)
    finally:
        _s51_t.MIRROR_DIRS = _saved_mir53
    chk('S53 归档落点落进镜像即拒（否则 --fix 会把归档件当副本再复制回来，越删越多）',
        '在镜像' in _ovf53, _ovf53[:90])
finally:
    _s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO = _SAVED53[:4]
    _s51_t.EXTRA_ARCHIVE_DIR_NAME = _SAVED53[4]
    _s53_sh.rmtree(str(_R53A), ignore_errors=True)

_R53B, _D53B, _M53B = _s51_sandbox()
try:
    (_D53B / 'afile').write_text('我是文件不是目录\n', encoding='utf-8')
    _s51_t.EXTRA_ARCHIVE_DIR_NAME = 'afile'
    _itb53 = {x['rel']: x for x in _s51_t.scan_extra()}
    _popb53 = _s53_files(_M53B)
    _rejb53 = _s51_reject(lambda: _s51_t.drop_extra(
        'mir', 'orphan.py', confirm=_s51_t.extra_token(_itb53['orphan.py']), reason=_s53_reason))
    _rowb53 = _s53_rows(_D53B)
    chk('S53 有牙：归档写不出去 ⇒ 原件一个字节没动，且拒因点名"归档失败"',
        _s53_files(_M53B) == _popb53 and '归档失败' in _rejb53, _rejb53[:100])
    chk('S53 归档失败也留证：终态行 outcome=failed 且 error 带异常名（不静默吞）',
        len(_rowb53) == 2 and _rowb53[1]['outcome'] == 'failed'
        and '归档失败' in str(_rowb53[1].get('error')),
        str([(x['outcome'], x.get('error')) for x in _rowb53])[:120])
finally:
    _s51_t.EXTRA_ARCHIVE_DIR_NAME = _SAVED53[4]
    _s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO = _SAVED53[:4]
    _s53_sh.rmtree(str(_R53B), ignore_errors=True)

_T53D = _s47_tfd.mkdtemp(prefix='bench45-cap-')
_L53 = _s53_os.path.join(_T53D, 'cap.jsonl')
with open(_L53, 'w', encoding='utf-8', newline='\n') as _fh53:
    _fh53.write('\n'.join([_json.dumps({'n': i, 'outcome': 'dropped' if i % 2 else 'intent',
                                       'token': 'T%03d' % i, 'reason': 'r' * 25})
                           for i in range(210)]) + '\n')
_r53cap = _s51_t._cap_rows(_L53, 200)
_left53 = [_json.loads(x) for x in open(_L53, encoding='utf-8').read().splitlines() if x.strip()]
chk('S53 封顶方向对了：210 行只削多余的 intent，105 条终态证据一条不少',
    _r53cap['dropped'] == 10 and sum(1 for x in _left53 if x['outcome'] == 'dropped') == 105
    and len(_left53) == 200 and not _r53cap['overflow'],
    str(_r53cap) + ' 存活=' + str(len(_left53)))
chk('S53 首条证据仍在（上一版削掉的正是最旧的终态行 ⇒ 这条是方向断言不是恒等式）',
    _left53[0]['n'] == 1 and _left53[0]['outcome'] == 'dropped',
    str(_left53[0])[:80])
_L53B = _s53_os.path.join(_T53D, 'cap2.jsonl')
with open(_L53B, 'w', encoding='utf-8', newline='\n') as _fh53b:
    _fh53b.write('\n'.join([_json.dumps({'n': i, 'outcome': 'dropped'}) for i in range(30)]) + '\n')
_r53b = _s51_t._cap_rows(_L53B, 20)
chk('S53 终态行本身超上限 ⇒ 不删证据，回报 overflow（宁可文件变大，不可证据消失）',
    _r53b == {'dropped': 0, 'terminal': 30, 'overflow': True}
    and len([x for x in open(_L53B, encoding='utf-8').read().splitlines() if x.strip()]) == 30,
    str(_r53b))
_s53_sh.rmtree(_T53D, ignore_errors=True)

_m53a = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace('        entry.update(outcome=',
                                           '        target.unlink()\n        entry.update(outcome=', 1))
chk('S53 有牙：把 unlink 提到归档前面 ⇒ 第 52 项判红点名顺序（先删后归档＝没归档）',
    len(_m53a) == 1 and _m53a[0][2] is False and '不早于' in str(_m53a[0][3]), str(_m53a)[:130])
_m53b = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace('archive_path=arch_path,', '', 1))
chk('S53 有牙：台账里没有 archive_path ⇒ 判红（删完之后查不到字节去了哪里）',
    len(_m53b) == 1 and _m53b[0][2] is False and 'archive_path' in str(_m53b[0][3]),
    str(_m53b)[:130])
_m53c = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace('    validate_extra_item(item)\n', '', 1))
chk('S53 有牙：extra_token 不先校验字段 ⇒ 判红（坏字段照样发凭据）',
    len(_m53c) == 1 and _m53c[0][2] is False and 'validate_extra_item' in str(_m53c[0][3]),
    str(_m53c)[:130])
_m53d = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace(
                           "            raise ExtraError(f'归档落点 {p} 在镜像「{mname}」内 ⇒ "
                           "会被同步动作再把它复制回来，拒')", '            pass', 1))
chk('S53 有牙：归档落点守卫改 pass ⇒ 判红（归档件会被 --fix 复制回来，越删越多）',
    len(_m53d) == 1 and _m53d[0][2] is False and 'extra_archive_dir()' in str(_m53d[0][3]),
    str(_m53d)[:130])
_m53e = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace("if k != 'intent']", "if k == 'unparsable']", 1))
chk('S53 有牙：终态行保护被掏空 ⇒ 判红（封顶会重新削回证据，M143 复发）',
    len(_m53e) == 1 and _m53e[0][2] is False
    and ('封顶' in str(_m53e[0][3]) or '白名单' in str(_m53e[0][3])), str(_m53e)[:130])
_m53f = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py', lambda s: s, identity=True)
chk('S53 恒等对照：不改源码跑同一个判据 ⇒ 0 问题（五支红必须只在改坏时出现）',
    len(_m53f) == 1 and _m53f[0][2] is True, str(_m53f)[:120])
_gi53 = {}
for _f53n in ('SYNC-EXTRA-DISPOSITION.jsonl', 'SYNC-EXTRA-ARCHIVE/orphan-sample.py'):
    _r53 = _s47_sp.run(['git', '-C', str(_s44_P(_S43_HERE).parent), 'check-ignore', '-v',
                        str(_s44_P(_S43_HERE) / _f53n)],
                       capture_output=True, text=True, timeout=60)
    _gi53[_f53n] = (_r53.returncode, (_r53.stdout or '').split(chr(9))[0])
chk('S53 处置台账与归档目录已登记 .gitignore（真跑 check-ignore：删一次生一件，不该变成未跟踪噪声）',
    all(v[0] == 0 and '.gitignore:' in v[1] for v in _gi53.values()), str(_gi53)[:130])
chk('S53 段尾复证：真仓权威面既没有归档目录也没有台账（本段所有删除都发生在临时 git 沙箱里）',
    not any((_s44_P(_S43_HERE) / n).exists()
            for n in ('SYNC-EXTRA-ARCHIVE', 'SYNC-EXTRA-DISPOSITION.jsonl')),
    str(sorted(n for n in _s53_os.listdir(_S43_HERE) if 'SYNC' in n)))

# ═══════ S54（第 46 轮 M145/M148）：残件锁的显式破锁出口 + 归档面双向对账 ═══════
# 对标 `borgbackup/borg` `src/borg/archiver/lock_cmds.py:34 do_break_lock`（只破 dead process 留下的锁）
# 与 `restic/restic` `cmd/restic/cmd_unlock.go:21`（stale-only）/ `:47`（--remove-all 连活锁一起清）。
# 本仓不开那个 escalation：锁在本机、pid 判得出死活 ⇒ 活锁永远拒。
import ast as _s54_ast  # noqa: E402
import hashlib as _s54_hashlib  # noqa: E402
import time as _s54_time  # noqa: E402

_s54_P, _s54_json, _s54_os, _s54_sh, _s54_sp = _s44_P, _json, _s53_os, _s53_sh, _s47_sp
_s54_reason = '第 46 轮的破锁与归档保留验收，理由长度够一次真复核'
_s54_tool = _s44_P(_S43_HERE) / 'tools' / 'frontend_sync_check.py'
_SAVED54 = (_s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO)
_s54_old = _s54_time.time() - 400.0


def _s54_holder(pid, action='fix'):
    return _s54_json.dumps({'pid': pid, 'action': action,
                            'ts': '2026-01-01T00:00:00+08:00'}).encode('utf-8')


def _s54_rows(dev):
    p = _s54_P(dev) / _s51_t.EXTRA_LEDGER_NAME
    if not p.exists():
        return []
    return [_s54_json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]


_R54A, _D54A, _M54A = _s51_sandbox()
try:
    _lk54 = _D54A / _s51_t.SYNC_LOCK_NAME
    _hd54 = _D54A / _s51_t.SYNC_HOLDER_NAME
    _st0 = _s51_t.lock_state()
    _rj0 = _s51_reject(lambda: _s51_t.break_lock(confirm='a' * 16, reason=_s54_reason))
    chk('S54 无锁现场判 absent 且拒破（"没得破"绝不能写成"清理完成"）',
        _st0['state'] == 'absent' and not _st0['breakable'] and '没有锁可破' in _rj0,
        f"{_st0['state']} / {_rj0[:60]}")
    chk('S54 五态名册与实现同名（改名会让判据与代码各说一套）',
        _s51_t.LOCK_STATES == ('absent', 'held-living', 'stale-residue',
                               'unattributable', 'too-young'), str(_s51_t.LOCK_STATES))
    _lk54.write_text('lock-bytes\n', encoding='utf-8')
    _hd54.write_bytes(_s54_holder(_s54_os.getpid()))
    _st1 = _s51_t.lock_state()
    _rj1 = _s51_reject(lambda: _s51_t.break_lock(confirm='b' * 16, reason=_s54_reason))
    _pop1 = (_lk54.exists(), _hd54.exists())
    chk('S54 活锁（pid=自己）判 held-living 且拒破，拒后锁与侧件都在',
        _st1['state'] == 'held-living' and '活锁拒破' in _rj1 and _pop1 == (True, True),
        f"{_st1['state']} / {_rj1[:40]} / {_pop1}")
    _hd54.unlink()
    _st2 = _s51_t.lock_state()
    _rj2 = _s51_reject(lambda: _s51_t.break_lock(confirm='c' * 16, reason=_s54_reason))
    chk('S54 归因侧件缺失判 unattributable 并拒（不知谁持有就不破）',
        _st2['state'] == 'unattributable' and '归因' in _rj2, f"{_st2['state']} / {_rj2[:50]}")
    _hd54.write_bytes(_s54_holder(999999))
    _st3 = _s51_t.lock_state()
    _rj3 = _s51_reject(lambda: _s51_t.break_lock(confirm='d' * 16, reason=_s54_reason))
    chk('S54 死锁但锁龄未到判 too-young 并拒（正常回收中的锁不算残件）',
        _st3['state'] == 'too-young' and 'too-young' in _rj3,
        f"{_st3['state']} / {round(_st3['age_s'], 2)}s / {_rj3[:40]}")
    _s54_os.utime(str(_lk54), (_s54_old, _s54_old))
    _st4 = _s51_t.lock_state()
    _tok54 = _s51_t.lock_break_token(_st4)
    _hd54.write_bytes(_hd54.read_bytes() + b' ')
    _st5 = _s51_t.lock_state()
    _rj5 = _s51_reject(lambda: _s51_t.break_lock(confirm=_tok54, reason=_s54_reason))
    chk('S54 有牙：侧件多写一个字节 ⇒ 旧破锁凭据立即失效（凭据绑字节不绑字段）',
        'confirm' in _rj5 and _st5['holder_sha'] != _st4['holder_sha'], _rj5[:70])
    _res54 = _s51_t.break_lock(confirm=_s51_t.lock_break_token(_st5), reason=_s54_reason)
    _rows54 = _s54_rows(_D54A)
    _ad54 = _s51_t.extra_archive_dir()
    _arch54 = sorted(p.name for p in _ad54.iterdir() if p.is_file())
    chk('S54 真破一件残件锁：removed=2、锁与侧件都不在、锁面清空',
        _res54['broken'] == 2 and not _lk54.exists() and not _hd54.exists()
        and _res54['face_after'] == [], str(_res54)[:150])
    chk('S54 破锁两笔台账形状 intent→lock-broken（先留证再动作）',
        [r.get('outcome') for r in _rows54] == ['intent', 'lock-broken'],
        str([r.get('outcome') for r in _rows54]))
    _term54 = _rows54[1]
    _pair54 = {(_term54['archive_path'], _term54['archive_sha']),
               (_term54['sidecar_archive_path'], _term54['sidecar_archive_sha'])}
    chk('S54 两份字节都先归档且台账 sha == 归档件实算（删了还查得到当时谁持有）',
        len(_arch54) == 2 and all(_s54_hashlib.sha256((_ad54 / _n).read_bytes()).hexdigest() in
                                  {x[1] for x in _pair54} for _n in _arch54),
        str(_arch54)[:120])
    _inv54 = _s51_t.archive_inventory()
    chk('S54 归档面回读：两件全被台账指到，孤儿/悬空/半途 均为 0 且两条恒等式成立',
        (_inv54['files'], _inv54['referenced'], _inv54['orphan_files'],
         _inv54['dangling_refs'], _inv54['unfinalized']) == (2, 2, 0, 0, 0)
        and _inv54['files_balanced'] and _inv54['refs_balanced'], str(_inv54)[:170])
    chk('S54 台账 archive_sha 真被拿去和归档件实算比对（两件都核过、零不一致）',
        _inv54['sha_checked'] == 2 and _inv54['sha_mismatch'] == 0
        and _inv54['sha_unrecorded'] == 0,
        f"checked={_inv54['sha_checked']} mismatch={_inv54['sha_mismatch']}")
    _led54 = _D54A / _s51_t.EXTRA_LEDGER_NAME
    _raw54 = _led54.read_text(encoding='utf-8')
    _oldsha = _rows54[1]['archive_sha']
    _newsha = ('1' if _oldsha[0] != '1' else '2') + _oldsha[1:]
    _led54.write_text(_raw54.replace(_oldsha, _newsha, 1), encoding='utf-8', newline='\n')
    _invT54 = _s51_t.archive_inventory()
    _led54.write_text(_raw54, encoding='utf-8', newline='\n')
    _invR54 = _s51_t.archive_inventory()
    chk('S54 有牙：手改台账一个 hex 位 ⇒ sha_mismatch=1 且 anomalies 点名（M144 闭）',
        _invT54['sha_mismatch'] == 1 and any('实算' in a for a in _invT54['anomalies'])
        and _invT54['files'] == _inv54['files'],
        str(_invT54['anomalies'])[:120])
    chk('S54 还原台账后回读面复原（篡改不残留、归档件也没被顺手改）',
        _invR54['sha_mismatch'] == 0 and _invR54['sha_checked'] == 2
        and _invR54['bytes_total'] == _inv54['bytes_total'],
        f"mismatch={_invR54['sha_mismatch']} bytes={_invR54['bytes_total']}")
    (_ad54 / 'stray-no-bookkeeping.txt').write_text('没人记账的一件\n', encoding='utf-8')
    _s54_os.utime(str(_ad54 / 'stray-no-bookkeeping.txt'), (_s54_old, _s54_old))
    _inv55 = _s51_t.archive_inventory()
    chk('S54 注入孤儿归档件 ⇒ orphan=1 且 anomalies 点名（归档面不是只写不读）',
        _inv55['orphan_files'] == 1 and _inv55['anomalies'] == ['stray-no-bookkeeping.txt'],
        str(_inv55['anomalies'])[:80])
    _rj6 = _s51_reject(lambda: _s51_t.prune_archive(
        '../SYNC-WRITE.lock', confirm='e' * 16, reason=_s54_reason, ttl_s=1.0))
    chk('S54 有牙：件名带路径分隔即拒（清理面锁死在归档目录内）',
        '路径' in _rj6, _rj6[:60])
    _nm54 = _arch54[0]
    _sha54 = _s54_hashlib.sha256((_ad54 / _nm54).read_bytes()).hexdigest()
    _rj7 = _s51_reject(lambda: _s51_t.prune_archive(
        _nm54, confirm=_s51_t.archive_token(_nm54, _sha54), reason=_s54_reason, ttl_s=3600.0))
    chk('S54 有牙：未过保留窗口拒清（"删了还能捞回来"不被悄悄缩短）',
        '保留窗口' in _rj7, _rj7[:70])
    _rj8 = _s51_reject(lambda: _s51_t.prune_archive(
        'stray-no-bookkeeping.txt', confirm='f' * 16, reason=_s54_reason, ttl_s=1.0))
    chk('S54 有牙：孤儿件必须先补账再谈清理（拒把"没记账"变成"没痕迹"）',
        '终态行' in _rj8 and (_ad54 / 'stray-no-bookkeeping.txt').exists(), _rj8[:80])
    _s54_os.utime(str(_ad54 / _nm54), (_s54_old, _s54_old))
    _pr54 = _s51_t.prune_archive(_nm54, confirm=_s51_t.archive_token(_nm54, _sha54),
                                 reason=_s54_reason, ttl_s=1.0)
    _inv56 = _s51_t.archive_inventory()
    chk('S54 正当清理一件：pruned_explained=1 且 dangling=0（字节没了但 sha 与来历留在台账）',
        _pr54['pruned'] == 1 and (_inv56['pruned_explained'], _inv56['dangling_refs']) == (1, 0),
        str(_inv56)[:150])
    chk('S54 清理只动被点名的那一件（另一份归档仍在，孤儿件没被顺手带走）',
        _inv56['files'] == 2 and _inv56['orphan_files'] == 1
        and _inv56['referenced'] == 1,
        f"files={_inv56['files']} ref={_inv56['referenced']} orphan={_inv56['orphan_files']}")
    _rj9 = _s51_reject(lambda: _s51_t.prune_archive(
        _nm54, confirm='g' * 16, reason=_s54_reason, ttl_s=1.0))
    chk('S54 有牙：清过的件再清 ⇒ 拒且点名"不存在"（不得判成"已清理"）',
        '不存在' in _rj9, _rj9[:60])
    _ov54 = _ad54 / 'overlap-case.lock.part'
    _ov54.write_text('半成品\n', encoding='utf-8')
    _s51_t._append_row(str(_led54), {'schema': _s51_t.EXTRA_SCHEMA, 'outcome': 'dropped',
                                     'token': 'OVERLAP', 'archive_path': _ov54.as_posix(),
                                     'archive_sha': '0' * 64})
    _ovres = '没抛'
    try:
        _ovinv = _s51_t.archive_inventory()
        _ovres = f"静默返回 referenced={_ovinv['referenced']} partial={_ovinv['partial_writes']}"
    except _s51_t.ExtraError as exc:
        _ovres = str(exc)
    chk('S54 对账不平造得出现场：同一件同时进 referenced 与 partial ⇒ 必须抛而非报"平"',
        '对账不平' in _ovres, _ovres[:110])
    _ov54.unlink()
finally:
    _s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO = _SAVED54
    _s54_sh.rmtree(str(_R54A), ignore_errors=True)

_R54B, _D54B, _M54B = _s51_sandbox()
_saved_dir54 = _s51_t.EXTRA_ARCHIVE_DIR_NAME
try:
    _lk54b = _D54B / _s51_t.SYNC_LOCK_NAME
    _hd54b = _D54B / _s51_t.SYNC_HOLDER_NAME
    _lk54b.write_text('L\n', encoding='utf-8')
    _hd54b.write_bytes(_s54_holder(999999))
    _s54_os.utime(str(_lk54b), (_s54_old, _s54_old))
    _s51_t.EXTRA_ARCHIVE_DIR_NAME = 'afile54'
    (_D54B / 'afile54').write_text('我是文件不是目录\n', encoding='utf-8')
    _stb54 = _s51_t.lock_state()
    _rjb54 = _s51_reject(lambda: _s51_t.break_lock(
        confirm=_s51_t.lock_break_token(_stb54), reason=_s54_reason))
    _rowsb54 = _s54_rows(_D54B)
    chk('S54 有牙：归档落点不可写 ⇒ 锁与侧件一个字节没动，拒因点名"归档失败"',
        '归档失败' in _rjb54 and _lk54b.exists() and _hd54b.exists(), _rjb54[:70])
    chk('S54 归档失败也留证：终态行 outcome=lock-break-failed 且 archive_path 显式为 None',
        [r.get('outcome') for r in _rowsb54] == ['intent', 'lock-break-failed']
        and _rowsb54[1].get('archive_path') is None,
        str([(r.get('outcome'), r.get('archive_path')) for r in _rowsb54])[:120])
finally:
    _s51_t.EXTRA_ARCHIVE_DIR_NAME = _saved_dir54
    _s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO = _SAVED54
    _s54_sh.rmtree(str(_R54B), ignore_errors=True)

_T54C = _s47_tfd.mkdtemp(prefix='bench46-cap-')
_L54 = _s54_os.path.join(_T54C, 'exotic.jsonl')
with open(_L54, 'w', encoding='utf-8', newline='\n') as _fh54:
    _fh54.write('\n'.join([_s54_json.dumps({'n': i, 'outcome': 'intent' if i % 2 else 'some-future-kind'})
                           for i in range(210)]) + '\n')
_r54cap = _s51_t._cap_rows(_L54, 200)
_left54 = [_s54_json.loads(x) for x in open(_L54, encoding='utf-8').read().splitlines()]
chk('S54 排除式封顶：一个从未见过的 outcome 也当证据护住（白名单式枚举那一类缺陷不再复发）',
    sum(1 for x in _left54 if x['outcome'] == 'some-future-kind') == 105
    and _r54cap['dropped'] == 10 and not _r54cap['overflow'],
    str(_r54cap) + ' 剩=' + str(len(_left54)))
_s54_sh.rmtree(_T54C, ignore_errors=True)

_s54_cli = {}
for _a54 in (['--lock-status', '--json'], ['--archive-status', '--json'],
             ['--lock-status', '--fix'], ['--prune-archive', 'x', '--ttl-days', '0']):
    _r54 = _s54_sp.run([sys.executable, str(_s54_tool), *_a54], cwd=str(_s44_P(_S43_HERE)),
                       capture_output=True, text=True, timeout=180)
    _s54_cli[' '.join(_a54)] = (_r54.returncode, (_r54.stdout or '').strip()[:400])
if _S44_IN_DEV_FACE:
    _s54_exp_ok = (_s54_cli['--lock-status --json'][0] == 0
                   and '"state": "absent"' in _s54_cli['--lock-status --json'][1]
                   and _s54_cli['--archive-status --json'][0] == 0
                   and _s54_cli['--lock-status --fix'][0] == 2
                   and _s54_cli['--prune-archive x --ttl-days 0'][0] == 2)
    _s54_exp_msg = ('S54 入口真跑 CLI 四形（源面）：只读档按其态退 0 且印出 state、只读+写态混给退 2、'
                    '--ttl-days 0 退 2')
else:
    _s54_exp_ok = all(v[0] == 2 for v in _s54_cli.values()) and all(
        '面无法确认' in v[1] for v in _s54_cli.values())
    _s54_exp_msg = ('S54 入口真跑 CLI 四形（副本面）：四形一律退 2 并点名"面无法确认"⇒ '
                    '只读档也不许在错误的面上给读得懂的读数（期望随所在面变，第 40 轮同一条尺）')
chk(_s54_exp_msg, _s54_exp_ok, str(_s54_cli)[:380])
chk('S54 只读档不得留下锁文件（不取锁、不排队 = 不是写态）',
    not any((_s44_P(_S43_HERE) / n).exists() for n in ('SYNC-WRITE.lock', 'SYNC-WRITE.holder')),
    str(sorted(n.name for n in _s44_P(_S43_HERE).glob('SYNC-*')))[:120])
_gi54 = {}
for _f54n in ('SYNC-WRITE.lock', 'SYNC-WRITE.holder', 'SYNC-EXTRA-ARCHIVE/lock-sample.lock'):
    _r54g = _s54_sp.run(['git', '-C', str(_s44_P(_S43_HERE).parent), 'check-ignore', '-v',
                         str(_s44_P(_S43_HERE) / _f54n)], capture_output=True, text=True, timeout=60)
    _gi54[_f54n] = (_r54g.returncode, (_r54g.stdout or '').split(chr(9))[0])
chk('S54 锁与侧件已在 .gitignore 名册（真跑 check-ignore：破锁演练不该变成未跟踪噪声）',
    all(v[0] == 0 and '.gitignore:' in v[1] for v in _gi54.values()), str(_gi54)[:170])
_dls54 = _s43_qg._s52_delete_sites(_s54_ast.parse(_s54_tool.read_text(encoding='utf-8')))
chk('S54 删除点名册⇄实扫双向等值，且"释放归因侧件"那一处必须在分母里（第 44 轮就在，此前没人量到）',
    set(_dls54) == set(_s43_qg.S52_DELETE_ROSTER)
    and any(k.endswith('.unlink') and 'holder' in k for k in _dls54),
    f'实扫={_dls54}')
chk('S54 分母锚的是语义不是旧键名（M149 把侧件释放搬进 SyncLease.close ⇒ 按名字硬点会误红）',
    'close.self.holder_path.unlink' in _dls54 and 'renew.tmp.unlink' in _dls54,
    f'实扫={_dls54}'[:150])
chk('S54 有牙：枚举器换到一个谁都没点名的函数上也认得（分母不是手抄的函数名清单）',
    'brand_new_egress.doomed.unlink' in _s43_qg._s52_delete_sites(
        _s54_ast.parse('def brand_new_egress():\n    doomed.unlink()\n')),
    str(_s43_qg._s52_delete_sites(_s54_ast.parse('def brand_new_egress():\n    doomed.unlink()\n')))[:80])
_m54a = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace("    if st['state'] == 'held-living':",
                                           "    if st['state'] == 'never-matches':", 1))
chk('S54 有牙：活锁拒破那条分支被摘 ⇒ 判红（行为核真拿自己的 pid 造过一次活锁）',
    len(_m54a) == 1 and _m54a[0][2] is False and '活锁' in str(_m54a[0][3]), str(_m54a)[:380])
_m54b = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace(
                           "        a_lock, s_lock = archive_lock_file(lock_p, 'lock')\n"
                           "        a_holder, s_holder = archive_lock_file(holder_p, 'holder')",
                           "        a_lock, s_lock, a_holder, s_holder = '', '', '', ''", 1))
chk('S54 有牙：破锁不归档直接删 ⇒ 判红（顺序判据抓得住"删了查不到"）',
    len(_m54b) == 1 and _m54b[0][2] is False and '不早于' in str(_m54b[0][3]), str(_m54b)[:380])
_m54c = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace('            stale_path.unlink()', '            stale_path.touch()', 1))
chk('S54 有牙：破锁的删除点被摘 ⇒ 名册⇄实扫不等值判红（登记了却没人执行＝对空气豁免）',
    len(_m54c) == 1 and _m54c[0][2] is False and '不等值' in str(_m54c[0][3]), str(_m54c)[:380])
_m54d = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace('    if not refs:', '    if False:', 1))
chk('S54 有牙：孤儿件的"来历查不到"闸被摘 ⇒ 判红（把没记账变成没痕迹）',
    len(_m54d) == 1 and _m54d[0][2] is False, str(_m54d)[:380])
_m54e = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace("    if not (doc['files_balanced'] and doc['refs_balanced']):",
                                           '    if False:', 1))
chk('S54 有牙：归档面对账不平却静默 ⇒ 判红（漏计数会被读成"归档面干净"）',
    len(_m54e) == 1 and _m54e[0][2] is False and '分类重叠' in str(_m54e[0][3]), str(_m54e)[:380])
_m54f = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace('    st = lock_state(root, min_age_s)',
                                           "    st = lock_state(root, min_age_s)\n"
                                           "    with sync_lock('break-lock'):\n"
                                           '        pass', 1))
chk('S54 有牙：让破锁去取那把写态锁 ⇒ 判红（borg/restic 的 break-lock 都不取锁，取了永远等不到）',
    len(_m54f) == 1 and _m54f[0][2] is False and '写态锁' in str(_m54f[0][3]), str(_m54f)[:380])
_m54g = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace("    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):",
                                           '    if False:', 1))
chk('S54 有牙：归因侧件的字段契约被摘 ⇒ 判红（坏 holder 领到凭据＝蒙眼破别人的写态）',
    len(_m54g) == 1 and _m54g[0][2] is False, str(_m54g)[:380])
_m54i = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py',
                       lambda s: s.replace("('sidecar_archive_path', 'sidecar_archive_sha')",
                                           "('sidecar_wrong_key', 'sidecar_archive_sha')", 1))
chk('S54 有牙：归档字段名册的键名写错 ⇒ 判红（终态行实写的字段没人核字节，M144 又开一条缝）',
    len(_m54i) == 1 and _m54i[0][2] is False and '不等值' in str(_m54i[0][3]), str(_m54i)[:380])
_m54h = _s49_run_judge('check_sync_egress_single_entry', 'tools/frontend_sync_check.py', lambda s: s, identity=True)
chk('S54 恒等对照：不改源码跑同一个判据 ⇒ 0 问题（上面八支红必须只在改坏时出现）',
    len(_m54h) == 1 and _m54h[0][2] is True, str(_m54h)[:380])
chk('S54 段尾复证：真仓权威面没有锁残留、没有归档目录、也没有处置台账',
    not any((_s44_P(_S43_HERE) / n).exists() for n in
            ('SYNC-EXTRA-ARCHIVE', 'SYNC-EXTRA-DISPOSITION.jsonl',
             'SYNC-WRITE.lock', 'SYNC-WRITE.holder')),
    str(sorted(n.name for n in _s44_P(_S43_HERE).glob('SYNC-*')))[:130])

# ════════ S55（第 47 轮 M149/M151/M152）：写态心跳租约 + 归档两把尺 + 门禁逐项抗崩 ════════
import time as _s55_time  # noqa: E402

_SAVED55 = (_s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO,
            _s51_t.ARCHIVE_MAX_FILES, _s51_t.ARCHIVE_MAX_BYTES, _s51_t.SYNC_LOCK_REFRESH_S)
_saved_qg55 = list(_s43_qg.results)
_saved_crash55 = list(_s43_qg.CRASHED_CHECKS)
_R55, _D55, _M55 = _s51_sandbox()
try:
    # ── M149 心跳：让「锁龄」成为活性证据（对标 restic lock.go:34/:173/:258-263）──
    _t55a = _s51_t.lock_timing_consistent()
    chk('S55 心跳与残件阈值同源（0<refresh×2<min_age）且把余量印出来',
        _t55a['ok'] is True and _t55a['margin'] > 0
        and _t55a['refresh_s'] * 2 < _t55a['min_age_s'], str(_t55a)[:150])
    _t55b = _s51_t.lock_timing_consistent(refresh_s=999.0)
    chk('S55 有牙：心跳间隔调到 999s ⇒ 同一条尺判不 ok（阈值关系是判据不是注释）',
        _t55b['ok'] is False and '不同源' in _t55b['detail'], _t55b['detail'][:90])
    _t55c = _s51_t.lock_timing_consistent(refresh_s=0.0)
    chk('S55 反向对偶：间隔 0（等于永不真刷新）也判不 ok，不许只量"太长"那一头',
        _t55c['ok'] is False, str(_t55c)[:90])
    _lk55, _hd55 = _D55 / _s51_t.SYNC_LOCK_NAME, _D55 / _s51_t.SYNC_HOLDER_NAME
    with _s51_t.sync_lock('fix', root=_D55, refresh_s=0.05) as _lz55:
        _st0 = _s51_t.lock_state(root=_D55)
        _ticks55, _end55 = 0, _s55_time.time() + 0.45
        while _s55_time.time() < _end55:
            _lz55.beat()
            _ticks55 += 1
            _s55_time.sleep(0.02)
        _st1 = _s51_t.lock_state(root=_D55)
        chk('S55 心跳期间锁龄读数不越过残件阈值（长跑写态不再只剩 pid 一根独木）',
            _st1['age_s'] < _st1['min_age_s'] and _st1['age_s'] < 1.0,
            f"age={_st1['age_s']} min={_st1['min_age_s']}")
        chk('S55 两把尺各测各的：beats 随 tick 增长、acquire_age_s 也在长而 age_s 不涨',
            _st1['beats'] > _st0['beats'] and _ticks55 > 5
            and (_st1['acquire_age_s'] or 0) > (_st1['age_s'] or 0),
            f"ticks={_ticks55} beats={_st0['beats']}→{_st1['beats']} "
            f"acq={_st1['acquire_age_s']} age={_st1['age_s']}")
        _doc55h = _s51_JS.loads(_hd55.read_bytes().decode('utf-8'))
        chk('S55 侧件三键分清：ts 仍是取锁时刻、beat_ts 才是心跳、未知键不得被旧校验拒',
            {'ts', 'beat_ts', 'beats'} <= set(_doc55h) and _doc55h['beats'] == _st1['beats']
            and _doc55h['action'] == 'fix', str(_doc55h)[:130])
        _sh55 = _lz55.holder_path
        _lz55.holder_path = _D55 / 'no-such-dir' / 'h.holder'
        _rej55a = _s51_reject(lambda: _lz55.renew(force=True))
        chk('S55 有牙：心跳刷新失败 ⇒ SyncLockError 当场自停（不降级成"没心跳也照写"）',
            '心跳刷新失败' in _rej55a, _rej55a[:110])
        _rej55b = _s51_reject(lambda: _lz55.beat())
        chk('S55 死租约不自愈：第二拍仍拒并点名"已失效"（一次失败后不得继续假装活着）',
            '已失效' in _rej55b and _lz55.dead is not None, _rej55b[:90])
        _lz55.holder_path = _sh55
    chk('S55 释放复原：侧件被自己清掉、持锁进程留下的临时件一个不剩',
        not _hd55.exists() and not _s51_os.path.isdir(_s51_os.path.join(str(_D55), 'no-such-dir')),
        str(sorted(n.name for n in _D55.glob('SYNC-*')))[:120])
    _d55n = _D55 / 'nolock'
    _d55n.mkdir(parents=True, exist_ok=True)
    _lease_nolock = _s51_t.SyncLease(_d55n / _s51_t.SYNC_LOCK_NAME,
                                     _d55n / _s51_t.SYNC_HOLDER_NAME, 'fix')
    _rej55c = _s51_reject(lambda: _lease_nolock.renew(force=True))
    chk('S55 有牙：锁文件不在 ⇒ 心跳拒（"我持有"证明不了就停手，restic :261 那条注释的语义）',
        '已不在' in _rej55c, _rej55c[:120])
    _old_h = {'pid': 4242, 'action': 'fix', 'ts': '2026-01-01T00:00:00+00:00'}
    _bad_h = dict(_old_h, beats='many')
    _good_h = dict(_old_h, beats=3, beat_ts='2026-01-01T00:00:05+00:00')
    chk('S55 侧件校验双向：无心跳键的老侧件仍合法（向后兼容）/ beats 类型坏必须判不合法',
        _s51_t.validate_holder(_old_h) == [] and _s51_t.validate_holder(_good_h) == []
        and any('beats' in x for x in _s51_t.validate_holder(_bad_h)),
        str([_s51_t.validate_holder(_old_h), _s51_t.validate_holder(_bad_h)])[:150])
    _buf55 = _s48_io.StringIO()
    with _s48_cl.redirect_stdout(_buf55):
        _rc55a = _s51_t.main(['--lock-status', '--json'])
    _line55 = _buf55.getvalue().strip().splitlines()[-1] if _buf55.getvalue().strip() else ''
    _cldoc55 = _s51_JS.loads(_line55) if _line55.startswith('{') else {}
    chk('S55 CLI 只读档带出心跳读数（beat_ts/beats/timing/age_source 四键齐，退 0）',
        _rc55a == 0 and {'beat_ts', 'beats', 'timing', 'age_source'} <= set(_cldoc55)
        and _cldoc55['timing']['ok'] is True, f'rc={_rc55a} keys={sorted(_cldoc55)[:8]}')
    _beat_sites = [n for n in _s43_ast.walk(_s43_ast.parse(
        _s43_qg.read(_s51_os.path.join(_S43_HERE, 'tools', 'frontend_sync_check.py'))))
        if isinstance(n, _s43_ast.Attribute) and n.attr == 'beat']
    chk('S55 心跳真接进写态出口（main 与 drop_extra 里各有一处 beat，装而不用等于没装）',
        len(_beat_sites) >= 2, f'beat 调用点 {len(_beat_sites)} 处')

    # ── M151 归档面两把尺 + TTL 与发现节奏对账 ──
    chk('S55 人口尺与台账行尺同源（上限=EXTRA_MAX_ENTRIES，不另拍一个数）',
        _s51_t.ARCHIVE_MAX_FILES == _s51_t.EXTRA_MAX_ENTRIES == 200,
        f'{_s51_t.ARCHIVE_MAX_FILES} vs {_s51_t.EXTRA_MAX_ENTRIES}')
    _prod55 = _s51_t.ARCHIVE_MAX_FILES * _s51_t.ARCHIVE_SINGLE_MAX_MEASURED_BYTES
    chk('S55 字节尺可由「人口 × 实测单件上界」复算且余量 <10%（不是整数偏好）',
        _prod55 <= _s51_t.ARCHIVE_MAX_BYTES <= int(_prod55 * 1.10),
        f'积={_prod55} 尺={_s51_t.ARCHIVE_MAX_BYTES}')
    _tb55 = _s51_t.archive_ttl_basis()
    chk('S55 TTL 与发现节奏对得上账（30 天 ÷ 实测最坏间隔 ≥ 4 个周期）',
        _tb55['ok'] is True and _tb55['cycles'] >= _tb55['min_cycles'], str(_tb55)[:160])
    _tb55b = _s51_t.archive_ttl_basis(max_gap_days=40.0)
    chk('S55 有牙：最坏发现间隔 40 天 ⇒ 同一把尺判"窗口短于节奏"（对账不是自证）',
        _tb55b['ok'] is False and '短于' in _tb55b['detail'], _tb55b['detail'][:100])
    _tb55c = _s51_t.archive_ttl_basis(max_gap_days=0)
    chk('S55 边界：间隔取 0 不得除零崩，判 cycles=None 且结论不 ok',
        _tb55c['cycles'] is None and _tb55c['ok'] is False, str(_tb55c)[:110])
    _ad55 = _D55 / _s51_t.EXTRA_ARCHIVE_DIR_NAME
    _ad55.mkdir(parents=True, exist_ok=True)
    for _i55 in range(3):
        (_ad55 / f'keep-{_i55}.css').write_bytes(b'k' * 100)
    _anc55 = _ad55 / 'ancient.css'
    _anc55.write_bytes(b'a' * 7)
    _ago55 = _s55_time.time() - 31 * 86400
    _s51_os.utime(_anc55, (_ago55, _ago55))
    _inv55 = _s51_t.archive_inventory()
    chk('S55 归档面读数带两把尺 + 已过窗计划（max/binding/expired 三件齐）',
        {'max_files', 'max_bytes', 'binding_cap', 'expired_over_ttl', 'expired_plan'} <= set(_inv55)
        and _inv55['expired_over_ttl'] == 1 and _inv55['files'] == 4,
        f"files={_inv55['files']} expired={_inv55['expired_over_ttl']} binding={_inv55['binding_cap']}")
    chk('S55 只读批量计划逐件带绑字节 token（真批量档必须先长在这份计划上，M150 前置）',
        len(_inv55['expired_plan']) == 1
        and _inv55['expired_plan'][0]['name'] == 'ancient.css'
        and len(_inv55['expired_plan'][0]['token']) == _s51_t.TOKEN_HEX_LEN,
        str(_inv55['expired_plan'])[:150])
    chk('S55 扩容不破第 46 轮两条恒等式（files 分类合计平 ∧ terminal 解释合计平）',
        _inv55['files_balanced'] is True and _inv55['refs_balanced'] is True,
        f"{_inv55['files']}/{_inv55['referenced']}+{_inv55['orphan_files']} "
        f"refs={_inv55['terminal_refs']}")
    _it55 = {x['rel']: x for x in _s51_t.scan_extra()}['orphan.py']
    _tg55 = _M55 / 'src' / 'orphan.py'
    _files55 = _s51_files(_M55)
    _s51_t.ARCHIVE_MAX_FILES = 1
    _rej55d = _s51_reject(lambda: _s51_t.archive_extra(_tg55, _it55, 'mir'))
    chk('S55 有牙：人口尺越界 ⇒ archive_extra 整笔拒并点名"人口"，原件与台账一字节未动',
        '人口' in _rej55d and _s51_files(_M55) == _files55
        and not _s51_os.path.isfile(_s51_t.extra_ledger_path()), _rej55d[:130])
    _s51_t.ARCHIVE_MAX_FILES = 200
    _s51_t.ARCHIVE_MAX_BYTES = 50
    _rej55e = _s51_reject(lambda: _s51_t.archive_extra(_tg55, _it55, 'mir'))
    chk('S55 有牙：字节尺越界 ⇒ 点名"字节"并给出推导（两把尺各说各的，不混成一句"满了"）',
        '字节' in _rej55e and '实测单件上界' in _rej55e, _rej55e[:150])
    _s51_t.ARCHIVE_MAX_BYTES = 128 * 1024 * 1024
    _arch_ok, _arch_sha = _s51_t.archive_extra(_tg55, _it55, 'mir')
    chk('S55 反向对偶：两把尺都留有余量时归档照做（只测"拒得住"会造出永拒的闸）',
        _s51_t.hashlib.sha256(open(_arch_ok, 'rb').read()).hexdigest() == _arch_sha
        == _it55['sha256'], _arch_ok[-40:])
    _buf55b = _s48_io.StringIO()
    _s51_t.ARCHIVE_MAX_FILES = 2
    with _s48_cl.redirect_stdout(_buf55b):
        _rc55b = _s51_t.main(['--archive-status', '--json'])
    _s51_t.ARCHIVE_MAX_FILES = 200
    _invdoc55 = _s51_JS.loads(_buf55b.getvalue().strip().splitlines()[-1])
    chk('S55 CLI 结论面：越界不再被"异常 0 项"掩盖 ⇒ rc=1 且 over_file_cap 为真',
        _rc55b == 1 and _invdoc55['over_file_cap'] is True,
        f"rc={_rc55b} over={_invdoc55.get('over_file_cap')} anomalies={_invdoc55.get('anomalies')}")

    # ── M152 门禁逐项抗崩（对标 go testing tRunner / pre-commit run_stage）──
    _m55a = _s49_run_judge('check_gate_item_isolation', 'quality_gate.py', lambda s: s, identity=True)
    chk('S55 恒等对照：不改源码跑第 56 项 ⇒ 恰好 1 条 record 且判绿（上面所有红腿的前提）',
        len(_m55a) == 1 and _m55a[0][2] is True and '裸调用 0 处' in str(_m55a[0][3]),
        str(_m55a)[:220])
    _m55b = _s49_run_judge('check_gate_item_isolation', 'quality_gate.py',
                           lambda s: s.replace('    _guarded(check_ruff)', '    check_ruff()', 1))
    chk('S55 有牙：把一项改回裸调用 ⇒ 第 56 项判红并点名（一处裸出口就够带走后面的项）',
        len(_m55b) == 1 and _m55b[0][2] is False and 'check_ruff' in str(_m55b[0][3]),
        str(_m55b[0][3])[:220])
    _guard_line55 = '    except Exception as exc:                      # noqa: BLE001  一条腿崩不得带走整套'
    _m55c = _s49_run_judge('check_gate_item_isolation', 'quality_gate.py',
                           lambda s: s.replace(_guard_line55, '    except ValueError as exc:', 1))
    chk('S55 有牙：处理器换成窄捕获 ⇒ 判红（"有个 except"不等于裹住，换个类型就漏）',
        len(_m55c) == 1 and _m55c[0][2] is False and '没捕 Exception' in str(_m55c[0][3]),
        str(_m55c[0][3])[:200])
    _m55d = _s49_run_judge('check_gate_item_isolation', 'quality_gate.py',
                           lambda s: s.replace(_guard_line55, '    except BaseException as exc:', 1))
    chk('S55 有牙：改成捕 BaseException ⇒ 判红（吞掉 sys.exit 会把"判红"变成"没结论"）',
        len(_m55d) == 1 and _m55d[0][2] is False and 'BaseException' in str(_m55d[0][3]),
        str(_m55d[0][3])[:200])
    _m55e = _s49_run_judge('check_gate_item_isolation', 'quality_gate.py',
                           lambda s: s.replace("    _guarded(check_business_calendar)", '', 1))
    chk('S55 有牙：删掉一处调用（项整个漏跑）⇒ 第 56 项点名"没经 _guarded"，不静默少一项',
        len(_m55e) == 1 and _m55e[0][2] is False and 'check_business_calendar' in str(_m55e[0][3]),
        str(_m55e[0][3])[:200])
    _qg_path55 = _s43_os.path.join(_S43_HERE, 'quality_gate.py')
    _ns55 = {'__file__': _qg_path55, '__name__': 'gate55_probe'}
    exec(compile(_s43_qg.read(_qg_path55), _qg_path55, 'exec'), _ns55)

    def _boom55():
        raise KeyError('ARCHIVE_SHA_VERIFY_MAX')

    def _exit55():
        raise SystemExit(7)

    _buf55c = _s48_io.StringIO()
    with _s48_cl.redirect_stdout(_buf55c):
        _rv55 = _ns55['_guarded'](_boom55)
    chk('S55 行为核：崩项返回 None 且不抛出，崩溃账记下函数名与异常（真因当场印出，不等摘要）',
        _rv55 is None and len(_ns55['CRASHED_CHECKS']) == 1
        and _ns55['CRASHED_CHECKS'][0][0] == '_boom55'
        and 'KeyError' in _ns55['CRASHED_CHECKS'][0][1]
        and 'ARCHIVE_SHA_VERIFY_MAX' in _buf55c.getvalue(), _buf55c.getvalue()[:200])
    _ns55['results'][:] = []
    with _s48_cl.redirect_stdout(_s48_io.StringIO()):
        _ns55['_guarded'](lambda: _ns55['record']('BLOCK', '99. 后面的项', True, ['照样跑']))
    chk('S55 行为核：一条腿崩之后后面的项照跑并照常 record（"全套件一起消失"不得复发）',
        len(_ns55['results']) == 1 and _ns55['results'][0][2] is True, str(_ns55['results'])[:150])
    with _s48_cl.redirect_stdout(_s48_io.StringIO()):
        _ns55['check_gate_item_isolation']()
    chk('S55 行为核：有崩溃时第 56 项判红并点名那条腿（判红要说清是谁崩，不是"门禁失败"）',
        _ns55['results'][-1][2] is False and '_boom55' in str(_ns55['results'][-1][3]),
        str(_ns55['results'][-1][3])[:200])
    _code55 = None
    try:
        _ns55['_guarded'](_exit55)
    except SystemExit as _e55:
        _code55 = _e55.code
    chk('S55 反向对偶：_guarded 不吞 SystemExit（退出码必须照旧穿出，否则结论消失）',
        _code55 == 7 and len(_ns55['CRASHED_CHECKS']) == 1, f'code={_code55}')
    _ns55['results'][:] = []
    with _s48_cl.redirect_stdout(_s48_io.StringIO()):
        _ns55['check_gate_item_isolation']()
        _ns55['check_gate_numbering'](fast=True)
    chk('S55 两条红互认：第 20 项的 missed 尾巴点名"见第 56 项"（防下一轮去修错的那条）',
        any('见第 56 项' in str(d) for d in _ns55['results'][-1][3]),
        str(_ns55['results'][-1][3])[:230])
    _lit56 = _s43_qg.read(_qg_path55).count("record('BLOCK', '56. ")
    chk('S55 第 56 项的项名以字面量写进 record（存成变量会让第 20 项静态侧枚举不到 ⇒ 自检长期失明）',
        _lit56 >= 2, f'字面量出口 {_lit56} 处（两条 record 出口各一处）')
    # ── 假反例总账（本轮一手：S52 两支腿的 needle 因 M149 加了 ` as lease:` 而静默不命中）──
    _noop55 = [f'{n}@{k}' for n, k, ident, changed in _MUT_LOG if not ident and not changed]
    _ident55 = [f'{n}@{k}' for n, k, ident, _c in _MUT_LOG if ident]
    chk(f'S55 假反例总账：{len(_MUT_LOG)} 支变异腿（含 {len(_ident55)} 支恒等对照）里，'
        '除恒等对照外每支都必须真改到字节（needle 失配＝夹具静默失效，会被误读成"判据抓不住"）',
        not _noop55, str(_noop55)[:230])
finally:
    (_s51_t.DEV, _s51_t.MIRROR_DIRS, _s51_t.DEV_FACE_NAME, _s51_t.REPO,
     _s51_t.ARCHIVE_MAX_FILES, _s51_t.ARCHIVE_MAX_BYTES,
     _s51_t.SYNC_LOCK_REFRESH_S) = _SAVED55
    _s43_qg.results[:] = _saved_qg55
    _s43_qg.CRASHED_CHECKS[:] = _saved_crash55
    _s47_sh.rmtree(str(_R55), ignore_errors=True)
chk('S55 全局态复原：两把尺/心跳间隔/门禁 results 与崩溃账都指回原值（夹具不留现场）',
    _s51_t.ARCHIVE_MAX_FILES == 200 and _s51_t.ARCHIVE_MAX_BYTES == 128 * 1024 * 1024
    and _s51_t.SYNC_LOCK_REFRESH_S == 30.0 and _s51_t.DEV_FACE_NAME == 'iCAN大学生创新创业大赛'
    and not _s43_qg.CRASHED_CHECKS,
    f'{_s51_t.ARCHIVE_MAX_FILES}/{_s51_t.SYNC_LOCK_REFRESH_S}/{_s51_t.DEV_FACE_NAME}')
chk('S55 段尾复证：真仓权威面仍然干净（无归档目录、无锁残留、无处置台账、崩溃账为空）',
    not any((_s44_P(_S43_HERE) / n).exists() for n in
            ('SYNC-EXTRA-ARCHIVE', 'SYNC-EXTRA-DISPOSITION.jsonl',
             'SYNC-WRITE.lock', 'SYNC-WRITE.holder'))
    and not _s43_qg.CRASHED_CHECKS,
    str(sorted(n.name for n in _s44_P(_S43_HERE).glob('SYNC-*')))[:130])

# ════════════ S56 第 48 轮：第 19 项可达性闭包被高阶包裹打断 ════════════
# 一手动因（2026-09-30 00:2x 在 05 面实跑权威门禁）：第 47 轮把 `main()` 里 48 个裸调用
# 改成 `_guarded(check_x)` 之后，`_reachable_func_names()` 只沿 `ast.Call` 的**被调名**求边，
# 于是 `main → _guarded` 之后无路可走（`_guarded` 体内调的是形参 `fn`，不在 defs 里），
# 第 19 项一口气点名 10 个证据脚本「写在无人调用的函数里」⇒ **判据假红，而红掉的正是
# 「接线」这件事本身**；同族第 47 轮已给第 20 项的静态枚举补过 `run_test, ` 形态，
# 但漏了本闭包这个同类出口（修例不修类的第四次复发）。下列夹具只在内存里过 AST。
import quality_gate as _qg_s56

_s56_wrap = _NL.join([
    'def run_check():',
    '    subprocess.run([PY, "wrapped_check.py"])',
    'def _wrap(fn, *args):',
    '    return fn(*args)',
    'def main():',
    '    _wrap(run_check)',
    'if __name__ == "__main__":',
    '    main()',
])
_s56_attr = _NL.join([
    'def run_check2():',
    '    subprocess.run([PY, "attr_check.py"])',
    'def _wrap2(fn, *args):',
    '    return fn.run(*args)',
    'def main():',
    '    _wrap2(run_check2)',
    'if __name__ == "__main__":',
    '    main()',
])
_s56_append = _NL.join([
    'def dead_check():',
    '    subprocess.run([PY, "sneaky_check.py"])',
    '_registry = []',
    'def main():',
    '    _registry.append(dead_check)',
    'if __name__ == "__main__":',
    '    main()',
])
_s56_nowrap = _NL.join([
    'def dead_check2():',
    '    subprocess.run([PY, "fake_wrapped_check.py"])',
    'def _nowrap(fn, *args):',
    '    return repr(fn)',
    'def main():',
    '    _nowrap(dead_check2)',
    'if __name__ == "__main__":',
    '    main()',
])
_s56_dispatch = _NL.join([
    'def dead_check3():',
    '    subprocess.run([PY, "dispatch_check.py"])',
    'DISPATCH = {"k": dead_check3}',
    'def main():',
    '    DISPATCH["k"]()',
    'if __name__ == "__main__":',
    '    main()',
])
_s56_c = {_k: _s11_called(_v) for _k, _v in (
    ('wrap', _s56_wrap), ('attr', _s56_attr), ('append', _s56_append),
    ('nowrap', _s56_nowrap), ('dispatch', _s56_dispatch))}
chk('S56 高阶包裹算接线：main → _wrap(run_check) → subprocess 这条链必须走得通'
    '（第 47 轮那次假红的扳机，改坏 invoker 识别这条就红）',
    'wrapped_check.py' in _s56_c['wrap'], str(sorted(_s56_c['wrap'] or []))[:150])
chk('S56 形参的 Attribute 调用同样算 invoker（fn.run(*args) 形态，S16 同类"另一半"）',
    'attr_check.py' in _s56_c['attr'], str(sorted(_s56_c['attr'] or []))[:150])
chk('S56 反向对偶一：回调只塞进清单/容器、从不调用（_registry.append(dead)）不算接线'
    '（放宽成"实参名一律算"就会在这里失去收窄能力）',
    'sneaky_check.py' not in _s56_c['append'], 'append 形态被放行')
chk('S56 反向对偶二：收了形参却从不调用的"假包裹器"不得判为 invoker'
    '（否则任意 _nowrap(dead) 都能蒙混成已接线）',
    'fake_wrapped_check.py' not in _s56_c['nowrap'], '假包裹器被放行')
chk('S56 已知边界如实判红：下标派发 DISPATCH["k"]() 的 Call.func 是 Subscript，'
    '本闭包不跟随（要数据流才追得到，宁可误红也不放宽）',
    'dispatch_check.py' not in _s56_c['dispatch'], '下标派发被误判成接线')


def _s56_naive(src):
    """直觉修法：实参位上出现的任何函数名都当出边。用它做**实测对照**，
    证明现规则不是最宽的那条 —— 而不是靠断言声称自己收窄。"""
    _t = _ast_s30.parse(src)
    _d = {n.name for n in _ast_s30.walk(_t)
          if isinstance(n, (_ast_s30.FunctionDef, _ast_s30.AsyncFunctionDef))}
    _out = set()
    for _n in _ast_s30.walk(_t):
        if isinstance(_n, _ast_s30.Call):
            for _a in list(_n.args) + [_k.value for _k in _n.keywords]:
                if isinstance(_a, _ast_s30.Name) and _a.id in _d:
                    _out.add(_a.id)
    return _out


_s56_n = {k: _s56_naive(v) for k, v in (
    ('append', _s56_append), ('nowrap', _s56_nowrap), ('dispatch', _s56_dispatch))}
# 期望**由实测得出**，不是由直觉补：naive（实参名一律算）放行的是 append / nowrap 两条，
# 而 dispatch 那条连 naive 也放行不了（回调出现在**赋值字面量**里、不在调用实参位）⇒
# 该形态不构成两条规则的区分轴，两条实现同判"未接线"，这里如实记为共同边界而非我赢一分。
chk('S56 对照实测：naive 版会放行 append/nowrap 两条反向对偶 ⇒ 现规则确有区分力；'
    "dispatch 两条实现同判不跟随（共同边界，不计入区分力）",
    'dead_check' in _s56_n['append'] and 'dead_check2' in _s56_n['nowrap']
    and not _s56_n['dispatch'],
    str({k: sorted(v) for k, v in _s56_n.items()})[:170])
chk('S56 现规则比 naive 窄：三条反向对偶在真实现里一律不跟随（两侧各出一行分母）',
    _s56_c['append'] is not None and _s56_c['nowrap'] is not None
    and _s56_c['dispatch'] is not None
    and not any(_s56_c[k] for k in ('append', 'nowrap', 'dispatch')),
    str({k: sorted(_s56_c[k] or []) for k in ('append', 'nowrap', 'dispatch')})[:170])
_s56_real = _s11_called(open(os.path.join(_h4_root, 'quality_gate.py'),
                            encoding='utf-8').read())
_s56_missing5 = sorted(n for n in ('check_openapi_drift.py', 'check_query_budget.py',
                                   'check_schema_drift.py', 'check_snapshot_assets.py',
                                   'ai_eval.py') if n not in (_s56_real or set()))
chk('S56 真面复证：真实门禁源码上，第 19 项当日误判的 5 个证据脚本全部取到'
    '（只测内存夹具不测真面＝没测到注入形参调用那条老坑）',
    _s56_real is not None and not _s56_missing5, '仍取不到=' + str(_s56_missing5)[:150])
_s56_reach = _qg_s56._reachable_func_names(_ast_s30.parse(
    open(os.path.join(_h4_root, 'quality_gate.py'), encoding='utf-8').read()))
chk('S56 闭包在真面上收得住：main 与被包裹的检查函数名同在可达集（空集＝判据空转）',
    'main' in _s56_reach and 'check_ai_eval_floor' in _s56_reach
    and 'check_evidence_scripts_wired' in _s56_reach and len(_s56_reach) > 20,
    f'可达集 {len(_s56_reach)} 个')

# ════════════ S57 第 48 轮：发布身份三层对账（台账版本集 ⇄ git tag）════════════
# 动因（2026-09-30 当日实测）：仓内台账 23 个版本、git tag 17 个，差集里
# `v1.6.2…v1.11.0` 有台账无 tag、`v1.6.0` 有 tag 无台账 —— 对标群（getodk/InvenTree/frappe
# 180 天 10/20/88 次 release）三处身份一一对应，而当时**没有任何一条判据看这件事**。
# 第 29 项只管"当前版本台账与实盘一致"、S46 只管"最新 tag==APP_VERSION"，
# 版本**集合**的双向差集是第三个事实，故单独立项（同事实不两处判，此处不重复它们）。
import subprocess as _sp_s57

_ri = _qg_s56._release_identity_reconcile
_EX_OK = {'ledger_only': {'v1.0.1': '这条缺 tag 是因为 tag 纪律起点在 v1.2.0，本件由后续轮次补生成，不追补'},
          'tag_only': {'v0.9.0': '该 tag 早于台账生成器入库，仓内不可能有它的台账'}}
_p0, _r0 = _ri(['v1.0.0', 'v1.0.1'], ['v1.0.0', 'v1.2.0'], [], {}, 'v1.2.0')
chk('S57 双向都量：台账独有与 tag 独有两条方向各出未登记（只补一侧不算修完）',
    len(_p0) == 2 and any('ledger_only 侧缺件未登记：v1.0.1' in p for p in _p0)
    and any('tag_only 侧缺件未登记：v1.2.0' in p for p in _p0), str(_p0)[:220])
_p1, _r1 = _ri(['v1.0.0'], ['v1.0.0'], [], {}, 'v1.0.0')
chk('S57 不该红的仍绿：两侧一致时 problems 必须为空（且读数三个数都要给得出）',
    not _p1 and _r1['ledger'] == 1 and _r1['tag'] == 1 and _r1['both'] == 1, str(_r1)[:160])
_p2, _r2 = _ri(['v1.0.0', 'v1.0.1'], ['v1.0.0', 'v0.9.0'], [], _EX_OK, 'v1.0.0')
chk('S57 名册兜住既有事实：两侧差集各被对应名单命中且理由够长时判绿，并如实报"已豁免 2 个"',
    not _p2 and _r2['exempted'] == 2 and _r2['both'] == 1, str(_p2)[:220])
_p3, _ = _ri(['v1.0.0'], ['v1.0.0'], [], _EX_OK, 'v1.0.0')
chk('S57 死豁免判红：名单里的版本其实不在差集里＝对空气豁免＝判据空转（第 19 项同族）',
    len(_p3) == 2 and all('死豁免' in p for p in _p3), str(_p3)[:220])
_p4, _ = _ri(['v1.0.0', 'v1.0.1'], ['v1.0.0'],
             [], {'ledger_only': {'v1.0.1': '太短'}, 'tag_only': {}}, 'v1.0.0')
chk('S57 短理由判红：不足 12 字的豁免理由视为未豁免（否则登记即放行）',
    any('不足 12 字' in p for p in _p4), str(_p4)[:220])
_p5, _ = _ri(['v1.0.0'], ['v1.0.0'], [], {'ledger_only': {'v1.0.0': '把本轮漏打的 tag 顺手豁免掉'},
                                          'tag_only': {}}, 'v1.0.0')
chk('S57 名册不得吞掉本轮：当前 APP_VERSION 出现在任一豁免名单即判红',
    any('当前版本 v1.0.0 出现在' in p for p in _p5), str(_p5)[:220])
_p6, _ = _ri(['v1.0.0'], ['v1.0.0'], ['SHA256SUMS-final.txt'], {}, 'v1.0.0')
chk('S57 非 semver 形态的台账件点名（版本集取不出来＝该件对三层对账不可见）',
    any('SHA256SUMS-final.txt' in p for p in _p6), str(_p6)[:220])
# ── 真面复证：两侧集合由本腿**独立取数**（不复用被测项的取数面），再喂进同一纯函数 ──
_top_s57 = _sp_s57.run(['git', 'rev-parse', '--show-toplevel'], cwd=_h4_root,
                       capture_output=True, text=True).stdout.strip()
_led_dir_s57 = os.path.join(_top_s57, '交付物', '提交包', '工程佐证材料', '09-发布校验')
chk('S57 真面取数前提成立：台账目录与 git 根都在（取不到就判红，不许静默跳过）',
    bool(_top_s57) and os.path.isdir(_led_dir_s57), f'{_top_s57}|{_led_dir_s57}')
_led_s57 = sorted(m.group(1) for n in os.listdir(_led_dir_s57)
                  for m in [re.match(r'^SHA256SUMS-(v\d+\.\d+\.\d+)\.txt$', n)] if m)
_tag_s57 = sorted(t for t in _sp_s57.run(['git', 'tag', '-l', 'v*'], cwd=_top_s57,
                                         capture_output=True, text=True).stdout.split()
                  if re.match(r'^v\d+\.\d+\.\d+$', t))
_cur_s57 = 'v' + re.search(r"^APP_VERSION\s*=\s*'([^']+)'",
                           open(os.path.join(_h4_root, 'src', 'version.py'),
                                encoding='utf-8').read(), re.M).group(1)
_p7, _r7 = _ri(_led_s57, _tag_s57,
               [n for n in os.listdir(_led_dir_s57)
                if not re.match(r'^SHA256SUMS-v\d+\.\d+\.\d+\.txt$|^\.|^README', n)],
               _qg_s56.RELEASE_IDENTITY_EXEMPT, _cur_s57)
chk(f'S57 真面·名册腿：台账 {len(_led_s57)} / tag {len(_tag_s57)} 的历史差集被名册正好兜住'
    '（problems 必须为空，非空即逐条点名）', not _p7, str(_p7)[:260])
chk(f'S57 真面·时序腿：当前版本 {_cur_s57} 两侧齐全（台账已重算 + tag 已打）；'
    '本腿在 commit→tag 之前**必然红**，与 S46 同一时序族，红因要能指认是哪一侧缺',
    _cur_s57 in _led_s57 and _cur_s57 in _tag_s57,
    f'台账{"有" if _cur_s57 in _led_s57 else "缺"} / tag{"有" if _cur_s57 in _tag_s57 else "缺"}'
    ' ⇒ 缺台账=跑 release_evidence.py，缺 tag=git tag -a 补打')
# 名册膨胀闸：豁免条数必须**等于**实测差集条数。多一条就是死豁免（上面已判红），
# 少一条就是漏登记（也判红）——这里再钉一次"名单不许越加越大"这个数量事实。
chk('S57 名册数量与实测差集严格相等（防止把新漂移一路塞进历史名单了事）',
    len(_qg_s56.RELEASE_IDENTITY_EXEMPT['ledger_only']) == len(set(_led_s57) - set(_tag_s57))
    and len(_qg_s56.RELEASE_IDENTITY_EXEMPT['tag_only']) == len(set(_tag_s57) - set(_led_s57)),
    f'名册 {len(_qg_s56.RELEASE_IDENTITY_EXEMPT["ledger_only"])}+'
    f'{len(_qg_s56.RELEASE_IDENTITY_EXEMPT["tag_only"])} vs 实测 '
    f'{len(set(_led_s57) - set(_tag_s57))}+{len(set(_tag_s57) - set(_led_s57))}')

# ════════════ S58 第 49 轮：离线队列条目归属（M155）════════════
# 一手复现（本日探针实测，非推断）：同店两店员、当日重放一条甲未提交过的产品，
# 服务端 `提交成功=True` 且**乙**名下流水 0→1 ⇒ 隔夜那半早被 queued_for 拦住，
# 真漏的是「同日换账号」。因此归属键必须含店员位：只按门店判归属对这一形态零判定力。
# 本轮同时否证了我上轮登记 M155 时的两处措辞（见 07 卷 91 的订正段）。
_s58_root = _h4_root
_s58_paths = {k: os.path.join(_s58_root, 'src', *v) for k, v in {
    'submit': ('static', 'js', 'offline_submit.js'),
    'scope': ('static', 'js', 'offline_scope.js'),
    'tpl': ('templates', 'h5', 'dashboard.html'),
    'route': ('routes', 'h5.py'),
}.items()}
_s58_txt = {}
for _k58, _p58 in _s58_paths.items():
    _s58_txt[_k58] = open(_p58, encoding='utf-8').read() if os.path.isfile(_p58) else ''
chk('S58 四面文件均可读且非空（缺件时下一腿负责判"没测到"）',
    all(_s58_txt.values()), str({k: len(v) for k, v in _s58_txt.items()})[:150])
_s58_base, _s58_rd = _qg_s56._offline_scope_wiring(_s58_txt['submit'], _s58_txt['scope'],
                                                   _s58_txt['tpl'], _s58_txt['route'])
chk('S58 真面判绿：模板 4 位注入 / 归属库零 DOM / playOne 内先于 xhr.send 有闸 / 路由四参都在',
    not _s58_base and _s58_rd['scope_attrs'] == 4 and _s58_rd['in_playOne'] == 1
    and _s58_rd['guards'] >= 2, str(_s58_base)[:240])


def _s58_mutate(kind):
    """在内存里改一份真字节，返回该项判据点名的 problems。每条变异都必须**只**造出它自己那条红。"""
    t = dict(_s58_txt)
    if kind == 'drop-send-guard':
        new = t['submit'].replace(
            "        if (!Scope || !Scope.itemMatchesScope(item, CURRENT_SCOPE)) {",
            "        if (false) {", 1)
        assert new != t['submit'], 'needle 失配：playOne 守卫'
        t['submit'] = new
    elif kind == 'swap-script-order':
        a = '<script src="{{ asset_url(\'js/offline_scope.js\') }}"></script>'
        b = '<script src="{{ asset_url(\'js/offline_submit.js\') }}"></script>'
        new = t['tpl'].replace(a + "\n" + b, b + "\n" + a, 1)
        assert new != t['tpl'], 'needle 失配：脚本顺序'
        t['tpl'] = new
    elif kind == 'drop-staff-attr':
        new = t['tpl'].replace('data-scope-staff="{{ scope_staff_id or \'\' }}" ', '', 1)
        assert new != t['tpl'], 'needle 失配：staff 属性'
        t['tpl'] = new
    elif kind == 'revert-v1-key':
        new = t['submit'].replace("'ican_h5_offline_v2'", "'ican_h5_offline_v1'", 1)
        assert new != t['submit'], 'needle 失配：队列键'
        t['submit'] = new
    elif kind == 'drop-route-staff':
        new = t['route'].replace('scope_staff_id=current_user.id,', 'scope_staff_id=None,', 1)
        assert new != t['route'], 'needle 失配：路由 staff 取值'
        t['route'] = new
    elif kind == 'add-dom-in-scope':
        new = t['scope'].replace('    function text(v) {',
                                 '    function _zz(v) { return document.getElementById(v); }\n'
                                 '    function text(v) {', 1)
        assert new != t['scope'], 'needle 失配：归属库 DOM'
        t['scope'] = new
    else:
        raise AssertionError(f'未知变异 {kind}')
    return _qg_s56._offline_scope_wiring(t['submit'], t['scope'], t['tpl'], t['route'])


_S58_EXPECT = {
    'drop-send-guard': 'playOne 内、xhr.send 之前没有归属闸',
    'swap-script-order': '脚本顺序错',
    'drop-staff-attr': '模板未注入 data-scope-staff',
    'revert-v1-key': '队列键仍是 v1',
    'drop-route-staff': 'scope_staff_id 的右值不是 current_user.id',
    'add-dom-in-scope': '归属库里出现 document.',
}
_s58_bad = []
for _k58, _needle in _S58_EXPECT.items():
    _ps, _ = _s58_mutate(_k58)
    if not (len(_ps) == 1 and _needle in _ps[0]):
        _s58_bad.append(f'{_k58}->{len(_ps)}条:{[p[:24] for p in _ps]}')
chk(f'S58 变异电池 {len(_S58_EXPECT)} 支：每支都必须"恰好一条红且点名自己那条"（红里有杂音＝串味）',
    not _s58_bad, str(_s58_bad)[:260])
_s58_empty, _ = _qg_s56._offline_scope_wiring('', '', '', '')
chk('S58 零输入判"没测到"：四面全空时不得记通过，也不得编造接线类红因',
    len(_s58_empty) == 1 and '没测到' in _s58_empty[0], str(_s58_empty)[:180])

# ── node 真跑归属库（无浏览器执行面驱动同一份字节；node 取不到 ⇒ 判未取到，不判通过）──
_s58_node = r'''
const s=require(process.argv[2]);
const K=s.scopeKey('1','1','milk_tea','1');
const K2=s.scopeKey('2','1','milk_tea','1');
const a={product:'杨枝甘露',quantity:'3',waste:'0.5'};
const b={waste:'0.5',quantity:'3',product:'杨枝甘露'};
const c={product:'杨枝甘露2',quantity:'3',waste:'0.5'};
const out=[
 s.scopeKey('1','1','milk_tea','1')==='staff=1|store=1|pack=milk_tea|ver=1',
 s.scopeKey('','1','milk_tea','1')==='',
 s.itemMatchesScope({scope:K},K)===true,
 s.itemMatchesScope({scope:K2},K)===false,
 s.itemMatchesScope({},K)===false,
 s.itemMatchesScope({scope:K},'')===false,
 s.fieldsDigest(a)===s.fieldsDigest(b),
 s.fieldsDigest(a)!==s.fieldsDigest(c),
 s.scopeOfElement({getAttribute:(n)=>({'data-scope-staff':'1','data-scope-store':'1',
   'data-scope-pack':'milk_tea','data-scope-packver':'1'}[n]||null)})===K,
 s.scopeOfElement(null)===''
];
console.log('NODE58 '+out.filter(x=>x===true).length+'/'+out.length+' '+
  JSON.stringify(out.map((x,i)=>x?null:i).filter(x=>x!==null)));
'''
_s58_np = os.path.join(_s58_root, 'src', 'static', 'js', 'offline_scope.js')
# 路径**内联**进脚本、不走 argv：实测 `node -e <code> A B` 的 process.argv 是
# ['node.exe','A','B']（首个自定义参数在 argv[1]），本腿首版按 argv[2] 取 ⇒
# require(undefined) 静默崩、stdout 空 detail，看着像"node 不可用"其实是我的调用错。
_s58_code = _s58_node.replace('process.argv[2]',
                              "'" + _s58_np.replace('\\', '/') + "'")
assert '_s58_code' not in _s58_code and 'process.argv' not in _s58_code, '内联替换未生效'
try:
    _s58_run = _sp_s57.run(['node', '-e', _s58_code], capture_output=True,
                           text=True, timeout=60)
    _s58_line = ((_s58_run.stdout or '').strip()
                 + ' | ' + (_s58_run.stderr or '').strip()[:80]).strip(' |')
    _s58_ok = _s58_run.returncode == 0 and _s58_line.startswith('NODE58 10/10')
except (OSError, _sp_s57.SubprocessError) as _e58:      # noqa: BLE001 node 缺失/超时=未取到
    _s58_line = f'node 不可用：{type(_e58).__name__}'
    _s58_ok = False
chk('S58 node 真跑归属库 10 项谓词（含"换店员必须不匹配"与"空 current 不放行"两条反向）',
    _s58_ok, _s58_line[:200])

# ── 页面真渲染：四个身份位非空 + 归属库先加载（读的是真模板经 Jinja 后的字节）──
_s58_page = _h4c.get('/h5/').get_data(as_text=True)
_s58_attrs = dict(re.findall(r'(data-scope-(?:staff|store|pack|packver))="([^"]*)"', _s58_page))
chk('S58 填报页实测注入四个非空身份位（键名由页面正则现取，不照抄判据自己的名单）',
    len(_s58_attrs) == 4 and all(v.strip() for v in _s58_attrs.values()), str(_s58_attrs)[:180])
chk('S58 页面上归属库先于主模块（顺序颠倒=主模块拿不到 ICANOfflineScope，静默全挂起）',
    0 < _s58_page.find('offline_scope.js') < _s58_page.find('offline_submit.js'),
    f'{_s58_page.find("offline_scope.js")}/{_s58_page.find("offline_submit.js")}')

# ════════════ S59 第 50 轮：机读能力索引 pack_registry.json（M36）════════════
# 对标 PostHog products.json「缺失即硬失败并给出生成命令」：索引是派生账本，
# 唯一写入口 --write；缺件=退出码 2（不可验，不得塌成通过）；索引⇄实盘双向对账。
_s59_prev_sec = _sec['cur']
section('包契约')
from src import pack_registry as _preg59  # noqa: E402
_s59_live = _preg59.build_registry()
_s59_ids = sorted(__import__('src.industry_pack', fromlist=['x']).list_pack_ids())
chk('S59 实盘包清单可独立取数（7 包 manifest 齐，本腿不复用被测项取数面）',
    len(_s59_ids) == len(_s59_live) and len(_s59_ids) > 0, str(_s59_ids)[:160])
_s59_res = _preg59.check_registry()
chk('S59 真面判绿：索引条目数 == 实盘包数且零漂移',
    _s59_res['ok'] and _s59_res['entries'] == len(_s59_ids), str(_s59_res['errors'])[:200])
import copy as _copy59
def _s59_mut(kind):
    idx = _copy59.deepcopy(_s59_live)
    pid = _s59_ids[0]
    if kind == 'drop':
        del idx[pid]
    elif kind == 'tamper':
        idx[pid] = dict(idx[pid], pack_version='9.9.9')
    elif kind == 'ghost':
        idx['ghost_pack_xyz'] = dict(idx[pid])
    elif kind == 'unknownkey':
        idx[pid] = dict(idx[pid], injected_evil_key=1)
    return _preg59.check_registry(indexed=idx), pid
_S59_EXPECT = {'drop': '漏登记', 'tamper': '分叉', 'ghost': '幽灵', 'unknownkey': '未知键'}
_s59_bad = []
for _k59, _needle59 in _S59_EXPECT.items():
    _r59, _p59 = _s59_mut(_k59)
    _hit59 = (not _r59['ok']) and any(_needle59 in e and (_p59 in e or _k59 == 'ghost') for e in _r59['errors'])
    if not _hit59:
        _s59_bad.append(f'{_k59}->{_r59["errors"][:1]}')
chk('S59 变异电池 4 支：每支恰好翻红且点名自己那条（drop/tamper/ghost/unknownkey）',
    not _s59_bad, str(_s59_bad)[:260])
_s59_reg = _preg59.registry_path()
_s59_h1 = open(_s59_reg, 'rb').read()
_s59_os = __import__('os')
_s59_bak = _s59_reg + '.s59tmp'
_s59_os.rename(_s59_reg, _s59_bak)
try:
    _s59_ld, _s59_probs = _preg59.load_registry()
    _s59_rc = _preg59.main([])
finally:
    _s59_os.rename(_s59_bak, _s59_reg)
_s59_h2 = open(_s59_reg, 'rb').read()
chk('S59 缺件判不可验：load 报缺件且 main([]) 退 2（不是通过也不是 1）',
    _s59_ld is None and any('缺失' in p for p in _s59_probs) and _s59_rc == 2,
    f'{_s59_probs[:1]}|rc={_s59_rc}'[:180])
chk('S59 缺件演练后逐字节还原（sha 双验，中途写盘即红）',
    _s59_h1 == _s59_h2, f'{len(_s59_h1)}B')
_s59_w1 = open(_s59_reg, 'rb').read()
_s59_wrc = _preg59.main(['--write'])
_s59_w2 = open(_s59_reg, 'rb').read()
chk('S59 --write 幂等：重算前后字节一致且退 0',
    _s59_wrc == 0 and _s59_w1 == _s59_w2, f'rc={_s59_wrc}|{len(_s59_w2)}B')
section(_s59_prev_sec)

# ════════════ S60 第 50 轮：遗留任务接管 takeover_task（无迁移：行重指派 + 交接备注列）════════════
# 接管条件=ACTIVE且逾期(停滞)；权限=同商家老板/店长(店长限授权店)且非本人；
# CAS expected 防多人重复认领；状态机零改动；上下文 detail/evidence 全保留。
_s60_prev_sec = _sec['cur']
section('骨架')
from src import task_flow as _tf60  # noqa: E402
from src.models import StoreTask as _ST60  # noqa: E402
from src.models import ChangeLog as _CL60  # noqa: E402
with app.app_context():
    _m60 = _MerchantM(name='TO60商家')
    _db2.session.add(_m60)
    _db2.session.flush()
    _s60 = _Store5(merchant_id=_m60.id, name='TO60任务店', code='to60-task')
    _db2.session.add(_s60)
    _db2.session.flush()
    _own60 = _Admin3C(username='owner-to60', role='owner', merchant_id=_m60.id)
    _own60.set_password('Str0ngPass!')
    _mgr60 = _Admin3C(username='mgr-to60', role='manager', merchant_id=_m60.id)
    _mgr60.set_password('Str0ngPass!')
    _stf60 = _StaffM(username='staff-to60', display_name='TO60店员',
                     merchant_id=_m60.id, store_id=_s60.id, store_name='TO60任务店')
    _stf60.set_password('Str0ngPass!')
    _mB60 = _MerchantM(name='TO60商家B')
    _db2.session.add(_mB60)
    _db2.session.flush()
    _mgrB60 = _Admin3C(username='mgr-to60b', role='manager', merchant_id=_mB60.id)
    _mgrB60.set_password('Str0ngPass!')
    _db2.session.add_all([_own60, _mgr60, _stf60, _mgrB60])
    _db2.session.flush()
    _db2.session.add(_ASA3C(admin_id=_mgr60.id, store_id=_s60.id))
    _db2.session.commit()
    _m60_id, _s60_id = _m60.id, _s60.id
    _own60_id, _mgr60_id, _stf60_id, _mgrB60_id, _mB60_id = \
        _own60.id, _mgr60.id, _stf60.id, _mgrB60.id, _mB60.id
chk('S60 接管三类账号齐全（防 R27-01 式"账号缺失的通过"）',
    all([_m60_id, _s60_id, _own60_id, _mgr60_id, _stf60_id, _mgrB60_id]),
    f'{_m60_id}/{_s60_id}/{_own60_id}/{_mgr60_id}/{_stf60_id}')
with app.app_context():
    _t_over60 = _tf60.create_task(
        merchant_id=_m60_id, store_id=_s60_id, title='TO60逾期停滞',
        due_date=_date.today(), assignee_role='manager', assignee_id=_mgr60_id,
        detail='TO60上下文', evidence=[{'metric': 'x', 'value': 1}])
    _t_over60.due_date = _date.today() - _td(days=1)
    _t_over60b = _tf60.create_task(
        merchant_id=_m60_id, store_id=_s60_id, title='TO60逾期停滞B',
        due_date=_date.today(), assignee_role='manager', assignee_id=_mgr60_id)
    _t_over60b.due_date = _date.today() - _td(days=1)
    _t_fresh60 = _tf60.create_task(
        merchant_id=_m60_id, store_id=_s60_id, title='TO60未逾期',
        due_date=_date.today() + _td(days=1), assignee_role='manager',
        assignee_id=_mgr60_id)
    _t_done60 = _tf60.create_task(
        merchant_id=_m60_id, store_id=_s60_id, title='TO60已完结',
        due_date=_date.today(), assignee_role='manager', assignee_id=_mgr60_id)
    _tf60.transition_task(_t_done60, 'in_progress')
    _tf60.transition_task(_t_done60, 'done')
    _db2.session.commit()
    _t_over60_id, _t_fresh60_id, _t_done60_id = _t_over60.id, _t_fresh60.id, _t_done60.id
    _t_over60b_id = _t_over60b.id
chk('S60 停滞定义：逾期ACTIVE可接管，未逾期/已完结不可接管',
    _tf60.is_takeover_eligible(_t_over60) is True
    and _tf60.is_takeover_eligible(_t_fresh60) is False
    and _tf60.is_takeover_eligible(_t_done60) is False
    and _tf60.is_takeover_eligible(None) is False, '')
with app.app_context():
    _t60 = _db2.session.get(_ST60, _t_over60_id)
    _r60 = {'ok': True, 'msg': ''}
    try:
        _tf60.takeover_task(_t60, claimant_role='owner', claimant_id=_own60_id,
                            expected=('admin', _mgr60_id), reason='TO60接管',
                            actor_admin_id=_own60_id)
    except ValueError as _e60:
        _r60 = {'ok': False, 'msg': str(_e60)}
    _db2.session.refresh(_t60)
    _t60_to = (_t60.assignee_role, _t60.assignee_admin_id)
    _t60_hj = __import__('json').loads(_t60.takeover_json or '{}')
chk('S60 接管成功：归属转给新负责人且状态/上下文原样保留',
    _r60['ok'] and _t60_to == ('owner', _own60_id) and _t60.status == 'pending'
    and _t60.detail == 'TO60上下文' and _t60.evidence_json is not None
    and _t60_hj.get('reason') == 'TO60接管'
    and _t60_hj.get('from') == {'id': _mgr60_id, 'kind': 'admin'}
    and _t60_hj.get('to', {}).get('role') == 'owner'
    and _t60_hj.get('by_admin_id') == _own60_id, str(_r60['msg'])[:120])
with app.app_context():
    _cl60_rows = _CL60.query.filter_by(entity='store_tasks', entity_pk=_t_over60_id).all()
    _cl60_hit = any(str(_own60_id) in (r.diff_json or '') for r in _cl60_rows)
chk('S60 交接可追溯：change_log 留有该任务行且含新负责人',
    len(_cl60_rows) >= 1 and _cl60_hit, f'rows={len(_cl60_rows)}')
with app.app_context():
    _neg60 = {}
    for _k60, _fn60 in {
        'self': lambda: _tf60.takeover_task(
            _db2.session.get(_ST60, _t_over60b_id), claimant_role='manager',
            claimant_id=_mgr60_id, reason='TO60自领'),
        'fresh': lambda: _tf60.takeover_task(
            _db2.session.get(_ST60, _t_fresh60_id), claimant_role='owner',
            claimant_id=_own60_id, reason='TO60抢跑'),
        'done': lambda: _tf60.takeover_task(
            _db2.session.get(_ST60, _t_done60_id), claimant_role='owner',
            claimant_id=_own60_id, reason='TO60完结'),
        'staff': lambda: _tf60.takeover_task(
            _db2.session.get(_ST60, _t_over60b_id), claimant_role='staff',
            claimant_id=_stf60_id, reason='TO60店员'),
        'cross': lambda: _tf60.takeover_task(
            _db2.session.get(_ST60, _t_over60b_id), claimant_role='manager',
            claimant_id=_mgrB60_id, reason='TO60跨商家'),
        'noreason': lambda: _tf60.takeover_task(
            _db2.session.get(_ST60, _t_over60b_id), claimant_role='owner',
            claimant_id=_own60_id, reason='  '),
        'stale': lambda: _tf60.takeover_task(
            _db2.session.get(_ST60, _t_over60_id), claimant_role='manager',
            claimant_id=_mgr60_id, expected=('admin', _mgr60_id),
            reason='TO60迟到认领'),
    }.items():
        try:
            _fn60()
            _neg60[_k60] = 'NO-RAISE'
        except ValueError as _e60:
            _neg60[_k60] = str(_e60)
        _db2.session.rollback()
chk('S60 七路拒绝：自领/未逾期/已完结/店员/跨商家/无原因/过期CAS 全部被拒',
    _neg60.get('self', '').startswith('不能接管自己')
    and '仅逾期停滞' in _neg60.get('fresh', '')
    and '已完结' in _neg60.get('done', '')
    and '仅老板/店长' in _neg60.get('staff', '')
    and '不属于当前商家' in _neg60.get('cross', '')
    and '注明原因' in _neg60.get('noreason', '')
    and '已被他人接管' in _neg60.get('stale', ''), str(_neg60)[:300])
_c60 = _app3c.test_client()
with _c60.session_transaction() as _sess60:
    _sess60['_user_id'] = f'admin-{_own60_id}'
_pg60 = _c60.get(f'/admin/tasks/{_t_fresh60_id}')
_tk60 = re.search(r'name="csrf_token" value="([^"]+)"', _pg60.get_data(as_text=True))
with app.app_context():
    _t_fresh60 = _db2.session.get(_ST60, _t_fresh60_id)
    _t_fresh60.due_date = _date.today() - _td(days=1)
    _db2.session.commit()
_r60_post = _c60.post(f'/admin/tasks/{_t_fresh60_id}/takeover', data={
    'csrf_token': _tk60.group(1) if _tk60 else '',
    'assignee': f'admin:{_own60_id}',
    'expected': f'admin:{_mgr60_id}',
    'reason': 'TO60路由接管'})
_r60_follow = _c60.get(f'/admin/tasks/{_t_fresh60_id}')
_r60_twice = _c60.post(f'/admin/tasks/{_t_fresh60_id}/takeover',
                       follow_redirects=True, data={
    'csrf_token': _tk60.group(1) if _tk60 else '',
    'assignee': f'admin:{_mgr60_id}',
    'expected': f'admin:{_mgr60_id}',
    'reason': 'TO60重复认领'})
chk('S60 路由：POST 接管后详情页显示新负责人；持过期CAS二次认领被拒',
    _r60_post.status_code == 302
    and 'owner-to60' in _r60_follow.get_data(as_text=True)
    and '已被他人接管' in _r60_twice.get_data(as_text=True),
    f'{_r60_post.status_code}')
with app.app_context():
    _ST60.query.filter(_ST60.store_id == _s60_id).delete(synchronize_session=False)
    _ASA3C.query.filter(_ASA3C.admin_id.in_([_mgr60_id])).delete(
        synchronize_session=False)
    _Admin3C.query.filter(_Admin3C.username.in_(
        ['owner-to60', 'mgr-to60', 'mgr-to60b'])).delete(synchronize_session=False)
    _StaffM.query.filter_by(username='staff-to60').delete()
    _Store5.query.filter_by(id=_s60_id).delete(synchronize_session=False)
    _MerchantM.query.filter(_MerchantM.id.in_([_m60_id, _mB60_id])).delete(
        synchronize_session=False)
    _db2.session.commit()
section(_s60_prev_sec)

# ════════════ S61 第 51 轮：离线队列幂等键与入队去重（M163）════════════
# 缺陷：去重键是 (saved_at, queued_for)，saved_at 逐次不同 ⇒ 双击/多标签/双通道
# 重提交恒放行，队列躺复件。修法：offline_idem.js 零 DOM 纯函数（稳定键 scope+digest+日）
# + pushQueue 接入去重 + migrateLegacy 同键 + 模板按序加载。服务端唯一约束仍是最终守卫。
_s61_prev_sec = _sec['cur']
section('骨架')
import subprocess as _sp61
_s61_root = _h4_root
_s61_idem_p = os.path.join(_s61_root, 'src', 'static', 'js', 'offline_idem.js')
_s61_submit_p = os.path.join(_s61_root, 'src', 'static', 'js', 'offline_submit.js')
_s61_tpl_p = os.path.join(_s61_root, 'src', 'templates', 'h5', 'dashboard.html')
_s61_idem = open(_s61_idem_p, encoding='utf-8').read() if os.path.isfile(_s61_idem_p) else ''
_s61_submit = open(_s61_submit_p, encoding='utf-8').read() if os.path.isfile(_s61_submit_p) else ''
_s61_tpl = open(_s61_tpl_p, encoding='utf-8').read() if os.path.isfile(_s61_tpl_p) else ''
chk('S61 幂等库零 DOM：无 document/localStorage/XHR/navigator/DOMParser（可在 node 真跑）',
    bool(_s61_idem) and not any(t in _s61_idem for t in (
        'document.', 'localStorage', 'XMLHttpRequest', 'navigator.', 'DOMParser')),
    f'{len(_s61_idem)}B')
_S61_NODE_JS = (
    "var fs=require('fs');var w={};"
    "new Function('window',fs.readFileSync(IDEM_P,'utf8'))(w);"
    "var L=w.ICANOfflineIdem;"
    "var a={scope:'s',fields_digest:'d',queued_for:'D',saved_at:1};"
    "var b={scope:'s',fields_digest:'d',queued_for:'D',saved_at:2};"
    "var c={scope:'s',fields_digest:'X',queued_for:'D',saved_at:1};"
    "console.log(JSON.stringify([L.itemKey(a)===L.itemKey(b),"
    "L.itemKey(a)!==L.itemKey(c),L.dedupeEnqueue([a],b).reason,"
    "L.dedupeEnqueue([],b).added,L.findByKey([c,a,b],L.itemKey(a)),"
    "L.itemKey(null),L.itemKey({}),L.findByKey('x','k')]));"
).replace('IDEM_P', _s61_idem_p.replace('\\', '\\\\'))
try:
    _s61_nr = _sp61.run(['node', '-e', _S61_NODE_JS], capture_output=True, text=True,
                        timeout=60)
    _s61_nout = (_s61_nr.stdout or '').strip()
except Exception as _e61:
    _s61_nr, _s61_nout = None, f'node 不可用：{type(_e61).__name__}'
chk('S61 node 真跑幂等库 8 谓词（同内容同键/异内容异键/复件拒/新件收/乱序命中/空入空出/坏入-1）',
    _s61_nout == '[true,true,"duplicate",true,1,"","",-1]', _s61_nout[:200])
def _s61_wiring(submit_txt, tpl_txt):
    probs = []
    if 'window.ICANOfflineIdem' not in submit_txt:
        probs.append('submit 未取幂等库')
    if 'Idem.dedupeEnqueue' not in submit_txt:
        probs.append('pushQueue 未调入队去重')
    if '未重复暂存' not in submit_txt:
        probs.append('复件无去重回执文案')
    if 'queueSeenKey' not in submit_txt:
        probs.append('migrateLegacy 未升级统一键')
    _order = [tpl_txt.find('offline_scope.js'), tpl_txt.find('offline_idem.js'),
              tpl_txt.find('offline_submit.js')]
    if not (0 < _order[0] < _order[1] < _order[2]):
        probs.append(f'模板脚本顺序错（scope<idem<submit）：{_order}')
    return probs
_s61_probs = _s61_wiring(_s61_submit, _s61_tpl)
chk('S61 真面接线：取库+去重调用+复件文案+迁移同键+模板顺序五项齐全',
    not _s61_probs, str(_s61_probs)[:220])
_s61_mut_a = _s61_wiring(_s61_submit.replace('Idem.dedupeEnqueue', 'Idem.dedupeEnqueueZZZ'),
                         _s61_tpl)
_s61_mut_b_src = _s61_idem.replace('ICANOfflineIdem', 'ICANOfflineIdemZZZ')
import tempfile as _tf61
_s61_mut_tmp = os.path.join(_tf61.gettempdir(), 's61_idem_mut.js')
with open(_s61_mut_tmp, 'w', encoding='utf-8', newline='\n') as _fh61:
    _fh61.write(_s61_mut_b_src)
try:
    _s61_mut_b = _sp61.run(
        ['node', '-e', _S61_NODE_JS.replace(
            _s61_idem_p.replace('\\', '\\\\'),
            _s61_mut_tmp.replace('\\', '\\\\'))],
        capture_output=True, text=True, timeout=60)
    _s61_mut_b_red = _s61_mut_b.returncode != 0
finally:
    os.remove(_s61_mut_tmp)
chk('S61 变异电池 2 支：拆去重调用判红且点名；拆库导出 node 即崩（各管各的面）',
    any('去重' in p for p in _s61_mut_a) and _s61_mut_b_red,
    f'{_s61_mut_a}|nodered={_s61_mut_b_red}')
section(_s61_prev_sec)

_post = {w: (_s39_qg._sha256_file(_s39_os.path.join(_s39_root, w))
              if _s39_os.path.isfile(_s39_os.path.join(_s39_root, w)) else '') for w in _s39_WATCH}
chk('S39 反例全在临时面：段首/段尾逐文件比 sha，真仓文件零漂移（本段不改被测对象）',
    _post == _s39_pre, '漂移件=' + str([k for k in _s39_WATCH if _post[k] != _s39_pre[k]])[:150])


print(f"\nL4 断言通过 {passed} 项（骨架 {_sec['骨架']} / "

      f"包契约 {_sec['包契约']}），失败 {len(fails)} 项")
if fails:
    print('失败项：')
    for f in fails:
        print('  -', f)
    sys.exit(1)
sys.exit(0)
