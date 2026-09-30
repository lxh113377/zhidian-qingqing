"""行业包引擎（P1）—— 行业内容与词表的唯一真相源。

三级回退（DB 覆盖 → 文件包 → 内置兜底）：
  1. **DB 覆盖层**：后台 DIY 内容，P4 起经 `register_db_override()` 注入；
     未注册 / 返回 None 时静默跳过（当前形态不改变既有行为）。
  2. **文件包**：`src/industry_packs/<pack_id>/`，随代码交付。`pack.json` 声明
     域表 / 词表 / 指标定义，内容文件由 `files` 段指向（questions/scenarios/products）。
  3. **内置兜底**：`FALLBACK_PACK`，保证缺目录 / 坏 JSON / 非法 schema 时
     也不会把异常扩散到业务层 —— 对外接口恒返回可用 pack。

设计约定（与既有模块保持一致）：
  1. 只读文件与纯数据，不触碰网络与 DB（DB 覆盖经注册函数由外部注入）；
  2. 解析失败一律回退 + WARNING 日志，绝不抛异常；
  3. 对外 pack 字典字段固定（缺失自动补默认），消费方无需防御式取值；
  4. 解析结果按「包 id + 文件指纹」缓存，避免每请求重复读盘。
"""
import json
import logging
import os
import re
import threading
import time

from flask import has_request_context, request

logger = logging.getLogger(__name__)

# 包根目录（与 src 同级，随交付包一起分发）
PACK_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'industry_packs')
DEFAULT_PACK_ID = 'milk_tea'
# 行业包 schema 版本（唯一真相源；pack_contract / 导入导出共用，禁止别处硬编码）
PACK_SCHEMA_VERSION = 1
PACK_ID_ENV = 'INDUSTRY_PACK'
# 请求级激活包快照键（PF-1）：同一请求的判重、查询与落库必须复用首次取值，
# 防止切包请求在两次调用之间改变全局态，形成「判重按 A 包 / 落库按 B 包」。
# 存进 WSGI environ 而非 Flask g：g 属于 app context，测试客户端嵌套 app
# context 时可能跨请求复用；environ 每个请求对象唯一，语义最严格。
_REQUEST_PACK_ENV_KEY = 'ican.active_pack_id'

# 包标识与包内内容文件白名单（P7-E / S-15）：路径拼装只认白名单。
# 旧实现用「黑名单过滤 .. 与分隔符」的方式，在 Windows 绝对路径 / 盘符 / UNC 下
# 会整体失效（os.path.join 遇到绝对路径会丢弃前面的 PACK_ROOT）。
PACK_ID_RE = re.compile(r'^[a-z][a-z0-9_]{0,49}$')
ALLOWED_PACK_FILES = frozenset({'pack.json', 'questions.json', 'scenarios.json',
                                'products.json', 'manifest.json'})

# 上限防护（后台写入校验共用，P4 起正式消费）
MAX_DOMAINS = 12
MAX_METRICS = 24

# DailySales 既有 typed 列全集（P4 后台「指标映射列名」白名单；
# 与 metrics.LEGACY_COLUMNS 同源，定义在本模块避免 metrics→industry_pack 循环导入）
LEGACY_COLUMN_SET = frozenset((
    'product_name', 'product_sales_qty', 'total_revenue',
    'raw_material_remaining', 'raw_material_waste', 'abnormal_notes',
))

# 内联内容段（P4 起 DB 覆盖层可直接并入包字典，无需落盘文件）
_INLINE_SECTIONS = ('questions', 'scenarios', 'products', 'knowledge', 'negotiation')

# ── 内置兜底包：奶茶四域（与 knowledge_base 既有常量同源，去重前的最后防线）──
_FALLBACK_DOMAIN_SPECS = (
    ('原料规范', ('原料', '损耗', '保存'),
     '熟记各原料储存条件与效期，强化「损耗率超 15% 即异常」的排查意识'),
    ('异议处理', ('异议', '安全', '卫生'),
     '巩固「先处理情绪再处理事情」原则，重点练习价格异议与催单场景的应答'),
    ('套餐价格', ('价格', '套餐'),
     '牢记在售产品单价与套餐门槛，练习先报主价再带优惠增值选项的报价'),
    ('服务话术', ('话术', '服务'),
     '打磨迎客/点单/交付/送客全流程话术，坚持「出错先致歉再补救」原则'),
)

