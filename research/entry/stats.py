#!/usr/bin/env python3
"""显著性检验: 日收益 block bootstrap 看夏普差异是否只是噪声"""
import json
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from analyze import load, equity_series, metrics  # noqa


def daily_returns(dump):
    eq = equity_series(dump)
    return [(eq[i][0], eq[i][1] / eq[i - 1][1] - 1) for i in range(1, len(eq))]


def sharpe(rets):
    n = len(rets)
    if n < 3:
        return 0.0
    mu = sum(rets) / n
    var = sum((r - mu) ** 2 for r in rets) / (n - 1)
    sd = math.sqrt(var)
    return (mu - 0.02 / 252) / sd * math.sqrt(252) if sd > 0 else 0.0


def main():
    a_path, b_path = sys.argv[1], sys.argv[2]
    a = load(a_path); b = load(b_path)
    ra = dict(daily_returns(a)); rb = dict(daily_returns(b))
    dates = [d for d in ra if d in rb]
    xa = [ra[d] for d in dates]; xb = [rb[d] for d in dates]
    sa, sb = sharpe(xa), sharpe(xb)
    diff = sb - sa
    # 配对 block bootstrap
    n = len(dates)
    block = 20
    nblocks = n // block
    random.seed(42)
    diffs = []
    for _ in range(2000):
        idx = []
        for _ in range(nblocks):
            s = random.randrange(0, n - block)
            idx.extend(range(s, s + block))
        diffs.append(sharpe([xb[i] for i in idx]) - sharpe([xa[i] for i in idx]))
    diffs.sort()
    lo, hi = diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs))]
    p_neg = sum(1 for d in diffs if d <= 0) / len(diffs)
    print(f"A={os.path.basename(a_path)} 夏普 {sa:.3f}")
    print(f"B={os.path.basename(b_path)} 夏普 {sb:.3f}")
    print(f"Δ夏普 = {diff:+.3f}   95% 置信区间 [{lo:+.3f}, {hi:+.3f}]   P(Δ≤0) = {p_neg:.2f}")
    print("判定:", "显著为正" if lo > 0 else ("不显著(含0)" if hi > 0 else "显著为负"))


if __name__ == "__main__":
    main()
