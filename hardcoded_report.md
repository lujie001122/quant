# 硬编码问题扫描报告

扫描时间: 2026-09-08
扫描范围: backtest_bt.py, signal_generator.py, executor.py, trade.py, risk_manager.py, money_manager.py, strategies/rsi_macd.py, strategies/t0.py, rotation.py, market_data.py, config.yaml

---

## 一、魔法数字（未引用配置的数值）

### backtest_bt.py

| 行号 | 硬编码值 | 当前值 | config.yaml 对应键 | 影响 |
|------|---------|--------|-------------------|------|
| 123 | 数据起始日期 | `"20250101"` | 无 | 回测默认拉全量K线的起始日期，未参数化 |
| 124 | 数据结束日期 | `"20261231"` | 无 | 回测默认拉全量K线的结束日期，未参数化 |
| 151 | RSI周期 | `14` | `market_data.rsi_period: 14` | WilderRSI指标period=14，未读config |
| 179 | MACD参数 | `fast=12, slow=26, signal=9` | `market_data.macd_fast/slow/signal` | MACDStatus指标参数，未读config |
| 249 | AO参数 | `fast=5, slow=34` | `market_data.ao_short/ao_long: 5/34` | AOIndicator指标参数，未读config |
| 261 | AO SMA周期 | `-5:` 和 `-34:` | `market_data.ao_short/ao_long` | 硬编码5和34日SMA，与参数不一致 |
| 271 | 每ETF资金基数 | `44000` | `total_fund/9 ≈ 24444` 或 `max_per_etf: 220000` | **严重**: fund_per_etf=44000与config中total_fund=220000/9≈24444不一致 |
| 279 | 建仓比例 | `0.30` | `entry.confirm_entry_ratio: 1.0` | init_pct=0.30与config的confirm_entry_ratio含义不同，但建仓30%未参数化 |
| 285 | 固定止损 | `0.08` | `stop_loss.hard_stop_pct: 0.25` | **严重**: 回测默认stop_loss=8%与config的25%不一致 |
| 598-604 | 均价止损分级 | `0.15/0.20/0.25` | 无对应 | 仓位>80%止损15%、50-80%止损20%、<50%止损25%，与risk_manager重复且未参数化 |
| 607-608 | 浮盈止损上移阈值 | `0.10/1.05` | 无对应 | 浮盈>10%止损线=成本+5%，未参数化 |
| 609-610 | 浮盈保本线阈值 | `0.05` | 无对应 | 浮盈>5%止损线=保本，未参数化 |
| 622 | 硬止损25% | `0.75` (1-0.25) | `stop_loss.hard_stop_pct: 0.25` | 硬编码0.75(1-25%)，未引用config |
| 631-638 | e1/e2移动止盈阈值 | `0.08/0.15/0.10/0.18` | `profit_take.trailing_8pct/15pct` | **严重**: 回测e1=8%/15%与config的trailing_8pct=0.1/trailing_15pct=0.08不一致（交叉） |
| 661 | 仓位上限50% | `0.50` | `entry.position_cap: 0.5` | 未引用config，硬编码 |
| 663 | 金字塔加仓浮盈阈值 | `0.05` | 无 | 浮盈>5%加仓1，未参数化 |
| 672 | 金字塔加仓2浮盈阈值 | `0.10` | 无 | 浮盈>10%加仓2，未参数化 |
| 730 | 趋势跟踪空仓天数 | `5` | `trend.empty_days_threshold: 5` | 未引用config |
| 810 | 做T无配对比例 | `0.20` | `signal.t0_sell_full_ratio: 0.2` | executor.py中做T无配对时ratio=0.20，未引用config |
| 872 | 减仓默认比例 | `0.30` | 无 | 减仓ratio未解析时默认30%，未参数化 |
| 892-893 | e1移动止盈ATR阈值 | `0.90/0.93/0.95` + `5/3` | 无 | e1模式ATR>5%→10%/ATR≥3%→7%/否则5%，全部硬编码 |
| 894-895 | e2移动止盈ATR阈值 | `0.88/0.92/0.95` + `5/3` | 无 | e2模式ATR>5%→12%/ATR≥3%→8%/否则5%，全部硬编码 |
| 1099 | 大盘指标代码 | `"510300"` | 无 | 沪深300ETF作为大盘指标，硬编码 |

