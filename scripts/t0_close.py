#!/usr/bin/env python3
"""
T0尾盘平仓脚本 — 每个交易日 14:50 执行

核心修复（2026-10-23）：
  判定成交不再只看 orders/*.json 的 status 字段，增加两个独立证据源：
    源2：THS 真实持仓（getHoldingShares）vs portfolio.json 基准持仓
    源3：当日委托回报（getEntrust，全部，不限是否可撤）
  三源任一显示买入腿已成交，即认定成交并发起平仓。

接口异常处理：
  status=False（unknown err） → 重启同花顺 → 最多重试 3 次
  3 次后仍失败 → 打印 [ALERT] + 以退出码 2 结束，绝不静默跳过

用法:
  python3 scripts/t0_close.py [--dry-run] [--verify]

  --dry-run  : 打印将要执行的操作，不真实撤单/下单
  --verify   : 只检查状态，输出三方证据，不做任何交易操作
"""

import sys
import os
import re
import json
import time
import logging
import subprocess
import urllib.request
from datetime import datetime

# ── 路径 ────────────────────────────────────────────────────────────────
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR      = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, ROOT_DIR)

ORDERS_DIR    = os.path.join(ROOT_DIR, 'orders')
LOG_PATH      = os.path.join(ROOT_DIR, 'logs', 't0_close.log')
TRADE_PY      = os.path.join(ROOT_DIR, 'trade.py')
PORTFOLIO_JSON = os.path.join(ROOT_DIR, 'portfolio.json')
RESTART_PY    = os.path.join(SCRIPT_DIR, 'restart_ths.py')

DRY_RUN  = '--dry-run' in sys.argv
VERIFY   = '--verify'  in sys.argv

MAX_RETRIES = 3          # 接口异常最多重试次数
RESTART_WAIT = 8         # 重启同花顺后等待秒数

# ── 日志 ────────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('t0_close')

# 全局：本次运行中检测到过夜风险的配对
_overnight_risk: list = []   # list of (code, shares) tuples


# ══════════════════════════════════════════════════════════════════════════
# 接口管理
# ══════════════════════════════════════════════════════════════════════════

def _restart_ths() -> None:
    """重启同花顺（--no-revoke 不撤单）。"""
    log.warning("  正在重启同花顺（--no-revoke）...")
    try:
        subprocess.run(
            [sys.executable, RESTART_PY, '--no-revoke'],
            timeout=30, capture_output=True, text=True
        )
    except Exception as ex:
        log.error(f"  重启同花顺异常: {ex}")
    time.sleep(RESTART_WAIT)


def _ths_alive() -> bool:
    """通过 getAccountInfo 检测同花顺接口是否正常。"""
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()
        r = e.getAccountInfo()
        time.sleep(1)
        return bool(r and isinstance(r, dict) and r.get('status'))
    except Exception:
        return False


def ensure_iface(label: str = '') -> bool:
    """
    确保接口正常。已正常直接返回 True。
    status=False 或异常 → 重启重试，最多 MAX_RETRIES 次。
    全部失败返回 False。
    """
    for attempt in range(1, MAX_RETRIES + 1):
        if _ths_alive():
            return True
        log.warning(f"  [{label}] 接口 unknown err，重启同花顺重试 ({attempt}/{MAX_RETRIES})...")
        _restart_ths()
    log.error(f"  [{label}] 接口重试 {MAX_RETRIES} 次仍失败")
    return False


# ══════════════════════════════════════════════════════════════════════════
# 基准持仓（portfolio.json，不依赖 THS）
# ══════════════════════════════════════════════════════════════════════════

def get_baseline_shares(code: str) -> int:
    """
    从 portfolio.json _signal_state[code]["shares"] 读取策略基准持仓。
    这是非 T0 的底仓，不受 THS 接口状态影响。
    读取失败返回 0（保守：宁可误判为有超额持仓也不漏报）。
    """
    try:
        with open(PORTFOLIO_JSON, encoding='utf-8') as f:
            data = json.load(f)
        shares = data.get('_signal_state', {}).get(code, {}).get('shares', 0)
        return int(shares)
    except Exception as ex:
        log.warning(f"  [{code}] 读 portfolio.json 失败: {ex}")
        return 0


# ══════════════════════════════════════════════════════════════════════════
# THS 真实持仓（源2）
# ══════════════════════════════════════════════════════════════════════════

