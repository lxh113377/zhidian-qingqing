# iCAN 项目部署包（私有化）

本目录包含将「全行业多门店轻量化 AI 管理智能体」部署的所需物料。
**两条部署路线并行**：
- **路线 A（默认）**：腾讯云 + Docker-Dify 私有化（本文档一~六节）
- **路线 B（免费，2026-08-26 新增）**：PythonAnywhere 免费层直接跑 Flask，
  零代码重写、0 费用，公网链接即可访问，AI 走规则版演示 → 见 `pythonanywhere/`

> 决策记录：路线 B 使用第三方云免费层，数据存平台免费空间；已获用户确认作为演示目的
> 的临时豁免，正式上线仍走路线 A（红线原义保留，见第五节）。

## 一、目录说明

```
部署/
├── docker-compose.yml   # Dify 自托管核心服务（私有化 AI）
├── .env.example         # 环境变量模板（复制为 .env 后填写）
├── README.md            # 本文件
├── pythonanywhere/      # 免费上线方案（wsgi.py + 上线指南，2026-08-26）
└── knowledge_base/      # RAG 知识库文档（上传到 Dify 开启检索）
    ├── 套餐价格.md
    ├── 服务话术.md
    ├── 异议处理.md
    └── 原料规范.md
```

## 二、前置条件

- 腾讯云服务器：2 核 4G 及以上（推荐 4 核 8G），已开放 8080 / 5000 端口
- 已安装 Docker + Docker Compose
- 已申请 LLM API Key（用于 Dify 模型供应商接入）
- 云数据库 MySQL（生产库），或先用 SQLite 演示

## 三、部署步骤

### 步骤 A：启动私有化 Dify

```bash
cd 部署/
cp .env.example .env        # 修改 DB_PASSWORD / DIFY_SECRET_KEY 为强密码
docker compose up -d        # 拉起 db / redis / weaviate / api / worker / web
```

启动后访问 `http://<服务器IP>:8080`，首次进入设置管理员账号，并在
「设置 → 模型供应商」中接入你的 LLM API Key。

### 步骤 B：在 Dify 中构建 AI 应用并开启 RAG

1. **上传知识库**：Dify 控制台「知识库 → 创建」，将 `knowledge_base/` 下
   4 篇文档逐篇上传并等待向量化完成。
2. **创建 Chat App**（用于问答考核 / 情景对话 / 上岗考核）：
   - 类型：聊天助手（Chat App）
   - 提示词：要求 AI 扮演门店培训考官，按业务知识出题、对店员作答点评、判定合格/不合格
   - 开启「上下文」引用上述 4 篇知识库
   - 发布后复制 **API Key**
3. **创建 Completion App**（用于备货建议 / 损耗提醒）：
   - 类型：补全（Completion / 文本生成）
   - 输入近 7 天销量与原料数据，输出次日备货建议与损耗异常提醒
   - 开启知识库上下文
   - 发布后复制 **API Key**

### 步骤 C：接入本项目 Flask 应用

在项目根目录创建 `.env`（注意不是部署目录的 .env）：

```env
DIFY_BASE_URL=http://<服务器IP>:8080
DIFY_CHAT_APP_KEY=<步骤B2 复制的 Chat App Key>
DIFY_ADVICE_APP_KEY=<步骤B3 复制的 Completion App Key>
DIFY_TIMEOUT=30
SECRET_KEY=<随机字符串>
DATABASE_URL=mysql+pymysql://user:pass@<云MySQL>:3306/ican
```

重启 Flask 服务：

```bash
python -m src.app
```

应用检测到 `DIFY_CHAT_APP_KEY` / `DIFY_ADVICE_APP_KEY` 即自动从规则版切换到
Dify 在线版；**未配置时自动回退规则版，演示不中断**。

## 四、本地快速演示（无需服务器）

```bash
python -m src.pack_demo --reset-all --days 30 --json
python -m src.app          # 默认 127.0.0.1:5000，规则版运行
```

重置会先自动备份到 `backups/demo-reset_*.db`，再重建一个商家、七家门店、
2100 条流水、70 条培训记录与 14 条异常案例。访问 `http://127.0.0.1:5000`：

| 角色 | 账号 | 密码 |
|---|---|---|
| 平台管理员 | `platform_demo` | `platform123` |
| 老板 | `owner_demo` | `owner123` |
| 奶茶示范店店长 | `milk_tea_manager` | `manager123` |
| 奶茶示范店店员 | `milk_tea_staff1` | `staff123` |

