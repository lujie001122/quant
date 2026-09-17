#!/usr/bin/env python3
"""
performance/reconcile.py — 对账

比较本地 position_state 表（本地系统持仓）与同花顺实际持仓
（EvolvingSim.getHoldingShares），逐ETF比较股数，结果写入
reconcile_log 表。

四类结果:
  match        两边持仓一致
  mismatch     两边都有但股数不同
  local_only   仅本地有（实际已卖出/转出）
  actual_only  仅实际有（本地记录丢失/手动买入）

用法:
  python3 performance/reconcile.py            # 对账今天
  python3 performance/reconcile.py 2026-09-17
"""

import sys
import os
from datetime import date, datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.repositories import PositionStateRepo, ReconcileLogRepo, SyncLogRepo


def _fetch_actual_positions() -> dict:
    """拉取同花顺实际持仓 {code: shares}"""
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()
        h = e.getHoldingShares()
        result = {}
        if isinstance(h, dict) and h.get('status'):
            for row in h.get('data', []) or []:
                # TODO: 列序按 EvolvingSim 实际返回调整（row[0]=代码, row[6]=股数）
                if not row or len(row) < 7:
                    continue
                try:
                    shares = int(row[6])
                except (ValueError, TypeError):
                    continue
                if shares > 0:
                    result[str(row[0]).strip()] = shares
        return result
    except Exception as ex:
        print(f"  ⚠️ 拉取同花顺持仓失败: {ex}")
        return {}


def reconcile(date_str: str = None) -> dict:
    """对账: 本地 position_state vs 同花顺实际持仓

    参数: date_str 'YYYY-MM-DD'（默认今天）
    返回: {match: [...], mismatch: [...], local_only: [...], actual_only: [...]}
    """
    d = date.fromisoformat(date_str) if date_str else date.today()
    started = datetime.now()
    log_id = SyncLogRepo.insert({
        'sync_type': 'reconcile', 'sync_date': d, 'started_at': started})

    local = {p.code: int(p.shares or 0)
             for p in PositionStateRepo.list_active()}
    actual = _fetch_actual_positions()

    result = {'match': [], 'mismatch': [], 'local_only': [], 'actual_only': []}

    try:
        all_codes = sorted(set(local) | set(actual))
        for code in all_codes:
            l_shares = local.get(code)
            a_shares = actual.get(code)

            if l_shares is not None and a_shares is not None:
                if l_shares == a_shares:
                    status = 'match'
                    result['match'].append(code)
                else:
                    status = 'mismatch'
                    result['mismatch'].append({
                        'code': code, 'local_shares': l_shares,
                        'actual_shares': a_shares,
                        'diff': l_shares - a_shares})
            elif l_shares is not None:
                status = 'local_only'
                result['local_only'].append({'code': code, 'local_shares': l_shares})
            else:
                status = 'actual_only'
                result['actual_only'].append({'code': code, 'actual_shares': a_shares})

            ReconcileLogRepo.insert({
                'reconcile_date': d,
                'code': code,
                'local_shares': l_shares,
                'actual_shares': a_shares,
                'diff_shares': (l_shares - a_shares)
                               if (l_shares is not None and a_shares is not None) else None,
                'status': status,
                'detail': {'local': l_shares, 'actual': a_shares},
            })

        SyncLogRepo.update(log_id, {
            'total_records': len(all_codes),
            'inserted': len(all_codes),
            'finished_at': datetime.now()})
    except Exception as ex:
        SyncLogRepo.update(log_id, {
            'failed': 1, 'error_msg': str(ex)[:2000],
            'finished_at': datetime.now()})
        raise

    # 打印摘要
    print(f"═══ 对账 {d} ═══")
    print(f"  ✅ 一致:   {len(result['match'])}只 {result['match']}")
    if result['mismatch']:
        print(f"  ⚠️ 股数不符: {len(result['mismatch'])}只")
        for m in result['mismatch']:
            print(f"     {m['code']}: 本地{m['local_shares']:,} vs "
                  f"实际{m['actual_shares']:,} (差{m['diff']:+,})")
    if result['local_only']:
        print(f"  ⚠️ 仅本地: {[x['code'] for x in result['local_only']]}")
    if result['actual_only']:
        print(f"  ⚠️ 仅实际: {[x['code'] for x in result['actual_only']]}")
    return result


if __name__ == '__main__':
    reconcile(sys.argv[1] if len(sys.argv) > 1 else None)
