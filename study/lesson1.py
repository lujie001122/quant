import akshare as ak
import pandas as pd
import backtrader as bt

# 从akshare获取上证指数每日数据
from backtrader import talib

sh_index = ak.stock_zh_index_daily(symbol="sh600900" )
# 添加一个'openinterest'列，Backtrader需要这个列，但很多数据源不提供，这里可以设为0
sh_index['openinterest'] = 0
# 将日期列转换为datetime类型，并设置为DataFrame的索引
sh_index.index = pd.to_datetime(sh_index.date)
# 重新排序DataFrame的列，以匹配Backtrader的要求
sh_index = sh_index[['open', 'high', 'low', 'close', 'volume', 'openinterest']]

def get_stock_data(stock_code, start_date, end_date):
    # 使用akshare获取股票数据
    df = ak.stock_zh_a_hist(symbol=stock_code, period="daily", start_date=start_date, end_date=end_date, adjust="hfq")
    return df


def prepare_data(stock_zh_a_hist_df):

    data_dict = {
        'datetime': stock_zh_a_hist_df['日期'].apply(lambda x: pd.to_datetime(x)),
        'open': stock_zh_a_hist_df['开盘'],
        'high': stock_zh_a_hist_df['最高'],
        'low': stock_zh_a_hist_df['最低'],
        'close': stock_zh_a_hist_df['收盘'],
        'volume': stock_zh_a_hist_df['成交量'],
        'openinterest': -1,  # 如果没有持仓量数据，可以设为-1或None
    }
    # 创建一个新的DataFrame
    data_for_backtrader = pd.DataFrame(data_dict)

    # 设置datetime列为索引
    data_for_backtrader.set_index('datetime', inplace=True)
    return data_for_backtrader

# 双均线策略
class DoubleMA_Strategy(bt.Strategy):
    params = (("sma5", 5), ("sma20", 10))  # 短期和长期均线的周期

    def __init__(self):
        print(self.datas[0])
        # 用于保存订单
        self.order = None
        # 计算MA5和MA20
        self.sma5 = bt.ind.MovingAverageSimple(self.data.close, period=self.params.sma5)
        self.sma20 = bt.ind.MovingAverageSimple(self.data.close, period=self.params.sma20)
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
                    '已买入, 价格: %.2f, 费用: %.2f, 佣金 %.2f' %
                    (order.executed.price,
                     order.executed.value,
                     order.executed.comm))
                self.buyprice = order.executed.price
                self.buycomm = order.executed.comm
            elif order.issell():
                self.log('已卖出, 价格: %.2f, 费用: %.2f, 佣金 %.2f' %
                         (order.executed.price,
                          order.executed.value,
                          order.executed.comm))
                # 记录当前交易数量
            self.bar_executed = len(self)
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('订单取消/保证金不足/拒绝')

            # 其他状态记录为：无挂起订单
        self.order = None
        # 交易状态通知，一买一卖算交易

    # 交易状态通知，一买一卖算交易
    def notify_trade(self, trade):
        if not trade.isclosed:
            return
        self.log('交易利润, 毛利润 %.2f, 净利润 %.2f' %
        (trade.pnl, trade.pnlcomm))
    def next(self):
        # 当MA5上穿MA20时买入
        if self.sma5[0] > self.sma20[0] and self.sma5[-1] <= self.sma20[-1]:
            if not self.position:  # 如果没有持仓，则买入
                self.order = self.order_target_percent(target=0.9)
        # 当MA5下穿MA20时卖出
        elif self.sma5[0] < self.sma20[0] and self.sma5[-1] >= self.sma20[-1]:
            if self.position:  # 如果有持仓，则卖出
                self.order = self.order_target_percent(target=0)

    def stop(self):
        self.log('期末资金 %.2f' %
                 (self.broker.getvalue()))
    # 初始化回测系统
cerebro = bt.Cerebro()
stock_code = "000001"
start_date = "20200101"
end_date = "20241213"
df = get_stock_data(stock_code, start_date, end_date)
df = prepare_data(df)

# 添加数据
data = bt.feeds.PandasData(dataname=df, fromdate=pd.to_datetime("2022-01-01"), todate=pd.to_datetime("2024-12-11"))
cerebro.adddata(data)

# 添加策略
cerebro.addstrategy(DoubleMA_Strategy)

# 设置初始资金
start_cash = 100000.0
cerebro.broker.setcash(start_cash)
cerebro.addsizer(bt.sizers.FixedSize, stake=10)
# 设置手续费（双边各0.03%）
cerebro.broker.setcommission(commission=0.0006)  # 0.03% * 2 = 0.06%, 转换为小数形式为0.0006

# 运行回测
cerebro.run()

# 输出回测结果
print('初始资金: %.2f' % start_cash)
print('回测结束后的总资金: %.2f' % cerebro.broker.getvalue())
print('净收益: %.2f' % (cerebro.broker.getvalue() - start_cash))

# 画图
cerebro.plot()