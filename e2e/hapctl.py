#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StorPool HAP 安装/uitest 设备控制脚本（P1-1）

分层定位（三件套）：
  mock-control-plane/selftest.py — 协议层：mock 端点 HTTP 语义自检（18 用例，独立可跑）
  e2e/hapctl.py                 — 设备层：HAP 构建/安装/启动/停止 + uitest 驱动原语（本文件）
  e2e/e2e.py                    — 流程层：完整 e2e 编排（H0b→注册→心跳→同步→保活→H4 分片下载）

本脚本可独立使用（子命令），也可被 e2e.py 作为库导入（Hdc / UiTree / hapctl 常量）。

子命令：
  build                     构建未签名 debug HAP（devecocli build；模拟器接受未签名包）
  install [--hap PATH]      卸载旧包 → 安装 HAP（默认 entry-default-unsigned.hap）
  launch                    启动应用（aa start EntryAbility）
  stop                      停止应用（aa force-stop）
  uninstall                 卸载应用
  devices                   列出在线设备（hdc list targets）
  dump                      抓取 uitest 布局树，打印文本节点摘要
  find --text PAT           按文本（正则）查找控件，打印 text/bounds
  click --text PAT          按文本点击（自动滚动查找，见 scroll-find 语义）
  click --xy X Y            按坐标点击
  swipe --x1 --y1 --x2 --y2 [--ms]     滑动
  smoke                     冒烟验证：安装→启动→校验四个区块标题 + H0b 全通过 + 注册成功

通用选项：
  --serial <target>         指定 hdc 目标（多设备场景；默认自动取唯一在线设备）
  --hap <path>              覆盖默认 HAP 路径
  --screen-height N         覆盖自动探测的屏幕高度（滚动步长用）

用法示例：
  python hapctl.py smoke
  python hapctl.py click --text "同步目录树"
  python hapctl.py click --row-of "report.pdf" --text "⬇"     # 同一行内点击 ⬇（H4 下载入口）

依赖：hdc（DevEco toolchains，已入 PATH）、uitest（设备端，API 9+）、构建需 devecocli。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

# ---------------- 常量（与仓库结构对齐） ----------------

BUNDLE = 'com.storpool.harmony'
ABILITY = 'EntryAbility'

# e2e/ 的上级即仓库根
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_HAP = os.path.join(REPO, 'entry', 'build', 'default',
                           'outputs', 'default', 'entry-default-unsigned.hap')

LAYOUT_REMOTE = '/data/local/tmp/storpool_layout.json'

# 主页四个区块标题（Index.ets SectionTitle；smoke 校验锚点）
SECTION_TITLES = [
    '① H0b — NAPI 桥接验证',
    '② H1 — 设备注册与控制面心跳',
    '③ H2 — 元数据同步（虚拟目录树）',
    '④ H3 — 后台长时任务保活',
]

# H0b 全通过文案（Index.ets runNapiChecks）
NAPI_OK_TEXT = '全部通过 4/4'


# ---------------- 通用执行封装 ----------------

