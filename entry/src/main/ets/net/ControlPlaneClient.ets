import { http } from '@kit.NetworkKit';

/**
 * 控制面 HTTP 客户端（H1/H2）
 *
 * 对接控制面 API（H1 阶段为 mock：mock-control-plane/server.py）：
 *   POST /devices/register   {deviceName, platform}          -> {deviceId, deviceToken, serverTime}
 *   POST /devices/heartbeat  {deviceId} (Bearer deviceToken) -> {status, serverTime}
 *   GET  /namespace?since=N  (Bearer deviceToken)            -> {version, mode, nodes}
 *
 * 鉴权：H1 用 Bearer token 简化；正式 mTLS 双向证书在控制面 Go 侧就绪后升级。
 * 错误：非 200 抛 HttpError（携带 statusCode，供调用方判定 401 自愈，P0-2）。
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

const TIMEOUT_MS = 5000;

export class ControlPlaneClient {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
  }

  private async postJson(path: string, body: object, token?: string): Promise<object> {
    const client: http.HttpRequest = http.createHttp();
    try {
      const header: Record<string, string> = { 'Content-Type': 'application/json' };
      if (token !== undefined && token.length > 0) {
        header['Authorization'] = `Bearer ${token}`;
      }
      const resp: http.HttpResponse = await client.request(`${this.baseUrl}${path}`, {
        method: http.RequestMethod.POST,
        header: header,
        extraData: JSON.stringify(body),
        expectDataType: http.HttpDataType.OBJECT,
        connectTimeout: TIMEOUT_MS,
        readTimeout: TIMEOUT_MS,
      });
      if (resp.responseCode !== 200) {
        throw new HttpError(resp.responseCode, path);
      }
      return resp.result as object;
    } finally {
      client.destroy();
    }
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
    const client: http.HttpRequest = http.createHttp();
    try {
      const header: Record<string, string> = { 'Authorization': `Bearer ${deviceToken}` };
      const resp: http.HttpResponse = await client.request(
        `${this.baseUrl}/namespace?since=${since}`, {
        method: http.RequestMethod.GET,
        header: header,
        expectDataType: http.HttpDataType.OBJECT,
        connectTimeout: TIMEOUT_MS,
        readTimeout: TIMEOUT_MS,
      });
      if (resp.responseCode !== 200) {
        throw new HttpError(resp.responseCode, '/namespace');
      }
      return resp.result as NamespaceResponse;
    } finally {
      client.destroy();
    }
  }
}