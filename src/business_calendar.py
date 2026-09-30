"""业务日与时钟的唯一真相源（对标第 29 轮 H77）。

为什么必须有它：本仓所有"今天"此前都取自**进程时钟**（裸 `date.today()` / `datetime.now()`，
AST 实数 45 处）。开发机在 UTC+8 上跑，两者与业务日恰好一致，于是缺陷被掩盖；
生产端（PythonAnywhere 免费层）服务器时区是 UTC ⇒ **每天北京时间 00:00—08:00 这 8 小时内，
服务端的"今天"是昨天**：店员早上补交会把数据写成前一天、看板"今日"空白、
`uq(staff,date,pack,product)` 判重判在错的日子上。同族的量坑 `src/ai_quota.py:53`
的注释早已自认，但上一轮只修了那一个出口 —— 本轮按"修一类而不是一个"收敛到本模块。

上游口径（同轮 `gh api` 取回原文逐行核过，缓存在 `D:/tmp/bench30/`）：
- Django `django/utils/timezone.py:187-198` `localdate()` = "先转到当前时区再取 date"，
  `:52` 默认时区由 `ZoneInfo(settings.TIME_ZONE)` 得出 ⇒ 业务日不是进程日期；
- Frappe `frappe/utils/data.py:388-390` `get_system_timezone()` 取**系统设置里的 time_zone**，
  `:402-416` 一律 `ZoneInfo` 转换，`:421-433` 留 `flags.current_date` 测试注入口；
- InvenTree `InvenTree/settings.py:741-747`：配了 `TIME_ZONE` 就当场 `ZoneInfo()` 校验，
  非法即 `ValueError` ⇒ "配错"必须是启动失败，不是静默按 UTC 跑。
"""

import os
import re
from datetime import date, datetime, timedelta, timezone

__all__ = ['tz_name', 'business_today', 'business_now', 'describe', 'validate_config',
           'FIXED_OFFSET_FALLBACK', 'UTC']

UTC = timezone.utc
_ENV_NAME = 'ICAN_BUSINESS_TZ'
_DEFAULT_TZ = 'Asia/Shanghai'
# IANA 名形状：`区域/城市`（可含 _、+、-、数字）或字面 UTC。挡住"北京""+8""GMT+8"这类手抄口径——
# 它们进 ZoneInfo 只会得到一个"看起来能用"的降级，正是要在这里拦住的形态。
_NAME_SHAPE = re.compile(r'^(UTC|[A-Za-z]+/[A-Za-z0-9_+\-/]+)$')
# tz 数据库缺失时的**显式**降级表：中国自 1991 年起不再实行夏令时 ⇒ +08:00 对这三地对任意日期精确。
# 只收录"无夏令时且偏移恒定"的时区，其余名字缺库即报错，绝不猜。
FIXED_OFFSET_FALLBACK = {
    'Asia/Shanghai': 8, 'Asia/Hong_Kong': 8, 'Asia/Macau': 8, 'Asia/Singapore': 8,
    'Asia/Chongqing': 8, 'Asia/Urumqi': 6, 'UTC': 0,
}

_state = {'tz': None, 'via': None, 'name': None, 'error': None}


def tz_name():
    """生效的业务时区名（env 可覆盖；空值回退默认，不静默按进程时区）。"""
    return (os.environ.get(_ENV_NAME) or _DEFAULT_TZ).strip() or _DEFAULT_TZ


