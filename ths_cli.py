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
    """桥未运行就启动它（后台常驻，直接 attach 当前 pid），等 socket 就绪。"""
    if _bridge_alive():
        return
    # 取当前 THS 副本 pid
    try:
        ths_pid = subprocess.check_output(
            ["pgrep", "-f", "MacOS/同花顺"], text=True
        ).split()[0].strip()
    except Exception:
        ths_pid = None

    print(f"桥未运行，正在启动（pid={ths_pid}）...", file=sys.stderr)
    bridge = os.path.join(SCRIPT_DIR, "ths_bridge.py")
    venv_py = os.path.join(SCRIPT_DIR, ".venv/bin/python3")
    py = venv_py if os.path.isfile(venv_py) else sys.executable
    cmd = [py, bridge]
    if ths_pid:
        cmd += ["--pid", ths_pid]
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


def _ensure_login_ready():
    """等 moni 登录就绪；count=0 则点「模拟」按钮，最多等 60 秒。"""
    resp = _call("state")
    result = _ok_or_die(resp)
    if result.get("moni_count", 0) > 0:
        return
    print("模拟仓未登录，尝试点击「模拟」按钮...", file=sys.stderr)
    script = '''
tell application "System Events"
    set theProcs to processes whose bundle identifier is "com.ths.lab"
    if theProcs is {} then return "no proc"
    set theProc to item 1 of theProcs
    set frontmost of theProc to true
    delay 0.3
    try
        click (first button of theProc whose name is "模拟")
        return "clicked"
    on error
        return "not found"
    end try
end tell'''
    subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True, text=True)
    for _ in range(30):  # 最多等 60 秒（每次 2s）
        _time.sleep(2)
        resp = _call("state")
        result = _ok_or_die(resp)
        if result.get("moni_count", 0) > 0:
            print("模拟仓登录完成", file=sys.stderr)
            return
    raise RuntimeError("等待模拟仓登录超时（60s）")


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


# ── 持仓字段编号映射（从 TQT OnTradeRsponceNotify: 协议响应反推）─────────────────
# 已知真实值：159928/15100股, 512800/11500股, 512890/33200股
# 字段编号通过 fetch_positions dump 验证后写入；首次以 adaptive 方式扫描
#   2013 = 股票代码 (zqdm)
#   2034 = 股票名称
#   2038 = 持仓数量（总股数/股票余额）
#   2039 = 可用数量
#   2055 = 参考成本价
#   2056 = 市值
#   2060 = 盈亏额
#   2066 = 盈亏比例
#   2069 = 现价
#   2070 = 交易市场/mkcode
_POS_FIELD_CODE  = (2013, 2008)        # 股票代码
_POS_FIELD_NAME  = (2034, 2003)        # 名称
_POS_FIELD_QTY   = (2038, 2126)        # 持仓数量
_POS_FIELD_AVAIL = (2039, 2040)        # 可用数量
_POS_FIELD_COST  = (2055, 2054)        # 成本价
_POS_FIELD_MV    = (2056, 2057)        # 市值
_POS_FIELD_PRICE = (2069, 2068)        # 现价
_POS_FIELD_MKT   = (2070, 2071)        # 市场


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
    """持仓查询：dispatch req_type=0x601 → TQT hook 缓存 → 解析协议响应行。
    数据完全来自协议响应，不读 NSTableView / 视图树 / 屏幕。"""
    resp = _call("fetch_positions")
    result = _ok_or_die(resp)

    if not result.get("ok"):
        err = result.get("error", "unknown")
        if args.json:
            _json_out({"ok": False, "error": err,
                       "cache_summary": result.get("cache_summary"),
                       "send_detected": result.get("send_detected")})
        else:
            print(f"持仓查询失败: {err}", file=sys.stderr)
            cs = result.get("cache_summary")
            if cs:
                for ntype, info in cs.items():
                    print(f"  cache nType={ntype} count={info['count']} keys={info['first_keys']}")
        return

    raw_rows = result.get("rows", [])
    source = result.get("source", "protocol_response")
    send_detected = result.get("send_detected")

    parsed = [_parse_pos_row(r) for r in raw_rows]
    # 过滤掉代码为空的行（可能是汇总行）
    parsed = [p for p in parsed if p["代码"].strip()]

    if not parsed:
        # 打印原始 dump 供调试字段编号
        print(f"持仓解析: 代码字段为空，原始 row[0] keys={list(raw_rows[0].keys()) if raw_rows else []}", file=sys.stderr)
        if args.json:
            _json_out({"ok": False, "error": "parsed 0 rows with non-empty code",
                       "raw_count": len(raw_rows),
                       "raw_row0": raw_rows[0] if raw_rows else None})
        return

    if args.json:
        _json_out({"ok": True, "count": len(parsed), "source": source,
                   "send_detected": send_detected,
                   "rows": [{k: v for k, v in p.items() if k != "_raw"} for p in parsed]})
        return

    fmt = "{:<10} {:<10} {:>8} {:>8} {:>10} {:>14}"
    print(fmt.format("代码", "名称", "数量", "可用", "成本", "市值"))
    print(f"  (source={source} send_detected={send_detected})")
    print("-" * 64)
    for p in parsed:
        print(fmt.format(p["代码"], p["名称"], p["数量"], p["可用"], p["成本"], p["市值"]))


