'use strict';
/*
 * ths_agent.js — Frida agent for 同花顺 (THSX)
 *
 * RPC exports (camelCase — Frida converts snake_case params to camelCase):
 *   ping / state
 *   readTableData(params)       — read tableSourceData from active TradeQueryTemplate
 *   readNSTableViews()          — scan all NSTableViews in view tree, return cell text
 *   readPositions()             — headless: dispatch req → wait → read cached TQT data
 *   readOrders()                — headless: dispatch req → wait → read cached TQT data
 *   sendChedan(params)          — call sendChedanRequestByTableIndex: by row index
 *   buy(params) / sell(params)
 *   captureStart / captureStop / captureDump
 *   sendLog
 */

// ── libobjc C API ────────────────────────────────────────────────────────────
var libobjc = Module.load('/usr/lib/libobjc.A.dylib');
var _objc_getClass          = new NativeFunction(libobjc.getExportByName('objc_getClass'),           'pointer', ['pointer']);
var _class_getInstanceMethod= new NativeFunction(libobjc.getExportByName('class_getInstanceMethod'), 'pointer', ['pointer','pointer']);
var _class_getClassMethod   = new NativeFunction(libobjc.getExportByName('class_getClassMethod'),    'pointer', ['pointer','pointer']);
var _method_getImplementation=new NativeFunction(libobjc.getExportByName('method_getImplementation'),'pointer', ['pointer']);
var _sel_registerName       = new NativeFunction(libobjc.getExportByName('sel_registerName'),        'pointer', ['pointer']);
var _objc_msgSend           = libobjc.getExportByName('objc_msgSend');
var _object_getClass         = new NativeFunction(libobjc.getExportByName('object_getClass'),         'pointer', ['pointer']);
var _class_getName           = new NativeFunction(libobjc.getExportByName('class_getName'),          'pointer', ['pointer']);

function classNameOf(objPtr) {
  try {
    if (!objPtr || objPtr.isNull()) return null;
    var c = _object_getClass(objPtr);
    if (!c || c.isNull()) return null;
    var np = _class_getName(c);
    if (!np || np.isNull()) return null;
    return np.readUtf8String();
  } catch(e) { return null; }
}

// ── typed msgSend wrappers ───────────────────────────────────────────────────
var msg0p = new NativeFunction(_objc_msgSend, 'pointer', ['pointer','pointer']);
var msg1p = new NativeFunction(_objc_msgSend, 'pointer', ['pointer','pointer','pointer']);
var msg2p = new NativeFunction(_objc_msgSend, 'pointer', ['pointer','pointer','pointer','pointer']);
var msg3p = new NativeFunction(_objc_msgSend, 'pointer', ['pointer','pointer','pointer','pointer','pointer']);
var msg0i = new NativeFunction(_objc_msgSend, 'int',     ['pointer','pointer']);
var msg1i = new NativeFunction(_objc_msgSend, 'int',     ['pointer','pointer','pointer']);
var msg0u = new NativeFunction(_objc_msgSend, 'uint64',  ['pointer','pointer']);
var msgSetInt    = new NativeFunction(_objc_msgSend, 'pointer', ['pointer','pointer','pointer','int64']);
// setNsInsid: signature v24@0:8q16 — takes int64 directly
var msgSetNsInsid = new NativeFunction(_objc_msgSend, 'void',    ['pointer','pointer','int64']);
// sendChedanRequestByTableIndex: takes long long row index (arm64: int64 in x2)
var msgChedan = new NativeFunction(_objc_msgSend, 'void', ['pointer','pointer','int64']);

// ── class / selector caches ──────────────────────────────────────────────────
var _clsCache = {}, _selCache = {};
function cls(name) {
  if (_clsCache[name] !== undefined) return _clsCache[name];
  var p = _objc_getClass(Memory.allocUtf8String(name));
  _clsCache[name] = (p && !p.isNull()) ? p : null;
  return _clsCache[name];
}
function sel(name) {
  if (_selCache[name] !== undefined) return _selCache[name];
  _selCache[name] = _sel_registerName(Memory.allocUtf8String(name));
  return _selCache[name];
}

// ── NSString helpers ─────────────────────────────────────────────────────────
var NSUTF8 = 0x08000100;
function jsStr(s) {
  var c = Memory.allocUtf8String(String(s));
  return msg2p(cls('NSString'), sel('stringWithCString:encoding:'), c, ptr(NSUTF8));
}
function nsStr(p) {
  if (!p || p.isNull()) return null;
  var utf8 = msg0p(p, sel('UTF8String'));
  if (!utf8 || utf8.isNull()) return null;
  return utf8.readUtf8String();
}
function nsIntVal(p) {
  if (!p || p.isNull()) return null;
  return msg0u(p, sel('integerValue'));
}

// ── IMP lookup ───────────────────────────────────────────────────────────────
function impFor(clsName, selName, isClass) {
  var c = cls(clsName);
  if (!c) return null;
  var s = sel(selName);
  var m = isClass ? _class_getClassMethod(c, s) : _class_getInstanceMethod(c, s);
  if (!m || m.isNull()) return null;
  return _method_getImplementation(m);
}

// ── 主线程执行 ──────────────────────────────────────────────────────────────
// 实测：Frida RPC 线程直接调 AppKit（NSApplication/windows/subviews）无死锁，
// 8 个窗口瞬时返回。dispatch_sync/dispatch_async + NativeCallback 反而冻结 JS 引擎。
// 故 runOnMain 直接同步执行 fn()，不做任何线程调度。

function runOnMain(fn) {
  return fn();
}

// ── 重入锁 ────────────────────────────────────────────────────────────────────
// 崩溃根因：hook onEnter 里调 ObjC 会再次命中自身 → 无限递归 → gum-js-loop 栈溢出
// 所有高风险 hook 必须在此锁保护下执行。
var _busySend = false;    // hookSend 重入锁
var _busyTQT  = false;    // installTQTHook 重入锁（TQT 方法）
var _busyUM   = false;    // UserModel.OnTradeRsponceNotify: 独立锁
var _busyDR   = false;    // onDataReady: onLeave 独立锁
var _busyFrame= false;    // getFrameStr（调 description）重入锁

// ── send detection ───────────────────────────────────────────────────────────
var sendCounter = 0;
var sendLog = [];
var SEND_LOG_CAP = 64;

// ── recv detection (socket 层收包 hook) ────────────────────────────────────────
var _recvLog = [];
var _recvHooked = false;

(function hookSend() {
  // hook 所有 writeTextData 变体
  // ★ 重入锁：onEnter 里调 nsStr→ObjC 可能再次命中自身，_busySend 防无限递归
  var variants = [
    'writeTextData:withTimeout:delegate:instance:',
    'writeTextData:encodingType:withTimeout:delegate:instance:',
    'writeTextData:forBusiness:withTimeout:delegate:instance:encodingType:finish:',
    'writeTextData:protocolId:marketId:encodingType:withTimeout:delegate:instance:',
    'writeTextData:protocolId:marketId:withTimeout:delegate:instance:',
  ];
  var hookedCount = 0;
  for (var vi = 0; vi < variants.length; vi++) {
    var imp = impFor('HXSocketCenter', variants[vi], false);
    if (!imp) continue;
    (function(variant, imp2) {
      Interceptor.attach(imp2, {
        onEnter: function (args) {
          if (_busySend) return;
          _busySend = true;
          try {
            sendCounter++;
            var rec = { seq: sendCounter, ts: Date.now(), thread: this.threadId, variant: variant };
            try { rec.data = nsStr(args[2]); } catch (e) { rec.data = '<?>'; }
            try { rec.instance = args[4].toString(); } catch(e2) { rec.instance = '<?>'; }
            try { rec.delegate = args[3].toString(); } catch(e3) { rec.delegate = '<?>'; }
            sendLog.push(rec);
            if (sendLog.length > SEND_LOG_CAP) sendLog.shift();
            if (captureActive) pushCapture({ kind: 'SEND', seq: sendCounter, ts: rec.ts, data: rec.data });
          } finally {
            _busySend = false;
          }
        }
      });
    })(variants[vi], imp);
    hookedCount++;
  }

  // 也 hook XIXBiQhWtIEfT_DW: 确认被调用，并读取 tradeCommuModel 状态
  // ★ 重入锁：调多个 ObjC 方法，_busySend 防重入
  var impXIX = impFor('UserModel', 'XIXBiQhWtIEfT_DW:', false);
  if (impXIX) {
    var _object_getClass2 = new NativeFunction(libobjc.getExportByName('object_getClass'), 'pointer', ['pointer']);
    var _class_getName2   = new NativeFunction(libobjc.getExportByName('class_getName'),  'pointer', ['pointer']);
    Interceptor.attach(impXIX, {
      onEnter: function(args) {
        if (_busySend) return;
        _busySend = true;
        try {
          var rec = { seq: -1, ts: Date.now(), thread: this.threadId,
            variant: 'XIXBiQhWtIEfT_DW:', req_ptr: args[2].toString() };
          try {
            var tcm = msg0p(args[0], sel('tradeCommuModel'));
            if (tcm && !tcm.isNull()) {
              var c = _object_getClass2(tcm);
              var np = c && !c.isNull() ? _class_getName2(c) : null;
              rec.tcm_ptr = tcm.toString();
              rec.tcm_class = np && !np.isNull() ? np.readUtf8String() : null;
              try { rec.tcm_isDisconnected = msg0i(tcm, sel('isDisconnected')); } catch(e) {}
              try { rec.tcm_isLostSocket   = msg0i(tcm, sel('isLostSocketConnection')); } catch(e) {}
              try { rec.tcm_sessionStatus  = msg0i(tcm, sel('getSessionStatus')); } catch(e) {}
              try { rec.tcm_sessionId      = String(msg0u(tcm, sel('dwSessionId'))); } catch(e) {}
              try { rec.tcm_autoReg        = msg0i(tcm, sel('autoReg')); } catch(e) {}
            } else {
              rec.tcm_ptr = null;
            }
          } catch(e) { rec.tcm_error = String(e); }
          try { rec.isUserLogin  = msg0i(args[0], sel('isUserLogin')); }  catch(e) {}
          try { rec.isDisconnect = msg0i(args[0], sel('isDisconnect')); } catch(e) {}
          sendLog.push(rec);
          if (sendLog.length > SEND_LOG_CAP) sendLog.shift();
        } finally {
          _busySend = false;
        }
      }
    });
  }

  if (hookedCount === 0) sendLog.push({error: 'HXSocketCenter writeTextData not found'});

  // hook commumodule 的混淆方法 — 仅记录指针，不调 ObjC，无需重入锁
  var commuMethods = ['kQvYehFExlWUHfgW:', 'DJAwpxdGFxRzelWL:', 'LOWNqsWstlJNohYU', 'vuPEXDRmhuGmWuqM:'];
  for (var ci = 0; ci < commuMethods.length; ci++) {
    var impCM = impFor('commumodule', commuMethods[ci], false);
    if (impCM) {
      (function(mname, imp3) {
        Interceptor.attach(imp3, {
          onEnter: function(args) {
            sendLog.push({ seq: -2, ts: Date.now(), thread: this.threadId,
              variant: 'commumodule.' + mname, arg2: args[2] ? args[2].toString() : null });
            if (sendLog.length > SEND_LOG_CAP) sendLog.shift();
          }
        });
      })(commuMethods[ci], impCM);
    }
  }
})();

// ── capture ──────────────────────────────────────────────────────────────────
var captureActive = false;
var captureBuf = [];
var captureSeq = 0;
var captureHooks = [];

function pushCapture(rec) {
  rec.seq = ++captureSeq;
  if (captureBuf.length >= 4000) captureBuf.shift();
  captureBuf.push(rec);
}

function hookForCapture(clsName, selName, isClass, label, argFn) {
  var imp = impFor(clsName, selName, isClass);
  if (!imp) { pushCapture({ kind: 'HOOK_MISS', cls: clsName, sel: selName }); return; }
  captureHooks.push(Interceptor.attach(imp, {
    onEnter: function (args) {
      if (!captureActive) return;
      var rec = { kind: 'CALL', label: label, ts: Date.now(), thread: this.threadId };
      try { if (argFn) rec.args = argFn(args); } catch (e) { rec.args = {err: String(e)}; }
      pushCapture(rec);
    }
  }));
}

function installCaptureHooks() {
  hookForCapture('TradeRequest', 'setRequestParamStr:Value:',      false, 'setStr',     function(a){ return {key:nsStr(a[2]), val:nsStr(a[3])}; });
  hookForCapture('TradeRequest', 'setRequestParamStrEx:Value:',    false, 'setStrEx',   function(a){ return {key:nsStr(a[2]), val:nsStr(a[3])}; });
  hookForCapture('TradeRequest', 'setRequestParamInteger:Value:',  false, 'setInt',     function(a){ return {key:nsStr(a[2]), val:a[3].toInt32()}; });
  hookForCapture('TradeRequest', 'setNsInsid:',                    false, 'setNsInsid', function(a){ return {nsInsid:nsStr(a[2])}; });
  hookForCapture('TradeRequest', 'setDelegate:',                   false, 'setDelegate',function(a){ return {delegate:a[2].toString()}; });
  hookForCapture('UserModel',    'XIXBiQhWtIEfT_DW:',             false, 'dispatch',   function(a){ return {req:a[2].toString()}; });
  hookForCapture('TradeQueryTemplate','sendChedanRequestByTableIndex:',false,'chedan',  function(a){ return {index:a[2].toInt32()}; });
}

