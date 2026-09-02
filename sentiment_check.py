"""舆情扫描 v5.0 — 全赛道分析（监控池+候选池）+ 吃肉/喝汤/回暖/风险

新增：
  - 覆盖监控池+候选池全部ETF
  - 每个ETF详细分析归类原因
  - 按赛道分组输出
"""
import json, urllib.request, re, logging, os
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger("sentiment_check")
logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")

NEG = {"利空": 3, "暴跌": 3, "崩盘": 3, "减持": 2, "亏损": 2, "下滑": 1,
       "预警": 2, "暴雷": 3, "跌停": 3, "退市": 3, "制裁": 3,
       "加征关税": 3, "关税战": 3, "关税壁垒": 3, "关税升级": 3, "关税": 1,
       "踩踏": 2, "抛售": 3, "危机": 3, "风险": 1, "债务": 2, "违约": 2}
POS = {"利好": 3, "涨停": 3, "突破": 2, "大增": 2, "预增": 2, "回购": 2,
       "增持": 2, "分红": 1, "扶持": 2, "超预期": 2, "新高": 2, "反弹": 1,
       "领涨": 2, "走强": 1, "退税": 3, "减免": 2, "豁免": 2}

GLOBAL_KW = [
    "美股", "纳指", "纳斯达克", "标普", "道指", "道琼斯",
    "外盘", "海外市场", "全球市场", "欧股", "日经", "恒生",
    "港股", "A股", "沪深", "创业板", "科创板",
    "大跌", "暴跌", "崩盘", "熔断", "狂泻", "重挫", "跳水",
    "暴涨", "狂飙", "井喷",
    "加息", "降息", "美联储", "通胀", "非农", "CPI", "PPI",
    "GDP", "PMI", "央行", "货币政策", "财政政策",
    "汇率", "人民币", "美元", "中美", "关税", "贸易战",
    "地缘", "制裁", "冲突",
]

NEG_THRESHOLD_WARN = 3
NEG_THRESHOLD_BLOCK = 6

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def fetch_sina():
    url = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&k=&num=30&page=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"})
        raw = urllib.request.urlopen(req, timeout=10).read()
        data = json.loads(raw)
        return [{"title": item.get("title", ""), "digest": item.get("intro", "")}
                for item in data.get("result", {}).get("data", []) if item.get("title")]
    except Exception as e:
        logger.warning(f"新浪抓取失败: {e}")
        return []


def fetch_10jqka():
    url = "https://news.10jqka.com.cn/tapp/news/push/stock/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": "https://news.10jqka.com.cn/"})
        raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
        data = json.loads(raw)
        return [{"title": item.get("title", ""), "digest": item.get("digest", "")}
                for item in data.get("data", {}).get("list", []) if item.get("title")]
    except Exception as e:
        logger.warning(f"同花顺抓取失败: {e}")
        return []


def fetch_eastmoney():
    url = "https://finance.eastmoney.com/a/cgsxw.html"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": "https://finance.eastmoney.com/"})
        raw = urllib.request.urlopen(req, timeout=10).read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gb2312", errors="replace")
        pattern = r'<div class="title">\s*<a[^>]*>([^<]+)</a>'
        titles = re.findall(pattern, text)
        return [{"title": t.strip()} for t in titles if t.strip()]
    except Exception as e:
        logger.warning(f"东财抓取失败: {e}")
        return []


def fetch_all():
    all_news, seen = [], set()
    for source_news in [fetch_sina(), fetch_10jqka(), fetch_eastmoney()]:
        for item in source_news:
            title = item["title"].strip()
            if title and title not in seen:
                seen.add(title)
                all_news.append(item)
    return all_news


def score(titles):
    n = sum(sum(v for k, v in NEG.items() if k in t) for t in titles)
    p = sum(sum(v for k, v in POS.items() if k in t) for t in titles)
    return n - p, n, p


def build_keywords(code, name, industry_kw_map=None):
    kw = [name]
    if not name.endswith("ETF"):
        kw.append(name + "ETF")
    kw.append(code)
    if industry_kw_map:
        for k in industry_kw_map.get(code, []):
            if k not in kw:
                kw.append(k)
    return kw


