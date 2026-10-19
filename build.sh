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
#   ./build.sh --check    # OpenAPI 契约一致性检查（tools/contract_check.py，fail → 非零）
#   ./build.sh --doctor  # 三机环境自诊断（输出 build/doctor-report.md）
#
# 前置依赖：
#   - Rust 工具链 + ohos 目标（rustup target add aarch64-unknown-linux-ohos x86_64-unknown-linux-ohos）
#   - DevEco Studio（提供 SDK sysroot + clang linker）
#   - devecocli（npm install -g @deveco/deveco-cli）
#
# 多机可移植性（round 4）：
#   - DEVECO_HOME 三路径探测：环境变量 → Program Files → %LOCALAPPDATA%
#   - 路径含空格时自动转 8.3 短路径（PowerShell Scripting.FileSystemObject）
#   - 交叉编译配置以 cargo 环境变量注入（CARGO_TARGET_*_LINKER/_RUSTFLAGS + CC_*）
#   - rust/.cargo/config.toml 保持原样（A 机路径作为默认，环境变量优先级覆盖）
# =============================================================================

set -euo pipefail

# ---- 路径常量 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUST_DIR="${SCRIPT_DIR}/rust"
ENTRY_DIR="${SCRIPT_DIR}/entry"
LIBS_DIR="${ENTRY_DIR}/libs"
ARM64_DIR="${LIBS_DIR}/arm64-v8a"
X86_64_DIR="${LIBS_DIR}/x86_64"

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

# ---- DEVECO_HOME 三路径探测 ----
# 顺序：环境变量 DEVECO_HOME → C:\Program Files\Huawei\DevEco Studio → %LOCALAPPDATA%\Huawei\DevEco Studio
# 取第一个存在 sdk/default/openharmony/native 的
detect_deveco_home() {
    local candidates=()

    # 1. 环境变量 DEVECO_HOME（用户显式指定）
    if [[ -n "${DEVECO_HOME:-}" ]]; then
        candidates+=("${DEVECO_HOME}")
    fi

    # 2. C:\Program Files\Huawei\DevEco Studio（A 机默认安装路径）
    candidates+=("C:/Program Files/Huawei/DevEco Studio")

    # 3. %LOCALAPPDATA%\Huawei\DevEco Studio（C 机默认安装路径）
    if [[ -n "${LOCALAPPDATA:-}" ]]; then
        # Windows 反斜杠 → 正斜杠
        local localappdata_norm="${LOCALAPPDATA//\\//}"
        candidates+=("${localappdata_norm}/Huawei/DevEco Studio")
    fi

    for candidate in "${candidates[@]}"; do
        local native_dir="${candidate}/sdk/default/openharmony/native"
        if [[ -d "${native_dir}" ]]; then
            DEVECO_HOME="${candidate}"
            return 0
        fi
    done

    log_error "DevEco Studio 未找到。请："
    log_error "  1. 安装 DevEco Studio: https://developer.huawei.com/consumer/cn/download/deveco-studio"
    log_error "  2. 或设置 DEVECO_HOME 环境变量指向安装路径"
    exit 1
}

# ---- 8.3 短路径转换（路径含空格时） ----
# PowerShell Scripting.FileSystemObject.GetFolder().ShortPath 返回 8.3 短路径
# 如 "C:/Program Files/Huawei/DevEco Studio" → "C:/PROGRA~1/Huawei/DEVECO~1"
to_short_path() {
    local path="$1"
    # 路径不含空格则直接返回
    if [[ "${path}" != *" "* ]]; then
        echo "${path}"
        return 0
    fi

    # 用 PowerShell Scripting.FileSystemObject 转 8.3 短路径
    local short_path
    short_path=$(powershell -Command "(New-Object -ComObject Scripting.FileSystemObject).GetFolder('${path}').ShortPath" 2>/dev/null | tr -d '\r' | tr '\\' '/')

    if [[ -n "${short_path}" && "${short_path}" != *" "* ]]; then
        echo "${short_path}"
    else
        # 转换失败或结果仍含空格，返回原路径（clang 可能仍能处理）
        log_warn "8.3 短路径转换失败，使用原路径: ${path}"
        echo "${path}"
    fi
}

