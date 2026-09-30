"""行业包 DB 覆盖层服务（P4 配置后台）—— 包 DIY 内容的唯一读写入口。

职责边界：
  - industry_pack：只读消费方（三级回退：DB 覆盖 → 文件包 → 内置兜底），
    不感知 DB；本模块经 `register_db_override()` 向其注入取数函数。
    覆盖内容以「内联段」形式并入包字典（questions/scenarios/products/
    knowledge 等），**不写回 src 目录文件**——权威目录/部署副本按 src 树
    哈希比对（门禁第 10 项），运行期生成文件会造成三副本漂移。
  - pack_store（本模块）：覆盖层存储的读写 + 入参校验 + 缓存失效收口。
    读路径以「文件包为底、DB 段覆盖」合并，未编辑段零漂移；
    写路径全部来自后台表单，逐段校验后落库，非法即拒绝（不入库）。
  - routes/industry：HTTP 层（CSRF/flash/重定向），只做视图编排。

设计约定（与既有模块一致）：
  1. 所有 DB 读取失败一律回退 + WARNING 日志，绝不把异常扩散到业务层；
  2. 任何内容/激活态变更后必须调用 `industry_pack.invalidate()`，
     否则指纹缓存会让改动延迟生效（缓存键含 revision）；
  3. 入参上限：域 ≤ MAX_DOMAINS、指标 ≤ MAX_METRICS、单字段长度有界，
     防误填/恶意大文本撑爆 payload。
"""
import json
import logging
import re
import threading
import time

from src import change_log, industry_pack
from src.models import IndustryPack, MetricDef, PackItem, db
from src.validators import (  # P7-E / S-8：与管理员产品页同口径的数值校验
    MAX_UNIT_PRICE,
    ParseError,
    check_range,
    parse_float,
)

logger = logging.getLogger(__name__)

# 内容段白名单（PackItem.section 取值）
SECTIONS = ('terms', 'domains', 'weak_keywords', 'synonyms',
            'formula_params', 'knowledge', 'questions', 'scenarios', 'products')
# 单值段（key=''）与多值段（knowledge 按域存）
_MULTI_SECTION = 'knowledge'

# 入参上限（防误填污染统计 / 超长文本撑爆 payload）
MAX_FIELD_LEN = 200            # 单字段（术语值/域名/关键词等）
MAX_KNOWLEDGE_LEN = 20000      # 单篇知识 md
MAX_QUESTION_LEN = 500         # 题干
MAX_ANSWER_LEN = 1000          # 参考答案
MAX_QUESTIONS = 200            # 题库条数上限
MAX_SCENARIO_LINES = 40        # 单情景话术条数上限
# P7-E / S-9：补齐此前缺失的数量上限（模块头注释承诺「长度/数量有界」但 4 段有缺口）
MAX_SYNONYMS = 60              # 同义词对上限
MAX_SCENARIOS = 20             # 情景个数上限
MAX_PRODUCTS = 60              # 在售产品条数上限
# P7-E / S-10：PackItem.key 列宽 50（models.py），域名即 key，必须与列宽同源
MAX_KEY_LEN = 50
PACK_ID_RE = re.compile(r'^[a-z][a-z0-9_]{0,49}$')
SCENARIO_TYPE_RE = re.compile(r'^[a-z][a-z0-9_]{0,29}$')

# 指标定义合法取值集（与 metrics.py / models.MetricDef 注释对齐）
VALID_VALUE_TYPES = ('number', 'integer', 'text')
VALID_AGGS = ('sum', 'avg', 'count', 'none')
VALID_ROLES = ('dimension', 'quantity', 'amount', 'stock', 'waste',
               'cost', 'note', 'other')
SKELETON_ROLES = ('dimension', 'quantity', 'amount')


# ─────────────────────────── 读路径（DB 覆盖层取数）───────────────────────────
def _json_or_none(raw):
    """payload JSON 解码；损坏返回 None（调用方忽略该段）。"""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning('[pack_store] payload 解码失败，按缺失处理: %.60s', str(raw)[:60])
        return None


def _pack_meta_row(pack_id):
    return IndustryPack.query.filter_by(pack_id=pack_id).first()


def pack_exists(pack_id):
    """包是否真实存在（P7-E / S-7）：格式合法 **且** 命中文件包或 DB 登记表。

    此前路由层只做正则校验，任意「格式合法但不存在的 id」都会拿默认包内容顶着该 id
    渲染，一保存就在幽灵 id 下写入 DB 覆盖行（不可见但生效）。

    Returns:
        bool: 存在 True；格式非法 / 查不到 / DB 异常均 False（异常只告警不外扩）。
    """
    if not pack_id or not PACK_ID_RE.match(pack_id):
        return False
    if pack_id in industry_pack.list_pack_ids():
        return True
    try:
        return _pack_meta_row(pack_id) is not None
    except Exception as exc:
        logger.warning('[pack_store] 包存在性校验失败 %s: %s', pack_id, exc)
        return False


