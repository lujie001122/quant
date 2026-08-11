#!/usr/bin/env python3
"""
行情获取与指标计算模块
从 signal_generator.py 抽取:
  - fetch_realtime_quotes, fetch_klines_daily, fetch_klines_5min
  - calc_rsi_wilder, calc_macd, calc_atr, calc_ao, calc_5min_indicators
  - 所有行情获取和指标计算函数
"""

import akshare as ak
import json
import time
import urllib.request
from datetime import datetime

from tracker import get_etfs_config, get_code_map

ETFS = get_etfs_config(fund_per_etf=44000)
CODE_MAP = {code: info["sid"] for code, info in get_code_map().items()}

API_DELAY = 1  # akshare调用间隔(秒)


# ============================================================
# 数据获取层（腾讯前复权 + akshare降级）
# ============================================================

def _akshare_retry(func, *args, retries=3, initial_delay=API_DELAY, **kwargs):
    """akshare调用重试封装"""
    for attempt in range(retries):
        try:
            time.sleep(initial_delay * (attempt + 1))
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                print(f"[RETRY {attempt+1}] {func.__name__} failed: {e}")
            else:
                raise


def _safe_float(val, default=0.0):
    """安全转换akshare返回值为float, 处理"-"、None、空字符串等异常值"""
    try:
        if val is None or val == "" or val == "-":
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


