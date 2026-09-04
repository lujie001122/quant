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
import logging
import time
import urllib.request
from datetime import datetime

logger = logging.getLogger(__name__)

from state_center import get_etfs_config, get_code_map

ETFS = get_etfs_config()
CODE_MAP = {code: info["sid"] for code, info in get_code_map().items()}

API_DELAY = 1  # akshare调用间隔(秒)


# ============================================================
# 数据获取层（腾讯前复权 → akshare前复权 → 东财前复权 → Sina兜底）
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
        if code.startswith("5"):
            sid = f"sh{code}"
        elif code.startswith("1"):
            sid = f"sz{code}"
    if not sid:
        raise RuntimeError(f"{code} 无代码映射")

    # ── 腾讯前复权 (主数据源) ──
    url = (f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={sid},day,,,1200,qfq")
    tencent_source_type = None
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

        klines_raw = stock_data[qfq_key].get("qfqday")
        if klines_raw:
            tencent_source_type = "tencent_qfq"
        else:
            # qfqday 为空 → 腾讯前复权不可用，不要降级为非复权 day
            day_raw = stock_data[qfq_key].get("day", [])
            if day_raw:
                logger.warning(f"{code} 腾讯qfqday为空(前复权不可用)，跳过腾讯非复权day")
                print(f"[WARN] {code} 腾讯前复权(qfqday)不可用，跳过非复权day，尝试akshare前复权")
            klines_raw = None

        if not klines_raw:
            raise RuntimeError(f"{code} 腾讯前复权接口K线为空")

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
            # 数据源标识: 腾讯前复权
            for k in klines:
                k["source"] = tencent_source_type
            # 按 start/end 过滤（start/end 为 YYYYMMDD 格式，K线日期为 YYYY-MM-DD）
            start_dt = datetime.strptime(start, "%Y%m%d").strftime("%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y%m%d").strftime("%Y-%m-%d")
            klines = [k for k in klines if start_dt <= k["date"] <= end_dt]
            return klines
    except Exception as e:
        logger.warning(f"{code} 腾讯前复权接口失败: {e}, 降级为akshare前复权")
        print(f"[WARN] {code} 腾讯前复权接口失败: {e}, 降级为akshare前复权")

    # ── akshare 前复权 (降级数据源1) ──
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
            for k in klines:
                k["source"] = "akshare_qfq"
            logger.warning(f"{code} 使用降级数据源 akshare前复权")
            print(f"[INFO] {code} 使用akshare前复权数据 (腾讯前复权不可用)")
            return klines
    except Exception as e2:
        logger.warning(f"{code} akshare前复权失败: {e2}, 尝试东财直接API")
        print(f"[WARN] {code} akshare前复权失败, 尝试东财直接API")

    # ── 东财直接API 前复权 (降级数据源2) ──
    # fqt=1 前复权, secid=市场代码.代码 (0=深市,1=沪市)
    try:
        market_id = "0" if code.startswith(("1", "15")) else "1"
        secid = f"{market_id}.{code}"
        em_url = (
            f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
            f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6&"
            f"fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&"
            f"klt=101&fqt=1&beg={start}&end={end}"
        )
        req = urllib.request.Request(em_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        em_klines = raw.get("data", {}).get("klines", [])
        if not em_klines:
            raise RuntimeError(f"{code} 东财API返回空数据")

        klines = []
        for line in em_klines:
            parts = line.split(",")
            if len(parts) < 11:
                continue
            try:
                klines.append({
                    "date": parts[0],
                    "open": float(parts[1]), "close": float(parts[2]),
                    "high": float(parts[3]), "low": float(parts[4]),
                    "volume": float(parts[5]), "amount": float(parts[6]),
                    "pct": float(parts[8]) if parts[8] else 0,
                })
            except (ValueError, IndexError):
                continue
        if klines:
            for k in klines:
                k["source"] = "eastmoney_qfq"
            logger.warning(f"{code} 使用降级数据源 东财直接API前复权")
            print(f"[INFO] {code} 使用东财直接API前复权 (腾讯/akshare不可用)")
            return klines
    except Exception as e3:
        logger.error(f"{code} 东财直接API前复权也失败: {e3}")

    # ── Sina 日K线 (兜底数据源3, 非复权) ──
    # 新浪K线API不支持前复权，仅作为最后兜底保证回测能跑完
    try:
        # 构造 Sina sid: sh510050 / sz159915
        if code.startswith("5") or code.startswith("6"):
            sina_sid = f"sh{code}"
        else:
            sina_sid = f"sz{code}"

        # datalen: 请求足够多的K线覆盖 start~end 区间
        start_dt = datetime.strptime(start, "%Y%m%d")
        end_dt = datetime.strptime(end, "%Y%m%d")
        datalen = min((end_dt - start_dt).days + 100, 1023)  # 新浪最大1023

        sina_url = (
            f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"CN_MarketData.getKLineData?symbol={sina_sid}&scale=240&ma=no&datalen={datalen}"
        )
        req = urllib.request.Request(sina_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            sina_raw = json.loads(resp.read().decode("utf-8"))

        if sina_raw and isinstance(sina_raw, list):
            klines = []
            start_fmt = start_dt.strftime("%Y-%m-%d")
            end_fmt = end_dt.strftime("%Y-%m-%d")
            for item in sina_raw:
                d = item.get("day", "")
                if not (start_fmt <= d <= end_fmt):
                    continue
                try:
                    o = float(item.get("open", 0))
                    c = float(item.get("close", 0))
                    h = float(item.get("high", 0))
                    l = float(item.get("low", 0))
                    v = float(item.get("volume", 0))
                    if o <= 0 or c <= 0:
                        continue
                    pct = round((c - o) / o * 100, 2) if o > 0 else 0
                    klines.append({
                        "date": d,
                        "open": o, "close": c, "high": h, "low": l,
                        "volume": v, "amount": 0, "pct": pct,
                    })
                except (ValueError, TypeError):
                    continue
            if klines:
                for k in klines:
                    k["source"] = "sina_no_adjust"
                logger.warning(f"{code} 使用兜底数据源 Sina非复权 (除权日可能跳空)")
                print(f"[WARN] {code} 所以前复权数据源均不可用，使用Sina非复权数据 (除权日价格可能跳空，回测仅供参考)")
                return klines
    except Exception as e4:
        logger.error(f"{code} Sina兜底也失败: {e4}")
        print(f"[ERROR] {code} Sina兜底也失败: {e4}")

    # 所有数据源均不可用 → 返回空列表而非报错退出，保证回测能继续
    logger.error(f"{code} 所有数据源均不可用 (腾讯/akshare/东财/Sina)")
    print(f"[ERROR] {code} 所有数据源均不可用 (腾讯/akshare/东财/Sina)，该ETF将无K线数据")
    return []


def fetch_klines_daily_arrays(code, sid):
    """
    获取日K线，返回拆分数组格式（供 rotation.py 使用）。

    参数:
      code: ETF 代码
      sid: 交易所代码 (如 sh515880)

    返回:
      {closes, highs, lows, volumes} 或 None
    """
    try:
        klines = fetch_klines_daily(code)
    except (RuntimeError, Exception):
        # 如果 code 映射失败（如指数 000001），用 sid 直接构造请求
        klines = _fetch_klines_by_sid(sid)
    if not klines:
        return None
    return {
        "closes": [k["close"] for k in klines],
        "highs": [k["high"] for k in klines],
        "lows": [k["low"] for k in klines],
        "volumes": [k["volume"] for k in klines],
        "dates": [k["date"] for k in klines],
    }


def _fetch_klines_by_sid(sid):
    """直接用 sid 从腾讯接口获取K线数据"""
    import urllib.request
    url = (f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={sid},day,,,1200,qfq")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        stock_data = raw.get("data", {})
        qfq_key = None
        for key in (sid, sid.lower(), sid.upper()):
            if key in stock_data:
                qfq_key = key
                break
        if not qfq_key:
            return None

        kline_list = stock_data[qfq_key].get("qfqday", [])
        if not kline_list:
            # qfqday为空 → 前复权不可用，不降级为非复权day
            logger.warning(f"{sid} 腾讯qfqday为空(前复权不可用)，_fetch_klines_by_sid跳过非复权day")
            return None

        klines = []
        for item in kline_list:
            if len(item) >= 6:
                klines.append({
                    "date": str(item[0]),
                    "open": float(item[1]) if item[1] else 0,
                    "close": float(item[2]) if item[2] else 0,
                    "high": float(item[3]) if item[3] else 0,
                    "low": float(item[4]) if item[4] else 0,
                    "volume": float(item[5]) if item[5] else 0,
                })
        return klines if klines else None
    except Exception:
        return None


def calc_max_drawdown(closes):
    """
    计算最大回撤（%），从收盘价序列。

    参数:
      closes: 收盘价序列

    返回:
      最大回撤百分比
    """
    if not closes or len(closes) < 2:
        return 0.0
    peak = closes[0]
    max_dd = 0.0
    for val in closes:
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2)


def fetch_klines_daily_df(code, start="20250101", end="20261231", adjust="qfq"):
    """获取日K线返回 pandas DataFrame"""
    import pandas as pd
    klines = fetch_klines_daily(code, start, end, adjust)
    if not klines:
        return None
    df = pd.DataFrame(klines)
    if not df.empty:
        df.set_index("date", inplace=True)
    return df


def fetch_klines_5min(code):
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
                    "source": "tencent",
                })
            except (ValueError, IndexError):
                continue
        return klines
    except Exception as e:
        logger.warning(f"{code} 5分钟K线获取失败: {e}")
        print(f"[WARN] {code} 5分钟K线获取失败: {e}")
        return []


# ============================================================
# 技术指标 — 从 factors/ 重新导出（消除重复代码）
# ============================================================

# 基础指标
from factors.indicators import calc_ma
from factors.indicators import calc_rsi_wilder
from factors.indicators import dynamic_spacing
from factors.indicators import calc_5min_indicators

# 独立因子
from factors.rsi import calc_rsi
from factors.macd import calc_macd
from factors.atr import calc_atr
from factors.ao import calc_ao

def calc_vol_ratio_120min(volumes, period=20):
    if len(volumes) < period + 1:
        return None
    avg_vol = sum(volumes[-(period + 1):-1]) / period
    return round(volumes[-1] / avg_vol, 2)


# calc_5min_indicators 和 dynamic_spacing 已从 factors.indicators 导入（上方第270-271行）
# 此处不再重复定义


# ============================================================
# 数据有效性校验（降级数据验证）
# ============================================================

def validate_and_align_data(data, source_type, code=None):
    """
    校验行情数据有效性并记录数据源类型。

    参数:
      data: K线数组 [{open, close, high, low, volume, ...}, ...]
      source_type: 数据源标识 ("sina" / "tencent" / "akshare")
      code: ETF代码（可选，用于日志）

    返回:
      (is_valid, reason)
    """
    tag = f"({code})" if code else ""

    # 检查1: 数据非空
    if data is None or (hasattr(data, '__len__') and len(data) == 0):
        return False, f"数据为空{tag}"

    # 检查2: 价格有效性 (>0)
    for i, k in enumerate(data):
        close_val = k.get("close", 0)
        try:
            close_val = float(close_val)
        except (ValueError, TypeError):
            return False, f"第{i}条K线收盘价无法解析{tag}"
        if close_val <= 0:
            return False, f"第{i}条K线收盘价非正({close_val}){tag}"

    # 检查3: 降级源警告
    if source_type == "akshare":
        logger.warning(f"{code} 使用降级数据源 akshare，指标计算可能偏差" if code else "使用降级数据源 akshare，指标计算可能偏差")
    elif source_type in ("sina", "sina_no_adjust"):
        logger.warning(f"{code} 使用兜底数据源 Sina非复权，除权日价格可能跳空" if code else "使用兜底数据源 Sina非复权，除权日价格可能跳空")
    elif source_type == "eastmoney_qfq":
        logger.warning(f"{code} 使用降级数据源 东财直接API前复权" if code else "使用降级数据源 东财直接API前复权")

    return True, f"数据有效 source={source_type}{tag}"

def validate_market_data(data, code=None):
    """
    验证行情数据有效性，防止降级数据格式不一致导致策略计算错误。

    参数:
      data: 行情数据字典，须包含 price/volume 字段
      code: ETF代码（可选，用于日志）

    返回:
      (is_valid: bool, reason: str)
    """
    source = f"({code})" if code else ""

    # 检查1: 数据非空
    if data is None:
        return False, f"数据为空{source}"

    # 检查2: 价格有效性
    price = data.get("price", 0)
    try:
        price = float(price)
    except (ValueError, TypeError):
        return False, f"价格无法解析{source}"
    if price <= 0:
        return False, f"价格异常({price}){source}"

    # 检查3: 成交量有效性
    volume = data.get("volume", 0)
    try:
        volume = float(volume)
    except (ValueError, TypeError):
        volume = 0
    if volume < 0:
        return False, f"成交量异常({volume}){source}"

    # 检查4: 最高/最低价一致性
    high = data.get("high", price)
    low = data.get("low", price)
    try:
        high = float(high)
        low = float(low)
    except (ValueError, TypeError):
        return True, f"通过（high/low不可解析，跳过一致性检查）{source}"

    if low > high:
        return False, f"最低价({low})>最高价({high})，数据异常{source}"

    # 检查5: 涨跌幅合理性（单日涨跌超20%视为异常）
    pct_change = data.get("pct_change", 0)
    try:
        pct_change = float(pct_change)
    except (ValueError, TypeError):
        pct_change = 0
    if abs(pct_change) > 20:
        return False, f"涨跌幅异常({pct_change}%){source}"

    return True, f"数据有效{source}"


def validate_klines_data(klines, code=None):
    """
    验证K线数据有效性。

    参数:
      klines: K线数组 [{open, close, high, low, volume}, ...]
      code: ETF代码（可选）

    返回:
      (is_valid, reason)
    """
    source = f"({code})" if code else ""

    if klines is None or (hasattr(klines, '__len__') and len(klines) == 0):
        return False, f"K线数据为空{source}"

    if isinstance(klines, dict):
        closes = klines.get("closes", [])
    else:
        closes = [k.get("close", 0) for k in klines]

    if len(closes) < 14:
        return False, f"K线数据不足(需≥14,实际{len(closes)}){source}"

    # 检查价格是否有效
    for c in closes:
        try:
            if float(c) <= 0:
                return False, f"K线收盘价含非正值{source}"
        except (ValueError, TypeError):
            return False, f"K线收盘价无法解析{source}"

    return True, f"K线数据有效{source}"