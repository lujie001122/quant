# 风控执行安全审计报告

## 审计对象
- `/Users/lujie/Documents/code/quant/risk_manager.py`
- `/Users/lujie/Documents/code/quant/executor.py`
- `/Users/lujie/Documents/code/quant/trade.py`
- `/Users/lujie/Documents/code/quant/signal_generator.py`
- `/Users/lujie/Documents/code/quant/position_info.py`

---

## 问题1: 阶梯止盈同日多档仅执行第一条

**风险等级: 🔴 严重 (CRITICAL)**  
**文件:** `signal_generator.py:409-416`  
**根因:** `risk_manager.py:_evaluate_stop_loss` (178-188行)会在单次调用中将全部三级阶梯止盈追加到`stop_actions`列表。但`signal_generator.py` (378-416行)使用带`break`的`for`循环遍历，**只匹配第一条`sell_active_10pct`就跳出**。`sell_active_15pct`和`sell_active_20pct`被静默丢弃。同时`reached_5pct`/`reached_8pct_ladder`已置True，后续调用不会再触发——15%/20%的卖出信号**永久丢失**。

**影响:** 大涨日(>8%)应同时执行10%+15%+20%=45%活动仓卖出，实际只卖出10%。

**修复:** `signal_generator.py:409-416` 应将阶梯止盈改为累加而非break短路，或改为累积position_ratio后统一输出一个信号。建议方案：移除该分支的`break`，将三个`position_ratio`累加为一个综合信号（如"45%(活动仓)"）。

---

## 问题2: trailing_stop_price无条件覆盖（缺少max保护）

**风险等级: 🟡 中等 (MEDIUM)**  
**文件:** `position_info.py:218-220`  
**根因:** `update_trailing_stop()` 在`reached_15pct`触发时：
1. Line 218: `self.trailing_stop_price = self.avg_cost * 1.05`
2. Line 220: 立即**无条件覆盖**为 `self.peak_price * trail_mult`

缺少`max()`保护。若峰值尚未充分拉开，`peak_price * trail_mult`可能低于`avg_cost * 1.05`，导致移动止盈线下移。对比`risk_manager.py:166`已正确使用`max(pos.trailing_stop_price, protect_stop)`。

**影响:** 极端情况下止盈线非预期下降，提前触发移动止盈，减少应得利润。

**修复:** 将`position_info.py:220`改为：
```python
self.trailing_stop_price = max(self.trailing_stop_price, self.peak_price * trail_mult)
```

---

## 问题3: 做T配对单无成交状态检查

**风险等级: 🔴 严重 (CRITICAL)**  
**文件:** `executor.py:390-401`, `trade.py:231-234`  
**根因:** `RealBroker.execute()`中做T配对单的上/下挂单**不检查主单是否成交**：

`executor.py:390-395` (t0_buy):
```python
result = self._call_evolving("buy", ...)
if order.pair_price > 0:       # ← 没有任何 result 检查
    time.sleep(1)
    self._call_evolving("sell", ...)
```
若`buy`返回None(下单失败)，配对`sell`依然执行 → **裸卖空**。`trade.py:231-234`同病。

同时`_call_evolving` (executor.py:350-358)用`except Exception: return None`吞掉所有异常，上层无法区分"下单失败"和"网络异常"。

**影响:** 做T买入失败时卖出配对单照常挂出，造成裸卖空(无持仓卖出)，触发券商风控或强制平仓。

**修复:** 配对单必须加入结果检查：
```python
result = self._call_evolving("buy", ...)
if result is None:
    return ExecutionResult(success=False, ...)  # 主单失败，不挂配对
# 配对单也应有重试/异常处理
```

---

## 问题4: getHoldingShares返回空时持仓数据可能被清空

**风险等级: 🟡 中等 (MEDIUM)**  
**文件:** `trade.py:91-121`  
**根因:** `sync()`中`getHoldingShares`返回`None`(异常)时，`isinstance(None, dict)`为False，同步块跳过，**旧数据幸存**——这半安全。

