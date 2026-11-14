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
import struct
import subprocess
import sys
import threading
import time
from typing import Optional

import frida

# ── 路径 ─────────────────────────────────────────────────────────────────────
AGENT_JS   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ths_agent.js")
SOCK_PATH  = "/tmp/ths_bridge.sock"
LOG_PATH   = "/tmp/ths_bridge.log"
FRAMES_DIR = "/tmp/ths_frames"
# 抓帧落盘默认关闭（/tmp/ths_frames 曾被写爆）：只在 ths_bridge.py --save-frames
# 或 THS_FRAME_SAVE=1 时开启；THS_FRAME_KEEP_DAYS=N 开启时启动清理 N 天前的旧帧
FRAME_SAVE_ENABLED = os.environ.get("THS_FRAME_SAVE", "").strip().lower() in ("1", "true", "yes", "on")
try:
    FRAME_KEEP_DAYS = int(os.environ.get("THS_FRAME_KEEP_DAYS", "7"))
except ValueError:
    FRAME_KEEP_DAYS = 7
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


# ══════════════════════════════════════════════════════════════════════════════
# 协议层 FrameSaver + 帧解析器
# 职责：
#   1. 保存 agent 通过 send({type:'sock_frame'}) 推来的原始字节到 /tmp/ths_frames/
#      （默认关闭，--save-frames / THS_FRAME_SAVE=1 才落盘；读取/解析能力不变）
#   2. parse_frames(data) 解析同花顺协议帧 → 记录列表
#      帧格式：FD FD FD FD + 8字节ASCII十六进制长度 + 0x09 + payload
#      payload：TLV —— 字段键（十进制字符串或二进制）+ 长度 + 值
# ══════════════════════════════════════════════════════════════════════════════

FRAME_MAGIC = b'\xfd\xfd\xfd\xfd'
FRAME_HDR_LEN = 4 + 8 + 1   # magic(4) + hex_len(8) + sep(1) = 13

# 已确认字段号（持仓 / 委托 / 成交 通用）
_FIELD_NAMES = {
    2102: "代码",    2103: "名称",    2106: "股东账户", 2108: "市场",
    2113: "合同号",  2117: "数量",    2119: "成交价",   2120: "成交量",
    2121: "可用",    2122: "成本",    2124: "现价",     2125: "市值",
    2126: "委托量",  2127: "委托价",  2135: "scdm",     2140: "时间",
    2147: "当日盈亏", 2105: "状态",   4008: "方向",     2096: "时间2",
    2109: "成交额",  2111: "成交量2",
}


