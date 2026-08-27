#!/usr/bin/env python3
"""
ETF轮动扫描器 v2.0
扫描范围: 原20只ETF + portfolio.json持仓 + etf_pool.json监控标的，去重
输出每个标的的技术面/舆情/调入调出建议到 /tmp/rotation_advice.json
默认不自动写入 etf_pool.json，由用户决策；--force 可强制写入
数据源: 腾讯前复权接口 + 新浪/东财/同花顺舆情
"""

import json
import sys
import os
from datetime import datetime

# ── 项目根目录 ──────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))

# tracker.py 提供标准ETF名称
# DELETED: import tracker
from market_data import calc_rsi_wilder, calc_macd, calc_ma, calc_atr, fetch_klines_daily_arrays, calc_max_drawdown
from factors.indicators import calc_ma as _calc_ma, calc_rsi_wilder as _calc_rsi

# ═══════════════════════════════════════════════════════
#  45只ETF候选池 (扩容 v2.1 — 覆盖AI/机器人/芯片/新能源/军工/低空等)
# ═══════════════════════════════════════════════════════
ETF_CANDIDATES = {
    # ── 科技/半导体/芯片 ──
    "159995": {"name": "芯片ETF",        "sid": "sz159995", "sector": "芯片"},
    "588000": {"name": "科创50ETF",      "sid": "sh588000", "sector": "科创"},
    # ── AI/机器人/算力 ──
    "159819": {"name": "AI龙头ETF",      "sid": "sz159819", "sector": "AI"},
    "159770": {"name": "机器人ETF",      "sid": "sz159770", "sector": "机器人"},
    # ── 通信/5G ──
    "515880": {"name": "通信ETF",        "sid": "sh515880", "sector": "通信"},
    # ── 宽基 ──
    "510300": {"name": "沪深300ETF",     "sid": "sh510300", "sector": "宽基"},
    # ── 军工/国防 ──
    "512660": {"name": "军工ETF",        "sid": "sh512660", "sector": "军工"},
    # ── 低空经济 ──
    # ── 消费电子/家电/汽车 ──
    # ── 新能源/光伏/电池/新能源车 ──
    "515790": {"name": "光伏ETF",        "sid": "sh515790", "sector": "光伏"},
    # ── 金融/证券/银行 ──
    "512880": {"name": "证券ETF",        "sid": "sh512880", "sector": "证券"},
    "512800": {"name": "银行ETF",        "sid": "sh512800", "sector": "银行"},
    # ── 红利/高股息 ──
    # ── 医药/创新药 ──
    "513780": {"name": "港股创新药ETF",   "sid": "sh513780", "sector": "医药"},
    # ── 周期/有色/煤炭/稀土 ──
    "159516": {"name": "半导体设备ETF",    "sid": "sz159516", "sector": "半导体"},
    "515220": {"name": "煤炭ETF",        "sid": "sh515220", "sector": "周期"},
    "518880": {"name": "黄金ETF",        "sid": "sh518880", "sector": "黄金"},
    # ── 医药细分 ──
    # ── 消费/必选 ──
    "159928": {"name": "消费ETF",        "sid": "sz159928", "sector": "消费"},
    # ── 恒生/港股 ──
    "513130": {"name": "恒生科技ETF",     "sid": "sh513130", "sector": "恒生科技"},
    # ── 海外 ──
    "513300": {"name": "纳斯达克ETF",     "sid": "sh513300", "sector": "纳斯达克"},
    # ── 商品 ──
    "159985": {"name": "豆粕ETF",        "sid": "sz159985", "sector": "商品"},
    # ── 券商 ──
    # ── 创业板 ──
    "159949": {"name": "创业板50ETF",     "sid": "sz159949", "sector": "创业板"},
    # ── 红利低波 ──
    "512890": {"name": "红利低波ETF",     "sid": "sh512890", "sector": "红利低波"},
    # ── 能源化工 ──
    "159981": {"name": "能源化工ETF",     "sid": "sz159981", "sector": "能源化工"},
    # ── 海外 ──

}

# ═══════════════════════════════════════════════════════
#  综合扫描范围 — 20只候选 + 持仓 + 监控池
# ═══════════════════════════════════════════════════════

