"""舆情扫描 v4.0 — 多源新闻抓取（新浪 + 东财 + 同花顺）+ 情感分词
- 三源合并，按标题去重
- 健壮异常处理
- 保留 v3.2 评分逻辑和输出格式
- v4.0: 新增 fetch_eastmoney() / fetch_10jqka()
"""
import json, urllib.request, re, logging, os
from datetime import datetime
from tracker import get_tracked_with_keywords

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
    # 美股指数
    "美股", "纳指", "纳斯达克", "标普", "道指", "道琼斯",
    # 外盘/海外
    "外盘", "海外市场", "全球市场", "欧股", "日经", "恒生",
    "港股", "A股", "沪深", "创业板", "科创板",
    # 极端行情
    "大跌", "暴跌", "崩盘", "熔断", "狂泻", "重挫", "跳水",
    "暴涨", "狂飙", "井喷",
    # 宏观政策
    "加息", "降息", "美联储", "通胀", "非农", "CPI", "PPI",
    "GDP", "PMI", "央行", "货币政策", "财政政策",
    # 汇率/地缘
    "汇率", "人民币", "美元", "中美", "关税", "贸易战",
    "地缘", "制裁", "冲突",
]

NEG_THRESHOLD_WARN = 3   # 关注阈值
NEG_THRESHOLD_BLOCK = 6  # 利空阈值

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def fetch_sina():
    """新浪滚动新闻 — 返回 [{\"title\": ..., \"digest\": ...}]"""
    url = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&k=&num=30&page=1"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Referer": "https://finance.sina.com.cn/",
        })
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
    """同花顺快讯 — 返回 [{\"title\": ..., \"digest\": ...}]"""
    url = "https://news.10jqka.com.cn/tapp/news/push/stock/"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Referer": "https://news.10jqka.com.cn/",
        })
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
    """东财要闻 — 返回 [{\"title\": ...}]"""
    url = "https://finance.eastmoney.com/a/cgsxw.html"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Referer": "https://finance.eastmoney.com/",
        })
        raw = urllib.request.urlopen(req, timeout=10).read()
        # 东财页面实际编码为 utf-8（非 gb2312）
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gb2312", errors="replace")
        # 提取 <div class="title"><a>TITLE</a>
        pattern = r'<div class="title">\s*<a[^>]*>([^<]+)</a>'
        titles = re.findall(pattern, text)
        return [{"title": t.strip()} for t in titles if t.strip()]
    except Exception as e:
        logger.warning(f"东财抓取失败: {e}")
        return []


def fetch_all():
    """合并三源新闻，按标题去重"""
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
    """构建关键词列表: [name, name+ETF, code] + industry_kw"""
    kw = [name]
    if not name.endswith("ETF"):
        kw.append(name + "ETF")
    kw.append(code)
    if industry_kw_map:
        for k in industry_kw_map.get(code, []):
            if k not in kw:
                kw.append(k)
    return kw


if __name__ == "__main__":
    print(f"🔍 舆情 | {datetime.now().strftime('%H:%M')}")
    news = fetch_all()
    print(f"  抓取{len(news)}条新闻（新浪+东财+同花顺）")
    alerts = []

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
        detail = f"  {level} {code} {info['name']} (负{neg}/正{pos})"
        if matched:
            detail += f"\n    {matched[0][:50]}"
            if len(matched) > 1:
                detail += f"\n    {matched[1][:50]}"
        print(detail)
        if s >= NEG_THRESHOLD_WARN:
            alerts.append(code)

    if alerts:
        print(f"\n⚠️ 预警: {', '.join(alerts)} 建议手动确认")
    else:
        print("\n✅ 无利空，正常交易")

    # ── 全局板块：美股/外盘/宏观关键词匹配 ──
    print(f"\n🌍 全局 | 美股/外盘/宏观")
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