#!/usr/bin/env python3
"""
固化的下单工具 — 所有买卖操作统一接口
用法:
  python3 trade.py sync                          # 同步持仓到 portfolio.json
  python3 trade.py buy CODE SHARES PRICE         # 买入（持仓增量确认）
  python3 trade.py sell CODE SHARES PRICE        # 卖出（持仓增量确认）
  python3 trade.py t0_buy CODE SHARES PRICE [PAIR_PRICE]  # 做T买入配对：买入+高位卖出挂单
  python3 trade.py t0_sell CODE SHARES PRICE [PAIR_PRICE] # 做T卖出配对：卖出+低位买入挂单
  python3 trade.py revoke CODE 买入              # 撤同方向未成交委托
  python3 trade.py revoke CODE 卖出              # 撤同方向未成交委托

铁律:
  1. 下单前查持仓 → 下单 → 下单后查持仓确认增量（不查委托）
  2. 增量 = 目标 → 成功。增量 ≠ 目标 → 报错，不重试
  3. 每次新建 EvolvingSim 实例，不跨操作复用
  4. AI Agent 只能通过 python3 trade.py 执行买卖，不得直接调 EvolvingSim
  5. 买入限价 ≤ 当前价，卖出限价 ≥ 当前价
  6. 做T配对：先撤同方向未成交单，再挂主单+配对单
  7. 仅在连续竞价时段(9:30-11:30,13:00-15:00)执行挂单
  8. 调用 EvolvingSim 前自动清理 /tmp/lock.json
  9. EvolvingSim 调用有 15 秒超时保护，超时后自动清理锁+僵尸进程+重建实例
 10. 进程级 fcntl.flock 互斥锁（/tmp/evolvingsim_trade.lock）防止并发调EvolvingSim
 11. pkill 只杀孤儿/僵尸 osascript（ppid=1 或 Z 状态），不影响正常运行的 osascript
"""
import sys, os, json, time, urllib.request
import subprocess
import fcntl

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PF_PATH = os.path.join(SCRIPT_DIR, 'portfolio.json')

from evolving.evolving import EvolvingSim
from tracker import get_code_map

CODE_MAP = {code: info["sid"] for code, info in get_code_map().items()}


# ─── 内部工具 ──────────────────────────────────────────────────────────

LOCK_PATH = '/tmp/lock.json'
TRADE_LOCK_PATH = '/tmp/evolvingsim_trade.lock'  # 进程级互斥锁，防止并发调EvolvingSim
EVOLVING_TIMEOUT = 15  # EvolvingSim 单次调用超时秒数（buy/sell 需 3-8s，给足余量）

DEBUG = os.environ.get('TRADE_DEBUG', '') == '1'


def _dbg(msg):
    """调试日志前缀 [DBG]"""
    ts = time.strftime('%H:%M:%S')
    print(f"[DBG {ts}] {msg}")


def _osascript_count():
    """返回当前 osascript 进程数"""
    try:
        # macOS pgrep 不支持 -c，改用 ps + wc
        r = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=2)
        count = r.stdout.count('osascript') - 1  # 减掉 pgrep 自身的那行
        return max(0, count)
    except Exception:
        return -1


def _lock_exists():
    """检查锁文件是否存在"""
    return os.path.exists(LOCK_PATH)


def _kill_osascript_zombies():
    """只杀死僵尸/孤儿 osascript 进程（ppid=1 或状态 Z），不影响正在运行的正常 osascript"""
    pre_count = _osascript_count()
    zombie_pids = []
    try:
        # 用 ps 找出所有 osascript 进程的 PID/PPID/状态
        result = subprocess.run(
            ['ps', '-eo', 'pid,ppid,state,command'],
            capture_output=True, text=True, timeout=3
        )
        for line in result.stdout.split('\n'):
            if 'osascript' in line:
                parts = line.split()
                if len(parts) >= 3:
                    pid = parts[0]
                    ppid = parts[1]
                    state = parts[2]
                    # 孤儿进程（ppid=1，父进程已被 kill）或 僵尸状态（Z）
                    if ppid == '1' or state.startswith('Z'):
                        zombie_pids.append(pid)
        if zombie_pids:
            subprocess.run(['kill', '-9'] + zombie_pids, capture_output=True, timeout=3)
    except Exception:
        pass
    if DEBUG:
        post_count = _osascript_count()
        _dbg(f"_kill_osascript_zombies: osascript {pre_count}→{post_count} (zombies found: {len(zombie_pids)})")


