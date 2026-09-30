# iCAN Flask 应用 Dockerfile
# 多阶段构建：先装依赖，再复制源码，最终镜像精简
FROM python:3.11-slim AS base

WORKDIR /app

# 系统依赖（curl 用于健康检查）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用源码
COPY src/ ./src/
COPY init_db.py quality_gate.py ./

# 数据目录（SQLite 开发模式 / 日志 / 留痕）
RUN mkdir -p /app/data /app/logs

# 环境变量
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# 启动命令：初始化数据库（幂等）+ waitress 生产服务器
CMD ["sh", "-c", "python init_db.py && waitress-serve --host=0.0.0.0 --port=8080 'src.app:create_app()'"]
