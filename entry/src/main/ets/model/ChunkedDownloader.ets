import { http } from '@kit.NetworkKit';
import { fileIo } from '@kit.CoreFileKit';
import { sha256Hex, blockRanges, BlockRange } from './StorPoolNapi';
import { NamespaceNode, HttpError } from '../net/ControlPlaneClient';

/**
 * H4 分片下载器
 *
 * 流程：blockRanges 规划（BEP 语义 128KiB）→ 逐片 Range GET（206）→ 内存重组
 *       → 全文件 sha256 端到端校验（Rust async work 工作线程计算）→ fs 落盘
 *
 * 协议（mock-control-plane /data 端点）：
 *   GET /data/<nodeId> + Bearer token + Range: bytes=<start>-<end> → 206 Partial Content
 *   响应头：Content-Range / X-Content-Sha256（全文件 sha，元数据缺失时兜底）/ ETag
 *
 * 校验两层：
 *   块级——响应字节长度 == 分片规划长度（防截断/错位）
 *   全文件——本地 sha256Hex == 元数据 node.sha256（或 X-Content-Sha256）
 *
 * 进度：每片/每阶段回调一次；由调用方驱动 UI 与 keeper.publishProgress（P0-3 真实进度）
 */

/** 单片响应（contentSha = 响应头 X-Content-Sha256，全文件 sha） */
interface RangeChunk {
  data: ArrayBuffer;
  contentSha: string;
}

export type DownloadStage = 'planning' | 'downloading' | 'verifying' | 'writing' | 'done' | 'error';

export interface DownloadProgress {
  fileName: string;
  stage: DownloadStage;
  receivedBytes: number;
  totalBytes: number;
  percent: number;   // 0-100（按已收字节占比）
  detail: string;
}

export interface DownloadResult {
  filePath: string;       // 落盘绝对路径
  receivedBytes: number;
  totalBytes: number;
  sha256Actual: string;   // 本地计算
  sha256Expected: string; // 服务端期望（元数据或响应头；空=无期望值，跳过校验）
  verified: boolean;
  durationMs: number;
}

const DEFAULT_BLOCK_SIZE = 128 * 1024;  // BEP 语义 128KiB 分片
const CONNECT_TIMEOUT_MS = 10000;
const READ_TIMEOUT_MS = 30000;          // 单片（128KiB）读超时，大块数据放宽

export class ChunkedDownloader {
  private baseUrl: string;
  private token: string;
  private cancelled: boolean = false;

  constructor(baseUrl: string, token: string) {
    this.baseUrl = baseUrl;
    this.token = token;
  }

  /** 取消进行中的下载（下一片开始前生效，抛 Error） */
  cancel(): void {
    this.cancelled = true;
  }

  private emit(onProgress: ((p: DownloadProgress) => void) | undefined,
    fileName: string, stage: DownloadStage,
    received: number, total: number, detail: string): void {
    if (onProgress === undefined) {
      return;
    }
    const p: DownloadProgress = {
      fileName: fileName,
      stage: stage,
      receivedBytes: received,
      totalBytes: total,
      percent: total > 0 ? Math.floor((received / total) * 100) : 100,
      detail: detail,
    };
    onProgress(p);
  }

  /** 响应头取值（兼容大小写差异：服务器/代理可能改写键名大小写） */
  private static headerValue(header: Record<string, string>, name: string): string {
    const lower: string = name.toLowerCase();
    const keys: string[] = Object.keys(header);
    for (let i = 0; i < keys.length; i++) {
      if (keys[i].toLowerCase() === lower) {
        return header[keys[i]];
      }
    }
    return '';
  }

  /** 单片 Range 请求：206 → {data, X-Content-Sha256}；非 206 抛 HttpError（401 供上层自愈） */
  private async fetchRange(nodeId: number, r: BlockRange): Promise<RangeChunk> {
    const client: http.HttpRequest = http.createHttp();
    try {
      const header: Record<string, string> = {
        'Authorization': `Bearer ${this.token}`,
        'Range': `bytes=${r.offset}-${r.offset + r.length - 1}`,
      };
      const resp: http.HttpResponse = await client.request(`${this.baseUrl}/data/${nodeId}`, {
        method: http.RequestMethod.GET,
        header: header,
        expectDataType: http.HttpDataType.ARRAY_BUFFER,
        connectTimeout: CONNECT_TIMEOUT_MS,
        readTimeout: READ_TIMEOUT_MS,
      });
      if (resp.responseCode !== 206) {
        throw new HttpError(resp.responseCode, `/data/${nodeId}`);
      }
      const rc: RangeChunk = {
        data: resp.result as ArrayBuffer,
        contentSha: ChunkedDownloader.headerValue(
          resp.header as Record<string, string>, 'X-Content-Sha256'),
      };
      return rc;
    } finally {
      client.destroy();
    }
  }

