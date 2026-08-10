# EvolvingSim 不稳定根因诊断报告

> 生成的: 2026-08-10 | 排查范围: trade.py + evolving.py + ascmds.py + helper.py

---

## 1. 架构总览

```
trade.py (封装层)                EvolvingSim (核心)            ascmds.py (AppleScript)
─────────────────               ─────────────────             ──────────────────────
_call_evolving(method, args)    EvolvingSim.__init__()        asgetHoldingSharesSim
 └─ subprocess.Popen             ├─ Lock() → /tmp/lock.json   asgetAccountInfoSim
    python -c "...                 ├─ helper.Config()           asissuingEntrustSim
    e = EvolvingSim()             └─ Logging()                 asrevokeEntrustSim
    e.buy(code, sh, pr)"
   timeout=8s
   → 超时杀进程+clean lock+杀僵尸
```

**关键发现**: `trade.py` 每次通过 **`subprocess.Popen`** 在独立子进程中调用 EvolvingSim。每个子进程新建 `Lock()` 实例竞争 `/tmp/lock.json`。

---

## 2. Lock 机制缺陷 —— 核心不稳定性来源

### 2.1 Lock 是文件锁而非进程锁

```python
# evolving.py: Lock class (line 8-53)
class Lock(object):
    _lockFilePath = '/tmp/lock.json'

    def requestLock(self):
        islock = islocked()
        tolerance = 15    # 15 sec
        elapsedTime = 0
        delta = 0.05      # 每 50ms 轮询
        while islock and elapsedTime < tolerance:
            time.sleep(delta)
            elapsedTime += delta
            islock = islocked()
        if islock:
            return False
        self.lock()
        return True
```

**问题清单**:

1. **TOCTOU 竞态条件**: `islocked()` 检查和 `lock()` 写入之间没有原子性保证。两个子进程可以同时读到 `lock=0`，然后都写入 `lock=1`，都认为获取锁成功。这是文件锁的经典 race condition。

2. **15 秒超时过短 + 无意义**: 如果前一个进程的 AppleScript 卡住（os.popen 同步阻塞超过 15s），下一个进程最多等 15 秒后直接放弃。但如果前一个进程的 osascript 还在执行（未释放锁），新进程拿到锁后会同时驱动同花顺 UI，产生严重冲突。

3. **unlock 只在成功路径执行**: `evolving.py` 中所有方法的 `self.__lock.unlock()` 在 `try/except` 之后。但如果 `os.popen(cmd).read()` 阻塞超过 timeout，Python 层面不会抛异常——`os.popen` 的 `.read()` 是**同步阻塞**的，不会自行超时。这意味着如果同花顺无响应，锁永远不会被释放。

4. **`Login` 和 `logout` 的锁语义反了**:
```python
# Service.loginClient() → 成功后 self.__lock.unlock()  (line 96)
# Service.logoutClient() → 成功后 self.__lock.lock()   (line 115)
```
登录后 unlock、登出后 lock——这个设计假设登录期间其他人不能用，但逻辑反直觉且不可靠。

### 2.2 锁释放路径分析

| 场景 | 锁会释放? | 说明 |
|------|----------|------|
| AppleScript 正常返回 | ✅ | `self.__lock.unlock()` 执行 |
| AppleScript 卡住 (<8s) | ❌ | `os.popen.read()` 同步阻塞，不会到 unlock |
| AppleScript 卡住 (>8s) | ⚠️ | trade.py 的 subprocess.TimeoutExpired → `proc.kill()` + `_clean_lock()` |
| 进程被 kill -9 | ❌ | 锁文件残留 |
| Python 异常 (如 JSON 解析) | ✅ | `except` 后仍有 `self.__lock.unlock()` |

**根本矛盾**: EvolvingSim 内部用 `os.popen` 同步阻塞执行 AppleScript，而 `trade.py` 用 `subprocess.Popen` + 8s 超时来"兜底"。但超时后只是清理锁文件，**不同步清理 EvolvingSim 内部可能仍在运行的 osascript 进程**。虽然 `trade.py` 调用了 `_kill_osascript_zombies()`，但由于子进程是 `proc.kill()` 而非优雅终止，EvolvingSim 内部启动的 osascript 可能成为孤儿进程继续操作同花顺 UI。

