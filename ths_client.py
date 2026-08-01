#!/usr/bin/env python3
"""
同花顺 Mac 客户端 Python 接口（基于 Evolving）
在 Evolving 之上封装：自动激活窗口、撤单、限价单、异常日志
"""
import json, time, os, sys, logging

# 添加 Evolving 路径
sys.path.insert(0, os.path.expanduser("~/hermes-trading/.venv/lib/python3.9/site-packages"))

from evolving import evolving

logger = logging.getLogger("ths_client")
logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")


class THSClient:
    """基于 Evolving 的同花顺客户端接口（精简版，不干扰Evolving）"""

    def __init__(self, mode="模拟"):
        self.mode = mode
        self._e = evolving.Evolving()
        self._activate()

    def _activate(self):
        """激活同花顺窗口（Evolving需要窗口在前台）"""
        os.system("""osascript -e 'tell application "同花顺" to activate' 2>/dev/null""")
        time.sleep(2)

    def _query_with_retry(self, method_name, *args, retries=1, delay=1):
        """通用查询方法：带重试和日志"""
        method = getattr(self._e, method_name, None)
        if method is None:
            logger.error(f"Evolving无方法: {method_name}")
            return {"status": False, "info": f"method {method_name} not found"}
        for attempt in range(retries + 1):
            try:
                result = method(*args)
                if not result.get("status"):
                    logger.warning(f"{method_name} 返回失败(尝试{attempt+1}): {result.get('info', 'unknown')}")
                    if attempt < retries:
                        time.sleep(delay)
                        continue
                return result
            except Exception as e:
                logger.error(f"{method_name} 异常(尝试{attempt+1}): {e}")
                if attempt < retries:
                    time.sleep(delay)
                    continue
                return {"status": False, "info": str(e)}
        return result

    # ─── 查询 ───

    def get_holding_shares(self, asset_type="stock"):
        """获取持仓"""
        return self._query_with_retry("getHoldingShares", asset_type)

    def get_account_info(self):
        """获取账户资金信息"""
        return self._query_with_retry("getAccountInfo")

    def get_entrust(self):
        """获取今日委托"""
        return self._query_with_retry("getEntrust")

    def get_closed_deals(self):
        """获取成交记录"""
        return self._query_with_retry("getClosedDeals")

    # ─── 交易（先撤同方向未成交挂单） ───

    def _revoke_pending(self, stock_code, direction):
        """撤销同标的同方向的未成交挂单
        direction: '买入' 或 '卖出'
        用revokeAllBuyEntrust/revokeAllSellEntrust批量撤更可靠。
        """
        try:
            if "买" in direction:
                self._e.revokeAllBuyEntrust()
            else:
                self._e.revokeAllSellEntrust()
            time.sleep(0.5)
        except Exception as e:
            logger.warning(f"撤单失败({direction}): {e}")

    def buy(self, stock_code, amount, price):
        """限价买入，返回 (status, contractNo)
        仓位上限: 单只ETF不超过4.4万(含手续费)
        先撤销同标的买入未成交挂单
        """
        self._revoke_pending(stock_code, "买入")
        if price and amount * price > 44000:
            amount = int(44000 / price / 100) * 100
            if amount < 100:
                logger.warning(f"买入{stock_code}超过4.4万仓位上限，无法下单")
                return False, "超过4.4万仓位上限"
        try:
            status, contract = self._e.buy(stock_code, amount, price)
            logger.info(f"买入{stock_code} {amount}股@{price}: status={status}, contract={contract}")
            return status, contract
        except Exception as e:
            logger.error(f"买入{stock_code}异常: {e}")
            return False, str(e)

    def sell(self, stock_code, amount, price):
        """限价卖出，返回 (status, contractNo)
        先撤销同标的卖出未成交挂单
        """
        self._revoke_pending(stock_code, "卖出")
        try:
            status, contract = self._e.sell(stock_code, amount, price)
            logger.info(f"卖出{stock_code} {amount}股@{price}: status={status}, contract={contract}")
            return status, contract
        except Exception as e:
            logger.error(f"卖出{stock_code}异常: {e}")
            return False, str(e)

    # ─── 撤单 ───

    def revoke_all(self):
        """撤销所有委托"""
        try:
            self._e.revokeAllEntrust()
            time.sleep(0.5)
            return True
        except Exception as e:
            logger.error(f"全撤异常: {e}")
            return False

    def revoke_all_buy(self):
        """撤销所有买入委托"""
        try:
            self._e.revokeAllBuyEntrust()
            time.sleep(0.5)
            return True
        except Exception as e:
            logger.error(f"撤买异常: {e}")
            return False

    def revoke_all_sell(self):
        """撤销所有卖出委托"""
        try:
            self._e.revokeAllSellEntrust()
            time.sleep(0.5)
            return True
        except Exception as e:
            logger.error(f"撤卖异常: {e}")
            return False

    # ─── 便捷 ───

    def summary(self):
        """获取账户概要：持仓 + 资金"""
        holdings = self.get_holding_shares()
        account = self.get_account_info()
        return {
            "holdings": holdings,
            "account": account,
        }


# ─── 单例 ───
_client = None

def get_client(mode="模拟"):
    global _client
    if _client is None:
        _client = THSClient(mode=mode)
    return _client


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: ths_client.py [holdings|account|entrust|summary|buy|sell|revoke_all|revoke_buy|revoke_sell]")
        sys.exit(0)

    client = THSClient()
    cmd = sys.argv[1]

    if cmd == "holdings":
        r = client.get_holding_shares()
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "account":
        r = client.get_account_info()
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "entrust":
        r = client.get_entrust()
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "summary":
        r = client.summary()
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "buy":
        code = sys.argv[2]
        amount = int(sys.argv[3])
        price = float(sys.argv[4])
        status, contract = client.buy(code, amount, price)
        print(f"限价买入: status={status}, contract={contract}")

    elif cmd == "sell":
        code = sys.argv[2]
        amount = int(sys.argv[3])
        price = float(sys.argv[4])
        status, contract = client.sell(code, amount, price)
        print(f"限价卖出: status={status}, contract={contract}")

    elif cmd == "revoke_all":
        client.revoke_all()
        print("全撤已执行")

    elif cmd == "revoke_buy":
        client.revoke_all_buy()
        print("撤买已执行")

    elif cmd == "revoke_sell":
        client.revoke_all_sell()
        print("撤卖已执行")

    else:
        print(f"未知命令: {cmd}")