## 五、红线提醒

- 入口只用微信 H5，禁止 APP / 企微 / 飞书
- 数据安全：**正式路线（A）** Dify 与 MySQL 均部署在自有服务器，禁用第三方 SaaS 与公共 Dify 云端；
  **免费路线（B，PythonAnywhere）** 为演示目的临时豁免（数据存平台免费空间），
  正式上线必须回切路线 A，红线原义保留
- 盲审材料不得出现学校名 / 校徽 / 指导老师 / 单位名

## 六、生产加固清单（上线前逐项打钩）

### 1. 生产启动用 waitress（禁 Flask dev server）

```bash
# .env 中 FLASK_ENV=production 后:
waitress-serve --host 0.0.0.0 --port 5000 --call src.app:create_app
# 或后台常驻:
nohup waitress-serve --host 0.0.0.0 --port 5000 --call src.app:create_app > app.log 2>&1 &
```

> ⚠️ **必须单进程 / 单 worker 部署**：接口限流（`src/rate_limit.py`）为**进程内**
> 滑动窗口，多进程（多 worker）或多实例会让限流各算各的而被绕过、进程内状态不一致。
> waitress 默认即「单进程 + 多线程」，线程共享内存，符合约束；**不要**用 gunicorn
> 多 worker 或起多个实例做负载均衡。

### 2. 环境变量强制项（缺失将拒绝启动）

- `SECRET_KEY`：随机强字符串（生产未配置时应用直接拒绝启动，防 session 伪造）
- `DATABASE_URL`：生产 MySQL 连接串
- `FLASK_ENV=production`：同时使 debug 强制关闭
- `TRUST_PROXY_HOPS`（可选，默认 1）：可信反向代理层数，决定 `X-Forwarded-For`
  取哪一跳作为限流用的客户端 IP。**前面没有反向代理时必须设为 0**，否则伪造的
  XFF 会反向污染 `remote_addr`（P7-E / S-4）

### 3. 数据库备份（每日自动）

```bash
python backup_db.py            # 手动备份到 backups/（SQLite复制/MySQL mysqldump）
# crontab 每日 03:00 自动备份，默认保留最近 30 份:
0 3 * * * cd /opt/ican && /usr/bin/python3 backup_db.py >> backups/backup.log 2>&1
```

### 4. init_db.py 生产守卫

`init_db.py` 默认会 drop_all 清库，生产环境默认拒绝执行；
确需初始化时显式确认：`ICAN_INIT_CONFIRM=YES python init_db.py`

**幂等初始化（推荐，2026-09-10 新增）**：容器/免费托管每次启动都会调用初始化，
若直接跑默认模式会「重启即清库」。改用：

```bash
ICAN_INIT_CONFIRM=YES python init_db.py --if-empty
```

`--if-empty` 仅在业务表不存在时建表播种，已初始化则直接跳过，**重启不丢数据**。
`deploy-free/start.sh` 已默认使用该模式。

### 5. 安全基线

