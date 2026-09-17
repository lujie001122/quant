#!/usr/bin/env python3
"""
performance/report_cli.py — 绩效分析 CLI 入口

命令清单:
  # ★核心: 持仓明细追溯
  python3 performance/report_cli.py position --code=515880

  # 每日明细
  python3 performance/report_cli.py daily --date=2026-09-17

  # 合同编号聚合
  python3 performance/report_cli.py contract --contract-no=20260917002

  # 月度汇总
  python3 performance/report_cli.py monthly --month=2026-09

  # 做T vs 普通对比
  python3 performance/report_cli.py t0-stats --start=2026-09-01 --end=2026-09-17

  # 信号来源胜率
  python3 performance/report_cli.py signal-stats --start=2026-09-01 --end=2026-09-17

  # 资金曲线
  python3 performance/report_cli.py equity --start=2026-09-01 --end=2026-09-17

  # HTML 仪表盘
  python3 performance/report_cli.py dashboard --start=2026-09-01 --end=2026-09-17 --output=report.html

  # 对账
  python3 performance/report_cli.py reconcile --date=2026-09-17

  # 数据迁移 (JSON → MySQL)
  python3 performance/report_cli.py migrate
"""

import argparse
import os
import sys
from datetime import date

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from performance.analyzer import get_analyzer

_LINE = '─' * 67
_DLINE = '═' * 67

# 分类中文名
_CAT_CN = {'normal': '普通', 't0': '做T', 'grid': '网格', 'sell_core': '卖底仓'}


def _fmt(v, n=2, sign=False):
    """None-safe 数值格式化"""
    if v is None:
        return '-'
    s = f"{v:+,.{n}f}" if sign else f"{v:,.{n}f}"
    return s


# ══════════════════════════════════════════════
# position — 持仓明细追溯（★核心）
# ══════════════════════════════════════════════

def cmd_position(args):
    az = get_analyzer()
    r = az.position_cost_trace(args.code)

    print(_DLINE)
    print(f"  持仓明细追溯: {r['code']} {r['name']}")
    print(_DLINE)

    pos = r['position']
    if pos:
        print("当前持仓：")
        print(f"  股数:            {pos['shares']:,}")
        print(f"  持仓均价:        {pos['avg_cost']}")
        print(f"  建仓均价:        {pos['entry_avg_cost']}")
        print(f"  首次建仓日期:    {pos['first_buy_date'] or '-'}")
        if pos['peak_price'] is not None:
            print(f"  峰值价格:        {pos['peak_price']}")
        if pos['trailing_stop_price'] is not None:
            print(f"  移动止盈线:      {pos['trailing_stop_price']}")
        if pos['base_price'] is not None:
            print(f"  网格基准价:      {pos['base_price']}")
        ladder = (f"2% {'✅' if pos['reached_2pct'] else '⬜'} / "
                  f"4% {'✅' if pos['reached_4pct'] else '⬜'} / "
                  f"6% {'✅' if pos['reached_6pct'] else '⬜'}")
        print(f"  阶梯止盈进度:    {ladder}")
    else:
        print("当前持仓: 无 position_state 记录（仅交易明细）")

    # 买入明细
    print(_LINE)
    print(f"买入明细 ({len(r['buys'])} 笔, 合计 {r['summary']['total_bought']:,} 股)")
    print(_LINE)
    if r['buys']:
        print(f"{'日期':<12}{'股数':>10}{'价格':>8}{'金额':>12}    "
              f"{'信号来源':<12}{'分类':<6}{'合同编号'}")
        for b in r['buys']:
            print(f"{b['trade_date']:<12}{b['shares']:>10,}{b['price']:>8} "
                  f"{_fmt(b['amount']):>12}    {b['signal_source'] or '-':<12}"
                  f"{_CAT_CN.get(b['trade_category'], b['trade_category']):<6}"
                  f"{b['contract_no'] or '-'}")
        print(_LINE)
        print(f"加权平均成本: {r['summary']['weighted_avg_cost']}")
    else:
        print("  （无买入记录）")

    # 卖出明细
    print(_LINE)
    print(f"卖出明细 ({len(r['sells'])} 笔, 合计 {r['summary']['total_sold']:,} 股)")
    print(_LINE)
    if r['sells']:
        print(f"{'日期':<12}{'股数':>10}{'价格':>8}{'盈亏':>10}{'盈亏%':>8}  "
              f"{'持仓天数':>6}  {'信号来源':<12}{'建仓均价':>8}")
        for s in r['sells']:
            pnl_s = f"{s['pnl']:+,.2f}" if s['pnl'] is not None else '-'
            pct_s = f"{(s['pnl_pct'] or 0) * 100:+.2f}%" if s['pnl_pct'] is not None else '-'
            hd = f"{s['hold_days']}天" if s['hold_days'] is not None else '-'
            print(f"{s['trade_date']:<12}{s['shares']:>10,}{s['price']:>8}{pnl_s:>10}"
                  f"{pct_s:>8}  {hd:>6}  {s['signal_source'] or '-':<12}{s['entry_cost'] or '-':>8}")
    else:
        print("  （无卖出记录）")

    print(_LINE)
    total_pnl = r['summary']['total_pnl'] or 0
    print(f"累计已实现盈亏: {total_pnl:+,.2f}")
    print(f"累计手续费:     {_fmt(r['summary']['total_fee'])}")
    print(_DLINE)


