#!/usr/bin/env python3
"""flow_data.py — 流量策略每日数据管道（盘后跑一次）。

三份数据，全部按"数据自己的日期"落库，绝不用"今天"去标记（PIT 纪律的根）：
  份额  上交所 query.sse.com.cn/commonQuery.do (sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L)
        深交所 szse.cn/api/report/ShowReport (CATALOGID=scsj_fund_jjgm, ETF)  → 2 个请求/天
        落库 data/shares/{sse,szse}/   +  当日快照 data/flows/YYYYMMDD.json
  净值  东财 fund.eastmoney.com/pingzhongdata/{code}.js         → data/nav/{code}.json
  不复权价 腾讯 qt.gtimg.cn 批量快照（溢价必须用不复权价，前复权会算错）
        落库 data/price/{code}.json（不复权日线） + data/pxq/{code}.json（前复权，收益/成交额用）

PIT 规则
  * 份额 T 日晚间公布 → 面板按 lag=1 使用 T-1（e3data.build(lag=1)），当日份额不会进当日信号
  * 溢价 = 不复权收盘 T / 净值(T-1) - 1（e3data prem_lag=1）
  * 抓不到就不写：缺净值的标的 prem=NaN → 当天不新建仓（不用旧值硬填）
  * 交易日历只追加真实成交过的日期（sh000001 日线），不猜

用法
  .venv/bin/python3 flow_data.py                 # 每日：份额 + 净值(候选池) + 价格 + 快照
  .venv/bin/python3 flow_data.py --nav-all       # 净值全池刷新（约 800 只，慢）
  .venv/bin/python3 flow_data.py --pxq-all       # 前复权全池刷新（周末跑一次即可）
  .venv/bin/python3 flow_data.py --check         # 只体检：缓存覆盖到哪天、各多少只
"""
import argparse
import io
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "quant2", "e3"))
sys.path.insert(0, os.path.join(_HERE, "quant2", "e4"))

DATA = os.environ.get("E3_DATA", os.path.join(_HERE, "quant2", "data"))
SHARES = os.path.join(DATA, "shares")
NAV = os.path.join(DATA, "nav")
PRICE = os.path.join(DATA, "price")
PXQ = os.path.join(DATA, "pxq")
META = os.path.join(DATA, "meta")
FLOWS = os.path.join(_HERE, "data", "flows")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")

_tl = threading.local()


