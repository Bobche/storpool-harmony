#!/usr/bin/env python3
"""P0-1/P0-2 mock 端点自检（tombstone 删除语义 + 版本回退用例）

用法：python selftest.py [port]   （默认 8765，须先启动 server.py）

用例清单：
  1. /health 可达（含 tombstones 计数字段）
  2. 注册成功拿 token
  3. 全量拉取（since=0，10 节点）
  4. 增量无新变更（since=当前版本 → 0 节点）
  5. tombstone 单节点（beach.jpg）
  6. 增量拉取携带墓碑（deleted=true）
  7. 级联墓碑（photos 目录 → 子树 3/4/6，已删的 5 跳过）
  8. 全量重拉只含活节点（墓碑不下发）
  9. 版本回退（rollback 到 v2）
  10. 回退后 resp.version < since（客户端须全量重拉的保护判定依据）
  11. 无 token 返回 401（P0-2 自愈判定依据）
  12. H4 全量下载 report.pdf（200 + X-Content-Sha256 与本地 sha256 一致 + 与 /namespace 元数据一致）
  13. Range 0-1023 → 206 + Content-Range + 片段内容正确
  14. 开放 Range（start-）→ 206 到文件尾
  15. 分片重组（0-65535 + 65536-131071）== 全量前 128KiB（H4 分片下载正确性）
  16. 越界 Range → 416
  17. 目录节点 → 400
  18. 已删除节点（tombstone）→ 404
"""
import hashlib
import json
import sys
import urllib.error
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
BASE = 'http://127.0.0.1:' + str(PORT)

passed = []
failed = []


def check(name: str, cond: bool, detail: str = '') -> None:
    (passed if cond else failed).append(name)
    mark = 'PASS' if cond else 'FAIL'
    print(f'[{mark}] {name} {detail}')


