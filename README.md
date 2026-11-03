# ETF 量化交易系统

集中轮动 + 金字塔加仓 + 网格 + 弹性做T + 全自动执行（同花顺模拟盘）

实盘信号与回测共用同一套判定逻辑，回测结果可直接对照实盘。

---

## 回测

```bash
source .venv/bin/activate
python3 backtest_bt.py --concentrated-pyramid --weekly-rotation --rotation-interval 5 \
    --trend-entry --init-pct=0.30 --top-n=3 --trail=e1 \
    --lookback=10 --momentum=simple --stop-loss=0.06 \
    --start=2024-07-01 --end=2026-10-21
```

### 基线（2024-07-01 → 2026-10-21）

| 指标 | 数值 |
|------|:---:|
| 总收益 | +73.00% |
| 年化 | +26.82% |
| 最大回撤 | 17.43% |
| Calmar | 1.54 |
| 夏普 | 1.14 |
| 交易笔数 | 250 |
| 胜率 | 37.9% |

改动策略或风控代码后，必须用这条命令复核，数字偏离基线要能解释清楚。

---

## 策略逻辑

### 选池与轮动

从 `etf_pool.json` 的候选池按动量排名，每 5 个交易日（周轮动）重算一次，取 TOP3 集中持有。

持仓中不在 TOP3 的标的：不清仓（对齐实盘），但不再加仓。

### 建仓（4 通道，按优先级）

所有通道都要求多头排列（MA5 > MA20），且当日买入次数未用完。

| 通道 | 条件 | 首笔比例 |
|------|------|:---:|
| 1 RSI 抄底 | MACD 金叉 + RSI≤55 | 30% |
| 2 趋势跟踪 | 空仓超 5 天 + MACD 红柱 | 30% |
| 3 突破入场 | 10 日新高 + MACD 金叉 | 30% |
| 4 分批建仓 | MACD 金叉/红柱放大 + RSI>40 + 站上 MA5 | 30% |

### 加仓（金字塔）

首笔 30% 后分两种路径：

- 逆势补仓：跌破首笔价 3% 且 MACD 不坏、RSI<40 → 加 15%
- 确认加仓：突破首笔价，或回踩 MA5 站稳两次 → 每次 30%，最多 2 次，间隔 1 个交易日

AO 指标是确认加仓的硬门槛；横盘超过 5 个自然日按 `entry.ao_timeout_days` 放行。

单只最多加仓 5 次，仓位上限 50%。

### 门控（entry_gate）

沪深300（510300）十日动量为负时，禁止新建仓，存量管理不受影响。

```yaml
entry_gate:
  enabled: true
  index_code: '510300'
  momentum_days: 10
```

### 网格

持仓区间内高抛低吸，网格价按基准价逐档展开。

- 间距按 ATR 动态：`0.6 × 基础间距(默认 2.5%) + 0.4 × ATR%`，限制在 1.25%~5%
- 触达容差为间距的 15%（`grid_tolerance`）
- 卖出 3 档，买入 5 档；跌破第 5 档冻结，等站上 MA5 解冻
- 单日最多买入 1 次

### 止盈

| 类型 | 条件 | 动作 |
|------|------|------|
| 移动止盈 e1 | 浮盈 3.5% 激活，7% 锁仓 | 回撤触发清仓 |
| 阶梯锁利 | 浮盈 30%/45%/55% | 各减 10%/15%/20% |
| 浮盈保护 | 浮盈 5% 后，止损线上移到成本+0.5% | 上移止损 |
| 硬止盈 | 浮盈 60% | 清仓 |

### 止损

| 档位 | 条件 | 动作 |
|------|------|------|
| 分档止损 | 仓位>80% → 7%；50-80% → 10%；<50% → 12% | 减仓 |
| 硬止损 | 峰值回撤 18% | 清仓 |
| 分级减仓 | 破 MA20 → 30%；DIF<0 → 30%；趋势恶化 | 清仓 |

### 做T

同一标的内当日对冲，赚价差。

- 买入后立刻挂配对卖出，卖出后立刻挂配对买入，双边都要挂
- 价差下限 150 元（`t0.min_spread`），配对价按 ATR×1.1 计算
- 每日 3 次（`max_daily_t0`），额度只在成交确认后扣
- 5 分钟级别信号：RSI + MACD + 量比
- 尾盘 14:50 强制平仓，平不掉的告警

---

## 实盘链路

```
signal_generator.py   生成信号（与回测同一套判定）
        │
        ▼
executor.py           去重、下单、成交核验、落库
        │
        ▼
trade.py              同花顺 EvolvingSim 下单
        │
        ▼
state_center.py       portfolio.json / orders/ 落盘
```

### 下单核验（必须）

下单成功不看返回码，只看委托表。

下单后轮询当日委托表（最多 5 次 × 2 秒），匹配到「代码 + 方向 + 数量 + 合同号」才算成功。