// ── tableSourceData reader ────────────────────────────────────────────────────
function dumpNSDict(d) {
  if (!d || d.isNull()) return {};
  var keys = msg0p(d, sel('allKeys'));
  if (!keys || keys.isNull()) return {};
  var n = Number(msg0u(keys, sel('count')));
  var row = {};
  for (var k = 0; k < n; k++) {
    var key = msg1p(keys, sel('objectAtIndex:'), ptr(k));
    var kv = Number(nsIntVal(key));
    var val = msg1p(d, sel('objectForKey:'), key);
    var vs = nsStr(val);
    if (vs === null) { try { vs = String(Number(nsIntVal(val))); } catch(e) { vs = val ? val.toString() : null; } }
    row[kv] = vs;
  }
  return row;
}

// ── TQT response cache (headless) ─────────────────────────────────────────────
var _tqtCache = {};
var _tqtCacheHooked = false;
// 最近一次 UserModel.OnTradeRsponceNotify: 的原始参数 dump（供调试）
var _userModelResponseDump = [];

function installTQTHook() {
  if (_tqtCacheHooked) return;

  // snapshotTQT 必须在 installTQTHook 函数顶层定义（非 if 块内），
  // 否则 refreshTableWithType: hook 里引用会报 ReferenceError (Frida V8 块级 function 不 hoist)
  var msgInt0 = new NativeFunction(_objc_msgSend, 'int', ['pointer','pointer']);
  var snapshotTQT = function(self_obj) {
    try {
      var nType = msgInt0(self_obj, sel('nType'));
      var arr = msg0p(self_obj, sel('tableSourceData'));
      var rows = [];
      if (arr && !arr.isNull()) {
        var n = Number(msg0u(arr, sel('count')));
        for (var i = 0; i < n; i++) {
          var rowObj = msg1p(arr, sel('objectAtIndexedSubscript:'), ptr(i));
          rows.push(dumpNSDict(rowObj));
        }
      }
      var existing = _tqtCache[nType];
      if (rows.length > 0) {
        _tqtCache[nType] = { ts: Date.now(), rows: rows, nType: nType };
      } else if (!existing) {
        // 没有历史缓存时才写空行（作为占位，表明该 nType 已被查询过）
        _tqtCache[nType] = { ts: Date.now(), rows: [], nType: nType };
      }
    } catch(e) {}
  };

  // ── 方案 A：TradeQueryTemplate（委托/行情类视图）────────────────────────────
  var impTQT = impFor('TradeQueryTemplate', 'OnTradeRsponceNotify:', false);
  if (impTQT) {
    Interceptor.attach(impTQT, {
      onEnter: function(args) {
        if (_busyTQT) return;
        _busyTQT = true;
        try {
          this._self = args[0];
          snapshotTQT(args[0]);
        } finally { _busyTQT = false; }
      },
      onLeave: function(retval) {
        if (_busyTQT) return;
        _busyTQT = true;
        try {
          if (this._self) snapshotTQT(this._self);
        } finally { _busyTQT = false; }
      }
    });
  }

  // ── 方案 A2：refreshTableWithType: 在 _tableData 填充后调用（晚于 OnTradeRsponceNotify:）
  // TQT.OnTradeRsponceNotify: 用 _dispatch_async 写入 _tableData，然后异步调 refreshTableWithType:
  // 在这里 snapshot 能拿到完整的 tableSourceData
  var impRefresh = impFor('TradeQueryTemplate', 'refreshTableWithType:', false);
  if (impRefresh) {
    Interceptor.attach(impRefresh, {
      onEnter: function(args) {
        if (_busyTQT) return;
        _busyTQT = true;
        try { snapshotTQT(args[0]); } finally { _busyTQT = false; }
      }
    });
  }

  // ── 方案 A3：onDataReady: 触发时 tableSourceData 可能还未填充，在 onLeave 读 ────
  var impDataReady = impFor('TradeQueryTemplate', 'onDataReady:', false);
  if (impDataReady) {
    Interceptor.attach(impDataReady, {
      onEnter: function(args) {
        this._tqt = args[0];
        // ★ 在 onEnter 时立刻 snapshot：历史委托数据在 onDataReady 进入时 tsd_count>0，
        //    但 onLeave 时可能已被清空
        if (!_busyDR && args[0] && !args[0].isNull()) {
          _busyDR = true;
          try { snapshotTQT(args[0]); } finally { _busyDR = false; }
        }
      },
      onLeave: function(retval) {
        if (_busyDR) return;
        _busyDR = true;
        try {
          if (this._tqt && !this._tqt.isNull()) snapshotTQT(this._tqt);
        } finally { _busyDR = false; }
      }
    });
  }

  // ── 方案 B：UserModel.OnTradeRsponceNotify: （持仓/委托协议响应）──────────
  // 已确证：dispatch 走 [activeUserModel XIXBiQhWtIEfT_DW: req]
  // 响应回调落在 [UserModel OnTradeRsponceNotify:]，参数是 TradeRequest 对象
  var impUM = impFor('UserModel', 'OnTradeRsponceNotify:', false);
  if (impUM) {
    var msgInt1 = new NativeFunction(_objc_msgSend, 'int', ['pointer','pointer']);

    function snapshotUserModelResponse(reqObj) {
      try {
        // 尝试从 TradeRequest 对象读 req_type 和 tableSourceData / responseData
        var nType = 0;
        // 多路径尝试读 nType / req_type
        try { nType = msgInt1(reqObj, sel('nType')); } catch(e) {}
        if (!nType) { try { nType = msgInt1(reqObj, sel('reqType')); } catch(e) {} }
        if (!nType) { try { nType = msgInt1(reqObj, sel('req_type')); } catch(e) {} }
        if (!nType) { try { nType = msgInt1(reqObj, sel('requestType')); } catch(e) {} }
        // 尝试从 requestParams 字典读 req_type
        if (!nType) {
          try {
            var rp = msg0p(reqObj, sel('requestParams'));
            if (rp && !rp.isNull()) {
              var rtKey = jsStr('req_type');
              var rtVal = msg1p(rp, sel('objectForKey:'), rtKey);
              if (rtVal && !rtVal.isNull()) nType = Number(nsIntVal(rtVal)) || 0;
            }
          } catch(e) {}
        }

        // 尝试读 tableSourceData（TradeQueryTemplate 已有此方法）
        var arr = null;
        try { arr = msg0p(reqObj, sel('tableSourceData')); } catch(e) {}

        var rows = [];
        if (arr && !arr.isNull()) {
          var n = Number(msg0u(arr, sel('count')));
          for (var i2 = 0; i2 < n; i2++) {
            var rowObj2 = msg1p(arr, sel('objectAtIndexedSubscript:'), ptr(i2));
            rows.push(dumpNSDict(rowObj2));
          }
        } else {
          // 尝试读 responseData (NSDictionary)
          try {
            var rd = msg0p(reqObj, sel('responseData'));
            if (rd && !rd.isNull()) rows.push(dumpNSDict(rd));
          } catch(e) {}
          // 尝试读 tradeData
          try {
            var td = msg0p(reqObj, sel('tradeData'));
            if (td && !td.isNull()) {
              var tc = Number(msg0u(td, sel('count')));
              for (var j = 0; j < tc; j++) {
                var tr = msg1p(td, sel('objectAtIndex:'), ptr(j));
                rows.push(dumpNSDict(tr));
              }
            }
          } catch(e) {}
        }

        // 记录 dump 供 dumpResponse RPC 使用
        var entry = { ts: Date.now(), nType: nType, row_count: rows.length,
                      rows_sample: rows.slice(0, 3),
                      obj_ptr: reqObj.toString() };
        _userModelResponseDump.push(entry);
        if (_userModelResponseDump.length > 20) _userModelResponseDump.shift();

        // 写入缓存（仅当有行或缓存为空时）
        if (nType) {
          var existing2 = _tqtCache[nType];
          if (rows.length > 0 || !existing2 || existing2.rows.length === 0) {
            _tqtCache[nType] = { ts: Date.now(), rows: rows, nType: nType };
          }
        }
        // nType 未能识别时，若有数据则存入 key -1；若无数据也存入 -1 供调试
        if (!nType) {
          var prev1 = _tqtCache[-1];
          if (rows.length > 0 || !prev1 || prev1.rows.length === 0) {
            _tqtCache[-1] = { ts: Date.now(), rows: rows, nType: -1 };
          }
        }
      } catch(e) {}
    }

    Interceptor.attach(impUM, {
      onEnter: function(args) {
        if (_busyUM) return;
        _busyUM = true;
        try {
          // args[0]=self(UserModel), args[2]=TradeRequest 参数
          snapshotUserModelResponse(args[2]);
        } finally { _busyUM = false; }
      }
    });
  }

  _tqtCacheHooked = true;
}

// ── setTableSourceData: hook（主数据出口）────────────────────────────────────
// 实测：点「交易」→「模拟」后 TradeQueryTemplate 大量触发此方法，
// args[2] 就是接口解析后的结构化数据数组（持仓/委托均走此出口）。
// 重入锁：_busyTSD，防止 onEnter 里调 ObjC 再次命中自身。
var _busyTSD = false;
var _tsdHooked = false;

function installSetTableSourceDataHook() {
  if (_tsdHooked) return;
  var impTSD = impFor('TradeQueryTemplate', 'setTableSourceData:', false);
  if (!impTSD) return;
  _tsdHooked = true;

  var msgInt0TSD = new NativeFunction(_objc_msgSend, 'int', ['pointer','pointer']);

  Interceptor.attach(impTSD, {
    onEnter: function(args) {
      if (_busyTSD) return;
      _busyTSD = true;
      try {
        var self_obj = args[0];
        var arr      = args[2];   // NSArray* tableSourceData
        if (!arr || arr.isNull()) return;

        // 读 nType
        var nType = 0;
        try { nType = msgInt0TSD(self_obj, sel('nType')); } catch(e) {}

        // 读行数
        var n = 0;
        try { n = Number(msg0u(arr, sel('count'))); } catch(e) { return; }
        if (n === 0) return;

        // 读所有行
        var rows = [];
        for (var i = 0; i < n; i++) {
          try {
            var rowObj = msg1p(arr, sel('objectAtIndexedSubscript:'), ptr(i));
            rows.push(dumpNSDict(rowObj));
          } catch(e) {}
        }
        if (rows.length === 0) return;

        // 存入缓存：key = nType（若 nType=0 则尝试按字段特征推断）
        var cacheKey = nType;
        if (!cacheKey) {
          // 委托特征：有合同号 2113
          if (rows[0][2113] !== undefined || rows[0]['2113'] !== undefined) {
            cacheKey = 1539; // 0x603
          } else {
            cacheKey = 0x601; // 持仓
          }
        }

        // 只升不降：已有更多行时不覆盖（防止翻页截断覆盖完整数据）
        var existing = _tqtCache[cacheKey];
        if (!existing || rows.length >= existing.rows.length) {
          _tqtCache[cacheKey] = {
            ts: Date.now(), rows: rows, nType: cacheKey,
            source: 'setTableSourceData',
            self_ptr: self_obj.toString()
          };
        }
      } catch(e) {}
      finally { _busyTSD = false; }
    }
  });
}

// spawn 模式下 ObjC 类可能未完全加载，延迟 1.5s 再装 hook
setTimeout(function() {
  try { installTQTHook(); } catch(e) {}
  try { installSetTableSourceDataHook(); } catch(e) {}
}, 1500);

// hook 安装后自动等账户就绪，然后触发一次持仓+委托请求填缓存
(function autoFetchOnReady() {
  var attempts = 0;
  var maxAttempts = 60; // 最多等 60s
  function tryFetch() {
    attempts++;
    if (attempts > maxAttempts) return;
    try {
      var au = getActiveMoniUser();
      if (!au.ok) { setTimeout(tryFetch, 1000); return; }
      // 账户就绪，自动派发持仓和委托请求
      try {
        delete _tqtCache[0x601];
        var rr = buildPositionsRequest();
      } catch(e1) {}
      try {
        delete _tqtCache[1539];
        var rr2 = buildOrdersRequest();
      } catch(e2) {}
    } catch(e) { setTimeout(tryFetch, 1000); return; }
  }
  setTimeout(tryFetch, 3000); // hook 安装完 1.5s 后再等 1.5s 开始轮询
})();

// ── snapshot tableSourceData from a TQT ptr ──────────────────────────────────
function readTableSourceDataFromPtr(view) {
  var arr = msg0p(view, sel('tableSourceData'));
  if (!arr || arr.isNull()) return [];
  var n = Number(msg0u(arr, sel('count')));
  var rows = [];
  for (var i = 0; i < n; i++) {
    var row = msg1p(arr, sel('objectAtIndexedSubscript:'), ptr(i));
    rows.push(dumpNSDict(row));
  }
  return rows;
}

// Walk all windows → find TradeQueryTemplate instances
// ★ 迭代版（显式栈 + visited Set + 节点上限 2000），消灭递归防止栈溢出
function findTradeQueryTemplates() {
  var NSApp = msg0p(cls('NSApplication'), sel('sharedApplication'));
  if (!NSApp || NSApp.isNull()) return [];
  var wins = msg0p(NSApp, sel('windows'));
  if (!wins || wins.isNull()) return [];
  var wn = Number(msg0u(wins, sel('count')));
  var found = [];
  var selTSD = sel('tableSourceData');
  var selRS  = sel('respondsToSelector:');
  for (var i = 0; i < wn; i++) {
    var w = msg1p(wins, sel('objectAtIndex:'), ptr(i));
    if (!w || w.isNull()) continue;
    var cv = msg0p(w, sel('contentView'));
    if (!cv || cv.isNull()) continue;
    var stack = [cv];
    var visited = {};
    var nodeCount = 0;
    while (stack.length > 0 && nodeCount < 2000) {
      var view = stack.pop();
      if (!view || view.isNull()) continue;
      var key = view.toString();
      if (visited[key]) continue;
      visited[key] = true;
      nodeCount++;
      try {
        if (msg1i(view, selRS, selTSD) !== 0) {
          found.push(view);
          if (found.length >= 4) return found;
        }
      } catch(e) {}
      try {
        var subs = msg0p(view, sel('subviews'));
        if (subs && !subs.isNull()) {
          var n = Number(msg0u(subs, sel('count')));
          for (var si = n - 1; si >= 0; si--) {
            var sub = msg1p(subs, sel('objectAtIndex:'), ptr(si));
            if (sub && !sub.isNull()) stack.push(sub);
          }
        }
      } catch(e) {}
    }
  }
  return found;
}

