---
name: etf-trading
description: "5-ETF quant trading - signals, backtesting, cron monitoring."
version: 1.0
tags: [etf, trading, quantitative, cron, macd, rsi]
---

# ETF Quantitative Trading System

Use when: user asks about ETF signals, backtesting, portfolio, intraday T, cron tasks, or any mention of 159516/515880/588170/159532/515050.

## System Overview

5-ETF portfolio, total capital 220k shared pool, 44k per-ETF cap:
- 159516 (半导体设备) / 515880 (通信) / 588170 (科创半导体) / 159532 / 515050 (中证全指)
- Data source: Sina API (primary) + akshare (fallback)
- Python 3.11, venv at ~/hermes-trading/.venv/
- Fund model: shared 220k pool. Position ratios are fractions of TOTAL fund (e.g. 30%=22万×30%=6.6万). User explicitly corrected: "这不是共享资金池，这是仓位管理" and "仓位上限也不用管" — position management, not capital allocation. **CRITICAL**: `_buy(data, pct)` uses `target_value = self.broker.getvalue() * pct` (total fund × ratio). User explicitly corrected "应该是按比例，不要写死" — do NOT hardcode per-ETF allocation (4.4万) or per-ETF 20% base. The ratio approach is: total fund × pct, with broker handling fund constraints naturally.
- **Git repo**: `git@github.com:lujie001122/quant.git` (main branch). Contains all strategy code + skill/SKILL.md + references/. Source files live at ~/hermes/scripts/ — push to repo after significant changes.
- **GitHub backup rule**: push to GitHub after every confirmed strategy file change. Daily cron backup at 21:00 runs git_backup.sh (syncs ~/hermes/scripts/ + skill files to ~/quant/ then pushes). Local repo at ~/quant/.
- **Security**: credentials (AppID, AppSecret, passwords, tokens, keys) must NEVER be uploaded to GitHub or any external platform. Store locally only.

## File Layout

| File | Location | Purpose |
|------|----------|---------|
| backtest_bt.py | ~/hermes/scripts/ | Backtrader backtest engine (v3.3, 5-ETF shared pool, 6 channels + trend profit sell + MA5 active sell, Analyzers, warmup data, position ratio=total fund×pct) |
| backtest_5etf_1m.py | DELETED | Legacy hand-written backtest — removed 2026-08-01, replaced by backtest_bt.py (backtrader) |
| signal_generator.py | ~/hermes/scripts/ | Live signal generator v3.1 (~1200 lines, refactored 2026-08-01: CODE_MAP/INVERTED_WEIGHTS module constants, _enter_position/_get_daily_log/to_dict/from_dict methods, _safe_float helper, fetch_klines_120min removed, t0 scoring functions now shared with intraday_t_once) |
| ~~engine.py~~ | ~~deleted~~ | Removed 2026-08-01 — signal_generator.py fully replaces it |
| ~~indicators.py~~ | ~~deleted~~ | Removed 2026-08-01 — MACD was inconsistent with signal_generator; intraday_t_once.py now imports from signal_generator |
| ~~fetch_klines_120min~~ | ~~deleted~~ | Removed 2026-08-01 — was a 1-line wrapper around fetch_klines_daily; callers now use fetch_klines_daily directly |
| intraday_t_once.py | ~/hermes/scripts/ | Intraday T+0 module v2.1 (t0 scoring functions + CODE_MAP imported from signal_generator — no more duplicate definitions) |
| sentiment_check.py | ~/hermes/scripts/ | Sina news sentiment v3.1 (logging, KeyError protection, bare except fix, threshold constants) |
| ths_client.py | ~/hermes/scripts/ | THSClient v3.1.5 (logging, _query_with_retry, exception handling in buy/sell) |
| portfolio.json | ~/hermes/scripts/ | Persistent position state |
| strategy_analysis.py | ~/hermes/scripts/ | Strategy deep analysis — channel distribution, stop-loss/take-profit breakdown, yearly stats, key findings (runs full backtest internally) |

Cron scripts (must live in ~/.hermes/scripts/):
- etf_premarket.sh - pre-market signal, runs at 9:00 weekdays
- etf_intraday.sh - intraday monitoring, runs every 30min during trading hours
- git_backup.sh - daily GitHub backup, syncs ~/hermes/scripts/ + skill files to ~/quant/ and pushes

## Cron Jobs

| Name | Schedule | Mode | Behavior |
|------|----------|------|----------|
| ETF舆情早报 | 30 8 * * 1-5 | Agent | Runs sentiment_check.py, analyzes 5 sectors for negative news, writes `sentiment_block.json` if any blocklisted |
| ETF交易监控 | 0,30 10-14 * * 1-5 | Agent | Runs signal_generator.py, checks sentiment_block.json for blocked ETFs, executes trades via THSClient (which wraps Evolving with activate + revoke-before-order + limit orders), silent when no signal |
| ETF复盘 | 0 20 * * 1-5 | Agent | Fetches holdings/account/entrust from 同花顺, gets ETF prices via akshare, analyzes trade quality + sector moves |
| ETF策略GitHub备份 | 0 21 * * * | Agent | Runs git_backup.sh, syncs all strategy files + skill to ~/quant/ and pushes to GitHub, silent when no changes |

All use agent mode (no_agent=false, LLM processes and decides). Deliver to origin.

**Sentiment-trade integration**: The 舆情早报 job writes `sentiment_block.json` at 8:30. The 交易监控 job reads this file before placing orders — blocked ETFs can only sell/reduce/stop-loss, not buy/add. The file is cleared when no blocklist applies.

**Cron setup notes**:
- For script-only jobs: scripts must live in `~/.hermes/scripts/` (cron only accepts relative filenames from this directory)
- Working copies also at `~/hermes/scripts/` — keep both in sync
- Agent-mode jobs use `enabled_toolsets` to limit token overhead (e.g. ["terminal", "file"] for trading)

## Strategy Logic (Quick Reference)

See references/strategy-details.md for full entry/exit/stop rules.
See references/cua-driver-china-setup.md for cua-driver installation on China-mainland networks (for operating 同花顺).
See references/evolving-setup.md for Evolving setup and usage (Mac同花顺自动化 — preferred over cua-driver).
See references/daily-review-format.md for the daily review (复盘) report format and sentiment block criteria.
See references/t0-scoring-audit.md for the T+0 scoring rewrite details (6 bugs found, same-day conflict fix, verification methodology).
See references/wechat-publishing.md for WeChat公众号 publishing setup (md2wechat tool, credentials, IP whitelist, integration).
See references/backtrader-migration.md for backtrader migration notes (custom indicators, COC mode, Analyzer usage, execution price differences).

### T+0 (做T) — v3.1.1 (2026-08-01)

**Buy T** (must score ≥2, RSI<40 mandatory):
- RSI<40 (mandatory, returns 0 if not met) +1
- MACD金叉/红柱放大 +1 (死叉/绿柱放大 → return 0)
- 量比<1.5 +1
- Price < cost×1.02 (2% premium allowed)

**Sell T** (must score ≥2, RSI>50 mandatory):
- RSI>50 (mandatory, returns 0 if not met) +1
- RSI>65 +1 (bonus)
- MACD红柱缩短/死叉/绿柱放大 +1 (金叉/红柱放大 → return 0)
- 量比>1.2 +1
- Profit: price>cost; Hedge: price<cost + 死叉/绿柱放大

**Independent hedge** (not through scoring):
- 绿柱缩短 + RSI<40 + price<cost → sell half active shares

**Key rule**: 绿柱缩短 is neutral — excluded from both buy and sell MACD conditions to prevent same-day buy+sell conflicts.

### Entry (6 channels, build_phase==0) — v3.1.2 (unified with backtest)
1. RSI bottom-fishing: MACD golden cross + RSI≤80, 30%
2. Trend follow: MACD红柱(放大/缩短) + price > MA20, 30%
3. Breakout: 20-day high (excl. today) + MACD golden cross, 30%
4. Batch build: golden cross / red bar expanding + RSI>40 + above MA5, 30%
5. Test bottom: RSI<35 + green bar shrinking + above MA5, 30%
6. Probe: green bar shrinking / oscillation + RSI>40 + above MA5, 15%

Confirm add (build_phase==1→2): 70% (AO momentum filter required — now properly enforced with if/elif)
Dip add (build_phase==1, inside correct branch): 15% per add, max 5 adds, RSI<40 required

### Stop-loss (priority order) — v3.1 (all 20 audit bugs fixed)
1. Average price stop 10%: unconditional clear
2. Hard stop 20%: peak drawdown 20% → clear (now implemented, was `pass`)
3. Tier 1: 2 consecutive days below MA20 (per-day tracking), reduce 30%, stop_level=1
4. Tier 2: DIF<0, reduce another 30%, stop_level=2
5. Tier 3: death cross / green bar expanding → clear, stop_level=3

**Backtest optimization attempt (v3.2)**: Tried early stop (7%/9%) and reduced build (20%/50%). Results: 3yr return dropped from 80.17% to 56.87%, drawdown only improved 4%. The build change was entirely responsible for the return drop; early stop had negligible effect. **Conclusion: do not modify build ratios or add early stop without strong evidence — the current strategy is already optimal.**

### Take-profit
- Hard 30%: base +30% clear
- Trailing: 8% → mark reached_8pct (no trail change), 15% → trail=cost+5%, then peak-5%
- Trend: red bar shrinking + break MA5, sell 20%

## User Preferences (MUST follow)

- Backtest = live consistency: cannot tolerate different algorithms in backtest vs live
- No per-ETF customization: treating it as overfitting
- No hardcoded absolute numbers: use proportional ratios (e.g. 20% of total fund per ETF, not 44000) — user explicitly corrected "应该是按比例，不要写死"
- Confirm before changes: never modify strategy or parameters without asking
- New ETF workflow: V8 backtest 1 year first, then show returns/drawdown/Calmar/trades, then ask if adding to pool
- Silent when no signal: cron pushes OK only when nothing to report; detailed content when there is
- Concise communication: short, direct, no fluff
- Intraday frequency: every 30 minutes, trading hours 10:00-14:30 (not 9:00-14:30)
- Default to 模拟 (simulation) account for 同花顺: use 模拟 by default, only switch to A股 when user explicitly says so
- 模拟账户自动执行: no confirmation needed for simulation account trades — execute directly via Evolving. Only confirm for real (A股) account trades.
- Daily workflow: 8:30 sentiment check → 10:00-14:30 trading signals + execution → 20:00 daily review
- Sentiment blocks trading: if sentiment check finds negative news for a sector, block buy/add for that ETF that day (sell/reduce/stop-loss still allowed)
- Sentiment report quality: both positive AND negative assessments must include specific reasons and events — never just "利好" or "利空" as labels

## Known Issues (remaining)

