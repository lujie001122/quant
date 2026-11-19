#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""追涨杀跌（chase）研究报告集 —— 从 chase_live.py 整块外移，行为零变化。

用法（仓库根目录下执行）：
    .venv/bin/python3 research/chase_report.py --abc
    .venv/bin/python3 research/chase_report.py --caps
    .venv/bin/python3 research/chase_report.py --index-map
    .venv/bin/python3 research/chase_report.py --corr-map
    .venv/bin/python3 research/chase_report.py --pod
    .venv/bin/python3 research/chase_report.py --guard
    .venv/bin/python3 research/chase_report.py --win
    .venv/bin/python3 research/chase_report.py --dd
    .venv/bin/python3 research/chase_report.py --check-days --start 2023-11-17 --end 2026-11-16
    .venv/bin/python3 research/chase_report.py --phases
    .venv/bin/python3 research/chase_report.py --trades --start 2023-11-17 --end 2026-11-16

只做只读研究：数据/撮合/统计全部复用 chase_live.py（同一份面板缓存、同一个 run()），
参数覆盖口径与原来的 chase_live.py --xxx 完全一致；不下单、不写 portfolio.json。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根

from chase_live import *                                        # noqa: F401,F403 —— run/prep/stats/W3Y/POD_VARIANTS…
from chase_live import _code6                                   # noqa: F401  —— 下划线工具，星号导入不含


# ════════════════════════════════════════════════════════════════════
# 同指数分组的审计（覆盖率 / 误并 / 漏并）—— 只读，不改任何交易逻辑
# ════════════════════════════════════════════════════════════════════
def index_map_report(cfg=None, win=120, quiet=False):
    """打印并落盘「标的指数 → ETF 清单」，同时用近 win 日收益率相关系数交叉验证。

    - 覆盖率：多少名称拿到指数键、多少只在多成员组里（即去重会归并掉多少只）
    - 误并：同一指数键内最低相关 < idx_corr_split 的组（名字一样但跟踪的指数不同）
    - 漏并：不同指数键但 ρ ≥ 0.97 的对（同一指数不同叫法，别名表没覆盖到）
    """
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    cols = P["cols"]
    names = P["names"]
    keys = [index_of(names.get(c, "")) for c in cols]
    grp = {}
    for i, k in enumerate(keys):
        if k:
            grp.setdefault(k, []).append(str(cols[i]))
    multi = {k: v for k, v in grp.items() if len(v) > 1}
    n_etf = sum(len(v) for v in grp.values())
    collapse = sum(len(v) - 1 for v in multi.values())
    print(f"[同指数] 名称 {len(cols)} 只 → 有指数键 {n_etf} 只 / {len(grp)} 个标的指数；"
          f"多成员组 {len(multi)} 个，去重最多归并 {collapse} 只（{collapse / max(n_etf, 1):.1%}）")

    C = P["C"]
    ret = pd.DataFrame(C[-win - 1:-1] / C[-win - 2:-2] - 1.0)
    good = ret.notna().mean() >= 0.8
    cpos = {str(c): j for j, c in enumerate(cols)}
    rc = [j for j, c in enumerate(cols) if bool(good.iloc[j])]
    col2row = {int(j): n for n, j in enumerate(rc)}
    M = ret.iloc[:, rc].corr(min_periods=int(win * 0.8))
    rows = []
    for k, v in sorted(multi.items(), key=lambda x: -len(x[1])):
        js = [col2row[cpos[c]] for c in v if cpos[c] in col2row]
        if len(js) < 2:
            rows.append(dict(index=k, n=len(v), min_rho=float("nan"), med_rho=float("nan"),
                             names=[names.get(c, "") for c in v]))
            continue
        sub = M.iloc[js, js].to_numpy()
        iu = np.triu_indices(len(js), 1)
        rows.append(dict(index=k, n=len(v), min_rho=float(np.nanmin(sub[iu])),
                         med_rho=float(np.nanmedian(sub[iu])),
                         names=[names.get(c, "") for c in v]))
    bad = [r for r in rows if np.isfinite(r["min_rho"]) and r["min_rho"] < cfg["idx_corr_split"]]
    print(f"[同指数] 组内相关：中位 ρ 的中位数 {np.nanmedian([r['med_rho'] for r in rows]):.4f}；"
          f"误并候选（组内最低 ρ < {cfg['idx_corr_split']}）{len(bad)}/{len(rows)} 组")
    for r in sorted(bad, key=lambda x: x["min_rho"])[:12]:
        print(f"    {r['index']:<10}{r['n']:3d}只 minρ={r['min_rho']:.3f} medρ={r['med_rho']:.3f}  "
              f"{', '.join(r['names'][:4])}")

    hi = M.to_numpy() >= 0.97
    np.fill_diagonal(hi, False)
    kk = [keys[j] for j in rc]
    pairs = {}
    for a, b in zip(*np.where(np.triu(hi, 1))):
        if kk[a] is None or kk[b] is None or kk[a] == kk[b]:
            continue
        key = tuple(sorted((kk[a], kk[b])))
        pairs.setdefault(key, []).append(float(M.iloc[a, b]))
    print(f"[同指数] 漏并候选（不同指数键但 ρ≥0.97）：{len(pairs)} 组键对")
    for (a, b), v in sorted(pairs.items(), key=lambda x: -len(x[1]))[:12]:
        print(f"    {a:<10} ~ {b:<10}{len(v):4d} 对  ρmax={max(v):.4f}  （不同指数，未合并）")
    json.dump(dict(n_etf=n_etf, n_index=len(grp), n_multi=len(multi), collapse=collapse,
                   groups={k: v for k, v in sorted(multi.items(), key=lambda x: -len(x[1]))},
                   within=[{k: v for k, v in r.items()} for r in rows],
                   cross_pairs=[dict(a=a, b=b, n=len(v), rho_max=max(v))
                                for (a, b), v in pairs.items()]),
              open(os.path.join(OUT, "index_map.json"), "w"), ensure_ascii=False, indent=1)
    print(f"  写完 {os.path.join(OUT, 'index_map.json')}")
    return dict(n_etf=n_etf, n_index=len(grp), n_multi=len(multi), collapse=collapse,
                bad=len(bad), cross=len(pairs))


