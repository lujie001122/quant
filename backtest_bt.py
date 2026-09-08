#!/usr/bin/env python3
"""
backtrader 回测引擎 v3: 9 ETF 共享资金池量化策略
用 backtrader 原生 order/broker 体系，策略只管信号+仓位比例

核心:
  - 9只ETF共享一个broker资金池(backtrader天然支持)
  - 每只ETF资金=总资金/9, 按比例下单
  - 4通道入场 + 分级止损 + 移动止盈 + 趋势止盈
  - Wilder RSI / 自定义MACD / AO动量(与实盘一致)

═══════════════════════════════════════════════════════════════════
  回测 vs 实盘 差异点（不重构，仅记录）
═══════════════════════════════════════════════════════════════════
1. 撮合引擎:
   - 回测: backtrader broker 模拟撮合，next()内下单即成交
   - 实盘: executor.SimBroker/RealBroker，通过同花顺委托下单

2. 挂单与撤单:
   - 回测: 无挂单检查，order提交后立即撮合，不存在撤单→重挂流程
   - 实盘: order_manager 维护挂单状态，有撤单→重挂机制（轮动撤单等）

3. Intent 去重:
   - 回测: 无 intent 文件去重机制，每根K线独立决策
   - 实盘: 通过 order_manager 写入 orders/intent/*.json 做去重，
           避免同一信号重复下单

4. 滑点与成交价:
   - 回测: SLIPPAGE_PCT (0.1%) 模拟滑点，按收盘价×(1±slippage)估算
   - 实盘: 依赖同花顺实际成交价，无显式滑点参数

5. 信号生成链路:
   - 回测: 直接调用 strategies.rsi_macd 的 evaluate_entry/evaluate_stop
           （简化路径，指标由 backtrader 内置计算）
   - 实盘: signal_generator.generate_signals() → order_manager.create_order()
           → order_manager.create_order() → executor.execute()
           （完整链路，含信号清洗、风控、仓位管理）
═══════════════════════════════════════════════════════════════════
"""
import sys
import os
from datetime import datetime, timedelta
import backtrader as bt
import pandas as pd
import yaml

# ── 加载 config.yaml ──
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
with open(_CONFIG_PATH) as f:
    CONF = yaml.safe_load(f)

# Phase 3: 引用 market_data + position_info + strategies 模块
from market_data import fetch_klines_daily, calc_rsi_wilder, calc_macd, calc_ao, calc_atr
from position_info import PositionInfo, DEFENSE_CODE, TREND_PROFIT_MAX_DAILY, TREND_PROFIT_COOLDOWN_DAYS
from strategies.rsi_macd import RSIMACDStrategy, check_defense
# 网格策略已删除(集中模式仓位已满，网格几乎不触发)
from risk_manager import check_stop_loss as _rm_check_stop_loss, resolve_stop_signal as _rm_resolve_stop

_rsi_macd_strategy = RSIMACDStrategy()


def evaluate_stop(pos, t, price, today_str):
    """评估止盈止损信号（统一入口：委托给 RiskManager，避免逻辑重复）"""
    return _rm_check_stop_loss(pos, t, price, today_str)


def resolve_stop_signal(pos, stop_actions):
    """解析止盈止损信号（统一入口：委托给 RiskManager）"""
    return _rm_resolve_stop(pos, stop_actions)

def check_stop_loss(pos, t, price, today_str):
    """risk_manager.check_stop_loss 的模块级别名"""
    return _rm_check_stop_loss(pos, t, price, today_str)


def evaluate_entry(pos, t, price, realtime, positions, all_klines, code, today_str, atr_pct, defense_weak):
    return _rsi_macd_strategy.evaluate_entry(pos, t, price, realtime, positions, all_klines, code, today_str, atr_pct, defense_weak)

# ═══════════════════════════════════════════════
#  Config (从 config.yaml 同步)
# ═══════════════════════════════════════════════
TRADE_START = CONF['backtest']['trade_start']
TRADE_END = CONF['backtest']['trade_end']
TOTAL_FUND = CONF['total_fund']
MAX_PER_ETF = CONF['max_per_etf']
FEE = CONF['backtest']['fee']
SLIPPAGE_PCT = CONF['backtest']['slippage_pct']
COOLDOWN_DAYS = CONF['cooldown_days']
DEAD_RATIO = CONF['dead_ratio']
ACTIVE_RATIO = CONF['active_ratio']
POSITION_CAP = CONF['entry']['position_cap']           # 仓位上限
CONFIRM_ENTRY_RATIO = CONF['entry']['confirm_entry_ratio']  # 确认加仓比例
RISK_FREE_RATE = CONF['backtest']['risk_free_rate']

# 默认ETF配置(回退用, etf_pool.json不存在时使用)
_DEFAULT_CODES = {
    "159516": {"name": "半导体设备ETF", "sid": "sz159516"},
    "515880": {"name": "通信ETF", "sid": "sh515880"},
    "588170": {"name": "科创半导体ETF", "sid": "sh588170"},
    "159532": {"name": "中证2000ETF", "sid": "sz159532"},
    "515050": {"name": "中证全指ETF", "sid": "sh515050"},
    "159611": {"name": "电力ETF", "sid": "sz159611"},
    "513780": {"name": "港股创新药ETF", "sid": "sh513780"},
    "512400": {"name": "有色金属ETF", "sid": "sh512400"},
    "515220": {"name": "煤炭ETF", "sid": "sh515220"},
}

from state_center import get_code_map, get_etfs_config

# 每周轮动: 引入候选池
from rotation import ETF_CANDIDATES

CODES = get_code_map()

# ═══════════════════════════════════════════════
#  Data fetch (腾讯前复权)
# ═══════════════════════════════════════════════

def fetch_daily(code, sid, data_start=None, data_end=None):
    """获取前复权日K线数据 → 委托 market_data.fetch_klines_daily"""
    # 默认拉全量（回退用），调用方可传入 data_start/data_end 限制区间
    start = data_start or "20250101"
    end = data_end or "20261231"
    klines = fetch_klines_daily(code, start=start, end=end)
    if not klines:
        return None
    df = pd.DataFrame(klines)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)
    return df


# ═══════════════════════════════════════════════
#  Custom Commission
# ═══════════════════════════════════════════════
class ETFCommission(bt.CommInfoBase):
    """固定手续费5元 + 0.1%滑点"""
    params = (("fee", FEE), ("slippage", SLIPPAGE_PCT))

    def _getcommission(self, size, price, pseudoexec):
        return abs(size) * price * self.p.slippage + self.p.fee


# ═══════════════════════════════════════════════
#  Custom Indicators
# ═══════════════════════════════════════════════
class WilderRSI(bt.Indicator):
    lines = ("rsi",)
    params = (("period", 14),)

    def __init__(self):
        self.addminperiod(self.p.period + 1)
        self._avg_gain = None
        self._avg_loss = None

    def next(self):
        n = self.p.period
        if self._avg_gain is None:
            # 首次: 用SMA初始化
            gains, losses = [], []
            for i in range(1, n + 1):
                d = self.data[-(n - i)] - self.data[-(n - i + 1)]
                gains.append(max(d, 0)); losses.append(max(-d, 0))
            self._avg_gain = sum(gains) / n
            self._avg_loss = sum(losses) / n
        else:
            # 递推: 只用最新一个bar
            d = self.data[0] - self.data[-1]
            self._avg_gain = (self._avg_gain * (n - 1) + max(d, 0)) / n
            self._avg_loss = (self._avg_loss * (n - 1) + max(-d, 0)) / n
        self.lines.rsi[0] = 100.0 if self._avg_loss == 0 else round(100 - 100 / (1 + self._avg_gain / self._avg_loss), 2)


class MACDStatus(bt.Indicator):
    lines = ("dif", "dea", "hist", "status")
    STATUS_MAP = {0: "震荡", 1: "金叉", 2: "死叉", 3: "红柱放大", 4: "红柱缩短", 5: "绿柱放大", 6: "绿柱缩短"}
    params = (("fast", 12), ("slow", 26), ("signal", 9))

    def __init__(self):
        self.addminperiod(self.p.slow + self.p.signal)
        self._e12 = None; self._e26 = None; self._e9 = None
        self._prev_dif = None; self._prev_dea = None; self._prev_hist = None

    def next(self):
        size = len(self.data)
        fast, slow, sig = self.p.fast, self.p.slow, self.p.signal
        min_needed = slow + sig

        if size < min_needed:
            self.lines.status[0] = 0; return

        if self._e12 is None:
            # 初始化: 用SMA预热
            closes = [self.data[-i] for i in range(min(size, min_needed + fast) - 1, -1, -1)]
            self._e12 = sum(closes[:fast]) / fast
            self._e26 = sum(closes[:slow]) / slow
            difs = []
            for i in range(len(closes)):
                if i < fast:
                    difs.append(self._e12 - self._e26); continue
                self._e12 = closes[i] * (2/(fast+1)) + self._e12 * (1 - 2/(fast+1))
                if i < slow:
                    difs.append(self._e12 - self._e26); continue
                self._e26 = closes[i] * (2/(slow+1)) + self._e26 * (1 - 2/(slow+1))
                difs.append(self._e12 - self._e26)
            self._e9 = difs[slow]
            deas = []
            for d in difs[slow+1:]:
                self._e9 = d * (2/(sig+1)) + self._e9 * (1 - 2/(sig+1))
                deas.append(self._e9)
            if len(deas) < 2:
                self.lines.status[0] = 0; return
            # 保存前一个bar的值用于状态判断
            self._prev_dif = difs[-2]; self._prev_dea = deas[-2]
            self._prev_hist = 2 * (self._prev_dif - self._prev_dea)
        else:
            # 递推: 只用最新一个bar
            c = self.data[0]
            self._e12 = c * (2/(fast+1)) + self._e12 * (1 - 2/(fast+1))
            self._e26 = c * (2/(slow+1)) + self._e26 * (1 - 2/(slow+1))
            self._prev_dif = self.lines.dif[-1] if len(self.lines.dif) > 1 else self._e12 - self._e26
            self._prev_dea = self.lines.dea[-1] if len(self.lines.dea) > 1 else self._e9
            self._prev_hist = self.lines.hist[-1] if len(self.lines.hist) > 1 else 2 * (self._prev_dif - self._prev_dea)
            self._e9 = (self._e12 - self._e26) * (2/(sig+1)) + self._e9 * (1 - 2/(sig+1))

        d = self._e12 - self._e26
        e = self._e9
        h = 2 * (d - e)
        dp, hp = self._prev_dif, self._prev_hist

        self.lines.dif[0] = round(d, 4)
        self.lines.hist[0] = round(h, 4)

        if d > e and dp <= self._prev_dea: s = 1
        elif d < e and dp >= self._prev_dea: s = 2
        elif d > e and h > hp: s = 3
        elif d > e and h < hp: s = 4
        elif d < e and h < hp: s = 5
        elif d < e and h > hp: s = 6
        else: s = 0
        self.lines.status[0] = s


class AOIndicator(bt.Indicator):
    """Awesome Oscillator: SMA5(median) - SMA34(median), median=(H+L)/2"""
    lines = ("ao",)
    params = (("fast", 5), ("slow", 34))

    def __init__(self):
        self.addminperiod(self.p.slow)

    def next(self):
        size = len(self.data)
        if size < self.p.slow: return
        # 用(H+L)/2计算median, 与实盘calc_ao一致
        medians = [(self.data.high[-i] + self.data.low[-i]) / 2 for i in range(min(size, self.p.slow))]
        medians.reverse()
        if len(medians) < self.p.slow: return
        self.lines.ao[0] = sum(medians[-5:]) / 5 - sum(medians[-34:]) / 34