# ── 内置兜底指标段（骨架 6 条，与 milk_tea/pack.json 同构）──
# 缺失后果（P7 实测）：`FALLBACK_PACK['metrics']` 为空时，缺包/坏 JSON 场景下
# 指标定义解析为空 → H5 填报表单骨架整块消失（表单只剩自定义指标组）。
# 补齐后「兜底包 = 奶茶骨架」，任何环境表单/看板口径与默认包一致（零回归）。
_FALLBACK_METRIC_SPECS = (
    ('product_name', '产品', 'text', '', 'dimension', 'count', True, 1),
    ('product_sales_qty', '销量', 'number', '杯', 'quantity', 'sum', True, 2),
    ('total_revenue', '营收', 'number', '元', 'amount', 'sum', True, 3),
    ('raw_material_remaining', '原料剩余', 'number', '份', 'stock', 'sum', False, 4),
    ('raw_material_waste', '原料损耗', 'number', '份', 'waste', 'sum', False, 5),
    ('abnormal_notes', '异常备注', 'text', '', 'note', 'none', False, 6),
)

FALLBACK_PACK = {
    'id': DEFAULT_PACK_ID,
    'name': '奶茶店',
    'version': 'builtin-fallback',
    'source': 'builtin',
    'description': '内置兜底行业包（文件包缺失时使用）',
    'terms': {
        'record_dimension': '产品',
        'quantity_unit': '杯',
        'material_group': '原料情况',
        'material_unit': '份',
        'staff_title': '店员',
        'customer_title': '顾客',
        'assistant_persona': '你是奶茶店经营助手',
        'analyst_persona': '你是奶茶店经营分析师',
        'trainer_persona': '你是奶茶店培训教练',
        'scenario_customer': '奶茶店顾客',
        # M-9（P7-E3-B2）：模板快捷提问里的行业样例随包 —— 此前「杨枝甘露」等
        # 硬编码在 assistant / ai_trainer 两页，切包后仍显示奶茶样例。
        'sample_dimension': '杨枝甘露',
        'sample_shelf_question': '珍珠煮好后能放多久？',
    },
    'domains': [
        {'name': name, 'knowledge_file': f'{name}.md', 'tip': tip,
         'keywords': list(kws)}
        for name, kws, tip in _FALLBACK_DOMAIN_SPECS
    ],
    'weak_keywords': ['原料', '损耗', '保存', '价格', '话术', '服务',
                      '异议', '卫生', '安全'],
    'synonyms': [['省钱', '优惠'], ['保存', '储存'], ['效期', '保质期'],
                 ['应对', '话术'], ['价钱', '价格'], ['怎么', '如何']],
    'metrics': [
        {'key': key, 'label': label, 'value_type': vtype, 'unit': unit,
         'role': role, 'agg': agg, 'required': required, 'sort': sort,
         'legacy_column': key}
        for key, label, vtype, unit, role, agg, required, sort
        in _FALLBACK_METRIC_SPECS
    ],
    'formula_params': {'waste_rate_threshold': 0.15},
    'files': {},
}

# 弱项关键词兜底（P7-E3-B / M-1 单一真相源）：内置兜底包声明即唯一列表，
# `knowledge_base.WEAK_KEYWORDS` 与 `learner_model._FALLBACK_DOMAIN_KEYWORDS`
# 的历史字面量副本一律由此派生（此前 4 份各自维护，切包后极易漂移）。
FALLBACK_WEAK_KEYWORDS = tuple(FALLBACK_PACK['weak_keywords'])

# 损耗率预警阈值默认值（M-4：与兜底包 formula_params 同源，
# `analytics.WASTE_RATE_THRESHOLD` 由此派生，避免两处默认值）。
DEFAULT_WASTE_RATE_THRESHOLD = FALLBACK_PACK['formula_params']['waste_rate_threshold']

