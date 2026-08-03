#!/usr/bin/env python3
"""
ETF轮动扫描器
扫描20只ETF，计算近4周动量，选动量TOP3(不含同板块) + 防御2只写入 etf_pool.json
数据源: 腾讯前复权接口
"""

import json
import urllib.request
import sys
from datetime import datetime

# ═══════════════════════════════════════════════
#  20只ETF候选池
# ═══════════════════════════════════════════════
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


# ═══════════════════════════════════════════════
#  数据获取 (腾讯前复权)
# ═══════════════════════════════════════════════

def fetch_klines(code, sid):
    """获取日K线(腾讯前复权接口), 返回收盘价列表"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={sid},day,,,60,qfq")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        stock_data = raw.get("data", {})
        qfq_key = None
        for key in (sid, sid.lower(), sid.upper()):
            if key in stock_data:
                qfq_key = key
                break
        if not qfq_key:
            return None

        klines = stock_data[qfq_key].get("qfqday") or stock_data[qfq_key].get("day", [])
        if not klines:
            return None

        closes = []
        for item in klines:
            if len(item) >= 3:
                try:
                    closes.append(float(item[2]))  # 收盘价
                except (ValueError, IndexError):
                    continue
        return closes if len(closes) >= 20 else None
    except Exception as e:
        print(f"  [WARN] {code} 腾讯接口失败: {e}")
        return None


# ═══════════════════════════════════════════════
#  动量计算
# ═══════════════════════════════════════════════

def calc_momentum_4w(closes):
    """计算近4周(20个交易日)涨幅"""
    if closes is None or len(closes) < 20:
        return None
    current = closes[-1]
    price_4w_ago = closes[-20]
    if price_4w_ago <= 0:
        return None
    return (current - price_4w_ago) / price_4w_ago


def calc_max_drawdown(closes):
    """计算历史最大回撤(%)"""
    if closes is None or len(closes) < 2:
        return None
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        dd = (peak - c) / peak * 100
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ═══════════════════════════════════════════════
#  轮动逻辑
# ═══════════════════════════════════════════════

def run_rotation():
    """扫描20只ETF，选动量TOP3+防御TOP2，写入 etf_pool.json"""
    print("▸ ETF轮动扫描 (20只候选)")
    print(f"  日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # 1. 获取数据并计算指标
    results = {}
    for code, cfg in ETF_CANDIDATES.items():
        closes = fetch_klines(code, cfg["sid"])
        if closes is None:
            print(f"  ✗ {code} {cfg['name']:10s}  数据不足,跳过")
            continue
        momentum = calc_momentum_4w(closes)
        max_dd = calc_max_drawdown(closes)
        results[code] = {
            "name": cfg["name"],
            "sid": cfg["sid"],
            "sector": cfg["sector"],
            "momentum_4w": momentum,
            "max_drawdown": max_dd,
            "close": closes[-1],
        }
        print(f"  ✓ {code} {cfg['name']:10s}  4周动量={momentum*100:+.2f}%  回撤={max_dd:.1f}%  收盘={closes[-1]:.3f}")

    if not results:
        print("\n✗ 无有效数据,退出")
        return

    # 2. 动量TOP3 (不含同板块)
    by_momentum = sorted(
        [(code, r) for code, r in results.items() if r["momentum_4w"] is not None],
        key=lambda x: x[1]["momentum_4w"],
        reverse=True,
    )

    momentum_picks = []
    picked_sectors = set()
    for code, r in by_momentum:
        if len(momentum_picks) >= 3:
            break
        if r["sector"] not in picked_sectors:
            momentum_picks.append(code)
            picked_sectors.add(r["sector"])

    mom_names = ", ".join(f"{c}({results[c]['name']})" for c in momentum_picks)
    print(f"\n▸ 动量TOP3: {mom_names}")

    # 3. 防御TOP2: 回撤<20%且近4周正收益
    defense_candidates = [
        (code, r) for code, r in results.items()
        if r["max_drawdown"] is not None and r["max_drawdown"] < 20
        and r["momentum_4w"] is not None and r["momentum_4w"] > 0
        and code not in momentum_picks  # 不与动量TOP3重复
    ]
    defense_candidates.sort(key=lambda x: x[1]["momentum_4w"], reverse=True)

    defense_picks = []
    defense_picked_sectors = set()
    for code, r in defense_candidates:
        if len(defense_picks) >= 2:
            break
        if r["sector"] not in defense_picked_sectors:
            defense_picks.append(code)
            defense_picked_sectors.add(r["sector"])

    # 如果防御不足2只，放宽条件: 只要回撤<30%且正收益
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

    def_names = ", ".join(f"{c}({results[c]['name']})" for c in defense_picks)
    print(f"▸ 防御TOP2: {def_names}")

    # 4. 合并写入 etf_pool.json
    all_picks = momentum_picks + defense_picks
    pool = {
        "codes": all_picks,
        "names": {c: results[c]["name"] for c in all_picks},
        "sids": {c: results[c]["sid"] for c in all_picks},
        "momentum": {c: round(results[c]["momentum_4w"] * 100, 2) for c in all_picks},
        "max_drawdown": {c: round(results[c]["max_drawdown"], 1) for c in all_picks},
        "type": {c: ("动量" if c in momentum_picks else "防御") for c in all_picks},
        "updated": datetime.now().strftime("%Y-%m-%d"),
    }

    output_path = "etf_pool.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)

    print(f"\n▸ 写入 {output_path}: {len(all_picks)} 只ETF")
    for c in all_picks:
        tag = pool["type"][c]
        print(f"  {tag}  {c} {results[c]['name']:10s}  动量={pool['momentum'][c]:+.2f}%  回撤={pool['max_drawdown'][c]:.1f}%")

    return pool


# ═══════════════════════════════════════════════
#  默认ETF池 (回退用)
# ═══════════════════════════════════════════════

DEFAULT_POOL = {
    "codes": ["159516", "515880", "588170", "159532", "515050"],
    "names": {
        "159516": "半导体设备ETF",
        "515880": "通信ETF",
        "588170": "科创半导体ETF",
        "159532": "ETF",
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
    import os
    pool_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etf_pool.json")
    if os.path.exists(pool_path):
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                pool = json.load(f)
            if pool.get("codes") and len(pool["codes"]) >= 3:
                return pool
        except Exception:
            pass
    return DEFAULT_POOL


if __name__ == "__main__":
    run_rotation()
