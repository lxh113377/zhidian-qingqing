"""查询范围守卫（H2：租户隔离下沉框架层，2026-09-24 开源对标改进批次1）。

背景：此前租户隔离靠路由逐处手写 `store_scope_clause` / `merchant_scope_clause` /
`active_pack_clause`（P7-E3-C 修 TOCTOU 时已把请求级快照沉到 request.environ，但仍属
「每处写对」而非「框架保证」）。本模块把隔离下沉为 SQLAlchemy 全局钩子：凡在
**请求上下文内**、由**已登录的非平台账号**发起的 ORM SELECT，一律自动注入租户范围
条件 —— 路由漏写过滤的查询由本守卫兜底（对标芋道 TenantLineInnerInterceptor 思想，
Python/SQLAlchemy 实现）。

语义边界（与 src/tenant.py 既有口径逐条对齐，唯一新增是「自动」）：
  - 店铺维度表（store_id 列）：Product / DailySales / QuizRecord / MetricDef /
    StoreTask / AIUsageLedger / ChangeLog —— 按「可见门店集合」过滤，NULL 归属历史行放行
    （同 `store_visible_clause`）。Staff 账号行不在守卫名单：身份加载与店员
    可见性由路由层 staff_visible/visible_staff_ids IDOR 收口承担（2026-09-21
    越权修复），守卫套在账号行上会让无归属店员被 strict 态藏掉自身行。
  - 商家维度表（merchant_id 列，无 store_id）：Store / ApiToken / WebhookSubscription
    —— 按当前商家过滤，NULL 归属历史行放行（同 `merchant_scope_clause`）。
  - 已登录但无归属（owner 无商家 / 店员无门店）→ 一律 `false()`（同 PH2-3B 安全兜底：
    无归属账号不得看到全平台数据）。
  - 平台管理员（role=platform）→ **不武装**：平台页只放行商家/门店/账号元数据，
    业务明细由路由层既有越权断言约束（可见 PH2-2C 口径）。
  - 请求外（CLI / init_db / 迁移脚本 / 定时线程）→ 不武装，既有离线链路零回归。
  - 写路径（INSERT/UPDATE/DELETE）不拦截：写侧越权防护仍由路由预检查与约束承担。
  - OPT-A 开放 API（Bearer Token，无 session 用户）与 /demo（匿名）天然不武装，
    它们自带显式租户过滤与 403/404 语义。

逃生舱与开关：
  - `unscoped()`：上下文管理器，受控跨域读（管理/维护场景），嵌套安全。
  - `QUERY_SCOPE_GUARD_ENABLED=0`（env 或 app.config）全局关闭，回退纯路由过滤。
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager

from flask import has_request_context, request
from sqlalchemy import event, false, or_
from sqlalchemy.orm import Session, with_loader_criteria

_SCOPE_ENV_KEY = '_ican_query_guard_scope'
_UNSET = object()
_INFLIGHT = object()  # 解析进行中哨兵（防递归，区别于「未武装」）
_ESCAPE_DEPTH = threading.local()  # unscoped() 嵌套深度（0 = 未逃逸）
# 安装态与锁同放一个 dict 容器：规避 `global`（PLW0603），沿用仓内 webhooks.py 范式
_INSTALL = {'done': False, 'lock': threading.Lock()}


def _truthy(raw, default=False):
    if raw is None:
        return default
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def _guard_enabled():
    """总开关：app.config 优先，env 兜底，默认开（H2 验收口径）。"""
    try:
        from flask import current_app
        value = current_app.config.get('QUERY_SCOPE_GUARD_ENABLED')
        if value is not None:
            return bool(value)
    except Exception:  # noqa: S110  理由：无应用上下文的防御性兜底（理论不该发生），此处抛异常会让所有 CLI/脚本路径整体不可用
        pass
    return _truthy(os.environ.get('QUERY_SCOPE_GUARD_ENABLED'), default=True)


@contextmanager
def unscoped():
    """受控跨域读逃生舱：块内 ORM SELECT 不注入租户条件（嵌套安全）。

    仅供管理与维护代码使用（如 ensure_default_tenant 类兜底回填）；
    业务路由**不得**用它绕过租户红线。
    """
    _ESCAPE_DEPTH.n = getattr(_ESCAPE_DEPTH, 'n', 0) + 1
    try:
        yield
    finally:
        _ESCAPE_DEPTH.n -= 1


def _resolve_scope():
    """解析当前请求的租户范围（每请求一次，缓存在 request.environ）。

    Returns:
        dict | None | 'strict'：
        - None：不武装（请求外 / 未登录 / 平台管理员 / 开关关闭 / 递归解析中）。
        - 'strict'：已登录但无归属 → 所有守卫表一律 false()。
        - dict：{'merchant_id': int|None, 'store_ids': tuple[int, ...]}。
    """
    if not has_request_context():
        return None
    if getattr(_ESCAPE_DEPTH, 'n', 0):
        return None
    cached = request.environ.get(_SCOPE_ENV_KEY, _UNSET)
    if cached is _INFLIGHT:
        # 解析进行中（visible_store_ids 内部查询 / user_loader 身份加载）：
        # 这些查询本身就是范围解析的真相源，不注入条件。
        return None
    if cached is not _UNSET:
        # 只缓存已武装态（dict / 'strict'）。未武装态不缓存：请求内首个查询
        # 可能发生在登录态建立之前（登录 POST 的口令校验、测试直取用户行），
        # 若把 None 固化，登录后的查询会全部漏防（2026-09-24 L4 实测踩坑）。
        return cached
    request.environ[_SCOPE_ENV_KEY] = _INFLIGHT
    scope = None
    # ↑ 必须先绑定再进 try：原来 `finally` 直接读 `scope`，一旦 `_compute_scope()` 抛异常，
    #   finally 就改成抛 UnboundLocalError（「cannot access local variable 'scope'」），
    #   把真实异常整个盖掉 —— 演示重置页 500 就是这么被伪装成"局部变量问题"的（2026-09-26 实测）。
    try:
        scope = _compute_scope() if _guard_enabled() else None
    finally:
        if scope is None:
            request.environ.pop(_SCOPE_ENV_KEY, None)
        else:
            request.environ[_SCOPE_ENV_KEY] = scope
    return scope


def _compute_scope():  # noqa: PLR0911 - 守卫判定本质是多分支决策表，拆函数只会割裂读法
    # 导入刻意放函数内：本模块由 create_app 早期装载，顶层导入 src.models 会与
    # app.py 形成循环导入。
    from flask_login import current_user

    from src.models import ROLE_PLATFORM, Admin, Staff
    from src.tenant import visible_store_ids

    if not getattr(current_user, 'is_authenticated', False):
        return None

    if isinstance(current_user, Admin):
        if (getattr(current_user, 'role', None) or 'owner') == ROLE_PLATFORM:
            return None  # 平台管理员：元数据面，业务明细由路由层约束
        merchant_id = getattr(current_user, 'merchant_id', None)
        if merchant_id is None:
            return 'strict'  # 已登录无归属：不放行任何业务行（PH2-3B 口径）
        return {'merchant_id': merchant_id,
                'store_ids': tuple(visible_store_ids(current_user))}
    if isinstance(current_user, Staff):
        store_id = getattr(current_user, 'store_id', None)
        if store_id is None:
            return 'strict'  # 店员无门店：同 store_scope_clause 的安全兜底
        return {'merchant_id': getattr(current_user, 'merchant_id', None),
                'store_ids': (store_id,)}
    return None


def _make_store_criteria(store_ids):
    """店铺维度条件工厂。

    ⚠️ 关键实现约束（2026-09-24 L4 实测踩坑）：范围值必须是**lambda 闭包单元**
    （在 lambda 体内直接引用），不能用默认参数或工厂函数间接传递 ——
    SQLAlchemy 的 lambda SQL 缓存只把「闭包单元里的可哈希值」纳入语句缓存键；
    间接引用会导致换租户后仍复用首次编译的旧绑定（实测 store_id=1 的缓存
    污染 store_id=2 的店员加载，load_user 恒 None → 302）。
    """
    if not store_ids:
        return lambda cls: false()
    return lambda cls: or_(cls.store_id.is_(None), cls.store_id.in_(store_ids))


def _make_merchant_criteria(merchant_id, store_ids):
    """商家维度条件工厂（闭包约束同上）。

    Store 额外放行「当前可见门店」（店员 merchant 为 NULL 的历史行）；
    其余商家维度表在无商家归属时不放行。
    """
    if merchant_id is not None:
        return lambda cls: or_(cls.merchant_id.is_(None), cls.merchant_id == merchant_id)
    if store_ids:
        def _crit(cls):
            if getattr(cls, '__tablename__', '') == 'stores':
                return or_(cls.merchant_id.is_(None), cls.id.in_(store_ids))
            return false()
        return _crit
    return lambda cls: false()


def _strict_criteria(cls):
    return false()


def _install_guard():
    """注册 do_orm_execute 钩子（幂等，Session 类级 → 覆盖全部会话）。"""
    if _INSTALL['done']:
        return
    with _INSTALL['lock']:
        if _INSTALL['done']:
            return

        from src import models as m

        # 店铺维度表（业务数据）。注意 Staff 不在内：账号行是身份加载
        # （flask_login user_loader 的 session.get）与既有 staff_visible /
        # visible_staff_ids 路由层 IDOR 收口（2026-09-21 越权修复）的对象；
        # 守卫若套在 Staff 行上，无归属店员（回归套件播种的 store/merchant
        # 双 NULL 账号）会被 strict 态藏掉自己的账号行 → 登录态失效。
        store_dim_models = (
            m.Product, m.DailySales, m.QuizRecord, m.MetricDef,
            m.StoreTask, m.AIUsageLedger, m.ChangeLog,
        )
        merchant_dim_models = (m.Store, m.ApiToken, m.WebhookSubscription, m.WebhookDelivery)

        @event.listens_for(Session, 'do_orm_execute')
        def _tenant_guard(execute_state):
            if not execute_state.is_select:
                return
            if getattr(_ESCAPE_DEPTH, 'n', 0):
                return
            scope = _resolve_scope()
            if scope is None:
                return
            if scope == 'strict':
                execute_state.statement = execute_state.statement.options(
                    *[with_loader_criteria(model, _strict_criteria, include_aliases=True)
                      for model in store_dim_models + merchant_dim_models])
                return
            store_crit = _make_store_criteria(scope['store_ids'])
            merchant_crit = _make_merchant_criteria(scope['merchant_id'],
                                                    scope['store_ids'])
            execute_state.statement = execute_state.statement.options(
                *[with_loader_criteria(model, store_crit, include_aliases=True)
                  for model in store_dim_models],
                *[with_loader_criteria(model, merchant_crit, include_aliases=True)
                  for model in merchant_dim_models])

        _INSTALL['done'] = True


def init_app(app):
    """Flask 装配入口：注册钩子并同步 app.config 开关。"""
    app.config.setdefault('QUERY_SCOPE_GUARD_ENABLED',
                          _truthy(os.environ.get('QUERY_SCOPE_GUARD_ENABLED'),
                                  default=True))
    _install_guard()
    return app


__all__ = ['init_app', 'unscoped']
