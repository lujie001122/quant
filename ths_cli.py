#!/usr/bin/env python3
"""
ths_cli.py — 同花顺 CLI，支持两个后端：
  --engine apple (默认)  全部走 EvolvingSim
  --engine frida         走 ths_bridge.py + ths_agent.js
"""
import argparse
import json
import subprocess
import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── JSON 输出 ─────────────────────────────────────────────────────────────────

def _json_out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ── 窗口恢复 ─────────────────────────────────────────────────────────────────

def _ensure_window():
    """同花顺窗口数=0 时，激活进程并 Cmd+N 新建窗口。"""
    script = '''
tell application "System Events"
    set theProcs to processes whose name is "同花顺"
    if theProcs is {} then return "no proc"
    set theProc to item 1 of theProcs
    if (count of windows of theProc) = 0 then
        set frontmost of theProc to true
        delay 0.5
        keystroke "n" using command down
        delay 2
    end if
    return count of windows of theProc
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           timeout=10, capture_output=True, text=True)
        return r.stdout.strip()
    except Exception as e:
        return f"ERR:{e}"


def _with_window_retry(fn):
    """执行 fn()，若窗口数=0 先恢复窗口再重试一次。"""
    result = fn()
    if result is None or (isinstance(result, dict) and not result.get("status")):
        _ensure_window()
        result = fn()
    return result


# ── EvolvingSim 实例 ─────────────────────────────────────────────────────────

def _get_evolving():
    sys.path.insert(0, os.path.join(SCRIPT_DIR, ".venv/lib/python3.11/site-packages"))
    from evolving.evolving import EvolvingSim
    return EvolvingSim()


# ── apple 后端子命令 ─────────────────────────────────────────────────────────

def apple_positions(args):
    e = _get_evolving()
    result = _with_window_retry(e.getHoldingShares)
    if not result or not result.get("status"):
        print(f"持仓查询失败: {result.get('info', result) if result else '无返回'}", file=sys.stderr)
        sys.exit(1)

    # 行结构: [0]代码 [1]名称 [2]市价 [3]盈亏 [4]当日盈亏 [5]浮动盈亏比
    #         [6]实际数量 [7]股票余额 [8]可用余额 [9]冻结数量 [10]成本价
    #         [11]市值 [12]仓位占比 [13]交易市场 [14]股东账户
    rows = result.get("data", [])
    if args.json:
        _json_out({"ok": True, "count": len(rows), "rows": rows})
        return

    fmt = "{:<10} {:<10} {:>8} {:>8} {:>10} {:>14}"
    print(fmt.format("代码", "名称", "数量", "可用", "成本", "市值"))
    print("-" * 62)
    for r in rows:
        if len(r) < 12:
            continue
        print(fmt.format(r[0], r[1], r[6], r[8], r[10], r[11]))


def apple_orders(args):
    e = _get_evolving()
    result = _with_window_retry(lambda: e.getEntrust("today", False))
    if not result or not result.get("status"):
        print(f"委托查询失败: {result.get('info', result) if result else '无返回'}", file=sys.stderr)
        sys.exit(1)

    # 行结构: [0]日期 [1]时间 [2]代码 [3]名称 [4]方向 [5]状态
    #         [6]委托量 [7]撤销量 [8]委托价 [9]成交价 [10]合同号 [11]属性
    rows = result.get("data", [])
    if args.json:
        _json_out({"ok": True, "count": len(rows), "rows": rows})
        return

    fmt = "{:<10} {:<8} {:<6} {:<4} {:<8} {:>6} {:>8} {:>8} {:<14}"
    print(fmt.format("时间", "代码", "名称", "方向", "状态", "委托量", "委托价", "成交价", "合同号"))
    print("-" * 78)
    for r in rows:
        if len(r) < 11:
            continue
        print(fmt.format(r[1], r[2], r[3], r[4], r[5], r[6], r[8], r[9], r[10]))


def apple_cancel(args):
    contract = args.contract
    e = _get_evolving()
    ok, msg = e.revokeEntrust(revokeType="contractNo", contractNo=contract)
    if ok:
        print(f"撤单成功  合同号={contract}")
        if args.json:
            _json_out({"ok": True, "contract": contract})
    else:
        print(f"撤单失败: {msg}", file=sys.stderr)
        if args.json:
            _json_out({"ok": False, "contract": contract, "error": msg})
        sys.exit(1)


def apple_buy(args):
    venv_py = os.path.join(SCRIPT_DIR, ".venv/bin/python3")
    py = venv_py if os.path.isfile(venv_py) else sys.executable
    trade = os.path.join(SCRIPT_DIR, "trade.py")
    r = subprocess.run(
        [py, trade, "buy", args.code, str(args.qty), str(args.price)],
        capture_output=False,
    )
    sys.exit(r.returncode)


def apple_sell(args):
    venv_py = os.path.join(SCRIPT_DIR, ".venv/bin/python3")
    py = venv_py if os.path.isfile(venv_py) else sys.executable
    trade = os.path.join(SCRIPT_DIR, "trade.py")
    r = subprocess.run(
        [py, trade, "sell", args.code, str(args.qty), str(args.price)],
        capture_output=False,
    )
    sys.exit(r.returncode)


def apple_account(args):
    e = _get_evolving()
    result = _with_window_retry(e.getAccountInfo)
    if args.json:
        _json_out(result)
    else:
        print(result)


# ── frida 后端（原有逻辑，只封装，不修改实现）────────────────────────────────

import socket as _socket
import time as _time

SOCK_PATH = "/tmp/ths_bridge.sock"
_req_id = 0


def _bridge_alive() -> bool:
    if not os.path.exists(SOCK_PATH):
        return False
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(SOCK_PATH)
        sock.close()
        return True
    except OSError:
        return False


def _ensure_bridge_running():
    """桥未运行就启动它（后台常驻，直接 attach 当前 pid），等 socket 就绪。
    ★ 一律带 --no-prime：钩子必须在开面板之前装好，点击顺序由 `prime` 命令驱动。"""
    if _bridge_alive():
        return
    # 取当前 THS 副本 pid
    try:
        ths_pid = subprocess.check_output(
            ["pgrep", "-f", "MacOS/同花顺"], text=True
        ).split()[0].strip()
    except Exception:
        ths_pid = None

    print(f"桥未运行，正在启动（pid={ths_pid}，--no-prime）...", file=sys.stderr)
    bridge = os.path.join(SCRIPT_DIR, "ths_bridge.py")
    venv_py = os.path.join(SCRIPT_DIR, ".venv/bin/python3")
    py = venv_py if os.path.isfile(venv_py) else sys.executable
    cmd = [py, bridge]
    if ths_pid:
        cmd += ["--pid", ths_pid]
    cmd += ["--no-prime"]
    subprocess.Popen(
        cmd,
        stdout=open("/tmp/ths_bridge.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(60):  # 最多等 30 秒 attach
        if _bridge_alive():
            return
        _time.sleep(0.5)
    raise RuntimeError("桥启动超时（30s）")


def _cred_md5():
    """读 ~/.ths_cred.json → (account, md5(password))。

    同花顺模拟登录入口 -[JYMoniLoginWindowController moniTradeLoginWithAccount:password:]
    收的 password 是 App 自己存的那份 md5（实测 aQuotaPassword == md5(明文)），
    所以这里在本地算 md5 再交给 agent。凭据只留在本机，不落日志、不进代码。
    """
    import hashlib
    path = os.path.expanduser("~/.ths_cred.json")
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path) as f:
            cred = json.load(f)
    except Exception as e:
        print(f"读取凭据失败: {e}", file=sys.stderr)
        return None, None
    account  = cred.get("account")  or cred.get("username")
    password = cred.get("password") or cred.get("passwd")
    if not account or not password:
        return None, None
    return account, hashlib.md5(password.encode()).hexdigest()


def _login_via_agent() -> dict:
    """程序化登录模拟仓：agent.loginMoni = 面板 OK 按钮同一条链
    （moniTradeLoginWithAccount:password: → 模拟服务器信息 → 建连 → 激活 → 拉资金）。
    不需要窗口、不需要点击，锁屏可用。返回 agent 结果 dict（失败也有 error/steps）。"""
    account, pw_md5 = _cred_md5()
    if not account or not pw_md5:
        print("未找到 ~/.ths_cred.json 或凭据不完整，跳过程序化登录", file=sys.stderr)
        return {"ok": False, "error": "no_credential"}
    tail = ("*" * (len(account) - 4) + account[-4:]) if len(account) >= 4 else "****"
    print(f"程序化登录模拟仓  account=...{tail}", file=sys.stderr)
    resp = _call("loginMoni", {"account": account, "password": pw_md5})
    res = (resp.get("result") or {}) if resp.get("ok") else {"ok": False, "error": resp.get("error")}
    if not res.get("ok"):
        print(f"  程序化登录失败: {res.get('error')}  steps={res.get('steps')}",
              file=sys.stderr)
    else:
        print(f"  程序化登录成功  moni_count={res.get('moni_count')}  "
              f"active_is_moni={res.get('active_is_moni')}  "
              f"资金就绪={res.get('zichan_ready')}  总资产={res.get('zichan_zzc')}",
              file=sys.stderr)
    return res


def _ensure_login_ready():
    """确保模拟仓登录态。顺序：
       ① 程序化登录（loginMoni，锁屏可用）
       ② loginSim（内存里已有模拟用户时直接切 active）
       ③ 点界面「交易→模拟」（锁屏必然失败，打印原因）
    """
    resp = _call("state")
    st = _ok_or_die(resp)
    if st.get("moni_count", 0) > 0 and st.get("active_is_moni"):
        return

    res = _login_via_agent()
    if res.get("ok") and res.get("active_is_moni"):
        return

    print("程序化登录未成功，尝试 loginSim（内存内切 active）...", file=sys.stderr)
    resp_ls = _call("loginSim")
    ls = (resp_ls.get("result") or {}) if resp_ls.get("ok") else {}
    if ls.get("active_is_moni"):
        print("  loginSim 成功", file=sys.stderr)
        return

    print("模拟仓未登录，尝试点击「交易→模拟」按钮"
          "（★ 当前屏幕锁定/无窗口时这一步必然失败，只是兜底）...", file=sys.stderr)
    script = '''
