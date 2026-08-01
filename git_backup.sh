#!/bin/bash
# 每日备份策略文件到GitHub
REPO="/Users/lujie/Documents/code/quant"

cd "$REPO"

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