#!/usr/bin/env python3
"""研究用统一数据缓存: 腾讯前复权(qfq) 分页拉取 2017-01-01 → 最新, 落盘 CSV.
所有实验共用这一份数据, 保证口径一致。取不到的标的直接记录并排除。
"""
import json, os, sys, time, urllib.request
import pandas as pd

H = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kline")
os.makedirs(OUT, exist_ok=True)
BEGIN = "2017-01-01"
END = "2026-11-14"


def sid_of(code):
    return ("sh" if code.startswith("5") else "sz") + code


def _get(sid, start, end):
    url = (f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={sid},day,{start},{end},640,qfq")
    req = urllib.request.Request(url, headers=H)
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8"))
    data = d.get("data", {})
    key = None
    for k in (sid, sid.upper(), sid.lower()):
        if k in data:
            key = k
            break
    if not key:
        return []
    arr = data[key].get("qfqday") or data[key].get("day") or []
    rows = []
    for a in arr:
        if len(a) < 6:
            continue
        rows.append({"date": a[0], "open": float(a[1]), "close": float(a[2]),
                     "high": float(a[3]), "low": float(a[4]), "volume": float(a[5])})
    return rows


def fetch_all(code, begin=BEGIN, end=END):
    sid = sid_of(code)
    chunks = {}
    cur_end = pd.Timestamp(end)
    for _ in range(12):
        rows = _get(sid, begin, cur_end.strftime("%Y-%m-%d"))
        if not rows:
            break
        new = 0
        for r in rows:
            if r["date"] not in chunks:
                chunks[r["date"]] = r
                new += 1
        first = min(chunks)
        if new == 0 or pd.Timestamp(first) <= pd.Timestamp(begin):
            break
        cur_end = pd.Timestamp(first) - pd.Timedelta(days=1)
        if cur_end <= pd.Timestamp(begin):
            break
    if not chunks:
        return None
    df = pd.DataFrame(sorted(chunks.values(), key=lambda r: r["date"]))
    df["date"] = pd.to_datetime(df["date"])
    return df


UNIVERSE = {
    # 现有候选池 16 + 默认 4
    "159995": "芯片ETF", "588000": "科创50ETF", "159819": "AI龙头ETF", "159770": "机器人ETF",
    "515880": "通信ETF", "512660": "军工ETF", "515790": "光伏ETF", "512880": "证券ETF",
    "512800": "银行ETF", "513780": "港股创新药ETF", "159516": "半导体设备ETF", "515220": "煤炭ETF",
    "518880": "黄金ETF", "159928": "消费ETF", "513130": "恒生科技ETF", "513300": "纳斯达克ETF",
    "159981": "能源化工ETF", "159611": "电力ETF", "515050": "中证全指ETF", "588170": "科创半导体ETF",
    # 宽基
    "510300": "沪深300ETF", "510500": "中证500ETF", "159915": "创业板ETF", "510880": "红利ETF",
    # 跨境
    "513030": "德国30ETF", "164824": "印度基金LOF", "513520": "日经225ETF", "159866": "日经ETF",
    "513050": "中概互联网ETF", "513180": "恒生科技指数ETF", "159920": "恒生ETF", "513100": "纳指ETF",
    "513500": "标普500ETF", "159941": "纳指LOF",
    # 商品
    "162411": "华宝油气LOF", "501018": "南方原油LOF", "159980": "有色ETF", "159985": "豆粕ETF",
    "161129": "原油基金LOF", "161226": "国投白银LOF",
    # 债券/货币
    "511010": "国债ETF", "511260": "十年国债ETF", "511360": "短融ETF", "511880": "银华日利",
    "511990": "华宝添益", "159649": "国开债ETF", "511020": "活跃国债ETF",
    # 港股/海外补充
    "513660": "恒生中国企业ETF", "159740": "恒生科技ETF(华夏)", "513330": "恒生互联网ETF",
    # 其他低相关
    "512400": "有色金属ETF", "159949": "创业板50ETF", "512890": "红利低波ETF",
    "159532": "中证2000ETF", "512010": "医药ETF", "515000": "科技ETF", "516160": "新能源ETF",
    "511380": "转债ETF", "159611X": "",
}

if __name__ == "__main__":
    codes = [c for c in UNIVERSE if "X" not in c]
    report = []
    for code in codes:
        path = os.path.join(OUT, f"{code}.csv")
        if os.path.exists(path):
            df = pd.read_csv(path, parse_dates=["date"])
        else:
            try:
                df = fetch_all(code)
            except Exception as e:
                df = None
                print(f"{code} {UNIVERSE[code]} FETCH-ERR {e}")
            if df is None or df.empty:
                print(f"{code} {UNIVERSE[code]} NO-DATA")
                report.append({"code": code, "name": UNIVERSE[code], "bars": 0})
                continue
            df.to_csv(path, index=False)
            time.sleep(0.15)
        report.append({"code": code, "name": UNIVERSE[code], "bars": len(df),
                       "first": str(df["date"].iloc[0].date()), "last": str(df["date"].iloc[-1].date())})
        print(f"{code} {UNIVERSE[code]:<14} {len(df):5d} {str(df['date'].iloc[0].date())} → {str(df['date'].iloc[-1].date())}")
    pd.DataFrame(report).to_csv(os.path.join(OUT, "_coverage.csv"), index=False)
