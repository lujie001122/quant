#!/usr/bin/env python3
"""显著性: 配对 block bootstrap (20日块, 2000次) —— Δ夏普 / Δ年化 的 95% CI

用法: ../../.venv/bin/python3 boot.py <A.json> <B.json> [标签A] [标签B]
A = 对照(基线), B = 候选
"""
import json
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "entry"))
from analyze import load, equity_series  # noqa

RF = 0.02


def daily(dump):
    eq = equity_series(dump)
    return [(eq[i][0], eq[i][1] / eq[i - 1][1] - 1) for i in range(1, len(eq))]


def stats(rets):
    n = len(rets)
    mu = sum(rets) / n
    var = sum((r - mu) ** 2 for r in rets) / (n - 1)
    sd = math.sqrt(var)
    sh = (mu - RF / 252) / sd * math.sqrt(252) if sd > 0 else 0.0
    ann = (math.prod([1 + r for r in rets])) ** (252.0 / n) - 1
    return sh, ann * 100


def boot(a_path, b_path, reps=2000, block=20, seed=42):
    ra = dict(daily(load(a_path)))
    rb = dict(daily(load(b_path)))
    dates = sorted(d for d in ra if d in rb)
    xa = [ra[d] for d in dates]
    xb = [rb[d] for d in dates]
    sa, aa = stats(xa)
    sb, ab = stats(xb)
    n = len(dates)
    nb = n // block
    random.seed(seed)
    ds, da = [], []
    for _ in range(reps):
        idx = []
        for _ in range(nb):
            s = random.randrange(0, n - block)
            idx.extend(range(s, s + block))
        s_a, a_a = stats([xa[i] for i in idx])
        s_b, a_b = stats([xb[i] for i in idx])
        ds.append(s_b - s_a)
        da.append(a_b - a_a)
    ds.sort(); da.sort()
    q = lambda v, p: v[int(p * (len(v) - 1))]
    return {
        "n_days": n, "sa": sa, "sb": sb, "aa": aa, "ab": ab,
        "d_sh": sb - sa, "d_ann": ab - aa,
        "sh_lo": q(ds, 0.025), "sh_hi": q(ds, 0.975),
        "ann_lo": q(da, 0.025), "ann_hi": q(da, 0.975),
        "p_sh": sum(1 for x in ds if x <= 0) / len(ds),
        "p_ann": sum(1 for x in da if x <= 0) / len(da),
    }


def main():
    pairs = []
    args = sys.argv[1:]
    if len(args) >= 2:
        pairs.append((args[0], args[1], args[2] if len(args) > 2 else "A",
                      args[3] if len(args) > 3 else "B"))
    else:
        # 默认: 所有通过初筛的候选 vs 基线
        d = os.path.join(HERE, "dumps")
        base = os.path.join(d, "基线.json")
        for name, tag in [("锁盈12pct", "H2 锁盈12%"), ("锁盈11pct", "H2 锁盈11%"),
                          ("前8日", "H4 前8日"), ("浮盈gt5pct+老赢家加仓", "H3h"),
                          ("锁盈12pct+前8日", "H2+H4")]:
            p = os.path.join(d, f"{name}.json")
            if os.path.exists(p):
                pairs.append((base, p, "基线", tag))
    print("| 候选 | 夏普Δ | 95% CI(夏普Δ) | 年化Δ | 95% CI(年化Δ) | P(Δ夏普≤0) | 判定 |")
    print("|---|---|---|---|---|---|---|")
    out = []
    for a, b, ta, tb in pairs:
        r = boot(a, b)
        verdict = ("显著为正" if r["sh_lo"] > 0 else
                   ("显著为负" if r["sh_hi"] < 0 else "噪声内(含0)"))
        print(f"| {tb} | {r['d_sh']:+.3f} | [{r['sh_lo']:+.3f}, {r['sh_hi']:+.3f}] | "
              f"{r['d_ann']:+.2f}pp | [{r['ann_lo']:+.2f}, {r['ann_hi']:+.2f}] | "
              f"{r['p_sh']:.2f} | {verdict} |")
        out.append({"cand": tb, **r})
    with open(os.path.join(HERE, "results", "bootstrap.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
