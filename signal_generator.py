#!/usr/bin/env python3
"""
量化交易信号生成器 v3.1
10项核心策略逻辑优化:
  P0: 分级减仓替代单一清仓 | 底仓/活动仓真正分离
  P1: 建仓门槛提高+分批 | 做T弹性条件 | 移动止盈 | 网格重置确认
  P2: 防御盾对冲 | 波动率自适应频率 | 空仓期 | 网格熔断
基础: akshare/Wilder RSI/SMA预热MACD/ATR动态间距/倒金字塔/5分钟量比
# 标的: 159516, 515880, 588170, 159532, 515050, 159611
# 策略: 震荡双模式 v4
# 资金: 各上限4.4万, 总计22万(6只共享)

"""

import akshare as ak
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta


# ============================================================
# 一、配置
# ============================================================

from tracker import get_etfs_config, get_code_map

ETFS = get_etfs_config(fund_per_etf=44000)
CODE_MAP = {code: info["sid"] for code, info in get_code_map().items()}

CASH_RESERVE = 0
TOTAL_FUND = 220000
DEAD_RATIO = 0.6
ACTIVE_RATIO = 0.4
GRID_BUY_LEVELS = 5
GRID_SELL_LEVELS = 3
MAX_DAILY_BUYS = 1   # 每天最多1次买入(正常)
MAX_DAILY_T0 = 2     # 每天最多2次做T(正常)
DEFENSE_CODE = "159611"  # 防御标的: 电力ETF
COOLDOWN_DAYS = 2

API_DELAY = 1  # akshare调用间隔(秒)

# 模块级常量: 倒金字塔权重
INVERTED_WEIGHTS = [0.20, 0.25, 0.30, 0.35, 0.40]


# ============================================================
# 二、数据获取层（腾讯前复权 + akshare降级）
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
# 三、技术指标（修正版）
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


# ============================================================
# 四、信号判定层（v3.1 十项优化）
# ============================================================

class PositionInfo:
    def __init__(self, base_price=None, shares=0, cost=0.0, peak_price=0.0):
        self.base_price = base_price
        self.shares = shares
        self.cost = cost
        self.avg_cost = cost / shares if shares > 0 else 0.0
        self.current_price = 0.0
        self.peak_price = peak_price
        self.dead_shares = 0   # 底仓(P0真正分离)
        self.active_shares = 0  # 活动仓(P0真正分离)
        self.daily_trade_log = {}
        # P0: 分级减仓状态
        self.stop_level = 0
        self.below_ma20_count = 0
        self.below_ma20_date = None
        # P1: 移动止盈
        self.trailing_stop_price = 0.0
        self.reached_8pct = False
        self.reached_15pct = False
        # P1: 分批建仓
        self.build_phase = 0
        self.build_first_price = 0.0
        self.ma5_touch_count = 0
        # P1: 网格重置冷却期
        self.last_reset_date = None
        # P2: 冷静期
        self.cooldown_until = None
        self.liquidate_dates = []
        # P1: 网格熔断
        self.grid_frozen = False
        self.last_grid_trigger = None  # 当天已触发的网格档位
        # 前一根RSI(做T动态RSI)
        self.prev_rsi = None
        # 补仓次数限制
        self.add_count = 0
        # 空仓天数(趋势跟踪通道需要)
        self.empty_days = 0
        self.empty_days_date = None  # 空仓天数去重标记
        # 前一日MACD状态(Test抄底需要)
        self.prev_macd_status = None
        # 保本止盈状态
        self.breakeven_activated = False

    @property
    def has_position(self):
        return self.shares > 0

    def profit_pct(self, current_price):
        if self.avg_cost == 0: return 0.0
        return (current_price - self.avg_cost) / self.avg_cost * 100

    def drawdown_from_peak(self, current_price):
        if self.peak_price == 0: return 0.0
        return (self.peak_price - current_price) / self.peak_price * 100

    def max_daily_buys(self, atr_pct):
        if atr_pct > 0.05: return 2
        return MAX_DAILY_BUYS

    def max_daily_t0(self, atr_pct):
        if atr_pct > 0.05: return 3
        return MAX_DAILY_T0

    def _get_daily_log(self, date_str):
        """获取当日交易日志, 带默认值和类型检查"""
        log = self.daily_trade_log.get(date_str, {"buy_count": 0, "t0_count": 0})
        if not isinstance(log, dict):
            log = {"buy_count": 0, "t0_count": 0}
        return log

    def can_buy_today(self, date_str, atr_pct=None):
        max_b = self.max_daily_buys(atr_pct) if atr_pct else MAX_DAILY_BUYS
        return self._get_daily_log(date_str).get("buy_count", 0) < max_b

    def can_t0_today(self, date_str, atr_pct=None):
        max_t = self.max_daily_t0(atr_pct) if atr_pct else MAX_DAILY_T0
        return self._get_daily_log(date_str).get("t0_count", 0) < max_t

    def record_buy(self, date_str):
        log = self.daily_trade_log.setdefault(date_str, {"buy_count": 0, "t0_count": 0})
        log["buy_count"] = log.get("buy_count", 0) + 1

    def record_t0(self, date_str):
        log = self.daily_trade_log.setdefault(date_str, {"buy_count": 0, "t0_count": 0})
        log["t0_count"] = log.get("t0_count", 0) + 1

    def is_in_cooldown(self, date_str):
        if self.cooldown_until is None: return False
        return date_str <= self.cooldown_until

    def update_trailing_stop(self, price):
        if not self.has_position or self.avg_cost == 0: return
        profit_pct = self.profit_pct(price)
        if profit_pct >= 8 - 0.01 and not self.reached_8pct:
            self.reached_8pct = True
            # 优化: 8%后不改止盈线，等15%后再设
        if profit_pct >= 15 - 0.01 and not self.reached_15pct:
            self.reached_15pct = True
            self.trailing_stop_price = self.avg_cost * 1.05
        if self.reached_15pct and self.peak_price > 0:
            self.trailing_stop_price = self.peak_price * 0.95

    def update_dead_active(self):
        """减仓/清仓后同步更新底仓/活动仓"""
        self.dead_shares = int(self.shares * DEAD_RATIO)
        self.active_shares = self.shares - self.dead_shares
        if self.active_shares == 0 and self.shares > 0:
            self.active_shares = int(self.shares * ACTIVE_RATIO)

    def reset_on_liquidate(self, date_str):
        """清仓后重置所有状态"""
        self.shares = 0
        self.cost = 0
        self.avg_cost = 0
        self.base_price = None
        self.dead_shares = 0
        self.active_shares = 0
        self.build_phase = 0
        self.build_first_price = 0.0
        self.stop_level = 0
        self.below_ma20_count = 0
        self.below_ma20_date = None
        self.add_count = 0
        self.ma5_touch_count = 0
        self.reached_8pct = False
        self.reached_15pct = False
        self.trailing_stop_price = 0.0
        self.grid_frozen = False
        self.last_grid_trigger = None
        self.peak_price = 0.0
        self.breakeven_activated = False
        self.liquidate_dates.append(date_str)
        self.empty_days = 0
        # 设置冷静期
        from datetime import datetime as _dt, timedelta as _td
        try:
            dt = _dt.strptime(date_str, "%Y-%m-%d")
            self.cooldown_until = (dt + _td(days=COOLDOWN_DAYS)).strftime("%Y-%m-%d")
        except Exception:
            self.cooldown_until = None

    def reduce_shares(self, pct, price=0.0):
        """减仓指定比例(0-100, 百分比), 取整到100股"""
        reduce_count = int(self.shares * pct / 10000) * 100
        if reduce_count < 100:
            # 小数量时向下取整到100的整数倍,不足100则取全部
            reduce_count = int(self.shares * pct / 100)
            reduce_count = reduce_count // 100 * 100
            if reduce_count < 100 and self.shares >= 100:
                reduce_count = 100
        if reduce_count > self.shares:
            reduce_count = self.shares
        self.shares -= reduce_count
        self.cost = self.avg_cost * self.shares if self.shares > 0 else 0
        # 减仓后用当前价作为新峰值, 硬止损20%仍然有效
        self.peak_price = price
        self.update_dead_active()

    def _enter_position(self, date_str, price, channel_name):
        """6通道入场统一初始化: record_buy + 建仓状态设置"""
        self.record_buy(date_str)
        self.build_phase = 1
        self.build_first_price = price
        self.base_price = price
        self.empty_days = 0
        return "买入", channel_name, "buy"

    def to_dict(self, today_str):
        """将状态序列化为字典(用于持久化)"""
        return {
            "build_phase": self.build_phase,
            "build_first_price": self.build_first_price,
            "stop_level": self.stop_level,
            "reached_8pct": self.reached_8pct,
            "reached_15pct": self.reached_15pct,
            "trailing_stop_price": self.trailing_stop_price,
            "cooldown_until": self.cooldown_until,
            "grid_frozen": self.grid_frozen,
            "last_grid_trigger": self.last_grid_trigger,
            "last_reset_date": self.last_reset_date,
            "peak_price": self.peak_price,
            "breakeven_activated": self.breakeven_activated,
            "below_ma20_count": self.below_ma20_count,
            "below_ma20_date": self.below_ma20_date,
            "add_count": self.add_count,
            "ma5_touch_count": self.ma5_touch_count,
            "prev_rsi": self.prev_rsi,
            "prev_macd_status": self.prev_macd_status,
            "liquidate_dates": self.liquidate_dates,
            "empty_days": self.empty_days,
            "empty_days_date": self.empty_days_date,
            "daily_trade_log": {today_str: self.daily_trade_log.get(today_str, {"buy_count": 0, "t0_count": 0})},
            "shares": self.shares,
            "avg_cost": self.avg_cost,
            "current_price": self.current_price,
        }

    def from_dict(self, data):
        """从字典恢复状态(用于持久化恢复)"""
        self.build_phase = data.get("build_phase", 0)
        self.build_first_price = data.get("build_first_price", 0.0)
        self.stop_level = data.get("stop_level", 0)
        self.reached_8pct = data.get("reached_8pct", False)
        self.reached_15pct = data.get("reached_15pct", False)
        self.trailing_stop_price = data.get("trailing_stop_price", 0.0)
        self.below_ma20_count = data.get("below_ma20_count", 0)
        self.below_ma20_date = data.get("below_ma20_date", None)
        self.cooldown_until = data.get("cooldown_until")
        self.grid_frozen = data.get("grid_frozen", False)
        self.peak_price = data.get("peak_price", 0.0)
        self.breakeven_activated = data.get("breakeven_activated", False)
        self.add_count = data.get("add_count", 0)
        self.ma5_touch_count = data.get("ma5_touch_count", 0)
        self.prev_rsi = data.get("prev_rsi")
        self.prev_macd_status = data.get("prev_macd_status")
        self.liquidate_dates = data.get("liquidate_dates", [])
        self.last_grid_trigger = data.get("last_grid_trigger")
        self.last_reset_date = data.get("last_reset_date")
        self.empty_days = data.get("empty_days", 0)
        self.empty_days_date = data.get("empty_days_date")
        self.shares = data.get("shares", self.shares)
        self.avg_cost = data.get("avg_cost", self.avg_cost)
        self.current_price = data.get("current_price", self.current_price)


