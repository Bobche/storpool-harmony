#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-13 长稳 soak — rcp Session 缓存无泄漏运行期证明（round 5 · 机器 C）
round 6（P2-6）扩展：判定门参数化（--gates，arkts 默认纳入）+ 阶段 C 静置
观察段（--settle，判 ArkTS GC 惰性 vs 真泄漏）——arkts heap +62% 观察项深挖。

背景：feat/rcp-session（07c71a8，已合入 main）把 ControlPlaneClient 的
rcp.Session 改为模块级共享缓存 Map<baseUrl, Session>（惰性创建、close()
移除、POST 显式 stringify），设计上限 ≤2（按 baseUrl 键）。round 4 的 e2e
全部跑在旧 http 实现上，本脚本在 rcp 实现基线上为「Session 缓存无泄漏」
提供运行期证据，同时是任务指令 P1-13 的交付物。

编排（先 A 后 B，可选 C；任务指令要求先闭环 P1-11 再跑本脚本）：
  阶段 A（心跳 soak）：App 注册后保持运行 --minutes 分钟（默认 10），
      每 --sample-every 秒（默认 30）采样：pidof → hidumper --mem <pid>
      → PSS / native heap / ark ts heap + server heartbeatCount（活性旁证）。
      心跳契约 15s（Index.ets HEARTBEAT_INTERVAL_MS=15000）→ 30s ≈ 每 2 拍 1 采。
  阶段 B（401 自愈循环压力）：每轮「点同步目录树（普通 GET，sessionCache
      复用路径）→ 重启 mock server（token 失效 → 下一次心跳 401 →
      selfHeal 清身份重注册 = client.close() 移除 Session + 惰性重建）」。
      该循环反复走 rcp.Session 生命周期最敏感路径（round4 heal401 只跑
      1 次，本阶段反复 --ui-loops 轮，默认 10）。注：App 的「重新注册」
      按钮实为 ensureRegistration（本地身份复用，不发 register），
      不构成 Session 重建路径，故不采用（2026-10-19 实测）。
  阶段 C（静置观察，P2-6 新增，--settle 秒 >0 启用）：阶段 B 结束后
      不做任何 UI 操作，继续按 --sample-every 采样。用于区分「阶段 B 的
      arkts 每轮 +575kB 线性增长」是 GC 惰性（静置后回落/走平）还是
      真保留（不回落）。不入主判定门，仅作定性证据输出。

判定门（P2-6 参数化）：--gates pss,native,arkts（默认全开）。规则与
PSS 同源：>20% 涨幅 ∧ 后半均值>前半×1.15 ∧ 斜率正 → 疑似泄漏。
arkts 门命中 ≠ 定性结论——ArkTS 堆有预热/GC 惰性，最终定性
（warmup_converged / suspected_leak）需结合多 run 终值对照与静置段
形态人工裁决（分析见任务回报）。

用法：
  python e2e/soak.py                          # 10min 心跳 + 10 轮自愈循环
  python e2e/soak.py --minutes 12 --ui-loops 20
  python e2e/soak.py --sample-every 20        # 加密采样
  python e2e/soak.py --settle 120             # 阶段 B 后静置观察 2min
  python e2e/soak.py --gates pss,native       # arkts 只观测不入门
  python e2e/soak.py --json e2e/logs/soak.json
  python e2e/soak.py --skip-build             # 复用已有 HAP

依赖：hdc / uitest / devecocli（构建时）/ 仅 Python 标准库；复用
e2e.py（MockServer/wait_for/ensure_screen_awake）与 hapctl.py 原语。
独立入口，不接入 e2e.py --phase all（时长 15min+ 不适合常规回归）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import e2e  # noqa: E402  MockServer / wait_for / ensure_screen_awake
from hapctl import (ABILITY, BUNDLE, DEFAULT_HAP, Hdc, HdcError, UiTree,  # noqa: E402
                    run)

HEARTBEAT_INTERVAL_S = 15   # Index.ets HEARTBEAT_INTERVAL_MS = 15000
RX_REG_OK = re.compile(r'注册成功（serverTime=(\d+)）')

