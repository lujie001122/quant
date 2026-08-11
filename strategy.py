#!/usr/bin/env python3
"""
策略判定模块
从 signal_generator.py 抽取:
  - PositionInfo 类
  - evaluate_stop 函数（分级止损/保本止盈/硬止损等）
  - evaluate_entry 函数（6通道建仓判定）
  - evaluate_t0 函数（做T评分+配对）
  - evaluate_grid 函数（网格触发）
  - 所有策略判定函数，不包含行情拉取
"""

from datetime import datetime
from market_data import ETFS, CODE_MAP

# ============================================================
# 常量(与 signal_generator 共享)
# ============================================================
CASH_RESERVE = 0
TOTAL_FUND = 220000
DEAD_RATIO = 0.6
ACTIVE_RATIO = 0.4
GRID_BUY_LEVELS = 5
GRID_SELL_LEVELS = 3
MAX_DAILY_BUYS = 1   # 每天最多1次买入(正常)
MAX_DAILY_T0 = 2     # 每天最多2次做T(正常)
DEFENSE_CODE = "159611"  # 防御标的: 电力ETF
COOLDOWN_DAYS = 2

# 模块级常量: 倒金字塔权重
INVERTED_WEIGHTS = [0.20, 0.25, 0.30, 0.35, 0.40]


# ============================================================
# PositionInfo 类
# ============================================================