### executor.py

| 行号 | 硬编码值 | 当前值 | config.yaml 对应键 | 影响 |
|------|---------|--------|-------------------|------|
| 129 | 初始资金 | `220000` | `total_fund: 220000` | SimBroker默认initial_cash=220000，未读config |
| 130 | 手续费 | `5.0` | `backtest.fee: 5` | SimBroker默认fee=5.0，未读config |
| 131 | 滑点 | `0.001` | `backtest.slippage_pct: 0.001` | SimBroker默认slippage_pct=0.001，未读config |
| 296 | EvolvingSim调用后sleep | `2`秒 | `order.retry_interval: 5` | sleep(2)未引用config，与retry_interval=5不一致 |
| 322 | 配对挂单间隔 | `time.sleep(1)` | 无 | t0配对挂单间隔1秒，未参数化 |
| 481 | 激活同花顺sleep | `time.sleep(1)` | 无 | 未参数化 |

### trade.py

| 行号 | 硬编码值 | 当前值 | config.yaml 对应键 | 影响 |
|------|---------|--------|-------------------|------|
| 60 | 最小股数 | `5000` | 无 | **严重**: _validate_shares最低5000股，与config和回测逻辑(100股最小)不一致 |
| 401 | 连续竞价时间 | `570/690/780/900` (分钟) | 无 | 硬编码交易时段判断 |
| 484 | 做T买入配对价差 | `1.02` (2%) | `t0.pair_atr_multiplier: 1.1` | **严重**: do_t0_buy默认pair_price=price*1.02，与config的ATR×1.1机制不一致 |
| 490 | 做T卖出配对价差 | `0.98` (2%) | `t0.pair_atr_multiplier: 1.1` | **严重**: do_t0_sell默认pair_price=price*0.98，同上 |
| 52 | EvolvingSim调用后sleep | `2`秒 | 无 | 未参数化 |
| 465 | 配对挂单间隔 | `5`秒 | 无 | 未参数化 |

### risk_manager.py

| 行号 | 硬编码值 | 当前值 | config.yaml 对应键 | 影响 |
|------|---------|--------|-------------------|------|
| 54 | 连续亏损熔断天数 | `3` | 无 | MAX_CONSECUTIVE_LOSS_DAYS=3，未参数化 |
| 55 | 最低日亏损计入阈值 | `0.01` (1%) | 无 | MIN_DAILY_LOSS_PCT=0.01，未参数化 |
| 154-159 | 均价止损分级 | `0.15/0.20/0.25` + `0.80/0.50` | 无 | 仓位分级止损阈值硬编码，与backtest_bt.py重复 |
| 161-167 | 浮盈止损上移 | `0.10/1.05/0.05` | 无 | 浮盈>10%→成本+5%，>5%→保本，与backtest_bt.py重复 |
| 175 | 硬止盈60% | `1.60` | `profit_take.hard_take_profit: 0.6` | base_price*1.60，已与config一致但值硬编码 |
| 185 | 硬止损25% | `0.75` (1-0.25) | `stop_loss.hard_stop_pct: 0.25` | peak_price*0.75，值硬编码未引用config |
| 194 | 连续破MA20天数 | `2` | 无 | below_ma20_count>=2触发减仓，未参数化 |
| 209 | RSI低位阈值 | `35` | `entry.test_cap_rsi: 35` | RSI<35确认趋势恶化，未引用config |

### money_manager.py

