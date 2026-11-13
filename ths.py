#!/usr/bin/env python3
"""ths.py — 同花顺桥（/tmp/ths_bridge.sock）Python 客户端。

直连 UNIX socket，一行 JSON 请求 → 一行 JSON 响应；一个实例复用一条连接，
断线自动重连一次（桥进程没了就按 ths_cli 的方式拉起 ths_bridge.py）。
取数失败一律抛 ThsError，不静默返回空 list。

    from ths import Ths
    t = Ths()
    t.account(); t.positions(); t.orders(); t.trades()
    t.detail("6360169748"); t.cancel("6360169748")   # 撤单默认 dry-run
    t.close()
"""
import json
import os
import socket
import subprocess
import sys
import time

from ths_cli import (_get_field, _parse_ord_row, _parse_pos_row, _parse_trd_row,
                     _ORD_FIELD_HTBH, _ACCT_NUM_FIELDS, _cred_md5)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOCK_PATH = "/tmp/ths_bridge.sock"
BRIDGE_LOG = "/tmp/ths_bridge.log"
BRIDGE_START_TIMEOUT = 30.0

_TRD_FIELD_DATE = (2141,)        # 成交日期（委托行用 2139）
_PRIME_ERR = ("cache_empty_need_prime", "need_open_positions_ui",
              "need_open_orders_ui", "need_open_trades_ui")


class ThsError(Exception):
    """桥调用失败。method=出错的桥方法，error=桥返回的原始错误，extra=附带明细。"""

    def __init__(self, method, error, result=None, **extra):
        self.method = method
        self.error = error
        self.result = result
        self.extra = extra
        msg = f"{method} 失败: {error}"
        if extra:
            msg += "  " + json.dumps(extra, ensure_ascii=False, default=str)
        super().__init__(msg)


def _norm_date(d):
    return str(d).replace("-", "").strip()


