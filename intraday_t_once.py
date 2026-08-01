"""
T+0 做T信号模块 v2.0
- 独立运行，从 portfolio.json 读取持仓
- 使用共享 indicators.py 确保算法一致
- 只在交易时段输出信号，非交易时段静默退出
"""
import json, urllib.request, math, os, sys
from datetime import datetime

# 添加脚本目录到 path，导入共享指标（统一用 signal_generator 的版本）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signal_generator import calc_rsi_wilder, calc_macd

# ═══ 只在交易时段运行 ═══
now = datetime.now()
if now.hour < 10 or (now.hour == 11 and now.minute > 30) or now.hour == 12 or now.hour >= 15:
    exit()

# ═══ 标的映射 ═══
CODES = {
    '159516': 'sz159516', '515880': 'sh515880', '588170': 'sh588170',
    '159532': 'sz159532', '515050': 'sh515050',
}

# ═══ 从 portfolio.json 读取持仓 ═══
pf_path = os.path.expanduser("~/hermes/scripts/portfolio.json")
positions = {}
if os.path.exists(pf_path):
    try:
        with open(pf_path) as f:
            pf = json.load(f)
        for code in CODES:
            pos_data = pf.get("positions", {}).get(code, {})
            shares = pos_data.get("shares", 0)
            if shares > 0:
                positions[code] = {
                    "shares": shares,
                    "cost": pos_data.get("avg_cost", 0),
                }
    except Exception:
        pass

if not positions:
    exit()  # 无持仓，静默退出

# ═══ T+0 评分函数（与 signal_generator.py 完全一致） ═══
def t0_buy_score(rsi, macd_status, vol_ratio_5min, vol_ratio_120):
    """做T买入评分: RSI超卖+MACD多头确认+量能温和
    RSI<40必须满足, 死叉/绿柱放大时禁止
    """
    if rsi is None or rsi >= 40:
        return 0
    if macd_status in ["死叉", "绿柱放大"]:
        return 0
    vol = vol_ratio_5min if vol_ratio_5min is not None else vol_ratio_120
    score = 1  # RSI<40已满足
    if macd_status in ["金叉", "红柱放大"]: score += 1
    if vol is not None and vol < 1.5: score += 1
    return score


def t0_sell_score(rsi, prev_rsi, macd_status, vol_ratio_5min, vol_ratio_120):
    """做T卖出评分: RSI偏高+MACD趋势结束+量能放大
    RSI>50必须满足, 金叉/红柱放大时禁止
    """
    if rsi is None or rsi <= 50:
        return 0
    if macd_status in ["金叉", "红柱放大"]:
        return 0
    vol = vol_ratio_5min if vol_ratio_5min is not None else vol_ratio_120
    score = 1  # RSI>50已满足
    if rsi > 65: score += 1
    if macd_status in ["红柱缩短", "死叉", "绿柱放大"]: score += 1
    if vol is not None and vol > 1.2: score += 1
    return score


# ═══ 主逻辑 ═══
signals = []

for code, sid in CODES.items():
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
        # 用评分制判断做T（与signal_generator.py一致）
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
    except Exception:
        pass

if signals:
    print(f"🔄 T信号 | {now.strftime('%H:%M')}")
    for s in signals:
        print(f"  📡 {s}")
