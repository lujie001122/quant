#!/usr/bin/env python3
"""把 results/*.json 汇总成 REPORT.md（只读结果文件 + 台账，不重跑回测）"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "research", "entry"))
from analyze import load, equity_series, metrics  # noqa

YEARS = [str(y) for y in range(2019, 2027)]
SEGS = ["19-21", "22-23", "24-26", "近9月"]


def rows(batch):
    path = os.path.join(HERE, "results", f"{batch}.json")
    with open(path) as f:
        return json.load(f)


def concurrency(dump):
    d = load(dump)
    spans = {}
    for fl in d["ledger"]:
        r = fl["rt"]
        s = spans.get(r)
        if s is None:
            spans[r] = [fl["date"], fl["date"]]
        else:
            s[0] = min(s[0], fl["date"])
            s[1] = max(s[1], fl["date"])
    spans = sorted(tuple(v) for v in spans.values())
    days = [e["date"] for e in d["equity"]]
    si, active, tot = 0, [], 0
    for dt in days:
        while si < len(spans) and spans[si][0] <= dt:
            active.append(spans[si][1])
            si += 1
        active = [e for e in active if e >= dt]
        tot += len(active)
    return tot / len(days), len(spans)


def main_table(rs, base, note=""):
    head = ("| 策略 | 年化 | Δpp | 回撤 | 夏普 | Δ夏普 | 平均仓位 | 笔数 | 胜率 | 均持(天) "
            "| 31天+笔 | 31天+盈亏占比 | 19-21 | 22-23 | 24-26 | 近9月 | 达标 |")
    sep = "|" + "---|" * 17
    out = [note, "", head, sep]
    for r in rs:
        f = r["full"]
        b31 = r["buckets"]["31天以上"]
        tot = sum(v["pnl"] for v in r["buckets"].values()) or 1
        seg = " | ".join((f"{r['segments'][k]:+.0f}%" if r["segments"][k] is not None else "—")
                         for k in SEGS)
        out.append(
            f"| {r['name']} | {f['ann']:+.2f}% | {f['ann']-base['full']['ann']:+.2f} | "
            f"{f['mdd']:.2f}% | {f['sharpe']:.2f} | {f['sharpe']-base['full']['sharpe']:+.2f} | "
            f"{r['exposure'] or 0:.1f}% | {r['trades']} | {r['win_rate']:.1f}% | {r['avg_hold']:.1f} | "
            f"{b31['n']} | {b31['pnl']/tot*100:.0f}% | {seg} | {'✓ 通过' if r['pass'] else '—'} |")
    return out


def main():
    w1, w2, w3, w4 = rows("W1"), rows("W2"), rows("W3"), rows("W4")
    base = w1[0]
    L = []
    a = L.append

    a("# research/width — 持仓宽度能否在回撤≤20%下把仓位/收益抬上去")
    a("")
    a("分支 `research/width`（main 未改动）。全期 2019-01-01→2026-11-13，"
      "`--momentum=simple`，数据 `data/kline_qfq` 前复权缓存，引擎 backtrader。")
    a("")
    a("基线命令（其余全部配置 = 实盘口径）:")
    a("```")
    a(".venv/bin/python3 backtest_bt.py --momentum=simple --start=2019-01-01 --end=2026-11-13 \\")
    a("  --concentrated-pyramid --top-n=5 --trail=e1 --lookback=10 --trend-entry --warmup")
    a("```")
    a("")
    a("口径说明:")
    a("- 夏普统一用净值曲线口径（日收益、无风险利率 2%/年，`research/entry/analyze.py`）。"
      "基线 0.66；引擎控制台口径为 0.63（同一曲线，日收益/无风险利率处理不同）。"
      "两种口径的达标结论一致：控制台口径下全场最高也只有 0.63，同样低于 0.75。")
    a("- 平均仓位取自 dump 的 `equity.cash/value`（1-cash/净值），与引擎 `exposure` "
      "记录逐日一致（基线 44.731% vs 44.731%）；`--no-timing` 时引擎不记 exposure 序列，"
      "故统一用现金口径。**未改引擎任何交易逻辑。**")
    a("- 达标线：年化≥13% 且 回撤≤20% 且 夏普≥0.75，三条同时满足。")
    a("")
    a( "**结论速览：四组全部不达标。最高年化仍是基线 10.23%。"
       "宽度（top-n）加不出仓位也加不出收益；加仓批数把仓位压低、回撤翻倍。**")
    a("")

    L += main_table(w1, base, "## 1. 第一组：同时持仓数量（top-n 5/6/8/10/12/15）")
    a("")
    a("并发持仓只数（按每笔回合的首末成交日统计）与每只占用：")
    a("")
    a("| top-n | 平均并发只数 | 平均每只占总资金 | 平均仓位 |")
    a("|---|---|---|---|")
    for r in w1:
        c, n = concurrency(r["dump"])
        a(f"| {r['name']} | {c:.2f} | {(r['exposure'] or 0)/c:.1f}% | {r['exposure'] or 0:.1f}% |")
    a("")
    a("读法：top-n 从 5 放到 15，平均仓位只从 44.7% 抬到 44.9%~50.3%（+0.2~+5.6pp），"
      "同时年化全部低于基线、夏普最多掉 0.33。按任务判据（仓位上去但夏普掉了=稀释），"
      "这是明确的稀释，不是成功。")
    a("")

    L += main_table(w2, base, "## 2. 第二组：三种抬仓位机制")
    a("")
    a("| 机制 | 代表配置 | Δ仓位 | Δ年化 | Δ夏普 | Δ回撤 | 判定 |")
    a("|---|---|---|---|---|---|---|")
    a("| (a) 宽持仓 | top-n=8 | +2.1pp | -2.67pp | -0.18 | +1.83pp | 稀释：多买 2pp 仓位，"
      "付出 0.18 夏普 |")
    a("| (a) 宽持仓 | top-n=12 | +2.6pp | -0.68pp | -0.04 | +1.90pp | 最不坏的宽度，但收益仍低于基线 |")
    a("| (b) 每槽加满 | top-n=5 + 4批 | -0.1pp | -0.54pp | -0.08 | +6.26pp | 仓位没动，回撤恶化 |")
    a("| (b) 每槽加满 | top-n=5 + 6批 | -3.0pp | -3.27pp | -0.29 | +22.12pp | 资金被加仓占死，仓位反降 |")
    a("| (b) 每槽加满 | top-n=5 + 8批 | -6.2pp | -1.97pp | -0.20 | +18.17pp | 同上，更糟 |")
    a("| (c) 结合 | top-n=8 + 4批 | -2.4pp | -0.69pp | -0.10 | +3.45pp | 仓位反而下降 |")
    a("| (c) 结合 | top-n=8 + 6批 | +6.3pp | -4.66pp | -0.34 | +7.47pp | 三者最差 |")
    a("")
    a("机制层面为什么 (b) 会降低仓位：加仓吃的是同一批资金，批数放宽后资金在已上涨标的上"
      "被反复占用（确认加仓占总买入金额 45.3%、1056 笔），新开仓被挤掉，回合数从 714 降到 441/333，"
      "同时把单票尾部风险放大——回撤从 12.9% 升到 31.0%/35.0%。")
    a("")

    L += main_table(w3, base, "## 3. 第三组：择时 × 持仓宽度")
    a("")
    a("| 宽度 | 择时开 年化/回撤/夏普 | 择时关 年化/回撤/夏普 | 仓位 开→关 | 结论 |")
    a("|---|---|---|---|---|")
    d = {r["name"]: r for r in w3 + w4}
    for w, on, off in (("t5", "t5 择时开", "t5 择时关"), ("t8", "t8 择时开", "t8 择时关"),
                       ("t10", "t10 择时开", "t10 择时关"), ("t12", "t12 择时开", "t12 择时关")):
        ro, rf = d[on], d[off]
        a(f"| {w} | {ro['full']['ann']:+.2f}% / {ro['full']['mdd']:.2f}% / {ro['full']['sharpe']:.2f} "
          f"| {rf['full']['ann']:+.2f}% / {rf['full']['mdd']:.2f}% / {rf['full']['sharpe']:.2f} "
          f"| {ro['exposure']:.1f}% → {rf['exposure']:.1f}% | "
          f"关掉择时只买到 +{rf['exposure']-ro['exposure']:.1f}pp 仓位，回撤 +{rf['full']['mdd']-ro['full']['mdd']:.1f}pp |")
    a("")
    a("择时没有被宽度稀释：每一个宽度下，关掉择时都只换来 +1.5~+6.1pp 仓位，"
      "回撤却升到 13.2%~22.0%（t8 关=20.13% 贴线、t12 关=22.04% 破线）。"
      "四档里 3 档 择时开 的夏普更高（t5 +0.04、t10 +0.04、t12 +0.22）；"
      "唯一例外是 t8：关掉后 9.49%/0.61 高于 7.56%/0.48，但回撤 20.13% 已经吃掉全部回撤预算——"
      "它不是“择时该关”的证据，而是“宽度本身就带 6pp 回撤”的证据。"
      "结论：择时在宽持仓下依然是唯一有效的回撤控制器，但它的价值被宽持仓的额外风险抵消掉了。")
    a("")

    a("## 4. 四段与分年（基线 vs 最强宽度变体 t12）")
    a("")
    a("| 策略 | 段 | 年化 | 回撤 | 夏普 |")
    a("|---|---|---|---|---|")
    seg_def = [("19-21", "2019-01-01", "2021-12-31"), ("22-23", "2022-01-01", "2023-12-31"),
               ("24-26", "2024-01-01", "2026-11-13"), ("近9月", "2026-02-13", "2026-11-13")]
    for r in (base, d["t12 基线"]):
        eq = equity_series(load(r["dump"]))
        for k, s0, s1 in seg_def:
            m = metrics(eq, s0, s1)
            a(f"| {r['name']} | {k} | {r['segments'][k]:+.1f}% | {r['segments_mdd'][k]:.1f}% | "
              f"{m['sharpe']:.2f} |")
    a("")
    a("| 分年收益 | " + " | ".join(YEARS) + " |")
    a("|---|" + "---|" * len(YEARS))
    for r in (base, d["t12 基线"], d["t8 择时开"]):
        a(f"| {r['name']} | " + " | ".join(
            (f"{r['years'][y]:+.1f}%" if r["years"][y] is not None else "—") for y in YEARS) + " |")
    a("")
    a("31 天以上长单：基线 72 笔、贡献 102% 的总盈亏（短单合计为负）；t12 为 208 笔、104%；"
      "t15 191 笔但只剩 85%——宽度把“长单赚钱”这件事的斜率摊薄了（均持 16.3→41.8 天，"
      "回合数 714→336），轮动退化成低换手持有。")
    a("")

    a("## 5. 邻域 + walk-forward")
    a("")
    a("### 5.1 t12 邻域（top-n ±1、lookback ±30%）")
    a("")
    a("| 配置 | 年化 | 回撤 | 夏普 | 平均仓位 |")
    a("|---|---|---|---|---|")
    for r in w4:
        a(f"| {r['name']} | {r['full']['ann']:+.2f}% | {r['full']['mdd']:.2f}% | "
          f"{r['full']['sharpe']:.2f} | {r['exposure']:.1f}% |")
    a("")
    a("t12 是孤峰不是平台：top-n 11 → 3.70%（回撤 27.74%），top-n 13 → 8.34%，"
      "lookback 7 → 7.12%，lookback 13 → 4.03%。邻域内 5 个点全部低于基线，"
      "说明 t12 的 9.56% 来自参数运气，不可作为可依赖的宽度结论。")
    a("")
    a("### 5.2 walk-forward（训练 3 年 → 测试 1 年，逐年选参）")
    a("")
    wf = json.load(open(os.path.join(HERE, "results", "wf.json")))
    a("| 测试年 | 训练窗口 | 逐年选中 |")
    a("|---|---|---|")
    for y in ("2022", "2023", "2024", "2025", "2026"):
        a(f"| {y} | {int(y)-3}→{int(y)-1} | {wf['selection'][y]} |")
    a("")
    a("| 方案 | 年化 | 回撤 | 夏普 | 累计 | 2022 | 2023 | 2024 | 2025 | 2026 |")
    a("|---|---|---|---|---|---|---|---|---|---|")
    for label in ("walk-forward(逐年选宽/批数)", "一直用基线t5"):
        r = wf[label]
        a(f"| {label} | {r['ann']:+.2f}% | {r['mdd']:.2f}% | {r['sharpe']:.2f} | {r['total']:+.1f}% | "
          + " | ".join(f"{r['years'][y]:+.1f}%" for y in ("2022", "2023", "2024", "2025", "2026")) + " |")
    a("")
    a("逐年选宽/加批数在样本外输给“一直用基线”3.8pp 年化（9.43% vs 13.25%）、夏普 0.52 vs 0.69。"
      "选参过程本身也有信号：5 个训练窗口里 4 个都选中了宽持仓/加批，"
      "但换到测试年就崩——宽度在训练窗口内是过拟合的产物。")
    a("")
    a("另外注意一个对任务目标有用的事实：同一段 2022-01→2026-11，“一直用基线 t5”是 "
      "13.25% / 15.69% / 0.69——年化和回撤都过线，只差夏普 0.06。"
      "也就是说 13% 这条线在近 5 年并不是遥不可及，缺的是把它变成全期都成立，"
      "而且没有任何一个宽度变体把它补上（t12/t8 在这 5 年都更低）。")
    a("")

    a("## 6. 结论：有没有一条能在回撤≤20%下做到 13%+")
    a("")
    a("**没有。** 三组主测 + 邻域 + walk-forward 全部低于 13%，也全部低于基线。")
    a("")
    a("卡在四个地方，都有数字：")
    a("")
    a("1. **宽度不是仓位的水龙头。** top-n 5→15，并发只数 4.41→5.06（顶到 5.63 就不再涨），"
      "平均仓位 44.7%→44.9~50.3%。原因是单槽建仓只投 0.5 槽、确认加仓又稀，"
      "放开“允许买谁”并不能凭空生成入场信号。")
    a("2. **单槽基数是绝对金额，不随净值走。** `fund_per_etf` 固定 ¥58,666（config: "
      "max_per_etf 220000 × 0.8 ÷ sizing_n 3）。期初占净值 26.7%，期末净值 47.4 万时只占 12.4%。"
      "这才是平均仓位 44.7%、且逐年从 59%（2019）滑到 31%（2026）的真正来源——不是 top-n。")
    a("3. **两种“抬仓位”的杠杆都在加风险而不是加收益。** 放宽加仓批数：仓位 44.6%→38.5%，"
      "回撤 12.9%→35.0%；放大单槽（此前结论 0.75/1.0）：年化 10.24→8.81→8.21。"
      "加仓位在这个信号集上是单调加尾部风险。")
    a("4. **择时是唯一被验证有效的风控，宽持仓下也依然有效**（4 个宽度全部 开>关 的回撤），"
      "它只压掉 1.5~6.1pp 仓位，救不回 13% 的缺口；反过来，关掉择时也换不到 13%——"
      "最好的一档 t8(关) 只有 9.49%。")
    a("")
    a("**距达标差多少**：基线 10.23% / 12.86% / 0.66（控制台口径 0.63），差 2.77pp 年化 + 0.09 夏普；"
      "全部宽度变体年化至少再低 0.68pp、夏普低 0.04 以上。差距不是“差一点仓位”，"
      "是宽的每一档都在同一个方向上更差。")
    a("")
    a("如果还要继续这条路，唯一没被这轮测过的数学出口是**按净值重算单槽基数（目标权重口径）**："
      "用现有成交序列折算，单槽随净值缩放后平均仓位约 67.5%（不是 95.6%），"
      "而 `research/structural` 的目标权重引擎 95.6% 仓位对应 21.10% 年化 / 31.96% 回撤——"
      "回撤直接破 20% 线。也就是说：仓位、收益、回撤在这套信号上是同一个变量，"
      "想拿 13%+ 就得接受 25%~32% 的回撤；想把回撤压在 20% 内，年化天花板就在 10% 附近。")
    a("")

    a("## 附：文件与复现")
    a("")
    a("| 文件 | 内容 |")
    a("|---|---|")
    a("| `research/width/runner.py` | W1-W4 批量回测（含仓位/并发/长单归因） |")
    a("| `research/width/wf.py` | walk-forward（训练3年→测试1年，逐年选参） |")
    a("| `research/width/dumps/*.json` | 每次回测的逐笔台账 + 净值 + 现金 |")
    a("| `research/width/results/*.json` | 四张表的结构化结果 |")
    a("| `research/width/logs/*.log` | 回测原始 stdout |")
    a("")
    a("复现：`.venv/bin/python3 research/width/runner.py W1 W2 W3 W4 --force` 然后 "
      "`.venv/bin/python3 research/width/wf.py`，最后 `.venv/bin/python3 research/width/build_report.py`。"
      "缓存存在时 runner 会跳过已跑过的配置（加 `--force` 重跑）。")
    a("")
    out = os.path.join(HERE, "REPORT.md")
    with open(out, "w") as f:
        f.write("\n".join(L) + "\n")
    print(f"✓ {out} ({len(L)} 行)")


if __name__ == "__main__":
    main()