def get_scan_targets():
    """返回去重后的扫描标的 {code: {name, sid, sector, source}}"""
    targets = {}

    # 1. 原20只ETF候选池
    for code, cfg in ETF_CANDIDATES.items():
        targets[code] = {
            "name": cfg["name"],
            "sid": cfg["sid"],
            "sector": cfg.get("sector", ""),
            "source": "候选池",
        }

    # 2. etf_pool.json 监控标的
    pool_path = os.path.join(_DIR, "etf_pool.json")
    if os.path.exists(pool_path):
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                pool = json.load(f)
            pool_names = pool.get("names", {})
            pool_sids = pool.get("sids", {})
            for code in pool.get("codes", []):
                if code not in targets:
                    targets[code] = {
                        "name": pool_names.get(code, code),
                        "sid": pool_sids.get(code, _auto_sid(code)),
                        "sector": "",
                        "source": "监控池",
                    }
        except Exception:
            pass

    # 3. portfolio.json 持仓标的
    pf_path = os.path.join(_DIR, "portfolio.json")
    if os.path.exists(pf_path):
        try:
            with open(pf_path, "r", encoding="utf-8") as f:
                pf = json.load(f)
            for code, pos in pf.get("positions", {}).items():
                if code not in targets:
                    targets[code] = {
                        "name": pos.get("name", code),
                        "sid": _auto_sid(code),
                        "sector": "",
                        "source": "持仓",
                    }
        except Exception:
            pass

    # 4. 用 tracker.py 标准名称覆盖 (腾讯接口名称不用于显示)
    #    ETF_CANDIDATES 中的名称已是标准名称，不覆盖
    #    只对非候选池标的（etf_pool/portfolio 来源）使用 tracker 标准名称
    try:
        code_map = state_center.get_code_map()
        for code in targets:
            if code not in ETF_CANDIDATES and code in code_map:
                targets[code]["name"] = code_map[code]["name"]
    except Exception:
        pass

    return targets


def _auto_sid(code):
    """自动生成 sid: 5开头→sh, 1开头→sz"""
    if code.startswith("5"):
        return f"sh{code}"
    elif code.startswith("1"):
        return f"sz{code}"
    return code


# ═══════════════════════════════════════════════════════
#  技术指标计算
# ═══════════════════════════════════════════════════════

def calc_momentum_4w(closes):
    """计算近4周(20个交易日)涨幅"""
    if len(closes) < 20:
        return None
    current = closes[-1]
    price_4w_ago = closes[-20]
    if price_4w_ago <= 0:
        return None
    return (current - price_4w_ago) / price_4w_ago


def calc_momentum_8w(closes):
    """计算近8周(40个交易日)涨幅"""
    if len(closes) < 40:
        return None
    current = closes[-1]
    price_8w_ago = closes[-40]
    if price_8w_ago <= 0:
        return None
    return (current - price_8w_ago) / price_8w_ago


def calc_volatility(closes, period=20):
    """计算年化波动率(%)，基于日收益率标准差"""
    import math
    if len(closes) < period + 1:
        return None
    daily_returns = []
    for i in range(len(closes) - period, len(closes)):
        if closes[i - 1] and closes[i - 1] > 0:
            daily_returns.append((closes[i] - closes[i - 1]) / closes[i - 1])
    if len(daily_returns) < 2:
        return None
    mean_r = sum(daily_returns) / len(daily_returns)
    variance = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
    daily_vol = math.sqrt(variance)
    annual_vol = daily_vol * math.sqrt(252) * 100  # 年化百分比
    return round(annual_vol, 2)


def calc_macd_trend_score(dif, dea, macd_bar, macd_status):
    """MACD趋势得分: 金叉+1, 死叉-1, 红柱+0.5, 绿柱-0.5"""
    score = 0.0
    if macd_status == "金叉":
        score += 1.0
    elif macd_status == "死叉":
        score -= 1.0
    elif macd_bar is not None:
        if macd_bar > 0 and macd_status in ("红柱放大", "红柱缩小"):
            score += 0.5
        elif macd_bar < 0 and macd_status in ("绿柱放大", "绿柱缩短"):
            score -= 0.5
    return score