# ── 缓存与 DB 覆盖钩子（用可变容器承载状态，避免重新绑定模块级名字）──
# PF-8/PF-15：包缓存按 pack_id 多槽保存，避免多包轮换时 100% miss；
# 文件指纹做短 TTL 缓存，避免每次 get_pack 都 os.walk 整棵包目录。
_CACHE = {'packs': {}}
_FINGERPRINT_CACHE = {'items': {}}
FINGERPRINT_TTL_SECONDS = 2.0
_LOCK = threading.RLock()
_HOOKS = {'db_override': None, 'active_pack_id': None, 'revision': 0}

# 覆盖层最近一次「读取失败」记录（P7-E3 / PF-9）。
# 背景：DB 抖动或表未建时覆盖层静默返回 None，管理员刚保存的词表/题库会全部无声
# 回退到文件包而 UI 零提示（保存不生效却以为生效）。这里只做「可见性」，
# 不改变任何回退行为——故障态下依旧保证对外返回可用 pack。
_OVERRIDE_ERROR = {'pack_id': None, 'message': None}


def note_override_error(pack_id, exc):
    """登记覆盖层读取失败（供后台顶部告警）。异常文本脱敏为「类型 + 摘要」。"""
    with _LOCK:
        _OVERRIDE_ERROR['pack_id'] = pack_id
        _OVERRIDE_ERROR['message'] = f'{type(exc).__name__}: {str(exc)[:120]}'


def clear_override_error(pack_id):
    """覆盖层读取成功：清掉同包的失败态（避免旧告警长期挂着误导）。"""
    with _LOCK:
        if _OVERRIDE_ERROR['pack_id'] == pack_id:
            _OVERRIDE_ERROR['pack_id'] = None
            _OVERRIDE_ERROR['message'] = None


def override_error():
    """覆盖层最近一次失败（PF-9 可见性）。

    Returns:
        dict | None: `{'pack_id','message'}`；无失败态返回 None。
    """
    with _LOCK:
        if not _OVERRIDE_ERROR['pack_id']:
            return None
        return dict(_OVERRIDE_ERROR)


def register_db_override(fn):
    """注册 DB 覆盖层取数函数（P4 配置后台在 app 初始化时调用）。

    Args:
        fn: `fn(pack_id) -> dict | None`；返回 None 视为该包无 DB 覆盖。
    """
    with _LOCK:
        _HOOKS['db_override'] = fn
    invalidate()


def set_active_pack(pack_id):
    """会话级/进程级激活行业包（P4 起改由 DB 持久化激活态）。

    Args:
        pack_id: 行业包 id；非字符串或空值视为回退默认包。

    Returns:
        str: 实际生效的包 id（不可解析时经 `get_pack()` 回退为默认包）。
    """
    normalized = pack_id if isinstance(pack_id, str) and pack_id else None
    with _LOCK:
        _HOOKS['active_pack_id'] = normalized
    if has_request_context():
        # 激活动作自身发生在请求内：清掉旧快照，由随后的 current_pack()
        # 用新全局态重新取一次；后续调用继续复用该请求的新快照。
        request.environ.pop(_REQUEST_PACK_ENV_KEY, None)
    invalidate()
    return current_pack()['id']


def invalidate():
    """清空解析缓存（内容/激活态变更后调用）。

    revision 参与缓存键：DB 覆盖层内容更新后只需调用本函数即可失效，
    无需重新注册取数函数。
    """
    with _LOCK:
        _HOOKS['revision'] += 1
        _CACHE['packs'].clear()
        _FINGERPRINT_CACHE['items'].clear()


# ─────────────────────────── 加载与规范化 ───────────────────────────
def _pack_dir(pack_id):
    """包目录（P7-E / S-15）：白名单解析 —— 非法 / 越界一律返回 None。

    旧实现 `os.path.join(PACK_ROOT, pack_id)` 对 Windows 绝对路径（`C:\\x`）、盘符、
    UNC 与 `..` 均无防护：`os.path.join` 遇到绝对路径会直接丢弃 PACK_ROOT。
    现改为「标识必须命中 PACK_ID_RE + realpath 后必须仍在 PACK_ROOT 内」双保险。

    Returns:
        str | None: 合法包目录绝对路径；非法返回 None（调用方按缺失处理）。
    """
    if not isinstance(pack_id, str) or not PACK_ID_RE.match(pack_id):
        logger.warning('[industry_pack] 包标识非法: %r', pack_id)
        return None
    root = os.path.realpath(PACK_ROOT)
    path = os.path.realpath(os.path.join(root, pack_id))
    if os.path.commonpath([path, root]) != root or path == root:
        logger.warning('[industry_pack] 包目录越界被拒: %r', pack_id)
        return None
    return path


