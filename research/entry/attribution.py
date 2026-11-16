#!/usr/bin/env python3
"""第1节 逐笔归因 + B6 高低波动分组（读 dumps/基线.json）"""
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from analyze import load, equity_series, metrics, round_trips, hold_bucket, HOLD_BUCKETS, \
    fills_by_exit, label  # noqa

CAP = 220000.0


def rt_with_vol(dump):
    """回合 + 首笔记录的vol20"""
    first_vol = {}
    for f in dump["ledger"]:
        if f["kind"] == "buy" and f["rt"] not in first_vol:
            first_vol[f["rt"]] = f.get("vol20", 0.0)
    out = round_trips(dump)
    for r in out:
        r["vol20"] = first_vol.get(r["rt"], 0.0)
    return out


def table_hold(rts):
    print("\n### 1.1 按持有天数分层（回合口径 = 空仓→空仓）\n")
    print("| 持有天数 | 笔数 | 占比 | 胜率 | 平均盈亏 | 总贡献 | 占总盈亏 | 平均持有 |")
    print("|---|---|---|---|---|---|---|---|")
    total_pnl = sum(r["pnl"] for r in rts)
    for b, _, _ in HOLD_BUCKETS + [("0天(当日)", 0, 0)]:
        sub = [r for r in rts if hold_bucket(r["hold"]) == b]
        if not sub:
            continue
        win = sum(1 for r in sub if r["pnl"] > 0)
        pnl = sum(r["pnl"] for r in sub)
        print(f"| {b} | {len(sub)} | {len(sub)/len(rts)*100:.1f}% | {win/len(sub)*100:.1f}% | "
              f"{pnl/len(sub):+,.0f} | {pnl:+,.0f} | {pnl/total_pnl*100:+.0f}% | "
              f"{sum(r['hold'] for r in sub)/len(sub):.1f}天 |")
    print(f"| **合计** | {len(rts)} | 100% | "
          f"{sum(1 for r in rts if r['pnl']>0)/len(rts)*100:.1f}% | "
          f"{total_pnl/len(rts):+,.0f} | {total_pnl:+,.0f} | 100% | — |")


def table_exit(dump, rts):
    print("\n### 1.2 按卖出原因分层（成交笔口径：每笔卖出单的已实现盈亏）\n")
    print("| 卖出原因 | 笔数 | 胜率 | 合计盈亏 | 占初始资金 |")
    print("|---|---|---|---|---|")
    agg = fills_by_exit(dump)
    for k, a in sorted(agg.items(), key=lambda x: x[1]["pnl"]):
        print(f"| {k} | {a['n']} | {a['win']/a['n']*100:.1f}% | {a['pnl']:+,.0f} | "
              f"{a['pnl']/CAP*100:+.1f}pp |")
    print("\n### 1.3 最终清仓原因（回合口径：决定这一笔生死的那一单）\n")
    print("| 最后一个卖出原因 | 笔数 | 胜率 | 平均盈亏 | 总贡献 |")
    print("|---|---|---|---|---|")
    g = defaultdict(list)
    for r in rts:
        g[label(r["last_type"], r["last_reason"])].append(r)
    for k, sub in sorted(g.items(), key=lambda x: sum(r["pnl"] for r in x[1])):
        pnl = sum(r["pnl"] for r in sub)
        print(f"| {k} | {len(sub)} | {sum(1 for r in sub if r['pnl']>0)/len(sub)*100:.1f}% | "
              f"{pnl/len(sub):+,.0f} | {pnl:+,.0f} |")


def table_entry_reason(rts):
    print("\n### 1.4 按入场通道 × 持有分层（负贡献来自哪种入场）\n")
    print("| 入场通道 | 笔数 | 胜率 | ≤10天笔数 | ≤10天贡献 | >10天贡献 | 总贡献 |")
    print("|---|---|---|---|---|---|---|")
    g = defaultdict(list)
    for r in rts:
        ch = r["entry_reason"].split(":")[0].split("(")[0].strip()
        g[ch].append(r)
    for k, sub in sorted(g.items(), key=lambda x: sum(r["pnl"] for r in x[1])):
        short = [r for r in sub if r["hold"] <= 10]
        long_ = [r for r in sub if r["hold"] > 10]
        print(f"| {k} | {len(sub)} | {sum(1 for r in sub if r['pnl']>0)/len(sub)*100:.1f}% | "
              f"{len(short)} | {sum(r['pnl'] for r in short):+,.0f} | "
              f"{sum(r['pnl'] for r in long_):+,.0f} | {sum(r['pnl'] for r in sub):+,.0f} |")


def table_vol_group(rts):
    print("\n### 1.5 B6 高低波动分组（按入场时20日日收益波动率三分位）\n")
    vs = sorted(r["vol20"] for r in rts if r["vol20"] > 0)
    if not vs:
        print("(无 vol20 数据)")
        return
    q1 = vs[len(vs) // 3]
    q2 = vs[len(vs) * 2 // 3]
    groups = [("低波动组", lambda v: v <= q1), ("中波动组", lambda v: q1 < v <= q2),
              ("高波动组", lambda v: v > q2)]
    print(f"（阈值: 低≤{q1*100:.2f}%日波动, 中≤{q2*100:.2f}%, 高>{q2*100:.2f}%）\n")
    print("| 波动分组 | 笔数 | 胜率 | 平均盈亏 | 总贡献 | 平均持有 | ≤10天笔数 | ≤10天贡献 |")
    print("|---|---|---|---|---|---|---|---|")
    for name, fn in groups:
        sub = [r for r in rts if fn(r["vol20"])]
        if not sub:
            continue
        short = [r for r in sub if r["hold"] <= 10]
        pnl = sum(r["pnl"] for r in sub)
        print(f"| {name} | {len(sub)} | {sum(1 for r in sub if r['pnl']>0)/len(sub)*100:.1f}% | "
              f"{pnl/len(sub):+,.0f} | {pnl:+,.0f} | {sum(r['hold'] for r in sub)/len(sub):.1f}天 | "
              f"{len(short)} | {sum(r['pnl'] for r in short):+,.0f} |")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "dumps", "基线.json")
    d = load(path)
    eq = equity_series(d)
    m = metrics(eq)
    print(f"基线: 年化{m['ann']:+.2f}% 回撤{m['mdd']:.2f}% 夏普{m['sharpe']:.2f} "
          f"总收益{m['total']:+.2f}% ({m['start']}→{m['end']})")
    rts = rt_with_vol(d)
    print(f"回合数 {len(rts)} | 平均持有 {sum(r['hold'] for r in rts)/len(rts):.1f}天 | "
          f"胜率 {sum(1 for r in rts if r['pnl']>0)/len(rts)*100:.1f}%")
    table_hold(rts)
    table_exit(d, rts)
    table_entry_reason(rts)
    table_vol_group(rts)


if __name__ == "__main__":
    main()