# ════════════════════════════════════════════════════════════════════
# 任务一：主题上限取舍（5 / 6 / 8 / 不限 × 3y / 5y）
# ════════════════════════════════════════════════════════════════════
def caps_report(cfg=None, caps=(5, 6, 8, 0, 2, 3, 4), quiet=False):
    """主题上限四档 × 两窗口。返回 rows（含最坏单日/两日、平均持仓、主题占比、拦截次数）。"""
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    rows = []
    for tag, (s0, s1) in (("3y", W3Y), ("5y", W5Y)):
        for cap in caps:
            prm = replace(params_from_cfg(cfg, start=s0, end=s1), theme_cap=int(cap))
            res = run(P, D, mask, pct, prm)
            st = stats(res, prm, f"{tag} 上限{cap or '不限'}")
            ex = risk_extra(res, st["label"])
            row = dict(window=tag, cap=int(cap), label=st["label"],
                       cagr=st["cagr"], mdd=st["mdd"], sharpe=st["sharpe"], calmar=st["calmar"],
                       avg_n=ex["avg_n"], share_p50=ex["share_p50"], share_p90=ex["share_p90"],
                       share_max=ex["share_max"], cap_blocked=int(res["cap_blocked"]),
                       worst1=ex["worst1"], worst1_date=ex["worst1_date"],
                       worst2=ex["worst2"], worst2_date=ex["worst2_date"],
                       n_trades=st["n_trades"], turn=st["turn_wk"])
            rows.append(row)
            if not quiet:
                print(f"{tag} 上限{cap or '不限':>2}  年化{row['cagr'] * 100:6.2f}% "
                      f"回撤{row['mdd'] * 100:6.1f}% 夏普{row['sharpe']:5.2f} "
                      f"Calmar{row['calmar']:5.2f} 持仓{row['avg_n']:4.1f}只 "
                      f"同主题占比中位{row['share_p50'] * 100:4.0f}%/90分位{row['share_p90'] * 100:4.0f}%"
                      f"/最大{row['share_max'] * 100:4.0f}% 拦截{row['cap_blocked']:4d}次 "
                      f"最坏单日{row['worst1'] * 100:6.2f}%({row['worst1_date']}) "
                      f"最坏两日{row['worst2'] * 100:6.2f}%({row['worst2_date']})",
                      flush=True)
    return rows


def pick_cap(rows, limit=-0.08):
    """推荐：在最坏单日不破 limit 的前提下年化最高（同等年化优先更小的集中度）。"""
    ok = [r for r in rows if r["worst1"] > limit]
    if not ok:
        return None
    return max(ok, key=lambda r: (round(r["cagr"], 6), -r["share_max"]))


# ════════════════════════════════════════════════════════════════════
# 相关性聚类的审计 / A-B-C 三档对照 / 阈值敏感性
# ════════════════════════════════════════════════════════════════════
def pair_corr(P, i, js, w=60):
    """近 w 个交易日（截止 T-1）指定标的的两两相关系数矩阵。返回 (矩阵, 最大ρ, 对应两只)。"""
    C = P["C"]
    w = max(20, int(w))
    lo = max(i - 1 - w, 1)
    R = C[lo:i]
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = R[1:] / R[:-1] - 1.0
    codes = [str(P["cols"][j]) for j in js]
    if len(js) < 2:
        return pd.DataFrame(), float("nan"), (None, None)
    M = pd.DataFrame(ret[:, js], columns=codes).corr(min_periods=max(20, int(len(ret) * 0.8)))
    a = M.to_numpy(dtype=float)
    iu = np.triu_indices(len(js), 1)
    v = a[iu]
    if not np.isfinite(v).any():
        return M, float("nan"), (None, None)
    k = int(np.nanargmax(v))
    return M, float(v[k]), (codes[iu[0][k]], codes[iu[1][k]])


def corr_map_report(cfg=None, quiet=False):
    """打印：最新交易日的相关性聚类结构 + 决策日分布 + 新目标清单与其最大两两 ρ。只读。"""
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    i = len(P["idx"]) - 1
    cols, names = P["cols"], P["names"]
    prm = params_from_cfg(cfg, start=cfg["live_start"], end=str(P["idx"][i].date()))
    snap = corr_snapshot(P, D, i, prm, mask)
    sizes = snap["sizes"]
    cid_all = snap["cid"]
    pool = np.where(mask[i])[0]
    n_pool_cl = len(np.unique(cid_all[pool]))
    n_big = sum(1 for c in np.unique(cid_all[pool]) if sizes[int(c)] >= 2)
    print(f"[相关性聚类] {P['idx'][i].date()} 池 {int(mask[i].sum())} 只 → {n_pool_cl} 簇"
          f"（≥2 只 {n_big} 簇；合并边 {snap['n_edges']}；窗口 {prm.idx_corr_window} 日 T-1；"
          f"阈值 ρ≥{prm.corr_threshold:.2f} 单链接）")
    for size, js in snap["big"]:
        ex = ", ".join(f"{_code6(cols[j])} {names.get(cols[j], '')}" for j in js[:5])
        print(f"    {size:4d} 只  {ex}")

    wd = sorted(weekly_decisions(P["idx"], cfg["live_start"], str(P["idx"][i].date())))
    ns, mx, pools = [], [], []
    for k in wd:
        s2 = corr_snapshot(P, D, k, prm, mask)
        pk = np.where(mask[k])[0]
        ns.append(len(np.unique(s2["cid"][pk])))
        mx.append(max(s2["sizes"][int(c)] for c in np.unique(s2["cid"][pk])))
        pools.append(len(pk))
    print(f"[相关性聚类] 决策日 {len(wd)} 个：簇数 中位{np.median(ns):.0f}/最少{min(ns)}；"
          f"最大簇 中位{np.median(mx):.0f}/最大{max(mx)} 只（池中位 {int(np.median(pools))} 只）")

    cid = snap["cid"]
    for lbl, cfgx in (("只做同指数去重(B)", dict(cfg, corr_cluster_cap=0)),
                      ("去重+相关簇上限(C)", dict(cfg))):
        tg = preview_targets(cfg=cfgx, quiet=True)
        js = [int(np.where(cols == sid)[0][0]) for sid in tg["targets"]]
        js.sort(key=lambda j: -(pct[i][j] if np.isfinite(pct[i][j]) else -1))
        M, rho, pr = pair_corr(P, i, js, cfg["idx_corr_window"])
        print(f"[{lbl}] {len(js)} 只，两两 ρ 最大 {rho:.4f}"
              + (f"（{_code6(pr[0])} ~ {_code6(pr[1])}）" if pr[0] else ""))
        for j in js:
            nm = names.get(cols[j], "")
            print(f"    {_code6(cols[j]):<7}{nm:<14}主题{str(theme_of(nm)):<10}"
                  f"指数{str(index_of(nm)):<10}分位{pct[i][j]:5.1%}  簇{cid[j]}({sizes[int(cid[j])]}只)")
    return dict(n_clusters=int(n_pool_cl), max_size=int(max(sizes[int(c)] for c in np.unique(cid_all[pool]))),
                decision_days=len(wd), cluster_med=float(np.median(ns)),
                max_size_med=float(np.median(mx)))


