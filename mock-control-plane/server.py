#!/usr/bin/env python3
"""StorPool mock 控制面 — H1 设备注册/心跳 + H2 元数据同步联调用

对应规划中的控制面 API（正式实现为 Go 侧另仓库，本 mock 覆盖 H1/H2 所需端点）：
  POST /devices/register   {deviceName, platform}          -> {deviceId, deviceToken, serverTime}
  POST /devices/heartbeat  {deviceId} (Bearer deviceToken) -> {status, serverTime}
  GET  /devices            -> 全量设备快照（联调观测用）
  GET  /namespace[?since=N] (Bearer deviceToken)           -> {version, nodes}  H2 元数据

用法：python server.py [port]   （默认 8765）
"""
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEVICES: dict = {}
LOCK = threading.Lock()
TOKENS: dict = {}  # token -> deviceId


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------- H2：虚拟目录树（种子数据） ----------------
# 节点结构：id / parentId / name / type(dir|file) / size(字节) / mtime / version
# version 单调递增；增量拉取 = version > since 的节点
NAMESPACE_VERSION = 3
_T0 = 1792200000000
NAMESPACE_NODES: list = [
    {'id': 1, 'parentId': 0, 'name': '', 'type': 'dir', 'size': 0, 'mtime': _T0, 'version': 1},
    # /photos
    {'id': 2, 'parentId': 1, 'name': 'photos', 'type': 'dir', 'size': 0, 'mtime': _T0, 'version': 1},
    {'id': 3, 'parentId': 2, 'name': 'cat.jpg', 'type': 'file', 'size': 2516582, 'mtime': _T0 + 3600000, 'version': 2},
    {'id': 4, 'parentId': 2, 'name': 'vacation', 'type': 'dir', 'size': 0, 'mtime': _T0, 'version': 1},
    {'id': 5, 'parentId': 4, 'name': 'beach.jpg', 'type': 'file', 'size': 5347737, 'mtime': _T0 + 7200000, 'version': 2},
    {'id': 6, 'parentId': 4, 'name': 'sunset.jpg', 'type': 'file', 'size': 3145728, 'mtime': _T0 + 7300000, 'version': 3},
    # /documents
    {'id': 7, 'parentId': 1, 'name': 'documents', 'type': 'dir', 'size': 0, 'mtime': _T0, 'version': 1},
    {'id': 8, 'parentId': 7, 'name': 'report.pdf', 'type': 'file', 'size': 819200, 'mtime': _T0 + 1800000, 'version': 2},
    {'id': 9, 'parentId': 7, 'name': 'notes.md', 'type': 'file', 'size': 12288, 'mtime': _T0 + 9000000, 'version': 3},
    # /backup（演示一个空目录）
    {'id': 10, 'parentId': 1, 'name': 'backup', 'type': 'dir', 'size': 0, 'mtime': _T0, 'version': 3},
]


class Handler(BaseHTTPRequestHandler):
    server_version = 'StorPoolMockCP/0.1'

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length > 0 else b'{}'
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _auth_device(self) -> str | None:
        """校验 Bearer token，返回 deviceId；失败返回 None（不写响应，由调用方决定）"""
        auth = self.headers.get('Authorization', '')
        token = auth.removeprefix('Bearer ').strip()
        with LOCK:
            return TOKENS.get(token)

    def do_POST(self) -> None:  # noqa: N802
        payload = self._read_body()
        if self.path == '/devices/register':
            device_id = 'dev-' + uuid.uuid4().hex[:12]
            token = 'tok-' + uuid.uuid4().hex[:16]
            with LOCK:
                DEVICES[device_id] = {
                    'deviceName': str(payload.get('deviceName', 'unknown')),
                    'platform': str(payload.get('platform', 'unknown')),
                    'capabilities': payload.get('capabilities', []),
                    'token': token,
                    'registeredAt': now_ms(),
                    'lastHeartbeat': None,
                    'heartbeatCount': 0,
                }
                TOKENS[token] = device_id
            print(f'[register] {device_id} name={payload.get("deviceName")} '
                  f'platform={payload.get("platform")}')
            self._json(200, {
                'deviceId': device_id,
                'deviceToken': token,
                'serverTime': now_ms(),
            })
            return

        if self.path == '/devices/heartbeat':
            auth = self.headers.get('Authorization', '')
            token = auth.removeprefix('Bearer ').strip()
            device_id = str(payload.get('deviceId', ''))
            with LOCK:
                dev = DEVICES.get(device_id)
                if dev is None or TOKENS.get(token) != device_id:
                    self._json(401, {'error': 'unauthorized or unknown device',
                                     'serverTime': now_ms()})
                    return
                dev['lastHeartbeat'] = now_ms()
                dev['heartbeatCount'] += 1
                count = dev['heartbeatCount']
            print(f'[heartbeat] {device_id} #{count}')
            self._json(200, {'status': 'ok', 'serverTime': now_ms()})
            return

        self._json(404, {'error': 'not found'})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == '/devices':
            with LOCK:
                snapshot = {
                    k: {
                        'deviceName': v['deviceName'],
                        'platform': v['platform'],
                        'registeredAt': v['registeredAt'],
                        'lastHeartbeat': v['lastHeartbeat'],
                        'heartbeatCount': v['heartbeatCount'],
                    }
                    for k, v in DEVICES.items()
                }
            self._json(200, snapshot)
            return
        if parsed.path == '/namespace':
            device_id = self._auth_device()
            if device_id is None:
                self._json(401, {'error': 'unauthorized', 'serverTime': now_ms()})
                return
            qs = parse_qs(parsed.query)
            since = int(qs.get('since', ['0'])[0])
            with LOCK:
                if since <= 0:
                    nodes = [dict(n) for n in NAMESPACE_NODES]
                    mode = 'full'
                else:
                    nodes = [dict(n) for n in NAMESPACE_NODES if n['version'] > since]
                    mode = 'incremental'
                version = NAMESPACE_VERSION
            print(f'[namespace] {device_id} mode={mode} since={since} '
                  f'-> {len(nodes)} nodes, version={version}')
            self._json(200, {'version': version, 'mode': mode, 'nodes': nodes})
            return
        if parsed.path == '/health':
            self._json(200, {'status': 'ok', 'devices': len(DEVICES),
                             'namespaceVersion': NAMESPACE_VERSION})
            return
        self._json(404, {'error': 'not found'})

    def log_message(self, fmt: str, *args) -> None:
        pass  # 自定义 print 已覆盖关键日志，抑制默认访问日志噪音


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'StorPool mock control plane listening on 0.0.0.0:{port}')
    print('endpoints: POST /devices/register | POST /devices/heartbeat | '
          'GET /devices | GET /namespace[?since=N] | GET /health')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nbye')


if __name__ == '__main__':
    main()