class FrameSaver:
    """线程安全的原始帧落盘管理器（默认不落盘）。"""

    MAX_TOTAL = 32 * 1024 * 1024   # 32 MB 总缓存上限
    MAX_FRAME = 256 * 1024          # 256 KB 单帧上限

    def __init__(self, frames_dir: str = FRAMES_DIR, enabled: bool = None,
                 keep_days: int = None):
        self._dir = frames_dir
        self._lock = threading.Lock()
        self._total_bytes = 0
        self._frame_count = 0
        self._dropped = 0
        self._enabled = FRAME_SAVE_ENABLED if enabled is None else bool(enabled)
        self._keep_days = FRAME_KEEP_DAYS if keep_days is None else int(keep_days)
        if self._enabled:
            os.makedirs(frames_dir, exist_ok=True)
            if self._keep_days > 0:
                self.purge_old(self._keep_days)

    def set_enabled(self, enabled: bool, keep_days: int = None) -> None:
        """显式开启/关闭落盘（--save-frames / THS_FRAME_SAVE）。"""
        self._enabled = bool(enabled)
        if keep_days is not None:
            self._keep_days = int(keep_days)
        if self._enabled:
            os.makedirs(self._dir, exist_ok=True)
            if self._keep_days > 0:
                self.purge_old(self._keep_days)

    def purge_old(self, days: int) -> int:
        """删除超过 days 天的旧帧文件，返回删除个数。"""
        cutoff = time.time() - max(int(days), 1) * 86400
        removed = 0
        try:
            names = os.listdir(self._dir)
        except OSError:
            return 0
        for name in names:
            path = os.path.join(self._dir, name)
            try:
                if not os.path.isfile(path) or os.path.getmtime(path) >= cutoff:
                    continue
                os.unlink(path)
                removed += 1
            except OSError:
                continue
        if removed:
            log.info("frame_saver: purged %d files older than %dd in %s", removed, days, self._dir)
        return removed

    def save(self, seq: int, data: Optional[bytes]) -> bool:
        """保存一帧到磁盘。默认不落盘；data 为 None 或超限时丢弃并计数。"""
        if not self._enabled or not data:
            return False
        n = len(data)
        with self._lock:
            if n > self.MAX_FRAME or self._total_bytes + n > self.MAX_TOTAL:
                self._dropped += 1
                log.debug("frame_saver: dropped frame seq=%d size=%d", seq, n)
                return False
            path = os.path.join(self._dir, f"frame_{seq:06d}.bin")
            try:
                with open(path, "wb") as f:
                    f.write(data)
                self._total_bytes += n
                self._frame_count += 1
                return True
            except OSError as e:
                log.warning("frame_saver: write error seq=%d: %s", seq, e)
                return False

    def save_named(self, filename: str, data: Optional[bytes]) -> bool:
        """保存一帧到磁盘，文件名由调用方指定。默认不落盘。"""
        if not self._enabled or not data:
            return False
        n = len(data)
        with self._lock:
            if n > self.MAX_FRAME or self._total_bytes + n > self.MAX_TOTAL:
                self._dropped += 1
                return False
            path = os.path.join(self._dir, filename)
            try:
                with open(path, "wb") as f:
                    f.write(data)
                self._total_bytes += n
                self._frame_count += 1
                return True
            except OSError as e:
                log.warning("frame_saver: write error %s: %s", filename, e)
                return False

    def stats(self) -> dict:
        with self._lock:
            return {
                "frame_count": self._frame_count,
                "total_bytes": self._total_bytes,
                "dropped": self._dropped,
                "dir": self._dir,
                "enabled": self._enabled,
                "keep_days": self._keep_days,
            }

    def list_frames(self) -> list:
        """返回落盘文件列表（按名称排序）。"""
        try:
            return sorted(
                f for f in os.listdir(self._dir) if f.startswith("frame_") and f.endswith(".bin")
            )
        except OSError:
            return []

    def read_frame(self, filename: str) -> Optional[bytes]:
        """读取指定帧文件内容。"""
        path = os.path.join(self._dir, filename)
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None


_frame_saver = FrameSaver()


def parse_tlv_payload(payload: bytes) -> list:
    """
    解析 TLV payload → list[dict]，每个 dict 是一条记录。
    同花顺 TLV 格式（实测）：
      字段键：4字节小端 int32（也可能是十进制字符串直到 '\\x00'）
      长度：2字节小端 uint16
      值：length 字节 UTF-8 字符串
    多条记录：同一组字段号循环出现，按首个字段号的重复位置切分记录。
    """
    records = []
    if not payload:
        return records

    i = 0
    n = len(payload)
    current: dict = {}
    first_key = None
    field_order = []

    def flush():
        nonlocal current, field_order
        if current:
            records.append(dict(current))
        current = {}
        field_order = []

    while i < n:
        # ── 读字段键（4字节小端 int，或跳过非结构字节）────────────────────
        if i + 6 > n:  # 至少需要 key(4)+len(2)
            break
        key_raw = payload[i:i+4]
        key = struct.unpack_from('<i', key_raw)[0]
        i += 4

        # 合法字段号范围大约 1000-9999，否则尝试跳过
        if not (1000 <= key <= 99999):
            # 退一步：尝试把 key 解读为其他格式，实际跳过 1 字节继续扫描
            i -= 3  # 退回，只消耗1字节
            continue

        val_len = struct.unpack_from('<H', payload, i)[0]
        i += 2
        if i + val_len > n:
            break

        val_bytes = payload[i:i+val_len]
        i += val_len
        try:
            val = val_bytes.decode('utf-8', errors='replace').rstrip('\x00')
        except Exception:
            val = val_bytes.hex()

        # 检测记录边界：字段号与第一条记录中的 first_key 相同时，说明新记录开始
        if first_key is None:
            first_key = key
        elif key == first_key and current:
            flush()

        current[key] = val
        field_order.append(key)

    flush()
    return records