tell application "System Events"
    set theProcs to processes whose bundle identifier is "com.ths.lab"
    if theProcs is {} then
        set theProcs to processes whose bundle identifier is "cn.com.10jqka.thsx"
    end if
    if theProcs is {} then return "no proc"
    set theProc to item 1 of theProcs
    set frontmost of theProc to true
    delay 0.5
    set allWins to windows of theProc
    set winCount to count of allWins
    if winCount = 0 then return "no windows"
    -- 遍历所有窗口，找到含 button 6 的那个（主交易窗口）
    set theWin to missing value
    repeat with wIdx from 1 to winCount
        set w to window wIdx of theProc
        try
            set dummy to button 6 of w
            set theWin to w
            exit repeat
        end try
    end repeat
    if theWin is missing value then set theWin to window 1 of theProc
    set btnCount to count of buttons of theWin
    -- 先打开交易面板（button 6），如果按钮数较少说明面板未展开
    if btnCount < 32 then
        try
            click button 6 of theWin
            delay 2
        end try
    end if
    -- 点模拟按钮（by name 优先，然后 button 12）
    try
        click (first button of theWin whose name is "模拟")
        return "clicked_by_name"
    on error
    end try
    try
        click button 12 of theWin
        return "clicked_b12"
    on error
        return "not found"
    end try
