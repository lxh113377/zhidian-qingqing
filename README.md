# 智电轻擎 — 轻量化 AI 门店管理智能体

> 以微信 H5 为入口，为不同行业的小微商家提供多门店数据管理、行业包自助定制、AI 决策驾驶舱、培训考核与跨店经营分析。
>
> 当前版本：**v1.28.0**　真相源 = `src/version.py` 的 `APP_VERSION`；门禁第 25 项把**代码 / 本行 / UPGRADE.md**
> 三处逐字对账，任一抄漏即判红（第 17 轮立约时文档写了「与 README 一致」而代码只查 UPGRADE，
> 第 30 轮实测才发现这条腿一直不存在 —— 现在补上判据，而不是把文字改软）。

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![Flask](https://img.shields.io/badge/Flask-3.0.3%20~%203.1.x-green)
![Gate](https://img.shields.io/badge/Quality%20Gate-run%20locally%20on%20authoritative%20copy-informational)

> 这里**不放** GitHub Actions 状态徽章：本仓 Actions 自 2026-09-25 起因账号额度/付款被拦
> （`gh api` 实测 run 的 job `steps=0`、日志为空 zip），徽章会**恒红**；而私有仓的徽章对外
> 也不渲染。门禁的真实执行面是本机权威副本 `quality_gate.py`，读数一律当场跑，
> 不靠徽章这种"昨天绿"的静态声明。

---

## 项目简介

**智电轻擎**面向「一个老板 + 多家门店」的小微商家场景，解决不同行业（奶茶、水果生鲜、便利店、烘焙、美妆、宠物、服装）缺少低门槛、可自定义、能覆盖多门店的统一管理工具这一痛点。

核心差异化：
- **行业包 DIY**：商家可在 7 个预置行业包基础上自助定制表单字段、考核题库、看板维度
- **四角色 AI 决策驾驶舱**：数据分析师 + 经营参谋 + 风险审计员 + 规则主持人协同输出经营建议
- **AI 培训考核**：RAG 问答 / 情景模拟 / 上岗考核三种模式，支持 DeepSeek 或私有化 Dify
- **微信 H5 零安装**：店员通过微信公众号 H5 即可完成每日数据填报

## 功能全景

| 模块 | 说明 |
|------|------|
| 数据填报 | 店员 H5 填报日销量、原料剩余、损耗、异常备注，唯一约束防重 |
| 管理后台 | 店员/产品管理、考核记录、ECharts 看板、CSV 导出 |
| AI 培训考核 | RAG 问答 / 情景模拟 / 上岗考核，LLM 双后端可插拔 |
| AI 经营智能 | 预测回测、损耗归因、离群检测、备货建议、购物篮关联 |
| AI 决策驾驶舱 | 四角色协同 + 证据仲裁 + 分歧检测 + 人工确认闭环 |
| 行业包引擎 | 7 预置包 + 文件/DB/兜底三级回退 + DIY 工作台 |
| 多租户多门店 | 商家-门店-角色三级隔离，SQLAlchemy 全局查询守卫 |
| 开放 API | 只读 REST + Bearer 令牌认证 + Webhook 事件外发 |
| 任务闭环 | AI findings 转任务、状态机、逾期追踪、效果回看 |

## 技术架构

```
┌─────────────────────────────────────────────────┐
│                  微信 H5 入口                     │
├─────────────────────────────────────────────────┤
│  Flask 3.x + Jinja2 + ECharts (前端)            │
│  ├── 11 路由蓝图 (auth/h5/admin/ai/multi/...)   │
│  ├── CSRF 保护 + CSP 安全头                      │
│  └── 限流 (滑动窗口原子计数)                      │
├─────────────────────────────────────────────────┤
│  业务服务层                                       │
│  ├── 行业包引擎 (industry_pack)                  │
│  ├── AI 决策驾驶舱 (decision_cockpit)            │
│  ├── 任务流 (task_flow)                          │
│  ├── 谈判引擎 (negotiation)                      │
│  └── 开放 API + Webhook (api_open / webhooks)   │
├─────────────────────────────────────────────────┤
│  数据层                                           │
│  ├── SQLAlchemy ORM + Flask-Login                │
│  ├── 租户隔离守卫 (query_guard)                   │
│  └── SQLite (开发) / MySQL (生产)                 │
├─────────────────────────────────────────────────┤
│  AI 后端 (双模式可插拔)                            │
│  ├── DeepSeek API (推荐，按 token 计费)           │
│  ├── Dify 私有化 (docker-compose 自托管)          │
│  └── 规则版 (零配置自动回退)                       │
└─────────────────────────────────────────────────┘
```

## 快速开始

### 环境要求

- Python 3.11+（`models.py` / `logging_config.py` 使用 `from datetime import UTC`，3.11 起才有；
  ruff `target-version = "py312"` 同源（当日实读 `ruff.toml:9`；本行曾写 `py311`，与配置文件不符）。
  声明版本区间见 `requirements.txt` 头部矩阵）
- pip

### 本地开发

```bash
# 1. 克隆仓库
git clone https://github.com/lxh113377/ican.git
cd ican

# 2. 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量（可选，不配则走规则版 AI）
cp 部署/.env.example .env
# 编辑 .env 填入 DeepSeek API Key 等

# 5. 初始化数据库并启动
python init_db.py
python src/app.py
# 浏览器打开 http://localhost:5000
```

### 演示数据

```bash
# 重置为 7 门店 30 天完整演示数据（1 商家 / 7 门店 / 23 账号 / 2100 流水）
python init_db.py --reset-all
```

### 免登录演示

在 `.env` 中设置 `DEMO_MERCHANT_ID=1` 后，访问 `/demo` 可直接查看四角色决策驾驶舱，无需登录。

## 数据备份与恢复演练

备份脚本 `backup_db.py` 的判据是**恢复得回来**，不是「文件复制成功」：

- SQLite 走在线备份 API（`source.backup(target)`）。直接复制正在写入的库会拿到撕裂页，
  所以产出后立刻 `PRAGMA integrity_check` + 逐表行数与原库核对，任一项不过就删掉副本、
  非零退出，且**不做旧备份清理**（失败轮不得动既有备份）。
- 源库连接以 `mode=ro` 打开：万一日志方向写反，SQLite 直接报 readonly，不会把空库灌回原库。
- MySQL 走 `mysqldump`，产物必须含尾部 `-- Dump completed` 标记才算写完（半截 dump 看着
  像正常 .sql，恢复时才知道残缺）。
- **每次备份都落一张证据卡** `<备份名>.manifest.json`（`src/backup_manifest.py` 唯一写入口）：
  记 `sha256 / size_bytes / status / error / 源库指纹 / 逐表行数 / 三项 checks / 被清理的份名`。
  对标 barman 的 `backup.info` 与 pgbackrest 的 manifest——**成败写在物件上，不是写在 stdout 里**。

```bash
python backup_db.py                    # 备份 + 当场复验 + 写证据卡（三件齐全才算成功）
python backup_db.py --keep 30          # 保留份数；0 / 非整数一律退出 2（拒绝清空全部备份）
python backup_db.py --verify <文件>    # 两层复验：字节层(integrity+行数) 与证据层(对账)
python backup_db.py --stamp <文件>     # 给历史备份补"回填态"卡：可清点，不得当可恢复凭据
python backup_db.py --selftest         # 10 条自检腿（门禁第 38 项每次实跑并数腿）
python backup_db.py --help             # 只看帮助，不执行任何备份动作
```

为什么 `integrity_check` 不够（2026-09-26 本机实测）：`backups/` 里躺着一份 **4,096 B、0 表 0 行**
的演示重置备份，`PRAGMA integrity_check` 对它照样返回 `ok`，旧版 `--verify` 因此判它
「✅ 可恢复」。空库永远恢复不出业务数据，所以复验加了**空库守卫**（0 表，或
`merchants/stores/staff/admin/products/daily_sales` 关键表全部 0 行 ⇒ 判不可恢复），
并把"留证之后备份又被改动"变成可检出的事实（卡上 `sha256` ⇄ 实盘逐字节对账）。

恢复演练（每季度一次；只在开发库上练，生产恢复须先停写并二次确认）：

1. `python backup_db.py`，记下输出的文件路径与行数；
2. `python backup_db.py --verify backups/ican_<时间戳>.db` 须退出 0
   （它会同时读 `backups/ican_<时间戳>.db.manifest.json` 做对账：卡缺失或状态非
   `SUCCESS`/`snapshot` 都判不可恢复——回填态的卡只证明字节没动，不追认"与原库逐表一致"）；
3. 行数抽查（本机无数 `sqlite3` 命令行，用项目解释器即可，实测输出 `(2100,)`）：
   `python -c "import sqlite3;print(sqlite3.connect('backups/ican_<时间戳>.db')"`
   `.execute('SELECT COUNT(*) FROM daily_sales').fetchone())"`
4. 反证（必须判坏，判不出来的校验等于没有校验）：
   `head -c 2048 backups/ican_<时间戳>.db > /tmp/torn.db && python backup_db.py --verify /tmp/torn.db; echo rc=$?`
   → 期望非零退出 + `❌ 不可恢复`。

## 变更归因与可追溯性

**谁、在什么时候、把哪条记录从什么改成了什么**，落在 `change_log` 表里，由 SQLAlchemy 的
`before_flush` / `after_flush` 监听器统一写入（`src/change_log.py`）。业务代码不调用它 ——
新增一个写点不需要"记得调一次审计"，这是对标 Odoo `log_access` 四列自动挂载的取舍。

- **记值的是主数据**：产品、门店、账号、行业包定义等 11 张表记字段级前像/后像。
- **不记值的是业务明细**：营业流水、考核记录、AI 台账只记「哪张表的哪条 id、什么动作」，
  字段值一律不入库（安全红线：商家业务明细不得暴露给平台管理员）。
- **凭据列永远不进审计**：`password` / `token_hash` / `secret_salt` / `code_hash` 等在名册层
  就被拒绝，`validate_registry()` 与门禁第 40 项双向核对（写错列名不会报错、只会静默记 null，
  所以这条必须机器化）。
- **删除不再是无痕动作**：老板点「删除产品」时，若该产品名下还有历史流水，系统**归档下架**
  而不是抹掉目录行；确实没有引用才物理删，且删除行带整条前像。
- **入口**：管理后台 → 产品管理 → 「变更记录」（`/admin/changes`）。平台管理员只看平台级行。

### 毛利率不再被目录动作改写

`daily_sales.product_cost` + `cost_basis` 是**提交那一刻**的成本快照。改前实测（同一份营收
4851.0 元、流水一行未动）：

| 只做目录动作 | 改前历史毛利率 | 改后 |
|---|---|---|
| 删除一个在售产品 | 62.15 → 61.93（−0.23） | 不动 |
| 给产品改名 | 62.15 → 63.66（+1.51） | 不动 |
| 改一次成本价 | 62.15 → 68.65（+6.49） | 不动 |

`cost_basis` 三档：`snapshot`（提交时按到的）、`backfill`（p006 补丁回填的，**不冒充当日真实
成本**）、`none`（当时该产品没有成本价，毛利率如实不计这一行；这类行会回退按目录当前值补算，
在看板上与快照档分开计数）。

```bash
python backup_db.py                    # 备份 + 证据卡
python init_db.py --if-empty           # 存量库补两列 + 建 change_log 表（幂等）
```

## 端点授权面与默认拒绝

**改造前实测（2026-09-27，权威副本 `src/`）**：`url_map` 86 条规则里 **16 条不带任何视图级守卫装饰器**；
盘上没有任何「公开端点名册」（`PUBLIC_*` 全仓 0 命中）；L4 全文只有 2 处 `url_map` 使用且都不是授权断言。
⇒ 结论不是「现在越权了」，而是「**漏挂装饰器不会有人知道**」：新增端点忘挂守卫 = 静默公开，无判据会红。

**上游口径**（同轮 `gh api` 取回原文）：Odoo `ir_http.py:392` 按路由声明的 `auth` 单点派发
（四种模式 `:315/358/364/369`，连 CORS 预检都要显式写 `auth='none'`）；InvenTree `settings.py:521-522`
把 DRF 默认权限钉成 `IsAuthenticated`；Frappe `whitelist(allow_guest=False)` 默认不对游客开放。
⇒ 三家共同点：**开放是声明出来的，不是「没人拦」**。

**现在怎么做**：`src/authz_ledger.py` 是端点授权的唯一声明源，三档名册 + 三方对账。

| 档 | 含义 | 条目 | 谁来拦 |
|---|---|---|---|
| `public` | 匿名可达 | 10 | 名册逐条带 `reason`，无 reason 即门禁判红 |
| `bearer` | 开放 API 只读数据 | 7 | 函数体必须命中 `_guard()/_authenticate()` + scope 校验 |
| `inline` | 凭据自助管理 | 4 | `login_required` + 函数体内商家归属与角色校验 |
| （其余 66） | 必须挂视图级守卫装饰器 | — | 门禁 AST 对账，漏挂即「未登记」 |

三方对账 = 声明侧 `EXEMPT` ⇄ 实现侧 AST 索引（守卫装饰器 + 函数体调用） ⇄ 运行侧 `app.url_map`。
**运行期默认拒绝**：`src/app.py` 的 `@app.before_request` 对「既不在名册也无守卫」的端点直接 403，
没有开关可关（关掉它等于把洞重新挖开）。

**新增端点的规矩**：挂 `login_required`/`role_required(...)` 即可，无需改名册；
只有确实要匿名可达时，才在 `EXEMPT` 加一条并写 `reason`（`/admin/*`、`/h5/*` 声明成 public 会被红线拦住）。

**怎么查（不开浏览器、不进后台）**：

```bash
python -m src.authz_ledger --check   # 三方对账，红即列出差集；末行 [GATE:authz-pass|fail]
python -m src.authz_ledger --dump    # 逐端点打印：路由 / 档位 / 守卫装饰器 / 豁免理由
```

**判据在哪**：门禁第 41 项（BLOCK，名册分母 ⇄ 独立计数 ⇄ 接线 ⇄ 公开档红线）；
L4 `S33` 段 25 条含**逐端点匿名实测**（探测数 = 规则数，不抽样）、三类角色越权腿、
7 类变异反证与还原自证。实测细节见 `../开源对标分析报告-2026-09-27-第27轮-*.md`。

## 数据擦除与账号注销

**改造前实测（2026-09-27）**：`注销|delete_account|erase` 在 `src/` **0 命中**；`routes` 里
`db.session.delete` 只有 2 处且都是产品行。**停用有开关（`is_active`），删除没有出口**，
而 `/register` 是匿名可达的公开档 ⇒ 开放注册进来的商家永远删不掉。

分母才是重点：ORM 元数据枚举 18 张表，**只有 8 张挂 `merchant_id`**。`daily_sales`
（演示库 2,100 行，全库最大业务表）、`quiz_records`、`products`、`metric_defs` 只有
`store_id`/`staff_id`/`pack_id` ⇒ **按 `merchant_id` 直觉去擦会整表漏掉营业流水**，
而既有 41 项门禁 / 冒烟 / 漏洞 / L4 里没有一条会因此变红。另实测：`grep merchant_id` 得 14 处，
与元数据的 8 张对不上 —— 擦除分母必须由元数据枚举得出，不能由 grep 命中数得出。

**上游口径**（同轮 `gh api` 取回原文逐行核过，行号见 `开源对标分析报告-2026-09-27-第28轮-*.md` §2）：Saleor
`saleor/graphql/account/mutations/account/account_delete.py:46/:54/:105` 把删除做成
**声明了 webhook 事件的可校验动作**；PostHog `posthog/tasks/delete_persons.py:23/:31` 是
**有界分块、失败只重试失败者**；Frappe `frappe/core/doctype/user/user.py:381-386` 守
「不可停用的最小集合」并连带清会话。

**现在怎么做**：`src/erasure.py` 是归属图与擦除的唯一声明源。

| 档 | 含义 | 表数 | 会红条件 |
|---|---|---|---|
| `merchant_id` | 表自带商家列，直取 | 8 | 声明该档却没有 `merchant_id` 列 |
| `store_id` / `store_or_staff` | 经门店/店员连坐 | 3 + 2 | 两个归属列都没有 |
| `email_list` | 只认注销时显式给出的注册邮箱 | 1 | 不给邮箱 ⇒ **如实报残留**，不静默跳过 |
| `declared_none` | 不属任何商家（预置行业包、迁移账本） | 3 | 缺 ≥20 字理由即判红 |
| `root` | `merchants` 行本身 | 1 | 不在删除顺序最后一步即判红 |

四条护栏，缺任一条就**抛错而不动盘**（"200 但什么也没删"会被读成删除成功）：

1. **备份凭证**：`execute()` 前先跑 `backup_proof()` —— 复用第 25 轮备份证据链的
   `integrity_check` + 逐表行数 + 空库守卫。0 表 0 行的"备份"会被拒（`integrity_check`
   对空库照样返回 ok，这正是当年那份 4,096 B 备份被判「✅ 可恢复」的形态）。
2. **不可擦最小集合**：平台租户与演示根行直接拒。
3. **有界分块**：单批 500 行、单表最多 400 批；每批一条聚合留痕写进 `change_log`
   （只记条数与 id 范围，**不记商家业务值** —— 第 26 轮红线同口径）。留痕一律记成
   **平台侧档（`merchant_id` 为 NULL）**：`change_log` 自己也在归属图里，若按被擦商家记账，
   这个动作会把自己的账一起擦掉，"谁在什么时候擦了这家店"恰恰在最需要它时无从回答
   （PostHog 把 `LOG_ACTIVITY` 归进 `STEPS_AFTER_DELETION`，`bulk_deletions.py:53-64`：
   删完的人不可能再被 resolve，所以证据不能随人被删）。这类行在 `/admin/changes` 的平台侧视图可见。
4. **复扫自证**：擦完返回逐表残留读数，空 dict 才叫擦干净；不做成布尔。

**出口**：`POST /admin/platform/merchants/<id>/erase`（`ROLE_PLATFORM` + CSRF + 逐字确认短语
+ 二次勾选 + 一次性 nonce + 备份路径）；`GET` 同一地址只回**零写入预览**（逐表将删多少行）。
该端点同时登记进 `routes/admin.py` 的蓝图级平台白名单 `_PLATFORM_ALLOWED_ENDPOINTS` ——
那条兜底是给"平台管理员不得看门店业务明细"用的，**它同样会把平台自己的入口拦成永久 403**；
门禁第 41 项 ⑧ 逐条对账「`role_required(ROLE_PLATFORM)` 的端点 ⇄ 白名单」，漏登记即红。
未做后台页面是明确取舍：新增模板要牵动设计体系三副本与演示快照重建，与该动作的低频不成比例，
已登记为待裁项（L62）。**CLI 刻意不提供 `--execute`** —— 只读入口用于体检，不可逆动作只走受控出口。

```bash
python -m src.erasure                 # 归属图 ⇄ ORM 元数据双向对账，末行 [GATE:erasure-pass]
python -m src.erasure --plan 2        # 只读预览：商家 2 逐表将删多少行
python -m src.erasure --verify 2      # 复扫：逐表残留（空 = 擦干净）
```

判据在门禁第 42 项（BLOCK，编号 1..42）与 L4 `S34` 段；`S34` 含 5 类变异反证
（漏登记 `daily_sales` / 清空归属图 / 文件级删该档 / CLI 开后门 / 留痕记回被擦商家）
与逐字节还原自证。

## 业务日切与时区

**改造前实测（2026-09-27）**：全仓 AST 数到 **45 处**裸 `date.today()` / `datetime.now()`
（grep 口径 67 行，含注释与带参形态 —— 分母按调用点而不是按命中行），而时区/日切方向
在门禁与三套测试里 **0 条判据**。开发机在 UTC+8，进程时钟与业务日恰好重合 ⇒ 缺陷被掩盖；
生产端 PythonAnywhere 免费层服务器时区是 **UTC** ⇒ 每天北京时间 **00:00—08:00 这 8 小时内
服务端的"今天"是昨天**：店员早上补交写成前一天、看板"今日"空白、
`uq(staff,date,pack,product)` 判重判在错的日子上。`src/ai_quota.py` 的注释早已自认同一形态会
算错当天用量，但此前只修了那一个出口。

**上游口径**（同轮 `gh api` 取回原文逐行核过）：Django `django/utils/timezone.py:187-198`
的 `localdate()` 是"先转当前时区再取 date"；Frappe `frappe/utils/data.py:388-390` 的业务时区
取**系统设置**而非进程时钟，并在 `:421-433` 留测试注入口；InvenTree
`InvenTree/settings.py:741-747` 配了 `TIME_ZONE` 就当场 `ZoneInfo()` 校验、非法即 `ValueError`
⇒ "配错"必须是启动失败，不是静默按 UTC 跑。

**现在怎么做**：`src/business_calendar.py` 是"今天"的唯一真相源。

| 口径 | 用法 | 说明 |
|---|---|---|
| 业务日 | `cal.business_today()` / `cal.business_now()` | 填报默认日期与查重键、看板与多店异常窗口、任务默认截止与逾期、CSV 文件名、开放 API 缺省日期、演示播种、开发态日志行 |
| 技术戳 | 显式 `datetime.now(UTC)` / `now(timezone.utc)` | 入库的 naive-UTC 列语义不变（**零 DDL、零历史改写**），台账/证据戳一律带时区 |
| 业务时区 | `ICAN_BUSINESS_TZ`（默认 `Asia/Shanghai`） | 必须是 IANA 名；填了就必须校验得过，否则 `create_app()` 直接 `ValueError` |

线上单靠 HTTP 就能核对：`/healthz` 的 `calendar` 字段回 `business_date / business_tz /
resolved_via / cross_day_now`。tz 数据库缺失时（Windows 或无 `tzdata` 的环境）走
`FIXED_OFFSET_FALLBACK` 的**显式**固定偏移（中国 1991 年起无夏令时，+08:00 对
`Asia/Shanghai` 精确），此时 `resolved_via` 会变成 `fixed-offset` 并在线可见 —— **禁静默降级**。
`requirements.txt` 已补 `tzdata`（`THIRD-PARTY-NOTICES.md` 同步登记），让生产走主路径。

判据：门禁第 43 项（BLOCK，编号 1..43）做「裸时钟点 ⇄ 业务日历调用 ⇄ 显式 UTC」三类读数对账
（裸点必须为 0 或带 ≥20 字理由的豁免、业务日历调用 ≥25 处反向地板、豁免名册陈旧即红、
`/healthz` 与 `create_app` 接线只认 Call 节点）；L4 `S35` 段 21 条含 4 类变异反证与逐字节还原自证。

## 写路径并发与丢失更新防护

后台是多人使用的：老板和店长可能同时改同一件产品。改前本仓**没有任何一处**回答
「我看到的那一行还是不是现在这一行」——实测：A 提交单价 11.00 得 200，
B（表单在 A 之前打开）提交 12.00 也得 200 ⇒ 终值 12.0，**A 的编辑无声消失**；
归因链 `change_log` 两行都记了，但"带冲突痕迹的行 = 0"（记了 diff ≠ 知道自己在覆盖别人）。
`src/conflict_guard.py` 补的是这个判定：

- **整行表单覆盖**（产品内联编辑）：渲染时把行指纹写进隐藏字段 `_occ`，保存前服务端重算比对，
  不一致 ⇒ 回 **409 `error:"conflict"`** + 机读 `fields:["unit_price", …]` + 人读文案「这一行在你打开之后已被他人修改（字段：…）」，
  **不改一个字段、不落一行数据**；凭据由表单携带 ⇒ 服务端不留跨请求状态，多 worker 与重启都不失效；
- **单列上下架**（产品/店员/门店）：走比较并交换 —— 客户端主张"我看到的值"，
  `UPDATE … WHERE id=? AND is_active=:expect`，命中 0 行即被拒。
  期望值**必须来自客户端**：服务端自己刚读的值当期望，WHERE 恒真，等于什么都没防
  （本模块第一版就是这样，自检时才被抓出）；
- **先查重后插入**（建账号 / 建门店 / 注册）：唯一约束竞态一律转成可读提示，不冒泡 500。

口径边界（写清楚，避免被当成万能锁）：
- 缺凭据的请求**放行并计数**（老页面/缓存/直接 API 调用都在这一类）。这是"先量误报率再接线"的
  刻意选择，计数在 `/healthz` 的 `conflict` 段与门禁第 45 项回执里可见；
- 冲突被拒也会留痕：`change_log` 记一条 `action=conflict_rejected` 的行（只记**表/主键/字段名**，
  不记任何业务值），且这条留痕自己提交 —— 被拒的请求不会走到业务 commit，
  留在同一事务里等于随请求回滚掉（实测第一版就是"409 发了、行没了"）；
- **没有"跳过冲突检查"的环境开关**：那等于把洞留给生产；
- SQLite/免费层没有可用的行级锁，所以采用「客户端带版本」这一侧的做法
  （与 Frappe `check_if_latest()` 同形态），而不是假装 `SELECT … FOR UPDATE` 有效。

门禁第 45 项按 AST 枚举这三类出口并要求"两端都在"：端点真的读 `_occ`、模板真的发出 `_occ`
—— 只有一端存在时校验会永远走"缺凭据放行"分支，那是假守卫。

## 开放 API

项目提供只读 REST API，供外部系统集成。详见 [openapi.yaml](openapi.yaml)。

```bash
# 1. 管理端签发令牌（需 session 登录）
POST /api/v1/manage/tokens

# 2. 使用令牌调用数据端点
curl -H "Authorization: Bearer ican_xxxx" https://your-host/api/v1/dashboard
curl -H "Authorization: Bearer ican_xxxx" https://your-host/api/v1/stores
curl -H "Authorization: Bearer ican_xxxx" https://your-host/api/v1/sales/daily?date=2026-09-24
```

### Webhook 事件

支持 `sales.submitted`（填报提交）和 `task.status_changed`（任务状态变更）两种事件。

```bash
# 管理端注册 Webhook 订阅
POST /api/v1/manage/webhooks
{
  "url": "https://your-server.com/callback",
  "event_types": "sales.submitted,task.status_changed"
}
```

出站事件带 `X-ICAN-Signature: t=<unix>,sha256=<hex>` 头，签名串为 `"<t>." + body`，
使用订阅时返回的签名密钥做 HMAC-SHA256。接收方须同时校验签名与时间窗（默认 ±300 秒）：
只签 body 的写法可以被抓到一次后无限重放，因此时间戳必须进签名串。

### 出站目标守卫（订阅时即判定）

回调地址由商家提供，而这个请求是**我们的服务器**发出去的 —— 不加约束等于把内网交给
每一个已认证的账号（`http://127.0.0.1:5000/…`、`http://169.254.169.254/latest/meta-data/`、
`http://2130706433/` 都是同一件事的三种写法）。`src/outbound_guard.py` 的口径：

- **按解析结果判，不按字符串形状猜**：先解析（可注入 resolver），任一地址非公网可路由即整条拒；
  解析失败或解析为空同样拒 —— 把"没解析出来"读成"应该没问题"是 SSRF 的常规死法；
- 端口只放 `80/443`，协议只放 `http/https`，带 userinfo（`http://u:p@host/`）一律拒；
- `localhost` / `.local` / `.internal` / 云元数据域名按名字先挡一层；
- **http 按已判定的 IP 直连并保留原 Host 头**，掐掉"校验时一个 IP、连接时另一个 IP"的
  DNS-rebinding 时差；https **不钉 IP**（钉了会让证书主机名校验失效，那是换一个洞），
  改为临发前重新解析；
- 订阅创建时就判（`subscribe_webhook` 直接报错，库里不留脏行），投递时**再判一次**。

想在本机/内网联调回调 ⇒ 把接收端放到公网（或临时用公网回调调试服务），或改用主动拉取的
只读 API；不提供 `WEBHOOK_ALLOW_PRIVATE` 这类开关 —— 为可测性留的后门就是生产的洞。

### 投递留痕与重试

`webhook_delivery` 表**每次尝试一行**：`attempt` / 状态码 / 错误类 / 是否被守卫拦下 /
下次重试时间 / 耗时；`payload` 只存 sha256 摘要，不存正文（商家业务明细不进第二份副本）。

- 退避：第 1 次失败后 5 分钟重试，第 2 次后 30 分钟，最多 3 次（`ATTEMPT_LIMIT`）；
  重试用同一个 `delivery_id` 递增 `attempt`，所以"一个事件投了几次"是可数的；
- 被守卫拦下的记录 `blocked=True` 且**不进重试队列** —— 那是策略判定，不是网络故障，
  重投只会再撞一次同一堵墙；
- 同一订阅连续 5 次投递失败 ⇒ 自动停用（`is_active=False`），该动作本身被归因链
  `change_log` 记成字段级变更，不需要谁手写留痕；
- 出口：`python -m src.webhooks --retry-due`（有界跑一轮，读数含 `disabled`）与 `--stats`。
  免费层没有常驻 worker，**刻意不自建调度器** —— 一个没人知道的调度器比没有调度器更危险。

错误响应形态（对标第 22 轮实测改造）：落在 `/api/v1/` 上的**全部**错误一律返回
`application/json`，形如：

```json
{"ok": false, "error": "not_found", "message": "资源不存在。", "request_id": "3d8103a6"}
```

`error` 是机读码（枚举见 `openapi.yaml` 的 `Error.error`，与 `src/api_errors.py` 的
`ERROR_CODES` 由门禁第 34 项双向对账）；`message` 给人看、措辞不承诺稳定，**不要拿它做判断**；
`request_id` 与响应头 `X-Request-Id` 同源，报障时给这一个串就能在 JSONL 里定位该请求。
浏览器路由（H5 与后台页面）仍返回 HTML 错误页 —— 分界按**路径前缀**，不按 `Accept` 协商，
这样忘记带 Accept 的脚本也拿得到可解析的 JSON。改造前实测 6 条 API 错误腿里有 5 条吐 HTML。

覆盖率地板：`python run_coverage.py` 在三套件下收集，`.coveragerc` 的 `fail_under = 80`
（实测基线 82%，留 2 个点余量）。未达标时该命令以非零退出，门禁第 35 项看守这条传播链。

分页与配额：列表端点支持 `limit`（默认 100，上限 500）与 `cursor`（上一页的
`next_cursor`），响应回 `has_more` / `next_cursor`；每个 `/api/v1` 响应带
`X-RateLimit-Limit` / `-Remaining` / `-Reset`，429 另带 `Retry-After`。

## 行业包

| 行业 | 标识 | 产品词 | 数量单位 | 特色字段 |
|------|------|--------|----------|----------|
| 奶茶 | `milk_tea` | 产品 | 杯 | 原料剩余 |
| 水果生鲜 | `fruit` | 品类 | 斤 | 鲜度等级 |
| 便利店 | `convenience` | 商品 | 件 | 临期预警 |
| 烘焙 | `bakery` | 产品 | 个 | 保质期 |
| 美妆 | `beauty` | 产品 | 件 | 批号 |
| 宠物 | `pet` | 产品 | 袋 | 库存周转 |
| 服装 | `apparel` | 产品 | 件 | 尺码 |

商家可在 DIY 工作台自定义表单字段、考核题库和看板维度。

## 依赖供应链与已知漏洞

本项目是**给别人的门店管数据**的服务，所以"依赖钉住了没有"和"钉住的那个版本安不安全"是两件事：
改前 45 项门禁里没有一个看过后者（`pip-audit` / `osv` / `GHSA` / `dependabot` 关键词全仓 **0 命中**）。
本轮实测把两面都补上：

- **谁在更新依赖**：仓库根 `.github/dependabot.yml`（pip 两个生态 + github-actions，周更、
  `cooldown: 7 天`、major 一律不自动合入）。⚠️ 一条诚实的自曝：2026-09-18 就写过一份 dependabot，
  但它一直躺在 `iCAN大学生创新创业大赛/.github/` 子目录里，而 **GitHub 只读仓库根的 `.github/`**
  ⇒ 它从未生效过；09-24 的处置是把交付副本里那份"根上不存在"的配置删掉（`9172ab7` / `c80f3e6`），
  **删掉宣称而没有补上能力**。门禁第 46 项现在按"根上没有 ⇒ 判红并点名从未生效"钉住这个区分。
- **有没有已知漏洞**：`python check_dependency_audit.py` 用 `pip-audit` 扫**两面** ——
  声明面（`requirements.txt` 解析）与环境面（当前 venv）。退出码是类别码：
  `0` 干净 / `1` 有未豁免漏洞 / `2` 工具不可用（UNVERIFIED，不静默绿）/ `3` 网络不可达 / `4` 台账不合法。
  本轮实测印证了两面不可互相替代：声明面 **0 条**，环境面 **10 条 / 2 包**
  （`pip 26.1.2` → PYSEC-2026-3721 / CVE-2026-13346；`pypdf 6.14.2` → 8 条，
  而 pypdf **未被声明、无人 import、无包依赖它** = 孤儿包，只有环境面看得见）。
  处置是升版与移除（pip → 26.2.1、卸载 pypdf），不是把漏洞写进豁免。
- **豁免是有期限的决定**：`DEPENDENCY-AUDIT-EXEMPTIONS.json` 每条必须给真实漏洞 id
  （PYSEC/GHSA/CVE 形状）、**≥20 字理由**与 ISO 到期日；过期即红、id 不在实测集合内即红（幽灵豁免）。
  台账由门禁**自己重读重判**，不采信审计回执里的自报字段（本轮负对照抓出：改台账文件曾不影响判定）。
- **审计必须接进阻断链**：CI 的"交付链守卫 D"真跑该脚本（工具钉 `pip-audit==2.10.1`，注释掉的步骤行
  不算接线），并把 `DEPENDENCY-AUDIT.json` 随交付副本走；门禁第 46 项要求它**存在、两面都在场、
  不超过 14 天**，缺/过期/只扫一面都判红并给出可抄的重扫命令。
- **钉版纪律**：`requirements.txt` 每条都必须带上界或精确钉（本轮补掉 `coverage`、`tzdata` 两条无上界）——
  pandas 2→3 已实测会打穿 CSV 护栏，"某次重建静默跳 major"不是假想风险。

## 质量保障

```bash
# 全量质量门禁（静态检查 + 冒烟测试 + 漏洞回归 + L4 断言）
python quality_gate.py

# 快速检查（仅静态，跳过测试）
python quality_gate.py --fast

# 冒烟测试
python smoke_test.py

# 漏洞回归测试
python test_vuln_regression.py

# L4 断言测试
python test_l4_assertions.py
```

### 三副本同步：一个交付动作只留一个出口（门禁第 52 项）

同一份源码在盘上有三处：权威源 `iCAN大学生创新创业大赛/`、部署包 `deploy-free/`、
提交副本 `交付物/提交包/工程佐证材料/05-源代码全文/`。三者靠复制维持一致，而
"文件在"不等于"字节一样"，所以同步由**一个**脚本负责：

```bash
python tools/frontend_sync_check.py          # 只读检查（三类差异逐条点名）
python tools/frontend_sync_check.py --fix    # 权威源 → 副本，复制后重新扫描复核
```

- **面从声明现取**：比对清单 = `quality_gate.ROOT_DELIVERED` ∪ `check_delivery_drift.EXTRA_TRACKED`
  ∪ `set_version.CARRIERS` 里的仓内 md ∪ 工具自身，一处都不手抄。
  任一声明读不出来 ⇒ 退 2 并点名，**绝不**把"我没取到面"写成"面里没有差异 = 通过"。
- **三类各出一个数**：`MISSING / DIFF / EXTRA`，全零才 `[PASS]`。`EXTRA` 只报不删——
  副本里多出来的文件可能是别人的现场，删除必须人工复核。
- **为什么本轮收这个口**：此前该脚本只管 `src/ → deploy-free/src/` 一格，根级交付件
  （`quality_gate.py`、`restore_db.py` 等 31 项）与 05 副本完全不在射程内，第 31—36 轮
  每轮都靠一个**不在仓库里**的一次性脚本补另一半（`git ls-files` 命中 0）。
  一个交付动作两个实现、其中一个不受版本控制 = 换台机器就断链。
  同轮还抓到错位的一半：只读检查器 `check_delivery_drift.py` 已经在管"白名单外那批文件
  三副本必须一致"，可同步侧完全不知道它 —— **查得到、修不了**。
- 门禁第 52 项因此是三件事的合取：写出口枚举 == 1（AST 现算，禁手写名单）、
  面 ⊇ 三处声明并集（真调用工具的 `declared_files()`，不读它对自身的描述）、
  本地闸已接线且源码里没有仓外绝对路径。

### 可维护性预算（规模与复杂度只降不升）

`MAINTENANCE_BUDGET.json` 是门禁第 37 项的唯一输入，逐文件记录行数预算与 C901 复杂度基线：

- **预算区间**：`实测行数 ≤ 预算 ≤ ceil(实测 × 1.10)`。下界拦"文件悄悄长大"，上界拦"把预算写成天文数字"。
- **复杂度基线**：`ruff --select C901 --config lint.mccabe.max-complexity=10 src` 的超限函数数
  只准降不准升；已经降了但基线仍写高值同样判红（台账失真也是失效）。
- **放宽必须显式**：`python quality_gate.py --write-budget --reason="为什么"`。
  不带 `--reason` 重跑只会沿用旧预算，让超长的那一项继续判红——放宽是一次人工决定，
  不是一个默认行为，理由会写进表里可追责。
- **表随副本走**：该文件登记在 `ROOT_DELIVERED` 三副本比对面内，避免出现"权威副本的表比代码旧"。

预算表在冻结窗口内只做"不许再长"，`test_l4_assertions.py`（约 8,000 行）的拆分是拆分排期里的
独立工程，不与本棘轮混做。

## 备份的可恢复性与提交阻断链

**备份只有被载回并断言过，才算备份。** 2026-09-27 第 33 轮实测：本仓写了 11 份 `backups/*.db`，
但 `def restore` 只有行业包的两处、`iterdump` 0 处 ⇒ **数据库从来没有载回路径**，
门禁第 38 项那句「回填态不得当可恢复凭据」没有兑现件。现在补上：

```bash
python restore_db.py --selftest                                    # 内置往返四腿（门禁第 47 项复跑同一条）
python restore_db.py --backup backups/<最新>.db                    # 恢复到 data/restore_check_<ts>.db，不碰在用库
python restore_db.py --backup backups/<最新>.db --into data/ican.db --force
```

三条硬规矩（都是实测换来的）：

1. **默认绝不覆盖在用库**。目标已存在且非空 ⇒ `rc=3` 拒绝，并明确写「这不算失败，算拒覆盖」；
   要覆盖必须 `--force`，且覆盖前先落**回滚锚点** `*.pre-restore-<ts>.bak`（对偶 discourse
   `lib/backup_restore.rb:23 rollback!`）。锚点建不出来就直接中止，绝不裸覆盖。
2. **源一律只读打开**。在线备份 API 的方向是 `source.backup(target)`；写反时若源可写，
   会把空库灌回真库（本机 2026-09-26 实测踩过）。所以恢复路径上源必须 `mode=ro`，
   门禁第 47 项按源码字面核这三件事：方向、只读、退出码直传。
3. **退出码是类别码**：`0` 成功 / `1` 校验或对账不过 / `2` 输入缺失（不存在、0 字节、非 SQLite）/
   `3` 拒覆盖 / `4` 证据件写不出去。「没测到」绝不塌成 `0`；失败路径本身也被实测过不会抛 traceback
   （本轮篡改样本第一次就是把异常直接穿出来，随后补了 `integrity_check` 的异常边界）。

每次真恢复都往 `RESTORE-EVIDENCE.json` 追一条（`backup` / `backup_path` / `backup_sha256` /
`row_total` / `key_rows` / `integrity` / `forced` / `rollback_anchor`）。门禁第 47 项**当场复算**这些账：
sha 与盘上字节不符、`row_total=0`、`--force` 却没留锚点，都判红；台账里只有自检腿而没有
`mode=restore` 也判红——**能力存在不等于能力用过**。演练夹具一律带 `--no-ledger`，不混进真账。

**提交阻断链（第 48 项）**：GitHub Actions 因账号付费额度拿不到 runner
（job `108580122204`：`startedAt=None / steps=0`，注解原文
"The job was not started because recent account payments have failed…"），
所以远端闸结构性不可用，本地必须是第二道闸：

```bash
python <技能仓>/A-project-handoff/scripts/greencheck.py bootstrap --repo-dir . --apply --install-hook
git config core.hooksPath .githooks        # 或 greencheck 直接装到 .git/hooks/pre-push
```

门禁第 48 项核三件事：`.ci/contract.json` 在库且**被 git 跟踪**、其中至少一项真的跑
`quality_gate.py`（**注释掉的行不算接线**，与第 46 项 CI 那把尺同源）；pre-push 钩子在位并按路径执行门禁；
跳过推送必须往 `.ci/prepush-skips.jsonl` 写一行带 `reason` + `ts`（逃生门要有计量，不禁止跳过）。

## 迁移补丁能不能撤（可逆性是三档，不是一句"应该能撤"）

2026-09-27 第 35轮实测：`src/schema_ledger.PatchRecord` 只有 `pid/title/kind` 三格，
`_PATCH_IMPLS` 只有正向执行体，`revert` / `PRAGMA user_version` 在 `src` + `init_db` 里 **0 命中**
⇒ 盘上没有任何一处回答"这条补丁撤得回来吗、代价是什么"。上游两家都是反着做的：
Django `RunPython` 缺 `reverse_code` 时往回跑直接 `raise NotImplementedError`
（`django/db/migrations/operations/special.py:205-206`），Alembic 模板里
`upgrade()` / `downgrade()` 成对存在（`alembic/templates/generic/script.py.mako:21/:26`）。

本项目不换迁移框架（红线：免费层单进程 + 存量库在跑），只把**判据形状**搬过来：
每条补丁必须自证档位，且声明与实现、文档三方同源（门禁第 50 项）。

| 档 | 含义 | 命令 |
|---|---|---|
| `auto` | 有可执行的反向动作（`init_db._PATCH_REVERSERS`） | `python init_db.py --rollback=p006_cost_backfill --yes` |
| `manual` | 只能整库回退到备份锚点 | `python init_db.py --rollback=<pid> --via-backup --yes` |
| `none` | 语义上无法反推（回填前的归属本来就是 NULL） | 同上，没有局部撤 |

三档只有一条是 `auto` 不是保守，而是**能精确撤的前提是当初留了标记**（`cost_basis='backfill'`）；
反向动作退回的是补丁前的真实状态 `cost_basis='none'`（该列 `NOT NULL DEFAULT 'none'`），
第一版写成了 `NULL`，被 L4 的真演习当场撞成 `IntegrityError` ⇒ **"声明 auto"没有跑得通的反向动作，
本身就是第二句假话**。撤成功后摘掉账本行 ⇒ 下次 `--if-empty` 会重新执行它，这条闭环是可测的。

同一轮补的第二个工具：`python set_version.py --check` / `--apply --version X.Y.Z`。
第 34 轮版本 bump 时我 grep 到了 4 个载体的清单仍漏改一个，代价是一整轮重跑；
现在载体清单写死在这个文件里，逐格读回、任一不唯一即整体拒绝（不做部分成功）。

### 那一条 `--via-backup` 挑的是哪份备份（2026-09-28 第 36 轮）

第 35 轮把档位如实分成 `auto 1 / manual 4 / none 2` 之后，"事故时要三条命令"这件事本身成了新洞：
`manual`/`none` 档要人自己**挑一份可用的备份**。当场实测：`backups/*.db` 14 件，带证据卡的只有
1 件，而**最新那件恰恰没有证据卡** ⇒ "挑最近一份"这句口头指令在这台机器上是没法执行的。

现在选择收进一处（门禁第 51 项核它）：

```bash
python restore_db.py --list-backups [--deep]      # 只读：逐件判资格，不动任何库
python init_db.py --rollback=p005_cost_snapshot --via-backup --yes    # 自动挑够格且早于该补丁的那份
```

一份备份要同时满足：文件名里有可信时刻（排序按**名字里的时刻**，不按 mtime——复制与同步盘会改
mtime）、有 `.manifest.json` 证据卡、龄在 `--max-age-h`（默认 72 小时）内、深核时
`integrity_check` 为 ok。任一不满足就给出**那一条**理由；全都不合格 ⇒ 拒绝，而不是将就选一个。
人仍可显式 `--backup <路径>` 指定一份不够格的，那是人在替工具担判断（保留逃生面，但工具不背书）。

同轮被自家判据抓出的第二个缺陷是**基准**：账本 `applied_at` 是 naive UTC，备份文件名的 14 位是
本地钟面（+08），直接比会差 8 小时（实测同一件事读作 `01:46` 与 `094600`）。换算只许走
`src/business_calendar.py` 的 `from_utc_naive()` / `from_wall_clock()` 两个原语，
文件名戳统一由 `restore_db.wall_ts()` 产出。

## 备份留几份 ⇄ 能载回哪份：三条尺必须见面（2026-09-28 第 38 轮）

上一条讲"够不够格载回"（按小时判，`MAX_AGE_H=72`）。这一条讲另一个问题：**盘上到底留了多少、
谁在管它们**。当场实测 `backups/` 人口 14 件，分属三族，而**做"留几份"这个决定的地方有三处，
彼此从不对账**：

| 尺 | 值 | 在哪 | 管哪些文件 |
|---|---|---|---|
| 份数上限（CLI 工具） | `backup_db.DEFAULT_KEEP = 30` | `backup_db.py` 的 `glob('ican_*.db' / 'ican_*.sql')` | 6 件 |
| 份数上限（平台重置页） | `src/pack_demo.BACKUP_KEEP = 10` | `pack_demo._prune_backups()` 按 `demo-reset_*.db` | 3 件 |
| 资格窗口（小时） | `restore_db.MAX_AGE_H = 72` | 只判"能不能载回"，不删任何东西 | 全部 14 件 |

于是出现一个谁都没错、合起来却没人看得见的局面：**30 份 × 一日一份 ≈ 720 小时，
而"能载回"只看 72 小时** ⇒ 账面留着 30 份，够格的却只有 1 件；另有 **5 件族外无主**
（`PH2-*` 4 件 + `ican-db-before-cisim-*` 1 件，最老 68 天）不在任何淘汰通道的射程里。
上一轮我登记的 M114 写的是"射程只认一种命名"，本轮实测**否证了一半**：
每条通道各自认自己的族没毛病，真缺口是**没有跨族视角，也没人把份数尺和小时尺放在一起**。

```bash
python restore_db.py --retention          # 只读体检：人口 / 每族件数与最老件 / 族外具名 / 三把尺对账
python restore_db.py --retention --json   # 同一份读数的机器形态（门禁第 53 项 import 的就是它）
```

三条硬约定：

- **族和尺都从生产者源码现取**：模式取自 `backup_db` 真正传给 `glob.glob()` 的字面量，
  `demo-reset_*` 由 `BACKUP_PREFIX` 常数 + `_prune_backups()` 里的 `endswith('.db')` 派生。
  读不出来就 `RetentionError` 判红，**绝不允许**把"我没取到射程"写成"族外为零 = 健康"。
- **新增"留几份"的决定必须登记**：任何模块在模块级引入名字含 `KEEP` 的常数（按 `_` 切词，
  `SLEEP_KEEPALIVE` 这类不误伤），却没被 `retention_channels()` 认到 ⇒ 门禁第 53 项当场红。
  这条是可自愈的：补通道或显式登记，一次正常提交就能变绿。
- **体检只读**：`retention_*` 函数体里出现任何 `remove/unlink/rmtree` 都判红
  （"保留体检"下一步最容易顺手加删；删除要走人工复核的专用通道，那条通道尚未建模 = M115）。

上游对照（本轮 `gh api` 取回原文逐行读过）：`discourse/discourse`
`lib/backup_restore/backup_store.rb:36-43`（计数尺）与 `:45-53`（时间尺 `delete_prior_to_n_days`，
`:47` 先验窗口为正、`:48` 再过 `cleanup_allowed?`）—— **两把尺写在同一个类里，各自带守卫**；
`frappe/frappe` `frappe/utils/backups.py:630/:644` 按小时淘汰，`:276-277` 从文件名解析时刻（与本模块
`name_ts()` 同一取向）。

## 部署

### PythonAnywhere（免费层推荐）

1. 注册 PythonAnywhere 账号
2. 上传代码并配置 WSGI
3. 参考 `部署/pythonanywhere/README.md`

### Docker（Dify 私有化 AI 后端）

```bash
cd 部署
cp .env.example .env
# 编辑 .env 填入强密码
docker compose up -d
# 浏览器打开 http://<服务器IP>:8080 进入 Dify 控制台
```

### 生产环境检查清单

- [ ] `SECRET_KEY` 设为 32 位以上随机字符串（弱密钥会被守卫拒绝启动）
- [ ] `FLASK_ENV=production` 显式声明
- [ ] `DATABASE_URL` 指向 MySQL（生产禁止 SQLite）
- [ ] `LLM_PROVIDER` 和 DeepSeek Key 已配置（或接受规则版回退）

## 运维：看数字，也看判定

`GET /healthz`（匿名可达）除了既有字段，还会回**阈值判定结果**：

```json
{"status": "ok", "db": "ok", "db_ms": 0.3, "ai": {"ai_inflight": 0, "ai_limit": 2, "ai_rejected": 0},
 "diag": {"requests": 520, "errors_5xx": 0, "slow": 0, "error_kinds": 0, "dropped": 0},
 "alerts": [{"code": "db_slow", "label": "探活用的 SELECT 1 耗时超阈", "value": 300.0, "threshold": 250.0}],
 "alert_count": 1}
```

- `alerts` 为空数组才是"无告警"；`status` 仍只由「库 + 迁移账本」决定 —— 它是对外契约，
  阈值改造没有权限把它改语义（`smoke_remote.py` 按 `status == 'ok'` 判部署冒烟）。
- 判定单源在 `src/alerts.py`，`smoke_remote.py` 与门禁第 36 项都读同一份，不各写一份阈值。
- 阈值全部可配（留空即用代码内默认），键与含义登记在 `部署/.env.example`：
  `ALERT_DB_MS`（默认 250，必须小于 `SLOW_REQUEST_MS=500`，语义是"库先于整条请求报警"）、
  `ALERT_ERROR_5XX_MAX`、`ALERT_ERROR_KINDS_MAX`、`ALERT_SLOW_RATIO`（分母不足 50 不判定，
  避免"1 个请求 1 个慢 = 100%"的假告警）、`ALERT_AI_INFLIGHT_RATIO`、`ALERT_AI_REJECTED_MAX`、
  `ALERT_DROPPED_MAX`；慢请求/慢查询打标阈值本身是 `SLOW_REQUEST_MS=500` / `SLOW_QUERY_MS=200`。
- 只出聚合整数：路径名、SQL 模板、表名一律不进公开端点（管理员侧看 `/admin/performance/diag`）。
- 4xx 比例**故意不做告警**：本机标定那次 480 请求里 4xx 占 0.346，因为探测本身就在打 404 与坏令牌
  —— 拿它报警等于让每次自检都产出一条告警。

留痕文件（`data/*.jsonl`）各有上限与备份份数（`*_MAX_BYTES` / `*_BACKUPS`），门禁第 36 项按 AST
枚举"以追加方式写 JSONL 的模块"逐个要求两者齐备，并对磁盘实测量做"不超自身上限 2 倍"的有界性探测。

## 项目结构

```
├── src/
│   ├── app.py              # Flask 应用入口
│   ├── config.py           # 环境配置 + 密钥强度守卫
│   ├── models.py           # SQLAlchemy 数据模型
│   ├── routes/             # 11 个路由蓝图
│   │   ├── auth.py         # 登录/注册/邮箱验证
│   │   ├── h5.py           # 店员 H5 填报
│   │   ├── admin.py        # 管理后台
│   │   ├── ai.py           # AI 培训/经营/驾驶舱
│   │   ├── multi.py        # 多门店经营大脑
│   │   ├── decision.py     # 决策驾驶舱 API
│   │   ├── tasks.py        # 任务管理
│   │   ├── diy.py          # DIY 工作台
│   │   ├── industry.py     # 行业包管理
│   │   └── stores.py       # 门店管理
│   ├── industry_packs/     # 7 个预置行业包
│   ├── api_open.py         # 只读开放 API
│   ├── webhooks.py         # Webhook 事件外发
│   ├── decision_cockpit.py # 四角色决策引擎
│   ├── industry_pack.py    # 行业包引擎
│   ├── task_flow.py        # 任务状态机
│   └── ...
├── init_db.py              # 数据库初始化 + 演示数据播种
├── quality_gate.py         # 质量门禁（9 项检查）
├── smoke_test.py           # 冒烟测试（192 项）
├── test_vuln_regression.py # 漏洞回归（153 项）
├── test_l4_assertions.py   # L4 断言（557 项）
├── openapi.yaml            # OpenAPI 3.0 规范
└── requirements.txt
```

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.11+ / Flask 3.x / SQLAlchemy / Flask-Login / Flask-WTF |
| 前端 | Jinja2 / ECharts / 原生 JS（零构建） |
| 数据库 | SQLite（开发）/ MySQL（生产） |
| AI | DeepSeek API / Dify 私有化 / 规则版回退 |
| 部署 | PythonAnywhere / Docker / waitress |
| CI | GitHub Actions（Quality Gate + 自动部署） |

## 对标分析

本项目已与 GitHub 上 4 个同类优质开源项目进行全面对标（Saleor / Ever Gauzy / NocoBase / InvenTree），详细报告见 [对标分析报告](../output/PH6-materials/benchmarking-report-2026-09-24.md)。

**核心优势**：四角色 AI 决策引擎、行业包 DIY、AI 培训考核、微信 H5 零安装
**主要差距**：API 文档完善度（已通过 OpenAPI 补齐）、异步任务队列（规划中）、插件系统（规划中）

## 许可证

尚未选定开源许可证（仓库中无 `LICENSE` 文件）。本项目当前为 iCAN 竞赛参赛代码，
私有仓库；若后续开源需由维护者显式选择并补充 `LICENSE`。
