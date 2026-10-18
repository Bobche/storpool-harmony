//! StorPool NAPI 桥接层（H0b 最小验证）
//!
//! 手写 OHOS NAPI C ABI 最小绑定（零框架依赖），调用 syncthing-rust 的
//! bep-protocol 真实编解码 API，验证 Rust → NAPI → ArkTS 全链路。
//!
//! 导出函数（ArkTS 侧 `import storpool from 'libstorpool_napi.so'`）：
//! - `bridgeVersion(): string`                     — 桥接层版本信息
//! - `ping(name: string): string`                  — 参数解析回显
//! - `encodeHello(dev, client, ver): string`       — BEP Hello 编码（hex）
//! - `decodeHello(hex): object`                    — BEP Hello 解码（round-trip）
//! - `sha256Hex(buf: ArrayBuffer): Promise<string>`— H4/P1-4：async work + buffer 通道，
//!                                                  工作线程计算 sha256（不阻塞 JS 线程）
//! - `blockRanges(total, blockSize): BlockRange[]` — H4：BEP 语义分片规划（同步轻计算）

use std::ffi::{c_char, c_void, CString};
use std::ptr;
use std::time::{SystemTime, UNIX_EPOCH};

// ================= NAPI C ABI 最小绑定 =================
// 符号来自 libace_napi.z.so（ArkTS 运行时提供，sysroot 内可链接）

type napi_env = *mut c_void;
type napi_value = *mut c_void;
type napi_callback_info = *mut c_void;
type napi_status = i32;
type napi_deferred = *mut c_void;   // H4/P1-4：promise 延迟解析句柄
type napi_async_work = *mut c_void; // H4/P1-4：异步工作句柄

const NAPI_OK: napi_status = 0;
const NAPI_MODULE_VERSION: i32 = 1;

#[repr(C)]
struct NapiModule {
    nm_version: i32,
    nm_flags: u32,
    nm_filename: *const c_char,
    nm_register_func: Option<unsafe extern "C" fn(napi_env, napi_value) -> napi_value>,
    nm_modname: *const c_char,
    nm_priv: *mut c_void,
    reserved: [*mut c_void; 4],
}

// 安全性：该结构体仅在 .init_array 注册时被运行时读取，之后不再修改
unsafe impl Sync for NapiModule {}

#[link(name = "ace_napi.z")]
extern "C" {
    fn napi_module_register(module: *const NapiModule) -> napi_status;
    fn napi_create_function(
        env: napi_env,
        utf8name: *const c_char,
        length: usize,
        cb: Option<unsafe extern "C" fn(napi_env, napi_callback_info) -> napi_value>,
        data: *mut c_void,
        result: *mut napi_value,
    ) -> napi_status;
    fn napi_get_cb_info(
        env: napi_env,
        cbinfo: napi_callback_info,
        argc: *mut usize,
        argv: *mut napi_value,
        this_arg: *mut napi_value,
        data: *mut *mut c_void,
    ) -> napi_status;
    fn napi_create_string_utf8(
        env: napi_env,
        s: *const c_char,
        length: usize,
        result: *mut napi_value,
    ) -> napi_status;
    fn napi_get_value_string_utf8(
        env: napi_env,
        value: napi_value,
        buf: *mut c_char,
        bufsize: usize,
        result: *mut usize,
    ) -> napi_status;
    fn napi_create_object(env: napi_env, result: *mut napi_value) -> napi_status;
    fn napi_create_int64(env: napi_env, value: i64, result: *mut napi_value) -> napi_status;
    fn napi_set_named_property(
        env: napi_env,
        object: napi_value,
        name: *const c_char,
        value: napi_value,
    ) -> napi_status;
    // ---- H4/P1-4：async + buffer + 数值 + 数组通道 ----
    fn napi_create_promise(
        env: napi_env,
        deferred: *mut napi_deferred,
        result: *mut napi_value,
    ) -> napi_status;
    fn napi_resolve_deferred(
        env: napi_env,
        deferred: napi_deferred,
        resolution: napi_value,
    ) -> napi_status;
    fn napi_reject_deferred(
        env: napi_env,
        deferred: napi_deferred,
        rejection: napi_value,
    ) -> napi_status;
    fn napi_create_error(
        env: napi_env,
        code: napi_value,
        msg: *const c_char,
        result: *mut napi_value,
    ) -> napi_status;
    fn napi_create_async_work(
        env: napi_env,
        async_resource: napi_value,
        async_resource_name: napi_value,
        execute: Option<unsafe extern "C" fn(napi_env, *mut c_void)>,
        complete: Option<unsafe extern "C" fn(napi_env, napi_status, *mut c_void)>,
        data: *mut c_void,
        result: *mut napi_async_work,
    ) -> napi_status;
    fn napi_queue_async_work(env: napi_env, work: napi_async_work) -> napi_status;
    fn napi_delete_async_work(env: napi_env, work: napi_async_work) -> napi_status;
    fn napi_get_arraybuffer_info(
        env: napi_env,
        value: napi_value,
        data: *mut *mut c_void,
        byte_length: *mut usize,
    ) -> napi_status;
    fn napi_get_value_double(env: napi_env, value: napi_value, result: *mut f64) -> napi_status;
    fn napi_create_array_with_length(
        env: napi_env,
        length: usize,
        result: *mut napi_value,
    ) -> napi_status;
    fn napi_set_element(env: napi_env, object: napi_value, index: u32, value: napi_value) -> napi_status;
}

