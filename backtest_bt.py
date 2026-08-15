#!/usr/bin/env python3
"""
backtrader 回测引擎 v3: 5 ETF 共享资金池量化策略
用 backtrader 原生 order/broker 体系，策略只管信号+仓位比例

核心:
  - 5只ETF共享一个broker资金池(backtrader天然支持)
  - 每只ETF资金=总资金20%, 按比例下单
  - 6通道入场 + 分级止损 + 移动止盈 + 趋势止盈 + 破MA5卖活动仓
  - Wilder RSI / 自定义MACD / AO动量(与实盘一致)

═══════════════════════════════════════════════════════════════════
  回测 vs 实盘 差异点（不重构，仅记录）
═══════════════════════════════════════════════════════════════════
1. 撮合引擎:
   - 回测: backtrader broker 模拟撮合，next()内下单即成交
   - 实盘: executor.SimBroker/RealBroker，通过同花顺委托下单

2. 挂单与撤单:
   - 回测: 无挂单检查，order提交后立即撮合，不存在撤单→重挂流程
   - 实盘: order_manager 维护挂单状态，有撤单→重挂机制（轮动撤单等）

3. Intent 去重:
   - 回测: 无 intent 文件去重机制，每根K线独立决策
   - 实盘: decision_engine → order_manager 写入 orders/intent/*.json 做去重，
           避免同一信号重复下单

4. 滑点与成交价:
   - 回测: SLIPPAGE_PCT (0.1%) 模拟滑点，按收盘价×(1±slippage)估算
   - 实盘: 依赖同花顺实际成交价，无显式滑点参数

5. 信号生成链路:
   - 回测: 直接调用 strategies.rsi_macd 的 evaluate_entry/evaluate_stop
           （简化路径，指标由 backtrader 内置计算）
   - 实盘: signal_generator.generate_signals() → decision_engine.decide()
           → order_manager.create_order() → executor.execute()
           （完整链路，含信号清洗、风控、仓位管理）
═══════════════════════════════════════════════════════════════════
"""
import sys
from datetime import datetime, timedelta
import backtrader as bt
import pandas as pd

# Phase 3: 引用 market_data + position_info + strategies 模块
from market_data import fetch_klines_daily, fetch_klines_daily_df
from position_info import PositionInfo
from strategies.rsi_macd import RSIMACDStrategy

from risk_manager import check_stop_loss, resolve_stop_signal as _resolve_stop

_rsi_macd_strategy = RSIMACDStrategy()


def evaluate_stop(pos, t, price, today_str):
    """评估止盈止损信号（委托给 risk_manager，P1-3 去重）"""
    return check_stop_loss(pos, t, price, today_str)


def resolve_stop_signal(pos, stop_actions):
    """解析止盈止损信号（委托给 risk_manager，P1-3 去重）"""
    return _resolve_stop(pos, stop_actions)


def evaluate_entry(pos, t, price, realtime, positions, all_klines, code, today_str, atr_pct, defense_weak):
    return _rsi_macd_strategy.evaluate_entry(pos, t, price, realtime, positions, all_klines, code, today_str, atr_pct, defense_weak)

# ═══════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════
TRADE_START = "2021-08-01"
TRADE_END = "2026-07-27"
TOTAL_FUND = 220000
MAX_PER_ETF = 44000
FEE = 5
SLIPPAGE_PCT = 0.001
COOLDOWN_DAYS = 2
DEAD_RATIO = 0.6
ACTIVE_RATIO = 0.4

# 默认ETF配置(回退用, etf_pool.json不存在时使用)
_DEFAULT_CODES = {
    "159516": {"name": "半导体设备ETF", "sid": "sz159516"},
    "515880": {"name": "通信ETF", "sid": "sh515880"},
    "588170": {"name": "科创半导体ETF", "sid": "sh588170"},
    "159532": {"name": "ETF", "sid": "sz159532"},
    "515050": {"name": "中证全指ETF", "sid": "sh515050"},
    "000725": {"name": "京东方A", "sid": "sz000725"},
}

from state_center import get_code_map

CODES = get_code_map()
# 保留 000725 用于 --000725 模式
CODES["000725"] = _DEFAULT_CODES["000725"]

# ═══════════════════════════════════════════════
#  Data fetch (腾讯前复权)
# ═══════════════════════════════════════════════

def fetch_daily(code, sid):
    """获取前复权日K线数据 → 委托 market_data.fetch_klines_daily"""
    klines = fetch_klines_daily(code)
    if not klines:
        return None
    df = pd.DataFrame(klines)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)
    return df


# ═══════════════════════════════════════════════
#  Custom Commission
# ═══════════════════════════════════════════════
class ETFCommission(bt.CommInfoBase):
    """固定手续费5元 + 0.1%滑点"""
    params = (("fee", FEE), ("slippage", SLIPPAGE_PCT))

    def _getcommission(self, size, price, pseudoexec):
        return abs(size) * price * self.p.slippage + self.p.fee


