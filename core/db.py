#!/usr/bin/env python3
"""
core/db.py — 数据库引擎 + Session 管理

- 配置从 config.yaml 的 database 段读取:
    database:
      url: "mysql+pymysql://root:***@127.0.0.1:3306/quant?charset=utf8mb4"
      echo: false
      pool_size: 10
      max_overflow: 20
      pool_recycle: 3600
      pool_pre_ping: true
- 惰性单例引擎 + sessionmaker
- get_session() 为 contextmanager: 正常退出 commit, 异常 rollback
- init_db(): Base.metadata.create_all() 自动建表（幂等）

初始化命令:
  python3 -c "from core.db import init_db; init_db()"
"""

import os
from contextlib import contextmanager

import yaml
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.models import Base

# 项目根目录（core/ 的上一级）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_PROJECT_ROOT, "config.yaml")

# 兜底配置（config.yaml 缺失 database 段时使用）
_DEFAULT_DB_CONFIG = {
    "url": "mysql+pymysql://root:lujie001122@127.0.0.1:3306/quant?charset=utf8mb4",
    "echo": False,
    "pool_size": 10,
    "max_overflow": 20,
    "pool_recycle": 3600,
    "pool_pre_ping": True,
}

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _load_db_config() -> dict:
    """读取 config.yaml 的 database 段，缺省项用默认值兜底"""
    cfg = dict(_DEFAULT_DB_CONFIG)
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            full = yaml.safe_load(f) or {}
        db_cfg = full.get("database") or {}
        if isinstance(db_cfg, dict):
            cfg.update({k: v for k, v in db_cfg.items() if v is not None})
    except FileNotFoundError:
        pass
    return cfg


def _get_engine() -> Engine:
    """惰性创建全局引擎单例"""
    global _engine
    if _engine is None:
        cfg = _load_db_config()
        _engine = create_engine(
            cfg["url"],
            echo=bool(cfg.get("echo", False)),
            pool_size=int(cfg.get("pool_size", 10)),
            max_overflow=int(cfg.get("max_overflow", 20)),
            pool_recycle=int(cfg.get("pool_recycle", 3600)),
            pool_pre_ping=bool(cfg.get("pool_pre_ping", True)),
            future=True,
        )
    return _engine


def _get_session_factory() -> sessionmaker:
    """惰性创建全局 sessionmaker 单例"""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=_get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _SessionLocal


@contextmanager
def get_session():
    """获取数据库会话（contextmanager）

    用法:
        with get_session() as s:
            s.add(obj)

    正常退出自动 commit，异常自动 rollback 并 raise。
    """
    s: Session = _get_session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    """自动建表（幂等，已存在的表不会重建）"""
    Base.metadata.create_all(_get_engine())


def dispose_engine() -> None:
    """释放引擎连接池（测试或进程退出时用）"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
        _engine = None
    _SessionLocal = None


if __name__ == "__main__":
    init_db()
    print("✅ 数据库表已就绪")
