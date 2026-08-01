# Backtrader Migration Notes (2026-08-01)

## Migration from hand-written backtest_5etf_1m.py to backtest_bt.py

### Architecture Change

**Old (hand-written)**: Manual cash tracking, position dict, share calculation, fund constraint checks in strategy code.
**New (backtrader)**: Strategy uses `self.buy()/self.sell()` via broker, backtrader handles order execution, cash management, and position tracking.

### Key Design Decisions

1. **Position management by ratio, not by cap**: `_buy(data, pct, reason)` takes a percentage of total fund value (e.g. 0.30 = 30%), computes target shares, lets broker reject if insufficient funds. `_sell(data, pct, reason)` takes a percentage of current position. User explicitly said "仓位上限也不用管" — no per-ETF cap.

2. **COC mode**: `set_coc(False)` uses next-day open price (more realistic). Hand-written used same-day close. The ~19% return difference (92.91% vs 73.85%) is primarily due to execution price difference.

3. **Custom indicators**: WilderRSI, MACDStatus, AOIndicator — all implemented as `bt.Indicator` subclasses. Must use `self.data[-i]` for historical access, NOT `self.data.close.get(size=N)`.

4. **Analyzers**: SharpeRatio, DrawDown, TradeAnalyzer, Returns — replace manual statistics. TradeAnalyzer only counts complete open-close cycles, not individual entries/exits.

### Backtrader Custom Indicator Pitfalls

1. `self.data.close.get(size=N)` raises `AttributeError: 'Lines_LineSeries_LineSeriesStub' object has no attribute 'close'` — use `[self.data[-i] for i in range(N-1, -1, -1)]` instead.

2. `self.data.high`/`self.data.low` are NOT accessible in custom indicators initialized with `d.close` as input. Only `self.data[0]` (the close line) works. For AO which needs (high+low)/2, approximate with close.

3. `len(self.data)` gives available bars in custom indicators, NOT `len(self.data.close)`.

4. Custom indicators must call `self.addminperiod(N)` to ensure enough warmup data.

### Annual Return Calculation

Use calendar days / 365, NOT trading days / 242:
```python
from datetime import date
d1 = date.fromisoformat(TRADE_START); d2 = date.fromisoformat(TRADE_END)
years = (d2 - d1).days / 365.0
annual_ret = ((final / TOTAL_FUND) ** (1 / years) - 1) * 100
```

### Trade Counting

Backtrader's TradeAnalyzer counts complete open-close cycles (25 trades in backtest). The hand-written backtest counted every buy/sell/reduce/liquidation (894 trades). These are different metrics — use the one appropriate for the analysis.

### Results Comparison (5yr, 2021-08-01 → 2026-07-27)

| Metric | Hand-written (same-day close) | Backtrader (next-day open) |
|--------|-------------------------------|---------------------------|
| Total return | +92.91% | +73.85% |
| Max drawdown | 14.66% | 13.79% |
| Sharpe | 1.21 | 0.90 |
| Win rate | 31.7% | 48.0% |
| Profit factor | 3.60 | 3.50 |

The ~19% return difference is primarily due to execution price (next-day open vs same-day close). The backtrader result is more realistic for live trading.