def pack_dir(pack_id):
    """包目录公开入口（同 `_pack_dir`）：供 pack_manifest 等兄弟模块使用，
    避免跨模块调用私有函数（与 M-18「轨迹层不再调私有函数」同一口径）。"""
    return _pack_dir(pack_id)


def _fingerprint(pack_id):
    """包目录文件指纹（相对路径 + mtime_ns + size），用于缓存失效判定。"""
    now = time.monotonic()
    with _LOCK:
        hit = _FINGERPRINT_CACHE['items'].get(pack_id)
        if hit and hit[0] > now:
            return hit[1]
    base = _pack_dir(pack_id)
    items = []
    if not base:
        out = ()
        with _LOCK:
            _FINGERPRINT_CACHE['items'][pack_id] = (
                now + FINGERPRINT_TTL_SECONDS, out)
        return out
    for root, _dirs, files in os.walk(base):
        for fn in files:
            path = os.path.join(root, fn)
            try:
                st = os.stat(path)
            except OSError:
                continue
            items.append((os.path.relpath(path, base), st.st_mtime_ns, st.st_size))
    out = tuple(sorted(items))
    with _LOCK:
        _FINGERPRINT_CACHE['items'][pack_id] = (
            now + FINGERPRINT_TTL_SECONDS, out)
    return out


def _read_json(path):
    """读取 JSON；缺失/损坏返回 None（不抛异常）。"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning('[industry_pack] 读取失败 %s: %s', path, exc)
        return None


def _as_str_list(value, limit=0):
    """把任意值规范为字符串列表（去空、去重保序、可选限长）。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip() and item not in out:
            out.append(item.strip())
        if limit and len(out) >= limit:
            break
    return out


def _normalize_terms(raw):
    """规范化 terms 段：仅保留字符串键值。"""
    if not isinstance(raw, dict):
        return dict(FALLBACK_PACK['terms'])
    out = {k: v.strip() for k, v in raw.items()
           if isinstance(k, str) and isinstance(v, str) and v.strip()}
    return out or dict(FALLBACK_PACK['terms'])


def _normalize_domains(raw):
    """规范化域表：name 必需；keywords 缺失则该域永不命中归因（保留但告警）。"""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_DOMAINS]:
        if not isinstance(item, dict):
            continue
        name = item.get('name')
        if not isinstance(name, str) or not name.strip():
            continue
        keywords = _as_str_list(item.get('keywords'))
        if not keywords:
            logger.warning('[industry_pack] 域「%s」未声明 keywords，归因将不命中', name)
        out.append({
            'name': name.strip(),
            'knowledge_file': (item.get('knowledge_file') or '').strip(),
            'tip': (item.get('tip') or '').strip(),
            'keywords': keywords,
        })
    return out


def _normalize_metrics(raw):
    """规范化指标定义（P2 起正式消费；此处保证结构可用）。"""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_METRICS]:
        if not isinstance(item, dict):
            continue
        key = item.get('key')
        if not isinstance(key, str) or not key.strip():
            continue
        try:
            _sort = int(item.get('sort') or 0)
        except (TypeError, ValueError):
            # Q4：手工编辑的文件包把 sort 写成非数字时回退 0，不让整包加载失败
            # （Web 保存路径 pack_store._apply_metric_row 已有同语义保护）。
            _sort = 0
        out.append({
            'key': key.strip(),
            'label': (item.get('label') or key).strip(),
            'value_type': (item.get('value_type') or 'number').strip(),
            'unit': (item.get('unit') or '').strip(),
            'role': (item.get('role') or 'other').strip(),
            'agg': (item.get('agg') or 'sum').strip(),
            'required': bool(item.get('required')),
            'sort': _sort,
            'legacy_column': (item.get('legacy_column') or '').strip(),
        })
    out.sort(key=lambda d: (d['sort'], d['key']))
    return out


