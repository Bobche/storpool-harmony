import { preferences } from '@kit.ArkData';
import { asset } from '@kit.AssetStoreKit';
import { util } from '@kit.ArkTS';
import { common } from '@kit.AbilityKit';
import { BusinessError } from '@kit.BasicServicesKit';

/**
 * 设备身份管理（H1 + P0-4）
 *
 * P0-4 改造：deviceToken 从 preferences 明文存储 → Asset Store Kit 加密存储。
 *   - deviceId / deviceName 仍存 preferences（非敏感，UI 显示用）
 *   - deviceToken 存 Asset Store（敏感凭据，加密保护）
 *   - 首次解锁后可访问（Accessibility.DEVICE_FIRST_UNLOCKED）
 *   - 兼容旧版：若 Asset 中无 token 但 preferences 有，自动迁移
 *   - 外部接口保持同步语义：load() 时预加载 token 到缓存，getDeviceToken()/isRegistered() 同步返回
 */
const PREF_NAME = 'storpool_device';
const KEY_DEVICE_ID = 'deviceId';
const KEY_DEVICE_TOKEN = 'deviceToken';  // 旧版遗留 key，迁移后清除
const KEY_DEVICE_NAME = 'deviceName';

// Asset Store 别名（每条关键资产的唯一索引）
const ASSET_ALIAS_TOKEN = 'storpool_device_token';

/** string → Uint8Array（Asset API 要求字节数组） */
function strToBytes(s: string): Uint8Array {
  const encoder = new util.TextEncoder();
  return encoder.encodeInto(s);
}

/** Uint8Array → string */
function bytesToStr(b: Uint8Array): string {
  const decoder = new util.TextDecoder();
  return decoder.decodeToString(b);
}

export class DeviceIdentity {
  private prefs?: preferences.Preferences;
  private cachedToken: string = '';  // load() 时预加载，save()/clear() 时同步更新

  async load(context: common.UIAbilityContext): Promise<void> {
    this.prefs = await preferences.getPreferences(context, { name: PREF_NAME });

    // P0-4 兼容迁移：旧版 token 存在 preferences 中，迁移到 Asset Store
    await this.migrateTokenToAsset();

    // 预加载 token 到缓存（保证 getDeviceToken()/isRegistered() 同步可用）
    this.cachedToken = await this.loadTokenFromAsset();
  }

  /**
   * P0-4 迁移：若 preferences 中有明文 token 而 Asset Store 中没有，则迁移。
   * 迁移成功后清除 preferences 中的明文 token。
   */
  private async migrateTokenToAsset(): Promise<void> {
    if (this.prefs === undefined) {
      return;
    }
    const legacyToken = this.prefs.getSync(KEY_DEVICE_TOKEN, '') as string;
    if (legacyToken.length === 0) {
      return; // 无遗留 token，无需迁移
    }

    // 检查 Asset Store 是否已有 token
    try {
      const query: asset.AssetMap = new Map();
      query.set(asset.Tag.ALIAS, strToBytes(ASSET_ALIAS_TOKEN));
      query.set(asset.Tag.RETURN_TYPE, asset.ReturnType.ALL);
      await asset.query(query);
      // Asset Store 已有 token，清除 preferences 中的明文遗留
      this.prefs.deleteSync(KEY_DEVICE_TOKEN);
      await this.prefs.flush();
    } catch (e) {
      const err = e as BusinessError;
      if (err.code === 24000002) {
        // 24000002 = asset not found → 需要迁移
        try {
          await this.saveTokenToAsset(legacyToken);
          // 迁移成功，清除 preferences 中的明文
          this.prefs.deleteSync(KEY_DEVICE_TOKEN);
          await this.prefs.flush();
        } catch (migrateErr) {
          // 迁移失败时保留 preferences 中的 token，下次启动再试
        }
      }
    }
  }

  /**
   * 将 deviceToken 存入 Asset Store（加密存储）。
   * 若已存在同别名资产，先 update；不存在则 add。
   */
  private async saveTokenToAsset(token: string): Promise<void> {
    const alias = strToBytes(ASSET_ALIAS_TOKEN);
    const secret = strToBytes(token);

    // 尝试 update（资产已存在时）
    try {
      const query: asset.AssetMap = new Map();
      query.set(asset.Tag.ALIAS, alias);
      const attrsToUpdate: asset.AssetMap = new Map();
      attrsToUpdate.set(asset.Tag.SECRET, secret);
      await asset.update(query, attrsToUpdate);
      return;
    } catch (e) {
      const err = e as BusinessError;
      if (err.code !== 24000002) {
        // 非 "not found" 错误，抛出
        throw e;
      }
      // 24000002 = not found → 需要 add
    }

    // add 新资产
    const attr: asset.AssetMap = new Map();
    attr.set(asset.Tag.SECRET, secret);
    attr.set(asset.Tag.ALIAS, alias);
    attr.set(asset.Tag.ACCESSIBILITY, asset.Accessibility.DEVICE_FIRST_UNLOCKED);
    await asset.add(attr);
  }

