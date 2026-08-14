#!/usr/bin/env python3
"""
策略基类
定义所有策略判定接口（协议/鸭子类型，不做严格的抽象强制）
"""


class BaseStrategy:
    """策略基类 — 定义所有策略判定接口

    子类只需实现自己关心的方法。调用方使用模块级函数，不直接依赖抽象接口。
    """

    def evaluate_stop(self, pos, t, price, today_str):
        """评估止盈止损信号
        返回: stop_actions: [(signal_name, signal_type), ...]
        """
        raise NotImplementedError

    def evaluate_entry(self, pos, t, price, realtime, positions, all_klines, code, today_str, atr_pct, defense_weak):
        """评估建仓信号
        返回: (action, position_ratio, trade_type, reason) 或 None
        """
        raise NotImplementedError

    def evaluate_t0(self, t, pos, price, today_str, atr_pct):
        """评估做T信号
        返回: dict with keys: t0_signal, buy_score, sell_score, ...
        """
        raise NotImplementedError

    def evaluate_t0_execute(self, result, t, pos, price, today_str, atr_pct, record_t0=True):
        """从 evaluate_t0 结果解析执行动作
        返回: (action, ratio, trade_type, reason, t0_pair) 或 None
        """
        raise NotImplementedError

    def evaluate_grid_signals(self, price, base_price, spacing, grid_frozen=False):
        """网格触达判定
        返回: (signal_str, weight)
        """
        raise NotImplementedError

    def check_grid_reset(self, price, base_price, spacing, date_str, last_reset_date=None):
        """网格基准重置
        返回: (new_base_price, reset, new_reset_date)
        """
        raise NotImplementedError