def _commit(action):
    """统一提交（P7-E / S-14）：失败即回滚 + 告警，返回 (ok, message)。

    此前 8 处写操作是裸 `db.session.commit()`，DB 抖动 / DataError / 唯一约束冲突
    会直接抛到路由层变成 500，且 session 未回滚会污染同线程后续请求。

    Returns:
        tuple: (True, '') 或 (False, 面向管理员的中文提示)。
    """
    try:
        db.session.commit()
        return True, ''
    except Exception as exc:
        db.session.rollback()
        logger.warning('[pack_store] %s 保存失败（已回滚）: %s', action, exc)
        return False, '保存失败，请稍后重试（数据库写入异常）'


def override_fn(pack_id):
    """DB 覆盖层取数函数（注册给 industry_pack）。

    合并规则：以文件包（或内置兜底）为底，DB 中存在且合法的段整体替换。
    返回 None 表示该包无任何 DB 痕迹（industry_pack 继续走文件包）。
    """
    try:
        out = _override_fn_inner(pack_id)
    except Exception as exc:  # DB 异常不得扩散到业务层（与 industry_pack 约定一致）
        logger.warning('[pack_store] 覆盖层读取异常 %s: %s', pack_id, exc)
        # PF-9：覆盖层在 pack_store 内部就被吞掉了（industry_pack 那层捕获不到），
        # 因此故障态必须在这里登记，否则管理员「保存不生效」永远是静默的。
        industry_pack.note_override_error(pack_id, exc)
        return None
    industry_pack.clear_override_error(pack_id)
    return out


def _override_fn_inner(pack_id):
    meta = _pack_meta_row(pack_id)
    items = PackItem.query.filter_by(pack_id=pack_id).all()
    metric_rows = MetricDef.query.filter_by(
        pack_id=pack_id, enabled=True).order_by(MetricDef.sort, MetricDef.key).all()
    if meta is None and not items and not metric_rows:
        return None  # 无任何 DB 痕迹：不干扰文件包

    base = industry_pack.load_file_pack(pack_id) or dict(industry_pack.FALLBACK_PACK)
    raw = dict(base)
    raw['id'] = pack_id
    if meta is not None:
        raw['name'] = meta.name or raw.get('name')
        raw['description'] = meta.description or raw.get('description')

    sections = {}
    for r in items:
        sections.setdefault(r.section, []).append(r)

    has_content_override = bool(items) or bool(metric_rows)
    raw['source'] = 'db' if has_content_override else base.get('source', 'file')

    def _payload(section):
        rows = sections.get(section)
        if not rows:
            return None
        if section == _MULTI_SECTION:
            out = {}
            for r in rows:
                val = _json_or_none(r.payload)
                if isinstance(val, str) and r.key:
                    out[r.key] = val
            return out or None
        return _json_or_none(rows[0].payload)

    for section in ('terms', 'domains', 'weak_keywords', 'synonyms',
                    'formula_params', 'questions', 'scenarios', 'products', 'manifest'):
        val = _payload(section)
        if val is not None:
            raw[section] = val
    # 清单必须是对象：被手改坏的 DB 行按「未声明」处理（回落文件包/合成态）。
    if not isinstance(raw.get('manifest'), dict):
        raw.pop('manifest', None)

    # 知识条目：DB 按域覆盖文件包（域内全文替换，未覆盖域保留文件版）
    kb = _payload(_MULTI_SECTION)
    if kb:
        merged = industry_pack.file_knowledge(pack_id)
        merged.update(kb)
        raw['knowledge'] = merged

    # 指标定义：DB 为唯一真相源（后台可增删启停；legacy_column 存 options_json）。
    # P7-B2 修复：options_json 未登记 legacy_column 的行（历史播种库/旧库）回退
    # 文件包同键定义 —— 否则 DB 覆盖层一旦接管，骨架字段全部失联
    # （skeleton_field() 拿不到映射列名 → H5 表单只剩自定义指标组，无法填报）。
    if metric_rows:
        file_legacy = {d['key']: d.get('legacy_column', '')
                       for d in base.get('metrics') or []}
        raw['metrics'] = [{
            'key': m.key, 'label': m.label, 'value_type': m.value_type,
            'unit': m.unit, 'role': m.role, 'agg': m.agg,
            'required': bool(m.required), 'sort': m.sort,
            'legacy_column': _legacy_column_of(m) or file_legacy.get(m.key, ''),
        } for m in metric_rows]
    return raw


def _legacy_column_of(m):
    """从 MetricDef.options_json 读 legacy_column（损坏/缺失返回空串）。"""
    if not m.options_json:
        return ''
    try:
        opts = json.loads(m.options_json)
    except (TypeError, ValueError):
        return ''
    val = opts.get('legacy_column') if isinstance(opts, dict) else None
    return val if isinstance(val, str) else ''


