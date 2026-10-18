#!/usr/bin/env python3
"""StorPool mock 控制面 — H1 设备注册/心跳 + H2 元数据同步联调用

对应规划中的控制面 API（正式实现为 Go 侧另仓库，本 mock 覆盖 H1/H2 所需端点）：
  POST /devices/register   {deviceName, platform}          -> {deviceId, deviceToken, serverTime}
  POST /devices/heartbeat  {deviceId} (Bearer deviceToken) -> {status, serverTime}
  GET  /devices            -> 全量设备快照（联调观测用）
  GET  /namespace[?since=N] (Bearer deviceToken)           -> {version, mode, nodes}  H2 元数据
  GET  /data/<nodeId>       (Bearer deviceToken)           -> 文件内容（H4 分片下载，支持 Range）
       无 Range 全量 200；Range: bytes=start-end -> 206 Partial Content（end 可空=到文件尾）
       响应头：ETag / X-Content-Sha256（全文件 sha256）/ Content-Range / Accept-Ranges
       file 节点的 /namespace JSON 亦携带 sha256 字段（客户端从元数据取期望校验值）
   POST /admin/namespace/delete   {id}                      -> 节点墓碑（级联子树）+ 版本推进（P0-1 删除语义用例）
   POST /admin/namespace/rollback {version}                 -> 服务端版本回退（模拟重置，P0-1 游标保护用例）
   POST /admin/namespace/add-file {name, size, parentId?, sha256Override?}
        -> 注入边界测试文件（0B/1B/块±1B 等），版本推进；sha256Override 可伪造元数据
           sha（客户端下载后必须检出不匹配，负向用例专用）

P0-1 协议语义（Go 控制面对齐基线）：
  - 删除：tombstone 模式——节点标记 deleted=true 并推进全局版本；增量拉取(version>since)
    携带墓碑，客户端负责本地删除；全量拉取只返回活节点（重拉无需墓碑）。
  - 版本回退：客户端须校验 resp.version >= 已同步游标，回退时清库全量重拉，
    禁止直接把游标写回退值（否则数据永久陈旧且无重拉触发）。

用法：python server.py [port]   （默认 8765）
"""
import hashlib
import json
import re
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


def add_file_node(name: str, size: int, parent_id: int = 1,
                  sha_override: str = '') -> dict:
    """H4 边界用例：注入任意大小的虚拟文件节点（版本推进，增量可发现）。

    sha_override 非空时元数据 sha256 用伪造值（/data 响应头仍返回真实值），
    供客户端"sha 不匹配必须报错"的负向用例检出。
    """
    global NAMESPACE_VERSION
    with LOCK:
        parent = next((n for n in NAMESPACE_NODES
                       if n['id'] == parent_id and n['type'] == 'dir'
                       and not n.get('deleted')), None)
        if parent is None:
            return {'error': f'parent dir {parent_id} not found'}
        new_id = max(n['id'] for n in NAMESPACE_NODES) + 1
        NAMESPACE_VERSION += 1
        node = {'id': new_id, 'parentId': parent_id, 'name': name, 'type': 'file',
                'size': size, 'mtime': now_ms(), 'version': NAMESPACE_VERSION}
        node['sha256'] = sha_override if sha_override else file_sha(node)
        NAMESPACE_NODES.append(node)
        return {'id': new_id, 'version': NAMESPACE_VERSION, 'size': size,
                'sha256': node['sha256']}


# ---------------- H4：文件数据源（确定性生成 + sha256） ----------------
DATA_CACHE: dict = {}  # node_id -> bytes（懒生成，进程内缓存）
SHA_CACHE: dict = {}   # node_id -> sha256 hex
SEED_BLOCK_LEN = 4096


