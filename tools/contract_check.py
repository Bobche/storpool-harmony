#!/usr/bin/env python3
"""StorPool OpenAPI 契约一致性检查器

以 docs/openapi.yaml 为单一事实源，对 mock-control-plane/server.py 逐端点验证：
- 正例：状态码 + 响应必需字段/类型
- 负例：错误 token→401、缺失必填字段→4xx、namespace 版本回退语义

用法：python tools/contract_check.py
退出码：0=全过，非0=有FAIL

Python 3.12 stdlib only（无 PyYAML——openapi.yaml 结构固定，用缩进/正则子集解析）
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
OPENAPI_PATH = os.path.join(PROJECT_ROOT, "docs", "openapi.yaml")
MOCK_SERVER_PATH = os.path.join(PROJECT_ROOT, "mock-control-plane", "server.py")

# ---- 结果收集 ----
_results = []


def record(name, passed, detail=""):
    _results.append((name, passed, detail))
    tag = "[PASS]" if passed else "[FAIL]"
    print(f"  {tag} {name}" + (f" -- {detail}" if detail else ""))


# ---- OpenAPI 解析（stdlib only，无 PyYAML） ----
def load_openapi():
    """从 openapi.yaml 提取端点契约。

    使用缩进+正则子集解析，不依赖 PyYAML。
    解析不了的字段记录为"不校验"并注明。
    """
    with open(OPENAPI_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # 1. 提取 components/schemas 下各 schema 的 required 字段
    schemas_required = {}  # schema_name -> [field_names]
    in_schemas = False
    current_schema = None
    in_required_list = False
    schema_base_indent = 0

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if stripped == "schemas:":
            in_schemas = True
            schema_base_indent = indent
            continue

        if not in_schemas:
            continue

        # 新 schema 定义（如 RegisterRequest:）
        if indent == schema_base_indent + 2 and stripped.endswith(":") and not stripped.startswith("#"):
            current_schema = stripped.rstrip(":")
            schemas_required[current_schema] = []
            in_required_list = False
            continue

        if current_schema and indent >= schema_base_indent + 4:
            # required 字段（行内格式：required: [a, b]）
            if stripped.startswith("required:"):
                inline = stripped[len("required:"):].strip()
                if inline.startswith("["):
                    fields = [f.strip().strip("'\"") for f in inline.strip("[]").split(",") if f.strip()]
                    schemas_required[current_schema] = fields
                    in_required_list = False
                else:
                    in_required_list = True
                continue

            # required 字段（多行格式：- field）
            if in_required_list and stripped.startswith("-"):
                field = stripped.lstrip("- ").strip("'\"")
                schemas_required[current_schema].append(field)
                continue

            if in_required_list and not stripped.startswith("-") and not stripped.startswith("#"):
                in_required_list = False

        # 离开 schemas 段
        if indent <= schema_base_indent and stripped and not stripped.startswith("#") and stripped != "schemas:":
            in_schemas = False

    # 2. 提取 paths 段下的端点契约
    endpoints = {}  # path -> {method -> {responses, required_fields, security, parameters}}
    in_paths = False
    current_path = None
    current_method = None
    path_indent = 0
    method_indent = 0
    in_responses = False
    resp_indent = 0

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if stripped == "paths:":
            in_paths = True
            path_indent = indent
            continue

        if not in_paths:
            continue

        # 离开 paths 段（回到顶层缩进）
        if indent <= path_indent and stripped and not stripped.startswith("#") and stripped != "paths:":
            in_paths = False
            continue

        # 路径行（如 /devices/register:）
        if indent == path_indent + 2 and stripped.startswith("/") and stripped.endswith(":"):
            current_path = stripped.rstrip(":")
            endpoints[current_path] = {}
            current_method = None
            in_responses = False
            continue

        # 方法行（如 post: 或 get:）
        if current_path and indent == path_indent + 4 and re.match(r"(post|get|put|delete|patch):$", stripped):
            current_method = stripped.rstrip(":")
            endpoints[current_path][current_method] = {
                "responses": [],
                "required_fields": [],
                "security": None,  # None=未指定（继承全局），[]=无需认证
                "parameters": [],
            }
            in_responses = False
            continue

        if not current_method or current_path not in endpoints:
            continue

        ep = endpoints[current_path][current_method]

        # security 段
        if stripped.startswith("security:"):
            inline = stripped[len("security:"):].strip()
            if inline == "[]":
                ep["security"] = []
            elif "bearerAuth" in inline:
                ep["security"] = ["bearerAuth"]
            continue

        # responses 段
        if stripped == "responses:":
            in_responses = True
            resp_indent = indent
            current_resp_status = None
            continue

        if in_responses:
            # 状态码行（如 '200':）
            if re.match(r"['\"]?\d+['\"]?:$", stripped):
                current_resp_status = stripped.strip("'\":")
                ep["responses"].append(current_resp_status)
                continue

            # $ref 引用（响应 schema）——只取 200 响应的 required_fields
            # 避免被后续 401/404 的 ErrorResponse required 覆盖
            if "$ref:" in stripped:
                ref = stripped.split("$ref:")[1].strip().strip("'\"")
                schema_name = ref.split("/")[-1]
                if schema_name in schemas_required and current_resp_status == "200":
                    ep["required_fields"] = list(schemas_required[schema_name])
                continue

            # 离开 responses 段
            if indent <= resp_indent and stripped and not stripped.startswith("#") and not re.match(r"['\"]?\d+['\"]?:$", stripped):
                in_responses = False

        # parameters 段
        if stripped == "parameters:":
            continue
        if stripped.startswith("- name:") and current_method:
            param_name = stripped.split("name:")[1].strip()
            ep["parameters"].append({"name": param_name})
            continue

    return endpoints, schemas_required


# ---- HTTP 工具 ----
def http_request(base_url, method, path, headers=None, body=None, timeout=10):
    """发送 HTTP 请求，返回 (status_code, response_json, response_headers)"""
    url = base_url + path
    req = urllib.request.Request(url, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode("utf-8")
    else:
        data = None

    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            status = resp.status
            # headers 大小写不敏感（HTTP 规范：header name case-insensitive）
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            resp_body = resp.read()
            try:
                resp_json = json.loads(resp_body)
            except (json.JSONDecodeError, ValueError):
                resp_json = None
            return status, resp_json, resp_headers
    except urllib.error.HTTPError as e:
        status = e.code
        resp_headers = {k.lower(): v for k, v in e.headers.items()}
        resp_body = e.read()
        try:
            resp_json = json.loads(resp_body)
        except (json.JSONDecodeError, ValueError):
            resp_json = None
        return status, resp_json, resp_headers


# ---- 空闲端口探测 ----
def find_free_port():
    """找一个空闲端口（避开 8765 常用端口，从 18765 开始）"""
    for port in range(18765, 18850):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError("找不到空闲端口")


def http_raw(base_url, method, path, headers=None, timeout=10):
    """发送 HTTP 请求，返回 (status_code, response_headers, raw_body_bytes)。

    与 http_request 的区别：保留原始二进制 body（/data 端点精确长度断言用）；
    headers 同样转为大小写不敏感 dict（round 6 协议面负例专用）。
    """
    url = base_url + path
    req = urllib.request.Request(url, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return status, resp_headers, resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        resp_headers = {k.lower(): v for k, v in e.headers.items()}
        return status, resp_headers, e.read()


# ---- 契约检查用例 ----
def run_checks(base_url, endpoints, schemas_required):
    """逐端点按契约发起请求验证"""

    # ========== POST /devices/register ==========
    register_resp = None
    if "/devices/register" in endpoints and "post" in endpoints["/devices/register"]:
        ep = endpoints["/devices/register"]["post"]

        # 正例
        name = "POST /devices/register -- 正例"
        status, resp, _ = http_request(
            base_url, "POST", "/devices/register",
            body={"deviceName": "contract-test", "platform": "harmonyos",
                  "capabilities": ["chunked-download"]},
        )
        ok = status == 200
        detail = f"status={status}"
        if ok and resp:
            for field in ep["required_fields"]:
                if field not in resp:
                    ok = False
                    detail += f", missing field: {field}"
                    break
            if ok:
                if not isinstance(resp.get("deviceId"), str):
                    ok = False; detail += ", deviceId not string"
                elif not isinstance(resp.get("deviceToken"), str):
                    ok = False; detail += ", deviceToken not string"
                elif not isinstance(resp.get("serverTime"), int):
                    ok = False; detail += ", serverTime not int"
                else:
                    detail = "status=200, all required fields present & typed"
        record(name, ok, detail)
        register_resp = resp if ok else None

        # 负例：缺失必填字段 deviceName
        name = "POST /devices/register -- 负例（缺失 deviceName）"
        status, resp, _ = http_request(
            base_url, "POST", "/devices/register",
            body={"platform": "harmonyos"},
        )
        if status == 200:
            record(name, False, f"mock 未校验必填字段（openapi 标注 required），status={status}")
        else:
            record(name, True, f"status={status}（4xx 符合契约）")
    else:
        record("POST /devices/register -- 契约缺失", False, "openapi.yaml 中未定义此端点")

    device_token = register_resp.get("deviceToken") if register_resp else None
    device_id = register_resp.get("deviceId") if register_resp else None

    # ========== POST /devices/heartbeat ==========
    if "/devices/heartbeat" in endpoints and "post" in endpoints["/devices/heartbeat"]:
        ep = endpoints["/devices/heartbeat"]["post"]

        # 正例
        if device_token and device_id:
            name = "POST /devices/heartbeat -- 正例"
            status, resp, _ = http_request(
                base_url, "POST", "/devices/heartbeat",
                headers={"Authorization": f"Bearer {device_token}"},
                body={"deviceId": device_id},
            )
            ok = status == 200
            detail = f"status={status}"
            if ok and resp:
                for field in ep["required_fields"]:
                    if field not in resp:
                        ok = False; detail += f", missing: {field}"
                        break
                if ok:
                    if resp.get("status") != "ok":
                        ok = False; detail += ", status!=ok"
                    elif not isinstance(resp.get("serverTime"), int):
                        ok = False; detail += ", serverTime not int"
                    else:
                        detail = "status=200, fields ok"
            record(name, ok, detail)
        else:
            record("POST /devices/heartbeat -- 正例", False, "注册失败，无 token")

        # 负例：错误 token -> 401
        name = "POST /devices/heartbeat -- 负例（错误 token -> 401）"
        status, resp, _ = http_request(
            base_url, "POST", "/devices/heartbeat",
            headers={"Authorization": "Bearer invalid-token"},
            body={"deviceId": "dev-invalid"},
        )
        ok = status == 401
        detail = f"status={status}"
        if ok and resp and "error" in resp:
            detail = "status=401, error field present"
        record(name, ok, detail)
    else:
        record("POST /devices/heartbeat -- 契约缺失", False, "openapi.yaml 中未定义此端点")

    # ========== GET /devices ==========
    if "/devices" in endpoints and "get" in endpoints["/devices"]:
        name = "GET /devices -- 正例"
        status, resp, _ = http_request(base_url, "GET", "/devices")
        ok = status == 200
        detail = f"status={status}"
        if ok and resp is not None:
            if not isinstance(resp, dict):
                ok = False; detail += ", response not object"
            else:
                detail = f"status=200, object with {len(resp)} devices"
        record(name, ok, detail)
    else:
        record("GET /devices -- 契约缺失", False, "openapi.yaml 中未定义此端点")

    # ========== GET /namespace ==========
    if "/namespace" in endpoints and "get" in endpoints["/namespace"]:
        ep = endpoints["/namespace"]["get"]

        if device_token:
            # 正例：全量拉取 since=0
            name = "GET /namespace?since=0 -- 正例（全量）"
            status, resp, _ = http_request(
                base_url, "GET", "/namespace?since=0",
                headers={"Authorization": f"Bearer {device_token}"},
            )
            ok = status == 200
            detail = f"status={status}"
            if ok and resp:
                for field in ep["required_fields"]:
                    if field not in resp:
                        ok = False; detail += f", missing: {field}"
                        break
                if ok:
                    if resp.get("mode") != "full":
                        ok = False; detail += ", mode!=full"
                    elif not isinstance(resp.get("nodes"), list):
                        ok = False; detail += ", nodes not list"
                    elif not isinstance(resp.get("version"), int):
                        ok = False; detail += ", version not int"
                    else:
                        detail = f"status=200, mode=full, {len(resp['nodes'])} nodes, v={resp['version']}"
            record(name, ok, detail)
            namespace_version = resp.get("version") if ok and resp else None

            # 正例：增量拉取 since>0
            name = "GET /namespace?since=1 -- 正例（增量）"
            status, resp, _ = http_request(
                base_url, "GET", "/namespace?since=1",
                headers={"Authorization": f"Bearer {device_token}"},
            )
            ok = status == 200
            detail = f"status={status}"
            if ok and resp:
                if resp.get("mode") != "incremental":
                    ok = False; detail += ", mode!=incremental"
                else:
                    detail = f"status=200, mode=incremental, {len(resp.get('nodes', []))} nodes"
            record(name, ok, detail)
        else:
            record("GET /namespace -- 正例", False, "注册失败，无 token")
            namespace_version = None

        # 负例：错误 token -> 401
        name = "GET /namespace?since=0 -- 负例（错误 token -> 401）"
        status, resp, _ = http_request(
            base_url, "GET", "/namespace?since=0",
            headers={"Authorization": "Bearer invalid-token"},
        )
        ok = status == 401
        detail = f"status={status}"
        record(name, ok, detail)
    else:
        record("GET /namespace -- 契约缺失", False, "openapi.yaml 中未定义此端点")
        namespace_version = None

    # ========== GET /data/{nodeId} ==========
    if "/data/{nodeId}" in endpoints and "get" in endpoints["/data/{nodeId}"]:
        if device_token:
            # 预取 /namespace 元数据（取 node=3 的 sha256，供后续 sha 比对断言用）
            ns_status, ns_resp, _ = http_request(
                base_url, "GET", "/namespace?since=0",
                headers={"Authorization": f"Bearer {device_token}"},
            )
            meta_sha = ''
            if ns_status == 200 and isinstance(ns_resp, dict):
                for n in ns_resp.get('nodes', []):
                    if isinstance(n, dict) and n.get('id') == 3:
                        meta_sha = n.get('sha256', '')
                        break

            # 正例：全量下载（无 Range）
            name = "GET /data/3 -- 正例（全量下载）"
            status, resp, headers = http_request(
                base_url, "GET", "/data/3",
                headers={"Authorization": f"Bearer {device_token}"},
            )
            ok = status == 200
            detail = f"status={status}"
            if ok:
                if "x-content-sha256" not in headers:
                    ok = False; detail += ", missing X-Content-Sha256"
                elif "etag" not in headers:
                    ok = False; detail += ", missing ETag"
                elif "accept-ranges" not in headers:
                    ok = False; detail += ", missing Accept-Ranges"
                else:
                    detail = "status=200, sha256/ETag/Accept-Ranges headers present"
            record(name, ok, detail)

            # 正例：Range 分片下载 -> 206
            name = "GET /data/3 -- 正例（Range -> 206）"
            status, resp, headers = http_request(
                base_url, "GET", "/data/3",
                headers={"Authorization": f"Bearer {device_token}",
                         "Range": "bytes=0-1023"},
            )
            ok = status == 206
            detail = f"status={status}"
            if ok:
                if "content-range" not in headers:
                    ok = False; detail += ", missing Content-Range"
                else:
                    detail = "status=206, Content-Range present"
            record(name, ok, detail)

            # ---- Range 语法族负例 ----
            # 负例：开放 Range bytes=0- -> 206 到文件尾
            name = "GET /data/3 -- 负例（开放 Range bytes=0-）"
            status, hdr, body = http_raw(
                base_url, "GET", "/data/3",
                headers={"Authorization": f"Bearer {device_token}",
                         "Range": "bytes=0-"},
            )
            ok = status in (200, 206)
            detail = f"status={status} len={len(body)}B"
            if ok and status == 206:
                cr = hdr.get('content-range', '')
                if not cr.startswith('bytes 0-'):
                    ok = False; detail += f", unexpected Content-Range: {cr}"
            record(name, ok, detail)

            # 负例：越界 Range bytes=99999999- -> 416 + Content-Range bytes */total
            name = "GET /data/3 -- 负例（越界 Range -> 416）"
            status, hdr, body = http_raw(
                base_url, "GET", "/data/3",
                headers={"Authorization": f"Bearer {device_token}",
                         "Range": "bytes=99999999-"},
            )
            ok = status == 416
            detail = f"status={status}"
            if ok:
                cr = hdr.get('content-range', '')
                if not cr.startswith('bytes */'):
                    ok = False; detail += f", missing Content-Range bytes */total: {cr}"
                else:
                    detail = f"status=416, Content-Range={cr}"
            record(name, ok, detail)

            # 负例：目录节点 -> 400
            name = "GET /data/1 -- 负例（目录节点 -> 400）"
            status, _, _ = http_request(
                base_url, "GET", "/data/1",
                headers={"Authorization": f"Bearer {device_token}"},
            )
            ok = status == 400
            record(name, ok, f"status={status}")

            # 负例：不存在节点 -> 404
            name = "GET /data/9999 -- 负例（不存在节点 -> 404）"
            status, _, _ = http_request(
                base_url, "GET", "/data/9999",
                headers={"Authorization": f"Bearer {device_token}"},
            )
            ok = status == 404
            record(name, ok, f"status={status}")

            # 正例：sha256 与元数据比对（ETag 引号包裹容忍）
            name = "GET /data/3 -- 正例（sha256 与元数据比对）"
            status, hdr, _ = http_raw(
                base_url, "GET", "/data/3",
                headers={"Authorization": f"Bearer {device_token}"},
            )
            ok = status == 200
            detail = f"status={status}"
            if ok:
                header_sha = (hdr.get('x-content-sha256') or '').strip()
                etag_val = (hdr.get('etag') or '').strip().replace('"', '')
                if not header_sha:
                    ok = False; detail += ", missing X-Content-Sha256"
                elif meta_sha and header_sha != meta_sha:
                    ok = False; detail += f", sha mismatch: {header_sha[:12]} vs meta {meta_sha[:12]}"
                elif etag_val and etag_val != header_sha:
                    ok = False; detail += f", ETag({etag_val[:12]}) != sha256({header_sha[:12]})"
                else:
                    detail = f"status=200, sha256 match meta={meta_sha[:12]} ETag stripped={etag_val[:12]}"
            record(name, ok, detail)
        else:
            record("GET /data/{nodeId} -- 正例", False, "注册失败，无 token")
    else:
        record("GET /data/{nodeId} -- 契约缺失", False, "openapi.yaml 中未定义此端点")

    # ========== POST /admin/namespace/delete ==========
    if "/admin/namespace/delete" in endpoints and "post" in endpoints["/admin/namespace/delete"]:
        ep = endpoints["/admin/namespace/delete"]["post"]

        # 正例
        name = "POST /admin/namespace/delete -- 正例（id=10 /backup）"
        status, resp, _ = http_request(
            base_url, "POST", "/admin/namespace/delete",
            body={"id": 10},
        )
        ok = status == 200
        detail = f"status={status}"
        if ok and resp:
            for field in ep["required_fields"]:
                if field not in resp:
                    ok = False; detail += f", missing: {field}"
                    break
            if ok:
                detail = "status=200, all required fields present"
        record(name, ok, detail)

        # 负例：不存在节点 -> 404
        name = "POST /admin/namespace/delete -- 负例（不存在节点 -> 404）"
        status, resp, _ = http_request(
            base_url, "POST", "/admin/namespace/delete",
            body={"id": 9999},
        )
        ok = status == 404
        detail = f"status={status}"
        record(name, ok, detail)
    else:
        record("POST /admin/namespace/delete -- 契约缺失", False, "openapi.yaml 中未定义此端点")

    # ========== POST /admin/namespace/rollback ==========
    if "/admin/namespace/rollback" in endpoints and "post" in endpoints["/admin/namespace/rollback"]:
        ep = endpoints["/admin/namespace/rollback"]["post"]

        # 正例
        name = "POST /admin/namespace/rollback -- 正例"
        status, resp, _ = http_request(
            base_url, "POST", "/admin/namespace/rollback",
            body={"version": 1},
        )
        ok = status == 200
        detail = f"status={status}"
        if ok and resp:
            for field in ep["required_fields"]:
                if field not in resp:
                    ok = False; detail += f", missing: {field}"
                    break
            if ok:
                detail = "status=200, all required fields present"
        record(name, ok, detail)
    else:
        record("POST /admin/namespace/rollback -- 契约缺失", False, "openapi.yaml 中未定义此端点")

    # ========== namespace 版本回退语义验证 ==========
    if device_token and namespace_version:
        # 先 rollback 到低版本，再用高 since 做增量拉取，验证 resp.version < since
        name = "namespace 版本回退语义 -- rollback 后 version < since"
        http_request(
            base_url, "POST", "/admin/namespace/rollback",
            body={"version": 1},
        )
        status, resp, _ = http_request(
            base_url, "GET", f"/namespace?since={namespace_version}",
            headers={"Authorization": f"Bearer {device_token}"},
        )
        ok = status == 200
        detail = f"status={status}"
        if ok and resp:
            if resp.get("version", 0) < namespace_version:
                detail = f"resp.version={resp.get('version')} < since={namespace_version} (回退语义正确)"
            else:
                ok = False
                detail = f"resp.version={resp.get('version')} >= since={namespace_version} (回退未生效)"
        record(name, ok, detail)
    else:
        record("namespace 版本回退语义", False, "前置条件不满足（无 token 或无 version）")

    # ========== GET /health ==========
    if "/health" in endpoints and "get" in endpoints["/health"]:
        ep = endpoints["/health"]["get"]

        name = "GET /health -- 正例"
        status, resp, _ = http_request(base_url, "GET", "/health")
        ok = status == 200
        detail = f"status={status}"
        if ok and resp:
            for field in ep["required_fields"]:
                if field not in resp:
                    ok = False; detail += f", missing: {field}"
                    break
            if ok:
                if resp.get("status") != "ok":
                    ok = False; detail += ", status!=ok"
                else:
                    detail = "status=200, all required fields present"
        record(name, ok, detail)
    else:
        record("GET /health -- 契约缺失", False, "openapi.yaml 中未定义此端点")


# ---- 主流程 ----
def main():
    print("=" * 60)
    print("StorPool OpenAPI 契约一致性检查")
    print("=" * 60)

    # 1. 解析 openapi.yaml
    print("\n[1] 解析 openapi.yaml...")
    endpoints, schemas_required = load_openapi()
    endpoint_count = sum(len(methods) for methods in endpoints.values())
    print(f"  提取端点: {len(endpoints)} paths, {endpoint_count} methods")
    for path, methods in sorted(endpoints.items()):
        for method, info in sorted(methods.items()):
            print(f"    {method.upper()} {path} -> responses={info['responses']}, "
                  f"required={info['required_fields']}, security={info['security']}")

    # 2. 启动 mock server
    print("\n[2] 启动 mock server...")
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, MOCK_SERVER_PATH, str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=PROJECT_ROOT,  # 仓库根（MOCK_SERVER_PATH 已是相对路径）
    )

    try:
        # 等待 server 就绪
        time.sleep(1.0)
        for attempt in range(10):
            try:
                status, _, _ = http_request(base_url, "GET", "/health", timeout=2)
                if status == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            print("  [ERROR] mock server 启动失败")
            proc.kill()
            sys.exit(1)

        print(f"  mock server 就绪: {base_url}")

        # 3. 逐端点验证
        print("\n[3] 契约验证...")
        run_checks(base_url, endpoints, schemas_required)

    finally:
        # 4. 关闭 mock server
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # 5. 汇总
    print("\n" + "=" * 60)
    passed = sum(1 for _, p, _ in _results if p)
    failed = sum(1 for _, p, _ in _results if not p)
    total = len(_results)
    print(f"汇总: {passed} PASS / {failed} FAIL / {total} TOTAL")

    if failed > 0:
        print("\n失败用例:")
        for name, p, detail in _results:
            if not p:
                print(f"  [FAIL] {name} -- {detail}")

    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()