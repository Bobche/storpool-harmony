import { http } from '@kit.NetworkKit';

/**
 * 控制面 HTTP 客户端（H1）
 *
 * 对接控制面 API（H1 阶段为 mock：mock-control-plane/server.py）：
 *   POST /devices/register   {deviceName, platform}          -> {deviceId, deviceToken, serverTime}
 *   POST /devices/heartbeat  {deviceId} (Bearer deviceToken) -> {status, serverTime}
 *
 * 鉴权：H1 用 Bearer token 简化；正式 mTLS 双向证书在控制面 Go 侧就绪后升级。
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

/** H2：虚拟目录树节点（控制面下发） */
export interface NamespaceNode {
  id: number;
  parentId: number;
  name: string;
  type: string;      // 'dir' | 'file'
  size: number;      // 字节
  mtime: number;     // 毫秒时间戳
  version: number;   // 节点版本（单调递增）
}

export interface NamespaceResponse {
  version: number;   // namespace 全局版本（增量游标）
  mode: string;      // 'full' | 'incremental'
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
        throw new Error(`HTTP ${resp.responseCode} ${path}`);
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

  /** H2：拉取虚拟目录树（since=0 全量；否则返回 version>since 的增量节点） */
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
        throw new Error(`HTTP ${resp.responseCode} /namespace`);
      }
      return resp.result as NamespaceResponse;
    } finally {
      client.destroy();
    }
  }
}