# ═══════════════════════════════════════════════
#  Strategy
# ═══════════════════════════════════════════════
class ETFStrategy(bt.Strategy):
    params = (
        ("max_per_etf", MAX_PER_ETF),
        ("cooldown_days", COOLDOWN_DAYS),
        ("fund_per_etf", 0),  # 每只ETF资金基数，默认0（与实盘一致）
        ("rotation_mode", False),  # 全仓轮动: 每周一满仓动量第1名
        ("concentrated_mode", False),  # 集中持仓: 只持有TOP N最强ETF
        ("weekly_rotation_mode", False),  # 每周轮动: 每5日从候选池重算TOP3动量排名
        ("concentrated_pyramid_mode", False),  # 集中TOP N+50%建仓+趋势加码
        ("pyramid_mode", False),  # 趋势加码: 盈利后金字塔加仓
        ("momentum_mode", "c2"),  # 动量评分: simple(20日涨幅) | c2(多因子加权)
        ("trail_mode", "fixed"),  # 移动止盈: fixed(固定7%) | e1(ATR自适应) | e2(ATR自适应2)
        ("init_pct", 0.30),  # 建仓比例: 0.30(30%) | 0.50(50%)
        ("top_n", 3),  # 集中持仓TOP N只ETF
        ("lookback", 20),  # 动量回看期(日)
        ("ma60_filter", False),  # MA60趋势过滤: 价格>MA60才持有
        ("dynamic_pct", False),  # 动态仓位: 动量越强仓位越大
        ("rebalance", 5),  # 重平衡周期(天): 每N天重排名换仓，默认5天
        ("stop_loss", 0.25),  # 固定止损比例: 对齐config.yaml stop_loss.hard_stop_pct=0.25
        ("trail_pct", 0.12),  # 移动止盈回撤比例(fixed模式): peak_price*(1-trail_pct)
        ("sector_diversify", False),  # 板块分散: TOP3必须来自不同板块
        ("confirm_days", 0),  # 换仓延迟确认天数: 连续N天在/不在TOP N才换仓
        ("trend_entry", False),  # 趋势建仓通道: TOP3+多头排列+空仓时直接50%建仓
        ("rsi_entry_max", 55),  # RSI抄底上限: 集中模式通道1的RSI阈值(默认55)
        ("rotation_interval", 5),  # 每周轮动间隔(天): 每N天重算TOP排名, 默认5天
    )

    # 板块分类: 用于板块分散
    SECTOR_MAP = {
        "159516": "半导体", "159532": "宽基",
        "515880": "通信",
        "159611": "电力",
        "515050": "宽基", "588170": "半导体",
        "513780": "医药",
        "512400": "有色",
        "515220": "煤炭",
        # 每周轮动候选池板块
        "159995": "半导体", "588000": "宽基", "159819": "科技",
        "159770": "科技", "510300": "宽基", "512660": "军工",
        "515790": "新能源", "512880": "金融", "512800": "金融",
        "518880": "黄金", "159928": "消费", "513130": "港股科技",
        "513300": "海外", "159985": "商品", "159949": "宽基",
        "512890": "红利", "159981": "能源",
    }

    # 指标预热所需的最小K线数: MACD(26+9=35) + AO(34) → 35
    MIN_WARMUP = 35

    def __init__(self):
        # 指标
        self.rsi = {}; self.macd = {}; self.ao = {}
        self.atr = {}
        self.ma5 = {}; self.ma20 = {}; self.ma10 = {}; self.ma60 = {}
        for d in self.datas:
            n = d._name
            self.rsi[n] = WilderRSI(d.close)
            self.macd[n] = MACDStatus(d.close)
            self.ao[n] = AOIndicator(d)
            self.atr[n] = bt.indicators.ATR(d, period=14)
            self.ma5[n] = bt.indicators.SMA(d.close, period=5)
            self.ma10[n] = bt.indicators.SMA(d.close, period=10)
            self.ma20[n] = bt.indicators.SMA(d.close, period=20)
            self.ma60[n] = bt.indicators.SMA(d.close, period=60)

        # 每ETF状态
        self.ps = {}
        for d in self.datas:
            n = d._name
            self.ps[n] = {
                "base": 0.0, "first_price": 0.0, "build_phase": 0,
                "bought_today": False, "stop_level": 0,
                "below_ma20": 0, "below_ma20_date": "",
                "reached_activation": False, "reached_lockin": False, "trail": 0.0,
                "add_count": 0, "empty_days": 0,
                "cooldown_until": "", "peak_price": 0.0,
                "ma5_touch_count": 0, "prev_macd_status": "震荡",
                "pending_order": None, "stop_cooldown": False,
                # 趋势止盈冷却机制: 单日最多3次，触发后3天冷却
                "trend_sell_today": 0,  # 当日趋势止盈次数
                "trend_sell_cooling_until": "",  # 趋势止盈冷却截止日期
                # 破MA5卖活动仓冷却机制: 单日最多3次，触发后3天冷却
                "ma5_sell_today": 0,  # 当日破MA5卖活动仓次数
                "ma5_sell_cooling_until": "",  # 破MA5卖活动仓冷却截止日期
                "breakeven_activated": False,  # 浮盈≥10%激活保本线
                "entry_avg_cost": 0.0,  # 建仓锁定均价(止损用)
                "market_reduced": False,  # 大盘自适应减仓标记
                "market_drawdown_level": 0,  # 大盘回撤减仓等级: 0=无, 1=>5%, 2=>8%, 3=>12%
                "market_drawdown_date": "",  # 大盘回撤减仓最近触发日期(防同日重复)
            }
        # 全仓轮动状态
        self._rotation_etf = None  # 当前持有的ETF名称
        self._rotation_entry_price = 0.0  # 轮动入场价(止损用)

        # 每周轮动状态
        self._weekly_top3 = set()  # 当前TOP3 ETF代码
        self._weekly_rotation_day = 0  # 轮动日计数器

        # 统计
        self.trades = []; self.win_trades = 0; self.loss_trades = 0
        self.win_amount = 0.0; self.loss_amount = 0.0
        self.daily_values = []
        self._order_pending = {}  # data_name -> order
        self._last_date = ""  # 上次交易日，用于每日计数器重置
        self._day_count = 0  # 交易日计数器，用于重平衡周期判断

    def notify_order(self, order):
        """订单状态回调"""
        name = order.data._name
        if order.status in [order.Completed]:
            self._order_pending.pop(name, None)
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self._order_pending.pop(name, None)

    def notify_trade(self, trade):
        """交易完成回调 — 统计盈亏 + 打印明细"""
        def _fmt_date(dt_val):
            if not dt_val:
                return ''
            try:
                if isinstance(dt_val, datetime):
                    return dt_val.strftime('%Y-%m-%d')
                return datetime.fromordinal(int(dt_val)).strftime('%Y-%m-%d')
            except:
                return str(dt_val)

        if trade.isclosed:
            name = trade.data._name
            ps = self.ps.get(name, {})
            entry_reason = ps.get('_last_entry_reason', '未知')
            exit_reason = ps.get('_last_exit_reason', '未知')
            hold_days = 0
            if trade.dtopen and trade.dtclose:
                try:
                    d_open = trade.dtopen if isinstance(trade.dtopen, datetime) else datetime.fromordinal(int(trade.dtopen))
                    d_close = trade.dtclose if isinstance(trade.dtclose, datetime) else datetime.fromordinal(int(trade.dtclose))
                    hold_days = (d_close - d_open).days
                except:
                    hold_days = 0
            pnl_pct = (trade.pnl / (trade.price * abs(trade.size))) * 100 if trade.price and trade.size else 0

            self.trades.append({
                'date': _fmt_date(trade.dtclose),
                'code': name,
                'direction': '卖出',
                'entry_reason': entry_reason,
                'exit_reason': exit_reason,
                'price': trade.price,
                'pnl': trade.pnl,
                'pnl_pct': pnl_pct,
                'hold_days': hold_days,
            })

            if trade.pnl >= 0:
                self.win_trades += 1; self.win_amount += trade.pnl
            else:
                self.loss_trades += 1; self.loss_amount += abs(trade.pnl)

    def _get_position_value(self, data):
        """获取持仓市值"""
        return self.getposition(data).size * data.close[0]

    def _get_avg_cost(self, data):
        """获取持仓均价 (backtrader position.price)"""
        return self.getposition(data).price

    def _get_shares(self, data):
        return self.getposition(data).size

    def _has_position(self, data):
        return self.getposition(data).size > 0

    def _buy(self, data, pct, reason):
        """按每只ETF资金基数买入，与实盘一致：使用 fund_per_etf 而非 TOTAL_FUND"""
        name = data._name
        target_value = min(self.p.fund_per_etf * pct, self.p.fund_per_etf * POSITION_CAP)
        price = data.close[0]
        target_shares = int(target_value / price / 100) * 100
        current = self._get_shares(data)
        need = target_shares - current
        if need < 100: return False  # min_shares from config
        # 资金约束
        cost = need * price * (1 + SLIPPAGE_PCT) + FEE
        if cost > self.broker.getcash():
            need = int(self.broker.getcash() / price / 100) * 100
            if need < 100: return False  # min_shares from config
        self._order_pending[name] = self.buy(data=data, size=need)
        self.ps[name]['_last_entry_reason'] = reason
        return True

    def _sell(self, data, pct, reason):
        """按仓位比例卖出"""
        name = data._name
        current = self._get_shares(data)
        if current < 100: return False  # 无持仓可卖
        shares = int(current * pct / 100 / 100) * 100
        if shares < 100: return False
        # 防止卖出超过持仓
        if shares > current:
            shares = (current // 100) * 100
            if shares < 100: return False
        self._order_pending[name] = self.sell(data=data, size=shares)
        self.ps[name]['_last_exit_reason'] = reason
        return True

    def _close(self, data, reason):
        """清仓"""
        name = data._name
        if self._get_shares(data) < 100: return False
        self._order_pending[name] = self.close(data=data)
        self.ps[name]['_last_exit_reason'] = reason
        return True

    # ─── 共享辅助方法（消除集中/轮动/金字塔模式间的重复代码）───

    def _calc_momentum_scores(self, lb):
        """计算lookback日动量排名 → (momentum_scores, ranked)"""
        momentum_scores = {}
        for d in self.datas:
            name = d._name
            if d.volume[0] < 0:
                momentum_scores[name] = -999
            elif len(d.close) >= lb + 1:
                if self.p.momentum_mode == "simple":
                    momentum_scores[name] = (d.close[0] - d.close[-lb]) / d.close[-lb] * 100
                else:  # c2 (default)
                    ret_lb = (d.close[0] - d.close[-lb]) / d.close[-lb] * 100
                    if lb == 20:
                        ma_lb_now = self.ma20[name][0]
                        ma_lb_ago = self.ma20[name][-lb] if len(self.ma20[name]) >= lb + 1 else ma_lb_now
                    else:
                        if len(d.close) >= lb * 2:
                            ma_lb_now = sum(d.close.get(size=lb)) / lb
                            ma_lb_ago = sum(d.close.get(size=lb, ago=lb)) / lb
                        else:
                            ma_lb_now = ma_lb_ago = d.close[0]
                    ma_slope = (ma_lb_now - ma_lb_ago) / ma_lb_ago * 100 if ma_lb_ago and ma_lb_ago > 0 else 0
                    atr_val = self.atr[name].atr[0]
                    price = d.close[0]
                    atr_norm = (atr_val / price * 100) if atr_val and price > 0 else 0
                    momentum_scores[name] = ret_lb * 0.4 + ma_slope * 0.4 + atr_norm * 0.2
            else:
                momentum_scores[name] = -999
        ranked = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)
        return momentum_scores, ranked

    def _calc_top_sets(self, ranked, top_n):
        """板块分散+TOP集合计算 → (top_set, top_drop)"""
        if self.p.sector_diversify:
            top_set = set(); seen = set()
            for name, _ in ranked:
                sector = self.SECTOR_MAP.get(name, name)
                if sector not in seen: seen.add(sector); top_set.add(name)
                if len(top_set) >= top_n: break
            top_drop = set(); seen2 = set()
            for name, _ in ranked:
                sector = self.SECTOR_MAP.get(name, name)
                if sector not in seen2: seen2.add(sector); top_drop.add(name)
                if len(top_drop) >= top_n * 2: break
        else:
            top_set = set(name for name, _ in ranked[:top_n] if ranked[0][1] > -999)
            top_drop = set(name for name, _ in ranked[:top_n * 2] if ranked[0][1] > -999)
        return top_set, top_drop

    def _update_confirm_tracker(self, momentum_scores, top_set, top_n):
        """换仓延迟确认跟踪 → (effective_top, effective_top_drop)"""
        if self.p.confirm_days <= 0:
            return top_set, set(name for name, _ in sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)[:top_n * 2])
        if not hasattr(self, '_confirm_tracker'):
            self._confirm_tracker = {}
        for name in momentum_scores:
            if name not in self._confirm_tracker:
                self._confirm_tracker[name] = {'in_top': 0, 'out_top': 0}
            if name in top_set:
                self._confirm_tracker[name]['in_top'] += 1
                self._confirm_tracker[name]['out_top'] = 0
            else:
                self._confirm_tracker[name]['out_top'] += 1
                self._confirm_tracker[name]['in_top'] = 0
        effective_top = set(name for name in momentum_scores if self._confirm_tracker[name]['in_top'] >= self.p.confirm_days)
        effective_top_drop = set(name for name in momentum_scores if self._confirm_tracker[name]['out_top'] < self.p.confirm_days)
        return effective_top, effective_top_drop

    def _calc_trail_params(self, price, avg, atr_pct):
        """移动止盈参数计算 → (trail_pct, trail_mult, trail_label, trail_activation, trail_lockin)"""
        trail_pct = self.p.trail_pct
        if self.p.trail_mode == "e1":
            trail_activation = CONF["profit_take"]["e1_activation"]
            trail_lockin = CONF["profit_take"]["e1_lockin"]
        elif self.p.trail_mode == "e2":
            trail_activation = CONF["profit_take"]["e2_activation"]
            trail_lockin = CONF["profit_take"]["e2_lockin"]
        else:
            trail_activation = trail_pct + 0.01
            trail_lockin = trail_pct + 0.08

        if self.p.trail_mode == "fixed":
            trail_mult = 1 - trail_pct
            trail_label = f"{trail_pct*100:.0f}%"
        elif self.p.trail_mode == "e1":
            if atr_pct > 5: trail_mult = 0.90; trail_label = "10%"
            elif atr_pct >= 3: trail_mult = 0.93; trail_label = "7%"
            else: trail_mult = 0.95; trail_label = "5%"
        else:  # e2
            if atr_pct > 5: trail_mult = 0.88; trail_label = "12%"
            elif atr_pct >= 3: trail_mult = 0.92; trail_label = "8%"
            else: trail_mult = 0.95; trail_label = "5%"
        return trail_pct, trail_mult, trail_label, trail_activation, trail_lockin

    def _update_trail_state(self, ps, price, avg, atr_pct):
        """更新移动止盈状态，返回 (should_close, reason)"""
        pp = (price - avg) / avg if avg > 0 else 0
        trail_pct, trail_mult, trail_label, trail_activation, trail_lockin = self._calc_trail_params(price, avg, atr_pct)
        if pp >= trail_activation and not ps["reached_activation"]:
            ps["reached_activation"] = True
        if pp >= trail_lockin and not ps["reached_lockin"]:
            ps["reached_lockin"] = True
            ps["trail"] = avg * 1.05
        if ps["reached_lockin"] and ps["peak_price"] > 0:
            ps["trail"] = ps["peak_price"] * trail_mult
        if ps["trail"] > 0 and price <= ps["trail"]:
            return True, f"移动止盈{trail_label} ATR={atr_pct:.1f}% trail={ps['trail']:.3f}"
        return False, ""

    def _reset_daily(self, date_str):
        """每日重置: bought_today + empty_days"""
        for ps in self.ps.values():
            ps["bought_today"] = False
        for name, ps in self.ps.items():
            if not any(self._has_position(d) for d in self.datas if d._name == name):
                if ps["build_phase"] == 0:
                    ps["empty_days"] += 1

    def _update_prev_macd(self):
        """更新prev_macd_status + 记录每日净值"""
        for name, ps in self.ps.items():
            ms = MACDStatus.STATUS_MAP.get(self.macd[name].status[0], "震荡")
            ps["prev_macd_status"] = ms
        date_str = self.data.datetime.date(0).strftime("%Y-%m-%d")
        total = self.broker.getvalue()
        self.daily_values.append((date_str, total, self.broker.getcash()))

    def _is_in_cooldown(self, ps, date_str):
        if not ps["cooldown_until"]: return False
        return date_str <= ps["cooldown_until"]

    def _init_on_entry(self, ps, price):
        ps["base"] = price; ps["first_price"] = price
        ps["build_phase"] = 1; ps["bought_today"] = True
        ps["add_count"] = 0; ps["empty_days"] = 0; ps["stop_cooldown"] = False
        ps["entry_avg_cost"] = price  # B1: 锁定建仓均价供止损判断
        ps["pyramid_count"] = 0  # 金字塔加仓次数

    def _full_liquidate_state(self, ps, date_str):
        """清仓后重置状态"""
        ps["base"] = 0; ps["first_price"] = 0; ps["build_phase"] = 0
        ps["stop_level"] = 0; ps["below_ma20"] = 0; ps["below_ma20_date"] = ""
        ps["reached_activation"] = False; ps["reached_lockin"] = False; ps["trail"] = 0
        ps["add_count"] = 0; ps["peak_price"] = 0; ps["ma5_touch_count"] = 0; ps["stop_cooldown"] = False
        ps["trend_sell_today"] = 0; ps["trend_sell_cooling_until"] = ""
        ps["ma5_sell_today"] = 0; ps["ma5_sell_cooling_until"] = ""
        ps["breakeven_activated"] = False
        ps["entry_avg_cost"] = 0.0
        ps["confirm_batch_count"] = 0; ps["confirm_batch_date"] = ""
        ps["pyramid_count"] = 0  # 重置金字塔加仓次数
        ps["market_reduced"] = False  # 重置大盘自适应减仓标记
        ps["market_drawdown_level"] = 0  # 重置大盘回撤减仓等级
        ps["market_drawdown_date"] = ""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            ps["cooldown_until"] = (dt + timedelta(days=self.p.cooldown_days)).strftime("%Y-%m-%d")
        except: ps["cooldown_until"] = ""

    def _is_monday(self, date_str):
        """判断是否为周一（0=Monday）"""
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").weekday() == 0
        except:
            return False

    def next(self):
        date_str = self.data.datetime.date(0).strftime("%Y-%m-%d")

        # 交易日计数: 每天递增，用于重平衡周期判断
        if date_str != self._last_date:
            self._last_date = date_str
            self._day_count += 1

        # ═══ 集中TOP N+50%建仓+趋势加码模式 ═══
        if self.p.concentrated_pyramid_mode:
            lb = self.p.lookback
            momentum_scores, ranked = self._calc_momentum_scores(lb)
            top_n = self.p.top_n
            top_set = set(name for name, _ in ranked[:top_n])
            top_drop = set(name for name, _ in ranked[:top_n * 2])
            self._reset_daily(date_str)

            for d in self.datas:
                name = d._name
                ps = self.ps[name]
                price = d.close[0]

                if d.volume[0] < 0:
                    continue
                if name in self._order_pending:
                    continue

                rsi_val = self.rsi[name].rsi[0]
                ms = MACDStatus.STATUS_MAP.get(self.macd[name].status[0], "震荡")
                dif_val = self.macd[name].dif[0]
                ma5_v = self.ma5[name][0]
                ma10_v = self.ma10[name][0]
                ma20_v = self.ma20[name][0]
                atr_val = self.atr[name].atr[0]

                has_pos = self._has_position(d)
                shares = self._get_shares(d)
                avg = self._get_avg_cost(d)

                if has_pos:
                    # 跌出TOP_N*2则清仓
                    if name not in top_drop:
                        self._close(d, f"集中金字塔清仓: 跌出TOP{top_n*2} 排名{momentum_scores.get(name, -999):.1f}%")
                        self._full_liquidate_state(ps, date_str)
                        continue

                    if price > ps["peak_price"]:
                        ps["peak_price"] = price

                    # 均价止损: 用锁定建仓均价
                    entry_cost = ps.get("entry_avg_cost", 0) or avg
                    if entry_cost > 0:
                        pos_ratio = shares * price / TOTAL_FUND if price > 0 else 0
                        _sl = CONF.get("stop_loss", {})
                        if pos_ratio > _sl.get("tier1_ratio", 0.80):
                            stop_pct = _sl.get("tier1_pct", 0.15)
                        elif pos_ratio >= _sl.get("tier2_ratio", 0.50):
                            stop_pct = _sl.get("tier2_pct", 0.20)
                        else:
                            stop_pct = _sl.get("tier3_pct", 0.25)
                        # P1-1: 浮盈止损上移 — 从config读取阈值
                        float_profit_pct = (price - entry_cost) / entry_cost if entry_cost > 0 else 0
                        if float_profit_pct > _sl.get("float_profit_trigger2", 0.10):
                            adjusted_stop = entry_cost * (1 + _sl.get("float_profit_stop2", 0.05))
                        elif float_profit_pct > _sl.get("float_profit_trigger1", 0.05):
                            adjusted_stop = entry_cost * (1 + _sl.get("float_profit_stop1", 0.0))
                        else:
                            adjusted_stop = entry_cost * (1 - stop_pct)
                        if price <= adjusted_stop:
                            if float_profit_pct > 0.05:
                                self._close(d, f"集中金字塔浮盈止损上移 浮盈{float_profit_pct*100:.1f}% entry={entry_cost:.3f} stop={adjusted_stop:.3f}")
                            else:
                                self._close(d, f"集中金字塔均价止损{stop_pct*100:.0f}% entry={entry_cost:.3f}")
                            self._full_liquidate_state(ps, date_str)
                            continue

                    # 硬止损25%
                    if ps["peak_price"] > 0 and price < ps["peak_price"] * (1 - CONF.get("stop_loss", {}).get("hard_stop_pct", 0.25)):
                        self._close(d, f"硬止损{CONF.get('stop_loss', {}).get('hard_stop_pct', 0.25)*100:.0f}% peak={ps['peak_price']:.3f}")
                        self._full_liquidate_state(ps, date_str)
                        continue

                    # 移动止盈（从config读取trail_pct）
                    atr_pct_cp = (atr_val / price * 100) if atr_val and price > 0 else 0
                    should_close, trail_reason = self._update_trail_state(ps, price, avg, atr_pct_cp)
                    if should_close:
                        self._close(d, trail_reason)
                        self._full_liquidate_state(ps, date_str)
                        continue

                    # ═══ 金字塔加码逻辑 ═══
                    if "pyramid_count" not in ps:
                        ps["pyramid_count"] = 0
                    

                    if entry_cost > 0 and price > entry_cost:
                        float_profit = (price - entry_cost) / entry_cost
                        current_value = shares * price
                        max_value = TOTAL_FUND * POSITION_CAP  # 单只ETF仓位上限50%

                        if ps["pyramid_count"] == 0 and float_profit > 0.05:
                            # 加仓金额 = 首次建仓金额 × 0.5
                            # 首次建仓金额 = fund_per_etf * 0.50, 所以加仓 pct = 0.25
                            add_pct = 0.25
                            add_value = self.p.fund_per_etf * add_pct
                            if current_value + add_value <= max_value:
                                if self._buy(d, add_pct, f"集中金字塔加仓1 浮盈{float_profit*100:.1f}%"):
                                    ps["pyramid_count"] = 1
                                    ps["bought_today"] = True
                        elif ps["pyramid_count"] == 1 and float_profit > 0.10:
                            add_pct = 0.25
                            add_value = self.p.fund_per_etf * add_pct
                            if current_value + add_value <= max_value:
                                if self._buy(d, add_pct, f"集中金字塔加仓2 浮盈{float_profit*100:.1f}%"):
                                    ps["pyramid_count"] = 2
                                    ps["bought_today"] = True
                else:
                    # 只对TOP N ETF允许建仓
                    if name not in top_set:
                        continue
                    if ps["build_phase"] > 0:
                        continue
                    if ps["bought_today"] or ps["stop_cooldown"] or self._is_in_cooldown(ps, date_str):
                        continue

                    # P2-2: 防御盾独立判断 — 只屏蔽同板块，非全市场暂停
                    defense_block = False
                    if DEFENSE_CODE and name != DEFENSE_CODE:
                        for dd in self.datas:
                            if dd._name == DEFENSE_CODE:
                                dd_price = dd.close[0]
                                dd_ma20 = self.ma20[DEFENSE_CODE][0] if DEFENSE_CODE in self.ma20 else 0
                                dd_dif = self.macd[DEFENSE_CODE].dif[0] if DEFENSE_CODE in self.macd else 0
                                dd_status = MACDStatus.STATUS_MAP.get(self.macd[DEFENSE_CODE].status[0], "震荡") if DEFENSE_CODE in self.macd else "震荡"
                                defense_weak = (dd_ma20 and dd_price < dd_ma20 and dd_dif is not None and dd_dif < 0 and dd_status in ["死叉", "绿柱放大"])
                                if defense_weak:
                                    defense_sector = self.SECTOR_MAP.get(DEFENSE_CODE, "")
                                    name_sector = self.SECTOR_MAP.get(name, "")
                                    if defense_sector and name_sector == defense_sector:
                                        defense_block = True  # 同板块，跳过建仓
                                    # 不同板块，允许建仓
                                break
                    if defense_block:
                        continue

                    atr_ok = (atr_val is not None and price > 0 and atr_val / price <= 0.50)
                    if not atr_ok:
                        continue

                    # 多头排列
                    if not (ma5_v and ma20_v and ma5_v > ma20_v):
                        continue

                    # 10日新高
                    h10 = None
                    if len(d.close) >= 11:
                        h10 = max(d.close.get(size=10, ago=1))

                    entered = False

                    # 通道1: RSI抄底 → 50%建仓
                    if not entered and rsi_val is not None and rsi_val <= self.p.rsi_entry_max and ms == "金叉":
                        if self._buy(d, 0.50, f"集中金字塔RSI抄底50% RSI={rsi_val:.1f}"):
                            self._init_on_entry(ps, price)
                            entered = True

                    # 通道2: 趋势跟踪 → 50%建仓
                    if not entered and ps["empty_days"] > 5 and ms in ("红柱放大", "红柱缩短"):
                        if self._buy(d, 0.50, f"集中金字塔趋势跟踪50% 空仓{ps['empty_days']}d"):
                            self._init_on_entry(ps, price)
                            entered = True

                    # 通道3: 突破入场 → 50%建仓
                    if not entered and h10 is not None and price > h10 and ms == "金叉":
                        if self._buy(d, 0.50, f"集中金字塔突破入场50% 10日高={h10:.3f}"):
                            self._init_on_entry(ps, price)
                            entered = True

                    # 通道4: 分批建仓 → 50%建仓
                    if not entered and ms in ("金叉", "红柱放大") and rsi_val is not None and rsi_val > 40 and price > ma5_v:
                        if self._buy(d, 0.50, f"集中金字塔分批建仓50% MACD{ms}"):
                            self._init_on_entry(ps, price)
                            entered = True

            # 更新prev_macd_status
            self._update_prev_macd()
            return

        # ═══ 每周轮动模式 (配合 --concentrated --weekly-rotation) ═══
        # 已修复: 所有CLI参数(--init-pct, --trail-pct, --momentum, --lookback, --top-n,
        #   --rebalance, --ma60-filter, --dynamic-pct, --sector-diversify, --confirm-days,
        #   --trend-entry, --rsi-entry-max, --trail, --stop-loss) 均生效
        if self.p.weekly_rotation_mode:
            self._weekly_rotation_day += 1
            is_rotation_day = (self._weekly_rotation_day % self.p.rotation_interval == 1)

            # ── 动量评分 ──
            lb = self.p.lookback
            momentum_scores, ranked = self._calc_momentum_scores(lb)
            top_n = self.p.top_n

            # ── 板块分散 + 换仓延迟确认 ──
            top_set, top_drop = self._calc_top_sets(ranked, top_n)
            effective_top, effective_top_drop = self._update_confirm_tracker(momentum_scores, top_set, top_n)
            self._reset_daily(date_str)

            # ── 每日风控: 止损/止盈 (不受轮动日限制) ──
            for d in self.datas:
                name = d._name
                ps = self.ps[name]
                price = d.close[0]
                if d.volume[0] < 0:
                    continue
                if name in self._order_pending:
                    continue
                if not self._has_position(d):
                    continue

                has_pos = self._has_position(d)
                shares = self._get_shares(d)
                avg = self._get_avg_cost(d)
                atr_val = self.atr[name].atr[0]

                # MA60趋势过滤: 价格<MA60则清仓 (与集中模式一致)
                if self.p.ma60_filter:
                    ma60_v = self.ma60[name][0]
                    if ma60_v and price < ma60_v:
                        self._close(d, f"每周轮动MA60过滤清仓 price={price:.3f}<MA60={ma60_v:.3f}")
                        self._full_liquidate_state(ps, date_str)
                        continue

                if price > ps["peak_price"]:
                    ps["peak_price"] = price

                # P2修复: 均价止损+硬止损25%已移除, 统一由risk_manager.check_stop_loss处理
                # (原L883-913的均价分级止损+硬止损25%与risk_manager重复)

                # ── 移动止盈 ──
                atr_pct_wr = (atr_val / price * 100) if atr_val and price > 0 else 0
                should_close, trail_reason = self._update_trail_state(ps, price, avg, atr_pct_wr)
                if should_close:
                    self._close(d, trail_reason)
                    self._full_liquidate_state(ps, date_str)
                    continue

                # ── 调用实盘 risk_manager.check_stop_loss ──
                pos, t = self._build_pos_and_tech(d, name, date_str)
                stop_actions = check_stop_loss(pos, t, price, date_str)
                self._sync_pos_to_ps(name, pos, date_str)
                liquidated = self._execute_stop_actions(d, name, stop_actions, date_str)
                if liquidated:
                    continue

            # ── 轮动日: 基于排名换仓+建仓 ──
            if is_rotation_day:
                self._weekly_top3 = top_set
                # 退出: 跌出TOP的持仓不再清仓，对齐实盘(只重置空仓状态)
                for d in self.datas:
                    name = d._name
                    ps = self.ps[name]
                    if d.volume[0] < 0:
                        continue
                    if name in self._order_pending:
                        continue
                    if not self._has_position(d) and name not in top_set:
                        # 已空仓+跌出TOP → 完全退出，重置状态
                        self._full_liquidate_state(ps, date_str)

                # ── 趋势加仓: 已持仓+仍在TOP+浮盈>3%+MA5>MA20 → 追加init_pct仓位 ──
                if self.p.trend_entry:
                    for d in self.datas:
                        name = d._name
                        ps = self.ps[name]
                        price = d.close[0]
                        if d.volume[0] < 0 or name in self._order_pending:
                            continue
                        if not self._has_position(d):
                            continue
                        if name not in effective_top:
                            continue
                        avg = self._get_avg_cost(d)
                        if avg <= 0:
                            continue
                        float_p = (price - avg) / avg
                        # 浮盈3%-20%时加仓 (超过20%不加, 避免追高)
                        if float_p < 0.03 or float_p > 0.20:
                            continue
                        ma5_v = self.ma5[name][0]
                        ma20_v = self.ma20[name][0]
                        if not (ma5_v and ma20_v and ma5_v > ma20_v):
                            continue
                        # 仓位上限: 不超过fund_per_etf的120%
                        shares = self._get_shares(d)
                        pos_value = shares * price
                        max_value = self.p.fund_per_etf * 1.2
                        if pos_value >= max_value:
                            continue
                        add_pct = self.p.init_pct * 0.5  # 加仓半仓
                        if self._buy(d, add_pct, f"每周轮动趋势加仓{add_pct*100:.0f}% 浮盈{float_p*100:.1f}%"):
                            pass  # 加仓不需要重置状态

                # 进入: effective_top中尚未持仓的标的 — 参数化建仓通道 (与集中模式一致)
                for d in self.datas:
                    name = d._name
                    ps = self.ps[name]
                    price = d.close[0]
                    if d.volume[0] < 0:
                        continue
                    if name in self._order_pending:
                        continue
                    if name not in effective_top:
                        continue
                    if self._has_position(d):
                        continue
                    if ps["build_phase"] > 0:
                        continue
                    if ps["bought_today"] or ps["stop_cooldown"] or self._is_in_cooldown(ps, date_str):
                        continue

                    # 换仓延迟确认
                    if self.p.confirm_days > 0 and self._confirm_tracker.get(name, {}).get('in_top', 0) < self.p.confirm_days:
                        continue

                    # MA60趋势过滤
                    if self.p.ma60_filter:
                        ma60_v = self.ma60[name][0]
                        if ma60_v and price < ma60_v:
                            continue

                    rsi_val = self.rsi[name].rsi[0]
                    ms = MACDStatus.STATUS_MAP.get(self.macd[name].status[0], "震荡")
                    ma5_v = self.ma5[name][0]
                    ma10_v = self.ma10[name][0]
                    ma20_v = self.ma20[name][0]
                    atr_val = self.atr[name].atr[0]

                    atr_ok = (atr_val is not None and price > 0 and atr_val / price <= 0.50)
                    if not atr_ok:
                        continue

                    # 多头排列
                    if not (ma5_v and ma20_v and ma5_v > ma20_v):
                        continue

                    # 10日新高
                    h10 = None
                    if len(d.close) >= 11:
                        h10 = max(d.close.get(size=10, ago=1))

                    # 动态仓位
                    if self.p.dynamic_pct:
                        mom_score = momentum_scores.get(name, 0)
                        if mom_score > 15:
                            pct = self.p.init_pct * 1.2
                        elif mom_score >= 8:
                            pct = self.p.init_pct
                        else:
                            pct = self.p.init_pct * 0.6
                    else:
                        pct = self.p.init_pct

                    entered = False

                    # 方案A: 趋势建仓通道 (--trend-entry)  P1修复: 加RSI<65过滤防追高
                    if not entered and self.p.trend_entry:
                        if rsi_val is not None and rsi_val < 65:
                            if self._buy(d, 0.50, f"每周轮动趋势建仓50% MA多头排列 RSI={rsi_val:.1f}"):
                                self._init_on_entry(ps, price)
                                entered = True

                    # 通道1: RSI抄底
                    if not entered and rsi_val is not None and rsi_val <= self.p.rsi_entry_max and ms == "金叉":
                        if self._buy(d, pct, f"每周轮动RSI抄底{pct*100:.0f}% RSI={rsi_val:.1f}"):
                            self._init_on_entry(ps, price)
                            entered = True

                    # 通道2: 趋势跟踪
                    if not entered and ps["empty_days"] > 5 and ms in ("红柱放大", "红柱缩短"):
                        if self._buy(d, pct, f"每周轮动趋势跟踪{pct*100:.0f}% 空仓{ps['empty_days']}d"):
                            self._init_on_entry(ps, price)
                            entered = True

                    # 通道3: 趋势入场(合并原突破入场+分批建仓)
                    if not entered and ms in ("金叉", "红柱放大") and rsi_val is not None and rsi_val > 40:
                        reason_extra = f" 10日高={h10:.3f}" if (h10 is not None and price > h10) else ""
                        if self._buy(d, pct, f"每周轮动趋势入场{pct*100:.0f}% MACD{ms}{reason_extra}"):
                            self._init_on_entry(ps, price)
                            entered = True

            # ── 大盘回撤保护: 510300的20日高点回撤替代破MA20减仓 ──
            # 使用沪深300ETF(510300)作为大盘指标
            market_index_code = "510300"
            market_drawdown_pct = 0.0  # 当前回撤百分比
            market_idx_price = 0.0
            market_idx_ma20 = 0.0
            if market_index_code in self.ps:
                for dd in self.datas:
                    if dd._name == market_index_code:
                        if dd.volume[0] > 0 and len(dd.close) > 20:
                            market_idx_price = dd.close[0]
                            market_idx_ma20 = self.ma20[market_index_code][0] if market_index_code in self.ma20 else 0
                            high_20 = max(dd.close[-i] for i in range(20))
                            if high_20 > 0:
                                market_drawdown_pct = (high_20 - market_idx_price) / high_20 * 100
                        break

            # 确定当前回撤等级
            # 等级: 0=无(回撤<5%), 1=>5%, 2=>8%, 3=>12%
            # 渐进减仓: 从等级0到等级1减30%, 从1到2减20%(累计50%), 从2到3减20%(累计70%)
            current_level = 0
            if market_drawdown_pct >= 12:
                current_level = 3
            elif market_drawdown_pct >= 8:
                current_level = 2
            elif market_drawdown_pct >= 5:
                current_level = 1

            # 恢复条件: 回撤<3% 或 价格收复MA20
            market_recovered = (market_drawdown_pct < 3) or (market_idx_ma20 > 0 and market_idx_price > market_idx_ma20)

            if current_level > 0:
                for d in self.datas:
                    name = d._name
                    ps = self.ps[name]
                    if d.volume[0] < 0:
                        continue
                    if name in self._order_pending:
                        continue
                    if not self._has_position(d):
                        continue
                    # 防同日重复触发
                    prev_level = ps.get("market_drawdown_level", 0)
                    prev_date = ps.get("market_drawdown_date", "")
                    if current_level <= prev_level:
                        continue  # 等级未升级，跳过
                    if prev_date == date_str and prev_level > 0:
                        continue  # 同日已触发过，跳过

                    # 计算本次需要额外减仓比例
                    # 目标累计减仓: level1=30%, level2=50%, level3=70%
                    target_reduction = {1: 30, 2: 50, 3: 70}[current_level]
                    already_reduced = {0: 0, 1: 30, 2: 50, 3: 70}[prev_level]
                    extra_reduction = target_reduction - already_reduced

                    if extra_reduction > 0:
                        shares = self._get_shares(d)
                        if shares >= 200:
                            if self._sell(d, extra_reduction, f"大盘回撤保护减仓{extra_reduction}% 回撤={market_drawdown_pct:.1f}%"):
                                ps["market_drawdown_level"] = current_level
                                ps["market_drawdown_date"] = date_str
                                ps["market_reduced"] = True
            elif market_recovered:
                # 恢复满仓: 对已减仓标的加仓回满仓
                for d in self.datas:
                    name = d._name
                    ps = self.ps[name]
                    if d.volume[0] < 0:
                        continue
                    if name in self._order_pending:
                        continue
                    if ps.get("market_drawdown_level", 0) > 0 and self._has_position(d):
                        # 已减仓且回撤恢复, 加仓补回
                        if self._buy(d, self.p.init_pct * 0.7, f"大盘回撤恢复加仓 回撤={market_drawdown_pct:.1f}%"):
                            ps["market_drawdown_level"] = 0
                            ps["market_drawdown_date"] = ""
                            ps["market_reduced"] = False

            self._update_prev_macd()
            return

        # ═══ 集中持仓模式 ═══
        if self.p.concentrated_mode:
            is_rebalance_day = (self._day_count % self.p.rebalance == 1)

            # ── 动量评分 ──
            lb = self.p.lookback
            momentum_scores, ranked = self._calc_momentum_scores(lb)
            top_n = self.p.top_n

            # ── 板块分散 + 换仓延迟确认 ──
            top_set, top_drop = self._calc_top_sets(ranked, top_n)
            effective_top, effective_top_drop = self._update_confirm_tracker(momentum_scores, top_set, top_n)
            self._reset_daily(date_str)

            # ── 每日风控检查: 止损/止盈 (不受rebalance影响) ──
            for d in self.datas:
                name = d._name
                ps = self.ps[name]
                price = d.close[0]

                if d.volume[0] < 0:
                    continue
                if name in self._order_pending:
                    continue
                if not self._has_position(d):
                    continue

                has_pos = self._has_position(d)
                shares = self._get_shares(d)
                avg = self._get_avg_cost(d)
                atr_val = self.atr[name].atr[0]

                # MA60趋势过滤: 价格<MA60则清仓
                if self.p.ma60_filter:
                    ma60_v = self.ma60[name][0]
                    if ma60_v and price < ma60_v:
                        self._close(d, f"MA60过滤清仓 price={price:.3f}<MA60={ma60_v:.3f}")
                        self._full_liquidate_state(ps, date_str)
                        continue

                if price > ps["peak_price"]:
                    ps["peak_price"] = price

                # P2修复: 均价止损+硬止损25%已移除, 统一由risk_manager.check_stop_loss处理
                # (原均价分级止损+浮盈止损上移+硬止损25%与risk_manager重复)

                # ── 移动止盈 ──
                atr_pct_cm = (atr_val / price * 100) if atr_val and price > 0 else 0
                should_close, trail_reason = self._update_trail_state(ps, price, avg, atr_pct_cm)
                if should_close:
                    self._close(d, trail_reason)
                    self._full_liquidate_state(ps, date_str)
                    continue

            # ── 重平衡日: 基于排名换仓 (方向A: --rebalance / 方向E: --confirm-days) ──
            if is_rebalance_day:
                for d in self.datas:
                    name = d._name
                    ps = self.ps[name]
                    price = d.close[0]

                    if d.volume[0] < 0:
                        continue
                    if name in self._order_pending:
                        continue

                    has_pos = self._has_position(d)
                    shares = self._get_shares(d)
                    avg = self._get_avg_cost(d)

                    rsi_val = self.rsi[name].rsi[0]
                    ms = MACDStatus.STATUS_MAP.get(self.macd[name].status[0], "震荡")
                    ma5_v = self.ma5[name][0]
                    ma10_v = self.ma10[name][0]
                    ma20_v = self.ma20[name][0]
                    atr_val = self.atr[name].atr[0]

                    if has_pos:
                        # 跌出effective_top_drop则清仓
                        if name not in effective_top_drop:
                            self._close(d, f"集中持仓清仓: 跌出TOP{top_n*2} 排名{momentum_scores.get(name, -999):.1f}%")
                            self._full_liquidate_state(ps, date_str)
                            continue
                    else:
                        # 只对effective_top ETF允许建仓
                        if name not in effective_top:
                            continue
                        # 换仓延迟确认: 连续N天在TOP才建仓
                        if self.p.confirm_days > 0 and self._confirm_tracker.get(name, {}).get('in_top', 0) < self.p.confirm_days:
                            continue
                        if ps["build_phase"] > 0:
                            continue
                        if ps["bought_today"] or ps["stop_cooldown"] or self._is_in_cooldown(ps, date_str):
                            continue

                        # MA60趋势过滤
                        if self.p.ma60_filter:
                            ma60_v = self.ma60[name][0]
                            if ma60_v and price < ma60_v:
                                continue

                        atr_ok = (atr_val is not None and price > 0 and atr_val / price <= 0.50)
                        if not atr_ok:
                            continue

                        # 多头排列
                        if not (ma5_v and ma20_v and ma5_v > ma20_v):
                            continue

                        # 10日新高
                        h10 = None
                        if len(d.close) >= 11:
                            h10 = max(d.close.get(size=10, ago=1))

                        # 动态仓位
                        if self.p.dynamic_pct:
                            mom_score = momentum_scores.get(name, 0)
                            if mom_score > 15:
                                pct = self.p.init_pct * 1.2
                            elif mom_score >= 8:
                                pct = self.p.init_pct
                            else:
                                pct = self.p.init_pct * 0.6
                        else:
                            pct = self.p.init_pct

                        entered = False

                        # 方案A: 趋势建仓通道 (--trend-entry)  P1修复: 加RSI<65过滤防追高
                        # TOP3 C2动量 + 多头排列 + 空仓 + RSI<65 → 直接50%建仓
                        if not entered and self.p.trend_entry:
                            if rsi_val is not None and rsi_val < 65:
                                if self._buy(d, 0.50, f"集中趋势建仓50% MA多头排列 RSI={rsi_val:.1f}"):
                                    self._init_on_entry(ps, price)
                                    entered = True

                        # 通道1: RSI抄底
                        if not entered and rsi_val is not None and rsi_val <= self.p.rsi_entry_max and ms == "金叉":
                            if self._buy(d, pct, f"集中RSI抄底{pct*100:.0f}% RSI={rsi_val:.1f}"):
                                self._init_on_entry(ps, price)
                                entered = True

                        # 通道2: 趋势跟踪
                        if not entered and ps["empty_days"] > 5 and ms in ("红柱放大", "红柱缩短"):
                            if self._buy(d, pct, f"集中趋势跟踪{pct*100:.0f}% 空仓{ps['empty_days']}d"):
                                self._init_on_entry(ps, price)
                                entered = True

                        # 通道3: 突破入场
                        if not entered and h10 is not None and price > h10 and ms == "金叉":
                            if self._buy(d, pct, f"集中突破入场{pct*100:.0f}% 10日高={h10:.3f}"):
                                self._init_on_entry(ps, price)
                                entered = True

                        # 通道4: 分批建仓
                        if not entered and ms in ("金叉", "红柱放大") and rsi_val is not None and rsi_val > 40 and price > ma5_v:
                            if self._buy(d, pct, f"集中分批建仓{pct*100:.0f}% MACD{ms}"):
                                self._init_on_entry(ps, price)
                                entered = True

            self._update_prev_macd()
            return

        # ═══ 全仓轮动模式 ═══
        if self.p.rotation_mode:
            # 计算20日动量排名
            momentum_scores = {}
            for d in self.datas:
                name = d._name
                if d.volume[0] < 0:
                    momentum_scores[name] = -999
                elif len(d.close) >= 21:
                    momentum_scores[name] = (d.close[0] - d.close[-20]) / d.close[-20] * 100
                else:
                    momentum_scores[name] = -999
            top_etf = max(momentum_scores, key=momentum_scores.get)
            top_momentum = momentum_scores[top_etf]

            # 找到当前持仓ETF
            held_etf = None
            held_data = None
            for d in self.datas:
                if self._has_position(d):
                    held_etf = d._name
                    held_data = d
                    break

            # 止损检查: 从买入价回撤10%
            if held_etf and self._rotation_entry_price > 0:
                held_price = held_data.close[0]
                if held_price <= self._rotation_entry_price * 0.90:
                    self._close(held_data, f"轮动止损-10% entry={self._rotation_entry_price:.3f}")
                    held_etf = None
                    self._rotation_etf = None
                    self._rotation_entry_price = 0.0

            # 每周一检查轮动
            if self._is_monday(date_str):
                if held_etf and held_etf != top_etf:
                    # 轮动: 清仓旧的，买入新的
                    self._close(held_data, f"轮动换仓 {held_etf}→{top_etf} 动量{top_momentum:.1f}%")
                    held_etf = None
                    self._rotation_etf = None
                    self._rotation_entry_price = 0.0

                if not held_etf and top_momentum > -999:
                    # 买入动量第1名
                    for d in self.datas:
                        if d._name == top_etf:
                            price = d.close[0]
                            # 100%资金买入
                            target_value = TOTAL_FUND * 0.98  # 留2%给手续费
                            target_shares = int(target_value / price / 100) * 100
                            if target_shares >= 100:
                                cost = target_shares * price * (1 + SLIPPAGE_PCT) + FEE
                                if cost <= self.broker.getcash():
                                    self._order_pending[top_etf] = self.buy(data=d, size=target_shares)
                                    self.ps[top_etf]['_last_entry_reason'] = f"轮动买入 动量{top_momentum:.1f}%"
                                    self._rotation_etf = top_etf
                                    self._rotation_entry_price = price

            # 记录每日净值
            total = self.broker.getvalue()
            self.daily_values.append((date_str, total, self.broker.getcash()))
            return  # 轮动模式不执行其他逻辑

        # 每日计数器重置: 趋势止盈的当日计数
        for ps in self.ps.values():
            ps["trend_sell_today"] = 0
            ps["ma5_sell_today"] = 0

        self._reset_daily(date_str)

        for d in self.datas:
            name = d._name
            ps = self.ps[name]
            price = d.close[0]

            # 跳过未上市ETF(volume=-1标记为未上市, 区别于节假日volume=0)
            if d.volume[0] < 0:
                continue

            # 有挂单时跳过(避免重复下单)
            if name in self._order_pending:
                continue

            # 指标
            rsi_val = self.rsi[name].rsi[0]
            macd_status = self.macd[name].status[0]
            dif_val = self.macd[name].dif[0]
            ms = MACDStatus.STATUS_MAP.get(macd_status, "震荡")
            ma5_v = self.ma5[name][0]
            ma10_v = self.ma10[name][0]
            ma20_v = self.ma20[name][0]
            ao_val = self.ao[name].ao[0]
            atr_val = self.atr[name].atr[0]

            # 10日新高
            h10 = None
            if len(d.close) >= 11:
                h10 = max(d.close.get(size=10, ago=1))

            # AO 5日前
            ao_5ago = None
            if len(self.ao[name].ao) >= 6:
                ao_5ago = self.ao[name].ao[-5]

            has_pos = self._has_position(d)
            shares = self._get_shares(d)
            avg = self._get_avg_cost(d)

            # ═══ EXIT LOGIC ═══
            if has_pos:
                if price > ps["peak_price"]: ps["peak_price"] = price

                # ── 极限方案C: 取消保本止盈 ──
                # (保本止盈已移除，保留注释以标记)

                # 更新移动止盈线 (backtrader用avg而非entry_cost计算盈利比例)
                trail_pct_val = self.p.trail_pct
                # P0修复: e1模式激活线5%/锁仓线12%, e2模式10%/18%
                if self.p.trail_mode == "e1":
                    trail_activation = CONF["profit_take"]["e1_activation"]
                    trail_lockin = CONF["profit_take"]["e1_lockin"]
                elif self.p.trail_mode == "e2":
                    trail_activation = CONF["profit_take"]["e2_activation"]
                    trail_lockin = CONF["profit_take"]["e2_lockin"]
                else:
                    trail_activation = trail_pct_val + 0.01
                    trail_lockin = trail_pct_val + 0.08
                pp = (price - avg) / avg if avg > 0 else 0
                if pp >= trail_activation - 0.0001 and not ps["reached_activation"]:
                    ps["reached_activation"] = True
                if pp >= trail_lockin - 0.0001 and not ps["reached_lockin"]:
                    ps["reached_lockin"] = True; ps["trail"] = avg * 1.05
                if ps["reached_lockin"] and ps["peak_price"] > 0:
                    ps["trail"] = ps["peak_price"] * (1 - trail_pct_val)

                # ── 调用实盘 risk_manager.check_stop_loss ──
                pos, t = self._build_pos_and_tech(d, name, date_str)
                stop_actions = check_stop_loss(pos, t, price, date_str)
                self._sync_pos_to_ps(name, pos, date_str)
                liquidated = self._execute_stop_actions(d, name, stop_actions, date_str)
                if liquidated:
                    continue
                if name in self._order_pending:
                    continue

                # ═══ 趋势加码逻辑 (金字塔加仓) ═══
                if self.p.pyramid_mode:
                    entry_cost = ps.get("entry_avg_cost", 0) or avg
                    if entry_cost > 0 and price > entry_cost:
                        float_profit = (price - entry_cost) / entry_cost
                        if "pyramid_count" not in ps:
                            ps["pyramid_count"] = 0
                        current_value = shares * price
                        max_value = TOTAL_FUND * POSITION_CAP
                        if ps["pyramid_count"] == 0 and float_profit > 0.05:
                            add_value = self.p.fund_per_etf * 0.30
                            if current_value + add_value <= max_value:
                                if self._buy(d, 0.30, f"金字塔加仓1 浮盈{float_profit*100:.1f}%"):
                                    ps["pyramid_count"] = 1
                                    ps["bought_today"] = True
                        elif ps["pyramid_count"] == 1 and float_profit > 0.10:
                            add_value = self.p.fund_per_etf * 0.30
                            if current_value + add_value <= max_value:
                                if self._buy(d, 0.30, f"金字塔加仓2 浮盈{float_profit*100:.1f}%"):
                                    ps["pyramid_count"] = 2
                                    ps["bought_today"] = True

            # ═══ ENTRY LOGIC ═══
            # ATR>50%跳过(除权日, 与实盘一致)
            atr_ok = (atr_val is not None and price > 0 and atr_val / price <= 0.50)

            # 防御盾过滤: 只拦同板块，非全市场暂停
            defense_weak = False
            if DEFENSE_CODE:
                for dd in self.datas:
                    if dd._name == DEFENSE_CODE:
                        dd_price = dd.close[0]
                        all_tech_d = {DEFENSE_CODE: {
                            "ma20": self.ma20[DEFENSE_CODE][0],
                            "dif": self.macd[DEFENSE_CODE].dif[0],
                            "macd_status": MACDStatus.STATUS_MAP.get(self.macd[DEFENSE_CODE].status[0], "震荡"),
                        }}
                        realtime_d = {DEFENSE_CODE: {"price": dd_price}}
                        defense_weak = check_defense(DEFENSE_CODE, all_tech_d, realtime_d)
                        if defense_weak:
                            defense_sector = self.SECTOR_MAP.get(DEFENSE_CODE, "")
                            name_sector = self.SECTOR_MAP.get(name, "")
                            if defense_sector and name_sector == defense_sector:
                                defense_weak = True  # 同板块，跳过建仓
                            else:
                                defense_weak = False  # 不同板块，允许建仓
                        break
            if defense_weak:
                continue  # 防御标的弱势且同板块，暂停建仓

            if not has_pos and ps["build_phase"] == 0 and not ps["bought_today"] and not ps["stop_cooldown"] and not self._is_in_cooldown(ps, date_str) and atr_ok:
                # ── 调用实盘策略模块 evaluate_entry ──
                # 构建 realtime / positions / all_klines
                _realtime = {dd._name: {"price": dd.close[0]} for dd in self.datas}
                _positions = {}
                for dd in self.datas:
                    nn = dd._name
                    _pp, _ = self._build_pos_and_tech(dd, nn, date_str)
                    _positions[nn] = _pp
                _all_klines = {}
                for dd in self.datas:
                    nn = dd._name
                    n_bars = min(21, len(dd.close))
                    closes = [float(dd.close[-(n_bars - i)]) for i in range(n_bars)]
                    _all_klines[nn] = [{"close": c} for c in closes]

                atr_pct = atr_val / price if atr_val and price > 0 else 0
                pos, t = self._build_pos_and_tech(d, name, date_str)
                entry_result = evaluate_entry(
                    pos, t, price, _realtime, _positions,
                    _all_klines, name, date_str, atr_pct, defense_weak
                )
                self._sync_pos_to_ps(name, pos, date_str)
                if entry_result and entry_result[0] == "买入" and entry_result[2] == "buy":
                    if self._buy(d, 0.30, entry_result[3]):
                        self._init_on_entry(ps, price)

            # ═══ ADD-ON LOGIC ═══ 调用实盘策略模块 evaluate_entry (build_phase==1)
            if ps["build_phase"] == 1 and ps["first_price"] > 0 and not ps["bought_today"]:
                # 使用实盘策略模块评估补仓/确认加仓
                _realtime = {dd._name: {"price": dd.close[0]} for dd in self.datas}
                _positions = {}
                for dd in self.datas:
                    nn = dd._name
                    _pp, _ = self._build_pos_and_tech(dd, nn, date_str)
                    _positions[nn] = _pp
                _all_klines = {}
                for dd in self.datas:
                    nn = dd._name
                    n_bars = min(21, len(dd.close))
                    closes = [float(dd.close[-(n_bars - i)]) for i in range(n_bars)]
                    _all_klines[nn] = [{"close": c} for c in closes]

                atr_pct = atr_val / price if atr_val and price > 0 else 0
                pos, t = self._build_pos_and_tech(d, name, date_str)
                entry_result = evaluate_entry(
                    pos, t, price, _realtime, _positions,
                    _all_klines, name, date_str, atr_pct, defense_weak
                )
                self._sync_pos_to_ps(name, pos, date_str)
                if entry_result and entry_result[0] == "买入" and entry_result[2] == "buy":
                    self._execute_addon_result(d, name, entry_result, date_str)
                elif entry_result and entry_result[0].startswith("持有"):
                    # 更新 ma5_touch_count (AO不回升时)
                    if "AO" in entry_result[3] or "ao" in entry_result[3].lower():
                        if price > ma5_v: ps["ma5_touch_count"] += 1
                        else: ps["ma5_touch_count"] = 0

        # ── End of day ──
        self._update_prev_macd()

    # ═══════════════════════════════════════════════
    # 统一策略调用桥: 将 backtrader 数据转为 PositionInfo + tech dict,
    # 调用实盘策略模块 (risk_manager / rsi_macd), 再转回 backtrader 操作
    # ═══════════════════════════════════════════════

    def _build_pos_and_tech(self, d, name, date_str):
        """将 backtrader 数据转为 (PositionInfo, tech dict)

        用于调用实盘策略模块 evaluate_stop / evaluate_entry
        """
        ps = self.ps[name]
        price = d.close[0]
        has_pos = self._has_position(d)
        avg = self._get_avg_cost(d)
        shares = self._get_shares(d)

        # ── 构建 PositionInfo ──
        pos = PositionInfo()
        if has_pos:
            pos.shares = int(shares)
            pos.avg_cost = avg
            pos.base_price = ps["base"] if ps["base"] > 0 else avg
            pos.peak_price = ps["peak_price"]
            pos.build_phase = ps["build_phase"]
            pos.build_first_price = ps["first_price"]
            pos.stop_level = ps["stop_level"]
            pos.below_ma20_count = ps["below_ma20"]
            pos.below_ma20_date = ps["below_ma20_date"] if ps["below_ma20_date"] else None
            pos.reached_8pct = ps["reached_activation"]
            pos.reached_15pct = ps["reached_lockin"]
            pos.trailing_stop_price = ps["trail"]
            pos.breakeven_activated = ps["breakeven_activated"]
            pos.entry_avg_cost = ps.get("entry_avg_cost", 0.0)
            pos.add_count = ps["add_count"]
            pos.ma5_touch_count = ps["ma5_touch_count"]
            pos.empty_days = ps["empty_days"]
            pos.prev_macd_status = ps["prev_macd_status"]
            pos.cooldown_until = ps["cooldown_until"] if ps["cooldown_until"] else None
            pos.confirm_batch_count = ps.get("confirm_batch_count", 0)
            pos.confirm_batch_date = ps.get("confirm_batch_date", None)
            pos.current_price = price
            pos.update_dead_active()
        else:
            pos.empty_days = ps["empty_days"]
            pos.prev_macd_status = ps["prev_macd_status"]
            pos.cooldown_until = ps["cooldown_until"] if ps["cooldown_until"] else None

        # ── 构建 tech dict ──
        rsi_val = self.rsi[name].rsi[0]
        ms = MACDStatus.STATUS_MAP.get(self.macd[name].status[0], "震荡")
        dif_val = self.macd[name].dif[0]
        ma5_v = self.ma5[name][0]
        ma10_v = self.ma10[name][0]
        ma20_v = self.ma20[name][0]
        atr_val = self.atr[name].atr[0]

        t = {
            "ma5": ma5_v,
            "ma10": ma10_v,
            "ma20": ma20_v,
            "dif": dif_val,
            "macd_status": ms,
            "rsi": rsi_val,
            "ao_now": self.ao[name].ao[0] if len(self.ao[name].ao) > 0 else None,
            "ao_5ago": self.ao[name].ao[-5] if len(self.ao[name].ao) >= 6 else None,
            "atr": atr_val,
        }
        return pos, t

    def _sync_pos_to_ps(self, name, pos, date_str):
        """将 PositionInfo 的状态变更同步回 self.ps[name]

        策略函数可能修改 pos 的 stop_level / below_ma20_count / reached_8pct /
        trailing_stop_price / build_phase / add_count 等字段，
        需要同步回回测引擎的 ps dict
        """
        ps = self.ps[name]
        ps["stop_level"] = pos.stop_level
        ps["below_ma20"] = pos.below_ma20_count
        ps["below_ma20_date"] = pos.below_ma20_date if pos.below_ma20_date else ""
        ps["reached_activation"] = pos.reached_8pct
        ps["reached_lockin"] = pos.reached_15pct
        ps["trail"] = pos.trailing_stop_price
        ps["breakeven_activated"] = pos.breakeven_activated
        ps["build_phase"] = pos.build_phase
        ps["add_count"] = pos.add_count
        ps["ma5_touch_count"] = pos.ma5_touch_count
        ps["empty_days"] = pos.empty_days
        if pos.cooldown_until:
            ps["cooldown_until"] = pos.cooldown_until
        ps["confirm_batch_count"] = getattr(pos, 'confirm_batch_count', 0)
        ps["confirm_batch_date"] = getattr(pos, 'confirm_batch_date', None)
        # entry_avg_cost / base / first_price / peak_price 由回测引擎管理，
        # 但策略 evaluate_entry 可能通过 pos._enter_position 更新了 build_phase,
        # 或 evaluate_stop 更新了 stop_level / below_ma20_count

    def _execute_stop_actions(self, d, name, stop_actions, date_str):
        """根据 risk_manager.check_stop_loss() 返回的 stop_actions 执行回测操作

        返回: True 表示已清仓/全平，调用方应 skip 后续逻辑
        """
        ps = self.ps[name]
        price = d.close[0]
        shares = self._get_shares(d)

        if not stop_actions:
            return False

        # 优先级: 清仓 > 减仓 > 卖活动仓
        for sig_name, sig_type in stop_actions:
            if sig_type in ("liquidate_trend", "liquidate_trailing", "liquidate_60pct",
                            "avg_stop_20pct", "hard_stop_25pct", "breakeven_stop"):
                self._close(d, sig_name)
                self._full_liquidate_state(ps, date_str)
                return True
            elif sig_type in ("reduce_30pct_all",):
                if self._sell(d, 30, sig_name):
                    ps["peak_price"] = price
                return False
            elif sig_type == "trend_profit_sell":
                active_shares = int(shares * ACTIVE_RATIO)
                sell_shares = int(active_shares * 0.10 / 100) * 100
                if sell_shares >= 100:
                    if self._sell(d, 10, sig_name):
                        ps["trend_sell_today"] += 1
                        if ps["trend_sell_today"] >= TREND_PROFIT_MAX_DAILY:
                            try:
                                dt = datetime.strptime(date_str, "%Y-%m-%d")
                                ps["trend_sell_cooling_until"] = (dt + timedelta(days=TREND_PROFIT_COOLDOWN_DAYS)).strftime("%Y-%m-%d")
                            except:
                                ps["trend_sell_cooling_until"] = ""
                return False
            elif sig_type == "sell_active_5pct":
                active_shares = int(shares * ACTIVE_RATIO)
                sell_shares = int(active_shares * 0.05 / 100) * 100
                if sell_shares >= 100:
                    if self._sell(d, 5, sig_name):
                        ps["ma5_sell_today"] += 1
                        if ps["ma5_sell_today"] >= TREND_PROFIT_MAX_DAILY:
                            try:
                                dt = datetime.strptime(date_str, "%Y-%m-%d")
                                ps["ma5_sell_cooling_until"] = (dt + timedelta(days=TREND_PROFIT_COOLDOWN_DAYS)).strftime("%Y-%m-%d")
                            except:
                                ps["ma5_sell_cooling_until"] = ""
                return False
        return False

    def _execute_entry_result(self, d, name, entry_result, date_str, init_pct=None):
        """根据 rsi_macd.evaluate_entry() 返回的 entry_result 执行回测买入操作

        参数:
          d: backtrader data
          name: ETF代码
          entry_result: (action, position_ratio, trade_type, reason) 或 None
          date_str: 日期
          init_pct: 建仓比例 (用于集中/轮动模式, 默认0.30)
        """
        ps = self.ps[name]
        if entry_result is None:
            return False
        action, position_ratio, trade_type, reason = entry_result
        if action.startswith("持有"):
            return False
        if trade_type != "buy":
            return False

        # 解析仓位比例: "30%(RSI抄底)" → 0.30
        if init_pct is not None:
            pct = init_pct
        elif position_ratio and "%" in str(position_ratio):
            try:
                pct_str = str(position_ratio).split("%")[0]
                # 去掉中文后缀如 "(补仓)" "(RSI抄底)"
                pct_num = ""
                for c in pct_str:
                    if c.isdigit() or c == '.':
                        pct_num += c
                    else:
                        break
                pct = float(pct_num) / 100.0 if pct_num else 0.30
            except:
                pct = 0.30
        else:
            pct = 0.30

        price = d.close[0]
        if self._buy(d, pct, reason):
            self._init_on_entry(ps, price)
            return True
        return False

    def _execute_addon_result(self, d, name, entry_result, date_str):
        """根据 rsi_macd.evaluate_entry() 返回的补仓/确认加仓结果执行操作

        entry_result 返回 ("买入", "15%(补仓)", "buy", ...) 或
                        ("买入", "30%(分批2)", "buy", ...) 等
        """
        ps = self.ps[name]
        if entry_result is None:
            return False
        action, position_ratio, trade_type, reason = entry_result
        if action != "买入":
            return False

        # 解析仓位比例
        if position_ratio and "%" in str(position_ratio):
            try:
                pct_str = str(position_ratio).split("%")[0]
                pct_num = ""
                for c in pct_str:
                    if c.isdigit() or c == '.':
                        pct_num += c
                    else:
                        break
                pct = float(pct_num) / 100.0 if pct_num else 0.15
            except:
                pct = 0.15
        else:
            pct = 0.15

        price = d.close[0]
        if "补仓" in str(position_ratio):
            if self._buy(d, pct, reason):
                ps["bought_today"] = True
                ps["add_count"] += 1
                return True
        elif "分批" in str(position_ratio) or "确认" in reason:
            if "confirm_batch_count" not in ps:
                ps["confirm_batch_count"] = 0
            if "confirm_batch_date" not in ps:
                ps["confirm_batch_date"] = ""
            if ps.get("confirm_batch_date") == date_str:
                return False
            if ps["confirm_batch_count"] >= 2:
                ps["build_phase"] = 2
                ps["bought_today"] = True
                avg = self._get_avg_cost(d)
                ps["base"] = avg
                ps["peak_price"] = price
                ps["first_price"] = price
                ps["reached_activation"] = False
                ps["reached_lockin"] = False
                ps["trail"] = 0
                ps["breakeven_activated"] = False
                return False
            if self._buy(d, pct, reason):
                ps["confirm_batch_count"] += 1
                ps["confirm_batch_date"] = date_str
                ps["add_count"] += 1
                if ps["confirm_batch_count"] >= 2:
                    ps["build_phase"] = 2
                    ps["bought_today"] = True
                    avg = self._get_avg_cost(d)
                    ps["base"] = avg
                    ps["peak_price"] = price
                    ps["first_price"] = price
                    ps["reached_activation"] = False
                    ps["reached_lockin"] = False
                    ps["trail"] = 0
                    ps["breakeven_activated"] = False
                return True
        else:
            if self._buy(d, pct, reason):
                ps["bought_today"] = True
                ps["add_count"] += 1
                return True
        return False

    def stop(self):
        print("\n" + "=" * 100)
        print("                    每笔交易明细")
        print("=" * 100)
        print(f"{'日期':<12} {'代码':<8} {'方向':<6} {'建仓信号':<28} {'卖出信号':<32} {'价格':>8} {'盈亏':>10} {'盈亏%':>8} {'持天':>4}")
        print("-" * 100)
        for t in self.trades:
            print(f"{t['date']:<12} {t['code']:<8} {t['direction']:<6} {t['entry_reason'][:27]:<28} {t['exit_reason'][:31]:<32} {t['price']:>8.3f} {t['pnl']:>+10.0f} {t['pnl_pct']:>+7.1f}% {t['hold_days']:>4d}")
        print("-" * 100)
        print(f"  总交易: {len(self.trades)} | 盈利: {self.win_trades} | 亏损: {self.loss_trades} | 胜率: {self.win_trades/len(self.trades)*100:.1f}%" if self.trades else "  无交易")
        print("=" * 100)


