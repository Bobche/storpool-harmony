import { common } from '@kit.AbilityKit';
import { StorPoolViewModel } from '../viewmodel/StorPoolViewModel';
import { SectionHeader } from '../common/UiKit';
import { NapiCheckSection } from '../sections/NapiCheckSection';
import { RegistrationSection } from '../sections/RegistrationSection';
import { KeepAliveSection } from '../sections/KeepAliveSection';
import { NamespaceBrowser } from '../components/NamespaceBrowser';

/**
 * StorPool 主页装配壳（P1-7 拆分后）
 *
 * 职责仅剩：@Entry 路由入口 + 生命周期绑定 + 四个功能区装配。
 * 状态与业务逻辑全部下沉：
 *   H0b → sections/NapiCheckSection（自包含）
 *   H1  → sections/RegistrationSection + viewmodel/StorPoolViewModel
 *   H2  → components/NamespaceBrowser + viewmodel（@ObjectLink 共享状态源）
 *   H3  → sections/KeepAliveSection + viewmodel
 * 服务调用（ControlPlaneClient/rcp Session、DeviceIdentity、BackgroundKeeper）
 * 均由 viewmodel/组件持有，页面不再直接 new。
 */
@Entry
@Component
struct Index {
  @State model: StorPoolViewModel = new StorPoolViewModel();

  aboutToAppear(): void {
    const ctx = this.getUIContext().getHostContext();
    if (ctx === undefined) {
      this.model.regStatus = '获取 Context 失败';
      return;
    }
    const abilityCtx = ctx as common.UIAbilityContext;
    this.model.bindContext(abilityCtx);
    this.model.ensureRegistration(abilityCtx);
  }

  aboutToDisappear(): void {
    this.model.dispose();
  }

  build() {
    Scroll() {
      Column({ space: 10 }) {
        // ---------- ① H0b：NAPI 桥接验证 ----------
        NapiCheckSection()

        // ---------- ② H1：设备注册与心跳 ----------
        RegistrationSection({ model: this.model })

        // ---------- ③ H2：元数据同步（虚拟目录树） ----------
        Divider().margin({ top: 12 })
        SectionHeader({
          title: '③ H2 — 元数据同步（虚拟目录树）',
          subtitle: 'GET /namespace → SQLite → 本地浏览'
        })
        NamespaceBrowser({ model: this.model })

        // ---------- ④ H3：后台长时任务保活 ----------
        KeepAliveSection({ model: this.model })
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