#!/usr/bin/env python3
"""
performance/sync_equity.py — 每日净值同步

每日收盘后把 portfolio.json 的 account 段写入 daily_equity 表，
作为资金曲线的数据源。可加入 cron:

  # 每个交易日 15:30 同步净值
  30 15 * * 1-5 cd /path/to/quant && .venv/bin/python3 performance/sync_equity.py

计算字段:
  daily_pnl       = 今日总资产 - 昨日总资产
  daily_return    = daily_pnl / 昨日总资产
  cumulative_return = 总资产 / 首日总资产 - 1
  max_drawdown    = 历史峰值回撤

用法:
  python3 performance/sync_equity.py          # 同步今天
  python3 performance/sync_equity.py 2026-09-17
"""

import json
import os
import sys
from datetime import date, datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.repositories import DailyEquityRepo, SyncLogRepo

PF_PATH = os.path.join(_PROJECT_ROOT, 'portfolio.json')


def _load_account() -> dict:
    """读取 portfolio.json 的 account 段"""
    with open(PF_PATH, 'r', encoding='utf-8') as f:
        pf = json.load(f)
    acct = pf.get('account', {}) or {}
    return {
        'total_asset': float(acct.get('total_asset', 0) or 0),
        'cash': float(acct.get('cash', 0) or 0),
        'market_value': float(acct.get('market_value', 0) or 0),
    }


def sync_equity(date_str: str = None) -> dict:
    """同步净值到 daily_equity 表（按 trade_date upsert，幂等）

    参数: date_str 'YYYY-MM-DD'（默认今天）
    """
    d = date.fromisoformat(date_str) if date_str else date.today()
    started = datetime.now()
    log_id = SyncLogRepo.insert({
        'sync_type': 'equity', 'sync_date': d, 'started_at': started})

    try:
        acct = _load_account()
        if not acct['total_asset']:
            raise ValueError("portfolio.json account.total_asset 为空，先跑 trade.py sync")

        # 取历史净值计算衍生指标
        history = DailyEquityRepo.range(end=d)
        prev = history[-1] if history else None
        prev_asset = float(prev.total_asset) if prev else None

        daily_pnl = None
        daily_return = None
        if prev_asset:
            daily_pnl = round(acct['total_asset'] - prev_asset, 2)
            daily_return = round(daily_pnl / prev_asset, 4) if prev_asset else None

        # 累计收益率与最大回撤（含今天）
        assets = [float(r.total_asset) for r in history if r.trade_date != d]
        first_asset = assets[0] if assets else acct['total_asset']
        assets.append(acct['total_asset'])
        cumulative_return = (acct['total_asset'] / first_asset - 1) if first_asset else None

        peak, max_dd = 0.0, 0.0
        for a in assets:
            peak = max(peak, a)
            if peak > 0:
                max_dd = min(max_dd, (a - peak) / peak)

        DailyEquityRepo.upsert({
            'trade_date': d,
            'total_asset': acct['total_asset'],
            'cash': acct['cash'],
            'market_value': acct['market_value'],
            'daily_pnl': daily_pnl,
            'daily_return': daily_return,
            'cumulative_return': round(cumulative_return, 4) if cumulative_return is not None else None,
            'max_drawdown': round(max_dd, 4),
        })

        SyncLogRepo.update(log_id, {
            'total_records': 1, 'inserted': 1,
            'finished_at': datetime.now()})
        print(f"✅ 净值同步 {d}: 总资产{acct['total_asset']:.2f} "
              f"现金{acct['cash']:.2f} 市值{acct['market_value']:.2f}")
        return {'date': d.isoformat(), 'total_asset': acct['total_asset']}
    except Exception as ex:
        SyncLogRepo.update(log_id, {
            'failed': 1, 'error_msg': str(ex)[:2000],
            'finished_at': datetime.now()})
        print(f"❌ 净值同步失败: {ex}")
        raise


if __name__ == '__main__':
    sync_equity(sys.argv[1] if len(sys.argv) > 1 else None)
