#!/usr/bin/env python3
"""
资金管理器 — 做T配对价计算

配对价必须落在当日涨跌幅区间内；目标价差被边界夹住时按比例降级；
界内连 1 个价位价差都凑不出 → 这笔做T不能做（ok=False，调用方必须跳过）。

用法:
  from money_manager import calc_t0_pair_plan
  plan = calc_t0_pair_plan(price, shares, is_buy_t0, atr_5min=atr, prev_close=昨收, code=代码)
  if plan["ok"]:
      pair_price = plan["price"]      # 合法挂单价
      ...                             # plan["reason"] 里有降级说明
"""


import os, yaml as _yaml
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
try:
    with open(_CONFIG_PATH) as _f:
        _CONF = _yaml.safe_load(_f) or {}
except Exception:
    _CONF = {}


MIN_SHARES = _CONF.get("trade", {}).get("min_shares", 5000)  # 从config读取，对齐实盘

_TICK = 0.001  # ETF 最小价位

_T0_CFG = _CONF.get("t0", {}) or {}


# 当日涨跌幅限制的判定依据（交易所规则，非策略参数）：
#   - 主板 ETF 10%：沪市 510/512/513/515/516/518/511 等、深市 159 里的非创业板品种
#   - 科创板 ETF 20%：上交所科创板，代码前缀 588/589（588000 科创50ETF、588170 等）
#   - 创业板 ETF 20%：深交所创业板，159xxx 里跟踪创业板指数/创业板50 的品种。
#     注意：159 前缀不是全部 20% —— 159995 芯片ETF、159819 AI龙头ETF、159770 机器人ETF
#     等仍是 10%，所以创业板必须用"已知清单"判断，不能按 159 通配。
# 覆盖优先级（config.yaml → t0 段）：
#   t0.price_limit_pct_overrides[code]  >  t0.chinext_etf_codes（整份替换清单）
#   > 内置创业板清单  >  588/589 前缀  >  t0.price_limit_pct 默认值
CHINEXT_ETF_CODES = {
    "159915",  # 创业板ETF（易方达）
    "159949",  # 创业板50ETF（华安）
    "159952",  # 创业板ETF（广发）
    "159957",  # 创业板ETF（华夏）
    "159958",  # 创业板ETF（工银瑞信）
    "159964",  # 创业板ETF（平安）
    "159967",  # 创业板ETF（南方）
    "159971",  # 创业板ETF（富国）
    "159977",  # 创业板ETF（天弘）
}


def _price_limit_pct(code=None):
    """当日涨跌幅限制比例（判断依据见 CHINEXT_ETF_CODES 上方注释）。"""
    pct = float(_T0_CFG.get("price_limit_pct", 0.10))
    code = str(code).strip() if code else ""

    overrides = _T0_CFG.get("price_limit_pct_overrides") or {}
    if code and code in overrides:
        return float(overrides[code])

    _cfg_chinext = _T0_CFG.get("chinext_etf_codes")
    chinext = {str(c) for c in _cfg_chinext} if _cfg_chinext else CHINEXT_ETF_CODES
    if code and code in chinext:
        return max(pct, 0.20)

    if code[:3] in ("588", "589"):
        return max(pct, 0.20)

    return pct


def day_price_limits(code=None, prev_close=None, ref_price=None):
    """当日涨跌停价 (limit_down, limit_up)。前收缺失时退化为现价±限制比例。

    交易所按最小价位四舍五入：ETF 0.001。
    """
    pct = _price_limit_pct(code)
    base = float(prev_close) if prev_close and float(prev_close) > 0 else float(ref_price or 0)
    if base <= 0:
        return 0.0, 0.0
    return round(base * (1 - pct) + 1e-9, 3), round(base * (1 + pct) + 1e-9, 3)


