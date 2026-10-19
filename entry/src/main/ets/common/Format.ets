/**
 * P1-7：格式化工具（原 Index.fmtTime / NamespaceBrowser.fmtSize 归位，逻辑逐字等价）
 */

/** 毫秒时间戳 → HH:MM:SS（本地时区） */
export function fmtTime(ms: number): string {
  const d = new Date(ms);
  const p = (n: number): string => n.toString().padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 字节数 → 人类可读（B / KB / MB，保留 1 位小数） */
export function fmtSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1048576) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / 1048576).toFixed(1)} MB`;
}