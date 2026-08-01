#!/usr/bin/env python3
"""
3-Year Backtest: 5 ETFs — 与 signal_generator.py 完全一致
2023-07-27 → 2026-07-27, 22万共享资金池, 单只ETF最大仓位4.4万

6通道入场(与signal_generator一致):
  1. RSI抄底: RSI≤80 + MACD金叉 → 30%
  2. 趋势跟踪: 空仓>10天 + 价>MA20 + MACD红柱 → 30%
  3. 突破入场: 20日新高(不含当日) + MACD金叉 → 30%
  4. 分批建仓: MACD金叉/红柱放大 + RSI>40 + 站MA5 → 30%
  5. Test抄底: RSI<35 + 绿柱缩短 + 站MA5 + 前日绿柱缩短 → 30%
  6. 试探建仓: 绿柱缩短/震荡 + RSI>30 + 站MA5 → 15%

止损止盈(与signal_generator一致):
  - 均价止损10%: 从买入均价跌10%清仓
  - 硬止盈30%: base_price*1.30
  - 移动止盈: 8%→成本+5%, 15%→峰值回撤5%
  - 硬止损20%: 市值峰值回撤20%
  - 分级止损: 破MA20→30%, DIF<0→30%, 死叉/绿柱放大→清仓
  - 趋势止盈/破MA5卖活动仓: 回测不启用(日K线无法模拟日内回转)
  - 分级止损恢复: 价>MA20+DIF>0+MACD红柱(加严)

确认加仓70%: AO动量过滤
补仓: 跌>3%+MACD非死叉/绿柱放大+add_count<5
"""
import json, urllib.request, math, sys
from datetime import datetime

TRADE_START = "2021-08-01"
TRADE_END   = "2026-07-27"

TOTAL_FUND = 220000       # 共享资金池
MAX_PER_ETF = 44000       # 单只ETF最大仓位

CODES = {
    "159516": {"name": "半导体设备ETF", "sid": "sz159516"},
    "515880": {"name": "通信ETF",       "sid": "sh515880"},
    "588170": {"name": "科创半导体ETF", "sid": "sh588170"},
    "159532": {"name": "ETF",          "sid": "sz159532"},
    "515050": {"name": "中证全指ETF",   "sid": "sh515050"},
}

DEAD_RATIO = 0.6
ACTIVE_RATIO = 0.4
FEE = 5
SLIPPAGE = 0.001
COOLDOWN_DAYS = 1

# ═══════════════════════════════════════════════
#  Indicator functions
# ═══════════════════════════════════════════════
def ma(c, n):
    if len(c) < n: return None
    return sum(c[-n:]) / n

def rsi(c, n=14):
    if len(c) < n + 1: return None
    gains = []; losses = []
    for i in range(1, len(c)):
        d = c[i] - c[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
    if avg_l == 0: return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 2)

def macd(c):
    slow, fast, sig = 26, 12, 9
    if len(c) < slow + sig: return None, None, "震荡"
    e12 = sum(c[:fast]) / fast
    e26 = sum(c[:slow]) / slow
    difs = []
    for i in range(len(c)):
        if i < fast:
            difs.append(e12 - e26)
            continue
        e12 = c[i] * (2/(fast+1)) + e12 * (1 - 2/(fast+1))
        if i < slow:
            difs.append(e12 - e26)
            continue
        e26 = c[i] * (2/(slow+1)) + e26 * (1 - 2/(slow+1))
        difs.append(e12 - e26)
    e9 = difs[slow]
    deas = []
    for d in difs[slow+1:]:
        e9 = d * (2/(sig+1)) + e9 * (1 - 2/(sig+1))
        deas.append(e9)
    d, e = difs[-1], deas[-1]
    dp, ep = difs[-2], deas[-2]
    h  = 2 * (d - e)
    hp = 2 * (dp - ep)
    if   d > e and dp <= ep: s = "金叉"
    elif d < e and dp >= ep: s = "死叉"
    elif d > e and h  > hp:  s = "红柱放大"
    elif d > e and h  < hp:  s = "红柱缩短"
    elif d < e and h  < hp:  s = "绿柱放大"
    elif d < e and h  > hp:  s = "绿柱缩短"
    else:                    s = "震荡"
    return round(d, 4), round(e, 4), s

