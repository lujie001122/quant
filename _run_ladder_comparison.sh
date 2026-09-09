#!/bin/bash
# 阶梯锁利+增强趋势止盈 回测对比
# 4组: 基线 / 方案1(阶梯锁利) / 方案2(增强趋势止盈) / 方案1+2

set -e
cd "$(dirname "$0")"

PERIOD="--start=2024-07-01 --end=2026-06-30"
MODE="--concentrated-pyramid"
PYTHON=".venv/bin/python"

echo "═══════════════════════════════════════════════════════════"
echo "  阶梯锁利+增强趋势止盈 回测对比"
echo "═══════════════════════════════════════════════════════════"
echo ""

# 基线
echo "━━━ 1/4: 基线 (无增强) ━━━"
$PYTHON backtest_bt.py $MODE $PERIOD 2>&1 | grep -E "总收益|年化|最大回撤|夏普|交易笔数|胜率|盈亏比|初始|最终|Calmar"
echo ""

# 方案1: 阶梯锁利
echo "━━━ 2/4: 方案1 — 阶梯锁利 ━━━"
$PYTHON backtest_bt.py $MODE $PERIOD --ladder 2>&1 | grep -E "总收益|年化|最大回撤|夏普|交易笔数|胜率|盈亏比|初始|最终|Calmar"
echo ""

# 方案2: 增强趋势止盈
echo "━━━ 3/4: 方案2 — 增强趋势止盈 ━━━"
$PYTHON backtest_bt.py $MODE $PERIOD --enhanced-trend 2>&1 | grep -E "总收益|年化|最大回撤|夏普|交易笔数|胜率|盈亏比|初始|最终|Calmar"
echo ""

# 方案1+2
echo "━━━ 4/4: 方案1+2 — 阶梯锁利+增强趋势止盈 ━━━"
$PYTHON backtest_bt.py $MODE $PERIOD --ladder --enhanced-trend 2>&1 | grep -E "总收益|年化|最大回撤|夏普|交易笔数|胜率|盈亏比|初始|最终|Calmar"
echo ""

echo "═══════════════════════════════════════════════════════════"
echo "  对比完成"
echo "═══════════════════════════════════════════════════════════"
