# 统一买卖做T流程方案

> 设计目标：将 `do_buy`/`do_sell`/`do_t0_buy`/`do_t0_sell` 四个独立分支统一为一条下单流水线。
> 不改 trade.py 代码，仅输出方案设计。

---

## 一、现状差异分析

| 步骤 | `do_buy`/`do_sell` | `do_t0_buy`/`do_t0_sell` | 问题 |
|------|---------------------|---------------------------|------|
| ① 校验100倍数 | ✅ `_validate_shares` | ✅ `_validate_shares` | 一致 |
| ② 同步订单成交 | ❌ 无 | ❌ 无 | 两者都缺 |
| ③ 撤单未成交 | ❌ 无 | ✅ `revoke_all()` | 普通买卖不撤单 → 可能重复挂单 |
| ④ 连续竞价检查 | ❌ 无 | ✅ `_is_continuous_auction()` | 做T特有，需保留 |
| ⑤ 下单 | ✅ `_trade_with_confirmation` | ✅ `_trade_with_confirmation` | 核心一致，但 order_action 参数不同 |
| ⑥ 配对反方向 | ❌ 无 | ✅ 配对 sell/buy | 做T特有 |
| ⑦ 写入订单 | ✅ `_trade_with_confirmation` 内部 | ✅ 主单内部 + 配对单手动 | 配对单的订单写入分散在外层 |

**核心缺失**：步骤②（getEntrust 同步）和步骤③（revoke_all）在普通买卖中完全缺失。

---

## 二、统一流程设计

```
统一入口 (action, code, shares, price, **kwargs)
  │
  ├─ 1. 校验100倍数 ────────────────────────────────── _validate_shares(shares)
  │
  ├─ 2. 同步订单成交信息 ────────────────────────────── _sync_entrust_to_orders()
  │    调用 getEntrust('today', False) 更新本地 orders/ 中 pending→filled/revoked
  │
  ├─ 3. 撤单所有未成交订单 ──────────────────────────── revoke_all()
  │    全撤 + 标记本地 pending→revoked
  │
  ├─ 4. (做T专属) 检查连续竞价时段 ──────────────────── _is_continuous_auction()
  │    非连续竞价→拒绝，仅 t0_buy/t0_sell 需要
  │
  ├─ 5. 下单新订单 ─────────────────────────────────── _trade_with_confirmation(...)
  │    走 EvolvingSim 下单 + 内部 _write_order
  │
  ├─ 6. (做T专属) 配对反方向订单 ────────────────────── 配对 sell/buy + _write_order
  │    仅主单成功时执行，配对失败不影响主单
  │
  └─ 7. 返回结果
```

### 2.1 参数设计

```python
def _unified_trade(
    action,          # 'buy' | 'sell' | 't0_buy' | 't0_sell'
    code, shares, price,
    pair_price=None,  # 做T配对价，None=自动计算
    skip_sync=False,  # 是否跳过 step2 同步（调用方已同步过时用）
    skip_revoke=False # 是否跳过 step3 撤单（调用方已撤过时用）
):
```

### 2.2 步骤详情

#### Step 1: 校验100倍数
```python
_validate_shares(shares)
```
现有逻辑，直接复用。

#### Step 2: 同步订单成交信息
```python
def _sync_entrust_to_orders():
    """调用 getEntrust('today', False) 同步同花顺委托状态到本地 orders/"""
    # 已有实现：signal_generator.py 第 536-600 行
    # 需迁移到 trade.py 中
```
- 调用 `EvolvingSim().getEntrust('today', False)` 获取今日全部委托
- 遍历 `orders/` 中 status='pending' 的订单文件
- 将同花顺中"已成交"→本地 filled，"已撤单/废单"→本地 revoked
- **失败不阻塞**：getEntrust 异常/超时 → 打印 ⚠️ 继续执行

#### Step 3: 撤单所有未成交订单
```python
revoke_all()
```
现有逻辑，直接复用。全撤后本地 pending→revoked，filled 保留。

#### Step 4: (做T专属) 检查连续竞价时段
```python
if action in ('t0_buy', 't0_sell') and not _is_continuous_auction():
    print(f"❌ 非连续竞价时段，不执行")
    return False
```
普通买卖不需要此检查。

#### Step 5: 下单
```python
order_action = action  # 'buy'/'sell'/'t0_buy'/'t0_sell'
status, before, after, contract = _trade_with_confirmation(
    action, code, shares, price, order_action=order_action
)
```
- `_trade_with_confirmation` 已有逻辑，下单 + 内部 `_write_order`
- `order_action` 决定订单文件名前缀（`buy`/`sell`/`t0_buy`/`t0_sell`）

