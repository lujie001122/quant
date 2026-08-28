# ETF轮动量化交易系统 v8.0

集中TOP3动量ETF，C2多因子评分选股，50%建仓，12%移动止盈，RSI+MACD入场，全自动AI Agent执行。

---

## 快速部署（从零开始）

### 1. 环境要求

- macOS + Python 3.9+
- 同花顺Mac版（需安装EvolvingSim插件，模拟账户已开通）
- Hermes Agent（用于定时任务和信号执行）

### 2. 克隆与安装

```bash
git clone git@github.com:lujie001122/quant.git
cd quant
python3 -m venv .venv
source .venv/bin/activate
pip install backtrader akshare thsdk evolving pyyaml pandas numpy requests lxml beautifulsoup4
```

**核心依赖：**
| 包 | 用途 |
|---|------|
| backtrader | 回测引擎 |
| akshare | A股行情数据（降级数据源） |
| thsdk + evolving | 同花顺模拟交易接口 |
| pyyaml | 配置文件 |
| pandas/numpy | 数据处理 |

### 3. 同花顺配置

1. 安装同花顺Mac版，开通模拟账户
2. 安装EvolvingSim插件（同花顺→工具→插件市场）
3. 启动同花顺，登录模拟账户
4. 验证：`python3 -c "from evolving.evolving import EvolvingSim; print(EvolvingSim().getAccountInfo())"`

### 4. 首次同步

```bash
source .venv/bin/activate
python3 trade.py sync    # 从同花顺读取持仓写入portfolio.json
cat portfolio.json       # 确认持仓数据正确
```

### 5. 回测验证

```bash
source .venv/bin/activate
# 集中TOP3模式（当前实盘配置）
python3 backtest_bt.py --concentrated --start=2025-01-01 --end=2025-12-31
# 期望输出：总收益+37.85% / 回撤8.99% / 夏普2.11

# 原始等权9只模式
python3 backtest_bt.py --start=2025-01-01 --end=2025-12-31
# 期望输出：总收益+3.07% / 回撤2.15% / 夏普0.32
```

### 6. 信号测试（不下单）

```bash
python3 signal_generator.py    # 仅生成信号，不执行
```

### 7. 首次下单（需谨慎）

```bash
python3 signal_generator.py --execute=buy_sell    # 生成信号并执行交易
```

### 8. 配置Hermes定时任务

7个定时任务，按以下顺序创建：

```
1. 早间舆情    | 0 8 * * *       | python3 sentiment_check.py
2. 同花顺重启  | 0 9,13 * * 1-5  | python3 trade.py sync && python3 scripts/restart_ths.py
3. 交易监控    | 0,30 10,11,13,14 * * 1-5 | 先restart_ths --no-revoke，再 signal_generator.py --execute=buy_sell
4. 做T监控    | 11,41 10,11,13,14 * * 1-5 | 先restart_ths --no-revoke，再 signal_generator.py --execute=t0
5. 晚间复盘    | 0 17 * * *      | 读取portfolio.json + sentiment_check.py 输出复盘
6. 轮动选池    | 0 8 * * 1       | python3 rotation.py（仅建议，不自动写入etf_pool.json）
7. GitHub备份  | 0 21 * * *      | git add -A && git commit && git push
```

所有定时任务pin到glm-5.1模型，workdir设为项目路径。

---

## 标的池（etf_pool.json）

| 代码 | 名称 | 市场 | 类型 |
|------|------|------|------|
| 159516 | 半导体设备ETF | sz | 动量 |
| 515880 | 通信ETF | sh | 动量 |
| 159532 | 中证2000ETF | sz | 防御 |
| 159611 | 电力ETF | sz | 防御 |
| 515050 | 中证全指ETF | sh | 防御 |
| 588170 | 科创半导体ETF | sh | 动量 |
| 513780 | 港股创新药ETF | sh | 动量 |
| 512400 | 有色ETF | sh | 动量 |
| 515220 | 煤炭ETF | sh | 防御 |

**新增标的必须同步3处：** etf_pool.json(names/sids/type/etf_pool) + rotation.py ETF_CANDIDATES + state_center.py DEFAULT_POOL

---

## 回测基线

### 集中TOP3模式（当前实盘配置）

回测命令：`python3 backtest_bt.py --concentrated --start=YYYY-MM-DD --end=YYYY-MM-DD`

