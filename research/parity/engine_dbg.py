"""Hunt25 快速结构引擎 — 日频 / 目标权重 / 无杠杆多头。

忠实复刻实盘"集中金字塔"链路的经济结构：
  动量排名 → TOP-N 建仓 / 跌出 TOP2N 清仓 → e1 ATR 分档移动止盈 → 硬止损 → 组合层择时乘数
差异（有意为之，且已量化）：
  实盘 _buy() 用 fund_per_etf*pct 的**固定名义金额**（以初始资金为基数），净值上涨后单笔
  金额不跟着涨 → 实际总仓位随净值被动稀释（全期平均仓位仅 43~55%）。本引擎用**目标权重**
  （资金按当前权益等权分配），才能真正测试"集中度/满仓"这类提高收益的结构。
  两种口径的差异在第 2 节用 parity 模式单独量化。

数据口径：腾讯前复权 CSV（research/kline/*.csv），单边成本 0.1%（与 config slippage_pct 一致）。
"""
import os
import numpy as np
import pandas as pd

COST = 0.001  # 单边成本


class Data:
    """对齐后的价格矩阵 + 预计算指标。"""

    def __init__(self, codes, dirpath, index="510300", extra=("511010", "511260", "511360", "511880")):
        self.index_code = index
        frames = {}
        need = list(codes) + [index] + [c for c in extra if c not in codes]
        for c in need:
            p = os.path.join(dirpath, c + ".csv")
            if not os.path.exists(p):
                continue
            df = pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index()
            frames[c] = df
        cal = frames[index].index
        self.cal = cal
        self.codes = [c for c in codes if c in frames]

        def mat(col):
            out = {c: frames[c][col].reindex(cal) for c in frames}
            return pd.DataFrame(out, index=cal)

        P = mat("close")
        H = mat("high")
        Lo = mat("low")
        # 未上市: 不参与排名/交易（用 NaN 表示），上市后停牌日用前值
        self.valid = P.notna()
        self.P = P.ffill()
        self.H = H.ffill()
        self.L = Lo.ffill()
        self.R = self.P.pct_change().fillna(0.0)

        # ATR14 (TR 的简单均值，与实盘 ATR 指标同量级)
        pc = self.P.shift(1)
        tr = pd.concat([self.H - self.L,
                        (self.H - pc).abs(),
                        (self.L - pc).abs()]).groupby(level=0).max()
        tr = tr.reindex(cal)
        self.ATRP = (tr.rolling(14, min_periods=5).mean() / self.P)

        for w in (5, 10, 15, 20, 40, 60, 120):
            setattr(self, f"MA{w}", self.P.rolling(w, min_periods=max(3, w // 2)).mean())
        # 滚动新高（突破入场用，预计算避免每 config 重算）
        self.HMAX = {w: self.P.rolling(w, min_periods=max(5, w // 2)).max().values for w in (20, 60)}
        # 动量矩阵
        self.MOM = {w: self.P / self.P.shift(w) - 1.0 for w in (5, 10, 15, 20, 40, 60, 120)}
        # 相对强弱基准: 指数同窗口动量
        self.IDXMOM = {w: (self.P[index] / self.P[index].shift(w) - 1.0) for w in self.MOM}

        self.Pn = self.P.values
        self.Rn = self.R.values
        self.Vn = self.valid.values
        self.ATRPn = self.ATRP.values
        self.MOMn = {w: v.values for w, v in self.MOM.items()}
        self.IDXMOMn = {w: v.values for w, v in self.IDXMOM.items()}
        self.MAn = {w: getattr(self, f"MA{w}").values for w in (5, 10, 15, 20, 40, 60, 120)}
        self.col = {c: i for i, c in enumerate(self.P.columns)}
        self.is_trade = np.array([c in self.codes for c in self.P.columns])

        # 指数择时特征
        ip = self.P[index].values
        ma20 = self.MAn[20][:, self.col[index]]
        ma60 = self.MAn[60][:, self.col[index]]
        ma120 = self.MAn[120][:, self.col[index]]
        slope20 = np.append([0] * 5, ma20[5:] / ma20[:-5] - 1.0)
        roll_max60 = pd.Series(ip).rolling(60, min_periods=20).max().values
        roll_min60 = pd.Series(ip).rolling(60, min_periods=20).min().values
        vol20 = (pd.Series(self.Rn[:, self.col[index]]).rolling(20, min_periods=10).std().values
                 * np.sqrt(252))
        self.IX = {
            "px": ip, "ma20": ma20, "ma60": ma60, "ma120": ma120,
            "slope20": slope20, "hi60": roll_max60, "lo60": roll_min60, "vol20": vol20,
            "atr": self.ATRPn[:, self.col[index]],
        }
        # 指数 N 日收益（崩盘过滤）
        self.IX["ret"] = {d: np.append([0.0] * d, ip[d:] / ip[:-d] - 1.0) for d in (5, 10, 20)}


def timing_series(ix, cfg):
    """组合层择时 → 每日总仓位乘数 L ∈ [lo, 1]。全部只用当日及之前的数据。"""
    mode = cfg["timing"]
    T = len(ix["px"])
    if mode == "none":
        return np.ones(T)

    ma = ix[f"ma{cfg['t_ma']}"]
    with np.errstate(invalid="ignore", divide="ignore"):
        dev = ix["px"] / ma - 1.0
    dev = np.nan_to_num(dev)
    slope = np.nan_to_num(ix["slope20"])
    vol = np.nan_to_num(ix["vol20"])
    hi = np.nan_to_num(ix["hi60"], nan=0.0)
    lo = np.nan_to_num(ix["lo60"], nan=0.0)
    px = ix["px"]
    atrp = np.nan_to_num(ix["atr"])

    if mode == "levels":
        # 三因子打分（与 structural 报告同构），三档乘数
        L1, L2, L3 = cfg["levels"]
        score = np.zeros(T)
        score += ((px > ma) & (dev > -0.001)).astype(float)
        score += (slope > 0).astype(float)
        above = (hi > 0) & ((hi - px) / np.where(hi > 0, hi, 1) < 0.05)
        score += above.astype(float)
        low = (lo > 0) & ((px - lo) / np.where(lo > 0, lo, 1) < 0.03)
        score -= 2.0 * low.astype(float)
        score -= (atrp > 1.5 * np.nanmedian(atrp) if np.nanmedian(atrp) > 0 else 0).astype(float)
        L = np.where(score >= 2, L3, np.where(score >= 0, L2, L1))
    elif mode == "cont":
        lo_lv = cfg["t_lo"]
        q = 0.6 * np.clip(dev / 0.08, -1, 1) + 0.4 * np.clip(slope / 0.01, -1, 1)
        L = lo_lv + (1.0 - lo_lv) * np.clip((q + 1.0) / 2.0, 0.0, 1.0)
    elif mode == "vol":
        L = np.clip(cfg["t_vol"] / np.where(vol > 0, vol, 1e9), cfg["t_lo"], 1.0)
    elif mode == "crash":
        L = np.ones(T)
        hit = ix["ret"][cfg["t_d"]] < -cfg["t_p"]
        # 触发后保持 t_hold 天在低档
        hold = int(cfg["t_hold"])
        if hit.any():
            idx = np.flatnonzero(hit)
            for j in idx:
                L[j:j + hold] = np.minimum(L[j:j + hold], cfg["t_lo"])
    elif mode == "combo":
        # cont 与 vol 取小，再叠加崩盘覆盖
        q = 0.6 * np.clip(dev / 0.08, -1, 1) + 0.4 * np.clip(slope / 0.01, -1, 1)
        Lc = cfg["t_lo"] + (1.0 - cfg["t_lo"]) * np.clip((q + 1.0) / 2.0, 0.0, 1.0)
        Lv = np.clip(cfg["t_vol"] / np.where(vol > 0, vol, 1e9), cfg["t_lo"], 1.0)
        L = np.minimum(Lc, Lv)
        hit = ix["ret"][cfg["t_d"]] < -cfg["t_p"]
        if hit.any():
            idx = np.flatnonzero(hit)
            for j in idx:
                L[j:j + int(cfg["t_hold"])] = np.minimum(L[j:j + int(cfg["t_hold"])], cfg["t_lo"])
    else:
        raise ValueError(mode)

    # 预热期（指数指标未就绪）不做择时
    warm = np.flatnonzero(np.isfinite(ix["ma60"]))
    if len(warm) and warm[0] > 0:
        L[:warm[0]] = 1.0
    return np.clip(L, 0.0, 1.0)


class Prepared:
    """把 config 编译成逐日矩阵，供 simulate 复用（同一 config 跑多个窗口只编译一次）。"""

    def __init__(self, data, cfg):
        self.cfg = cfg
        self.data = data
        n = len(data.codes)
        # —— 动量得分 ——
        mt = cfg["mom"]
        if mt["type"] == "single":
            S = data.MOMn[mt["lb"]].copy()
        else:
            S = np.zeros_like(data.MOMn[20])
            for w, wt in mt["mix"]:
                S = S + wt * np.nan_to_num(data.MOMn[w])
        if cfg.get("accel", 0) > 0:
            S = S + cfg["accel"] * (np.nan_to_num(data.MOMn[10]) - np.nan_to_num(data.MOMn[20]))
        idx_col = data.col[data.index_code]
        if cfg.get("rel", False):
            base = np.nan_to_num(data.IDXMOMn[20])[:, None]
            S = np.where(S > base, S, -1e9)  # 只在强于大盘的标的里选
        S = np.where(data.Vn, S, -1e9)
        S = np.where(np.isfinite(S), S, -1e9)
        self.SCORE = S

        # —— 入场过滤（只作用于新开仓）——
        P = data.Pn
        e = cfg["entry"]
        if e == "ma5ma20":
            ok = P > data.MAn[5] * 0.995  # 避免浮点边界
            ok = (data.MAn[5] > data.MAn[20])
        elif e == "brk20":
            ok = P >= data.HMAX[20]
        elif e == "brk60":
            ok = P >= data.HMAX[60]
        elif e == "pb10":
            ok = (P >= data.MAn[10] * 0.98) & (P <= data.MAn[10] * 1.04)
        else:
            ok = np.ones_like(P, dtype=bool)
        self.ENTRY = ok & data.Vn

        # —— 移动止盈档 ——
        tm = cfg["trail"]
        if tm == "e1":
            a = np.nan_to_num(data.ATRPn)
            alt = np.where(a > 0.05, 0.07, np.where(a >= 0.03, 0.05, 0.03))
            self.TRAIL = alt
            self.TRAIL_KIND = "e1"
        elif tm == "none":
            self.TRAIL = np.full_like(P, 9.99)
            self.TRAIL_KIND = "fixed"
        elif tm.startswith("atr"):
            a = np.nan_to_num(data.ATRPn)
            self.TRAIL = np.clip(cfg["atr_mult"] * a, 0.02, 0.25)
            self.TRAIL_KIND = "atr"
        else:
            self.TRAIL = np.full_like(P, cfg["trail_pct"])
            self.TRAIL_KIND = "fixed"
        self.L = timing_series(data.IX, cfg)

    def simulate(self, i0, i1):
        cfg = self.cfg
        d = self.data
        P, R, V = d.Pn, d.Rn, d.Vn
        n_slot = cfg["n"]
        cap = cfg["cap"]
        gross0 = cfg["gross"]
        stop = cfg["stop"]
        tstop = cfg["time_stop"]
        hedge_i = d.col[cfg["hedge"]] if cfg.get("hedge") else None
        is_trade = d.is_trade
        S, EN, TR = self.SCORE, self.ENTRY, self.TRAIL
        L = self.L

        riv = int(cfg.get("rank_iv", 1))
        lag = int(cfg.get("lag", 1))
        cand_mask = V[i0] & is_trade
        top_set = None
        top_drop = set()
        N = P.shape[1]
        w = np.zeros(N)
        peak = np.zeros(N)
        entry = np.zeros(N)
        age = np.zeros(N, dtype=int)
        eq = 1.0
        eqs = np.empty(i1 - i0)
        expo = 0.0
        trades = 0
        log = []
        for i in range(i0, i1):
            r = R[i]
            pr = float(w @ r)
            eq *= (1.0 + pr)
            if pr != 0.0:
                w = w * (1.0 + r) / (1.0 + pr)

            px = P[i]
            held = w > 1e-9
            if hedge_i is not None:
                held[hedge_i] = False
            # 峰值/年龄
            peak = np.where(held, np.maximum(peak, px), peak)
            age = np.where(held, age + 1, age)
            prof = np.where(entry > 0, px / np.where(entry > 0, entry, 1) - 1.0, 0.0)

            # ---- 退出判定（信号用 T-lag，成交用 T 收盘）----
            si = i - lag
            if self.TRAIL_KIND == "e1":
                act = prof >= 0.035
                tp = np.where(act, peak * (1.0 - TR[si]), 0.0)
                lock = prof >= 0.07
                tp = np.where(lock, np.maximum(tp, entry * 1.05), tp)
            else:
                tp = peak * (1.0 - TR[si])
            ex = held & (tp > 0) & (px <= tp)
            ex |= held & (prof <= -stop)
            if tstop > 0:
                ex |= held & (age >= tstop) & (prof <= 0)
            for _j in np.flatnonzero(ex):
                log.append((str(d.cal[i].date()), d.codes[_j], "SELL", round(float(prof[_j])*100,2)))
            _exited = set(int(_j) for _j in np.flatnonzero(ex))

            # ---- 排名 / TOP 集合（rank_iv>1 时只在重排日更新）----
            do_rank = (riv == 1) or (i - i0) % riv == 0 or top_set is None
            if do_rank:
                s = S[si].copy()
                cand_mask = V[si] & is_trade
                s = np.where(cand_mask, s, -1e9)
                order = np.argsort(-s)
                top_drop = set(order[: n_slot * int(cfg.get("drop_mult", 2))].tolist())
                top_set = order[:n_slot]
                top_set = [j for j in top_set if cand_mask[j]]
            keep = held & ~ex
            for j in np.flatnonzero(keep):
                if j not in top_drop:
                    keep[j] = False
                    ex[j] = True
                    log.append((str(d.cal[i].date()), d.codes[j], "SELL", round(float(prof[j])*100,2)))
            n_keep = int(keep.sum())
            slots = n_slot - n_keep

            # ---- 目标权重 ----
            gt = gross0 * L[si]
            slot_w = min(cap, gt / n_slot) if n_slot > 0 else 0.0
            wt = np.zeros(N)
            added = []
            if n_keep > 0:
                wt[keep] = w[keep]          # 在位仓位原样保留（与实盘一致：不追补）
                s_keep = float(wt.sum())
                if s_keep > gt:             # 超过目标总仓位才按比例压回
                    wt[keep] *= gt / s_keep
            if slots > 0:
                for j in top_set:
                    if slots <= 0:
                        break
                    if keep[j] or not EN[si, j] or not cand_mask[j]:
                        continue
                    if cfg.get("no_reentry") and j in _exited:
                        continue
                    room = gt - float(wt.sum())
                    if room <= 1e-6:
                        break
                    wt[j] = min(slot_w, room)
                    log.append((str(d.cal[i].date()), d.codes[j], "BUY", 0.0))
                    added.append(j)
                    slots -= 1
            tot = float(wt.sum())
            if tot > 1.0:                   # 硬约束：无杠杆，总敞口 ≤ 100%
                wt *= 1.0 / tot
                tot = 1.0
            if hedge_i is not None:
                wt[hedge_i] = max(0.0, 1.0 - tot)

            # ---- 执行成本 ----
            turn = float(np.abs(wt - w).sum())
            if turn > 0:
                eq *= (1.0 - COST * turn)
                trades += int(len(added) + int(ex.sum()))

            # ---- 状态更新 ----
            newpos = (wt > 1e-9) & (w <= 1e-9)
            entry[newpos] = px[newpos]
            peak[newpos] = px[newpos]
            age[newpos] = 0
            entry[np.where(wt <= 1e-9)] = 0.0
            peak[np.where(wt <= 1e-9)] = 0.0
            age[np.where(wt <= 1e-9)] = 0
            w = wt
            expo += tot  # 风险资产敞口(不含避风资产)
            eqs[i - i0] = eq
        return {"eq": eqs, "expo": expo / max(1, i1 - i0), "trades": trades, "log": log}


def metrics(eqs, cal_slice):
    eqs = np.asarray(eqs, dtype=float)
    if len(eqs) < 2:
        return {}
    tot = eqs[-1] / 1.0 - 1.0
    yrs = len(eqs) / 252.0
    cagr = (eqs[-1]) ** (1.0 / yrs) - 1.0 if eqs[-1] > 0 else -1.0
    run = np.maximum.accumulate(eqs)
    mdd = float(np.max(1.0 - eqs / run))
    ret = np.diff(eqs) / eqs[:-1]
    sd = ret.std(ddof=1)
    sharpe = float(ret.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    s = pd.Series(eqs, index=cal_slice)
    yearly = s.groupby(s.index.year).apply(lambda x: x.iloc[-1] / x.iloc[0] - 1.0).to_dict()
    return {"total": float(tot), "cagr": float(cagr), "mdd": mdd, "sharpe": sharpe,
            "calmar": float(cagr / mdd) if mdd > 0 else 0.0,
            "yearly": {int(k): float(v) for k, v in yearly.items()}}
