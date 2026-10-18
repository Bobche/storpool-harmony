export const bridgeVersion: () => string;
export const ping: (name: string) => string;
export const encodeHello: (deviceName: string, clientName: string, clientVersion: string) => string;
export const decodeHello: (hex: string) => HelloInfo;
export const sha256Hex: (buf: ArrayBuffer) => Promise<string>;
export const blockRanges: (totalSize: number, blockSize: number) => BlockRange[];

export interface HelloInfo {
  deviceName: string;
  clientName: string;
  clientVersion: string;
  numConnections: number;
  timestamp: number;
}

export interface BlockRange {
  index: number;
  offset: number;
  length: number;
}