| 区间 | 总收益 | 年化收益 | 最大回撤 | 夏普 | 交易笔数 | 胜率 | 盈亏比 |
|------|--------|----------|----------|------|---------|------|--------|
| 3年(2024-2026) | +19.88% | +7.07% | 22.98% | 0.38 | 43 | 30.2% | 3.04 |
| 2年(2025-2026) | +73.55% | +39.61% | 8.99% | 2.10 | 31 | 51.6% | 4.78 |
| 1年(2025) | +37.85% | +37.98% | 8.99% | 2.11 | 17 | 29.4% | 7.39 |
| 9个月(2025.4-12) | +26.75% | +37.14% | 8.89% | 2.03 | 14 | 28.6% | 4.64 |
| 6个月(2026.1-6) | +37.96% | +92.03% | 6.54% | 3.38 | 6 | 16.7% | 4.42 |
| 3个月(2025.10-12) | -0.23% | -0.92% | 0.89% | -1.63 | 4 | 0.0% | 0.00 |

### 原始等权9只模式

回测命令：`python3 backtest_bt.py --start=YYYY-MM-DD --end=YYYY-MM-DD`

| 区间 | 总收益 | 年化收益 | 最大回撤 | 夏普 | 交易笔数 |
|------|--------|----------|----------|------|---------|
| 3年(2024-2026) | +8.77% | +2.84% | 4.17% | 0.24 | 105 |
| 2年(2025-2026) | +8.70% | +4.27% | 2.15% | 0.71 | 67 |
| 1年(2025) | +3.07% | +3.08% | 2.15% | 0.32 | 41 |

### 回测参数说明

| 参数 | 命令行 | 默认值 | 说明 |
|------|--------|--------|------|
| --concentrated | - | False | 启用集中TOP3模式 |
| --momentum | simple/c2 | c2 | 动量评分方式（c2=多因子加权） |
| --trail | fixed/e1/e2 | fixed | 移动止盈方式（fixed=固定12%） |
| --init-pct | 0.30/0.50 | 0.50 | 建仓比例 |
| --top-n | 2/3 | 3 | 集中持仓TOP几只 |
| --lookback | 10-30 | 20 | 动量回看期 |
| --trail-pct | 0.07-0.12 | 0.12 | 移动止盈回撤比例(fixed模式) |
| --stop-loss | 0.08-0.12 | 0.08 | 固定止损比例 |
| --rebalance | 5-15 | 5 | 重平衡周期(天) |
| --sector-diversify | - | False | 板块分散(TOP3不同赛道) |
| --ma60-filter | - | False | MA60趋势过滤 |
| --confirm-days | 0-2 | 0 | 换仓延迟确认天数 |
| --dynamic-pct | - | False | 动态仓位(动量越强越大) |
| --concentrated-pyramid | - | False | 集中TOP2+50%建仓+趋势加码 |
| --rotation | - | False | 全仓轮动模式 |
| --pyramid | - | False | 趋势加码模式 |

---

## 架构

```
market_data.py          行情获取(腾讯+新浪+akshare)+指标计算(RSI/MACD/ATR/AO/MA)
        │
        ▼
signal_generator.py     信号生成：遍历ETF池，调rsi_macd/t0/grid生成买卖信号
        │                      集中模式下：非监控池ETF只允许卖出
        ├──▶ strategies/rsi_macd.py   建仓(6通道) + 止损(3级) + 止盈
        ├──▶ strategies/t0.py         做T(5分钟MACD, RSI<50/>50)
        └──▶ strategies/grid.py       网格(倒金字塔5档买/3档卖)
        │
        ▼
executor.py             信号执行：遍历信号调trade.py下单(限价单+配对挂单)
        │
        ▼
trade.py                同花顺接口：buy/sell/t0_buy/t0_sell/sync/revoke_all
        │
        ▼
EvolvingSim             同花顺模拟交易API(thsdk+evolving)

支撑模块：
  risk_manager.py       止损评估(分级止损/保本止盈/硬止损/趋势止盈/移动止盈)
  money_manager.py      仓位计算(分批建仓/底仓活动仓再平衡)
  position_info.py      持仓状态模型(entry_avg_cost/confirm_batch持久化)
  state_center.py       全局状态管理(portfolio.json读写/信号状态/资金分配)
  rotation.py           轮动选池(22只候选ETF，集中TOP3模式输出前3)
  sell_signal_filter.py 卖出信号过滤(重复/冲突/冷却)
  sentiment_check.py    舆情检查(负面舆情暂停建仓)
  tracker.py            持仓追踪(盈亏/成本/信号历史)
  order_manager.py      订单生命周期管理(提交/撤单/超时/持久化)
```

---

## 核心文件清单

