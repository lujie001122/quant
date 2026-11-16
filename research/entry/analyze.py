#!/usr/bin/env python3
"""逐笔归因 / 分段绩效 分析工具（纯读取 --dump 出来的 json，不改回测）"""
import json
import math
import sys
from collections import defaultdict
from datetime import datetime

RF_ANNUAL = 0.02


def load(path):
    with open(path) as f:
        return json.load(f)


def equity_series(dump):
    """[(date, value)] 升序"""
    return [(e["date"], float(e["value"])) for e in dump["equity"]]


def _slice(eq, start=None, end=None):
    return [(d, v) for d, v in eq if (start is None or d >= start) and (end is None or d <= end)]


def metrics(eq, start=None, end=None):
    """年化 / 最大回撤 / 夏普 / 总收益（区间内净值曲线口径）"""
    s = _slice(eq, start, end)
    if len(s) < 3:
        return None
    v0, v1 = s[0][1], s[-1][1]
    d0 = datetime.strptime(s[0][0], "%Y-%m-%d")
    d1 = datetime.strptime(s[-1][0], "%Y-%m-%d")
    years = (d1 - d0).days / 365.0
    total = (v1 / v0 - 1) * 100
    ann = ((v1 / v0) ** (1 / years) - 1) * 100 if years > 0.05 else 0.0
    peak = s[0][1]
    mdd = 0.0
    for _, v in s:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak * 100)
    rets = []
    for i in range(1, len(s)):
        rets.append(s[i][1] / s[i - 1][1] - 1)
    n = len(rets)
    if n > 2:
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / (n - 1)
        sd = math.sqrt(var)
        sharpe = (mean - RF_ANNUAL / 252) / sd * math.sqrt(252) if sd > 0 else 0.0
    else:
        sharpe = 0.0
    return {"ann": ann, "mdd": mdd, "sharpe": sharpe, "total": total,
            "days": len(s), "start": s[0][0], "end": s[-1][0]}


def round_trips(dump, start=None, end=None):
    """把成交台账聚合成"回合"（空仓→空仓 算一笔）"""
    rt = defaultdict(lambda: {"buys": 0.0, "sells": 0.0, "pnl": 0.0, "open_date": None,
                              "close_date": None, "code": None, "reasons": [],
                              "stypes": [], "entry_reasons": []})
    for f in dump["ledger"]:
        key = f["rt"]
        r = rt[key]
        r["code"] = f["code"]
        if f["kind"] == "buy":
            r["buys"] += float(f["size"]) * float(f["price"])
            r["pnl"] += float(f["pnl"])  # = -comm
            if r["open_date"] is None:
                r["open_date"] = f["date"]
            r["entry_reasons"].append(f["reason"])
        else:
            r["sells"] += float(f["size"]) * float(f["price"])
            r["pnl"] += float(f["pnl"])
            r["close_date"] = f["date"]
            r["reasons"].append(f["reason"])
            r["stypes"].append(f["stype"])
    out = []
    for k, r in rt.items():
        if r["open_date"] is None or r["close_date"] is None:
            continue
        if start and r["close_date"] < start:
            continue
        if end and r["close_date"] > end:
            continue
        hold = (datetime.strptime(r["close_date"], "%Y-%m-%d")
                - datetime.strptime(r["open_date"], "%Y-%m-%d")).days
        cost = r["buys"] if r["buys"] > 0 else 1.0
        out.append({"rt": k, "code": r["code"], "open": r["open_date"],
                    "close": r["close_date"], "hold": hold, "pnl": r["pnl"],
                    "ret_pct": r["pnl"] / cost * 100,
                    "n_buys": len(r["entry_reasons"]),
                    "last_type": r["stypes"][-1] if r["stypes"] else "",
                    "last_reason": r["reasons"][-1] if r["reasons"] else "",
                    "entry_reason": r["entry_reasons"][0] if r["entry_reasons"] else ""})
    out.sort(key=lambda x: x["close"])
    return out


HOLD_BUCKETS = [("1-3天", 1, 3), ("4-10天", 4, 10), ("11-30天", 11, 30),
                ("31天以上", 31, 10 ** 6)]


def hold_bucket(days):
    for name, lo, hi in HOLD_BUCKETS:
        if lo <= days <= hi:
            return name
    return "0天(当日)"


STYPE_LABEL = {
    "trail": "移动止盈",
    "liquidate_trailing": "移动止盈",
    "sell_active_5pct": "破MA5卖活动仓",
    "reduce_30pct_all": "破MA20减仓(1级)",
    "liquidate_trend": "破MA20/趋势恶化清仓",
    "trend_profit_sell": "趋势止盈(卖10%)",
    "trend_profit_sell_enhanced": "趋势止盈增强(卖20%)",
    "sell_active_10pct": "阶梯止盈10%",
    "sell_active_15pct": "阶梯止盈15%",
    "sell_active_20pct": "阶梯止盈20%",
    "hard_stop_25pct": "硬止损(峰值回撤)",
    "avg_stop_20pct": "均价止损",
    "breakeven_stop": "保本/浮盈止损",
    "liquidate_60pct": "硬止盈(+60%)",
    "drop_out_top": "跌出TOP-N清仓",
    "ma60_filter": "MA60过滤清仓",
    "timing_trim": "择时降档减仓",
}


def label(stype, reason=""):
    if stype in STYPE_LABEL:
        return STYPE_LABEL[stype]
    if "移动止盈" in reason:
        return "移动止盈"
    if not stype:
        return "其他/未标注"
    return stype


def fills_by_exit(dump, start=None, end=None):
    """按卖出原因聚合（成交笔口径）"""
    agg = defaultdict(lambda: {"n": 0, "win": 0, "pnl": 0.0, "hold": 0})
    for f in dump["ledger"]:
        if f["kind"] != "sell":
            continue
        if start and f["date"] < start:
            continue
        if end and f["date"] > end:
            continue
        k = label(f["stype"], f["reason"])
        a = agg[k]
        a["n"] += 1
        a["pnl"] += float(f["pnl"])
        a["hold"] += f["hold_days"]
        if float(f["pnl"]) > 0:
            a["win"] += 1
    return agg


def fmt_pct(x):
    return f"{x:+.2f}%"


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "base.json"
    d = load(path)
    eq = equity_series(d)
    m = metrics(eq)
    print(f"全期: 年化{m['ann']:+.2f}% 回撤{m['mdd']:.2f}% 夏普{m['sharpe']:.2f} 总收益{m['total']:+.2f}% ({m['start']}→{m['end']})")
    rts = round_trips(d)
    print(f"回合数: {len(rts)}")
    for b, _, _ in HOLD_BUCKETS:
        sub = [r for r in rts if hold_bucket(r["hold"]) == b]
        if not sub:
            continue
        win = sum(1 for r in sub if r["pnl"] > 0)
        print(f"  {b:8s} n={len(sub):4d} 胜率{win/len(sub)*100:5.1f}% "
              f"平均{sum(r['pnl'] for r in sub)/len(sub):+9.0f} 合计{sum(r['pnl'] for r in sub):+10.0f}")
    print("卖出原因:")
    for k, a in sorted(fills_by_exit(d).items(), key=lambda x: x[1]["pnl"]):
        print(f"  {k:22s} n={a['n']:4d} 胜率{a['win']/a['n']*100:5.1f}% 合计{a['pnl']:+10.0f}")
