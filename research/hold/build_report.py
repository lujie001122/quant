#!/usr/bin/env python3
"""把 results/*.json 拼成报告表格 (避免手抄数字)"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SEG = ["2019-21", "22-23", "24-26", "近9月"]
YEARS = [str(y) for y in range(2019, 2027)]


def load(name):
    p = os.path.join(HERE, "results", f"{name}.json")
    return json.load(open(p)) if os.path.exists(p) else []


def table(rows, title=None):
    base = rows[0]
    if title:
        print(f"\n**{title}**\n")
    print("| 策略 | 年化 | Δpp | 回撤 | 夏普 | Δ夏普 | 回合 | 胜率 | 均持(天) | "
          "31天+笔数 | 31天+盈亏 | 31天+占比 | 仓位% |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        f = r["full"]
        b = r["buckets"]["31天以上"]
        tot = sum(v["pnl"] for v in r["buckets"].values()) or 1
        print(f"| {r['name']} | {f['ann']:+.2f}% | {f['ann']-base['full']['ann']:+.2f} | "
              f"{f['mdd']:.2f}% | {f['sharpe']:.2f} | {f['sharpe']-base['full']['sharpe']:+.2f} | "
              f"{r['trades']} | {r['win_rate']:.1f}% | {r['avg_hold']:.1f} | "
              f"{b['n']} | {b['pnl']:+,.0f} | {b['pnl']/tot*100:.0f}% | "
              f"{r['exposure']:.0f} |")


def segtable(rows, title="四段年化 + 分年总收益"):
    print(f"\n**{title}**\n")
    print("| 策略 | " + " | ".join(SEG) + " | " + " | ".join(YEARS) + " |")
    print("|---|" + "---|" * (len(SEG) + len(YEARS)))
    for r in rows:
        s = " | ".join(f"{r['segments'][k]:+.0f}%" if r["segments"].get(k) is not None else "—"
                       for k in SEG)
        y = " | ".join(f"{r['years'][k]:+.0f}%" if r["years"].get(k) is not None else "—"
                       for k in YEARS)
        print(f"| {r['name']} | {s} | {y} |")


def main():
    for tag in ("H1", "H2", "H3", "H4", "H5"):
        rows = load(tag)
        if rows:
            table(rows, f"{tag} 全期")
            segtable(rows)
    for tag in ("H2x", "H4x"):
        rows = load(tag)
        if rows:
            table(rows, f"{tag} 邻域")
    rows = load("H45")
    if rows:
        table(rows, "叠加")
        segtable(rows, "叠加 · 四段与分年")
    # GRID 矩阵: Δpp
    g = load("GRID")
    if g:
        base = g[0]
        print("\n**GRID: 锁盈 × 持有保护 (年化, 括号=Δpp vs 基线)**\n")
        print("| 锁盈 | 保护0日 | 保护5日 | 保护6日 | 保护8日 | 保护10日 |")
        print("|---|---|---|---|---|---|")
        for l in (9, 11, 12, 14, 16):
            cells = []
            for h in (0, 5, 6, 8, 10):
                name = f"锁盈{l}%_保护{h}日"
                r = next((x for x in g if x["name"] == name), None)
                cells.append(f"{r['full']['ann']:+.2f}% ({r['full']['ann']-base['full']['ann']:+.2f})"
                             if r else "—")
            print(f"| {l}% | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
