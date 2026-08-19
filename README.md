# ETF量化交易系统 v5.0

5层+1中心架构，macOS全自动量化交易。回测+实盘一体化。**所有接口通过CLI调用，AI Agent可零前端依赖复制运行。**

## 项目概述

本系统是一套完整的ETF量化交易框架，覆盖从行情获取、策略信号生成、风险决策到订单执行的全链路。架构采用「5层+1中心」设计：调度层编排全流程，决策层做置信度加权投票，策略层生成买卖信号，执行层对接同花顺模拟交易，数据层提供行情与指标，state_center 作为统一状态中心贯穿各层。

```
┌─────────────────────────────────────────────────────────┐
│                    调度层 Orchestration                  │
│  signal_generator.py  ← cron 入口，编排全流程            │
├─────────────────────────────────────────────────────────┤
│                    决策层 Decision                       │
│  decision_engine.py    → 置信度加权投票，输出 OrderIntent │
│  signal_cleaner.py     → AI 信号清洗 + 合理性检查        │
│  money_manager.py      → 仓位计算 + 分批建仓             │
│  risk_manager.py       → 止损/仓位上限/日亏损限额        │
│  sell_signal_filter.py → 卖出信号合理性验证              │
│  ai_signal.py          → Claude AI 辅助信号源            │
├─────────────────────────────────────────────────────────┤
│                    策略层 Strategy                       │
│  strategies/           → 策略判定模块包                  │
│    base.py             → 策略基类                        │
│    rsi_macd.py         → RSI+MACD 建仓/止损              │
│    grid.py             → 网格加仓减仓                    │
│    t0.py               → 做T配对策略                     │
│  position_info.py      → 持仓状态模型 + 全局常量         │
├─────────────────────────────────────────────────────────┤
│                    执行层 Execution                      │
│  executor.py           → 交易执行器 (Sim/Real Broker)     │
│  order_manager.py      → 订单生命周期管理                │
│  trade.py              → 固化下单工具 (CLI)              │
├─────────────────────────────────────────────────────────┤
│                    数据层 Data                           │
│  market_data.py        → 行情获取 + 指标计算              │
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
| EvolvingSim | 同花顺模拟交易 Python 封装（随项目提供） |

## 快速开始

```bash
# 1. 克隆仓库
git clone git@github.com:lujie001122/quant.git
cd quant

# 2. 创建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. 配置 config.yaml（调整资金、止损、标的等参数）
# 至少需要修改 total_fund 为你的实际资金

# 4. 运行回测验证
python3 backtest_bt.py

