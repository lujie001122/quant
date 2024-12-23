import backtrader as bt

class SignalData(bt.feeds.PandasData):
    lines = ('golden_cross','dead_cross','rsi_signal','kdj_signal')
    params = (
          ('golden_cross', -1),
          ('dead_cross', -2),
          ('rsi_signal', -3),
          ('kdj_signal', -4),
        )
class M5RsiStrategy(bt.Strategy):
    def __init__(self):
        self.golden_cross = self.datas[0].golden_cross
        self.dead_cross = self.datas[0].dead_cross
        self.rsi_signal = self.datas[0].rsi_signal
        self.kdj_signal = self.datas[0].kdj_signal

    def next(self):
        # 获取当前数据的收盘价
        close_price = self.data.close[0]
        # 检查是否有持仓
        # K 在 20 向上交叉 D 时买入信号
        if not self.position and (self.rsi_signal[0]>0 and self.golden_cross[0]>0 ) or (self.rsi_signal[0]>0 and self.kdj_signal[0]>0)  :
            # 获取当前可用现金
            cash = self.broker.getcash() * 0.8
            # 计算可购买的比特币数量，考虑到可能有小数精度问题，使用 round 函数
            size = round(cash / close_price, 8)
            self.order = self.buy(size=size)

        if  self.position and (self.dead_cross[0] > 0  or   self.kdj_signal[0] < 0 or self.rsi_signal[0] < 0 )  :
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

            # 其他状态记录为：无挂起订单
        self.order = None
        # 交易状态通知，一买一卖算交易

    # 交易状态通知，一买一卖算交易
    def notify_trade(self, trade):
        if not trade.isclosed:
            return
        self.log('交易利润, 毛利润 %.2f, 净利润 %.2f' %
        (trade.pnl, trade.pnlcomm))