class PositionInfo:
    def __init__(self, base_price=None, shares=0, cost=0.0, peak_price=0.0):
        self.base_price = base_price
        self.shares = shares
        self.cost = cost
        self.avg_cost = cost / shares if shares > 0 else 0.0
        self.current_price = 0.0
        self.peak_price = peak_price
        self.dead_shares = 0   # 底仓(P0真正分离)
        self.active_shares = 0  # 活动仓(P0真正分离)
        self.daily_trade_log = {}
        # P0: 分级减仓状态
        self.stop_level = 0
        self.below_ma20_count = 0
        self.below_ma20_date = None
        # P1: 移动止盈
        self.trailing_stop_price = 0.0
        self.reached_8pct = False
        self.reached_15pct = False
        # P1: 分批建仓
        self.build_phase = 0
        self.build_first_price = 0.0
        self.ma5_touch_count = 0
        # P1: 网格重置冷却期
        self.last_reset_date = None
        # P2: 冷静期
        self.cooldown_until = None
        self.liquidate_dates = []
        # P1: 网格熔断
        self.grid_frozen = False
        self.last_grid_trigger = None  # 当天已触发的网格档位
        # 前一根RSI(做T动态RSI)
        self.prev_rsi = None
        # 补仓次数限制
        self.add_count = 0
        # 空仓天数(趋势跟踪通道需要)
        self.empty_days = 0
        self.empty_days_date = None  # 空仓天数去重标记
        # 前一日MACD状态(Test抄底需要)
        self.prev_macd_status = None
        # 保本止盈状态
        self.breakeven_activated = False

    @property
    def has_position(self):
        return self.shares > 0

    def profit_pct(self, current_price):
        if self.avg_cost == 0: return 0.0
        return (current_price - self.avg_cost) / self.avg_cost * 100

    def drawdown_from_peak(self, current_price):
        if self.peak_price == 0: return 0.0
        return (self.peak_price - current_price) / self.peak_price * 100

    def max_daily_buys(self, atr_pct):
        if atr_pct > 0.05: return 2
        return MAX_DAILY_BUYS

    def max_daily_t0(self, atr_pct):
        if atr_pct > 0.05: return 3
        return MAX_DAILY_T0

    def _get_daily_log(self, date_str):
        """获取当日交易日志, 带默认值和类型检查"""
        log = self.daily_trade_log.get(date_str, {"buy_count": 0, "t0_count": 0})
        if not isinstance(log, dict):
            log = {"buy_count": 0, "t0_count": 0}
        return log

    def can_buy_today(self, date_str, atr_pct=None):
        max_b = self.max_daily_buys(atr_pct) if atr_pct else MAX_DAILY_BUYS
        return self._get_daily_log(date_str).get("buy_count", 0) < max_b

    def can_t0_today(self, date_str, atr_pct=None):
        max_t = self.max_daily_t0(atr_pct) if atr_pct else MAX_DAILY_T0
        return self._get_daily_log(date_str).get("t0_count", 0) < max_t

    def record_buy(self, date_str):
        log = self.daily_trade_log.setdefault(date_str, {"buy_count": 0, "t0_count": 0})
        log["buy_count"] = log.get("buy_count", 0) + 1

    def record_t0(self, date_str):
        log = self.daily_trade_log.setdefault(date_str, {"buy_count": 0, "t0_count": 0})
        log["t0_count"] = log.get("t0_count", 0) + 1

    def is_in_cooldown(self, date_str):
        if self.cooldown_until is None: return False
        return date_str <= self.cooldown_until

    def update_trailing_stop(self, price):
        if not self.has_position or self.avg_cost == 0: return
        profit_pct = self.profit_pct(price)
        if profit_pct >= 8 - 0.01 and not self.reached_8pct:
            self.reached_8pct = True
            # 优化: 8%后不改止盈线，等15%后再设
        if profit_pct >= 15 - 0.01 and not self.reached_15pct:
            self.reached_15pct = True
            self.trailing_stop_price = self.avg_cost * 1.05
        if self.reached_15pct and self.peak_price > 0:
            self.trailing_stop_price = self.peak_price * 0.95

    def update_dead_active(self):
        """减仓/清仓后同步更新底仓/活动仓"""
        self.dead_shares = int(self.shares * DEAD_RATIO)
        self.active_shares = self.shares - self.dead_shares
        if self.active_shares == 0 and self.shares > 0:
            self.active_shares = int(self.shares * ACTIVE_RATIO)

    def reset_on_liquidate(self, date_str):
        """清仓后重置所有状态"""
        self.shares = 0
        self.cost = 0
        self.avg_cost = 0
        self.base_price = None
        self.dead_shares = 0
        self.active_shares = 0
        self.build_phase = 0
        self.build_first_price = 0.0
        self.stop_level = 0
        self.below_ma20_count = 0
        self.below_ma20_date = None
        self.add_count = 0
        self.ma5_touch_count = 0
        self.reached_8pct = False
        self.reached_15pct = False
        self.trailing_stop_price = 0.0
        self.grid_frozen = False
        self.last_grid_trigger = None
        self.peak_price = 0.0
        self.breakeven_activated = False
        self.liquidate_dates.append(date_str)
        self.empty_days = 0
        from datetime import datetime as _dt, timedelta as _td
        try:
            dt = _dt.strptime(date_str, "%Y-%m-%d")
            self.cooldown_until = (dt + _td(days=COOLDOWN_DAYS)).strftime("%Y-%m-%d")
        except Exception:
            self.cooldown_until = None

    def reduce_shares(self, pct, price=0.0):
        """减仓指定比例(0-100, 百分比), 取整到100股"""
        reduce_count = int(self.shares * pct / 10000) * 100
        if reduce_count < 100:
            # 小数量时向下取整到100的整数倍,不足100则取全部
            reduce_count = int(self.shares * pct / 100)
            reduce_count = reduce_count // 100 * 100
            if reduce_count < 100 and self.shares >= 100:
                reduce_count = 100
        if reduce_count > self.shares:
            reduce_count = self.shares
        self.shares -= reduce_count
        self.cost = self.avg_cost * self.shares if self.shares > 0 else 0
        # 减仓后用当前价作为新峰值, 硬止损20%仍然有效
        self.peak_price = price
        self.update_dead_active()

    def _enter_position(self, date_str, price, channel_name):
        """6通道入场统一初始化: record_buy + 建仓状态设置"""
        self.record_buy(date_str)
        self.build_phase = 1
        self.build_first_price = price
        self.base_price = price
        self.empty_days = 0
        return "买入", channel_name, "buy"

    def to_dict(self, today_str):
        """将状态序列化为字典(用于持久化)"""
        return {
            "build_phase": self.build_phase,
            "build_first_price": self.build_first_price,
            "stop_level": self.stop_level,
            "reached_8pct": self.reached_8pct,
            "reached_15pct": self.reached_15pct,
            "trailing_stop_price": self.trailing_stop_price,
            "cooldown_until": self.cooldown_until,
            "grid_frozen": self.grid_frozen,
            "last_grid_trigger": self.last_grid_trigger,
            "last_reset_date": self.last_reset_date,
            "peak_price": self.peak_price,
            "breakeven_activated": self.breakeven_activated,
            "below_ma20_count": self.below_ma20_count,
            "below_ma20_date": self.below_ma20_date,
            "add_count": self.add_count,
            "ma5_touch_count": self.ma5_touch_count,
            "prev_rsi": self.prev_rsi,
            "prev_macd_status": self.prev_macd_status,
            "liquidate_dates": self.liquidate_dates,
            "empty_days": self.empty_days,
            "empty_days_date": self.empty_days_date,
            "daily_trade_log": {today_str: self.daily_trade_log.get(today_str, {"buy_count": 0, "t0_count": 0})},
            "shares": self.shares,
            "avg_cost": self.avg_cost,
            "current_price": self.current_price,
        }

    def from_dict(self, data):
        """从字典恢复状态(用于持久化恢复)"""
        self.build_phase = data.get("build_phase", 0)
        self.build_first_price = data.get("build_first_price", 0.0)
        self.stop_level = data.get("stop_level", 0)
        self.reached_8pct = data.get("reached_8pct", False)
        self.reached_15pct = data.get("reached_15pct", False)
        self.trailing_stop_price = data.get("trailing_stop_price", 0.0)
        self.below_ma20_count = data.get("below_ma20_count", 0)
        self.below_ma20_date = data.get("below_ma20_date", None)
        self.cooldown_until = data.get("cooldown_until")
        self.grid_frozen = data.get("grid_frozen", False)
        self.peak_price = data.get("peak_price", 0.0)
        self.breakeven_activated = data.get("breakeven_activated", False)
        self.add_count = data.get("add_count", 0)
        self.ma5_touch_count = data.get("ma5_touch_count", 0)
        self.prev_rsi = data.get("prev_rsi")
        self.prev_macd_status = data.get("prev_macd_status")
        self.liquidate_dates = data.get("liquidate_dates", [])
        self.last_grid_trigger = data.get("last_grid_trigger")
        self.last_reset_date = data.get("last_reset_date")
        self.empty_days = data.get("empty_days", 0)
        self.empty_days_date = data.get("empty_days_date")
        self.shares = data.get("shares", self.shares)
        self.avg_cost = data.get("avg_cost", self.avg_cost)
        self.current_price = data.get("current_price", self.current_price)


