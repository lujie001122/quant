# 卖出后亏损率放大修复方案

## 问题背景

卖出某 ETF 后，券商 avg_cost 会将已实现亏损重新摊入剩余持仓成本，导致 `trade.py sync` 后 `portfolio.json` 中 `avg_cost` 上浮，亏损率被动放大。

---

## 根因与修复方案

### 根因1（🔴 优先修复）：券商 avg_cost 摊入已实现亏损，sync 后覆盖 _signal_state

**问题链路**：
1. 卖出部分 ETF 持仓后，券商（同花顺）将已实现亏损重新分摊到剩余股分的 `avg_cost` 中
2. `trade.py sync` 从同花顺 `getHoldingShares` 读取 `row[10]`（券商 avg_cost）
3. 无条件写回 `_signal_state[code]['avg_cost']`（见 backup/trade_20260810_1955.py:333-334）
4. 当前 `trade.py sync()` 虽然简化了，但 `portfolio.json` 的 `positions[code].avg_cost` 仍然来自券商（trade.py:98）
5. `signal_generator.py:89-91` 从 `positions.avg_cost` 恢复 `pos.avg_cost`，覆盖 `_signal_state` 中的值
6. 导致 `profit_pct` = `(price - 上浮后的avg_cost) / 上浮后的avg_cost` → 亏损率放大

**修复方案**：

**方案A（推荐）：sync 时保留 _signal_state 的 avg_cost，不随券商更新**
```
位置: trade.py sync() 函数，或 signal_generator.py 第 74-99 行恢复逻辑

逻辑:
1. sync 时，对于已在 _signal_state 中的 code：
   - IF _signal_state[code].shares > 0（之前有持仓）
   - THEN 比较券商 shares 与 _signal_state 记录的 shares：
     - 如果 shares 减少（卖出了部分），说明券商 avg_cost 已被污染
     - 保留 _signal_state 中的旧 avg_cost（摊入前的真实成本）
     - 只更新 shares 和 current_price
     - 添加标记：avg_cost_source = 'signal_state'（不被券商覆盖）
   - 如果 shares 不变或增加（买入），正常用券商 avg_cost
2. 对于 _signal_state 中 shares=0 但券商有持仓的 code：
   - 视为新买入，正常取券商 avg_cost
```

**方案B（备用）：仅用 cost/shares 自行计算，完全不用券商 avg_cost**
```
位置: trade.py sync() + signal_generator.py 恢复逻辑

逻辑:
1. 在 _signal_state 中新增字段 total_cost（累计买入成本，不含已实现亏损）
2. 每次 signal_generator 产生 buy 信号并执行后，更新 total_cost += 买入金额
3. 每次 sell/liquidate/reduce，根据卖出比例等比减少 total_cost
4. avg_cost = total_cost / shares（自行计算，完全不依赖券商）
5. sync 时只读取券商 shares，avg_cost 用自行计算的值
```

**推荐方案A**，因为改动最小，只需在 sync 恢复逻辑中加一层判断。

### 根因2（🔴 优先修复）：_signal_state 与 broker 不一致，反复清仓循环

**问题链路**：
1. `signal_generator.py:253-259` 清仓信号触发后调用 `pos.reset_on_liquidate()` → shares=0, avg_cost=0
2. 同时写 `_signal_state` 为清仓后状态
3. 但 `trade.py` 实际下单可能未成交/部分成交 → 券商仍有持仓
4. 下次 `signal_generator` 运行：从 `_signal_state` 恢复 → shares=0，判定无持仓
5. 从 `portfolio.json` 的 `positions` 恢复 → shares>0（券商同步的），判定有持仓
6. 第 87-98 行只恢复 `shares/avg_cost/current_price`，不恢复关键策略状态（stop_level/build_phase/peak_price 等）
7. 导致 `build_phase=0`，触发建仓信号 → 重新买入
8. 同时止损信号可能再次触发清仓 → 循环

**修复方案**：

```
位置: signal_generator.py 第 74-103 行的持仓恢复逻辑

逻辑:
1. 在从 _signal_state + positions 恢复后，增加一致性检查：
   - IF _signal_state.shares == 0 AND positions.shares > 0:
     - 说明上次清仓未完全执行，或 sync 后发现券商仍有持仓
     - 此时：标记 pos._pending_liquidate = True
     - 不要重新建仓，而是等待下次 sync 确认后再决策
     - pos.shares 取 positions.shares（券商实际值）
     - pos.build_phase 保持 _signal_state 的值（=0，不清仓）
     - 关键：将 pos.avg_cost 设为券商值（因为清仓信号的 avg_cost 已重置为 0）
   
2. 在信号生成阶段（line 335-368），如果 pos._pending_liquidate 为 True：
   - 跳过 evaluate_entry（不触发建仓）
   - continue 到「持有」逻辑
   - reason 设为 "上次清仓未完成,等sync确认"
   
3. 增加清仓前的前置检查（line 251-259）：
   - 在 reset_on_liquidate 之前，检查 portfolio.json 中的 shares
   - 如果 shares 与 pos.shares 不一致（说明已部分卖出但清仓信号是新的）：
     - 不执行 reset_on_liquidate，改为 reduce 操作
```

