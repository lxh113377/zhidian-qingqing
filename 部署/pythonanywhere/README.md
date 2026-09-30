# iCAN 免费部署包（PythonAnywhere 免费层）

> **状态：2026-09-11 实机部署完成并验证通过。**
> - 线上地址：**https://lxh123.pythonanywhere.com**（真实系统：可登录 / 填报 / 看板 / 真实 AI）
> - 运行时：Python 3.12（平台预装 Flask 3.0.3 / pandas 2.2.2 / flask_sqlalchemy 3.1.1 / flask_login / flask_wtf / requests / dotenv / waitress，本项目实测兼容，**无需 pip 安装**）
> - AI：**DeepSeek 云端 API 实测可用**（免费层出站白名单含精确主机名 `api.deepseek.com`）
>
> 与腾讯云 + Docker-Dify 私有化方案并存；数据存在平台免费空间（用户已确认接受第三方云免费层，演示目的临时豁免）。

## 0. 一键上线（默认入口）

> **0.0 自动上线（2026-09-23 起）**：推送到 `main` 且改动命中
> `iCAN大学生创新创业大赛/{src, init_db.py, requirements.txt, 部署/pythonanywhere}/**` 时，
> `.github/workflows/deploy-pa.yml` 会**先跑门禁、再自动部署、最后线上探针复验**；密钥 `PA_API_TOKEN`
> 存于仓库 Actions secrets（缺失时该 job 跳过并告警，不会红）。
> ⚠️ **工作流必须放在「仓库根」的 `.github/workflows/`** —— 仓库根是工作区根，代码在
> `iCAN大学生创新创业大赛/`；放在项目目录内的同名文件 GitHub **不会执行**（2026-09-23 实测踩到）。
> ⚠️ 用 PowerShell 管道写入 `PA_API_TOKEN` 会带 BOM（`\ufeff`），脚本已自动清理；
> 若报 `latin-1 codec can't encode character '\ufeff'` 即此成因。
> 下面的脚本仍是**人工入口**，也是 CI 失败时的排障路径。

> 今后优先使用本入口，禁止再从聊天记录里手工拼部署步骤。

```powershell
cd <项目根>\iCAN大学生创新创业大赛
.\部署\pythonanywhere\one_click_deploy.ps1
```

也可以双击或运行：

```bat
部署\pythonanywhere\one_click_deploy.cmd
```

脚本会 fail-closed 完成：
1. 校验 `%TEMP%\pa_token.txt` 或 `PA_API_TOKEN`；
2. 校验权威 `代码仓库` 工作树干净；
3. 备份线上 WSGI；
4. 打包 `src + init_db.py + requirements.txt + pa_wsgi.py`；
5. 上传、解压、执行 `init_db.py --if-empty`；
6. 无论成功失败都恢复原 WSGI；
7. reload 并验证首页、`decision_cockpit.js`、线上 `decision_cockpit.py`；
8. 写入 `output/pa-deploy-<timestamp>.json` 证据。

成功标志：

```text
[PA-DEPLOY-PASS] commit=... package=... evidence=...
```

## 0.1 方案对比速览

| 项 | 免费层方案 | 腾讯云私有化方案 |
|---|---|---|
| 费用 | 0 元（注册即用） | 服务器月租 + Key 费用 |
| 技术栈 | Flask 原代码，**零重写** | Flask 原代码 |
| 公网链接 | `https://<你>.pythonanywhere.com` | 服务器 IP / 域名 |
| AI 能力 | **DeepSeek 云端 API（实测可用）**；未配 Key 时自动回退规则版 | Dify 私有化在线 AI |
| 数据 | 平台免费空间（第三方云） | 自有服务器 |
| 生效时长 | 需**每 1 个月登录一次**并在 Web 页点「Run until 1 month from today」按钮保活（2026-09-23 官网页面实测；旧写法「3 个月」系文档漂移，已修正） | 永久 |

