# 同花顺下单替代方案调研报告

> 调研日期: 2026-08-10
> 当前系统: EvolvingSim (AppleScript) → 同花顺 Mac 模拟交易

---

## 一、现有系统架构与问题总结

### 当前架构

```
signal_generator.py → trade.py → EvolvingSim → os.popen("osascript ...") → 同花顺 Mac 客户端
```

### 核心问题

| 问题 | 根因 | 当前缓解措施 |
|------|------|------------|
| 窗口可访问性不稳定 | 同花顺 Mac 版自绘 UI，AppleScript `count windows` 返回 0 | `osascript activate` 激活窗口 |
| 锁文件僵尸进程 | `subprocess.run(timeout=N)` 超时不杀子进程，osascript 僵尸持锁 | trade.py: Popen + kill + clean lock |
| osascript activate 卡死 | 同花顺窗口焦点切换时 AppleScript 同步阻塞 | 8s 超时 + kill zombie |
| 14:30 后模拟交易停止 | 同花顺模拟交易服务器限制 | 无解 |

### 本质问题

**同花顺 Mac 版不是为自动化设计的**。AppleScript 字典是残废的（只有 `activate`/`quit`/`open`），所有操作依赖 `System Events` 的 UI 脚本，而 Mac 版同花顺用的是自绘 UI（非原生 Cocoa），导致 `System Events` 对窗口/按钮的检测不可靠。

---

## 二、方案对比

### 方案 1: PyAutoGUI 键盘模拟

**原理**: 绕过 AppleScript，直接通过键盘快捷键模拟操作同花顺窗口。

**实现方式**:
```python
import pyautogui
# 1. 激活同花顺窗口 (Cmd+Tab 或 AppleScript activate)
# 2. 键盘快捷键定位到交易面板
# 3. 输入代码 → Tab → 输入价格 → Tab → 输入数量 → Enter
# 4. 截图做 OCR 确认
```

**可行性分析**:

| 维度 | 评估 |
|------|------|
| 同花顺键盘快捷键 | ✅ 同花顺 Mac 支持 F1买入/F2卖出/F3撤单 等快捷键 |
| 输入可靠性 | ✅ 键盘模拟比 AppleScript UI 脚本更底层，不依赖 Accessibility 树 |
| 窗口焦点 | ⚠️ 仍需同花顺在前台，锁屏/切窗口会中断 |
| 回执确认 | ⚠️ 需要 OCR 截图确认下单结果，无法直接读返回值 |
| macOS 权限 | ⚠️ 需要辅助功能权限 + 屏幕录制权限（macOS 每次弹窗确认） |

**优点**:
- 不依赖 AppleScript 的 `System Events`，避免窗口检测问题
- 同花顺快捷键支持完整（F1买入/F2卖出/F3撤单/F4持仓/F5委托/F6成交）
- 输入比点击按钮更可靠（不受 UI 布局变化影响）
- 可以配合 `pyautogui.locateOnScreen()` 做图像识别确认

**缺点**:
- 无法直接读取持仓/委托/成交数据（需要 OCR 或截图+图像识别）
- 需要同花顺保持在前台，锁屏/切窗口时失效
- macOS 权限弹窗问题（屏幕录制权限每次重启需要确认）
- 回执确认依赖 OCR，精度不如 API 返回值
- 14:30 模拟交易停止问题不变

**实现难度**: ⭐⭐⭐ (中等)
**稳定性预期**: ⭐⭐⭐⭐ (较好，比 AppleScript UI 脚本稳定)
**推荐指数**: ⭐⭐⭐ (作为过渡方案)

---

### 方案 2: 同花顺网页版 API

**原理**: 同花顺有网页版交易（https://trade.10jqka.com.cn），通过 HTTP 抓包或 API 直接下单。

**调研结论**: ❌ **不可行**

**原因**:
1. **同花顺网页版无公开 API**：网页版交易是闭源的，没有开放的 REST API
2. **反爬严格**：同花顺网页版有验证码、签名校验、Cookie 加密等多层反爬
3. **模拟交易无网页版**：模拟交易只在客户端内，网页版是实盘交易
4. **法律风险**：逆向工程抓包下单接口可能违反同花顺用户协议
5. **社区无成熟方案**：GitHub 上无可靠的同花顺网页版交易 API 封装

**替代思路**: 同花顺 iFind 终端有 Python API，但那是数据接口（行情/财务），不是交易接口。

**可行性**: ❌ 不可行
**推荐指数**: ⭐ (不推荐)

