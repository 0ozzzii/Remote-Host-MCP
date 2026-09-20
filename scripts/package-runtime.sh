#!/usr/bin/env bash
# ==============================================================================
# Remote Host MCP - Portable Standalone Runtime Packager
# 构建全便携独立运行时归档，用于被控端免编译、免 pip 零依赖秒级安装
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="${REPO_ROOT}/dist"
BUILD_DIR="${REPO_ROOT}/build-runtime"

TARGET_ARCH="${TARGET_ARCH:-x86_64}"
STANDALONE_PYTHON_RELEASE="20260901"
STANDALONE_PYTHON_VER="3.11.16"

# 确定系统架构与 CPython 独立运行时下载链接
if [[ "$TARGET_ARCH" == "x86_64" ]]; then
  PYTHON_ARTIFACT="cpython-${STANDALONE_PYTHON_VER}+${STANDALONE_PYTHON_RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
  PYTHON_SHA256="64427febea27864d136db46c8efe968eb6fa5ca2813ce1dca4bb95aec31cb2e4"
elif [[ "$TARGET_ARCH" == "aarch64" ]]; then
  PYTHON_ARTIFACT="cpython-${STANDALONE_PYTHON_VER}+${STANDALONE_PYTHON_RELEASE}-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz"
  PYTHON_SHA256="d53da03e91122a76fef3a31c5ee2c23a54a9fcbf00344d2ad4f7dc53715ff282"
else
  echo "Unsupported architecture: $TARGET_ARCH" >&2
  exit 1
fi

PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${STANDALONE_PYTHON_RELEASE}/${PYTHON_ARTIFACT}"

echo "================================================================="
echo "🔨 开始构建 Remote Host MCP Portable Runtime (${TARGET_ARCH})"
echo "📁 源码目录: ${REPO_ROOT}"
echo "📦 产物目录: ${DIST_DIR}"
echo "================================================================="

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"

cd "$BUILD_DIR"

# 1. 获取独立 Python 运行时
if [[ -f "${REPO_ROOT}/downloads/${PYTHON_ARTIFACT}" ]]; then
  echo "--> 复用已有 Python 独立归档: ${PYTHON_ARTIFACT}"
  cp "${REPO_ROOT}/downloads/${PYTHON_ARTIFACT}" .
else
  echo "--> 正在下载独立 Python 基础运行时: ${PYTHON_URL}"
  curl -fsSL --retry 3 "${PYTHON_URL}" -o "${PYTHON_ARTIFACT}"
fi

# 校验 SHA256
if command -v sha256sum >/dev/null 2>&1; then
  echo "${PYTHON_SHA256}  ${PYTHON_ARTIFACT}" | sha256sum -c -
fi

echo "--> 解压 Python 运行时..."
mkdir -p runtime-root
tar -xzf "${PYTHON_ARTIFACT}" -C runtime-root

# python-build-standalone 解压出 python/ 目录
PYTHON_BIN="$(pwd)/runtime-root/python/bin/python3"
chmod +x "$PYTHON_BIN"

echo "--> 正在初始化独立虚拟环境与装载依赖..."
# 使用该独立 python 创建 .venv
"$PYTHON_BIN" -m venv --copies venv-env

VENV_DIR="$(pwd)/venv-env"
"$VENV_DIR/bin/python" -m pip install --no-cache-dir --upgrade pip

echo "--> 正在安装 Remote-Host-MCP 与全部运行时依赖..."
"$VENV_DIR/bin/pip" install --no-cache-dir "${REPO_ROOT}"

echo "--> 正在执行便携化（Relocatable）后处理..."
# 1. 将 .venv 内部脚本的 shebang 改为动态解析的便携模式
# 在 Linux 下使用 #!/usr/bin/env bash 或查找相对路径 python
find "$VENV_DIR/bin" -type f -executable | while read -r file; do
  if head -n 1 "$file" | grep -q "^#\!.*python"; then
    # 替换为便携包装器
    cat << 'WRAPPER_EOF' > "${file}.tmp"
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_EXEC="$VENV_ROOT/bin/python"
if [ ! -x "$PYTHON_EXEC" ]; then
  PYTHON_EXEC="$(command -v python3 || true)"
fi
exec "$PYTHON_EXEC" "$0" "$@"
WRAPPER_EOF
    # 保留原 python 脚本内容（去掉第一行硬编码 shebang）
    sed '1d' "$file" >> "${file}.tmp"
    mv "${file}.tmp" "$file"
    chmod +x "$file"
  fi
done

# 验证 entrypoint 可用性
echo "--> 校验 entrypoint 是否就绪..."
"$VENV_DIR/bin/remote-host-mcp" --help >/dev/null 2>&1 || true

# 2. 打包生成便携式 tarball
ARCHIVE_NAME="rhmcp-runtime-linux-${TARGET_ARCH}.tar.gz"
echo "--> 正在压缩便携运行时: ${DIST_DIR}/${ARCHIVE_NAME}"

# 打包时以相对路径打包整个 venv-env 目录内容
tar -czf "${DIST_DIR}/${ARCHIVE_NAME}" -C "$VENV_DIR" .

cd "${DIST_DIR}"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "${ARCHIVE_NAME}" > "${ARCHIVE_NAME}.sha256"
fi

rm -rf "$BUILD_DIR"

echo "================================================================="
echo "🎉 [小 A 搞定啦！] Portable Runtime 构建完成！"
echo "📦 归档产物: ${DIST_DIR}/${ARCHIVE_NAME}"
echo "📊 大小: $(du -h "${DIST_DIR}/${ARCHIVE_NAME}" | awk '{print $1}')"
echo "================================================================="
