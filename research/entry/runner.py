#!/usr/bin/env python3
"""批量回测 + 分段绩效汇总
用法: ../../.venv/bin/python3 runner.py <batch>
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PY = os.path.join(ROOT, ".venv", "bin", "python3")
sys.path.insert(0, HERE)
from analyze import load, equity_series, metrics, round_trips  # noqa

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
    # ── B 组 ──
    "B1": {"B1 量价1.2x": ["--vol-confirm=1.2"]},
    "B1n": {"B1 量价1.5x": ["--vol-confirm=1.5"], "B1 量价1.0x": ["--vol-confirm=1.0"]},
    "B2": {"B2 风险调整动量": ["--mom-quality=ra"], "B2 动量xR2": ["--mom-quality=r2"]},
    "B3": {"B3 相对强弱20日": ["--rs-filter=20"], "B3 相对强弱60日": ["--rs-filter=60"]},
    "B4": {"B4 只回踩MA5/10": ["--entry-mode=pullback"]},
    "B5": {"B5 连续2日确认": ["--entry-confirm=2"], "B5 连续3日确认": ["--entry-confirm=3"]},
    # ── C 组 ──
    "C1": {"C1 首笔20%": ["--pyramid-init=0.20"], "C1 首笔30%": ["--pyramid-init=0.30"],
           "C1 首笔40%": ["--pyramid-init=0.40"]},
    "C2": {"C2 加仓须创新高": ["--addon-mode=high"]},
    "C3": {"C3 上限70%": ["--pos-cap=0.70"],
           "C3 上限70%+4批": ["--pos-cap=0.70", "--addon-max=4"],
           "C3 仅4批加仓": ["--addon-max=4"], "C3 3批加仓": ["--addon-max=3"]},
    # ── 邻域稳健性 ──
    "NB3": {"B3 RS14日": ["--rs-filter=14"], "B3 RS26日": ["--rs-filter=26"]},
    "NC1": {"C1 首笔21%": ["--pyramid-init=0.21"], "C1 首笔39%": ["--pyramid-init=0.39"]},
    # ── 额外入场严格化探针 ──
    "X": {"X 动量>0": ["--min-mom=0"], "X 动量>3%": ["--min-mom=3"],
          "X 价>MA60": ["--entry-ma60"], "X 动量>0+MA60": ["--min-mom=0", "--entry-ma60"]},
    # ── 横截面综合入场分 ──
    "S": {"S 综合分≥0.40": ["--entry-score=0.40"], "S 综合分≥0.50": ["--entry-score=0.50"],
          "S 综合分≥0.60": ["--entry-score=0.60"], "S 综合分≥0.70": ["--entry-score=0.70"]},
    # ── 波动率倒数加权(B6) ──
    "V": {"V 目标波动1.0%/日": ["--vol-target=0.010"], "V 目标波动1.3%/日": ["--vol-target=0.013"],
          "V 目标波动1.7%/日": ["--vol-target=0.017"]},
    "NB3b": {"B3 RS18日": ["--rs-filter=18"], "B3 RS22日": ["--rs-filter=22"]},
    "RB": {f"前{rk}名+RS{rs}日": [f"--entry-rank={rk}", f"--rs-filter={rs}"]
           for rk in (2, 3, 4) for rs in (14, 20, 26)},
    "STACK": {"叠加(RS20+前3名+首笔30%)": ["--rs-filter=20", "--entry-rank=3", "--pyramid-init=0.30"],
              "叠加+3批": ["--rs-filter=20", "--entry-rank=3", "--pyramid-init=0.30", "--addon-max=3"],
              "叠加+波动加权": ["--rs-filter=20", "--entry-rank=3", "--vol-target=0.010"]},
    "R": {"R 只买前3名": ["--entry-rank=3"], "R 只买前2名": ["--entry-rank=2"],
          "R 只买前4名": ["--entry-rank=4"], "R 前3名+RS20": ["--entry-rank=3", "--rs-filter=20"]},
    "MIX": {"RS20+首笔30%": ["--rs-filter=20", "--pyramid-init=0.30"],
            "RS20+首笔30%+波动加权": ["--rs-filter=20", "--pyramid-init=0.30", "--vol-target=0.013"],
            "RS20+3批加仓": ["--rs-filter=20", "--addon-max=3"],
            "RS20+首笔30%+3批": ["--rs-filter=20", "--pyramid-init=0.30", "--addon-max=3"]},
}


def run_one(name, extra):
    safe = name.replace(" ", "_").replace("%", "pct").replace("/", "_")
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
    yrs = {}
    for y in YEARS:
        yrs[y] = metrics(eq, f"{y}-01-01", f"{y}-12-31")
    rts = round_trips(d)
    n = len(rts)
    win = sum(1 for r in rts if r["pnl"] > 0)
    return {"name": name, "args": extra, "full": full,
            "segments": {k: (v["ann"] if v else None) for k, v in segs.items()},
            "years": {k: (v["total"] if v else None) for k, v in yrs.items()},
            "trades": n, "win_rate": win / n * 100 if n else 0,
            "avg_hold": sum(r["hold"] for r in rts) / n if n else 0,
            "dump": dump, "log": log}


def summarize(rows, base_row):
    print(f"\n{'策略':22s} {'年化':>8s} {'Δpp':>7s} {'回撤':>7s} {'夏普':>6s} {'Δsh':>6s} "
          f"{'笔数':>5s} {'胜率':>6s} {'均持':>5s} | 分段年化(19-21/22-23/24-26/近9月) | 逐年")
    for r in rows:
        if r is None:
            continue
        f = r["full"]
        d_ann = f["ann"] - base_row["full"]["ann"]
        d_sh = f["sharpe"] - base_row["full"]["sharpe"]
        seg = "/".join(f"{r['segments'][k]:+.0f}" if r["segments"][k] is not None else "—"
                       for k, _, _ in SEGMENTS)
        ystr = " ".join(f"{r['years'][y]:+.0f}" if r["years"][y] is not None else "—" for y in YEARS)
        print(f"{r['name']:22s} {f['ann']:>+7.2f}% {d_ann:>+6.2f} {f['mdd']:>6.2f}% {f['sharpe']:>6.2f} "
              f"{d_sh:>+6.2f} {r['trades']:>5d} {r['win_rate']:>5.1f}% {r['avg_hold']:>5.1f} | "
              f"{seg} | {ystr}")


def main():
    batch = sys.argv[1] if len(sys.argv) > 1 else "base"
    cfgs = BATCHES[batch]
    # 基线总是先跑(或复用)
    base_path = os.path.join(HERE, "dumps", "基线.json")
    if os.path.exists(base_path):
        d = load(base_path)
        eq = equity_series(d)
        rts = round_trips(d)
        win = sum(1 for r in rts if r["pnl"] > 0)
        base_row = {"name": "基线", "full": metrics(eq), "trades": len(rts),
                    "win_rate": win / len(rts) * 100,
                    "avg_hold": sum(r["hold"] for r in rts) / len(rts),
                    "segments": {k: (metrics(eq, s, e)["ann"] if metrics(eq, s, e) else None)
                                 for k, s, e in SEGMENTS},
                    "years": {y: (metrics(eq, f"{y}-01-01", f"{y}-12-31")["total"]
                                  if metrics(eq, f"{y}-01-01", f"{y}-12-31") else None) for y in YEARS}}
    else:
        base_row = run_one("基线", [])
    rows = [base_row]
    for name, extra in cfgs.items():
        r = run_one(name, extra)
        rows.append(r)
        print(f"  ✓ {name} 完成")
    summarize(rows, base_row)
    out = os.path.join(HERE, "results", f"{batch}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"\n结果已存 {out}")


if __name__ == "__main__":
    main()