# ---- 交叉编译环境注入 ----
# 以 cargo 环境变量形式注入 linker/sysroot/rustflags/CC 等
# 环境变量优先级高于 rust/.cargo/config.toml，实现多机可移植
# config.toml 内 A 机路径作为默认（有 config.toml 时），环境变量覆盖之
setup_cross_compile_env() {
    local deveco_short
    deveco_short=$(to_short_path "${DEVECO_HOME}")

    local sdk_native="${deveco_short}/sdk/default/openharmony/native"
    local clang="${sdk_native}/llvm/bin/clang.exe"
    local clangxx="${sdk_native}/llvm/bin/clang++.exe"
    local llvm_ar="${sdk_native}/llvm/bin/llvm-ar.exe"
    local sysroot="${sdk_native}/sysroot"

    local arm64_lib_dir="${sysroot}/usr/lib/aarch64-linux-ohos"
    local x86_64_lib_dir="${sysroot}/usr/lib/x86_64-linux-ohos"

    # CARGO_TARGET_*_LINKER（环境变量优先级高于 config.toml）
    export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_OHOS_LINKER="${clang}"
    export CARGO_TARGET_X86_64_UNKNOWN_LINUX_OHOS_LINKER="${clang}"

    # CARGO_TARGET_*_RUSTFLAGS（环境变量优先级高于 config.toml）
    # 短路径版 rustflags 与 config.toml 内 A 机路径共存时 clang 忽略无效 -L（C 实测）
    export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_OHOS_RUSTFLAGS="-C link-arg=--target=aarch64-unknown-linux-ohos -C link-arg=--sysroot=${sysroot} -C link-arg=-L${arm64_lib_dir}"
    export CARGO_TARGET_X86_64_UNKNOWN_LINUX_OHOS_RUSTFLAGS="-C link-arg=--target=x86_64-unknown-linux-ohos -C link-arg=--sysroot=${sysroot} -C link-arg=-L${x86_64_lib_dir}"

    # CC_*, CXX_*, AR_*, CFLAGS_*, CXXFLAGS_*（cc-rs 编译 C 依赖如 ring 用）
    export CC_aarch64_unknown_linux_ohos="${clang}"
    export CXX_aarch64_unknown_linux_ohos="${clangxx}"
    export AR_aarch64_unknown_linux_ohos="${llvm_ar}"
    export CFLAGS_aarch64_unknown_linux_ohos="--sysroot=${sysroot}"
    export CXXFLAGS_aarch64_unknown_linux_ohos="--sysroot=${sysroot}"

    export CC_x86_64_unknown_linux_ohos="${clang}"
    export CXX_x86_64_unknown_linux_ohos="${clangxx}"
    export AR_x86_64_unknown_linux_ohos="${llvm_ar}"
    export CFLAGS_x86_64_unknown_linux_ohos="--sysroot=${sysroot}"
    export CXXFLAGS_x86_64_unknown_linux_ohos="--sysroot=${sysroot}"

    # OHOS_NATIVE（cc-rs 用）
    export OHOS_NATIVE="${sdk_native}"

    log_info "  SDK native (短路径): ${sdk_native}"
}

