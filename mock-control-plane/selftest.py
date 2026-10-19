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
  --- H4 边界用例（依赖 /admin/namespace/add-file）---
  19. 0B 空文件：200 + 0 字节 + sha=e3b0c442…（空输入 sha256 真值）
  20. 1B 文件：Range 0-0 → 206 单字节
  21. 块-1B（131071B）：单片覆盖，开放 Range == 全量
  22. 块+1B（131073B）：末片 1B + 两片重组 == 全量
  23. sha256Override 篡改：元数据 sha ≠ 响应头真实 sha（客户端负向检出依据）
  24. 增量拉取发现全部注入节点（版本推进语义）
  --- register 契约校验负例（round 5，required=[deviceName, platform]）---
  25. 缺失 deviceName（platform 在）→ 400 + error body
  26. 缺失 platform（deviceName 在）→ 400
  27. 空字符串 deviceName → 400
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


# 边界用例基线：注入 0B / 1B / 块-1B / 块+1B / sha 篡改 文件到根目录（id=1）
BLOCK = 131072  # 128KiB，BEP 语义块大小


def add_edge_file(name: str, size: int, sha_override: str = '') -> dict:
    code, r = req('POST', '/admin/namespace/add-file',
                  {'name': name, 'size': size, 'parentId': 1,
                   **({'sha256Override': sha_override} if sha_override else {})})
    assert code == 200, f'add-file {name} 失败: {r}'
    return r


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

    # ---------- H4 边界用例（19-24）：块大小临界 + sha 篡改检出 ----------
    v_edge0 = rbns.get('version', 0)  # 回退后的版本基线，增量用例参照

    # 19. 0B 空文件：全量 200 + 0 字节 + sha=e3b0c442…（空输入 sha256 真值）
    n0 = add_edge_file('edge-0b.bin', 0)
    code, hdr, body0 = req_raw('GET', f'/data/{n0["id"]}', token=tok)
    empty_sha = hashlib.sha256(b'').hexdigest()
    check('0B 空文件全量下载', code == 200 and len(body0) == 0
          and (hdr.get('X-Content-Sha256') or hdr.get('x-content-sha256')) == empty_sha
          and n0['sha256'] == empty_sha,
          f'id={n0["id"]} sha={empty_sha[:12]}…')

    # 20. 1B 文件：Range bytes=0-0 → 206 单字节
    n1 = add_edge_file('edge-1b.bin', 1)
    code, hdr, body1 = req_raw('GET', f'/data/{n1["id"]}', token=tok,
                               range_header='bytes=0-0')
    check('1B 文件 Range 0-0 → 206', code == 206 and len(body1) == 1
          and hdr.get('Content-Range') == f'bytes 0-0/1',
          f'{len(body1)}B Content-Range={hdr.get("Content-Range")}')

    # 21. 块-1B（131071）：单片覆盖，开放 Range → 206 全量
    n2 = add_edge_file('edge-block-minus1.bin', BLOCK - 1)
    code, hdr, body2 = req_raw('GET', f'/data/{n2["id"]}', token=tok,
                               range_header='bytes=0-')
    check('块-1B 开放 Range → 206 全量', code == 206 and len(body2) == BLOCK - 1
          and body2 == req_raw('GET', f'/data/{n2["id"]}', token=tok)[2],
          f'{len(body2)}B')

    # 22. 块+1B（131073）：末片 1B（Range 131072-）+ 两片重组 == 全量
    n3 = add_edge_file('edge-block-plus1.bin', BLOCK + 1)
    code, hdr, tail = req_raw('GET', f'/data/{n3["id"]}', token=tok,
                              range_header=f'bytes={BLOCK}-')
    _, _, head = req_raw('GET', f'/data/{n3["id"]}', token=tok,
                         range_header=f'bytes=0-{BLOCK - 1}')
    code_full, _, full3 = req_raw('GET', f'/data/{n3["id"]}', token=tok)
    check('块+1B 末片 1B + 重组 == 全量',
          code == 206 and len(tail) == 1 and head + tail == full3
          and hdr.get('Content-Range') == f'bytes {BLOCK}-{BLOCK}/131073',
          f'{len(head)}+{len(tail)}B')

    # 23. sha256Override 篡改：元数据 sha ≠ /data 响应头真实 sha（客户端检出依据）
    fake_sha = 'f' * 64
    n4 = add_edge_file('edge-corrupt.bin', 4096, sha_override=fake_sha)
    code, hdr, _ = req_raw('GET', f'/data/{n4["id"]}', token=tok)
    real_sha = hdr.get('X-Content-Sha256') or hdr.get('x-content-sha256') or ''
    check('sha 篡改节点：元数据与响应头 sha 分离',
          code == 200 and n4['sha256'] == fake_sha and real_sha != fake_sha
          and len(real_sha) == 64,
          f'元数据={fake_sha[:8]}… 响应头={real_sha[:8]}…')

    # 24. 增量拉取发现全部注入节点（版本推进语义）
    # 注意基线：用例 9 已回退版本到 v2，since=v2 的增量必然包含墓碑与 v3 旧节点，
    # 因此断言注入的 5 个节点"都在"其中，且各带正确 version 字段（> v_edge0）
    code, inc2 = req('GET', f'/namespace?since={v_edge0}', token=tok)
    inc_nodes = {n['id']: n for n in inc2.get('nodes', [])}
    injected = [n0['id'], n1['id'], n2['id'], n3['id'], n4['id']]
    all_found = all(i in inc_nodes for i in injected)
    versions_ok = all(not inc_nodes[i].get('deleted')
                      and inc_nodes[i]['version'] > v_edge0 for i in injected if i in inc_nodes)
    check('增量拉取发现 5 个注入节点', code == 200 and all_found and versions_ok,
          f'found={sorted(i for i in injected if i in inc_nodes)} '
          f'versions_ok={versions_ok}')

    # ---------- register 契约校验负例（round 5）：required=[deviceName, platform] ----------
    # 25. 缺失 deviceName（platform 在）→ 400 + error body（ErrorResponse 语义）
    code, err = req('POST', '/devices/register', {'platform': 'pytest'})
    check('缺 deviceName → 400 + error body', code == 400
          and isinstance(err.get('error'), str) and 'deviceName' in err.get('error', ''),
          f"status={code} error={err.get('error', '')[:40]}")

    # 26. 缺失 platform（deviceName 在）→ 400
    code, err = req('POST', '/devices/register', {'deviceName': 'selftest-neg'})
    check('缺 platform → 400', code == 400
          and isinstance(err.get('error'), str) and 'platform' in err.get('error', ''),
          f"status={code} error={err.get('error', '')[:40]}")

    # 27. 空字符串 deviceName → 400（契约 type: string 且非空）
    code, err = req('POST', '/devices/register',
                    {'deviceName': '   ', 'platform': 'pytest'})
    check('空 deviceName → 400', code == 400,
          f"status={code} error={err.get('error', '')[:40]}")

    print(f'\n结果：{len(passed)} passed, {len(failed)} failed')
    sys.exit(0 if not failed else 1)


if __name__ == '__main__':
    main()