#!/usr/bin/env python3
"""
ths_login.py — 自动登录同花顺副本并进入模拟仓。
退出码: 0 成功 / 1 失败
"""
import json
import os
import subprocess
import sys
import time

CRED_FILE   = os.path.expanduser("~/.ths_cred.json")
BUNDLE_ID   = "cn.com.10jqka.thsx"
AGENT_JS    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ths_agent.js")

# ── 读凭据 ────────────────────────────────────────────────────────────────────

def load_creds():
    with open(CRED_FILE) as f:
        d = json.load(f)
    return d["account"], d["password"]

# ── AppleScript 辅助 ──────────────────────────────────────────────────────────

def osa(script: str, timeout: int = 20) -> str:
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()

def osa_ok(script: str, timeout: int = 20) -> bool:
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0

# ── 步骤 ──────────────────────────────────────────────────────────────────────

def step_ensure_window():
    """如果没有窗口先激活 + Cmd+N。"""
    osa(f"""
tell application "System Events"
    set theProcs to processes whose bundle identifier is "{BUNDLE_ID}"
    if theProcs is {{}} then return
    set theProc to item 1 of theProcs
    if count of (windows of theProc) = 0 then
        set frontmost of theProc to true
        delay 0.5
        keystroke "n" using command down
        delay 2
    end if
end tell""")


def step_dismiss_reopen():
    """处理「重新打开」对话框。"""
    osa(f"""
tell application "System Events"
    set theProcs to processes whose bundle identifier is "{BUNDLE_ID}"
    if theProcs is {{}} then return
    set theProc to item 1 of theProcs
    try
        click button "重新打开" of window 1 of theProc
        delay 1
    end try
end tell""")


def step_login(account: str, password: str) -> bool:
    """
    在登录窗口填账号密码并点登录。
    先检测是否已有登录窗口，没有就先走交易面板 → 立即登录。
    密码不打印。
    """
    # 尝试直接往已有登录 sheet/window 填写
    filled = osa(f"""
tell application "System Events"
    set theProcs to processes whose bundle identifier is "{BUNDLE_ID}"
    if theProcs is {{}} then return "no_proc"
    set theProc to item 1 of theProcs
    -- 先看有没有 sheet（登录弹窗通常是 sheet）
    try
        set sh to sheet 1 of window 1 of theProc
        -- 找账号/密码文本框（一般 text field 1 = 账号，2 = 密码）
        set value of text field 1 of sh to "{account}"
        set value of text field 2 of sh to "{password}"
        click button 1 of sh
        return "done"
    end try
    return "no_sheet"
end tell""")

    if filled == "done":
        print(f"已提交登录（账号后四位: ...{account[-4:]}）")
        return True

    # 没有 sheet，尝试通过交易面板进入登录
    print("未找到登录 sheet，尝试 交易→A股→立即登录 路径")
    osa(f"""
tell application "System Events"
    set theProcs to processes whose bundle identifier is "{BUNDLE_ID}"
    if theProcs is {{}} then return
    set theProc to item 1 of theProcs
    try
        click button 6 of window 1 of theProc
        delay 0.8
    end try
    try
        click button "A股" of window 1 of theProc
        delay 0.8
    end try
    try
        click button "立即登录" of window 1 of theProc
        delay 1
    end try
end tell""")

    # 再填登录表单
    filled2 = osa(f"""
tell application "System Events"
    set theProcs to processes whose bundle identifier is "{BUNDLE_ID}"
    if theProcs is {{}} then return "no_proc"
    set theProc to item 1 of theProcs
    try
        set sh to sheet 1 of window 1 of theProc
        set value of text field 1 of sh to "{account}"
        set value of text field 2 of sh to "{password}"
        click button 1 of sh
        return "done"
    end try
    return "no_sheet"
end tell""")

    if filled2 == "done":
        print(f"已提交登录（账号后四位: ...{account[-4:]}）")
        return True

    print("ERROR: 无法定位登录 sheet", file=sys.stderr)
    return False


def step_open_simtrade():
    """点 交易(button 6) → A股 → 模拟，容忍各步失败。"""
    osa(f"""
tell application "System Events"
    set theProcs to processes whose bundle identifier is "{BUNDLE_ID}"
    if theProcs is {{}} then return
    set theProc to item 1 of theProcs
    try
        click button 6 of window 1 of theProc
        delay 1
    end try
    try
        click button "A股" of window 1 of theProc
        delay 0.8
    end try
    try
        click button "模拟" of window 1 of theProc
        delay 0.8
    end try
end tell""")


def frida_check_login() -> bool:
    """用 Frida 确认 GetMoniUserArr count > 0 且 getActiveUser != nil。"""
    # 用 pgrep -x 拿 PID（Frida 看到的进程名是「同花顺」不是 THSX）
    try:
        pid_out = subprocess.check_output(["pgrep", "-x", "THSX"], text=True).strip()
    except subprocess.CalledProcessError:
        print("Frida 检查失败: THSX 进程不存在", file=sys.stderr)
        return False
    pid = int(pid_out.split()[0])

    # 把脚本写到临时文件，避免路径转义问题
    import tempfile
    helper = f"""
import frida, sys, json
dev = frida.get_local_device()
sess = dev.attach({pid})
sc = sess.create_script(open({AGENT_JS!r}).read())
sc.on('message', lambda m, d: None)
sc.load()
result = sc.exports_sync.state()
print(json.dumps(result))
sess.detach()
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(helper)
        tmp = f.name

    try:
        r = subprocess.run(
            [sys.executable, tmp],
            capture_output=True, text=True, timeout=30
        )
    finally:
        os.unlink(tmp)

    if r.returncode != 0:
        print(f"Frida 检查失败: {r.stderr.strip()[:300]}", file=sys.stderr)
        return False
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        print(f"Frida 输出解析失败: {r.stdout[:200]!r}", file=sys.stderr)
        return False

    moni = data.get("moni_count", 0)
    active = data.get("active_user_ptr")
    print(f"Frida state: moni_count={moni}  active_user_ptr={active}  active_is_moni={data.get('active_is_moni')}")
    return int(moni) > 0 and active is not None


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    account, password = load_creds()

    # 1. 保证窗口存在
    step_ensure_window()
    time.sleep(1)

    # 2. 处理「重新打开」对话框
    step_dismiss_reopen()

    # 3. 登录
    if not step_login(account, password):
        sys.exit(1)

    # 等登录完成，最多 30 秒
    print("等待登录完成...")
    for i in range(30):
        time.sleep(1)
        if frida_check_login():
            print("登录成功 ✓")
            break
    else:
        print("ERROR: 登录超时（30s 内 GetMoniUserArr 仍为空）", file=sys.stderr)
        sys.exit(1)

    # 4. 进模拟仓
    step_open_simtrade()
    print("已点击 交易→A股→模拟")

    sys.exit(0)


if __name__ == "__main__":
    main()
