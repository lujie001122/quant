"""
共享监控标的模块 — 统一读取 etf_pool.json + portfolio.json
signal_generator / sentiment_check / backtest 均从本模块导入，消除重复代码。
"""

import json
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
_POOL_PATH = os.path.join(_DIR, "etf_pool.json")
_PF_PATH = os.path.join(_DIR, "portfolio.json")


# ── 默认回退（etf_pool.json 不存在时使用） ──────────────────────────
_DEFAULT_CODES = ["159516", "515880", "588170", "159532", "515050"]
_DEFAULT_NAMES = {
    "159516": "半导体设备ETF",
    "515880": "通信ETF",
    "588170": "科创半导体ETF",
    "159532": "ETF",
    "515050": "中证全指ETF",
}
_DEFAULT_SIDS = {
    "159516": "sz159516",
    "515880": "sh515880",
    "588170": "sh588170",
    "159532": "sz159532",
    "515050": "sh515050",
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


def _load_portfolio():
    """加载 portfolio.json，不存在则返回空 dict"""
    if not os.path.exists(_PF_PATH):
        return {}
    try:
        with open(_PF_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_tracked_codes():
    """
    返回去重合并后的监控标的代码列表。
    来源: etf_pool.json 的 codes + portfolio.json 的 positions keys。
    """
    pool = _load_pool()
    pf = _load_portfolio()

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
    pf = _load_portfolio()

    pool_names = pool.get("names", {}) if pool else {}
    pool_sids = pool.get("sids", {}) if pool else {}
    positions = pf.get("positions", {})

    # 默认回退 sid/name（仅用于 pool 不存在时的默认标的）
    default_sids = dict(_DEFAULT_SIDS)
    default_names = dict(_DEFAULT_NAMES)

    result = {}
    for code in get_tracked_codes():
        # name: pool > portfolio > default > code
        name = (pool_names.get(code)
                or positions.get(code, {}).get("name")
                or default_names.get(code)
                or code)
        # sid: pool > default > auto-generate
        sid = pool_sids.get(code) or default_sids.get(code)
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
    返回 {code: {name, fund, base_spacing, style}} 配置字典（供 signal_generator 使用）。
    与 signal_generator._build_etfs_config() 逻辑一致。
    """
    pool = _load_pool()
    pf = _load_portfolio()

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
            # 已在默认列表中，用 portfolio 中的名称覆盖默认名称
            name = pos_data.get("name")
            if name and name != _DEFAULT_NAMES.get(code, ""):
                etfs[code]["name"] = name

    return etfs


def get_tracked_with_keywords():
    """
    返回 {code: {name, kw}}，供 sentiment_check 使用。
    kw = [name, name+"ETF"(如果不以ETF结尾), code]
    """
    pool = _load_pool()
    pf = _load_portfolio()

    tracked = {}

    # 从 pool 构建
    if pool:
        codes = pool.get("codes", [])
        names = pool.get("names", {})
        for code in codes:
            name = names.get(code, code)
            kw = [name]
            if not name.endswith("ETF"):
                kw.append(name + "ETF")
            kw.append(code)
            tracked[code] = {"name": name, "kw": kw}
    else:
        # 回退
        _DEFAULT_TRACKED = {
            "159516": {"name": "半导体设备ETF", "kw": ["半导体设备ETF", "半导体设备ETFETF", "159516"]},
            "515880": {"name": "通信ETF", "kw": ["通信ETF", "通信ETFETF", "515880"]},
            "588170": {"name": "科创半导体ETF", "kw": ["科创半导体ETF", "科创半导体ETFETF", "588170"]},
            "159532": {"name": "中证2000ETF", "kw": ["中证2000ETF", "中证2000ETFETF", "159532"]},
            "515050": {"name": "中证全指ETF", "kw": ["中证全指ETF", "中证全指ETFETF", "515050"]},
        }
        tracked = dict(_DEFAULT_TRACKED)

    # 合并 portfolio 实际持仓
    for code, pos_data in pf.get("positions", {}).items():
        name = pos_data.get("name", code)
        if code not in tracked:
            kw = [name]
            if not name.endswith("ETF"):
                kw.append(name + "ETF")
            kw.append(code)
            tracked[code] = {"name": name, "kw": kw}
        else:
            # 已在列表中，用 portfolio 中的名称覆盖（如果不同）
            if name and name != tracked[code]["name"]:
                tracked[code]["name"] = name
                kw = [name]
                if not name.endswith("ETF"):
                    kw.append(name + "ETF")
                kw.append(code)
                tracked[code]["kw"] = kw

    return tracked