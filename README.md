# ETF量化交易系统 v5.0

5层+1中心架构，macOS全自动量化交易。回测+实盘一体化。

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

| 层 | 核心文件 | 职责 |
|---|---------|------|
| 调度 | `signal_generator.py` | cron 入口，编排全流程：拉行情→调策略→决策→执行 |
| 决策 | `decision_engine.py` | 置信度加权投票，融合策略+AI信号，输出 OrderIntent |
| 决策 | `signal_cleaner.py` | 近期卖出降权 / 收益率虚高降权 / 信号合理性检查 |
| 决策 | `money_manager.py` | 仓位规模计算 / 分批建仓 / 底仓活动仓动态平衡 |
| 决策 | `risk_manager.py` | 止损检查 / 仓位上限(50%) / 日亏损限额 |
| 决策 | `sell_signal_filter.py` | 卖出信号多重校验（持仓/成本/数量/偏差） |
| 决策 | `ai_signal.py` | Claude API 辅助信号，confidence 阈值过滤 |
| 策略 | `strategies/` | RSI+MACD 建仓/止损 + 网格加仓 + 做T配对 |
| 策略 | `position_info.py` | 持仓状态模型 + 模块级常量 |
| 执行 | `executor.py` | SimBroker(回测) / RealBroker(实盘) 统一接口 |
| 执行 | `order_manager.py` | 订单生成/撤单/查询/超时，兼容 orders/ 目录 |
| 执行 | `trade.py` | 固化 CLI 下单工具 (buy/sell/t0_buy/t0_sell/revoke) |
| 数据 | `market_data.py` | 行情获取(Sina/腾讯/akshare) + 指标计算 |
| 数据 | `factors/` | RSI/MACD/ATR/AO/MA 独立因子计算 |
| 中心 | `state_center.py` | 统一状态中心：持仓、订单、信号、子账户隔离 |

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

## 环境

- Python 3.11 + macOS
- 同花顺客户端 + EvolvingSim
- 依赖：backtrader, akshare, evolving

## 安装

```bash
git clone git@github.com:lujie001122/quant.git
cd quant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 使用方法

### 实盘交易

```bash
# 全自动交易（cron 调度）
python3 signal_generator.py --execute

# 仅生成信号，不执行
python3 signal_generator.py

# 手动下单
python3 trade.py buy 159516 100 1.234
python3 trade.py sell 159516 100 1.250
python3 trade.py t0_buy 588170 200 1.100 1.120   # 做T买入+配对卖出
python3 trade.py t0_sell 588170 200 1.120 1.100   # 做T卖出+配对买入
python3 trade.py revoke_all                        # 全撤

# 轮动选池
python3 rotation.py

# 舆情检测
python3 sentiment_check.py
```

### 回测

```bash
# 完整回测
python3 backtest_bt.py

# 做T触发率专项回测
python3 backtest_t0_trigger_rate.py
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

## 回测结果

| 周期 | 收益 | 回撤 | 夏普 | 交易 |
|------|------|------|------|------|
| 2年 | +125.67% | 18.55% | 1.84 | 48 |
| 2026年(8M) | +20.61% | 7.88% | 1.70 | 16 |
| 2025年 | +8.49% | 6.81% | 0.57 | 24 |
| 2024年 | +11.18% | 7.71% | 0.66 | 16 |

## 策略

### 建仓（6通道，price > MA20）
RSI抄底 / MACD金叉追涨 / 突破追 / 分批建仓 / 确认加仓 / 试探仓

### 出场
均价止损12% / 保本止盈 / 分级止损(3级) / 移动止盈 / 硬止盈30%

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

## 重构历史

### v5.0 (2026-08-14) — 5层+1中心架构

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

### v4.0 (2026-08-11) — 4层架构

- 4层架构重构：market_data / strategy / signal_generator / trade
- 消除全部重复代码（RSI/MACD/ATR只写一次）
- 回测引用 strategy，验证 +20.61% 无退化
- 删除废弃文件 intraday_t_once.py
- 做T MA20斜率放宽 0.1%→0.3%（买）/0.5%（卖）