# hidumper --mem <pid> 输出解析（round5 实测格式）：
#   主表按类别行 + 汇总行；列序 = Pss Total / Shared Clean / ...（kB）
#   表头行「Total          Clean」为字母，数据行「Total          65433」为数字，
#   故 \d+ 锚定即可区分；第二段「native heap:」为冒号小节标题，不会匹配。
RX_MEM_TOTAL = re.compile(r'^\s*Total\s+(\d+)', re.M)
RX_MEM_NATIVE = re.compile(r'^\s*native heap\s+(\d+)', re.M)
RX_MEM_ARKTS = re.compile(r'^\s*ark ts heap\s+(\d+)', re.M)


# ============================================================
# 设备侧采样原语
# ============================================================

def app_pid(hdc: Hdc) -> int | None:
    code, out = hdc.shell(f'pidof {BUNDLE}', timeout=15)
    pid = (out or '').strip().split()[0] if (out or '').strip() else ''
    return int(pid) if pid.isdigit() else None


def sample_memory(hdc: Hdc, pid: int) -> dict | None:
    """hidumper --mem <pid> → {pss, native_heap, ark_ts_heap}（kB）；失败 None。"""
    code, out = hdc.shell(f'hidumper --mem {pid}', timeout=30)
    if code != 0 or not out:
        return None
    m_t = RX_MEM_TOTAL.search(out)
    if not m_t:  # 无汇总行 = 解析失败（格式变更/进程已死）
        return None
    m_n = RX_MEM_NATIVE.search(out)
    m_a = RX_MEM_ARKTS.search(out)
    return {
        'pss_kb': int(m_t.group(1)),
        'native_heap_kb': int(m_n.group(1)) if m_n else -1,
        'ark_ts_heap_kb': int(m_a.group(1)) if m_a else -1,
    }


def hb_max(srv: 'e2e.MockServer') -> int:
    """server /devices 中 harmonyos 设备的最大 heartbeatCount。"""
    devs = {k: v for k, v in srv.devices().items()
            if v.get('platform') == 'harmonyos'}
    counts = [int(v.get('heartbeatCount') or 0) for v in devs.values()]
    return max(counts) if counts else 0


# ============================================================
# 分析（纯函数，stdlib-only）
# ============================================================

def _slope_kb_per_min(ts: list[float], ps: list[int]) -> float:
    """最小二乘斜率（kB/min）；样本 <2 或时间跨度为 0 → 0。"""
    if len(ts) < 2 or max(ts) - min(ts) < 1:
        return 0.0
    n = len(ts)
    mt, mp = sum(ts) / n, sum(ps) / n
    den = sum((t - mt) ** 2 for t in ts)
    if den == 0:
        return 0.0
    return sum((t - mt) * (p - mp) for t, p in zip(ts, ps)) / den * 60.0


def analyze(samples: list[dict], key: str = 'pss_kb') -> dict:
    """泄漏判定：>20% 涨幅 且 后半均值 > 前半均值×1.15 且 斜率为正 → 疑似。"""
    vals = [s[key] for s in samples if s.get(key, -1) > 0]
    times = [s['t'] for s in samples if s.get(key, -1) > 0]
    if len(vals) < 3:
        return {'verdict': 'insufficient_samples', 'n': len(vals)}
    first, last = vals[0], vals[-1]
    delta = last - first
    delta_pct = delta / first * 100 if first else 0.0
    half = len(vals) // 2
    avg1 = sum(vals[:half]) / half
    avg2 = sum(vals[half:]) / (len(vals) - half)
    slope = _slope_kb_per_min(times, vals)
    monotone_rise = avg2 > avg1 * 1.15 and slope > 0
    suspected = delta_pct > 20 and monotone_rise
    return {
        'n': len(vals),
        'first_kb': first,
        'last_kb': last,
        'delta_kb': delta,
        'delta_pct': round(delta_pct, 2),
        'slope_kb_per_min': round(slope, 1),
        'first_half_avg_kb': round(avg1),
        'second_half_avg_kb': round(avg2),
        'verdict': 'suspected_leak' if suspected else 'no_leak_evidence',
    }