  /**
   * 分片下载 node（type='file'）到 destDir/<清洗后文件名>，返回结果（含 sha256 三方比对）。
   * @throws Error  参数非法 / 已取消 / 分片长度不符 / sha256 不匹配 / 落盘失败
   * @throws HttpError  401（身份失效→上层自愈）/ 404（墓碑）/ 400（目录节点）/ 416（Range 越界）
   */
  async download(node: NamespaceNode, destDir: string,
    onProgress?: (p: DownloadProgress) => void): Promise<DownloadResult> {
    const startAt: number = Date.now();
    this.cancelled = false;
    if (node.type !== 'file') {
      throw new Error(`节点 ${node.id} 非文件（type=${node.type}），拒绝下载`);
    }

    // ---- 阶段一：分片规划（Rust blockRanges，BEP 128KiB 语义）----
    this.emit(onProgress, node.name, 'planning', 0, node.size,
      `规划分片：${node.size}B ÷ ${DEFAULT_BLOCK_SIZE / 1024}KiB`);
    const ranges: BlockRange[] = blockRanges(node.size, DEFAULT_BLOCK_SIZE);
    const total: Uint8Array = new Uint8Array(node.size);
    let shaExpected: string = node.sha256 ?? '';
    let received: number = 0;

    // ---- 阶段二：逐片下载（顺序 Range GET → 206 → 内存重组）----
    for (let i = 0; i < ranges.length; i++) {
      if (this.cancelled) {
        throw new Error(`下载已取消（已收 ${received}/${node.size}B）`);
      }
      const r: BlockRange = ranges[i];
      const rc: RangeChunk = await this.fetchRange(node.id, r);
      if (rc.data.byteLength !== r.length) {
        throw new Error(`分片 #${r.index} 长度不符：期望 ${r.length}B，实得 ${rc.data.byteLength}B`);
      }
      if (shaExpected.length === 0 && rc.contentSha.length > 0) {
        shaExpected = rc.contentSha; // 元数据缺 sha 时取响应头兜底
      }
      total.set(new Uint8Array(rc.data), r.offset);
      received += r.length;
      this.emit(onProgress, node.name, 'downloading', received, node.size,
        `分片 ${i + 1}/${ranges.length}（${r.length}B）`);
    }

    // ---- 阶段三：全文件 sha256 端到端校验（Rust async work，不阻塞 UI 线程）----
    this.emit(onProgress, node.name, 'verifying', received, node.size, 'sha256 全文件校验中');
    const shaActual: string = await sha256Hex(total.buffer);
    const verified: boolean = shaExpected.length > 0 && shaActual === shaExpected;
    if (shaExpected.length > 0 && !verified) {
      this.emit(onProgress, node.name, 'error', received, node.size, 'sha256 校验失败');
      throw new Error(`sha256 不匹配：期望 ${shaExpected}，实得 ${shaActual}`);
    }

    // ---- 阶段四：落盘（destDir 不存在则建；文件名清洗防路径穿越）----
    this.emit(onProgress, node.name, 'writing', received, node.size, '写入本地文件');
    if (!fileIo.accessSync(destDir)) {
      fileIo.mkdirSync(destDir, true);
    }
    const safeName: string = node.name.replace(/[/\\:*?"<>|]/g, '_');
    const filePath: string = `${destDir}/${safeName}`;
    const file = fileIo.openSync(filePath,
      fileIo.OpenMode.READ_WRITE | fileIo.OpenMode.CREATE | fileIo.OpenMode.TRUNC);
    try {
      fileIo.writeSync(file.fd, total.buffer);
    } finally {
      fileIo.closeSync(file);
    }

    // ---- 完成 ----
    const result: DownloadResult = {
      filePath: filePath,
      receivedBytes: received,
      totalBytes: node.size,
      sha256Actual: shaActual,
      sha256Expected: shaExpected,
      verified: verified,
      durationMs: Date.now() - startAt,
    };
    this.emit(onProgress, node.name, 'done', received, node.size,
      `完成：${received}B，校验${verified ? '通过' : '跳过（无期望值）'}，用时 ${result.durationMs}ms`);
    return result;
  }
}