def run(cmd, timeout=60):
    """执行外部命令，返回 (returncode, 合并输出)。找不到命令返回 127，超时 124。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except subprocess.TimeoutExpired:
        return 124, f'[TIMEOUT {timeout}s] ' + ' '.join(str(c) for c in cmd)
    except FileNotFoundError:
        return 127, f'[NOT FOUND] {cmd[0]}'


# ---------------- hdc 封装 ----------------

class HdcError(Exception):
    pass


class Hdc:
    """hdc 命令封装（支持 -t 多目标）"""

    def __init__(self, serial: str | None = None):
        self.serial = serial

    def _base(self):
        return ['hdc'] + (['-t', self.serial] if self.serial else [])

    @staticmethod
    def targets() -> list:
        """在线设备 serial 列表（过滤 [Empty]/失败行）"""
        _, out = run(['hdc', 'list', 'targets'], timeout=15)
        serials = []
        for line in (out or '').splitlines():
            s = line.strip()
            if not s or s in ('[Empty]', 'Empty', 'No devices found.'):
                continue
            # "Attached EOF" 等状态行跳过；serial 形如 127.0.0.1:5555 / emulator-xxx / 真机 SN
            if s.startswith('[') or s.lower().startswith(('attached', 'failed')):
                continue
            serials.append(s)
        return serials

    @classmethod
    def pick(cls, want: str | None = None) -> 'Hdc':
        """选择设备：显式指定 > 唯一在线 > 报错（多设备需 --serial）"""
        serials = cls.targets()
        if want:
            if want not in serials:
                raise HdcError(f'设备 {want} 不在线；当前在线: {serials or "无"}')
            return cls(want)
        if len(serials) == 1:
            return cls(serials[0])
        if not serials:
            raise HdcError('无在线设备（hdc list targets 为空）。'
                           '启动模拟器后重试，或用 devecocli emulator start <name>。')
        raise HdcError(f'多台设备在线 {serials}，请用 --serial 指定目标。')

    def shell(self, cmd, timeout=30):
        return run(self._base() + ['shell', cmd], timeout=timeout)

    def shell_ok(self, cmd, timeout=30):
        code, out = self.shell(cmd, timeout=timeout)
        if code != 0:
            raise HdcError(f'hdc shell 失败（{code}）: {cmd}\n{out}')
        return out

    def install(self, hap_path, timeout=180):
        return run(self._base() + ['install', hap_path], timeout=timeout)

    def uninstall(self, bundle=BUNDLE, timeout=90):
        return run(self._base() + ['uninstall', bundle], timeout=timeout)

    def recv(self, remote_path, local_path, timeout=30):
        return run(self._base() + ['file', 'recv', remote_path, local_path],
                   timeout=timeout)

    def is_installed(self, bundle=BUNDLE) -> bool:
        code, out = self.shell(f'bm dump -n {bundle}', timeout=30)
        return code == 0 and bundle in out


# ---------------- uitest 布局树 ----------------

class UiNode:
    """layout 树节点（attributes 兼容 attributes/attribute 两种键名）"""

    __slots__ = ('attrs', 'children')

    def __init__(self, raw: dict):
        self.attrs = raw.get('attributes') or raw.get('attribute') or {}
        self.children = [UiNode(c) for c in raw.get('children', [])]

    @property
    def text(self) -> str:
        return str(self.attrs.get('text') or '')

    @property
    def type(self) -> str:
        return str(self.attrs.get('type') or '')

    @property
    def bounds_str(self) -> str:
        return str(self.attrs.get('bounds') or '')

    def bounds(self):
        """解析 "[l,t][r,b]" → (l, t, r, b)"""
        m = re.fullmatch(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]',
                         self.bounds_str.strip())
        if not m:
            return None
        l, t, r, b = (int(x) for x in m.groups())
        return (l, t, r, b)

    def center(self):
        b = self.bounds()
        if b is None:
            return None
        l, t, r, btm = b
        return ((l + r) // 2, (t + btm) // 2)

    def iter_all(self):
        yield self
        for c in self.children:
            yield from c.iter_all()


class UiTree:
    """uitest dumpLayout 封装：抓取/解析/查找/滚动查找"""

    def __init__(self, hdc: Hdc):
        self.hdc = hdc
        self.root: UiNode | None = None
        self.screen = None  # (w, h)

    def dump(self) -> 'UiTree':
        """抓取布局树（dumpLayout -p 指定路径 → file recv → 解析）"""
        self.hdc.shell(f'rm -f {LAYOUT_REMOTE}', timeout=10)
        code, out = self.hdc.shell(f'uitest dumpLayout -p {LAYOUT_REMOTE}',
                                   timeout=40)
        out_all = out
        if code != 0 or 'finish' not in out_all.lower():
            # 老版本 fallback：默认路径 dump（部分镜像不支持 -p）
            code2, out2 = self.hdc.shell('uitest dumpLayout', timeout=40)
            out_all += out2
        local = os.path.join(tempfile.gettempdir(), 'storpool_layout.json')
        if os.path.exists(local):
            os.remove(local)
        rcode, rout = self.hdc.recv(LAYOUT_REMOTE, local, timeout=30)
        if rcode != 0 or not os.path.exists(local):
            raise HdcError(f'layout 拉取失败: {rout or out_all!r}')
        with open(local, encoding='utf-8') as f:
            self.root = UiNode(json.load(f))
        b = self.root.bounds()
        if b:
            self.screen = (b[2] - b[0], b[3] - b[1])
        return self

    def find_text(self, pattern) -> list:
        """按文本正则查找，返回命中节点列表（含空文本跳过）"""
        rx = re.compile(pattern)
        if self.root is None:
            return []
        return [n for n in self.root.iter_all()
                if n.text and rx.search(n.text)]

    def first_text(self, pattern) -> UiNode | None:
        hits = self.find_text(pattern)
        return hits[0] if hits else None

    def visible(self, node: UiNode) -> bool:
        """bounds 与屏幕矩形相交且面积非零（屏外/零尺寸不可点）"""
        b = node.bounds()
        if b is None:
            return False
        l, t, r, btm = b
        if r <= l or btm <= t:
            return False
        if self.screen:
            w, h = self.screen
            return l < w and t < h and r > 0 and btm > 0
        return True

    def smallest_subtree_with(self, patterns) -> UiNode | None:
        """找同时包含全部 patterns 文本的最小子树节点（同行定位用）"""
        rxs = [re.compile(p) for p in patterns]

        def has_all(node: UiNode) -> bool:
            texts = [n.text for n in node.iter_all() if n.text]
            return all(any(rx.search(t) for t in texts) for rx in rxs)

        if self.root is None or not has_all(self.root):
            return None
        node = self.root
        changed = True
        while changed:  # 逐层下沉：找到仍满足 has_all 的最深子节点
            changed = False
            for c in node.children:
                if has_all(c):
                    node = c
                    changed = True
                    break
        return node

    # ---- 驱动原语 ----

    def click_xy(self, x, y) -> str:
        code, out = self.hdc.shell(f'uitest uiInput click {int(x)} {int(y)}',
                                   timeout=20)
        if code != 0:
            raise HdcError(f'click ({x},{y}) 失败: {out}')
        return out

    def click_node(self, node: UiNode):
        c = node.center()
        if c is None:
            raise HdcError(f'节点无可点坐标: text={node.text!r} '
                           f'bounds={node.bounds_str!r}')
        self.click_xy(*c)
        return c

    def swipe(self, x1, y1, x2, y2, ms=400) -> str:
        code, out = self.hdc.shell(
            f'uitest uiInput swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} '
            f'{int(ms)}', timeout=20)
        if code != 0:
            raise HdcError(f'swipe 失败: {out}')
        return out

    def swipe_up(self, fraction=0.45, ms=400):
        """页面上滑（内容向下滚动）：起点 55% 屏高 → 终点 (55-fraction*100)% 屏高"""
        h = self.screen[1] if self.screen else 2340
        w = self.screen[0] if self.screen else 1080
        self.swipe(w // 2, int(h * 0.55), w // 2, int(h * (0.55 - fraction)), ms)

    def scroll_find(self, pattern, max_swipes=8, settle=0.8):
        """滚动查找（不点击）：可见即返回；屏外/未找到则上滑重试。超时 None。"""
        for _ in range(max_swipes + 1):
            self.dump()
            hits = self.find_text(pattern)
            vis = [n for n in hits if self.visible(n)]
            if vis:
                return vis[0]
            if not hits:
                self.swipe_up()
            else:  # 找到但屏外 → 滚动
                b = hits[0].bounds()
                h = self.screen[1] if self.screen else 2340
                if b and b[1] > h:  # 在屏幕下方 → 上滑
                    self.swipe_up()
                else:  # 在上方 → 下滑回滚
                    w = self.screen[0] if self.screen else 1080
                    self.swipe(w // 2, int(h * 0.3), w // 2, int(h * 0.7))
            time.sleep(settle)
        return None

    def scroll_find_click(self, pattern, max_swipes=8, settle=0.8) -> UiNode:
        """滚动查找并点击：可见即点；不可见则上滑重试。返回被点节点。"""
        node = self.scroll_find(pattern, max_swipes, settle)
        if node is None:
            raise HdcError(f'scroll_find_click 未找到可点击目标: {pattern}')
        self.click_node(node)
        return node


# ---------------- 设备操作（构建/安装/启动/停止） ----------------

def cmd_build(_args) -> int:
    print('[build] devecocli build --build-mode debug …')
    code, out = run(['devecocli.cmd' if os.name == 'nt' else 'devecocli',
                     'build', '--build-mode', 'debug'],
                    timeout=900, )
    tail = '\n'.join(out.splitlines()[-8:])
    print(tail)
    if code != 0 or 'BUILD SUCCESSFUL' not in out:
        print('[build] FAILED')
        return 1
    if not os.path.exists(DEFAULT_HAP):
        print(f'[build] 构建成功但未找到产物: {DEFAULT_HAP}')
        return 1
    size = os.path.getsize(DEFAULT_HAP)
    print(f'[build] OK -> {DEFAULT_HAP} ({size/1024:.0f} KiB)')
    return 0


def cmd_devices(_args) -> int:
    serials = Hdc.targets()
    if not serials:
        print('无在线设备。模拟器: devecocli emulator list / start <name>')
        return 1
    for s in serials:
        print(s)
    return 0


def hap_path_of(args) -> str:
    p = args.hap or DEFAULT_HAP
    if not os.path.exists(p):
        raise HdcError(f'HAP 不存在: {p}（先 python hapctl.py build 或 --hap 指定）')
    return os.path.abspath(p)


def cmd_install(args) -> int:
    hdc = Hdc.pick(args.serial)
    hap = hap_path_of(args)
    print(f'[install] 目标设备: {hdc.serial or "(唯一)"}')
    hdc.uninstall()  # 幂等：未安装时忽略失败
    print(f'[install] 安装 {hap}')
    code, out = hdc.install(hap)
    print(out.strip()[:500])
    ok = code == 0 and ('success' in out.lower() or 'ok' in out.lower()
                        or 'install' in out.lower() and 'fail' not in out.lower())
    if not ok:
        print('[install] FAILED（未签名包仅模拟器可装；真机需签名）')
        return 1
    if not hdc.is_installed():
        print('[install] FAILED: bm dump 未找到包')
        return 1
    print(f'[install] OK: {BUNDLE}')
    return 0


def cmd_launch(args) -> int:
    hdc = Hdc.pick(args.serial)
    code, out = hdc.shell(f'aa start -b {BUNDLE} -a {ABILITY}', timeout=30)
    print(out.strip()[:300])
    ok = code == 0 and ('start' in out.lower() or 'ok' in out.lower())
    print(f'[launch] {"OK" if ok else "FAILED"}')
    return 0 if ok else 1


def cmd_stop(args) -> int:
    hdc = Hdc.pick(args.serial)
    hdc.shell(f'aa force-stop {BUNDLE}', timeout=30)
    print(f'[stop] {BUNDLE}（force-stop 已发送）')
    return 0


def cmd_uninstall(args) -> int:
    hdc = Hdc.pick(args.serial)
    code, out = hdc.uninstall()
    print(out.strip()[:300])
    print(f'[uninstall] code={code}')
    return 0


def cmd_dump(args) -> int:
    hdc = Hdc.pick(args.serial)
    ui = UiTree(hdc).dump()
    print(f'screen={ui.screen} 节点数={sum(1 for _ in ui.root.iter_all())}')
    for n in ui.root.iter_all():
        if n.text:
            print(f'  [{n.type}] {n.text!r} @{n.bounds_str}')
    return 0


def cmd_find(args) -> int:
    hdc = Hdc.pick(args.serial)
    ui = UiTree(hdc).dump()
    hits = ui.find_text(args.text)
    if not hits:
        print(f'未找到文本匹配: {args.text}')
        return 1
    for n in hits:
        vis = '可见' if ui.visible(n) else '屏外'
        print(f'[{n.type}] {n.text!r} @{n.bounds_str} {vis} center={n.center()}')
    return 0


def cmd_click(args) -> int:
    hdc = Hdc.pick(args.serial)
    ui = UiTree(hdc)
    if args.xy:
        x, y = args.xy
        ui.click_xy(x, y)
        print(f'[click] ({x},{y})')
        return 0
    pattern = args.text
    if not pattern:
        print('需要 --text PAT 或 --xy X Y')
        return 2
    if args.row_of:
        # 同行定位：先找包含 row_of 与 pattern 的最小子树，再点子树内 pattern 节点
        ui.dump()
        sub = ui.smallest_subtree_with([args.row_of, pattern])
        if sub is None:
            print(f'未找到同时包含 {args.row_of!r} 与 {pattern!r} 的容器')
            return 1
        rx = re.compile(pattern)
        target = next((n for n in sub.iter_all() if n.text and rx.search(n.text)),
                      None)
        if target is None:
            print('容器内未找到目标节点（异常）')
            return 1
        if not ui.visible(target):
            # 滚动到目标行再重定位
            node = ui.scroll_find_click(args.row_of)
            print(f'[click] 已滚动点击 {node.text!r}（行内目标请重试本命令）')
            return 0
        c = ui.click_node(target)
        print(f'[click] {target.text!r} @ {c}')
        return 0
    node = ui.scroll_find_click(pattern)
    print(f'[click] {node.text!r} @ {node.center()}')
    return 0


def cmd_swipe(args) -> int:
    hdc = Hdc.pick(args.serial)
    UiTree(hdc).swipe(args.x1, args.y1, args.x2, args.y2, args.ms)
    print(f'[swipe] ({args.x1},{args.y1})→({args.x2},{args.y2}) {args.ms}ms')
    return 0


def cmd_smoke(args) -> int:
    """冒烟：起临时 server → 安装→启动→校验区块标题/H0b/注册（uitest 验证主路径）"""
    hdc = Hdc.pick(args.serial)

    def step(name, ok, detail=''):
        mark = 'PASS' if ok else 'FAIL'
        print(f'[{mark}] {name} {detail}')
        return ok

    results = []
    srv = _SmokerServer(8765)
    if not srv.start():
        print('[BLOCKED] mock server 启动失败（8765 被非 server 进程占用？）')
        return 3
    try:
        hap = hap_path_of(args)
        hdc.uninstall()
        code, out = hdc.install(hap)
        results.append(step('安装 HAP', code == 0 and hdc.is_installed(),
                            out.strip().splitlines()[-1][:80] if out.strip() else ''))
        code, out = hdc.shell(f'aa start -b {BUNDLE} -a {ABILITY}', timeout=30)
        results.append(step('启动应用', code == 0))
        time.sleep(4)  # 等首帧 + H0b 同步检查 + 注册异步发起

        ui = UiTree(hdc)
        ui.dump()
        for title in SECTION_TITLES:
            # ③④ 区块在屏外 → scroll_find 自动滚动（滚动后整体重新 dump）
            found = (ui.find_text(re.escape(title))
                     and any(ui.visible(n) for n in ui.find_text(re.escape(title))))
            if not found:
                found = ui.scroll_find(re.escape(title)) is not None
            results.append(step(f'区块标题 {title[:6]}…', bool(found)))
        results.append(step('H0b NAPI 全通过',
                            ui.scroll_find(re.escape(NAPI_OK_TEXT), max_swipes=2)
                            is not None))
        # 注册是异步的：轮询最多 15s（server 已就绪）
        reg_ok = False
        reg_pat = r'注册成功（serverTime=\d+）|已注册（本地身份复用）'
        for _ in range(15):
            ui.dump()
            if ui.find_text(reg_pat):
                reg_ok = True
                break
            time.sleep(1)
        results.append(step('H1 注册成功', reg_ok))
    finally:
        srv.stop()

    ok_cnt = sum(1 for r in results if r)
    print(f'\nsmoke 结果: {ok_cnt}/{len(results)} passed')
    return 0 if ok_cnt == len(results) else 1


class _SmokerServer:
    """smoke 内嵌临时 mock server（独立于 e2e.MockServer，避免反向依赖）"""

    def __init__(self, port):
        self.port = port
        self.proc = None

    def start(self, wait=10) -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/health',
                                        timeout=2) as r:
                if r.status == 200:
                    return True  # 外部 server 已在跑，复用
        except OSError:
            pass
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        log = open(os.path.join(tempfile.gettempdir(), 'storpool_smoke_server.log'),
                   'w', encoding='utf-8')
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(REPO, 'mock-control-plane', 'server.py'),
             str(self.port)],
            cwd=os.path.join(REPO, 'mock-control-plane'),
            stdout=log, stderr=subprocess.STDOUT, env=env)
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f'http://127.0.0.1:{self.port}/health', timeout=2) as r:
                    if r.status == 200:
                        return True
            except OSError:
                pass
            time.sleep(0.4)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------- CLI ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description='StorPool HAP 安装/uitest 设备控制')
    ap.add_argument('--serial', help='hdc 目标 serial（多设备时必填）')
    ap.add_argument('--hap', help=f'HAP 路径（默认 {DEFAULT_HAP}）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('build').set_defaults(func=cmd_build)
    sub.add_parser('devices').set_defaults(func=cmd_devices)
    sub.add_parser('install').set_defaults(func=cmd_install)
    sub.add_parser('launch').set_defaults(func=cmd_launch)
    sub.add_parser('stop').set_defaults(func=cmd_stop)
    sub.add_parser('uninstall').set_defaults(func=cmd_uninstall)
    sub.add_parser('dump').set_defaults(func=cmd_dump)

    p_find = sub.add_parser('find')
    p_find.add_argument('--text', required=True, help='文本正则')
    p_find.set_defaults(func=cmd_find)

    p_click = sub.add_parser('click')
    p_click.add_argument('--text', help='文本正则（自动滚动查找）')
    p_click.add_argument('--xy', nargs=2, type=int, metavar=('X', 'Y'))
    p_click.add_argument('--row-of', dest='row_of',
                         help='同行定位：包含该文本的容器内点击 --text 目标')
    p_click.set_defaults(func=cmd_click)

    p_sw = sub.add_parser('swipe')
    p_sw.add_argument('--x1', type=int, required=True)
    p_sw.add_argument('--y1', type=int, required=True)
    p_sw.add_argument('--x2', type=int, required=True)
    p_sw.add_argument('--y2', type=int, required=True)
    p_sw.add_argument('--ms', type=int, default=400)
    p_sw.set_defaults(func=cmd_swipe)

    sub.add_parser('smoke').set_defaults(func=cmd_smoke)

    args = ap.parse_args()
    try:
        return args.func(args)
    except HdcError as e:
        print(f'[BLOCKED] {e}')
        return 3


if __name__ == '__main__':
    sys.exit(main())