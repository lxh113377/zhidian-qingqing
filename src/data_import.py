"""数据批量导入（H5 对标改进：让已有收银系统数据的商家一次性迁移流水）。

设计要点（2026-09-24）：
  - **纯逻辑模块**：解析 / 校验 / 报告 / 落库全部可脱离 HTTP 直测（L4 复用）。
  - **零新增依赖**：CSV 用 stdlib csv（utf-8-sig / gbk 自动嗅探）；xlsx 无
    openpyxl 时明确提示「另存为 CSV」，不做静默降级。
  - **无状态两段式**：preview 与 commit 都从同一上传文件重新解析
    （session 仅 3500B 预算，不缓存预览），commit 幂等可重放。
  - **行校验顺序复用 h5.submit**（维度非空 → 数值解析 → 符号与上限 →
    产品白名单 → 查重），保证导入行与店员手工填报行受同一套约束。
  - **100% 回执**：任何一行要么入库、要么标记 duplicate（幂等跳过）、
    要么进入 errors（行号 + 字段 + 原因），不存在静默丢弃。
  - **租户隔离**：产品白名单与店员解析都限定在所选门店（manager 仅
    授权门店）；pack_id 取该门店激活包（store.active_pack_id 回退全局），
    与「一店一当前包」语义一致。

安全边界：文件 ≤2MB / ≤5000 行 / 日期不得晚于今天、早于 2000-01-01；
数值上限复用 validators.MAX_* 常量；落库单事务，任一异常整体回滚。
"""
from __future__ import annotations

import csv
import io
from datetime import date

from src.models import DailySales, Product, Staff, db
from src.validators import MAX_MATERIAL, MAX_REVENUE, MAX_SALES_QTY
from src import business_calendar as cal

# 上传边界（免费层内存保护；2100 行演示样例约 100KB，2MB 足够 5 万行）
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
MIN_DATE = date(2000, 1, 1)

# 表头别名 → 标准字段（大小写不敏感；中文表头同样支持）
HEADER_ALIASES = {
    'date': 'date', '日期': 'date', 'record_date': 'date', '填报日期': 'date',
    'product': 'product', '产品': 'product', 'product_name': 'product',
    '产品名': 'product', '品名': 'product', '商品': 'product',
    'qty': 'qty', '销量': 'qty', '数量': 'qty', 'product_sales_qty': 'qty',
    'revenue': 'revenue', '营收': 'revenue', '金额': 'revenue',
    'total_revenue': 'revenue', '营业额': 'revenue',
    'staff': 'staff', '店员': 'staff', '员工': 'staff', 'username': 'staff',
    '店员账号': 'staff',
    'notes': 'notes', '备注': 'notes', '异常备注': 'notes',
    'abnormal_notes': 'notes',
    'waste': 'waste', '损耗': 'waste', 'raw_material_waste': 'waste',
    'remaining': 'remaining', '剩余': 'remaining',
    'raw_material_remaining': 'remaining', '原料剩余': 'remaining',
}
REQUIRED_FIELDS = ('date', 'product', 'qty')


class ImportError_(Exception):
    """整体性失败（文件级），携带用户可读原因。"""


