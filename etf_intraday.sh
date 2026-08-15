#!/bin/bash
# ETF 盘中监控 — macOS，每30分钟，无信号静默
# P3-3: 使用新管线 signal_generator → decision_engine → executor
source /Users/lujie/Documents/code/quant/.venv/bin/activate
SCRIPTS=/Users/lujie/Documents/code/quant

# 主信号（新管线：decision_engine 内部调用 signal_generator）
SIG_OUT=$(python3 "$SCRIPTS/decision_engine.py" 2>/dev/null)

# 解析主信号，提取有操作的标的
HAS_ACTION=false
FORMATTED=""
if [ -n "$SIG_OUT" ]; then
    FORMATTED=$(echo "$SIG_OUT" | python3 -c "
import sys, json
t = sys.stdin.read()
# 跳过日志行，找到第一个 {
s = t.rfind('{')
e = t.rfind('}') + 1 if s >= 0 else -1
if s < 0 or e <= s:
    sys.exit()
d = json.loads(t[s:e])
signals = d.get('signals', {})
lines = []
for code, sig in signals.items():
    a = sig.get('action', '')
    if '买' in a or '入' in a or '卖' in a or '清' in a or '减' in a:
        lines.append(f'  {code} → {a}: {sig.get(\"reason\", \"\")[:50]}')
if lines:
    print('📡 盘中信号')
    for l in lines:
        print(l)
" 2>/dev/null)
    [ -n "$FORMATTED" ] && HAS_ACTION=true
fi

# 合并输出
if $HAS_ACTION; then
    echo "🔄 盘中监控 | $(date '+%m-%d %H:%M')"
    [ -n "$FORMATTED" ] && echo "$FORMATTED"
fi