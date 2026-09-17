#!/usr/bin/env python3
"""
tests/test_analyzer.py — 绩效分析引擎单元测试

前置: 先跑 python3 tests/seed_data.py 写入模拟数据。

用法:
  source .venv/bin/activate
  python3 tests/seed_data.py && python3 tests/test_analyzer.py
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from performance.analyzer import get_analyzer

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def test_position_cost_trace():
    """★核心: 持仓成本追溯"""
    print("\n[1] position_cost_trace(515880)")
    az = get_analyzer()
    r = az.position_cost_trace('515880')

    check("代码/名称", r['code'] == '515880' and r['name'] == '通信ETF')
    check("持仓存在", r['position'] is not None)
    check("持仓股数=106600", r['position']['shares'] == 106600,
          f"实际{r['position']['shares']}")
    check("持仓均价=0.657", r['position']['avg_cost'] == 0.657)
    check("建仓均价=0.668", r['position']['entry_avg_cost'] == 0.668)
    check("首次建仓=2026-09-10", r['position']['first_buy_date'] == '2026-09-10')
    check("阶梯止盈 2/4/6 全True",
          r['position']['reached_2pct'] and r['position']['reached_4pct']
          and r['position']['reached_6pct'])

    # 买入: 50000+30000+26600+21300(做T买) = 127900
    check("买入4笔", len(r['buys']) == 4, f"实际{len(r['buys'])}")
    check("累计买入127900股", r['summary']['total_bought'] == 127900,
          f"实际{r['summary']['total_bought']}")
    check("第一笔买入信号=RSI抄底", r['buys'][0]['signal_source'] == 'RSI抄底')
    check("第一笔买入50000股@0.665",
          r['buys'][0]['shares'] == 50000 and r['buys'][0]['price'] == 0.665)
    check("第一笔合同编号", r['buys'][0]['contract_no'] == '20260910001')
    check("买入有signal_date", r['buys'][0]['signal_date'] == '2026-09-10')

    # 卖出: 20000(趋势止盈) + 21300(做T) = 41300
    check("卖出2笔", len(r['sells']) == 2, f"实际{len(r['sells'])}")
    check("累计卖出41300股", r['summary']['total_sold'] == 41300)
    trend_sell = r['sells'][0]
    check("趋势止盈盈亏=+300", trend_sell['pnl'] == 300)
    check("盈亏比例=0.0225", trend_sell['pnl_pct'] == 0.0225)
    check("建仓成本=0.665", trend_sell['entry_cost'] == 0.665)
    check("持仓天数=6", trend_sell['hold_days'] == 6)
    check("信号来源=趋势止盈", trend_sell['signal_source'] == '趋势止盈')

    # 汇总: 300 + 149.10 = 449.10
    check("累计盈亏=449.10", r['summary']['total_pnl'] == 449.10,
          f"实际{r['summary']['total_pnl']}")
    # 加权成本: (33250+19560+17236.80+14654.40)/127900 ≈ 0.6621
    check("加权成本>0.65且<0.67",
          0.65 < r['summary']['weighted_avg_cost'] < 0.67,
          f"实际{r['summary']['weighted_avg_cost']}")
    # 做T配对
    t0_sell = [s for s in r['sells'] if s['trade_category'] == 't0']
    check("做T卖出有pair_deal_no", t0_sell and t0_sell[0]['pair_deal_no'] == '20260917010')


def test_daily_report():
    print("\n[2] daily_report(2026-09-17)")
    r = get_analyzer().daily_report('2026-09-17')
    check("2笔交易", r['totals']['total_trades'] == 2,
          f"实际{r['totals']['total_trades']}")
    check("买入1笔/卖出1笔", r['totals']['buy_count'] == 1
          and r['totals']['sell_count'] == 1)
    check("已实现盈亏=149.10", r['totals']['realized_pnl'] == 149.10)
    cats = {c['category']: c for c in r['by_category']}
    check("做T分类统计存在", 't0' in cats)


def test_contract_detail():
    print("\n[3] contract_detail(20260910001)")
    r = get_analyzer().contract_detail('20260910001')
    check("委托存在", r['entrust'] is not None)
    check("委托50000股已成", r['entrust']['order_shares'] == 50000
          and r['entrust']['status'] == '已成')
    check("两笔成交", r['agg'] and r['agg']['deal_count'] == 2)
    check("聚合股数50000", r['agg']['total_shares'] == 50000)
    check("聚合均价0.665", r['agg']['avg_fill_price'] == 0.665)


def test_monthly_summary():
    print("\n[4] monthly_summary(2026-09)")
    r = get_analyzer().monthly_summary('2026-09')
    o = r['overall']
    # 卖出: 20000(+300), 21300(+149.10), 5000(-615), 9800(+225.20) → 3盈1亏
    check("总交易11笔", o['total_trades'] == 11, f"实际{o['total_trades']}")
    check("卖出4笔", o['sell_trades'] == 4)
    check("3盈1亏", o['win_count'] == 3 and o['loss_count'] == 1)
    check("胜率75%", o['win_rate'] == 0.75, f"实际{o['win_rate']}")
    check("总盈亏=59.30", o['total_pnl'] == 59.30, f"实际{o['total_pnl']}")
    check("分ETF统计3只", len(r['by_code']) == 3)
    check("信号来源统计≥4种", len(r['by_signal']) >= 4)


def test_t0_vs_normal():
    print("\n[5] t0_vs_normal_stats")
    r = get_analyzer().t0_vs_normal_stats('2026-09-01', '2026-09-30')
    cats = {c['category']: c for c in r['categories']}
    check("normal分类存在", 'normal' in cats)
    check("t0分类存在", 't0' in cats)
    check("grid分类存在", 'grid' in cats)
    check("normal盈亏=-315.00", cats['normal']['pnl'] == -315.00,
          f"实际{cats['normal']['pnl']}")
    check("t0配对盈亏=149.10", cats['t0']['pair_pnl'] == 149.10)


def test_equity_curve():
    print("\n[6] equity_curve")
    r = get_analyzer().equity_curve('2026-09-01', '2026-09-30')
    check("5个交易日", len(r['dates']) == 5, f"实际{len(r['dates'])}")
    check("首日总资产216500", r['total_assets'][0] == 216500)
    check("区间收益率≈0.92%", abs(r['total_return'] - 0.0092) < 0.0005,
          f"实际{r['total_return']}")
    check("最大回撤<0", r['max_drawdown'] <= 0)


def test_signal_stats():
    print("\n[7] signal_source_stats")
    rows = get_analyzer().signal_source_stats('2026-09-01', '2026-09-30')
    src = {x['signal_source']: x for x in rows}
    check("趋势止盈1笔全胜", src.get('趋势止盈', {}).get('win_rate') == 1.0)
    check("均价止损亏损-615", src.get('均价止损', {}).get('total_pnl') == -615.0)


def test_trade_recorder_mysql():
    print("\n[8] trade_recorder.record() 写 MySQL")
    from core.trade_recorder import TradeRecorder
    from core.repositories import TradeRepo
    rec = TradeRecorder()
    t = rec.record(code='515880', direction='buy', shares=1000,
                   price=0.66, amount=660.0, fee=0.5,
                   tag='t0', account='main',
                   contract_no='TEST-CONTRACT-001', deal_no='TEST-DEAL-001',
                   signal_date='2026-09-17', signal_source='RSI抄底',
                   strategy_name='ETF轮动', trade_category='t0',
                   pair_deal_no='TEST-PAIR', reason='测试记录')
    check("返回trade_id非空", t.trade_id is not None)
    rows = [x for x in TradeRepo.list_by_code('515880')
            if x.contract_no == 'TEST-CONTRACT-001']
    check("MySQL可查到", len(rows) == 1)
    if rows:
        check("signal_source透传", rows[0].signal_source == 'RSI抄底')
        check("trade_category=t0", rows[0].trade_category == 't0')
        check("signal_date透传", str(rows[0].signal_date) == '2026-09-17')
        check("tag→category映射", rows[0].trade_category == 't0')
        # 清理测试记录
        from core.db import get_session
        from core.models import Trade
        with get_session() as s:
            s.query(Trade).filter(Trade.contract_no == 'TEST-CONTRACT-001').delete()
        check("清理测试记录", True)


def main():
    print("═══ 绩效分析引擎单元测试 ═══")
    test_position_cost_trace()
    test_daily_report()
    test_contract_detail()
    test_monthly_summary()
    test_t0_vs_normal()
    test_equity_curve()
    test_signal_stats()
    test_trade_recorder_mysql()

    print("\n═══════════════════════════════════════════")
    print(f"  结果: {PASS} 通过, {FAIL} 失败")
    print("═══════════════════════════════════════════")
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
