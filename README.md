# ETF轮动量化交易系统 v7.0

9只ETF共享资金池，RSI+MACD建仓止损 + 网格 + 做T + 轮动选池，全自动AI Agent执行。

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
| akshare | A股行情数据 |
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
python3 backtest_bt.py --start=2025-01-01 --end=2025-12-31
# 期望输出：总收益+26.39% / 回撤9.29% / 夏普1.35 / 36笔
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
| 588170 | 科创半导体ETF | sh | 动量 |
| 513780 | 港股创新药ETF | sh | 动量 |
| 512400 | 有色ETF | sh | 动量 |
| 159532 | 中证2000ETF | sz | 防御 |
| 515050 | 中证全指ETF | sh | 防御 |
| 159611 | 电力ETF | sz | 防御 |
| 515220 | 煤炭ETF | sh | 防御 |

**新增标的必须同步3处：** etf_pool.json(names/sids/type/etf_pool) + rotation.py ETF_CANDIDATES + state_center.py DEFAULT_POOL

---

## 回测基线

| 区间 | 总收益 | 最大回撤 | 夏普 | 交易笔数 |
|------|--------|----------|------|---------|
| 3年(2024-2026) | +43.75% | 19.62% | 0.78 | 55 |
| 2年(2025-2026) | +60.91% | 9.29% | 1.49 | 60 |
| 1年(2025) | +26.39% | 9.29% | 1.35 | 36 |
| 9个月 | +48.10% | 6.74% | 2.56 | 25 |
| 6个月 | +17.09% | 6.98% | 1.63 | 18 |
| 3个月 | +4.89% | 3.67% | 1.19 | 6 |

回测命令：`python3 backtest_bt.py --start=YYYY-MM-DD --end=YYYY-MM-DD`

---

## 架构

```
market_data.py          行情获取(腾讯+新浪)+指标计算(RSI/MACD/ATR/AO/MA)
        │
        ▼
signal_generator.py     信号生成：遍历9只ETF，调rsi_macd/t0/grid生成买卖信号
        │
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
  order_manager.py      订单生命周期管理(提交/撤单/超时/持久化)
  rotation.py           轮动选池(49只ETF动量排名，4攻1守)
  sell_signal_filter.py 卖出信号过滤(重复/冲突/冷却)
  sentiment_check.py    舆情检查(负面舆情暂停建仓)
  tracker.py            持仓追踪(盈亏/成本/信号历史)
```

---

## 核心文件清单

| 文件 | 行数 | 职责 |
|------|------|------|
| backtest_bt.py | 1122 | backtrader回测引擎(9只ETF共享资金池) |
| signal_generator.py | 620 | 信号生成主入口 |
| executor.py | 1032 | 信号执行(调trade.py下单) |
| trade.py | 558 | 同花顺模拟交易(唯一下单入口) |
| market_data.py | 529 | 行情+指标(所有指标只在此文件) |
| state_center.py | 962 | 全局状态+portfolio.json读写 |
| rotation.py | 929 | 轮动选池(49只候选ETF) |
| risk_manager.py | 381 | 止损/止盈评估 |
| money_manager.py | 257 | 仓位计算/再平衡 |
| position_info.py | 341 | 持仓状态模型 |
| order_manager.py | 567 | 订单管理 |
| strategies/rsi_macd.py | 310 | 建仓+止损策略 |
| strategies/t0.py | 154 | 做T策略 |
| strategies/grid.py | 95 | 网格策略 |
| config.yaml | 117 | 全部策略参数 |
| etf_pool.json | - | ETF标的池+行业关键词+动量数据 |

**总代码量：~9700行**

---

## 策略逻辑

### 建仓（6通道，任一满足即触发）

| 通道 | 条件 |
|------|------|
| 1 | RSI<40 + MACD金叉 |
| 2 | RSI<40 + 红柱(任意) |
| 3 | RSI<40 + AO反转(红转绿) |
| 4 | 多头排列(MA5>MA10>MA20) + 回踩MA20 |
| 5 | 破MA20 + MACD支撑(DIF>0) |
| 6 | RSI<30极值反弹 |

**分批建仓**：首笔→确认加仓(1次)→补仓(最多5次)，倒金字塔权重

### 止损（分级，由risk_manager统一管理）

| 级别 | 条件 | 动作 |
|------|------|------|
| Level 1 | MACD绿柱 | 减活动仓20% |
| Level 2 | 破MA20 | 减活动仓30% |
| Level 3 | MACD死叉 | 清仓 |

**额外止损**：硬止损-25%、保本止盈(浮盈>30%触发，回撤到成本+2%清仓)、移动止盈7%

### 止盈

| 类型 | 条件 | 动作 |
|------|------|------|
| 移动止盈 | 从峰值回撤7% | 清仓 |
| 趋势止盈 | 浮盈>15% + 破MA5 | 卖活动仓10% |
| 硬止盈 | 浮盈>60% | 清仓 |

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
| 候选范围 | 49只ETF(覆盖12个行业) |
| 排名周期 | 4周+8周动量加权 |
| 选池规则 | 4攻1守 |
| 执行频率 | 每周一8:00 |

---

## 关键参数（config.yaml）

| 参数 | 值 | 说明 |
|------|-----|------|
| total_fund | 220000 | 总资金 |
| active_ratio | 0.7 | 活动仓占比 |
| dead_ratio | 0.3 | 底仓占比 |
| max_daily_buys | 1 | 每日买入上限(ATR>5%时2次) |
| max_daily_t0 | 2 | 每日做T上限(ATR>5%时3次) |
| cooldown_days | 2 | 清仓冷静期/趋势止盈冷却 |
| empty_days_threshold | 5 | 空仓等待天数(按ETF独立计数) |
| hard_take_profit | 0.6 | 硬止盈60% |
| hard_stop_pct | 0.25 | 硬止损25% |
| trailing_8pct | 0.1 | 移动止盈7%触发线(1-0.07=0.93) |
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
2. **40+条优化唯一有效**：去掉512170医疗ETF（2025+6.1%）
3. **7%固定回撤是甜点**：5→7有效，7→8失败，ATR跟踪止损全失败
4. **限制买入/加速卖出都伤趋势市**——R²/MA120乖离/卖出冷却/放量卖出/相关性过滤/Score加权/近3日跌幅全部失败
5. **回测"bug"是局部最优产物**——P0-1/2/7修复反而变差，不强行统一
6. **核心矛盾**：2025震荡市靠止损保命 vs 2026趋势市靠持仓吃利润，不可兼得

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
      "avg_cost": 0.702,        // 同花顺成本(按实际成交计算)
      "current_price": 0.743,
      "base_price": 0.712,
      "market_value": 122743
    }
  },
  "_signal_state": {
    "159516": {
      "build_phase": 1,          // 0=空仓 1=建仓中 2=确认完成
      "stop_level": 0,           // 止损级别 0/1/2/3
      "entry_avg_cost": 0.759,   // 我们的建仓成本(加权平均,卖出不变)
      "peak_price": 0.741,       // 峰值价(移动止盈用)
      "reached_8pct": true,      // 是否触发过移动止盈
      "grid_frozen": false,      // 网格冻结
      "confirm_batch_count": 2,  // 确认加仓批次
      "cooldown_until": null     // 冷却期
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
| 512170行情偶发nan | 已从池中删除 |
| 回测验证失败 | 立即回滚，git revert |

---

## 文件变更规则

- 每次策略文件修改后必须推送到GitHub
- 回测验证基线一致后才合并
- 敏感信息(密码/密钥/token)只存本地，不上传GitHub
