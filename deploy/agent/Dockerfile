# NimoOS Agent — 离线镜像。构建上下文 = NimoOS-AI 仓库根(COPY agent/)。
FROM python:3.11-slim

# 沙箱运行时 + LLM 命令工具链(bwrap --ro-bind /usr 后命令用容器的 /usr)
RUN apt-get update && apt-get install -y --no-install-recommends \
      bubblewrap util-linux tini \
      libmagic1 ca-certificates \
      bash coreutils findutils grep sed gawk \
      git curl wget \
      jq ripgrep fd-find \
      unzip zip tar gzip xz-utils \
      file \
 && rm -rf /var/lib/apt/lists/*

# 静态 ffmpeg(避免 apt 拉入 ~200MB X11/wayland 依赖)
ARG FFMPEG_ARCH=amd64
RUN set -eux; cd /tmp; \
    wget -q "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${FFMPEG_ARCH}-static.tar.xz" -O ffmpeg.tar.xz; \
    tar -xf ffmpeg.tar.xz; \
    cp ffmpeg-*-static/ffmpeg ffmpeg-*-static/ffprobe /usr/local/bin/; \
    rm -rf /tmp/ffmpeg*

# agent 源码 + Python 依赖(烤进镜像,host 不再建 venv)
COPY agent/ /usr/share/nimoos/agent/
RUN pip install --no-cache-dir -r /usr/share/nimoos/agent/requirements.txt

WORKDIR /usr/share/nimoos/agent
ENV AGENT_DB_PATH=/var/lib/nimoos/ai/agent/agent.db
EXPOSE 8282
# tini 作 PID 1 回收 bwrap 产生的孤儿/僵尸进程
ENTRYPOINT ["/usr/bin/tini", "--", "python", "main.py"]
