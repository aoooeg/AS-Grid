# 使用官方 Python 3.9 运行时作为基础镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV TZ=UTC

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 确保 src 目录存在
RUN mkdir -p /app/src

# 创建日志目录
RUN mkdir -p /app/log

# 创建用户组和用户（使用动态UID/GID）
ARG USER_ID=1000
ARG GROUP_ID=1000
RUN groupadd -g ${GROUP_ID} trader && \
    useradd -u ${USER_ID} -g ${GROUP_ID} -m trader

# 设置权限（在切换到用户之前）
RUN chown -R trader:trader /app && \
    chmod +x /app/scripts/start.sh

# 切换到非 root 用户
USER trader

# 暴露端口（如果需要健康检查或监控）
EXPOSE 8080

# 设置健康检查（检查进程是否运行）
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# 运行应用
CMD ["/app/scripts/start.sh"] 