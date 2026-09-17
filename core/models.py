#!/usr/bin/env python3
"""
core/models.py — SQLAlchemy 2.0 ORM 模型（7张表）

绩效分析管理端的全部数据表。全部采用 SQLAlchemy 2.0 declarative 风格
（`Mapped` + `mapped_column`），不用旧版 `Column`。

表清单:
  1. entrusts      委托主表（同花顺委托，合同编号唯一）
  2. deals         成交明细表（同花顺成交，成交编号唯一）
  3. trades        交易记录主表 ★核心（本地每笔成交，信号溯源字段齐全）
  4. position_state 持仓状态表（portfolio.json _signal_state 的镜像，供分析）
  5. daily_equity  每日净值表（资金曲线）
  6. reconcile_log 对账记录表（本地持仓 vs 同花顺实际持仓）
  7. sync_log      同步日志表（entrust/deal/equity/reconcile 各类同步）

设计约定:
  - 金额用 DECIMAL(14,2)，价格用 DECIMAL(10,4)，比例用 DECIMAL(8,4)
  - 去重: entrusts.contract_no / deals.deal_no 唯一，写入用 INSERT IGNORE
  - signal_date 与 trade_date 可能不同（隔夜单），分别记录
  - 做T配对: trades.pair_deal_no 关联配对成交编号
"""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    func,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ══════════════════════════════════════════════
# Base
# ══════════════════════════════════════════════

class Base(DeclarativeBase):
    """全局 declarative base"""
    pass


# 交易分类枚举: normal=普通信号交易 / t0=做T / grid=网格 / sell_core=卖底仓
TRADE_CATEGORY = ('normal', 't0', 'grid', 'sell_core')
# 买卖方向（同花顺中文）
DIRECTION_CN = ('买入', '卖出')
# 本地买卖方向（英文）
DIRECTION_EN = ('buy', 'sell')
# 对账状态
RECONCILE_STATUS = ('match', 'mismatch', 'local_only', 'actual_only')
# 同步类型
SYNC_TYPE = ('entrust', 'deal', 'equity', 'reconcile')


# ══════════════════════════════════════════════
# 1. entrusts 委托主表
# ══════════════════════════════════════════════

class Entrust(Base):
    """委托主表 — 同花顺委托记录

    数据来源: EvolvingSim.getEntrust('today')，经 performance/sync_ths.py 同步。
    contract_no 为同花顺合同编号，全局唯一，重复同步用 INSERT IGNORE 去重。
    """
    __tablename__ = 'entrusts'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contract_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False,
                                             comment='合同编号（同花顺委托编号）')
    entrust_no: Mapped[Optional[str]] = mapped_column(String(32), comment='委托编号')
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True, comment='ETF代码')
    name: Mapped[Optional[str]] = mapped_column(String(32), comment='ETF名称')
    direction: Mapped[str] = mapped_column(Enum(*DIRECTION_CN, name='direction_cn'),
                                           nullable=False, comment='买卖方向')
    order_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), comment='委托价格')
    order_shares: Mapped[Optional[int]] = mapped_column(Integer, comment='委托数量')
    filled_shares: Mapped[Optional[int]] = mapped_column(Integer, comment='已成交数量')
    avg_fill_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), comment='加权成交均价')
    total_fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), comment='总手续费')
    status: Mapped[Optional[str]] = mapped_column(String(16), comment='已报/部成/已成/已撤/废单')
    trade_category: Mapped[Optional[str]] = mapped_column(
        Enum(*TRADE_CATEGORY, name='trade_category_e'),
        default='normal', comment='交易分类')
    signal_source: Mapped[Optional[str]] = mapped_column(String(64), comment='信号来源')
    strategy_name: Mapped[Optional[str]] = mapped_column(String(32), comment='策略名称')
    signal_date: Mapped[Optional[date]] = mapped_column(Date, comment='信号生成日期')
    order_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment='委托时间')
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 comment='创建时间')
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment='更新时间')

    # 一对多: 一笔委托对应多笔成交，级联删除
    deals: Mapped[List["Deal"]] = relationship(
        back_populates='entrust', cascade='all, delete-orphan',
        foreign_keys='Deal.contract_no', primaryjoin='Entrust.contract_no == Deal.contract_no')

    __table_args__ = (
        Index('idx_code_date', 'code', 'order_time'),
        Index('idx_category', 'trade_category'),
        Index('idx_status', 'status'),
        {'comment': '委托主表（同花顺）'},
    )

    def __repr__(self):
        return f"<Entrust {self.contract_no} {self.code} {self.direction} {self.order_shares}@{self.order_price} {self.status}>"


# ══════════════════════════════════════════════
# 2. deals 成交明细表
# ══════════════════════════════════════════════

