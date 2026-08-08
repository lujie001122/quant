# ETF量化交易系统 v3.2

7只ETF共享220k资金池，macOS全自动量化交易。回测+实盘一体化。

## 环境要求

- Python 3.11
- macOS（同花顺客户端 + EvolvingSim 依赖）
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
| `tracker.py` | 共享模块，统一读取 `etf_pool.json` + `portfolio.json`，所有脚本共用 | 被其他脚本 `import` |
| `signal_generator.py` | 信号生成核心：建仓/止损/做T配对，RSI+MACD+ATR+MA20多策略 | `python3 signal_generator.py` |
| `trade.py` | 统一下单工具（固化）：sync/buy/sell/t0_buy/t0_sell/revoke | `python3 trade.py sync` |
| `backtest_bt.py` | 回测引擎（backtrader），支持 `--start/--end` 自定义区间 | `python3 backtest_bt.py --start=2026-01-01 --end=2026-08-07` |
| `sentiment_check.py` | 舆情检测 v4.0，三源（新浪+东财+同花顺）60+新闻，全局+行业关键词 | `python3 sentiment_check.py` |
| `rotation.py` | 每周一轮动扫描，动量TOP3+防御TOP2，输出建议不自动写池 | `python3 rotation.py` |
| `intraday_t_once.py` | 独立做T信号脚本，与 signal_generator 做T逻辑互补 | `python3 intraday_t_once.py` |
| `batch_test.py` | 批量回测工具，多周期对比 | `python3 batch_test.py` |

## 配置文件

| 文件 | 说明 |
|------|------|
| `etf_pool.json` | **唯一标的出口**，含 codes/names/sids/momentum/max_drawdown/type/industry_kw。`tracker.py` 自动合并 `portfolio.json` 持仓 |
| `portfolio.json` | 持仓记录+信号状态持久化，交易系统读写 |
| `sentiment_block.json` | 舆情拦截状态 |
| `.env` | 公众号凭证（AppID/AppSecret），需自行创建，已 gitignore |

## 数据源

- 日线K线：腾讯前复权接口（通过 akshare）
- 5分钟K线：腾讯接口 `ifzq.gtimg.cn`
- 实时行情：新浪接口
- 注意事项：5分钟K线仅支持当日数据，历史回测需用 akshare

## 当前标的（2026-08-08）

| 代码 | 名称 | 类型 | 动量 |
|------|------|------|------|
| 159516 | 半导体设备ETF | 动量 | 7.8% |
| 515880 | 通信ETF | 动量 | 6.5% |
| 588170 | 科创半导体ETF | 动量 | 6.2% |
| 159532 | 中证2000ETF | 防御 | 5.4% |
| 515050 | 中证全指ETF | 防御 | 4.8% |
| 159611 | 电力ETF | 防御 | 4.2% |
| 512170 | 医疗ETF | 防御 | 3.8% |

## 回测结果（2026-08-08 更新）

| 周期 | 总收益 | 年化 | 最大回撤 | 夏普 | 交易笔数 |
|------|--------|------|----------|------|----------|
| 3个月 (5/8→8/7) | -4.99% | -18.56% | 12.48% | -1.18 | 8 |
| 5个月 (3/1→8/7) | +14.31% | +35.94% | 10.88% | 1.27 | 14 |
| 8个月 (1/1→8/7) | +20.61% | +36.85% | 7.88% | 1.70 | 16 |

## 策略概述

### 建仓策略（6通道）
- RSI抄底 / MACD金叉追涨 / 突破追 / 分批建仓 / 确认加仓 / 试探仓
- 全通道强制 MA20 趋势过滤（price > MA20）
- 资金：共享资金池220k，单只ETF上限50%

### 出场策略
- **均价止损12%**：买入均价跌12%无条件清仓
- **保本止盈**：浮盈≥10%激活保本线，回落至成本+2%清仓
- **分级止损**：破MA20→DIF<0→MACD恶化（3级递进）
- **移动止盈**：8%基础+15%峰值回撤5%
- **硬止盈30%**：基准价×1.30清仓

### 做T策略（v2026-08-08 最终版）
- **数据源**：5分钟分时K线，所有指标（ATR/RSI/MACD）均基于5分钟数据
- **评分**：RSI<45买入/RSI>55卖出，3项共振（RSI+MACD+量比）≥2分触发
- **极端兜底**：RSI>75仅卖出，RSI<25仅买入
- **趋势过滤**：5分钟MA20斜率>0.1%禁T卖，<-0.1%禁T买
- **配对挂单**：ATR×1.1价差，数量=总持仓×30%，价差×量>10元
- **运行时间**：11:00后（等5分钟K线积累），每日2-3次

## 定时任务

| 任务 | 频率 | 时间 |
|------|------|------|
| 早间舆情→小红书 | 每天 | 8:00 |
| ETF交易监控 | 工作日 | 10:00-15:00 每20分 |
| ETF做T信号 | 工作日 | 11:00-14:30 每30分 |
| 晚间复盘→小红书 | 每天 | 17:00 |
| 轮动选池（建议） | 每周一 | 8:00 |
| GitHub备份 | 每天 | 21:00 |

## 2026-08-08 改动日志

### 做T策略重构
- RSI阈值放宽到45/55，增加极端兜底（RSI>75卖/<25买）
- 配对挂单ATR改为5分钟数据（非日线），乘数1.1，数量30%
- 新增MA20斜率趋势过滤（单边市不做T）
- 11:00后才运行做T（等5分钟K线积累）
- 做T独立cron，与交易监控分离

### 数据源错配修复
- trade.py ATR估算从`price*2%`改为从腾讯接口拉取5分钟K线计算
- 做T频率控制从日线ATR改为5分钟ATR
- 配对乘数从2.0统一为1.1
- 策略描述文档与实际代码对齐

### 回测实盘对齐
- 均价止损10%→12%（与回测一致）
- 新增保本止盈逻辑（浮盈≥10%激活保本线）
- MA20全通道过滤（6个建仓通道全部强制）
- 卖出比例统一（趋势止盈10%，破MA5卖5%）

### 跨文件一致性修复
- etf_pool.json 从6个标的扩展到7个（增加512170医疗ETF）
- Shell脚本路径从旧`~/hermes/scripts`更新到当前路径
- 冷却期从2天/3天不一致统一为2天
- 文档描述与实际代码对齐

### 代码清理
- 删除6个无效文件（历史快照、分析工具、废弃ths_client）
- 项目精简到14个核心文件

## 给AI智能体的使用说明

1. 阅读本 README 了解项目结构
2. 配置 `.env` 文件（公众号 AppID/AppSecret）
3. 先跑 `python3 backtest_bt.py --period=3m` 验证环境正常
4. 修改代码后跑回测验证不退化
5. **标的修改**：只改 `etf_pool.json`，`tracker.py` 自动同步到所有脚本
6. 不要直接编辑 `portfolio.json`（程序自动维护）
7. 改坏了 `git checkout` 回退
8. `.env` / `portfolio.json` 不上传 GitHub

### 注意事项

- 同花顺下单通过 `trade.py`，不直接调 EvolvingSim
- `trade.py` 每次操作前自动清理 `/tmp/lock.json`
- 做T所有指标用5分钟数据，严禁用日线数据
- 回测 `backtest_bt.py` 不含做T模块（日线级策略），做T需单独分析
- 策略参数用比例，不改绝对数值