---

### 方案 3: 换券商 — 华泰 QMT / 国信 TradeStation 等

#### 3.1 华泰证券 QMT (XtQuant)

**简介**: 华泰证券的量化交易平台，提供 Python API (`xtquant`) 用于下单和查询。

**关键信息**:

| 维度 | 详情 |
|------|------|
| 平台支持 | **仅 Windows**。QMT 客户端是 Windows 原生程序，无 Mac 版 |
| API 类型 | 本地 API（`xtquant` Python 包），通过本地端口与 QMT 客户端通信 |
| 模拟交易 | ✅ 支持模拟交易（miniqmt） |
| 账户要求 | 华泰证券账户 + 50万资产门槛（部分营业部可降低） |
| ETF 支持 | ✅ 支持 ETF 买卖 |
| 下单方式 | `xt_trader.order_stock()` 同步返回订单号 |
| 持仓查询 | `xt_trader.query_stock_position()` 实时持仓 |

**Mac 上的变通方案**:
- 在 Windows 虚拟机（Parallels/VMware）中运行 QMT + miniqmt
- 通过 HTTP/gRPC 桥接 Mac 上的 `signal_generator.py` 与 Windows 虚拟机中的 QMT

**优点**:
- 原生 Python API，不依赖屏幕抓取/AppleScript
- 返回结构化数据（订单号、成交状态、持仓），无需 OCR
- 下单和查询分离，可以做完整的交易确认
- 华泰是国内量化交易首选券商，社区活跃，文档完善

**缺点**:
- **Mac 不直接支持** — 需要 Windows 虚拟机
- 需要开通华泰证券账户（门槛 50 万）
- 模拟交易需要 miniqmt，配置略复杂
- 需要额外维护 Windows 虚拟机
- 切换券商涉及资金转移、账户迁移

**实现难度**: ⭐⭐⭐⭐ (困难，需要虚拟机 + 跨平台桥接)
**稳定性预期**: ⭐⭐⭐⭐⭐ (非常稳定，原生 API)
**推荐指数**: ⭐⭐⭐⭐ (长期最佳方案，但需要投入)

#### 3.2 国信证券 TradeStation

**简介**: 国信证券的量化交易平台，基于 TradeStation 技术。

**关键信息**:

| 维度 | 详情 |
|------|------|
| 平台支持 | **仅 Windows** |
| API 类型 | EasyLanguage 脚本语言（非 Python 原生） |
| Python 支持 | 需要通过 DLL 桥接，非原生 |
| 模拟交易 | ✅ 支持 |
| Mac 支持 | ❌ 无 |

**评价**: 不如 QMT 适合 Python 量化交易场景，不推荐。

#### 3.3 其他券商

| 券商 | 平台 | API | Mac | 推荐 |
|------|------|-----|-----|------|
| 华泰证券 | QMT | ✅ Python (xtquant) | ❌ (需虚拟机) | ⭐⭐⭐⭐ |
| 国信证券 | TradeStation | ⚠️ EasyLanguage | ❌ | ⭐⭐ |
| 中信证券 | CATS | ⚠️ C++ | ❌ | ⭐⭐ |
| 东方财富 | 东财终端 | ❌ 无公开 API | ❌ | ⭐ |
| 华宝证券 | 华宝智投 | ✅ REST API | ✅ | ⭐⭐⭐ |
| 聚宽 (JoinQuant) | 云端 | ✅ Python | ✅ | ⭐⭐⭐ |

#### 3.4 华宝证券 LTS

**新发现**: 华宝证券提供 LTS (Lightweight Trading System) REST API，支持 Mac。

- 开户门槛低（无 50 万限制）
- 提供 HTTP REST API，语言无关
- 支持模拟交易
- **Mac 原生支持**（HTTP 接口，任何平台可用）
- ETF 支持需确认

**推荐指数**: ⭐⭐⭐ (门槛低，但券商规模小)

---

### 方案 4: AppleScript 重写（精简版）

**原理**: 放弃 EvolvingSim，直接写精简的 AppleScript 命令，去除锁机制。

**当前 EvolvingSim 的问题**:
1. 锁机制 (`/tmp/lock.json`) 是 EvolvingSim 自己加的，每次初始化请求锁 15 秒
2. EvolvingSim 的 AppleScript 复杂（27 处 `click button "模拟"` 补丁），大量冗余操作
3. `os.popen()` 无超时控制

**精简 AppleScript 方案**:

