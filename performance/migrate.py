#!/usr/bin/env python3
"""
performance/migrate.py — 一次性把现有 JSON 数据迁移到 MySQL

迁移内容:
  1. data/trades/trades_*.json → trades 表
     （旧格式字段: date/time/code/direction/shares/price/amount/fee/pnl/tag/account）
  2. portfolio.json 的 _signal_state → position_state 表
  3. orders/*.json → entrusts 表（旧格式: code/action/shares/price/contract/status/created_at）

迁移完成后 JSON 文件移到 archive/ 目录（不删除）。

用法:
  python3 performance/migrate.py          # 执行迁移
  python3 performance/migrate.py --dry    # 只统计不写入
"""

import json
import os
import shutil
import sys
from datetime import date, datetime
from decimal import Decimal

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.db import get_session
from core.models import Trade
from core.repositories import EntrustRepo, PositionStateRepo, TradeRepo

DATA_TRADES_DIR = os.path.join(_PROJECT_ROOT, 'data', 'trades')
ORDERS_DIR = os.path.join(_PROJECT_ROOT, 'orders')
PF_PATH = os.path.join(_PROJECT_ROOT, 'portfolio.json')
ARCHIVE_DIR = os.path.join(_PROJECT_ROOT, 'archive')


def _to_dec(v):
    try:
        return Decimal(str(v))
    except Exception:
        return None


# ══════════════════════════════════════════════
# 1. trades JSON → trades 表
# ══════════════════════════════════════════════

def migrate_trades(dry: bool = False) -> dict:
    """data/trades/trades_*.json → trades 表

    旧JSON没有 signal_source 等新字段，tag 映射到 trade_category:
      t0→t0, grid→grid, 其他→normal
    """
    stats = {'files': 0, 'records': 0, 'migrated': 0, 'skipped': 0}
    if not os.path.isdir(DATA_TRADES_DIR):
        return stats

    for fname in sorted(os.listdir(DATA_TRADES_DIR)):
        if not fname.startswith('trades_') or not fname.endswith('.json'):
            continue
        # 跳过备份文件
        if '.bak' in fname:
            continue
        stats['files'] += 1
        path = os.path.join(DATA_TRADES_DIR, fname)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                records = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(records, list):
            continue

        for r in records:
            stats['records'] += 1
            if dry:
                stats['migrated'] += 1
                continue
            try:
                t_date = date.fromisoformat(str(r.get('date', ''))[:10])
            except ValueError:
                stats['skipped'] += 1
                continue

            tag = r.get('tag', '') or ''
            category = tag if tag in ('t0', 'grid') else 'normal'
            direction = str(r.get('direction', 'buy')).lower()
            if direction not in ('buy', 'sell'):
                stats['skipped'] += 1
                continue

            t_time = None
            try:
                t_time = datetime.strptime(str(r.get('time', '')), '%H:%M:%S').time()
            except (ValueError, TypeError):
                pass

            shares = int(r.get('shares', 0) or 0)
            price = _to_dec(r.get('price'))
            amount = _to_dec(r.get('amount'))
            if amount is None and price is not None:
                amount = (price * shares).quantize(Decimal('0.01'))

            TradeRepo.insert({
                'trade_date': t_date,
                'trade_time': t_time,
                'code': str(r.get('code', '')),
                'name': r.get('name') or '',
                'direction': direction,
                'shares': shares,
                'price': price,
                'amount': amount,
                'fee': _to_dec(r.get('fee', 0)) or Decimal('0'),
                'pnl': _to_dec(r.get('pnl', 0)) or Decimal('0'),
                'trade_category': category,
                'account': r.get('account') or 'main',
            })
            stats['migrated'] += 1

    return stats


# ══════════════════════════════════════════════
# 2. portfolio.json _signal_state → position_state 表
# ══════════════════════════════════════════════

# _signal_state key → position_state 列 的映射
_SIG_FIELDS = {
    'shares': 'shares',
    'avg_cost': 'avg_cost',
    'entry_avg_cost': 'entry_avg_cost',
    'base_price': 'base_price',
    'peak_price': 'peak_price',
    'trailing_stop_price': 'trailing_stop_price',
    'stop_level': 'stop_level',
    'build_phase': 'build_phase',
    'grid_frozen': 'grid_frozen',
    'empty_days': 'empty_days',
    'prev_macd_status': 'prev_macd_status',
    'prev_rsi': 'prev_rsi',
    'daily_trade_log': 'daily_trade_log',
    'liquidate_dates': 'liquidate_dates',
}


