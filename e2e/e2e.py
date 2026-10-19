#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StorPool 完整 e2e 流程脚本（P1-1，机器 C：feat/e2e-scripts）

三件套分层：
  mock-control-plane/selftest.py — 协议层：mock 端点 HTTP 语义自检（24 用例）
  e2e/hapctl.py                  — 设备层：HAP 构建/安装/启动 + uitest 驱动原语
  e2e/e2e.py                     — 流程层：本文件（编排协议层 + 设备层成完整 e2e）

流程（对应三机分工方案中机器 C 的验证中心职责）：
  阶段 http   ：启动 mock server → selftest.py 协议自检 → 关闭
                （独立 server 实例，自检的墓碑/回退操作不污染设备阶段状态）
  阶段 device ：全新 mock server → 安装 HAP → 启动应用 → 六步验证：
                H0b  NAPI 四项检查全通过（UI「全部通过 4/4」）
                H1   注册成功（UI + server /devices 出现 harmonyos 设备）
                H1+  心跳持续（server heartbeatCount ≥ 2，心跳间隔 15s）
                H2   同步目录树（UI「已同步（全量 10 节点」+ statLine 10节点5文件）
                H3   申请保活（UI「已保活 nid=」）
                H4   分片下载 report.pdf（documents 目录 → ⬇ → 「✅ … sha256 校验通过」
                      + server 端 7 片 206 日志 + 设备侧落盘文件 sha256 三方一致）

用法：
  python e2e.py                        # 全流程（http + device + heal401；无设备自动尝试启模拟器）
  python e2e.py --phase http           # 仅协议层自检（无需设备）
  python e2e.py --phase device         # 设备端流程（六步验证 + heal401 自愈场景）
  python e2e.py --phase heal401        # 仅 P0-2 401 自愈场景（heal401.py）
  python e2e.py --skip-build           # 跳过构建（复用已有 HAP）
  python e2e.py --serial <target>      # 多设备时指定目标
  python e2e.py --json report.json     # 机器可读报告（控制台汇总始终输出）
  python e2e.py --hb-wait 25           # 第 2 次心跳等待上限（默认 22s）
  python e2e.py --run-slow             # heal401 冷却时序真实等待 30s（默认 SKIP）

依赖：hdc / uitest（API 9+）/ devecocli（构建时）/ 仅 Python 标准库。
注意：未签名 HAP 仅可装模拟器；真机需签名（挂机器 A 的 AGC 实名流程）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MOCK_DIR = os.path.join(REPO, 'mock-control-plane')
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from hapctl import (BUNDLE, ABILITY, DEFAULT_HAP, Hdc, HdcError, UiTree, run)  # noqa: E402

PORT = 8765
BASE = f'http://127.0.0.1:{PORT}'
LOG_DIR = os.path.join(HERE, 'logs')

# selftest.py 24 用例（协议层，作阶段校验基准；a1e354e 起用例 19-24 入库）
SELFTEST_CASES = 24


# ============================================================
# 记录器：用例状态（PASS / FAIL / BLOCKED / SKIP）
# ============================================================

class Report:
    def __init__(self):
        self.cases: list = []  # {name, status, detail}
        self.t0 = time.time()

    def case(self, name, status, detail=''):
        mark = {'PASS': '✅', 'FAIL': '❌', 'BLOCKED': '⛔', 'SKIP': '⏭'}.get(status, ' ')
        print(f'  [{mark} {status}] {name}' + (f' — {detail}' if detail else ''))
        self.cases.append({'name': name, 'status': status, 'detail': str(detail)[:400]})
        return status == 'PASS'

    def expect(self, name, cond, ok_detail='', fail_detail=''):
        return self.case(name, 'PASS' if cond else 'FAIL',
                         ok_detail if cond else (fail_detail or ok_detail))

    def to_dict(self):
        n = {s: sum(1 for c in self.cases if c['status'] == s)
             for s in ('PASS', 'FAIL', 'BLOCKED', 'SKIP')}
        return {
            'startedAt': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.t0)),
            'durationSec': round(time.time() - self.t0, 1),
            'cases': self.cases,
            'summary': {**n, 'total': len(self.cases)},
        }


# ============================================================
# mock server 生命周期
# ============================================================