# 5. 启动实盘交易
python3 signal_generator.py --execute
```

## 核心模块说明

### 调度层

| 模块 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `signal_generator.py` | Cron 入口，编排全流程：拉行情→调策略→决策→执行 | config.yaml, 行情数据 | 交易信号 → 执行 |

**CLI 调用**：
```bash
python3 signal_generator.py            # 仅生成信号，不执行
python3 signal_generator.py --execute  # 生成信号并执行交易
```

### 决策层

| 模块 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `decision_engine.py` | 置信度加权投票，融合策略+AI信号 | 策略信号 + AI信号 | `OrderIntent` 列表 |
| `signal_cleaner.py` | 信号清洗：近期卖出降权、收益率虚高降权 | 原始信号 + 持仓状态 | 清洗后的信号 |
| `money_manager.py` | 仓位计算、分批建仓、底仓/活动仓平衡 | OrderIntent + 账户状态 | 具体下单数量 |
| `risk_manager.py` | 止损检查、仓位上限、日亏损限额 | 持仓 + 行情 | 风控阻断/放行 |
| `sell_signal_filter.py` | 卖出信号多重校验 | 卖出信号 + 持仓 | 过滤后的卖出信号 |
| `ai_signal.py` | Claude API 辅助信号源 | 持仓 + 行情 + 舆情 | AI 置信度信号 |

### 策略层

| 模块 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `strategies/base.py` | 策略基类，定义统一接口 | — | 抽象方法 |
| `strategies/rsi_macd.py` | RSI+MACD 建仓/止损信号 | K线数据 + 持仓 | 买入/卖出信号 |
| `strategies/grid.py` | 网格加仓减仓信号 | 持仓 + 价格 | 网格买入/卖出信号 |
| `strategies/t0.py` | 做T配对信号（5分钟K线） | 5分钟K线 + 持仓 | 做T买入/卖出+配对价 |
| `position_info.py` | 持仓状态模型 + 全局常量 | config.yaml | PositionInfo 对象 |

### 执行层

| 模块 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `executor.py` | 统一交易执行器，SimBroker(回测)/RealBroker(实盘) | OrderIntent | 成交结果 |
| `order_manager.py` | 订单生命周期管理：提交/撤单/查询/超时 | OrderIntent | 订单状态 (pending→filled/revoked/failed) |
| `trade.py` | 固化 CLI 下单工具 | CLI 参数 | 下单结果 |

**CLI 调用**：
```bash
python3 trade.py buy 159516 100 1.234          # 买入
python3 trade.py sell 159516 100 1.250         # 卖出
python3 trade.py t0_buy 588170 200 1.100 1.120 # 做T买入+配对卖出
python3 trade.py t0_sell 588170 200 1.120 1.100 # 做T卖出+配对买入
python3 trade.py revoke_all                     # 全撤所有委托
```

### 数据层

| 模块 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `market_data.py` | 行情获取（腾讯/akshare）+ 指标计算 | ETF 代码列表 | 实时行情 + K线 + 技术指标 |
| `factors/indicators.py` | RSI/MACD/ATR/AO/MA 统一计算 | 价格序列 | 技术指标值 |
| `factors/rsi.py` | RSI 独立因子 | 价格序列 | RSI 值 |
| `factors/macd.py` | MACD 独立因子 | 价格序列 | MACD 值 |
| `factors/atr.py` | ATR 波动率因子 | 价格序列 | ATR 值 |
| `factors/ao.py` | AO 动量因子 | 价格序列 | AO 值 |

### 中心模块

| 模块 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `state_center.py` | 统一状态中心：持仓/订单/信号/子账户隔离 | 各层读写请求 | 统一状态 |

### 辅助模块

| 模块 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `rotation.py` | 轮动选池 + 入场预检 | 行情数据 | 候选标的 |
| `sentiment_check.py` | 多源舆情抓取 + 情感评分 | 标的代码 | 舆情评分 |
| `return_calculator.py` | 收益率/年化/夏普/最大回撤计算 | 交易记录 | 绩效指标 |
| `backtest_bt.py` | backtrader 回测引擎 | 历史K线 + config.yaml | 回测报告 |
| `backtest_t0_trigger_rate.py` | 做T触发率专项回测 | 5分钟K线 | 触发率统计 |
| `config.yaml` | 统一配置（资金/止损/止盈/做T/建仓） | — | 全局参数 |
| `scripts/restart_ths.py` | 同花顺全撤+杀进程+重启脚本 | — | 同花顺重启 |

## 回测数据

方案C（极限版，单只上限100%、止损20%、取消保本止盈/MA20过滤）完整回测结果：

| 周期 | 收益 | 年化 | 回撤 | 夏普 | 交易 |
|------|------|------|------|------|------|
| 2.5年 (2024.1-2026.6) | **+110.91%** | **34.85%** | **8.16%** | **1.91** | 52 |
| 逐年 2024 | +28.60% | 28.60% | 7.96% | 1.34 | 25 |
| 逐年 2025 | -2.27% | -2.28% | 7.18% | -0.57 | 15 |
| 逐年 2026(6M) | +38.19% | 92.69% | 8.67% | 3.50 | 9 |

**复利**：1.286 × 0.977 × 1.382 = **+73.6%**。每年回撤 < 8.7%。

## 实盘数据

实盘从2026年初开始运行，当前数据：

| 指标 | 数值 |
|------|------|
| 总资产 | ¥228,674 |
| 累计盈利 | **+¥28,674** |
| 起始资金 | ¥200,000 |
| 运行时长 | 6个月+ |
| 当前持仓 | 半导体设备ETF(159516)、通信ETF(515880) |

## 定时任务（7个 Cron Job）

通过 `hermes cron` 调度，所有任务均为 CLI 调用：

| # | 任务 | Cron 表达式 | 频率 | 命令 |
|---|------|------------|------|------|
| 1 | 同花顺全撤+重启 | `0 9,13 * * 1-5` | 工作日 9:00/13:00 | `python3 scripts/restart_ths.py` |
| 2 | 交易监控 | `*/20 10-11,13-15 * * 1-5` | 工作日每20分 | `python3 signal_generator.py --execute` |
| 3 | 做T监控 | `0,30 11,13-14 * * 1-5` | 工作日每30分 | `python3 signal_generator.py --execute` |
| 4 | 早间舆情 | `0 8 * * *` | 每天 8:00 | `python3 sentiment_check.py` |
| 5 | 晚间复盘 | `0 17 * * *` | 每天 17:00 | `python3 return_calculator.py` |
| 6 | 轮动选池 | `0 8 * * 1` | 每周一 8:00 | `python3 rotation.py` |
| 7 | GitHub备份 | `0 21 * * *` | 每天 21:00 | `git add -A && git commit -m "auto backup" && git push` |

> **注意**：交易监控和做T监控在 11:30-13:00 午休时段跳过，通过 cron 表达式自动实现。

## AI Agent 可复制性

本系统设计为**其他 AI Agent 可克隆复制运行**，无需前端、无需人工干预：

- ✅ **所有接口通过 CLI 调用**：`signal_generator.py`、`trade.py`、`rotation.py`、`sentiment_check.py` 等均为纯命令行工具
- ✅ **零前端依赖**：无 Web UI、无 GUI、无 Electron
- ✅ **配置文件驱动**：所有参数集中在 `config.yaml`，修改参数无需改代码
- ✅ **统一状态管理**：`state_center.py` 提供持仓/订单/信号的读写接口，任何模块均可直接调用
- ✅ **明确的模块边界**：每层有清晰的输入输出契约，AI Agent 可逐模块理解并替换
- ✅ **独立的回测验证**：`backtest_bt.py` 引用策略模块，修改参数后跑回测即可验证无退化
- ✅ **Git 版本化管理**：所有代码和配置都在 Git 中，Agent 可 clone 后直接运行

**一个 AI Agent 复制运行的完整流程**：

```bash
# 1. Clone
git clone git@github.com:lujie001122/quant.git && cd quant