def ao(highs, lows):
    if len(highs) < 34: return []
    median = [(highs[i] + lows[i]) / 2 for i in range(len(highs))]
    ao_vals = []
    for i in range(33, len(median)):
        sma5  = sum(median[i-4:i+1]) / 5
        sma34 = sum(median[i-33:i+1]) / 34
        ao_vals.append(sma5 - sma34)
    return ao_vals

# ═══════════════════════════════════════════════
#  Fetch daily K-line from Sina API
# ═══════════════════════════════════════════════
def fetch_daily(code, sid):
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sid}&scale=240&ma=no&datalen=1250")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn"
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = []
    for d in data:
        result.append({
            "date":   d["day"],
            "open":   float(d["open"]),
            "close":  float(d["close"]),
            "high":   float(d["high"]),
            "low":    float(d["low"]),
            "volume": float(d["volume"]),
        })
    # 前复权: 检测除权/分红(open/prev_close < 0.95视为调整)
    adj_factor = 1.0
    factors = [1.0] * len(result)
    for i in range(len(result) - 1, -1, -1):
        factors[i] = adj_factor
        if i > 0 and result[i-1]["close"] > 0:
            ratio = result[i]["open"] / result[i-1]["close"]
            if ratio < 0.95:  # 5%阈值检测除权/分红
                adj_factor *= ratio
    for i in range(len(result)):
        result[i]["open"]  = round(result[i]["open"]  * factors[i], 4)
        result[i]["close"] = round(result[i]["close"] * factors[i], 4)
        result[i]["high"]  = round(result[i]["high"]  * factors[i], 4)
        result[i]["low"]   = round(result[i]["low"]   * factors[i], 4)
    return result

# ═══════════════════════════════════════════════
#  Pull data
# ═══════════════════════════════════════════════
print("▸ 拉取历史K线 (Sina API, datalen=1250)...")
all_klines = {}
for code, cfg in CODES.items():
    try:
        all_klines[code] = fetch_daily(code, cfg["sid"])
        print(f"  ✓ {code} {cfg['name']:10s}  {len(all_klines[code]):3d} 条K线")
    except Exception as e:
        print(f"  ✗ {code} {cfg['name']:10s}  拉取失败: {e}")
        sys.exit(1)

klines_all = {}
for code, kl in all_klines.items():
    for k in kl:
        klines_all.setdefault(k["date"], {})[code] = k

dates = sorted(k for k in klines_all if TRADE_START <= k <= TRADE_END)
if len(dates) < 30:
    print(f"✗ 回测区间内仅 {len(dates)} 个交易日，数据不足")
    sys.exit(1)
print(f"\n▸ 回测区间: {dates[0]} → {dates[-1]}  ({len(dates)} 个交易日)")

# ═══════════════════════════════════════════════
#  Position state (共享资金池, 单只最大4.4万)
# ═══════════════════════════════════════════════
class Pos:
    __slots__ = (
        'shares', 'cost', 'avg', 'base', 'peak', 'first_price',
        'build_phase', 'bought_today', 'stop_level', 'below_ma20',
        'below_ma20_date', 'reached_8', 'reached_15', 'trail',
        'add_count', 'empty_days', 'empty_days_date',
        'last_entry_date', 'stop_cooldown', 'cooldown_until',
        'dead_shares', 'active_shares', 'prev_rsi',
        'ma5_touch_count', 'peak_price',
    )
    def __init__(self):
        self.shares         = 0
        self.cost           = 0.0
        self.avg            = 0.0
        self.base           = 0.0
        self.peak           = 0.0
        self.peak_price     = 0.0
        self.first_price    = 0.0
        self.build_phase    = 0
        self.bought_today   = False
        self.stop_level     = 0
        self.below_ma20     = 0
        self.below_ma20_date = ""
        self.reached_8      = False
        self.reached_15     = False
        self.trail          = 0.0
        self.add_count      = 0
        self.empty_days     = 0
        self.empty_days_date = ""
        self.last_entry_date = ""
        self.stop_cooldown  = False
        self.cooldown_until = ""
        self.dead_shares    = 0
        self.active_shares  = 0
        self.prev_rsi       = None
        self.ma5_touch_count = 0

    @property
    def has_position(self): return self.shares > 0

    def market_value(self, price):
        return self.shares * price

    def update_dead_active(self):
        self.dead_shares = int(self.shares * DEAD_RATIO)
        self.active_shares = self.shares - self.dead_shares

    def full_liquidate(self, price, date_str):
        """清仓"""
        self.shares = 0
        self.cost = 0
        self.avg = 0
        self.base = 0
        self.peak = 0
        self.peak_price = 0
        self.build_phase = 0
        self.stop_level = 0
        self.below_ma20 = 0
        self.below_ma20_date = ""
        self.reached_8 = False
        self.reached_15 = False
        self.trail = 0
        self.add_count = 0
        self.dead_shares = 0
        self.active_shares = 0
        self.ma5_touch_count = 0
        self.cooldown_until = date_str  # 1天冷却期

    def is_in_cooldown(self, date_str):
        if not self.cooldown_until: return False
        return date_str <= self.cooldown_until