def _normalize_synonyms(raw):
    """规范化同义词对（仅保留 [str, str] 二元组）。"""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if (isinstance(item, (list, tuple)) and len(item) == 2
                and all(isinstance(x, str) and x for x in item)):
            out.append((item[0], item[1]))
    return out


def _normalize_pack(raw, pack_id):
    """把原始包字典规范为固定 schema；关键段非法时回退内置兜底。"""
    if not isinstance(raw, dict):
        raw = {}
    domains = _normalize_domains(raw.get('domains'))
    if not domains:
        logger.warning('[industry_pack] 包 %s 域表为空，回退内置域表', pack_id)
        domains = _normalize_domains(FALLBACK_PACK['domains'])
    # 指标定义同理回退（P7）：包未声明任何指标 → 内置骨架，保证 H5 填报表单
    # 恒有骨架字段（否则空表单：连产品/销量都无法提交，属功能级退化）
    metrics = _normalize_metrics(raw.get('metrics'))
    if not metrics:
        logger.warning('[industry_pack] 包 %s 指标定义为空，回退内置骨架指标', pack_id)
        metrics = _normalize_metrics(FALLBACK_PACK['metrics'])
    weak_keywords = _as_str_list(raw.get('weak_keywords'))
    if not weak_keywords:
        weak_keywords = [kw for d in domains for kw in d['keywords']]
    formula_params = raw.get('formula_params')
    if not isinstance(formula_params, dict):
        formula_params = dict(FALLBACK_PACK['formula_params'])
    files = raw.get('files')
    if not isinstance(files, dict):
        files = {}
    pack = {
        'id': (raw.get('id') or pack_id or DEFAULT_PACK_ID),
        'schema_version': (
            raw.get('schema_version')
            if isinstance(raw.get('schema_version'), int) and raw.get('schema_version') >= 1
            else PACK_SCHEMA_VERSION
        ),
        'name': (raw.get('name') or raw.get('id') or pack_id or DEFAULT_PACK_ID),
        'version': str(raw.get('version') or ''),
        'source': raw.get('source') or 'file',
        'description': raw.get('description') or '',
        'terms': _normalize_terms(raw.get('terms')),
        'domains': domains,
        'weak_keywords': weak_keywords,
        'synonyms': _normalize_synonyms(raw.get('synonyms')),
        'metrics': metrics,
        'formula_params': formula_params,
        'files': files,
    }
    # 内联内容段透传（P4 DB 覆盖层；消费方优先读内联，缺失回落文件）
    for section in _INLINE_SECTIONS:
        if raw.get(section) is not None:
            pack[section] = raw[section]
    # 清单声明（H3）：DB 覆盖层携带优先（导入包随载荷进来），否则读文件包
    # manifest.json；两处都没有 → None，由 pack_manifest 合成为「与当前平台兼容」，
    # 历史包零行为变化。归一化与判定统一在 pack_manifest，本模块只管搬运。
    declared = raw.get('manifest')
    if not isinstance(declared, dict):
        from . import pack_manifest
        declared = pack_manifest.read_declared(pack['id'])
    pack['manifest'] = declared or None
    return pack


def _load_file_pack(pack_id):
    """读取文件包（pack.json 不在/损坏 → None）。"""
    base = _pack_dir(pack_id)
    if not base:
        return None
    raw = _read_json(os.path.join(base, 'pack.json'))
    if raw is None:
        return None
    raw.setdefault('source', 'file')
    return _normalize_pack(raw, pack_id)


def load_file_pack(pack_id):
    """文件包读取的公开入口（P4 覆盖层合并的基底）。"""
    return _load_file_pack(pack_id)


def load_pack_file_json(pack_id, filename):
    """读取包目录内指定 JSON 文件（缺失/损坏 → None；路径限定包目录内）。"""
    if filename not in ALLOWED_PACK_FILES:
        logger.warning('[industry_pack] 包内文件名不在白名单: %r', filename)
        return None
    base = _pack_dir(pack_id)
    if not base:
        return None
    return _read_json(os.path.join(base, filename))


