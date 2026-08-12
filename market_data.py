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
    """安全转换返回值为float, 处理None、空字符串等异常值"""
    try:
        if val is None or val == "" or val == "-":
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


# 扶瑶API配置
_FUYAO_BASE = "https://fuyao.aicubes.cn"
_FUYAO_HEADERS = {"X-api-key": "sk-fuyao-KuEuKMVJQ8dVfZhi0AzVDULSxAzOAwv2"}


def _code_to_thscode(code):
    """将ETF代码转换为扶瑶thscode格式: 1xxxxx→.SZ, 5xxxxx→.SH"""
    if code.startswith("1"):
        return f"{code}.SZ"
    elif code.startswith("5"):
        return f"{code}.SH"
    else:
        return f"{code}.SZ"  # fallback


def _fetch_single_quote(code):
    """通过扶瑶API获取单只ETF实时行情"""
    thscode = _code_to_thscode(code)
    url = f"{_FUYAO_BASE}/api/fund/market/snapshot?thscode={thscode}"
    req = urllib.request.Request(url, headers=_FUYAO_HEADERS)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"扶瑶API返回错误(code={data.get('code')}): {data.get('message')}")
    items = data.get("data", {}).get("item", [])
    if not items:
        raise RuntimeError(f"扶瑶API返回空数据: {code}")
    return items[0]


def fetch_realtime_quotes():
    """获取ETF实时行情（扶瑶API: /api/fund/market/snapshot）"""
    result = {}
    for code in ETFS:
        try:
            item = _fetch_single_quote(code)
            price = _safe_float(item.get("last_price"))
            prev = _safe_float(item.get("prev_price"))
            pct = _safe_float(item.get("price_change_ratio_pct"))
            chg = _safe_float(item.get("price_change"))
            result[code] = {
                "name": ETFS[code]["name"],
                "price": price,
                "pct_change": round(pct, 2),
                "change": round(chg, 3),
                "volume": _safe_float(item.get("volume")),
                "amount": _safe_float(item.get("turnover")),
                "high": _safe_float(item.get("high_price")),
                "low": _safe_float(item.get("low_price")),
                "open": _safe_float(item.get("open_price")),
                "prev_close": prev,
            }
        except Exception as e:
            print(f"[WARN] {code} 扶瑶API获取行情失败: {e}")
            continue
    if not result:
        raise RuntimeError("所有ETF行情获取均失败")
    return result


def fetch_klines_daily(code, start="20250101", end="20261231", adjust="qfq", sid=None):
    """获取日K线(腾讯前复权接口, 自带qfq无需手动复权)"""
    if sid is None:
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


def fetch_klines_daily_arrays(code, sid=None):
    """获取日K线并转换为 {closes, highs, lows, volumes} 四个数组
    统一 rotation.py 和 backtest_bt 的格式转换。
    """
    klines = fetch_klines_daily(code, sid=sid)
    if not klines or len(klines) < 30:
        return None

    closes = [d["close"] for d in klines]
    highs = [d["high"] for d in klines]
    lows = [d["low"] for d in klines]
    volumes = [d["volume"] for d in klines]
    return {"closes": closes, "highs": highs, "lows": lows, "volumes": volumes,
            "_raw_klines": klines}


def fetch_klines_daily_df(code, sid=None):
    """获取日K线并转换为 pandas DataFrame (OHLCV)
    统一 backtest_bt 的格式转换。
    """
    import pandas as pd
    klines = fetch_klines_daily(code, sid=sid)
    rows = []
    for k in klines:
        rows.append({
            "date": k["date"],
            "open": k["open"],
            "close": k["close"],
            "high": k["high"],
            "low": k["low"],
            "volume": k["volume"],
        })
    if len(rows) <= 50:
        raise RuntimeError(f"{code} K线不足({len(rows)}条)")
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    return df[["open", "high", "low", "close", "volume"]]


def calc_max_drawdown(closes):
    """计算历史最大回撤(%)
    从 rotation.py 移动到此统一管理。
    """
    if len(closes) < 2:
        return None
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        dd = (peak - c) / peak * 100
        if dd > max_dd:
            max_dd = dd
    return max_dd


def fetch_klines_5min(code, today=None):
    """获取5分钟K线(腾讯接口, 1200根覆盖约20天历史)
    返回: [{"time","open","close","high","low","volume","amount"}, ...]
    today参数预留，供回测传入日期使用
    """
    sid = CODE_MAP.get(code, "")
    if not sid:
        return []

    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sid},m5,,1200"
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