# ---- 环境检查 ----
check_env() {
    log_info "环境检查..."

    # Rust
    if ! command -v cargo &>/dev/null; then
        log_error "cargo 未找到。请安装 Rust 工具链：https://rustup.rs"
        exit 1
    fi
    log_info "  cargo: $(cargo --version)"

    # DevEco SDK（三路径探测）
    detect_deveco_home
    local sdk_native="${DEVECO_HOME}/sdk/default/openharmony/native"
    if [[ ! -d "${sdk_native}" ]]; then
        log_error "DevEco SDK native 目录不存在: ${sdk_native}"
        log_error "请设置 DEVECO_HOME 环境变量指向 DevEco Studio 安装路径"
        exit 1
    fi
    log_info "  DEVECO_HOME: ${DEVECO_HOME}"

    # 交叉编译环境注入（8.3 短路径 + cargo 环境变量）
    setup_cross_compile_env

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

# ---- OpenAPI 契约一致性检查（round 5） ----
# 定位仓库根（build.sh 所在目录）后调用 tools/contract_check.py，
# 透传其退出码：15/15 PASS → 0；任一 FAIL → 非零（set -e 下脚本以非零退出）。
# 独立子命令，不触碰 Rust/HAP 构建路径（check_env/build_rust/build_hap 均不调用）。
run_contract_check() {
    log_info "OpenAPI 契约一致性检查（tools/contract_check.py）..."

    if ! command -v python &>/dev/null; then
        log_error "python 未找到。请安装 Python 3.12+（仅 stdlib 依赖）"
        exit 1
    fi

    # 定位仓库根：SCRIPT_DIR 即 build.sh 所在目录（仓库根）
    local check_script="${SCRIPT_DIR}/tools/contract_check.py"
    if [[ ! -f "${check_script}" ]]; then
        log_error "contract_check.py 未找到: ${check_script}"
        exit 1
    fi

    # 透传退出码（contract_check 内部 fail → sys.exit(1)）
    python "${check_script}"
}

# ---- 三机环境自诊断（round 6） ----
# 输出标准化对比报告，换机排障 = diff 三份报告。
# 退出码：关键项（python/git/仓库完整性）缺损 → 非零；工具类缺失仅 INFO。
doctor() {
    log_info "三机环境自诊断..."

    # 仓库根（SCRIPT_DIR 即 build.sh 所在目录）
    local repo_root="${SCRIPT_DIR}"
    local report="${repo_root}/build/doctor-report.md"
    mkdir -p "$(dirname "${report}")"

    # 收集项
    local items=()
    local status_pass=0
    local status_info=0
    local status_miss=0

    # 工具：探测并记录，返回状态 PASS/INFO/MISS
    add_item() {
        items+=("$1|$2|$3")
        case "$3" in
            PASS) status_pass=$((status_pass + 1)) ;;
            INFO) status_info=$((status_info + 1)) ;;
            MISS) status_miss=$((status_miss + 1)) ;;
        esac
    }

    # ---- OS / hostname ----
    local os_name="$(uname -s 2>/dev/null || echo unknown)"
    local host_name="$(hostname 2>/dev/null || echo unknown)"
    add_item "OS" "${os_name}" "INFO"
    add_item "hostname" "${host_name}" "INFO"

    # ---- Python ----
    local py_ver=""
    if command -v python &>/dev/null; then
        py_ver="$(python --version 2>&1 || echo 'unknown')"
        add_item "python" "${py_ver}" "PASS"
    else
        add_item "python" "NOT FOUND" "MISS"
    fi

    # ---- Git ----
    if command -v git &>/dev/null; then
        local git_ver="$(git --version 2>/dev/null || echo unknown)"
        add_item "git" "${git_ver}" "PASS"
        local ac="$(git config --get core.autocrlf 2>/dev/null || echo unset)"
        add_item "git config core.autocrlf" "${ac}" "INFO"
        local sslb="$(git config --get http.sslBackend 2>/dev/null || echo unset)"
        add_item "git config http.sslBackend" "${sslb}" "INFO"
        local proxy="$(git config --get http.proxy 2>/dev/null || echo unset)"
        add_item "git config http.proxy" "${proxy:-unset}" "INFO"
    else
        add_item "git" "NOT FOUND" "MISS"
    fi

    # ---- devecocli ----
    if command -v devecocli &>/dev/null; then
        local dc_ver="$(devecocli --version 2>/dev/null || echo unknown)"
        add_item "devecocli" "${dc_ver}" "PASS"
    else
        add_item "devecocli" "NOT FOUND (npm install -g @deveco/deveco-cli)" "MISS"
    fi

    # ---- DevEco 三路径探测（复用既有函数） ----
    local deveco_found=""
    if detect_deveco_home 2>/dev/null; then
        deveco_found="${DEVECO_HOME}"
        add_item "DevEco Studio" "${deveco_found}" "PASS"
    else
        add_item "DevEco Studio" "NOT FOUND" "MISS"
    fi

    # ---- hdc 可达性 ----
    if command -v hdc &>/dev/null; then
        local hdc_targets="$(hdc list targets 2>/dev/null | grep -v "^$" || echo empty)"
        local hdc_count=$(echo "${hdc_targets}" | grep -c . 2>/dev/null || echo 0)
        add_item "hdc targets" "${hdc_count} device(s)" "INFO"
    else
        add_item "hdc" "NOT FOUND (DevEco SDK toolchains)" "INFO"
    fi

    # ---- cargo（缺失仅 INFO——机器角色不同） ----
    if command -v cargo &>/dev/null; then
        local cr="$(cargo --version 2>/dev/null || echo unknown)"
        add_item "cargo" "${cr}" "INFO"
    else
        add_item "cargo" "NOT FOUND" "INFO"
    fi

    # ---- node ----
    if command -v node &>/dev/null; then
        local nv="$(node --version 2>/dev/null || echo unknown)"
        add_item "node" "${nv}" "INFO"
    else
        add_item "node" "NOT FOUND" "INFO"
    fi

    # ---- entry/libs 双 ABI .so 存在性与字节数 ----
    local arm64_so="${repo_root}/entry/libs/arm64-v8a/libstorpool_napi.so"
    local x86_64_so="${repo_root}/entry/libs/x86_64/libstorpool_napi.so"
    if [[ -f "${arm64_so}" ]]; then
        add_item "entry/libs/arm64-v8a/libstorpool_napi.so" "$(wc -c < "${arm64_so}") bytes" "INFO"
    else
        add_item "entry/libs/arm64-v8a/libstorpool_napi.so" "NOT FOUND" "INFO"
    fi
    if [[ -f "${x86_64_so}" ]]; then
        add_item "entry/libs/x86_64/libstorpool_napi.so" "$(wc -c < "${x86_64_so}") bytes" "INFO"
    else
        add_item "entry/libs/x86_64/libstorpool_napi.so" "NOT FOUND" "INFO"
    fi

    # ---- 写报告 ----
    {
        echo "# StorPool HarmonyOS — 三机环境自诊断报告"
        echo ""
        echo "生成时间: $(date '+%Y-%m-%dT%H:%M:%S')"
        echo "仓库根: ${repo_root}"
        echo ""
        echo "| 项 | 值 | 状态 |"
        echo "|---|---|---|"
        local IFS='|'
        for entry in "${items[@]}"; do
            read -r key val status <<< "${entry}"
            echo "| ${key} | ${val} | ${status} |"
        done
        echo ""
        echo "汇总: ${status_pass} PASS / ${status_info} INFO / ${status_miss} MISS"
        if [[ ${status_miss} -gt 0 ]]; then
            echo ""
            echo "> 关键项缺损（MISS），请按上表排查。"
        fi
    } > "${report}"

    log_info "  报告: ${report}"
    log_info "  汇总: ${status_pass} PASS / ${status_info} INFO / ${status_miss} MISS"

    # 关键项缺损 → 非零退出
    if [[ ${status_miss} -gt 0 ]]; then
        return 1
    fi
    return 0
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
        --check)
            run_contract_check
            ;;
        --doctor)
            doctor || exit $?
            ;;
        all|"")
            check_env
            build_rust
            build_hap
            ;;
        *)
            echo "用法: $0 [--rust|--hap|--clean|--check|--doctor]"
            exit 1
            ;;
    esac

    log_info "全部完成 ✓"
}

main "$@"