# ── 委托字段编号映射（从 TQT OnTradeRsponceNotify: 协议响应确认）────────────────
# 已确证（来自用户样本 6353269582 / 512890 卖出 100 @1.242 全部撤单）：
#   2113 = 合同号 (htbh)
#   2140 = 时间
#   2126 = 委托数量
#   2127 = 委托价格
#   2135 = 股东代码 (scdm)
# 推断（与 readOrders 已知映射一致）：
#   2096 = 时间（备用）
#   2102 = 股票代码 (stockcode)
#   2103 = 股票名称
#   4008 = 交易类型 jylx（买/卖方向）
#   2105 = 委托状态
#   2117 = 委托数量（备用）
#   2118 = 委托价格（备用）
#   2120 = 成交数量
#   2119 = 成交价格
#   2108 = mkcode（交易市场）
#   2106 = gdzh（股东账户）
_ORD_FIELD_TIME    = (2140, 2096)      # 时间
_ORD_FIELD_CODE    = (2102,)           # 股票代码
_ORD_FIELD_NAME    = (2103,)           # 名称
_ORD_FIELD_JYLX   = (4008,)           # 交易类型/方向
_ORD_FIELD_STATUS  = (2105,)           # 委托状态
_ORD_FIELD_QTY     = (2126, 2117)      # 委托数量
_ORD_FIELD_PRICE   = (2127, 2118)      # 委托价格
_ORD_FIELD_CVOL    = (2120,)           # 成交数量
_ORD_FIELD_CPRICE  = (2119,)           # 成交价格
_ORD_FIELD_HTBH    = (2113,)           # 合同号
_ORD_FIELD_MKCODE  = (2108,)           # 市场代码
_ORD_FIELD_SCDM    = (2135,)           # 股东代码
_ORD_FIELD_GDZH    = (2106,)           # 股东账户


def _parse_ord_row(row):
    """把协议委托行（数字 key 字典）解析成结构化字典。"""
    return {
        "时间":   _get_field(row, *_ORD_FIELD_TIME),
        "代码":   _get_field(row, *_ORD_FIELD_CODE),
        "名称":   _get_field(row, *_ORD_FIELD_NAME),
        "方向":   _get_field(row, *_ORD_FIELD_JYLX),
        "状态":   _get_field(row, *_ORD_FIELD_STATUS),
        "委托量": _get_field(row, *_ORD_FIELD_QTY),
        "委托价": _get_field(row, *_ORD_FIELD_PRICE),
        "成交量": _get_field(row, *_ORD_FIELD_CVOL),
        "成交价": _get_field(row, *_ORD_FIELD_CPRICE),
        "合同号": _get_field(row, *_ORD_FIELD_HTBH),
        "mkcode": _get_field(row, *_ORD_FIELD_MKCODE),
        "scdm":   _get_field(row, *_ORD_FIELD_SCDM),
        "gdzh":   _get_field(row, *_ORD_FIELD_GDZH),
        "_raw":   row,
    }


