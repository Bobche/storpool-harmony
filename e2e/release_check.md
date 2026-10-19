# Release 构建全链路验证规程（P2-7，2026-10-19）

> 机器 C 领地文档。首次全链路验证于 round6 完成（代码基线 main@a1e354e），
> 本文档沉淀可复用的命令级规程与已知事实，供后续 release 回归/送审前检查使用。

## 1. 前置条件

- 模拟器在线（`hdc list targets` 有且仅有 `127.0.0.1:5555`）
- `devecocli` 可用（1.3.0+）
- 工作区干净，处于待验证代码基线

## 2. 构建与产物核验

```bash
# 全量干净构建（避免增量缓存干扰首次验证）
devecocli build clean
devecocli build --build-mode release 2>&1 | tee e2e/logs/release_build.log

# 产物（hvigor 不按 buildMode 分目录，debug/release 共用同一路径，后构建覆盖先构建）
#   entry/build/default/outputs/default/entry-default-unsigned.hap

# 双 ABI .so 完整性核验
unzip -l entry/build/default/outputs/default/entry-default-unsigned.hap
#   验证点：libs/arm64-v8a/libstorpool_napi.so 与 libs/x86_64/libstorpool_napi.so 均在列

# 尺寸对比（debug 基线另跑一次 clean+debug 构建）
ls -la entry/build/default/outputs/default/entry-default-unsigned.hap
```

**r6 实测基线**（a1e354e）：release HAP 988,020 B（964.9 KiB）vs debug HAP
1,112,146 B（1086.1 KiB），release 小 121.2 KiB（11.2%）；双 ABI .so =
arm64-v8a 380,536 B + x86_64 406,840 B（release strip 过）。

## 3. 已知事实（无需告警）

| 现象 | 定性 |
|------|------|
| `WARN: Will skip sign 'hos_hap'. No signingConfigs profile is configured` | 预期。项目 signingConfigs 为空，产物 unsigned；**模拟器允许安装 unsigned HAP**，真机/送审需签名（P2-9 AGC 链路，材料办理中） |
| 混淆 mapping 无输出 | 预期。保守集仅 `-enable-filename-obfuscation` 且未配置 `-print-namecache`。将来放开激进混淆（property/toplevel/export）前**必须**加 `-print-namecache <path>` 留崩溃还原映射，并过 e2e 全量回归 |
| release abc 中原始文件路径全保留 | r6 实测：`-enable-filename-obfuscation` 对当前代码结构无可见变化（`entry/src/main/ets/pages/Index.ets` 等 21 处路径原样）。标识符零改动 → NAPI named import / JSON 字段 / 路由零断裂风险（P1-9 保守集本意）。abc 与 debug 的差异来自调试信息剥离（字符串数 1169→1090） |
| `DoNativeStrip` 任务出现 | 正常。release 模式 strip .so（3938ce3 入库的就是 strip 后产物） |

## 4. e2e 三场景验收（release HAP）

```bash
HAP=e2e/logs/<备份的release产物>.hap   # 或直接用默认路径产物

# 1) 全量（debug 基线 30P/0F/1S，约 96s）
python e2e/e2e.py --phase all --skip-build --hap $HAP --json e2e/logs/e2e_release_all.json

# 2) heal401 单跑（基线 11P/0F/1S，约 38s）
python e2e/e2e.py --phase heal401 --skip-build --hap $HAP --json e2e/logs/e2e_release_heal401.json

# 3) soak 冒烟（全绿即可）
python e2e/soak.py --minutes 2 --ui-loops 2 --skip-build --hap $HAP --json e2e/logs/soak_release_smoke.json
```

**NAPI 不断裂证据链**（混淆回归的硬指标）：
- `H0b NAPI 桥接检查全通过`（UI 文案「全部通过 4/4」）
- `H4 分片下载成功`（blockRanges NAPI）
- `H4 落盘文件 sha256 三方一致`（sha256Hex NAPI）
- 应用启动无 `libmodname`/`dlopen` 类加载错误（注册+心跳正常即证）

**r6 实测**：三场景全部与 debug 基线逐项一致（30P/0F/1S @91.8s；11P/0F/1S @37.9s；
soak 冒烟阶段 A hb 1→9、阶段 B 2/2 自愈、pid 0 变化、泄漏判定全部门内）。

## 5. 注意事项

- 构建产物与运行日志（`e2e/logs/`）一律不入库
- 若验证需要 debug/release 双产物对比，先构建的模式产物要**先拷贝备份**
  （同一路径会被后构建覆盖）
- e2e/soak.py 不带 `--skip-build` 时默认构建 debug——release 验证必须显式
  `--skip-build --hap <release产物>`