1. engine.py failed (收益 +41% -> +6%), reverted - do not re-enable
2. No T+0 simulation in backtest (day-K limitation)
3. Base/active position split not in backtest
4. dynamic_spacing: backtest uses fixed, live uses 0.6*base+0.4*ATR
- **5-min volume ratio: fetch_klines_5min always returns empty** — retained with `# TODO: 未来启用5分钟量比`, `calc_5min_vol_ratio` deleted (was never called)
- **120-min K-line: degrades to daily K-line** — `fetch_klines_120min` deleted entirely; callers now use `fetch_klines_daily` directly
7. **Cron last-run buy risk**: 14:30 run may execute buys at close auction (15:00), which can have unfavorable prices. Should consider restricting last run to sell-only.
8. **T+0 buy/sell share t0_count**: Both buy-T and sell-T call `record_t0()`, sharing one counter. After a T+0 buy, the paired T+0 sell is blocked by `MAX_DAILY_T0=1`. This is intentional — prevents frequent trading. T+0 buy and sell are independent signals, not paired operations. Note: the independent hedge channel (绿柱缩短+RSI<40+价低于成本) also uses `record_t0()`, so it shares the same counter.
9. **t0_sell_score receives prev_rsi but never uses it**: Dead parameter. Low priority.
10. **通道2 (breakout entry) added back**: Now matches backtest — 20日新高(不含当日) + MACD金叉 → 30%.
11. **Backtest vs live NOW CONSISTENT** (v3.1.5, 2026-08-01): All critical inconsistencies resolved. v3.1.5 changes from v3.1.4: (1) shared 220k pool + 44k per-ETF cap replaces independent pools (professional quant approach), (2) 6 entry channels now set base_price (was missing → hard stop 30% never triggered), (3) Test抄底 channel now checks prev_macd_status (was missing → look-ahead bias), (4) forward-adjust 0.95 unified in both signal_generator and backtest, (5) liquidation now calls reset_on_liquidate() (was missing → state machine stuck), (6) breakout entry 20-day high now excludes current day (was including it → look-ahead bias), (7) stop-level recovery requires MACD红柱 (tightened in both files).
12. **MACD calculation unified**: signal_generator.py now uses the same SMA-init algorithm as backtest_5etf_1m.py — DIF starts from i=0 (not i=slow), DEA initialized from difs[slow] (not SMA of first signal values). This ensures identical golden cross / death cross timing.
13. **reduce_shares bug (CRITICAL)**: `reduce_shares(30)` was computing `int(shares * 30 / 100) * 100 = 30000` for 1000 shares, resulting in full liquidation instead of 30% reduction. Fixed to `int(shares * pct / 10000) * 100`. Always test with actual numbers after modifying share calculation code.
14. **Floating-point precision**: `(1.15 - 1.0) / 1.0 * 100 = 14.99999...` which fails `>= 15`. Fixed by adding 0.01 tolerance: `if profit_pct >= 15 - 0.01`. Applied to both 8% and 15% thresholds in both signal_generator.py and backtest_5etf_1m.py.
- **Cooldown unified to 2 days**: COOLDOWN_DAYS=2 in both signal_generator and backtest (was 1 in backtest, 2 in signal_generator — inconsistency fixed 2026-08-01). Was temporarily changed to 3 as part of v3.2 drawdown optimization #2, but reverted because 5yr return dropped from 91%→82% while drawdown only improved 19%→18%.
16. **Trend profit sell / MA5 sell in backtest**: These are intraday operations that cannot be simulated with daily K-line. They are commented out in backtest but active in live. This is a documented known difference. If included in backtest, they produce 779 trades (50% of total) and inflate drawdown.
17. **Shared pool vs independent pools**: v3.1.5 uses shared 220k pool with 44k per-ETF cap. Independent pools (v3.1.4) had 42.97% drawdown because cash couldn't recycle across ETFs — this was an artifact, not a real risk measure. Shared pool with cap gives 15.92% drawdown (more realistic) and +89.32% return. The user (professional quant) explicitly corrected: "实盘资金不能共用，仓位管理你怎么考虑的？" — shared pool + position cap is the correct approach.

## Fixed Issues (2026-08-01 comprehensive rewrite)

All 20 bugs from the audit have been fixed in signal_generator.py v3.1 rewrite. Key fixes:

| # | Bug | Fix |
|---|-----|-----|
| 1 | urllib.request not imported at top level | Added `import urllib.request` at line 19 |
| 2 | AO filter bypassed in build_phase==1 confirm | Changed to `if/elif` structure — AO not rising → skip add entirely |
| 3 | 补仓 dead code (inside build_phase==0) | Moved to `build_phase==1` branch |
| 4 | Grid frozen never unfreezes | Added unfreeze logic: `if grid_frozen and price > MA5: grid_frozen = False` |
| 5 | Hard stop 20% is `pass`-ed | Implemented: `if price <= peak_price * 0.80: hard_stop_20pct` |
| 6 | build_phase==1 confirm unreachable | Now in correct branch (under `not has_position` with build_phase==1 check) |
| 7 | add_count/ma5_touch_count/prev_rsi/daily_trade_log not persisted | All added to `_signal_state` save/load |
| 8 | last_grid_trigger not reset across days | Added day-check on restore: if trigger != today_str, reset to None |
| 9 | liquidate_dates/cooldown_until never written | Added `reset_on_liquidate()` method: sets cooldown 3 days, records liquidate date |
| 10 | stop_level=3 never set | Tier 3 now sets `pos.stop_level = 3` |
| 11 | build_phase never resets after liquidation | `reset_on_liquidate()` resets build_phase to 0 |
| 12 | dead_shares/active_shares not updated on reduce | Added `update_dead_active()` method, called after any share change |
| 13 | RSI抄底 missing RSI≤80 | Added `(t["rsi"] is None or t["rsi"] <= 80)` condition |
| 14 | AO assigned twice | Removed duplicate assignment |
| 15 | 通道2 dead code | Removed entirely |
| 16 | below_ma20_count per-day tracking | Uses `below_ma20_date` to ensure only 1 increment per day |
| 17 | Tier 3 stop-loss expanded | Now includes 绿柱缩短 (not just 死叉/绿柱放大) |
| 18 | T+0 sell hedge expanded | `hedge_ok` includes 绿柱缩短 |
| 19 | daily_trade_log type safety | Added `isinstance(log, dict)` check |
| 20 | portfolio.json dual-path sync | signal_generator now copies to `~/.hermes/scripts/` after every write |

**Backtest baseline after v3.1.3 fixes**: 3yr +81.65%, drawdown 13.81%, Calmar 5.91

**Backtest baseline after v3.1.5 (shared pool + 44k cap, subagent audit fixes)**: 3yr +89.32%, drawdown 15.92%, Calmar 5.61, Sharpe 1.54, win rate 34.8%, profit factor 7.11

**Backtest baseline after code quality refactoring (2026-08-01, cooldown+peak_price bug fixes)**: 5yr +75.34%, drawdown 20.00%, Calmar 3.77, Sharpe 1.02, win rate 33.5%, profit factor 2.80. The lower results are CORRECT — old baseline contained bugs (COOLDOWN_DAYS=1 in backtest vs 2 in signal_generator; peak_price=0 on reduce disabled hard stop 20%). Also uses sample std dev for Sharpe and total profit factor (both are more correct statistics).

**Backtest baseline after v3.3 consistency fixes (2026-08-01, backtrader engine, backtest-live consistency)**: 5yr +37.49%/5.97%dd/Calmar 1.10/Sharpe 0.89/62笔/48.4%wr/3.32pf. **This is the REAL result** — the previous +93.47% was inflated by position ratio base mismatch (backtest used total fund × pct, live uses per-ETF fund × pct). Four fixes applied: (1) entry ratio unified to per-ETF 20%×pct, (2) AO uses (H+L)/2 (was close approximation), (3) COOLDOWN_DAYS=2 unified in both files, (4) trend profit sell + MA5 active share sell added to backtest. The return dropped from +93% to +37%, but drawdown also dropped from 13% to 6% — the strategy is more conservative and realistic.

**Strategy deep analysis (2026-08-01, 894 trades over 5yr)**:
| Metric | Value | Implication |
|--------|-------|-------------|
| 试探建仓占比 | 74% (143/192) | 5 channels, but only 1 used — conditions too loose |
| 止损3清仓 | 116次 (62% of liquidations) | Tiered stop reduces 30%+30% but still deteriorates to full clear |
| 止盈:止损 | 57:131 (70% stop out) | Most trades lose, but profit factor 2.80 means wins are much larger |
| 硬止损20% | 0次触发 | Never fires — tiered stop already reduced shares below threshold |
| 移动止盈8% vs 15% | 21:15 | Most positions peak at 8% then retrace — 8% trail line too tight |
| 均价止损10% | 15次 | Deep bag-holds that hit -10% from average cost |

**5 optimization directions identified (3 APPLIED, 2 REJECTED — 2026-08-01)**:
1. **Probe entry RSI>30→40** — ✅ APPLIED. +2.85% return, fewer low-quality entries. 试探建仓 was 74% of entries; conditions too loose.
2. **Tiered stop 1 reduce 50% instead of 30%** — ❌ REJECTED. -2.57% return. Too aggressive, reduces position before rebounds.
3. **Avg-cost stop 15% instead of 10%** — ❌ REJECTED. -32.21% return. Stop too loose, deep bag-holds.
4. **8% trailing stop: no trail change at 8%, only at 15%** — ✅ APPLIED. +17.96% return (biggest single improvement). 8%→trail=cost+5% was only 3% profit window; removing it lets profits run.
5. **Dip-add (补仓) requires RSI<40** — ✅ APPLIED. +18.08% return. Prevents counter-trend adds in non-oversold conditions.

**Combined effect of ①④⑤**: 5yr +92.91%/14.66%dd/Calmar 6.34/Sharpe 1.21. Return improved from +75.34% to +92.91%, drawdown from 20.00% to 14.66%.

**Analysis script**: `~/hermes/scripts/strategy_analysis.py` — runs the full backtest and produces channel distribution, stop-loss/take-profit breakdown, yearly stats, and key findings.

**v3.2 drawdown optimization attempts (2026-08-01, ALL THREE REVERTED)**: 
- **Attempt #1**: Tried early stop (7%/9%) and reduced build (20%/50%). 3yr return dropped from 80.17% to 56.87%, drawdown only improved 4%. Build change was entirely responsible for the return drop. REVERTED.
- **Attempt #2**: Tried MA20-rising filter on confirm-add (prevent adding in downtrend), probe entry 15%→10%, cooldown 1→3 days. 5yr drawdown 19.11%→17.88%, but return 91.10%→81.92%, Calmar 4.77→4.58. 3yr Calmar dropped from 5.61→4.74. REVERTED.
- **Attempt #3**: Confirm-add 70%→50% (reduce position size in choppy markets). 5yr drawdown 19.11%→17.66%, but return 91.10%→57.76%, Calmar 4.77→3.27. 3yr return dropped from 89.32%→58.57%, Calmar 5.61→4.03. The heavy confirm-add is the strategy's core profit engine — halving it destroys trend-capture ability. REVERTED.
**Conclusion: 19% drawdown is structural — the strategy's strength is capturing trends via heavy confirm-add, and choppy markets are its weakness.** Three failed optimization attempts (entry filters, reduced build, reduced confirm-add) all show the same pattern: 2% drawdown improvement costs 10-35% return. **Do not attempt further drawdown optimization without a fundamentally different approach (e.g. market regime detection, dynamic position sizing based on volatility, or a completely different strategy).**

**v3.1.4 key changes (2026-08-01, professional quant review)**:
1. **6 channels unified**: backtest now has all 6 entry channels (added channel 4: 分批建仓, channel 6: 试探建仓), matching signal_generator.
2. **Trend profit sell / MA5 sell commented out in backtest**: these are intraday operations that can't be simulated with daily K-line. Live retains them. This is a documented known difference.
3. **Forward-adjust threshold 0.70→0.95**: the old threshold missed small dividends/splits. 0.95 catches 5%+ adjustments.
4. **Grid/build logic separated**: grid signals no longer trigger build_phase==0 entry (grid only applies to existing positions). Prevents grid from acting as a shadow entry channel.
5. **Stop-level recovery tightened**: requires MACD红柱 (not just DIF>0), matching backtest. Prevents premature recovery from tiered stop.
6. **Position cap 44k**: ths_client.py buy() now caps at 44k per ETF, preventing over-allocation.
7. **Backtest statistics added**: Sharpe ratio, annual return, win rate, profit factor, per-ETF breakdown.