> ⚠️ 免费层方案接受「数据放第三方云免费层」；正式上线仍建议走腾讯云私有化（红线原义保留）。

## 1. 前置准备（一次性）

1. 注册 PythonAnywhere 免费账号：https://www.pythonanywhere.com/ （邮箱注册，无需信用卡）。
2. **验证邮箱（强制）**：未验证前账号被锁在 `/registration/confirm_email_gate/`，
   **API、建站、控制台全部不可用**。收到「Confirm your email」邮件后点链接（QQ 邮箱可
   能在垃圾邮件/订阅邮件文件夹）。
3. 记住用户名 → 子域名 = `<用户名>.pythonanywhere.com`。

## 2. 上传项目代码

主代码仓库是**私有**的，PA 控制台 `git clone` 拉不到 → 走上传：

- **方式 A（推荐，免控制台）**：把 `output/ican-pa.zip`（构建脚本见 §6）上传到
  `/home/<用户名>/ican.zip`，再由 WSGI 导入期解压（见 §6 注意事项）。
- **方式 B（有控制台）**：Bash 控制台执行
  `cd ~ && unzip -q -o ican.zip -d ican && mkdir -p ican/data`。

包内容：`src/`（含模板与静态资源）、`init_db.py`、`requirements.txt`、
`pa_wsgi.py`（PA 专用 WSGI，ASCII 路径）、`.env.example`。

## 3. 建库 + 播种演示数据

- **免控制台**：直接上传本地已播种好的 `data/ican.db` 到 `/home/<用户名>/ican/data/ican.db`
  （权威库每次重建快照集时会重新播种，数据可复现）。
- **有控制台**：
  ```bash
  cd ~/ican && mkdir -p data
  ICAN_INIT_CONFIRM=YES python3.12 init_db.py
  ```
> `init_db.py` 默认写 `data/ican.db`（SQLite）。限流/会话为进程内实现，免费层单实例足够。

### 3.1 七门店 30 天数据重建（PH3-0）

本地或线上需要全库重建时，优先使用平台管理员页面
`/admin/platform/demo-reset`（双重确认 + 自动备份 + 最多保留 10 份）。
等价 CLI：

```bash
python -m src.pack_demo --reset-all --days 30 --json
```

重建后为 1 个商家、7 家门店、23 个账号、105 个产品、42 条指标定义、
2100 条流水、70 条培训记录、14 条异常案例，日期截至执行当天。

## 4. 配置 WSGI + 环境变量（核心步骤）

1. **Web 标签页 → 新建 Web App**：Manual configuration → **Python 3.12**。
2. **WSGI 文件**：直接改写默认文件
   `/var/www/<用户名>_pythonanywhere_com_wsgi.py`，内容为 `pa_wsgi.py` 的等价实现
   （`PROJECT_ROOT = /home/<用户名>/ican`，导入期显式 load `.env`，缺守卫变量即报错）。
   > 说明：API 的 `wsgi_file_path` PATCH **实测不生效**，改默认文件最稳（见 §6）。
3. **环境变量**（写 `.env` 放到 `/home/<用户名>/ican/`，或 Web → Environment variables）：

   | 变量 | 值 | 说明 |
   |---|---|---|
   | `FLASK_ENV` | `production` | 生产配置（守护全开） |
   | `SECRET_KEY` | ≥16 位强随机串（建议 48 字节 urlsafe） | **缺失 / 弱值 / 曾出现在仓库文档中的值 → 拒绝启动**（详见 §4.1） |
   | `DATABASE_URL` | `sqlite:////home/<用户名>/ican/data/ican.db` | 绝对路径需 4 条斜杠 |
   | `LLM_PROVIDER` | `deepseek`（或 `auto`/`none`） | 可选，启用在线 AI |
   | `DEEPSEEK_API_KEY` | `sk-…` | 可选；`DIFY_ENABLED` 不填时「有 Key 即启用」 |
