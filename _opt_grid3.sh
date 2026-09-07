#!/bin/bash
# 精简参数网格搜索 - 最关键的参数组合
cd /Users/lujie/Documents/code/quant

BASE_CMD="python3 backtest_bt.py --concentrated --weekly-rotation --momentum=simple"

echo "============================================"
echo "精简参数搜索 - 基准年化13.49% - 目标50%"
echo "============================================"

# 批次1: init_pct + rotation_interval (最关键)
echo ""
echo "--- 批次1: init_pct + rotation_interval ---"
for ip in 1.0 1.5 2.0 3.0; do
  for ri in 5 10 20; do
    result=$($BASE_CMD --rotation-interval=$ri --init-pct=$ip --rebalance=3 2>&1 | grep -E "年化收益|最大回撤" | tr '\n' ' ')
    echo "init_pct=$ip rotation_interval=$ri: $result"
  done
done

# 批次2: top_n + trail_pct 
echo ""
echo "--- 批次2: top_n + trail_pct ---"
for tn in 2 3 4 5; do
  for tp in 0.12 0.15 0.20; do
    result=$($BASE_CMD --rotation-interval=20 --init-pct=0.50 --rebalance=3 --top-n=$tn --trail-pct=$tp 2>&1 | grep -E "年化收益|最大回撤" | tr '\n' ' ')
    echo "top_n=$tn trail_pct=$tp: $result"
  done
done

# 批次3: lookback + rebalance
echo ""
echo "--- 批次3: lookback + rebalance ---"
for lb in 10 20 30; do
  for rb in 3 5 10; do
    result=$($BASE_CMD --rotation-interval=20 --init-pct=0.50 --rebalance=$rb --lookback=$lb 2>&1 | grep -E "年化收益|最大回撤" | tr '\n' ' ')
    echo "lookback=$lb rebalance=$rb: $result"
  done
done

# 批次4: rsi_entry_max + 额外flag
echo ""
echo "--- 批次4: rsi_entry_max ---"
for rsi in 40 50 55 65 70; do
  result=$($BASE_CMD --rotation-interval=20 --init-pct=0.50 --rebalance=3 --rsi-entry-max=$rsi 2>&1 | grep -E "年化收益|最大回撤" | tr '\n' ' ')
  echo "rsi_entry_max=$rsi: $result"
done

# 批次5: trend-entry + sector-diversify + ma60-filter + confirm-days
echo ""
echo "--- 批次5: 特殊flag ---"
for flag in "--trend-entry" "--sector-diversify" "--ma60-filter" "--confirm-days=2" "--confirm-days=3" "--dynamic-pct" "--trend-entry --sector-diversify" "--ma60-filter --trend-entry"; do
  result=$($BASE_CMD --rotation-interval=20 --init-pct=0.50 --rebalance=3 $flag 2>&1 | grep -E "年化收益|最大回撤" | tr '\n' ' ')
  echo "flag=$flag: $result"
done

echo ""
echo "============================================"
echo "搜索完成"
echo "============================================"
