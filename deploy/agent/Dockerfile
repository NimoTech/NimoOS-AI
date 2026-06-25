# NimoOS Agent — 离线镜像。构建上下文 = NimoOS-AI 仓库根(COPY agent/)。
#
# 可复现/离线建议:钉死基础镜像 digest,避免每次 build 去 docker.io 重解析 tag
# (重解析一旦拿到不同镜像会让下面所有层全部重建;离线环境直接 TLS 超时)。
# 在能联网的机器上取 digest:
#   docker pull python:3.11-slim
#   docker inspect --format '{{index .RepoDigests 0}}' python:3.11-slim
# 然后把下一行替换为:  FROM python:3.11-slim@sha256:<digest>

# egress-proxy 二进制在【宿主】预编译(package-agent.sh 在 docker build 前 go build),
# 这里直接 COPY 进镜像 —— 避开多阶段 golang:1.21 镜像依赖(离线 NAS 上不一定有该基础镜像)。
FROM python:3.11-slim

# 沙箱运行时 + LLM 命令工具链(bwrap --ro-bind /usr 后命令用容器的 /usr)
RUN apt-get update && apt-get install -y --no-install-recommends \
      bubblewrap util-linux tini \
      iproute2 \
      libmagic1 ca-certificates \
      bash coreutils findutils grep sed gawk \
      git curl wget \
      jq ripgrep fd-find \
      unzip zip tar gzip xz-utils \
      file \
 && rm -rf /var/lib/apt/lists/*

# Node(npm/npx)+ uv(uvx):stdio MCP server 运行时(用户填 `npx -y ...` / `uvx ...` 用到时自下载)
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

# stdio server 的 HOME / 包缓存指向持久卷,容器重启不重下(目录由 main.py 启动时 mkdir)
ENV NIMOOS_MCP_HOME=/var/lib/nimoos/ai/agent/mcp-home
ENV npm_config_cache=/var/lib/nimoos/ai/agent/mcp-home/.npm
ENV UV_CACHE_DIR=/var/lib/nimoos/ai/agent/mcp-home/.uv

# 静态 ffmpeg(避免 apt 拉入 ~200MB X11/wayland 依赖)
ARG FFMPEG_ARCH=amd64
RUN set -eux; cd /tmp; \
    wget -q "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${FFMPEG_ARCH}-static.tar.xz" -O ffmpeg.tar.xz; \
    tar -xf ffmpeg.tar.xz; \
    cp ffmpeg-*-static/ffmpeg ffmpeg-*-static/ffprobe /usr/local/bin/; \
    rm -rf /tmp/ffmpeg*

# 依赖层与代码层分离(层缓存关键):先只拷 requirements.txt 并装依赖,再拷源码。
# 这样改任意 *.py 只让最后一层 COPY 失效(秒级、零下载),pip 层只在 requirements.txt
# 变更时才重装。烤进镜像,host 不再建 venv。
COPY agent/requirements.txt /usr/share/nimoos/agent/requirements.txt
RUN pip install --no-cache-dir -r /usr/share/nimoos/agent/requirements.txt
COPY agent/ /usr/share/nimoos/agent/

WORKDIR /usr/share/nimoos/agent
ENV AGENT_DB_PATH=/var/lib/nimoos/ai/agent/agent.db
EXPOSE 8282

# egress-proxy: 宿主预编译的静态二进制(CGO_ENABLED=0),由 package-agent.sh 在 build 前生成
COPY deploy/agent/egress-proxy/egress-proxy /usr/local/bin/egress-proxy
RUN chmod +x /usr/local/bin/egress-proxy

# tini 作 PID 1 回收 bwrap 产生的孤儿/僵尸进程
ENTRYPOINT ["/usr/bin/tini", "--", "python", "main.py"]