# ══════════════════════════════════════════════
# daily — 每日明细
# ══════════════════════════════════════════════

def cmd_daily(args):
    d = args.date
    r = get_analyzer().daily_report(d)

    print(_DLINE)
    print(f"  每日交易明细: {d}")
    print(_DLINE)
    t = r['totals']
    print(f"总笔数: {t['total_trades']}  买入: {t['buy_count']}  卖出: {t['sell_count']}")
    print(f"买入金额: {t['total_buy_amount']:,.2f}  卖出金额: {t['total_sell_amount']:,.2f}")
    print(f"已实现盈亏: {t['realized_pnl']:+,.2f}  手续费: {t['total_fee']:,.2f}")

    if r['by_category']:
        print(_LINE)
        print("分类汇总:")
        for c in r['by_category']:
            print(f"  {_CAT_CN.get(c['category'], c['category']):<6} "
                  f"笔数{c['count']:>4} (买{c['buy_count']}/卖{c['sell_count']})  "
                  f"金额{c['amount']:>12,.2f}  盈亏{c['pnl']:>+10,.2f}")

    print(_LINE)
    if r['trades']:
        print(f"{'时间':<10}{'方向':<5}{'代码':<9}{'股数':>10}{'价格':>8}"
              f"{'金额':>12}{'盈亏':>10}  {'信号来源':<12}{'分类'}")
        for x in r['trades']:
            pnl_s = f"{x['pnl']:+,.2f}" if x['direction'] == 'sell' else '-'
            print(f"{x['trade_time'] or '-':<10}"
                  f"{'买入' if x['direction'] == 'buy' else '卖出':<5}{x['code']:<9}"
                  f"{x['shares']:>10,}{x['price']:>8}{_fmt(x['amount']):>12}{pnl_s:>10}  "
                  f"{x['signal_source'] or '-':<12}"
                  f"{_CAT_CN.get(x['trade_category'], x['trade_category'])}")
    print(_DLINE)


# ══════════════════════════════════════════════
# contract — 合同编号聚合
# ══════════════════════════════════════════════

def cmd_contract(args):
    r = get_analyzer().contract_detail(args.contract_no)
    print(_DLINE)
    print(f"  合同编号明细: {args.contract_no}")
    print(_DLINE)
    if r['entrust']:
        e = r['entrust']
        print(f"委托: {e['code']} {e['name']} {e['direction']} "
              f"{e['order_shares']:,}股 @ {e['order_price']}  状态: {e['status']}")
        print(f"      已成交: {e['filled_shares'] or 0:,}股  "
              f"加权均价: {e['avg_fill_price'] or '-'}  手续费: {e['total_fee'] or 0}")
    else:
        print("  （entrusts 表无此合同编号）")

    agg = r['agg']
    if agg and agg['deal_count']:
        print(f"成交聚合: {agg['deal_count']}笔 共{agg['total_shares']:,}股 "
              f"金额{agg['total_amount']:,.2f} 均价{agg['avg_fill_price']}")
        print(_LINE)
        for d in r['deals']:
            print(f"  {d['deal_time'] or '-'}  {d['deal_no']}  {d['direction']}  "
                  f"{d['deal_shares']:,}股 @ {d['deal_price']}  "
                  f"{_fmt(d['deal_amount'])}  费{_fmt(d['fee'])}")
    else:
        print("  （deals 表无此合同的成交）")
    print(_DLINE)


