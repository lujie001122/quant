#!/usr/bin/env python3
"""组合层择时（R2）——回测与实盘共用同一份实现 + 同一份 config.yaml。

口径（全部来自 config.yaml `timing` 段，代码里不写死）:
  指数: timing.index_code（沪深300ETF 510300）
  趋势结构: close > MA(ma_fast) 且 MA(ma_fast) > MA(ma_slow)          +1
  趋势斜率: MA(ma_fast) 相对 slope_days 个交易日前上升                 +1
  位置:     距 high_window 日最高价回撤 < high_dd_pct                  +1
  位置破位: 距 low_window 日最低价 < low_near_pct                      -2
  波动:     ATR(atr_period)/price > atr_spike_mult × 近 atr_window 日中位数  -1
  打分 → 档位: score >= score_full → levels[2]; score >= score_mid → levels[1]; 否则 levels[0]

指标算法与回测引擎 backtrader 逐位一致:
  - MA  = 每根 bar 对最近 period 个收盘价做 math.fsum / period（backtrader Average）
  - ATR = Wilder 平滑（backtrader MovAv.Smoothed）：首值 = 前 period 个 TR 的均值，
          之后 atr = (atr_prev*(period-1) + tr) / period
"""
import os
import math
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

DEFAULTS = {
    "enabled": True,
    "index_code": "510300",
    "ma_fast": 20,
    "ma_slow": 60,
    "slope_days": 5,
    "high_window": 60,
    "low_window": 60,
    "high_dd_pct": 0.05,
    "low_near_pct": 0.03,
    "atr_period": 14,
    "atr_window": 120,
    "atr_spike_mult": 1.5,
    "score_full": 2,
    "score_mid": 0,
    "levels": [0.5, 0.8, 1.0],
    "gross": 0.90,
    "trim": True,
    "trim_band": 0.10,
    "insufficient_level": 1.0,
}


def load_config():
    """读取并补全 config.yaml 的 timing 段。"""
    cfg = dict(DEFAULTS)
    try:
        with open(_CONFIG_PATH) as f:
            conf = yaml.safe_load(f) or {}
        cfg.update(conf.get("timing") or {})
        cfg["data"] = conf.get("data") or {}
    except Exception:
        cfg["data"] = {}
    levels = cfg.get("levels") or DEFAULTS["levels"]
    if isinstance(levels, str):
        levels = [float(x) for x in levels.split(",")]
    cfg["levels"] = [float(x) for x in levels]
    if len(cfg["levels"]) != 3:
        raise ValueError(f"timing.levels 必须是三档, 当前={cfg['levels']}")
    return cfg


def _atr_series_arrays(closes, highs, lows, period):
    """Wilder 平滑 ATR，与 backtrader MovAv.Smoothed 逐位一致。

    必须从数据起点递推（递归平滑），截断历史会改变 ATR 与中位数。
    """
    n = len(closes)
    tr = [None] * n
    for i in range(1, n):
        h = float(highs[i])
        l = float(lows[i])
        pc = float(closes[i - 1])
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    out = [None] * n
    if n >= period + 1:
        out[period] = math.fsum(tr[1:period + 1]) / period
        for i in range(period + 1, n):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def bars_source(bars):
    """数据源标注: 任一 bar 来自 Sina/akshare 即视为已降级。"""
    srcs = []
    for b in bars:
        s = b.get("source")
        if s and s not in srcs:
            srcs.append(s)
    if not srcs:
        return "unknown", False
    degraded = any(s in ("sina", "akshare") for s in srcs)
    return "+".join(srcs), degraded


def evaluate(bars, cfg=None):
    """对指数日K（升序, 含 open/high/low/close）计算择时状态。

    返回 dict:
      level / score / detail / components / enough / data_source / degraded
    """
    src, degraded = bars_source(bars)
    bars = bars or []
    return evaluate_arrays([b["close"] for b in bars],
                           [b["high"] for b in bars],
                           [b["low"] for b in bars],
                           cfg=cfg, data_source=src, degraded=degraded)


