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
| `tracker.py` | 共享模块，统一读取 `etf_pool.json` + `portfolio.json`，信号/舆情/回测共用 | 被其他脚本 `import` |
| `backtest_bt.py` | 回测引擎（backtrader），支持 `--period=5y/3y/2y/1y/6m/3m` | `python3 backtest_bt.py --period=3y` |
| `signal_generator.py` | 日频信号生成，MACD+RSI+AO多策略，通过 `tracker` 读取标的 | `python3 signal_generator.py` |
| `sentiment_check.py` | 舆情检测 v4.0，三源（新浪+东财+同花顺）60+新闻，行业关键词匹配 | `python3 sentiment_check.py` |
| `trade.py` | EvolvingSim 下单工具，buy/sell/sync 一命令执行 | `python3 trade.py buy/sell/sync` |
| `rotation.py` | 每周一扫描20只ETF，动量TOP3+防御TOP2写入 `etf_pool.json` | `python3 rotation.py` |
| `intraday_t_once.py` | 日内做T，RSI+MACD+5分钟量比 | `python3 intraday_t_once.py` |
| `batch_test.py` | 批量回测，多周期对比 | `python3 batch_test.py` |

## 配置文件

| 文件 | 说明 |
|------|------|
| `etf_pool.json` | **唯一标的出口**，含 codes/names/sids/momentum/max_drawdown/type/industry_kw。`tracker.py` 自动合并 `portfolio.json` 持仓 |
| `portfolio.json` | 持仓记录+信号状态，交易系统读写 |
| `pending_orders.json` | 待成交订单 |
| `sentiment_block.json` | 舆情拦截状态 |
| `.env` | 公众号凭证（AppID/AppSecret），需自行创建，已 gitignore |

## 数据源

腾讯前复权K线接口（通过 akshare）。

## 当前标的（2026-08-05）

| 代码 | 名称 | 类型 | 动量 |
|------|------|------|------|
| 159516 | 半导体设备ETF | 动量 | 12.82% |
| 515880 | 通信ETF | 动量 | 9.27% |
| 588170 | 科创半导体ETF | 动量 | 8.55% |
| 159532 | 中证2000ETF | 防御 | 6.74% |
| 515050 | 中证全指ETF | 防御 | 5.12% |

## 回测结果（当前标的，2026-08-05）

| 周期 | 总收益 | 年化 | 最大回撤 | 夏普 | 胜率 | 盈亏比 | 交易笔数 |
|------|--------|------|----------|------|------|--------|----------|
| 3年 (2023-08→2026-08) | +105% | +27% | 18% | 1.2 | 42% | 2.1 | 62 |

## 策略概述

- **入场**：RSI抄底 / MACD金叉追涨 / 突破追 / 分批建仓 / 确认加仓 / 试探仓（6通道）
- **出场**：均价止损12% / 保本止盈 / 分级止损（破MA20→DIF<0→MACD恶化） / 移动止盈 / 硬止盈30%
- **资金**：共享资金池220k，每只ETF占20%，清仓冷却3天，MA20趋势过滤
- **做T**：RSI软评分+MACD+5分钟量比，仓位20-30%，同方向30分钟冷却

## 2026-08-05 改动日志

### 1. 标的统一（tracker.py 共享模块）

- `tracker.py` 成为所有脚本的单一数据源
- `etf_pool.json` 为唯一出口，自动合并 `portfolio.json` 中实际持仓
- `signal_generator.py` / `sentiment_check.py` / `backtest_bt.py` 统一通过 `tracker` 读取
- 回退值全部同步在一处（`_DEFAULT_CODES` / `_DEFAULT_NAMES` / `_DEFAULT_SIDS`）

### 2. 选池切回旧标的

标的从轮动标的（512690/517090/512170/159611/159766）切回：
- 159516 半导体设备ETF
- 515880 通信ETF
- 588170 科创半导体ETF
- 159532 中证2000ETF
- 515050 中证全指ETF

回测 3 年 +105%（原轮动标的 -14%）。

### 3. stop_level=2 卡死修复

- 问题：`stop_level` 升到 2 后，若 MACD 未死叉/绿柱放大，信号永久卡在减仓状态
- 修复：`signal_generator.py` L848-851，`stop_level=2` 但 MACD 非死叉/非绿柱放大时自动重置为 0
- 回测同步修复：`backtest_bt.py` L486-487，价格回 MA20 上方 + DIF>0 + MACD 红柱时重置

### 4. 持仓标的纳入监控

- 159532 中证2000ETF 58600股（实际持仓）纳入所有信号通道（做T/买入/卖出）
- `position_ratio` 从 0% 改为实际 44%（基于持仓市值/总资产）
- `tracker.get_tracked_codes()` 自动合并 `portfolio.json` 的 positions keys

### 5. 舆情系统 v4.0

- 三源新闻抓取：新浪滚动 + 东财要闻 + 同花顺快讯，合并去重 60+ 条
- 行业关键词：`etf_pool.json` 新增 `industry_kw` 字段（光模块/CPO/制裁/5G/6G等）
- 利空/利好分词评分，阈值：≥3 关注，≥6 拦截
- 成功抓取美国禁运光模块新闻

### 6. 下单工具 trade.py 固化

- 独立 Python 脚本，`python3 trade.py sync|buy|sell|revoke`
- buy/sell 参数：`code shares price`（无需 `--` 前缀）
- EvolvingSim 调用顺序 `buy(code, amount, price)` / `sell(code, amount, price)` 验证通过
- sync 自动同步持仓到 `portfolio.json`

### 7. 今日盘中信号

| 标的 | 操作 | 数量 | 价格 | 状态 |
|------|------|------|------|------|
| 159532 | 网格卖出 | 11,700股 | 1.570 | 已成交 |
| 512170 | 加仓 | 39,100股 | — | 已成交（累计156,800股） |
| 515050 | 买入 | 6,600股 | — | 挂单中 |

### 8. 小红书发布规范

- 调用方式：Python heredoc 调 `publish_image()`，不用 CLI `--content`
- 封面：HTML + Playwright 生成专属封面图
- 配合 `etf-xiaohongshu-posting` skill 自动发布

## 给AI智能体的使用说明

如果你是Claude Code、Codex、Hermes等AI智能体，拿到本仓库后：

1. 阅读本 README 了解项目结构
2. 配置 `.env` 文件（公众号 AppID/AppSecret）
3. 先跑 `python3 backtest_bt.py --period=3m` 验证环境正常
4. 修改代码后跑 `python3 backtest_bt.py --period=3y` 验证回测不退化
5. **标的修改**：只改 `etf_pool.json`，`tracker.py` 自动同步到所有脚本
6. 不要直接编辑 `portfolio.json` 和 `etf_pool.json`（程序自动维护）
7. 改坏了 `git checkout` 回退
8. `.env` / `portfolio.json` / `pending_orders.json` 不上传 GitHub

### 注意事项

- 同花顺下单直接调用 `EvolvingSim`，不激活窗口
- 持仓查询用 `e.getHoldingShares()`，不调用 `_activate()`
- 数据源优先级：腾讯前复权接口 > akshare，不用 Sina
- 策略参数用比例，不改绝对数值
- **stop_level=2 卡死**：若发现信号长期卡在减仓，检查 MACD 状态是否触发自动重置（L848-851）
- **舆情阈值**：`NEG_THRESHOLD_WARN=3` / `NEG_THRESHOLD_BLOCK=6`，仅在 `sentiment_check.py` 内调整