import { NamespaceNode, ControlPlaneClient, NamespaceResponse, HttpError } from '../net/ControlPlaneClient';
import { NamespaceStore, NamespaceStat } from '../model/NamespaceStore';
import { ChunkedDownloader, DownloadProgress, DownloadResult } from '../model/ChunkedDownloader';
import keeper from '../model/backgroundKeeperInstance';
import { StorPoolViewModel } from '../viewmodel/StorPoolViewModel';
import { Theme } from '../common/Theme';
import { fmtSize } from '../common/Format';
import { common } from '@kit.AbilityKit';

/**
 * 虚拟目录树浏览组件（H2 + H4）
 *
 * 职责：拉取（全量/增量）→ SQLite 缓存 → 面包屑 + 列表浏览
 *       文件项分片下载（H4：Range GET → 重组 → sha256 校验 → 落盘 filesDir/downloads）
 * 数据流：控制面 GET /namespace?since=游标 → upsert namespace 表 → 本地查询渲染
 * P1-7：ctrlUrl/deviceToken/registered 三个单向 @Prop + onAuthLost 回调收敛为
 * @ObjectLink model（同一 @Observed 状态源，401 自愈直接 model.selfHeal）；
 * 色值引用 Theme；「已同步/✅」字符串前缀判色改布尔位。UI 文案逐字保留（e2e 依赖）。
 */
class Crumb {
  id: number = 0;
  name: string = '/';

  constructor(id: number, name: string) {
    this.id = id;
    this.name = name;
  }
}

@Component
export struct NamespaceBrowser {
  @ObjectLink model: StorPoolViewModel;

  @State syncing: boolean = false;
  @State syncMsg: string = '未同步';
  @State syncOk: boolean = false;
  @State statLine: string = '';
  @State crumbs: Crumb[] = [];
  @State entries: NamespaceNode[] = [];

  // ---- H4：分片下载状态 ----
  @State dlRunning: boolean = false;
  @State dlName: string = '';
  @State dlPercent: number = 0;
  @State dlDetail: string = '';
  @State dlMsg: string = '';
  @State dlOk: boolean = false;

  private store: NamespaceStore = new NamespaceStore();
  private storeReady: boolean = false;
  private downloader: ChunkedDownloader | null = null;

  private async ensureStore(): Promise<boolean> {
    if (this.storeReady) {
      return true;
    }
    try {
      const ctx = this.getUIContext().getHostContext() as common.UIAbilityContext;
      if (ctx === undefined) {
        this.syncMsg = '获取 Context 失败';
        this.syncOk = false;
        return false;
      }
      await this.store.init(ctx);
      this.storeReady = true;
      return true;
    } catch (e) {
      this.syncMsg = `SQLite 初始化失败: ${e}`;
      this.syncOk = false;
      return false;
    }
  }

  /** 同步：首次全量（since=0），此后按游标增量（P0-1：含墓碑删除与版本回退保护） */
  async sync(): Promise<void> {
    if (!this.model.regOk || this.model.deviceToken.length === 0) {
      this.syncMsg = '请先注册设备';
      this.syncOk = false;
      return;
    }
    if (!(await this.ensureStore())) {
      return;
    }
    this.syncing = true;
    try {
      const since: number = await this.store.getSyncedVersion();
      const client = new ControlPlaneClient(this.model.ctrlUrl);
      const resp: NamespaceResponse = await client.fetchNamespace(this.model.deviceToken, since);
      if (resp.version < since) {
        // P0-1b：服务端版本回退（重启/重置）——本次增量响应的 nodes 按 version>since
        // 过滤，回退场景下必为空，绝不可直接 resetAll（会清空本地树）。
        // 正确链路：再发一次 since=0 全量拉取 → 清库重存 → 游标写服务端当前版本。
        const full: NamespaceResponse = await client.fetchNamespace(this.model.deviceToken, 0);
        await this.store.resetAll(full.nodes, full.version);
        this.syncMsg = `检测到服务端版本回退 v${since}→v${full.version}，已全量重拉 `
          + `${full.nodes.length} 节点`;
        this.syncOk = false; // 回退属异常事件，保持中性色（与拆前一致）
      } else {
        await this.store.applyChanges(resp.nodes, resp.version);
        this.syncMsg = `已同步（${resp.mode === 'full' ? '全量' : '增量'} `
          + `${resp.nodes.length} 节点，v${since}→v${resp.version}）`;
        this.syncOk = true;
      }
      await this.refreshStat();
      await this.refreshRoot();
    } catch (e) {
      if (e instanceof HttpError && e.statusCode === 401) {
        // P0-2：token 失效（mock 重启内存丢失/服务端重置）→ 直接触发宿主自愈
        this.syncMsg = '身份失效（401），已触发自动重注册';
        this.syncOk = false;
        this.model.selfHeal('namespace');
      } else {
        this.syncMsg = `同步失败: ${e}`;
        this.syncOk = false;
      }
    }
    this.syncing = false;
  }