function readTableSourceData(view) {
  var arr = msg0p(view, sel('tableSourceData'));
  if (!arr || arr.isNull()) return [];
  var n = Number(msg0u(arr, sel('count')));
  var rows = [];
  for (var i = 0; i < n; i++) {
    var row = msg1p(arr, sel('objectAtIndexedSubscript:'), ptr(i));
    rows.push(dumpNSDict(row));
  }
  return rows;
}

// ── NSTableView scanner ───────────────────────────────────────────────────────

function getFrameStr(view) {
  if (_busyFrame) return '';
  _busyFrame = true;
  try {
    var desc = nsStr(msg0p(view, sel('description')));
    if (desc) {
      var m = desc.match(/frame\s*=\s*\{([^}]*)\}/);
      if (m) return m[1].replace(/\s+/g, '');
    }
  } catch(e) {}
  finally { _busyFrame = false; }
  return '';
}

function nsTableViewColumns(tv) {
  var cols = msg0p(tv, sel('tableColumns'));
  if (!cols || cols.isNull()) return [];
  var n = Number(msg0u(cols, sel('count')));
  var result = [];
  for (var i = 0; i < n; i++) {
    var col = msg1p(cols, sel('objectAtIndex:'), ptr(i));
    var header = null;
    try {
      var hcell = msg0p(col, sel('headerCell'));
      if (hcell && !hcell.isNull())
        header = nsStr(msg0p(hcell, sel('stringValue')));
    } catch(e) {}
    if (!header) {
      try { header = nsStr(msg0p(col, sel('identifier'))); } catch(e) {}
    }
    result.push({ col: col, header: header || ('col' + i) });
  }
  return result;
}

function cellText(cell) {
  if (!cell || cell.isNull()) return '';
  try {
    var sv = msg0p(cell, sel('stringValue'));
    if (sv && !sv.isNull()) {
      var t = nsStr(sv);
      if (t !== null) return t;
    }
  } catch(e) {}
  try {
    var ov = msg0p(cell, sel('objectValue'));
    if (ov && !ov.isNull()) {
      var t2 = nsStr(ov);
      if (t2 !== null) return t2;
    }
  } catch(e) {}
  return '';
}

function cellTextFromDataSource(ds, tv, col, rowIdx) {
  var selDS = sel('tableView:objectValueForTableColumn:row:');
  if (msg1i(ds, sel('respondsToSelector:'), selDS) === 0) return null;
  try {
    var fn = new NativeFunction(_objc_msgSend, 'pointer',
      ['pointer','pointer','pointer','pointer','long']);
    var val = fn(ds, selDS, tv, col, rowIdx);
    if (!val || val.isNull()) return null;
    var t = nsStr(val);
    if (t !== null) return t;
    try { return String(Number(nsIntVal(val))); } catch(e) {}
  } catch(e) {}
  return null;
}

function readNSTableViewData(tv) {
  var nRows = 0;
  try { nRows = msg0i(tv, sel('numberOfRows')); } catch(e) { return { error: 'numberOfRows failed: ' + e }; }
  var cols = nsTableViewColumns(tv);
  var nCols = cols.length;
  var frame = getFrameStr(tv);

  var ds = null;
  try {
    ds = msg0p(tv, sel('dataSource'));
    if (ds && ds.isNull()) ds = null;
  } catch(e) { ds = null; }

  var rows = [];
  for (var r = 0; r < nRows && r < 500; r++) {
    var row = {};
    for (var c = 0; c < nCols; c++) {
      var header = cols[c].header;
      var text = '';
      try {
        var fn3 = new NativeFunction(_objc_msgSend, 'pointer',
          ['pointer','pointer','long','long','int']);
        var cell = fn3(tv, sel('viewAtColumn:row:makeIfNecessary:'), c, r, 0);
        if (cell && !cell.isNull()) {
          text = cellText(cell);
        }
      } catch(e) {}
      if (!text) {
        try {
          var fn4 = new NativeFunction(_objc_msgSend, 'pointer',
            ['pointer','pointer','long','long']);
          var cell2 = fn4(tv, sel('preparedCellAtColumn:row:'), c, r);
          if (cell2 && !cell2.isNull()) text = cellText(cell2);
        } catch(e) {}
      }
      if (!text && ds) {
        var dv = cellTextFromDataSource(ds, tv, cols[c].col, r);
        if (dv !== null) text = dv;
      }
      row[header] = text;
    }
    rows.push(row);
  }
  return { nRows: nRows, nCols: nCols, frame: frame, cols: cols.map(function(x){return x.header;}), rows: rows };
}

// 遍历所有窗口找 NSTableView — 迭代版（显式栈 + visited + 节点上限 2000）
// ★ 消灭递归，彻底防止 gum-js-loop 栈溢出
function findAllNSTableViews() {
  var NSApp = msg0p(cls('NSApplication'), sel('sharedApplication'));
  if (!NSApp || NSApp.isNull()) return { error: 'NSApplication nil' };
  var wins = msg0p(NSApp, sel('windows'));
  if (!wins || wins.isNull()) return { error: 'windows nil' };
  var wn = Number(msg0u(wins, sel('count')));
  var allViews = [];
  var selNR = sel('numberOfRows');
  var selTC = sel('tableColumns');
  var selRS = sel('respondsToSelector:');
  for (var i = 0; i < wn; i++) {
    var w = msg1p(wins, sel('objectAtIndex:'), ptr(i));
    if (!w || w.isNull()) continue;
    var cv = msg0p(w, sel('contentView'));
    if (!cv || cv.isNull()) continue;
    var stack = [cv];
    var visited = {};
    var nodeCount = 0;
    while (stack.length > 0 && nodeCount < 2000) {
      var view = stack.pop();
      if (!view || view.isNull()) continue;
      var key = view.toString();
      if (visited[key]) continue;
      visited[key] = true;
      nodeCount++;
      var isTable = false;
      try {
        if (msg1i(view, selRS, selNR) !== 0 &&
            msg1i(view, selRS, selTC) !== 0) {
          allViews.push(view);
          isTable = true;
        }
      } catch(e) {}
      if (isTable) continue; // NSTableView 不再往下遍历子视图
      try {
        var subs = msg0p(view, sel('subviews'));
        if (subs && !subs.isNull()) {
          var n = Number(msg0u(subs, sel('count')));
          for (var si = n - 1; si >= 0; si--) {
            var sub = msg1p(subs, sel('objectAtIndex:'), ptr(si));
            if (sub && !sub.isNull()) stack.push(sub);
          }
        }
      } catch(e) {}
    }
  }
  return { views: allViews };
}

// ── trade helpers ────────────────────────────────────────────────────────────
function getActiveMoniUser() {
  var um = msg0p(cls('UserManager'), sel('shareInstance'));
  if (!um || um.isNull()) return { error: 'UserManager nil' };
  var moniArr = msg0p(um, sel('GetMoniUserArr'));
  if (!moniArr || moniArr.isNull() || msg0u(moniArr, sel('count')) === 0)
    return { error: 'GetMoniUserArr empty — 请先点「模拟」登录' };
  var n = Number(msg0u(moniArr, sel('count')));

  // 先试 getActiveUser，确认它是否在 moniArr 里
  var u = msg0p(um, sel('getActiveUser'));
  if (u && !u.isNull()) {
    for (var i = 0; i < n; i++) {
      if (msg1p(moniArr, sel('objectAtIndex:'), ptr(i)).equals(u))
        return { ok: true, user: u, um: um };
    }
  }

  // getActiveUser 返回 nil 或不在 moniArr 里：直接用 moniArr 的第一个用户
  var fallback = msg1p(moniArr, sel('objectAtIndex:'), ptr(0));
  if (fallback && !fallback.isNull())
    return { ok: true, user: fallback, um: um, via_fallback: true };

  return { error: 'GetMoniUserArr has 0 valid entries' };
}

function buildTradeRequest(reqType, p) {
  var trCls = cls('TradeRequest');
  if (!trCls) return { error: 'TradeRequest class not found' };
  var req = msg0p(msg0p(trCls, sel('alloc')), sel('init'));
  if (!req || req.isNull()) return { error: 'TradeRequest alloc/init failed' };

  var au = getActiveMoniUser();
  var nsInsid = null;
  if (au.ok && msg1i(au.user, sel('respondsToSelector:'), sel('nsInsid')) !== 0) {
    nsInsid = msg0p(au.user, sel('nsInsid'));
  }
  if (nsInsid && !nsInsid.isNull()) msg1p(req, sel('setNsInsid:'), nsInsid);
  msg1p(req, sel('setDelegate:'), ptr(0));

  var sc  = p.stockcode || '';
  var mk  = p.mkcode    || '1';
  var qty = String(p.qty   || '0');
  var prc = String(p.price || '0');
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('stockcode'), jsStr(sc));
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('zqdm'),      jsStr(sc));
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('mkcode'),    jsStr(mk));
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('scdm'),      jsStr(mk));
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('amount'),    jsStr(qty));
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('wtsl'),      jsStr(qty));
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('price'),     jsStr(prc));
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('wtjg'),      jsStr(prc));
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('interact'),  jsStr('1'));
  msgSetInt(req, sel('setRequestParamInteger:Value:'), jsStr('req_type'), reqType);

  if (p.extra) {
    for (var k in p.extra)
      msg2p(req, sel('setRequestParamStr:Value:'), jsStr(k), jsStr(String(p.extra[k])));
  }
  return { ok: true, req: req, nsInsidSet: !!(nsInsid && !nsInsid.isNull()) };
}

function dispatchTrade(req, user) {
  var s = sel('XIXBiQhWtIEfT_DW:');
  if (msg1i(user, sel('respondsToSelector:'), s) === 0)
    return { error: 'active user does not respond to XIXBiQhWtIEfT_DW:' };
  msg1p(user, s, req);
  return { ok: true };
}

// ── sendChedanDirect ─────────────────────────────────────────────────────────
function sendChedanFromRow(row) {
  var sc   = row[0x836] || '';
  var scdm = row[0x857] || '';
  var gdzh = row[0x83a] || '';
  var mkRaw= row[0x83c] || '';
  var jylx = row[0xfa8] || null;

  var trCls = cls('TradeRequest');
  if (!trCls) return { error: 'TradeRequest class not found' };

  var req = msg0p(msg0p(trCls, sel('alloc')), sel('init'));
  if (!req || req.isNull()) return { error: 'TradeRequest alloc/init failed' };

  var au = getActiveMoniUser();
  if (!au.ok) return { error: au.error };

  var nsInsid = null;
  if (msg1i(au.user, sel('respondsToSelector:'), sel('nsInsid')) !== 0)
    nsInsid = msg0p(au.user, sel('nsInsid'));
  if (nsInsid && !nsInsid.isNull()) msg1p(req, sel('setNsInsid:'), nsInsid);

  msg1p(req, sel('setDelegate:'), ptr(0));
  msgSetInt(req, sel('setRequestParamInteger:Value:'), jsStr('req_type'), 4);
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('stockcode'), jsStr(sc));
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('zqdm'),      jsStr(sc));
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('gdzh'),      jsStr(gdzh));
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('mkcode'),    jsStr(mkRaw));
  msg2p(req, sel('setRequestParamStr:Value:'), jsStr('scdm'),      jsStr(scdm));
  if (jylx && String(jylx) !== '' && String(jylx) !== '0') {
    msg2p(req, sel('setRequestParamStr:Value:'), jsStr('jylx'), jsStr(String(jylx)));
  }

  var d = dispatchTrade(req, au.user);
  if (!d.ok) return { error: d.error };
  return { ok: true };
}

// ── buildPositionsRequest ────────────────────────────────────────────────────
// nsInsid=0x186eb (100075), req_type=0x601 (1537) — 已确证
// ★ delegate 必须是 TQT 实例（同 requestByType:BeginDate:EndDate: 真实路径）
//   delegate=au.user 时响应落在 UserModel.OnTradeRsponceNotify:，
//   该方法只处理 0x605/0x900/0xe03/0x1000，0x601 被静默丢弃。
function buildPositionsRequest() {
  var au = getActiveMoniUser();
  if (!au.ok) return { error: au.error };

  // ★ 不用 ObjC.chooseSync（在某些条件下 abort），直接用 au.user 作 delegate
  // 响应落在 UserModel.OnTradeRsponceNotify:，该 hook 已缓存所有 nType

  var trCls = cls('TradeRequest');
  if (!trCls) return { error: 'TradeRequest class not found' };
  var req = msg0p(msg0p(trCls, sel('alloc')), sel('init'));
  if (!req || req.isNull()) return { error: 'TradeRequest alloc/init failed' };

  msgSetNsInsid(req, sel('setNsInsid:'), 0x186eb);
  msg1p(req, sel('setDelegate:'), au.user);
  msgSetInt(req, sel('setRequestParamInteger:Value:'), jsStr('req_type'), 0x601);

  var d = dispatchTrade(req, au.user);
  if (!d.ok) return { error: d.error };
  return { ok: true, tqt_delegate: tqtDelegate ? tqtDelegate.toString() : null };
}

