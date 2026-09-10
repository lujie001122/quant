#!/usr/bin/env python3
"""
资金管理器 — 做T配对价计算

v2 阶段2: 仅保留 calc_t0_pair_price，其余函数已迁移或废弃。

用法:
  from money_manager import calc_t0_pair_price
  pair_price = calc_t0_pair_price(price, shares, is_buy_t0, atr_5min=atr)
"""


import os, yaml as _yaml
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
try:
    with open(_CONFIG_PATH) as _f:
        _CONF = _yaml.safe_load(_f) or {}
except Exception:
    _CONF = {}


MIN_SHARES = _CONF.get("trade", {}).get("min_shares", 5000)  # 从config读取，对齐实盘


class MoneyManager:
    """资金管理器 — v2: 仅保留做T配对价计算"""

    @staticmethod
    def calc_t0_pair_price(current_price, shares, is_buy_t0, atr_5min=None, pair_atr_multiplier=None):
        """计算做T配对挂单价

        Bug D 修复：优先使用ATR乘数计算价差，否则回退到固定min_spread。

        参数:
          current_price: 当前价
          shares: 配对股数
          is_buy_t0: True=T0买入(配对卖出), False=T0卖出(配对买入)
          atr_5min: 5分钟ATR值（用于动态价差计算）
          pair_atr_multiplier: ATR乘数（从config t0.pair_atr_multiplier读取）

        返回:
          float: 配对挂单价, 或 0.0 表示无效

        公式:
          - 优先ATR动态价差: spread = atr_5min * pair_atr_multiplier
          - 回退固定价差: spread = min_spread (150元)
          - 买入信号: pair_price = (shares*price + spread) / shares
          - 卖出信号: pair_price = (shares*price - spread) / shares
        """
        if shares <= 0 or current_price <= 0:
            return 0.0

        # Bug D: 优先使用ATR动态价差
        spread = _CONF.get("t0", {}).get("min_spread", 150)  # 默认固定价差
        if atr_5min is not None and atr_5min > 0:
            if pair_atr_multiplier is None:
                pair_atr_multiplier = _CONF.get("t0", {}).get("pair_atr_multiplier", 1.1)
            atr_spread = atr_5min * pair_atr_multiplier * shares
            if atr_spread > 0:
                spread = atr_spread  # ATR价差生效

        if is_buy_t0:
            # 做T买入：先买后卖，配对的卖出价需高于买入价
            return round(current_price + spread / shares, 3)
        else:
            # 做T卖出：先卖后买，配对的买入价需低于卖出价
            return round(current_price - spread / shares, 3)


# ═══════════════════════════════════════════════
# 模块级快捷函数
# ═══════════════════════════════════════════════

def calc_t0_pair_price(current_price, shares, is_buy_t0, atr_5min=None, pair_atr_multiplier=None):
    return MoneyManager.calc_t0_pair_price(current_price, shares, is_buy_t0, atr_5min, pair_atr_multiplier)