def calc_weighted_score(r):
    """多因子加权打分: 4周动量×0.4 + 8周动量×0.3 + RSI强度×0.15 + MACD趋势×0.15"""
    score = 0.0
    # 4周动量
    m4 = r.get("momentum_4w")
    if m4 is not None:
        score += m4 * 100 * 0.4  # 转为百分比
    # 8周动量
    m8 = r.get("momentum_8w")
    if m8 is not None:
        score += m8 * 100 * 0.3
    # RSI 强度 (RSI-50)
    rsi = r.get("rsi")
    if rsi is not None:
        score += (rsi - 50) * 0.15
    # MACD 趋势
    macd_s = r.get("macd_trend_score", 0)
    score += macd_s * 0.15
    return round(score, 2)


def calc_ma_slope(closes, ma_period=20, lookback=5):
    """计算MA斜率: 最近lookback天MA20的变化率"""
    if len(closes) < ma_period + lookback:
        return None
    ma_list = []
    for i in range(ma_period, len(closes) + 1):
        ma_list.append(sum(closes[i - ma_period:i]) / ma_period)
    if len(ma_list) < lookback + 1:
        return None
    # 最近lookback天的MA变化
    ma_recent = ma_list[-1]
    ma_prev = ma_list[-lookback - 1]
    if ma_prev <= 0:
        return None
    return (ma_recent - ma_prev) / ma_prev


def calc_entry_precheck(r):
    """
    策略入场预检：MACD / RSI / MA20 三项检查
    返回 {macd_check, macd_ok, rsi_check, rsi_ok, ma20_check, ma20_ok, ok_count, mark}
    """
    ok_count = 0

    # 1. MACD 状态：红柱/金叉 为可入场条件
    macd_status = r.get("macd_status", "")
    macd_good = macd_status in ("红柱放大", "红柱缩小", "金叉")
    macd_bad = macd_status in ("死叉", "绿柱放大", "绿柱缩短")
    if macd_good:
        macd_check = macd_status
        macd_ok = True
        ok_count += 1
    elif macd_bad:
        macd_check = macd_status
        macd_ok = False
    else:
        macd_check = macd_status if macd_status else "N/A"
        macd_ok = False

    # 2. RSI：30-70 为正常（可入场条件）
    rsi = r.get("rsi")
    if rsi is not None:
        if rsi > 70:
            rsi_check = f"超买({rsi:.1f})"
            rsi_ok = False
        elif rsi < 30:
            rsi_check = f"超卖({rsi:.1f})"
            rsi_ok = False
        else:
            rsi_check = f"正常({rsi:.1f})"
            rsi_ok = True
            ok_count += 1
    else:
        rsi_check = "N/A"
        rsi_ok = False

    # 3. 价格 vs MA20：价格在 MA20 上方为可入场条件
    close = r.get("close")
    ma20 = r.get("ma20")
    if close is not None and ma20 is not None:
        pct = r.get("ma20_pct", 0)
        if close >= ma20:
            ma20_check = f"上方({pct:+.1f}%)"
            ma20_ok = True
            ok_count += 1
        else:
            ma20_check = f"下方({pct:+.1f}%)"
            ma20_ok = False
    else:
        ma20_check = "N/A"
        ma20_ok = False

    # 综合标记
    if ok_count >= 3:
        mark = "✅ 可入场"
    elif ok_count == 2:
        mark = "⚠️ 观察"
    else:
        mark = "❌ 不适合"

    return {
        "macd_check": macd_check,
        "macd_ok": macd_ok,
        "rsi_check": rsi_check,
        "rsi_ok": rsi_ok,
        "ma20_check": ma20_check,
        "ma20_ok": ma20_ok,
        "ok_count": ok_count,
        "mark": mark,
    }


# ═══════════════════════════════════════════════════════
#  舆情抓取 (复用 sentiment_check 的逻辑)
# ═══════════════════════════════════════════════════════

from sentiment_check import fetch_all as fetch_all_news, score as score_sentiment, build_keywords


# ═══════════════════════════════════════════════════════
#  轮动逻辑
# ═══════════════════════════════════════════════════════

