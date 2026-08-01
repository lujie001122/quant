# Evolving — Mac同花顺自动化

## Overview

Evolving is a Python package that controls 同花顺 on macOS via AppleScript. It is the preferred way to automate 同花顺 operations on Mac — far more stable than cua-driver screenshot-based control.

Install:
```bash
pip install evolving -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install xmltodict -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## Config File

Required at `~/.config/evolving/config.xml` — even for read-only operations like getHoldingShares. Dummy values work for simulation accounts:

```xml
<?xml version="1.0" encoding="utf-8"?>
<evolving>
    <trading>
        <userid>模拟</userid>
        <password>模拟</password>
        <broker_code>模拟</broker_code>
        <broker_account>模拟</broker_account>
        <broker_password>模拟</broker_password>
        <bank_name></bank_name>
        <bank_account></bank_account>
        <bank_password></bank_password>
        <comment>模拟账户测试</comment>
    </trading>
</evolving>
```

## Key Methods

| Category | Methods |
|----------|---------|
| Trading | `buy(code, amount, price=None)`, `sell(code, amount, price=None)`, `buyStock`, `sellStock`, `buyGem`, `sellGem`, `buySciTech`, `sellSciTech` |
| Positions | `getHoldingShares(assetType='stock')`, `getAllHoldingShares`, `getAccountInfo`, `getCapitalDetails` |
| Orders | `getEntrust`, `revokeEntrust`, `revokeAllEntrust`, `revokeAllBuyEntrust`, `revokeAllSellEntrust` |
| Login | `loginBroker`, `isBrokerLoggedIn`, `logoutBroker` |
| IPO | `oneKeyIPO`, `getIPO`, `getIPOentrust`, `getIPOwinningLots` |
| Transfer | `transfer_bank2broker`, `transfer_broker2bank` |
| Query | `getClosedDeals`, `getBids`, `getAssetType(code)` |

## Usage Pattern

```python
from evolving import evolving
e = evolving.Evolving()

# Get holdings (returns dict with 'status', 'comment', 'data')
result = e.getHoldingShares('stock')

# Get account info
info = e.getAccountInfo()

# Buy ETF (code, amount in shares, price=None means best price)
status, contractNo = e.buy('159532', 1000, price=1.330)