def worst_day_pos(res, P, date=None):
    """最坏单日（或指定日）的持仓清单 [(代码, 名称, 市值)]，核对「那天到底押了什么」。"""
    r = res["ret"].to_numpy(dtype=float)
    if date is None:
        k = int(np.nanargmin(r))
    else:
        k = int(np.where(res["ret"].index == pd.Timestamp(date))[0][0])
    cols, names = P["cols"], P["names"]
    pos = [(str(cols[j]), names.get(cols[j], ""), float(v))
           for j, v in res["pos_log"][k] if np.isfinite(v)]
    pos.sort(key=lambda x: -x[2])
    return str(res["ret"].index[k].date()), float(r[k]), pos


# ════════════════════════════════════════════════════════════════════
# 单吊赛道（theme_cap=0）专项报告 —— 四个版本 / 护栏消融 / 四窗口 / 最惨一段
# 只调 run() 的入参，不改一行策略；输出纯文本（QQ 直接用）
# ════════════════════════════════════════════════════════════════════
POD_VARIANTS = [
    ("V1 参照 theme6/index1", dict(theme_cap=6, index_cap=1)),
    ("V2 单吊 theme0/index1", dict(theme_cap=0, index_cap=1)),
    ("V3 强制单吊动量第一赛道", dict(theme_cap=0, index_cap=1, theme_mode="top1")),
    ("V4 单吊+集中4只", dict(theme_cap=0, index_cap=1, n_hold=4)),
]

GUARD_VARIANTS = [
    ("V2 无护栏", dict(theme_cap=0, index_cap=1)),
    ("V2+熔断6%(清最弱)", dict(theme_cap=0, index_cap=1, guard_day=0.06)),
    ("V2+熔断8%(清最弱)", dict(theme_cap=0, index_cap=1, guard_day=0.08)),
    ("V2+熔断10%(清最弱)", dict(theme_cap=0, index_cap=1, guard_day=0.10)),
    ("参考 熔断4%(清最弱)", dict(theme_cap=0, index_cap=1, guard_day=0.04)),
    ("V2+熔断6%(整体减半)", dict(theme_cap=0, index_cap=1, guard_day=0.06, guard_day_act="half")),
    ("V2+赛道破MA20过半→半仓", dict(theme_cap=0, index_cap=1, guard_theme=0.5)),
    ("V2+单吊赛道破MA20→半仓", dict(theme_cap=0, index_cap=1, guard_theme=0.5,
                                guard_theme_lead=True)),
    ("V2+熔断6%+单吊赛道半仓", dict(theme_cap=0, index_cap=1, guard_day=0.06, guard_theme=0.5,
                              guard_theme_lead=True)),
]


def _f1(name, st, ex):
    return (f"{name}\n  年化{st['cagr'] * 100:7.2f}% 累计{st['cups'] * 100:8.2f}% "
            f"波动{st['vol'] * 100:5.1f}% 夏普{st['sharpe']:5.2f} "
            f"回撤{st['mdd'] * 100:6.1f}% Calmar{st['calmar']:5.2f}")


def _f2(st, ex):
    return (f"  换手{st['turn_wk'] * 100:5.1f}%/周 成本{st['cost_yr'] * 100:5.2f}%/年 "
            f"平均持仓{ex['avg_n']:4.1f}只 最大单赛道占比 中位{ex['share_p50'] * 100:4.1f}%"
            f"/90分位{ex['share_p90'] * 100:5.1f}%/最大(市值){ex['sharew_max'] * 100:5.1f}% "
            f"交易{st['n_trades']}笔")


def _f3(ex):
    return (f"  最坏单日{ex['worst1'] * 100:6.2f}%({ex['worst1_date']}) "
            f"最坏两日{ex['worst2'] * 100:6.2f}%({ex['worst2_date']}) "
            f"最坏五日{ex['worst5'] * 100:6.2f}%({ex['worst5_date']})")


def _f4(eq):
    return "  分年 " + " ".join(f"{k}:{v * 100:+.1f}%" for k, v in by_year(eq).items())


def _f5(st, ex):
    """护栏口径：触发次数 + 护栏自己造成的换手/成本（相对无护栏的差在报告里另列）。"""
    return (f"  护栏触发 单日熔断{ex['guard_day_trig']}次(清{ex['guard_weak_sells']}只) "
            f"赛道崩塌{ex['guard_theme_trig']}次(改权{ex['guard_trims']}笔) "
            f"护栏换手¥{ex['guard_turn']:,.0f} 占总成交{ex['guard_turn'] / max(ex['_turn_amt'], 1e-9) * 100:4.1f}%")


