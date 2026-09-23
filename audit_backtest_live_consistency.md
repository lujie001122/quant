# 回测 vs 实盘 一致性审计报告

## 1. 指标计算一致性

### 1.1 RSI (Wilder RSI)

| 来源 | 文件:行号 | 关键逻辑 |
|---|---|---|
| 回测 | backtest_bt.py:149-173 (WilderRSI类) | SMA初始化→递归Wilder平滑 |
| 实盘 | factors/indicators.py:35-52 (calc_rsi_wilder) | SMA初始化→递归Wilder平滑 |
| 实盘(副本) | factors/rsi.py:11-37 (calc_rsi) | SMA初始化→递归Wilder平滑 |

**结论:** ✅ **一致** — 两个实现使用完全相同的SMA首次初始化 + (n-1)/n递归平滑 + round(2)。但存在函数重复定义(indicators.py和rsi.py)，属维护异味。

---

### 1.2 MACD

| 来源 | 文件:行号 | 初始值 | 递推公式 | 状态判定逻辑 |
|---|---|---|---|---|
| 回测 | backtest_bt.py:176-243 (MACDStatus类) | SMA(12)/SMA(26)/SMA(9) | 2/(N+1) EMA | 金叉/死叉/红柱放大缩短/绿柱放大缩短 |
| 实盘 | factors/macd.py:11-72 (calc_macd) | SMA(12)/SMA(26)/SMA(9) | 2/(N+1) EMA | 金叉/死叉/红柱放大缩短/绿柱放大缩短 |

**结论:** ✅ **一致** — 数学公式完全一致，状态机判定条件一致(回测用int编码0-6，实盘用string，映射1:1)。

---

### 1.3 AO (Awesome Oscillator)

| 来源 | 文件:行号 | 关键逻辑 |
|---|---|---|
| 回测 | backtest_bt.py:246-262 (AOIndicator类) | (H+L)/2 → SMA5 - SMA34 |
| 实盘 | factors/ao.py:11-36 (calc_ao) | (H+L)/2 → SMA5 - SMA34 |

**结论:** ✅ **一致** — 均使用(最高+最低)/2作为中价，SMA5 - SMA34。

---

## 2. 移动止盈参数一致性

### 🔴 [高风险] backtest_bt.py:561-585 vs position_info.py:206-220

| 差异项 | 回测 _calc_trail_params | 实盘 update_trailing_stop |
|---|---|---|
| trail_mode支持 | fixed / e1(ATR自适应) / e2(ATR自适应2) | **仅 fixed 模式** |
| trail_pct来源 | `self.p.trail_pct` (策略参数, 可被CLI覆盖) | `_config.get('trail_pct', 0.12)` (配置固定) |
| ATR自适应 | e1: trail_mult∈{0.93,0.95,0.97}, e2: {0.88,0.92,0.95} | **无ATR自适应逻辑** |
| 方法签名 | `_calc_trail_params(price, avg, atr_pct)` — 传入ATR | `update_trailing_stop(price)` — 无ATR参数 |

**影响:** 若回测使用 `--trail=e1` 或 `--trail=e2` (ATR自适应模式)，实盘无法复现相同止盈行为。回测结果与实盘不可比。

**修复:** 若实盘需支持e1/e2，需改造 `position_info.update_trailing_stop` 增加atr_pct参数和ATR自适应逻辑；否则回测应固定 `--trail=fixed`。

---

### ⚠ [中风险] 属性命名混乱

**文件:** position_info.py:214-220, 同时又对应 backtest_bt.py:591-595

| 回测命名 | position_info命名 | 实际含义 |
|---|---|---|
| `reached_activation` | `reached_8pct` | activation = trail_pct + 1% (trail_pct=0.12时=13%, 非8%) |
| `reached_lockin` | `reached_15pct` | lockin = trail_pct + 8% (trail_pct=0.12时=20%, 非15%) |

`_build_pos_and_tech` (backtest_bt.py:1761-1762) 做了映射:
```python
pos.reached_8pct = ps["reached_activation"]
pos.reached_15pct = ps["reached_lockin"]
```

