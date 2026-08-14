#!/usr/bin/env python3
"""
state_center.py — 统一状态中心

从 tracker.py 升级，提供持仓/订单/行情/信号状态的统一读写接口。
目标是消除 signal_generator.py 和 trade.py 之间的重复代码。

功能：
  - ETF 配置读取（来自 tracker.py）
  - Portfolio 读写（持仓、账户、信号状态）
  - 订单管理（orders/ 目录读写、同步同花顺、全撤）
  - Intent 文件管理（防重复下单）
  - 信号状态持久化
  - 行情数据获取辅助

用法：
  from state_center import (
      SubAccount, StateCenter, get_main_position, get_t0_position,
      get_main_cash, get_t0_cash,
      get_tracked_codes, get_code_map, get_etfs_config,
      load_portfolio, save_portfolio,
      get_position_shares, get_position_info,
      load_today_orders, load_intent_files,
      intent_to_dedup_key, write_intent, cleanup_intent_files,
      check_pending_orders, sync_entrust_to_orders, revoke_all,
      trade_type_to_mode, get_signal_direction, signal_direction,
      build_retry_cmd, detect_error_type,
  )
"""

import json
import os
import sys
import time
from datetime import datetime

# ====================================================================
# 〇、子账户模型（做T与主策略逻辑隔离）
# ====================================================================

class SubAccount:
    """逻辑子账户 — 持仓与资金隔离"""
    def __init__(self, name: str):
        self.name = name  # "main" 或 "t0"
        self.positions = {}  # symbol -> {volume, avg_cost, realized_pnl}
        self.cash = 0.0
        self.initial_cash = 0.0  # 初始现金（用于盈亏计算）


class StateCenter:
    """全局状态中心 — 管理主策略与做T两个逻辑子账户"""
    _instance = None

    def __init__(self):
        self.main_account = SubAccount("main")
        self.t0_account = SubAccount("t0")

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_position(self, symbol: str, account: str = "main"):
        """获取指定子账户的持仓信息"""
        if account == "main":
            return self.main_account.positions.get(symbol)
        else:
            return self.t0_account.positions.get(symbol)

    def set_position(self, symbol: str, pos: dict, account: str = "main"):
        """设置指定子账户的持仓"""
        if account == "main":
            self.main_account.positions[symbol] = pos
        else:
            self.t0_account.positions[symbol] = pos

    def get_account(self, account: str = "main") -> SubAccount:
        """获取子账户对象"""
        if account == "t0":
            return self.t0_account
        return self.main_account

    # ── 现金管理（做T与主策略现金池隔离）──
    def get_cash(self, account: str = "main") -> float:
        """获取指定子账户的可用现金"""
        if account == "main":
            return self.main_account.cash
        else:
            return self.t0_account.cash

    def update_cash(self, amount: float, account: str = "main"):
        """更新指定子账户的现金余额"""
        if account == "main":
            self.main_account.cash += amount
        else:
            self.t0_account.cash += amount

    def get_total_cash(self) -> float:
        """获取总现金（main + t0）"""
        return self.main_account.cash + self.t0_account.cash

    def get_daily_pnl(self) -> float:
        """获取当日盈亏（基于初始现金）"""
        total = self.get_total_cash()
        initial = self.main_account.initial_cash + self.t0_account.initial_cash
        return total - initial

    def get_total_asset(self) -> float:
        """获取总资产（现金 + 持仓市值）"""
        return self.get_total_cash()  # 简化版，实际需加持仓市值

    def sync_from_portfolio(self, portfolio_data: dict):
        """从 portfolio.json 同步账户状态到 StateCenter"""
        account = portfolio_data.get("account", {})
        cash = account.get("cash", 0.0)
        total_asset = account.get("total_asset", 0.0)

        # 默认按 80/20 分配（主策略80%，做T20%）
        if self.main_account.initial_cash == 0:
            self.main_account.initial_cash = cash * 0.8
            self.t0_account.initial_cash = cash * 0.2
            self.main_account.cash = self.main_account.initial_cash
            self.t0_account.cash = self.t0_account.initial_cash


