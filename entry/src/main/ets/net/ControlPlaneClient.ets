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

export class ControlPlaneClient {
  private session: rcp.Session;

  constructor(baseUrl: string) {
    this.session = rcp.createSession({
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
  }

  /** 关闭 Session，释放连接资源 */
  close(): void {
    this.session.close();
  }

  /**
   * 发送 POST 请求（内部统一方法）
   *
   * @param path API 路径（如 '/devices/register'）
   * @param body 请求体对象（将作为 JSON 发送）
   * @param token 可选的 Bearer token
   * @returns 解析后的 JSON 响应对象
   * @throws HttpError 当响应状态码非 200 时
   */
  private async postJson(path: string, body: object, token?: string): Promise<object> {
    const headers: rcp.RequestHeaders = { 'content-type': 'application/json' };
    if (token !== undefined && token.length > 0) {
      headers['authorization'] = `Bearer ${token}`;
    }
    const request: rcp.Request = new rcp.Request(path, 'POST', headers, body);
    const resp: rcp.Response = await this.session.fetch(request);
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
    const resp: rcp.Response = await this.session.fetch(request);
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
}