def parse_frames(data: bytes) -> list:
    """
    在字节流 data 中扫描所有同花顺协议帧，解析并返回记录列表。
    帧格式：FD FD FD FD + 8字节ASCII十六进制长度 + 0x09 + payload
    返回：[{"frame_offset": int, "payload_len": int, "records": [...]}, ...]
    """
    results = []
    i = 0
    n = len(data)
    while i <= n - FRAME_HDR_LEN:
        idx = data.find(FRAME_MAGIC, i)
        if idx < 0:
            break
        i = idx
        # 读 8 字节 ASCII 十六进制长度
        if i + FRAME_HDR_LEN > n:
            break
        hex_len_bytes = data[i+4:i+12]
        sep = data[i+12]
        try:
            payload_len = int(hex_len_bytes, 16)
        except ValueError:
            i += 1
            continue
        if sep != 0x09:
            i += 1
            continue
        payload_start = i + FRAME_HDR_LEN
        if payload_start + payload_len > n:
            # 帧不完整，停止扫描
            break
        payload = data[payload_start:payload_start + payload_len]
        records = parse_tlv_payload(payload)
        results.append({
            "frame_offset": i,
            "payload_len": payload_len,
            "records": records,
            "payload_hex_preview": payload[:200].hex() if payload else "",
        })
        i = payload_start + payload_len

    return results


def humanize_records(records: list) -> list:
    """把字段号键替换为可读名称，不影响原始数据。"""
    out = []
    for rec in records:
        h = {}
        for k, v in rec.items():
            name = _FIELD_NAMES.get(int(k) if str(k).lstrip('-').isdigit() else -1, str(k))
            h[name] = v
        out.append(h)
    return out


# ── 进程管理 ──────────────────────────────────────────────────────────────────

def _get_copy_pid() -> Optional[int]:
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


