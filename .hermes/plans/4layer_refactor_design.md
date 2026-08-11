# 4层架构重构方案（纯设计，不修改代码）

> 状态：待确认  
> 日期：2026-08-11

---

## 一、当前架构问题

| 问题 | 具体表现 |
|------|----------|
| **行情函数散落** | `fetch_realtime_quotes` / `fetch_klines_daily` / `fetch_klines_5min` 在 `signal_generator.py` 和 `trade.py` 中各有一份（`trade.py` 有 `_fetch_klines_5min` 重复实现） |
| **指标函数散落** | `calc_rsi_wilder` 在 `signal_generator.py`、`backtest_t0_trigger_rate.py`、`rotation.py` 各有一份。`calc_macd` 在 `signal_generator.py`、`backtest_t0_trigger_rate.py`、`rotation.py` 各有一份，且实现不同（EMA vs SMA-init） |
| **策略逻辑独立重复** | `backtest_bt.py` 的 `ETFStrategy.next()` 中完整重写了止损/止盈/建仓6通道逻辑，与 `signal_generator.py` 的 `generate_signals()` 几乎一致但独立维护 |
| **下单层混入策略** | `trade.py` 有自己的 `_calc_atr()` 和 `_estimate_atr()`（做T的ATR计算），属于策略逻辑混入下单层 |
| **signal_generator 职责过重** | 2229行，同时承担行情获取、指标计算、策略判定、信号执行4个职责 |

---

## 二、4层架构总览

```
┌─────────────────────────────────────────────────────────────┐
│  signal_generator.py  (调用层)                               │
│  - 仅负责: 组装数据 → 调用策略 → 输出结果 + 调用 trade.py   │
│  - 保留: generate_signals(), execute_signals(), main()      │
├─────────────────────────────────────────────────────────────┤
│                    ↓  import                                │
├────────────────────────────┬────────────────────────────────┤
│  strategy.py (策略层)      │  market_data.py (行情层)        │
│  - PositionInfo 类         │  - fetch_realtime_quotes()     │
│  - 6通道建仓逻辑           │  - fetch_klines_daily()        │
│  - 止损/止盈/移动止盈      │  - fetch_klines_5min()         │
│  - 做T评分 (t0_buy/sell)   │  - calc_ma / calc_rsi_wilder   │
│  - 网格信号/重置           │  - calc_atr / calc_macd        │
│  - 分批建仓/补仓/确认      │  - calc_ao / calc_vol_ratio    │
│  - 防御盾/空仓期/冷静期    │  - calc_5min_indicators()      │
│                            │  - dynamic_spacing()            │
├────────────────────────────┴────────────────────────────────┤
│                    ↓  import                                │
├────────────────────────────┬────────────────────────────────┤
│  backtest_bt.py (回测层)   │  trade.py (下单层)              │
│  - backtrader 引擎         │  - EvolvingSim 封装            │
│  - 资金管理 / 仓位计算     │  - do_buy / do_sell            │
│  - 手续费 / 滑点           │  - do_t0_buy / do_t0_sell      │
│  - 指标转 backtrader       │  - do_revoke / sync            │
│    Indicator 适配器        │  - 持仓增量确认               │
│  - 策略逻辑 import strategy│  - 铁律检查 (限价方向等)       │
│    不再独立重复实现        │  - 不包含任何策略/行情逻辑     │
└────────────────────────────┴────────────────────────────────┘
```

---

## 三、各层详细设计

### 3.1 行情层：`market_data.py`（新建）

**职责**：行情数据获取 + 技术指标计算。所有脚本只 import market_data，不各自实现行情拉取和指标计算。

**抽取来源**：`signal_generator.py` 第 50-450 行

| 函数 | 行号 | 说明 |
|------|------|------|
| `_akshare_retry(func, *args, retries=3, initial_delay=1)` | 54-64 | akshare 调用重试封装 |
| `_safe_float(val, default=0.0)` | 67-74 | 安全转换 akshare 返回值 |
| `fetch_realtime_quotes()` | 77-140 | 实时行情（Sina 主源→akshare 备用） |
| `fetch_klines_daily(code, start, end, adjust)` | 143-212 | 日K线（腾讯前复权→akshare 降级） |
| `fetch_klines_5min(code, today=None)` | 215-272 | 5分钟K线（腾讯接口） |
| `calc_ma(closes, period)` | 279-282 | 简单移动平均 |
| `calc_rsi_wilder(closes, period=14)` | 284-299 | Wilder RSI |
| `calc_atr(klines, period=14)` | 301-315 | 平均真实波幅 |
| `calc_macd(closes, fast=12, slow=26, sig=9)` | 317-352 | SMA-init MACD |
| `calc_ao(highs, lows, short=5, long=34)` | 355-366 | Awesome Oscillator |
| `calc_vol_ratio_120min(volumes, period=20)` | 368-372 | 120分钟量比 |
| `calc_5min_indicators(klines_5min)` | 375-443 | 5分钟分时指标聚合 |
| `dynamic_spacing(atr, price, base_spacing)` | 445-450 | 动态间距计算 |