def run_rotation(force_write=False):
    """
    扫描全部标的，输出技术面+舆情+调入调出建议。
    force_write=False: 输出到 /tmp/rotation_advice.json，不写 etf_pool.json
    force_write=True: 额外写入 etf_pool.json（原行为）
    """
    print("▸ ETF轮动扫描 v2.0")
    print(f"  日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # 1. 获取扫描范围
    targets = get_scan_targets()
    print(f"  扫描范围: {len(targets)} 只ETF（候选池+持仓+监控池去重）\n")

    # 2. 预加载行业关键词（从 etf_pool.json）
    industry_kw_map = {}
    pool_path = os.path.join(_DIR, "etf_pool.json")
    if os.path.exists(pool_path):
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                pool = json.load(f)
            industry_kw_map = pool.get("industry_kw", {})
        except Exception:
            pass

    # 3. 获取K线数据并计算技术指标
    print("─" * 60)
    print("  【技术面扫描】")
    results = {}
    for code, cfg in targets.items():
        sid = cfg["sid"]
        kline = fetch_klines_daily_arrays(code, sid)
        if kline is None:
            print(f"  ✗ {code} {cfg['name']:12s}  数据不足,跳过")
            continue

        closes = kline["closes"]
        highs = kline["highs"]
        lows = kline["lows"]

        # 计算各项指标
        rsi = calc_rsi_wilder(closes)
        dif, dea, macd_bar, macd_status = calc_macd(closes)
        ma20 = calc_ma(closes, 20)
        _klines = [{"high": h, "low": l, "close": c} for h, l, c in zip(highs, lows, closes)]
        atr = calc_atr(_klines)
        momentum_4w = calc_momentum_4w(closes)
        momentum_8w = calc_momentum_8w(closes)
        volatility = calc_volatility(closes)
        max_dd = calc_max_drawdown(closes)
        macd_trend_score = calc_macd_trend_score(dif, dea, macd_bar, macd_status)
        close = closes[-1]

        # MA20 位置
        if ma20 and close:
            ma20_pct = round((close - ma20) / ma20 * 100, 2)
            ma20_pos = f"{'上方' if close >= ma20 else '下方'}{abs(ma20_pct):.1f}%"
        else:
            ma20_pct = None
            ma20_pos = "N/A"

        results[code] = {
            "name": cfg["name"],
            "sid": sid,
            "sector": cfg.get("sector", ""),
            "source": cfg.get("source", ""),
            "close": close,
            "rsi": rsi,
            "macd_dif": dif,
            "macd_dea": dea,
            "macd_bar": macd_bar,
            "macd_status": macd_status,
            "ma20": ma20,
            "ma20_pct": ma20_pct,
            "ma20_pos": ma20_pos,
            "atr": atr,
            "momentum_4w": momentum_4w,
            "momentum_8w": momentum_8w,
            "volatility": volatility,
            "max_drawdown": max_dd,
            "macd_trend_score": macd_trend_score,
        }

        mom_str = f"{momentum_4w*100:+.2f}%" if momentum_4w is not None else "N/A"
        rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
        print(f"  ✓ {code} {cfg['name']:12s}  价格={close:.3f}  RSI={rsi_str}  "
              f"MACD={macd_status}  动量={mom_str}  MA20{ma20_pos}  ATR={atr}")

    if not results:
        print("\n✗ 无有效数据,退出")
        return

    # 4. 舆情扫描
    print(f"\n{'─' * 60}")
    print("  【舆情扫描】")
    news = fetch_all_news()
    print(f"  抓取 {len(news)} 条新闻（新浪+东财+同花顺）\n")

    for code, r in results.items():
        kw = build_keywords(code, r["name"], industry_kw_map)
        matched_titles = []
        for n in news:
            title = n.get("title", "")
            if any(k in title for k in kw):
                matched_titles.append(title)

        s, neg, pos = score_sentiment(matched_titles)
        if s >= 6:
            level = "🛑 利空"
        elif s >= 3:
            level = "⚠️ 关注"
        elif s <= -3:
            level = "📈 利好"
        else:
            level = "✅ 正常"

        r["sentiment_score"] = s
        r["sentiment_neg"] = neg
        r["sentiment_pos"] = pos
        r["sentiment_level"] = level
        r["matched_news"] = matched_titles[:5]  # 最多保留5条

        news_preview = matched_titles[:2] if matched_titles else []
        preview_str = " | ".join(t[:40] for t in news_preview) if news_preview else "无相关新闻"
        print(f"  {level} {code} {r['name']:12s}  (负{neg}/正{pos})  {preview_str}")

    # 5. 生成调入调出建议 (v2.1 — 多因子打分+多周期确认+动态配比+板块热度)
    print(f"\n{'─' * 60}")
    print("  【调入调出建议】")

    # ── 计算加权得分 ──
    for code, r in results.items():
        r["weighted_score"] = calc_weighted_score(r)

    # ── 获取上证指数MA20斜率，决定攻防配比 ──
    sh_idx = fetch_klines_daily_arrays("000001", "sh000001")
    ma_slope = None
    if sh_idx and len(sh_idx["closes"]) >= 20 + 5:
        ma_slope = calc_ma_slope(sh_idx["closes"])
    if ma_slope is not None and ma_slope > 0:
        momentum_count = 4
        defense_count = 1
        ratio_label = "4攻1守"
    else:
        momentum_count = 2
        defense_count = 3
        ratio_label = "2攻3守"
    slope_str = f"{ma_slope*100:+.2f}%" if ma_slope is not None else "N/A"
    print(f"  上证MA20斜率={slope_str} → {ratio_label} (MA20上升=4攻1守, 否则2攻3守)")

    # ── 板块热度评分 ──
    sector_heat = {}
    for code, r in results.items():
        sector = r.get("sector", "")
        if sector:
            if sector not in sector_heat:
                sector_heat[sector] = []
            if r["momentum_4w"] is not None:
                sector_heat[sector].append(r["momentum_4w"])
    sector_avg = {}
    for sec, mlist in sector_heat.items():
        if mlist:
            sector_avg[sec] = round(sum(mlist) / len(mlist) * 100, 2)
    top_sectors = sorted(sector_avg.items(), key=lambda x: x[1], reverse=True)[:5]
    print(f"  板块热度TOP5: {', '.join(f'{s}({v:+.2f}%)' for s, v in top_sectors)}")

    # ── 加权打分排序 ──
    by_weighted = sorted(
        [(code, r) for code, r in results.items() if r["momentum_4w"] is not None],
        key=lambda x: x[1]["weighted_score"],
        reverse=True,
    )

    # ── 动量TOP3/4: 多周期确认(4w+8w都为正) + 同板块只取最高分 ──
    momentum_picks = []
    picked_sectors = set()
    for code, r in by_weighted:
        if len(momentum_picks) >= momentum_count:
            break
        sector = r.get("sector", "")
        # 多周期动量确认: 4周+8周动量都为正
        m4 = r["momentum_4w"]
        m8 = r["momentum_8w"]
        if m4 is None or m4 <= 0:
            continue
        if m8 is None or m8 <= 0:
            continue
        if sector not in picked_sectors:
            momentum_picks.append(code)
            picked_sectors.add(sector)

    mom_strs = [f"{c}({results[c]['weighted_score']:.1f})" for c in momentum_picks]
    print(f"  动量池({len(momentum_picks)}/{momentum_count}): {', '.join(mom_strs)}")

    # ── 防御TOP2/3: 收紧条件 (回撤<15% + 动量>3% + 波动率>15%) ──
    defense_candidates = [
        (code, r) for code, r in results.items()
        if r["max_drawdown"] is not None and r["max_drawdown"] < 15
        and r["momentum_4w"] is not None and r["momentum_4w"] > 0.03
        and r["volatility"] is not None and r["volatility"] > 15
        and code not in momentum_picks
    ]
    defense_candidates.sort(key=lambda x: x[1]["weighted_score"], reverse=True)

    defense_picks = []
    defense_picked_sectors = set()
    for code, r in defense_candidates:
        if len(defense_picks) >= defense_count:
            break
        sector = r.get("sector", "")
        if sector not in defense_picked_sectors:
            defense_picks.append(code)
            defense_picked_sectors.add(sector)

    # 放宽条件：回撤<20% + 动量>0% + 波动率>10%
    if len(defense_picks) < defense_count:
        relaxed = [
            (code, r) for code, r in results.items()
            if r["max_drawdown"] is not None and r["max_drawdown"] < 20
            and r["momentum_4w"] is not None and r["momentum_4w"] > 0
            and r["volatility"] is not None and r["volatility"] > 10
            and code not in momentum_picks and code not in defense_picks
        ]
        relaxed.sort(key=lambda x: x[1]["weighted_score"], reverse=True)
        for code, r in relaxed:
            if len(defense_picks) >= defense_count:
                break
            sector = r.get("sector", "")
            if sector not in defense_picked_sectors:
                defense_picks.append(code)
                defense_picked_sectors.add(sector)

    def_strs = [f"{c}(DD={results[c]['max_drawdown']:.1f}%,M={results[c]['momentum_4w']*100:.1f}%)" for c in defense_picks]
    print(f"  防御池({len(defense_picks)}/{defense_count}): {', '.join(def_strs)}")

    all_picks = momentum_picks + defense_picks
    pick_types = {}
    for c in momentum_picks:
        pick_types[c] = "动量"
    for c in defense_picks:
        pick_types[c] = "防御"

    # 当前 etf_pool.json 中的标的
    current_pool_codes = set()
    if os.path.exists(pool_path):
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                pool = json.load(f)
            current_pool_codes = set(pool.get("codes", []))
        except Exception:
            pass

    # 为每个标的生成建议
    for code, r in results.items():
        reasons = []
        advice = "保持"

        if code in momentum_picks:
            advice = "调入"
            reasons.append(f"动量TOP{momentum_count} (加权={r['weighted_score']:.1f}, 4w={r['momentum_4w']*100:+.2f}%, 8w={r['momentum_8w']*100:+.2f}% rf)" if r['momentum_8w'] else f"动量TOP{momentum_count} (加权={r['weighted_score']:.1f})")
        elif code in defense_picks:
            advice = "调入"
            reasons.append(f"防御优选 (回撤={r['max_drawdown']:.1f}%, 动量={r['momentum_4w']*100:+.2f}%, 波动={r['volatility']:.1f}%)")
        elif code in current_pool_codes and code not in all_picks:
            advice = "调出"
            reasons.append("不在新一轮TOP3/防御中，动量不足或被替换")
        else:
            # 检查是否为持仓标的
            if code in current_pool_codes:
                reasons.append("当前池中但非本轮优选")
            else:
                reasons.append("未达入选标准")

        # 补充技术面理由
        if r["rsi"] is not None:
            if r["rsi"] > 70:
                reasons.append(f"RSI={r['rsi']:.1f} 超买")
            elif r["rsi"] < 30:
                reasons.append(f"RSI={r['rsi']:.1f} 超卖")
        if r["macd_status"] in ("死叉",):
            reasons.append(f"MACD死叉")
        if r["sentiment_score"] >= 6:
            reasons.append("舆情利空，谨慎")
        if r["sentiment_score"] <= -6:
            reasons.append("舆情利好")

        # 策略入场预检
        precheck = calc_entry_precheck(r)
        r["precheck"] = precheck

        r["advice"] = advice
        r["reasons"] = reasons

        icon = {"调入": "🟢", "调出": "🔴", "保持": "⚪"}.get(advice, "⚪")
        pk = precheck
        print(f"  {icon} {advice}  {code} {r['name']:12s}  |  {'; '.join(reasons)}")
        print(f"    📋 入场预检: MACD={pk['macd_check']} | RSI={pk['rsi_check']} | MA20={pk['ma20_check']} → {pk['mark']}")

    # 6. 输出到 /tmp/rotation_advice.json
    output = {
        "scan_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_scanned": len(results),
        "total_candidates": len(targets),
        "ratio_label": ratio_label,
        "momentum_count": momentum_count,
        "defense_count": defense_count,
        "ma_slope": round(ma_slope * 100, 4) if ma_slope is not None else None,
        "sector_heat": sector_avg,
        "momentum_picks": momentum_picks,
        "defense_picks": defense_picks,
        "all_picks": all_picks,
        "current_pool": sorted(current_pool_codes),
        "etfs": {},
    }

    for code, r in results.items():
        entry = {
            "name": r["name"],
            "sid": r["sid"],
            "sector": r["sector"],
            "source": r["source"],
            "technical": {
                "close": r["close"],
                "rsi": r["rsi"],
                "macd_dif": r["macd_dif"],
                "macd_dea": r["macd_dea"],
                "macd_bar": r["macd_bar"],
                "macd_status": r["macd_status"],
                "ma20": r["ma20"],
                "ma20_pct": r["ma20_pct"],
                "ma20_pos": r["ma20_pos"],
                "atr": r["atr"],
                "momentum_4w": round(r["momentum_4w"] * 100, 2) if r["momentum_4w"] is not None else None,
                "momentum_8w": round(r["momentum_8w"] * 100, 2) if r["momentum_8w"] is not None else None,
                "volatility": r["volatility"],
                "max_drawdown": round(r["max_drawdown"], 1) if r["max_drawdown"] is not None else None,
                "weighted_score": r["weighted_score"],
                "macd_trend_score": r["macd_trend_score"],
            },
            "sentiment": {
                "score": r["sentiment_score"],
                "neg": r["sentiment_neg"],
                "pos": r["sentiment_pos"],
                "level": r["sentiment_level"],
                "matched_news": r["matched_news"],
            },
            "advice": r["advice"],
            "reasons": r["reasons"],
            "precheck": {
                "macd_check": r["precheck"]["macd_check"],
                "macd_ok": r["precheck"]["macd_ok"],
                "rsi_check": r["precheck"]["rsi_check"],
                "rsi_ok": r["precheck"]["rsi_ok"],
                "ma20_check": r["precheck"]["ma20_check"],
                "ma20_ok": r["precheck"]["ma20_ok"],
                "ok_count": r["precheck"]["ok_count"],
                "mark": r["precheck"]["mark"],
            },
        }
        output["etfs"][code] = entry

    out_path = "/tmp/rotation_advice.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'─' * 60}")
    print(f"▸ 建议已写入 {out_path}")
    print(f"  配比: {ratio_label}  |  调入: {len(momentum_picks) + len(defense_picks)} 只  |  "
          f"调出: {sum(1 for r in results.values() if r['advice'] == '调出')} 只")
    print(f"  ⚠ 未自动写入 etf_pool.json，请手动确认后执行")

    # 7. --force 模式：自动写入 etf_pool.json
    if force_write and all_picks:
        # ═══ 记录旧选池，用于清理被移除标的的挂单 ═══
        old_pool_codes = list(current_pool_codes)

        pool_out = {
            "codes": all_picks,
            "names": {c: results[c]["name"] for c in all_picks},
            "sids": {c: results[c]["sid"] for c in all_picks},
            "momentum": {c: round(results[c]["momentum_4w"] * 100, 2) for c in all_picks},
            "momentum_8w": {c: round(results[c]["momentum_8w"] * 100, 2) if results[c]["momentum_8w"] is not None else None for c in all_picks},
            "volatility": {c: results[c]["volatility"] for c in all_picks},
            "weighted_score": {c: results[c]["weighted_score"] for c in all_picks},
            "max_drawdown": {c: round(results[c]["max_drawdown"], 1) for c in all_picks},
            "type": {c: pick_types[c] for c in all_picks},
            "ratio_label": ratio_label,
            "industry_kw": industry_kw_map,
            "updated": datetime.now().strftime("%Y-%m-%d"),
        }
        with open(pool_path, "w", encoding="utf-8") as f:
            json.dump(pool_out, f, ensure_ascii=False, indent=2)
        print(f"  🔧 --force: 已写入 {pool_path} ({len(all_picks)} 只ETF)")

        # ═══ 清理被移除标的的未成交挂单 ═══
        removed_codes = set(old_pool_codes) - set(all_picks)
        if removed_codes:
            print(f"\n{'─' * 60}")
            print(f"  【轮动清理】移除 {len(removed_codes)} 只标的: {removed_codes}")
            rm = RotationManager()
            today_str = datetime.now().strftime('%Y-%m-%d')
            for symbol in removed_codes:
                rm.mark_removed(symbol, today_str)

    return output


