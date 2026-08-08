#!/usr/bin/env python3
"""更细节的诊断"""
import json, sys
sys.path.insert(0, '.')

def calc_rsi_wilder(closes, period=14):
    if len(closes) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0)); losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)

def calc_macd(closes, fast=5, slow=34, sig=5):
    if len(closes) < slow + sig: return None, None, None, "数据不足"
    e12 = sum(closes[:fast]) / fast; e26 = sum(closes[:slow]) / slow
    difs = []
    for i in range(len(closes)):
        if i < fast: difs.append(e12 - e26); continue
        e12 = closes[i] * (2/(fast+1)) + e12 * (1 - 2/(fast+1))
        if i < slow: difs.append(e12 - e26); continue
        e26 = closes[i] * (2/(slow+1)) + e26 * (1 - 2/(slow+1))
        difs.append(e12 - e26)
    e9 = difs[slow]; deas = []
    for d in difs[slow+1:]:
        e9 = d * (2/(sig+1)) + e9 * (1 - 2/(sig+1))
        deas.append(e9)
    if len(deas) < 2: return None, None, None, "数据不足"
    d_now, d_prev = difs[-1], difs[-2]
    e_now, e_prev = deas[-1], deas[-2]
    h_now = 2*(d_now-e_now); h_prev = 2*(d_prev-e_prev)
    if d_now>e_now and d_prev<=e_prev: s="金叉"
    elif d_now<e_now and d_prev>=e_prev: s="死叉"
    elif d_now>e_now and h_now>h_prev: s="红柱放大"
    elif d_now>e_now and h_now<h_prev: s="红柱缩短"
    elif d_now<e_now and h_now<h_prev: s="绿柱放大"
    elif d_now<e_now and h_now>h_prev: s="绿柱缩短"
    else: s="震荡"
    return round(d_now,4), round(e_now,4), round(h_now,4), s

with open('/tmp/t0_analysis_klines.json') as f:
    all_klines = json.load(f)

# 515050: Check all bars where RSI < 40
klines = all_klines['515050']
closes = [k['close'] for k in klines]
volumes = [k['volume'] for k in klines]
min_bars = 16
print("515050 - 所有RSI<40的时间点:")
for i in range(min_bars, len(klines) + 1):
    wc = closes[:i]; wv = volumes[:i]
    rsi = calc_rsi_wilder(wc, 14)
    if rsi is not None and rsi < 40:
        time_str = klines[i-1]['time']; price = closes[i-1]
        if len(wv) >= 21:
            avg_vol = sum(wv[-21:-1])/20
            vol_ratio = round(wv[-1]/avg_vol, 2) if avg_vol > 0 else None
        else:
            vol_ratio = None
        macd_s = "N/A"
        if len(wc) >= 40:
            _,_,_,s = calc_macd(wc, 5, 34, 5)
            macd_s = s
        print(f"  {time_str} price={price:.3f} RSI={rsi:.1f} MACD={macd_s} vol_ratio={vol_ratio}")

# 512170: RSI milestones
print("\n512170 - RSI关键突破点:")
klines = all_klines['512170']
closes = [k['close'] for k in klines]
seen_40 = False; seen_60 = False; seen_75 = False
for i in range(min_bars, len(klines) + 1):
    wc = closes[:i]
    rsi = calc_rsi_wilder(wc, 14)
    if rsi is not None:
        time_str = klines[i-1]['time']; price = closes[i-1]
        if not seen_40 and rsi >= 40:
            seen_40 = True; print(f"  RSI≥40: {time_str} price={price:.3f} RSI={rsi:.1f}")
        if not seen_60 and rsi >= 60:
            seen_60 = True; print(f"  RSI≥60: {time_str} price={price:.3f} RSI={rsi:.1f}")
        if not seen_75 and rsi >= 75:
            seen_75 = True; print(f"  RSI≥75: {time_str} price={price:.3f} RSI={rsi:.1f}")

# All three ETFs: price vs avg_cost relationship
print("\n价格 vs 成本对比:")
for code, cost in [('512170', 0.329), ('159532', 1.382), ('515050', 0.977)]:
    closes = [k['close'] for k in all_klines[code]]
    below = sum(1 for c in closes if c < cost)
    above = sum(1 for c in closes if c >= cost)
    print(f"  {code}: 成本={cost:.3f}, 低于成本{below}根, 高于成本{above}根, "
          f"范围{min(closes):.3f}-{max(closes):.3f}")

# 512170 最后8根K线详细
print("\n512170 尾盘详细（最后8根）:")
klines = all_klines['512170']
closes = [k['close'] for k in klines]
for j in range(len(klines)-8, len(klines)):
    k = klines[j]
    print(f"  {k['time']} O={k['open']:.3f} H={k['high']:.3f} L={k['low']:.3f} C={k['close']:.3f} V={k['volume']:.0f}")