# 共享资金池
cash = TOTAL_FUND
positions = {c: Pos() for c in CODES}
prev_ms = {c: "震荡" for c in CODES}

# 统计
trades = []
win_trades = 0
loss_trades = 0
win_amount = 0.0
loss_amount = 0.0

def log_trade(d, c, a, p, s, r, pnl=0):
    trades.append({"date": d, "code": c, "action": a, "price": p,
                   "shares": s, "reason": r, "pnl": pnl})

def do_buy(pos, price, shares):
    """买入，返回实际买入股数，扣减共享资金池"""
    global cash
    cost = shares * price * (1 + SLIPPAGE) + FEE
    # 仓位上限: 单只ETF不超过4.4万
    mkt_after = pos.shares * price + cost
    if mkt_after > MAX_PER_ETF:
        shares = int((MAX_PER_ETF - pos.shares * price) / (price * (1 + SLIPPAGE)) / 100) * 100
        if shares < 100: return 0
        cost = shares * price * (1 + SLIPPAGE) + FEE
    # 资金不足时缩减
    if cost > cash:
        shares = int(cash / (price * (1 + SLIPPAGE)) / 100) * 100
        if shares < 100: return 0
        cost = shares * price * (1 + SLIPPAGE) + FEE
    cash -= cost
    pos.cost += cost
    pos.shares += shares
    pos.avg = pos.cost / pos.shares
    pos.peak = max(pos.peak, price)
    pos.peak_price = max(pos.peak_price, price)
    pos.update_dead_active()
    return shares

def do_sell(pos, price, shares):
    """卖出，回收共享资金池"""
    global cash
    if shares > pos.shares: shares = pos.shares
    if shares <= 0: return 0
    proceeds = shares * price * (1 - SLIPPAGE) - FEE
    cash += proceeds
    pos.cost = pos.avg * (pos.shares - shares) if pos.shares > shares else 0
    pos.shares -= shares
    pos.update_dead_active()
    return shares

# ═══════════════════════════════════════════════
#  Main loop
# ═══════════════════════════════════════════════
daily_values = []
entry_dates = {c: "" for c in CODES}