#### Step 6: (做T专属) 配对反方向订单
```python
if action in ('t0_buy', 't0_sell') and status in ('filled', 'pending'):
    if action == 't0_buy':
        pair_action = 'sell'
        pair_order_action = 't0_sell'
        pair_price = pair_price or round(price * 1.02, 3)
    else:  # t0_sell
        pair_action = 'buy'
        pair_order_action = 't0_buy'
        pair_price = pair_price or round(price * 0.98, 3)
    
    # 配对单直接调 EvolvingSim，不查持仓增量
    result = _call_evolving(pair_action, code, shares, pair_price)
    if result is not None:
        p_ok, p_contract = result
        _write_order(code, pair_order_action, shares, pair_price, p_contract, 'pending' if p_ok else 'failed')
    else:
        _write_order(code, pair_order_action, shares, pair_price, None, 'failed')
```

#### Step 7: 返回结果
```python
return status in ('filled', 'pending')
```

---

## 三、调用方改造

### 3.1 四个入口函数简化为统一入口

```python
def do_buy(code, shares, price):
    return _unified_trade('buy', code, shares, price)

def do_sell(code, shares, price):
    return _unified_trade('sell', code, shares, price)

def do_t0_buy(code, shares, price, pair_price=None):
    return _unified_trade('t0_buy', code, shares, price, pair_price=pair_price)

def do_t0_sell(code, shares, price, pair_price=None):
    return _unified_trade('t0_sell', code, shares, price, pair_price=pair_price)
```

### 3.2 CLI 入口不变

```python
# main 中四个分支保持原样
if cmd == 'buy':
    do_buy(...)
elif cmd == 'sell':
    do_sell(...)
elif cmd == 't0_buy':
    do_t0_buy(...)
elif cmd == 't0_sell':
    do_t0_sell(...)
```

---

## 四、需要迁移/新增的代码

### 4.1 迁移 `_sync_entrust_to_orders` 到 trade.py

从 `signal_generator.py` 第 536-600 行迁移到 `trade.py`，作为 `trade.py` 的内置函数。

**依赖**：`_load_today_orders()` 也需要从 `signal_generator.py` 迁移，或 trade.py 中实现一个轻量版本（遍历 orders/ 目录读今日文件）。

### 4.2 新增 `_unified_trade` 函数

约 40 行，整合上述 7 步流程。

### 4.3 简化四个入口函数

`do_buy`/`do_sell`/`do_t0_buy`/`do_t0_sell` 各缩为 2-3 行调用 `_unified_trade`。

---

## 五、关键设计决策

### 5.1 Step 2 放 Step 3 之前

**为什么同步先于撤单？**
- 同步后撤单，`revoke_all()` 才能正确标记所有 pending 为 revoked
- 如果先撤单再同步，同花顺中已成交但本地 pending 的订单不会被标记为 filled

### 5.2 普通买卖增加 revoke_all

**为什么普通买卖也要撤单？**
- 场景：上次 cron 下了 buy 挂单未成交，这次有新信号→先撤旧挂单再生效新单
- 避免同方向重复挂单（一个方向只能有一个有效委托）
- 当前正常买卖不撤单，如果用户手动/其他进程挂了单，新单会冲突

### 5.3 配对单继续用 `_call_evolving` 直接调用

**为什么配对单不走 `_trade_with_confirmation`？**
- 配对单不查持仓增量（确认不了，因为配对单是未来挂单，不是当前成交）
- 配对单只需要 `_call_evolving` + `_write_order`，不需要 `_trade_with_confirmation` 的增量确认逻辑
- 保持原有独立逻辑不变

### 5.4 `skip_sync` 和 `skip_revoke` 参数

**为什么需要跳过参数？**
- 场景：`execute_signals` 在循环中处理多个信号
  - 循环前调一次 `_sync_entrust_to_orders()` + `revoke_all()`
  - 循环中每个信号调 `_unified_trade(..., skip_sync=True, skip_revoke=True)`
- 避免 N 个信号执行 N 次 getEntrust + N 次 revoke_all（浪费时间和同花顺连接）

---

## 六、伪代码（完整）