```applescript
-- 买入 ETF
tell application "同花顺" to activate
delay 0.3
tell application "System Events"
    tell process "同花顺"
        keystroke "f" using command down  -- 或 click button "买入"
        delay 0.2
        keystroke "159532"  -- 代码
        delay 0.1
        keystroke tab
        delay 0.1
        keystroke "1.500"  -- 价格
        delay 0.1
        keystroke tab
        delay 0.1
        keystroke "1000"  -- 数量
        delay 0.1
        keystroke return  -- 确认
    end tell
end tell
```

**关键改进**:
1. **不用锁**：直接 `osascript` 命令，不经过 EvolvingSim 的 Lock 机制
2. **用 `keystroke` 代替 `click button`**：比点击按钮更可靠，不受 UI 布局影响
3. **超时保护**：Python 侧 `subprocess.run(timeout=5)` 控制
4. **无 EvolvingSim 依赖**：去掉 `from evolving import EvolvingSim`

**优点**:
- 实现简单，几百行代码
- 不依赖第三方库 EvolvingSim
- 无锁机制，不会僵尸进程阻塞
- keystroke 比 `click button` 更可靠

**缺点**:
- 仍需同花顺保持在前台
- 无法直接读取持仓数据（需要 OCR 或解析同花顺导出文件）
- 14:30 模拟交易停止问题不变
- 同花顺窗口焦点问题不变（但 keystroke 比 click button 更容忍）

**实现难度**: ⭐⭐ (简单)
**稳定性预期**: ⭐⭐⭐ (比 EvolvingSim 好，但不如 PyAutoGUI)
**推荐指数**: ⭐⭐⭐ (快速止血方案)

---

### 方案 5: 其他替代方案

#### 5.1 聚宽 (JoinQuant) 云端

- 提供云端 Python 环境 + 模拟交易 API
- 支持 `order()` / `order_target()` 等标准 API
- **缺点**: 代码在云端运行，无法与本地的 `signal_generator.py` 集成
- 需要把策略迁移到聚宽平台

#### 5.2 东方财富 choice 数据 + 同花顺下单分离

- 数据用东方财富/腾讯 API，下单用精简 AppleScript
- 不换券商，只换下单层
- 相当于方案 1 + 方案 4 的组合

#### 5.3 虚拟机跑 Windows 同花顺

- 在 Parallels 中运行 Windows 版同花顺
- Windows 版同花顺的自动化方案更成熟（有 pywinauto, 通达信 API 等）
- 通过共享文件夹或 HTTP 与 Mac 通信
- **缺点**: 需要维护 Windows 虚拟机，资源开销大

#### 5.4 用 iPhone/iPad 同花顺 + 自动化

- iOS 同花顺支持 Siri 捷径 / 快捷指令
- 可以通过 Mac 控制 iPhone 自动化
- **缺点**: 复杂度过高，不稳定

---

## 三、综合对比矩阵

| 方案 | 稳定性 | 实现难度 | 维护成本 | Mac原生 | 数据回执 | 14:30问题 | 推荐指数 |
|------|--------|---------|---------|---------|---------|----------|---------|
| 现状 (EvolvingSim) | ⭐⭐ | - | ⭐⭐ | ✅ | ✅ API | ❌ | ⭐⭐ |
| 1. PyAutoGUI 键盘模拟 | ⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | ✅ | ⚠️ OCR | ❌ | ⭐⭐⭐ |
| 2. 同花顺网页版API | ❌ | ❌ | ❌ | - | - | - | ⭐ |
| 3. 华泰QMT+虚拟机 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ | ⚠️ 虚拟机 | ✅ API | ✅ | ⭐⭐⭐⭐ |
| 3. 华宝LTS | ⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐ | ✅ | ✅ API | ✅ | ⭐⭐⭐ |
| 4. AppleScript 精简版 | ⭐⭐⭐ | ⭐⭐ | ⭐⭐ | ✅ | ⚠️ OCR | ❌ | ⭐⭐⭐ |
| 5. 聚宽云端 | ⭐⭐⭐⭐ | ⭐⭐ | ⭐ | - | ✅ API | ✅ | ⭐⭐⭐ |

---

## 四、推荐路径

### 短期 (本周): 方案 4 AppleScript 精简版

**目标**: 止血，替换 EvolvingSim，减少锁/僵尸进程问题。

