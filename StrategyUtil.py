import numpy as np
import talib


def formatStrategyPandas(df,m5=5,m10=10,kdj_min=20,kdj_max=80,rsi_min=30,rsi_max=70):
    # 计算 KD 指标
    high = np.array(df['high'])
    low = np.array(df['low'])
    close = np.array(df['close'])
    k, d = talib.STOCH(high, low, close, fastk_period=9, slowk_period=3, slowd_period=3)

    # 初始化信号列
    df['kdj_signal'] = 0
    # 生成信号
    for i in range(1, len(df)):
        if k[i] > d[i] and k[i - 1] <= d[i - 1] and k[i] > kdj_min:
            df.loc[df.index[i], 'kdj_signal'] = 1
        elif k[i] < d[i] and k[i - 1] >= d[i - 1] and k[i] < kdj_max:
            df.loc[df.index[i], 'kdj_signal'] = -1
    # 计算 RSI 指标
    df['rsi_signal'] = 0
    close_prices = np.array(df['close'])
    rsi_values = talib.RSI(close_prices, timeperiod=14)
    for i in range(1, len(df)):
        if rsi_values[i]>rsi_min and rsi_values[i] < rsi_max:
            df.loc[df.index[i], 'rsi_signal'] = 1
        else:
            df.loc[df.index[i], 'rsi_signal'] = -1
    #m5,m10 金叉死叉
    # 计算五日和十日均线
    df['sma5'] = df['close'].rolling(window=m5).mean()
    df['sma10'] = df['close'].rolling(window=m10).mean()

    # 计算金叉死叉信号
    df['golden_cross'] = np.where((df['sma5'] > df['sma10']) & (df['sma5'].shift(1) <= df['sma10'].shift(1)), True,
                                  False)
    df['dead_cross'] = np.where((df['sma5'] < df['sma10']) & (df['sma5'].shift(1) >= df['sma10'].shift(1)), True, False)

    return df