def migrate_positions(dry: bool = False) -> dict:
    """portfolio.json _signal_state → position_state 表"""
    stats = {'records': 0, 'migrated': 0}
    if not os.path.exists(PF_PATH):
        return stats
    try:
        with open(PF_PATH, 'r', encoding='utf-8') as f:
            pf = json.load(f)
    except (json.JSONDecodeError, OSError):
        return stats

    positions = pf.get('positions', {}) or {}
    sig_state = pf.get('_signal_state', {}) or {}

    for code, ss in sig_state.items():
        if not isinstance(ss, dict):
            continue
        stats['records'] += 1
        if dry:
            stats['migrated'] += 1
            continue

        data = {'code': code}
        data['name'] = positions.get(code, {}).get('name') or ''
        for src, dst in _SIG_FIELDS.items():
            v = ss.get(src)
            if v is None:
                continue
            if dst in ('avg_cost', 'entry_avg_cost', 'base_price', 'peak_price',
                       'trailing_stop_price', 'prev_rsi'):
                v = _to_dec(v)
            data[dst] = v

        # 阶梯止盈标记（_signal_state 用 reached_15/3/5/8pct，本表用 2/4/6）
        # 映射: 2pct←reached_3pct, 4pct←reached_5pct, 6pct←reached_8pct
        data['reached_2pct'] = bool(ss.get('reached_3pct', False))
        data['reached_4pct'] = bool(ss.get('reached_5pct', False))
        data['reached_6pct'] = bool(ss.get('reached_8pct', False))

        # 冷却期 / 首次建仓日期
        if ss.get('cooldown_until'):
            try:
                data['cooldown_until'] = date.fromisoformat(str(ss['cooldown_until'])[:10])
            except ValueError:
                pass
        if ss.get('build_first_price') and positions.get(code, {}).get('shares', 0) > 0:
            # 无显式首次建仓日期字段，用 confirm_batch_date / 峰值推断，找不到留空
            pass
        first_buy = None
        for k in ('confirm_batch_date', 'empty_days_date'):
            v = ss.get(k)
            if v:
                try:
                    first_buy = date.fromisoformat(str(v)[:10])
                    break
                except ValueError:
                    pass
        if first_buy:
            data['first_buy_date'] = first_buy

        PositionStateRepo.upsert(data)
        stats['migrated'] += 1

    return stats


# ══════════════════════════════════════════════
# 3. orders/*.json → entrusts 表
# ══════════════════════════════════════════════

def migrate_orders(dry: bool = False) -> dict:
    """orders/*.json → entrusts 表（contract 为 pending 的跳过）"""
    stats = {'files': 0, 'migrated': 0, 'skipped': 0}
    if not os.path.isdir(ORDERS_DIR):
        return stats

    for fname in sorted(os.listdir(ORDERS_DIR)):
        if not fname.endswith('.json'):
            continue
        stats['files'] += 1
        path = os.path.join(ORDERS_DIR, fname)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                o = json.load(f)
        except (json.JSONDecodeError, OSError):
            stats['skipped'] += 1
            continue

        contract = str(o.get('contract', '') or '')
        if not contract or contract == 'pending':
            stats['skipped'] += 1
            continue

        order_time = None
        try:
            order_time = datetime.strptime(o.get('created_at', ''),
                                           '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            pass

        action = str(o.get('action', ''))
        if 't0_buy' in action:
            direction, category = '买入', 't0'
        elif 't0_sell' in action:
            direction, category = '卖出', 't0'
        elif action == 'buy':
            direction, category = '买入', 'normal'
        elif action == 'sell':
            direction, category = '卖出', 'normal'
        else:
            stats['skipped'] += 1
            continue

        if dry:
            stats['migrated'] += 1
            continue

        n = EntrustRepo.insert_ignore({
            'contract_no': contract,
            'code': str(o.get('code', '')),
            'direction': direction,
            'order_price': _to_dec(o.get('price')),
            'order_shares': int(o.get('shares', 0) or 0),
            'status': o.get('status') or '已报',
            'trade_category': category,
            'order_time': order_time,
        })
        stats['migrated'] += n

    return stats


# ══════════════════════════════════════════════
# 4. 归档
# ══════════════════════════════════════════════

def archive_files(dry: bool = False) -> int:
    """迁移完成后把 JSON 移到 archive/（不删除）"""
    if dry:
        return 0
    moved = 0
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    for src_dir, pattern in [(DATA_TRADES_DIR, 'trades_*.json'),
                             (ORDERS_DIR, '*.json')]:
        if not os.path.isdir(src_dir):
            continue
        sub = os.path.join(ARCHIVE_DIR, os.path.basename(src_dir))
        os.makedirs(sub, exist_ok=True)
        for fname in os.listdir(src_dir):
            if '.bak' in fname:
                continue
            import fnmatch
            if fnmatch.fnmatch(fname, pattern):
                shutil.move(os.path.join(src_dir, fname),
                            os.path.join(sub, fname))
                moved += 1
    return moved


def run_migration(dry: bool = False, do_archive: bool = True) -> dict:
    """执行全部迁移

    参数:
      dry: 只统计不写入
      do_archive: 迁移成功后把 JSON 移到 archive/
    """
    print(f"═══ JSON → MySQL 迁移{' (DRY RUN)' if dry else ''} ═══")

    t = migrate_trades(dry)
    print(f"  trades:   {t['migrated']}/{t['records']} 条 ({t['files']} 个文件, 跳过{t['skipped']})")

    p = migrate_positions(dry)
    print(f"  positions: {p['migrated']}/{p['records']} 只")

    o = migrate_orders(dry)
    print(f"  entrusts: {o['migrated']} 条 ({o['files']} 个文件, 跳过{o['skipped']})")

    moved = 0
    if not dry and do_archive and t['migrated'] > 0:
        moved = archive_files()
        print(f"  archive:  移动 {moved} 个文件 → archive/")

    return {'trades': t, 'positions': p, 'entrusts': o, 'archived': moved}


if __name__ == '__main__':
    run_migration(dry='--dry' in sys.argv)
