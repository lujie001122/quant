"""Minimal, independent backtest engine for the greenfield research.

Deliberately standalone: no import of the existing trading system's modules.
Conventions
  - signal computed on close(t), position held from close(t) to close(t+k)
  - cost charged on traded notional at each rebalance, per side
  - missing price -> ffill (return 0), never back-filled
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 242.0


# ---------------------------------------------------------------- metrics
def metrics(ret: pd.Series, rf=0.0) -> dict:
    r = ret.dropna()
    if len(r) < 20:
        return {}
    eq = (1 + r).cumprod()
    years = len(r) / TRADING_DAYS
    ann = eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    dd = (eq / eq.cummax() - 1).min()
    vol = r.std() * math.sqrt(TRADING_DAYS)
    sharpe = (r.mean() * TRADING_DAYS - rf) / vol if vol > 0 else np.nan
    downside = r[r < 0].std() * math.sqrt(TRADING_DAYS)
    sortino = (r.mean() * TRADING_DAYS - rf) / downside if downside and downside > 0 else np.nan
    return dict(ann=ann, mdd=dd, vol=vol, sharpe=sharpe, sortino=sortino,
                calmar=ann / abs(dd) if dd < 0 else np.nan,
                total=eq.iloc[-1] - 1, n=len(r), years=years,
                win=float((r > 0).mean()))


def yearly(ret: pd.Series) -> pd.Series:
    r = ret.dropna()
    return r.groupby(r.index.year).apply(lambda x: (1 + x).prod() - 1)


def fmt(m: dict) -> str:
    if not m:
        return "n/a"
    return (f"ann={m['ann']*100:6.2f}%  mdd={m['mdd']*100:6.1f}%  "
            f"sharpe={m['sharpe']:.2f}  calmar={m['calmar']:.2f}  vol={m['vol']*100:4.1f}%")


# ---------------------------------------------------------------- helpers
def _ffill_prices(close: pd.DataFrame) -> pd.DataFrame:
    return close.ffill()


def eligible_mask(close: pd.DataFrame, amount: pd.DataFrame,
                  min_days=60, min_amt=2e7, start=None, allowed=None,
                  meta_cat: dict | None = None, amt_win=20) -> pd.DataFrame:
    px = close.notna()
    hist = px.cumsum()
    amt_ok = amount.rolling(amt_win, min_periods=max(5, amt_win // 2)).mean() > min_amt
    m = px & (hist >= min_days) & amt_ok
    if allowed is not None and meta_cat is not None:
        cols = [c for c in close.columns if meta_cat.get(c) in allowed]
        keep = pd.DataFrame(False, index=close.index, columns=close.columns)
        keep[cols] = True
        m &= keep
    if start is not None:
        m = m.mul(pd.Series(close.index >= pd.Timestamp(start), index=close.index), axis=0)
    return m


def momentum(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    return close / close.shift(lookback) - 1.0


# ---------------------------------------------------------------- rotation
def run_rotation(close: pd.DataFrame, elig: pd.DataFrame, score: pd.DataFrame,
                 topn: int = 5, rebal: int = 5, cost: float = 0.001,
                 cash_ret: pd.Series | None = None, min_score: float | None = None,
                 vol_target: float | None = None, weight_mode: str = "eq",
                 hold_buffer: int = 0, verbose=False) -> dict:
    """Top-N long-only rotation. Returns dict with returns/weights/turnover."""
    px = close.ffill()
    ret = px.pct_change().fillna(0.0)
    dates = px.index
    w_prev = pd.Series(0.0, index=px.columns)
    w_hist = pd.DataFrame(0.0, index=dates, columns=px.columns)
    port = pd.Series(0.0, index=dates)
    turn = pd.Series(0.0, index=dates)
    picks_log = {}

    for i, d in enumerate(dates):
        # apply yesterday's weights to today's return
        if i > 0:
            port.iloc[i] = float((w_prev * ret.iloc[i]).sum())
            if w_prev.sum() < 1 - 1e-9 and cash_ret is not None:
                port.iloc[i] += (1 - w_prev.sum()) * float(cash_ret.get(d, 0.0))
        if i % rebal != 0:
            w_hist.iloc[i] = w_prev.values
            continue
        s = score.iloc[i]
        ok = elig.iloc[i] & s.notna()
        cand = s[ok]
        if min_score is not None:
            cand = cand[cand > min_score]
        if hold_buffer > 0 and w_prev.sum() > 0:
            held = w_prev[w_prev > 0].index
            keep = [c for c in held if c in cand.index]
            newc = cand.drop(index=keep, errors="ignore").nlargest(max(0, topn - len(keep)))
            sel = list(keep) + list(newc.index)
        else:
            sel = list(cand.nlargest(topn).index)
        w = pd.Series(0.0, index=px.columns)
        if sel:
            if weight_mode == "eq":
                w[sel] = 1.0 / len(sel)
            elif weight_mode == "score":
                v = cand[sel].clip(lower=0)
                w[sel] = v / v.sum() if v.sum() > 0 else 1.0 / len(sel)
            elif weight_mode == "invvol":
                vol = ret.iloc[max(0, i - 60):i][sel].std() * math.sqrt(TRADING_DAYS)
                iv = 1.0 / vol.replace(0, np.nan)
                iv = iv.fillna(iv.mean() if iv.notna().any() else 1.0)
                w[sel] = iv / iv.sum()
            if vol_target:
                vol = ret.iloc[max(0, i - 60):i][sel].std().mean() * math.sqrt(TRADING_DAYS)
                if vol and vol > 0:
                    scale = min(1.0, vol_target / vol)
                    w *= scale
        turn.iloc[i] = float((w - w_prev).abs().sum()) / 2.0
        port.iloc[i] -= turn.iloc[i] * cost
        w_prev = w
        w_hist.iloc[i] = w.values
        picks_log[d] = sel

    port.iloc[0] = 0.0
    return dict(ret=port, weights=w_hist, turnover=turn, picks=picks_log)


# ---------------------------------------------------------------- trend
def run_trend(close: pd.DataFrame, elig: pd.DataFrame, topn: int = 5,
              rebal: int = 5, cost: float = 0.001, ma: int = 60,
              lookback: int = 20, use_abs_mom: bool = True,
              cash_ret: pd.Series | None = None) -> dict:
    """Trend: hold top-N names whose close > MA(ma); else cash."""
    px = close.ffill()
    above = (px > px.rolling(ma, min_periods=ma // 2).mean()).astype(float)
    mom = momentum(px, lookback)
    score = mom.where(above > 0)
    return run_rotation(close, elig, score, topn=topn, rebal=rebal, cost=cost,
                        cash_ret=cash_ret, min_score=0.0 if use_abs_mom else None)


# ---------------------------------------------------------------- buy & hold
def run_equal_weight(close: pd.DataFrame, elig: pd.DataFrame, cost: float = 0.001,
                     rebal: int = 21) -> dict:
    """Equal-weight the eligible set, rebalance monthly."""
    return run_rotation(close, elig, score=close * 0 + 1.0, topn=10_000,
                        rebal=rebal, cost=cost)


def buy_hold(close: pd.Series, cost=0.001) -> pd.Series:
    r = close.ffill().pct_change().fillna(0.0)
    r.iloc[1] -= cost
    return r


# ---------------------------------------------------------------- deflated sharpe
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def norm_ppf(p):
    # Acklam's approximation
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def deflated_sharpe(sr: float, n_trials: int, n_obs: int, skew=0.0, kurt=3.0,
                    sr_variance: float | None = None) -> dict:
    """Bailey & Lopez de Prado (2014) deflated Sharpe ratio.

    sr      : observed annualised Sharpe (non-annualise before calling: pass SR daily)
    n_trials: number of independent-ish configurations tried
    n_obs   : number of return observations
    """
    if n_trials <= 1:
        sr0 = 0.0
    else:
        e = 0.5772156649
        z1 = norm_ppf(1 - 1.0 / n_trials)
        z2 = norm_ppf(1 - 1.0 / (n_trials * math.e))
        sr0 = math.sqrt(max(sr_variance if sr_variance else 1.0, 1e-12)) * \
              ((1 - e) * z1 + e * z2)
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4.0 * sr ** 2))
    z = (sr - sr0) * math.sqrt(max(n_obs - 1, 1)) / denom
    return dict(sr=sr, sr0=sr0, z=z, dsr=norm_cdf(z), n_trials=n_trials, n_obs=n_obs)
