"""CSV 出口转义与入口还原（对标第 20 轮 H42）—— 单一真相源。

## 为什么必须有这一层

`src/routes/admin.py` 的看板导出把 `product_name` 与 `abnormal_notes` 原样写进 CSV，
而这两列**都是用户自由文本**（店员填报、后台改品名）。Excel/WPS 会把以
``=`` ``+`` ``-`` ``@`` 或制表符/回车开头的单元格当公式求值 —— 也就是说
"店员在备注里敲几个字符，老板打开报表时在他自己的机器上执行"，
这是一条**跨信任边界的数据流**，与 XSS 同类，只是落点在桌面软件里。

本仓既有防线测不到它：`ruff --select S`（门禁第 28 项）看的是代码里的安全反模式，
门禁第 5 项扫的是材料字面串，三套测试对导出只断言 HTTP 200。**数据面缺陷不在
代码面判据的射程内**，所以它必须有自己的判据（见 `test_l4_assertions.py` S23 与门禁第 32 项）。

## 为什么"只转义"不够，必须成对

上游 Frappe 的实现给了关键的一票（`frappe/utils/csvutils.py:14/17/24/121`，逐行核实）：
它同时有 `escape_formula_injection` 与 `unescape_formula_injection`，后者的注释写明理由——
前导单引号是**显示标记而不是值**，若不还原，"导出→在表格里改→再导入"这条自然路径
会把引号永久烘进数据（例如以文本形式存的电话号码、负数量）。

本项目同样有这条回灌路径（`src/data_import.py` 支持从 CSV 重新导入填报），所以
**转义与还原必须成对存在**，且共用同一份触发字符表 —— 两处各写一份迟早会漂移
（本仓"同类出口只修一半"的第 N 次预防）。

## 已明确承认的取舍（写在这里，并钉进测试而不是只写注释）

`'+S-001` 这类**本来就以前导单引号 + 触发字符开头**的值，还原时会丢掉那个引号。
CSV 本身无法区分"我打的引号"与"转义标记"，Frappe 在注释里也承认同一取舍。
本仓的口径：宁可丢一个显示引号，不可让公式在老板机器上执行；该行为由 L4 断言显式钉住，
将来谁想改这条语义，必须先让那条断言变红并给出理由。
"""
import os
import re

# 电子表格软件会当作公式求值的行首字符（含制表符与回车——它们同样能改变解析起点）
FORMULA_TRIGGER_CHARS = ('=', '+', '-', '@', '\t', '\r')

# 前导单引号 + 触发字符：这是本模块写出去的转义标记，不是用户数据
_ESCAPED_PREFIX_RE = re.compile(r"^'(?=[" + re.escape(''.join(FORMULA_TRIGGER_CHARS)) + r"])")

# 出口行数上限。标定说明（对标 R236「先标定再定硬度」）：本轮实测演示库该端点
# 365 天窗口只出 300 行 / 15,553 B（约 52 B/行），样本量太小，**测不出 ORM 逐行驻留
# 的边际成本**，因此这个数**不按微基准推导，按策略取**：5 万行 ≈ 2.6 MB 响应，
# 落在上游 Superset 的 DISPLAY_MAX_ROW=10000 与 SQL_MAX_ROW=100000 之间，
# 且是现网最大合法窗口的 167 倍。判据取双侧（恰上限放行 / 超一行即拒），
# 不取"我算出来的精确值"——那是假精度。
DEFAULT_MAX_EXPORT_ROWS = 50000
MAX_EXPORT_ROWS_ENV = 'MAX_EXPORT_ROWS'


def max_export_rows():
    """出口行数上限（可环境变量覆盖）；非法值回退默认并留 WARNING，不静默。"""
    import logging
    raw = os.environ.get(MAX_EXPORT_ROWS_ENV)
    if raw in (None, ''):
        return DEFAULT_MAX_EXPORT_ROWS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning('%s 非整数（%r），回退默认 %d',
                                            MAX_EXPORT_ROWS_ENV, raw, DEFAULT_MAX_EXPORT_ROWS)
        return DEFAULT_MAX_EXPORT_ROWS
    if value < 1:
        logging.getLogger(__name__).warning('%s=%d 小于 1，回退默认 %d',
                                            MAX_EXPORT_ROWS_ENV, value, DEFAULT_MAX_EXPORT_ROWS)
        return DEFAULT_MAX_EXPORT_ROWS
    return value


def escape_cell(value):
    """单元格 → 安全文本：命中触发字符则前置单引号（渲染不求值）。

    非字符串（数值/日期/None）原样返回：转义只对文本单元有意义，
    给数字加引号会改变 Excel 的列类型判定，那是另一种破坏。
    """
    if isinstance(value, str) and value.startswith(FORMULA_TRIGGER_CHARS):
        return "'" + value
    return value


def unescape_cell(value):
    """还原 `escape_cell` 写出的标记；与它共用触发字符表，语义必须对称。"""
    if isinstance(value, str) and _ESCAPED_PREFIX_RE.match(value):
        return value[1:]
    return value


def escape_dataframe(df):
    """返回副本：逐格转义。

    刻意**不改原 DataFrame**：调用方随后还要用原始值做统计/校验，
    把转义烘进内存态会让"报表数字"与"导出内容"两处不一致。

    刻意**不按 dtype 筛列**：本项目跑的是 pandas 3.x，字符串列的 dtype 不再是
    ``object``，按 `dtype == 'object'` 筛会一列都匹配不上 ⇒ 转义静默变空操作、
    判据还照样绿（本仓"覆盖面按形状算"的老毛病在我自己新写的代码上复发，
    由 L4 的端到端断言当场抓红）。`escape_cell` 本来就只动 str，
    全列过一遍既与 dtype 无关，也不会误伤数值（非字符串原样返回）。
    """
    out = df.copy()
    for col in out.columns:
        out[col] = out[col].map(escape_cell)
    return out


def rows_exceed(row_count, limit=None):
    """行数是否越界（纯函数，便于判据直接喂边界样本而不必造真数据）。"""
    cap = limit if limit is not None else max_export_rows()
    return row_count > cap


__all__ = [
    'DEFAULT_MAX_EXPORT_ROWS',
    'FORMULA_TRIGGER_CHARS',
    'MAX_EXPORT_ROWS_ENV',
    'escape_cell',
    'escape_dataframe',
    'max_export_rows',
    'rows_exceed',
    'unescape_cell',
]