def _clean_lock():
    """清理 EvolvingSim 的锁文件，避免异常退出后残留导致 15s 阻塞"""
    existed = os.path.exists(LOCK_PATH)
    if existed:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass
    if DEBUG and existed:
        _dbg(f"_clean_lock: removed /tmp/lock.json")


_TRADE_LOCK_FD = None  # 进程级锁文件描述符（单例）


def _acquire_trade_lock(timeout=20):
    """
    获取进程级互斥锁（fcntl.flock），确保同一时间只有一个进程调用 EvolvingSim。
    解决 EvolvingSim Lock.requestLock 的 TOCTOU 竞态条件。
    同时清理 EvolvingSim 的 lock.json，确保新实例 __init__ 的 unlock() 不会
    意外释放其他进程的锁。
    """
    global _TRADE_LOCK_FD
    try:
        fd = os.open(TRADE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)  # 阻塞等待排他锁
        _TRADE_LOCK_FD = fd
        # 持有锁后清理 EvolvingSim 的 lock.json（此时没有其他进程用 EvolvingSim）
        _clean_lock()
        return True
    except Exception as e:
        if DEBUG:
            _dbg(f"_acquire_trade_lock FAILED: {e}")
        return False


def _release_trade_lock():
    """释放进程级互斥锁"""
    global _TRADE_LOCK_FD
    if _TRADE_LOCK_FD is not None:
        try:
            fcntl.flock(_TRADE_LOCK_FD, fcntl.LOCK_UN)
            os.close(_TRADE_LOCK_FD)
        except Exception:
            pass
        finally:
            _TRADE_LOCK_FD = None


def _call_evolving(method_name, *args, timeout=None):
    """
    在子进程中调用 EvolvingSim 方法，带超时保护。

    原理：EvolvingSim 底层通过 os.popen 调用 AppleScript 操作同花顺。
    如果 AppleScript 卡住（弹窗/焦点丢失），os.popen 会同步阻塞不返回。
    本函数将 EvolvingSim 调用放进子进程，超时后杀掉子进程并清理残留。

    Args:
        method_name: EvolvingSim 方法名（如 'getHoldingShares', 'buy', 'sell'）
        *args: 传递给方法的参数
        timeout: 超时秒数，默认 EVOLVING_TIMEOUT (8s)

    Returns:
        EvolvingSim 方法的返回值（dict/tuple），失败返回 None
    """
    if timeout is None:
        timeout = EVOLVING_TIMEOUT

    # 获取进程级互斥锁（解决 EvolvingSim Lock TOCTOU 竞态 + __init__ unlock 问题）
    if not _acquire_trade_lock():
        print(f"  ⚠️ 无法获取进程锁，跳过 EvolvingSim.{method_name}")
        return None

    # 构建在子进程中执行的 Python 脚本
    # 注意：使用 sys.executable 确保使用 venv 中的 Python
    args_repr = ", ".join(repr(a) for a in args)
    code = (
        f"import sys, json; "
        f"sys.path.insert(0, {SCRIPT_DIR!r}); "
        f"from evolving.evolving import EvolvingSim; "
        f"e = EvolvingSim(); "
        f"result = e.{method_name}({args_repr}); "
        f"print(json.dumps(result, ensure_ascii=False, default=str))"
    )

    if DEBUG:
        _dbg(f"_call_evolving({method_name}, {args_repr}) timeout={timeout}s "
             f"osascript={_osascript_count()} lock={'YES' if _lock_exists() else 'NO'}")

    start = time.time()
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, '-c', code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=SCRIPT_DIR,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        elapsed = time.time() - start
        returncode = proc.returncode
        if returncode == 0 and stdout.strip():
            if DEBUG:
                _dbg(f"_call_evolving({method_name}) OK ({elapsed:.2f}s) "
                     f"osascript={_osascript_count()} lock={'YES' if _lock_exists() else 'NO'}")
            return json.loads(stdout.strip())
        else:
            stderr_preview = (stderr or '').strip()[:200]
            if DEBUG:
                _dbg(f"_call_evolving({method_name}) FAIL ({elapsed:.2f}s) rc={returncode} "
                     f"stderr={stderr_preview[:100]}")
            print(f"  ⚠️ EvolvingSim.{method_name} 执行失败: {stderr_preview}")
            return None
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        if DEBUG:
            _dbg(f"_call_evolving({method_name}) TIMEOUT ({elapsed:.2f}s) "
                 f"osascript_before_kill={_osascript_count()} lock={'YES' if _lock_exists() else 'NO'}")
        print(f"  ⚠️ EvolvingSim.{method_name} 超时 ({timeout}s)，强制清理...")
        # 显式杀掉子进程，确保残留的 osascript 也被清理
        if proc is not None and proc.pid:
            try:
                proc.kill()
                proc.wait(timeout=3)
                if DEBUG:
                    _dbg(f"_call_evolving({method_name}) killed pid={proc.pid}")
            except Exception as ex:
                if DEBUG:
                    _dbg(f"_call_evolving({method_name}) kill error: {ex}")
        _clean_lock()
        _kill_osascript_zombies()
        time.sleep(1)
        if DEBUG:
            _dbg(f"_call_evolving({method_name}) after_cleanup "
                 f"osascript={_osascript_count()} lock={'YES' if _lock_exists() else 'NO'}")
        return None
    except json.JSONDecodeError:
        elapsed = time.time() - start
        if DEBUG:
            _dbg(f"_call_evolving({method_name}) JSON_DECODE_ERROR ({elapsed:.2f}s)")
        print(f"  ⚠️ EvolvingSim.{method_name} 返回数据解析失败")
        _clean_lock()
        return None
    except Exception as e:
        elapsed = time.time() - start
        if DEBUG:
            _dbg(f"_call_evolving({method_name}) EXCEPTION ({elapsed:.2f}s): {e}")
        print(f"  ⚠️ EvolvingSim.{method_name} 异常: {e}")
        _clean_lock()
        return None
    finally:
        _release_trade_lock()


