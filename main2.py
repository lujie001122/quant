import math

import backtrader as bt

import SqlUtil
import StrategyUtil
from M5RsiStrategy import M5RsiStrategy,SignalData

from ccxt_utils import fetch_ohlcv
# import config


def main():
    cerebro = bt.Cerebro()
    # 从 OKX 获取数据
    symbol = 'DOGE/USDT'
    timeframe = '15m'
    df = fetch_ohlcv('okx', symbol, timeframe)
    m5 = 3
    m10 = 6
    kdj_min = 10
    kdj_max = 64
    rsi_min = 11
    rsi_max = 44


    df = StrategyUtil.formatStrategyPandas(df,m5=m5,m10=m10,kdj_min=kdj_min,kdj_max=kdj_max,rsi_min=rsi_min,rsi_max=rsi_max)
    # 使用自定义数据类
    data = SignalData(dataname=df)
    cerebro.adddata(data)
    # 添加策略
    cerebro.addstrategy(M5RsiStrategy)
    # 设置初始资金和佣金
    start_cash = 10000.0
    cerebro.broker.setcash(start_cash)
    cerebro.broker.setcommission(commission=0.001)
    # 运行回测
    print('初始资金: %.2f' % cerebro.broker.getvalue())
    cerebro.run()
    print('最终资金: %.2f' % cerebro.broker.getvalue())
    print('净收益: %.2f' % (cerebro.broker.getvalue() - start_cash))
    cerebro.plot()

if __name__ == "__main__":
    main()