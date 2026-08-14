#!/usr/bin/env python3
"""
factors/atr.py — ATR 指标计算（Average True Range）

从 market_data.py 提取的纯计算函数，无IO依赖。
"""

from typing import Optional


def calc_atr(klines, period=14):
    """
    Wilder平滑ATR。

    参数:
      klines: K线数组 [{high, low, close}, ...]
      period: ATR周期（默认14）

    返回:
      ATR值，数据不足返回 None
    """
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        k_prev, k_curr = klines[i - 1], klines[i]
        tr = max(
            k_curr["high"] - k_curr["low"],
            abs(k_curr["high"] - k_prev["close"]),
            abs(k_curr["low"] - k_prev["close"]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return round(atr, 4)