# ETF量化交易系统 v9.0

每周轮动 + 集中持仓 + 4通道入场 + 移动止盈 + 阶梯锁利 + 全自动AI Agent执行

---

## 回测命令

```bash
source .venv/bin/activate
python3 backtest_bt.py --concentrated --weekly-rotation --rotation-interval 5 \
    --trend-entry --enhanced-trend \
    --init-pct=0.95 --top-n=6 --trail=e1 --lookback=14 \
    --rebalance=2 --momentum=simple --stop-loss=0.06
```

## 回测基线（2024-07-01 → 2026-06-30）

| 周期 | 总收益 | 年化 | 回撤 | 夏普 |
|------|:---:|:---:|:---:|:---:|
| 3年 | +33.17% | +10.02% | 24.78% | 0.56 |
| 2年 | +71.35% | +30.95% | 19.33% | 1.37 |
| 1年 | +9.44% | +9.47% | 14.31% | 0.48 |
| 9个月 | +10.77% | +14.72% | 13.79% | 0.72 |
| 6个月 | +18.76% | +41.72% | 5.29% | 2.47 |

详细数据：`python3 backtest_bt.py --concentrated --weekly-rotation --rotation-interval 5 --trend-entry --enhanced-trend --init-pct=0.95 --top-n=6 --trail=e1 --lookback=14 --rebalance=2 --momentum=simple --stop-loss=0.06 --start=YYYY-MM-DD --end=YYYY-MM-DD`

---

## 策略逻辑

### 轮动

每5个交易日（周轮动）从26只候选ETF中重算动量排名，选TOP6。

### 建仓（轮动日，4通道按优先级）

| 优先级 | 通道 | 条件 |
|:---:|------|------|
| 1 | 趋势建仓 | `--trend-entry` 开启，多头排列 + RSI<65 → 50% |
| 2 | RSI抄底 | RSI≤55 + MACD金叉 |
| 3 | 趋势跟踪 | 空仓>5天 + MACD红柱 |
| 4 | 突破入场 | 10日新高 + MACD金叉 |

### 加仓（轮动日）

- 趋势加仓：持仓+在TOP + 浮盈3%-20% + 多头排列 → 追加半仓

### 减仓/退出（每日）

| 类型 | 条件 | 动作 |
|------|------|------|
| 移动止盈 e1 | 激活线5%，ATR自适应回撤5%/7%/10% | 清仓 |
| 分级止损 | 3级（MA20→DIF→MACD） | 减仓/清仓 |
| 硬止损 | 峰值回撤25% | 清仓 |
| 硬止盈 | 浮盈60% | 清仓 |
| 增强趋势止盈 | MACD红柱缩短+RSI<60 | 卖20%活动仓 |
| 破MA5卖 | RSI>50+破MA5 | 卖5%活动仓 |
| 大盘回撤保护 | 510300回撤>5%/8%/12% | 减仓30%/50%/70% |
| 浮盈止损上移 | 浮盈>5%→保本线，>10%→成本+5% | 止损线上移 |

### 不再做的事

- 跌出TOP不主动清仓（对齐实盘）
- 不做网格（集中模式仓位已满不触发）
- 不做Test抄底/试探建仓（通道5-6极少触发）

---

## 参数

| 参数 | 值 | 说明 |
|------|:---:|------|
| top_n | 6 | 集中持仓TOP几只 |
| init_pct | 0.95 | 建仓比例 |
| trail | e1 | 移动止盈模式（e1=ATR自适应） |
| e1_activation | 0.05 | 激活线5% |
| e1_lockin | 0.12 | 锁仓线12% |
| momentum | simple | 动量评分（simple=简单涨幅） |
| lookback | 14 | 动量回看期 |
| rotation_interval | 5 | 轮动间隔（5天=周轮动） |
| stop_loss | 0.06 | 止损基础比例 |
| rebalance | 2 | 重平衡周期 |

---

## 架构

```
market_data.py          行情获取(腾讯前复权) + 指标计算(RSI/MACD/ATR)
        │
        ▼
backtest_bt.py          回测引擎 → 调用实盘信号管线
        │
        ├──▶ risk_manager.py       止损止盈评估
        └──▶ strategies/rsi_macd.py 建仓入场
```

---

## 文件清单

| 文件 | 行数 | 职责 |
|------|:---:|------|
| backtest_bt.py | 2100+ | 回测引擎 |
| signal_generator.py | 600+ | 实盘信号生成 |
| executor.py | 1000+ | 信号执行 |
| trade.py | 540+ | 同花顺交易接口 |
| market_data.py | 560+ | 行情+指标 |
| risk_manager.py | 370+ | 止损止盈 |
| strategies/rsi_macd.py | 240+ | 建仓策略 |
| strategies/t0.py | 150+ | 做T策略 |
| rotation.py | 940+ | 轮动选池 |
| config.yaml | 140+ | 全部参数 |

---

## 部署

### 定时任务（8个）

| 时间 | 任务 |
|------|------|
| 8:00 每日 | 早间舆情 |
| 9:00 工作日 | 全撤+重启同花顺 |
| 10:00-14:30 每30分 | 交易信号监控 |
| 10:11-14:41 每30分 | 做T信号 |
| 13:00 工作日 | 重启同花顺（不撤单） |
| 17:00 每日 | 晚间复盘 |
| 21:00 每日 | GitHub备份 |
| 每月1号 | 轮动选池 |