def get_real_holding_shares(code: str) -> int:
    """
    通过 getHoldingShares 获取当前真实持仓。
    接口正常但未持仓返回 0；接口失败返回 -1。
    注意：调用前需已通过 ensure_iface()。
    """
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()
        result = e.getHoldingShares()
        time.sleep(1)
        if not (result and isinstance(result, dict) and result.get('status')):
            log.warning(f"  [{code}] getHoldingShares status=False（unknown err）")
            return -1
        for row in result.get('data', []):
            if row and str(row[0]) == code:
                # row[6] = 股票余额（与 trade.py sync 一致）
                return int(float(row[6]))
        return 0
    except Exception as ex:
        log.warning(f"  [{code}] getHoldingShares 异常: {ex}")
        return -1


# ══════════════════════════════════════════════════════════════════════════
# 当日委托回报（源3）
# ══════════════════════════════════════════════════════════════════════════

def get_today_all_entrusts(code: str) -> list:
    """
    获取今日全部委托（含已成交、已撤单），不限是否可撤。
    返回该标的的委托行列表，接口失败返回 None。

    getEntrust 列顺序（stock，length=11）：
      [0]委托日期 [1]委托时间 [2]证券代码 [3]证券名称 [4]操作
      [5]备注(状态) [6]委托数量 [7]撤销数量 [8]委托价格 [9]成交价格
      [10]合同编号
    """
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()
        # isRevocable=False → 取今日全部委托，不限可撤
        result = e.getEntrust('today', False)
        time.sleep(1)
        if not (result and isinstance(result, dict) and result.get('status')):
            log.warning(f"  [{code}] getEntrust status=False（unknown err）")
            return None
        rows = []
        for row in result.get('data', []):
            if row and len(row) >= 7 and str(row[2]) == code:
                rows.append(row)
        return rows
    except Exception as ex:
        log.warning(f"  [{code}] getEntrust 异常: {ex}")
        return None


def _entrust_buy_filled(entrusts: list, buy_shares: int) -> bool:
    """
    从委托列表中判断是否存在已成交的买入委托，数量 >= buy_shares。
    行格式见 get_today_all_entrusts。
    """
    if not entrusts:
        return False
    for row in entrusts:
        if len(row) < 7:
            continue
        action  = str(row[4])   # 操作: "证券买入" 等
        remark  = str(row[5])   # 备注: "已成交"/"未成交"/"部分成交"/"已撤单"
        qty_str = str(row[6])   # 委托数量
        if '买' not in action:
            continue
        # 备注包含"已成"或"全部成交"（不含"未成"/"撤"）
        filled = ('已成' in remark or '全部' in remark) and '未成' not in remark and '撤' not in remark
        if not filled:
            continue
        try:
            qty = int(float(qty_str))
        except ValueError:
            continue
        if qty >= buy_shares:
            return True
    return False


def _entrust_sell_filled(entrusts: list, sell_shares: int) -> bool:
    """同上，判断卖出腿是否已成交。"""
    if not entrusts:
        return False
    for row in entrusts:
        if len(row) < 7:
            continue
        action  = str(row[4])
        remark  = str(row[5])
        qty_str = str(row[6])
        if '卖' not in action:
            continue
        filled = ('已成' in remark or '全部' in remark) and '未成' not in remark and '撤' not in remark
        if not filled:
            continue
        try:
            qty = int(float(qty_str))
        except ValueError:
            continue
        if qty >= sell_shares:
            return True
    return False


def _entrust_has_pending_sell(entrusts: list) -> bool:
    """当日委托中是否已有挂卖单（幂等检查用）。"""
    if not entrusts:
        return False
    for row in entrusts:
        if len(row) < 6:
            continue
        action = str(row[4])
        remark = str(row[5])
        if '卖' in action and '未成' in remark:
            return True
    return False


def _entrust_has_pending_buy(entrusts: list) -> bool:
    """当日委托中是否已有挂买单（幂等检查用）。"""
    if not entrusts:
        return False
    for row in entrusts:
        if len(row) < 6:
            continue
        action = str(row[4])
        remark = str(row[5])
        if '买' in action and '未成' in remark:
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════
# 三方成交检测
# ══════════════════════════════════════════════════════════════════════════

