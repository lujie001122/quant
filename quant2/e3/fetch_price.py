#!/usr/bin/env python3
"""E3 step 0: fill the UNADJUSTED daily close cache for every code in the E2 panel.

Premium/discount must be measured on raw close vs unit NAV, so the qfq panel is unusable.
E1 already cached 283 cross-border/commodity codes; E3 needs the full ~800 name pool because
the live strategy rotates over the whole ETF pool. Same Tencent endpoint as E1, no fq flag.

Cache: data/price/{code}.json -> {"code","name","bars":[[date,o,c,h,l,vol,amt]],"complete":bool}
"""
import argparse, json, os, random, csv, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
import threading
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("E3_DATA", os.path.join(ROOT, "data"))
PRICE = os.path.join(DATA, "price")
PXQ = os.path.join(DATA, "pxq")
META = os.path.join(DATA, "meta")
URL = "https://ifzq.gtimg.cn/appstock/app/newfqkline/get"
START = "2009-01-01"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
           "Referer": "https://gu.qq.com/"}

_tl = threading.local()


def sess():
    if not hasattr(_tl, "s"):
        s = requests.Session(); s.headers.update(HEADERS); _tl.s = s
    return _tl.s


def last_settled_day():
    cal = json.load(open(os.path.join(META, "index_calendar.json")))
    today = datetime.now().strftime("%Y-%m-%d")
    return [d for d in cal if d < today][-1]


def request_bars(code, start, end, count=640, retries=4):
    last = None
    for k in range(retries):
        try:
            r = sess().get(URL, params={"param": f"{code},day,{start},{end},{count},"}, timeout=20)
            if r.status_code != 200:
                raise RuntimeError(f"http {r.status_code}")
            d = r.json()
            if d.get("code") != 0:
                raise RuntimeError(f"api {d.get('msg')}")
            node = d.get("data", {}).get(code, {})
            if not isinstance(node, dict):
                return [], None
            return node.get("day") or [], None
        except Exception as e:
            last = e
            time.sleep(0.3 * (k + 1) + random.random() * 0.3)
    raise RuntimeError(f"{code} failed after {retries}: {last}")


def parse_row(r):
    return [r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]),
            float(r[8]) * 10000.0 if len(r) > 8 and str(r[8]) not in ("", "0.00") else None]


def fetch_one(code, end):
    path = os.path.join(PRICE, f"{code}.json")
    if os.path.exists(path):
        try:
            d = json.load(open(path))
            if d.get("bars") and d.get("complete"):
                return code, len(d["bars"]), "cached"
        except Exception:
            pass
    rows, cur_end, complete = {}, end, False
    for _ in range(30):
        arr, _nm = request_bars(code, START, cur_end)
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
    bars = [rows[k] for k in sorted(rows)]
    json.dump({"code": code, "name": None, "bars": bars, "complete": complete,
               "src": "tencent/newfqkline day (unadjusted)"}, open(path, "w"), ensure_ascii=False)
    return code, len(bars), ("new" if bars else "empty")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    os.makedirs(PRICE, exist_ok=True)
    codes = sorted(f[:-5] for f in os.listdir(PXQ) if f.endswith(".json"))
    if not a.all:
        codes = [c for c in codes if not os.path.exists(os.path.join(PRICE, f"{c}.json"))]
    end = last_settled_day()
    print(f"price fetch: {len(codes)} codes, {a.workers} workers, end={end}", flush=True)
    t0 = time.time(); n = 0; empty = []
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(fetch_one, c, end): c for c in codes}
        for f in as_completed(futs):
            code, k, st = f.result(); n += 1
            if st == "empty" or k < 200:
                empty.append((code, k))
            if n % 100 == 0:
                print(f"  {n}/{len(codes)} {time.time()-t0:.1f}s", flush=True)
    print(f"done {len(codes)} in {time.time()-t0:.1f}s; thin/empty {len(empty)}", flush=True)
    print("thin:", empty[:20], flush=True)


if __name__ == "__main__":
    main()