**依赖**：
```python
import akshare as ak
import json
import time
import urllib.request
from datetime import datetime
from tracker import get_code_map, get_etfs_config
```

**被依赖关系**（谁 import 它）：
- `signal_generator.py` → 用于获取行情和指标
- `strategy.py` → 用于指标计算（做T评分、网格等需要ATR/RSI）
- `backtest_bt.py` → 用于获取历史K线（替代 `fetch_daily`）
- `trade.py` → 用于 `fetch_klines_5min` + `calc_atr`（替代 `_fetch_klines_5min` + `_calc_atr`）
- `rotation.py` → 替代自己的 `fetch_klines` + `calc_rsi` + `calc_macd` + `calc_ma` + `calc_atr`
- `backtest_t0_trigger_rate.py` → 替代自己的 `calc_rsi_wilder` + `calc_macd` + `calc_atr`
- `intraday_t_once.py` → 已经依赖 `signal_generator`，改为依赖 `market_data`

---

### 3.2 策略层：`strategy.py`（新建）

**职责**：所有策略判断逻辑——建仓信号、止损止盈、做T评分、网格信号、仓位管理。纯计算，不涉及行情获取和下单。

**抽取来源**：`signal_generator.py` 第 450-808 行 + `generate_signals()` 中的策略判定逻辑

#### 3.2.1 类和函数

| 类/函数 | 来源行号 | 说明 |
|---------|----------|------|
| `PositionInfo` 类 | 457-680 | 持仓状态管理（含所有状态字段和方法） |
| `is_auction_time(now=None)` | 682-687 | 集合竞价时段判断 |
| `t0_buy_score(rsi_5min, macd_5min_status, vol_ratio_5min)` | 690-716 | 做T买入评分 |
| `t0_sell_score(rsi_5min, macd_5min_status, vol_ratio_5min)` | 719-746 | 做T卖出评分 |
| `evaluate_grid_signals(price, base_price, spacing, grid_frozen)` | 749-770 | 网格触达判定 |
| `check_grid_reset(price, base_price, spacing, date_str, last_reset_date)` | 773-788 | 网格基准重置 |
| `compute_grid_table(base_price, spacing, fund)` | 791-807 | 网格表计算 |

#### 3.2.2 新增策略函数（从 `generate_signals()` 中拆出）

这些函数目前是 `generate_signals()` 内部的 inline 逻辑，需要拆成独立函数：

| 新增函数 | 职责 | 对应原代码行号 |
|----------|------|----------------|
| `evaluate_stop_signals(pos, price, t, today_str)` | 止损/止盈/分级减仓/趋势止盈/破MA5判定 | 994-1082 |
| `evaluate_entry_signals(pos, code, price, t, all_klines, today_str, atr_pct, defense_weak, _position_capped)` | 6通道建仓+补仓+确认加仓 | 1294-1418 |
| `evaluate_t0_signals(pos, t, today_str, is_market_hours, in_auction, atr_pct)` | 做T信号判定（含MA20斜率过滤） | 1084-1136 |
| `evaluate_grid_action(pos, price, base, spacing, today_str, atr_pct, _position_capped)` | 网格买入/卖出 action + 熔断 | 1261-1291 |

**`evaluate_stop_signals` 详细逻辑**：
- 保本止盈：浮盈≥10%激活，回落到成本+2%触发
- 均价止损12%
- 硬止盈30%
- 移动止盈（8%/15%两档）
- 硬止损20%（峰值回撤）
- 分级止损1级：连续2天破MA20→减30%
- 分级止损2级：DIF<0→再减30%
- 分级止损3级：MACD死叉/绿柱放大→清仓
- 趋势止盈：MACD红柱缩短+破MA5→卖10%活动仓
- 破MA5卖活动仓5%（RSI>50时）
- 返回 `[(signal_name, signal_type), ...]`

