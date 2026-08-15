# ETF量化交易系统 v5.0

5层+1中心架构，macOS 全自动量化交易。回测+实盘一体化，极简 CLI 下单。

## 架构

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
│  position_info.py      → PositionInfo 共享状态模型       │
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

### 各层模块说明

| 层 | 文件 | 职责 |
|---|------|------|
| **调度** | `signal_generator.py` | cron 入口，编排全流程：拉行情→调策略→决策→执行 |
| **决策** | `decision_engine.py` | 置信度加权投票，融合策略信号+AI信号，输出 OrderIntent |
| **决策** | `signal_cleaner.py` | 近期卖出降权 / 收益率虚高降权 / 信号合理性检查 |
| **决策** | `money_manager.py` | 仓位规模计算 / 分批建仓 / 底仓活动仓动态平衡 |
| **决策** | `risk_manager.py` | 止损检查 / 仓位上限(50%) / 日亏损限额 |
| **决策** | `sell_signal_filter.py` | 卖出信号多重校验（持仓/成本/数量/偏差） |
| **决策** | `ai_signal.py` | Claude API 辅助信号，confidence 阈值过滤 |
| **策略** | `strategies/` | RSI+MACD 建仓/止损 + 网格加仓 + 做T配对 |
| **策略** | `position_info.py` | 持仓状态模型 + 模块级常量 |
| **执行** | `executor.py` | SimBroker(回测) / RealBroker(实盘) 统一接口 |
| **执行** | `order_manager.py` | 订单生成/撤单/查询/超时，兼容 orders/ 目录 |
| **执行** | `trade.py` | 固化 CLI 下单工具，极简模式 |
| **数据** | `market_data.py` | 行情获取(Sina/腾讯/akshare) + 指标计算 |
| **数据** | `factors/` | RSI/MACD/ATR/AO/MA 独立因子计算 |
| **中心** | `state_center.py` | 统一状态中心：持仓、订单、信号、子账户隔离 |

## 辅助模块

| 文件 | 用途 |
|------|------|
| `rotation.py` | 轮动选池 + 入场预检 |
| `sentiment_check.py` | 多源舆情抓取 (新浪/东财/同花顺) + 情感评分 |
| `return_calculator.py` | 总资产收益率 / 年化 / 夏普 / 最大回撤 / 盈亏比 |
| `backtest_bt.py` | backtrader 回测引擎 (5 ETF 共享资金池) |
| `backtest_t0_trigger_rate.py` | 做T触发率专项回测 |
| `config.yaml` | 统一配置管理 (资金/止损/止盈/做T/建仓参数) |
| `tracker.py` | 旧共享模块，已被 state_center 取代 (保留兼容) |

## 回测结果

**方案C极限版** (2021-08-01 → 2026-07-27, 5年回测)

| 指标 | 数值 |
|------|------|
| 累计收益 | **+35.51%** |
| 年化收益 | **66%** |
| 夏普比率 | **2.88** |
| 初始资金 | ¥220,000 |
| 回测引擎 | backtrader |

### 方案C极限版参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 单只ETF上限 | 100% | 取消底仓锁，全仓可调 |
| 底仓比例 | 0% | 全部活动仓 |
| 均价止损 | 20% | 放宽止损空间 |
| 硬止损 | 25% | 从峰值回撤 |
| 硬止盈 | 60% | 大幅提高止盈目标 |
| 保本触发 | 浮盈30% | 提高保本止盈门槛 |
| 建仓通道 | 6通道 | 去掉MA20过滤，大幅提仓 |
| 日亏损限额 | 取消 | 极限宽松 |

## 实盘账户状态

> 最后更新：2026-08-15 14:38

| 指标 | 数值 |
|------|------|
| 总资产 | **¥218,002** |
| 持仓市值 | ¥209,530 |
| 可用现金 | ¥8,472 |
| 累计盈亏 | **+¥18,002** |

### 当前持仓

| 代码 | 名称 | 持仓 | 成本 | 现价 | 盈亏 |
|------|------|------|------|------|------|
| 159516 | 半导体设备ETF | 193,500股 | 0.751 | 0.742 | -1.20% |
| 515050 | 中证全指ETF | 21,600股 | 0.882 | 1.074 | +21.74% |
| 588170 | 科创半导体ETF | 28,900股 | 1.029 | 1.021 | -0.74% |
| 515880 | 通信ETF | 19,200股 | 0.650 | 0.690 | +6.22% |

## 使用方法

### trade.py — 极简下单模式

```bash
# 同步持仓到 portfolio.json
python3 trade.py sync

# 买入
python3 trade.py buy 159516 100 1.234

# 卖出
python3 trade.py sell 159516 100 1.250

# 做T买入 + 配对卖出挂单
python3 trade.py t0_buy 588170 200 1.100 1.120

# 做T卖出 + 配对买入挂单
python3 trade.py t0_sell 588170 200 1.120 1.100

# 全撤所有买卖委托
python3 trade.py revoke_all
```

### 全自动交易

```bash
# 全自动交易（cron 调度）
python3 signal_generator.py --execute

# 仅生成信号，不执行
python3 signal_generator.py
```

### 辅助工具

