import ccxt
import backtrader as bt
import pandas as pd
import numpy as np


class OKXDataFeed(bt.feeds.PandasData):
    lines = ('vpt',)
    params = (('vpt', -1),)


class CombinedStrategy(bt.Strategy):
    params = (
        ('sma_fast_period', 5),
        ('sma_slow_period', 10),
        ('rsi_period', 14),
        ('rsi_buy_threshold', 30),
        ('rsi_sell_threshold', 70),
        ('k_period', 9),
        ('d_period', 3),
        ('vpt_period', 7),
        ('boll_period', 20),
        ('boll_std', 2)
    )

    def __init__(self):
        self.sma_fast = bt.indicators.SimpleMovingAverage(self.data.close, period=self.params.sma_fast_period)
        self.sma_slow = bt.indicators.SimpleMovingAverage(self.data.close, period=self.params.sma_slow_period)
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)
        self.stoch = bt.indicators.Stochastic(self.data.high, self.data.low, self.data.close, period=self.params.k_period)
        self.k = self.stoch.lines.percK
        self.d = self.stoch.lines.percD
        self.vpt = self.calculate_vpt()
        self.vpt_ma = bt.indicators.SimpleMovingAverage(self.vpt, period=self.params.vpt_period)
        self.boll = bt.indicators.BollingerBands(self.data.close, period=self.params.boll_period, devfactor=self.params.boll_std)

        # 买入信号
        self.buy_signal = (
            (bt.indicators.CrossUp(self.sma_fast, self.sma_slow)) &
            (bt.indicators.CrossUp(self.k, self.d) & (self.k > 20)) &
            (self.rsi < self.params.rsi_buy_threshold) &
            (self.data.close > self.sma_fast) &
            (bt.indicators.CrossUp(self.vpt, self.vpt_ma) & (self.data.close > self.sma_fast)) &
            (self.data.close > self.boll.lines.top)
        )

        # 卖出信号
        self.sell_signal = (
            (bt.indicators.CrossDown(self.sma_fast, self.sma_slow)) &
            (bt.indicators.CrossDown(self.k, self.d) & (self.k < 80)) &
            (self.rsi > self.params.rsi_sell_threshold) &
            (self.data.close < self.sma_fast) &
            (bt.indicators.CrossDown(self.vpt, self.vpt_ma) & (self.data.close < self.sma_fast)) &
            (self.data.close < self.boll.lines.bot)
        )

    def calculate_vpt(self):
        vpt = np.zeros(len(self.data))
        for i in range(1, len(self.data)):
            vpt[i] = vpt[i - 1] + (self.data.volume[i] * (self.data.close[i] - self.data.close[i - 1]) / self.data.close[i - 1])
        return vpt

    def next(self):
        # 买入信号
        if self.buy_signal[0]:
            self.buy()
        # 卖出信号
        elif self.sell_signal[0]:
            self.sell()


def fetch_ohlcv(exchange, symbol, timeframe, limit=1000):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('datetime', inplace=True)
    return df


def main():
    api_key = "d3b3d339-cb05-44d9-a179-a443e2e832d8"
    secret = "2EB8F8730B211394F3464752A5D4D791"
    password = "Qazwsx12@"
    # 初始化 OKX 交易所
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

    # 获取数据
    symbol = 'BTC/USDT'
    timeframe = '1h'
    df = fetch_ohlcv(exchange, symbol, timeframe)
    print(df)
    # 初始化 BackTrader
    cerebro = bt.Cerebro()
    data = OKXDataFeed(dataname=df)
    print(data)
    cerebro.adddata(data)
    cerebro.addstrategy(CombinedStrategy)
    cerebro.broker.setcash(10000.0)
    cerebro.broker.setcommission(commission=0.001)

    # 运行回测
    print('Starting Portfolio Value: %.2f' % cerebro.broker.getvalue())
    cerebro.run()
    print('Final Portfolio Value: %.2f' % cerebro.broker.getvalue())


if __name__ == "__main__":
    main()