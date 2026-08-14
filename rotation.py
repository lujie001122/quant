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
import tracker
from market_data import calc_rsi_wilder, calc_macd, calc_ma, calc_atr, fetch_klines_daily_arrays, calc_max_drawdown

# ═══════════════════════════════════════════════════════
#  20只ETF候选池 (原硬编码)
# ═══════════════════════════════════════════════════════
ETF_CANDIDATES = {
    "512480": {"name": "半导体ETF",    "sid": "sh512480", "sector": "科技"},
    "515880": {"name": "通信ETF",      "sid": "sh515880", "sector": "科技"},
    "515050": {"name": "中证全指ETF",  "sid": "sh515050", "sector": "宽基"},
    "512100": {"name": "中证1000ETF",  "sid": "sh512100", "sector": "宽基"},
    "510050": {"name": "上证50ETF",    "sid": "sh510050", "sector": "宽基"},
    "510300": {"name": "沪深300ETF",   "sid": "sh510300", "sector": "宽基"},
    "159915": {"name": "创业板ETF",    "sid": "sz159915", "sector": "宽基"},
    "588000": {"name": "科创50ETF",    "sid": "sh588000", "sector": "科技"},
    "159869": {"name": "游戏ETF",      "sid": "sz159869", "sector": "消费"},
    "512880": {"name": "证券ETF",      "sid": "sh512880", "sector": "金融"},
    "512690": {"name": "酒ETF",        "sid": "sh512690", "sector": "消费"},
    "159766": {"name": "旅游ETF",      "sid": "sz159766", "sector": "消费"},
    "516670": {"name": "畜牧ETF",      "sid": "sz516670", "sector": "农业"},
    "515790": {"name": "光伏ETF",      "sid": "sh515790", "sector": "新能源"},
    "510880": {"name": "红利ETF",      "sid": "sh510880", "sector": "红利"},
    "517090": {"name": "共赢ETF",      "sid": "sh517090", "sector": "红利"},
    "516510": {"name": "云计算ETF",    "sid": "sh516510", "sector": "科技"},
    "516160": {"name": "新能源ETF",    "sid": "sh516160", "sector": "新能源"},
    "512170": {"name": "医疗ETF",      "sid": "sh512170", "sector": "医药"},
    "159611": {"name": "电力ETF",      "sid": "sz159611", "sector": "公用"},
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
        code_map = tracker.get_code_map()
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
        momentum = calc_momentum_4w(closes)
        max_dd = calc_max_drawdown(closes)
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
            "momentum_4w": momentum,
            "max_drawdown": max_dd,
        }

        mom_str = f"{momentum*100:+.2f}%" if momentum is not None else "N/A"
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

    # 5. 生成调入调出建议
    print(f"\n{'─' * 60}")
    print("  【调入调出建议】")

    # 动量排序
    by_momentum = sorted(
        [(code, r) for code, r in results.items() if r["momentum_4w"] is not None],
        key=lambda x: x[1]["momentum_4w"],
        reverse=True,
    )

    # 选动量TOP3 (不含同板块)
    momentum_picks = []
    picked_sectors = set()
    for code, r in by_momentum:
        if len(momentum_picks) >= 3:
            break
        sector = r.get("sector", "")
        if sector not in picked_sectors:
            momentum_picks.append(code)
            picked_sectors.add(sector)

    # 防御TOP2: 回撤<20%且近4周正收益
    defense_candidates = [
        (code, r) for code, r in results.items()
        if r["max_drawdown"] is not None and r["max_drawdown"] < 20
        and r["momentum_4w"] is not None and r["momentum_4w"] > 0
        and code not in momentum_picks
    ]
    defense_candidates.sort(key=lambda x: x[1]["momentum_4w"], reverse=True)

    defense_picks = []
    defense_picked_sectors = set()
    for code, r in defense_candidates:
        if len(defense_picks) >= 2:
            break
        sector = r.get("sector", "")
        if sector not in defense_picked_sectors:
            defense_picks.append(code)
            defense_picked_sectors.add(sector)

    # 放宽条件
    if len(defense_picks) < 2:
        relaxed = [
            (code, r) for code, r in results.items()
            if r["max_drawdown"] is not None and r["max_drawdown"] < 30
            and r["momentum_4w"] is not None and r["momentum_4w"] > 0
            and code not in momentum_picks and code not in defense_picks
        ]
        relaxed.sort(key=lambda x: x[1]["momentum_4w"], reverse=True)
        for code, r in relaxed:
            if len(defense_picks) >= 2:
                break
            defense_picks.append(code)

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
            reasons.append(f"动量TOP3 (4周动量={r['momentum_4w']*100:+.2f}%)")
        elif code in defense_picks:
            advice = "调入"
            reasons.append(f"防御优选 (回撤={r['max_drawdown']:.1f}%, 动量={r['momentum_4w']*100:+.2f}%)")
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
                "max_drawdown": round(r["max_drawdown"], 1) if r["max_drawdown"] is not None else None,
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
    print(f"  调入: {len(momentum_picks) + len(defense_picks)} 只  |  "
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
            "max_drawdown": {c: round(results[c]["max_drawdown"], 1) for c in all_picks},
            "type": {c: pick_types[c] for c in all_picks},
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
            cleanup_removed_symbols(old_pool_codes, all_picks, results)

    return output


# ═══════════════════════════════════════════════════════
#  轮动清理：选池后自动撤单被移除标的的未成交挂单
# ═══════════════════════════════════════════════════════

def cleanup_removed_symbols(old_pool, new_pool,
                            results=None):
    """
    轮动选池后自动清理被移除标的的未成交挂单。

    参数:
      old_pool: 旧选池代码列表
      new_pool: 新选池代码列表
      results: 轮动扫描结果（可选，用于日志）

    返回:
      {cancelled: int, skipped: int, errors: list}
    """
    removed = set(old_pool) - set(new_pool)
    result = {"cancelled": 0, "skipped": 0, "errors": []}

    if not removed:
        return result

    for symbol in removed:
        try:
            # 尝试使用 order_manager 撤单
            from order_manager import OrderManager
            om = OrderManager()
            pending = om.get_pending_orders(symbol)
            if pending:
                for order in pending:
                    try:
                        om.cancel_order(order.order_id)
                        print(f"  🔄 轮动移除 {symbol}，已撤单 {order.order_id}")
                        result["cancelled"] += 1
                    except Exception as e:
                        print(f"  ⚠ 撤单失败 {order.order_id}: {e}")
                        result["errors"].append(str(e))
            else:
                print(f"  ℹ 轮动移除 {symbol}，无未成交挂单")
                result["skipped"] += 1
        except ImportError:
            # order_manager 不可用时，通过文件系统清理
            try:
                import os as _os
                orders_dir = _os.path.join(_DIR, "orders")
                if _os.path.exists(orders_dir):
                    cancelled_local = 0
                    for fname in _os.listdir(orders_dir):
                        if fname.endswith(".json") and symbol in fname:
                            fpath = _os.path.join(orders_dir, fname)
                            try:
                                import json as _json
                                with open(fpath, "r") as f:
                                    od = _json.load(f)
                                if od.get("status") == "pending":
                                    od["status"] = "revoked"
                                    od["revoke_reason"] = f"轮动移除 {symbol}"
                                    with open(fpath, "w") as f:
                                        _json.dump(od, f, ensure_ascii=False)
                                    cancelled_local += 1
                                    print(f"  🔄 轮动移除 {symbol}，标记已撤单 {fname}")
                            except Exception:
                                pass
                    result["cancelled"] = cancelled_local
                    if cancelled_local == 0:
                        result["skipped"] += 1
                else:
                    result["skipped"] += 1
            except Exception as e:
                print(f"  ⚠ 清理失败 {symbol}: {e}")
                result["errors"].append(str(e))

    return result


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
            if pool.get("codes") and len(pool["codes"]) >= 3:
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