**v3.1.5 key changes (2026-08-01, subagent audit + user correction)**:
1. **Shared 220k pool + 44k per-ETF cap** replaces independent pools (v3.1.4). User explicitly corrected: "实盘资金不能共用，仓位管理你怎么考虑的？" — independent pools have low capital efficiency and artificially high drawdown. Shared pool + cap is the professional approach.
2. **6 entry channels now set base_price**: was missing → hard stop 30% never triggered. Each channel now sets `pos.base_price = price` on entry.
3. **Test抄底 channel now checks prev_macd_status**: was missing → look-ahead bias. Backtest used `prev_ms[code] == "绿柱缩短"` but signal_generator didn't. Now signal_generator has `prev_macd_status` field, updated end-of-day, persisted to portfolio.json.
4. **Liquidation now calls reset_on_liquidate()**: was missing → state machine stuck after liquidation (shares/cost/stop_level not reset). Now all stop_actions that produce "清仓" call `pos.reset_on_liquidate(today_str)`.
5. **Breakout entry 20-day high now excludes current day**: was `[-21:-1]` which included the last bar (could be today). Fixed to `[-22:-1]` with `len >= 21` check.
6. **Forward-adjust 0.95 unified in signal_generator**: was 0.70 in signal_generator vs 0.95 in backtest. Now both use 0.95.
7. **Multi-period results** (v3.1.5): 3yr +89.32%/15.92%/5.61

### v3.1.3 intraday_t_once.py fixes (2026-08-01)

| # | Bug | Fix |
|---|-----|-----|
| 1 | T+0 scoring completely different from signal_generator | Replaced with identical scoring functions (RSI<40 mandatory buy, RSI>50 mandatory sell, MACD mutual exclusion) |
| 2 | T+0 buy/sell used hardcoded RSI>60+死叉 / RSI<40+金叉 | Replaced with scoring system (≥2 triggers) |
| 3 | portfolio.json path was ~/.hermes/scripts/ | Fixed to ~/hermes/scripts/ (matching signal_generator) |
| 4 | t_shares calculation: `floor(shares * 0.2 / 100) * 100` | Fixed to `int(shares * 20 / 100 / 100) * 100` (clearer semantics) |
| 5 | No profit/hedge check on T+0 sell | Added: profit_ok (price>cost) or hedge_ok (price<cost + 死叉/绿柱放大) |
| 6 | T+0 buy missing price threshold | Added: price < cost×1.02 (matching signal_generator) |

**Note**: intraday_t_once.py uses 5-minute K-line for MACD (more sensitive for intraday), while signal_generator.py uses daily K-line (more stable for pre-market signals). This is intentional — different time horizons need different sensitivities.

### v3.1.2 fixes (2026-08-01, 10-pass audit)

| # | Bug | Fix |
|---|-----|-----|
| 1 | MACD calculation differs from backtest | Rewrote calc_macd to use same SMA-init algorithm as backtest |
| 2 | Missing 趋势跟踪 channel | Added: MACD红柱 + price>MA20 → 30% |
| 3 | Missing 突破入场 channel | Added: 20日新高 + MACD金叉 → 30% |
| 4 | Tier 3 stop-loss includes 绿柱缩短 | Removed to match backtest (only 死叉/绿柱放大) |
| 5 | `reduce_shares(30)` clears entire position | Fixed: pct/10000 instead of pct/100 |
| 6 | Floating-point: 15% profit = 14.99999 | Added 0.01 tolerance on 8% and 15% thresholds |
| 7 | Cooldown 3 days vs backtest 1 day | Unified to 1 day |
| 8 | Hard stop 20% uses price vs market value | Changed to market value (shares × price) |

### v3.1.3 fixes (2026-08-01, 6-pass deep audit)

| # | Bug | Fix | Impact |
|---|-----|-----|--------|
| 1 | **prev_ms updated same day** (backtest L277) | Moved prev_ms update to end-of-day loop; Test抄底 now correctly uses yesterday's MACD | Backtest improved: +80.17%→+81.65%, drawdown 15.34%→13.81% |
| 2 | **signal_generator missing empty_days** | Added empty_days field + per-day tracking (empty_days_date) + trend-following channel now requires empty_days>10 | 趋势跟踪通道 now works in live |
| 3 | **add_count reset to 0 on confirm** (signal) | Changed to add_count += 1 (matching backtest L516) | Prevents infinite re-add after confirm |
| 4 | **peak_price not reset on reduce** | Added self.peak_price = 0 in reduce_shares() (matching backtest L349) | Hard stop 20% now uses correct peak after partial sell |
| 5 | **Stop-loss priority order wrong** | Moved 硬止盈30% and 移动止盈 before 分级止损 in stop_actions list (matching backtest order) | Prevents 分级止损 overriding 硬止盈 |
| 6 | **realtime KeyError crash** | Added guard: `if code not in realtime: continue` and `if code not in all_tech: continue` | No crash on missing ETF data |

**Backtest result after v3.1.3**: 3yr +81.65%, drawdown 13.81%, Calmar 5.91

See references/signal-generator-audit.md for the full detailed audit with line numbers and fix suggestions.

## Evaluating New ETFs

When user asks to evaluate a new ETF code (e.g. "512690怎么样", "516780回测看看"):
1. **Run the same strategy** — use an inline Python script that embeds the full backtest logic (6 channels, tiered stop-loss, shared pool model). Do NOT copy and modify backtest_5etf_1m.py — inline scripts are more reliable and avoid sed/exec issues.
2. **Show multi-period results**: 3年/1年/6个月 minimum. User wants to see the full picture.
3. **Give professional judgment**: compare against current 5-ETF baseline (3yr +89.32%/dd 15.92%/Calmar 5.61). Flag if the ETF is unsuitable (e.g. long-term bearish, wrong sector, poor strategy fit).
4. **Do NOT add to portfolio without user approval** — show results, give recommendation, let user decide.
5. **Stocks are NOT ETFs** — strategy is designed for ETFs (high liquidity, no delisting risk, no black swan). If user asks to backtest a stock (e.g. 002775), run it but clearly state the strategy is not designed for stocks and recommend against adding.
6. **A-share ETFs are T+1, not T+0** — only cross-border ETFs (跨境ETF) are T+0. The strategy works on ETFs because of liquidity/no-delisting/no-black-swan, NOT because of T+0. Do NOT cite T+0 as a reason for ETF suitability.

## Running Single-ETF Backtest

The backtest script (`backtest_5etf_1m.py`) hardcodes all 5 ETFs. To backtest a single ETF:
1. **Preferred**: use an inline Python script (see "Evaluating New ETFs" above) — embeds the full strategy logic, avoids file copy issues.
2. **Alternative**: Copy the script: `cp backtest_5etf_1m.py backtest_<code>_1y.py`, modify `TRADE_START`/`CODES`/`INIT_CASH`, run with venv.
3. Do NOT modify the original script — always work on a copy or inline script

## WeChat Official Account Publishing

User wants to publish ETF daily reports / 复盘 to their WeChat official account (公众号).

### Tool: md2wechat (installed in venv)

CLI tool that converts Markdown to WeChat-compatible HTML and publishes to drafts. Uses `wechatpy` SDK internally.

```bash
# Publish markdown article to drafts (never auto-publishes)
WECHAT_APPID=xxx WECHAT_APP_SECRET=xxx md2wechat \
  --markdown article.md --style tech --title "ETF日报"

# Visual styles: academic_gray (default), tech, festival, announcement
# --comment to enable comments, --type newspic for 小绿书 format
```

**Setup requirements**:
1. Credentials stored locally at `~/hermes/scripts/.env.wechat` — NEVER upload to GitHub
2. **IP whitelist**: login mp.weixin.qq.com → 设置 → 开发 → 基本配置 → IP白名单, add the machine's public IP. Without this, API returns errcode 40164.
3. Article must contain at least one image (used as cover). Use `![cover](path)` in markdown.
4. md2wechat only saves to drafts (草稿箱) — user manually reviews and publishes.

**Subscription account (未认证订阅号) limitations**: many APIs return errcode 48001 (unauthorized):
- ✅ Draft API: works (create/list drafts)
- ❌ Comment open/close: requires certified service account — user must manually enable comments in WeChat backend
- ❌ Original declaration (原创声明): requires certified service account — user must manually enable when publishing
- ❌ Freepublish API: requires certified service account
- Workaround: `--comment` flag in md2wechat won't take effect on subscription accounts; remind user to manually enable comments + original declaration in WeChat backend after publishing

**Article style preference**: user rejected first draft as "太虚了" (too vague/hype). Write practical, actionable content — real code snippets, real numbers, real pitfalls. Include GitHub repo link for interested readers. No fluff or marketing language.

**Integration with cron**: the ETF复盘 job can optionally generate a markdown report and push to drafts via md2wechat, so user reviews on phone.

## Operating 同花顺

**Prefer API-based data access over screen scraping.** Use Sina API + akshare (already in the system) for data. Screen-scraping 同花顺 via `computer_use` is fragile and model-dependent — only use it if the user specifically needs to place orders or access features unavailable via API.

### THSClient Python Wrapper (基于 Evolving — v3.1.5)

A Python wrapper (`ths_client.py`) at `~/hermes/scripts/` wraps Evolving with retry logic. Key design decisions:

- **Auto-activate**: `__init__()` calls `_activate()` (osascript activate 同花顺 + 2s delay) — Evolving requires 同花顺 to be frontmost.
- **All orders are limit orders**: `buy()` and `sell()` require `price` parameter (no default, no market orders). User explicitly requires: "全部用限价单".
- **Revoke before placing same-direction orders**: `buy()` calls `revokeAllBuyEntrust()` before placing; `sell()` calls `revokeAllSellEntrust()` before placing. Uses Evolving batch methods (not iterating getEntrust — data format is unreliable flat-list concatenation).
- **44k per-ETF cap** enforced in `buy()`. Query methods have 1 retry on failure.
- **Revoke uses Evolving batch methods**: `revoke_all()` → `revokeAllEntrust()`, `revoke_all_buy()` → `revokeAllBuyEntrust()`, `revoke_all_sell()` → `revokeAllSellEntrust()`.

### Evolving Direct (首选，用于下单和交易)

**Always use Evolving directly for buy/sell/revoke operations.** Do NOT wrap it in additional layers. Evolving handles window activation and navigation internally.

```python
from evolving import evolving
e = evolving.Evolving()

# Query
e.getHoldingShares("stock")    # returns dict with status/data/info
e.getAccountInfo()              # returns dict with status/data/info

# Trade (simulation account: no confirmation needed)
status, contract = e.buy("159532", 1000)      # returns (bool, contractNo)
status, contract = e.sell("159532", 1000)      # returns (bool, contractNo)

# Revoke
e.revokeAllEntrust()            # revoke all — may fail, see pitfalls
```

**CRITICAL: Do NOT add extra osascript activate, has_window checks, or dismiss_alerts before Evolving calls.** These interfere with Evolving's internal flow and cause failures. Call Evolving methods directly — if they fail, try once more after a short delay, or ask the user to ensure 同花顺's trading panel is open.

### Evolving (底层，THSClient 基于此)

Evolving is a Python package that controls 同花顺 on macOS via AppleScript. THSClient wraps it; call Evolving directly only if THSClient doesn't expose the needed method.

Install: `pip install evolving -i https://pypi.tuna.tsinghua.edu.cn/simple && pip install xmltodict -i https://pypi.tuna.tsinghua.edu.cn/simple`

