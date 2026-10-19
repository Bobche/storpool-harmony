import { Theme } from './Theme';

/**
 * P1-7：公共 UI 积木（此前在 Index.ets / NamespaceBrowser.ets 中重复书写的布局模式）
 * - SectionHeader：区块标题 + 副标题（原 @Builder SectionTitle）
 * - LabeledRow  ：「固定宽标签 + 值文本」行（原重复 5 处的 Row 模板）
 * - InfoCard    ：灰底圆角信息容器（原重复 4 处的 padding/radius/bg 组合）
 * 渲染结果与拆前逐像素一致，仅收敛书写。
 */

@Component
export struct SectionHeader {
  title: string = '';
  subtitle: string = '';

  build() {
    Column({ space: 2 }) {
      Text(this.title).fontSize(Theme.TITLE_SIZE).fontWeight(FontWeight.Bold)
      Text(this.subtitle).fontSize(Theme.SUBTITLE_SIZE).fontColor(Theme.TEXT_HINT)
    }
    .width('100%')
    .alignItems(HorizontalAlign.Start)
    .margin({ top: 20, bottom: 8 })
  }
}

@Component
export struct LabeledRow {
  label: string = '';
  value: string = '';
  valueColor: string | undefined = undefined;

  build() {
    Row({ space: 8 }) {
      Text(this.label).fontSize(Theme.SMALL_SIZE).fontColor(Theme.TEXT_LABEL).width(Theme.LABEL_WIDTH)
      Text(this.value.length > 0 ? this.value : '—')
        .fontSize(Theme.SMALL_SIZE)
        .layoutWeight(1)

        .fontColor(this.valueColor !== undefined ? this.valueColor : Theme.TEXT_PRIMARY)
    }
    .width('100%')
  }
}

@Component
export struct InfoCard {
  @BuilderParam content: () => void;

  build() {
    Column({ space: 5 }) {
      this.content()
    }
    .width('100%')
    .padding(Theme.CARD_PADDING)
    .borderRadius(Theme.CARD_RADIUS)
    .backgroundColor(Theme.CARD_BG)
    .alignItems(HorizontalAlign.Start)
  }
}