/**
 * StorPool NAPI 桥接封装层
 *
 * 底层实现：rust/storpool-napi（Rust cdylib → libstorpool_napi.so）
 * 核心能力：syncthing-rust bep-protocol 真实 BEP 编解码
 *
 * H1 起设备注册/心跳/元数据同步等接口在本层继续扩展。
 */
export { bridgeVersion, ping, encodeHello, decodeHello, HelloInfo } from 'libstorpool_napi.so';