| 行号 | 硬编码值 | 当前值 | config.yaml 对应键 | 影响 |
|------|---------|--------|-------------------|------|
| 52 | 最小股数 | `5000` | 无 | **严重**: calc_position_size返回max(shares, 5000)，与回测100股最小单位不一致 |
| 65 | 最小股数 | `5000` | 无 | calc_position_size_percent同上 |
| 77 | 做T默认比例 | `0.30` | `t0.pair_ratio: 0.3` | 未引用config |
| 78 | 最小股数 | `5000` | 无 | calc_t0_shares同上 |
| 97 | 首笔建仓比例 | `0.30` | 无 | get_initial_entry_ratio固定返回0.30，未参数化 |
| 107 | 首笔/确认比例 | `0.30/1.00` | 无 | get_build_ratio固定返回值，未参数化 |
| 155 | 最小股数 | `5000` | 无 | calc_grid_buy_shares同上 |
| 213 | 做T配对价差 | `150` | `t0.min_spread: 150` | calc_t0_pair_price固定150元价差，未引用config |
| 222 | 做T价差最小值 | `150` | `t0.min_spread: 150` | is_t0_pair_viable的min_spread默认150，未引用config |

### strategies/rsi_macd.py

| 行号 | 硬编码值 | 当前值 | config.yaml 对应键 | 影响 |
|------|---------|--------|-------------------|------|
| 39 | 集合竞价时间 | `555-565/897-900` | 无 | 9:15-9:25和14:57-15:00，硬编码 |
| 53-54 | RSI买卖阈值 | `75/25` | `t0.rsi_extreme_buy: 25 / rsi_extreme_sell: 75` | t0_buy_score/t0_sell_score中RSI极端阈值，未引用config |
| 62-63 | 量比阈值 | `1.5/1.2` | 无 | 买入量比<1.5、卖出量比>1.2，未参数化 |
| 97 | 止损参数默认值 | `avg_pct=0.20, hard_pct=0.25` | `stop_loss` | _compute_stop_loss_price默认参数，与risk_manager重复 |
| 104 | 保本margin | `0.02` | `stop_loss.breakeven_margin: 0.02` | 未引用config |
| 117 | 8%锁仓线 | `1.05` (成本+5%) | `profit_take.trailing_8pct: 0.08` | reached_8pct后止损=成本×1.05，硬编码 |
| 160 | 每ETF资金 | `44000` | `total_fund/9` | **严重**: ETFS.get(code, {}).get("fund", 44000)默认44000 |
| 199 | ATR异常阈值 | `0.50` (50%) | 无 | ATR>50%跳过建仓，未参数化 |
| 203 | 止盈期不新开阈值 | `1.30` (30%) | `profit_take.hard_take_profit: 0.6` | **严重**: base_price*1.30=30%不新开，与hard_take_profit=60%含义不同但混淆 |
| 208-209 | RSI阈值 | `40/30` | `entry.rsi_min: 40 / rsi_minimal: 30` | rsi_ok=RSI>40, rsi_minimal=RSI>30，未引用config |
| 218 | RSI抄底上限 | `55` | 回测rsi_entry_max=55 | rsi_macd.py通道1 RSI≤55，实盘与回测一致但未读config |
| 265 | 逆势补仓跌幅 | `-0.03` | `entry.dip_pct: -0.03` | 未引用config |
| 265 | 逆势补仓RSI上限 | `40` | `entry.rsi_min: 40` | 未引用config |
| 265 | 补仓次数上限 | `5` | `entry.add_count_limit: 5` | add_count<5，未引用config |

### strategies/t0.py

| 行号 | 硬编码值 | 当前值 | config.yaml 对应键 | 影响 |
|------|---------|--------|-------------------|------|
| 46-49 | MA20斜率阈值 | `0.003/-0.005` | `t0.ma20_slope_sell_cap: 0.003 / ma20_slope_buy_cap: -0.005` | **严重**: 斜率>0.003禁止卖出，<-0.005禁止买入，值与config不一致（config中sell_cap=0.003, buy_cap=-0.005）→实际一致，但未引用config |
| 107 | 做T买入比例 | `0.30` | `t0.pair_ratio: 0.3` | ratio=0.30，未引用config |
| 111 | 做T最小股数 | `5000` | 无 | max(..., 5000)，未参数化 |
| 130 | 做T卖出比例 | `0.2` | `signal.t0_sell_half_ratio: 0.1 / t0_sell_full_ratio: 0.2` | **严重**: 做T卖出ratio=0.2(20%)，与config的t0_sell_half_ratio=0.1不一致 |
| 134 | 做T最小股数 | `5000` | 无 | 同上 |
| 115/138 | 做T配对价差 | `150` | `t0.min_spread: 150` | spread>150才生成配对，未引用config |

