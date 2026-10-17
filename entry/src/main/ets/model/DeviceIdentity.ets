import { preferences } from '@kit.ArkData';
import { common } from '@kit.AbilityKit';

/**
 * 设备身份管理（H1）
 *
 * deviceId / deviceToken / deviceName 持久化在 preferences，
 * 首次注册后稳定复用，应用重启后无需重新注册。
 */
const PREF_NAME = 'storpool_device';
const KEY_DEVICE_ID = 'deviceId';
const KEY_DEVICE_TOKEN = 'deviceToken';
const KEY_DEVICE_NAME = 'deviceName';

export class DeviceIdentity {
  private prefs?: preferences.Preferences;

  async load(context: common.UIAbilityContext): Promise<void> {
    this.prefs = await preferences.getPreferences(context, { name: PREF_NAME });
  }

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

  getDeviceToken(): string {
    return this.getStr(KEY_DEVICE_TOKEN);
  }

  getDeviceName(): string {
    return this.getStr(KEY_DEVICE_NAME);
  }

  isRegistered(): boolean {
    return this.getDeviceId().length > 0 && this.getDeviceToken().length > 0;
  }

  /** 生成随机设备名（首次注册用）："HarmonyOS-XXXX" */
  static generateDeviceName(): string {
    const suffix = Math.floor(Math.random() * 0xFFFF).toString(16).toUpperCase().padStart(4, '0');
    return `HarmonyOS-${suffix}`;
  }

  async save(deviceId: string, deviceToken: string, deviceName: string): Promise<void> {
    if (this.prefs === undefined) {
      throw new Error('DeviceIdentity not loaded');
    }
    this.prefs.putSync(KEY_DEVICE_ID, deviceId);
    this.prefs.putSync(KEY_DEVICE_TOKEN, deviceToken);
    this.prefs.putSync(KEY_DEVICE_NAME, deviceName);
    await this.prefs.flush();
  }
}