#!/usr/bin/env python3
"""
factors/macd.py — MACD 指标计算

从 market_data.py 提取的纯计算函数，无IO依赖。
"""

from typing import Optional, Tuple


def calc_macd(closes, fast=12, slow=26, sig=9):
    """
    SMA-init MACD — 与 backtest_bt.py 完全一致。

    参数:
      closes: 收盘价序列
      fast: 快线周期（默认12）
      slow: 慢线周期（默认26）
      sig: 信号线周期（默认9）

    返回:
      (dif, dea, bar, status)
      status: "金叉"/"死叉"/"红柱放大"/"红柱缩短"/"绿柱放大"/"绿柱缩短"/"震荡"/"数据不足"
    """
    if len(closes) < slow + sig:
        return None, None, None, "数据不足"

    # EMA初始化
    e12 = sum(closes[:fast]) / fast
    e26 = sum(closes[:slow]) / slow

    difs = []
    for i in range(len(closes)):
        if i < fast:
            difs.append(e12 - e26)
            continue
        e12 = closes[i] * (2 / (fast + 1)) + e12 * (1 - 2 / (fast + 1))
        if i < slow:
            difs.append(e12 - e26)
            continue
        e26 = closes[i] * (2 / (slow + 1)) + e26 * (1 - 2 / (slow + 1))
        difs.append(e12 - e26)

    e9 = difs[slow]
    deas = []
    for d in difs[slow + 1:]:
        e9 = d * (2 / (sig + 1)) + e9 * (1 - 2 / (sig + 1))
        deas.append(e9)

    if len(deas) < 2:
        return None, None, None, "数据不足"

    d_now, d_prev = difs[-1], difs[-2]
    e_now, e_prev = deas[-1], deas[-2]
    h_now = 2 * (d_now - e_now)
    h_prev = 2 * (d_prev - e_prev)

    if d_now > e_now and d_prev <= e_prev:
        s = "金叉"
    elif d_now < e_now and d_prev >= e_prev:
        s = "死叉"
    elif d_now > e_now and h_now > h_prev:
        s = "红柱放大"
    elif d_now > e_now and h_now < h_prev:
        s = "红柱缩短"
    elif d_now < e_now and h_now < h_prev:
        s = "绿柱放大"
    elif d_now < e_now and h_now > h_prev:
        s = "绿柱缩短"
    else:
        s = "震荡"

    return round(d_now, 4), round(e_now, 4), round(h_now, 4), s