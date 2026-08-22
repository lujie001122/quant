# ETF量化交易系统 v7.0 — 智能体可复刻

5层+1中心架构，macOS全自动量化交易。回测+实盘一体化。**所有接口通过CLI调用，AI Agent可零前端依赖复制运行。**

## 项目概述

本系统是一套完整的ETF轮动量化交易框架，AI Agent自动执行。覆盖从行情获取、策略信号生成、风险决策到订单执行的全链路。架构采用「5层+1中心」设计：

| 层 | 模块 | 职责 |
|---|------|------|
| **调度层** | cron → `signal_generator.py` | 编排全流程，定时触发 |
| **决策层** | `decision_engine.py` | 信号→清洗→仓位→风控→OrderIntent |
| **策略层** | `strategies/` | RSI+MACD建仓、网格、做T |
| **执行层** | `executor.py` + `trade.py` | 同花顺模拟交易，统一下单CLI |
| **数据层** | `market_data.py` | 腾讯K线 + 新浪实时行情 |
| **中心** | `state_center.py` | 统一状态管理（持仓/订单/信号） |

**当前状态**：同花顺模拟账户，初始20万，累计盈利 **+¥28,674**（+14.3%），运行超6个月。

```
┌─────────────────────────────────────────────────────────┐
│                    调度层 Orchestration                  │
│  signal_generator.py  ← cron 入口，编排全流程            │
├─────────────────────────────────────────────────────────┤
│                    决策层 Decision                       │
│  decision_engine.py    → 决策管线，输出 OrderIntent       │
│  signal_cleaner.py     → 信号清洗 + 合理性检查           │
│  money_manager.py      → 仓位计算 + 分批建仓 + 做T配对   │
│  risk_manager.py       → 分级止损/冷却/日亏损限额        │
│  sell_signal_filter.py → 卖出信号合理性验证              │
├─────────────────────────────────────────────────────────┤
│                    策略层 Strategy                       │
│  strategies/           → 策略判定模块包                  │
│    base.py             → 策略基类                        │
│    rsi_macd.py         → 7通道建仓 + 5种卖出             │
│    grid.py             → 5档买/5档卖网格                 │
│    t0.py               → 做T配对策略                     │
│  position_info.py      → 持仓状态模型 + 全局常量         │
├─────────────────────────────────────────────────────────┤
│                    执行层 Execution                      │
│  executor.py           → SimBroker/RealBroker 统一接口    │
│  order_manager.py      → 订单状态机 lifecycle             │
│  trade.py              → 统一下单 CLI                    │
├─────────────────────────────────────────────────────────┤
│                    数据层 Data                           │
│  market_data.py        → 腾讯K线+新浪行情+指标计算         │
│  factors/              → 独立因子模块                    │
│    indicators.py       → RSI/MACD/ATR/AO/MA 计算        │
│    rsi.py / macd.py    → 单因子独立实现                  │
│    atr.py / ao.py      → 波动率/动量因子                 │
└─────────────────────────────────────────────────────────┘
                         ↕
              ┌──────────────────────┐
              │   state_center.py    │
              │   统一状态中心        │
              │  · 持仓/订单/信号    │
              │  · 子账户隔离        │
              │  · Intent 去重       │
              └──────────────────────┘
```

## 环境要求

| 依赖 | 版本/说明 |
|------|----------|
| Python | 3.11+ |
| 操作系统 | macOS（需同花顺Mac版） |
| 同花顺 | 同花顺Mac客户端 + 模拟交易功能 |
| EvolvingSim | 同花顺模拟交易 Python 封装（`evolving/` 目录） |

## 核心文件清单（输入→输出契约）

每个文件都是独立可调用的模块，AI Agent可按文件逐一理解并替换。

### 调度层

| 文件 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `signal_generator.py` | Cron入口，编排全流程 | `config.yaml`, 行情数据 | 交易信号 → 执行 |

**CLI**：
```bash
python3 signal_generator.py            # 仅生成信号
python3 signal_generator.py --execute  # 生成信号并执行交易
```

### 决策层

