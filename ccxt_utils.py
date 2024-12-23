import ccxt
import pandas as pd


def fetch_ohlcv(exchange_id, symbol, timeframe, limit=2000):
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

    all_ohlcv = []
    fetched = 0
    since = None
    while fetched < limit:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=min(limit, limit - fetched), since=since)
        if not ohlcv:
            break
        all_ohlcv.extend(ohlcv)
        fetched += len(ohlcv)
        # 获取最后一个时间戳，用于下一次调用的 since 参数
        since = ohlcv[-1][0] + 1
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    print(len(df))
    return df


def place_order(exchange, symbol, side, amount=None, price=None):
    if side == 'buy':
        if amount is None:
            # 计算能购买的最大数量
            balance = exchange.fetch_balance()
            print(balance)
            usdt_balance = balance['total']['USDT']
            ticker = exchange.fetch_ticker(symbol)
            price = ticker['ask'] if 'ask' in ticker else ticker['close']
            amount = usdt_balance / price
        order = exchange.create_market_buy_order(symbol, amount)
    elif side == 'sell':
        if amount is not None:
            # balance = exchange.fetch_balance()
            # print(balance)
            # btc_balance = balance['total']['BTC']
            order = exchange.create_market_sell_order(symbol, amount)
    return order
symbol = 'DOGE/USDT'
timeframe = '15m'
df = fetch_ohlcv('okx', symbol, timeframe)
print(df)