**Critical setup requirements:**
1. **Config file required**: `~/.config/evolving/config.xml` must exist even for read-only operations. Dummy values work for simulation accounts (see references/evolving-setup.md).
2. **Accessibility permission for Terminal.app**: System Settings → Privacy & Security → Accessibility → enable Terminal (or whatever terminal Hermes runs in). Without this, `osascript` calls via System Events will time out silently.
3. **同花顺 keyboard shortcuts**: 同花顺 Mac supports keyboard navigation. Prefer keyboard-driven AppleScript over screenshot-based (cua-driver) when possible.

### cua-driver (desktop automation — 已卸载)

cua-driver was installed then uninstalled per user request. User chose Evolving (AppleScript-based) as the sole 同花顺 automation method. Do not reinstall unless user asks. Installation reference preserved in references/cua-driver-china-setup.md.

### Data alternatives to 同花顺

- akshare (installed) — A股行情/指标, most reliable
- tushare — needs token, high data quality
- efinance — lightweight, 东方财富 data source
- easytrader — automated order placement, but **Windows only** (depends on pywinauto)

## Pitfalls

- Cron script paths: must be relative filenames under ~/.hermes/scripts/, NOT absolute paths. Working scripts at ~/hermes/scripts/ must be copied to ~/.hermes/scripts/ for cron.
- venv activation: scripts use source ~/hermes-trading/.venv/bin/activate (macOS path, not /home/ubuntu/)
- MACD state: red bar shrinking / green bar shrinking does NOT require h>0/h<0 - keep consistent with backtest
- Wilder RSI: must use exponential smoothing, not simple average
- SMA warm MACD: EMA initial value uses SMA, not c[0]
- Backtest script has `\r\n` line endings (from Windows/Linux export) — may cause issues with some Python tools; use `dos2unix` or `sed` if needed
- Computer use for 同花顺: fragile, requires vision-capable model, may time out. Prefer Evolving (AppleScript-based) for Mac同花顺 automation, or API-based approach for data
- Evolving + Terminal.app Accessibility: even with CuaDriver accessibility granted, Terminal.app itself needs Accessibility permission too. Without it, System Events AppleScript calls hang silently (no error, just timeout). Grant at System Settings → Privacy & Security → Accessibility → enable Terminal
- Evolving config file: `~/.config/evolving/config.xml` is required for ALL operations, even read-only queries like getHoldingShares. Dummy values work for simulation accounts
- 同花顺 keyboard shortcuts: 同花顺 Mac supports keyboard navigation natively. When automating via AppleScript, prefer keyboard-driven approaches over screenshot-based (cua-driver) — more reliable, no vision model needed
- User workflow: start with simulation account (模拟) to test Evolving, then switch to real account for live trading
- When running backtest for a single ETF, copy the script first — never modify the original. The script uses `exec()` which chokes on the shebang line; use a file copy with `sed` or manual edits instead
- delegate_task for computer_use operations: the subagent may hang on capture calls (observed 4+ min timeout). Prefer running computer_use directly in the main session
- pip install akshare on macOS: very slow (3min+), may timeout. Use `--no-deps` first then install remaining deps separately, or use `--timeout 60`
- Evolving ascmds.py "A股" patching: to use 模拟 account, we patched the installed package to replace all `click button "A股"` with `click button "模拟"`. This is fragile — pip upgrades will overwrite the patch. A better approach: modify the AppleScript at call time, or maintain a local fork/patch script. For now, the user's Evolving install has this patch applied.
- cua-driver was installed then uninstalled per user request. User chose Evolving (AppleScript-based) as the sole 同花顺 automation method. Do not reinstall cua-driver unless user asks. The cua-driver installation reference (references/cua-driver-china-setup.md) is kept for future reinstallation if needed.
- Evolving operational issues observed: `getEntrust()` and `revokeAllEntrust()` may return `status: False, info: 'unknown err'` or `'超过93天'` (simulation account inactive >93 days). Buy/sell and getHoldingShares work reliably; order query and revocation are less stable. If revoke fails, try alternative: `revokeAllBuyEntrust()` / `revokeAllSellEntrust()` separately, or use AppleScript directly to click the 撤单 button in the 委托 tab.
- 同花顺 window visibility (CRITICAL): 同花顺 Mac uses a custom-drawn UI that sometimes reports 0 windows to AppleScript (`count of windows` returns 0). This happens when the trading panel is closed or minimized. **Evolving's AppleScript calls ALL require window 1 to exist** — if the window is gone, every operation (buy, sell, getHoldingShares, etc.) will fail with "unknown err". Before Evolving calls, ensure 同花顺's trading window is open. If window count is 0: (1) `activate` alone won't restore the window, (2) try menu "窗口" → "主窗口", (3) try keyboard shortcuts, (4) if all else fails, ask the user to manually open the trading panel. The `activate` command always works; it's the window content that may be missing. After a `killall + open` restart, 同花顺 may not auto-open the trading window — user intervention may be needed.
- portfolio.json sync: when syncing from 同花顺 to portfolio.json, use Evolving's `getHoldingShares()` to get actual positions, then update `_signal_state` accordingly (build_phase=1 for held positions, build_first_price=cost, peak_price=cost). Keep both copies in sync: ~/hermes/scripts/ and ~/.hermes/scripts/
- osascript Accessibility: `osascript` itself needs Accessibility permission (separate from Terminal.app). If `System Events` calls return error -1719, the missing grant is for `osascript` specifically. Check System Settings → Privacy & Security → Accessibility for both Terminal and osascript.
- Evolving buy() return: when buy succeeds, returns `(True, contractNo)`. When contractNo is `"--"`, the order may have been placed but the contract number wasn't captured — check `getEntrust()` to verify. When buy fails, returns `(False, None)`.
- File extraction line-number prefix: files extracted from the user's tar.gz had `1|` line-number prefixes on every line (from read_file format). This causes JSON parse errors and Python syntax errors. Fix: `sed 's/^[0-9]*|//' input.py > output.py`. Always check extracted files for this pattern before running.
- **Evolving revocation — correct API methods**: `e.revoke()` does NOT exist. Use `e.revokeEntrust(contractNo)` for single-order revoke, or `e.revokeAllBuyEntrust()`/`e.revokeAllSellEntrust()`/`e.revokeAllEntrust()` for batch revoke. The batch methods are preferred — they're more reliable and avoid parsing the flat-list getEntrust() output.
- Evolving getEntrust() data format: returns a flat-list concatenation where contract numbers shift position between records. The `comment` field has 11 column names but each data row has 11+ items with the contract number from the previous row prepended. **Do NOT try to parse contract numbers from getEntrust() — use batch revoke methods instead.** If you need a specific contract number, use the one returned by buy()/sell().
- **THSClient _revoke_pending uses batch revoke**: `buy()` calls `revokeAllBuyEntrust()` before placing; `sell()` calls `revokeAllSellEntrust()`. This is a deliberate choice — parsing getEntrust() to find same-code/same-direction orders is unreliable due to the flat-list format. Batch revoke clears ALL pending orders of that direction, which is correct behavior (old stale orders should be replaced by the new signal).
- **Subagent audit false positives**: when subagents report "backtest vs live inconsistency", always verify by reading the actual code. In the v3.1.5 audit, 8 of 11 reported issues were either already fixed (empty_days/stop_level reset in reset_on_liquidate), misread (cooldown was 1 day in both files, not 3 vs 1), or by design (green bar shrinking in hedge vs scoring — different logic paths). **Do not blindly trust subagent findings — verify each one against the actual code before "fixing" it.**
- **All orders must be limit orders**: `buy()` and `sell()` require `price` parameter — no market orders, no `price=None` default. User explicitly requires: "全部用限价单".
- **Revoke before placing same-direction orders**: when signal_generator produces a buy signal, first revoke all pending buy orders (using `revokeAllBuyEntrust()`), then place the new limit order. Same for sell signals. This prevents stale orders from interfering with new signals. THSClient does this automatically in `buy()` and `sell()` methods.
- **t0_sell_score signature unified (v3.1.5)**: both signal_generator.py and intraday_t_once.py now have `t0_sell_score(rsi, prev_rsi, macd_status, vol_ratio_5min, vol_ratio_120)`. The prev_rsi parameter is currently unused but kept for future expansion. intraday_t_once.py passes `None` for prev_rsi since it doesn't track it.
- Single-ETF backtest: when running a single-ETF backtest, `sed` the original script for TRADE_START/INIT_CASH changes, then use Python to replace the CODES dict (multi-line replacement). The `exec()` approach fails on the shebang line — always use a file copy approach.
- THSClient has_window() check: 同花顺 5.3.2 uses a self-drawn UI; `count of windows` via AppleScript returns 0 even when the trading panel is visible on screen. Do NOT use AX window count as a gate — it will falsely block all operations. Instead, check if the 同花顺 process is visible (`name of every process whose visible is true`), or just try Evolving calls directly and handle failures (dismiss_alerts + retry). **The key insight**: Evolving's AppleScript works fine even when AX reports 0 windows — the window IS accessible to System Events, just not via the standard `count of windows` query. Always try Evolving directly before assuming the window is unavailable.
- User prefers Evolving-based Python wrapper: when asked to build a 同花顺 interface, build it on top of Evolving (not raw AppleScript from scratch). The user explicitly chose this approach over writing AppleScript directly. The ths_client.py wrapper must be thin (no extra window checks, no osascript, no ensure_ready) — just pass through to Evolving.
- Do not overthink 同花顺 window state: if the user says the trading panel is open, trust them and just call Evolving. Do not spend time trying to verify via AX window count or trying to programmatically open the window — these approaches are unreliable with 同花顺's self-drawn UI. If Evolving calls fail, the issue is likely a popup/alert, not a missing window. **Do NOT waste time trying to open the window programmatically** (cliclick, F6, Cmd+T, menu clicks, etc. — all failed). The only reliable way to open the trading panel is for the user to do it manually. After a 同花顺 restart, the user must manually open the trading panel before Evolving can work.
- Sentiment report must include specific reasons for BOTH positive AND negative signals: user explicitly requires "利空要有，利好也要有原因理由" — cannot just write "利好" or "利空" as a label, must include the specific event/policy/data that drove the assessment.
- When Evolving calls fail after a 同花顺 restart, the most likely cause is the user needs to manually open the trading panel. Do NOT try to automate opening it — just ask the user to open it. This has been verified across multiple approaches (cliclick, AppleScript menu clicks, keyboard shortcuts, URL schemes) — none work reliably with 同花顺's self-drawn UI.
- Trade reporting format: user requires ALL trades to include: reason, action (buy/sell), stock code, stock name, price. For T+0 trades, must also show available shares per ETF (not frozen) so user knows what can be sold today. Example: "买入 588170 科创半导体 6600股 @0.976, 原因: MACD绿柱缩短+RSI46.18+站上MA5, 可用: 300股(6600冻结)"
- Do NOT add extra has_window/activate checks before Evolving calls. Evolving handles window activation internally. Adding osascript activate or has_window checks PER-CALL can interfere and cause failures. **Exception**: THSClient.__init__() calls `_activate()` ONCE at instantiation — this is correct and necessary because Evolving requires 同花顺 to be frontmost. Do NOT add per-call activate/dismiss_alerts on top of this.
- When Evolving calls fail, the most common cause is a popup/alert blocking the UI (e.g. "超过93天"). Use dismiss_alerts() or direct AppleScript to click confirmation buttons, then retry. Do NOT assume the window is missing.
- Position management: user (professional quant) corrected the fund pool model: "这不是共享资金池，这是仓位管理" and "仓位上限也不用管" — the correct approach is **position management by ratio**, not capital allocation with caps. Strategy manages signal + position ratios (e.g. 30%/70% of total fund), broker handles fund constraints. NO per-ETF cap. Backtrader version implements this correctly with `_buy(data, pct, reason)` and `_sell(data, pct, reason)`.
- ths_client.py wrapper causes order failures (PRE-v3.1.3): the wrapper added has_window checks and dismiss_alerts calls that interfered with Evolving's internal window management. **v3.1.3 rewrite removed all osascript/ensure_ready/dismiss_alerts** — now a thin pass-through. Safe to use for both queries and trading.
- When Evolving operations fail intermittently (e.g. getHoldingShares returns "unknown err"), do NOT immediately assume 同花顺 is broken. Try again — the failure is often transient (popup timing, brief UI state). A single retry after 2-3 seconds usually succeeds. Do NOT waste time diagnosing the window state.
- Trade signal execution: when signal_generator.py produces buy/sell signals, execute them immediately via Evolving. Do NOT add intermediate verification steps (checking window state, re-running signals, etc.) — these add latency and can cause failures.
- **Strategy parameter changes must be backtest-validated**: user explicitly requires that any change to build ratios, stop-loss thresholds, or entry/exit logic be verified via backtest BEFORE and AFTER. The v3.2 optimization attempt (reduced build 20%/50%, early stop 7%/9%) cut 3yr returns from 80% to 57% — a 23% drop for only 4% drawdown improvement. The existing tiered stop (MA20/DIF/death cross) already catches most losses; adding early stop on top is redundant. **Never change strategy parameters based on intuition alone — always run the backtest.**
- **v3.2 drawdown optimization (second attempt, also failed)**: tried MA20-rising filter on confirm-add, probe 15%→10%, cooldown 1→3 days. 5yr drawdown 19.11%→17.88% but return 91.10%→81.92%, Calmar 4.77→4.58. 3yr Calmar dropped from 5.61→4.74. **Third attempt**: confirm-add 70%→50%. 5yr drawdown 19.11%→17.66% but return 91.10%→57.76%, Calmar 4.77→3.27. The heavy confirm-add is the core profit engine — reducing it destroys trend-capture ability. **Three failed optimization attempts confirm the current strategy is already near-optimal. Do not try further drawdown optimization without a fundamentally different approach.** When reverting multi-change optimizations, revert ALL changes atomically (both signal_generator.py AND backtest_5etf_1m.py) and verify backtest returns to baseline.
- **v3.3 optimization attempt (2026-08-01, ALL THREE REJECTED on backtrader)**: tried 3 additional optimization directions on the backtrader engine. Results vs baseline (+115.66% return, 13.35% DD, 1.22 Sharpe): (1) **ADX<20 filter** — REJECTED: return +115.66%→+61.51%, Sharpe 1.22→0.68. ADX<20 is too common in A-share ETFs, filtering out many valid entries. (2) **40% per-ETF position cap** — REJECTED: return +115.66%→+94.13%, Sharpe 1.22→0.97. The cap limits the strongest ETF (515880 通信) which was the main profit driver; concentration in the best-performing ETF is a feature, not a bug. (3) **MA60 multi-line alignment (MA5>MA20>MA60)** — REJECTED: return +115.66%→+78.87%, Sharpe 1.22→0.84. Higher win rate (65.2%) but too few trades (23 vs 42) and much lower profit factor (1.92 vs 3.52). **All three combined**: +78.10% return, 20.43% DD, 0.80 Sharpe — worst of all. **Key insight**: the strategy's strength is concentrated positions in trending ETFs; adding diversification or market-regime filters destroys this edge. The 515880 concentration was the profit engine — limiting it is counterproductive.

