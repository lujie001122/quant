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
import sys
from datetime import datetime

# ── 日志降噪: 设 QUANT_DEBUG=1 环境变量开启详细日志 ──
_DEBUG = os.environ.get("QUANT_DEBUG", "").strip() in ("1", "true", "yes")

# ── 行情数据与指标 ──
from market_data import (
    fetch_realtime_quotes, fetch_klines_daily, fetch_klines_5min,
    calc_ma, calc_rsi_wilder, calc_macd, calc_atr, calc_ao,
    calc_vol_ratio_120min, calc_5min_indicators, dynamic_spacing,
    ETFS,
)

# ── 持仓信息模型 + 常量 ──
from position_info import (
    PositionInfo,
    DEAD_RATIO, ACTIVE_RATIO, TOTAL_FUND, DEFENSE_CODE, MAX_POSITION_RATIO,
)

# ── 仓位管理器 ──
from core.position_manager import PositionManager, get_position_manager

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


def evaluate_t0_execute(result, t, pos, price, today_str, atr_pct, record_t0=True):
    return _t0_strategy.evaluate_t0_execute(result, t, pos, price, today_str, atr_pct, record_t0)

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
            # 恢复持仓状态
            saved_state = pf_data.get("_signal_state", {})
            for code in ETFS:
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

    # 获取数据
    try:
        realtime = fetch_realtime_quotes()
    except Exception as e:
        print(f"[WARN] fetch_realtime_quotes失败: {e}")
        realtime = {code: {"name": ETFS[code]["name"], "price": 0, "pct_change": 0, "change": 0,
                           "volume": 0, "amount": 0, "high": 0, "low": 0, "open": 0, "prev_close": 0}
                    for code in ETFS}

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

    if all_tech is None:
        all_tech = {}
        for code in ETFS:
            klines = all_klines[code]
            closes = [k["close"] for k in klines]
            volumes = [k["volume"] for k in klines]
            ma5 = calc_ma(closes, 5)
            ma10 = calc_ma(closes, 10)
            ma20 = calc_ma(closes, 20)
            rsi = calc_rsi_wilder(closes, 14)
            dif, dea, macd_bar, macd_status = calc_macd(closes)
            vol_ratio_120 = calc_vol_ratio_120min(volumes, 20)
            atr = calc_atr(klines, 14)
            base_s = ETFS[code]["base_spacing"]
            spacing = dynamic_spacing(atr, realtime[code]["price"], base_s)

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

    # 网格基准价
    base_prices = {}
    for code, pos in positions.items():
        if pos.has_position and pos.base_price:
            price = realtime[code]["price"]
            spacing = all_tech[code]["spacing"]
            new_base, reset, new_reset_date = check_grid_reset(
                price, pos.base_price, spacing, today_str, pos.last_reset_date
            )
            if reset:
                pos.base_price = new_base
                pos.last_reset_date = new_reset_date
            base_prices[code] = pos.base_price
        else:
            base_prices[code] = realtime[code]["price"]

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

    # 信号生成
    signals_output = {}
    for code, cfg in ETFS.items():
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
        fund = _allocated_funds.get(code, cfg["fund"])
        base = base_prices[code]
        atr_pct = (t.get("atr_5min") or t["atr"]) / price if (t.get("atr_5min") or t["atr"]) and price else 0

        # 更新峰值和移动止盈线
        if pos.has_position:
            if price > pos.peak_price:
                pos.peak_price = price
            pos.update_trailing_stop(price)

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
        sell_score = t0_result["sell_score"]
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
            # 用日线K线最近5日MA20斜率判断下行
            _kl = all_klines.get(code, [])
            if len(_kl) >= 6:
                _closes_tail = [k["close"] for k in _kl[-6:]]
                from market_data import calc_ma as _calc_ma
                _ma20_prev = _calc_ma(_closes_tail[:-1], min(20, len(_closes_tail)-1))
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

        # 【一】轮动清仓: etf_pool内已持仓标的，若rotation_top3_codes有效且不包含该标的 → 清仓
        # 优先级与其他清仓类信号相同，不受TOP3保护拦截（它本来就不在TOP3里）
        # rotation_top3_codes为None（文件缺失）时不触发清仓
        _rotation_liquidate = (
            pos.has_position
            and rotation_top3_codes is not None   # 有有效数据才执行
            and code not in rotation_top3_codes   # 不在TOP3 → 清仓
            and pool_codes                        # etf_pool有效
            and code in pool_codes                # 只对etf_pool内标的执行（排除手动买入的非池内标的）
        )
        if _rotation_liquidate:
            action = "清仓"
            position_ratio = "100%"
            trade_type = "liquidate"
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
        if action == "持有":
            t0_exec = evaluate_t0_execute(t0_result, t, pos, price, today_str, atr_pct, record_t0=False)
            if t0_exec is not None:
                action, position_ratio, trade_type, reason = t0_exec[:4]
                t0_pair = t0_exec[4] if len(t0_exec) > 4 else None
        elif buy_score >= 2 and pos.has_position and pos.can_t0_today(today_str, atr_pct):
            # 其他 action 被优先级1抢占时(如趋势止盈卖10%)，T0买入信号仍应生成配对挂单
            t0_exec = evaluate_t0_execute(t0_result, t, pos, price, today_str, atr_pct, record_t0=False)
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
    _pt_cfg = _sp_cfg.get("profit_take", {})
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
        "signals": signals_output,
        "market_environment": "全市场弱势(515080也破MA20)" if defense_weak else "正常",
        "human_readable": human_readable,
        "strategy_policy": {
            "stop_loss": (
                f"均价止损分档(仓位>{_t1_r}%→{_t1}%, {_t2_r}-{_t1_r}%→{_t2}%, <{_t2_r}%→{_t3}%)"
                f"+硬止损(峰值回撤{_hs}%)"
                "+分级减仓(破MA20→30%, DIF<0→30%, 趋势恶化→清仓)"
            ),
            "profit_take": f"移动止盈(8%后成本+5%, 15%后峰值回撤5%)+硬止盈({_htp}%)→清仓",
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
            with open(pf_path, "w") as _f:
                json.dump(pf_data, _f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    return result


# ============================================================
# 三、执行层 — 所有函数已移至 executor.py
# 统一通过 executor.execute_signals() 调用。
# ============================================================


def execute_signals(result, mode=None):
    """遍历信号执行交易（P2-9: 已迁移到 executor.py，此处保留向后兼容）"""
    from executor import execute_signals as _exec_execute
    return _exec_execute(result, mode=mode)


def main():
    # 解析 --execute 参数
    execute_mode = None  # None 表示未指定 --execute，不执行
    execute_flag = False  # 标记是否出现了 --execute 参数
    for arg in sys.argv:
        if arg == "--execute":
            execute_flag = True
            execute_mode = None  # 全部（None 在此上下文表示全部模式）
            break
        if arg.startswith("--execute="):
            execute_flag = True
            val = arg.split("=", 1)[1].strip().lower()
            if val in ("buy_sell", "t0"):
                execute_mode = val
            else:
                print(f"[WARN] 无效的 --execute 值: {val}, 使用默认模式(全部)")
                execute_mode = None
            break

    try:
        result = generate_signals()
        json_str = json.dumps(result, indent=2, ensure_ascii=False)
        if sys.stdout.encoding != "utf-8":
            sys.stdout.reconfigure(encoding="utf-8")
        print(json_str)

        if execute_flag:  # 只要在命令行出现了 --execute，就执行信号
            execute_signals(result, mode=execute_mode)

        return result
    except Exception as e:
        error_msg = {"error": str(e), "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        print(json.dumps(error_msg, indent=2, ensure_ascii=False))
        return None

if __name__ == "__main__":
    main()