def _get_position(code):
    """返回 (shares, avg_cost) 或 (0, 0)"""
    if os.path.exists(PF_PATH):
        with open(PF_PATH) as f:
            pf = json.load(f)
        pos = pf.get('positions', {}).get(code, {})
        return pos.get('shares', 0), pos.get('avg_cost', 0.0)
    return 0, 0.0


def _activate_tonghuashun():
    """激活同花顺窗口，解决 AppleScript 底层间歇断连"""
    os.system("osascript -e 'tell application \"同花顺\" to activate'")


def _retry_get_holding_shares(max_retries=3, delay=2):
    """带重试+超时保护+僵尸清理的 getHoldingShares"""
    if DEBUG:
        _dbg(f"_retry_get_holding_shares START (max_retries={max_retries}) "
             f"osascript={_osascript_count()} lock={'YES' if _lock_exists() else 'NO'}")
    for attempt in range(1, max_retries + 1):
        # 每次重试前先激活窗口 + 清理僵尸 osascript 进程，确保无残留进程占用锁
        _activate_tonghuashun()
        _kill_osascript_zombies()
        _clean_lock()

        if DEBUG:
            _dbg(f"_retry_get_holding_shares attempt={attempt}/{max_retries} "
                 f"osascript={_osascript_count()} lock={'YES' if _lock_exists() else 'NO'}")

        h = _call_evolving('getHoldingShares')
        if isinstance(h, dict) and h.get('status'):
            if DEBUG:
                _dbg(f"_retry_get_holding_shares SUCCESS attempt={attempt}")
            return h

        if attempt < max_retries:
            print(f"⚠️ EvolvingSim 连接失败，{delay}秒后重试 ({attempt}/{max_retries})")
            time.sleep(delay)

    if DEBUG:
        _dbg(f"_retry_get_holding_shares ALL_FAILED osascript={_osascript_count()} lock={'YES' if _lock_exists() else 'NO'}")
    raise RuntimeError("❌ 连接同花顺失败，请确认同花顺在模拟交易界面")