// ================= 辅助函数 =================

unsafe fn make_string(env: napi_env, s: &str) -> napi_value {
    let mut result: napi_value = ptr::null_mut();
    let cs = CString::new(s).unwrap_or_else(|_| CString::new("<invalid utf8>").unwrap());
    let st = unsafe { napi_create_string_utf8(env, cs.as_ptr(), s.len(), &mut result) };
    if st != NAPI_OK {
        return ptr::null_mut();
    }
    result
}

/// 读取第 `index` 个字符串参数（两段式：先取长度再取内容）
unsafe fn arg_string(
    env: napi_env,
    info: napi_callback_info,
    index: usize,
) -> Result<String, String> {
    let mut argc: usize = 8;
    let mut argv: [napi_value; 8] = [ptr::null_mut(); 8];
    let st = unsafe {
        napi_get_cb_info(env, info, &mut argc, argv.as_mut_ptr(), ptr::null_mut(), ptr::null_mut())
    };
    if st != NAPI_OK {
        return Err(format!("napi_get_cb_info failed: {st}"));
    }
    if argc <= index {
        return Err(format!("missing arg #{index} (got {argc})"));
    }
    let mut len: usize = 0;
    let st = unsafe { napi_get_value_string_utf8(env, argv[index], ptr::null_mut(), 0, &mut len) };
    if st != NAPI_OK {
        return Err(format!("napi_get_value_string_utf8(len) failed: {st}"));
    }
    let mut buf: Vec<u8> = vec![0u8; len + 1];
    let mut written: usize = 0;
    let st = unsafe {
        napi_get_value_string_utf8(
            env,
            argv[index],
            buf.as_mut_ptr() as *mut c_char,
            buf.len(),
            &mut written,
        )
    };
    if st != NAPI_OK {
        return Err(format!("napi_get_value_string_utf8 failed: {st}"));
    }
    Ok(String::from_utf8_lossy(&buf[..written]).into_owned())
}

unsafe fn export_fn(
    env: napi_env,
    exports: napi_value,
    name: &str,
    f: unsafe extern "C" fn(napi_env, napi_callback_info) -> napi_value,
) {
    let cname = CString::new(name).unwrap();
    let mut fn_val: napi_value = ptr::null_mut();
    let st = unsafe {
        napi_create_function(env, cname.as_ptr(), name.len(), Some(f), ptr::null_mut(), &mut fn_val)
    };
    if st != NAPI_OK {
        return;
    }
    unsafe { napi_set_named_property(env, exports, cname.as_ptr(), fn_val) };
}

unsafe fn set_str(env: napi_env, obj: napi_value, key: &str, val: &str) {
    let v = unsafe { make_string(env, val) };
    let ck = CString::new(key).unwrap();
    unsafe { napi_set_named_property(env, obj, ck.as_ptr(), v) };
}

unsafe fn set_i64(env: napi_env, obj: napi_value, key: &str, val: i64) {
    let mut v: napi_value = ptr::null_mut();
    if unsafe { napi_create_int64(env, val, &mut v) } == NAPI_OK {
        let ck = CString::new(key).unwrap();
        unsafe { napi_set_named_property(env, obj, ck.as_ptr(), v) };
    }
}