**`evaluate_entry_signals` 详细逻辑**：
- 过滤器1：防御标的弱势→全市场暂停
- 过滤器2：ATR异常>50%→跳过
- 过滤器3：止盈期不新开
- 通道1：RSI抄底（MACD金叉+RSI≤80+价>MA20）→30%
- 通道2：趋势跟踪（空仓>10天+价>MA20+MACD红柱）→30%
- 通道3：突破入场（20日新高+金叉）→30%
- 通道4：分批建仓（金叉/红柱放大+RSI>40+站MA5+价>MA20）→30%
- 通道5：Test抄底（RSI<35+绿柱缩短+站MA5+价>MA20+前一日绿柱缩短）→30%
- 通道6：试探建仓（绿柱缩短/震荡+RSI>40+站MA5+价>MA20）→15%
- 补仓：跌超3%+MACD非空头+RSI<40→15%
- 确认加仓：突破首笔价或回踩MA5站稳+AO上升→70%

**依赖**：
```python
from market_data import calc_atr, calc_macd, calc_rsi_wilder, calc_ao, dynamic_spacing, calc_5min_indicators
from tracker import get_etfs_config
```

**被依赖关系**：
- `signal_generator.py` → `generate_signals()` 调用策略函数
- `backtest_bt.py` → `ETFStrategy.next()` 调用策略函数（替代重复实现）
- `backtest_t0_trigger_rate.py` → 导入 `t0_buy_score` / `t0_sell_score`
- `intraday_t_once.py` → 导入 `t0_buy_score` / `t0_sell_score`

---

### 3.3 回测层：`backtest_bt.py`（重构）

**职责**：backtrader 引擎 + 资金管理 + 指标适配器。策略逻辑不再独立实现，改为 import strategy 模块。

**当前问题**：

| 当前实现 | 行号 | 问题 |
|----------|------|------|
| `fetch_daily()` | 50-101 | 与 `market_data.fetch_klines_daily()` 重复 |
| `WilderRSI` 类 | 118-142 | 与 `market_data.calc_rsi_wilder()` 重复 |
| `MACDStatus` 类 | 145-212 | 与 `market_data.calc_macd()` 重复 |
| `AOIndicator` 类 | 215-230 | 与 `market_data.calc_ao()` 重复 |
| `ETFStrategy.next()` 中 exit 逻辑 | 417-513 | 与 `strategy.evaluate_stop_signals()` 重复 |
| `ETFStrategy.next()` 中 entry 逻辑 | 514-550 | 与 `strategy.evaluate_entry_signals()` 重复 |
| `ETFStrategy.next()` 中 add-on 逻辑 | 552-574 | 与 `strategy.evaluate_entry_signals()` 中的补仓/确认部分重复 |

**重构后的依赖关系**：

```
backtest_bt.py
├── import market_data          # fetch_klines_daily(), 指标计算
├── import strategy             # evaluate_stop_signals(), evaluate_entry_signals()
├── import backtrader as bt    # 引擎
└── import tracker             # get_code_map()
```

**保留内容**：
- `ETFCommission` 类（手续费+滑点）
- `ETFStrategy.__init__()` — 指标注册（改用 backtrader 原生 Indicator 包装 market_data 函数）
- `ETFStrategy._buy()` / `_sell()` / `_close()` — 下单方法
- `ETFStrategy._get_position_value()` / `_get_avg_cost()` / `_get_shares()` — 持仓查询
- `ETFStrategy.notify_order()` / `notify_trade()` — 回调
- `ETFStrategy.next()` — 改为调用 `strategy.evaluate_stop_signals()` 和 `strategy.evaluate_entry_signals()`，只保留 backtrader 特有的下单转换
- `main()` — 引擎设置、数据加载、分析器

**关键设计决策**：回测中的策略函数调用与实盘 `generate_signals()` 保持一致，参数接口对齐。回测特有的 `ps` 状态字典需要适配 `PositionInfo` 的属性。

---

### 3.4 下单层：`trade.py`（重构）

**职责**：仅负责下单 / 撤单 / 持仓同步。不包含任何策略逻辑和行情拉取。

**当前问题**：

| 当前实现 | 行号 | 问题 |
|----------|------|------|
| `_calc_atr()` | ~300-310 | 与 `market_data.calc_atr()` 重复 |
| `_fetch_klines_5min()` | ~315-365 | 与 `market_data.fetch_klines_5min()` 重复 |
| `_estimate_atr()` | ~370-395 | 策略逻辑（做T的ATR计算），应移到 `strategy` |

**重构后**：

