#!/usr/bin/env python3
"""
量化交易信号生成器 v3.1
10项核心策略逻辑优化:
  P0: 分级减仓替代单一清仓 | 底仓/活动仓真正分离
  P1: 建仓门槛提高+分批 | 做T弹性条件 | 移动止盈 | 网格重置确认
  P2: 防御盾对冲 | 波动率自适应频率 | 空仓期 | 网格熔断
基础: akshare/Wilder RSI/SMA预热MACD/ATR动态间距/倒金字塔/5分钟量比
# 标的: 159516, 515880, 588170, 159532, 515050, 159611, 513780, 512400, 515220
# 策略: 震荡双模式 v4
# 资金: 各上限4.4万, 总计22万(9只共享)

"""

import json
import os
import re
import sys
from datetime import datetime

# ── 日志降噪: 设 QUANT_DEBUG=1 环境变量开启详细日志 ──
_DEBUG = os.environ.get("QUANT_DEBUG", "").strip() in ("1", "true", "yes")

# ── 行情数据与指标 ──
from market_data import (
    fetch_realtime_quotes, fetch_klines_daily, fetch_klines_5min,
    calc_ma, calc_rsi_wilder, calc_macd, calc_atr, calc_ao,
    calc_vol_ratio_120min, calc_5min_indicators, dynamic_spacing,
    ETFS, data_source_report,
)
# ── 组合层择时（与 backtest_bt.py 共用同一份实现 + config.yaml）──
import timing as _timing_mod

# ── 持仓信息模型 + 常量 ──
import trailing
from position_info import (
    PositionInfo,
    DEAD_RATIO, ACTIVE_RATIO, TOTAL_FUND, DEFENSE_CODE, MAX_POSITION_RATIO,
)

# ── 仓位管理器 ──
from core.position_manager import get_position_manager

# ── 交易记录器 ──
from core.trade_recorder import get_recorder

# ── 策略判定 ──
from strategies.rsi_macd import (
    check_defense,
)
from strategies.grid import (
    evaluate_grid_signals, check_grid_reset, compute_grid_table,
    check_grid_overall_stop, is_v2_enabled,
)
from strategies.rsi_macd import RSIMACDStrategy
from strategies.t0 import T0Strategy

_rsi_macd_strategy = RSIMACDStrategy()
_t0_strategy = T0Strategy()


def evaluate_stop(pos, t, price, today_str):
    """评估止盈止损信号（统一入口：委托给 RiskManager，避免逻辑重复）"""
    from risk_manager import check_stop_loss as _rm_check_stop
    return _rm_check_stop(pos, t, price, today_str)


def resolve_stop_signal(pos, stop_actions):
    """解析止盈止损信号（统一入口：委托给 RiskManager）"""
    from risk_manager import resolve_stop_signal as _rm_resolve_stop
    return _rm_resolve_stop(pos, stop_actions)


def evaluate_entry(pos, t, price, realtime, positions, all_klines, code, today_str, atr_pct, defense_weak):
    return _rsi_macd_strategy.evaluate_entry(pos, t, price, realtime, positions, all_klines, code, today_str, atr_pct, defense_weak)


def evaluate_t0(t, pos, price, today_str, atr_pct):
    return _t0_strategy.evaluate_t0(t, pos, price, today_str, atr_pct)


def evaluate_t0_execute(result, t, pos, price, today_str, atr_pct, record_t0=True, available_shares=None,
                        prev_close=None, code=None):
    return _t0_strategy.evaluate_t0_execute(result, t, pos, price, today_str, atr_pct, record_t0,
                                            available_shares=available_shares, prev_close=prev_close, code=code)

# ── 状态中心（执行层已迁移到 executor.py，此文件仅保留需要的导入） ──


# ============================================================
# 一、配置(仅 signal_generator 独有的常量)
# ============================================================

# ============================================================
# 轮动TOP3保护
# ============================================================

# 轮动建议文件(由 rotation.py 每周一生成, 集中金字塔TOP3模式写 all_picks)
ROTATION_ADVICE_PATH = "/tmp/rotation_advice.json"

# 日内减仓信号类型(轮动TOP3标的禁止触发):
#   破MA5卖活动仓5% / 趋势止盈(原版+增强) / 阶梯止盈卖活动仓 / 分级止损减仓30%
# 注意: 清仓类(liquidate_*)、轮动清仓(跌出TOP3)不受此集合限制,仍正常执行
INTRADAY_REDUCE_SIG_TYPES = frozenset({
    "trend_profit_sell",
    "trend_profit_sell_enhanced",
    "sell_active_5pct",
    "sell_active_10pct",
    "sell_active_15pct",
    "sell_active_20pct",
    "reduce_30pct_all",
})


def load_rotation_top3_codes():
    """读取轮动TOP3标的代码集合(rotation_advice.json 的 all_picks / picks)。

    返回:
      set : 轮动TOP3标的代码集合
      None: 文件不存在或读取失败 → 打印告警，不拦截，不触发轮动清仓
    """
    try:
        with open(ROTATION_ADVICE_PATH, encoding="utf-8") as _f:
            _data = json.load(_f)
        _picks = _data.get("all_picks") or _data.get("picks") or []
        return set(_picks)
    except Exception:
        # 【五】rotation文件缺失时显式告警：取消TOP3保护，但不触发轮动清仓
        _today = datetime.now().strftime("%Y-%m-%d")
        print(f"[WARN] rotation_advice.json 读取失败，TOP3保护已取消（文件={ROTATION_ADVICE_PATH}，日期={_today}）"
              f"；轮动清仓本次不执行（无数据不清仓）")
        return None


# ============================================================
# 综合信号生成
# ============================================================

