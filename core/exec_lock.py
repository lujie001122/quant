#!/usr/bin/env python3
"""
exec_lock.py — 执行层互斥锁（同一 mode 同一时刻只允许一个进程执行）

锁文件: <repo>/.run/execute_<mode>.lock
  - flock(LOCK_EX|LOCK_NB)：内核级排他锁，进程退出/崩溃自动释放，不会留永久死锁
  - 锁文件内写 JSON 元数据 {pid, since, since_ts, mode, argv}：
    抢不到锁时直接打印持有者 pid/since，方便定位是哪个进程在跑
  - 僵尸锁：持有进程崩溃时 flock 由内核自动释放，下一个进程直接拿到锁并覆盖元数据；
    锁文件里残留的旧元数据不会被删除（避免 unlink 竞态：删文件会让另一个进程
    打开新 inode 拿到锁，等于没锁）
  - LOCK_TIMEOUT（默认 600s = 10 分钟）用于告警：持有者存活但超过该时长仍占锁，
    打印 [LOCK] 提示，仍不抢占 —— 宁可少下一单，也不能把正在执行的下单抢成并发

用法:
  from core.exec_lock import ExecLock
  lock = ExecLock('t0')
  if not lock.acquire():
      return          # 已有执行在进行中，本次跳过（退出码 0）
  try:
      ...
  finally:
      lock.release()
"""

import fcntl
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_DIR = os.path.join(ROOT, ".run")
LOCK_TIMEOUT = int(os.environ.get("QUANT_LOCK_TIMEOUT", "600") or 600)
_READ_RETRIES = 3
_READ_RETRY_WAIT = 0.05


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:      # 进程存在但无权限发信号
        return True
    except OSError:
        return False


def _read_lock_info(path: str):
    """读锁文件元数据。写入方只做一次 write+fsync，这里仍重试几次防读到半截。"""
    for _ in range(_READ_RETRIES):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        if text.strip():
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        time.sleep(_READ_RETRY_WAIT)
    return None


class ExecLock:
    """按 mode 划分的 flock 文件锁。"""

    def __init__(self, mode: str, timeout: int = None):
        mode = str(mode or "").strip()
        if not mode:
            raise ValueError("ExecLock 需要 mode（t0 / buy_sell / t0_close）")
        self.mode = mode
        self.timeout = LOCK_TIMEOUT if timeout is None else int(timeout)
        self.path = os.path.join(LOCK_DIR, f"execute_{mode}.lock")
        self._fd = None

    # ── 锁被占用时的提示 ──────────────────────────────────────────────
    def _report_busy(self, info: dict) -> None:
        pid = info.get("pid", "未知")
        since = info.get("since") or "未知"
        try:
            since_ts = float(info.get("since_ts") or 0)
        except (TypeError, ValueError):
            since_ts = 0.0
        if since_ts > 0 and _pid_alive(pid):
            age = time.time() - since_ts
            if age > self.timeout:
                print(f"[LOCK] 锁已持续 {age:.0f}s（>{self.timeout}s），"
                      f"持有进程 pid={pid} 仍存活，不抢占：{self.path}")
        print(f"已有执行在进行中（pid={pid} since={since}），本次跳过")

    # ── 加锁 ──────────────────────────────────────────────────────────
    def acquire(self) -> bool:
        """非阻塞加锁。成功 True；已被占用 False（调用方打印提示后退出 0）。"""
        os.makedirs(LOCK_DIR, exist_ok=True)
        for attempt in (1, 2):
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                info = _read_lock_info(self.path) or {}
                os.close(fd)
                if attempt == 1:
                    # 持有者可能刚释放锁：稍等重试一次，避免把"刚好结束"误判成跳过
                    time.sleep(0.2)
                    continue
                self._report_busy(info)
                return False

            meta = {
                "pid": os.getpid(),
                "since": _now_str(),
                "since_ts": time.time(),
                "mode": self.mode,
                "argv": os.path.basename(sys.argv[0] or ""),
            }
            try:
                os.ftruncate(fd, 0)
                os.write(fd, json.dumps(meta, ensure_ascii=False).encode("utf-8"))
                os.fsync(fd)
            except OSError:
                pass
            self._fd = fd
            return True

        self._report_busy({})
        return False

    # ── 解锁 ──────────────────────────────────────────────────────────
    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        finally:
            self._fd = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"已有执行在进行中（mode={self.mode}）")
        return self

    def __exit__(self, *exc_info):
        self.release()
        return False
