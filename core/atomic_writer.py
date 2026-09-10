#!/usr/bin/env python3
"""
core/atomic_writer.py — 原子文件写入 + 自动备份 + 损坏恢复

解决问题:
  - 写JSON中途崩溃导致文件损坏（半写状态）
  - portfolio.json 损坏后无法自动恢复
  - 无备份机制，出问题只能手动修复

核心功能:
  1. atomic_write_json: 写入临时文件 → fsync → rename（原子操作）
  2. atomic_read_json: 读取失败自动从备份恢复
  3. 自动备份: 每次写入前自动备份到 .bak 文件
  4. 备份轮转: 保留最近 N 份备份 (.bak, .bak1, .bak2)

用法:
  from core.atomic_writer import atomic_write_json, atomic_read_json

  # 写入（原子 + 自动备份）
  atomic_write_json("portfolio.json", data)

  # 读取（损坏自动恢复）
  data = atomic_read_json("portfolio.json")

  # 指定备份数量
  atomic_write_json("portfolio.json", data, max_backups=5)
"""

import json
import os
import shutil
import tempfile
import traceback
from datetime import datetime
from typing import Any, Optional


# ══════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════

DEFAULT_MAX_BACKUPS = 3  # 默认保留3份备份


# ══════════════════════════════════════════════
# 核心函数
# ══════════════════════════════════════════════

def atomic_write_json(
    filepath: str,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    max_backups: int = DEFAULT_MAX_BACKUPS,
    backup_suffix: str = ".bak",
) -> bool:
    """原子写入JSON文件

    流程:
      1. 备份当前文件（如存在）
      2. 写入临时文件（同目录）
      3. fsync 确保落盘
      4. os.replace 原子替换

    参数:
      filepath: 目标文件路径
      data: 要写入的数据（必须可JSON序列化）
      indent: JSON缩进
      ensure_ascii: JSON ensure_ascii 参数
      max_backups: 保留的备份数量
      backup_suffix: 备份文件后缀

    返回:
      bool: 写入是否成功
    """
    filepath = os.path.abspath(filepath)

    # ── Step 1: 备份当前文件 ──
    if os.path.exists(filepath):
        _rotate_backups(filepath, max_backups=max_backups, backup_suffix=backup_suffix)

    # ── Step 2: 写入临时文件 ──
    dir_name = os.path.dirname(filepath)
    os.makedirs(dir_name, exist_ok=True)

    try:
        # 使用同目录临时文件，确保同一文件系统（rename才能原子）
        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp",
            prefix=".atomic_",
            dir=dir_name,
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
                f.flush()
                os.fsync(f.fileno())

            # ── Step 3: 原子替换 ──
            os.replace(tmp_path, filepath)
            return True

        except Exception:
            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    except Exception as e:
        print(f"[atomic_writer] 写入失败 {filepath}: {e}")
        traceback.print_exc()
        return False


def atomic_read_json(
    filepath: str,
    *,
    default: Any = None,
    max_backups: int = DEFAULT_MAX_BACKUPS,
    backup_suffix: str = ".bak",
) -> Any:
    """原子读取JSON文件，损坏时自动从备份恢复

    流程:
      1. 尝试读取主文件
      2. 失败则按 .bak → .bak1 → .bak2 顺序尝试备份
      3. 恢复成功后自动写回主文件
      4. 全部失败返回 default

    参数:
      filepath: 文件路径
      default: 全部失败时返回的默认值
      max_backups: 最大备份数
      backup_suffix: 备份后缀

    返回:
      解析后的数据，或 default
    """
    filepath = os.path.abspath(filepath)

    # ── Step 1: 尝试读取主文件 ──
    data = _try_read_json(filepath)
    if data is not None:
        return data

    print(f"[atomic_writer] 主文件损坏: {filepath}，尝试从备份恢复...")

    # ── Step 2: 按顺序尝试备份文件 ──
    for i in range(max_backups):
        bak_path = filepath + backup_suffix if i == 0 else f"{filepath}{backup_suffix}{i}"
        data = _try_read_json(bak_path)
        if data is not None:
            print(f"[atomic_writer] 从备份恢复成功: {bak_path}")
            # 恢复成功后写回主文件
            atomic_write_json(filepath, data, max_backups=max_backups, backup_suffix=backup_suffix)
            return data

    # ── Step 3: 全部失败 ──
    print(f"[atomic_writer] 所有备份均失败，返回默认值")
    return default


