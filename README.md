# ETF量化交易系统 v3.1.5

5只ETF共享资金池量化策略，22万共享资金池 + 单只4.4万上限。

## 标的池

| 代码 | 名称 |
|------|------|
| 159516 | 半导体设备ETF |
| 515880 | 通信ETF |
| 588170 | 科创半导体ETF |
| 159532 | ETF |
| 515050 | 中证全指ETF |

## 策略概要

- **6通道入场**: RSI抄底/趋势跟踪/突破入场/分批建仓/Test抄底/试探
- **分级止损**: 均价止损10% → 硬止盈30% → 移动止盈 → 分级止损 → 硬止损20%
- **做T**: T+0日内交易（RSI+MACD+量比评分系统）

## 回测结果（5年）

| 指标 | 数值 |
|------|------|
| 总收益 | +91.10% |
| 年化收益 | +13.87% |
| 最大回撤 | 19.11% |
| Calmar比率 | 4.77 |
| 夏普比率 | 1.16 |
| 胜率 | 32.8% |
| 盈亏比 | 6.41 |

## 文件说明

| 文件 | 说明 |
|------|------|
| backtest_5etf_1m.py | 回测引擎 |
| signal_generator.py | 实盘信号生成器 |
| intraday_t_once.py | 做T模块 |
| sentiment_check.py | 舆情检测 |
| ths_client.py | 同花顺交易客户端 |
| etf_premarket.sh | 盘前信号脚本 |
| etf_intraday.sh | 盘中监控脚本 |
| portfolio.json | 持仓状态 |
| skill/SKILL.md | 策略提示词（完整版） |
| references/ | 策略详情/审计报告/配置指南 |

## 运行环境

- Python 3.11, venv: ~/hermes-trading/.venv/
- 数据源: Sina API (主) + akshare (备)
- 交易执行: 同花顺 + Evolving (macOS)
