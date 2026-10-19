import { StorPoolViewModel } from '../viewmodel/StorPoolViewModel';
import { Theme } from '../common/Theme';
import { SectionHeader, LabeledRow } from '../common/UiKit';

/**
 * H1 区块：设备注册 + 控制面心跳（状态与动作全部委托 StorPoolViewModel）
 * UI 文案/结构自 pages/Index.ets 逐字迁移（e2e 以「注册/重新注册/启动心跳/停止心跳」文本驱动，不可改）。
 */
@Component
export struct RegistrationSection {
  @ObjectLink model: StorPoolViewModel;

  build() {
    Column({ space: 10 }) {
      Divider().margin({ top: 12 })
      SectionHeader({
        title: '② H1 — 设备注册与控制面心跳',
        subtitle: 'POST /devices/register + /devices/heartbeat'
      })

      Row({ space: 8 }) {
        Text('控制面').fontSize(Theme.SMALL_SIZE).width(48)
        TextInput({ text: this.model.ctrlUrl, placeholder: 'http://host:port' })
          .fontSize(Theme.SMALL_SIZE)
          .height(36)
          .layoutWeight(1)
          .onChange((v: string) => this.model.ctrlUrl = v)
      }
      .width('100%')

      Column({ space: 5 }) {
        this.regInfo()
      }
      .width('100%')
      .padding(Theme.CARD_PADDING)
      .borderRadius(Theme.CARD_RADIUS)
      .backgroundColor(Theme.CARD_BG)
      .alignItems(HorizontalAlign.Start)

      Row({ space: 10 }) {
        Button(this.model.regOk ? '重新注册' : '注册')
          .fontSize(Theme.BODY_SIZE)
          .height(Theme.BTN_HEIGHT)
          .layoutWeight(1)
          .onClick(() => this.model.reRegister())
        if (this.model.hbRunning) {
          Button('停止心跳')
            .fontSize(Theme.BODY_SIZE)
            .height(Theme.BTN_HEIGHT)
            .layoutWeight(1)
            .backgroundColor(Theme.BTN_STOP)
            .onClick(() => {
              this.model.stopHeartbeat();
              this.model.hbStatus = '已手动停止';
            })
        } else {
          Button('启动心跳')
            .fontSize(Theme.BODY_SIZE)
            .height(Theme.BTN_HEIGHT)
            .layoutWeight(1)
            .backgroundColor(Theme.BTN_HEARTBEAT_ON)
            .onClick(() => this.model.startHeartbeat())
        }
      }
      .width('100%')
    }
    .width('100%')
    .alignItems(HorizontalAlign.Start)
  }

  @Builder
  regInfo() {
    LabeledRow({
      label: '注册状态',
      value: this.model.regStatus,
      valueColor: this.model.regOk ? Theme.SUCCESS : Theme.DANGER
    })
    LabeledRow({ label: 'deviceName', value: this.model.deviceName })
    LabeledRow({ label: 'deviceId', value: this.model.deviceId })
    LabeledRow({
      label: '心跳',
      value: this.model.hbRunning
        ? `${this.model.hbCount} 次 / 最近 ${this.model.hbLastTime} / ${this.model.hbStatus}`
        : '未运行'
    })
  }
}