# ============================================================
# 辅助函数
# ============================================================

def is_auction_time(now=None):
    """集合竞价时段: 9:15-9:25, 14:57-15:00"""
    if now is None:
        now = datetime.now()
    t = now.hour * 60 + now.minute
    return (555 <= t <= 565) or (897 <= t <= 900)  # 9:15=555, 9:25=565, 14:57=897, 15:00=900


def t0_buy_score(rsi_5min, macd_5min_status, vol_ratio_5min):
    """做T买入评分(基于5分钟分时): RSI超卖+MACD多头确认+量能温和
    ★ RSI极端兜底通道: RSI < 25 直接返回3分(跳过MACD/量比检查)
    - RSI_5min 25-45: 原有3项共振评分逻辑
    - RSI_5min < 45 必须满足(超卖才摊低)
    - MACD_5min 金叉或红柱(趋势确认)
    - 量比 < 1.5(不放量砸盘)
    """
    if rsi_5min is None:
        return 0
    # RSI极端互斥: RSI > 75 时只允许卖出，买入强制归零
    if rsi_5min > 75:
        return 0
    # RSI极端兜底通道: RSI < 25 直接满分，不检查 MACD 和量比
    if rsi_5min < 25:
        return 3
    # RSI 25-75: 原有3项共振评分逻辑
    if rsi_5min >= 45:
        return 0  # 5分钟RSI不超卖，不做T买入
    if macd_5min_status in ["死叉", "绿柱放大"]:
        return 0  # 趋势恶化，禁止做T买入
    score = 1  # RSI_5min<45已满足
    if macd_5min_status in ["金叉", "红柱放大"]:
        score += 1
    if vol_ratio_5min is not None and vol_ratio_5min < 1.5:
        score += 1
    return score


