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
import os
from datetime import datetime, timedelta
import backtrader as bt
import pandas as pd
import yaml

# ── 加载 config.yaml ──
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
with open(_CONFIG_PATH) as f:
    CONF = yaml.safe_load(f)

# Phase 3: 引用 market_data + position_info + strategies 模块
from market_data import fetch_klines_daily, calc_rsi_wilder, calc_macd, calc_ao, calc_atr
from position_info import PositionInfo, DEFENSE_CODE, TREND_PROFIT_MAX_DAILY, TREND_PROFIT_COOLDOWN_DAYS
from strategies.rsi_macd import RSIMACDStrategy, check_defense
from strategies.grid import GridStrategy

_rsi_macd_strategy = RSIMACDStrategy()
_grid_strategy = GridStrategy()


def evaluate_stop(pos, t, price, today_str):
    return _rsi_macd_strategy.evaluate_stop(pos, t, price, today_str)


def resolve_stop_signal(pos, stop_actions):
    return _rsi_macd_strategy.resolve_stop_signal(pos, stop_actions)


def evaluate_entry(pos, t, price, realtime, positions, all_klines, code, today_str, atr_pct, defense_weak):
    return _rsi_macd_strategy.evaluate_entry(pos, t, price, realtime, positions, all_klines, code, today_str, atr_pct, defense_weak)

# ═══════════════════════════════════════════════
#  Config (从 config.yaml 同步)
# ═══════════════════════════════════════════════
TRADE_START = CONF['backtest']['trade_start']
TRADE_END = CONF['backtest']['trade_end']
TOTAL_FUND = CONF['total_fund']
MAX_PER_ETF = CONF['max_per_etf']
FEE = CONF['backtest']['fee']
SLIPPAGE_PCT = CONF['backtest']['slippage_pct']
COOLDOWN_DAYS = CONF['cooldown_days']
DEAD_RATIO = CONF['dead_ratio']
ACTIVE_RATIO = CONF['active_ratio']
POSITION_CAP = CONF['entry']['position_cap']           # 仓位上限
CONFIRM_ENTRY_RATIO = CONF['entry']['confirm_entry_ratio']  # 确认加仓比例
RISK_FREE_RATE = CONF['backtest']['risk_free_rate']

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