# ─────────────────────────── 包列表与元信息 ───────────────────────────
# P3：all_packs 位于 app.context_processor，后台每个页面渲染都会执行
# （listdir + DB 查询 + 逐包 get_pack）。这里做进程级短 TTL 缓存（与
# industry_pack.FINGERPRINT_TTL_SECONDS 同量级），内容变更统一经
# _invalidate() 失效；激活态不入缓存（读时现算，set_active 永不陈旧）。
# DB-only 元信息（name/description 直改 DB 绕过本模块）最多延迟一个 TTL。
_ALL_PACKS_TTL_SECONDS = 2.0
_ALL_PACKS_CACHE = {'items': None, 'expires': 0.0}
_ALL_PACKS_LOCK = threading.Lock()


def _invalidate():
    """缓存失效统一收口：industry_pack 全局失效 + 本模块 all_packs 失效。"""
    industry_pack.invalidate()
    with _ALL_PACKS_LOCK:
        _ALL_PACKS_CACHE['items'] = None


def all_packs():
    """全部包摘要（文件包 + 仅 DB 包），含激活态与统计数。"""
    now = time.monotonic()
    with _ALL_PACKS_LOCK:
        items = _ALL_PACKS_CACHE['items']
        fresh = items is not None and now < _ALL_PACKS_CACHE['expires']
    if not fresh:
        items = _build_all_packs()
        with _ALL_PACKS_LOCK:
            _ALL_PACKS_CACHE['items'] = items
            _ALL_PACKS_CACHE['expires'] = time.monotonic() + _ALL_PACKS_TTL_SECONDS
    active = active_pack_id()
    return [dict(row, active=(row['id'] == active)) for row in items]


def _build_all_packs():
    """构建包摘要（无激活态字段；由 all_packs 读时补齐）。"""
    file_ids = set(industry_pack.list_pack_ids())
    metas = {m.pack_id: m for m in IndustryPack.query.all()}
    ids = list(file_ids)
    for meta in metas.values():
        if meta.pack_id not in ids:
            ids.append(meta.pack_id)
    from src import pack_manifest
    out = []
    for pid in sorted(ids):
        pack = industry_pack.get_pack(pid)
        meta = metas.get(pid)
        out.append({
            'id': pid,
            'name': (meta.name if meta else pack['name']),
            'description': (meta.description if meta else pack.get('description', '')),
            'source': pack['source'],
            'is_file_pack': pid in file_ids,
            'domain_count': len(pack['domains']),
            'metric_count': len(pack['metrics']),
            'question_count': len(_questions_of(pack)),
            'scenario_count': len((pack.get('scenarios') or {}).get('pool') or {}),
            'manifest': pack_manifest.manifest_view(pack),
        })
    return out


def _questions_of(pack):
    """从包字典取题库列表（DB 内联段或文件段，缺失返回 []）。"""
    q = pack.get('questions')
    return q if isinstance(q, list) else []


def active_pack_id():
    """当前激活包 id（委托 industry_pack）。"""
    return industry_pack.active_pack_id()


def restore_active():
    """启动自检：从 DB 恢复激活包（进程重启后激活态不丢）。

    返回实际生效的包 id；DB 不可用时静默回退（industry_pack 默认链）。
    """
    try:
        meta = IndustryPack.query.filter_by(is_active=True).first()
    except Exception as exc:
        logger.warning('[pack_store] 激活态恢复失败（按默认链继续）: %s', exc)
        return industry_pack.active_pack_id()
    if meta and meta.pack_id != industry_pack.active_pack_id():
        industry_pack.set_active_pack(meta.pack_id)
    return industry_pack.active_pack_id()


def set_active(pack_id):
    """激活指定包（进程级 + DB 持久化）。

    Returns:
        tuple: (ok, message)。
    """
    pack = industry_pack.get_pack(pack_id)
    if pack['source'] == 'builtin' and pack_id not in industry_pack.list_pack_ids():
        return False, f'包 {pack_id} 不可用，无法激活'
    # H3 兼容性闸门（对标 InvenTree plugin/registry.py check_version）：不兼容即拒绝激活，
    # 原因交调用方 flash。判在任何 DB/进程态改动之前——失败时原激活包保持不变。
    from src import pack_manifest
    gate = pack_manifest.check_pack(pack, pack_id=pack_id)
    if not gate['ok']:
        return False, ('行业包「' + pack['name'] + '」与当前平台不兼容，已拒绝激活：'
                       + '；'.join(gate['errors']))
    meta = _pack_meta_row(pack_id)
    IndustryPack.query.filter_by(is_active=True).update({'is_active': False})
    if meta is None:
        meta = IndustryPack(pack_id=pack_id, name=pack['name'],
                            description=pack.get('description', ''), is_active=True)
        db.session.add(meta)
    else:
        meta.is_active = True
    ok, err = _commit('激活包')
    if not ok:
        return False, err
    industry_pack.set_active_pack(pack_id)
    message = f'已激活行业包「{meta.name}」'
    if gate['warnings']:
        message += '（提示：' + '；'.join(gate['warnings']) + '）'
    return True, message


