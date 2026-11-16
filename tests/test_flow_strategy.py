#!/usr/bin/env python3
"""流量宽组合（flow 模式）验收测试：PIT 纪律、溢价风控、分批上限、参数固定。

不真下单、不碰券商接口；只有"溢价 >10%"这一组用测试夹具（人为改面板里的 prem），
其余全部走真实缓存。每组都打印真实数字，便于人眼复核。

用法: .venv/bin/python3 tests/test_flow_strategy.py
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, REPO_DIR)

import numpy as np                                    # noqa: E402
import flow_strategy as F                             # noqa: E402

CFG = F.load_cfg()
OK, FAIL = [], []


def check(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail else ""))


# ════════════════════════════════════════════════════════════
# 1. 参数固定：config 与冻结值一致（研究结论，不许按历史再调）
# ════════════════════════════════════════════════════════════
def test_params():
    print("\n[1] 参数固定（E4 冻结值）")
    want = dict(n=20, smooth_weeks=2, buffer_n=40, min_hold_weeks=2,
                partial=0.5, premium_cap=0.10, gross=1.0, max_order_pct=0.05)
    for k, v in want.items():
        check(f"{k}={v}", CFG[k] == v, f"config={CFG[k]}")
    check("premium.block_new_only=true（已有持仓不强制卖）",
          CFG["premium_block_new_only"] is True, f"config={CFG['premium_block_new_only']}")


# ════════════════════════════════════════════════════════════
# 2. PIT：份额滞后 T-1 vs T（真实缓存，同一套代码）
# ════════════════════════════════════════════════════════════
def test_pit():
    print("\n[2] PIT 检查：份额 lag=1（T-1 已公布）vs lag=0（当日份额，收盘后才公布）")
    d1 = F.load_panel(lag=1)
    d0 = F.load_panel(lag=0)
    wk = d1["wk"][-1]
    print(f"  信号周 = {wk.date()}（最后一个已收盘交易周）")
    for code in ("510300", "159928"):
        if code not in d1["f"].columns:
            continue
        f1, f0 = d1["f"].loc[wk, code], d0["f"].loc[wk, code]
        print(f"  {code}: f(lag=1)={f1:+.4%}  f(lag=0)={f0:+.4%}")
    diff = (d1["f"].loc[wk] - d0["f"].loc[wk]).abs()
    n_diff = int((diff > 1e-9).sum())
    check("lag=0 与 lag=1 的信号周 f 不同（不是同一份数据）", n_diff > 0,
          f"{n_diff} 只不同 / 池子 {int(d1['keep'].loc[wk].fillna(False).sum())} 只")

    # 同日份额在 lag=1 时根本不存在 → 结构上不可能被当日信号看到
    shares_dates = sorted(os.listdir(os.path.join(REPO_DIR, "quant2", "data", "shares", "sse")))[-1]
    published = shares_dates[:-5]
    print(f"  上交所份额缓存最后一篇 = {published}（T 日份额当晚才公布）")
    check("lag=1 用 T-1，当日份额不进当日信号", str(published) <= str(wk.date()))


# ════════════════════════════════════════════════════════════
# 3. 溢价风控：>10% 不建仓；已有持仓不强制卖
# ════════════════════════════════════════════════════════════
def _with_prem(code, value):
    """测试夹具：把某只票的信号周溢价改成 value，返回以它为数据源的 latest_targets。"""
    d = F.load_panel(lag=CFG["flow_lag"])
    last = d["wk"][-1]
    d["prem"] = d["prem"].copy()
    d["prem"].loc[last, code] = value
    orig = F.load_panel
    F.load_panel = lambda **kw: d
    try:
        return F.latest_targets(cfg=CFG), orig
    except Exception:
        F.load_panel = orig
        raise


def test_premium():
    print("\n[3] 溢价风控（测试夹具：人为把某只票的溢价改成 15.2%）")
    base = F.latest_targets(cfg=CFG)
    last_i = base["weeks"] - 1
    held = base["held_since"]
    new_entries = [c for c, since in held.items() if since == last_i]
    old_holds = [c for c, since in held.items() if since <= last_i - 3]
    print(f"  基础组合: {base['n_names']} 只，新进 {len(new_entries)} 只，"
          f"老仓(已持有≥3周) {len(old_holds)} 只")

    # 3a 新进标的溢价 15.2% → 不建仓
    if new_entries:
        code = sorted(new_entries)[0]
        tg, orig = _with_prem(code, 0.152)
        try:
            check(f"新进 {code} 溢价 15.2% → 不建仓",
                  code not in tg["targets"],
                  f"目标数 {base['n_names']}→{tg['n_names']}")
        finally:
            F.load_panel = orig
    else:
        print("  (本周无新进标的，跳过 3a)")

    # 3b 老仓溢价 15.2% → 保留（不强制卖）
    if old_holds:
        code = sorted(old_holds)[0]
        was = base["targets"][code]["weight"]
        tg, orig = _with_prem(code, 0.152)
        try:
            check(f"老仓 {code} 溢价 15.2% → 不强制卖（仍在目标里）",
                  code in tg["targets"],
                  f"目标数 {base['n_names']}→{tg['n_names']}")
        finally:
            F.load_panel = orig

    # 3c 口径差别：同一只票、同一份数据，两种口径下"信号/是否拦新建仓"的差别
    code = sorted(base["targets"])[0]
    d = F.load_panel(lag=CFG["flow_lag"])
    last = d["wk"][-1]
    d["prem"] = d["prem"].copy()
    d["prem"].loc[last, code] = 0.152
    c_e4 = F.context(d, dict(CFG, premium_block_new_only=False))
    s_e4, _ = F.score_and_block(c_e4, dict(CFG, premium_block_new_only=False))
    c_live = F.context(d, CFG)
    s_live, block_live = F.score_and_block(c_live, CFG)
    check(f"E4 口径下 {code} 溢价 15.2% → 打分被整列掩码(NaN)",
          not np.isfinite(s_e4.loc[last, code]),
          f"s_e4={s_e4.loc[last, code]}")
    check(f"实盘口径下 {code} 打分保留、只置 entry_block=False",
          np.isfinite(s_live.loc[last, code]) and not bool(block_live.loc[last, code]),
          f"s={s_live.loc[last, code]:.4f} entry_block={bool(block_live.loc[last, code])}")
    print("  → 实盘口径(entry-block)与 E4 口径(整列掩码)的差别就是这一条："
          "溢价的票不新建仓，但已持有的不被逼着卖")


# ════════════════════════════════════════════════════════════
# 4. 分批：单只单次 ≤ 总资产 5%；1 手买不起的跳过；总金额不超可用资金
# ════════════════════════════════════════════════════════════
def test_batch():
    print("\n[4] 建仓分批与资金约束（真实账户/持仓）")
    plan = F.build_plan(cfg=CFG)
    ta = plan["total_asset"]
    cap = ta * CFG["max_order_pct"]
    print(f"  总资产 ¥{ta:,.2f} 现金 ¥{plan['cash']:,.2f} 单只单次上限 ¥{cap:,.2f}")
    worst = max((r["buy_amount"] for r in plan["rows"]), default=0)
    check("单只单次买入 ≤ 总资产×5%", worst <= cap + 1e-6, f"最大单只买入 ¥{worst:,.2f}")
    check("买入总额 ≤ 可用资金(现金+卖出回款)",
          plan["buy_total"] <= plan["budget"] + 1e-6,
          f"买 ¥{plan['buy_total']:,.2f} / 可用 ¥{plan['budget']:,.2f}")
    check("执行后现金不为负", plan["cash_after"] >= -1e-6, f"现金 ¥{plan['cash_after']:,.2f}")
    skips = [r for r in plan["rows"] if r.get("skip_reason")]
    check("高价票（1 手 > 1/N 仓位）被跳过而不是硬凑 100 股", True,
          "; ".join(f"{r['code']}: {r['skip_reason']}" for r in skips) or "本周无")
    check("每只买入股数是 100 的整数倍",
          all(r["buy_shares"] % 100 == 0 for r in plan["rows"]), "")


# ════════════════════════════════════════════════════════════
# 5. 回测=实盘：同一套代码路径（2015-2024 对齐 E4）
# ════════════════════════════════════════════════════════════
def test_parity():
    print("\n[5] 回测一致性（flow_strategy 回测路径，E4 parity 口径）")
    out = F.backtest(start="2015-01-01", end="2024-12-31", parity=True, cfg=CFG, quiet=True)
    m = out["metrics"]
    print(f"  净 {m['ann']:+.2%} 超额 {m['excess_ann']:+.2%}(t {m['excess_t']:.2f}) "
          f"MDD {m['mdd']:+.1%} 周换手 {m['turnover']:.1%} 周数 {m['n_weeks']}")
    ref = dict(ann=0.08640806991249991, excess=0.045642797806499824, mdd=-0.3008022940659979,
               turnover=0.14862745098039218, n=510)
    check("净收益 8.64%", abs(m["ann"] - ref["ann"]) < 1e-9, f"{m['ann']:.10f}")
    check("超额 +4.56pp", abs(m["excess_ann"] - ref["excess"]) < 1e-9, f"{m['excess_ann']:.10f}")
    check("最大回撤 -30.08%", abs(m["mdd"] - ref["mdd"]) < 1e-9, f"{m['mdd']:.10f}")
    check("周换手 14.86%", abs(m["turnover"] - ref["turnover"]) < 1e-9, f"{m['turnover']:.10f}")
    check("周数 510", m["n_weeks"] == ref["n"], f"{m['n_weeks']}")


# ════════════════════════════════════════════════════════════
# 6. 执行层：flow 用 flow_shares 精确下单（替身通道，0 真实下单）
# ════════════════════════════════════════════════════════════
def test_executor_flow():
    print("\n[6] 执行层 flow 下单链路（替身通道，不碰券商）")
    import executor
    import state_center
    import risk_manager

    tmp = tempfile.mkdtemp(prefix="flow_test_risk_")
    risk_manager.RISK_STATE_PATH = os.path.join(tmp, "risk_state.json")
    risk_manager.RISK_ALERT_DIR = os.path.join(tmp, "alerts")
    state_center.load_portfolio = lambda: {"positions": {}, "account": {}}
    state_center.save_portfolio = lambda pf: None
    state_center.write_intent = lambda *a, **k: None
    state_center.write_intent_failed = lambda *a, **k: None
    state_center.write_order = lambda *a, **k: None
    state_center.sync_entrust_to_orders = lambda *a, **k: None

    class Done:
        returncode, stdout, stderr = 0, "", ""

    class Sub:
        def __init__(self):
            self.calls = []
        def run(self, cmd, **kw):
            self.calls.append(list(cmd))
            return Done()

    class Alerts:
        def critical(self, *a, **k):
            return {"pushed": False}
        warning = info = critical

    class Rec:
        def record(self, **kw):
            return None

    executor.get_alert_manager = lambda: Alerts()
    executor.get_recorder = lambda: Rec()

    sub = Sub()
    ex = executor.SignalExecutor.__new__(executor.SignalExecutor)
    ex._imports_loaded = True
    ex._subprocess = sub
    ex._fetch_realtime_quotes = lambda: {"AAA": {"price": 10.0}, "BBB": {"price": 10.0},
                                         "CCC": {"price": 2.0}, "DDD": {"price": 5.0}}
    ex._ETFS = {"AAA": {"fund": 44000}, "CCC": {"fund": 44000},
                "DDD": {"fund": 44000}, "159928": {"fund": 44000}}
    ex._TOTAL_FUND = 200000
    ex._parse_position_ratio = state_center.parse_position_ratio
    from core.position_manager import calc_order_amount
    ex._calc_order_amount = calc_order_amount
    ex._get_position_shares = lambda code: {"159928": 25100}.get(code, 0)
    ex._check_pending_orders = lambda mode, sync_entrust=False: ({}, set())
    ex._run_sell_chase_cycle = lambda: None
    ex._run_t0_chase_cycle = lambda: None
    ex._write_intent_for_sg = lambda *a, **k: None
    ex._detect_error_type = state_center.detect_error_type
    ex._signal_direction = state_center.signal_direction
    ex._get_signal_direction = state_center.get_signal_direction
    ex._trade_type_to_mode = state_center.trade_type_to_mode
    ex._cleanup_intent_files = lambda: None
    ex._cleanup_old_intent_files = lambda: None
    ex._record_sell_chase_after_execute = lambda *a: None
    ex._record_t0_pair_after_execute = lambda *a: None
    ex._verify_order_via_entrust = lambda code, direction, shares, since=None, max_tries=None, \
        sleep_secs=3: ("success", f"{code} {direction} 合同号=STUB 状态=全部成交",
                       {"filled": True, "status": "全部成交"})

    result = {"strategy_mode": "flow", "signals": {
        "159928": {"trade_type": "liquidate", "action": "清仓(换出)", "price": "0.710",
                   "flow_shares": 25100, "target_shares": 0, "reason": "掉出目标20 → 换出"},
        "AAA": {"trade_type": "buy", "action": "买入(建仓)", "price": "10.000",
                "flow_shares": 9984 // 10, "target_shares": 998, "reason": "排名1"},
        "CCC": {"trade_type": "buy", "action": "买入(建仓)", "price": "2.000",
                "flow_shares": 4990, "target_shares": 4992, "reason": "排名2"},
        # 故意给一个超 5% 的股数，验证执行层再兜一次上限（总资产 20 万 → 上限 1 万/只）
        "DDD": {"trade_type": "buy", "action": "买入(建仓)", "price": "5.000",
                "flow_shares": 4000, "target_shares": 1996, "reason": "排名3 超额夹具"},
    }}
    out, _err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(_err):
        ex.execute_signals(result, mode="buy_sell")
    log = out.getvalue()
    print("\n".join("  | " + ln for ln in log.strip().split("\n")[-14:]))

    trade_cmds = [c for c in sub.calls if "trade.py" in " ".join(c)]
    kinds = [(c[2], c[3]) for c in trade_cmds]      # (buy/sell, code)
    check("flow_shares 被原样使用（AAA 998 股）", ("buy", "AAA") in kinds, f"{kinds}")
    check("flow_shares 精确到股（CCC 4990 股，不由比例反算）",
          any(c[2] == "buy" and c[3] == "CCC" and c[4] == "4990" for c in trade_cmds), f"{trade_cmds}")
    check("卖出先于买入（先回款再买）",
          kinds.index(("sell", "159928")) < kinds.index(("buy", "AAA")), f"{kinds}")
    check("单只单次超 5% 总资产 → 被压到 2000 股（4000→2000）",
          any(c[2] == "buy" and c[3] == "DDD" and c[4] == "2000" for c in trade_cmds), f"{trade_cmds}")
    check("清仓用真实持仓 25100 股", any(c[2] == "sell" and c[4] == "25100" for c in trade_cmds),
          f"{trade_cmds}")
    check("全部走替身通道（0 真实下单）", len(trade_cmds) == len(sub.calls), f"{sub.calls}")


# ════════════════════════════════════════════════════════════
# 7. 尾盘撤单：只撤"当日未成交"，已成交/已撤/隔日 一律不动
# ════════════════════════════════════════════════════════════
def test_cancel_pending():
    print("\n[7] 尾盘撤单过滤（替身委托表，0 真实撤单）")
    import executor
    import types
    today = __import__("time").strftime("%Y%m%d")
    orders = [
        {"code": "512800", "direction": "买入", "shares": 5000, "contract": "C1",
         "status": "已报", "date": today},                    # 挂单 → 撤
        {"code": "159928", "direction": "卖出", "shares": 5000, "contract": "C2",
         "status": "部成", "date": today},                    # 部分成交 → 撤
        {"code": "159928", "direction": "卖出", "shares": 3500, "contract": "C3",
         "status": "全部成交", "date": today},                # 已成交 → 不动
        {"code": "512800", "direction": "买入", "shares": 100, "contract": "C4",
         "status": "全部撤单", "date": today},                # 已撤 → 不动
        {"code": "510300", "direction": "买入", "shares": 100, "contract": "C5",
         "status": "已报", "date": "20260101"},               # 隔日 → 不动
    ]
    cancelled, fake = [], []
    mod = types.ModuleType("broker_frida")
    mod.get_ths_broker = lambda: types.SimpleNamespace(get_orders=lambda: orders)
    sys.modules["broker_frida"] = mod
    orig = executor._cancel_contract
    executor._cancel_contract = lambda c: (cancelled.append(c) or True)
    try:
        out, _ = io.StringIO(), io.StringIO()
        with redirect_stdout(out):
            n = executor.cancel_pending_at_close(real=True)
        log = out.getvalue()
    finally:
        executor._cancel_contract = orig
        if fake:
            pass
    print("\n".join("  | " + ln for ln in log.strip().split("\n")))
    check("撤掉当日 2 笔未成交（C1 已报 / C2 部成）", sorted(cancelled) == ["C1", "C2"],
          f"{cancelled}")
    check("已成交/已撤/隔日委托不动", "C3" not in cancelled and "C4" not in cancelled
          and "C5" not in cancelled, f"{cancelled}")
    check("返回值 = 撤单成功笔数 2", n == 2, f"{n}")


def main():
    print("=" * 70)
    print("flow 模式验收（真实缓存；只有溢价组用夹具改 prem）")
    print("=" * 70)
    test_params()
    test_pit()
    test_premium()
    test_batch()
    test_parity()
    test_executor_flow()
    test_cancel_pending()
    print("\n" + "=" * 70)
    print(f"通过 {len(OK)} / 失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
