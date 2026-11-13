'use strict';
/*
 * ths_agent.js — Frida agent for 同花顺 (THSX)
 *
 * ★ 架构 v3：单钩子
 *   唯一钩子：-[TradeQueryTemplate setTableSourceData:]
 *     捕获持仓 / 委托 / 成交 的 tableSourceData 快照进内存缓存。
 *     不 hook syscall、不 hook NSTableView、不遍历视图树。
 *
 * RPC exports：
 *   ping / state
 *   primeCache / getCacheStatus
 *   readPositionsDirect / readOrdersDirect / readTradesDirect
 *   readAccountDirect   ← UserManager→getActiveUser→getUserInfo→zichanData
 *                          （纯 getter，无 hook、无点击，资金九字段）
 *   sendChedanByIndex   ← 撤单只走 App 自己的 -[TradeQueryTemplate
 *                          sendChedanRequestByTableIndex:]，不手拼 req_type/nsInsid
 *   buy / sell / login / loginSim
 */

// ── libobjc C API ────────────────────────────────────────────────────────────
var libobjc = Module.load('/usr/lib/libobjc.A.dylib');
var _objc_getClass           = new NativeFunction(libobjc.getExportByName('objc_getClass'),            'pointer', ['pointer']);
var _class_getInstanceMethod = new NativeFunction(libobjc.getExportByName('class_getInstanceMethod'),  'pointer', ['pointer','pointer']);
var _class_getClassMethod    = new NativeFunction(libobjc.getExportByName('class_getClassMethod'),     'pointer', ['pointer','pointer']);
var _method_getImplementation= new NativeFunction(libobjc.getExportByName('method_getImplementation'), 'pointer', ['pointer']);
var _sel_registerName        = new NativeFunction(libobjc.getExportByName('sel_registerName'),         'pointer', ['pointer']);
var _objc_msgSend            = libobjc.getExportByName('objc_msgSend');
var _object_getClass         = new NativeFunction(libobjc.getExportByName('object_getClass'),          'pointer', ['pointer']);
var _class_getName           = new NativeFunction(libobjc.getExportByName('class_getName'),            'pointer', ['pointer']);

// ── typed msgSend wrappers ───────────────────────────────────────────────────
var msg0p = new NativeFunction(_objc_msgSend, 'pointer', ['pointer','pointer']);
var msg1p = new NativeFunction(_objc_msgSend, 'pointer', ['pointer','pointer','pointer']);
var msg2p = new NativeFunction(_objc_msgSend, 'pointer', ['pointer','pointer','pointer','pointer']);
var msg0i = new NativeFunction(_objc_msgSend, 'int',     ['pointer','pointer']);
var msg1i = new NativeFunction(_objc_msgSend, 'int',     ['pointer','pointer','pointer']);
var msg0u = new NativeFunction(_objc_msgSend, 'uint64',  ['pointer','pointer']);
var msgSetInt     = new NativeFunction(_objc_msgSend, 'pointer', ['pointer','pointer','pointer','int64']);
var msgSetNsInsid = new NativeFunction(_objc_msgSend, 'void',    ['pointer','pointer','int64']);
// 单 int 参数（-[UserModel SetType:(int)]）—— 注意 msgSetInt 是 2 参数的，
// 拿它调 SetType: 会把 int 塞进错误寄存器，等于什么都没设。
var msg1int = new NativeFunction(_objc_msgSend, 'pointer', ['pointer','pointer','int']);

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

// ── NSString / cStr helpers ──────────────────────────────────────────────────
var NSUTF8 = 0x08000100;
function jsStr(s) {
  var c = Memory.allocUtf8String(String(s));
  return msg2p(cls('NSString'), sel('stringWithCString:encoding:'), c, ptr(NSUTF8));
}
function cStr(s) {
  return Memory.allocUtf8String(String(s));
}
function nsStr(p) {
  if (!p || p.isNull()) return null;
  try {
    var utf8 = msg0p(p, sel('UTF8String'));
    if (!utf8 || utf8.isNull()) return null;
    return utf8.readUtf8String();
  } catch(e) { return null; }
}
function className(p) {
  try {
    var c = _object_getClass(p);
    if (!c || c.isNull()) return '?';
    var nm = _class_getName(c);
    if (!nm || nm.isNull()) return '?';
    return String(nm.readUtf8String());
  } catch (e) { return '?'; }
}
var _class_getSuperclass = new NativeFunction(libobjc.getExportByName('class_getSuperclass'), 'pointer', ['pointer']);
function isKindOfClassName(p, want) {
  try {
    var c = _object_getClass(p);
    while (c && !c.isNull()) {
      var nm = _class_getName(c);
      if (nm && !nm.isNull() && String(nm.readUtf8String()) === want) return true;
      c = _class_getSuperclass(c);
    }
  } catch (e) {}
  return false;
}
function nsIntVal(p) {
  if (!p || p.isNull()) return null;
  return msg0u(p, sel('integerValue'));
}

// ── send detection ───────────────────────────────────────────────────────────
var sendCounter = 0;

// ── NSDictionary → JS object ─────────────────────────────────────────────────
function dumpNSDict(d) {
  if (!d || d.isNull()) return {};
  try {
    var keys = msg0p(d, sel('allKeys'));
    if (!keys || keys.isNull()) return {};
    var n = Number(msg0u(keys, sel('count')));
    var row = {};
    for (var k = 0; k < n; k++) {
      try {
        var key = msg1p(keys, sel('objectAtIndex:'), ptr(k));
        var kv  = Number(nsIntVal(key));
        var val = msg1p(d, sel('objectForKey:'), key);
        var vs  = nsStr(val);
        if (vs === null) {
          try { vs = String(Number(nsIntVal(val))); } catch(e) {
            vs = val ? val.toString() : null;
          }
        }
        row[kv] = vs;
      } catch(ek) {}
    }
    return row;
  } catch(e) { return {}; }
}

// ── 缓存分类判断 ──────────────────────────────────────────────────────────────
// ★ 2026-11-12 现场实测（字段号来源：hook 收到的 tableSourceData 每行 description 原文）
//   持仓行 3 行：{2117 持仓量, 2118 冻结, 2164 可用, 2121 股份余额, 2122 成本,
//                 2124 现价, 2125 市值, 2147 盈亏, 2102 代码, 2103 名称,
//                 2106 股东账户, 2108 市场, 2135="", 2109="", 4008=""}
//   委托行 9 行（当日全部委托，含已成交/已撤）：
//     {2139=20261112 委托日期, 2140=11:19:48 委托时间, 2102=159928 代码,
//      2103=消费ETF汇 名称, 2109=买入 方向, 2105=未成交 状态, 2126=5000 委托量,
//      2120=0 撤销数量, 2127=0.686 委托价, 2129=0.000 成交价, 2135=6359383521 合同号,
//      2108=深圳Ａ股 市场, 2106=00105815865 股东账户, 4008=0 委托属性}
//   成交行 4 行（当日成交明细）：
//     {2141=20261112 成交日期, 2142=14:30:36 成交时间, 2102=512890 代码,
//      2103=红利低波ET 名称, 2109=卖出 方向, 2128=100 成交量, 2129=1.229 成交价,
//      2131=122.900 成交金额, 2135=6359272087 合同号, 2130=6725345540 成交编号,
//      2108=上海Ａ股 市场, 2106=A481535989 股东账户, 2165/2147/4008 空}
//   判据：
//     委托 = 2139（委托日期）且 2135（合同号）——成交行也有 2135，但没有 2139；
//     成交 = 2141（成交日期）且 2128（成交量）；
//     持仓 = 2122（成本）/ 2125（市值）。
function _classifyRows(rows) {
  if (!rows || rows.length === 0) return null;
  var r0 = rows[0];
  var hasKey = function(k) {
    return (r0[k] !== undefined && r0[k] !== null) ||
           (r0[String(k)] !== undefined && r0[String(k)] !== null);
  };

  // 委托：2139（委托日期）+ 2135（合同号）
  if (hasKey(2139) && hasKey(2135)) return 'orders';
  // 成交：2141（成交日期）+ 2128（成交量）
  //   ★ 成交行同样带 2135（合同号），只是没有 2139，绝不能按 2135 单独归成委托
  if (hasKey(2141) && hasKey(2128)) return 'trades';
  // 持仓：2122（成本）与 2125（市值）
  if (hasKey(2122) && hasKey(2125)) return 'positions';
  // 宽松持仓（只有 2122 或只有 2125）
  if (hasKey(2122) || hasKey(2125)) return 'positions';
  return null;
}

// ── 三级缓存 ──────────────────────────────────────────────────────────────────
var _cache = {
  positions: { rows: [], ts: 0, instance: null },
  orders:    { rows: [], ts: 0, instance: null },
  trades:    { rows: [], ts: 0, instance: null },
};

