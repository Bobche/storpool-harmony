export const bridgeVersion: () => string;
export const ping: (name: string) => string;
export const encodeHello: (deviceName: string, clientName: string, clientVersion: string) => string;
export const decodeHello: (hex: string) => HelloInfo;

export interface HelloInfo {
  deviceName: string;
  clientName: string;
  clientVersion: string;
  numConnections: number;
  timestamp: number;
}