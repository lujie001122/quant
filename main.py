import requests
#获取概念股数据
def getGnbk():
    headers = {
        'accept': '*/*',
        'accept-language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        # 'cookie': 'qgqp_b_id=a6aac145e613aed3e5bf91a087f559d1; mtp=1; uidal=8652345535054280%e8%82%a1%e5%8f%8b80u5337Z70; sid=133502958; vtpst=|; ct=I48OXtJ6rnJfpxp5tYiSMFx-42FDUEsjR0FQMAFsvFX7345SY9lwtGymKV47d1cTF4x8WG8nMm-ATSF1q86jZgW8O8EIi_MEHvjbHRHCin4dpPjrxyv8RKoIf80gTPUoqnU-Y0IHkHHHJaInP62FOZPmWrQd2D2rMrTS39jR1o4; ut=FobyicMgeV7bodPh3F8eYhoe-KRgilxz4DPXo9UkIzr9tIlPp8K-rCIs1MhbYZt41_281IKEiY3NMlGhhhKusJTfx4Bb7awzxl4MfvXzn0kcRlT4cqqpgiuiVJUEE2d6dnF-2MLpkbfKSq_fAyoFqm1TB-pnLDxI_BUYw5He9emoieyzNtwA9pjjUfEZPtghimD8xkU8BlUFkNF8RUDzSQXsjv4fveg6d2_Y2ZRQoRgsz6-jQCiJ0jzuKjO9GIP6R8OcTtFPXXoqxJyGCHC6IA; pi=8652345535054280%3Bo8652345535054280%3B%E8%82%A1%E5%8F%8B80u5337Z70%3BC5Ku1UmhkM30w2PyJYIuISlqQm7gg%2Fn9xQvJNoIXZQ6cxRIftqZcdKH2WpGF9RviufKSIhvKyHmAOR9BUNDwURj%2F6z8BRrlcfzGSWDZo9UWUzb%2FYGlw1JSWHLq5ChXYAtmmj%2FZmsvKMt19lgaz%2FtzuqGeANtfA6lz0CXKvSVHRtRpp2Y6qJ0eLGkXxVH59U9to%2FKFtia%3BifksX802G%2BWFu7fQOY1FZPyRQ8z72vaVxTH25Sl8Gifb27NZpIi5n9qHhJd1tXYNn68nGTEt5fQDXmFW5jC0%2Br3yGD9%2Bx4HRuTBADoT%2FvrCtP95N0M5YmDNO7wF2wZfgEnaAC9JWhfHJGl1%2BotJrf6DIgrsUnQ%3D%3D; emshistory=%5B%22%E6%B8%A4%E6%B5%B7%E7%A7%9F%E8%B5%81%22%5D; st_si=13487525035905; websitepoptg_api_time=1728535971964; HAList=ty-90-BK1139-%u4E2D%u7279%u4F30%2Cty-1-513560-%u9999%u6E2F%u79D1%u6280ETF%2Cty-1-688121-%u5353%u7136%u80A1%u4EFD%2Cty-0-000651-%u683C%u529B%u7535%u5668%2Cty-0-002333-%u7F57%u666E%u65AF%u91D1%2Cty-0-301345-%u6D9B%u6D9B%u8F66%u4E1A%2Cty-1-175907-21%u9996%u7F6E03%2Cty-0-002789-%u5EFA%u827A%u96C6%u56E2%2Cty-0-002188-%u4E2D%u5929%u670D%u52A1%2Cty-0-159767-%u7535%u6C60%u9F99%u5934ETF; st_asi=delete; st_pvi=48498409736388; st_sp=2024-05-24%2009%3A38%3A05; st_inirUrl=https%3A%2F%2Fwww.baidu.com%2Flink; st_sn=70; st_psi=20241010135132228-117001356556-3961399236',
        'referer': 'https://data.eastmoney.com/bkzj/gn_10.html',
        'sec-ch-ua': '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'script',
        'sec-fetch-mode': 'no-cors',
        'sec-fetch-site': 'same-site',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
    }
    params = {
        # 'cb': 'jQuery112304966120647741896_1728453514000',
        'fid': 'f62',
        'po': '1',
        'pz': '50',
        'pn': '1',
        'np': '1',
        'fltt': '2',
        'invt': '2',
        'ut': '',
        'fs': 'm:90 t:3',
        'fields': 'f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124,f1,f13',
    }

    response = requests.get('https://push2.eastmoney.com/api/qt/clist/get', params=params,  headers=headers )
    print(response.text)