**影响:** 功能上正确(数值对应)，但`reached_8pct`/`reached_15pct`是硬编码误导名。当 trail_pct != 0.12 时，属性名与实际阈值脱节，易导致维护者误读代码。

---

## 3. enhanced_trend 参数传递差异

### ⚠ [中风险] signal_generator 缺失 enhanced_trend 参数

| 来源 | 文件:行号 | 参数传递 |
|---|---|---|
| 回测 | backtest_bt.py:727 | `check_stop_loss(... , enhanced_trend=self.p.enhanced_trend)` |
| 实盘 | signal_generator.py:57-60 | `def evaluate_stop(pos, t, price, today_str):` — **缺失 enhanced_trend 参数** |

**影响:** 实盘signal_generator中，evaluate_stop调用`_rm_check_stop(pos, t, price, today_str)`时未传enhanced_trend，默认使用`False`。即使config.yaml配置了增强趋势止盈，实盘也不会生效。回测中 `--enhanced-trend` 命令行参数改变的止盈行为无法在实盘复现。

**修复:** signal_generator.py:57 改为 `def evaluate_stop(pos, t, price, today_str, enhanced_trend=False):` 并传递参数。

---

## 4. 状态恢复差异

### ✅ 低风险 — 字段差异由模式隔离

| 回测 _full_liquidate_state | position_info reset_on_liquidate |
|---|---|
| 独有: `stop_cooldown`, `trend_sell_today`, `trend_sell_cooling_until`, `ma5_sell_today`, `ma5_sell_cooling_until`, `confirm_batch_count/date`, `pyramid_count`, `market_reduced`, `market_drawdown_level/date` | 独有: `grid_frozen`, `last_grid_trigger`, `last_grid_trigger_date`, `liquidate_dates` |

**影响:** 字段差异源于模式不同(backtest_bt管理多种交易模式状态，position_info侧重网格交易)。backtest使用独立`ps dict`管理状态，不与position_info完全对齐，但通过`_build_pos_and_tech`/`_sync_pos_to_ps`桥接。**功能上正确，但存在未来状态漂移风险。**

---

## 5. 回测收盘价±滑点 vs 实盘限价单差异

### ✅ 已知差异 (backtest_bt.py:15-28 已文档化)

| 差异项 | 回测 | 实盘 |
|---|---|---|
| 成交价 | 下一Bar开盘价(coc=False) ± SLIPPAGE_PCT(0.1%) | 同花顺限价单实际成交价 |
| 撮合模式 | backtrader broker即时撮合,无挂单检查 | 委托→撤单→重挂完整流程 |
| Intent去重 | 无 intent 文件去重 | order_manager 维护 orders/intent/*.json |
| 信号链路 | 直接调用策略模块 | signal_generator → order_manager → executor |

**结论:** 这是架构性差异，backtest_bt.py 文档已标注。不属于本次可修复的范围，但使用回测结果时应考虑 0.1%滑点与实际成交价差异。

---

## 汇总

| # | 差异 | 风险等级 | 影响 | 修复建议 |
|---|---|---|---|---|
| 1 | 移动止盈 trail_mode仅回测支持e1/e2，实盘update_trailing_stop仅固定模式 | 🔴 **高** | trail_mode非fixed时回测结果与实盘不可比 | 回测固定`--trail=fixed`，或实盘增加ATR自适应逻辑 |
| 2 | signal_generator.evaluate_stop未传递enhanced_trend参数 | ⚠ **中** | `--enhanced-trend`回测行为无法在实盘复现 | signal_generator.py:57增加enhanced_trend参数 |
| 3 | position_info属性名reached_8pct/15pct语义偏差 | ⚠ **中** | 维护风险，trail_pct≠0.12时代码误读 | 重命名为reached_activation/reached_lockin |
| 4 | 状态恢复字段不一致(ps dict vs PositionInfo) | ⚠ **低** | 已通过桥接同步，但存在未来漂移风险 | 统一使用PositionInfo作为规范状态模型 |
| 5 | 回测成交模型与实盘架构性差异 | ✅ **已知** | 已文档化，不可消除 | 回测结果仅用0.1%滑点近似 |