  /** H4：分片下载文件节点到 filesDir/downloads（进度联动 UI + keeper 实况窗） */
  async startDownload(node: NamespaceNode): Promise<void> {
    if (this.dlRunning) {
      this.dlMsg = '已有下载进行中，请稍候';
      this.dlOk = false;
      return;
    }
    if (!this.model.regOk || this.model.deviceToken.length === 0) {
      this.dlMsg = '设备未注册，无法下载';
      this.dlOk = false;
      return;
    }
    const ctx = this.getUIContext().getHostContext() as common.UIAbilityContext;
    if (ctx === undefined) {
      this.dlMsg = '获取 Context 失败';
      this.dlOk = false;
      return;
    }
    this.dlRunning = true;
    this.dlName = node.name;
    this.dlPercent = 0;
    this.dlDetail = '';
    this.dlMsg = '';
    const downloader = new ChunkedDownloader(this.model.ctrlUrl, this.model.deviceToken);
    this.downloader = downloader;
    try {
      const destDir: string = `${ctx.filesDir}/downloads`;
      const onProgress = (p: DownloadProgress): void => {
        this.dlPercent = p.percent;
        this.dlDetail = `${p.stage} · ${p.detail}`;
        // P0-3：真实进度联动实况窗（keeper 未运行时其内部直接 return，安全）
        keeper.publishProgress(`StorPool 下载 ${p.fileName}`, p.detail, p.percent);
      };
      const result: DownloadResult = await downloader.download(node, destDir, onProgress);
      this.dlMsg = `✅ ${node.name} 已下载（${result.receivedBytes}B，sha256 校验`
        + `${result.verified ? '通过' : '跳过'}，${result.durationMs}ms）→ ${result.filePath}`;
      this.dlOk = true;
    } catch (e) {
      if (e instanceof HttpError && e.statusCode === 401) {
        this.dlMsg = '❌ 下载失败：身份失效（401），已触发自动重注册';
        this.model.selfHeal('namespace');
      } else {
        this.dlMsg = `❌ 下载失败: ${e}`;
      }
      this.dlOk = false;
    }
    this.dlRunning = false;
    this.downloader = null;
  }

  private async refreshStat(): Promise<void> {
    try {
      const s: NamespaceStat = await this.store.stat();
      this.statLine = `${s.totalNodes} 节点 · ${s.fileCount} 文件 · `
        + `${fmtSize(s.totalSize)}`;
    } catch (e) {
      this.statLine = '';
    }
  }

  /** 重置到根目录（同步后调用） */
  private async refreshRoot(): Promise<void> {
    const root = await this.store.findRoot();
    if (root === null) {
      this.crumbs = [];
      this.entries = [];
      return;
    }
    this.crumbs = [new Crumb(root.id, '/')];
    await this.refreshEntries();
  }

  private async refreshEntries(): Promise<void> {
    if (this.crumbs.length === 0) {
      this.entries = [];
      return;
    }
    const currentId: number = this.crumbs[this.crumbs.length - 1].id;
    this.entries = await this.store.listChildren(currentId);
  }

  enterDir(node: NamespaceNode): void {
    this.crumbs.push(new Crumb(node.id, node.name));
    this.refreshEntries();
  }

  jumpTo(index: number): void {
    if (index < 0 || index >= this.crumbs.length) {
      return;
    }
    this.crumbs = this.crumbs.slice(0, index + 1);
    this.refreshEntries();
  }

