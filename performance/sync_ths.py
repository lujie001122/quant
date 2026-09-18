#!/usr/bin/env python3
"""
performance/sync_ths.py — 同花顺委托+成交同步

从 EvolvingSim 拉取当日委托（getEntrust）和成交（getDeal），
写入 MySQL entrusts / deals 表，并回填 entrusts 的加权成交均价。

数据流:
  getEntrust('today') ──解析──▶ EntrustRepo.insert_ignore ──▶ entrusts 表
  getDeal('today')    ──解析──▶ DealRepo.insert_ignore  ──▶ deals 表
                                        │
                                        └─聚合──▶ EntrustRepo.update_fill_info

去重: entrusts.contract_no / deals.deal_no 唯一 + INSERT IGNORE，
      重复同步安全（幂等），可放在 cron 里每几分钟跑一次。

⚠️ 字段映射基于 EvolvingSim 当前的返回格式，如实际列序变化需调整
   _MAP_ENTRUST / _MAP_DEAL 中的 TODO 注释处。

用法:
  python3 performance/sync_ths.py all      # 同步委托+成交
  python3 performance/sync_ths.py entrust  # 仅委托
  python3 performance/sync_ths.py deal     # 仅成交
"""

import sys
import os
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.db import get_session
from core.models import SyncLog
from core.repositories import DealRepo, EntrustRepo, SyncLogRepo


# ══════════════════════════════════════════════
# EvolvingSim 拉取
# ══════════════════════════════════════════════

def _call_evolving(method_name: str, *args):
    """调用 EvolvingSim 方法（懒加载，同花顺未开时返回 None）"""
    try:
        from evolving.evolving import EvolvingSim
        e = EvolvingSim()
        return getattr(e, method_name)(*args)
    except Exception as ex:
        print(f"  ⚠️ EvolvingSim.{method_name} 异常: {ex}")
        return None


# ══════════════════════════════════════════════
# 解析
# ══════════════════════════════════════════════

def _to_int(v) -> Optional[int]:
    try:
        return int(float(str(v).replace(',', '')))
    except (ValueError, TypeError):
        return None


def _to_dec(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v).replace(',', ''))
    except Exception:
        return None