def t0_sell_score(rsi_5min, macd_5min_status, vol_ratio_5min):
    """做T卖出评分(基于5分钟分时): RSI超买+MACD空头确认+量能放大
    ★ RSI极端互斥: RSI < 25 时只允许买入，卖出强制归零
    - RSI > 75: 极端兜底直接满分
    - RSI_5min 55-75: 原有3项共振评分逻辑
    - RSI_5min > 55 必须满足(超买才卖)
    - MACD_5min 死叉或绿柱(趋势结束)
    - 量比 > 1.2(放量拉升)
    """
    if rsi_5min is None:
        return 0
    # RSI极端互斥: RSI < 25 时只允许买入，卖出强制归零
    if rsi_5min < 25:
        return 0
    # RSI极端兜底通道: RSI > 75 直接满分，不检查 MACD 和量比
    if rsi_5min > 75:
        return 3
    # RSI 25-75: 原有3项共振评分逻辑
    if rsi_5min <= 55:
        return 0  # 5分钟RSI不超买，不卖出
    if macd_5min_status in ["金叉", "红柱放大"]:
        return 0  # 趋势向好，禁止做T卖出
    score = 1  # RSI_5min>55已满足
    if macd_5min_status in ["死叉", "绿柱放大"]:
        score += 1
    if vol_ratio_5min is not None and vol_ratio_5min > 1.2:
        score += 1
    return score


# ============================================================
# evaluate_stop: 分级止损/保本止盈/硬止损/趋势止盈/移动止盈
# ============================================================

def evaluate_stop(pos, t, price, today_str):
    """评估止盈止损信号
    参数:
      pos: PositionInfo 实例
      t: 技术指标字典 (包含 ma5, ma20, dif, macd_status, rsi 等)
      price: 当前价格
      today_str: 日期字符串 "YYYY-MM-DD"
    返回:
      stop_actions: [(signal_name, signal_type), ...]
        signal_type: "breakeven_stop" | "avg_stop_12pct" | "liquidate_30pct" |
                     "liquidate_trailing" | "hard_stop_20pct" |
                     "reduce_30pct_all" | "liquidate_trend" |
                     "trend_profit_sell" | "sell_active_5pct"
    """
    stop_actions = []

    if not pos.has_position:
        return stop_actions

    # ── 保本止盈: 浮盈≥10%激活, 回落到成本+2%清仓 ──
    if pos.avg_cost > 0:
        profit_pct = (price - pos.avg_cost) / pos.avg_cost
        if profit_pct >= 0.10 - 0.0001:
            pos.breakeven_activated = True
        if pos.breakeven_activated and price <= pos.avg_cost * 1.02:
            stop_actions.append((f"保本止盈(avg={pos.avg_cost:.3f}→现价{price:.3f})", "breakeven_stop"))

    # ── 均价止损12%: 从买入均价跌12%无条件清仓 ──
    if pos.avg_cost > 0 and price <= pos.avg_cost * 0.88:
        stop_actions.append((f"均价止损12%(avg={pos.avg_cost:.3f}→现价{price:.3f})", "avg_stop_12pct"))

    # ── 硬止盈30%: base_price*1.30 ──
    if pos.base_price and price >= pos.base_price * 1.30:
        stop_actions.append((f"硬止盈30%(基准{pos.base_price:.3f}→现价{price:.3f})", "liquidate_30pct"))

    # ── 移动止盈 ──
    if pos.trailing_stop_price > 0 and price <= pos.trailing_stop_price:
        label = "移动止盈8%(成本+5%)" if not pos.reached_15pct else "移动止盈15%(峰值回撤5%)"
        stop_actions.append((f"{label}:触线{pos.trailing_stop_price:.3f}", "liquidate_trailing"))

    # ── 硬止损20%: 从价格峰值回撤20%清仓 ──
    if pos.peak_price > 0 and price < pos.peak_price * 0.80:
        stop_actions.append((f"硬止损20%(峰值{pos.peak_price:.3f}→现价{price:.3f})", "hard_stop_20pct"))

    # Level 1a: 连续2天破MA20 → 减30%总仓(按天累加，同一天多次运行只算1次)
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

    # Level 1c: MACD死叉/绿柱放大 → 清仓
    if pos.stop_level == 2 and t["macd_status"] in ["死叉", "绿柱放大"]:
        stop_actions.append(("分级止损3级:MACD趋势恶化清仓", "liquidate_trend"))
        pos.stop_level = 3

    # 分级止损恢复(加严: 价>MA20+DIF>0+MACD红柱)
    if pos.stop_level > 0 and t["ma20"] and price > t["ma20"] and t["dif"] is not None and t["dif"] > 0 and t["macd_status"] in ["红柱放大", "红柱缩短"]:
        pos.stop_level = 0
        pos.below_ma20_count = 0
        pos.below_ma20_date = None

    # 分级止损恢复: stop_level=2 但 MACD 未恶化且价格恢复 → 重置
    if pos.stop_level == 2 and t["macd_status"] not in ["死叉", "绿柱放大"] and t["ma20"] and price > t["ma20"] and t["dif"] is not None and t["dif"] > 0:
        pos.stop_level = 0
        pos.below_ma20_count = 0
        pos.below_ma20_date = None

    # P1: 趋势止盈(MACD红柱缩短+破MA5)
    if t["macd_status"] == "红柱缩短" and price < t["ma5"]:
        stop_actions.append(("趋势止盈(MACD红柱缩短+破MA5)卖10%活动仓", "trend_profit_sell"))

    # 破MA5卖活动仓5%(P0: active=0时不触发)
    if price < t["ma5"] and t["rsi"] and t["rsi"] > 50:
        if pos.active_shares > 0:
            stop_actions.append(("破MA5卖活动仓5%", "sell_active_5pct"))

    return stop_actions