```python
# trade.py 的 import 变化
from market_data import fetch_klines_5min, calc_atr   # 替代 _fetch_klines_5min, _calc_atr
from strategy import ???                                # 替代 _estimate_atr
```

**删除**：
- `_calc_atr()` → 直接用 `market_data.calc_atr()`
- `_fetch_klines_5min()` → 直接用 `market_data.fetch_klines_5min()`
- `_estimate_atr()` → 做T的ATR配对价计算逻辑移到 `strategy.py` 新增函数 `calc_t0_pair_price()`

**保留**：
- `_call_evolving()` — EvolvingSim 子进程封装
- `_get_holding_shares()` / `_get_price()` — 持仓查询
- `_trade_with_confirmation()` — 下单+增量确认
- `sync()` — 持仓同步
- `do_buy()` / `do_sell()` — 普通买卖
- `do_t0_buy()` / `do_t0_sell()` — 做T配对（配对价计算改为从外部传入）
- `do_revoke()` — 撤单
- `_is_continuous_auction()` — 连续竞价判断
- `_clean_lock()` / `_kill_osascript_zombies()` — 进程管理

**`do_t0_buy` / `do_t0_sell` 接口变化**：
- `pair_price` 参数从可选变为**必传**（由 `signal_generator.py` 或 `strategy.py` 计算好后传入）
- 不再内部调用 `_estimate_atr()` 计算配对价

---

## 四、其他文件改动

### 4.1 `rotation.py`

| 当前 | 改为 |
|------|------|
| 自有 `fetch_klines()` | `from market_data import fetch_klines_daily` |
| 自有 `calc_rsi()` | `from market_data import calc_rsi_wilder` |
| 自有 `calc_macd()` | `from market_data import calc_macd` |
| 自有 `calc_ma()` | `from market_data import calc_ma` |
| 自有 `calc_atr()` | `from market_data import calc_atr` |

### 4.2 `backtest_t0_trigger_rate.py`

| 当前 | 改为 |
|------|------|
| 自有 `calc_rsi_wilder()` | `from market_data import calc_rsi_wilder` |
| 自有 `calc_macd()` | `from market_data import calc_macd` |
| 自有 `calc_atr()` | `from market_data import calc_atr` |
| 自有 `t0_buy_score()` | `from strategy import t0_buy_score` |
| 自有 `t0_sell_score()` | `from strategy import t0_sell_score` |
| 自有 `fetch_sina_5min()` | `from market_data import fetch_klines_5min`（需适配） |

### 4.3 `intraday_t_once.py`

| 当前 | 改为 |
|------|------|
| `from signal_generator import calc_rsi_wilder, calc_macd, t0_buy_score, t0_sell_score, CODE_MAP` | `from market_data import calc_rsi_wilder, calc_macd` + `from strategy import t0_buy_score, t0_sell_score` + `from tracker import get_code_map` |

### 4.4 `signal_generator.py`（瘦身后）

**保留**：
- `generate_signals()` — 组装数据→调用策略→输出结果（核心编排逻辑）
- `execute_signals()` — 信号执行（调用 trade.py）
- `main()` — 入口
- 所有持久化辅助函数（`_load_last_executed`、`_save_last_executed`、`_save_trade_error`、`_check_pending_orders` 等）
- `_parse_position_ratio()`、`_get_position_shares()`、`_signal_direction()` 等执行层辅助函数
- 配置常量（`CASH_RESERVE`、`TOTAL_FUND`、`DEAD_RATIO` 等）

**删除**：
- 行情获取函数（→ `market_data.py`）
- 指标计算函数（→ `market_data.py`）
- `PositionInfo` 类（→ `strategy.py`）
- 策略判定函数（→ `strategy.py`）
- 做T评分函数（→ `strategy.py`）
- 网格函数（→ `strategy.py`）

**重构后的 `generate_signals()` 结构**：
```python
def generate_signals(positions=None, all_klines=None, all_tech=None):
    # 1. 获取行情数据（调用 market_data）
    realtime = market_data.fetch_realtime_quotes()
    # 2. 获取K线数据（调用 market_data）
    all_klines = {code: market_data.fetch_klines_daily(code) for code in ETFS}
    # 3. 计算技术指标（调用 market_data）
    all_tech = {code: compute_tech(klines, realtime) for code in ETFS}
    # 4. 策略判定（调用 strategy）
    for code in ETFS:
        stop_actions = strategy.evaluate_stop_signals(pos, price, t, today_str)
        t0_signal = strategy.evaluate_t0_signals(pos, t, ...)
        entry_action = strategy.evaluate_entry_signals(pos, code, price, t, ...)
    # 5. 输出结果
    return result
```