def _get_holding_shares(code=None):
    """
    查 EvolvingSim 持仓（带重试+窗口激活）。
    返回 dict {code: shares} 或 (若 code 指定) 单只数量。
    """
    h = _retry_get_holding_shares()
    result = {}
    for row in h.get('data', []):
        if row and len(row) >= 7:
            result[row[0]] = int(row[6])
    if code:
        return result.get(code, 0)
    return result


def _get_price(code):
    """获取当前市价（来自 EvolvingSim 持仓数据）"""
    _clean_lock()
    h = _call_evolving('getHoldingShares')
    if not (isinstance(h, dict) and h.get('status')):
        return None
    for row in h.get('data', []):
        if row and len(row) >= 3 and row[0] == code:
            return float(row[2])
    return None


# ─── sync ──────────────────────────────────────────────────────────────

def _retry_get_account_info(max_retries=3, delay=2):
    """带重试+窗口激活的 getAccountInfo"""
    for attempt in range(1, max_retries + 1):
        _activate_tonghuashun()
        time.sleep(0.5)
        _clean_lock()
        acct = _call_evolving('getAccountInfo')
        if isinstance(acct, dict) and acct.get('status'):
            return acct
        if attempt < max_retries:
            print(f"⚠️ getAccountInfo 失败，{delay}秒后重试 ({attempt}/{max_retries})")
            time.sleep(delay)
    print("⚠️ 获取账户信息失败")
    return None


def sync():
    """同步持仓到 portfolio.json（带重试+窗口激活）"""
    h = _retry_get_holding_shares()
    acct = _retry_get_account_info()

    if os.path.exists(PF_PATH):
        with open(PF_PATH) as f:
            pf = json.load(f)
    else:
        pf = {'positions': {}, 'account': {}}

    # 用 tracker 标准名称覆盖 EvolvingSim 返回的截断名
    code_map = get_code_map()

    for row in h.get('data', []):
        if not row or len(row) < 12:
            continue
        code = row[0]
        shares_val = int(row[6])
        cost = float(row[10])
        if shares_val > 0:
            cur_price = float(row[2])
            name = code_map.get(code, {}).get('name') or row[1]
            pf['positions'][code] = {
                'name': name,
                'shares': shares_val,
                'avg_cost': cost,
                'current_price': cur_price,
                'market_price': cur_price,
                'market_value': float(row[11]),
                'pnl': float(row[3]),
                'pnl_pct': round(float(row[3]) / (cost * shares_val) * 100, 2) if cost > 0 else 0,
            }
        elif code in pf['positions']:
            del pf['positions'][code]

    if isinstance(acct, dict) and acct.get('status'):
        d = acct['data']
        pf['account'] = {
            'total_asset': float(d['总资产']),
            'cash': float(d['可用金额']),
            'market_value': float(d['总市值']),
        }
    pf['last_updated'] = time.strftime('%Y-%m-%d %H:%M')

    # 4. 同步 _signal_state
    ss = pf.get('_signal_state', {})
    active_codes = set()
    for row in h.get('data', []):
        if not row or len(row) < 12:
            continue
        code = row[0]
        shares_val = int(row[6])
        if shares_val > 0:
            active_codes.add(code)
            if code in ss:
                ss[code]['shares'] = shares_val
                ss[code]['avg_cost'] = float(row[10])
                ss[code]['current_price'] = float(row[2])
            # code 不在 _signal_state 中的情况不处理（signal_generator 负责管理）

    # 清除已清仓的 code（同花顺里没有了但 _signal_state 里还有）
    for code in list(ss.keys()):
        if code not in active_codes:
            del ss[code]

    pf['_signal_state'] = ss

    with open(PF_PATH, 'w') as f:
        json.dump(pf, f, ensure_ascii=False, indent=2)

    print(f"✅ 同步: {len(pf['positions'])}只 | 总资产{pf['account']['total_asset']:.0f}")


# ─── 持仓增量确认下单 ────────────────────────────────────────────────