def resolve_stop_signal(pos, stop_actions):
    """从 stop_actions 列表解析最终止盈止损信号文本"""
    if not stop_actions:
        if pos.has_position:
            return "未触发"
        else:
            return "未触发(无持仓)"

    # 优先级: 清仓 > 减仓 > 卖活动仓
    for sig_name, sig_type in stop_actions:
        if sig_type in ("liquidate_trend", "liquidate_trailing", "liquidate_30pct", "avg_stop_12pct", "hard_stop_20pct", "breakeven_stop"):
            return sig_name
    for sig_name, sig_type in stop_actions:
        if sig_type in ("reduce_30pct_all",):
            return sig_name
    return stop_actions[0][0]


# ============================================================
# evaluate_entry: 6通道建仓判定
# ============================================================

def evaluate_entry(pos, t, price, realtime, positions, all_klines, code, today_str, atr_pct, defense_weak):
    """6通道建仓判定 + 补仓/确认加仓
    返回:
      (action, position_ratio, trade_type, reason)
      如果 action 不为 "持有" 相关，说明已匹配通道
      如果 action 以 "持有" 开头，说明无信号
    """
    if pos.has_position or pos.is_in_cooldown(today_str):
        return None  # 由调用方处理持有逻辑

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

    if pos.build_phase == 0:
        # 通道1: RSI抄底 (MACD金叉+RSI≤80+价>MA20) → 30%
        if t["macd_status"] == "金叉" and (t["rsi"] is None or t["rsi"] <= 80) and t["ma20"] and price > t["ma20"] and pos.can_buy_today(today_str, atr_pct):
            action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(RSI抄底)")
            return (action, position_ratio, trade_type, f"RSI抄底:MACD金叉(RSI={t['rsi']})")

        # 通道2: 趋势跟踪 (空仓>10天+价>MA20+MACD红柱) → 30%
        if pos.empty_days > 10 and t["macd_status"] in ["红柱放大", "红柱缩短"] and t["ma20"] and price > t["ma20"] and pos.can_buy_today(today_str, atr_pct):
            action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(趋势跟踪)")
            return (action, position_ratio, trade_type, f"趋势跟踪:价>MA20+{t['macd_status']}")

        # 通道3: 突破入场 (20日新高+MACD金叉+价>MA20) → 30%
        if t["macd_status"] == "金叉" and t["ma20"] and price > t["ma20"] and pos.can_buy_today(today_str, atr_pct):
            if len(all_klines.get(code, [])) >= 21:
                closes_20 = [k["close"] for k in all_klines[code][-21:-1]]
                if closes_20 and price > max(closes_20):
                    action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(突破入场)")
                    return (action, position_ratio, trade_type, "突破入场:20日新高+金叉")

        # 通道4: 分批建仓 (金叉/红柱放大+RSI>40+站MA5+价>MA20) → 30%
        if t["macd_status"] in ["金叉", "红柱放大"] and rsi_ok and price > t["ma5"] and t["ma20"] and price > t["ma20"] and pos.can_buy_today(today_str, atr_pct):
            action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(分批1)")
            return (action, position_ratio, trade_type, "分批建仓1:首笔30%试探(MACD健康+RSI满足+站上MA5)")

        # 通道5: Test抄底 (RSI<35+绿柱缩短+站MA5+价>MA20+前一日也是绿柱缩短) → 30%
        if t["rsi"] is not None and t["rsi"] < 35 and t["macd_status"] == "绿柱缩短" and t["ma5"] and price > t["ma5"] and t["ma20"] and price > t["ma20"] and pos.prev_macd_status == "绿柱缩短" and pos.can_buy_today(today_str, atr_pct):
            action, position_ratio, trade_type = pos._enter_position(today_str, price, "30%(Test抄底)")
            return (action, position_ratio, trade_type, f"Test抄底:RSI={t['rsi']:.1f}+绿柱缩短+站MA5")

        # 通道6: 试探建仓 (绿柱缩短/震荡+RSI>40+站MA5+价>MA20) → 15%
        if t["macd_status"] in ["绿柱缩短", "震荡"] and rsi_minimal and price > t["ma5"] and t["ma20"] and price > t["ma20"] and pos.can_buy_today(today_str, atr_pct):
            action, position_ratio, trade_type = pos._enter_position(today_str, price, "15%(试探)")
            return (action, position_ratio, trade_type, f"试探建仓:15%底仓(MACD{t['macd_status']}+RSI{t['rsi']}+站上MA5)")

        return ("持有(观望)", "0%", None, f"建仓条件未满足(MACD{t['macd_status']},RSI{t['rsi']})")

    elif pos.build_phase == 1:
        _position_ratio_val = pos.shares * price / TOTAL_FUND
        _position_capped = _position_ratio_val >= 0.50

        # ── 逆势补仓: 跌超3%允许第二次买入（需检查仓位上限）──
        if pos.build_first_price > 0 and pos.add_count < 5:
            dip_pct = (price - pos.build_first_price) / pos.build_first_price
            if dip_pct < -0.03 and pos.can_buy_today(today_str, atr_pct) and t["macd_status"] not in ["死叉", "绿柱放大"] and (t["rsi"] is None or t["rsi"] < 40):
                if _position_capped:
                    return ("持有(仓位已满)", f"{_position_ratio_val*100:.0f}%", None, f"补仓信号但仓位{_position_ratio_val*100:.0f}%已达50%上限,跳过")
                pos.record_buy(today_str)
                pos.add_count += 1
                return ("买入", "15%(补仓)", "buy", f"逆势补仓:跌{dip_pct*100:.0f}%加仓15%(MACD{t['macd_status']})")

        # ── 确认加仓: 突破首笔价或回踩MA5站稳（需检查仓位上限）──
        if price > pos.build_first_price or pos.ma5_touch_count >= 2:
            ao_now = t.get("ao_now")
            ao_5ago = t.get("ao_5ago")
            ao_rising = ao_now is not None and ao_5ago is not None and ao_now > ao_5ago
            if not ao_rising:
                return ("持有(等确认)", None, None, f"分批建仓1已入场,AO未回升(now={ao_now:.4f},5ago={ao_5ago:.4f}),跳过加仓")
            elif _position_capped:
                return ("持有(仓位已满)", f"{_position_ratio_val*100:.0f}%", None, f"确认加仓信号但仓位{_position_ratio_val*100:.0f}%已达50%上限,跳过")
            elif pos.can_buy_today(today_str, atr_pct):
                pos.record_buy(today_str)
                pos.build_phase = 2
                pos.base_price = pos.avg_cost
                pos.peak_price = price
                pos.build_first_price = price
                pos.reached_8pct = False
                pos.reached_15pct = False
                pos.trailing_stop_price = 0.0
                pos.add_count += 1
                return ("买入", "70%(分批2)", "buy", f"分批建仓2:确认加70%(突破首笔价{pos.build_first_price:.3f}或回踩MA5站稳)")
            else:
                return ("持有(等确认)", None, None, "建仓确认信号出现但今日已买入,等明日")
        else:
            if price > t["ma5"]:
                pos.ma5_touch_count += 1
            else:
                pos.ma5_touch_count = 0
            return ("持有(等确认)", None, None, f"分批建仓1已入场,等回踩MA5站稳(站{pos.ma5_touch_count}根)或突破首笔价{pos.build_first_price:.3f}")

    return None


