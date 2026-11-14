#!/usr/bin/env python3
"""
daily_baseline.py — 每标的当日开盘/前一日收盘持仓快照

用途: t0_close.py 以「真实持仓 - 基线持仓」判定单边敞口，基线必须固定，
      不能随日内成交/同步变化。快照只在开盘后第一次 trade.py sync 时落盘，
      盘中再 sync 不覆盖（文件已存在则跳过）。

文件: logs/t0_baseline_YYYYMMDD.json
  {"date": "2026-11-14", "created_at": "...", "source": "trade.py sync",
   "positions": {"159928": 15100, ...}}

用法:
  from core.daily_baseline import load_baseline, snapshot_if_missing
"""

import json
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_DIR = os.path.join(ROOT, "logs")

# 只在这个时段内建立基线（开盘后 5 分钟内），避免盘中 sync 写进"已被交易过"的持仓
SNAPSHOT_DEADLINE = "09:35"


def _day(day: str = None) -> str:
    return day or datetime.now().strftime("%Y%m%d")


def path_for(day: str = None) -> str:
    return os.path.join(BASELINE_DIR, f"t0_baseline_{_day(day)}.json")


def load_baseline(day: str = None) -> dict:
    """返回当日基线 {code: shares}；文件缺失/损坏返回 {}。"""
    path = path_for(day)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        positions = data.get("positions") or {}
        return {str(k): int(v) for k, v in positions.items()}
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return {}


def snapshot_if_missing(positions: dict, day: str = None) -> bool:
    """开盘后第一次 sync 时写基线；文件已存在或不在开盘时段则不动。返回是否写入。"""
    if not positions:
        return False
    path = path_for(day)
    if os.path.exists(path):
        return False
    now = datetime.now()
    if now.strftime("%H:%M") > SNAPSHOT_DEADLINE:
        return False
    data = {
        "date": now.strftime("%Y-%m-%d"),
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "trade.py sync",
        "positions": {str(k): int(v.get("shares", 0) if isinstance(v, dict) else v)
                      for k, v in positions.items()},
    }
    os.makedirs(BASELINE_DIR, exist_ok=True)
    try:
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        return False
