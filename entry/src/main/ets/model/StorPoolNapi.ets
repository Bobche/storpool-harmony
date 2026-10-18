/**
 * StorPool NAPI 桥接封装层
 *
 * 底层实现：rust/storpool-napi（Rust cdylib → libstorpool_napi.so）
 * 核心能力：syncthing-rust bep-protocol 真实 BEP 编解码
 *
 * H4 新增：sha256Hex（async work 工作线程计算）/ blockRanges（BEP 语义分片规划）
 */
export { bridgeVersion, ping, encodeHello, decodeHello, sha256Hex, blockRanges,
  HelloInfo, BlockRange } from 'libstorpool_napi.so';