# ═══════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════
def main():
    # ── 命令行参数 ──
    run_000725 = "--000725" in sys.argv
    run_515880 = "--515880" in sys.argv
    run_rotation = "--rotation" in sys.argv  # 全仓轮动模式
    run_concentrated = "--concentrated" in sys.argv  # 集中持仓模式
    run_weekly_rotation = "--weekly-rotation" in sys.argv  # 每周轮动: 从候选池重算TOP3动量
    run_concentrated_pyramid = "--concentrated-pyramid" in sys.argv  # 集中TOP N+50%建仓+趋势加码
    run_pyramid = "--pyramid" in sys.argv  # 趋势加码模式

    # 集中持仓策略参数
    momentum_mode = "c2"  # simple | c2
    trail_mode = "fixed"  # fixed | e1 | e2
    init_pct = 0.30       # 0.30 | 0.50
    top_n = 3             # 集中持仓TOP N只ETF
    lookback = 20         # 动量回看期(日)
    ma60_filter = False   # MA60趋势过滤
    dynamic_pct = False   # 动态仓位
    rebalance = 5         # 重平衡周期(天)
    stop_loss = 0.25      # 固定止损比例(对齐config.yaml hard_stop_pct)
    trail_pct = CONF.get('trail_pct', 0.12)      # 移动止盈回撤比例(fixed模式): 从config读取
    sector_diversify = False  # 板块分散
    confirm_days = 0      # 换仓延迟确认天数
    trend_entry = False   # 趋势建仓通道
    rsi_entry_max = 55    # RSI抄底上限
    rotation_interval = 5  # 每周轮动间隔(天)

    # --start=YYYY-MM-DD / --end=YYYY-MM-DD 自定义区间
    custom_start = None
    custom_end = None
    for arg in sys.argv:
        if arg.startswith("--start="):
            custom_start = arg.split("=", 1)[1]
        elif arg.startswith("--end="):
            custom_end = arg.split("=", 1)[1]
        elif arg.startswith("--momentum="):
            momentum_mode = arg.split("=", 1)[1]
        elif arg.startswith("--trail="):
            trail_mode = arg.split("=", 1)[1]
        elif arg.startswith("--init-pct="):
            init_pct = float(arg.split("=", 1)[1])
        elif arg.startswith("--top-n="):
            top_n = int(arg.split("=", 1)[1])
        elif arg.startswith("--lookback="):
            lookback = int(arg.split("=", 1)[1])
        elif arg == "--ma60-filter":
            ma60_filter = True
        elif arg == "--dynamic-pct":
            dynamic_pct = True
        elif arg.startswith("--rebalance="):
            rebalance = int(arg.split("=", 1)[1])
        elif arg.startswith("--stop-loss="):
            stop_loss = float(arg.split("=", 1)[1])
        elif arg.startswith("--trail-pct="):
            trail_pct = float(arg.split("=", 1)[1])
        elif arg == "--sector-diversify":
            sector_diversify = True
        elif arg.startswith("--confirm-days="):
            confirm_days = int(arg.split("=", 1)[1])
        elif arg == "--trend-entry":
            trend_entry = True
        elif arg.startswith("--rsi-entry-max="):
            rsi_entry_max = float(arg.split("=", 1)[1])
        elif arg.startswith("--rotation-interval="):
            rotation_interval = int(arg.split("=", 1)[1])

    # --period X 支持: 从 config.yaml 读取
    period_map = CONF['backtest']['periods']
    period = None
    for arg in sys.argv:
        if arg.startswith("--period="):
            period = arg.split("=")[1]
            break

    if run_000725:
        active_codes = {"000725": {"name": "京东方A", "sid": "sz000725"}}
        trade_start, trade_end = "2025-08-01", "2026-07-27"
    elif run_515880:
        active_codes = {"515880": CODES["515880"]}
        trade_start, trade_end = "2024-08-01", "2026-07-27"
    elif custom_start and custom_end:
        active_codes = {k: v for k, v in CODES.items() if k != "000725"}
        trade_start, trade_end = custom_start, custom_end
    elif period and period in period_map:
        active_codes = {k: v for k, v in CODES.items() if k != "000725"}
        trade_start, trade_end = period_map[period]
    else:
        active_codes = {k: v for k, v in CODES.items() if k != "000725"}
        trade_start, trade_end = TRADE_START, TRADE_END

    # 每周轮动模式: 将候选池(rotation.py ETF_CANDIDATES)中的ETF也加入数据加载范围
    if run_weekly_rotation:
        candidate_added = 0
        for code, cfg in ETF_CANDIDATES.items():
            if code not in active_codes:
                active_codes[code] = {"name": cfg["name"], "sid": cfg["sid"]}
                candidate_added += 1
        # 510300 沪深300ETF 作为大盘自适应仓位指标，不参与候选池
        if "510300" not in active_codes:
            active_codes["510300"] = {"name": "沪深300ETF", "sid": "sh510300"}
        print(f"▸ 每周轮动: 候选池新增 {candidate_added} 只ETF (共{len(active_codes)}只)")

    print("▸ 拉取历史K线 (腾讯前复权 API)...")
    # 计算K线拉取起始日: trade_start 往前推 120 个交易日（预热期）
    data_start = (pd.Timestamp(trade_start) - pd.offsets.BDay(120)).strftime("%Y%m%d")
    data_end = pd.Timestamp(trade_end).strftime("%Y%m%d")
    print(f"  K线区间: {data_start} → {data_end} (预热120个交易日)")
    dataframes = {}
    for code, cfg in active_codes.items():
        try:
            df = fetch_daily(code, cfg["sid"], data_start=data_start, data_end=data_end)
            if df is None or len(df) == 0:
                print(f"  ⚠ {code} {cfg['name']:10s}  无数据, 跳过")
                continue
            dataframes[code] = df
            print(f"  ✓ {code} {cfg['name']:10s}  {len(df):3d} 条K线 (前复权)")
        except Exception as e:
            print(f"  ✗ {code} {cfg['name']:10s}  拉取失败: {e}"); sys.exit(1)

    # 移除未成功加载的ETF from active_codes
    for code in list(active_codes.keys()):
        if code not in dataframes:
            del active_codes[code]

    start = pd.Timestamp(trade_start); end = pd.Timestamp(trade_end)
    # 切片并移除回测区间内无数据的ETF
    empty_codes = []
    for code in list(dataframes.keys()):
        sliced = dataframes[code][start:end]
        if len(sliced) == 0:
            print(f"  ⚠ {code} {active_codes[code]['name']:10s}  回测区间内无数据, 跳过")
            empty_codes.append(code)
        else:
            dataframes[code] = sliced
    for code in empty_codes:
        del dataframes[code]
        del active_codes[code]
    if not dataframes:
        print("✗ 所有ETF在回测区间内均无数据, 退出")
        sys.exit(1)

    # ── 对齐数据: 晚上市ETF补前置行, 使backtrader多数据同步不卡住 ──
    # backtrader要求所有数据都就绪才触发next(), 如果588170从2025年才有数据,
    # 则前3.5年的next()都不会被调用. 解决方案: 用首根K线价格回填上市前日期,
    # 策略中通过volume<0标记跳过未上市ETF.
    all_dates = pd.date_range(start=start, end=end, freq="B")  # 工作日
    for code in dataframes:
        df = dataframes[code]
        # 记录原始上市日期(reindex前)
        first_date = df.index[0]
        # 重新索引到完整交易日历
        df = df.reindex(all_dates)
        # 上市前: 用首根K线价格回填(bfill), volume设为-1标记为"未上市"
        # 上市后缺日(节假日): 用前值填充(ffill), volume=0
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].ffill().bfill()
        df["volume"] = df["volume"].fillna(0).astype(float)
        # 将上市前的volume标记为-1(区别于节假日的0)
        df.loc[df.index < first_date, "volume"] = -1.0
        dataframes[code] = df

    cerebro = bt.Cerebro()

    # 计算每只ETF的资金基数（与实盘一致：从ETFS配置读取）
    _etfs_cfg = get_etfs_config()
    FUND_PER_ETF = list(_etfs_cfg.values())[0]["fund"] if _etfs_cfg else 44000
    if run_concentrated or run_concentrated_pyramid or run_weekly_rotation:
        if run_weekly_rotation:
            mode_label = "每周轮动"
        elif run_concentrated_pyramid:
            mode_label = "集中金字塔"
        else:
            mode_label = "集中持仓"
        print(f"▸ {mode_label}模式: 每只ETF资金基数 ¥{FUND_PER_ETF:,} (从ETFS配置读取)")
    else:
        print(f"▸ 每只ETF资金基数: ¥{FUND_PER_ETF:,} (从ETFS配置读取)")

    for code, df in dataframes.items():
        data = bt.feeds.PandasData(dataname=df, name=code)
        cerebro.adddata(data, name=code)

    cerebro.addstrategy(ETFStrategy, fund_per_etf=FUND_PER_ETF,
                        rotation_mode=run_rotation,
                        concentrated_mode=run_concentrated,
                        weekly_rotation_mode=run_weekly_rotation,
                        concentrated_pyramid_mode=run_concentrated_pyramid,
                        pyramid_mode=run_pyramid,
                        momentum_mode=momentum_mode,
                        trail_mode=trail_mode,
                        init_pct=init_pct,
                        top_n=top_n,
                        lookback=lookback,
                        ma60_filter=ma60_filter,
                        dynamic_pct=dynamic_pct,
                        rebalance=rebalance,
                        stop_loss=stop_loss,
                        trail_pct=trail_pct,
                        sector_diversify=sector_diversify,
                        confirm_days=confirm_days,
                        trend_entry=trend_entry,
                        rsi_entry_max=rsi_entry_max,
                        rotation_interval=rotation_interval)

    # Analyzers
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', timeframe=bt.TimeFrame.Days, annualize=True, riskfreerate=RISK_FREE_RATE)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')

    # 手续费+滑点
    comm = ETFCommission()
    cerebro.broker.addcommissioninfo(comm)

    cerebro.broker.setcash(TOTAL_FUND)
    # 不用COC, 用次日开盘价成交(更真实)
    cerebro.broker.set_coc(False)

    print(f"\n▸ 回测区间: {trade_start} → {trade_end}")
    print(f"▸ 引擎: backtrader v{bt.__version__}")
    print(f"▸ 共享资金池: ¥{TOTAL_FUND:,}")
    if run_concentrated:
        print(f"▸ 策略参数: momentum={momentum_mode} trail={trail_mode} init_pct={init_pct} top_n={top_n} lookback={lookback} ma60={ma60_filter} dynamic_pct={dynamic_pct}")
    if run_weekly_rotation:
        print(f"▸ 策略参数: momentum={momentum_mode} trail={trail_mode} init_pct={init_pct} top_n={top_n} lookback={lookback} rotation_interval={rotation_interval} trail_pct={trail_pct} stop_loss={stop_loss}")
    if run_000725:
        print(f"▸ 单标的模式: 000725 京东方A")

    results = cerebro.run()
    strat = results[0]

    # Analyzer结果
    final = cerebro.broker.getvalue()
    total_ret = (final - TOTAL_FUND) / TOTAL_FUND * 100

    dd = strat.analyzers.drawdown.get_analysis()
    max_dd = dd.max.drawdown
    ta = strat.analyzers.trades.get_analysis()
    sharpe_a = strat.analyzers.sharpe.get_analysis()

    total_trades = ta.get('total', {}).get('total', 0)
    won = ta.get('won', {}).get('total', 0)
    lost = ta.get('lost', {}).get('total', 0)
    win_rate = won / total_trades * 100 if total_trades else 0
    avg_win = ta.get('won', {}).get('pnl', {}).get('average', 0)
    avg_loss = abs(ta.get('lost', {}).get('pnl', {}).get('average', 1))
    pf = avg_win / avg_loss if avg_loss > 0 else 0

    # 年化 (用实际日期差)
    from datetime import date
    d1 = date.fromisoformat(trade_start); d2 = date.fromisoformat(trade_end)
    years = (d2 - d1).days / 365.0
    annual_ret = ((final / TOTAL_FUND) ** (1 / years) - 1) * 100 if years > 0 else 0
    calmar = annual_ret / max_dd if max_dd > 0 else 0

    print("\n" + "=" * 70)
    print("                    回 测 结 果 (backtrader)")
    print("=" * 70)
    print(f"\n  初始资金:    ¥{TOTAL_FUND:>12,.0f}")
    print(f"  最终资金:    ¥{final:>12,.0f}")
    print(f"  总收益:       {total_ret:>+11.2f}%")
    print(f"  年化收益:     {annual_ret:>+11.2f}%")
    print(f"  最大回撤:     {max_dd:>11.2f}%")
    print(f"  Calmar 比率:  {calmar:>11.2f}")
    print(f"  夏普比率:     {sharpe_a.get('sharperatio', 0) or 0:>11.2f}")
    print(f"  交易笔数:     {total_trades:>11d}")
    print(f"  胜率:         {win_rate:>11.1f}%")
    print(f"  盈亏比:       {pf:>11.2f}")

    print(f"\n{'─' * 70}")
    print(f"  {'ETF':<8} {'名称':<12} {'持仓':>10} {'市值':>12} {'状态':>8}")
    print(f"  {'─' * 70}")
    for d in strat.datas:
        name = d._name
        pos = strat.getposition(d)
        cfg = active_codes.get(name, {"name": name})
        mkt = pos.size * d.close[0]
        status = "空仓" if pos.size == 0 else ("建仓中" if strat.ps[name]["build_phase"] == 1 else "持仓中")
        print(f"  {name:<8} {cfg['name']:<12} {pos.size:>10,d} ¥{mkt:>11,.0f} {status:>8}")

    print(f"\n{'=' * 70}")
    print(f"  回测完成 (backtrader引擎)")
    print(f"{'=' * 70}")

if __name__ == "__main__":
    main()
