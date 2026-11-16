#!/usr/bin/env python3
"""E2 phase-1 gate: dimension check of the exchange 份额 panel.

Ground truth = 天天基金 f10 规模变动 (quarterly, official source): 期末总份额(亿份) and 期末净资产(亿元).
Two independent tests per ETF and quarter:
  A) our exchange 份额 at the quarter-end trading day  vs  期末总份额      (ratio -> 1)
  B) our 份额 x unit NAV                             vs  期末净资产      (ratio -> 1)
If the unit were 万份 / 千份 the ratio would be off by 1e4 / 1e3, and mis-scaled or mis-joined
data would show up as random ratios.

Usage: python3 quant2/e2/check_dim.py --codes 510300,159928,513100
"""
import argparse, json, os, re

import numpy as np
import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_shares():
    sse = {}
    for f in sorted(os.listdir(os.path.join(ROOT, "data", "shares", "sse"))):
        if f.endswith(".json"):
            sse[f[:-5]] = json.load(open(os.path.join(ROOT, "data", "shares", "sse", f)))
    sh = pd.DataFrame(sse).T
    sh.index = pd.to_datetime(sh.index)
    szr = {}
    for f in sorted(os.listdir(os.path.join(ROOT, "data", "shares", "szse"))):
        if f.endswith(".json"):
            for dt, vals in json.load(open(os.path.join(ROOT, "data", "shares", "szse", f))).items():
                szr.setdefault(dt, {}).update(vals)
    sz = pd.DataFrame(szr).T
    sz.index = pd.to_datetime(sz.index)
    return sh.sort_index(), sz.sort_index()


def unit_nav(code):
    bare = code[2:] if code[:2] in ("sh", "sz") else code
    p = os.path.join(ROOT, "data", "nav", f"{bare}.json")
    d = json.load(open(p))
    rows = [(r[0], float(r[1])) for r in d.get("rows", []) if r[1]]
    s = pd.Series(dict(rows))
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def em_gmbd(code, retries=4):
    import time
    H = {"User-Agent": "Mozilla/5.0", "Referer": "https://fundf10.eastmoney.com/"}
    txt = None
    for k in range(retries):
        try:
            txt = requests.get(f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=gmbd&code={code}&rt=0.1",
                               headers=H, timeout=20).text
            break
        except Exception:
            time.sleep(1 + k)
    if not txt:
        return pd.DataFrame()
    rows = re.findall(r"<tr><td>(\d{4}-\d{2}-\d{2})</td>(.*?)</tr>", txt)
    out = []
    for dt, rest in rows:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", rest)
        vals = [re.sub(r"[,\s]", "", x) for x in tds]
        if len(vals) >= 4:
            def num(x):
                try:
                    return float(x)
                except Exception:
                    return np.nan
            out.append({"date": dt, "sub": num(vals[0]), "red": num(vals[1]),
                        "shares_yi": num(vals[2]), "netasset_yi": num(vals[3])})
    return pd.DataFrame(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="510300,159928,513100")
    a = ap.parse_args()
    sh, sz = load_shares()
    res = []
    for code in a.codes.split(","):
        panel = sh if code[0] == "5" or code.startswith("5") else sz
        col = code[2:] if code[0] == "5" else code
        col = code
        if col not in panel.columns:
            col = code[2:]
        if col not in panel.columns:
            print(f"{code}: NOT in shares panel"); continue
        s = panel[col].dropna()
        nav = unit_nav(code)
        g = em_gmbd(code)
        if g.empty:
            print(f"{code}: f10 empty"); continue
        for _, r in g.iterrows():
            d = pd.Timestamp(r["date"])
            past = s.index[s.index <= d]
            if len(past) == 0:
                continue
            dd = past[-1]
            ours = s.loc[dd]
            navd = nav.index[nav.index <= d]
            nv = nav.loc[navd[-1]] if len(navd) else np.nan
            res.append({"code": code, "qdate": r["date"], "our_date": dd.date(),
                        "our_shares_yi": ours / 1e8, "f10_shares_yi": r["shares_yi"],
                        "ratio_sh": ours / 1e8 / r["shares_yi"] if r["shares_yi"] else np.nan,
                        "our_nav": nv, "f10_netasset_yi": r["netasset_yi"],
                        "our_netasset_yi": ours * nv / 1e8 if nv == nv else np.nan,
                        "ratio_na": (ours * nv / 1e8) / r["netasset_yi"] if r["netasset_yi"] else np.nan})
    df = pd.DataFrame(res)
    pd.set_option("display.width", 200)
    print(df.tail(24).to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\n--- summary ---")
    print("share ratio: mean %.4f  median %.4f  min %.4f  max %.4f  n=%d  |err|>3%%: %d" % (
        df.ratio_sh.mean(), df.ratio_sh.median(), df.ratio_sh.min(), df.ratio_sh.max(),
        df.ratio_sh.notna().sum(), (df.ratio_sh.sub(1).abs() > 0.03).sum()))
    print("netasset ratio: mean %.4f median %.4f min %.4f max %.4f |err|>3%%: %d" % (
        df.ratio_na.mean(), df.ratio_na.median(), df.ratio_na.min(), df.ratio_na.max(),
        (df.ratio_na.sub(1).abs() > 0.03).sum()))
    os.makedirs(os.path.join(ROOT, "out", "e2"), exist_ok=True)
    df.to_csv(os.path.join(ROOT, "out", "e2", "dim_check.csv"), index=False)


if __name__ == "__main__":
    main()