class Ths:
    def __init__(self, sock_path=SOCK_PATH, timeout=35.0):
        self.sock_path = sock_path
        self.timeout = timeout
        self._sock = None
        self._req_id = 0
        self._ensure_bridge()

    # ── 连接 ────────────────────────────────────────────────────────────────
    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.sock_path)
        except OSError:
            s.close()
            raise
        return s

    def _probe(self):
        try:
            self._connect().close()
            return True
        except OSError:
            return False

    def _ensure_bridge(self):
        if self._probe():
            return
        self._start_bridge()
        deadline = time.time() + BRIDGE_START_TIMEOUT
        while time.time() < deadline:
            if self._probe():
                return
            time.sleep(0.5)
        raise ThsError("start_bridge", f"桥启动超时（{BRIDGE_START_TIMEOUT:.0f}s）")

    def _start_bridge(self):
        py = os.path.join(SCRIPT_DIR, ".venv/bin/python3")
        if not os.path.isfile(py):
            py = sys.executable
        cmd = [py, os.path.join(SCRIPT_DIR, "ths_bridge.py")]
        # 不指定 --pid、不跳过 prime：桥自己保证「先杀重开 App → 装钩子 → 建窗口
        # → 拿到下单面板实例」，这是下单接口能用的前提。
        subprocess.Popen(cmd, stdout=open(BRIDGE_LOG, "a"),
                         stderr=subprocess.STDOUT, start_new_session=True)

    def _drop(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self):
        self._drop()

    # ── 请求 ────────────────────────────────────────────────────────────────
    def _roundtrip(self, method, params):
        sock = self._sock or self._connect()
        self._sock = sock
        self._req_id += 1
        req = {"id": self._req_id, "method": method, "params": params or {}}
        sock.sendall(json.dumps(req, ensure_ascii=False).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionResetError("桥关闭了连接")
            buf += chunk
        resp = json.loads(buf.split(b"\n", 1)[0])
        if resp.get("id") != self._req_id:
            raise ValueError(f"响应 id 不匹配: {resp.get('id')} != {self._req_id}")
        if not resp.get("ok"):
            res = resp.get("result")
            err = res.get("error") if isinstance(res, dict) else res
            raise ThsError(method, err or "unknown bridge error", result=res)
        return resp.get("result") or {}

    def _call(self, method, params=None):
        """发一行 JSON；传输层失败重连一次（桥没了就重启桥）。"""
        last = None
        for attempt in (0, 1):
            try:
                return self._roundtrip(method, params)
            except (OSError, ValueError) as e:
                last = e
                self._drop()
                if attempt == 0:
                    self._ensure_bridge()
        raise ThsError(method, f"桥连接失败: {last!r}")

    # ── 取数（缓存空则程序化补抓，不点界面）────────────────────────────────
    def _prime(self, cat):
        try:
            self._call("ensure_query_templates", {"types": [cat]})
        except ThsError:
            return False
        deadline = time.time() + 12.0
        while time.time() < deadline:
            st = self._call("get_cache_status")
            if (st.get(cat) or {}).get("count", 0) > 0:
                return True
            time.sleep(0.5)
        return False

    def _read(self, method, cat, params=None):
        res = self._call(method, params)
        if not res.get("ok"):
            if res.get("error") in _PRIME_ERR:
                self._prime(cat)
                res = self._call(method, params)
            if not res.get("ok"):
                raise ThsError(method, res.get("error") or "空响应", result=res)
        return res

    # ── 状态 / 登录 ─────────────────────────────────────────────────────────
    def state(self):
        return self._call("state")

    def login(self):
        """确保模拟仓为 active 且资金已回填。返回 state dict。

        主路径 agent.loginMoni（与「交易→模拟」面板 OK 按钮同一条链），
        锁屏 / 无窗口也能完成；失败再退 loginSim，最后退 HXConnection。
        """
        st = self.state()
        if st.get("active_is_moni") and st.get("active_trade_commu_model"):
            return st

        account, pw_md5 = _cred_md5()
        if account and pw_md5:
            r = self._call("loginMoni", {"account": account, "password": pw_md5})
            if r.get("ok") and r.get("active_is_moni"):
                return self.state()
            err = r.get("error") or "active_is_moni=False"
        else:
            err = "no_credential"

        ls = self._call("loginSim")
        if ls.get("active_is_moni"):
            return self.state()

        if account:
            r = self._call("login", {"account": account, "password": pw_md5,
                                     "loginType": 0})
            if r.get("dispatched"):
                self._sleep_until_active(10.0)

        st = self.state()
        if not st.get("active_is_moni"):
            raise ThsError("login", f"loginMoni/loginSim/login 均未成功（loginMoni: {err}）",
                           result=st)
        return st

    def _sleep_until_active(self, wait):
        deadline = time.time() + wait
        while time.time() < deadline:
            st = self.state()
            if st.get("active_is_moni") and st.get("active_trade_commu_model"):
                return st
            time.sleep(1.0)
        return None

    def ensure_login(self):
        """幂等：active_is_moni 为 False 时才登录。"""
        st = self.state()
        if st.get("active_is_moni"):
            return st
        return self.login()

    # ── 数据 ────────────────────────────────────────────────────────────────
    def account(self):
        res = self._call("read_account_direct")
        if not res.get("ok"):
            raise ThsError("read_account_direct", res.get("error") or "无账户数据",
                           result=res)
        out = {}
        for k in _ACCT_NUM_FIELDS:
            v = str(res.get(k) or "").strip().replace(",", "")
            try:
                out[k] = float(v) if v else 0.0
            except ValueError:
                raise ThsError("read_account_direct", f"字段 {k} 不是数字: {v!r}",
                               result=res)
        out["资金账号"] = str(res.get("资金账号") or "").strip()
        out["is_moni"] = res.get("is_moni")
        return out

    def positions(self):
        res = self._read("read_positions", "positions")
        raw = res.get("rows") or []
        parsed = [p for p in (_parse_pos_row(r) for r in raw) if p["代码"].strip()]
        if raw and not parsed:
            raise ThsError("read_positions", "解析后代码为空", result=res)
        return [{k: v for k, v in p.items() if k != "_raw"} for p in parsed]

    def orders(self, date=None):
        params = None
        if date:
            d = _norm_date(date)
            params = {"beginDate": d, "endDate": d}
        res = self._read("read_orders", "orders", params)
        raw = res.get("rows") or []
        parsed = [_parse_ord_row(r) for r in raw]
        if date:
            d = _norm_date(date)
            hit = [p for p in parsed if p["日期"] == d]
            if parsed and not hit:
                raise ThsError("read_orders", f"桥缓存里没有 {d} 的委托",
                               result={"cache_dates": sorted({p["日期"] for p in parsed})})
            parsed = hit
        return [{k: v for k, v in p.items() if k != "_raw"} for p in parsed]

    def trades(self, date=None):
        params = None
        if date:
            d = _norm_date(date)
            params = {"beginDate": d, "endDate": d}
        res = self._read("read_trades", "trades", params)
        raw = res.get("rows") or []
        parsed = [_parse_trd_row(r) for r in raw]
        for p, r in zip(parsed, raw):
            p["日期"] = _get_field(r, *_TRD_FIELD_DATE)
        if date:
            d = _norm_date(date)
            hit = [p for p in parsed if p["日期"] == d]
            if parsed and not hit:
                raise ThsError("read_trades", f"桥缓存里没有 {d} 的成交",
                               result={"cache_dates": sorted({p["日期"] for p in parsed})})
            parsed = hit
        return [{k: v for k, v in p.items() if k != "_raw"} for p in parsed]

    def _find_order(self, contract):
        res = self._read("read_orders", "orders")
        raw = res.get("rows") or []
        for idx, row in enumerate(raw):
            if _get_field(row, *_ORD_FIELD_HTBH) == str(contract):
                return idx, row, res
        raise ThsError("read_orders", f"委托缓存里找不到合同号 {contract}",
                       result={"row_count": len(raw)})

    def detail(self, contract):
        _, row, _ = self._find_order(contract)
        p = _parse_ord_row(row)
        return {k: v for k, v in p.items() if k != "_raw"}

    def cancel(self, contract, real=False):
        """按合同号定位委托行下标，调 App 自己的
        -[TradeQueryTemplate sendChedanRequestByTableIndex:]。

        real=False（默认）：只 probe 校验，不发请求，返回将发送的字段。
        real=True：真发，随后带日期强制重查委托表（撤单实测 15~25s 才反映），
        状态没变就换实例/重发，最多若干次；都没变抛 ThsError。
        """
        contract = str(contract)
        row_index, row, res = self._find_order(contract)
        p = _parse_ord_row(row)
        probe = self._call("send_chedan_by_index",
                           {"contract": contract, "probe": True})
        if not probe.get("ok"):
            raise ThsError("send_chedan_by_index", probe.get("error") or "probe 失败",
                           result=probe)

        info = {
            "contract": contract,
            "contract_field": 2135,
            "row_index": row_index,
            "row_count": len(res.get("rows") or []),
            "stockcode": p["代码"],
            "name": p["名称"],
            "action": p["方向"],
            "qty": p["委托量"],
            "price": p["委托价"],
            "status": p["状态"],
            "market": p["市场"],
            "gdzh": p["股东账户"],
            "attr": _get_field(row, 4008),
            "probe": probe,
        }
        if not real:
            return {"dry_run": True, **info}
        if ("撤" in p["状态"]) or ("废" in p["状态"]):
            return {"dry_run": False, **info, "old_status": p["状态"],
                    "new_status": p["状态"], "confirmed_cancelled": True,
                    "note": "委托已是撤单/废单状态，无需再撤"}

        # 撤单：委托表可能有多个 TQT 实例，逐个试；每次发完强制重查，20s 观察窗
        sent = None
        new_status = p["状态"]
        n_cand = len(probe.get("candidates") or []) or 1
        for attempt in range(1, n_cand * 2 + 1):
            pick = (attempt - 1) % n_cand
            r = self._call("send_chedan_by_index", {"contract": contract, "candidate": pick})
            if not r.get("ok"):
                raise ThsError("send_chedan_by_index", r.get("error") or "撤单调用失败",
                               result=r)
            sent = r
            deadline = time.time() + 20
            today = time.strftime("%Y-%m-%d")
            while time.time() < deadline:
                time.sleep(4)
                res2 = self._call("read_orders", {"beginDate": today, "endDate": today})
                new_status = None
                for row2 in res2.get("rows") or []:
                    if _get_field(row2, *_ORD_FIELD_HTBH) == contract:
                        new_status = _get_field(row2, 2105)
                        break
                if new_status != p["状态"]:
                    break
            if new_status != p["状态"]:
                break

        out = {"dry_run": False, "sent": sent, "attempts": attempt, **info,
               "old_status": p["状态"], "new_status": new_status}
        if new_status is None:
            out["confirmed_cancelled"] = True
            out["note"] = "已从委托表移除"
            return out
        if new_status == p["状态"]:
            raise ThsError("cancel", f"撤单后状态未变: {new_status}", result=out)
        out["confirmed_cancelled"] = ("撤" in new_status) or ("废" in new_status)
        return out

    # ── 下单（接口化，不走 UI 自动化）────────────────────────────────────────
    # agent 侧 placeOrder 优先调 App 自己的 -[JYMaiMaiView sendWeituoRequest]，
    # 它自己填 TradeRequest、自己 getInstanceID、自己派发给 active user。
    # 我们只填「代码/方向/价格/数量」四个业务字段。
    _PRIME_SCRIPT = '''
tell application "System Events"
    set theProcs to processes whose name is "同花顺"
    if theProcs is {} then return "no proc"
    set theProc to item 1 of theProcs
    set frontmost of theProc to true
    delay 1
    keystroke "n" using command down
    delay 3
    set winCount to count of windows of theProc
    repeat with wIdx from winCount to 1 by -1
        set w to window wIdx of theProc
        try
            click button 6 of w
            delay 3
            try
                click button "模拟" of w
            end try
            return "primed"
        end try
    end repeat
    return "no button6"
end tell
'''

    def _ensure_view(self, tries=3):
        """确保 agent 拿到了下单面板（JYMaiMaiView）实例。

        该实例只在窗口/交易页创建时产生，agent 的钩子必须先装好；
        这里没有就直接开一个新窗口点交易页，让 App 自己创建出来。
        """
        for _ in range(tries):
            if self.state().get("mai_mai_view"):
                return True
            subprocess.run(["osascript", "-e", self._PRIME_SCRIPT],
                           capture_output=True, text=True)
            time.sleep(2)
        return bool(self.state().get("mai_mai_view"))

    def place(self, code, side, shares, price, wait=10.0):
        """下单并核验委托表。返回 {ok, contract, send_detected, status, via, raw}。"""
        side = str(side).lower()
        if side not in ("buy", "sell"):
            raise ValueError("side 必须是 buy / sell")
        price = f"{float(price):.3f}".rstrip("0").rstrip(".")
        shares = str(int(shares))

        self._ensure_view()
        before = {p["合同号"] for p in self.orders()}
        r = self._call("place_order", {"code": str(code), "side": side,
                                       "price": price, "shares": shares})
        if not r.get("ok"):
            raise ThsError("place_order", r.get("error") or "下单调用失败", result=r)

        want_dir = "买入" if side == "buy" else "卖出"
        deadline = time.time() + wait
        hit = None
        while time.time() < deadline:
            time.sleep(1.0)
            for p in self.orders():
                if p["合同号"] in before:
                    continue
                if p["代码"] != str(code) or p["方向"] != want_dir:
                    continue
                try:
                    if int(p["委托量"]) == int(shares) and abs(float(p["委托价"]) - float(price)) < 1e-6:
                        hit = p
                        break
                except (TypeError, ValueError):
                    continue
            if hit:
                break
        if not hit:
            raise ThsError("place_order",
                           f"已发送但委托表未出现该单（{code} {want_dir} {shares}@{price}）",
                           result=r)
        return {"ok": True, "contract": hit["合同号"], "status": hit["状态"],
                "side": side, "code": str(code), "shares": int(shares), "price": price,
                "send_detected": r.get("send_detected"), "via": r.get("via") or r.get("method"),
                "raw": r}

    def buy(self, code, shares, price, wait=10.0):
        return self.place(code, "buy", shares, price, wait=wait)

    def sell(self, code, shares, price, wait=10.0):
        return self.place(code, "sell", shares, price, wait=wait)