def get_all_targets():
    """合并监控池+候选池，去重。返回 {code: {name, kw, sector, is_monitor}}"""
    targets = {}
    _DIR = os.path.dirname(os.path.abspath(__file__))

    # 候选池
    try:
        from rotation import ETF_CANDIDATES
        for code, cfg in ETF_CANDIDATES.items():
            targets[code] = {
                "name": cfg["name"],
                "sector": cfg.get("sector", ""),
                "is_monitor": False,
                "kw": build_keywords(code, cfg["name"]),
            }
    except ImportError:
        pass

    # 监控池（叠加行业关键词）
    pool_path = os.path.join(_DIR, "etf_pool.json")
    if os.path.exists(pool_path):
        with open(pool_path, encoding="utf-8") as f:
            pool = json.load(f)
        pool_names = pool.get("names", {})
        industry_kw = pool.get("industry_kw", {})
        for code in pool.get("etf_pool", []):
            name = pool_names.get(code, code)
            if code in targets:
                targets[code]["is_monitor"] = True
                targets[code]["kw"] = build_keywords(code, name, industry_kw)
            else:
                targets[code] = {
                    "name": name,
                    "sector": "",
                    "is_monitor": True,
                    "kw": build_keywords(code, name, industry_kw),
                }

    return targets


def fetch_market_data():
    """获取所有标的的行情数据。"""
    try:
        from market_data import fetch_klines_daily
        from rotation import calc_momentum_4w, calc_rsi_wilder
    except ImportError:
        return {}

    targets = get_all_targets()
    data = {}
    for code in targets:
        try:
            klines = fetch_klines_daily(code)
            if not klines or len(klines) < 20:
                continue
            closes = [k["close"] for k in klines]
            m4 = calc_momentum_4w(closes)
            rsi = calc_rsi_wilder(closes, 14)
            pct_5d = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
            pct_20d = (closes[-1] / closes[-20] - 1) * 100 if len(closes) >= 20 else 0
            data[code] = {
                "name": targets[code]["name"],
                "m4": m4,
                "rsi": rsi,
                "pct_5d": pct_5d,
                "pct_20d": pct_20d,
                "price": closes[-1],
            }
        except Exception as e:
            logger.warning(f"{code} 行情获取失败: {e}")
    return data


def classify_track(code, mkt, sentiment_score):
    """分类并返回 (track_label, reason)。"""
    if not mkt:
        return ("⏳ 数据不足", "无行情数据")

    m4 = mkt.get("m4", 0)
    rsi = mkt.get("rsi", 50)
    pct_5d = mkt.get("pct_5d", 0)
    pct_20d = mkt.get("pct_20d", 0)

    reasons = []
    # 风险优先检查
    if rsi > 75:
        reasons.append(f"RSI={rsi:.0f}极度超买")
    if sentiment_score < -3:
        reasons.append(f"舆情利空(得分{sentiment_score})")
    if m4 < -0.08:
        reasons.append(f"4周动量{m4*100:.1f}%大幅下跌")
    if rsi > 70 or sentiment_score <= -3 or m4 < -0.05:
        if not reasons:
            if rsi > 70:
                reasons.append(f"RSI={rsi:.0f}超买")
            if m4 < -0.05:
                reasons.append(f"4周动量{m4*100:.1f}%偏弱")
        return ("⚠️ 提示风险", "；".join(reasons) if reasons else "综合风险信号")

    # 吃肉
    if m4 > 0.08 and rsi < 70 and pct_5d > 2 and sentiment_score >= 0:
        reasons.append(f"4周强动量+{m4*100:.1f}%")
        reasons.append(f"5日涨+{pct_5d:.1f}%")
        reasons.append(f"RSI={rsi:.0f}健康")
        return ("🥩 吃肉", "；".join(reasons))

    # 喝汤
    if m4 > 0.03 and pct_5d > 0 and sentiment_score >= 0:
        reasons.append(f"4周动量+{m4*100:.1f}%温和上行")
        reasons.append(f"5日涨+{pct_5d:.1f}%")
        return ("🥣 喝汤", "；".join(reasons))

    # 回暖
    if m4 > 0 and pct_5d > 0:
        if m4 > 0.02:
            reasons.append(f"4周动量+{m4*100:.1f}%转正回暖")
        else:
            reasons.append(f"4周动量{m4*100:.1f}%微弱转正")
        reasons.append(f"5日涨+{pct_5d:.1f}%短线回暖")
        return ("🔄 重点关注回暖", "；".join(reasons))
    if m4 > 0:
        reasons.append(f"4周动量+{m4*100:.1f}%开始转正")
        reasons.append(f"5日涨{pct_5d:+.1f}%待确认")
        return ("🔄 重点关注回暖", "；".join(reasons))

    # 观察
    if m4 > -0.03:
        reasons.append(f"4周动量{m4*100:.1f}%横盘震荡")
        return ("⏳ 观察中", "；".join(reasons))
    reasons.append(f"4周动量{m4*100:.1f}%下行趋势")
    return ("⏳ 观察中", "；".join(reasons))


