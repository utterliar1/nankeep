FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/utterliar1/nankeep"
LABEL org.opencontainers.image.description="NanShare strm 库精简 · 只读优先 + 复核后二次确认删除"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# CI 注入版本；本地构建留空则用源码默认
ARG APP_VERSION=
ENV APP_VERSION=${APP_VERSION}

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 本项目 stdlib only：没有 requirements.txt，也没有 pip install 步骤
# --chown + chmod：COPY 会保留「构建宿主的文件模式」（受宿主 umask 影响，可能是 0600），
# 而运行用户是 uid 1000 —— 不显式放开就会 `Permission denied` 读不到 .py。
COPY --chown=1000:1000 nankeep.py nankeep_web.py nankeep_domain.py nankeep.config.json ./
COPY --chown=1000:1000 static/ ./static/

RUN chmod -R a+rX /app \
    && mkdir -p /data/out && chown -R 1000:1000 /data

# ── 运行用户 ──
# 默认 **root**：转存侧 strm 指向 CD2 / OpenList 等 FUSE 挂载点上的真实文件，
# 那些文件在宿主上是 root:root 755 + `default_permissions`，uid 1000 写不了
# （实测 Permission denied，root 可以）。要删网盘源文件就必须是 root。
#
# 只要不启用 remote（不删网盘文件），可以改回最小权限运行：
#   在 compose 里加   user: "1000:1000"
# /data 已 chown 给 1000，两种模式都能用。
# 参考 2026-10-14 实测：fnOS 对 Administrators 组的存储层特权不随容器边界失效，
# 所以 uid 1000 删 NAS 上的 strm 仍然没问题 —— 只有 FUSE 挂载点需要 root。
USER root

ENV NANKEEP_DATA=/data \
    NANKEEP_OUT=/data/out \
    NANKEEP_ROOT=/library \
    NANKEEP_PORT=8322 \
    NANKEEP_VERSION=${APP_VERSION} \
    NANKEEP_BIND=0.0.0.0

EXPOSE 8322

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8322/api/health',timeout=4).status==200 else 1)"

CMD ["python", "-u", "nankeep_web.py"]

