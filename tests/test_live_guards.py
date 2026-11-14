#!/usr/bin/env python3
"""第三批实盘隐患回归测试（全部用替身，不真下单、不碰券商接口）。

覆盖：
  1) reduce_half 不再被 buy_sell 模式白名单丢弃 → 一路走到执行层
  2) 断连重试：委托表命中就不重跑（哪怕命令输出含"同花顺"）；真断连才重跑
  3) 去掉 os.system("open -a 同花顺") / cliclick 后，100 股远价限价单仍能成功

用法: .venv/bin/python3 tests/test_live_guards.py
"""
import io
import os
import sys
from contextlib import redirect_stdout, redirect_stderr

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, REPO_DIR)

import state_center
import executor
import trade


# ─── 替身 ────────────────────────────────────────────────────

class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class StubSubprocess:
    """模拟子进程：按顺序吐出预设结果，并记录每次调用。"""
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if self.results:
            return self.results.pop(0)
        return Completed()


class StubVerifier:
    """模拟 executor._verify_order_via_entrust 的返回。"""
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = 0

    def __call__(self, code, direction, shares, since=None, max_tries=None, sleep_secs=3):
        self.calls += 1
        r = self.results.pop(0) if self.results else 'success'
        if r == 'success':
            return 'success', f'{code} {direction} 合同号=STUB 状态=全部成交', {'filled': True, 'status': '全部成交'}
        if r == 'unknown':
            return 'unknown', 'stub: 委托表读不到', None
        return 'failed', 'stub: 委托表无此笔', None


class StubAlert:
    def __init__(self):
        self.calls = []

    def critical(self, *a, **k):
        self.calls.append(('critical', a))
        return {'pushed': False, 'should_push': True}

    warning = info = critical


class StubRecorder:
    def record(self, **kwargs):
        return None


# ─── 隔离层：禁止测试写真实 portfolio/订单/intent ────────────
state_center.load_portfolio = lambda: {"positions": {}, "account": {}}
state_center.save_portfolio = lambda pf: None
state_center.write_intent = lambda *a, **k: None
state_center.write_intent_failed = lambda *a, **k: None
state_center.write_order = lambda *a, **k: None
state_center.sync_entrust_to_orders = lambda *a, **k: None
executor.get_alert_manager = lambda: StubAlert()
executor.get_recorder = lambda: StubRecorder()


def make_executor(subproc, verifier, shares=10000, price=2.0):
    ex = executor.SignalExecutor.__new__(executor.SignalExecutor)
    ex._imports_loaded = True
    ex._subprocess = subproc
    ex._fetch_realtime_quotes = lambda: {"159915": {"price": price, "prev_close": price}}
    ex._ETFS = {"159915": {"fund": 44000}}
    ex._TOTAL_FUND = 220000
    ex._parse_position_ratio = state_center.parse_position_ratio
    ex._get_position_shares = lambda code: shares
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
    ex._verify_order_via_entrust = verifier
    return ex


def run_exec(ex, result, mode="buy_sell"):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        ex.execute_signals(result, mode=mode)
    return out.getvalue(), err.getvalue()


PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


# ─── 用例1: reduce_half 不再被白名单丢弃 ─────────────────────

def test_reduce_half_reaches_execution():
    print("=" * 70)
    print("【1】reduce_half 进到执行层（buy_sell 模式）")
    print("=" * 70)
    check("reduce_half 在 BUY_SELL_TYPES 白名单",
          "reduce_half" in executor.BUY_SELL_TYPES)
    check("buy_sell 模式放行 reduce_half",
          executor.signal_passes_mode_filter("reduce_half", "buy_sell") is True)

    subproc = StubSubprocess([Completed(21, "", "##ERRCODE## ORDER_NOT_CONFIRMED stub")])
    ex = make_executor(subproc, StubVerifier())
    result = {"signals": {"159915": {
        "trade_type": "reduce_half", "action": "减仓",
        "position_ratio": "减50%总仓", "price": "2.000",
        "reason": "网格整体止损-8%减半仓",
    }}}
    out, _ = run_exec(ex, result)
    print("\n".join("  | " + ln for ln in out.strip().split("\n")[:12]))
    check("执行层收到减半仓信号（1/1 减半仓 159915）",
          "[1/1] 减半仓 159915 5000股 @2.000" in out, out)
    check("真的发出了卖出命令（未被白名单丢弃）",
          len(subproc.calls) == 1 and "sell" in subproc.calls[0] and "159915" in subproc.calls[0],
          f"calls={subproc.calls}")
    check("减仓比例按 50% 计算（10000*50%=5000）", "5000" in subproc.calls[0], subproc.calls)


# ─── 用例2: 断连重试不重复下单 ───────────────────────────────