**做法**:
1. 在 `trade.py` 同级新建 `ths_keystroke.py`，用 `keystroke` 代替 EvolvingSim
2. 保持 `trade.py` 的接口不变 (buy/sell/sync/revoke)
3. 内部用 `subprocess.run(["osascript", "-e", "..."])` 直接执行快捷键命令
4. 持仓查询通过同花顺的"导出持仓"功能 + 读文件实现

**预计工作量**: 1-2 天

### 中期 (本月): 方案 1 PyAutoGUI 键盘模拟

**目标**: 提高稳定性，增加 OCR 回执确认。

**做法**:
1. 在方案 4 基础上，增加 PyAutoGUI 截图 + 图像识别确认
2. 持仓查询用 OCR 读取同花顺持仓表格
3. 错误处理：截图保存，方便人工核查

**预计工作量**: 3-5 天

### 长期 (下月): 方案 3a 华泰 QMT

**目标**: 彻底解决 Mac 同花顺自动化问题，获得原生 API。

**做法**:
1. 开通华泰证券账户（或已有账户）
2. 在 Parallels 中安装 QMT + miniqmt
3. 在 Windows 虚拟机中运行一个轻量 HTTP 桥接服务（FastAPI）
4. Mac 上的 `trade.py` 通过 HTTP 调用桥接服务下单
5. 接口适配：`trade.py` 的 `buy/sell/sync` 接口不变，底层从 EvolvingSim 切换到 HTTP

**预计工作量**: 1-2 周

---

## 五、具体实施建议

### 立即行动：方案 4 精简 AppleScript

```python
# ths_keystroke.py - 极简同花顺键盘控制
import subprocess, time

def _keystroke(applescript: str, timeout: int = 5) -> str:
    """执行 AppleScript，带超时保护"""
    proc = subprocess.Popen(
        ["osascript", "-e", applescript],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout.decode()
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError("AppleScript 超时")

def buy(code: str, price: float, shares: int):
    """买入: F1 → 输入代码/价格/数量 → Enter"""
    script = f'''
    tell application "同花顺" to activate
    delay 0.3
    tell application "System Events"
        tell process "同花顺"
            key code 122  -- F1 买入
            delay 0.3
            keystroke "{code}"
            delay 0.15
            keystroke tab
            delay 0.15
            keystroke "{price}"
            delay 0.15
            keystroke tab
            delay 0.15
            keystroke "{shares}"
            delay 0.15
            keystroke return
        end tell
    end tell
    '''
    return _keystroke(script)

def sell(code: str, price: float, shares: int):
    """卖出: F2 → 输入代码/价格/数量 → Enter"""
    script = f'''
    tell application "同花顺" to activate
    delay 0.3
    tell application "System Events"
        tell process "同花顺"
            key code 120  -- F2 卖出
            delay 0.3
            keystroke "{code}"
            delay 0.15
            keystroke tab
            delay 0.15
            keystroke "{price}"
            delay 0.15
            keystroke tab
            delay 0.15
            keystroke "{shares}"
            delay 0.15
            keystroke return
        end tell
    end tell
    '''
    return _keystroke(script)
```

### 同花顺 Mac 快捷键参考

| 快捷键 | 功能 | key code |
|--------|------|----------|
| F1 | 买入 | 122 |
| F2 | 卖出 | 120 |
| F3 | 撤单 | 99 |
| F4 | 持仓 | 118 |
| F5 | 委托 | 96 |
| F6 | 成交 | 97 |
| Cmd+F | 搜索代码 | - |

---

## 六、风险与注意事项

1. **同花顺版本更新**：快捷键可能变化，需要维护映射表
2. **macOS 权限**：辅助功能权限 + 屏幕录制权限，每次重启 macOS 需要确认
3. **14:30 模拟交易限制**：这是同花顺服务器端限制，客户端方案无法解决，只有换券商才能解决
4. **华泰 QMT 门槛**：50 万资产门槛，部分营业部可以沟通降低
5. **合规风险**：自动化交易需要确认券商是否允许程序化下单（模拟交易通常没问题）

---

## 七、结论

**当前最紧迫的问题**是 EvolvingSim 的锁机制和 osascript 僵尸进程导致的间歇性断连。这个问题可以用**方案 4 (AppleScript 精简版)** 在 1-2 天内解决。

**根本性解决**需要换券商，推荐**华泰 QMT**（需要 Windows 虚拟机）或**华宝 LTS**（Mac 原生 HTTP API）。

**不推荐**同花顺网页版 API（不存在）和纯 OCR 方案（精度不够）。

**推荐执行顺序**: 方案4 → 方案1 → 方案3a