def _reject_new_pack(pack_id, name, copy_from):
    """新建包前置校验（P7-E / S-3）；通过返回 (True, '')。

    copy_from 必须与 pack_id 同白名单且真实存在：旧守卫依赖 get_pack() 的
    「不可解析即回退默认包」特性（source 恒为 file/db），条件永不成立，任意串
    （含 ../..）都会被拼进包目录去列读 md 文件。
    """
    if not PACK_ID_RE.match(pack_id):
        return False, '包标识只允许小写字母开头的字母/数字/下划线（≤50 位）'
    if _pack_meta_row(pack_id) or pack_id in industry_pack.list_pack_ids():
        return False, f'包标识 {pack_id} 已存在'
    if not name or len(name) > 50:
        return False, '行业名称必填（≤50 字）'
    if copy_from:
        if not PACK_ID_RE.match(copy_from):
            return False, '来源包标识非法'
        if not pack_exists(copy_from):
            return False, f'来源包 {copy_from} 不存在'
    return True, ''


def create_pack(pack_id, name, description='', copy_from=None):
    """新建行业包（可复制现有包内容为起点）。

    Returns:
        tuple: (ok, message)。pack_id 非法/已存在 → (False, 原因)。
    """
    pack_id = (pack_id or '').strip()
    name = (name or '').strip()
    ok, msg = _reject_new_pack(pack_id, name, copy_from)
    if not ok:
        return False, msg
    description = (description or '').strip()[:200]

    meta = IndustryPack(pack_id=pack_id, name=name, description=description)
    db.session.add(meta)
    if copy_from:
        _copy_sections(copy_from, pack_id)
    else:
        # PH2-4A：空白包写入中性「通用门店」骨架，避免三级回退塞入奶茶内容
        _seed_neutral_skeleton(pack_id)
    ok, err = _commit('新建包')
    if not ok:
        return False, err
    _invalidate()
    return True, f'行业包「{name}」创建成功'


def set_store_pack(store_id, pack_id):
    """设置门店当前行业包（PH2-6，一店一当前包）。

    Args:
        store_id: 门店主键。
        pack_id: 行业包 id；空串 = 清除门店覆盖、跟随全局激活包。

    Returns:
        tuple: (ok, message)。
    """
    from src.models import Store
    store = db.session.get(Store, store_id)
    if store is None:
        return False, '门店不存在'
    target = (pack_id or '').strip()
    if target:
        if not pack_exists(target):
            return False, f'行业包 {target} 不存在'
        if industry_pack.get_pack(target).get('id') != target:
            return False, f'行业包 {target} 不可用'
    store.active_pack_id = target or None
    ok, err = _commit('设置门店行业包')
    if not ok:
        return False, err
    _invalidate()
    return True, f'门店当前行业包已设为 {target or "跟随全局"}' if target \
        else (True, '门店已改为跟随全局行业包')


def _seed_neutral_skeleton(pack_id):
    """空白包起点：写入中性「通用门店」骨架（PH2-4A）。

    不做这一步时，空包各段为 None → `_normalize_pack()` 三级回退塞入奶茶
    内置兜底，表现为「新建的宠物店包里出现珍珠奶茶题库」。
    """
    from src import pack_skeleton
    sections = pack_skeleton.neutral_sections()
    for section in ('terms', 'domains', 'weak_keywords', 'synonyms',
                    'formula_params', 'questions', 'scenarios', 'products'):
        db.session.add(PackItem(pack_id=pack_id, section=section, key='',
                                payload=json.dumps(sections[section], ensure_ascii=False)))
    for domain, text in sections['knowledge'].items():
        db.session.add(PackItem(pack_id=pack_id, section=_MULTI_SECTION, key=domain,
                                payload=json.dumps(text, ensure_ascii=False)))
    for d in sections['metrics']:
        db.session.add(MetricDef(
            pack_id=pack_id, key=d['key'], label=d['label'],
            value_type=d['value_type'], unit=d['unit'], role=d['role'],
            agg=d['agg'], required=d['required'], enabled=True, sort=d['sort'],
            options_json=(json.dumps({'legacy_column': d['legacy_column']})
                          if d.get('legacy_column') else None),
        ))


def _copy_sections(src_id, dst_id):
    """复制来源包全部内容段到目标包（新建时的起点；DB-only 包同样可复制）。"""
    src_pack = industry_pack.get_pack(src_id)
    for section in ('terms', 'domains', 'weak_keywords', 'synonyms',
                    'formula_params', 'questions', 'scenarios', 'products'):
        val = src_pack.get(section)
        if val is None and section in ('questions', 'scenarios', 'products'):
            val = _file_section_value(src_id, section)
        if val is None:
            continue
        db.session.add(PackItem(pack_id=dst_id, section=section, key='',
                                payload=json.dumps(val, ensure_ascii=False)))
    kb = src_pack.get('knowledge')
    if not kb:
        kb = industry_pack.file_knowledge(src_id)
    for domain, text in (kb or {}).items():
        db.session.add(PackItem(pack_id=dst_id, section=_MULTI_SECTION, key=domain,
                                payload=json.dumps(text, ensure_ascii=False)))
    # 指标定义：以规范化包指标为源（DB 无行时仍能复制文件包指标，防新库空复制）
    for d in src_pack.get('metrics') or []:
        db.session.add(MetricDef(
            pack_id=dst_id, key=d['key'], label=d['label'],
            value_type=d['value_type'], unit=d['unit'], role=d['role'],
            agg=d['agg'], required=d['required'], enabled=True, sort=d['sort'],
            options_json=(json.dumps({'legacy_column': d['legacy_column']})
                          if d.get('legacy_column') else None)))


