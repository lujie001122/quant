# Path Migration & Claude Code (2026-08-01)

## Final Structure

```
~/Documents/
├── code/quant/                 ← 统一策略代码库（Git + cron + 回测 + 实盘）
│   ├── backtest_bt.py
│   ├── signal_generator.py
│   ├── intraday_t_once.py
│   ├── ths_client.py
│   ├── sentiment_check.py
│   ├── portfolio.json
│   ├── sentiment_block.json
│   ├── git_backup.sh
│   ├── etf_premarket.sh
│   ├── etf_intraday.sh
│   └── README.md
└── AI/                         ← AI工作区
    ├── projects/
    ├── plans/
    ├── notes/
    └── etf-analysis/
        ├── optimize_test.py
        ├── strategy_analysis.py
        └── audit_report.md
```

## Deleted

| Path | Reason |
|------|--------|
| `~/quant/` | 迁移到 `~/Documents/code/quant/` |
| `~/hermes/scripts/` | 策略文件统一到仓库，cron已指向仓库 |
| `~/.hermes/scripts/` | 旧备份，已过期 |
| `~/etf_system/` | 7月30日旧快照 |
| `~/hermes/` | 空目录 |
| `~/node_modules/` | 全局npm（electron相关，不再需要） |

## Retained

| Path | Reason |
|------|--------|
| `~/hermes-trading/.venv/` | Python虚拟环境（策略代码依赖） |
| `~/logs/evolving/` | 同花顺Evolving日志 |

## Cron Jobs

All 4 cron jobs now use `workdir: /Users/lujie/Documents/code/quant/` — unified with Git repo. Prompts updated to not reference old paths.

## Claude Code Integration

Claude Code v2.1.220 installed via npm. Available for coding delegation.

**Setup:**
```bash
npm install -g --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code
```

**Config at `~/.claude/settings.json`** — uses custom LiteLLM gateway with glm-5.1 model.

**Usage:**
```bash
# Print mode (preferred for one-shot tasks)
claude -p "Fix the bug in signal_generator.py" --max-turns 10

# With permissions bypass for automation
claude -p "task" --dangerously-skip-permissions --allowedTools "Read,Edit,Bash"
```

**When user says "写代码不行" or similar**: delegate coding tasks to Claude Code via `claude -p` print mode. Prefer this over direct file editing for complex features.

**Skill reference**: Load `claude-code` skill for full CLI reference, tmux orchestration, and interactive mode.