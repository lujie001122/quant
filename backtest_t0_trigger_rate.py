#!/usr/bin/env python3
"""
做T信号回测：触发率 + 成交率分析
基于新浪5分钟K线接口（每标的100根K线覆盖约3个完整交易日）
回测 signal_generator 中的 t0_buy_score / t0_sell_score 逻辑：
- RSI < 45 买入 / > 55 卖出 + 3项共振 ≥ 2分
- RSI > 75 极端卖出 / < 25 极端买入
- MA20 斜率趋势过滤
- 11点后才运行

输出：
1. 每天触发多少次做T信号（买入/卖出）
2. 配对挂单成交率
3. 优化建议
"""
import urllib.request, json, sys, os
from datetime import datetime, timedelta
from collections import defaultdict

# 复制 signal_generator.py 的核心函数
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
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_macd(closes, fast=5, slow=34, sig=5):
    """做T专用MACD参数 (5,34,5)"""
    if len(closes) < slow + sig:
        return None, None, None, "数据不足"
    
    def ema(data, period):
        if len(data) < 2:
            return []
        k = 2 / (period + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result
    
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
    dea = ema(dif, sig)
    
    macd_bar = [2 * (dif[i] - dea[i]) for i in range(len(dif))]
    
    if len(macd_bar) < 3:
        return dif[-1], dea[-1], macd_bar[-1], "数据不足"
    
    bar_now, bar_prev = macd_bar[-1], macd_bar[-2]
    dif_now, dif_prev = dif[-1], dif[-2]
    dea_now, dea_prev = dea[-1], dea[-2]
    
    if dif_now > dea_now and dif_prev <= dea_prev:
        status = "金叉"
    elif dif_now < dea_now and dif_prev >= dea_prev:
        status = "死叉"
    elif bar_now > 0:
        if bar_now > bar_prev:
            status = "红柱放大"
        else:
            status = "红柱缩短"
    else:
        if bar_now < bar_prev:
            status = "绿柱放大"
        else:
            status = "绿柱缩短"
    
    return dif[-1], dea[-1], macd_bar[-1], status


def calc_atr(klines, period=14):
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        high = klines[i]["high"]
        low = klines[i]["low"]
        prev_close = klines[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


def t0_buy_score(rsi_5min, macd_5min_status, vol_ratio_5min):
    if rsi_5min is None:
        return 0
    if rsi_5min > 75:
        return 0
    if rsi_5min < 25:
        return 3
    if rsi_5min >= 45:
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
    if rsi_5min <= 55:
        return 0
    if macd_5min_status in ["金叉", "红柱放大"]:
        return 0
    score = 1
    if macd_5min_status in ["死叉", "绿柱放大"]:
        score += 1
    if vol_ratio_5min is not None and vol_ratio_5min > 1.2:
        score += 1
    return score


def fetch_sina_5min(sid, datalen=1200):
    """拉取新浪5分钟K线"""
    url = f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sid}&scale=5&ma=no&datalen={datalen}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        klines = []
        for d in data:
            try:
                klines.append({
                    "time": d['day'],
                    "open": float(d['open']),
                    "close": float(d['close']),
                    "high": float(d['high']),
                    "low": float(d['low']),
                    "volume": float(d['volume']),
                })
            except (ValueError, KeyError):
                continue
        return klines
    except Exception as e:
        print(f"  [ERR] {sid}: {e}")
        return []


# 标的映射
CODES = {
    "159516": "sz159516", "515880": "sh515880", "588170": "sh588170",
    "159532": "sz159532", "515050": "sh515050", "159611": "sz159611",
    "512170": "sh512170",
}
NAMES = {
    "159516": "半导体设备ETF", "515880": "通信ETF", "588170": "科创半导体ETF",
    "159532": "中证2000ETF", "515050": "通信ETF", "159611": "电力ETF",
    "512170": "医疗ETF",
}


def analyze_day(klines_day, day):
    """
    对一个交易日的K线进行做T信号分析
    模拟 signal_generator.py 的做T评分逻辑
    11:00之后每根K线检查一次
    """
    results = []
    
    # MACD(5,34,5)需要至少39根K线, RSI(14)需要15根, MA20斜率需要25根
    min_bars = 39
    for i in range(min_bars, len(klines_day)):
        window = klines_day[:i + 1]
        last_k = window[-1]
        t = last_k["time"]
        
        # 11:00 之前跳过
        # 时间格式: "2026-08-07 09:35:00"
        hour = int(t.split(" ")[1].split(":")[0]) if " " in t else 0
        if hour < 11:
            continue
        
        closes = [k["close"] for k in window]
        
        # RSI (14)
        rsi = calc_rsi_wilder(closes, 14)
        
        # MACD (5,34,5)
        _, _, _, macd_status = calc_macd(closes, 5, 34, 5)
        
        # 量比
        volumes = [k["volume"] for k in window]
        vol_ratio = None
        if len(volumes) >= 21:
            avg_vol = sum(volumes[-21:-1]) / 20
            if avg_vol > 0:
                vol_ratio = round(volumes[-1] / avg_vol, 2)
        
        # MA20 斜率
        ma20_slope = None
        if len(closes) >= 25:
            ma20_now = sum(closes[-20:]) / 20
            ma20_5ago = sum(closes[-25:-5]) / 20
            if ma20_5ago > 0:
                ma20_slope = round((ma20_now - ma20_5ago) / ma20_5ago, 6)
        
        # ATR (14) - 用于配对挂单
        atr = calc_atr(window, 14)
        
        buy_score = t0_buy_score(rsi, macd_status, vol_ratio)
        sell_score = t0_sell_score(rsi, macd_status, vol_ratio)
        
        # MA20 趋势过滤（与 signal_generator.py 对齐: 斜率>0.3%禁T卖, 斜率<-0.3%禁T买）
        if ma20_slope is not None:
            if ma20_slope > 0.003:
                sell_score = 0
            elif ma20_slope < -0.003:
                buy_score = 0
        
        cur_price = last_k["close"]
        
        if buy_score >= 2 or sell_score >= 2:
            results.append({
                "time": t,
                "code": "",
                "price": cur_price,
                "buy_score": buy_score,
                "sell_score": sell_score,
                "rsi": round(rsi, 1) if rsi else None,
                "macd_status": macd_status,
                "vol_ratio": vol_ratio,
                "ma20_slope": ma20_slope,
                "atr_5min": round(atr, 4) if atr else None,
                "pair_price": None,
                "pair_fillable": False,
            })
    
    return results


def check_pair_fill(signals, klines_day):
    """
    检查配对挂单能否在当天成交
    ATR × 1.1 价差的配对挂单，检查后续K线是否触达
    """
    for sig in signals:
        if sig["atr_5min"] is None:
            continue
        
        # 配对挂单价
        if sig["buy_score"] >= 2:
            # 做T买入后，配对卖出挂单 = price + ATR × 1.1
            pair_price = sig["price"] + sig["atr_5min"] * 1.1
        elif sig["sell_score"] >= 2:
            # 做T卖出后，配对买入挂单 = price - ATR × 1.1
            pair_price = sig["price"] - sig["atr_5min"] * 1.1
        else:
            continue
        
        sig["pair_price"] = round(pair_price, 4)
        
        # 检查信号之后的K线是否触达配对价
        sig_time = sig["time"]
        filled = False
        for k in klines_day:
            if k["time"] <= sig_time:
                continue
            if k["high"] >= pair_price and k["low"] <= pair_price:
                filled = True
                break
        
        sig["pair_fillable"] = filled


def main():
    print("=" * 80)
    print("做T信号回测: 触发率 + 成交率分析")
    print("回测逻辑: signal_generator.py t0_buy_score / t0_sell_score")
    print(f"数据源: 新浪5分钟K线 (datalen=800, 覆盖约18个交易日)")
    print("=" * 80)
    
    # 拉取所有标的的5分钟K线
    all_klines = {}
    for code, sid in CODES.items():
        print(f"\n拉取 {code} ({NAMES.get(code, '')}) ...", end=" ", flush=True)
        klines = fetch_sina_5min(sid, 800)
        all_klines[code] = klines
        if klines:
            dates = sorted(set(k["time"].split(" ")[0] for k in klines))
            print(f"{len(klines)}根K线, 日期: {dates[0]} ~ {dates[-1]}")
        else:
            print("无数据")
    
    # 确定共同日期范围（取所有标的都有数据的日期交集，最近3个完整交易日）
    all_dates = set()
    for code, klines in all_klines.items():
        if klines:
            dates = set(k["time"].split(" ")[0] for k in klines)
            if all_dates:
                all_dates &= dates
            else:
                all_dates = dates
    
    sorted_dates = sorted(all_dates)
    print(f"\n共同交易日: {sorted_dates}")
    
    # 取最近5个完整的交易日 + 今天(如有)
    today_str = datetime.now().strftime("%Y-%m-%d")
    completed_days = [d for d in sorted_dates if d < today_str]
    
    target_days = completed_days[-5:] if len(completed_days) >= 5 else completed_days
    if today_str in sorted_dates:
        target_days.append(today_str)
    target_days = target_days[-5:]
    
    print(f"回测日期: {target_days}\n")
    
    # ════════════════════════════════════════════════════
    # 逐日逐标回测
    # ════════════════════════════════════════════════════
    
    day_summary = {}
    
    for day in target_days:
        print(f"    {'=' * 60}")
        print(f"    {day} {'(今天, 未完成)' if day == today_str else '(已完成)'}")
        print(f"    {'=' * 60}")
        
        day_signals = []
        
        for code, sid in CODES.items():
            klines = all_klines.get(code, [])
            if not klines:
                continue
            
            # 筛选当天的K线
            klines_day = [k for k in klines if k["time"].startswith(day)]
            if len(klines_day) < 48:
                continue
            
            # 执行做T信号分析
            signals = analyze_day(klines_day, day)
            
            # 检查配对挂单成交
            check_pair_fill(signals, klines_day)
            
            for s in signals:
                s["code"] = code
                s["name"] = NAMES.get(code, "")
            
            if signals:
                day_signals.extend(signals)
                
                for s in signals:
                    direction = "🔴卖出" if s["sell_score"] >= 2 else "🟢买入"
                    pair_info = ""
                    if s["pair_price"]:
                        pair_info = f"配对@{s['pair_price']:.4f} ({'✅可成交' if s['pair_fillable'] else '❌未触达'})"
                    
                    slope_str = f"斜率{s['ma20_slope']:.4f}" if s['ma20_slope'] is not None else "N/A"
                    print(f"      {s['time']} {direction} {code} {NAMES.get(code,'')[:6]} "
                          f"@{s['price']:.4f} RSI={s['rsi']} MACD={s['macd_status']} "
                          f"量比={s['vol_ratio']} MA20_{slope_str} "
                          f"[buy_score={s['buy_score']} sell_score={s['sell_score']}] "
                          f"{pair_info}")
        
        # 按方向分类统计
        buy_count = sum(1 for s in day_signals if s["buy_score"] >= 2)
        sell_count = sum(1 for s in day_signals if s["sell_score"] >= 2)
        has_pair = sum(1 for s in day_signals if s["pair_price"] is not None)
        pair_filled = sum(1 for s in day_signals if s["pair_fillable"])
        total = len(day_signals)
        
        day_summary[day] = {
            "total": total,
            "buy": buy_count,
            "sell": sell_count,
            "has_pair": has_pair,
            "pair_filled": pair_filled,
            "by_code": defaultdict(lambda: {"buy": 0, "sell": 0, "pair": 0, "filled": 0}),
        }
        
        for s in day_signals:
            code = s["code"]
            if s["buy_score"] >= 2:
                day_summary[day]["by_code"][code]["buy"] += 1
            elif s["sell_score"] >= 2:
                day_summary[day]["by_code"][code]["sell"] += 1
            if s["pair_price"]:
                day_summary[day]["by_code"][code]["pair"] += 1
            if s["pair_fillable"]:
                day_summary[day]["by_code"][code]["filled"] += 1
        
        print(f"\n    📊 {day} 汇总:")
        print(f"       总信号: {total} (买入: {buy_count}, 卖出: {sell_count})")
        print(f"       可配对挂单: {has_pair}")
        print(f"       配对可成交: {pair_filled}")
        if has_pair > 0:
            print(f"       配对成交率: {pair_filled}/{has_pair} = {pair_filled/has_pair*100:.1f}%")
        
        # 按标的统计
        print(f"       按标的:")
        for code in sorted(CODES.keys()):
            d = day_summary[day]["by_code"][code]
            if d["buy"] + d["sell"] > 0:
                fill_rate = f"{d['filled']}/{d['pair']}成交" if d['pair'] > 0 else "无配对"
                print(f"         {code} {NAMES.get(code,'')[:8]}: 买{d['buy']}卖{d['sell']} {fill_rate}")
    
    # ════════════════════════════════════════════════════
    # 整体汇总
    # ════════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("📊 整体回测汇总")
    print(f"{'=' * 80}")
    
    total_signals = 0
    total_buy = 0
    total_sell = 0
    total_pair = 0
    total_filled = 0
    code_total = defaultdict(lambda: {"buy": 0, "sell": 0, "pair": 0, "filled": 0, "days": set()})
    
    for day, summary in day_summary.items():
        total_signals += summary["total"]
        total_buy += summary["buy"]
        total_sell += summary["sell"]
        total_pair += summary["has_pair"]
        total_filled += summary["pair_filled"]
        for code, d in summary["by_code"].items():
            code_total[code]["buy"] += d["buy"]
            code_total[code]["sell"] += d["sell"]
            code_total[code]["pair"] += d["pair"]
            code_total[code]["filled"] += d["filled"]
            code_total[code]["days"].add(day)
    
    print(f"\n交易日数: {len(target_days)}")
    print(f"总信号数: {total_signals} (买入: {total_buy}, 卖出: {total_sell})")
    print(f"日均信号: {total_signals / len(target_days):.1f}")
    print(f"总可配对: {total_pair}")
    print(f"总成交: {total_filled}")
    if total_pair > 0:
        print(f"配对成交率: {total_filled}/{total_pair} = {total_filled / total_pair * 100:.1f}%")
    
    print(f"\n按标的汇总:")
    print(f"{'代码':<8} {'名称':<14} {'天数':<6} {'买入':<6} {'卖出':<6} {'配对':<6} {'成交':<6} {'成交率':<8}")
    print("-" * 72)
    for code in sorted(CODES.keys()):
        d = code_total[code]
        name = NAMES.get(code, "")
        days_count = len(d["days"])
        rate = f"{d['filled']/d['pair']*100:.0f}%" if d["pair"] > 0 else "N/A"
        print(f"{code:<8} {name:<14} {days_count:<6} {d['buy']:<6} {d['sell']:<6} {d['pair']:<6} {d['filled']:<6} {rate:<8}")
    
    # ════════════════════════════════════════════════════
    # RSI阈值分析 — 检查RSI分布
    # ════════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("📊 RSI阈值敏感性分析")
    print(f"{'=' * 80}")
    
    # 对每个5分钟K线快照计算RSI，看阈值触发频率
    rsi_buckets_buy = defaultdict(int)
    rsi_buckets_sell = defaultdict(int)
    all_rsi_points = []
    
    for code, sid in CODES.items():
        klines = all_klines.get(code, [])
        if not klines:
            continue
        for day in target_days:
            klines_day = [k for k in klines if k["time"].startswith(day)]
            if len(klines_day) < 39:
                continue
            for i in range(39, len(klines_day)):
                k = klines_day[i]
                t = k["time"]
                hour = int(t.split(" ")[1].split(":")[0]) if " " in t else 0
                if hour < 11:
                    continue
                window = klines_day[:i + 1]
                closes = [k["close"] for k in window]
                rsi = calc_rsi_wilder(closes, 14)
                if rsi is None:
                    continue
                all_rsi_points.append((code, day, t, rsi))
                if rsi < 25:
                    rsi_buckets_buy["RSI<25(极端)"] += 1
                elif rsi < 35:
                    rsi_buckets_buy["RSI<35"] += 1
                elif rsi < 45:
                    rsi_buckets_buy["RSI<45"] += 1
                elif rsi > 75:
                    rsi_buckets_sell["RSI>75(极端)"] += 1
                elif rsi > 65:
                    rsi_buckets_sell["RSI>65"] += 1
                elif rsi > 55:
                    rsi_buckets_sell["RSI>55"] += 1
    
    total_snapshots = len(all_rsi_points)
    print(f"11:00后的5分钟K线快照总数: {total_snapshots} (7只标的 × {len(target_days)}天)")
    
    print(f"\n买入侧 RSI 分布 (触发买入分数 ≥1 的RSI区间):")
    for bucket in ["RSI<25(极端)", "RSI<35", "RSI<45"]:
        count = rsi_buckets_buy[bucket]
        print(f"  {bucket}: {count}次 ({count/total_snapshots*100:.1f}%)")
    
    print(f"\n卖出侧 RSI 分布 (触发卖出分数 ≥1 的RSI区间):")
    for bucket in ["RSI>55", "RSI>65", "RSI>75(极端)"]:
        count = rsi_buckets_sell[bucket]
        print(f"  {bucket}: {count}次 ({count/total_snapshots*100:.1f}%)")
    
    # ════════════════════════════════════════════════════
    # MA20斜率分析
    # ════════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("📊 MA20斜率趋势过滤分析")
    print(f"{'=' * 80}")
    
    ma20_stats = {"bull": 0, "bear": 0, "neutral": 0, "bull_blocked": 0, "bear_blocked": 0}
    
    for code, sid in CODES.items():
        klines = all_klines.get(code, [])
        if not klines:
            continue
        for day in target_days:
            klines_day = [k for k in klines if k["time"].startswith(day)]
            if len(klines_day) < 39:
                continue
            for i in range(39, len(klines_day)):
                k = klines_day[i]
                t = k["time"]
                hour = int(t.split(" ")[1].split(":")[0]) if " " in t else 0
                if hour < 11:
                    continue
                window = klines_day[:i + 1]
                closes = [k["close"] for k in window]
                if len(closes) < 25:
                    continue
                ma20_now = sum(closes[-20:]) / 20
                ma20_5ago = sum(closes[-25:-5]) / 20
                if ma20_5ago <= 0:
                    continue
                slope = (ma20_now - ma20_5ago) / ma20_5ago
                
                # 检查卖出信号是否被MA20斜率拦截
                rsi = calc_rsi_wilder(closes, 14)
                _, _, _, macd_status = calc_macd(closes, 5, 34, 5)
                volumes = [k["volume"] for k in window]
                vol_ratio = None
                if len(volumes) >= 21:
                    avg_vol = sum(volumes[-21:-1]) / 20
                    if avg_vol > 0:
                        vol_ratio = round(volumes[-1] / avg_vol, 2)
                
                sell_score = t0_sell_score(rsi, macd_status, vol_ratio)
                buy_score = t0_buy_score(rsi, macd_status, vol_ratio)
                
                if slope > 0.003:
                    ma20_stats["bull"] += 1
                    if sell_score >= 2:
                        ma20_stats["bull_blocked"] += 1
                elif slope < -0.003:
                    ma20_stats["bear"] += 1
                    if buy_score >= 2:
                        ma20_stats["bear_blocked"] += 1
                else:
                    ma20_stats["neutral"] += 1
    
    print(f"MA20斜率分布 (11:00后):")
    print(f"  上升趋势(斜率>0.3%):  {ma20_stats['bull']}次, 其中阻拦T卖出: {ma20_stats['bull_blocked']}次")
    print(f"  下降趋势(斜率<-0.3%): {ma20_stats['bear']}次, 其中阻拦T买入: {ma20_stats['bear_blocked']}次")
    print(f"  震荡(±0.3%以内):     {ma20_stats['neutral']}次")
    
    # ════════════════════════════════════════════════════
    # 优化建议
    # ════════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("💡 优化建议")
    print(f"{'=' * 80}")
    
    # 1. RSI阈值分析
    print(f"\n1. RSI 阈值评估:")
    rsi_buy_range = rsi_buckets_buy["RSI<45"] - rsi_buckets_buy["RSI<35"]
    rsi_sell_range = rsi_buckets_sell["RSI>55"] - rsi_buckets_sell["RSI>65"]
    print(f"   RSI在35-45之间的买入候选: {rsi_buy_range}次")
    print(f"   RSI在55-65之间的卖出候选: {rsi_sell_range}次")
    
    if total_pair > 0 and total_filled / total_pair < 0.5:
        print(f"   ⚠️ 配对成交率低({total_filled}/{total_pair}={total_filled/total_pair*100:.1f}%)，")
        print(f"   建议: 降低ATR乘数(1.1→0.8)使挂单价差更小，或只在趋势中做T")
    elif total_pair > 0:
        print(f"   配对成交率尚可，ATR×1.1乘数合理")
    
    # 2. MA20斜率过滤
    bull_pct = ma20_stats['bull'] / (ma20_stats['bull'] + ma20_stats['bear'] + ma20_stats['neutral']) * 100 if (ma20_stats['bull'] + ma20_stats['bear'] + ma20_stats['neutral']) > 0 else 0
    print(f"\n2. MA20 斜率过滤评估:")
    print(f"   上升趋势占比: {bull_pct:.1f}%")
    if ma20_stats['bull_blocked'] > 0:
        print(f"   被拦截的T卖出信号: {ma20_stats['bull_blocked']}次")
    if ma20_stats['bear_blocked'] > 0:
        print(f"   被拦截的T买入信号: {ma20_stats['bear_blocked']}次")
    
    # 3. 信号触发频率
    avg_per_code_per_day = total_signals / len(CODES) / len(target_days)
    print(f"\n3. 信号触发频率:")
    print(f"   每标的每天平均: {avg_per_code_per_day:.2f}次")
    if avg_per_code_per_day < 0.5:
        print(f"   ⚠️ 信号触发过于稀少，RSI阈值(45/55)可能偏严")
        print(f"   建议: 放宽RSI阈值至(40/60)或降低共振要求(≥2→≥1)")
    elif avg_per_code_per_day > 3:
        print(f"   ⚠️ 信号触发过于频繁，可能增加交易成本")
        print(f"   建议: 加严RSI阈值或提高共振要求")

    print(f"\n回测完成。")


if __name__ == "__main__":
    main()