FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/utterliar1/nankeep"
LABEL org.opencontainers.image.description="NanShare strm 库治理 · 只读优先 + 复核后二次确认删除"

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
COPY nankeep.py nankeep_web.py nankeep.config.json ./
COPY static/ ./static/

RUN mkdir -p /data/out && chown -R 1000:1000 /data

# 非 root 运行。实测（2026-10-14，NAS 上 docker run 验证）：
# uid 1000 在容器内即可对库目录 touch/cp/rm——包括 root:root 700 属主的目录，
# 与宿主运行用户的行为一致（fnOS 对 Administrators 组的存储层特权，不随容器边界失效）。
USER 1000:1000

ENV NANKEEP_DATA=/data \
    NANKEEP_OUT=/data/out \
    NANKEEP_ROOT=/library \
    NANKEEP_PORT=8322 \
    NANKEEP_BIND=0.0.0.0

EXPOSE 8322

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8322/api/health',timeout=4).status==200 else 1)"

CMD ["python", "-u", "nankeep_web.py"]