def frida_orders(args):
    """委托查询：dispatch req_type=0x603 → TQT hook 缓存 → 解析协议响应行。
    数据完全来自协议响应，不读 NSTableView / 视图树 / 屏幕。"""
    resp = _call("fetch_orders")
    result = _ok_or_die(resp)

    if not result.get("ok"):
        err = result.get("error", "unknown")
        if args.json:
            _json_out({"ok": False, "error": err,
                       "cache_summary": result.get("cache_summary"),
                       "send_detected": result.get("send_detected")})
        else:
            print(f"委托查询失败: {err}", file=sys.stderr)
            cs = result.get("cache_summary")
            if cs:
                for ntype, info in cs.items():
                    print(f"  cache nType={ntype} count={info['count']} keys={info['first_keys']}")
        return

    raw_rows = result.get("rows", [])
    source = result.get("source", "protocol_response")
    send_detected = result.get("send_detected")

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

    fmt = "{:<10} {:<8} {:<6} {:<4} {:<8} {:>6} {:>8} {:>8} {:<14}"
    print(fmt.format("时间", "代码", "名称", "方向", "状态", "委托量", "委托价", "成交价", "合同号"))
    print(f"  (source={source} send_detected={send_detected})")
    print("-" * 80)
    for p in parsed:
        print(fmt.format(p["时间"], p["代码"], p["名称"], p["方向"],
                         p["状态"], p["委托量"], p["委托价"], p["成交价"], p["合同号"]))


def frida_cancel(args):
    """撤单 dry-run：从协议响应（fetch_orders）按合同号反查撤单字段并打印。
    不发送任何请求，不读视图树。数据来源：协议响应。"""
    contract = args.contract

    resp = _call("fetch_orders")
    result = _ok_or_die(resp)

    if not result.get("ok"):
        err = result.get("error", "unknown")
        print(f"撤单失败: 委托查询错误: {err}", file=sys.stderr)
        if args.json:
            _json_out({"ok": False, "error": err})
        sys.exit(1)

    raw_rows = result.get("rows") or []
    if not raw_rows:
        print(f"撤单失败: 委托列表为空 (source={result.get('source')})", file=sys.stderr)
        if args.json:
            _json_out({"ok": False, "error": "orders empty"})
        sys.exit(1)

    # 在协议响应行中按合同号字段 2113 查找
    target_raw = None
    target_idx = None
    for idx, row in enumerate(raw_rows):
        htbh = _get_field(row, *_ORD_FIELD_HTBH)
        if htbh == contract:
            target_raw = row
            target_idx = idx
            break

    if target_raw is None:
        print(f"撤单失败: 委托列表中未找到合同号 {contract}", file=sys.stderr)
        if args.json:
            _json_out({"ok": False, "error": f"contract {contract} not found",
                       "row_count": len(raw_rows)})
        sys.exit(1)

    p = _parse_ord_row(target_raw)
    stockcode = p["代码"]
    jylx      = p["方向"]
    scdm      = p["scdm"]
    mkcode    = p["mkcode"]
    gdzh      = p["gdzh"]
    source    = result.get("source", "protocol_response")

    if not stockcode:
        print(f"警告: stockcode 为空，raw keys={list(target_raw.keys())}", file=sys.stderr)

    # 始终 dry-run（不真撤单）
    print("[DRY-RUN] 撤单字段（不会真的发送）:")
    print(f"  source:    {source}")
    print(f"  合同号:    {contract}")
    print(f"  req_type:  4  (nsInsid=0x186da)")
    print(f"  stockcode: {stockcode}")
    print(f"  jylx:      {jylx}")
    print(f"  scdm:      {scdm}")
    print(f"  mkcode:    {mkcode}")
    print(f"  gdzh:      {gdzh}")
    print(f"  row_index: {target_idx}")
    if args.json:
        _json_out({
            "ok": True, "dry_run": True, "source": source,
            "contract": contract,
            "req_type": 4, "nsInsid": "0x186da",
            "stockcode": stockcode, "jylx": jylx,
            "scdm": scdm, "mkcode": mkcode, "gdzh": gdzh,
            "row_index": target_idx,
            "raw_keys": list(target_raw.keys()),
        })


