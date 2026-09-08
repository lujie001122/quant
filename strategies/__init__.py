#!/usr/bin/env python3
"""
strategies/ — 策略判定模块包

从 strategy.py 拆分:
  - BaseStrategy: 抽象基类
  - RSIMACDStrategy: RSI+MACD 建仓/止损策略
  - T0Strategy: 做T配对策略
"""

from strategies.base import BaseStrategy
from strategies.rsi_macd import RSIMACDStrategy
from strategies.t0 import T0Strategy