def detect_leg_status(code: str, buy_order: dict, sell_order: dict, pair_id: str):
    """
    三方证据判断买卖腿成交状态。

    返回 dict:
      {
        'buy_filled':   bool,
        'sell_filled':  bool,
        'iface_ok':     bool,     # THS 接口是否可用
        'evidence': {
          'file':      str,       # 文件 status
          'holdings':  str,       # 持仓证据
          'entrust':   str,       # 委托证据
        }
      }
    """
    buy_shares  = int(buy_order.get('shares',  0))
    sell_shares = int(sell_order.get('shares', 0))

    # ── 源1：文件状态 ──────────────────────────────────────────────────
    file_buy_status  = buy_order.get('status',  'unknown')
    file_sell_status = sell_order.get('status', 'unknown')
    file_buy_filled  = (file_buy_status  == 'filled')
    file_sell_filled = (file_sell_status == 'filled')
    ev_file = f"buy={file_buy_status} sell={file_sell_status}"

    # ── 接口可用性 ────────────────────────────────────────────────────
    iface_ok = ensure_iface(pair_id)

    if not iface_ok:
        return {
            'buy_filled':  file_buy_filled,
            'sell_filled': file_sell_filled,
            'iface_ok':    False,
            'evidence': {
                'file':     ev_file,
                'holdings': '接口不可用',
                'entrust':  '接口不可用',
            }
        }

    # ── 源2：真实持仓 vs 基准持仓 ─────────────────────────────────────
    baseline    = get_baseline_shares(code)
    real_shares = get_real_holding_shares(code)

    if real_shares >= 0:
        excess = real_shares - baseline
        hold_buy_filled  = excess >= buy_shares   # 持仓超出基准 ≥ 买入量
        hold_sell_filled = (baseline - real_shares) >= sell_shares  # 持仓低于基准 ≥ 卖出量
        ev_holdings = (f"real={real_shares} baseline={baseline} excess={excess} "
                       f"→ buy_filled={hold_buy_filled} sell_filled={hold_sell_filled}")
    else:
        hold_buy_filled  = False
        hold_sell_filled = False
        ev_holdings = 'getHoldingShares 返回 -1'

    # ── 源3：当日委托回报 ──────────────────────────────────────────────
    entrusts = get_today_all_entrusts(code)
    if entrusts is None:
        ent_buy_filled  = False
        ent_sell_filled = False
        ev_entrust = 'getEntrust 返回 None'
    else:
        ent_buy_filled  = _entrust_buy_filled(entrusts,  buy_shares)
        ent_sell_filled = _entrust_sell_filled(entrusts, sell_shares)
        ev_entrust = (f"共{len(entrusts)}条 "
                      f"→ buy_filled={ent_buy_filled} sell_filled={ent_sell_filled}")

    # ── 综合判定（三源任一为真即认定） ───────────────────────────────
    final_buy_filled  = file_buy_filled  or hold_buy_filled  or ent_buy_filled
    final_sell_filled = file_sell_filled or hold_sell_filled or ent_sell_filled

    return {
        'buy_filled':  final_buy_filled,
        'sell_filled': final_sell_filled,
        'iface_ok':    True,
        '_entrusts':   entrusts,          # 供后续幂等检查复用
        'evidence': {
            'file':     ev_file,
            'holdings': ev_holdings,
            'entrust':  ev_entrust,
        }
    }


# ══════════════════════════════════════════════════════════════════════════
# 市场行情
# ══════════════════════════════════════════════════════════════════════════

def _fetch_sina_quote(code: str):
    """通过 Sina API 获取实时报价，返回 (bid1, ask1, current)，失败返回 None。"""
    try:
        from state_center import get_code_map
        code_map = get_code_map()
        sid = code_map.get(code, {}).get('sid', '')
    except Exception:
        sid = ''

    if not sid:
        try:
            with open(os.path.join(ROOT_DIR, 'etf_pool.json')) as f:
                pool_data = json.load(f)
            for item in pool_data.get('etf_pool_detail', []):
                if item.get('code') == code:
                    sid = item.get('sid', '')
                    break
        except Exception:
            pass

    if not sid:
        log.warning(f"[{code}] 找不到 sid，无法获取行情")
        return None

    try:
        url = f"https://hq.sinajs.cn/list={sid}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode('gbk')
        idx = raw.find('"')
        if idx == -1:
            return None
        content = raw[idx+1:raw.rfind('"')]
        parts = content.split(',')
        if len(parts) < 10:
            return None
        current = float(parts[3]) if parts[3] else 0.0
        bid1    = float(parts[6]) if parts[6] else 0.0
        ask1    = float(parts[7]) if parts[7] else 0.0
        return bid1, ask1, current
    except Exception as e:
        log.warning(f"[{code}] Sina 行情获取失败: {e}")
        return None


