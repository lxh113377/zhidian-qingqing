# UPGRADE — 存量数据库升级到当前版本

> 当前版本：**v1.28.0**（字面量唯一真相源 = `src/version.py` 的 `APP_VERSION`；门禁第 25 项校验本行、
> README 版本行与代码三处逐字一致）
> 适用：已有一段时间没跑过迁移的 SQLite 库（本机开发库、PythonAnywhere 线上库、交付副本内库）
> 本文件的补丁名单**不是手写清单**：门禁第 24 项会把它与 `src/schema_ledger.PATCHES` 对账，
> 少一条或多一条都判红 —— 手写文档必然漂移，这是本项目第 17 轮对标的直接结论之一。

## 一条命令升级

```bash
python init_db.py --if-empty
```

`--if-empty` 只在库已存在（`admin` 表在）时走**增量迁移**分支：先 `create_all()` 补建新表，
再按注册表顺序跑「账本里还没记」的补丁。它**不会** drop 任何表、不会清数据。
生产环境需显式确认：`ICAN_INIT_CONFIRM=YES python init_db.py --if-empty`。

⚠️ 不带 `--if-empty` 的默认模式是**清库重建演示数据**，升级存量库严禁使用。

## 补丁注册表（按执行顺序）

| pid | 种类 | 作用 | 幂等方式 |
|---|---|---|---|
| `p001_ensure_columns` | ddl | 旧库补齐多租户/随包新增列（7 表 13 列） | 先 `inspect` 取现有列，只 ALTER 缺的；整批 ALTER 在**同一事务**内 |
| `p002_store_scoped_uq` | ddl | 唯一约束收敛到门店范围：`products` / `metric_defs` / `daily_sales` 三表重建 | 先探测唯一约束**形状**，已是新形状即跳过；单表重建走同一事务 |
| `p005_cost_snapshot` | ddl | `daily_sales` 补 `product_cost`/`cost_basis` 两列 + 建 `change_log` 表 | 逐列探测后 ALTER；新表 `checkfirst`，重复执行为 `noop` |
| `p007_webhook_delivery` | ddl | 建出站投递留痕表 `webhook_delivery`（每次尝试一行：attempt/状态码/错误类/是否被守卫拦下/下次重试时间） | `create_all(checkfirst=True)` 建新表，不动任何既有列与历史行；复跑记 `noop` |
| `p008_task_takeover` | ddl | `store_tasks` 补 `takeover_json` 列（遗留任务接管链：from/to/by/at/reason 当次交接） | 先 `inspect` 探测列是否存在，已有即跳过；只 ALTER 缺列，不回填旧行（读到 NULL = 无交接史） |
| `p003_default_tenant` | data | 建默认商家/门店并把存量 NULL 归属行回填 | 查后建 + 按维度回填，零回填时记 `noop` |
| `p004_pack_products` | data | 按行业包补齐产品清单（回填 `pack_id IS NULL` → 默认包 + 增量补齐） | 先查同名再插，不覆盖既有价格/上下架态 |
| `p006_cost_backfill` | data | 存量流水回填成本快照，来源一律标 `backfill` | 只补 `product_cost IS NULL` 且目录有成本价的行；取的是**回填时刻**的当前值，不冒充当日真实成本 |

`p005`/`p007` 排在 `p003`/`p004`/`p006` 之前是判据逼出来的（S17：结构补丁一律先于数据补丁）——
`pid` 是账本主键、一经发布不得改名，所以位置由声明元组顺序决定，编号只表示引入轮次。

`kind` 分成 `ddl` / `data` 两类是有意为之（对标 ToolJet 把 schema 迁移与 data 迁移拆成两个
datasource）：结构补丁必须先于数据补丁，因为回填依赖新列已经存在。

## 账本：怎么知道"这个库升到第几步"

```bash
curl -s http://127.0.0.1:5000/healthz | python -m json.tool
```

`schema` 段即答案：

```json
{"declared": 4, "applied": 4, "pending": [], "orphans": [], "ledger_table": true}
```

| 字段 | 含义 | 非期望值说明什么 |
|---|---|---|
| `pending` | 声明了但账本里没有的 pid | 非空 = 该库没升到最新版，跑上面的命令 |
| `orphans` | 账本里有但声明里已删的 pid | 非空 = 有人改了已发布的 pid 名字（**禁止**，见下） |
| `ledger_table` | 账本表是否可查 | `false` = 从没跑过 `--if-empty`，此时 `applied` 不可信 |

账本表是 `schema_patch_log`（`patch_id` 主键 + `mode` + `kind` + `detail` + `applied_at`）。
`mode` 两个取值：`applied` 真改了东西；`noop` 跑过了但结构/数据已满足 ——
这一格正是 Metabase 用 `preConditions: onFail: MARK_RAN` 表达的同一语义：
**"已满足"要记成"跑过"，而不是报错或静默跳过。**