def is_auction_time(now=None):
    """集合竞价时段: 9:15-9:25, 14:57-15:00"""
    if now is None:
        now = datetime.now()
    t = now.hour * 60 + now.minute
    return (555 <= t <= 565) or (897 <= t <= 900)  # 9:15=555, 9:25=565, 14:57=897, 15:00=900


def t0_buy_score(rsi_5min, macd_5min_status, vol_ratio_5min):
    """做T买入评分(基于5分钟分时): RSI超卖+MACD多头确认+量能温和
    ★ RSI极端兜底通道: RSI < 25 直接返回3分(跳过MACD/量比检查)
    - RSI_5min 25-45: 原有3项共振评分逻辑
    - RSI_5min < 45 必须满足(超卖才摊低)
    - MACD_5min 金叉或红柱(趋势确认)
    - 量比 < 1.5(不放量砸盘)
    """
    if rsi_5min is None:
        return 0
    # RSI极端互斥: RSI > 75 时只允许卖出，买入强制归零
    if rsi_5min > 75:
        return 0
    # RSI极端兜底通道: RSI < 25 直接满分，不检查 MACD 和量比
    if rsi_5min < 25:
        return 3
    # RSI 25-75: 原有3项共振评分逻辑
    if rsi_5min >= 45:
        return 0  # 5分钟RSI不超卖，不做T买入
    if macd_5min_status in ["死叉", "绿柱放大"]:
        return 0  # 趋势恶化，禁止做T买入
    score = 1  # RSI_5min<45已满足
    if macd_5min_status in ["金叉", "红柱放大"]:
        score += 1
    if vol_ratio_5min is not None and vol_ratio_5min < 1.5:
        score += 1
    return score


def t0_sell_score(rsi_5min, macd_5min_status, vol_ratio_5min):
    """做T卖出评分(基于5分钟分时): RSI超买+MACD空头确认+量能放大
    ★ RSI极端互斥: RSI < 25 时只允许买入，卖出强制归零
    - RSI > 75: 极端兜底直接满分
    - RSI_5min 55-75: 原有3项共振评分逻辑
    - RSI_5min > 55 必须满足(超买才卖)
    - MACD_5min 死叉或绿柱(趋势结束)
    - 量比 > 1.2(放量拉升)
    """
    if rsi_5min is None:
        return 0
    # RSI极端互斥: RSI < 25 时只允许买入，卖出强制归零
    if rsi_5min < 25:
        return 0
    # RSI极端兜底通道: RSI > 75 直接满分，不检查 MACD 和量比
    if rsi_5min > 75:
        return 3
    # RSI 25-75: 原有3项共振评分逻辑
    if rsi_5min <= 55:
        return 0  # 5分钟RSI不超买，不卖出
    if macd_5min_status in ["金叉", "红柱放大"]:
        return 0  # 趋势向好，禁止做T卖出
    score = 1  # RSI_5min>55已满足
    if macd_5min_status in ["死叉", "绿柱放大"]:
        score += 1
    if vol_ratio_5min is not None and vol_ratio_5min > 1.2:
        score += 1
    return score


def evaluate_grid_signals(price, base_price, spacing, grid_frozen=False):
    """网格触达(含熔断机制)"""
    if base_price is None:
        return "无(未设基准价)", 0
    if grid_frozen:
        return "网格冻结(跌破第5档,等站上MA5解冻)", 0

    tolerance = spacing * 0.15

    for i in range(1, GRID_BUY_LEVELS + 1):
        grid_price = base_price * (1 - spacing * i)
        if abs(price - grid_price) <= tolerance * grid_price:
            weight = INVERTED_WEIGHTS[i - 1]
            frozen_note = " [熔断!第5档后冻结网格]" if i == 5 else ""
            return f"触及第{i}档买入(网格价{grid_price:.3f},仓位{weight*100:.0f}%){frozen_note}", weight

    for i in range(1, GRID_SELL_LEVELS + 1):
        grid_price = base_price * (1 + spacing * i)
        if abs(price - grid_price) <= tolerance * grid_price:
            return f"触及第{i}档卖出(网格价{grid_price:.3f})", 0.20

    return "无", 0


def check_grid_reset(price, base_price, spacing, date_str, last_reset_date=None):
    """网格基准重置(含冷却期+下行重置)"""
    if base_price is None:
        return base_price, False, last_reset_date
    # 冷却期: 同一天不二次重置
    if last_reset_date is not None and date_str == last_reset_date:
        return base_price, False, last_reset_date
    # 上行重置
    upper_limit = base_price * (1 + spacing * 4)
    if price > upper_limit:
        return round(price, 3), True, date_str
    # 下行重置
    lower_limit = base_price * (1 - spacing * 6)
    if price < lower_limit:
        return round(price, 3), True, date_str
    return base_price, False, last_reset_date


def compute_grid_table(base_price, spacing, fund):
    if base_price is None: return None
    import math
    table = {"base": base_price, "spacing": spacing, "buy": [], "sell": []}
    for i in range(1, GRID_BUY_LEVELS + 1):
        p = base_price * (1 - spacing * i)
        w = INVERTED_WEIGHTS[i - 1]
        per_fund = fund * w
        if math.isnan(p) or p <= 0:
            continue
        shares = int(per_fund / p / 100) * 100
        table["buy"].append({"grid": i, "price": round(p, 3), "shares": shares,
                             "fund": round(shares * p, 0), "weight": f"{w*100:.0f}%"})
    for i in range(1, GRID_SELL_LEVELS + 1):
        p = base_price * (1 + spacing * i)
        table["sell"].append({"grid": i, "price": round(p, 3)})
    return table


# ============================================================
# 五、综合信号生成
# ============================================================