---

## 3. AppleScript 命令对比分析

### 3.1 `getHoldingShares` vs `buy/sell (issuingEntrust)` 的 AppleScript 差异

| 维度 | `getHoldingShares` (同步持仓) | `buy/sell` (下单委托) |
|------|------------------------------|----------------------|
| 入口 AppScript | `asgetHoldingSharesSim` | `asissuingEntrustSim` |
| 初始 delay | `delay 0.5` | `delay 0.4` |
| 导航步骤 | button 1 → button 6 → "模拟" → "股票" → "持仓" | button 1 → button 6 → "模拟" → "股票" → "持仓"→"委托"→checkbox→读委托列表→输代码→输价格→输数量→"确定买入/卖出"→确认弹窗→"确认"→再读委托列表→diff |
| UI 操作数 | ~6 步 | ~35+ 步 |
| 读取数据 | 只读 scroll area 4/5 的 static text | 读+写+点击+弹窗确认+二次读取 |
| AppleScript 预估耗时 | ~1-2s | ~3-8s+ |
| 是否修改 UI 状态 | 否（纯读） | 是（输入、点击、弹窗） |
| 失败模式 | 窗口不在前台、网络延迟 | **所有 sync 的问题 + 弹窗处理失败 + 价格输入错误 + 确认超时** |

**为什么 sync (getHoldingShares) 能成功但 buy/sell 不行**:
1. getHoldingShares 是纯读取操作，只访问 Accessibility 树，不修改 UI 状态
2. buy/sell 涉及大量 UI 交互——输入框焦点、按钮点击、弹窗等待、二次确认，每个环节都可能因同花顺 UI 状态不一致而失败
3. buy/sell 中的 `delay 0.25` 等硬编码等待在慢速机器/网络下可能不足
4. 下单脚本中 "sometimes in area 4 sometimes in area 5" 的 try/catch 模式本身就是不稳定设计——如果同花顺更新 UI 布局，area 号会变

### 3.2 getAccountInfo 对比

`asgetAccountInfoSim`：`click button 6 → "A股" → "模拟" → delay 0.6 → 读 static text`
简单直接，纯读取，跟 getHoldingShares 类似——所以也能成功。

### 3.3 所有 AppleScript 的共同弱点

| 弱点 | 影响 | 代码证据 |
|------|------|---------|
| `tell app "同花顺" to activate` + `delay 0.4-0.5` | 硬编码等待，窗口切换慢则失败 | ascmds.py 几乎每个脚本 |
| `button N of window 1` (数字索引) | 同花顺版本升级后按钮位置变化则全部失效 | button 1, button 6 等 |
| `"sometimes in area 4 sometimes in area 5"` | 表结构漂移 | scroll area 4/5 的 try/catch |
| 无超时机制 | `os.popen.read()` 永远阻塞 | evolving.py 直接 os.popen |
| 无错误恢复 | 中间步骤失败后没有回滚/重试逻辑 | 整体 try/catch 后直接 return failed |
| 依赖 `cliclick` 工具 | 额外依赖，坐标点击易受分辨率影响 | `do shell script "/usr/local/bin/cliclick"` |

---

## 4. 同花顺 Mac 版 AppleScript 响应条件

基于 AppleScript 代码分析，同花顺必须满足以下条件才能响应命令：

### 4.1 必要条件
1. **进程存在**: `process "同花顺"` 必须正在运行
2. **主窗口可见**: `window 1` 必须存在且不在最小化状态
3. **登录完成**: 用户在 "模拟交易" 面板已完成登录（所有脚本都先到交易面板）
4. **Accessibility 权限**: macOS 必须授予辅助功能权限给 osascript/Terminal

### 4.2 不稳定原因
1. **同花顺 Mac 版本身不稳定**: macOS 版同花顺是基于 Windows 版的移植（可能是 Electron/Wine 封装），其 Accessibility 树结构可能随数据加载、网络延迟动态变化
2. **窗口焦点竞争**: 多个 osascript 进程同时 `tell app "同花顺" to activate` 会导致焦点抖动
3. **延迟依赖**: AppleScript 中的 `delay 0.4-0.5` 在高负载或网络差时不够
4. **同花顺弹窗**: 行情推送、新闻弹窗、系统通知可能遮挡交易窗口，导致 Accessibility 元素不可见

