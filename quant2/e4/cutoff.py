#!/usr/bin/env python3
"""E4 task 1: re-estimate the flow book with the sample cut at 2024-12-31.

Two separate questions, deliberately kept apart:

(A) SAME BOOK, TWO WINDOWS -- the "2025-26 contribution".  Build the book on the full panel,
    then score weeks < 2025-01-01 and weeks >= 2025-01-01 separately.  No truncation artifact.
(B) REBUILD ON A TRUNCATED PANEL -- the honesty check.  Data ends 2024-12-27, forward returns
    are rebuilt on the truncated price panel, walk-forward test years stop at 2024.  Anything
    the strategy "knew" about 2025-26 (parameter choice, book path) is gone.

All books run on the deterministic engine2 (see engine2.py for why).
"""
import json, os, sys
import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "e3"))
import booklib as B          # noqa: E402
import engine2               # noqa: E402
import e3stats as S          # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "e4")

# name -> (smooth weeks, buffer in N, minhold, partial)
CONFIGS = {
    "flow_lowturn": (2, 40, 2, 1.0),     # smooth2 + buffer2N + hold2  (deterministic, E3 row 9.9%)
    "flow_lowturn_p50": (2, 40, 2, 0.5),  # + 分批换一半 (E3's flagship 9.7% -- see seed note)
    "flow_base": (1, None, 0, 1.0),      # no turnover control (60% weekly turnover)
}


def book_of(c, spec):
    k, buf, hold, p50 = spec
    return engine2.simulate(B.smooth(-B.zr(c["f"]), k), c["ret1"], c["valid"],
                            N=20, buffer_n=buf, minhold=hold, partial=p50, cost=B.COST)