# 2. 安装
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. 配置
# 编辑 config.yaml，修改 total_fund 为实际资金

# 4. 验证回测
python3 backtest_bt.py
# 预期：2.5年 +110.91%，年化 34.85%

# 5. 设置定时任务
hermes cron add \
  --name "交易监控" \
  --schedule "*/20 10-11,13-15 * * 1-5" \
  --command "cd /path/to/quant && .venv/bin/python3 signal_generator.py --execute"

# 6. 启动运行
# 同花顺需保持运行状态，模拟交易需开启
```

## 当前标的

| 代码 | 名称 | 类型 |
|------|------|------|
| 159516 | 半导体设备ETF | 动量 |
| 515880 | 通信ETF | 动量 |
| 588170 | 科创半导体ETF | 动量 |
| 159532 | 中证2000ETF | 防御 |
| 515050 | 通信ETF | 防御 |
| 159611 | 电力ETF | 防御 |
| 512170 | 医疗ETF | 防御 |

## 子账户架构

系统使用双子账户模型实现主策略与做T逻辑隔离：

| 账户 | 用途 | 资金比例 |
|------|------|---------|
| main | 主策略建仓/止损/网格 | 80% |
| t0 | 做T配对交易 | 20% |

```python
from state_center import StateCenter, get_main_position, get_t0_position
sc = StateCenter.get_instance()
main_pos = sc.get_position("159516", account="main")
t0_pos = sc.get_position("159516", account="t0")
```

## 策略概要

### 建仓（6通道，price > MA20）
RSI抄底 / MACD金叉追涨 / 突破追 / 分批建仓 / 确认加仓 / 试探仓

### 出场
均价止损12% / 保本止盈 / 分级止损(3级) / 移动止盈 / 硬止盈30%

### 做T
- 5分钟K线，MACD(5,34,5)
- RSI<45买/>55卖，3项共振≥2分
- MA20斜率>0.3%禁T卖，<-0.5%禁T买
- 配对固定价差，数量30%
- 11:00后运行，每标的每日≤2次

## 配置文件

核心参数在 `config.yaml` 中统一管理：

| 配置段 | 关键参数 |
|--------|---------|
| 资金管理 | `total_fund`, `max_per_etf`, `dead_ratio`, `active_ratio` |
| 止损参数 | `avg_cost_pct`, `hard_stop_pct`, `breakeven_trigger` |
| 止盈参数 | `hard_take_profit`, `trailing_8pct`, `trailing_15pct` |
| 建仓通道 | `entry.rsi_min`, `entry.dip_pct`, `entry.position_cap` |
| 做T参数 | `t0.rsi_buy_max`, `t0.pair_ratio`, `t0.min_spread` |
| 回测参数 | `backtest.trade_start`, `backtest.trade_end`, `backtest.periods` |
| 执行器 | `broker_type` (sim/real) |

## 执行流程

```
cron → signal_generator.py --execute
  ├─ 拉行情 (market_data.py)
  ├─ 生成信号 (strategies/ + ai_signal.py)
  ├─ 信号清洗 (signal_cleaner.py)
  ├─ 决策引擎 (decision_engine.py)
  │   ├─ money_manager.py → 仓位计算
  │   ├─ risk_manager.py  → 风控检查
  │   └─ sell_signal_filter.py → 卖单校验
  ├─ 订单管理 (order_manager.py)
  │   ├─ Intent 去重 (orders/intent/)
  │   ├─ 挂单检查（未成交→撤旧挂新）
  │   └─ 限价修正（买≤现价，卖≥现价）
  ├─ 交易执行 (executor.py → trade.py)
  └─ 状态同步 (state_center.py → portfolio.json)
```

## 重构历史

### v5.0 (2026-08) — 5层+1中心架构

- **Phase 4**: decision_engine + order_manager + executor + ai_signal 独立
- **Phase 3**: strategies/ 策略包拆分 (base / rsi_macd / grid / t0)
- **Phase 2**: state_center 统一状态中心 + signal_cleaner 信号清洗
- **Phase 1**: factors/ 因子提取 + config.yaml 统一配置 + return_calculator
- 子账户模型：main/t0 现金池隔离，做T与主策略逻辑完全解耦
- 消除全部重复代码，回测引用 strategies/ 验证无退化
- trade.py 去掉行情拉取代码
- 挂单检查：未成交挂单自动撤旧挂新
- 防御过滤激活：DEFENSE_CODE=159611
- 同花顺激活改用 open -a

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