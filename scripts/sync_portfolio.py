#!/usr/bin/env python3
"""同步同花顺持仓到远程MySQL管理端

用法:
  python3 scripts/sync_portfolio.py          # 同步持仓
  python3 scripts/sync_portfolio.py --all    # 持仓+今日交易+清空错误数据

流程:
  1. trade.py sync 获取同花顺最新持仓
  2. 更新 position_state 表
  3. 清空 trades 表（历史成交同花顺不提供API，新交易通过trade_recorder写入）
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
import yaml

# 读配置
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.yaml')) as f:
    config = yaml.safe_load(f)

DB_URL = config['database']['url'].replace('***', 'lujie001122')
engine = create_engine(DB_URL)

def sync_positions():
    """同步持仓到 position_state"""
    # 先读本地portfolio.json
    pf_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'portfolio.json')
    if not os.path.exists(pf_path):
        print("✗ portfolio.json 不存在，先执行 trade.py sync")
        return False
    
    with open(pf_path) as f:
        pf = json.load(f)
    
    positions = pf.get('positions', {})
    signal_state = pf.get('_signal_state', {})
    account = pf.get('account', {})
    
    with engine.connect() as conn:
        # 清空旧数据
        conn.execute(text('DELETE FROM position_state'))
        conn.execute(text('DELETE FROM trades'))
        
        count = 0
        for code, p in positions.items():
            shares = p.get('shares', 0)
            if shares <= 0:
                continue
            
            name = p.get('name', '')
            avg_cost = p.get('avg_cost', 0)
            cur_price = p.get('current_price', 0)
            
            s = signal_state.get(code, {})
            entry = s.get('entry_avg_cost', avg_cost)
            peak = s.get('peak_price', cur_price)
            trail = s.get('trailing_stop_price', 0) or 0
            stop_lv = s.get('stop_level', 0)
            build = s.get('build_phase', 0)
            fb = s.get('first_buy_date', datetime.now().strftime('%Y-%m-%d'))
            
            core = int(shares * 0.3)
            
            conn.execute(text(
                "INSERT INTO position_state (code, name, shares, core_shares, grid_shares, active_shares, avg_cost, entry_avg_cost, peak_price, trailing_stop_price, stop_level, build_phase, reached_2pct, reached_4pct, reached_6pct, grid_frozen, empty_days, prev_macd_status, first_buy_date, updated_at) VALUES (:code, :name, :shares, :core, 0, 0, :avg, :entry, :peak, :trail, :stop, :build, 0, 0, 0, 0, 0, '', :fb, NOW())"
            ), {
                "code": code, "name": name, "shares": shares, "core": core,
                "avg": avg_cost, "entry": entry, "peak": peak, "trail": trail,
                "stop": stop_lv, "build": build, "fb": fb
            })
            count += 1
            print(f"  ✓ {code} {name}: {shares:,}股")
        
        conn.commit()
        print(f"\n✅ position_state 已更新: {count}只持仓")
        print(f"   总资产: ¥{account.get('total_asset', 0):,.0f}")
        return True

if __name__ == '__main__':
    do_all = '--all' in sys.argv
    
    print("=== 同步同花顺持仓到管理端MySQL ===\n")
    
    # Step 1: 从同花顺拉取最新数据
    print("① trade.py sync ...")
    os.system('cd /Users/lujie/Documents/code/quant && source .venv/bin/activate && python3 trade.py sync')
    
    # Step 2: 同步到MySQL
    print("\n② 写入 position_state ...")
    sync_positions()
    
    print("\n=== 完成 ===")
    print("管理端 http://110.40.168.227 刷新查看")