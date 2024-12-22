import ccxt
import backtrader as bt
import talib
import pandas as pd
import numpy as np

class SignalData(bt.feeds.PandasData):
    lines = ('signal','rsi_value','vpt_value')
    params = (
        ('signal', -1),
        ('rsi_value', -2),
        ('vpt_value', -3),
              )
def fetch_ohlcv(exchange, symbol, timeframe, limit=2000):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
    df.set_index('datetime', inplace=True)
    # 计算 VPT 指标
    close_prices = np.array(df['close'])
    volume = np.array(df['volume'])
    vpt_values = talib.VPT(close_prices, volume)
    df['vpt_value'] = vpt_values
    return df


class VPTMAStrategy(bt.Strategy):
    params = (
        ('vpt_period', 7),
        ('ma_period', 10),
    )

    def __init__(self):
        self.close = self.data.close
        self.volume = self.data.volume
        # 计算 VPT 指标
        self.vpt = self.datas[0].vpt_value
        # 计算 VPT 的 7 日移动平均线
        self.vpt_ma = bt.indicators.SimpleMovingAverage(self.vpt, period=self.params.vpt_period)
        # 计算价格的短期移动平均线
        self.ma = bt.indicators.SimpleMovingAverage(self.close, period=self.params.ma_period)
        self.order = None

    def next(self):
        if self.order:
            return
        # 获取最新的指标和价格数据
        vpt_value = self.vpt[0]
        vpt_ma_value = self.vpt_ma[0]
        prev_vpt_value = self.vpt[-1]
        prev_vpt_ma_value = self.vpt_ma[-1]
        close_price = self.close[0]
        ma_value = self.ma[0]
        prev_close_price = self.close[-1]
        prev_ma_value = self.ma[-1]

        # 检查买入信号
        if vpt_value > vpt_ma_value and prev_vpt_value <= prev_vpt_ma_value and close_price > ma_value:
            self.buy()
        # 检查卖出信号
        elif vpt_value < vpt_ma_value and prev_vpt_value >= prev_vpt_ma_value and close_price < ma_value:
            self.sell()


def main():
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
        'proxies': {
            'http': 'socks5://127.0.0.1:10808',
            'https': 'socks5h://127.0.0.1:10808',
        }
    })
    exchange.setSandboxMode(True)
    symbol = 'BTC/USDT'
    timeframe = '15m'
    df = fetch_ohlcv(exchange, symbol, timeframe)

    cerebro = bt.Cerebro()
    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(VPTMAStrategy)
    cerebro.broker.setcash(10000.0)
    cerebro.broker.setcommission(commission=0.001)
    print('初始资金: %.2f' % cerebro.broker.getvalue())
    cerebro.run()
    print('最终资金: %.2f' % cerebro.broker.getvalue())


if __name__ == "__main__":
    main()