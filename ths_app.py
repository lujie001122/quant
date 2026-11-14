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


def launch() -> int:
    """启动 THSX.app（不杀任何进程），返回新进程 pid。"""
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