def sess():
    if not hasattr(_tl, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        _tl.s = s
    return _tl.s


# ════════════════════════════════════════════════════════════
# 交易日历
# ════════════════════════════════════════════════════════════
def calendar():
    with open(os.path.join(META, "index_calendar.json")) as f:
        return list(json.load(f))


def _index_days(start="2026-01-01"):
    """sh000001 日线日期（真实成交过才有）——只用来追加日历，不覆盖历史。"""
    r = sess().get("https://ifzq.gtimg.cn/appstock/app/newfqkline/get",
                   params={"param": f"sh000001,day,{start},{datetime.now():%Y-%m-%d},640,"},
                   headers={"Referer": "https://gu.qq.com/"}, timeout=25)
    j = r.json()
    node = (j.get("data") or {}).get("sh000001") or {}
    return [b[0] for b in (node.get("day") or [])]


def update_calendar(verbose=True):
    """把日历追加到最新真实交易日（只加不减，历史日期一律不动）。"""
    cal = calendar()
    last = cal[-1]
    try:
        new = [d for d in _index_days(last) if d > last]
    except Exception as e:
        print(f"[calendar] 拉 sh000001 失败({e})，日历不追加", file=sys.stderr)
        return 0, last
    if new:
        cal = sorted(set(cal) | set(new))
        with open(os.path.join(META, "index_calendar.json"), "w") as f:
            json.dump(sorted(cal), f)
        if verbose:
            print(f"[calendar] 追加 {len(new)} 个交易日: {new[0]} … {cal[-1]}")
    elif verbose:
        print(f"[calendar] 无新增（最后交易日 {last}）")
    return len(new), cal[-1]


def target_day():
    """本次落库对应的交易日：收盘后取今天（若是交易日），否则取最近一个交易日。"""
    cal = calendar()
    today = datetime.now().strftime("%Y-%m-%d")
    if today in cal and datetime.now().strftime("%H%M") >= "1505":
        return today
    past = [d for d in cal if d < today]
    return past[-1] if past else cal[-1]


# ════════════════════════════════════════════════════════════
# 1) 份额：上交所 + 深交所（2 个请求/天）
# ════════════════════════════════════════════════════════════
def fetch_sse_shares(day, back=6):
    """上交所基金份额。返回 (published_date, {code: shares})。

    STAT_DATE 只认接口自己回传的日期：T 日份额当晚才公布，抓不到就往前退，
    绝不把 T-1 的数标成 T。
    """
    cal = [d for d in calendar() if d <= day][-back:]
    for d in reversed(cal):
        try:
            r = sess().get("https://query.sse.com.cn/commonQuery.do",
                           params={"sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
                                   "STAT_DATE": d},
                           headers={"Referer": "https://www.sse.com.cn/"}, timeout=25)
            res = (r.json() or {}).get("result") or []
        except Exception as e:
            print(f"[sse] {d} 请求失败: {e}", file=sys.stderr)
            continue
        if not res:
            continue
        out = {}
        for row in res:
            code = str(row.get("SEC_CODE") or "").strip()
            vol = row.get("TOT_VOL")
            if not code or vol in (None, ""):
                continue
            try:
                v = float(vol) * 10000.0          # 接口给"万份"
            except Exception:
                continue
            if v > 0:
                out[code] = v
        if out:
            return str(res[0].get("STAT_DATE") or d), out
    return None, {}


def fetch_szse_shares(day, months_back=2):
    """深交所基金份额（xlsx，窗口 ≤6 个月）。返回 {date: {code: shares}}（多日一次拿回）。"""
    end = pd.Timestamp(day)
    got = {}
    for k in range(months_back):
        start = (end.replace(day=1) - pd.offsets.MonthBegin(k)).replace(day=1)
        stop = min((start + pd.offsets.MonthBegin(1)) - pd.Timedelta(days=1), end)
        try:
            r = sess().get("https://www.szse.cn/api/report/ShowReport",
                           params={"SHOWTYPE": "xlsx", "CATALOGID": "scsj_fund_jjgm",
                                   "TABKEY": "tab1", "txtStart": str(start.date()),
                                   "txtEnd": str(stop.date()), "jjlb": "ETF",
                                   "random": str(random.random())},
                           headers={"Host": "www.szse.cn",
                                    "Referer": "https://www.szse.cn/market/fund/volume/etf/index.html"},
                           timeout=40)
            if r.status_code != 200 or not r.content:
                continue
            df = pd.read_excel(io.BytesIO(r.content), engine="openpyxl")
        except Exception as e:
            print(f"[szse] {start.date()}~{stop.date()} 失败: {e}", file=sys.stderr)
            continue
        if df is None or len(df) == 0:
            continue
        for d, c, v in zip(pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d"),
                           df["基金代码"].astype(str).str.zfill(6),
                           df["基金规模(份)"]):
            try:
                fv = float(v)
            except Exception:
                continue
            if fv > 0:
                got.setdefault(d, {})[c] = fv
        if got:
            break
    return got


def save_shares(sse_pub, sse, szse_by_date):
    """写份额缓存（sse 按日一文件；szse 按月窗口一文件，增量合并）。"""
    os.makedirs(os.path.join(SHARES, "sse"), exist_ok=True)
    os.makedirs(os.path.join(SHARES, "szse"), exist_ok=True)
    if sse_pub and sse:
        p = os.path.join(SHARES, "sse", f"{sse_pub}.json")
        old = {}
        if os.path.exists(p):
            try:
                old = json.load(open(p))
            except Exception:
                old = {}
        old.update(sse)
        with open(p, "w") as f:
            json.dump(old, f)
    n_days = 0
    for d, vals in (szse_by_date or {}).items():
        ts = pd.Timestamp(d)
        start = ts.replace(day=1).date()
        stop = ((ts.replace(day=1) + pd.offsets.MonthBegin(1)) - pd.Timedelta(days=1)).date()
        p = os.path.join(SHARES, "szse", f"{start}_{stop}.json")
        cur = {}
        if os.path.exists(p):
            try:
                cur = json.load(open(p))
            except Exception:
                cur = {}
        cur.setdefault(d, {}).update(vals)
        with open(p, "w") as f:
            json.dump(cur, f)
        n_days += 1
    return n_days


# ════════════════════════════════════════════════════════════
# 2) 净值：东财 pingzhongdata
# ════════════════════════════════════════════════════════════
def _json_block(txt, key):
    m = re.search(key + r"\s*=\s*(\[.*?\])\s*;", txt, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(1))
    except Exception:
        return []


def fetch_nav_one(code, retries=3):
    """单只净值（全量 series）。返回 (code, ncodes_written|None, last_date)。"""
    path = os.path.join(NAV, f"{code}.json")
    old = None
    if os.path.exists(path):
        try:
            old = json.load(open(path))
        except Exception:
            old = None
    last = None
    if old and old.get("rows"):
        last = old["rows"][-1][0]
    for k in range(retries):
        try:
            r = sess().get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                           headers={"Referer": "https://fund.eastmoney.com/"}, timeout=25)
            if r.status_code == 514:
                time.sleep(2.0 + k)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"http {r.status_code}")
            txt = r.text
            if "Data_netWorthTrend" not in txt:
                return code, None, last
            net = _json_block(txt, "Data_netWorthTrend")
            rows = []
            for it in net:
                if it.get("y") is None:
                    continue
                ts = int(it["x"])
                d0 = datetime.utcfromtimestamp(ts / 1000 + 8 * 3600).strftime("%Y-%m-%d")
                rows.append([d0, float(it["y"])])
            rows.sort(key=lambda x: x[0])
            if not rows:
                return code, None, last
            nm = re.search(r'fS_name\s*=\s*"([^"]*)"', txt)
            with open(path, "w") as f:
                json.dump({"code": code, "ok": True, "name": nm.group(1) if nm else None,
                           "rows": rows, "src": "eastmoney/pingzhongdata"}, f,
                          ensure_ascii=False)
            return code, len(rows), rows[-1][0]
        except Exception as e:
            if k == retries - 1:
                print(f"[nav] {code} 失败: {e}", file=sys.stderr)
            time.sleep(0.4 * (k + 1))
    return code, None, last