def test_disconnect_no_duplicate():
    print("\n" + "=" * 70)
    print("【2a】命令输出含'同花顺'但实际已成交 → 不重跑（防重复下单）")
    print("=" * 70)
    # 结构化退出码 20=断连；stdout 里全是"同花顺"自然语言（旧实现会据此误判断连）
    first = Completed(20, "⚠️ EvolvingSim 返回 None，重启同花顺后重试一次\n"
                          "⏳ 旧通道等待同花顺 API 就绪 15s...\n"
                          "📊 sync: 同花顺委托表已刷新", "")
    subproc = StubSubprocess([first, Completed(0), Completed(0)])
    verifier = StubVerifier(['success', 'success'])  # 委托表里已有该笔
    ex = make_executor(subproc, verifier)
    out, _ = run_exec(ex, {"signals": {"159915": {
        "trade_type": "buy", "action": "买入", "position_ratio": "30%", "price": "2.000"}}})

    trade_calls = [c for c in subproc.calls if "trade.py" in " ".join(c)]
    restart_calls = [c for c in subproc.calls if "restart_ths.py" in " ".join(c)]
    print("\n".join("  | " + ln for ln in out.strip().split("\n")[-12:]))
    check("委托表命中 → 打印'不重跑命令'", "不重跑命令" in out, out)
    check("下单命令只跑 1 次（未重复下单）", len(trade_calls) == 1, f"trade_calls={trade_calls}")
    check("没有重启同花顺", len(restart_calls) == 0, f"restart_calls={restart_calls}")

    print("\n" + "=" * 70)
    print("【2b】真断连（委托表确认无此笔）→ 重启后重跑一次")
    print("=" * 70)
    subproc = StubSubprocess([
        Completed(20, "旧通道两次无响应(断连)", "##ERRCODE## THS_DISCONNECT"),
        Completed(0),                       # restart_ths.py
        Completed(0, "✅ 重试下单成功"),      # 重跑 cmd
    ])
    verifier = StubVerifier(['failed', 'success'])
    ex = make_executor(subproc, verifier)
    out, _ = run_exec(ex, {"signals": {"159915": {
        "trade_type": "buy", "action": "买入", "position_ratio": "30%", "price": "2.000"}}})

    trade_calls = [c for c in subproc.calls if "trade.py" in " ".join(c)]
    restart_calls = [c for c in subproc.calls if "restart_ths.py" in " ".join(c)]
    print("\n".join("  | " + ln for ln in out.strip().split("\n")[-12:]))
    check("委托表无此笔 → 重启并重跑", "重跑一次" in out, out)
    check("下单命令共 2 次（首跑+重跑）", len(trade_calls) == 2, f"trade_calls={trade_calls}")
    check("重启同花顺 1 次", len(restart_calls) == 1, f"restart_calls={restart_calls}")

    print("\n" + "=" * 70)
    print("【2c】断连判定只看结构化错误码/退出码，不看中文")
    print("=" * 70)
    det = state_center.detect_error_type
    check("含'同花顺'但退出码1 → 不再判断连", det("重启同花顺后重试", "", 1)[0] == "other",
          det("重启同花顺后重试", "", 1))
    check("含'同花顺'且退出码0 → 正常成功", det("同花顺委托表已刷新", "", 0)[0] is None,
          det("同花顺委托表已刷新", "", 0))
    check("退出码20 → 断连", det("", "", 20)[0] == "tonghuashun_disconnect",
          det("", "", 20))
    check("##ERRCODE## THS_DISCONNECT → 断连",
          det("", "##ERRCODE## THS_DISCONNECT old", 1)[0] == "tonghuashun_disconnect")
    check("##ERRCODE## PAIR_LEG_FAILED → 配对腿失败(不重试)",
          det("主腿已成交，同花顺配对失败", "##ERRCODE## PAIR_LEG_FAILED", 22)[0] == "pair_leg_failed")


# ─── 用例3: 去掉 open/cliclick 后 100 股限价单仍成功 ─────────

def test_order_without_focus_stealing():
    print("\n" + "=" * 70)
    print("【3】无 os.system('open -a 同花顺')/cliclick，100股远价限价单仍成功")
    print("=" * 70)

    syscalls = []
    real_system = os.system
    os.system = lambda cmd: syscalls.append(cmd) or 0

    class StubBroker:
        placed = []

        def place(self, method, code, shares, price):
            StubBroker.placed.append((method, code, shares, price))
            return {"contract": "STUB20261114001", "status": "未成交", "via": "stub-frida"}

    import broker_frida
    real_broker = broker_frida.ThsBroker
    broker_frida.ThsBroker = StubBroker
    real_verify = trade._verify_order_in_entrust
    trade._verify_order_in_entrust = lambda *a, **k: (True, "STUB20261114001", "stub 核验通过")
    trade._CONF.setdefault("trade", {})["min_shares"] = 100

    try:
        ok = trade._unified_trade("buy", "159915", 100, 1.800)  # 远低于盘口 2.000 的合法限价
    finally:
        os.system = real_system
        broker_frida.ThsBroker = real_broker
        trade._verify_order_in_entrust = real_verify
        trade._CONF["trade"]["min_shares"] = 5000

    check("下单成功", ok is True, f"ok={ok}")
    check("走的是 frida 接口", StubBroker.placed and StubBroker.placed[0][0] == "buy",
          StubBroker.placed)
    check("限价单价格/数量原样下出（100股 @1.800）",
          StubBroker.placed and StubBroker.placed[0][2:] == (100, 1.8), StubBroker.placed)
    check("全程没有 os.system（open -a 同花顺 / cliclick）", syscalls == [], syscalls)


if __name__ == "__main__":
    test_reduce_half_reaches_execution()
    test_disconnect_no_duplicate()
    test_order_without_focus_stealing()

    print("\n" + "=" * 70)
    print(f"结果: {PASS} 通过, {FAIL} 失败")
    print("=" * 70)
    sys.exit(0 if FAIL == 0 else 1)
