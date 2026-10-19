import { StorPoolViewModel } from '../viewmodel/StorPoolViewModel';
import { Theme } from '../common/Theme';
import { SectionHeader, LabeledRow, InfoCard } from '../common/UiKit';

/**
 * H3 区块：后台长时任务保活（状态与动作全部委托 StorPoolViewModel）
 * UI 文案逐字迁移——e2e 断言依赖「申请保活」按钮文本与「已保活 nid=<数字>」正则，不可改。
 * KeepAliveResult 已结构化：失败状态由 errorCode/errorMessage 拼接，不再解析 FAIL: 前缀。
 */
@Component
export struct KeepAliveSection {
  @ObjectLink model: StorPoolViewModel;

  build() {
    Column({ space: 10 }) {
      Divider().margin({ top: 12 })
      SectionHeader({
        title: '④ H3 — 后台长时任务保活',
        subtitle: 'dataTransfer + 实况窗进度 + 网络监听'
      })

      InfoCard({
        content: () => {
          this.kaInfo()
        }
      })

      Row({ space: 10 }) {
        Button(this.model.kaRunning ? '保活中' : '申请保活')
          .fontSize(Theme.BODY_SIZE)
          .height(Theme.BTN_HEIGHT)
          .layoutWeight(1)
          .backgroundColor(this.model.kaRunning ? Theme.BTN_DISABLED : Theme.BTN_KEEPALIVE)
          .enabled(!this.model.kaRunning)
          .onClick(() => this.model.doStartKeepAlive())
        Button('停止保活')
          .fontSize(Theme.BODY_SIZE)
          .height(Theme.BTN_HEIGHT)
          .layoutWeight(1)
          .backgroundColor(Theme.BTN_STOP)
          .onClick(() => this.model.doStopKeepAlive())
      }
      .width('100%')

      Text('心跳间隔 15s；H3 长时任务保活 + 实况窗进度通知')
        .fontSize(Theme.TINY_SIZE)
        .fontColor(Theme.TEXT_MUTED)
        .margin({ bottom: 30 })
    }
    .width('100%')
    .alignItems(HorizontalAlign.Start)
  }

  @Builder
  kaInfo() {
    LabeledRow({
      label: '保活状态',
      value: this.model.kaStatus,
      valueColor: this.model.kaRunning ? Theme.SUCCESS : Theme.DANGER
    })
    LabeledRow({ label: '网络状态', value: this.model.netStatus })
  }
}