import math
import backtrader as bt
import SqlUtil
import StrategyUtil
from M5RsiStrategy import M5RsiStrategy, SignalData
from ccxt_utils import fetch_ohlcv
import threading
import time


def backtest(df, im5, im10, ikdj_min, ikdj_max, irsi_min, irsi_max):
    df = StrategyUtil.formatStrategyPandas(df, m5=im5, m10=im10, kdj_min=ikdj_min, kdj_max=ikdj_max, rsi_min=irsi_min, rsi_max=irsi_max)
    cerebro = bt.Cerebro()
    data = SignalData(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(M5RsiStrategy)
    start_cash = 10000.0
    cerebro.broker.setcash(start_cash)
    cerebro.broker.setcommission(commission=0.001)
    cerebro.run()
    profit = (cerebro.broker.getvalue() - start_cash)
    print(f'初始：10000，结束：{cerebro.broker.getvalue():.2f}, 净收益: {profit:.2f}')
    if profit > 0:
        sql = """INSERT INTO `strategy`( `m5`, `m10`, `kdj_min`, `kdj_max`, `rsi_min`, `rsi_max`, `profit`)
                                             VALUES ('{m5}', '{m10}', '{kdj_min}', '{kdj_max}', '{rsi_min}', '{rsi_max}', '{profit}')""" \
            .format(m5=im5, m10=im10, kdj_min=ikdj_min, kdj_max=ikdj_max, rsi_min=irsi_min, rsi_max=irsi_max,
                    profit=profit)
        SqlUtil.insert(sql)
    return profit


def main():
    symbol = 'BNB/USDT'
    timeframe = '15m'
    df = fetch_ohlcv('okx', symbol, timeframe)
    m5 = 3
    m10 = 10
    kdj_min = 20
    kdj_max = 90
    rsi_min = 20
    rsi_max = 90
    for im5 in range(m5, int(math.sqrt(m10) + 3)):
        for im10 in range(int(math.sqrt(m10)) + 3, m10):
            for ikdj_min in range(int(math.sqrt(kdj_max)), kdj_min):
                for ikdj_max in range(int(math.sqrt(kdj_max)) + 10, kdj_max):
                    for irsi_min in range(int(math.sqrt(rsi_max)), rsi_min):
                        for irsi_max in range(int(math.sqrt(rsi_max)) + 10, rsi_max):
                            print(im5, im10, ikdj_min, ikdj_max, irsi_min, irsi_max)
                            while threading.active_count() > 2000:
                                print("线程池已满，休眠 10 秒")
                                time.sleep(5)
                            print("=========================》线程池数量：",threading.active_count())
                            thread = threading.Thread(target=backtest, args=(df.copy(), im5, im10, ikdj_min, ikdj_max, irsi_min, irsi_max))
                            thread.start()


if __name__ == "__main__":
    main()