for di, date in enumerate(dates):
    day_data = klines_all[date]

    for p in positions.values():
        p.bought_today = False
        p.stop_cooldown = False

    for code, cfg in CODES.items():
        pos = positions[code]
        kls = all_klines[code]
        idx = next((i for i, k in enumerate(kls) if k["date"] == date), None)
        if idx is None or idx < 50:
            continue

        price   = kls[idx]["close"]
        closes  = [kls[i]["close"] for i in range(max(0, idx - 60), idx + 1)]
        ma5_v   = ma(closes, 5)
        ma20_v  = ma(closes, 20)
        r       = rsi(closes, 14)
        dif, dea, ms = macd(closes)
        if ma5_v is None or r is None or dif is None:
            continue

        # AO
        ao_highs = [kls[i]["high"] for i in range(idx + 1)]
        ao_lows  = [kls[i]["low"]  for i in range(idx + 1)]
        ao_vals  = ao(ao_highs, ao_lows)

        # ── EXIT LOGIC (持仓时) ──
        if pos.has_position:
            # 更新峰值
            if price > pos.peak_price:
                pos.peak_price = price
            if price > pos.peak:
                pos.peak = price

            # 均价止损10%
            if pos.avg > 0 and price <= pos.avg * 0.90:
                pnl = pos.shares * price - pos.cost
                log_trade(date, code, "均价止损10%", price, pos.shares,
                         f"avg={pos.avg:.3f}", pnl)
                if pnl >= 0: win_trades += 1; win_amount += pnl
                else: loss_trades += 1; loss_amount += abs(pnl)
                cash += pos.shares * price * (1 - SLIPPAGE) - FEE
                pos.full_liquidate(price, date)
                pos.stop_cooldown = True
                continue

            # 硬止盈30%
            if pos.base > 0 and price >= pos.base * 1.30:
                pnl = pos.shares * price - pos.cost
                log_trade(date, code, "硬止盈30%", price, pos.shares,
                         f"base={pos.base:.3f}", pnl)
                if pnl >= 0: win_trades += 1; win_amount += pnl
                else: loss_trades += 1; loss_amount += abs(pnl)
                cash += pos.shares * price * (1 - SLIPPAGE) - FEE
                pos.full_liquidate(price, date)
                pos.stop_cooldown = True
                continue

            # 移动止盈
            pp = (price - pos.avg) / pos.avg if pos.avg > 0 else 0
            if pp >= 0.08 - 0.0001 and not pos.reached_8:
                pos.reached_8 = True
                pos.trail = pos.avg * 1.05
            if pp >= 0.15 - 0.0001 and not pos.reached_15:
                pos.reached_15 = True
            if pos.reached_15 and pos.peak_price > 0:
                pos.trail = pos.peak_price * 0.95

            if pos.trail > 0 and price <= pos.trail:
                pnl = pos.shares * price - pos.cost
                label = "移动止盈8%" if not pos.reached_15 else "移动止盈15%"
                log_trade(date, code, label, price, pos.shares,
                         f"trail={pos.trail:.3f}", pnl)
                if pnl >= 0: win_trades += 1; win_amount += pnl
                else: loss_trades += 1; loss_amount += abs(pnl)
                cash += pos.shares * price * (1 - SLIPPAGE) - FEE
                pos.full_liquidate(price, date)
                pos.stop_cooldown = True
                continue

            # 硬止损20% (市值峰值回撤)
            mkt_val = pos.shares * price
            peak_val = pos.peak_price * pos.shares
            if peak_val > 0 and mkt_val < peak_val * 0.80:
                pnl = mkt_val - pos.cost
                log_trade(date, code, "硬止损20%", price, pos.shares,
                         f"mkt={mkt_val:.0f}<peak×0.8={peak_val*0.80:.0f}", pnl)
                if pnl >= 0: win_trades += 1; win_amount += pnl
                else: loss_trades += 1; loss_amount += abs(pnl)
                cash += pos.shares * price * (1 - SLIPPAGE) - FEE
                pos.full_liquidate(price, date)
                pos.stop_cooldown = True
                continue

            # 分级止损
            if ma20_v and price < ma20_v:
                if pos.below_ma20_date != date:
                    pos.below_ma20 += 1
                    pos.below_ma20_date = date
            else:
                pos.below_ma20 = 0
                pos.below_ma20_date = ""

            # Level 1: 破MA20减30%
            if pos.below_ma20 >= 2 and pos.stop_level == 0:
                ss = int(pos.shares * 30 / 100 / 100) * 100
                if ss >= 100:
                    do_sell(pos, price, ss)
                    pos.stop_level = 1
                    pos.stop_cooldown = True
                    pos.peak_price = 0  # 减仓后重置peak
                    log_trade(date, code, "止损1-30%", price, ss, f"MA20连续{pos.below_ma20}日跌破")

            # Level 2: DIF<0减30%
            if pos.stop_level == 1 and dif < 0:
                ss = int(pos.shares * 30 / 100 / 100) * 100
                if ss >= 100:
                    do_sell(pos, price, ss)
                    pos.stop_level = 2
                    pos.stop_cooldown = True
                    pos.peak_price = 0
                    log_trade(date, code, "止损2-30%", price, ss, "DIF<0")

            # Level 3: MACD死叉/绿柱放大清仓
            if pos.stop_level == 2 and ms in ("死叉", "绿柱放大"):
                pnl = pos.shares * price - pos.cost
                log_trade(date, code, "止损3清仓", price, pos.shares, f"MACD{ms}", pnl)
                if pnl >= 0: win_trades += 1; win_amount += pnl
                else: loss_trades += 1; loss_amount += abs(pnl)
                cash += pos.shares * price * (1 - SLIPPAGE) - FEE
                pos.full_liquidate(price, date)
                pos.stop_cooldown = True
                continue

            # 分级止损恢复(加严: 价>MA20+DIF>0+MACD红柱)
            if pos.stop_level > 0 and ma20_v and price > ma20_v and dif > 0 and ms in ("红柱放大", "红柱缩短"):
                pos.stop_level = 0
                pos.below_ma20 = 0

            # 趋势止盈/破MA5卖活动仓: 回测不启用(日K线无法模拟日内回转)
            # 实盘中保留此逻辑(日内可买回)

        # ── ENTRY LOGIC (空仓时) ──
        if not pos.has_position and pos.build_phase == 0 and not pos.bought_today and not pos.stop_cooldown and not pos.is_in_cooldown(date):
            entered = False

            # 通道1: RSI抄底 (RSI≤80 + MACD金叉)
            if not entered and r <= 80 and ms == "金叉":
                ss = math.ceil(MAX_PER_ETF * 0.30 / price / 100) * 100
                if ss >= 100:
                    actual = do_buy(pos, price, ss)
                    if actual >= 100:
                        pos.base = price; pos.first_price = price; pos.build_phase = 1
                        pos.bought_today = True; pos.add_count = 0
                        pos.empty_days = 0; pos.last_entry_date = date
                        entry_dates[code] = date
                        log_trade(date, code, "RSI抄底30%", price, actual, f"RSI={r} 金叉")
                        entered = True

            # 通道2: 趋势跟踪 (空仓>10天+价>MA20+MACD红柱)
            if not entered and pos.empty_days > 10 and price > ma20_v and ms in ("红柱放大", "红柱缩短"):
                ss = math.ceil(MAX_PER_ETF * 0.30 / price / 100) * 100
                if ss >= 100:
                    actual = do_buy(pos, price, ss)
                    if actual >= 100:
                        pos.base = price; pos.first_price = price; pos.build_phase = 1
                        pos.bought_today = True; pos.add_count = 0
                        pos.empty_days = 0; pos.last_entry_date = date
                        entry_dates[code] = date
                        log_trade(date, code, "趋势跟踪30%", price, actual, f"空仓{pos.empty_days}d 价>MA20 {ms}")
                        entered = True

            # 通道3: 突破入场 (20日新高+MACD金叉)
            if not entered and idx >= 20:
                h20 = max(kls[i]["close"] for i in range(idx - 20, idx))
                if price > h20 and ms == "金叉":
                    ss = math.ceil(MAX_PER_ETF * 0.30 / price / 100) * 100
                    if ss >= 100:
                        actual = do_buy(pos, price, ss)
                        if actual >= 100:
                            pos.base = price; pos.first_price = price; pos.build_phase = 1
                            pos.bought_today = True; pos.add_count = 0
                            pos.empty_days = 0; pos.last_entry_date = date
                            entry_dates[code] = date
                            log_trade(date, code, "突破入场30%", price, actual, f"20日高={h20:.3f} 金叉")
                            entered = True

            # 通道4: 分批建仓 (MACD金叉/红柱放大+RSI>40+站MA5)
            if not entered and ms in ("金叉", "红柱放大") and r > 40 and price > ma5_v:
                ss = math.ceil(MAX_PER_ETF * 0.30 / price / 100) * 100
                if ss >= 100:
                    actual = do_buy(pos, price, ss)
                    if actual >= 100:
                        pos.base = price; pos.first_price = price; pos.build_phase = 1
                        pos.bought_today = True; pos.add_count = 0
                        pos.empty_days = 0; pos.last_entry_date = date
                        entry_dates[code] = date
                        log_trade(date, code, "分批建仓30%", price, actual, f"MACD{ms} RSI={r} 站MA5")
                        entered = True

            # 通道5: Test抄底 (RSI<35+绿柱缩短+站MA5+前日绿柱缩短)
            if not entered and r < 35 and ms == "绿柱缩短" and price > ma5_v and prev_ms.get(code) == "绿柱缩短":
                ss = math.ceil(MAX_PER_ETF * 0.30 / price / 100) * 100
                if ss >= 100:
                    actual = do_buy(pos, price, ss)
                    if actual >= 100:
                        pos.base = price; pos.first_price = price; pos.build_phase = 1
                        pos.bought_today = True; pos.add_count = 0
                        pos.empty_days = 0; pos.last_entry_date = date
                        entry_dates[code] = date
                        log_trade(date, code, "Test抄底30%", price, actual, f"RSI={r}<35 绿柱缩短")
                        entered = True

            # 通道6: 试探建仓 (绿柱缩短/震荡+RSI>30+站MA5)
            if not entered and ms in ("绿柱缩短", "震荡") and r > 30 and price > ma5_v:
                ss = math.ceil(MAX_PER_ETF * 0.15 / price / 100) * 100
                if ss >= 100:
                    actual = do_buy(pos, price, ss)
                    if actual >= 100:
                        pos.base = price; pos.first_price = price; pos.build_phase = 1
                        pos.bought_today = True; pos.add_count = 0
                        pos.empty_days = 0; pos.last_entry_date = date
                        entry_dates[code] = date
                        log_trade(date, code, "试探建仓15%", price, actual, f"MACD{ms} RSI={r}")
                        entered = True

        # ── ADD-ON LOGIC (补仓+确认加仓) ──
        if pos.build_phase == 1 and pos.first_price > 0 and not pos.bought_today:
            dip = (price - pos.first_price) / pos.first_price

            # 逆势补仓: 跌>3%+MACD非死叉/绿柱放大+add_count<5
            if dip < -0.03 and ms not in ("死叉", "绿柱放大") and pos.add_count < 5:
                ss = math.ceil(MAX_PER_ETF * 0.15 / price / 100) * 100
                if ss >= 100:
                    actual = do_buy(pos, price, ss)
                    if actual >= 100:
                        pos.bought_today = True
                        pos.add_count += 1
                        log_trade(date, code, f"补仓15%", price, actual, f"跌{dip*100:.0f}% add#{pos.add_count}")

            # 确认加仓70%: 突破首笔价+AO过滤
            elif price > pos.first_price and pos.build_phase == 1:
                ao_ok = True
                if len(ao_vals) >= 6:
                    ao_now  = ao_vals[-1]
                    ao_5ago = ao_vals[-6]
                    if ao_now <= ao_5ago:
                        ao_ok = False
                if ao_ok:
                    ss = math.ceil(MAX_PER_ETF * 0.70 / price / 100) * 100
                    if ss >= 100:
                        actual = do_buy(pos, price, ss)
                        if actual >= 100:
                            pos.build_phase = 2
                            pos.bought_today = True
                            pos.add_count += 1
                            # 确认加仓后重置基准
                            pos.base = pos.avg
                            pos.peak = price
                            pos.peak_price = price
                            pos.first_price = price
                            pos.reached_8 = False
                            pos.reached_15 = False
                            pos.trail = 0
                            log_trade(date, code, "确认加70%", price, actual, f"突破→phase2 avg={pos.avg:.3f}")
                else:
                    # MA5触控计数
                    if price > ma5_v:
                        pos.ma5_touch_count += 1
                    else:
                        pos.ma5_touch_count = 0

    # ── End of day ──
    # Increment empty_days
    for c in CODES:
        pos = positions[c]
        if not pos.has_position and pos.build_phase == 0:
            pos.empty_days += 1

    # Update prev_ms at end of day
    for c in CODES:
        if c in day_data:
            kls = all_klines[c]
            idx = next((i for i, k in enumerate(kls) if k["date"] == date), None)
            if idx is not None and idx >= 50:
                closes = [kls[i]["close"] for i in range(max(0, idx - 60), idx + 1)]
                _, _, ms_val = macd(closes)
                prev_ms[c] = ms_val

    # Portfolio value
    mkt_total = sum(positions[c].shares * day_data[c]["close"] for c in CODES if c in day_data)
    total = cash + mkt_total
    daily_values.append((date, total, cash, mkt_total))

