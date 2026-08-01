# ETF Strategy Details

Full entry/exit/stop rules for the 5-ETF portfolio system.

## Entry (5 channels, build_phase==0)

### Channel 1: RSI Bottom-Fishing
- Condition: MACD golden cross
- Position: 30% of allocation
- Notes: simplest entry, just golden cross triggers

### Channel 2: Breakout Entry
- Condition: 20-day new high + golden cross
- Notes: Channel 1 already covers golden cross, so this is simplified

### Channel 3: Batch Build
- Conditions: golden cross OR red bar expanding + RSI>40 + above MA5
- Position: 30% of allocation

### Channel 4: Test Bottom
- Conditions: RSI<35 + green bar shrinking + above MA5
- Position: 30% of allocation

### Channel 5: Probe
- Conditions: green bar shrinking / oscillation + RSI>30 + above MA5
- Position: 15% of allocation

## Confirm Add (build_phase 1→2)
- Break above first_price OR MA5 touch count >= 2
- AO momentum filter: AO must be rising (ao_now > ao_5ago), otherwise skip
- Position: 70% of allocation
- After confirmed add: reset base/peak/trail/reached flags

## Stop-Loss (priority order, highest first)

### 1. Average Price Stop 10%
- From buy average price, drop 10% → unconditional clear
- No exceptions

### 2. Hard Stop 20%
- From market value peak, drawdown 20% → clear
- Backtest has this, live simplified

### 3. Tiered Stop Level 1
- 2 consecutive days below MA20 → reduce 30% total position

### 4. Tiered Stop Level 2
- DIF < 0 → reduce another 30%

### 5. Tiered Stop Level 3
- MACD death cross / green bar expanding → clear all

## Take-Profit

### Hard Take-Profit 30%
- Base price +30% → clear

### Trailing Stop
- Profit reaches 8%: trail = cost + 5%
- Profit reaches 15%: trail = peak - 5%

### Trend Take-Profit
- MACD red bar shrinking + break MA5 → sell 20% active position
- RSI>50 + break MA5 → sell 10% active position

## Position Add (补仓)

- Counter-trend add: drop >3% + NOT death cross / green bar expanding → add 15%
- Limit: add_count < 5
- After confirmed add reaches 70%: reset base/peak/trail/reached flags

## Intraday T+0 (signal_generator.py v3.1)

### Buy T Scoring (need ≥2 for signal)
- RSI < 40 → +1 (was 35, raised to catch rebound-day RSI)
- MACD in [金叉, 红柱放大, **绿柱缩短**] → +1 (绿柱缩短 added — reversal signal)
- Volume ratio < 1.5 → +1 (was 1.0, raised because rebound days have higher volume)
- Score 2 → half active position; Score 3 → full active position
- **Constraint**: price must be below avg_cost (摊低)

### Sell T Scoring (need ≥2 for signal)
- RSI > 65 → +1
- MACD in [红柱缩短, 死叉, 绿柱放大] → +1
- Volume ratio > 1.2 → +1
- Score 2 → half active position; Score 3 → full active position
- **Profit constraint**: price must be above avg_cost, OR hedge (死叉/绿柱放大 + price below cost)