# ════════════════════════════════════════════════════════════
# 3) 价格：腾讯批量快照（不复权）+ 单只日线（前复权）
# ════════════════════════════════════════════════════════════
def _sids(codes):
    return [("sh" + c if c.startswith(("5", "6")) else "sz" + c) for c in codes]


def fetch_quotes(codes, chunk=60):
    """腾讯批量行情：{code: {name, price, prev_close, vol_lots, amount, date}}。"""
    out = {}
    sid2code = {s: c for s, c in zip(_sids(codes), codes)}
    sids = list(sid2code)
    for i in range(0, len(sids), chunk):
        part = sids[i:i + chunk]
        try:
            r = sess().get("https://qt.gtimg.cn/q=" + ",".join(part),
                           headers={"Referer": "https://gu.qq.com/"}, timeout=20)
            r.encoding = "gbk"
            for line in r.text.split(";"):
                if "=" not in line:
                    continue
                sid = line.split("=", 1)[0].strip().replace("v_", "")
                code = sid2code.get(sid)
                f = line.split("=", 1)[1].strip().strip('"').split("~")
                if not code or len(f) < 38:
                    continue
                try:
                    out[code] = dict(name=f[1], price=float(f[3]), prev_close=float(f[4]),
                                     vol_lots=float(f[6] or 0),
                                     amount=float(f[37] or 0) * 10000.0,
                                     date=datetime.strptime(f[30][:8], "%Y%m%d").strftime("%Y-%m-%d")
                                     if len(f[30]) >= 8 else None)
                except Exception:
                    continue
        except Exception as e:
            print(f"[quote] 批量行情失败({len(part)}只): {e}", file=sys.stderr)
    return out