  /**
   * 从 Asset Store 读取 deviceToken。
   * 返回空字符串表示未存储或读取失败。
   */
  private async loadTokenFromAsset(): Promise<string> {
    try {
      const query: asset.AssetMap = new Map();
      query.set(asset.Tag.ALIAS, strToBytes(ASSET_ALIAS_TOKEN));
      query.set(asset.Tag.RETURN_TYPE, asset.ReturnType.ALL);
      const results: Array<asset.AssetMap> = await asset.query(query);
      if (results.length === 0) {
        return '';
      }
      const secretBytes = results[0].get(asset.Tag.SECRET) as Uint8Array;
      if (secretBytes === undefined) {
        return '';
      }
      return bytesToStr(secretBytes);
    } catch (e) {
      const err = e as BusinessError;
      if (err.code === 24000002) {
        return ''; // not found，正常情况
      }
      return ''; // 其他错误也返回空，调用方按未注册处理
    }
  }

  /**
   * 从 Asset Store 删除 deviceToken。
   */
  private async removeTokenFromAsset(): Promise<void> {
    try {
      const query: asset.AssetMap = new Map();
      query.set(asset.Tag.ALIAS, strToBytes(ASSET_ALIAS_TOKEN));
      await asset.remove(query);
    } catch (e) {
      const err = e as BusinessError;
      if (err.code !== 24000002) {
        // 非 "not found" 错误，抛出
        throw e;
      }
      // not found 时静默忽略
    }
  }

  // ================= 公共 API（保持外部接口同步语义不变） =================

  private getStr(key: string): string {
    if (this.prefs === undefined) {
      return '';
    }
    const v = this.prefs.getSync(key, '') as string;
    return v === undefined ? '' : v;
  }

  getDeviceId(): string {
    return this.getStr(KEY_DEVICE_ID);
  }

  /**
   * P0-4：返回缓存的 deviceToken。
   * token 在 load() 时从 Asset Store 异步预加载到缓存，
   * save() 时同步更新缓存，因此外部调用无需改为 async。
   */
  getDeviceToken(): string {
    return this.cachedToken;
  }

  getDeviceName(): string {
    return this.getStr(KEY_DEVICE_NAME);
  }

  /**
   * 同步判定是否已注册（基于 load() 时预加载的缓存值）。
   * 外部接口保持不变，无需 await。
   */
  isRegistered(): boolean {
    return this.getDeviceId().length > 0 && this.cachedToken.length > 0;
  }

  /** 生成随机设备名（首次注册用）："HarmonyOS-XXXX" */
  static generateDeviceName(): string {
    const suffix = Math.floor(Math.random() * 0xFFFF).toString(16).toUpperCase().padStart(4, '0');
    return `HarmonyOS-${suffix}`;
  }

  /**
   * P0-4：保存设备身份。
   * deviceId / deviceName → preferences（非敏感）
   * deviceToken → Asset Store（加密存储）
   */
  async save(deviceId: string, deviceToken: string, deviceName: string): Promise<void> {
    if (this.prefs === undefined) {
      throw new Error('DeviceIdentity not loaded');
    }
    this.prefs.putSync(KEY_DEVICE_ID, deviceId);
    this.prefs.putSync(KEY_DEVICE_NAME, deviceName);
    await this.prefs.flush();

    // P0-4：token 存入 Asset Store
    await this.saveTokenToAsset(deviceToken);
    this.cachedToken = deviceToken;
  }

  /**
   * P0-4：清除设备身份（P0-2：401 自愈用）。保留 deviceName 供重注册复用。
   * deviceId 从 preferences 删除，deviceToken 从 Asset Store 删除。
   */
  async clear(): Promise<void> {
    if (this.prefs === undefined) {
      throw new Error('DeviceIdentity not loaded');
    }
    this.prefs.deleteSync(KEY_DEVICE_ID);
    // 清除旧版遗留的明文 token（如果还在）
    this.prefs.deleteSync(KEY_DEVICE_TOKEN);
    await this.prefs.flush();

    // P0-4：从 Asset Store 删除 token
    await this.removeTokenFromAsset();
    this.cachedToken = '';
  }
}