#!/usr/bin/env python3
"""
T0尾盘平仓脚本 — 每个交易日 14:50 执行

逻辑：
  1. 扫描 orders/*.json，找当天 action=t0_buy/t0_sell 且 status=pending 的单
  2. 先单笔撤单（revokeType='contractNo'），失败则全撤（revokeType='allBuyAndSell'）
  3. 撤单后按对手价市价平掉暴露的腿：
       t0_buy(pending买单)→卖出(卖一价×0.995)
       t0_sell(pending卖单)→买回(买一价×1.005)
  4. 数量须100整数倍且≥5000股，不足则跳过告警
  5. 幂等：重复运行检查持仓/挂单后跳过已平仓的

用法:
  python3 scripts/t0_close.py [--dry-run]
"""

import sys
import os
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


# ── 扫描未完成 T0 订单 ──────────────────────────────────────────────────
def _today() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def scan_pending_t0() -> list:
    """返回当天所有 status=pending 的 t0_buy/t0_sell 订单列表，每项含文件路径。"""
    today = _today()
    results = []
    for fname in os.listdir(ORDERS_DIR):
        if not fname.endswith('.json'):
            continue
        fpath = os.path.join(ORDERS_DIR, fname)
        try:
            with open(fpath) as f:
                order = json.load(f)
        except Exception:
            continue
        if order.get('status') != 'pending':
            continue
        if order.get('action') not in ('t0_buy', 't0_sell'):
            continue
        created = order.get('created_at', '')
        if not created.startswith(today):
            continue
        order['_file'] = fpath
        results.append(order)
    return results


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

    pending_orders = scan_pending_t0()
    if not pending_orders:
        print("无未完成T0")
        log.info("无未完成T0，直接退出")
        return

    log.info(f"找到 {len(pending_orders)} 笔未完成T0: "
             + ", ".join(f"{o['code']}({o['action']}×{o['shares']})" for o in pending_orders))

    for order in pending_orders:
        code     = order['code']
        action   = order['action']   # t0_buy / t0_sell
        shares   = int(order['shares'])
        price    = float(order['price'])
        contract = order.get('contract', 'pending')

        log.info(f"--- 处理 {code} {action} {shares}股 @{price} contract={contract} ---")

        # ── 幂等检查 ────────────────────────────────────────────────────
        if action == 't0_buy':
            # t0_buy 挂单未成交：说明买入已成交，卖出还没挂/没成
            # 检查该代码是否还有挂卖单（若已被撤或无挂单，就需要卖出平仓）
            pending_sells = [r for r in get_pending_entrust(code)
                             if '卖' in str(r[4] if len(r) > 4 else '')]
            if pending_sells:
                log.info(f"  [{code}] 已有挂卖单 {len(pending_sells)} 笔，跳过（幂等）")
                continue
            # 检查持仓，若持仓不足则可能已被平
            held = get_holding_shares(code)
            if held == 0:
                log.info(f"  [{code}] 持仓为0，已平仓，跳过（幂等）")
                continue
            if held < 0:
                log.warning(f"  [{code}] 持仓查询失败，保守跳过")
                continue

        elif action == 't0_sell':
            # t0_sell 挂单未成交：卖出挂单未成，买回腿也未完成
            # 检查是否还有挂买单（全撤后此挂单应已不存在）
            pending_buys = [r for r in get_pending_entrust(code)
                            if '买' in str(r[4] if len(r) > 4 else '')]
            if pending_buys:
                log.info(f"  [{code}] 已有挂买单 {len(pending_buys)} 笔，跳过（幂等）")
                continue

        # ── 数量校验 ─────────────────────────────────────────────────────
        min_shares = 5000
        if shares < min_shares:
            msg = f"[{code}] 数量 {shares} < {min_shares}，跳过（告警）"
            log.warning(msg)
            _log_record(code, action, shares, price, '数量不足跳过', False, contract)
            continue
        if shares % 100 != 0:
            # 向下取整到100的倍数
            shares = (shares // 100) * 100
            if shares < min_shares:
                log.warning(f"[{code}] 修正后数量 {shares} < {min_shares}，跳过")
                _log_record(code, action, shares, price, '数量不足跳过', False, contract)
                continue
            log.info(f"  [{code}] 数量修正为 {shares}（100的整数倍）")

        # ── 撤单 ─────────────────────────────────────────────────────────
        if DRY_RUN:
            log.info(f"  [DRY-RUN] 将撤单 contractNo={contract}")
            revoke_ok = True
        else:
            revoke_ok = revoke_order(contract)

        if not revoke_ok:
            log.error(f"  [{code}] 撤单失败，跳过平仓")
            _log_record(code, action, shares, price, '撤单失败跳过', False, contract)
            continue

        # ── 取对手价 ─────────────────────────────────────────────────────
        if action == 't0_buy':
            # 买单已成交，需要卖出平掉多头
            close_action = 'sell'
            close_price  = get_close_price(code, 'sell')
        else:
            # 卖单（t0_sell）未成交，需要买回
            close_action = 'buy'
            close_price  = get_close_price(code, 'buy')

        if close_price <= 0:
            log.error(f"  [{code}] 行情获取失败，跳过平仓")
            _log_record(code, action, shares, price, '行情失败跳过', False, contract)
            continue

        log.info(f"  [{code}] 平仓: {close_action} {shares}股 @{close_price}（原价 {price}）原因=T0尾盘平仓")

        # ── 下单 ─────────────────────────────────────────────────────────
        ok = place_order(close_action, code, shares, close_price)

        _log_record(code, action, shares, close_price, 'T0尾盘平仓', ok, contract)
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
