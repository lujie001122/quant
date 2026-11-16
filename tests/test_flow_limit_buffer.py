#!/usr/bin/env python3
"""flow 模式限价跳空缓冲验收。

纯函数单测（不联网、不下单）：
  1. 跳空场景 现价1.000/涨停1.100/buffer1% → 1.010（不是1.000）
  2. 边界 现价0.300（±10%）→ 限价 ≤ 0.330
  3. 现价接近涨停 1.095/1.100 → 限价夹到 1.100
  4. 卖出方向 现价×(1-1%)，接近跌停时夹到跌停价
  5. 最小价位 / config 读取 / flow 分支打印格式

--live 额外跑一次实盘 dry-run（只读行情 + 生产函数，不下单），
逐只打印 flow 买入信号的 现价/限价/涨停。

用法:
  .venv/bin/python3 tests/test_flow_limit_buffer.py
  .venv/bin/python3 tests/test_flow_limit_buffer.py --live
"""
import os
import sys
from contextlib import redirect_stdout
from io import StringIO

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, REPO_DIR)

import yaml                                            # noqa: E402
import executor as E                                   # noqa: E402

OK, FAIL = [], []


def check(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail else ""))


def cfg_buffer():
    with open(os.path.join(REPO_DIR, "config.yaml")) as f:
        return float((yaml.safe_load(f) or {})["flow"]["limit_buffer_pct"])


# ════════════════════════════════════════════════════════════
# 1. 跳空场景：等价限价会挂 1.000，缓冲后应挂 1.010
# ════════════════════════════════════════════════════════════
def test_gap_buffer():
    print("\n[1] 跳空缓冲：现价1.000 涨停1.100 buffer1%")
    r = E.flow_limit_price("159628", 1.000, True, 0.01, prev_close=1.000)
    check("限价 = 1.010（不是 1.000）", r["price"] == 1.010, f"got={r['price']:.3f}")
    check("涨停 = 1.100", r["limit_up"] == 1.100, f"got={r['limit_up']:.3f}")
    check("未触发涨跌停夹", r["clamped"] is False)

    r0 = E.flow_limit_price("159628", 1.000, True, 0.0, prev_close=1.000)
    check("buffer=0 时退回 1.000（对照）", r0["price"] == 1.000, f"got={r0['price']:.3f}")


# ════════════════════════════════════════════════════════════
# 2. 低价边界：0.300 × 1.01 = 0.303，不得越过 0.330
# ════════════════════════════════════════════════════════════
def test_low_price_boundary():
    print("\n[2] 低价边界：现价0.300 涨跌停±10%")
    r = E.flow_limit_price("159628", 0.300, True, 0.01, prev_close=0.300)
    check("限价 = 0.303", r["price"] == 0.303, f"got={r['price']:.3f}")
    check("限价 ≤ 涨停0.330", r["price"] <= 0.330, f"got={r['price']:.3f}")
    check("涨停 = 0.330", r["limit_up"] == 0.330, f"got={r['limit_up']:.3f}")


# ════════════════════════════════════════════════════════════
# 3. 接近涨停：1.095×1.01=1.106 → 必须夹到 1.100
# ════════════════════════════════════════════════════════════
def test_near_limit_up():
    print("\n[3] 接近涨停：现价1.095 涨停1.100")
    r = E.flow_limit_price("159628", 1.095, True, 0.01, prev_close=1.000)
    check("缓冲价先算到 1.106", r["buffered"] == 1.106, f"got={r['buffered']:.3f}")
    check("限价被夹到涨停 1.100", r["price"] == 1.100, f"got={r['price']:.3f}")
    check("触发夹板标记", r["clamped"] is True)

    r2 = E.flow_limit_price("159628", 1.100, True, 0.01, prev_close=1.000)
    check("已涨停(1.100)仍不越界", r2["price"] == 1.100, f"got={r2['price']:.3f}")


# ════════════════════════════════════════════════════════════
# 4. 卖出方向：现价×(1-1%)，接近跌停夹到跌停价
# ════════════════════════════════════════════════════════════
def test_sell_buffer():
    print("\n[4] 卖出缓冲：现价1.234 → 1.222；接近跌停夹到 0.900")
    r = E.flow_limit_price("159928", 1.234, False, 0.01, prev_close=1.234)
    check("卖出限价 = 1.222", r["price"] == 1.222, f"got={r['price']:.3f}")
    check("跌停 = 1.111", r["limit_down"] == 1.111, f"got={r['limit_down']:.3f}")

    r2 = E.flow_limit_price("159928", 0.905, False, 0.01, prev_close=1.000)
    check("缓冲价 0.896 被夹到跌停 0.900", r2["price"] == 0.900, f"got={r2['price']:.3f}")
    check("触发跌停夹", r2["clamped"] is True)