# Sell
status, contractNo = e.sell('159532', 1000)
```

## assetType Detection

- Starts with `688` → `sciTech`
- Starts with `300` → `gem`
- Everything else (including ETFs) → `stock`

## Prerequisites

1. 同花顺 Mac client must be running and logged in
2. Terminal.app must have Accessibility permission (System Settings → Privacy & Security → Accessibility → enable Terminal)
3. 同花顺 supports keyboard shortcuts — prefer keyboard-driven AppleScript over screenshot-based approaches

## Known Issues

- Evolving's AppleScript commands use `os.popen()` which blocks. If 同花顺 is not responsive, calls will hang indefinitely (no timeout). Consider wrapping in a timeout mechanism.
- The `getAssetType` method treats ETFs as 'stock' — correct for 159516/515880/588170/159532/515050
- Logs go to `~/logs/evolving/` — create the directory if it doesn't exist
- **`getEntrust()` / `revokeAllEntrust()` unreliable**: May return `status: False, info: 'unknown err'` or `'超过93天'` (simulation account inactive >93 days). Buy/sell and `getHoldingShares` work reliably; order query and revocation are less stable. Workaround: try `revokeAllBuyEntrust()` / `revokeAllSellEntrust()` separately, or use AppleScript directly to click 撤单 in 委托 tab.
- **Why revocation fails (root cause)**: Evolving's `revokeAllEntrust` AppleScript does: click "股票" → click "委托" → click "全撤" → click "确认" (sheet dialog). It CAN handle confirmation dialogs. The failure occurs because the "超过93天" business warning popup appears when clicking "委托" (before reaching "全撤"), and this popup is not a standard sheet dialog — it's an unexpected alert that the try block catches as a generic failure. The "超过93天" warning is specific to simulation accounts inactive for >93 days. Direct AppleScript bypass (clicking "全撤" button directly) works because it skips the "委托" tab navigation step.
- **Buy returns `(True, "--")`**: Contract number may not be captured — verify via `getEntrust()` or check 同花顺 UI directly.
- **cua-driver uninstalled**: User chose Evolving as the sole 同花顺 automation method. cua-driver was installed and then removed. Do not reinstall unless user asks. Installation reference preserved in references/cua-driver-china-setup.md for future use.
- **同花顺 window disappears (CRITICAL)**: 同花顺 Mac uses a custom-drawn UI. When the trading panel is closed/minimized, `count of windows` returns 0 and ALL Evolving operations fail with "unknown err". `activate` alone does NOT restore the window. Fixes: (1) menu "窗口" → "主窗口", (2) keyboard shortcuts, (3) ask user to manually open trading panel. After `killall + open` restart, 同花顺 may not auto-open the trading window — user intervention may be needed.
- **Direct AppleScript revocation workaround**: When Evolving's `revokeAllEntrust()` fails (common with "超过93天" warning), bypass Evolving and click the button directly:
  ```bash
  osascript -e 'tell application "同花顺" to activate
  delay 0.5
  tell application "System Events"
      tell process "同花顺"
          click button "全撤" of window 1
      end tell
  end tell'
  ```
  Available button names in 同花顺's window 1: 全撤, 撤买, 撤卖, 委托, 持仓, 成交, 资金明细, 买入, 卖出, 确定买入, A股, 模拟, 股票, 重填

## 模拟 vs A股 Account

Evolving's AppleScript defaults to clicking the "A股" button. For simulation accounts, you must click "模拟" instead.

### Current Patch (fragile)

The installed `ascmds.py` was patched to replace all `click button "A股"` with `click button "模拟"` (27 occurrences). This works but **will be overwritten by pip upgrade**. 

To re-apply the patch:
```python
from evolving import ascmds
import inspect
ascmds_path = inspect.getfile(ascmds)
with open(ascmds_path, 'r') as f:
    src = f.read()
src = src.replace('click button "A股"', 'click button "模拟"')
with open(ascmds_path, 'w') as f:
    f.write(src)
```

When user switches to real A股 account, reverse the patch or maintain a local fork with a config-driven account type selector.

## THSClient Python Wrapper

A Python wrapper (`~/hermes/scripts/ths_client.py`) wraps Evolving with:
- **Automatic alert dismissal** — calls `dismiss_alerts()` before/after each Evolving call
- **Retry on failure** — if Evolving returns `status: False`, dismisses alerts and retries once
- **Fixed revocation** — uses direct AppleScript to click 全撤/撤买/撤卖 buttons (bypassing Evolving's broken `revokeAllEntrust` which fails on "超过93天" popup)
- **Mode parameter** — "模拟" (default) or "A股"

Usage:
```python
from ths_client import THSClient, get_client
client = THSClient(mode="模拟")

# Query
client.get_holding_shares()    # returns dict
client.get_account_info()      # returns dict
client.get_entrust()           # returns dict

# Trade
client.buy("159532", 1000, price=1.330)   # returns (status, contractNo)
client.sell("159532", 1000)                # returns (status, contractNo)

# Revoke
client.revoke_all()           # all orders
client.revoke_all_buy()       # buy orders only
client.revoke_all_sell()      # sell orders only
```

CLI: `python3 ths_client.py [holdings|account|entrust|summary|buy|sell|revoke_all|revoke_buy|revoke_sell]`

**Important**: The `has_window()` method in THSClient is unreliable — 同花顺 5.3.2 uses a self-drawn UI that reports 0 windows to AppleScript even when the trading panel is visible. Do NOT use `has_window()` as a gate before Evolving calls. Instead, try Evolving directly and handle failures.

## Workflow: Sync Portfolio from 同花顺

1. Use `e.getHoldingShares('stock')` to get actual positions
2. Map to `_signal_state` in `portfolio.json`: set `build_phase=1`, `build_first_price=cost`, `peak_price=cost` for held positions; `build_phase=0` for empty
3. Sync both copies: `~/hermes/scripts/portfolio.json` and `~/.hermes/scripts/portfolio.json`
