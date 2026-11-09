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

// ── send detection ───────────────────────────────────────────────────────────
var sendCounter = 0;
var sendLog = [];
var SEND_LOG_CAP = 64;

(function hookSend() {
  var imp = impFor('HXSocketCenter', 'writeTextData:withTimeout:delegate:instance:', false);
  if (!imp) { sendLog.push({error: 'HXSocketCenter writeTextData not found'}); return; }
  Interceptor.attach(imp, {
    onEnter: function (args) {
      sendCounter++;
      var rec = { seq: sendCounter, ts: Date.now(), thread: this.threadId };
      try { rec.data = nsStr(args[2]); } catch (e) { rec.data = '<?>'; }
      sendLog.push(rec);
      if (sendLog.length > SEND_LOG_CAP) sendLog.shift();
      if (captureActive) pushCapture({ kind: 'SEND', seq: sendCounter, ts: rec.ts, data: rec.data });
    }
  });
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

function installTQTHook() {
  if (_tqtCacheHooked) return;
  var imp = impFor('TradeQueryTemplate', 'OnTradeRsponceNotify:', false);
  if (!imp) { return; }
  var msgInt0 = new NativeFunction(_objc_msgSend, 'int', ['pointer','pointer']);

  function snapshotTQT(self_obj) {
    try {
      var nType = msgInt0(self_obj, sel('nType'));
      var arr = msg0p(self_obj, sel('tableSourceData'));
      if (!arr || arr.isNull()) return;
      var n = Number(msg0u(arr, sel('count')));
      var rows = [];
      for (var i = 0; i < n; i++) {
        var rowObj = msg1p(arr, sel('objectAtIndexedSubscript:'), ptr(i));
        rows.push(dumpNSDict(rowObj));
      }
      // 只在有数据或之前没有缓存时写入，避免 onLeave 清空 onEnter 已缓存的数据
      var existing = _tqtCache[nType];
      if (rows.length > 0 || !existing || existing.rows.length === 0) {
        _tqtCache[nType] = { ts: Date.now(), rows: rows, nType: nType };
      }
    } catch(e) {}
  }

  Interceptor.attach(imp, {
    onEnter: function(args) {
      this._self = args[0];
      snapshotTQT(args[0]);
    },
    onLeave: function(retval) {
      // 方法执行完毕后再读一次，此时数据可能已填充
      if (this._self) snapshotTQT(this._self);
    }
  });
  _tqtCacheHooked = true;
}

(function(){ try { installTQTHook(); } catch(e) {} })();

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
function findTradeQueryTemplates() {
  var NSApp = msg0p(cls('NSApplication'), sel('sharedApplication'));
  if (!NSApp || NSApp.isNull()) return [];
  var wins = msg0p(NSApp, sel('windows'));
  if (!wins || wins.isNull()) return [];
  var wn = Number(msg0u(wins, sel('count')));
  var found = [];
  for (var i = 0; i < wn; i++) {
    var w = msg1p(wins, sel('objectAtIndex:'), ptr(i));
    if (!w || w.isNull()) continue;
    var cv = msg0p(w, sel('contentView'));
    if (!cv || cv.isNull()) continue;
    collectTQT(cv, found, 0);
    if (found.length >= 4) break;
  }
  return found;
}

function collectTQT(view, acc, depth) {
  if (!view || view.isNull() || depth > 20) return;
  if (msg1i(view, sel('respondsToSelector:'), sel('tableSourceData')) !== 0) {
    acc.push(view);
  }
  var subs = msg0p(view, sel('subviews'));
  if (!subs || subs.isNull()) return;
  var n = Number(msg0u(subs, sel('count')));
  for (var i = 0; i < n; i++) {
    var sub = msg1p(subs, sel('objectAtIndex:'), ptr(i));
    collectTQT(sub, acc, depth + 1);
  }
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
  try {
    var desc = nsStr(msg0p(view, sel('description')));
    if (desc) {
      var m = desc.match(/frame\s*=\s*\{([^}]*)\}/);
      if (m) return m[1].replace(/\s+/g, '');
    }
  } catch(e) {}
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

// 遍历所有窗口找 NSTableView — 必须在主线程调用
function collectNSTableViews(view, acc, depth) {
  if (!view || view.isNull() || depth > 30) return;
  if (msg1i(view, sel('respondsToSelector:'), sel('numberOfRows')) !== 0 &&
      msg1i(view, sel('respondsToSelector:'), sel('tableColumns')) !== 0) {
    acc.push(view);
    return;
  }
  var subs = msg0p(view, sel('subviews'));
  if (!subs || subs.isNull()) return;
  var n = Number(msg0u(subs, sel('count')));
  for (var i = 0; i < n; i++) {
    var sub = msg1p(subs, sel('objectAtIndex:'), ptr(i));
    collectNSTableViews(sub, acc, depth + 1);
  }
}

// findAllNSTableViews — direct sync call, no dispatch needed
function findAllNSTableViews() {
  var NSApp = msg0p(cls('NSApplication'), sel('sharedApplication'));
  if (!NSApp || NSApp.isNull()) return { error: 'NSApplication nil' };
  var wins = msg0p(NSApp, sel('windows'));
  if (!wins || wins.isNull()) return { error: 'windows nil' };
  var wn = Number(msg0u(wins, sel('count')));
  var allViews = [];
  for (var i = 0; i < wn; i++) {
    var w = msg1p(wins, sel('objectAtIndex:'), ptr(i));
    if (!w || w.isNull()) continue;
    var cv = msg0p(w, sel('contentView'));
    if (!cv || cv.isNull()) continue;
    collectNSTableViews(cv, allViews, 0);
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
  var u = msg0p(um, sel('getActiveUser'));
  if (!u || u.isNull()) return { error: 'getActiveUser nil' };
  var n = Number(msg0u(moniArr, sel('count')));
  var isMoni = false;
  for (var i = 0; i < n; i++) {
    if (msg1p(moniArr, sel('objectAtIndex:'), ptr(i)).equals(u)) { isMoni = true; break; }
  }
  if (!isMoni) return { error: 'REFUSED: active user is not a moni account' };
  return { ok: true, user: u, um: um };
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
function buildPositionsRequest() {
  var au = getActiveMoniUser();
  if (!au.ok) return { error: au.error };

  var trCls = cls('TradeRequest');
  if (!trCls) return { error: 'TradeRequest class not found' };
  var req = msg0p(msg0p(trCls, sel('alloc')), sel('init'));
  if (!req || req.isNull()) return { error: 'TradeRequest alloc/init failed' };

  // 使用协议确证的 nsInsid=0x186eb（持仓查询专用），直接传 int64
  msgSetNsInsid(req, sel('setNsInsid:'), 0x186eb);
  msg1p(req, sel('setDelegate:'), ptr(0));
  msgSetInt(req, sel('setRequestParamInteger:Value:'), jsStr('req_type'), 0x601);

  var d = dispatchTrade(req, au.user);
  if (!d.ok) return { error: d.error };
  return { ok: true };
}

// ── buildOrdersRequest ────────────────────────────────────────────────────────
// nsInsid=0x186da (100058), req_type=0x603 (1539) — 已确证
function buildOrdersRequest() {
  var au = getActiveMoniUser();
  if (!au.ok) return { error: au.error };

  var trCls = cls('TradeRequest');
  if (!trCls) return { error: 'TradeRequest class not found' };
  var req = msg0p(msg0p(trCls, sel('alloc')), sel('init'));
  if (!req || req.isNull()) return { error: 'TradeRequest alloc/init failed' };

  // 使用协议确证的 nsInsid=0x186da（委托查询专用），直接传 int64
  msgSetNsInsid(req, sel('setNsInsid:'), 0x186da);
  msg1p(req, sel('setDelegate:'), ptr(0));
  // req_type=1539 (0x603) 委托查询，已证实
  msgSetInt(req, sel('setRequestParamInteger:Value:'), jsStr('req_type'), 0x603);

  var d = dispatchTrade(req, au.user);
  if (!d.ok) return { error: d.error };
  return { ok: true };
}

// ── RPC exports ──────────────────────────────────────────────────────────────
rpc.exports = {
  ping: function () {
    return { ok: true, agent: 'ths_agent', ts: Date.now() };
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
        moni_accounts: moniAccts, send_counter: sendCounter
      };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  readTableData: function () {
    try {
      var views = findTradeQueryTemplates();
      if (!views || views.length === 0) return { ok: false, error: 'no TradeQueryTemplate found' };
      var result = [];
      for (var i = 0; i < views.length; i++) {
        var rows = readTableSourceData(views[i]);
        result.push({ ptr: views[i].toString(), row_count: rows.length, rows: rows });
      }
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

      // ── 递归收集 view 树中所有文本 ──────────────────────────────────────
      function collectTexts(view, depth) {
        if (!view || view.isNull() || depth > 12) return [];
        var texts = [];
        // 取 className
        var clsName = '';
        try {
          var clsObj = msg0p(view, sel('class'));
          if (clsObj && !clsObj.isNull()) {
            var clsNamePtr = msg0p(clsObj, sel('description'));
            clsName = nsStr(clsNamePtr) || '';
          }
        } catch(e) {}
        // 尝试读 stringValue
        var sv = '';
        try {
          var svp = msg0p(view, sel('stringValue'));
          if (svp && !svp.isNull()) sv = nsStr(svp) || '';
        } catch(e) {}
        if (sv !== '') texts.push({ cls: clsName, text: sv });
        // 如果 stringValue 为空，尝试 attributedStringValue → string
        if (sv === '') {
          try {
            var asvp = msg0p(view, sel('attributedStringValue'));
            if (asvp && !asvp.isNull()) {
              var s2 = nsStr(msg0p(asvp, sel('string')));
              if (s2 && s2 !== '') texts.push({ cls: clsName, text: s2 });
            }
          } catch(e) {}
        }
        // 递归 subviews
        try {
          var subs = msg0p(view, sel('subviews'));
          if (subs && !subs.isNull()) {
            var sn = Number(msg0u(subs, sel('count')));
            for (var si2 = 0; si2 < sn; si2++) {
              var sub = msg1p(subs, sel('objectAtIndex:'), ptr(si2));
              var childTexts = collectTexts(sub, depth + 1);
              for (var ci = 0; ci < childTexts.length; ci++) texts.push(childTexts[ci]);
            }
          }
        } catch(e) {}
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
                  var ct = collectTexts(cellView, 0);
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
            // 有 rowView，递归收集所有文本
            var allTexts = collectTexts(rowView, 0);
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
      var rr = buildPositionsRequest();
      if (rr.error) return { ok: false, error: 'dispatch: ' + rr.error };
      var before = sendCounter;
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
            // fallback: 扫缓存中持仓特征的 nType（有 2038/持仓量字段）
            var best = null;
            var keys = Object.keys(_tqtCache);
            for (var i = 0; i < keys.length; i++) {
              var c = _tqtCache[keys[i]];
              if (!c || !c.rows || c.rows.length === 0) continue;
              // 持仓行有成本/数量字段（key 范围 2013-2070）
              var r0 = c.rows[0];
              var hasPos = (r0[2038] !== undefined || r0['2038'] !== undefined ||
                            r0[2039] !== undefined || r0['2039'] !== undefined);
              if (hasPos && (!best || c.ts > best.ts)) best = c;
            }
            if (best) {
              resolve({ ok: true, source: 'protocol_response_scan',
                        nType: best.nType, ts: best.ts,
                        count: best.rows.length, rows: best.rows,
                        send_detected: sendCounter > before });
              return;
            }
            // 返回缓存摘要供调试
            var summary = {};
            for (var si = 0; si < keys.length; si++) {
              var cc = _tqtCache[keys[si]];
              summary[keys[si]] = {
                count: cc.rows.length,
                first_keys: cc.rows.length > 0 ? Object.keys(cc.rows[0]).slice(0, 15) : []
              };
            }
            resolve({ ok: false, error: 'positions cache empty after 2.5s',
                      cache_summary: summary, send_detected: sendCounter > before });
          } catch(e) { resolve({ ok: false, error: String(e) }); }
        }, 2500);
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
      // 清除旧的委托缓存，确保拿到本次刷新的数据
      delete _tqtCache[0x603];
      var rr = buildOrdersRequest();
      if (rr.error) return { ok: false, error: 'dispatch: ' + rr.error };
      var before = sendCounter;
      return new Promise(function(resolve) {
        setTimeout(function() {
          try {
            var cached = _tqtCache[0x603];
            if (cached && cached.rows && cached.rows.length > 0) {
              resolve({ ok: true, source: 'protocol_response',
                        nType: 0x603, ts: cached.ts,
                        count: cached.rows.length, rows: cached.rows,
                        send_detected: sendCounter > before });
              return;
            }
            // fallback: 扫缓存中有合同号字段 2113 的 nType
            var best = null;
            var keys = Object.keys(_tqtCache);
            for (var i = 0; i < keys.length; i++) {
              var c = _tqtCache[keys[i]];
              if (!c || !c.rows || c.rows.length === 0) continue;
              var r0 = c.rows[0];
              if ((r0[2113] !== undefined || r0['2113'] !== undefined) &&
                  (!best || c.ts > best.ts)) best = c;
            }
            if (best) {
              resolve({ ok: true, source: 'protocol_response_scan',
                        nType: best.nType, ts: best.ts,
                        count: best.rows.length, rows: best.rows,
                        send_detected: sendCounter > before });
              return;
            }
            // 返回缓存摘要供调试
            var summary = {};
            for (var si = 0; si < keys.length; si++) {
              var cc = _tqtCache[keys[si]];
              summary[keys[si]] = {
                count: cc.rows.length,
                first_keys: cc.rows.length > 0 ? Object.keys(cc.rows[0]).slice(0, 15) : []
              };
            }
            resolve({ ok: false, error: 'orders cache empty after 2.5s',
                      cache_summary: summary, send_detected: sendCounter > before });
          } catch(e) { resolve({ ok: false, error: String(e) }); }
        }, 2500);
      });
    } catch(e) { return { ok: false, error: String(e) }; }
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