| 文件 | 行数 | 职责 |
|------|------|------|
| backtest_bt.py | 1817 | backtrader回测引擎(支持集中/轮动/金字塔等多种模式) |
| signal_generator.py | 639 | 信号生成主入口 |
| executor.py | 1032 | 信号执行(调trade.py下单) |
| trade.py | 558 | 同花顺模拟交易(唯一下单入口) |
| market_data.py | 567 | 行情+指标(所有指标只在此文件) |
| state_center.py | 967 | 全局状态+portfolio.json读写 |
| rotation.py | 945 | 轮动选池(22只候选ETF，C2多因子动量) |
| risk_manager.py | 386 | 止损/止盈评估 |
| money_manager.py | 257 | 仓位计算/再平衡 |
| position_info.py | 352 | 持仓状态模型 |
| order_manager.py | 567 | 订单管理 |
| strategies/rsi_macd.py | 318 | 建仓+止损策略 |
| strategies/t0.py | 154 | 做T策略 |
| strategies/grid.py | 95 | 网格策略 |
| config.yaml | 120 | 全部策略参数 |
| etf_pool.json | - | ETF标的池+行业关键词+动量数据 |

**总代码量：~10800行**

---

## 策略逻辑

### 集中TOP3模式（当前实盘）

**核心思路**：不做等权分散9只，只持C2多因子动量排名前3的ETF，每只分1/3资金(73333元)，50%建仓(36666元)。

**C2多因子动量评分**：
```
score = 20日涨幅×0.4 + MA20斜率×0.4 + ATR归一化(ATR/price)×0.2
```

**每5个交易日重平衡**：
1. 计算所有9只ETF的C2动量得分
2. 只持有TOP3，跌出TOP6的持仓清仓
3. 对新进入TOP3的ETF按信号建仓
4. 止损/止盈每天检查，不受重平衡周期影响

### 建仓（6通道，任一满足即触发）

| 通道 | 条件 |
|------|------|
| 1 | RSI≤55 + MACD金叉 → 建仓30%(集中模式50%) |
| 2 | MACD金叉/绿柱缩短 + RSI 35-55 → 分批建仓 |
| 3 | 突破10日高点 + MACD金叉 → 突破入场 |
| 4 | 多头排列(MA5>MA10>MA20) + 回踩MA20 → 趋势跟踪 |
| 5 | RSI≤55 + MACD金叉 + 空仓N天 → 试探建仓 |
| 6 | RSI<30极值反弹 → 抄底 |

### 止损（分级，由risk_manager统一管理）

| 级别 | 条件 | 动作 |
|------|------|------|
| Level 1 | MACD绿柱 | 减活动仓20% |
| Level 2 | 破MA20 | 减活动仓30% |
| Level 3 | MACD死叉 | 清仓 |

**额外止损**：硬止损-25%、保本止盈(浮盈>30%触发，回撤到成本+2%清仓)、固定止损8%(集中模式)

### 止盈

| 类型 | 条件 | 动作 | 集中模式 |
|------|------|------|---------|
| 移动止盈 | 从峰值回撤7% | 清仓 | **回撤12%** |
| 激活阈值 | 浮盈8% | 开始跟踪 | **浮盈13%** |
| 锁仓阈值 | 浮盈15% | 确认止盈 | **浮盈20%** |
| 趋势止盈 | 浮盈>15% + 破MA5 | 卖活动仓10% | 同 |
| 硬止盈 | 浮盈>60% | 清仓 | 同 |

**关键差异**：集中模式止盈从7%放宽到12%，让利润更奔跑。配置在config.yaml的trail_pct。

### 做T（5分钟K线）

| 操作 | 条件 |
|------|------|
| 做T买入 | 5分钟RSI<50 + MACD金叉/红柱 |
| 做T卖出 | 5分钟RSI>50 + MACD死叉/绿柱 |
| 配对挂单 | 买入价 ± ATR×1.1 |
| 做T数量 | 持仓×30%，最低5000股，100倍数取整 |

### 网格

| 参数 | 值 |
|------|-----|
| 买入档位 | 5档，倒金字塔(权重0.2/0.15/0.2/0.35/0.4) |
| 卖出档位 | 3档 |
| 间距 | base_spacing(默认2.5%) × 动态因子(ATR加权) |

### 轮动选池

| 参数 | 值 |
|------|-----|
| 候选范围 | 22只ETF(覆盖半导体/通信/医药/军工/有色等) |
| 排名方式 | C2多因子(20日涨幅+MA20斜率+ATR归一化) |
| 集中模式输出 | TOP3（每只fund_per_etf=73333元） |
| 执行频率 | 每周一8:00 |

---

## 关键参数（config.yaml）