但若`getHoldingShares`返回**有效dict但shares=0**（盘中临时空数据），`if shares_val > 0`分支不进入，`elif code in pf['positions']`进入 → **持仓被删除**。随后`portfolio.json`中该标的消失，下一次信号可能因无持仓状态而产生虚假开仓。

**影响:** 盘中瞬时断连或同花顺返回空数据时，portfolio.json中的真实持仓被误删，引发后续错误交易决策。

**修复:** 在`sync()`中增加安全缓冲：若`pf`已有持仓数据而新数据中shares=0，应有次数/天数阈值验证，避免单次空数据即删除。或至少增加日志告警。

---

## 问题5: _validate_shares自动修正可能导致仓位超限

**风险等级: 🟡 中等 (MEDIUM)**  
**文件:** `trade.py:64-69`, `executor.py:874-877`  
**根因:** `_validate_shares`对不足5000股的数量**静默膨胀**：
```python
if shares < _min_shares:
    shares = _min_shares   # 例如 100 → 5000，50倍膨胀
```

信号生成器按比例算出的股数（如`int(44000 * 0.02 / 2.0 / 100) * 100 = 400`）被直接提升至5000，实际成交额从¥800膨胀到¥10000，约12.5倍。**信号级的风控(仓位比例)被绕过**。且`executor.py:877`用`max(int(...), 5000)`同样膨胀。

**影响:** 小信号(建仓/轻仓加仓)的股数被强制放大，突破仓位控制。总基金¥220K时5000股@¥2=¥10000≈4.5%，尚可接受；但多只标的同时触发时累计风险不可控。

**修复:** 方案A: 5000下限保留，但修正时发出WARN而非silent。方案B: 动态最小股数基于仓位比例计算，`min(5000, 信号计算值)`或仅警告不自动修正。

---

## 附录: 异常吞没汇总

| 位置 | 吞没类型 | 风险 |
|---|---|---|
| `executor.py:356-358` | `_call_evolving`用`except Exception: return None` | 所有EvolvingSim异常变None |
| `executor.py:440-441` | `cancel`用`except Exception: return False` | 撤单失败静默 |
| `executor.py:449-450` | `cancel_all`用`except Exception: return False` | 全撤失败静默 |
| `executor.py:476-477` | `query`用`except Exception: return None` | 查询失败静默 |
| `executor.py:512-513` | `get_positions`用`except Exception: return {}` | 持仓查询失败静默 |
| `executor.py:537-538` | `get_account`用`except Exception: return {"total_asset":0}` | 账户查询失败静默 |
| `executor.py:545-546` | `is_connected`用`except Exception: return False` | 连接检查失败静默 |
| `executor.py:554-555` | `_ensure_tonghuashun_active`用`except Exception: pass` | 窗口激活失败静默 |
| `executor.py:994-995` | 成交后持仓更新用`except Exception: pass` | 状态不同步 |
| `executor.py:1014-1015` | 清仓状态重置用`except Exception: pass` | 状态不同步 |
| `executor.py:1026-1027` | 交易记录用`except Exception: print` | 交易记录丢失 |
| `executor.py:1094-1095` | 告警推送用`except Exception: print` | 告警丢失 |

**建议:** 至少将`except Exception: return/None/False/pass`改为区分预期异常(如网络超时)与意外异常(如属性错误)，意外异常应打印stack trace。

---

## 修复优先级建议

| 优先级 | 问题 | 风险等级 |
|---|---|---|
| **P0** | 阶梯止盈只执行10%(问题1) → 直接造成利润损失 | 🔴 CRITICAL |
| **P0** | 做T配对裸卖空(问题3) → 券商风控风险 | 🔴 CRITICAL |
| **P1** | trailing_stop覆盖(问题2) → 止盈线漂移 | 🟡 MEDIUM |
| **P1** | sync空数据删持仓(问题4) → 交易决策错误 | 🟡 MEDIUM |
| **P2** | shares自动膨胀(问题5) → 仓位越限 | 🟡 MEDIUM |