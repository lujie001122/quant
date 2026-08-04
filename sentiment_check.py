"""舆情扫描 v3.2 — Sina滚动新闻 + 情感分词
- 健壮异常处理
- 修复bare except
- 修复KeyError
- v3.2: 从 etf_pool.json 动态读取标的和关键词
"""
import json, urllib.request, logging, os
from datetime import datetime
from tracker import get_tracked_with_keywords

logger = logging.getLogger("sentiment_check")
logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")

TRACKED = get_tracked_with_keywords()

NEG = {"利空": 3, "暴跌": 3, "崩盘": 3, "减持": 2, "亏损": 2, "下滑": 1,
       "预警": 2, "暴雷": 3, "跌停": 3, "退市": 3, "制裁": 3, "关税": 3,
       "踩踏": 2, "抛售": 3, "危机": 3, "风险": 1, "债务": 2, "违约": 2}
POS = {"利好": 3, "涨停": 3, "突破": 2, "大增": 2, "预增": 2, "回购": 2,
       "增持": 2, "分红": 1, "扶持": 2, "超预期": 2, "新高": 2, "反弹": 1,
       "领涨": 2, "走强": 1}

NEG_THRESHOLD_WARN = 3   # 关注阈值
NEG_THRESHOLD_BLOCK = 6  # 利空阈值


def fetch():
    url = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&k=&num=30&page=1"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        })
        raw = urllib.request.urlopen(req, timeout=10).read()
        data = json.loads(raw)
        return data.get("result", {}).get("data", [])
    except Exception as e:
        logger.warning(f"舆情抓取失败: {e}")
        return []


def score(titles):
    n, p = 0, 0
    for t in titles:
        n += sum(v for k, v in NEG.items() if k in t)
        p += sum(v for k, v in POS.items() if k in t)
    return n - p, n, p


print(f"🔍 舆情 | {datetime.now().strftime('%H:%M')}")
news = fetch()
print(f"  抓取{len(news)}条新闻")
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
