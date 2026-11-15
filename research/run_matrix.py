#!/usr/bin/env python3
"""研究实验矩阵执行器: 固定数据源(腾讯qfq缓存)、固定基线参数, 逐条路线 × 五个窗口。
用法: .venv/bin/python3 research/run_matrix.py <tag> [--only exp1,exp2] [--jobs 5]
结果: research/results/<tag>.json + .csv
"""
import json, os, re, subprocess, sys, time
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "research", "kline")
RES = os.path.join(ROOT, "research", "results")
os.makedirs(RES, exist_ok=True)

BASE = ["--concentrated-pyramid", "--weekly-rotation", "--rotation-interval", "5",
        "--trend-entry", "--init-pct=0.50", "--top-n=3", "--trail=e1",
        "--lookback=10", "--momentum=simple"]
WIDE = "--universe-file=research/pool_wide.json"

WINDOWS = {
    "full": ("2019-01-01", "2026-11-13"),
    "s0":   ("2024-07-01", "2026-06-30"),
    "s0b":  ("2024-08-01", "2026-07-27"),
    "s1":   ("2019-01-01", "2021-12-31"),
    "s2":   ("2022-01-01", "2023-12-31"),
    "s3":   ("2024-01-01", "2026-11-13"),
    "s4":   ("2026-02-14", "2026-11-13"),
}

EXPS = {
    "B0":   BASE,
    "R1":   BASE + [WIDE],
    "R2":   BASE + ["--timing"],
    "R2w":  BASE + [WIDE, "--timing"],
    "R3":   BASE + ["--risk-parity", "--top-n=3"],
    "R3n5": BASE + ["--risk-parity", "--top-n=5"],
    "R4mr": BASE + ["--mr=1.0", "--mr-only", "--mr-z=2.0"],
    "R4":   BASE + ["--mr=0.3", "--mr-z=2.0"],
    "R5i5": BASE + ["--rank-interval=5"],
    "R5i10": BASE + ["--rank-interval=10"],
    "R5h":  BASE + ["--hysteresis=0.2"],
    "R5i5h": BASE + ["--rank-interval=5", "--hysteresis=0.2"],
    "R3ctl": BASE + ["--top-n=5"],
    "R2g":   BASE + ["--timing", "--timing-gross=1.0"],
    "R2L":   BASE + ["--timing", "--timing-levels=0.5,0.8,1.0"],
    "R2tp":  BASE + ["--timing", "--tp-ladder"],
    "R2atr": BASE + ["--timing", "--trail=atr", "--atr-mult=2.5"],
    "R2atrtp": BASE + ["--timing", "--trail=atr", "--atr-mult=2.5", "--tp-ladder"],
    "R2r3":  BASE + ["--timing", "--risk-parity", "--top-n=5"],
    "R2r3tp": BASE + ["--timing", "--risk-parity", "--top-n=5", "--tp-ladder"],
    "R6atr": BASE + ["--trail=atr", "--atr-mult=2.5"],
    "R6atr2": BASE + ["--trail=atr", "--atr-mult=3.5"],
    "R6tp": BASE + ["--tp-ladder"],
    "R6atrtp": BASE + ["--trail=atr", "--atr-mult=2.5", "--tp-ladder"],
    "R35":   BASE + ["--top-n=5", "--rank-interval=5"],
    "R35t":  BASE + ["--top-n=5", "--rank-interval=5", "--timing"],
    "R35tL": BASE + ["--top-n=5", "--rank-interval=5", "--timing", "--timing-levels=0.5,0.8,1.0"],
    "R35tatr": BASE + ["--top-n=5", "--rank-interval=5", "--timing", "--trail=atr", "--atr-mult=2.5"],
    "R35tatrtp": BASE + ["--top-n=5", "--rank-interval=5", "--timing", "--trail=atr", "--atr-mult=2.5", "--tp-ladder"],
    "R35ttp": BASE + ["--top-n=5", "--rank-interval=5", "--timing", "--tp-ladder"],
    "R5t":   BASE + ["--rank-interval=5", "--timing"],
    "R1t5":  BASE + ["--top-n=5", "--rank-interval=5", "--timing", WIDE],
    "R38t":  BASE + ["--top-n=8", "--rank-interval=5", "--timing"],
    "R3ctlt": BASE + ["--top-n=5", "--timing"],
    "R3ctltL": BASE + ["--top-n=5", "--timing", "--timing-levels=0.5,0.8,1.0"],
    "R3ctltp": BASE + ["--top-n=5", "--timing", "--tp-ladder"],
    "R3ctlatr": BASE + ["--top-n=5", "--timing", "--trail=atr", "--atr-mult=2.5"],
    "R3ctltLtp": BASE + ["--top-n=5", "--timing", "--timing-levels=0.5,0.8,1.0", "--tp-ladder"],

}

