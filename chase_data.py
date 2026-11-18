#!/usr/bin/env python3
"""chase_data.py — 追涨杀跌的每日数据管道（盘后跑一次）。

三件事，全部按"数据自己的日期"落库（PIT：份额 T-1，绝不偷看当日）：
  1) 交易日历  sh000001 日线追加真实成交日（复用 flow_data.update_calendar）
  2) 前复权日线 quant2/data/pxq/{sid}.json —— 全池增量（默认 12 线程）
       每只只拉最近 RECENT 根（datalen 形式，收盘后含当天那根），和缓存重叠段逐日比对：
         * 重叠一致 → 只追加新日期（正常日更，1 次请求/只）
         * 重叠不一致（除息导致整条 qfq 序列重标）→ 自动全量重抓（日期窗口分页，极少发生）
       带日期窗口的形式收盘后拿不到当天那根（实测 15:10 仍缺），只用于全量回补历史。
  3) 不复权日线 quant2/data/price/{sid}.json —— 腾讯批量行情一次拉完（涨跌停/实物价口径）
  份额/净值走 flow_data.run()（同一套上交所/深交所/东财管道，--shares 调用）

用法
  .venv/bin/python3 chase_data.py --update          # 盘后：日历 + 全池 K 线增量 + 份额
  .venv/bin/python3 chase_data.py --kline           # 只更 K 线（不抓份额）
  .venv/bin/python3 chase_data.py --shares          # 只更份额/净值（= flow_data.run()）
  .venv/bin/python3 chase_data.py --check           # 只体检：各缓存覆盖到哪天、多少只
  .venv/bin/python3 chase_data.py --force-full      # 全量重抓前复权（周末/发现漂移时用）
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(_HERE, "quant2", "data")
META = os.path.join(DATA, "meta")
PXQ = os.path.join(DATA, "pxq")
RAWP = os.path.join(DATA, "price")
UNIVERSE = os.path.join(META, "universe.csv")

MIN_BARS_OK = 60
END = "2026-12-31"
DRIFT = 0.002          # 重叠段收盘价相对偏差超过 0.2% 视为 qfq 重标 → 全量重抓
RECENT = 40            # 增量用 datalen 取最近 N 根（覆盖重叠校验 + 收盘后当天那根）
BENCH = "sh510300"     # 面板/信号日以它的最后一根为准（与 chase_live.BENCH 同口径）
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
URL = "https://ifzq.gtimg.cn/appstock/app/newfqkline/get"


def sess():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://gu.qq.com/"})
    return s


def write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _get(s, sid, param, retries=4):
    err = None
    for k in range(retries):
        try:
            r = s.get(URL, params={"param": param}, timeout=25)
            if r.status_code != 200:
                err = f"http {r.status_code}"
                time.sleep(1.0 + k * 1.5)
                continue
            j = r.json()
            node = (j.get("data") or {}).get(sid) or {}
            arr = node.get("qfqday") or node.get("day") or []
            return arr, (None if arr else "empty")
        except Exception as e:                               # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            time.sleep(1.0 + k * 1.5)
    return [], err


def _page(s, sid, start, end, qfq, retries=4):
    """带日期窗口（回补历史用：窗口上沿不含收盘后的当天那根）。"""
    return _get(s, sid, f"{sid},day,{start},{end},640,{'qfq' if qfq else ''}", retries)


def _recent(s, sid, n, qfq, retries=4):
    """datalen 形式（不带日期窗口）：收盘后已经含当天那根。"""
    return _get(s, sid, f"{sid},day,,,{n},{'qfq' if qfq else ''}", retries)


def _norm(arr):
    """腾讯行 → [date, open, close, high, low, vol, amount]（amount 单位元）。"""
    out = []
    for row in arr:
        try:
            d0 = str(row[0])[:10]
            o, c, h, l, v = float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
            amt = None
            if len(row) > 8 and str(row[8]) not in ("", "0.00", "None"):
                amt = float(row[8]) * 10000.0
            out.append([d0, o, c, h, l, v, amt])
        except Exception:                                    # noqa: BLE001
            continue
    return out


def fetch_recent(sid, n=RECENT, qfq=True, s=None):
    """最近 N 根（datalen 形式）。收盘后「带日期窗口」还看不到当天那根，这个形式能看到。"""
    arr, err = _recent(s or sess(), sid, n, qfq)
    return _norm(arr), err


def fetch_full(sid, first, qfq=True, s=None):
    """上市日 → END 分页全量（单页 640 根上限，按 760 天窗口切）。"""
    s = s or sess()
    rows, cur = [], (first or "2015-01-01")
    for _ in range(10):
        seg_end = min((pd.Timestamp(cur) + pd.Timedelta(days=760)).strftime("%Y-%m-%d"), END)
        arr, err = _page(s, sid, cur, seg_end, qfq)
        if err and err != "empty":
            return rows, err
        if not arr:
            break
        got = [g for g in _norm(arr) if g[0] >= cur]
        if not got:
            break
        rows.extend(got)
        if seg_end >= END:
            break
        nxt = (pd.Timestamp(rows[-1][0]) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        if nxt == cur:
            break
        cur = nxt
    ded = {r[0]: r for r in rows}
    if ded:                                  # 窗口形式不含收盘后当天那根 → 用 datalen 尾巴补
        tail, _ = fetch_recent(sid, RECENT, qfq, s)
        ded.update({r[0]: r for r in tail if r[0] > max(ded)})
    return [ded[k] for k in sorted(ded)], None


def _read_bars(path):
    try:
        return (json.load(open(path)).get("bars") or [])
    except Exception:                                        # noqa: BLE001
        return []


def update_one(job):
    """job = (sid, name, first, full)。返回 {sid, status, added, total, last, err}。"""
    sid, name, first, full = job
    out = dict(sid=sid, name=name, status="", added=0, total=0, last=None, err=None)
    p = os.path.join(PXQ, f"{sid}.json")
    old = _read_bars(p)
    s = sess()
    try:
        if full or not old:
            rows, err = fetch_full(sid, first, True, s)
            if err:
                out.update(status="fail", err=err)
                return out
            if len(rows) < 10:
                out.update(status="empty", err=f"只有{len(rows)}根")
                return out
            out.update(status="full", added=len(rows) - len(old), total=len(rows))
        else:
            last = old[-1][0]
            # 增量走 datalen：带日期窗口的 qfq 序列收盘后一段时间内没有当天那根（实测 15:10 仍缺），
            # datalen 形式有。历史回补仍用 fetch_full 的日期窗口。
            rows, err = fetch_recent(sid, RECENT, True, s)
            if err and err != "empty":
                out.update(status="fail", err=err)
                return out
            new = {r[0]: r for r in rows}
            ref = {r[0]: r for r in old[-10:]}
            drift = max((abs(new[d][2] / ref[d][2] - 1) for d in ref if d in new and ref[d][2]),
                        default=0.0)
            if drift > DRIFT:                    # 除息重标：整条序列作废，全量重抓
                rows, err = fetch_full(sid, first, True, s)
                if err:
                    out.update(status="fail", err=f"重标后全量失败 {err}")
                    return out
                out.update(status="rescale", added=len(rows) - len(old), total=len(rows))
            else:
                add = [r for r in rows if r[0] > last]
                if not add:
                    out.update(status="cached", total=len(old), last=last)
                    return out
                rows, out["status"], out["added"] = old + add, "append", len(add)
                out["total"] = len(rows)
        out["last"] = rows[-1][0]
        payload = dict(code=sid, name=name, bars=rows, complete=True,
                       src="tencent/newfqkline day qfq",
                       fetched_at=datetime.now().isoformat(timespec="seconds"))
        write_json(p, payload)
    except Exception as e:                                   # noqa: BLE001
        out.update(status="fail", err=f"{type(e).__name__}: {e}")
    return out


def universe():
    return pd.read_csv(UNIVERSE, dtype={"code": str})


def refresh_universe_stats():
    """把 universe.csv 的 last / n 列刷成缓存里的真实值（列名不变，只更新值）。"""
    uni = universe()
    last, n = [], []
    for sid in uni["code"]:
        b = _read_bars(os.path.join(PXQ, f"{sid}.json"))
        last.append(b[-1][0] if b else None)
        n.append(len(b))
    uni["last"], uni["n"] = last, n
    uni.to_csv(UNIVERSE, index=False)
    return uni


def update_kline(jobs_n=12, force_full=False, verbose=True):
    """全池前复权增量 + 不复权批量补当日。返回统计（含 stale：目标日没落库）。"""
    import flow_data as FD
    FD.update_calendar(verbose=verbose)
    day = FD.target_day()
    cal_last = FD.calendar()[-1]
    uni = universe()
    jobs = [(r.code, r.name, str(r.first)[:10], bool(force_full)) for r in uni.itertuples(index=False)]
    t0, ok, bad, added = time.time(), 0, [], 0
    with ThreadPoolExecutor(max_workers=jobs_n) as ex:
        futs = {ex.submit(update_one, j): j for j in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r["err"]:
                bad.append(r)
            else:
                ok += 1
                added += r["added"]
            if verbose and (i % 300 == 0 or i == len(jobs)):
                print(f"  K线 {i}/{len(jobs)} ok={ok} fail={len(bad)} 新增{added}根 "
                      f"{time.time() - t0:.0f}s", flush=True)
    # 不复权（涨跌停/实物价口径）：腾讯批量行情，一次拿全池当日 OHLC
    codes = [r.code[2:] for r in uni.itertuples(index=False) if r.code[:2] in ("sh", "sz")]
    quotes = FD.fetch_quotes(codes)
    n_px = FD.save_price_bars(quotes, day)
    uni2 = refresh_universe_stats()
    lasts = pd.to_datetime(uni2["last"], errors="coerce")
    latest = str(uni2.set_index("code")["last"].get(BENCH))   # 面板/信号日的闸门
    at_day = int((lasts == pd.Timestamp(day)).sum())
    stale = bool(day == cal_last and latest != day)
    res = dict(at=datetime.now().isoformat(timespec="seconds"), day=day, cal_last=cal_last,
               ok=ok, fail=len(bad), added_bars=added, raw_saved=n_px,
               latest=latest, at_day=at_day, stale=stale,
               pool=int((uni2["n"] >= MIN_BARS_OK).sum()), codes=len(uni2),
               failed=[{"code": r["sid"], "name": r["name"], "reason": r["err"]} for r in bad[:40]],
               secs=round(time.time() - t0, 1))
    if verbose:
        if stale:
            # 跑完却还是旧数据 = 谎报成功，必须让上层（cron/策略）看到非 0 退出码
            print(f"[K线][ERROR] 目标日={day} 但库内最新只到 {latest or '无'}（{BENCH}）"
                  f"——今天那根没入库，策略会用旧数据 | 成功{ok}/{len(jobs)} 失败{len(bad)} "
                  f"新增{added}根 到位{at_day}/{len(jobs)}只 | {res['secs']}s", flush=True)
        else:
            print(f"[K线] 目标日={day} 成功{ok}/{len(jobs)} 失败{len(bad)} | "
                  f"新增 {added} 根，最新日期 {latest} | 到位{at_day}/{len(jobs)}只 "
                  f"不复权落库{n_px}只 | 池子{n_pool_str(res['pool'], res['codes'])} | {res['secs']}s",
                  flush=True)
    return res


def n_pool_str(pool, codes):
    return f"{pool}/{codes} 只（≥{MIN_BARS_OK}根）"


def update_shares(verbose=True):
    """份额 + 净值（复用 flow_data.run，PIT: 份额 T-1 / 净值 T-1）。"""
    import flow_data as FD
    snap = FD.run(verbose=verbose)
    return dict(date=snap["date"], shares=len(snap["shares"]), nav=len(snap["nav"]),
                shares_asof=snap["shares_asof"], generated_at=snap["generated_at"])


def check():
    uni = universe()
    pxq_last, raw_last, nbar = {}, {}, []
    for sid in uni["code"]:
        b = _read_bars(os.path.join(PXQ, f"{sid}.json"))
        if b:
            pxq_last[sid] = b[-1][0]
            nbar.append(len(b))
        r = _read_bars(os.path.join(RAWP, f"{sid}.json"))
        if r:
            raw_last[sid] = r[-1][0]
    cal = json.load(open(os.path.join(META, "index_calendar.json")))
    bench = pxq_last.get(BENCH)          # 面板/信号日以它为闸门，不是"最大值"
    out = dict(calendar_last=cal[-1],
               pxq=dict(codes=len(pxq_last),
                        bench_last=bench,
                        at_bench=int(sum(v == bench for v in pxq_last.values())),
                        max_last=max(pxq_last.values()) if pxq_last else None,
                        pool_ge60=int((pd.Series(nbar) >= MIN_BARS_OK).sum())),
               raw=dict(codes=len(raw_last),
                        last=max(raw_last.values()) if raw_last else None),
               universe=len(uni))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="日历 + K线增量 + 份额（盘后一次跑完）")
    ap.add_argument("--kline", action="store_true", help="只更 K 线")
    ap.add_argument("--shares", action="store_true", help="只更份额/净值")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force-full", action="store_true")
    ap.add_argument("--jobs", type=int, default=12)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
        return 0
    out = {}
    if a.update or a.kline:
        out["kline"] = update_kline(jobs_n=a.jobs, force_full=a.force_full, verbose=not a.json)
    if a.update or a.shares:
        out["shares"] = update_shares(verbose=not a.json)
    if not out:
        ap.print_help()
        return 0
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    # 目标日没落库 → 非 0 退出，让 cron/上层任务看到失败
    return 2 if (out.get("kline") or {}).get("stale") else 0


if __name__ == "__main__":
    sys.exit(main())