def get_main_position(code: str):
    """便捷函数：获取主策略子账户持仓"""
    return StateCenter.get_instance().get_position(code, account="main")


def get_t0_position(code: str):
    """便捷函数：获取做T子账户持仓"""
    return StateCenter.get_instance().get_position(code, account="t0")


def get_main_cash() -> float:
    """便捷函数：获取主策略子账户现金"""
    return StateCenter.get_instance().get_cash(account="main")


def get_t0_cash() -> float:
    """便捷函数：获取做T子账户现金"""
    return StateCenter.get_instance().get_cash(account="t0")


# ── 路径常量 ──────────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
_POOL_PATH = os.path.join(_DIR, "etf_pool.json")
_PF_PATH = os.path.join(_DIR, "portfolio.json")
_ORDERS_DIR = os.path.join(_DIR, "orders")
_INTENT_DIR = os.path.join(_ORDERS_DIR, "intent")


# ====================================================================
# 一、ETF 配置（来自 tracker.py，保持向后兼容）
# ====================================================================

# ── 默认回退 ──────────────────────────────────────────────────────────
_DEFAULT_CODES = ["159516", "515880", "588170", "159532", "515050", "159611", "512170"]
_DEFAULT_NAMES = {
    "159516": "半导体设备ETF",
    "515880": "通信ETF",
    "588170": "科创半导体ETF",
    "159532": "中证2000ETF",
    "515050": "中证全指ETF",
    "159611": "电力ETF",
    "512170": "医疗ETF",
}
_DEFAULT_SIDS = {
    "159516": "sz159516",
    "515880": "sh515880",
    "588170": "sh588170",
    "159532": "sz159532",
    "515050": "sh515050",
    "159611": "sz159611",
    "512170": "sh512170",
}


def _load_pool():
    """加载 etf_pool.json，不存在或无效则返回 None"""
    if not os.path.exists(_POOL_PATH):
        return None
    try:
        with open(_POOL_PATH, "r", encoding="utf-8") as f:
            pool = json.load(f)
        if pool.get("codes") and len(pool["codes"]) >= 3:
            return pool
    except Exception:
        pass
    return None


