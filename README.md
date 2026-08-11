# ETF量化交易系统 v4.0

4层架构，macOS全自动量化交易。回测+实盘一体化。

## 架构

```
market_data.py → strategy.py → signal_generator.py → trade.py
    (数据)        (策略)          (编排)             (交易)
```

| 层 | 文件 | 职责 |
|---|------|------|
| 数据 | `market_data.py` | 行情获取 + 指标计算（RSI/MACD/ATR/AO） |
| 策略 | `strategy.py` | 建仓/止损/做T/网格/防御判定 |
| 编排 | `signal_generator.py` | 调策略→信号→调 trade.py，`--execute` 全自动 |
| 交易 | `trade.py` | sync/buy/sell/t0_buy/t0_sell/revoke |

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

## 核心文件

| 文件 | 用途 |
|------|------|
| `market_data.py` | 行情获取 + 指标计算 |
| `strategy.py` | 策略判定 + PositionInfo |
| `signal_generator.py` | 信号编排，`--execute` 全自动 |
| `trade.py` | 下单/撤单/持仓同步 |
| `backtest_bt.py` | 回测引擎（引用 strategy） |
| `sentiment_check.py` | 舆情检测 |
| `rotation.py` | 轮动选池（含入场预检） |
| `tracker.py` | 共享配置 |

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

## 定时任务

| 任务 | 时间 | 频率 |
|------|------|------|
| 交易监控 | 10:00-15:00 | 每20分 工作日 |
| 做T监控 | 11:00-14:30 | 每30分 工作日 |
| 早间舆情 | 8:00 | 每天 |
| 晚间复盘 | 17:00 | 每天 |
| 轮动选池 | 周一 8:00 | 每周 |
| GitHub备份 | 21:00 | 每天 |

## 执行流程

```
cron → signal_generator.py --execute
  ├─ 拉行情 → 生成信号
  ├─ 检查挂单（未成交→撤旧挂新，已成交→跳过）
  ├─ 限价修正（买≤现价，卖≥现价）
  ├─ 调 trade.py 下单
  └─ AI 只通知，不干预
```

## 2026-08-11 改动

- 4层架构重构：market_data / strategy / signal_generator / trade
- 消除全部重复代码（RSI/MACD/ATR只写一次）
- 回测引用 strategy，验证 +20.61% 无退化
- trade.py 去掉行情拉取代码
- 删除废弃文件 intraday_t_once.py
- 做T MA20斜率放宽 0.1%→0.3%（买）/0.5%（卖）
- 挂单检查：未成交挂单自动撤旧挂新
- 防御过滤激活：DEFENSE_CODE=159611
- 同花顺激活改用 open -a