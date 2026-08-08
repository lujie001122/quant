#!/usr/bin/env python3
"""回测2026年8月7日全天5分钟K线做T信号
验证RSI极端兜底通道: RSI<25直接买入做T3分, RSI>75直接卖出做T3分
数据源: 腾讯5分钟K线接口
"""

import sys
import urllib.request
import json

sys.path.insert(0, '/Users/lujie/Documents/code/quant')
from signal_generator import (
    t0_buy_score, t0_sell_score, calc_5min_indicators, ETFS, CODE_MAP
)

target_date = "20260807"  # 2026年8月7日
etf_codes = list(ETFS.keys())
print(f"回测日期: {target_date}")
print(f"标的: {etf_codes}")

all_results = {}

for code in etf_codes:
    name = ETFS[code]["name"]
    sid = CODE_MAP.get(code, "")
    if not sid:
        print(f"  {code}: 无代码映射，跳过")
        continue

    print(f"\n{'='*60}")
    print(f"【{code} {name}】sid={sid}")

    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sid},m5,,,48"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  获取失败: {e}")
        continue

    stock_data = raw.get("data", {})
    if sid not in stock_data:
        print(f"  sid {sid} 不在返回数据中")
        continue

    m5_raw = stock_data[sid].get("m5", [])
    if not m5_raw:
        print(f"  无5分钟K线数据")
        continue

    # 构建标准kline格式并过滤目标日期
    klines = []
    for item in m5_raw:
        if len(item) < 6:
            continue
        time_str = item[0]
        if not time_str.startswith(target_date):
            continue
        try:
            o, c, h, l = float(item[1]), float(item[2]), float(item[3]), float(item[4])
            v = float(item[5])
        except (ValueError, IndexError):
            continue
        if o == 0 or c == 0:
            continue
        klines.append({
            "time": time_str,
            "open": o, "close": c, "high": h, "low": l,
            "volume": v, "amount": 0,
        })

    print(f"  当日有效K线: {len(klines)}根")

    if len(klines) < 14:
        print(f"  K线不足14根，无法计算RSI(14)")
        continue

    signals = []

    # 从第14根开始，逐根用滑动窗口计算指标
    for i in range(14, len(klines)):
        window = klines[:i + 1]
        indicators = calc_5min_indicators(window)
        if indicators is None:
            continue

        rsi = indicators["rsi_5min"]
        macd_status = indicators["macd_5min_status"]
        vol_ratio = indicators["vol_ratio_5min"]

        buy_score = t0_buy_score(rsi, macd_status, vol_ratio)
        sell_score = t0_sell_score(rsi, macd_status, vol_ratio)

        if buy_score >= 2 or sell_score >= 2:
            signal_type = ""
            score = 0
            if buy_score >= 2:
                signal_type = "买入做T"
                score = buy_score
            elif sell_score >= 2:
                signal_type = "卖出做T"
                score = sell_score

            signals.append({
                "time": klines[i]["time"],
                "close": klines[i]["close"],
                "RSI_5min": rsi,
                "MACD_5min": macd_status,
                "vol_ratio_5min": vol_ratio,
                "buy_score": buy_score,
                "sell_score": sell_score,
                "signal": signal_type,
                "score": score
            })

    all_results[code] = {
        "name": name,
        "total_klines": len(klines),
        "signals": signals,
        "buy_signals": [s for s in signals if "买入" in s["signal"]],
        "sell_signals": [s for s in signals if "卖出" in s["signal"]],
    }

    print(f"  买入做T信号: {len(all_results[code]['buy_signals'])}个")
    print(f"  卖出做T信号: {len(all_results[code]['sell_signals'])}个")

    for s in signals:
        rsi_str = f"{s['RSI_5min']}" if s['RSI_5min'] is not None else "N/A"
        vol_str = f"{s['vol_ratio_5min']}" if s['vol_ratio_5min'] is not None else "N/A"
        extreme = ""
        if s['RSI_5min'] is not None:
            if s['RSI_5min'] < 25 and s['buy_score'] == 3:
                extreme = " ★极端RSI兜底!"
            elif s['RSI_5min'] > 75 and s['sell_score'] == 3:
                extreme = " ★极端RSI兜底!"
        print(f"    {s['time']} | price={s['close']:.3f} | RSI={rsi_str} | MACD={s['MACD_5min']} | vol={vol_str} | buy={s['buy_score']}/sell={s['sell_score']} → {s['signal']}({s['score']}分){extreme}")

# ======== 汇总 ========
print("\n" + "=" * 60)
print(f"【汇总】2026年8月7日 做T信号统计")
print("=" * 60)
total_buy = 0
total_sell = 0
for code, res in all_results.items():
    nb = len(res["buy_signals"])
    ns = len(res["sell_signals"])
    total_buy += nb
    total_sell += ns
    tag = f"买入T={nb}, 卖出T={ns}" if nb > 0 or ns > 0 else "无信号"
    print(f"  {code} {res['name']}: {tag}")
print(f"\n总计: 买入做T={total_buy}个, 卖出做T={total_sell}个")

# ======== 极端RSI兜底通道 ========
print("\n【极端RSI兜底通道触发明细】")
extreme_count = 0
for code, res in all_results.items():
    for s in res["signals"]:
        if s['RSI_5min'] is not None:
            if s['RSI_5min'] < 25 and s['buy_score'] == 3:
                extreme_count += 1
                print(f"  ★ {code} {s['time']}: RSI={s['RSI_5min']}, 买入做T(3分, RSI极端兜底)")
            elif s['RSI_5min'] > 75 and s['sell_score'] == 3:
                extreme_count += 1
                print(f"  ★ {code} {s['time']}: RSI={s['RSI_5min']}, 卖出做T(3分, RSI极端兜底)")

if extreme_count == 0:
    print(f"  当日无极端RSI触发（所有RSI均在25-75之间）")
else:
    print(f"\n  共{extreme_count}次极端RSI兜底触发")

# ======== 输出JSON ========
print("\n【JSON结果】")
output = {
    "date": "2026-08-07",
    "total_buy_signals": total_buy,
    "total_sell_signals": total_sell,
    "extreme_rsi_triggers": extreme_count,
    "details": {code: {
        "name": res["name"],
        "buy_count": len(res["buy_signals"]),
        "sell_count": len(res["sell_signals"]),
        "signals": res["signals"]
    } for code, res in all_results.items()}
}
print(json.dumps(output, indent=2, ensure_ascii=False, default=str))