def _trade_with_confirmation(action, code, shares, price):
    """
    通用下单 + 持仓增量确认。
    action: 'buy' | 'sell'
    返回 (success, before_shares, after_shares)
    """
    if DEBUG:
        _dbg(f"_trade_with_confirmation START action={action} code={code} shares={shares} price={price} "
             f"osascript={_osascript_count()} lock={'YES' if _lock_exists() else 'NO'}")

    # 1. 查当前持仓
    holding_map = _get_holding_shares()
    before = holding_map.get(code, 0)

    if DEBUG:
        _dbg(f"_trade_with_confirmation before={before} code={code}")

    # 2. 下单（新实例，带超时保护）
    _clean_lock()
    result = _call_evolving(action, code, shares, price)
    if result is None:
        print(f"❌ {action} {code} {shares}股@{price} → 下单超时/失败")
        if DEBUG:
            _dbg(f"_trade_with_confirmation RESULT=None osascript={_osascript_count()} lock={'YES' if _lock_exists() else 'NO'}")
        return False, before, before

    ok, contract = result

    if not ok:
        print(f"❌ {action} {code} {shares}股@{price} → 下单失败: {contract}")
        if DEBUG:
            _dbg(f"_trade_with_confirmation result NOT OK: ({ok}, {contract})")
        return False, before, before

    # 3. 等一等让成交生效
    time.sleep(1.5)

    # 4. 持仓增量确认
    if DEBUG:
        _dbg(f"_trade_with_confirmation confirming position change...")
    holding_map2 = _get_holding_shares()
    after = holding_map2.get(code, 0)

    if action == 'buy':
        delta = after - before
        expected = shares
    else:
        delta = before - after
        expected = shares

    if DEBUG:
        _dbg(f"_trade_with_confirmation after={after} delta={delta} expected={expected}")

    if delta == expected:
        # 5. 成交确认 → 同步 portfolio
        sync()
        label = '买入' if action == 'buy' else '卖出'
        print(f"✅ {label} {code} {shares}股@{price} → 持仓从{before}→{after}股 合同:{contract}")
        return True, before, after
    else:
        label = '买入' if action == 'buy' else '卖出'
        print(f"❌ {label} {code} {shares}股@{price} → 下单成功但持仓未变（{before}→{after}），"
              f"期望增量{expected}实际增量{delta}，可能未成交")
        return False, before, after


# ─── buy / sell ────────────────────────────────────────────────────────

def do_buy(code, shares, price):
    return _trade_with_confirmation('buy', code, shares, price)


def do_sell(code, shares, price):
    return _trade_with_confirmation('sell', code, shares, price)


# ─── T0 做T配对 ───────────────────────────────────────────────────────

def _is_continuous_auction():
    """连续竞价时段: 9:30-11:30, 13:00-15:00"""
    now = time.localtime()
    t = now.tm_hour * 60 + now.tm_min
    return (570 <= t <= 690) or (780 <= t <= 900)  # 9:30=570, 11:30=690, 13:00=780, 15:00=900


# ─── 5分钟ATR获取（从腾讯接口拉取5分钟K线计算） ──────────────────────

def _calc_atr(klines, period=14):
    """计算ATR（基于K线列表），与 signal_generator 的 calc_atr 一致"""
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        k_prev, k_curr = klines[i - 1], klines[i]
        tr = max(k_curr["high"] - k_curr["low"],
                 abs(k_curr["high"] - k_prev["close"]),
                 abs(k_curr["low"] - k_prev["close"]))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return round(atr, 4)


def _fetch_klines_5min(code):
    """从腾讯接口获取5分钟K线，返回 [{\"time\",\"open\",\"close\",\"high\",\"low\",\"volume\",\"amount\"}, ...]"""
    sid = CODE_MAP.get(code, "")
    if not sid:
        return []

    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sid},m5,,48"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        stock_data = raw.get("data", {})
        qfq_key = None
        for key in (sid, sid.lower(), sid.upper()):
            if key in stock_data:
                qfq_key = key
                break
        if not qfq_key:
            return []

        kline_list = stock_data[qfq_key].get("m5") or stock_data[qfq_key].get("m5", [])
        if not kline_list:
            return []

        klines = []
        for item in kline_list:
            if len(item) < 6:
                continue
            try:
                o, c, h, l = float(item[1]), float(item[2]), float(item[3]), float(item[4])
                v = float(item[5])
                amt = float(item[7]) if len(item) > 7 else 0
                klines.append({
                    "time": item[0],
                    "open": o, "close": c, "high": h, "low": l,
                    "volume": v, "amount": amt,
                })
            except (ValueError, IndexError):
                continue
        return klines
    except Exception as e:
        print(f"[WARN] {code} 5分钟K线获取失败: {e}")
        return []


