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
import json, urllib.request, math, sys
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
COOLDOWN_DAYS = 2
DEAD_RATIO = 0.6
ACTIVE_RATIO = 0.4

CODES = {
    "159516": {"name": "半导体设备ETF", "sid": "sz159516"},
    "515880": {"name": "通信ETF", "sid": "sh515880"},
    "588170": {"name": "科创半导体ETF", "sid": "sh588170"},
    "159532": {"name": "ETF", "sid": "sz159532"},
    "515050": {"name": "中证全指ETF", "sid": "sh515050"},
}

# ═══════════════════════════════════════════════
#  Data fetch
# ═══════════════════════════════════════════════
def fetch_daily(code, sid):
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sid}&scale=240&ma=no&datalen=1250")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    rows = []
    for d in data:
        try:
            rows.append({
                "date": d["day"], "open": float(d["open"]), "close": float(d["close"]),
                "high": float(d["high"]), "low": float(d["low"]), "volume": float(d["volume"]),
            })
        except (ValueError, KeyError):
            continue
    # 前复权
    adj = 1.0; factors = [1.0] * len(rows)
    for i in range(len(rows) - 1, -1, -1):
        factors[i] = adj
        if i > 0 and rows[i-1]["close"] > 0:
            ratio = rows[i]["open"] / rows[i-1]["close"]
            if ratio < 0.95: adj *= ratio
    for i in range(len(rows)):
        for k in ("open", "close", "high", "low"):
            rows[i][k] = round(rows[i][k] * factors[i], 4)
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

    def next(self):
        n = self.p.period
        closes = [self.data[-i] for i in range(n, -1, -1)]
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, 0)); losses.append(max(-d, 0))
        avg_g = sum(gains[:n]) / n; avg_l = sum(losses[:n]) / n
        for i in range(n, len(gains)):
            avg_g = (avg_g * (n - 1) + gains[i]) / n
            avg_l = (avg_l * (n - 1) + losses[i]) / n
        self.lines.rsi[0] = 100.0 if avg_l == 0 else round(100 - 100 / (1 + avg_g / avg_l), 2)


