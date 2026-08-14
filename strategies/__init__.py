#!/usr/bin/env python3
"""
strategies/ — 策略判定模块包

从 strategy.py 拆分:
  - BaseStrategy: 抽象基类
  - RSIMACDStrategy: RSI+MACD 建仓/止损策略
  - T0Strategy: 做T配对策略
  - GridStrategy: 网格加仓减仓策略

保持与 strategy.py 的完全向后兼容。
"""

from strategies.base import BaseStrategy
from strategies.rsi_macd import RSIMACDStrategy
from strategies.t0 import T0Strategy
from strategies.grid import GridStrategy

# 向后兼容: 保持原有模块级函数引用
# (strategy.py 负责 final re-export)