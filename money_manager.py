#!/usr/bin/env python3
"""
资金管理器 — 做T配对价计算

目标价差 = max(最小价位 × min_ticks, 卖出价 × min_spread_pct)（比例为主）。
金额 min_spread_amount 只作参考上限：比例价差够 150 元就用比例，不够也不为凑金额拉大。

硬保护：配对价必须距现价 ≤ pair_max_dist_pct(1.5%)，且距当日涨跌停至少 1 个价位；
越界 → ok=False + 打印 [ALERT]，整笔做T放弃（宁可少做一次，也不挂永远不成交的单）。

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

# 默认值仅作 config 缺省兜底，实盘一律走 config.yaml 的 t0 段
_DEF_MIN_SPREAD_PCT = 0.003      # 最小价差比例 0.3%（0.7元ETF → 0.002，可成交）
_DEF_MIN_TICKS = 2               # 最小价差 = 至少 N 个最小价位
_DEF_MIN_SPREAD_AMOUNT = 150.0   # 金额参考值：只记录，不参与定价
_DEF_MAX_DIST_PCT = 0.015        # 配对价距现价上限 1.5%
_DEF_LIMIT_MARGIN_TICKS = 1      # 配对价距涨跌停至少 N 个价位


# 当日涨跌幅限制的判定依据（交易所规则，非策略参数）：
#   - 主板 ETF 10%：沪市 510/512/513/515/516/518/511 等、深市 159 里的非创业板品种
#   - 科创板 ETF 20%：上交所科创板，代码前缀 588/589（588000 科创50ETF、588170 等）
#   - 创业板 ETF 20%：深交所创业板（含 双创/科创创业 系），159xxx 里跟踪创业板指数的品种。
#     注意：159 前缀不是全部 20% —— 159995 芯片ETF、159819 AI龙头ETF、159770 机器人ETF
#     等仍是 10%，所以创业板必须用"已知清单"判断，不能按 159 通配。
#   - 债券 ETF 10%：科创债ETF（511120/511150/159110 等 12 只）名字里有"科创"但跟踪的是
#     信用债指数，交易所按债券基金给 10%，绝不能进这份清单。
#
# 这份清单怎么来的（2026-11-19，全池 1761 只系统性判定）：
#   ① 数据判据：腾讯不复权日线全历史，剔除 |单日涨跌|>25% 的份额折算/脏数据后，
#      出现过 |单日涨跌| 落在 19.75%~20.10% 的 → 铁证 20%（主板 10% 永远打不出 20%）。
#      本清单 30 只有此铁证（159915 在 2024-09-30 打 +20.00%，159991 2024-10-08 +20.08% …）。
#   ② 类型判据：跟踪创业板/双创指数的 ETF 按交易所规则就是 20%，哪怕上市太短还没打出过 20%
#      （如 159069/159190 等 2025-2026 年新基金，历史最大才 5%）。159918 中创400、159942
#      中创100、159976/159979 湾创、159974 央企创新、159987 科技创新 是跨市场主题指数，
#      历史多次精确打 ±10.00%（159942 有 9 天），确认为 10%，不进清单。
#   ③ 159335 央企科创ETF 例外：名字带"科创"但跟踪中证央企科技创新指数（成分以主板央企为主），
#      2024-10-08 全市场暴涨日只涨 7.43%、2025-04-07 只跌 5.62%，从未接近 10%，保守留 10%。
# 反向核对：清单内 86 只全部有 ≥5% 的历史波动；判 20% 但历史最大 <5% 的 = 0 只。
#
# 覆盖优先级（config.yaml → t0 段）：
#   t0.price_limit_pct_overrides[code]  >  t0.chinext_etf_codes（整份替换清单）
#   > 内置创业板清单  >  588/589 前缀  >  t0.price_limit_pct 默认值
CHINEXT_ETF_CODES = {
    # ── 创业板系 ETF（深交所：跟踪创业板指数的 ETF 涨跌幅 20%）──
    "159009",  # 创业板新能源ETF易方达
    "159069",  # 创业板新能源ETF华泰柏瑞
    "159076",  # 创业板新能源ETF华宝
    "159107",  # 创业板软件ETF富国
    "159122",  # 创业板新能源ETF富国
    "159149",  # 创业板新能源ETF工银
    "159187",  # 创业板新能源ETF景顺
    "159190",  # 创业板新能源ETF天弘
    "159205",  # 创业板ETF东财
    "159242",  # 创业板人工智能ETF大成
    "159243",  # 创业板人工智能ETF招商
    "159246",  # 创业板人工智能ETF富国
    "159247",  # 创业板ETF汇添富
    "159256",  # 创业板软件ETF华夏
    "159261",  # 创业板新能源ETF鹏华
    "159270",  # 创业板200ETF南方
    "159279",  # 创业板人工智能ETF华安
    "159287",  # 创业板综ETF博时
    "159288",  # 创业板综ETF银华
    "159289",  # 创业板综指ETF鹏华
    "159290",  # 创业板综指增强ETF东财
    "159291",  # 创业板综增强ETF招商
    "159292",  # 创业板综增强ETF华宝
    "159293",  # 创业板综增强ETF建信
    "159295",  # 创业综指ETF西部利得
    "159298",  # 创业板50ETF大成
    "159363",  # 创业板人工智能ETF华宝
    "159367",  # 创业板50ETF华夏
    "159368",  # 创业板新能源ETF华夏
    "159369",  # 创业板50ETF易方达
    "159370",  # 创50ETF工银
    "159371",  # 创业板50ETF富国
    "159372",  # 创业板50ETF万家
    "159373",  # 创业板50ETF嘉实
    "159375",  # 创业板50ETF国泰
    "159377",  # 创业板医药ETF国泰
    "159381",  # 创业板人工智能ETF华夏
    "159382",  # 创业板人工智能ETF南方
    "159383",  # 创业板50ETF华泰柏瑞
    "159387",  # 创业板新能源ETF国泰
    "159388",  # 创业板人工智能ETF国泰
    "159541",  # 创业板综ETF万家
    "159563",  # 创业板综ETF华夏
    "159571",  # 创业板200ETF富国
    "159572",  # 创业板200ETF易方达
    "159573",  # 创业板200ETF华夏
    "159575",  # 创业板200ETF银华
    "159597",  # 创业板成长ETF易方达
    "159675",  # 创业板增强ETF嘉实
    "159676",  # 创业板增强ETF富国
    "159681",  # 创业板50ETF鹏华
    "159682",  # 创业板50ETF景顺
    "159773",  # 创业板科技ETF华泰柏瑞
    "159777",  # 创科技ETF国联安
    "159804",  # 创中盘88ETF国寿
    "159808",  # 创100ETF融通
    "159810",  # 创业板ETF浦银
    "159814",  # 创业大盘ETF西部利得
    "159821",  # 创业板ETF中银证券
    "159836",  # 创业板300ETF天弘
    "159908",  # 创业板ETF博时
    "159915",  # 创业板ETF易方达
    "159948",  # 创业板ETF南方
    "159949",  # 创业板50ETF华安
    "159952",  # 创业板ETF广发
    "159955",  # 创业板ETF嘉实
    "159956",  # 创业板ETF建信
    "159957",  # 创业板ETF华夏
    "159958",  # 创业板ETF工银
    "159964",  # 创业板ETF平安
    "159966",  # 创业板价值ETF华夏
    "159967",  # 创业板成长ETF华夏
    "159971",  # 创业板ETF富国
    "159977",  # 创业板ETF天弘
    "159991",  # 创业板大盘ETF招商
    # ── 双创系 ETF（创业板+科创板 双指数，同样 20%）──
    "159012",  # 科创创业50ETF鹏华
    "159022",  # 科创创业人工智能ETF富国
    "159139",  # 科创创业人工智能ETF华泰柏瑞
    "159140",  # 科创创业人工智能ETF易方达
    "159141",  # 科创创业人工智能ETF永赢
    "159142",  # 科创创业人工智能ETF景顺
    "159603",  # 科创创业ETF天弘
    "159780",  # 科创创业50ETF南方
    "159781",  # 科创创业ETF易方达
    "159782",  # 双创50ETF银华
    "159783",  # 科创创业50ETF华夏
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
        """做T配对价方案（比例为主 + 硬保护）。返回 dict，ok=False 表示这笔做T不能做。

        目标价差 = max(最小价位×min_ticks, 现价×min_spread_pct) × 股数（比例为主）；
        ATR 价差更大时才放宽。min_spread_amount(150元) 只作参考，不参与定价：
        比例价差 ≥150 元就用比例，<150 元也保持比例，绝不为凑金额拉大价差。
        硬保护：配对价必须距现价 ≤ pair_max_dist_pct，且距涨跌停 ≥1 个价位；
        越界 → ok=False + [ALERT]，整笔做T放弃（宁可少做一次，不挂永远不成交的单）。
        """
        res = {
            "ok": False, "code": code, "current_price": current_price, "shares": shares,
            "is_buy_t0": is_buy_t0, "price": 0.0, "target_spread": 0.0,
            "ref_spread_amount": 0.0, "actual_spread": 0.0, "actual_spread_yuan": 0.0,
            "clamped": False, "limit_up": 0.0, "limit_down": 0.0,
            "min_spread_yuan": 0.0, "max_spread_yuan": 0.0, "reason": "",
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

        # ── 目标价差：比例为主（默认 0.3%），最少 min_ticks 个价位 ──
        # 金额只记录不参与定价：1500股×0.7元凑150元 = 0.1元/股(14%) → 必被跌停夹死。
        # 每股价差取整到整档（0.001），配对价才能精确挂出，且"目标价差"与"实际价差"一致。
        min_pct = float(_T0_CFG.get("min_spread_pct", _DEF_MIN_SPREAD_PCT))
        min_ticks = int(_T0_CFG.get("min_ticks", _DEF_MIN_TICKS))
        res["ref_spread_amount"] = round(
            float(_T0_CFG.get("min_spread_amount", _DEF_MIN_SPREAD_AMOUNT)), 2)
        per_share = max(_TICK * min_ticks, current_price * min_pct)
        if atr_5min is not None and float(atr_5min) > 0:
            if pair_atr_multiplier is None:
                pair_atr_multiplier = _T0_CFG.get("pair_atr_multiplier", 1.1)
            per_share = max(per_share, float(atr_5min) * float(pair_atr_multiplier))
        per_share = round(int(per_share / _TICK + 0.5 + 1e-9) * _TICK, 3)
        target = round(per_share * shares, 2)
        res["target_spread"] = target
        target_price = current_price + per_share if is_buy_t0 else current_price - per_share

        limit_down, limit_up = day_price_limits(code, prev_close, current_price)
        if limit_up <= 0:
            pct = _price_limit_pct(code)
            limit_down = round(current_price * (1 - pct), 3)
            limit_up = round(current_price * (1 + pct), 3)
        res["limit_down"], res["limit_up"] = limit_down, limit_up

        # ── 安全区间：配对卖 > 现价 / 配对买 < 现价，距涨跌停 ≥1 档，距现价 ≤ max_dist ──
        margin = _TICK * int(_T0_CFG.get("limit_margin_ticks", _DEF_LIMIT_MARGIN_TICKS))
        max_dist = float(_T0_CFG.get("pair_max_dist_pct", _DEF_MAX_DIST_PCT))
        if is_buy_t0:
            lo = round(current_price + _TICK, 3)
            hi = round(min(limit_up - margin, current_price * (1 + max_dist)), 3)
        else:
            lo = round(max(limit_down + margin, current_price * (1 - max_dist)), 3)
            hi = round(current_price - _TICK, 3)
        res["min_spread_yuan"] = round(_TICK * shares, 2)
        res["max_spread_yuan"] = round((hi - current_price) * shares, 2) if is_buy_t0 \
            else round((current_price - lo) * shares, 2)

        price = round(target_price, 3)
        if hi < lo or not (lo - 1e-9 <= price <= hi + 1e-9):
            print(f"[ALERT] T0 配对价异常（{price:.3f}），不下单")
            res["reason"] = (f"配对价{price:.3f}超出安全区间{lo:.3f}~{hi:.3f}"
                             f"（现价{current_price:.3f}，跌停{limit_down:.3f}，"
                             f"涨停{limit_up:.3f}，距现价上限±{max_dist*100:.1f}%），放弃本次做T")
            return res

        actual = abs(price - current_price)
        res.update(ok=True, price=price, actual_spread=round(actual, 3),
                   actual_spread_yuan=round(actual * shares, 2))
        return res

# ═══════════════════════════════════════════════
# 模块级快捷函数
# ═══════════════════════════════════════════════

def calc_t0_pair_plan(current_price, shares, is_buy_t0, atr_5min=None,
                      pair_atr_multiplier=None, prev_close=None, code=None):
    return MoneyManager.calc_t0_pair_plan(current_price, shares, is_buy_t0, atr_5min,
                                           pair_atr_multiplier, prev_close, code)