def _parse_entrust_row(row: list) -> Optional[dict]:
    """解析一行委托记录 → entrusts 表字段

    当前映射（TODO: 如 EvolvingSim 返回格式变化在此调整）:
      row[0]=合同编号  row[2]=代码  row[4]=方向(买入/卖出)
      row[6]=委托价    row[7]=委托量 row[9]=状态  row[10]=时间
    """
    try:
        if not row or len(row) < 11:
            return None
        contract_no = str(row[0]).strip()
        if not contract_no or contract_no in ('-', ''):
            return None
        order_time = None
        try:
            order_time = datetime.strptime(str(row[10]), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            try:
                order_time = datetime.strptime(str(row[10]), "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                order_time = None

        direction = str(row[4]).strip()
        if direction not in ('买入', '卖出'):
            return None

        return {
            'contract_no': contract_no,
            'code': str(row[2]).strip(),
            'direction': direction,
            'order_price': _to_dec(row[6]),
            'order_shares': _to_int(row[7]),
            'status': str(row[9]).strip(),
            'order_time': order_time,
        }
    except Exception:
        return None


def _parse_deal_row(row: list) -> Optional[dict]:
    """解析一行成交记录 → deals 表字段

    当前映射（TODO: 如 EvolvingSim 返回格式变化在此调整）:
      row[0]=成交编号  row[1]=合同编号  row[2]=代码  row[3]=方向
      row[4]=成交价    row[5]=成交量    row[6]=手续费 row[7]=时间
    """
    try:
        if not row or len(row) < 8:
            return None
        deal_no = str(row[0]).strip()
        if not deal_no or deal_no in ('-', ''):
            return None
        direction = str(row[3]).strip()
        if direction not in ('买入', '卖出'):
            return None

        deal_time = None
        try:
            deal_time = datetime.strptime(str(row[7]), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            try:
                deal_time = datetime.strptime(str(row[7]), "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                deal_time = None

        price = _to_dec(row[4])
        shares = _to_int(row[5])
        amount = _to_dec(row[6]) if False else None  # 手续费在 row[6]，金额用 价x量 计算
        if price is not None and shares:
            amount = (price * shares).quantize(Decimal('0.01'))

        return {
            'deal_no': deal_no,
            'contract_no': str(row[1]).strip() or None,
            'code': str(row[2]).strip(),
            'direction': direction,
            'deal_price': price,
            'deal_shares': shares,
            'deal_amount': amount,
            'fee': _to_dec(row[6]),
            'deal_time': deal_time,
        }
    except Exception:
        return None


# ══════════════════════════════════════════════
# 同步主逻辑
# ══════════════════════════════════════════════

def sync_entrusts() -> dict:
    """同步当日委托 → entrusts 表（INSERT IGNORE 去重）

    返回: {total, inserted, failed}
    """
    started = datetime.now()
    log_id = SyncLogRepo.insert({
        'sync_type': 'entrust', 'sync_date': started.date(),
        'started_at': started,
    })

    result = {'total': 0, 'inserted': 0, 'failed': 0}
    try:
        resp = _call_evolving('getEntrust', 'today')
        rows = []
        if isinstance(resp, dict) and resp.get('status'):
            rows = resp.get('data', []) or []
        elif isinstance(resp, list):
            rows = resp

        result['total'] = len(rows)
        for row in rows:
            data = _parse_entrust_row(row)
            if data is None:
                result['failed'] += 1
                continue
            n = EntrustRepo.insert_ignore(data)
            result['inserted'] += n

        SyncLogRepo.update(log_id, {
            'total_records': result['total'],
            'inserted': result['inserted'],
            'failed': result['failed'],
            'finished_at': datetime.now(),
        })
        print(f"✅ 委托同步: 拉取{result['total']} 新增{result['inserted']} 失败{result['failed']}")
    except Exception as ex:
        SyncLogRepo.update(log_id, {
            'total_records': result['total'],
            'failed': result['failed'],
            'error_msg': str(ex)[:2000],
            'finished_at': datetime.now(),
        })
        print(f"❌ 委托同步失败: {ex}")
    return result


def sync_deals() -> dict:
    """同步当日成交 → deals 表，并回填 entrusts 加权均价

    返回: {total, inserted, failed, contracts_updated}
    """
    started = datetime.now()
    log_id = SyncLogRepo.insert({
        'sync_type': 'deal', 'sync_date': started.date(),
        'started_at': started,
    })

    result = {'total': 0, 'inserted': 0, 'failed': 0, 'contracts_updated': 0}
    try:
        resp = _call_evolving('getClosedDeals', 'today')
        rows = []
        if isinstance(resp, dict) and resp.get('status'):
            rows = resp.get('data', []) or []
        elif isinstance(resp, list):
            rows = resp

        result['total'] = len(rows)
        touched_contracts = set()
        for row in rows:
            data = _parse_deal_row(row)
            if data is None:
                result['failed'] += 1
                continue
            n = DealRepo.insert_ignore(data)
            result['inserted'] += n
            if data.get('contract_no'):
                touched_contracts.add(data['contract_no'])

        # 回填 entrusts: 加权均价 / 已成交数量 / 总手续费
        for cno in touched_contracts:
            agg = DealRepo.aggregate_by_contract(cno)
            if agg and agg['total_shares']:
                ent = EntrustRepo.get_by_contract(cno)
                status = ent.status if ent else None
                # 状态推断: 成交量=委托量 → 已成，否则部成
                if ent and ent.order_shares and agg['total_shares'] >= ent.order_shares:
                    status = '已成'
                elif ent:
                    status = status or '部成'
                EntrustRepo.update_fill_info(
                    cno, filled_shares=agg['total_shares'],
                    avg_fill_price=agg['avg_fill_price'],
                    total_fee=agg['total_fee'], status=status)
                result['contracts_updated'] += 1

        SyncLogRepo.update(log_id, {
            'total_records': result['total'],
            'inserted': result['inserted'],
            'failed': result['failed'],
            'finished_at': datetime.now(),
        })
        print(f"✅ 成交同步: 拉取{result['total']} 新增{result['inserted']} "
              f"回填委托{result['contracts_updated']} 失败{result['failed']}")
    except Exception as ex:
        SyncLogRepo.update(log_id, {
            'total_records': result['total'],
            'failed': result['failed'],
            'error_msg': str(ex)[:2000],
            'finished_at': datetime.now(),
        })
        print(f"❌ 成交同步失败: {ex}")
    return result


def sync_all() -> dict:
    """同步委托 + 成交"""
    e = sync_entrusts()
    d = sync_deals()
    return {'entrust': e, 'deal': d}


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if cmd == 'entrust':
        sync_entrusts()
    elif cmd == 'deal':
        sync_deals()
    else:
        sync_all()
