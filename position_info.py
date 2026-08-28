#!/usr/bin/env python3
"""
PositionInfo 类 + 模块级常量

Phase 3 重构后从 strategy.py 迁移至此。
PositionInfo 是共享状态模型，所有策略模块和回测引擎都依赖它。
"""

# ═══════════════════════════════════════════════
# 模块级常量（默认值，会被 config.yaml 覆盖）
# ═══════════════════════════════════════════════

import yaml
import os

def _load_config():
    """从 config.yaml 加载配置，返回合并后的默认值"""
    _DEFAULTS = {
        'total_fund': 220000,
        'dead_ratio': 0.3,      # 底仓30%
        'active_ratio': 0.7,    # 活动仓70%
        'grid_buy_levels': 5,
        'grid_sell_levels': 3,
        'max_daily_buys': 1,
        'max_daily_t0': 2,
        'defense_code': '159611',
        'cooldown_days': 2,
        'inverted_weights': [0.20, 0.25, 0.30, 0.35, 0.40],
    }
    config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
    try:
        with open(config_path, 'r') as f:
            cfg = yaml.safe_load(f) or {}
        _DEFAULTS['total_fund'] = cfg.get('total_fund', _DEFAULTS['total_fund'])
        _DEFAULTS['dead_ratio'] = cfg.get('dead_ratio', _DEFAULTS['dead_ratio'])
        _DEFAULTS['active_ratio'] = cfg.get('active_ratio', _DEFAULTS['active_ratio'])
        _DEFAULTS['grid_buy_levels'] = cfg.get('grid_buy_levels', _DEFAULTS['grid_buy_levels'])
        _DEFAULTS['grid_sell_levels'] = cfg.get('grid_sell_levels', _DEFAULTS['grid_sell_levels'])
        _DEFAULTS['max_daily_buys'] = cfg.get('max_daily_buys', _DEFAULTS['max_daily_buys'])
        _DEFAULTS['max_daily_t0'] = cfg.get('max_daily_t0', _DEFAULTS['max_daily_t0'])
        _DEFAULTS['defense_code'] = cfg.get('defense_code', _DEFAULTS['defense_code'])
        _DEFAULTS['cooldown_days'] = cfg.get('cooldown_days', _DEFAULTS['cooldown_days'])
        _DEFAULTS['inverted_weights'] = cfg.get('inverted_weights', _DEFAULTS['inverted_weights'])
        _DEFAULTS['max_per_etf'] = cfg.get('max_per_etf', 220000)  # 极限方案C: 100%
        _DEFAULTS['max_position_ratio'] = cfg.get('entry', {}).get('position_cap', 1.00)  # 极限方案C
        _DEFAULTS['max_daily_loss_pct'] = cfg.get('stop_loss', {}).get('daily_loss_limit_pct', 0.20)  # 极限方案C
        _DEFAULTS['trend_profit_max_daily'] = cfg.get('trend_profit', {}).get('max_daily', 5)
        _DEFAULTS['trend_profit_cooldown_days'] = cfg.get('trend_profit', {}).get('cooldown_days', 1)
    except Exception:
        pass
    return _DEFAULTS

_config = _load_config()

TOTAL_FUND = _config['total_fund']
DEAD_RATIO = _config['dead_ratio']
ACTIVE_RATIO = _config['active_ratio']
GRID_BUY_LEVELS = _config['grid_buy_levels']
GRID_SELL_LEVELS = _config['grid_sell_levels']
MAX_DAILY_BUYS = _config['max_daily_buys']
MAX_DAILY_T0 = _config['max_daily_t0']
DEFENSE_CODE = _config['defense_code']
COOLDOWN_DAYS = _config['cooldown_days']
INVERTED_WEIGHTS = _config['inverted_weights']
MAX_PER_ETF = _config.get('max_per_etf', 44000)
MAX_POSITION_RATIO = _config.get('max_position_ratio', 0.50)
MAX_DAILY_LOSS_PCT = _config.get('max_daily_loss_pct', 0.12)

TREND_PROFIT_MAX_DAILY = _config.get('trend_profit_max_daily', 5)
TREND_PROFIT_COOLDOWN_DAYS = _config.get('trend_profit_cooldown_days', 1)


