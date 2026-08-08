#!/usr/bin/env python3
"""
深度分析：检查 159532 / 515050 为什么没有产生做T信号
同时诊断 512170 的信号质量
"""

import json
import sys
sys.path.insert(0, '.')

def calc_rsi_wilder(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)

def calc_macd(closes, fast=5, slow=34, sig=5):
    if len(closes) < slow + sig:
        return None, None, None, "数据不足"
    e12 = sum(closes[:fast]) / fast
    e26 = sum(closes[:slow]) / slow
    difs = []
    for i in range(len(closes)):
        if i < fast:
            difs.append(e12 - e26)
            continue
        e12 = closes[i] * (2/(fast+1)) + e12 * (1 - 2/(fast+1))
        if i < slow:
            difs.append(e12 - e26)
            continue
        e26 = closes[i] * (2/(slow+1)) + e26 * (1 - 2/(slow+1))
        difs.append(e12 - e26)
    e9 = difs[slow]
    deas = []
    for d in difs[slow+1:]:
        e9 = d * (2/(sig+1)) + e9 * (1 - 2/(sig+1))
        deas.append(e9)
    if len(deas) < 2:
        return None, None, None, "数据不足"
    d_now, d_prev = difs[-1], difs[-2]
    e_now, e_prev = deas[-1], deas[-2]
    h_now  = 2 * (d_now - e_now)
    h_prev = 2 * (d_prev - e_prev)
    if   d_now > e_now and d_prev <= e_prev: s = "金叉"
    elif d_now < e_now and d_prev >= e_prev: s = "死叉"
    elif d_now > e_now and h_now > h_prev:   s = "红柱放大"
    elif d_now > e_now and h_now < h_prev:   s = "红柱缩短"
    elif d_now < e_now and h_now < h_prev:   s = "绿柱放大"
    elif d_now < e_now and h_now > h_prev:   s = "绿柱缩短"
    else: s = "震荡"
    return round(d_now, 4), round(e_now, 4), round(h_now, 4), s

def calc_vol_ratio(volumes, period=20):
    if len(volumes) < period + 1:
        return None
    avg_vol = sum(volumes[-(period+1):-1]) / period
    if avg_vol > 0:
        return round(volumes[-1] / avg_vol, 2)
    return None

def t0_buy_score(rsi, macd_status, vol_ratio):
    if rsi is None: return 0
    if rsi > 75: return 0
    if rsi < 25: return 3
    if rsi >= 40: return 0
    if macd_status in ["死叉", "绿柱放大"]: return 0
    score = 1
    if macd_status in ["金叉", "红柱放大"]: score += 1
    if vol_ratio is not None and vol_ratio < 1.5: score += 1
    return score

def t0_sell_score(rsi, macd_status, vol_ratio):
    if rsi is None: return 0
    if rsi < 25: return 0
    if rsi > 75: return 3
    if rsi <= 60: return 0
    if macd_status in ["金叉", "红柱放大"]: return 0
    score = 1
    if macd_status in ["死叉", "绿柱放大"]: score += 1
    if vol_ratio is not None and vol_ratio > 1.2: score += 1
    return score