def delete_pack(pack_id):
    """删除 DB 包（文件包不可删；激活包不可删）。"""
    if pack_id in industry_pack.list_pack_ids():
        return False, '内置文件包不可删除（可编辑其 DB 覆盖内容）'
    meta = _pack_meta_row(pack_id)
    if meta is None:
        return False, '包不存在'
    if meta.is_active or active_pack_id() == pack_id:
        return False, '不能删除当前激活包，请先切换到其他包'
    change_log.purge(PackItem, pack_id=pack_id)
    change_log.purge(MetricDef, pack_id=pack_id)
    db.session.delete(meta)
    ok, err = _commit('删除包')
    if not ok:
        return False, err
    _invalidate()
    return True, f'包 {pack_id} 已删除'


def reset_section(pack_id, section):
    """恢复某段为内置默认（删除 DB 覆盖项，回退文件包）。"""
    if section not in SECTIONS and section != 'metrics':
        return False, '未知内容段'
    # P7-E3 / PF-13：DB-only 包（后台自建、无文件包）没有「内置默认」可回退。
    # 此时 reset 会先删光 MetricDef 再一条也播不出，表单被内置奶茶骨架顶上，
    # 而提示语仍说「已恢复内置默认」——自建包被静默污染。无内置源即拒绝。
    if section == 'metrics' and industry_pack.load_file_pack(pack_id) is None:
        return False, ('该包为自建包，没有内置指标定义可恢复；'
                       '请直接编辑指标，或改用「复制内置包」新建')
    n = change_log.purge(PackItem, pack_id=pack_id, section=section)
    if section == 'metrics':
        change_log.purge(MetricDef, pack_id=pack_id)
        _reseed_metrics(pack_id)
        n = 0
    ok, err = _commit('恢复默认')
    if not ok:
        return False, err
    _invalidate()
    return True, f'已恢复内置默认（清除 {n} 条覆盖）'


def _reseed_metrics(pack_id):
    """按文件包 metrics 重新播种指标定义（恢复默认用）。"""
    pack = industry_pack.load_file_pack(pack_id)
    if not pack:
        return
    for d in pack['metrics']:
        db.session.add(MetricDef(
            pack_id=pack_id, key=d['key'], label=d['label'],
            value_type=d['value_type'], unit=d['unit'], role=d['role'],
            agg=d['agg'], required=d['required'], enabled=True, sort=d['sort'],
            options_json=(json.dumps({'legacy_column': d['legacy_column']})
                          if d.get('legacy_column') else None)))


# ─────────────────────────── 写路径（分段校验）───────────────────────────
def _put_section(pack_id, section, value):
    change_log.purge(PackItem, pack_id=pack_id, section=section, key='')
    db.session.add(PackItem(pack_id=pack_id, section=section, key='',
                            payload=json.dumps(value, ensure_ascii=False)))


def _ensure_meta(pack_id):
    """确保包在 DB 有登记行（编辑文件包时自动登记，供后台展示名/描述）。"""
    meta = _pack_meta_row(pack_id)
    if meta is None:
        pack = industry_pack.get_pack(pack_id)
        meta = IndustryPack(pack_id=pack_id, name=pack['name'],
                            description=pack.get('description', ''),
                            is_active=(pack_id == active_pack_id()))
        db.session.add(meta)
    return meta


def save_terms(pack_id, form):
    """保存门店信息与术语表（表单键 terms__<key>）。"""
    current = industry_pack.get_pack(pack_id)['terms']
    updated = dict(current)
    for key in current:
        raw = form.get(f'terms__{key}')
        if raw is not None:
            val = raw.strip()[:MAX_FIELD_LEN]
            if val:
                updated[key] = val
    _ensure_meta(pack_id)
    _put_section(pack_id, 'terms', updated)
    ok, err = _commit('门店信息与术语')
    if not ok:
        return False, err
    _invalidate()
    return True, '门店信息与术语已保存'