class MoneyManager:
    """资金管理器 — v2: 仅保留做T配对价计算"""

    @staticmethod
    def calc_t0_pair_plan(current_price, shares, is_buy_t0, atr_5min=None,
                          pair_atr_multiplier=None, prev_close=None, code=None):
        """做T配对价方案（带涨跌幅边界）。返回 dict，ok=False 表示这笔做T不能做。

        目标价差 = max(min_spread, atr_5min*pair_atr_multiplier*shares)（元）。
        股数小 → 每股价差大 → 必须用当日涨跌停价夹住，夹住后按比例降级价差。
        连 1 个最小价位(0.001)的价差都凑不出来 → ok=False，调用方必须跳过整笔做T。
        """
        res = {
            "ok": False, "code": code, "current_price": current_price, "shares": shares,
            "is_buy_t0": is_buy_t0, "price": 0.0, "target_spread": 0.0,
            "actual_spread": 0.0, "actual_spread_yuan": 0.0, "clamped": False,
            "limit_up": 0.0, "limit_down": 0.0, "min_spread_yuan": 0.0,
            "max_spread_yuan": 0.0, "reason": "",
        }
        try:
            current_price = float(current_price)
            shares = int(shares)
        except (TypeError, ValueError):
            res["reason"] = "价格/股数无法解析"
            return res
        if shares <= 0 or current_price <= 0:
            res["reason"] = "价格或股数无效"
            return res

        # 目标价差：固定 min_spread 与 ATR 价差取大
        target = float(_T0_CFG.get("min_spread", 150))
        if atr_5min is not None and float(atr_5min) > 0:
            if pair_atr_multiplier is None:
                pair_atr_multiplier = _T0_CFG.get("pair_atr_multiplier", 1.1)
            atr_spread = float(atr_5min) * float(pair_atr_multiplier) * shares
            if atr_spread > target:
                target = atr_spread
        res["target_spread"] = round(target, 2)
        target_price = current_price + target / shares if is_buy_t0 else current_price - target / shares

        limit_down, limit_up = day_price_limits(code, prev_close, current_price)
        res["limit_down"], res["limit_up"] = limit_down, limit_up
        pct = _price_limit_pct(code)
        if limit_up <= 0:
            limit_down = round(current_price * (1 - pct), 3)
            limit_up = round(current_price * (1 + pct), 3)
            res["limit_down"], res["limit_up"] = limit_down, limit_up

        # 合法可挂区间：配对卖 > 现价，配对买 < 现价，且都在当日涨跌幅内
        if is_buy_t0:
            lo, hi = round(current_price + _TICK, 3), limit_up
        else:
            lo, hi = limit_down, round(current_price - _TICK, 3)
        min_spread_yuan = round(_TICK * shares, 2)
        res["min_spread_yuan"] = min_spread_yuan
        res["max_spread_yuan"] = round((hi - current_price) * shares, 2) if is_buy_t0 \
            else round((current_price - lo) * shares, 2)

        if hi < lo:
            res["reason"] = (f"涨跌幅区间内无合法配对价(允许区间{lo:.3f}~{hi:.3f}, "
                             f"现价{current_price:.3f}, 跌停{limit_down:.3f}, 涨停{limit_up:.3f})")
            return res

        price = round(min(max(target_price, lo), hi), 3)
        actual = abs(price - current_price)
        if actual * shares < min_spread_yuan - 1e-9:
            res["reason"] = (f"界内最大价差{res['max_spread_yuan']:.0f}元 < 最小有效价差"
                             f"{min_spread_yuan:.0f}元(1个价位×{shares}股)，跳过做T")
            return res

        res.update(ok=True, price=price, actual_spread=round(actual, 3),
                   actual_spread_yuan=round(actual * shares, 2))
        res["clamped"] = abs(price - round(target_price, 3)) > 1e-9
        if res["clamped"]:
            res["reason"] = (f"配对价被界限制约：目标价差{target:.0f}元 → 实际可挂"
                             f"{res['actual_spread_yuan']:.0f}元（配对价{price:.3f}, "
                             f"跌停{limit_down:.3f}, 涨停{limit_up:.3f}）")
        return res

# ═══════════════════════════════════════════════
# 模块级快捷函数
# ═══════════════════════════════════════════════

def calc_t0_pair_plan(current_price, shares, is_buy_t0, atr_5min=None,
                      pair_atr_multiplier=None, prev_close=None, code=None):
    return MoneyManager.calc_t0_pair_plan(current_price, shares, is_buy_t0, atr_5min,
                                           pair_atr_multiplier, prev_close, code)