# ============================================================
# evaluate_t0: 做T评分+配对
# ============================================================

def evaluate_t0(t, pos, price, today_str, atr_pct):
    """评估做T信号
    参数:
      t: 技术指标字典
      pos: PositionInfo 实例
      price: 当前价格
      today_str: 日期字符串
      atr_pct: ATR百分比
    返回:
      dict with keys: t0_signal, buy_score, sell_score, trade_type, action, position_ratio, reason, t0_pair
      或 None (无信号)
    """
    rsi_5min = t.get("rsi_5min")
    macd_5min_status = t.get("macd_5min_status")
    vol_ratio_5min = t.get("vol_ratio_5min")
    ma20_5min_slope = t.get("ma20_5min_slope")

    now = datetime.now()
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=0, second=0, microsecond=0)
    is_market_hours = market_open <= now <= market_close
    in_auction = is_auction_time(now)

    # 11点前不做T（5分钟K线不够48根，MA20斜率不可靠）
    if not is_market_hours or rsi_5min is None or in_auction or now.hour < 11:
        buy_score = 0
        sell_score = 0
    else:
        buy_score = t0_buy_score(rsi_5min, macd_5min_status, vol_ratio_5min)
        sell_score = t0_sell_score(rsi_5min, macd_5min_status, vol_ratio_5min)

        # MA20斜率趋势过滤: 强趋势禁止反向做T
        if ma20_5min_slope is not None:
            if ma20_5min_slope > 0.003:   # 上升趋势(斜率>0.3%) → 禁止T卖出
                sell_score = 0
            elif ma20_5min_slope < -0.005:  # 下降趋势(斜率<-0.5%) → 禁止T买入
                buy_score = 0

    t0_signal = "无"
    if not is_market_hours:
        t0_signal = "无(非交易时段)"
    elif in_auction:
        t0_signal = "无(集合竞价时段)"

    result = {
        "t0_signal": t0_signal,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "trade_type": None,
        "action": None,
        "position_ratio": None,
        "reason": None,
        "t0_pair": None,
    }

    if not pos.has_position:
        if buy_score >= 2 or sell_score >= 2:
            result["t0_signal"] = "有做T5m信号但无底仓"
        return result

    # T入: 5分钟RSI<45+5分钟MACD多头+量比温和
    if buy_score >= 2:
        if pos.can_t0_today(today_str, atr_pct):
            t0_signal = f"买入做T5m({buy_score}项共振,RSI5m={rsi_5min},MACD5m={macd_5min_status},量比5m={vol_ratio_5min})"
        else:
            t0_signal = f"买入做T5m({buy_score}项信号,今日做T次数已用尽({pos._get_daily_log(today_str).get('t0_count', 0)}/{pos.max_daily_t0(atr_pct)}))"
        result["t0_signal"] = t0_signal
    # T出: 5分钟RSI>55+5分钟MACD空头+量比放大
    elif sell_score >= 2:
        if pos.can_t0_today(today_str, atr_pct):
            profit_ok = pos.avg_cost > 0 and price > pos.avg_cost
            if profit_ok:
                t0_signal = f"卖出做T5m({sell_score}项共振,RSI5m={rsi_5min},MACD5m={macd_5min_status},量比5m={vol_ratio_5min})"
            else:
                t0_signal = f"卖出做T5m信号但价低于成本({pos.avg_cost:.3f}),不执行"
        else:
            t0_signal = f"卖出做T5m({sell_score}项信号,今日做T次数已用尽({pos._get_daily_log(today_str).get('t0_count', 0)}/{pos.max_daily_t0(atr_pct)}))"
        result["t0_signal"] = t0_signal

    return result


