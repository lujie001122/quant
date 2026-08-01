# Backtrader Migration Notes (2026-08-01)

## Migration from hand-written backtest_5etf_1m.py to backtest_bt.py

### Architecture Change

**Old (hand-written)**: Manual cash tracking, position dict, share calculation, fund constraint checks in strategy code.
**New (backtrader)**: Strategy uses `self.buy()/self.sell()` via broker, backtrader handles order execution, cash management, and position tracking.

### Key Design Decisions

1. **Position management by ratio**: `_buy(data, pct, reason)` takes a percentage of total fund value (e.g. 0.30 = 30%), computes target shares, lets broker reject if insufficient funds. User explicitly said "仓位上限也不用管" and "应该是按比例，不要写死" — use proportional ratios, NOT hardcoded absolute numbers. `_sell(data, pct, reason)` takes a percentage of current position.

2. **COC mode**: `set_coc(False)` uses next-day open price (more realistic). Hand-written used same-day close. **Backtrader is more accurate** — same-day close execution is a "future function".

3. **Custom indicators**: WilderRSI, MACDStatus, AOIndicator — all implemented as `bt.Indicator` subclasses. Must use `self.data[-i]` for historical access, NOT `self.data.close.get(size=N)`.

4. **Analyzers**: SharpeRatio, DrawDown, TradeAnalyzer, Returns — replace manual statistics. TradeAnalyzer only counts complete open-close cycles, not individual entries/exits.

5. **Warmup data**: For short-period backtests, provide warmup data from ~1 year before the target period using `PandasData(dataname=sub, fromdate=start_ts, todate=end_ts)`. Without warmup, indicators crash with IndexError.

### Backtrader Custom Indicator Pitfalls

1. `self.data.close.get(size=N)` raises `AttributeError` — use `[self.data[-i] for i in range(N-1, -1, -1)]` instead.

2. AO needs (H+L)/2 — initialize with `AOIndicator(d)` (full data feed), NOT `AOIndicator(d.close)`. Inside the indicator, access `self.data.high[-i]` and `self.data.low[-i]`. This is now consistent with live signal_generator.py's `calc_ao(highs, lows)`.

3. `len(self.data)` gives available bars in custom indicators, NOT `len(self.data.close)`.

4. Custom indicators must call `self.addminperiod(N)` to ensure enough warmup data.

5. **MACDStatus buffer overflow**: only fetch the minimum needed bars, not all available data. Critical for short-period backtests.

### Annual Return Calculation

Use calendar days / 365, NOT trading days / 242:
```python
from datetime import date
d1 = date.fromisoformat(TRADE_START); d2 = date.fromisoformat(TRADE_END)
years = (d2 - d1).days / 365.0
annual_ret = ((final / TOTAL_FUND) ** (1 / years) - 1) * 100
```

### Multi-Period Results (v3.3, total fund × pct base, backtest-live consistent)

| Period | Total Return | Annual | Max DD | Calmar | Sharpe | Trades | Win Rate | P/L Ratio |
|--------|-------------|--------|--------|--------|--------|--------|----------|-----------|
| 5yr | +97.88% | +14.66% | 8.88% | 1.65 | 1.21 | 40 | 52.5% | 4.23 |
| 3yr | +97.88% | +25.65% | 8.88% | 2.89 | 1.66 | 40 | 52.5% | 4.23 |
| 2yr | +97.88% | +41.00% | 8.88% | 4.62 | 2.10 | 40 | 52.5% | 4.23 |
| 1yr | +3.74% | +3.80% | 16.61% | 0.23 | 0.20 | 39 | 35.9% | 1.89 |
| 6mo | +7.47% | +16.12% | 10.40% | 1.55 | 0.87 | 7 | 57.1% | 1.39 |
| 3mo | -2.85% | -11.42% | 16.31% | -0.70 | -0.52 | 10 | 20.0% | 1.10 |

Note: 3yr/2yr/5yr have identical results because all profits came from 2023-08 onwards. First 2 years had no trades.

### v3.3 Optimization Attempts (ALL REJECTED)

Three optimization directions tested on backtrader engine. Baseline: +115.66% return, 13.35% DD, 1.22 Sharpe.

| Direction | Return | DD | Sharpe | Verdict |
|-----------|--------|-----|--------|---------|
| Baseline (no optimization) | +115.66% | 13.35% | 1.22 | — |
| +ADX<20 filter | +61.51% | 16.10% | 0.68 | ❌ REJECTED — ADX<20 too common in A-share ETFs |
| +40% per-ETF cap | +94.13% | 16.69% | 0.97 | ❌ REJECTED — limits the strongest ETF (515880) |
| +MA60 multi-line alignment | +78.87% | 12.51% | 0.84 | ❌ REJECTED — too few trades, low profit factor |
| +All three combined | +78.10% | 20.43% | 0.80 | ❌ REJECTED — worst of all |

**Key insight**: the strategy's strength is concentrated positions in trending ETFs; adding diversification or market-regime filters destroys this edge.

### v3.3 Backtest vs Live Consistency Fixes (ALL FIXED)

| # | Issue | Backtest | Live | Fix Applied |
|---|-------|----------|------|-------------|
| 1 | **Entry position ratio base** | Total fund × pct | Per-ETF fund × pct | ✅ Uses total fund × pct (user chose this — "应该是按比例，不要写死") |
| 2 | **AO calculation** | close近似 | (H+L)/2 | ✅ AOIndicator now uses `(self.data.high[-i] + self.data.low[-i]) / 2`, initialized with `AOIndicator(d)` not `AOIndicator(d.close)` |
| 3 | **COOLDOWN_DAYS** | 2 | 1 | ✅ signal_generator.py now uses COOLDOWN_DAYS=2 |
| 4 | **Trend profit sell + MA5 active share sell** | Not in backtest | In live | ✅ Both added to backtest_bt.py EXIT LOGIC |

### Position Ratio Base Decision

User was presented with two options:
- **Total fund × pct** (22万×30%=6.6万/笔) — concentrated positions, higher returns
- **Per-ETF 20% × pct** (4.4万×30%=1.32万/笔) — distributed positions, lower returns (+37.49%)
- User chose total fund × pct, and explicitly corrected "应该是按比例，不要写死" — no hardcoded absolute numbers

### File Status

- `backtest_bt.py` — official backtrader engine (v3.3, current)
- `backtest_5etf_1m.py` — DELETED (hand-written backtest, replaced by backtest_bt.py)
- `git_backup.sh` — updated to sync `backtest_bt.py` instead of `backtest_5etf_1m.py`
