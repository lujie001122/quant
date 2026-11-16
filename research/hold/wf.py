#!/usr/bin/env python3
"""Walk-forward: 训练3年 → 测试1年, 逐年选参数, 拼接曲线 vs 一直用基线"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PY = os.path.join(ROOT, ".venv", "bin", "python3")
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "entry"))
from analyze import load, equity_series, metrics  # noqa

BASE = ["--momentum=simple", "--concentrated-pyramid", "--top-n=5", "--trail=e1",
        "--lookback=10", "--trend-entry", "--warmup"]
END = "2026-11-13"
TEST_YEARS = [2022, 2023, 2024, 2025, 2026]

CANDIDATES = {
    "基线": [],
    "锁盈11%": ["--trail-lock=0.11"],
    "锁盈12%": ["--trail-lock=0.12"],
    "锁盈9%+前6日": ["--trail-lock=0.09", "--hold-floor=6"],
    "锁盈12%+前8日+3N": ["--trail-lock=0.12", "--hold-floor=8", "--drop-mult=3"],
}


def run(args, start, end, tag):
    safe = tag.replace(" ", "_").replace("%", "pct").replace("+", "_").replace("日", "d")
    dump = os.path.join(HERE, "dumps", "wf", f"{safe}.json")
    log = os.path.join(HERE, "logs", "wf", f"{safe}.log")
    os.makedirs(os.path.dirname(dump), exist_ok=True)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    if not os.path.exists(dump):
        cmd = [PY, "backtest_bt.py"] + BASE + args + [f"--start={start}", f"--end={end}",
                                                      f"--dump={dump}"]
        with open(log, "w") as f:
            p = subprocess.run(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT)
        if p.returncode != 0:
            raise RuntimeError(f"{tag} 失败: {log}")
    d = load(dump)
    return d, equity_series(d)


def chain(series_list):
    out, base = [], 1.0
    for s in series_list:
        v0 = s[0][1]
        for d, v in s:
            out.append((d, base * v / v0))
        base = out[-1][1]
    return out


def main():
    sel, wf_series, base_series = {}, [], []
    print(f"{'测试年':8s} {'训练窗口':24s} {'各候选训练夏普':58s} 选中")
    for y in TEST_YEARS:
        tr_s, tr_e = f"{y-3}-01-01", f"{y-1}-12-31"
        te_s, te_e = f"{y}-01-01", (END if y == 2026 else f"{y}-12-31")
        scores = {}
        for name, args in CANDIDATES.items():
            _, eq = run(args, tr_s, tr_e, f"train_{y}_{name}")
            m = metrics(eq)
            scores[name] = m["sharpe"] if m else -9
        best = max(scores, key=scores.get)
        sel[y] = best
        desc = " ".join(f"{k}={v:.2f}" for k, v in sorted(scores.items(), key=lambda x: -x[1]))
        print(f"{y:<8d} {tr_s}→{tr_e:<10s} {desc:<58s} {best}")
        _, eq = run(CANDIDATES[best], te_s, te_e, f"test_{y}_{best}")
        wf_series.append(eq)
        _, eq = run(CANDIDATES["基线"], te_s, te_e, f"test_{y}_基线")
        base_series.append(eq)

    res = {}
    for label, series in (("walk-forward(逐年选参)", wf_series), ("一直用基线", base_series)):
        c = chain(series)
        m = metrics(c)
        yrs = {s[0][0][:4]: (s[-1][1] / s[0][1] - 1) * 100 for s in series}
        print(f"\n{label}: 年化{m['ann']:+.2f}% 回撤{m['mdd']:.2f}% 夏普{m['sharpe']:.2f} "
              f"累计{m['total']:+.2f}% ({m['start']}→{m['end']})")
        print("  逐年: " + " ".join(f"{y}:{v:+.1f}%" for y, v in yrs.items()))
        res[label] = {"ann": m["ann"], "mdd": m["mdd"], "sharpe": m["sharpe"],
                      "total": m["total"], "years": yrs}
    res["selection"] = {str(k): v for k, v in sel.items()}
    with open(os.path.join(HERE, "results", "wf.json"), "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
