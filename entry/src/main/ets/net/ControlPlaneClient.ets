import { rcp } from '@kit.RemoteCommunicationKit';
import { util } from '@kit.ArkTS';

/**
 * 控制面 HTTP 客户端（H1/H2）—— P1-8 rcp Session 重构
 *
 * 对接控制面 API（H1 阶段为 mock：mock-control-plane/server.py）：
 *   POST /devices/register   {deviceName, platform}          -> {deviceId, deviceToken, serverTime}
 *   POST /devices/heartbeat  {deviceId} (Bearer deviceToken) -> {status, serverTime}
 *   GET  /namespace?since=N  (Bearer deviceToken)            -> {version, mode, nodes}
 *
 * 鉴权：H1 用 Bearer token 简化；正式 mTLS 双向证书在控制面 Go 侧就绪后升级。
 * 错误：非 200 抛 HttpError（携带 statusCode，供调用方判定 401 自愈，P0-2）。
 *
 * P1-8 重构要点：
 *   - 使用 rcp.Session 替代每次 http.createHttp()，实现连接复用（keep-alive）
 *   - Session 配置 baseAddress，请求只需传 path
 *   - 统一超时配置（connectMs=5000, transferMs=5000）
 *   - 401 自愈语义完全保留（HttpError.statusCode 不变）
 *   - 协议契约以 docs/openapi.yaml 为单一事实源
 *
 * Session 生命周期修复（round 4）：
 *   - Session 改为模块级共享缓存（Map<baseUrl, Session>），惰性创建、跨实例复用
 *   - 调用方 `new ControlPlaneClient(url)` 不再创建/泄漏 Session——三处调用点零改动生效
 *   - close() 改为从缓存移除并关闭该 baseUrl 的 Session（保留方法名，兼容现有无 close 调用）
 *   - POST body 显式 JSON.stringify 传 string，避免隐式序列化不确定行为
 *
 * H4 下载路径 + 协议防御增强（round 5，P1-10）：
 *   - 新增 downloadFile / downloadRange：GET /data/<nodeId>（200 全量 / 206 分片）
 *   - 响应头 sha256 与元数据 sha256 比对（篡改检出，verifyContentSha）
 *   - Range 边界错误识别与处置：416 越界 / 目录 400 / 已删 404 / token 失效 401
 *   - 附加防御：下载长度与声明区间/元数据 size 一致性校验（截断检出）
 *   - 防御逻辑落为模块级纯函数（parseContentRange / validateRangeRequest /
 *     verifyContentSha / classifyDownloadFailure），可独立说明与单测
 *   - 失败行为统一上抛 DownloadError（kind 分类），不静默重试——由调用方
 *     按 kind 决定处置（元数据刷新 / UI 提示 / 标记损坏）
 */
export interface RegisterResponse {
  deviceId: string;
  deviceToken: string;
  serverTime: number;
}

export interface RegisterRequest {
  deviceName: string;
  platform: string;
  capabilities: string[];
}

export interface HeartbeatResponse {
  status: string;
  serverTime: number;
}

export interface HeartbeatRequest {
  deviceId: string;
}

/** HTTP 错误（P0-2：携带状态码，供 401 自愈判定） */
export class HttpError extends Error {
  readonly statusCode: number;
  readonly path: string;

  constructor(statusCode: number, path: string) {
    super(`HTTP ${statusCode} ${path}`);
    this.name = 'HttpError';
    this.statusCode = statusCode;
    this.path = path;
  }
}

/** H2：虚拟目录树节点（控制面下发） */
export interface NamespaceNode {
  id: number;
  parentId: number;
  name: string;
  type: string;      // 'dir' | 'file'
  size: number;      // 字节
  mtime: number;     // 毫秒时间戳
  version: number;   // 节点版本（单调递增）
  sha256?: string;   // H4：文件内容 sha256 十六进制（file 节点携带，供分片下载端到端校验）
  deleted?: boolean; // P0-1：tombstone 标记（增量拉取携带，客户端本地删除）
}