### T+0 Constraints
- Must have position (底仓) — cannot sell what you don't have
- Active shares must be > 0 for sell T (don't sell dead/底仓)
- Max 1 T per day (normal), 2 per day (ATR > 5%)
- T+0 buy: must have price < avg_cost
- T+0 sell: must have price > avg_cost (profit) or hedge condition

## Key Technical Decisions

1. **Forward-adjusted data**: Sina API does not support forward-adjustment. Self-developed detection: jump when open/prev_close < 0.70, auto-calculate adjustment factor
2. **Wilder RSI**: Both live and backtest use exponential smoothing RSI (not simple average)
3. **SMA-warm MACD**: EMA initial value uses SMA, not c[0], to avoid early data bias
4. **MACD state filtering**: Red bar shrinking / green bar shrinking does NOT require h>0/h<0 (consistent with backtest)
5. **Base/active position split**: Live has (60%/40%), backtest does not (simplified to total position)
6. **Intraday T**: Live has (in signal_generator.py), backtest does not

## Single-ETF Backtest Results (159532)

1-year backtest (2025-07-27 → 2026-07-27), 44k capital:
- **Return: -3.49%**, Max drawdown: 7.21%, Calmar: -0.48
- 22 trades, frequent stop-loss cycles (4 full liquidations)
- AO filter blocked 20+ confirm-add attempts — strategy underperforms on this ETF due to repeated stop-loss in choppy market
- Compare: 5-ETF portfolio 1-year return +55.99%
- Verdict: 159532 not recommended for addition to pool — negative return, poor Calmar

## Latest Backtest Results (5-ETF Portfolio)

| Period | Return | Drawdown | Calmar |
|--------|--------|----------|--------|
| 3-year | +80.17% | 15.34% | 5.23 |
| 1-year | +56.53% | 6.31% | 8.95 |
| 6-month | +25.71% | 5.00% | 5.14 |
| 3-month | +11.64% | 5.74% | 2.03 |

### Strategy Optimization Attempts (v3.2 — REVERTED)

Tried two changes: (1) reduced build from 30%/70% to 20%/50%, (2) added early stop at 7%/9% loss.

| Variant | Build | Confirm | Early Stop | 3yr Return | Max DD |
|---------|-------|---------|------------|------------|--------|
| v3.1 (current) | 30% | 70% | none | 80.17% | 15.34% |
| Build only | 20% | 50% | none | 56.56% | 11.27% |
| Build+Stop | 20% | 50% | 7%/9% | 56.87% | 11.35% |
| Build 25% | 25% | 45% | 8%/9% | 55.95% | 11.97% |

**Key finding**: The return drop was entirely caused by reduced build ratios (20%/50% = 70% total vs 30%/70% = 100% total). Early stop had negligible impact — most losses were already caught by the existing tiered stop (MA20/DIF/death cross). **Conclusion: do not modify build ratios without strong evidence.**

### T+0 Scoring Optimization (v3.1 — APPLIED)

Original T+0 buy scoring missed rebound-day signals because:
- MACD condition was only 金叉/红柱放大, missing 绿柱缩短 (reversal signal)
- RSI threshold 35 was too strict for rebound days (RSI often 36-45)
- Volume ratio 1.0 was too strict for rebound days (volume naturally higher)

Changes applied:
- MACD: added "绿柱缩短" to buy score
- RSI: threshold 35 → 40
- Volume ratio: threshold 1.0 → 1.5

This ensures T+0 signals fire on strong reversal days (e.g. -8% then +8% bounce).

## Stop-Loss Chain Diagnostics

### Critical Bug: below_ma20_count Persistence (FIXED)

The `below_ma20_count` field was NOT persisted to portfolio.json's `_signal_state`, resetting to 0 on every signal_generator.py run. This paralyzed the entire tiered stop-loss chain:

1. **Tier 1** (2 consecutive days below MA20) could never trigger → counter always 0
2. **Tier 2** (DIF<0) requires `stop_level=1` from Tier 1 → stuck at `stop_level=0`
3. **Tier 3** (death cross) requires `stop_level=2` from Tier 2 → stuck

**Real-world impact**: 159516 held at -9.66% loss with `stop_signal: "未触发"`, despite being far below MA20 for many days.

**Fix**: Added `below_ma20_count` to both the load and save sections of signal_generator.py's portfolio.json handling. When manually fixing existing positions: set `below_ma20_count=2` and `stop_level=1` (or 2 if DIF<0) for positions already well below MA20.

### Stop-Level Sync Issue

When portfolio.json has stale `stop_level=0` for positions that have been below MA20 for many days, the tiered stop-loss chain is stuck at the start. After fixing below_ma20_count persistence, also manually set `stop_level` to match reality:
- `stop_level=1` if position is below MA20
- `stop_level=2` if position is below MA20 AND DIF<0

### Stop-Profit Signal Weaknesses (documented, not changed)

These are known trade-offs — the backtest shows +80% return with these rules:
1. **8% trailing stop triggers too early**: only 3% profit window (cost+5% sell after cost+8% peak)
2. **8%-15% gap**: no trailing protection between 8% and 15% profit
3. **Trend profit sell only sells active shares**: may be 0 if all is base position (dead shares)
4. **Hard 30% rarely triggers**: ETFs rarely gain 30% from base

### Sell Signal Diagnosis Checklist

When reviewing why sell signals aren't firing for a position:
1. Check `stop_level` in portfolio.json — does it match the current market state?
2. Check `below_ma20_count` in portfolio.json — is it persisted and accurate?
3. Compute each stop-loss trigger price: avg_cost*0.90 (10%), MA20, DIF threshold
4. Check MACD status — does it match the required condition for each tier?
5. Check `reached_8pct`/`reached_15pct` — are trailing stops set correctly?
6. Most common failure mode: persistence bug causing the entire chain to be stuck at stop_level=0
