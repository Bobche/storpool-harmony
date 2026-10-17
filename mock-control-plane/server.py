#!/usr/bin/env python3
"""StorPool mock 控制面 — H1 设备注册/心跳 + H2 元数据同步联调用

对应规划中的控制面 API（正式实现为 Go 侧另仓库，本 mock 覆盖 H1/H2 所需端点）：
  POST /devices/register   {deviceName, platform}          -> {deviceId, deviceToken, serverTime}
  POST /devices/heartbeat  {deviceId} (Bearer deviceToken) -> {status, serverTime}
  GET  /devices            -> 全量设备快照（联调观测用）
  GET  /namespace[?since=N] (Bearer deviceToken)           -> {version, mode, nodes}  H2 元数据
  POST /admin/namespace/delete   {id}                      -> 节点墓碑（级联子树）+ 版本推进（P0-1 删除语义用例）
  POST /admin/namespace/rollback {version}                 -> 服务端版本回退（模拟重置，P0-1 游标保护用例）

P0-1 协议语义（Go 控制面对齐基线）：
  - 删除：tombstone 模式——节点标记 deleted=true 并推进全局版本；增量拉取(version>since)
    携带墓碑，客户端负责本地删除；全量拉取只返回活节点（重拉无需墓碑）。
  - 版本回退：客户端须校验 resp.version >= 已同步游标，回退时清库全量重拉，
    禁止直接把游标写回退值（否则数据永久陈旧且无重拉触发）。

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
#           / deleted(可选，tombstone 标记)
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


def tombstone_node(node_id: int) -> dict | None:
    """P0-1a：节点墓碑（级联子树，parentId 链 BFS 收集后代）。

    推进全局版本一次，所有受影响节点绑定同一版本，确保一次增量拉取全部命中。
    """
    global NAMESPACE_VERSION
    with LOCK:
        target = next((n for n in NAMESPACE_NODES if n['id'] == node_id), None)
        if target is None:
            return None
        affected = [target]
        affected_ids = {target['id']}
        frontier = [target['id']]
        while frontier:
            pid = frontier.pop()
            for n in NAMESPACE_NODES:
                if (n['parentId'] == pid and n['id'] not in affected_ids
                        and not n.get('deleted')):
                    affected.append(n)
                    affected_ids.add(n['id'])
                    frontier.append(n['id'])
        NAMESPACE_VERSION += 1
        for n in affected:
            n['deleted'] = True
            n['version'] = NAMESPACE_VERSION
            n['mtime'] = now_ms()
        return {'version': NAMESPACE_VERSION, 'tombstoned': sorted(affected_ids)}


def rollback_namespace(target_version: int) -> dict:
    """P0-1b：服务端版本回退（模拟重置/降级后全局版本倒退）。

    客户端下次增量拉取将看到 resp.version < since，须触发全量重拉。
    """
    global NAMESPACE_VERSION
    with LOCK:
        previous = NAMESPACE_VERSION
        NAMESPACE_VERSION = target_version
        return {'version': NAMESPACE_VERSION, 'previous': previous}


class Handler(BaseHTTPRequestHandler):
    server_version = 'StorPoolMockCP/0.2'

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

        if self.path == '/admin/namespace/delete':
            node_id = int(payload.get('id', 0))
            result = tombstone_node(node_id)
            if result is None:
                self._json(404, {'error': f'node {node_id} not found',
                                 'serverTime': now_ms()})
                return
            print(f'[admin] tombstone node {node_id} -> {result}')
            self._json(200, {'ok': True, 'serverTime': now_ms(), **result})
            return

        if self.path == '/admin/namespace/rollback':
            target_version = int(payload.get('version', 0))
            result = rollback_namespace(target_version)
            print(f'[admin] rollback namespace version '
                  f'{result["previous"]} -> {result["version"]}')
            self._json(200, {'ok': True, 'serverTime': now_ms(), **result})
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
                    # 全量只返回活节点（重拉不需要墓碑）
                    nodes = [dict(n) for n in NAMESPACE_NODES if not n.get('deleted')]
                    mode = 'full'
                else:
                    # 增量返回活节点 + 墓碑（deleted=true），客户端负责本地删除
                    nodes = [dict(n) for n in NAMESPACE_NODES if n['version'] > since]
                    mode = 'incremental'
                version = NAMESPACE_VERSION
            print(f'[namespace] {device_id} mode={mode} since={since} '
                  f'-> {len(nodes)} nodes, version={version}')
            self._json(200, {'version': version, 'mode': mode, 'nodes': nodes})
            return
        if parsed.path == '/health':
            with LOCK:
                tombs = sum(1 for n in NAMESPACE_NODES if n.get('deleted'))
            self._json(200, {'status': 'ok', 'devices': len(DEVICES),
                             'namespaceVersion': NAMESPACE_VERSION,
                             'tombstones': tombs})
            return
        self._json(404, {'error': 'not found'})

    def log_message(self, fmt: str, *args) -> None:
        pass  # 自定义 print 已覆盖关键日志，抑制默认访问日志噪音


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'StorPool mock control plane listening on 0.0.0.0:{port}')
    print('endpoints: POST /devices/register | POST /devices/heartbeat | '
          'GET /devices | GET /namespace[?since=N] | GET /health | '
          'POST /admin/namespace/delete | POST /admin/namespace/rollback')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nbye')


if __name__ == '__main__':
    main()