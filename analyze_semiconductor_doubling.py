#!/usr/bin/env python3
"""
分析159516半导体设备ETF和588170科创半导体ETF在2026年3-6月翻倍行情中的指标
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from market_data import fetch_klines_daily
from factors.indicators import calc_ma, calc_rsi_wilder, calc_macd, calc_atr, calc_vol_ratio
from factors.rsi import calc_rsi
from factors.atr import calc_atr as calc_atr_pure
import json

def get_full_rsi_series(closes, period=14):
    """计算完整的RSI序列（每根K线）"""
    rsi_series = []
    for i in range(len(closes)):
        if i < period:
            rsi_series.append(None)
        else:
            rsi = calc_rsi(closes[:i+1], period)
            rsi_series.append(rsi)
    return rsi_series

def get_full_macd_series(closes, fast=12, slow=26, sig=9):
    """计算完整的MACD序列（每根K线）"""
    difs, deas, bars, statuses = [], [], [], []
    for i in range(len(closes)):
        if i < slow + sig:
            difs.append(None); deas.append(None); bars.append(None); statuses.append(None)
        else:
            dif, dea, bar, status = calc_macd(closes[:i+1], fast, slow, sig)
            difs.append(dif); deas.append(dea); bars.append(bar); statuses.append(status)
    return difs, deas, bars, statuses

def get_ma_series(closes, period):
    """计算完整MA序列"""
    mas = []
    for i in range(len(closes)):
        if i < period - 1:
            mas.append(None)
        else:
            mas.append(round(sum(closes[i-period+1:i+1])/period, 4))
    return mas

def get_ma20_slope(ma20_series):
    """计算MA20斜率：每根K线的MA20相对于5天前的变化率"""
    slopes = []
    for i in range(len(ma20_series)):
        if i < 5 or ma20_series[i] is None or ma20_series[i-5] is None or ma20_series[i-5] == 0:
            slopes.append(None)
        else:
            slopes.append(round((ma20_series[i] - ma20_series[i-5]) / ma20_series[i-5] * 100, 4))
    return slopes

def get_atr_series(klines, period=14):
    """计算完整ATR序列"""
    atrs = []
    for i in range(len(klines)):
        if i < period:
            atrs.append(None)
        else:
            atr = calc_atr(klines[:i+1], period)
            atrs.append(atr)
    return atrs

def get_vol_ratio_series(volumes, period=20):
    """计算完整量比序列"""
    ratios = []
    for i in range(len(volumes)):
        if i < period:
            ratios.append(None)
        else:
            avg = sum(volumes[i-period:i]) / period
            ratios.append(round(volumes[i] / avg, 2) if avg > 0 else None)
    return ratios

def get_rsi_20d_max(rsi_series):
    """计算RSI近20日最高值序列"""
    maxs = []
    for i in range(len(rsi_series)):
        if i < 20:
            maxs.append(None)
        else:
            vals = [v for v in rsi_series[i-19:i+1] if v is not None]
            maxs.append(max(vals) if vals else None)
    return maxs

def analyze_etf(code, name):
    """分析单只ETF"""
    print(f"\n{'='*80}")
    print(f"  {code} {name}")
    print(f"{'='*80}")
    
    # 拉取数据（从2025-09-01开始，确保指标预热线足够）
    klines = fetch_klines_daily(code, start="20250901", end="20260825")
    print(f"数据条数: {len(klines)}, 区间: {klines[0]['date']} ~ {klines[-1]['date']}")
    
    closes = [k['close'] for k in klines]
    highs = [k['high'] for k in klines]
    lows = [k['low'] for k in klines]
    volumes = [k['volume'] for k in klines]
    dates = [k['date'] for k in klines]
    
    # 计算指标序列
    ma5 = get_ma_series(closes, 5)
    ma10 = get_ma_series(closes, 10)
    ma20 = get_ma_series(closes, 20)
    rsi = get_full_rsi_series(closes, 14)
    difs, deas, bars, statuses = get_full_macd_series(closes)
    ma20_slope = get_ma20_slope(ma20)
    atrs = get_atr_series(klines, 14)
    vol_ratios = get_vol_ratio_series(volumes, 20)
    rsi_20d_max = get_rsi_20d_max(rsi)
    
    # 只分析2026年数据
    start_idx = None
    end_idx = None
    for i, d in enumerate(dates):
        if d >= "2026-01-01" and start_idx is None:
            start_idx = i
        if d <= "2026-08-25":
            end_idx = i
    
    if start_idx is None:
        print("ERROR: 未找到2026年数据")
        return None
    
    # 月度关键指标
    monthly_checkpoints = [
        ("2026-01-02", "1月"),
        ("2026-02-02", "2月"),
        ("2026-03-01", "3月"),
        ("2026-04-01", "4月"),
        ("2026-05-04", "5月"),
        ("2026-06-01", "6月"),
        ("2026-07-01", "7月"),
        ("2026-08-03", "8月"),
    ]
    
    print(f"\n{'='*80}")
    print(f"  月度关键指标时间线")
    print(f"{'='*80}")
    print(f"{'月份':<8} {'日期':<12} {'收盘价':>8} {'RSI':>8} {'DIF':>10} {'DEA':>10} {'MACD柱':>10} {'状态':<10} {'MA5':>8} {'MA10':>8} {'MA20':>8} {'多头排列':<10} {'MA20斜率':>10} {'量比':>6} {'ATR':>8} {'RSI20高':>8}")
    print("-" * 150)
    
    for check_date, month_label in monthly_checkpoints:
        # 找最接近的交易日
        best_idx = None
        for i in range(start_idx, end_idx + 1):
            if dates[i] >= check_date:
                best_idx = i
                break
        if best_idx is None:
            continue
        
        i = best_idx
        is_bull_align = ""
        if ma5[i] and ma10[i] and ma20[i]:
            if ma5[i] > ma10[i] > ma20[i]:
                is_bull_align = "✅是"
            else:
                is_bull_align = "❌否"
        
        slope_str = f"{ma20_slope[i]:.2f}%" if ma20_slope[i] is not None else "N/A"
        rsi_str = f"{rsi[i]:.1f}" if rsi[i] is not None else "N/A"
        dif_str = f"{difs[i]:.4f}" if difs[i] is not None else "N/A"
        dea_str = f"{deas[i]:.4f}" if deas[i] is not None else "N/A"
        bar_str = f"{bars[i]:.4f}" if bars[i] is not None else "N/A"
        status_str = statuses[i] if statuses[i] else "N/A"
        ma5_str = f"{ma5[i]:.4f}" if ma5[i] else "N/A"
        ma10_str = f"{ma10[i]:.4f}" if ma10[i] else "N/A"
        ma20_str = f"{ma20[i]:.4f}" if ma20[i] else "N/A"
        vol_str = f"{vol_ratios[i]:.2f}" if vol_ratios[i] else "N/A"
        atr_str = f"{atrs[i]:.4f}" if atrs[i] else "N/A"
        rsi20_str = f"{rsi_20d_max[i]:.1f}" if rsi_20d_max[i] else "N/A"
        
        print(f"{month_label:<8} {dates[i]:<12} {closes[i]:>8.4f} {rsi_str:>8} {dif_str:>10} {dea_str:>10} {bar_str:>10} {status_str:<10} {ma5_str:>8} {ma10_str:>8} {ma20_str:>8} {is_bull_align:<10} {slope_str:>10} {vol_str:>6} {atr_str:>8} {rsi20_str:>8}")
    
    # 找趋势启动信号
    print(f"\n{'='*80}")
    print(f"  趋势启动信号分析")
    print(f"{'='*80}")
    
    # 1. MA20斜率从负转正
    print("\n--- MA20斜率从负转正 ---")
    ma20_turn = None
    for i in range(start_idx, end_idx + 1):
        if ma20_slope[i] is not None and ma20_slope[i] > 0:
            # 检查前5天基本都是负的
            neg_count = 0
            for j in range(max(start_idx, i-5), i):
                if ma20_slope[j] is not None and ma20_slope[j] < 0:
                    neg_count += 1
            if neg_count >= 3 and ma20_turn is None:
                ma20_turn = i
                print(f"  MA20斜率首次转正: {dates[i]}  斜率={ma20_slope[i]:.4f}%  收盘价={closes[i]:.4f}")
    
    # 2. MACD零轴上金叉
    print("\n--- MACD零轴上金叉(DIF>0 + 金叉) ---")
    macd_zero_golden = []
    for i in range(start_idx, end_idx + 1):
        if statuses[i] == "金叉" and difs[i] is not None and difs[i] > 0:
            macd_zero_golden.append((i, dates[i], closes[i], difs[i], deas[i]))
    
    for idx, dt, price, dif, dea in macd_zero_golden:
        print(f"  {dt}: 收盘价={price:.4f}  DIF={dif:.4f}  DEA={dea:.4f}")
    
    # 3. 多头排列形成
    print("\n--- 多头排列首次形成(MA5>MA10>MA20) ---")
    bull_align_first = None
    for i in range(start_idx, end_idx + 1):
        if ma5[i] and ma10[i] and ma20[i] and ma5[i] > ma10[i] > ma20[i]:
            if bull_align_first is None:
                bull_align_first = i
                print(f"  多头排列首次形成: {dates[i]}  收盘价={closes[i]:.4f}  MA5={ma5[i]:.4f}  MA10={ma10[i]:.4f}  MA20={ma20[i]:.4f}")
    
    # 持续多头排列的天数
    bull_align_periods = []
    in_bull = False
    bull_start = None
    for i in range(start_idx, end_idx + 1):
        if ma5[i] and ma10[i] and ma20[i] and ma5[i] > ma10[i] > ma20[i]:
            if not in_bull:
                bull_start = i
                in_bull = True
        else:
            if in_bull:
                bull_align_periods.append((dates[bull_start], dates[i-1], closes[bull_start], closes[i-1]))
                in_bull = False
    if in_bull:
        bull_align_periods.append((dates[bull_start], dates[end_idx], closes[bull_start], closes[end_idx]))
    
    print(f"\n  多头排列持续期数: {len(bull_align_periods)}")
    for start_d, end_d, start_p, end_p in bull_align_periods:
        print(f"    {start_d} ~ {end_d}: 价格 {start_p:.4f} → {end_p:.4f} (+{(end_p/start_p-1)*100:.1f}%)")
    
    # 4. RSI近20日最高值首次突破70
    print("\n--- RSI近20日最高值首次突破70 ---")
    rsi_70_first = None
    for i in range(start_idx, end_idx + 1):
        if rsi_20d_max[i] is not None and rsi_20d_max[i] > 70:
            if rsi_70_first is None:
                rsi_70_first = i
                print(f"  RSI近20日最高首次>70: {dates[i]}  RSI_20d_max={rsi_20d_max[i]:.1f}  收盘价={closes[i]:.4f}")
    
    # 5. 通道7条件：RSI近20日曾>70 + 当前RSI 45-55 + 站MA20
    print("\n--- 通道7(强势回调)入场条件 ---")
    ch7_entries = []
    for i in range(start_idx, end_idx + 1):
        if (rsi_20d_max[i] is not None and rsi_20d_max[i] > 70 and
            rsi[i] is not None and 45 <= rsi[i] <= 55 and
            ma20[i] is not None and closes[i] > ma20[i]):
            ch7_entries.append((i, dates[i], closes[i], rsi[i], rsi_20d_max[i]))
    
    if ch7_entries:
        for idx, dt, price, rsi_v, rsi20 in ch7_entries:
            print(f"  ✅ 入场信号: {dt}  价格={price:.4f}  RSI={rsi_v:.1f}  RSI_20d_max={rsi20:.1f}")
    else:
        print(f"  ❌ 无入场信号")
    
    # 6. 通道8条件：MACD零轴上金叉(DIF>0 + 金叉 + 站MA20)
    print("\n--- 通道8(零轴金叉)入场条件 ---")
    ch8_entries = []
    for i in range(start_idx, end_idx + 1):
        if (statuses[i] == "金叉" and difs[i] is not None and difs[i] > 0 and
            ma20[i] is not None and closes[i] > ma20[i]):
            ch8_entries.append((i, dates[i], closes[i], difs[i], deas[i]))
    
    if ch8_entries:
        for idx, dt, price, dif, dea in ch8_entries:
            print(f"  ✅ 入场信号: {dt}  价格={price:.4f}  DIF={dif:.4f}  DEA={dea:.4f}")
    else:
        print(f"  ❌ 无入场信号")
    
    # 7. 当前策略(多头排列)入场
    print("\n--- 当前策略(多头排列=MA5>MA10>MA20)入场 ---")
    current_entries = []
    for i in range(start_idx, end_idx + 1):
        if ma5[i] and ma10[i] and ma20[i] and ma5[i] > ma10[i] > ma20[i]:
            if len(current_entries) == 0 or dates[i] > current_entries[-1][1]:
                # 前一个交易日不满足多头排列
                prev_ok = False
                if i > 0 and ma5[i-1] and ma10[i-1] and ma20[i-1]:
                    prev_ok = ma5[i-1] > ma10[i-1] > ma20[i-1]
                if not prev_ok:
                    current_entries.append((i, dates[i], closes[i], ma5[i], ma10[i], ma20[i]))
    
    for idx, dt, price, m5, m10, m20 in current_entries:
        print(f"  ✅ 入场信号: {dt}  价格={price:.4f}  MA5={m5:.4f}  MA10={m10:.4f}  MA20={m20:.4f}")
    
    # 8. 收益对比
    print(f"\n{'='*80}")
    print(f"  收益对比 (假设入场后持有到2026-08-25)")
    print(f"{'='*80}")
    
    # 找最后一天的收盘价
    last_price = closes[end_idx]
    last_date = dates[end_idx]
    print(f"  最终价格({last_date}): {last_price:.4f}")
    
    # 翻倍价格区间
    min_price = min(closes[start_idx:end_idx+1])
    max_price = max(closes[start_idx:end_idx+1])
    min_date = dates[closes.index(min_price, start_idx, end_idx+1)]
    max_date = dates[closes.index(max_price, start_idx, end_idx+1)]
    print(f"  最低价: {min_price:.4f} ({min_date})")
    print(f"  最高价: {max_price:.4f} ({max_date})")
    print(f"  区间涨幅: {(max_price/min_price - 1)*100:.1f}%")
    
    # 通道7最早入场
    if ch7_entries:
        first_ch7 = ch7_entries[0]
        gain_ch7 = (last_price / first_ch7[2] - 1) * 100
        peak_gain_ch7 = (max_price / first_ch7[2] - 1) * 100
        print(f"\n  通道7(强势回调)最早入场: {first_ch7[1]} @ {first_ch7[2]:.4f}")
        print(f"    持有至今收益: {gain_ch7:.1f}%")
        print(f"    区间最高收益: {peak_gain_ch7:.1f}%")
    
    # 通道8最早入场
    if ch8_entries:
        first_ch8 = ch8_entries[0]
        gain_ch8 = (last_price / first_ch8[2] - 1) * 100
        peak_gain_ch8 = (max_price / first_ch8[2] - 1) * 100
        print(f"\n  通道8(零轴金叉)最早入场: {first_ch8[1]} @ {first_ch8[2]:.4f}")
        print(f"    持有至今收益: {gain_ch8:.1f}%")
        print(f"    区间最高收益: {peak_gain_ch8:.1f}%")
    
    # 当前策略最早入场
    if current_entries:
        first_entry = current_entries[0]
        gain_current = (last_price / first_entry[2] - 1) * 100
        peak_gain_current = (max_price / first_entry[2] - 1) * 100
        print(f"\n  当前策略(多头排列)最早入场: {first_entry[1]} @ {first_entry[2]:.4f}")
        print(f"    持有至今收益: {gain_current:.1f}%")
        print(f"    区间最高收益: {peak_gain_current:.1f}%")
    
    # 底部价格日期
    print(f"\n  价格底部区域(前3个月最低点):")
    jan_mar_closes = [(dates[i], closes[i]) for i in range(start_idx, end_idx+1) if dates[i] < "2026-04-01"]
    jan_mar_closes.sort(key=lambda x: x[1])
    for dt, price in jan_mar_closes[:5]:
        print(f"    {dt}: {price:.4f}")
    
    return {
        'code': code,
        'name': name,
        'dates': dates,
        'closes': closes,
        'ma5': ma5, 'ma10': ma10, 'ma20': ma20,
        'rsi': rsi, 'difs': difs, 'deas': deas, 'bars': bars, 'statuses': statuses,
        'ma20_slope': ma20_slope, 'atrs': atrs, 'vol_ratios': vol_ratios,
        'rsi_20d_max': rsi_20d_max,
        'start_idx': start_idx, 'end_idx': end_idx,
        'ch7_entries': ch7_entries,
        'ch8_entries': ch8_entries,
        'current_entries': current_entries,
        'bull_align_first': bull_align_first,
        'ma20_turn': ma20_turn,
        'rsi_70_first': rsi_70_first,
        'macd_zero_golden': macd_zero_golden,
        'min_price': (min_price, min_date),
        'max_price': (max_price, max_date),
        'last_price': (last_price, last_date),
    }


def main():
    results = {}
    
    for code, name in [("159516", "半导体设备ETF"), ("588170", "科创半导体ETF")]:
        try:
            r = analyze_etf(code, name)
            if r:
                results[code] = r
        except Exception as e:
            print(f"\n❌ {code} 分析失败: {e}")
            import traceback
            traceback.print_exc()
    
    # 汇总对比
    print(f"\n\n{'='*80}")
    print(f"  汇总对比")
    print(f"{'='*80}")
    
    print(f"\n{'ETF':<25} {'最低价':>10} {'最高价':>10} {'涨幅':>8} {'MA20转正':<12} {'RSI70首破':<12} {'多头排列':<12} {'通道7入场':<12} {'通道8入场':<12} {'策略入场':<12}")
    print("-" * 130)
    
    for code, r in results.items():
        min_p, min_d = r['min_price']
        max_p, max_d = r['max_price']
        gain = (max_p/min_p - 1) * 100
        
        ma20_turn_str = r['dates'][r['ma20_turn']] if r['ma20_turn'] else "N/A"
        rsi70_str = r['dates'][r['rsi_70_first']] if r['rsi_70_first'] else "N/A"
        bull_align_str = r['dates'][r['bull_align_first']] if r['bull_align_first'] else "N/A"
        
        ch7_str = r['ch7_entries'][0][1] if r['ch7_entries'] else "N/A"
        ch8_str = r['ch8_entries'][0][1] if r['ch8_entries'] else "N/A"
        cur_str = r['current_entries'][0][1] if r['current_entries'] else "N/A"
        
        print(f"{r['name']:<25} {min_p:>10.4f} {max_p:>10.4f} {gain:>7.1f}% {ma20_turn_str:<12} {rsi70_str:<12} {bull_align_str:<12} {ch7_str:<12} {ch8_str:<12} {cur_str:<12}")
    
    # 收益对比
    print(f"\n{'='*80}")
    print(f"  入场收益对比")
    print(f"{'='*80}")
    print(f"{'ETF':<25} {'策略':<15} {'入场日期':<12} {'入场价':>10} {'当前价':>10} {'至今收益':>10} {'最高收益':>10}")
    print("-" * 95)
    
    for code, r in results.items():
        last_p, last_d = r['last_price']
        max_p, max_d = r['max_price']
        
        # 通道7
        if r['ch7_entries']:
            ch7 = r['ch7_entries'][0]
            gain = (last_p / ch7[2] - 1) * 100
            peak = (max_p / ch7[2] - 1) * 100
            print(f"{r['name']:<25} {'通道7(强势回调)':<15} {ch7[1]:<12} {ch7[2]:>10.4f} {last_p:>10.4f} {gain:>9.1f}% {peak:>9.1f}%")
        
        # 通道8
        if r['ch8_entries']:
            ch8 = r['ch8_entries'][0]
            gain = (last_p / ch8[2] - 1) * 100
            peak = (max_p / ch8[2] - 1) * 100
            print(f"{r['name']:<25} {'通道8(零轴金叉)':<15} {ch8[1]:<12} {ch8[2]:>10.4f} {last_p:>10.4f} {gain:>9.1f}% {peak:>9.1f}%")
        
        # 当前策略
        if r['current_entries']:
            cur = r['current_entries'][0]
            gain = (last_p / cur[2] - 1) * 100
            peak = (max_p / cur[2] - 1) * 100
            print(f"{r['name']:<25} {'当前(多头排列)':<15} {cur[1]:<12} {cur[2]:>10.4f} {last_p:>10.4f} {gain:>9.1f}% {peak:>9.1f}%")
    
    # 结论
    print(f"\n{'='*80}")
    print(f"  结论")
    print(f"{'='*80}")

if __name__ == '__main__':
    main()