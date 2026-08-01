# Signal Generator v3.1 Audit — 2026-08-01

## Status: ALL 20 FIXABLE BUGS RESOLVED

Comprehensive rewrite of signal_generator.py completed 2026-08-01.
All 20 bugs below have been fixed. Backtest unchanged: 3yr +80.17%, drawdown 15.34%.

## Fixed Bugs

| # | Bug | Severity | Fix |
|---|-----|----------|-----|
| 1 | urllib.request not imported at top level | 🔴 | Added `import urllib.request` at line 19 |
| 2 | AO filter bypassed — `if not ao_rising` sets action but doesn't prevent fall-through | 🔴 | Changed to `if/elif` structure |
| 3 | 补仓 dead code inside build_phase==0 block | 🔴 | Moved to build_phase==1 branch |
| 4 | Grid frozen never unfreezes | 🔴 | Added `if grid_frozen and price > MA5: grid_frozen = False` |
| 5 | Hard stop 20% is `pass`-ed | 🔴 | Implemented: `if price <= peak_price * 0.80` |
| 6 | build_phase==1 confirm unreachable after state restoration | 🔴 | Restructured: build_phase==1 now under correct branch |
| 7 | add_count not persisted | 🟡 | Added to save/load |
| 8 | ma5_touch_count not persisted | 🟡 | Added to save/load |
| 9 | prev_rsi not persisted | 🟡 | Added to save/load |
| 10 | daily_trade_log not persisted | 🟡 | Today's log saved/loaded |
| 11 | last_grid_trigger not reset across days | 🟡 | Day-check on restore |
| 12 | liquidate_dates/cooldown_until never written | 🟡 | `reset_on_liquidate()` sets cooldown 3 days |
| 13 | stop_level=3 never set | 🟡 | Tier 3 now sets `pos.stop_level = 3` |
| 14 | build_phase never resets after liquidation | 🔴 | `reset_on_liquidate()` resets to 0 |
| 15 | dead_shares/active_shares not updated on reduce | 🟡 | Added `update_dead_active()` method |
| 16 | RSI抄底 missing RSI≤80 condition | 🟡 | Added `(t["rsi"] is None or t["rsi"] <= 80)` |
| 17 | AO assigned twice | 🟢 | Removed duplicate |
| 18 | 通道2 dead code | 🟢 | Removed |
| 19 | daily_trade_log type safety | 🟡 | Added `isinstance(log, dict)` check |
| 20 | portfolio.json dual-path sync | 🟡 | Auto-copy to `~/.hermes/scripts/` |

## Remaining Backtest-vs-Live Inconsistencies (not fixed)

These require strategy-level decisions, not code fixes:

1. **趋势跟踪通道** — backtest has it (空仓>10天+价>MA20+红柱), signal_generator missing
2. **MACD calculation** — different DIF/DEA initialization between backtest and signal_generator
3. **止盈止损优先级** — backtest: 硬止盈→移动止盈→分级止损, signal_generator: 分级止损→移动止盈→硬止盈
4. **T+0 buy/sell share t0_count** — intentional design choice, not a bug
5. **t0_sell_score receives prev_rsi but never uses it** — low priority, dead parameter

## Key Code Patterns to Watch

1. **Fall-through logic**: `if condition: action = "skip"` without `else/continue` — subsequent code still executes
2. **Missing persistence**: any PositionInfo field not in `_signal_state` save/load resets on restart
3. **Stuck state**: stop_level, build_phase, grid_frozen can create dead-end states without reset logic
4. **Branch misplacement**: code inside wrong if/elif branch (e.g. build_phase==1 logic inside build_phase==0)

## New Methods Added to PositionInfo

- `update_dead_active()`: Recalculates dead_shares/active_shares after any share change
- `reset_on_liquidate(date_str)`: Resets all state on liquidation, sets 1-day cooldown. Now also resets `base_price = None`.
- `reduce_shares(pct, price=0.0)`: Reduces position by percentage, updates dead/active shares. `price` param sets peak_price to current price (not 0) so hard stop 20% remains effective.
- `_enter_position(date_str, price, channel_name)`: Unified 6-channel entry initialization — sets record_buy, build_phase=1, build_first_price, base_price, empty_days=0. Returns (action, position_ratio, trade_type).
- `_get_daily_log(date_str)`: Centralized daily_trade_log access with default value + isinstance check (was repeated 3 times).
- `to_dict(today_str)`: Serialize state to dict for persistence (was 20+ lines of field-by-field assignment).
- `from_dict(data)`: Restore state from dict (was 16 lines of field-by-field assignment).

## Module-level Constants (extracted 2026-08-01)

- `CODE_MAP`: ETF code to Sina code mapping (was defined 3 times locally in fetch_realtime_quotes, fetch_klines_daily, and intraday_t_once)
- `INVERTED_WEIGHTS`: `[0.20, 0.25, 0.30, 0.35, 0.40]` (was defined 2 times in evaluate_grid_signals and compute_grid_table)

## Module-level Helpers (added 2026-08-01)

- `_safe_float(val, default=0.0)`: Safe float conversion handling `"-"`, `None`, empty string. Replaces inline `if x != "-" else 0` patterns in akshare fallback parsing.

## Deleted Functions (2026-08-01)

- `fetch_klines_120min()`: Was a 1-line wrapper around `fetch_klines_daily`. Callers now use `fetch_klines_daily` directly.
- `calc_5min_vol_ratio()`: Was never called (fetch_klines_5min always returns empty). Deleted.

## Code Quality Refactoring (2026-08-01, 14 fixes)

P0 bug fixes:
- `reset_on_liquidate()` now sets `self.base_price = None` (was missing)
- `reduce_shares(pct, price=0.0)` sets `self.peak_price = price` instead of `0` (was disabling hard stop 20%)
- Sina K-line `float()` wrapped in `try/except ValueError` (was crashing on malformed data)
- `fetch_realtime_quotes()` call wrapped in `try/except` (was crashing entire signal generator)
- `price == 0` guard added in signal loop (handles API failure fallback)

P1 structural improvements:
- `CODE_MAP` / `INVERTED_WEIGHTS` extracted to module constants
- `_enter_position()` replaces 6x7-line repeated entry initialization
- `to_dict()` / `from_dict()` replaces 20+ line field-by-field serialization
- `_get_daily_log()` replaces 3x repeated log access pattern
- `fetch_klines_120min` deleted (1-line wrapper)
- `calc_5min_vol_ratio` deleted (dead code)
- `fetch_klines_5min` annotated with TODO

P2 robustness:
- Summary output uses `trade_type` field instead of Chinese string matching