class MACDStatus(bt.Indicator):
    lines = ("dif", "dea", "hist", "status")
    STATUS_MAP = {0: "震荡", 1: "金叉", 2: "死叉", 3: "红柱放大", 4: "红柱缩短", 5: "绿柱放大", 6: "绿柱缩短"}
    params = (("fast", 12), ("slow", 26), ("signal", 9))

    def __init__(self):
        self.addminperiod(self.p.slow + self.p.signal)

    def next(self):
        size = len(self.data)
        min_needed = self.p.slow + self.p.signal
        if size < min_needed:
            self.lines.status[0] = 0; return
        # 只取最近min_needed条数据
        closes = [self.data[-i] for i in range(min(size, min_needed + self.p.fast) - 1, -1, -1)]
        fast, slow, sig = self.p.fast, self.p.slow, self.p.signal
        e12 = sum(closes[:fast]) / fast; e26 = sum(closes[:slow]) / slow
        difs = []
        for i in range(len(closes)):
            if i < fast: difs.append(e12 - e26); continue
            e12 = closes[i] * (2/(fast+1)) + e12 * (1 - 2/(fast+1))
            if i < slow: difs.append(e12 - e26); continue
            e26 = closes[i] * (2/(slow+1)) + e26 * (1 - 2/(slow+1))
            difs.append(e12 - e26)
        e9 = difs[slow]; deas = []
        for d in difs[slow+1:]:
            e9 = d * (2/(sig+1)) + e9 * (1 - 2/(sig+1))
            deas.append(e9)
        if len(deas) < 2:
            self.lines.status[0] = 0; return
        d, e = difs[-1], deas[-1]; dp, ep = difs[-2], deas[-2]
        h, hp = 2*(d-e), 2*(dp-ep)
        self.lines.dif[0] = round(d, 4); self.lines.hist[0] = round(h, 4)
        if d > e and dp <= ep: s = 1
        elif d < e and dp >= ep: s = 2
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

    def __init__(self):
        # 指标
        self.rsi = {}; self.macd = {}; self.ao = {}
        self.ma5 = {}; self.ma20 = {}
        for d in self.datas:
            n = d._name
            self.rsi[n] = WilderRSI(d.close)
            self.macd[n] = MACDStatus(d.close)
            self.ao[n] = AOIndicator(d)
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
                "add_count": 0, "empty_days": 0, "stop_cooldown": False,
                "cooldown_until": "", "peak_price": 0.0,
                "ma5_touch_count": 0, "prev_macd_status": "震荡",
                "pending_order": None,  # 跟踪挂单
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
        target_value = self.broker.getvalue() * pct
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
        shares = int(current * pct / 100 / 100) * 100
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
        ps["add_count"] = 0; ps["empty_days"] = 0

    def _full_liquidate_state(self, ps, date_str):
        """清仓后重置状态"""
        ps["base"] = 0; ps["first_price"] = 0; ps["build_phase"] = 0
        ps["stop_level"] = 0; ps["below_ma20"] = 0; ps["below_ma20_date"] = ""
        ps["reached_8"] = False; ps["reached_15"] = False; ps["trail"] = 0
        ps["add_count"] = 0; ps["peak_price"] = 0; ps["ma5_touch_count"] = 0
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            ps["cooldown_until"] = (dt + timedelta(days=self.p.cooldown_days)).strftime("%Y-%m-%d")
        except: ps["cooldown_until"] = ""
        ps["stop_cooldown"] = True

    def next(self):
        date_str = self.data.datetime.date(0).strftime("%Y-%m-%d")

        for ps in self.ps.values():
            ps["bought_today"] = False; ps["stop_cooldown"] = False

        for d in self.datas:
            name = d._name
            ps = self.ps[name]
            price = d.close[0]

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

                # 均价止损10%
                if avg > 0 and price <= avg * 0.90:
                    self._close(d, f"均价止损10% avg={avg:.3f}")
                    self._full_liquidate_state(ps, date_str)
                    continue

                # 硬止盈30%
                if ps["base"] > 0 and price >= ps["base"] * 1.30:
                    self._close(d, f"硬止盈30% base={ps['base']:.3f}")
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

                # 硬止损20%
                mkt_val = shares * price; peak_val = ps["peak_price"] * shares
                if peak_val > 0 and mkt_val < peak_val * 0.80:
                    self._close(d, f"硬止损20% mkt={mkt_val:.0f}")
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
                        ps["stop_level"] = 1; ps["stop_cooldown"] = True; ps["peak_price"] = price

                if ps["stop_level"] == 1 and dif_val < 0:
                    if self._sell(d, 30, "止损2-30% DIF<0"):
                        ps["stop_level"] = 2; ps["stop_cooldown"] = True; ps["peak_price"] = price

                if ps["stop_level"] == 2 and ms in ("死叉", "绿柱放大"):
                    self._close(d, f"止损3清仓 MACD{ms}")
                    self._full_liquidate_state(ps, date_str)
                    continue

                if ps["stop_level"] > 0 and ma20_v and price > ma20_v and dif_val > 0 and ms in ("红柱放大", "红柱缩短"):
                    ps["stop_level"] = 0; ps["below_ma20"] = 0

                # 趋势止盈: MACD红柱缩短+破MA5 → 卖20%活动仓
                if ms == "红柱缩短" and price < ma5_v:
                    active_shares = int(shares * ACTIVE_RATIO)
                    sell_shares = int(active_shares * 0.20 / 100) * 100
                    if sell_shares >= 100:
                        self._sell(d, 20, f"趋势止盈 红柱缩短+破MA5")

                # 破MA5卖活动仓10%: RSI>50时
                if price < ma5_v and rsi_val is not None and rsi_val > 50:
                    active_shares = int(shares * ACTIVE_RATIO)
                    if active_shares > 0:
                        self._sell(d, 10, f"破MA5卖活动仓10% RSI={rsi_val:.1f}")

            # ═══ ENTRY LOGIC ═══
            if not has_pos and ps["build_phase"] == 0 and not ps["bought_today"] and not ps["stop_cooldown"] and not self._is_in_cooldown(ps, date_str):
                entered = False

                # 通道1: RSI抄底
                if not entered and rsi_val is not None and rsi_val <= 80 and ms == "金叉":
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
    print("▸ 拉取历史K线 (Sina API, datalen=1250)...")
    dataframes = {}
    for code, cfg in CODES.items():
        try:
            df = fetch_daily(code, cfg["sid"])
            dataframes[code] = df
            print(f"  ✓ {code} {cfg['name']:10s}  {len(df):3d} 条K线")
        except Exception as e:
            print(f"  ✗ {code} {cfg['name']:10s}  拉取失败: {e}"); sys.exit(1)

    start = pd.Timestamp(TRADE_START); end = pd.Timestamp(TRADE_END)
    for code in dataframes:
        dataframes[code] = dataframes[code][start:end]

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

    print(f"\n▸ 回测区间: {TRADE_START} → {TRADE_END}")
    print(f"▸ 引擎: backtrader v{bt.__version__}")
    print(f"▸ 共享资金池: ¥{TOTAL_FUND:,}")

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
    d1 = date.fromisoformat(TRADE_START); d2 = date.fromisoformat(TRADE_END)
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
        cfg = CODES.get(name, {"name": name})
        mkt = pos.size * d.close[0]
        status = "空仓" if pos.size == 0 else ("建仓中" if strat.ps[name]["build_phase"] == 1 else "持仓中")
        print(f"  {name:<8} {cfg['name']:<12} {pos.size:>10,d} ¥{mkt:>11,.0f} {status:>8}")

    print(f"\n{'=' * 70}")
    print(f"  回测完成 (backtrader引擎)")
    print(f"{'=' * 70}")

if __name__ == "__main__":
    main()
