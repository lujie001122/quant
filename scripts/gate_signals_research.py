#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
反转确认信号研究脚本（只读数据，不碰策略）
构造四个日频 0/1 信号，评估其对后续收益的预测力。
时间范围: 2024-07-01 ~ 2026-10-27

S1 两融止跌:     沪市融资余额20日线性斜率>=0 → 1 (T+1公布,数据后移1天)
S2 红利拥挤缓解: 512800 20日涨幅 - 510300 20日涨幅 <= 0 → 1
S3 分母端松动:   美债10年20日变化<=0 或 原油20日跌幅>5% → 1 (二者取或)
S4 ETF放量承接:  池内ETF中 量比>1.2且收盘>MA5 的只数>=3 → 1
"""

import sys, os, json, warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

REPO = "/Users/lujie/Documents/code/quant"
sys.path.insert(0, REPO)

import akshare as ak
from market_data import fetch_klines_daily

ANALYSIS_START = "2024-07-01"
ANALYSIS_END   = "2026-10-27"
FETCH_START    = "20231101"
FETCH_END      = "20261027"
WINDOW         = 20

ETF_POOL_FILE  = os.path.join(REPO, "etf_pool.json")

# ==================== 辅助函数 ====================

def _find_col(df, keywords):
    """在列名中做 AND 模糊匹配"""
    for c in df.columns:
        cs = str(c)
        if all(kw in cs for kw in keywords):
            return c
    return None

def _find_date_col(df):
    for c in df.columns:
        cs = str(c)
        if "日期" in cs or "date" in cs.lower() or "time" in cs.lower():
            return c
    return df.columns[0]

# ==================== 数据获取 ====================

def fetch_etf_ohlc(code):
    """用本地 market_data 取前复权日K, 返回 (close, volume) 两个 Series"""
    try:
        klines = fetch_klines_daily(code, start=FETCH_START, end="20261231", adjust="qfq")
        if not klines:
            print(f"  [WARN] {code} 返回空数据")
            return pd.Series(dtype=float), pd.Series(dtype=float)
        df = pd.DataFrame(klines)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        return df["close"].astype(float), df["volume"].astype(float)
    except Exception as e:
        print(f"  [WARN] {code} 行情获取失败: {e}")
        return pd.Series(dtype=float), pd.Series(dtype=float)

def fetch_margin_sse():
    """沪市融资余额"""
    try:
        df = ak.stock_margin_sse(start_date="20231101", end_date=FETCH_END)
        dcol = _find_date_col(df)
        df[dcol] = pd.to_datetime(df[dcol])
        df = df.set_index(dcol).sort_index()
        mcol = _find_col(df, ["融资余额"])
        if mcol is None:
            print("  [WARN] 两融: 未找到融资余额列, 列名:", list(df.columns))
            return pd.Series(dtype=float)
        return df[mcol].astype(float)
    except Exception as e:
        print(f"  [WARN] 沪市两融数据获取失败: {e}")
        return pd.Series(dtype=float)

def fetch_us_10y():
    """美国10年期国债收益率"""
    try:
        try:
            df = ak.bond_zh_us_rate(start_date="20231101", end_date=FETCH_END)
        except TypeError:
            df = ak.bond_zh_us_rate()
        dcol = _find_date_col(df)
        df[dcol] = pd.to_datetime(df[dcol])
        df = df.set_index(dcol).sort_index()
        col = _find_col(df, ["美国", "10"])
        if col is None:
            for c in df.columns:
                if "10" in str(c) and ("美" in str(c) or "US" in str(c).upper()):
                    col = c
                    break
        if col is None:
            print("  [WARN] 美债: 未找到10年期列, 列名:", list(df.columns))
            return pd.Series(dtype=float)
        return df[col].astype(float)
    except Exception as e:
        print(f"  [WARN] 美债数据获取失败: {e}")
        return pd.Series(dtype=float)

def fetch_crude_oil():
    """WTI原油期货收盘价"""
    try:
        df = ak.futures_foreign_hist(symbol="CL")
        dcol = _find_date_col(df)
        df[dcol] = pd.to_datetime(df[dcol])
        df = df.set_index(dcol).sort_index()
        col = _find_col(df, ["收盘"])
        if col is None:
            col = _find_col(df, ["close"])
        if col is None:
            col = df.columns[-1]
        return df[col].astype(float)
    except Exception as e:
        print(f"  [WARN] 原油数据获取失败: {e}")
        return pd.Series(dtype=float)

# ==================== 信号计算 ====================

def linear_slope(values):
    """线性回归斜率"""
    v = np.asarray(values, dtype=float)
    if len(v) < 2 or np.isnan(v).any():
        return np.nan
    x = np.arange(len(v), dtype=float)
    return np.polyfit(x, v, 1)[0]

def compute_s1(margin_raw, calendar):
    """S1 两融止跌: 沪市融资余额20日线性斜率>=0 → 1
    两融T+1公布, 数据后移1个交易日, 使T日信号只用T-1及以前数据"""
    m = margin_raw.reindex(calendar)
    m = m.shift(1)               # T+1 后移
    s1 = pd.Series(np.nan, index=calendar, dtype=float)
    vals = m.values
    for i in range(WINDOW, len(calendar)):
        window = vals[i - WINDOW + 1: i + 1]   # shift后对应 margin[i-20..i-1]
        slope = linear_slope(window)
        if not np.isnan(slope):
            s1.iloc[i] = 1.0 if slope >= 0 else 0.0
    return s1

def compute_s2(close_512800, close_510300, calendar):
    """S2 红利拥挤缓解: 512800 20日涨幅 - 510300 20日涨幅 <= 0 → 1"""
    cb = close_512800.reindex(calendar)
    c3 = close_510300.reindex(calendar)
    s2 = pd.Series(np.nan, index=calendar, dtype=float)
    for i in range(WINDOW, len(calendar)):
        b0, b1 = cb.iloc[i - WINDOW], cb.iloc[i]
        s0, s1v = c3.iloc[i - WINDOW], c3.iloc[i]
        if (np.isnan(b0) or np.isnan(b1) or np.isnan(s0) or np.isnan(s1v)
                or b0 == 0 or s0 == 0):
            continue
        ret_bank = b1 / b0 - 1
        ret_300  = s1v / s0 - 1
        diff = ret_bank - ret_300
        s2.iloc[i] = 1.0 if diff <= 0 else 0.0
    return s2

def compute_s3(us10y_raw, oil_raw, calendar):
    """S3 分母端松动: 美债10年20日变化<=0 或 原油20日跌幅>5% → 1"""
    u = us10y_raw.reindex(calendar).ffill()
    o = oil_raw.reindex(calendar).ffill()
    s3 = pd.Series(np.nan, index=calendar, dtype=float)
    for i in range(WINDOW, len(calendar)):
        u_ok = (not np.isnan(u.iloc[i]) and not np.isnan(u.iloc[i - WINDOW]))
        o_ok = (not np.isnan(o.iloc[i]) and not np.isnan(o.iloc[i - WINDOW])
                and o.iloc[i - WINDOW] != 0)
        if not (u_ok or o_ok):
            continue
        flag = 0
        if u_ok and (u.iloc[i] - u.iloc[i - WINDOW]) <= 0:
            flag = 1
        if o_ok:
            oil_ret = o.iloc[i] / o.iloc[i - WINDOW] - 1
            if oil_ret < -0.05:
                flag = 1
        s3.iloc[i] = float(flag)
    return s3

def compute_s4(pool_closes, pool_vols, calendar):
    """S4 ETF放量承接: 池内ETF中 量比>1.2 且 收盘>MA5 的只数>=3 → 1
    量比 = 当日量 / 前20日均量(不含当日)"""
    s4 = pd.Series(np.nan, index=calendar, dtype=float)
    ac = {code: c.reindex(calendar) for code, c in pool_closes.items()}
    av = {code: v.reindex(calendar) for code, v in pool_vols.items()}
    for i in range(WINDOW, len(calendar)):
        count = 0
        available = 0
        for code in ac:
            c = ac[code]
            v = av[code]
            if (np.isnan(c.iloc[i]) or np.isnan(v.iloc[i])
                    or np.isnan(v.iloc[i - WINDOW: i].values).any()
                    or np.isnan(c.iloc[i - 4: i + 1].values).any()):
                continue
            available += 1
            ma20_vol = v.iloc[i - WINDOW: i].mean()
            if ma20_vol <= 0:
                continue
            vol_ratio = v.iloc[i] / ma20_vol
            ma5 = c.iloc[i - 4: i + 1].mean()
            if vol_ratio > 1.2 and c.iloc[i] > ma5:
                count += 1
        if available > 0:
            s4.iloc[i] = 1.0 if count >= 3 else 0.0
    return s4

# ==================== 主流程 ====================

def main():
    with open(ETF_POOL_FILE, "r", encoding="utf-8") as f:
        pool_cfg = json.load(f)
    etf_pool = pool_cfg.get("etf_pool", [])

    print("=" * 62)
    print("  反转确认信号研究（只读数据，不碰策略）")
    print("=" * 62)
    print(f"分析区间: {ANALYSIS_START} ~ {ANALYSIS_END}")
    print(f"ETF池: {etf_pool}")
    print()

    # --- 获取 510300 (主日历 + 前瞻收益基准) ---
    print("[1/6] 获取沪深300ETF(510300)日K ...")
    close_300, _ = fetch_etf_ohlc("510300")
    calendar = close_300.index
    print(f"  交易日数: {len(calendar)}")
    print(f"  日历范围: {calendar[0].date()} ~ {calendar[-1].date()}")

    # --- S1: 两融 ---
    print("[2/6] 获取沪市融资余额 ...")
    margin = fetch_margin_sse()
    print(f"  两融数据点: {len(margin)}")

    # --- S2: 512800 ---
    print("[3/6] 获取银行ETF(512800)日K ...")
    close_512800, _ = fetch_etf_ohlc("512800")
    print(f"  512800 数据点: {len(close_512800)}")

    # --- S3: 美债 + 原油 ---
    print("[4/6] 获取美债10年收益率 & 原油期货 ...")
    us10y = fetch_us_10y()
    print(f"  美债数据点: {len(us10y)}")
    oil = fetch_crude_oil()
    print(f"  原油数据点: {len(oil)}")

    # --- S4: ETF 池 ---
    print("[5/6] 获取ETF池行情 ...")
    pool_c, pool_v = {}, {}
    for code in etf_pool:
        c, v = fetch_etf_ohlc(code)
        pool_c[code] = c
        pool_v[code] = v
        print(f"  {code}: {len(c)} 条")

    # --- 计算信号 ---
    print("[6/6] 计算四个信号 ...")
    s1 = compute_s1(margin, calendar)
    s2 = compute_s2(close_512800, close_300, calendar)
    s3 = compute_s3(us10y, oil, calendar)
    s4 = compute_s4(pool_c, pool_v, calendar)

    signals = pd.DataFrame({"S1": s1, "S2": s2, "S3": s3, "S4": s4}, index=calendar)

    # 过滤到分析区间
    mask = (calendar >= pd.Timestamp(ANALYSIS_START)) & (calendar <= pd.Timestamp(ANALYSIS_END))
    sc = signals.loc[mask].copy()
    total_days = len(sc)

    # 总分 (4个信号齐全才计算)
    sc["total"] = sc[["S1", "S2", "S3", "S4"]].sum(axis=1, skipna=False)

    # ── 导出每日信号 CSV (供回测 gate score 使用) ──
    _export_path = "/tmp/gate_score.csv"
    _exp = sc[["S1", "S2", "S3", "S4"]].copy().fillna(0).astype(int)
    _exp["total"] = _exp.sum(axis=1)
    _exp.index = _exp.index.strftime("%Y-%m-%d")
    _exp.to_csv(_export_path, index_label="date")
    print(f"  [导出] gate_score.csv: {len(_exp)} 行 → {_export_path}")

    # --- 前瞻收益 (510300) ---
    close_arr = close_300.reindex(calendar).values
    n = len(close_arr)
    fwd = {}
    for k in [5, 10, 20]:
        arr = np.full(n, np.nan)
        for i in range(n - k):
            if close_arr[i] > 0 and not np.isnan(close_arr[i]) and not np.isnan(close_arr[i + k]):
                arr[i] = close_arr[i + k] / close_arr[i] - 1
        fwd[k] = pd.Series(arr, index=calendar)
    fwd_cut = {k: fwd[k].loc[mask] for k in fwd}

    # ==================== 输出统计 ====================
    print()
    print("=" * 62)
    print("  统计结果")
    print("=" * 62)

    # ---- 1. 各信号触发频率 ----
    print()
    print("【1】各信号触发频率 (1 的比例)")
    print("-" * 48)
    print(f"{'信号':<10} {'触发天数':>8} {'有效天数':>8} {'触发频率':>8}")
    for col in ["S1", "S2", "S3", "S4"]:
        valid = sc[col].notna()
        trig = int((sc[col] == 1).sum())
        nvalid = int(valid.sum())
        freq = trig / nvalid * 100 if nvalid else 0
        print(f"{col:<10} {trig:>8} {nvalid:>8} {freq:>7.1f}%")

    # ---- 2. 总分分布 ----
    print()
    print("【2】总分分布 (交易日数)")
    print("-" * 48)
    tv = sc["total"].notna()
    for s in range(5):
        cnt = int((sc["total"] == s).sum())
        pct = cnt / int(tv.sum()) * 100 if tv.sum() else 0
        print(f"  总分={s}: {cnt:>5} 天 ({pct:>5.1f}%)")
    print(f"  有效天数(4信号齐全): {int(tv.sum())}")
    print(f"  缺失天数(任一信号缺失): {total_days - int(tv.sum())}")

    # ---- 3. 信号与后续收益关系 ----
    print()
    print("【3】信号与后续 5/10/20 日 510300 收益关系")
    print("-" * 62)
    has_t = sc["total"].notna()
    for label, cond in [("总分>=2", sc["total"] >= 2), ("总分<2", sc["total"] < 2)]:
        sel = has_t & cond
        print(f"\n  [{label}] 样本数={int(sel.sum())}")
        print(f"  {'持有期':<8} {'平均收益':>10} {'胜率':>8} {'样本数':>8}")
        for k in [5, 10, 20]:
            r = fwd_cut[k][sel].dropna()
            if len(r) > 0:
                avg = r.mean() * 100
                wr = (r > 0).sum() / len(r) * 100
                print(f"  {k}日{'':<5} {avg:>9.2f}% {wr:>7.1f}% {len(r):>8}")
            else:
                print(f"  {k}日{'':<5} {'N/A':>10} {'N/A':>8} {0:>8}")

    # 按总分细分
    print()
    print("  --- 按总分 0~4 细分 ---")
    print(f"  {'总分':<6}{'5日均收益':>11}{'10日均收益':>12}{'20日均收益':>12}{'样本数':>8}")
    for s in range(5):
        sel = has_t & (sc["total"] == s)
        parts = []
        for k in [5, 10, 20]:
            r = fwd_cut[k][sel].dropna()
            parts.append(f"{r.mean()*100:>9.2f}%" if len(r) else f"{'N/A':>10}")
        print(f"  {s:<6}{parts[0]:>11}{parts[1]:>12}{parts[2]:>12}{int(sel.sum()):>8}")

    # ---- 4. 总分>=2 连续区间 ----
    print()
    print("【4】总分>=2 连续区间统计")
    print("-" * 48)
    flag = (sc["total"] >= 2).astype(int)
    runs = []
    start = None
    for i in range(len(flag)):
        if flag.iloc[i] == 1 and start is None:
            start = i
        elif flag.iloc[i] == 0 and start is not None:
            runs.append(i - start)
            start = None
    if start is not None:
        runs.append(len(flag) - start)
    if runs:
        print(f"  连续区间数: {len(runs)}")
        print(f"  最长: {max(runs)} 天")
        print(f"  最短: {min(runs)} 天")
        print(f"  平均: {np.mean(runs):.1f} 天")
    else:
        print("  无总分>=2 的连续区间")

    # ---- 5. 数据缺失情况 ----
    print()
    print("【5】数据缺失情况 (每个信号缺数据的天数)")
    print("-" * 48)
    print(f"{'信号':<10} {'缺失天数':>8} {'缺失比例':>8}")
    for col in ["S1", "S2", "S3", "S4"]:
        miss = int(sc[col].isna().sum())
        pct = miss / total_days * 100 if total_days else 0
        print(f"{col:<10} {miss:>8} {pct:>7.1f}%")

    # ---- 一句话结论 ----
    print()
    print("=" * 62)
    sel_hi = has_t & (sc["total"] >= 2)
    sel_lo = has_t & (sc["total"] < 2)
    r20_hi = fwd_cut[20][sel_hi].dropna()
    r20_lo = fwd_cut[20][sel_lo].dropna()
    if len(r20_hi) >= 5 and len(r20_lo) >= 5:
        diff_ret = (r20_hi.mean() - r20_lo.mean()) * 100
        wr_hi = (r20_hi > 0).mean() * 100
        wr_lo = (r20_lo > 0).mean() * 100
        diff_wr = wr_hi - wr_lo
        max_run = max(runs) if runs else 0
        if max_run > 0 and max_run / total_days > 0.4:
            verdict = (f"不值得做门控 — 总分>=2 最长连续{max_run}天, "
                       f"占区间{max_run/total_days*100:.0f}%, 长期开着无区分度")
        elif abs(diff_ret) < 0.5 and abs(diff_wr) < 5:
            verdict = "不值得做门控 — 高低分组20日收益差不足0.5%, 胜率差不足5%"
        elif diff_ret > 1 and diff_wr > 5:
            verdict = (f"有一定预测力 — 20日收益差{diff_ret:.2f}%, 胜率差{diff_wr:.1f}pp, "
                       f"可考虑做门控但需样本外验证")
        else:
            verdict = (f"预测力有限 — 20日收益差{diff_ret:.2f}%, 胜率差{diff_wr:.1f}pp, "
                       f"不建议直接做门控")
    else:
        verdict = "样本不足, 无法判断"
    print(f"结论: {verdict}")
    print("=" * 62)

if __name__ == "__main__":
    main()
