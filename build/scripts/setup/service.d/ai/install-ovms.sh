#!/bin/bash
# 幂等安装 OVMS(OpenVINO Model Server)裸二进制到 /opt/ovms,部署时由 setup-ai.sh 调用。
# 机器同构,固定 ubuntu24 / python_off 包(Debian13 缺 libpython,python_on 不可用)。
# 失败只告警并 exit 0,绝不阻断 AI 服务安装。安装上下文以 root 运行,无需 sudo。
set -u

DEST="/opt/ovms"
VERSION="${OVMS_VERSION:-2026.2.1}"
PKG="${OVMS_PKG:-ovms_ubuntu24_${VERSION}_python_off.tar.gz}"
URL="${OVMS_URL:-https://storage.openvinotoolkit.org/repositories/openvino_model_server/packages/${VERSION}/${PKG}}"
WORK="/tmp/ovms-dl"

if [ -x "${DEST}/bin/ovms" ]; then
    echo "✅ OVMS 已存在于 ${DEST}/bin/ovms,跳过下载。"
    exit 0
fi

echo "==> 安装 OVMS:${PKG}"
mkdir -p "${WORK}" || { echo "⚠ 无法创建 ${WORK},跳过 OVMS 安装。"; exit 0; }
cd "${WORK}" || { echo "⚠ 无法进入 ${WORK},跳过 OVMS 安装。"; exit 0; }

dl() {
    local url="$1" out="$2"
    if command -v aria2c >/dev/null 2>&1; then
        aria2c -x16 -s16 -c --file-allocation=none -o "$out" "$url"
    elif command -v curl >/dev/null 2>&1; then
        curl -fL -C - -o "$out" "$url"
    else
        wget -c -O "$out" "$url"
    fi
}

if ! dl "${URL}" "${PKG}"; then
    echo "⚠ OVMS 下载失败(${URL});跳过。可稍后重跑安装或手动安装。"
    exit 0
fi
if ! tar tzf "${PKG}" >/dev/null 2>&1; then
    echo "⚠ 下载的 ${PKG} 不是有效 gzip(可能错误页);跳过 OVMS 安装。"
    exit 0
fi

rm -rf "${WORK}/extract"; mkdir -p "${WORK}/extract"
if ! tar xzf "${PKG}" -C "${WORK}/extract"; then
    echo "⚠ 解压 ${PKG} 失败;跳过。"; exit 0
fi
ovms_bin="$(find "${WORK}/extract" -type f -path '*/bin/ovms' | head -1)"
if [ -z "${ovms_bin}" ]; then
    echo "⚠ 解压后未找到 bin/ovms;跳过。"; exit 0
fi
binroot="$(dirname "$(dirname "${ovms_bin}")")"   # 含 bin/ 与 lib/ 的目录
rm -rf "${DEST}"
if ! cp -a "${binroot}" "${DEST}"; then
    echo "⚠ 复制到 ${DEST} 失败;跳过。"; exit 0
fi
if [ -x "${DEST}/bin/ovms" ]; then
    echo "✅ OVMS 已安装到 ${DEST}。"
else
    echo "⚠ ${DEST}/bin/ovms 不在位,安装可能不完整。"
fi
exit 0
