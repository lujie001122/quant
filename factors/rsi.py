#!/usr/bin/env python3
"""
factors/rsi.py — RSI 指标计算

从 market_data.py 提取的纯计算函数，无IO依赖。
"""

from typing import Optional


def calc_rsi(closes, period=14):
    """
    Wilder平滑RSI

    参数:
      closes: 收盘价序列
      period: RSI周期（默认14）

    返回:
      RSI值（0-100），数据不足返回 None
    """
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)