## 回滚（每条补丁的可逆性是三档，不是一句"应该能撤"）

第 35 轮起，`src/schema_ledger.PatchRecord` 每条补丁都带 `rev` + `rev_note` 两格声明，
门禁第 50 项核「声明 ⇄ 反向实现 ⇄ 本节文档」三方同源。三档语义与处置：

| 档 | 含义 | 怎么撤 | 本表覆盖的补丁 |
|---|---|---|---|
| `auto` | 有可执行的反向动作（绑在 `init_db._PATCH_REVERSERS`） | `python init_db.py --rollback=p006_cost_backfill --yes` | `p006_cost_backfill` |
| `manual` | 撤它必然丢数据，只能整库回退到补丁前的备份锚点 | `python restore_db.py --backup <锚点> --into <在用库> --force` | `p001_ensure_columns`、`p002_store_scoped_uq`、`p005_cost_snapshot`、`p007_webhook_delivery`、`p008_task_takeover` |
| `none` | 语义上无法反推，撤了也回不到"原来的样子" | 同上（整库回退），**没有**局部撤 | `p003_default_tenant`、`p004_pack_products` |

`auto` 档只有一条不是偷懒，而是**能精确撤的唯一前提是当初留了标记**：
p006 的回填行带 `cost_basis='backfill'`，所以反向动作就是
`UPDATE daily_sales SET product_cost=NULL, cost_basis='none' WHERE cost_basis='backfill'`
—— 退回的是**补丁前的真实状态**（`cost_basis` 列是 `NOT NULL DEFAULT 'none'`，
写 NULL 会直接 `IntegrityError`；第一版就写错了，被 L4 S41 的真演习当场撞回）。
没有标记的那类（建默认归属、按包补录产品）事后无法区分"补丁写的"与"用户写的"，
只能声明 `none` —— 这正是 Django `RunPython(reverse_code=...)` 的区分方式：
缺 `reverse_code` 时它 `raise NotImplementedError`（`special.py:205-206`），而不是安静跳过。

三条硬约定：

1. **动手前先落锚点**：`--rollback` 会先调 `restore_db.pre_rollback_copy`（同一个出口，
   锚点名带 `pre-rollback` 标记、证据卡里 `tool=init_db.rollback`）。锚点建不出来就直接拒绝，
   绝不允许"无锚点改库"。
2. **撤干净 = 账本摘行**：成功后调 `schema_ledger.unrecord_pid(pid)`，于是这个库重新"欠着"该补丁；
   下一次 `python init_db.py --if-empty` 会把它再执行一遍。这条闭环本身就是回滚的验收判据
   （L4 S41 的真演习量的是：撤 → 断言值回 NULL → 再补 → 断言账本回来）。
3. **`--yes` 不可省**：回滚是破坏性写库动作；缺 `--yes`、pid 不在声明名单、档位不是 `auto`
   一律**非零退出且不写库**。

### `manual` / `none` 档的一条命令（第 36 轮起）

上表"怎么撤"那一栏原本要人敲两条（自己挑一份备份、再跑 `restore_db.py`）。现在一条：

```bash
python init_db.py --rollback=p005_cost_snapshot --via-backup --yes
python init_db.py --rollback=p005_cost_snapshot --via-backup \
    --backup=ican_20260927_035845.db --yes          # 要点名某一份时用这条
```

它会 ① 读账本拿该补丁的 `applied_at`；② 挑一份**够格且早于该时刻**的备份（不点名时走
`restore_db.pick_latest_eligible()`）；③ 给在用库建覆盖前锚点（含证据卡）；④ 载回并逐表复算；
⑤ 断言账本里那一行确实消失 —— 没消失就报错中止，**不宣称成功**。三种拒绝：该补丁是 `auto`
（有精确反向实现，不该拿整库回退换掉之后的全部业务数据）、缺 `--yes`、备份时刻**不早于**补丁
应用时刻（载回它也撤不掉）。

### 备份"够不够格"由谁判（门禁第 51 项核这张表）

`backups/*.db` 是本机历史堆积物，实测 14 件里只有 1 件带证据卡 —— 所以"出事时挑最近那份"
此前是句没法执行的话。判定现在收在一处（`restore_db.eligibility()`，纯函数、只吃读数）：

| 态 | 触发条件 | 为什么不够格 |
|---|---|---|
| `nameless` | 文件名里没有 `20YYYYMMDD_HHMMSS` | 无法排序就无从声称"最近"；排序按**名字里的时刻**而不是 mtime（复制/同步盘会改 mtime） |
| `no_manifest` | 缺 `<备份>.manifest.json` | 第 33 轮的不变式：没被载回核过的备份只是一个文件 |
| `stale` | 龄 > `--max-age-h`（默认 72 小时） | 连时间戳都拿不到也算 stale —— 取不到即不合格，不是"没有反证" |
| `integrity_fail` / `unreadable` | 深核时 `PRAGMA integrity_check` 非 ok / 根本打不开 | 只有 `--deep` 才跑；浅扫时该字段是 `unchecked`，**未核不得读成 ok** |
| `manifest_ghost` | 有证据卡但没有对应的 `.db` | 这份备份实际不存在，与"有件无卡"分开记（差集三类不折叠成一句"对不上"） |

