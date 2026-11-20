#!/usr/bin/env python3
"""ths_app.py — 同花顺副本 THSX.app 的唯一路径/进程入口。

铁律：
  1. 默认只「检测进程是否在跑 → 不在才启动」，绝不 SIGKILL 正在运行的 同花顺。
     杀进程只有一条路：显式 force_restart()（人工 --force-restart）。
  2. 启动路径全仓只此一处：/tmp/ths_lab/THSX.app。/tmp/ths_lab 下另一个坏副本一律不碰。
  3. 启动后等进程出现 + 窗口可用，都有超时，超时抛明确错误。
"""
import os
import signal
import subprocess
import time

APP_PATH = "/tmp/ths_lab/THSX.app"                      # 唯一启动路径
APP_BIN = os.path.join(APP_PATH, "Contents", "MacOS", "同花顺")
PROC_NAME = "同花顺"                                     # ps/pgrep 里的进程名
BUNDLE_ID = "cn.com.10jqka.thsx"                        # THSX.app 的 bundle id
APP_START_TIMEOUT = 20.0                                # 等进程出现的上限（秒）
APP_WINDOW_TIMEOUT = 20.0                               # 等窗口可用的上限（秒）
REBUILD_SH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "scripts", "ths_app.sh")      # 副本重建（唯一实现）


def _cmdline(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "args="],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, ValueError):
        return ""


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


SCREEN_LOCK_CHECKS = 3      # 连续采样次数：全部 locked 才算锁屏
SCREEN_LOCK_GAP = 1.0       # 采样间隔（秒）
# 锁屏时前台只可能是这两个进程。它们当前台才算「疑似锁屏」，还要功能性否决一次。
LOCK_FRONTMOST = ("loginwindow", "ScreenSaverEngine")


def _osascript(script: str, timeout: float = 5.0) -> tuple:
    """跑一段 osascript → (rc, stdout, stderr, 耗时ms)。异常统一 rc=-1。"""
    t0 = time.time()
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as ex:
        return -1, "", f"{type(ex).__name__}: {ex}", (time.time() - t0) * 1000
    return (r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip(),
            (time.time() - t0) * 1000)


