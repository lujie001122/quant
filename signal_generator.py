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

# ── 策略判定 ──
from strategy import (
    PositionInfo,
    evaluate_stop, resolve_stop_signal,
    evaluate_entry, evaluate_t0, evaluate_t0_execute,
    evaluate_grid_signals, check_grid_reset, compute_grid_table,
    _parse_position_ratio, _get_position_shares, check_defense,
    DEAD_RATIO, ACTIVE_RATIO, TOTAL_FUND, DEFENSE_CODE,
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

        # 优先级1: 止盈止损（不受仓位上限约束，必须执行）
        if stop_actions:
            for sig_name, sig_type in stop_actions:
                if sig_type in ("liquidate_trend", "liquidate_trailing", "liquidate_30pct", "avg_stop_12pct", "hard_stop_20pct", "breakeven_stop"):
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

        # 兜底：如果 t0_signal 有有效信号，将 trade_type 和 action 设为 t0（覆盖止盈止损等优先级）
        if t0_signal and t0_signal not in ("无", "无(非交易时段)", "无(集合竞价时段)"):
            if "买入" in t0_signal or "卖出" in t0_signal:
                trade_type = "t0"
                if "买入" in t0_signal:
                    action = "买入"
                elif "卖出" in t0_signal:
                    action = "卖出"

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
            "entry": "5通道(RSI抄底/分批建仓/Test抄底/试探/补仓)统一30%→确认70%→补仓15%",
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
# 三、执行层 — 持久化 + 异常处理 + 告警推送
# ============================================================

# ─── 订单管理（orders/ 目录） ────────────────────────────────────────

def _load_today_orders():
    """从 orders/ 目录加载今日所有订单，返回 {filename: order_dict}"""
    orders_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'orders')
    if not os.path.exists(orders_dir):
        return {}
    today = datetime.now().strftime('%Y%m%d')
    orders = {}
    for fname in os.listdir(orders_dir):
        if fname.startswith(today) and fname.endswith('.json'):
            try:
                with open(os.path.join(orders_dir, fname)) as f:
                    order = json.load(f)
                orders[fname] = order
            except (json.JSONDecodeError, IOError):
                pass
    return orders


def _trade_type_to_mode(action):
    """将 action 字段映射为 mode: 'buy_sell' | 't0'
    action 是 trade_type 值: 'buy'/'sell'/'liquidate'/'reduce' → 'buy_sell'; 't0' → 't0'
    """
    if action == "t0":
        return "t0"
    return "buy_sell"


def _get_signal_direction(trade_type, action):
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


