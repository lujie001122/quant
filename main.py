# TA-Lib  量化指标
# CCXT  实盘框架
# BackTrader 回测策略
# 股票数据


import ccxt
# os.environ["http_proxy"] = "http://127.0.0.1:10808"
# os.environ["https_proxy"] = "http://127.0.0.1:10808"

apikey = "d3b3d339-cb05-44d9-a179-a443e2e832d8"
secretkey = "2EB8F8730B211394F3464752A5D4D791"
passphrase = "Qazwsx12@"
# 创建 OKEx 交易所对象，并设置为模拟交易环境
okx = ccxt.okx({
    'apiKey': apikey,  # 你的 API Key
    'secret': secretkey,    # 你的 Secret
    'password': passphrase,  # 你的 API 密码（如果有）
    'enableRateLimit': True,  # 启用速率限制

    'proxies':{
        'http': 'socks5://127.0.0.1:10808',
        'https': 'socks5h://127.0.0.1:10808',
    }
})
okx.setSandboxMode(True)

# okx.proxies={
#     'http': 'socks5://127.0.0.1:10809',
#     'https': 'socks5h://127.0.0.1:10809',
# }
# print(okx.load_markets())
print(okx.fetchBalance())