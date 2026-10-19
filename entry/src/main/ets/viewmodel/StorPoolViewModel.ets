import { ControlPlaneClient, RegisterResponse, HeartbeatResponse, HttpError } from '../net/ControlPlaneClient';
import { DeviceIdentity } from '../model/DeviceIdentity';
import { KeepAliveResult } from '../model/BackgroundKeeper';
import keeper from '../model/backgroundKeeperInstance';
import { fmtTime } from '../common/Format';
import { common } from '@kit.AbilityKit';

/**
 * P1-7：主页 ViewModel——H1（设备注册/心跳/401 自愈）与 H3（保活）域的
 * 单一状态源（@Observed，供 section 组件 @ObjectLink 观察）。
 * 逻辑从 pages/Index.ets 逐行迁移，行为零变更；页面组件不再直接
 * new ControlPlaneClient / 操作 timer——调用形态与 07c71a8（rcp Session）
 * 完全一致，不受 B 侧 net/ 面内部重构影响。
 */

const HEARTBEAT_INTERVAL_MS = 15000;
const SELF_HEAL_COOLDOWN_MS = 30000;

@Observed
export class StorPoolViewModel {
  // ---- H1：设备注册与心跳 ----
  ctrlUrl: string = 'http://10.0.2.2:8765';
  regStatus: string = '未注册';
  regOk: boolean = false;
  deviceName: string = '';
  deviceId: string = '';
  deviceToken: string = '';
  hbCount: number = 0;
  hbLastTime: string = '';
  hbRunning: boolean = false;
  hbStatus: string = '';

  // ---- H3：后台保活 ----
  kaStatus: string = '未申请';
  kaRunning: boolean = false;
  netStatus: string = '未知';

  private identity: DeviceIdentity = new DeviceIdentity();
  private hbTimer: number = -1;
  // P0-2：401 自愈冷却（重注册成功前连续 401 时防重注册风暴）
  private lastSelfHealAt: number = 0;
  // 页面生命周期内注入的 UIAbility Context（注册/自愈路径复用）
  private ctx: common.UIAbilityContext | null = null;

  bindContext(ctx: common.UIAbilityContext): void {
    this.ctx = ctx;
  }

  // ================= H1：注册 + 心跳 =================

  async ensureRegistration(ctx: common.UIAbilityContext): Promise<void> {
    try {
      this.regStatus = '初始化…';
      await this.identity.load(ctx);

      if (this.identity.isRegistered()) {
        this.deviceId = this.identity.getDeviceId();
        this.deviceToken = this.identity.getDeviceToken();
        this.deviceName = this.identity.getDeviceName();
        this.regOk = true;
        this.regStatus = '已注册（本地身份复用）';
        this.startHeartbeat();
        return;
      }

      this.regStatus = '注册中…';
      // P0-2：自愈重注册时复用持久化 deviceName（clear() 有意保留），首次注册才生成新名
      let name: string = this.identity.getDeviceName();
      if (name.length === 0) {
        name = DeviceIdentity.generateDeviceName();
      }
      const client = new ControlPlaneClient(this.ctrlUrl);
      const resp: RegisterResponse = await client.register(name, 'harmonyos');
      await this.identity.save(resp.deviceId, resp.deviceToken, name);
      this.deviceId = resp.deviceId;
      this.deviceToken = resp.deviceToken;
      this.deviceName = name;
      this.regOk = true;
      this.regStatus = `注册成功（serverTime=${resp.serverTime}）`;
      this.startHeartbeat();
    } catch (e) {
      this.regOk = false;
      this.regStatus = `注册失败: ${e}`;
    }
  }

  startHeartbeat(): void {
    this.stopHeartbeat();
    if (!this.identity.isRegistered()) {
      return;
    }
    this.hbRunning = true;
    this.hbStatus = '心跳已启动';
    this.hbTimer = setInterval(() => {
      this.doHeartbeat();
    }, HEARTBEAT_INTERVAL_MS);
    this.doHeartbeat(); // 立即执行一次
  }

  stopHeartbeat(): void {
    if (this.hbTimer >= 0) {
      clearInterval(this.hbTimer);
      this.hbTimer = -1;
    }
    this.hbRunning = false;
  }