def http_json(path: str, timeout=5):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {}


class MockServer:
    """mock-control-plane/server.py 子进程管理（设备阶段用独立实例保证干净状态）"""

    def __init__(self, tag: str):
        self.tag = tag
        self.proc: subprocess.Popen | None = None
        self.external = False
        self.log_path = os.path.join(LOG_DIR, f'server_{tag}.log')
        self.log_fh = None

    def probe(self) -> bool:
        code, h = http_json('/health', timeout=2)
        return code == 200 and 'namespaceVersion' in h

    def start(self, wait=15) -> bool:
        os.makedirs(LOG_DIR, exist_ok=True)
        if self.probe():  # 已有 server 在跑（外部启动）→ 复用，退出时不杀
            self.external = True
            print(f'  [server:{self.tag}] 端口 {PORT} 已有 mock server，复用（外部实例）')
            return True
        self.log_fh = open(self.log_path, 'w', encoding='utf-8')
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(MOCK_DIR, 'server.py'), str(PORT)],
            cwd=MOCK_DIR, stdout=self.log_fh, stderr=subprocess.STDOUT, env=env)
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.probe():
                print(f'  [server:{self.tag}] 就绪 @ {BASE}（pid={self.proc.pid}）')
                return True
            if self.proc.poll() is not None:
                print(f'  [server:{self.tag}] 启动即退出，见 {self.log_path}')
                return False
            time.sleep(0.4)
        print(f'  [server:{self.tag}] {wait}s 内 /health 未就绪')
        return False

    def devices(self) -> dict:
        _, d = http_json('/devices', timeout=5)
        return d if isinstance(d, dict) else {}

    def log_text(self) -> str:
        try:
            with open(self.log_path, encoding='utf-8') as f:
                return f.read()
        except OSError:
            return ''

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.log_fh:
            self.log_fh.close()
        if self.proc:
            print(f'  [server:{self.tag}] 已停止' + ('' if self.external else ''))


# ============================================================
# 模拟器自动拉起（无设备时；镜像/实例就绪前提下）
# ============================================================

def try_start_emulator() -> bool:
    cli = shutil.which('devecocli') or shutil.which('devecocli.cmd')
    if not cli:
        print('  [emulator] devecocli 不可用，跳过自动拉起')
        return False
    code, out = run([cli, 'emulator', 'list'], timeout=60)
    names = re.findall(r'^\s*[-*]?\s*([A-Za-z0-9_\-\.]+)\s*$', out or '',
                       re.M) if code == 0 else []
    # 过滤列表头部说明行
    names = [n for n in names if not re.match(r'(?i)listing|emulator$|no ', n)]
    if not names:
        print('  [emulator] 无本地模拟器实例（devecocli emulator list 为空）。')
        print('             镜像就绪后：devecocli emulator create <name> '
              '--device-type phone，或用 DevEco Device Manager 创建。')
        return False
    name = names[0]
    print(f'  [emulator] 启动实例 {name} …')
    run([cli, 'emulator', 'start', name], timeout=30)
    deadline = time.time() + 240
    while time.time() < deadline:
        if Hdc.targets():
            print(f'  [emulator] 设备已上线: {Hdc.targets()}')
            time.sleep(5)  # 等系统服务（uitest/bm）完全就绪
            return True
        time.sleep(4)
    print('  [emulator] 240s 内设备未上线')
    return False


# ============================================================
# 阶段 http：协议层自检（selftest.py 24 用例）
# ============================================================

def phase_http(rep: Report) -> bool:
    print('\n== 阶段 http：mock 协议层自检 ==')
    srv = MockServer('http')
    if not srv.start():
        srv.stop()
        rep.case('server 启动（http 阶段）', 'FAIL', f'详见 {srv.log_path}')
        return False
    rep.expect('server 启动（http 阶段）', True, BASE)
    try:
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        p = subprocess.run([sys.executable, os.path.join(MOCK_DIR, 'selftest.py'),
                            str(PORT)], cwd=MOCK_DIR, capture_output=True,
                           text=True, encoding='utf-8', errors='replace',
                           env=env, timeout=120)
        out = (p.stdout or '')
        m = re.search(r'(\d+) passed, (\d+) failed', out)
        passed, failed = (int(m.group(1)), int(m.group(2))) if m else (-1, -1)
        ok = p.returncode == 0 and failed == 0 and passed == SELFTEST_CASES
        rep.expect(f'协议层自检（selftest.py {SELFTEST_CASES} 用例）', ok,
                   f'{passed} passed / {failed} failed',
                   out.strip().splitlines()[-1] if out.strip() else 'no output')
        if not ok:
            print('\n'.join(l for l in out.splitlines() if 'FAIL' in l)[:600])
        return ok
    finally:
        srv.stop()


