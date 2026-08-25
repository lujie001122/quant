#!/usr/bin/env python3
"""
RSIMACDStrategy — RSI+MACD建仓止损策略

从 strategy.py 拆分:
  - evaluate_stop: 分级止损/保本止盈/硬止损/趋势止盈/移动止盈
  - evaluate_entry: 6通道建仓判定
  - t0_buy_score / t0_sell_score: 做T评分(从 strategy 移至此, 供 T0Strategy 调用)
  - resolve_stop_signal: 止损信号解析
  - _compute_stop_loss_price / _compute_breakeven_price / _compute_trailing_stop: 止损计算辅助
"""

from strategies.base import BaseStrategy
from datetime import datetime
from position_info import DEAD_RATIO, ACTIVE_RATIO, TOTAL_FUND, GRID_BUY_LEVELS, GRID_SELL_LEVELS, MAX_DAILY_BUYS, MAX_DAILY_T0, DEFENSE_CODE, COOLDOWN_DAYS, INVERTED_WEIGHTS, MAX_POSITION_RATIO
from market_data import ETFS

# ═══════════════════════════════════════════════
# 常量（从 position_info 统一导入，避免跨文件定义不一致）
# ═══════════════════════════════════════════════


# ═══════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════

def is_auction_time(now=None):
    """集合竞价时段: 9:15-9:25, 14:57-15:00"""
    if now is None:
        now = datetime.now()
    t = now.hour * 60 + now.minute
    return (555 <= t <= 565) or (897 <= t <= 900)


def t0_buy_score(rsi_5min, macd_5min_status, vol_ratio_5min):
    """做T买入评分(基于5分钟分时): RSI超卖+MACD多头确认+量能温和

    ★ RSI极端兜底通道: RSI < 25 直接返回3分(跳过MACD/量比检查)
    - RSI_5min 25-50: 原有3项共振评分逻辑
    - RSI_5min < 50 必须满足(超卖才摊低，震荡市放宽)
    - MACD_5min 金叉或红柱(趋势确认，加分项；死叉/绿柱不归零，仅不加分)
    - 量比 < 1.5(不放量砸盘)
    """
    if rsi_5min is None:
        return 0
    if rsi_5min > 75:
        return 0
    if rsi_5min < 25:
        return 3
    if rsi_5min >= 50:
        return 0
    score = 1
    if macd_5min_status in ["金叉", "红柱放大"]:
        score += 1
    if vol_ratio_5min is not None and vol_ratio_5min < 1.5:
        score += 1
    return score


def t0_sell_score(rsi_5min, macd_5min_status, vol_ratio_5min):
    """做T卖出评分(基于5分钟分时): RSI超买+MACD空头确认+量能放大

    ★ RSI极端互斥: RSI < 25 时只允许买入，卖出强制归零
    - RSI > 75: 极端兜底直接满分
    - RSI_5min 50-75: 原有3项共振评分逻辑
    - RSI_5min > 50 必须满足(超买才卖，震荡市放宽)
    - MACD_5min 死叉或绿柱(趋势结束，加分项；金叉/红柱不归零，仅不加分)
    - 量比 > 1.2(放量拉升)
    """
    if rsi_5min is None:
        return 0
    if rsi_5min < 25:
        return 0
    if rsi_5min > 75:
        return 3
    if rsi_5min <= 50:
        return 0
    score = 1
    if macd_5min_status in ["死叉", "绿柱放大"]:
        score += 1
    if vol_ratio_5min is not None and vol_ratio_5min > 1.2:
        score += 1
    return score


# ═══════════════════════════════════════════════
# 止损计算辅助
# ═══════════════════════════════════════════════

def _compute_stop_loss_price(avg_cost, peak_price, avg_pct=0.20, hard_pct=0.25):
    """计算止损价格(极限方案C: 放宽)"""
    avg_stop = avg_cost * (1 - avg_pct) if avg_cost > 0 else 0.0
    hard_stop = peak_price * (1 - hard_pct) if peak_price > 0 else 0.0
    return avg_stop, hard_stop


def _compute_breakeven_price(avg_cost, margin=0.02):
    """计算保本价格: 成本 + margin"""
    return avg_cost * (1 + margin) if avg_cost > 0 else 0.0