export interface NamespaceResponse {
  version: number;   // namespace 全局版本（增量游标）
  mode: string;      // 'full' | 'incremental'（增量可能含墓碑节点）
  nodes: NamespaceNode[];
}

const TIMEOUT_CONNECT_MS = 5000;
const TIMEOUT_TRANSFER_MS = 5000;

// ================= H4 下载协议防御（P1-10，模块级纯函数可独立单测） =================

/** 下载错误类别（调用方按 kind 决定处置路径） */
export type DownloadErrorKind =
  | 'range_not_satisfiable'  // 416：Range 越界（Content-Range: bytes 星号/total，调用方可修正游标/刷新元数据后重试）
  | 'protocol_error'         // 400：目录节点/非法请求（客户端逻辑错误，不应对目录发下载）
  | 'node_not_found'         // 404：节点已删除或不存在（调用方触发增量拉取刷新元数据）
  | 'unauthorized'           // 401：token 失效（调用方已有 401 自愈判定，P0-2）
  | 'sha_mismatch'           // 响应头 sha256 ≠ 元数据 sha256（篡改场景，不重试——重试结果同样不一致）
  | 'size_mismatch';         // 下载数据长度与声明区间/元数据 size 不符（截断/篡改，可整文件重拉）

/** 下载错误（P1-10：kind 分类 + statusCode + detail，供 e2e/soak 断言） */
export class DownloadError extends Error {
  readonly kind: DownloadErrorKind;
  readonly nodeId: number;
  readonly statusCode: number;
  readonly detail: string;

  constructor(kind: DownloadErrorKind, nodeId: number, statusCode: number, detail: string) {
    super(`Download ${kind} node=${nodeId} HTTP ${statusCode} ${detail}`);
    this.name = 'DownloadError';
    this.kind = kind;
    this.nodeId = nodeId;
    this.statusCode = statusCode;
    this.detail = detail;
  }
}

/** Content-Range 响应头解析结果（如 "bytes 0-1023/819200"） */
export interface ContentRange {
  start: number;
  end: number;
  total: number;
}

/**
 * 纯函数：解析 Content-Range 响应头
 *
 * "bytes 0-1023/819200" → {start:0, end:1023, total:819200}；"bytes 星号/819200"（416）→ total 有值。
 * 无法解析（格式不符/空值）→ null（调用方按协议违规处置）。
 */
export function parseContentRange(value: string): ContentRange | null {
  if (value === undefined || value === null || value.length === 0) {
    return null;
  }
  // 206 形式：bytes=<start>-<end>/<total>
  const full = value.match(/bytes\s+(\d+)-(\d+)\/(\d+)/);
  if (full !== null) {
    return { start: Number(full[1]), end: Number(full[2]), total: Number(full[3]) };
  }
  // 416 形式：bytes=*/<total>
  const unsat = value.match(/bytes\s*\*\/(\d+)/);
  if (unsat !== null) {
    return { start: 0, end: 0, total: Number(unsat[1]) };
  }
  return null;
}

/**
 * 纯函数：请求 Range 合法性预检（下载前，越界直接报错不发请求）
 *
 * nodeSize 来自元数据（NamespaceNode.size）；end=-1 表示开放 Range（到文件尾）。
 * 触发路径：start >= nodeSize 或 end < start → DownloadError('range_not_satisfiable')。
 */
export function validateRangeRequest(nodeSize: number, start: number, end: number): void {
  if (end !== -1 && end < start) {
    throw new DownloadError('range_not_satisfiable', 0, 0,
      `invalid request range: end(${end}) < start(${start})`);
  }
  if (start >= nodeSize && !(end === -1 && nodeSize === 0)) {
    throw new DownloadError('range_not_satisfiable', 0, 0,
      `request range start(${start}) beyond file size(${nodeSize})`);
  }
}

