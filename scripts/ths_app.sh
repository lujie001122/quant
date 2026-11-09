#!/usr/bin/env bash
# scripts/ths_app.sh — start / stop / status 同花顺副本
# 用法: ths_app.sh start | stop | status

set -euo pipefail

APP_COPY="/tmp/thsx2.app"
BUNDLE_ID="cn.com.10jqka.thsx"
PROC_NAME="THSX"
ORIG_NAME="同花顺"

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

    # 启动副本
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
    start)  cmd_start  ;;
    stop)   cmd_stop   ;;
    status) cmd_status ;;
    *)
        echo "usage: $0 start | stop | status" >&2
        exit 1
        ;;
esac
