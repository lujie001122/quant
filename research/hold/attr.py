#!/usr/bin/env python3
"""诊断表: 关键 dump 的分层盈亏结构(卖出原因 + 持有天数桶)

用法: ../../.venv/bin/python3 attr.py 基线 锁盈12pct H1b_趋势未破则保留 ...
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "entry"))
from analyze import load, equity_series, metrics, round_trips, hold_bucket, fills_by_exit  # noqa

KEYS = ["跌出TOP-N清仓", "移动止盈", "破MA5卖活动仓", "破MA20减仓(1级)", "趋势止盈(卖10%)"]


def row(name):
    p = os.path.join(HERE, "dumps", f"{name}.json")
    d = load(p)
    m = metrics(equity_series(d))
    rts = round_trips(d)
    agg = fills_by_exit(d)
    cells = [f"{name}"]
    cells.append(f"{m['ann']:+.2f}")
    cells.append(f"{m['mdd']:.2f}")
    cells.append(f"{m['sharpe']:.2f}")
    cells.append(f"{len(rts)}")
    for b in ("1-3天", "4-10天", "11-30天", "31天以上"):
        sub = [r for r in rts if hold_bucket(r["hold"]) == b]
        cells.append(f"{len(sub)}/{sum(r['pnl'] for r in sub)/1000:+.0f}k")
    for k in KEYS:
        a = agg.get(k, {"n": 0, "pnl": 0})
        cells.append(f"{a['n']}/{a['pnl']/1000:+.0f}k")
    return cells


def main():
    names = sys.argv[1:] or ["基线"]
    head = ["策略", "年化", "回撤", "夏普", "回合",
            "1-3天", "4-10天", "11-30天", "31天+",
            "跌出TOP清仓", "移动止盈", "破MA5", "破MA20减仓", "趋势止盈"]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for n in names:
        try:
            print("| " + " | ".join(row(n)) + " |")
        except FileNotFoundError:
            print(f"| {n} | 缺 dump |")


if __name__ == "__main__":
    main()
