#!/usr/bin/env python3
"""
record_login.py — 录制同花顺登录调用链

用法：
  python3 scripts/record_login.py [--pid PID] [--out /tmp/login_trace.json]

流程：
  1. attach 到副本进程（先 attach 再装 hook，时序铁律）
  2. 装宽谱 hook：
     - UserManager 所有实例方法 + 类方法（class_copyMethodList）
     - UserModel 名字含 login/loginComplete/setActive/active/connect/commu 的方法
     - TradeRequest setter: setNsInsid: / setRequestParamStr:Value: / setRequestParamInteger:Value:
  3. 点 button 6（交易）→ 等 4s → 点「模拟」→ 等 10s
  4. dump 调用链到 --out（默认 /tmp/login_trace.json）
"""
import argparse
import json
import os
import subprocess
import sys
import time

try:
    import frida
except ImportError:
    sys.exit("需要 frida：pip install frida")

# ── agent JS（内联，不依赖 ths_agent.js）──────────────────────────────────────
RECORD_JS = r"""
'use strict';

var libobjc = Module.load('/usr/lib/libobjc.A.dylib');
var _objc_getClass          = new NativeFunction(libobjc.getExportByName('objc_getClass'),            'pointer', ['pointer']);
var _class_copyMethodList   = new NativeFunction(libobjc.getExportByName('class_copyMethodList'),     'pointer', ['pointer','pointer']);
var _method_getName         = new NativeFunction(libobjc.getExportByName('method_getName'),           'pointer', ['pointer']);
var _method_getImplementation=new NativeFunction(libobjc.getExportByName('method_getImplementation'), 'pointer', ['pointer']);
var _sel_getName            = new NativeFunction(libobjc.getExportByName('sel_getName'),              'pointer', ['pointer']);
var _class_getInstanceMethod= new NativeFunction(libobjc.getExportByName('class_getInstanceMethod'),  'pointer', ['pointer','pointer']);
var _class_getClassMethod   = new NativeFunction(libobjc.getExportByName('class_getClassMethod'),     'pointer', ['pointer','pointer']);
var _sel_registerName       = new NativeFunction(libobjc.getExportByName('sel_registerName'),         'pointer', ['pointer']);
var _objc_msgSend           = libobjc.getExportByName('objc_msgSend');
var _object_getClass        = new NativeFunction(libobjc.getExportByName('object_getClass'),          'pointer', ['pointer']);
var _class_getName          = new NativeFunction(libobjc.getExportByName('class_getName'),            'pointer', ['pointer']);
var _class_getSuperclass    = new NativeFunction(libobjc.getExportByName('class_getSuperclass'),      'pointer', ['pointer']);

var msg0p = new NativeFunction(_objc_msgSend, 'pointer', ['pointer','pointer']);
var msg0u = new NativeFunction(_objc_msgSend, 'uint64',  ['pointer','pointer']);

function sel(name) { return _sel_registerName(Memory.allocUtf8String(name)); }
function cls(name) {
    var p = _objc_getClass(Memory.allocUtf8String(name));
    return (p && !p.isNull()) ? p : null;
}
function nsStr(p) {
    if (!p || p.isNull()) return null;
    try {
        var utf8 = msg0p(p, sel('UTF8String'));
        return utf8 && !utf8.isNull() ? utf8.readUtf8String() : null;
    } catch(e) { return null; }
}
function classNameOf(objPtr) {
    try {
        if (!objPtr || objPtr.isNull()) return null;
        var c = _object_getClass(objPtr);
        if (!c || c.isNull()) return null;
        var np = _class_getName(c);
        return np && !np.isNull() ? np.readUtf8String() : null;
    } catch(e) { return null; }
}
function safeDesc(p) {
    if (!p || p.isNull()) return null;
    try {
        var cn = classNameOf(p);
        if (!cn) return p.toString();
        // NSString → 直接读
        if (cn === '__NSCFString' || cn === 'NSTaggedPointerString' || cn.indexOf('NSString') >= 0) {
            var s = nsStr(p);
            return s !== null ? ('"' + s.substring(0, 80) + '"') : ('<' + cn + ':' + p + '>');
        }
        // NSNumber
        if (cn.indexOf('NSNumber') >= 0 || cn.indexOf('__NSCFNumber') >= 0) {
            try {
                var iv = new NativeFunction(_objc_msgSend, 'int64', ['pointer','pointer'])(p, sel('longLongValue'));
                return String(iv);
            } catch(e) {}
        }
        return '<' + cn + ':' + p + '>';
    } catch(e) { return p.toString(); }
}

// ── trace log ─────────────────────────────────────────────────────────────────
var traceLog = [];
var traceSeq = 0;
// 重入锁（防止 hook 里调 ObjC 再次命中自身）
var _busy = false;

function pushTrace(rec) {
    rec.seq = ++traceSeq;
    traceLog.push(rec);
    if (traceLog.length > 2000) traceLog.shift();
}

// ── 枚举一个类的所有方法（实例或类方法）─────────────────────────────────────
function getMethodList(clsPtr, isClass) {
    var targetCls = isClass ? _object_getClass(clsPtr) : clsPtr;
    if (!targetCls || targetCls.isNull()) return [];
    var countBuf = Memory.alloc(4);
    countBuf.writeU32(0);
    var methods = _class_copyMethodList(targetCls, countBuf);
    var count = countBuf.readU32();
    var result = [];
    for (var i = 0; i < count; i++) {
        var m = methods.add(i * Process.pointerSize).readPointer();
        if (!m || m.isNull()) continue;
        try {
            var sn = _sel_getName(_method_getName(m));
            var selName = sn && !sn.isNull() ? sn.readUtf8String() : null;
            if (selName) result.push({ selName: selName, imp: _method_getImplementation(m) });
        } catch(e) {}
    }
    // 注意：class_copyMethodList 返回的 methods 需要 free，但在 agent 生命周期内可接受内存泄漏
    return result;
}

// ── 安全 hook 单个 IMP ─────────────────────────────────────────────────────
function safeHook(clsName, selName, imp, isClass) {
    if (!imp || imp.isNull()) return false;
    try {
        Interceptor.attach(imp, {
            onEnter: function(args) {
                if (_busy) return;
                _busy = true;
                try {
                    var rec = {
                        ts: Date.now(), thread: this.threadId,
                        cls: clsName, sel: selName, is_class: isClass,
                    };
                    // args[0]=self/meta, args[1]=SEL, args[2..]=params
                    try { rec.self_ptr = args[0].toString(); } catch(e) {}
                    try { rec.self_cls = classNameOf(args[0]); } catch(e) {}
                    // 最多读前 4 个参数
                    var params = [];
                    for (var i = 2; i < 6; i++) {
                        try {
                            if (!args[i]) break;
                            params.push(safeDesc(args[i]));
                        } catch(e) { break; }
                    }
                    if (params.length) rec.params = params;
                    pushTrace(rec);
                } finally { _busy = false; }
            }
        });
        return true;
    } catch(e) { return false; }
}

// ── 1. UserManager 全部方法 ───────────────────────────────────────────────
(function hookUserManager() {
    var c = cls('UserManager');
    if (!c) { pushTrace({ warn: 'UserManager class not found' }); return; }

    // 实例方法
    var instMethods = getMethodList(c, false);
    var hookedInst = 0;
    for (var i = 0; i < instMethods.length; i++) {
        if (safeHook('UserManager', instMethods[i].selName, instMethods[i].imp, false)) hookedInst++;
    }
    // 类方法
    var classMethods = getMethodList(c, true);
    var hookedCls = 0;
    for (var j = 0; j < classMethods.length; j++) {
        if (safeHook('UserManager', classMethods[j].selName, classMethods[j].imp, true)) hookedCls++;
    }
    pushTrace({ info: 'UserManager hooked', inst: hookedInst, cls_methods: hookedCls });
})();

// ── 2. UserModel 名字含关键词的方法 ──────────────────────────────────────
(function hookUserModel() {
    var c = cls('UserModel');
    if (!c) { pushTrace({ warn: 'UserModel class not found' }); return; }
    var keywords = ['login', 'Login', 'active', 'Active', 'setActive', 'connect',
                    'Connect', 'commu', 'Commu', 'Complete', 'complete'];
    function matchesKw(name) {
        for (var k = 0; k < keywords.length; k++) {
            if (name.indexOf(keywords[k]) >= 0) return true;
        }
        return false;
    }
    var instMethods = getMethodList(c, false);
    var hookedInst = 0;
    for (var i = 0; i < instMethods.length; i++) {
        if (matchesKw(instMethods[i].selName)) {
            if (safeHook('UserModel', instMethods[i].selName, instMethods[i].imp, false)) hookedInst++;
        }
    }
    var classMethods = getMethodList(c, true);
    var hookedCls = 0;
    for (var j = 0; j < classMethods.length; j++) {
        if (matchesKw(classMethods[j].selName)) {
            if (safeHook('UserModel', classMethods[j].selName, classMethods[j].imp, true)) hookedCls++;
        }
    }
    pushTrace({ info: 'UserModel hooked', inst: hookedInst, cls_methods: hookedCls });
})();

// ── 3. TradeRequest setter ────────────────────────────────────────────────
(function hookTradeRequest() {
    var c = cls('TradeRequest');
    if (!c) { pushTrace({ warn: 'TradeRequest class not found' }); return; }
    var targets = [
        'setNsInsid:',
        'setRequestParamStr:Value:',
        'setRequestParamStrEx:Value:',
        'setRequestParamInteger:Value:',
        'setDelegate:',
    ];
    var hooked = 0;
    for (var i = 0; i < targets.length; i++) {
        var selP = _sel_registerName(Memory.allocUtf8String(targets[i]));
        var m = _class_getInstanceMethod(c, selP);
        if (!m || m.isNull()) continue;
        var imp = _method_getImplementation(m);
        if (safeHook('TradeRequest', targets[i], imp, false)) hooked++;
    }
    pushTrace({ info: 'TradeRequest hooked', count: hooked });
})();

// ── 4. UserModel.XIXBiQhWtIEfT_DW:（dispatch）─────────────────────────
(function hookDispatch() {
    var c = cls('UserModel');
    if (!c) return;
    var selP = _sel_registerName(Memory.allocUtf8String('XIXBiQhWtIEfT_DW:'));
    var m = _class_getInstanceMethod(c, selP);
    if (!m || m.isNull()) { pushTrace({ warn: 'XIXBiQhWtIEfT_DW: not found' }); return; }
    var imp = _method_getImplementation(m);
    safeHook('UserModel', 'XIXBiQhWtIEfT_DW:', imp, false);
})();

// ── RPC ───────────────────────────────────────────────────────────────────
rpc.exports = {
    dump: function() { return traceLog.slice(); },
    clear: function() { traceLog = []; traceSeq = 0; },
};
"""