def _compute_trailing_stop(avg_cost, peak_price, reached_8pct, reached_15pct):
    """计算移动止盈价格"""
    if reached_15pct and peak_price > 0:
        return peak_price * 0.93
    if reached_8pct:
        return avg_cost * 1.05
    return 0.0


# ═══════════════════════════════════════════════
# 通用辅助
# ═══════════════════════════════════════════════

# _parse_position_ratio / _get_position_shares 已统一到 state_center
# 统一使用 state_center.parse_position_ratio / state_center.get_position_shares


def check_defense(DEFENSE_CODE, all_tech, realtime):
    """检查防御标的(515080)是否弱势
    返回 True 表示全市场弱势，应暂停建仓
    """
    if not DEFENSE_CODE or DEFENSE_CODE not in all_tech:
        return False
    d_tech = all_tech[DEFENSE_CODE]
    d_price = realtime.get(DEFENSE_CODE, {}).get("price", 0)
    d_ma20 = d_tech.get("ma20")
    d_dif = d_tech.get("dif")
    d_macd = d_tech.get("macd_status")
    if d_ma20 and d_price < d_ma20 and d_dif is not None and d_dif < 0 and d_macd in ["死叉", "绿柱放大"]:
        return True
    return False


# ═══════════════════════════════════════════════
# RSIMACDStrategy
# ═══════════════════════════════════════════════

