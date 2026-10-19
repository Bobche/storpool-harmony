import { backgroundTaskManager } from '@kit.BackgroundTasksKit';
import { wantAgent, WantAgent } from '@kit.AbilityKit';
import { notificationManager } from '@kit.NotificationKit';
import { connection } from '@kit.NetworkKit';
import { common } from '@kit.AbilityKit';
import { BusinessError } from '@kit.BasicServicesKit';

/** P1-7：保活操作结构化结果（替代原 "OK:<id>"/"FAIL:<code>" 字符串协议） */
export interface KeepAliveResult {
  ok: boolean;
  notificationId: number; // 成功时的系统通知 id；失败/未启动为 -1
  errorCode: number;      // 失败时的 BusinessError code；成功为 0
  errorMessage: string;   // 失败原因描述；成功为空
}

/** H3 后台长时任务保活：dataTransfer 长时任务 + 实况窗进度通知 + 网络状态监听 */
export class BackgroundKeeper {
  private notificationId: number = -1;
  private running: boolean = false;
  private netCon: connection.NetConnection | null = null;
  // P0-3：进度上报节流（500ms 内不重复发布；100% 终态始终放行）
  private lastPublishAt: number = 0;

  isRunning(): boolean {
    return this.running;
  }

  getNotificationId(): number {
    return this.notificationId;
  }

  /** 申请 dataTransfer 长时任务 */
  async startKeepAlive(context: common.UIAbilityContext, onNetChange: (online: boolean) => void): Promise<KeepAliveResult> {
    if (this.running) {
      const dup: KeepAliveResult = { ok: true, notificationId: this.notificationId, errorCode: 0, errorMessage: '' };
      return dup;
    }
    try {
      const info: wantAgent.WantAgentInfo = {
        wants: [{
          bundleName: 'com.storpool.harmony',
          abilityName: 'EntryAbility'
        }],
        actionType: wantAgent.OperationType.START_ABILITY,
        requestCode: 0,
        wantAgentFlags: [wantAgent.WantAgentFlags.UPDATE_PRESENT_FLAG]
      };
      const agent: WantAgent = await wantAgent.getWantAgent(info);
      const res: backgroundTaskManager.ContinuousTaskNotification =
        await backgroundTaskManager.startBackgroundRunning(context, ['dataTransfer'], agent);
      this.notificationId = res.notificationId;
      this.running = true;
      this.startNetWatch(onNetChange);
      const ok: KeepAliveResult = { ok: true, notificationId: this.notificationId, errorCode: 0, errorMessage: '' };
      return ok;
    } catch (e) {
      const err = e as BusinessError;
      const fail: KeepAliveResult = {
        ok: false, notificationId: -1, errorCode: err.code ?? -1, errorMessage: err.message ?? ''
      };
      return fail;
    }
  }

  /** 停止长时任务 */
  async stopKeepAlive(context: common.UIAbilityContext): Promise<KeepAliveResult> {
    if (!this.running) {
      const idle: KeepAliveResult = { ok: true, notificationId: -1, errorCode: 0, errorMessage: '' };
      return idle;
    }
    try {
      await backgroundTaskManager.stopBackgroundRunning(context);
      this.running = false;
      this.notificationId = -1;
      this.stopNetWatch();
      const ok: KeepAliveResult = { ok: true, notificationId: -1, errorCode: 0, errorMessage: '' };
      return ok;
    } catch (e) {
      const err = e as BusinessError;
      const fail: KeepAliveResult = {
        ok: false, notificationId: this.notificationId, errorCode: err.code ?? -1, errorMessage: err.message ?? ''
      };
      return fail;
    }
  }

  /** 更新实况窗进度通知（dataTransfer 要求 10 分钟内有进度更新，否则任务被系统取消）
   *  P0-3 合规语义：progressValue 只允许真实进度——下载中传真实百分比，
   *  无传输任务的心跳保活传 0（不伪造百分比）；500ms 节流防通知轰炸
   */
  async publishProgress(title: string, fileName: string, progressValue: number): Promise<void> {
    if (!this.running || this.notificationId < 0) {
      return;
    }
    const now = Date.now();
    const isFinal = progressValue >= 100;
    if (!isFinal && now - this.lastPublishAt < 500) {
      return; // 节流窗口内丢弃（终态 100% 始终放行）
    }
    this.lastPublishAt = now;
    try {
      const template: notificationManager.NotificationTemplate = {
        name: 'downloadTemplate',
        data: {
          title: title,
          fileName: fileName,
          progressValue: progressValue
        }
      };
      const liveView: notificationManager.NotificationSystemLiveViewContent = {
        typeCode: 8,
        title: title,
        text: fileName
      };
      const content: notificationManager.NotificationContent = {
        notificationContentType: notificationManager.ContentType.NOTIFICATION_CONTENT_SYSTEM_LIVE_VIEW,
        systemLiveView: liveView
      };
      const request: notificationManager.NotificationRequest = {
        id: this.notificationId,
        content: content,
        notificationSlotType: notificationManager.SlotType.LIVE_VIEW,
        template: template
      };
      await notificationManager.publish(request);
    } catch (e) {
      // 通知发布失败不影响保活任务本身
    }
  }

  private startNetWatch(onNetChange: (online: boolean) => void): void {
    if (this.netCon !== null) {
      return;
    }
    try {
      this.netCon = connection.createNetConnection();
      this.netCon.on('netAvailable', () => {
        onNetChange(true);
      });
      this.netCon.on('netLost', () => {
        onNetChange(false);
      });
      this.netCon.register((err) => {
        if (err) {
          console.error('H3 net register failed:', JSON.stringify(err));
        }
      });
    } catch (e) {
      console.error('H3 net create failed:', JSON.stringify(e));
    }
  }

  private stopNetWatch(): void {
    if (this.netCon === null) {
      return;
    }
    try {
      this.netCon.unregister(() => {
        // 忽略
      });
    } catch (e) {
      // 忽略
    }
    this.netCon = null;
  }
}