def evaluate_arrays(closes, highs, lows, cfg=None, data_source="unknown", degraded=False):
    """同 evaluate，但直接吃 close/high/low 数组。

    注意: ATR 是 Wilder 递归平滑, 必须从**数据起点**开始递推, 只喂最近 N 根
    会得到不同的 ATR, 阈值会翻档。回测端因此传 510300 的完整历史。
    """
    cfg = cfg or load_config()
    mf = int(cfg["ma_fast"])
    ms = int(cfg["ma_slow"])
    slope_n = int(cfg["slope_days"])
    ap = int(cfg["atr_period"])
    n = len(closes)
    base = {"level": float(cfg["insufficient_level"]), "score": 0,
            "detail": "index-bars-insufficient", "components": {},
            "enough": False, "data_source": data_source, "degraded": degraded}

    if not cfg.get("enabled", True):
        base.update(level=1.0, detail="timing-disabled", enough=True)
        return base
    if n < ms + slope_n:
        return base

    closes = [float(x) for x in closes]

    def _sma_at(end, period):
        """end 为窗口末位置(含), 返回 fsum/period（与 backtrader Average 一致）"""
        if end + 1 < period:
            return None
        return math.fsum(closes[end - period + 1:end + 1]) / period

    atr = _atr_series_arrays(closes, highs, lows, ap)
    price = closes[-1]
    ma_fast = _sma_at(n - 1, mf)
    ma_slow = _sma_at(n - 1, ms)
    ma_fast_prev = _sma_at(n - 1 - slope_n, mf)
    if ma_fast is None or ma_slow is None or ma_fast_prev is None:
        return base
    slope = (ma_fast - ma_fast_prev) / ma_fast_prev if ma_fast_prev > 0 else 0.0

    hw = int(cfg["high_window"])
    lw = int(cfg["low_window"])
    high_n = max(closes[-hw:])
    low_n = min(closes[-lw:])

    atr_now = atr[-1]
    atr_pct = atr_now / price if (atr_now and price > 0) else 0.0
    hist = []
    for i in range(n - 1, -1, -1):
        if len(hist) >= int(cfg["atr_window"]):
            break
        if atr[i] is None or closes[i] <= 0:
            break
        hist.append(atr[i] / closes[i])
    hist.sort()
    atr_med = hist[len(hist) // 2] if hist else atr_pct

    score = 0
    c = {}
    c["trend"] = bool(price > ma_fast and ma_fast > ma_slow)
    c["slope"] = bool(slope > 0)
    c["near_high"] = bool(high_n > 0 and (high_n - price) / high_n < float(cfg["high_dd_pct"]))
    c["near_low"] = bool(low_n > 0 and (price - low_n) / low_n < float(cfg["low_near_pct"]))
    c["vol_spike"] = bool(atr_pct > 0 and atr_med > 0 and atr_pct > float(cfg["atr_spike_mult"]) * atr_med)
    score += 1 if c["trend"] else 0
    score += 1 if c["slope"] else 0
    score += 1 if c["near_high"] else 0
    score -= 2 if c["near_low"] else 0
    score -= 1 if c["vol_spike"] else 0

    levels = cfg["levels"]
    if score >= int(cfg["score_full"]):
        level = levels[2]
    elif score >= int(cfg["score_mid"]):
        level = levels[1]
    else:
        level = levels[0]

    detail = (f"px={price:.3f} ma{mf}={ma_fast:.3f} ma{ms}={ma_slow:.3f} "
              f"slope{slope_n}={slope*100:+.2f}% hi{hw}dd={(high_n-price)/high_n*100:.1f}% "
              f"atr={atr_pct*100:.2f}%(med{atr_med*100:.2f}%)")
    return {"level": float(level), "score": score, "detail": detail,
            "components": c, "enough": True, "data_source": data_source, "degraded": degraded}



def state_label(score, src=""):
    """人类可读的大盘状态标签。"""
    if src in ("sina", "akshare"):
        return f"数据降级({src}) score={score}"
    if score >= 2:
        return f"进攻(score={score})"
    if score >= 0:
        return f"中性(score={score})"
    return f"防守(score={score})"
