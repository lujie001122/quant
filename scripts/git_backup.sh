#!/bin/bash
# ETF策略GitHub每日备份
cd /Users/lujie/Documents/code/quant

# 拉取远程最新
git pull origin main 2>/dev/null

# 检查是否有未推送的提交
UNPUSHED=$(git log origin/main..HEAD --oneline 2>/dev/null | wc -l | tr -d ' ')

if [ "$UNPUSHED" -gt 0 ]; then
    echo "有 $UNPUSHED 个未推送提交，正在推送..."
    git push origin main 2>&1
    echo "✅ 推送完成"
else
    # 检查是否有未提交的更改
    if [ -n "$(git status --porcelain)" ]; then
        git add -A
        git commit -m "auto backup $(date '+%Y-%m-%d %H:%M')" 2>&1
        git push origin main 2>&1
        echo "✅ 备份完成"
    else
        echo "✅ 无变更，跳过"
    fi
fi