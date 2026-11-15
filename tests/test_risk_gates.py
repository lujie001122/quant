#!/usr/bin/env python3
"""三道实盘风控闸门回归测试（全部替身，不真下单、不碰券商接口、不写真实状态文件）。

覆盖:
  1) 日亏限额闸门触发（阈值读 config.yaml risk.daily_loss_limit）
  2) 连亏熔断闸门触发（逐笔成交记账 → 连亏 N 笔 → 冷却 X 天）
  3) 单票仓位上限闸门触发 / 压缩到上限
  4) 闸门未触发时 PASS 放行（打印三道结论与数值）
  5) 卖出/减仓路径不受闸门影响（日亏已触限仍能卖出）
  6) risk_state.json 并发原子写：非法 JSON 0 次

用法: .venv/bin/python3 tests/test_risk_gates.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, REPO_DIR)

import state_center
import executor
import risk_manager


# ─── 替身 ────────────────────────────────────────────────────

class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class StubSubprocess:
    """模拟子进程：记录调用，默认返回成功。"""

    def __init__(self):
        self.calls = []

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return Completed(0, "", "")


class StubVerifier:
    """模拟 executor._verify_order_via_entrust。"""

    def __call__(self, code, direction, shares, since=None, max_tries=None, sleep_secs=3):
        return 'success', f'{code} {direction} 合同号=STUB 状态=全部成交', {'filled': True, 'status': '全部成交'}


class StubAlert:
    def __init__(self):
        self.calls = []

    def critical(self, *a, **k):
        self.calls.append(('critical', a))
        return {'pushed': False}

    warning = info = critical


class StubRecorder:
    def record(self, **kwargs):
        return None


# ─── 隔离层：不写真实 portfolio / 订单 / intent ───────────────

TMP_DIR = tempfile.mkdtemp(prefix="risk_gate_test_")
risk_manager.RISK_STATE_PATH = os.path.join(TMP_DIR, "risk_state.json")
risk_manager.RISK_ALERT_DIR = os.path.join(TMP_DIR, "alerts")

PORTFOLIO = {"positions": {}, "account": {"total_asset": 200000.0}, "_signal_state": {}}

state_center.load_portfolio = lambda: json.loads(json.dumps(PORTFOLIO))
state_center.save_portfolio = lambda pf: None
state_center.write_intent = lambda *a, **k: None
state_center.write_intent_failed = lambda *a, **k: None
state_center.write_order = lambda *a, **k: None
executor.get_alert_manager = lambda: StubAlert()
executor.get_recorder = lambda: StubRecorder()

TODAY = datetime.now().strftime("%Y-%m-%d")


def reset(positions=None, total_asset=200000.0, signal_state=None):
    """重置替身持仓 + 删除风控状态/告警文件"""
    global PORTFOLIO
    PORTFOLIO = {
        "positions": positions or {},
        "account": {"cash": total_asset, "total_asset": total_asset},
        "_signal_state": signal_state or {},
    }
    for p in (risk_manager.RISK_STATE_PATH, risk_manager.RISK_STATE_PATH + ".bak",
              risk_manager.RISK_STATE_PATH + ".bak1", risk_manager.RISK_STATE_PATH + ".bak2"):
        if os.path.exists(p):
            os.remove(p)
    if os.path.isdir(risk_manager.RISK_ALERT_DIR):
        shutil.rmtree(risk_manager.RISK_ALERT_DIR)


def seed_state(**fields):
    st = risk_manager.load_risk_state(TODAY)
    st.update(fields)
    st["date"] = fields.get("date", TODAY)
    risk_manager.save_risk_state(st)
    return st


def make_executor(shares_held=0, price=1.0):
    ex = executor.SignalExecutor.__new__(executor.SignalExecutor)
    ex._imports_loaded = True
    ex._subprocess = StubSubprocess()
    ex._fetch_realtime_quotes = lambda: {CODE: {"price": price, "prev_close": price}}
    ex._ETFS = {CODE: {"fund": 44000}}
    ex._TOTAL_FUND = 220000
    ex._parse_position_ratio = state_center.parse_position_ratio
    from core.position_manager import calc_order_amount
    ex._calc_order_amount = calc_order_amount
    ex._get_position_shares = lambda code: shares_held
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
    ex._verify_order_via_entrust = StubVerifier()
    return ex


CODE = "512800"


def buy_signal(ratio="30%", price="1.000"):
    return {"signals": {CODE: {"trade_type": "buy", "action": "买入",
                               "position_ratio": ratio, "price": price,
                               "reason": "测试建仓"}}}


def sell_signal(ratio="50%", price="1.000"):
    return {"signals": {CODE: {"trade_type": "sell", "action": "卖出",
                               "position_ratio": ratio, "price": price,
                               "reason": "测试卖出"}}}


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
        if detail:
            print("     详见上方原始输出")


def show(out, keyword=None, limit=14):
    lines = out.strip().split("\n")
    if keyword:
        lines = [ln for ln in lines if keyword in ln]
    print("\n".join("  | " + ln for ln in lines[:limit]))


def trade_calls(ex):
    return [c for c in ex._subprocess.calls if "trade.py" in " ".join(c)]


# ═════════════════════════════════════════════════════════════
# 1. 日亏限额闸门
# ═════════════════════════════════════════════════════════════

def test_daily_loss_gate():
    print("=" * 74)
    print("【闸门a】日亏限额（阈值 1.5%，当日已实现 -3600 / 总资产 200000 = -1.8%）")
    print("=" * 74)
    reset()
    orig = json.loads(json.dumps(risk_manager._cfg.get("risk", {})))
    risk_manager._cfg["risk"]["daily_loss_limit"] = 0.015
    try:
        seed_state(daily_realized_pnl=-3600.0, total_asset=200000.0)
        ex = make_executor(shares_held=0)
        out, _ = run_exec(ex, buy_signal())
        show(out, "[RISK]")
        check("打印拒绝开仓 + 亏损/限额数值",
              f"[RISK] 拒绝开仓 {CODE}：当日亏损 -1.8% 已触达日亏限额 -1.5%" in out, out)
        check("未发出任何下单命令", len(trade_calls(ex)) == 0, trade_calls(ex))
        alert_file = os.path.join(risk_manager.RISK_ALERT_DIR, f"alert_{TODAY}.log")
        alert_txt = open(alert_file, encoding="utf-8").read() if os.path.exists(alert_file) else ""
        check("alerts 日志写入 [RISK] 拦截记录", "[RISK] 拒绝开仓" in alert_txt, alert_txt)
        print(f"  │ alerts 日志: {alert_txt.strip()}")
    finally:
        risk_manager._cfg["risk"] = orig


# ═════════════════════════════════════════════════════════════
# 2. 连亏熔断闸门（成交记账 → 熔断 → 拦截）
# ═════════════════════════════════════════════════════════════

def test_circuit_breaker_gate():
    print("\n" + "=" * 74)
    print("【闸门b】连亏熔断（逐笔亏损记账 → 连亏 4 笔 → 冷却 3 天）")
    print("=" * 74)
    reset(positions={CODE: {"shares": 10000, "avg_cost": 1.0}},
          signal_state={CODE: {"entry_avg_cost": 1.0}})

    # 第1笔：走 executor 卖出成交路径，验证"成交后记账"接线（卖价 0.98 < 成本 1.0）
    ex = make_executor(shares_held=10000, price=0.98)
    out, _ = run_exec(ex, sell_signal(ratio="10%", price="0.980"))
    show(out, "[RISK] 记账")
    st = risk_manager.load_risk_state(TODAY)
    check("成交后自动记账（逐笔写入 risk_state.trades）", len(st["trades"]) == 1, st)
    check("第1笔亏损已计入当日已实现", st["daily_realized_pnl"] < 0, st)

    # 再补 3 笔亏损，凑够 consecutive_loss_count=4
    for i in range(3):
        risk_manager.record_trade_result(CODE, "卖出", 10000, 0.980, today_str=TODAY,
                                         quotes={CODE: {"price": 0.98, "prev_close": 0.98}},
                                         total_asset=200000.0)
    st = risk_manager.load_risk_state(TODAY)
    info = risk_manager.get_consecutive_loss_info(TODAY)
    print(f"  │ 状态: 连亏 {info['consecutive_losses']} 笔, 熔断至 {info['circuit_breaker_until']}")
    check("连亏计数 = 4（阈值来自 config.yaml）", info["consecutive_losses"] == 4, info)
    check("熔断截止日 = 今天 + 3 天",
          info["circuit_breaker_until"] == (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"),
          info)

    # 熔断期内买入 → 拦截
    ex = make_executor(shares_held=0)
    out, _ = run_exec(ex, buy_signal())
    show(out, "[RISK]")
    check("熔断期内拒绝开仓（打印连亏笔数 + 冷却日期）",
          "[RISK] 熔断中（连亏 4 笔，冷却至 " in out and "拒绝开仓" in out, out)
    check("熔断期内未发出下单命令", len(trade_calls(ex)) == 0, trade_calls(ex))

    # reset_circuit_breaker 解除后可正常开仓
    risk_manager.reset_circuit_breaker(TODAY)
    ex = make_executor(shares_held=0)
    out, _ = run_exec(ex, buy_signal())
    check("reset_circuit_breaker() 后恢复放行", "[RISK] PASS 连亏熔断" in out, out)


# ═════════════════════════════════════════════════════════════
# 3. 单票仓位上限闸门
# ═════════════════════════════════════════════════════════════

def test_position_cap_gate():
    print("\n" + "=" * 74)
    print("【闸门c】单票仓位上限（现持 110000 股@1.0=55%，再买 5300 股 → 目标 58%）")
    print("=" * 74)
    reset(positions={CODE: {"shares": 110000, "avg_cost": 1.0, "current_price": 1.0,
                            "market_value": 110000.0}})
    ex = make_executor(shares_held=110000)
    out, _ = run_exec(ex, buy_signal(ratio="10%"))
    show(out, "[RISK]")
    check("打印 单票占比 58% > 上限 50%",
          f"[RISK] 拒绝开仓 {CODE}：单票占比 58% > 上限 50%" in out, out)
    check("超限时未发出下单命令", len(trade_calls(ex)) == 0, trade_calls(ex))

    print("\n" + "-" * 74)
    print("【闸门c-压缩】现持 90000 股@1.0=45%，买入 26666 股 → 目标 58%，压缩到上限")
    print("-" * 74)
    reset(positions={CODE: {"shares": 90000, "avg_cost": 1.0, "current_price": 1.0,
                            "market_value": 90000.0}})
    ex = make_executor(shares_held=90000)
    out, _ = run_exec(ex, buy_signal(ratio="100%"))
    show(out, "[RISK]")
    calls = trade_calls(ex)
    check("打印压缩股数", "[RISK] 压缩开仓" in out and "压缩股数" in out, out)
    check("压缩后仍下单（股数=10000，占比压回 50%）",
          len(calls) == 1 and "10000" in calls[0], calls)


# ═════════════════════════════════════════════════════════════
# 4. 闸门未触发 → PASS 放行
# ═════════════════════════════════════════════════════════════

def test_pass_through():
    print("\n" + "=" * 74)
    print("【PASS】空仓 + 无亏损 + 无连亏 → 三道闸门全部放行")
    print("=" * 74)
    reset()
    ex = make_executor(shares_held=0)
    out, _ = run_exec(ex, buy_signal())
    show(out, "[RISK]")
    check("日亏限额 PASS（含数值）", "[RISK] PASS 日亏限额" in out, out)
    check("连亏熔断 PASS（含数值）", "[RISK] PASS 连亏熔断" in out, out)
    check("单票上限 PASS（含数值）", "[RISK] PASS 单票上限" in out, out)
    check("正常下单 1 笔", len(trade_calls(ex)) == 1, trade_calls(ex))


# ═════════════════════════════════════════════════════════════
# 5. 卖出路径不受闸门限制
# ═════════════════════════════════════════════════════════════

def test_sell_exempt():
    print("\n" + "=" * 74)
    print("【免检】日亏已触限（-1.8%）+ 熔断中 → 卖出仍然执行")
    print("=" * 74)
    reset(positions={CODE: {"shares": 10000, "avg_cost": 1.0}},
          signal_state={CODE: {"entry_avg_cost": 1.0}})
    seed_state(daily_realized_pnl=-3600.0,
               consecutive_losses=4, circuit_breaker_until="2099-01-01")
    ex = make_executor(shares_held=10000)
    out, _ = run_exec(ex, sell_signal(ratio="50%", price="0.990"))
    show(out, "[RISK]")
    calls = trade_calls(ex)
    check("打印免检说明（卖出/减仓/止损/做T平仓）", "[RISK] 免检" in out, out)
    check("日亏触限 + 熔断中，卖出命令照常发出", len(calls) == 1 and "sell" in calls[0], calls)
    check("卖出未出现任何拒绝开仓拦截", "拒绝开仓" not in out, out)


# ═════════════════════════════════════════════════════════════
# 6. 并发原子写
# ═════════════════════════════════════════════════════════════

def _writer_process(path, idx, rounds, alert_dir):
    import io as _io
    from contextlib import redirect_stdout as _rstdout
    import risk_manager as rm
    rm.RISK_STATE_PATH = path
    rm.RISK_ALERT_DIR = alert_dir
    # 并发时 atomic_writer 备份轮转偶发竞争告警（主文件原子性不受影响），子进程输出静音
    _sink = _io.StringIO()
    for i in range(rounds):
        payload = {
            "date": TODAY,
            "daily_realized_pnl": round(-(idx * 100 + i), 2),
            "daily_pnl": round(-(idx * 100 + i), 2),
            "consecutive_losses": idx,
            "trades": [{"code": f"{idx}-{i}", "pnl": -1.0}],
        }
        with _rstdout(_sink):
            rm.save_risk_state(payload)


def _reader_process(path, rounds, q):
    import json as _json
    bad = 0
    reads = 0
    for _ in range(rounds):
        try:
            with open(path, "r", encoding="utf-8") as f:
                _json.load(f)
            reads += 1
        except FileNotFoundError:
            pass
        except _json.JSONDecodeError:
            bad += 1
        time.sleep(0.001)
    q.put((bad, reads))


def test_atomic_concurrent_writes():
    print("\n" + "=" * 74)
    print("【原子写】4 进程并发写 risk_state.json + 1 进程持续读，统计非法 JSON")
    print("=" * 74)
    import multiprocessing as mp
    reset()
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    writers = [ctx.Process(target=_writer_process,
                           args=(risk_manager.RISK_STATE_PATH, i, 25, risk_manager.RISK_ALERT_DIR))
               for i in range(4)]
    reader = ctx.Process(target=_reader_process,
                         args=(risk_manager.RISK_STATE_PATH, 400, q))
    reader.start()
    for w in writers:
        w.start()
    for w in writers:
        w.join(60)
    reader.join(60)
    bad, reads = q.get(timeout=5) if not q.empty() else (-1, 0)
    print(f"  │ 并发读 {reads} 次，非法 JSON {bad} 次；进程退出码: {[w.exitcode for w in writers]}")
    ok_main, main_size = True, 0
    try:
        with open(risk_manager.RISK_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        main_size = len(json.dumps(data))
    except Exception as ex:
        ok_main = False
        print(f"  │ 主文件解析失败: {ex}")
    print(f"  │ 主文件 JSON 合法={ok_main}, 字节数={os.path.getsize(risk_manager.RISK_STATE_PATH)}, "
          f"内容长度={main_size}")
    check("并发写期间读到非法 JSON 次数 = 0", bad == 0, f"bad={bad}")
    check("最终主文件为合法 JSON", ok_main)
    check("所有写进程正常退出", all(w.exitcode == 0 for w in writers),
          [w.exitcode for w in writers])


# ═════════════════════════════════════════════════════════════

def main():
    test_daily_loss_gate()
    test_circuit_breaker_gate()
    test_position_cap_gate()
    test_pass_through()
    test_sell_exempt()
    test_atomic_concurrent_writes()

    print("\n" + "=" * 74)
    print(f"结果: PASS {PASS} / FAIL {FAIL}")
    print("=" * 74)
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
