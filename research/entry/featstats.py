#!/usr/bin/env python3
"""入场特征条件收益分析: 哪个入场特征能事前分辨"短命亏损单" """
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from analyze import load, round_trips  # noqa

FEATS = ["rank", "mom", "rsi", "atr_pct", "ma5_ma20", "px_ma5", "px_ma60",
         "rs20", "vma", "vol20", "timing", "npos"]
LABEL = {"rank": "动量排名位次", "mom": "10日动量%", "rsi": "RSI", "atr_pct": "ATR%",
         "ma5_ma20": "MA5/MA20乖离%", "px_ma5": "价距MA5%", "px_ma60": "价距MA60%",
         "rs20": "20日相对沪深300(pp)", "vma": "量比(量/20日均量)", "vol20": "20日波动率%",
         "timing": "择时档位", "npos": "入场时持仓只数"}


def rts_with_feat(dump):
    first = {}
    for f in dump["ledger"]:
        if f["kind"] == "buy" and f["rt"] not in first:
            first[f["rt"]] = f.get("feat") or {}
    out = round_trips(dump)
    keep = []
    for r in out:
        ft = first.get(r["rt"], {})
        r["feat"] = ft
        keep.append(r)
    return keep


def qstats(sub):
    n = len(sub)
    if not n:
        return None
    win = sum(1 for r in sub if r["pnl"] > 0)
    pnl = sum(r["pnl"] for r in sub)
    short = [r for r in sub if r["hold"] <= 10]
    return {"n": n, "win": win / n * 100, "avg": pnl / n, "pnl": pnl,
            "short_n": len(short), "short_pct": len(short) / n * 100,
            "short_pnl": sum(r["pnl"] for r in short),
            "hold": sum(r["hold"] for r in sub) / n}


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "dumps", "基线.json")
    d = load(path)
    rts = rts_with_feat(d)
    rts = [r for r in rts if r["feat"]]
    print(f"回合数(有特征) {len(rts)}  总计 {sum(r['pnl'] for r in rts):+,.0f}\n")
    for f in FEATS:
        vals = sorted([r["feat"].get(f) for r in rts if r["feat"].get(f) is not None])
        if len(vals) < 20:
            continue
        # 5分位
        cuts = [vals[int(len(vals) * k / 5)] for k in range(1, 5)]
        buckets = defaultdict(list)
        for r in rts:
            v = r["feat"].get(f)
            if v is None:
                continue
            idx = sum(1 for c in cuts if v >= c)
            buckets[idx].append(r)
        print(f"── {LABEL[f]} ({f}) 分位切点 {[round(c,2) for c in cuts]}")
        print("   区间            n    胜率   平均盈亏   总贡献   ≤10天占比  ≤10天贡献  均持")
        for idx in sorted(buckets):
            s = qstats(buckets[idx])
            lo = "低" if idx == 0 else ""
            hi = "高" if idx == 4 else ""
            rng = f"Q{idx+1}{lo}{hi}"
            print(f"   {rng:<12s} {s['n']:>4d} {s['win']:>5.1f}% {s['avg']:>+8.0f} "
                  f"{s['pnl']:>+9.0f} {s['short_pct']:>8.1f}% {s['short_pnl']:>+10.0f} {s['hold']:>6.1f}")
        print()


if __name__ == "__main__":
    main()
