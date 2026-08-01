# ETF Strategy Details

Full entry/exit/stop rules for the 5-ETF portfolio system.

## Entry (6 channels, build_phase==0) — v3.2

### Channel 1: RSI Bottom-Fishing
- Condition: MACD golden cross + RSI≤80
- Position: 30% of allocation

### Channel 2: Trend Follow
- Condition: MACD红柱(放大/缩短) + price > MA20 + empty_days > 10
- Position: 30% of allocation

### Channel 3: Breakout Entry
- Condition: 20-day new high (excl. today) + MACD golden cross
- Position: 30% of allocation

### Channel 4: Batch Build
- Conditions: golden cross / red bar expanding + RSI>40 + above MA5
- Position: 30% of allocation

### Channel 5: Test Bottom
- Conditions: RSI<35 + green bar shrinking + above MA5 + prev_macd_status==绿柱缩短
- Position: 30% of allocation

### Channel 6: Probe (v3.2: RSI tightened from 30→40)
- Conditions: green bar shrinking / oscillation + RSI>40 + above MA5
- Position: 15% of allocation
- Note: was 74% of entries with RSI>30; tightened to 40 reduces low-quality entries

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

## Take-Profit (v3.2: trailing stop delayed)

### Hard Take-Profit 30%
- Base price +30% → clear

### Trailing Stop (v3.2 change)
- Profit reaches 8%: mark reached_8pct, **no trail change** (was: trail = cost + 5%)
- Profit reaches 15%: trail = cost + 5%, then peak - 5%
- **Why**: 8%→trail=cost+5% was only 3% profit window. Delaying trail to 15% lets profits run. Biggest single improvement: +17.96% return.

### Trend Take-Profit
- MACD red bar shrinking + break MA5 → sell 20% active position
- RSI>50 + break MA5 → sell 10% active position

## Position Add (补仓) — v3.2: RSI<40 required

- Counter-trend add: drop >3% + NOT death cross / green bar expanding + **RSI<40** → add 15%
- Limit: add_count < 5
- **Why RSI<40**: prevents counter-trend adds in non-oversold conditions. Was +18.08% return improvement.

## Intraday T+0 (signal_generator.py v3.1)

### Buy T Scoring (need ≥2 for signal)
- RSI < 40 (mandatory) → +1
- MACD in [金叉, 红柱放大] → +1 (死叉/绿柱放大 → return 0)
- Volume ratio < 1.5 → +1
- Price < cost×1.02 (2% premium allowed)

### Sell T Scoring (need ≥2 for signal)
- RSI > 50 (mandatory) → +1
- RSI > 65 → +1 (bonus)
- MACD in [红柱缩短, 死叉, 绿柱放大] → +1 (金叉/红柱放大 → return 0)
- Volume ratio > 1.2 → +1
- Profit: price>cost; Hedge: price<cost + 死叉/绿柱放大

### Independent hedge (not through scoring)
- 绿柱缩短 + RSI<40 + price<cost → sell half active shares

