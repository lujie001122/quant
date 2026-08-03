"""
T+0 做T信号模块 v3.0
- 独立运行，从 portfolio.json 读取持仓
- 使用共享 signal_generator 的指标和评分函数确保算法一致
- 只在交易时段输出信号，非交易时段静默退出
P0: 5分钟K线量比 + RSI用已完成K线
P1: RSI软评分 + 去掉成本价过滤
P2: 仓位与评分挂钩
P3: 时间过滤 + 做T冷却期
"""
import json, urllib.request, math, os, sys, time
from datetime import datetime

# 添加脚本目录到 path，导入共享指标和评分函数
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signal_generator import calc_rsi_wilder, calc_macd, t0_buy_score, t0_sell_score, CODE_MAP

# ═══ 冷却期状态文件 ═══
COOLDOWN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".t0_cooldown.json")


def load_cooldown():
    """加载冷却期状态: {code: last_trigger_timestamp}"""
    if not os.path.exists(COOLDOWN_FILE):
        return {}
    try:
        with open(COOLDOWN_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cooldown(data):
    """保存冷却期状态"""
    try:
        with open(COOLDOWN_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def is_in_cooldown(code, cooldown_data, now_ts, cooldown_minutes=30):
    """检查标的是否在冷却期内"""
    last_ts = cooldown_data.get(code, 0)
    return (now_ts - last_ts) < cooldown_minutes * 60


def record_cooldown(code, cooldown_data, now_ts):
    """记录触发时间"""
    cooldown_data[code] = now_ts


# ═══ 时间过滤 ═══
def check_time_filter(now):
    """
    返回 (allow_t_in, allow_t_out, reason)
    - 9:30-9:45 跳过
    - 11:25-13:05 跳过
    - 14:45-15:00 只允许T出
    """
    t = now.hour * 60 + now.minute  # 分钟数

    # 9:30-9:45 跳过（开盘集合竞价后波动大）
    if 570 <= t < 585:  # 9:30=570, 9:45=585
        return False, False, "开盘波动期(9:30-9:45)"

    # 11:25-13:05 跳过（午休前后流动性差）
    if 685 <= t < 785:  # 11:25=685, 13:05=785
        return False, False, "午休低流动性期(11:25-13:05)"

    # 14:45-15:00 只允许T出（尾盘只出不入）
    if 885 <= t < 900:  # 14:45=885, 15:00=900
        return False, True, "尾盘只出(14:45-15:00)"

    return True, True, ""


# ═══ RSI软评分 ═══
def rsi_soft_score_buy(rsi):
    """T入 RSI软评分: <25→2分, 25-35→1.5分, 35-45→1分, >45→0分"""
    if rsi is None:
        return 0
    if rsi < 25:
        return 2.0
    elif rsi < 35:
        return 1.5
    elif rsi < 45:
        return 1.0
    else:
        return 0


def rsi_soft_score_sell(rsi):
    """T出 RSI软评分: >70→2分, 60-70→1.5分, 50-60→1分, <50→0分"""
    if rsi is None:
        return 0
    if rsi > 70:
        return 2.0
    elif rsi > 60:
        return 1.5
    elif rsi > 50:
        return 1.0
    else:
        return 0


# ═══ 仓位与评分挂钩 ═══
def score_to_position_pct(score):
    """2分→20%, 2.5分→25%, 3分→30%"""
    if score < 2.0:
        return 0
    elif score < 2.5:
        return 20
    elif score < 3.0:
        return 25
    else:
        return 30


# ═══ 只在交易时段运行 ═══
now = datetime.now()
if now.hour < 10 or (now.hour == 11 and now.minute > 30) or now.hour == 12 or now.hour >= 15:
    exit()

# ═══ 时间过滤检查 ═══
allow_t_in, allow_t_out, time_reason = check_time_filter(now)
if not allow_t_in and not allow_t_out:
    # 完全禁止时段，静默退出
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
now_ts = time.time()
cooldown_data = load_cooldown()

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

        # P0: 排除最后一根未完成K线，用已完成K线计算RSI
        completed = data[:-1]  # 去掉最后一根（当前正在形成的K线）
        cur = float(data[-1]['close'])  # 当前价格仅用于价格条件判断

        closes = [float(d['close']) for d in completed]
        volumes = [float(d['volume']) for d in completed]

        # 使用共享指标模块计算 RSI 和 MACD（基于已完成K线）
        rsi = calc_rsi_wilder(closes, 14)
        dif, dea, macd_bar, macd_status = calc_macd(closes)
        if rsi is None or macd_status is None:
            continue

        # P0: 用5分钟K线的volume字段计算量比
        vol_ratio_5min = None
        if len(volumes) >= 21:
            avg_vol = sum(volumes[-21:-1]) / 20  # 前20根5分钟K线均量
            if avg_vol > 0:
                vol_ratio_5min = round(volumes[-1] / avg_vol, 2)

        # 用共享评分函数判断做T（传入量比）
        buy_score = t0_buy_score(rsi, macd_status, vol_ratio_5min, None)
        sell_score = t0_sell_score(rsi, None, macd_status, vol_ratio_5min, None)

        # P1: RSI软评分叠加
        rsi_buy = rsi_soft_score_buy(rsi)
        rsi_sell = rsi_soft_score_sell(rsi)

        # 综合评分 = MACD评分 + RSI软评分
        # MACD评分: buy_score中RSI占1分，减去后得MACD贡献
        macd_buy = buy_score - 1 if buy_score > 0 else 0  # MACD贡献(0-2分)
        macd_sell = sell_score - 1 if sell_score > 0 else 0  # MACD贡献(0-2分)

        total_buy_score = rsi_buy + macd_buy
        total_sell_score = rsi_sell + macd_sell

        # P2: 仓位与评分挂钩
        buy_pct = score_to_position_pct(total_buy_score)
        sell_pct = score_to_position_pct(total_sell_score)

        sig = None

        # T入: 综合评分≥2分 + 时间允许 + 不在冷却期
        if buy_pct > 0 and allow_t_in and not is_in_cooldown(code, cooldown_data, now_ts):
            t_shares = int(pos['shares'] * buy_pct / 100 / 100) * 100  # 取整到100股
            if t_shares >= 100:
                record_cooldown(code, cooldown_data, now_ts)
                sig = (f"T入\n  {t_shares}股 @{cur:.3f} ({buy_pct}%仓位)\n"
                       f"  ¥{t_shares * cur / 10000:.1f}万\n"
                       f"  RSI{rsi:.1f}(软评分{rsi_buy}) {macd_status}(评分{macd_buy}) 综合{total_buy_score}\n"
                       f"  量比{vol_ratio_5min or 'N/A'}")

        # T出: 综合评分≥2分 + 时间允许 + 不在冷却期
        if sig is None and sell_pct > 0 and allow_t_out and not is_in_cooldown(code, cooldown_data, now_ts):
            t_shares = int(pos['shares'] * sell_pct / 100 / 100) * 100  # 取整到100股
            if t_shares >= 100:
                # P1: 纯市场结构条件（去掉成本价过滤）
                # 止盈: MACD趋势结束
                profit_ok = macd_status in ["红柱缩短", "死叉", "绿柱放大"]
                # 避险: MACD趋势恶化
                hedge_ok = macd_status in ["死叉", "绿柱放大"]
                if profit_ok or hedge_ok:
                    record_cooldown(code, cooldown_data, now_ts)
                    label = "避险" if hedge_ok else ""
                    sig = (f"T出{label}\n  {t_shares}股 @{cur:.3f} ({sell_pct}%仓位)\n"
                           f"  ¥{t_shares * cur / 10000:.1f}万\n"
                           f"  RSI{rsi:.1f}(软评分{rsi_sell}) {macd_status}(评分{macd_sell}) 综合{total_sell_score}\n"
                           f"  量比{vol_ratio_5min or 'N/A'}")

        if sig:
            # 附加时间过滤提示
            if not allow_t_in:
                sig += "\n  [当前时段只允许T出]"
            signals.append(f"{code} {sig}")
    except Exception as e:
        print(f"[WARN] {code} 做T信号获取失败: {e}")

# 保存冷却期状态
save_cooldown(cooldown_data)

if signals:
    header = f"🔄 T信号 | {now.strftime('%H:%M')}"
    if not allow_t_in:
        header += " [尾盘只出模式]"
    print(header)
    for s in signals:
        print(f"  📡 {s}")
