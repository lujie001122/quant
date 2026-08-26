#!/usr/bin/env python3
"""
分析 159516/588170 在 2026-02-25 ~ 2026-08-25 的详细交易流水
找出利润流失点
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtrader as bt
import pandas as pd
import yaml
from datetime import datetime, timedelta

# ── 加载配置 ──
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.yaml')
with open(_CONFIG_PATH) as f:
    CONF = yaml.safe_load(f)

from market_data import fetch_klines_daily
from position_info import TREND_PROFIT_MAX_DAILY, TREND_PROFIT_COOLDOWN_DAYS

TOTAL_FUND = CONF['total_fund']
FEE = CONF['backtest']['fee']
SLIPPAGE_PCT = CONF['backtest']['slippage_pct']
COOLDOWN_DAYS = CONF['cooldown_days']
DEAD_RATIO = CONF['dead_ratio']
ACTIVE_RATIO = CONF['active_ratio']
POSITION_CAP = CONF['entry']['position_cap']
RISK_FREE_RATE = CONF['backtest']['risk_free_rate']

# ── 复用backtest_bt的指标和策略 ──
from backtest_bt import (
    WilderRSI, MACDStatus, AOIndicator, ETFCommission,
    ETFStrategy, evaluate_stop, resolve_stop_signal, evaluate_entry,
    fetch_daily
)

# ═══════════════════════════════════════════════
# 自定义策略：详细日志版
# ═══════════════════════════════════════════════
class VerboseETFStrategy(ETFStrategy):
    """继承ETFStrategy，在每次买卖时输出详细日志"""

    def __init__(self):
        super().__init__()
        self.action_log = []  # 存储所有操作

    def _log_action(self, action_type, data, shares, price, reason, position_after):
        """记录操作"""
        name = data._name
        date_str = self.data.datetime.date(0).strftime("%Y-%m-%d")
        ps = self.ps[name]
        entry = {
            'date': date_str,
            'code': name,
            'action': action_type,
            'shares': shares,
            'price': price,
            'reason': reason,
            'position_after': position_after,
            'peak_price': ps.get('peak_price', 0),
            'trail': ps.get('trail', 0),
            'avg_cost': self._get_avg_cost(data),
            'reached_8': ps.get('reached_8', False),
            'reached_15': ps.get('reached_15', False),
            'build_phase': ps.get('build_phase', 0),
            'base': ps.get('base', 0),
            'first_price': ps.get('first_price', 0),
            'broker_value': self.broker.getvalue(),
            'broker_cash': self.broker.getcash(),
        }
        self.action_log.append(entry)

    def _buy(self, data, pct, reason):
        """记录买入前的状态"""
        name = data._name
        price = data.close[0]
        current = self._get_shares(data)
        target_value = min(TOTAL_FUND * pct, TOTAL_FUND * POSITION_CAP)
        target_shares = int(target_value / price / 100) * 100
        need = target_shares - current
        if need < 100:
            return False
        cost = need * price * (1 + SLIPPAGE_PCT) + FEE
        if cost > self.broker.getcash():
            need = int(self.broker.getcash() / price / 100) * 100
            if need < 100:
                return False

        self._log_action('买入', data, need, price, reason, current + need)
        result = super()._buy(data, pct, reason)
        return result

    def _sell(self, data, pct, reason):
        """记录卖出前的状态"""
        name = data._name
        current = self._get_shares(data)
        if current < 100:
            return False
        price = data.close[0]
        shares = int(current * pct / 100 / 100) * 100
        if shares < 100:
            return False
        if shares > current:
            shares = (current // 100) * 100
            if shares < 100:
                return False

        self._log_action('卖出', data, shares, price, reason, current - shares)
        result = super()._sell(data, pct, reason)
        return result

    def _close(self, data, reason):
        """记录清仓前的状态"""
        name = data._name
        current = self._get_shares(data)
        if current < 100:
            return False
        price = data.close[0]

        self._log_action('清仓', data, current, price, reason, 0)
        result = super()._close(data, reason)
        return result

    def stop(self):
        """重写stop，输出详细流水"""
        super().stop()
        self._print_detailed_log()

    def _print_detailed_log(self):
        """打印完整交易流水"""
        print("\n")
        print("=" * 140)
        print("                    159516 / 588170 详细交易流水 (2026-02-25 ~ 2026-08-25)")
        print("=" * 140)

        # 过滤目标区间
        filtered = [a for a in self.action_log
                    if "2026-02-25" <= a['date'] <= "2026-08-25"]

        if not filtered:
            print("\n  ⚠ 目标区间内无交易记录")
            return

        print(f"\n  总操作数: {len(filtered)}")
        print()
        print(f"{'日期':<12} {'代码':<8} {'操作':<6} {'股数':>10} {'价格':>10} {'持仓后':>10} "
              f"{'成本':>10} {'峰值':>10} {'移动止盈':>10} {'Phase':>6} {'市值':>12} {'原因'}")
        print("-" * 140)

        for a in filtered:
            phase_str = str(a['build_phase'])
            mkt_val = a['position_after'] * a['price']
            trail_str = f"{a['trail']:.4f}" if a['trail'] > 0 else "0"
            print(f"{a['date']:<12} {a['code']:<8} {a['action']:<6} {a['shares']:>10,d} "
                  f"{a['price']:>10.4f} {a['position_after']:>10,d} {a['avg_cost']:>10.4f} "
                  f"{a['peak_price']:>10.4f} {trail_str:>10} {phase_str:>6} "
                  f"¥{mkt_val:>10,.0f} {a['reason'][:60]}")

        print("-" * 140)

        # ── 按ETF分组分析 ──
        for code in ['159516', '588170']:
            code_actions = [a for a in filtered if a['code'] == code]
            if not code_actions:
                continue

            print(f"\n{'='*100}")
            print(f"  {code} 交易分析")
            print(f"{'='*100}")

            # 统计买入/卖出
            buys = [a for a in code_actions if a['action'] == '买入']
            sells = [a for a in code_actions if a['action'] == '卖出']
            closes = [a for a in code_actions if a['action'] == '清仓']

            print(f"\n  买入: {len(buys)}笔, 卖出: {len(sells)}笔, 清仓: {len(closes)}笔")

            # 清仓详情
            if closes:
                print(f"\n  --- 清仓记录 ---")
                for c in closes:
                    print(f"  {c['date']} 清仓 @ {c['price']:.4f} | 原因: {c['reason']}")

            # 移动止盈触发详情
            trail_closes = [c for c in closes if '移动止盈' in c['reason']]
            if trail_closes:
                print(f"\n  --- 移动止盈触发详情 ---")
                for c in trail_closes:
                    print(f"  {c['date']} {c['reason']} | 卖出价={c['price']:.4f} | 峰值={c['peak_price']:.4f}")

            # 破MA5卖活动仓详情
            ma5_sells = [s for s in sells if '破MA5卖活动仓' in s['reason']]
            if ma5_sells:
                print(f"\n  --- 破MA5卖活动仓 (碎片化卖出) ---")
                total_sold = sum(s['shares'] for s in ma5_sells)
                print(f"  共 {len(ma5_sells)} 笔, 累计卖出 {total_sold:,} 股")
                for s in ma5_sells:
                    print(f"  {s['date']} 卖出 {s['shares']:,}股 @ {s['price']:.4f} | 持仓后 {s['position_after']:,}股 | {s['reason']}")

            # 趋势止盈详情
            trend_sells = [s for s in sells if '趋势止盈' in s['reason']]
            if trend_sells:
                print(f"\n  --- 趋势止盈卖出 ---")
                for s in trend_sells:
                    print(f"  {s['date']} 卖出 {s['shares']:,}股 @ {s['price']:.4f} | {s['reason']}")

            # 网格卖出
            grid_sells = [s for s in sells if '网格' in s['reason']]
            if grid_sells:
                print(f"\n  --- 网格卖出 ---")
                for s in grid_sells:
                    print(f"  {s['date']} 卖出 {s['shares']:,}股 @ {s['price']:.4f} | {s['reason']}")

            # 买入详情
            if buys:
                print(f"\n  --- 买入记录 ---")
                for b in buys:
                    print(f"  {b['date']} 买入 {b['shares']:,}股 @ {b['price']:.4f} | {b['reason']}")

            # ── 持仓变化时间线 ──
            print(f"\n  --- 持仓变化时间线 ---")
            prev_pos = 0
            for a in code_actions:
                delta = a['position_after'] - prev_pos
                delta_str = f"+{delta:,}" if delta > 0 else f"{delta:,}"
                print(f"  {a['date']} {a['action']:<4} {a['shares']:>8,d}股 @{a['price']:.4f} "
                      f"持仓:{prev_pos:>8,d}→{a['position_after']:>8,d} (Δ{delta_str:>10}) "
                      f"峰值:{a['peak_price']:.4f} 止盈线:{a['trail']:.4f} {a['reason'][:50]}")
                prev_pos = a['position_after']

        # ── 关键发现 ──
        print(f"\n{'='*100}")
        print(f"  关键数据点")
        print(f"{'='*100}")

        for code in ['159516', '588170']:
            code_actions = [a for a in filtered if a['code'] == code]
            if not code_actions:
                continue

            # 找首日买入价和最后持仓价
            first_buy = next((a for a in code_actions if a['action'] == '买入'), None)
            last_action = code_actions[-1] if code_actions else None

            if first_buy:
                print(f"\n  {code}: 首笔买入 {first_buy['date']} @ {first_buy['price']:.4f}")

            if last_action and last_action['position_after'] > 0:
                print(f"  {code}: 最终持仓 {last_action['position_after']:,}股 @ {last_action['price']:.4f} "
                      f"市值 ¥{last_action['position_after'] * last_action['price']:,.0f}")

            # 峰值价格
            peak = max((a['peak_price'] for a in code_actions if a['peak_price'] > 0), default=0)
            if peak > 0:
                print(f"  {code}: 期间最高峰值价 {peak:.4f}")

            # 移动止盈触发
            trail_triggers = [a for a in code_actions if '移动止盈' in a['reason']]
            if trail_triggers:
                for t in trail_triggers:
                    loss_from_peak = (t['peak_price'] / t['price'] - 1) * 100 if t['peak_price'] > 0 else 0
                    print(f"  {code}: 移动止盈触发 {t['date']} @{t['price']:.4f} 峰值{t['peak_price']:.4f} "
                          f"从峰值回撤{loss_from_peak:.1f}%")

        # 最终资金
        print(f"\n  最终资金: ¥{self.broker.getvalue():,.0f}")
        print(f"  现金: ¥{self.broker.getcash():,.0f}")


# ═══════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════
def main():
    trade_start = "2026-02-25"
    trade_end = "2026-08-25"

    # 只分析159516和588170
    from state_center import get_code_map
    all_codes = get_code_map()
    active_codes = {
        "159516": all_codes["159516"],
        "588170": all_codes["588170"],
    }

    print("▸ 拉取历史K线 (腾讯前复权 API)...")
    data_start = (pd.Timestamp(trade_start) - pd.offsets.BDay(120)).strftime("%Y%m%d")
    data_end = pd.Timestamp(trade_end).strftime("%Y%m%d")
    print(f"  K线区间: {data_start} → {data_end} (预热120个交易日)")

    dataframes = {}
    for code, cfg in active_codes.items():
        try:
            df = fetch_daily(code, cfg["sid"], data_start=data_start, data_end=data_end)
            dataframes[code] = df
            print(f"  ✓ {code} {cfg['name']:10s}  {len(df):3d} 条K线 (前复权)")
        except Exception as e:
            print(f"  ✗ {code} {cfg['name']:10s}  拉取失败: {e}")
            sys.exit(1)

    start = pd.Timestamp(trade_start)
    end = pd.Timestamp(trade_end)

    # 切片
    for code in list(dataframes.keys()):
        sliced = dataframes[code][start:end]
        if len(sliced) == 0:
            print(f"  ⚠ {code} 回测区间内无数据, 跳过")
            del dataframes[code]
            del active_codes[code]
        else:
            dataframes[code] = sliced

    if not dataframes:
        print("✗ 所有ETF在回测区间内均无数据, 退出")
        sys.exit(1)

    # 对齐数据
    all_dates = pd.date_range(start=start, end=end, freq="B")
    for code in dataframes:
        df = dataframes[code]
        first_date = df.index[0]
        df = df.reindex(all_dates)
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].ffill().bfill()
        df["volume"] = df["volume"].fillna(0).astype(float)
        df.loc[df.index < first_date, "volume"] = -1.0
        dataframes[code] = df

    cerebro = bt.Cerebro()

    for code, df in dataframes.items():
        data = bt.feeds.PandasData(dataname=df, name=code)
        cerebro.adddata(data, name=code)

    cerebro.addstrategy(VerboseETFStrategy)

    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe',
                        timeframe=bt.TimeFrame.Days, annualize=True,
                        riskfreerate=RISK_FREE_RATE)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')

    comm = ETFCommission()
    cerebro.broker.addcommissioninfo(comm)
    cerebro.broker.setcash(TOTAL_FUND)
    cerebro.broker.set_coc(False)

    print(f"\n▸ 回测区间: {trade_start} → {trade_end}")
    print(f"▸ ETF: 159516 半导体设备ETF + 588170 科创半导体ETF")
    print(f"▸ 共享资金池: ¥{TOTAL_FUND:,}")
    print()

    results = cerebro.run()
    strat = results[0]

    # 最终结果
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

    from datetime import date
    d1 = date.fromisoformat(trade_start)
    d2 = date.fromisoformat(trade_end)
    years = (d2 - d1).days / 365.0
    annual_ret = ((final / TOTAL_FUND) ** (1 / years) - 1) * 100 if years > 0 else 0
    calmar = annual_ret / max_dd if max_dd > 0 else 0

    print("\n" + "=" * 70)
    print("                    回 测 结 果")
    print("=" * 70)
    print(f"\n  初始资金:    ¥{TOTAL_FUND:>12,.0f}")
    print(f"  最终资金:    ¥{final:>12,.0f}")
    print(f"  总收益:       {total_ret:>+11.2f}%")
    print(f"  年化收益:     {annual_ret:>+11.2f}%")
    print(f"  最大回撤:     {max_dd:>11.2f}%")
    print(f"  Calmar:       {calmar:>11.2f}")
    print(f"  夏普:         {sharpe_a.get('sharperatio', 0) or 0:>11.2f}")
    print(f"  交易笔数:     {total_trades:>11d}")
    print(f"  胜率:         {win_rate:>11.1f}%")

    print(f"\n  ETF持仓:")
    for d in strat.datas:
        name = d._name
        pos = strat.getposition(d)
        cfg = active_codes.get(name, {"name": name})
        mkt = pos.size * d.close[0]
        status = "空仓" if pos.size == 0 else "持仓中"
        print(f"  {name:<8} {cfg['name']:<12} {pos.size:>10,d}股 ¥{mkt:>11,.0f} {status:>8}")


if __name__ == "__main__":
    main()