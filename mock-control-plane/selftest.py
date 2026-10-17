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
"""
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

    print(f'\n结果：{len(passed)} passed, {len(failed)} failed')
    sys.exit(0 if not failed else 1)


if __name__ == '__main__':
    main()