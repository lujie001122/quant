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
_DEFAULT_CODES = ["159516", "515880", "588170", "159532", "515050", "159611", "513780"]
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
        if pool.get("etf_pool") and len(pool["etf_pool"]) >= 3:
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
        codes.update(pool["etf_pool"])
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
        codes = pool["etf_pool"]
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
    kw = [name, name+"ETF"(如果不以ETF结尾), code] + industry_kw(从 etf_pool.json)
    """
    pool = _load_pool()
    pf = _load_portfolio()

    # 行业关键词 (从 etf_pool.json)
    industry_kw_map = pool.get("industry_kw", {}) if pool else {}

    tracked = {}

    # 从 pool 构建
    if pool:
        codes = pool.get("etf_pool", [])
        names = pool.get("names", {})
        for code in codes:
            name = names.get(code, code)
            kw = [name]
            if not name.endswith("ETF"):
                kw.append(name + "ETF")
            kw.append(code)
            # 合并行业关键词
            ikw = industry_kw_map.get(code, [])
            for k in ikw:
                if k not in kw:
                    kw.append(k)
            tracked[code] = {"name": name, "kw": kw}
    else:
        # 回退
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
            # 行业关键词
            ikw = industry_kw_map.get(code, [])
            for k in ikw:
                if k not in kw:
                    kw.append(k)
            tracked[code] = {"name": name, "kw": kw}
        else:
            # 已在列表中，用 portfolio 中的名称覆盖（如果不同）
            if name and name != tracked[code]["name"]:
                tracked[code]["name"] = name
                kw = [name]
                if not name.endswith("ETF"):
                    kw.append(name + "ETF")
                kw.append(code)
                # 合并行业关键词
                ikw = industry_kw_map.get(code, [])
                for k in ikw:
                    if k not in kw:
                        kw.append(k)
                tracked[code]["kw"] = kw

    return tracked