| 文件 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `decision_engine.py` | 决策管线：融合信号→清洗→仓位→风控 | 策略信号 + 持仓 | `OrderIntent` 列表 |
| `signal_cleaner.py` | 信号清洗：近期卖出降权、收益率虚高降权 | 原始信号 + 持仓 | 清洗后信号 |
| `money_manager.py` | 仓位计算、分批建仓、做T配对价 | OrderIntent + 账户 | 交易股数 + 配对价 |
| `risk_manager.py` | 止损/仓位上限/日亏损限额/连续亏损熔断 | 持仓 + 行情 | 风控阻断/放行 |
| `sell_signal_filter.py` | 卖出信号多重校验 | 卖出信号 + 持仓 | 过滤后卖出信号 |

### 策略层

| 文件 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `strategies/base.py` | 策略基类，定义统一接口 | — | 抽象方法 |
| `strategies/rsi_macd.py` | 7通道建仓 + 5种卖出 + 做T评分 | K线 + 持仓 | 买/卖/做T信号 |
| `strategies/grid.py` | 网格触达(熔断) + 基准重置 + 网格表 | 持仓 + 价格 | 网格买/卖信号 |
| `strategies/t0.py` | 做T配对策略（5分钟K线） | 5分钟K线 + 持仓 | 做T买入/卖出 + 配对价 |
| `position_info.py` | 持仓状态模型 + 模块级常量 | `config.yaml` | `PositionInfo` 对象 |

### 执行层

| 文件 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `executor.py` | 统一交易执行器，SimBroker/RealBroker | `OrderIntent` | `ExecutionResult` |
| `order_manager.py` | 订单生命周期：提交/撤单/查询/超时 | `OrderIntent` | `Order` (pending→filled/revoked/failed) |
| `trade.py` | 统一下单CLI | CLI参数 | 下单结果 |

**CLI**：
```bash
python3 trade.py buy 159516 100 1.234          # 买入
python3 trade.py sell 159516 100 1.250         # 卖出
python3 trade.py t0_buy 588170 200 1.100 1.120 # 做T买入+配对卖出
python3 trade.py t0_sell 588170 200 1.120 1.100 # 做T卖出+配对买入
python3 trade.py revoke_all                     # 全撤所有委托
python3 trade.py sync                           # 同步持仓到 portfolio.json
```

### 数据层

| 文件 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `market_data.py` | 腾讯K线(前复权) + 新浪实时行情 + 指标计算 | ETF代码列表 | 日K/5分钟K + RSI/MACD/ATR/AO/MA20 |
| `factors/indicators.py` | RSI/MACD/ATR/AO/MA 统一计算 | 价格序列 | 技术指标值 |
| `factors/rsi.py` | Wilder RSI 独立因子 | 价格序列 | RSI值 |
| `factors/macd.py` | MACD 独立因子 | 价格序列 | DIF/DEA/柱 |
| `factors/atr.py` | ATR 波动率因子 | 价格序列 | ATR值 |
| `factors/ao.py` | AO 动量因子 | 价格序列 | AO值 |

### 中心模块

| 文件 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `state_center.py` | 统一状态中心：持仓/订单/信号/子账户隔离 | 各层读写请求 | 统一状态 |

### 配置与辅助

| 文件 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `config.yaml` | **22个配置组**，所有参数集中管理 | — | 全局参数 |
| `etf_pool.json` | 8只ETF标的池（代码/名称/动量/回撤/行业关键词） | — | ETF配置 |
| `backtest_bt.py` | backtrader回测引擎 | 历史K线 + `config.yaml` | 回测报告 |
| `rotation.py` | 轮动选池 + 入场预检 | 行情数据 | 候选标的 |
| `sentiment_check.py` | 多源舆情抓取 + 情感评分 | 标的代码 | 舆情评分 |
| `return_calculator.py` | 收益率/年化/夏普/最大回撤 | 交易记录 | 绩效指标 |
| `scripts/restart_ths.py` | 同花顺全撤+杀进程+重启 | —（`--no-revoke`跳过撤单） | 同花顺重启 |
| `portfolio.json` | 持仓/账户快照（运行时） | state_center写入 | 持仓JSON |

