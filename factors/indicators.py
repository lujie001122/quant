#!/usr/bin/env python3
"""
factors/ — 指标计算因子模块
从 market_data.py 提取的纯计算函数，无IO依赖。

包含:
  - calc_ma: 简单移动平均
  - calc_rsi_wilder: Wilder RSI
  - calc_macd: SMA-init MACD
  - calc_atr: 真实波幅均值
  - calc_ao: Awesome Oscillator
  - calc_vol_ratio: 量比
  - calc_5min_indicators: 5分钟分时指标
  - dynamic_spacing: 动态网格间距
"""

from typing import List, Dict, Optional


# ═══════════════════════════════════════════════════════
# 基础指标
# ═══════════════════════════════════════════════════════

def calc_ma(closes: list, period: int) -> Optional[float]:
    """简单移动平均"""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_rsi_wilder(closes: list, period: int = 14) -> Optional[float]:
    """Wilder平滑RSI"""
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


def calc_macd(closes: list, fast: int = 12, slow: int = 26, sig: int = 9):
    """
    SMA-init MACD — 与 backtest_bt.py 完全一致
    返回: (dif, dea, bar, status)
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


def calc_atr(klines: list, period: int = 14) -> Optional[float]:
    """Wilder平滑ATR"""
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


def calc_ao(highs: list, lows: list, short: int = 5, long: int = 34):
    """
    Awesome Oscillator: SMA5(median) - SMA34(median)
    返回: (ao_now, ao_5ago)
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


def calc_vol_ratio(volumes: list, period: int = 20) -> Optional[float]:
    """量比: 当前量 / 过去N期均量"""
    if len(volumes) < period + 1:
        return None
    avg_vol = sum(volumes[-(period + 1):-1]) / period
    if avg_vol == 0:
        return None
    return round(volumes[-1] / avg_vol, 2)


# ═══════════════════════════════════════════════════════
# 复合指标
# ═══════════════════════════════════════════════════════

def calc_5min_indicators(klines_5min: list) -> Optional[Dict]:
    """
    基于5分钟K线计算日内分时指标
    返回:
      rsi_5min, macd_5min_status/dif/dea/bar,
      vol_ratio_5min, atr_5min, ma20_5min_slope,
      day_high, day_low, day_pct
    """
    if not klines_5min or len(klines_5min) < 14:
        return None

    closes = [k["close"] for k in klines_5min]
    volumes = [k["volume"] for k in klines_5min]
    highs = [k["high"] for k in klines_5min]
    lows = [k["low"] for k in klines_5min]

    rsi_5min = calc_rsi_wilder(closes, 14)
    dif5, dea5, bar5, status5 = calc_macd(closes, fast=5, slow=34, sig=5)

    vol_ratio_5min = None
    if len(volumes) >= 21:
        avg_vol_20 = sum(volumes[-21:-1]) / 20
        if avg_vol_20 > 0:
            vol_ratio_5min = round(volumes[-1] / avg_vol_20, 2)

    if len(klines_5min) >= 2:
        day_open = klines_5min[0]["open"]
        day_high = max(highs)
        day_low = min(lows)
        day_close = closes[-1]
        day_pct = round((day_close - day_open) / day_open * 100, 2) if day_open > 0 else 0
    else:
        day_open = klines_5min[0]["open"] if klines_5min else 0
        day_high = day_open
        day_low = day_open
        day_close = closes[-1] if closes else 0
        day_pct = 0

    atr_5min = calc_atr(klines_5min, 14)

    ma20_5min_slope = None
    if len(closes) >= 20:
        ma20_now = sum(closes[-20:]) / 20
        if len(closes) >= 25:
            ma20_5ago = sum(closes[-25:-5]) / 20
            if ma20_5ago and ma20_5ago > 0:
                ma20_5min_slope = round((ma20_now - ma20_5ago) / ma20_5ago, 6)

    return {
        "rsi_5min": rsi_5min,
        "macd_5min_status": status5,
        "macd_5min_dif": dif5,
        "macd_5min_dea": dea5,
        "macd_5min_bar": bar5,
        "vol_ratio_5min": vol_ratio_5min,
        "atr_5min": atr_5min,
        "ma20_5min_slope": ma20_5min_slope,
        "day_high": day_high,
        "day_low": day_low,
        "day_pct": day_pct,
    }


def dynamic_spacing(atr: Optional[float], price: float, base_spacing: float) -> float:
    """
    动态网格间距: 0.6*base + 0.4*atr_pct, 钳制在 [base*0.5, base*2.0]
    """
    if atr is None or price == 0:
        return base_spacing
    atr_pct = atr / price
    adjusted = 0.6 * base_spacing + 0.4 * atr_pct
    return round(max(base_spacing * 0.5, min(base_spacing * 2.0, adjusted)), 4)