def save_price_bars(quotes, day):
    """把当天不复权收盘写进 price 缓存（同一天重复跑覆盖，不重复追加）。"""
    os.makedirs(PRICE, exist_ok=True)
    n = 0
    for code, q in quotes.items():
        if not q.get("price") or q.get("date") != day:
            continue
        sid = "sh" + code if code.startswith(("5", "6")) else "sz" + code
        p = os.path.join(PRICE, f"{sid}.json")
        cur = {"code": sid, "name": q.get("name"), "bars": [], "complete": True}
        if os.path.exists(p):
            try:
                cur = json.load(open(p))
            except Exception:
                pass
        bars = cur.get("bars") or []
        c = q["price"]
        row = [day, c, c, c, c, q.get("vol_lots") or 0.0, q.get("amount") or 0.0]
        if bars and bars[-1][0] == day:
            bars[-1] = row
        elif not bars or bars[-1][0] < day:
            bars.append(row)
        else:
            continue
        cur["bars"] = bars
        cur.setdefault("src", "tencent/qt.gtimg.cn (unadjusted close)")
        with open(p, "w") as f:
            json.dump(cur, f, ensure_ascii=False)
        n += 1
    return n


def fetch_pxq_one(code, day):
    """单只前复权日线（腾讯 newfqkline, qfq=是）。只补 day 这一根。"""
    sid = "sh" + code if code.startswith(("5", "6")) else "sz" + code
    p = os.path.join(PXQ, f"{sid}.json")
    cur = {"code": sid, "name": None, "bars": [], "complete": True}
    if os.path.exists(p):
        try:
            cur = json.load(open(p))
        except Exception:
            pass
    bars = cur.get("bars") or []
    if bars and bars[-1][0] >= day:
        return code, "cached"
    try:
        r = sess().get("https://ifzq.gtimg.cn/appstock/app/newfqkline/get",
                       params={"param": f"{sid},day,{day},{day},10,qfq"},
                       headers={"Referer": "https://gu.qq.com/"}, timeout=20)
        node = (r.json().get("data") or {}).get(sid) or {}
        arr = node.get("qfqday") or node.get("day") or []
    except Exception as e:
        return code, f"fail {e}"
    if not arr:
        return code, "empty"
    for row in arr:
        d0, o, c, h, l, v = row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
        amt = float(row[8]) * 10000.0 if len(row) > 8 and str(row[8]) not in ("", "0.00") else None
        nb = [d0, o, c, h, l, v, amt]
        if bars and bars[-1][0] == d0:
            bars[-1] = nb
        elif not bars or bars[-1][0] < d0:
            bars.append(nb)
    cur["bars"] = bars
    with open(p, "w") as f:
        json.dump(cur, f, ensure_ascii=False)
    return code, "ok"


# ════════════════════════════════════════════════════════════
# 4) 候选池（净值/前复权只刷这些，约 60 只/天）
# ════════════════════════════════════════════════════════════
def candidate_codes():
    """今天可能被交易的标的：持仓 + 当前目标 20 只 + 打分前 2N 名。

    净值只刷这一层：既控制请求量，又保证"能买的标的当天都有新溢价"，
    刷不到的标的 prem=NaN → 当天不新建仓（不用旧值硬填）。
    """
    import flow_strategy as F
    cfg = F.load_cfg()
    tg = F.latest_targets(cfg=cfg)
    codes = set(tg["targets"].keys())
    try:
        hold, _ = F.read_holdings()
        codes |= set(hold)
    except Exception:
        pass
    try:
        d = F.load_panel(cfg["flow_lag"], None)
        c = F.context(d, cfg)
        score = F.B.smooth(-F.B.zr(c["f"].where(c["keep"])), int(cfg["smooth_weeks"]))
        last = c["weeks"][-1]
        s = score.loc[last].where(c["keep"].loc[last].fillna(False)).dropna()
        codes |= set(s.sort_values(ascending=False).index[:int(cfg["buffer_n"])])
    except Exception as e:
        print(f"[cand] 取打分前 N 失败: {e}", file=sys.stderr)
    return sorted(codes)