def save_domains(pack_id, form):
    """保存知识域与词表（domains__name/keywords/tip 多值 + weak_keywords + synonyms）。"""
    names = form.getlist('domains__name')
    keywords = form.getlist('domains__keywords')
    tips = form.getlist('domains__tip')
    if not any(n.strip() for n in names):
        return False, '至少保留一个知识域'
    if len(names) > industry_pack.MAX_DOMAINS:
        return False, f'知识域不能超过 {industry_pack.MAX_DOMAINS} 个'
    domains = []
    for i, raw_name in enumerate(names):
        # P7-E / S-10：域名即 PackItem.key（列宽 50），必须与列宽同源截断，
        # 否则 MySQL 严格模式抛 DataError → 500，SQLite 则静默不一致。
        name = raw_name.strip()[:MAX_KEY_LEN]
        if not name:
            continue
        kws = [k.strip()[:30] for k in (keywords[i] if i < len(keywords) else '').split(',')
               if k.strip()][:20]
        domains.append({
            'name': name,
            'knowledge_file': f'{name}.md',
            'tip': (tips[i].strip()[:MAX_FIELD_LEN] if i < len(tips) else ''),
            'keywords': kws,
        })
    if not domains:
        return False, '知识域名称不能全部为空'
    _ensure_meta(pack_id)
    _put_section(pack_id, 'domains', domains)

    # 弱项关键词：单输入框逗号分隔（模板 join(',')），按逗号展开
    weak = []
    for raw in form.getlist('weak_keywords'):
        for part in raw.split(','):
            w = part.strip()[:30]
            if w and w not in weak:
                weak.append(w)
    weak = weak[:60]
    if weak:
        _put_section(pack_id, 'weak_keywords', weak)

    _put_section(pack_id, 'synonyms', _parse_synonyms(form))

    ok, err = _commit('知识域与词表')
    if not ok:
        return False, err
    _invalidate()
    return True, f'知识域与词表已保存（{len(domains)} 个域）'


def save_knowledge(pack_id, form):
    """保存知识条目（表单：knowledge__<域名> = md 全文）。"""
    domains = industry_pack.get_pack(pack_id)['domains']
    saved = 0
    for d in domains:
        name = d['name']
        raw = form.get(f'knowledge__{name}')
        if raw is None:
            continue
        text = raw.strip()[:MAX_KNOWLEDGE_LEN]
        change_log.purge(PackItem, pack_id=pack_id, section=_MULTI_SECTION, key=name)
        if text:
            db.session.add(PackItem(pack_id=pack_id, section=_MULTI_SECTION,
                                    key=name,
                                    payload=json.dumps(text, ensure_ascii=False)))
        saved += 1
    if not saved:
        return False, '没有可保存的知识条目'
    _ensure_meta(pack_id)
    ok, err = _commit('知识条目')
    if not ok:
        return False, err
    _invalidate()
    return True, f'知识条目已保存（{saved} 篇）'


def save_questions(pack_id, form):
    """保存题库（表单：questions__q/a/weak 多值行）。"""
    qs = form.getlist('questions__q')
    as_ = form.getlist('questions__a')
    ws = form.getlist('questions__weak')
    out = []
    for i, raw_q in enumerate(qs):
        q = raw_q.strip()[:MAX_QUESTION_LEN]
        a = (as_[i].strip()[:MAX_ANSWER_LEN] if i < len(as_) else '')
        if not q or not a:
            continue
        out.append({'q': q, 'a': a,
                    'weak': (ws[i].strip()[:50] if i < len(ws) else '')})
    if len(out) > MAX_QUESTIONS:
        return False, f'题库不能超过 {MAX_QUESTIONS} 题'
    if not out:
        return False, '题库不能为空（每题需题干+参考答案）'
    _ensure_meta(pack_id)
    _put_section(pack_id, 'questions', out)
    ok, err = _commit('题库')
    if not ok:
        return False, err
    _invalidate()
    return True, f'题库已保存（{len(out)} 题）'


def save_scenarios(pack_id, form):
    """保存情景剧本（表单：scenarios__type/label/role/persona/goal/lines）。"""
    types = form.getlist('scenarios__type')
    labels = form.getlist('scenarios__label')
    roles = form.getlist('scenarios__role')
    personas = form.getlist('scenarios__persona')
    goals = form.getlist('scenarios__goal')
    lines = form.getlist('scenarios__lines')
    pool, role_map, label_map = {}, {}, {}
    for i, raw_t in enumerate(types):
        t = raw_t.strip()[:30]
        if not t or not SCENARIO_TYPE_RE.match(t) or t in pool:
            continue
        # P7-E / S-9：情景「个数」此前无上限（只有单情景话术行数有上限）
        if len(pool) >= MAX_SCENARIOS:
            break
        ls = [x.strip()[:200] for x in (lines[i] if i < len(lines) else '').splitlines()
              if x.strip()][:MAX_SCENARIO_LINES]
        if not ls:
            continue
        pool[t] = ls
        role_map[t] = [
            (roles[i].strip()[:50] if i < len(roles) else '') or '顾客',
            (personas[i].strip()[:200] if i < len(personas) else ''),
            (goals[i].strip()[:200] if i < len(goals) else ''),
        ]
        label_map[t] = (labels[i].strip()[:30] if i < len(labels) else '') or t
    if not pool:
        return False, '至少保留一个有效情景（类型用小写字母/下划线，话术非空）'
    default = (form.get('scenarios__default') or '').strip()
    if default not in pool:
        default = next(iter(pool))
    _ensure_meta(pack_id)
    _put_section(pack_id, 'scenarios',
                 {'pool': pool, 'roles': role_map, 'labels': label_map,
                  'default': default})
    ok, err = _commit('情景剧本')
    if not ok:
        return False, err
    _invalidate()
    return True, f'情景剧本已保存（{len(pool)} 个情景）'


