#!/usr/bin/env python3
"""H1-H5 批量回测: 让赢家更大更久

用法: ../../.venv/bin/python3 runner.py <batch> [batch...]
每个配置一行: 年化/回撤/夏普/笔数/胜率/31天+单的笔数与贡献/分四段/分年
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PY = os.path.join(ROOT, ".venv", "bin", "python3")
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "entry"))
from analyze import load, equity_series, metrics, round_trips, hold_bucket  # noqa
from collections import defaultdict  # noqa

BASE = ["--momentum=simple", "--start=2019-01-01", "--end=2026-11-13",
        "--concentrated-pyramid", "--top-n=5", "--trail=e1", "--lookback=10",
        "--trend-entry", "--warmup"]

SEGMENTS = [
    ("2019-21", "2019-01-01", "2021-12-31"),
    ("22-23", "2022-01-01", "2023-12-31"),
    ("24-26", "2024-01-01", "2026-11-13"),
    ("近9月", "2026-02-13", "2026-11-13"),
]
YEARS = [str(y) for y in range(2019, 2027)]

BATCHES = {
    "base": {"基线": []},
    # ═══ H1 换仓容忍度 ═══
    "H1": {
        "H1a 跌出TOP-3N才清": ["--drop-mult=3"],
        "H1b 趋势未破则保留": ["--drop-need-trend"],
        "H1c 跌出先减半": ["--drop-half"],
        "H1d 3N+趋势未破保留": ["--drop-mult=3", "--drop-need-trend"],
        "H1e 先减半+趋势未破保留": ["--drop-half", "--drop-need-trend"],
    },
    # ═══ H2 移动止盈放宽 ═══
    "H2": {
        "H2a 激活6%/锁盈12%": ["--trail-act=0.06", "--trail-lock=0.12"],
        "H2b 激活10%/锁盈20%": ["--trail-act=0.10", "--trail-lock=0.20"],
        "H2c ATR3.5x": ["--trail=atr", "--atr-mult=3.5"],
        "H2d ATR4.5x": ["--trail=atr", "--atr-mult=4.5"],
        "H2e ATR3.5x+6%/12%": ["--trail=atr", "--atr-mult=3.5",
                               "--trail-act=0.06", "--trail-lock=0.12"],
        "H2f 只放宽锁盈12%": ["--trail-lock=0.12"],
    },
    # ═══ H3 只对赢家加仓 ═══
    "H3": {
        "H3a 加仓须浮盈>5%": ["--addon-profit=0.05"],
        "H3b 加仓须浮盈>1ATR": ["--addon-profit-atr=1.0"],
        "H3c 老赢家第3/4批(>20日)": ["--winner-addon=2", "--winner-days=20"],
        "H3d 单笔上限70%": ["--order-cap=0.70"],
        "H3e 单笔70%+4批": ["--order-cap=0.70", "--addon-max=4"],
        "H3f 加仓单批40%": ["--addon-pct=0.40"],
        "H3g 老赢家加仓+单笔70%": ["--winner-addon=2", "--winner-days=20", "--order-cap=0.70"],
        "H3h 浮盈>5%+老赢家加仓": ["--addon-profit=0.05", "--winner-addon=2", "--winner-days=20"],
        "H3i 单批40%+老赢家加仓": ["--addon-pct=0.40", "--winner-addon=2", "--winner-days=20"],
        "H3j 单批40%+单笔70%": ["--addon-pct=0.40", "--order-cap=0.70"],
    },
    # ═══ H5 长单事前特征 → 落地入场过滤 ═══
    "H5": {
        "H5a 只买低波动(vol≤1.2%)": ["--vol-max=0.012"],
        "H5b 去掉高波动(vol≤1.8%)": ["--vol-max=0.018"],
        "H5c 只买高波动(vol≥1.2%)": ["--vol-min=0.012"],
        "H5d 只买高波动(vol≥1.8%)": ["--vol-min=0.018"],
        "H5e 低波动+锁盈12%+前8日": ["--vol-max=0.012", "--trail-lock=0.12", "--hold-floor=8"],
    },
    # ═══ H2 邻域扫描 ═══
    "H2x": {
        "锁盈9%": ["--trail-lock=0.09"],
        "锁盈11%": ["--trail-lock=0.11"],
        "锁盈12%": ["--trail-lock=0.12"],
        "锁盈14%": ["--trail-lock=0.14"],
        "锁盈16%": ["--trail-lock=0.16"],
        "锁盈20%": ["--trail-lock=0.20"],
        "gap5%": ["--trail-gap=0.05"],
        "gap7%": ["--trail-gap=0.07"],
        "gap9%": ["--trail-gap=0.09"],
        "锁盈12%+gap5%": ["--trail-lock=0.12", "--trail-gap=0.05"],
        "锁盈12%+gap7%": ["--trail-lock=0.12", "--trail-gap=0.07"],
    },
    # ═══ H4 邻域扫描 ═══
    "H4x": {
        "前5日": ["--hold-floor=5"],
        "前6日": ["--hold-floor=6"],
        "前7日": ["--hold-floor=7"],
        "前8日": ["--hold-floor=8"],
        "前9日": ["--hold-floor=9"],
        "前10日": ["--hold-floor=10"],
        "前12日": ["--hold-floor=12"],
    },
    # ═══ 二维网格: 锁盈 × 持有保护 ═══
    "GRID": {f"锁盈{int(l*100)}%_保护{h}日": ([f"--trail-lock={l}"] + ([f"--hold-floor={h}"] if h else []))
             for l in (0.09, 0.11, 0.12, 0.14, 0.16) for h in (0, 5, 6, 8, 10)},
    # ═══ 叠加 ═══
    "H45": {
        "锁盈12%+前8日": ["--trail-lock=0.12", "--hold-floor=8"],
        "锁盈12%+前6日": ["--trail-lock=0.12", "--hold-floor=6"],
        "锁盈12%+前10日": ["--trail-lock=0.12", "--hold-floor=10"],
        "锁盈12%+前8日+gap5%": ["--trail-lock=0.12", "--hold-floor=8", "--trail-gap=0.05"],
        "锁盈12%+前8日+浮盈5%加仓": ["--trail-lock=0.12", "--hold-floor=8", "--addon-profit=0.05"],
        "锁盈12%+前8日+趋势保留": ["--trail-lock=0.12", "--hold-floor=8", "--drop-need-trend"],
        "锁盈12%+前8日+3N": ["--trail-lock=0.12", "--hold-floor=8", "--drop-mult=3"],
    },
    # ═══ H4 持有期下限 ═══
    "H4": {
        "H4a 前3日保护": ["--hold-floor=3"],
        "H4b 前5日保护": ["--hold-floor=5"],
        "H4c 前8日保护": ["--hold-floor=8"],
        "H4d 前10日保护": ["--hold-floor=10"],
    },
}

CONC = {  # 并发跑时按块分组
    "h1": ["H1"], "h2": ["H2"], "h3": ["H3"], "h4": ["H4"],
}


def bucket_stats(rts):
    out = {}
    for b in ("1-3天", "4-10天", "11-30天", "31天以上"):
        sub = [r for r in rts if hold_bucket(r["hold"]) == b]
        out[b] = {"n": len(sub), "pnl": sum(r["pnl"] for r in sub),
                  "win": (sum(1 for r in sub if r["pnl"] > 0) / len(sub) * 100) if sub else 0.0}
    return out


def run_one(name, extra):
    safe = name.replace(" ", "_").replace("%", "pct").replace("/", "_").replace(">", "gt")
    dump = os.path.join(HERE, "dumps", f"{safe}.json")
    log = os.path.join(HERE, "logs", f"{safe}.log")
    os.makedirs(os.path.dirname(dump), exist_ok=True)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    cmd = [PY, "backtest_bt.py"] + BASE + extra + [f"--dump={dump}"]
    with open(log, "w") as f:
        p = subprocess.run(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        print(f"!! {name} 失败, 见 {log}")
        return None
    d = load(dump)
    eq = equity_series(d)
    full = metrics(eq)
    segs = {k: metrics(eq, s, e) for k, s, e in SEGMENTS}
    yrs = {y: metrics(eq, f"{y}-01-01", f"{y}-12-31") for y in YEARS}
    rts = round_trips(d)
    n = len(rts)
    expo = d.get("exposure") or []
    return {
        "name": name, "args": extra,
        "full": {k: full[k] for k in ("ann", "mdd", "sharpe", "total")},
        "segments": {k: (v["ann"] if v else None) for k, v in segs.items()},
        "years": {k: (v["total"] if v else None) for k, v in yrs.items()},
        "trades": n,
        "win_rate": sum(1 for r in rts if r["pnl"] > 0) / n * 100 if n else 0,
        "avg_hold": sum(r["hold"] for r in rts) / n if n else 0,
        "buckets": bucket_stats(rts),
        "exposure": sum(e["ratio"] for e in expo) / len(expo) * 100 if expo else None,
        "dump": dump,
    }


def load_base():
    path = os.path.join(HERE, "dumps", "基线.json")
    if not os.path.exists(path):
        return run_one("基线", [])
    d = load(path)
    eq = equity_series(d)
    segs = {k: metrics(eq, s, e) for k, s, e in SEGMENTS}
    yrs = {y: metrics(eq, f"{y}-01-01", f"{y}-12-31") for y in YEARS}
    rts = round_trips(d)
    expo = d.get("exposure") or []
    return {"name": "基线", "args": [], "full": {k: metrics(eq)[k] for k in ("ann", "mdd", "sharpe", "total")},
            "segments": {k: (v["ann"] if v else None) for k, v in segs.items()},
            "years": {k: (v["total"] if v else None) for k, v in yrs.items()},
            "trades": len(rts), "win_rate": sum(1 for r in rts if r["pnl"] > 0) / len(rts) * 100,
            "avg_hold": sum(r["hold"] for r in rts) / len(rts),
            "buckets": bucket_stats(rts), "exposure": sum(e["ratio"] for e in expo) / len(expo) * 100 if expo else None,
            "dump": path}


def summarize(rows, base):
    print(f"\n{'策略':26s} {'年化':>8s} {'Δpp':>6s} {'回撤':>7s} {'夏普':>6s} {'Δsh':>6s} "
          f"{'笔数':>5s} {'胜率':>6s} {'均持':>5s} {'仓位':>5s} │ "
          f"{'31d+笔':>6s} {'31d+盈亏':>9s} {'31d+占比':>8s} │ "
          f"分段年化(19-21/22-23/24-26/近9月) │ 逐年")
    for r in rows:
        if not r:
            continue
        f = r["full"]
        d_ann = f["ann"] - base["full"]["ann"]
        d_sh = f["sharpe"] - base["full"]["sharpe"]
        b31 = r["buckets"]["31天以上"]
        tot_pnl = sum(v["pnl"] for v in r["buckets"].values()) or 1
        seg = "/".join(f"{r['segments'][k]:+.0f}" if r["segments"][k] is not None else "—"
                       for k, _, _ in SEGMENTS)
        ystr = " ".join(f"{r['years'][y]:+.0f}" if r["years"][y] is not None else "—" for y in YEARS)
        expo = f"{r['exposure']:.0f}" if r["exposure"] is not None else "—"
        print(f"{r['name']:26s} {f['ann']:>+7.2f}% {d_ann:>+6.2f} {f['mdd']:>6.2f}% {f['sharpe']:>6.2f} "
              f"{d_sh:>+6.2f} {r['trades']:>5d} {r['win_rate']:>5.1f}% {r['avg_hold']:>5.1f} {expo:>5s} │ "
              f"{b31['n']:>6d} {b31['pnl']:>+9.0f} {b31['pnl']/tot_pnl*100:>7.0f}% │ "
              f"{seg} │ {ystr}")


def main():
    batches = sys.argv[1:] or ["H1", "H2", "H3", "H4"]
    base = load_base()
    for batch in batches:
        cfgs = BATCHES[batch]
        rows = [base] if batch == "base" else [base]
        print(f"\n══════ {batch} ══════")
        for name, extra in cfgs.items():
            r = run_one(name, extra)
            if r:
                rows.append(r)
                print(f"  ✓ {name}")
        summarize(rows, base)
        out = os.path.join(HERE, "results", f"{batch}.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