def fetch_daily(code, sid, data_start=None, data_end=None):
    """获取前复权日K线数据 → 委托 market_data.fetch_klines_daily"""
    # 默认拉全量（回退用），调用方可传入 data_start/data_end 限制区间
    start = data_start or "20250101"
    end = data_end or "20261231"
    klines = fetch_klines_daily(code, start=start, end=end)
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
        self.ma5 = {}; self.ma20 = {}; self.ma10 = {}
        for d in self.datas:
            n = d._name
            self.rsi[n] = WilderRSI(d.close)
            self.macd[n] = MACDStatus(d.close)
            self.ao[n] = AOIndicator(d)
            self.atr[n] = bt.indicators.ATR(d, period=14)
            self.ma5[n] = bt.indicators.SMA(d.close, period=5)
            self.ma10[n] = bt.indicators.SMA(d.close, period=10)
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
                "active_sell_until": "",  # 活动仓卖出冷却日期(已废弃，对齐实盘)
                # 趋势止盈冷却机制: 单日最多3次，触发后3天冷却
                "trend_sell_today": 0,  # 当日趋势止盈次数
                "trend_sell_cooling_until": "",  # 趋势止盈冷却截止日期
                # 破MA5卖活动仓冷却机制: 单日最多3次，触发后3天冷却
                "ma5_sell_today": 0,  # 当日破MA5卖活动仓次数
                "ma5_sell_cooling_until": "",  # 破MA5卖活动仓冷却截止日期
                "breakeven_activated": False,  # 浮盈≥10%激活保本线
                "entry_avg_cost": 0.0,  # 建仓锁定均价(止损用)
                # 网格策略状态
                "grid_base_price": 0.0,  # 网格基准价
                "grid_frozen": False,  # 网格冻结
                "last_grid_trigger": None,  # 上次网格触发
                "last_reset_date": "",  # 上次网格重置日期
                "grid_entry_avg": 0.0,  # 网格买入均价(独立止损用)
            }

        # 统计
        self.trades = []; self.win_trades = 0; self.loss_trades = 0
        self.win_amount = 0.0; self.loss_amount = 0.0
        self.daily_values = []
        self._order_pending = {}  # data_name -> order
        self._last_date = ""  # 上次交易日，用于每日计数器重置

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
        """按总资金比例买入，与实盘一致：使用 TOTAL_FUND 而非动态 portfolio value"""
        name = data._name
        target_value = min(TOTAL_FUND * pct, TOTAL_FUND * POSITION_CAP)
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
        ps["entry_avg_cost"] = price  # B1: 锁定建仓均价供止损判断

    def _full_liquidate_state(self, ps, date_str):
        """清仓后重置状态"""
        ps["base"] = 0; ps["first_price"] = 0; ps["build_phase"] = 0
        ps["stop_level"] = 0; ps["below_ma20"] = 0; ps["below_ma20_date"] = ""
        ps["reached_8"] = False; ps["reached_15"] = False; ps["trail"] = 0
        ps["add_count"] = 0; ps["peak_price"] = 0; ps["ma5_touch_count"] = 0; ps["stop_cooldown"] = False
        ps["active_sell_count"] = 0; ps["active_sell_until"] = ""
        ps["trend_sell_today"] = 0; ps["trend_sell_cooling_until"] = ""
        ps["ma5_sell_today"] = 0; ps["ma5_sell_cooling_until"] = ""
        ps["breakeven_activated"] = False
        ps["entry_avg_cost"] = 0.0
        ps["grid_base_price"] = 0.0; ps["grid_frozen"] = False
        ps["last_grid_trigger"] = None; ps["last_reset_date"] = ""
        ps["grid_entry_avg"] = 0.0
        ps["confirm_batch_count"] = 0; ps["confirm_batch_date"] = ""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            ps["cooldown_until"] = (dt + timedelta(days=self.p.cooldown_days)).strftime("%Y-%m-%d")
        except: ps["cooldown_until"] = ""

    def next(self):
        date_str = self.data.datetime.date(0).strftime("%Y-%m-%d")

        # 每日计数器重置: 新交易日重置趋势止盈和破MA5卖活动仓的当日计数
        if date_str != self._last_date:
            self._last_date = date_str
            for ps in self.ps.values():
                ps["trend_sell_today"] = 0
                ps["ma5_sell_today"] = 0
                # B3: 跨天重置网格触发档位
                ps["last_grid_trigger"] = None

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
            ma10_v = self.ma10[name][0]
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

            # ═══ EXIT LOGIC ═══
            if has_pos:
                if price > ps["peak_price"]: ps["peak_price"] = price

                # ── 极限方案C: 取消保本止盈 ──
                # (保本止盈已移除，保留注释以标记)

                # 网格买入独立止损-10%: 网格买入均价跌10%止损
                if ps.get("grid_entry_avg", 0) > 0 and price <= ps["grid_entry_avg"] * 0.90:
                    grid_shares = int(shares * 0.20 / 100) * 100  # 卖20%仓位
                    if grid_shares >= 100:
                        if self._sell(d, 20, f"网格止损-10% grid_entry={ps['grid_entry_avg']:.3f}"):
                            ps["grid_entry_avg"] = 0.0  # 止损后重置
                            ps["peak_price"] = price

                # 均价止损分级: 仓位<50%→-25%, 50-80%→-20%, >80%→-15% — B1: 用锁定建仓均价而非动态均价
                entry_cost = ps.get("entry_avg_cost", 0) or avg
                if entry_cost > 0:
                    pos_ratio = shares * price / TOTAL_FUND if price > 0 else 0
                    if pos_ratio > 0.80:
                        stop_pct = 0.15
                    elif pos_ratio >= 0.50:
                        stop_pct = 0.20
                    else:
                        stop_pct = 0.25
                    if price <= entry_cost * (1 - stop_pct):
                        self._close(d, f"均价止损{stop_pct*100:.0f}% 仓位{pos_ratio*100:.0f}% entry_cost={entry_cost:.3f}")
                        self._full_liquidate_state(ps, date_str)
                        continue

                # 硬止盈60%(极限方案C)
                if ps["base"] > 0 and price >= ps["base"] * 1.60:
                    self._close(d, f"硬止盈60% base={ps['base']:.3f}")
                    self._full_liquidate_state(ps, date_str)
                    continue

                # 移动止盈(8%后不改线，15%后再设)
                pp = (price - avg) / avg if avg > 0 else 0
                if pp >= 0.08 - 0.0001 and not ps["reached_8"]:
                    ps["reached_8"] = True
                if pp >= 0.15 - 0.0001 and not ps["reached_15"]:
                    ps["reached_15"] = True; ps["trail"] = avg * 1.05
                if ps["reached_15"] and ps["peak_price"] > 0:
                    ps["trail"] = ps["peak_price"] * 0.95

                if ps["trail"] > 0 and price <= ps["trail"]:
                    label = "移动止盈8%" if not ps["reached_15"] else "移动止盈15%"
                    self._close(d, f"{label} trail={ps['trail']:.3f}")
                    self._full_liquidate_state(ps, date_str)
                    continue

                # 硬止损25%: 价格从峰值回撤25%(极限方案C)
                if ps["peak_price"] > 0 and price < ps["peak_price"] * 0.75:
                    self._close(d, f"硬止损25% peak={ps['peak_price']:.3f}")
                    self._full_liquidate_state(ps, date_str)
                    continue

                # 分级止损
                if ma20_v and price < ma20_v:
                    if ps["below_ma20_date"] != date_str:
                        ps["below_ma20"] += 1; ps["below_ma20_date"] = date_str
                else:
                    ps["below_ma20"] = 0; ps["below_ma20_date"] = ""

                if ps["below_ma20"] >= 2 and ps["stop_level"] == 0:
                    if self._sell(d, 30, f"止损1-30% MA20连续{ps['below_ma20']}日"):
                        ps["stop_level"] = 1; ps["peak_price"] = price

                # 有挂单时不再继续卖出(止损1已提交，止损2等次日)
                if name in self._order_pending:
                    continue

                if ps["stop_level"] == 1 and dif_val < 0:
                    if self._sell(d, 30, "止损2-30% DIF<0"):
                        ps["stop_level"] = 2; ps["peak_price"] = price

                if name in self._order_pending:
                    continue

                if ps["stop_level"] == 2 and ms in ("死叉", "绿柱放大"):
                    self._close(d, f"止损3清仓 MACD{ms}")
                    self._full_liquidate_state(ps, date_str)
                    continue

                if ps["stop_level"] > 0 and ma20_v and price > ma20_v and dif_val > 0 and ms in ("红柱放大", "红柱缩短"):
                    ps["stop_level"] = 0; ps["below_ma20"] = 0

                # 分级止损恢复: Level 2 单独恢复 (MACD未恶化+价格恢复，对齐实盘)
                if ps["stop_level"] == 2 and ms not in ("死叉", "绿柱放大") and ma20_v and price > ma20_v and dif_val > 0:
                    ps["stop_level"] = 0; ps["below_ma20"] = 0

                # 趋势止盈: MACD红柱缩短+破MA5+破MA10 → 卖10%活动仓 (冷却机制: 从config读取)
                # 检查冷却期
                trend_cooling = ps["trend_sell_cooling_until"] and date_str <= ps["trend_sell_cooling_until"]
                if (not trend_cooling and name not in self._order_pending and ms == "红柱缩短" and price < ma5_v
                        and ma10_v and price < ma10_v and ps["trend_sell_today"] < TREND_PROFIT_MAX_DAILY):
                    active_shares = int(shares * ACTIVE_RATIO)
                    sell_shares = int(active_shares * 0.10 / 100) * 100
                    if sell_shares >= 100:
                        if self._sell(d, 10, f"趋势止盈 红柱缩短+破MA5"):
                            ps["trend_sell_today"] += 1
                            # 当日达到上限 → 触发冷却
                            if ps["trend_sell_today"] >= TREND_PROFIT_MAX_DAILY:
                                try:
                                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                                    ps["trend_sell_cooling_until"] = (dt + timedelta(days=TREND_PROFIT_COOLDOWN_DAYS)).strftime("%Y-%m-%d")
                                except:
                                    ps["trend_sell_cooling_until"] = ""

                # 破MA5卖活动仓5%: RSI>50时 (冷却机制: 从config读取)
                # 检查冷却期
                ma5_cooling = ps["ma5_sell_cooling_until"] and date_str <= ps["ma5_sell_cooling_until"]
                if (not ma5_cooling and name not in self._order_pending and price < ma5_v and rsi_val is not None and rsi_val > 50
                        and ps["ma5_sell_today"] < TREND_PROFIT_MAX_DAILY):
                    active_shares = int(shares * ACTIVE_RATIO)
                    sell_shares = int(active_shares * 0.05 / 100) * 100
                    if sell_shares >= 100:
                        if self._sell(d, 5, f"破MA5卖活动仓5% RSI={rsi_val:.1f}"):
                            ps["ma5_sell_today"] += 1
                            # 当日达到3次 → 触发3天冷却
                            if ps["ma5_sell_today"] >= TREND_PROFIT_MAX_DAILY:
                                try:
                                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                                    ps["ma5_sell_cooling_until"] = (dt + timedelta(days=TREND_PROFIT_COOLDOWN_DAYS)).strftime("%Y-%m-%d")
                                except:
                                    ps["ma5_sell_cooling_until"] = ""

                # ═══ 网格策略 (对齐实盘) ═══
                # 网格基准价初始化
                if ps["grid_base_price"] == 0.0 and ps["base"] > 0:
                    ps["grid_base_price"] = ps["base"]
                elif ps["grid_base_price"] == 0.0 and ps["first_price"] > 0:
                    ps["grid_base_price"] = ps["first_price"]

                # 网格解冻: 站上MA5解冻
                if ps["grid_frozen"] and ma5_v and price > ma5_v:
                    ps["grid_frozen"] = False

                if ps["grid_base_price"] > 0:
                    # 计算动态间距 (ATR/price 钳制)
                    atr_pct_grid = atr_val / price if atr_val and price > 0 else 0.025
                    spacing = round(max(0.0125, min(0.05, 0.6 * 0.025 + 0.4 * atr_pct_grid)), 4)

                    # 网格重置检查
                    upper_limit = ps["grid_base_price"] * (1 + spacing * 4)
                    lower_limit = ps["grid_base_price"] * (1 - spacing * 6)
                    if price > upper_limit and ps["last_reset_date"] != date_str:
                        ps["grid_base_price"] = round(price, 3)
                        ps["last_reset_date"] = date_str
                    elif price < lower_limit and ps["last_reset_date"] != date_str:
                        ps["grid_base_price"] = round(price, 3)
                        ps["last_reset_date"] = date_str

                    # 网格信号评估
                    grid_signal, grid_weight = _grid_strategy.evaluate_grid_signals(
                        price, ps["grid_base_price"], spacing, ps["grid_frozen"])

                    # 网格买入
                    if grid_signal != "无" and "买入" in grid_signal and not ps["grid_frozen"] and name not in self._order_pending:
                        grid_key = grid_signal.split("(")[0] if "(" in grid_signal else grid_signal
                        if ps["last_grid_trigger"] != grid_key:
                            ps["last_grid_trigger"] = grid_key
                            pct = grid_weight  # 网格权重 (0.20-0.40)
                            if self._buy(d, pct, f"网格{grid_signal}"):
                                ps["bought_today"] = True
                                # 更新网格买入均价
                                if ps["grid_entry_avg"] == 0.0:
                                    ps["grid_entry_avg"] = price
                                else:
                                    # 加权平均
                                    ps["grid_entry_avg"] = (ps["grid_entry_avg"] * 0.5 + price * 0.5)
                                if "熔断" in grid_signal:
                                    ps["grid_frozen"] = True
                            # B3: 不立即重置，跨天重置

                    # 网格卖出 (趋势止盈触发当天不触发网格卖出)
                    if grid_signal != "无" and "卖出" in grid_signal and name not in self._order_pending:
                        # 趋势止盈互斥: 当天趋势止盈已触发则跳过网格卖出
                        if ps["trend_sell_today"] > 0:
                            continue
                        grid_key = grid_signal.split("(")[0] if "(" in grid_signal else grid_signal
                        if ps["last_grid_trigger"] != grid_key:
                            ps["last_grid_trigger"] = grid_key
                            # 卖活动仓
                            active_shares = int(shares * ACTIVE_RATIO)
                            if active_shares >= 100:
                                sell_shares = int(active_shares * 0.20 / 100) * 100
                                if sell_shares >= 100:
                                    if self._sell(d, 20, f"网格{grid_signal}"):
                                        ps["grid_base_price"] = round(price, 3)  # 上移基准价
                            # B3: 不立即重置，跨天重置

            # ═══ ENTRY LOGIC ═══
            # ATR>50%跳过(除权日, 与实盘一致)
            atr_ok = (atr_val is not None and price > 0 and atr_val / price <= 0.50)

            # 防御盾过滤: 统一调用 check_defense (O3: 替代内联逻辑)
            defense_weak = False
            if DEFENSE_CODE:
                for dd in self.datas:
                    if dd._name == DEFENSE_CODE:
                        dd_price = dd.close[0]
                        all_tech_d = {DEFENSE_CODE: {
                            "ma20": self.ma20[DEFENSE_CODE][0],
                            "dif": self.macd[DEFENSE_CODE].dif[0],
                            "macd_status": MACDStatus.STATUS_MAP.get(self.macd[DEFENSE_CODE].status[0], "震荡"),
                        }}
                        realtime_d = {DEFENSE_CODE: {"price": dd_price}}
                        defense_weak = check_defense(DEFENSE_CODE, all_tech_d, realtime_d)
                        break
            if defense_weak and name != DEFENSE_CODE:
                continue  # 防御标的弱势，全市场暂停建仓（防御标自身除外）

            if not has_pos and ps["build_phase"] == 0 and not ps["bought_today"] and not ps["stop_cooldown"] and not self._is_in_cooldown(ps, date_str) and atr_ok:
                # 多头排列: MA5>MA10>MA20才允许建仓
                if not (ma5_v and ma10_v and ma20_v and ma5_v > ma10_v > ma20_v):
                    continue
                entered = False

                # 通道1: RSI抄底 (RSI≤50+金叉)
                if not entered and rsi_val is not None and rsi_val <= 50 and ms == "金叉":
                    if self._buy(d, 0.30, f"RSI抄底30% RSI={rsi_val:.1f}"):
                        self._init_on_entry(ps, price); entered = True

                # 通道2: 趋势跟踪 (空仓>10天+MACD红柱，去掉MA20过滤对齐实盘)
                if not entered and ps["empty_days"] > 10 and ms in ("红柱放大", "红柱缩短"):
                    if self._buy(d, 0.30, f"趋势跟踪30% 空仓{ps['empty_days']}d"):
                        self._init_on_entry(ps, price); entered = True

                # 通道3: 突破入场
                if not entered and h20 is not None and price > h20 and ms == "金叉":
                    if self._buy(d, 0.30, f"突破入场30% 20日高={h20:.3f}"):
                        self._init_on_entry(ps, price); entered = True

                # 通道4: 分批建仓
                if not entered and ms in ("金叉", "红柱放大") and rsi_val is not None and rsi_val > 40 and price > ma5_v:
                    if self._buy(d, 0.30, f"分批建仓30% MACD{ms} RSI={rsi_val:.1f}"):
                        self._init_on_entry(ps, price); entered = True

                # 通道5: Test抄底
                if not entered and rsi_val is not None and rsi_val < 35 and ms == "绿柱缩短" and price > ma5_v and ps["prev_macd_status"] == "绿柱缩短":
                    if self._buy(d, 0.30, f"Test抄底30% RSI={rsi_val:.1f}"):
                        self._init_on_entry(ps, price); entered = True

                # 通道6: 试探建仓 (RSI>40+站MA5+站MA20+多头排列)
                if not entered and ms in ("绿柱缩短", "震荡") and rsi_val is not None and rsi_val > 40 and price > ma5_v and ma20_v and price > ma20_v:
                    if self._buy(d, 0.30, f"试探建仓30% MACD{ms} RSI={rsi_val:.1f}"):
                        self._init_on_entry(ps, price); entered = True

            # ═══ ADD-ON LOGIC ═══
            if ps["build_phase"] == 1 and ps["first_price"] > 0 and not ps["bought_today"]:
                dip = (price - ps["first_price"]) / ps["first_price"]
                avg = self._get_avg_cost(d)

                # 补仓: RSI<40
                if dip < -0.03 and ms not in ("死叉", "绿柱放大") and ps["add_count"] < 5 and (rsi_val is None or rsi_val < 40):
                    if self._buy(d, 0.15, f"补仓15% 跌{dip*100:.0f}%"):
                        ps["bought_today"] = True; ps["add_count"] += 1

                # 确认加仓分批: 分3次每次30%, 间隔1天
                elif (price > ps["first_price"] or ps["ma5_touch_count"] >= 2) and ps["build_phase"] == 1:
                    ao_ok = True
                    if ao_val is not None and ao_5ago is not None and ao_val <= ao_5ago:
                        ao_ok = False
                    if ao_ok:
                        # 确认加仓分批: 最多3次
                        if "confirm_batch_count" not in ps:
                            ps["confirm_batch_count"] = 0
                        if "confirm_batch_date" not in ps:
                            ps["confirm_batch_date"] = ""
                        if ps.get("confirm_batch_date") == date_str:
                            pass  # 同日已加仓, 等下个交易日
                        elif ps["confirm_batch_count"] >= 3:
                            ps["build_phase"] = 2
                            ps["bought_today"] = True
                            ps["base"] = avg; ps["peak_price"] = price; ps["first_price"] = price
                            ps["reached_8"] = False; ps["reached_15"] = False; ps["trail"] = 0
                            ps["breakeven_activated"] = False
                        else:
                            if self._buy(d, 0.30, f"确认加仓{ps['confirm_batch_count']+1}/3批30%"):
                                ps["confirm_batch_count"] += 1
                                ps["confirm_batch_date"] = date_str
                                ps["add_count"] += 1
                                if ps["confirm_batch_count"] >= 3:
                                    ps["build_phase"] = 2; ps["bought_today"] = True
                                    ps["base"] = avg; ps["peak_price"] = price; ps["first_price"] = price
                                    ps["reached_8"] = False; ps["reached_15"] = False; ps["trail"] = 0
                                    ps["breakeven_activated"] = False
                    else:
                        if price > ma5_v: ps["ma5_touch_count"] += 1
                        else: ps["ma5_touch_count"] = 0

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
            ma10_v = self.ma10[name][0]
            ma20_v = self.ma20[name][0]
            atr_val = self.atr[name].atr[0]

            t = {
                "ma5": ma5_v,
                "ma10": ma10_v,
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

        # P1-6: DecisionEngine 交叉验证 — 已禁用：generate_signals() 联网拉实时行情，
        # 回测每个 bar 都调导致极慢（3个月回测10分钟+），需离线数据源后才能启用
        # self._validate_decision_engine(date_str, strategy_positions)

    def _validate_decision_engine(self, date_str, strategy_positions):
        """P1-6: 回测通过 DecisionEngine 管线做交叉验证。
        调用 DecisionEngine.process_signals() 生成 OrderIntent，
        与回测实际交易方向对比，差异记录日志。"""
        try:
            from decision_engine import DecisionEngine
            from signal_generator import generate_signals

            # 构建简化的 result dict（模拟 signal_generator 输出）
            engine = DecisionEngine()
            result = generate_signals()
            if result is None:
                return

            # 构建 realtime
            realtime = {}
            for d in self.datas:
                realtime[d._name] = {"price": d.close[0]}

            # 传入真实持仓状态
            intents = engine.process(result, strategy_positions, realtime, mode='buy_sell')
            if intents:
                for intent in intents:
                    print(f"[DE-VALIDATE] {date_str} {intent.code} "
                          f"DecisionEngine: {intent.action} {intent.shares}股 "
                          f"@{intent.price:.3f} ({intent.trade_type}) {intent.reason[:60]}")
        except Exception as e:
            pass  # DecisionEngine 不可用时静默跳过

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

    # --period X 支持: 从 config.yaml 读取
    period_map = CONF['backtest']['periods']
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
    # 计算K线拉取起始日: trade_start 往前推 120 个交易日（预热期）
    data_start = (pd.Timestamp(trade_start) - pd.offsets.BDay(120)).strftime("%Y%m%d")
    data_end = pd.Timestamp(trade_end).strftime("%Y%m%d")
    print(f"  K线区间: {data_start} → {data_end} (预热120个交易日)")
    dataframes = {}
    for code, cfg in active_codes.items():
        try:
            df = fetch_daily(code, cfg["sid"], data_start=data_start, data_end=data_end)
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
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', timeframe=bt.TimeFrame.Days, annualize=True, riskfreerate=RISK_FREE_RATE)
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
