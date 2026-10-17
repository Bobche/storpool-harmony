import { NamespaceNode } from '../net/ControlPlaneClient';
import { NamespaceStore, NamespaceStat } from '../model/NamespaceStore';
import { ControlPlaneClient, NamespaceResponse } from '../net/ControlPlaneClient';
import { common } from '@kit.AbilityKit';

/**
 * 虚拟目录树浏览组件（H2）
 *
 * 职责：拉取（全量/增量）→ SQLite 缓存 → 面包屑 + 列表浏览
 * 数据流：控制面 GET /namespace?since=游标 → upsert namespace 表 → 本地查询渲染
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
  @Prop ctrlUrl: string = '';
  @Prop deviceToken: string = '';
  @Prop registered: boolean = false;

  @State syncing: boolean = false;
  @State syncMsg: string = '未同步';
  @State statLine: string = '';
  @State crumbs: Crumb[] = [];
  @State entries: NamespaceNode[] = [];

  private store: NamespaceStore = new NamespaceStore();
  private storeReady: boolean = false;

  private static fmtSize(bytes: number): string {
    if (bytes < 1024) {
      return `${bytes} B`;
    }
    if (bytes < 1048576) {
      return `${(bytes / 1024).toFixed(1)} KB`;
    }
    return `${(bytes / 1048576).toFixed(1)} MB`;
  }

  private async ensureStore(): Promise<boolean> {
    if (this.storeReady) {
      return true;
    }
    try {
      const ctx = this.getUIContext().getHostContext() as common.UIAbilityContext;
      if (ctx === undefined) {
        this.syncMsg = '获取 Context 失败';
        return false;
      }
      await this.store.init(ctx);
      this.storeReady = true;
      return true;
    } catch (e) {
      this.syncMsg = `SQLite 初始化失败: ${e}`;
      return false;
    }
  }

  /** 同步：首次全量（since=0），此后按游标增量 */
  async sync(): Promise<void> {
    if (!this.registered || this.deviceToken.length === 0) {
      this.syncMsg = '请先注册设备';
      return;
    }
    if (!(await this.ensureStore())) {
      return;
    }
    this.syncing = true;
    try {
      const since: number = await this.store.getSyncedVersion();
      const client = new ControlPlaneClient(this.ctrlUrl);
      const resp: NamespaceResponse = await client.fetchNamespace(this.deviceToken, since);
      await this.store.upsertNodes(resp.nodes);
      await this.store.setSyncedVersion(resp.version);
      this.syncMsg = `已同步（${resp.mode === 'full' ? '全量' : '增量'} `
        + `${resp.nodes.length} 节点，v${since}→v${resp.version}）`;
      await this.refreshStat();
      await this.refreshRoot();
    } catch (e) {
      this.syncMsg = `同步失败: ${e}`;
    }
    this.syncing = false;
  }

  private async refreshStat(): Promise<void> {
    try {
      const s: NamespaceStat = await this.store.stat();
      this.statLine = `${s.totalNodes} 节点 · ${s.fileCount} 文件 · `
        + `${NamespaceBrowser.fmtSize(s.totalSize)}`;
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
          .fontSize(12)
          .fontColor(this.syncMsg.startsWith('已同步') ? '#2E7D32' : '#666666')
          .layoutWeight(1)
          .maxLines(1)
          .textOverflow({ overflow: TextOverflow.Ellipsis })
        if (this.syncing) {
          LoadingProgress().width(20).height(20)
        }
        Button('同步目录树')
          .fontSize(12)
          .height(32)
          .enabled(this.registered && !this.syncing)
          .onClick(() => this.sync())
      }
      .width('100%')

      if (this.statLine.length > 0) {
        Text(this.statLine).fontSize(11).fontColor('#888888')
      }

      // 面包屑
      if (this.crumbs.length > 0) {
        Scroll() {
          Row({ space: 4 }) {
            ForEach(this.crumbs, (c: Crumb, idx?: number) => {
              Row({ space: 4 }) {
                Text(c.name.length > 0 ? c.name : '/')
                  .fontSize(12)
                  .fontColor(idx === this.crumbs.length - 1 ? '#333333' : '#1976D2')
                  .onClick(() => this.jumpTo(idx as number))
                if (idx !== undefined && idx < this.crumbs.length - 1) {
                  Text('›').fontSize(12).fontColor('#AAAAAA')
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
                  .fontSize(13)
                  .layoutWeight(1)
                  .maxLines(1)
                  .textOverflow({ overflow: TextOverflow.Ellipsis })
                if (n.type === 'dir') {
                  Text('›').fontSize(16).fontColor('#AAAAAA')
                } else {
                  Text(NamespaceBrowser.fmtSize(n.size))
                    .fontSize(11)
                    .fontColor('#888888')
                }
              }
              .width('100%')
              .padding({ left: 10, right: 10, top: 8, bottom: 8 })
              .borderRadius(8)
              .backgroundColor('#FAFAFA')
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
        Text('（空目录）').fontSize(12).fontColor('#AAAAAA')
      }

      if (!this.registered) {
        Text('设备未注册，无法同步（先完成上方 H1 注册）')
          .fontSize(11)
          .fontColor('#C62828')
      }
    }
    .width('100%')
    .padding(10)
    .borderRadius(8)
    .backgroundColor('#F5F5F5')
    .alignItems(HorizontalAlign.Start)
  }
}