# ═══════════════════════════════════════════════
#  Results
# ═══════════════════════════════════════════════
print("\n" + "=" * 70)
print("                    回 测 结 果")
print("=" * 70)

final_total = daily_values[-1][1]
total_ret   = (final_total - TOTAL_FUND) / TOTAL_FUND * 100

# Max drawdown
peak_v = TOTAL_FUND
max_dd = 0.0
dd_date = ""
for d, t, c, m in daily_values:
    if t > peak_v: peak_v = t
    dd = (peak_v - t) / peak_v * 100
    if dd > max_dd:
        max_dd = dd
        dd_date = d

calmar = total_ret / max_dd if max_dd > 0 else 0.0

# 年化收益
trading_days = len(daily_values)
years = trading_days / 242
annual_ret = ((final_total / TOTAL_FUND) ** (1 / years) - 1) * 100 if years > 0 else 0

# 夏普比率
daily_returns = []
for i in range(1, len(daily_values)):
    prev_val = daily_values[i-1][1]
    curr_val = daily_values[i][1]
    if prev_val > 0:
        daily_returns.append((curr_val - prev_val) / prev_val)
if daily_returns:
    avg_daily_ret = sum(daily_returns) / len(daily_returns)
    std_daily_ret = (sum((r - avg_daily_ret)**2 for r in daily_returns) / len(daily_returns)) ** 0.5
    sharpe = (avg_daily_ret / std_daily_ret) * (242 ** 0.5) if std_daily_ret > 0 else 0