### rotation.py

| 行号 | 硬编码值 | 当前值 | config.yaml 对应键 | 影响 |
|------|---------|--------|-------------------|------|
| 491-497 | 攻守配比 | `4/1 或 2/3` | 无 | 上证MA20斜率>0→4攻1守，否则2攻3守，未参数化 |
| 548-549 | 防御池筛选条件 | `DD<15%, M>3%, Vol>15%` | 无 | 防御候选池筛选条件全部硬编码 |
| 569-571 | 放宽防御池条件 | `DD<20%, M>0%, Vol>10%` | 无 | 放宽条件全部硬编码 |
| 801 | 观察期天数 | `5` | 无 | OBSERVE_DAYS=5，未参数化 |
| 901 | 再准入RSI阈值 | `40` | 无 | RSI>40方可再准入，未参数化 |
| 213-230 | 多因子权重 | `0.4/0.3/0.15/0.15` | 无 | 4周动量×0.4+8周动量×0.3+RSI×0.15+MACD×0.15，未参数化 |
| 194 | 年化交易日 | `252` | 无 | math.sqrt(252)，未参数化 |

### market_data.py

| 行号 | 硬编码值 | 当前值 | config.yaml 对应键 | 影响 |
|------|---------|--------|-------------------|------|
| 24 | API调用间隔 | `1` | `market_data.api_delay: 1` | API_DELAY=1，与config一致但直接赋值未读取 |
| 120 | K线默认起始日期 | `"20250101"` | 无 | fetch_klines_daily默认start，未参数化 |
| 120 | K线默认结束日期 | `"20261231"` | 无 | fetch_klines_daily默认end，未参数化 |
| 133 | K线数量 | `1200` | `market_data.kline_limit: 1200` | 腾讯接口datalen=1200，与config一致但硬编码 |
| 186 | Sina K线数量 | `5000` | 无 | Sina降级源datalen=5000，未参数化 |
| 366 | 5分钟K线数量 | `48` | 无 | 腾讯5分钟K线48根，未参数化 |

---

## 二、硬编码的ETF代码

| 文件 | 行号 | 硬编码ETF代码 | 影响 |
|------|------|-------------|------|
| backtest_bt.py | 97-107 | 9只默认ETF代码（159516/515880/588170等） | etf_pool.json不存在时的回退列表，与state_center动态加载不一致 |
| backtest_bt.py | 295-310 | SECTOR_MAP中22只ETF代码+板块 | 板块分类硬编码，新增ETF需手动添加 |
| backtest_bt.py | 1099 | `"510300"` 沪深300ETF | 大盘自适应指标，硬编码 |
| risk_manager.py | 间接引用 | DEFENSE_CODE=`"159611"` (via position_info) | 防御标的代码，已在config.yaml配置defense_code |
| rotation.py | 27-76 | ETF_CANDIDATES 27只ETF代码 | 轮动候选池，硬编码维护 |
| rotation.py | 911-927 | DEFAULT_POOL 5只ETF代码 | etf_pool.json不存在时的默认池 |
| market_data.py | 486 | `"000001"` + `"sh000001"` | 上证指数代码，rotation.py用于大盘判断 |
| signal_generator.py | 555 | `"515080"` | 市场环境描述文字中引用515080，应引用DEFENSE_CODE |
| strategies/rsi_macd.py | 196 | `"515080"` | 防御标的515080硬编码在日志文字中 |

---

## 三、与config.yaml不一致的重复定义