  build() {
    Column({ space: 8 }) {
      Row({ space: 8 }) {
        Text(this.syncMsg)
          .fontSize(Theme.SMALL_SIZE)
          .fontColor(this.syncOk ? Theme.SUCCESS : Theme.TEXT_LABEL)
          .layoutWeight(1)
          .maxLines(1)
          .textOverflow({ overflow: TextOverflow.Ellipsis })
        if (this.syncing) {
          LoadingProgress().width(20).height(20)
        }
        Button('同步目录树')
          .fontSize(Theme.SMALL_SIZE)
          .height(Theme.BTN_HEIGHT_SMALL)
          .enabled(this.model.regOk && !this.syncing)
          .onClick(() => this.sync())
      }
      .width('100%')

      if (this.statLine.length > 0) {
        Text(this.statLine).fontSize(Theme.SUBTITLE_SIZE).fontColor(Theme.TEXT_HINT)
      }

      // 面包屑
      if (this.crumbs.length > 0) {
        Scroll() {
          Row({ space: 4 }) {
            ForEach(this.crumbs, (c: Crumb, idx?: number) => {
              Row({ space: 4 }) {
                Text(c.name.length > 0 ? c.name : '/')
                  .fontSize(Theme.SMALL_SIZE)
                  .fontColor(idx === this.crumbs.length - 1 ? Theme.TEXT_PRIMARY : Theme.LINK)
                  .onClick(() => this.jumpTo(idx as number))
                if (idx !== undefined && idx < this.crumbs.length - 1) {
                  Text('›').fontSize(Theme.SMALL_SIZE).fontColor(Theme.TEXT_PLACEHOLDER)
                }
              }
            }, (c: Crumb, idx?: number) => `${c.id}-${idx}`)
          }
          .padding(4)
        }
        .scrollable(ScrollDirection.Horizontal)
        .width('100%')
      }

      // 目录内容列表
      if (this.entries.length > 0) {
        List({ space: 4 }) {
          ForEach(this.entries, (n: NamespaceNode) => {
            ListItem() {
              Row({ space: 8 }) {
                Text(n.type === 'dir' ? '📁' : '📄').fontSize(16)
                Text(n.name.length > 0 ? n.name : '/')
                  .fontSize(Theme.BODY_SIZE)
                  .layoutWeight(1)
                  .maxLines(1)
                  .textOverflow({ overflow: TextOverflow.Ellipsis })
                if (n.type === 'dir') {
                  Text('›').fontSize(16).fontColor(Theme.TEXT_PLACEHOLDER)
                } else {
                  Text(fmtSize(n.size))
                    .fontSize(Theme.SUBTITLE_SIZE)
                    .fontColor(Theme.TEXT_HINT)
                  // H4：分片下载入口（下载中禁用）
                  Button(this.dlRunning && this.dlName === n.name ? '…' : '⬇')
                    .fontSize(Theme.SMALL_SIZE)
                    .height(28)
                    .width(36)
                    .padding(0)
                    .backgroundColor(this.dlRunning && this.dlName === n.name ? Theme.BTN_DISABLED : Theme.BTN_DOWNLOAD)
                    .enabled(this.model.regOk && !this.dlRunning)
                    .onClick(() => {
                      this.startDownload(n);
                    })
                }
              }
              .width('100%')
              .padding({ left: 10, right: 10, top: 8, bottom: 8 })
              .borderRadius(Theme.CARD_RADIUS)
              .backgroundColor(Theme.ENTRY_BG)
              .onClick(() => {
                if (n.type === 'dir') {
                  this.enterDir(n);
                }
              })
            }
          }, (n: NamespaceNode) => `${n.id}-${n.name}-${n.version}`)
        }
        .width('100%')
        .height(220)
        .scrollBar(BarState.Auto)
      } else if (this.statLine.length > 0) {
        Text('（空目录）').fontSize(Theme.SMALL_SIZE).fontColor(Theme.TEXT_PLACEHOLDER)
      }

      // H4：下载进度区块（下载中展示进度条；结果消息常驻展示）
      if (this.dlRunning || this.dlMsg.length > 0) {
        Column({ space: 4 }) {
          if (this.dlRunning) {
            Row({ space: 8 }) {
              Text(this.dlName)
                .fontSize(Theme.SMALL_SIZE)
                .layoutWeight(1)
                .maxLines(1)
                .textOverflow({ overflow: TextOverflow.Ellipsis })
              Text(`${this.dlPercent}%`)
                .fontSize(Theme.SMALL_SIZE)
                .fontColor(Theme.LINK)
            }
            .width('100%')
            Progress({ value: this.dlPercent, total: 100, type: ProgressType.Linear })
              .width('100%')
            Text(this.dlDetail)
              .fontSize(Theme.TINY_SIZE)
              .fontColor(Theme.TEXT_HINT)
              .maxLines(1)
              .textOverflow({ overflow: TextOverflow.Ellipsis })
          }
          if (this.dlMsg.length > 0) {
            Text(this.dlMsg)
              .fontSize(Theme.SUBTITLE_SIZE)
              .fontColor(this.dlOk ? Theme.SUCCESS : Theme.DANGER)
              .maxLines(2)
              .textOverflow({ overflow: TextOverflow.Ellipsis })
          }
        }
        .width('100%')
        .padding(8)
        .borderRadius(6)
        .backgroundColor(Theme.DOWNLOAD_BOX_BG)
        .alignItems(HorizontalAlign.Start)
      }

      if (!this.model.regOk) {
        Text('设备未注册，无法同步（先完成上方 H1 注册）')
          .fontSize(Theme.SUBTITLE_SIZE)
          .fontColor(Theme.DANGER)
      }
    }
    .width('100%')
    .padding(Theme.CARD_PADDING)
    .borderRadius(Theme.CARD_RADIUS)
    .backgroundColor(Theme.CARD_BG)
    .alignItems(HorizontalAlign.Start)
  }
}