def evaluate_t0_execute(result, t, pos, price, today_str, atr_pct):
    """从 evaluate_t0 结果中解析出具体的执行动作(trade_type, action, position_ratio, reason, t0_pair)
    在 generate_signals 的优先级2阶段调用
    """
    buy_score = result["buy_score"]
    sell_score = result["sell_score"]
    t0_signal = result["t0_signal"]

    if ((buy_score >= 2 or sell_score >= 2) and pos.has_position
          and pos.can_t0_today(today_str, atr_pct)):

        if "买入" in t0_signal:
            _position_ratio_val = pos.shares * price / TOTAL_FUND
            _position_capped = _position_ratio_val >= 0.50
            if _position_capped:
                return ("持有(仓位已满)", f"{_position_ratio_val*100:.0f}%", None,
                        f"做T买入信号但仓位{_position_ratio_val*100:.0f}%已达50%上限,跳过")
            pos.record_t0(today_str)
            ratio = 0.30
            # 配对卖出挂单: 高位卖出 (5min ATR×1.1)
            atr_5m = t.get("atr_5min")
            t0_pair = None
            if atr_5m and price > 0:
                pair_price = round(price + atr_5m * 1.1, 3)
                pair_shares = int(pos.shares * 0.30 / 100) * 100
                if pair_shares >= 100 and pair_price > 0:
                    spread = (pair_price - price) * pair_shares
                    if spread > 10:
                        t0_pair = {
                            "trade_type": "t0_pair",
                            "action": "卖出(配对挂单)",
                            "price": price,
                            "pair_price": pair_price,
                            "shares": pair_shares,
                            "spread": round(spread, 2),
                            "reason": f"做T买入后高位卖出: ATR_5min={atr_5m:.4f}, 挂单价={pair_price:.3f}",
                        }
            return ("买入", f"{ratio*100:.0f}%", "t0", t0_signal + "(量比)", t0_pair)

        elif "卖出" in t0_signal:
            pos.record_t0(today_str)
            ratio = 0.1 if "半" in t0_signal else 0.2
            # 配对买入挂单: 低位吃回 (5min ATR×1.1)
            atr_5m = t.get("atr_5min")
            t0_pair = None
            if atr_5m and price > 0:
                pair_price = round(price - atr_5m * 1.1, 3)
                pair_shares = int(pos.shares * 0.30 / 100) * 100
                if pair_shares >= 100 and pair_price > 0:
                    spread = (price - pair_price) * pair_shares
                    if spread > 10:
                        t0_pair = {
                            "trade_type": "t0_pair",
                            "action": "买入(配对挂单)",
                            "price": price,
                            "pair_price": pair_price,
                            "shares": pair_shares,
                            "spread": round(spread, 2),
                            "reason": f"做T卖出后低位接回: ATR_5min={atr_5m:.4f}, 挂单价={pair_price:.3f}",
                        }
            return ("卖出", f"{ratio*100:.0f}%", "t0", t0_signal + "(量比)", t0_pair)

    return None


