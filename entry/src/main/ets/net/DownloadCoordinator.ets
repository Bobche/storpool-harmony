import { ControlPlaneClient, DownloadError, NamespaceNode } from './ControlPlaneClient';

/**
 * 下载编排状态机（round 6，P2-3）——防御原语之上的可复用编排层
 *
 * 背景：round5 任务2 交付了 6 类 DownloadError 防御原语（parseContentRange /
 * validateRangeRequest / verifyContentSha / classifyDownloadFailure /
 * downloadFile / downloadRange），本文件把"防御原语 → 可复用编排层"做成纯逻辑，
 * 设备端 H4 下载链路接入时由调用方（ViewModel/UI 面，A 领地）接线，本层零 UI 依赖。
 *
 * 编排语义：
 *   - 串行队列（并发=1，对齐 H4 语义）；任务入队/取消/清空
 *   - 失败处置按 DownloadErrorKind 六类状态机驱动（对照 round5 防御表）：
 *     sha_mismatch → 不重试，标记损坏回调
 *     size_mismatch → 整文件重拉一次，仍败则回调
 *     range_not_satisfiable → 携 total 修正游标后续传重试一次
 *     node_not_found → 触发元数据刷新信号回调（不自拉元数据，信号外发）
 *     unauthorized → 上抛自愈信号（P0-2 链路归调用方/ViewModel，勿在此层私建）
 *     protocol_error → fail-fast 不吞
 *   - 状态变化回调透出（进度：queued → running → (queued 重试) → success/failed，
 *     供未来 publishProgress/实况窗联动）；重试总次数上限 2/任务（防无限循环）
 *
 * ArkTS 严格模式合规：对象字面量均带接口注解；throw 仅 Error 类型
 * （DownloadError extends Error）；无 any；非空处理用 undefined 检查。
 */

/** 下载任务状态（onStateChange 透出的进度序列） */
export type DownloadTaskState = 'queued' | 'running' | 'success' | 'failed' | 'cancelled';

/** 令牌提供器（401 自愈后 token 会变化，调用方动态返回当前值） */
export type TokenProvider = () => string;

/** 编排层回调（全部为信号外发；本层不自建 P0-2 链路、不自拉元数据） */
export interface DownloadCoordinatorCallbacks {
  /**
   * 状态变化（进度透出，供未来 publishProgress/实况窗联动）
   * @param note 状态说明（nodeId、字节数、重试序号、错误摘要）
   */
  onStateChange(taskId: number, state: DownloadTaskState, note: string): void;
  /** sha_mismatch：标记损坏回调（不重试——元数据被篡改时重试结果同样不一致） */
  onCorrupted(taskId: number, nodeId: number, detail: string): void;
  /** node_not_found：元数据刷新信号（不自拉元数据，信号外发；调用方可修复后重新入队） */
  onMetadataRefreshNeeded(taskId: number, nodeId: number): void;
  /** unauthorized：401 自愈信号（P0-2 链路归调用方/ViewModel；自愈后可重新入队） */
  onAuthHealNeeded(taskId: number, nodeId: number, statusCode: number): void;
}

/** 下载任务（队列元素） */
export interface DownloadTask {
  id: number;
  node: NamespaceNode;
  start: number;         // 起始字节（-1 无意义，end=-1 表示开放区间）
  end: number;           // 结束字节（含）；-1 = 到文件尾
  retryCount: number;    // 重试总计数（上限 MAX_RETRIES_PER_TASK）
  retriedFull: boolean;  // size_mismatch 已整文件重拉过标志（每任务一次）
  state: DownloadTaskState;
}

/**
 * 纯函数：从 DownloadError.detail 解析文件总大小（416 修正游标用）
 *
 * 兼容两种来源：服务端 416（"range beyond file size (total=N)"）与
 * 本地预检（"request range start(S) beyond file size(M)"）。
 * 解析不到返回 -1（调用方放弃重试，直接 failed）。
 */
