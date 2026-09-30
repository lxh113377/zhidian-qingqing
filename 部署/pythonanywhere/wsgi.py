"""PythonAnywhere WSGI 引导入口（免费层部署）。

使用方法：
1. 在 PythonAnywhere「Web」标签中，把 WSGI 配置文件的 Python 路径指向本文件：
   /home/<你的用户名>/ican/部署/pythonanywhere/wsgi.py
2. 在「Web → Environment variables」逐项填入下面变量（不留明文在代码里）：
   - SECRET_KEY=<随机强字符串>
   - FLASK_ENV=production
   - DATABASE_URL=sqlite:////home/<你的用户名>/ican/data/ican.db
3. 建议在 wsgi.py 同目录放 .env（.env 方式见 README），
   本项目 src/config 在导入期读环境变量，必须保证这些变量早于 import 生效。
"""
import os
import sys

# ① 项目根目录（本文件位于 部署/pythonanywhere/wsgi.py，上溯 3 级即项目根）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ② 生产守卫环境变量（PythonAnywhere 网页端环境变量优先级更高，会被 setdefault 尊重；
#    三项缺失任一时，src.app.create_app 会拒绝启动，命中项目「生产三守卫」红线）
os.environ.setdefault('FLASK_ENV', 'production')
# 注意：SECRET_KEY / DATABASE_URL 请务必在「Web → Environment variables」或 .env 中显式配置，
#       不要在本文件写死（写死 = 泄露到代码仓库，违反安全红线）。
#       下方仅为「未配置时立即报错」的可读提示：
if not os.environ.get('SECRET_KEY') or not os.environ.get('DATABASE_URL'):
    raise RuntimeError(
        'PythonAnywhere 部署缺少 SECRET_KEY 或 DATABASE_URL 环境变量。\n'
        '请到 Web 标签 → Environment variables 添加后保存（或参考 README 用 .env），'
        '再 Reload 本站点。'
    )

# P7-E / S-1（2026-09-13）：SECRET_KEY 还需通过「强度守卫」——长度 ≥16 位、非已知弱值、
# 且不得是仓库/文档中出现过的任何值（session 是客户端签名 Cookie，密钥泄露即可伪造管理员会话）。
# 该检查在 create_app() 内执行。若线上仍是旧弱密钥且暂时无法更换，可临时在 Environment
# variables 增设 ICAN_ALLOW_WEAK_SECRET=1 放行（只告警不阻断）；换好强密钥后务必删除该开关
# （更换步骤见 pythonanywhere/README.md §4.1）。

# ③ 创建应用（生产环境 + 守卫全过才会返回）
from src.app import create_app

application = create_app('production')