end tell'''
    r = subprocess.run(["osascript", "-e", script], timeout=15, capture_output=True, text=True)
    click_msg = (r.stdout or "").strip() or (r.stderr or "").strip()
    for _ in range(30):  # 最多等 60 秒（每次 2s）
        _time.sleep(2)
        resp = _call("state")
        result = _ok_or_die(resp)
        if result.get("moni_count", 0) > 0 and result.get("active_is_moni"):
            print("模拟仓登录完成（点击路径）", file=sys.stderr)
            return
    locked = _screen_locked()
    hint = ("屏幕已锁定（CGSSessionScreenIsLocked=Yes），osascript 点击不可能生效" if locked
            else "窗口数为 0 或模拟按钮不可点")
    raise RuntimeError(f"等待模拟仓登录超时（60s）；点击兜底结果={click_msg or '(无输出)'}；{hint}。"
                       f"程序化登录失败请先看上面 loginMoni 的 error/steps")


def _screen_locked() -> bool:
    """读 CGSSessionScreenIsLocked（锁屏时任何点击都用不了）。"""
    try:
        r = subprocess.run(["ioreg", "-n", "Root", "-d1"], capture_output=True, text=True, timeout=5)
        return "CGSSessionScreenIsLocked" in r.stdout and "Yes" in r.stdout.split("CGSSessionScreenIsLocked")[1][:40]
    except Exception:
        return False


def _call(method: str, params: dict = None) -> dict:
    global _req_id
    _req_id += 1
    req = {"id": _req_id, "method": method, "params": params or {}}
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(35)
        sock.connect(SOCK_PATH)
        sock.sendall(json.dumps(req, ensure_ascii=False).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        sock.close()
        return json.loads(buf.split(b"\n", 1)[0])
    except FileNotFoundError:
        print("桥未运行（socket 不存在）", file=sys.stderr)
        sys.exit(2)
    except (ConnectionRefusedError, OSError) as e:
        print(f"无法连接桥: {e}", file=sys.stderr)
        sys.exit(2)


def _ok_or_die(resp: dict):
    if not resp.get("ok"):
        err = resp.get("result", {})
        if isinstance(err, dict):
            err = err.get("error", str(err))
        print(f"错误: {err}", file=sys.stderr)
        sys.exit(1)
    return resp.get("result")


def _get_field(row, *keys):
    """从协议响应 row（可能是 int key 或 str key）取字段值。"""
    for k in keys:
        v = row.get(k)
        if v is not None:
            return str(v)
        v = row.get(str(k))
        if v is not None:
            return str(v)
    return ""


# ── 持仓字段编号映射（从 TQT tableSourceData 实证确认）────────────────────────
# 已实证字段（2026-11-10，3 只持仓真实数据验证）：
#   2102 = 股票代码
#   2103 = 股票名称
#   2117 = 持仓数量（总股数）
#   2164 = 股票余额（等于持仓数量，不是可卖量）
#   2118 = 冻结数量
#   2121 = 可用余额 ← 真正的可卖数量（当日买入被 T+1 冻结后为 0）
#   2026-11-13 实测：159770 全部当日买入 → 2117/2164=24300、2118 冻结=24300、
#   2121 可用=0，卖单被柜台拒「股票余额不足，不允许卖空」。旧映射用 2164 当可用，
#   才造成「可用 24300 却发不出卖单」的误判。
#   2122 = 成本价
#   2124 = 现价
#   2125 = 市值
#   2147 = 盈亏额
#   2106 = 股东账户
#   2108 = 交易市场
_POS_FIELD_CODE  = (2102, 2013, 2008)      # 股票代码
_POS_FIELD_NAME  = (2103, 2034, 2003)      # 名称
_POS_FIELD_QTY   = (2117, 2038, 2126)      # 持仓数量
_POS_FIELD_AVAIL = (2121, 2039, 2040)      # 可用余额（可卖量）
_POS_FIELD_COST  = (2122, 2055, 2054)      # 成本价
_POS_FIELD_MV    = (2125, 2056, 2057)      # 市值
_POS_FIELD_PRICE = (2124, 2069, 2068)      # 现价
_POS_FIELD_MKT   = (2108, 2070, 2071)      # 市场


def _parse_pos_row(row):
    """把协议持仓行（数字 key 字典）解析成结构化字典。"""
    return {
        "代码":   _get_field(row, *_POS_FIELD_CODE),
        "名称":   _get_field(row, *_POS_FIELD_NAME),
        "数量":   _get_field(row, *_POS_FIELD_QTY),
        "可用":   _get_field(row, *_POS_FIELD_AVAIL),
        "成本":   _get_field(row, *_POS_FIELD_COST),
        "市值":   _get_field(row, *_POS_FIELD_MV),
        "现价":   _get_field(row, *_POS_FIELD_PRICE),
        "市场":   _get_field(row, *_POS_FIELD_MKT),
        "_raw":   row,
    }


def frida_positions(args):
    """持仓查询：读单钩子缓存。缓存空时自动 prime 一次。"""
    resp = _call("read_positions")
    result = resp.get("result", {}) if resp.get("ok") else {}

    # 缓存空 → 自动 prime 一次再重试
    if not result.get("ok") and result.get("error") in (
            "cache_empty_need_prime", "need_open_positions_ui"):
        _auto_prime_if_empty("positions")
        resp = _call("read_positions")
        result = resp.get("result", {}) if resp.get("ok") else {}

    if not result.get("ok"):
        err = result.get("error", "no positions data")
        if args.json:
            _json_out({"ok": True, "count": 0, "rows": [],
                       "note": f"positions empty after prime: {err}"})
        else:
            print(f"持仓查询: 0 行（prime 后仍为空）  err={err}")
        return

    raw_rows = result.get("rows", [])
    source   = result.get("source", "scan_direct")

    parsed = [_parse_pos_row(r) for r in raw_rows]
    parsed = [p for p in parsed if p["代码"].strip()]

    if not parsed:
        if args.json:
            _json_out({"ok": False, "error": "parsed 0 rows with non-empty code",
                       "raw_count": len(raw_rows),
                       "raw_row0": raw_rows[0] if raw_rows else None})
        else:
            print(f"持仓解析: 代码字段为空，raw_row0 keys={list(raw_rows[0].keys()) if raw_rows else []}", file=sys.stderr)
        return

    if args.json:
        _json_out({"ok": True, "count": len(parsed), "source": source,
                   "rows": [{k: v for k, v in p.items() if k != "_raw"} for p in parsed]})
        return

    fmt = "{:<10} {:<10} {:>8} {:>8} {:>10} {:>14}"
    print(fmt.format("代码", "名称", "数量", "可用", "成本", "市值"))
    print(f"  (source={source})")
    print("-" * 64)
    for p in parsed:
        print(fmt.format(p["代码"], p["名称"], p["数量"], p["可用"], p["成本"], p["市值"]))


# ── 委托字段编号映射（2026-11-12 实测：9 条真实委托全字段确认）─────────────────
# 数据来源：TQT setTableSourceData: 快照（实例 1，nType=3），当日委托 9 行实测：
#   row = {2139=20261112 委托日期, 2140=11:19:48 委托时间, 2102=159928 代码,
#          2103=消费ETF汇 名称, 2109=买入 操作(方向), 2105=未成交 备注(状态),
#          2126=5000 委托数量, 2120=0 撤销数量, 2127=0.686 委托价,
#          2129=0.000 成交价, 2135=6359383521 合同编号, 2108=深圳Ａ股 市场,
#          2106=00105815865 股东账户, 4008=0 委托属性}
# 与同花顺界面「委托」表 12 列一一对应（委托日期/委托时间/证券代码/证券名称/操作/
# 备注/委托数量/撤销数量/委托价格/成交价格/合同编号/委托属性）。
# ★ 关键：委托表**没有成交量列**，2120 是「撤销数量」（全部撤单行 2120=委托量，
#   全部成交行 2120=0）。所以"成交量"只能由状态推：全部成交→委托量，撤单/未成交→0。
_ORD_FIELD_DATE    = (2139,)           # 委托日期
_ORD_FIELD_TIME    = (2140,)           # 委托时间
_ORD_FIELD_CODE    = (2102,)           # 证券代码
_ORD_FIELD_NAME    = (2103,)           # 证券名称
_ORD_FIELD_JYLX    = (2109,)           # 操作：买入/卖出
_ORD_FIELD_STATUS  = (2105,)           # 备注/状态：未成交/全部成交/全部撤单
_ORD_FIELD_QTY     = (2126,)           # 委托数量
_ORD_FIELD_PRICE   = (2127,)           # 委托价格
_ORD_FIELD_CANCELQ = (2120,)           # 撤销数量（不是成交量！）
_ORD_FIELD_CPRICE  = (2129,)           # 成交价格
_ORD_FIELD_HTBH    = (2135,)           # ★合同号（界面「合同编号」列）
_ORD_FIELD_MKCODE  = (2108,)           # 市场：深圳Ａ股/上海Ａ股
_ORD_FIELD_ATTR    = (4008,)           # 委托属性
_ORD_FIELD_GDZH    = (2106,)           # 股东账户

# 市场名 → mkcode（同花顺协议：0=深圳，1=上海）
_MKCODE_BY_MARKET = {"深圳Ａ股": "0", "上海Ａ股": "1"}
# 操作 → jylx（0=买入，1=卖出）
_JYLX_BY_ACTION = {"买入": "0", "卖出": "1"}


def _order_filled_qty(status: str, qty: str, cancel_qty: str) -> str:
    """委托行无成交量字段（2120 是撤销数量），只能按状态推：
    全部成交 → 委托量；未成交/撤单 → 0；部分成交 → 空（无法确定）。"""
    if "全部成交" in status:
        return qty
    if "撤" in status or "未成交" in status:
        return "0"
    return ""


def _parse_ord_row(row):
    """把协议委托行（数字 key 字典）解析成结构化字典。"""
    status = _get_field(row, *_ORD_FIELD_STATUS)
    qty    = _get_field(row, *_ORD_FIELD_QTY)
    cancelq = _get_field(row, *_ORD_FIELD_CANCELQ)
    return {
        "日期":   _get_field(row, *_ORD_FIELD_DATE),
        "时间":   _get_field(row, *_ORD_FIELD_TIME),
        "代码":   _get_field(row, *_ORD_FIELD_CODE),
        "名称":   _get_field(row, *_ORD_FIELD_NAME),
        "方向":   _get_field(row, *_ORD_FIELD_JYLX),
        "状态":   status,
        "委托量": qty,
        "委托价": _get_field(row, *_ORD_FIELD_PRICE),
        "成交量": _order_filled_qty(status, qty, cancelq),
        "成交价": _get_field(row, *_ORD_FIELD_CPRICE),
        "合同号": _get_field(row, *_ORD_FIELD_HTBH),
        "市场":   _get_field(row, *_ORD_FIELD_MKCODE),
        "撤销量": cancelq,
        "股东账户": _get_field(row, *_ORD_FIELD_GDZH),
        "_raw":   row,
    }


def _find_trading_win_script():
    """返回一段 AppleScript 片段（不含 tell 外壳），找到含 button 6 的主窗口存入 theWin。"""
    return '''
    set theProcs to processes whose name is "同花顺"
    if theProcs is {} then return "no proc"
    set theProc to item 1 of theProcs
    set frontmost of theProc to true
    delay 0.3
    set winCount to count of windows of theProc
    if winCount is 0 then return "no windows"
    set theWin to missing value
    repeat with wIdx from 1 to winCount
        set w to window wIdx of theProc
        try
            set dummy to button 6 of w
            set theWin to w
            exit repeat
        end try
    end repeat
    if theWin is missing value then
        set maxUe to 0
        repeat with wIdx from 1 to winCount
            set w to window wIdx of theProc
            try
                set ueCount to count of UI elements of w
                if ueCount > maxUe then
                    set maxUe to ueCount
                    set theWin to w
                end if
            end try
        end repeat
    end if
    if theWin is missing value then return "no trading window"'''


def _prime_click_sequence():
    """实测唯一能抓到委托的点击序列（顺序错就读不到）：
        button 6（交易）→ 等 4s → button「模拟」→ 等 8s → button「委托」→ 等 6s
        → 取消「只显示可撤委托」勾选（否则只有未成交的 3 条）
    前提：钩子必须在开面板之前装好（bridge 用 --no-prime attach，本函数才第一次开面板）。
    返回 AppleScript 执行结果字符串。"""
    _ensure_window()   # 窗口数为 0 时（同花顺偶发关窗）激活进程 + Cmd+N 恢复窗口
    script = '''
