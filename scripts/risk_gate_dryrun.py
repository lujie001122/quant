#!/usr/bin/env python3
"""实盘风控闸门实况试算（只读：不下单、不写订单、不写风控状态）。

用当前真实 portfolio.json + 实时行情，按实盘下单金额口径算一遍三道闸门，
确认"接线后今天会不会被误拦"。

用法: .venv/bin/python3 scripts/risk_gate_dryrun.py
"""
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, REPO_DIR)

import risk_manager
from state_center import load_portfolio
from core.position_manager import calc_order_amount


def main():
    today_str = datetime.now().strftime('%Y-%m-%d')
    pf = load_portfolio() or {}
    positions = pf.get('positions') or {}
    total_asset = float((pf.get('account') or {}).get('total_asset') or 0)
    cfg = risk_manager.risk_config()

    print("=" * 72)
    print(f"实盘风控闸门试算  date={today_str}  总资产={total_asset:,.2f}  持仓={len(positions)} 只")
    print(f"阈值(config.yaml risk): 日亏限额 -{cfg['daily_loss_limit']*100:.1f}%  "
          f"连亏熔断 {cfg['consecutive_loss_count']} 笔  "
          f"冷却 {cfg['cooldown_days']} 天  单票上限 {cfg['single_position_cap']*100:.0f}%")
    print("=" * 72)

    quotes = {}
    try:
        from market_data import fetch_realtime_quotes
        quotes = fetch_realtime_quotes(list(positions.keys())) or {}
        print(f"[行情] 实时行情 {len(quotes)} 只（含昨收，用于当日浮动盈亏）")
    except Exception as ex:
        print(f"[行情] ⚠️ 实时行情获取失败({ex})，浮动盈亏按风控状态里最后一次记录计算")

    for code, pos in positions.items():
        mv = float(pos.get('market_value') or 0)
        print(f"  持仓 {code} {pos.get('name', '')} {pos.get('shares')}股 "
              f"市值={mv:,.0f} 占比={mv/total_asset*100 if total_asset else 0:.1f}%")

    info = risk_manager.get_daily_loss_info(today_str, quotes=quotes, total_asset=total_asset)
    cl = risk_manager.get_consecutive_loss_info(today_str)
    print(f"\n[状态] 当日已实现 {info['realized']:+.0f} 浮动 {info['floating']:+.0f} "
          f"= {info['daily_pnl']:+.0f} 元（{info['pct']*100:+.2f}%）")
    print(f"[状态] 连亏 {cl['consecutive_losses']}/{cl['consecutive_loss_count']} 笔，"
          f"熔断截止='{cl['circuit_breaker_until'] or '无'}'")

    blocked = []
    print("\n" + "-" * 72)
    print("模拟买入（实盘下单金额口径：总资产×80%÷3×30%）")
    print("-" * 72)
    for code, pos in positions.items():
        price = float((quotes.get(code) or {}).get('price') or pos.get('current_price') or 0)
        if price <= 0:
            continue
        plan = calc_order_amount(total_asset, 0.30, price=price)
        res = risk_manager.pre_trade_gate(
            code, price, plan['shares'], trade_type='buy', total_asset=total_asset,
            quotes=quotes, today_str=today_str)
        if not res['ok']:
            blocked.append((code, res['reason']))

    print("\n" + "-" * 72)
    print("模拟新开仓（监控池中未持仓标的，按 3 万元一笔）")
    print("-" * 72)
    try:
        import json
        with open(os.path.join(REPO_DIR, 'etf_pool.json'), encoding='utf-8') as f:
            pool = json.load(f).get('etf_pool') or []
    except Exception:
        pool = []
    candidates = [c for c in pool if c not in positions][:2]
    if not candidates:
        print("  （监控池无未持仓标的，跳过）")
    for code in candidates:
        try:
            from market_data import fetch_realtime_quotes
            q = fetch_realtime_quotes([code]).get(code) or {}
        except Exception:
            q = {}
        price = float(q.get('price') or 0)
        if price <= 0:
            print(f"  {code} 无实时价，跳过")
            continue
        q = dict(quotes, **{code: q})
        shares = max(int(30000 / price / 100) * 100, 100)
        res = risk_manager.pre_trade_gate(code, price, shares, trade_type='buy',
                                          total_asset=total_asset, quotes=q,
                                          today_str=today_str)
        if not res['ok']:
            blocked.append((code, res['reason']))

    print("\n" + "=" * 72)
    if blocked:
        print(f"[RISK] 结论: 今日有 {len(blocked)} 笔会被拦下")
        for code, reason in blocked:
            print(f"        {code}: {reason}")
    else:
        print("[RISK] 结论: 今日全部放行，接线不会误拦现有实盘交易")
    print("（本次试算只读：未下单、未写订单/intent/风控状态）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
