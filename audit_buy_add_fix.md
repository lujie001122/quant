# 加仓金额不够根因审计报告

> 审计范围: `_buy` 函数 + 资金分配链（backtest_bt.py / state_center.py / position_manager.py）

---

## 问题1: [严重][backtest_bt.py:450-458] `_buy` 中 `need = target - current` 使所有加仓失败

### 根因

```python
def _buy(self, data, pct, reason):
    target_value = min(self.p.fund_per_etf * pct, self.p.fund_per_etf * POSITION_CAP)
    target_shares = max(int(target_value / price / 100) * 100, MIN_SHARES)
    current = self._get_shares(data)
    need = target_shares - current          # ← BUG: 目标减现有
    if need < MIN_SHARES: return False      # ← 所有加仓在此返回 False
```

`_buy` 把 `pct` 当作 **目标持仓比例** 来计算 `target_value = fund_per_etf * pct`，然后 `need = target_shares - current`。

但 **所有加仓调用方** 传入的 `pct` 是 **本次加仓/追加比例**，不是目标总持仓比例。加仓比例必然 ≤ 首次建仓比例 → `need ≤ 0` → 永远返回 False。

### 六条调用路径全部命中

| 调用位置 | pct传入值 | 首次建仓pct | need | 结果 |
|---|---|---|---|---|
| L.750 集中金字塔加仓1 | 0.25 | 0.50 (L.847) | **负值** | ❌ 失败 |
| L.757 集中金字塔加仓2 | 0.25 | 0.50 | **负值** | ❌ 失败 |
| L.1524 金字塔加仓1 | 0.30 | 0.30 (L.1586) | **0** | ❌ 失败 |
| L.1530 金字塔加仓2 | 0.30 | 0.30 | **0** | ❌ 失败 |
| L.2022 补仓 (15%) | 0.15 | 0.30 | **负值** | ❌ 失败 |
| L.2045 分批追加 (30%) | 0.30 | 0.30 | **0** | ❌ 失败 |

### 数值示例 (fund_per_etf=58666, price=1.0, concentrated_mode)

```
首次建仓 (pct=0.30):  target=17600  current=0        need=17600 ✓
集中金字塔加仓 (pct=0.25): target=14667  current=29333 need=-14666 ✗
补仓 (pct=0.15):          target=8800   current=17600  need=-8800  ✗
```

### 修复代码

```python
def _buy(self, data, pct, reason):
    target_value = min(self.p.fund_per_etf * pct, self.p.fund_per_etf * POSITION_CAP)
    price = data.close[0]
    target_shares = max(int(target_value / price / 100) * 100, MIN_SHARES)
    # BUGFIX: _buy传入的是"本次买入/加仓"比例，不是目标总持仓比例
    # 应改为 need = target_shares（不管已有持仓，按本次给定比例买入）
    need = target_shares  # 不用 - current
    if need < MIN_SHARES:
        return False
    # 资金约束
    cost = need * price * (1 + SLIPPAGE_PCT) + FEE
    if cost > self.broker.getcash():
        need = int(self.broker.getcash() / price / 100) * 100
        if need < MIN_SHARES:
            return False
    self._order_pending[name] = self.buy(data=data, size=need)
    self.ps[name]['_last_entry_reason'] = reason
    return True
```

---

## 问题2: [中][state_center.py:340-358] `get_etfs_config` 使用 `max_per_etf` 作为资金基数

### 代码

```python
# state_center.py L.351-362
max_per_etf = _cfg.get('max_per_etf', 220000)      # ← 应是 total_fund
cash_reserve_ratio = _cfg.get('cash_reserve_ratio', 0.20)
investable_fund = int(max_per_etf * (1 - cash_reserve_ratio))
```

`get_etfs_config` 读取 `max_per_etf` 作为资金基数，`max_per_etf` 语义是"单只ETF资金上限"。当前 config.yaml 中 `total_fund` 和 `max_per_etf` 都是 220000，巧合一致才没出错。如未来调整 `max_per_etf` 将导致 fund_per_etf 计算链断裂。

### 计算链完整对比: 回测 vs 实盘

| 环节 | 回测 (get_etfs_config) | 实盘 (PositionManager.allocate_fund) |
|---|---|---|
| 资金基数 | `max_per_etf` = 220000 | `total_fund` = 220000 |
| cash_reserve | 0.20 (配置缺失, 用默认) | 无此项 |
| active_ratio | 不应用 | 0.7 |
| 集中模式分配 | 176000 // 3 = **58666** | 154000 * 0.2 = **30800** |

**回测 fund_per_etf = 58666 反而比实盘 30800 更大**，说明 fund_per_etf 数值本身不是"加仓金额不够"的原因 — 根因在问题1的 need 逻辑。

---

## 问题3: [低][state_center.py:353] `cash_reserve_ratio=0.20` 配置不存在

config.yaml 中没有 `cash_reserve_ratio` 字段，`get_etfs_config` 使用默认值 0.20，导致:
- investable_fund = 220000 × 0.8 = 176000 (凭空少44000)
- concentrated mode: fund_per_etf = 176000 // 3 = **58666** (本应为 220000 // 3 ≈ 73333)

但此问题在当前回测场景下被问题1掩盖（因为加仓在 need 判断就失败了，fund_per_etf 是否打折不影响结果）。

---

## 问题4: [低][position_manager.py:376-377] 实盘 `allocate_fund` 使用 `actual_total_asset`

```python
fund_base = actual_total_asset if actual_total_asset is not None and actual_total_asset > 0 else self.total_fund
active_fund = fund_base * self.active_ratio  # 220000 * 0.7 = 154000
```

实盘用 `active_ratio=0.7` 打折到 154000，然后集中模式下 `top_weight=0.6/top_n=0.2` → 每只 30800。回测根本没有应用 `active_ratio`，两者计算链不一致但此差异不是加仓失败的主因。

---

## 综合结论

| 优先级 | 问题 | 文件:行 | 风险等级 | 根因 |
|---|---|---|---|---|
| P0 | `_buy` need 计算错误 | backtest_bt.py:456-458 | **严重** | `need = target_shares - current` 导致所有加仓调用传入的 add_pct ≤ init_pct 时 need ≤ 0，全部 return False。六条加仓路径全部瘫痪。 |
| P1 | `get_etfs_config` 用错配置项 | state_center.py:351 | **中** | 用 `max_per_etf` 代替 `total_fund`，语义错误但当前值巧合相同 |
| P2 | `cash_reserve_ratio` 配置缺失 | state_center.py:353 | **低** | 默认 0.20 不打折逻辑，但数值偏差被问题1掩盖 |

**根因一句话**: `_buy` 的 `need = target_shares - current` 语义是"补足到目标仓位"，但所有调用方传入的 pct 是"追加比例"而非"目标比例"，导致 `need ≤ 0 → return False`，所有加仓全部静默失败。