def flow_ic(c, minsample=20):
    out = {}
    keep, fwd = c["keep"], c["fwd"]
    for h in (1, 2, 4):
        out[f"h{h}"] = {}
        for tag2, lo, hi in (("2015-19", "2015-01-01", "2020-01-01"),
                             ("2020-24", "2020-01-01", "2025-01-01"),
                             ("2025-26", "2025-01-01", "2027-01-01")):
            wk = c["weeks"][(c["weeks"] >= pd.Timestamp(lo)) & (c["weeks"] < pd.Timestamp(hi))]
            if len(wk) < minsample:
                continue
            out[f"h{h}"][tag2] = S.ic_stats(S.rank_ic(c["f"].where(keep), fwd[h], weeks=wk))
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    out = {}
    allc = {tag: B.ctx(None if tag == "full" else "2024-12-31") for tag in ("full", "cut2024")}
    books = {}
    print("=" * 116)
    print("(A) SAME BOOK, TWO WINDOWS  -- 2025-26 contribution")
    print(f"{'config':18s} {'window':9s} {'yrs':>4s} {'gross':>7s} {'turn':>6s} {'net_a':>7s} "
          f"{'cagr':>7s} {'mdd':>7s} {'ew':>7s} {'excess':>7s} {'t':>6s}")
    for name, spec in CONFIGS.items():
        b = book_of(allc["full"], spec)
        books[name] = b
        b.to_pickle(os.path.join(OUT, f"book_full_{name}.pkl"))
        w = B.split_windows(b, allc["full"]["ew"])
        full_m = B.ev(b, allc["full"]["ew"])
        out.setdefault("windows", {})[name] = {"pre2025": w["pre2025"], "from2025": w["from2025"],
                                               "full": full_m}
        for tag in ("pre2025", "from2025"):
            s = w[tag]
            print(f"{name:18s} {tag:9s} {s['n_years']:4.1f} {s['gross_ann']:+7.3f} "
                  f"{s['turnover']:6.3f} {s['ann']:+7.3f} {s['cagr']:+7.3f} {s['mdd']:+7.3f} "
                  f"{s['ew_ann']:+7.3f} {s['excess_ann']:+7.3f} {s['excess_t']:+6.2f}")
        print(f"{name:18s} {'FULL':9s} {full_m['n_weeks']/52:4.1f} {full_m['gross_ann']:+7.3f} "
              f"{full_m['turnover']:6.3f} {full_m['ann']:+7.3f} {full_m['cagr']:+7.3f} "
              f"{full_m['mdd']:+7.3f} {full_m['ew_ann']:+7.3f} {full_m['excess_ann']:+7.3f} "
              f"{full_m['excess_t']:+6.2f}")

    print("-" * 116)
    print("2025-26 CONTRIBUTION (annualised pp, same book)")
    out["contribution"] = {}
    for name in CONFIGS:
        w = out["windows"][name]
        pre, post = w["pre2025"], w["from2025"]
        out["contribution"][name] = {
            "net_pre": pre["ann"], "net_post": post["ann"],
            "ex_pre": pre["excess_ann"], "ex_post": post["excess_ann"],
            "ex_total_pre": pre["excess_total"], "ex_total_post": post["excess_total"],
            "share_of_full_excess_from_2025": (post["excess_total"]) /
            (pre["excess_total"] + post["excess_total"]) if (pre["excess_total"] + post["excess_total"]) else np.nan}
        c = out["contribution"][name]
        print(f"  {name:18s} net {pre['ann']:+.3f} (2015-24) vs {post['ann']:+.3f} (2025-26) | "
              f"excess {pre['excess_ann']:+.3f} vs {post['excess_ann']:+.3f} | "
              f"cum excess pre {pre['excess_total']:+.3f} post {post['excess_total']:+.3f} "
              f"-> 2025-26 share {c['share_of_full_excess_from_2025']:.0%}")

    print("-" * 116)
    print("(B) REBUILD ON TRUNCATED PANEL (data ends 2024-12-27, fwd rebuilt)")
    out["cut"] = {}
    for tag, c in allc.items():
        ew_m = B.ev(c["ew"], c["ew"])
        out.setdefault("sample", {})[tag] = {
            "weeks": [str(c["weeks"][0].date()), str(c["weeks"][-1].date())],
            "n_weeks": int(len(c["weeks"])), "ew": ew_m, "flow_ic": flow_ic(c)}
        print(f"[{tag}] {out['sample'][tag]['weeks'][0]}~{out['sample'][tag]['weeks'][1]} "
              f"({out['sample'][tag]['n_weeks']}w)  EW net {ew_m['ann']:+.3f} cagr {ew_m['cagr']:+.3f} "
              f"mdd {ew_m['mdd']:+.3f}")
        for name, spec in CONFIGS.items():
            b = book_of(c, spec)
            m = B.ev(b, c["ew"])
            m["by_year"] = B.by_year(b, c["ew"])
            out["cut"].setdefault(name, {})[tag] = m
            b.to_pickle(os.path.join(OUT, f"book_{tag}_{name}.pkl"))
            print(f"   {name:18s} gross {m['gross_ann']:+.3f} turn {m['turnover']:.3f} "
                  f"cost {m['cost_ann']:+.3f} net {m['ann']:+.3f} cagr {m['cagr']:+.3f} "
                  f"mdd {m['mdd']:+.3f} ex {m['excess_ann']:+.3f} t {m['excess_t']:+.2f}")

    print("-" * 116)
    print("FLOW RANK IC BY PERIOD (data to 2024 only / all weeks)")
    for tag in ("cut2024", "full"):
        print(f"  [{tag}]")
        for h in ("h1", "h2", "h4"):
            cells = [f"{per}: {v['ic']:+.4f}(t{v['t']:+.2f})"
                     for per, v in out["sample"][tag]["flow_ic"][h].items()]
            print(f"    {h}: " + " | ".join(cells))

    print("-" * 116)
    print("WALK-FORWARD (train 3 calendar years -> test next year)")
    out["wf"] = {}
    for tag, c in allc.items():
        folds_to = 2026 if tag == "full" else 2024
        gb, _ = B.grid_books(c)
        w = B.walk_forward(c, gb, test_years=range(2018, folds_to + 1))
        out["wf"][tag] = w
        print(f"  [{tag}] OOS net {w['oos_net_ann']:+.3f} EW {w['oos_ew_ann']:+.3f} "
              f"excess {w['oos_excess_ann']:+.3f} t {w['oos_excess_t']:+.2f} "
              f"sharpe {w['oos_sharpe']:.2f} mdd {w['oos_mdd']:+.3f} "
              f"{w['wins']}W/{w['losses']}L n={w['n_weeks']}")
        for fo in w["folds"]:
            print(f"      {fo['year']} {fo['chosen']:30s} train {fo['train_net']:+.3f} "
                  f"test {fo['test_net']:+.3f} ew {fo['ew_net']:+.3f} turn {fo['test_turn']:.2f}")

    print("-" * 116)
    print("BY YEAR (net / EW), truncated panel vs full")
    for tag in ("cut2024", "full"):
        m = out["cut"]["flow_lowturn"][tag]
        print(f"  [{tag}] " + " ".join(f"{y}:{v['net']:+.2f}/{v['ew']:+.2f}"
                                       for y, v in m["by_year"].items()))

    json.dump(out, open(os.path.join(OUT, "E4_cutoff.json"), "w"), indent=1, ensure_ascii=False, default=float)
    print("\nsaved", os.path.join(OUT, "E4_cutoff.json"))


if __name__ == "__main__":
    main()
