#!/usr/bin/env python3
"""H5: 31天以上长单能不能事前识别?

读 dumps/基线.json, 用每笔建仓时的 ? feat ? 快照(入场当日特征) 做区分度检验:
  1. 单特征 AUC (Mann-Whitney): 长单 vs 短单 的可分性
  2. 中位数分组: 高/低两半的长单占比、平均持有、总盈亏
  3. 三分位: 长单占比 与 盈亏 是否单调
  4. 逻辑回归 时序外样本 (2019-2023训练 → 2024-2026检验)
  5. 基准率对比: 随机挑 N 笔的期望长单数
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "entry"))
from analyze import load, round_trips  # noqa

FEATS = [
    ("rsi", "入场RSI"),
    ("vma", "量比(当日量/20日均量)"),
    ("px_ma5", "价距MA5(%)"),
    ("px_ma60", "价距MA60(%)"),
    ("ma5_ma20", "MA5/MA20乖离(%)"),
    ("atr_pct", "ATR占价(%)"),
    ("vol20", "20日波动率(%)"),
    ("mom", "动量分"),
    ("rs20", "相对沪深300强弱(pp)"),
    ("rank", "动量排名"),
    ("npos", "建仓时已有持仓数"),
    ("timing", "择时档位"),
]


def feat_rt(dump):
    """回合 + 首笔买入的特征快照"""
    first = {}
    for f in dump["ledger"]:
        if f["kind"] == "buy" and f["rt"] not in first:
            first[f["rt"]] = f.get("feat") or {}
    days = {}
    for f in dump["ledger"]:
        if f["kind"] == "sell" and f.get("hold_days"):
            days[f["rt"]] = max(days.get(f["rt"], 0), f["hold_days"])
    out = []
    for r in round_trips(dump):
        r["feat"] = first.get(r["rt"], {})
        out.append(r)
    return out


def auc(vals_pos, vals_neg):
    """P(长单值 > 短单值)"""
    if not vals_pos or not vals_neg:
        return 0.5
    s = 0.0
    for a in vals_pos:
        for b in vals_neg:
            s += 1.0 if a > b else (0.5 if a == b else 0.0)
    return s / (len(vals_pos) * len(vals_neg))


def tertile(vals):
    v = sorted(vals)
    return v[len(v) // 3], v[2 * len(v) // 3]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "dumps", "基线.json")
    rts = feat_rt(load(path))
    for r in rts:
        r["long"] = r["hold"] >= 31
    long_ = [r for r in rts if r["long"]]
    short = [r for r in rts if not r["long"]]
    n, nl = len(rts), len(long_)
    print(f"样本: {n} 回合, 长单(≥31天) {nl} 笔 = {nl/n*100:.1f}%, "
          f"平均持有 {sum(r['hold'] for r in rts)/n:.1f}天")
    print(f"长单合计盈亏 {sum(r['pnl'] for r in long_):+,.0f} / 全样本 {sum(r['pnl'] for r in rts):+,.0f}")

    print("\n### 5.1 单特征区分度 (AUC: 0.5=完全不可分, 1=完美可分)\n")
    print("| 特征 | 长单中位数 | 短单中位数 | AUC | 判定 |")
    print("|---|---|---|---|---|")
    rows = []
    for key, label in FEATS:
        lp = [r["feat"].get(key) for r in long_ if r["feat"].get(key) is not None]
        sp = [r["feat"].get(key) for r in short if r["feat"].get(key) is not None]
        if len(lp) < 10 or len(sp) < 10:
            continue
        a = auc(lp, sp)
        med = lambda v: sorted(v)[len(v) // 2]
        verdict = ("有可分辨倾向" if abs(a - 0.5) >= 0.10 else
                   ("弱倾向" if abs(a - 0.5) >= 0.05 else "不可分"))
        rows.append((label, key, med(lp), med(sp), a, verdict))
        print(f"| {label} | {med(lp):.3f} | {med(sp):.3f} | {a:.3f} | {verdict} |")
    rows.sort(key=lambda x: -abs(x[4] - 0.5))

    best = rows[0] if rows else None
    if best:
        print(f"\n最强单特征: {best[0]} (AUC={best[4]:.3f})")

    print("\n### 5.2 各特征三分位分组: 长单占比与盈亏\n")
    print("| 特征 | 低三分位 长单占比/盈亏 | 中三分位 | 高三分位 | 单调? |")
    print("|---|---|---|---|---|")
    for label, key, _, _, a, _ in rows[:6]:
        vals = [r["feat"].get(key) for r in rts if r["feat"].get(key) is not None]
        t1, t2 = tertile(vals)
        cells, ratios = [], []
        for lo, hi in ((-1e9, t1), (t1, t2), (t2, 1e9)):
            sub = [r for r in rts if r["feat"].get(key) is not None and lo < r["feat"][key] <= hi]
            if not sub:
                cells.append("—"); continue
            lr = sum(1 for r in sub if r["long"]) / len(sub) * 100
            pnl = sum(r["pnl"] for r in sub)
            ratios.append(lr)
            cells.append(f"{lr:.0f}% / {pnl:+,.0f}")
        mono = ("递增" if ratios == sorted(ratios) else
                ("递减" if ratios == sorted(ratios, reverse=True) else "非单调"))
        print(f"| {label} | " + " | ".join(cells) + f" | {mono} |")

    print("\n### 5.3 逻辑回归 时序外样本检验 (2019-2023 训练 → 2024-2026 检验)\n")
    keys = ["rsi", "vma", "px_ma5", "atr_pct", "rs20", "rank"]
    try:
        import numpy as np
        X, y, dt = [], [], []
        for r in rts:
            if any(r["feat"].get(k) is None for k in keys):
                continue
            X.append([r["feat"][k] for k in keys]); y.append(1.0 if r["long"] else 0.0)
            dt.append(r["open"])
        X = np.array(X, dtype=float); y = np.array(y); dt = np.array(dt)
        mu, sd = X.mean(0), X.std(0) + 1e-9
        Z = (X - mu) / sd
        Z = np.c_[np.ones(len(Z)), Z]
        tr = dt < "2024-01-01"
        w = np.zeros(Z.shape[1])
        for _ in range(200):
            p = 1 / (1 + np.exp(-Z[tr] @ w))
            g = Z[tr].T @ (p - y[tr]) + 1e-3 * w
            H = Z[tr].T @ (Z[tr] * (p * (1 - p))[:, None]) + 1e-3 * np.eye(len(w))
            w -= np.linalg.solve(H, g)
        p_all = 1 / (1 + np.exp(-Z @ w))
        def boots_auc(score, label):
            pos = score[(label == 1)]; neg = score[(label == 0)]
            if len(pos) < 5 or len(neg) < 5:
                return None
            return auc(list(pos), list(neg))
        a_tr = boots_auc(p_all[tr], y[tr])
        te = ~tr
        a_te = boots_auc(p_all[te], y[te])
        print(f"训练段 2019-2023: 长单 {int(y[tr].sum())}/{int(tr.sum())}, AUC={a_tr:.3f}")
        print(f"检验段 2024-2026: 长单 {int(y[te].sum())}/{int(te.sum())}, AUC={a_te:.3f}")
        print("系数(标准化): " + ", ".join(f"{k}={w[i+1]:+.2f}" for i, k in enumerate(keys)))
        print("解读: 检验段 AUC≈0.5 即'事前无法区分'; 训练段高但检验段掉回 0.5 = 纯拟合噪声")
    except Exception as e:
        print(f"(回归跳过: {e})")

    print("\n### 5.4 基准率: 长单是'运气'的定量含义\n")
    p = nl / n
    print(f"无条件长单概率 = {p*100:.1f}%。若某规则把长单占比提到 X%, "
          f"对应提升倍数 X/{p*100:.1f}。")
    print(f"随机抽 72 笔, 其中长单数的期望 = {72*p:.0f} 笔 (对照真实 {nl} 笔)")


if __name__ == "__main__":
    main()