def print_table(title: str, samples: list[dict]) -> None:
    print(f'\n  -- {title} 内存时序（kB）--')
    print(f'  {"t(s)":>7} {"pid":>6} {"PSS":>8} {"native":>8} {"arkts":>8} {"hb":>4}')
    for s in samples:
        print(f'  {s["t"]:>7.0f} {s.get("pid") or "-":>6} {s["pss_kb"]:>8}'
              f' {s.get("native_heap_kb", -1):>8} {s.get("ark_ts_heap_kb", -1):>8}'
              f' {s.get("hb", -1):>4}')


# ============================================================
# 阶段 B：401 自愈循环（每轮走一遍 Session close→重建 最敏感路径）
# ============================================================

def _wait_re_register(ui: UiTree, base_server_time: int, timeout: int):
    """等自愈重注册成功的 UI 文案（serverTime 严格大于基准）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ui.dump()
        except (HdcError, json.JSONDecodeError, OSError):
            time.sleep(1.5)
            continue
        for n in ui.find_text(RX_REG_OK.pattern):
            if ui.visible(n):
                m = RX_REG_OK.search(n.text or '')
                if m and int(m.group(1)) > base_server_time:
                    return n
        time.sleep(1.5)
    return None


def ui_loop(hdc: Hdc, ui: UiTree, srv: 'e2e.MockServer', loops: int,
            samples: list[dict], t0: float) -> dict:
    """每轮：同步(GET 复用) → 锚定 serverTime → 重启 server → 等自愈 → 采样。

    heal 判定锚定：stop server 前 dump 一次 UI，把 base 抬到当前已显示的
    最大 serverTime——否则会匹配到上一轮遗留文案（冒烟实测教训）。
    sync 失败不计 failed：token 失效时同步 401 会先触发自愈（良性路径），
    Session 生命周期验证只依赖 heal 轮次。
    """
    failed = 0
    healed = 0
    last_server_time = 0
    pid = app_pid(hdc)
    for i in range(1, loops + 1):
        ok_sync = ok_heal = False
        # 1) 点「同步目录树」：普通 GET 路径（sessionCache 命中复用）
        try:
            ui.scroll_find_click(re.escape('同步目录树'))
            ok_sync = e2e.wait_for(ui, r'已同步（', timeout=18) is not None
        except HdcError:
            pass
        # 2) 锚定 UI 当前最大 serverTime（防止匹配旧文案）
        try:
            ui.dump()
            for n in ui.find_text(RX_REG_OK.pattern):
                m = RX_REG_OK.search(n.text or '')
                if m:
                    last_server_time = max(last_server_time, int(m.group(1)))
        except (HdcError, json.JSONDecodeError, OSError):
            pass
        # 3) 重启 mock server：TOKENS 内存丢失 → 下一次心跳（≤15s）401
        #    → selfHeal：清身份 → reRegister（POST register，走新 token）
        #    期间旧 client.close() 移除 Session，新请求惰性重建。
        srv.stop()
        if not srv.start():
            return {'ok': False, 'failed': failed, 'healed': healed,
                    'note': f'loop {i}: mock server 重启失败'}
        # 4) 等自愈重注册（UI serverTime 严格大于锚定值）
        n = _wait_re_register(ui, last_server_time, timeout=40)
        if n is not None:
            m = RX_REG_OK.search(n.text or '')
            if m:
                last_server_time = max(last_server_time, int(m.group(1)))
            ok_heal = True
            healed += 1
        else:
            failed += 1  # heal 是关键路径：Session close→重建未发生 = 异常
        # 5) 采样
        pid = app_pid(hdc) or pid
        mem = sample_memory(hdc, pid) if pid else None
        devs = {k: v for k, v in srv.devices().items()
                if v.get('platform') == 'harmonyos'}
        rec = {'loop': i, 't': round(time.time() - t0, 1), 'pid': pid,
               'sync': ok_sync, 'heal': ok_heal,
               'server_devices': len(devs),
               'hb': max((int(v.get('heartbeatCount') or 0)
                          for v in devs.values()), default=0)}
        if mem:
            rec.update(mem)
        else:
            rec.update({'pss_kb': -1, 'native_heap_kb': -1,
                        'ark_ts_heap_kb': -1})
        samples.append(rec)
        print(f'  [loop {i:>2}/{loops}] pss={rec["pss_kb"]}kB '
              f'native={rec["native_heap_kb"]}kB sync={int(ok_sync)} '
              f'heal={int(ok_heal)} devs={rec["server_devices"]}'
              + ('' if mem else '  ⚠ 采样失败'))
        if failed > loops * 0.3:
            return {'ok': False, 'failed': failed, 'healed': healed,
                    'note': f'自愈失败率过高（{failed}/{i}），中止'}
    return {'ok': failed == 0, 'failed': failed, 'healed': healed,
            'note': f'{loops} 轮完成（自愈 {healed}），失败 {failed}'}


# ============================================================
# 主流程
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser(description='P1-13 长稳 soak（心跳 + 内存采样）')
    ap.add_argument('--minutes', type=float, default=10.0,
                    help='阶段 A 心跳 soak 时长（分钟，默认 10）')
    ap.add_argument('--sample-every', type=int, default=30,
                    help='阶段 A 采样间隔（秒，默认 30 ≈ 每 2 拍心跳）')
    ap.add_argument('--ui-loops', type=int, default=10,
                    help='阶段 B 401 自愈循环轮数（默认 10）')
    ap.add_argument('--gates', default='pss,native,arkts',
                    help='泄漏判定门指标，逗号分隔 pss,native,arkts'
                         '（默认全开；未列出的指标仍采样/分析，只是不入'
                         '泄漏判定）')
    ap.add_argument('--settle', type=int, default=0,
                    help='阶段 C 静置观察时长（秒，默认 0 不跑；建议 120，'
                         '判阶段 B arkts 增长是 GC 惰性还是真保留）')
    ap.add_argument('--serial', help='hdc 目标（多设备必填）')
    ap.add_argument('--hap', help='覆盖默认 HAP 路径')
    ap.add_argument('--skip-build', action='store_true', help='复用已有 HAP')
    ap.add_argument('--json', dest='json_out', help='JSON 报告输出路径')
    args = ap.parse_args()

    gates = {g.strip().lower() for g in args.gates.split(',') if g.strip()}
    unknown = gates - {'pss', 'native', 'arkts'}
    if unknown:
        print(f'✗ --gates 含未知指标: {",".join(sorted(unknown))}'
              f'（可用：pss,native,arkts）')
        return 2
    print(f'StorPool soak（P1-13）— {args.minutes}min 心跳 + '
          f'{args.ui_loops} 轮 401 自愈循环，采样间隔 {args.sample_every}s，'
          f'判定门 [{",".join(g for g in ("pss", "native", "arkts") if g in gates)}]'
          + (f'，静置观察 {args.settle}s' if args.settle > 0 else ''))

    # ---- 设备 ----
    hdc = Hdc.pick(args.serial)
    print(f'设备: {hdc.serial}')
    e2e.ensure_screen_awake(hdc)

    # ---- HAP 就绪 ----
    hap = args.hap or DEFAULT_HAP
    if not (args.skip_build and os.path.exists(hap)):
        if not os.path.exists(hap):
            cli = shutil.which('devecocli') or shutil.which('devecocli.cmd')
            if not cli:
                print('✗ devecocli 不可用且无现成 HAP')
                return 2
            print('[build] devecocli build --build-mode debug …')
            code, out = run([cli, 'build', '--build-mode', 'debug'], timeout=900)
            if code != 0 or not os.path.exists(hap):
                print('✗ HAP 构建失败：\n' + '\n'.join(out.splitlines()[-5:]))
                return 2
    print(f'HAP: {hap}（{os.path.getsize(hap)//1024} KiB）')

    # ---- server + 全新安装启动 ----
    srv = e2e.MockServer('soak')
    if not srv.start():
        srv.stop()
        print('✗ mock server 启动失败')
        return 2
    ok_all = True
    try:
        hdc.uninstall()
        code, out = hdc.install(hap)
        if not (code == 0 and hdc.is_installed()):
            print('✗ HAP 安装失败: ' + (out or '').strip()[:120])
            return 2
        code, out = hdc.shell(f'aa start -b {BUNDLE} -a {ABILITY}', timeout=30)
        if code != 0:
            print('✗ 应用启动失败: ' + (out or '').strip()[:120])
            return 2
        ui = UiTree(hdc)
        reg = e2e.wait_for(ui, RX_REG_OK.pattern, timeout=30)
        if reg is None:
            print('✗ 30s 内未见注册成功文案')
            return 2
        print(f'注册成功: {reg.text}')
        # 心跳基线（startHeartbeat 注册后立即 1 拍）
        base_hb = 0
        deadline = time.time() + 20
        while time.time() < deadline:
            base_hb = hb_max(srv)
            if base_hb >= 1:
                break
            time.sleep(2)
        if base_hb < 1:
            print('✗ 20s 内 server 未见心跳（soak 前提不成立）')
            return 2
        pid = app_pid(hdc)
        print(f'心跳运行中（hb={base_hb}），pid={pid}，开始阶段 A')

        # ---- 阶段 A：心跳 soak ----
        t0 = time.time()
        samples_a: list[dict] = []
        t_end = t0 + args.minutes * 60
        next_t = time.time()
        pid_changes = 0
        last_pid = pid
        while True:
            now = time.time()
            if now >= t_end:
                break
            pid = app_pid(hdc)
            if pid is None:
                print(f'  ⚠ t={now-t0:.0f}s pidof 未命中（App 可能被杀）')
                samples_a.append({'t': round(now - t0, 1), 'pid': None,
                                  'pss_kb': -1, 'native_heap_kb': -1,
                                  'ark_ts_heap_kb': -1, 'hb': hb_max(srv)})
            else:
                if last_pid and pid != last_pid:
                    pid_changes += 1
                    print(f'  ⚠ pid 变化 {last_pid} → {pid}（曲线基线重置，已标注）')
                last_pid = pid
                mem = sample_memory(hdc, pid)
                rec = {'t': round(now - t0, 1), 'pid': pid, 'hb': hb_max(srv)}
                rec.update(mem or {'pss_kb': -1, 'native_heap_kb': -1,
                                   'ark_ts_heap_kb': -1})
                samples_a.append(rec)
                print(f'  [A t={rec["t"]:>6.0f}s] pss={rec["pss_kb"]}kB '
                      f'native={rec["native_heap_kb"]}kB '
                      f'arkts={rec["ark_ts_heap_kb"]}kB hb={rec["hb"]}')
            next_t += args.sample_every
            time.sleep(max(0.0, min(next_t - time.time(), t_end - time.time())))

        # 阶段 A 心跳活性定格（阶段 B 会重启 server，hb 计数清零）
        hb_final_a = hb_max(srv)
        t_a = time.time() - t0

        # ---- 阶段 B：401 自愈循环（Session close→重建 反复压力） ----
        print(f'\n== 阶段 B：401 自愈循环（{args.ui_loops} 轮，'
              f'每轮 = 同步 GET 复用 + server 重启触发自愈重注册）==')
        samples_b: list[dict] = []
        loop_stat = ui_loop(hdc, ui, srv, args.ui_loops, samples_b, t0)
        if not loop_stat['ok']:
            print(f'  ⚠ {loop_stat["note"]}')

        # 阶段 B 收尾：等最后一轮自愈后的心跳恢复（新设备 hb ≥ 2）
        hb_recovered = False
        deadline = time.time() + 40
        while time.time() < deadline:
            devs = {k: v for k, v in srv.devices().items()
                    if v.get('platform') == 'harmonyos'}
            latest = max((int(v.get('heartbeatCount') or 0)
                          for v in devs.values()), default=0)
            if latest >= 2:
                hb_recovered = True
                break
            time.sleep(3)

        # ---- 阶段 C：静置观察（P2-6，--settle >0 启用）----
        # 阶段 B 结束后不做任何 UI 操作，继续采样：arkts 若在静置期
        # 回落/走平 → GC 惰性佐证；维持高位不落 → 真保留嫌疑。
        samples_c: list[dict] = []
        if args.settle > 0:
            print(f'\n== 阶段 C：静置观察（{args.settle}s，无 UI 操作）==')
            c_end = time.time() + args.settle
            c_next = time.time()
            while True:
                now = time.time()
                if now >= c_end:
                    break
                pid = app_pid(hdc)
                if pid is None:
                    print('  ⚠ [C] pidof 未命中（App 可能被杀）')
                else:
                    mem = sample_memory(hdc, pid)
                    rec = {'t': round(now - t0, 1), 'pid': pid,
                           'hb': hb_max(srv)}
                    rec.update(mem or {'pss_kb': -1, 'native_heap_kb': -1,
                                       'ark_ts_heap_kb': -1})
                    samples_c.append(rec)
                    print(f'  [C t={rec["t"]:>6.0f}s] pss={rec["pss_kb"]}kB '
                          f'native={rec["native_heap_kb"]}kB '
                          f'arkts={rec["ark_ts_heap_kb"]}kB hb={rec["hb"]}')
                c_next += args.sample_every
                time.sleep(max(0.0, min(c_next - time.time(),
                                        c_end - time.time())))

        # ---- 分析与结论 ----
        total_s = time.time() - t0
        # pid 变化会重置基线 → 以最长稳定 pid 段分析
        seg = _longest_pid_segment(samples_a)
        seg_b = [s for s in samples_b if s.get('pss_kb', -1) > 0]
        seg_c = [s for s in samples_c if s.get('pss_kb', -1) > 0]
        print_table(f'阶段 A（pid={seg[0].get("pid") if seg else "-"} 稳定段）', seg)
        print_table('阶段 B（401 自愈循环）', seg_b)
        if seg_c:
            print_table('阶段 C（静置观察）', seg_c)

        # 判定门指标 ×（阶段 A/B/C）全分析；--gates 只过滤「入泄漏判定」
        # 的项，未入门指标仍采样/分析（观测口径不变，退出码不受影响）。
        METRICS = (('pss', 'pss_kb'), ('native', 'native_heap_kb'),
                   ('arkts', 'ark_ts_heap_kb'))
        analysis: dict = {}
        for ph, seg_ph in (('phase_a', seg), ('phase_b', seg_b),
                           ('phase_c', seg_c)):
            for g, key in METRICS:
                analysis[f'{ph}_{g}'] = analyze(seg_ph, key)

        def _fmt(tag: str, a: dict) -> str:
            if a.get('verdict') == 'insufficient_samples':
                return f'  {tag}: 样本不足（n={a.get("n", 0)}）'
            return (f'  {tag}: {a["first_kb"]}→{a["last_kb"]}kB '
                    f'（{a["delta_kb"]:+d}kB / {a["delta_pct"]:+.1f}%，'
                    f'斜率 {a["slope_kb_per_min"]:+.1f}kB/min，'
                    f'后半/前半均值 {a["second_half_avg_kb"]}/'
                    f'{a["first_half_avg_kb"]}）→ {a["verdict"]}')

        print('\n== 泄漏判定（门：>20% 涨幅 且 后半均值>前半×1.15 且 斜率>0）==')
        for ph, label in (('phase_a', '阶段A'), ('phase_b', '阶段B'),
                          ('phase_c', '阶段C静置')):
            if ph == 'phase_c' and not seg_c:
                continue
            for g, glabel in (('pss', 'PSS     '), ('native', 'native堆'),
                              ('arkts', 'arkts堆 ')):
                mark = '' if g in gates else ' [仅观测未入门]'
                print(_fmt(f'{label} {glabel}{mark}', analysis[f'{ph}_{g}']))

        # 主判定只看压力段（A/B）；静置段 C 是定性证据不入门
        suspected_hits = [f'{ph}.{g}'
                          for ph in ('phase_a', 'phase_b')
                          for g in ('pss', 'native', 'arkts')
                          if g in gates
                          and analysis.get(f'{ph}_{g}', {}).get('verdict')
                          == 'suspected_leak']
        suspected = bool(suspected_hits)
        verdict = (f'疑似泄漏（门命中: {",".join(suspected_hits)}；'
                   '回报 A 仲裁，不改码）' if suspected
                   else '无泄漏证据（判定门指标均在门内）')

        # 静置段定性：阶段 B 末 → 静置末 的 arkts 变化（GC 惰性判别）
        settle_note = ''
        if seg_b and seg_c:
            def _last_valid(seq: list[dict], key: str) -> int | None:
                for s in reversed(seq):
                    if s.get(key, -1) > 0:
                        return s[key]
                return None
            b_end = _last_valid(seg_b, 'ark_ts_heap_kb')
            c_end = _last_valid(seg_c, 'ark_ts_heap_kb')
            if b_end and c_end:
                pct = (c_end - b_end) / b_end * 100
                form = ('回落' if c_end < b_end
                        else ('走平' if pct < 5 else '继续上涨'))
                settle_note = (f'阶段B末 arkts={b_end}kB → 静置末 {c_end}kB'
                               f'（{pct:+.1f}%，{form}；'
                               + ('GC 惰性佐证' if pct < 5 else '真保留嫌疑')
                               + '）')
        hb_expect_a = max(1, int(t_a / HEARTBEAT_INTERVAL_S))
        hb_ok = hb_final_a >= base_hb + max(1, hb_expect_a - 2)
        print(f'\n== 结论 ==')
        print(f'  阶段A 心跳活性: hb {base_hb} → {hb_final_a}'
              f'（{t_a:.0f}s ≈ 期望 {hb_expect_a}±2 拍，'
              f'{"✓" if hb_ok else "✗"}）'
              f'{" ⚠ 有断拍" if not hb_ok else ""}')
        print(f'  阶段B 自愈循环: {loop_stat["note"]}；循环后心跳恢复: '
              f'{"✓" if hb_recovered else "✗"}')
        print(f'  pid 变化: {pid_changes} 次（0 = App 全程存活）')
        if settle_note:
            print(f'  静置观察: {settle_note}')
        print(f'  泄漏判定: {verdict}')

        # ---- JSON 报告 ----
        report = {
            'meta': {
                'started': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'total_s': round(total_s, 1),
                'phase_a_s': round(t_a, 1),
                'minutes': args.minutes, 'sample_every': args.sample_every,
                'ui_loops': args.ui_loops, 'pid_changes': pid_changes,
                'gates': sorted(gates), 'settle_s': args.settle,
                'hb_base': base_hb, 'hb_final_a': hb_final_a, 'hb_ok': hb_ok,
                'hb_recovered_after_b': hb_recovered,
                'settle_note': settle_note,
                'ui_loop': loop_stat, 'verdict': verdict,
            },
            'phase_a_samples': samples_a,
            'phase_b_samples': samples_b,
            'phase_c_samples': samples_c,
            'analysis': analysis,
        }
        if args.json_out:
            os.makedirs(os.path.dirname(os.path.abspath(args.json_out)),
                        exist_ok=True)
            with open(args.json_out, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f'\n报告: {args.json_out}')

        # ---- 还原现场 ----
        try:
            hdc.shell(f'aa force-stop {BUNDLE}', timeout=30)
        except HdcError:
            pass
        ok_all = not suspected and hb_ok and hb_recovered
        # 注：arkts 门命中（GC 惰性可能误报）时退出码为 1 属预期——
        # 最终定性（warmup_converged/suspected_leak）以任务回报的
        # 多 run 对照 + 静置段形态分析为准，勿只看退出码。
        return 0 if ok_all else 1
    finally:
        srv.stop()


def _longest_pid_segment(samples: list[dict]) -> list[dict]:
    """取 pid 连续不变的最长样本段（pid 变化会重置内存基线）。"""
    best: list[dict] = []
    cur: list[dict] = []
    cur_pid = None
    for s in samples:
        p = s.get('pid')
        if p and p == cur_pid:
            cur.append(s)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [s] if p else []
            cur_pid = p
    if len(cur) > len(best):
        best = cur
    return best


if __name__ == '__main__':
    sys.exit(main())