export function extractTotalFromDetail(detail: string): number {
  const m1 = detail.match(/total=(\d+)/);
  if (m1 !== null) {
    return Number(m1[1]);
  }
  const m2 = detail.match(/file size\((\d+)\)/);
  if (m2 !== null) {
    return Number(m2[1]);
  }
  return -1;
}

export class DownloadCoordinator {
  /** 每任务重试总次数上限（size 重拉 + range 修正合计，防无限循环） */
  private static readonly MAX_RETRIES_PER_TASK = 2;

  private client: ControlPlaneClient;
  private tokenProvider: TokenProvider;
  private callbacks: DownloadCoordinatorCallbacks;
  private queue: DownloadTask[] = [];
  private draining: boolean = false;  // 串行泵在跑标志（并发=1）
  private epoch: number = 0;          // clear() 时加一，使正在 await 的泵停止取新任务
  private nextTaskId: number = 1;

  constructor(client: ControlPlaneClient, tokenProvider: TokenProvider,
              callbacks: DownloadCoordinatorCallbacks) {
    this.client = client;
    this.tokenProvider = tokenProvider;
    this.callbacks = callbacks;
  }

  /**
   * 任务入队（默认全量下载；rangeStart/rangeEnd 可选指定分片）
   * @returns taskId（后续取消/回调关联用）
   */
  enqueue(node: NamespaceNode, rangeStart: number = 0, rangeEnd: number = -1): number {
    const task: DownloadTask = {
      id: this.nextTaskId,
      node: node,
      start: rangeStart,
      end: rangeEnd,
      retryCount: 0,
      retriedFull: false,
      state: 'queued',
    };
    this.nextTaskId += 1;
    this.queue.push(task);
    this.callbacks.onStateChange(task.id, 'queued',
      `node=${node.id} range=${rangeStart}-${rangeEnd}`);
    this.pump();
    return task.id;
  }

  /** 取消 queued 状态的任务；running/已完成任务不可取消，返回 false */
  cancel(taskId: number): boolean {
    for (let i = 0; i < this.queue.length; i++) {
      const t: DownloadTask | undefined = this.queue[i];
      if (t !== undefined && t.id === taskId && t.state === 'queued') {
        this.queue.splice(i, 1);
        t.state = 'cancelled';
        this.callbacks.onStateChange(t.id, 'cancelled', 'cancelled while queued');
        return true;
      }
    }
    return false;
  }

  /**
   * 清空队列：所有 queued 任务转 cancelled 并回调；正在执行的任务跑完当前
   * 单步后，泵因 epoch 变化不再取新任务（已下发请求无法中途撤回）。
   */
  clear(): void {
    this.epoch += 1;
    while (this.queue.length > 0) {
      const t: DownloadTask | undefined = this.queue.shift();
      if (t === undefined) {
        break;
      }
      t.state = 'cancelled';
      this.callbacks.onStateChange(t.id, 'cancelled', 'cleared');
    }
  }

  /** 队列中未处理任务数 */
  pendingCount(): number {
    return this.queue.length;
  }

  /** 启动串行泵（并发=1；已在跑则无操作，避免并发泵） */
  private pump(): void {
    if (this.draining) {
      return;
    }
    this.draining = true;
    this.drainLoop();
  }

  /** 泵循环：逐任务执行直到队列空或 clear()（epoch 变化） */
  private async drainLoop(): Promise<void> {
    const myEpoch = this.epoch;
    try {
      while (this.queue.length > 0) {
        if (this.epoch !== myEpoch) {
          return;  // 期间 clear() 过，停止取新任务
        }
        const task: DownloadTask | undefined = this.queue.shift();
        if (task === undefined) {
          return;
        }
        await this.runTask(task);
      }
    } finally {
      this.draining = false;
    }
  }

