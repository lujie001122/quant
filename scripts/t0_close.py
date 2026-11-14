#!/usr/bin/env python3
"""
T0尾盘平仓脚本 — 每个交易日 14:50 执行

判定规则（2026-11-14 修复 11-13 单边敞口漏平）：
  1. 每只持仓取 真实持仓(frida) - 基线(baseline)
  2. diff<0（卖多了）→ 买回差额；diff>0（买多了）→ 卖出差额；diff=0 → 不动作
  3. 配对文件（orders/*_t0_*.json）只用于解释原因/写日志，不作"无需动作"依据
  4. 平仓价=卖一/买一/现价，并用当日涨跌停价夹边界（money_manager.day_price_limits）
  5. 每只都打印 baseline/real/diff；有动作再打印方向/合同号/成交价/数量/平仓后持仓

基线优先级：当日最早一笔 T0 订单的 baseline_shares → logs/t0_baseline_YYYYMMDD.json
            → _signal_state 兜底（日志标注来源）

接口异常：frida 优先、EvolvingSim 兜底；读不到真实持仓 → [ALERT] 并以退出码 2 结束

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

def get_baseline_shares(code: str, order: dict = None) -> tuple:
    """
    读取配对的基准持仓，返回 (baseline, source_label)。

    优先级：
    1. 配对记录里的 baseline_shares（配对建立时固定写入，永不改写）
    2. portfolio.json _signal_state[code]["shares"]（缺失时兜底，日志标注"基线缺失"）

    参数 order: 任意一条配对腿的 dict（buy_order 或 sell_order），
                含 baseline_shares 字段时直接用；传 None 则直接走兜底。
    """
    # ── 优先：配对记录里的固定基线 ───────────────────────────────────────
    if order is not None:
        v = order.get('baseline_shares')
        if v is not None:
            try:
                return int(v), '配对记录 baseline_shares'
            except (TypeError, ValueError):
                pass

    # ── 兜底：_signal_state（sync 可能已更新，需在日志中标注） ─────────────
    try:
        with open(PORTFOLIO_JSON, encoding='utf-8') as f:
            data = json.load(f)
        shares = data.get('_signal_state', {}).get(code, {}).get('shares', 0)
        return int(shares), '_signal_state shares（基线缺失，可能已被 sync 覆盖）'
    except Exception as ex:
        log.warning(f'  [{code}] 读 portfolio.json 失败: {ex}')
        return 0, '_signal_state shares（读取失败）'


def get_local_position_shares(code: str) -> int:
    """
    从 portfolio.json positions[code]["shares"] 读取本地同步的实际持仓。
    这是 trade.py sync 写入的最后一次同步值，不依赖 THS 实时接口。
    读取失败返回 -1（表示数据不可用，区别于持仓为0）。
    """
    try:
        with open(PORTFOLIO_JSON, encoding='utf-8') as f:
            data = json.load(f)
        pos = data.get('positions', {}).get(code)
        if pos is None:
            # positions 中无此标的，说明上次 sync 时持仓为 0
            return 0
        return int(pos.get('shares', 0))
    except Exception as ex:
        log.warning(f"  [{code}] 读 portfolio.json positions 失败: {ex}")
        return -1


# 判断 local_shares 与预期值是否足够接近（允许 100 股误差，处理零股取整）
_SHARE_TOLERANCE = 100


def _shares_match(a: int, b: int) -> bool:
    return abs(a - b) <= _SHARE_TOLERANCE


def assess_by_local_data(code: str, buy_shares: int, sell_shares: int,
                         buy_order: dict = None, sell_order: dict = None) -> dict:
    """
    第二级降级判断：接口不可用时，用本地 portfolio.json 数据判断腿成交状态。

    T0 做法：先卖出 sell_shares（减仓），再买回 buy_shares（加仓）。
    设 baseline = 配对记录里的 baseline_shares（配对建立时固定写入，永不改写）。
                  缺失时回退 _signal_state[code]["shares"]，日志标注"基线缺失"。
    设 local   = positions[code]["shares"]（本地 sync 记录的实际持仓）。

    持仓变化分析：
      两腿均未成交 → local == baseline（持仓未动）
      两腿均已成交 → local == baseline - sell_shares + buy_shares
                      buy==sell 时等于 baseline，与"均未成"数值相同
                      ⚠️ 两者数值无法区分，必须输出"无法确认"，不得断言已成交
      仅卖腿成交   → local == baseline - sell_shares  ← 单边暴露，真风险
      仅买腿成交   → local == baseline + buy_shares   ← 异常超买

    返回 dict:
      level         : 2（本地数据可用）或 3（数据矛盾/缺失）
      buy_filled    : bool
      sell_filled   : bool
      skip_no_alert : bool  接口不可用且本地判定无风险，跳过不报警
      single_side   : str   'sell_only'|'buy_only'|'' 单边暴露方向
      inconclusive  : bool  True=无法区分两腿都成交/两腿都未成交，必须人工核对
      evidence      : str   日志用说明字符串（含基线来源）
    """
    # 优先读配对记录里的固定基线；缺失时兜底 _signal_state
    ref_order = buy_order or sell_order
    baseline, baseline_src = get_baseline_shares(code, ref_order)
    local = get_local_position_shares(code)

    base_info = (f"baseline={baseline}（来自:{baseline_src}）"
                 f" local={local} buy_shares={buy_shares} sell_shares={sell_shares}")

    _base = {
        'buy_filled': False, 'sell_filled': False,
        'skip_no_alert': False, 'single_side': '',
        'inconclusive': False,
    }

    # ── Level 3：数据不可用 ──────────────────────────────────────────────
    if local < 0:
        return {**_base, 'level': 3,
                'evidence': f"[第三级] portfolio.json 读取失败，无法判断 | {base_info}"}

    if baseline <= 0 and local <= 0:
        return {**_base, 'level': 3,
                'evidence': f"[第三级] baseline 和 local 均为 0，数据矛盾或缺失 | {base_info}"}

    # ── Level 2：用持仓变化判断 ──────────────────────────────────────────
    diff = local - baseline   # 正值表示持仓相比基线增加

    # 情形 A：仅买腿成交（持仓增加 ≈ buy_shares）→ 异常超买，真风险
    if buy_shares > 0 and _shares_match(diff, buy_shares) and diff > 0:
        return {**_base, 'level': 2,
                'buy_filled': True, 'sell_filled': False,
                'skip_no_alert': False, 'single_side': 'buy_only',
                'evidence': (
                    f"[第二级] 持仓增加 {diff} ≈ buy_shares={buy_shares}，"
                    f"判定买腿已成交、卖腿未成交，单边暴露（超买） | {base_info}"
                )}

    # 情形 B：仅卖腿成交（持仓减少 ≈ sell_shares）→ 单边暴露，真风险
    if sell_shares > 0 and _shares_match(-diff, sell_shares) and diff < 0:
        return {**_base, 'level': 2,
                'buy_filled': False, 'sell_filled': True,
                'skip_no_alert': False, 'single_side': 'sell_only',
                'evidence': (
                    f"[第二级] 持仓减少 {-diff} ≈ sell_shares={sell_shares}，"
                    f"判定卖腿已成交、买腿未成交，单边暴露 | {base_info}"
                )}

    # 情形 C：持仓 ≈ baseline（diff ≈ 0）
    # → 两腿都未成交  OR  两腿都成交（buy==sell 时净持仓回归基线）
    # → 仅凭本地持仓无法区分，绝不断言"已成交"，必须输出"无法确认"
    if _shares_match(diff, 0):
        sym_ambiguous = _shares_match(buy_shares, sell_shares)
        if sym_ambiguous:
            # buy==sell：两腿都成/两腿都未成，本地数据无法区分
            return {**_base, 'level': 2,
                    'buy_filled': False, 'sell_filled': False,
                    'skip_no_alert': False, 'single_side': '',
                    'inconclusive': True,
                    'evidence': (
                        f"[第二级] 持仓未变化（diff={diff}≈0）且 buy_shares==sell_shares，"
                        f"本地数据无法区分两腿都成交/两腿都未成交，需人工核对 | {base_info}"
                    )}
        else:
            # buy != sell 但持仓 ≈ baseline：两腿都未成交（net≠0若只一腿成交，已被上面情形捕获）
            return {**_base, 'level': 2,
                    'buy_filled': False, 'sell_filled': False,
                    'skip_no_alert': True, 'single_side': '',
                    'evidence': (
                        f"[第二级] 持仓未变化（diff={diff}≈0），buy≠sell，"
                        f"判定两腿均未成交，无过夜风险 | {base_info}"
                    )}

    # 情形 D：持仓变化不符合任何预期模式 → Level 3
    return {**_base, 'level': 3,
            'evidence': (
                f"[第三级] 持仓变化 diff={diff} 不符合预期模式，数据矛盾 | {base_info}"
            )}


# ══════════════════════════════════════════════════════════════════════════
# THS 真实持仓（源2）
# ══════════════════════════════════════════════════════════════════════════

def get_real_holding_shares(code: str) -> int:
    """
    通过 getHoldingShares 获取当前真实持仓。
    接口正常但未持仓返回 0；接口失败返回 -1。
    注意：调用前需已通过 ensure_iface()。
    """
    info = get_real_holding_info(code)
    return info['shares']


def get_real_holding_info(code: str) -> dict:
    """
    通过 getHoldingShares 获取当前真实持仓完整信息。
    返回 dict: shares / available / avg_cost
    shares=-1 表示接口失败。
    row 列顺序（与 trade.py sync 一致）：
      [0]=代码 [1]=名称 [2]=现价 [6]=总持股 [8]=可用余额 [9]=冻结 [10]=成本价
    """
    _default = {'shares': -1, 'available': 0, 'avg_cost': 0.0}
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()
        result = e.getHoldingShares()
        time.sleep(1)
        if not (result and isinstance(result, dict) and result.get('status')):
            log.warning(f"  [{code}] getHoldingShares status=False（unknown err）")
            return _default
        for row in result.get('data', []):
            if row and len(row) > 10 and str(row[0]) == code:
                return {
                    'shares':    int(float(row[6])),
                    'available': int(float(row[8])),
                    'avg_cost':  float(row[10]),
                }
        return {'shares': 0, 'available': 0, 'avg_cost': 0.0}
    except Exception as ex:
        log.warning(f"  [{code}] getHoldingShares 异常: {ex}")
        return _default


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
        'iface_ok':     bool,       # THS 接口是否可用
        'inconclusive': bool,       # True=无法确认成交状态，必须人工核对
        'evidence': {
          'file':      str,
          'holdings':  str,
          'entrust':   str,
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
        # ── 第二/三级降级：接口不可用，用本地数据判断 ─────────────────
        local_assess = assess_by_local_data(
            code, buy_shares, sell_shares,
            buy_order=buy_order, sell_order=sell_order,
        )
        ev_local = local_assess['evidence']
        level    = local_assess['level']
        inconclusive = local_assess.get('inconclusive', False)

        # 文件状态已明确（filled）时优先采信；但 inconclusive 情形下
        # 若两腿文件状态均非 filled，仍维持 inconclusive
        if file_buy_filled or file_sell_filled:
            final_buy  = file_buy_filled  or local_assess['buy_filled']
            final_sell = file_sell_filled or local_assess['sell_filled']
            # 文件已明确至少一腿，inconclusive 解除
            inconclusive = False
        else:
            final_buy  = local_assess['buy_filled']
            final_sell = local_assess['sell_filled']

        return {
            'buy_filled':     final_buy,
            'sell_filled':    final_sell,
            'iface_ok':       False,
            'local_level':    level,
            'skip_no_alert':  local_assess['skip_no_alert'],
            'single_side':    local_assess['single_side'],
            'inconclusive':   inconclusive,
            'evidence': {
                'file':     ev_file,
                'holdings': f'[第{level}级降级] {ev_local}',
                'entrust':  '接口不可用，跳过委托查询',
            }
        }

    # ── 源2：真实持仓 vs 基准持仓 ─────────────────────────────────────
    baseline, baseline_src = get_baseline_shares(code, buy_order)
    real_info   = get_real_holding_info(code)   # 含 shares / available / avg_cost
    real_shares = real_info['shares']
    log.info(f"  [{pair_id}] 基线来源: {baseline_src}，baseline={baseline}")

    if real_shares >= 0:
        excess = real_shares - baseline
        hold_buy_filled  = excess >= buy_shares
        hold_sell_filled = (baseline - real_shares) >= sell_shares
        ev_holdings = (f"real={real_shares} baseline={baseline}（{baseline_src}）"
                       f" excess={excess}"
                       f" → buy_filled={hold_buy_filled} sell_filled={hold_sell_filled}")
    else:
        hold_buy_filled  = False
        hold_sell_filled = False
        ev_holdings = f'getHoldingShares 返回 -1 | baseline={baseline}（{baseline_src}）'

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
        'buy_filled':   final_buy_filled,
        'sell_filled':  final_sell_filled,
        'iface_ok':     True,
        'inconclusive': False,
        '_entrusts':    entrusts,
        # 供 main() 仓位对齐用，iface_ok=True 且 real_shares>=0 时才可信
        '_real_info':   real_info,
        '_baseline':    baseline,
        'evidence': {
            'file':     ev_file,
            'holdings': ev_holdings,
            'entrust':  ev_entrust,
        }
    }


# ══════════════════════════════════════════════════════════════════════════
# 市价单直接下单（不经 trade.py，price=None 即对手价）
# ══════════════════════════════════════════════════════════════════════════

def place_order_market(action: str, code: str, shares: int,
                       price=None) -> tuple:
    """
    直接调 EvolvingSim.issuingEntrust(stockCode, amount, price, tradingAction)。
    price=None  → 对手价（市价单）
    price=float → 指定价格（跌停/涨停价兜底）

    返回 (ok: bool, raw_result)
    DRY_RUN 时不真实下单，返回 (True, 'DRY-RUN')。
    """
    price_label = f"@{price}" if price is not None else "@市价(None=对手价)"
    log.info(f"  [下单] {action} {code} {shares}股 {price_label}")
    if DRY_RUN or VERIFY:
        log.info(f"  [{'VERIFY' if VERIFY else 'DRY-RUN'}] 跳过真实下单")
        return True, 'DRY-RUN'
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()
        trading_action = 'buy' if action == 'buy' else 'sell'
        result = e.issuingEntrust(
            stockCode=code,
            amount=shares,
            price=price,
            tradingAction=trading_action,
        )
        time.sleep(1)
        ok = (result is not None and isinstance(result, tuple)
              and len(result) > 0 and result[0] is True)
        return ok, result
    except Exception as ex:
        log.error(f"  [下单] EvolvingSim 异常: {ex}")
        return False, None


def find_contract_from_entrust(code: str, action: str, shares: int,
                                price_hint=None) -> str:
    """
    下单后查当日委托表，匹配 代码+方向+数量，取最新一条的合同号。
    price_hint 传 None 时不过滤价格（市价单成交价不定）。
    返回合同号字符串，找不到返回空串。

    getEntrust 列：[0]日期 [1]时间 [2]代码 [3]名称 [4]操作
                   [5]备注 [6]委托数量 [7]撤销数量 [8]委托价格 [9]成交价格 [10]合同编号
    """
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()
        result = e.getEntrust('today', False)
        time.sleep(1)
        if not (result and isinstance(result, dict) and result.get('status')):
            return ''
        direction_kw = '买' if action == 'buy' else '卖'
        candidates = []
        for row in result.get('data', []):
            if not row or len(row) < 11:
                continue
            if str(row[2]) != code:
                continue
            if direction_kw not in str(row[4]):
                continue
            try:
                qty = int(float(row[6]))
            except ValueError:
                continue
            if qty != shares:
                continue
            candidates.append(row)
        if not candidates:
            return ''
        # 取最新一条（按时间字段排序，取 row[1] 最大的）
        candidates.sort(key=lambda r: str(r[1]), reverse=True)
        return str(candidates[0][10])
    except Exception as ex:
        log.warning(f"  [核验] getEntrust 查询异常: {ex}")
        return ''


# ══════════════════════════════════════════════════════════════════════════
# 平仓参数
# ══════════════════════════════════════════════════════════════════════════

# 补平下单后等待成交的最长秒数（可调整）
CLOSE_MARKET_WAIT   = 30
# 轮询间隔
CLOSE_POLL_INTERVAL = 5


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

# ══════════════════════════════════════════════════════════════════════════
# 下单
# ══════════════════════════════════════════════════════════════════════════

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
            log.info(f"  ✓ 两腿均已成交（文件/委托确认），无需操作")
        elif status.get('inconclusive'):
            shares   = buy_order.get('shares', '?')
            buy_p    = buy_order.get('price', '?')
            sell_p   = sell_order.get('price', '?')
            buy_c    = buy_order.get('contract', '?')
            sell_c   = sell_order.get('contract', '?')
            local_v  = get_local_position_shares(code)
            log.warning(
                f"  ⚠ [ALERT] 无法确认成交状态，请人工核对！\n"
                f"    配对方向: 买入{shares}股@{buy_p} / 卖出{shares}股@{sell_p}\n"
                f"    买入合同号: {buy_c}  卖出合同号: {sell_c}\n"
                f"    当前持仓: {local_v} 股\n"
                f"    原因: 买入量==卖出量，本地持仓无法区分双边成交/双边未成"
            )
        else:
            log.info(f"  ？ 两腿均未确认成交")
            if not status['iface_ok']:
                log.warning(f"  [ALERT] 接口不可用，无法确认，存在过夜风险: {code} {buy_order.get('shares')} 股")
        log.info("")

    log.info("=== verify 完成 ===")


# ══════════════════════════════════════════════════════════════════════════
# 真实持仓 vs 基线：单边敞口补平
#   判据只有一条：real_shares - baseline_shares（持仓差是最硬的证据）
#   配对文件（t0_buy/t0_sell 订单）只用来解释原因、写日志，不决定"无需动作"
# ══════════════════════════════════════════════════════════════════════════

INTENT_DIR = os.path.join(ORDERS_DIR, 'intent')

_T0_ORDER_RE  = re.compile(r'^(\d{8})_(\d+)_t0_(buy|sell)_(\d+)\.json$')
_T0_INTENT_RE = re.compile(r'^(\d{8})_(\d+)_(t0_buy|t0_sell)(_failed)?\.json$')


def _min_shares() -> int:
    try:
        import yaml as _yaml
        with open(os.path.join(ROOT_DIR, 'config.yaml'), encoding='utf-8') as f:
            cfg = _yaml.safe_load(f) or {}
        return int((cfg.get('trade') or {}).get('min_shares', 100))
    except Exception:
        return 100


def _is_filled_status(status_text) -> bool:
    s = str(status_text or '').strip()
    if not s or '未成交' in s:
        return False
    if '撤' in s or '废' in s:
        return False
    return '全部成交' in s or '已成交' in s


# ── 真实持仓读取（frida 优先，EvolvingSim 兜底） ──────────────────────────

def _read_frida_positions():
    """frida 持仓原始表。返回 (positions|None, 原因)。

    只有"拿到非空持仓表"才算读成功；接口报错/返回空 dict 一律 None（读不到），
    由调用方回退旧通道或告警——绝不把"读不到"当成"没有持仓"。
    """
    try:
        from broker_frida import ThsBroker
        pos = ThsBroker().get_positions() or {}
    except Exception as ex:
        return None, f'frida 接口报错: {ex}'
    if not pos:
        return None, 'frida 返回空持仓'
    return pos, ''


def read_all_holdings():
    """返回 ({code: shares}, 来源)；读取失败返回 (None, 原因)。"""
    pos, frida_err = _read_frida_positions()
    if pos is not None:
        return {c: int(v.get('shares') or 0) for c, v in pos.items()}, 'frida'
    log.warning(f"  frida 持仓读取失败（{frida_err}），回退 EvolvingSim")

    try:
        with open(PORTFOLIO_JSON, encoding='utf-8') as f:
            codes = list((json.load(f).get('positions') or {}).keys())
    except Exception:
        codes = []
    if not codes:
        return None, f'{frida_err}；旧通道也无持仓清单（portfolio.json 无 positions）'

    out = {}
    for c in codes:
        info = get_real_holding_info(c)
        if info['shares'] < 0:
            return None, f'{frida_err}；EvolvingSim getHoldingShares 失败({c})'
        out[c] = info['shares']
    return out, 'EvolvingSim'


def read_holding(code):
    """单只真实持仓；读不到返回 None。"""
    pos, _reason = _read_frida_positions()
    if pos is not None:
        if code not in pos:
            return 0  # 持仓表非空但没有该标的 → 确实无该标的
        return int((pos.get(code) or {}).get('shares') or 0)
    info = get_real_holding_info(code)
    return info['shares'] if info['shares'] >= 0 else None


def read_available(code):
    """单只可用数量；读不到/不在持仓表返回 None（调用方必须跳过，不得当 0）。"""
    pos, _reason = _read_frida_positions()
    if pos is not None:
        if code not in pos:
            return None  # 找不到标的 → 未知，不当 0
        return int((pos.get(code) or {}).get('available') or 0)
    info = get_real_holding_info(code)
    if info['shares'] <= 0:
        return None  # 旧通道查不到该标的（或持仓为0）→ 未知
    return info['available']


# ── 当日 T0 活动 + 基线 ──────────────────────────────────────────────────

def scan_t0_activity() -> dict:
    """扫描今日 T0 订单/意图（含不完整配对），返回 {code: {...}}。

    基线取当日最早一笔 T0 订单的 baseline_shares（trade.py 在下单前固定写入），
    它是"这笔做T开始前"的持仓，普通买卖不会污染它。
    """
    today = _today_compact()
    act: dict = {}

    def entry(code):
        return act.setdefault(code, {'orders': [], 'intents': [],
                                     'baseline': None, 'baseline_src': ''})

    try:
        fnames = sorted(os.listdir(ORDERS_DIR))
    except OSError:
        fnames = []
    for fname in fnames:
        m = _T0_ORDER_RE.match(fname)
        if not m or m.group(1) != today:
            continue
        try:
            with open(os.path.join(ORDERS_DIR, fname), encoding='utf-8') as f:
                d = json.load(f)
        except Exception:
            continue
        entry(m.group(2))['orders'].append({
            'file': fname, 'side': m.group(3), 'x': m.group(4),
            'status': str(d.get('status') or ''),
            'shares': int(d.get('shares') or 0),
            'price': float(d.get('price') or 0),
            'contract': str(d.get('contract') or ''),
            'baseline_shares': d.get('baseline_shares'),
            'created_at': str(d.get('created_at') or ''),
        })

    if os.path.isdir(INTENT_DIR):
        for fname in sorted(os.listdir(INTENT_DIR)):
            m = _T0_INTENT_RE.match(fname)
            if not m or m.group(1) != today:
                continue
            try:
                with open(os.path.join(INTENT_DIR, fname), encoding='utf-8') as f:
                    d = json.load(f)
            except Exception:
                continue
            entry(m.group(2))['intents'].append({
                'file': fname, 'action': m.group(3),
                'status': str(d.get('status') or ''),
                'shares': d.get('shares'),
                'created_at': str(d.get('created_at') or ''),
            })

    for e in act.values():
        cands = [o for o in e['orders'] if o.get('baseline_shares') is not None]
        cands.sort(key=lambda o: (o['created_at'], o['file']))
        if cands:
            e['baseline'] = int(cands[0]['baseline_shares'])
            e['baseline_src'] = f"T0首单 {cands[0]['file']}"
    return act


def resolve_baseline(code, activity, snapshot):
    """返回 (baseline, 来源)。优先级: T0首单基线 → 开盘快照 → _signal_state 兜底。"""
    e = activity.get(code)
    if e and e.get('baseline') is not None:
        return int(e['baseline']), e['baseline_src']
    if snapshot and code in snapshot:
        return int(snapshot[code]), '开盘基线快照'
    try:
        with open(PORTFOLIO_JSON, encoding='utf-8') as f:
            pf = json.load(f)
        v = (pf.get('_signal_state', {}).get(code) or {}).get('shares')
        if v is None:
            v = (pf.get('positions', {}).get(code) or {}).get('shares')
        if v is not None:
            return int(v), '兜底 _signal_state（基线缺失）'
    except Exception:
        pass
    return None, '基线缺失'


def describe_t0_activity(act) -> str:
    """把配对文件翻译成人读的"原因"（只写日志，不参与决策）。"""
    if not act:
        return '无T0记录'
    legs = []
    for side in ('buy', 'sell'):
        for o in act.get('orders', []):
            if o['side'] != side:
                continue
            legs.append(f"t0_{side} {o['shares']}股@{o['price']:.3f} "
                        f"status={o['status'] or 'unknown'}"
                        f"{' contract=' + o['contract'] if o['contract'] else ''}")
    for it in act.get('intents', []):
        legs.append(f"intent {it['action']} status={it['status'] or '未标注'}")
    if not legs:
        return '无T0订单文件'
    sides = {o['side'] for o in act.get('orders', [])}
    tail = ' → 单边腿（配对不完整）' if len(sides) == 1 else ''
    return ' + '.join(legs) + tail


# ── 行情 / 涨跌停 ────────────────────────────────────────────────────────

def get_quote(code) -> dict:
    try:
        from market_data import fetch_realtime_quotes
        return (fetch_realtime_quotes([code]) or {}).get(code) or {}
    except Exception as ex:
        log.warning(f"  [{code}] 行情获取失败: {ex}")
        return {}


def calc_close_limit_price(code, action, quote):
    """平仓限价：买用卖一/现价，卖用买一/现价，并按当日涨跌停夹边界。

    返回 (price, limit_down, limit_up, note)；price<=0 表示不可下单。
    """
    from money_manager import day_price_limits

    try:
        cur = float(quote.get('price') or 0)
        prev_close = float(quote.get('prev_close') or 0)
    except (TypeError, ValueError):
        cur, prev_close = 0.0, 0.0

    limit_down, limit_up = day_price_limits(code, prev_close, cur)
    if limit_up <= 0 or limit_down <= 0:
        return 0.0, limit_down, limit_up, f'涨跌停价无效(昨收={prev_close}, 现价={cur})'

    label = '卖一' if action == 'buy' else '买一'
    try:
        raw = float((quote.get('ask1') if action == 'buy' else quote.get('bid1')) or 0)
    except (TypeError, ValueError):
        raw = 0.0
    if raw <= 0:
        raw, label = cur, '现价'
    if raw <= 0:
        return 0.0, limit_down, limit_up, '现价/盘口均不可用'

    price = round(min(max(raw, limit_down), limit_up), 3)
    note = ''
    if abs(price - raw) > 1e-9:
        note = f'{label}{raw:.3f} 越界，按涨跌停边界取 {price:.3f}'
    return price, limit_down, limit_up, note


# ── 撤单 / 查在途 ────────────────────────────────────────────────────────

def _cancel_contract_only(contract) -> bool:
    """只撤指定合同号（绝不降级全撤，避免误撤别的标的）。"""
    if not contract or contract == 'pending':
        return False
    try:
        from broker_frida import ThsBroker
        ThsBroker().cancel_contract(contract, real=True)
        log.info(f"  撤单成功(frida) contract={contract}")
        return True
    except Exception as ex:
        log.warning(f"  frida 撤单失败({ex})，回退 EvolvingSim")
    try:
        from evolving.evolving import EvolvingSim
        r = EvolvingSim().revokeEntrust(revokeType='contractNo', contractNo=contract)
        time.sleep(2)
        if r is not None:
            log.info(f"  撤单成功(EvolvingSim) contract={contract}")
            return True
    except Exception as ex:
        log.error(f"  EvolvingSim 撤单异常: {ex}")
    return False


def _mark_order_cancelled(fname: str) -> None:
    path = os.path.join(ORDERS_DIR, fname)
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        d['status'] = 'cancelled'
        d['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as ex:
        log.warning(f"  标记订单 cancelled 失败 {fname}: {ex}")


def _cancel_pending_t0_legs(code, act):
    """撤掉该标的今日未成交的 T0 腿，防补平后对侧腿再成交造成双倍敞口。"""
    pending = [o for o in (act or {}).get('orders', [])
               if str(o.get('status') or '') in ('pending', 'pending_confirm', 'partial')]
    if not pending:
        return True, []
    for o in pending:
        contract = str(o.get('contract') or '')
        if DRY_RUN or VERIFY:
            log.info(f"  [{'VERIFY' if VERIFY else 'DRY-RUN'}] 将撤未成交T0腿 "
                     f"{code} {o.get('side')} contract={contract or 'pending'}")
            continue
        if not _cancel_contract_only(contract):
            log.error(f"[ALERT] [{code}] 撤未成交T0腿失败 contract={contract or 'pending'}，"
                      f"跳过补平（防对侧腿成交造成双倍敞口），请人工处理")
            return False, []
        _mark_order_cancelled(o['file'])
    return True, [str(o.get('contract') or '') for o in pending]


def _has_pending_order(code, action):
    """当日是否已有同方向未成交委托（补平幂等检查）。"""
    direction = '买入' if action == 'buy' else '卖出'
    try:
        from broker_frida import ThsBroker
        for o in ThsBroker().get_orders() or []:
            if o.get('code') != code or direction not in str(o.get('direction') or ''):
                continue
            st = str(o.get('status') or '')
            if '撤' in st or '废' in st:
                continue
            if _is_filled_status(st):
                continue
            return True, o
    except Exception as ex:
        log.warning(f"  [{code}] 查当日委托失败: {ex}")
    return False, None


def _clear_chase_state_for_code(code: str) -> None:
    """补平成功后清掉该标的今日追价记录，避免收盘后继续追。"""
    chase_path = os.path.join(ORDERS_DIR, 't0_chase_state.json')
    if not os.path.exists(chase_path):
        return
    try:
        with open(chase_path, encoding='utf-8') as f:
            chase = json.load(f)
        keys = [k for k in chase if k.startswith(_today_compact()) and code in k]
        if not keys:
            return
        for k in keys:
            del chase[k]
        with open(chase_path, 'w', encoding='utf-8') as f:
            json.dump(chase, f, ensure_ascii=False, indent=2)
        log.info(f"  [{code}] 追价记录已清除: {keys}")
    except Exception as ex:
        log.warning(f"  [{code}] 清除追价记录失败: {ex}")


# ── 补平下单 / 等成交 ────────────────────────────────────────────────────

def _place_close_order(action, code, shares, price):
    """补平下单：frida 优先；已发出但未确认时禁止回退（防重复下单）。"""
    if DRY_RUN or VERIFY:
        log.info(f"  [{'VERIFY' if VERIFY else 'DRY-RUN'}] 跳过真实下单: "
                 f"{action} {code} {shares}股 @{price:.3f}")
        return True, 'DRY-RUN', {'via': 'dry-run'}

    try:
        from broker_frida import ThsBroker
        r = ThsBroker().place(action, code, shares, price) or {}
        contract = str(r.get('contract') or '').strip()
        log.info(f"  下单通道=frida 合同号={contract or '未取到'} status={r.get('status')}")
        return bool(contract), contract, r
    except Exception as ex:
        msg = str(ex)
        if '已发送' in msg or '未拿到合同号' in msg:
            log.error(f"  frida 已发送但未确认，禁止回退重下: {msg}")
            return False, '', {'error': msg}
        log.warning(f"  frida 下单失败({msg})，回退 EvolvingSim")

    ok, raw = place_order_market(action, code, shares, price)
    contract = find_contract_from_entrust(code, action, shares) if ok else ''
    log.info(f"  下单通道=EvolvingSim 合同号={contract or '未取到'}")
    return ok, contract, raw


def _wait_close_fill(code, action, shares, contract, baseline, fallback_price,
                     timeout=None):
    """等补平成交：先看委托状态，再看真实持仓是否回归基线。"""
    timeout = CLOSE_MARKET_WAIT if timeout is None else timeout
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(CLOSE_POLL_INTERVAL)
        if contract and contract != 'DRY-RUN':
            try:
                from broker_frida import ThsBroker
                o = ThsBroker().query_contract(contract)
                if o and _is_filled_status(o.get('status')):
                    px = float(o.get('avg_fill_price') or 0) or fallback_price
                    log.info(f"  ✅ 成交确认: contract={contract} "
                             f"状态={o.get('status')} 成交价={px:.3f}")
                    return True, px
            except Exception:
                pass
        h = read_holding(code)
        if h is not None and abs(h - baseline) <= _SHARE_TOLERANCE:
            log.info(f"  ✅ 成交确认: 真实持仓已回归基线 real={h} baseline={baseline}")
            return True, fallback_price
        log.info(f"  等待成交中… 合同号={contract or '未取到'}")
    return False, fallback_price


def reconcile_code(code, diff, baseline, real_before, act):
    """按 real-baseline 差额补平。True=已补平/干跑；False=失败(记过夜风险)；None=无需动作。"""
    action = 'buy' if diff < 0 else 'sell'
    direction = '买入' if action == 'buy' else '卖出'
    min_shares = _min_shares()
    shares = abs(int(diff)) // 100 * 100
    reason = describe_t0_activity(act)

    log.info(f"[补平] 标的={code} baseline={baseline} real={real_before} diff={diff} "
             f"→ 需{direction} {shares}股 | 配对解释: {reason}")

    if shares < min_shares:
        log.error(f"[ALERT] [{code}] 差额 {abs(diff)} 不足 {min_shares} 股，无法补平，请人工核对")
        return False

    # 先撤掉未成交的 T0 腿（可能是这条敞口的对侧腿），再按最新持仓补平
    ok, _canceled = _cancel_pending_t0_legs(code, act)
    if not ok:
        return False

    # 撤单后重新取真实持仓，按最新差额下单（撤单期间可能有成交）
    real_now = read_holding(code)
    if real_now is None:
        log.error(f"[ALERT] [{code}] 补平前读真实持仓失败，不交易")
        return False
    diff_now = real_now - baseline
    if abs(diff_now) < min_shares:
        log.info(f"  [{code}] 撤单后差额={diff_now}，已无敞口，无需补平")
        return None

    if (diff_now > 0) != (diff > 0):
        action = 'buy' if diff_now < 0 else 'sell'
        direction = '买入' if action == 'buy' else '卖出'
    diff, real_before = diff_now, real_now
    shares = abs(diff) // 100 * 100

    # 补平幂等：已有同方向挂单（例如上一轮补平单还在途）则本轮不再下
    has_pending, order = _has_pending_order(code, action)
    if has_pending:
        log.info(f"  [{code}] 已有{direction}挂单 contract={order.get('contract')} "
                 f"status={order.get('status')}，本次跳过（幂等）")
        return None

    if action == 'sell':
        avail = read_available(code)
        if avail is None:
            log.error(f"[ALERT] [{code}] 可用量读取失败（不在持仓表/接口异常），跳过补平，存在过夜风险")
            return False
        if 0 <= avail < shares:
            log.warning(f"  [{code}] 可用 {avail} < 需补卖 {shares}，按可用数量下单")
            shares = avail // 100 * 100
        if shares < min_shares:
            log.error(f"[ALERT] [{code}] 可补卖数量不足 {min_shares} 股（可用={avail}），请人工核对")
            return False

    quote = get_quote(code)
    price, limit_down, limit_up, note = calc_close_limit_price(code, action, quote)
    if price <= 0:
        log.error(f"[ALERT] [{code}] 平仓价无效: {note}")
        return False
    log.info(f"  [{code}] 平仓价={price:.3f}（{note or '盘口/现价'}） "
             f"涨停={limit_up:.3f} 跌停={limit_down:.3f} 现价={quote.get('price')}")

    if DRY_RUN or VERIFY:
        log.info(f"  [{'VERIFY' if VERIFY else 'DRY-RUN'}] 将补平 {direction} {code} "
                 f"{shares}股 @{price:.3f}（差额={diff}，涨跌停边界校验通过）")
        log.info(f"[平仓完成(干跑)] 标的={code} 差额={diff} 方向={direction} 合同号=DRY-RUN "
                 f"成交价={price:.3f} 数量={shares} 平仓后持仓(预期)={baseline}")
        return True

    ok, contract, raw = _place_close_order(action, code, shares, price)
    if not ok:
        log.error(f"[ALERT] [{code}] 补平下单失败: {raw}")
        return False

    filled, fill_price = _wait_close_fill(code, action, shares, contract, baseline, price)
    if not filled:
        log.warning(f"  [{code}] {CLOSE_MARKET_WAIT}s 未确认成交，撤单")
        if contract:
            _cancel_contract_only(contract)
        log.error(f"[ALERT] [{code}] 补平未成交已撤单，存在过夜风险（差额={diff}）")
        return False

    real_after = read_holding(code)
    log.info(f"[平仓完成] 标的={code} 差额={diff} 方向={direction} 合同号={contract} "
             f"成交价={fill_price:.3f} 数量={shares} 平仓后持仓={real_after} "
             f"(baseline={baseline} real_before={real_before}) 依据={reason}")
    _log_record(code, action, shares, fill_price, '真实持仓vs基线补平', True, contract)
    _clear_chase_state_for_code(code)

    if real_after is not None and abs(real_after - baseline) > _SHARE_TOLERANCE:
        log.error(f"[ALERT] [{code}] 补平后仍有差额 real={real_after} baseline={baseline}，请人工核对")
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════

def main():
    mode_tag = '[DRY-RUN]' if DRY_RUN else ('[VERIFY]' if VERIFY else '')
    log.info(f"=== T0尾盘平仓开始 {mode_tag} ===")

    holdings, src = read_all_holdings()
    if holdings is None:
        log.error(f"[ALERT] 取数失败，未平仓，存在过夜风险（{src}），为安全不交易，退出码 2")
        sys.exit(2)

    activity = scan_t0_activity()
    try:
        from core.daily_baseline import load_baseline
        snapshot = load_baseline()
    except Exception as ex:
        log.warning(f"  读取开盘基线快照失败: {ex}")
        snapshot = {}

    log.info(f"真实持仓来源={src}，持仓 {len(holdings)} 只；当日T0活动 {len(activity)} 只；"
             f"开盘基线快照 {len(snapshot)} 只")

    # 逐只打印 baseline / real / diff（无动作也打），配对文件只作解释
    rows = []
    for code in sorted(set(holdings) | set(activity)):
        real = int(holdings.get(code, 0))
        base, bsrc = resolve_baseline(code, activity, snapshot)
        diff = (real - base) if base is not None else None
        rows.append((code, real, base, diff, activity.get(code)))
        explain = describe_t0_activity(activity.get(code)) if code in activity else '无当日T0活动'
        log.info(f"[核对] 标的={code} baseline={base if base is not None else '缺失'}"
                 f"({bsrc}) real={real} diff={diff if diff is not None else 'N/A'} | {explain}")

    for code, real, base, diff, act in rows:
        if diff is None:
            log.warning(f"  [{code}] 基线缺失，无法判定，跳过（请人工核对）")
            continue
        if code not in activity:
            if diff:
                log.info(f"  [{code}] 非T0标的（diff={diff} 来自普通买卖/建仓），不动作")
            continue
        if diff == 0:
            log.info(f"  [{code}] diff=0，无需动作 | 配对解释: {describe_t0_activity(act)}")
            continue
        try:
            result = reconcile_code(code, diff, base, real, act)
        except Exception as ex:
            log.error(f"[ALERT] [{code}] 补平过程异常: {ex}")
            result = False
        if result is False:
            _overnight_risk.append((code, abs(diff)))

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
    from core.exec_lock import ExecLock
    _lock = ExecLock('t0_close')
    if not _lock.acquire():
        sys.exit(0)
    try:
        if VERIFY:
            main_verify()
        else:
            main()
    finally:
        _lock.release()
