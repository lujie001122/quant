#!/usr/bin/env python3
"""
T0Strategy — 做T配对策略

从 strategy.py 拆分:
  - evaluate_t0: 做T评分+配对
  - evaluate_t0_execute: 从评分结果解析执行动作
  - 做T评分依赖 rsi_macd.py 中的 t0_buy_score / t0_sell_score
"""

from strategies.base import BaseStrategy
from strategies.rsi_macd import t0_buy_score, t0_sell_score, is_auction_time, TOTAL_FUND
from position_info import MAX_POSITION_RATIO
from money_manager import calc_t0_pair_price
from datetime import datetime


class T0Strategy(BaseStrategy):
    """做T配对策略 — 评估做T信号并生成配对挂单"""

    def evaluate_t0(self, t, pos, price, today_str, atr_pct):
        """评估做T信号

        返回:
          dict with keys: t0_signal, buy_score, sell_score, trade_type, action, position_ratio, reason, t0_pair
        """
        rsi_5min = t.get("rsi_5min")
        macd_5min_status = t.get("macd_5min_status")
        vol_ratio_5min = t.get("vol_ratio_5min")
        ma20_5min_slope = t.get("ma20_5min_slope")

        now = datetime.now()
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=0, second=0, microsecond=0)
        is_market_hours = market_open <= now <= market_close
        in_auction = is_auction_time(now)

        if not is_market_hours or rsi_5min is None or in_auction:
            buy_score = 0
            sell_score = 0
        else:
            buy_score = t0_buy_score(rsi_5min, macd_5min_status, vol_ratio_5min)
            sell_score = t0_sell_score(rsi_5min, macd_5min_status, vol_ratio_5min)

            if ma20_5min_slope is not None:
                if ma20_5min_slope > 0.003:
                    sell_score = 0
                elif ma20_5min_slope < -0.005:
                    buy_score = 0

        t0_signal = "无"
        if not is_market_hours:
            t0_signal = "无(非交易时段)"
        elif in_auction:
            t0_signal = "无(集合竞价时段)"

        result = {
            "t0_signal": t0_signal,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "trade_type": None,
            "action": None,
            "position_ratio": None,
            "reason": None,
            "t0_pair": None,
        }

        if not pos.has_position:
            if buy_score >= 2 or sell_score >= 2:
                result["t0_signal"] = "有做T5m信号但无底仓"
            return result

        if buy_score >= 2:
            if pos.can_t0_today(today_str, atr_pct):
                t0_signal = f"买入做T5m({buy_score}项共振,RSI5m={rsi_5min},MACD5m={macd_5min_status},量比5m={vol_ratio_5min})"
            else:
                t0_signal = f"买入做T5m({buy_score}项信号,今日做T次数已用尽({pos._get_daily_log(today_str).get('t0_count', 0)}/{pos.max_daily_t0(atr_pct)}))"
            result["t0_signal"] = t0_signal
        elif sell_score >= 2:
            if pos.can_t0_today(today_str, atr_pct):
                profit_ok = pos.avg_cost > 0 and price > pos.avg_cost
                if profit_ok:
                    t0_signal = f"卖出做T5m({sell_score}项共振,RSI5m={rsi_5min},MACD5m={macd_5min_status},量比5m={vol_ratio_5min})"
                else:
                    t0_signal = f"卖出做T5m信号但价低于成本({pos.avg_cost:.3f}),不执行"
            else:
                t0_signal = f"卖出做T5m({sell_score}项信号,今日做T次数已用尽({pos._get_daily_log(today_str).get('t0_count', 0)}/{pos.max_daily_t0(atr_pct)}))"
            result["t0_signal"] = t0_signal

        return result

    def evaluate_t0_execute(self, result, t, pos, price, today_str, atr_pct, record_t0=True):
        """从 evaluate_t0 结果中解析出具体的执行动作

        返回: (action, position_ratio, trade_type, reason, t0_pair) 或 None
        """
        buy_score = result["buy_score"]
        sell_score = result["sell_score"]
        t0_signal = result["t0_signal"]

        if ((buy_score >= 2 or sell_score >= 2) and pos.has_position
              and pos.can_t0_today(today_str, atr_pct)):

            if "买入" in t0_signal:
                if record_t0:
                    pos.record_t0(today_str)
                ratio = 0.30
                atr_5m = t.get("atr_5min")
                t0_pair = None
                if atr_5m and price > 0:
                    pair_shares = max(int(pos.shares * 0.30 / 100) * 100, 5000)
                    pair_price = calc_t0_pair_price(price, pair_shares, True)  # 固定+230价差
                    if pair_shares >= 100 and pair_price > 0:
                        spread = abs(pair_price - price) * pair_shares
                        if spread > 200:
                            t0_pair = {
                                "trade_type": "t0_pair",
                                "action": "卖出(配对挂单)",
                                "price": price,
                                "pair_price": pair_price,
                                "shares": pair_shares,
                                "spread": round(spread, 2),
                                "reason": f"做T买入后高位卖出: 固定价差=230元, 挂单价={pair_price:.3f}",
                            }
                return ("买入", f"{ratio*100:.0f}%", "t0", t0_signal + "(量比)", t0_pair)

            elif "卖出" in t0_signal:
                if record_t0:
                    pos.record_t0(today_str)
                ratio = 0.2
                atr_5m = t.get("atr_5min")
                t0_pair = None
                if atr_5m and price > 0:
                    pair_shares = max(int(pos.shares * 0.30 / 100) * 100, 5000)
                    pair_price = calc_t0_pair_price(price, pair_shares, False)  # 固定+230价差
                    if pair_shares >= 100 and pair_price > 0:
                        spread = abs(price - pair_price) * pair_shares
                        if spread > 200:
                            t0_pair = {
                                "trade_type": "t0_pair",
                                "action": "买入(配对挂单)",
                                "price": price,
                                "pair_price": pair_price,
                                "shares": pair_shares,
                                "spread": round(spread, 2),
                                "reason": f"做T卖出后低位接回: 固定价差=230元, 挂单价={pair_price:.3f}",
                            }
                return ("卖出", f"{ratio*100:.0f}%", "t0", t0_signal + "(量比)", t0_pair)

        return None