// ── 已捕获的 TQT 实例指针（用于主动派发查询）─────────────────────────────────
// key: 'positions'|'orders'|'trades'  value: NativePointer
var _tqtInst = {
  positions: null,
  orders:    null,
  trades:    null,
};
// 所有见过的「委托」TQT 实例（同一窗口/新窗口会各有一个，tableSourceData 各自独立）。
// 撤单按合同号在这些实例里找行，避免用到那个 0 行的旧实例。
var _tqtOrdersList = [];
function _rememberOrdersInst(p) {
  if (!p || p.isNull()) return;
  var key = p.toString();
  for (var i = 0; i < _tqtOrdersList.length; i++) {
    if (_tqtOrdersList[i].toString() === key) return;
  }
  _tqtOrdersList.push(p);
  if (_tqtOrdersList.length > 8) _tqtOrdersList.shift();
}

// ── 待归属分类（确定性归属，主判据）────────────────────────────────────────────
// requestQueryDirect 派发前设置，hook 收到快照时优先用这个分类登记实例。
// 空列表（0 行）也照登记，彻底消除「空列表无法归类」的死循环。
var _pendingCat = null;

// ── 程序化创建 TradeQueryTemplate（去掉「界面创建实例」这最后一个依赖）─────────
// 反编译依据：
//   HXBaseTradeView::loadFormNibName:bundle:  = NSNib 实例化后返回第一个
//     isKindOfClass:[self class] 的顶层对象（TradeQueryTemplate 继承它）
//   三个 nib 内的 NSTableView.identifier 自带 req_type：
//     JYInQuotaQuickTradePTWTSearchView → 1539（委托）
//     JYInQuotaQuickTradePTCJSearchView → 1538（成交）
//     JYInQuotaQuickTradePTCCSearchView → 1537（持仓）
//   调用链：nib 创建 → didAppear（内部 didLoad + requestByType:BeginDate:EndDate:）
//   全程不点任何按钮、不遍历视图树、不 hook UserManager。
var _TQT_NIB = {
  positions: 'JYInQuotaQuickTradePTCCSearchView',
  orders:    'JYInQuotaQuickTradePTWTSearchView',
  trades:    'JYInQuotaQuickTradePTCJSearchView',
};
// 本 agent 自己创建（并 objc_retain 持有）的实例，避免被 autorelease 回收
var _tqtOwned = { positions: null, orders: null, trades: null };

var _objc_retain = new NativeFunction(libobjc.getExportByName('objc_retain'), 'pointer', ['pointer']);

function _configBundle() {
  var cmCls = cls('configmanager');
  if (!cmCls) return null;
  var cm = msg0p(cmCls, sel('shareInstance'));
  if (!cm || cm.isNull()) return null;
  var b = msg0p(cm, sel('GetBundle'));
  return (b && !b.isNull()) ? b : null;
}

function _performOnMain(obj, selectorName, arg) {
  var fn = new NativeFunction(_objc_msgSend, 'void',
    ['pointer','pointer','pointer','pointer','int8']);
  var mainSels = sel('performSelectorOnMainThread:withObject:waitUntilDone:');
  fn(obj, mainSels, sel(selectorName), arg || ptr(0), 0);
}

// 创建（或复用）某一分类的 TQT 实例；成功返回 { ok, ptr, created }
function _createQueryTemplate(cat) {
  if (_tqtOwned[cat] && !_tqtOwned[cat].isNull())
    return { ok: true, ptr: _tqtOwned[cat], created: false };
  if (_tqtInst[cat] && !_tqtInst[cat].isNull())
    return { ok: true, ptr: _tqtInst[cat], created: false };

  var tqtCls = cls('TradeQueryTemplate');
  if (!tqtCls) return { ok: false, error: 'TradeQueryTemplate class not found' };
  var bundle = _configBundle();
  if (!bundle) return { ok: false, error: 'configmanager.GetBundle nil' };
  var nib = _TQT_NIB[cat];
  if (!nib) return { ok: false, error: 'unknown category: ' + cat };

  var inst = null;
  try {
    inst = msg2p(tqtCls, sel('loadFormNibName:bundle:'), jsStr(nib), bundle);
  } catch(e) {
    return { ok: false, error: 'loadFormNibName:bundle: threw: ' + e };
  }
  if (!inst || inst.isNull())
    return { ok: false, error: 'loadFormNibName:bundle: returned nil for ' + nib };

  inst = _objc_retain(inst);          // ★ 顶层对象无 superview 持有，必须自己 retain
  _tqtOwned[cat] = inst;
  _tqtInst[cat]  = inst;

  try { msg1p(inst, sel('setViewName:'), jsStr(nib)); } catch(e) {}

  // ★ 在 AppKit 主线程执行 didAppear：设置 didLoad、日期、通知观察者，
  //   并自动调 requestByType:BeginDate:EndDate:（内部即发起查询）。
  //   用 performSelectorOnMainThread 而不是 ObjC.schedule：本进程没有 ObjC bridge。
  try {
    _performOnMain(inst, 'didAppear');
  } catch(e) {
    return { ok: true, ptr: inst, created: true, warn: 'didAppear schedule failed: ' + e };
  }

  // 持仓（1537）走 requestChicang + notification，没有 delegate 回调，
  // 3 秒后若缓存仍空，用模板自己的 refreshTableWithType: 从 App 内部数据对象再拉一次。
  if (cat === 'positions') {
    try {
      setTimeout(function() {
        try {
          if (_cache.positions.rows.length === 0) {
            _performOnMain(inst, 'refreshTableWithType:', jsStr('1537'));
          }
        } catch(e) {}
      }, 3000);
    } catch(e) {}
  }

  return { ok: true, ptr: inst, created: true, nib: nib };
}


// ── 重入锁 ───────────────────────────────────────────────────────────────────
var _hookInProgress = false;

// ── 唯一钩子：-[TradeQueryTemplate setTableSourceData:] ───────────────────────
(function installHook() {
  var tqtCls = cls('TradeQueryTemplate');
  if (!tqtCls) {
    send({ type: 'log', text: '[ths_agent] TradeQueryTemplate class not found, hook skipped' });
    return;
  }
  var selTSD = sel('setTableSourceData:');
  var method = _class_getInstanceMethod(tqtCls, selTSD);
  if (!method || method.isNull()) {
    send({ type: 'log', text: '[ths_agent] setTableSourceData: method not found, hook skipped' });
    return;
  }

  Interceptor.attach(_method_getImplementation(method), {
    onEnter: function(args) {
      // 重入锁：防止递归崩溃
      if (_hookInProgress) {
        this._skip = true;
        return;
      }
      _hookInProgress = true;
      this._skip = false;

      try {
        // args[0] = self (TradeQueryTemplate 实例)
        // args[1] = _cmd
        // args[2] = NSArray (tableSourceData)
        var instPtr = args[0];
        var arrPtr  = args[2];

        // ★ 确定性归属：派发前 requestQueryDirect 设置了 _pendingCat，立即消费
        // 即使列表为空也要登记实例，彻底消除「空列表无法归类」死循环
        var pendingSnapshot = _pendingCat;
        _pendingCat = null; // 立即清空，防止后续递归误用

        if (!arrPtr || arrPtr.isNull()) {
          _hookInProgress = false;
          this._skip = true;
          return;
        }

        var n = 0;
        try { n = Number(msg0u(arrPtr, sel('count'))); } catch(e) {}

        if (n === 0) {
          // ★ 空列表：登记实例指针；但不覆盖已有非空缓存（防止把有效数据清零）
          if (pendingSnapshot && _cache[pendingSnapshot] !== undefined) {
            if (instPtr && !instPtr.isNull()) {
              _tqtInst[pendingSnapshot] = instPtr;
              if (pendingSnapshot === 'orders') _rememberOrdersInst(instPtr);
            }
            // 只有当前缓存本身为空时才写空快照（更新 ts 供轮询感知）
            if (_cache[pendingSnapshot].rows.length === 0) {
              _cache[pendingSnapshot].ts       = Date.now();
              _cache[pendingSnapshot].instance = instPtr ? instPtr.toString() : null;
            }
            // 否则：保留已有非空缓存，只更新实例指针（已在上面完成）
          }
          _hookInProgress = false;
          this._skip = true;
          return;
        }

        // 逐行读取：只用 objectAtIndex: + description（不调用任何 UserManager 方法）
        var rows = [];
        for (var i = 0; i < n; i++) {
          try {
            var item = msg1p(arrPtr, sel('objectAtIndex:'), ptr(i));
            if (!item || item.isNull()) continue;
            rows.push(dumpNSDict(item));
          } catch(er) {}
        }

        // ★ pendingSnapshot 优先；行内容特征判断作辅助兜底（当 pendingCat 为空时）
        var category = pendingSnapshot || _classifyRows(rows) || null;

        if (category && _cache[category] !== undefined) {
          _cache[category].rows     = rows;
          _cache[category].ts       = Date.now();
          _cache[category].instance = instPtr ? instPtr.toString() : null;
          // 保存真实 NativePointer 供 requestQueryDirect 使用
          if (instPtr && !instPtr.isNull()) {
            _tqtInst[category] = instPtr;
            if (category === 'orders') _rememberOrdersInst(instPtr);
          }
        }
      } catch(outer) {
        // 捕获所有异常，不让钩子崩溃
      } finally {
        _hookInProgress = false;
      }
    }
  });

  send({ type: 'log', text: '[ths_agent] single hook installed: -[TradeQueryTemplate setTableSourceData:]' });
})();