// ── buildOrdersRequest ────────────────────────────────────────────────────────
// nsInsid=0x186da (100058), req_type=0x603 (1539) — 已确证
// ★ 核心修复：服务端用 instanceID=0x186da 路由响应，必须新建一个 nsInsid=0x186da 的
//   TQT 实例作为 delegate，才能让响应落进 OnTradeRsponceNotify:。
//   复用现有持仓 TQT（nsInsid=0x186eb）永远收不到委托响应。
// params: { beginDate: 'YYYYMMDD', endDate: 'YYYYMMDD' }  — 可选，设置日期范围
function buildOrdersRequest(dateParams) {
  var au = getActiveMoniUser();
  if (!au.ok) return { error: au.error };

  // ★ 不用 ObjC.chooseSync（在某些条件下 abort），直接用 au.user 作 delegate
  // 响应落在 UserModel.OnTradeRsponceNotify:，该 hook 已缓存所有 nType

  var trCls = cls('TradeRequest');
  if (!trCls) return { error: 'TradeRequest class not found' };
  var req = msg0p(msg0p(trCls, sel('alloc')), sel('init'));
  if (!req || req.isNull()) return { error: 'TradeRequest alloc/init failed' };

  msgSetNsInsid(req, sel('setNsInsid:'), 0x186da);
  msg1p(req, sel('setDelegate:'), au.user);
  msgSetInt(req, sel('setRequestParamInteger:Value:'), jsStr('req_type'), 0x603);

  // 设置日期范围（如果提供）
  if (dateParams && dateParams.beginDate) {
    msg2p(req, sel('setRequestParamStr:Value:'), jsStr('beginDate'), jsStr(String(dateParams.beginDate)));
  }
  if (dateParams && dateParams.endDate) {
    msg2p(req, sel('setRequestParamStr:Value:'), jsStr('endDate'), jsStr(String(dateParams.endDate)));
  }

  var d = dispatchTrade(req, au.user);
  if (!d.ok) return { error: d.error };
  return { ok: true };
}