def file_knowledge(pack_id):
    """文件包 knowledge/*.md 全文 {域名: md 文本}（目录缺失返回空 dict）。"""
    out = {}
    base = _pack_dir(pack_id)
    if not base:
        return out
    kdir = os.path.join(base, 'knowledge')
    if not os.path.isdir(kdir):
        return out
    for fn in sorted(os.listdir(kdir)):
        if not fn.endswith('.md'):
            continue
        try:
            with open(os.path.join(kdir, fn), encoding='utf-8') as f:
                out[fn[:-3]] = f.read()
        except OSError:
            continue
    return out


def _builtin_pack(pack_id):
    """内置兜底包（复用奶茶域表，保证对外接口恒返回可用结构）。"""
    fallback = dict(FALLBACK_PACK)
    fallback['id'] = pack_id or DEFAULT_PACK_ID
    return _normalize_pack(fallback, fallback['id'])


def _load_pack(pack_id):
    """取包：DB 覆盖 → 文件包；两者均不可得返回 None（由 get_pack 决定最终兜底）。"""
    override = _HOOKS['db_override']
    if override is not None:
        try:
            raw = override(pack_id)
        except Exception as exc:  # 覆盖层异常不得扩散到业务层
            logger.warning('[industry_pack] DB 覆盖层异常 %s: %s', pack_id, exc)
            note_override_error(pack_id, exc)
            raw = None
        else:
            # 调用成功即视为覆盖层可用（返回 None 只是「该包无 DB 痕迹」），清旧告警
            clear_override_error(pack_id)
        if raw:
            raw.setdefault('source', 'db')
            return _normalize_pack(raw, pack_id)
    return _load_file_pack(pack_id)


def list_pack_ids():
    """列出所有内置文件包 id（目录下含 pack.json 的子目录）。"""
    try:
        names = sorted(os.listdir(PACK_ROOT))
    except OSError:
        return []
    return [n for n in names
            if os.path.isfile(os.path.join(PACK_ROOT, n, 'pack.json'))]


def list_packs():
    """列出全部可用包摘要（供后台包管理页展示）。"""
    ids = list_pack_ids() or [DEFAULT_PACK_ID]
    out = []
    for pid in ids:
        pack = get_pack(pid)
        out.append({
            'id': pack['id'],
            'name': pack['name'],
            'version': pack['version'],
            'source': pack['source'],
            'domain_count': len(pack['domains']),
            'metric_count': len(pack['metrics']),
            'active': pid == active_pack_id(),
        })
    return out


def active_pack_id():
    """当前激活包 id：内存激活态 → 环境变量 → 默认包。

    PF-1：请求上下文内首次调用即建立快照，同一请求后续调用不再读取全局态；
    判重、产品白名单与落库因此共享同一包 id。请求外保持原有全局语义。

    不在此处做可用性预过滤（否则「仅存在于 DB 覆盖层的包」不可达，
    P4 后台新建包会失效）；不可解析时由 `get_pack()` 统一回退默认包。

    PH2-6：**门店级覆盖**优先 —— 当前门店显式设置了 `active_pack_id` 时用它，
    实现「一店一当前包」；未设置则回落全局激活态（存量单店账号零变化）。
    """
    in_request = has_request_context()
    if in_request:
        cached = request.environ.get(_REQUEST_PACK_ENV_KEY)
        if isinstance(cached, str) and cached:
            return cached
        # 门店级覆盖（仅请求内生效；查询失败不影响全局回落链）
        try:
            from src import tenant
            store = tenant.current_store()
            store_pack = getattr(store, 'active_pack_id', None) if store else None
            if isinstance(store_pack, str) and store_pack:
                request.environ[_REQUEST_PACK_ENV_KEY] = store_pack
                return store_pack
        except Exception:  # noqa: S110  理由：DB 覆盖层异常必须回落到文件包而不是打断请求（P7-E3-A 设计），上一分支已 return store_pack
            pass
    with _LOCK:
        configured = _HOOKS['active_pack_id']
    for cid in (configured, os.environ.get(PACK_ID_ENV)):
        if isinstance(cid, str) and cid:
            pid = cid
            break
    else:
        pid = DEFAULT_PACK_ID
    if in_request:
        request.environ[_REQUEST_PACK_ENV_KEY] = pid
    return pid