def _proc_running(name: str) -> bool:
    try:
        return subprocess.run(["pgrep", "-x", name],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def gui_frontmost() -> tuple:
    """功能性读：System Events 取当前前台应用名 → (ok, name, evidence)。"""
    rc, out, err, ms = _osascript(
        'tell application "System Events" to get name of first process '
        'whose frontmost is true')
    if rc != 0 or not out:
        return False, "", (f"osascript 取前台应用失败 rc={rc} 耗时{ms:.0f}ms: "
                           f"{err[:160] or '(空输出)'}")
    return True, out, f"osascript rc=0 耗时{ms:.0f}ms 前台应用={out}"


def gui_window_probe(app: str = "") -> tuple:
    """功能性读（无害）：取指定/前台进程的前台窗口名 → (ok, detail)。

    锁屏时这个 GUI 读操作拿不到可交互窗口（报错或空），解锁时可正常返回。
    用「窗口名|窗口数」拼接，标题为空也能看出是真读到了东西。
    """
    target = f'process "{app}"' if app else ('first process whose frontmost is true')
    rc, out, err, ms = _osascript(f'''tell application "System Events"
    set p to {target}
    set n to name of front window of p
    return (n as text) & "|" & (count of windows of p)
end tell''')
    if rc != 0 or "|" not in out:
        return False, (f"读窗口失败 rc={rc} 耗时{ms:.0f}ms: "
                       f"{err[:160] or out[:80] or '(空输出)'}")
    return True, f"读窗口成功 rc=0 耗时{ms:.0f}ms: 窗口={out}"


def probe_screen_lock() -> tuple:
    """单次功能性锁屏探测 → (state, evidence, raw)，state ∈ locked/unlocked/unknown。

    判据（不再看 ioreg 的 CGSSessionScreenIsLocked —— 那个键会僵死，锁过之后
    用 Touch ID / Apple Watch 解锁不会改回 No）：
      1. 先用 System Events 读真实前台应用：不是 loginwindow / ScreenSaverEngine
         → 一定没锁（真正锁屏时前台只可能是这两个）；
      2. 前台是 loginwindow / ScreenSaverEngine 才算「疑似」，再尝试一次无害的
         窗口读；能成功返回 → 否决，判定未锁屏；
      3. 疑似 + 窗口读失败 → locked。
    osascript 整体失败（rc≠0 / 空输出 / 超时）= unknown，未知一律不算锁屏。
    """
    ok, front, ev = gui_frontmost()
    if not ok:
        return "unknown", ev, ev
    if front not in LOCK_FRONTMOST:
        return "unlocked", f"{ev}（不在锁屏前台名单，判定未锁屏）", ev
    if front == "ScreenSaverEngine" and not _proc_running("ScreenSaverEngine"):
        ev2 = f"{ev}；但 ScreenSaverEngine 进程不存在（状态自相矛盾）"
        return "unknown", ev2, ev2
    w_ok, w_ev = gui_window_probe(front)
    if w_ok:
        # 否决规则：GUI 操作还能成功 → 不管前台是谁都没锁
        return "unlocked", f"{ev}；{w_ev} → 否决，判定未锁屏", f"{ev}\n{w_ev}"
    return "locked", f"{ev}；{w_ev}", f"{ev}\n{w_ev}"


def screen_lock_samples(checks: int = SCREEN_LOCK_CHECKS,
                        gap: float = SCREEN_LOCK_GAP) -> list:
    """连续 checks 次探测，返回 [(state, evidence, raw), ...]。"""
    samples = []
    for i in range(checks):
        if i:
            time.sleep(gap)
        samples.append(probe_screen_lock())
    return samples


def screen_locked() -> bool:
    """连续 SCREEN_LOCK_CHECKS 次探测全部 locked 才算锁屏。

    锁屏时同花顺重启后登录回不来（loginZiYingFuWu 要网络+活跃会话），UI 点击也无效。
    unknown（osascript 失败/超时/空输出）或任意一次没锁 → False：宁可重启，
    也不要因为一次探测失败静默吞掉整次重启（重启真失败会报错，误判锁屏不会）。
    """
    return all(state == "locked" for state, _, _ in screen_lock_samples())


def app_pid():
    """返回正在运行的 THSX.app 主进程 pid；没在跑返回 None。

    只认可执行文件 realpath 落在 APP_PATH 下的进程，所以 /tmp/ths_lab 下同名的
    坏副本、或原版 (/Applications/同花顺.app) 都不会被误认。
    """
    try:
        out = subprocess.check_output(
            ["pgrep", "-x", PROC_NAME], text=True, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return None
    app_prefix = os.path.realpath(APP_PATH) + os.sep
    for s in out.split():
        try:
            pid = int(s)
        except ValueError:
            continue
        exe = _cmdline(pid).split(" ", 1)[0]
        if exe and os.path.realpath(exe).startswith(app_prefix):
            return pid
    return None


def count_windows() -> int:
    script = f'''
tell application "System Events"
    set theProcs to processes whose bundle identifier is "{BUNDLE_ID}"
    if theProcs is {{}} then return 0
    return count of (windows of item 1 of theProcs)
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script], timeout=5,
                           capture_output=True, text=True)
        return int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0
    except Exception:
        return 0


def ensure_window() -> None:
    """窗口数为 0 时激活进程 + Cmd+N（尽力而为，锁屏下会失败）。"""
    script = f'''
tell application "System Events"
    set theProcs to processes whose bundle identifier is "{BUNDLE_ID}"
    if theProcs is {{}} then return
    set theProc to item 1 of theProcs
    if (count of windows of theProc) = 0 then
        set frontmost of theProc to true
        delay 0.5
        keystroke "n" using command down
    end if
end tell'''
    try:
        subprocess.run(["osascript", "-e", script], timeout=8, capture_output=True)
    except Exception:
        pass


def wait_process(timeout: float = APP_START_TIMEOUT) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = app_pid()
        if pid:
            return pid
        time.sleep(0.5)
    raise RuntimeError(f"{APP_PATH} 启动超时（{timeout:.0f}s）：进程未出现")


def wait_window(timeout: float = APP_WINDOW_TIMEOUT) -> bool:
    """等窗口可用；20s 内没有就 Cmd+N 再等 5s，仍没有返回 False。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if count_windows() > 0:
            return True
        time.sleep(0.5)
    ensure_window()
    deadline = time.time() + 5
    while time.time() < deadline:
        if count_windows() > 0:
            return True
        time.sleep(0.5)
    return False


def copy_ready() -> bool:
    """副本可用 = bundle id 对 + 没有 hardened runtime + adhoc 签名自校验通过。

    重启电脑后 /tmp 被清空，重新 ditto 出来的原版带 hardened runtime 和沙箱 entitlements，
    frida attach/spawn 会被 taskgated 拒掉（unable to access process ... from the current
    user account）。这种副本必须先按 scripts/ths_app.sh rebuild 重签。
    """
    info = os.path.join(APP_PATH, "Contents", "Info.plist")
    try:
        bid = subprocess.check_output(
            ["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleIdentifier", info],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return False
    if bid != BUNDLE_ID:
        return False
    sig = subprocess.run(["codesign", "-dv", APP_PATH],
                         capture_output=True, text=True)
    if "(runtime)" in (sig.stdout + sig.stderr):
        return False
    return subprocess.run(["codesign", "--verify", "--deep", "--strict", APP_PATH],
                          capture_output=True).returncode == 0


def ensure_copy() -> None:
    """副本缺失/签名不可注入时重建一次（幂等，复用 scripts/ths_app.sh rebuild）。"""
    if copy_ready():
        return
    r = subprocess.run(["bash", REBUILD_SH, "rebuild"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not copy_ready():
        raise RuntimeError(
            f"重建 {APP_PATH} 失败：{(r.stderr or r.stdout).strip()[:300]}"
        )


def launch() -> int:
    """启动 THSX.app（不杀任何进程），返回新进程 pid。"""
    ensure_copy()
    subprocess.Popen(["open", APP_PATH],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return wait_process()


def ensure_running(require_window: bool = True) -> int:
    """已在跑 → 直接返回 pid；没在跑 → 启动后返回 pid。全程不杀进程。"""
    pid = app_pid()
    if pid:
        return pid
    pid = launch()
    if require_window and not wait_window():
        raise RuntimeError(
            f"{APP_PATH} 已启动 pid={pid}，但 {APP_WINDOW_TIMEOUT:.0f}s+5s 内窗口不可用"
            f"（锁屏或 UI 未就绪）"
        )
    return pid


def force_restart() -> int:
    """人工显式重启（仅 --force-restart / scripts/restart_ths.py 调用）：
    SIGKILL 现有 THSX 进程 → 重新启动。默认路径永不调用。"""
    pid = app_pid()
    if pid:
        os.kill(pid, signal.SIGKILL)
        deadline = time.time() + 5
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.2)
    return ensure_running(require_window=False)