# ═══════════════════════════════════════════════════════
#  轮动再准入管理 (RotationManager)
# ═══════════════════════════════════════════════════════

class RotationManager:
    """
    管理被轮动移出标的的再准入规则。

    被移出的标的进入观察期（默认5个交易日），观察期内不可重新纳入；
    观察期结束后需满足连续N日条件（价格>MA20、RSI>40）方可再准入。
    """

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, observe_days=None):
        # 避免单例重复初始化
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self.removed_history = {}  # symbol -> {removed_at, observe_until}
        self.OBSERVE_DAYS = observe_days if observe_days is not None else 5

    @classmethod
    def reset_instance(cls):
        """重置单例（仅用于测试）"""
        cls._instance = None

    # ── 移出记录 ──────────────────────────────────────────
    def mark_removed(self, symbol, date_str):
        """
        记录标的被移出，设置观察期结束日期。

        参数:
          symbol: ETF 代码
          date_str: 移出日期 (YYYY-MM-DD)
        """
        from datetime import timedelta
        removed_dt = datetime.strptime(date_str, "%Y-%m-%d")
        observe_until_dt = removed_dt + timedelta(days=self.OBSERVE_DAYS)
        self.removed_history[symbol] = {
            "removed_at": date_str,
            "observe_until": observe_until_dt.strftime("%Y-%m-%d"),
        }
        print(f"  🕐 轮动移除 {symbol}，观察期至 {observe_until_dt.strftime('%Y-%m-%d')}")

    # ── 再准入检查 ────────────────────────────────────────
    def should_re_add(self, symbol, current_date):
        """
        检查标的是否可重新纳入轮动池。

        规则:
          1. 不在 removed_history 中 → True（从未被移出）
          2. 当前日期 < observe_until → False（观察期内）
          3. 观察期过后 → 调用 check_conditions 连续3日条件检查

        参数:
          symbol: ETF 代码
          current_date: 当前日期 (YYYY-MM-DD)

        返回:
          bool
        """
        if symbol not in self.removed_history:
            return True

        record = self.removed_history[symbol]
        observe_until = record["observe_until"]

        if current_date < observe_until:
            return False

        # 观察期已过，检查是否满足再准入条件
        ok = self.check_conditions(symbol, days=3)
        if ok:
            # 满足条件，清除移出记录
            del self.removed_history[symbol]
        return ok

    # ── 条件检查 ──────────────────────────────────────────
    def check_conditions(self, symbol, days=3):
        """
        检查标的是否满足重新纳入条件：
          - 最近 days 天收盘价均 > MA20
          - 最近 days 天 RSI(14) 均 > 40（未超卖）

        参数:
          symbol: ETF 代码
          days: 需要连续满足的天数

        返回:
          bool
        """
        # 获取 sid
        sid = _auto_sid(symbol)
        kline = fetch_klines_daily_arrays(symbol, sid)
        if kline is None:
            return False

        closes = kline["closes"]
        if len(closes) < 20 + days:
            return False

        # 逐日检查最近 days 天
        for i in range(1, days + 1):
            subset = closes[:-i] if i > 1 else closes
            # 取最后 len(subset) 天的数据用于计算
            daily_closes = closes[:len(closes) - i + 1] if i > 1 else closes

            # 修正：取到倒数第i天为止的数据
            daily_closes = closes[:len(closes) - i + 1]

            close_i = daily_closes[-1]
            ma20_i = _calc_ma(daily_closes, 20)
            rsi_i = _calc_rsi(daily_closes, 14)

            if ma20_i is None or rsi_i is None:
                return False

            if close_i <= ma20_i:
                return False
            if rsi_i <= 40:
                return False

        return True


# ═══════════════════════════════════════════════════════
#  etf_pool.json 加载/回退 (供其他模块使用)
# ═══════════════════════════════════════════════════════

DEFAULT_POOL = {
    "codes": ["159516", "515880", "588170", "159532", "515050"],
    "names": {
        "159516": "半导体设备ETF",
        "515880": "通信ETF",
        "588170": "科创半导体ETF",
        "159532": "中证2000ETF",
        "515050": "中证全指ETF",
    },
    "sids": {
        "159516": "sz159516",
        "515880": "sh515880",
        "588170": "sh588170",
        "159532": "sz159532",
        "515050": "sh515050",
    },
}


def load_etf_pool():
    """加载 etf_pool.json，不存在则回退到默认5只"""
    pool_path = os.path.join(_DIR, "etf_pool.json")
    if os.path.exists(pool_path):
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                pool = json.load(f)
            if pool.get("etf_pool") and len(pool["etf_pool"]) >= 3:
                return pool
        except Exception:
            pass
    return DEFAULT_POOL


# ═══════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    force = "--force" in sys.argv or "-f" in sys.argv
    run_rotation(force_write=force)