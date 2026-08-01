#!/bin/bash
# 每日备份策略文件到GitHub
REPO="/Users/lujie/Documents/code/quant"
SCRIPTS="/Users/lujie/hermes/scripts"
SKILL_DIR="/Users/lujie/.hermes/skills/mlops/etf-trading"

cd "$REPO"

# 同步最新策略文件
cp "$SCRIPTS"/backtest_bt.py .
cp "$SCRIPTS"/signal_generator.py .
cp "$SCRIPTS"/intraday_t_once.py .
cp "$SCRIPTS"/sentiment_check.py .
cp "$SCRIPTS"/ths_client.py .
cp "$SCRIPTS"/portfolio.json .
cp "$SCRIPTS"/etf_intraday.sh .
cp "$SCRIPTS"/etf_premarket.sh .

# 同步skill
mkdir -p skill references
cp "$SKILL_DIR"/SKILL.md skill/
cp "$SKILL_DIR"/references/*.md references/ 2>/dev/null

# 提交推送
git add -A
CHANGES=$(git diff --cached --stat)
if [ -z "$CHANGES" ]; then
    echo "无变更，跳过"
    exit 0
fi

# 更新README中的日期
sed -i '' "s/更新 ([0-9-]*)/更新 ($(date +%Y-%m-%d))/" README.md
git add README.md

git commit -m "daily backup $(date +%Y-%m-%d)"
git push origin main 2>&1