class Deal(Base):
    """成交明细表 — 同花顺成交记录

    数据来源: EvolvingSim.getDeal('today')，经 performance/sync_ths.py 同步。
    deal_no 全局唯一（INSERT IGNORE 去重）。contract_no 关联 entrusts.contract_no。
    """
    __tablename__ = 'deals'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deal_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False,
                                         comment='成交编号')
    contract_no: Mapped[Optional[str]] = mapped_column(String(32), index=True,
                                                       comment='关联 entrusts.contract_no')
    code: Mapped[str] = mapped_column(String(10), nullable=False, comment='ETF代码')
    direction: Mapped[str] = mapped_column(Enum(*DIRECTION_CN, name='direction_cn_d'),
                                           nullable=False, comment='买卖方向')
    deal_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), comment='成交价格')
    deal_shares: Mapped[Optional[int]] = mapped_column(Integer, comment='成交数量')
    deal_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), comment='成交金额')
    fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), comment='手续费')
    deal_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment='成交时间')
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 comment='创建时间')

    # 多对一: 归属委托
    entrust: Mapped[Optional["Entrust"]] = relationship(
        back_populates='deals', viewonly=True,
        foreign_keys=[contract_no], primaryjoin='Deal.contract_no == Entrust.contract_no')

    __table_args__ = (
        Index('idx_code_time', 'code', 'deal_time'),
        {'comment': '成交明细表（同花顺）'},
    )

    def __repr__(self):
        return f"<Deal {self.deal_no} {self.code} {self.direction} {self.deal_shares}@{self.deal_price}>"


# ══════════════════════════════════════════════
# 3. trades 交易记录主表 ★核心
# ══════════════════════════════════════════════

class Trade(Base):
    """交易记录主表 — 每笔成交的完整溯源

    数据来源: core/trade_recorder.py 的 record()（executor.py 成交后调用）。
    核心价值: signal_source / strategy_name / signal_date 让每笔交易可以回答
    "哪天买的、什么信号触发、成本多少、赚了还是亏了"。
    """
    __tablename__ = 'trades'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True, comment='成交日期')
    trade_time: Mapped[Optional[time]] = mapped_column(Time, comment='成交时间')
    code: Mapped[str] = mapped_column(String(10), nullable=False, comment='ETF代码')
    name: Mapped[Optional[str]] = mapped_column(String(32), comment='ETF名称')
    direction: Mapped[str] = mapped_column(Enum(*DIRECTION_EN, name='direction_en'),
                                           nullable=False, comment='buy/sell')
    shares: Mapped[int] = mapped_column(Integer, nullable=False, comment='成交股数')
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), comment='成交价格')
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), comment='成交金额')
    fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), default=0, comment='手续费')
    pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), default=0,
                                                   comment='已实现盈亏（卖出时）')
    pnl_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment='盈亏比例')
    contract_no: Mapped[Optional[str]] = mapped_column(String(32), comment='合同编号')
    deal_no: Mapped[Optional[str]] = mapped_column(String(32), comment='成交编号')
    signal_date: Mapped[Optional[date]] = mapped_column(Date, comment='信号生成日期（隔夜单与成交日不同）')
    signal_source: Mapped[Optional[str]] = mapped_column(
        String(64), index=True, comment='信号来源: RSI抄底/趋势止盈/做T/网格/阶梯止盈等')
    strategy_name: Mapped[Optional[str]] = mapped_column(String(32), comment='策略名称')
    entry_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4),
                                                          comment='建仓均价（卖出时填写）')
    hold_days: Mapped[Optional[int]] = mapped_column(Integer, default=0, comment='持仓天数')
    trade_category: Mapped[Optional[str]] = mapped_column(
        Enum(*TRADE_CATEGORY, name='trade_category_t'), default='normal',
        comment='交易分类: normal/t0/grid/sell_core')
    pair_deal_no: Mapped[Optional[str]] = mapped_column(String(32),
                                                        comment='做T配对成交编号')
    pair_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), comment='做T套利金额')
    account: Mapped[Optional[str]] = mapped_column(String(16), default='main', comment='main/t0')
    reason: Mapped[Optional[str]] = mapped_column(String(255), comment='交易原因')
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index('idx_code_date', 'code', 'trade_date'),
        Index('idx_contract', 'contract_no'),
        Index('idx_category', 'trade_category'),
        {'comment': '交易记录主表（含信号溯源）'},
    )

    def __repr__(self):
        return (f"<Trade {self.trade_date} {self.code} {self.direction} "
                f"{self.shares}@{self.price} {self.signal_source}>")


# ══════════════════════════════════════════════
# 4. position_state 持仓状态表
# ══════════════════════════════════════════════