// ── RPC exports ──────────────────────────────────────────────────────────────
rpc.exports = {
  ping: function () {
    return { ok: true, agent: 'ths_agent', ts: Date.now() };
  },

  // ── readTqtDirect: 通过 ObjC runtime 读内存中所有 TQT 实例的 tableSourceData ──
  // 不依赖网络响应 hook，适合 bridge 重连后缓存为空但 app 已有数据的情况
  readTqtDirect: function () {
    try {
      var tqtCls = cls('TradeQueryTemplate');
      if (!tqtCls) return { ok: false, error: 'TradeQueryTemplate class not found' };

      // 用 Frida 的 ObjC.chooseSync 枚举堆上所有 TQT 实例
      // 在 Frida pure-JS agent 里，ObjC 全局可通过 globalThis.ObjC 访问
      var ObjCGlobal = (typeof ObjC !== 'undefined') ? ObjC :
                       (typeof globalThis !== 'undefined' && globalThis.ObjC) ? globalThis.ObjC : null;
      if (!ObjCGlobal) return { ok: false, error: 'ObjC runtime not available in this context' };

      var instances = ObjCGlobal.chooseSync(ObjCGlobal.classes.TradeQueryTemplate);
      if (!instances || instances.length === 0)
        return { ok: false, error: 'no TradeQueryTemplate instances in heap' };

      var result = [];
      var msgI = new NativeFunction(_objc_msgSend, 'int', ['pointer','pointer']);
      for (var i = 0; i < instances.length; i++) {
        try {
          var inst = instances[i];
          var instPtr = inst.handle || inst;
          var nType = 0;
          try { nType = msgI(instPtr, sel('nType')); } catch(e) {}
          var arr = null;
          try { arr = msg0p(instPtr, sel('tableSourceData')); } catch(e) {}
          var rows = [];
          if (arr && !arr.isNull()) {
            var n = Number(msg0u(arr, sel('count')));
            for (var j = 0; j < n; j++) {
              try { rows.push(dumpNSDict(msg1p(arr, sel('objectAtIndexedSubscript:'), ptr(j)))); } catch(e) {}
            }
          }
          result.push({ ptr: instPtr.toString(), nType: nType, row_count: rows.length, rows: rows });
          // 同时填充缓存
          if (rows.length > 0 && nType) {
            _tqtCache[nType] = { ts: Date.now(), rows: rows, nType: nType };
          }
        } catch(ei) {}
      }
      return { ok: true, instance_count: instances.length, instances: result };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── dumpResponseHooks: 检查 UserModel/TradeQueryTemplate 的 OnTradeRsponceNotify: 实现是否存在
  // 同时 dump _userModelResponseDump 捕获内容
  dumpResponseHooks: function () {
    try {
      var tqtImp = impFor('TradeQueryTemplate', 'OnTradeRsponceNotify:', false);
      var umImp  = impFor('UserModel',          'OnTradeRsponceNotify:', false);
      // 也检查 class method
      var tqtCls = impFor('TradeQueryTemplate', 'OnTradeRsponceNotify:', true);
      var umCls  = impFor('UserModel',          'OnTradeRsponceNotify:', true);

      // 枚举 UserModel 的方法（只取含 Trade/Notify/Response 的）
      var umMethods = [];
      try {
        var _class_copyMethodList = new NativeFunction(
          libobjc.getExportByName('class_copyMethodList'), 'pointer', ['pointer','pointer']);
        var _method_getName = new NativeFunction(
          libobjc.getExportByName('method_getName'), 'pointer', ['pointer']);
        var _sel_getName = new NativeFunction(
          libobjc.getExportByName('sel_getName'), 'pointer', ['pointer']);
        var countBuf = Memory.alloc(4);
        var umCls2 = cls('UserModel');
        if (umCls2) {
          var methods = _class_copyMethodList(umCls2, countBuf);
          var count = countBuf.readU32();
          for (var i = 0; i < count; i++) {
            var m = methods.add(i * Process.pointerSize).readPointer();
            var sn = _sel_getName(_method_getName(m));
            if (sn && !sn.isNull()) {
              var name = sn.readUtf8String();
              if (/[Tt]rade|[Nn]otify|[Rr]esponse|[Rr]esponce|[Qq]uery/i.test(name))
                umMethods.push(name);
            }
          }
        }
      } catch(e) { umMethods.push('enum_error: ' + String(e)); }

      return {
        ok: true,
        tqt_imp: tqtImp ? tqtImp.toString() : null,
        um_imp:  umImp  ? umImp.toString()  : null,
        tqt_cls_imp: tqtCls ? tqtCls.toString() : null,
        um_cls_imp:  umCls  ? umCls.toString()  : null,
        um_trade_methods: umMethods,
        tqt_cache_keys: Object.keys(_tqtCache),
        user_model_dump_count: _userModelResponseDump.length,
        user_model_dump: _userModelResponseDump.slice(),
      };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  state: function () {
    try {
      var um = msg0p(cls('UserManager'), sel('shareInstance'));
      if (!um || um.isNull()) return { ok: false, error: 'UserManager nil' };
      var moniArr  = msg0p(um, sel('GetMoniUserArr'));
      var shipanArr= msg0p(um, sel('GetShiPanUserArr'));
      var moniCnt  = (moniArr  && !moniArr.isNull())  ? Number(msg0u(moniArr,  sel('count'))) : 0;
      var shipanCnt= (shipanArr&& !shipanArr.isNull()) ? Number(msg0u(shipanArr,sel('count'))) : 0;
      var active   = msg0p(um, sel('getActiveUser'));
      var activePtr= (active && !active.isNull()) ? active.toString() : null;

      // arrayUser — 所有用户总列表
      var allArr  = msg0p(um, sel('arrayUser'));
      var allCnt  = (allArr && !allArr.isNull()) ? Number(msg0u(allArr, sel('count'))) : 0;
      var allUsers = [];
      if (allArr && !allArr.isNull()) {
        for (var ai = 0; ai < allCnt; ai++) {
          var au2 = msg1p(allArr, sel('objectAtIndex:'), ptr(ai));
          var rec2 = { ptr: au2.toString() };
          // 多种方式读账号
          try { rec2.zjzh = nsStr(msg0p(au2, sel('zjzh'))); } catch(e){}
          try { if (!rec2.zjzh) rec2.zjzh = nsStr(msg0p(au2, sel('account'))); } catch(e){}
          try { if (!rec2.zjzh) rec2.zjzh = nsStr(msg0p(au2, sel('userName'))); } catch(e){}
          // isMoniUser 返回 BOOL，用 msg1i
          try {
            var isMoniSel = sel('isMoniUser');
            if (msg1i(au2, sel('respondsToSelector:'), isMoniSel) !== 0)
              rec2.isMoni = (msg1i(au2, isMoniSel) !== 0);
          } catch(e){}
          // GetType 返回整数
          try {
            var typeSel = sel('GetType');
            if (msg1i(au2, sel('respondsToSelector:'), typeSel) !== 0)
              rec2.type = Number(msg0u(au2, typeSel));
          } catch(e){}
          // isUserLogin
          try {
            var loginSel = sel('isUserLogin');
            if (msg1i(au2, sel('respondsToSelector:'), loginSel) !== 0)
              rec2.isLogin = (msg1i(au2, loginSel) !== 0);
          } catch(e){}
          // bActiveUser
          try {
            var activeSel = sel('bActiveUser');
            if (msg1i(au2, sel('respondsToSelector:'), activeSel) !== 0)
              rec2.bActive = (msg1i(au2, activeSel) !== 0);
          } catch(e){}
          allUsers.push(rec2);
        }
      }

      var moniAccts= [];
      if (moniArr && !moniArr.isNull()) {
        for (var i = 0; i < moniCnt; i++) {
          var u = msg1p(moniArr, sel('objectAtIndex:'), ptr(i));
          var z = null;
          try { z = nsStr(msg0p(u, sel('zjzh'))); } catch(e){}
          if (!z) { try { z = nsStr(msg0p(u, sel('account'))); } catch(e){} }
          moniAccts.push({ ptr: u.toString(), account: z });
        }
      }
      var isActiveMoni = false;
      if (activePtr && moniCnt > 0) {
        for (var j = 0; j < moniCnt; j++) {
          if (msg1p(moniArr, sel('objectAtIndex:'), ptr(j)).equals(active)) { isActiveMoni = true; break; }
        }
      }
      return {
        ok: true, moni_count: moniCnt, shipan_count: shipanCnt,
        active_user_ptr: activePtr, active_is_moni: isActiveMoni,
        moni_accounts: moniAccts, send_counter: sendCounter,
        array_user_count: allCnt, all_users: allUsers,
        // active user 连接状态
        active_is_disconnect: (function() {
          if (!active || active.isNull()) return null;
          try { return msg1i(active, sel('isDisconnect')) !== 0; } catch(e) { return null; }
        })(),
        active_is_user_login: (function() {
          if (!active || active.isNull()) return null;
          try { return msg1i(active, sel('isUserLogin')) !== 0; } catch(e) { return null; }
        })(),
        active_trade_commu_model: (function() {
          if (!active || active.isNull()) return null;
          try {
            var tcm = msg0p(active, sel('tradeCommuModel'));
            return (tcm && !tcm.isNull()) ? tcm.toString() : null;
          } catch(e) { return null; }
        })(),
      };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  readTableData: function () {
    try {
      var result = [];
      // 方案 A: ObjC.chooseSync 堆扫描（不走视图树，不会 abort）
      try {
        var tqtObj = ObjC.classes.TradeQueryTemplate;
        if (tqtObj) {
          var views = ObjC.chooseSync(tqtObj);
          if (views && views.length > 0) {
            for (var i = 0; i < views.length; i++) {
              try {
                var v = views[i];
                if (msg1i(v, sel('respondsToSelector:'), sel('tableSourceData')) === 0) continue;
                var rows = readTableSourceData(v);
                result.push({ ptr: v.toString(), row_count: rows.length, rows: rows });
              } catch(e2) {}
            }
          }
        }
      } catch(eChoose) {}

      // 方案 B: 视图树遍历兜底（持仓面板时安全，委托面板时可能 abort）
      if (result.length === 0) {
        var views2 = findTradeQueryTemplates();
        if (views2 && views2.length > 0) {
          for (var j = 0; j < views2.length; j++) {
            var rows2 = readTableSourceData(views2[j]);
            result.push({ ptr: views2[j].toString(), row_count: rows2.length, rows: rows2 });
          }
        }
      }

      if (result.length === 0) return { ok: false, error: 'no TradeQueryTemplate instances with data' };
      return { ok: true, views: result };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── 扫描所有 NSTableView ──────────────────────────────────────────────────
  readNSTableViews: function () {
    try {
      var r = findAllNSTableViews();
      if (r.error) return { ok: false, error: r.error };
      var tables = [];
      for (var i = 0; i < r.views.length; i++) {
        var tv = r.views[i];
        try {
          var data = readNSTableViewData(tv);
          tables.push({ ptr: tv.toString(), idx: i, data: data });
        } catch(e) {
          tables.push({ ptr: tv.toString(), idx: i, error: String(e) });
        }
      }
      return { ok: true, count: tables.length, tables: tables };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── readPositions: 发 req_type=0x601 → 等 2.2s → 扫 NSTableView ──────────
  readPositions: function () {
    try {
      var rr = buildPositionsRequest();
      if (rr.error) return { ok: false, error: 'dispatch: ' + rr.error };
      var before = sendCounter;
      return new Promise(function(resolve) {
        setTimeout(function() {
          try {
            var r = findAllNSTableViews();
            if (r.error) { resolve({ ok: false, error: r.error }); return; }
            var tables = [];
            for (var i = 0; i < r.views.length; i++) {
              var tv = r.views[i];
              try {
                var data = readNSTableViewData(tv);
                tables.push({ ptr: tv.toString(), idx: i, data: data });
              } catch(e) {
                tables.push({ ptr: tv.toString(), idx: i, error: String(e) });
              }
            }
            var posTable = null, posScore = -1;
            for (var j = 0; j < tables.length; j++) {
              var t = tables[j];
              if (t.error || !t.data || !t.data.rows || t.data.rows.length === 0) continue;
              var cols = t.data.cols || [];
              var score = 0;
              for (var k = 0; k < cols.length; k++) {
                var h = cols[k];
                if (/代码|zqdm|stockcode/i.test(h)) score += 3;
                if (/股票|证券|名称/i.test(h)) score += 2;
                if (/数量|持仓|余额|可用/i.test(h)) score += 2;
                if (/成本|市值|盈亏|收益/i.test(h)) score += 1;
              }
              if (t.data.rows.length > 0) {
                var vals = Object.values(t.data.rows[0]).join(' ');
                if (/\b\d{6}\b/.test(vals)) score += 5;
              }
              if (score > posScore) { posScore = score; posTable = t; }
            }
            resolve({ ok: true, send_detected: sendCounter > before,
                      all_tables: tables.length, pos_table: posTable, pos_score: posScore });
          } catch(e) { resolve({ ok: false, error: String(e) }); }
        }, 2200);
      });
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── readOrders: 纯读 _tqtCache，完全不 dispatch 任何 ObjC 请求 ─────────────
  // 用户切到委托 tab 时 app 自动发 req_type=0x603，OnTradeRsponceNotify: hook
  // 把结果缓存到 _tqtCache。本函数只读缓存，不做任何 msgSend，不会死锁。
  // 委托表列 key（十进制 NSNumber → JS number）：
  //   2096=时间  2102=代码  2103=名称  4008=jylx(方向)  2105=状态
  //   2117=委托量  2118=委托价  2120=成交量  2119=成交价
  //   2113=合同号  2108=mkcode  2135=scdm  2106=gdzh
  readOrders: function () {
    try {
      var keys = Object.keys(_tqtCache);
      var cached = _tqtCache[0x603];
      if (cached && cached.rows && cached.rows.length > 0) {
        return { ok: true, source: 'tqt_cache', nType: cached.nType,
                 ts: cached.ts, count: cached.rows.length, rows: cached.rows };
      }
      var cached2 = _tqtCache[0x401];
      if (cached2 && cached2.rows && cached2.rows.length > 0) {
        return { ok: true, source: 'tqt_cache_0x401', nType: cached2.nType,
                 ts: cached2.ts, count: cached2.rows.length, rows: cached2.rows };
      }
      var best = null;
      for (var ki = 0; ki < keys.length; ki++) {
        var c = _tqtCache[keys[ki]];
        if (!c || !c.rows || c.rows.length === 0) continue;
        if (c.rows[0][2113] !== undefined || c.rows[0]['2113'] !== undefined) {
          if (!best || c.ts > best.ts) best = c;
        }
      }
      if (best) {
        return { ok: true, source: 'tqt_cache_scan', nType: best.nType,
                 ts: best.ts, count: best.rows.length, rows: best.rows };
      }
      var summary = {};
      for (var si = 0; si < keys.length; si++) {
        var cc = _tqtCache[keys[si]];
        summary[keys[si]] = { count: cc.rows.length,
          first_keys: cc.rows.length > 0 ? Object.keys(cc.rows[0]).slice(0, 10) : [] };
      }
      return { ok: false, source: 'no_cache', cache_keys: keys, cache_summary: summary };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── readOrdersFromTable: 直接读委托 NSTableView 的 cell subviews ───────────
  // 策略：
  //   1. 找所有 NSTableView（同 readPositions 的路径）
  //   2. 对每个 table：用 rowViewAtRow:makeIfNecessary: 取 rowView，
  //      递归遍历 subviews，收集所有 NSTextField / NSTextView 的 stringValue
  //   3. 选行数最多且列数合理的表作为委托表
  //   4. 同时返回每张表的 cell 类名 dump，供调试
  readOrdersFromTable: function () {
    try {
      var r = findAllNSTableViews();
      if (r.error) return { ok: false, error: r.error };

      // ── 迭代版收集 view 树中所有文本（显式栈 + visited + 节点上限 500）──────
      // ★ 不调 description（会触发 hook 重入），只调 stringValue/attributedStringValue
      function collectTexts(rootView) {
        var texts = [];
        if (!rootView || rootView.isNull()) return texts;
        var stack = [rootView];
        var visited = {};
        var nodeCount = 0;
        while (stack.length > 0 && nodeCount < 500) {
          var view = stack.pop();
          if (!view || view.isNull()) continue;
          var key = view.toString();
          if (visited[key]) continue;
          visited[key] = true;
          nodeCount++;
          // 类名：用 classNameOf（已加 null 保护，不调 description）
          var clsName = classNameOf(view) || '';
          // 尝试读 stringValue
          var sv = '';
          try {
            var svp = msg0p(view, sel('stringValue'));
            if (svp && !svp.isNull()) sv = nsStr(svp) || '';
          } catch(e) {}
          if (sv !== '') { texts.push({ cls: clsName, text: sv }); }
          // stringValue 为空时尝试 attributedStringValue → string
          if (sv === '') {
            try {
              var asvp = msg0p(view, sel('attributedStringValue'));
              if (asvp && !asvp.isNull()) {
                var s2 = nsStr(msg0p(asvp, sel('string')));
                if (s2 && s2 !== '') texts.push({ cls: clsName, text: s2 });
              }
            } catch(e) {}
          }
          // 把子视图压栈（逆序保持遍历顺序）
          try {
            var subs = msg0p(view, sel('subviews'));
            if (subs && !subs.isNull()) {
              var sn = Number(msg0u(subs, sel('count')));
              for (var si2 = sn - 1; si2 >= 0; si2--) {
                var sub = msg1p(subs, sel('objectAtIndex:'), ptr(si2));
                if (sub && !sub.isNull()) stack.push(sub);
              }
            }
          } catch(e) {}
        }
        return texts;
      }

      // ── 对每张 NSTableView 读 rowView → subview texts ──────────────────
      var fnRowView = new NativeFunction(_objc_msgSend, 'pointer',
        ['pointer','pointer','long','int']);

      var allTables = [];
      for (var ti = 0; ti < r.views.length; ti++) {
        var tv = r.views[ti];
        var nRows = 0;
        try { nRows = msg0i(tv, sel('numberOfRows')); } catch(e) { continue; }
        if (nRows === 0) continue;

        var tableRows = [];
        var firstRowClsDump = null;

        for (var ri = 0; ri < nRows && ri < 200; ri++) {
          // rowViewAtRow:makeIfNecessary: YES=1
          var rowView = null;
          try { rowView = fnRowView(tv, sel('rowViewAtRow:makeIfNecessary:'), ri, 1); } catch(e) {}

          if (!rowView || rowView.isNull()) {
            // fallback: viewAtColumn:row:makeIfNecessary: 逐列拼
            var rowFn = new NativeFunction(_objc_msgSend, 'pointer',
              ['pointer','pointer','long','long','int']);
            var nCols2 = 0;
            try {
              var colArr = msg0p(tv, sel('tableColumns'));
              if (colArr && !colArr.isNull()) nCols2 = Number(msg0u(colArr, sel('count')));
            } catch(e) {}
            var cellTexts = [];
            for (var ci2 = 0; ci2 < nCols2; ci2++) {
              try {
                var cellView = rowFn(tv, sel('viewAtColumn:row:makeIfNecessary:'), ci2, ri, 1);
                if (cellView && !cellView.isNull()) {
                  var ct = collectTexts(cellView);
                  cellTexts.push(ct.map(function(x){return x.text;}).filter(function(x){return x!=='';}).join('|'));
                  if (ri === 0 && firstRowClsDump === null) {
                    firstRowClsDump = ct;
                  }
                } else {
                  cellTexts.push('');
                }
              } catch(e) { cellTexts.push(''); }
            }
            tableRows.push(cellTexts);
          } else {
            // 有 rowView，迭代收集所有文本
            var allTexts = collectTexts(rowView);
            if (ri === 0 && firstRowClsDump === null) firstRowClsDump = allTexts;
            tableRows.push(allTexts.map(function(x){return x.text;}).filter(function(x){return x!=='';}) );
          }
        }

        allTables.push({
          ptr: tv.toString(),
          nRows: nRows,
          rows: tableRows,
          first_row_cls_dump: firstRowClsDump
        });
      }

      // ── 账户汇总关键词（命中这些词说明是汇总面板，不是委托表）──────────
      var ACCT_KEYWORDS = /总资产|总市值|总盈亏|资金余额|可取金额|可用金额|当日盈亏|浮动盈亏|持仓市值/;

      // ── 判断一张表是否是账户汇总表 ──────────────────────────────────────
      function isAccountSummary(t) {
        if (!t.rows || t.rows.length === 0) return false;
        var hitCount = 0;
        for (var ri2 = 0; ri2 < Math.min(t.rows.length, 10); ri2++) {
          var row2 = t.rows[ri2];
          if (!Array.isArray(row2)) continue;
          for (var ci3 = 0; ci3 < row2.length; ci3++) {
            var cell2 = row2[ci3];
            if (typeof cell2 === 'object' && cell2 !== null) cell2 = cell2.text || '';
            if (ACCT_KEYWORDS.test(String(cell2))) { hitCount++; break; }
          }
        }
        // 超过半数行命中汇总关键词，认定为账户汇总表
        return hitCount >= Math.max(1, Math.floor(t.rows.length * 0.4));
      }

      // ── 选委托表：排除账户汇总，优先列数 >= 5，有 6 位股票代码的得高分 ──
      var bestTable = null, bestScore = -1;
      var allTablesDump = allTables.map(function(t3){
        return { ptr: t3.ptr, nRows: t3.nRows, nCols: t3.nCols,
                 is_acct: isAccountSummary(t3),
                 first_row: t3.rows[0] || [],
                 first_row_cls_dump: t3.first_row_cls_dump };
      });

      for (var bi = 0; bi < allTables.length; bi++) {
        var t2 = allTables[bi];
        if (!t2.rows || t2.rows.length === 0) continue;
        if (isAccountSummary(t2)) continue;   // 排除账户汇总

        var nonEmptyRows = 0, hasStockCode = false;
        for (var rj = 0; rj < t2.rows.length; rj++) {
          var cols2 = t2.rows[rj];
          var nonEmpty = 0;
          if (Array.isArray(cols2)) {
            for (var cj = 0; cj < cols2.length; cj++) {
              var cv = cols2[cj];
              if (typeof cv === 'object' && cv !== null) cv = cv.text || '';
              var cvs = String(cv).trim();
              if (cvs !== '') nonEmpty++;
              if (/^\d{6}$/.test(cvs)) hasStockCode = true;
            }
          }
          if (nonEmpty >= 2) nonEmptyRows++;
        }

        var nCols3 = t2.nCols || (t2.rows[0] ? t2.rows[0].length : 0);
        var score2 = nonEmptyRows * 10 + (nCols3 >= 5 ? 50 : 0) + (hasStockCode ? 100 : 0);
        if (score2 > bestScore) { bestScore = score2; bestTable = t2; }
      }

      if (!bestTable) {
        return { ok: false, error: 'no orders table found (all tables are account summary or empty)',
                 all_tables: allTablesDump };
      }

      return {
        ok: true,
        source: 'nstableview_subviews',
        ptr: bestTable.ptr,
        nRows: bestTable.nRows,
        rows: bestTable.rows,
        first_row_cls_dump: bestTable.first_row_cls_dump,
        all_tables_count: allTables.length,
        all_tables: allTablesDump
      };
    } catch(e) { return { ok: false, error: String(e), stack: e.stack }; }
  },

  sendChedan: function (params) {
    try {
      var au = getActiveMoniUser();
      if (!au.ok) return { ok: false, error: au.error };

      var viewPtr = params && params.view_ptr ? ptr(params.view_ptr) : null;
      var rowIdx  = (params && params.row_index !== undefined) ? parseInt(params.row_index) : -1;
      if (!viewPtr || viewPtr.isNull()) return { ok: false, error: 'need view_ptr' };
      if (rowIdx < 0) return { ok: false, error: 'need row_index >= 0' };

      var before = sendCounter;
      var s = sel('sendChedanRequestByTableIndex:');
      if (msg1i(viewPtr, sel('respondsToSelector:'), s) === 0)
        return { ok: false, error: 'view does not respond to sendChedanRequestByTableIndex:' };

      msgChedan(viewPtr, s, rowIdx);

      return new Promise(function(resolve) {
        setTimeout(function() {
          var sent = sendCounter - before;
          resolve({ ok: true, row_index: rowIdx, send_detected: sent > 0, send_count: sent });
        }, 400);
      });
    } catch(e) { return { ok: false, error: String(e), stack: e.stack }; }
  },

  sendChedanDirect: function (params) {
    try {
      var au = getActiveMoniUser();
      if (!au.ok) return { ok: false, error: au.error };

      var viewPtr = params && params.view_ptr ? ptr(params.view_ptr) : null;
      var rowIdx  = (params && params.row_index !== undefined) ? parseInt(params.row_index) : -1;
      if (!viewPtr || viewPtr.isNull()) return { ok: false, error: 'need view_ptr' };
      if (rowIdx < 0) return { ok: false, error: 'need row_index >= 0' };

      var rows = readTableSourceData(viewPtr);
      if (rowIdx >= rows.length) return { ok: false, error: 'row_index out of range, total=' + rows.length };
      var row = rows[rowIdx];

      var before = sendCounter;
      var r = sendChedanFromRow(row);
      if (r.error) return { ok: false, error: r.error };

      return new Promise(function(resolve) {
        setTimeout(function() {
          var sent = sendCounter - before;
          resolve({ ok: true, row_index: rowIdx, send_detected: sent > 0, send_count: sent,
                    row_keys: Object.keys(row).join(',') });
        }, 400);
      });
    } catch(e) { return { ok: false, error: String(e), stack: e.stack }; }
  },

  // ── fetchPositions: 派发持仓查询(req_type=0x601) → 等 TQT hook 填缓存 → 返回行 ──
  // 响应缓存在 _tqtCache[0x601]，每行是数字 key → 字符串值的字典。
  // 已知字段编号（从真实持仓 159928/512800/512890 反推）：
  //   2013 = 股票代码 (zqdm)
  //   2034 = 股票名称
  //   2038 = 持仓数量（股票余额）
  //   2039 = 可用数量
  //   2055 = 成本价（参考 0.703/0.835/1.211 附近）
  //   2056 = 市值（参考 10615/10177/41234 附近）
  //   2060 = 当日盈亏
  //   2066 = 盈亏比例
  //   2069 = 市价（当前价）
  //   2070 = 交易市场 / mkcode
  //   2008 = 证券代码（可能与 2013 相同）
  //   注：确切编号以真实 dump 为准；这里列出的是推断值，CLI 解析以实际 key 匹配
  fetchPositions: function () {
    try {
      // 清除旧的持仓缓存，确保拿到本次刷新的数据
      delete _tqtCache[0x601];
      var dispatchErr = null;
      var before = sendCounter;
      try {
        var rr = buildPositionsRequest();
        if (rr.error) dispatchErr = rr.error;
      } catch(dispatchEx) {
        dispatchErr = String(dispatchEx);
      }
      return new Promise(function(resolve) {
        setTimeout(function() {
          try {
            var cached = _tqtCache[0x601];
            if (cached && cached.rows && cached.rows.length > 0) {
              resolve({ ok: true, source: 'protocol_response',
                        nType: 0x601, ts: cached.ts,
                        count: cached.rows.length, rows: cached.rows,
                        send_detected: sendCounter > before });
              return;
            }
            // fallback: 扫缓存中持仓特征的 nType
            // 已确证持仓行字段：2102=代码, 2103=名称, 2117=数量, 2122=成本价, 2125=市值, 2147=盈亏
            // 委托行有 2113=合同号, 2105=委托状态（持仓没有）
            var best = null;
            var keys = Object.keys(_tqtCache);
            for (var i = 0; i < keys.length; i++) {
              var c = _tqtCache[keys[i]];
              if (!c || !c.rows || c.rows.length === 0) continue;
              var r0 = c.rows[0];
              // 持仓特征：有市值(2125)或盈亏(2147)，且无合同号(2113)
              var hasPos = ((r0[2125] !== undefined || r0['2125'] !== undefined ||
                             r0[2147] !== undefined || r0['2147'] !== undefined) &&
                            (r0[2113] === undefined && r0['2113'] === undefined));
              if (hasPos && (!best || c.ts > best.ts)) best = c;
            }
            if (best) {
              resolve({ ok: true, source: 'protocol_response_scan',
                        nType: best.nType, ts: best.ts,
                        count: best.rows.length, rows: best.rows,
                        send_detected: sendCounter > before });
              return;
            }
            // 返回缓存摘要供调试（含每个 nType 第一行完整值）
            var summary = {};
            for (var si = 0; si < keys.length; si++) {
              var cc = _tqtCache[keys[si]];
              summary[keys[si]] = {
                count: cc.rows.length,
                first_keys: cc.rows.length > 0 ? Object.keys(cc.rows[0]).slice(0, 15) : [],
                first_row: cc.rows.length > 0 ? cc.rows[0] : null
              };
            }
            resolve({ ok: false, error: 'positions cache empty after 5s',
                      cache_summary: summary, send_detected: sendCounter > before });
          } catch(e) { resolve({ ok: false, error: String(e) }); }
        }, 5000);
      });
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── fetchOrders: 派发委托查询(req_type=0x603) → 等 TQT hook 填缓存 → 返回行 ──
  // 响应缓存在 _tqtCache[0x603]，每行是数字 key → 字符串值的字典。
  // 已知字段编号（从合同号 6353269582 样本反推）：
  //   2096 = 时间
  //   2102 = 股票代码 (stockcode)
  //   2103 = 股票名称
  //   4008 = 交易类型 / jylx（方向：买1/卖2）
  //   2105 = 委托状态
  //   2117 = 委托数量
  //   2118 = 委托价格
  //   2120 = 成交数量
  //   2119 = 成交价格
  //   2113 = 合同号 (htbh)  ← 已证实
  //   2108 = mkcode（交易市场）
  //   2135 = scdm（股东代码）
  //   2106 = gdzh（股东账户）
  //   2140 = 时间（已确认存在）
  //   2126 = 数量（已确认存在）
  //   2127 = 价格（已确认存在）
  //   2135 = 合同号或股东代码（已确认存在）
  fetchOrders: function () {
    try {
      // ★ 策略：先读 cache（app 点委托后 onDataReady: onLeave 会填）；
      //   cache[1539] 存在（包括 rows=0）则直接返回——0 委托是合法结果。
      //   只有 cache[1539] 不存在时才 dispatch buildOrdersRequest 再等 4 秒。
      var cachedNow = _tqtCache[1539];
      if (cachedNow !== undefined) {
        return { ok: true, source: 'tqt_cache_0x603',
                 nType: 1539, ts: cachedNow.ts,
                 count: (cachedNow.rows || []).length, rows: cachedNow.rows || [],
                 send_detected: false };
      }
      // cache 为空，主动 dispatch
      delete _tqtCache[1539];
      var rr = buildOrdersRequest();
      if (rr.error) return { ok: false, error: 'dispatch: ' + rr.error };
      var before = sendCounter;
      return new Promise(function(resolve) {
        setTimeout(function() {
          try {
            // 优先读 _tqtCache[1539]（nType=0x603），存在即成功（0 行是合法结果）
            var cached = _tqtCache[1539];
            if (cached !== undefined) {
              resolve({ ok: true, source: 'tqt_cache_0x603',
                        nType: 1539, ts: cached.ts,
                        count: (cached.rows || []).length, rows: cached.rows || [],
                        send_detected: sendCounter > before });
              return;
            }
            // fallback: 扫缓存中含合同号字段(2113)的 nType
            var best2 = null;
            var keys2 = Object.keys(_tqtCache);
            for (var j = 0; j < keys2.length; j++) {
              var c2 = _tqtCache[keys2[j]];
              if (!c2 || !c2.rows || c2.rows.length === 0) continue;
              var rr2 = c2.rows[0];
              if ((rr2[2113] !== undefined || rr2['2113'] !== undefined) &&
                  (!best2 || c2.ts > best2.ts)) best2 = c2;
            }
            if (best2) {
              resolve({ ok: true, source: 'tqt_cache_scan',
                        nType: best2.nType, ts: best2.ts,
                        count: best2.rows.length, rows: best2.rows,
                        send_detected: sendCounter > before });
              return;
            }
            // 返回缓存摘要供调试
            var summary = {};
            for (var si = 0; si < keys2.length; si++) {
              var cc = _tqtCache[keys2[si]];
              summary[keys2[si]] = {
                count: cc.rows.length,
                first_keys: cc.rows.length > 0 ? Object.keys(cc.rows[0]).slice(0, 15) : []
              };
            }
            resolve({ ok: false, error: 'orders tableSourceData empty after 4s',
                      cache_summary: summary, send_detected: sendCounter > before });
          } catch(e) { resolve({ ok: false, error: String(e) }); }
        }, 4000);
      });
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── refreshTQT: 通过已有 TQT 实例调 refreshTableWithType: 刷新数据 ────────
  // params: { type: <int> }  type=1 持仓, type=2 委托 (推测)
  // 刷新后读 tableSourceData 返回
  refreshTQT: function (params) {
    try {
      var type = (params && params.type !== undefined) ? parseInt(params.type) : 2;
      var tqts = findTradeQueryTemplates();
      if (!tqts || tqts.length === 0) return { ok: false, error: 'no TQT instance found' };

      // 取第一个 TQT 实例
      var tqt = tqts[0];
      var selRefresh = sel('refreshTableWithType:');
      if (msg1i(tqt, sel('respondsToSelector:'), selRefresh) === 0)
        return { ok: false, error: 'TQT does not respond to refreshTableWithType:' };

      // 调 refreshTableWithType: 传入 type（int）
      var fnRefresh = new NativeFunction(_objc_msgSend, 'void', ['pointer','pointer','int']);
      fnRefresh(tqt, selRefresh, type);

      // 等 2 秒让数据回来
      return new Promise(function(resolve) {
        setTimeout(function() {
          try {
            var rows = readTableSourceData(tqt);
            resolve({ ok: true, type: type, tqt_ptr: tqt.toString(),
                      count: rows.length, rows: rows });
          } catch(e) { resolve({ ok: false, error: String(e) }); }
        }, 2000);
      });
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── readOrdersViaDataSource: 通过 NSTableView dataSource 读委托 ───────────
  // 找 NSTableView → 取 dataSource → 若是 TQT 则读 numberOfRowsInTableView:
  // + tableView:objectValueForTableColumn:row:（接口层数据出口）
  readOrdersViaDataSource: function () {
    try {
      var r = findAllNSTableViews();
      if (r.error) return { ok: false, error: r.error };
      var views = r.views;
      if (!views || views.length === 0) return { ok: false, error: 'no NSTableView found' };

      var selDS = sel('dataSource');
      var selNR = sel('numberOfRowsInTableView:');
      var selOB = sel('tableView:objectValueForTableColumn:row:');
      var selRS = sel('respondsToSelector:');
      var selTSD = sel('tableSourceData');
      var selTC = sel('tableColumns');

      var results = [];
      for (var i = 0; i < views.length; i++) {
        var tv = views[i];
        var ds = null;
        try { ds = msg0p(tv, selDS); } catch(e) {}
        if (!ds || ds.isNull()) continue;

        // 检查 dataSource 是否是 TQT（响应 tableSourceData）
        var isTQT = false;
        try { isTQT = msg1i(ds, selRS, selTSD) !== 0; } catch(e) {}
        if (!isTQT) continue;

        // 读 numberOfRowsInTableView:
        var nRows = 0;
        try { nRows = msg1i(ds, selNR, tv); } catch(e) {}
        if (nRows === 0) continue;

        // 读 tableSourceData（优先）
        var rows = [];
        try { rows = readTableSourceData(ds); } catch(e) {}

        if (rows.length > 0) {
          results.push({ tv_ptr: tv.toString(), ds_ptr: ds.toString(),
                          nRows: nRows, source: 'tableSourceData',
                          count: rows.length, rows: rows });
        }
      }

      if (results.length === 0) return { ok: false, error: 'no TQT dataSource with data found' };
      return { ok: true, tables: results };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── scanClassMethods: 扫类的所有实例方法，返回名称列表 ────────────────────
  scanClassMethods: function (params) {
    try {
      var clsName = (params && params.cls) ? params.cls : 'HXConnection';
      var isClass  = (params && params.meta) ? true : false;
      var _class_copyMethodList2 = new NativeFunction(
        libobjc.getExportByName('class_copyMethodList'), 'pointer', ['pointer','pointer']);
      var _method_getName2 = new NativeFunction(
        libobjc.getExportByName('method_getName'), 'pointer', ['pointer']);
      var _sel_getName2 = new NativeFunction(
        libobjc.getExportByName('sel_getName'), 'pointer', ['pointer']);
      var c = cls(clsName);
      if (!c) return { ok: false, error: 'class not found: ' + clsName };
      var target = isClass ? msg0p(c, sel('class')) : c;
      var countBuf2 = Memory.alloc(4);
      var methods2 = _class_copyMethodList2(target, countBuf2);
      var count2 = countBuf2.readU32();
      var names = [];
      for (var i = 0; i < count2; i++) {
        var m2 = methods2.add(i * Process.pointerSize).readPointer();
        var sn2 = _sel_getName2(_method_getName2(m2));
        if (sn2 && !sn2.isNull()) names.push(sn2.readUtf8String());
      }
      return { ok: true, cls: clsName, meta: isClass, count: names.length, methods: names.sort() };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── registerDelegate: 把 activeUser 注册到 HXSocketCenter，让回包路由到它 ──
  // HXSocketCenter configAddDelegate:instance: 注册后，
  // deliverSocketDataToCenter:withInstance:readTime: 才会把数据路由给 instance
  registerDelegate: function () {
    try {
      var au = getActiveMoniUser();
      if (!au.ok) return { ok: false, error: au.error };

      // 拿 HXSocketCenter 单例（通过 class method）
      var hxsc = cls('HXSocketCenter');
      if (!hxsc) return { ok: false, error: 'HXSocketCenter class not found' };

      // 检查是否有 configAddDelegate:instance: 方法（class method）
      var selCAD = sel('configAddDelegate:instance:');
      var hasClassMethod = _class_getClassMethod(hxsc, selCAD);
      var hasInstMethod  = _class_getInstanceMethod(hxsc, selCAD);
      if (!hasClassMethod && !hasInstMethod)
        return { ok: false, error: 'configAddDelegate:instance: not found' };

      // 调用 class method：[HXSocketCenter configAddDelegate:activeUser instance:activeUser]
      if (hasClassMethod) {
        msg2p(hxsc, selCAD, au.user, au.user);
      } else {
        // 实例方法：先拿单例
        var selSD = sel('socketDataDelegate');
        // HXSocketCenter 没有明显的单例方法，跳过
        return { ok: false, error: 'configAddDelegate is instance method, need singleton — not supported yet' };
      }
      return { ok: true, registered: au.user.toString() };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── enableRecv: hook deliverSocketDataToCenter: 只记录指针，完全不读内存 ──
  enableRecv: function () {
    try {
      if (_recvHooked) return { ok: true, already: true };
      _recvHooked = true;
      var hooked = [];

      var selDeliver = 'deliverSocketDataToCenter:withInstance:readTime:';
      var imp2 = impFor('HXSocketCenter', selDeliver, false) ||
                 impFor('HXSocketCenter', selDeliver, true);
      if (imp2) {
        Interceptor.attach(imp2, {
          onEnter: function(args) {
            try {
              // 只记录指针，完全不读内存，不调 ObjC
              _recvLog.push({
                ts: Date.now(),
                sel: selDeliver,
                thread: this.threadId,
                arg2: args[2] ? args[2].toString() : null,
                arg3: args[3] ? args[3].toString() : null,
              });
              if (_recvLog.length > 500) _recvLog.shift();
            } catch(e3) {}
          }
        });
        hooked.push(selDeliver);
      }

      return { ok: true, hooked: hooked };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  recvDump: function () {
    return { ok: true, count: _recvLog.length, entries: _recvLog.slice(-50) };
  },

  buy: function (params) { return _order(0x102, params); },
  sell: function (params) { return _order(0x103, params); },

  captureStart: function () {
    if (captureActive) return { ok: true, already: true };
    captureBuf = []; captureSeq = 0; captureActive = true;
    installCaptureHooks();
    return { ok: true, hooks: captureHooks.length };
  },

  openWindow: function () {
    try {
      var NSApp = msg0p(cls('NSApplication'), sel('sharedApplication'));
      if (!NSApp || NSApp.isNull()) return { ok: false, error: 'NSApplication nil' };
      var msgPerf = new NativeFunction(_objc_msgSend, 'void',
        ['pointer','pointer','pointer','pointer','int']);
      msgPerf(NSApp, sel('performSelectorOnMainThread:withObject:waitUntilDone:'),
        sel('unhideWithoutActivation'), ptr(0), 0);
      msgPerf(NSApp, sel('performSelectorOnMainThread:withObject:waitUntilDone:'),
        sel('newDocument:'), ptr(0), 0);
      return { ok: true, dispatched: true };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── login: 通过 HXAuthenticationManager 完成完整登录流程 ─────────────────────
  // authorizeWithUserName:password:loginType:loginFlag:success:failure:cancel:
  // 这是真正的登录入口，会走 RSA 加密 → 服务器验证 → 创建 UserModel → addUser:
  login: function (params) {
    try {
      var account   = (params && params.account)              ? params.account   : '';
      var password  = (params && params.password)             ? params.password  : '';
      var loginType = (params && params.loginType !== undefined) ? params.loginType : 0;
      if (!account || !password)
        return { ok: false, error: 'need account and password' };

      var hxc = cls('HXConnection');
      if (!hxc) return { ok: false, error: 'HXConnection class not found' };

      // 拿 HXAuthenticationManager 实例
      var authMgr = msg0p(hxc, sel('authenticationManager'));
      if (!authMgr || authMgr.isNull())
        return { ok: false, error: 'HXConnection.authenticationManager nil' };

      // 检查 authorizeWithUserName:password:loginType:loginFlag:success:failure:cancel:
      var selAuth = sel('authorizeWithUserName:password:loginType:loginFlag:success:failure:cancel:');
      if (msg1i(authMgr, sel('respondsToSelector:'), selAuth) === 0)
        return { ok: false, error: 'authorizeWithUserName:... not found on authMgr' };

      var nsAcct = jsStr(account);
      var nsPass = jsStr(password);

      // 先试 nil blocks — 很多 ObjC 方法检查 block 非 nil 才调
      var sendBefore = sendCounter;
      try {
        // args: receiver, cmd, userName, password, loginType(int), loginFlag(int),
        //       success(block), failure(block), cancel(block)
        var authFn = new NativeFunction(_objc_msgSend,
          'void', ['pointer','pointer','pointer','pointer','int','int','pointer','pointer','pointer']);
        authFn(authMgr, selAuth, nsAcct, nsPass, loginType, 0, ptr(0), ptr(0), ptr(0));
      } catch(authErr) {
        return { ok: false, error: 'auth call failed: ' + String(authErr) };
      }
      var sendAfter = sendCounter;

      return { ok: true, dispatched: true, method: 'authorizeWithUserName:password:loginType:loginFlag:success:failure:cancel:',
               loginType: loginType, loginFlag: 0,
               authMgr: authMgr.toString(),
               send_before: sendBefore, send_after: sendAfter };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  captureStop: function () {
    captureActive = false;
    return { ok: true, entries: captureBuf.length };
  },
  captureDump: function () {
    return { ok: true, active: captureActive, count: captureBuf.length, entries: captureBuf.slice() };
  },
  sendLog: function () {
    return { ok: true, count: sendLog.length, entries: sendLog.slice() };
  },

  // 查持仓（旧路径兼容）
  queryPositions: function () {
    try {
      var rr = buildPositionsRequest();
      if (rr.error) return { ok: false, error: rr.error };
      var before = sendCounter;
      return new Promise(function(resolve) {
        setTimeout(function() {
          try {
            var views = findTradeQueryTemplates();
            var result = [];
            for (var i = 0; i < views.length; i++) {
              var rows = readTableSourceData(views[i]);
              result.push({ ptr: views[i].toString(), row_count: rows.length, rows: rows });
            }
            resolve({ ok: true, send_detected: sendCounter > before, views: result });
          } catch(e) { resolve({ ok: false, error: String(e) }); }
        }, 2500);
      });
    } catch(e) { return { ok: false, error: String(e) }; }
  }
};

// ── hookTQTCallbacks: 广谱 hook TQT 所有响应回调，找委托响应真正落点 ──────────
var _tqtCbLog = [];
var _tqtCbHooked = false;

rpc.exports.hookTqtCallbacks = function() {
  if (_tqtCbHooked) return { ok: true, already: true, log: _tqtCbLog.slice(-20) };
  _tqtCbHooked = true;
  // TQT 所有可能的响应回调（从 102 个方法中筛出）
  var targets = [
    'OnTradeRsponceNotify:',
    'onWeituoNotify:',
    'onDataReady:',
    'refreshTableWithType:',
    'reloadTradeData',
    'updateWeiTuoInfo',
    'reloadClearTradeDataWithType:',
    'timeOut:',
  ];
  var hooked = [];
  for (var i = 0; i < targets.length; i++) {
    var imp = impFor('TradeQueryTemplate', targets[i], false);
    if (!imp) { _tqtCbLog.push({ miss: targets[i] }); continue; }
    (function(name, impF) {
      Interceptor.attach(impF, {
        onEnter: function(args) {
          var rec = { cb: name, ts: Date.now(), self: args[0] ? args[0].toString() : null };
          // 对 refreshTableWithType:/reloadClearTradeDataWithType: 读 type 参数
          if (name.indexOf(':') !== -1 && name !== 'OnTradeRsponceNotify:' && name !== 'onWeituoNotify:' && name !== 'onDataReady:') {
            try { rec.arg2 = args[2] ? args[2].toInt32() : null; } catch(e) {}
          }
          // 读 TQT 的 tableSourceData
          if (args[0] && !args[0].isNull()) {
            try {
              var arr = msg0p(args[0], sel('tableSourceData'));
              rec.tsd_count = arr && !arr.isNull() ? Number(msg0u(arr, sel('count'))) : 0;
              try { rec.nType = msg0i(args[0], sel('nType')); } catch(e) {}
            } catch(e) { rec.tsd_err = String(e); }
          }
          _tqtCbLog.push(rec);
          if (_tqtCbLog.length > 200) _tqtCbLog.shift();
        }
      });
    })(targets[i], imp);
    hooked.push(targets[i]);
  }
  // 也 hook UserModel.OnTradeRsponceNotify:
  var impUM2 = impFor('UserModel', 'OnTradeRsponceNotify:', false);
  if (impUM2) {
    Interceptor.attach(impUM2, {
      onEnter: function(args) {
        var rec = { cb: 'UserModel.OnTradeRsponceNotify:', ts: Date.now() };
        try {
          var nType2 = 0;
          try { nType2 = msg0i(args[2], sel('nType')); } catch(e) {}
          if (!nType2) { try { nType2 = msg0i(args[2], sel('reqType')); } catch(e) {} }
          rec.nType = nType2;
        } catch(e) {}
        _tqtCbLog.push(rec);
        if (_tqtCbLog.length > 200) _tqtCbLog.shift();
      }
    });
    hooked.push('UserModel.OnTradeRsponceNotify:');
  }
  return { ok: true, hooked: hooked, missed: _tqtCbLog.filter(function(x){return x.miss;}).map(function(x){return x.miss;}) };
};

rpc.exports.dumpTqtCbLog = function() {
  return { ok: true, count: _tqtCbLog.length, log: _tqtCbLog.slice(-50) };
};

// ── readOrdersDirect: 调 TQT.requestByType:BeginDate:EndDate: 强制刷新，
//    然后轮询 tableSourceData（不依赖 hook 回调）────────────────────────────────
// params: { beginDate: 'YYYYMMDD', endDate: 'YYYYMMDD' }  — 均可选，默认今天
rpc.exports.readOrdersDirect = function(params) {
  try {
    // 先用 ObjC.chooseSync 堆扫（更可靠，不走视图树）
    var views = [];
    try {
      var tqtObj = ObjC.classes.TradeQueryTemplate;
      if (tqtObj) {
        var heap = ObjC.chooseSync(tqtObj);
        if (heap && heap.length > 0) views = heap;
      }
    } catch(eC) {}
    if (views.length === 0) views = findTradeQueryTemplates();
    if (!views || views.length === 0) return { ok: false, error: 'no TQT instance found' };

    // 计算日期字符串
    var beginDate = (params && params.beginDate) ? String(params.beginDate) : null;
    var endDate   = (params && params.endDate)   ? String(params.endDate)   : null;
    if (!beginDate || !endDate) {
      var today = new Date();
      var ymd = today.getFullYear().toString() +
                ('0'+(today.getMonth()+1)).slice(-2) +
                ('0'+today.getDate()).slice(-2);
      if (!beginDate) beginDate = ymd;
      if (!endDate)   endDate   = ymd;
    }
    var nsBegin = jsStr(beginDate);
    var nsEnd   = jsStr(endDate);

    // 找 nType=1539(0x603) 的实例，或用第一个
    var tqt = null;
    var msgInt0x = new NativeFunction(_objc_msgSend, 'int', ['pointer','pointer']);
    for (var i = 0; i < views.length; i++) {
      try {
        var nt = msgInt0x(views[i], sel('nType'));
        if (nt === 1539 || nt === 0x603) { tqt = views[i]; break; }
      } catch(e) {}
    }
    if (!tqt) tqt = views[0];

    // ★ 关键修复：先把 TQT 的 nType 设为 0x603（委托），
    //   再设 beginDate/endDate 属性，才能让请求带上日期范围
    try {
      var fnSetNT = new NativeFunction(_objc_msgSend, 'void', ['pointer','pointer','long']);
      fnSetNT(tqt, sel('setNType:'), 0x603);
    } catch(eSet) {}

    // 设置 beginDate / endDate 属性（app 内部通过 setter 传日期，
    // requestByType:BeginDate:EndDate: 的参数只是触发请求，真正的日期从属性读取）
    try { msg1p(tqt, sel('setBeginDate:'), nsBegin); } catch(e) {}
    try { msg1p(tqt, sel('setEndDate:'),   nsEnd);   } catch(e) {}

    // 清空旧缓存
    delete _tqtCache[1539];

    var before = sendCounter;

    // ★ 路径 1: setDateForBegin:andEnd:andTitle:（app 内部历史委托正确入口，已确证响应）
    var nsTitle = jsStr('\u5f53\u65e5\u59d4\u6258'); // "当日委托"
    var selSetDate = sel('setDateForBegin:andEnd:andTitle:');
    try {
      if ((new NativeFunction(_objc_msgSend, 'int', ['pointer','pointer','pointer']))(tqt, sel('respondsToSelector:'), selSetDate) !== 0) {
        var fnSetDate = new NativeFunction(_objc_msgSend, 'void', ['pointer','pointer','pointer','pointer','pointer']);
        fnSetDate(tqt, selSetDate, nsBegin, nsEnd, nsTitle);
      } else {
        // 路径 2: requestByType:BeginDate:EndDate:
        var fnReq = new NativeFunction(_objc_msgSend, 'void', ['pointer','pointer','long','pointer','pointer']);
        fnReq(tqt, sel('requestByType:BeginDate:EndDate:'), 0x603, nsBegin, nsEnd);
      }
    } catch(e) {}

    // 路径 3: upRequestRangeDateForActiveUser（追加触发，不影响上面）
    try {
      var selUp = sel('upRequestRangeDateForActiveUser');
      if ((new NativeFunction(_objc_msgSend, 'int', ['pointer','pointer','pointer']))(tqt, sel('respondsToSelector:'), selUp) !== 0) {
        msg0p(tqt, selUp);
      }
    } catch(e) {}

    // 轮询最多 8 秒（16 × 500ms），扫 _tqtCache 全部条目（不只 1539）
    return new Promise(function(resolve) {
      var attempts = 0;
      var maxAttempts = 16;
      function poll() {
        attempts++;

        // ① 优先读 _tqtCache[1539]（setTableSourceData: hook 写入）
        var cached = _tqtCache[1539];
        if (cached && cached.rows && cached.rows.length > 0) {
          resolve({ ok: true, source: cached.source || 'cache_after_request',
                    count: cached.rows.length, rows: cached.rows,
                    attempts: attempts, beginDate: beginDate, endDate: endDate,
                    send_detected: sendCounter > before });
          return;
        }

        // ② 读 TQT 实例的 tableSourceData（直接拿）
        try {
          var rows = readTableSourceData(tqt);
          if (rows && rows.length > 0) {
            // 存入缓存
            _tqtCache[1539] = { ts: Date.now(), rows: rows, nType: 1539, source: 'poll_tsd' };
            resolve({ ok: true, source: 'poll_tsd',
                      count: rows.length, rows: rows,
                      attempts: attempts, beginDate: beginDate, endDate: endDate,
                      send_detected: sendCounter > before });
            return;
          }
        } catch(e) {}

        // ③ 扫全缓存——找含合同号(2113)的最新条目
        var keys = Object.keys(_tqtCache);
        var best = null;
        for (var ki = 0; ki < keys.length; ki++) {
          var c = _tqtCache[keys[ki]];
          if (!c || !c.rows || c.rows.length === 0) continue;
          var r0 = c.rows[0];
          if (r0[2113] !== undefined || r0['2113'] !== undefined) {
            if (!best || c.ts > best.ts) best = c;
          }
        }
        if (best) {
          resolve({ ok: true, source: best.source || 'cache_scan',
                    count: best.rows.length, rows: best.rows,
                    attempts: attempts, beginDate: beginDate, endDate: endDate,
                    send_detected: sendCounter > before });
          return;
        }

        if (attempts >= maxAttempts) {
          resolve({ ok: true, source: 'poll_empty', count: 0, rows: [],
                    attempts: attempts, beginDate: beginDate, endDate: endDate,
                    send_detected: sendCounter > before,
                    note: 'no orders for date range' });
          return;
        }
        setTimeout(poll, 500);
      }
      setTimeout(poll, 500);
    });
  } catch(e) { return { ok: false, error: String(e) }; }
};

// ── readPositionsDirect: 从 setTableSourceData: 缓存读持仓 ──────────────────────
// 优先 source=setTableSourceData（新路径），兜底 protocol_response（旧路径）
// 若缓存空则派发一次 req_type=0x601 并等 5s
rpc.exports.readPositionsDirect = function() {
  try {
    // 优先读 setTableSourceData: hook 写入的缓存（key=0x601）
    var cached = _tqtCache[0x601];
    if (cached && cached.rows && cached.rows.length > 0) {
      return { ok: true, source: cached.source || 'setTableSourceData',
               count: cached.rows.length, rows: cached.rows, nType: 0x601 };
    }

    // 扫缓存：找有持仓特征（有市值/盈亏/成本，无合同号）的条目
    var keys = Object.keys(_tqtCache);
    var best = null;
    for (var i = 0; i < keys.length; i++) {
      var c = _tqtCache[keys[i]];
      if (!c || !c.rows || c.rows.length === 0) continue;
      var r0 = c.rows[0];
      var hasPos = ((r0[2125] !== undefined || r0['2125'] !== undefined ||
                     r0[2147] !== undefined || r0['2147'] !== undefined ||
                     r0[2122] !== undefined || r0['2122'] !== undefined) &&
                    (r0[2113] === undefined && r0['2113'] === undefined));
      if (hasPos && (!best || c.ts > best.ts)) best = c;
    }
    if (best) {
      return { ok: true, source: best.source || 'cache_scan',
               count: best.rows.length, rows: best.rows, nType: best.nType };
    }

    // 缓存无数据，主动派发
    delete _tqtCache[0x601];
    var before = sendCounter;
    try { buildPositionsRequest(); } catch(e) {}

    return new Promise(function(resolve) {
      var attempts = 0;
      function poll() {
        attempts++;
        var c2 = _tqtCache[0x601];
        if (c2 && c2.rows && c2.rows.length > 0) {
          resolve({ ok: true, source: c2.source || 'setTableSourceData_after_dispatch',
                    count: c2.rows.length, rows: c2.rows, nType: 0x601,
                    send_detected: sendCounter > before, attempts: attempts });
          return;
        }
        if (attempts >= 10) {
          // 再次扫全缓存
          var keys2 = Object.keys(_tqtCache);
          var best2 = null;
          for (var j = 0; j < keys2.length; j++) {
            var cc = _tqtCache[keys2[j]];
            if (!cc || !cc.rows || cc.rows.length === 0) continue;
            var r1 = cc.rows[0];
            var hp = ((r1[2125] !== undefined || r1['2125'] !== undefined ||
                       r1[2122] !== undefined || r1['2122'] !== undefined) &&
                      (r1[2113] === undefined && r1['2113'] === undefined));
            if (hp && (!best2 || cc.ts > best2.ts)) best2 = cc;
          }
          if (best2) {
            resolve({ ok: true, source: best2.source || 'cache_scan_after_dispatch',
                      count: best2.rows.length, rows: best2.rows, nType: best2.nType,
                      send_detected: sendCounter > before, attempts: attempts });
          } else {
            resolve({ ok: false, error: 'positions cache empty after dispatch',
                      send_detected: sendCounter > before, attempts: attempts,
                      cache_keys: Object.keys(_tqtCache) });
          }
          return;
        }
        setTimeout(poll, 500);
      }
      setTimeout(poll, 500);
    });
  } catch(e) { return { ok: false, error: String(e) }; }
};

// ── testUpRequest: 测试 setBeginDate:/setEndDate: + upRequestRangeDateForActiveUser 路径 ─
// params: { beginDate: 'YYYYMMDD', endDate: 'YYYYMMDD' }
rpc.exports.testUpRequest = function(params) {
  try {
    var beginDate = (params && params.beginDate) ? String(params.beginDate) : null;
    var endDate   = (params && params.endDate)   ? String(params.endDate)   : null;
    if (!beginDate || !endDate) {
      var today = new Date();
      var ymd = today.getFullYear().toString() +
                ('0'+(today.getMonth()+1)).slice(-2) +
                ('0'+today.getDate()).slice(-2);
      if (!beginDate) beginDate = ymd;
      if (!endDate)   endDate   = ymd;
    }
    var nsBegin = jsStr(beginDate);
    var nsEnd   = jsStr(endDate);

    // 找 nType=1539 的 TQT 实例
    var views = [];
    try {
      var tqtObj = ObjC.classes.TradeQueryTemplate;
      if (tqtObj) { var heap = ObjC.chooseSync(tqtObj); if (heap && heap.length > 0) views = heap; }
    } catch(e) {}
    if (views.length === 0) views = findTradeQueryTemplates();
    if (!views || views.length === 0) return { ok: false, error: 'no TQT' };

    var msgIntX = new NativeFunction(_objc_msgSend, 'int', ['pointer','pointer']);
    var tqt = null;
    for (var i = 0; i < views.length; i++) {
      try { var nt = msgIntX(views[i], sel('nType')); if (nt === 1539 || nt === 0x603) { tqt = views[i]; break; } } catch(e) {}
    }
    if (!tqt) tqt = views[0];

    // setNType + setBeginDate + setEndDate
    try { (new NativeFunction(_objc_msgSend, 'void', ['pointer','pointer','long']))(tqt, sel('setNType:'), 0x603); } catch(e) {}
    try { msg1p(tqt, sel('setBeginDate:'), nsBegin); } catch(e) {}
    try { msg1p(tqt, sel('setEndDate:'),   nsEnd);   } catch(e) {}

    // 读回确认
    var readBegin = '', readEnd = '';
    try { var rb = msg0p(tqt, sel('beginDate')); readBegin = rb && !rb.isNull() ? nsStr(rb) : 'nil'; } catch(e) {}
    try { var re = msg0p(tqt, sel('endDate'));   readEnd   = re && !re.isNull() ? nsStr(re) : 'nil'; } catch(e) {}

    var before = sendCounter;

    // ★ 路径 1: setDateForBegin:andEnd:andTitle: — app 内部查询历史委托的正确入口
    var selSetDate = sel('setDateForBegin:andEnd:andTitle:');
    var nsTitle = jsStr('\u59d4\u6258'); // "委托"
    var responds_setDate = false;
    try { responds_setDate = (new NativeFunction(_objc_msgSend, 'int', ['pointer','pointer','pointer']))(tqt, sel('respondsToSelector:'), selSetDate) !== 0; } catch(e) {}
    if (responds_setDate) {
      try {
        var fnSetDate = new NativeFunction(_objc_msgSend, 'void', ['pointer','pointer','pointer','pointer','pointer']);
        fnSetDate(tqt, selSetDate, nsBegin, nsEnd, nsTitle);
      } catch(e) {}
    }

    // ★ 路径 2: updateWeiTuoInfo — 触发 TQT 用当前属性刷新委托数据
    var responds_upRequest = false;
    var selUp = sel('upRequestRangeDateForActiveUser');
    try { responds_upRequest = (new NativeFunction(_objc_msgSend, 'int', ['pointer','pointer','pointer']))(tqt, sel('respondsToSelector:'), selUp) !== 0; } catch(e) {}
    if (responds_upRequest) {
      try { msg0p(tqt, selUp); } catch(e) {}
    }

    // ★ 路径 3: updateWeiTuoInfo
    var selUpdate = sel('updateWeiTuoInfo');
    var responds_update = false;
    try { responds_update = (new NativeFunction(_objc_msgSend, 'int', ['pointer','pointer','pointer']))(tqt, sel('respondsToSelector:'), selUpdate) !== 0; } catch(e) {}
    if (responds_update) {
      try { msg0p(tqt, selUpdate); } catch(e) {}
    }

    return new Promise(function(resolve) {
      setTimeout(function() {
        var sc_after = sendCounter;
        var rows = [];
        try { rows = readTableSourceData(tqt); } catch(e) {}
        // 也检查 cache
        var cached = _tqtCache[1539];
        resolve({
          ok: true,
          beginDate: beginDate, endDate: endDate,
          readBegin: readBegin, readEnd: readEnd,
          responds_to_setDate: responds_setDate,
          responds_to_upRequest: responds_upRequest,
          responds_to_update: responds_update,
          send_before: before, send_after: sc_after, send_delta: sc_after - before,
          tsd_rows: rows.length,
          cache_rows: cached ? (cached.rows || []).length : -1,
          rows: rows.slice(0, 3)
        });
      }, 3000);
    });
  } catch(e) { return { ok: false, error: String(e) }; }
};

// ── forceOrdersFetch: buildOrdersRequest + 15s 轮询，不做 Interceptor.replace
// params: { beginDate: 'YYYYMMDD', endDate: 'YYYYMMDD' }
rpc.exports.forceOrdersFetch = function(params) {
  try {
    var beginDate = (params && params.beginDate) ? String(params.beginDate) : null;
    var endDate   = (params && params.endDate)   ? String(params.endDate)   : null;
    if (!beginDate || !endDate) {
      var today = new Date();
      var ymd = today.getFullYear().toString() +
                ('0'+(today.getMonth()+1)).slice(-2) +
                ('0'+today.getDate()).slice(-2);
      if (!beginDate) beginDate = ymd;
      if (!endDate)   endDate   = ymd;
    }

    // 清空旧委托缓存
    delete _tqtCache[1539];

    // 派发委托请求（带日期）
    var before3 = sendCounter;
    var rr3 = buildOrdersRequest({ beginDate: beginDate, endDate: endDate });
    var send_ok = !rr3.error;
    var send_err = rr3.error || null;

    // 轮询最多 15 秒（30 × 500ms）
    return new Promise(function(resolve) {
      var attempts3 = 0;
      function poll3() {
        attempts3++;
        // ① 优先看 1539
        var c3 = _tqtCache[1539];
        if (c3 && c3.rows && c3.rows.length > 0) {
          resolve({ ok: true, source: c3.source, count: c3.rows.length, rows: c3.rows,
                    attempts: attempts3, beginDate: beginDate, endDate: endDate,
                    send_detected: sendCounter > before3 });
          return;
        }
        // ② 扫全缓存找含合同号(2113)的最新条目
        var keys3 = Object.keys(_tqtCache);
        var best3 = null;
        for (var ki3 = 0; ki3 < keys3.length; ki3++) {
          var cc3 = _tqtCache[keys3[ki3]];
          if (!cc3 || !cc3.rows || cc3.rows.length === 0) continue;
          var r3 = cc3.rows[0];
          if (r3[2113] !== undefined || r3['2113'] !== undefined) {
            if (!best3 || cc3.ts > best3.ts) best3 = cc3;
          }
        }
        if (best3) {
          resolve({ ok: true, source: best3.source || 'cache_scan_force',
                    count: best3.rows.length, rows: best3.rows,
                    attempts: attempts3, beginDate: beginDate, endDate: endDate,
                    send_detected: sendCounter > before3 });
          return;
        }
        // ③ 超时：返回缓存摘要
        if (attempts3 >= 30) {
          var summary3 = {};
          var kk3 = Object.keys(_tqtCache);
          for (var si3 = 0; si3 < kk3.length; si3++) {
            var scc3 = _tqtCache[kk3[si3]];
            summary3[kk3[si3]] = { count: (scc3.rows||[]).length, source: scc3.source };
          }
          resolve({ ok: false, error: 'orders empty after 15s', attempts: attempts3,
                    beginDate: beginDate, endDate: endDate,
                    send_detected: sendCounter > before3, send_ok: send_ok, send_err: send_err,
                    cache_summary: summary3 });
          return;
        }
        setTimeout(poll3, 500);
      }
      setTimeout(poll3, 500);
    });
  } catch(e) { return { ok: false, error: String(e) }; }
};

function _order(reqType, params) {
  try {
    if (!params || !params.stockcode || !params.qty || !params.price)
      return { ok: false, error: 'need stockcode, qty, price' };
    var au = getActiveMoniUser();
    if (!au.ok) return { ok: false, error: au.error };
    var before = sendCounter;
    var b = buildTradeRequest(reqType, {
      stockcode: params.stockcode,
      mkcode: params.mkcode || '1',
      qty: params.qty,
      price: params.price
    });
    if (!b.ok) return { ok: false, error: b.error };
    var d = dispatchTrade(b.req, au.user);
    if (!d.ok) return { ok: false, error: d.error };
    return new Promise(function(resolve) {
      setTimeout(function() {
        var sent = sendCounter - before;
        resolve({
          ok: true,
          action: reqType === 0x102 ? 'buy' : 'sell',
          stockcode: params.stockcode, qty: params.qty, price: params.price,
          nsInsid_set: b.nsInsidSet, send_detected: sent > 0, send_count: sent
        });
      }, 350);
    });
  } catch(e) { return { ok: false, error: String(e) }; }
}