def get_close_price(code: str, direction: str) -> float:
    """
    direction='sell' → ask1×0.995（略低于卖一，保证成交）
    direction='buy'  → bid1×1.005（略高于买一，保证成交）
    """
    q = _fetch_sina_quote(code)
    if q is None:
        return 0.0
    bid1, ask1, current = q
    if direction == 'sell':
        p = ask1 if ask1 > 0 else current * 0.995
        return round(p * 0.995, 3)
    else:
        p = bid1 if bid1 > 0 else current * 1.005
        return round(p * 1.005, 3)


# ══════════════════════════════════════════════════════════════════════════
# 扫描配对
# ══════════════════════════════════════════════════════════════════════════

def _today() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def _today_compact() -> str:
    return datetime.now().strftime('%Y%m%d')


def scan_t0_pairs() -> list:
    """
    扫描 orders/ 目录，按 code+序号 配对当天的 t0_buy_X / t0_sell_X。
    文件名格式：{YYYYMMDD}_{code}_t0_{buy|sell}_{X}.json
    仅返回两条腿都存在的完整配对。
    """
    today = _today_compact()
    pat = re.compile(r'^(\d{8})_(\d+)_t0_(buy|sell)_(\d+)\.json$')
    pairs: dict = {}

    for fname in os.listdir(ORDERS_DIR):
        m = pat.match(fname)
        if not m:
            continue
        date_str, code, side, x = m.group(1), m.group(2), m.group(3), m.group(4)
        if date_str != today:
            continue
        fpath = os.path.join(ORDERS_DIR, fname)
        try:
            with open(fpath) as f:
                order = json.load(f)
        except Exception:
            continue
        order['_file'] = fpath
        key = (code, x)
        if key not in pairs:
            pairs[key] = {}
        pairs[key][side] = order

    result = []
    for (code, x), legs in pairs.items():
        if 'buy' not in legs or 'sell' not in legs:
            log.warning(f"[{code}#{x}] 配对不完整，跳过（只有 {list(legs.keys())}）")
            continue
        result.append({
            'code':       code,
            'pair_id':    f"{code}_{x}",
            'buy_order':  legs['buy'],
            'sell_order': legs['sell'],
        })
    return result


# ══════════════════════════════════════════════════════════════════════════
# 撤单
# ══════════════════════════════════════════════════════════════════════════

def revoke_order(contract_no: str) -> bool:
    """先单笔撤，失败降级全撤。返回是否撤单成功。"""
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()

        if contract_no and contract_no != 'pending':
            result = e.revokeEntrust(revokeType='contractNo', contractNo=contract_no)
            time.sleep(2)
            if result is not None:
                log.info(f"  单笔撤单成功 contractNo={contract_no}")
                return True
            log.warning(f"  单笔撤单返回 None（unknown err），降级全撤")

        result = e.revokeEntrust(revokeType='allBuyAndSell')
        time.sleep(2)
        if result is not None:
            log.info(f"  全撤成功（allBuyAndSell）")
            return True
        log.error(f"  全撤也失败")
        return False
    except Exception as ex:
        log.error(f"  撤单异常: {ex}")
        return False


# ══════════════════════════════════════════════════════════════════════════
# 下单
# ══════════════════════════════════════════════════════════════════════════

def place_order(action: str, code: str, shares: int, price: float) -> bool:
    """调用 python3 trade.py sell/buy 下单，返回是否成功。"""
    cmd = [sys.executable, TRADE_PY, action, code, str(shares), str(price)]
    log.info(f"  下单: {' '.join(cmd)}")
    if DRY_RUN or VERIFY:
        log.info(f"  [{'VERIFY' if VERIFY else 'DRY-RUN'}] 跳过真实下单")
        return True
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = (r.stdout + r.stderr).strip()
        if output:
            log.info(f"  trade.py 输出: {output}")
        return r.returncode == 0
    except Exception as ex:
        log.error(f"  trade.py 调用异常: {ex}")
        return False


# ══════════════════════════════════════════════════════════════════════════
# 日志记录
# ══════════════════════════════════════════════════════════════════════════

