"""舆情扫描 v4.1 — 多源新闻抓取（新浪 + 东财 + 同花顺）+ 情感分词 + 赛道分析

新增赛道分析（main入口）：
  🥩 吃肉 — 强动量+积极情绪，趋势向上
  🥣 喝汤 — 温和正面，趋势平稳
  🔄 重点关注回暖 — 有回暖迹象，从弱转强
  ⚠️ 提示风险 — 超买/利空/下行风险
"""
import json, urllib.request, re, logging
from datetime import datetime
from state_center import get_tracked_with_keywords

logger = logging.getLogger("sentiment_check")
logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")

TRACKED = get_tracked_with_keywords()

NEG = {"利空": 3, "暴跌": 3, "崩盘": 3, "减持": 2, "亏损": 2, "下滑": 1,
       "预警": 2, "暴雷": 3, "跌停": 3, "退市": 3, "制裁": 3,
       "加征关税": 3, "关税战": 3, "关税壁垒": 3, "关税升级": 3, "关税": 1,
       "踩踏": 2, "抛售": 3, "危机": 3, "风险": 1, "债务": 2, "违约": 2}
POS = {"利好": 3, "涨停": 3, "突破": 2, "大增": 2, "预增": 2, "回购": 2,
       "增持": 2, "分红": 1, "扶持": 2, "超预期": 2, "新高": 2, "反弹": 1,
       "领涨": 2, "走强": 1, "退税": 3, "减免": 2, "豁免": 2}

# 全局关键词 — 不绑定任何 ETF，覆盖美股/外盘/宏观
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
        results = []
        for item in data.get("result", {}).get("data", []):
            title = item.get("title", "")
            if title:
                results.append({"title": title, "digest": item.get("intro", "")})
        return results
    except Exception as e:
        logger.warning(f"新浪抓取失败: {e}")
        return []


def fetch_10jqka():
    url = "https://news.10jqka.com.cn/tapp/news/push/stock/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": "https://news.10jqka.com.cn/"})
        raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
        data = json.loads(raw)
        results = []
        for item in data.get("data", {}).get("list", []):
            title = item.get("title", "")
            if title:
                results.append({"title": title, "digest": item.get("digest", "")})
        return results
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
    all_news = []
    seen = set()
    for source_news in [fetch_sina(), fetch_10jqka(), fetch_eastmoney()]:
        for item in source_news:
            title = item["title"].strip()
            if title and title not in seen:
                seen.add(title)
                all_news.append(item)
    return all_news


def score(titles):
    n, p = 0, 0
    for t in titles:
        n += sum(v for k, v in NEG.items() if k in t)
        p += sum(v for k, v in POS.items() if k in t)
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