| 参数 | config.yaml | 代码中的值 | 文件 | 影响 |
|------|-----------|----------|------|------|
| 每ETF资金 | `total_fund: 220000` (÷9≈24444) | `44000` | backtest_bt.py:271, rsi_macd.py:160 | **严重**: 回测和实盘资金基数不同，回测44000远大于24444 |
| 固定止损 | `stop_loss.hard_stop_pct: 0.25` | `0.08` | backtest_bt.py:285 | **严重**: 回测默认8%止损，实盘25%止损 |
| 移动止盈e1 | `trailing_8pct: 0.08, trailing_15pct: 0.1` | 激活线8%/锁仓线15% | backtest_bt.py:631-632 | config中8pct/15pct命名与含义混淆 |
| 做T卖出比例 | `t0_sell_half_ratio: 0.1` | `0.2` (20%) | t0.py:130 | **严重**: config定义10%半仓卖出，代码用20% |
| 做T配对价差 | trade.py用`2%`比例 | money_manager用`150元`固定 | trade.py:484/490 vs money_manager.py:213 | **严重**: 两套做T配对价差计算方式完全不同 |
| 均价止损分级 | config无定义 | `0.15/0.20/0.25` | risk_manager.py:154-159, backtest_bt.py:598-604 | 两处重复定义，config未集中管理 |
| 逆势补仓 | `entry.dip_pct: -0.03` | `-0.03` | rsi_macd.py:265 | 值一致但代码未读取config |
| RSI阈值 | `entry.rsi_min: 40` | `40` | rsi_macd.py:208 | 值一致但代码未读取config |
| RSI极值 | `t0.rsi_extreme_buy: 25/sell: 75` | `25/75` | rsi_macd.py:53-54, 79-81 | 值一致但代码未读取config |
| MA20斜率 | `t0.ma20_slope_buy_cap/sell_cap` | `0.003/-0.005` | t0.py:46-49 | 值一致但代码未读取config |
| 最小交易股数 | config无定义 | `5000`(实盘)/`100`(回测) | trade.py:60, money_manager.py vs backtest_bt.py | **严重**: 实盘最低5000股、回测最低100股，config未统一 |

---

## 四、策略阈值未参数化（应提取到config.yaml）

| 阈值 | 当前硬编码位置 | 当前值 | 建议 |
|------|-------------|--------|------|
| 均价止损分级(仓位>80%/50-80%/<50%) | risk_manager.py:154-159, backtest_bt.py:598-604 | 15%/20%/25% | 添加 `stop_loss.tier1/tier2/tier3` |
| 均价止损仓位分级阈值(80%/50%) | 同上 | 0.80/0.50 | 添加 `stop_loss.tier1_ratio/tier2_ratio` |
| 浮盈止损上移阈值(5%/10%) | risk_manager.py:162-163, backtest_bt.py:607-608 | 0.05/0.10 | 添加 `stop_loss.float_profit_trigger1/trigger2` |
| 浮盈止损上移比例(保本/+5%) | 同上 | 0%/5% | 添加 `stop_loss.float_profit_stop1/stop2` |
| 连续破MA20天数 | risk_manager.py:194 | 2 | 添加 `stop_loss.ma20_break_days` |
| RSI低位确认阈值 | risk_manager.py:209 | 35 | 添加 `stop_loss.rsi_low_confirm` |
| 连续亏损熔断天数 | risk_manager.py:54 | 3 | 添加 `risk.max_consecutive_loss_days` |
| 最低日亏损计入 | risk_manager.py:55 | 0.01 | 添加 `risk.min_daily_loss_pct` |
| 最低交易股数(实盘) | trade.py:60, money_manager.py | 5000 | 添加 `trade.min_shares` |
| 做T量比阈值(买<1.5/卖>1.2) | rsi_macd.py:62-63, 88 | 1.5/1.2 | 添加 `t0.vol_ratio_buy_max/vol_ratio_sell_min` |
| 轮动攻守配比(4攻1守/2攻3守) | rotation.py:491-497 | 4/1 或 2/3 | 添加 `rotation.momentum_count_bull/defense_count_bull` 等 |
| 防御池筛选条件 | rotation.py:548-549 | DD<15%,M>3%,Vol>15% | 添加 `rotation.defense_*` 系列 |
| 轮动观察期 | rotation.py:801 | 5天 | 添加 `rotation.observe_days` |
| 再准入RSI阈值 | rotation.py:901 | 40 | 添加 `rotation.re_entry_rsi_min` |
| 多因子权重 | rotation.py:213-230 | 0.4/0.3/0.15/0.15 | 添加 `rotation.weight_m4/weight_m8/weight_rsi/weight_macd` |
| ATR异常阈值 | rsi_macd.py:199 | 0.50 | 添加 `entry.atr_abnormal_threshold` |
| 止盈期不新开阈值 | rsi_macd.py:203 | 1.30 | 添加 `entry.profit_block_threshold` |
| RSI抄底上限 | rsi_macd.py:218 | 55 | 添加 `entry.rsi_entry_max` (回测已有，实盘未读) |
| e1/e2移动止盈ATR分级倍数 | backtest_bt.py:892-937 | 0.90/0.93/0.95等 | 添加 `profit_take.e1_atr_high_mult` 等系列 |
| 金字塔加仓浮盈阈值 | backtest_bt.py:663/672 | 0.05/0.10 | 添加 `pyramid.profit_trigger1/trigger2` |
| 大盘自适应指标ETF | backtest_bt.py:1099 | "510300" | 添加 `market.index_code` |
| 集合竞价时间 | rsi_macd.py:39 | 9:15-9:25,14:57-15:00 | 添加 `market.auction_periods` |
| K线数据默认日期范围 | market_data.py:120 | 20250101-20261231 | 添加 `market_data.default_start/end` |
| 5分钟K线数量 | market_data.py:366 | 48 | 添加 `market_data.kline_5min_limit` |