def file_data(node: dict) -> bytes:
    """H4：确定性文件内容——4KiB 种子块（sha256(node id+name) 派生）平铺到 size。

    跨重启内容不变，客户端可对任意分片做 sha256 校验。
    """
    nid = node['id']
    if nid not in DATA_CACHE:
        seed = hashlib.sha256(f'storpool-seed-{nid}-{node["name"]}'.encode()).digest()
        block4k = seed * (SEED_BLOCK_LEN // len(seed))  # 32B × 128 = 4096B
        n_blocks = (node['size'] + SEED_BLOCK_LEN - 1) // SEED_BLOCK_LEN
        DATA_CACHE[nid] = (block4k * n_blocks)[:node['size']]
    return DATA_CACHE[nid]


def file_sha(node: dict) -> str:
    nid = node['id']
    if nid not in SHA_CACHE:
        SHA_CACHE[nid] = hashlib.sha256(file_data(node)).hexdigest()
    return SHA_CACHE[nid]


# 启动即为全部 file 节点补齐 sha256 元数据字段（/namespace 与 /data 共用真值）
for _n in NAMESPACE_NODES:
    if _n['type'] == 'file':
        _n['sha256'] = file_sha(_n)


class Handler(BaseHTTPRequestHandler):
    server_version = 'StorPoolMockCP/0.2'

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, code: int, body: bytes, extra_headers: dict) -> None:
        """H4：二进制响应（/data 端点），自动带 Accept-Ranges 与 Content-Length"""
        self.send_response(code)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Accept-Ranges', 'bytes')
        for k, v in extra_headers.items():
            self.send_header(k, v)
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

        if self.path == '/admin/namespace/add-file':
            name = str(payload.get('name', ''))
            size = int(payload.get('size', -1))
            parent_id = int(payload.get('parentId', 1))
            sha_override = str(payload.get('sha256Override', ''))
            if not name or size < 0:
                self._json(400, {'error': 'name (non-empty) and size (>=0) required',
                                 'serverTime': now_ms()})
                return
            result = add_file_node(name, size, parent_id, sha_override)
            if 'error' in result:
                self._json(404, {**result, 'serverTime': now_ms()})
                return
            print(f'[admin] add-file {name} id={result["id"]} '
                  f'size={size} parent={parent_id}'
                  + (' [sha OVERRIDE]' if sha_override else ''))
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
        m = re.fullmatch(r'/data/(\d+)', parsed.path)
        if m:
            # H4：分片下载端点（无 Range 全量 200；Range: bytes=s-e -> 206）
            device_id = self._auth_device()
            if device_id is None:
                self._json(401, {'error': 'unauthorized', 'serverTime': now_ms()})
                return
            node_id = int(m.group(1))
            with LOCK:
                node = next((n for n in NAMESPACE_NODES if n['id'] == node_id), None)
                if node is None or node.get('deleted'):
                    self._json(404, {'error': f'node {node_id} not found'})
                    return
                if node['type'] != 'file':
                    self._json(400, {'error': f'node {node_id} is not a file'})
                    return
                data = file_data(node)
                sha = file_sha(node)
            total = len(data)
            rng = self.headers.get('Range', '').strip()
            mm = re.fullmatch(r'bytes=(\d+)-(\d*)', rng)
            if mm:
                start = int(mm.group(1))
                end = int(mm.group(2)) if mm.group(2) else total - 1
                end = min(end, total - 1)
                if start >= total:
                    self._bytes(416, b'', {'Content-Range': f'bytes */{total}',
                                           'X-Content-Sha256': sha})
                    return
                if end >= start:
                    chunk = data[start:end + 1]
                    self._bytes(206, chunk, {
                        'Content-Range': f'bytes {start}-{end}/{total}',
                        'ETag': f'"{sha}"',
                        'X-Content-Sha256': sha,
                    })
                    print(f'[data] {device_id} node={node_id} '
                          f'range={start}-{end} ({len(chunk)}B, 206)')
                    return
            # 无 Range / 无法解析的 Range：全量 200
            self._bytes(200, data, {'ETag': f'"{sha}"', 'X-Content-Sha256': sha})
            print(f'[data] {device_id} node={node_id} full ({total}B, 200)')
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
          'GET /devices | GET /namespace[?since=N] | GET /data/<id>[Range] | '
          'GET /health | POST /admin/namespace/delete | POST /admin/namespace/rollback | '
          'POST /admin/namespace/add-file')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nbye')


if __name__ == '__main__':
    main()