def get_pack(pack_id=None):
    """取指定包（默认当前激活包）；按「id + 文件指纹 + 覆盖层 revision」缓存。

    不可解析的包回退默认包；连默认包都不可得时用内置兜底，
    保证任何环境下 `current_pack()` 恒返回可用结构。
    """
    pid = pack_id or active_pack_id()
    for _attempt in range(3):
        with _LOCK:
            key = (pid, _fingerprint(pid), _HOOKS['revision'])
            hit = _CACHE['packs'].get(pid)
            if hit and hit[0] == key:
                return hit[1]

        loaded_pid = pid
        pack = _load_pack(loaded_pid)
        if pack is None and loaded_pid != DEFAULT_PACK_ID:
            logger.warning('[industry_pack] 包 %s 不可解析，回退默认包 %s',
                           loaded_pid, DEFAULT_PACK_ID)
            loaded_pid = DEFAULT_PACK_ID
            pack = _load_pack(loaded_pid)
        if pack is None:
            pack = _builtin_pack(loaded_pid)

        with _LOCK:
            latest = (loaded_pid, _fingerprint(loaded_pid), _HOOKS['revision'])
            hit = _CACHE['packs'].get(loaded_pid)
            if hit and hit[0] == latest:
                return hit[1]
            if loaded_pid == pid and latest == key:
                _CACHE['packs'][loaded_pid] = (latest, pack)
                return pack
            # 加载期间 revision/文件指纹变化：不写陈旧缓存，重读一轮。
            pid = loaded_pid
    return pack


def current_pack():
    """当前激活行业包（唯一真相源入口）。"""
    return get_pack()


# ─────────────────────────── 只读访问器 ───────────────────────────
def pack_terms():
    """行业术语表（表单标签 / 文案参数化用）。"""
    return current_pack()['terms']


def pack_domains():
    """知识域名称列表（顺序即能力雷达轴顺序）。"""
    return [d['name'] for d in current_pack()['domains']]


def pack_domain_defs():
    """知识域定义列表（含 knowledge_file / tip / keywords）。"""
    return current_pack()['domains']


def pack_domain_tips():
    """域 → 复习建议（仅保留非空 tip）。"""
    return {d['name']: d['tip'] for d in current_pack()['domains'] if d['tip']}


def pack_domain_map():
    """弱项关键词 → 域（按 weak_keywords 顺序构造，先命中即归因）。"""
    mapping = {}
    pack = current_pack()
    by_kw = {}
    for d in pack['domains']:
        for kw in d['keywords']:
            by_kw.setdefault(kw, d['name'])
    for kw in pack['weak_keywords'] + list(by_kw):
        if kw in by_kw and kw not in mapping:
            mapping[kw] = by_kw[kw]
    return mapping


def pack_weak_keywords():
    """弱项关键词有序元组（顺序即归因优先级）。"""
    return tuple(current_pack()['weak_keywords'])


def pack_synonyms():
    """检索同义词对列表 [[口语, 知识库惯用词], ...]。"""
    return list(current_pack()['synonyms'])


def pack_metrics():
    """指标定义列表（P2 起消费）。"""
    return current_pack()['metrics']


def pack_formula_params():
    """派生指标公式参数（如 waste_rate_threshold）。"""
    return current_pack()['formula_params']


