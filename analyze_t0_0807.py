#!/usr/bin/env python3
"""
分析 8月7日 做T信号 + 配对挂单成交率
对标: 512170 / 159532 / 515050
"""

import json
import sys
sys.path.insert(0, '.')

# ── 持仓数据（来自 portfolio.json 8月7日） ──
POSITIONS = {
    '512170': {'name': '医疗ETF华', 'shares': 67000, 'avg_cost': 0.329},
    '159532': {'name': '中证2000ET', 'shares': 23500, 'avg_cost': 1.382},
    '515050': {'name': '通信ETF华', 'shares': 52200, 'avg_cost': 0.977},
}

# ── 技术指标函数（从 signal_generator.py 复制） ──

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

def calc_atr_window(klines_window, period=14):
    """计算滑动窗口内ATR"""
    if len(klines_window) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines_window)):
        k_prev, k_curr = klines_window[i - 1], klines_window[i]
        tr = max(k_curr["high"] - k_curr["low"],
                 abs(k_curr["high"] - k_prev["close"]),
                 abs(k_curr["low"] - k_prev["close"]))
        trs.append(tr)
    if len(trs) < period: return None
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return round(atr, 4)

def calc_vol_ratio(volumes, period=20):
    """当前量 / 过去N根均量"""
    if len(volumes) < period + 1:
        return None
    avg_vol = sum(volumes[-(period+1):-1]) / period
    if avg_vol > 0:
        return round(volumes[-1] / avg_vol, 2)
    return None

# ── 评分函数（从 signal_generator.py 复制） ──

def t0_buy_score(rsi_5min, macd_5min_status, vol_ratio_5min):
    if rsi_5min is None:
        return 0
    if rsi_5min > 75:
        return 0
    if rsi_5min < 25:
        return 3
    if rsi_5min >= 40:
        return 0
    if macd_5min_status in ["死叉", "绿柱放大"]:
        return 0
    score = 1
    if macd_5min_status in ["金叉", "红柱放大"]:
        score += 1
    if vol_ratio_5min is not None and vol_ratio_5min < 1.5:
        score += 1
    return score

def t0_sell_score(rsi_5min, macd_5min_status, vol_ratio_5min):
    if rsi_5min is None:
        return 0
    if rsi_5min < 25:
        return 0
    if rsi_5min > 75:
        return 3
    if rsi_5min <= 60:
        return 0
    if macd_5min_status in ["金叉", "红柱放大"]:
        return 0
    score = 1
    if macd_5min_status in ["死叉", "绿柱放大"]:
        score += 1
    if vol_ratio_5min is not None and vol_ratio_5min > 1.2:
        score += 1
    return score

def is_auction_time(time_str):
    """集合竞价时段: 9:15-9:25, 14:57-15:00"""
    h = int(time_str[8:10])
    m = int(time_str[10:12])
    t = h * 60 + m
    return (555 <= t <= 565) or (897 <= t <= 900)

# ── 主分析 ──