def _report(cfg, variants, windows, title, with_guard=False):
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    out = []
    for tag, (s0, s1) in windows:
        print(f"\n=== {title} {tag} {s0} → {s1} ===")
        for nm, kw in variants:
            # 护栏默认关：四个版本/四窗口口径先看"策略本身"，护栏代价单独用 --guard 消融
            prm = replace(params_from_cfg(cfg, start=s0, end=s1),
                          **dict({"guard_day": 0.0, "guard_theme": 0.0}, **kw))
            res = run(P, D, mask, pct, prm)
            st = stats(res, prm, nm)
            ex = risk_extra(res, nm)
            ex["_turn_amt"] = float(sum(res["tr_by"].values()))
            print(_f1(nm, st, ex))
            print(_f2(st, ex))
            print(_f3(ex))
            if with_guard:
                print(_f5(st, ex))
            print(_f4(res["eq"]), flush=True)
            out.append(dict(window=tag, name=nm, kw=kw, cagr=st["cagr"], cups=st["cups"],
                            vol=st["vol"], sharpe=st["sharpe"], mdd=st["mdd"],
                            calmar=st["calmar"], turn=st["turn_wk"], cost=st["cost_yr"],
                            avg_n=ex["avg_n"], sharew_max=ex["sharew_max"],
                            share_max=ex["share_max"], worst1=ex["worst1"],
                            worst1_date=ex["worst1_date"], worst2=ex["worst2"],
                            worst2_date=ex["worst2_date"], worst5=ex["worst5"],
                            worst5_date=ex["worst5_date"], n_trades=st["n_trades"],
                            years=st["years"], by_year=by_year(res["eq"]),
                            guard=dict(res["guard"])))
    return out


def pod_report(cfg=None, windows=(("3y", W3Y), ("5y", W5Y))):
    """四个版本 × 两窗口：年化/累计/波动/夏普/回撤/Calmar/换手/成本/持仓/赛道占比/最坏1-2-5日/分年。"""
    return _report(cfg, POD_VARIANTS, windows, "四版本")


def guard_report(cfg=None, windows=(("3y", W3Y), ("5y", W5Y))):
    """V2 单吊下的护栏消融：不加 vs 熔断 6/8/10% vs 赛道崩塌半仓 vs 组合。"""
    return _report(cfg, GUARD_VARIANTS, windows, "护栏消融", with_guard=True)


def windows_report(cfg=None, variants=None, ends=(("1y", "2025-11-17"), ("2y", "2024-11-17"))):
    """四个时间窗口（1y/2y/3y/5y）在同一版本上的年化对比。"""
    wins = list(ends) + [("3y", W3Y[0]), ("5y", W5Y[0])]
    return _report(cfg, variants or POD_VARIANTS[:2],
                   [(t, (s, W3Y[1])) for t, s in wins], "四窗口")


def dd_report(cfg=None, variants=None, windows=(("3y", W3Y), ("5y", W5Y))):
    """历史上最惨的一段：峰值日 → 谷底日 → 回本日，谷底押的是什么赛道，亏多少，多久回本。"""
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    cols, names = P["cols"], P["names"]
    out = []
    for tag, (s0, s1) in windows:
        for nm, kw in (variants or POD_VARIANTS[:2]):
            prm = replace(params_from_cfg(cfg, start=s0, end=s1), **kw)
            res = run(P, D, mask, pct, prm)
            eq = res["eq"]
            dd = eq / eq.cummax() - 1
            k = int(np.argmin(dd.to_numpy(dtype=float)))
            pk = int(np.argmax(eq.to_numpy(dtype=float)[:k + 1]))
            depth = float(dd.iloc[k])
            rec_k, rec_days = None, None
            for m in range(k, len(eq)):
                if eq.iloc[m] >= eq.iloc[pk]:
                    rec_k = m
                    rec_days = int(np.busday_count(eq.index[k].date(), eq.index[m].date()))
                    break
            pos = [(str(cols[j]), names.get(cols[j], ""), float(v))
                   for j, v in res["pos_log"][k] if np.isfinite(v)]
            pos.sort(key=lambda x: -x[2])
            tot = sum(v for _, _, v in pos) or 1.0
            tc = {}
            for _, nmm, v in pos:
                tc[theme_of(nmm)] = tc.get(theme_of(nmm), 0.0) + v
            top_theme, top_share = max(tc.items(), key=lambda kv: kv[1])
            hold_days = int(np.busday_count(eq.index[pk].date(), eq.index[k].date()))
            print(f"\n=== {nm} {tag} 最惨一段 ===")
            print(f"  峰值 {eq.index[pk].date()} 净值{eq.iloc[pk] / eq.iloc[0]:.4f} → "
                  f"谷底 {eq.index[k].date()} 净值{eq.iloc[k] / eq.iloc[0]:.4f} "
                  f"回撤{depth * 100:6.2f}% 用时{hold_days}个交易日")
            print(f"  回本 {eq.index[rec_k].date() if rec_k else '至今未回本'} "
                  f"（自谷底{rec_days if rec_days is not None else '-'}个交易日，"
                  f"自峰值{int(np.busday_count(eq.index[pk].date(), eq.index[rec_k].date())) if rec_k else '-'}个交易日）")
            print(f"  谷底持仓 {len(pos)} 只，最大赛道 {top_theme} 占市值 {top_share / tot * 100:.1f}%")
            for c, nmm, v in pos:
                print(f"    {_code6(c)}  {nmm[:14]:<16}{theme_of(nmm):<10}{index_of(nmm):<12}"
                      f"市值¥{v:>10,.0f} {v / tot * 100:5.1f}%")
            out.append(dict(name=nm, window=tag, peak=str(eq.index[pk].date()),
                            trough=str(eq.index[k].date()), depth=depth,
                            hold_days=hold_days, rec=str(eq.index[rec_k].date()) if rec_k else None,
                            rec_days=rec_days, top_theme=top_theme,
                            top_share=float(top_share / tot), n_pos=len(pos)))
    return out