## 策略参数（当前最新 v7.0）

### 建仓策略（RSI+MACD多头排列）

| 参数 | 值 | 说明 |
|------|-----|------|
| 建仓条件 | RSI < 40 | RSI超卖 |
| 趋势确认 | MA5 > MA10 > MA20 | 多头排列 |
| 价格过滤 | price > MA20 | 站上MA20 |
| 试探仓位 | 30% | 首笔建仓 |
| 确认加仓 | 2次 × 30% | 间隔1天 |
| 底仓锁定 | 30%（dead_ratio） | 不参与卖出 |
| 活动仓 | 70%（active_ratio） | 可灵活交易 |
| 补仓间距 | 5% | 确认加仓触发间距 |

### 止损策略（分级）

| 条件 | 止损比例 |
|------|---------|
| 仓位 < 50% | -25% |
| 仓位 50-80% | -20% |
| 仓位 > 80% | -15% |

### 止盈策略

| 类型 | 条件 | 说明 |
|------|------|------|
| 趋势止盈 | MACD红柱缩短 + 破MA5 + price < MA10确认 | 冷却2天，每日最多2次 |
| 硬止盈 | +60%（hard_take_profit） | 固定止盈线 |
| 保本止盈 | 触发 +30% 后回撤至 +2% | breakeven保护 |
| 移动止盈 | +8%/+15% 两档 | 动态追踪 |

### 网格策略

| 参数 | 值 | 说明 |
|------|-----|------|
| 买入档位 | 5档 | 倒金字塔加仓（20%/25%/30%/35%/40%） |
| 卖出档位 | 5档 | 每档固定20%仓位 |
| 网格止损 | -10% | 跌破网格下限 |
| 互斥规则 | 与趋势止盈互斥 | 同一时间只触发一种 |
| 熔断 | 触及第5档买入后冻结 | 站上MA5解冻 |

### 做T策略

| 参数 | 值 | 说明 |
|------|-----|------|
| 买入触发 | RSI < 45 | 5分钟K线RSI |
| 卖出触发 | RSI > 55 | 5分钟K线RSI |
| 极端兜底 | RSI < 25 直接满分买 / RSI > 75 直接满分卖 | 跳过MACD/量比 |
| MACD周期 | (5, 34, 5) | 5分钟 |
| 共振要求 | ≥ 2项 | RSI + MACD + 量比 |
| 配对价差 | 固定 ¥230 | 含滑点缓冲 |
| 最小股数 | ≥ 5000 股 | 满足价差收益 |
| 运行时段 | 11:00 后 | 避免早盘波动 |
| 每日上限 | 每标的 ≤ 2次 | 可配置 |
| MA20斜率过滤 | 斜率 > 0.3% 禁T卖 / < -0.5% 禁T买 | 趋势保护 |

### 风控参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 清仓冷静期 | 2天 | cooldown_days |
| 空仓阈值 | 10天 | 空仓超时重新评估 |
| 日亏损限额 | 20%（avg_cost_pct） | 触发后停止交易 |
| 连续亏损熔断 | 3天 | 日亏损 > 1% 计入 |
| 单只上限 | ¥220,000 | max_per_etf |

## 回测数据

| 周期 | 收益 | 年化 | 回撤 | 夏普 | 交易 |
|------|------|------|------|------|------|
| 基线 2.5年 (2024.1-2026.6) | **+110.91%** | **34.85%** | **8.16%** | **1.91** | 52 |
| 优化后 1年 (2025.08-2026.08) | **+27.56%** | **27.56%** | 12.94% | 1.19 | — |
| 6个月 (2026.01-2026.07) | **+26.92%** | **61.71%** | — | 2.69 | — |
| 逐年 2024 | +28.60% | 28.60% | 7.96% | 1.34 | 25 |
| 逐年 2025 | -2.27% | -2.28% | 7.18% | -0.57 | 15 |
| 逐年 2026(6M) | +38.19% | 92.69% | 8.67% | 3.50 | 9 |