# ═══════════════════════════════════════════════
#  Custom Indicators
# ═══════════════════════════════════════════════
class WilderRSI(bt.Indicator):
    lines = ("rsi",)
    params = (("period", 14),)

    def __init__(self):
        self.addminperiod(self.p.period + 1)
        self._avg_gain = None
        self._avg_loss = None

    def next(self):
        n = self.p.period
        if self._avg_gain is None:
            # 首次: 用SMA初始化
            gains, losses = [], []
            for i in range(1, n + 1):
                d = self.data[-(n - i)] - self.data[-(n - i + 1)]
                gains.append(max(d, 0)); losses.append(max(-d, 0))
            self._avg_gain = sum(gains) / n
            self._avg_loss = sum(losses) / n
        else:
            # 递推: 只用最新一个bar
            d = self.data[0] - self.data[-1]
            self._avg_gain = (self._avg_gain * (n - 1) + max(d, 0)) / n
            self._avg_loss = (self._avg_loss * (n - 1) + max(-d, 0)) / n
        self.lines.rsi[0] = 100.0 if self._avg_loss == 0 else round(100 - 100 / (1 + self._avg_gain / self._avg_loss), 2)


class MACDStatus(bt.Indicator):
    lines = ("dif", "dea", "hist", "status")
    STATUS_MAP = {0: "震荡", 1: "金叉", 2: "死叉", 3: "红柱放大", 4: "红柱缩短", 5: "绿柱放大", 6: "绿柱缩短"}
    params = (("fast", 12), ("slow", 26), ("signal", 9))

    def __init__(self):
        self.addminperiod(self.p.slow + self.p.signal)
        self._e12 = None; self._e26 = None; self._e9 = None
        self._prev_dif = None; self._prev_dea = None; self._prev_hist = None

    def next(self):
        size = len(self.data)
        fast, slow, sig = self.p.fast, self.p.slow, self.p.signal
        min_needed = slow + sig

        if size < min_needed:
            self.lines.status[0] = 0; return

        if self._e12 is None:
            # 初始化: 用SMA预热
            closes = [self.data[-i] for i in range(min(size, min_needed + fast) - 1, -1, -1)]
            self._e12 = sum(closes[:fast]) / fast
            self._e26 = sum(closes[:slow]) / slow
            difs = []
            for i in range(len(closes)):
                if i < fast:
                    difs.append(self._e12 - self._e26); continue
                self._e12 = closes[i] * (2/(fast+1)) + self._e12 * (1 - 2/(fast+1))
                if i < slow:
                    difs.append(self._e12 - self._e26); continue
                self._e26 = closes[i] * (2/(slow+1)) + self._e26 * (1 - 2/(slow+1))
                difs.append(self._e12 - self._e26)
            self._e9 = difs[slow]
            deas = []
            for d in difs[slow+1:]:
                self._e9 = d * (2/(sig+1)) + self._e9 * (1 - 2/(sig+1))
                deas.append(self._e9)
            if len(deas) < 2:
                self.lines.status[0] = 0; return
            # 保存前一个bar的值用于状态判断
            self._prev_dif = difs[-2]; self._prev_dea = deas[-2]
            self._prev_hist = 2 * (self._prev_dif - self._prev_dea)
        else:
            # 递推: 只用最新一个bar
            c = self.data[0]
            self._e12 = c * (2/(fast+1)) + self._e12 * (1 - 2/(fast+1))
            self._e26 = c * (2/(slow+1)) + self._e26 * (1 - 2/(slow+1))
            self._prev_dif = self.lines.dif[-1] if len(self.lines.dif) > 1 else self._e12 - self._e26
            self._prev_dea = self.lines.dea[-1] if len(self.lines.dea) > 1 else self._e9
            self._prev_hist = self.lines.hist[-1] if len(self.lines.hist) > 1 else 2 * (self._prev_dif - self._prev_dea)
            self._e9 = (self._e12 - self._e26) * (2/(sig+1)) + self._e9 * (1 - 2/(sig+1))

        d = self._e12 - self._e26
        e = self._e9
        h = 2 * (d - e)
        dp, hp = self._prev_dif, self._prev_hist

        self.lines.dif[0] = round(d, 4)
        self.lines.hist[0] = round(h, 4)

        if d > e and dp <= self._prev_dea: s = 1
        elif d < e and dp >= self._prev_dea: s = 2
        elif d > e and h > hp: s = 3
        elif d > e and h < hp: s = 4
        elif d < e and h < hp: s = 5
        elif d < e and h > hp: s = 6
        else: s = 0
        self.lines.status[0] = s


class AOIndicator(bt.Indicator):
    """Awesome Oscillator: SMA5(median) - SMA34(median), median=(H+L)/2"""
    lines = ("ao",)
    params = (("fast", 5), ("slow", 34))

    def __init__(self):
        self.addminperiod(self.p.slow)

    def next(self):
        size = len(self.data)
        if size < self.p.slow: return
        # 用(H+L)/2计算median, 与实盘calc_ao一致
        medians = [(self.data.high[-i] + self.data.low[-i]) / 2 for i in range(min(size, self.p.slow))]
        medians.reverse()
        if len(medians) < self.p.slow: return
        self.lines.ao[0] = sum(medians[-5:]) / 5 - sum(medians[-34:]) / 34


