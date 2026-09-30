"""PythonAnywhere WSGI 入口（免费层，**ASCII 路径版**：与项目根同目录）。

放置位置：`/home/<用户名>/ican/pa_wsgi.py`（与 `src/`、`init_db.py`、`requirements.txt` 同级）。
（旧版 `wsgi.py` 按 `部署/pythonanywhere/` 三级上溯解析项目根，中文目录名在 PA 上易出问题，
  故新增本 ASCII 版；两者等价，PA 上**用本文件**。）

配置（二选一，推荐 A）：
  A. Web 标签 → Environment variables 逐项填写（页面可见可改，改完 Reload 生效）
  B. 把本目录 `.env.example` 复制为 `.env` 放到**本文件同目录**（src/app.py 导入期 load_dotenv）

必填（生产三守卫，缺任一应用拒绝启动）：
  SECRET_KEY=<随机强字符串>
  FLASK_ENV=production
  DATABASE_URL=sqlite:////home/<用户名>/ican/data/ican.db

可选（启用在线 AI —— 免费层出站白名单已含 api.deepseek.com，实测可直连）：
  LLM_PROVIDER=deepseek
  DEEPSEEK_API_KEY=sk-xxxx
"""
import os
import sys

# ① 项目根 = 本文件所在目录（PA 部署布局：~/ican/pa_wsgi.py 与 src/ 同级）
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ② .env 兜底：显式指向项目根（PA 的工作目录不保证是项目根，不能让 load_dotenv 靠猜）
_ENV_FILE = os.path.join(PROJECT_ROOT, '.env')
if os.path.exists(_ENV_FILE):
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_FILE)
    except ImportError:
        pass

# ③ 生产守卫：缺 SECRET_KEY / DATABASE_URL 立即报错（不静默降级）
os.environ.setdefault('FLASK_ENV', 'production')
if not os.environ.get('SECRET_KEY') or not os.environ.get('DATABASE_URL'):
    raise RuntimeError(
        'PythonAnywhere 部署缺少 SECRET_KEY 或 DATABASE_URL 环境变量。\n'
        '请在 Web 标签 → Environment variables 配置后点 Reload；\n'
        '或把 .env.example 复制为 .env（与本文件同目录）填好后 Reload。'
    )

# P7-E / S-1（2026-09-13）：SECRET_KEY 还需通过「强度守卫」——长度 ≥16 位、非已知弱值、
# 且不得是仓库/文档中出现过的任何值（session 是客户端签名 Cookie，密钥泄露即可伪造管理员会话）。
# 该检查在 create_app() 内执行。若线上仍是旧弱密钥且暂时无法更换，可临时在 Environment
# variables 增设 ICAN_ALLOW_WEAK_SECRET=1 放行（只告警不阻断）；换好强密钥后务必删除该开关
# （更换步骤见 pythonanywhere/README.md §4.1）。

from src.app import create_app  # noqa: E402

application = create_app('production')
