#!/usr/bin/env python3
"""把 results/*.json 汇总成 markdown 表（B组/C组/稳健性/叠加）"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
YEARS = [str(y) for y in range(2019, 2027)]
SEGS = ["2019-21", "22-23", "24-26", "近9月"]


def load_rows(batch):
    p = os.path.join(RES, f"{batch}.json")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return json.load(f)


def base_row():
    for b in ("base", "B1"):
        rows = load_rows(b)
        if rows:
            return rows[0]
    return None


def table(batches, title, dedup_base=True):
    b = base_row()
    rows = []
    for bt in batches:
        for r in load_rows(bt):
            if r is None:
                continue
            if dedup_base and r["name"] == "基线":
                continue
            rows.append(r)
    if not rows:
        return
    print(f"\n### {title}\n")
    print("| 配置 | 年化 | Δ年化(pp) | 回撤 | Δ回撤(pp) | 夏普 | Δ夏普 | 笔数 | 胜率 | 均持天 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in [b] + rows:
        f = r["full"]
        da = f["ann"] - b["full"]["ann"]
        dd = f["mdd"] - b["full"]["mdd"]
        ds = f["sharpe"] - b["full"]["sharpe"]
        mark = "**基线**" if r is b else r["name"]
        print(f"| {mark} | {f['ann']:+.2f}% | {da:+.2f} | {f['mdd']:.2f}% | {dd:+.2f} | "
              f"{f['sharpe']:.2f} | {ds:+.2f} | {r['trades']} | {r['win_rate']:.1f}% | {r['avg_hold']:.1f} |")

    print(f"\n四段年化 + 逐年收益（%）\n")
    print("| 配置 | " + " | ".join(SEGS) + " | " + " | ".join(YEARS) + " |")
    print("|---" * (1 + len(SEGS) + len(YEARS)) + "|")
    for r in [b] + rows:
        segs = " | ".join(f"{r['segments'][k]:+.0f}" if r["segments"].get(k) is not None else "—" for k in SEGS)
        yrs = " | ".join(f"{r['years'][k]:+.0f}" if r["years"].get(k) is not None else "—" for k in YEARS)
        mark = "**基线**" if r is b else r["name"]
        print(f"| {mark} | {segs} | {yrs} |")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "B"):
        table(["B1", "B1n", "B2", "B3", "B4", "B5"], "B 组：6 类入场条件（逐项单独开/关）")
    if which in ("all", "C"):
        table(["C1", "C2", "C3"], "C 组：分批/加仓结构")
    if which in ("all", "R"):
        table(["NB3", "NB3b", "NC1", "R", "RB"], "邻域稳健性（±30% 参数）")
    if which in ("all", "M"):
        table(["MIX", "STACK", "X", "S", "V"], "叠加与附加探针")