**✅ BACKTEST vs LIVE CONSISTENCY AUDIT (2026-08-01)** — 6 critical inconsistencies found between backtest_bt.py and signal_generator.py. **ALL 6 FIXED in v3.3**:

| # | Issue | Backtest | Live | Fix Applied |
|---|-------|----------|------|-------------|
| 1 | **Entry position ratio base** | Total fund ×30% | Per-ETF fund ×30% | ✅ Fixed: `_buy()` uses `etf_fund = broker.getvalue() * 0.20` then `target_value = etf_fund * pct` |
| 2 | **Confirm add 70%** | Total fund ×70% | Per-ETF fund ×70% | ✅ Fixed: same as above |
| 3 | **AO calculation** | close近似median | (H+L)/2 median | ✅ Fixed: AOIndicator now uses `(self.data.high[-i] + self.data.low[-i]) / 2` |
| 4 | **COOLDOWN_DAYS** | 2 | 1 | ✅ Fixed: signal_generator.py now uses COOLDOWN_DAYS=2 |
| 5 | **Trend profit sell + MA5 active share sell** | Not in backtest | In live (2 sell signals) | ✅ Fixed: both added to backtest_bt.py EXIT LOGIC |
| 6 | **Overfitting risk** | 515880=15/27 buys, first 3yr zero trades | — | 🟡 Documented: strategy is regime-dependent, works in trending markets |