def deep_analyze(code, klines, avg_cost):
    n = len(klines)
    closes = [k['close'] for k in klines]
    volumes = [k['volume'] for k in klines]
    opens = [k['open'] for k in klines]

    print(f"\n{'='*70}")
    print(f"  📈 {code} 深度诊断")
    print(f"  成本: {avg_cost:.3f}, 现价: {closes[-1]:.3f}, 盈利率: {(closes[-1]/avg_cost-1)*100:+.2f}%")
    print(f"{'='*70}")

    # RSI分布分析
    min_bars = 40  # need MACD(5,34,5) = 39 bars minimum
    rsi_history = []
    for i in range(min_bars, n + 1):
        wc = closes[:i]
        r = calc_rsi_wilder(wc, 14)
        if r is not None:
            rsi_history.append((klines[i-1]['time'], r))

    if rsi_history:
        rsi_vals = [x[1] for x in rsi_history]
        print(f"\n  RSI变化: {min(rsi_vals):.1f} ~ {max(rsi_vals):.1f}, 均值: {sum(rsi_vals)/len(rsi_vals):.1f}")
        print(f"  RSI<25: {sum(1 for x in rsi_vals if x < 25)}次, RSI 25-40: {sum(1 for x in rsi_vals if 25 <= x < 40)}次")
        print(f"  RSI 40-60: {sum(1 for x in rsi_vals if 40 <= x <= 60)}次, RSI 60-75: {sum(1 for x in rsi_vals if 60 < x <= 75)}次")
        print(f"  RSI>75: {sum(1 for x in rsi_vals if x > 75)}次")

    # 逐步分析每根K线的评分
    print(f"\n  🔍 逐K线评分（从第{min_bars}根起）:")
    print(f"  {'时间':<12} {'价格':<8} {'RSI':<7} {'MACD':<10} {'量比':<7} {'买入分':<7} {'卖出分':<7} {'备注'}")
    print(f"  {'-'*75}")

    buy_triggers = 0
    sell_triggers = 0
    buy_near = 0
    sell_near = 0

    for i in range(min_bars, n + 1):
        window_closes = closes[:i]
        window_volumes = volumes[:i]
        time_str = klines[i-1]['time']
        price = klines[i-1]['close']

        rsi = calc_rsi_wilder(window_closes, 14)
        dif, dea, bar, macd_status = calc_macd(window_closes, 5, 34, 5)
        vol_ratio = calc_vol_ratio(window_volumes, 20)

        if rsi is None or macd_status == "数据不足":
            continue

        buy_s = t0_buy_score(rsi, macd_status, vol_ratio)
        sell_s = t0_sell_score(rsi, macd_status, vol_ratio)

        note = ""
        if buy_s >= 2 and price < avg_cost * 1.02:
            buy_triggers += 1
            note = "★ T买入信号"
        elif buy_s >= 2:
            note = f"买但价格>{avg_cost*1.02:.3f}" if not (price < avg_cost * 1.02) else ""
            buy_near += 1

        if sell_s >= 2 and price > avg_cost:
            sell_triggers += 1
            note = "★ T卖出信号"
        elif sell_s >= 2:
            note = f"卖但价格<={avg_cost:.3f}"
            sell_near += 1

        # 只打印有意义的行（任何分数>=1或特殊时间）
        if buy_s >= 1 or sell_s >= 1 or (i % 8 == 0):
            print(f"  {time_str:<12} {price:<8.3f} {rsi:<7.1f} {macd_status:<10} {vol_ratio or 'N/A':<7} {buy_s:<7} {sell_s:<7} {note}")

    print(f"\n  📊 汇总: T买入触发{buy_triggers}次, T卖出触发{sell_triggers}次")
    print(f"     买入接近({buy_near}次), 卖出接近({sell_near}次)")

    # 诊断为什么信号少
    print(f"\n  🩺 诊断:")
    rsi_vals_all = []
    for i in range(min_bars, n + 1):
        r = calc_rsi_wilder(closes[:i], 14)
        if r is not None:
            rsi_vals_all.append(r)

    if all(r >= 40 for r in rsi_vals_all):
        print(f"     ❌ 全天RSI≥40，买入信号无法触发（需要RSI<40）")
    if all(r <= 60 for r in rsi_vals_all):
        print(f"     ❌ 全天RSI≤60，卖出信号无法触发（需要RSI>60）")

    # MACD方向分析
    macd_history = []
    for i in range(min_bars, n + 1):
        _, _, _, s = calc_macd(closes[:i], 5, 34, 5)
        if s != "数据不足":
            macd_history.append((klines[i-1]['time'], s))

    bullish = sum(1 for _, s in macd_history if s in ['金叉', '红柱放大', '红柱缩短'])
    bearish = sum(1 for _, s in macd_history if s in ['死叉', '绿柱放大', '绿柱缩短'])
    print(f"     MACD统计: 多头{bullish}次, 空头{bearish}次")

    # 价格变动模式
    pct_changes = [(closes[i] - closes[i-1]) / closes[i-1] * 100 for i in range(1, len(closes))]
    up_bars = sum(1 for p in pct_changes if p > 0)
    down_bars = sum(1 for p in pct_changes if p < 0)
    print(f"     涨跌K线: 涨{up_bars}根, 跌{down_bars}根, 最大波动{max(abs(p) for p in pct_changes):.2f}%")

    return buy_triggers, sell_triggers


# ── Main ──
with open('/tmp/t0_analysis_klines.json') as f:
    all_klines = json.load(f)

POSITIONS = {
    '512170': 0.329,
    '159532': 1.382,
    '515050': 0.977,
}

for code in ['512170', '159532', '515050']:
    deep_analyze(code, all_klines[code], POSITIONS[code])