- 自动选择只经 `pick_latest_eligible()`：它读的是**已排好形的清单的头**，
  所以"挑中的那份"与"清单上写的那份"不可能不一致。
- **人显式 `--backup <路径>` 仍可选到不够格的备份** —— 那是人在替工具担判断：工具照流程核它、
  复算它，但不背书"这是最好的选择"。这条不对称是故意的：自动选择必须可证明，人工指定保留逃生面。
- 想先看清楚再决定：`python restore_db.py --list-backups [--deep] [--json]`（只读，不动任何库）。

### 两种 naive 时刻不得直接比（本轮实测抓到的 8 小时错档）

账本列 `applied_at` 存的是 **naive UTC**（`src/models.py` 里
`datetime.now(UTC).replace(tzinfo=None)`），而备份文件名里那 14 位是**人眼看到的钟面**（+08）。
直接比会差整 8 小时：实测同一次 p007 应用，账本记 `2026-09-27 01:46:09`、备份名写 `094600`。
换算原语只有一个来源 —— `src/business_calendar.py` 的 `from_utc_naive()`（DB 列口径）与
`from_wall_clock()`（钟面口径）；文件名戳统一由 `restore_db.wall_ts()` 产出。
任何绕过这两个函数的写法都会被门禁第 43 项拦（它本轮刚被加固：
`__import__('datetime').datetime.now()` 这类"先拿模块对象再调"的形态此前整类逃逸）。

## 中断以后怎么办

每个结构补丁跑在**单个事务**里，账本行在补丁事务**之后**单独提交。因此只有三种结局：

1. **补丁崩了** → 该补丁的结构变更整体回滚，账本不记 → 库回到跑它之前的状态，
   控制台会打印 `🔴 迁移中断：停在 <pid>（<标题>）`，重跑即从这一条继续。
2. **补丁成功、记账前崩**（极窄窗口）→ 重跑时补丁自身幂等，判为 `noop` 并补上账本，
   不会重复改结构。这是 Ghost 把「迁移必须幂等、跑两次安全」写成硬性要求的原因。
3. **换表中途留下 `{table}_rebuild` 中间表** → 由 `_adopt_orphan_rebuild()` 收编：
   列集合与 ORM 一致才自动改名；不一致则打印 `⚠️ …拒绝自动收编`，
   **不动表、交人工**。猜错一次的代价是丢数据。

### 人工恢复步骤（仅在 `pending` 反复非空或出现 `orphans` 时）

```bash
python backup_db.py                       # 先留一份库文件（保留 30 份）
python check_schema_drift.py --json       # 看缺表/缺列/缺索引/缺唯一约束形状
python init_db.py --if-empty              # 重跑增量迁移
python check_schema_drift.py && echo "结构已对齐"
```

## 升级后的验收（四件事，缺一不可）

```bash
python quality_gate.py                    # 必须在权威交付副本目录下跑，第 10 项才有真效力
python verify_local.py                    # 本地等价验证链（含门禁 + 判据自测 + 镜像新鲜度）
python smoke_test.py                      # 链路冒烟
python check_schema_drift.py              # ORM ⇄ 实库三向差集（含唯一约束形状）
```

## 版本契约

- 应用版本：`src/version.py:APP_VERSION`，经 `GET /healthz` 的 `version.app` 对外可见；
  commit 短号由部署注入 `ICAN_GIT_COMMIT`（不进代码，避免 CI 浅克隆下 `git describe` 失败）。
- 打 tag：`git tag -a v<X.Y.Z> -m "<说明>" && git push origin v<X.Y.Z>`；
  发布产物由 `.github/workflows/release.yml` 在 tag push 时生成。
- **已发布的版本号不可复用**（对标 Appwrite「Published versions are immutable」）：
  发过了就只能发新 patch，不能改 tag 指向。
- 已发布的 `pid` **不可改名、不可删除**：账本按 pid 认账，改名等于把存量库的历史记录
  变成孤儿账本、并让该补丁在新库上重跑一次。要废弃一个补丁 = 新增一条把它撤回来的补丁。

## 不在本文件的范围

- **回滚 schema**：本项目不提供 downgrade。回滚一律走文件级还原
  （`python backup_db.py` 的产物或 `backups/` 下的库文件），理由见 `07-next-steps.md` 的 M46。
- **MySQL 存量库**：`check_schema_drift.py` 与全部补丁只面向 SQLite（生产红线：未授权
  MySQL/付费资源不得启用）。若将来迁库，本文件的补丁注册表需重新评审。
