# ETF量化交易系统

5只ETF共享220k资金池，macOS全自动量化交易。回测+实盘一体化。

## 环境要求

- Python 3.11
- macOS（同花顺客户端依赖）
- 依赖见 `requirements.txt`

## 安装

```bash
git clone git@github.com:lujie001122/quant.git
cd quant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

验证安装：`python3 backtest_bt.py --period=3m`

## 核心文件

| 文件 | 用途 | 运行方式 |
|------|------|----------|
| `backtest_bt.py` | 回测引擎（backtrader），支持 `--period=5y/3y/2y/1y/6m/3m` | `python3 backtest_bt.py --period=3y` |
| `signal_generator.py` | 日频信号生成，MACD+RSI+AO多策略 | `python3 signal_generator.py` |
| `rotation.py` | 每周一扫描20只ETF，动量TOP3+防御TOP2写入 `etf_pool.json` | `python3 rotation.py` |
| `sentiment_check.py` | 舆情检测，动态读取 `etf_pool.json` 关键词，利空拦截交易 | `python3 sentiment_check.py` |
| `ths_client.py` | 同花顺 EvolvingSim 下单（模拟账户），买卖/持仓查询 | `python3 ths_client.py buy/sell` |
| `intraday_t_once.py` | 日内做T，RSI+MACD+5分钟量比 | `python3 intraday_t_once.py` |
| `batch_test.py` | 批量回测，多周期对比 | `python3 batch_test.py` |

## 配置文件

| 文件 | 说明 |
|------|------|
| `etf_pool.json` | 轮动选池，回测/信号/舆情均动态读取。`rotation.py` 写入，含codes/names/sids/momentum/max_drawdown/type |
| `portfolio.json` | 持仓记录，交易系统读写 |
| `pending_orders.json` | 待成交订单 |
| `sentiment_block.json` | 舆情拦截状态 |
| `.env` | 公众号凭证（AppID/AppSecret），需自行创建，已 gitignore |

## 数据源

腾讯前复权K线接口（通过 akshare）。

## 当前标的（2026-08-04）

| 代码 | 名称 | 类型 | 动量 |
|------|------|------|------|
| 512690 | 酒ETF | 动量 | 12.82% |
| 517090 | 共赢ETF | 动量 | 9.27% |
| 512170 | 医疗ETF | 动量 | 8.55% |
| 159611 | 电力ETF | 防御 | 6.74% |
| 159766 | 旅游ETF | 防御 | 5.12% |

## 回测结果（当前标的，2026-08-04）

| 周期 | 总收益 | 年化 | 最大回撤 | 夏普 | 胜率 | 盈亏比 | 交易笔数 |
|------|--------|------|----------|------|------|--------|----------|
| 3年 (2023-08→2026-07) | -13.99% | -4.92% | 15.10% | -0.58 | 28.6% | 1.34 | 49 |
| 1年 (2025-08→2026-07) | -3.33% | -3.38% | 10.04% | -0.57 | 14.3% | 0.85 | 21 |
| 3个月 (2026-04→2026-07) | +4.57% | +19.62% | 2.56% | 1.25 | 0.0% | 0.00 | 4 |

## 策略概述

- **入场**：RSI抄底 / MACD金叉追涨 / 突破追 / 分批建仓 / 确认加仓 / 试探仓（6通道）
- **出场**：均价止损12% / 保本止盈 / 分级止损（破MA20→DIF<0→MACD恶化） / 移动止盈 / 硬止盈30%
- **资金**：共享资金池220k，每只ETF占20%，清仓冷却3天，MA20趋势过滤
- **做T**：RSI软评分+MACD+5分钟量比，仓位20-30%，同方向30分钟冷却

## 给AI智能体的使用说明

如果你是Claude Code、Codex、Hermes等AI智能体，拿到本仓库后：

1. 阅读本 README 了解项目结构
2. 配置 `.env` 文件（公众号 AppID/AppSecret）
3. 先跑 `python3 backtest_bt.py --period=3m` 验证环境正常
4. 修改代码后跑回测验证，不要直接编辑 `portfolio.json` 和 `etf_pool.json`
5. 改坏了 `git checkout` 回退
6. `.env` / `portfolio.json` / `pending_orders.json` 不上传 GitHub

### 注意事项

- 同花顺下单直接调用 `EvolvingSim`，不激活窗口
- 持仓查询用 `e.getHoldingShares()`，不调用 `_activate()`
- 数据源优先级：腾讯前复权接口 > akshare，不用 Sina
- 策略参数用比例，不改绝对数值