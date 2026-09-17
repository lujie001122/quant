# ETF量化交易系统 · 绩效分析管理端

## 简介

MySQL + SQLAlchemy ORM 驱动的绩效分析系统。点开持仓标的，一眼看清每笔买入明细——哪天买的、什么信号触发的、每笔成本多少、赚了还是亏了。

## 安装

```bash
# 依赖已内置 requirements.txt
pip install SQLAlchemy pymysql jinja2

# 首次初始化数据库（自动建表）
python3 -c "from core.db import init_db; init_db()"
```

## 配置

`config.yaml` 已包含 `database` 段，默认连接本地 MySQL `quant` 数据库。

## 命令

```bash
# ★ 核心：持仓明细追溯
python3 performance/report_cli.py position --code=515880

# 每日交易明细
python3 performance/report_cli.py daily --date=2026-09-17

# 合同编号聚合
python3 performance/report_cli.py contract --contract-no=20260910001

# 月度绩效
python3 performance/report_cli.py monthly --month=2026-09

# 做T vs 普通对比
python3 performance/report_cli.py t0-stats --start=2026-09-01 --end=2026-09-17

# 信号来源胜率
python3 performance/report_cli.py signal-stats --start=2026-09-01 --end=2026-09-17

# 资金曲线
python3 performance/report_cli.py equity --start=2026-09-01 --end=2026-09-17

# HTML 仪表盘
python3 performance/report_cli.py dashboard --start=2026-09-01 --end=2026-09-17 --output=report.html

# 同花顺委托+成交同步
python3 performance/sync_ths.py all

# 对账
python3 performance/report_cli.py reconcile --date=2026-09-17

# JSON→MySQL 迁移
python3 performance/report_cli.py migrate
```

## 架构

```
quant/
├── core/
│   ├── models.py          # 7张表 ORM（SQLAlchemy 2.0）
│   ├── db.py              # 引擎 + Session 管理
│   ├── repositories.py    # 数据访问层
│   └── trade_recorder.py  # 写 MySQL（替代 JSON）
├── performance/
│   ├── analyzer.py        # 分析引擎（position_cost_trace）
│   ├── report_cli.py      # CLI 入口
│   ├── sync_ths.py        # 同花顺委托+成交同步
│   ├── sync_equity.py     # 每日净值同步
│   ├── report_html.py     # HTML 仪表盘
│   ├── reconcile.py       # 对账
│   ├── migrate.py         # JSON→MySQL 迁移
│   └── templates/report.html  # HTML 模板
├── tests/
│   ├── seed_data.py       # 测试数据
│   └── test_analyzer.py   # 单元测试
└── README_report.md       # 本文件
```

## 验证

```bash
python3 tests/seed_data.py
python3 tests/test_analyzer.py
python3 performance/report_cli.py position --code=515880
```