```python
def _sync_entrust_to_orders():
    """同步 getEntrust 状态到本地 orders/ (迁移自 signal_generator.py)"""
    try:
        e = EvolvingSim()
        ent = e.getEntrust('today', False)
        time.sleep(2)
    except Exception as ex:
        print(f"  ⚠️ 同步委托状态失败: {ex}")
        return

    if not (isinstance(ent, dict) and ent.get('status') and ent.get('data')):
        print(f"  ⚠️ getEntrust 返回异常，跳过同步")
        return

    # 构建同花顺委托索引: {code: {direction: status}}
    ths_map = {}
    for row in ent['data']:
        if not row or len(row) <= 10:
            continue
        ths_map.setdefault(row[2], {})[row[4]] = row[5]

    # 遍历本地 pending 订单，按同花顺状态更新
    today = datetime.now().strftime('%Y%m%d')
    updated = 0
    for fname in os.listdir(ORDERS_DIR):
        if not (fname.startswith(today) and fname.endswith('.json')):
            continue
        order_path = os.path.join(ORDERS_DIR, fname)
        try:
            with open(order_path) as f:
                order = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue
        if order.get('status') != 'pending':
            continue
        code = order.get('code', '')
        direction = order.get('direction', '')
        ths_status = ths_map.get(code, {}).get(direction)
        if not ths_status:
            continue

        new_status = None
        if ths_status in ('已成交', '全部成交', '部分成交'):
            new_status = 'filled'
        elif ths_status in ('已撤单', '已撤销', '废单'):
            new_status = 'revoked'

        if new_status:
            _update_order_status(code, order.get('action', ''), new_status)
            updated += 1

    if updated:
        print(f"  ✅ 同步 {updated} 笔订单状态")


def _unified_trade(action, code, shares, price,
                   pair_price=None,
                   skip_sync=False, skip_revoke=False):
    """
    统一买卖做T下单流程。

    流程: 校验→同步→撤单→(做T:检查时段)→下单→(做T:配对)→返回
    """
    # 1. 校验100倍数
    _validate_shares(shares)

    # 2. 同步订单成交信息
    if not skip_sync:
        _sync_entrust_to_orders()

    # 3. 撤单所有未成交订单
    if not skip_revoke:
        revoke_all()

    # 4. (做T) 检查连续竞价时段
    if action in ('t0_buy', 't0_sell') and not _is_continuous_auction():
        print(f"❌ 非连续竞价时段(9:30-11:30,13:00-15:00)，不执行挂单")
        return False

    # 5. 下单新订单
    order_action = action
    status, before, after, contract = _trade_with_confirmation(
        action, code, shares, price, order_action=order_action
    )

    # 6. (做T) 配对反方向订单
    if action in ('t0_buy', 't0_sell') and status in ('filled', 'pending'):
        if action == 't0_buy':
            pair_action = 'sell'
            pair_order_action = 't0_sell'
            if pair_price is None:
                pair_price = round(price * 1.02, 3)
        else:  # t0_sell
            pair_action = 'buy'
            pair_order_action = 't0_buy'
            if pair_price is None:
                pair_price = round(price * 0.98, 3)

        print(f"  🔁 配对{pair_action} {code} {shares}股@{pair_price}")

        result = _call_evolving(pair_action, code, shares, pair_price)
        if result is not None:
            p_ok, p_contract = result
            if p_ok:
                print(f"   ✅ 配对{pair_action}挂单 {code} {shares}股@{pair_price} 合同:{p_contract}")
            else:
                print(f"   ⚠️ 配对{pair_action}挂单失败: {p_contract}")
        else:
            print(f"   ⚠️ 配对{pair_action}挂单失败")
            p_contract = None

        _write_order(code, pair_order_action, shares, pair_price, p_contract,
                     'pending' if p_ok else 'failed')

    # 7. 返回
    return status in ('filled', 'pending')
```

---

## 七、风险与注意事项

### 7.1 getEntrust 间歇超时
- `_sync_entrust_to_orders` 中的 getEntrust 可能超时或返回异常
- **设计**：异常捕获 + 打印 ⚠️ + 继续执行，不阻塞下单流程
- 同步失败时，本地 orders/ 状态可能滞后，但撤单 + 下单流程不受影响

### 7.2 revoke_all 撤销配对单
- 场景：A 标的做T买入（buy + 配对 sell），B 标的同时做T卖出（sell + 配对 buy）
- 如果先跑 A 再做 B，B 的 revoke_all 会撤销 A 的配对挂单
- **解决方案**：`_unified_trade` 中撤单→下单→配对这个顺序确保配对单在撤单后下，不会被本轮撤单影响
- 但跨标的的冲突仍然存在——这需要 `execute_signals` 层面处理（先全撤→再批量下单→再批量配对，或逐标的执行）

### 7.3 配对单不增量确认
- 配对单是未来挂单，不是当前成交，无法通过 getHoldingShares 增量确认
- 维持现有逻辑：`_call_evolving` 直接调用 + `_write_order` 记录 pending

### 7.4 普通买卖增加 revoke_all 的副作用
- 正常买卖现在也撤单了，如果用户之前有手动挂单（非 trade.py 下的），也会被撤
- **这是预期行为**：统一策略要求下单前清除所有未成交委托，确保干净状态

---

## 八、实施步骤

1. **迁移 `_sync_entrust_to_orders` 到 trade.py**
   - 复制 signal_generator.py 第 536-600 行到 trade.py
   - 依赖：`_load_today_orders()` 的轻量实现（直接读 ORDERS_DIR 中今日文件，不需要 signal_generator 的复杂版本）

2. **新增 `_unified_trade` 函数**
   - 按上述伪代码实现
   - 约 40-50 行

3. **简化四个入口函数**
   - `do_buy`/`do_sell`/`do_t0_buy`/`do_t0_sell` 各缩为 2-3 行

4. **测试**
   - `python3 trade.py buy CODE 100 1.0` → 校验→同步→撤单→下单
   - `python3 trade.py t0_buy CODE 100 1.0` → 校验→同步→撤单→下单→配对
   - `python3 trade.py revoke_all` → 不受影响，独立调用