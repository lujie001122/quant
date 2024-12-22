from datetime import datetime

import ccxt
import backtrader as bt
import numpy as np
import pandas as pd
import talib

from CommInfoFractional import CommInfoFractional


class SignalData(bt.feeds.PandasData):
    lines = ('signal','rsi_value')
    params = (
        ('signal', -1),
         ('rsi_value', -2),
              )
class SimpleCrossStrategy(bt.Strategy):
    params = (
        ('sma_fast_period', 7),  # 缩短快速移动平均线周期
        ('sma_slow_period', 13),  # 缩短慢速移动平均线周期
        ('boll_period', 20),
        ('boll_std', 2),
        ('trade_size', 20000)
    )

    def __init__(self):
        # 计算五日和十日均线
        self.sma5 = bt.indicators.SimpleMovingAverage(self.data, period=self.params.sma_fast_period)
        self.sma10 = bt.indicators.SimpleMovingAverage(self.data, period=self.params.sma_slow_period)
        self.rsi_value = self.datas[0].rsi_value

    def next(self):
        # 获取当前数据的收盘价
        close_price = self.data.close[0]
        if not self.position and self.sma5[0] > self.sma10[0] and self.sma5[-1] <= self.sma10[-1] and (self.rsi_value[0] > 20 and self.rsi_value[0] < 90 ):#(self.rsi_value[0] > 30 and self.rsi_value[0] < 70 ):
            # 获取当前可用现金
            cash = self.broker.getcash()*0.8
            # 计算可购买的比特币数量，考虑到可能有小数精度问题，使用 round 函数
            size = round(cash / close_price, 8)
            self.order = self.buy(size=size)
        if (self.position and self.sma5[0] < self.sma10[0] and self.sma5[-1] >= self.sma10[-1]) or self.rsi_value[0] > 90:
            # 获取当前持仓量
            size = self.getposition(self.data).size
            self.order = self.sell(size=size)

    def log(self, txt, dt=None):
        # 记录策略的执行日志
        dt = dt or self.datas[0].datetime.date(0)
        print('%s, %s' % (dt.isoformat(), txt))
        # 订单状态通知，买入卖出都是下单

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            # broker 提交/接受了，买/卖订单则什么都不做
            return
        # 检查一个订单是否完成
        # 注意: 当资金不足时，broker会拒绝订单
        if order.status in [order.Completed]:
            if order.isbuy():
                self.log(
                    '已买入, 价格: %.2f, 执行总价值: %.2f, 佣金 %.2f' %
                    (order.executed.price,
                     order.executed.value,
                     order.executed.comm))
                self.buyprice = order.executed.price
                self.buycomm = order.executed.comm
            elif order.issell():
                self.log('已卖出, 价格: %.2f, 执行总价值: %.2f, 佣金 %.2f' %
                         (order.executed.price,
                          order.executed.value,
                          order.executed.comm))
                # 记录当前交易数量
            self.bar_executed = len(self)
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('订单取消/保证金不足/拒绝')
            print(order)

            # 其他状态记录为：无挂起订单
        self.order = None
        # 交易状态通知，一买一卖算交易

    # 交易状态通知，一买一卖算交易
    def notify_trade(self, trade):
        if not trade.isclosed:
            return
        self.log('交易利润, 毛利润 %.2f, 净利润 %.2f' %
        (trade.pnl, trade.pnlcomm))


def fetch_ohlcv(exchange, symbol, timeframe, limit=2000):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
    df.set_index('datetime', inplace=True)
    # 计算 RSI 指标
    close_prices = np.array(df['close'])
    rsi_values = talib.RSI(close_prices, timeperiod=14)
    df['rsi_value'] = rsi_values
    return df


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
    symbol = 'DOGE/USDT'
    timeframe = '15m'
    df = fetch_ohlcv(exchange, symbol, timeframe)

    start_cash = 10000.0
    cerebro = bt.Cerebro()
    data = SignalData(dataname=df)
    cerebro.adddata(data)


    cerebro.addstrategy(SimpleCrossStrategy)
    cerebro.broker.setcash(start_cash)
    cerebro.broker.setcommission(commission=0.0001)
    # comminfo = CommInfoFractional(commission=0.0001)
    # cerebro.broker.addcommissioninfo(comminfo)
    # cerebro.broker.set_checksubmit(False)  # 避免下单时检查保证金，仅用于回测
    # 运行回测
    cerebro.run()
    # 输出回测结果
    print('初始资金: %.2f' % start_cash)
    print('回测结束后的总资金: %.2f' % cerebro.broker.getvalue())
    print('净收益: %.2f' % (cerebro.broker.getvalue() - start_cash))
    # 画图
    cerebro.plot()

if __name__ == "__main__":
    main()