// ── 诊断：交易请求真正下发到 commumodule 的次数（低频，仅下单/撤单时触发）─────
// -[commumodule kQvYehFExlWUHfgW:] 见 /tmp/ths_decomp2/functions/0x100adc6d0.c
var commuSendCount = 0;
var commuLastReqType = null;
(function hookCommuSend() {
  try {
    var c = cls('commumodule');
    if (!c) return;
    var m = _class_getInstanceMethod(c, sel('kQvYehFExlWUHfgW:'));
    if (!m || m.isNull()) return;
    Interceptor.attach(_method_getImplementation(m), {
      onEnter: function (args) {
        commuSendCount++;
        try { commuLastReqType = nsStr(msg0p(args[2], sel('getReqTypeString'))); } catch (e) {}
      }
    });
    send({ type: 'log', text: '[ths_agent] commumodule send hook installed' });
  } catch (e) { send({ type: 'log', text: '[ths_agent] commu hook error: ' + e }); }
})();

// ── 捕获 JYMaiMaiView 实例（低频：视图创建时一次）──────────────────────────
// 反编译依据：/tmp/ths_decomp2/functions/0x100ae7278.c -[JYMaiMaiView onTrade:]
//   写入 mmParam(strCode/strPrice/strAmount/strSetCode/strGdzh/nReqType)
//   → -[JYMaiMaiView sendWeituoRequest] → _sendWeituoRequestWithParams:
//   → TradeRequest + -[UserModel XIXBiQhWtIEfT_DW:]
// 我们只捕获实例指针，绝不遍历视图树、不改 App 状态。
var _maiMaiView = null;
(function hookMaiMaiView() {
  try {
    var c = cls('JYMaiMaiView');
    if (!c) { send({ type: 'log', text: '[ths_agent] JYMaiMaiView not found, view hook skipped' }); return; }
    ['initWithCoder:', 'initWithFrame:'].forEach(function (selName) {
      var m = _class_getInstanceMethod(c, sel(selName));
      if (!m || m.isNull()) return;
      Interceptor.attach(_method_getImplementation(m), {
        onEnter: function (args) { this._self = args[0]; },
        onLeave: function () {
          if (this._self && !this._self.isNull() && isKindOfClassName(this._self, 'JYMaiMaiView'))
            _maiMaiView = this._self;
        }
      });
    });
    send({ type: 'log', text: '[ths_agent] JYMaiMaiView hook installed' });
  } catch (e) { send({ type: 'log', text: '[ths_agent] JYMaiMaiView hook error: ' + e }); }
})();

// ── 活跃用户（仅用于交易派发，不在钩子里调用）──────────────────────────────
function getActiveMoniUser() {
  var um = msg0p(cls('UserManager'), sel('shareInstance'));
  if (!um || um.isNull()) return { error: 'UserManager nil' };
  var moniArr = msg0p(um, sel('GetMoniUserArr'));
  if (!moniArr || moniArr.isNull() || msg0u(moniArr, sel('count')) === 0)
    return { error: 'GetMoniUserArr empty — 请先点「模拟」登录' };
  var n = Number(msg0u(moniArr, sel('count')));

  var u = msg0p(um, sel('getActiveUser'));
  if (u && !u.isNull()) {
    for (var i = 0; i < n; i++) {
      if (msg1p(moniArr, sel('objectAtIndex:'), ptr(i)).equals(u))
        return { ok: true, user: u, um: um };
    }
  }
  var fallback = msg1p(moniArr, sel('objectAtIndex:'), ptr(0));
  if (fallback && !fallback.isNull())
    return { ok: true, user: fallback, um: um, via_fallback: true };
  return { error: 'GetMoniUserArr has 0 valid entries' };
}

// ── 派发 TradeRequest ─────────────────────────────────────────────────────
function dispatchTrade(req, user) {
  var s = sel('XIXBiQhWtIEfT_DW:');
  if (msg1i(user, sel('respondsToSelector:'), s) === 0)
    return { error: 'active user does not respond to XIXBiQhWtIEfT_DW:' };
  msg1p(user, s, req);
  sendCounter++;
  return { ok: true };
}

function buildTradeRequest(reqType, p) {
  var trCls = cls('TradeRequest');
  if (!trCls) return { error: 'TradeRequest class not found' };
  var req = msg0p(msg0p(trCls, sel('alloc')), sel('init'));
  if (!req || req.isNull()) return { error: 'TradeRequest alloc/init failed' };
  msgSetNsInsid(req, sel('setNsInsid:'), (p.nsInsid ? Number(p.nsInsid) : 0x186eb));
  msg1p(req, sel('setDelegate:'), ptr(0));
  var sc  = p.stockcode || '';
  var mk  = p.mkcode    || '1';
  var qty = String(p.qty   || '0');
  var prc = String(p.price || '0');
  msg2p(req, sel('setRequestParamStr:Value:'), cStr('stockcode'), cStr(sc));
  msg2p(req, sel('setRequestParamStr:Value:'), cStr('zqdm'),      cStr(sc));
  msg2p(req, sel('setRequestParamStr:Value:'), cStr('mkcode'),    cStr(mk));
  msg2p(req, sel('setRequestParamStr:Value:'), cStr('scdm'),      cStr(mk));
  msg2p(req, sel('setRequestParamStr:Value:'), cStr('amount'),    cStr(qty));
  msg2p(req, sel('setRequestParamStr:Value:'), cStr('wtsl'),      cStr(qty));
  msg2p(req, sel('setRequestParamStr:Value:'), cStr('price'),     cStr(prc));
  msg2p(req, sel('setRequestParamStr:Value:'), cStr('wtjg'),      cStr(prc));
  msg2p(req, sel('setRequestParamStr:Value:'), cStr('interact'),  cStr(p.interact === undefined ? '1' : String(p.interact)));
  if (p.gdzh) {
    msg2p(req, sel('setRequestParamStr:Value:'), cStr('gdzh'), cStr(String(p.gdzh)));
  }
  msgSetInt(req, sel('setRequestParamInteger:Value:'), jsStr('req_type'), reqType);
  if (p.extra) {
    for (var k in p.extra)
      msg2p(req, sel('setRequestParamStr:Value:'), cStr(k), cStr(String(p.extra[k])));
  }
  return { ok: true, req: req };
}

// ── buy / sell helper ────────────────────────────────────────────────────────
function _order(reqType, params) {
  try {
    if (!params || !params.stockcode || !params.qty || !params.price)
      return { ok: false, error: 'need stockcode, qty, price' };
    var au = getActiveMoniUser();
    if (!au.ok) return { ok: false, error: au.error };
    var before = sendCounter;
    var b = buildTradeRequest(reqType, {
      stockcode: params.stockcode,
      mkcode:    params.mkcode || '1',
      qty:       params.qty,
      price:     params.price
    });
    if (!b.ok) return { ok: false, error: b.error };
    var d = dispatchTrade(b.req, au.user);
    if (!d.ok) return { ok: false, error: d.error };
    return new Promise(function(resolve) {
      setTimeout(function() {
        resolve({
          ok: true,
          action: reqType === 0x102 ? 'buy' : 'sell',
          stockcode: params.stockcode, qty: params.qty, price: params.price,
          send_detected: sendCounter > before, send_count: sendCounter - before
        });
      }, 350);
    });
  } catch(e) { return { ok: false, error: String(e) }; }
}

// ── 市场/股东账户解析（App 自己维护的 UserInfoModel.marketArray）──────────────
// 反编译依据：/tmp/ths_decomp2/functions/0x100b862d4.c
//   -[UserInfoModel getGdzhBySetCode:] → findMarket: → getOneAccount → 账户号
//   -[AccountModel strAccount] 就是 2106/2105 里那个股东账户（上海 A481535989，深圳 00105815865）
// mkcode：1=深圳Ａ股 2=上海Ａ股 8=沪HK 9=深HK :＝北京Ａ股（App 原始市场码）
function _marketCodeFor(code) {
  var c = String(code || '');
  if (/^(5|6|9)/.test(c)) return '2';   // 沪市（5xxxxx/6xxxxx；9 为沪 B）
  if (/^(0|1|2|3)/.test(c)) return '1'; // 深市
  if (/^(4|8)/.test(c)) return ':';     // 北交所
  return '1';
}