- 密码：全部经 werkzeug PBKDF2 哈希存储，无明文
- CSRF：全站表单经 Flask-WTF CSRFProtect 防护
- 越权：店员访问 /admin/* 返回 403；未登录访问受保护页 302 跳登录
- 密钥：`SECRET_KEY` 只从环境变量注入，**禁止硬编码默认值**（部署脚本缺失即快速失败）
- 留痕：`data/ai_call_log.jsonl` 记录每次在线 AI 调用（provider/延迟/是否回退，见 `src/ai_trace.py`）

## 七、微信入口接入路径与前置条件（红线「唯一入口 = 微信公众号 H5」）

当前实现为账号密码登录；**微信网页授权（OAuth2）为生产必经步骤**，前置条件与路径如下：

1. **服务号**（企业主体）+ 微信认证（个人订阅号不支持网页授权）；
2. **已备案域名**（HTTPS）：微信要求授权回调域名备案且走 https；
3. 公众号后台「接口权限 → 网页授权」配置**授权回调域名**；
4. 业务侧改造点（约 1 个路由）：`/login` 增加 `wechat` 分支 ——
   跳 `https://open.weixin.qq.com/connect/oauth2/authorize`（snsapi_base 静默授权）
   → 回调用 `code` 换 `openid` → 绑定 `Staff.wechat_openid` → 免密登录；
5. **演示降级**：未具备服务号/备案域名时，用手机浏览器全屏打开 H5 演示，
   并在答辩中说明「公众号菜单 → 跳转 H5，现为简化登录；生产接网页授权自动识别店员身份」。

> 说明：接入后仍满足「只用微信、不装 APP」的红线；`Staff` 表已预留可扩展字段，
> 改造不触碰业务逻辑。

## 八、运行期观测与故障定位（2026-09-26 开源对标第 19 轮）

### 1. 一个请求号贯穿全链（用户侧只需抄这一串）

- 每个请求进场即分配 8 位 `request_id`，同时回写响应头 `X-Request-Id`；
  日志（人类格式与 JSON 格式都有）与 `data/ai_call_log.jsonl` 的每条 AI 留痕都带它。
- 出错页（500）正文印「错误编号 = 该 id」，报障方只需抄这一串，管理员即可定位服务端记录。
- **为什么必须自带编号**：单进程 waitress 复用线程，只靠时间戳对齐会把上一个请求的
  日志算到这次头上；id 在请求结束时清除，跨请求不串号。

### 2. 探活端点 `GET /healthz`（公开，无需登录）

| 字段 | 含义 | 注意 |
|---|---|---|
| `status` | `ok` / `degraded` | `degraded` 有四种成因：DB 探测失败、补丁账本表缺失、有未完成补丁、有孤儿补丁 |
| `db` / `db_ms` | 数据库可用性与单查询往返毫秒 | 探活慢≠挂了，`db_ms` 单独一档便于区分「活着但很慢」 |
| `version` | 应用版本与 CI 注入的 commit 短号 | 未注入时 commit 为空串（代码明确不去跑 git 猜） |
| `ai` | 出站并发在途数 / 上限 / 累计拒绝数 | 拒绝数增长 = 触发降级，不是上游故障 |
| `diag` | 请求计数、状态分类、慢请求数、异常类名指纹 | **只出聚合整数**，路径名与 SQL 模板不在公开端点出现 |

> `status=degraded` 仍返回 HTTP 200：免费层没有自动重启，让探活方拿到 502 反而看不到细节；
> 判据要看 `status` 字段而不是状态码。

### 3. 管理员侧细粒度快照（须登录且须管理员会话）

`MEMORY_ENDPOINT_ENABLED=true` 时注册（默认关，因为快照里含**路径名与 SQL 模板**）：

- `GET /admin/performance/diag`：慢路径 TopN、异常类名计数、慢查询模板聚合、淘汰与丢弃计数；
- `GET /admin/performance/memory`：tracemalloc 分配点快照。

路径里有 `admin` 只是路径约定，实际保护靠管理员会话校验 —— 匿名与店员一律 403。

### 4. 离线取证：不连库把一次故障讲清楚

```bash
python log_triage.py                       # 全站汇总：状态分布、最慢请求榜（含 AI 归因）
python log_triage.py --rid 4a8e14e9        # 单请求全链路：访问行 + WARN + AI 出站按原序
python log_triage.py --json                # 机器可读（接 jq / 贴工单）
python log_triage.py --selftest            # 夹具自证（改解析规则后必须先跑这条）
```

归因口径：`business_ms = 总耗时 - AI 等待`；`ai_pattern` 区分
`single / serial / parallel / partial`（并行是驾驶舱红线，判据是纯算术：
总耗时贴着「各次之和」= 串行，贴着「最慢一次」= 并行）。
旧行没有 request_id 时标 `legacy` 并单独计数，**不猜时间戳**。

### 5. 可调阈值一律登记在 `.env.example`

慢请求/慢查询阈值、AI 出站并发上界、日志目录、各类开关都写在 `部署/.env.example`，
每项带代码默认值与出处行号；L4 断言 S21 会把「代码里能调但文档没写」和
「文档写了但代码不读」两个方向都判红 —— 找不到某项开关时以该文件为准，别猜。

### 6. CI 里静态镜像比对的结论怎么读

`deploy-pa.yml` 的镜像比对步骤按退出码分诊，读到标签就知处置，不需要猜：
`MIRROR-OK`（一致）/ `MIRROR-DRIFT`（要重传）/ `MIRROR-NET`（不可达，**内容一致性未验证**）/
`MIRROR-STALEDATE`（只差日期，当天重建一次）/ `MIRROR-USAGE`（命令写错，属工作流缺陷）。
该步骤刻意不阻断部署结论，所以「流水线绿」不等于「镜像一致」—— 以标签为准。