def load_portfolio():
    """加载 portfolio.json，不存在则返回空 dict"""
    if not os.path.exists(_PF_PATH):
        return {}
    try:
        with open(_PF_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_portfolio(pf_data):
    """保存 portfolio.json"""
    try:
        with open(_PF_PATH, "w", encoding="utf-8") as f:
            json.dump(pf_data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"[state_center] ⚠️ 保存 portfolio 失败: {e}")


def get_tracked_codes():
    """
    返回去重合并后的监控标的代码列表。
    来源: etf_pool.json 的 codes + portfolio.json 的 positions keys。
    """
    pool = _load_pool()
    pf = load_portfolio()

    codes = set()
    if pool:
        codes.update(pool["codes"])
    else:
        codes.update(_DEFAULT_CODES)
    codes.update(pf.get("positions", {}).keys())
    return sorted(codes)


def get_code_map():
    """
    返回 {code: {"name": str, "sid": str}} 的映射。
    - sid 优先从 etf_pool.json 取；portfolio-only 标的自动生成（5→sh, 1→sz）
    - 名称优先从 pool 取，不存在则从 portfolio 的 positions[code].name 取
    """
    pool = _load_pool()
    pf = load_portfolio()

    pool_names = pool.get("names", {}) if pool else {}
    pool_sids = pool.get("sids", {}) if pool else {}
    positions = pf.get("positions", {})

    result = {}
    for code in get_tracked_codes():
        name = (pool_names.get(code)
                or positions.get(code, {}).get("name")
                or _DEFAULT_NAMES.get(code)
                or code)
        sid = pool_sids.get(code) or _DEFAULT_SIDS.get(code)
        if not sid:
            if code.startswith("5"):
                sid = f"sh{code}"
            elif code.startswith("1"):
                sid = f"sz{code}"
            else:
                sid = code
        result[code] = {"name": name, "sid": sid}

    return result


def get_etfs_config(fund_per_etf=44000):
    """
    返回 {code: {name, fund, base_spacing, style}} 配置字典。
    与 signal_generator._build_etfs_config() 逻辑一致。
    """
    pool = _load_pool()
    pf = load_portfolio()

    etfs = {}

    if pool:
        codes = pool["codes"]
        names = pool.get("names", {})
        for code in codes:
            etfs[code] = {
                "name": names.get(code, code),
                "fund": fund_per_etf,
                "base_spacing": 0.025,
                "style": "轮动标的",
            }
    else:
        for code in _DEFAULT_CODES:
            base_sp = 0.03 if code in ("588170", "159532") else 0.025
            etfs[code] = {
                "name": _DEFAULT_NAMES.get(code, code),
                "fund": fund_per_etf,
                "base_spacing": base_sp,
                "style": "核心高波动仓位",
            }

    # 合并 portfolio 实际持仓
    for code, pos_data in pf.get("positions", {}).items():
        if code not in etfs:
            etfs[code] = {
                "name": pos_data.get("name", code),
                "fund": fund_per_etf,
                "base_spacing": 0.025,
                "style": "实际持仓",
            }
        else:
            name = pos_data.get("name")
            if name and name != _DEFAULT_NAMES.get(code, ""):
                etfs[code]["name"] = name

    return etfs


def get_tracked_with_keywords():
    """
    返回 {code: {name, kw}}，供 sentiment_check 使用。
    kw = [name, name+"ETF"(如果不以ETF结尾), code] + industry_kw(从 etf_pool.json)
    """
    pool = _load_pool()
    pf = load_portfolio()

    industry_kw_map = pool.get("industry_kw", {}) if pool else {}

    tracked = {}

    if pool:
        codes = pool.get("codes", [])
        names = pool.get("names", {})
        for code in codes:
            name = names.get(code, code)
            kw = [name]
            if not name.endswith("ETF"):
                kw.append(name + "ETF")
            kw.append(code)
            ikw = industry_kw_map.get(code, [])
            for k in ikw:
                if k not in kw:
                    kw.append(k)
            tracked[code] = {"name": name, "kw": kw}
    else:
        _DEFAULT_INDUSTRY_KW = {
            "159516": ["半导体", "芯片", "光刻", "CPO", "光模块"],
            "515880": ["通信", "5G", "6G", "光通信", "CPO", "光模块"],
            "588170": ["科创", "半导体", "芯片", "科创板"],
            "159532": ["中证2000", "小盘", "微盘"],
            "515050": ["全指", "A股", "沪深"],
            "159611": ["电力", "绿电", "新能源"],
            "512170": ["医疗", "医药", "医疗器械", "生物医药", "创新药"],
        }
        _DEFAULT_TRACKED = {
            "159516": {"name": "半导体设备ETF", "kw": ["半导体设备ETF", "159516"]},
            "515880": {"name": "通信ETF", "kw": ["通信ETF", "515880"]},
            "588170": {"name": "科创半导体ETF", "kw": ["科创半导体ETF", "588170"]},
            "159532": {"name": "中证2000ETF", "kw": ["中证2000ETF", "159532"]},
            "515050": {"name": "中证全指ETF", "kw": ["中证全指ETF", "515050"]},
            "159611": {"name": "电力ETF", "kw": ["电力ETF", "159611"]},
            "512170": {"name": "医疗ETF", "kw": ["医疗ETF", "512170"]},
        }
        for code, ikw in _DEFAULT_INDUSTRY_KW.items():
            if code in _DEFAULT_TRACKED:
                for k in ikw:
                    if k not in _DEFAULT_TRACKED[code]["kw"]:
                        _DEFAULT_TRACKED[code]["kw"].append(k)
        tracked = dict(_DEFAULT_TRACKED)

    # 合并 portfolio 实际持仓
    for code, pos_data in pf.get("positions", {}).items():
        name = pos_data.get("name", code)
        if code not in tracked:
            kw = [name]
            if not name.endswith("ETF"):
                kw.append(name + "ETF")
            kw.append(code)
            ikw = industry_kw_map.get(code, [])
            for k in ikw:
                if k not in kw:
                    kw.append(k)
            tracked[code] = {"name": name, "kw": kw}
        else:
            if name and name != tracked[code]["name"]:
                tracked[code]["name"] = name
                kw = [name]
                if not name.endswith("ETF"):
                    kw.append(name + "ETF")
                kw.append(code)
                ikw = industry_kw_map.get(code, [])
                for k in ikw:
                    if k not in kw:
                        kw.append(k)
                tracked[code]["kw"] = kw

    return tracked


# ====================================================================
# 二、持仓信息查询
# ====================================================================

def get_position_shares(code):
    """从 portfolio.json 读取当前持仓股数"""
    pf = load_portfolio()
    return pf.get("positions", {}).get(code, {}).get("shares", 0)


def get_position_info(code):
    """从 portfolio.json 读取指定标的的持仓信息"""
    pf = load_portfolio()
    return pf.get("positions", {}).get(code, {})


def get_all_positions():
    """返回所有持仓 {code: pos_dict}"""
    pf = load_portfolio()
    return pf.get("positions", {})


def get_account_info():
    """返回账户信息 dict"""
    pf = load_portfolio()
    return pf.get("account", {})


def get_signal_state():
    """返回 _signal_state dict"""
    pf = load_portfolio()
    return pf.get("_signal_state", {})


def save_signal_state(state_dict, account_update=None):
    """保存信号状态到 portfolio.json"""
    pf = load_portfolio()
    pf["_signal_state"] = state_dict
    if account_update:
        pf["account"] = account_update
        pf["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_portfolio(pf)


# ====================================================================
# 三、订单管理（orders/ 目录）
# ====================================================================

def load_today_orders():
    """从 orders/ 目录加载今日所有订单，返回 {filename: order_dict}"""
    if not os.path.exists(_ORDERS_DIR):
        return {}
    today = datetime.now().strftime('%Y%m%d')
    orders = {}
    for fname in os.listdir(_ORDERS_DIR):
        if fname.startswith(today) and fname.endswith('.json'):
            try:
                with open(os.path.join(_ORDERS_DIR, fname)) as f:
                    order = json.load(f)
                orders[fname] = order
            except (json.JSONDecodeError, IOError):
                pass
    return orders


def load_intent_files():
    """从 orders/intent/ 目录加载今日所有 intent 文件，返回 {filename: intent_dict}"""
    if not os.path.exists(_INTENT_DIR):
        return {}
    today = datetime.now().strftime('%Y%m%d')
    intents = {}
    for fname in os.listdir(_INTENT_DIR):
        if fname.startswith(today) and fname.endswith('.json'):
            try:
                with open(os.path.join(_INTENT_DIR, fname)) as f:
                    intent = json.load(f)
                intents[fname] = intent
            except (json.JSONDecodeError, IOError):
                pass
    return intents


def intent_to_dedup_key(intent):
    """从 intent 文件推导 dedup 三元组 (code, mode, direction)。
    intent['action'] 可以是 'buy'/'sell'/'t0_buy'/'t0_sell'。"""
    code = intent.get('code', '')
    action = intent.get('action', '')
    direction = intent.get('direction', '')
    if 't0_' in action:
        mode = 't0'
    else:
        mode = 'buy_sell'
    if not direction:
        direction = '买入' if 'buy' in action else '卖出'
    return (code, mode, direction)


def write_intent(code, action, direction, mode=None):
    """写 intent 文件到 orders/intent/ 目录。
    action: 'buy'/'sell'/'t0_buy'/'t0_sell'
    direction: '买入'/'卖出'
    mode: 自动推导，不传则根据 action 推导
    """
    os.makedirs(_INTENT_DIR, exist_ok=True)
    today = datetime.now().strftime('%Y%m%d')
    if mode is None:
        mode = 't0' if 't0_' in action else 'buy_sell'
    intent_path = os.path.join(_INTENT_DIR, f'{today}_{code}_{action}.json')
    intent = {
        'code': code,
        'action': action,
        'direction': direction,
        'mode': mode,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    try:
        with open(intent_path, 'w') as f:
            json.dump(intent, f, ensure_ascii=False, indent=2)
    except IOError:
        pass


def write_intent_failed(code, action, shares, price, reason='failed'):
    """下单失败时写 intent 文件（status=failed），下次 cron 可去重识别。"""
    os.makedirs(_INTENT_DIR, exist_ok=True)
    today = datetime.now().strftime('%Y%m%d')
    intent_path = os.path.join(_INTENT_DIR, f'{today}_{code}_{action}_failed.json')
    direction = '买入' if 'buy' in action else '卖出'
    intent = {
        'code': code,
        'action': action,
        'shares': shares,
        'price': price,
        'direction': direction,
        'status': 'failed',
        'reason': reason,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    try:
        with open(intent_path, 'w') as f:
            json.dump(intent, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"  ⚠️ failed intent 文件写入失败: {e}")


def write_order(code, action, shares, price, contract, status='pending'):
    """写订单到 orders/ 目录。"""
    os.makedirs(_ORDERS_DIR, exist_ok=True)
    today = datetime.now().strftime('%Y%m%d')
    order_path = os.path.join(_ORDERS_DIR, f'{today}_{code}_{action}.json')
    order = {
        'code': code,
        'action': action,
        'shares': shares,
        'price': price,
        'contract': contract,
        'status': status,
        'direction': '买入' if 'buy' in action else '卖出',
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    try:
        with open(order_path, 'w') as f:
            json.dump(order, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"  ⚠️ 订单持久化失败: {e}")


def update_order_status(code, action, new_status):
    """更新 orders/ 中订单的状态。"""
    today = datetime.now().strftime('%Y%m%d')
    order_path = os.path.join(_ORDERS_DIR, f'{today}_{code}_{action}.json')
    if not os.path.exists(order_path):
        print(f"  ⚠️ 订单文件不存在: {order_path}")
        return
    try:
        with open(order_path, 'r') as f:
            order = json.load(f)
        order['status'] = new_status
        order['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(order_path, 'w') as f:
            json.dump(order, f, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, IOError) as e:
        print(f"  ⚠️ 更新订单状态失败: {e}")


# ====================================================================
# 四、同花顺委托同步 + 全撤
# ====================================================================

def sync_entrust_to_orders(e=None, orders=None):
    """通过 EvolvingSim.getEntrust 同步同花顺委托状态到本地 orders/ 文件。
    将 pending 的订单根据同花顺实际状态更新为 filled/revoked。
    e: 可选复用的 EvolvingSim 实例，None 则自建
    orders: 可选预加载的今日订单 {filename: order_dict}，None 则自加载
    """
    from evolving.evolving import EvolvingSim
    own_e = e is None
    if own_e:
        e = EvolvingSim()
    try:
        ent = e.getEntrust('today', False)
        time.sleep(2)
    except Exception as ex:
        print(f"[sync_entrust] ⚠️ EvolvingSim 调用失败: {ex}")
        return

    if not (isinstance(ent, dict) and ent.get('status') and ent.get('data')):
        print(f"[sync_entrust] ⚠️ getEntrust 返回异常，跳过同步")
        return

    # 构建同花顺委托索引: {code: {direction: status}}
    ths_map = {}
    for row in ent['data']:
        if not row or len(row) <= 10:
            continue
        code = row[2]
        direction = row[4]
        status = row[5]
        if code not in ths_map:
            ths_map[code] = {}
        ths_map[code][direction] = status

    # 遍历本地 pending 订单，同步同花顺状态
    if orders is None:
        orders = load_today_orders()
    updated = 0

    for fname, order in orders.items():
        if order.get('status') != 'pending':
            continue
        code = order.get('code', '')
        direction = order.get('direction', '')
        if code not in ths_map or direction not in ths_map[code]:
            continue
        ths_status = ths_map[code][direction]

        new_status = None
        if ths_status in ('已成交', '全部成交', '部分成交'):
            new_status = 'filled'
        elif ths_status in ('已撤单', '已撤销', '废单'):
            new_status = 'revoked'

        if new_status:
            order_path = os.path.join(_ORDERS_DIR, fname)
            try:
                order['status'] = new_status
                order['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                with open(order_path, 'w') as f:
                    json.dump(order, f, ensure_ascii=False, indent=2)
                updated += 1
                print(f"[sync_entrust] {code} {direction} pending→{new_status} (同花顺:{ths_status})")
            except (IOError, json.JSONDecodeError) as ex:
                print(f"[sync_entrust] ⚠️ 更新 {fname} 失败: {ex}")

    if updated:
        print(f"[sync_entrust] 同步完成: {updated} 笔订单状态已更新")


def revoke_all(e=None, orders=None):
    """全撤：调用 EvolvingSim 撤销全部买卖委托，并标记本地 pending 订单为 revoked。
    e: 可选复用的 EvolvingSim 实例，None 则自建
    orders: 可选预加载的今日订单 {filename: order_dict}，None 则自加载
    """
    from evolving.evolving import EvolvingSim
    own_e = e is None
    if own_e:
        e = EvolvingSim()
    revoke_ok = False
    try:
        result = e.revokeEntrust(revokeType='allBuyAndSell')
        time.sleep(2)
        if result is not None:
            revoke_ok = True
            print(f"  ✅ 全撤成功")
    except Exception as ex:
        print(f"  ⚠️ revokeEntrust 异常: {ex}")

    # 标记本地 pending 订单为 revoked
    if orders is None:
        orders = load_today_orders()
    marked = 0
    for fname, order in orders.items():
        if order.get('status') == 'pending':
            order_path = os.path.join(_ORDERS_DIR, fname)
            try:
                order['status'] = 'revoked'
                order['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                with open(order_path, 'w') as f:
                    json.dump(order, f, ensure_ascii=False, indent=2)
                marked += 1
            except (IOError, json.JSONDecodeError) as ex:
                print(f"  ⚠️ 标记 revoked 失败 {fname}: {ex}")
    if marked:
        print(f"  ✅ 标记 {marked} 笔 pending 订单为 revoked")

    # 清理 intent 文件
    cleanup_intent_force()

    return revoke_ok


def check_pending_orders(mode=None, sync_entrust=True):
    """检查 orders/ 目录中今日的挂单状态，同步同花顺委托状态。
    mode: 'buy_sell' | 't0' | None=全部
    sync_entrust: 是否同步同花顺委托状态（默认 True）。
    返回: (pending_map, filled_codes)
      pending_map: {code: direction} 今日未成交的挂单（受 mode 过滤）
      filled_codes: set of (code, mode, direction) 今日已成交/已挂单 + intent 文件 + failed 订单（去重用）
    """
    if sync_entrust:
        sync_entrust_to_orders()

    orders = load_today_orders()
    pending_map = {}
    filled_codes = set()

    # 1. 收集订单文件的 filled/pending
    for fname, order in orders.items():
        code = order.get('code', '')
        direction = order.get('direction', '')
        status = order.get('status', '')
        action = order.get('action', '')
        order_mode = 't0' if 't0_' in action else 'buy_sell'

        # filled_codes 不受 mode 过滤，全部收集（去重用）
        if status in ('filled', 'pending', 'failed'):
            filled_codes.add((code, order_mode, direction))

        # pending_map 受 mode 过滤
        if mode and order_mode != mode:
            continue

        if status == 'pending':
            pending_map[code] = direction

    # 2. 收集 intent 文件（下单前写入的意图记录，防同 cron 内重复）
    intents = load_intent_files()
    for fname, intent in intents.items():
        intent_key = intent_to_dedup_key(intent)
        filled_codes.add(intent_key)
        print(f"[check_pending] intent 去重: {intent_key} (来源:{fname})")

    return pending_map, filled_codes


# ====================================================================
# 五、工具函数
# ====================================================================

def trade_type_to_mode(action):
    """将 action 字段映射为 mode: 'buy_sell' | 't0'
    action 是 trade_type 值: 'buy'/'sell'/'liquidate'/'reduce' → 'buy_sell'; 't0' → 't0'
    """
    if action == "t0":
        return "t0"
    return "buy_sell"


def get_signal_direction(trade_type, action):
    """从信号 trade_type 和 action 推导方向: '买入' | '卖出' | 'unknown'"""
    if trade_type == "buy":
        return "买入"
    if trade_type in ("sell", "liquidate", "reduce"):
        return "卖出"
    if trade_type == "t0":
        if "买入" in action:
            return "买入"
        if "卖出" in action:
            return "卖出"
    return "unknown"


def signal_direction(sig):
    """从信号判定交易方向：'买入' | '卖出'"""
    trade_type = sig.get("trade_type", "")
    action = sig.get("action", "")
    if trade_type in ("buy", "t0"):
        return "买入" if "买入" in action else "卖出"
    if trade_type in ("sell", "liquidate", "reduce"):
        return "卖出"
    return None


def detect_error_type(stdout, stderr, returncode, exit_reason=None):
    """从 subprocess 输出中检测错误类型。
    返回 (error_type, detail)
    error_type: 'tonghuashun_disconnect' | 'not_filled' | 'timeout' | 'other'
    """
    combined = (stdout or "") + "\n" + (stderr or "")

    if exit_reason == "timeout":
        return "timeout", "下单超时(120s)"

    if "连接同花顺失败" in combined or "EvolvingSim 连接失败" in combined or "同花顺" in combined:
        return "tonghuashun_disconnect", "同花顺断连"

    if "下单成功但持仓未变" in combined or "期望增量" in combined:
        return "not_filled", "下单未成交(增量确认失败)"

    if returncode != 0:
        return "other", f"退出码={returncode}"

    return None, None


def build_retry_cmd(trade_script, code, trade_type, sig, price, shares):
    """构建重试命令字符串"""
    if trade_type in ("buy", "sell", "liquidate", "reduce"):
        return f"python3 trade.py {trade_type if trade_type in ('buy','sell') else 'sell'} {code} {shares} {price:.3f}"
    elif trade_type == "t0":
        t0_pair = sig.get("t0_pair")
        if t0_pair:
            pair_price = t0_pair.get("pair_price", 0)
            pair_shares = t0_pair.get("shares", 0)
            if "买入" in sig.get("action", ""):
                return f"python3 trade.py t0_buy {code} {pair_shares} {price:.3f} {pair_price:.3f}"
            else:
                return f"python3 trade.py t0_sell {code} {pair_shares} {price:.3f} {pair_price:.3f}"
        else:
            if "买入" in sig.get("action", ""):
                return f"python3 trade.py buy {code} {shares} {price:.3f}"
            else:
                return f"python3 trade.py sell {code} {shares} {price:.3f}"
    return ""


def parse_position_ratio(ratio_str):
    """从 position_ratio 字符串中提取比例(0-1浮点数)
    例: "30%(RSI抄底)" → 0.30, "5%(活动仓)" → 0.05, "15%(补仓)" → 0.15
    """
    if not ratio_str:
        return 0.0
    try:
        pct_str = ratio_str.split("%")[0]
        return float(pct_str) / 100.0
    except (ValueError, IndexError):
        return 0.0


# ====================================================================
# 六、Intent 清理
# ====================================================================

def cleanup_intent_files():
    """收盘后清理订单意图文件。当日 15:00 后调用，删除所有 intent 文件。"""
    now = datetime.now()
    if now.hour < 15:
        return

    if not os.path.exists(_INTENT_DIR):
        return

    today = now.strftime('%Y%m%d')
    removed = 0
    for fname in os.listdir(_INTENT_DIR):
        if fname.startswith(today) and fname.endswith('.json'):
            try:
                os.remove(os.path.join(_INTENT_DIR, fname))
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"[cleanup_intent] 清理 {removed} 个 intent 文件（收盘后）")


def cleanup_intent_force():
    """无条件清理所有 intent 文件（用于 revoke_all 或手动清理）。"""
    if not os.path.exists(_INTENT_DIR):
        return
    removed = 0
    for fname in os.listdir(_INTENT_DIR):
        if fname.endswith('.json'):
            try:
                os.remove(os.path.join(_INTENT_DIR, fname))
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"[cleanup_intent_force] 清理 {removed} 个 intent 文件")


def cleanup_old_intent_files():
    """清理非今日的 intent 文件（残留清理）。"""
    if not os.path.exists(_INTENT_DIR):
        return
    today = datetime.now().strftime('%Y%m%d')
    removed = 0
    for fname in os.listdir(_INTENT_DIR):
        if fname.endswith('.json') and not fname.startswith(today):
            try:
                os.remove(os.path.join(_INTENT_DIR, fname))
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"[cleanup_old_intent] 清理 {removed} 个过期 intent 文件")


# ====================================================================
# 七、行情数据辅助
# ====================================================================

def fetch_and_build_tech(code, etfs_config):
    """获取指定标的的日线/5分钟数据并计算技术指标。
    返回 tech dict 或 None。
    """
    from market_data import (
        fetch_klines_daily, fetch_klines_5min,
        calc_ma, calc_rsi_wilder, calc_macd, calc_atr, calc_ao,
        calc_vol_ratio_120min, calc_5min_indicators, dynamic_spacing,
    )
    try:
        klines = fetch_klines_daily(code)
        if not klines:
            return None
        closes = [k["close"] for k in klines]
        volumes = [k["volume"] for k in klines]
        ma5 = calc_ma(closes, 5)
        ma10 = calc_ma(closes, 10)
        ma20 = calc_ma(closes, 20)
        rsi = calc_rsi_wilder(closes, 14)
        dif, dea, macd_bar, macd_status = calc_macd(closes)
        vol_ratio_120 = calc_vol_ratio_120min(volumes, 20)
        atr = calc_atr(klines, 14)
        base_s = etfs_config.get(code, {}).get("base_spacing", 0.025)
        price = closes[-1] if closes else 0
        spacing = dynamic_spacing(atr, price, base_s)

        highs = [k["high"] for k in klines]
        lows = [k["low"] for k in klines]
        ao_now, ao_5ago = calc_ao(highs, lows)

        klines_5min = fetch_klines_5min(code)
        indicators_5min = calc_5min_indicators(klines_5min)

        return {
            "ma5": ma5, "ma10": ma10, "ma20": ma20,
            "rsi": rsi, "dif": dif, "dea": dea, "macd_bar": macd_bar,
            "macd_status": macd_status, "vol_ratio_120": vol_ratio_120,
            "atr": atr, "spacing": spacing,
            "vol_ratio_5min": indicators_5min["vol_ratio_5min"] if indicators_5min else None,
            "atr_5min": indicators_5min["atr_5min"] if indicators_5min else None,
            "rsi_5min": indicators_5min["rsi_5min"] if indicators_5min else None,
            "macd_5min_status": indicators_5min["macd_5min_status"] if indicators_5min else None,
            "macd_5min_dif": indicators_5min["macd_5min_dif"] if indicators_5min else None,
            "macd_5min_dea": indicators_5min["macd_5min_dea"] if indicators_5min else None,
            "macd_5min_bar": indicators_5min["macd_5min_bar"] if indicators_5min else None,
            "day_high": indicators_5min["day_high"] if indicators_5min else None,
            "day_low": indicators_5min["day_low"] if indicators_5min else None,
            "day_pct": indicators_5min["day_pct"] if indicators_5min else None,
            "ma20_5min_slope": indicators_5min["ma20_5min_slope"] if indicators_5min else None,
            "ao_now": ao_now, "ao_5ago": ao_5ago,
        }
    except Exception as e:
        print(f"[state_center] ⚠️ fetch_and_build_tech({code}) 失败: {e}")
        return None