def _log_record(code, action, shares, price, reason, filled, contract):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status = '成交' if filled else '未成交'
    log.info(
        f"[LOG] {ts} | code={code} | action={action} | shares={shares} "
        f"| price={price} | reason={reason} | status={status} | contract={contract}"
    )


# ══════════════════════════════════════════════════════════════════════════
# --verify 模式
# ══════════════════════════════════════════════════════════════════════════

def main_verify():
    """--verify 模式：只检查状态，输出三方证据，不交易。"""
    log.info("=== T0平仓 --verify 模式（只读，不下单）===")
    pairs = scan_t0_pairs()
    if not pairs:
        log.info("无当天T0配对")
        return

    log.info(f"找到 {len(pairs)} 组T0配对: " + ", ".join(p['pair_id'] for p in pairs))
    log.info("")

    for pair in pairs:
        code      = pair['code']
        pair_id   = pair['pair_id']
        buy_order = pair['buy_order']
        sell_order= pair['sell_order']

        log.info(f"{'='*60}")
        log.info(f"配对: {pair_id}  ({buy_order.get('created_at','')})")
        log.info(f"  买入: {buy_order.get('shares')} 股 @{buy_order.get('price')}")
        log.info(f"  卖出: {sell_order.get('shares')} 股 @{sell_order.get('price')}")

        status = detect_leg_status(code, buy_order, sell_order, pair_id)
        ev = status['evidence']

        log.info(f"  [源1 文件状态]  {ev['file']}")
        log.info(f"  [源2 真实持仓]  {ev['holdings']}")
        log.info(f"  [源3 当日委托]  {ev['entrust']}")
        log.info(f"  接口可用: {status['iface_ok']}")
        log.info(f"  综合判定: buy_filled={status['buy_filled']}  sell_filled={status['sell_filled']}")

        if status['buy_filled'] and not status['sell_filled']:
            log.info(f"  ★ 需要平仓（卖出 {buy_order.get('shares')} 股）")
        elif not status['buy_filled'] and status['sell_filled']:
            log.info(f"  ★ 需要平仓（买回 {sell_order.get('shares')} 股）")
        elif status['buy_filled'] and status['sell_filled']:
            log.info(f"  ✓ 两腿均已成交，无需操作")
        else:
            log.info(f"  ？ 两腿均未确认成交")
            if not status['iface_ok']:
                log.warning(f"  [ALERT] 接口不可用，无法确认，存在过夜风险: {code} {buy_order.get('shares')} 股")
        log.info("")

    log.info("=== verify 完成 ===")


# ══════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════