```bash
# 轮动选池
python3 rotation.py

# 舆情检测
python3 sentiment_check.py

# 回测
python3 backtest_bt.py
python3 backtest_t0_trigger_rate.py
```

## 配置文件

所有参数统一由 `config.yaml` 管理：

```yaml
# 资金管理
total_fund: 220000          # 总资金池
max_per_etf: 220000         # 单只ETF上限
dead_ratio: 0.0             # 底仓比例
active_ratio: 1.0           # 活动仓比例

# 止损参数
stop_loss:
  avg_cost_pct: 0.20        # 均价止损20%
  hard_stop_pct: 0.25       # 硬止损25%
  breakeven_trigger: 0.30   # 浮盈30%激活保本止盈
  breakeven_margin: 0.02    # 回落至成本+2%清仓

# 止盈参数
profit_take:
  hard_take_profit: 0.60    # 硬止盈60%
  trailing_8pct: 0.10       # 8%后移动止盈
  trailing_15pct: 0.08      # 15%后移动止盈

# 建仓通道
entry:
  rsi_min: 40
  rsi_minimal: 30
  position_cap: 1.00        # 单只仓位上限100%

# 做T参数
t0:
  rsi_buy_max: 45           # RSI<45才做T买入
  rsi_sell_min: 55          # RSI>55才做T卖出
  pair_ratio: 0.30          # 做T30%持仓数量
  min_hour: 11              # 11点前不做T

# 执行器
broker_type: "sim"          # sim=回测, real=实盘

# AI 信号
ai_signal:
  enabled: false
  model: "claude-sonnet-4-20250514"
  confidence_threshold: 0.65
```

完整配置见 [config.yaml](config.yaml)。

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

## 策略

### 建仓（6通道，方案C：去掉MA20过滤）

| 通道 | 条件 | 仓位 |
|------|------|------|
| RSI抄底 | MACD金叉 + RSI≤80 | 30% |
| 趋势跟踪 | 空仓>10天 + MACD红柱 | 30% |
| 突破入场 | 20日新高 + MACD金叉 | 30% |
| 分批建仓 | 金叉/红柱放大 + RSI>40 + 站MA5 | 30% |
| Test抄底 | RSI<35 + 绿柱缩短 + 站MA5 | 30% |
| 试探建仓 | 绿柱缩短/震荡 + RSI>40 + 站MA5 | 60% |

### 出场

- 均价止损 20%
- 硬止盈 60%
- 硬止损 25%（从峰值回撤）
- 保本止盈：浮盈30%触发，回落至成本+2%清仓
- 移动止盈：8%→成本+10%，15%→峰值回撤8%

### 做T

- 5分钟K线，MACD(5,34,5)
- RSI<45买/>55卖，3项共振≥2分
- MA20斜率>0.3%禁T卖，<-0.5%禁T买
- 配对ATR×1.1，数量30%，价差×量>10元
- 11:00后运行，每标的每日≤2次

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

## 定时任务

| 任务 | 时间 | 频率 |
|------|------|------|
| 交易监控 | 10:00-15:00 | 每20分 工作日 |
| 做T监控 | 11:00-14:30 | 每30分 工作日 |
| 早间舆情 | 8:00 | 每天 |
| 晚间复盘 | 17:00 | 每天 |
| 轮动选池 | 周一 8:00 | 每周 |
| GitHub备份 | 21:00 | 每天 |

## 环境

- Python 3.11 + macOS
- 同花顺客户端 + EvolvingSim
- 依赖：backtrader, akshare, evolving, pandas, numpy, yaml

## 安装

```bash
git clone git@github.com:lujie001122/quant.git
cd quant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 重构历史

### v5.0 (2026-08-14) — 5层+1中心架构

- **Phase 4**: decision_engine + order_manager + executor + ai_signal 独立
- **Phase 3**: strategies/ 策略包拆分 (base / rsi_macd / grid / t0)
- **Phase 2**: state_center 统一状态中心 + signal_cleaner 信号清洗
- **Phase 1**: factors/ 因子提取 + config.yaml 统一配置 + return_calculator
- 子账户模型：main/t0 现金池隔离，做T与主策略逻辑完全解耦
- 消除全部重复代码，回测引用 strategies/ 验证无退化
- trade.py 去掉行情拉取代码，极简下单模式
- 挂单检查：未成交挂单自动撤旧挂新
- 防御过滤激活：DEFENSE_CODE=159611
- 同花顺激活改用 open -a

### 方案C极限版 (2026-08-04)

- 取消底仓锁，全部活动仓
- 均价止损 20%，硬止损 25%，硬止盈 60%
- 保本止盈触发线上调至浮盈 30%
- 建仓通道去掉 MA20 过滤，大幅提仓
- 取消日亏损限额
- 回测：+35.51%，年化 66%，夏普 2.88

### v4.0 (2026-08-11) — 4层架构

- 4层架构重构：market_data / strategy / signal_generator / trade
- 消除全部重复代码（RSI/MACD/ATR只写一次）
- 回测引用 strategy，验证 +20.61% 无退化
- 删除废弃文件 intraday_t_once.py
- 做T MA20斜率放宽 0.1%→0.3%（买）/0.5%（卖）

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