if __name__ == "__main__":
    now = datetime.now().strftime("%H:%M")
    print(f"🔍 全赛道舆情分析 | {now}")
    news = fetch_all()
    print(f"  抓取 {len(news)} 条新闻（新浪+东财+同花顺）\n")

    targets = get_all_targets()
    mkt_data = fetch_market_data()

    # 舆情关键词匹配
    news_titles = [n.get("title", "") for n in news]

    # 分组收集
    categories = defaultdict(list)

    for code, info in sorted(targets.items()):
        if code not in mkt_data:
            cat = "⏳ 数据不足"
            detail = f"  {cat}  {code} {info['name']}"
            categories[cat].append(detail)
            continue

        mkt = mkt_data[code]
        matched = [t for t in news_titles if any(kw in t for kw in info["kw"])]
        s, neg, pos = score(matched)

        track, reason = classify_track(code, mkt, s)

        monitor_tag = "📌" if info["is_monitor"] else "  "
        sector_tag = f"[{info['sector']}]" if info.get("sector") else ""

        lines = []
        lines.append(f"{monitor_tag} {track:<16} {code} {info['name']:<14} {sector_tag}")
        lines.append(f"   原因: {reason}")
        mkt_line = f"   m4={mkt['m4']*100:+.1f}%  rsi={mkt['rsi']:.0f}  5d={mkt['pct_5d']:+.1f}%  20d={mkt['pct_20d']:+.1f}%"
        if s != 0:
            mkt_line += f"  舆情(负{neg}/正{pos})"
        lines.append(mkt_line)
        if matched:
            lines.append(f"   新闻: {matched[0][:55]}")
        categories[track].append("\n".join(lines))

    # 按赛道输出
    track_order = ["🥩 吃肉", "🥣 喝汤", "🔄 重点关注回暖", "⏳ 观察中", "⚠️ 提示风险", "⏳ 数据不足"]

    for track in track_order:
        items = categories.get(track, [])
        if not items:
            continue
        print(f"\n{'='*50}")
        print(f"{track} ({len(items)})")
        print(f"{'='*50}")
        for item in items:
            print(item)
            print()

    # 监控池汇总
    print(f"\n{'='*50}")
    print("📌 监控池汇总")
    print(f"{'='*50}")
    monitor_list = []
    for code, info in sorted(targets.items()):
        if info["is_monitor"] and code in mkt_data:
            mkt = mkt_data[code]
            track, _ = classify_track(code, mkt, 0)
            monitor_list.append((track, code, info["name"]))
    for track, code, name in monitor_list:
        print(f"  {track:<16} {code} {name}")

    # 全球板块
    print(f"\n{'='*50}")
    print("🌍 全局 | 美股/外盘/宏观")
    print(f"{'='*50}")
    global_matched = [t for t in news_titles if any(kw in t for kw in GLOBAL_KW)]
    s, neg, pos = score(global_matched)
    if s >= NEG_THRESHOLD_BLOCK:
        print(f"  🛑 利空 (负{neg}/正{pos})")
    elif s >= NEG_THRESHOLD_WARN:
        print(f"  ⚠️ 关注 (负{neg}/正{pos})")
    else:
        print(f"  ✅ 正常 (负{neg}/正{pos})")
    if global_matched:
        for title in global_matched[:10]:
            print(f"    · {title[:60]}")
        if len(global_matched) > 10:
            print(f"    ... 共{len(global_matched)}条")