  /** 单任务执行：调 downloadRange（内含防御链），失败进六类状态机 */
  private async runTask(task: DownloadTask): Promise<void> {
    task.state = 'running';
    this.callbacks.onStateChange(task.id, 'running',
      `node=${task.node.id} range=${task.start}-${task.end} attempt=${task.retryCount}`);
    try {
      const token: string = this.tokenProvider();
      const data: Uint8Array = await this.client.downloadRange(token, task.node, task.start, task.end);
      task.state = 'success';
      this.callbacks.onStateChange(task.id, 'success', `node=${task.node.id} ${data.length}B`);
      return;
    } catch (e) {
      // 非防御原语异常（网络层等）归一为 protocol_error，fail-fast 不吞
      const err = e instanceof DownloadError
        ? e
        : new DownloadError('protocol_error', task.node.id, 0,
            e instanceof Error ? e.message : 'unknown error');
      this.handleFailure(task, err);
    }
  }

  /** 失败处置状态机（六类，对照 round5 防御表） */
  private handleFailure(task: DownloadTask, err: DownloadError): void {
    if (err.kind === 'sha_mismatch') {
      // 不重试，标记损坏回调
      task.state = 'failed';
      this.callbacks.onStateChange(task.id, 'failed', `sha_mismatch: ${err.detail}`);
      this.callbacks.onCorrupted(task.id, err.nodeId, err.detail);
      return;
    }

    if (err.kind === 'size_mismatch') {
      // 整文件重拉一次（预算内且未整拉过），仍败则回调
      if (!task.retriedFull && task.retryCount < DownloadCoordinator.MAX_RETRIES_PER_TASK) {
        task.retriedFull = true;
        task.retryCount += 1;
        task.start = 0;
        task.end = -1;
        task.state = 'queued';
        this.callbacks.onStateChange(task.id, 'queued',
          `size_mismatch -> full re-pull (attempt ${task.retryCount}/${DownloadCoordinator.MAX_RETRIES_PER_TASK})`);
        this.queue.push(task);
        return;
      }
      task.state = 'failed';
      this.callbacks.onStateChange(task.id, 'failed', `size_mismatch: ${err.detail}`);
      return;
    }

    if (err.kind === 'range_not_satisfiable') {
      // 携 total 修正游标后续传重试一次（start 修正到文件尾）
      const total = extractTotalFromDetail(err.detail);
      if (total >= 0 && task.retryCount < DownloadCoordinator.MAX_RETRIES_PER_TASK) {
        task.retryCount += 1;
        task.start = total;
        task.end = -1;
        task.state = 'queued';
        this.callbacks.onStateChange(task.id, 'queued',
          `range_not_satisfiable -> resume from ${total} (attempt ${task.retryCount}/${DownloadCoordinator.MAX_RETRIES_PER_TASK})`);
        this.queue.push(task);
        return;
      }
      task.state = 'failed';
      this.callbacks.onStateChange(task.id, 'failed', `range_not_satisfiable: ${err.detail}`);
      return;
    }

    if (err.kind === 'node_not_found') {
      // 触发元数据刷新信号回调（不自拉元数据，信号外发）
      task.state = 'failed';
      this.callbacks.onStateChange(task.id, 'failed', `node_not_found: ${err.detail}`);
      this.callbacks.onMetadataRefreshNeeded(task.id, err.nodeId);
      return;
    }

    if (err.kind === 'unauthorized') {
      // 上抛自愈信号（P0-2 链路归调用方/ViewModel，勿在此层私建）；自愈后可重新入队
      task.state = 'failed';
      this.callbacks.onStateChange(task.id, 'failed', `unauthorized: ${err.detail}`);
      this.callbacks.onAuthHealNeeded(task.id, err.nodeId, err.statusCode);
      return;
    }

    // protocol_error：fail-fast 不吞（客户端逻辑错误/网络层异常）
    task.state = 'failed';
    this.callbacks.onStateChange(task.id, 'failed', `protocol_error: ${err.detail}`);
  }
}