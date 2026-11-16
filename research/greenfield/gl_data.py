"""Greenfield ETF research — data layer (腾讯前复权 only).

Universe list comes from Sina's ETF board listing (a *list*, not prices).
All prices are Tencent 前复权 (qfq). ifzq.gtimg.cn is the working host;
web.ifzq.gtimg.cn returns WAF 501 (see task brief).
"""
from __future__ import annotations

import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "etf_universe"
DAILY = DATA / "daily"
M5 = DATA / "m5"
for _p in (DATA, DAILY, M5):
    _p.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# 4 windows so each request stays under the 1200-bar cap of the Tencent API
WINDOWS = [
    ("2014-01-01", "2017-12-31"),
    ("2018-01-01", "2020-12-31"),
    ("2021-01-01", "2023-12-31"),
    ("2024-01-01", "2026-11-30"),
]


def _get(url, timeout=20, retries=4):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200 and r.text.strip():
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(0.4 * (i + 1))
    raise RuntimeError(f"fetch failed: {last} :: {url[:120]}")


# ---------------------------------------------------------------- universe
def get_etf_list(force=False) -> pd.DataFrame:
    """Sina ETF board: symbol/code/name. Listing only — no prices used."""
    fp = DATA / "universe_raw.csv"
    if fp.exists() and not force:
        return pd.read_csv(fp, dtype={"code": str})
    rows = []
    for page in range(1, 16):
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node=etf_hq_fund")
        txt = _get(url)
        try:
            arr = json.loads(txt)
        except json.JSONDecodeError:
            arr = json.loads(txt.replace("'", '"'))
        if not arr:
            break
        rows.extend(arr)
        if len(arr) < 100:
            break
    df = pd.DataFrame(rows)
    df = df[["symbol", "code", "name"]].drop_duplicates("code")
    fp.write_text(df.to_csv(index=False), encoding="utf-8")
    return df


# ---------------------------------------------------------------- daily qfq
def fetch_daily(symbol: str) -> pd.DataFrame:
    """Tencent qfq daily bars, stitched over WINDOWS. Returns empty df on failure."""
    frames = []
    for beg, end in WINDOWS:
        url = ("https://ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={symbol},day,{beg},{end},1200,qfq")
        try:
            txt = _get(url)
            js = json.loads(txt)
        except Exception:
            continue
        node = (js.get("data") or {}).get(symbol) or {}
        bars = node.get("qfqday") or node.get("day") or []
        if not bars:
            continue
        f = pd.DataFrame([b[:6] for b in bars],
                         columns=["date", "open", "close", "high", "low", "volume"])
        frames.append(f)
        time.sleep(0.02)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df[df["date"].astype(str).str.match(r"^\d{4}-\d{2}-\d{2}$")]
    for c in ["open", "close", "high", "low", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).drop_duplicates("date").sort_values("date")
    return df.reset_index(drop=True)


def _worker(symbol: str) -> tuple[str, int, str]:
    fp = DAILY / f"{symbol}.csv"
    if fp.exists() and fp.stat().st_size > 200:
        try:
            n = sum(1 for _ in fp.open()) - 1
            return symbol, n, "cached"
        except Exception:
            pass
    try:
        df = fetch_daily(symbol)
    except Exception as e:  # noqa: BLE001
        return symbol, 0, f"ERR {e}"[:160]
    if df.empty:
        return symbol, 0, "empty"
    df.to_csv(fp, index=False)
    return symbol, len(df), "ok"


def download_daily(symbols, workers=6, force=False):
    if force:
        for s in symbols:
            fp = DAILY / f"{s}.csv"
            if fp.exists():
                fp.unlink()
    out = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_worker, s): s for s in symbols}
        for fu in as_completed(futs):
            sym, n, st = fu.result()
            out[sym] = (n, st)
            done += 1
            if done % 100 == 0:
                print(f"  [{done}/{len(symbols)}] {sym} bars={n} {st}", flush=True)
    return out


def load_daily(symbol: str) -> pd.DataFrame:
    fp = DAILY / f"{symbol}.csv"
    if not fp.exists():
        return pd.DataFrame()
    df = pd.read_csv(fp)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


def load_all(symbols=None) -> dict[str, pd.DataFrame]:
    if symbols is None:
        symbols = [p.stem for p in DAILY.glob("*.csv")]
    out = {}
    for s in symbols:
        df = load_daily(s)
        if len(df) > 30:
            out[s] = df
    return out