def generate_signals(positions=None, all_klines=None, all_tech=None):
    """
    positions: 每个标的的PositionInfo(含状态追踪)
    all_klines: 120分钟K线数据(已获取)
    all_tech: 技术指标数据(已计算)
    首次调用时会获取数据并计算指标
    """
    if positions is None:
        positions = {code: PositionInfo() for code in ETFS}

    today_str = datetime.now().strftime("%Y-%m-%d")

    # 读取 portfolio.json 判断哪些标的今日已交易
    import os as _os
    pf_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "portfolio.json")
    already_traded_today = set()
    pf_data = {}
    if _os.path.exists(pf_path):
        try:
            with open(pf_path) as _f:
                pf_data = json.load(_f)
            for code, pos_data in pf_data.get("positions", {}).items():
                if pos_data.get("shares", 0) > 0:
                    for t in pos_data.get("trades", []):
                        if t.get("date") == today_str:
                            already_traded_today.add(code)
                            break
            # 恢复持仓状态
            saved_state = pf_data.get("_signal_state", {})
            for code in ETFS:
                if code in positions:
                    p = positions[code]
                    # 从 _signal_state 恢复信号状态
                    if code in saved_state:
                        s = saved_state[code]
                        p.from_dict(s)
                        # 恢复daily_trade_log（当天）
                        saved_log = s.get("daily_trade_log", {})
                        if today_str in saved_log:
                            p.daily_trade_log[today_str] = saved_log[today_str]
                    # 持仓数量从 portfolio 恢复（即使没有 _signal_state 也要恢复持仓）
                    pos_pf = pf_data["positions"].get(code, {})
                    if pos_pf.get("shares", 0) > 0:
                        p.shares = pos_pf["shares"]
                        p.cost = pos_pf.get("total_cost", pos_pf["shares"] * pos_pf.get("avg_cost", 0))
                        p.avg_cost = pos_pf["avg_cost"]
                        p.current_price = pos_pf.get("current_price", 0.0)
                        p.base_price = pos_pf.get("base_price") or pos_pf["avg_cost"]
                        p.update_dead_active()
                        # 持仓恢复后：如果 build_phase=0 但实际有持仓，修正为 build_phase=1
                        if p.has_position and p.build_phase == 0:
                            p.build_phase = 1
                            p.add_count = 0
                    # last_grid_trigger跨天重置
                    if p.last_grid_trigger and p.last_grid_trigger != today_str:
                        p.last_grid_trigger = None
        except Exception:
            pass

    # 获取数据
    try:
        realtime = fetch_realtime_quotes()
    except Exception as e:
        print(f"[WARN] fetch_realtime_quotes失败: {e}")
        realtime = {code: {"name": ETFS[code]["name"], "price": 0, "pct_change": 0, "change": 0,
                           "volume": 0, "amount": 0, "high": 0, "low": 0, "open": 0, "prev_close": 0}
                    for code in ETFS}

    # 如果没有传入K线和指标数据，需要获取
    if all_klines is None:
        all_klines = {}
        for code in ETFS:
            all_klines[code] = fetch_klines_daily(code)

    if all_tech is None:
        all_tech = {}
        for code in ETFS:
            klines = all_klines[code]
            closes = [k["close"] for k in klines]
            volumes = [k["volume"] for k in klines]
            ma5 = calc_ma(closes, 5)
            ma10 = calc_ma(closes, 10)
            ma20 = calc_ma(closes, 20)
            rsi = calc_rsi_wilder(closes, 14)
            dif, dea, macd_bar, macd_status = calc_macd(closes)
            vol_ratio_120 = calc_vol_ratio_120min(volumes, 20)
            atr = calc_atr(klines, 14)
            base_s = ETFS[code]["base_spacing"]
            spacing = dynamic_spacing(atr, realtime[code]["price"], base_s)

            # AO momentum
            highs = [k["high"] for k in klines]
            lows = [k["low"] for k in klines]
            ao_now, ao_5ago = calc_ao(highs, lows)

            # 5分钟分时指标
            klines_5min = fetch_klines_5min(code)
            indicators_5min = calc_5min_indicators(klines_5min)

            all_tech[code] = {
                "ma5": ma5, "ma10": ma10, "ma20": ma20,
                "rsi": rsi, "dif": dif, "dea": dea, "macd_bar": macd_bar,
                "macd_status": macd_status, "vol_ratio_120": vol_ratio_120,
                "atr": atr, "spacing": spacing,
                "vol_ratio_5min": indicators_5min["vol_ratio_5min"] if indicators_5min else None,
                "atr_5min": indicators_5min["atr_5min"] if indicators_5min else None,
                "rsi_5min": indicators_5min["rsi_5min"] if indicators_5min else None,
                "macd_5min_status": indicators_5min["macd_5min_status"] if indicators_5min else None,
                "macd_5min_dif": indicators_5min["macd_5min_dif"] if indicators_5min else None,
                "macd_5min_dea": indicators_5min["macd_5min_dea"] if indicators_5min else None,
                "macd_5min_bar": indicators_5min["macd_5min_bar"] if indicators_5min else None,
                "day_high": indicators_5min["day_high"] if indicators_5min else None,
                "day_low": indicators_5min["day_low"] if indicators_5min else None,
                "day_pct": indicators_5min["day_pct"] if indicators_5min else None,
                "ma20_5min_slope": indicators_5min["ma20_5min_slope"] if indicators_5min else None,
                "ao_now": ao_now, "ao_5ago": ao_5ago,
            }

    # 网格基准价
    base_prices = {}
    for code, pos in positions.items():
        if pos.has_position and pos.base_price:
            price = realtime[code]["price"]
            spacing = all_tech[code]["spacing"]
            new_base, reset, new_reset_date = check_grid_reset(
                price, pos.base_price, spacing, today_str, pos.last_reset_date
            )
            if reset:
                pos.base_price = new_base
                pos.last_reset_date = new_reset_date
            base_prices[code] = pos.base_price
        else:
            base_prices[code] = realtime[code]["price"]

    # P2: 市场环境过滤(检查515080防御标的是否也弱势)
    defense_weak = False
    if DEFENSE_CODE and DEFENSE_CODE in all_tech and DEFENSE_CODE in all_klines:
        d_tech = all_tech[DEFENSE_CODE]
        d_price = realtime[DEFENSE_CODE]["price"]
        d_ma20 = d_tech["ma20"]
        d_dif = d_tech["dif"]
        d_macd = d_tech["macd_status"]
        if d_ma20 and d_price < d_ma20 and d_dif is not None and d_dif < 0 and d_macd in ["死叉", "绿柱放大"]:
            defense_weak = True

    # 信号生成
    signals_output = {}
    for code, cfg in ETFS.items():
        if code not in realtime:
            signals_output[code] = {"action": "持有(观望)", "reason": "无实时数据", "price": "N/A"}
            continue
        r = realtime[code]
        if r.get("price", 0) == 0:
            signals_output[code] = {"action": "持有(观望)", "reason": "无实时数据", "price": "N/A"}
            continue
        if code not in all_tech or all_tech[code].get("macd_status") == "数据不足":
            signals_output[code] = {"action": "持有(观望)", "reason": "指标数据不足", "price": f"{realtime[code]['price']:.3f}"}
            continue
        t = all_tech[code]
        pos = positions[code]
        price = r["price"]
        spacing = t["spacing"]
        fund = cfg["fund"]
        base = base_prices[code]
        atr_pct = (t.get("atr_5min") or t["atr"]) / price if (t.get("atr_5min") or t["atr"]) and price else 0

        # 更新峰值和移动止盈线
        if pos.has_position:
            if price > pos.peak_price:
                pos.peak_price = price
            pos.update_trailing_stop(price)

        # ── 网格解冻: 站上MA5解冻 ──
        if pos.grid_frozen and t["ma5"] and price > t["ma5"]:
            pos.grid_frozen = False

        # P0: 分级减仓 =====
        stop_signal = "未触发"
        stop_actions = []

        if pos.has_position:
            # ── 保本止盈: 浮盈≥10%激活, 回落到成本+2%清仓 ──
            if pos.avg_cost > 0:
                profit_pct = (price - pos.avg_cost) / pos.avg_cost
                if profit_pct >= 0.10 - 0.0001:
                    pos.breakeven_activated = True
                if pos.breakeven_activated and price <= pos.avg_cost * 1.02:
                    stop_actions.append((f"保本止盈(avg={pos.avg_cost:.3f}→现价{price:.3f})", "breakeven_stop"))

            # ── 均价止损12%: 从买入均价跌12%无条件清仓 ──
            if pos.avg_cost > 0 and price <= pos.avg_cost * 0.88:
                stop_actions.append((f"均价止损12%(avg={pos.avg_cost:.3f}→现价{price:.3f})", "avg_stop_12pct"))

            # ── 硬止盈30%: base_price*1.30 ──
            if pos.base_price and price >= pos.base_price * 1.30:
                stop_actions.append((f"硬止盈30%(基准{pos.base_price:.3f}→现价{price:.3f})", "liquidate_30pct"))

            # ── 移动止盈 ──
            if pos.trailing_stop_price > 0 and price <= pos.trailing_stop_price:
                label = "移动止盈8%(成本+5%)" if not pos.reached_15pct else "移动止盈15%(峰值回撤5%)"
                stop_actions.append((f"{label}:触线{pos.trailing_stop_price:.3f}", "liquidate_trailing"))

            # ── 硬止损20%: 从价格峰值回撤20%清仓 ──
            if pos.peak_price > 0 and price < pos.peak_price * 0.80:
                stop_actions.append((f"硬止损20%(峰值{pos.peak_price:.3f}→现价{price:.3f})", "hard_stop_20pct"))

            # Level 1a: 连续2天破MA20 → 减30%总仓(按天累加，同一天多次运行只算1次)
            if t["ma20"] and pos.stop_level == 0:
                if price < t["ma20"]:
                    if pos.below_ma20_date != today_str:
                        pos.below_ma20_count += 1
                        pos.below_ma20_date = today_str
                    if pos.below_ma20_count >= 2:
                        stop_actions.append(("分级止损1级:连续2天破MA20减30%", "reduce_30pct_all"))
                        pos.stop_level = 1
                else:
                    pos.below_ma20_count = 0
                    pos.below_ma20_date = None

            # Level 1b: DIF<0 → 再减30%
            if pos.stop_level == 1 and t["dif"] is not None and t["dif"] < 0:
                stop_actions.append(("分级止损2级:DIF<0再减30%", "reduce_30pct_all"))
                pos.stop_level = 2

            # Level 1c: MACD死叉/绿柱放大 → 清仓
            if pos.stop_level == 2 and t["macd_status"] in ["死叉", "绿柱放大"]:
                stop_actions.append(("分级止损3级:MACD趋势恶化清仓", "liquidate_trend"))
                pos.stop_level = 3

            # 分级止损恢复(加严: 价>MA20+DIF>0+MACD红柱)
            if pos.stop_level > 0 and t["ma20"] and price > t["ma20"] and t["dif"] is not None and t["dif"] > 0 and t["macd_status"] in ["红柱放大", "红柱缩短"]:
                pos.stop_level = 0
                pos.below_ma20_count = 0
                pos.below_ma20_date = None

            # 分级止损恢复: stop_level=2 但 MACD 未恶化且价格恢复 → 重置
            # 加严条件: price > MA20 AND DIF > 0 都满足才重置, 防止过早恢复
            if pos.stop_level == 2 and t["macd_status"] not in ["死叉", "绿柱放大"] and t["ma20"] and price > t["ma20"] and t["dif"] is not None and t["dif"] > 0:
                pos.stop_level = 0
                pos.below_ma20_count = 0
                pos.below_ma20_date = None

        # P1: 趋势止盈(MACD红柱缩短+破MA5)
        if pos.has_position and t["macd_status"] == "红柱缩短" and price < t["ma5"]:
            stop_actions.append(("趋势止盈(MACD红柱缩短+破MA5)卖10%活动仓", "trend_profit_sell"))

        # 破MA5卖活动仓5%(P0: active=0时不触发)
        if pos.has_position and price < t["ma5"] and t["rsi"] and t["rsi"] > 50:
            if pos.active_shares > 0:
                stop_actions.append(("破MA5卖活动仓5%", "sell_active_5pct"))

        # 止盈止损优先级处理
        if stop_actions:
            # 优先级: 清仓 > 减仓 > 卖活动仓
            for sig_name, sig_type in stop_actions:
                if sig_type in ("liquidate_trend", "liquidate_trailing", "liquidate_30pct", "avg_stop_12pct", "hard_stop_20pct", "breakeven_stop"):
                    stop_signal = sig_name
                    break
            if stop_signal == "未触发":
                for sig_name, sig_type in stop_actions:
                    if sig_type in ("reduce_30pct_all",):
                        stop_signal = sig_name
                        break
            if stop_signal == "未触发":
                stop_signal = stop_actions[0][0]
        elif pos.has_position:
            stop_signal = "未触发"
        else:
            stop_signal = "未触发(无持仓)"

        # Level 2: 做T弹性条件（基于5分钟分时数据）
        # 集合竞价时段不生成做T信号
        now = datetime.now()
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=0, second=0, microsecond=0)
        is_market_hours = market_open <= now <= market_close
        in_auction = is_auction_time(now)

        # 5分钟指标
        rsi_5min = t.get("rsi_5min")
        macd_5min_status = t.get("macd_5min_status")
        vol_ratio_5min = t.get("vol_ratio_5min")
        ma20_5min_slope = t.get("ma20_5min_slope")

        # 11点前不做T（5分钟K线不够48根，MA20斜率不可靠）
        if not is_market_hours or rsi_5min is None or in_auction or now.hour < 11:
            buy_score = 0
            sell_score = 0
        else:
            buy_score = t0_buy_score(rsi_5min, macd_5min_status, vol_ratio_5min)
            sell_score = t0_sell_score(rsi_5min, macd_5min_status, vol_ratio_5min)

            # MA20斜率趋势过滤: 强趋势禁止反向做T
            if ma20_5min_slope is not None:
                if ma20_5min_slope > 0.003:   # 上升趋势(斜率>0.3%) → 禁止T卖出
                    sell_score = 0
                elif ma20_5min_slope < -0.005:  # 下降趋势(斜率<-0.5%) → 禁止T买入
                    buy_score = 0

        t0_signal = "无"
        if not is_market_hours:
            t0_signal = "无(非交易时段)"
        elif in_auction:
            t0_signal = "无(集合竞价时段)"
        elif pos.has_position:
            # T入: 5分钟RSI<45+5分钟MACD多头+量比温和
            if buy_score >= 2:
                if pos.can_t0_today(today_str, atr_pct):
                    t0_signal = f"买入做T5m({buy_score}项共振,RSI5m={rsi_5min},MACD5m={macd_5min_status},量比5m={vol_ratio_5min})"
                else:
                    t0_signal = f"买入做T5m({buy_score}项信号,今日做T次数已用尽({pos._get_daily_log(today_str).get('t0_count', 0)}/{pos.max_daily_t0(atr_pct)}))"
            # T出: 5分钟RSI>55+5分钟MACD空头+量比放大
            elif sell_score >= 2:
                if pos.can_t0_today(today_str, atr_pct):
                    profit_ok = pos.avg_cost > 0 and price > pos.avg_cost
                    if profit_ok:
                        t0_signal = f"卖出做T5m({sell_score}项共振,RSI5m={rsi_5min},MACD5m={macd_5min_status},量比5m={vol_ratio_5min})"
                    else:
                        t0_signal = f"卖出做T5m信号但价低于成本({pos.avg_cost:.3f}),不执行"
                else:
                    t0_signal = f"卖出做T5m({sell_score}项信号,今日做T次数已用尽({pos._get_daily_log(today_str).get('t0_count', 0)}/{pos.max_daily_t0(atr_pct)}))"
        elif buy_score >= 2 or sell_score >= 2:
            t0_signal = "有做T5m信号但无底仓"

        pos.prev_rsi = t["rsi"]
        # prev_macd_status只在日期变化时更新(同一天多次运行不覆盖)
        if pos.prev_macd_status is None:
            pos.prev_macd_status = t["macd_status"]

        # Level 3: 网格触达(仅用于已持仓标的的网格加仓，不触发建仓)
        grid_signal, grid_weight = evaluate_grid_signals(price, base, spacing, pos.grid_frozen)
        if grid_signal != "无" and "买入" in grid_signal and not pos.can_buy_today(today_str, atr_pct):
            grid_signal += "(今日已买入,等明日)"

        # ═══ 综合action判定 ═══
        action = "持有"
        # 计算实际持仓占比（基于总资产，用于无信号时的展示）
        _total_asset = pf_data.get("account", {}).get("total_asset", TOTAL_FUND) if pf_data else TOTAL_FUND
        if pos.has_position and _total_asset > 0:
            _mv = pos.shares * price
            position_ratio = f"{_mv / _total_asset * 100:.0f}%"
        else:
            position_ratio = "0%"
        reason = ""
        trade_type = None
        t0_pair = None  # 做T配对挂单

        # 提前计算持仓占比（用于仓位上限检查，防重复计算）
        _position_ratio_val = pos.shares * price / TOTAL_FUND if pos.has_position else 0
        _position_capped = pos.has_position and _position_ratio_val >= 0.50

        # 优先级1: 止盈止损（不受仓位上限约束，必须执行）
        if stop_actions:
            for sig_name, sig_type in stop_actions:
                if sig_type in ("liquidate_trend", "liquidate_trailing", "liquidate_30pct", "avg_stop_12pct", "hard_stop_20pct", "breakeven_stop"):
                    action = "清仓"
                    position_ratio = "100%"
                    reason = sig_name
                    trade_type = "liquidate"
                    pos.reset_on_liquidate(today_str)
                    break
                elif sig_type in ("reduce_30pct_all",):
                    action = "减仓"
                    position_ratio = "减30%总仓"
                    reason = sig_name
                    trade_type = "reduce"
                    break
                elif sig_type == "trend_profit_sell":
                    action = "卖出"
                    position_ratio = "10%(活动仓)"
                    reason = sig_name
                    trade_type = "sell"
                    break
                elif sig_type == "sell_active_5pct":
                    action = "卖出"
                    position_ratio = "5%(活动仓)"
                    reason = sig_name
                    trade_type = "sell"
                    break

        # 优先级2: 做T（仓位超限时禁止T入，允许T出）
        # 二次检查 can_t0_today，防止信号阶段通过后action阶段状态改变
        elif ((buy_score >= 2 or sell_score >= 2) and pos.has_position
              and pos.can_t0_today(today_str, atr_pct)):
            if "买入" in t0_signal:
                if _position_capped:
                    action = "持有(仓位已满)"
                    position_ratio = f"{_position_ratio_val*100:.0f}%"
                    reason = f"做T买入信号但仓位{_position_ratio_val*100:.0f}%已达50%上限,跳过"
                    trade_type = None
                else:
                    pos.record_t0(today_str)
                    action = "买入"
                    ratio = 0.30
                    position_ratio = f"{ratio*100:.0f}%"
                    reason = t0_signal + "(量比)"
                    trade_type = "t0"
                    # 配对卖出挂单: 高位卖出 (5min ATR×1.1)
                    atr_5m = t.get("atr_5min")
                    if atr_5m and price > 0:
                        pair_price = round(price + atr_5m * 1.1, 3)
                        pair_shares = int(pos.shares * 0.30 / 100) * 100
                        if pair_shares >= 100 and pair_price > 0:
                            # 价差校验: (卖出价-买入价) * 数量 > 10元
                            spread = (pair_price - price) * pair_shares
                            if spread > 10:
                                t0_pair = {
                                    "trade_type": "t0_pair",
                                    "action": "卖出(配对挂单)",
                                    "price": price,
                                    "pair_price": pair_price,
                                    "shares": pair_shares,
                                    "spread": round(spread, 2),
                                    "reason": f"做T买入后高位卖出: ATR_5min={atr_5m:.4f}, 挂单价={pair_price:.3f}",
                                }
                            else:
                                t0_signal += f"(价差{spread:.0f}元≤10,放弃配对)"
            elif "卖出" in t0_signal:
                pos.record_t0(today_str)
                action = "卖出"
                ratio = 0.1 if "半" in t0_signal else 0.2
                position_ratio = f"{ratio*100:.0f}%"
                reason = t0_signal + "(量比)"
                trade_type = "t0"
                # 配对买入挂单: 低位吃回 (5min ATR×1.1)
                atr_5m = t.get("atr_5min")
                if atr_5m and price > 0:
                    pair_price = round(price - atr_5m * 1.1, 3)
                    # 卖出数量 = shares * 0.30
                    pair_shares = int(pos.shares * 0.30 / 100) * 100
                    if pair_shares >= 100 and pair_price > 0:
                        # 价差校验: (现价-买入价) * 数量 > 10元
                        spread = (price - pair_price) * pair_shares
                        if spread > 10:
                            t0_pair = {
                                "trade_type": "t0_pair",
                                "action": "买入(配对挂单)",
                                "price": price,
                                "pair_price": pair_price,
                                "shares": pair_shares,
                                "spread": round(spread, 2),
                                "reason": f"做T卖出后低位接回: ATR_5min={atr_5m:.4f}, 挂单价={pair_price:.3f}",
                            }
                        else:
                            t0_signal += f"(价差{spread:.0f}元≤10,放弃配对)"

        # 优先级3: 网格(仅持仓时生效，不触发建仓；仓位超限时禁止网格买入)
        elif grid_signal != "无" and "买入" in grid_signal and "等明日" not in grid_signal and not pos.grid_frozen and pos.has_position:
            if _position_capped:
                action = "持有(仓位已满)"
                position_ratio = f"{_position_ratio_val*100:.0f}%"
                reason = f"网格买入信号但仓位{_position_ratio_val*100:.0f}%已达50%上限,跳过"
                trade_type = None
            else:
                # 检查是否已触发过同一档位
                grid_key = grid_signal.split("(")[0] if "(" in grid_signal else grid_signal
                if pos.last_grid_trigger == grid_key:
                    action = "持有(等下一档)"
                    reason = f"网格{grid_key}今日已触发,等下一档"
                else:
                    pos.record_buy(today_str)
                    pos.last_grid_trigger = grid_key
                    action = "买入"
                    position_ratio = f"{grid_weight*100:.0f}%"
                    reason = grid_signal
                    trade_type = "buy"
                # 网格熔断标记
                if "熔断" in grid_signal:
                    pos.grid_frozen = True
        elif grid_signal != "无" and "卖出" in grid_signal:
            if pos.active_shares > 0:
                action = "卖出"
                position_ratio = "5%(活动仓)"
                reason = grid_signal
                trade_type = "sell"
            else:
                action = "持有"
                reason = grid_signal + "(活动仓为0,不卖底仓)"

        # 无信号: 建仓/补仓/持有逻辑（统一入口，action仍为"持有"时进入）
        if action == "持有":
            if not pos.has_position and not pos.is_in_cooldown(today_str):
                # P2: 市场环境过滤
                if defense_weak:
                    action = "持有(观望)"
                    position_ratio = "0%"
                    reason = "防御标的515080也弱势,全市场暂停建仓"
                else:
                    # P1: 分批建仓（放宽条件 + 三过滤器）
                    # 过滤器1: ATR异常(>50%跳过, 防除权)
                    if atr_pct > 0.50:
                        action = "持有(观望)"; position_ratio = "0%"
                        reason = f"ATR异常{atr_pct*100:.0f}%,疑似除权,暂停建仓"
                    # 过滤器2: 止盈期不新开(任何持仓盈利>30%时禁止新开)
                    elif any(p.has_position and p.base_price and r2["price"] > p.base_price * 1.30
                             for c2, p in positions.items() if c2 != code
                             for r2 in [realtime.get(c2, {})] if r2):
                        action = "持有(观望)"; position_ratio = "0%"
                        reason = "止盈期不新开(已有仓位触发硬止盈)"

                    else:
                        rsi_ok = t["rsi"] is not None and t["rsi"] > 40
                        rsi_minimal = t["rsi"] is not None and t["rsi"] > 30

                        if pos.build_phase == 0:
                            # 通道1: RSI抄底 (MACD金叉+RSI≤80+价>MA20) → 30%
                            if t["macd_status"] == "金叉" and (t["rsi"] is None or t["rsi"] <= 80) and t["ma20"] and price > t["ma20"] and pos.can_buy_today(today_str, atr_pct):
                                action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(RSI抄底)")
                                reason = f"RSI抄底:MACD金叉(RSI={t['rsi']})"
                            # 通道2: 趋势跟踪 (空仓>10天+价>MA20+MACD红柱) → 30%
                            elif pos.empty_days > 10 and t["macd_status"] in ["红柱放大", "红柱缩短"] and t["ma20"] and price > t["ma20"] and pos.can_buy_today(today_str, atr_pct):
                                action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(趋势跟踪)")
                                reason = f"趋势跟踪:价>MA20+{t['macd_status']}"
                            # 通道3: 突破入场 (20日新高+MACD金叉+价>MA20) → 30%
                            elif t["macd_status"] == "金叉" and t["ma20"] and price > t["ma20"] and pos.can_buy_today(today_str, atr_pct):
                                # 检查20日新高(不含当日)
                                if len(all_klines.get(code, [])) >= 21:
                                    # 不含当日: 用倒数第2到第22根(排除最后一根=当日)
                                    closes_20 = [k["close"] for k in all_klines[code][-21:-1]]
                                    if closes_20 and price > max(closes_20):
                                        action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(突破入场)")
                                        reason = f"突破入场:20日新高+金叉"
                            # 通道4: 分批建仓 (金叉/红柱放大+RSI>40+站MA5+价>MA20) → 30%
                            if action == "持有" and t["macd_status"] in ["金叉", "红柱放大"] and rsi_ok and price > t["ma5"] and t["ma20"] and price > t["ma20"] and pos.can_buy_today(today_str, atr_pct):
                                action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(分批1)")
                                reason = "分批建仓1:首笔30%试探(MACD健康+RSI满足+站上MA5)"
                            # 通道5: Test抄底 (RSI<35+绿柱缩短+站MA5+价>MA20+前一日也是绿柱缩短) → 30%
                            if action == "持有" and t["rsi"] is not None and t["rsi"] < 35 and t["macd_status"] == "绿柱缩短" and t["ma5"] and price > t["ma5"] and t["ma20"] and price > t["ma20"] and pos.prev_macd_status == "绿柱缩短" and pos.can_buy_today(today_str, atr_pct):
                                action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(Test抄底)")
                                reason = f"Test抄底:RSI={t['rsi']:.1f}+绿柱缩短+站MA5"
                            # 通道6: 试探建仓 (绿柱缩短/震荡+RSI>40+站MA5+价>MA20) → 15%
                            if action == "持有" and t["macd_status"] in ["绿柱缩短", "震荡"] and rsi_minimal and price > t["ma5"] and t["ma20"] and price > t["ma20"] and pos.can_buy_today(today_str, atr_pct):
                                action, position_ratio, trade_type = pos._enter_position(today_str, price, "15%(试探)")
                                reason = f"试探建仓:15%底仓(MACD{t['macd_status']}+RSI{t['rsi']}+站上MA5)"
                            if action == "持有":
                                action = "持有(观望)"
                                position_ratio = "0%"
                                reason = f"建仓条件未满足(MACD{t['macd_status']},RSI{t['rsi']})"

                        elif pos.build_phase == 1:
                            # ── 逆势补仓: 跌超3%允许第二次买入（需检查仓位上限）──
                            matched = False
                            if pos.build_first_price > 0 and pos.add_count < 5:
                                dip_pct = (price - pos.build_first_price) / pos.build_first_price
                                if dip_pct < -0.03 and pos.can_buy_today(today_str, atr_pct) and t["macd_status"] not in ["死叉", "绿柱放大"] and (t["rsi"] is None or t["rsi"] < 40):
                                    if _position_capped:
                                        action = "持有(仓位已满)"
                                        position_ratio = f"{_position_ratio_val*100:.0f}%"
                                        reason = f"补仓信号但仓位{_position_ratio_val*100:.0f}%已达50%上限,跳过"
                                        trade_type = None
                                    else:
                                        pos.record_buy(today_str)
                                        pos.add_count += 1
                                        action = "买入"
                                        position_ratio = "15%(补仓)"
                                        reason = f"逆势补仓:跌{dip_pct*100:.0f}%加仓15%(MACD{t['macd_status']})"
                                        trade_type = "buy"
                                    matched = True
                            # ── 确认加仓: 突破首笔价或回踩MA5站稳（需检查仓位上限）──
                            if not matched and (price > pos.build_first_price or pos.ma5_touch_count >= 2):
                                # AO动量过滤: 加仓确认需要AO上升
                                ao_now = t.get("ao_now")
                                ao_5ago = t.get("ao_5ago")
                                ao_rising = ao_now is not None and ao_5ago is not None and ao_now > ao_5ago
                                if not ao_rising:
                                    action = "持有(等确认)"
                                    reason = f"分批建仓1已入场,AO未回升(now={ao_now:.4f},5ago={ao_5ago:.4f}),跳过加仓"
                                elif _position_capped:
                                    action = "持有(仓位已满)"
                                    position_ratio = f"{_position_ratio_val*100:.0f}%"
                                    reason = f"确认加仓信号但仓位{_position_ratio_val*100:.0f}%已达50%上限,跳过"
                                    trade_type = None
                                elif pos.can_buy_today(today_str, atr_pct):
                                    pos.record_buy(today_str)
                                    pos.build_phase = 2
                                    # 确认加仓后重置止盈止损基准
                                    pos.base_price = pos.avg_cost
                                    pos.peak_price = price
                                    pos.build_first_price = price
                                    pos.reached_8pct = False
                                    pos.reached_15pct = False
                                    pos.trailing_stop_price = 0.0
                                    pos.add_count += 1
                                    action = "买入"
                                    position_ratio = "70%(分批2)"
                                    reason = f"分批建仓2:确认加70%(突破首笔价{pos.build_first_price:.3f}或回踩MA5站稳)"
                                    trade_type = "buy"
                                else:
                                    action = "持有(等确认)"
                                    reason = "建仓确认信号出现但今日已买入,等明日"
                            else:
                                if price > t["ma5"]:
                                    pos.ma5_touch_count += 1
                                else:
                                    pos.ma5_touch_count = 0
                                action = "持有(等确认)"
                                reason = f"分批建仓1已入场,等回踩MA5站稳(站{pos.ma5_touch_count}根)或突破首笔价{pos.build_first_price:.3f}"
                            # 如果 action 仍然为 "持有"（即补仓/确认加仓都没匹配到），重置 build_phase
                            # 这覆盖了 stale build_phase=1 但实际无持仓且无有效信号的情况
                            if action == "持有":
                                pos.build_phase = 0
                                pos.build_first_price = 0.0
                                pos.add_count = 0
                                pos.ma5_touch_count = 0
                                reason = "建仓信号不匹配,重置建仓状态"

            elif pos.has_position:
                # 持有逻辑（无止损/做T/网格信号时的 fallback）
                if pos.grid_frozen:
                    reason = "网格冻结(跌破第5档),等站上MA5解冻"
                elif _position_capped:
                    action = "持有(仓位已满)"
                    reason = f"当前仓位{_position_ratio_val*100:.0f}%已达目标50%"
                elif price > t["ma5"] and t["rsi"] and t["rsi"] > 50:
                    reason = "趋势偏强,继续持有"
                elif price < t["ma5"]:
                    reason = "破MA5但未触发分级止损,继续持有"
                else:
                    reason = "震荡区间,继续持有"
            else:
                action = "持有(冷静期)"
                reason = "连续清仓后进入冷静期,暂停建仓"

        # 附加标注
        vol_label = ""
        if t.get("vol_ratio_5min") is not None:
            vol_label = f"5分钟量比{t['vol_ratio_5min']}"
        elif t.get("vol_ratio_120") is not None:
            vol_label = f"120分钟量比{t['vol_ratio_120']}(降级)"

        # empty_days递增: 空仓且非建仓期(按天去重,同一天多次运行只算1次)
        if not pos.has_position and pos.build_phase == 0:
            if pos.empty_days_date != today_str:
                pos.empty_days += 1
                pos.empty_days_date = today_str

        signals_output[code] = {
            "price": f"{price:.3f}",
            "rsi": t["rsi"],
            "macd_status": t["macd_status"],
            "atr": t["atr"],
            "atr_pct": round(atr_pct * 100, 2),
            "dynamic_spacing": t["spacing"],
            "vol_ratio_5min": t.get("vol_ratio_5min"),
            "vol_ratio_120": t.get("vol_ratio_120"),
            "vol_label": vol_label,
            "action": action,
            "grid_signal": grid_signal,
            "t0_signal": t0_signal,
            "stop_signal": stop_signal,
            "position_ratio": position_ratio,
            "reason": reason,
            "trade_type": trade_type,
            "stop_level": pos.stop_level,
            "trailing_stop": round(pos.trailing_stop_price, 3) if pos.trailing_stop_price else None,
            "build_phase": pos.build_phase,
            "grid_frozen": pos.grid_frozen,
            "last_grid_trigger": pos.last_grid_trigger,
            "dead_active_split": f"底仓{DEAD_RATIO*100:.0f}%/活动仓{ACTIVE_RATIO*100:.0f}%",
            "daily_trade_count": pos.daily_trade_log.get(today_str, {"buy_count": 0, "t0_count": 0}),
            "market_weak": defense_weak if code != DEFENSE_CODE else False,
            "t0_pair": t0_pair,
        }

    # 可读汇总(用trade_type精确匹配, 不依赖中文字符串)
    liquid_s = [c for c, s in signals_output.items() if s.get("trade_type") == "liquidate"]
    reduce_s = [c for c, s in signals_output.items() if s.get("trade_type") == "reduce"]
    sell_s = [c for c, s in signals_output.items() if s.get("trade_type") == "sell" or (s.get("trade_type") == "t0" and "卖出" in s.get("action", ""))]
    buy_s = [c for c, s in signals_output.items() if s.get("trade_type") == "buy" or (s.get("trade_type") == "t0" and "买入" in s.get("action", ""))]
    hold_s = [c for c, s in signals_output.items() if s.get("trade_type") is None]

    parts = []
    if liquid_s: parts.append(f"紧急清仓: {','.join(liquid_s)}")
    for c in reduce_s: parts.append(f"减仓{c}({signals_output[c]['reason']})")
    for c in sell_s: parts.append(f"卖出{c}({signals_output[c]['reason'][:30]})")
    for c in buy_s: parts.append(f"买入{c}({signals_output[c]['reason'][:30]})")
    for c in hold_s: parts.append(f"{c}{signals_output[c]['reason'][:20]}")
    # 做T配对挂单
    for c, s in signals_output.items():
        if s.get("t0_pair"):
            p = s["t0_pair"]
            parts.append(f"配对挂单{c}:{p['action']}@{p['price']:.3f}({p['shares']}股)")

    human_readable = "。".join(parts) if parts else "所有标的均无操作信号,继续观望。"

    result = {
        "version": "v3.1",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "signals": signals_output,
        "market_environment": "全市场弱势(515080也破MA20)" if defense_weak else "正常",
        "human_readable": human_readable,
        "strategy_policy": {
            "stop_loss": "均价止损12%+硬止损20%+分级减仓(破MA20→30%, DIF<0→30%, 趋势恶化→清仓)",
            "profit_take": "移动止盈(8%后成本+5%, 15%后峰值回撤5%)+硬止盈30%→清仓",
            "t0_signal": "弹性做T(5分钟分时:RSI5m<45买入+MACD5m金叉/红柱+量比<1.5; RSI5m>55卖出+MACD5m死叉/绿柱+量比>1.2,配对ATR×1.1,30%数量,11点后运行)",
            "entry": "5通道(RSI抄底/分批建仓/Test抄底/试探/补仓)统一30%→确认70%→补仓15%",
            "frequency": "正常1买2T/天, ATR>5%时2买3T/天, 11:00后只做T不买入",
        },
    }

    grid_tables = {}
    for code in ETFS:
        grid_tables[code] = compute_grid_table(base_prices[code], all_tech[code]["spacing"], ETFS[code]["fund"])
    result["grid_tables"] = grid_tables

    # 持久化信号状态到 portfolio.json
    if pf_path and pf_data is not None:
        try:
            state_to_save = {}
            for code, pos in positions.items():
                # 收盘后更新prev_macd_status供次日使用
                if code in all_tech:
                    pos.prev_macd_status = all_tech[code]["macd_status"]
                state_to_save[code] = pos.to_dict(today_str)
            pf_data["_signal_state"] = state_to_save
            # 更新 account 字段
            mv = sum(p.get("market_value", 0) for p in pf_data.get("positions", {}).values())
            cash = pf_data.get("account", {}).get("cash", 0) or 0
            pf_data["account"] = {
                "total_asset": round(mv + cash, 2),
                "cash": cash,
                "market_value": round(mv, 2),
            }
            pf_data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(pf_path, "w") as _f:
                json.dump(pf_data, _f, ensure_ascii=False, indent=2)
            # 同步到~/.hermes/scripts/ (已废弃，项目已迁移至 ~/Documents/code/quant/)
        except Exception:
            pass

    return result


