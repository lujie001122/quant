#!/usr/bin/env python3
"""同花顺重启脚本。

用法:
  python3 scripts/restart_ths.py               # 先撤掉全部未成交委托，再重启（09:30 口径）
  python3 scripts/restart_ths.py --no-revoke   # 只重启，不撤单（13:00 口径）
  python3 scripts/restart_ths.py --dry-run     # 只打印计划，不真撤单/不重启

撤单走 frida 接口（ThsBroker.cancel_contract real=True）；撤单失败只打印，不阻塞重启。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NO_REVOKE = "--no-revoke" in sys.argv
DRY_RUN = "--dry-run" in sys.argv


def _is_pending(order: dict) -> bool:
    """未成交（含部分成交/已报）才撤；已成交/已撤/废单跳过。"""
    st = str(order.get("status") or "")
    if "撤" in st or "废" in st:
        return False
    return not ("全部成交" in st or "已成交" in st)


def revoke_all_pending() -> None:
    """撤掉全部未成交委托。任何失败只打印，不阻塞后续重启。"""
    try:
        from broker_frida import ThsBroker
        broker = ThsBroker(timeout=30)
        orders = broker.get_orders() or []
    except Exception as ex:
        msg = str(ex)
        if "为空" in msg or "0 行" in msg:
            print("无未成交委托（委托表为空）")
        else:
            print(f"⚠️ 读取委托表失败: {msg}（不阻塞重启，继续）")
        return

    pending = [o for o in orders if _is_pending(o)]
    if not pending:
        print("无未成交委托，跳过撤单")
        return

    print(f"发现 {len(pending)} 笔未成交委托，开始撤单:")
    for o in pending:
        contract = str(o.get("contract") or "")
        desc = (f"{o.get('code')} {o.get('direction')} {o.get('shares')}股 "
                f"合同号={contract or '无'} 状态={o.get('status') or '未知'}")
        if DRY_RUN:
            print(f"  [DRY-RUN] 将撤单: {desc}")
            continue
        print(f"  撤单: {desc}")
        try:
            broker.cancel_contract(contract, real=True)
            print(f"    ✅ 已撤 {contract}")
        except Exception as ex:
            print(f"    ⚠️ 撤单失败（不阻塞重启）: {ex}")


def restart() -> None:
    if DRY_RUN:
        print("[DRY-RUN] 将执行: pkill -9 -x 同花顺 && open -a /Applications/同花顺.app")
        return
    os.system("pkill -9 -x 同花顺 2>/dev/null")
    time.sleep(3)
    os.system("open -a /Applications/同花顺.app")
    time.sleep(5)


def main() -> None:
    if NO_REVOKE:
        print("重启同花顺（--no-revoke：不撤单）")
    else:
        print("重启同花顺（先撤全部未成交委托）")
    if not NO_REVOKE:
        revoke_all_pending()
    restart()
    print("✅ 同花顺已重启（不撤单）" if NO_REVOKE else "✅ 同花顺已重启（已先撤单）")


if __name__ == "__main__":
    main()
