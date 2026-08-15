#!/bin/bash
# ETF 盘前信号 — macOS
# P3-3: 使用新管线 signal_generator → decision_engine → executor
source /Users/lujie/Documents/code/quant/.venv/bin/activate
SCRIPTS=/Users/lujie/Documents/code/quant

echo "📊 ETF 盘前信号 | $(date '+%m-%d %H:%M')"
echo ""

# 舆情
echo "🔍 舆情扫描"
python3 "$SCRIPTS/sentiment_check.py" 2>/dev/null
echo ""

# 信号（新管线：decision_engine 内部调用 signal_generator + 决策引擎）
python3 "$SCRIPTS/decision_engine.py" 2>/dev/null | python3 -c "
import sys, json, math
t = sys.stdin.read()
s = t.rfind('{')
e = t.rfind('}') + 1 if s >= 0 else -1
if s < 0 or e <= s:
    print('⚠️ 信号生成失败')
    sys.exit()
d = json.loads(t[s:e])

print(f\"📡 市场环境: {d['market_environment']}\")
print()

# 分类
signals = d['signals']
buy_list = []
sell_list = []
hold_list = []
for code, sig in signals.items():
    a = sig['action']
    if '买' in a or '入' in a:
        buy_list.append((code, sig))
    elif '卖' in a or '清' in a or '减' in a:
        sell_list.append((code, sig))
    else:
        hold_list.append((code, sig))

if buy_list:
    print('🔴 买入信号')
    for code, sig in buy_list:
        reason = sig.get('reason', '')
        print(f'  {code} → {reason[:60]}')
    print()

if sell_list:
    print('🟢 卖出/减仓信号')
    for code, sig in sell_list:
        stop = sig.get('stop_signal', '')[:40]
        reason = sig.get('reason', '')[:40]
        print(f'  {code} → {reason or stop}')
    print()

print('📋 持仓总览')
print(f\"{'ETF':<8} {'价格':>7} {'RSI':>6} {'MACD':>8} {'操作':<12} {'信号'}\")
print('─' * 55)
for code, sig in signals.items():
    price = sig.get('price', 0)
    price_str = f'{price:.3f}' if isinstance(price, (int, float)) and not math.isnan(price) else 'N/A'
    rsi = sig.get('rsi', '?')
    macd = sig.get('macd_status', '?')
    action = sig['action']
    stop = sig.get('stop_signal', '')[:25]
    print(f'{code:<8} {price_str:>7} {str(rsi):>6} {macd:>8} {action:<12} {stop}')
"