#!/usr/bin/env python3
"""
performance — 绩效分析管理端

模块:
  analyzer      分析引擎（position_cost_trace 持仓成本追溯等）
  report_cli    CLI 入口
  report_html   HTML 仪表盘生成
  sync_ths      同花顺委托/成交同步
  sync_equity   每日净值同步
  reconcile     对账
  migrate       JSON → MySQL 一次性迁移
"""

from performance.analyzer import PerformanceAnalyzer, get_analyzer

__all__ = ['PerformanceAnalyzer', 'get_analyzer']