def frida_buy(args):
    resp = _call("buy", {"stockcode": args.code, "qty": str(args.qty), "price": str(args.price)})
    result = _ok_or_die(resp)
    if args.json:
        _json_out(result)
    else:
        print(f"买入  {result.get('stockcode')}  数量={result.get('qty')}  "
              f"价格={result.get('price')}  send_detected={result.get('send_detected')}")


def frida_sell(args):
    resp = _call("sell", {"stockcode": args.code, "qty": str(args.qty), "price": str(args.price)})
    result = _ok_or_die(resp)
    if args.json:
        _json_out(result)
    else:
        print(f"卖出  {result.get('stockcode')}  数量={result.get('qty')}  "
              f"价格={result.get('price')}  send_detected={result.get('send_detected')}")


# ── 主入口 ────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(prog="ths_cli", description="同花顺 CLI")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--engine", choices=["apple", "frida"], default="apple",
                   help="后端引擎 (默认: apple)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("positions", help="持仓查询")
    sub.add_parser("orders",    help="委托查询")
    sub.add_parser("account",   help="账户信息")

    cp = sub.add_parser("cancel", help="撤单")
    cp.add_argument("contract", help="合同号")
    cp.add_argument("--execute", action="store_true", help="真实执行撤单（默认 dry-run 只打印）")

    bp = sub.add_parser("buy", help="买入（走 trade.py）")
    bp.add_argument("code"); bp.add_argument("qty", type=int); bp.add_argument("price", type=float)

    sp = sub.add_parser("sell", help="卖出（走 trade.py）")
    sp.add_argument("code"); sp.add_argument("qty", type=int); sp.add_argument("price", type=float)

    # frida 兼容命令
    sub.add_parser("start",   help="启动桥进程 (frida)")
    sub.add_parser("stop",    help="停止桥进程 (frida)")
    sub.add_parser("status",  help="桥状态 (frida)")
    sub.add_parser("ping",    help="ping agent (frida)")

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
        frida_dispatch = {
            "positions": frida_positions,
            "orders":    frida_orders,
            "cancel":    frida_cancel,
            "buy":       frida_buy,
            "sell":       frida_sell,
        }
        if args.cmd in frida_dispatch:
            _ensure_bridge_running()
            # orders/cancel 用纯 cache 路径，不调任何 ObjC，跳过 state 检查避免死锁
            if args.cmd not in ("orders", "cancel"):
                _ensure_login_ready()
            frida_dispatch[args.cmd](args)
        elif args.cmd == "start":
            bridge = os.path.join(SCRIPT_DIR, "ths_bridge.py")
            venv_py = os.path.join(SCRIPT_DIR, ".venv/bin/python3")
            py = venv_py if os.path.isfile(venv_py) else sys.executable
            proc = subprocess.Popen(
                [py, bridge],
                stdout=open("/tmp/ths_bridge.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            print(f"桥已启动  pid={proc.pid}  log=/tmp/ths_bridge.log")
        elif args.cmd == "stop":
            subprocess.run(["pkill", "-f", "ths_bridge.py"], check=False)
            print("已发送 SIGTERM 给 ths_bridge.py")
        elif args.cmd == "status":
            resp = _call("state")
            result = _ok_or_die(resp)
            if args.json:
                _json_out(result)
            else:
                print(result)
        elif args.cmd == "ping":
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