class RSIMACDStrategy(BaseStrategy):
    """RSI+MACD 建仓/止损策略"""

    @staticmethod
    def _update_entry_cost(pos, price, ratio, code):
        """买入时加权平均更新 entry_avg_cost（不回改shares，回测引擎自己管理）"""
        fund = ETFS.get(code, {}).get("fund", 44000)
        shares = int(fund * ratio / price / 100) * 100
        if shares >= 100 and pos.shares > 0 and pos.entry_avg_cost > 0:
            pos.entry_avg_cost = (pos.entry_avg_cost * pos.shares + price * shares) / (pos.shares + shares)
        elif shares >= 100:
            pos.entry_avg_cost = price

    def evaluate_stop(self, pos, t, price, today_str):
        """评估止盈止损信号

        返回:
          stop_actions: [(signal_name, signal_type), ...]
            signal_type: "breakeven_stop" | "avg_stop_20pct" | "liquidate_60pct" |
                         "liquidate_trailing" | "hard_stop_25pct" |
                         "reduce_30pct_all" | "liquidate_trend" |
                         "trend_profit_sell" | "sell_active_5pct"
        """
        stop_actions = []

        if not pos.has_position:
            return stop_actions

        # ── 均价止损分级: 仓位<50%→-25%, 50-80%→-20%, >80%→-15% ──
        if pos.entry_cost > 0:
            pos_ratio = pos.shares * price / TOTAL_FUND if price > 0 else 0
            if pos_ratio > 0.80:
                stop_pct = 0.15
            elif pos_ratio >= 0.50:
                stop_pct = 0.20
            else:
                stop_pct = 0.25
            if price <= pos.entry_cost * (1 - stop_pct):
                stop_actions.append((f"均价止损{stop_pct*100:.0f}%(仓位{pos_ratio*100:.0f}%,avg={pos.entry_cost:.3f}→现价{price:.3f})", "avg_stop_20pct"))

        # ── 硬止盈60%: base_price*1.60(极限方案C) ──
        if pos.base_price and price >= pos.base_price * 1.60:
            stop_actions.append((f"硬止盈60%(基准{pos.base_price:.3f}→现价{price:.3f})", "liquidate_60pct"))

        # ── 移动止盈 ──
        if pos.trailing_stop_price > 0 and price <= pos.trailing_stop_price:
            label = "移动止盈8%(成本+5%)" if not pos.reached_15pct else "移动止盈15%(峰值回撤5%)"
            stop_actions.append((f"{label}:触线{pos.trailing_stop_price:.3f}", "liquidate_trailing"))

        # ── 硬止损25%: 从价格峰值回撤25%清仓(极限方案C: 放宽) ──
        if pos.peak_price > 0 and price < pos.peak_price * 0.75:
            stop_actions.append((f"硬止损25%(峰值{pos.peak_price:.3f}→现价{price:.3f})", "hard_stop_25pct"))

        # Level 1a: 连续2天破MA20 → 减30%总仓
        if t["ma20"] and pos.stop_level == 0:
            if price < t["ma20"]:
                if pos.below_ma20_date != today_str:
                    pos.below_ma20_count += 1
                    pos.below_ma20_date = today_str
                if pos.below_ma20_count >= 2:
                    stop_actions.append(("分级止损1级:连续2天破MA20减30%", "reduce_30pct_all"))
                    pos.stop_level = 1
            else:
                pos.below_ma20_count = 0
                pos.below_ma20_date = None

        # Level 1b: DIF<0 → 再减30%
        if pos.stop_level == 1 and t["dif"] is not None and t["dif"] < 0:
            stop_actions.append(("分级止损2级:DIF<0再减30%", "reduce_30pct_all"))
            pos.stop_level = 2

        # Level 1c: MACD死叉/绿柱放大 → 清仓（需额外确认: price<MA20持续3天 或 RSI<35）
        if pos.stop_level == 2 and t["macd_status"] in ["死叉", "绿柱放大"]:
            below_ma20_3days = pos.below_ma20_count >= 3
            rsi_low = t["rsi"] is not None and t["rsi"] < 35
            if below_ma20_3days or rsi_low:
                stop_actions.append(("分级止损3级:MACD趋势恶化清仓", "liquidate_trend"))
                pos.stop_level = 3

        # 分级止损恢复(价>MA20+DIF>0+MACD红柱)
        if pos.stop_level > 0 and t["ma20"] and price > t["ma20"] and t["dif"] is not None and t["dif"] > 0 and t["macd_status"] in ["红柱放大", "红柱缩短"]:
            pos.stop_level = 0
            pos.below_ma20_count = 0
            pos.below_ma20_date = None

        # 分级止损恢复: stop_level=2 但 MACD 未恶化且价格恢复 → 重置
        if pos.stop_level == 2 and t["macd_status"] not in ["死叉", "绿柱放大"] and t["ma20"] and price > t["ma20"] and t["dif"] is not None and t["dif"] > 0:
            pos.stop_level = 0
            pos.below_ma20_count = 0
            pos.below_ma20_date = None

        # P1: 趋势止盈(MACD红柱缩短+破MA5+破MA10) — 带冷却检查 (B6)
        if t["macd_status"] == "红柱缩短" and price < t["ma5"] and t.get("ma10") and price < t["ma10"]:
            if pos.can_trend_profit_today(today_str):
                stop_actions.append((f"趋势止盈(MACD红柱缩短+破MA5)卖10%活动仓", "trend_profit_sell"))
                pos.record_trend_profit(today_str)

        # 破MA5卖活动仓5%(active=0时不触发) — 带冷却检查 (B6)
        if price < t["ma5"] and t["rsi"] and t["rsi"] > 50:
            if pos.active_shares > 0 and pos.can_ma5_sell_today(today_str):
                stop_actions.append((f"破MA5卖活动仓5%", "sell_active_5pct"))
                pos.record_ma5_sell(today_str)

        return stop_actions

    def resolve_stop_signal(self, pos, stop_actions):
        """从 stop_actions 列表解析最终止盈止损信号文本"""
        if not stop_actions:
            if pos.has_position:
                return "未触发"
            else:
                return "未触发(无持仓)"

        # 优先级: 清仓 > 减仓 > 卖活动仓
        for sig_name, sig_type in stop_actions:
            if sig_type in ("liquidate_trend", "liquidate_trailing", "liquidate_60pct", "avg_stop_20pct", "hard_stop_25pct", "breakeven_stop"):
                return sig_name
        for sig_name, sig_type in stop_actions:
            if sig_type in ("reduce_30pct_all",):
                return sig_name
        return stop_actions[0][0]

    def evaluate_entry(self, pos, t, price, realtime, positions, all_klines, code, today_str, atr_pct, defense_weak):
        """6通道建仓判定 + 补仓/确认加仓

        返回:
          (action, position_ratio, trade_type, reason) 或 None
        """
        if pos.has_position or pos.is_in_cooldown(today_str):
            return None

        # P2: 市场环境过滤
        if defense_weak:
            return ("持有(观望)", "0%", None, "防御标的515080也弱势,全市场暂停建仓")

        # 过滤器1: ATR异常(>50%跳过, 防除权)
        if atr_pct > 0.50:
            return ("持有(观望)", "0%", None, f"ATR异常{atr_pct*100:.0f}%,疑似除权,暂停建仓")

        # 过滤器2: 止盈期不新开(任何持仓盈利>30%时禁止新开)
        if any(p.has_position and p.base_price and r2["price"] > p.base_price * 1.30
               for c2, p in positions.items() if c2 != code
               for r2 in [realtime.get(c2, {})] if r2):
            return ("持有(观望)", "0%", None, "止盈期不新开(已有仓位触发硬止盈)")

        rsi_ok = t["rsi"] is not None and t["rsi"] > 40
        rsi_minimal = t["rsi"] is not None and t["rsi"] > 30
        # 多头排列: MA5>MA10>MA20 才允许建仓
        bullish_align = t["ma5"] and t.get("ma10") and t["ma20"] and t["ma5"] > t["ma10"] > t["ma20"]

        if pos.build_phase == 0:
            # 所有建仓通道要求多头排列
            if not bullish_align:
                return ("持有(观望)", "0%", None, f"非多头排列(MA5={t['ma5']:.3f},MA10={t.get('ma10',0):.3f},MA20={t['ma20']:.3f}),暂停建仓")
            # 通道1: RSI抄底 (MACD金叉+RSI≤55) → 30%(极限方案C: 去掉MA20过滤)
            if t["macd_status"] == "金叉" and (t["rsi"] is not None and t["rsi"] <= 55) and pos.can_buy_today(today_str, atr_pct):
                action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(RSI抄底)")
                self._update_entry_cost(pos, price, 0.30, code)
                return (action, position_ratio, trade_type, f"RSI抄底:MACD金叉(RSI={t['rsi']})")

            # 通道2: 趋势跟踪 (空仓>10天+MACD红柱) → 30%(极限方案C: 去掉MA20过滤)
            if pos.empty_days > 10 and t["macd_status"] in ["红柱放大", "红柱缩短"] and pos.can_buy_today(today_str, atr_pct):
                action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(趋势跟踪)")
                self._update_entry_cost(pos, price, 0.30, code)
                return (action, position_ratio, trade_type, f"趋势跟踪:价>MA20+{t['macd_status']}")

            # 通道3: 突破入场 (10日新高+MACD金叉) → 30%(极限方案C: 去掉MA20过滤)
            if t["macd_status"] == "金叉" and pos.can_buy_today(today_str, atr_pct):
                if len(all_klines.get(code, [])) >= 11:
                    closes_10 = [k["close"] for k in all_klines[code][-11:-1]]
                    if closes_10 and price > max(closes_10):
                        action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(突破入场)")
                        self._update_entry_cost(pos, price, 0.30, code)
                        return (action, position_ratio, trade_type, "突破入场:10日新高+金叉")

            # 通道4: 分批建仓 (金叉/红柱放大+RSI>40+站MA5) → 30%(极限方案C: 去掉MA20过滤)
            if t["macd_status"] in ["金叉", "红柱放大"] and rsi_ok and price > t["ma5"] and pos.can_buy_today(today_str, atr_pct):
                action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(分批1)")
                self._update_entry_cost(pos, price, 0.30, code)
                return (action, position_ratio, trade_type, "分批建仓1:首笔30%试探(MACD健康+RSI满足+站上MA5)")

            # 通道5: Test抄底 (RSI<35+绿柱缩短+站MA5) → 30%(极限方案C: 去掉MA20过滤)
            if t["rsi"] is not None and t["rsi"] < 35 and t["macd_status"] == "绿柱缩短" and t["ma5"] and price > t["ma5"] and pos.prev_macd_status == "绿柱缩短" and pos.can_buy_today(today_str, atr_pct):
                action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(Test抄底)")
                self._update_entry_cost(pos, price, 0.30, code)
                return (action, position_ratio, trade_type, f"Test抄底:RSI={t['rsi']:.1f}+绿柱缩短+站MA5")

            # 通道6: 试探建仓 (绿柱缩短/震荡+RSI>35+站MA5+站MA20) → 30%
            if t["macd_status"] in ["绿柱缩短", "震荡"] and t["rsi"] is not None and t["rsi"] > 35 and price > t["ma5"] and t["ma20"] and price > t["ma20"] and pos.can_buy_today(today_str, atr_pct):
                action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(试探)")
                self._update_entry_cost(pos, price, 0.30, code)
                return (action, position_ratio, trade_type, f"试探建仓:30%底仓(MACD{t['macd_status']}+RSI{t['rsi']}+站MA5+站MA20)")

            return ("持有(观望)", "0%", None, f"建仓条件未满足(MACD{t['macd_status']},RSI{t['rsi']})")

        elif pos.build_phase == 1:
            _position_ratio_val = pos.shares * price / TOTAL_FUND
            _position_capped = _position_ratio_val >= MAX_POSITION_RATIO

            # ── 逆势补仓: 跌超3%允许第二次买入（需检查仓位上限）──
            if pos.build_first_price > 0 and pos.add_count < 5:
                dip_pct = (price - pos.build_first_price) / pos.build_first_price
                if dip_pct < -0.03 and pos.can_buy_today(today_str, atr_pct) and t["macd_status"] not in ["死叉", "绿柱放大"] and (t["rsi"] is None or t["rsi"] < 40):
                    if _position_capped:
                        return ("持有(仓位已满)", f"{_position_ratio_val*100:.0f}%", None, f"补仓信号但仓位{_position_ratio_val*100:.0f}%已达{MAX_POSITION_RATIO*100:.0f}%上限,跳过")
                    pos.record_buy(today_str)
                    self._update_entry_cost(pos, price, 0.15, code)
                    pos.add_count += 1
                    return ("买入", "15%(补仓)", "buy", f"逆势补仓:跌{dip_pct*100:.0f}%加仓15%(MACD{t['macd_status']})")

            # ── 确认加仓: 突破首笔价或回踩MA5站稳 → 分3次每次30%，当天可加仓 ──
            if price > pos.build_first_price or pos.ma5_touch_count >= 2:
                ao_now = t.get("ao_now")
                ao_5ago = t.get("ao_5ago")
                ao_rising = ao_now is not None and ao_5ago is not None and ao_now > ao_5ago
                if not ao_rising:
                    return ("持有(等确认)", None, None, f"分批建仓1已入场,AO未回升(now={ao_now:.4f},5ago={ao_5ago:.4f}),跳过加仓")
                elif _position_capped:
                    return ("持有(仓位已满)", f"{_position_ratio_val*100:.0f}%", None, f"确认加仓信号但仓位{_position_ratio_val*100:.0f}%已达{MAX_POSITION_RATIO*100:.0f}%上限,跳过")
                elif pos.can_buy_today(today_str, atr_pct):
                    # 确认加仓分批: 最多3次, 每次30%, 当天可加仓
                    if not hasattr(pos, 'confirm_batch_count'):
                        pos.confirm_batch_count = 0
                    if not hasattr(pos, 'confirm_batch_date'):
                        pos.confirm_batch_date = None
                    if pos.confirm_batch_count >= 3:
                        pos.build_phase = 2
                        pos.base_price = pos.avg_cost
                        pos.peak_price = price
                        pos.add_count += 1
                        return ("持有(建仓完成)", None, None, "确认加仓3批已完成, 进入phase2")
                    pos.record_buy(today_str)
                    self._update_entry_cost(pos, price, 0.30, code)
                    pos.confirm_batch_count += 1
                    pos.confirm_batch_date = today_str
                    pos.add_count += 1
                    if pos.confirm_batch_count >= 3:
                        pos.build_phase = 2
                        pos.base_price = pos.avg_cost
                        pos.peak_price = price
                        pos.build_first_price = price
                        pos.reached_8pct = False
                        pos.reached_15pct = False
                        pos.trailing_stop_price = 0.0
                    return ("买入", "30%(分批2)", "buy", f"确认加仓{pos.confirm_batch_count}/3批: 30%(突破或回踩MA5)" if pos.confirm_batch_count < 3 else "确认加仓3/3批完成→phase2")
                else:
                    return ("持有(等确认)", None, None, "建仓确认信号出现但今日已买入,等明日")
            else:
                if price > t["ma5"]:
                    pos.ma5_touch_count += 1
                else:
                    pos.ma5_touch_count = 0
                return ("持有(等确认)", None, None, f"分批建仓1已入场,等回踩MA5站稳(站{pos.ma5_touch_count}根)或突破首笔价{pos.build_first_price:.3f}")

        return None