class PositionInfo:
    def __init__(self, base_price=None, shares=0, peak_price=0.0):
        self.base_price = base_price
        self.shares = shares
        self.avg_cost = 0.0
        self.current_price = 0.0
        self.peak_price = peak_price
        self.dead_shares = 0
        self.active_shares = 0
        self.daily_trade_log = {}
        self.stop_level = 0
        self.below_ma20_count = 0
        self.below_ma20_date = None
        self.trailing_stop_price = 0.0
        self.reached_8pct = False
        self.reached_15pct = False
        self.build_phase = 0
        self.build_first_price = 0.0
        self.ma5_touch_count = 0
        self.last_reset_date = None
        self.cooldown_until = None
        self.liquidate_dates = []
        self.grid_frozen = False
        self.last_grid_trigger = None
        self.prev_rsi = None
        self.add_count = 0
        self.empty_days = 0
        self.empty_days_date = None
        self.prev_macd_status = None
        self.breakeven_activated = False
        self.entry_avg_cost = 0.0
        self.trend_profit_trigger_date = None
        self.ma5_sell_trigger_date = None

    @property
    def entry_cost(self):
        return self.entry_avg_cost if self.entry_avg_cost > 0 else self.avg_cost

    @property
    def has_position(self):
        return self.shares > 0

    def profit_pct(self, current_price):
        if self.avg_cost == 0: return 0.0
        return (current_price - self.avg_cost) / self.avg_cost * 100

    def drawdown_from_peak(self, current_price):
        if self.peak_price == 0: return 0.0
        return (self.peak_price - current_price) / self.peak_price * 100

    def max_daily_buys(self, atr_pct):
        if atr_pct > 0.05: return 2
        return MAX_DAILY_BUYS

    def max_daily_t0(self, atr_pct):
        if atr_pct > 0.05: return 3
        return MAX_DAILY_T0

    def _get_daily_log(self, date_str):
        log = self.daily_trade_log.get(date_str, {"buy_count": 0, "t0_count": 0, "trend_profit_count": 0, "ma5_sell_count": 0})
        if not isinstance(log, dict):
            log = {"buy_count": 0, "t0_count": 0, "trend_profit_count": 0, "ma5_sell_count": 0}
        return log

    def can_buy_today(self, date_str, atr_pct=None):
        max_b = self.max_daily_buys(atr_pct) if atr_pct else MAX_DAILY_BUYS
        return self._get_daily_log(date_str).get("buy_count", 0) < max_b

    def can_t0_today(self, date_str, atr_pct=None):
        max_t = self.max_daily_t0(atr_pct) if atr_pct else MAX_DAILY_T0
        return self._get_daily_log(date_str).get("t0_count", 0) < max_t

    def record_buy(self, date_str):
        log = self.daily_trade_log.setdefault(date_str, {"buy_count": 0, "t0_count": 0})
        log["buy_count"] = log.get("buy_count", 0) + 1

    def record_t0(self, date_str):
        log = self.daily_trade_log.setdefault(date_str, {"buy_count": 0, "t0_count": 0, "trend_profit_count": 0, "ma5_sell_count": 0})
        log["t0_count"] = log.get("t0_count", 0) + 1

    def can_trend_profit_today(self, date_str):
        """趋势止盈冷却检查：单日最多 N 次，触发后冷却 N 天（从次日算起）"""
        log = self._get_daily_log(date_str)
        if log.get("trend_profit_count", 0) >= TREND_PROFIT_MAX_DAILY:
            return False
        if self.trend_profit_trigger_date is None:
            return True
        try:
            from datetime import datetime as _dt, timedelta as _td
            d = _dt.strptime(date_str, "%Y-%m-%d")
            t = _dt.strptime(self.trend_profit_trigger_date, "%Y-%m-%d")
            cooldown_end = t + _td(days=TREND_PROFIT_COOLDOWN_DAYS)
            return d <= t or d > cooldown_end
        except Exception:
            return True

    def record_trend_profit(self, date_str):
        log = self.daily_trade_log.setdefault(date_str, {"buy_count": 0, "t0_count": 0, "trend_profit_count": 0, "ma5_sell_count": 0})
        log["trend_profit_count"] = log.get("trend_profit_count", 0) + 1
        self.trend_profit_trigger_date = date_str

    def can_ma5_sell_today(self, date_str):
        """破MA5卖活动仓冷却检查：单日最多 N 次，触发后冷却 N 天（从次日算起）"""
        log = self._get_daily_log(date_str)
        if log.get("ma5_sell_count", 0) >= TREND_PROFIT_MAX_DAILY:
            return False
        if self.ma5_sell_trigger_date is None:
            return True
        try:
            from datetime import datetime as _dt, timedelta as _td
            d = _dt.strptime(date_str, "%Y-%m-%d")
            t = _dt.strptime(self.ma5_sell_trigger_date, "%Y-%m-%d")
            cooldown_end = t + _td(days=TREND_PROFIT_COOLDOWN_DAYS)
            return d <= t or d > cooldown_end
        except Exception:
            return True

    def record_ma5_sell(self, date_str):
        log = self.daily_trade_log.setdefault(date_str, {"buy_count": 0, "t0_count": 0, "trend_profit_count": 0, "ma5_sell_count": 0})
        log["ma5_sell_count"] = log.get("ma5_sell_count", 0) + 1
        self.ma5_sell_trigger_date = date_str

    def is_in_cooldown(self, date_str):
        if self.cooldown_until is None: return False
        return date_str <= self.cooldown_until

    def update_trailing_stop(self, price):
        if not self.has_position or self.avg_cost == 0: return
        profit_pct = self.profit_pct(price)
        # 从config读取trail_pct，集中模式用12%止盈，非集中模式保持7%止盈
        trail_pct = _config.get('trail_pct', 0.12)
        concentrated = _config.get('concentrated_mode', False)
        if concentrated:
            activation = trail_pct + 0.01
            lockin = trail_pct + 0.08
            trail_mult = 1 - trail_pct
        else:
            activation = 0.08
            lockin = 0.15
            trail_mult = 0.93
        if profit_pct >= activation - 0.0001 and not self.reached_8pct:
            self.reached_8pct = True
        if profit_pct >= lockin - 0.0001 and not self.reached_15pct:
            self.reached_15pct = True
            self.trailing_stop_price = self.avg_cost * 1.05
        if self.reached_15pct and self.peak_price > 0:
            self.trailing_stop_price = self.peak_price * trail_mult

    def update_dead_active(self):
        self.dead_shares = int(self.shares * DEAD_RATIO)
        self.active_shares = self.shares - self.dead_shares
        if self.active_shares == 0 and self.shares > 0:
            self.active_shares = int(self.shares * ACTIVE_RATIO)

    def reset_on_liquidate(self, date_str):
        self.shares = 0
        self.avg_cost = 0
        self.entry_avg_cost = 0
        self.base_price = None
        self.dead_shares = 0
        self.active_shares = 0
        self.build_phase = 0
        self.build_first_price = 0.0
        self.stop_level = 0
        self.below_ma20_count = 0
        self.below_ma20_date = None
        self.add_count = 0
        self.ma5_touch_count = 0
        self.reached_8pct = False
        self.reached_15pct = False
        self.trailing_stop_price = 0.0
        self.grid_frozen = False
        self.last_grid_trigger = None
        self.peak_price = 0.0
        self.breakeven_activated = False
        self.liquidate_dates.append(date_str)
        self.empty_days = 0
        try:
            from datetime import datetime as _dt, timedelta as _td
            dt = _dt.strptime(date_str, "%Y-%m-%d")
            self.cooldown_until = (dt + _td(days=COOLDOWN_DAYS)).strftime("%Y-%m-%d")
        except Exception:
            self.cooldown_until = None

    def reduce_shares(self, pct, price=0.0):
        reduce_count = int(self.shares * pct / 10000) * 100
        if reduce_count < 100:
            reduce_count = int(self.shares * pct / 100)
            reduce_count = reduce_count // 100 * 100
            if reduce_count < 100 and self.shares >= 100:
                reduce_count = 100
        if reduce_count > self.shares:
            reduce_count = self.shares
        self.shares -= reduce_count
        self.peak_price = price
        self.update_dead_active()

    def add_shares(self, buy_price, buy_shares):
        """加仓/做T买入：加权平均更新 entry_avg_cost。卖出时不变。"""
        old_shares = self.shares
        new_shares = old_shares + buy_shares
        if self.entry_avg_cost > 0 and old_shares > 0:
            self.entry_avg_cost = (self.entry_avg_cost * old_shares + buy_price * buy_shares) / new_shares
        else:
            self.entry_avg_cost = buy_price

    def _enter_position(self, date_str, price, channel_name):
        self.record_buy(date_str)
        self.build_phase = 1
        self.build_first_price = price
        self.base_price = price
        self.empty_days = 0
        return "买入", channel_name, "buy"

    def to_dict(self, today_str):
        return {
            "build_phase": self.build_phase,
            "build_first_price": self.build_first_price,
            "stop_level": self.stop_level,
            "reached_8pct": self.reached_8pct,
            "reached_15pct": self.reached_15pct,
            "trailing_stop_price": self.trailing_stop_price,
            "cooldown_until": self.cooldown_until,
            "grid_frozen": self.grid_frozen,
            "last_grid_trigger": self.last_grid_trigger,
            "last_reset_date": self.last_reset_date,
            "peak_price": self.peak_price,
            "breakeven_activated": self.breakeven_activated,
            "below_ma20_count": self.below_ma20_count,
            "below_ma20_date": self.below_ma20_date,
            "add_count": self.add_count,
            "ma5_touch_count": self.ma5_touch_count,
            "prev_rsi": self.prev_rsi,
            "prev_macd_status": self.prev_macd_status,
            "liquidate_dates": self.liquidate_dates,
            "empty_days": self.empty_days,
            "empty_days_date": self.empty_days_date,
            "daily_trade_log": {today_str: self.daily_trade_log.get(today_str, {"buy_count": 0, "t0_count": 0})},
            "trend_profit_trigger_date": self.trend_profit_trigger_date,
            "ma5_sell_trigger_date": self.ma5_sell_trigger_date,
            "confirm_batch_count": getattr(self, 'confirm_batch_count', 0),
            "confirm_batch_date": getattr(self, 'confirm_batch_date', None),
            "shares": self.shares,
            "avg_cost": self.avg_cost,
            "current_price": self.current_price,
            "entry_avg_cost": self.entry_avg_cost,
            "base_price": self.base_price,
        }

    def from_dict(self, data):
        self.build_phase = data.get("build_phase", 0)
        self.build_first_price = data.get("build_first_price", 0.0)
        self.stop_level = data.get("stop_level", 0)
        self.reached_8pct = data.get("reached_8pct", False)
        self.reached_15pct = data.get("reached_15pct", False)
        self.trailing_stop_price = data.get("trailing_stop_price", 0.0)
        self.below_ma20_count = data.get("below_ma20_count", 0)
        self.below_ma20_date = data.get("below_ma20_date", None)
        self.cooldown_until = data.get("cooldown_until")
        self.grid_frozen = data.get("grid_frozen", False)
        self.peak_price = data.get("peak_price", 0.0)
        self.breakeven_activated = data.get("breakeven_activated", False)
        self.add_count = data.get("add_count", 0)
        self.ma5_touch_count = data.get("ma5_touch_count", 0)
        self.prev_rsi = data.get("prev_rsi")
        self.prev_macd_status = data.get("prev_macd_status")
        self.liquidate_dates = data.get("liquidate_dates", [])
        self.last_grid_trigger = data.get("last_grid_trigger")
        self.last_reset_date = data.get("last_reset_date")
        self.empty_days = data.get("empty_days", 0)
        self.empty_days_date = data.get("empty_days_date")
        self.shares = data.get("shares", self.shares)
        self.avg_cost = data.get("avg_cost", self.avg_cost)
        self.current_price = data.get("current_price", self.current_price)
        self.entry_avg_cost = data.get("entry_avg_cost", 0.0)
        self.trend_profit_trigger_date = data.get("trend_profit_trigger_date")
        self.ma5_sell_trigger_date = data.get("ma5_sell_trigger_date")
        self.confirm_batch_count = data.get("confirm_batch_count", 0)
        self.confirm_batch_date = data.get("confirm_batch_date", None)
        self.base_price = data.get("base_price")