def abc_report(cfg=None, thrs=(0.85, 0.90, 0.95), quiet=False):
    """A/B/C 三档 × 3y/5y + 相关性阈值敏感性。返回 dict(rows, sens)。"""
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    variants = [
        ("A 无去重(theme5)", dict(index_cap=0, corr_cluster_cap=0, theme_cap=5)),
        ("B 同指数去重", dict(index_cap=1, corr_cluster_cap=0, theme_cap=6)),
        ("C 去重+相关簇≤2", dict(index_cap=1, corr_cluster_cap=2, theme_cap=6,
                                 corr_threshold=0.90)),
    ]
    rows, keep_res = [], {}
    for tag, (s0, s1) in (("3y", W3Y), ("5y", W5Y)):
        print(f"\n=== {tag} {s0} → {s1} ===")
        for nm, kw in variants:
            # 护栏默认关：四个版本/四窗口口径先看"策略本身"，护栏代价单独用 --guard 消融
            prm = replace(params_from_cfg(cfg, start=s0, end=s1),
                          **dict({"guard_day": 0.0, "guard_theme": 0.0}, **kw))
            res = run(P, D, mask, pct, prm)
            st = stats(res, prm, f"{tag} {nm}")
            ex = risk_extra(res, st["label"])
            row = dict(window=tag, name=nm, cagr=st["cagr"], mdd=st["mdd"], sharpe=st["sharpe"],
                       calmar=st["calmar"], turn=st["turn_wk"], cost=st["cost_yr"],
                       avg_n=ex["avg_n"], corr_p50=ex["corr_p50"], corr_p90=ex["corr_p90"],
                       corr_max=ex["corr_max"], corr_cnt_max=ex["corr_cnt_max"],
                       idx_max=ex["idx_max2"], worst1=ex["worst1"], worst1_date=ex["worst1_date"],
                       worst2=ex["worst2"], worst2_date=ex["worst2_date"],
                       corr_blocked=ex["corr_blocked"], corr_sells=ex["corr_sells"],
                       idx_blocked=ex["idx_blocked"], n_trades=st["n_trades"],
                       year=by_year(res["eq"]))
            rows.append(row)
            keep_res[(tag, nm)] = res
            print(f"{nm:<18} 年化{row['cagr'] * 100:6.2f}% 累计{st['cups'] * 100:7.2f}% "
                  f"回撤{row['mdd'] * 100:6.1f}% 夏普{row['sharpe']:5.2f} Calmar{row['calmar']:5.2f} "
                  f"换手{row['turn'] * 100:5.1f}%/周 成本{row['cost'] * 100:4.2f}%/年 "
                  f"持仓{row['avg_n']:4.1f}只 相关簇占比 中位{row['corr_p50'] * 100:3.0f}%"
                  f"/90分位{row['corr_p90'] * 100:3.0f}%/最大{row['corr_max'] * 100:3.0f}%"
                  f"(最多{row['corr_cnt_max']}只) 拦截{row['corr_blocked']:4d} 卖出{row['corr_sells']:3d} "
                  f"最坏单日{row['worst1'] * 100:6.2f}%({row['worst1_date']}) "
                  f"最坏两日{row['worst2'] * 100:6.2f}%", flush=True)
            print(f"{'':18} 分年:" + " ".join(f"{y}:{v * 100:.2f}%" for y, v in row["year"].items()),
                  flush=True)

    sens = []
    for tag, (s0, s1) in (("3y", W3Y), ("5y", W5Y)):
        for thr in thrs:
            prm = replace(params_from_cfg(cfg, start=s0, end=s1), index_cap=1, theme_cap=6,
                          corr_cluster_cap=2, corr_threshold=float(thr))
            res = run(P, D, mask, pct, prm)
            st = stats(res, prm, f"{tag} ρ≥{thr}")
            ex = risk_extra(res, st["label"])
            row = dict(window=tag, thr=float(thr), cagr=st["cagr"], mdd=st["mdd"],
                       sharpe=st["sharpe"], calmar=st["calmar"], turn=st["turn_wk"],
                       cost=st["cost_yr"], avg_n=ex["avg_n"], corr_max=ex["corr_max"],
                       corr_cnt_max=ex["corr_cnt_max"], worst1=ex["worst1"],
                       worst2=ex["worst2"], n_trades=st["n_trades"],
                       corr_blocked=ex["corr_blocked"])
            sens.append(row)
            print(f"{tag} 阈值{thr:.2f}  年化{row['cagr'] * 100:6.2f}% 回撤{row['mdd'] * 100:6.1f}% "
                  f"夏普{row['sharpe']:5.2f} Calmar{row['calmar']:5.2f} 换手{row['turn'] * 100:5.1f}% "
                  f"成本{row['cost'] * 100:4.2f}% 持仓{row['avg_n']:4.1f}只 "
                  f"相关簇最大占比{row['corr_max'] * 100:3.0f}%(最多{row['corr_cnt_max']}只) "
                  f"最坏单日{row['worst1'] * 100:6.2f}% 拦截{row['corr_blocked']:4d}", flush=True)

    dA, rA, posA = worst_day_pos(keep_res[("3y", variants[0][0])], P)
    print(f"\n[A 档最坏单日] {dA} {rA * 100:.2f}% 持仓 {len(posA)} 只：")
    print("    " + ", ".join(f"{c} {n}({v / 1e4:.0f}万)" for c, n, v in posA))
    for nm, _ in variants:
        d, r, pos = worst_day_pos(keep_res[("3y", nm)], P, date=dA)
        print(f"[{nm}] {d} 当日 {r * 100:6.2f}%，持仓 {len(pos)} 只："
              + (", ".join(f"{c} {n}" for c, n, v in pos) if pos else "空仓"))
    dC, rC, posC = worst_day_pos(keep_res[("3y", variants[2][0])], P)
    print(f"[C 档自身最坏单日] {dC} {rC * 100:.2f}% 持仓 {len(posC)} 只："
          + ", ".join(f"{c} {n}" for c, n, v in posC))
    return dict(rows=rows, sens=sens)