**实盘 8月**：+11.5%（其中做T贡献显著）。

## 定时任务（7个 Cron Job）

通过 `hermes cron` 调度，所有任务均为 CLI 调用，全部 pin 到 `custom/glm-5.1`：

| # | 任务 | Cron 表达式 | 频率 | 命令 |
|---|------|------------|------|------|
| 1 | 早间舆情 | `0 8 * * *` | 每天 8:00 | `python3 sentiment_check.py` |
| 2 | 同花顺全撤+重启 | `0 9,13 * * 1-5` | 工作日 9:00/13:00 | `python3 scripts/restart_ths.py` |
| 3 | 交易监控 | `0,30 10-11,13-15 * * 1-5` | 工作日每30分 | `python3 signal_generator.py --execute` |
| 4 | 做T监控 | `11,41 10-11,13-15 * * 1-5` | 工作日每30分(偏移) | `python3 signal_generator.py --execute` |
| 5 | 晚间复盘 | `0 17 * * *` | 每天 17:00 | `python3 return_calculator.py` |
| 6 | 轮动选池 | `0 8 * * 1` | 每周一 8:00 | `python3 rotation.py` |
| 7 | GitHub备份 | `0 21 * * *` | 每天 21:00 | `git add -A && git commit -m "auto backup" && git push` |

> 交易监控和做T监控在 11:30-13:00 午休时段跳过，通过 cron 表达式自动实现。

## 关键铁律

### 代码修改铁律
- **Hermes 绝不改代码**：所有代码修改全由 Claude 执行（`delegate_task`），Hermes 只读取和分析
- **一个功能只在一处定义**：禁止跨文件重复实现同一逻辑

### 策略铁律
- **做T和买卖信号完全独立**：两个子账户（main/t0），现金池隔离，互不干扰
- **回测不含做T**：backtrader 回测只验证主策略建仓/止损/止盈
- **回测与实盘参数一致**：策略代码同一份，config.yaml 驱动

### 同花顺铁律
- **不信返回值**：EvolvingSim 返回 `(False, None)` 也可能实际成交，以 `getHoldingShares` 为准
- **下单默认成功**：trade.py 调 EvolvingSim 后不验证返回值
- **断连不重试只报 ALERT**：EvolvingSim 连接失败直接告警，不自动重试

### 交易铁律
- **股数最低 5000 股**：所有交易（含做T）必须满足最小股数
- **数量必须 100 整数倍**：同花顺硬性要求

## 6步复刻流程

一个 AI Agent 从零复刻整个系统的完整步骤：

### 第1步：安装依赖

```bash
git clone git@github.com:lujie001122/quant.git
cd quant
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

依赖清单（`requirements.txt`）：
```
akshare>=1.10
backtrader>=1.9
pandas>=1.5
```

### 第2步：配置参数

编辑 `config.yaml`（22个配置组），至少修改：
- `total_fund` → 实际资金
- `max_per_etf` → 单只上限

编辑 `etf_pool.json`，确认标的池（8只ETF：`159516, 515880, 588170, 159532, 515050, 159611, 512170, 513780`）。

### 第3步：配置同花顺

1. 安装同花顺Mac版 + 开启模拟交易
2. 确保 `evolving/` 目录下的 EvolvingSim Python 封装可用
3. 验证同花顺可被 `open -a /Applications/同花顺.app` 启动

### 第4步：验证下单通道

```bash
python3 trade.py sync
# 预期：同步持仓到 portfolio.json，无报错
```

### 第5步：回测验证

```bash
python3 backtest_bt.py
# 预期：基线 2.5年 +110.91%，年化 34.85%，回撤 8.16%，夏普 1.91
```

### 第6步：配置定时任务

```bash
hermes cron add \
  --name "交易监控" \
  --schedule "0,30 10-11,13-15 * * 1-5" \
  --command "cd /path/to/quant && .venv/bin/python3 signal_generator.py --execute" \
  --model "custom/glm-5.1"
