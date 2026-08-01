# Backtrader Migration Notes (2026-08-01)

## Migration from hand-written backtest_5etf_1m.py to backtest_bt.py

### Architecture Change

**Old (hand-written)**: Manual cash tracking, position dict, share calculation, fund constraint checks in strategy code.
**New (backtrader)**: Strategy uses `self.buy()/self.sell()` via broker, backtrader handles order execution, cash management, and position tracking.

### Key Design Decisions

1. **Position management by ratio, not by cap**: `_buy(data, pct, reason)` takes a percentage of total fund value (e.g. 0.30 = 30%), computes target shares, lets broker reject if insufficient funds. `_sell(data, pct, reason)` takes a percentage of current position. User explicitly said "仓位上限也不用管" — no per-ETF cap.

2. **COC mode**: `set_coc(False)` uses next-day open price (more realistic). Hand-written used same-day close. The ~19% return difference is primarily due to execution price difference. **Backtrader is more accurate** — same-day close execution is a "future function" (can't actually buy at the signal price).

3. **Custom indicators**: WilderRSI, MACDStatus, AOIndicator — all implemented as `bt.Indicator` subclasses. Must use `self.data[-i]` for historical access, NOT `self.data.close.get(size=N)`.

4. **Analyzers**: SharpeRatio, DrawDown, TradeAnalyzer, Returns — replace manual statistics. TradeAnalyzer only counts complete open-close cycles, not individual entries/exits.

5. **Warmup data**: For short-period backtests, provide warmup data from ~1 year before the target period using `PandasData(dataname=sub, fromdate=start_ts, todate=end_ts)`. Without warmup, indicators (MA60, ADX, MACD) crash with IndexError.

### Backtrader Custom Indicator Pitfalls

1. `self.data.close.get(size=N)` raises `AttributeError: 'Lines_LineSeries_LineSeriesStub' object has no attribute 'close'` — use `[self.data[-i] for i in range(N-1, -1, -1)]` instead.

2. `self.data.high`/`self.data.low` are NOT accessible in custom indicators initialized with `d.close` as input. Only `self.data[0]` (the close line) works. For AO which needs (high+low)/2, approximate with close. **NOTE**: This approximation creates a backtest-vs-live inconsistency — signal_generator.py uses `(H+L)/2` for AO, while backtest uses close. Fix pending.

3. `len(self.data)` gives available bars in custom indicators, NOT `len(self.data.close)`.

4. Custom indicators must call `self.addminperiod(N)` to ensure enough warmup data.

5. **MACDStatus buffer overflow**: when computing `closes = [self.data[-i] for i in range(size-1, -1, -1)]`, if `size` exceeds the actual buffer, it throws IndexError. Fix: only fetch the minimum needed bars: `closes = [self.data[-i] for i in range(min(size, min_needed + self.p.fast) - 1, -1, -1)]`. Critical for short-period backtests with warmup data using `fromdate`.

### Annual Return Calculation

Use calendar days / 365, NOT trading days / 242:
```python
from datetime import date
d1 = date.fromisoformat(TRADE_START); d2 = date.fromisoformat(TRADE_END)
years = (d2 - d1).days / 365.0
annual_ret = ((final / TOTAL_FUND) ** (1 / years) - 1) * 100
```

### Trade Counting

Backtrader's TradeAnalyzer counts complete open-close cycles (25-43 trades in backtest). The hand-written backtest counted every buy/sell/reduce/liquidation (894 trades). These are different metrics — use the one appropriate for the analysis.

### Multi-Period Results (v3.2, with warmup from 2020-01-01)

| Period | Total Return | Annual | Max DD | Calmar | Sharpe | Trades | Win Rate | P/L Ratio |
|--------|-------------|--------|--------|--------|--------|--------|----------|-----------|
| 5yr | +93.47% | +14.14% | 12.92% | 1.09 | 1.10 | 43 | 48.8% | 4.40 |
| 3yr | +93.47% | +24.71% | 12.92% | 1.91 | 1.52 | 43 | 48.8% | 4.40 |
| 2yr | +93.47% | +39.41% | 12.92% | 3.05 | 1.93 | 43 | 48.8% | 4.40 |
| 1yr | +48.63% | +49.45% | 14.53% | 3.40 | 1.54 | 33 | 39.4% | 3.44 |
| 6mo | +24.44% | +57.37% | 17.51% | 3.28 | 1.71 | 15 | 60.0% | 1.17 |
| 3mo | -2.53% | -10.19% | 16.33% | -0.62 | -0.46 | 10 | 20.0% | 1.09 |

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

**Key insight**: the strategy's strength is concentrated positions in trending ETFs; adding diversification or market-regime filters destroys this edge. 515880 (通信ETF) was the main profit driver — limiting it is counterproductive.

## ⚠️ Backtest vs Live Consistency Audit (2026-08-01)

**6 critical inconsistencies found between backtest_bt.py and signal_generator.py.** The backtest results are NOT reliable for predicting live performance until these are fixed.

### P0 — Must Fix (backtest results are misleading)

| # | Issue | Backtest | Live | Impact |
|---|-------|----------|------|--------|
| 1 | **Entry position ratio base** | Total fund 22万×30%=6.6万/笔 | Per-ETF 4.4万×30%=1.32万/笔 | Backtest buys 5x more per trade |
| 2 | **Confirm add 70%** | Total fund 22万×70%=15.4万 | Per-ETF 4.4万×70%=3.08万 | Backtest adds 5x more |
| 3 | **AO calculation** | close近似median | (H+L)/2 median | AO values differ, affects confirm-add |
| 4 | **COOLDOWN_DAYS** | 2 | 1 | Backtest more conservative |
| 5 | **Trend profit sell + MA5 active share sell** | Not in backtest | In live (2 sell signals) | Backtest overestimates returns |

### P1 — Overfitting Risk

| # | Issue | Evidence |
|---|-------|----------|
| 6 | **515880 concentration** | 15/27 buys are 515880 — strategy depends on one ETF |
| 7 | **First 3yr zero trades** | 6-channel entry conditions too strict, only works in specific market regimes |
| 8 | **Magic numbers** | 8%/15%/5%/10%/20%/30%/3%/35%/40% all hardcoded, no sensitivity analysis |

### Fix Priority

1. **Unify entry ratio base** — either backtest uses per-ETF amounts or live uses total-fund ratios
2. **Fix AO to use (H+L)/2** in backtest (requires passing OHLC data to AOIndicator)
3. **Unify COOLDOWN_DAYS** to 2 (change signal_generator.py from 1 to 2)
4. **Add trend profit sell and MA5 active share sell** to backtest
5. **Run parameter sensitivity analysis** on key thresholds (±2% on each)

### How to Fix Entry Ratio Base

The core issue: `_buy(data, 0.30, reason)` in backtest means 30% of total fund (22万), while signal_generator's "30%" means 30% of per-ETF allocation (4.4万). Two options:

**Option A: Backtest matches live** — change `_buy` to use per-ETF allocation:
```python
def _buy(self, data, pct, reason):
    name = data._name
    etf_fund = TOTAL_FUND / len(self.datas)  # 4.4万 per ETF
    target_value = etf_fund * pct  # 4.4万 × 30% = 1.32万
    ...
```

**Option B: Live matches backtest** — change signal_generator to use total-fund ratios:
```python
# In signal_generator.py, change fund from 44000 to 220000
# But this would require rethinking the entire position management
```

Option A is simpler and more realistic — each ETF gets an equal allocation of the total fund.

### File Status

- `backtest_bt.py` — official backtrader engine (current)
- `backtest_5etf_1m.py.bak` — hand-written backtest (superseded, kept for reference)
