使用CCXT对接okx，然后利用BackTrader回测；
每个信号都用ta-lib单独标注，同时满足才可开平仓
策略逻辑：
金叉：五日均线向上突破十日均线，买入信号
死叉：五日均线向下突破十日均线，卖出信号

K在20向上交叉D时买入信号，K在80向下交叉D时卖出信号

买入信号：当RSI低于30（超卖）且价格上穿其短期MA时，可能是一个买入信号。
卖出信号：当RSI高于70（超买）且价格下穿其短期MA时，可能是一个卖出信号

当 VPT 指标从下向上突破其 7 日移动平均线，且价格在短期均线之上，产生买入信号。
当 VPT 指标从上向下突破其 7 日移动平均线，且价格在短期均线之下，产生卖出信号。

当价格突破上轨时，可能是价格加速上涨的信号，可作为买入信号；但也可能是超买信号，需要结合其他指标进一步确认。
当价格跌破下轨时，可能是价格加速下跌的信号，可作为卖出信号；但也可能是超卖信号，需要结合其他指标进一步确认。