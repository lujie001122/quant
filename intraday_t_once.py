"""
T+0 做T信号模块 v2.1
- 独立运行，从 portfolio.json 读取持仓
- 使用共享 signal_generator 的指标和评分函数确保算法一致
- 只在交易时段输出信号，非交易时段静默退出
"""
import json, urllib.request, math, os, sys
from datetime import datetime

# 添加脚本目录到 path，导入共享指标和评分函数
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signal_generator import calc_rsi_wilder, calc_macd, t0_buy_score, t0_sell_score, CODE_MAP

# ═══ 只在交易时段运行 ═══
now = datetime.now()
if now.hour < 10 or (now.hour == 11 and now.minute > 30) or now.hour == 12 or now.hour >= 15:
    exit()

# ═══ 从 portfolio.json 读取持仓 ═══
pf_path = os.path.expanduser("~/hermes/scripts/portfolio.json")
positions = {}
if os.path.exists(pf_path):
    try:
        with open(pf_path) as f:
            pf = json.load(f)
        for code in CODE_MAP:
            pos_data = pf.get("positions", {}).get(code, {})
            shares = pos_data.get("shares", 0)
            if shares > 0:
                positions[code] = {
                    "shares": shares,
                    "cost": pos_data.get("avg_cost", 0),
                }
    except Exception as e:
        print(f"[WARN] 读取portfolio.json失败: {e}")

if not positions:
    exit()  # 无持仓，静默退出


# ═══ 主逻辑 ═══
signals = []

for code, sid in CODE_MAP.items():
    if code not in positions:
        continue
    pos = positions[code]
    try:
        # 获取5分钟K线
        url = (f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
               f'CN_MarketData.getKLineData?symbol={sid}&scale=5&ma=no&datalen=100')
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if len(data) < 50:
            continue

        closes = [float(d['close']) for d in data]
        cur = closes[-1]

        # 使用共享指标模块计算 RSI 和 MACD
        rsi = calc_rsi_wilder(closes, 14)
        dif, dea, macd_bar, macd_status = calc_macd(closes)
        if rsi is None or macd_status is None:
            continue

        pct = (cur / pos['cost'] - 1) * 100
        t_shares = int(pos['shares'] * 20 / 100 / 100) * 100  # 20%取整到100股

        sig = None
        # 用共享评分函数判断做T（确保与signal_generator.py完全一致）
        buy_score = t0_buy_score(rsi, macd_status, None, None)
        sell_score = t0_sell_score(rsi, None, macd_status, None, None)

        # T入: 评分≥2 + 价低于成本×1.02
        if buy_score >= 2 and pos['cost'] > 0 and cur < pos['cost'] * 1.02:
            sig = (f"T入\n  {t_shares}股 @{cur:.3f}\n"
                   f"  ¥{t_shares * cur / 10000:.1f}万\n"
                   f"  RSI{rsi:.1f} {macd_status} 评分{buy_score}\n  成本{pos['cost']:.3f}")
        # T出: 评分≥2 + (止盈或避险)
        elif sell_score >= 2:
            profit_ok = pos['cost'] > 0 and cur > pos['cost']
            hedge_ok = pos['cost'] > 0 and cur < pos['cost'] and macd_status in ["死叉", "绿柱放大"]
            if profit_ok or hedge_ok:
                label = "避险" if hedge_ok else ""
                sig = (f"T出{label}\n  {t_shares}股 @{cur:.3f}\n"
                       f"  ¥{t_shares * cur / 10000:.1f}万\n"
                       f"  RSI{rsi:.1f} {macd_status} 评分{sell_score}\n  成本{pos['cost']:.3f}")

        if sig:
            signals.append(f"{code} {sig}")
    except Exception as e:
        print(f"[WARN] {code} 做T信号获取失败: {e}")

if signals:
    print(f"🔄 T信号 | {now.strftime('%H:%M')}")
    for s in signals:
        print(f"  📡 {s}")
