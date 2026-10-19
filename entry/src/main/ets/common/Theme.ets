/**
 * P1-7：全局视觉令牌（Theme tokens）
 *
 * 收敛此前散落在 Index.ets / NamespaceBrowser.ets 的硬编码颜色与尺寸，
 * 单一事实源；组件层只引用命名常量，禁止再写裸色值。
 * 语义按现有 UI 逐字映射（改色值=行为变更，本文件只做收敛，取值与现状一致）。
 */
export class Theme {
  // ---- 文本色 ----
  static readonly TEXT_PRIMARY: string = '#333333';   // 主文本（面包屑当前页）
  static readonly TEXT_SECONDARY: string = '#555555'; // 检查项详情
  static readonly TEXT_LABEL: string = '#666666';     // 标签列 / 中性状态文本
  static readonly TEXT_HINT: string = '#888888';      // 副标题 / 统计行 / 尺寸
  static readonly TEXT_MUTED: string = '#999999';     // 页脚说明
  static readonly TEXT_PLACEHOLDER: string = '#AAAAAA'; // 分隔符 / 空目录提示

  // ---- 状态色 ----
  static readonly SUCCESS: string = '#2E7D32'; // 深绿：成功态文本
  static readonly DANGER: string = '#C62828';  // 深红：失败态文本
  static readonly LINK: string = '#1976D2';    // 蓝：可点击面包屑 / 进度文本

  // ---- 按钮色 ----
  static readonly BTN_HEARTBEAT_ON: string = '#66BB6A';  // 启动心跳
  static readonly BTN_STOP: string = '#EF5350';          // 停止心跳/停止保活
  static readonly BTN_KEEPALIVE: string = '#5C6BC0';     // 申请保活
  static readonly BTN_DISABLED: string = '#9E9E9E';      // 进行中置灰
  static readonly BTN_DOWNLOAD: string = '#42A5F5';      // 分片下载 ⬇

  // ---- 容器背景 ----
  static readonly CARD_BG: string = '#F5F5F5';     // 灰卡片：信息区块
  static readonly ENTRY_BG: string = '#FAFAFA';    // 目录列表项
  static readonly DOWNLOAD_BOX_BG: string = '#FFFFFF'; // 下载进度白盒

  // ---- 尺寸令牌 ----
  static readonly LABEL_WIDTH: number = 64;    // 「标签-值」行标签列宽
  static readonly CARD_PADDING: number = 10;
  static readonly CARD_RADIUS: number = 8;
  static readonly TITLE_SIZE: number = 17;
  static readonly SUBTITLE_SIZE: number = 11;
  static readonly BODY_SIZE: number = 13;
  static readonly SMALL_SIZE: number = 12;
  static readonly TINY_SIZE: number = 10;
  static readonly BTN_HEIGHT: number = 34;      // 主按钮行
  static readonly BTN_HEIGHT_SMALL: number = 32; // 区块内小按钮
}