def _get_orig_pid() -> Optional[int]:
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

    # 2. 副本若已在跑：杀掉重开。JYMaiMaiView（下单面板）实例只在窗口创建时产生，
    #    agent 的视图钩子必须赶在它之前装好，否则拿不到下单入口的实例指针。
    copy_pid = _get_copy_pid()
    if copy_pid:
        log.info("killing running copy pid=%d (restart to install hooks first)", copy_pid)
        try:
            os.kill(copy_pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(1.5)

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
    set theProcs to processes whose bundle identifier is "com.ths.lab"
    if theProcs is {} then
        set theProcs to processes whose bundle identifier is "cn.com.10jqka.thsx"
    end if
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
    set theProcs to processes whose bundle identifier is "com.ths.lab"
    if theProcs is {} then
        set theProcs to processes whose bundle identifier is "cn.com.10jqka.thsx"
    end if
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

    def spawn_connect(self):
        """spawn 副本 → attach → load script → resume，确保 hook 在所有 ObjC init 之前生效。"""
        with self._lock:
            self._disconnect_locked()
            dev = frida.get_local_device()
            log.info("spawning %s", APP_BIN)
            pid = dev.spawn([APP_BIN])
            log.info("spawned pid=%d, attaching...", pid)
            session = dev.attach(pid)
            session.on("detached", self._on_detach)
            js_src = open(AGENT_JS, encoding="utf-8").read()
            script = session.create_script(js_src)
            script.on("message", self._on_message)
            script.load()
            log.info("script loaded, resuming pid=%d", pid)
            dev.resume(pid)
            self._session = session
            self._script  = script
            self._rpc     = script.exports_sync
            self._pid     = pid
            log.info("frida spawn+attach ok pid=%d", pid)
            return pid

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
        elif message.get("type") == "send":
            payload = message.get("payload", {})
            if isinstance(payload, dict) and payload.get("type") == "sock_frame":
                # 协议层原始帧：落盘到 /tmp/ths_frames/<timestamp>-<seq>.bin
                seq = payload.get("seq", 0)
                ts = int(time.time() * 1000)
                fname = f"{ts}-{seq:06d}.bin"
                saved = _frame_saver.save_named(fname, data)
                if saved and data:
                    hex64 = data[:64].hex()
                    log.info("sock_frame seq=%d len=%d file=%s hex64=%s",
                             seq, len(data), fname, hex64)
            elif isinstance(payload, dict) and payload.get("type") == "log":
                log.info("agent: %s", payload.get("text", ""))
            else:
                log.debug("agent send message: %s", payload)
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
    # 与 ths_agent.js 的 rpc.exports 逐条对齐（34 个导出）。
    # 调用名走 frida.exports_sync：蛇形自动转驼峰（frida.core._to_camel_case），
    # 例 read_positions → readPositions，因此这里一律写蛇形，映射逻辑不改。

    # 基础
    "ping", "state", "last_trade_rsp",
    # 买卖
    "buy", "sell",
    # 登录
    "login", "loginSim", "loginMoni", "login_sim",
    # capture / sendLog（兼容，agent 侧仍有 captureStart/Stop/Dump、sendLog）
    "capture_start", "capture_stop", "capture_dump", "send_log",
    # ── hook-free 新路径 ──────────────────────────────────────────────────────
    "scan_instances",
    "read_positions", "read_orders", "read_trades",
    "read_account_direct",
    "send_chedan_by_index",
    "place_order",
    "place_order_via_view",
    # ── 旧名兼容别名（agent 侧仍有同名导出）──────────────────────────────────
    "read_positions_direct",
    "read_orders_direct",
    "read_trades_direct",
    "fetch_positions",
    "fetch_orders",
    "ensure_ready",
    "prime_cache",
    "get_cache_status",
    "ensure_query_templates",
    "login_sim",
    "request_query_direct",
}


# ── 本地方法表（不需要 agent RPC 的方法在这里直接处理）──────────────────────────

def _handle_local(method: str, params: dict):
    """返回 (handled, result)。若 handled=False 则交给 agent.call()。"""
    if method == "get_frames_list":
        return True, {"ok": True, "frames": _frame_saver.list_frames(), "stats": _frame_saver.stats()}
    if method == "get_frames_stats":
        return True, {"ok": True, **_frame_saver.stats()}
    if method == "parse_frames_file":
        # params: {"filename": "frame_000001.bin"} 或 {"filename": "latest"}
        fname = params.get("filename", "latest")
        if fname == "latest":
            files = _frame_saver.list_frames()
            if not files:
                return True, {"ok": False, "error": "no frames saved yet"}
            fname = files[-1]
        data = _frame_saver.read_frame(fname)
        if data is None:
            return True, {"ok": False, "error": f"frame file not found: {fname}"}
        frames = parse_frames(data)
        # 汇总所有帧里的所有记录
        all_records = []
        for fr in frames:
            all_records.extend(fr.get("records", []))
        # 转换字段名
        readable = humanize_records(all_records)
        # 预览前 200 字节 hex
        preview_hex = data[:200].hex()
        return True, {
            "ok": True,
            "filename": fname,
            "file_size": len(data),
            "preview_hex": preview_hex,
            "frame_count": len(frames),
            "record_count": len(all_records),
            "records": readable,
            "raw_records": all_records,
            "frames_meta": [{"offset": f["frame_offset"], "payload_len": f["payload_len"],
                              "record_count": len(f["records"])} for f in frames],
        }
    if method == "parse_all_frames":
        # 解析所有落盘帧，合并记录，按字段分类（orders/positions/trades）
        files = _frame_saver.list_frames()
        if not files:
            return True, {"ok": False, "error": "no frames saved yet"}
        all_records = []
        for fname in files:
            data = _frame_saver.read_frame(fname)
            if not data:
                continue
            for fr in parse_frames(data):
                all_records.extend(fr.get("records", []))
        readable = humanize_records(all_records)
        # 分类
        orders, positions, trades = [], [], []
        for rec in readable:
            if "合同号" in rec and rec.get("合同号", ""):
                if "成交额" in rec or "成交量" in rec:
                    trades.append(rec)
                else:
                    orders.append(rec)
            elif "成本" in rec or "市值" in rec:
                positions.append(rec)
        return True, {
            "ok": True,
            "total_records": len(all_records),
            "orders": orders,
            "positions": positions,
            "trades": trades,
            "stats": _frame_saver.stats(),
        }
    return False, None


