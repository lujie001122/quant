#!/usr/bin/env python3
"""
同花顺全撤 + 杀进程 + 重启脚本

用法:
  python3 scripts/restart_ths.py
  或
  .venv/bin/python3 scripts/restart_ths.py

流程:
  1. 调 EvolvingSim.revokeEntrust('allBuyAndSell') 全撤所有买卖委托
  2. pkill -9 -x 同花顺 杀进程
  3. sleep 3 秒
  4. open -a /Applications/同花顺.app 重启
  5. sleep 5 秒
  6. 打印结果
"""

import sys
import os
import time
import subprocess

# 确保从 quant 项目根目录运行，以正确导入 evolving 模块
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

from evolving.evolving import EvolvingSim


def main():
    no_revoke = "--no-revoke" in sys.argv

    print("=" * 50)
    print("同花顺" + ("杀进程 + 重启（不撤单）" if no_revoke else "全撤 + 杀进程 + 重启"))
    print("=" * 50)

    if no_revoke:
        print("\n[1/4] ⏭️ 跳过撤单（--no-revoke）")
    else:
        # 1. 全撤所有买卖委托
        print("\n[1/4] 全撤所有买卖委托...")
        try:
            e = EvolvingSim()
            result = e.revokeEntrust(revokeType='allBuyAndSell')
            if result is not None:
                ok = result[0] if isinstance(result, (list, tuple)) else result
                msg = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else str(result)
                print(f"  {'✅ 全撤成功' if ok else '⚠️ 全撤失败'}: {msg}")
            else:
                print(f"  ⚠️ 全撤返回 None（可能无委托或同花顺未运行）")
        except Exception as ex:
            print(f"  ⚠️ 全撤异常: {ex}")
        time.sleep(2)

    # 2. 杀同花顺进程
    print("\n[2/4] 杀同花顺进程...")
    try:
        subprocess.run(["pkill", "-9", "-x", "同花顺"], capture_output=True, text=True)
        print("  ✅ pkill -9 -x 同花顺 已执行")
    except Exception as ex:
        print(f"  ⚠️ pkill 执行异常: {ex}")

    # 3. sleep 3 秒
    print("\n[3/4] 等待 3 秒...")
    time.sleep(3)

    # 4. 重启同花顺
    print("\n[4/4] 重启同花顺...")
    try:
        subprocess.run(["open", "-a", "/Applications/同花顺.app"], capture_output=True, text=True)
        print("  ✅ open -a /Applications/同花顺.app 已执行")
    except Exception as ex:
        print(f"  ⚠️ open 执行异常: {ex}")

    # 5. sleep 5 秒
    print("\n等待 5 秒让同花顺完全启动...")
    time.sleep(5)

    # 6. 打印结果
    print("\n" + "=" * 50)
    print("✅ 同花顺全撤 + 杀进程 + 重启 完成")
    print("=" * 50)


if __name__ == '__main__':
    main()