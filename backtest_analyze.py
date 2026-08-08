#!/usr/bin/env python3
"""
Detailed Backtest Trade Analyzer — captures per-ETF trade details across years.
Runs backtest_bt.py with extended logging to extract channel usage, win/loss per ETF,
and trade timing patterns.
"""
import sys
sys.path.insert(0, '/Users/lujie/Documents/code/quant')

import json, urllib.request, os
from datetime import datetime, timedelta
import backtrader as bt
import pandas as pd

# Import the backtest components
from backtest_bt import (
    fetch_daily, ETFCommission, WilderRSI, MACDStatus, AOIndicator,
    ETFStrategy, TOTAL_FUND, MAX_PER_ETF, FEE, SLIPPAGE_PCT, COOLDOWN_DAYS,
    DEAD_RATIO, ACTIVE_RATIO, CODES
)

# ─── Extended Strategy with detailed logging ───
class DetailedETFStrategy(ETFStrategy):
    """Extended strategy that logs every trade decision with full context"""

    def __init__(self):
        super().__init__()
        # Per-ETF trade log
        self.trade_log = []  # list of dicts: {date, code, action, reason, price, pnl, channel, ...}
        self.entry_log = []  # entry details
        self.exit_log = []   # exit details
        self.channel_counts = {}  # channel -> count
        for d in self.datas:
            n = d._name
            self.channel_counts[n] = {
                "RSI抄底": 0, "趋势跟踪": 0, "突破入场": 0,
                "分批建仓": 0, "Test抄底": 0, "试探建仓": 0,
                "补仓": 0, "确认加仓": 0,
                "均价止损": 0, "硬止盈": 0, "移动止盈": 0,
                "分级止损": 0, "硬止损": 0, "保本止盈": 0,
                "趋势止盈": 0, "破MA5卖": 0,
                "total_entries": 0, "total_exits": 0,
                "win_count": 0, "loss_count": 0,
                "win_amount": 0.0, "loss_amount": 0.0,
                "total_pnl": 0.0,
            }

    def _buy(self, data, pct, reason):
        name = data._name
        price = data.close[0]
        channel = "unknown"
        if "RSI抄底" in reason: channel = "RSI抄底"
        elif "趋势跟踪" in reason: channel = "趋势跟踪"
        elif "突破入场" in reason: channel = "突破入场"
        elif "分批建仓" in reason or "分批1" in reason: channel = "分批建仓"
        elif "Test抄底" in reason: channel = "Test抄底"
        elif "试探建仓" in reason or "试探" in reason: channel = "试探建仓"
        elif "补仓" in reason: channel = "补仓"
        elif "确认加" in reason: channel = "确认加仓"

        self.entry_log.append({
            "date": self.data.datetime.date(0).strftime("%Y-%m-%d"),
            "code": name,
            "action": "BUY",
            "channel": channel,
            "pct": pct,
            "price": round(price, 3),
            "reason": reason,
            "rsi": self.rsi[name].rsi[0] if self.rsi[name].rsi[0] else None,
            "macd": MACDStatus.STATUS_MAP.get(self.macd[name].status[0], "震荡"),
            "ma20": round(self.ma20[name][0], 3) if self.ma20[name][0] else None,
        })
        if channel in self.channel_counts[name]:
            self.channel_counts[name][channel] += 1
        self.channel_counts[name]["total_entries"] += 1
        return super()._buy(data, pct, reason)

    def _sell(self, data, pct, reason):
        name = data._name
        price = data.close[0]
        self.trade_log.append({
            "date": self.data.datetime.date(0).strftime("%Y-%m-%d"),
            "code": name,
            "action": "SELL_PART",
            "pct": pct,
            "price": round(price, 3),
            "reason": reason,
            "avg_cost": round(self._get_avg_cost(data), 3),
            "shares": self._get_shares(data),
        })
        return super()._sell(data, pct, reason)

    def _close(self, data, reason):
        name = data._name
        price = data.close[0]
        avg = self._get_avg_cost(data)
        pnl_pct = (price - avg) / avg * 100 if avg > 0 else 0

        exit_type = "unknown"
        if "均价止损" in reason: exit_type = "均价止损"
        elif "硬止盈" in reason: exit_type = "硬止盈"
        elif "移动止盈" in reason: exit_type = "移动止盈"
        elif "分级止损" in reason or "止损" in reason: exit_type = "分级止损"
        elif "硬止损" in reason: exit_type = "硬止损"
        elif "保本止盈" in reason: exit_type = "保本止盈"
        elif "趋势止盈" in reason: exit_type = "趋势止盈"

        self.exit_log.append({
            "date": self.data.datetime.date(0).strftime("%Y-%m-%d"),
            "code": name,
            "action": "CLOSE",
            "price": round(price, 3),
            "avg_cost": round(avg, 3),
            "pnl_pct": round(pnl_pct, 2),
            "reason": reason,
            "exit_type": exit_type,
        })
        if exit_type in self.channel_counts[name]:
            self.channel_counts[name][exit_type] += 1
        self.channel_counts[name]["total_exits"] += 1
        if pnl_pct >= 0:
            self.channel_counts[name]["win_count"] += 1
            self.channel_counts[name]["win_amount"] += pnl_pct * avg * self._get_shares(data) / 100
        else:
            self.channel_counts[name]["loss_count"] += 1
            self.channel_counts[name]["loss_amount"] += abs(pnl_pct * avg * self._get_shares(data) / 100)
        self.channel_counts[name]["total_pnl"] += pnl_pct * avg * self._get_shares(data) / 100
        return super()._close(data, reason)