  async doHeartbeat(): Promise<void> {
    const deviceId = this.identity.getDeviceId();
    const token = this.identity.getDeviceToken();
    if (deviceId.length === 0 || token.length === 0) {
      return;
    }
    try {
      const client = new ControlPlaneClient(this.ctrlUrl);
      const resp: HeartbeatResponse = await client.heartbeat(deviceId, token);
      if (resp.status === 'ok') {
        this.hbCount += 1;
        this.hbLastTime = fmtTime(Date.now());
        this.hbStatus = '在线';
        // H3：保活运行中时，每次心跳联动更新实况窗进度（防 dataTransfer 10 分钟无进度被取消）
        // P0-3：无传输任务时进度合规传 0（实况窗进度不得伪造，真实进度由 ChunkedDownloader 上报）
        if (keeper.isRunning()) {
          keeper.publishProgress(`StorPool 在线（心跳 ${this.hbCount} 次，无传输任务）`, 'heartbeat', 0);
        }
      } else {
        this.hbStatus = `心跳异常: status=${resp.status}`;
      }
    } catch (e) {
      if (e instanceof HttpError && e.statusCode === 401) {
        // P0-2：token/身份失效自愈（mock 重启内存丢失、服务端重置均会 401）
        this.selfHeal('heartbeat');
      } else {
        this.hbStatus = `心跳失败: ${e}`;
      }
    }
  }

  /**
   * P0-2：401 自愈——清身份 → 重新注册 → 恢复心跳。
   * 30s 冷却：重注册成功前若连续 401，只写状态不再触发，避免重注册风暴。
   */
  async selfHeal(source: string): Promise<void> {
    const now = Date.now();
    if (now - this.lastSelfHealAt < SELF_HEAL_COOLDOWN_MS) {
      this.hbStatus = `身份失效（401，${source}），自愈冷却中（30s）`;
      return;
    }
    this.lastSelfHealAt = now;
    this.hbStatus = '身份失效（401）→ 清身份 → 自动重注册…';
    try {
      await this.identity.clear();
    } catch (err) {
      // 未加载/清除失败不阻塞重注册路径
    }
    this.reRegister();
  }

  reRegister(): void {
    this.stopHeartbeat();
    this.hbCount = 0;
    this.hbLastTime = '';
    this.hbStatus = '';
    this.regOk = false;
    this.regStatus = '重新注册中…';
    // 重新注册：复用已有 deviceName（若存在），否则生成新的（逻辑在 ensureRegistration 内）
    if (this.ctx !== null) {
      this.ensureRegistration(this.ctx);
    }
  }

  // ================= H3：后台保活 =================

  async doStartKeepAlive(): Promise<void> {
    if (this.ctx === null) {
      this.kaStatus = '获取 Context 失败';
      return;
    }
    this.kaStatus = '申请中…';
    const result: KeepAliveResult = await keeper.startKeepAlive(
      this.ctx,
      (online: boolean) => {
        this.netStatus = online ? '在线（netAvailable）' : '离线（netLost）';
      });
    if (result.ok) {
      this.kaRunning = true;
      this.kaStatus = `已保活 nid=${result.notificationId}`;
      // P0-3：初始无传输任务，进度合规传 0（真实进度由 ChunkedDownloader 上报）
      keeper.publishProgress('StorPool 在线（无传输任务）', 'heartbeat', 0);
    } else {
      this.kaRunning = false;
      this.kaStatus = `申请失败 ${result.errorCode} ${result.errorMessage}`;
    }
  }

  async doStopKeepAlive(): Promise<void> {
    if (this.ctx === null) {
      return;
    }
    const result: KeepAliveResult = await keeper.stopKeepAlive(this.ctx);
    this.kaRunning = false;
    this.kaStatus = result.ok ? '已停止' : `停止失败 ${result.errorCode} ${result.errorMessage}`;
  }

  /** 页面卸载时释放 timer（不杀 keeper——长时任务跨页面存续是 H3 语义） */
  dispose(): void {
    this.stopHeartbeat();
  }
}