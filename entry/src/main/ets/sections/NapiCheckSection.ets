import { bridgeVersion, ping, encodeHello, decodeHello, HelloInfo } from '../model/StorPoolNapi';
import { Theme } from '../common/Theme';
import { SectionHeader } from '../common/UiKit';

/**
 * H0b 区块：Rust → NAPI → ArkTS 全链路四项检查（自包含，无外部状态依赖）
 * 逻辑自 pages/Index.ets 迁移，行为零变更；
 * 颜色判定由字符串前缀改为布尔位（P1-7：消灭 OK:/FAIL: 式字符串协议）。
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

@Component
export struct NapiCheckSection {
  @State items: CheckItem[] = [];
  @State napiSummary: string = 'H0b NAPI 桥接验证';
  @State napiAllPass: boolean = false;
  @State napiRunning: boolean = false;

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
    this.napiAllPass = passed === results.length && results.length > 0;
    this.napiSummary = this.napiAllPass
      ? `全部通过 ${passed}/${results.length} — NAPI 全链路 OK`
      : `未全通过 ${passed}/${results.length}`;
    this.napiRunning = false;
  }

  aboutToAppear(): void {
    this.runNapiChecks();
  }

  build() {
    Column({ space: 10 }) {
      SectionHeader({
        title: '① H0b — NAPI 桥接验证',
        subtitle: 'Rust(syncthing-rust) → NAPI → ArkTS'
      })
      Text(this.napiSummary)
        .fontSize(Theme.BODY_SIZE)
        .fontColor(this.napiAllPass ? Theme.SUCCESS : Theme.DANGER)
      ForEach(this.items, (it: CheckItem) => {
        Column({ space: 4 }) {
          Row({ space: 6 }) {
            Text(it.pass ? '✅' : '❌').fontSize(13)
            Text(it.title).fontSize(Theme.BODY_SIZE).fontWeight(FontWeight.Medium)
          }
          Text(it.detail)
            .fontSize(Theme.SUBTITLE_SIZE)
            .fontColor(Theme.TEXT_SECONDARY)
            .maxLines(2)
            .textOverflow({ overflow: TextOverflow.Ellipsis })
        }
        .width('100%')
        .padding(Theme.CARD_PADDING)
        .borderRadius(Theme.CARD_RADIUS)
        .backgroundColor(Theme.CARD_BG)
        .alignItems(HorizontalAlign.Start)
      }, (it: CheckItem) => it.title)
      Button('重新运行 NAPI 检查')
        .fontSize(Theme.BODY_SIZE)
        .height(Theme.BTN_HEIGHT)
        .onClick(() => this.runNapiChecks())
    }
    .width('100%')
    .alignItems(HorizontalAlign.Start)
  }
}