def run_detailed_backtest(start_date, end_date, label=""):
    """Run backtest with detailed logging and return results"""
    active_codes = {k: v for k, v in CODES.items() if k != "000725"}
    trade_start, trade_end = start_date, end_date

    print(f"\n{'='*70}")
    print(f"  Detailed Backtest: {label} ({start_date} → {end_date})")
    print(f"{'='*70}")

    # Fetch data
    dataframes = {}
    for code, cfg in active_codes.items():
        try:
            df = fetch_daily(code, cfg["sid"])
            dataframes[code] = df
        except Exception as e:
            print(f"  ✗ {code} {cfg['name']}: fetch failed: {e}")

    start = pd.Timestamp(trade_start); end = pd.Timestamp(trade_end)
    empty_codes = []
    for code in list(dataframes.keys()):
        sliced = dataframes[code][start:end]
        if len(sliced) == 0:
            empty_codes.append(code)
        else:
            dataframes[code] = sliced
    for code in empty_codes:
        del dataframes[code]
        del active_codes[code]

    if not dataframes:
        print("✗ No data in period")
        return None

    # Align dates
    all_dates = pd.date_range(start=start, end=end, freq="B")
    for code in dataframes:
        df = dataframes[code]
        first_date = df.index[0]
        df = df.reindex(all_dates)
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].ffill().bfill()
        df["volume"] = df["volume"].fillna(0).astype(float)
        df.loc[df.index < first_date, "volume"] = -1.0
        dataframes[code] = df

    cerebro = bt.Cerebro()
    for code, df in dataframes.items():
        data = bt.feeds.PandasData(dataname=df, name=code)
        cerebro.adddata(data, name=code)

    cerebro.addstrategy(DetailedETFStrategy)
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', timeframe=bt.TimeFrame.Days, annualize=True, riskfreerate=0.02)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')

    comm = ETFCommission()
    cerebro.broker.addcommissioninfo(comm)
    cerebro.broker.setcash(TOTAL_FUND)
    cerebro.broker.set_coc(False)

    results = cerebro.run()
    strat = results[0]

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

    d1 = pd.Timestamp(trade_start).to_pydatetime().date()
    d2 = pd.Timestamp(trade_end).to_pydatetime().date()
    years = (d2 - d1).days / 365.0
    annual_ret = ((final / TOTAL_FUND) ** (1 / years) - 1) * 100 if years > 0 else 0
    calmar = annual_ret / max_dd if max_dd > 0 else 0

    # Per-ETF summary
    etf_summary = {}
    for code in active_codes:
        if code not in dataframes:
            continue
        ch = strat.channel_counts.get(code, {})
        etf_summary[code] = {
            "name": active_codes[code]["name"],
            "entries": ch.get("total_entries", 0),
            "exits": ch.get("total_exits", 0),
            "wins": ch.get("win_count", 0),
            "losses": ch.get("loss_count", 0),
            "total_pnl": round(ch.get("total_pnl", 0), 2),
            "channels": {
                k: v for k, v in ch.items()
                if k in ("RSI抄底", "趋势跟踪", "突破入场", "分批建仓", "Test抄底", "试探建仓", "补仓", "确认加仓")
                and v > 0
            },
            "exits_detail": {
                k: v for k, v in ch.items()
                if k in ("均价止损", "硬止盈", "移动止盈", "分级止损", "硬止损", "保本止盈", "趋势止盈", "破MA5卖")
                and v > 0
            },
        }

    # Daily values for equity curve analysis
    daily_values = strat.daily_values

    # Empty days analysis
    total_empty_days = 0
    for name, ps in strat.ps.items():
        total_empty_days += ps["empty_days"]

    return {
        "label": label,
        "start": trade_start,
        "end": trade_end,
        "final": final,
        "total_ret": round(total_ret, 2),
        "annual_ret": round(annual_ret, 2),
        "max_dd": round(max_dd, 2),
        "sharpe": round(sharpe_a.get('sharperatio', 0) or 0, 2),
        "calmar": round(calmar, 2),
        "total_trades": total_trades,
        "won": won,
        "lost": lost,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(pf, 2),
        "etf_summary": etf_summary,
        "entry_log": strat.entry_log,
        "exit_log": strat.exit_log,
        "trade_log": strat.trade_log,
        "daily_values": daily_values,
        "total_empty_days": total_empty_days,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--output", default="")
    args = p.parse_args()

    result = run_detailed_backtest(args.start, args.end, args.label)

    if result:
        # Output JSON for analysis
        output_path = args.output or f"/tmp/backtest_analysis_{args.label.replace(' ', '_')}.json"

        # Simplify for JSON output
        simple = {
            "label": result["label"],
            "start": result["start"],
            "end": result["end"],
            "total_ret": result["total_ret"],
            "annual_ret": result["annual_ret"],
            "max_dd": result["max_dd"],
            "sharpe": result["sharpe"],
            "calmar": result["calmar"],
            "total_trades": result["total_trades"],
            "won": result["won"],
            "lost": result["lost"],
            "win_rate": result["win_rate"],
            "profit_factor": result["profit_factor"],
            "etf_summary": result["etf_summary"],
            "entry_log": result["entry_log"],
            "exit_log": result["exit_log"],
            "total_empty_days": result["total_empty_days"],
        }

        with open(output_path, "w") as f:
            json.dump(simple, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n✅ Results saved to {output_path}")

        # Print summary
        print(f"\n{'='*70}")
        print(f"  SUMMARY: {result['label']}")
        print(f"{'='*70}")
        print(f"  Total Return: {result['total_ret']:+.2f}%")
        print(f"  Annual Return: {result['annual_ret']:+.2f}%")
        print(f"  Max Drawdown: {result['max_dd']:.2f}%")
        print(f"  Sharpe: {result['sharpe']:.2f} | Calmar: {result['calmar']:.2f}")
        print(f"  Trades: {result['total_trades']} | Win Rate: {result['win_rate']:.1f}% | PF: {result['profit_factor']:.2f}")
        print(f"\n  Per-ETF:")
        for code, s in result["etf_summary"].items():
            print(f"    {code} {s['name']:12s}: entries={s['entries']}, exits={s['exits']}, "
                  f"wins={s['wins']}, losses={s['losses']}, pnl={s['total_pnl']:+.0f}")
            if s["channels"]:
                print(f"      Entry channels: {s['channels']}")
            if s["exits_detail"]:
                print(f"      Exit types: {s['exits_detail']}")

        print(f"\n  Entry Log ({len(result['entry_log'])} entries):")
        for e in result["entry_log"][:20]:
            print(f"    {e['date']} {e['code']} {e['channel']:10s} @{e['price']:.3f} "
                  f"({e['pct']*100:.0f}%) RSI={e['rsi']} MACD={e['macd']}")

        print(f"\n  Exit Log ({len(result['exit_log'])} exits):")
        for e in result["exit_log"]:
            print(f"    {e['date']} {e['code']} {e['exit_type']:10s} @{e['price']:.3f} "
                  f"avg={e['avg_cost']:.3f} pnl={e['pnl_pct']:+.1f}%")