CLICK_SCRIPT = '''
tell application "System Events"
    set theProcs to processes whose name is "同花顺"
    if theProcs is {} then return "no proc"
    set theProc to item 1 of theProcs
    set frontmost of theProc to true
    delay 0.5
    set allWins to windows of theProc
    if (count of allWins) = 0 then return "no windows"
    set theWin to item 1 of allWins
    try
        click button 6 of theWin
    on error errMsg
        return "b6_err:" & errMsg
    end try
    delay 4
    try
        click button "模拟" of theWin
        return "ok_by_name"
    on error
    end try
    try
        click button 12 of theWin
        return "ok_b12"
    on error errMsg2
        return "moni_err:" & errMsg2
    end try
end tell'''


def _get_ths_pid():
    try:
        out = subprocess.check_output(["pgrep", "-x", "同花顺"], text=True)
        for pid_s in out.split():
            pid = int(pid_s.strip())
            try:
                cmd = subprocess.check_output(
                    ["ps", "-p", str(pid), "-o", "args="], text=True
                ).strip()
                if "/tmp/ths_lab" in cmd or "/private/tmp/ths_lab" in cmd:
                    return pid
            except subprocess.CalledProcessError:
                pass
    except subprocess.CalledProcessError:
        pass
    return None


def main():
    ap = argparse.ArgumentParser(description="录制同花顺登录调用链")
    ap.add_argument("--pid", type=int, default=None, help="直接指定进程 pid")
    ap.add_argument("--out", default="/tmp/login_trace.json", help="输出文件路径")
    ap.add_argument("--wait", type=int, default=12, help="点击模拟后等待秒数（默认 12）")
    args = ap.parse_args()

    pid = args.pid
    if not pid:
        pid = _get_ths_pid()
    if not pid:
        sys.exit("找不到同花顺副本进程，请先启动 /tmp/ths_lab/THSX.app 再运行此脚本")

    print(f"[record_login] attach pid={pid}", flush=True)
    dev = frida.get_local_device()
    session = dev.attach(pid)

    messages = []
    def on_message(msg, data):
        if msg.get("type") == "error":
            print(f"[agent error] {msg.get('description')}", file=sys.stderr)
        messages.append(msg)

    script = session.create_script(RECORD_JS)
    script.on("message", on_message)
    script.load()
    rpc = script.exports_sync
    print("[record_login] hook 已装载，清空旧日志...", flush=True)
    rpc.clear()

    print("[record_login] 点击 交易(button 6) → 模拟...", flush=True)
    r = subprocess.run(["osascript", "-e", CLICK_SCRIPT],
                       capture_output=True, text=True, timeout=20)
    click_result = r.stdout.strip()
    print(f"[record_login] AppleScript 返回: {click_result}", flush=True)

    print(f"[record_login] 等待 {args.wait}s 让调用链走完...", flush=True)
    time.sleep(args.wait)

    trace = rpc.dump()
    print(f"[record_login] 录制到 {len(trace)} 条记录", flush=True)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2)
    print(f"[record_login] 已写入 {args.out}", flush=True)

    # 简要打印关键条目
    print("\n── 关键调用链（按 seq）──")
    keywords = ["login", "Login", "active", "Active", "setActive",
                "connect", "Connect", "commu", "Commu",
                "setNsInsid", "setRequestParam",
                "XIXBiQhWtIEfT_DW", "shareInstance", "GetMoniUserArr",
                "loginZiYing", "strMoNiActive"]
    printed = 0
    for rec in trace:
        s = rec.get("sel", "") or ""
        cls_name = rec.get("cls", "") or ""
        info = rec.get("info", "") or ""
        warn = rec.get("warn", "") or ""
        if info or warn:
            print(f"  [{rec.get('seq','')}] {info or warn}")
            continue
        matched = any(k in s or k in cls_name for k in keywords)
        if matched:
            params = rec.get("params", [])
            print(f"  [{rec.get('seq','')}] {cls_name}({'+'if rec.get('is_class') else '-'})"
                  f" [{s}]  params={params}")
            printed += 1
        if printed >= 80:
            print("  ... (更多见输出文件)")
            break

    session.detach()
    print(f"\n完整记录：{args.out}")


if __name__ == "__main__":
    main()