def fetch_realtime_quotes():
    """获取ETF实时行情（Sina主源0.2s→akshare备用16s）"""
    sina_url = "https://hq.sinajs.cn/list=" + ",".join(CODE_MAP.values())

    # 主数据源: Sina (单请求0.2秒)
    try:
        req = urllib.request.Request(sina_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("gbk")
        result = {}
        for line in raw.strip().split("\n"):
            if not line.strip(): continue
            idx = line.find('"')
            if idx == -1: continue
            parts = line[idx+1:line.rfind('"')].split(",")
            if len(parts) < 6: continue
            for code, sid in CODE_MAP.items():
                if sid not in line: continue
                try:
                    price = float(parts[3])
                    prev = float(parts[2])
                except ValueError:
                    continue  # 跳过异常数据行
                pct = round((price - prev) / prev * 100, 2) if prev else 0
                result[code] = {
                    "name": ETFS[code]["name"],
                    "price": price,
                    "pct_change": pct,
                    "change": round(price - prev, 3),
                    "volume": float(parts[8]) if len(parts) > 8 else 0,
                    "amount": 0,
                    "high": float(parts[4]),
                    "low": float(parts[5]),
                    "open": float(parts[1]),
                    "prev_close": prev,
                }
        return result
    except Exception:
        pass

    # 备用: akshare
    df = _akshare_retry(ak.fund_etf_spot_em)
    result = {}
    for code in ETFS:
        row = df[df["代码"] == code]
        if len(row) == 0:
            raise RuntimeError(f"未找到ETF {code} 的实时行情数据")
        r = row.iloc[0]
        result[code] = {
            "name": r["名称"],
            "price": _safe_float(r["最新价"]),
            "pct_change": _safe_float(r["涨跌幅"]),
            "change": _safe_float(r["涨跌额"]),
            "volume": _safe_float(r["成交量"]),
            "amount": _safe_float(r["成交额"]),
            "high": _safe_float(r["最高价"]),
            "low": _safe_float(r["最低价"]),
            "open": _safe_float(r["开盘价"]),
            "prev_close": _safe_float(r["昨收"]),
        }
    return result


def fetch_klines_daily(code, start="20250101", end="20261231", adjust="qfq"):
    """获取日K线(腾讯前复权接口, 自带qfq无需手动复权)"""
    sid = CODE_MAP.get(code, "")
    if not sid:
        raise RuntimeError(f"{code} 无代码映射")

    # 腾讯接口: qfq=前复权, datalen=1200条
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={sid},day,,,1200,qfq")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        # 解析: data[sid]["qfqday"] 数组, 每项 ["日期","开","收","高","低","成交量"]
        stock_data = raw.get("data", {})
        qfq_key = None
        for key in (sid, sid.lower(), sid.upper()):
            if key in stock_data:
                qfq_key = key
                break
        if not qfq_key:
            raise RuntimeError(f"{code} 腾讯接口返回无数据")

        klines_raw = stock_data[qfq_key].get("qfqday") or stock_data[qfq_key].get("day", [])
        if not klines_raw:
            raise RuntimeError(f"{code} 腾讯接口K线为空")

        klines = []
        for item in klines_raw:
            if len(item) < 6:
                continue
            try:
                o, c, h, l = float(item[1]), float(item[2]), float(item[3]), float(item[4])
                v = float(item[5])
                pct = round((c - o) / o * 100, 2) if o > 0 else 0
                klines.append({
                    "date": item[0],
                    "open": o, "close": c, "high": h, "low": l,
                    "volume": v, "amount": 0, "pct": pct,
                })
            except (ValueError, IndexError):
                continue
        if klines:
            return klines
    except Exception as e:
        print(f"[WARN] {code} 腾讯接口失败: {e}, 降级为akshare")

    # 降级: akshare
    try:
        df = _akshare_retry(
            ak.fund_etf_hist_em,
            symbol=code, period="daily",
            start_date=start, end_date=end, adjust=adjust
        )
        klines = []
        for _, row in df.iterrows():
            klines.append({
                "date": str(row["日期"]),
                "open": float(row["开盘"]), "close": float(row["收盘"]),
                "high": float(row["最高"]), "low": float(row["最低"]),
                "volume": float(row["成交量"]), "amount": float(row["成交额"]),
                "pct": float(row["涨跌幅"]),
            })
        if klines:
            return klines
    except Exception as e2:
        raise RuntimeError(f"{code} 所有K线数据源均不可用: {e2}")


def fetch_klines_5min(code, today=None):
    """获取5分钟K线(腾讯接口, 48根覆盖4小时交易时段)
    返回: [{"time","open","close","high","low","volume","amount"}, ...]
    非交易时段返回空列表
    """
    sid = CODE_MAP.get(code, "")
    if not sid:
        return []

    # 非交易时段快速返回（9:30前或15:00后）
    now = datetime.now()
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now < market_open or now > market_close:
        return []

    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sid},m5,,48"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        stock_data = raw.get("data", {})
        # 找到对应 sid 的数据
        qfq_key = None
        for key in (sid, sid.lower(), sid.upper()):
            if key in stock_data:
                qfq_key = key
                break
        if not qfq_key:
            return []

        kline_list = stock_data[qfq_key].get("m5") or stock_data[qfq_key].get("m5", [])
        if not kline_list:
            return []

        klines = []
        for item in kline_list:
            if len(item) < 6:
                continue
            try:
                o, c, h, l = float(item[1]), float(item[2]), float(item[3]), float(item[4])
                v = float(item[5])
                amt = float(item[7]) if len(item) > 7 else 0
                klines.append({
                    "time": item[0],
                    "open": o, "close": c, "high": h, "low": l,
                    "volume": v, "amount": amt,
                })
            except (ValueError, IndexError):
                continue
        return klines
    except Exception as e:
        print(f"[WARN] {code} 5分钟K线获取失败: {e}")
        return []


# ============================================================
# 技术指标（修正版）
# ============================================================

def calc_ma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period

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

def calc_atr(klines, period=14):
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        k_prev, k_curr = klines[i - 1], klines[i]
        tr = max(k_curr["high"] - k_curr["low"],
                 abs(k_curr["high"] - k_prev["close"]),
                 abs(k_curr["low"] - k_prev["close"]))
        trs.append(tr)
    if len(trs) < period: return None
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return round(atr, 4)

def calc_macd(closes, fast=12, slow=26, sig=9):
    """SMA-init MACD — 与 backtest_5etf_1m.py 完全一致"""
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


