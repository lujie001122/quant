import os

import ccxt


class OKXTrader:
    def __init__(self, api_key, secret, password, is_demo=True, default_type='swap'):
        self.exchange = ccxt.okx({
            'apiKey': api_key,
            'secret': secret,
            'password': password,
            'options': {
                'defaultType': default_type,
                'demo': is_demo
            },
            'proxies': {
                'http': 'socks5://127.0.0.1:10808',
                'https': 'socks5h://127.0.0.1:10808',
            }
        })
        self.exchange.setSandboxMode(True)

    def load_markets(self):
        """
        加载市场数据
        """
        try:
            markets = self.exchange.load_markets()
            return markets
        except Exception as e:
            print(f"Error loading markets: {e}")
            return None

    def fetch_balance(self):
        """
        获取账户余额信息
        """
        try:
            balance = self.exchange.fetch_balance()
            return balance
        except Exception as e:
            print(f"Error fetching balance: {e}")
            return None

    def create_order(self, symbol, order_type, side, amount, price=None):
        """
        创建订单
        :param symbol: 交易对，如 'BTC/USDT'
        :param order_type: 订单类型，如 'limit'、'market' 等
        :param side: 订单方向，'buy' 或 'sell'
        :param amount: 订单数量
        :param price: 订单价格（对于限价单），可选
        """
        try:
            if order_type == 'limit' and price is None:
                raise ValueError("Price must be provided for limit orders")
            order = self.exchange.create_order(symbol, order_type, side, amount, price)
            return order
        except ValueError as ve:
            print(ve)
            return None
        except Exception as e:
            print(f"Error creating order: {e}")
            return None

    def cancel_order(self, order_id, symbol):
        """
        取消订单
        :param order_id: 订单 ID
        :param symbol: 交易对，如 'BTC/USDT'
        """
        try:
            result = self.exchange.cancel_order(order_id, symbol)
            return result
        except Exception as e:
            print(f"Error canceling order: {e}")
            return None


# 示例使用
if __name__ == "__main__":

    api_key = "d3b3d339-cb05-44d9-a179-a443e2e832d8"
    secret = "2EB8F8730B211394F3464752A5D4D791"
    password = "Qazwsx12@"
    trader = OKXTrader(api_key, secret, password)


    # 加载市场数据
    # markets = trader.load_markets()
    # if markets:
    #     print(markets)
    # 获取账户余额
    balance = trader.fetch_balance()
    if balance:
        print(balance)
    # 创建一个订单
    order = trader.create_order('BTC/USDT', 'limit', 'buy', 0.01, 10000)
    if order:
        print(order)
    # 取消一个订单（假设已有订单，需要替换为真实订单 ID 和交易对）
    # cancel_result = trader.cancel_order('ORDER_ID', 'BTC/USDT')
    # if cancel_result:
    #     print(cancel_result)