### 4.3 需要的前置状态（模拟交易）
所有 Sim 后缀的脚本都需要同花顺处在 "**模拟交易 → A股 → 股票**" 页面：
```
同花顺主窗口
 ├─ 自选 (button 1)
 ├─ ...
 ├─ 交易 (button 6)  ← 必须已点击
     ├─ A股 (button)  ← 必须已选中
     ├─ 模拟 (button) ← 必须已选中（Sim 系列）
     └─ 股票 (button) ← 必须已选中
         ├─ 持仓
         ├─ 委托
         └─ ...
```

---

## 5. trade.py 层面的问题

### 5.1 `_call_evolving` 的设计缺陷

```python
# trade.py: line 88-170 (简化)
def _call_evolving(method_name, *args, timeout=8):
    code = (
        f"import sys, json; "
        f"sys.path.insert(0, {SCRIPT_DIR!r}); "
        f"from evolving.evolving import EvolvingSim; "
        f"e = EvolvingSim(); "
        f"result = e.{method_name}({args_repr}); "
        f"print(json.dumps(result, ...))"
    )
    proc = subprocess.Popen([sys.executable, '-c', code], ...)
    stdout, stderr = proc.communicate(timeout=timeout)
```

**问题**:

1. **每次调用新建子进程**: sync 操作会连续调用 `getHoldingShares`(stock) → `getHoldingShares`(sciTech) → `getHoldingShares`(gem) → `getAccountInfo`，共 **4 个子进程**，每个都新建 EvolvingSim 实例、竞争同一个 Lock。如果 buy/sell 操作也加上前后持仓确认（`_trade_with_confirmation`），则再多 2-3 次 getHoldingShares 调用。

2. **8 秒超时不足**: `buy/sell` 的 AppleScript (`issuingEntrustSim`) 非常长，包含：导航→查委托→输入代码→输入价格(可能读卖5/买5)→输入数量→点击确认→等待弹窗→点击二次确认→再查委托→diff合同号。正常网络下需 3-5 秒，网络慢或同花顺响应慢时很容易超过 8 秒。

3. **`os.system("osascript -e 'tell app 同花顺 to activate'")` 可能卡死**: `_activate_tonghuashun()` 使用 `os.system` 同步阻塞。如果同花顺正在加载/崩溃恢复，`osascript activate` 本身就会卡住。用户报告的 "`osascript activate` 都卡死" 正是这个路径。

### 5.2 `_retry_get_holding_shares` 的问题

```python
def _retry_get_holding_shares(max_retries=3, delay=2):
    for attempt in range(1, max_retries + 1):
        _activate_tonghuashun()   # ← 可能卡住
        _kill_osascript_zombies() # ← pkill -9 osascript
        _clean_lock()
        h = _call_evolving('getHoldingShares')
```

每次重试都 `pkill -9 -f osascript`，这会**无条件杀死所有 osascript 进程**，包括正在执行的其他 EvolvingSim 调用。如果 sync 和 buy 并发执行（如 cron 同时触发），sync 的重试会杀掉 buy 的 osascript。

### 5.3 `_trade_with_confirmation` 的持仓确认窗口

```python
# trade.py
def _trade_with_confirmation(action, code, shares, price):
    before = _get_holding_shares().get(code, 0)   # 调用 1
    result = _call_evolving(action, code, shares, price)  # 调用 2
    time.sleep(1.5)
    after = _get_holding_shares().get(code, 0)    # 调用 3
    if delta == expected:
        sync()  # 调用 4+5+6+7 (getHoldingShares x3 + getAccountInfo)
```

一个 buy 操作最少触发 **7 次** EvolvingSim 子进程调用。每次都可能因 Lock 竞争、AppleScript 阻塞、同花顺 UI 延迟而失败。

---

## 6. 根因排序（影响从大到小）

### 🔴 严重 (Crash/Dataloss 级别)

