#!/usr/bin/env bash
# scripts/ths_app.sh — start / stop / status / rebuild 同花顺副本
# 用法: ths_app.sh start | stop | status | rebuild
#
# 副本为什么必须重建（每次重启电脑 /tmp 被清空后都要）：
#   原版是 App Store 包，带 hardened runtime（codesign flags=0x10000(runtime)）+ 沙箱
#   entitlements。frida 的 task_for_pid 被 taskgated 拒掉，attach/spawn 都报
#   "unable to access process ... from the current user account"。
#   重建 = 换 bundle id（避免和 /Applications 原版撞车）+ 去掉 entitlements 重签 ad-hoc，
#   签出来的进程没有 hardened runtime，frida 可注入；登录态仍读
#   ~/Library/Preferences/cn.com.10jqka.thsx.plist（副本不沙箱，数据不丢）。

set -euo pipefail

APP_COPY="/tmp/ths_lab/THSX.app"  # 全仓唯一启动路径（ths_app.py）
APP_SRC="/Applications/同花顺.app"
BUNDLE_ID="cn.com.10jqka.thsx"
PROC_NAME="THSX.app/Contents/MacOS/同花顺"
ORIG_NAME="同花顺"

# 副本是否可用：存在 + bundle id 正确 + 没有 hardened runtime + 签名自校验通过
_copy_ok() {
    [[ -d "$APP_COPY" && -x "$APP_COPY/Contents/MacOS/同花顺" ]] || return 1
    [[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' \
          "$APP_COPY/Contents/Info.plist" 2>/dev/null || true)" == "$BUNDLE_ID" ]] || return 1
    codesign -dv "$APP_COPY" 2>&1 | grep -q "flags=.*(runtime)" && return 1
    codesign --verify --deep --strict "$APP_COPY" >/dev/null 2>&1 || return 1
    return 0
}

# 重建副本（幂等）：ditto 原版 → 改 bundle id → 清 xattr → 去 entitlements 重签 ad-hoc
rebuild_copy() {
    if _copy_ok; then
        echo "THSX copy ok: $APP_COPY (bundle=$BUNDLE_ID, adhoc)"
        return 0
    fi
    [[ -d "$APP_SRC" ]] || { echo "ERROR: 原版不存在: $APP_SRC" >&2; return 1; }
    local tmp="$APP_COPY.rebuild.$$"
    echo "rebuilding $APP_COPY from $APP_SRC ..."
    rm -rf "$tmp"
    mkdir -p "$(dirname "$APP_COPY")"
    ditto "$APP_SRC" "$tmp"
    /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $BUNDLE_ID" "$tmp/Contents/Info.plist"
    xattr -cr "$tmp"
    # 不传 --entitlements / --options runtime：拿掉沙箱与 hardened runtime，只留 adhoc 签名
    codesign --force --deep --sign - "$tmp" >/dev/null 2>&1
    codesign --verify --deep --strict "$tmp" >/dev/null 2>&1
    rm -rf "$APP_COPY"
    mv "$tmp" "$APP_COPY"
    _copy_ok
    echo "rebuilt $APP_COPY (bundle=$BUNDLE_ID, adhoc, no hardened runtime)"
}

_pgrep_thsx() { pgrep -f "$PROC_NAME" 2>/dev/null || true; }
_pgrep_orig() { pgrep -f "$ORIG_NAME"  2>/dev/null || true; }

ensure_window() {
    osascript -e "
tell application \"System Events\"
    set theProcs to processes whose bundle identifier is \"$BUNDLE_ID\"
    if theProcs is {} then return
    set theProc to item 1 of theProcs
    set winCount to count of (windows of theProc)
    if winCount = 0 then
        set frontmost of theProc to true
        delay 0.5
        keystroke \"n\" using command down
        delay 1
    end if
end tell" 2>/dev/null || true
}

cmd_start() {
    # 杀原版
    local orig_pids
    orig_pids=$(_pgrep_orig)
    if [[ -n "$orig_pids" ]]; then
        echo "killing 原版 同花顺 pid(s): $orig_pids"
        echo "$orig_pids" | xargs kill -9 2>/dev/null || true
        sleep 0.5
    fi

    # 已在跑就直接保证窗口然后登录
    local pids
    pids=$(_pgrep_thsx)
    if [[ -n "$pids" ]]; then
        echo "THSX already running  pid=$(echo "$pids" | head -1)"
        ensure_window
        local login_py2
        login_py2="$(cd "$(dirname "$0")" && pwd)/ths_login.py"
        local venv_py2
        venv_py2="$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/python3"
        local py2="$venv_py2"
        [[ -x "$py2" ]] || py2="python3"
        echo "auto-login via $login_py2 ..."
        if "$py2" "$login_py2"; then
            echo "login ok"
        else
            echo "ERROR: auto-login failed" >&2
            exit 1
        fi
        return 0
    fi

    # 启动副本（副本不存在/签名不对 —— 比如刚重启过电脑 —— 先重建）
    rebuild_copy
    echo "launching $APP_COPY ..."
    open -a "$APP_COPY" 2>/dev/null

    # 等进程出现，最多 15 秒
    local i=0
    while [[ $i -lt 30 ]]; do
        pids=$(_pgrep_thsx)
        [[ -n "$pids" ]] && break
        sleep 0.5
        (( i++ )) || true
    done

    if [[ -z "$pids" ]]; then
        echo "ERROR: THSX did not start within 15s" >&2
        exit 1
    fi

    sleep 2
    ensure_window

    local pid
    pid=$(echo "$pids" | head -1)
    echo "started  pid=$pid"

    # 自动登录
    local login_py
    login_py="$(cd "$(dirname "$0")" && pwd)/ths_login.py"
    local venv_py
    venv_py="$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/python3"
    local py="$venv_py"
    [[ -x "$py" ]] || py="python3"
    echo "auto-login via $login_py ..."
    if "$py" "$login_py"; then
        echo "login ok"
    else
        echo "ERROR: auto-login failed" >&2
        exit 1
    fi
}

cmd_stop() {
    local pids
    pids=$(_pgrep_thsx)
    if [[ -z "$pids" ]]; then
        echo "THSX not running"
        return 0
    fi
    echo "killing THSX pid(s): $pids"
    echo "$pids" | xargs kill -9 2>/dev/null || true
    echo "stopped"
}

cmd_status() {
    local pids
    pids=$(_pgrep_thsx)
    if [[ -n "$pids" ]]; then
        local pid
        pid=$(echo "$pids" | head -1)
        echo "running  pid=$pid"
        # 窗口数
        local wins
        wins=$(osascript -e "
tell application \"System Events\"
    set theProcs to processes whose bundle identifier is \"$BUNDLE_ID\"
    if theProcs is {} then return 0
    count of (windows of item 1 of theProcs)
end tell" 2>/dev/null) || wins="?"
        echo "windows=$wins"
    else
        echo "not running"
    fi

    # 原版
    local orig
    orig=$(_pgrep_orig)
    if [[ -n "$orig" ]]; then
        echo "WARNING: 原版 同花顺 is running pid(s): $orig"
    fi
}

case "${1:-}" in
    start)   cmd_start   ;;
    stop)    cmd_stop    ;;
    status)  cmd_status  ;;
    rebuild) rebuild_copy ;;
    *)
        echo "usage: $0 start | stop | status | rebuild" >&2
        exit 1
        ;;
esac