def fetch_market_data():
    """获取各ETF的动量/价格数据用于赛道分类。"""
    try:
        from market_data import fetch_klines_daily
        from rotation import calc_momentum_4w, calc_momentum_8w, calc_rsi_wilder
    except ImportError:
        return {}

    with open("etf_pool.json") as f:
        pool = json.load(f)
    codes = pool.get("etf_pool", [])
    names = pool.get("names", {})

    data = {}
    for code in codes:
        try:
            klines = fetch_klines_daily(code)
            if not klines or len(klines) < 20:
                continue
            closes = [k["close"] for k in klines]
            m4 = calc_momentum_4w(closes)
            rsi = calc_rsi_wilder(closes, 14)
            # 近5日涨跌幅
            pct_5d = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
            # 近20日涨跌幅
            pct_20d = (closes[-1] / closes[-20] - 1) * 100 if len(closes) >= 20 else 0
            data[code] = {
                "name": names.get(code, code),
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
    """
    将ETF归类到四个赛道：
      🥩 吃肉 — m4>8, RSI<70, 5日涨幅>2%, 情绪>=0
      🥣 喝汤 — m4>3, 5日涨幅>0, 情绪>=0
      🔄 重点关注回暖 — 20日涨幅从负转正或m4从负转正
      ⚠️ 提示风险 — RSI>70超买, 或情绪<-3, 或m4<-5
    """
    if not mkt:
        return "⏳ 数据不足"

    m4 = mkt.get("m4", 0)
    rsi = mkt.get("rsi", 50)
    pct_5d = mkt.get("pct_5d", 0)
    pct_20d = mkt.get("pct_20d", 0)

    # 风险：超买或利空情绪或大幅下跌
    if rsi > 70 or sentiment_score <= -3 or m4 < -0.05:
        return "⚠️ 提示风险"
    # 吃肉：强动量+温和RSI+短期上涨
    if m4 > 0.08 and rsi < 70 and pct_5d > 2 and sentiment_score >= 0:
        return "🥩 吃肉"
    # 回暖：20日跌幅收窄，短期回暖
    if pct_20d > -2 and pct_5d > 1 and m4 > 0:
        return "🔄 重点关注回暖"
    # 喝汤：温和正面
    if m4 > 0.03 and pct_5d > 0 and sentiment_score >= 0:
        return "🥣 喝汤"
    # 回暖：弱势回暖
    if m4 > 0 and pct_5d > 0:
        return "🔄 重点关注回暖"
    # 默认：喝汤或平稳
    if m4 > 0:
        return "🥣 喝汤"
    return "⏳ 观察中"


if __name__ == "__main__":
    now = datetime.now().strftime("%H:%M")
    print(f"🔍 舆情分析 | {now}")
    news = fetch_all()
    print(f"  抓取 {len(news)} 条新闻（新浪+东财+同花顺）\n")

    # 获取行情数据
    mkt_data = fetch_market_data()

    # 逐ETF分析
    per_etf = []
    for code, info in TRACKED.items():
        matched = []
        for n in news:
            try:
                title = n.get("title", "")
                if any(kw in title for kw in info["kw"]):
                    matched.append(title)
            except (KeyError, TypeError):
                continue

        s, neg, pos = score(matched)
        if s >= NEG_THRESHOLD_BLOCK:
            level = "🛑 利空"
        elif s >= NEG_THRESHOLD_WARN:
            level = "⚠️ 关注"
        else:
            level = "✅ 正常"

        # 赛道分类
        track = classify_track(code, mkt_data.get(code), s)

        # 行情简述
        mkt = mkt_data.get(code, {})
        mkt_str = ""
        if mkt:
            mkt_str = f"  m4={mkt['m4']*100:+.1f}%  rsi={mkt['rsi']:.0f}  5d={mkt['pct_5d']:+.1f}%"

        detail = f"  {track:<16} {level} {code} {info['name']}"
        if mkt_str:
            detail += f"\n     {mkt_str}"
        if matched:
            detail += f"\n     {matched[0][:50]}"
        print(detail)
        print()

        per_etf.append((code, info["name"], track, level, s))

    # 赛道汇总
    print("=" * 50)
    print("📊 赛道总览")
    print("=" * 50)

    categories = {"🥩 吃肉": [], "🥣 喝汤": [], "🔄 重点关注回暖": [], "⚠️ 提示风险": [], "⏳ 观察中": [], "⏳ 数据不足": []}
    for code, name, track, level, s in per_etf:
        cat = track if track in categories else "⏳ 观察中"
        categories[cat].append(f"{code} {name}")

    for cat, items in categories.items():
        if not items:
            continue
        print(f"\n{cat} ({len(items)})")
        for item in items:
            print(f"  · {item}")

    # 预警
    alerts = [(code, name) for code, name, track, level, s in per_etf if "风险" in track]
    if alerts:
        print(f"\n⚠️ 风险预警: {', '.join(f'{c} {n}' for c, n in alerts)}")
    else:
        print("\n✅ 无显著风险信号")

    # 全球板块
    print(f"\n{'=' * 50}")
    print("🌍 全局 | 美股/外盘/宏观")
    print("=" * 50)
    global_matched = []
    for n in news:
        try:
            title = n.get("title", "")
            if any(kw in title for kw in GLOBAL_KW):
                global_matched.append(title)
        except (KeyError, TypeError):
            continue
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