**After fixes**: 5yr +37.49%/5.97%dd (was +93.47%/12.92%dd). The return drop is correct — the old +93% was inflated by position ratio mismatch.
- Position management review: when reviewing positions, compare each ETF's market value against its 44k allocation. Flag any over-allocation (e.g. 159516 at 139% of quota). However, do NOT implement automatic position capping — the user considers this overfitting. Instead, flag the issue and let the user decide.
- portfolio.json path: signal_generator.py reads from `~/hermes/scripts/portfolio.json` (path was fixed from `~/.hermes/scripts/`). Cron jobs may still write to `~/.hermes/scripts/portfolio.json`. **Always keep both copies in sync** — after any trade, copy: `cp ~/hermes/scripts/portfolio.json ~/.hermes/scripts/portfolio.json`. If signal_generator shows "无持仓" for positions that exist, check the path mismatch first.
- T+0 scoring parameters (v3.1.1 update — 2026-08-01): **Major rewrite of t0_buy_score and t0_sell_score.** Key changes: (1) RSI<40 is now MANDATORY for buy (return 0 if not met) — previously RSI was optional and MACD+量比 alone could score ≥2; (2) RSI>50 is now MANDATORY for sell (return 0 if not met) — prevents selling in oversold zone; (3) 绿柱缩短 removed from BOTH buy and sell MACD conditions — it was causing simultaneous buy+sell signals on the same day; (4) 死叉/绿柱放大 now BLOCK buy (return 0); (5) 金叉/红柱放大 now BLOCK sell (return 0); (6) Independent hedge channel added: 绿柱缩短+RSI<40+价低于成本 → sell hedge (not through scoring); (7) Buy price threshold relaxed from cost to cost×1.02. **Root cause**: the old scoring let MACD+量比 reach ≥2 without RSI, causing RSI=59 "buy" signals and RSI=39 "sell" signals. The 绿柱缩短 overlap caused same-day buy+sell conflicts (9 instances in 159532 backtest).
- Do NOT try to open 同花顺 trading window programmatically: cliclick, F6, Cmd+T, menu clicks, URL schemes, reopen — ALL failed. The only reliable method is user manually opens it. If Evolving fails, just ask the user to ensure the trading panel is open and retry once. Do NOT spend multiple turns trying different approaches.
- When user says a UI element is present, trust them and proceed. Do NOT waste time verifying via AX window count. The user explicitly said "交易面板默认就在" and "一直是点开的" — spending 10+ turns trying to verify was a mistake.
- Cron job trading must use THSClient (which wraps Evolving with activate + revoke + limit orders): updated the ETF交易监控 cron prompt to use `from ths_client import THSClient; c = THSClient(); c.buy(code, amount, price)` etc. THSClient now handles window activation, revoke-before-order, and limit-order enforcement internally.
- Trade reporting: user requires complete info for every trade: reason, action, stock code, stock name, price. For T+0: must show available shares per ETF (not frozen). For buy signals: must include the signal reason (e.g. "MACD绿柱缩短+RSI46.18+站上MA5"). User was frustrated when trade execution wasn't clearly communicated.
- **Backtest parameter changes must prove their value**: the v3.2 optimization attempt (build 20%/50%, early stop 7%/9%) cut 3yr returns from 80% to 57% with only 4% drawdown improvement. Early stop had negligible effect — the return drop was entirely from reduced build ratios. **The existing strategy (build 30%/70%, tiered stop) is verified optimal — do not modify without strong evidence and user approval.**
- **5-year drawdown root cause (2026-08-01)**: The 19.11% drawdown at 2024-09-20 was caused by 515880 (通信ETF) and 515050 (中证全指) repeatedly entering → confirming → getting stopped out in the Aug-Sep 2024 choppy market. 515880 had 312 trades (vs 159516's 194, 588170's 65) — the most active ETF, indicating it's poorly suited to the strategy in sideways markets. Three optimization attempts (MA20-rising filter + probe 10% + cooldown 3 days; confirm-add 70%→50%) all reduced drawdown by only 2% but cut returns by 10-35%. **The 19% drawdown is structural — the strategy's strength is capturing trends, and choppy markets are its weakness. Accept this trade-off rather than overfitting.**
- **CRITICAL: below_ma20_count persistence** (FIXED 2026-08-01): Now persisted to portfolio.json with `below_ma20_date` for per-day tracking. The entire tiered stop-loss chain (tier 1→2→3) now works correctly after restarts.
- **Stop-level sync** (FIXED 2026-08-01): `reset_on_liquidate()` resets stop_level, build_phase, and all counters on liquidation. `stop_level=3` is now set on tier 3 trigger.
- **Stop-profit signal weaknesses** (documented, not yet changed per user preference): (1) 8% trailing stop triggers too early — only 3% profit window (cost+5% sell after cost+8% peak); (2) 8%-15% gap with no trailing protection; (3) trend profit sell only sells active shares (may be 0 if all is base position); (4) hard 30% rarely triggers for ETFs. These are design trade-offs — the backtest shows +80% return with these rules, so do not change without evidence.
- **Signal sell-chain review**: when user asks to review sell signals, diagnose from the actual data: (1) check if stop_level in portfolio.json matches reality, (2) check if below_ma20_count is persisted, (3) compute each stop-loss trigger price vs current price, (4) check if MACD status matches the required condition for each tier. The most common failure mode is the persistence bug causing the entire chain to be stuck.
- **below_ma20_count per-day tracking**: FIXED (2026-08-01). Added `below_ma20_date` field — only increments `below_ma20_count` once per trading day. Both `below_ma20_count` and `below_ma20_date` are now persisted to portfolio.json. The counter now correctly implements "连续2天破MA20" (2 consecutive days, not 2 consecutive runs).
- **Tier 3 stop-loss unified with backtest**: FIXED (2026-08-01 v3.1.2). Tier 3 now uses only 死叉/绿柱放大 (matching backtest). 绿柱缩短 was removed from Tier 3 to maintain backtest consistency.
- **T+0 sell hedge expanded**: FIXED (2026-08-01). `hedge_ok` in T+0 sell now uses only 死叉/绿柱放大 (绿柱缩短 removed from hedge_ok to avoid same-day buy+sell conflict). A separate independent hedge channel handles 绿柱缩短+RSI<40+价低于成本.
- **Stop-level=2 no longer dead-end**: FIXED (2026-08-01). Tier 3 triggers on 绿柱缩短, so positions at stop_level=2 can progress to liquidation.
- **Grid unfreeze**: FIXED (2026-08-01). Added `if grid_frozen and price > MA5: grid_frozen = False` in signal loop.
- **Hard stop 20% implemented**: FIXED (2026-08-01). `if price <= peak_price * 0.80: hard_stop_20pct` — protects high-profit positions from large retracements.
- **Cooldown after liquidation**: FIXED (2026-08-01). `reset_on_liquidate()` sets `cooldown_until` to 1 day after clear (unified with backtest). Prevents immediate re-entry.
- **build_phase resets on liquidation**: FIXED (2026-08-01). `reset_on_liquidate()` resets build_phase to 0, allowing proper re-entry cycle.
- **All state fields persisted**: FIXED (2026-08-01). add_count, ma5_touch_count, prev_rsi, daily_trade_log (today only), last_grid_trigger, liquidate_dates all saved/loaded.
- **AO filter properly enforced**: FIXED (2026-08-01). Changed to if/elif structure — when AO not rising, the 70% add is entirely skipped.
- **补仓 in correct branch**: FIXED (2026-08-01). Moved from build_phase==0 to build_phase==1, where it correctly fires.
- **RSI≤80 condition added**: FIXED (2026-08-01). RSI抄底 now requires RSI≤80, matching backtest.
- **signal_generator.py comprehensive rewrite**: 2026-08-01. All 20 audit bugs fixed in single rewrite. See "Fixed Issues" section above for full list.
- **Do NOT restart 同花顺 to fix Evolving issues**: killing and restarting 同花顺 requires the user to manually reopen the trading panel and re-login. This wastes time and creates more problems. Instead, just ask the user to confirm the trading panel is open, then retry Evolving calls directly.
- **Proxy errors during non-trading hours**: akshare's eastmoney API (fund_etf_hist_em) fails with ProxyError when the proxy is misconfigured or during off-hours. The signal_generator gracefully falls back to Sina API. Do not treat these as critical errors — they are expected in the China-mainland network environment.
- **Cron 14:30 run may buy at close**: the 14:30 trading monitor run executed a buy for 159516 at 15:00 (close auction). This is risky — close-auction fills can have unfavorable prices. Consider skipping buy signals on the last run (14:30) or only allowing sell/reduce/stop-loss on the final intraday check.
- **signal_generator.py v3.1 has 28 bugs found in audit** (2026-08-01): ALL 20 fixable bugs have been resolved in the comprehensive rewrite. See "Fixed Issues" section above. **Remaining backtest-vs-live inconsistencies are documented but not yet addressed** — they require careful strategy-level decisions.
- **When reviewing signal_generator.py code**: always check for fall-through logic (if without else/continue), missing persistence fields, and state that gets stuck. The most common pattern is: guard condition sets a "skip" action but doesn't prevent subsequent code from executing. After the 2026-08-01 rewrite, all 20 known bugs are fixed — but new code changes should be audited for the same patterns.
- **Backtest vs live inconsistencies (from audit)**: key differences between backtest_5etf_1m.py and signal_generator.py: (1) 趋势跟踪通道 — backtest has it (空仓>10天+价>MA20+红柱), signal_generator missing; (2) MACD calculation — different DIF/DEA initialization; (3) 止盈止损优先级 — backtest: 硬止盈→移动止盈→分级止损, signal_generator: 分级止损→移动止盈→硬止盈. These cause real divergence between backtest results and live signals.
- **Verification methodology lesson (2026-08-01)**: "Checking 5 times" means running the code and verifying LOGIC with data, not just checking syntax/import/no-crash. The user called out that 5 verification passes missed the T+0 scoring bugs because all 5 only checked "does it run without error" — nobody ran historical data to verify the scoring logic actually produced correct signals. **Always validate signal logic with historical data, not just code execution.** For T+0: run the scoring functions against 60 days of real K-line data and check: (1) no same-day buy+sell conflicts, (2) RSI conditions are actually enforced, (3) signal counts make sense (e.g. buy signals should be rare, not 20/day). For share calculation bugs: test with actual numbers (e.g. reduce_shares(30) on 1000 shares should give 700, not 0).
- **When doing comprehensive code rewrites**: backup the original file first (`cp file file.bak`), then write the complete new file. After writing, run: (1) syntax/lint check, (2) `python3 -c "import signal_generator"`, (3) `python3 signal_generator.py`, (4) `python3 backtest_5etf_1m.py` to verify unchanged results, (5) verify each specific fix with targeted checks.
- **10-pass audit methodology (2026-08-01)**: For comprehensive code review, do 10 structured passes: (1) read both files, line-by-line comparison; (2) fix critical issues, backtest; (3) boundary/extreme cases; (4) stop-loss/take-profit completeness; (5) entry/add logic; (6) state machine (liquidation/reduction); (7) data persistence; (8) parameter consistency; (9) full live run; (10) final backtest. User requires this level of rigor for trading systems.
- **reduce_shares unit test**: ALWAYS test reduce_shares with actual numbers after any code change. The formula `int(shares * pct / 100) * 100` is wrong for pct=30 (produces 30000 for 1000 shares). Correct formula: `int(shares * pct / 10000) * 100`. Test with: shares=1000 pct=30→700, shares=95600 pct=30→67000, shares=300 pct=30→210.
- **Floating-point comparison in financial code**: never use exact `>=` for percentage thresholds. `(1.15 - 1.0) / 1.0 * 100 = 14.99999...` fails `>= 15`. Always add a small tolerance: `>= 15 - 0.01`. This applies to ALL financial percentage comparisons (profit, loss, drawdown).
- **MACD calculation consistency**: signal_generator.py v3.1.2 uses the same SMA-init algorithm as backtest_5etf_1m.py. Key: DIF starts from i=0 (not i=slow), DEA initialized from difs[slow] (not SMA of first signal values). DO NOT change the MACD algorithm in one file without updating the other — they MUST produce identical results for the same input data.
- **6-pass deep audit methodology (2026-08-01)**: For thorough code review beyond bug-fixing, do 6 structured passes: (1) future-function / look-ahead bias; (2) overfitting / parameter hardcoding / survivorship bias; (3) code logic bugs (boundary / race / state machine); (4) backtest-vs-live consistency; (5) data fetch / computation / persistence robustness; (6) live execution risks + extreme scenarios. The user demands this level of rigor for trading systems — "排查6遍" means 6 REAL passes with findings, not 6 syntax checks.
- **prev_ms look-ahead bug pattern**: When backtesting, any variable that stores "previous day's" indicator must be updated at END-OF-DAY, not immediately after computing today's indicator. If updated mid-loop, the "previous" value is actually today's, making the condition redundant (e.g. Test抄底 `prev_ms==绿柱缩短` becomes `ms==绿柱缩短`). Always check: does the "previous X" variable get written before or after the logic that reads it?
- **empty_days per-day tracking**: Like below_ma20_count, empty_days must only increment once per calendar day (not per script run). Use a separate date field (empty_days_date) for deduplication. The signal_generator may run 4+ times per trading day (9:30/10:30/13:30/14:30).
- **add_count on confirm add**: After confirm-add (70%), signal_generator was resetting add_count to 0 (allowing 5 more dip-adds), while backtest incremented it by 1. This is a backtest-vs-live consistency issue that could lead to over-allocation. Always check: does the confirm-add reset or increment the add counter?
- **peak_price reset on partial sell**: When reducing shares (e.g. 30% stop-loss), `reduce_shares(pct, price)` now sets `self.peak_price = price` (current price as new peak). Previously was `self.peak_price = 0` which temporarily disabled hard stop 20% until the next run. The `price` parameter is required — pass the current price at the time of reduction.
- **Stop-loss priority order matters**: backtest processes stops in order: 均价止损10% → 硬止盈30% → 移动止盈 → 分级止损 → 硬止损20%. If 硬止盈30% and 分级止损 both trigger on the same day, the backtest takes 硬止盈 (full profit exit). signal_generator was collecting all stop_actions then picking by type priority, but the order of collection was wrong (分级止损 before 硬止盈). Fix: collect stop_actions in the same order as backtest.
- **realtime data KeyError guard**: When fetching realtime quotes, any ETF may be missing (halted, API error, network timeout). Always guard with `if code not in realtime: continue` before accessing `realtime[code]`. Same for all_tech. A missing ETF should produce a "持有(观望)" signal, not a crash.
- **intraday_t_once.py now imports from signal_generator** (not indicators.py): `from signal_generator import calc_rsi_wilder, calc_macd`. This ensures MACD calculation is identical to the main signal generator. The old indicators.py had a different MACD algorithm (SMA-warm with different DEA initialization) that produced different golden/death cross signals.
- **5-minute vs daily K-line for T+0**: intraday_t_once.py uses 5-minute K-line (more sensitive for intraday), signal_generator.py uses daily K-line (more stable for pre-market). This is intentional. But the SCORING FUNCTIONS must be identical — only the input data differs.
- **Backtest results are fast because they use daily K-line**: the backtest runs in seconds because it processes 726 days × 5 ETFs of daily data. This is correct — no minute-level data is needed for the strategy (which is based on daily MA20/RSI/MACD). T+0 is intraday-only and cannot be backtested with daily data.
- **Deleted files (2026-08-01)**: engine.py (replaced by signal_generator.py), indicators.py (MACD inconsistent with signal_generator; intraday_t_once.py now imports from signal_generator), backtest_159532_1y.py (single-ETF copy, redundant), signal_generator.py.bak2. Total files reduced from 11 to 7.
- **ths_client.py v3.1.3 rewrite (2026-08-01)**: removed ALL osascript/has_window/dismiss_alerts/ensure_ready/subprocess imports. Now a thin Evolving wrapper: buy/sell call `e.buy()`/`e.sell()` directly; revoke iterates `getEntrust()` + `e.revoke()` per item (no more AppleScript "全撤"/"撤买"/"撤卖" button clicking). File reduced from 227 to ~100 lines. The old version's ensure_ready() was the root cause of order failures — Evolving handles window activation internally, any extra osascript calls interfere.
- **Same-day buy+sell conflict detection**: after any T+0 scoring change, run historical data validation: for each ETF over 60 days, check that no day has both buy_score≥2 AND sell_score≥2 (unless MACD conditions are mutually exclusive). The old scoring had 9 same-day conflicts on 159532; after fix, 0 conflicts.
- **When user says 're-run backtest' or 'show me data', always actually run it**: never pull numbers from memory or previous session output. The user explicitly called out '别偷懒' — they want real execution, not cached results. Always run `python3 backtest_5etf_1m.py` and show the actual output.
- **Independent fund pools vs shared pool**: v3.1.4 used independent 44k per ETF (high drawdown 42.97%). v3.1.5 corrected to shared 220k pool + 44k per-ETF cap (drawdown 15.92%, return +89.32%). User explicitly corrected: "实盘资金不能共用，仓位管理你怎么考虑的？" — shared pool + position cap is the professional approach. Independent pools have low capital efficiency and artificially high drawdown.
- **Day-K backtest cannot simulate intraday operations**: trend profit sell (红柱缩短+破MA5) and MA5 active-share sell are intraday operations. When included in backtest, they produced 779 trades (50% of total) and inflated drawdown to 42.97% — but this is a measurement artifact, not a real strategy flaw. In live trading, these are same-day operations that can be reversed. Document this as a known backtest-vs-live difference.
- **Grid signals must NOT trigger new positions**: grid was designed for existing-position cost-averaging, not as an entry channel. When grid triggered build_phase==0, it created a shadow entry channel bypassing the 6-channel entry logic. Fixed v3.1.4: grid only applies when `pos.has_position`.
- **Forward-adjust threshold 0.70 is too loose for ETFs**: ETFs rarely drop >30% in one day, but they DO have 2-5% dividends. A 0.70 threshold misses these. Changed to 0.95 — catches 5%+ adjustments. This is a common pitfall in China A-share backtesting.
- **Stop-level recovery must be strict**: recovery from tiered stop (破MA20→30%→DIF<0→30%→清仓) should require MACD红柱, not just DIF>0. DIF>0 alone can recover while MACD is still in绿柱缩短 (green bar shrinking), which is a weak recovery signal. The stricter condition prevents premature re-entry after stop-loss.
- **Professional quant review methodology**: when reviewing a trading system, check: (1) future-function/look-ahead, (2) backtest-vs-live consistency (entry channels, stop-loss, take-profit), (3) fund pool model (shared pool + per-ETF cap is correct; independent pools are wrong), (4) forward-adjustment quality, (5) grid vs entry logic separation, (6) intraday operations in daily backtest. These are the structural issues that matter most — not just code bugs.
- **Entry channels must set base_price**: every entry channel must set `pos.base_price = price` alongside `pos.build_phase = 1`. Without this, the hard stop 30% condition (`price >= base_price * 1.30`) never triggers because base_price is None. This was a critical bug found by subagent audit — all 6 channels were missing it.
- **prev_macd_status for Test抄底**: the Test抄底 channel requires "前一日也是绿柱缩短" — this means the signal_generator must track `prev_macd_status` (updated end-of-day, persisted to portfolio.json). Without it, the channel only checks today's MACD, creating a look-ahead bias vs backtest's `prev_ms` check.
- **Liquidation must call reset_on_liquidate()**: when generating a "清仓" signal, the signal_generator must call `pos.reset_on_liquidate(today_str)` to clear shares/cost/stop_level/build_phase. Without this, the state machine stays stuck — next run still thinks there's a position.
- **Breakout entry 20-day high must exclude current day**: `all_klines[code][-21:-1]` includes the last bar which may be today. Use `[-22:-1]` with `len >= 21` check to exclude the current bar. Backtest correctly uses `range(idx-20, idx)` which excludes idx.
- **Subagent audit pattern for trading systems**: use delegate_task with 3 parallel subagents to audit: (1) signal_generator.py (future-function, consistency, state machine), (2) backtest_5etf_1m.py (indicators, entry logic, fund pool), (3) intraday_t_once.py + ths_client.py + shell scripts + portfolio.json. This catches cross-file inconsistencies that a single pass misses.
- **Evolving requires 同花顺 window activation (CRITICAL)**: Evolving's AppleScript calls ALL require 同花顺 to be the active (frontmost) window. If another app is focused, ALL Evolving calls return `status=False, info='unknown err'`. THSClient.__init__() now calls `self._activate()` which runs `osascript -e 'tell application "同花顺" to activate'` + 2s delay. This is NOT the same as the old has_window/ensure_ready anti-pattern — it's a single activation at init time, not per-call interference. Do NOT remove this activation step.
- **Wrapper library lesson**: when wrapping a third-party library (like Evolving), you MUST understand its runtime requirements before writing the wrapper. The Evolving+window-activation issue was missed because the wrapper was written without understanding that Evolving's AppleScript requires the target app to be frontmost. **Always test the raw library first, then wrap it — don't wrap blindly.** If a wrapper hides a required setup step, it's a bug in the wrapper, not the library.
- **THSClient v3.1.5 (2026-08-01)**: Added `_activate()` in `__init__` that calls osascript to activate 同花顺 window + 2s sleep. This is the ONLY osascript call — all other operations are direct Evolving pass-through. The 44k per-ETF cap is enforced in `buy()`. Query methods (get_holding_shares, get_account_info, get_entrust, get_closed_deals) have 1 retry on failure. **All orders are limit orders (price required, no market orders).** `buy()` calls `revokeAllBuyEntrust()` before placing; `sell()` calls `revokeAllSellEntrust()` before placing. `revoke_all()` uses `revokeAllEntrust()`, `revoke_all_buy()` uses `revokeAllBuyEntrust()`, `revoke_all_sell()` uses `revokeAllSellEntrust()` — all use Evolving batch methods, NOT iterating getEntrust().
- **Backtest yearly benchmark (post-refactoring, 2026-08-01)**: baseline changed due to cooldown+peak_price fixes. New 5yr: +75.34%/20.00%dd. Previous v3.1.5 benchmark: 2022 -6.94%/8.17%dd, 2023 +5.57%/10.21%dd, 2024 +17.28%/14.13%dd, 2025 +47.30%/6.36%dd, 2026YTD +24.83%/10.19%dd. No year with drawdown >15%.
- **Yearly backtest requires TRADE_END replacement**: when running yearly backtests, you must replace BOTH `TRADE_START` and `TRADE_END` in the script. The regex must account for whitespace: `re.sub(r'TRADE_END\s*=\s*".*?"', f'TRADE_END = "{end}"', src)`. A simple `re.sub(r'TRADE_END = ".*?"', ...)` will FAIL because the original has `TRADE_END   = "..."` (3 spaces before =). Each year starts fresh (no position carryover) — the backtest engine initializes all positions to empty at the start of the date range.
- **Evolving `e.revoke()` does NOT exist**: the correct single-order revoke method is `e.revokeEntrust(contractNo)`. Batch methods: `e.revokeAllEntrust()`, `e.revokeAllBuyEntrust()`, `e.revokeAllSellEntrust()`. Using `e.revoke()` will raise `AttributeError: 'Evolving' object has no attribute 'revoke'`. Always verify Evolving API methods by checking `dir(e)` before using them.
- **md2wechat requires IP whitelist**: WeChat API returns errcode 40164 if the calling IP is not in the whitelist. Login mp.weixin.qq.com → 设置 → 开发 → 基本配置 → IP白名单, add the machine's public IP. This is a prerequisite — md2wechat will fail with "invalid ip" until whitelisted.
- **md2wechat requires cover image**: WeChat draft API mandates a cover image. If the markdown has no images, md2wechat returns `MISSING_COVER_IMAGE`. Add at least one `![alt](path)` — the first image is used as cover.
- **Credentials never go to GitHub**: AppID/AppSecret/passwords/tokens are stored locally only (e.g. ~/hermes/scripts/.env.wechat). Never commit .env files or credential strings to any git repo.
- **Hermes Desktop app location**: after building, the app is at `~/.hermes/hermes-agent/apps/desktop/release/mac-arm64/Hermes.app`. Copy to `/Applications/` for Launchpad visibility: `cp -R ~/.hermes/hermes-agent/apps/desktop/release/mac-arm64/Hermes.app /Applications/`. The app won't appear in Launchpad/Finder until copied there.
- **md2wechat --comment on subscription accounts**: the `--comment` flag is accepted but has no effect on 未认证订阅号 (errcode 48001). Remind user to manually enable comments + original declaration in WeChat backend.
- **WeChat article style**: user explicitly rejected vague/hype content ("太虚了"). Articles must be practical and actionable: real code, real numbers, step-by-step instructions, real pitfalls. Include GitHub repo links. No marketing language.
- **Code quality audit pattern for trading systems**: use 3 parallel subagents to audit: (1) signal_generator.py — code duplication, exception handling, state management, performance; (2) backtest_5etf_1m.py — same + consistency with signal_generator + statistics correctness; (3) intraday_t_once.py + ths_client.py + sentiment_check.py — function duplication, exception handling, config sync. After audit, apply fixes in parallel, then run backtest to verify. Key: bug fixes that affect trading decisions WILL change backtest results — document before/after and explain which are bug fixes vs strategy modifications.
- **Bug fixes can change backtest results and that's OK**: when fixing bugs that affect trading decisions (cooldown period, peak_price on reduce, stop-level recovery), the backtest WILL produce different results. This is correct behavior — the old results contained bug-induced "false profits". Always document before/after. The user's principle "回测与实盘必须一致" means bugs that make backtest inconsistent with live are WORSE than bugs that inflate returns. Fix the bug first, accept the result change, then investigate if the new result requires strategy adjustment.
- **Backtest statistics correctness**: use (1) sample standard deviation (n-1 denominator) for Sharpe ratio, not population std dev; (2) total profit factor (total wins / total losses), not average win / average loss. These are the standard financial metrics — the old backtest used incorrect formulas.
- **Duplicate function definitions are the most dangerous silent drift risk**: when two files define the same function (e.g. t0_buy_score in both signal_generator.py and intraday_t_once.py), modifying one without the other causes algorithm divergence. The fix is always to import from the canonical source — never copy-paste algorithm code between files.
- **Trade-level PnL is only available on liquidation trades**: buy/sell/reduce trades in the backtest don't carry PnL (they're partial position changes). Only full liquidation (清仓) trades have meaningful PnL. When analyzing trade performance, group entries+exits into complete trade cycles (entry→exit), or analyze by liquidation type only. The strategy_analysis.py script handles this by categorizing liquidation types.
- **Strategy analysis methodology**: to find optimization opportunities, analyze: (1) entry channel distribution (which channels are used most — if one dominates, conditions may be too loose), (2) stop-loss/take-profit distribution (what % of trades end in profit vs loss), (3) stop-loss effectiveness (do tiered stops actually prevent full liquidation, or do positions still deteriorate to tier 3?), (4) trailing stop timing (8% vs 15% trigger frequency — if 8% dominates, the trail line may be too tight), (5) never-triggered protections (hard stop 20% never firing suggests the logic is wrong).
- **Optimization testing methodology (v3.2 lesson)**: test each optimization independently first, then combine only the positive ones. Use a dedicated test script (optimize_test.py) that runs the same backtest logic with feature flags. Test per-change: (1) probe RSI>40, (2) stop-level 1 reduce 50%, (3) avg-cost stop 15%, (4) 8% no trail change, (5) dip-add RSI<40. Some changes that look good in theory (e.g. wider stop-loss, more aggressive reduction) actually hurt returns — always let the data decide. The biggest wins came from fixing the most obvious problems (loose entry conditions, premature trailing stop, aggressive counter-trend adds) rather than adding new complexity.
- **v3.2 optimization results (2026-08-01, APPLIED)**: 3 changes applied: (1) probe RSI>30→40 (+2.85% return), (2) trailing stop 8% no longer sets trail line (+17.96% return), (3) dip-add requires RSI<40 (+18.08% return). Combined: return +75.34%→+92.91%, drawdown 20.00%→14.66%, Calmar 3.77→6.34. Two changes REJECTED: stop-level 1 reduce 50% (-2.57% return) and avg-cost stop 15% (-32.21% return).
- **Backtrader migration (2026-08-01)**: migrated from hand-written backtest_5etf_1m.py to backtest_bt.py using backtrader framework. Key differences: (1) backtrader uses `self.buy()/self.sell()` via broker, not manual cash management; (2) strategy only manages signal + position ratios, broker handles fund constraints; (3) NO per-ETF position cap — user explicitly said "仓位上限也不用管"; (4) backtrader uses next-day open price by default (more realistic), hand-written used same-day close; (5) backtrader's TradeAnalyzer only counts complete open-close cycles (25 trades), not individual entries/exits (894 in hand-written); (6) use backtrader Analyzers (SharpeRatio, DrawDown, TradeAnalyzer) instead of manual statistics; (7) annual return calculation uses calendar days / 365, not trading days / 242. **Backtrader is more accurate** — same-day close execution in hand-written backtest is a "future function" (can't actually buy at the signal price). Next-day open is realistic. The ~19% return difference is the "illusion" value.
- **Backtrader custom indicator pitfalls**: (1) `self.data.close.get(size=N)` does NOT work in custom indicators — use `[self.data[-i] for i in range(N-1, -1, -1)]` instead; (2) `self.data.high`/`self.data.low` ARE accessible in custom indicators initialized with `d` (the full data feed), NOT `d.close` — use `AOIndicator(d)` not `AOIndicator(d.close)` to access OHLC; (3) `len(self.data)` gives the number of available bars, not `len(self.data.close)`; (4) **MACDStatus buffer overflow**: when computing `closes = [self.data[-i] for i in range(size-1, -1, -1)]`, if `size` is larger than the actual buffer, it throws IndexError. Fix: only fetch the minimum needed bars: `closes = [self.data[-i] for i in range(min(size, min_needed + self.p.fast) - 1, -1, -1)]`. This is critical for short-period backtests with warmup data using `fromdate`.
- **Backtrader warmup data for short-period backtests**: when running backtests for short periods (1-6 months), you MUST provide warmup data from at least 1 year before the target period. Use `PandasData(dataname=sub, name=code, fromdate=start_ts, todate=end_ts)` where `sub` includes data from the warmup period, but `fromdate` limits execution to the target period. Without warmup, indicators (MA60, ADX, MACD) won't have enough data to compute and will crash with IndexError.
- **Backtrader multi-period backtest results (v3.3, 2026-08-01, with warmup, backtest-live consistent)**:
  | Period | Total Return | Annual | Max DD | Calmar | Sharpe | Trades | Win Rate | P/L Ratio |
  |--------|-------------|--------|--------|--------|--------|--------|----------|-----------|
  | 5yr | +37.49% | +6.59% | 5.97% | 1.10 | 0.89 | 62 | 48.4% | 3.32 |
  | 3yr | +37.49% | +11.24% | 5.97% | 1.88 | 1.34 | 62 | 48.4% | 3.32 |
  | 2yr | +37.49% | +17.38% | 5.97% | 2.91 | 1.77 | 62 | 48.4% | 3.32 |
  | 1yr | +19.16% | +19.45% | 6.00% | 3.24 | 1.49 | 51 | 41.2% | 3.37 |
  | 6mo | +21.01% | +48.52% | 3.77% | 12.87 | 3.23 | 21 | 52.4% | 4.55 |
  | 3mo | -0.91% | -3.78% | 5.78% | -0.65 | -0.59 | 11 | 18.2% | 2.11 |
  Note: 3yr/2yr/5yr have identical results because all profits came from 2023-08 onwards. First 2 years had no trades. The +37% return is the REAL result after fixing position ratio base — the previous +93% was inflated 5x by using total fund instead of per-ETF fund for position sizing.