PAT = {
    "ret": r"总收益:\s*([+\-0-9.]+)%",
    "ann": r"年化收益:\s*([+\-0-9.]+)%",
    "mdd": r"最大回撤:\s*([0-9.]+)%",
    "sharpe": r"夏普比率:\s*([+\-0-9.]+)",
    "calmar": r"Calmar 比率:\s*([+\-0-9.]+)",
    "trades": r"交易笔数:\s*(\d+)",
    "winrate": r"胜率:\s*([0-9.]+)%",
    "expo": r"平均总仓位:\s*([0-9.]+)%",
    "timing": r"择时分档占比:\s*(.+)",
}


def run_one(job):
    tag, exp, win = job
    s, e = WINDOWS[win]
    cmd = [os.path.join(ROOT, ".venv/bin/python3"), os.path.join(ROOT, "backtest_bt.py")] + EXPS[exp] + \
          [f"--start={s}", f"--end={e}"]
    env = dict(os.environ, QR_KLINE_CACHE=CACHE, PYTHONWARNINGS="ignore")
    t0 = time.time()
    p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=1800)
    out = p.stdout + p.stderr
    rec = {"tag": tag, "exp": exp, "win": win, "code": p.returncode, "secs": round(time.time() - t0, 1)}
    for k, pat in PAT.items():
        m = re.search(pat, p.stdout)
        if not m:
            rec[k] = None
        elif k == "timing":
            rec[k] = m.group(1).strip()
        elif k == "trades":
            rec[k] = int(m.group(1))
        else:
            rec[k] = float(m.group(1))
    if p.returncode != 0:
        rec["err"] = out[-1500:]
    return rec


def main():
    tag = sys.argv[1]
    only = None
    jobs = 5
    for a in sys.argv[2:]:
        if a.startswith("--only="):
            only = set(a.split("=", 1)[1].split(","))
        elif a.startswith("--jobs="):
            jobs = int(a.split("=", 1)[1])
    exps = [e for e in EXPS if (only is None or e in only)]
    todo = [(tag, e, w) for e in exps for w in WINDOWS]
    t0 = time.time()
    recs = []
    with ProcessPoolExecutor(max_workers=jobs) as ex:
        for i, r in enumerate(ex.map(run_one, todo), 1):
            recs.append(r)
            flag = "" if r["code"] == 0 else "  <<FAIL>>"
            print(f"[{i}/{len(todo)}] {r['exp']:<7} {r['win']:<5} ret={r['ret']} ann={r['ann']} "
                  f"mdd={r['mdd']} sh={r['sharpe']} {r['secs']}s{flag}", flush=True)
    path = os.path.join(RES, f"{tag}.json")
    with open(path, "w") as f:
        json.dump(recs, f, ensure_ascii=False, indent=1)
    # 宽表
    import pandas as pd
    df = pd.DataFrame(recs)
    piv = df.pivot_table(index="exp", columns="win", values="ann", aggfunc="first")
    piv = piv.reindex([e for e in EXPS if e in set(df["exp"])])
    df.to_csv(os.path.join(RES, f"{tag}.csv"), index=False)
    print(f"\n== 年化(%) 透视 ==\n{piv.round(2).to_string()}")
    print(f"总耗时 {time.time()-t0:.0f}s → {path}")


if __name__ == "__main__":
    main()
