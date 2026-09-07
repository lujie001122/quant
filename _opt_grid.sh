#!/bin/bash
# 参数网格搜索 - 基于当前最优参数做微调
cd /Users/lujie/Documents/code/quant

BASE_CMD="python3 backtest_bt.py --concentrated --weekly-rotation --momentum=simple"

echo "============================================"
echo "参数网格搜索 - 目标年化50%"
echo "============================================"
echo ""

# 1. trail_pct: 0.08, 0.10, 0.12(当前), 0.15, 0.18
echo "--- 1. trail_pct ---"
for tp in 0.08 0.10 0.12 0.15 0.18; do
  result=$($BASE_CMD --rotation-interval=20 --init-pct=0.50 --rebalance=3 --trail-pct=$tp 2>&1 | grep -E "年化收益|最大回撤" | tr '\n' ' ')
  echo "trail_pct=$tp: $result"
done

# 2. top_n: 2, 3(当前), 4, 5
echo ""
echo "--- 2. top_n ---"
for tn in 2 3 4 5; do
  result=$($BASE_CMD --rotation-interval=20 --init-pct=0.50 --rebalance=3 --top-n=$tn 2>&1 | grep -E "年化收益|最大回撤" | tr '\n' ' ')
  echo "top_n=$tn: $result"
done

# 3. lookback: 10, 15, 20(当前), 25, 30
echo ""
echo "--- 3. lookback ---"
for lb in 10 15 20 25 30; do
  result=$($BASE_CMD --rotation-interval=20 --init-pct=0.50 --rebalance=3 --lookback=$lb 2>&1 | grep -E "年化收益|最大回撤" | tr '\n' ' ')
  echo "lookback=$lb: $result"
done

# 4. rotation_interval: 5, 10, 15, 20(当前), 25
echo ""
echo "--- 4. rotation_interval ---"
for ri in 5 10 15 20 25; do
  result=$($BASE_CMD --rotation-interval=$ri --init-pct=0.50 --rebalance=3 2>&1 | grep -E "年化收益|最大回撤" | tr '\n' ' ')
  echo "rotation_interval=$ri: $result"
done

# 5. rebalance: 1, 2, 3(当前), 5, 10
echo ""
echo "--- 5. rebalance ---"
for rb in 1 2 3 5 10; do
  result=$($BASE_CMD --rotation-interval=20 --init-pct=0.50 --rebalance=$rb 2>&1 | grep -E "年化收益|最大回撤" | tr '\n' ' ')
  echo "rebalance=$rb: $result"
done

# 6. init_pct: 0.30, 0.50(当前), 0.70, 1.0
echo ""
echo "--- 6. init_pct ---"
for ip in 0.30 0.50 0.70 1.0; do
  result=$($BASE_CMD --rotation-interval=20 --init-pct=$ip --rebalance=3 2>&1 | grep -E "年化收益|最大回撤" | tr '\n' ' ')
  echo "init_pct=$ip: $result"
done

echo ""
echo "============================================"
echo "网格搜索完成"
echo "============================================"
