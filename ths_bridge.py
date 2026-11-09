#!/usr/bin/env python3
"""
ths_bridge.py — 常驻桥接服务，连接 Frida agent 与本地 UNIX socket。

协议：每行一个 JSON 对象
  请求：{"id": <int>, "method": <str>, "params": {}}
  响应：{"id": <int>, "ok": <bool>, "result": <any>}

日志：/tmp/ths_bridge.log
Socket：/tmp/ths_bridge.sock
"""
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import frida

# ── 路径 ─────────────────────────────────────────────────────────────────────
AGENT_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ths_agent.js")
SOCK_PATH = "/tmp/ths_bridge.sock"
LOG_PATH  = "/tmp/ths_bridge.log"
APP_COPY  = "/tmp/ths_lab/THS.app"
APP_BIN   = "/tmp/ths_lab/THS.app/Contents/MacOS/同花顺"
APP_ORIG_NAME = "同花顺"
BUNDLE_ID = "com.ths.lab"
APP_COPY_PATH_PREFIX = "/private/tmp/ths_lab"  # realpath of /tmp/ths_lab on macOS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("ths_bridge")


# ── 进程管理 ──────────────────────────────────────────────────────────────────

def _get_copy_pid() -> int | None:
    """返回副本 /tmp/thsx2.app 的 pid，没有返回 None。"""
    try:
        out = subprocess.check_output(["pgrep", "-x", APP_ORIG_NAME], text=True)
        for pid_s in out.split():
            pid = int(pid_s.strip())
            try:
                cmd = subprocess.check_output(["ps", "-p", str(pid), "-o", "args="], text=True).strip()
                if APP_COPY_PATH_PREFIX in cmd:
                    return pid
            except subprocess.CalledProcessError:
                pass
    except subprocess.CalledProcessError:
        pass
    return None


def _get_orig_pid() -> int | None:
    """返回原版 /Applications/同花顺.app 的 pid，没有返回 None。"""
    try:
        out = subprocess.check_output(["pgrep", "-x", APP_ORIG_NAME], text=True)
        for pid_s in out.split():
            pid = int(pid_s.strip())
            try:
                cmd = subprocess.check_output(["ps", "-p", str(pid), "-o", "args="], text=True).strip()
                if "/Applications/" in cmd:
                    return pid
            except subprocess.CalledProcessError:
                pass
    except subprocess.CalledProcessError:
        pass
    return None


def ensure_app() -> int:
    """确保副本在跑；返回 PID。"""
    # 1. 杀原版（只杀 /Applications/ 路径的）
    orig_pid = _get_orig_pid()
    if orig_pid:
        log.info("killing original 同花顺 pid=%d", orig_pid)
        os.kill(orig_pid, signal.SIGKILL)
        time.sleep(0.5)

    # 2. 检查副本是否已经在跑
    copy_pid = _get_copy_pid()
    if copy_pid:
        log.info("copy already running pid=%d", copy_pid)
        _ensure_window()
        return copy_pid

    # 3. 启动副本
    # 确保 Info.plist 禁止窗口恢复（重签后会丢这个 key）
    _ensure_plist_no_restore()
    log.info("launching %s", APP_COPY)
    subprocess.Popen(
        ["open", APP_COPY],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # 等待进程出现，最多 25 秒
    for _ in range(50):
        copy_pid = _get_copy_pid()
        if copy_pid:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("thsx2.app did not start within 25 s")

    # 等窗口出现，最多 30 秒
    for _ in range(60):
        wins = _count_windows()
        if wins and wins > 0:
            break
        time.sleep(0.5)
    _ensure_window()
    return copy_pid


def _ensure_plist_no_restore():
    """确保 Info.plist 有 NSQuitAlwaysKeepsWindows=false，避免 StateRestoration 卡死。"""
    plist = os.path.join(APP_COPY, "Contents", "Info.plist")
    r = subprocess.run(
        ["/usr/libexec/PlistBuddy", "-c", "Print :NSQuitAlwaysKeepsWindows", plist],
        capture_output=True, text=True,
    )
    if "false" in r.stdout:
        return  # 已存在
    if "Does Not Exist" in r.stderr or r.returncode != 0:
        subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c", "Add :NSQuitAlwaysKeepsWindows bool false", plist],
            check=False,
        )
    else:
        subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c", "Set :NSQuitAlwaysKeepsWindows false", plist],
            check=False,
        )
    # 修改 plist 后必须重签
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", APP_COPY],
                   capture_output=True)
    log.info("patched Info.plist + re-signed")