else:
    sharpe = 0

# 胜率/盈亏比
total_trades = win_trades + loss_trades
win_rate = win_trades / total_trades * 100 if total_trades > 0 else 0
avg_win = win_amount / win_trades if win_trades > 0 else 0
avg_loss = loss_amount / loss_trades if loss_trades > 0 else 0
profit_factor = avg_win / avg_loss if avg_loss > 0 else 0

print(f"\n  初始资金:    ¥{TOTAL_FUND:>12,.0f}  (共享资金池, 单只上限¥{MAX_PER_ETF:,})")
print(f"  最终资金:    ¥{final_total:>12,.0f}")
print(f"  总收益:       {total_ret:>+11.2f}%")
print(f"  年化收益:     {annual_ret:>+11.2f}%")
print(f"  最大回撤:     {max_dd:>11.2f}%  ({dd_date})")
print(f"  Calmar 比率:  {calmar:>11.2f}")
print(f"  夏普比率:     {sharpe:>11.2f}")
print(f"  交易笔数:     {len(trades):>11d}")
print(f"  胜率:         {win_rate:>11.1f}%")
print(f"  盈亏比:       {profit_factor:>11.2f}")

# Per-ETF
last_data = day_data
print(f"\n{'─' * 70}")
print(f"  {'ETF':<8} {'名称':<12} {'交易笔数':>6} {'持仓':>10} {'市值':>12} {'状态':>8}")
print(f"  {'─' * 70}")
for c, cfg in CODES.items():
    pos = positions[c]
    mkt = pos.shares * last_data.get(c, {}).get("close", 0)
    status = "空仓" if not pos.has_position else ("建仓中" if pos.build_phase == 1 else "持仓中")
    etf_trades = sum(1 for t in trades if t["code"] == c)
    print(f"  {c:<8} {cfg['name']:<12} {etf_trades:>6} {pos.shares:>10,d} ¥{mkt:>11,.0f} {status:>8}")

# Trade log
print(f"\n{'─' * 70}")
print(f"  交易记录 ({len(trades)} 笔):")
for t in trades:
    print(f"  {t['date']} | {t['code']} {t['action']:<10s} {t['price']:.3f} ×{t['shares']:>6d} | {t['reason']}")

print(f"\n{'=' * 70}")
print(f"  回测完成: {dates[0]} → {dates[-1]}  ({len(dates)} 个交易日)")
print(f"  策略: 6通道入场+分级止损+共享资金池(单只上限4.4万)")
print(f"{'=' * 70}")