- **Backtrader COC mode**: `set_coc(True)` uses same-day close price for execution (less realistic but matches hand-written backtest). `set_coc(False)` uses next-day open (more realistic). The hand-written backtest used same-day close, so COC=True gives closer results, but COC=False is more realistic for live trading.
- **Backtest vs live consistency audit methodology**: when the user asks "is the backtest consistent with live?" or "check for overfitting", do a systematic comparison: (1) Read both backtest_bt.py and signal_generator.py completely; (2) Compare entry channels one by one — check conditions, position ratios, AND the base amount (total fund vs per-ETF fund); (3) Compare exit logic — stop-loss levels, take-profit, trailing stop; (4) Compare indicator calculations — AO, RSI, MACD algorithms; (5) Compare config constants — COOLDOWN_DAYS, DEAD_RATIO, thresholds; (6) Check for live-only features not in backtest (trend profit sell, MA5 active share sell, T+0, grid); (7) Check for overfitting — does the strategy only work on one ETF? Does it only work in one time period? Are magic numbers hardcoded? (8) Present findings as a priority table (P0/P1/P2) with specific values showing the inconsistency. The user is a professional quant — they want precise, actionable findings, not vague descriptions.
- **Backtest position ratio base — FIXED v3.3**: backtest_bt.py now uses `_buy(data, pct)` where `etf_fund = broker.getvalue() * 0.20` (per-ETF allocation = 20% of total fund) and `target_value = etf_fund * pct`. This matches live: 4.4万×30%=1.32万 per entry. The old approach (`broker.getvalue() * pct`) gave 22万×30%=6.6万 — 5x larger than live. **Always verify position ratio base matches live before trusting backtest results.**
- **Overfitting red flags to check**: (1) any single ETF accounts for >50% of trades; (2) the strategy has zero trades for extended periods (e.g. 3 years) — this means entry conditions are too strict and only work in specific market regimes; (3) all profits come from one time period — the strategy is regime-dependent; (4) more than 5 hardcoded magic numbers without sensitivity analysis.