def main():
    mode_tag = '[DRY-RUN]' if DRY_RUN else ''
    log.info(f"=== T0尾盘平仓开始 {mode_tag} ===")

    pairs = scan_t0_pairs()
    if not pairs:
        log.info("无当天T0配对，直接退出")
        return

    log.info(f"找到 {len(pairs)} 组T0配对: " + ", ".join(p['pair_id'] for p in pairs))

    for pair in pairs:
        code       = pair['code']
        pair_id    = pair['pair_id']
        buy_order  = pair['buy_order']
        sell_order = pair['sell_order']

        log.info(f"--- 配对 {pair_id} ---")

        # ── 三方成交检测 ───────────────────────────────────────────────
        status = detect_leg_status(code, buy_order, sell_order, pair_id)
        ev = status['evidence']
        buy_filled  = status['buy_filled']
        sell_filled = status['sell_filled']
        iface_ok    = status['iface_ok']
        entrusts    = status.get('_entrusts')   # 当日委托列表，供幂等复用

        log.info(f"  [源1 文件状态]  {ev['file']}")
        log.info(f"  [源2 真实持仓]  {ev['holdings']}")
        log.info(f"  [源3 当日委托]  {ev['entrust']}")

        # ── 判断需要平仓的方向 ─────────────────────────────────────────
        if buy_filled and sell_filled:
            log.info(f"  [{pair_id}] 两腿均已成交，跳过")
            continue

        if not buy_filled and not sell_filled:
            if not iface_ok:
                # 接口不可用且文件状态均为 pending → 无法确认 → 告警
                shares = int(buy_order.get('shares', 0))
                log.error(
                    f"[ALERT] T0 平仓失败，存在过夜风险: {code} {shares} 股"
                    f"（接口不可用，无法确认成交状态）"
                )
                _overnight_risk.append((code, shares))
            else:
                # 接口正常但两腿均未成交（可能真的都没成）
                log.info(f"  [{pair_id}] 两腿均未成交（接口正常，三方均为 pending），跳过")
            continue

        if buy_filled and not sell_filled:
            filled_order  = buy_order
            pending_order = sell_order
            close_action  = 'sell'
        else:  # sell_filled and not buy_filled
            filled_order  = sell_order
            pending_order = buy_order
            close_action  = 'buy'

        shares   = int(filled_order['shares'])
        price    = float(filled_order['price'])
        contract = pending_order.get('contract', 'pending')

        log.info(f"  [{pair_id}] 已成交腿={filled_order['action']} {shares}股 @{price}，"
                 f"需撤 {pending_order['action']} contractNo={contract}，平仓方向={close_action}")

        # ── 数量校验 ──────────────────────────────────────────────────
        min_shares = 5000
        if shares < min_shares:
            log.warning(f"[{code}] 数量 {shares} < {min_shares}，跳过（告警）")
            _log_record(code, close_action, shares, price, '数量不足跳过', False, contract)
            continue
        if shares % 100 != 0:
            shares = (shares // 100) * 100
            if shares < min_shares:
                log.warning(f"[{code}] 修正后数量 {shares} < {min_shares}，跳过")
                _log_record(code, close_action, shares, price, '数量不足跳过', False, contract)
                continue
            log.info(f"  [{code}] 数量修正为 {shares}（100的整数倍）")

        # ── 幂等检查 ──────────────────────────────────────────────────
        # 避免重复跑时重复下单
        if not DRY_RUN:
            if close_action == 'sell':
                # 若已有挂卖单 → 上次已处理过
                if entrusts is not None and _entrust_has_pending_sell(entrusts):
                    log.info(f"  [{code}] 已有挂卖单，跳过（幂等）")
                    continue
                # 若持仓已回归基准 → 已平仓
                real = get_real_holding_shares(code)
                if real >= 0:
                    baseline = get_baseline_shares(code)
                    if real <= baseline:
                        log.info(f"  [{code}] 持仓已回归基准（real={real} baseline={baseline}），跳过（幂等）")
                        continue
            else:  # buy
                if entrusts is not None and _entrust_has_pending_buy(entrusts):
                    log.info(f"  [{code}] 已有挂买单，跳过（幂等）")
                    continue

        # ── 撤单 ──────────────────────────────────────────────────────
        if DRY_RUN:
            log.info(f"  [DRY-RUN] 将撤单 {pending_order['action']} contractNo={contract}")
            revoke_ok = True
        else:
            revoke_ok = revoke_order(contract)

        if not revoke_ok:
            log.error(f"  [{code}] 撤单失败，跳过平仓")
            _log_record(code, close_action, shares, price, '撤单失败跳过', False, contract)
            continue

        # ── 取对手价 ──────────────────────────────────────────────────
        close_price = get_close_price(code, close_action)
        if close_price <= 0:
            log.error(f"  [{code}] 行情获取失败，跳过平仓")
            _log_record(code, close_action, shares, price, '行情失败跳过', False, contract)
            continue

        log.info(f"  [{code}] 平仓: {close_action} {shares}股 @{close_price}（原成交价 {price}）原因=T0尾盘平仓")

        # ── 下单 ──────────────────────────────────────────────────────
        ok = place_order(close_action, code, shares, close_price)
        _log_record(code, close_action, shares, close_price, 'T0尾盘平仓', ok, contract)

        if ok:
            log.info(f"  [{code}] 平仓下单{'成功（dry-run）' if DRY_RUN else '成功'}")
        else:
            log.error(f"  [{code}] 平仓下单失败")
            _overnight_risk.append((code, shares))

    # ── 最终告警 + 退出码 ────────────────────────────────────────────────
    log.info("=== T0尾盘平仓结束 ===")

    if _overnight_risk:
        log.error("")
        log.error("!!! ═══════════════════════════════════════════════════════ !!!")
        for c, s in _overnight_risk:
            log.error(f"[ALERT] T0 平仓失败，存在过夜风险: {c} {s} 股")
        log.error("!!! ═══════════════════════════════════════════════════════ !!!")
        log.error("")
        sys.exit(2)


if __name__ == '__main__':
    if VERIFY:
        main_verify()
    else:
        main()
