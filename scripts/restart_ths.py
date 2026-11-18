#!/usr/bin/env python3
"""同花顺重启脚本。

用法:
  python3 scripts/restart_ths.py               # 先撤掉全部未成交委托，再重启（09:30 口径）
  python3 scripts/restart_ths.py --no-revoke   # 只重启，不撤单（13:00 口径）
  python3 scripts/restart_ths.py --dry-run     # 只打印计划，不真撤单/不重启/不等待

铁律:
  1. 锁屏 → 不 kill、不重启，打印 [SKIP] 退出码 0（App 已登录时重启是纯风险）。
  2. 重启后必须等到 moni_count>0 / active_is_moni=True / positions 缓存>0 行才返回；
     默认最多等 120 秒（RESTART_LOGIN_TIMEOUT 可覆盖），超时退出码 2，
     cron 的 && 链自然跳过后续下单——宁可不下单，也不在没登录时瞎跑。
  3. 撤单走 frida 接口（ThsBroker.cancel_contract real=True）；撤单失败只打印，不阻塞重启。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ths import Ths, ThsError                        # 桥客户端（登录态/持仓缓存）
from ths_app import (APP_PATH, count_windows, ensure_window,       # 唯一启动/重启入口
                     force_restart, screen_locked, wait_window)

NO_REVOKE = "--no-revoke" in sys.argv
DRY_RUN = "--dry-run" in sys.argv


def _login_timeout() -> float:
    return float(os.environ.get("RESTART_LOGIN_TIMEOUT", "120"))


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


def restart() -> float:
    """显式人工/定时重启：这是全仓唯一允许杀同花顺的入口。返回发起重启的时刻。"""
    if DRY_RUN:
        print(f"[DRY-RUN] 将执行: SIGKILL 现有同花顺 && open {APP_PATH}")
        return time.time()
    pid = force_restart()
    if not wait_window():
        print(f"⚠ 已启动 {APP_PATH} pid={pid}，但窗口暂不可用（UI 未就绪）")
    print(f"   THSX.app pid={pid}")
    return time.time()


def wait_login_ready(ths: Ths, timeout: float) -> int:
    """等模拟仓登录真正可用，返回持仓行数。

    判据：moni_count>0 且 active_is_moni=True，且持仓查询能正常返回。
    ★ 空仓（0 行）也算成功：持仓缓存 >0 行的旧判据在清仓后永远等不到，
      会把「已登录的空账户」误判成登录失败。
    ★ 没窗口先补一个：SIGKILL 后重开的 App 可能零窗口，模拟仓登录入口
      （JYMoniLoginWindowController）要主窗口在才通，否则 loginMoni 一直报
      「moni user 未落地」。
    """
    deadline = time.time() + timeout
    last = "未开始"
    win_tries = 0
    while True:
        try:
            if count_windows() == 0 and win_tries < 6:
                win_tries += 1
                ensure_window()
                time.sleep(2)
            ths.ensure_login()
            st = ths.state()
            last = (f"moni_count={st.get('moni_count')} "
                    f"active_is_moni={st.get('active_is_moni')}")
            if st.get("moni_count", 0) > 0 and st.get("active_is_moni"):
                try:
                    rows = ths.positions()
                except ThsError as ex:
                    last = f"{last} 持仓查询失败: {ex.error}"
                else:
                    print(f"   持仓 {len(rows)} 行")
                    return len(rows)
        except ThsError as ex:
            last = f"登录失败: {ex.error}"
        elapsed = time.time() - (deadline - timeout)
        if time.time() >= deadline:
            raise ThsError("wait_login", f"{timeout:.0f}s 内未完成模拟仓登录", last=last)
        print(f"  ⏳ 等登录 {elapsed:.0f}s/{timeout:.0f}s  {last}")
        time.sleep(5)


def main() -> int:
    if screen_locked():
        print("[SKIP] 屏幕已锁定，跳过重启（App 保持当前状态）")
        return 0

    if NO_REVOKE:
        print("重启同花顺（--no-revoke：不撤单）")
    else:
        print("重启同花顺（先撤全部未成交委托）")
    if not NO_REVOKE:
        revoke_all_pending()

    t0 = restart()
    if DRY_RUN:
        print("[DRY-RUN] 不重启、不等登录")
        return 0

    timeout = _login_timeout()
    print(f"⏳ 等模拟仓登录完成（loginMoni/loginSim 轮询，最多 {timeout:.0f} 秒）...")
    try:
        ths = Ths()
    except ThsError as ex:
        print(f"[FAIL] 重启后 {timeout:.0f} 秒内未完成模拟仓登录")
        print(f"  桥不可用: {ex}")
        return 2
    try:
        n_pos = wait_login_ready(ths, min(timeout, max(1.0, timeout - (time.time() - t0))))
    except ThsError as ex:
        print(f"[FAIL] 重启后 {timeout:.0f} 秒内未完成模拟仓登录")
        print(f"  最后状态: {ex.extra.get('last') or ex.error}")
        return 2
    finally:
        ths.close()

    print(f"✅ 模拟仓登录完成，持仓 {n_pos} 行，耗时 {time.time() - t0:.1f}s（含重启）")
    print("✅ 同花顺已重启（不撤单）" if NO_REVOKE else "✅ 同花顺已重启（已先撤单）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
