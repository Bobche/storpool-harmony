import { Theme } from './Theme';

/**
 * P1-7：公共 UI 积木（此前在 Index.ets / NamespaceBrowser.ets 中重复书写的布局模式）
 * - SectionHeader：区块标题 + 副标题（原 @Builder SectionTitle）
 * - LabeledRow  ：「固定宽标签 + 值文本」行（原重复 5 处的 Row 模板，@Prop 保证父刷新透传）
 * - 卡片容器    ：普通 Column + Theme 令牌（方案对比说明见文件底部注释）
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
  // @Prop：父组件（@ObjectLink/@State 观察域）刷新时子组件值同步刷新。
  // 若用无装饰器成员，仅创建时一次性赋值、后续状态变化不更新（V1 冒烟实锤）。
  @Prop label: string = '';
  @Prop value: string = '';
  @Prop valueColor: string = '';   // 空串 = 用默认正文色

  build() {
    Row({ space: 8 }) {
      Text(this.label).fontSize(Theme.SMALL_SIZE).fontColor(Theme.TEXT_LABEL).width(Theme.LABEL_WIDTH)
      Text(this.value.length > 0 ? this.value : '—')
        .fontSize(Theme.SMALL_SIZE)
        .layoutWeight(1)

        .fontColor(this.valueColor.length > 0 ? this.valueColor : Theme.TEXT_PRIMARY)
    }
    .width('100%')
  }
}

// 卡片容器说明：原「padding(10)+borderRadius(8)+bg#F5F5F5」重复 4 处。
// 曾尝试两种收敛方案，均因 V1 刷新/共享机制被否：
//  1) @BuilderParam 插槽式 InfoCard 组件——父组件 @ObjectLink 刷新不会重建
//     子组件插槽（冒烟实测冻结首帧）；
//  2) 全局 @Styles 跨文件 export——官方限制「只能在当前文件内使用 @Styles，
//     不支持 export」（arkts-style 文档）。
// 故卡片 = 消费组件自有 build 内的普通 Column + Theme 令牌样式（4 链式调用），
// 样式的唯一真源仍是 Theme；组件观察域正确，刷新语义无歧义。