查不到、或接口异常，一律判失败：非零退出、不扣做T额度、不写成交记录，并提示人工核对。

### 数据一致性

- `trade.py sync` 会校验「总资产」与「可用金额 + 市值」，偏差超过 2% 拒绝写盘
- 空数据一律跳过，不清空持仓
- 持仓以同花顺为准，本地只是镜像

---

## 参数（config.yaml）

| 参数 | 值 | 说明 |
|------|:---:|------|
| concentrated_top_n | 3 | 集中持仓只数 |
| max_daily_t0 | 3 | 每日做T次数 |
| max_daily_buys | 1 | 每日买入次数 |
| grid_tolerance | 0.15 | 网格触发阈值 |
| entry.position_cap | 0.5 | 单只仓位上限 |
| entry.ao_timeout_days | 5 | AO 横盘放行天数 |
| entry.rsi_min | 40 | 加仓最低 RSI |
| stop_loss.tier1_pct | 0.07 | 高仓位止损线 |
| stop_loss.hard_stop_pct | 0.18 | 硬止损 |
| profit_take.e1_activation | 0.035 | 移动止盈激活线 |
| ladder_profit | 0.10/0.15/0.20 | 阶梯锁利比例 |
| t0.min_spread | 150 | 做T最小价差（元） |
| t0.pair_ratio | 0.3 | 做T配对比例 |
| trade.min_shares | 100 | 最小交易单位 |
| strategy_v2.enabled | false | 实验特性开关 |

---

## 文件清单

| 文件 | 行数 | 职责 |
|------|:---:|------|
| backtest_bt.py | 2415 | 回测引擎（backtrader） |
| executor.py | 1632 | 信号执行与核验 |
| state_center.py | 1016 | 状态与订单落盘 |
| signal_generator.py | 988 | 实盘信号生成 |
| scripts/t0_close.py | 994 | 尾盘T0平仓 |
| rotation.py | 934 | 轮动选池 |
| trade.py | 615 | 同花顺交易接口 |
| strategies/t0.py | 589 | 做T策略 |
| risk_manager.py | 434 | 止损止盈 |
| market_data.py | 426 | 行情与指标 |
| position_info.py | 394 | 持仓状态 |
| strategies/rsi_macd.py | 264 | 建仓策略 |
| strategies/grid.py | 234 | 网格 |
| config.yaml | 169 | 全部参数 |
| tests/ | — | 委托核验等关键路径单测 |

---

## 定时任务

| 时间 | 任务 |
|------|------|
| 08:00 每日 | 早间舆情 |
| 09:00 周一 | 轮动选池，生成调仓建议 |
| 09:30 工作日 | 全撤 + 重启同花顺 + 同步持仓 |
| 10:00-14:30 每30分 | 买卖信号监控 |
| 10:11-14:41 每30分 | 做T信号监控 |
| 13:00 工作日 | 午间重启（不撤单） |
| 14:50 工作日 | 做T尾盘平仓 |
| 17:00 每日 | 晚间复盘 |
| 21:00 每日 | GitHub 备份 |

每次下单前都先重启同花顺（不撤单），避免接口卡死。

---

## 常用命令

```bash
# 同步持仓（先确保同花顺在运行）
python3 trade.py sync

# 买卖信号（不执行）
python3 signal_generator.py

# 买卖信号（执行）
python3 signal_generator.py --execute=buy_sell

# 做T（执行）
python3 signal_generator.py --execute=t0

# 尾盘平仓
python3 scripts/t0_close.py

# 轮动建议
python3 rotation.py --weekly --top-n=3

# 舆情
python3 sentiment_check.py

# 备份
bash scripts/git_backup.sh
```

---

## 环境

- Python 3.11（`.venv`）
- 券商端：同花顺 Mac 版 + EvolvingSim（AppleScript 驱动）
- 数据：腾讯前复权为主，新浪兜底；两融/美债/原油走 akshare
- 仓库：`git@github.com:lujie001122/quant.git`

---

## 变更记录

### 2026-11-03
- 下单后强制核验委托表（5 次轮询），接口异常或未匹配一律失败，不扣做T额度
- 修复做T买单静默失败：返回码为 0 但委托表无记录，仍被判成功

### 2026-11-02
- T0 配对固化基线股数，避免 sync 覆盖后无法判断单边暴露
- 买=卖且本地持仓无变化时，改为报「无法确认」，不再断言已成交

### 2026-10-28
- 清理死代码，config.yaml 删除无引用的 market_data 段
- `t0_close` 三级降级：接口不可用时用本地持仓兜底

### 2026-10-27
- MA20 斜率修正为真正的 20 日（原为 5 日均值）
- 做T额度改为按成交计数，不再被信号占用

---

风险提示：本项目为个人交易工具，不构成投资建议。市场有风险，实盘需自行承担后果。