# ════════════════════════════════════════════════════════════
# 5. 最小价位 / config / 打印格式（走 SignalExecutor 生产方法）
# ════════════════════════════════════════════════════════════
def test_tick_config_and_print():
    print("\n[5] 最小价位 / config / [FLOW] 打印")
    check("ETF 159628 tick=0.001", E.price_tick("159628") == 0.001)
    check("ETF 510300 tick=0.001", E.price_tick("510300") == 0.001)
    check("股票 600000 tick=0.01", E.price_tick("600000") == 0.01)
    check("config flow.limit_buffer_pct=0.01", cfg_buffer() == 0.01, f"got={cfg_buffer()}")

    buf = StringIO()
    with redirect_stdout(buf):
        px = E.SignalExecutor()._flow_limit_price(
            "159628", 1.234, True, {"price": 1.234, "prev_close": 1.2345})
    out = buf.getvalue().strip()
    want = "[FLOW] 159628 限价 1.234 → 1.246(缓冲1.0%) 现价 1.234 涨停 1.358"
    check("返回缓冲后限价 1.246", px == 1.246, f"got={px:.3f}")
    check("打印格式含 现价/限价/涨停/缓冲", out == want, f"got={out}")


# ════════════════════════════════════════════════════════════
# 6. 14:45 尾盘撤单窗口（只验触发条件，不真撤）
# ════════════════════════════════════════════════════════════
def test_cancel_pending_window():
    from unittest.mock import patch

    print("\n[6] --cancel-pending：14:45 窗口触发")
    ex = E.SignalExecutor()
    called = []
    with patch.object(E, "cancel_pending_at_close", lambda: called.append(1)), \
            patch.object(E.time, "strftime", return_value="14:45"):
        hit = ex._maybe_cancel_pending()
    check("14:45 触发撤单", hit is True and called == [1], f"hit={hit} called={called}")

    for hm, want in (("14:44", False), ("15:00", False), ("09:31", False)):
        with patch.object(E, "cancel_pending_at_close", lambda: called.append(1)), \
                patch.object(E.time, "strftime", return_value=hm):
            got = ex._maybe_cancel_pending()
        check(f"{hm} 不触发", got is want, f"got={got}")


# ════════════════════════════════════════════════════════════
# 7. 实盘 dry-run：真实 flow 计划 + 真实行情，只读不下单
# ════════════════════════════════════════════════════════════
def test_live():
    print("\n[6] 实盘 dry-run：flow 买入信号逐只算限价（不下单）")
    from signal_generator import build_flow_signals
    from market_data import fetch_realtime_quotes

    result = build_flow_signals()
    buy_codes = [code for code, sig in result["signals"].items()
                 if sig.get("trade_type") == "buy" and sig.get("flow_shares")]
    live = fetch_realtime_quotes(buy_codes)
    buf_pct = cfg_buffer()
    ex = E.SignalExecutor()
    rows = []
    for code in buy_codes:
        q = live.get(code) or {}
        live_px = float(q.get("price") or 0)
        r = E.flow_limit_price(code, live_px, True, buf_pct, q.get("prev_close"))
        # 走生产方法打印 [FLOW] 限价对照（传信号价作兜底，实时价优先）
        px = ex._flow_limit_price(code, float(result["signals"][code]["price"]), True, q)
        assert px == r["price"]
        rows.append((code, live_px, px, r["limit_up"], r["clamped"]))

    print(f"  {'代码':<8}{'现价':>8}{'限价':>8}{'涨停':>8}  夹板")
    for code, live_px, px, up, clamped in rows:
        print(f"  {code:<8}{live_px:>8.3f}{px:>8.3f}{up:>8.3f}  {'是' if clamped else ''}")

    ok_up = all(px <= up + 1e-9 for _, _, px, up, _ in rows if up > 0)
    ok_buf = all(px >= round(live_px * (1 + buf_pct) / 0.001) * 0.001 - 1e-9
                 for _, live_px, px, _, clamped in rows if not clamped)
    check(f"共 {len(rows)} 只买入信号", len(rows) == len(
        [s for s in result["signals"].values()
         if s.get("trade_type") == "buy" and s.get("flow_shares")]))
    check("全部 限价 ≤ 涨停", ok_up)
    check(f"全部限价 ≥ 现价×{1 + buf_pct:.2f}（未被夹时）", ok_buf)


def main():
    live = "--live" in sys.argv
    print("=" * 60)
    print("flow 限价跳空缓冲验收" + ("（含实盘 dry-run）" if live else ""))
    print("=" * 60)
    test_gap_buffer()
    test_low_price_boundary()
    test_near_limit_up()
    test_sell_buffer()
    test_tick_config_and_print()
    test_cancel_pending_window()
    if live:
        test_live()
    print("\n" + "=" * 60)
    print(f"结果: {len(OK)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
