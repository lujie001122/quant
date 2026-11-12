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
  msgSetNsInsid(req, sel('setNsInsid:'), 0x186eb);
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
  msg2p(req, sel('setRequestParamStr:Value:'), cStr('interact'),  cStr('1'));
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

  // ── readPositions / readOrders / readTrades（别名）───────────────────────
  readPositions: function () { return rpc.exports.readPositionsDirect(); },
  readOrders:    function (p) { return rpc.exports.readOrdersDirect(p); },
  readTrades:    function (p) { return rpc.exports.readTradesDirect(p); },

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
      if (!params) return { ok: false, error: 'need params.rowIndex' };
      var rowIndex = Number(params.rowIndex);
      if (!isFinite(rowIndex) || rowIndex < 0 || Math.floor(rowIndex) !== rowIndex)
        return { ok: false, error: 'bad rowIndex: ' + params.rowIndex };

      var inst = _tqtInst.orders;
      if (!inst || inst.isNull())
        return { ok: false, error: 'no_orders_tqt_instance',
                 note: '尚未捕获到委托 TQT 实例，请先打开一次「当日委托」页面' };

      // 指针健壮性：类名必须是 TradeQueryTemplate，否则宁可不发
      var clsName = '';
      try {
        var c = _object_getClass(inst);
        if (!c || c.isNull()) return { ok: false, error: 'orders_instance_class_nil' };
        var nm = _class_getName(c);
        clsName = (nm && !nm.isNull()) ? String(nm.readUtf8String()) : '';
      } catch(ec) { return { ok: false, error: 'orders_instance_ptr_unusable: ' + ec }; }
      if (clsName !== 'TradeQueryTemplate')
        return { ok: false, error: 'orders_instance_not_tradequerytemplate',
                 instance_class: clsName, instance: inst.toString() };

      var selChedan = sel('sendChedanRequestByTableIndex:');
      if (msg1i(inst, sel('respondsToSelector:'), selChedan) === 0)
        return { ok: false, error: 'sendChedanRequestByTableIndex: not found on instance' };

      // 读实例自己的 tableSourceData（App 内部用的就是这份），校验下标与合同号
      var data = msg0p(inst, sel('tableSourceData'));
      if (!data || data.isNull())
        return { ok: false, error: 'orders_instance_tableSourceData_nil' };
      var n = Number(msg0u(data, sel('count')));
      if (rowIndex >= n)
        return { ok: false, error: 'row_index_out_of_range',
                 rowIndex: rowIndex, table_count: n };

      var row = dumpNSDict(msg1p(data, sel('objectAtIndex:'), ptr(rowIndex)));
      var rowContract = (row[2135] !== undefined) ? String(row[2135]) : '';
      var expect = (params.contract !== undefined && params.contract !== null)
                   ? String(params.contract) : '';
      if (expect !== '' && rowContract !== expect)
        return { ok: false, error: 'contract_mismatch_at_index',
                 rowIndex: rowIndex, expect_contract: expect, row_contract: rowContract };

      var info = {
        row_index:   rowIndex,
        table_count: n,
        contract:    rowContract,
        stockcode:   (row[2102] !== undefined) ? String(row[2102]) : '',
        name:        (row[2103] !== undefined) ? String(row[2103]) : '',
        action:      (row[2109] !== undefined) ? String(row[2109]) : '',
        status:      (row[2105] !== undefined) ? String(row[2105]) : '',
        qty:         (row[2126] !== undefined) ? String(row[2126]) : '',
        price:       (row[2127] !== undefined) ? String(row[2127]) : '',
        market:      (row[2108] !== undefined) ? String(row[2108]) : '',
        gdzh:        (row[2106] !== undefined) ? String(row[2106]) : '',
        attr:        (row[4008] !== undefined) ? String(row[4008]) : '',
        instance:    inst.toString(),
      };

      // probe：只校验到「可以调用」为止，不发请求
      if (params.probe)
        return _objMerge({ ok: true, probe: true, dispatched: false,
                           method: 'sendChedanRequestByTableIndex:' }, info);

      var tsBefore  = _cache.orders.ts;
      var chedanFn  = new NativeFunction(_objc_msgSend, 'void', ['pointer','pointer','int64']);
      chedanFn(inst, selChedan, rowIndex);

      return new Promise(function(resolve) {
        setTimeout(function() {
          resolve(_objMerge({
            ok: true, probe: false, dispatched: true,
            method: 'sendChedanRequestByTableIndex:',
            orders_cache_ts_before: tsBefore,
            orders_cache_ts_after:  _cache.orders.ts,
            orders_cache_refreshed: _cache.orders.ts > tsBefore,
          }, info));
        }, 600);
      });
    } catch(e) { return { ok: false, error: String(e) }; }
  },

  // ── buy / sell ────────────────────────────────────────────────────────────
  buy:  function (params) { return _order(0x102, params); },
  sell: function (params) { return _order(0x103, params); },

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