def save_products(pack_id, form):
    """保存在售产品（表单：products__name/price/cost 多值行）。"""
    names = form.getlist('products__name')
    prices = form.getlist('products__price')
    costs = form.getlist('products__cost')
    out = []
    for i, raw_n in enumerate(names):
        n = raw_n.strip()[:100]
        if not n:
            continue
        try:
            # P7-E / S-8：单价/成本改走 validators（NaN / Inf / 负数 / 超上限全部拒绝），
            # 旧实现 `round(float(raw), 2)` 只捕 ValueError，'nan'/'inf' 能直接入库污染毛利。
            price = _price(prices[i] if i < len(prices) else '', '单价')
            cost = _price(costs[i] if i < len(costs) else '', '成本价')
        except ParseError as exc:
            return False, str(exc)
        out.append({
            'product_name': n,
            'unit_price': price,
            'cost_price': cost,
            'category': '',
        })
    if not out:
        return False, '产品列表不能为空'
    # P7-E / S-9：产品条数此前无上限
    if len(out) > MAX_PRODUCTS:
        return False, f'产品不能超过 {MAX_PRODUCTS} 个'
    _ensure_meta(pack_id)
    _put_section(pack_id, 'products', out)
    ok, err = _commit('产品列表')
    if not ok:
        return False, err
    _invalidate()
    return True, f'产品列表已保存（{len(out)} 个）'


def save_metrics(pack_id, form):
    """保存指标定义（metrics__key/label/value_type/unit/role/agg/required/sort/enabled + 删除标记）。

    骨架指标（role ∈ dimension/quantity/amount）不可删除；
    非法 key/类型/聚合/角色 → 拒绝保存（不入库）。
    """
    # 注意：不过滤空键行（各字段 getlist 索引必须对齐），循环内跳过空键
    keys = form.getlist('metrics__key')
    existing = {m.key: m for m in MetricDef.query.filter_by(pack_id=pack_id).all()}
    # P7-E / S-9：旧口径只校验「本次提交的去重键数」，未计入 DB 存量 → 可分批绕过上限
    submitted = {k.strip() for k in keys if k.strip()}
    if len(set(existing) | submitted) > industry_pack.MAX_METRICS:
        return False, (f'指标不能超过 {industry_pack.MAX_METRICS} 个'
                       f'（已有 {len(existing)} 个，本次提交 {len(submitted)} 个）')
    skeleton_keys = {m.key for m in existing.values() if m.role in SKELETON_ROLES}

    to_delete = {k.strip() for k in form.getlist('metrics__delete') if k.strip()}
    removable = to_delete & set(existing) - skeleton_keys
    blocked_delete = to_delete & skeleton_keys
    if blocked_delete:
        db.session.rollback()
        return False, f'骨架指标不可删除：{"、".join(sorted(blocked_delete))}'

    seen = set()
    for i, raw_key in enumerate(keys):
        key = raw_key.strip()
        if not key:
            continue  # 空键行（新增未填）跳过
        if not PACK_ID_RE.match(key):
            return False, f'指标键「{key}」非法（小写字母开头的字母/数字/下划线）'
        if key in seen:
            continue
        seen.add(key)
        try:
            _apply_metric_row(existing, pack_id, key, i, form)
        except ValueError as exc:
            db.session.rollback()
            return False, str(exc)
    for key in removable:
        db.session.delete(existing[key])
    _ensure_meta(pack_id)
    ok, err = _commit('指标定义')
    if not ok:
        return False, err
    _invalidate()
    return True, f'指标定义已保存（{len(seen)} 项）'


def _apply_metric_row(existing, pack_id, key, i, form):
    """单行指标 → MetricDef（新建或更新）；枚举/映射列名非法抛 ValueError。"""
    label = _field(form, i, 'metrics__label') or key
    vtype = _field(form, i, 'metrics__value_type') or 'number'
    agg = _field(form, i, 'metrics__agg') or 'sum'
    role = _field(form, i, 'metrics__role') or 'other'
    if vtype not in VALID_VALUE_TYPES or agg not in VALID_AGGS or role not in VALID_ROLES:
        raise ValueError(f'指标「{key}」的类型/聚合/角色取值非法')
    legacy = _field(form, i, 'metrics__legacy_column')
    if legacy and legacy not in industry_pack.LEGACY_COLUMN_SET:
        raise ValueError(f'指标「{key}」的映射列名非法：{legacy}')
    m = existing.get(key)
    if m is None:
        m = MetricDef(pack_id=pack_id, key=key)
        db.session.add(m)
    m.label = label[:50]
    m.value_type = vtype
    m.unit = _field(form, i, 'metrics__unit')[:20]
    m.role = role
    m.agg = agg
    m.required = _field(form, i, 'metrics__required') == '1'
    m.enabled = _field(form, i, 'metrics__enabled') != '0'
    try:
        m.sort = int(_field(form, i, 'metrics__sort') or 0)
    except ValueError:
        m.sort = 0
    m.options_json = json.dumps({'legacy_column': legacy}) if legacy else None