tell application "System Events"
''' + _find_trading_win_script() + '''
    -- 1) 点 button 6 打开交易面板
    try
        click button 6 of theWin
    on error errMsg
        return "b6_err:" & errMsg
    end try
    delay 4
    -- 2) 点「模拟」登录模拟仓
    try
        click (first button of theWin whose name is "模拟")
    on error errMsg2
        return "moni_err:" & errMsg2
    end try
    delay 8
    -- 3) 点「委托」tab，触发委托 tableSourceData 快照
    try
        click (first button of theWin whose name is "委托")
    on error errMsg3
        return "wt_err:" & errMsg3
    end try
    delay 3
    -- 4) 取消「只显示可撤委托」勾选：勾着只有未成交的 3 条，取消后才是当日全部 9 条
    set cbInfo to "no_checkbox"
    try
        repeat with cb in (every checkbox of theWin)
            set cbHelp to ""
            try
                set cbHelp to (help of cb) as string
            end try
            if cbHelp is not "点击切换至多股同列" then
                if (value of cb) is 1 then
                    perform action "AXPress" of cb
                    set cbInfo to "unchecked"
                else
                    set cbInfo to "already_off"
                end if
            end if
        end repeat
    on error cbErr
        set cbInfo to "cb_err:" & cbErr
    end try
    delay 4
    return "done:交易→模拟→委托 可撤过滤=" & cbInfo
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           timeout=40, capture_output=True, text=True)
        return r.stdout.strip() or r.stderr.strip() or "(empty)"
    except Exception as e:
        return f"ERR:{e}"


