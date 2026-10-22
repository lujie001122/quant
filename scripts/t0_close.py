#!/usr/bin/env python3
"""
T0尾盘平仓脚本 — 每个交易日 14:50 执行

逻辑：
  1. 扫描 orders/，按 code+日期+序号 配对 t0_buy_X / t0_sell_X
  2. 只处理"一条腿 filled、另一条腿 pending"的配对
     - 买入腿已成交 → 撤配对卖单 → 卖出同数量（平多头）
     - 卖出腿已成交 → 撤配对买单 → 买回同数量（补仓位）
  3. 两条腿都 pending 或都 filled → 跳过
  4. 撤单优先 contractNo，失败退化 allBuyAndSell
  5. 数量须 ≥5000 股、100 整数倍，不满足则跳过告警
  6. 幂等：查 getHoldingShares / getEntrust，避免重复平仓

用法:
  python3 scripts/t0_close.py [--dry-run]
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
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR   = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, ROOT_DIR)

ORDERS_DIR = os.path.join(ROOT_DIR, 'orders')
LOG_PATH   = os.path.join(ROOT_DIR, 'logs', 't0_close.log')
TRADE_PY   = os.path.join(ROOT_DIR, 'trade.py')

DRY_RUN = '--dry-run' in sys.argv

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


# ── 市场行情（取买一/卖一价）───────────────────────────────────────────
def _fetch_sina_quote(code: str):
    """通过 Sina API 获取实时报价，返回 (bid1, ask1, current)，失败返回 None。
    Sina 返回字段顺序（逗号分隔）：
      0:名称 1:开盘 2:昨收 3:现价 4:最高 5:最低 6:买一价 7:卖一价 8:成交量 9:成交额
      10-19: 买一~五 (vol, price 交替)  20-29: 卖一~五
    """
    # 从 etf_pool / state_center 取 sid
    try:
        from state_center import get_code_map
        code_map = get_code_map()
        sid = code_map.get(code, {}).get('sid', '')
    except Exception:
        sid = ''

    if not sid:
        # 尝试从 etf_pool 直接读
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
        bid1    = float(parts[6]) if parts[6] else 0.0   # 买一价
        ask1    = float(parts[7]) if parts[7] else 0.0   # 卖一价
        return bid1, ask1, current
    except Exception as e:
        log.warning(f"[{code}] Sina 行情获取失败: {e}")
        return None


def get_close_price(code: str, direction: str) -> float:
    """
    direction='sell' → 取卖一价(ask1)×0.995 保证成交
    direction='buy'  → 取买一价(bid1)×1.005 保证成交
    fallback: current×0.995 或 current×1.005
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


# ── 扫描并配对当天 T0 订单 ─────────────────────────────────────────────
def _today() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def _today_compact() -> str:
    return datetime.now().strftime('%Y%m%d')


def scan_t0_pairs() -> list:
    """
    扫描 orders/ 目录，按 code+序号 配对当天的 t0_buy_X / t0_sell_X。
    文件名格式：{YYYYMMDD}_{code}_t0_{buy|sell}_{X}.json

    返回列表，每项为 dict：
      {
        'code':       str,
        'pair_id':    str,   # "{code}_{X}"
        'buy_order':  dict,  # t0_buy 文件内容（含 _file）
        'sell_order': dict,  # t0_sell 文件内容（含 _file）
      }
    仅返回两条腿都存在的完整配对。
    """
    today = _today_compact()
    # 文件名正则: YYYYMMDD_code_t0_{buy|sell}_X.json
    pat = re.compile(
        r'^(\d{8})_(\d+)_t0_(buy|sell)_(\d+)\.json$'
    )

    # key: (code, X) -> {'buy': order, 'sell': order}
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


# ── 持仓检查 ─────────────────────────────────────────────────────────────
def get_holding_shares(code: str) -> int:
    """通过 EvolvingSim.getHoldingShares 获取持仓股数，API 故障返回 -1。"""
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()
        result = e.getHoldingShares()
        time.sleep(1)
        if result and isinstance(result, dict) and result.get('status'):
            for row in result.get('data', []):
                if row and row[0] == code:
                    return int(row[6])
        return 0
    except Exception as ex:
        log.warning(f"[{code}] getHoldingShares 失败: {ex}")
        return -1


# ── 挂单检查 ─────────────────────────────────────────────────────────────
def get_pending_entrust(code: str) -> list:
    """返回当天该标的仍在挂单（未成交）的委托列表。"""
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()
        result = e.getEntrust('today', False)
        time.sleep(1)
        pending = []
        if result and isinstance(result, dict) and result.get('status'):
            for row in result.get('data', []):
                if not row or len(row) < 9:
                    continue
                if str(row[0]) == code and '未成' in str(row[8]):
                    pending.append(row)
        return pending
    except Exception as ex:
        log.warning(f"[{code}] getEntrust 失败: {ex}")
        return []


# ── 撤单 ─────────────────────────────────────────────────────────────────
def revoke_order(contract_no: str) -> bool:
    """先单笔撤，失败降级全撤。返回是否撤单成功。"""
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()

        # 尝试单笔撤单
        if contract_no and contract_no != 'pending':
            result = e.revokeEntrust(revokeType='contractNo', contractNo=contract_no)
            time.sleep(2)
            if result is not None:
                log.info(f"  单笔撤单成功 contractNo={contract_no}")
                return True
            log.warning(f"  单笔撤单返回 None（unknown err），降级全撤")

        # 全撤兜底
        result = e.revokeEntrust(revokeType='allBuyAndSell')
        time.sleep(2)
        if result is not None:
            log.info(f"  全撤成功（allBuyAndSell）")
            return True
        log.error(f"  全撤也失败，跳过")
        return False
    except Exception as ex:
        log.error(f"  撤单异常: {ex}")
        return False


