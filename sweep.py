#!/usr/bin/env python3
"""
Run wifi_5g_topology.py many times with different settings and collect
all results into one CSV.

  Sweep A - background load: 0, 20, 40, 60, 80 % (losses as in the assignment)
  Sweep B - Wi-Fi loss:      0, 0.5, 1, 2 %      (background fixed at 60 %)

Run:   sudo python3 sweep.py               # about 10 minutes
       sudo python3 sweep.py --duration 20 --repeats 2
Output: sweep_results/all_runs.csv (+ one folder per run)
Then:  python3 plot.py                     # makes the graphs
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOPO = os.path.join(HERE, "wifi_5g_topology.py")
OUT = os.path.join(HERE, "sweep_results")

BG_VALUES = [0.0, 0.2, 0.4, 0.6, 0.8]
WIFI_LOSS_VALUES = [0.0, 0.5, 1.0, 2.0]
DEFAULT = {"bg": 0.6, "wifi_loss": 1.0, "g5_loss": 0.1}


def run_one(name, bg, wifi_loss, g5_loss, duration):
    outdir = os.path.join(OUT, name)
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cmd = [sys.executable, TOPO, "--out", outdir, "--duration", str(duration),
           "--bg-share", str(bg), "--wifi-loss", str(wifi_loss),
           "--g5-loss", str(g5_loss)]
    print(f"\n=== {name}: bg={bg*100:.0f}%  wifi_loss={wifi_loss}%  "
          f"5g_loss={g5_loss}% ===", flush=True)
    with open(os.path.join(OUT, f"{name}.log"), "w") as log:
        subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    try:
        with open(os.path.join(outdir, "summary.json")) as fh:
            return json.load(fh)
    except Exception:
        print("   !! run failed, see", os.path.join(OUT, f"{name}.log"))
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--repeats", type=int, default=1)
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("Please run with sudo:  sudo python3 sweep.py")
    os.makedirs(OUT, exist_ok=True)

    plan = []
    for bg in BG_VALUES:
        plan.append(("bg", bg, DEFAULT["wifi_loss"], DEFAULT["g5_loss"]))
    for wl in WIFI_LOSS_VALUES:
        plan.append(("wifiloss", DEFAULT["bg"], wl, DEFAULT["g5_loss"]))

    rows = []
    total = len(plan) * args.repeats
    n = 0
    start = time.time()
    for rep in range(1, args.repeats + 1):
        for sweep, bg, wl, gl in plan:
            n += 1
            name = f"{sweep}_bg{int(bg*100)}_wl{wl}_r{rep}"
            print(f"[{n}/{total}]", end="")
            res = run_one(name, bg, wl, gl, args.duration)
            if not res:
                continue
            for path in ("wifi", "5g"):
                r = res.get(path, {})
                rows.append({
                    "sweep": sweep, "repeat": rep, "path": path,
                    "bg_share_pct": int(bg * 100),
                    "wifi_loss_pct": wl, "g5_loss_pct": gl,
                    "capacity_mbps": r.get("capacity_mbps"),
                    "bg_achieved_mbps": r.get("bg_achieved_mbps"),
                    "fg_tcp_mbps": r.get("fg_tcp_mbps"),
                    "fg_tcp_retransmits": r.get("fg_tcp_retransmits"),
                    "idle_rtt_ms": r.get("idle", {}).get("rtt_avg_ms"),
                    "loaded_rtt_ms": r.get("loaded", {}).get("rtt_avg_ms"),
                })
                print(f"   {path:>4}: TCP {rows[-1]['fg_tcp_mbps']} Mb/s, "
                      f"RTT {rows[-1]['loaded_rtt_ms']} ms", flush=True)

    csv_path = os.path.join(OUT, "all_runs.csv")
    with open(csv_path, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["empty"])
        wr.writeheader()
        wr.writerows(rows)
    subprocess.run(["chmod", "-R", "a+rwX", OUT])
    print(f"\nDone in {(time.time()-start)/60:.1f} min. Results: {csv_path}")
    print("Next:  python3 plot.py")


if __name__ == "__main__":
    main()
