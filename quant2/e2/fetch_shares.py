#!/usr/bin/env python3
"""E2 step 1: daily ETF fund-share (份额) panel from the two exchanges.

Sources (both date-addressable, so history is real, not a snapshot):
  SH: https://query.sse.com.cn/commonQuery.do  sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L
      STAT_DATE=YYYY-MM-DD -> all SH ETFs' 基金份额 for that date  (akshare fund_etf_scale_sse)
  SZ: https://www.szse.cn/api/report/ShowReport CATALOGID=scsj_fund_jjgm
      txtStart/txtEnd (<=6 months) -> daily 基金份额 per SZ fund  (akshare fund_scale_daily_szse)

Cache: data/shares/sse/<date>.json  and  data/shares/szse/<date>.json  -> {"code": shares}
Empty results are cached as {} with a marker file so re-runs do not hammer the API.
"""
import argparse, json, os, random, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed

import akshare as ak
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META = os.path.join(ROOT, "data", "meta")
OUT = os.path.join(ROOT, "data", "shares")


def calendar():
    cal = pd.DatetimeIndex(pd.to_datetime(json.load(open(os.path.join(META, "index_calendar.json")))))
    today = pd.Timestamp.now().normalize()
    return cal[cal < today]


def _fetch_sse(day):
    for k in range(5):
        try:
            df = ak.fund_etf_scale_sse(date=day.strftime("%Y%m%d"))
            if df is None or len(df) == 0:
                return day, {}
            out = {}
            for c, v in zip(df["基金代码"].astype(str), df["基金份额"]):
                try:
                    fv = float(v)
                except Exception:
                    continue
                if fv > 0:
                    out[c] = fv
            return day, out
        except Exception as e:
            if k == 4:
                return day, None
            time.sleep(0.5 * (k + 1) + random.random() * 0.4)


def pull_sse(start, end, workers=8, force=False):
    os.makedirs(os.path.join(OUT, "sse"), exist_ok=True)
    days = [d for d in calendar() if start <= d <= end]
    todo = [d for d in days if force or not os.path.exists(
        os.path.join(OUT, "sse", f"{d.date()}.json"))]
    print(f"[SSE] {len(days)} trading days, {len(todo)} to fetch", flush=True)
    ok = bad = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(_fetch_sse, d): d for d in todo}
        for i, f in enumerate(as_completed(futs), 1):
            day, res = f.result()
            if res is None:
                bad += 1
                print(f"  FAIL {day.date()}", flush=True)
            else:
                json.dump(res, open(os.path.join(OUT, "sse", f"{day.date()}.json"), "w"))
                ok += 1
            if i % 200 == 0:
                print(f"  {i}/{len(todo)} ok={ok} bad={bad}", flush=True)
    print(f"[SSE] done ok={ok} bad={bad}", flush=True)


def pull_szse(start, end, force=False):
    """SZSE allows <=6 month windows; use 1-month windows for safety."""
    os.makedirs(os.path.join(OUT, "szse"), exist_ok=True)
    cur = pd.Timestamp(start).replace(day=1)
    end = pd.Timestamp(end)
    while cur <= end:
        nxt = (cur + pd.offsets.MonthBegin(1)) - pd.Timedelta(days=1)
        nxt = min(nxt, end)
        tag = f"{cur.date()}_{nxt.date()}"
        path = os.path.join(OUT, "szse", f"{tag}.json")
        if os.path.exists(path) and not force:
            cur = cur + pd.offsets.MonthBegin(1)
            continue
        got = {}
        for k in range(4):
            try:
                df = ak.fund_scale_daily_szse(start_date=cur.strftime("%Y%m%d"),
                                              end_date=nxt.strftime("%Y%m%d"), symbol="ETF")
                if df is not None and len(df):
                    for d, c, v in zip(pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d"),
                                       df["基金代码"].astype(str), df["基金份额"]):
                        try:
                            fv = float(v)
                        except Exception:
                            continue
                        if fv > 0:
                            got.setdefault(d, {})[c] = fv
                json.dump(got, open(path, "w"))
                print(f"[SZSE] {tag}: {len(got)} days", flush=True)
                break
            except Exception as e:
                if k == 3:
                    print(f"[SZSE] {tag} FAIL {e}", flush=True)
                time.sleep(1.0 * (k + 1))
        cur = cur + pd.offsets.MonthBegin(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--only", default="both")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--szse-start", default="2016-11-01")
    a = ap.parse_args()
    end = pd.Timestamp(a.end) if a.end else calendar()[-1]
    if a.only in ("both", "sse"):
        pull_sse(pd.Timestamp(a.start), end, force=a.force)
    if a.only in ("both", "szse"):
        pull_szse(pd.Timestamp(a.szse_start), end, force=a.force)
