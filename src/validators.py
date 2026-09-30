"""表单入参校验工具 — 供路由层复用的纯函数集合。

职责边界：
  - 只做「格式 / 符号 / 范围 / 复杂度」这类与框架无关的校验；
  - 不做 DB 查询、不 flash、不 abort，由路由层决定如何反馈用户；
  - 返回值统一为 (值, 错误信息)，错误信息为 None 表示校验通过。

对应审查项：F9（口令复杂度）、F13（类型转换显式失败）、F14（数值范围）。
"""
import math
import re

# ── 业务合理上限（防误填天量数据污染看板统计）──
MAX_SALES_QTY = 100000          # 单产品单日销量上限（份）
MAX_REVENUE = 1000000.0         # 单产品单日营收上限（元）
MAX_MATERIAL = 100000.0         # 原料剩余/损耗上限（份）
MAX_UNIT_PRICE = 100000.0       # 产品单价上限（元）

# ── 口令复杂度（F9）──
MIN_PASSWORD_LENGTH = 8
PASSWORD_RULE_TEXT = f'密码长度至少 {MIN_PASSWORD_LENGTH} 位，且必须同时包含字母和数字'


class ParseError(ValueError):
    """入参格式非法（无法转换为目标类型）。

    路由层应据此返回 400，而不是静默回退默认值（F13）。
    """


def parse_int(raw, field_label, default=None, allow_empty=True):
    """把表单原始字符串解析为 int，格式非法时抛 ParseError。

    Args:
        raw: 表单原始值（str 或 None）。
        field_label: 字段中文名，用于组装错误信息。
        default: 空值时返回的默认值。
        allow_empty: 为 False 时空值也视为非法。

    Returns:
        int | None: 解析结果；空值且 allow_empty 时返回 default。

    Raises:
        ParseError: 值非空但不是合法整数，或空值且 allow_empty=False。
    """
    text = (raw or '').strip() if isinstance(raw, str) else raw
    if text is None or text == '':
        if allow_empty:
            return default
        raise ParseError(f'{field_label}不能为空')
    try:
        return int(str(text))
    except (TypeError, ValueError):
        # from None：切断底层异常链，避免 int() 的英文报错细节混入用户可见信息。
        raise ParseError(f'{field_label}必须是整数，当前提交值不合法') from None


def parse_float(raw, field_label, default=None, allow_empty=True):
    """把表单原始字符串解析为 float，格式非法时抛 ParseError。

    Args:
        raw: 表单原始值（str 或 None）。
        field_label: 字段中文名，用于组装错误信息。
        default: 空值时返回的默认值。
        allow_empty: 为 False 时空值也视为非法。

    Returns:
        float | None: 解析结果；空值且 allow_empty 时返回 default。

    Raises:
        ParseError: 值非空但不是合法数字，或空值且 allow_empty=False。
    """
    text = (raw or '').strip() if isinstance(raw, str) else raw
    if text is None or text == '':
        if allow_empty:
            return default
        raise ParseError(f'{field_label}不能为空')
    try:
        value = float(str(text))
    except (TypeError, ValueError):
        # from None：切断底层异常链，避免 float() 的英文报错细节混入用户可见信息。
        raise ParseError(f'{field_label}必须是数字，当前提交值不合法') from None
    # math.isfinite 一次排除 NaN 与 ±Inf：
    # float('nan') / float('inf') 都能通过 float() 转换，但入库后会污染统计聚合。
    if not math.isfinite(value):
        raise ParseError(f'{field_label}必须是有效数字')
    return value


def check_range(value, field_label, maximum, minimum=0):
    """校验数值符号与上限（F14）。

    Args:
        value: 待校验数值，None 表示未填写（直接通过）。
        field_label: 字段中文名。
        maximum: 允许的最大值（含）。
        minimum: 允许的最小值（含），默认 0 即非负。

    Returns:
        str | None: 校验不通过时的中文错误信息，通过时为 None。
    """
    if value is None:
        return None
    if value < minimum:
        # 保留「不能为负数」措辞，与既有回归用例断言口径一致
        return f'{field_label}不能为负数' if minimum == 0 else \
               f'{field_label}不能小于 {minimum}'
    if value > maximum:
        return f'{field_label}超出合理范围（上限 {maximum:g}），请核对后重新填写'
    return None


def validate_password_strength(password):
    """校验口令复杂度（F9）：长度 ≥8 且同时含字母与数字。

    Args:
        password: 明文口令。

    Returns:
        str | None: 不满足时的中文错误信息，满足时为 None。
    """
    pwd = password or ''
    if len(pwd) < MIN_PASSWORD_LENGTH:
        return PASSWORD_RULE_TEXT
    if not re.search(r'[A-Za-z]', pwd):
        return PASSWORD_RULE_TEXT
    if not re.search(r'\d', pwd):
        return PASSWORD_RULE_TEXT
    return None