def _sync_entrust_to_orders():
    """通过 EvolvingSim.getEntrust 同步同花顺委托状态到本地 orders/ 文件。
    将 pending 的订单根据同花顺实际状态更新为 filled/revoked。
    """
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()
        # getEntrust('today', False) 返回今日全部委托(含已成交、已撤单)
        ent = e.getEntrust('today', False)
        time.sleep(2)
    except Exception as e:
        print(f"[_sync_entrust] ⚠️ EvolvingSim 调用失败: {e}")
        return

    if not (isinstance(ent, dict) and ent.get('status') and ent.get('data')):
        print(f"[_sync_entrust] ⚠️ getEntrust 返回异常，跳过同步")
        return

    # 构建同花顺委托索引: {code: {direction: status}}
    # row[2]=code, row[4]=direction, row[5]=status, row[10]=contract
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
    orders = _load_today_orders()
    orders_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'orders')
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
            order_path = os.path.join(orders_dir, fname)
            try:
                order['status'] = new_status
                order['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                with open(order_path, 'w') as f:
                    json.dump(order, f, ensure_ascii=False, indent=2)
                updated += 1
                print(f"[_sync_entrust] {code} {direction} pending→{new_status} (同花顺:{ths_status})")
            except (IOError, json.JSONDecodeError) as e:
                print(f"[_sync_entrust] ⚠️ 更新 {fname} 失败: {e}")

    if updated:
        print(f"[_sync_entrust] 同步完成: {updated} 笔订单状态已更新")


def _check_pending_orders(mode=None, sync_entrust=True):
    """检查 orders/ 目录中今日的挂单状态，同步同花顺委托状态。
    mode: 'buy_sell' | 't0' | None=全部
    sync_entrust: 是否同步同花顺委托状态（默认 True）。_unified_trade 内部已有同步，
                  调用方可传 False 避免重复同步。
    返回: (pending_map, filled_codes)
      pending_map: {code: direction} 今日未成交的挂单（受 mode 过滤）
      filled_codes: set of (code, mode, direction) 今日已成交/已挂单（不受 mode 过滤，全部收集）
    """
    # ── 同步同花顺委托状态，更新本地订单文件 ──
    if sync_entrust:
        _sync_entrust_to_orders()

    orders = _load_today_orders()
    pending_map = {}
    filled_codes = set()

    for fname, order in orders.items():
        code = order.get('code', '')
        direction = order.get('direction', '')
        status = order.get('status', '')
        action = order.get('action', '')
        order_mode = 't0' if 't0_' in action else 'buy_sell'

        # filled_codes 不受 mode 过滤，全部收集（去重用）
        if status in ('filled', 'pending'):
            filled_codes.add((code, order_mode, direction))

        # pending_map 受 mode 过滤
        if mode and order_mode != mode:
            continue

        if status == 'pending':
            pending_map[code] = direction

    return pending_map, filled_codes


def _detect_error_type(stdout, stderr, returncode, exit_reason=None):
    """从 subprocess 输出中检测错误类型。
    返回 (error_type, detail)
    error_type: 'tonghuashun_disconnect' | 'not_filled' | 'timeout' | 'other'
    """
    combined = (stdout or "") + "\n" + (stderr or "")

    if exit_reason == "timeout":
        return "timeout", "下单超时(120s)"

    # 同花顺断连特征
    if "连接同花顺失败" in combined or "EvolvingSim 连接失败" in combined or "同花顺" in combined:
        return "tonghuashun_disconnect", "同花顺断连"

    # 下单成功但持仓未变 → 未成交
    if "下单成功但持仓未变" in combined or "期望增量" in combined:
        return "not_filled", "下单未成交(增量确认失败)"

    if returncode != 0:
        return "other", f"退出码={returncode}"

    return None, None


def _signal_direction(sig):
    """从信号判定交易方向：'买入' | '卖出'"""
    trade_type = sig.get("trade_type", "")
    action = sig.get("action", "")

    if trade_type in ("buy", "t0"):
        return "买入" if "买入" in action else "卖出"
    if trade_type in ("sell", "liquidate", "reduce"):
        return "卖出"
    return None


def _build_retry_cmd(trade_script, code, trade_type, sig, price, shares):
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
            # 无配对做T：降级为普通买卖重试命令
            if "买入" in sig.get("action", ""):
                return f"python3 trade.py buy {code} {shares} {price:.3f}"
            else:
                return f"python3 trade.py sell {code} {shares} {price:.3f}"
    return ""


def execute_signals(result, mode=None):
    """遍历信号，调用 trade.py 执行交易（含异常处理+状态持久化+告警推送）

    mode: None=全部, 'buy_sell'=买卖信号+做T信号都执行(互不干扰), 't0'=只执行做T

    --execute 下单逻辑（v3.1.2）：
    - 买入限价 = min(信号价, 当前实时价)，确保 ≤ 卖一价能挂进去
    - 卖出限价 = max(信号价, 当前实时价)，确保 ≥ 买一价能挂进去
    - 做T配对价 = 当前实时价 ± ATR×1.1（重新计算，不跟信号价比较）
    """
    import subprocess

    if result is None:
        print("[EXECUTE] 无信号结果，跳过执行")
        return

    signals = result.get("signals", {})
    if not signals:
        print("[EXECUTE] 无信号数据，跳过执行")
        return

    if mode:
        print(f"[EXECUTE] 模式过滤: {mode}")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    trade_script = os.path.join(script_dir, "trade.py")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # ═══ 获取当前实时价（用于 --execute 下单） ═══
    live_quotes = {}
    try:
        live_quotes = fetch_realtime_quotes()
        print(f"[EXECUTE] 获取实时行情: {len(live_quotes)} 只ETF")
    except Exception as e:
        print(f"[EXECUTE] ⚠️ 获取实时行情失败: {e}，将使用信号价作为兜底")

    # 按模式过滤信号
    # buy_sell 模式：执行买入/卖出/止盈止损/网格 + 也执行做T信号（互不干扰）
    # t0 模式：只执行做T信号
    BUY_SELL_TYPES = {"buy", "sell", "liquidate", "reduce"}  # 买入/卖出/止盈止损/网格
    T0_TYPES = {"t0"}  # 做T

    actionable = []
    for code, sig in signals.items():
        trade_type = sig.get("trade_type")
        action = sig.get("action", "")
        # 兜底：从 t0_signal 字段推导 trade_type 和 action（覆盖已有值）
        t0_sig = sig.get("t0_signal", "")
        if t0_sig and t0_sig not in ("无", "无(非交易时段)", "无(集合竞价时段)"):
            if "买入" in t0_sig or "卖出" in t0_sig:
                trade_type = "t0"
                # 从 t0_signal 推导正确的 action 方向，并回写 sig dict
                if "买入" in t0_sig:
                    action = "买入"
                elif "卖出" in t0_sig:
                    action = "卖出"
                sig["trade_type"] = trade_type
                sig["action"] = action
        if not trade_type:
            continue
        # 模式过滤
        if mode == "buy_sell":
            # buy_sell 模式：执行买卖信号 + 做T信号（两者互不干扰）
            if trade_type not in BUY_SELL_TYPES and trade_type not in T0_TYPES:
                continue
        elif mode == "t0":
            # t0 模式：只执行做T信号
            if trade_type not in T0_TYPES:
                continue
        actionable.append((code, sig))

    if not actionable:
        print("[EXECUTE] 无交易信号，跳过执行")
        return

    print(f"[EXECUTE] 共 {len(actionable)} 个交易信号，开始执行...")
    print("=" * 60)

    # ═══ 全撤：执行前先撤销所有未成交委托 ═══
    # 失败时告警但不阻塞后续执行
    print("[EXECUTE] 全撤所有未成交委托...")
    try:
        revoke_proc = subprocess.run(
            [sys.executable or "python3", trade_script, "revoke_all"],
            capture_output=True, text=True, timeout=30, cwd=script_dir
        )
        if revoke_proc.returncode != 0:
            print(f"  ⚠️ revoke_all 返回码非零({revoke_proc.returncode})，继续执行...")
            if revoke_proc.stdout:
                print(f"  {revoke_proc.stdout.strip()}")
            if revoke_proc.stderr:
                print(f"  [stderr] {revoke_proc.stderr.strip()}")
        else:
            if revoke_proc.stdout:
                for line in revoke_proc.stdout.strip().split("\n"):
                    print(f"  {line}")
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ revoke_all 超时，继续执行...")
    except Exception as e:
        print(f"  ⚠️ revoke_all 异常: {e}，继续执行...")

    # ═══ 撤单后重新收集 filled_codes（revoked 不在 filled_codes 里，允许挂新单） ═══
    # 不调 _sync_entrust_to_orders（_unified_trade 内部已有，避免重复同步）
    pending_map, filled_codes = _check_pending_orders(mode, sync_entrust=False)
    if filled_codes:
        print(f"[EXECUTE] 去重: {len(filled_codes)} 笔已成交/已挂单，跳过重复信号")
    if pending_map:
        print(f"[EXECUTE] 当前 pending 挂单: {len(pending_map)} 笔")

    success_count = 0
    fail_count = 0
    skip_count = 0

    for i, (code, sig) in enumerate(actionable):
        trade_type = sig["trade_type"]
        action = sig.get("action", "")

        # ═══ 去重检查：同 code + 同 mode + 同 direction + 已成交 → 跳过 ═══
        sig_mode = _trade_type_to_mode(trade_type)
        sig_direction = _get_signal_direction(trade_type, action)
        if (code, sig_mode, sig_direction) in filled_codes:
            print(f"  ⏭️ {code} mode={sig_mode} dir={sig_direction} 今日已成交，跳过")
            skip_count += 1
            continue

        # ═══ 获取当前实时价 ═══
        signal_price_str = sig.get("price", "0")
        try:
            signal_price = float(signal_price_str)
        except (ValueError, TypeError):
            signal_price = 0.0

        live = live_quotes.get(code, {})
        live_price = live.get("price", 0.0) if live else 0.0

        # ═══ 下单限价方向修正 ═══
        is_buy_direction = (
            trade_type == "buy"
            or (trade_type == "t0" and "买入" in action)
        )
        is_sell_direction = (
            trade_type in ("sell", "liquidate", "reduce")
            or (trade_type == "t0" and "卖出" in action)
        )

        if live_price > 0:
            if is_buy_direction:
                if signal_price > 0 and live_price < signal_price:
                    price = live_price
                    print(f"[EXECUTE] {code} 买入限价修正: 信号价={signal_price:.3f} > 当前价={live_price:.3f}, 使用当前价={price:.3f}")
                else:
                    price = signal_price if signal_price > 0 else live_price
                    print(f"[EXECUTE] {code} 信号价={signal_price:.3f}, 当前价={live_price:.3f}, 买入限价={price:.3f}")
            elif is_sell_direction:
                if signal_price > 0 and live_price > signal_price:
                    price = live_price
                    print(f"[EXECUTE] {code} 卖出限价修正: 信号价={signal_price:.3f} < 当前价={live_price:.3f}, 使用当前价={price:.3f}")
                else:
                    price = signal_price if signal_price > 0 else live_price
                    print(f"[EXECUTE] {code} 信号价={signal_price:.3f}, 当前价={live_price:.3f}, 卖出限价={price:.3f}")
            else:
                price = signal_price if signal_price > 0 else live_price
                print(f"[EXECUTE] {code} 未知方向(trade_type={trade_type}), 信号价={signal_price:.3f}, 当前价={live_price:.3f}, 兜底限价={price:.3f}")
        else:
            price = signal_price
            print(f"[EXECUTE] {code} 实时价获取失败，兜底用信号价={signal_price:.3f}")

        if price <= 0:
            print(f"[EXECUTE] ⚠️ {code} 价格无效(信号={signal_price_str}, 实时={live_price})，跳过")
            fail_count += 1
            continue

        t0_pair = sig.get("t0_pair")

        # ═══ 信号解析 + 构建命令 ═══
        cmd = None
        label = None
        shares = 0

        # ── 做T信号 ──
        if trade_type == "t0" and t0_pair:
            pair_shares = t0_pair.get("shares", 0)
            sig_atr = sig.get("atr_5min") or sig.get("atr", 0)
            try:
                sig_atr = float(sig_atr)
            except (ValueError, TypeError):
                sig_atr = 0.0
            old_pair_price = t0_pair.get("pair_price", 0)

            if "买入" in action:
                pair_price = round(price + sig_atr * 1.1, 3) if sig_atr > 0 else old_pair_price
            elif "卖出" in action:
                pair_price = round(price - sig_atr * 1.1, 3) if sig_atr > 0 else old_pair_price
            else:
                pair_price = old_pair_price

            if sig_atr > 0:
                print(f"[EXECUTE] {code} 做T配对价重算: 5minATR={sig_atr:.4f}, 当前价={price:.3f}, 配对价={pair_price:.3f}")
            shares = pair_shares

            if pair_shares < 100:
                print(f"[EXECUTE] ⚠️ {code} 做T股数不足({pair_shares})，跳过")
                fail_count += 1
                continue

            if "买入" in action:
                cmd = ["python3", trade_script, "t0_buy",
                       code, str(pair_shares), f"{price:.3f}", f"{pair_price:.3f}"]
                label = f"做T买入 {code} {pair_shares}股 @{price:.3f} (配对@{pair_price:.3f})"
            elif "卖出" in action:
                cmd = ["python3", trade_script, "t0_sell",
                       code, str(pair_shares), f"{price:.3f}", f"{pair_price:.3f}"]
                label = f"做T卖出 {code} {pair_shares}股 @{price:.3f} (配对@{pair_price:.3f})"
            else:
                print(f"[EXECUTE] ⚠️ {code} 做T方向未知(action={action})，跳过")
                fail_count += 1
                continue

        # ── 做T信号(无配对挂单，降级为普通买卖) ──
        elif trade_type == "t0" and not t0_pair:
            current_shares = _get_position_shares(code)
            if current_shares < 100:
                print(f"[EXECUTE] ⚠️ {code} 做T但无持仓({current_shares}股)，跳过")
                fail_count += 1
                continue
            ratio = 0.30
            shares = int(current_shares * ratio / 100) * 100
            if shares < 100:
                shares = 100
            if "买入" in action:
                cmd = ["python3", trade_script, "buy", code, str(shares), f"{price:.3f}"]
                label = f"做T买入(无配对) {code} {shares}股 @{price:.3f}"
            elif "卖出" in action:
                cmd = ["python3", trade_script, "sell", code, str(shares), f"{price:.3f}"]
                label = f"做T卖出(无配对) {code} {shares}股 @{price:.3f}"
            else:
                print(f"[EXECUTE] ⚠️ {code} 做T方向未知(action={action})，跳过")
                fail_count += 1
                continue

        # ── 普通买入 ──
        elif trade_type == "buy":
            ratio = _parse_position_ratio(sig.get("position_ratio", ""))
            fund = ETFS.get(code, {}).get("fund", 44000)
            shares = int(fund * ratio / price / 100) * 100
            if shares < 100:
                print(f"[EXECUTE] ⚠️ {code} 买入股数不足(fund={fund}, ratio={ratio:.0%}, shares={shares})，跳过")
                fail_count += 1
                continue
            cmd = ["python3", trade_script, "buy", code, str(shares), f"{price:.3f}"]
            label = f"买入 {code} {shares}股 @{price:.3f}"

        # ── 普通卖出 ──
        elif trade_type == "sell":
            current_shares = _get_position_shares(code)
            ratio = _parse_position_ratio(sig.get("position_ratio", ""))
            if ratio > 0:
                shares = int(current_shares * ratio / 100) * 100
            else:
                shares = current_shares
            if shares < 100 or shares > current_shares:
                shares = current_shares
            if shares < 100:
                print(f"[EXECUTE] ⚠️ {code} 卖出股数不足(current={current_shares}, shares={shares})，跳过")
                fail_count += 1
                continue
            cmd = ["python3", trade_script, "sell", code, str(shares), f"{price:.3f}"]
            label = f"卖出 {code} {shares}股 @{price:.3f}"

        # ── 清仓 ──
        elif trade_type == "liquidate":
            current_shares = _get_position_shares(code)
            shares = current_shares
            if current_shares < 100:
                print(f"[EXECUTE] ⚠️ {code} 清仓但无持仓({current_shares}股)，跳过")
                fail_count += 1
                continue
            cmd = ["python3", trade_script, "sell", code, str(current_shares), f"{price:.3f}"]
            label = f"清仓 {code} {current_shares}股 @{price:.3f}"

        # ── 减仓 ──
        elif trade_type == "reduce":
            current_shares = _get_position_shares(code)
            ratio = _parse_position_ratio(sig.get("position_ratio", ""))
            if ratio > 0:
                shares = int(current_shares * ratio / 100) * 100
            else:
                shares = int(current_shares * 0.30 / 100) * 100
            if shares < 100 or shares > current_shares:
                shares = min(current_shares, max(shares, 100))
            if shares < 100 or current_shares < 100:
                print(f"[EXECUTE] ⚠️ {code} 减仓股数不足(current={current_shares}, shares={shares})，跳过")
                fail_count += 1
                continue
            cmd = ["python3", trade_script, "sell", code, str(shares), f"{price:.3f}"]
            label = f"减仓 {code} {shares}股 @{price:.3f}"

        else:
            print(f"[EXECUTE] ⚠️ {code} 未知交易类型({trade_type})，跳过")
            fail_count += 1
            continue

        # ═══ 执行命令（含重试） ═══
        direction = _signal_direction(sig)
        print(f"\n[{i+1}/{len(actionable)}] {label}")
        print(f"  → {' '.join(cmd)}")

        retry_cmd = _build_retry_cmd(trade_script, code, trade_type, sig, price, shares)
        max_retries = 3
        success = False
        final_error = None
        final_error_type = None
        before_shares = 0
        after_shares = 0

        os.system("open -a 同花顺")
        os.system("cliclick c:960,50")

        for attempt in range(1, max_retries + 1):
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=120, cwd=script_dir
                )
                stdout = proc.stdout or ""
                stderr_out = proc.stderr or ""

                err_type, err_detail = _detect_error_type(stdout, stderr_out, proc.returncode)

                if err_type is None and proc.returncode == 0:
                    if stdout:
                        for line in stdout.strip().split("\n"):
                            if "持仓从" in line and "→" in line:
                                try:
                                    parts = line.split("持仓从")[1].split("→")
                                    before_shares = int(parts[0].strip())
                                    after_shares = int(parts[1].split("股")[0].strip())
                                except (ValueError, IndexError):
                                    pass
                                break
                        print(stdout.strip())
                    if stderr_out:
                        print(f"  [stderr] {stderr_out.strip()}")
                    print(f"  ✅ {code} {direction} {shares}股@{price:.3f} → 持仓{before_shares}→{after_shares}股")
                    success_count += 1
                    success = True
                    break

                elif err_type == "tonghuashun_disconnect":
                    if attempt < max_retries:
                        print(f"  ⚠️ 同花顺断连，激活窗口后重试 ({attempt}/{max_retries})...")
                        os.system("osascript -e 'tell application \"同花顺\" to activate'")
                        time.sleep(2)
                    else:
                        if stdout:
                            print(stdout.strip())
                        if stderr_out:
                            print(f"  [stderr] {stderr_out.strip()}")
                        alert_msg = f"[ALERT] ❌ {code} {direction}失败: 同花顺断连(3次重试全失败)。重试: {retry_cmd}"
                        print(alert_msg, file=sys.stderr)
                        final_error = err_detail
                        final_error_type = err_type

                elif err_type == "not_filled":
                    if stdout:
                        print(stdout.strip())
                    if stderr_out:
                        print(f"  [stderr] {stderr_out.strip()}")
                    alert_msg = f"[ALERT] ❌ {code} {direction}失败: 下单未成交(增量确认失败)。重试: {retry_cmd}"
                    print(alert_msg, file=sys.stderr)
                    final_error = err_detail
                    final_error_type = err_type
                    break

                else:
                    if stdout:
                        print(stdout.strip())
                    if stderr_out:
                        print(f"  [stderr] {stderr_out.strip()}")
                    print(f"  ❌ 失败 (exit={proc.returncode})")
                    final_error = err_detail or f"退出码={proc.returncode}"
                    final_error_type = err_type or "other"
                    break

            except subprocess.TimeoutExpired:
                if attempt < max_retries:
                    print(f"  ⚠️ 超时，重试 ({attempt}/{max_retries})...")
                else:
                    alert_msg = f"[ALERT] ❌ {code} {direction}失败: 下单超时(120s×3)。重试: {retry_cmd}"
                    print(alert_msg, file=sys.stderr)
                    final_error = "超时"
                    final_error_type = "timeout"

            except Exception as e:
                print(f"  ❌ 异常: {e}")
                final_error = str(e)
                final_error_type = "exception"
                break

        if not success:
            fail_count += 1

        if i < len(actionable) - 1:
            time.sleep(3)

    print("\n" + "=" * 60)
    print(f"[EXECUTE] 执行完成: 成功 {success_count}, 失败 {fail_count}, 跳过 {skip_count}")


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