#!/usr/bin/env python3
"""同花顺重启脚本（不撤单）"""
import os
import sys
import time

# 解析参数
no_revoke = "--no-revoke" in sys.argv

# 杀死同花顺
os.system("pkill -9 -x 同花顺 2>/dev/null")
time.sleep(3)

# 启动同花顺
os.system("open -a /Applications/同花顺.app")
time.sleep(5)

print("✅ 同花顺已重启（不撤单）")