def all_pool_codes():
    with open(os.path.join(META, "universe.csv")) as f:
        import csv
        return sorted({r["code"][2:] for r in csv.DictReader(f)
                       if r["code"][:2] in ("sh", "sz")})


# ════════════════════════════════════════════════════════════
# 5) 主流程
# ════════════════════════════════════════════════════════════
def snapshot(day, sse_pub, sse, szse, nav_last, quotes, path=None):
    """当日快照 data/flows/YYYYMMDD.json：份额 + 净值 + 不复权收盘 + PIT 标记。

    份额只收"已公布且 ≤ 当日"的：上交所按接口回传的 STAT_DATE，深交所取其最新已公布日。
    shares_asof 记录真实公布日，绝不把 T-1 的份额标成 T。
    """
    os.makedirs(FLOWS, exist_ok=True)
    shares = {}
    szse_days = sorted([d for d in (szse or {}) if d <= day])
    szse_asof = szse_days[-1] if szse_days else None
    if szse_asof:
        shares.update(szse[szse_asof])
    shares.update(sse or {})
    asofs = [d for d in (sse_pub, szse_asof) if d]
    shares_asof = max(asofs) if asofs else None
    snap = dict(
        date=day,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        shares_asof=shares_asof,
        shares=shares,
        shares_source=dict(sse_published=sse_pub, sse_n=len(sse or {}),
                           szse_published=szse_asof,
                           szse_n=len(shares) - len(sse or {})),
        nav={c: v for c, v in (nav_last or {}).items()},
        close_raw={c: q["price"] for c, q in (quotes or {}).items() if q.get("price")},
        amount={c: q.get("amount") for c, q in (quotes or {}).items() if q.get("amount")},
        pit=dict(shares_lag_days=1, nav_lag_days=1, price_lag_days=0,
                 note="份额/净值都是 T 日盘后已公布口径，面板按 lag=1 使用；价格为 T 日不复权收盘"),
        source=dict(shares="sse.com.cn/commonQuery + szse.cn/ShowReport(scsj_fund_jjgm)",
                    nav="fund.eastmoney.com/pingzhongdata",
                    price="qt.gtimg.cn batch quote (unadjusted)"),
    )
    p = path or os.path.join(FLOWS, f"{day.replace('-', '')}.json")
    with open(p, "w") as f:
        json.dump(snap, f, ensure_ascii=False)
    return p, snap


def run(nav_all=False, pxq_all=False, workers=10, verbose=True):
    t0 = time.time()
    update_calendar(verbose=verbose)
    day = target_day()

    # 1) 份额（2 个请求）
    sse_pub, sse = fetch_sse_shares(day)
    szse = fetch_szse_shares(day)
    ndays = save_shares(sse_pub, sse, szse)
    szse_today = len((szse or {}).get(day) or {})
    if verbose:
        print(f"[份额] 目标日={day} 上交所 {len(sse)} 只(公布日 {sse_pub}) | "
              f"深交所 {szse_today} 只(本窗口 {ndays} 天)")

    # 2) 候选池
    codes = all_pool_codes() if (nav_all or pxq_all) else candidate_codes()
    if verbose:
        print(f"[池] {'全池' if (nav_all or pxq_all) else '候选'}: {len(codes)} 只")

    # 3) 净值（东财）
    nav_last, nav_ok, nav_fail = {}, 0, 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch_nav_one, c): c for c in codes}
        for fu in as_completed(futs):
            code, n, last = fu.result()
            if n:
                nav_ok += 1
                if last:
                    nav_last[code] = last
            else:
                nav_fail += 1
    if verbose:
        n_days_nav = len({v for v in nav_last.values()})
        print(f"[净值] 刷新 {nav_ok} 只 (失败/空 {nav_fail})，最新净值日期 {max(nav_last.values()) if nav_last else '-'}"
              f"，共 {n_days_nav} 个不同日期")

    # 4) 不复权收盘（腾讯批量）+ 前复权（单只补当天）
    quotes = fetch_quotes(codes)
    n_px = save_price_bars(quotes, day)
    pxq_ok = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(fetch_pxq_one, c, day) for c in codes]
        for fu in as_completed(futs):
            _, st = fu.result()
            if st in ("ok", "cached"):
                pxq_ok += 1
    if verbose:
        print(f"[价格] 不复权收盘落库 {n_px} 只({day}) | 前复权 {pxq_ok}/{len(codes)} 只")

    # 5) 快照
    p, snap = snapshot(day, sse_pub, sse, szse, nav_last, quotes)
    if verbose:
        print(f"[快照] {p}  份额 {len(snap['shares'])} 只 / 净值 {len(snap['nav'])} 只 / "
              f"收盘 {len(snap['close_raw'])} 只  ({time.time()-t0:.1f}s)")
    return snap


