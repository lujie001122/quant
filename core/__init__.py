#!/usr/bin/env python3
"""
core — 基础设施模块包

提供量化交易系统底层能力:
  - atomic_writer: 原子文件读写 + 自动备份 + 损坏恢复
  - alert: 告警推送 + 分级 + 去重 + 日志落盘
  - trade_recorder: 交易记录 + 对账 + 绩效报告
  - position_manager: 仓位管理 + Kelly公式 + 风险平价 + 资金分配
"""

from core.atomic_writer import atomic_write_json, atomic_read_json
from core.alert import AlertManager, AlertLevel, get_alert_manager
from core.trade_recorder import TradeRecorder, get_recorder
from core.position_manager import PositionManager, get_position_manager
