import { bridgeVersion, ping, encodeHello, decodeHello, HelloInfo } from '../model/StorPoolNapi';
import { ControlPlaneClient, RegisterResponse, HeartbeatResponse } from '../net/ControlPlaneClient';
import { DeviceIdentity } from '../model/DeviceIdentity';
import { NamespaceBrowser } from '../components/NamespaceBrowser';
import { common } from '@kit.AbilityKit';
import keeper from '../model/backgroundKeeperInstance';

/**
 * StorPool 主页（H0b/H1/H2/H3 验证）
 *
 * 区块一（H0b）：Rust → NAPI → ArkTS 全链路四项检查
 * 区块二（H1） ：设备注册 + 控制面心跳
 *   - 首次进入自动注册（deviceName 随机生成，身份持久化）
 *   - 注册成功后每 15s 心跳一次
 * 区块三（H2） ：元数据同步（虚拟目录树）
 * 区块四（H3） ：后台长时任务保活（dataTransfer + 实况窗进度 + 网络监听）
 */
class CheckItem {
  title: string = '';
  detail: string = '';
  pass: boolean = false;

  constructor(title: string, detail: string, pass: boolean) {
    this.title = title;
    this.detail = detail;
    this.pass = pass;
  }
}

const HEARTBEAT_INTERVAL_MS = 15000;

@Entry
@Component
struct Index {
  // ---- H0b：NAPI 验证 ----
  @State items: CheckItem[] = [];
  @State napiSummary: string = 'H0b NAPI 桥接验证';
  @State napiRunning: boolean = false;

  // ---- H1：设备注册与心跳 ----
  @State ctrlUrl: string = 'http://10.0.2.2:8765';
  @State regStatus: string = '未注册';
  @State regOk: boolean = false;
  @State deviceName: string = '';
  @State deviceId: string = '';
  @State deviceToken: string = '';
  @State hbCount: number = 0;
  @State hbLastTime: string = '';
  @State hbRunning: boolean = false;
  @State hbStatus: string = '';

  // ---- H3：后台保活 ----
  @State kaStatus: string = '未申请';
  @State kaRunning: boolean = false;
  @State netStatus: string = '未知';

  // 非状态成员
  private identity: DeviceIdentity = new DeviceIdentity();
  private hbTimer: number = -1;

  // ================= H0b：NAPI 检查 =================

  runNapiChecks(): void {
    this.napiRunning = true;
    const results: CheckItem[] = [];

    try {
      const v: string = bridgeVersion();
      results.push(new CheckItem('bridgeVersion()', v, v.startsWith('storpool-napi')));
    } catch (e) {
      results.push(new CheckItem('bridgeVersion()', `异常: ${e}`, false));
    }

    try {
      const r: string = ping('StorPool-H0b');
      results.push(new CheckItem('ping("StorPool-H0b")', r, r === 'pong: StorPool-H0b'));
    } catch (e) {
      results.push(new CheckItem('ping()', `异常: ${e}`, false));
    }

    let hex: string = '';
    try {
      hex = encodeHello('HarmonyOS-Device', 'storpool-napi', '0.1.0');
      const ok: boolean = hex.length > 0 && hex.length % 2 === 0;
      results.push(new CheckItem('encodeHello(dev, client, ver)',
        `${hex.substring(0, 40)}…（${hex.length / 2} 字节 BEP Hello）`, ok));
    } catch (e) {
      results.push(new CheckItem('encodeHello()', `异常: ${e}`, false));
    }

    if (hex.length > 0) {
      try {
        const h: HelloInfo = decodeHello(hex);
        const ok: boolean = h.deviceName === 'HarmonyOS-Device'
          && h.clientName === 'storpool-napi'
          && h.clientVersion === '0.1.0'
          && h.numConnections === 1
          && h.timestamp > 0;
        results.push(new CheckItem('decodeHello(hex) round-trip',
          `${h.deviceName} / ${h.clientName} ${h.clientVersion}`, ok));
      } catch (e) {
        results.push(new CheckItem('decodeHello()', `异常: ${e}`, false));
      }
    }

    this.items = results;
    const passed: number = results.filter((it: CheckItem): boolean => it.pass).length;
    this.napiSummary = passed === results.length && results.length > 0
      ? `全部通过 ${passed}/${results.length} — NAPI 全链路 OK`
      : `未全通过 ${passed}/${results.length}`;
    this.napiRunning = false;
  }

  // ================= H1：注册 + 心跳 =================

