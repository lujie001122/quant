# T+0 Scoring Audit (2026-08-01)

## Original Bugs Found

### Bug 1: RSI not mandatory for buy T
- `t0_buy_score` used additive scoring: RSI<40 (+1), MACD condition (+1), 量比 (+1), need ≥2
- Problem: MACD+量比 alone could reach 2 without RSI, triggering buy at RSI=59
- Fix: RSI<40 now mandatory — return 0 if not met

### Bug 2: RSI not mandatory for sell T
- `t0_sell_score` used additive scoring: RSI>65 (+1), MACD condition (+1), 量比 (+1), need ≥2
- Problem: MACD+量比 alone could reach 2 without RSI, triggering sell at RSI=39
- Fix: RSI>50 now mandatory — return 0 if not met. RSI>65 is bonus +1

### Bug 3: 绿柱缩短 in both buy and sell MACD conditions
- Buy: `["金叉", "红柱放大", "绿柱缩短"]`
- Sell: `["红柱缩短", "死叉", "绿柱放大", "绿柱缩短"]`
- Result: RSI=30 + 绿柱缩短 + 量比<1.5 → buy score=2 AND sell score=2 (via 绿柱缩短+量比>1.2)
- 159532 had 9 same-day buy+sell conflicts in 60 trading days
- Fix: Removed 绿柱缩短 from both. Buy: 金叉/红柱放大 only. Sell: 红柱缩短/死叉/绿柱放大 only.

### Bug 4: No MACD direction filter
- Buy T could trigger during 死叉/绿柱放大 (trend deteriorating)
- Sell T could trigger during 金叉/红柱放大 (trend improving)
- Fix: 死叉/绿柱放大 → return 0 for buy. 金叉/红柱放大 → return 0 for sell.

### Bug 5: Buy price threshold too strict
- Required price < cost exactly, but cost×1.02 would still be a good entry
- Fix: Changed to price < cost × 1.02

### Bug 6: No hedge for 绿柱缩短+oversold
- 绿柱缩短 removed from both buy and sell MACD conditions
- But 绿柱缩短 + RSI<40 + price<cost is genuinely dangerous (oversold + trend weakening)
- Fix: Independent hedge channel, not through scoring

## Verification Results

After fix, 60-day historical data check:
- 159516: buy 0→0, sell 21→24, conflicts 0 ✅
- 588170: buy 0→0, sell 30→22, conflicts 0 ✅
- 159532: buy 28→8, sell 14→6, conflicts 9→0 ✅

Backtest unchanged: 3yr +80.17%, drawdown 15.34%, Calmar 5.23

## Lesson: Verification Methodology

5 passes of "does it run without error" missed all these bugs. The correct verification for signal logic is:
1. Run scoring functions against 60 days of real K-line data
2. Check for same-day buy+sell conflicts (should be 0)
3. Check RSI conditions are enforced (buy only at RSI<40, sell only at RSI>50)
4. Check signal counts make sense (buy signals should be rare in downtrend)
5. Check MACD direction filter (no buy during 死叉, no sell during 金叉)
