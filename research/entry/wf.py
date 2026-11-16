#!/usr/bin/env python3
"""Walk-forward: 训练3年→测试1年，逐年选参数，拼接曲线 vs 一直用基线
用法: ../../.venv/bin/python3 wf.py
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

BASE = ["--momentum=simple", "--concentrated-pyramid", "--top-n=5", "--trail=e1",
        "--lookback=10", "--trend-entry", "--warmup"]

TEST_YEARS = [2022, 2023, 2024, 2025, 2026]

CANDIDATES = {
    "基线": [],
    "B3_RS20": ["--rs-filter=20"],
    "C1_首笔30%": ["--pyramid-init=0.30"],
    "S_综合分": None,   # 由 CLI 覆盖
    "V_波动加权": None,
    "叠加": None,
}


def run(args, start, end, tag):
    dump = os.path.join(HERE, "dumps", "wf", f"{tag}.json")
    log = os.path.join(HERE, "logs", "wf", f"{tag}.log")
    os.makedirs(os.path.dirname(dump), exist_ok=True)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    if os.path.exists(dump):
        pass
    else:
        cmd = [PY, "backtest_bt.py"] + BASE + args + [f"--start={start}", f"--end={end}",
                                                      f"--dump={dump}"]
        with open(log, "w") as f:
            p = subprocess.run(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT)
        if p.returncode != 0:
            raise RuntimeError(f"{tag} 失败: {log}")
    d = load(dump)
    return d, equity_series(d)


def chain(series_list):
    """把多段净值曲线首尾相乘成一条"""
    out = []
    base = 1.0
    for s in series_list:
        v0 = s[0][1]
        for d, v in s:
            out.append((d, base * v / v0))
        base = out[-1][1]
    return out


def main():
    cands = dict(CANDIDATES)
    # 可选: 覆盖候选
    for a in sys.argv[1:]:
        k, v = a.split("=", 1)
        cands[k] = v.split() if v else []

    sel = {}
    wf_series = []
    base_series = []
    print(f"{'测试年':8s} {'训练窗口':24s} {'各候选训练夏普':60s} 选中")
    for y in TEST_YEARS:
        tr_s, tr_e = f"{y-3}-01-01", f"{y-1}-12-31"
        te_s = f"{y}-01-01"
        te_e = "2026-11-13" if y == 2026 else f"{y}-12-31"
        scores = {}
        for name, args in cands.items():
            if args is None:
                continue
            d, eq = run(args, tr_s, tr_e, f"train_{y}_{name}")
            m = metrics(eq)
            scores[name] = m["sharpe"] if m else -9
        best = max(scores, key=scores.get)
        sel[y] = best
        desc = " ".join(f"{k}={v:.2f}" for k, v in sorted(scores.items(), key=lambda x: -x[1]))
        print(f"{y:<8d} {tr_s}→{tr_e:<10s} {desc:<60s} {best}")
        d, eq = run(cands[best], te_s, te_e, f"test_{y}_{best}")
        wf_series.append(eq)
        d, eq = run(cands["基线"], te_s, te_e, f"test_{y}_基线")
        base_series.append(eq)

    for label, series in (("walk-forward(逐年选参)", wf_series), ("一直用基线", base_series)):
        c = chain(series)
        m = metrics(c)
        print(f"\n{label}: 年化{m['ann']:+.2f}% 回撤{m['mdd']:.2f}% 夏普{m['sharpe']:.2f} "
              f"累计{m['total']:+.2f}% ({m['start']}→{m['end']})")
        yrs = {}
        base = 1.0
        for s in series:
            yrs[s[0][0][:4]] = (s[-1][1] / s[0][1] - 1) * 100
        print("  逐年: " + " ".join(f"{y}:{v:+.1f}%" for y, v in yrs.items()))
    with open(os.path.join(HERE, "results", "wf.json"), "w") as f:
        json.dump({"selection": sel, "candidates": {k: v for k, v in cands.items()}}, f,
                  ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
