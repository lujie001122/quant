# ETF量化交易系统 v3.5

> 5只ETF共享22万资金池，macOS全自动交易。回测5年+94%，最大回撤13%。

## 安装部署

```bash
git clone git@github.com:lujie001122/quant.git
cd quant
python3 -m venv .venv
source .venv/bin/activate
pip install akshare backtrader pandas numpy
python3 backtest_bt.py   # 验证回测
```

## 文件说明

| 文件 | 用途 | 运行方式 |
|------|------|---------|
| `backtest_bt.py` | 回测引擎 | `python3 backtest_bt.py` |
| `signal_generator.py` | 信号生成 | `python3 signal_generator.py` |
| `rotation.py` | 每周选池 | `python3 rotation.py` |
| `intraday_t_once.py` | 日内做T | `python3 intraday_t_once.py` |
| `sentiment_check.py` | 舆情扫描 | `python3 sentiment_check.py` |
| `ths_client.py` | 同花顺下单 | `python3 ths_client.py buy/sell` |
| `etf_pool.json` | 当前标的池 | rotation.py 写入 |
| `portfolio.json` | 持仓状态 | 交易系统写入 |
| `.env` | 微信凭证 | 需自行创建(已gitignore) |

## 运行流程

```
每周一 8:00   rotation.py           → 扫描20只ETF，选TOP5写入 etf_pool.json
工作日 8:30   sentiment_check.py    → 舆情扫描，利空则拦截当日交易
工作日 10-14  signal_generator.py   → 每30分钟跑信号，有买点→ths_client.py下单
每天   20:00  复盘任务              → 持仓+舆情汇总
每天   21:00  git_backup.sh         → GitHub备份
```

## 策略参数

**入场(6通道)**: RSI抄底30% / MACD金叉追涨30% / 突破追30% / 分批建仓15% / 确认加仓70% / 试探10%

**出场**: 均价止损12% / 保本止盈(浮盈>10%后成本价+2%触发) / 分级止损(破MA20→DIF<0→MACD恶化) / 移动止盈(涨8%后回撤5%) / 硬止盈30%

**做T**: RSI软评分+MACD+5分钟量比，仓位20-30%，同方向30分钟冷却

**资金**: 共享资金池22万，清仓后冷却3天，MA20趋势过滤

## 回测结果

| 周期 | 总收益 | 年化 | 回撤 | 夏普 | 笔数 | 胜率 |
|------|--------|------|------|------|------|------|
| 5年 | **+94.21%** | +14.23% | 13.35% | 0.75 | 46 | 47.8% |
| 3年 | +94.21% | +24.94% | 13.35% | 1.04 | 46 | 47.8% |
| 2年 | **+128.81%** | +51.70% | 14.67% | 1.75 | 23 | 56.5% |
| 1年 | +11.11% | +11.27% | 9.53% | 0.60 | 15 | 46.7% |

过拟合CV=0.16，策略稳定。

## 技术要求

- macOS + Python 3.11
- 同花顺客户端(模拟交易界面保持前台，不切换窗口)
- Evolving库(同花顺Python接口)
- 微信公众号订阅号(用于发文章草稿，需AppID/AppSecret)

## 给AI Agent的指令

如果你是一个AI Agent，拿到这个仓库后:

1. 阅读本README了解项目结构
2. 所有代码修改通过Claude Code完成，不直接编辑文件
3. 策略参数用比例，不改绝对数值
4. 改动后跑 `python3 backtest_bt.py` 验证回测
5. 改坏了 `git checkout` 回退
6. `.env` 和 `portfolio.json` 不上传GitHub

### 重要规则

- 同花顺下单: 直接调用 `EvolvingSim`，不激活窗口(用户已保持交易界面)
- 持仓查询: `e.getHoldingShares()` — 不调用 `_activate()`
- 数据源: 腾讯前复权接口 > akshare，不用Sina
- 过拟合检测: 6个不同起点跑2年，CV<0.5才安全