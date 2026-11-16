#!/usr/bin/env python3
"""W 系列批量回测: 持仓宽度 (top-n) × 加仓批数 × 择时

用法: .venv/bin/python3 research/width/runner.py W1 W2 W3 [--force]

基线: --momentum=simple --start=2019-01-01 --end=2026-11-13 --concentrated-pyramid
      --top-n=5 --trail=e1 --lookback=10 --trend-entry --warmup   (择时跟随 config)
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PY = os.path.join(ROOT, ".venv", "bin", "python3")
sys.path.insert(0, os.path.join(ROOT, "research", "entry"))
from analyze import load, equity_series, metrics, round_trips, hold_bucket  # noqa

BASE = ["--momentum=simple", "--start=2019-01-01", "--end=2026-11-13",
        "--concentrated-pyramid", "--top-n=5", "--trail=e1", "--lookback=10",
        "--trend-entry", "--warmup"]

SEGMENTS = [
    ("19-21", "2019-01-01", "2021-12-31"),
    ("22-23", "2022-01-01", "2023-12-31"),
    ("24-26", "2024-01-01", "2026-11-13"),
    ("近9月", "2026-02-13", "2026-11-13"),
]
YEARS = [str(y) for y in range(2019, 2027)]

BATCHES = {
    # ═══ 第一组: 同时持仓数量 ═══
    "W1": {
        "t5 基线": [],
        "t6": ["--top-n=6"],
        "t8": ["--top-n=8"],
        "t10": ["--top-n=10"],
        "t12": ["--top-n=12"],
        "t15": ["--top-n=15"],
    },
    # ═══ 第二组: 抬仓位的三种机制 ═══
    "W2": {
        "a1 宽持仓t6": ["--top-n=6"],
        "a2 宽持仓t8": ["--top-n=8"],
        "a3 宽持仓t12": ["--top-n=12"],
        "b1 加批t5_4批": ["--addon-max=4"],
        "b2 加批t5_6批": ["--addon-max=6"],
        "b3 加批t5_8批": ["--addon-max=8"],
        "c1 t8_4批": ["--top-n=8", "--addon-max=4"],
        "c2 t8_6批": ["--top-n=8", "--addon-max=6"],
        "c3 t10_4批": ["--top-n=10", "--addon-max=4"],
    },
    # ═══ 第四组: 最强宽持仓变体的邻域 (top-n ±1, lookback ±30%) ═══
    "W4": {
        "t12 基线": ["--top-n=12"],
        "t11": ["--top-n=11"],
        "t13": ["--top-n=13"],
        "t12_lb7": ["--top-n=12", "--lookback=7"],
        "t12_lb13": ["--top-n=12", "--lookback=13"],
        "t12_lb7_4批": ["--top-n=12", "--lookback=7", "--addon-max=4"],
    },
    # ═══ 第三组: 择时 × 宽度 ═══
    "W3": {
        "t5 择时开": [],
        "t5 择时关": ["--no-timing"],
        "t8 择时开": ["--top-n=8"],
        "t8 择时关": ["--top-n=8", "--no-timing"],
        "t10 择时开": ["--top-n=10"],
        "t10 择时关": ["--top-n=10", "--no-timing"],
        "t12 择时开": ["--top-n=12"],
        "t12 择时关": ["--top-n=12", "--no-timing"],
    },
}


_CACHE = os.path.join(ROOT, "data", "kline_qfq")
_PRICE = {}


def _prices(code):
    """本地前复权缓存 → {date: close}"""
    if code in _PRICE:
        return _PRICE[code]
    out = {}
    path = os.path.join(_CACHE, f"{code}.csv")
    if os.path.exists(path):
        with open(path) as f:
            next(f)
            for line in f:
                p = line.strip().split(",")
                if len(p) > 2:
                    try:
                        out[p[0]] = float(p[2])
                    except ValueError:
                        pass
    _PRICE[code] = out
    return out


def rebuild_exposure(d):
    """档位关闭时引擎不记 exposure — 用成交台账 + 本地收盘价重建 (与引擎 gross/eq 同口径)"""
    eq = {e["date"]: float(e["value"]) for e in d["equity"]}
    holds = {}
    fills = {}
    for fl in d["ledger"]:
        s = float(fl["size"])
        holds[fl["code"]] = holds.get(fl["code"], 0.0) + (s if fl["kind"] == "buy" else -s)
        fills.setdefault(fl["date"], {})[fl["code"]] = holds[fl["code"]]
    codes = list(holds)
    cur = {c: 0.0 for c in codes}
    ratios, counts = [], []
    for dt in sorted(eq):
        day = fills.get(dt)
        if day:
            cur.update(day)
        gv, cnt = 0.0, 0
        for c in codes:
            if cur[c] > 1:
                px = _prices(c).get(dt)
                if px:
                    gv += cur[c] * px
                    cnt += 1
        e = eq[dt]
        if e > 0:
            ratios.append(gv / e)
            counts.append(cnt)
    return (sum(ratios) / len(ratios) * 100 if ratios else None,
            sum(counts) / len(counts) if counts else None)


def concurrency(d):
    """平均并发持仓只数 + 平均每只占用 (占总资金%)"""
    eq = {e["date"]: float(e["value"]) for e in d["equity"]}
    holds, fills = {}, {}
    for fl in d["ledger"]:
        s = float(fl["size"])
        holds[fl["code"]] = holds.get(fl["code"], 0.0) + (s if fl["kind"] == "buy" else -s)
        fills.setdefault(fl["date"], {})[fl["code"]] = holds[fl["code"]]
    codes = list(holds)
    cur = {c: 0.0 for c in codes}
    cnts = []
    for dt in sorted(eq):
        cur.update(fills.get(dt, {}))
        cnts.append(sum(1 for c in codes if cur[c] > 1))
    return sum(cnts) / len(cnts) if cnts else 0.0


def safe(name):
    return (name.replace(" ", "_").replace("=", "").replace("%", "pct")
            .replace("/", "_").replace(">", "gt").replace("(", "").replace(")", ""))


def run_one(name, extra, force=False, tag_suffix=""):
    s = safe(name) + tag_suffix
    dump = os.path.join(HERE, "dumps", f"{s}.json")
    log = os.path.join(HERE, "logs", f"{s}.log")
    os.makedirs(os.path.dirname(dump), exist_ok=True)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    if force or not os.path.exists(dump):
        cmd = [PY, "backtest_bt.py"] + BASE + extra + [f"--dump={dump}"]
        with open(log, "w") as f:
            p = subprocess.run(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT)
        if p.returncode != 0:
            print(f"!! {name} 失败, 见 {log}")
            return None
    return analyze(dump, name, extra)


def analyze(dump, name, extra):
    d = load(dump)
    eq = equity_series(d)
    full = metrics(eq)
    if full is None:
        print(f"!! {name} 净值太短")
        return None
    segs = {k: metrics(eq, s, e) for k, s, e in SEGMENTS}
    yrs = {y: metrics(eq, f"{y}-01-01", f"{y}-12-31") for y in YEARS}
    rts = round_trips(d)
    n = len(rts)
    expo = [1.0 - e["cash"] / e["value"] for e in d["equity"] if e.get("value")]
    exposure = sum(expo) / len(expo) * 100 if expo else None
    buckets = {}
    for b in ("1-3天", "4-10天", "11-30天", "31天以上"):
        sub = [r for r in rts if hold_bucket(r["hold"]) == b]
        buckets[b] = {"n": len(sub), "pnl": sum(r["pnl"] for r in sub),
                      "win": (sum(1 for r in sub if r["pnl"] > 0) / len(sub) * 100) if sub else 0.0}
    return {
        "name": name, "args": extra, "dump": dump,
        "full": {k: full[k] for k in ("ann", "mdd", "sharpe", "total")},
        "segments": {k: (v["ann"] if v else None) for k, v in segs.items()},
        "segments_mdd": {k: (v["mdd"] if v else None) for k, v in segs.items()},
        "years": {k: (v["total"] if v else None) for k, v in yrs.items()},
        "trades": n,
        "win_rate": sum(1 for r in rts if r["pnl"] > 0) / n * 100 if n else 0,
        "avg_hold": sum(r["hold"] for r in rts) / n if n else 0,
        "buckets": buckets,
        "exposure": exposure,
        "pass": (full["ann"] >= 13 and full["mdd"] <= 20 and full["sharpe"] >= 0.75),
    }


def summarize(rows, base, title):
    print(f"\n══════ {title} ══════")
    print(f"{'策略':16s} {'年化':>7s} {'Δpp':>6s} {'回撤':>6s} {'夏普':>6s} {'Δsh':>6s} "
          f"{'仓位':>6s} {'笔数':>5s} {'胜率':>6s} {'均持':>5s} │ "
          f"{'31d+笔':>6s} {'31d+盈亏':>9s} {'占比':>5s} │ {'19-21':>6s} {'22-23':>6s} "
          f"{'24-26':>6s} {'近9月':>6s} │ 达标")
    for r in rows:
        if not r:
            continue
        f = r["full"]
        b31 = r["buckets"]["31天以上"]
        tot_pnl = sum(v["pnl"] for v in r["buckets"].values()) or 1
        seg = " ".join(f"{r['segments'][k]:>+6.0f}" if r["segments"][k] is not None else "     —"
                       for k, _, _ in SEGMENTS)
        ex = r["exposure"] or 0.0
        print(f"{r['name']:16s} {f['ann']:>+6.2f}% {f['ann']-base['full']['ann']:>+6.2f} "
              f"{f['mdd']:>5.2f}% {f['sharpe']:>6.2f} {f['sharpe']-base['full']['sharpe']:>+6.2f} "
              f"{ex:>5.1f}% {r['trades']:>5d} {r['win_rate']:>5.1f}% {r['avg_hold']:>5.1f} │ "
              f"{b31['n']:>6d} {b31['pnl']:>+9.0f} {b31['pnl']/tot_pnl*100:>4.0f}% │ {seg} │ "
              f"{'✓' if r['pass'] else ''}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    batches = args or ["W1"]
    base = run_one("t5 基线", [], force=force)
    if base is None:
        sys.exit(1)
    for batch in batches:
        rows = [base]
        for name, extra in BATCHES[batch].items():
            if name == "t5 基线":
                continue
            r = run_one(name, extra, force=force)
            if r:
                rows.append(r)
                print(f"  ✓ {name}: 年化{r['full']['ann']:+.2f}% 回撤{r['full']['mdd']:.2f}% "
                      f"夏普{r['full']['sharpe']:.2f} 仓位{r['exposure'] or 0:.1f}%")
        summarize(rows, base, batch)
        out = os.path.join(HERE, "results", f"{batch}.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