def _count_windows() -> int:
    """用 AppleScript 读窗口数。"""
    script = '''
tell application "System Events"
    set theProcs to processes whose bundle identifier is "cn.com.10jqka.thsx"
    if theProcs is {} then return 0
    return count of (windows of item 1 of theProcs)
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           timeout=5, capture_output=True, text=True)
        return int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0
    except Exception:
        return 0


def _ensure_window():
    """如果没有窗口，用 AppleScript 激活 + Cmd+N。"""
    script = """
tell application "System Events"
    set theProcs to processes whose bundle identifier is "cn.com.10jqka.thsx"
    if theProcs is {} then return
    set theProc to item 1 of theProcs
    set winCount to count of (windows of theProc)
    if winCount = 0 then
        set frontmost of theProc to true
        delay 0.5
        keystroke "n" using command down
        delay 1
    end if
end tell
"""
    try:
        subprocess.run(
            ["osascript", "-e", script],
            timeout=8,
            capture_output=True,
        )
    except Exception as e:
        log.warning("ensure_window AppleScript error: %s", e)


# ── Frida 连接 ────────────────────────────────────────────────────────────────

class AgentSession:
    def __init__(self):
        self._session  = None
        self._script   = None
        self._rpc      = None
        self._lock     = threading.Lock()
        self._pid      = None

    def connect(self, pid: int):
        with self._lock:
            self._disconnect_locked()
            log.info("attaching frida to pid=%d", pid)
            dev     = frida.get_local_device()
            session = dev.attach(pid)
            session.on("detached", self._on_detach)
            js_src  = open(AGENT_JS, encoding="utf-8").read()
            script  = session.create_script(js_src)
            script.on("message", self._on_message)
            script.load()
            self._session = session
            self._script  = script
            self._rpc     = script.exports_sync
            self._pid     = pid
            log.info("frida attached ok pid=%d", pid)

    def _disconnect_locked(self):
        try:
            if self._script:
                self._script.unload()
        except Exception:
            pass
        try:
            if self._session:
                self._session.detach()
        except Exception:
            pass
        self._session = self._script = self._rpc = None

    def _on_detach(self, reason, crash):
        log.warning("frida session detached: %s", reason)
        with self._lock:
            self._session = self._script = self._rpc = None

    def _on_message(self, message, data):
        if message.get("type") == "error":
            log.error("agent JS error: %s\n%s", message.get("description"), message.get("stack"))
        else:
            log.debug("agent message: %s", message)

    def call(self, method: str, params: dict):
        if not self._lock.acquire(timeout=35):
            raise RuntimeError(f"agent lock timeout — previous RPC may be stuck (method={method})")
        try:
            if self._rpc is None:
                raise RuntimeError("agent not connected")
            fn = getattr(self._rpc, method, None)
            if fn is None:
                raise AttributeError(f"rpc method not found: {method}")
            if params:
                return fn(params)
            return fn()
        finally:
            self._lock.release()

    @property
    def connected(self) -> bool:
        return self._rpc is not None


_agent = AgentSession()


# ── 重连循环 ──────────────────────────────────────────────────────────────────

def reconnect_loop():
    backoff = 2
    while True:
        if not _agent.connected:
            try:
                pid = ensure_app()
                _agent.connect(pid)
                backoff = 2
            except Exception as e:
                log.error("reconnect failed: %s — retry in %ds", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
        time.sleep(5)


# ── 请求处理 ──────────────────────────────────────────────────────────────────

ALLOWED_METHODS = {
    "ping", "state",
    "read_table_data", "send_chedan",
    "query_positions", "send_chedan_direct",
    "read_ns_table_views", "read_positions", "read_orders",
    "read_orders_from_table",
    "open_window",
    "buy", "sell",
    "capture_start", "capture_stop", "capture_dump",
    "send_log",
    "ensure_ready",
    # 协议响应路径（不读视图树）
    "fetch_positions", "fetch_orders",
}


def handle_request(req: dict) -> dict:
    req_id = req.get("id", 0)
    method = req.get("method", "")
    params = req.get("params") or {}

    if method not in ALLOWED_METHODS:
        return {"id": req_id, "ok": False, "result": {"error": f"unknown method: {method}"}}

    try:
        result = _agent.call(method, params)
        return {"id": req_id, "ok": True, "result": result}
    except RuntimeError as e:
        return {"id": req_id, "ok": False, "result": {"error": str(e)}}
    except Exception as e:
        log.exception("method %s failed", method)
        return {"id": req_id, "ok": False, "result": {"error": str(e)}}


# ── UNIX socket 服务器 ────────────────────────────────────────────────────────

def handle_client(conn: socket.socket, addr):
    log.info("client connected")
    try:
        buf = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError as e:
                    resp = {"id": 0, "ok": False, "result": {"error": f"bad json: {e}"}}
                    conn.sendall(json.dumps(resp).encode() + b"\n")
                    continue
                resp = handle_request(req)
                conn.sendall(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
    except ConnectionResetError:
        pass
    except Exception as e:
        log.error("client handler error: %s", e)
    finally:
        conn.close()
        log.info("client disconnected")


def serve():
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o600)
    srv.listen(8)
    log.info("listening on %s", SOCK_PATH)
    while True:
        conn, addr = srv.accept()
        t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
        t.start()


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    import argparse as _ap
    ap = _ap.ArgumentParser(description="ths_bridge — Frida agent bridge")
    ap.add_argument("--pid", type=int, default=None,
                    help="直接 attach 指定 pid，不自动启动/重启 app")
    args = ap.parse_args()

    if not os.path.isfile(AGENT_JS):
        sys.exit(f"agent not found: {AGENT_JS}")

    if args.pid:
        # 直接 attach 模式：不调用 ensure_app，不自动重启
        log.info("direct-pid mode: attaching to pid=%d (no auto-restart)", args.pid)
        try:
            _agent.connect(args.pid)
        except Exception as e:
            log.error("attach failed: %s", e)
            sys.exit(1)
        # 静态重连：只有 agent 断开才重连到同一 pid，不重启进程
        def _static_reconnect():
            while True:
                time.sleep(5)
                if not _agent.connected:
                    log.warning("agent disconnected, re-attaching to pid=%d", args.pid)
                    try:
                        _agent.connect(args.pid)
                    except Exception as e:
                        log.error("re-attach failed: %s", e)
        t = threading.Thread(target=_static_reconnect, daemon=True)
        t.start()
    else:
        # 原始自动模式（不传 --pid 时保留原逻辑，但本次任务不走这里）
        try:
            pid = ensure_app()
            _agent.connect(pid)
        except Exception as e:
            log.error("initial connect failed: %s", e)

        t = threading.Thread(target=reconnect_loop, daemon=True)
        t.start()

    # 前台运行 socket 服务
    serve()


if __name__ == "__main__":
    main()