def save_formula_params(pack_id, form):
    """保存公式参数（损耗率阈值等）。"""
    current = dict(industry_pack.get_pack(pack_id)['formula_params'])
    raw = form.get('formula_params__waste_rate_threshold')
    if raw is not None and raw.strip():
        try:
            val = float(raw)
        except ValueError:
            return False, '损耗率阈值必须是数字（如 0.15）'
        if not 0 < val < 1:
            return False, '损耗率阈值应在 0~1 之间（0.15 = 15%）'
        current['waste_rate_threshold'] = val
    _ensure_meta(pack_id)
    _put_section(pack_id, 'formula_params', current)
    ok, err = _commit('公式参数')
    if not ok:
        return False, err
    _invalidate()
    return True, '公式参数已保存'


# ─────────────────────────── 编辑器视图数据 ───────────────────────────
def pack_detail(pack_id):
    """包内容编辑器所需的全量视图数据（按段展开为表单行）。"""
    pack = industry_pack.get_pack(pack_id)
    meta = _pack_meta_row(pack_id)
    scenarios = pack.get('scenarios') or _file_section_value(pack_id, 'scenarios') or {}
    file_kb = industry_pack.file_knowledge(pack_id)
    db_kb = pack.get('knowledge') or {}
    knowledge = {d['name']: db_kb.get(d['name'], file_kb.get(d['name'], ''))
                 for d in pack['domains']}
    return {
        'id': pack_id,
        'name': (meta.name if meta else pack['name']),
        'description': (meta.description if meta else pack.get('description', '')),
        'source': pack['source'],
        'active': pack_id == active_pack_id(),
        'terms': pack['terms'],
        'domains': pack['domains'],
        'weak_keywords': pack['weak_keywords'],
        'synonyms': pack['synonyms'],
        'knowledge': knowledge,
        'questions': pack.get('questions') or _file_section_value(pack_id, 'questions') or [],
        'scenarios': scenarios if isinstance(scenarios, dict) else {},
        'products': pack.get('products') or _file_section_value(pack_id, 'products') or [],
        'metrics': [{
            'key': m.key, 'label': m.label, 'value_type': m.value_type,
            'unit': m.unit, 'role': m.role, 'agg': m.agg,
            'required': bool(m.required), 'enabled': bool(m.enabled),
            'sort': m.sort, 'legacy_column': _legacy_column_of(m),
        } for m in MetricDef.query.filter_by(pack_id=pack_id).order_by(
            MetricDef.sort, MetricDef.key).all()],
        'formula_params': pack['formula_params'],
        'skeleton_roles': SKELETON_ROLES,
        'valid_value_types': VALID_VALUE_TYPES,
        'valid_aggs': VALID_AGGS,
        'valid_roles': VALID_ROLES,
        'legacy_columns': list(industry_pack.LEGACY_COLUMN_SET),
        'max_domains': industry_pack.MAX_DOMAINS,
        'max_metrics': industry_pack.MAX_METRICS,
    }


# ─────────────────────────── 文件段读取工具 ───────────────────────────
def _file_section_value(pack_id, section):
    """读文件包某段的原始 JSON 值（questions/scenarios/products；缺失 None）。"""
    name = {'questions': 'questions.json', 'scenarios': 'scenarios.json',
            'products': 'products.json'}[section]
    return industry_pack.load_pack_file_json(pack_id, name)


def _parse_synonyms(form):
    """同义词对解析：textarea 每行一对「口语,惯用词」，上限 MAX_SYNONYMS。

    P7-E / S-9：该段此前无条数上限（弱项词有 [:60]，同义词没有）。
    """
    out = []
    for raw in form.getlist('synonyms'):
        for row in str(raw).splitlines():
            parts = [p.strip()[:30] for p in row.split(',') if p.strip()]
            if len(parts) == 2:
                out.append(parts)
    return out[:MAX_SYNONYMS]


def _field(form, index, name):
    """取多值字段第 index 项（越界返回空串）。"""
    vals = form.getlist(name)
    return (vals[index].strip() if index < len(vals) else '')


def _price(raw, label):
    """产品价格解析（P7-E / S-8）：空 → None，其余一律走 validators 强校验。

    与管理员产品页（`routes/admin.py` 的 parse_float + check_range）同口径：
    排除 NaN / ±Inf（float() 能通过但会污染毛利与营收聚合）、负数与超上限值。

    Args:
        raw: 表单原始值。
        label: 字段中文名（单价 / 成本价）。

    Returns:
        float | None: 合法值（保留 2 位）；空值返回 None。

    Raises:
        ParseError: 值非法 / 非有限数 / 越界。
    """
    if raw is None or not str(raw).strip():
        return None
    value = parse_float(raw, label, allow_empty=False)
    err = check_range(value, label, MAX_UNIT_PRICE, minimum=0)
    if err:
        raise ParseError(err)
    return round(value, 2)