### T+0 Constraints
- Must have position (底仓) — cannot sell what you don't have
- Active shares must be > 0 for sell T (don't sell dead/底仓)
- Max 1 T per day (normal), 2 per day (ATR > 5%)
- 绿柱缩短 is neutral — excluded from both buy and sell MACD conditions to prevent same-day buy+sell conflicts

## Key Technical Decisions

1. **Forward-adjusted data**: Sina API does not support forward-adjustment. Self-developed detection: jump when open/prev_close < 0.95, auto-calculate adjustment factor
2. **Wilder RSI**: Both live and backtest use exponential smoothing RSI (not simple average)
3. **SMA-warm MACD**: EMA initial value uses SMA, not c[0], to avoid early data bias
4. **MACD state filtering**: Red bar shrinking / green bar shrinking does NOT require h>0/h<0 (consistent with backtest)
5. **Base/active position split**: Live has (60%/40%), backtest does not (simplified to total position)
6. **Intraday T**: Live has (in signal_generator.py), backtest does not (day-K limitation)
7. **Backtest framework**: hand-written Python (no third-party framework). Uses Sina API for K-line data, self-contained loop with position management.

## Latest Backtest Results (5-ETF Portfolio)

### v3.2 (2026-08-01, 3 optimizations applied)
5yr +92.91%, drawdown 14.66%, Calmar 6.34, Sharpe 1.21, win rate 31.7%, profit factor 3.60

| Period | Return | Drawdown | Calmar |
|--------|--------|----------|--------|
| 5-year | +92.91% | 14.66% | 6.34 |
| 3-year | +89.32% | 15.92% | 5.61 |
| 1-year | +56.53% | 6.31% | 8.95 |

### v3.2 Optimization Results (per-change)

| Change | Δ Return | Δ Drawdown | Verdict |
|--------|----------|------------|---------|
| ① Probe RSI>30→40 | +2.85% | -0.89% | ✅ Applied |
| ② Stop-level 1 reduce 50% | -2.57% | -0.07% | ❌ Rejected |
| ③ Avg-cost stop 15% | -32.21% | +0.04% | ❌ Rejected |
| ④ 8% no trail change | +17.96% | -3.89% | ✅ Applied |
| ⑤ Dip-add RSI<40 | +18.08% | -9.67% | ✅ Applied |
| Combined ①④⑤ | +17.57% | -5.34% | ✅ Applied |

### Strategy Deep Analysis (v3.1 baseline, 894 trades over 5yr)

| Metric | Value | Implication |
|--------|-------|-------------|
| 试探建仓占比 | 74% (143/192) | 5 channels, but only 1 used — conditions too loose (fixed with RSI>40) |
| 止损3清仓 | 116次 (62% of liquidations) | Tiered stop reduces 30%+30% but still deteriorates to full clear |
| 止盈:止损 | 57:131 (70% stop out) | Most trades lose, but profit factor 2.80 means wins are much larger |
| 硬止损20% | 0次触发 | Never fires — tiered stop already reduced shares below threshold |
| 移动止盈8% vs 15% | 21:15 | Most positions peak at 8% then retrace — 8% trail line too tight (fixed with delayed trail) |
| 均价止损10% | 15次 | Deep bag-holds that hit -10% from average cost |

### Previous Optimization Attempts (v3.2 drawdown — ALL REVERTED)

| Attempt | Change | 3yr Return | 3yr DD | Verdict |
|---------|--------|------------|--------|---------|
| #1 | Build 20%/50% + early stop 7%/9% | 56.87% | 11.35% | ❌ Reverted |
| #2 | MA20-rising filter + probe 10% + cooldown 3 | 81.92% | 17.88% | ❌ Reverted |
| #3 | Confirm-add 70%→50% | 57.76% | 17.66% | ❌ Reverted |

**Conclusion**: 19% drawdown is structural — the strategy's strength is capturing trends via heavy confirm-add. The v3.2 strategy optimization (①④⑤) fixed the real issues (loose entry, premature trailing, aggressive dip-add) WITHOUT changing core strategy parameters.

## Stop-Loss Chain Diagnostics

### Critical Bug: below_ma20_count Persistence (FIXED)

The `below_ma20_count` field was NOT persisted to portfolio.json's `_signal_state`, resetting to 0 on every signal_generator.py run. This paralyzed the entire tiered stop-loss chain:

1. **Tier 1** (2 consecutive days below MA20) could never trigger → counter always 0
2. **Tier 2** (DIF<0) requires `stop_level=1` from Tier 1 → stuck at `stop_level=0`
3. **Tier 3** (death cross) requires `stop_level=2` from Tier 2 → stuck

**Fix**: Added `below_ma20_count` to both the load and save sections of signal_generator.py's portfolio.json handling.

### Stop-Profit Signal Weaknesses (v3.2: item 1 FIXED)

1. ~~8% trailing stop triggers too early: only 3% profit window~~ → **FIXED**: 8% no longer sets trail line
2. 8%-15% gap: no trailing protection between 8% and 15% → by design, let profits run
3. Trend profit sell only sells active shares: may be 0 if all is base position
4. Hard 30% rarely triggers for ETFs

### Sell Signal Diagnosis Checklist

When reviewing why sell signals aren't firing for a position:
1. Check `stop_level` in portfolio.json — does it match the current market state?
2. Check `below_ma20_count` in portfolio.json — is it persisted and accurate?
3. Compute each stop-loss trigger price: avg_cost*0.90 (10%), MA20, DIF threshold
4. Check MACD status — does it match the required condition for each tier?
5. Check `reached_8pct`/`reached_15pct` — are trailing stops set correctly?
6. Most common failure mode: persistence bug causing the entire chain to be stuck at stop_level=0