# ══════════════════════════════════════════════
# monthly — 月度汇总
# ══════════════════════════════════════════════

def cmd_monthly(args):
    r = get_analyzer().monthly_summary(args.month)
    print(_DLINE)
    print(f"  月度绩效: {args.month}")
    print(_DLINE)
    o = r['overall']
    print(f"总交易: {o['total_trades']}  卖出: {o['sell_trades']}  "
          f"胜率: {o['win_rate'] * 100:.1f}%  盈亏比: {o['profit_factor']}")
    print(f"总盈亏: {o['total_pnl']:+,.2f}  手续费: {o['total_fee']:,.2f}  "
          f"净盈亏: {o['net_pnl']:+,.2f}")

    if r['by_code']:
        print(_LINE)
        print(f"{'代码':<9}{'名称':<14}{'笔数':>5}{'盈亏':>12}{'胜率':>8}")
        for c in r['by_code']:
            print(f"{c['code']:<9}{c['name'][:12]:<14}{c['trades']:>5}"
                  f"{c['pnl']:>+12,.2f}{c['win_rate'] * 100:>7.1f}%")

    if r['by_signal']:
        print(_LINE)
        print("信号来源:")
        for s in r['by_signal']:
            print(f"  {s['signal_source']:<12} {s['sell_count']}笔  "
                  f"胜率{s['win_rate'] * 100:>5.1f}%  "
                  f"盈亏{s['total_pnl']:>+10,.2f}  平均{s['avg_pnl']:>+8,.2f}")
    print(_DLINE)


# ══════════════════════════════════════════════
# t0-stats — 做T vs 普通对比
# ══════════════════════════════════════════════

def cmd_t0_stats(args):
    r = get_analyzer().t0_vs_normal_stats(args.start, args.end)
    print(_DLINE)
    print(f"  做T / 普通 / 网格 对比: {args.start} ~ {args.end}")
    print(_DLINE)
    print(f"{'分类':<8}{'笔数':>6}{'买/卖':>10}{'买入金额':>14}{'卖出金额':>14}"
          f"{'已实现盈亏':>14}{'配对盈亏':>12}{'胜率':>8}")
    for c in r['categories']:
        print(f"{_CAT_CN.get(c['category'], c['category']):<8}{c['trades']:>6}"
              f"{c['buy_count']}/{c['sell_count']:>6}"
              f"{c['buy_amount']:>14,.2f}{c['sell_amount']:>14,.2f}"
              f"{c['pnl']:>+14,.2f}{c['pair_pnl']:>+12,.2f}"
              f"{c['win_rate'] * 100:>7.1f}%")
    print(_DLINE)


# ══════════════════════════════════════════════
# signal-stats — 信号来源胜率
# ══════════════════════════════════════════════

def cmd_signal_stats(args):
    rows = get_analyzer().signal_source_stats(args.start, args.end)
    print(_DLINE)
    print(f"  信号来源胜率: {args.start} ~ {args.end}（卖出交易）")
    print(_DLINE)
    print(f"{'信号来源':<16}{'笔数':>6}{'盈/亏':>10}{'胜率':>8}"
          f"{'总盈亏':>12}{'平均盈亏':>12}")
    for s in rows:
        print(f"{s['signal_source']:<16}{s['sell_count']:>6}"
              f"{s['win_count']}/{s['loss_count']:>6}{s['win_rate'] * 100:>7.1f}%"
              f"{s['total_pnl']:>+12,.2f}{s['avg_pnl']:>+12,.2f}")
    if not rows:
        print("  （区间内无带 signal_source 的卖出记录）")
    print(_DLINE)


# ══════════════════════════════════════════════
# equity — 资金曲线
# ══════════════════════════════════════════════

