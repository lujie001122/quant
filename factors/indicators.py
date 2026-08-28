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

# 从独立因子文件导入（避免重复定义）
from factors.macd import calc_macd
from factors.atr import calc_atr
from factors.ao import calc_ao


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