---

## 五、模块依赖关系图

```
                    ┌──────────────┐
                    │   tracker.py │  (配置管理，不变)
                    └──────┬───────┘
                           │ import
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
   ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
   │market_data.py│ │ strategy.py  │ │  trade.py    │
   │  (行情+指标)  │ │  (策略逻辑)   │ │  (下单执行)   │
   └──────┬───────┘ └──────┬───────┘ └──────────────┘
          │                │
          │    import      │
          └────────┬───────┘
                   │
     ┌─────────────┼─────────────────┐
     ▼             ▼                 ▼
┌─────────┐ ┌──────────────┐ ┌──────────────────┐
│backtest │ │  signal_     │ │ rotation.py      │
│ _bt.py  │ │  generator.py│ │                  │
│(回测引擎)│ │  (信号编排)   │ │ (轮动扫描)        │
└─────────┘ └──────┬───────┘ └──────────────────┘
                   │
                   │ import (不再 import strategy/market_data 内部函数)
                   ▼
            ┌──────────────┐
            │  trade.py    │  (仅 signal_generator 调用)
            └──────────────┘

其他文件:
  intraday_t_once.py       → import market_data + strategy
  backtest_t0_trigger_rate.py → import market_data + strategy
  sentiment_check.py       → 不变 (只依赖 tracker)
```

---

## 六、风险与注意事项

| 风险 | 缓解措施 |
|------|----------|
| **backtest 与实盘策略不一致** | 回测的 `ETFStrategy.next()` 改为调用 `strategy.evaluate_*` 函数，与实盘 `generate_signals()` 调用相同函数，确保逻辑统一 |
| **指标计算差异** | `rotation.py` 的 `calc_macd` 使用标准 EMA 实现，而 `signal_generator.py` 使用 SMA-init。统一后 rotation 也应使用 SMA-init 版本以获得一致性 |
| **trade.py 的 `_estimate_atr` 移除** | 做T配对价计算逻辑需要从 `do_t0_buy/do_t0_sell` 中抽到 `strategy.py`，`signal_generator.py` 的 `execute_signals()` 也需相应调整——配对价由 signal_generator 计算好传入 trade.py |
| **PositionInfo 状态序列化** | `to_dict()` / `from_dict()` 方法在 `portfolio.json` 的 `_signal_state` 中持久化，移动后 import 路径需保持不变 |
| **backtrader 与纯 Python 的适配** | 回测中用 backtrader 的 line 对象，策略函数需要接受标量值。需要在 `ETFStrategy.next()` 中做一层薄的适配：从 backtrader line 取值→调用 strategy 函数→转为 backtrader order |

---

## 七、实施顺序建议

1. **Phase 1**：创建 `market_data.py`，从 `signal_generator.py` 抽取行情+指标函数，修改 `signal_generator.py` 的 import
2. **Phase 2**：创建 `strategy.py`，抽取 `PositionInfo` + 策略函数，修改 `signal_generator.py` 的 `generate_signals()` 调用策略函数
3. **Phase 3**：重构 `trade.py`，删除重复的行情/指标函数，改为 import `market_data`
4. **Phase 4**：重构 `backtest_bt.py`，删除重复的策略逻辑，改为 import `strategy`
5. **Phase 5**：重构 `rotation.py`、`backtest_t0_trigger_rate.py`、`intraday_t_once.py`，统一 import 来源
6. **Phase 6**：全量回归测试（实盘信号对比 + 回测结果对比）

---

## 八、确认清单

请确认以下设计决策：

- [ ] **market_data.py 包含指标计算**（而非单独的 indicators.py）—— 指标计算是行情数据的衍生，放在一起减少 import 层级
- [ ] **strategy.py 的 4 个 evaluate_* 函数拆分粒度** —— 是否合理，还是需要更细/更粗的拆分
- [ ] **backtest_bt.py 保留 backtrader 原生 Indicator 类**（WilderRSI/MACDStatus/AOIndicator）还是改为调用 market_data 函数 —— 推荐保留 Indicator 类（backtrader 需要 line 对象做自动对齐），但内部计算逻辑改为调用 market_data
- [ ] **trade.py 的 `do_t0_buy/do_t0_sell` 的 `pair_price` 改为必传参数** —— 策略计算配对价，下单层只管执行
- [ ] **实施顺序** —— 是否按 Phase 1-6 顺序执行，还是需要调整