---

## 五、重复逻辑（同一阈值在多处硬编码，修改一处会遗漏另一处）

| 逻辑 | 出现位置 | 风险 |
|------|---------|------|
| 均价止损分级(15%/20%/25%) | risk_manager.py:154-159, backtest_bt.py:598-604 | 两处独立硬编码，修改不同步 |
| 浮盈止损上移(5%保本/10%+5%) | risk_manager.py:162-163, backtest_bt.py:607-608 | 同上 |
| 硬止损25%(peak*0.75) | risk_manager.py:185, backtest_bt.py:622 | 同上 |
| 硬止盈60%(base*1.60) | risk_manager.py:175 | 仅一处，但与config定义方式不同 |
| e1/e2移动止盈 | backtest_bt.py:631-638, 891-937, 1287-1334 | **3处**重复定义e1/e2模式阈值 |
| 最小交易股数(5000 vs 100) | trade.py:60, money_manager.py多处 vs backtest_bt.py:454 | 实盘5000/回测100，不一致 |
| 做T配对价差(150元) | money_manager.py:213, t0.py:115/138 | 两处硬编码150 |
| 每ETF资金(44000) | backtest_bt.py:271, rsi_macd.py:160 | 两处硬编码44000 |
| RSI极端阈值(25/75) | rsi_macd.py:53-54, 79-81 | 与config定义一致但未读取 |

---

## 六、严重问题摘要（需优先修复）

1. **每ETF资金不一致**: config total_fund=220000/9≈24444 vs 代码硬编码44000 → 回测和实盘仓位基数差异82%
2. **止损比例不一致**: config hard_stop_pct=25% vs 回测默认8% → 回测止损过紧
3. **做T卖出比例不一致**: config t0_sell_half_ratio=0.1(10%) vs 代码0.2(20%) → 实盘卖出量翻倍
4. **做T配对价差机制冲突**: trade.py用2%比例 vs money_manager用150元固定 → 两种计算方式结果不同
5. **最小交易股数不一致**: 实盘5000股 vs 回测100股 → 回测无法模拟小单
6. **e1/e2移动止盈3处重复**: backtest_bt.py中3个模式分支各写一遍阈值 → 修改极易遗漏
7. **30+个策略阈值未参数化**: 集中在止损分级、做T量比、轮动攻守配比、金字塔加仓等关键逻辑