def check():
    """体检：份额/净值/不复权价/前复权 各覆盖到哪天、多少只、多少天。"""
    def _dates(sub, nested=False):
        d0 = os.path.join(SHARES, sub)
        ds = []
        for fn in os.listdir(d0):
            if not fn.endswith(".json"):
                continue
            if nested:
                try:
                    ds.extend(json.load(open(os.path.join(d0, fn))).keys())
                except Exception:
                    pass
            else:
                ds.append(fn[:-5])
        return sorted(ds)

    sse_d = _dates("sse")
    szse_d = _dates("szse", nested=True)
    navs = [f for f in os.listdir(NAV) if f.endswith(".json")]
    nav_last = {}
    for fn in navs:
        try:
            rows = json.load(open(os.path.join(NAV, fn))).get("rows") or []
        except Exception:
            continue
        if rows:
            nav_last[fn[:-5]] = rows[-1][0]
    px = [f for f in os.listdir(PRICE) if f.endswith(".json")]
    px_last = {}
    for fn in px:
        try:
            bars = json.load(open(os.path.join(PRICE, fn))).get("bars") or []
        except Exception:
            continue
        if bars:
            px_last[fn[2:-5]] = bars[-1][0]
    pq = [f for f in os.listdir(PXQ) if f.endswith(".json")]
    pq_last = {}
    for fn in pq:
        try:
            bars = json.load(open(os.path.join(PXQ, fn))).get("bars") or []
        except Exception:
            continue
        if bars:
            pq_last[fn[2:-5]] = bars[-1][0]
    flows = sorted(f[:-5] for f in os.listdir(FLOWS)) if os.path.isdir(FLOWS) else []
    out = dict(
        sse=dict(days=len(sse_d), first=sse_d[0] if sse_d else None, last=sse_d[-1] if sse_d else None),
        szse=dict(days=len(set(szse_d)), first=min(szse_d) if szse_d else None,
                  last=max(szse_d) if szse_d else None),
        nav=dict(codes=len(nav_last), first=min(nav_last.values()) if nav_last else None,
                 last=max(nav_last.values()) if nav_last else None),
        price=dict(codes=len(px_last), last=max(px_last.values()) if px_last else None),
        pxq=dict(codes=len(pq_last), last=max(pq_last.values()) if pq_last else None),
        flows=dict(snapshots=len(flows), last=flows[-1] if flows else None),
    )
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nav-all", action="store_true", help="净值刷新全池（约 800 只）")
    ap.add_argument("--pxq-all", action="store_true", help="前复权全池刷新（周末跑）")
    ap.add_argument("--check", action="store_true", help="只体检不抓数")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.check:
        return 0 if check() else 0
    snap = run(nav_all=a.nav_all, pxq_all=a.pxq_all, workers=a.workers, verbose=not a.json)
    if a.json:
        print(json.dumps(snap, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
