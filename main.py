import okx.MarketData as MarketData
import requests


# 定义代理服务器信息
proxies = {
    'http': 'http://127.0.0.1:10809',
    'https': 'https://127.0.0.1:10809',
}


flag = "0"  # 实盘:0, 模拟盘：1



# 传递会话给 MarketAPI
marketDataAPI = MarketData.MarketAPI(flag=flag, proxy=proxies)


# 获取交易产品 K 线数据
result = marketDataAPI.get_candlesticks(
    instId="BTC-USDT"
)
print(result)