# ============================================================
# 六、输出
# ============================================================

def _parse_position_ratio(ratio_str):
    """从 position_ratio 字符串中提取比例(0-1浮点数)
    例: "30%(RSI抄底)" → 0.30, "5%(活动仓)" → 0.05, "15%(补仓)" → 0.15
    """
    if not ratio_str:
        return 0.0
    try:
        pct_str = ratio_str.split("%")[0]
        return float(pct_str) / 100.0
    except (ValueError, IndexError):
        return 0.0


def _get_position_shares(code):
    """从 portfolio.json 读取当前持仓股数"""
    pf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")
    try:
        if os.path.exists(pf_path):
            with open(pf_path, "r") as f:
                pf = json.load(f)
            return pf.get("positions", {}).get(code, {}).get("shares", 0)
    except Exception:
        pass
    return 0


# ============================================================
# 六、执行层 — 持久化 + 异常处理 + 告警推送
# ============================================================

LAST_EXECUTED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_executed.json")
TRADE_ERRORS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".trade_errors.json")


def _load_last_executed():
    """加载上次执行记录，返回 {code: {action, shares, price, time_window, timestamp}}"""
    if os.path.exists(LAST_EXECUTED_PATH):
        try:
            with open(LAST_EXECUTED_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _trade_type_to_mode(action):
    """将 action 字段映射为 mode: 'buy_sell' | 't0'
    action 是 trade_type 值: 'buy'/'sell'/'liquidate'/'reduce' → 'buy_sell'; 't0' → 't0'
    """
    if action == "t0":
        return "t0"
    return "buy_sell"


def _save_last_executed(code, action, shares, price, direction, before_shares, after_shares, pending=False, mode=None):
    """保存执行记录到 .last_executed.json
    pending=True:  订单已挂但未成交（挂单中）
    pending=False: 订单已成交（默认）
    mode: 'buy_sell' | 't0' — 区分建仓信号和做T信号，互不干扰过滤
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    last = _load_last_executed()
    last[code] = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "code": code,
        "action": action,
        "shares": shares,
        "price": price,
        "direction": direction,
        "time_window": today_str,
        "before_shares": before_shares,
        "after_shares": after_shares,
        "pending": pending,
        "mode": mode or _trade_type_to_mode(action),
    }
    try:
        with open(LAST_EXECUTED_PATH, "w") as f:
            json.dump(last, f, ensure_ascii=False, indent=2)
    except IOError:
        pass


def _save_trade_error(code, action, price, shares, error_msg, retry_cmd):
    """记录交易错误到 .trade_errors.json"""
    errors = []
    if os.path.exists(TRADE_ERRORS_PATH):
        try:
            with open(TRADE_ERRORS_PATH, "r") as f:
                errors = json.load(f)
        except (json.JSONDecodeError, IOError):
            errors = []
    if not isinstance(errors, list):
        errors = []

    errors.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "code": code,
        "action": action,
        "price": price,
        "shares": shares,
        "error": error_msg,
        "retry_cmd": retry_cmd,
    })
    try:
        with open(TRADE_ERRORS_PATH, "w") as f:
            json.dump(errors, f, ensure_ascii=False, indent=2)
    except IOError:
        pass


def _check_pending_orders(mode=None):
    """检查 .last_executed.json 中今日的挂单状态
    mode: 'buy_sell' | 't0' | None=全部 — 按模式过滤成交记录
    返回: (pending_map, filled_codes)
      pending_map: {code: direction} 今日未成交的挂单
      filled_codes: set of codes 今日已成交
    """
    last = _load_last_executed()
    today_str = datetime.now().strftime("%Y-%m-%d")
    pending_map = {}
    filled_codes = set()

    for code, entry in last.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("time_window") != today_str:
            continue
        # 模式过滤：只看指定 mode 的成交记录
        # 兼容旧记录（无 mode 字段）：从 action 推导 mode
        entry_mode = entry.get("mode") or _trade_type_to_mode(entry.get("action", ""))
        if mode and entry_mode != mode:
            continue
        if entry.get("pending", False):
            # 挂单中（未成交）
            pending_map[code] = entry.get("direction", "")
        else:
            # 已成交
            filled_codes.add(code)

    return pending_map, filled_codes


def _clear_pending_order(code):
    """清除指定 code 的挂单记录（撤单后调用）"""
    last = _load_last_executed()
    if code in last:
        del last[code]
        try:
            with open(LAST_EXECUTED_PATH, "w") as f:
                json.dump(last, f, ensure_ascii=False, indent=2)
        except IOError:
            pass


def _is_duplicate_signal(code, direction, today_str, mode=None):
    """检查是否为同方向重复信号：同代码+同方向+同时间窗口+同模式。
    mode: 'buy_sell' | 't0' | None — 只检查同 mode 的记录
    返回 True 表示同方向旧单仍在，需要先撤旧单再挂新单（不再直接跳过）。"""
    last = _load_last_executed()
    entry = last.get(code, {})
    if not entry or not isinstance(entry, dict):
        return False
    # 模式过滤：旧单 mode 必须与当前 mode 一致
    # 兼容旧记录（无 mode 字段）：从 action 推导 mode
    entry_mode = entry.get("mode") or _trade_type_to_mode(entry.get("action", ""))
    if mode and entry_mode != mode:
        return False
    return (entry.get("direction") == direction
            and entry.get("time_window") == today_str)


def _detect_error_type(stdout, stderr, returncode, exit_reason=None):
    """从 subprocess 输出中检测错误类型。
    返回 (error_type, detail)
    error_type: 'tonghuashun_disconnect' | 'not_filled' | 'timeout' | 'other'
    """
    combined = (stdout or "") + "\n" + (stderr or "")

    if exit_reason == "timeout":
        return "timeout", "下单超时(120s)"

    # 同花顺断连特征
    if "连接同花顺失败" in combined or "EvolvingSim 连接失败" in combined or "同花顺" in combined:
        return "tonghuashun_disconnect", "同花顺断连"

    # 下单成功但持仓未变 → 未成交
    if "下单成功但持仓未变" in combined or "期望增量" in combined:
        return "not_filled", "下单未成交(增量确认失败)"

    if returncode != 0:
        return "other", f"退出码={returncode}"

    return None, None


def _signal_direction(sig):
    """从信号判定交易方向：'买入' | '卖出'"""
    trade_type = sig.get("trade_type", "")
    action = sig.get("action", "")

    if trade_type in ("buy", "t0"):
        return "买入" if "买入" in action else "卖出"
    if trade_type in ("sell", "liquidate", "reduce"):
        return "卖出"
    return None


def _build_retry_cmd(trade_script, code, trade_type, sig, price, shares):
    """构建重试命令字符串"""
    if trade_type in ("buy", "sell", "liquidate", "reduce"):
        return f"python3 trade.py {trade_type if trade_type in ('buy','sell') else 'sell'} {code} {shares} {price:.3f}"
    elif trade_type == "t0":
        t0_pair = sig.get("t0_pair", {})
        pair_price = t0_pair.get("pair_price", 0)
        pair_shares = t0_pair.get("shares", 0)
        if "买入" in sig.get("action", ""):
            return f"python3 trade.py t0_buy {code} {pair_shares} {price:.3f} {pair_price:.3f}"
        else:
            return f"python3 trade.py t0_sell {code} {pair_shares} {price:.3f} {pair_price:.3f}"
    return ""


def execute_signals(result, mode=None):
    """遍历信号，调用 trade.py 执行交易（含异常处理+状态持久化+告警推送）

    mode: None=全部, 'buy_sell'=只执行买入/卖出/止盈止损/网格, 't0'=只执行做T

    --execute 下单逻辑（v3.1.2）：
    - 买入限价 = min(信号价, 当前实时价)，确保 ≤ 卖一价能挂进去
    - 卖出限价 = max(信号价, 当前实时价)，确保 ≥ 买一价能挂进去
    - 做T配对价 = 当前实时价 ± ATR×1.1（重新计算，不跟信号价比较）
    """
    import subprocess

    if result is None:
        print("[EXECUTE] 无信号结果，跳过执行")
        return

    signals = result.get("signals", {})
    if not signals:
        print("[EXECUTE] 无信号数据，跳过执行")
        return

    if mode:
        print(f"[EXECUTE] 模式过滤: {mode}")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    trade_script = os.path.join(script_dir, "trade.py")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # ═══ 挂单检查：检查 .last_executed.json 中今日未成交的挂单 ═══
    pending_map, filled_codes = _check_pending_orders(mode=mode)
    # 标记今日已成交的 code → 跳过不重复挂
    if pending_map:
        print(f"[EXECUTE] 发现 {len(pending_map)} 只ETF有未成交挂单: {dict(pending_map)}")
    if filled_codes:
        print(f"[EXECUTE] 发现 {len(filled_codes)} 只ETF今日已成交: {filled_codes}")

    # ═══ 获取当前实时价（用于 --execute 下单） ═══
    live_quotes = {}
    try:
        live_quotes = fetch_realtime_quotes()
        print(f"[EXECUTE] 获取实时行情: {len(live_quotes)} 只ETF")
    except Exception as e:
        print(f"[EXECUTE] ⚠️ 获取实时行情失败: {e}，将使用信号价作为兜底")

    # 按模式过滤信号
    BUY_SELL_TYPES = {"buy", "sell", "liquidate", "reduce"}  # 买入/卖出/止盈止损/网格
    T0_TYPES = {"t0"}  # 做T

    actionable = []
    for code, sig in signals.items():
        trade_type = sig.get("trade_type")
        if not trade_type:
            continue
        # 模式过滤
        if mode == "buy_sell" and trade_type not in BUY_SELL_TYPES:
            continue
        if mode == "t0" and trade_type not in T0_TYPES:
            continue
        actionable.append((code, sig))

    if not actionable:
        print("[EXECUTE] 无交易信号，跳过执行")
        return

    print(f"[EXECUTE] 共 {len(actionable)} 个交易信号，开始执行...")
    print("=" * 60)

    success_count = 0
    fail_count = 0
    skip_count = 0

    for i, (code, sig) in enumerate(actionable):
        trade_type = sig["trade_type"]
        action = sig.get("action", "")

        # ═══ 挂单检查：旧单已成交 → 跳过不重复挂 ═══
        if code in filled_codes:
            direction = _signal_direction(sig)
            print(f"\n[{i+1}/{len(actionable)}] ⏭️  {code} {direction} 今日已成交，跳过不重复挂")
            skip_count += 1
            continue

        # ═══ 获取当前实时价 ═══
        signal_price_str = sig.get("price", "0")
        try:
            signal_price = float(signal_price_str)
        except (ValueError, TypeError):
            signal_price = 0.0

        live = live_quotes.get(code, {})
        live_price = live.get("price", 0.0) if live else 0.0

        # ═══ 下单限价方向修正 ═══
        # 买入方向（buy/t0买入）：限价 = min(信号价, 当前价)，确保 ≤ 卖一价能挂进去
        # 卖出方向（sell/liquidate/reduce/t0卖出）：限价 = max(信号价, 当前价)，确保 ≥ 买一价能挂进去
        is_buy_direction = (
            trade_type == "buy"
            or (trade_type == "t0" and "买入" in action)
        )
        is_sell_direction = (
            trade_type in ("sell", "liquidate", "reduce")
            or (trade_type == "t0" and "卖出" in action)
        )

        if live_price > 0:
            if is_buy_direction:
                # 买入限价 = min(信号价, 当前价)，如果信号价高于当前价说明行情涨了，用当前价挂
                if signal_price > 0 and live_price < signal_price:
                    price = live_price
                    print(f"[EXECUTE] {code} 买入限价修正: 信号价={signal_price:.3f} > 当前价={live_price:.3f}, 使用当前价={price:.3f}")
                else:
                    price = signal_price if signal_price > 0 else live_price
                    print(f"[EXECUTE] {code} 信号价={signal_price:.3f}, 当前价={live_price:.3f}, 买入限价={price:.3f}")
            elif is_sell_direction:
                # 卖出限价 = max(信号价, 当前价)，如果信号价低于当前价说明行情跌了，用当前价挂
                if signal_price > 0 and live_price > signal_price:
                    price = live_price
                    print(f"[EXECUTE] {code} 卖出限价修正: 信号价={signal_price:.3f} < 当前价={live_price:.3f}, 使用当前价={price:.3f}")
                else:
                    price = signal_price if signal_price > 0 else live_price
                    print(f"[EXECUTE] {code} 信号价={signal_price:.3f}, 当前价={live_price:.3f}, 卖出限价={price:.3f}")
            else:
                # 未知方向，保守用信号价
                price = signal_price if signal_price > 0 else live_price
                print(f"[EXECUTE] {code} 未知方向(trade_type={trade_type}), 信号价={signal_price:.3f}, 当前价={live_price:.3f}, 兜底限价={price:.3f}")
        else:
            # 兜底：实时价获取失败，用信号价
            price = signal_price
            print(f"[EXECUTE] {code} 实时价获取失败，兜底用信号价={signal_price:.3f}")

        if price <= 0:
            print(f"[EXECUTE] ⚠️ {code} 价格无效(信号={signal_price_str}, 实时={live_price})，跳过")
            fail_count += 1
            continue

        t0_pair = sig.get("t0_pair")

        # ═══ 信号解析 + 构建命令 ═══
        cmd = None
        label = None
        shares = 0

        # ── 做T信号 ──
        if trade_type == "t0" and t0_pair:
            pair_shares = t0_pair.get("shares", 0)
            # 配对价重新计算: 当前实时价 ± ATR×1.1（不跟信号价比较）
            sig_atr = sig.get("atr", 0)
            try:
                sig_atr = float(sig_atr)
            except (ValueError, TypeError):
                sig_atr = 0.0
            old_pair_price = t0_pair.get("pair_price", 0)

            if "买入" in action:
                # 做T买入: 配对卖出挂单 = 当前价 + ATR×1.1
                pair_price = round(price + sig_atr * 1.1, 3) if sig_atr > 0 else old_pair_price
            elif "卖出" in action:
                # 做T卖出: 配对买入挂单 = 当前价 - ATR×1.1
                pair_price = round(price - sig_atr * 1.1, 3) if sig_atr > 0 else old_pair_price
            else:
                pair_price = old_pair_price

            if sig_atr > 0:
                print(f"[EXECUTE] {code} 做T配对价重算: 信号价={old_pair_price}, 当前价={price:.3f}, ATR={sig_atr:.4f}, 新配对价={pair_price:.3f}")
            shares = pair_shares

            if pair_shares < 100:
                print(f"[EXECUTE] ⚠️ {code} 做T股数不足({pair_shares})，跳过")
                fail_count += 1
                continue

            if "买入" in action:
                cmd = ["python3", trade_script, "t0_buy",
                       code, str(pair_shares), f"{price:.3f}", f"{pair_price:.3f}"]
                label = f"做T买入 {code} {pair_shares}股 @{price:.3f} (配对@{pair_price:.3f})"
            elif "卖出" in action:
                cmd = ["python3", trade_script, "t0_sell",
                       code, str(pair_shares), f"{price:.3f}", f"{pair_price:.3f}"]
                label = f"做T卖出 {code} {pair_shares}股 @{price:.3f} (配对@{pair_price:.3f})"
            else:
                print(f"[EXECUTE] ⚠️ {code} 做T方向未知(action={action})，跳过")
                fail_count += 1
                continue

        # ── 普通买入 ──
        elif trade_type == "buy":
            ratio = _parse_position_ratio(sig.get("position_ratio", ""))
            fund = ETFS.get(code, {}).get("fund", 44000)
            shares = int(fund * ratio / price / 100) * 100
            if shares < 100:
                print(f"[EXECUTE] ⚠️ {code} 买入股数不足(fund={fund}, ratio={ratio:.0%}, shares={shares})，跳过")
                fail_count += 1
                continue
            cmd = ["python3", trade_script, "buy", code, str(shares), f"{price:.3f}"]
            label = f"买入 {code} {shares}股 @{price:.3f}"

        # ── 普通卖出 ──
        elif trade_type == "sell":
            current_shares = _get_position_shares(code)
            ratio = _parse_position_ratio(sig.get("position_ratio", ""))
            if ratio > 0:
                shares = int(current_shares * ratio / 100) * 100
            else:
                shares = current_shares
            if shares < 100 or shares > current_shares:
                shares = current_shares
            if shares < 100:
                print(f"[EXECUTE] ⚠️ {code} 卖出股数不足(current={current_shares}, shares={shares})，跳过")
                fail_count += 1
                continue
            cmd = ["python3", trade_script, "sell", code, str(shares), f"{price:.3f}"]
            label = f"卖出 {code} {shares}股 @{price:.3f}"

        # ── 清仓 ──
        elif trade_type == "liquidate":
            current_shares = _get_position_shares(code)
            shares = current_shares
            if current_shares < 100:
                print(f"[EXECUTE] ⚠️ {code} 清仓但无持仓({current_shares}股)，跳过")
                fail_count += 1
                continue
            cmd = ["python3", trade_script, "sell", code, str(current_shares), f"{price:.3f}"]
            label = f"清仓 {code} {current_shares}股 @{price:.3f}"

        # ── 减仓 ──
        elif trade_type == "reduce":
            current_shares = _get_position_shares(code)
            ratio = _parse_position_ratio(sig.get("position_ratio", ""))
            if ratio > 0:
                shares = int(current_shares * ratio / 100) * 100
            else:
                shares = int(current_shares * 0.30 / 100) * 100
            if shares < 100 or shares > current_shares:
                shares = min(current_shares, max(shares, 100))
            if shares < 100 or current_shares < 100:
                print(f"[EXECUTE] ⚠️ {code} 减仓股数不足(current={current_shares}, shares={shares})，跳过")
                fail_count += 1
                continue
            cmd = ["python3", trade_script, "sell", code, str(shares), f"{price:.3f}"]
            label = f"减仓 {code} {shares}股 @{price:.3f}"

        else:
            print(f"[EXECUTE] ⚠️ {code} 未知交易类型({trade_type})，跳过")
            fail_count += 1
            continue

        # ═══ 执行前检查：未成交挂单或同方向重复信号 → 先撤旧单再挂新单 ═══
        direction = _signal_direction(sig)

        # 检查1: 来自上次执行未成交的挂单（pending_map）
        need_revoke = False
        if code in pending_map and direction:
            pending_dir = pending_map[code]
            if pending_dir == direction:
                # 同方向有未成交挂单 → 撤旧挂新
                print(f"\n[{i+1}/{len(actionable)}] 🔄 {code} 上次未成交挂单({pending_dir})，先撤旧单再挂新单...")
                need_revoke = True
            else:
                # 不同方向有未成交挂单 → 也要撤（避免冲突），撤旧方向
                print(f"\n[{i+1}/{len(actionable)}] 🔄 {code} 上次未成交挂单({pending_dir})方向与新信号({direction})不同，撤旧单后挂新单...")
                need_revoke = True

        # 检查2: 同方向重复信号（已成交但同天又来信号）
        if direction and _is_duplicate_signal(code, direction, today_str, mode=mode):
            need_revoke = True
            print(f"\n[{i+1}/{len(actionable)}] 🔄 {code} 同方向重复信号，先撤旧单再挂新单...")

        if need_revoke:
            revoke_dir = direction
            if code in pending_map and pending_map.get(code) != direction:
                revoke_dir = pending_map[code]  # 撤旧方向
            revoke_cmd = ["python3", trade_script, "revoke", code, revoke_dir]
            print(f"  → 撤单: {' '.join(revoke_cmd)}")
            try:
                revoke_proc = subprocess.run(
                    revoke_cmd, capture_output=True, text=True,
                    timeout=60, cwd=script_dir
                )
                if revoke_proc.stdout:
                    print(revoke_proc.stdout.strip())
                if revoke_proc.returncode == 0:
                    print(f"  ✅ {code} 同方向旧单已撤，准备挂新单")
                    # 清除挂单记录
                    _clear_pending_order(code)
                else:
                    print(f"  ⚠️ 撤单返回非0 (exit={revoke_proc.returncode})，继续尝试挂新单")
                    if revoke_proc.stderr:
                        print(f"  [stderr] {revoke_proc.stderr.strip()}")
            except subprocess.TimeoutExpired:
                print(f"  ⚠️ 撤单超时(60s)，继续尝试挂新单")
            except Exception as e:
                print(f"  ⚠️ 撤单异常: {e}，继续尝试挂新单")
            # 不 continue，继续往下走到执行命令，挂新单

        # ═══ 执行命令（含重试） ═══
        print(f"\n[{i+1}/{len(actionable)}] {label}")
        print(f"  → {' '.join(cmd)}")

        retry_cmd = _build_retry_cmd(trade_script, code, trade_type, sig, price, shares)
        max_retries = 3
        success = False
        final_error = None
        final_error_type = None
        before_shares = 0
        after_shares = 0

        for attempt in range(1, max_retries + 1):
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=120, cwd=script_dir
                )
                stdout = proc.stdout or ""
                stderr_out = proc.stderr or ""

                # 检测错误类型
                err_type, err_detail = _detect_error_type(stdout, stderr_out, proc.returncode)

                if err_type is None and proc.returncode == 0:
                    # 成功
                    if stdout:
                        # 解析持仓变化（从输出中提取 before→after）
                        for line in stdout.strip().split("\n"):
                            if "持仓从" in line and "→" in line:
                                try:
                                    parts = line.split("持仓从")[1].split("→")
                                    before_shares = int(parts[0].strip())
                                    after_shares = int(parts[1].split("股")[0].strip())
                                except (ValueError, IndexError):
                                    pass
                                break
                        print(stdout.strip())
                    if stderr_out:
                        print(f"  [stderr] {stderr_out.strip()}")
                    print(f"  ✅ {code} {direction} {shares}股@{price:.3f} → 持仓{before_shares}→{after_shares}股")
                    _save_last_executed(code, trade_type, shares, price, direction, before_shares, after_shares, mode=mode)
                    success_count += 1
                    success = True
                    break

                elif err_type == "tonghuashun_disconnect":
                    # 同花顺断连 → 重试+窗口激活
                    if attempt < max_retries:
                        print(f"  ⚠️ 同花顺断连，激活窗口后重试 ({attempt}/{max_retries})...")
                        os.system("osascript -e 'tell application \"同花顺\" to activate'")
                        time.sleep(2)
                    else:
                        # 3次重试全失败 → 告警
                        if stdout:
                            print(stdout.strip())
                        if stderr_out:
                            print(f"  [stderr] {stderr_out.strip()}")
                        alert_msg = f"[ALERT] ❌ {code} {direction}失败: 同花顺断连(3次重试全失败)。重试: {retry_cmd}"
                        print(alert_msg, file=sys.stderr)
                        _save_trade_error(code, trade_type, price, shares, "同花顺断连(3次重试全失败)", retry_cmd)
                        final_error = err_detail
                        final_error_type = err_type

                elif err_type == "not_filled":
                    # 下单未成交 → 不重试，直接告警，记录挂单状态
                    if stdout:
                        print(stdout.strip())
                    if stderr_out:
                        print(f"  [stderr] {stderr_out.strip()}")
                    alert_msg = f"[ALERT] ❌ {code} {direction}失败: 下单未成交(增量确认失败)。重试: {retry_cmd}"
                    print(alert_msg, file=sys.stderr)
                    _save_trade_error(code, trade_type, price, shares, "下单未成交(增量确认失败)", retry_cmd)
                    _save_last_executed(code, trade_type, shares, price, direction, before_shares, after_shares, pending=True, mode=mode)
                    final_error = err_detail
                    final_error_type = err_type
                    break

                else:
                    # 其他错误
                    if stdout:
                        print(stdout.strip())
                    if stderr_out:
                        print(f"  [stderr] {stderr_out.strip()}")
                    print(f"  ❌ 失败 (exit={proc.returncode})")
                    final_error = err_detail or f"退出码={proc.returncode}"
                    final_error_type = err_type or "other"
                    break

            except subprocess.TimeoutExpired:
                if attempt < max_retries:
                    print(f"  ⚠️ 超时，重试 ({attempt}/{max_retries})...")
                else:
                    alert_msg = f"[ALERT] ❌ {code} {direction}失败: 下单超时(120s×3)。重试: {retry_cmd}"
                    print(alert_msg, file=sys.stderr)
                    _save_trade_error(code, trade_type, price, shares, "下单超时(120s×3)", retry_cmd)
                    final_error = "超时"
                    final_error_type = "timeout"

            except Exception as e:
                print(f"  ❌ 异常: {e}")
                final_error = str(e)
                final_error_type = "exception"
                break

        if not success:
            fail_count += 1
            # 非重试类错误也记录
            if final_error_type and final_error_type not in ("tonghuashun_disconnect", "not_filled", "timeout"):
                _save_trade_error(code, trade_type, price, shares, final_error or "未知错误", retry_cmd)

        # 每次操作间隔3秒（最后一个不等待）
        if i < len(actionable) - 1:
            time.sleep(3)

    print("\n" + "=" * 60)
    print(f"[EXECUTE] 执行完成: 成功 {success_count}, 失败 {fail_count}, 跳过 {skip_count}")


def main():
    # 解析 --execute 参数
    # --execute          → mode=None (全部)
    # --execute=buy_sell → mode='buy_sell' (买入/卖出/止盈止损/网格)
    # --execute=t0       → mode='t0' (只执行做T)
    execute_mode = None
    for arg in sys.argv:
        if arg == "--execute":
            execute_mode = None  # 全部
            break
        if arg.startswith("--execute="):
            val = arg.split("=", 1)[1].strip().lower()
            if val in ("buy_sell", "t0"):
                execute_mode = val
            else:
                print(f"[WARN] 无效的 --execute 值: {val}, 使用默认模式(全部)")
                execute_mode = None
            break

    try:
        result = generate_signals()
        json_str = json.dumps(result, indent=2, ensure_ascii=False)
        if sys.stdout.encoding != "utf-8":
            sys.stdout.reconfigure(encoding="utf-8")
        print(json_str)

        if execute_mode is not None:  # --execute 或 --execute=xxx
            execute_signals(result, mode=execute_mode)

        return result
    except Exception as e:
        error_msg = {"error": str(e), "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        print(json.dumps(error_msg, indent=2, ensure_ascii=False))
        return None

if __name__ == "__main__":
    main()
