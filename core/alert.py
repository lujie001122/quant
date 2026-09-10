#!/usr/bin/env python3
"""
alert.py — 告警系统（webhook推送 + 分级 + 去重 + 日志落盘）

解决问题:
  - 系统异常时无通知，依赖人工巡检
  - 告警泛滥，同一问题反复推送
  - 告警没有分级，critical 和 info 混在一起
  - 告警无处可查，无法回溯

核心功能:
  1. 三级告警: info / warning / critical
  2. Webhook推送: 支持企业微信/钉钉/飞书/自定义
  3. 5分钟去重: 同一告警5分钟内不重复推送
  4. 日志落盘: 所有告警记录到文件，支持回溯
  5. 告警统计: 按级别/时间统计告警频次

用法:
  from alert import AlertManager, AlertLevel

  am = AlertManager(webhook_url="https://...")
  am.info("订单提交", f"买入 {code} {shares}股")
  am.warning("止损触发", f"{code} 亏损超过15%")
  am.critical("系统异常", "行情数据连接断开")
"""

import json
import os
import time
import traceback
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from enum import IntEnum
from threading import Lock
from typing import Optional, Dict, List


# ══════════════════════════════════════════════
# 告警级别
# ══════════════════════════════════════════════

class AlertLevel(IntEnum):
    """告警级别（数值越大越严重）"""
    INFO = 0
    WARNING = 1
    CRITICAL = 2


# 告警级别对应的emoji和前缀
_LEVEL_DISPLAY = {
    AlertLevel.INFO: ("ℹ️", "INFO"),
    AlertLevel.WARNING: ("⚠️", "WARN"),
    AlertLevel.CRITICAL: ("🚨", "CRIT"),
}


# ══════════════════════════════════════════════
# 告警记录
# ══════════════════════════════════════════════

class AlertRecord:
    """一条告警记录"""

    __slots__ = ('level', 'title', 'message', 'timestamp', 'dedup_key', 'pushed')

    def __init__(
        self,
        level: AlertLevel,
        title: str,
        message: str,
        dedup_key: str = "",
        pushed: bool = False,
    ):
        self.level = level
        self.title = title
        self.message = message
        self.timestamp = datetime.now()
        self.dedup_key = dedup_key
        self.pushed = pushed

    def to_dict(self) -> dict:
        return {
            "level": self.level.name,
            "title": self.title,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "dedup_key": self.dedup_key,
            "pushed": self.pushed,
        }

    def __repr__(self):
        emoji, prefix = _LEVEL_DISPLAY[self.level]
        return f"[{prefix}] {self.title}: {self.message}"


# ══════════════════════════════════════════════
# 告警管理器
# ══════════════════════════════════════════════

