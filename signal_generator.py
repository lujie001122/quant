#!/usr/bin/env python3
"""
量化交易信号生成器 v3.1
10项核心策略逻辑优化:
  P0: 分级减仓替代单一清仓 | 底仓/活动仓真正分离
  P1: 建仓门槛提高+分批 | 做T弹性条件 | 移动止盈 | 网格重置确认
  P2: 防御盾对冲 | 波动率自适应频率 | 空仓期 | 网格熔断
基础: akshare/Wilder RSI/SMA预热MACD/ATR动态间距/倒金字塔/5分钟量比
# 标的: 159516, 515880, 588170, 159532, 515050, 159611
# 策略: 震荡双模式 v4
# 资金: 各上限4.4万, 总计22万(6只共享)

"""

import json
import os
import sys
import time
from datetime import datetime

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
    DEAD_RATIO, ACTIVE_RATIO, TOTAL_FUND, DEFENSE_CODE,
)

# ── 策略判定 ──
from strategies.rsi_macd import (
    is_auction_time,
    t0_buy_score, t0_sell_score,
    _compute_stop_loss_price, _compute_breakeven_price, _compute_trailing_stop,
    _parse_position_ratio, _get_position_shares,
    check_defense,
)
from strategies.grid import (
    evaluate_grid_signals, check_grid_reset, compute_grid_table,
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

# ── 状态中心（执行层瘦身：订单管理、同步、intent、错误检测等） ──
from state_center import (
    # 持仓信息
    get_position_shares as _get_position_shares,
    # 订单管理
    load_today_orders as _load_today_orders,
    load_intent_files as _load_intent_files,
    intent_to_dedup_key as _intent_to_dedup_key,
    sync_entrust_to_orders as _sync_entrust_to_orders,
    check_pending_orders as _check_pending_orders,
    # 工具函数
    trade_type_to_mode as _trade_type_to_mode,
    get_signal_direction as _get_signal_direction,
    signal_direction as _signal_direction,
    detect_error_type as _detect_error_type,
    # build_retry_cmd as _build_retry_cmd,  # [注释] retry_cmd 不再使用
    parse_position_ratio as _parse_position_ratio,
    # intent 清理
    cleanup_intent_files as _cleanup_intent_files,
    cleanup_intent_force as _cleanup_intent_force,
    cleanup_old_intent_files as _cleanup_old_intent_files,
)


# ============================================================
# 一、配置(仅 signal_generator 独有的常量)
# ============================================================

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
                        p.from_dict(s)
                        # 恢复daily_trade_log（当天）
                        saved_log = s.get("daily_trade_log", {})
                        if today_str in saved_log:
                            p.daily_trade_log[today_str] = saved_log[today_str]
                    # 持仓数量从 portfolio 恢复（即使没有 _signal_state 也要恢复持仓）
                    pos_pf = pf_data["positions"].get(code, {})
                    if pos_pf.get("shares", 0) > 0:
                        p.shares = pos_pf["shares"]
                        p.cost = pos_pf.get("total_cost", pos_pf["shares"] * pos_pf.get("avg_cost", 0))
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
                    # last_grid_trigger跨天重置
                    if p.last_grid_trigger and p.last_grid_trigger != today_str:
                        p.last_grid_trigger = None
        except Exception:
            pass

    # 获取数据
    try:
        realtime = fetch_realtime_quotes()
    except Exception as e:
        print(f"[WARN] fetch_realtime_quotes失败: {e}")
        realtime = {code: {"name": ETFS[code]["name"], "price": 0, "pct_change": 0, "change": 0,
                           "volume": 0, "amount": 0, "high": 0, "low": 0, "open": 0, "prev_close": 0}
                    for code in ETFS}

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
        fund = cfg["fund"]
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
        grid_signal, grid_weight = evaluate_grid_signals(price, base, spacing, pos.grid_frozen)
        if grid_signal != "无" and "买入" in grid_signal and not pos.can_buy_today(today_str, atr_pct):
            grid_signal += "(今日已买入,等明日)"

        # ═══ 综合action判定 ═══
        action = "持有"
        # 计算实际持仓占比（基于总资产，用于无信号时的展示）
        _total_asset = pf_data.get("account", {}).get("total_asset", TOTAL_FUND) if pf_data else TOTAL_FUND
        if pos.has_position and _total_asset > 0:
            _mv = pos.shares * price
            position_ratio = f"{_mv / _total_asset * 100:.0f}%"
        else:
            position_ratio = "0%"
        reason = ""
        trade_type = None
        t0_pair = None  # 做T配对挂单

        # 提前计算持仓占比（用于仓位上限检查，防重复计算）
        _position_ratio_val = pos.shares * price / TOTAL_FUND if pos.has_position else 0
        _position_capped = pos.has_position and _position_ratio_val >= 0.50

        # ═══════════════════════════════════════════════════════
        # 信号优先级规则（prd 系统优化方案2 漏洞三）
        #
        # 优先级: 止盈止损 > 主策略信号 > 做T信号
        # - 止盈/止损信号：最高优先级，不受仓位上限约束，必须立即执行
        # - 主策略信号：建仓/加仓/减仓等，需通过仓位上限和资金管理检查
        # - 做T信号：最低优先级，被止盈止损抢占时降级为配对挂单
        # - 做T与主策略信号冲突时，主策略信号优先
        # ═══════════════════════════════════════════════════════
        # 优先级1: 止盈止损（不受仓位上限约束，必须执行）
        if stop_actions:
            for sig_name, sig_type in stop_actions:
                if sig_type in ("liquidate_trend", "liquidate_trailing", "liquidate_60pct", "avg_stop_20pct", "hard_stop_25pct", "breakeven_stop"):
                    action = "清仓"
                    position_ratio = "100%"
                    reason = sig_name
                    trade_type = "liquidate"
                    pos.reset_on_liquidate(today_str)
                    break
                elif sig_type in ("reduce_30pct_all",):
                    action = "减仓"
                    position_ratio = "减30%总仓"
                    reason = sig_name
                    trade_type = "reduce"
                    pos.reduce_shares(pct=30, price=price)
                    break
                elif sig_type == "trend_profit_sell":
                    action = "卖出"
                    position_ratio = "10%(活动仓)"
                    reason = sig_name
                    trade_type = "sell"
                    pos.reduce_shares(pct=10, price=price)
                    break
                elif sig_type == "sell_active_5pct":
                    action = "卖出"
                    position_ratio = "5%(活动仓)"
                    reason = sig_name
                    trade_type = "sell"
                    pos.reduce_shares(pct=5, price=price)
                    break

        # 优先级2: 做T（仓位超限时禁止T入，允许T出）
        if action == "持有":
            t0_exec = evaluate_t0_execute(t0_result, t, pos, price, today_str, atr_pct)
            if t0_exec is not None:
                action, position_ratio, trade_type, reason = t0_exec[:4]
                t0_pair = t0_exec[4] if len(t0_exec) > 4 else None
        elif buy_score >= 2 and pos.has_position and pos.can_t0_today(today_str, atr_pct):
            # 其他 action 被优先级1抢占时(如趋势止盈卖10%)，T0买入信号仍应生成配对挂单
            print(f"[DEBUG {code}] elif branch: action={action!r}, t0_signal={t0_signal!r}")
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
                    reason = f"网格买入信号但仓位{_position_ratio_val*100:.0f}%已达50%上限,跳过"
                    trade_type = None
                else:
                    # 检查是否已触发过同一档位
                    grid_key = grid_signal.split("(")[0] if "(" in grid_signal else grid_signal
                    if pos.last_grid_trigger == grid_key:
                        action = "持有(等下一档)"
                        reason = f"网格{grid_key}今日已触发,等下一档"
                    else:
                        pos.record_buy(today_str)
                        pos.last_grid_trigger = grid_key
                        action = "买入"
                        position_ratio = f"{grid_weight*100:.0f}%"
                        reason = grid_signal
                        trade_type = "buy"
                    # 网格熔断标记
                    if "熔断" in grid_signal:
                        pos.grid_frozen = True
            elif grid_signal != "无" and "卖出" in grid_signal:
                if pos.active_shares > 0:
                    action = "卖出"
                    position_ratio = "5%(活动仓)"
                    reason = grid_signal
                    trade_type = "sell"
                else:
                    action = "持有"
                    reason = grid_signal + "(活动仓为0,不卖底仓)"

        # 无信号: 建仓/补仓/持有逻辑（统一入口，action仍为"持有"时进入）
        if action == "持有":
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
                        reason = f"当前仓位{_position_ratio_val*100:.0f}%已达目标50%"
                    elif price > t["ma5"] and t["rsi"] and t["rsi"] > 50:
                        reason = "趋势偏强,继续持有"
                    elif price < t["ma5"]:
                        reason = "破MA5但未触发分级止损,继续持有"
                    else:
                        reason = "震荡区间,继续持有"
                else:
                    action = "持有(冷静期)"
                    reason = "连续清仓后进入冷静期,暂停建仓"

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
        if t0_signal and t0_signal not in ("无", "无(非交易时段)", "无(集合竞价时段)"):
            if "买入" in t0_signal or "卖出" in t0_signal:
                if trade_type is None:
                    trade_type = "t0"
                    if "买入" in t0_signal:
                        action = "买入"
                    elif "卖出" in t0_signal:
                        action = "卖出"

        # 兜底：position_ratio 为 None 时设为 "0%"
        if position_ratio is None:
            position_ratio = "0%"

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

    result = {
        "version": "v3.1",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "signals": signals_output,
        "market_environment": "全市场弱势(515080也破MA20)" if defense_weak else "正常",
        "human_readable": human_readable,
        "strategy_policy": {
            "stop_loss": "均价止损12%+硬止损20%+分级减仓(破MA20→30%, DIF<0→30%, 趋势恶化→清仓)",
            "profit_take": "移动止盈(8%后成本+5%, 15%后峰值回撤5%)+硬止盈30%→清仓",
            "t0_signal": "弹性做T(5分钟分时:RSI5m<45买入+MACD5m金叉/红柱+量比<1.5; RSI5m>55卖出+MACD5m死叉/绿柱+量比>1.2,配对ATR×1.1,30%数量,11点后运行)",
            "entry": "6通道(RSI抄底/趋势跟踪/突破入场/分批建仓/Test抄底/试探)统一30%→确认70%→补仓15%",
            "frequency": "正常1买2T/天, ATR>5%时2买3T/天, 11:00后只做T不买入",
        },
    }

    grid_tables = {}
    for code in ETFS:
        grid_tables[code] = compute_grid_table(base_prices[code], all_tech[code]["spacing"], ETFS[code]["fund"])
    result["grid_tables"] = grid_tables

    # 持久化信号状态到 portfolio.json
    if pf_path and pf_data is not None:
        try:
            state_to_save = {}
            for code, pos in positions.items():
                # 收盘后更新prev_macd_status供次日使用
                if code in all_tech:
                    pos.prev_macd_status = all_tech[code]["macd_status"]
                state_to_save[code] = pos.to_dict(today_str)
            pf_data["_signal_state"] = state_to_save
            # 更新 account 字段
            mv = sum(p.get("market_value", 0) for p in pf_data.get("positions", {}).values())
            cash = pf_data.get("account", {}).get("cash", 0) or 0
            pf_data["account"] = {
                "total_asset": round(mv + cash, 2),
                "cash": cash,
                "market_value": round(mv, 2),
            }
            pf_data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(pf_path, "w") as _f:
                json.dump(pf_data, _f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    return result


# ============================================================
# 三、执行层 — 所有函数已移至 state_center.py
# 包括: _load_today_orders, _load_intent_files, _intent_to_dedup_key,
#       _write_intent_for_sg, _sync_entrust_to_orders, _check_pending_orders,
#       _detect_error_type, _signal_direction, _build_retry_cmd,
#       _cleanup_intent_files, _cleanup_intent_force, _cleanup_old_intent_files
# 统一从 state_center 导入，见文件顶部 import 段。
# ============================================================


def execute_signals(result, mode=None):
    """遍历信号执行交易（P2-9: 已迁移到 executor.py，此处保留向后兼容）"""
    from executor import execute_signals as _exec_execute
    return _exec_execute(result, mode=mode)


def main():
    # 解析 --execute 参数
    execute_mode = None
    for arg in sys.argv:
        if arg == "--execute":
            execute_mode = None  # 全部
            break
        if arg.startswith("--execute="):
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

        if execute_mode is not None:  # --execute 或 --execute=xxx
            execute_signals(result, mode=execute_mode)

        return result
    except Exception as e:
        error_msg = {"error": str(e), "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        print(json.dumps(error_msg, indent=2, ensure_ascii=False))
        return None

if __name__ == "__main__":
    main()