# ============================================================
# 阶段 device：设备端六步流程
# ============================================================

def expected_report_sha256() -> str:
    """server 确定性生成的 report.pdf（id=8）sha256（与 server.py file_data 同逻辑）"""
    seed = hashlib.sha256(b'storpool-seed-8-report.pdf').digest()
    block4k = seed * (4096 // len(seed))
    n_blocks = (819200 + 4095) // 4096
    return hashlib.sha256((block4k * n_blocks)[:819200]).hexdigest()


def wait_for(ui: UiTree, pattern, timeout=20, poll=1.5):
    """轮询 dump 直到文本正则命中（可见），返回首个命中节点，超时 None

    ⚠ P2-8 锚定审计注记（2026-10-19，round6）：
    本函数是**存在性等待**——只认「文本出现过」，不区分本轮/遗留。
    安全前提 = 断言点处于「卸载重装 + 单次流程」结构（phase_device 全部
    断言点满足：uninstall→install→aa start 后 UI 全新，无旧文案残留路径）。
    **多轮循环场景（401 自愈循环、soak 阶段 B 等）禁止直接用本函数等待
    注册类文案**——旧轮文案残留会立即误匹配（soak r5 实锤的「滞后一轮」
    陷阱）。必须用 heal401._wait_re_register 式锚定：serverTime 严格大于
    本轮基准值才认。注入测试见 round6 交付（4/4：锚定拒绝旧值/返回新值/
    存在性误匹配对照/基数=0 退化对照）。真机实证：heal401 场景注册文案
    为覆盖形态（单槽位更新），锚定系无害兜底。
    """
    rx = re.compile(pattern)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ui.dump()
        except (HdcError, json.JSONDecodeError, OSError):
            time.sleep(poll)  # dump 偶发失败（应用启动窗口）→ 重试
            continue
        for n in ui.find_text(rx.pattern):
            if ui.visible(n):
                return n
        time.sleep(poll)
    return None


def ensure_screen_awake(hdc: Hdc):
    """屏幕唤醒 + 锁屏自动上滑解锁（模拟器冷启动常锁屏；无密码场景）。

    幂等：已解锁时无任何动作。锁屏判定：dump 含大时钟星期文本且无桌面/应用特征。
    """
    try:
        hdc.shell('power-shell wakeup', timeout=10)
        hdc.shell('power-shell timeout -t 1800000', timeout=10)  # 30 分钟不锁屏
        time.sleep(0.8)
        ui = UiTree(hdc).dump()
    except (HdcError, json.JSONDecodeError, OSError):
        return  # dump 失败不阻塞主流程（后续阶段各自有 dump 重试）
    texts = [n.text for n in ui.root.iter_all() if n.text]
    has_clock = any(re.search(r'星期[一二三四五六日天]', t) for t in texts)
    has_home = any(('StorPool' in t or t in ('设置', '图库', '文件管理', '日历')
                    or 'H0b' in t) for t in texts)
    if has_clock and not has_home:
        w, h = ui.screen or (1080, 2340)
        print('  [screen] 检测到锁屏，上滑解锁…')
        ui.swipe(w // 2, int(h * 0.85), w // 2, int(h * 0.25), 500)
        time.sleep(1.5)


def phase_device(rep: Report, hdc: Hdc, args) -> bool:
    print('\n== 阶段 device：设备端全流程 ==')

    # ---- 0. HAP 构建（或复用） ----
    hap = args.hap or DEFAULT_HAP
    if args.skip_build and os.path.exists(hap):
        rep.case('HAP 就绪（--skip-build 复用）', 'PASS',
                 f'{hap}（{os.path.getsize(hap)//1024} KiB）')
    else:
        cli = shutil.which('devecocli') or shutil.which('devecocli.cmd')
        code, out = run([cli, 'build', '--build-mode', 'debug'], timeout=900)
        ok = code == 0 and 'BUILD SUCCESSFUL' in out and os.path.exists(hap)
        rep.expect('HAP 构建（未签名 debug）', ok,
                   f'{os.path.getsize(hap)//1024} KiB' if ok else
                   '\n'.join(out.splitlines()[-5:])[-400:])
        if not ok:
            return False

    # ---- 全新 server（设备阶段独立实例） ----
    srv = MockServer('device')
    if not srv.start():
        srv.stop()
        rep.case('server 启动（device 阶段）', 'FAIL', f'详见 {srv.log_path}')
        return False
    rep.expect('server 启动（device 阶段）', True, BASE)

    launched = False
    try:
        # ---- 屏幕唤醒/解锁（幂等） ----
        ensure_screen_awake(hdc)

        # ---- 安装 + 启动（卸载保证全新身份/数据） ----
        hdc.uninstall()
        code, out = hdc.install(hap)
        ok = code == 0 and hdc.is_installed()
        rep.expect('安装 HAP（卸载重装）', ok, out.strip().splitlines()[-1][:80]
                   if out.strip() else '')
        if not ok:
            rep.case('安装失败提示', 'SKIP', '未签名包仅模拟器可装；真机需签名')
            return False

        code, out = hdc.shell(f'aa start -b {BUNDLE} -a {ABILITY}', timeout=30)
        ok = code == 0
        rep.expect('启动应用（aa start）', ok, out.strip()[:100])
        if not ok:
            return False
        launched = True

        ui = UiTree(hdc)

        # ---- H0b：NAPI 四项检查（aboutToAppear 同步执行，稍候首帧） ----
        n0 = wait_for(ui, re.escape('全部通过 4/4'), timeout=25)
        rep.expect('H0b NAPI 桥接检查全通过', n0 is not None,
                   n0.text if n0 else '25s 内未见「全部通过 4/4」')

        # ---- H1：注册（异步；全新安装 → 必走注册成功路径） ----
        reg = wait_for(ui, r'注册成功（serverTime=\d+）|已注册（本地身份复用）',
                       timeout=25)
        rep.expect('H1 注册成功（UI）', reg is not None,
                   reg.text if reg else '25s 内未见注册成功文案')

        dev_on_server = {}
        for _ in range(10):
            dev_on_server = {k: v for k, v in srv.devices().items()
                             if v.get('platform') == 'harmonyos'}
            if dev_on_server:
                break
            time.sleep(1)
        rep.expect('H1 注册成功（server /devices 可见）', bool(dev_on_server),
                   f'{len(dev_on_server)} 台 harmonyos 设备')

        # ---- H1+：心跳持续（间隔 15s；等第 2 次心跳到 server） ----
        hb_wait = args.hb_wait
        hb_node = wait_for(ui, r'\d+ 次 / 最近 \d\d:\d\d:\d\d', timeout=10)
        rep.expect('H1 心跳 UI（≥1 次）', hb_node is not None,
                   hb_node.text if hb_node else '未见心跳计数')
        target_dev = next(iter(dev_on_server.values()), {}) if dev_on_server else {}
        count1 = int(target_dev.get('heartbeatCount') or 0)
        hb2_ok = False
        deadline = time.time() + hb_wait
        while time.time() < deadline:
            d = {k: v for k, v in srv.devices().items()
                 if v.get('platform') == 'harmonyos'}
            counts = [int(v.get('heartbeatCount') or 0) for v in d.values()]
            if counts and max(counts) >= 2:
                hb2_ok = True
                count1 = max(counts)
                break
            time.sleep(2)
        rep.expect('H1 心跳持续（server heartbeatCount ≥ 2）', hb2_ok,
                   f'heartbeatCount={count1}（等待 {hb_wait}s）')

        # ---- H2：同步目录树（点击区块③按钮） ----
        try:
            ui.scroll_find_click(re.escape('同步目录树'))
            clicked_sync = True
        except HdcError as e:
            clicked_sync = False
            rep.case('H2 触发同步（点击按钮）', 'FAIL', str(e)[:200])
        if clicked_sync:
            sync = wait_for(ui, r'已同步（全量 10 节点，v0→v\d+）', timeout=20)
            rep.expect('H2 同步成功（UI 全量 10 节点）', sync is not None,
                       sync.text if sync else '20s 内未见「已同步（全量 10 节点」')
            stat = wait_for(ui, r'10 节点 · 5 文件 · ', timeout=8)
            rep.expect('H2 统计行（10 节点 · 5 文件）', stat is not None,
                       stat.text if stat else '未见 statLine')
            rep.expect('H2 server 日志（mode=full）',
                       'mode=full' in srv.log_text(),
                       'server 收到全量拉取请求')

        # ---- H3：申请保活 ----
        try:
            ui.scroll_find_click(re.escape('申请保活'))
            ka = wait_for(ui, r'已保活 nid=\d+', timeout=15)
            rep.expect('H3 保活申请成功（已保活 nid=）', ka is not None,
                       ka.text if ka else '15s 内未见「已保活 nid=」')
        except HdcError as e:
            rep.case('H3 触发保活（点击按钮）', 'FAIL', str(e)[:200])

        # ---- H4：documents → report.pdf 分片下载 ----
        try:
            ui.scroll_find_click(r'^documents$')
            entered = wait_for(ui, re.escape('report.pdf'), timeout=10)
            if entered is None:
                rep.case('H4 进入 documents 目录', 'FAIL', '未见 report.pdf 条目')
            else:
                rep.case('H4 进入 documents 目录', 'PASS', '列表含 report.pdf')
                # 同行点击 ⬇（smallest subtree: report.pdf + ⬇）
                ui.dump()
                sub = ui.smallest_subtree_with([re.escape('report.pdf'),
                                                re.escape('⬇')])
                dl_btn = None
                if sub is not None:
                    rx = re.compile(re.escape('⬇'))
                    dl_btn = next((n for n in sub.iter_all()
                                   if n.text and rx.fullmatch(n.text.strip())), None)
                if dl_btn is not None and ui.visible(dl_btn):
                    ui.click_node(dl_btn)
                    dl = wait_for(ui, r'✅ report\.pdf 已下载（819200B，sha256 校验通过',
                                  timeout=60)
                    rep.expect('H4 分片下载成功（UI sha256 校验通过）',
                               dl is not None,
                               dl.text[:120] if dl else '60s 内未见下载成功文案')
                    # server 端：7 片 206（6×128KiB + 1×32KiB = 819200B）
                    n206 = len(re.findall(r'\[data\].*206\)', srv.log_text()))
                    rep.expect('H4 server 日志（≥7 片 206）', n206 >= 7,
                               f'{n206} 片 Range 请求')
                    # 设备侧：拉回落盘文件，sha256 三方一致
                    pull_and_verify_sha(rep, hdc)
                else:
                    rep.case('H4 定位下载按钮（⬇）', 'FAIL',
                             '未找到 report.pdf 行内 ⬇ 按钮（可能需滚动）')
        except HdcError as e:
            rep.case('H4 触发下载', 'FAIL', str(e)[:200])

        # ---- 停止应用（还原现场） ----
        if launched:
            hdc.shell(f'aa force-stop {BUNDLE}', timeout=30)
        return True
    finally:
        srv.stop()


def pull_and_verify_sha(rep: Report, hdc: Hdc):
    """best-effort：从应用沙箱拉回 downloads/report.pdf 校验 sha256（不可读则 SKIP）"""
    candidates = [
        f'/data/app/el2/100/base/{BUNDLE}/haps/entry/files/downloads/report.pdf',
        f'/data/storage/el2/base/files/downloads/report.pdf',  # 不太可能（相对沙箱）
    ]
    remote = None
    for c in candidates:
        code, out = hdc.shell(f'ls -l {c}', timeout=10)
        if code == 0 and 'report.pdf' in out:
            remote = c
            break
    if remote is None:
        rep.case('H4 落盘文件拉回校验', 'SKIP',
                 '应用沙箱 downloads 不可读（模拟器权限策略），UI sha256 校验已覆盖')
        return
    local = os.path.join(LOG_DIR, 'report.pdf.device')
    code, out = hdc.recv(remote, local, timeout=30)
    if code != 0 or not os.path.exists(local):
        rep.case('H4 落盘文件拉回校验', 'SKIP', f'hdc file recv 失败: {out[:80]}')
        return
    sha = hashlib.sha256()
    with open(local, 'rb') as f:
        sha.update(f.read())
    actual = sha.hexdigest()
    expected = expected_report_sha256()
    rep.expect('H4 落盘文件 sha256 三方一致', actual == expected,
               f'{actual[:12]}… == {expected[:12]}…')


# ============================================================
# 主入口
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser(description='StorPool 完整 e2e 流程')
    ap.add_argument('--phase', choices=['http', 'device', 'heal401', 'all'],
                    default='all')
    ap.add_argument('--serial', help='hdc 目标（多设备必填）')
    ap.add_argument('--hap', help='覆盖默认 HAP 路径')
    ap.add_argument('--skip-build', action='store_true', help='复用已有 HAP')
    ap.add_argument('--json', dest='json_out', help='机器可读报告输出路径')
    ap.add_argument('--hb-wait', type=int, default=22,
                    help='第 2 次心跳等待上限秒数（默认 22s）')
    ap.add_argument('--run-slow', action='store_true',
                    help='heal401 30s 冷却时序真实等待（默认 SKIP 标记）')
    ap.add_argument('--no-auto-emulator', action='store_true',
                    help='无设备时不尝试自动启动模拟器实例')
    args = ap.parse_args()

    print(f'StorPool e2e — phase={args.phase} bundle={BUNDLE}')
    os.makedirs(LOG_DIR, exist_ok=True)
    rep = Report()
    device_ok = True

    try:
        if args.phase in ('http', 'all'):
            http_ok = phase_http(rep)
            if args.phase == 'http':
                device_ok = False  # 仅 http 模式不算 device 结果
        if args.phase in ('device', 'all', 'heal401'):
            # 设备解析（BLOCKED 语义：环境不可用，非脚本失败）
            hdc = None
            try:
                hdc = Hdc.pick(args.serial)
            except HdcError as e:
                if args.no_auto_emulator:
                    rep.case('设备在线', 'BLOCKED', str(e))
                else:
                    print(f'  [device] {e} → 尝试自动启动模拟器…')
                    if try_start_emulator():
                        try:
                            hdc = Hdc.pick(args.serial)
                        except HdcError as e2:
                            rep.case('设备在线（模拟器拉起后）', 'BLOCKED', str(e2))
                    else:
                        rep.case('设备在线', 'BLOCKED',
                                 '无设备且模拟器拉起失败（镜像/实例未就绪？）')
            if hdc is not None:
                rep.case('设备在线', 'PASS', f'serial={hdc.serial or "(唯一)"}')
                args.hap = args.hap  # noqa
                import heal401  # 延迟导入：heal401 → e2e 单向依赖，避免循环
                if args.phase == 'heal401':
                    device_ok = heal401.phase_heal401(rep, hdc, args)
                else:
                    device_ok = phase_device(rep, hdc, args)
                    # P0-2 401 自愈场景追加（失败经 rep FAIL 计数反映，
                    # 不截断 device 阶段其余用例的执行）
                    heal401.phase_heal401(rep, hdc, args)
    except KeyboardInterrupt:
        print('\n[中断] 清理中…')
        device_ok = False
    finally:
        pass

    # ---- 汇总 ----
    summary = rep.to_dict()
    print('\n' + '=' * 60)
    print(f"e2e 结果: {summary['summary']['PASS']} passed / "
          f"{summary['summary']['FAIL']} failed / "
          f"{summary['summary']['BLOCKED']} blocked / "
          f"{summary['summary']['SKIP']} skipped"
          f"（{summary['durationSec']}s）")
    for c in rep.cases:
        if c['status'] != 'PASS':
            print(f"  · [{c['status']}] {c['name']} — {c['detail'][:120]}")

    if args.json_out:
        with open(args.json_out, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f'报告: {args.json_out}')
    print(f'日志目录: {LOG_DIR}')

    if summary['summary']['BLOCKED']:
        return 3
    return 0 if (summary['summary']['FAIL'] == 0 and device_ok) else 1


if __name__ == '__main__':
    sys.exit(main())