## Code Quality Refactoring (2026-08-01) — All Files

**22 engineering fixes across 5 files.** Strategy parameters unchanged. Two bug fixes (cooldown period, peak_price on reduce) caused backtest results to change — this is correct behavior (old data included bug-induced "false profits").

### Backtest impact (5-year, 1207 trading days)
| Metric | Before | After | Reason |
|--------|--------|-------|--------|
| Total return | +91.10% | +75.34% | Cooldown 1→2 days (fewer trades); peak_price=price on reduce (hard stop 20% now active after partial sell) |
| Max drawdown | 19.11% | 20.00% | More protective exits triggered |
| Sharpe | 1.16 | 1.02 | Sample std dev (was population std dev) |
| Profit factor | 6.41 | 2.80 | Total win/loss ratio (was average win/avg loss) |
| Trades | 985 | 894 | Fewer entries due to longer cooldown |

**These changes are CORRECT bug fixes, not strategy modifications.** The old baseline contained bugs that inflated returns.

### signal_generator.py (14 fixes)

**14 engineering fixes applied without changing strategy behavior.** Key structural changes:

### Module-level constants extracted
- `CODE_MAP` — ETF code to Sina code mapping (was defined 3 times locally)
- `INVERTED_WEIGHTS` — inverted pyramid weights `[0.20, 0.25, 0.30, 0.35, 0.40]` (was defined 2 times locally)

### New PositionInfo methods
- `_enter_position(date_str, price, channel_name)` — 6-channel entry initialization (was 7 lines repeated 6 times). Returns `(action, position_ratio, trade_type)`.
- `_get_daily_log(date_str)` — daily_trade_log access with default value + type check (was 3 lines repeated 3 times)
- `to_dict(today_str)` — serialize state to dict (was 20+ lines of field-by-field assignment)
- `from_dict(data)` — restore state from dict (was 16 lines of field-by-field assignment)

### New module-level helper
- `_safe_float(val, default=0.0)` — safe float conversion handling `"-"`, `None`, empty string (was inline `if x != "-" else 0` repeated 10 times in akshare fallback)

### Bug fixes (P0)
- `reset_on_liquidate()` now sets `self.base_price = None` (was missing, stale base_price after liquidation)
- `reduce_shares(pct, price=0.0)` now sets `self.peak_price = price` instead of `0` (was temporarily disabling hard stop 20%)
- Sina K-line parsing: `float(parts[3])`/`float(parts[2])` wrapped in `try/except ValueError` (was crashing on malformed data)
- `fetch_realtime_quotes()` call wrapped in `try/except` (was crashing entire signal generator on API failure)
- Portfolio.json dual-path sync: needs warning log on sync failure (pending)

### Code removal
- `fetch_klines_120min()` deleted — was 1-line wrapper around `fetch_klines_daily`; callers now use `fetch_klines_daily` directly
- `calc_5min_vol_ratio()` deleted — was never called (fetch_klines_5min always returns empty)
- `fetch_klines_5min()` retained with `# TODO: 未来启用5分钟量比`

### Robustness improvement (P2)
- Summary output now uses `trade_type` field for categorization instead of Chinese string matching on `action` (was fragile)

### Refactoring pattern for similar codebases
When refactoring trading signal generators without changing behavior:
1. **Extract repeated dict literals to module constants** — reduces typo risk and maintenance burden
2. **Extract repeated initialization patterns to methods** — 6-channel entry was 7 lines x 6 = 42 lines to 6 one-liners
3. **Use to_dict/from_dict for serialization** — 20+ lines to 1 line, and adding new fields only requires updating one place
4. **Wrap external API calls in try/except at the call site** — do not let one API failure crash the entire signal generator
5. **Use trade_type enum for categorization, not string matching** — Chinese action strings are fragile for programmatic use

### backtest_5etf_1m.py (6 fixes)

1. **COOLDOWN_DAYS=2** (was 1, inconsistent with signal_generator's COOLDOWN_DAYS=2). This is the main cause of backtest result change — fewer re-entries after liquidation.
2. **peak_price=price on reduce** (was `pos.peak_price = 0`). Hard stop 20% now works immediately after partial sell, instead of being temporarily disabled.
3. **`do_liquidate()` helper** — unified 5-copy清仓 logic (pnl calculation + win/loss stats + cash recovery + full_liquidate + stop_cooldown).
4. **`init_on_entry()` method** — unified 6-copy entry initialization (base/first_price/build_phase/bought_today/add_count/empty_days).
5. **Sharpe ratio uses sample std dev** (was population std dev — `len-1` denominator for unbiased estimate).
6. **Profit factor uses total win/total loss** (was avg_win/avg_loss — total ratio is the standard metric).
7. **`entry_dates` dead code removed** (was set but never read).
8. **`pos.peak` redundant field removed** (was tracked alongside `pos.peak_price` but never used for decisions).
9. **`fetch_daily()` ValueError protection** — `try/except` on `float(d["open"])` etc.
10. **`full_liquidate()` uses timedelta** — cooldown calculated from date_str (was just `cooldown_until = date_str`).
11. **Function names renamed** — `ma`→`calc_ma`, `rsi`→`calc_rsi_wilder`, `macd`→`calc_macd`, `ao`→`calc_ao` (matching signal_generator naming).

### intraday_t_once.py (2 fixes)

1. **t0_buy_score/t0_sell_score imported from signal_generator** — deleted 30-line duplicate definitions. Was `from signal_generator import calc_rsi_wilder, calc_macd` only; now also imports `t0_buy_score, t0_sell_score, CODE_MAP`. This eliminates the most dangerous silent drift risk — the two files had identical code but modifying one without the other would cause algorithm divergence.
2. **CODES dict replaced with CODE_MAP import** — eliminated third copy of the same mapping.

### ths_client.py (3 fixes)

1. **`_query_with_retry()` method** — unified 3 identical query methods (get_holding_shares/get_account_info/get_entrust) with configurable retries and delay. Replaced copy-paste retry logic.
2. **`logging` module** — all operations now log at appropriate levels (INFO for successful trades, WARNING for failed queries/revocations, ERROR for exceptions). Was `except: pass` — silent swallowing made trading failures impossible to diagnose.
3. **Buy/sell try/except with logging** — `buy()` and `sell()` now catch exceptions and return `(False, str(e))` instead of crashing.

### sentiment_check.py (4 fixes)

1. **Bare `except` replaced with `except Exception as e`** — bare except catches SystemExit/KeyboardInterrupt, which is wrong for a long-running process.
2. **`n.get("title", "")` with KeyError protection** — was `n["title"]` which crashes on malformed API response.
3. **`logging` module** — fetch failures now logged instead of silently returning empty list.
4. **Magic numbers extracted to constants** — `NEG_THRESHOLD_WARN=3`, `NEG_THRESHOLD_BLOCK=6`.

### Key lesson: bug fixes can change backtest results

When fixing bugs that affect trading decisions (cooldown period, peak_price), the backtest WILL produce different results. This is correct — the old results contained bug-induced "false profits". Always document the before/after and explain which changes are bug fixes vs strategy modifications. The user explicitly requires: "回测与实盘必须一致" — bugs that make backtest inconsistent with live are worse than bugs that inflate returns.