# ════════════════════════════════════════════════════════════════════
# 决策日序列校验 + 相位扫描
#
# 这一节的存在理由：decide_every 是「每 3 个交易日」，最容易出的错是回测从窗口起点开始数、
# 实盘从「今天」开始数，于是两边错开 1~2 天（看起来每天都有信号，实际不是同一批日子）。
# 校验方法：run() 内部用的决策日集合 vs 只读全局交易日历独立重算的集合，逐一比对，
# 并把 3y 窗口的前 10 / 后 10 个决策日连同 T+1 执行日打印出来（肉眼可核）。
# ════════════════════════════════════════════════════════════════════
def check_days(cfg=None, s0=None, s1=None):
    """决策日序列校验：回测口径 == 实盘口径，且每个决策日的次日开盘就是执行日。"""
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    idx = P["idx"]
    every = int(cfg["decide_every"])
    ph = int(cfg["decision_phase"])
    anchor = cfg["decision_anchor"]
    s0, s1 = s0 or W3Y[0], s1 or W3Y[1]
    prm = params_from_cfg(cfg, start=s0, end=s1)
    print(f"[决策日] 锚点={anchor}（第 0 个交易日）  频率=每 {every} 个交易日  "
          f"相位={ph}（决策日 = (序号-相位) % {every} == 0）  窗口 {s0} → {s1}")
    back = [str(pd.Timestamp(idx[i]).date()) for i in run(P, D, mask, pct, prm)["decisions"]]
    # 独立重算：只读 meta/index_calendar.json，不碰面板、不碰 run()
    cal = full_calendar()
    a = int(cal.searchsorted(pd.Timestamp(anchor)))
    o = np.arange(len(cal)) - a
    sel = cal[(np.mod(o - ph, every) == 0) & (cal >= pd.Timestamp(s0)) & (cal <= pd.Timestamp(s1))]
    live = [str(d.date()) for d in sel]
    ok = back == live
    print(f"[决策日] 回测 run() 取 {len(back)} 个 ｜ 实盘按日历独立重算 {len(live)} 个 "
          f"｜ 一致={'是' if ok else '否'}")
    if not ok:
        miss = sorted(set(live) - set(back))
        extra = sorted(set(back) - set(live))
        print(f"    ✗ 不一致：日历有而回测没有 {miss[:5]}；回测有而日历没有 {extra[:5]}")
    # 序号/取模/T+1 执行日（前 10 + 后 10）
    pos = {str(pd.Timestamp(d).date()): k for k, d in enumerate(idx)}
    for lbl, part in (("前 10", back[:10]), ("后 10", back[-10:])):
        print(f"  {lbl}个决策日：")
        for dt in part:
            k = pos[dt]
            nxt = str(pd.Timestamp(idx[k + 1]).date()) if k + 1 < len(idx) else "-（窗口末日，不执行）"
            oo = int(day_ordinal(idx[[k]], anchor)[0])
            print(f"    {dt}  序号{oo:5d}  %{every}={oo % every}  →  T+1 开盘 {nxt}")
    today = day_info(idx, len(idx) - 1, every, anchor, ph)
    print(f"[决策日] 最新交易日 {today['date']}：序号={today['ord']} %{every}={today['mod']} → "
          f"{'决策日' if today['is_decision'] else '非决策日'}")
    return dict(ok=bool(ok), n=len(back), today=today)


def phases_report(cfg=None, windows=(("3y", W3Y), ("5y", W5Y))):
    """同一锚点的 N 个相位 × 两窗口：最好 / 中位 / 最差（相位 = 启动偏移，不是可调参数）。"""
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    every = int(cfg["decide_every"] or 3)
    out = {}
    for tag, (s0, s1) in windows:
        print(f"\n=== {tag} {s0} → {s1}：每 {every} 个交易日决策，锚点 {cfg['decision_anchor']} ===")
        rows = []
        for ph in range(every):
            prm = replace(params_from_cfg(cfg, start=s0, end=s1), decision_phase=ph)
            res = run(P, D, mask, pct, prm)
            st = stats(res, prm)
            ex = risk_extra(res)
            row = dict(window=tag, phase=ph, cagr=st["cagr"], cups=st["cups"], mdd=st["mdd"],
                       sharpe=st["sharpe"], calmar=st["calmar"], turn=st["turn_wk"],
                       cost=st["cost_yr"], avg_n=ex["avg_n"], n_trades=st["n_trades"],
                       worst1=ex["worst1"], worst2=ex["worst2"], worst5=ex["worst5"],
                       t=st["t"], year=by_year(res["eq"]))
            rows.append(row)
            mark = "  ←本档" if ph == int(cfg["decision_phase"]) else ""
            print(f"  相位{ph}: 年化{row['cagr'] * 100:7.2f}% 累计{row['cups'] * 100:8.2f}% "
                  f"回撤{row['mdd'] * 100:6.1f}% 夏普{row['sharpe']:5.2f} Calmar{row['calmar']:5.2f} "
                  f"换手{row['turn'] * 100:5.1f}%/周 成本{row['cost'] * 100:4.2f}%/年 "
                  f"持仓{row['avg_n']:4.2f}只 笔数{row['n_trades']:4d} "
                  f"最坏1{row['worst1'] * 100:6.2f}% 最坏2{row['worst2'] * 100:6.2f}% "
                  f"最坏5{row['worst5'] * 100:6.2f}% t={row['t']:5.2f}{mark}")
            print(f"          分年 " + " ".join(f"{y}:{v * 100:+.1f}%" for y, v in row["year"].items()))
        cg = np.array([r["cagr"] for r in rows])
        print(f"  → {tag} 相位：最好{np.nanmax(cg) * 100:.2f}% 中位{float(np.nanmedian(cg)) * 100:.2f}% "
              f"最差{np.nanmin(cg) * 100:.2f}%（相差{np.nanmax(cg) * 100 - np.nanmin(cg) * 100:.1f}pp）")
        out[tag] = rows
    return out


