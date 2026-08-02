#!/usr/bin/env python3
"""
批量回测脚本: 对 38 个 ETF 逐个跑一年回测
复用 backtest_bt.py 中的 fetch_daily、ETFCommission、ETFStrategy

输出: 代码、名称、收益%、回撤%、夏普、胜率、笔数
按收益排序，最后标注建议加入标的池的候选
"""
import json, urllib.request, time, sys
from datetime import date
import backtrader as bt
import pandas as pd

from backtest_bt import fetch_daily, ETFCommission, ETFStrategy

# ═══════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════
TRADE_START = "2025-08-01"
TRADE_END   = "2026-07-27"
TOTAL_FUND  = 220000

CODES = [
    "159227", "159255", "159273", "159516", "159532", "159546", "159566",
    "159611", "159781", "159807", "159857", "159915", "159995", "510170",
    "512400", "512480", "512670", "512820", "512880", "512930", "513160",
    "513350", "513390", "513400", "513780", "515050", "515220", "515880",
    "516780", "516920", "518880", "560580", "560860", "562500", "562570",
    "563530", "588050", "588170",
]


# ═══════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════
def code_to_sid(code: str) -> str:
    """代码 → Sina sid: 1/3 开头 → sz, 5/6 开头 → sh"""
    return f"sz{code}" if code[0] in ("1", "3") else f"sh{code}"


def fetch_name(code: str) -> str:
    """从 Sina 行情接口获取 ETF 名称，失败则返回代码本身"""
    sid = code_to_sid(code)
    url = f"https://hq.sinajs.cn/list={sid}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("gbk")
            if '="' in text:
                name = text.split('="')[1].split(",")[0]
                if name.strip():
                    return name.strip()
    except Exception:
        pass
    return code


# ═══════════════════════════════════════════════
#  单标的回测
# ═══════════════════════════════════════════════
def run_single(code: str, sid: str) -> dict | None:
    """
    对单个 ETF 运行回测
    返回 {"code", "name", "return_pct", "max_dd", "sharpe", "win_rate", "trades}
    失败返回 None
    """
    try:
        df = fetch_daily(code, sid)
    except Exception as e:
        print(f"  ✗ {code} 数据拉取失败: {e}")
        return None

    # 截取回测区间
    start_ts = pd.Timestamp(TRADE_START)
    end_ts   = pd.Timestamp(TRADE_END)
    df = df[start_ts:end_ts]

    if len(df) < 30:
        print(f"  ✗ {code} 数据不足: {len(df)} 条")
        return None

    # ── Cerebro ──
    cerebro = bt.Cerebro()
    data = bt.feeds.PandasData(dataname=df, name=code)
    cerebro.adddata(data, name=code)

    cerebro.addstrategy(ETFStrategy)

    # Analyzers
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe",
                        timeframe=bt.TimeFrame.Days, annualize=True, riskfreerate=0.02)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")

    # 手续费 + 滑点
    cerebro.broker.addcommissioninfo(ETFCommission())
    cerebro.broker.setcash(TOTAL_FUND)
    cerebro.broker.set_coc(False)

    results = cerebro.run()
    strat = results[0]

    # ── 提取指标 ──
    final = cerebro.broker.getvalue()
    total_ret = (final - TOTAL_FUND) / TOTAL_FUND * 100

    dd = strat.analyzers.drawdown.get_analysis()
    max_dd = dd.max.drawdown

    sharpe_val = strat.analyzers.sharpe.get_analysis().get("sharperatio", 0) or 0

    ta = strat.analyzers.trades.get_analysis()
    total_trades = ta.get("total", {}).get("total", 0)
    won = ta.get("won", {}).get("total", 0)
    win_rate = won / total_trades * 100 if total_trades else 0

    return {
        "code":      code,
        "return_pct": total_ret,
        "max_dd":    max_dd,
        "sharpe":    sharpe_val,
        "win_rate":  win_rate,
        "trades":    total_trades,
    }


# ═══════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════
def main():
    print("=" * 88)
    print("  批量回测: 38 ETF × 1年  (2025-08-01 → 2026-07-27)")
    print(f"  起始资金: ¥{TOTAL_FUND:,}")
    print("=" * 88)

    # ── 1. 获取名称 ──
    print("\n▸ 获取 ETF 名称 ...")
    name_map = {}
    for code in CODES:
        name = fetch_name(code)
        name_map[code] = name
        print(f"  {code} → {name}")

    # ── 2. 逐个回测 ──
    results = []
    total = len(CODES)
    print(f"\n▸ 开始逐个回测 ({total} 只) ...")
    t_all = time.time()

    for i, code in enumerate(CODES, 1):
        sid = code_to_sid(code)
        name = name_map[code]
        print(f"  [{i:2d}/{total}] {code} {name:<12s} ...", end=" ", flush=True)
        t0 = time.time()
        r = run_single(code, sid)
        elapsed = time.time() - t0
        if r:
            r["name"] = name
            results.append(r)
            print(f"收益 {r['return_pct']:+7.2f}%  回撤 {r['max_dd']:.2f}%  "
                  f"夏普 {r['sharpe']:.2f}  ({elapsed:.1f}s)")
        else:
            print(f"跳过 ({elapsed:.1f}s)")

    elapsed_all = time.time() - t_all

    # ── 3. 按收益排序 ──
    results.sort(key=lambda x: x["return_pct"], reverse=True)

    # ── 4. 输出表格 ──
    print(f"\n{'=' * 88}")
    print(f"  {'代码':<8} {'名称':<14} {'收益%':>9} {'回撤%':>8} {'夏普':>8} "
          f"{'胜率%':>8} {'笔数':>6}")
    print(f"  {'─' * 84}")
    for r in results:
        print(f"  {r['code']:<8} {r['name']:<14} {r['return_pct']:>+9.2f} "
              f"{r['max_dd']:>8.2f} {r['sharpe']:>8.2f} {r['win_rate']:>8.1f} "
              f"{r['trades']:>6d}")
    print(f"  {'─' * 84}")

    # ── 5. 候选标注 ──
    # 条件: 收益>0 且 夏普>0 且 胜率>40% 且 笔数>=3
    candidates = [
        r for r in results
        if r["return_pct"] > 0 and r["sharpe"] > 0
        and r["win_rate"] > 40 and r["trades"] >= 3
    ]

    print(f"\n  ★ 建议加入标的池的候选 ({len(candidates)} 只):")
    print(f"  {'─' * 84}")
    for r in candidates:
        print(f"  ★ {r['code']:<6} {r['name']:<14} {r['return_pct']:>+9.2f}%  "
              f"夏普 {r['sharpe']:.2f}  胜率 {r['win_rate']:.1f}%  "
              f"{r['trades']}笔")
    print(f"  {'─' * 84}")

    # ── 汇总 ──
    print(f"\n{'=' * 88}")
    print(f"  批量回测完成: {len(results)}/{total} 只成功  "
          f"{len(candidates)} 只候选  总耗时 {elapsed_all:.0f}s")
    print(f"{'=' * 88}")


if __name__ == "__main__":
    main()
