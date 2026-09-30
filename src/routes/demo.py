"""免登录只读演示入口（`GET /demo`）。

设计契约（冻结）：`交付物/iCAN-参赛交付物/应用方案/demo-只读演示入口-设计契约.md`。

存在的理由
──────────
评委/客户第一次接触本作品时没有任何账号，而真实系统的驾驶舱在登录之后。
若不给一个「打开即见」的入口，技术实现分（30 分）会因为「看不到」而丢失。

硬约束（逐条对应契约第二节，任何一条被破坏都等于降低交付可信度）
────────────────────────────────────────────────────────────
1. **仅 GET**：不注册任何写方法，`POST /demo` 由 Flask 直接 405。
2. **免登录**：不读 `current_user`、不依赖会话，因此也不受角色权限层影响。
3. **零模型调用**：`allow_ai=False` 让编排短路，不检查、不初始化在线客户端。
4. **零数据库写入**：只走 `check_quota`/`usage_today` 读路径；用量账本仅由
   `ai_quota.record_usage` 写入，本入口不触发。
5. **零文件写入**：`write_trace=False` 跳过 `data/decision_trace.jsonl`。
6. **不建任务**：本蓝图不导入 `task_flow`，且模板以 `readonly=True` 渲染，
   不含人工确认表单。
7. **范围不可篡改**：门店集合完全由服务端按 `DEMO_MERCHANT_ID` 解析，
   不读取任何请求参数（query / form 一律忽略）。
8. **未配置即 404**：缺失、非法、商家不存在或无门店时一律 404，
   不区分原因，避免泄露「某个商家是否存在」。
"""
import os

from flask import Blueprint, abort, current_app, render_template

from src import decision_cockpit
from src.models import Merchant, Store

demo_bp = Blueprint('demo', __name__)

# 演示商家 id 的环境变量名。**按请求读取**而非导入期固化：
# 只读入口没有安全面，换取「改完环境变量即生效、测试可注入」的可验证性。
DEMO_MERCHANT_ENV = 'DEMO_MERCHANT_ID'

# 只读页固定标记，便于自动化断言与人工核对「这是只读入口」。
READONLY_HEADER = 'X-Demo-Readonly'


def demo_merchant():
    """解析演示商家；任何异常形态一律返回 None（由路由转 404）。

    校验链：环境变量存在 → 可转 int → 商家存在且启用。
    刻意不区分失败原因：错误信息不承担排障职责，但会泄露租户是否存在。

    Returns:
        Merchant | None: 解析成功返回商家对象，否则 None。
    """
    raw = (os.environ.get(DEMO_MERCHANT_ENV) or '').strip()
    if not raw:
        return None
    try:
        merchant_id = int(raw)
    except (TypeError, ValueError):
        return None
    return Merchant.query.filter_by(id=merchant_id, is_active=True).first()


def demo_store_ids(merchant):
    """演示门店集合：该商家全部门店（按 id 稳定排序）。

    Args:
        merchant: 已解析的演示商家。

    Returns:
        list[int]: 门店 id 列表；无门店时为空列表。
    """
    rows = Store.query.filter_by(merchant_id=merchant.id).order_by(Store.id).all()
    return [row.id for row in rows]


@demo_bp.route('/demo', methods=['GET'])
def index():
    """渲染只读驾驶舱首屏；不产生任何模型调用、数据库写入或文件副作用。"""
    merchant = demo_merchant()
    if merchant is None:
        abort(404)
    store_ids = demo_store_ids(merchant)
    if not store_ids:
        abort(404)

    decision = decision_cockpit.run_decision(
        merchant.id,
        store_ids,
        days=decision_cockpit.DECISION_DAYS,
        allow_ai=False,
        write_trace=False,
    )
    response = current_app.make_response(render_template(
        'demo/index.html',
        decision=decision,
        merchant_name=merchant.name,
        store_count=len(store_ids),
    ))
    response.headers[READONLY_HEADER] = '1'
    return response


__all__ = ['DEMO_MERCHANT_ENV', 'READONLY_HEADER', 'demo_bp', 'demo_merchant', 'demo_store_ids']
