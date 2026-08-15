#!/usr/bin/env python3
"""
factors/ao.py — AO 指标计算（Awesome Oscillator）

从 market_data.py 提取的纯计算函数，无IO依赖。
"""

from typing import Optional


def calc_ao(highs, lows, short=5, long=34):
    """
    Awesome Oscillator: SMA5(median) - SMA34(median)。

    参数:
      highs: 最高价序列
      lows: 最低价序列
      short: 短周期（默认5）
      long: 长周期（默认34）

    返回:
      (ao_now, ao_5ago)
    """
    if len(highs) < long:
        return None, None
    medians = [(h + l) / 2 for h, l in zip(highs, lows)]
    sma5 = sum(medians[-short:]) / short
    sma34 = sum(medians[-long:]) / long
    ao_now = sma5 - sma34
    # AO 5 bars ago
    if len(medians) >= long + 5:
        sma5_5 = sum(medians[-short - 5:-5]) / short
        sma34_5 = sum(medians[-long - 5:-5]) / long
    else:
        sma5_5, sma34_5 = sma5, sma34
    ao_5ago = sma5_5 - sma34_5
    return ao_now, ao_5ago