def cmd_equity(args):
    r = get_analyzer().equity_curve(args.start, args.end)
    print(_DLINE)
    print(f"  资金曲线: {args.start} ~ {args.end}")
    print(_DLINE)
    print(f"{'日期':<12}{'总资产':>14}{'当日盈亏':>12}{'当日收益率':>12}{'累计收益率':>12}")
    for i, d in enumerate(r['dates']):
        dr = r['daily_returns'][i] if i < len(r['daily_returns']) else None
        cr = (r['total_assets'][i] / r['total_assets'][0] - 1) \
            if r['total_assets'] and r['total_assets'][0] else None
        print(f"{d:<12}{r['total_assets'][i]:>14,.2f}"
              f"{r['daily_pnls'][i] if i < len(r['daily_pnls']) else 0:>+12,.2f}"
              f"{dr * 100 if dr is not None else 0:>+11.2f}%"
              f"{cr * 100 if cr is not None else 0:>+11.2f}%")
    if r['dates']:
        print(_LINE)
        print(f"区间收益率: {r['total_return'] * 100:+.2f}%  "
              f"最大回撤: {r['max_drawdown'] * 100:.2f}%")
    else:
        print("  （daily_equity 表无数据，先跑 performance/sync_equity.py）")
    print(_DLINE)


# ══════════════════════════════════════════════
# dashboard — HTML 仪表盘
# ══════════════════════════════════════════════

def cmd_dashboard(args):
    from performance.report_html import build_report
    out = build_report(args.start, args.end, args.output)
    # macOS 直接打开
    try:
        import subprocess
        subprocess.run(['open', out], check=False)
    except Exception:
        pass


# ══════════════════════════════════════════════
# reconcile / migrate / init-db
# ══════════════════════════════════════════════

def cmd_reconcile(args):
    from performance.reconcile import reconcile
    reconcile(args.date)


def cmd_migrate(args):
    from performance.migrate import run_migration
    run_migration(dry=args.dry)


def cmd_init_db(args):
    from core.db import init_db
    init_db()
    print("✅ 数据库表已就绪（7张表）")


# ══════════════════════════════════════════════
# argparse
# ══════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='report_cli.py',
        description='ETF量化 · 绩效分析管理端 CLI')
    sub = p.add_subparsers(dest='command', required=True)

    sp = sub.add_parser('position', help='★持仓明细追溯（买入/卖出明细）')
    sp.add_argument('--code', required=True, help='ETF代码, 如 515880')
    sp.set_defaults(func=cmd_position)

    sp = sub.add_parser('daily', help='每日交易明细+分类汇总')
    sp.add_argument('--date', default=date.today().isoformat(), help='YYYY-MM-DD')
    sp.set_defaults(func=cmd_daily)

    sp = sub.add_parser('contract', help='合同编号聚合')
    sp.add_argument('--contract-no', required=True, help='合同编号')
    sp.set_defaults(func=cmd_contract)

    sp = sub.add_parser('monthly', help='月度绩效汇总')
    sp.add_argument('--month', required=True, help='YYYY-MM')
    sp.set_defaults(func=cmd_monthly)

    sp = sub.add_parser('t0-stats', help='做T vs 普通 vs 网格对比')
    sp.add_argument('--start', required=True, help='YYYY-MM-DD')
    sp.add_argument('--end', required=True, help='YYYY-MM-DD')
    sp.set_defaults(func=cmd_t0_stats)

    sp = sub.add_parser('signal-stats', help='信号来源胜率')
    sp.add_argument('--start', required=True, help='YYYY-MM-DD')
    sp.add_argument('--end', required=True, help='YYYY-MM-DD')
    sp.set_defaults(func=cmd_signal_stats)

    sp = sub.add_parser('equity', help='资金曲线')
    sp.add_argument('--start', required=True, help='YYYY-MM-DD')
    sp.add_argument('--end', required=True, help='YYYY-MM-DD')
    sp.set_defaults(func=cmd_equity)

    sp = sub.add_parser('dashboard', help='生成HTML仪表盘')
    sp.add_argument('--start', required=True, help='YYYY-MM-DD')
    sp.add_argument('--end', required=True, help='YYYY-MM-DD')
    sp.add_argument('--output', default=None, help='输出路径, 默认 report.html')
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser('reconcile', help='对账（本地 vs 同花顺）')
    sp.add_argument('--date', default=date.today().isoformat(), help='YYYY-MM-DD')
    sp.set_defaults(func=cmd_reconcile)

    sp = sub.add_parser('migrate', help='JSON数据迁移到MySQL')
    sp.add_argument('--dry', action='store_true', help='只统计不写入')
    sp.set_defaults(func=cmd_migrate)

    sp = sub.add_parser('init-db', help='建表（幂等）')
    sp.set_defaults(func=cmd_init_db)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