def req(method: str, path: str, body: dict = None, token: str = '') -> tuple:
    url = BASE + path
    data = json.dumps(body).encode('utf-8') if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header('Content-Type', 'application/json')
    if token:
        r.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def req_raw(method: str, path: str, body: dict = None,
            token: str = '', range_header: str = '') -> tuple:
    """H4：二进制请求（/data 端点），返回 (status, headers, bytes)"""
    url = BASE + path
    data = json.dumps(body).encode('utf-8') if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if token:
        r.add_header('Authorization', 'Bearer ' + token)
    if range_header:
        r.add_header('Range', range_header)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def main() -> None:
    # 1. health 基线
    code, h = req('GET', '/health')
    check('health 可达', code == 200,
          f"v={h.get('namespaceVersion')} tombs={h.get('tombstones')}")

    # 2. 注册
    code, reg = req('POST', '/devices/register',
                    {'deviceName': 'selftest', 'platform': 'pytest'})
    check('注册成功', code == 200 and 'deviceToken' in reg)
    tok = reg.get('deviceToken', '')

    # 3. 全量
    code, ns = req('GET', '/namespace?since=0', token=tok)
    base_count = len(ns.get('nodes', []))
    v0 = ns.get('version', 0)
    check('全量拉取 10 节点', code == 200 and ns.get('mode') == 'full'
          and base_count == 10, f'{base_count} 节点 v{v0}')

    # 4. 增量无新变更
    code, ns2 = req(f'GET', f'/namespace?since={v0}', token=tok)
    check('增量无新变更', code == 200 and len(ns2.get('nodes', [])) == 0, '0 节点')

    # 5. tombstone 单节点（id=5 beach.jpg，无子节点）
    code, d = req('POST', '/admin/namespace/delete', {'id': 5})
    check('删除 beach.jpg（单节点墓碑）', code == 200 and d.get('tombstoned') == [5],
          f"v{d.get('version')} tombstoned={d.get('tombstoned')}")

    # 6. 增量拉取携带墓碑
    code, inc = req('GET', f'/namespace?since={v0}', token=tok)
    tombs = [n for n in inc.get('nodes', []) if n.get('deleted') is True]
    check('增量拉取携带墓碑', code == 200 and len(tombs) == 1 and tombs[0]['id'] == 5,
          f"{len(inc.get('nodes', []))} 节点（含 {len(tombs)} 墓碑）")

    # 7. 级联墓碑（id=2 photos → 3,4,6；5 已是墓碑跳过）
    code, d2 = req('POST', '/admin/namespace/delete', {'id': 2})
    check('级联删除 photos 子树', code == 200 and d2.get('tombstoned') == [2, 3, 4, 6],
          f"v{d2.get('version')} tombstoned={d2.get('tombstoned')}")

    # 8. 全量重拉只含活节点
    code, full2 = req('GET', '/namespace?since=0', token=tok)
    alive = sorted(n['id'] for n in full2.get('nodes', []))
    check('全量重拉只含活节点', code == 200 and alive == [1, 7, 8, 9, 10],
          f"{len(full2.get('nodes', []))} 节点 {alive}")

    # 9. 版本回退
    v_before = d2.get('version', 0)
    code, rb = req('POST', '/admin/namespace/rollback', {'version': 2})
    check('版本回退到 v2', code == 200 and rb.get('version') == 2
          and rb.get('previous') == v_before,
          f"{rb.get('previous')}→{rb.get('version')}")

    # 10. 回退后 resp.version < since（客户端全量重拉的判定依据）
    code, rbns = req('GET', f'/namespace?since={v_before}', token=tok)
    check('回退后 resp.version < since', code == 200
          and rbns.get('version', 0) < v_before,
          f"v{rbns.get('version')} < since={v_before}（客户端须全量重拉）")

    # 11. 无 token → 401
    code, _ = req('GET', '/namespace?since=0')
    check('无 token 返回 401', code == 401)

    # ---------- H4：/data 分片下载 ----------
    # 12. 全量下载 report.pdf（id=8，819200B）：sha256 三方一致（本地算/响应头/元数据）
    code, hdr, body = req_raw('GET', '/data/8', token=tok)
    local_sha = hashlib.sha256(body).hexdigest()
    hdr_sha = (hdr.get('X-Content-Sha256') or hdr.get('x-content-sha256') or '')
    meta_sha = next((n.get('sha256', '') for n in full2.get('nodes', []) if n['id'] == 8), '')
    check('全量下载 200 + sha256 三方一致', code == 200 and len(body) == 819200
          and local_sha == hdr_sha == meta_sha,
          f'{len(body)}B sha={local_sha[:12]}…')

    # 13. Range 0-1023 → 206 片段正确
    code, hdr, piece = req_raw('GET', '/data/8', token=tok, range_header='bytes=0-1023')
    check('Range 0-1023 → 206 片段正确', code == 206 and len(piece) == 1024
          and piece == body[:1024]
          and hdr.get('Content-Range') == 'bytes 0-1023/819200',
          f'Content-Range={hdr.get("Content-Range")}')

    # 14. 开放 Range（131072-）→ 206 到文件尾
    code, hdr, tail = req_raw('GET', '/data/8', token=tok, range_header='bytes=131072-')
    check('开放 Range → 206 到文件尾', code == 206
          and len(tail) == 819200 - 131072 and tail == body[131072:],
          f'{len(tail)}B')

    # 15. 分片重组 == 全量前 128KiB（H4 分片下载核心语义）
    _, _, p1 = req_raw('GET', '/data/8', token=tok, range_header='bytes=0-65535')
    _, _, p2 = req_raw('GET', '/data/8', token=tok, range_header='bytes=65536-131071')
    check('分片重组 == 全量前 128KiB', p1 + p2 == body[:131072],
          f'{len(p1)}+{len(p2)}B')

    # 16. 越界 Range → 416
    code, hdr, _ = req_raw('GET', '/data/8', token=tok, range_header='bytes=99999999-')
    check('越界 Range → 416', code == 416,
          f'Content-Range={hdr.get("Content-Range")}')

    # 17. 目录节点 → 400
    code, _, _ = req_raw('GET', '/data/1', token=tok)
    check('目录节点 → 400', code == 400)

    # 18. 已删除节点（id=3 已被级联墓碑）→ 404
    code, _, _ = req_raw('GET', '/data/3', token=tok)
    check('已删除节点 → 404', code == 404)

    print(f'\n结果：{len(passed)} passed, {len(failed)} failed')
    sys.exit(0 if not failed else 1)


if __name__ == '__main__':
    main()