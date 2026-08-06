#!/usr/bin/env python3
"""
backtrader 回测引擎 v3: 5 ETF 共享资金池量化策略
用 backtrader 原生 order/broker 体系，策略只管信号+仓位比例

核心:
  - 5只ETF共享一个broker资金池(backtrader天然支持)
  - 每只ETF资金=总资金20%, 按比例下单
  - 6通道入场 + 分级止损 + 移动止盈 + 趋势止盈 + 破MA5卖活动仓
  - Wilder RSI / 自定义MACD / AO动量(与实盘一致)
"""
import json, urllib.request, math, os, sys
from datetime import datetime, timedelta
import backtrader as bt
import pandas as pd

# ═══════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════
TRADE_START = "2021-08-01"
TRADE_END = "2026-07-27"
TOTAL_FUND = 220000
MAX_PER_ETF = 44000
FEE = 5
SLIPPAGE_PCT = 0.001
COOLDOWN_DAYS = 3
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

from tracker import get_code_map

CODES = get_code_map()
# 保留 000725 用于 --000725 模式
CODES["000725"] = _DEFAULT_CODES["000725"]

# ═══════════════════════════════════════════════
#  Data fetch (腾讯前复权)
# ═══════════════════════════════════════════════

def fetch_daily(code, sid):
    """获取前复权日K线数据 (腾讯接口, 自带前复权)
    code: 6位代码如 "515880"
    sid:  带市场前缀如 "sh515880", "sz159516"
    """
    # 腾讯接口: qfq=前复权, datalen=1250条
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={sid},day,,,1250,qfq")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    # 解析: data[sid]["qfqday"] 数组, 每项 ["日期","开","收","高","低","成交量"]
    stock_data = raw.get("data", {})
    # sid可能是小写或大写, 尝试匹配
    qfq_key = None
    for key in (sid, sid.lower(), sid.upper()):
        if key in stock_data:
            qfq_key = key
            break
    if not qfq_key:
        raise RuntimeError(f"{code} 腾讯接口返回无数据: {list(stock_data.keys())}")

    klines = stock_data[qfq_key].get("qfqday") or stock_data[qfq_key].get("day", [])
    if not klines:
        raise RuntimeError(f"{code} 腾讯接口K线为空")

    rows = []
    for item in klines:
        if len(item) < 6:
            continue
        try:
            rows.append({
                "date": item[0],
                "open": float(item[1]),
                "close": float(item[2]),
                "high": float(item[3]),
                "low": float(item[4]),
                "volume": float(item[5]),
            })
        except (ValueError, IndexError):
            continue

    if len(rows) <= 50:
        raise RuntimeError(f"{code} 腾讯接口K线不足({len(rows)}条)")

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    return df[["open", "high", "low", "close", "volume"]]


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

            # ═══ EXIT LOGIC ═══
            if has_pos:
                if price > ps["peak_price"]: ps["peak_price"] = price

                # 保本止盈: 浮盈≥10%后激活, 价格回落到成本+2%时清仓
                pp = (price - avg) / avg if avg > 0 else 0
                if pp >= 0.10 - 0.0001:
                    ps["breakeven_activated"] = True
                if ps["breakeven_activated"] and avg > 0 and price <= avg * 1.02:
                    self._close(d, f"保本止盈 avg={avg:.3f} price={price:.3f}")
                    self._full_liquidate_state(ps, date_str)
                    continue

                # 均价止损12%
                if avg > 0 and price <= avg * 0.88:
                    self._close(d, f"均价止损12% avg={avg:.3f}")
                    self._full_liquidate_state(ps, date_str)
                    continue

                # 硬止盈30%
                if ps["base"] > 0 and price >= ps["base"] * 1.30:
                    self._close(d, f"硬止盈30% base={ps['base']:.3f}")
                    self._full_liquidate_state(ps, date_str)
                    continue

                # 移动止盈(8%后不改线，15%后再设)
                # pp 已在保本止盈中计算
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

                # 硬止损20%: 价格从峰值回撤20%
                if ps["peak_price"] > 0 and price < ps["peak_price"] * 0.80:
                    self._close(d, f"硬止损20% peak={ps['peak_price']:.3f}")
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

                # 趋势止盈: MACD红柱缩短+破MA5 → 卖10%活动仓(累计最多3次,3天冷却)
                if (name not in self._order_pending and ms == "红柱缩短" and price < ma5_v
                        and ps["active_sell_count"] < 3
                        and date_str >= ps["active_sell_until"]):
                    active_shares = int(shares * ACTIVE_RATIO)
                    sell_shares = int(active_shares * 0.10 / 100) * 100
                    if sell_shares >= 100:
                        if self._sell(d, 10, f"趋势止盈 红柱缩短+破MA5"):
                            ps["active_sell_count"] += 1
                            ps["active_sell_until"] = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=3)).strftime("%Y-%m-%d")

                # 破MA5卖活动仓5%: RSI>50时(累计最多3次,3天冷却)
                if (name not in self._order_pending and price < ma5_v and rsi_val is not None and rsi_val > 50
                        and ps["active_sell_count"] < 3
                        and date_str >= ps["active_sell_until"]):
                    active_shares = int(shares * ACTIVE_RATIO)
                    sell_shares = int(active_shares * 0.05 / 100) * 100
                    if sell_shares >= 100:
                        if self._sell(d, 5, f"破MA5卖活动仓5% RSI={rsi_val:.1f}"):
                            ps["active_sell_count"] += 1
                            ps["active_sell_until"] = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=3)).strftime("%Y-%m-%d")

            # ═══ ENTRY LOGIC ═══
            # P1: 入场MA20趋势过滤 — 新开仓时要求price > MA20
            ma20_ok = (ma20_v and price > ma20_v)
            # ATR>50%跳过(除权日, 与实盘一致)
            atr_ok = (atr_val is not None and price > 0 and atr_val / price <= 0.50)
            if not has_pos and ps["build_phase"] == 0 and not ps["bought_today"] and not ps["stop_cooldown"] and not self._is_in_cooldown(ps, date_str) and ma20_ok and atr_ok:
                entered = False

                # 通道1: RSI抄底 (RSI为None时允许,与实盘一致)
                if not entered and (rsi_val is None or rsi_val <= 80) and ms == "金叉":
                    if self._buy(d, 0.30, f"RSI抄底30% RSI={rsi_val:.1f}"):
                        self._init_on_entry(ps, price); entered = True

                # 通道2: 趋势跟踪
                if not entered and ps["empty_days"] > 10 and price > ma20_v and ms in ("红柱放大", "红柱缩短"):
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

                # 通道6: 试探建仓
                if not entered and ms in ("绿柱缩短", "震荡") and rsi_val is not None and rsi_val > 40 and price > ma5_v:
                    if self._buy(d, 0.15, f"试探建仓15% MACD{ms} RSI={rsi_val:.1f}"):
                        self._init_on_entry(ps, price); entered = True

            # ═══ ADD-ON LOGIC ═══
            if ps["build_phase"] == 1 and ps["first_price"] > 0 and not ps["bought_today"]:
                dip = (price - ps["first_price"]) / ps["first_price"]

                # 补仓: RSI<40
                if dip < -0.03 and ms not in ("死叉", "绿柱放大") and ps["add_count"] < 5 and (rsi_val is None or rsi_val < 40):
                    if self._buy(d, 0.15, f"补仓15% 跌{dip*100:.0f}%"):
                        ps["bought_today"] = True; ps["add_count"] += 1

                # 确认加仓70%
                elif price > ps["first_price"] and ps["build_phase"] == 1:
                    ao_ok = True
                    if ao_val is not None and ao_5ago is not None and ao_val <= ao_5ago:
                        ao_ok = False
                    if ao_ok:
                        if self._buy(d, 0.70, f"确认加70% 突破→phase2"):
                            ps["build_phase"] = 2; ps["bought_today"] = True; ps["add_count"] += 1
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

        # 记录每日净值
        total = self.broker.getvalue()
        self.daily_values.append((date_str, total, self.broker.getcash()))

    def stop(self):
        pass  # 结果输出在main()中用analyzers


# ═══════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════
def main():
    # ── 命令行参数 ──
    run_000725 = "--000725" in sys.argv
    run_515880 = "--515880" in sys.argv

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
    elif period and period in period_map:
        active_codes = {k: v for k, v in CODES.items() if k != "000725"}
        trade_start, trade_end = period_map[period]
    elif run_515880:
        active_codes = {"515880": CODES["515880"]}
        trade_start, trade_end = "2024-08-01", "2026-07-27"
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
    for code in dataframes:
        dataframes[code] = dataframes[code][start:end]

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