| 参数 | 值 | 说明 |
|------|-----|------|
| total_fund | 220000 | 总资金 |
| concentrated_mode | true | 启用集中TOP3模式 |
| concentrated_top_n | 3 | 持有TOP几只 |
| trail_pct | 0.12 | 移动止盈回撤比例(集中模式) |
| active_ratio | 0.7 | 活动仓占比 |
| dead_ratio | 0.3 | 底仓占比 |
| entry.position_cap | 0.50 | 建仓比例(集中模式50%) |
| max_daily_buys | 1 | 每日买入上限(ATR>5%时2次) |
| max_daily_t0 | 2 | 每日做T上限(ATR>5%时3次) |
| cooldown_days | 2 | 清仓冷静期/趋势止盈冷却 |
| empty_days_threshold | 5 | 空仓等待天数(按ETF独立计数) |
| hard_take_profit | 0.6 | 硬止盈60% |
| hard_stop_pct | 0.25 | 硬止损25% |
| rsi_buy_max / rsi_sell_min | 50/50 | 做T RSI阈值 |
| pair_ratio | 0.3 | 做T比例(持仓×30%) |
| pair_atr_multiplier | 1.1 | 做T配对价差倍数 |

---

## 下单铁律

1. **只按信号价挂单，不追价**
2. **股数≥5000股**，100倍数取整（<5000自动修正为5000）
3. 买入限价≤卖一价，卖出限价≥买一价
4. 同花顺返回值不可信，下单默认成功
5. 做T配对挂单：固定价差 = ATR × 1.1
6. 趋势止盈/破MA5卖活动仓：单日2次，冷却2天
7. 清仓冷静期2天
8. 每日买入上限1次(ATR>5%时2次)，做T上限2次(ATR>5%时3次)

---

## 优化铁律（血泪教训）

1. **每次只改一条**→跑回测→有效保留/无效回滚
2. **50+条优化已测，有效改善汇总**：
   - 去掉512170医疗ETF（旧基线+6.1%，已从池中删除）
   - 集中TOP3替代等权9只（2025从+3%→+37.85%）
   - C2多因子动量替代简单20日涨幅（2026从+18.5%→+20.17%）
   - 止盈从7%放宽到12%（2026从+17.7%→+30.58%）
   - 建仓比例从30%提到50%（收益翻倍）
3. **已测失败的优化方向**：
   - 趋势加码/金字塔加仓：回撤恶化(20%+)，2026亏损
   - 全仓轮动：2025亏16%，彻底失败
   - 入场通道精简+量能确认：2026恶化
   - 动态仓位(动量越强仓位越大)：2025降10%
   - MA60趋势过滤：2025降8%
   - 动量回看期≠20日：全部亏损
   - 自适应止盈(e1/e2)：不如fixed 12%
   - 止损放宽(10%/12%)：2026恶化
   - 换仓延迟确认：2025大幅恶化
4. **核心矛盾**：2025震荡市靠止损保命 vs 2026趋势市靠持仓吃利润，12%止盈是目前最优平衡点

---

## 数据流

```
同花顺 ──sync──▶ portfolio.json ──▶ signal_generator ──▶ executor ──▶ trade.py ──▶ 同花顺
                    │                     │
                    ▼                     ▼
              _signal_state          signals JSON
              (建仓/止损/网格状态)     (买卖/做T信号)
```

### portfolio.json结构

```json
{
  "positions": {
    "159516": {
      "name": "半导体设备ETF",
      "shares": 165200,
      "avg_cost": 0.702,
      "current_price": 0.743,
      "base_price": 0.712,
      "market_value": 122743
    }
  },
  "_signal_state": {
    "159516": {
      "build_phase": 1,
      "stop_level": 0,
      "entry_avg_cost": 0.759,
      "peak_price": 0.741,
      "reached_activation": true,
      "grid_frozen": false,
      "confirm_batch_count": 2,
      "cooldown_until": null
    }
  }
}
```

**entry_avg_cost vs avg_cost**：
- `avg_cost`(同花顺)：按实际成交价重新计算，手动加仓后可能变化大
- `entry_avg_cost`(我们)：建仓加权平均，买入更新，卖出不变，用于策略判断

---

## 故障处理

| 场景 | 处理 |
|------|------|
| 同花顺断连 | 只报一次ALERT跳过信号，不重试 |
| sync总资产偏差>5% | 报警重跑 |
| 下单返回(False,None) | 认为成功（同花顺返回不可信） |
| 行情数据源限流 | 腾讯→新浪→akshare逐级降级，等待恢复 |
| 回测验证失败 | 立即回滚，git revert |

---

## 文件变更规则

- 每次策略文件修改后必须推送到GitHub
- 回测验证基线一致后才合并
- 敏感信息(密码/密钥/token)只存本地，不上传GitHub
- 集中模式参数变更需同步：config.yaml + backtest_bt.py params + 实盘代码(risk_manager/position_info/rsi_macd)
