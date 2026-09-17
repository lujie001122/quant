#!/usr/bin/env python3
"""
performance/webapp.py — 绩效分析 FastAPI 实时管理端

把原静态 HTML 报告（report_html.py）改为实时 Web 服务:
  - GET /                      仪表盘首页（jinja2 渲染 templates/report.html，
                               数据由浏览器 JS 从 /api/* 动态拉取，30秒自动刷新）
  - GET /api/positions         当前所有持仓列表
  - GET /api/position/{code}   持仓成本追溯（position_cost_trace）
  - GET /api/daily?date=       每日交易明细
  - GET /api/monthly?month=    月度绩效
  - GET /api/t0-stats?start=&end=      做T统计
  - GET /api/signal-stats?start=&end=  信号来源胜率
  - GET /api/equity?start=&end=        资金曲线
  - GET /api/reconcile?date=   对账（本地 vs 同花顺实际持仓）

启动:
  cd /Users/lujie/Documents/code/quant
  source .venv/bin/activate
  uvicorn performance.webapp:app --host 127.0.0.1 --port 8791 --reload

浏览器: http://127.0.0.1:8791
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select
from starlette.requests import Request

from core.db import get_session
from core.models import Trade
from core.repositories import PositionStateRepo, TradeRepo
from performance.analyzer import get_analyzer
from performance.reconcile import reconcile as run_reconcile

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')

app = FastAPI(title='ETF量化 · 绩效分析管理端', version='1.0.0')
templates = Jinja2Templates(directory=TEMPLATE_DIR)
az = get_analyzer()


# ──────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────

def _parse_date_or_400(s: str, field: str) -> date:
    """'YYYY-MM-DD' -> date，非法则抛 400"""
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"{field} 格式错误: {s!r}，应为 YYYY-MM-DD")


def _parse_month_or_400(s: str) -> tuple:
    """'YYYY-MM' -> (year, month)，非法则抛 400"""
    try:
        y, m = s.split('-')
        y, m = int(y), int(m)
        if not (1 <= m <= 12):
            raise ValueError
        return y, m
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"month 格式错误: {s!r}，应为 YYYY-MM")


def _default_range() -> tuple:
    """默认统计区间: 库内最早~最晚交易日（兜底近30天）"""
    with get_session() as sess:
        dmin = sess.scalar(select(func.min(Trade.trade_date)))
        dmax = sess.scalar(select(func.max(Trade.trade_date)))
    today = date.today()
    if dmin is None:
        return (today - timedelta(days=30)).isoformat(), today.isoformat()
    return dmin.isoformat(), (dmax or today).isoformat()


# ──────────────────────────────────────────────
# 页面路由
# ──────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse, summary='仪表盘首页')
def index(request: Request):
    """jinja2 渲染 report.html（复用静态版模板，服务端带全量数据渲染）。

    - 图表/明细数据在页面加载时即可见（服务端渲染），并注入
      30 秒自动刷新（模板本身不改动，刷新脚本随响应注入）。
    - /api/* 提供同源 JSON，可被前端脚本或外部工具直接消费。
    """
    start, end = _default_range()
    s, e = date.fromisoformat(start), date.fromisoformat(end)

    # 持仓卡片（position_cost_trace 全量，与 report_html.build_report 一致）
    positions = []
    for p in PositionStateRepo.list_active():
        trace = az.position_cost_trace(p.code)
        if trace['position']:
            trace['position']['name'] = p.name or trace['name']
        positions.append(trace)

    curve = az.equity_curve(start, end)
    by_code = TradeRepo.stats_by_code(s, e)
    by_signal = TradeRepo.stats_by_signal(s, e)
    t0_stats = az.t0_vs_normal_stats(start, end)

    # 交易明细表（区间内全部，前端可搜索/排序）
    trades = []
    with get_session() as sess:
        stmt = (select(Trade)
                .where(Trade.trade_date >= s, Trade.trade_date <= e)
                .order_by(Trade.trade_date.desc(), Trade.id.desc())
                .limit(2000))
        for t in sess.scalars(stmt):
            trades.append(az._trade_to_dict(t))

    # 注入30秒自动刷新（不修改模板文件本身）
    refresh_script = (
        '<script>\n'
        'if (location.search.indexOf("noreload") === -1) {\n'
        '  setTimeout(() => location.reload(), 30000);\n'
        '  document.querySelector("h1").insertAdjacentHTML("afterend",\n'
        '    \'<div class="sub" id="auto-refresh">↻ 30秒自动刷新中 · <a href="/?noreload" '
        'style="color:var(--blue)">暂停</a></div>\');\n'
        '}\n'
        '</script>\n</body>')

    # jinja2 autoescape 下 {{ curve_json }} 需要 Markup 才不会被转义
    rendered = templates.get_template('report.html').render(
        generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        start=start,
        end=end,
        positions=positions,
        curve_json=Markup(json.dumps(curve, ensure_ascii=False)),
        by_code=by_code,
        by_signal=by_signal,
        t0_categories=t0_stats['categories'],
        trades=trades,
    )
    html = rendered.replace('</body>', refresh_script, 1)
    return HTMLResponse(content=html)


# ──────────────────────────────────────────────
# JSON API
# ──────────────────────────────────────────────

@app.get('/api/positions', summary='当前所有持仓列表')
def api_positions():
    """活跃持仓 + 每个持仓的成本追溯摘要"""
    result = []
    for p in PositionStateRepo.list_active():
        trace = az.position_cost_trace(p.code)
        trace['position']['name'] = p.name or trace['name']
        result.append(trace)
    return {'count': len(result), 'positions': result}


@app.get('/api/position/{code}', summary='持仓成本追溯')
def api_position_trace(code: str):
    """某标的完整买入/卖出明细（哪天买的、什么信号、成本多少、赚亏）"""
    trace = az.position_cost_trace(code)
    if not trace['buys'] and not trace['sells'] and trace['position'] is None:
        raise HTTPException(status_code=404, detail=f'无标的数据: {code}')
    return trace


@app.get('/api/daily', summary='每日交易明细')
def api_daily(date: str = Query(..., description='YYYY-MM-DD')):
    _parse_date_or_400(date, 'date')
    return az.daily_report(date)


@app.get('/api/monthly', summary='月度绩效')
def api_monthly(month: str = Query(..., description='YYYY-MM')):
    _parse_month_or_400(month)
    return az.monthly_summary(month)


@app.get('/api/t0-stats', summary='做T vs 普通 vs 网格统计')
def api_t0_stats(
    start: str = Query(..., description='YYYY-MM-DD'),
    end: str = Query(..., description='YYYY-MM-DD'),
):
    _parse_date_or_400(start, 'start')
    _parse_date_or_400(end, 'end')
    return az.t0_vs_normal_stats(start, end)


@app.get('/api/signal-stats', summary='信号来源胜率')
def api_signal_stats(
    start: str = Query(..., description='YYYY-MM-DD'),
    end: str = Query(..., description='YYYY-MM-DD'),
):
    _parse_date_or_400(start, 'start')
    _parse_date_or_400(end, 'end')
    return {'start': start, 'end': end,
            'stats': az.signal_source_stats(start, end)}


@app.get('/api/equity', summary='资金曲线')
def api_equity(
    start: Optional[str] = Query(None, description='YYYY-MM-DD'),
    end: Optional[str] = Query(None, description='YYYY-MM-DD'),
):
    if start:
        _parse_date_or_400(start, 'start')
    if end:
        _parse_date_or_400(end, 'end')
    return az.equity_curve(start, end)


@app.get('/api/reconcile', summary='对账（本地 vs 同花顺）')
def api_reconcile(date_: Optional[str] = Query(None, alias='date',
                                               description='YYYY-MM-DD，默认今天')):
    if date_:
        _parse_date_or_400(date_, 'date')
    try:
        result = run_reconcile(date_)
    except Exception as ex:  # 对账底层失败时返回结构化错误而非500裸栈
        return JSONResponse(
            status_code=502,
            content={'error': f'对账失败: {ex}'})
    return {'date': date_ or date.today().isoformat(), **result}


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('performance.webapp:app', host='127.0.0.1', port=8791, reload=False)