def panel_close(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    return pd.DataFrame({k: v["close"] for k, v in data.items()}).sort_index()


def panel_amount(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Approx turnover in RMB: volume(手)*100*close."""
    return pd.DataFrame({k: v["volume"] * 100.0 * v["close"] for k, v in data.items()}).sort_index()


# ---------------------------------------------------------------- m5
def fetch_m5(symbol: str, n=500) -> pd.DataFrame:
    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={symbol},m5,,{n}"
    txt = _get(url)
    js = json.loads(txt)
    node = (js.get("data") or {}).get(symbol) or {}
    bars = node.get("m5") or []
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame([b[:6] for b in bars],
                      columns=["dt", "open", "close", "high", "low", "volume"])
    for c in ["open", "close", "high", "low", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["dt"] = pd.to_datetime(df["dt"], format="%Y%m%d%H%M")
    return df.dropna(subset=["close"])


def _m5_worker(symbol):
    fp = M5 / f"{symbol}.csv"
    if fp.exists() and fp.stat().st_size > 200:
        return symbol, sum(1 for _ in fp.open()) - 1, "cached"
    try:
        df = fetch_m5(symbol)
    except Exception as e:  # noqa: BLE001
        return symbol, 0, f"ERR {e}"[:120]
    if df.empty:
        return symbol, 0, "empty"
    df.to_csv(fp, index=False)
    return symbol, len(df), "ok"


def download_m5(symbols, workers=6):
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_m5_worker, s): s for s in symbols}
        for fu in as_completed(futs):
            sym, n, st = fu.result()
            out[sym] = (n, st)
    return out


# ---------------------------------------------------------------- categories
CROSS_KW = ["纳指", "纳斯达克", "标普", "道琼斯", "恒生", "港股", "恒指", "H股", "日经", "日本",
            "德国", "法国", "英国", "欧洲", "美国", "中概", "海外", "亚太", "东南亚", "越南",
            "印度", "沙特", "巴西", "新兴市场", "全球", "国际", "MS­CI", "MSCI", "沙特"]
COMMOD_KW = ["黄金", "白银", "豆粕", "原油", "能源化工", "商品", "有色期货", "沪铜", "铜期货", "农业期货"]
BOND_KW = ["债", "国债", "政金", "信用", "城投", "短融", "金融债", "公司债", "转债"]
MONEY_KW = ["货币", "现金", "添益", "快线", "保证金", "日利", "理财"]
BROAD_KW = ["沪深300", "上证50", "中证500", "中证1000", "中证2000", "创业板", "科创50", "科创100",
            "科创200", "科创综指", "上证180", "深证100", "中证A500", "中证A50", "中证A100", "A500",
            "A50", "双创", "500ETF", "300ETF", "800", "深证成指", "上证综指", "中证100", "中证800",
            "中证全指", "自由现金流", "全指"]
INDUSTRY_KW = ["银行", "证券", "券商", "保险", "地产", "房地产", "医药", "医疗", "消费", "白酒",
               "食品", "饮料", "家电", "汽车", "军工", "国防", "电力", "公用", "煤炭", "钢铁",
               "有色", "化工", "建材", "农业", "养殖", "畜牧", "传媒", "游戏", "通信", "计算机",
               "电子", "芯片", "半导体", "新能源", "光伏", "电池", "储能", "机械", "基建", "物流",
               "港口", "航空", "旅游", "教育", "软件", "云计算", "创新药", "生物", "中药", "疫苗",
               "医疗器械", "非银", "金融", "石油", "天然气", "电网", "稀土", "锂", "氢能", "风电",
               "建材", "钢铁", "养殖", "农业", "零售", "纺织", "造纸", "包装", "快递"]


def classify(name: str) -> str:
    n = str(name)
    if any(k in n for k in MONEY_KW):
        return "货币"
    if any(k in n for k in BOND_KW):
        return "债券"
    if any(k in n for k in COMMOD_KW):
        return "商品"
    if any(k in n for k in CROSS_KW):
        return "跨境QDII"
    if any(k in n for k in BROAD_KW):
        return "宽基"
    if any(k in n for k in INDUSTRY_KW):
        return "行业"
    return "主题"


# ---------------------------------------------------------------- underlying dedup
_SUFFIX = ["华泰柏瑞", "易方达", "国联安", "工银瑞信", "摩根士丹利", "施罗德", "贝莱德",
           "华安", "华夏", "国泰", "工银", "广发", "博时", "富国", "南方", "嘉实", "天弘",
           "招商", "汇添富", "银华", "景顺", "摩根", "鹏华", "海富通", "大成", "永赢",
           "平安", "中银", "建信", "交银", "浦银", "兴业", "华宝", "国投", "泰康", "人保",
           "中金", "中信", "申万菱信", "万家", "诺安", "前海", "融通", "中欧", "兴全",
           "东财", "华富", "长城", "长信", "国海", "国联", "金鹰", "华商", "民生", "路博迈",
           "范达", "国寿安保", "西部利得", "中航", "红土", "华润元大", "德邦", "东海"]
_SYN = [("纳斯达克", "纳指"), ("标普500", "标普"), ("恒指科技", "恒生科技"),
        ("港股科技", "恒生科技"), ("恒生科技指数", "恒生科技"), ("港股通科技", "恒生科技"),
        ("中概互联网", "中概互联"), ("恒生互联网", "中概互联"), ("互联中概", "中概互联"),
        ("日经225", "日经"), ("黄金9999", "黄金"), ("黄金基金", "黄金"), ("黄金ETF", "黄金")]


def underlying_key(name: str) -> str:
    n = str(name).replace("ETF", "").replace("XD", "").strip()
    for a, b in _SYN:
        n = n.replace(a, b)
    for s in _SUFFIX:
        n = n.replace(s, "")
    n = n.replace("基金", "").replace("指数", "").strip()
    return n or str(name)


def dedup_universe(u: pd.DataFrame) -> pd.DataFrame:
    u = u.copy()
    u["ukey"] = u["name"].map(underlying_key)
    u = u.sort_values("amt250", ascending=False).drop_duplicates("ukey")
    return u.sort_values("amt250", ascending=False)
