#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-2 401 自愈设备层 e2e 用例（2026-10-19 round 4 · 机器 C：feat/e2e-heal401）

补运行期验证空白：selftest 18 例只覆盖协议层 401 语义，「设备心跳收到 401 →
清身份 → 重注册 → 30s 冷却」整条 app 链路此前仅有 H3 期手工验证记录，
评审 P0-2 缺一条常备运行期回归用例。

场景编排（对应 Index.ets doHeartbeat / selfHeal / reRegister 实现）：
  1. 全新安装启动 → 注册成功（deviceId₁/token₁，UI serverTime₁）
  2. 心跳运行中（server₁ heartbeatCount ≥ 1；startHeartbeat 立即执行一次）
  3. 重启 mock server（杀进程重起：TOKENS 内存字典清空，token₁ 失效）
  4. 下一次心跳（≤15s）带 token₁ → 401 → selfHeal（首次无冷却）：
     清身份 → reRegister → 注册成功（deviceId₂，UI serverTime₂ > serverTime₁）
     → startHeartbeat 立即心跳 → server₂ heartbeatCount 恢复增长
  5. [默认 SKIP / --run-slow 真实跑] 30s 冷却时序：重注册后立即再重启
     server → 窗口内下一次心跳 401 只写「自愈冷却中（30s）」不重注册
     （防风暴）；窗口过期后下一次 401 才真正重注册并恢复心跳。

断言全部走 uitest dumpLayout 文本匹配 + server /devices 状态，
不依赖 Index.ets 代码结构；仅 Python 标准库；复用 hapctl.py 原语与
e2e.py 的 MockServer / wait_for / ensure_screen_awake。

用法（经 e2e.py 编排）：
  python e2e.py --phase heal401            # 单独跑本场景
  python e2e.py --phase all                # http + device + heal401
  python e2e.py --phase heal401 --run-slow # 冷却时序真实等待 30s
单场景净时长 ≤90s（不含 HAP 构建；默认模式心跳等待 ~15s×2 + 注册/重启开销）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time

import e2e  # 同目录：MockServer / wait_for / ensure_screen_awake / BASE
from hapctl import (ABILITY, BUNDLE, DEFAULT_HAP, Hdc, HdcError, UiTree,
                    run)

REG_OK_RX = re.compile(r'注册成功（serverTime=(\d+)）')
COOLDOWN_RX = re.compile(r'自愈冷却中（30s）')
HEARTBEAT_INTERVAL_S = 15  # Index.ets HEARTBEAT_INTERVAL_MS = 15000
COOLDOWN_WINDOW_S = 30     # Index.ets selfHeal 冷却阈值


def _harmonyos_devices(srv: 'e2e.MockServer') -> dict:
    """server /devices 中 platform=harmonyos 的 {deviceId: info}"""
    return {k: v for k, v in srv.devices().items()
            if v.get('platform') == 'harmonyos'}


def _wait_heartbeat(srv: 'e2e.MockServer', min_count: int, timeout: int) -> int:
    """轮询 server 直到任一 harmonyos 设备 heartbeatCount ≥ min_count（返回最大值）"""
    deadline = time.time() + timeout
    best = 0
    while time.time() < deadline:
        devs = _harmonyos_devices(srv)
        counts = [int(v.get('heartbeatCount') or 0) for v in devs.values()]
        if counts:
            best = max(best, max(counts))
            if best >= min_count:
                return best
        time.sleep(2)
    return best


