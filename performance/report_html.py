#!/usr/bin/env python3
"""
performance/report_html.py — HTML 仪表盘生成

用 jinja2 渲染 templates/report.html（plotly 从 CDN 引入）:
  - 持仓卡片: 点击任一持仓 → 展开买入明细（对应 position_cost_trace）
  - 资金曲线（折线）
  - 每日盈亏柱状图（红绿）
  - 分ETF贡献饼图
  - 做T/普通/网格占比饼图
  - 信号来源胜率表
  - 交易明细表（前端搜索/排序/按日期筛选）

数据全部从 MySQL 读取，渲染后生成单个自包含 HTML（无本地依赖）。

用法:
  python3 performance/report_html.py --start=2026-09-01 --end=2026-09-17 \
      --output=report.html
"""

import json
import os
import sys
from datetime import date, datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.repositories import PositionStateRepo, TradeRepo

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
DEFAULT_OUTPUT = os.path.join(_PROJECT_ROOT, 'report.html')


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def build_report(start: str, end: str, output: str = None) -> str:
    """生成 HTML 仪表盘

    参数:
      start/end: 'YYYY-MM-DD' 统计区间（含两端）
      output: 输出文件路径（默认项目根 report.html）
    返回: 输出文件路径
    """
    from performance.analyzer import get_analyzer
    az = get_analyzer()
    s, e = _parse_date(start), _parse_date(end)

    # ── 数据准备 ──
    # 持仓卡片（position_cost_trace 全量）
    positions = []
    for p in PositionStateRepo.list_active():
        trace = az.position_cost_trace(p.code)
        trace['position']['name'] = p.name or trace['name']
        positions.append(trace)

    # 资金曲线 + 每日盈亏
    curve = az.equity_curve(start, end)

    # 分ETF统计 / 信号胜率 / 做T对比
    by_code = TradeRepo.stats_by_code(s, e)
    by_signal = TradeRepo.stats_by_signal(s, e)
    t0_stats = az.t0_vs_normal_stats(start, end)

    # 交易明细表（区间内全部交易，前端可搜索/排序）
    trades = []
    from core.db import get_session
    from core.models import Trade
    from sqlalchemy import select
    with get_session() as sess:
        stmt = (select(Trade)
                .where(Trade.trade_date >= s, Trade.trade_date <= e)
                .order_by(Trade.trade_date.desc(), Trade.id.desc())
                .limit(2000))
        for t in sess.scalars(stmt):
            trades.append(az._trade_to_dict(t))

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(['html']),
    )
    tpl = env.get_template('report.html')

    html = tpl.render(
        generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        start=start, end=end,
        positions=positions,
        curve_json=json.dumps(curve, ensure_ascii=False),
        by_code=by_code,
        by_signal=by_signal,
        t0_categories=t0_stats['categories'],
        trades=trades,
    )

    out = output or DEFAULT_OUTPUT
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✅ HTML仪表盘已生成: {out} ({len(trades)}笔交易, {len(positions)}个持仓)")
    return out


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='生成HTML绩效仪表盘')
    parser.add_argument('--start', required=True, help='开始日期 YYYY-MM-DD')
    parser.add_argument('--end', required=True, help='结束日期 YYYY-MM-DD')
    parser.add_argument('--output', default=None, help='输出文件路径')
    args = parser.parse_args()
    build_report(args.start, args.end, args.output)