| # | 根因 | 症状 |
|---|------|------|
| 1 | **os.popen 同步阻塞无超时** | AppleScript 卡住 → EvolvingSim 进程永久挂起 → Lock 永不释放 → 后续调用全部 15s 超时失败 |
| 2 | **Lock 无原子性 (TOCTOU)** | 两个进程同时获锁 → 同时操作同花顺 UI → 竞态崩溃/状态混乱 |
| 3 | **pkill -9 osascript 无差别杀进程** | sync 重试杀掉正在执行的 buy → buy 失败但锁文件已被 sync 清理 → 状态不一致 |

### 🟡 高影响 (Reliability)

| # | 根因 | 症状 |
|---|------|------|
| 4 | **8 秒超时对 buy/sell 太紧** | 正常下单偶尔超时，网络慢/同花顺慢时频繁超时 |
| 5 | **AppleScript 硬编码 delay + button index + area number** | 同花顺版本升级/分辨率变化/系统负载变化 → 全盘失效 |
| 6 | **每次操作新建进程+实例** | 无状态复用，锁竞争放大，启动开销累加 |
| 7 | **_activate_tonghuashun 用 os.system 同步阻塞** | 同花顺无响应时激活命令本身就卡死 |

### 🟢 中影响 (Design)

| # | 根因 | 症状 |
|---|------|------|
| 8 | **trade.py 的 retry 逻辑用 `_get_holding_shares()` 而非 `_call_evolving('getHoldingShares')`** | retry 时重新激活窗口+杀僵尸，可能打断自己的前一次调用 |
| 9 | **Lock 的 login/logout 语义反了** | 登出后锁住，登录后解锁——状态管理混乱 |
| 10 | **`_trade_with_confirmation` 失败后仍调 sync()** | 下单失败但持仓被重读，可能覆盖正确数据 |

---

## 7. 为什么"有时能成功有时不能"

根据对代码的全面分析，"间歇性成功/失败"的根因是：

1. **同花顺 Mac 版的 Accessibility 树不稳定**: 同花顺是 Windows 程序的 Mac 移植，其 UI 不是原生 Cocoa。Accessibility 元素（window 1 的位置、button index、scroll area 编号）在数据加载、网络延迟、行情刷新时会变化。

2. **竞态条件**: 多个 cron 任务（如 30 分钟同步 + 按需 buy/sell）同时触发时，多个 EvolvingSim 子进程竞争 Lock 和同花顺焦点。

3. **Mac 系统资源**: 内存压力、CPU 节流、系统更新后 Accessibility 权限重置都会影响 AppleScript 稳定性。

4. **"短暂恢复后几分钟又断"**: 这符合 "孤儿 osascript 进程" 的场景——某个操作失败后 osascript 没被清理，持续占用同花顺的 Accessibility 通道，后续操作全部排队等待直到该进程被手动杀掉或超时。

---

## 8. 改善建议（不修改代码，仅建议方向）

1. **最短路径修复**: 将 AppleScript 中的 `osascript` 调用加上 `with timeout of 10 seconds` 块，让 AppleScript 自己超时退出
2. **中优先**: 把 `os.popen` 改为 `subprocess.run` 并设置 shell-level timeout
3. **高优先但工作量大**: 用文件锁 (`fcntl.flock`) 替代 JSON 文件锁，保证原子性
4. **结构性改进**: 用单个守护进程（长生命周期的 EvolvingSim 实例）替代每次新建子进程
5. **监控**: 添加 osascript 僵尸进程检测脚本，在 cron 中定期 `pkill -f "osascript"` 清理

---

## 9. 文件清单

| 文件 | 行数 | 角色 |
|------|------|------|
| `trade.py` | 742 | 封装层：子进程超时保护、重试、锁清理 |
| `evolving/evolving.py` | 2017 | 核心：Lock/Evolving/EvolvingSim 类 |
| `evolving/ascmds.py` | 2527 | AppleScript 字符串常量（全部 UI 操作） |
| `evolving/helper.py` | 233 | 配置加载（XML）、日志、邮件 |
| `evolving/__init__.py` | ? | 包初始化 |