  private static fmtTime(ms: number): string {
    const d = new Date(ms);
    const p = (n: number): string => n.toString().padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  async ensureRegistration(): Promise<void> {
    try {
      this.regStatus = '初始化…';
      const ctx = this.getUIContext().getHostContext();
      if (ctx === undefined) {
        this.regStatus = '获取 Context 失败';
        return;
      }
      await this.identity.load(ctx as common.UIAbilityContext);

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
      const name: string = DeviceIdentity.generateDeviceName();
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
        this.hbLastTime = Index.fmtTime(Date.now());
        this.hbStatus = '在线';
        // H3：保活运行中时，每次心跳联动更新实况窗进度（防 dataTransfer 10 分钟无进度被取消）
        if (keeper.isRunning()) {
          const pct: number = (this.hbCount * 5) % 101;
          keeper.publishProgress(`StorPool 同步中（心跳 ${this.hbCount} 次）`, 'heartbeat', pct);
        }
      } else {
        this.hbStatus = `心跳异常: status=${resp.status}`;
      }
    } catch (e) {
      this.hbStatus = `心跳失败: ${e}`;
    }
  }

  // ================= H3：后台保活 =================

  async doStartKeepAlive(): Promise<void> {
    this.kaStatus = '申请中…';
    const ctx = this.getUIContext().getHostContext();
    if (ctx === undefined) {
      this.kaStatus = '获取 Context 失败';
      return;
    }
    const result: string = await keeper.startKeepAlive(
      ctx as common.UIAbilityContext,
      (online: boolean) => {
        this.netStatus = online ? '在线（netAvailable）' : '离线（netLost）';
      });
    if (result.startsWith('OK')) {
      this.kaRunning = true;
      this.kaStatus = `已保活 nid=${result.substring(3)}`;
      const pct: number = (this.hbCount * 5) % 101;
      keeper.publishProgress(`StorPool 同步中（心跳 ${this.hbCount} 次）`, 'heartbeat', pct);
    } else {
      this.kaRunning = false;
      this.kaStatus = `申请失败 ${result}`;
    }
  }

  async doStopKeepAlive(): Promise<void> {
    const ctx = this.getUIContext().getHostContext();
    if (ctx === undefined) {
      return;
    }
    const result: string = await keeper.stopKeepAlive(ctx as common.UIAbilityContext);
    this.kaRunning = false;
    this.kaStatus = result.startsWith('OK') ? '已停止' : `停止失败 ${result}`;
  }

  reRegister(): void {
    this.stopHeartbeat();
    this.hbCount = 0;
    this.hbLastTime = '';
    this.hbStatus = '';
    this.regOk = false;
    this.regStatus = '重新注册中…';
    // 重新注册：复用已有 deviceName（若存在），否则生成新的
    this.ensureRegistration();
  }

  // ================= 生命周期 =================

  aboutToAppear(): void {
    this.runNapiChecks();
    this.ensureRegistration();
  }

  aboutToDisappear(): void {
    this.stopHeartbeat();
  }

  // ================= UI =================

  @Builder
  SectionTitle(title: string, subtitle: string) {
    Column({ space: 2 }) {
      Text(title).fontSize(17).fontWeight(FontWeight.Bold)
      Text(subtitle).fontSize(11).fontColor('#888888')
    }
    .width('100%')
    .alignItems(HorizontalAlign.Start)
    .margin({ top: 20, bottom: 8 })
  }

