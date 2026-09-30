# API 预留说明（老板小程序 / 未来扩展）

> 当前版本：数据经 H5 表单人工填报。本文档预留「老板小程序拿到微信 API 权限后
> 自动导入订单」的接口约定，届时按此实现即可平滑接入，无需改动现有模块。

## 一、设计原则

- 现有 H5 填报链路（`/h5/submit`）保持不变，API 仅作为**批量自动导入**的并行入口
- 写入同一张 `daily_sales` 表，遵守同一唯一约束 `(staff_id, record_date, product_name)`
- AI 只做建议，API 只做数据搬运，经营决策仍由老板人工完成（红线）

## 二、预留接口约定

### POST /api/v1/sales/batch

批量导入营业数据（小程序服务端调用，不走微信 H5 登录态）。

**鉴权**：HTTP Header `X-API-Key: <服务器 .env 中配置的 API_IMPORT_KEY>`
（与 Flask-Login 会话体系隔离，独立密钥，可随时吊销）

**请求体**：

```json
{
  "staff_username": "staff01",
  "record_date": "2026-07-22",
  "items": [
    {"product_name": "珍珠奶茶", "qty": 32, "revenue": 384.00},
    {"product_name": "柠檬水",   "qty": 18, "revenue": 144.00}
  ]
}
```

**响应**：

```json
{"code": 0, "imported": 2, "skipped": 0, "message": "ok"}
```

- `code=0` 成功；`code=1001` 鉴权失败；`code=1002` 参数错误
- 同日同产品已存在 → 计入 `skipped` 跳过（不覆盖人工填报，冲突以人工为准）

## 三、落地条件（暂未实现，按需开启）

1. 老板小程序获得微信支付/订单 API 权限
2. 在 `.env` 配置 `API_IMPORT_KEY` 并新建 `src/routes/api.py` 蓝图（约 80 行）
3. 复用 `models.DailySales` 与唯一约束，无需改表结构

> 本文件仅为接口约定，当前代码未包含该路由；盲审与演示不涉及本接口。