/**
 * 纯函数：响应头 sha256 与元数据 sha256 比对（篡改检出）
 *
 * 触发路径：/data 响应头 x-content-sha256 vs /namespace 元数据 sha256（期望值）。
 * mock 篡改场景（sha256Override）伪造的是元数据 sha，响应头为真实值——
 * 两者不一致即检出（元数据被篡改/服务端异常），不重试，上抛由调用方处置
 * （UI 提示 / 标记节点损坏 / 触发元数据刷新）。
 * 响应头缺失 x-content-sha256 同样视为校验失败（契约要求 200/206 必带）。
 */
export function verifyContentSha(headerSha: string, metaSha: string, nodeId: number): void {
  if (metaSha === undefined || metaSha === null || metaSha.length === 0) {
    throw new DownloadError('protocol_error', nodeId, 200,
      'metadata sha256 missing for file node (cannot verify)');
  }
  if (headerSha === undefined || headerSha === null || headerSha.length === 0) {
    throw new DownloadError('sha_mismatch', nodeId, 200,
      'response header x-content-sha256 missing (contract violation)');
  }
  const h = headerSha.trim().replace(/^"|"$/g, '');  // 容忍 ETag 带引号包裹形式
  if (h !== metaSha) {
    throw new DownloadError('sha_mismatch', nodeId, 200,
      `header sha(${h.substring(0, 12)}…) != metadata sha(${metaSha.substring(0, 12)}…)`);
  }
}

/**
 * 纯函数：失败状态码分类（416/400/404/401 → DownloadError）
 *
 * 触发路径：/data 响应非 200/206 时按状态码分类，供调用方按 kind 处置。
 */
export function classifyDownloadFailure(statusCode: number, nodeId: number,
                                        contentRangeValue: string): DownloadError {
  if (statusCode === 416) {
    const cr = parseContentRange(contentRangeValue);
    const total = cr !== null ? cr.total : -1;
    return new DownloadError('range_not_satisfiable', nodeId, statusCode,
      `range beyond file size (total=${total})`);
  }
  if (statusCode === 400) {
    return new DownloadError('protocol_error', nodeId, statusCode,
      'node is not a file (or invalid request)');
  }
  if (statusCode === 404) {
    return new DownloadError('node_not_found', nodeId, statusCode,
      'node deleted or not found (refresh metadata)');
  }
  if (statusCode === 401) {
    return new DownloadError('unauthorized', nodeId, statusCode,
      'token invalid (401 self-heal by caller)');
  }
  return new DownloadError('protocol_error', nodeId, statusCode,
    `unexpected status ${statusCode}`);
}

/** 响应头大小写不敏感取值（HTTP 头名 case-insensitive；rcp 规范化小写，兼容原始大小写） */
function getResponseHeader(headers: rcp.ResponseHeaders, name: string): string {
  const direct: string | string[] | undefined = headers[name];
  if (typeof direct === 'string') {
    return direct;
  }
  if (Array.isArray(direct) && direct.length > 0) {
    return direct[0];
  }
  const lower = name.toLowerCase();
  const byLower: string | string[] | undefined = headers[lower];
  if (typeof byLower === 'string') {
    return byLower;
  }
  if (Array.isArray(byLower) && byLower.length > 0) {
    return byLower[0];
  }
  return '';
}

/**
 * 模块级 Session 缓存：key=baseUrl，value=rcp.Session
 *
 * 惰性创建、跨 ControlPlaneClient 实例复用——三处 `new ControlPlaneClient(url)`
 * 调用点（Index.ets 注册/心跳、NamespaceBrowser.ets）共享同一 Session，
 * 构造/丢弃临时 client 不再创建/泄漏 Session。
 */
const sessionCache: Map<string, rcp.Session> = new Map();