def _estimate_atr(code, price=None):
    """
    获取5分钟ATR（从腾讯接口拉取5分钟K线计算）。
    如果5分钟数据不可用，降级为价格×2%的粗估。
    """
    if price is None:
        price = _get_price(code) or 0

    klines_5min = _fetch_klines_5min(code)
    if klines_5min:
        atr = _calc_atr(klines_5min, 14)
        if atr is not None:
            return atr

    # 降级：价格×2%粗估
    print(f"[WARN] {code} 5分钟ATR不可用，降级为价格×2%粗估")
    return round(price * 0.02, 3)


def do_t0_buy(code, shares, price, pair_price=None):
    """
    做T买入配对：
      1. 检查连续竞价时段
      2. 撤同方向(买入)未成交委托
      3. 主单：买入 shares@price（限价≤当前价）
      4. 配对：挂卖出单 shares@pair_price（限价≥当前价，ATR×2）
    配对失败不影响主单结果。
    """
    if not _is_continuous_auction():
        print(f"❌ 非连续竞价时段(9:30-11:30,13:00-15:00)，不执行挂单")
        return False

    # 撤同方向未成交委托(买入方向)
    do_revoke(code, '买入')

    # 获取当前市价用于限价检查
    cur_price = _get_price(code) or price

    # 买入限价: ≤ 当前价
    buy_limit = min(price, cur_price)
    if buy_limit != price:
        print(f"   ⚠️ 买入限价调整: {price} → {buy_limit} (≤当前价{cur_price})")

    # 配对价格
    if pair_price is None:
        atr = _estimate_atr(code, price)
        pair_price = round(price + atr * 1.1, 3)
    else:
        atr = None

    print(f"🔁 做T买入 {code} {shares}股@{buy_limit} | ATR≈{atr or 'N/A'} | 配对卖出挂单@{pair_price}")

    # 主单
    ok, before, after = _trade_with_confirmation('buy', code, shares, buy_limit)

    if ok:
        # 配对卖出挂单: 限价 ≥ 当前价
        sell_limit = max(pair_price, cur_price)
        if sell_limit != pair_price:
            print(f"   ⚠️ 卖出限价调整: {pair_price} → {sell_limit} (≥当前价{cur_price})")
        _clean_lock()
        result = _call_evolving('sell', code, shares, sell_limit)
        if result is not None:
            p_ok, p_contract = result
            if p_ok:
                print(f"   ✅ 配对卖出挂单 {code} {shares}股@{sell_limit} 合同:{p_contract}")
            else:
                print(f"   ⚠️ 配对卖出挂单失败 {code} {shares}股@{sell_limit}: {p_contract}")
        else:
            print(f"   ⚠️ 配对卖出挂单超时 {code} {shares}股@{sell_limit}")

        print(f"✅ 做T买入 {code} {shares}股@{buy_limit} | 配对卖出挂单 {shares}股@{sell_limit}")
    else:
        print(f"❌ 做T买入主单失败，不挂配对单")

    return ok