fn hex_encode(data: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(data.len() * 2);
    for &b in data {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

fn hex_decode(s: &str) -> Result<Vec<u8>, String> {
    fn val(c: u8) -> Result<u8, String> {
        match c {
            b'0'..=b'9' => Ok(c - b'0'),
            b'a'..=b'f' => Ok(c - b'a' + 10),
            b'A'..=b'F' => Ok(c - b'A' + 10),
            _ => Err(format!("invalid hex char: {}", c as char)),
        }
    }
    let bytes = s.as_bytes();
    if bytes.len() % 2 != 0 {
        return Err("hex length must be even".into());
    }
    let mut out = Vec::with_capacity(bytes.len() / 2);
    for pair in bytes.chunks(2) {
        out.push((val(pair[0])? << 4) | val(pair[1])?);
    }
    Ok(out)
}

/// 读取第 `index` 个 number 参数（f64，ArkTS number）
unsafe fn arg_f64(env: napi_env, info: napi_callback_info, index: usize) -> Result<f64, String> {
    let mut argc: usize = 8;
    let mut argv: [napi_value; 8] = [ptr::null_mut(); 8];
    let st = unsafe {
        napi_get_cb_info(env, info, &mut argc, argv.as_mut_ptr(), ptr::null_mut(), ptr::null_mut())
    };
    if st != NAPI_OK {
        return Err(format!("napi_get_cb_info failed: {st}"));
    }
    if argc <= index {
        return Err(format!("missing arg #{index} (got {argc})"));
    }
    let mut v: f64 = 0.0;
    let st = unsafe { napi_get_value_double(env, argv[index], &mut v) };
    if st != NAPI_OK {
        return Err(format!("napi_get_value_double failed: {st}"));
    }
    Ok(v)
}

/// 构造 Error 对象（reject 用）
unsafe fn make_error(env: napi_env, msg: &str) -> napi_value {
    let mut e: napi_value = ptr::null_mut();
    let cmsg = CString::new(msg).unwrap_or_else(|_| CString::new("error").unwrap());
    let st = unsafe { napi_create_error(env, ptr::null_mut(), cmsg.as_ptr(), &mut e) };
    if st != NAPI_OK {
        return ptr::null_mut();
    }
    e
}

fn now_millis() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

// ================= 导出的 NAPI 函数 =================

/// `bridgeVersion(): string` — 桥接层与被链接核心的信息
unsafe extern "C" fn napi_bridge_version(env: napi_env, _info: napi_callback_info) -> napi_value {
    let s = format!(
        "storpool-napi {} | bridge ok at t={} | bep-protocol: syncthing-rust",
        env!("CARGO_PKG_VERSION"),
        now_millis()
    );
    unsafe { make_string(env, &s) }
}

/// `ping(name: string): string` — 验证参数解析与字符串回传
unsafe extern "C" fn napi_ping(env: napi_env, info: napi_callback_info) -> napi_value {
    match unsafe { arg_string(env, info, 0) } {
        Ok(name) => unsafe { make_string(env, &format!("pong: {name}")) },
        Err(e) => unsafe { make_string(env, &format!("ping error: {e}")) },
    }
}

/// `encodeHello(deviceName, clientName, clientVersion): string`
/// 构造真实 BEP Hello 消息并编码为 hex —— 证明 syncthing-rust 协议层真实工作
unsafe extern "C" fn napi_encode_hello(env: napi_env, info: napi_callback_info) -> napi_value {
    let dev = unsafe { arg_string(env, info, 0) };
    let cn = unsafe { arg_string(env, info, 1) };
    let cv = unsafe { arg_string(env, info, 2) };
    let (dev, cn, cv) = match (dev, cn, cv) {
        (Ok(a), Ok(b), Ok(c)) => (a, b, c),
        _ => {
            return unsafe { make_string(env, "encodeHello expects (deviceName, clientName, clientVersion)") }
        }
    };
    let hello = bep_protocol::Hello::new(dev, cn, cv);
    match bep_protocol::encode_message(&hello) {
        Ok(bytes) => unsafe { make_string(env, &hex_encode(&bytes)) },
        Err(e) => unsafe { make_string(env, &format!("encode error: {e}")) },
    }
}

/// `decodeHello(hex): object` — 解码 Hello，与 encodeHello 构成 round-trip
unsafe extern "C" fn napi_decode_hello(env: napi_env, info: napi_callback_info) -> napi_value {
    let hex = match unsafe { arg_string(env, info, 0) } {
        Ok(h) => h,
        Err(e) => return unsafe { make_string(env, &format!("decodeHello error: {e}")) },
    };
    let bytes = match hex_decode(&hex) {
        Ok(b) => b,
        Err(e) => return unsafe { make_string(env, &format!("hex error: {e}")) },
    };
    match bep_protocol::decode_message::<bep_protocol::Hello>(&bytes) {
        Ok(h) => unsafe { hello_to_object(env, &h) },
        Err(e) => unsafe { make_string(env, &format!("decode error: {e}")) },
    }
}

unsafe fn hello_to_object(env: napi_env, h: &bep_protocol::Hello) -> napi_value {
    let mut obj: napi_value = ptr::null_mut();
    if unsafe { napi_create_object(env, &mut obj) } != NAPI_OK {
        return ptr::null_mut();
    }
    unsafe {
        set_str(env, obj, "deviceName", &h.device_name);
        set_str(env, obj, "clientName", &h.client_name);
        set_str(env, obj, "clientVersion", &h.client_version);
        set_i64(env, obj, "numConnections", h.num_connections as i64);
        set_i64(env, obj, "timestamp", h.timestamp);
    }
    obj
}

// ================= H4/P1-4：sha256Hex（async work + ArrayBuffer 通道） =================

struct Sha256Task {
    bytes: Vec<u8>,
    hex: String,
    deferred: napi_deferred,
    work: napi_async_work,
}

/// 工作线程执行体：只做 hash 计算，绝不触碰 napi_env（NAPI async 铁律）
unsafe extern "C" fn sha256_execute(_env: napi_env, data: *mut c_void) {
    use sha2::{Digest, Sha256};
    let task = unsafe { &mut *(data as *mut Sha256Task) };
    let mut h = Sha256::new();
    h.update(&task.bytes);
    task.hex = hex_encode(&h.finalize());
}

/// JS 线程完成体：resolve promise → 删除 async work → 释放 task
unsafe extern "C" fn sha256_complete(env: napi_env, status: napi_status, data: *mut c_void) {
    let task = unsafe { Box::from_raw(data as *mut Sha256Task) };
    if status == NAPI_OK {
        let v = unsafe { make_string(env, &task.hex) };
        unsafe { napi_resolve_deferred(env, task.deferred, v) };
    } else {
        let e = unsafe { make_error(env, "sha256Hex async work aborted") };
        unsafe { napi_reject_deferred(env, task.deferred, e) };
    }
    unsafe { napi_delete_async_work(env, task.work) };
}

/// `sha256Hex(buf: ArrayBuffer): Promise<string>` — 工作线程计算 sha256，hex 回传
unsafe extern "C" fn napi_sha256_hex(env: napi_env, info: napi_callback_info) -> napi_value {
    let mut deferred: napi_deferred = ptr::null_mut();
    let mut promise: napi_value = ptr::null_mut();
    if unsafe { napi_create_promise(env, &mut deferred, &mut promise) } != NAPI_OK {
        return ptr::null_mut();
    }
    // 解析参数 0：ArrayBuffer（先建 promise 再解析，任何失败路径都返回合法 promise）
    let mut argc: usize = 8;
    let mut argv: [napi_value; 8] = [ptr::null_mut(); 8];
    let got = unsafe {
        napi_get_cb_info(env, info, &mut argc, argv.as_mut_ptr(), ptr::null_mut(), ptr::null_mut())
    };
    if got != NAPI_OK || argc < 1 {
        let e = unsafe { make_error(env, "sha256Hex expects (ArrayBuffer)") };
        unsafe { napi_reject_deferred(env, deferred, e) };
        return promise;
    }
    let mut data_ptr: *mut c_void = ptr::null_mut();
    let mut data_len: usize = 0;
    if unsafe { napi_get_arraybuffer_info(env, argv[0], &mut data_ptr, &mut data_len) } != NAPI_OK {
        let e = unsafe { make_error(env, "arg #0 is not an ArrayBuffer") };
        unsafe { napi_reject_deferred(env, deferred, e) };
        return promise;
    }
    // 拷贝出 buffer 内容：async 期间 JS 侧可能 detach/复用，拷贝最安全
    let bytes: Vec<u8> =
        unsafe { std::slice::from_raw_parts(data_ptr as *const u8, data_len) }.to_vec();
    let task = Box::into_raw(Box::new(Sha256Task {
        bytes: bytes,
        hex: String::new(),
        deferred: deferred,
        work: ptr::null_mut(),
    }));
    let name = unsafe { make_string(env, "sha256Hex") };
    let mut work: napi_async_work = ptr::null_mut();
    let st = unsafe {
        napi_create_async_work(
            env,
            ptr::null_mut(),
            name,
            Some(sha256_execute),
            Some(sha256_complete),
            task as *mut c_void,
            &mut work,
        )
    };
    if st != NAPI_OK {
        let task = unsafe { Box::from_raw(task) };
        let e = unsafe { make_error(env, "napi_create_async_work failed") };
        unsafe { napi_reject_deferred(env, task.deferred, e) };
        return promise;
    }
    unsafe { (*task).work = work };
    if unsafe { napi_queue_async_work(env, work) } != NAPI_OK {
        let task = unsafe { Box::from_raw(task) };
        let e = unsafe { make_error(env, "napi_queue_async_work failed") };
        unsafe { napi_reject_deferred(env, task.deferred, e) };
        unsafe { napi_delete_async_work(env, work) };
        return promise;
    }
    promise
}

// ================= H4：blockRanges（BEP 语义分片规划，同步轻计算） =================

/// `blockRanges(totalSize: number, blockSize: number): BlockRange[]`
/// 返回 [{index, offset, length}]；最后一块自动截断到 totalSize。
unsafe extern "C" fn napi_block_ranges(env: napi_env, info: napi_callback_info) -> napi_value {
    let total = unsafe { arg_f64(env, info, 0) };
    let bs = unsafe { arg_f64(env, info, 1) };
    let (total, bs) = match (total, bs) {
        (Ok(a), Ok(b)) => (a, b),
        _ => {
            return unsafe {
                make_string(env, "blockRanges expects (totalSize: number, blockSize: number)")
            }
        }
    };
    if total <= 0.0 || bs <= 0.0 {
        let mut empty: napi_value = ptr::null_mut();
        if unsafe { napi_create_array_with_length(env, 0, &mut empty) } == NAPI_OK {
            return empty;
        }
        return ptr::null_mut();
    }
    let n = ((total + bs - 1.0) / bs).floor() as usize;
    let mut arr: napi_value = ptr::null_mut();
    if unsafe { napi_create_array_with_length(env, n, &mut arr) } != NAPI_OK {
        return ptr::null_mut();
    }
    for i in 0..n {
        let offset: f64 = i as f64 * bs;
        let len: f64 = if i + 1 >= n { total - offset } else { bs };
        let mut obj: napi_value = ptr::null_mut();
        if unsafe { napi_create_object(env, &mut obj) } != NAPI_OK {
            continue;
        }
        unsafe {
            set_i64(env, obj, "index", i as i64);
            set_i64(env, obj, "offset", offset as i64);
            set_i64(env, obj, "length", len as i64);
            napi_set_element(env, arr, i as u32, obj);
        }
    }
    arr
}

// ================= 模块注册 =================

unsafe extern "C" fn module_init(env: napi_env, exports: napi_value) -> napi_value {
    unsafe {
        export_fn(env, exports, "bridgeVersion", napi_bridge_version);
        export_fn(env, exports, "ping", napi_ping);
        export_fn(env, exports, "encodeHello", napi_encode_hello);
        export_fn(env, exports, "decodeHello", napi_decode_hello);
        export_fn(env, exports, "sha256Hex", napi_sha256_hex);
        export_fn(env, exports, "blockRanges", napi_block_ranges);
    }
    exports
}

/// nm_modname 必须与 so 文件名匹配：libstorpool_napi.so → "storpool_napi"
static MODULE: NapiModule = NapiModule {
    nm_version: NAPI_MODULE_VERSION,
    nm_flags: 0,
    nm_filename: ptr::null(),
    nm_register_func: Some(module_init),
    nm_modname: b"storpool_napi\0".as_ptr() as *const c_char,
    nm_priv: ptr::null_mut(),
    reserved: [ptr::null_mut(); 4],
};

/// 链接器将此函数指针放入 `.init_array`，.so 被 dlopen 时自动注册 NAPI 模块
/// （等价于 C 侧 `__attribute__((constructor))`，OHOS 同样支持）
#[link_section = ".init_array"]
#[used]
static REGISTER_MODULE: unsafe extern "C" fn() = register_module;

unsafe extern "C" fn register_module() {
    unsafe { napi_module_register(&MODULE) };
}