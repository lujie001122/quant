import math
import time

import backtrader as bt
import ccxt
import pandas as pd


import StrategyUtil
from M5RsiStrategy import M5RsiStrategy,SignalData

from ccxt_utils import fetch_ohlcv, place_order
def fetch_ohlcv(exchange_id, symbol, timeframe, limit=300):
    m5 = 3
    m10 = 6
    kdj_min = 10
    kdj_max = 64
    rsi_min = 11
    rsi_max = 44

    api_key = "d3b3d339-cb05-44d9-a179-a443e2e832d8"
    secret = "2EB8F8730B211394F3464752A5D4D791"
    password = "Qazwsx12@"
    exchange = ccxt.okx({
        'apiKey': api_key,
        'secret': secret,
        'password': password,
        'options': {
            'defaultType': 'spot'
        },
        # 'proxies': {
        #     'http': 'socks5://127.0.0.1:10808',
        #     'https': 'socks5h://127.0.0.1:10808',
        # }
    })
    exchange.setSandboxMode(True)

    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
    df.set_index('datetime', inplace=True)
    df = StrategyUtil.formatStrategyPandas(df, m5=m5, m10=m10, kdj_min=kdj_min,
                                           kdj_max=kdj_max, rsi_min=rsi_min,
                                           rsi_max=rsi_max)
    return df

def main():


    cerebro = bt.Cerebro()
    symbol = 'DOGE/USDT'
    timeframe = '15m'
    df = fetch_ohlcv('okx', symbol, timeframe)


    # 这里添加数据，使用 Backtrader 的 DataFeed 机制
    data = SignalData(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(M5RsiStrategy)
    start_cash = 100000.0
    cerebro.broker.setcash(start_cash)
    cerebro.broker.setcommission(commission=0.001)

    api_key = "d3b3d339-cb05-44d9-a179-a443e2e832d8"
    secret = "2EB8F8730B211394F3464752A5D4D791"
    password = "Qazwsx12@"
    exchange = ccxt.okx({
        'apiKey': api_key,
        'secret': secret,
        'password': password,
        'options': {
            'defaultType': 'spot'
        },
        # 'proxies': {
        #     'http': 'socks5://127.0.0.1:10808',
        #     'https': 'socks5h://127.0.0.1:10808',
        # }
    })
    exchange.setSandboxMode(True)
    # 获取初始账户余额
    balance = exchange.fetch_balance()
    print(f"初始账户余额: {balance}")
    while True:
        try:
            # 获取最新的 OHLCV 数据
            df = fetch_ohlcv(exchange, symbol,timeframe)
            print(df)
            if len(df) == 0:
                print("No OHLCV data available.")
                time.sleep(60*8)
                continue

            # 获取当前持仓
            symbol_parts = symbol.split('/')
            symbol_balance_key = symbol_parts[0]
            symbol_balance = balance.get(symbol_balance_key, {'free': 0})  # 使用 get 方法避免 KeyError
            position = symbol_balance['free']
            print(df.iloc[-1]['rsi_signal']>1)
            print(df.iloc[-1]['golden_cross']>1)
            print(df.iloc[-1]['kdj_signal']>0)
            # 买入逻辑
            # 交易逻辑：双移动平均交叉策略
            if position <= 0 and (df.iloc[-1]['rsi_signal']>0 and df.iloc[-1]['golden_cross'] > 0 ) or (df.iloc[-1]['rsi_signal']>0 and df.iloc[-1]['kdj_signal']>0)  :
                # 买入逻辑
                order_amount = start_cash/ df.iloc[-1]['close'] # 用初始资金全部买入
                place_order(exchange, symbol, 'buy', order_amount)
            elif  position > 0 and (df.iloc[-1]['dead_cross'] >1  or   df.iloc[-1]['kdj_signal']< 0 or df.iloc[-1]['rsi_signal'] < 0 ) :
                # 卖出逻辑
                place_order(exchange, symbol, 'sell',position)

            # 更新账户余额
            balance = exchange.fetch_balance()
            print(f"当前账户余额: {balance}")

            # 等待一段时间再进行下一次检查
            time.sleep(60*8)  # 每 60 秒检查一次
        except Exception as e:
            print(f"Error occurred: {e}")
            time.sleep(60*8)



if __name__ == "__main__":
    main()