def _wait_re_register(ui: UiTree, first_server_time: int, timeout: int):
    """等待重注册成功的 UI 文案（serverTime 严格大于基准，区分新旧注册）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ui.dump()
        except (HdcError, json.JSONDecodeError, OSError):
            time.sleep(1.5)  # dump 偶发失败 → 重试（与 e2e.wait_for 同语义）
            continue
        for n in ui.find_text(REG_OK_RX.pattern):
            if ui.visible(n):
                m = REG_OK_RX.search(n.text or '')
                if m and int(m.group(1)) > first_server_time:
                    return n
        time.sleep(1.5)
    return None


def phase_heal401(rep, hdc: Hdc, args) -> bool:
    """P0-2 401 自愈主场景。返回 False 表示场景中断（已记 FAIL/BLOCKED 用例）。"""
    print('\n== 阶段 heal401：P0-2 401 自愈（设备层） ==')

    # ---- 0. HAP 就绪（device 阶段刚构建则复用；不存在才构建） ----
    hap = args.hap or DEFAULT_HAP
    if not os.path.exists(hap):
        cli = shutil.which('devecocli') or shutil.which('devecocli.cmd')
        if not cli:
            rep.case('heal401 HAP 构建', 'BLOCKED', 'devecocli 不可用且无现成 HAP')
            return False
        code, out = run([cli, 'build', '--build-mode', 'debug'], timeout=900)
        ok = code == 0 and os.path.exists(hap)
        rep.expect('heal401 HAP 构建', ok,
                   f'{os.path.getsize(hap) // 1024} KiB' if ok else
                   '\n'.join(out.splitlines()[-5:])[-400:])
        if not ok:
            return False

    srv = e2e.MockServer('heal401')
    if not srv.start():
        srv.stop()
        rep.case('heal401 server 启动（首轮）', 'FAIL', f'详见 {srv.log_path}')
        return False
    rep.expect('heal401 server 启动（首轮）', True, e2e.BASE)

    launched = False
    try:
        e2e.ensure_screen_awake(hdc)

        # ---- 1. 全新安装 + 启动（卸载保证本地身份清零，必走全新注册路径） ----
        hdc.uninstall()
        code, out = hdc.install(hap)
        ok = code == 0 and hdc.is_installed()
        rep.expect('heal401 安装 HAP（卸载重装）', ok,
                   out.strip().splitlines()[-1][:80] if out.strip() else '')
        if not ok:
            return False

        code, out = hdc.shell(f'aa start -b {BUNDLE} -a {ABILITY}', timeout=30)
        rep.expect('heal401 启动应用', code == 0, out.strip()[:100])
        if code != 0:
            return False
        launched = True

        ui = UiTree(hdc)

        # ---- 2. 全新注册成功 → 记录 serverTime₁ / deviceId₁ ----
        reg1 = e2e.wait_for(ui, REG_OK_RX.pattern, timeout=25)
        if reg1 is None:
            rep.expect('heal401 全新注册成功（UI）', False,
                       '25s 内未见「注册成功（serverTime=…）」')
            return False
        m = REG_OK_RX.search(reg1.text or '')
        server_time_1 = int(m.group(1)) if m else 0
        rep.expect('heal401 全新注册成功（UI）', True, reg1.text)

        dev1: dict = {}
        for _ in range(10):
            dev1 = _harmonyos_devices(srv)
            if dev1:
                break
            time.sleep(1)
        device_id_1 = next(iter(dev1), '')
        rep.expect('heal401 注册可见（server /devices）', bool(dev1),
                   device_id_1 or 'server 未见 harmonyos 设备')
        if not dev1:
            return False

        # ---- 3. 心跳运行中（未建立则 401 不会发生，后续断言无意义） ----
        hb1 = _wait_heartbeat(srv, min_count=1, timeout=15)
        rep.expect('heal401 心跳运行中（server heartbeatCount ≥1）',
                   hb1 >= 1, f'heartbeatCount={hb1}')
        if hb1 < 1:
            return False

        # ---- 4. 重启 mock server：TOKENS 内存丢失，token₁ 失效 ----
        srv.stop()
        restart_ok = srv.start()
        rep.expect('heal401 mock server 重启（token 内存丢失）', restart_ok, e2e.BASE)
        if not restart_ok:
            return False

        # ---- 5. 401 自愈：下一次心跳（≤15s）→ 401 → 清身份 → 重注册 ----
        # 首次 401 无冷却；重注册成功 + startHeartbeat 立即执行一次心跳
        reg2 = _wait_re_register(ui, server_time_1,
                                 timeout=HEARTBEAT_INTERVAL_S + 30)
        rep.expect('heal401 401 自愈重注册（UI serverTime₂ > serverTime₁）',
                   reg2 is not None,
                   reg2.text if reg2 else
                   f'{HEARTBEAT_INTERVAL_S + 30}s 内未见 serverTime 新于 '
                   f'{server_time_1} 的注册成功文案')
        if reg2 is None:
            return False
        m2 = REG_OK_RX.search(reg2.text or '')
        server_time_2 = int(m2.group(1)) if m2 else server_time_1

        # ---- 6. deviceId 轮换（server₂ 分配新身份） ----
        dev2: dict = {}
        for _ in range(10):
            dev2 = _harmonyos_devices(srv)
            if dev2:
                break
            time.sleep(1)
        device_id_2 = next(iter(dev2), '')
        rep.expect('heal401 deviceId 轮换（deviceId₂ ≠ deviceId₁）',
                   bool(device_id_2) and device_id_2 != device_id_1,
                   f'{device_id_1 or "(无)"} → {device_id_2 or "(server 未见)"}')

        # ---- 7. 30s 冷却时序 / 心跳恢复增长（两条路径，见模块 docstring） ----
        if getattr(args, 'run_slow', False):
            # 冷却场景须卡在首次 selfHeal（≈serverTime₂ 前 15s）+30s 窗口内
            # 触发二次 401：跳过「心跳 ≥2」断言直接重启 server
            phase_heal401_cooldown(rep, ui, srv, server_time_2)
        else:
            hb2 = _wait_heartbeat(srv, min_count=2,
                                  timeout=HEARTBEAT_INTERVAL_S + 15)
            rep.expect('heal401 心跳恢复增长（server heartbeatCount ≥2）',
                       hb2 >= 2, f'heartbeatCount={hb2}')
            rep.case('heal401 冷却时序（30s 防风暴窗口）', 'SKIP',
                     '真实等待 30s+，加 --run-slow 启用（冷却窗口内二次 401 '
                     '只写状态不重注册；窗口过期后下一次 401 才恢复重注册）')

        return True
    finally:
        if launched:
            hdc.shell(f'aa force-stop {BUNDLE}', timeout=30)
        srv.stop()


def phase_heal401_cooldown(rep, ui: UiTree, srv: 'e2e.MockServer',
                           server_time_2: int) -> None:
    """30s 冷却时序（--run-slow）：窗口内二次 401 只写状态不重注册（防风暴）；
    窗口过期后下一次 401 心跳才真正重注册并恢复心跳。"""
    srv.stop()
    if not srv.start():
        rep.case('heal401 冷却时序（30s 防风暴窗口）', 'FAIL', 'server 重启失败')
        return
    # 下一次心跳（≤15s）带 token₂ → 401 → selfHeal 落在冷却窗口内
    cd = e2e.wait_for(ui, COOLDOWN_RX.pattern,
                      timeout=HEARTBEAT_INTERVAL_S + 10)
    rep.expect('heal401 冷却时序（30s 窗口内 401 不重注册）', cd is not None,
               cd.text if cd else
               f'{HEARTBEAT_INTERVAL_S + 10}s 内未见「自愈冷却中（30s）」文案')

    # 窗口过期后恢复：下一次 401 心跳（最迟 +15s）→ 真正重注册（serverTime₃）
    reg3 = _wait_re_register(ui, server_time_2,
                             timeout=COOLDOWN_WINDOW_S + HEARTBEAT_INTERVAL_S + 15)
    rep.expect('heal401 冷却过期后重注册（UI serverTime₃ > serverTime₂）',
               reg3 is not None,
               reg3.text if reg3 else '冷却窗口过期后未见恢复重注册')
    hb3 = _wait_heartbeat(srv, min_count=1,
                          timeout=HEARTBEAT_INTERVAL_S + 10)
    rep.expect('heal401 冷却过期后心跳恢复（heartbeatCount ≥1）',
               hb3 >= 1, f'heartbeatCount={hb3}')