def generate_signals(positions=None, all_klines=None, all_tech=None):
    """
    positions: 每个标的的PositionInfo(含状态追踪)
    all_klines: 120分钟K线数据(已获取)
    all_tech: 技术指标数据(已计算)
    首次调用时会获取数据并计算指标
    """
    if positions is None:
        positions = {code: PositionInfo() for code in ETFS}

    today_str = datetime.now().strftime("%Y-%m-%d")

    # 读取 etf_pool.json 获取监控池标的（非监控池仅允许卖出）
    pool_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etf_pool.json")
    pool_codes = set()
    if os.path.exists(pool_path):
        try:
            with open(pool_path) as _f:
                _pool = json.load(_f)
            pool_codes = set(_pool.get("etf_pool", []))
        except Exception:
            pass

    # 读取 portfolio.json 判断哪些标的今日已交易
    pf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")
    already_traded_today = set()
    pf_data = {}
    # 被调出池子但仍持仓的标的（需要额外监控）
    orphan_codes = set()
    if os.path.exists(pf_path):
        try:
            with open(pf_path) as _f:
                pf_data = json.load(_f)
            for code, pos_data in pf_data.get("positions", {}).items():
                if pos_data.get("shares", 0) > 0:
                    for t in pos_data.get("trades", []):
                        if t.get("date") == today_str:
                            already_traded_today.add(code)
                            break
            # 识别"调出池子但仍持仓"的孤儿标的
            for code, pos_data in pf_data.get("positions", {}).items():
                if pos_data.get("shares", 0) > 0 and code not in pool_codes and code not in ETFS:
                    orphan_codes.add(code)
                    print(f"[ORPHAN] {code} 已调出etf_pool但仍持仓{pos_data['shares']}股，纳入监控")
        except Exception:
            print("[WARN] portfolio.json 读取失败，跳过孤儿标的识别")

    # ── 孤儿标的补充进 positions / all_klines / all_tech ──
    # 从 market_data.ETFS 取不到 orphan 的静态配置，用 etf_pool.json 的 names/sids 补全
    _orphan_meta = {}
    if orphan_codes and os.path.exists(pool_path):
        try:
            with open(pool_path) as _f:
                _pool_full = json.load(_f)
            _pool_names = _pool_full.get("names", {})
            _pool_sids  = _pool_full.get("sids", {})
            for oc in orphan_codes:
                _orphan_meta[oc] = {
                    "name": _pool_names.get(oc, oc),
                    "sid":  _pool_sids.get(oc, oc),
                    "fund": 50000,       # 孤儿标的只做卖出，fund 仅占位
                    "base_spacing": 0.01,
                }
        except Exception:
            pass

    # 孤儿标的加入 positions（若尚未存在）
    for oc in orphan_codes:
        if oc not in positions:
            positions[oc] = PositionInfo()

    # 恢复持仓状态（遍历范围扩展到 ETFS + orphan_codes）
    _all_monitored_codes = set(ETFS.keys()) | orphan_codes
    if pf_data:
        try:
            # 恢复持仓状态
            saved_state = pf_data.get("_signal_state", {})
            for code in _all_monitored_codes:
                if code in positions:
                    p = positions[code]
                    # 从 _signal_state 恢复信号状态
                    if code in saved_state:
                        s = saved_state[code]
                        # Bug N: 不从 _signal_state 恢复 shares/avg_cost/current_price
                        # 这些字段由 portfolio positions 独写（下一行），避免旧值覆盖
                        _saved_shares = s.pop("shares", None)
                        _saved_avg_cost = s.pop("avg_cost", None)
                        _saved_cur_price = s.pop("current_price", None)
                        p.from_dict(s)
                        # 恢复 _signal_state 中的字段供后续使用
                        if _saved_shares is not None: s["shares"] = _saved_shares
                        if _saved_avg_cost is not None: s["avg_cost"] = _saved_avg_cost
                        if _saved_cur_price is not None: s["current_price"] = _saved_cur_price
                        # 恢复daily_trade_log（当天）
                        saved_log = s.get("daily_trade_log", {})
                        if today_str in saved_log:
                            p.daily_trade_log[today_str] = saved_log[today_str]
                    # 持仓数量从 portfolio 恢复（即使没有 _signal_state 也要恢复持仓）
                    pos_pf = pf_data["positions"].get(code, {})
                    p.shares = pos_pf.get("shares", 0)
                    if p.shares > 0:
                        p.avg_cost = pos_pf["avg_cost"]
                        p.current_price = pos_pf.get("current_price", 0.0)
                        p.base_price = pos_pf.get("base_price") or pos_pf["avg_cost"]
                        p.update_dead_active()
                        # 持仓恢复后：如果 build_phase=0 但实际有持仓，修正为 build_phase=1
                        if p.has_position and p.build_phase == 0:
                            p.build_phase = 1
                            p.add_count = 0
                        # 反向死锁修复: build_phase>0 但无持仓(下单失败/未成交) → 复位,
                        # 否则 evaluate_entry 的 phase 分支永远不命中, 该标的永久无法再建仓
                        if (not p.has_position) and p.build_phase and p.build_first_date:
                            try:
                                _stuck_days = (datetime.strptime(today_str, "%Y-%m-%d")
                                               - datetime.strptime(p.build_first_date, "%Y-%m-%d")).days
                            except Exception:
                                _stuck_days = 0
                            if _stuck_days >= 2:
                                print(f"[状态自愈] {code} build_phase={p.build_phase} 但持仓0股"
                                      f"(首批{_stuck_days}天前) → 复位为未建仓")
                                p.build_phase = 0
                                p.build_first_price = 0.0
                                p.build_first_date = None
                                p.peak_price = 0.0
                                p.entry_avg_cost = 0.0
                                p.add_count = 0
                                p.confirm_batch_count = 0
                                p.confirm_batch_date = None
                                p.trailing_stop_price = 0.0
                        # entry_avg_cost初始化: 如果为0但实际有持仓, 用当前avg_cost
                        if p.entry_avg_cost == 0 and p.shares > 0:
                            p.entry_avg_cost = p.avg_cost
                    # Bug11: last_grid_trigger跨天重置 — 用日期字段判断
                    if p.last_grid_trigger and p.last_grid_trigger_date != today_str:
                        p.last_grid_trigger = None
                        p.last_grid_trigger_date = None
        except Exception:
            print("[WARN] portfolio.json 读取失败，跳过状态恢复")
            pass

    # ── 门控状态记录 + blocked→allowed 自动触发轮动 ──
    # 状态文件路径
    _gate_state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate_state.json")

    def _load_gate_state():
        try:
            if os.path.exists(_gate_state_path):
                with open(_gate_state_path) as _gf:
                    return json.load(_gf)
        except Exception:
            pass
        return {}

    def _save_gate_state(state):
        try:
            with open(_gate_state_path, "w") as _gf:
                json.dump(state, _gf, ensure_ascii=False, indent=2)
        except Exception as _ge:
            print(f"[WARN] gate_state.json 写入失败: {_ge}")

    # 获取数据
    try:
        realtime = fetch_realtime_quotes()
    except Exception as e:
        print(f"[WARN] fetch_realtime_quotes失败: {e}")
        realtime = {code: {"name": ETFS[code]["name"], "price": 0, "pct_change": 0, "change": 0,
                           "volume": 0, "amount": 0, "high": 0, "low": 0, "open": 0, "prev_close": 0}
                    for code in ETFS}

    # 孤儿标的补充行情（fetch_realtime_quotes 只覆盖 ETFS，需单独补取）
    for _oc in orphan_codes:
        if _oc not in realtime:
            try:
                _oc_rt = fetch_realtime_quotes([_oc]) if _oc not in realtime else {}
                if _oc_rt and _oc in _oc_rt:
                    realtime[_oc] = _oc_rt[_oc]
                else:
                    _oc_meta = _orphan_meta.get(_oc, {})
                    realtime[_oc] = {
                        "name": _oc_meta.get("name", _oc), "price": 0, "pct_change": 0,
                        "change": 0, "volume": 0, "amount": 0,
                        "high": 0, "low": 0, "open": 0, "prev_close": 0,
                    }
            except Exception:
                _oc_meta = _orphan_meta.get(_oc, {})
                realtime[_oc] = {
                    "name": _oc_meta.get("name", _oc), "price": 0, "pct_change": 0,
                    "change": 0, "volume": 0, "amount": 0,
                    "high": 0, "low": 0, "open": 0, "prev_close": 0,
                }

    # ── 缓存行情到 StateCenter ──
    try:
        from state_center import StateCenter as _SC
        _sc = _SC.get_instance()
        _sc.set_cached_prices({code: realtime.get(code, {}).get("price", 0) for code in ETFS if code in realtime})
        _sc.mark_today_open()
        _open_asset = _sc._today_open_asset
        if _DEBUG:
            print(f"[SC] 缓存行情已更新, mark_today_open={'%.0f' % _open_asset if _open_asset else 'N/A'}")
    except Exception as _sc_err:
        print(f"[WARN] StateCenter 缓存行情失败: {_sc_err}")

    # 如果没有传入K线和指标数据，需要获取
    if all_klines is None:
        all_klines = {}
        for code in ETFS:
            all_klines[code] = fetch_klines_daily(code)
        # 孤儿标的 K 线
        for _oc in orphan_codes:
            try:
                all_klines[_oc] = fetch_klines_daily(_oc)
            except Exception:
                all_klines[_oc] = []

    if all_tech is None:
        all_tech = {}
        # 统一处理 ETFS + 孤儿标的
        _tech_codes = list(ETFS.keys()) + [oc for oc in orphan_codes if oc not in ETFS]
        for code in _tech_codes:
            _is_orphan = code not in ETFS
            _cfg_base_spacing = _orphan_meta.get(code, {}).get("base_spacing", 0.01) if _is_orphan else ETFS[code]["base_spacing"]
            klines = all_klines.get(code, [])
            if not klines:
                all_tech[code] = {"macd_status": "数据不足"}
                continue
            closes = [k["close"] for k in klines]
            volumes = [k["volume"] for k in klines]
            ma5 = calc_ma(closes, 5)
            ma10 = calc_ma(closes, 10)
            ma20 = calc_ma(closes, 20)
            rsi = calc_rsi_wilder(closes, 14)
            dif, dea, macd_bar, macd_status = calc_macd(closes)
            vol_ratio_120 = calc_vol_ratio_120min(volumes, 20)
            atr = calc_atr(klines, 14)
            base_s = _cfg_base_spacing
            _cur_price = realtime.get(code, {}).get("price", closes[-1] if closes else 1)
            spacing = dynamic_spacing(atr, _cur_price, base_s)

            # AO momentum
            highs = [k["high"] for k in klines]
            lows = [k["low"] for k in klines]
            ao_now, ao_5ago = calc_ao(highs, lows)

            # 5分钟分时指标
            klines_5min = fetch_klines_5min(code)
            indicators_5min = calc_5min_indicators(klines_5min)

            all_tech[code] = {
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

    # 网格基准价（兼容孤儿标的：realtime 可能不含该 code）
    base_prices = {}
    for code, pos in positions.items():
        _rt_price = realtime.get(code, {}).get("price", 0)
        _tech_entry = all_tech.get(code, {})
        if pos.has_position and pos.base_price and _rt_price and _tech_entry.get("spacing"):
            price = _rt_price
            spacing = _tech_entry["spacing"]
            new_base, reset, new_reset_date = check_grid_reset(
                price, pos.base_price, spacing, today_str, pos.last_reset_date
            )
            if reset:
                pos.base_price = new_base
                pos.last_reset_date = new_reset_date
            base_prices[code] = pos.base_price
        else:
            base_prices[code] = _rt_price

    # P2: 市场环境过滤(检查防御标的是否也弱势)
    defense_weak = check_defense(DEFENSE_CODE, all_tech, realtime)

    # ── 大盘择时: 防守期禁止新建仓 ──
    _regime_is_defense = False
    try:
        import yaml as _yaml
        _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        with open(_cfg_path) as _cf:
            _conf = _yaml.safe_load(_cf)
        _regime_cfg = _conf.get("regime_filter", {})
        _regime_enabled = _regime_cfg.get("enabled", False)
        if _regime_enabled:
            _market_code = _regime_cfg.get("market_code", "510300")
            _ma_period = int(_regime_cfg.get("ma_period", 200))
            _confirm_days = int(_regime_cfg.get("confirm_days", 2))
            _needed = _ma_period + _confirm_days + 5
            import datetime as _dt
            _end_str = _dt.date.today().strftime("%Y%m%d")
            _start_str = (_dt.date.today() - _dt.timedelta(days=int(_needed * 1.8))).strftime("%Y%m%d")
            _mklines = fetch_klines_daily(_market_code, start=_start_str, end=_end_str)
            if _mklines and len(_mklines) >= _ma_period:
                _closes = [k["close"] for k in _mklines]
                _ma_val = sum(_closes[-_ma_period:]) / _ma_period
                _cur = _closes[-1]
                _prev = _closes[-2] if len(_closes) >= 2 else _cur
                if not hasattr(generate_signals, "_regime_state"):
                    generate_signals._regime_state = "attack"
                if _cur < _ma_val and _prev < _ma_val:
                    generate_signals._regime_state = "defense"
                elif _cur > _ma_val and _prev > _ma_val:
                    generate_signals._regime_state = "attack"
                _regime_is_defense = (generate_signals._regime_state == "defense")
    except Exception:
        pass  # 获取失败默认不限制

    # ── 仓位管理器: 动态资金分配（替代固定 cfg["fund"]） ──
    _pm = get_position_manager()
    active_codes = [code for code in ETFS if code in realtime and realtime[code].get("price", 0) > 0]
    # Bug C 修复: 传入 actual_total_asset 给 allocate_fund，而非用 config 固定值
    _total_asset_for_alloc = TOTAL_FUND
    try:
        from state_center import StateCenter as _SC_alloc
        _sc_alloc = _SC_alloc.get_instance()
        _sc_alloc_ta = _sc_alloc.get_total_asset()
        if _sc_alloc_ta and _sc_alloc_ta > 0:
            _total_asset_for_alloc = _sc_alloc_ta
    except Exception:
        _total_asset_for_alloc = pf_data.get("account", {}).get("total_asset", TOTAL_FUND) if pf_data else TOTAL_FUND
    _allocated_funds = _pm.allocate_fund(active_codes, actual_total_asset=_total_asset_for_alloc)
    if _DEBUG:
        print(f"[PM] 资金分配(总资产={_total_asset_for_alloc:.0f}): {_allocated_funds}")

    # ══════════════════════════════════════════════════════════
    # 组合层择时（R2）: 与回测 backtest_bt.py 共用 timing.py + config.yaml
    #   输出: 三档总仓位乘数（0.5/0.8/1.0），买入目标仓位统一乘该系数
    # ══════════════════════════════════════════════════════════
    _timing_cfg = _timing_mod.load_config()
    _timing_mult = 1.0
    _timing_score = 0
    _timing_detail = "timing-disabled"
    _timing_source = "none"
    _timing_degraded = False
    _timing_prev_level = None
    try:
        _timing_prev_level = _load_gate_state().get("timing_level")
    except Exception:
        _timing_prev_level = None
    _timing_enabled = bool(_timing_cfg.get("enabled", True))
    if _timing_enabled:
        try:
            import datetime as _dt_tm
            _tm_end = _dt_tm.date.today().strftime("%Y%m%d")
            _tm_start = (_dt_tm.date.today() - _dt_tm.timedelta(days=760)).strftime("%Y%m%d")
            _idx_klines = fetch_klines_daily(_timing_cfg["index_code"], start=_tm_start, end=_tm_end)
            _tst = _timing_mod.evaluate(_idx_klines or [], _timing_cfg)
            _timing_mult = _tst["level"]
            _timing_score = _tst["score"]
            _timing_detail = _tst["detail"]
            _timing_source = _tst["data_source"]
            _timing_degraded = bool(_tst["degraded"])
            print(f"[择时] 指数={_timing_cfg['index_code']} 状态={_timing_mod.state_label(_timing_score, _timing_source)} "
                  f"档位乘数={_timing_mult:.2f} | {_timing_detail} | 数据源={_timing_source}")
            if _timing_degraded:
                print(f"[!数据源降级!] 择时指数 {_timing_cfg['index_code']} 用了未复权源({_timing_source})，"
                      f"档位可能失真，禁止与回测口径直接对比")
        except Exception as _tm_err:
            print(f"[择时] 计算失败({_tm_err})，本次按满档 1.0 执行")
    else:
        print("[择时] config.yaml timing.enabled=false，本次按满档 1.0 执行")

    # ── Kelly公式辅助: 从近60日交易计算Kelly比例 ──
    try:
        _recorder = get_recorder()
        _report = _recorder.performance_report(days=60)
        _kelly_half = _pm.kelly_from_history(
            wins=_report.get("win_count", 0),
            losses=_report.get("loss_count", 0),
            avg_win=_report.get("avg_win", 0),
            avg_loss=abs(_report.get("avg_loss", 0)) if _report.get("avg_loss", 0) != 0 else 0,
            fraction=0.5,
        )
        if _DEBUG:
            print(f"[Kelly] 近60日: 胜率{_report.get('win_rate', 0):.1%} "
                  f"盈亏比{abs(_report.get('avg_win', 0) / _report.get('avg_loss', 1)):.2f} "
                  f"半Kelly={_kelly_half:.1%}")
    except Exception as _ke:
        if _DEBUG:
            print(f"[Kelly] 计算跳过: {_ke}")

    # 轮动TOP3标的(集中金字塔TOP3): 禁止日内减仓 / 调入标的直接建仓
    rotation_top3_codes = load_rotation_top3_codes()

    # 【四】读取当日最大卖出比例配置
    _max_daily_sell_pct = 0.5  # 默认50%
    try:
        import yaml as _yaml_risk
        _cfg_path_risk = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        with open(_cfg_path_risk) as _cf_risk:
            _conf_risk = _yaml_risk.safe_load(_cf_risk)
        _max_daily_sell_pct = float(_conf_risk.get("risk", {}).get("max_daily_sell_pct", 0.5))
    except Exception:
        pass

    # ── 入场门控: 沪深300ETF 10日动量 ──
    _entry_gate_allowed = True  # 默认放行（取不到数据时也放行）
    _entry_gate_momentum = None  # 记录当次动量值
    try:
        import yaml as _ygcfg
        _cfg_path_eg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        with open(_cfg_path_eg) as _cf_eg:
            _conf_eg = _ygcfg.safe_load(_cf_eg)
        _eg = _conf_eg.get("entry_gate", {})
        if _eg.get("enabled", False):
            _eg_code = _eg.get("index_code", "510300")
            _eg_days = int(_eg.get("momentum_days", 10))
            import datetime as _dt_eg
            _eg_end = _dt_eg.date.today().strftime("%Y%m%d")
            _eg_start = (_dt_eg.date.today() - _dt_eg.timedelta(days=int(_eg_days * 2.5))).strftime("%Y%m%d")
            _eg_klines = fetch_klines_daily(_eg_code, start=_eg_start, end=_eg_end)
            if _eg_klines and len(_eg_klines) > _eg_days:
                _eg_cur = _eg_klines[-1]["close"]
                _eg_prev = _eg_klines[-1 - _eg_days]["close"]
                _eg_mom = (_eg_cur - _eg_prev) / _eg_prev * 100
                _entry_gate_momentum = round(_eg_mom, 4)
                if _eg_mom >= 0:
                    _entry_gate_allowed = True
                    print(f"[ENTRY_GATE] {_eg_code} 十日动量 +{_eg_mom:.2f}% → 允许建仓")
                else:
                    _entry_gate_allowed = False
                    print(f"[ENTRY_GATE] {_eg_code} 十日动量 {_eg_mom:.2f}% → 今日禁止新建仓（存量管理不受影响）")
            else:
                print(f"[ENTRY_GATE] {_eg_code} K线数据不足(需>{_eg_days}条,实得{len(_eg_klines) if _eg_klines else 0}条),放行")
    except Exception as _eg_err:
        print(f"[ENTRY_GATE] 获取 510300 数据失败: {_eg_err}，放行所有买入信号")

    # ── 门控状态持久化 + blocked→allowed 自动触发轮动 ──
    _gate_state = _load_gate_state()
    _gate_status_now = "allowed" if _entry_gate_allowed else "blocked"
    _gate_prev_date = _gate_state.get("date", "")
    _gate_prev_status = _gate_state.get("status", "")
    _gate_rotation_triggered_today = (
        _gate_state.get("rotation_triggered_date", "") == today_str
    )

    # blocked → allowed 且今天还没触发过 → 自动重跑轮动
    if (
        _gate_prev_status == "blocked"
        and _gate_status_now == "allowed"
        and not _gate_rotation_triggered_today
    ):
        print(f"[GATE-TURN] 门控由负转正（上次={_gate_prev_date} blocked，今日={today_str} allowed，动量={_entry_gate_momentum}%）→ 重新轮动")
        try:
            import rotation as _rot_mod
            _rot_result = _rot_mod.run_rotation(force_write=True)
            # run_rotation 返回 top3/all_picks 列表或 dict
            if isinstance(_rot_result, dict):
                _new_top3 = _rot_result.get("all_picks") or _rot_result.get("picks") or []
            elif isinstance(_rot_result, list):
                _new_top3 = _rot_result
            else:
                _new_top3 = []
            print(f"[GATE-TURN] 轮动完成，新TOP3={_new_top3}")
            # 刷新 rotation_top3_codes 以便本次信号使用最新 TOP3
            rotation_top3_codes = set(_new_top3) if _new_top3 else rotation_top3_codes
            # 标记今日已触发，避免同一天重复触发
            _gate_state["rotation_triggered_date"] = today_str
            _gate_state["rotation_triggered_top3"] = _new_top3
        except Exception as _rot_err:
            print(f"[GATE-TURN] 自动轮动失败: {_rot_err}")

    # 记录今日门控状态（无论是否触发轮动都更新）
    _gate_state.update({
        "date": today_str,
        "status": _gate_status_now,
        "momentum": _entry_gate_momentum,
    })
    _save_gate_state(_gate_state)

    # 信号生成
    # 构建统一迭代列表: ETFS 的标准 cfg + 孤儿标的的临时 cfg
    _iter_codes = list(ETFS.items()) + [
        (oc, {
            "name": _orphan_meta.get(oc, {}).get("name", oc),
            "fund": _orphan_meta.get(oc, {}).get("fund", 50000),
            "base_spacing": _orphan_meta.get(oc, {}).get("base_spacing", 0.01),
        })
        for oc in orphan_codes
    ]
    signals_output = {}
    for code, cfg in _iter_codes:
        if code not in realtime:
            signals_output[code] = {"action": "持有(观望)", "reason": "无实时数据", "price": "N/A"}
            continue
        r = realtime[code]
        if r.get("price", 0) == 0:
            signals_output[code] = {"action": "持有(观望)", "reason": "无实时数据", "price": "N/A"}
            continue
        if code not in all_tech or all_tech[code].get("macd_status") == "数据不足":
            signals_output[code] = {"action": "持有(观望)", "reason": "指标数据不足", "price": f"{realtime[code]['price']:.3f}"}
            continue
        t = all_tech[code]
        pos = positions[code]
        price = r["price"]
        spacing = t["spacing"]
        base = base_prices[code]
        atr_pct = (t.get("atr_5min") or t["atr"]) / price if (t.get("atr_5min") or t["atr"]) and price else 0

        # 更新峰值和移动止盈线
        if pos.has_position:
            if price > pos.peak_price:
                pos.peak_price = price
            pos.update_trailing_stop(price, atr_pct)

        # ── 网格解冻: 站上MA5解冻 ──
        if pos.grid_frozen and t["ma5"] and price > t["ma5"]:
            pos.grid_frozen = False

        # P0: 分级减仓 =====
        stop_actions = evaluate_stop(pos, t, price, today_str)
        # strategy_v2: 网格整体止损（-8%减半 / -12%清仓），与现有止损去重
        _grid_overall = check_grid_overall_stop(pos, price, today_str)
        if _grid_overall:
            stop_actions = list(stop_actions) + _grid_overall
        stop_signal = resolve_stop_signal(pos, stop_actions)

        # Level 2: 做T弹性条件（基于5分钟分时数据）
        t0_result = evaluate_t0(t, pos, price, today_str, atr_pct)
        buy_score = t0_result["buy_score"]
        t0_signal = t0_result["t0_signal"]

        pos.prev_rsi = t["rsi"]
        # prev_macd_status只在日期变化时更新(同一天多次运行不覆盖)
        if pos.prev_macd_status is None:
            pos.prev_macd_status = t["macd_status"]

        # Level 3: 网格触达(仅用于已持仓标的的网格加仓，不触发建仓)
        # strategy_v2: 传入 MA20 及其斜率供趋势过滤使用
        _ma20_val = t.get("ma20")
        _ma20_slope_down = False
        if _ma20_val and is_v2_enabled():
            # 用日线K线前20根收盘均值判断MA20斜率下行
            _kl = all_klines.get(code, [])
            if len(_kl) >= 21:
                _closes_tail = [k["close"] for k in _kl[-21:]]
                from market_data import calc_ma as _calc_ma
                _ma20_prev = _calc_ma(_closes_tail[:-1], 20)
                _ma20_slope_down = (_ma20_prev is not None and _ma20_val < _ma20_prev)
        grid_signal, grid_weight = evaluate_grid_signals(
            price, base, spacing, pos.grid_frozen,
            ma20=_ma20_val, ma20_slope_down=_ma20_slope_down
        )
        if grid_signal != "无" and "买入" in grid_signal and not pos.can_buy_today(today_str, atr_pct):
            grid_signal += "(今日已买入,等明日)"

        # ═══ 综合action判定 ═══
        action = "持有"
        # 计算实际持仓占比（基于总资产，用于无信号时的展示）
        # Bug J 修复: 复用循环外已计算的 _total_asset_for_alloc，避免每个ETF重复调用 StateCenter
        _total_asset = _total_asset_for_alloc
        if pos.has_position and _total_asset > 0:
            _mv = pos.shares * price
            position_ratio = f"{_mv / _total_asset * 100:.0f}%"
        else:
            position_ratio = "0%"
        reason = ""
        trade_type = None
        t0_pair = None  # 做T配对挂单

        # 提前计算持仓占比（Bug3: 分母从固定TOTAL_FUND改为动态_total_asset）
        _position_ratio_val = pos.shares * price / _total_asset if pos.has_position and _total_asset > 0 else 0
        _position_capped = pos.has_position and _position_ratio_val >= MAX_POSITION_RATIO

        # ═══════════════════════════════════════════════════════
        # 信号优先级规则（prd 系统优化方案2 漏洞三）
        #
        # 优先级: 止盈止损 > 主策略信号 > 做T信号
        # - 止盈/止损信号：最高优先级，不受仓位上限约束，必须立即执行
        # - 主策略信号：建仓/加仓/减仓等，需通过仓位上限和资金管理检查
        # - 做T信号：最低优先级，被止盈止损抢占时降级为配对挂单
        # - 做T与主策略信号冲突时，主策略信号优先
        # ═══════════════════════════════════════════════════════

        # 【一】轮动清仓: 已持仓标的不在TOP3 → 清仓
        # 适用于: etf_pool内标的调出TOP3 + 已调出池子但仍持仓的标的（含ETFS合并持仓的）
        # rotation_top3_codes为None（文件缺失）时不触发清仓
        _rotation_liquidate = (
            pos.has_position
            and rotation_top3_codes is not None   # 有有效数据才执行
            and code not in rotation_top3_codes   # 不在TOP3 → 清仓
            and (
                (pool_codes and code in pool_codes)          # 池内标的调出TOP3
                or (pos.has_position and code not in pool_codes)  # 已调出池子但仍持仓
            )
        )
        if _rotation_liquidate:
            action = "清仓"
            position_ratio = "100%"
            trade_type = "liquidate"
            if code not in pool_codes:
                reason = "已调出池子但仍持仓，不在TOP3→清仓"
                print(f"[轮动清仓] {code} 已调出池子但仍持仓，不在TOP3集合{rotation_top3_codes}，触发清仓")
            else:
                reason = "轮动调出TOP3"
                print(f"[轮动清仓] {code} 不在TOP3集合{rotation_top3_codes}，触发清仓")

        # 优先级1: 止盈止损（不受仓位上限约束，必须执行）
        # 若已触发轮动清仓，止损止盈的清仓类同样保持清仓，其余类型跳过
        top3_protected = False
        ladder_tiers = []  # 累计阶梯止盈触发的档位 [(pct, sig_name), ...]
        grid_sell_tiers = []  # 【三】累计网格卖出档位 [(pct, sig_name), ...]
        trend_sell_tiers = []  # 【三】累计趋势止盈卖出档位 [(pct, sig_name), ...]
        if stop_actions:
            for sig_name, sig_type in stop_actions:
                # TOP3保护: 轮动TOP3标的禁止日内减仓(破MA5卖活动仓/趋势止盈/阶梯止盈/分级减仓)
                # 清仓类信号(liquidate_*)与轮动清仓(跌出TOP3)不受影响,仍正常执行
                if rotation_top3_codes is not None and code in rotation_top3_codes \
                        and sig_type in INTRADAY_REDUCE_SIG_TYPES:
                    top3_protected = True
                    continue
                if sig_type in ("liquidate_trend", "liquidate_trailing", "liquidate_60pct",
                                "avg_stop_20pct", "hard_stop_25pct", "breakeven_stop",
                                "grid_overall_clear"):
                    action = "清仓"
                    position_ratio = "100%"
                    reason = sig_name
                    trade_type = "liquidate"
                    # O4: reset_on_liquidate 移到成交确认后执行，此处不再提前重置
                    # 【三】清仓类直接跳出，忽略其他信号
                    ladder_tiers.clear()
                    grid_sell_tiers.clear()
                    trend_sell_tiers.clear()
                    break
                elif sig_type in ("reduce_30pct_all",):
                    # 【三】减仓类：若已有清仓则skip，否则记录
                    if trade_type == "liquidate":
                        continue
                    action = "减仓"
                    position_ratio = "减30%总仓"
                    reason = sig_name
                    trade_type = "reduce"
                    # B3: reduce_shares 移到 executor 成交确认后执行，此处不再提前修改持仓
                elif sig_type == "grid_overall_half":
                    # strategy_v2: 网格整体止损 -8% 减半仓
                    if trade_type == "liquidate":
                        continue
                    action = "减仓"
                    position_ratio = "减50%总仓"
                    reason = sig_name
                    trade_type = "reduce_half"
                elif sig_type == "trend_profit_sell":
                    # 【三】趋势止盈: 累计
                    if trade_type == "liquidate":
                        continue
                    trend_sell_tiers.append((10, sig_name))
                elif sig_type == "sell_active_5pct":
                    # 【三】破MA5减仓: 累计
                    if trade_type == "liquidate":
                        continue
                    grid_sell_tiers.append((5, sig_name))
                elif sig_type in ("sell_active_10pct", "sell_active_15pct", "sell_active_20pct"):
                    # 阶梯止盈: 累计比例一次下单，避免循环覆盖只留最后一档
                    if trade_type == "liquidate":
                        continue
                    try:
                        _pct = int(sig_type.replace("sell_active_", "").replace("pct", ""))
                    except ValueError:
                        _pct = 10
                    ladder_tiers.append((_pct, sig_name))
                    # 不在此处设置 action/position_ratio，循环结束后统一累计下单

            # 【三】合并所有非清仓卖出信号（阶梯止盈 + 趋势止盈 + 破MA5）
            if trade_type != "liquidate":
                _all_sell_tiers = ladder_tiers + trend_sell_tiers + grid_sell_tiers
                if _all_sell_tiers and action in ("持有", "卖出"):
                    _total = sum(t[0] for t in _all_sell_tiers)
                    _tier_str = "/".join(f"{t[0]}%" for t in _all_sell_tiers)
                    # 【四】当日卖出总量上限（清仓类不受限）
                    _today_sold_pct = 0.0
                    try:
                        _tlog = pos.daily_trade_log.get(today_str, {})
                        _today_sold_pct = float(_tlog.get("sell_pct", 0.0))
                    except Exception:
                        pass
                    _remaining_pct = max(0, _max_daily_sell_pct * 100 - _today_sold_pct)
                    if _total > _remaining_pct:
                        print(f"[日限] {code} 累计卖出{_total}%超限，截断至{_remaining_pct:.0f}%"
                              f"（今日已卖{_today_sold_pct:.0f}%，上限{_max_daily_sell_pct*100:.0f}%）")
                        _total = _remaining_pct
                    if _total > 0:
                        action = "卖出"
                        position_ratio = f"{_total:.0f}%(活动仓)"
                        reason = "+".join(t[1] for t in _all_sell_tiers)
                        trade_type = "sell"
                        print(f"[合并卖出] {code} 触发档位={_tier_str} 累计比例={_total:.0f}%")
                    else:
                        # 已达当日上限，跳过
                        action = "持有(日限)"
                        reason = f"当日卖出已达上限{_max_daily_sell_pct*100:.0f}%，跳过"
                        trade_type = None

        # 优先级2: 做T（仓位超限时禁止T入，允许T出）
        # available_shares: 复用已加载的 pf_data（行163），不重复读文件；取不到时为 None
        _available_shares = pf_data.get("positions", {}).get(code, {}).get("available")
        if action == "持有":
            t0_exec = evaluate_t0_execute(t0_result, t, pos, price, today_str, atr_pct, record_t0=False, available_shares=_available_shares, prev_close=r.get("prev_close"), code=code)
            if t0_exec is not None:
                action, position_ratio, trade_type, reason = t0_exec[:4]
                t0_pair = t0_exec[4] if len(t0_exec) > 4 else None
        elif buy_score >= 2 and pos.has_position and pos.can_t0_today(today_str, atr_pct):
            # 其他 action 被优先级1抢占时(如趋势止盈卖10%)，T0买入信号仍应生成配对挂单
            t0_exec = evaluate_t0_execute(t0_result, t, pos, price, today_str, atr_pct, record_t0=False, available_shares=_available_shares, prev_close=r.get("prev_close"), code=code)
            if t0_exec is not None:
                t0_pair = t0_exec[4] if len(t0_exec) > 4 else None
                # 将配对挂单信息附加到 reason 中
                if t0_pair:
                    pair_action = t0_pair.get("action", "")
                    pair_price = t0_pair.get("pair_price", 0)
                    reason += f" | T0配对挂单: {pair_action}({pair_price:.3f})"

        # 优先级3: 网格(仅持仓时生效，不触发建仓；仓位超限时禁止网格买入)
        if action == "持有":
            if grid_signal != "无" and "买入" in grid_signal and "等明日" not in grid_signal and not pos.grid_frozen and pos.has_position:
                if _position_capped:
                    action = "持有(仓位已满)"
                    position_ratio = f"{_position_ratio_val*100:.0f}%"
                    reason = f"网格买入信号但仓位{_position_ratio_val*100:.0f}%已达{MAX_POSITION_RATIO*100:.0f}%上限,跳过"
                    trade_type = None
                else:
                    # 检查是否已触发过同一档位
                    grid_key = grid_signal.split("(")[0] if "(" in grid_signal else grid_signal
                    # Bug11: 用日期字段判断是否同一天触发
                    if pos.last_grid_trigger == grid_key and pos.last_grid_trigger_date == today_str:
                        action = "持有(等下一档)"
                        reason = f"网格{grid_key}今日已触发,等下一档"
                    else:
                        pos.record_buy(today_str)
                        pos.last_grid_trigger = grid_key
                        pos.last_grid_trigger_date = today_str  # Bug11: 记录日期
                        action = "买入"
                        position_ratio = f"{grid_weight*100:.0f}%"
                        reason = grid_signal
                        trade_type = "buy"
                    # 网格熔断标记
                    if "熔断" in grid_signal:
                        pos.grid_frozen = True
            elif grid_signal != "无" and "卖出" in grid_signal:
                # 趋势止盈互斥: 当天趋势止盈已触发则跳过网格卖出
                if any(sig_type == "trend_profit_sell" for _, sig_type in stop_actions):
                    action = "持有(趋势止盈优先)"
                    reason = "趋势止盈已触发,跳过网格卖出"
                else:
                    # 提取卖出档位做去重（与买入侧对称）
                    grid_key = grid_signal.split("(")[0] if "(" in grid_signal else grid_signal
                    # Bug11: 用日期字段判断是否同一天触发
                    if pos.last_grid_trigger == grid_key and pos.last_grid_trigger_date == today_str:
                        action = "持有(等下一档)"
                        reason = f"网格{grid_key}今日已触发,等下一档"
                    elif pos.active_shares > 0:
                        pos.last_grid_trigger = grid_key
                        pos.last_grid_trigger_date = today_str  # Bug11: 记录日期
                        # 更新base_price到当前价，使网格上移，防止同一档位重复触发
                        pos.base_price = round(price, 3)
                        # 【四】网格卖出也受当日卖出上限约束
                        _gw_pct = int(grid_weight * 100) if (is_v2_enabled() and grid_weight and grid_weight > 0) else 5
                        _today_sold_pct_g = 0.0
                        try:
                            _tlog_g = pos.daily_trade_log.get(today_str, {})
                            _today_sold_pct_g = float(_tlog_g.get("sell_pct", 0.0))
                        except Exception:
                            pass
                        _remaining_pct_g = max(0, _max_daily_sell_pct * 100 - _today_sold_pct_g)
                        _gw_pct = min(_gw_pct, _remaining_pct_g)
                        if _gw_pct > 0:
                            action = "卖出"
                            position_ratio = f"{int(_gw_pct)}%(活动仓)"
                            reason = grid_signal
                            trade_type = "sell"
                        else:
                            action = "持有(日限)"
                            reason = f"网格卖出被日限({_max_daily_sell_pct*100:.0f}%)截断，跳过"
                    else:
                        action = "持有"
                        reason = grid_signal + "(活动仓为0,不卖底仓)"

        # 无信号: 建仓/补仓/持有逻辑（统一入口，action仍为"持有"时进入）
        # ── 轮动调入TOP3直接建仓: 跳过"冷静期/非多头排列"等建仓过滤, 直接买入30% ──
        # 已持仓的TOP3标的(has_position=True)不在此列, 维持原有止损止盈逻辑
        _top3_rotation_buy = (
            action == "持有"
            and rotation_top3_codes is not None
            and code in rotation_top3_codes
            and not pos.has_position
            and not _regime_is_defense  # 大盘择时: 防守期禁止新建仓
            and _entry_gate_allowed     # 入场门控: 510300十日动量为负时禁止新建仓
        )
        if _top3_rotation_buy:
            pos._enter_position(today_str, price, "30%(轮动调入)")
            action = "买入"
            position_ratio = "30%"
            trade_type = "buy"
            reason = "轮动调入TOP3,直接建仓"
        if action == "持有" and not (_regime_is_defense and not pos.has_position):
            # 大盘择时: 防守期跳过新建仓 evaluate_entry (has_position=True 的补仓信号照常评估)
            entry_result = evaluate_entry(
                pos, t, price, realtime, positions, all_klines, code, today_str, atr_pct, defense_weak
            )
            if entry_result is not None:
                entry_action, entry_ratio, entry_trade_type, entry_reason = entry_result
                if entry_action.startswith("持有"):
                    action = entry_action
                    position_ratio = entry_ratio if entry_ratio else "0%"
                    reason = entry_reason
                    trade_type = entry_trade_type
                else:
                    # 入场门控: 510300 十日动量为负时禁止新建仓/加仓
                    if not _entry_gate_allowed:
                        action = "持有(入场门控)"
                        position_ratio = "0%"
                        reason = "入场门控: 510300十日动量为负,禁止新建仓/加仓"
                        trade_type = None
                    else:
                        action = entry_action
                        position_ratio = entry_ratio
                        reason = entry_reason
                        trade_type = entry_trade_type
            else:
                # evaluate_entry returned None → 有持仓但不是建仓阶段
                if pos.has_position:
                    if pos.grid_frozen:
                        reason = "网格冻结(跌破第5档),等站上MA5解冻"
                    elif _position_capped:
                        action = "持有(仓位已满)"
                        reason = f"当前仓位{_position_ratio_val*100:.0f}%已达目标{MAX_POSITION_RATIO*100:.0f}%"
                    elif price > t["ma5"] and t["rsi"] and t["rsi"] > 50:
                        reason = "趋势偏强,继续持有"
                    elif price < t["ma5"]:
                        reason = "破MA5但未触发分级止损,继续持有"
                    else:
                        reason = "震荡区间,继续持有"
                else:
                    action = "持有(冷静期)"
                    reason = "连续清仓后进入冷静期,暂停建仓"

        # TOP3保护: 日内减仓信号被跳过时标注原因(仅当最终无其他交易信号时)
        if top3_protected and action == "持有" and trade_type is None:
            reason = "TOP3保护,不日内减仓"

        # 附加标注
        vol_label = ""
        if t.get("vol_ratio_5min") is not None:
            vol_label = f"5分钟量比{t['vol_ratio_5min']}"
        elif t.get("vol_ratio_120") is not None:
            vol_label = f"120分钟量比{t['vol_ratio_120']}(降级)"

        # empty_days递增: 空仓且非建仓期(按天去重,同一天多次运行只算1次)
        if not pos.has_position and pos.build_phase == 0:
            if pos.empty_days_date != today_str:
                pos.empty_days += 1
                pos.empty_days_date = today_str

        # 兜底：如果 t0_signal 有有效信号，仅当 trade_type 未设置时覆盖为 t0
        # 止损/止盈/减仓/卖出/T0已设置信号（liquidate/reduce/sell/t0）优先级更高，T0 不覆盖
        # 注意：只有 t0_signal 确实是可执行的做T信号时才覆盖，排除"不执行"、"已用尽"、"但无底仓"等否定信号
        if t0_signal and t0_signal not in ("无", "无(非交易时段)", "无(集合竞价时段)"):
            if ("买入" in t0_signal or "卖出" in t0_signal):
                # 排除不可执行的做T信号
                t0_not_executable = any(kw in t0_signal for kw in ("不执行", "已用尽", "但无底仓", "价低于成本"))
                if trade_type is None and not t0_not_executable:
                    trade_type = "t0"
                    if "买入" in t0_signal:
                        action = "买入"
                    elif "卖出" in t0_signal:
                        action = "卖出"

        # 兜底：position_ratio 为 None 时设为 "0%"
        if position_ratio is None:
            position_ratio = "0%"

        # ── 非监控池标的：只允许卖出/清仓，禁止买入 ──
        # 轮动调入TOP3直接建仓不受监控池限制(轮动TOP3本身就是最新调入池)
        if pool_codes and code not in pool_codes and not _top3_rotation_buy:
            if trade_type in ("buy",) or (trade_type == "t0" and "买入" in action):
                action = "持有(非监控池)"
                position_ratio = "0%"
                reason = f"非监控池标的，仅允许卖出; {reason}"
                trade_type = None

        # 孤儿标的标注（已调出池子但仍持仓）
        _orphan_tag = " [已调出池子但仍持仓]" if code in orphan_codes else ""
        if _orphan_tag:
            reason = reason + _orphan_tag if reason else _orphan_tag.strip()

        # ── 组合层择时: 所有趋势买入的目标仓位统一乘档位系数 ──
        # 最终 target 金额/股数由 executor 按同一 position_ratio 下单，不另算一套
        _ratio_raw = position_ratio
        _mult_applied = 1.0
        _target_amount = None
        _target_shares = None
        if trade_type == "buy":
            _m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)%", str(position_ratio))
            if _m and price > 0:
                _raw_pct = float(_m.group(1)) / 100.0
                _final_pct = _raw_pct * _timing_mult
                _suffix = str(position_ratio)[_m.end():]
                position_ratio = f"{_final_pct * 100:.0f}%{_suffix}"
                _mult_applied = _timing_mult
                # 目标金额/股数用 executor 的唯一计算处（calc_order_amount），避免两套算法
                _plan_t = _pm.calc_order_amount(
                    _total_asset_for_alloc, _final_pct * 100, price=price,
                    static_fund=cfg.get("fund", 44000))
                _target_amount = _plan_t["amount"]
                _target_shares = _plan_t["shares"]
                reason = (f"{reason} [择时档位{_timing_mult:.1f}: "
                          f"{_raw_pct*100:.0f}%→{_final_pct*100:.0f}%]")

        signals_output[code] = {
            "price": f"{price:.3f}",
            "rsi": t["rsi"],
            "macd_status": t["macd_status"],
            "atr": t["atr"],
            "atr_5min": t.get("atr_5min"),
            "atr_pct": round(atr_pct * 100, 2),
            "dynamic_spacing": t["spacing"],
            "vol_ratio_5min": t.get("vol_ratio_5min"),
            "vol_ratio_120": t.get("vol_ratio_120"),
            "vol_label": vol_label,
            "action": action,
            "grid_signal": grid_signal,
            "t0_signal": t0_signal,
            "stop_signal": stop_signal,
            "position_ratio": position_ratio,
            "position_ratio_raw": _ratio_raw,
            "timing_level": _timing_mult,
            "timing_multiplier": _mult_applied,
            "timing_score": _timing_score,
            "target_amount": round(_target_amount, 2) if _target_amount is not None else None,
            "target_shares": _target_shares,
            "reason": reason,
            "trade_type": trade_type,
            "stop_level": pos.stop_level,
            "trailing_stop": round(pos.trailing_stop_price, 3) if pos.trailing_stop_price else None,
            "build_phase": pos.build_phase,
            "grid_frozen": pos.grid_frozen,
            "last_grid_trigger": pos.last_grid_trigger,
            "dead_active_split": f"底仓{DEAD_RATIO*100:.0f}%/活动仓{ACTIVE_RATIO*100:.0f}%",
            "daily_trade_count": pos.daily_trade_log.get(today_str, {"buy_count": 0, "t0_count": 0}),
            "market_weak": defense_weak if code != DEFENSE_CODE else False,
            "t0_pair": t0_pair,
        }

    # ── 择时降档: 把总仓位压到 gross×档位（与回测 _timing_trim 同一口径）──
    _timing_trimmed = []
    if _timing_enabled and pf_data:
        _equity = _total_asset_for_alloc
        _mv_total = 0.0
        for _c, _s in signals_output.items():
            _sh = int(pf_data.get("positions", {}).get(_c, {}).get("shares", 0) or 0)
            _mv_total += _sh * float(_s.get("price", 0) or 0)
        _ratio = _mv_total / _equity if _equity > 0 else 0.0
        _target = float(_timing_cfg["gross"]) * _timing_mult
        _need = 0.0
        if (_timing_prev_level is not None
                and _timing_mult < float(_timing_prev_level) - 1e-9
                and _ratio > _target + float(_timing_cfg["trim_band"])):
            _need = (_ratio - _target) / _ratio
        if _need > 0:
            print(f"[择时降档] 档位 {float(_timing_prev_level):.1f}→{_timing_mult:.1f} "
                  f"总仓位{_ratio*100:.0f}% > 目标{_target*100:.0f}% → 各持仓减{_need*100:.0f}%")
            for _c, _s in signals_output.items():
                if _s.get("trade_type") is not None:
                    continue  # 已有卖出/清仓/买入信号的标的优先，不叠加择时减仓
                _pd = pf_data.get("positions", {}).get(_c, {})
                _sh = int(_pd.get("shares", 0) or 0)
                _avail = int(_pd.get("available", _sh) or _sh)
                if _sh <= 0 or _avail <= 0:
                    continue
                _trim_sh = min(int(_sh * _need / 100) * 100, (_avail // 100) * 100)
                if _trim_sh < 100:
                    continue
                _s.update({
                    "action": "减仓",
                    "trade_type": "reduce",
                    "position_ratio": f"{_need*100:.0f}%",
                    "reduce_shares": _trim_sh,
                    "reason": (f"择时降档减仓 档位{_timing_mult:.1f} "
                               f"仓位{_ratio*100:.0f}%→{_target*100:.0f}%"),
                })
                _timing_trimmed.append((_c, _trim_sh))

    # 记录本次档位，次日只在"降档"当天触发减仓
    try:
        _gs2 = _load_gate_state()
        _gs2.update({"timing_level": _timing_mult, "timing_score": _timing_score,
                     "timing_detail": _timing_detail, "timing_date": today_str,
                     "timing_source": _timing_source})
        _save_gate_state(_gs2)
    except Exception:
        pass

    # ── 目标仓位明细（含择时乘数, 一眼看出这单是否被压过）──
    print(f"[目标仓位] 总资产¥{_total_asset_for_alloc:,.0f} | 择时状态={_timing_mod.state_label(_timing_score, _timing_source)} "
          f"| 档位乘数={_timing_mult:.2f}")
    print(f"  {'代码':<8}{'名称':<14}{'现价':>8}{'当前股数':>10}{'乘数':>6}{'乘数前':>8}{'乘数后':>8}"
          f"{'目标金额':>12}{'目标股数':>10}  动作")
    for _c, _s in signals_output.items():
        _sh = int(pf_data.get("positions", {}).get(_c, {}).get("shares", 0) or 0) if pf_data else 0
        _tt = _s.get("trade_type")
        if _tt != "buy" and _sh <= 0:
            continue
        _px = float(_s.get("price", 0) or 0)
        _mult_s = f"{_timing_mult:.2f}"
        if _tt == "buy":
            _raw = _s.get("position_ratio_raw", "-")
            _fin = _s.get("position_ratio", "-")
            _amt = _s.get("target_amount")
            _tsh = _s.get("target_shares")
            _amt_s = f"¥{_amt:,.0f}" if _amt is not None else "-"
            _sh_s = f"{_tsh:,}" if _tsh is not None else "-"
        else:
            _raw = "-"
            _fin = "-"
            _amt_s = f"¥{_sh * _px:,.0f}"
            _sh_s = f"{_sh:,}"
        print(f"  {_c:<8}{ETFS.get(_c, {}).get('name', _c):<14}{_px:>8.3f}{_sh:>10,}"
              f"{_mult_s:>6}{_raw:>8}{_fin:>8}{_amt_s:>12}{_sh_s:>10}  {_s.get('action', '')}")

    # 可读汇总(用trade_type精确匹配, 不依赖中文字符串)
    liquid_s = [c for c, s in signals_output.items() if s.get("trade_type") == "liquidate"]
    reduce_s = [c for c, s in signals_output.items() if s.get("trade_type") == "reduce"]
    sell_s = [c for c, s in signals_output.items() if s.get("trade_type") == "sell" or (s.get("trade_type") == "t0" and "卖出" in s.get("action", ""))]
    buy_s = [c for c, s in signals_output.items() if s.get("trade_type") == "buy" or (s.get("trade_type") == "t0" and "买入" in s.get("action", ""))]
    hold_s = [c for c, s in signals_output.items() if s.get("trade_type") is None]

    parts = []
    if liquid_s: parts.append(f"紧急清仓: {','.join(liquid_s)}")
    for c in reduce_s: parts.append(f"减仓{c}({signals_output[c]['reason']})")
    for c in sell_s: parts.append(f"卖出{c}({signals_output[c]['reason'][:30]})")
    for c in buy_s: parts.append(f"买入{c}({signals_output[c]['reason'][:30]})")
    for c in hold_s: parts.append(f"{c}{signals_output[c]['reason'][:20]}")
    # 做T配对挂单
    for c, s in signals_output.items():
        if s.get("t0_pair"):
            p = s["t0_pair"]
            parts.append(f"配对挂单{c}:{p['action']}@{p['price']:.3f}({p['shares']}股)")

    human_readable = "。".join(parts) if parts else "所有标的均无操作信号,继续观望。"

    # 动态读取止损/止盈参数用于文案，避免数字与配置漂移
    try:
        import yaml as _yaml_sp
        _sp_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        with open(_sp_cfg_path) as _sp_cf:
            _sp_cfg = _yaml_sp.safe_load(_sp_cf)
    except Exception:
        _sp_cfg = {}
    _sl_cfg = _sp_cfg.get("stop_loss", {})
    _t1 = int(_sl_cfg.get("tier1_pct", 0.07) * 100)
    _t2 = int(_sl_cfg.get("tier2_pct", 0.10) * 100)
    _t3 = int(_sl_cfg.get("tier3_pct", 0.25) * 100)
    _t1_r = int(_sl_cfg.get("tier1_ratio", 0.80) * 100)
    _t2_r = int(_sl_cfg.get("tier2_ratio", 0.50) * 100)
    _hs = int(_sl_cfg.get("hard_stop_pct", 0.18) * 100)
    _htp = int((_sl_cfg.get("hard_take_profit_mult", 1.60) - 1) * 100)

    result = {
        "version": "v3.1",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy_mode": "pyramid",   # 执行层据此取单票上限（pyramid 保持 risk.single_position_cap 0.50）
        "signals": signals_output,
        "timing": {
            "index_code": _timing_cfg.get("index_code"),
            "enabled": _timing_enabled,
            "score": _timing_score,
            "level": _timing_mult,
            "detail": _timing_detail,
            "data_source": _timing_source,
            "degraded": _timing_degraded,
            "prev_level": _timing_prev_level,
            "trimmed": _timing_trimmed,
        },
        "market_environment": "全市场弱势(515080也破MA20)" if defense_weak else "正常",
        "human_readable": human_readable,
        "strategy_policy": {
            "stop_loss": (
                f"均价止损分档(仓位>{_t1_r}%→{_t1}%, {_t2_r}-{_t1_r}%→{_t2}%, <{_t2_r}%→{_t3}%)"
                f"+硬止损(峰值回撤{_hs}%)"
                "+分级减仓(破MA20→30%, DIF<0→30%, 趋势恶化→清仓)"
            ),
            "profit_take": f"{trailing.describe_long()}+硬止盈({_htp}%)→清仓",
            "t0_signal": "弹性做T(5分钟分时:RSI5m<50买入+MACD5m金叉/红柱+量比<1.5; RSI5m>50卖出+MACD5m死叉/绿柱+量比>1.2,30%数量,开盘后即可运行)",
            "entry": "4通道(RSI抄底/趋势跟踪/突破入场/分批建仓)统一30%→确认2次→补仓15%",
            "frequency": "正常1买2T/天, ATR>5%时2买3T/天",
        },
    }

    grid_tables = {}
    for code in ETFS:
        grid_tables[code] = compute_grid_table(base_prices[code], all_tech[code]["spacing"], _allocated_funds.get(code, ETFS[code]["fund"]))
    result["grid_tables"] = grid_tables

    # 持久化信号状态到 portfolio.json（Bug5: positions由sync独写，_signal_state由signal_generator独写）
    if pf_path and pf_data is not None:
        try:
            state_to_save = {}
            for code, pos in positions.items():
                # 收盘后更新prev_macd_status供次日使用
                if code in all_tech:
                    pos.prev_macd_status = all_tech[code]["macd_status"]
                state_to_save[code] = pos.to_dict(today_str)
            # 只更新 _signal_state、account、last_updated，不触碰 positions（由sync独写）
            pf_data["_signal_state"] = state_to_save
            # Bug15: account.total_asset 用 StateCenter 实时计算（替代旧 market_value 求和）
            try:
                from state_center import StateCenter as _SC
                sc = _SC.get_instance()
                sc.sync_from_portfolio(pf_data)
                # 缓存实时行情价格到 StateCenter
                sc.set_cached_prices({code: realtime.get(code, {}).get("price", 0) for code in positions if code in realtime})
                _ta = sc.get_total_asset()
                cash = pf_data.get("account", {}).get("cash", 0) or 0
                _mv = _ta - cash if _ta > 0 else 0
                pf_data["account"] = {
                    "total_asset": round(_ta, 2),
                    "cash": cash,
                    "market_value": round(_mv, 2),
                }
            except Exception:
                pass  # StateCenter 不可用时保留原 account 不覆盖
            pf_data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            from core.atomic_writer import atomic_write_json
            atomic_write_json(pf_path, pf_data)
        except Exception:
            pass

    # ── 数据源断言: 本次取数用了腾讯还是降级源，必须留痕 ──
    _ds_txt, _ = data_source_report()
    print(_ds_txt)
    return result


# ============================================================
# 三、执行层 — 所有函数已移至 executor.py
# 统一通过 executor.execute_signals() 调用。
# ============================================================


def _parse_execute_args():
    """解析 --execute / --execute=t0|buy_sell。返回 (execute_flag, mode)。"""
    for arg in sys.argv[1:]:
        if arg == "--execute":
            return True, None
        if arg.startswith("--execute="):
            val = arg.split("=", 1)[1].strip().lower()
            if val in ("buy_sell", "t0"):
                return True, val
            print(f"[WARN] 无效的 --execute 值: {val}, 使用默认模式(全部)")
            return True, None
    return False, None


def _parse_exec_style():
    """执行口径: auction(开盘集合竞价挂涨跌停价) | continuous(现价限价，09:31 旧路径)。

    优先级: --auction / --style=auction > config.yaml execution.style > continuous。
    """
    argv = sys.argv[1:]
    for arg in argv:
        if arg == "--auction":
            return "auction"
        if arg.startswith("--style="):
            val = arg.split("=", 1)[1].strip().lower()
            if val in ("auction", "continuous"):
                return val
            print(f"[WARN] 无效的 --style 值: {val}, 按 config.yaml 走")
    try:
        import yaml as _y
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")) as _f:
            _ex = (_y.safe_load(_f) or {}).get("execution") or {}
        return str(_ex.get("style", "continuous")).strip().lower()
    except Exception:
        return "continuous"


def _exec_cfg():
    """config.yaml -> execution 段（竞价截止/兜底时点）。读不到就给默认值。"""
    try:
        import yaml as _y
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")) as _f:
            return ((_y.safe_load(_f) or {}).get("execution") or {})
    except Exception:
        return {}


def _build_signals(mode):
    """按策略模式生成信号（main 与 09:31 兜底共用同一入口）。"""
    if mode == "chase":
        return build_chase_signals()
    if mode == "flow":
        return build_flow_signals()
    return generate_signals()


def _wait_until(hm):
    """睡到当天 hm:00（已过则立即返回）。"""
    import time as _t
    tgt = datetime.strptime(datetime.now().strftime("%Y-%m-%d ") + hm, "%Y-%m-%d %H:%M")
    secs = (tgt - datetime.now()).total_seconds()
    if secs <= 0:
        return
    print(f"[AUCTION] 等到 {hm} 再走连续竞价兜底（{secs:.0f} 秒）…", flush=True)
    _t.sleep(secs)


def _alert_critical(title, msg, dedup_key):
    try:
        from core.alert import get_alert_manager
        get_alert_manager().critical(title, msg, dedup_key=dedup_key)
    except Exception as ex:
        print(f"[WARN] alert 告警失败: {ex}")


def _continuous_fallback(execute_mode, fallback_at, reason):
    """09:31 兜底：等到 fallback_at → 重新 sync 持仓 → 重算信号 → 旧路径（现价限价）下单。

    之所以重算而不是复用 09:16 那份结果：竞价那轮可能部分成交，
    持仓已经变了；而策略信号取自 T-1 收盘，重算的仍是同一批指令。
    """
    import subprocess
    _alert_critical("竞价挂单失败，转 09:31 兜底",
                    f"原因: {reason} → 等 {fallback_at} 走连续竞价（现价限价）",
                    dedup_key=f"auction_fallback:{datetime.now():%Y%m%d}")
    if "--fallback-now" not in sys.argv[1:]:
        _wait_until(fallback_at)

    from ths import check_tradable
    ok, why = check_tradable()
    if not ok:
        print(f"[FALLBACK] ❌ {why}，兜底也走不了，退出码非 0", file=sys.stderr)
        _alert_critical("09:31 兜底仍不可用", why,
                        dedup_key=f"auction_fallback_dead:{datetime.now():%Y%m%d}")
        return 2

    script_dir = os.path.dirname(os.path.abspath(__file__))
    print("[FALLBACK] 重新同步持仓…", flush=True)
    try:
        proc = subprocess.run([sys.executable, os.path.join(script_dir, "trade.py"), "sync"],
                              capture_output=True, text=True, timeout=180, cwd=script_dir)
        print((proc.stdout or "").strip()[-500:])
    except Exception as ex:
        print(f"[FALLBACK] ⚠️ sync 失败({ex})，继续用现有 portfolio.json 下单")

    result = _build_signals(_parse_strategy_mode())
    print(json.dumps(result, indent=2, ensure_ascii=False))
    from executor import execute_signals as _exec_execute
    res = _exec_execute(result, mode=execute_mode, style="continuous",
                        strict_dedup=True)
    print(f"[FALLBACK] 连续竞价结果: 成功 {res['success']} 失败 {res['fail']} 跳过 {res['skip']}")
    return 0 if res["fail"] == 0 else 4


def _acquire_exec_locks(execute_flag, execute_mode):
    """执行层互斥锁：同一 mode 同时只允许一个进程。

    --execute（全部模式）同时占 t0 和 buy_sell 两把锁，避免与单模式执行并发下单。
    抢不到锁时 ExecLock 已打印「已有执行在进行中（pid=… since=…），本次跳过」，
    这里返回 None，main 直接退出（退出码 0，不算失败）。
    """
    if not execute_flag:
        return []
    from core.exec_lock import ExecLock
    modes = [execute_mode] if execute_mode else ["t0", "buy_sell"]
    locks = []
    for m in modes:
        lk = ExecLock(m)
        if not lk.acquire():
            for held in reversed(locks):
                held.release()
            return None
        locks.append(lk)
    return locks


def _strategy_mode():
    """config.yaml -> strategy.mode（缺省 flow）。"""
    try:
        import yaml as _y
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")) as _f:
            return str(((_y.safe_load(_f) or {}).get("strategy") or {}).get("mode", "flow")).lower()
    except Exception:
        return "flow"


def _parse_strategy_mode():
    """策略模式: chase(追涨杀跌) | flow(宽组合流量倾斜) | pyramid(旧集中金字塔)。

    优先级: --mode=chase / --mode chase  >  config.yaml strategy.mode  >  默认 flow。
    一键切: python3 signal_generator.py --mode=flow / --mode=pyramid
    """
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg.startswith("--mode="):
            val = arg.split("=", 1)[1].strip().lower()
            if val in ("chase", "flow", "pyramid"):
                return val
            print(f"[WARN] 无效的 --mode 值: {val}, 按 config.yaml 走")
        elif arg == "--mode" and i + 1 < len(argv):
            val = argv[i + 1].strip().lower()
            if val in ("chase", "flow", "pyramid"):
                return val
    return _strategy_mode()


def build_flow_signals():
    """mode=flow 的信号：宽组合 20 只等权 + 流量倾斜 + 溢价风控 + 低换手。

    策略/数据全部在 flow_strategy.py（与 2015-2024 回测同一套代码、同一份缓存），
    这里只做两件事：把目标权重换算成"今天下多少股"，再套成和 pyramid 一样的信号格式
    （price / target_amount / target_shares / action / trade_type / reason），
    另加 flow_shares = 今日实际下单股数（已按"单只一次 ≤ 总资产5%"和可用资金约束算好）。

    不择时：flow 模式不乘 timing 档位（研究结论: 宽书上做趋势/择时把超额打掉）。
    """
    import flow_strategy as _fs
    cfg = _fs.load_cfg()
    plan = _fs.build_plan(cfg=cfg)
    names = _fs._pool_names()
    as_of = plan["as_of"]

    signals = {}

    # ① 卖出（先卖后买：A 股卖出回款当日可用于买入）
    for r in plan["rows"]:
        if r["sell_shares"] <= 0:
            continue
        full_exit = r["cur_shares"] > 0 and r["sell_shares"] >= r["cur_shares"]
        if full_exit:
            act, tt = "清仓(换出)", "liquidate"
            reason = f"掉出流量目标20/缓冲带(当前排名{r['rank'] or '-'}) → 换出"
        else:
            act, tt = "减仓(回目标)", "sell"
            reason = f"超目标权重 {r['weight']:.1%} → 减到 {r['target_shares']:,} 股"
        signals[r["code"]] = dict(
            price=f"{r['price']:.3f}", action=act, trade_type=tt,
            flow_shares=int(r["sell_shares"]), target_shares=int(r["target_shares"]),
            target_amount=r["target_amount"],
            position_ratio=f"{r['weight'] * 100:.0f}%",
            position_ratio_raw=f"{r['weight'] * 100:.0f}%",
            timing_level=1.0, timing_multiplier=1.0, timing_score=None,
            prem=r["prem"], flow_rank=r["rank"], reason=reason, t0_pair=None,
        )

    # ② 买入
    for r in plan["rows"]:
        if r["buy_shares"] <= 0:
            continue
        prem_s = f"{r['prem']:+.1%}" if r["prem"] is not None else "-"
        signals[r["code"]] = dict(
            price=f"{r['price']:.3f}", action="买入(建仓)", trade_type="buy",
            flow_shares=int(r["buy_shares"]), target_shares=int(r["target_shares"]),
            target_amount=r["target_amount"],
            position_ratio=f"{max(r['weight'], cfg['max_order_pct']) * 100:.0f}%",
            position_ratio_raw=f"{r['weight'] * 100:.0f}%",
            timing_level=1.0, timing_multiplier=1.0, timing_score=None,
            prem=r["prem"], flow_rank=r["rank"],
            reason=(f"流量倾斜 排名{r['rank']}/N 目标权重{r['weight']:.1%} "
                    f"溢价{prem_s} 信号周{as_of}"),
            t0_pair=None,
        )

    # ③ 持有 / 跳过（1 手都买不起的高价票）
    for r in plan["rows"]:
        if r["code"] in signals or not r["in_target"]:
            continue
        _skip = r.get("skip_reason")
        signals[r["code"]] = dict(
            price=f"{r['price']:.3f}",
            action=("跳过" if _skip else "持有"), trade_type=None,
            flow_shares=0, target_shares=int(r["target_shares"]),
            target_amount=r["target_amount"],
            position_ratio=f"{r['weight'] * 100:.0f}%",
            position_ratio_raw=f"{r['weight'] * 100:.0f}%",
            timing_level=1.0, timing_multiplier=1.0, timing_score=None,
            prem=r["prem"], flow_rank=r["rank"],
            reason=(_skip or f"目标持仓 权重{r['weight']:.1%} 已在位"), t0_pair=None,
        )

    buys = [r for r in plan["rows"] if r["buy_shares"] > 0]
    sells = [r for r in plan["rows"] if r["sell_shares"] > 0]
    n_hold = len([r for r in plan["rows"] if r["in_target"] and not r.get("skip_reason")]) - len(
        [r for r in buys if r["in_target"]]) - len([r for r in sells if r["in_target"]])
    # ── 目标仓位明细（与 pyramid 同一张表格式）──
    _vm = float(plan.get("vol_mult") or 1.0)          # flow 的波动乘数（已作用在目标金额上）
    print(f"[目标仓位] 总资产¥{plan['total_asset']:,.0f} | 流量宽组合 N={cfg['n']} 等权 "
          f"| 信号周={as_of} | 价格日={plan['price_date']} | 无择时（flow 不乘档位）")
    print(f"  {'代码':<8}{'名称':<14}{'现价':>8}{'当前股数':>10}{'乘数':>6}{'乘数前':>8}{'乘数后':>8}"
          f"{'目标金额':>12}{'目标股数':>10}  动作")
    for r in plan["rows"]:
        act = ("跳过" if r.get("skip_reason") else
               "买入" if r["buy_shares"] > 0 else
               "卖出" if r["sell_shares"] > 0 else
               ("持有" if r["in_target"] else "空"))
        w_pre = r["weight"] / _vm if _vm > 0 else r["weight"]
        print(f"  {r['code']:<8}{(r['name'] or names.get(r['code'], ''))[:13]:<14}"
              f"{r['price']:>8.3f}{r['cur_shares']:>10,}{_vm:>6.2f}"
              f"{w_pre * 100:>7.1f}%{r['weight'] * 100:>7.1f}%"
              f"{'¥' + format(r['target_amount'], ',.0f'):>12}{r['target_shares']:>10,}  {act}")

    human = (f"流量宽组合(信号周{as_of})：买{len(buys)}只 ¥{plan['buy_total']:,.0f}，"
             f"卖{len(sells)}只 ¥{plan['sell_total']:,.0f}，持{n_hold}只，"
             f"执行后现金 ¥{plan['cash_after']:,.0f}")
    over = [r for r in plan["rows"]
            if r["prem"] is not None and r["prem"] == r["prem"]
            and r["prem"] > cfg["premium_cap"]]
    if over:
        print(f"[溢价风控] {len(over)} 只溢价 > {cfg['premium_cap']:.0%}："
              f"{','.join(r['code'] for r in over)} → 不新建仓（已有持仓不强制卖）")

    return {
        "version": "v3.1-flow",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy_mode": "flow",
        "signals": signals,
        "timing": {"index_code": None, "enabled": False, "score": None, "level": 1.0,
                   "detail": "flow 模式不择时", "data_source": "flow", "degraded": False,
                   "prev_level": None, "trimmed": False},
        "market_environment": "流量宽组合(等权20只, 无择时)",
        "human_readable": human,
        "flow": {
            "as_of": as_of, "price_date": plan["price_date"],
            "total_asset": plan["total_asset"], "cash": plan["cash"],
            "budget": plan["budget"], "buy_total": plan["buy_total"],
            "want_total": plan["want_total"], "sell_total": plan["sell_total"],
            "cash_after": plan["cash_after"], "n_targets": plan["n_targets"],
            "pool": plan["pool"], "cfg": plan["cfg"],
            "holdings_source": plan["holdings_source"],
            "account_source": plan["account_source"],
        },
        "strategy_policy": {
            "select": f"份额周变动截面百分位(反向)平滑{cfg['smooth_weeks']}周 → 取{cfg['n']}只等权",
            "buffer": f"缓冲带 {cfg['buffer_n']} 名（已在持仓不掉出前{cfg['buffer_n']}不卖）",
            "min_hold": f"新买入持有期下限 {cfg['min_hold_weeks']} 周",
            "partial": f"分批换仓 每周只换 {cfg['partial']:.0%}",
            "premium": f"溢价 >{cfg['premium_cap']:.0%} 不新建仓（已有持仓不强制卖）",
            "cost": f"单边成本 {cfg['cost']:.2%}；单只单次下单 ≤ 总资产{cfg['max_order_pct']:.0%}",
        },
    }


def build_chase_signals():
    """mode=chase 的信号：动量分位 + 空仓开关 + 主题上限 + ATR 网格 + ATR 跟踪止损。

    策略、数据、撮合全部在 chase_live.py（回测 --backtest 与实盘走同一个 run()），
    这里只做两件事：把目标权重换算成"今天下多少股"，再套成和 flow/pyramid 一样的信号格式
    （price / target_amount / target_shares / action / trade_type / reason），
    另加 flow_shares = 今日实际下单股数（执行层认这个字段；cap_pct 覆盖单只下单上限）。

    空仓开关优先：510300 收盘 < MA{index_ma} → 目标全空，所有持仓标清仓（次日开盘卖）。
    """
    import chase_live as _cl
    cfg = _cl.load_cfg()
    tg = _cl.latest_targets(cfg=cfg, quiet=True)
    stale = _cl.stale_warning(tg["as_of"])
    if stale:
        print(f"⚠️⚠️ [数据陈旧] {stale} —— 信号日={tg['as_of']}，"
              f"先跑 scripts/chase_daily.sh 补当天那根，别静默用旧数据")
    plan = _cl.build_plan(cfg=cfg, tg=tg, quiet=True)

    signals = {}

    # ① 空仓开关：目标全空 → 每只持仓都清仓
    if not plan["on"]:
        for r in plan["rows"]:
            if r["cur_shares"] <= 0:
                continue
            signals[r["code"]] = dict(
                price=f"{r['price']:.3f}", action="清仓(空仓开关)", trade_type="liquidate",
                flow_shares=int(r["cur_shares"]), target_shares=0, target_amount=0.0,
                position_ratio="0%", position_ratio_raw="0%",
                timing_level=0.0, timing_multiplier=0.0, timing_score=None,
                prem=None, flow_rank=0, cap_pct=float(cfg["max_order_pct"]),
                reason=(f"510300 收盘 < MA{cfg['index_ma']} → 目标仓位全空，次日开盘清仓"),
                t0_pair=None)
    else:
        # ② 卖出（先卖后买：A 股卖出回款当日可用于买入）
        for r in plan["rows"]:
            if r["sell_shares"] <= 0:
                continue
            full_exit = r["cur_shares"] > 0 and r["sell_shares"] >= r["cur_shares"]
            if full_exit:
                act, tt = "清仓(换出)", "liquidate"
                why = (f"动量分位跌出持有带(<{cfg['hold_pct']:.0%})"
                       f"或主题上限{cfg['theme_cap']}只/空仓开关 → 换出")
            else:
                act, tt = "减仓(网格)", "sell"
                why = (f"网格超配 → 减到目标 {r['target_shares']:,} 股"
                       f"（权重 {r['weight']:.1%}）")
            signals[r["code"]] = dict(
                price=f"{r['price']:.3f}", action=act, trade_type=tt,
                flow_shares=int(r["sell_shares"]), target_shares=int(r["target_shares"]),
                target_amount=r["target_amount"],
                position_ratio=f"{r['weight'] * 100:.0f}%",
                position_ratio_raw=f"{r['weight'] * 100:.0f}%",
                timing_level=1.0, timing_multiplier=1.0, timing_score=None,
                prem=None, flow_rank=0, what=why,
                reason=f"{why} | 信号日{plan['as_of']}", t0_pair=None)

        # ③ 买入（含网格加仓）
        for r in plan["rows"]:
            if r["buy_shares"] <= 0:
                continue
            grid = r["cur_shares"] > 0
            signals[r["code"]] = dict(
                price=f"{r['price']:.3f}",
                action=("加仓(网格)" if grid else "买入(建仓)"), trade_type="buy",
                flow_shares=int(r["buy_shares"]), target_shares=int(r["target_shares"]),
                target_amount=r["target_amount"],
                position_ratio=f"{max(r['weight'], cfg['max_order_pct']) * 100:.0f}%",
                position_ratio_raw=f"{r['weight'] * 100:.0f}%",
                timing_level=1.0, timing_multiplier=1.0, timing_score=None,
                prem=None, flow_rank=0, cap_pct=float(cfg["max_order_pct"]),
                reason=(f"{'网格低配加仓' if grid else '动量分位前' + str(int(cfg['entry_pct'] * 100)) + '%'}"
                        f" 主题{r['theme']} 分位{(r['pct'] if r['pct'] == r['pct'] else 0):.0%} "
                        f"目标权重{r['weight']:.1%} 信号日{plan['as_of']}"),
                t0_pair=None)

        # ④ 持有
        for r in plan["rows"]:
            if r["code"] in signals or not r["in_target"]:
                continue
            signals[r["code"]] = dict(
                price=f"{r['price']:.3f}", action="持有", trade_type=None,
                flow_shares=0, target_shares=int(r["target_shares"]),
                target_amount=r["target_amount"],
                position_ratio=f"{r['weight'] * 100:.0f}%",
                position_ratio_raw=f"{r['weight'] * 100:.0f}%",
                timing_level=1.0, timing_multiplier=1.0, timing_score=None,
                prem=None, flow_rank=0,
                reason=(f"目标持仓 主题{r['theme']} 权重{r['weight']:.1%} 已在位"
                        + (f" | {r['note']}" if r["note"] else "")),
                t0_pair=None)

    buys = [r for r in plan["rows"] if r["buy_shares"] > 0]
    sells = [r for r in plan["rows"] if r["sell_shares"] > 0]

    print(f"[目标仓位] 总资产¥{plan['total_asset']:,.0f} | 追涨杀跌 N={cfg['n_hold']} "
          f"主题上限{cfg['theme_cap']} | 空仓开关={'空仓' if not plan['on'] else '持仓'}"
          f" | 信号日={plan['as_of']} | 价格日={plan['price_date']}")
    print(f"  {'代码':<8}{'名称':<14}{'现价':>8}{'当前股数':>10}{'目标金额':>12}{'目标股数':>10}"
          f"{'差额':>9}  动作")
    for r in plan["rows"]:
        act = ("清仓" if (not plan["on"] and r["cur_shares"] > 0) else
               "买入" if r["buy_shares"] > 0 else
               "卖出" if r["sell_shares"] > 0 else
               ("持有" if r["in_target"] else "空"))
        print(f"  {r['code']:<8}{(r['name'] or '')[:13]:<14}{r['price']:>8.3f}{r['cur_shares']:>10,}"
              f"{'¥' + format(r['target_amount'], ',.0f'):>12}{r['target_shares']:>10,}"
              f"{r['delta_shares']:>9,}  {act}")

    human = (f"追涨杀跌(信号日{plan['as_of']})："
             + ("空仓开关触发，全部清仓" if not plan["on"] else
                f"买{len(buys)}只 ¥{plan['buy_total']:,.0f}，卖{len(sells)}只 ¥{plan['sell_total']:,.0f}，"
                f"执行后现金 ¥{plan['cash_after']:,.0f}（仓位 {plan['pos_ratio_after']:.0%}）"))

    return {
        "version": "v3.1-chase",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy_mode": "chase",
        "signals": signals,
        "timing": {"index_code": cfg["index_code"], "enabled": bool(cfg["regime"]),
                   "score": None, "level": 1.0 if plan["on"] else 0.0,
                   "detail": f"510300 vs MA{cfg['index_ma']}（空仓开关）",
                   "data_source": "chase_live", "degraded": False,
                   "prev_level": None, "trimmed": False},
        "market_environment": f"追涨杀跌(动量分位+空仓开关) 空仓开关={'空仓' if not plan['on'] else '持仓'}",
        "human_readable": human,
        "chase": {
            "as_of": plan["as_of"], "price_date": plan["price_date"],
            "data_stale": stale,
            "total_asset": plan["total_asset"], "cash": plan["cash"],
            "budget": plan["budget"], "buy_total": plan["buy_total"],
            "want_total": plan["want_total"], "sell_total": plan["sell_total"],
            "cash_after": plan["cash_after"], "pos_ratio_after": plan["pos_ratio_after"],
            "n_targets": plan["n_targets"], "n_pool": plan["n_pool"], "on": plan["on"],
            "cfg": plan["cfg"], "prm": plan["prm"],
            "holdings_source": plan["holdings_source"],
            "account_source": plan["account_source"],
        },
        "strategy_policy": {
            "select": f"20日动量截面分位 >= {1 - cfg['entry_pct']:.0%}（前 {cfg['entry_pct']:.0%}）",
            "buffer": f"缓冲带：已持有分位 >= {cfg['hold_pct']:.0%} 就留",
            "regime": f"510300 收盘 < MA{cfg['index_ma']} → 次日开盘清仓、不建仓",
            "theme": f"同主题最多 {cfg['theme_cap']} 只",
            "index": (f"同一标的指数最多 {cfg['index_cap']} 只（组内留 T-1 日均成交额最大的那只）"
                      if cfg.get("index_cap") else "同指数去重已关闭"),
            "stop": f"ATR 跟踪止损 峰值-{cfg['stop_k']}×ATR14（{cfg['stop_freq']}）",
            "grid": f"网格档距 {cfg['grid_step']}×ATR14，权重 {cfg['grid_lo']}~{cfg['grid_hi']} 倍",
        },
    }


def main():
    argv = sys.argv[1:]
    execute_flag, execute_mode = _parse_execute_args()
    plan_only = ("--plan" in argv) or ("--dry-run" in argv)
    style = _parse_exec_style()
    exec_cfg = _exec_cfg()
    _auc = exec_cfg.get("auction") or {}
    submit_by = str(_auc.get("submit_by") or "09:20")
    fallback_at = str(_auc.get("fallback_time") or "09:31")

    # ── 未登录/桥读不到 → 不生成信号（干跑除外：干跑要在任意登录状态下都能核对委托）──
    if not plan_only:
        from ths import check_tradable
        ok, why = check_tradable()
        if not ok:
            print(f"[FAIL] {why}，不生成信号（未登录/持仓读不到时退出码非 0）", file=sys.stderr)
            if execute_flag and style == "auction":
                return _continuous_fallback(execute_mode, fallback_at,
                                            f"竞价前 THS 不可用: {why}")
            return 2

    # ── 执行层互斥锁：同一 mode 同一时刻只允许一个进程从"读信号"到"下单完成" ──
    locks = _acquire_exec_locks(execute_flag and not plan_only, execute_mode)
    if locks is None:
        return 0

    try:
        try:
            mode = _parse_strategy_mode()
            result = _build_signals(mode)
            json_str = json.dumps(result, indent=2, ensure_ascii=False)
            if sys.stdout.encoding != "utf-8":
                sys.stdout.reconfigure(encoding="utf-8")
            print(json_str)

            if plan_only:
                from executor import execute_signals as _exec_execute
                res = _exec_execute(result, mode=execute_mode, style=style, plan_only=True)
                print(f"[PLAN] 干跑结束: 计划 {len(res['orders'])} 笔, "
                      f"失败 {res['fail']}, 跳过 {res['skip']}")
                return 0 if res["fail"] == 0 else 4

            if not execute_flag:  # 只要在命令行出现了 --execute，就执行信号
                return 0

            from executor import execute_signals as _exec_execute
            if style != "auction":
                res = _exec_execute(result, mode=execute_mode)
                return 0 if res["fail"] == 0 else 4

            # ── 09:16 竞价挂单窗口：09:20-09:25 不可撤单，必须 09:20 前挂出 ──
            hm = datetime.now().strftime("%H:%M")
            if hm >= submit_by:
                print(f"[AUCTION] 当前 {hm} 已过竞价挂单截止 {submit_by}，直接走连续竞价（09:31 路径）")
                res = _exec_execute(result, mode=execute_mode, style="continuous",
                                    strict_dedup=True)
                return 0 if res["fail"] == 0 else 4

            print(f"[AUCTION] 竞价挂单窗口: 现在 {hm} < 截止 {submit_by}")
            res = _exec_execute(result, mode=execute_mode, style="auction")
            if res["fail"] == 0:
                return 0
            return _continuous_fallback(
                execute_mode, fallback_at,
                f"竞价 {res['fail']} 笔未挂出（成功{res['success']}/跳过{res['skip']}）")
        except Exception as e:
            error_msg = {"error": str(e), "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            print(json.dumps(error_msg, indent=2, ensure_ascii=False))
            return 1
    finally:
        for _lk in reversed(locks):
            _lk.release()

if __name__ == "__main__":
    sys.exit(main())