def _try_read_json(filepath: str) -> Optional[Any]:
    """尝试读取并解析JSON文件

    返回:
      解析后的数据，或 None（文件不存在/损坏）
    """
    if not os.path.exists(filepath):
        return None

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        print(f"[atomic_writer] 读取失败 {filepath}: {e}")
        return None


def _rotate_backups(
    filepath: str,
    max_backups: int = DEFAULT_MAX_BACKUPS,
    backup_suffix: str = ".bak",
) -> None:
    """轮转备份文件

    保留最近 max_backups 份备份:
      .bak2 ← .bak1 (删除最老的 .bak2)
      .bak1 ← .bak
      .bak  ← 当前文件
    """
    # 从最老的开始删除/移动
    for i in range(max_backups - 1, 0, -1):
        older = f"{filepath}{backup_suffix}{i}"
        newer = filepath + backup_suffix if i == 1 else f"{filepath}{backup_suffix}{i - 1}"
        if os.path.exists(newer):
            try:
                if os.path.exists(older):
                    os.remove(older)
                shutil.move(newer, older)
            except OSError as e:
                print(f"[atomic_writer] 备份轮转失败 {newer} -> {older}: {e}")

    # 备份当前文件到 .bak
    bak_path = filepath + backup_suffix
    try:
        if os.path.exists(bak_path):
            # .bak → .bak1
            bak1_path = f"{filepath}{backup_suffix}1"
            if os.path.exists(bak1_path):
                os.remove(bak1_path)
            shutil.move(bak_path, bak1_path)
        shutil.copy2(filepath, bak_path)
    except OSError as e:
        print(f"[atomic_writer] 备份失败 {filepath} -> {bak_path}: {e}")


# ══════════════════════════════════════════════
# 便捷函数
# ══════════════════════════════════════════════

def safe_save_portfolio(data: Any, filepath: str = "portfolio.json") -> bool:
    """安全保存 portfolio.json（原子写入 + 备份）

    参数:
      data: portfolio 数据
      filepath: 文件路径（默认项目根目录下的 portfolio.json）

    返回:
      bool: 是否成功
    """
    if not os.path.isabs(filepath):
        filepath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), filepath)
    return atomic_write_json(filepath, data)


def safe_load_portfolio(filepath: str = "portfolio.json") -> Any:
    """安全读取 portfolio.json（损坏自动恢复）

    参数:
      filepath: 文件路径

    返回:
      portfolio 数据，或 None
    """
    if not os.path.isabs(filepath):
        filepath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), filepath)
    return atomic_read_json(filepath, default=None)


def get_backup_info(filepath: str, backup_suffix: str = ".bak") -> dict:
    """获取文件的备份信息

    返回:
      dict: {主文件大小, 备份列表: [{路径, 大小, 修改时间}]}
    """
    filepath = os.path.abspath(filepath)
    info = {
        "main_file": filepath,
        "main_exists": os.path.exists(filepath),
        "main_size": os.path.getsize(filepath) if os.path.exists(filepath) else 0,
        "backups": [],
    }

    # .bak
    bak_path = filepath + backup_suffix
    if os.path.exists(bak_path):
        info["backups"].append({
            "path": bak_path,
            "size": os.path.getsize(bak_path),
            "mtime": datetime.fromtimestamp(os.path.getmtime(bak_path)).isoformat(),
        })

    # .bak1, .bak2, ...
    for i in range(1, 10):
        bak_path = f"{filepath}{backup_suffix}{i}"
        if os.path.exists(bak_path):
            info["backups"].append({
                "path": bak_path,
                "size": os.path.getsize(bak_path),
                "mtime": datetime.fromtimestamp(os.path.getmtime(bak_path)).isoformat(),
            })
        else:
            break

    return info