def _decode(raw: bytes) -> str:
    """字节流 → 文本：utf-8-sig → gbk 逐级嗅探，全失败抛 ImportError_。"""
    for encoding in ('utf-8-sig', 'gbk'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportError_('文件编码无法识别，请用 UTF-8 或 GBK 编码的 CSV 重新导出')


def _map_headers(header_row) -> dict:
    """表头行 → {列下标: 标准字段}；未识别列忽略。返回映射与识别结果。

    忽略是**有意**的（用户手头的表常带无关列），但"静默"不是：未识别列名一并回传，
    由 `build_report()` 放进 `ignored_columns` 给用户看（对标第 20 轮 M67）。
    动因：看板导出是宽表（含当前包自定义指标列），而自定义指标键不在 `HEADER_ALIASES` 里
    ⇒ "导出→在 Excel 改→再导入"这条自然路径会**悄悄丢掉那些列**，用户以为导回来了。
    """
    mapping = {}
    ignored = []
    for idx, cell in enumerate(header_row):
        key = str(cell or '').strip().lower()
        field = HEADER_ALIASES.get(key)
        if field and field not in mapping.values():
            mapping[idx] = field
        elif str(cell or '').strip():
            ignored.append(str(cell).strip())
    return {'mapping': mapping, 'ignored': ignored}


def _parse_rows(text: str):
    """CSV 文本 → (字段映射, 忽略列名, 行列表)。空文件 / 无表头 / 超行数拒收。

    每一格先过 `csv_guard.unescape_cell`：出口转义写进 CSV 的前导单引号是**显示标记**，
    不还原就会把它烘进业务数据（电话号码、负数量首字符同理）。转义与还原共用
    `src/csv_guard.py` 里同一份触发字符表，语义必须对称。
    """
    from src import csv_guard

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ImportError_('文件为空，请检查是否导出了正确的 CSV')
    header = _map_headers(rows[0])
    mapping = header['mapping']
    missing = [f for f in REQUIRED_FIELDS if f not in mapping.values()]
    if missing:
        raise ImportError_(
            '表头缺少必填列：%s（需要 日期/产品/销量 三列，可选 营收/店员/备注/损耗/剩余）'
            % '、'.join(missing))
    if len(rows) - 1 > MAX_ROWS:
        raise ImportError_('单次最多导入 %d 行，当前 %d 行，请分批上传'
                           % (MAX_ROWS, len(rows) - 1))
    body = [[csv_guard.unescape_cell(cell) for cell in row] for row in rows[1:]]
    return mapping, header['ignored'], body


def _to_date(raw: str):
    """解析日期串，支持 4 种常见分隔；失败返回 None。"""
    from datetime import datetime
    raw = (raw or '').strip()
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y%m%d'):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _to_number(raw: str):
    """数值解析：空 → None；非法 → 'NaN' 哨兵（调用方给行级回执）。"""
    raw = (raw or '').strip().replace(',', '')
    if raw == '':
        return None
    try:
        return float(raw)
    except ValueError:
        return 'NaN'


def build_report(admin, store, raw: bytes, default_staff_id: int,
                 commit: bool = False) -> dict:
    """解析 + 校验（可选落库）。

    Args:
        admin: 当前登录 Admin（老板/店长）。
        store: 目标门店（调用方已校验属于 admin 可见范围）。
        raw: 上传文件字节流。
        default_staff_id: 默认店员（行内 staff 列缺省/未知时使用）。
        commit: False = dry-run 只出报告；True = 有效行落库（幂等）。

    Returns:
        dict: {total, inserted, duplicates, errors:[{row, field, reason}],
        ignored_columns:[表头里未被识别的列名]}（commit=False 时 inserted 恒 0）

    Raises:
        ImportError_: 文件级失败（编码/表头/行数/文件大小）。
    """
    from src import industry_pack

    if len(raw) > MAX_FILE_BYTES:
        raise ImportError_('文件超过 %d MB，请分批上传' % (MAX_FILE_BYTES // 1024 // 1024))
    mapping, ignored_cols, rows = _parse_rows(_decode(raw))

    pack_id = store.active_pack_id or industry_pack.active_pack_id()
    # 产品白名单：该门店 + 该包的在售产品（同 h5.submit 语义，含默认包 NULL 直通）
    products = Product.query.filter(
        Product.is_active.is_(True)).all()
    valid_products = {p.product_name: p for p in products
                      if p.store_id == store.id
                      and (p.pack_id == pack_id
                           or (pack_id == industry_pack.DEFAULT_PACK_ID
                               and p.pack_id is None))}
    # 店员字典：该门店在售店员 username → id
    staff_map = {s.username: s.id for s in Staff.query.filter(
        Staff.is_active.is_(True), Staff.store_id == store.id).all()}
    # 查重键预取（一次性批量载入该门店该包的既有行键，避免逐行 N+1 查询）
    existing_keys = {
        (r[0], r[1], r[2], r[3]) for r in db.session.query(
            DailySales.store_id, DailySales.staff_id, DailySales.record_date,
            DailySales.product_name).filter(
            DailySales.store_id == store.id,
            DailySales.pack_id == pack_id).all()}

    def _field(row, name):
        for idx, f in mapping.items():
            if f == name and idx < len(row):
                return row[idx]
        return ''

    today = cal.business_today()
    report = {'total': len(rows), 'inserted': 0, 'duplicates': 0, 'errors': [],
              'ignored_columns': ignored_cols}
    seen = set()          # 本次文件内部查重
    to_insert = []

    for line_no, row in enumerate(rows, start=2):   # 行号含表头（Excel 习惯）
        def err(field, reason):
            report['errors'].append({'row': line_no, 'field': field,
                                     'reason': reason})
        # 1) 日期
        rec_date = _to_date(_field(row, 'date'))
        if rec_date is None:
            err('date', '日期格式无法识别（支持 2026-09-24 / 2026/9/24 等写法）')
            continue
        if rec_date > today:
            err('date', '日期不能晚于今天')
            continue
        if rec_date < MIN_DATE:
            err('date', '日期早于 2000-01-01，请检查数据源')
            continue
        # 2) 产品（白名单 = 该门店该包在售产品）
        product_name = str(_field(row, 'product')).strip()
        if not product_name:
            err('product', '产品名为空')
            continue
        if product_name not in valid_products:
            err('product', '产品「%s」不在该门店当前行业包的在售列表中' % product_name)
            continue
        # 3) 数量与金额
        qty = _to_number(_field(row, 'qty'))
        if qty is None or qty == 'NaN' or qty != int(qty) or qty < 0 or qty > MAX_SALES_QTY:
            err('qty', '销量须为 0~%d 的整数' % MAX_SALES_QTY)
            continue
        product = valid_products[product_name]
        revenue = _to_number(_field(row, 'revenue'))
        if revenue == 'NaN' or (revenue is not None
                                and (revenue < 0 or revenue > MAX_REVENUE)):
            err('revenue', '营收须为 0~%s 的数值' % MAX_REVENUE)
            continue
        if revenue is None:
            revenue = int(qty) * float(product.unit_price or 0)  # 与填报页同源自动计算
        # 4) 可选数值列
        waste = _to_number(_field(row, 'waste'))
        if waste == 'NaN' or (waste is not None
                              and (waste < 0 or waste > MAX_MATERIAL)):
            err('waste', '损耗须为 0~%s 的数值' % MAX_MATERIAL)
            continue
        remaining = _to_number(_field(row, 'remaining'))
        if remaining == 'NaN' or (remaining is not None
                                  and (remaining < 0 or remaining > MAX_MATERIAL)):
            err('remaining', '剩余须为 0~%s 的数值' % MAX_MATERIAL)
            continue
        # 5) 店员：行内 username → 门店店员字典 → 默认店员
        staff_raw = str(_field(row, 'staff')).strip()
        staff_id = staff_map.get(staff_raw) if staff_raw else default_staff_id
        if staff_id is None:
            err('staff', '店员「%s」不属于该门店（且未选择默认店员）' % staff_raw)
            continue
        # 6) 查重（幂等）：库内已有 + 文件内重复都算 duplicate
        key = (store.id, staff_id, rec_date, pack_id, product_name)
        dup_key = (key[0], key[1], key[2], key[4])
        if key in seen or dup_key in existing_keys:
            report['duplicates'] += 1
            continue
        seen.add(key)
        notes = str(_field(row, 'notes')).strip()
        to_insert.append(DailySales(
            staff_id=staff_id, store_id=store.id, record_date=rec_date,
            product_name=product_name, product_sales_qty=int(qty),
            total_revenue=round(revenue, 2),
            raw_material_waste=None if waste is None else ('%g' % waste),
            raw_material_remaining=None if remaining is None else ('%g' % remaining),
            abnormal_notes=notes or None, pack_id=pack_id))

    if commit and to_insert:
        db.session.add_all(to_insert)
        db.session.commit()      # 单事务：IntegrityError 由调用方捕获回滚
        report['inserted'] = len(to_insert)
    return report