def _prime_cache_tabs():
    """点 持仓/委托/成交 三个 tab 各一次（退化路径，requestQueryDirect 失败时用）。
    遍历所有窗口找到含 button 6 的主交易窗口，不硬编码 window 1。"""
    script = '''
tell application "System Events"
''' + _find_trading_win_script() + '''
    set results to {}
    repeat with tabName in {"持仓", "委托", "成交"}
        set hit to false
        try
            click (first button of theWin whose name is tabName)
            set hit to true
        end try
        set end of results to (tabName & ":" & (hit as string))
        delay 2.5
    end repeat
    return results as string
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           timeout=25, capture_output=True, text=True)
        return r.stdout.strip()
    except Exception as e:
        return f"ERR:{e}"


def frida_orders(args):
    """委托查询：读单钩子缓存。缓存空时自动 prime 一次。0 行委托是合法结果（当天无委托）。"""

    params = {}
    if getattr(args, "date", None):
        d = args.date.replace("-", "")
        params = {"beginDate": d, "endDate": d}

    resp = _call("read_orders", params if params else None)
    result = resp.get("result", {}) if resp else {}
    # 缓存空 → 自动 prime 一次再重试
    if not result.get("ok") and result.get("error") in (
            "cache_empty_need_prime", "need_open_orders_ui"):
        _auto_prime_if_empty("orders")
        resp = _call("read_orders", params if params else None)
        result = resp.get("result", {}) if resp else {}
    if not result.get("ok"):
        err = result.get("error", "委托查询失败")
        if args.json:
            _json_out({"ok": True, "count": 0, "rows": [],
                       "note": f"orders empty after prime: {err}"})
        else:
            print(f"委托查询: 0 行（prime 后仍为空，当天无委托）  err={err}")
        return

    raw_rows = result.get("rows", [])
    source = result.get("source", "read_orders")
    send_detected = result.get("send_detected")
    date_used = result.get("beginDate") or result.get("endDate")

    if not raw_rows:
        # 0 委托是合法结果（当天无委托记录）
        if args.json:
            _json_out({"ok": True, "count": 0, "source": source,
                       "rows": [], "note": "no orders for date: " + (date_used or "today")})
        else:
            print(f"委托查询: 0 行  source={source}  date={date_used or 'today'}")
        return

    parsed = [_parse_ord_row(r) for r in raw_rows]

    if not parsed:
        if args.json:
            _json_out({"ok": False, "error": "0 order rows",
                       "raw_count": len(raw_rows),
                       "raw_row0": raw_rows[0] if raw_rows else None})
        else:
            print(f"委托查询: 0 行  source={source}", file=sys.stderr)
        return

    if args.json:
        _json_out({"ok": True, "count": len(parsed), "source": source,
                   "send_detected": send_detected,
                   "rows": [{k: v for k, v in p.items() if k != "_raw"} for p in parsed]})
        return

    fmt = "{:<10} {:<8} {:<10} {:<4} {:<8} {:>7} {:>8} {:>7} {:>8} {:<13} {:<8}"
    print(fmt.format("时间", "代码", "名称", "方向", "状态",
                     "委托量", "委托价", "成交量", "成交价", "合同号", "市场"))
    print(f"  (source={source} send_detected={send_detected})")
    print("-" * 100)
    for p in parsed:
        print(fmt.format(p["时间"], p["代码"], p["名称"], p["方向"], p["状态"],
                         p["委托量"], p["委托价"], p["成交量"], p["成交价"],
                         p["合同号"], p["市场"]))


# ── 资金字段（agent readAccountDirect 原名，值为两位小数字符串）─────────────────
_ACCT_NUM_FIELDS = ("总资产", "总市值", "总盈亏", "当日盈亏",
                    "资金余额", "可取金额", "可用金额", "冻结金额")


def frida_account(args):
    """账户资金：agent readAccountDirect（UserManager→activeUser→zichanData）。

    纯 getter 链，不点界面、不依赖缓存、不需要 prime；agent 返回已格式化的
    两位小数字符串，这里统一转 float 输出（资金账号保持字符串）。
    """
    resp = _call("read_account_direct")
    if not resp or not resp.get("ok"):
        err = (resp or {}).get("result", {})
        if isinstance(err, dict):
            err = err.get("error", str(err))
        if args.json:
            _json_out({"ok": False, "error": err})
        else:
            print(f"账户查询失败: {err}", file=sys.stderr)
        sys.exit(1)

    result = resp.get("result") or {}
    if not result.get("ok"):
        err = result.get("error", "no account data")
        if args.json:
            _json_out({"ok": False, "error": err, "fields": result})
        else:
            print(f"账户查询失败: {err}", file=sys.stderr)
        sys.exit(1)

    source = "read_account_direct"
    acct = {}
    for k in _ACCT_NUM_FIELDS:
        v = str(result.get(k) or "").strip().replace(",", "")
        acct[k] = float(v) if v else 0.0
    acct["资金账号"] = str(result.get("资金账号") or "").strip()

    if args.json:
        _json_out({"ok": True, "source": source,
                   "is_moni": result.get("is_moni"), **acct})
        return

    print(f"账户资金  (source={source}  is_moni={result.get('is_moni')})")
    print("-" * 44)
    for k in _ACCT_NUM_FIELDS:
        print(f"  {k:<6} {acct[k]:>14,.2f}")
    print(f"  {'资金账号':<6} {acct['资金账号']:>14}")


def frida_cancel(args):
    """撤单：走 ths.Ths.cancel（App 自己的 sendChedanRequestByTableIndex:）。

    默认 dry-run（只 probe，不发）；--real 才真发，并轮询委托表确认状态变化。
    """
    from ths import Ths, ThsError
    t = Ths()
    try:
        r = t.cancel(args.contract, real=bool(args.real))
    except ThsError as ex:
        err = {"ok": False, "error": str(ex), "result": ex.result}
        if args.json:
            _json_out(err)
        else:
            print(f"撤单失败: {ex}", file=sys.stderr)
        sys.exit(1)
    finally:
        t.close()
    out = {"ok": True, **r}
    if args.json:
        _json_out(out)
    else:
        print(f"撤单  合同号={r['contract']}  {r.get('old_status')} → {r.get('new_status')}")
    return out


def frida_login(args):
    """冷启动模拟仓登录（锁屏、无窗口、零点击）。

    主路径 agent.loginMoni（= 「交易→模拟」面板 OK 按钮同一条链）：
      ① -[JYMoniLoginWindowController moniTradeLoginWithAccount:password:]
         取模拟服务器信息 → 建连 → 登录，UserManager 里出现 isMoni=1 的 UserModel
      ② 补 A股 类型位 + setActiveUser:（getActiveUser 的类型掩码只认 1/2，
         只置 SetType:4 会永远返回 nil）
      ③ getCacheDataObject.requestZijin（资金回填，否则 zichanData 全空）
    失败才退化：loginSim → 点界面「交易→模拟」（锁屏必失败，会打印原因）。
    """
    st = (_call("state").get("result") or {})
    if st.get("active_is_moni") and st.get("moni_count"):
        print("已是模拟仓登录态，补齐资金/激活", file=sys.stderr)

    res = _login_via_agent()
    if not (res.get("ok") and res.get("active_is_moni")):
        _ensure_login_ready()          # 内含 loginSim 与点击兜底

    st_f = _call("state").get("result") or {}
    if not st_f.get("active_is_moni"):
        print("登录失败: active_is_moni 仍为 False", file=sys.stderr)
        if args.json:
            _json_out({"ok": False, "method": "loginMoni",
                       "error": res.get("error") or "active_is_moni=False",
                       "login": res, "state": st_f})
        sys.exit(1)

    _do_prime()
    if args.json:
        _json_out({"ok": True, "method": "loginMoni", "state": st_f, "login": res})
    else:
        _print_state(st_f)
        print(f"  资金: 总资产={res.get('zichan_zzc') or '(未就绪)'}")


def _cache_counts():
    """读三类缓存行数。"""
    r = _call("get_cache_status")
    st = r.get("result", {}) if r.get("ok") else {}
    return {c: st.get(c, {}).get("count", 0) for c in ("positions", "orders", "trades")}


def _click_tab(name: str) -> str:
    """在交易主窗口点指定 tab（持仓/委托/成交），返回执行结果字符串。"""
    script = '''
tell application "System Events"
''' + _find_trading_win_script() + '''
    try
        click (first button of theWin whose name is "%s")
        return "clicked:%s"
    on error errMsg
        return "err:" & errMsg
    end try