def trades_report(res, prm):
    """逐笔成交明细（只读导出）：打印 + 写 dumps/trades_{start}_{end}.json。

    数据全部来自 run() 的 legs（真实成交，非估算）；reason 是 decide() 当时的触发条件标注。
    """
    legs = res["legs"]
    eq = res["eq"]
    hn = res["hold_n"]
    offd = res["off_dates"]
    idx = eq.index

    def nat(a, b):
        return (pd.Timestamp(b) - pd.Timestamp(a)).days

    buys_by_day = {}
    for t in legs:
        buys_by_day.setdefault(t["entry_d"], []).append(t)

    RSN = {"regime": "空仓开关触发（510300 跌破 MA60）",
           "buffer": "动量掉出缓冲带（分位<0.70）",
           "rot": "决策日换仓（有更强的标的）",
           "eod": "回测结束平仓",
           "stop": "止损（ATR 跟踪止损触发）",
           "guard": "护栏卖出（单日熔断）",
           "data": "决策日换仓（当日动量分位缺失，按不达标处理）"}

    print("")
    print("=" * 78)
    print(f"逐笔成交明细  {prm.start} → {prm.end}   （run() 实际成交，非估算）")
    print("=" * 78)
    for n, t in enumerate(legs, 1):
        why = t["reason"]
        code6 = t["code"][2:] if t["code"][:2] in ("sh", "sz") else t["code"]
        print(f"第{n}笔 买入 {t['entry_d']} 开盘价 {t['entry_p']:.4f} 买 {t['entry_q']:,} 股 "
              f"金额 {t['entry_amt']:,.0f} 元（{code6} {t['name']}，动量分位 "
              f"{t['entry_pct'] * 100:.1f}%（{t['entry_d']}）/ "
              f"{t['entry_pct_dec'] * 100:.1f}%（决策日 {t['entry_bdec_d']}））")
        td = t["exit_i"] - t["entry_i"]
        nd = nat(t["entry_d"], t["exit_d"])
        if why == "eod":
            print(f"      至 {t['exit_d']} 回测结束仍持有，按当日收盘价 {t['exit_p']:.4f} 估值 "
                  f"持有 {td} 个交易日（自然日 {nd} 天） 浮动盈亏 "
                  f"{t['pnl']:+,.0f} 元 {t['ret'] * 100:+.2f}%")
        else:
            print(f"      卖出 {t['exit_d']} 开盘价 {t['exit_p']:.4f} 卖 {t['exit_q']:,} 股 "
                  f"金额 {t['exit_amt']:,.0f} 元 持有 {td} 个交易日（自然日 {nd} 天） "
                  f"盈亏 {t['pnl']:+,.0f} 元 {t['ret'] * 100:+.2f}%")
        det = []
        if why == "regime":
            det.append(f"决策日 {t['dec_d']} 510300 收 {t['b_close_dec']:.4f} < MA60 "
                       f"{t['b_ma_dec']:.4f}（低 {abs(t['b_close_dec'] / t['b_ma_dec'] - 1) * 100:.2f}%）")
        elif why == "buffer":
            det.append(f"决策日 {t['dec_d']} 动量分位 {t['sell_pct'] * 100:.1f}% < 70%")
        elif why == "rot":
            det.append(f"决策日 {t['dec_d']} 动量分位 {t['sell_pct'] * 100:.1f}% 仍在缓冲带内，"
                       f"被同指数去重/赛道上限/持仓只数挤出")
        elif why == "data":
            det.append(f"决策日 {t['dec_d']} 动量分位缺失")
        elif why == "eod":
            det.append(f"窗口最后一个交易日 {t['exit_d']} 收盘，非策略卖出")
        if why in ("rot", "buffer", "data"):
            _nb = [x for x in buys_by_day.get(t["exit_d"], []) if x is not t]
            if _nb:
                det.append("同日开盘换入 " + "、".join(
                    f"{x['code'][2:]} {x['name']}（分位 {x['entry_pct_dec'] * 100:.1f}%）"
                    for x in _nb))
        print(f"      卖出原因：{RSN.get(why, why)}" + ("；" + "；".join(det) if det else ""))
        if np.isfinite(t["b_close"]) and np.isfinite(t["b_ma"]) and t["b_ma"] > 0:
            _dd = (t["b_close"] / t["b_ma"] - 1) * 100
            print(f"      卖出日 510300 收 {t['b_close']:.4f} / MA60 {t['b_ma']:.4f}"
                  f"（{'高' if _dd >= 0 else '低'} {abs(_dd):.2f}%）")
        print("")

    eq0, eq1 = float(eq.iloc[0]), float(eq.iloc[-1])
    hold_days = int((hn > 0).sum())
    total_days = int(len(eq))
    s_pnl = sum(t["pnl"] for t in legs)
    closed = [t for t in legs if t["reason"] != "eod"]
    s_closed = sum(t["pnl"] for t in closed)
    print("-" * 78)
    print(f"总览  {prm.start} → {prm.end}")
    print(f"  交易日 {total_days} 天；起始资金 {eq0:,.0f} 元（窗口起点空仓起步，不继承窗口外持仓）")
    if legs:
        _last = max((t["exit_d"] for t in closed), default="—")
        print(f"  第一笔买入 {legs[0]['entry_d']}；最后一笔真实卖出 {_last}"
              + (f"；期末 {legs[-1]['exit_d']} 仍持有 {len(legs) - len(closed)} 只（按收盘价估值）"
                 if len(closed) < len(legs) else ""))
    print(f"  在仓 {hold_days} 天 / 总 {total_days} 天 = {hold_days / total_days * 100:.1f}%"
          f"（空仓 {total_days - hold_days} 天）")
    print(f"  成交 {len(legs)} 笔（已平仓 {len(closed)} 笔 + 期末仍持有 {len(legs) - len(closed)} 笔）")
    print(f"  已平仓 {len(closed)} 笔盈亏加总 {s_closed:+,.0f} 元（算术加总，不复利）")
    if len(closed) < len(legs):
        print(f"  含期末持仓浮盈的 {len(legs)} 笔加总 {s_pnl:+,.0f} 元（浮盈 "
              f"{s_pnl - s_closed:+,.0f} 元，按 {legs[-1]['exit_d']} 收盘价估值）")
    print(f"  复利累计 {eq1 / eq0 - 1:+.2%}（期末权益 {eq1:,.0f} 元，与 --backtest 输出一致）")
    eps = []
    for k, ds in enumerate(offd):
        i = int(idx.searchsorted(pd.Timestamp(ds)))
        if eps and i == eps[-1][-1][0] + 1:
            eps[-1].append((i, ds))
        else:
            eps.append([(i, ds)])
    if eps:
        print(f"  空仓开关触发 {len(eps)} 次（510300 收盘 < MA60 的连续区间，共 {len(offd)} 个交易日）：")
        for k, e in enumerate(eps, 1):
            _st, _ed = e[0][1], e[-1][1]
            sold = [t for t in legs if t["reason"] == "regime" and t["dec_d"] in [_x[1] for _x in e]]
            tail = ("，次日开盘清仓 " + "、".join(f"{t['code'][2:]} {t['name']}" for t in sold)
                    if sold else "，当时已空仓（无仓位可清）")
            print(f"    {k}) {_st} → {_ed} 共 {len(e)} 个交易日{tail}")
    else:
        print("  空仓开关触发 0 次（窗口内 510300 始终在 MA60 上方）")
    print("-" * 78)
    p = os.path.join(DUMPS, f"trades_{prm.start}_{prm.end}.json")
    json.dump(dict(start=prm.start, end=prm.end, eq0=eq0, eq1=eq1, cups=eq1 / eq0 - 1,
                   hold_days=hold_days, total_days=total_days,
                   pnl_sum=s_pnl, pnl_sum_closed=s_closed,
                   regime_episodes=[dict(start=e[0][1], end=e[-1][1], days=len(e)) for e in eps],
                   legs=[{k: (None if isinstance(v, float) and not np.isfinite(v) else v)
                          for k, v in t.items()} for t in legs]),
              open(p, "w"), ensure_ascii=False, indent=1, default=str)
    print("明细已写", p)



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--caps", action="store_true", help="主题上限 5/6/8/不限 × 3y/5y 对照")
    ap.add_argument("--index-map", action="store_true",
                    help="同指数分组审计（标的指数 → ETF 清单 + 组内相关系数验证）")
    ap.add_argument("--corr-map", action="store_true",
                    help="相关性聚类审计（簇结构 + 新目标清单两两 ρ 最大值）")
    ap.add_argument("--abc", action="store_true", help="A/B/C 三档 × 3y/5y + 阈值敏感性")
    ap.add_argument("--check-days", action="store_true",
                    help="决策日序列校验（回测 run() vs 全局日历独立重算，前10/后10 + T+1）")
    ap.add_argument("--phases", action="store_true",
                    help="每 N 日决策的 N 个相位 × 3y/5y（最好/中位/最差）")
    ap.add_argument("--pod", action="store_true",
                    help="单吊赛道四个版本(V1参照/V2单吊/V3强制单吊/V4集中4只) × 3y/5y 全指标")
    ap.add_argument("--guard", action="store_true",
                    help="V2 单吊下的护栏消融（熔断 6/8/10% + 赛道崩塌半仓）")
    ap.add_argument("--win", action="store_true", help="1y/2y/3y/5y 四窗口年化")
    ap.add_argument("--dd", action="store_true", help="历史上最惨一段（峰值/谷底/回本/持有赛道）")
    ap.add_argument("--trades", action="store_true",
                    help="逐笔成交明细（只读导出：打印 + 写 dumps/trades_START_END.json）")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--theme-cap", type=int, default=None)
    ap.add_argument("--index-cap", type=int, default=None, help="同一标的指数最多几只（0=关）")
    ap.add_argument("--corr-cap", type=int, default=None, help="同一相关性簇最多几只（0=关）")
    ap.add_argument("--corr-thr", type=float, default=None, help="相关性聚类阈值（默认 0.90）")
    ap.add_argument("--n-hold", type=int, default=None, help="目标持仓只数（默认 8）")
    ap.add_argument("--theme-mode", default=None, help="cap|top1（top1=强制全仓动量第一的赛道）")
    ap.add_argument("--guard-day", type=float, default=None, help="单日亏损熔断阈值（0=关）")
    ap.add_argument("--guard-theme", type=float, default=None,
                    help="赛道崩塌阈值（0=关，0.5=过半破MA20）")
    ap.add_argument("--cost", type=float, default=None)
    a = ap.parse_args()
    cfg = load_cfg()
    if a.theme_cap is not None:
        cfg["theme_cap"] = int(a.theme_cap)
    if a.index_cap is not None:
        cfg["index_cap"] = int(a.index_cap)
    if a.corr_cap is not None:
        cfg["corr_cluster_cap"] = int(a.corr_cap)
    if a.corr_thr is not None:
        cfg["corr_threshold"] = float(a.corr_thr)
    if a.n_hold is not None:
        cfg["n_hold"] = int(a.n_hold)
    if a.theme_mode is not None:
        cfg["theme_mode"] = str(a.theme_mode)
    if a.guard_day is not None:
        cfg["guard_day"] = float(a.guard_day)
    if a.guard_theme is not None:
        cfg["guard_theme"] = float(a.guard_theme)
    if a.cost is not None:
        cfg["cost"] = float(a.cost)
    os.makedirs(DUMPS, exist_ok=True)

    if a.pod or a.guard or a.win or a.dd:
        out = {}
        if a.dd:
            out["dd"] = dd_report(cfg)
        if a.pod:
            out["pod"] = pod_report(cfg)
        if a.guard:
            out["guard"] = guard_report(cfg)
        if a.win:
            v = POD_VARIANTS[:2] if not a.guard_day and not a.guard_theme else [
                ("V2+护栏", dict(theme_cap=0, index_cap=1,
                                 guard_day=float(cfg["guard_day"]),
                                 guard_theme=float(cfg["guard_theme"])))]
            out["win"] = windows_report(cfg, variants=v)
        p = os.path.join(DUMPS, f"pod_{datetime.now():%Y%m%d_%H%M}.json")
        json.dump(out, open(p, "w"), ensure_ascii=False, indent=1, default=float)
        print("\n写完", p)
        return 0

    if a.check_days:
        check_days(cfg, s0=a.start, s1=a.end)
        return 0

    if a.phases:
        out = phases_report(cfg)
        json.dump(out, open(os.path.join(DUMPS, "phases.json"), "w"),
                  ensure_ascii=False, indent=1, default=float)
        print("\n写完", os.path.join(DUMPS, "phases.json"))
        return 0

    if a.index_map:
        index_map_report(cfg)
        return 0

    if a.corr_map:
        corr_map_report(cfg)
        return 0

    if a.abc:
        out = abc_report(cfg)
        json.dump(out, open(os.path.join(DUMPS, "abc.json"), "w"),
                  ensure_ascii=False, indent=1, default=float)
        print("\n写完", os.path.join(DUMPS, "abc.json"))
        return 0

    if a.caps:
        rows = caps_report(cfg)
        best = pick_cap(rows)
        if best:
            print(f"\n[推荐] 上限={best['cap'] or '不限'}（{best['window']} 年化{best['cagr'] * 100:.2f}% "
                  f"最坏单日{best['worst1'] * 100:.2f}%）")
        json.dump(rows, open(os.path.join(DUMPS, "caps.json"), "w"),
                  ensure_ascii=False, indent=1, default=float)
        return 0

    if a.trades:
        P, D, mask, pct = prep()
        prm = replace(params_from_cfg(cfg, start=a.start or W3Y[0],
                                      end=a.end or str(P["idx"][-1].date())))
        trades_report(run(P, D, mask, pct, prm), prm)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