class PositionState(Base):
    """持仓状态表 — portfolio.json `_signal_state` 的数据库镜像

    portfolio.json 仍是运行时状态（频繁读写），本表通过 trade.py sync()
    / performance/migrate.py 同步，供绩效分析查询。
    """
    __tablename__ = 'position_state'

    code: Mapped[str] = mapped_column(String(10), primary_key=True, comment='ETF代码')
    name: Mapped[Optional[str]] = mapped_column(String(32), comment='ETF名称')
    shares: Mapped[int] = mapped_column(Integer, default=0, comment='总持仓')
    core_shares: Mapped[int] = mapped_column(Integer, default=0, comment='底仓')
    grid_shares: Mapped[int] = mapped_column(Integer, default=0, comment='网格仓')
    active_shares: Mapped[int] = mapped_column(Integer, default=0, comment='活动仓')
    avg_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), comment='持仓均价')
    entry_avg_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), comment='建仓均价')
    base_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), comment='网格基准价')
    peak_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), comment='峰值价格')
    trailing_stop_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4),
                                                                   comment='移动止盈线')
    stop_level: Mapped[int] = mapped_column(Integer, default=0, comment='止损等级')
    build_phase: Mapped[int] = mapped_column(Integer, default=0, comment='建仓阶段')
    reached_2pct: Mapped[bool] = mapped_column(Boolean, default=False, comment='阶梯止盈2%标记')
    reached_4pct: Mapped[bool] = mapped_column(Boolean, default=False, comment='阶梯止盈4%标记')
    reached_6pct: Mapped[bool] = mapped_column(Boolean, default=False, comment='阶梯止盈6%标记')
    grid_frozen: Mapped[bool] = mapped_column(Boolean, default=False, comment='网格冻结')
    cooldown_until: Mapped[Optional[date]] = mapped_column(Date, comment='冷却截止日')
    empty_days: Mapped[int] = mapped_column(Integer, default=0, comment='空仓天数')
    prev_macd_status: Mapped[Optional[str]] = mapped_column(String(16), comment='上次MACD状态')
    prev_rsi: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), comment='上次RSI')
    daily_trade_log: Mapped[Optional[dict]] = mapped_column(JSON, comment='当日交易计数')
    first_buy_date: Mapped[Optional[date]] = mapped_column(Date, comment='首次建仓日期')
    liquidate_dates: Mapped[Optional[list]] = mapped_column(JSON, comment='清仓日期列表')
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment='更新时间')

    def __repr__(self):
        return f"<PositionState {self.code} {self.shares}@{self.avg_cost}>"


# ══════════════════════════════════════════════
# 5. daily_equity 每日净值表
# ══════════════════════════════════════════════

class DailyEquity(Base):
    """每日净值表 — 资金曲线数据源

    数据来源: portfolio.json 的 account 段，performance/sync_equity.py 每日收盘后写入。
    """
    __tablename__ = 'daily_equity'

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True, comment='交易日期')
    total_asset: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), comment='总资产')
    cash: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), comment='现金')
    market_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), comment='总市值')
    daily_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), comment='当日盈亏')
    daily_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment='当日收益率')
    cumulative_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4),
                                                                 comment='累计收益率')
    max_drawdown: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment='最大回撤')

    def __repr__(self):
        return f"<DailyEquity {self.trade_date} total={self.total_asset}>"


# ══════════════════════════════════════════════
# 6. reconcile_log 对账记录表
# ══════════════════════════════════════════════

class ReconcileLog(Base):
    """对账记录表 — 本地持仓 vs 同花顺实际持仓

    数据来源: performance/reconcile.py，每日（或手动）对账一次。
    """
    __tablename__ = 'reconcile_log'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reconcile_date: Mapped[date] = mapped_column(Date, index=True, comment='对账日期')
    code: Mapped[str] = mapped_column(String(10), comment='ETF代码')
    local_shares: Mapped[Optional[int]] = mapped_column(Integer, comment='本地持仓股数')
    actual_shares: Mapped[Optional[int]] = mapped_column(Integer, comment='实际持仓股数')
    diff_shares: Mapped[Optional[int]] = mapped_column(Integer, comment='差值 local-actual')
    status: Mapped[Optional[str]] = mapped_column(
        Enum(*RECONCILE_STATUS, name='reconcile_status'), comment='对账结果')
    detail: Mapped[Optional[dict]] = mapped_column(JSON, comment='明细')

    def __repr__(self):
        return f"<ReconcileLog {self.reconcile_date} {self.code} {self.status}>"


# ══════════════════════════════════════════════
# 7. sync_log 同步日志表
# ══════════════════════════════════════════════

class SyncLog(Base):
    """同步日志表 — 各类同步任务的执行记录

    sync_type: entrust（委托）/ deal（成交）/ equity（净值）/ reconcile（对账）
    """
    __tablename__ = 'sync_log'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sync_type: Mapped[str] = mapped_column(Enum(*SYNC_TYPE, name='sync_type'),
                                           nullable=False, comment='同步类型')
    sync_date: Mapped[Optional[date]] = mapped_column(Date, comment='同步数据日期')
    total_records: Mapped[Optional[int]] = mapped_column(Integer, default=0, comment='拉取总数')
    inserted: Mapped[Optional[int]] = mapped_column(Integer, default=0, comment='新插入数')
    updated: Mapped[Optional[int]] = mapped_column(Integer, default=0, comment='更新数')
    failed: Mapped[Optional[int]] = mapped_column(Integer, default=0, comment='失败数')
    error_msg: Mapped[Optional[str]] = mapped_column(Text, comment='错误信息')
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment='开始时间')
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment='结束时间')

    def __repr__(self):
        return f"<SyncLog {self.sync_type} {self.sync_date} +{self.inserted}/{self.total_records}>"