function _accountForMarket(mkcode) {
  var um = msg0p(cls('UserManager'), sel('shareInstance'));
  if (!um || um.isNull()) return null;
  var user = msg0p(um, sel('getActiveUser'));
  if (!user || user.isNull()) return null;
  var info = msg0p(user, sel('getUserInfo'));
  if (!info || info.isNull()) return null;
  var arr = msg0p(info, sel('marketArray'));
  if (!arr || arr.isNull()) return null;
  var n = Number(msg0u(arr, sel('count')));
  for (var i = 0; i < n; i++) {
    var m = msg1p(arr, sel('objectAtIndex:'), ptr(i));
    var mc = nsStr(msg0p(m, sel('strMarketCode')));
    if (mc !== mkcode) continue;
    var accs = msg0p(m, sel('accArray'));
    if (!accs || accs.isNull() || msg0u(accs, sel('count')) === 0) continue;
    var a = msg1p(accs, sel('objectAtIndex:'), ptr(0));
    return nsStr(msg0p(a, sel('strAccount')));
  }
  return null;
}

// ── 浅合并（避免 ES6 对象展开，QuickJS 下更稳）───────────────────────────────
function _objMerge(dst, src) {
  for (var k in src) { if (src.hasOwnProperty(k)) dst[k] = src[k]; }
  return dst;
}

// ── capture (保留兼容) ───────────────────────────────────────────────────────
var captureActive = false;
var captureBuf    = [];
var captureSeq    = 0;
var sendLog       = [];