def do_t0_sell(code, shares, price, pair_price=None):
    """
    做T卖出配对：
      1. 检查连续竞价时段
      2. 撤同方向(卖出)未成交委托
      3. 主单：卖出 shares@price（限价≥当前价）
      4. 配对：挂买入单 shares@pair_price（限价≤当前价，ATR×2）
    配对失败不影响主单结果。
    """
    if not _is_continuous_auction():
        print(f"❌ 非连续竞价时段(9:30-11:30,13:00-15:00)，不执行挂单")
        return False

    # 撤同方向未成交委托(卖出方向)
    do_revoke(code, '卖出')

    # 获取当前市价用于限价检查
    cur_price = _get_price(code) or price

    # 卖出限价: ≥ 当前价
    sell_limit = max(price, cur_price)
    if sell_limit != price:
        print(f"   ⚠️ 卖出限价调整: {price} → {sell_limit} (≥当前价{cur_price})")

    # 配对价格
    if pair_price is None:
        atr = _estimate_atr(code, price)
        pair_price = round(price - atr * 1.1, 3)
    else:
        atr = None

    print(f"🔁 做T卖出 {code} {shares}股@{sell_limit} | ATR≈{atr or 'N/A'} | 配对买入挂单@{pair_price}")

    # 主单
    ok, before, after = _trade_with_confirmation('sell', code, shares, sell_limit)

    if ok:
        # 配对买入挂单: 限价 ≤ 当前价
        buy_limit = min(pair_price, cur_price)
        if buy_limit != pair_price:
            print(f"   ⚠️ 买入限价调整: {pair_price} → {buy_limit} (≤当前价{cur_price})")
        _clean_lock()
        result = _call_evolving('buy', code, shares, buy_limit)
        if result is not None:
            p_ok, p_contract = result
            if p_ok:
                print(f"   ✅ 配对买入挂单 {code} {shares}股@{buy_limit} 合同:{p_contract}")
            else:
                print(f"   ⚠️ 配对买入挂单失败 {code} {shares}股@{buy_limit}: {p_contract}")
        else:
            print(f"   ⚠️ 配对买入挂单超时 {code} {shares}股@{buy_limit}")

        print(f"✅ 做T卖出 {code} {shares}股@{sell_limit} | 配对买入挂单 {shares}股@{buy_limit}")
    else:
        print(f"❌ 做T卖出主单失败，不挂配对单")

    return ok


# ─── revoke ────────────────────────────────────────────────────────────

def do_revoke(code, direction):
    """
    撤同标的同方向未成交委托。
    direction: '买入' | '卖出'

    流程:
      1. 查 getEntrust → 找 code + direction + 未成交
      2. 逐一 revokeEntrust(contractNo)
      3. 撤完确认委托清空
    """
    _clean_lock()

    # 1. 查委托（带超时保护）
    ent = _call_evolving('getEntrust', 'today', True)
    if not (isinstance(ent, dict) and ent.get('status') and ent.get('data')):
        print(f"⚠️ 无待撤委托 {code} {direction}")
        return True

    # 2. 找匹配的未成交委托
    targets = []
    for row in ent['data']:
        if row and len(row) > 10 and row[2] == code and row[4] == direction and row[5] == '未成交':
            contract = row[10]
            if contract.strip():
                targets.append(contract)

    if not targets:
        print(f"⚠️ 无待撤委托 {code} {direction}")
        return True

    # 3. 逐一撤单（带超时保护）
    revoked = 0
    for contract in targets:
        _clean_lock()
        result = _call_evolving('revokeEntrust', contract)
        if result is not None:
            status, info = result
            if status:
                print(f"   ✅ 撤: {contract}")
                revoked += 1
            else:
                print(f"   ❌ 撤单失败 {contract}: {info}")
        else:
            print(f"   ❌ 撤单超时 {contract}")
        time.sleep(0.3)

    print(f"✅ 撤单 {code} {direction}：{revoked}笔已撤")
    return revoked > 0


# ─── main ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == 'sync':
        sync()

    elif cmd == 'buy':
        if len(sys.argv) != 5:
            print("用法: trade.py buy CODE SHARES PRICE")
            sys.exit(1)
        do_buy(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))

    elif cmd == 'sell':
        if len(sys.argv) != 5:
            print("用法: trade.py sell CODE SHARES PRICE")
            sys.exit(1)
        do_sell(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))

    elif cmd == 't0_buy':
        if len(sys.argv) < 5:
            print("用法: trade.py t0_buy CODE SHARES PRICE [PAIR_PRICE]")
            sys.exit(1)
        pair_p = float(sys.argv[5]) if len(sys.argv) >= 6 else None
        do_t0_buy(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), pair_p)

    elif cmd == 't0_sell':
        if len(sys.argv) < 5:
            print("用法: trade.py t0_sell CODE SHARES PRICE [PAIR_PRICE]")
            sys.exit(1)
        pair_p = float(sys.argv[5]) if len(sys.argv) >= 6 else None
        do_t0_sell(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), pair_p)

    elif cmd == 'revoke':
        if len(sys.argv) != 4:
            print("用法: trade.py revoke CODE 买入|卖出")
            sys.exit(1)
        do_revoke(sys.argv[2], sys.argv[3])

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)