end tell''' % (name, name)
    try:
        r = subprocess.run(["osascript", "-e", script],
                           timeout=20, capture_output=True, text=True)
        return r.stdout.strip() or r.stderr.strip() or "(empty)"
    except Exception as e:
        return f"ERR:{e}"


def _poll_counts(pred, max_wait: float = 15.0):
    """每秒读一次缓存计数，pred 成立且连续 2s 无变化后返回最新计数。"""
    prev = _cache_counts()
    last_change = 0.0
    elapsed = 0.0
    cur = dict(prev)
    while elapsed < max_wait:
        _time.sleep(1.0)
        elapsed += 1.0
        cur = _cache_counts()
        changed = []
        for cat in ("positions", "orders", "trades"):
            if cur[cat] != prev[cat]:
                changed.append(f"{cat}:{prev[cat]}→{cur[cat]}")
                prev[cat] = cur[cat]
                last_change = elapsed
        if changed:
            print(f"  [{elapsed:.0f}s] 变化: {', '.join(changed)}  当前: "
                  f"pos={cur['positions']} ord={cur['orders']} trd={cur['trades']}",
                  file=sys.stderr)
        if pred(cur) and elapsed - last_change >= 2:
            break
    return cur


def _do_prime(only: str = None):
    """按分类补抓：只抓缓存为空的那一类，不再「有数据就整体跳过」。

    positions / orders 空 → 基础点击序列（实测唯一能抓到委托的路径）：
        button 6（交易）→ 4s →「模拟」→ 8s →「委托」→ 取消「只显示可撤委托」→ 4s
        （勾着可撤过滤时委托表只有未成交的几行，取消后才是当日全部行）
    trades 空 → 面板已打开后点「成交」tab；实测点 tab 会新建表格对象并推快照（当日 4 条）
        → 只点该 tab，绝不为了成交去点「委托」，避免可撤过滤把 9 行委托冲掉。
        若「成交」本来就是选中态（点它不产生变化），切「持仓」再切回「成交」强制刷新。

    only: 只补指定分类（'positions'/'orders'/'trades'），None = 补所有空的分类。
    """
    cur = _cache_counts()
    targets = [only] if only else [c for c in ("positions", "orders", "trades") if cur[c] == 0]
    targets = [c for c in targets if cur[c] == 0]
    if not targets:
        print(f"prime: 缓存已有数据 pos={cur['positions']} ord={cur['orders']} "
              f"trd={cur['trades']}，跳过重抓", file=sys.stderr)
        return cur
    print(f"prime: 缺 {','.join(targets)} → 按分类补抓", file=sys.stderr)

    # ① 程序化：nib → TradeQueryTemplate → didAppear → requestByType:（不点任何按钮）
    res = _programmatic_prime(targets)
    cur = _cache_counts()
    if res.get("active_is_moni"):
        created = res.get("created") or {}
        # 模板已建且查询已发出：0 行也是有效结果，不再退化成点击
        targets = [c for c in targets if cur[c] == 0 and not created.get(c)]
    if not targets:
        print(f"prime 完成（程序化零点击）: pos={cur['positions']} ord={cur['orders']} "
              f"trd={cur['trades']}", file=sys.stderr)
        return cur

    print("prime: 程序化未取到数据 → 退回界面点击序列", file=sys.stderr)

    if "positions" in targets or "orders" in targets:
        print("prime: 执行点击序列（交易→4s→模拟→8s→委托→取消可撤过滤→4s）...",
              file=sys.stderr)
        click_result = _prime_click_sequence()
        print(f"prime: 点击序列完成: {click_result}", file=sys.stderr)
        cur = _poll_counts(lambda c: c["positions"] > 0 and c["orders"] > 0, 18)

    if "trades" in targets and cur["trades"] == 0:
        r = _click_tab("成交")
        print(f"prime: 点「成交」tab: {r}", file=sys.stderr)
        if not r.startswith("clicked"):
            # 交易窗口/面板没开（按钮找不到）→ 先跑基础序列再点一次
            print("prime: 交易面板未开，先跑基础序列...", file=sys.stderr)
            _prime_click_sequence()
            r = _click_tab("成交")
            print(f"prime: 重试「成交」tab: {r}", file=sys.stderr)
        cur = _poll_counts(lambda c: c["trades"] > 0, 8)
        if cur["trades"] == 0:
            # 「成交」本来就是选中态时点它不会推新快照 → 先切「持仓」再切回「成交」强制刷新
            _click_tab("持仓")
            _time.sleep(1.0)
            r2 = _click_tab("成交")
            print(f"prime: 「成交」已选中无新快照，切持仓再切回: {r2}", file=sys.stderr)
            cur = _poll_counts(lambda c: c["trades"] > 0, 10)

    print(f"prime 完成: pos={cur['positions']} ord={cur['orders']} trd={cur['trades']}",
          file=sys.stderr)
    # 通知 agent（noop，保持兼容）
    _call("prime_cache")
    return cur


def _programmatic_prime(cats) -> dict:
    """程序化建 TradeQueryTemplate 并触发查询（零点击）。返回 ensure 结果。"""
    resp = _call("ensure_query_templates", {"types": list(cats)})
    res = resp.get("result", {}) if resp.get("ok") else {"ok": False, "error": resp.get("error")}
    if not res.get("created"):
        print(f"[programmatic] 建模板失败: {res.get('errors') or res.get('error')}",
              file=sys.stderr)
        return res
    print(f"[programmatic] 建模板: created={res.get('created')} "
          f"active_is_moni={res.get('active_is_moni')} moni_count={res.get('moni_count')}",
          file=sys.stderr)
    if not res.get("active_is_moni"):
        # 模拟账户还没登录：模板建了也查不到数据，直接退回登录/点击兜底，不空等
        return res
    # 等主线程 didAppear 的查询结果落到单钩子缓存（最多 12s）
    deadline = _time.time() + 12.0
    last = None
    while _time.time() < deadline:
        cur = _cache_counts()
        if all(cur.get(c, 0) > 0 for c in cats):
            break
        if cur != last:
            print(f"[programmatic] 缓存: {cur}", file=sys.stderr)
            last = cur
        _time.sleep(0.5)
    return res


def _auto_prime_if_empty(cat: str) -> bool:
    """若指定分类缓存为空，只补该分类一次。返回补后该分类是否有数据。"""
    if _cache_counts().get(cat, 0) > 0:
        return True
    print(f"[auto-prime] {cat} 缓存为空，按分类补抓...", file=sys.stderr)
    final = _do_prime(only=cat)
    return (final or {}).get(cat, 0) > 0


def frida_prime(args):
    """手动触发缓存填充（已验证路径：交易→4s→模拟→8s→委托→取消可撤过滤 + 轮询）。"""
    final = _do_prime()
    # 读缓存状态（_do_prime 已打印变化过程）
    resp = _call("get_cache_status")
    status = resp.get("result", {}) if resp.get("ok") else {}
    if args.json:
        _json_out({"ok": True, "cache": status})
    else:
        pos = status.get("positions", {})
        ord_ = status.get("orders", {})
        trd  = status.get("trades", {})
        print(f"缓存状态: positions={pos.get('count',0)}行  "
              f"orders={ord_.get('count',0)}行  "
              f"trades={trd.get('count',0)}行")


def _print_state(st):
    """格式化打印 state 结构。"""
    print(f"  moni_count={st.get('moni_count')}  "
          f"active_is_moni={st.get('active_is_moni')}  "
          f"active_is_disconnect={st.get('active_is_disconnect')}  "
          f"active_is_user_login={st.get('active_is_user_login')}")
    tcm = st.get("active_trade_commu_model") or ""
    print(f"  active_trade_commu_model={str(tcm)[:30] or '(空)'}")


# ── 成交明细字段编号映射（2026-11-12 现场实测：当日 4 条成交全字段确认）─────────
# 数据来源：点「成交」tab 后 TQT setTableSourceData: 快照（4 行），每行 description 原文：
#   {2141 = 20261112       成交日期
#    2142 = "14:30:36"     成交时间
#    2102 = 512890         证券代码
#    2103 = "红利低波ET"   证券名称
#    2109 = "卖出"         方向（买入/卖出）
#    2128 = 100            成交量
#    2129 = "1.229"        成交价格
#    2131 = "122.900"      成交金额（= 成交量 × 成交价）
#    2135 = 6359272087     合同编号
#    2130 = 6725345540     成交编号
#    2108 = "上海Ａ股"     市场
#    2106 = A481535989     股东账户
#    2165 = ""  2147 = ""  4008 = ""  2201 = NUL}
# ★ 与委托行的区别：成交行有 2141（成交日期）/ 2142（成交时间）/ 2128（成交量），
#   没有 2139（委托日期）/ 2140（委托时间）/ 2126（委托量）。
# ★ 成交行也有 2135（合同号），但 2129 在成交行是成交价、在委托行才是成交价；方向都看 2109。
_TRD_FIELD_TIME   = (2142,)        # 成交时间
_TRD_FIELD_CODE   = (2102,)        # 证券代码
_TRD_FIELD_NAME   = (2103,)        # 证券名称
_TRD_FIELD_JYLX   = (2109,)        # 方向：买入/卖出
_TRD_FIELD_CPRICE = (2129,)        # 成交价格
_TRD_FIELD_CVOL   = (2128,)        # 成交量
_TRD_FIELD_AMT    = (2131,)        # 成交金额
_TRD_FIELD_HTBH   = (2135,)        # 合同编号


def _parse_trd_row(row):
    """把协议成交行（数字 key 字典）解析成结构化字典。"""
    return {
        "时间":     _get_field(row, *_TRD_FIELD_TIME),
        "代码":     _get_field(row, *_TRD_FIELD_CODE),
        "名称":     _get_field(row, *_TRD_FIELD_NAME),
        "方向":     _get_field(row, *_TRD_FIELD_JYLX),
        "成交价":   _get_field(row, *_TRD_FIELD_CPRICE),
        "成交量":   _get_field(row, *_TRD_FIELD_CVOL),
        "成交金额": _get_field(row, *_TRD_FIELD_AMT),
        "合同号":   _get_field(row, *_TRD_FIELD_HTBH),
        "_raw":     row,
    }


def frida_trades(args):
    """成交明细查询：读单钩子缓存。缓存空时自动 prime 一次。"""
    params = {}
    if getattr(args, "date", None):
        d = args.date.replace("-", "")
        params = {"beginDate": d, "endDate": d}

    resp = _call("read_trades", params if params else None)
    result = resp.get("result", {}) if resp else {}

    # 缓存空 → 自动 prime 一次再重试
    if not result.get("ok") and result.get("error") in (
            "cache_empty_need_prime", "need_open_trades_ui"):
        _auto_prime_if_empty("trades")
        resp = _call("read_trades", params if params else None)
        result = resp.get("result", {}) if resp else {}

    if not result.get("ok"):
        err = result.get("error", "")
        if args.json:
            _json_out({"ok": True, "count": 0, "rows": [],
                       "note": f"trades empty after prime: {err}"})
        else:
            print(f"成交明细: 0 条（prime 后仍为空，当天无成交）  err={err}")
        return

    raw_rows = result.get("rows", [])
    source   = result.get("source", "read_trades")
    note     = result.get("note", "")

    if not raw_rows:
        if args.json:
            _json_out({"ok": True, "count": 0, "source": source, "rows": [],
                       "note": note or "no trades today"})
        else:
            print(f"成交明细: 0 条  source={source}  {note}")
        return

    parsed = [_parse_trd_row(r) for r in raw_rows]
    parsed = [p for p in parsed if p["代码"].strip()]

    if args.json:
        _json_out({"ok": True, "count": len(parsed), "source": source,
                   "rows": [{k: v for k, v in p.items() if k != "_raw"} for p in parsed]})
        return

    fmt = "{:<10} {:<8} {:<8} {:<4} {:>8} {:>8} {:>12} {:<14}"
    print(fmt.format("时间", "代码", "名称", "方向", "成交价", "成交量", "成交金额", "合同号"))
    print(f"  (source={source})")
    print("-" * 82)
    for p in parsed:
        print(fmt.format(p["时间"], p["代码"], p["名称"], p["方向"],
                         p["成交价"], p["成交量"], p["成交金额"], p["合同号"]))


def frida_detail(args):
    """委托详情：从委托缓存按合同号取全字段。缓存空时自动 prime 一次。"""
    contract = args.contract

    resp = _call("read_orders", None)
    result = resp.get("result", {}) if resp else {}

    # 缓存空 → 自动 prime 一次再重试
    if not result.get("ok") and result.get("error") in (
            "cache_empty_need_prime", "need_open_orders_ui"):
        _auto_prime_if_empty("orders")
        resp = _call("read_orders", None)
        result = resp.get("result", {}) if resp else {}

    if not result.get("ok"):
        err = result.get("error", "委托查询失败")
        if args.json:
            _json_out({"ok": False, "error": err})
        else:
            print(f"委托详情查询失败: {err}", file=sys.stderr)
        sys.exit(1)

    raw_rows = result.get("rows") or []
    source   = result.get("source", "read_orders")

    target_raw = None
    for row in raw_rows:
        htbh = _get_field(row, *_ORD_FIELD_HTBH)
        if htbh == contract:
            target_raw = row
            break

    if target_raw is None:
        msg = f"未在委托列表（{len(raw_rows)} 行）中找到合同号 {contract}"
        if args.json:
            _json_out({"ok": False, "error": msg, "row_count": len(raw_rows)})
        else:
            print(f"委托详情: {msg}", file=sys.stderr)
        sys.exit(1)

    p = _parse_ord_row(target_raw)

    if args.json:
        _json_out({
            "ok": True,
            "source": source,
            "note": "委托详情来自 orders 行全字段（无独立详情接口）",
            "contract": contract,
            "detail": {k: v for k, v in p.items() if k != "_raw"},
            "raw_keys": list(target_raw.keys()),
            "raw": target_raw,
        })
        return

    print(f"委托详情  合同号={contract}  (source={source})")
    print(f"  注：数据来自 orders 行全字段，无独立详情接口")
    print("-" * 60)
    for field, value in p.items():
        if field == "_raw":
            continue
        print(f"  {field:<10} {value}")
    print(f"\n  原始字段（共 {len(target_raw)} 个）:")
    for k, v in sorted(target_raw.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0):
        print(f"    {k:<8} = {v}")


def _frida_place(args, side):
    """接口化下单：走 ths.Ths.buy/sell（App 自己的下单方法 + 委托表核验）。"""
    from ths import Ths, ThsError
    t = Ths()
    try:
        r = t.buy(args.code, args.qty, args.price) if side == "buy" \
            else t.sell(args.code, args.qty, args.price)
    except ThsError as ex:
        err = {"ok": False, "error": str(ex), "result": ex.result}
        if args.json:
            _json_out(err)
        else:
            print(f"{'买入' if side == 'buy' else '卖出'}失败: {ex}", file=sys.stderr)
        sys.exit(1)
    finally:
        t.close()
    out = {"ok": True, **r}
    if args.json:
        _json_out(out)
    else:
        print(f"{'买入' if side == 'buy' else '卖出'}  合同号={r['contract']}  "
              f"{r['code']}  数量={r['shares']}  价格={r['price']}  状态={r['status']}  "
              f"通道={r.get('via')}")
    return out


def frida_buy(args):
    return _frida_place(args, "buy")


def frida_sell(args):
    return _frida_place(args, "sell")


# ── 主入口 ────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(prog="ths_cli", description="同花顺 CLI")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--engine", choices=["apple", "frida"], default="apple",
                   help="后端引擎 (默认: apple)")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ── 通用命令 ──────────────────────────────────────────────────────────────
    sub.add_parser("positions", help="持仓查询")

    op = sub.add_parser("orders", help="委托查询")
    op.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                    help="查询日期（默认今天），格式 YYYY-MM-DD，支持历史日期")

    sub.add_parser("account", help="账户信息")

    cp = sub.add_parser("cancel", help="撤单")
    cp.add_argument("contract", help="合同号")
    cp.add_argument("--real", action="store_true",
                    help="真实执行撤单（默认 dry-run 仅打印字段，加此标志才真发）")

    bp = sub.add_parser("buy", help="买入（走 trade.py）")
    bp.add_argument("code"); bp.add_argument("qty", type=int); bp.add_argument("price", type=float)

    sp2 = sub.add_parser("sell", help="卖出（走 trade.py）")
    sp2.add_argument("code"); sp2.add_argument("qty", type=int); sp2.add_argument("price", type=float)

    # ── frida 专用命令 ────────────────────────────────────────────────────────
    sub.add_parser("login", help="attach 后获取模拟仓登录态 (frida)")
    sub.add_parser("prime", help="手动触发缓存填充：点持仓/委托/成交三个 tab (frida)")

    tp = sub.add_parser("trades", help="成交明细 (frida)")
    tp.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                    help="查询日期（默认今天）")

    dp = sub.add_parser("detail", help="委托详情（按合同号）(frida)")
    dp.add_argument("contract", help="合同号")

    sub.add_parser("state", help="打印当前 state（同 status）(frida)")

    sp_start = sub.add_parser("start", help="启动桥进程 (frida)")
    sp_start.add_argument("--pid", type=int, default=None,
                          help="attach 指定 pid（默认自动取同花顺副本 pid）")
    sub.add_parser("stop",   help="停止桥进程 (frida)")
    sub.add_parser("status", help="桥状态 (frida)")
    sub.add_parser("ping",   help="ping agent (frida)")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.engine == "apple":
        dispatch = {
            "positions": apple_positions,
            "orders":    apple_orders,
            "cancel":    apple_cancel,
            "buy":       apple_buy,
            "sell":      apple_sell,
            "account":   apple_account,
        }
        fn = dispatch.get(args.cmd)
        if fn is None:
            print(f"命令 '{args.cmd}' 在 apple 引擎下不可用", file=sys.stderr)
            sys.exit(2)
        fn(args)

    else:  # frida
        # ── 需要先 attach + login 检查的命令 ────────────────────────────────
        # orders/cancel/detail/trades 走纯 cache 或按需 TQT 路径，不强制 state 检查
        NO_LOGIN_CHECK = ("orders", "cancel", "detail", "trades", "login", "prime",
                          "start", "stop", "status", "state", "ping")

        frida_dispatch = {
            "positions": frida_positions,
            "orders":    frida_orders,
            "account":   frida_account,
            "cancel":    frida_cancel,
            "trades":    frida_trades,
            "detail":    frida_detail,
            "login":     frida_login,
            "prime":     frida_prime,
            "buy":       frida_buy,
            "sell":      frida_sell,
        }

        if args.cmd in frida_dispatch:
            _ensure_bridge_running()
            if args.cmd not in NO_LOGIN_CHECK:
                _ensure_login_ready()
            frida_dispatch[args.cmd](args)

        elif args.cmd in ("status", "state"):
            _ensure_bridge_running()
            resp = _call("state")
            result = _ok_or_die(resp)
            if args.json:
                _json_out(result)
            else:
                _print_state(result)

        elif args.cmd == "start":
            bridge = os.path.join(SCRIPT_DIR, "ths_bridge.py")
            venv_py = os.path.join(SCRIPT_DIR, ".venv/bin/python3")
            py = venv_py if os.path.isfile(venv_py) else sys.executable
            # ★ 只 attach 已跑的副本；不带 --no-prime 就会在装钩子前自动点开交易面板
            pid = getattr(args, "pid", None)
            if not pid:
                try:
                    pid = int(subprocess.check_output(
                        ["pgrep", "-f", "MacOS/同花顺"], text=True).split()[0])
                except Exception:
                    pid = None
            cmd = [py, bridge] + (["--pid", str(pid)] if pid else []) + ["--no-prime"]
            proc = subprocess.Popen(
                cmd,
                stdout=open("/tmp/ths_bridge.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            print(f"桥已启动  mode=attach  pid={pid}  proc={proc.pid}  "
                  f"no_prime=True  log=/tmp/ths_bridge.log")

        elif args.cmd == "stop":
            subprocess.run(["pkill", "-f", "ths_bridge.py"], check=False)
            print("已发送 SIGTERM 给 ths_bridge.py")

        elif args.cmd == "ping":
            _ensure_bridge_running()
            resp = _call("ping")
            result = _ok_or_die(resp)
            if args.json:
                _json_out(result)
            else:
                print(f"pong  ts={result.get('ts')}  agent={result.get('agent')}")

        else:
            print(f"未知命令: {args.cmd}", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