def handle_request(req: dict) -> dict:
    req_id = req.get("id", 0)
    method = req.get("method", "")
    params = req.get("params") or {}

    # 本地帧解析方法（不需要 agent RPC）
    LOCAL_METHODS = {"get_frames_list", "get_frames_stats", "parse_frames_file", "parse_all_frames"}
    if method in LOCAL_METHODS:
        handled, result = _handle_local(method, params)
        if handled:
            return {"id": req_id, "ok": True, "result": result}

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

def _prime_trade_panel():
    """attach 成功后执行一次：找到含 button 6 的交易窗口 → 点交易 tab → 等 4s → 点「模拟」→ 等 6s。
    同花顺可能有多个窗口（登录框 + 主窗口），交易按钮在主窗口，遍历所有窗口找到含 button 6 的那个。"""
    script = '''
tell application "System Events"
    set theProcs to processes whose name is "同花顺"
    if theProcs is {} then return "no proc"
    set theProc to item 1 of theProcs
    set frontmost of theProc to true
    delay 0.5
    -- ★ 先开一个新窗口：JYMaiMaiView（下单面板）只在窗口/交易页创建时实例化，
    --   agent 的视图钩子必须赶在实例化之前装好，才能拿到下单入口的实例指针。
    keystroke "n" using command down
    delay 2
    set allWins to windows of theProc
    set winCount to count of allWins
    if winCount = 0 then return "no windows"
    -- 从新窗口往回找含 button 6 的那个（主交易窗口）
    set theWin to missing value
    repeat with wIdx from winCount to 1 by -1
        set w to window wIdx of theProc
        try
            set dummy to button 6 of w
            set theWin to w
            exit repeat
        end try
    end repeat
    if theWin is missing value then
        -- 兜底：用按钮数最多的窗口
        set maxBtns to 0
        repeat with wIdx from 1 to winCount
            set w to window wIdx of theProc
            try
                set bc to count of buttons of w
                if bc > maxBtns then
                    set maxBtns to bc
                    set theWin to w
                end if
            end try
        end repeat
    end if
    if theWin is missing value then return "no trading window"
    -- 点交易 tab（button 6）
    try
        click button 6 of theWin
    on error errMsg
        return "b6_err:" & errMsg
    end try
    delay 4
    -- 点「模拟」按钮
    try
        click button "模拟" of theWin
        return "ok_by_name"
    on error
    end try
    try
        click button 12 of theWin
        return "ok_b12"
    on error errMsg2
        return "moni_err:" & errMsg2
    end try
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           timeout=15, capture_output=True, text=True)
        result = r.stdout.strip()
        log.info("prime_trade_panel: %s", result)
        return result
    except Exception as e:
        log.warning("prime_trade_panel error: %s", e)
        return f"ERR:{e}"


def main():
    import argparse as _ap
    ap = _ap.ArgumentParser(description="ths_bridge — Frida agent bridge")
    ap.add_argument("--pid", type=int, default=None,
                    help="直接 attach 指定 pid，不自动启动/重启 app")
    ap.add_argument("--spawn", action="store_true",
                    help="用 frida spawn 启动副本，hook 在所有 ObjC init 之前生效（推荐）")
    ap.add_argument("--no-prime", action="store_true",
                    help="跳过 attach 后的自动点击「交易→模拟」流程")
    ap.add_argument("--save-frames", action="store_true",
                    help="把协议层原始帧落盘到 /tmp/ths_frames（默认关闭）")
    ap.add_argument("--clean-frames", type=int, default=None, metavar="DAYS",
                    help="启动时删除 /tmp/ths_frames 里超过 DAYS 天的旧帧")
    args = ap.parse_args()

    if not os.path.isfile(AGENT_JS):
        sys.exit(f"agent not found: {AGENT_JS}")

    if args.clean_frames is not None:
        _removed = _frame_saver.purge_old(args.clean_frames)
        log.info("clean-frames: removed %d files older than %dd", _removed, args.clean_frames)
    if args.save_frames:
        _frame_saver.set_enabled(True)
        log.info("frame saving ENABLED -> %s (keep_days=%d)", FRAMES_DIR, FRAME_KEEP_DAYS)
    else:
        log.info("frame saving disabled (use --save-frames or THS_FRAME_SAVE=1 to enable)")

    _no_prime = getattr(args, "no_prime", False)

    if args.spawn:
        # spawn 模式：先杀已有副本，再 spawn+attach，确保 hook 最早生效
        copy_pid = _get_copy_pid()
        if copy_pid:
            log.info("killing existing copy pid=%d before spawn", copy_pid)
            os.kill(copy_pid, signal.SIGKILL)
            time.sleep(1)
        _ensure_plist_no_restore()
        try:
            _agent.spawn_connect()
        except Exception as e:
            log.error("spawn failed: %s", e)
            sys.exit(1)
        if not _no_prime:
            threading.Thread(target=_prime_trade_panel, daemon=True).start()

        def _spawn_reconnect():
            while True:
                time.sleep(5)
                if not _agent.connected:
                    log.warning("agent disconnected after spawn, re-spawning...")
                    try:
                        c = _get_copy_pid()
                        if c:
                            os.kill(c, signal.SIGKILL)
                            time.sleep(1)
                        _agent.spawn_connect()
                        if not _no_prime:
                            _prime_trade_panel()
                    except Exception as e:
                        log.error("re-spawn failed: %s", e)
        t = threading.Thread(target=_spawn_reconnect, daemon=True)
        t.start()

    elif args.pid:
        # 直接 attach 模式：不调用 ensure_app，不自动重启
        log.info("direct-pid mode: attaching to pid=%d (no auto-restart)", args.pid)
        try:
            _agent.connect(args.pid)
        except Exception as e:
            log.error("attach failed: %s", e)
            sys.exit(1)
        if not _no_prime:
            threading.Thread(target=_prime_trade_panel, daemon=True).start()

        # 静态重连：只有 agent 断开才重连到同一 pid，不重启进程
        def _static_reconnect():
            while True:
                time.sleep(5)
                if not _agent.connected:
                    log.warning("agent disconnected, re-attaching to pid=%d", args.pid)
                    try:
                        _agent.connect(args.pid)
                        if not _no_prime:
                            _prime_trade_panel()
                    except Exception as e:
                        log.error("re-attach failed: %s", e)
        t = threading.Thread(target=_static_reconnect, daemon=True)
        t.start()
    else:
        # 原始自动模式（attach 已跑进程）
        try:
            pid = ensure_app()
            _agent.connect(pid)
        except Exception as e:
            log.error("initial connect failed: %s", e)
        if not _no_prime:
            threading.Thread(target=_prime_trade_panel, daemon=True).start()

        t = threading.Thread(target=reconnect_loop, daemon=True)
        t.start()

    # 前台运行 socket 服务
    serve()


if __name__ == "__main__":
    main()
