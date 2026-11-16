#!/usr/bin/env python3
"""E4 appendix: how much of E3's flagship (+5.1pp, t 3.04) was hash-seed luck?

E3's 分批 branch truncated an unordered Python set, so the book depended on PYTHONHASHSEED.
This script re-runs the identical config under several seeds and records the spread.
Usage: for s in 0 1 2 3 5 7 11 42; do PYTHONHASHSEED=$s python3 seed_noise.py $s; done
"""
import json, os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "e3"))
import booklib as B
import e3data, engine

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "e4")
seed = sys.argv[1] if len(sys.argv) > 1 else "?"
d = e3data.build()
keep, f = d["keep"], d["f"]
ret1 = d["fwd"][1].where(keep); valid = keep & ret1.notna()
ew = engine.ew_book(valid, ret1, cost=B.COST)
sc = B.smooth(-B.zr(f), 2)
b = engine.simulate(sc, ret1, valid, N=20, buffer_n=40, minhold=2, partial=0.5, cost=B.COST)
m = B.ev(b, ew)
print(json.dumps({"seed": seed, "ann": m["ann"], "turn": m["turnover"],
                  "excess": m["excess_ann"], "t": m["excess_t"], "mdd": m["mdd"]}))