def pack_waste_rate_threshold():
    """当前包损耗率预警阈值（0~1 之间的 float）。未声明/非法一律回退内置默认。

    P7-E3-B / M-4：看板判定（`analytics._waste_threshold`）与 AI 规则文案
    （`dify_client` / `ai_explain`）共用本入口，杜绝「看板 8% / AI 说 15%」漂移。
    """
    try:
        value = float(pack_formula_params().get(
            'waste_rate_threshold', DEFAULT_WASTE_RATE_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_WASTE_RATE_THRESHOLD
    return value if value > 0 else DEFAULT_WASTE_RATE_THRESHOLD


def pack_negotiation():
    """谈判引擎场景参数段（P5；可选）。

    结构为 ``{场景名: [标的价倍率, 预算下界, 心理价底线, 预算上界, 初始耐心]}``；
    未定义或结构非法时返回空 dict，由 `negotiation` 模块回退内置常量。
    """
    val = current_pack().get('negotiation')
    return dict(val) if isinstance(val, dict) else {}


def pack_knowledge_dir():
    """当前包知识库目录（不存在返回 None，调用方自行回退）。"""
    base = _pack_dir(current_pack()['id'])
    if not base:
        return None
    path = os.path.join(base, 'knowledge')
    return path if os.path.isdir(path) else None


def pack_knowledge_inline():
    """当前包内联知识条目 {域名: md 全文}（P4 DB 覆盖层；无覆盖返回空 dict）。"""
    val = current_pack().get('knowledge')
    return dict(val) if isinstance(val, dict) else {}


def _load_pack_json_file(filename):
    """加载包内 JSON 内容文件（按 files 段解析，缺失/损坏 → None）。"""
    pack = current_pack()
    name = (pack['files'] or {}).get(filename) or f'{filename}.json'
    if not isinstance(name, str) or not name:
        return None
    # P7-E / S-15：文件名只取 basename 并断言仍在包目录内（包内容可被后台改写）
    name = os.path.basename(name)
    base = _pack_dir(pack['id'])
    if not base or not name:
        return None
    path = os.path.realpath(os.path.join(base, name))
    if os.path.commonpath([path, base]) != base:
        logger.warning('[industry_pack] 包内容文件越界被拒: %r', name)
        return None
    return _read_json(path)


def pack_questions():
    """题库（[{q, a, weak}]）；缺失返回空列表（调用方回退内嵌题库）。

    优先级：DB 覆盖层内联段 → 文件包 questions.json。
    """
    inline = current_pack().get('questions')
    data = inline if isinstance(inline, list) else _load_pack_json_file('questions')
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get('q') and item.get('a'):
            out.append({'q': item['q'], 'a': item['a'],
                        'weak': item.get('weak') or ''})
    return out


def pack_scenarios():
    """情景剧本（{pool, roles, labels, default}）；缺失返回空字典。

    优先级：DB 覆盖层内联段 → 文件包 scenarios.json。
    """
    inline = current_pack().get('scenarios')
    data = inline if isinstance(inline, dict) else _load_pack_json_file('scenarios')
    if not isinstance(data, dict):
        return {}
    pool = data.get('pool')
    if not isinstance(pool, dict):
        return {}
    return {
        'pool': pool,
        'roles': data.get('roles') if isinstance(data.get('roles'), dict) else {},
        'labels': data.get('labels') if isinstance(data.get('labels'), dict) else {},
        'default': data.get('default') or '',
    }


def _products_from_pack(pack):
    """从包字典解析产品模板（内联段优先，其次 files 段指向的 products.json）。

    Args:
        pack: 已规范化的包字典（`get_pack()` 产物）。

    Returns:
        list[dict]: 合法产品行（缺 product_name 的行丢弃）。
    """
    inline = pack.get('products')
    if isinstance(inline, list):
        data = inline
    else:
        name = (pack.get('files') or {}).get('products') or 'products.json'
        base = _pack_dir(pack['id'])
        data = (_read_json(os.path.join(base, os.path.basename(name)))
                if base and isinstance(name, str) and name else None)
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get('product_name'):
            out.append({
                'product_name': item['product_name'],
                'unit_price': item.get('unit_price'),
                'cost_price': item.get('cost_price'),
                'category': item.get('category') or '',
            })
    return out


def pack_products_of(pack_id):
    """指定包的在售产品模板（P7-B2：init_db 按包播种 / 后台展示用）。

    不可解析的包经 `get_pack()` 回退默认包，故调用方应按
    `list_pack_ids()` 的 id 逐个取用。
    """
    return _products_from_pack(get_pack(pack_id))


def pack_products():
    """当前包在售产品模板（[{product_name, unit_price, cost_price, category}]）。

    优先级：DB 覆盖层内联段 → 文件包 products.json。
    """
    return _products_from_pack(current_pack())