def _resolve():
    """把时区名解析成 `tzinfo`，并记下**到底走了哪条路**（降级必须可见，不许静默）。"""
    name = tz_name()
    if _state.get('name') == name and _state.get('tz') is not None:
        return _state['tz'], _state['via']
    tz, via, err = None, None, None
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            tz, via = ZoneInfo(name), 'zoneinfo'
        except ZoneInfoNotFoundError:
            # 分不清"名字不存在"还是"机器没 tz 数据库"：available_timezones() 能列出来就是名字错，
            # 列不出来就是缺库 ⇒ 后者才允许走下面的显式兜底。
            try:
                known = set(__import__('zoneinfo').available_timezones())
            except Exception:                       # noqa: BLE001 列不出来 = 无库
                known = set()
            if name in known:
                err = f'时区名 {name!r} 在数据库里不存在（可用清单里没有它）'
            elif name in FIXED_OFFSET_FALLBACK:
                hours = FIXED_OFFSET_FALLBACK[name]
                tz, via = timezone(timedelta(hours=hours)), 'fixed-offset'
            else:
                err = (f'本机无 tz 数据库（zoneinfo 列不出 {name!r}），且 {name!r} 不在'
                       ' FIXED_OFFSET_FALLBACK（只收无夏令时、偏移恒定的时区）⇒ 请安装 tzdata 或改配置')
    except Exception as exc:                         # noqa: BLE001 解析层任何异常都必须可见
        err = f'解析时区 {name!r} 失败：{type(exc).__name__}: {exc}'
    if err is not None:
        raise ValueError(f'业务时区配置不可用（{_ENV_NAME}={name!r}）：{err}。'
                         ' 宁可启动失败，也不得按进程时钟静默跑——那会把"今天"算错 8 小时。')
    _state.update({'tz': tz, 'via': via, 'name': name, 'error': None})
    return tz, via


def business_today(now=None):
    """业务日（date）。`now` 可注入（aware datetime 按 UTC 解释前的原时刻，naive 视为 UTC）。

    纯函数 ⇒ 测试可确定性喂"UTC 23:30"验日切，不依赖跑测试那一刻的墙钟。
    """
    moment = _aware(now)
    tz, _via = _resolve()
    return moment.astimezone(tz).date()


def business_now(now=None):
    """业务时区里的 aware datetime（展示给人看的时钟：日志行、页面"更新于"、导出文件名）。"""
    tz, _via = _resolve()
    return _aware(now).astimezone(tz)


def from_wall_clock(moment):
    """把"钟面读出来的 naive 时刻"（备份/锚点文件名里那 14 位）变成 aware UTC。

    方向与 `_aware()` **相反**，所以必须分名而不许混用：
      - `_aware(naive)` 按 UTC 解释 —— 对偶 DB 列（`src/models.py:535-536` 的
        `applied_at = datetime.now(UTC).replace(tzinfo=None)` 就是 naive UTC）；
      - 本函数按**业务时区钟面**解释 —— 对偶文件名（`backup_db.py` 用strftime 写的是人眼看到的钟点）。
    两者直接比会差整一个 UTC 偏移（第 36 轮实测：同一件事账本记 `01:46`、文件名写 `094600`）。
    已带 tzinfo 的输入只做换算、不做猜测。
    """
    tz, _via = _resolve()
    return (moment if moment.tzinfo is not None else moment.replace(tzinfo=tz)).astimezone(UTC)


def from_utc_naive(moment):
    """naive UTC（DB 列口径）→ aware UTC。显式命名，防调用方拿错另一头。"""
    return _aware(moment)


def wall_stamp(moment=None):
    """14 位业务钟面戳 —— 备份名/锚点名的**唯一**合法来源（文件名与账本比对时靠它定基准）。"""
    return business_now(moment).strftime('%Y%m%d_%H%M%S')


def _aware(moment):
    if moment is None:
        return datetime.now(UTC)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def validate_config():
    """配置自证（供门禁与启动期复用）：返回问题列表，空 = 可用。"""
    problems = []
    name = tz_name()
    if not _NAME_SHAPE.match(name):
        problems.append(f'{_ENV_NAME}={name!r} 不是 IANA 名（要 `区域/城市` 或 UTC），'
                        '禁用 "+8"/"GMT+8"/中文口径这类手抄偏移')
        return problems
    try:
        _resolve()
    except ValueError as exc:
        problems.append(str(exc))
    return problems


def describe():
    """给 `/healthz` 与门禁回执用的读数：业务日、时区、**走的是哪条解析路径**。"""
    tz, via = _resolve()
    moment = datetime.now(UTC)
    return {'business_tz': tz_name(), 'resolved_via': via,
            'business_date': business_today(moment).isoformat(),
            'utc_date': moment.astimezone(UTC).date().isoformat(),
            'business_now': business_now(moment).isoformat(timespec='seconds'),
            'cross_day_now': business_today(moment) != moment.astimezone(UTC).date()}