  build() {
    Scroll() {
      Column({ space: 10 }) {
        // ---------- 区块一：H0b NAPI ----------
        this.SectionTitle('① H0b — NAPI 桥接验证', 'Rust(syncthing-rust) → NAPI → ArkTS')
        Text(this.napiSummary)
          .fontSize(13)
          .fontColor(this.napiSummary.startsWith('全部通过') ? '#2E7D32' : '#C62828')
        ForEach(this.items, (it: CheckItem) => {
          Column({ space: 4 }) {
            Row({ space: 6 }) {
              Text(it.pass ? '✅' : '❌').fontSize(13)
              Text(it.title).fontSize(13).fontWeight(FontWeight.Medium)
            }
            Text(it.detail)
              .fontSize(11)
              .fontColor('#555555')
              .maxLines(2)
              .textOverflow({ overflow: TextOverflow.Ellipsis })
          }
          .width('100%')
          .padding(10)
          .borderRadius(8)
          .backgroundColor('#F5F5F5')
          .alignItems(HorizontalAlign.Start)
        }, (it: CheckItem) => it.title)
        Button('重新运行 NAPI 检查')
          .fontSize(13)
          .height(34)
          .onClick(() => this.runNapiChecks())

        // ---------- 区块二：H1 设备注册与心跳 ----------
        Divider().margin({ top: 12 })
        this.SectionTitle('② H1 — 设备注册与控制面心跳', 'POST /devices/register + /devices/heartbeat')

        Row({ space: 8 }) {
          Text('控制面').fontSize(12).width(48)
          TextInput({ text: this.ctrlUrl, placeholder: 'http://host:port' })
            .fontSize(12)
            .height(36)
            .layoutWeight(1)
            .onChange((v: string) => this.ctrlUrl = v)
        }
        .width('100%')

        Column({ space: 5 }) {
          Row({ space: 8 }) {
            Text('注册状态').fontSize(12).fontColor('#666666').width(64)
            Text(this.regStatus).fontSize(12)
              .fontColor(this.regOk ? '#2E7D32' : '#C62828')
              .layoutWeight(1)
          }
          Row({ space: 8 }) {
            Text('deviceName').fontSize(12).fontColor('#666666').width(64)
            Text(this.deviceName.length > 0 ? this.deviceName : '—').fontSize(12).layoutWeight(1)
          }
          Row({ space: 8 }) {
            Text('deviceId').fontSize(12).fontColor('#666666').width(64)
            Text(this.deviceId.length > 0 ? this.deviceId : '—').fontSize(12)
              .maxLines(1)
              .textOverflow({ overflow: TextOverflow.Ellipsis })
              .layoutWeight(1)
          }
          Row({ space: 8 }) {
            Text('心跳').fontSize(12).fontColor('#666666').width(64)
            Text(this.hbRunning
              ? `${this.hbCount} 次 / 最近 ${this.hbLastTime} / ${this.hbStatus}`
              : '未运行').fontSize(12).layoutWeight(1)
          }
        }
        .width('100%')
        .padding(10)
        .borderRadius(8)
        .backgroundColor('#F5F5F5')
        .alignItems(HorizontalAlign.Start)

        Row({ space: 10 }) {
          Button(this.regOk ? '重新注册' : '注册')
            .fontSize(13)
            .height(34)
            .layoutWeight(1)
            .onClick(() => this.reRegister())
          if (this.hbRunning) {
            Button('停止心跳')
              .fontSize(13)
              .height(34)
              .layoutWeight(1)
              .backgroundColor('#EF5350')
              .onClick(() => {
                this.stopHeartbeat();
                this.hbStatus = '已手动停止';
              })
          } else {
            Button('启动心跳')
              .fontSize(13)
              .height(34)
              .layoutWeight(1)
              .backgroundColor('#66BB6A')
              .onClick(() => this.startHeartbeat())
          }
        }
        .width('100%')

        // ---------- 区块三：H2 元数据同步（虚拟目录树） ----------
        Divider().margin({ top: 12 })
        this.SectionTitle('③ H2 — 元数据同步（虚拟目录树）', 'GET /namespace → SQLite → 本地浏览')
        NamespaceBrowser({
          ctrlUrl: this.ctrlUrl,
          deviceToken: this.deviceToken,
          registered: this.regOk,
        })

        // ---------- 区块四：H3 后台保活 ----------
        Divider().margin({ top: 12 })
        this.SectionTitle('④ H3 — 后台长时任务保活', 'dataTransfer + 实况窗进度 + 网络监听')

        Column({ space: 5 }) {
          Row({ space: 8 }) {
            Text('保活状态').fontSize(12).fontColor('#666666').width(64)
            Text(this.kaStatus).fontSize(12)
              .fontColor(this.kaRunning ? '#2E7D32' : '#C62828')
              .layoutWeight(1)
          }
          Row({ space: 8 }) {
            Text('网络状态').fontSize(12).fontColor('#666666').width(64)
            Text(this.netStatus).fontSize(12).layoutWeight(1)
          }
        }
        .width('100%')
        .padding(10)
        .borderRadius(8)
        .backgroundColor('#F5F5F5')
        .alignItems(HorizontalAlign.Start)

        Row({ space: 10 }) {
          Button(this.kaRunning ? '保活中' : '申请保活')
            .fontSize(13)
            .height(34)
            .layoutWeight(1)
            .backgroundColor(this.kaRunning ? '#9E9E9E' : '#5C6BC0')
            .enabled(!this.kaRunning)
            .onClick(() => this.doStartKeepAlive())
          Button('停止保活')
            .fontSize(13)
            .height(34)
            .layoutWeight(1)
            .backgroundColor('#EF5350')
            .onClick(() => this.doStopKeepAlive())
        }
        .width('100%')

        Text('心跳间隔 15s；H3 长时任务保活 + 实况窗进度通知')
          .fontSize(10)
          .fontColor('#999999')
          .margin({ bottom: 30 })
      }
      .width('100%')
      .padding({ left: 16, right: 16 })
    }
    .width('100%')
    .height('100%')
    .align(Alignment.Top)
    .scrollBar(BarState.Auto)
  }
}