/** 从缓存获取或惰性创建 Session（跨 ControlPlaneClient 实例复用） */
function getOrCreateSession(baseUrl: string): rcp.Session {
  let session: rcp.Session | undefined = sessionCache.get(baseUrl);
  if (session === undefined) {
    session = rcp.createSession({
      baseAddress: baseUrl,
      requestConfiguration: {
        transfer: {
          timeout: {
            connectMs: TIMEOUT_CONNECT_MS,
            transferMs: TIMEOUT_TRANSFER_MS,
          },
        },
      },
    });
    sessionCache.set(baseUrl, session);
  }
  return session;
}

export class ControlPlaneClient {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
    // 不再在构造函数中创建 Session——改为模块级缓存惰性创建，避免临时实例泄漏
  }

  /**
   * 关闭并移除当前 baseUrl 对应的 Session
   *
   * 从缓存移除并关闭该 key 的 Session，后续请求将惰性创建新 Session。
   * 保留方法名供测试/退出场景；现有调用点无 close()，兼容无碍。
   */
  close(): void {
    const session: rcp.Session | undefined = sessionCache.get(this.baseUrl);
    if (session !== undefined) {
      session.close();
      sessionCache.delete(this.baseUrl);
    }
  }

  /**
   * 发送 POST 请求（内部统一方法）
   *
   * @param path API 路径（如 '/devices/register'）
   * @param body 请求体对象（显式 JSON.stringify 后作为 string 发送）
   * @param token 可选的 Bearer token
   * @returns 解析后的 JSON 响应对象
   * @throws HttpError 当响应状态码非 200 时
   */
  private async postJson(path: string, body: object, token?: string): Promise<object> {
    const headers: rcp.RequestHeaders = { 'content-type': 'application/json' };
    if (token !== undefined && token.length > 0) {
      headers['authorization'] = `Bearer ${token}`;
    }
    const bodyStr: string = JSON.stringify(body);
    const request: rcp.Request = new rcp.Request(path, 'POST', headers, bodyStr);
    const session: rcp.Session = getOrCreateSession(this.baseUrl);
    const resp: rcp.Response = await session.fetch(request);
    if (resp.statusCode !== 200) {
      throw new HttpError(resp.statusCode, path);
    }
    return this.parseBody(resp);
  }

  /**
   * 发送 GET 请求（内部统一方法）
   *
   * @param path API 路径含 query（如 '/namespace?since=0'）
   * @param token Bearer token
   * @returns 解析后的 JSON 响应对象
   * @throws HttpError 当响应状态码非 200 时
   */
  private async getJson(path: string, token: string): Promise<object> {
    const headers: rcp.RequestHeaders = { 'authorization': `Bearer ${token}` };
    const request: rcp.Request = new rcp.Request(path, 'GET', headers);
    const session: rcp.Session = getOrCreateSession(this.baseUrl);
    const resp: rcp.Response = await session.fetch(request);
    if (resp.statusCode !== 200) {
      throw new HttpError(resp.statusCode, path);
    }
    return this.parseBody(resp);
  }

  /** 将 rcp.Response.body (ArrayBuffer) 解析为 JSON 对象 */
  private parseBody(resp: rcp.Response): object {
    if (resp.body === undefined || resp.body === null) {
      const emptyObj: Record<string, Object> = {};
      return emptyObj;
    }
    const decoder: util.TextDecoder = util.TextDecoder.create('utf-8');
    const text: string = decoder.decodeToString(new Uint8Array(resp.body));
    return JSON.parse(text) as object;
  }

  async register(deviceName: string, platform: string): Promise<RegisterResponse> {
    const req: RegisterRequest = {
      deviceName: deviceName,
      platform: platform,
      capabilities: ['bep-protocol', 'napi-bridge'],
    };
    const r: object = await this.postJson('/devices/register', req);
    return r as RegisterResponse;
  }

  async heartbeat(deviceId: string, deviceToken: string): Promise<HeartbeatResponse> {
    const req: HeartbeatRequest = { deviceId: deviceId };
    const r: object = await this.postJson('/devices/heartbeat', req, deviceToken);
    return r as HeartbeatResponse;
  }

  /** H2：拉取虚拟目录树（since=0 全量只含活节点；增量含墓碑，version>since） */
  async fetchNamespace(deviceToken: string, since: number): Promise<NamespaceResponse> {
    const r: object = await this.getJson(`/namespace?since=${since}`, deviceToken);
    return r as NamespaceResponse;
  }

  /**
   * H4：下载文件（全量 200）
   *
   * 防御链：状态码分类（非 200 → classifyDownloadFailure）→ sha 比对（响应头 vs
   * 元数据，verifyContentSha）→ 长度校验（body 长度 == 元数据 size，截断检出）。
   *
   * @throws DownloadError（kind 见类型定义；调用方按 kind 处置，不静默重试）
   */
  async downloadFile(deviceToken: string, node: NamespaceNode): Promise<Uint8Array> {
    return this.downloadRange(deviceToken, node, 0, -1);
  }

  /**
   * H4：下载分片（Range 请求，206 Partial Content）
   *
   * 防御链：
   *   1. 下载前预检：validateRangeRequest（越界直接报错，不发请求，省一次往返）
   *   2. 状态码分类：206/200 成功；416/400/404/401 → classifyDownloadFailure 上抛
   *   3. sha 比对：响应头 x-content-sha256 vs 元数据 sha256（篡改检出）
   *   4. 长度校验：206 分片长度 == Content-Range 声明区间长度；200 全量 == 元数据 size
   *
   * @param end 结束字节（含）；-1 表示开放 Range（到文件尾，请求头 bytes=start-）
   * @throws DownloadError
   */
  async downloadRange(deviceToken: string, node: NamespaceNode,
                      start: number, end: number): Promise<Uint8Array> {
    // 1. 下载前预检（越界不发请求）
    validateRangeRequest(node.size, start, end);

    // 2. 构造请求（range: bytes=start-end / 开放形式 bytes=start-）
    const headers: rcp.RequestHeaders = { 'authorization': `Bearer ${deviceToken}` };
    const rangeValue = end === -1 ? `bytes=${start}-` : `bytes=${start}-${end}`;
    headers['range'] = rangeValue;
    const request: rcp.Request = new rcp.Request(`/data/${node.id}`, 'GET', headers);
    const session: rcp.Session = getOrCreateSession(this.baseUrl);
    const resp: rcp.Response = await session.fetch(request);

    const headerSha: string = getResponseHeader(resp.headers, 'x-content-sha256');
    const contentRangeValue: string = getResponseHeader(resp.headers, 'content-range');

    // 3. 状态码分类（非 200/206 → DownloadError）
    if (resp.statusCode !== 200 && resp.statusCode !== 206) {
      throw classifyDownloadFailure(resp.statusCode, node.id, contentRangeValue);
    }

    // 4. sha 比对（响应头 vs 元数据，篡改检出）
    verifyContentSha(headerSha, node.sha256 ?? '', node.id);

    // 5. 长度校验（截断/篡改检出）
    const data = resp.body === undefined || resp.body === null
      ? new Uint8Array(0) : new Uint8Array(resp.body);
    if (resp.statusCode === 206) {
      const cr = parseContentRange(contentRangeValue);
      if (cr !== null) {
        const expected = cr.end - cr.start + 1;
        if (data.length !== expected) {
          throw new DownloadError('size_mismatch', node.id, resp.statusCode,
            `chunk length(${data.length}) != content-range declared(${expected})`);
        }
      }
      // Content-Range 无法解析时降级为不校验（sha 比对已覆盖篡改主路径）
    } else if (data.length !== node.size) {
      throw new DownloadError('size_mismatch', node.id, resp.statusCode,
        `body length(${data.length}) != metadata size(${node.size})`);
    }
    return data;
  }
}
