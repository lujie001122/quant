# 修复方案：本金减小导致亏损率放大

## 问题根因

减仓或清仓后，**券商侧 `avg_cost` 被污染**，并通过 `signal_generator.py` 的状态恢复链路反写回 `PositionInfo.avg_cost`，导致止损条件误判。

### 完整因果链

```
1. 信号触发「减仓30%」→ reduce_shares(pct=30)
   └─ strategy.py:191-192: self.shares -= reduce_count
                          self.cost = self.avg_cost * self.shares
   └─ ★ avg_cost 不变（保持原始买入均价），这里是正确的

2. trade.py 执行卖出 → 券商真实成交
   └─ 券商（同花顺）按先进先出/加权平均重算 avg_cost
   └─ 卖出部分后，剩余持仓的 avg_cost 被券商重置为「剩余持仓均价」

3. trade.py sync 同步回 portfolio.json
   └─ trade.py:98: 'avg_cost': cost  ← 券商返回的 avg_cost
   └─ 此时 portfolio.json["positions"][code]["avg_cost"] 已被污染

4. 下次 signal_generator.py 生成信号
   └─ signal_generator.py:87-91:
        pos_pf = pf_data["positions"].get(code, {})
        p.avg_cost = pos_pf["avg_cost"]   ← ★ 从券商 contaminated avg_cost 恢复
   └─ 此时 PositionInfo.avg_cost = 券商重算后的均价 ≠ 原始买入均价

5. 止损条件误判
   └─ strategy.py:367: price <= pos.avg_cost * 0.88
   └─ strategy.py:363: price <= pos.avg_cost * 1.02
   └─ 减仓后 avg_cost 被券商抬高，亏损率被放大，连续触发止损
```

### 具体场景举例

持有 10000 股，原始买入均价 1.000，现价 0.900（浮亏10%）。

减仓 3000 股卖出 → 券商端剩余 7000 股的 avg_cost 可能变成 ~0.970（因为卖出的3000股按 1.000 成本出，剩余按比例计算）。

信号系统恢复后 `avg_cost = 0.970`：
- 原来：`profit_pct = (0.900 - 1.000) / 1.000 = -10%`，未触发 12% 止损
- 污染后：`profit_pct = (0.900 - 0.970) / 0.970 = -7.2%`？
- **实际上是亏损被摊入剩余成本**，avg_cost 变小了，导致 `price / avg_cost` 比值变小，亏损看起来更大

## 修复方案（最小改动）

### 方案：`_signal_state` 中持久化「原始买入均价」，恢复时优先使用

#### 修改 1：`strategy.py` — `PositionInfo` 新增 `entry_avg_cost` 字段

```python
# PositionInfo.__init__ 新增:
self.entry_avg_cost = 0.0  # 原始买入均价（建仓时锁定，不受减仓影响）
```

#### 修改 2：`strategy.py` — `to_dict` 保存 `entry_avg_cost`

```python
# to_dict return 中新增:
"entry_avg_cost": self.entry_avg_cost,
```

#### 修改 3：`strategy.py` — `from_dict` 恢复 `entry_avg_cost`

```python
# from_dict 中新增:
self.entry_avg_cost = data.get("entry_avg_cost", self.entry_avg_cost)
```

#### 修改 4：`strategy.py` — `_enter_position` 建仓时锁定

```python
# _enter_position 中新增:
self.entry_avg_cost = price  # 建仓时锁定原始买入价
```

#### 修改 5：`strategy.py` — `reduce_shares` **不修改** avg_cost 和 entry_avg_cost

当前 `reduce_shares` 中 `self.cost = self.avg_cost * self.shares` 已经是正确行为（不改 avg_cost），只需确保不碰 `entry_avg_cost`。

**但关键是**：`reset_on_liquidate` 中需要重置 `entry_avg_cost = 0`。

#### ★ 修改 6（核心修复）：`strategy.py` — 止损条件改用 `entry_avg_cost`

```python
# evaluate_stop 中，两处止损条件：

# 1. 保本止盈 (line 359-364)
# 改前：profit_pct = (price - pos.avg_cost) / pos.avg_cost
# 改后：entry = pos.entry_avg_cost if pos.entry_avg_cost > 0 else pos.avg_cost

# 2. 均价止损12% (line 367-368)
# 改前：price <= pos.avg_cost * 0.88
# 改后：price <= (pos.entry_avg_cost if pos.entry_avg_cost > 0 else pos.avg_cost) * 0.88
```

或者更简洁：在 `PositionInfo` 上新增属性：

```python
@property
def entry_cost(self):
    """止损/止盈使用的基准成本：优先用原始买入价"""
    return self.entry_avg_cost if self.entry_avg_cost > 0 else self.avg_cost
```

然后在 `evaluate_stop` 中将 `pos.avg_cost` 替换为 `pos.entry_cost`。

## 改动清单

| 文件 | 位置 | 改动 | 影响 |
|------|------|------|------|
| `strategy.py` | `__init__` | 新增 `self.entry_avg_cost = 0.0` | 新字段 |
| `strategy.py` | `to_dict` | 新增 `"entry_avg_cost"` 到返回字典 | 持久化 |
| `strategy.py` | `from_dict` | 新增 `self.entry_avg_cost = data.get(...)` | 恢复 |
| `strategy.py` | `_enter_position` | 新增 `self.entry_avg_cost = price` | 建仓锁定 |
| `strategy.py` | `reset_on_liquidate` | 新增 `self.entry_avg_cost = 0` | 清仓重置 |
| `strategy.py` | 新增 `@property entry_cost` | 返回 `entry_avg_cost or avg_cost` | 统一入口 |
| `strategy.py` | `evaluate_stop` L359-368 | `pos.avg_cost` → `pos.entry_cost` | 止损用原始价 |

**不改动**：`signal_generator.py`、`trade.py`、`profit_pct()`、`update_trailing_stop()`、移动止盈逻辑。

## 不改动的理由

1. **`signal_generator.py` 恢复逻辑不动**：`avg_cost` 仍然从 portfolio 恢复（用于展示/记录），但止损不再依赖它
2. **`trade.py` sync 不动**：券商 avg_cost 继续同步到 portfolio — 它作为参考数据保留
3. **`profit_pct()` 方法不动**：该方法仍基于 `avg_cost`，用于日志/展示目的。`entry_cost` 是止损专用的基准
4. **移动止盈线不动**：`update_trailing_stop` 中 `avg_cost * 1.05` 和 `peak_price * 0.95` — 移动止盈基于峰值，不依赖 avg_cost 精度

## 注意：`evaluate_stop` 中还需要检查 `update_trailing_stop`

`strategy.py:138` 中 `self.trailing_stop_price = self.avg_cost * 1.05` — 这个用 `avg_cost` 是合理的，因为它是建仓后的初始移动止损线设置点（15% 盈利后设到成本+5%），即使 avg_cost 有小幅偏差也不影响。**不改**。

## 验证方式

1. 执行减仓后检查 `entry_avg_cost` 是否保持不变
2. 止损条件应基于 `entry_avg_cost` 而非券商返回的 `avg_cost`
3. 回测验证：减仓后连续止损不会误触发