### 根因3：peak_price 重置 + avg_cost 上浮

**问题链路**：
1. `reduce_shares()` 中 `self.peak_price = price`（strategy.py:194），用当前价替换历史峰值
2. 卖出部分后 avg_cost 上浮（来自根因1）
3. `peak_price` 被替换为远低于真实峰值的当前价
4. 硬止损20% 条件 `price < peak_price * 0.80` 极易触发（strategy.py:380）
5. → 再次清仓，形成连锁反应

**修复方案**：

```
位置: strategy.py reduce_shares() 方法 (line 180-195)

逻辑:
修改 reduce_shares 中的 peak_price 更新逻辑：
- 当前代码: self.peak_price = price  （无条件用当前价替换）
- 修改为: self.peak_price = max(self.peak_price, price)  （只升不降）

理由: peak_price 代表该持仓的历史最高价，只在买入或价格上涨时更新。
减仓只是降低仓位，不改变历史峰值。
如果确实需要重置（如完全清仓后重新建仓），只在 reset_on_liquidate 或 _enter_position 时设置。
```

### 根因4：reduce_shares 小仓位低比例可能不执行

**问题链路**：
1. `reduce_shares(pct=5)`：小仓位 × 5% → 可能 < 100 股
2. `reduce_shares` 代码（strategy.py:182-192）：
   - `int(self.shares * pct / 10000) * 100` → 对 1000 股 × 5% = 50 股 → int(50/100)*100 = 0
   - 虽然有兜底逻辑（<100 取全部 → reduce_count=100），但对极端小仓位(如 200 股) × 5% = 10 股 → 兜底后减 100 股
3. 如果 pos.shares 很小且 reduce_shares 传入的 pct 很小，可能实际不减仓

**修复方案**：

```
位置: strategy.py reduce_shares() 方法 (line 180-195)

逻辑:
在函数开头增加最小执行量检查：
- MIN_REDUCE_SHARES = 100
- IF self.shares * pct / 100 < MIN_REDUCE_SHARES:
  - IF self.shares < MIN_REDUCE_SHARES:
    - 总仓不足100股，全清（等同于 liquidate）
    - reduce_count = self.shares
  - ELSE:
    - 至少减 MIN_REDUCE_SHARES 股
    - reduce_count = MIN_REDUCE_SHARES
```

### 根因5：网格卖出后无止损重置

**问题链路**：
1. 网格卖出只减 20% 仓位（strategy.py:740 `return f"触及第{i}档卖出...", 0.20`）
2. `reduce_shares(pct=20, price=price)` 被调用
3. `peak_price = price`（当前价，根因3），降低硬止损触发线
4. 网格卖出后没有检查是否需要重置止损状态（如 stop_level 恢复）
5. 价格继续下跌 → 止损条件更容易触发

**修复方案**：

```
位置: strategy.py reduce_shares() 或 signal_generator.py 网格卖出执行处

逻辑:
1. reduce_shares 后增加止损状态评估：
   - 减仓后如果持仓占比降低到安全水平（如 < 30%），重置 stop_level
   - 减仓后如果 float profit > 0，重置 breakeven_activated

2. 在 signal_generator.py 第 325-333 行的网格卖出执行处：
   - 网格卖出后调用 pos.reset_stop_if_safe()
   - reset_stop_if_safe() 检查：
     - IF 当前价格 > pos.avg_cost（浮盈状态）
     - THEN 重置 stop_level=0, below_ma20_count=0
```

---

## 执行优先级与顺序

| 优先级 | 根因 | 修复难度 | 影响范围 | 建议顺序 |
|--------|------|----------|----------|----------|
| 🔴 P0 | 根因1: sync后avg_cost被污染 | 低 | 核心（所有盈亏计算） | 第1个修复 |
| 🔴 P0 | 根因2: 清仓循环 | 中 | 中（特定场景） | 第2个修复 |
| 🟡 P1 | 根因3: peak_price替换 | 极低（1行改动） | 中（止损信号） | 第3个修复 |
| 🟢 P2 | 根因4: 小仓位不执行 | 低 | 低（边缘场景） | 第4个修复 |
| 🟢 P2 | 根因5: 网格卖出无止损重置 | 低 | 低（网格场景） | 第5个修复 |

---

## 验证方法

修复后通过以下方式验证：

1. **根因1验证**：卖出部分仓位后 sync，检查 `_signal_state[code].avg_cost` 是否与卖出前一致
2. **根因2验证**：模拟清仓信号触发 → 下单未成交 → 下次 cron 运行，确认不触发重新建仓
3. **根因3验证**：减仓后检查 peak_price 是否保持历史最高值
4. **根因4验证**：对小仓位（200股）执行 reduce_shares(5%)，确认至少减100股
5. **根因5验证**：网格卖出后检查 stop_level 是否被正确重置