class AlertManager:
    """告警管理器 — 统一告警推送、去重、日志落盘

    参数:
      webhook_url: Webhook推送地址（企业微信/钉钉/飞书等）
      dedup_seconds: 去重时间窗口（秒），默认300秒=5分钟
      log_dir: 告警日志目录
      min_push_level: 最低推送级别（低于此级别只记日志不推送）
      enabled: 是否启用推送（False时只记日志）
    """

    def __init__(
        self,
        webhook_url: str = "",
        dedup_seconds: int = 300,
        log_dir: str = "",
        min_push_level: AlertLevel = AlertLevel.WARNING,
        enabled: bool = True,
    ):
        self.webhook_url = webhook_url
        self.dedup_seconds = dedup_seconds
        self.min_push_level = min_push_level
        self.enabled = enabled and bool(webhook_url)

        # 去重缓存: dedup_key -> 最后推送时间
        self._dedup_cache: Dict[str, float] = {}
        self._lock = Lock()

        # 告警历史（内存，最多保留最近1000条）
        self._history: List[AlertRecord] = []
        self._max_history = 1000

        # 日志目录
        if log_dir:
            self._log_dir = log_dir
        else:
            # 默认: 项目根目录/logs/alerts/
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self._log_dir = os.path.join(project_root, "logs", "alerts")

        os.makedirs(self._log_dir, exist_ok=True)

        # 从环境变量读取webhook_url（可选）
        if not self.webhook_url:
            env_url = os.environ.get("ALERT_WEBHOOK_URL", "")
            if env_url:
                self.webhook_url = env_url
                self.enabled = True

    # ── 便捷方法 ──

    def info(self, title: str, message: str, dedup_key: str = "") -> bool:
        """发送 INFO 级别告警"""
        return self.send(AlertLevel.INFO, title, message, dedup_key)

    def warning(self, title: str, message: str, dedup_key: str = "") -> bool:
        """发送 WARNING 级别告警"""
        return self.send(AlertLevel.WARNING, title, message, dedup_key)

    def critical(self, title: str, message: str, dedup_key: str = "") -> bool:
        """发送 CRITICAL 级别告警"""
        return self.send(AlertLevel.CRITICAL, title, message, dedup_key)

    # ── 核心发送 ──

    def send(
        self,
        level: AlertLevel,
        title: str,
        message: str,
        dedup_key: str = "",
    ) -> bool:
        """发送告警

        参数:
          level: 告警级别
          title: 告警标题（简短）
          message: 告警详情
          dedup_key: 去重键（空则自动生成: level:title）

        返回:
          bool: 是否实际推送（去重时返回False）
        """
        if not dedup_key:
            dedup_key = f"{level.name}:{title}"

        now = time.time()
        pushed = False

        # ── 去重检查 ──
        with self._lock:
            last_sent = self._dedup_cache.get(dedup_key, 0)
            should_push = (now - last_sent) >= self.dedup_seconds

            if should_push:
                self._dedup_cache[dedup_key] = now

        # ── 创建告警记录 ──
        record = AlertRecord(
            level=level,
            title=title,
            message=message,
            dedup_key=dedup_key,
            pushed=False,
        )

        # ── 推送（仅达到级别要求且未去重） ──
        if should_push and level >= self.min_push_level and self.enabled:
            push_ok = self._push_webhook(record)
            record.pushed = push_ok

        # ── 记录历史 ──
        with self._lock:
            self._history.append(record)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        # ── 日志落盘 ──
        self._write_log(record)

        return should_push

    # ── Webhook推送 ──

    def _push_webhook(self, record: AlertRecord) -> bool:
        """通过Webhook推送告警

        支持格式:
          - 企业微信（Markdown格式）
          - 钉钉（Markdown格式）
          - 飞书（富文本格式）
          - 通用JSON POST
        """
        if not self.webhook_url:
            return False

        emoji, prefix = _LEVEL_DISPLAY[record.level]
        timestamp_str = record.timestamp.strftime("%Y-%m-%d %H:%M:%S")

        # 尝试检测webhook类型并格式化
        payload = self._format_payload(record, emoji, prefix, timestamp_str)

        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            print(f"[alert] Webhook推送失败: {e}")
            return False

    def _format_payload(self, record: AlertRecord, emoji: str, prefix: str, ts: str) -> dict:
        """根据webhook URL自动选择格式"""
        url = self.webhook_url.lower()

        if "qyapi.weixin" in url or "work.weixin" in url:
            # 企业微信 Markdown 格式
            content = f"{emoji} **[{prefix}]** {record.title}\n> {record.message}\n> {ts}"
            return {
                "msgtype": "markdown",
                "markdown": {"content": content},
            }
        elif "oapi.dingtalk" in url:
            # 钉钉 Markdown 格式
            content = f"{emoji} **[{prefix}]** {record.title}\n\n{record.message}\n\n{ts}"
            return {
                "msgtype": "markdown",
                "markdown": {"title": f"{prefix}: {record.title}", "text": content},
            }
        elif "open.feishu" in url or "open.lark" in url:
            # 飞书 富文本格式
            return {
                "msg_type": "interactive",
                "card": {
                    "header": {
                        "title": {"tag": "plain_text", "content": f"{emoji} [{prefix}] {record.title}"},
                        "template": "red" if record.level == AlertLevel.CRITICAL else "orange" if record.level == AlertLevel.WARNING else "blue",
                    },
                    "elements": [
                        {"tag": "markdown", "content": record.message},
                        {"tag": "plain_text", "content": ts},
                    ],
                },
            }
        else:
            # 通用JSON格式
            return {
                "level": record.level.name,
                "title": record.title,
                "message": record.message,
                "timestamp": ts,
            }

    # ── 日志落盘 ──

    def _write_log(self, record: AlertRecord) -> None:
        """将告警写入日志文件"""
        try:
            date_str = record.timestamp.strftime("%Y-%m-%d")
            log_file = os.path.join(self._log_dir, f"alert_{date_str}.log")

            emoji, prefix = _LEVEL_DISPLAY[record.level]
            line = f"[{record.timestamp.strftime('%H:%M:%S')}] [{prefix}] {record.title}: {record.message}"
            if record.pushed:
                line += " [PUSHED]"

            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            print(f"[alert] 日志写入失败: {e}")

    # ── 查询接口 ──

    def get_recent_alerts(self, count: int = 20, level: Optional[AlertLevel] = None) -> List[AlertRecord]:
        """获取最近N条告警

        参数:
          count: 返回数量
          level: 过滤级别（None则不过滤）
        """
        with self._lock:
            alerts = self._history[-count:] if count < len(self._history) else list(self._history)
        if level is not None:
            alerts = [a for a in alerts if a.level == level]
        return alerts

    def get_stats(self, hours: int = 24) -> dict:
        """获取告警统计

        参数:
          hours: 统计最近N小时

        返回:
          dict: {total, by_level: {INFO: N, WARNING: N, CRITICAL: N}, pushed_count}
        """
        cutoff = datetime.now() - timedelta(hours=hours)
        with self._lock:
            recent = [a for a in self._history if a.timestamp >= cutoff]

        by_level = {level.name: 0 for level in AlertLevel}
        pushed_count = 0
        for a in recent:
            by_level[a.level.name] += 1
            if a.pushed:
                pushed_count += 1

        return {
            "total": len(recent),
            "by_level": by_level,
            "pushed_count": pushed_count,
            "hours": hours,
        }

    def clear_dedup_cache(self) -> None:
        """清除去重缓存（用于测试或手动重发）"""
        with self._lock:
            self._dedup_cache.clear()

    def cleanup_dedup_cache(self) -> int:
        """清理过期的去重缓存条目

        返回:
          int: 清理的条目数
        """
        now = time.time()
        expired_keys = []
        with self._lock:
            for key, ts in self._dedup_cache.items():
                if now - ts > self.dedup_seconds * 2:
                    expired_keys.append(key)
            for key in expired_keys:
                del self._dedup_cache[key]
        return len(expired_keys)


# ══════════════════════════════════════════════
# 全局单例（可选使用）
# ══════════════════════════════════════════════

_global_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    """获取全局告警管理器实例"""
    global _global_manager
    if _global_manager is None:
        _global_manager = AlertManager()
    return _global_manager


def set_alert_manager(manager: AlertManager) -> None:
    """设置全局告警管理器实例"""
    global _global_manager
    _global_manager = manager