# ── 下单（通过 python3 trade.py）──────────────────────────────────────────
def place_order(action: str, code: str, shares: int, price: float) -> bool:
    """调用 python3 trade.py sell/buy 下单，返回是否成功。"""
    cmd = [sys.executable, TRADE_PY, action, code, str(shares), str(price)]
    log.info(f"  下单: {' '.join(cmd)}")
    if DRY_RUN:
        log.info(f"  [DRY-RUN] 跳过真实下单")
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


# ── 主流程 ────────────────────────────────────────────────────────────────
def main():
    log.info(f"=== T0尾盘平仓开始 {'[DRY-RUN]' if DRY_RUN else ''} ===")

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

        buy_status  = buy_order.get('status', '')
        sell_status = sell_order.get('status', '')

        log.info(f"--- 配对 {pair_id}: buy={buy_status} sell={sell_status} ---")

        # ── 判断哪条腿成交、哪条腿挂单 ──────────────────────────────────
        if buy_status == 'filled' and sell_status == 'pending':
            # 买入腿已成交，配对卖单未成 → 撤卖单 → 卖出平多头
            filled_order  = buy_order
            pending_order = sell_order
            close_action  = 'sell'
        elif sell_status == 'filled' and buy_status == 'pending':
            # 卖出腿已成交，配对买单未成 → 撤买单 → 买回补仓
            filled_order  = sell_order
            pending_order = buy_order
            close_action  = 'buy'
        elif buy_status == 'filled' and sell_status == 'filled':
            log.info(f"  [{pair_id}] 两腿均已成交，跳过")
            continue
        else:
            # 两腿都 pending，或状态不明
            log.info(f"  [{pair_id}] 两腿均未成交（buy={buy_status} sell={sell_status}），跳过")
            continue

        shares   = int(filled_order['shares'])
        price    = float(filled_order['price'])
        contract = pending_order.get('contract', 'pending')  # 撤的是 pending 腿的合同号

        log.info(f"  [{pair_id}] 已成交腿={filled_order['action']} {shares}股 @{price}，"
                 f"需撤 {pending_order['action']} contractNo={contract}，平仓方向={close_action}")

        # ── 幂等检查（仅实盘；dry-run 纯由订单文件判定，保证可重复）────────
        if not DRY_RUN:
            if close_action == 'sell':
                # 已有挂卖单说明上次已处理
                pending_sells = [r for r in get_pending_entrust(code)
                                 if '卖' in str(r[4] if len(r) > 4 else '')]
                if pending_sells:
                    log.info(f"  [{code}] 已有挂卖单 {len(pending_sells)} 笔，跳过（幂等）")
                    continue
                held = get_holding_shares(code)
                if held == 0:
                    log.info(f"  [{code}] 持仓为0，已平仓，跳过（幂等）")
                    continue
                if held < 0:
                    log.warning(f"  [{code}] 持仓查询失败，保守跳过")
                    continue
            else:  # buy
                pending_buys = [r for r in get_pending_entrust(code)
                                if '买' in str(r[4] if len(r) > 4 else '')]
                if pending_buys:
                    log.info(f"  [{code}] 已有挂买单 {len(pending_buys)} 笔，跳过（幂等）")
                    continue

        # ── 数量校验 ──────────────────────────────────────────────────────
        min_shares = 5000
        if shares < min_shares:
            msg = f"[{code}] 数量 {shares} < {min_shares}，跳过（告警）"
            log.warning(msg)
            _log_record(code, close_action, shares, price, '数量不足跳过', False, contract)
            continue
        if shares % 100 != 0:
            shares = (shares // 100) * 100
            if shares < min_shares:
                log.warning(f"[{code}] 修正后数量 {shares} < {min_shares}，跳过")
                _log_record(code, close_action, shares, price, '数量不足跳过', False, contract)
                continue
            log.info(f"  [{code}] 数量修正为 {shares}（100的整数倍）")

        # ── 撤单（撤 pending 腿）─────────────────────────────────────────
        if DRY_RUN:
            log.info(f"  [DRY-RUN] 将撤单 {pending_order['action']} contractNo={contract}")
            revoke_ok = True
        else:
            revoke_ok = revoke_order(contract)

        if not revoke_ok:
            log.error(f"  [{code}] 撤单失败，跳过平仓")
            _log_record(code, close_action, shares, price, '撤单失败跳过', False, contract)
            continue

        # ── 取对手价 ──────────────────────────────────────────────────────
        close_price = get_close_price(code, close_action)
        if close_price <= 0:
            log.error(f"  [{code}] 行情获取失败，跳过平仓")
            _log_record(code, close_action, shares, price, '行情失败跳过', False, contract)
            continue

        log.info(f"  [{code}] 平仓: {close_action} {shares}股 @{close_price}（原成交价 {price}）原因=T0尾盘平仓")

        # ── 下单 ──────────────────────────────────────────────────────────
        ok = place_order(close_action, code, shares, close_price)

        _log_record(code, close_action, shares, close_price, 'T0尾盘平仓', ok, contract)
        if ok:
            log.info(f"  [{code}] 平仓下单{'成功（dry-run）' if DRY_RUN else '成功'}")
        else:
            log.error(f"  [{code}] 平仓下单失败")

    log.info("=== T0尾盘平仓结束 ===")


def _log_record(code, action, shares, price, reason, filled, contract):
    """写结构化日志行（便于 grep）。"""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status = '成交' if filled else '未成交'
    log.info(
        f"[LOG] {ts} | code={code} | action={action} | shares={shares} "
        f"| price={price} | reason={reason} | status={status} | contract={contract}"
    )


if __name__ == '__main__':
    main()
