import ccxt
import backtrader as bt
import pandas as pd
import numpy as np


class OKXDataFeed(bt.feeds.PandasData):
    # 明确所需的线数据
    lines = ('vpt', 'high', 'low', 'open', 'close', 'volume')
    # 参数映射，确保每个线数据的位置正确
    params = (
        ('vpt', -1),
        ('high', 1),
        ('low', 2),
        ('open', 0),
        ('close', 3),
        ('volume', 4)
    )


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
        # 先检查数据是否包含所需属性
        if not hasattr(self.data, 'high'):
            raise ValueError("Data does not contain 'high' attribute.")
        # 初始化指标
        self.sma_fast = bt.indicators.SimpleMovingAverage(self.data.close, period=self.params.sma_fast_period)
        self.sma_slow = bt.indicators.SimpleMovingAverage(self.data.close, period=self.params.sma_slow_period)
        self.rsi = bt.indicators.RelativeStrengthIndex(period=self.params.rsi_period)
        self.stoch = bt.indicators.Stochastic(self.data.high, self.data.low, self.data.close, period=self.params.k_period)
        self.k = self.stoch.lines.percK
        self.d = self.stoch.lines.percD
        self.vpt = self.calculate_vpt()
        self.vpt_ma = bt.indicators.SimpleMovingAverage(self.vpt, period=self.params.vpt_period)
        self.boll = bt.indicators.BollingerBands(self.data.close, period=self.params.boll_period, devfactor=self.params.boll_std)
        # 买入和卖出信号
        self.buy_signal_1 = bt.And(self.sma_fast > self.sma_slow, self.sma_fast(-1) <= self.sma_slow(-1))
        self.sell_signal_1 = bt.And(self.sma_fast < self.sma_slow, self.sma_fast(-1) >= self.sma_slow(-1))
        self.buy_signal_2 = bt.And(self.k > self.d, self.k(-1) <= self.d(-1), self.k > 20)
        self.sell_signal_2 = bt.And(self.k < self.d, self.k(-1) >= self.d(-1), self.k < 80)
        self.buy_signal_3 = bt.And(self.rsi < self.params.rsi_buy_threshold, self.data.close > self.sma_fast)
        self.sell_signal_3 = bt.And(self.rsi > self.params.rsi_sell_threshold, self.data.close < self.sma_fast)
        self.buy_signal_4 = bt.And(self.vpt > self.vpt_ma, self.data.close > self.sma_fast)
        self.sell_signal_4 = bt.And(self.vpt < self.vpt_ma, self.data.close < self.sma_fast)
        self.buy_signal_5 = self.data.close > self.boll.lines.top
        self.sell_signal_5 = self.data.close < self.boll.lines.bot

    def calculate_vpt(self):
        vpt = np.zeros(len(self.data))
        for i in range(1, len(self.data)):
            vpt[i] = vpt[i - 1] + (self.data.volume[i] * (self.data.close[i] - self.data.close[i - 1]) / self.data.close[i - 1])
        return vpt

    def next(self):
        # 同时满足多个买入信号时买入
        if all([self.buy_signal_1, self.buy_signal_2, self.buy_signal_3, self.buy_signal_4, self.buy_signal_5]):
            self.buy()
        # 同时满足多个卖出信号时卖出
        elif all([self.sell_signal_1, self.sell_signal_2, self.sell_signal_3, self.sell_signal_4, self.sell_signal_5]):
            self.sell()


def fetch_ohlcv(exchange, symbol, timeframe, fromdate=None, todate=None, limit=None):
    ohlcv = []
    if fromdate and todate:
        from_ms = int(pd.Timestamp(fromdate).timestamp() * 1000)
        to_ms = int(pd.Timestamp(todate).timestamp() * 1000)
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=from_ms, limit=limit)
        last_timestamp = ohlcv[-1][0] if ohlcv else from_ms
        while last_timestamp < to_ms:
            new_ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=last_timestamp, limit=limit)
            if not new_ohlcv:
                break
            ohlcv.extend(new_ohlcv[1:])  # 避免重复添加第一个元素
            last_timestamp = new_ohlcv[-1][0]
    else:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
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
    timeframe = '1d'
    fromdate = "2024-12-11"
    todate = "2024-12-19"
    df = fetch_ohlcv(exchange, symbol, timeframe, fromdate=fromdate, todate=todate,limit=200)

    # 初始化 BackTrader
    cerebro = bt.Cerebro()
    # 使用修改后的 OKXDataFeed 类，并确保数据完整性，添加日期范围
    data = OKXDataFeed(dataname=df, fromdate=pd.to_datetime(fromdate), todate=pd.to_datetime(todate))
    data = bt.feeds.PandasData(dataname=data, fromdate=pd.to_datetime("2022-01-01"), todate=pd.to_datetime("2024-12-11"))
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