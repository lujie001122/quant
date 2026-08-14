#!/usr/bin/env python3
"""
ai_signal.py — AI 信号源

调用 Claude API 分析持仓和舆情，生成辅助交易信号。
confidence 阈值过滤，低置信度信号自动丢弃。

用法:
  from ai_signal import AISignalGenerator
  ai = AISignalGenerator()
  ai_signals = ai.generate(positions, sentiment_summary, market_context)

Phase 4: 独立 AI 信号源，与策略信号互补。
"""

import json
import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from decision_engine import OrderIntent


# ═══════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════

# 置信度阈值
CONFIDENCE_THRESHOLD = 0.65  # 低于此阈值的信号丢弃
STRONG_CONFIDENCE = 0.85     # 高置信度信号

# API 配置
DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.3

# 请求超时
REQUEST_TIMEOUT = 30  # 秒


# ═══════════════════════════════════════════════════════
# AI 信号生成器
# ═══════════════════════════════════════════════════════

class AISignalGenerator:
    """AI 信号生成器 — 调用 Claude API 分析持仓和舆情

    职责:
      1. 构建分析 prompt（持仓+舆情+市场环境）
      2. 调用 Claude API
      3. 解析结构化输出为 Signal
      4. 置信度阈值过滤
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.model = model
        self.confidence_threshold = confidence_threshold
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = base_url

        # 统计
        self._call_count = 0
        self._signal_count = 0

    # ══════════════════════════════════════════════════
    # 主入口
    # ══════════════════════════════════════════════════

    def generate(
        self,
        positions: Optional[Dict[str, Dict]] = None,
        sentiment_summary: Optional[str] = None,
        market_context: Optional[str] = None,
        today: Optional[str] = None,
    ) -> List[OrderIntent]:
        """生成 AI 交易信号

        参数:
          positions: {code: {shares, avg_cost, current_price, pnl_pct, ...}, ...}
          sentiment_summary: 舆情摘要（从 sentiment_check.py 获取）
          market_context: 市场环境描述
          today: 日期字符串

        返回:
          List[OrderIntent]: AI 信号列表（已过滤低置信度）
        """
        if today is None:
            today = datetime.now().strftime("%Y-%m-%d")

        # ── 构建 prompt ──
        prompt = self._build_prompt(positions, sentiment_summary, market_context)

        # ── 调用 Claude API ──
        response = self._call_claude(prompt)
        if response is None:
            return []

        # ── 解析信号 ──
        signals = self._parse_response(response, positions, today)
        self._call_count += 1

        return signals

    # ══════════════════════════════════════════════════
    # Prompt 构建
    # ══════════════════════════════════════════════════

    def _build_prompt(
        self,
        positions: Optional[Dict[str, Dict]],
        sentiment_summary: Optional[str],
        market_context: Optional[str],
    ) -> str:
        """构建分析 prompt"""
        parts = []

        parts.append("你是一个量化交易 AI 助手，负责分析 ETF 持仓和舆情，生成辅助交易信号。")
        parts.append("")
        parts.append("## 任务")
        parts.append("分析以下持仓和舆情信息，为每个标的生成交易建议。")
        parts.append("")

        # ── 持仓信息 ──
        if positions:
            parts.append("## 当前持仓")
            parts.append("| 代码 | 名称 | 持仓 | 成本 | 现价 | 盈亏% | 仓位% |")
            parts.append("|------|------|------|------|------|-------|-------|")
            total_value = sum(
                p.get("shares", 0) * p.get("current_price", 0)
                for p in positions.values()
            )
            for code, pos in positions.items():
                if pos.get("shares", 0) <= 0:
                    continue
                name = pos.get("name", code)
                shares = pos.get("shares", 0)
                cost = pos.get("avg_cost", 0)
                price = pos.get("current_price", 0)
                pnl = ((price - cost) / cost * 100) if cost > 0 else 0
                ratio = (shares * price / total_value * 100) if total_value > 0 else 0
                parts.append(f"| {code} | {name} | {shares}股 | {cost:.3f} | {price:.3f} | {pnl:+.1f}% | {ratio:.1f}% |")
        else:
            parts.append("## 当前持仓: 空仓")

        parts.append("")

        # ── 舆情摘要 ──
        if sentiment_summary:
            parts.append("## 舆情摘要")
            parts.append(sentiment_summary)
        else:
            parts.append("## 舆情摘要: 无特别舆情")

        parts.append("")

        # ── 市场环境 ──
        if market_context:
            parts.append("## 市场环境")
            parts.append(market_context)
        else:
            parts.append("## 市场环境: 正常")

        parts.append("")

        # ── 输出格式要求 ──
        parts.append("## 输出格式")
        parts.append("请以 JSON 格式输出，每个信号包含以下字段:")
        parts.append("```json")
        parts.append("{")
        parts.append('  "signals": [')
        parts.append("    {")
        parts.append('      "code": "ETF代码",')
        parts.append('      "action": "买入/卖出/持有",')
        parts.append('      "reason": "建议理由(简短)",')
        parts.append('      "confidence": 0.0-1.0,')
        parts.append('      "position_pct": 建议仓位比例(0.0-1.0)')
        parts.append("    }")
        parts.append("  ]")
        parts.append("}")
        parts.append("```")
        parts.append("")
        parts.append("注意事项:")
        parts.append("- 只对有明显机会的标的给出 买入/卖出 建议")
        parts.append("- confidence 必须真实反映你的确信程度（0.5=不确定, 0.9=非常确定）")
        parts.append("- 没有把握的标的给 持有，confidence 0.5")
        parts.append("- 不要输出任何 JSON 以外的文字")

        return "\n".join(parts)

    # ══════════════════════════════════════════════════
    # API 调用
    # ══════════════════════════════════════════════════

    def _call_claude(self, prompt: str) -> Optional[str]:
        """调用 Claude API（通过 anthropic SDK 或 HTTP）"""
        # 尝试使用 anthropic SDK
        try:
            return self._call_via_sdk(prompt)
        except ImportError:
            pass
        except Exception as e:
            print(f"  [AI] SDK 调用失败: {e}，尝试 HTTP...")

        # 回退到 HTTP
        try:
            return self._call_via_http(prompt)
        except Exception as e:
            print(f"  [AI] HTTP 调用失败: {e}")
            return None

    def _call_via_sdk(self, prompt: str) -> Optional[str]:
        """通过 anthropic SDK 调用"""
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        if self.base_url:
            client = anthropic.Anthropic(
                api_key=self.api_key,
                base_url=self.base_url,
            )

        message = client.messages.create(
            model=self.model,
            max_tokens=DEFAULT_MAX_TOKENS,
            temperature=DEFAULT_TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
        )

        return message.content[0].text

    def _call_via_http(self, prompt: str) -> Optional[str]:
        """通过 HTTP 直接调用（兼容 LiteLLM 网关）"""
        import urllib.request
        import urllib.error

        api_key = self.api_key or os.environ.get("OPENAI_API_KEY", "")
        base_url = self.base_url or "https://api.anthropic.com/v1/messages"

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }

        body = {
            "model": self.model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "temperature": DEFAULT_TEMPERATURE,
            "messages": [{"role": "user", "content": prompt}],
        }

        req = urllib.request.Request(
            base_url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["content"][0]["text"]

    # ══════════════════════════════════════════════════
    # 响应解析
    # ══════════════════════════════════════════════════

    def _parse_response(
        self,
        response: str,
        positions: Optional[Dict[str, Dict]],
        today: str,
    ) -> List[OrderIntent]:
        """解析 Claude 响应为 OrderIntent 列表"""
        try:
            # 提取 JSON 块
            json_str = self._extract_json(response)
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  [AI] 响应解析失败: {e}")
            print(f"  [AI] 原始响应: {response[:200]}...")
            return []

        signals = data.get("signals", [])
        if not isinstance(signals, list):
            return []

        intents = []
        for sig in signals:
            # ── 置信度过滤 ──
            confidence = float(sig.get("confidence", 0.5))
            if confidence < self.confidence_threshold:
                print(f"  [AI] {sig.get('code')} 置信度{confidence:.2f}<{self.confidence_threshold}，丢弃")
                continue

            # ── 跳过"持有"信号 ──
            action = sig.get("action", "持有")
            if action == "持有":
                continue

            code = str(sig.get("code", ""))
            if not code:
                continue

            # ── 获取价格 ──
            price = 0.0
            if positions and code in positions:
                price = positions[code].get("current_price", 0.0)

            if price <= 0:
                continue

            # ── 计算数量 ──
            position_pct = float(sig.get("position_pct", 0.0))
            fund = 44000  # 默认单只分配
            if positions and code in positions:
                pos = positions[code]
                fund = pos.get("fund", 44000)

            shares = int(fund * position_pct / price / 100) * 100 if position_pct > 0 else 0

            if action == "买入" and shares < 100:
                continue
            if action == "卖出" and positions and code in positions:
                current = positions[code].get("shares", 0)
                shares = min(shares, current) if shares > 0 else current

            direction = "买入" if action == "买入" else "卖出"
            trade_type = "buy" if action == "买入" else "sell"

            intent = OrderIntent(
                code=code,
                action=action,
                trade_type=trade_type,
                direction=direction,
                price=price,
                shares=shares,
                reason=sig.get("reason", ""),
                confidence=confidence,
                source="ai",
                signal_price=price,
                live_price=price,
            )
            intents.append(intent)
            self._signal_count += 1

        return intents

    @staticmethod
    def _extract_json(text: str) -> str:
        """从文本中提取 JSON 块"""
        text = text.strip()

        # 尝试提取 ```json ... ``` 块
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            return text[start:end].strip()

        # 尝试提取 ``` ... ``` 块
        if "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            return text[start:end].strip()

        # 尝试提取 { ... }
        if text.startswith("{"):
            return text

        # 查找第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return text[start:end + 1]

        return text

    # ══════════════════════════════════════════════════
    # 统计
    # ══════════════════════════════════════════════════

    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            "call_count": self._call_count,
            "signal_count": self._signal_count,
            "avg_signals_per_call": self._signal_count / max(self._call_count, 1),
        }


# ═══════════════════════════════════════════════════════
# 模块级快捷函数
# ═══════════════════════════════════════════════════════

_ai_generator: Optional[AISignalGenerator] = None


def get_ai_generator() -> AISignalGenerator:
    """获取默认 AI 信号生成器"""
    global _ai_generator
    if _ai_generator is None:
        _ai_generator = AISignalGenerator()
    return _ai_generator


def generate_ai_signals(
    positions: Optional[Dict] = None,
    sentiment_summary: Optional[str] = None,
    market_context: Optional[str] = None,
) -> List[OrderIntent]:
    """快捷函数: 生成 AI 信号"""
    return get_ai_generator().generate(positions, sentiment_summary, market_context)


# ═══════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    # 模拟持仓数据
    mock_positions = {
        "159516": {
            "name": "半导体设备ETF",
            "shares": 5000,
            "avg_cost": 1.250,
            "current_price": 1.380,
            "fund": 44000,
        },
        "515880": {
            "name": "通信ETF",
            "shares": 3000,
            "avg_cost": 1.520,
            "current_price": 1.480,
            "fund": 44000,
        },
    }

    mock_sentiment = "半导体板块受AI芯片需求推动走强，通信板块受5G招标延迟影响偏弱。"

    print("=== AI 信号生成器测试 ===")
    ai = AISignalGenerator()
    print(f"模型: {ai.model}")
    print(f"置信度阈值: {ai.confidence_threshold}")

    signals = ai.generate(mock_positions, mock_sentiment, "市场整体震荡偏强")
    print(f"\n生成 {len(signals)} 个 AI 信号:")
    for s in signals:
        print(f"  {s.code} {s.action} {s.shares}股 @{s.price:.3f} "
              f"(置信度:{s.confidence:.2f}) [{s.reason}]")

    print(f"\n统计: {ai.get_stats()}")