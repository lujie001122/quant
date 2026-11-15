# 实盘管线复现框架（research/parity）

用于量化"实盘管线 vs 回测引擎"的差异，不参与生产调度。

- `bt_ext.py`：把 `signal_generator.py + executor.py` 的决策链逐日重放到 backtrader。
  已验证与 `backtest_bt.py --no-timing` 完全一致（同参数下年化/回撤/笔数逐一相同）。
- `engine_dbg.py`：`research/hunt/engine.py`（结构引擎）的副本，加了买卖日志与 no_reentry 开关。

消融开关（环境变量 `PARITY_AB`，逗号分隔）：
`selfheal`(建仓态自愈) `livetrail`(改用实盘 position_info 止盈) `mech`(机械入场) `capn`(持仓数上限)
`noadd`(禁用加仓) `nostops`(禁用实盘止损栈) `sizeeq`(等权目标仓位) `e1struct`(结构引擎 e1 口径)
`univ23`(剔除3只标的) `live12`(等价 livetrail)
其他：`PARITY_WARMUP=1` 预热修复、`PARITY_COC=1` 收盘成交、`PARITY_TRAIL_MODE=e1|atr|fixed`。

```bash
QR_KLINE_CACHE=/tmp/h25/research/kline python3 research/parity/bt_ext.py \
  --concentrated-pyramid --weekly-rotation --rotation-interval 5 --trend-entry \
  --init-pct=0.50 --momentum=simple --trail=e1 --top-n=5 --lookback=15 \
  --start=2019-01-01 --end=2026-11-13
```