# ============================================================
# evaluate_grid: 网格触发 + 重置确认 + 网格表
# ============================================================

def evaluate_grid_signals(price, base_price, spacing, grid_frozen=False):
    """网格触达(含熔断机制)"""
    if base_price is None:
        return "无(未设基准价)", 0
    if grid_frozen:
        return "网格冻结(跌破第5档,等站上MA5解冻)", 0

    tolerance = spacing * 0.15

    for i in range(1, GRID_BUY_LEVELS + 1):
        grid_price = base_price * (1 - spacing * i)
        if abs(price - grid_price) <= tolerance * grid_price:
            weight = INVERTED_WEIGHTS[i - 1]
            frozen_note = " [熔断!第5档后冻结网格]" if i == 5 else ""
            return f"触及第{i}档买入(网格价{grid_price:.3f},仓位{weight*100:.0f}%){frozen_note}", weight

    for i in range(1, GRID_SELL_LEVELS + 1):
        grid_price = base_price * (1 + spacing * i)
        if abs(price - grid_price) <= tolerance * grid_price:
            return f"触及第{i}档卖出(网格价{grid_price:.3f})", 0.20

    return "无", 0


def check_grid_reset(price, base_price, spacing, date_str, last_reset_date=None):
    """网格基准重置(含冷却期+下行重置)"""
    if base_price is None:
        return base_price, False, last_reset_date
    # 冷却期: 同一天不二次重置
    if last_reset_date is not None and date_str == last_reset_date:
        return base_price, False, last_reset_date
    # 上行重置
    upper_limit = base_price * (1 + spacing * 4)
    if price > upper_limit:
        return round(price, 3), True, date_str
    # 下行重置
    lower_limit = base_price * (1 - spacing * 6)
    if price < lower_limit:
        return round(price, 3), True, date_str
    return base_price, False, last_reset_date


def compute_grid_table(base_price, spacing, fund):
    if base_price is None: return None
    import math
    table = {"base": base_price, "spacing": spacing, "buy": [], "sell": []}
    for i in range(1, GRID_BUY_LEVELS + 1):
        p = base_price * (1 - spacing * i)
        w = INVERTED_WEIGHTS[i - 1]
        per_fund = fund * w
        if math.isnan(p) or p <= 0:
            continue
        shares = int(per_fund / p / 100) * 100
        table["buy"].append({"grid": i, "price": round(p, 3), "shares": shares,
                             "fund": round(shares * p, 0), "weight": f"{w*100:.0f}%"})
    for i in range(1, GRID_SELL_LEVELS + 1):
        p = base_price * (1 + spacing * i)
        table["sell"].append({"grid": i, "price": round(p, 3)})
    return table