#!/usr/bin/env python3
"""
factors/ — 指标计算因子模块

从 market_data.py 提取的纯计算函数，无IO依赖。

各因子独立文件:
  - rsi.py: calc_rsi
  - macd.py: calc_macd
  - atr.py: calc_atr
  - ao.py: calc_ao
  - indicators.py: 复合指标 (calc_5min_indicators, dynamic_spacing 等)

向后兼容: 所有函数名保持与 market_data.py 一致。
"""

# 从独立因子文件导入
from factors.rsi import calc_rsi
from factors.macd import calc_macd
from factors.atr import calc_atr
from factors.ao import calc_ao

# 从 indicators.py 导入复合指标和辅助函数
from factors.indicators import (
    calc_ma,
    calc_rsi_wilder,
    calc_vol_ratio,
    calc_5min_indicators,
    dynamic_spacing,
)

# calc_rsi_wilder 是 calc_rsi 的别名，保持兼容
calc_rsi_wilder = calc_rsi