```

完成以上6步后，系统即可全自动运行。同花顺需保持运行状态，模拟交易需开启。

## 当前标的池

| 代码 | 名称 | 类型 | 动量 |
|------|------|------|------|
| 159516 | 半导体设备ETF | 动量 | 12.82 |
| 515880 | 通信ETF | 动量 | 9.27 |
| 588170 | 科创半导体ETF | 动量 | 8.55 |
| 159532 | 中证2000ETF | 防御 | 6.74 |
| 515050 | 中证全指ETF | 防御 | 5.12 |
| 159611 | 电力ETF | 防御 | 6.06 |
| 512170 | 医疗ETF | 防御 | 8.00 |
| 513780 | 港股创新药ETF | 备用 | — |

## 子账户架构

系统使用双子账户模型实现主策略与做T逻辑隔离：

| 账户 | 用途 | 资金隔离 |
|------|------|---------|
| `main` | 主策略建仓/止损/网格 | 独立现金池 |
| `t0` | 做T配对交易 | 独立现金池 |

```python
from state_center import StateCenter, get_main_position, get_t0_position
sc = StateCenter.get_instance()
main_pos = sc.get_position("159516", account="main")
t0_pos = sc.get_position("159516", account="t0")
```

## 配置文件

核心参数在 `config.yaml` 中统一管理（22个配置组）：

| 配置组 | 关键参数 |
|--------|---------|
| 资金管理 | `total_fund: 220000`, `max_per_etf: 220000`, `dead_ratio: 0.3`, `active_ratio: 0.7` |
| 建仓 | `entry.rsi_min: 40`, `entry.position_cap: 1.0`, `entry.dip_pct: -0.03` |
| 止损 | `stop_loss.hard_stop_pct: 0.25`, `stop_loss.avg_cost_pct: 0.20`, `stop_loss.breakeven_trigger: 0.3` |
| 止盈 | `profit_take.hard_take_profit: 0.6`, `profit_take.trailing_8pct: 0.1`, `profit_take.trailing_15pct: 0.08` |
| 趋势止盈 | `trend_profit.cooldown_days: 2`, `trend_profit.max_daily: 2` |
| 做T | `t0.rsi_buy_max: 45`, `t0.rsi_sell_min: 55`, `t0.min_spread: 200`, `t0.pair_ratio: 0.3` |
| 网格 | `grid_buy_levels: 5`, `grid_sell_levels: 3`, `grid_tolerance: 0.15` |
| 网格重置 | `grid_reset.upper_levels: 4`, `grid_reset.lower_levels: 6` |
| 倒金字塔 | `inverted_weights: [0.2, 0.25, 0.3, 0.35, 0.4]` |
| 风控 | `cooldown_days: 2`, `max_daily_buys: 1`, `max_daily_t0: 2` |
| 趋势 | `trend.empty_days_threshold: 10`, `trend.ma20_slope_period: 5` |
| 行情数据 | `market_data.kline_limit: 1200`, `market_data.rsi_period: 14`, `market_data.macd_fast: 12` |
| 回测 | `backtest.trade_start: "2021-08-01"`, `backtest.slippage_pct: 0.001`, `backtest.fee: 5` |
| 执行器 | `broker_type: sim` (sim/real) |
| 信号 | `signal.before_11: "11:00"`, `signal.max_retries: 3` |
| 订单 | `order.max_retries: 3`, `order.timeout_seconds: 300`, `order.retry_interval: 5` |
| AI信号 | `ai_signal.enabled: false`, `ai_signal.model: claude-sonnet-4-20250514` |
| ATR | `atr_high_threshold: 0.05` |
| 防御 | `defense_code: "159611"` |

## 执行流程

```
cron → signal_generator.py --execute
  ├─ 拉行情 (market_data.py)
  │   ├─ 腾讯K线 (fetch_klines_daily / fetch_klines_5min)
  │   └─ 新浪实时行情 (fetch_realtime_quotes)
  ├─ 生成信号 (strategies/)
  │   ├─ rsi_macd.py: 建仓/止损/止盈
  │   ├─ grid.py: 网格触达
  │   └─ t0.py: 做T配对
  ├─ 信号清洗 (signal_cleaner.py)
  │   ├─ 近期卖出降权
  │   └─ 收益率虚高降权
  ├─ 决策引擎 (decision_engine.py)
  │   ├─ money_manager.py → 仓位计算 + 做T配对价
  │   ├─ risk_manager.py  → 风控检查（止损/上限/日亏损/熔断）
  │   └─ sell_signal_filter.py → 卖单校验
  ├─ 订单管理 (order_manager.py)
  │   ├─ Intent 去重 (orders/intent/*.json)
  │   ├─ 挂单检查（未成交→撤旧挂新）
  │   └─ 限价修正（买≤现价，卖≥现价）
  ├─ 交易执行 (executor.py → trade.py)
  │   └─ RealBroker → EvolvingSim
  └─ 状态同步 (state_center.py → portfolio.json)
```

## AI Agent 可复制性

本系统设计为**其他 AI Agent 可克隆复制运行**，无需前端、无需人工干预：

- ✅ **所有接口通过 CLI 调用**：`signal_generator.py`、`trade.py`、`rotation.py`、`sentiment_check.py` 等均为纯命令行工具
- ✅ **零前端依赖**：无 Web UI、无 GUI、无 Electron
- ✅ **配置文件驱动**：所有参数集中在 `config.yaml`（22个配置组），修改参数无需改代码
- ✅ **统一状态管理**：`state_center.py` 提供持仓/订单/信号的读写接口，任何模块均可直接调用
- ✅ **明确的模块边界**：每层有清晰的输入输出契约，AI Agent 可逐模块理解并替换
- ✅ **独立的回测验证**：`backtest_bt.py` 引用策略模块，修改参数后跑回测即可验证无退化
- ✅ **Git 版本化管理**：所有代码和配置都在 Git 中，Agent 可 clone 后直接运行

## 已知问题

| 问题 | 影响 | 处理方式 |
|------|------|---------|
| 腾讯K线 close 有延迟 | 当日盈亏计算偏高 | 算当日盈亏用新浪实时行情 |
| 512170 行情偶发 NaN | 医疗ETF数据缺失 | 策略层做 NaN 兜底，跳过该标的 |
| EvolvingSim 返回 `(False, None)` 但实际成交 | 无法判断成交状态 | 以 `getHoldingShares` 为准，不信返回值 |
| EvolvingSim 断连 | 下单失败 | 不重试，直接报 ALERT |

## 重构历史

### v7.0 (2026-08) — 智能体可复刻

- 全面更新 README：为 AI Agent 复刻优化，完整文件契约、参数、铁律、复刻流程
- 回测数据更新：基线2.5年 +110.91%，6个月 +26.92%
- 策略参数固化：建仓/止损/止盈/网格/做T 全部文档化
- 定时任务精确化：7个 cron job，每个 pin 到 `custom/glm-5.1`
- 实盘数据更新：初始20万 → 当前22.3万+

### v6.0 (2026-08) — 架构稳定

- 5层+1中心架构稳定运行
- decision_engine + order_manager + executor + ai_signal 独立
- strategies/ 策略包拆分（base / rsi_macd / grid / t0）
- state_center 统一状态中心 + signal_cleaner 信号清洗
- factors/ 因子提取 + config.yaml 统一配置 + return_calculator
- 子账户模型：main/t0 现金池隔离，做T与主策略逻辑完全解耦
- 消除全部重复代码，回测引用 strategies/ 验证无退化
- 挂单检查：未成交挂单自动撤旧挂新
- 同花顺激活改用 `open -a`

## .gitignore 说明

```
.venv/          # Python 虚拟环境
__pycache__/    # Python 缓存
*.pyc           # 编译文件
.env            # 环境变量（含敏感信息）
.hermes/        # Hermes Agent 配置目录
backup/         # 备份文件
plans/          # 计划文档
orders/         # 订单记录（每日生成）
.idea/          # PyCharm IDE 配置
.DS_Store       # macOS 系统文件
```