def analyze_etf(code, klines, pos_info):
    name = pos_info['name']
    avg_cost = pos_info['avg_cost']
    shares = pos_info['shares']
    active_shares = int(shares * 0.4)  # 活动仓 40%

    n = len(klines)
    closes = [k['close'] for k in klines]
    volumes = [k['volume'] for k in klines]

    # 计算全窗口ATR（用于配对挂单价格）
    full_atr = calc_atr_window(klines, 14)

    print(f"\n{'='*70}")
    print(f"  {code} {name}")
    print(f"  持仓: {shares}股, 成本: {avg_cost:.3f}, 活动仓: {active_shares}股")
    print(f"  全日ATR: {full_atr:.4f} ({full_atr/klines[-1]['close']*100:.2f}%)")
    print(f"  价格区间: {min(closes):.3f} - {max(closes):.3f}, 变动: {(closes[-1]/closes[0]-1)*100:+.2f}%")
    print(f"{'='*70}")

    signals = []
    # 从第15根K线开始（需要至少14+1根才能算RSI，再加MACD需要slow+sig=39根）
    # 实际上MACD(5,34,5)需要34+5=39根，但我们可以逐步计算
    min_bars = max(15, 34 + 5 + 1)  # 40根确保MACD也能算

    for i in range(min_bars, n + 1):
        window = klines[:i]
        time_str = window[-1]['time']
        current_price = window[-1]['close']

        # 集合竞价过滤
        if is_auction_time(time_str):
            continue

        # 计算滑动窗口指标
        window_closes = [k['close'] for k in window]
        window_volumes = [k['volume'] for k in window]

        rsi = calc_rsi_wilder(window_closes, 14)
        dif, dea, bar, macd_status = calc_macd(window_closes, 5, 34, 5)
        vol_ratio = calc_vol_ratio(window_volumes, 20)

        if rsi is None or macd_status == "数据不足":
            continue

        # 买入评分
        buy_score = t0_buy_score(rsi, macd_status, vol_ratio)
        if buy_score >= 2:
            # 额外条件: 现价 < 持仓成本 × 1.02
            if current_price < avg_cost * 1.02:
                signals.append({
                    'time': time_str,
                    'type': 'T买入',
                    'score': buy_score,
                    'price': current_price,
                    'rsi': rsi,
                    'macd': macd_status,
                    'vol_ratio': vol_ratio
                })

        # 卖出评分
        sell_score = t0_sell_score(rsi, macd_status, vol_ratio)
        if sell_score >= 2:
            # 额外条件: 现价 > 持仓成本
            if current_price > avg_cost:
                signals.append({
                    'time': time_str,
                    'type': 'T卖出',
                    'score': sell_score,
                    'price': current_price,
                    'rsi': rsi,
                    'macd': macd_status,
                    'vol_ratio': vol_ratio
                })

    # ── 打印信号列表 ──
    print(f"\n  📊 做T信号（共 {len(signals)} 次）:")
    if signals:
        print(f"  {'时间':<12} {'类型':<8} {'评分':<5} {'价格':<8} {'RSI':<7} {'MACD':<10} {'量比':<6}")
        print(f"  {'-'*60}")
        for s in signals:
            print(f"  {s['time']:<12} {s['type']:<8} {s['score']:<5} {s['price']:<8.3f} {s['rsi']:<7.2f} {s['macd']:<10} {s['vol_ratio'] or 'N/A':<6}")

    # ── 时间段分布 ──
    if signals:
        morning = [s for s in signals if s['time'] < '202608071130']
        afternoon = [s for s in signals if '202608071300' <= s['time'] < '202608071500']
        late = [s for s in signals if s['time'] >= '202608071500']
        print(f"\n  ⏰ 时间段分布:")
        print(f"     上午(9:30-11:30): {len(morning)}次")
        for s in morning:
            print(f"       {s['time']} {s['type']} score={s['score']} price={s['price']:.3f}")
        print(f"     下午(13:00-15:00): {len(afternoon)}次")
        for s in afternoon:
            print(f"       {s['time']} {s['type']} score={s['score']} price={s['price']:.3f}")
        print(f"     尾盘(15:00): {len(late)}次")

    # ── 配对挂单成交率分析 ──
    print(f"\n  🎯 配对挂单成交率分析:")
    print(f"     全窗口ATR={full_atr:.4f}")

    atr_multipliers = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    print(f"\n     {'ATR倍数':<8} {'配对价差':<10} {'信号数':<7} {'成交':<5} {'成交率':<8} {'备注'}")
    print(f"     {'-'*55}")

    for mult in atr_multipliers:
        spread = full_atr * mult
        filled = 0
        total = len(signals)
        fill_details = []

        for s in signals:
            pair_price = s['price'] + spread if s['type'] == 'T买入' else s['price'] - spread
            # 检查后续K线是否触达配对价
            signal_idx = None
            for j, k in enumerate(klines):
                if k['time'] == s['time']:
                    signal_idx = j
                    break
            if signal_idx is None:
                continue

            hit = False
            hit_time = None
            hit_price = None
            for k in klines[signal_idx + 1:]:
                if s['type'] == 'T买入':
                    # 配对卖出: 需要后续价格 >= 配对价
                    if k['high'] >= pair_price:
                        hit = True
                        hit_time = k['time']
                        hit_price = k['high']
                        break
                else:
                    # 配对买入: 需要后续价格 <= 配对价
                    if k['low'] <= pair_price:
                        hit = True
                        hit_time = k['time']
                        hit_price = k['low']
                        break

            if hit:
                filled += 1
                fill_details.append((s['time'], s['type'], s['price'], pair_price, hit_time, hit_price))

        rate = filled / total * 100 if total > 0 else 0
        note = ""
        if mult == 2.0:
            note = "← 当前配置"
        print(f"     ATR×{mult:<4.1f} {spread:<10.4f} {total:<7} {filled:<5} {rate:<8.1f}% {note}")

        if total > 0 and (mult == 1.0 or mult == 1.5 or mult == 2.0):
            for detail in fill_details:
                sig_time, sig_type, sig_price, pair_p, hit_time, hit_p = detail
                print(f"       ✓ {sig_time} {sig_type} {sig_price:.3f}→{pair_p:.3f} 触达@{hit_time} {hit_p:.3f}")
            unfilled = total - filled
            if unfilled > 0:
                for s in signals:
                    signal_idx = next((j for j, k in enumerate(klines) if k['time'] == s['time']), None)
                    pair_price = s['price'] + spread if s['type'] == 'T买入' else s['price'] - spread
                    hit = any(
                        (k['high'] >= pair_price if s['type'] == 'T买入' else k['low'] <= pair_price)
                        for k in klines[signal_idx + 1:]
                    ) if signal_idx else False
                    if not hit:
                        # 找最接近的
                        extreme = max(k['high'] for k in klines[signal_idx+1:]) if s['type'] == 'T买入' else min(k['low'] for k in klines[signal_idx+1:])
                        print(f"       ✗ {s['time']} {s['type']} {s['price']:.3f}→{pair_price:.3f} 未触达(最接近:{extreme:.3f})")

    # ── 价差校验 ──
    print(f"\n  💰 价差校验（(卖价-买价) × 数量 > 100元）:")
    t0_buy_amount = active_shares if active_shares > 0 else int(shares * 0.4)
    t0_sell_amount = int(shares * 0.2 // 100) * 100
    print(f"     T买入数量: {t0_buy_amount}股, T卖出数量: {t0_sell_amount}股")
    for mult in [1.0, 1.5, 2.0, 2.5]:
        spread = full_atr * mult
        for s in signals:
            amt = t0_buy_amount if s['type'] == 'T买入' else t0_sell_amount
            profit = spread * amt
            status = "✅" if profit > 100 else "❌"
            if mult == 2.0:
                print(f"     ATR×{mult} {s['type']}@{s['time']}: 价差{spread:.4f}×{amt}股={profit:.0f}元 {status}")

    return signals, full_atr


# ── 整体汇总 ──
def main():
    with open('/tmp/t0_analysis_klines.json') as f:
        all_klines = json.load(f)

    all_results = {}
    total_signals = 0

    for code in ['512170', '159532', '515050']:
        signals, atr = analyze_etf(code, all_klines[code], POSITIONS[code])
        all_results[code] = {'signals': signals, 'atr': atr}
        total_signals += len(signals)

    # ── 综合建议 ──
    print(f"\n\n{'='*70}")
    print(f"  📋 综合汇总与建议")
    print(f"{'='*70}")
    print(f"\n  三只标的总信号数: {total_signals}")

    print(f"\n  🔑 关键发现:")
    for code in ['512170', '159532', '515050']:
        r = all_results[code]
        s = r['signals']
        buys = [x for x in s if x['type'] == 'T买入']
        sells = [x for x in s if x['type'] == 'T卖出']
        pos = POSITIONS[code]
        print(f"     {code} {pos['name']}: {len(s)}信号(买{len(buys)}/卖{len(sells)}), ATR={r['atr']:.4f}")

    print(f"\n  💡 建议:")

    # 计算各ATR倍数下的加权成交率
    print(f"\n     ATR倍数建议:")
    atr_multipliers = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    for mult in atr_multipliers:
        total_filled = 0
        total_sig = 0
        for code in ['512170', '159532', '515050']:
            signals = all_results[code]['signals']
            atr = all_results[code]['atr']
            klines = all_klines[code]
            spread = atr * mult
            for s in signals:
                total_sig += 1
                pair_price = s['price'] + spread if s['type'] == 'T买入' else s['price'] - spread
                signal_idx = next((j for j, k in enumerate(klines) if k['time'] == s['time']), None)
                if signal_idx:
                    hit = any(
                        (k['high'] >= pair_price if s['type'] == 'T买入' else k['low'] <= pair_price)
                        for k in klines[signal_idx + 1:]
                    )
                    if hit:
                        total_filled += 1
        rate = total_filled / total_sig * 100 if total_sig > 0 else 0
        marker = " ★推荐" if 60 <= rate <= 85 else (" ← 当前" if mult == 2.0 else "")
        print(f"       ATR×{mult:.1f}: {total_filled}/{total_sig} = {rate:.0f}%{marker}")

    print(f"\n     ⚠️ 价差阈值建议: 维持 >100元，但考虑到小额ETF(512170价格0.33)，")
    print(f"        建议改为 >50元 或按百分比(>0.3%价差)校验")

    print(f"\n  📊 整体评价:")
    print(f"     8月7日是一个典型的单边上涨日（512170涨4.85%，159532涨1.25%，515050涨1.46%），")
    print(f"     做T信号在单边市中表现如下：")
    print(f"     - 买入信号在上涨趋势中容易触发但难配对卖出（因为价格持续上涨，卖出配对价过高）")
    print(f"     - 卖出信号在上涨趋势中稀少（RSI不容易超买回撤）")
    print(f"     - 单边市中做T风险大于收益，建议加入趋势过滤（如MA5/MA20方向判断）")

if __name__ == '__main__':
    main()