def calc_ao(highs, lows, short=5, long=34):
    """Awesome Oscillator: SMA5(median) - SMA34(median), median=(H+L)/2"""
    if len(highs) < long: return None, None
    medians = [(h+l)/2 for h,l in zip(highs, lows)]
    sma5 = sum(medians[-short:]) / short
    sma34 = sum(medians[-long:]) / long
    ao_now = sma5 - sma34
    # AO 5 bars ago
    sma5_5 = sum(medians[-short-5:-5]) / short if len(medians) >= long+5 else sma5
    sma34_5 = sum(medians[-long-5:-5]) / long if len(medians) >= long+5 else sma34
    ao_5ago = sma5_5 - sma34_5
    return ao_now, ao_5ago

def calc_vol_ratio_120min(volumes, period=20):
    if len(volumes) < period + 1:
        return None
    avg_vol = sum(volumes[-(period + 1):-1]) / period
    return round(volumes[-1] / avg_vol, 2)


def calc_5min_indicators(klines_5min):
    """基于5分钟K线计算日内分时指标
    返回: {
        "rsi_5min": Wilder RSI(14) on 5min closes,
        "macd_5min_dif/dea/bar/status": MACD(5,34,5) on 5min closes,
        "vol_ratio_5min": 当前5分钟量 / 过去20根均量,
        "day_high/day_low/day_pct": 当日最高/最低/涨跌幅
    }
    数据不足时返回 None 对应字段
    """
    if not klines_5min or len(klines_5min) < 14:
        return None

    closes = [k["close"] for k in klines_5min]
    volumes = [k["volume"] for k in klines_5min]
    highs = [k["high"] for k in klines_5min]
    lows = [k["low"] for k in klines_5min]

    # 5分钟 RSI(14) — Wilder 平滑
    rsi_5min = calc_rsi_wilder(closes, 14)

    # 5分钟 MACD(5, 34, 5) — 日内快参数
    dif5, dea5, bar5, status5 = calc_macd(closes, fast=5, slow=34, sig=5)

    # 5分钟量比: 当前5分钟量 / 过去20根均量
    vol_ratio_5min = None
    if len(volumes) >= 21:
        avg_vol_20 = sum(volumes[-21:-1]) / 20
        if avg_vol_20 > 0:
            vol_ratio_5min = round(volumes[-1] / avg_vol_20, 2)

    # 当日最高/最低/涨跌幅
    if len(klines_5min) >= 2:
        day_open = klines_5min[0]["open"]
        day_high = max(highs)
        day_low = min(lows)
        day_close = closes[-1]
        day_pct = round((day_close - day_open) / day_open * 100, 2) if day_open > 0 else 0
    else:
        day_open = klines_5min[0]["open"] if klines_5min else 0
        day_high = day_open
        day_low = day_open
        day_close = closes[-1] if closes else 0
        day_pct = 0

    # 5分钟 ATR(14) — 基于5分钟K线，用于做T配对挂单
    atr_5min = calc_atr(klines_5min, 14)

    # 5分钟 MA20 斜率 — 用于做T趋势过滤
    ma20_5min_slope = None
    if len(closes) >= 20:
        ma20_now = sum(closes[-20:]) / 20
        ma20_5ago = sum(closes[-25:-5]) / 20 if len(closes) >= 25 else None
        if ma20_5ago and ma20_5ago > 0:
            ma20_5min_slope = round((ma20_now - ma20_5ago) / ma20_5ago, 6)

    return {
        "rsi_5min": rsi_5min,
        "macd_5min_status": status5,
        "macd_5min_dif": dif5,
        "macd_5min_dea": dea5,
        "macd_5min_bar": bar5,
        "vol_ratio_5min": vol_ratio_5min,
        "atr_5min": atr_5min,
        "ma20_5min_slope": ma20_5min_slope,
        "day_high": day_high,
        "day_low": day_low,
        "day_pct": day_pct,
    }

def dynamic_spacing(atr, price, base_spacing):
    if atr is None or price == 0:
        return base_spacing
    atr_pct = atr / price
    adjusted = 0.6 * base_spacing + 0.4 * atr_pct
    return round(max(base_spacing * 0.5, min(base_spacing * 2.0, adjusted)), 4)