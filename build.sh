#!/usr/bin/env bash
# =============================================================================
# StorPool HarmonyOS — Rust 双 ABI 编译 + HAP 打包自动化
# P1-2: build.sh
#
# 用法：
#   ./build.sh            # 编译 Rust + 复制 .so + 构建 HAP
#   ./build.sh --rust     # 仅编译 Rust + 复制 .so
#   ./build.sh --hap      # 仅构建 HAP（需先有 .so）
#   ./build.sh --clean    # 清理 Rust target 后重新编译
#
# 前置依赖：
#   - Rust 工具链 + ohos 目标（rustup target add aarch64-unknown-linux-ohos x86_64-unknown-linux-ohos）
#   - DevEco Studio（提供 SDK sysroot + clang linker）
#   - devecocli（npm install -g @deveco/deveco-cli）
#
# 交叉编译配置见 rust/.cargo/config.toml（自包含，覆盖全局 ~/.cargo/config.toml）
# =============================================================================

set -euo pipefail

# ---- 路径常量 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUST_DIR="${SCRIPT_DIR}/rust"
ENTRY_DIR="${SCRIPT_DIR}/entry"
LIBS_DIR="${ENTRY_DIR}/libs"
ARM64_DIR="${LIBS_DIR}/arm64-v8a"
X86_64_DIR="${LIBS_DIR}/x86_64"

# DevEco SDK 路径（8.3 短路径避免空格问题）
DEVECO_HOME="${DEVECO_HOME:-C:/Program Files/Huawei/DevEco Studio}"
SDK_NATIVE="${DEVECO_HOME}/sdk/default/openharmony/native"

# Rust 目标三元组
TARGET_ARM64="aarch64-unknown-linux-ohos"
TARGET_X86_64="x86_64-unknown-linux-ohos"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---- 环境检查 ----
check_env() {
    log_info "环境检查..."

    # Rust
    if ! command -v cargo &>/dev/null; then
        log_error "cargo 未找到。请安装 Rust 工具链：https://rustup.rs"
        exit 1
    fi
    log_info "  cargo: $(cargo --version)"

    # DevEco SDK
    if [[ ! -d "${SDK_NATIVE}" ]]; then
        log_error "DevEco SDK native 目录不存在: ${SDK_NATIVE}"
        log_error "请设置 DEVECO_HOME 环境变量指向 DevEco Studio 安装路径"
        exit 1
    fi
    log_info "  SDK native: ${SDK_NATIVE}"

    # Rust 目标
    local missing_targets=()
    for target in "${TARGET_ARM64}" "${TARGET_X86_64}"; do
        if ! rustup target list --installed 2>/dev/null | grep -q "${target}"; then
            missing_targets+=("${target}")
        fi
    done
    if [[ ${#missing_targets[@]} -gt 0 ]]; then
        log_warn "缺少 Rust 目标: ${missing_targets[*]}"
        log_info "正在安装..."
        for target in "${missing_targets[@]}"; do
            rustup target add "${target}"
        done
    fi
    log_info "  Rust 目标: ${TARGET_ARM64}, ${TARGET_X86_64} ✓"
}

# ---- Rust 编译 ----
build_rust() {
    log_info "开始 Rust 双 ABI 编译..."

    cd "${RUST_DIR}"

    # 编译 aarch64（真机）
    log_info "  [1/2] 编译 ${TARGET_ARM64}（真机 ABI）..."
    cargo build --release --target "${TARGET_ARM64}"
    local arm64_so="${RUST_DIR}/target/${TARGET_ARM64}/release/libstorpool_napi.so"
    if [[ ! -f "${arm64_so}" ]]; then
        log_error "aarch64 编译产物未找到: ${arm64_so}"
        exit 1
    fi
    log_info "  ✓ ${TARGET_ARM64} 编译成功"

    # 编译 x86_64（模拟器）
    log_info "  [2/2] 编译 ${TARGET_X86_64}（模拟器 ABI）..."
    cargo build --release --target "${TARGET_X86_64}"
    local x86_64_so="${RUST_DIR}/target/${TARGET_X86_64}/release/libstorpool_napi.so"
    if [[ ! -f "${x86_64_so}" ]]; then
        log_error "x86_64 编译产物未找到: ${x86_64_so}"
        exit 1
    fi
    log_info "  ✓ ${TARGET_X86_64} 编译成功"

    # 复制 .so 到 entry/libs/
    log_info "复制 .so 文件到 entry/libs/..."
    mkdir -p "${ARM64_DIR}" "${X86_64_DIR}"
    cp "${arm64_so}" "${ARM64_DIR}/libstorpool_napi.so"
    cp "${x86_64_so}" "${X86_64_DIR}/libstorpool_napi.so"
    log_info "  ✓ arm64-v8a/libstorpool_napi.so"
    log_info "  ✓ x86_64/libstorpool_napi.so"

    cd "${SCRIPT_DIR}"
}

# ---- HAP 构建 ----
build_hap() {
    log_info "开始 HAP 构建（devecocli）..."

    if ! command -v devecocli &>/dev/null; then
        log_error "devecocli 未找到。请安装: npm install -g @deveco/deveco-cli"
        exit 1
    fi

    devecocli build --project "${SCRIPT_DIR}" --mode release
    log_info "  ✓ HAP 构建完成"
}

# ---- 清理 ----
clean_rust() {
    log_info "清理 Rust target 目录..."
    cd "${RUST_DIR}"
    cargo clean
    cd "${SCRIPT_DIR}"
    log_info "  ✓ 清理完成"
}

# ---- 主流程 ----
main() {
    local mode="${1:-all}"

    case "${mode}" in
        --rust)
            check_env
            build_rust
            ;;
        --hap)
            build_hap
            ;;
        --clean)
            clean_rust
            check_env
            build_rust
            build_hap
            ;;
        all|"")
            check_env
            build_rust
            build_hap
            ;;
        *)
            echo "用法: $0 [--rust|--hap|--clean]"
            exit 1
            ;;
    esac

    log_info "全部完成 ✓"
}

main "$@"