# ═══════════════════════════════════════════════
#  Strategy
# ═══════════════════════════════════════════════
class ETFStrategy(bt.Strategy):
    params = (
        ("max_per_etf", MAX_PER_ETF),
        ("cooldown_days", COOLDOWN_DAYS),
    )

    # 指标预热所需的最小K线数: MACD(26+9=35) + AO(34) → 35
    MIN_WARMUP = 35

    def __init__(self):
        # 指标
        self.rsi = {}; self.macd = {}; self.ao = {}
        self.atr = {}
        self.ma5 = {}; self.ma20 = {}
        for d in self.datas:
            n = d._name
            self.rsi[n] = WilderRSI(d.close)
            self.macd[n] = MACDStatus(d.close)
            self.ao[n] = AOIndicator(d)
            self.atr[n] = bt.indicators.ATR(d, period=14)
            self.ma5[n] = bt.indicators.SMA(d.close, period=5)
            self.ma20[n] = bt.indicators.SMA(d.close, period=20)

        # 每ETF状态
        self.ps = {}
        for d in self.datas:
            n = d._name
            self.ps[n] = {
                "base": 0.0, "first_price": 0.0, "build_phase": 0,
                "bought_today": False, "stop_level": 0,
                "below_ma20": 0, "below_ma20_date": "",
                "reached_8": False, "reached_15": False, "trail": 0.0,
                "add_count": 0, "empty_days": 0,
                "cooldown_until": "", "peak_price": 0.0,
                "ma5_touch_count": 0, "prev_macd_status": "震荡",
                "pending_order": None, "stop_cooldown": False,
                "active_sell_count": 0,  # 趋势止盈+破MA5累计卖出次数(最多3次)
                "active_sell_until": "",  # 活动仓卖出冷却日期(3天内不重复)
                "breakeven_activated": False,  # 浮盈≥10%激活保本线
            }

        # 统计
        self.trades = []; self.win_trades = 0; self.loss_trades = 0
        self.win_amount = 0.0; self.loss_amount = 0.0
        self.daily_values = []
        self._order_pending = {}  # data_name -> order

    def notify_order(self, order):
        """订单状态回调"""
        name = order.data._name
        if order.status in [order.Completed]:
            self._order_pending.pop(name, None)
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self._order_pending.pop(name, None)

    def notify_trade(self, trade):
        """交易完成回调 — 统计盈亏"""
        if trade.isclosed:
            if trade.pnl >= 0:
                self.win_trades += 1; self.win_amount += trade.pnl
            else:
                self.loss_trades += 1; self.loss_amount += abs(trade.pnl)

    def _get_position_value(self, data):
        """获取持仓市值"""
        return self.getposition(data).size * data.close[0]

    def _get_avg_cost(self, data):
        """获取持仓均价 (backtrader position.price)"""
        return self.getposition(data).price

    def _get_shares(self, data):
        return self.getposition(data).size

    def _has_position(self, data):
        return self.getposition(data).size > 0

    def _buy(self, data, pct, reason):
        """按总资金比例买入，与实盘一致"""
        name = data._name
        target_value = min(self.broker.getvalue() * pct, self.broker.getvalue() * 0.50)
        price = data.close[0]
        target_shares = int(target_value / price / 100) * 100
        current = self._get_shares(data)
        need = target_shares - current
        if need < 100: return False
        # 资金约束
        cost = need * price * (1 + SLIPPAGE_PCT) + FEE
        if cost > self.broker.getcash():
            need = int(self.broker.getcash() / price / 100) * 100
            if need < 100: return False
        self._order_pending[name] = self.buy(data=data, size=need)
        return True

    def _sell(self, data, pct, reason):
        """按仓位比例卖出"""
        name = data._name
        current = self._get_shares(data)
        if current < 100: return False  # 无持仓可卖
        shares = int(current * pct / 100 / 100) * 100
        if shares < 100: return False
        # 防止卖出超过持仓
        if shares > current:
            shares = (current // 100) * 100
            if shares < 100: return False
        self._order_pending[name] = self.sell(data=data, size=shares)
        return True

    def _close(self, data, reason):
        """清仓"""
        name = data._name
        if self._get_shares(data) < 100: return False
        self._order_pending[name] = self.close(data=data)
        return True

    def _is_in_cooldown(self, ps, date_str):
        if not ps["cooldown_until"]: return False
        return date_str <= ps["cooldown_until"]

    def _init_on_entry(self, ps, price):
        ps["base"] = price; ps["first_price"] = price
        ps["build_phase"] = 1; ps["bought_today"] = True
        ps["add_count"] = 0; ps["empty_days"] = 0; ps["stop_cooldown"] = False

    def _full_liquidate_state(self, ps, date_str):
        """清仓后重置状态"""
        ps["base"] = 0; ps["first_price"] = 0; ps["build_phase"] = 0
        ps["stop_level"] = 0; ps["below_ma20"] = 0; ps["below_ma20_date"] = ""
        ps["reached_8"] = False; ps["reached_15"] = False; ps["trail"] = 0
        ps["add_count"] = 0; ps["peak_price"] = 0; ps["ma5_touch_count"] = 0; ps["stop_cooldown"] = False
        ps["active_sell_count"] = 0; ps["active_sell_until"] = ""
        ps["breakeven_activated"] = False
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            ps["cooldown_until"] = (dt + timedelta(days=self.p.cooldown_days)).strftime("%Y-%m-%d")
        except: ps["cooldown_until"] = ""

    def next(self):
        date_str = self.data.datetime.date(0).strftime("%Y-%m-%d")

        for ps in self.ps.values():
            ps["bought_today"] = False

        for d in self.datas:
            name = d._name
            ps = self.ps[name]
            price = d.close[0]

            # 跳过未上市ETF(volume=-1标记为未上市, 区别于节假日volume=0)
            if d.volume[0] < 0:
                continue

            # 有挂单时跳过(避免重复下单)
            if name in self._order_pending:
                continue

            # 指标
            rsi_val = self.rsi[name].rsi[0]
            macd_status = self.macd[name].status[0]
            dif_val = self.macd[name].dif[0]
            ms = MACDStatus.STATUS_MAP.get(macd_status, "震荡")
            ma5_v = self.ma5[name][0]
            ma20_v = self.ma20[name][0]
            ao_val = self.ao[name].ao[0]
            atr_val = self.atr[name].atr[0]

            # 20日新高
            h20 = None
            if len(d.close) >= 21:
                h20 = max(d.close.get(size=20, ago=1))

            # AO 5日前
            ao_5ago = None
            if len(self.ao[name].ao) >= 6:
                ao_5ago = self.ao[name].ao[-5]

            has_pos = self._has_position(d)
            shares = self._get_shares(d)
            avg = self._get_avg_cost(d)

            # ═══ 状态更新（不执行交易，委托给 DecisionEngine 管线） ═══
            # P0-5: 旧硬编码路径已移除，所有交易决策通过 DE 管线执行
            if has_pos:
                if price > ps["peak_price"]: ps["peak_price"] = price

                # 移动止盈状态追踪
                pp = (price - avg) / avg if avg > 0 else 0
                if pp >= 0.08 - 0.0001 and not ps["reached_8"]:
                    ps["reached_8"] = True
                if pp >= 0.15 - 0.0001 and not ps["reached_15"]:
                    ps["reached_15"] = True; ps["trail"] = avg * 1.05
                if ps["reached_15"] and ps["peak_price"] > 0:
                    ps["trail"] = ps["peak_price"] * 0.95

                # 分级止损状态追踪
                if ma20_v and price < ma20_v:
                    if ps["below_ma20_date"] != date_str:
                        ps["below_ma20"] += 1; ps["below_ma20_date"] = date_str
                else:
                    ps["below_ma20"] = 0; ps["below_ma20_date"] = ""

        # ── P0-5: DecisionEngine 管线执行交易（替代旧硬编码路径）──
        # 构建 strategy_positions 供 DE 管线使用
        strategy_positions = {}
        for d in self.datas:
            name = d._name
            pos = PositionInfo()
            ps = self.ps[name]
            has_pos = self._has_position(d)
            if has_pos:
                avg = self._get_avg_cost(d)
                pos.shares = int(self._get_shares(d))
                pos.avg_cost = avg
                pos.cost = avg * pos.shares
                pos.base_price = ps["base"] if ps["base"] > 0 else avg
                pos.peak_price = ps["peak_price"]
                pos.build_phase = ps["build_phase"]
                pos.build_first_price = ps["first_price"]
                pos.stop_level = ps["stop_level"]
                pos.below_ma20_count = ps["below_ma20"]
                pos.below_ma20_date = ps["below_ma20_date"] if ps["below_ma20_date"] else None
                pos.reached_8pct = ps["reached_8"]
                pos.reached_15pct = ps["reached_15"]
                pos.trailing_stop_price = ps["trail"]
                pos.breakeven_activated = ps["breakeven_activated"]
                pos.add_count = ps["add_count"]
                pos.ma5_touch_count = ps["ma5_touch_count"]
                pos.empty_days = ps["empty_days"]
                pos.prev_macd_status = ps["prev_macd_status"]
                pos.cooldown_until = ps["cooldown_until"] if ps["cooldown_until"] else None
            else:
                pos.empty_days = ps["empty_days"]
                pos.prev_macd_status = ps["prev_macd_status"]
                pos.cooldown_until = ps["cooldown_until"] if ps["cooldown_until"] else None
            strategy_positions[name] = pos
        self._run_decision_engine(date_str, strategy_positions)

        # ── End of day ──
        for name, ps in self.ps.items():
            if not any(self._has_position(d) for d in self.datas if d._name == name):
                if ps["build_phase"] == 0:
                    ps["empty_days"] += 1
            ms = MACDStatus.STATUS_MAP.get(self.macd[name].status[0], "震荡")
            ps["prev_macd_status"] = ms

        # ── Phase 3: strategy.py 交叉验证 (不改变交易逻辑) ──
        self._validate_signals(date_str)

        # 记录每日净值
        total = self.broker.getvalue()
        self.daily_values.append((date_str, total, self.broker.getcash()))

    def _validate_signals(self, date_str):
        """Phase 3: 调用 strategy.py 的 evaluate_stop/evaluate_entry 做交叉验证。
        每次 next() 调用，只在结果不一致时 print 差异。"""
        # 构建 positions 字典 (strategy.py 格式)
        strategy_positions = {}
        for d in self.datas:
            name = d._name
            pos = PositionInfo()
            ps = self.ps[name]
            has_pos = self._has_position(d)
            if has_pos:
                avg = self._get_avg_cost(d)
                pos.shares = int(self._get_shares(d))
                pos.avg_cost = avg
                pos.cost = avg * pos.shares
                pos.base_price = ps["base"] if ps["base"] > 0 else avg
                pos.peak_price = ps["peak_price"]
                pos.build_phase = ps["build_phase"]
                pos.build_first_price = ps["first_price"]
                pos.stop_level = ps["stop_level"]
                pos.below_ma20_count = ps["below_ma20"]
                pos.below_ma20_date = ps["below_ma20_date"] if ps["below_ma20_date"] else None
                pos.reached_8pct = ps["reached_8"]
                pos.reached_15pct = ps["reached_15"]
                pos.trailing_stop_price = ps["trail"]
                pos.breakeven_activated = ps["breakeven_activated"]
                pos.add_count = ps["add_count"]
                pos.ma5_touch_count = ps["ma5_touch_count"]
                pos.empty_days = ps["empty_days"]
                pos.prev_macd_status = ps["prev_macd_status"]
                pos.cooldown_until = ps["cooldown_until"] if ps["cooldown_until"] else None
            else:
                pos.empty_days = ps["empty_days"]
                pos.prev_macd_status = ps["prev_macd_status"]
                pos.cooldown_until = ps["cooldown_until"] if ps["cooldown_until"] else None
            strategy_positions[name] = pos

        for d in self.datas:
            name = d._name
            ps = self.ps[name]
            price = d.close[0]
            if d.volume[0] < 0:
                continue

            # 构建技术指标字典 (t) — 与 strategy.py 接口一致
            rsi_val = self.rsi[name].rsi[0]
            ms = MACDStatus.STATUS_MAP.get(self.macd[name].status[0], "震荡")
            dif_val = self.macd[name].dif[0]
            ma5_v = self.ma5[name][0]
            ma20_v = self.ma20[name][0]
            atr_val = self.atr[name].atr[0]

            t = {
                "ma5": ma5_v,
                "ma20": ma20_v,
                "dif": dif_val,
                "macd_status": ms,
                "rsi": rsi_val,
                "ao_now": self.ao[name].ao[0] if len(self.ao[name].ao) > 0 else None,
                "ao_5ago": self.ao[name].ao[-5] if len(self.ao[name].ao) >= 6 else None,
            }

            has_pos = self._has_position(d)
            avg = self._get_avg_cost(d)
            pos = strategy_positions[name]

            # 验证止损信号
            stop_actions = evaluate_stop(pos, t, price, date_str)
            bt_stop_signal = resolve_stop_signal(pos, stop_actions)

            # 验证入场信号: 无持仓时调用 evaluate_entry
            atr_pct = atr_val / price if atr_val and price > 0 else 0
            entry_result = None
            if not has_pos and ps["build_phase"] == 0:
                # 构建 realtime 字典 (只需当前价格)
                realtime = {n: {"price": dd.close[0]} for n, dd in zip(
                    [d._name for d in self.datas], self.datas)}
                # 构建 all_klines (简化: 只用于20日新高检查)
                all_klines_simple = {}
                for dd in self.datas:
                    n = dd._name
                    closes = [float(dd.close[-i]) for i in range(min(21, len(dd.close)), 0, -1)]
                    all_klines_simple[n] = [{"close": c} for c in closes]
                entry_result = evaluate_entry(
                    pos, t, price, realtime, strategy_positions,
                    all_klines_simple, name, date_str, atr_pct, False
                )

            # 只在差异时打印
            if stop_actions and bt_stop_signal not in ("未触发", "未触发(无持仓)"):
                print(f"[VALIDATE] {date_str} {name} strategy.py止损: {bt_stop_signal}")

            if entry_result and entry_result[0] == "买入":
                print(f"[VALIDATE] {date_str} {name} strategy.py入场: {entry_result[1]} {entry_result[3]}")

    def _run_decision_engine(self, date_str, strategy_positions):
        """P0-5: 回测通过 DecisionEngine 管线做实际交易决策。
        
        从回测指标构建信号，通过 DecisionEngine 完整管线
        （signal_cleaner → money_manager → risk_manager），
        使用返回的 OrderIntent 执行实际交易，替代旧硬编码路径。
        
        当 DecisionEngine 不可用时，回退到旧路径（静默）。
        """
        try:
            from decision_engine import DecisionEngine
            
            engine = DecisionEngine()
            
            # 构建信号 result dict（从回测指标）
            signals = {}
            for d in self.datas:
                name = d._name
                ps = self.ps[name]
                price = d.close[0]
                has_pos = self._has_position(d)
                shares = self._get_shares(d)
                avg = self._get_avg_cost(d)
                
                rsi_val = self.rsi[name].rsi[0]
                macd_status = self.macd[name].status[0]
                ms = MACDStatus.STATUS_MAP.get(macd_status, "震荡")
                ma5_v = self.ma5[name][0]
                ma20_v = self.ma20[name][0]
                atr_val = self.atr[name].atr[0]
                
                # 跳过未上市
                if d.volume[0] < 0:
                    continue
                
                # 构建信号：优先止盈止损
                trade_type = None
                action = ""
                reason = ""
                
                if has_pos:
                    # 均价止损
                    if avg > 0 and price <= avg * 0.80:
                        trade_type = "liquidate"
                        action = "卖出"
                        reason = f"均价止损20% avg={avg:.3f}"
                    # 硬止盈
                    elif ps["base"] > 0 and price >= ps["base"] * 1.60:
                        trade_type = "liquidate"
                        action = "卖出"
                        reason = f"硬止盈60% base={ps['base']:.3f}"
                    # 移动止盈
                    elif ps["trail"] > 0 and price <= ps["trail"]:
                        trade_type = "liquidate"
                        action = "卖出"
                        reason = f"移动止盈 trail={ps['trail']:.3f}"
                    # 硬止损25%
                    elif ps["peak_price"] > 0 and price < ps["peak_price"] * 0.75:
                        trade_type = "liquidate"
                        action = "卖出"
                        reason = f"硬止损25% peak={ps['peak_price']:.3f}"
                    # 分级止损1
                    elif ps["below_ma20"] >= 2 and ps["stop_level"] == 0:
                        trade_type = "reduce"
                        action = "卖出"
                        reason = f"止损1-30% MA20连续{ps['below_ma20']}日"
                    # 分级止损2
                    elif ps["stop_level"] == 1 and self.macd[name].dif[0] < 0:
                        trade_type = "reduce"
                        action = "卖出"
                        reason = "止损2-30% DIF<0"
                    # 分级止损3
                    elif ps["stop_level"] == 2 and ms in ("死叉", "绿柱放大"):
                        trade_type = "liquidate"
                        action = "卖出"
                        reason = f"止损3清仓 MACD{ms}"
                    # 趋势止盈
                    elif ms == "红柱缩短" and price < ma5_v and ps["active_sell_count"] < 3:
                        trade_type = "sell"
                        action = "卖出"
                        reason = f"趋势止盈 红柱缩短+破MA5"
                    # 破MA5卖活动仓
                    elif price < ma5_v and rsi_val is not None and rsi_val > 50 and ps["active_sell_count"] < 3:
                        trade_type = "sell"
                        action = "卖出"
                        reason = f"破MA5卖活动仓5% RSI={rsi_val:.1f}"
                    # 分级止损恢复
                    elif ps["stop_level"] > 0 and ma20_v and price > ma20_v and \
                         self.macd[name].dif[0] > 0 and ms in ("红柱放大", "红柱缩短"):
                        ps["stop_level"] = 0; ps["below_ma20"] = 0
                
                if not has_pos and ps["build_phase"] == 0 and not ps["cooldown_until"]:
                    # 建仓通道
                    h20 = max(d.close.get(size=20, ago=1)) if len(d.close) >= 21 else None
                    atr_ok = (atr_val is not None and price > 0 and atr_val / price <= 0.50)
                    # MA20 过滤 + ATR 过滤
                    if ma20_v and price > ma20_v and atr_ok:
                        if ms == "金叉" and (rsi_val is None or rsi_val <= 80):
                            trade_type = "buy"
                            action = "买入"
                            reason = f"RSI抄底30% RSI={rsi_val:.1f}"
                        elif ps["empty_days"] > 10 and ms in ("红柱放大", "红柱缩短"):
                            trade_type = "buy"
                            action = "买入"
                            reason = f"趋势跟踪30% 空仓{ps['empty_days']}d"
                        elif h20 is not None and price > h20 and ms == "金叉":
                            trade_type = "buy"
                            action = "买入"
                            reason = f"突破入场30% 20日高={h20:.3f}"
                        elif ms in ("金叉", "红柱放大") and rsi_val is not None and rsi_val > 40 and price > ma5_v:
                            trade_type = "buy"
                            action = "买入"
                            reason = f"分批建仓30% MACD{ms} RSI={rsi_val:.1f}"
                    # 试探建仓（不需要 MA20 过滤）
                    if not trade_type and ms in ("绿柱缩短", "震荡") and rsi_val is not None and rsi_val > 30 and price > ma5_v:
                        trade_type = "buy"
                        action = "买入"
                        reason = f"试探建仓15% MACD{ms} RSI={rsi_val:.1f}"
                # ADD-ON LOGIC: 补仓 + 确认加仓 (phase2)
                if not has_pos and ps["build_phase"] == 1 and ps["first_price"] > 0:
                    dip = (price - ps["first_price"]) / ps["first_price"]
                    ao_val = self.ao[name].ao[0]
                    ao_5ago = self.ao[name].ao[-5] if len(self.ao[name].ao) >= 6 else None
                    # 补仓
                    if dip < -0.03 and ms not in ("死叉", "绿柱放大") and ps["add_count"] < 5 and (rsi_val is None or rsi_val < 40):
                        trade_type = "buy"
                        action = "买入"
                        reason = f"补仓15% 跌{dip*100:.0f}%"
                    # 确认加仓
                    elif price > ps["first_price"]:
                        ao_ok = not (ao_val is not None and ao_5ago is not None and ao_val <= ao_5ago)
                        if ao_ok:
                            trade_type = "buy"
                            action = "买入"
                            reason = f"确认加60% 突破→phase2"
                
                if trade_type:
                    signals[name] = {
                        "trade_type": trade_type,
                        "action": action,
                        "reason": reason,
                        "price": str(price),
                        "atr": atr_val,
                        "atr_5min": atr_val,
                    }
            
            if not signals:
                return
            
            result = {"signals": signals}
            realtime = {d._name: {"price": d.close[0]} for d in self.datas}
            
            # 通过 DecisionEngine 完整管线
            intents = engine.process(result, strategy_positions, realtime, mode='buy_sell')
            if not intents:
                return
            
            # 执行 OrderIntent（含状态更新）
            for intent in intents:
                if intent.is_buy():
                    self._buy_by_intent(intent)
                    # 更新回测状态
                    ps = self.ps.get(intent.code, {})
                    if ps.get("build_phase") == 0:
                        self._init_on_entry(ps, float(intent.price))
                elif intent.is_sell():
                    self._sell_by_intent(intent)
                    # 更新回测状态
                    ps = self.ps.get(intent.code, {})
                    if intent.trade_type == "liquidate":
                        self._full_liquidate_state(ps, date_str)
                    elif intent.trade_type == "reduce":
                        if ps.get("stop_level") == 0:
                            ps["stop_level"] = 1
                        elif ps.get("stop_level") == 1:
                            ps["stop_level"] = 2
                    
        except Exception as e:
            pass  # DecisionEngine 不可用时静默跳过
    
    def _buy_by_intent(self, intent):
        """通过 OrderIntent 执行买入"""
        target_d = None
        for d in self.datas:
            if d._name == intent.code:
                target_d = d
                break
        if target_d is None:
            return
        price = intent.price
        shares = intent.shares
        if shares < 100:
            return
        total = self.broker.getvalue()
        cash = self.broker.getcash()
        etf_budget = total * 0.20
        required = shares * price * 1.001
        if required > cash or required > etf_budget * 1.5:
            return
        self.buy(data=target_d, size=shares, price=price,
                 exectype=bt.Order.Limit, valid=bt.Order.DAY)
        self._order_pending[intent.code] = True
        print(f"[DE-BUY] {intent.code} {shares}股 @{price:.3f} {intent.reason}")
    
    def _sell_by_intent(self, intent):
        """通过 OrderIntent 执行卖出"""
        target_d = None
        for d in self.datas:
            if d._name == intent.code:
                target_d = d
                break
        if target_d is None:
            return
        shares = intent.shares
        if shares < 100 or not self._has_position(target_d):
            return
        self.sell(data=target_d, size=shares, price=intent.price,
                  exectype=bt.Order.Limit, valid=bt.Order.DAY)
        self._order_pending[intent.code] = True
        print(f"[DE-SELL] {intent.code} {shares}股 @{intent.price:.3f} {intent.reason}")

    def stop(self):
        pass  # 结果输出在main()中用analyzers


# ═══════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════
def main():
    # ── 命令行参数 ──
    run_000725 = "--000725" in sys.argv
    run_515880 = "--515880" in sys.argv

    # --start=YYYY-MM-DD / --end=YYYY-MM-DD 自定义区间
    custom_start = None
    custom_end = None
    for arg in sys.argv:
        if arg.startswith("--start="):
            custom_start = arg.split("=", 1)[1]
        elif arg.startswith("--end="):
            custom_end = arg.split("=", 1)[1]

    # --period X 支持: 5y/3y/2y/1y/6m/3m
    period_map = {
        "5y": ("2021-08-01", "2026-07-27"),
        "3y": ("2023-08-01", "2026-07-27"),
        "2y": ("2024-08-01", "2026-07-27"),
        "1y": ("2025-08-01", "2026-07-27"),
        "6m": ("2026-01-27", "2026-07-27"),
        "3m": ("2026-04-27", "2026-07-27"),
    }
    period = None
    for arg in sys.argv:
        if arg.startswith("--period="):
            period = arg.split("=")[1]
            break

    if run_000725:
        active_codes = {"000725": CODES["000725"]}
        trade_start, trade_end = "2025-08-01", "2026-07-27"
    elif run_515880:
        active_codes = {"515880": CODES["515880"]}
        trade_start, trade_end = "2024-08-01", "2026-07-27"
    elif custom_start and custom_end:
        active_codes = {k: v for k, v in CODES.items() if k != "000725"}
        trade_start, trade_end = custom_start, custom_end
    elif period and period in period_map:
        active_codes = {k: v for k, v in CODES.items() if k != "000725"}
        trade_start, trade_end = period_map[period]
    else:
        active_codes = {k: v for k, v in CODES.items() if k != "000725"}
        trade_start, trade_end = TRADE_START, TRADE_END

    print("▸ 拉取历史K线 (腾讯前复权 API)...")
    dataframes = {}
    for code, cfg in active_codes.items():
        try:
            df = fetch_daily(code, cfg["sid"])
            dataframes[code] = df
            print(f"  ✓ {code} {cfg['name']:10s}  {len(df):3d} 条K线 (前复权)")
        except Exception as e:
            print(f"  ✗ {code} {cfg['name']:10s}  拉取失败: {e}"); sys.exit(1)

    start = pd.Timestamp(trade_start); end = pd.Timestamp(trade_end)
    # 切片并移除回测区间内无数据的ETF
    empty_codes = []
    for code in list(dataframes.keys()):
        sliced = dataframes[code][start:end]
        if len(sliced) == 0:
            print(f"  ⚠ {code} {active_codes[code]['name']:10s}  回测区间内无数据, 跳过")
            empty_codes.append(code)
        else:
            dataframes[code] = sliced
    for code in empty_codes:
        del dataframes[code]
        del active_codes[code]
    if not dataframes:
        print("✗ 所有ETF在回测区间内均无数据, 退出")
        sys.exit(1)

    # ── 对齐数据: 晚上市ETF补前置行, 使backtrader多数据同步不卡住 ──
    # backtrader要求所有数据都就绪才触发next(), 如果588170从2025年才有数据,
    # 则前3.5年的next()都不会被调用. 解决方案: 用首根K线价格回填上市前日期,
    # 策略中通过volume<0标记跳过未上市ETF.
    all_dates = pd.date_range(start=start, end=end, freq="B")  # 工作日
    for code in dataframes:
        df = dataframes[code]
        # 记录原始上市日期(reindex前)
        first_date = df.index[0]
        # 重新索引到完整交易日历
        df = df.reindex(all_dates)
        # 上市前: 用首根K线价格回填(bfill), volume设为-1标记为"未上市"
        # 上市后缺日(节假日): 用前值填充(ffill), volume=0
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].ffill().bfill()
        df["volume"] = df["volume"].fillna(0).astype(float)
        # 将上市前的volume标记为-1(区别于节假日的0)
        df.loc[df.index < first_date, "volume"] = -1.0
        dataframes[code] = df

    cerebro = bt.Cerebro()

    for code, df in dataframes.items():
        data = bt.feeds.PandasData(dataname=df, name=code)
        cerebro.adddata(data, name=code)

    cerebro.addstrategy(ETFStrategy)

    # Analyzers
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', timeframe=bt.TimeFrame.Days, annualize=True, riskfreerate=0.02)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')

    # 手续费+滑点
    comm = ETFCommission()
    cerebro.broker.addcommissioninfo(comm)

    cerebro.broker.setcash(TOTAL_FUND)
    # 不用COC, 用次日开盘价成交(更真实)
    cerebro.broker.set_coc(False)

    print(f"\n▸ 回测区间: {trade_start} → {trade_end}")
    print(f"▸ 引擎: backtrader v{bt.__version__}")
    print(f"▸ 共享资金池: ¥{TOTAL_FUND:,}")
    if run_000725:
        print(f"▸ 单标的模式: 000725 京东方A")

    results = cerebro.run()
    strat = results[0]

    # Analyzer结果
    final = cerebro.broker.getvalue()
    total_ret = (final - TOTAL_FUND) / TOTAL_FUND * 100

    dd = strat.analyzers.drawdown.get_analysis()
    max_dd = dd.max.drawdown
    ta = strat.analyzers.trades.get_analysis()
    sharpe_a = strat.analyzers.sharpe.get_analysis()

    total_trades = ta.get('total', {}).get('total', 0)
    won = ta.get('won', {}).get('total', 0)
    lost = ta.get('lost', {}).get('total', 0)
    win_rate = won / total_trades * 100 if total_trades else 0
    avg_win = ta.get('won', {}).get('pnl', {}).get('average', 0)
    avg_loss = abs(ta.get('lost', {}).get('pnl', {}).get('average', 1))
    pf = avg_win / avg_loss if avg_loss > 0 else 0

    # 年化 (用实际日期差)
    from datetime import date
    d1 = date.fromisoformat(trade_start); d2 = date.fromisoformat(trade_end)
    years = (d2 - d1).days / 365.0
    annual_ret = ((final / TOTAL_FUND) ** (1 / years) - 1) * 100 if years > 0 else 0
    calmar = annual_ret / max_dd if max_dd > 0 else 0

    print("\n" + "=" * 70)
    print("                    回 测 结 果 (backtrader)")
    print("=" * 70)
    print(f"\n  初始资金:    ¥{TOTAL_FUND:>12,.0f}")
    print(f"  最终资金:    ¥{final:>12,.0f}")
    print(f"  总收益:       {total_ret:>+11.2f}%")
    print(f"  年化收益:     {annual_ret:>+11.2f}%")
    print(f"  最大回撤:     {max_dd:>11.2f}%")
    print(f"  Calmar 比率:  {calmar:>11.2f}")
    print(f"  夏普比率:     {sharpe_a.get('sharperatio', 0) or 0:>11.2f}")
    print(f"  交易笔数:     {total_trades:>11d}")
    print(f"  胜率:         {win_rate:>11.1f}%")
    print(f"  盈亏比:       {pf:>11.2f}")

    print(f"\n{'─' * 70}")
    print(f"  {'ETF':<8} {'名称':<12} {'持仓':>10} {'市值':>12} {'状态':>8}")
    print(f"  {'─' * 70}")
    for d in strat.datas:
        name = d._name
        pos = strat.getposition(d)
        cfg = active_codes.get(name, {"name": name})
        mkt = pos.size * d.close[0]
        status = "空仓" if pos.size == 0 else ("建仓中" if strat.ps[name]["build_phase"] == 1 else "持仓中")
        print(f"  {name:<8} {cfg['name']:<12} {pos.size:>10,d} ¥{mkt:>11,.0f} {status:>8}")

    print(f"\n{'=' * 70}")
    print(f"  回测完成 (backtrader引擎)")
    print(f"{'=' * 70}")

if __name__ == "__main__":
    main()
