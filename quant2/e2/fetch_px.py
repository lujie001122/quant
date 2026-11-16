#!/usr/bin/env python3
"""E2 step 2: daily qfq close for the ETF universe (Tencent newfqkline, fq=qfq).

qfq is used (not unadjusted) because E2 needs TOTAL RETURN for forward excess returns;
dividends would otherwise show up as fake negative weeks. Breadth/amount also captured for
the liquidity filter.

Cache: data/pxq/{code}.json -> {"code","name","bars":[[date,o,c,h,l,vol,amt]],"complete":bool}
"""
import argparse, csv, json, os, random, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "pxq")
META = os.path.join(ROOT, "data", "meta")
URL = "https://ifzq.gtimg.cn/appstock/app/newfqkline/get"
START = "2014-01-01"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
           "Referer": "https://gu.qq.com/"}

_tl = threading.local()


def sess():
    if not hasattr(_tl, "s"):
        s = requests.Session(); s.headers.update(HEADERS)
        _tl.s = s
    return _tl.s


def last_settled_day():
    cal = json.load(open(os.path.join(META, "index_calendar.json")))
    today = datetime.now().strftime("%Y-%m-%d")
    return [d for d in cal if d < today][-1]


def request_bars(code, start, end, count=640, retries=4):
    params = {"param": f"{code},day,{start},{end},{count},qfq"}
    last = None
    for k in range(retries):
        try:
            r = sess().get(URL, params=params, timeout=20)
            if r.status_code != 200:
                raise RuntimeError(f"http {r.status_code}")
            d = r.json()
            if d.get("code") != 0:
                raise RuntimeError(f"api {d.get('msg')}")
            node = d.get("data", {}).get(code, {})
            if not isinstance(node, dict):
                return [], None
            arr = node.get("qfqday") or node.get("day") or []
            name = None
            qt = node.get("qt", {}).get(code)
            if isinstance(qt, list) and len(qt) > 1:
                name = qt[1]
            return arr, name
        except Exception as e:
            last = e
            time.sleep(0.4 * (k + 1) + random.random() * 0.3)
    raise RuntimeError(f"{code} failed after {retries}: {last}")


def parse_row(r):
    dt, o, c, h, l, v = r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
    amt = float(r[8]) * 10000.0 if len(r) > 8 and str(r[8]) not in ("", "0.00") else None
    return [dt, o, c, h, l, v, amt]


def fetch_one(code, end, force=False):
    path = os.path.join(OUT, f"{code}.json")
    if os.path.exists(path) and not force:
        try:
            d = json.load(open(path))
            if d.get("bars") and d.get("complete"):
                return code, len(d["bars"]), "cached"
        except Exception:
            pass
    rows, name, cur_end, complete = {}, None, end, False
    for _ in range(30):
        arr, nm = request_bars(code, START, cur_end)
        if nm:
            name = nm
        if not arr:
            complete = True
            break
        fresh = 0
        for r in arr:
            try:
                p = parse_row(r)
            except Exception:
                continue
            if p[0] > end:
                continue
            if p[0] not in rows:
                fresh += 1
            rows[p[0]] = p
        first = min(x[0] for x in arr)
        if first <= START or fresh == 0:
            complete = True
            break
        cur_end = date.fromordinal(datetime.strptime(first, "%Y-%m-%d").toordinal() - 1).strftime("%Y-%m-%d")
        time.sleep(0.05)
    bars = [rows[k] for k in sorted(rows)]
    json.dump({"code": code, "name": name, "bars": bars, "complete": complete,
               "src": "tencent/newfqkline day qfq"}, open(path, "w"), ensure_ascii=False)
    return code, len(bars), ("new" if bars else "empty")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2014-01-01", help="only codes with first trade <= this")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    end = last_settled_day()
    codes = [r["code"] for r in csv.DictReader(open(os.path.join(META, "universe.csv")))
             if r["first"] <= a.since]
    print(f"px fetch end={end} codes={len(codes)}", flush=True)
    t0 = time.time(); n = 0; empty = []
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(fetch_one, c, end, a.force): c for c in codes}
        for f in as_completed(futs):
            code, k, st = f.result(); n += 1
            if st == "empty":
                empty.append(code)
            if n % 200 == 0:
                print(f"  {n}/{len(codes)} {time.time()-t0:.1f}s", flush=True)
    print(f"done {len(codes)} in {time.time()-t0:.1f}s; empty={len(empty)} {empty[:10]}", flush=True)


if __name__ == "__main__":
    main()