// ── RPC exports ──────────────────────────────────────────────────────────────
rpc.exports = {

  ping: function () {
    return { ok: true, agent: 'ths_agent_single_hook', ts: Date.now() };
  },

  // ── state：查登录态 ───────────────────────────────────────────────────────
  state: function () {
    try {
      var um = msg0p(cls('UserManager'), sel('shareInstance'));
      if (!um || um.isNull()) return { ok: false, error: 'UserManager nil' };
      var moniArr   = msg0p(um, sel('GetMoniUserArr'));
      var shipanArr = msg0p(um, sel('GetShiPanUserArr'));
      var moniCnt   = (moniArr   && !moniArr.isNull())   ? Number(msg0u(moniArr,   sel('count'))) : 0;
      var shipanCnt = (shipanArr && !shipanArr.isNull())  ? Number(msg0u(shipanArr, sel('count'))) : 0;
      var active    = msg0p(um, sel('getActiveUser'));
      var activePtr = (active && !active.isNull()) ? active.toString() : null;

      var isActiveMoni = false;
      if (activePtr && moniCnt > 0) {
        for (var j = 0; j < moniCnt; j++) {
          if (msg1p(moniArr, sel('objectAtIndex:'), ptr(j)).equals(active)) {
            isActiveMoni = true; break;
          }
        }
      }
      return {
        ok: true, moni_count: moniCnt, shipan_count: shipanCnt,
        active_user_ptr: activePtr, active_is_moni: isActiveMoni,
        send_counter: sendCounter,
        active_is_disconnect: (function() {
          if (!active || active.isNull()) return null;
          try { return msg0i(active, sel('isDisconnect')) !== 0; } catch(e) { return null; }
        })(),
        active_is_user_login: (function() {
          if (!active || active.isNull()) return null;
          try { return msg0i(active, sel('isUserLogin')) !== 0; } catch(e) { return null; }
        })(),
        active_trade_commu_model: (function() {
          if (!active || active.isNull()) return null;
          try {
            var tcm = msg0p(active, sel('tradeCommuModel'));
            return (tcm && !tcm.isNull()) ? tcm.toString() : null;
          } catch(e) { return null; }
        })(),
        commu_send_count: commuSendCount, commu_last_req_type: commuLastReqType, mai_mai_view: (_maiMaiView && !_maiMaiView.isNull()) ? _maiMaiView.toString() : null};
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── getCacheStatus：缓存状态 ──────────────────────────────────────────────
  getCacheStatus: function () {
    var tpl = {};
    for (var _c in _TQT_NIB) {
      tpl[_c] = !!(_tqtOwned[_c] || _tqtInst[_c]);
    }
    return {
      ok: true,
      positions: { count: _cache.positions.rows.length, ts: _cache.positions.ts },
      orders:    { count: _cache.orders.rows.length,    ts: _cache.orders.ts    },
      trades:    { count: _cache.trades.rows.length,    ts: _cache.trades.ts    },
      templates: tpl,
    };
  },

  // ── ensureQueryTemplates：程序化创建三分类 TQT 实例并触发查询 ─────────────
  // 不点任何界面。创建后 didAppear 在主线程自动发 requestByType:BeginDate:EndDate:。
  // params.types: ['positions','orders','trades'] 的子集
  ensureQueryTemplates: function (params) {
    try {
      var types = (params && params.types) ? params.types : ['positions','orders','trades'];
      var created = {}, errors = {}, pointers = {};
      for (var i = 0; i < types.length; i++) {
        var cat = types[i];
        if (!_TQT_NIB[cat]) { errors[cat] = 'unknown category'; continue; }
        var r = _createQueryTemplate(cat);
        if (r.ok) {
          created[cat] = r.created;
          pointers[cat] = r.ptr ? r.ptr.toString() : null;
          if (r.warn) errors[cat] = r.warn;
        } else {
          errors[cat] = r.error;
        }
      }
      var um = cls('UserManager') ? msg0p(cls('UserManager'), sel('shareInstance')) : null;
      var moni = (um && !um.isNull()) ? msg0p(um, sel('GetMoniUserArr')) : null;
      var moniCount = (moni && !moni.isNull()) ? Number(msg0u(moni, sel('count'))) : 0;
      var active = (um && !um.isNull()) ? msg0p(um, sel('getActiveUser')) : null;
      var activeIsMoni = false;
      if (active && !active.isNull() && moni && !moni.isNull()) {
        for (var k = 0; k < moniCount; k++) {
          try {
            if (msg1p(moni, sel('objectAtIndex:'), ptr(k)).equals(active)) { activeIsMoni = true; break; }
          } catch(e) {}
        }
      }
      return {
        ok: Object.keys(errors).length === 0,
        created: created,
        errors: errors,
        pointers: pointers,
        moni_count: moniCount,
        active_is_moni: activeIsMoni,
        note: 'nib→TradeQueryTemplate didAppear（主线程）已触发 requestByType:BeginDate:EndDate:',
      };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── primeCache：noop，兼容旧调用 ─────────────────────────────────────────
  primeCache: function () {
    return {
      ok: true,
      note: 'single-hook mode: use requestQueryDirect to dispatch queries',
      positions: _cache.positions.rows.length,
      orders:    _cache.orders.rows.length,
      trades:    _cache.trades.rows.length,
    };
  },

  // ── requestQueryDirect：在捕获到的 TQT 实例上主动派发查询，轮询等快照 ───
  // ★ 方案B：严格按 category 匹配实例，绝不跨类型派发，没有对应实例返回明确错误
  // params.types: 要查的分类数组，如 ['orders','trades','positions']
  // params.beginDate / params.endDate: NSString 日期，格式 'YYYYMMDD'（可选，默认今天）
  requestQueryDirect: function (params) {
    try {
      var types      = (params && params.types) ? params.types : ['positions','orders','trades'];
      var today      = (function() {
        var d = new Date();
        var y = d.getFullYear();
        var m = ('0'+(d.getMonth()+1)).slice(-2);
        var day = ('0'+d.getDate()).slice(-2);
        return '' + y + m + day;
      })();
      var beginDate  = (params && params.beginDate) ? String(params.beginDate) : today;
      var endDate    = (params && params.endDate)   ? String(params.endDate)   : today;

      // 分类→req_type 映射
      var typeMap = { positions: 1537, orders: 1539, trades: 1538 };

      // 构造 NSString 日期参数（必须是 NSString，不能传 C 字符串）
      var nsBegin = jsStr(beginDate);
      var nsEnd   = jsStr(endDate);

      // requestByType:BeginDate:EndDate: 选择器
      var selReq = sel('requestByType:BeginDate:EndDate:');

      // 记录派发前各分类的时间戳，用于检测是否有新快照
      var snapBefore = {
        positions: _cache.positions.ts,
        orders:    _cache.orders.ts,
        trades:    _cache.trades.ts,
      };

      // ★ 确定性归属：用任意可用的 TQT 实例派发，每次派发前设置 _pendingCat
      // hook 收到快照后按 _pendingCat 登记，空列表也能登记实例，彻底消除死循环
      var anyInst = _tqtInst.positions || _tqtInst.orders || _tqtInst.trades;
      if (!anyInst) {
        return { ok: false, error: 'no_tqt_instance',
                 note: '尚未捕获到任何 TQT 实例，请先通过 UI 触发一次页面加载后再调用' };
      }

      // ★ arm64 上 int 参数必须用 'int' 而非 'int32'
      var reqFn = new NativeFunction(_objc_msgSend,
        'void', ['pointer','pointer','int','pointer','pointer']);

      var selReq  = sel('requestByType:BeginDate:EndDate:');
      var hasReq  = msg1i(anyInst, sel('respondsToSelector:'), selReq) !== 0;
      if (!hasReq) {
        return { ok: false, error: 'requestByType:BeginDate:EndDate: not found on TQT instance' };
      }

      var dispatched = [];
      var skipped    = [];
      for (var i = 0; i < types.length; i++) {
        var cat = types[i];
        var rt  = typeMap[cat];
        if (!rt) continue;
        try {
          // ★ 派发前设 _pendingCat，hook 触发时优先用此分类登记（即使列表为空）
          _pendingCat = cat;
          reqFn(anyInst, selReq, rt, nsBegin, nsEnd);
          dispatched.push(cat);
          // 给 hook 一点时间处理这条派发（尤其是空列表情形）
          Thread.sleep(0.05);
        } catch(de) {
          _pendingCat = null; // 派发失败清空
          skipped.push(cat + ':' + de);
        }
      }

      if (dispatched.length === 0) {
        return { ok: false, error: 'all dispatches failed', types: types };
      }

      // 轮询最多 8 秒，等待 hook 捕获到新的 setTableSourceData: 快照
      var POLL_MS  = 300;
      var MAX_WAIT = 8000;
      var waited   = 0;
      var results  = {};

      while (waited < MAX_WAIT) {
        // 同步等待（Frida JS 是单线程，用 Thread.sleep 阻塞等待）
        Thread.sleep(POLL_MS / 1000);
        waited += POLL_MS;

        var allDone = true;
        for (var j = 0; j < dispatched.length; j++) {
          var c = dispatched[j];
          if (results[c]) continue; // 已拿到
          if (_cache[c].ts > snapBefore[c]) {
            results[c] = {
              ok: true, source: 'dispatch_hook',
              count: _cache[c].rows.length,
              rows:  _cache[c].rows,
              ts:    _cache[c].ts,
              beginDate: beginDate, endDate: endDate,
            };
          } else {
            allDone = false;
          }
        }
        if (allDone) break;
      }

      // 超时未到的分类，退化返回当前缓存（可能是旧数据或空）
      for (var k = 0; k < dispatched.length; k++) {
        var cc = dispatched[k];
        if (!results[cc]) {
          if (_cache[cc].rows.length > 0) {
            results[cc] = {
              ok: true, source: 'cache_fallback',
              count: _cache[cc].rows.length,
              rows:  _cache[cc].rows,
              ts:    _cache[cc].ts,
              note:  'timeout: no new snapshot within 8s, returned existing cache',
              beginDate: beginDate, endDate: endDate,
            };
          } else {
            results[cc] = {
              ok: false, source: 'timeout_empty',
              error: 'no snapshot received within 8s and cache is empty',
              beginDate: beginDate, endDate: endDate,
            };
          }
        }
      }

      return {
        ok: true,
        dispatched: dispatched,
        skipped: skipped,
        beginDate: beginDate, endDate: endDate,
        waited_ms: waited,
        results: results,
      };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── readPositionsDirect：先主动派发查询，退化到缓存 ──────────────────────
  readPositionsDirect: function () {
    var inst = _tqtInst.positions ||
               _tqtInst.orders    ||
               _tqtInst.trades;
    if (inst && !inst.isNull()) {
      // 有实例：主动派发持仓查询
      var r = rpc.exports.requestQueryDirect({ types: ['positions'] });
      if (r && r.results && r.results.positions && r.results.positions.ok) {
        return r.results.positions;
      }
    }
    // 退化：读现有缓存
    if (_cache.positions.rows.length === 0) {
      return { ok: false, error: 'cache_empty_need_prime',
               note: '持仓缓存为空，且尚无 TQT 实例可派发查询' };
    }
    return {
      ok: true, source: 'cache',
      count: _cache.positions.rows.length,
      rows:  _cache.positions.rows,
      ts:    _cache.positions.ts,
    };
  },

  // ── readOrdersDirect：缓存优先；缓存空才派发查询 ─────────────────────────
  readOrdersDirect: function (params) {
    var beginDate = (params && params.beginDate) ? String(params.beginDate) : null;
    var endDate   = (params && params.endDate)   ? String(params.endDate)   : null;
    // ★ 缓存非空 → 直接返回已抓到的真实委托（不重新派发，避免空快照覆盖 9 条真实数据）
    if (_cache.orders.rows.length > 0) {
      return {
        ok: true, source: 'cache',
        count: _cache.orders.rows.length,
        rows:  _cache.orders.rows,
        ts:    _cache.orders.ts,
        instance: _cache.orders.instance,
      };
    }
    // 缓存空 → 只用 orders 专属实例派发一次
    var inst = _tqtInst.orders;
    if (inst && !inst.isNull()) {
      var qp = { types: ['orders'] };
      if (beginDate) { qp.beginDate = beginDate; qp.endDate = endDate || beginDate; }
      var r = rpc.exports.requestQueryDirect(qp);
      if (r && r.results && r.results.orders && r.results.orders.ok) {
        return r.results.orders;
      }
    }
    // 没有委托实例 → 返回现有缓存（有则返回，无则返回明确错误，不崩）
    if (_cache.orders.rows.length > 0) {
      return {
        ok: true, source: 'cache',
        count: _cache.orders.rows.length,
        rows:  _cache.orders.rows,
        ts:    _cache.orders.ts,
        note: '无委托 TQT 实例，返回现有缓存',
      };
    }
    return { ok: false, error: 'need_open_orders_ui',
             note: '尚未捕获到委托 TQT 实例，请先在同花顺 UI 中打开一次「当日委托」页面后再查询' };
  },

  // ── readTradesDirect：缓存优先；缓存空才用成交专属实例派发一次 ─────────────
  readTradesDirect: function (params) {
    var beginDate = (params && params.beginDate) ? String(params.beginDate) : null;
    var endDate   = (params && params.endDate)   ? String(params.endDate)   : null;
    // ★ 缓存非空 → 直接返回已抓到的真实成交（不重新派发，避免空快照覆盖 4 条真实数据）
    if (_cache.trades.rows.length > 0) {
      return {
        ok: true, source: 'cache',
        count: _cache.trades.rows.length,
        rows:  _cache.trades.rows,
        ts:    _cache.trades.ts,
        instance: _cache.trades.instance,
      };
    }
    // 缓存空 → 只用 trades 专属实例派发一次
    var inst = _tqtInst.trades;
    if (inst && !inst.isNull()) {
      var qp2 = { types: ['trades'] };
      if (beginDate) { qp2.beginDate = beginDate; qp2.endDate = endDate || beginDate; }
      var r2 = rpc.exports.requestQueryDirect(qp2);
      if (r2 && r2.results && r2.results.trades && r2.results.trades.ok) {
        return r2.results.trades;
      }
    }
    return { ok: false, error: 'need_open_trades_ui',
             note: '尚未捕获到成交 TQT 实例，请先在同花顺 UI 中打开一次「成交」页后再查询' };
  },

  // ── readAccountDirect：UserManager → activeUser → userInfo → zichanData ──
  // 纯 getter 链，不 hook、不点界面、不发请求；字段全是已格式化的 NSString
  // （两位小数），原样返回，不在这里转数字。
  // ★ -zichanData 内部有 dispatch_semaphore_wait，会短暂阻塞（不崩），可反复调用。
  // ★ 不缓存 UserModel：切账户后是另一个对象，每次重新取。
  readAccountDirect: function () {
    try {
      var umCls = cls('UserManager');
      if (!umCls) return { ok: false, error: 'UserManager class not found' };
      var um = msg0p(umCls, sel('shareInstance'));
      if (!um || um.isNull()) return { ok: false, error: 'UserManager.shareInstance nil' };

      var user = msg0p(um, sel('getActiveUser'));
      if (!user || user.isNull()) return { ok: false, error: 'getActiveUser nil' };
      var isMoni = null;
      try { isMoni = (msg0i(user, sel('isMoniUser')) !== 0); } catch(e) {}

      var info = msg0p(user, sel('getUserInfo'));
      if (!info || info.isNull()) return { ok: false, error: 'getUserInfo nil' };

      var zc = msg0p(info, sel('zichanData'));
      if (!zc || zc.isNull()) return { ok: false, error: 'zichanData nil' };

      var MAP = {
        '总资产': 'strZzc', '总市值': 'strZsz', '总盈亏': 'strZyk', '当日盈亏': 'strDryk',
        '资金余额': 'strZjye', '可取金额': 'strKqje', '可用金额': 'strKyje',
        '资金账号': 'strZjzh', '冻结金额': 'strDjje'
      };
      var out = { ok: true, is_moni: isMoni };
      for (var name in MAP) {
        if (!MAP.hasOwnProperty(name)) continue;
        var v = nsStr(msg0p(zc, sel(MAP[name])));
        out[name] = (v === null) ? '' : v;
      }
      // App 的 strDryk 要等持仓行情推送才算；持仓表每行 2147 就是当日盈亏，
      // 缓存里有行时直接汇总，避免「当日盈亏」恒为空。
      if (!out['当日盈亏']) {
        try {
          var sum = 0, got = false, rows = _cache.positions.rows;
          for (var i = 0; i < rows.length; i++) {
            var raw = rows[i]['2147'];
            if (raw === undefined || raw === null || raw === '') continue;
            var f = parseFloat(String(raw).replace(/,/g, ''));
            if (!isNaN(f)) { sum += f; got = true; }
          }
          if (got) { out['当日盈亏'] = sum.toFixed(2); out['当日盈亏来源'] = '持仓表2147汇总'; }
        } catch (e) {}
      }
      if (!out['总资产']) {
        return { ok: false, error: 'zichanData.strZzc empty',
                 fields: out, note: '资金数据未就绪，稍后重试' };
      }
      return out;
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── readPositions / readOrders / readTrades（别名）───────────────────────
  readPositions: function () { return rpc.exports.readPositionsDirect(); },
  readOrders:    function (p) { return rpc.exports.readOrdersDirect(p); },
  readTrades:    function (p) { return rpc.exports.readTradesDirect(p); },
  read_account_direct: function () { return rpc.exports.readAccountDirect(); },

  // ── sendChedanByIndex：撤单只走 App 自己的方法（唯一正确路径）──────────────
  // 反编译依据：/tmp/ths_decomp2/functions/0x100b74864.c
  //   void -[TradeQueryTemplate sendChedanRequestByTableIndex:](long long rowIndex)
  //   它自己：self.tableSourceData[rowIndex] 取 2102/2135/2106/2108/4008
  //           → alloc TradeRequest → setNsInsid(实例自己的值) → setDelegate(self)
  //           → setRequestParamInteger("req_type", 4)
  //           → 填 stockcode/zqdm/htbh(2135)/gdzh(2106)/mkcode+scdm(2108)/jylx(4008)
  //           → 用当前 active user 派发
  //   所以这里只传行下标，绝不自己拼 req_type/nsInsid 任何字段。
  // params: { rowIndex: number, contract?: string(校验用), probe?: true=只校验不发送 }
  sendChedanByIndex: function (params) {
    try {
      if (!params) return { ok: false, error: 'need params' };
      var expect = (params.contract !== undefined && params.contract !== null)
                   ? String(params.contract) : '';
      if (!expect) return { ok: false, error: 'need contract' };

      // 所有见过「委托」TQT 实例的 tableSourceData 各自独立，撤单必须用
      // 「表里确实有这笔委托」的那个实例；实例可能不止一个，逐个尝试。
      var cands = [];
      for (var i = 0; i < _tqtOrdersList.length; i++) cands.push(_tqtOrdersList[i]);
      if (_tqtInst.orders && !_tqtInst.orders.isNull()) {
        var dup = false;
        for (var j = 0; j < cands.length; j++) if (cands[j].equals(_tqtInst.orders)) { dup = true; break; }
        if (!dup) cands.push(_tqtInst.orders);
      }
      if (cands.length === 0)
        return { ok: false, error: 'no_orders_tqt_instance',
                 note: '尚未捕获到委托 TQT 实例，请先打开一次「当日委托」页面' };

      var matches = [];
      for (var k = 0; k < cands.length; k++) {
        var ins = cands[k];
        var clsOk = false;
        try { clsOk = isKindOfClassName(ins, 'TradeQueryTemplate'); } catch (e) {}
        if (!clsOk) continue;
        var data = msg0p(ins, sel('tableSourceData'));
        if (!data || data.isNull()) continue;
        var cnt = Number(msg0u(data, sel('count')));
        for (var m0 = 0; m0 < cnt; m0++) {
          var rm = dumpNSDict(msg1p(data, sel('objectAtIndex:'), ptr(m0)));
          if (String(rm[2135] === undefined ? '' : rm[2135]) === expect) {
            matches.push({ inst: ins, rowIndex: m0, tableCount: cnt, cls: className(ins), row: rm });
            break;
          }
        }
      }
      if (matches.length === 0)
        return { ok: false, error: 'contract_not_found_in_any_orders_table',
                 contract: expect, instances: cands.length };

      var pick = Number(params.candidate || 0);
      if (pick >= matches.length) pick = 0;
      var hit = matches[pick];
      var row = hit.row, inst = hit.inst, rowIndex = hit.rowIndex;

      var info = {
        row_index:   rowIndex,
        table_count: hit.tableCount,
        candidate:   pick,
        candidates:  matches.map(function(x) { return { cls: x.cls, row_index: x.rowIndex, table_count: x.tableCount }; }),
        contract:    String(row[2135] === undefined ? '' : row[2135]),
        stockcode:   String(row[2102] === undefined ? '' : row[2102]),
        name:        String(row[2103] === undefined ? '' : row[2103]),
        action:      String(row[2109] === undefined ? '' : row[2109]),
        status:      String(row[2105] === undefined ? '' : row[2105]),
        qty:         String(row[2126] === undefined ? '' : row[2126]),
        price:       String(row[2127] === undefined ? '' : row[2127]),
        market:      String(row[2108] === undefined ? '' : row[2108]),
        gdzh:        String(row[2106] === undefined ? '' : row[2106]),
        attr:        String(row[4008] === undefined ? '' : row[4008]),
        instance:    inst.toString(),
        instance_class: hit.cls,
        instances_scanned: cands.length,
      };

      if (params.probe)
        return _objMerge({ ok: true, probe: true, dispatched: false,
                           method: 'sendChedanRequestByTableIndex:' }, info);

      var chedanFn = new NativeFunction(_objc_msgSend, 'void', ['pointer','pointer','int64']);
      chedanFn(inst, sel('sendChedanRequestByTableIndex:'), rowIndex);
      return _objMerge({ ok: true, probe: false, dispatched: true,
                         method: 'sendChedanRequestByTableIndex:' }, info);
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── placeOrder：下单走 App 自己的 TradeRequest 构建 + 自己的派发方法 ────────
  // 反编译依据：
  //   /tmp/ths_decomp2/functions/0x100aedf78.c
  //     -[JYMaiMaiView _sendWeituoRequestWithParams:]   ← 「买入/卖出」确定按钮的最终调用
  //       它用 TradeRequest + setNsInsid(自己的 getInstanceID) + setDelegate(self)
  //       + stockcode/zqdm/mkcode/scdm/amount/wtsl/price/wtjg/gdzh(股东账户)/interact=1
  //       + setRequestParamInteger("req_type", JYWeituoParam.nReqType)
  //       然后对 active user 发 XIXBiQhWtIEfT_DW:（见 0x100acb5c0.c UserModel）
  //   本例完全照抄它的字段集合，只是没有视图实例：nsInsid 用 App 的
  //   HXInstanceIDMan 分配（0x100b3fa24.c HXBaseTradeView::getInstanceID 的兜底路径），
  //   gdzh 从 App 自己的 UserInfoModel.marketArray 取，不手填常量。
  // params: {code, side:'buy'|'sell', price, shares, nsInsid?}
  placeOrder: function (params) {
    try {
      if (!params) return { ok: false, error: 'need params' };
      var code = String(params.code || '').trim();
      var side = String(params.side || '').toLowerCase();
      var price = String(params.price === undefined ? '' : params.price);
      var shares = String(params.shares === undefined ? '' : params.shares);
      if (!code) return { ok: false, error: 'need code' };
      if (side !== 'buy' && side !== 'sell') return { ok: false, error: "side must be buy|sell" };
      if (!/^\d+$/.test(shares) || Number(shares) <= 0) return { ok: false, error: 'bad shares: ' + shares };
      if (!(Number(price) > 0)) return { ok: false, error: 'bad price: ' + price };

      // ★ 优先调 App 自己的 -[JYMaiMaiView sendWeituoRequest]（唯一实测能成交的路径）
      if (params.prefer !== 'request' && _maiMaiView && !_maiMaiView.isNull()
          && isKindOfClassName(_maiMaiView, 'JYMaiMaiView')) {
        var rv = rpc.exports.placeOrderViaView(params);
        if (rv && rv.ok) { rv.via = 'view'; return rv; }
      }

      var au = getActiveMoniUser();
      if (!au.ok) return { ok: false, error: au.error };

      var mkcode = params.mkcode || _marketCodeFor(code);
      var gdzh = params.gdzh || _accountForMarket(mkcode);
      if (!gdzh) return { ok: false, error: 'no_gdzh_for_market_' + mkcode + ' (UserInfoModel.marketArray)' };

      var reqType = (side === 'buy') ? 0x102 : 0x103;
      var insId = 0x186eb;
      if (params.nsInsid !== undefined && params.nsInsid !== null) {
        insId = Number(params.nsInsid);
      } else {
        try {
          var idm = msg0p(cls('HXInstanceIDMan'), sel('shareInstance'));
          if (idm && !idm.isNull()) {
            var fresh = Number(msg0u(idm, sel('getInstanceID')));
            if (fresh > 0) insId = fresh;
          }
        } catch (e) {}
      }

      var before = sendCounter;
      var b = buildTradeRequest(reqType, {
        stockcode: code, mkcode: mkcode, gdzh: gdzh,
        qty: shares, price: price, nsInsid: insId,
        interact: params.interact
      });
      if (!b.ok) return { ok: false, error: b.error };
      var d = dispatchTrade(b.req, au.user);
      if (!d.ok) return { ok: false, error: d.error };
      return new Promise(function(resolve) {
        setTimeout(function() {
          resolve({
            ok: true, side: side, code: code, shares: shares, price: price,
            mkcode: mkcode, gdzh: gdzh, nsInsid: insId, req_type: reqType,
            via: 'request',
            method: 'TradeRequest → -[UserModel XIXBiQhWtIEfT_DW:]',
            send_detected: sendCounter > before, send_count: sendCounter - before
          });
        }, 350);
      });
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── buy / sell（兼容旧名，统一走 placeOrder）───────────────────────────────
  buy:  function (params) {
    var p = params || {};
    return rpc.exports.placeOrder({ code: p.stockcode || p.code, side: 'buy',
                                    price: p.price, shares: p.qty || p.shares,
                                    mkcode: p.mkcode, gdzh: p.gdzh, nsInsid: p.nsInsid });
  },
  sell: function (params) {
    var p = params || {};
    return rpc.exports.placeOrder({ code: p.stockcode || p.code, side: 'sell',
                                    price: p.price, shares: p.qty || p.shares,
                                    mkcode: p.mkcode, gdzh: p.gdzh, nsInsid: p.nsInsid });
  },

  // ── loginSim ──────────────────────────────────────────────────────────────
  loginSim: function () {
    try {
      var um = cls('UserManager');
      if (!um) return { ok: false, error: 'UserManager class not found' };
      var inst = msg0p(um, sel('shareInstance'));
      if (!inst || inst.isNull()) return { ok: false, error: 'UserManager.shareInstance nil' };

      var moniArr = msg0p(inst, sel('GetMoniUserArr'));
      var mc = (moniArr && !moniArr.isNull()) ? Number(msg0u(moniArr, sel('count'))) : 0;

      if (mc === 0) {
        var selZY = sel('loginZiYingFuWu');
        if (msg1i(inst, sel('respondsToSelector:'), selZY) !== 0) {
          new NativeFunction(_objc_msgSend, 'void', ['pointer', 'pointer'])(inst, selZY);
        }
        return { ok: false, need_wait: true,
          error: 'moni_count=0，账户未加载。请先点交易→模拟（或等候 loginZiYingFuWu 完成）后再调用' };
      }

      // setMoNiSelectedActiveUser（无参，直接切到模拟）
      var selMoni = sel('setMoNiSelectedActiveUser');
      if (msg1i(inst, sel('respondsToSelector:'), selMoni) !== 0) {
        new NativeFunction(_objc_msgSend, 'void', ['pointer', 'pointer'])(inst, selMoni);
        var active = msg0p(inst, sel('getActiveUser'));
        var isMoniActive = false;
        var tcm = null;
        if (active && !active.isNull()) {
          for (var i = 0; i < mc; i++) {
            if (msg1p(moniArr, sel('objectAtIndex:'), ptr(i)).equals(active)) {
              isMoniActive = true; break;
            }
          }
          try { var t = msg0p(active, sel('tradeCommuModel')); tcm = (t && !t.isNull()) ? t.toString() : null; } catch(e) {}
        }
        return { ok: true, method: 'setMoNiSelectedActiveUser',
          moni_count: mc, active_is_moni: isMoniActive,
          active_ptr: active ? active.toString() : null,
          active_trade_commu_model: tcm };
      }

      // 方法2：setActiveUser:
      var moniUser = msg1p(moniArr, sel('objectAtIndex:'), ptr(0));
      if (!moniUser || moniUser.isNull())
        return { ok: false, error: 'GetMoniUserArr[0] is nil' };
      msg1p(inst, sel('setActiveUser:'), moniUser);
      var active2 = msg0p(inst, sel('getActiveUser'));
      var isMoniActive2 = (active2 && !active2.isNull()) ? active2.equals(moniUser) : false;
      var tcm2 = null;
      try { var t2 = msg0p(moniUser, sel('tradeCommuModel')); tcm2 = (t2 && !t2.isNull()) ? t2.toString() : null; } catch(e) {}
      return { ok: true, method: 'setActiveUser:',
        moni_count: mc, active_is_moni: isMoniActive2,
        active_ptr: active2 ? active2.toString() : null,
        active_trade_commu_model: tcm2 };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── loginMoni：冷启动直接完成模拟仓登录（锁屏/无窗口可用）─────────────────
  // 反编译入口（/tmp/ths_decomp2）：
  //   -[JYMoniLoginWindowController moniTradeLoginWithAccount:password:] (0x100b61aec)
  //     ← 就是「交易→模拟」面板 OK 按钮走的那条路（同账号分支）
  //   内部：aQuotaUserID → getMoniServerInfoWithSucceedBlock（HTTP 取模拟服务器）
  //        → 造 TradeLoginParam → [UserManager getOneUserModel] → isMoniUser=1
  //        → SetType:4 → [UserModel DJAwpxdGFxRzelWL:] → commumodule 建连登录
  //   之后还要做两件 App 由界面代劳的事：
  //     a) SetType:1 补 A股 位 + setActiveUser:（getActiveUser 的类型掩码只认 1/2，
  //        只置 4 的话 getActiveUser 恒为 nil）
  //     b) getCacheDataObject.requestZijin（资金查询），否则 zichanData 全空
  // params: { account: '176...', password: '<md5(明文)>' }  ← 与 App 记忆值同形态
  loginMoni: function (params) {
    var out = { ok: false, steps: [] };
    try {
      var umCls = cls('UserManager');
      if (!umCls) return { ok: false, error: 'UserManager class not found' };
      var um = msg0p(umCls, sel('shareInstance'));
      if (!um || um.isNull()) return { ok: false, error: 'UserManager.shareInstance nil' };

      function moniCount() {
        var a = msg0p(um, sel('GetMoniUserArr'));
        return (a && !a.isNull()) ? Number(msg0u(a, sel('count'))) : 0;
      }
      function findMoniUser() {
        var arr = msg0p(um, sel('arrayUser'));
        var n = (arr && !arr.isNull()) ? Number(msg0u(arr, sel('count'))) : 0;
        for (var i = 0; i < n; i++) {
          var u = msg1p(arr, sel('objectAtIndex:'), ptr(i));
          if (!u || u.isNull()) continue;
          try { if (msg0i(u, sel('isMoniUser')) !== 0) return u; } catch (e) {}
        }
        return null;
      }

      var mc = moniCount();
      out.moni_count_before = mc;

      // ① 没有模拟用户 → 调面板同款登录入口
      if (mc === 0) {
        var account  = params && params.account;
        var password = params && params.password;
        if (!account || !password) return { ok: false, error: 'need account/password' };

        var ctrlCls = cls('JYMoniLoginWindowController');
        if (!ctrlCls) return { ok: false, error: 'JYMoniLoginWindowController class not found' };
        var ctrl = msg0p(ctrlCls, sel('shareInstance'));
        if (!ctrl || ctrl.isNull())
          return { ok: false, error: 'JYMoniLoginWindowController.shareInstance nil' };
        var selLogin = sel('moniTradeLoginWithAccount:password:');
        if (msg1i(ctrl, sel('respondsToSelector:'), selLogin) === 0)
          return { ok: false, error: 'moniTradeLoginWithAccount:password: not found' };

        var t0 = Date.now();
        msg2p(ctrl, selLogin, jsStr(account), jsStr(password));
        out.steps.push('moniTradeLoginWithAccount:password: dispatched');

        while (Date.now() - t0 < 20000) {
          Thread.sleep(0.3);
          mc = moniCount();
          if (mc > 0) break;
        }
        out.moni_count = mc;
        if (mc === 0) {
          return { ok: false, error: 'moni user 20s 内未落地（登录请求可能被拒/网络未通）',
                   steps: out.steps, elapsed_ms: Date.now() - t0 };
        }
        out.steps.push('moni user in ' + (Date.now() - t0) + 'ms');
      } else {
        out.moni_count = mc;
      }

      // ② 激活：补 A股 位 + 设为 active
      var user = findMoniUser();
      if (!user) return { ok: false, error: 'arrayUser 里找不到 isMoniUser=1 的 UserModel',
                          steps: out.steps, moni_count: mc };
      try { msg1int(user, sel('SetType:'), 1); } catch (e) {}
      msg1p(um, sel('setActiveUser:'), user);
      var active = msg0p(um, sel('getActiveUser'));
      out.active_ptr = (active && !active.isNull()) ? active.toString() : null;
      out.active_is_moni = !!(active && !active.isNull() && active.equals(user));
      if (!out.active_is_moni) {
        return { ok: false, error: 'setActiveUser: 后 getActiveUser 仍不是模拟用户',
                 steps: out.steps, active_ptr: out.active_ptr };
      }
      out.steps.push('active ok');

      // ③ 拉资金（App 平常由资金面板代劳）：zichanData 全空时靠 requestZijin 回填
      var cd = msg0p(user, sel('getCacheDataObject'));
      var zzc = null;
      for (var round = 0; round < 3; round++) {
        try { msg0p(cd, sel('requestZijin')); } catch (e) {}
        var t1 = Date.now();
        while (Date.now() - t1 < 6000) {
          Thread.sleep(0.3);
          var info = msg0p(user, sel('getUserInfo'));
          var zc = (info && !info.isNull()) ? msg0p(info, sel('zichanData')) : null;
          zzc = (zc && !zc.isNull()) ? nsStr(msg0p(zc, sel('strZzc'))) : null;
          if (zzc) break;
        }
        if (zzc) break;
      }
      // 持仓查询让 App 把「总盈亏」按持仓重算一遍（否则刚登录时可能还是 0）
      if (zzc) {
        try { msg0p(cd, sel('requestChicang')); } catch (e) {}
        Thread.sleep(1.5);
        try {
          var info2 = msg0p(user, sel('getUserInfo'));
          var zc2 = (info2 && !info2.isNull()) ? msg0p(info2, sel('zichanData')) : null;
          if (zc2 && !zc2.isNull()) {
            out.zichan_zyk = nsStr(msg0p(zc2, sel('strZyk'))) || '';
          }
        } catch (e) {}
      }
      out.zichan_ready = !!zzc;
      out.zichan_zzc = zzc || '';
      if (zzc) out.steps.push('zichan ready');

      try { out.moni_count = moniCount(); } catch (e) {}
      try { out.active_is_disconnect = (msg0i(active, sel('isDisconnect')) !== 0); } catch (e) {}
      try { out.active_is_user_login = (msg0i(active, sel('isUserLogin')) !== 0); } catch (e) {}
      out.ok = true;
      return out;
    } catch (e) { return { ok: false, error: String(e), steps: out.steps }; }
  },

  // ── login（HXConnection 路径）────────────────────────────────────────────
  login: function (params) {
    try {
      var account   = (params && params.account)  ? params.account  : '';
      var password  = (params && params.password) ? params.password : '';
      var loginType = (params && params.loginType !== undefined) ? params.loginType : 0;
      if (!account || !password)
        return { ok: false, error: 'need account and password' };

      var hxc = cls('HXConnection');
      if (!hxc) return { ok: false, error: 'HXConnection class not found' };
      var authMgr = msg0p(hxc, sel('authenticationManager'));
      if (!authMgr || authMgr.isNull())
        return { ok: false, error: 'HXConnection.authenticationManager nil' };

      var selAuth = sel('authorizeWithUserName:password:loginType:loginFlag:success:failure:cancel:');
      if (msg1i(authMgr, sel('respondsToSelector:'), selAuth) === 0)
        return { ok: false, error: 'authorizeWithUserName:... not found on authMgr' };

      var nsAcct = jsStr(account);
      var nsPass = jsStr(password);
      var sendBefore = sendCounter;

      var authFn = new NativeFunction(_objc_msgSend,
        'void', ['pointer','pointer','pointer','pointer','int','int','pointer','pointer','pointer']);
      authFn(authMgr, selAuth, nsAcct, nsPass, loginType, 0, ptr(0), ptr(0), ptr(0));

      return { ok: true, dispatched: true,
               method: 'authorizeWithUserName:password:loginType:loginFlag:success:failure:cancel:',
               loginType: loginType, loginFlag: 0,
               authMgr: authMgr.toString(),
               send_before: sendBefore, send_after: sendCounter };
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── capture / sendLog（兼容旧调用） ───────────────────────────────────────
  captureStart: function () {
    captureActive = true; captureBuf = []; captureSeq = 0;
    return { ok: true, hooks: 1, note: 'single-hook mode — setTableSourceData: hook active' };
  },
  captureStop:  function () { captureActive = false; return { ok: true, entries: captureBuf.length }; },
  captureDump:  function () { return { ok: true, active: captureActive, count: captureBuf.length, entries: captureBuf.slice() }; },
  sendLog:      function () { return { ok: true, count: sendLog.length, entries: sendLog.slice() }; },

  // ── placeOrderViaView：直接调 App 自己的 -[JYMaiMaiView sendWeituoRequest] ──
  // 这是「买入/卖出」确定按钮 onTrade: 之后真正下单的方法：它自己读 mmParam
  // （strCode/strPrice/strAmount/strSetCode/strGdzh/nReqType），自己 new TradeRequest、
  // 自己 setNsInsid(getInstanceID)、setDelegate(self)，然后发给 active user。
  // 我们只负责把 mmParam 字段填成要下的单，协议字段一个都不碰。
  placeOrderViaView: function (params) {
    try {
      var p = params || {};
      var inst = _maiMaiView;
      if (!inst || inst.isNull())
        return { ok: false, error: 'no_mai_mai_view', note: '交易面板未创建过，先打开一次交易页' };
      var cn = className(inst);
      if (!isKindOfClassName(inst, 'JYMaiMaiView'))
        return { ok: false, error: 'not_maimai_view', instance_class: cn };

      var code = String(p.code || '').trim();
      var side = String(p.side || '').toLowerCase();
      var price = String(p.price === undefined ? '' : p.price);
      var shares = String(p.shares === undefined ? '' : p.shares);
      if (side !== 'buy' && side !== 'sell') return { ok: false, error: 'side must be buy|sell' };
      if (!/^\d+$/.test(shares) || Number(shares) <= 0) return { ok: false, error: 'bad shares' };
      if (!(Number(price) > 0)) return { ok: false, error: 'bad price' };

      var mm = msg0p(inst, sel('mmParam'));
      if (!mm || mm.isNull()) return { ok: false, error: 'mmParam nil' };

      var setcode = p.setcode || _marketCodeFor(code);
      var gdzh = p.gdzh || _accountForMarket(setcode);
      if (!gdzh) return { ok: false, error: 'no_gdzh_for_setcode_' + setcode };
      var reqType = (side === 'buy') ? 0x102 : 0x103;

      msg1p(mm, sel('setStrCode:'),    jsStr(code));
      msg1p(mm, sel('setStrPrice:'),   jsStr(price));
      msg1p(mm, sel('setStrAmount:'),  jsStr(shares));
      msg1p(mm, sel('setStrSetCode:'), jsStr(String(setcode)));
      msg1p(mm, sel('setStrGdzh:'),    jsStr(gdzh));
      msg1int(mm, sel('setNReqType:'), reqType);

      var commuBefore = commuSendCount;
      msg0p(inst, sel('sendWeituoRequest'));

      return {
        ok: true, via: 'JYMaiMaiView::sendWeituoRequest',
        side: side, code: code, shares: shares, price: price,
        setcode: String(setcode), gdzh: gdzh, req_type: reqType,
        view: inst.toString(),
        commu_send_detected: commuSendCount > commuBefore,
        commu_send_count: commuSendCount - commuBefore,
        commu_last_req_type: commuLastReqType
      };
    } catch (e) { return { ok: false, error: String(e) }; }
  },

  // ── 兼容旧 RPC 名 ─────────────────────────────────────────────────────────
  fetchPositions:   function ()  { return rpc.exports.readPositionsDirect(); },
  fetchOrders:      function (p) { return rpc.exports.readOrdersDirect(p); },
  read_positions:   function ()  { return rpc.exports.readPositionsDirect(); },
  read_orders:      function (p) { return rpc.exports.readOrdersDirect(p); },
  read_trades:      function (p) { return rpc.exports.readTradesDirect(p); },
  ensureReady:      function ()  { return { ok: true }; },
  scanInstances:    function ()  {
    return {
      ok: true, source: 'single_hook_cache',
      note: 'memory scan disabled in single-hook mode',
      cache_status: {
        positions: _cache.positions.rows.length,
        orders:    _cache.orders.rows.length,
        trades:    _cache.trades.rows.length,
      }
    };
  },
};