4. 点 **Reload**。

### 4.1 SECRET_KEY 强度要求与更换步骤（2026-09-13 新增，P7-E / S-1）

**为什么**：Flask 的 session 是**客户端签名 Cookie**，密钥即「伪造管理员会话」的钥匙。
旧守卫只比对「开发默认值」这一个字符串，文档里出现过的演示密钥可以顺利通过守卫；
故升级为「已知弱值黑名单 + 强度下限（≥16 位、字符有随机性）」。

**生成强密钥**（任一台机器执行即可）：

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

**线上更换步骤（顺序不能颠倒，否则站点会拒绝启动）**：

| 步骤 | 操作 | 验证 |
|---|---|---|
| ① | 生成新强密钥（上一条命令） | 长度 ≥ 16（建议 64+） |
| ② | 若**当前**用的是弱值，先在 Web → Environment variables 增补 `ICAN_ALLOW_WEAK_SECRET=1`（过渡期放行），Reload | 站点仍可访问 |
| ③ | 把 `SECRET_KEY` 改为新强值，Reload | 站点可访问（登录态会失效一次，需重新登录，属预期） |
| ④ | 更新代码到新版本（含强度守卫）后 Reload | 站点可访问 |
| ⑤ | 删除 `ICAN_ALLOW_WEAK_SECRET`，Reload | 站点可访问；服务端日志无「安全降级」告警 |

> 说明：旧密钥一旦出现在仓库或本文档中即视为**已泄露**，必须更换，不可复用。

## 5. 验证清单（2026-09-11 实测全部通过）

| # | 检查 | 预期 |
|---|---|---|
| 1 | `https://lxh123.pythonanywhere.com/` | 首页渲染（雾山青 + 墨绿黑暗默认主题） |
| 2 | 平台 `platform_demo / platform123` | 仅进入演示数据重建页，业务明细 403 |
| 3 | 老板 `owner_demo / owner123` | 7 家门店排行、趋势与跨店分析正常 |
| 4 | 店长 `milk_tea_manager / manager123` | 仅能查看奶茶示范店 |
| 5 | 店员 `milk_tea_staff1 / staff123` | H5 填报与 AI 培训正常，仅见本人记录 |
| 6 | AI 经营分析页 | 徽章显示「**大模型生成**」= DeepSeek 联网成功；无 Key 时显示「规则版演示」 |
| 7 | 手机微信内打开同链接 | 页面正常（自带 HTTPS 证书） |

## 6. 通道注意事项（本次踩坑，复用必看）

1. **PA API 只认表单编码**：`requests.post(..., json={...})` 会被忽略
   （建站报 `missing_domain_name_error`），必须用 `data={...}`。
2. **修改 `wsgi_file_path` 的 PATCH 不生效**（返回 200 但站点仍跑默认页）→ 直接改写
   `/var/www/<domain>_wsgi.py`。
3. **控制台 API 需浏览器启动**：API 创建的 console 在 `send_input` 时返回
   `412 Console not yet started`，脚本无法唤醒 → **无控制台时可用「WSGI 导入期执行」
   做一次性引导**（解压 zip、探测依赖、初始化等），把结果写入文件或作为 HTTP 响应返回。
4. **脚本化登录**：POST `/login/` 必须带上页面里**全部** `<input>`（含隐藏字段
   `login_view-current_step`），否则 400；脚本 POST 业务表单需带 `Referer`，否则 400。
5. **免费层出站白名单**：仅允许列表内域名，`api.deepseek.com` 在内（精确主机名，
   不要换成 `platform.deepseek.com`）；付费账号无限制。

## 7. 回滚 / 下线

- 改完代码重传 → **Reload** 即生效。
- 停止服务：Web 标签页关闭站点；删除目录即完全下线。
- 迁移回腾讯云：把 `data/ican.db` 换 MySQL（改 `DATABASE_URL`），DIFY_* 填 Key，
  见父级 `README.md`。
