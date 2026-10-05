#!/usr/bin/env python3
"""
Time-varying background traffic on the Wi-Fi + 5G topology.

Every STEP seconds the background UDP load on each path jumps to a new random
level (default 30-90 % of capacity, about 60 % on average). Wi-Fi and 5G change
independently. A TCP flow and ping run on each path the whole time, and a live
table shows second by second how TCP and RTT react.

Run:   sudo python3 vary_traffic.py                    # 60 s, new level every 5 s
       sudo python3 vary_traffic.py --duration 120 --step 10 --seed 7
Output: vary_results/timeseries.csv, vary_results/vary_throughput.png,
        vary_results/vary_rtt.png (graphs need python3-matplotlib)
"""
import argparse
import csv
import os
import random
import re
import subprocess
import time

from mininet.log import setLogLevel, info

import netconfig
import wifi_5g_topology as topo

PATHS = topo.PATHS
NAMES = ("wifi", "5g")
LABEL = {"wifi": "Wi-Fi", "5g": "5G"}

IPERF_RE = re.compile(r"\]\s+([\d.]+)-([\d.]+)\s+sec\s+[\d.]+\s+\w?Bytes\s+"
                      r"([\d.]+)\s+([KMG]?)bits/sec")
PING_RE = re.compile(r"^\[([\d.]+)\].*time=([\d.]+) ms")
UNIT = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}


# ---------------------------------------------------------------- parsing ---
def read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def iperf_intervals(text):
    """[(end_second, mbit_s)] for the 1-second interval lines of an iperf3 client."""
    out = []
    for line in text.splitlines():
        if "sender" in line or "receiver" in line:
            continue
        m = IPERF_RE.search(line)
        if m and float(m.group(2)) - float(m.group(1)) <= 1.5:
            out.append((float(m.group(2)), float(m.group(3)) * UNIT[m.group(4)]))
    return out


def ping_samples(text):
    """[(unix_time, rtt_ms)]"""
    out = []
    for line in text.splitlines():
        m = PING_RE.search(line)
        if m:
            out.append((float(m.group(1)), float(m.group(2))))
    return out


def step_times(text):
    return [float(x) for x in text.split() if x.strip()]


# ------------------------------------------------------------ experiment ---
def make_schedule(steps, lo, hi, rng):
    return [round(rng.uniform(lo, hi), 2) for _ in range(steps)]


def bar(value, cap, width=12):
    n = 0 if not value or not cap else min(width, round(width * value / cap))
    return "#" * n + "." * (width - n)


def run(net, duration, step, lo, hi, seed, outdir):
    sta1, srv = net.get("sta1"), net.get("srv")
    os.makedirs(outdir, exist_ok=True)
    rng = random.Random(seed)
    nsteps = -(-duration // step)
    sched = {n: make_schedule(nsteps, lo, hi, rng) for n in NAMES}

    srv.cmd("pkill -9 iperf3")
    for p in PATHS.values():
        srv.cmd(f"iperf3 -s -D -p {p['bg_port']}")
        srv.cmd(f"iperf3 -s -D -p {p['fg_port']}")
    for p in PATHS.values():                      # warm-up (ARP)
        sta1.cmd(f"ping -c 3 -i 0.2 {p['server_ip']}")
    time.sleep(1)

    # Background: one shell loop per path, each step is a short iperf3 UDP run
    for n in NAMES:
        p = PATHS[n]
        lines = []
        for share in sched[n]:
            rate = max(1, int(p["bw"] * share))
            lines.append(f"date +%s.%N >> {outdir}/bg_{n}_times.txt")
            lines.append(f"iperf3 -c {p['server_ip']} -p {p['bg_port']} -u "
                         f"-b {rate}M -t {step} > /dev/null 2>&1")
        script = os.path.join(outdir, f"bg_{n}.sh")
        with open(script, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        open(f"{outdir}/bg_{n}_times.txt", "w").close()

    t0 = time.time()
    for n in NAMES:
        p = PATHS[n]
        sta1.cmd(f"bash {outdir}/bg_{n}.sh &")
        sta1.cmd(f"date +%s.%N > {outdir}/fg_{n}_start.txt; "
                 f"iperf3 -c {p['server_ip']} -p {p['fg_port']} -t {duration} "
                 f"-i 1 --forceflush > {outdir}/fg_{n}.log 2>&1 &")
        sta1.cmd(f"stdbuf -oL ping -D -i 0.5 -w {duration} {p['server_ip']} "
                 f"> {outdir}/ping_{n}.log 2>&1 &")

    # ---- live view -------------------------------------------------------
    info("\n*** LIVE: background changes every %ds (%.0f-%.0f %% of capacity)\n"
         % (step, lo * 100, hi * 100))
    hdr = (f"{'t(s)':>4} | {'Wi-Fi bg':>8} {'TCP':>6} {'RTT':>6} {'':12} | "
           f"{'5G bg':>8} {'TCP':>6} {'RTT':>6} {'':12}")
    info(hdr + "\n" + "-" * len(hdr) + "\n")
    for sec in range(1, duration + 3):
        time.sleep(max(0, t0 + sec - time.time()))
        cells = []
        for n in NAMES:
            cap = PATHS[n]["bw"]
            k = min(nsteps - 1, int((sec - 1) // step))
            bg = cap * sched[n][k]
            iv = iperf_intervals(read(f"{outdir}/fg_{n}.log"))
            tcp = iv[-1][1] if iv else None
            pg = ping_samples(read(f"{outdir}/ping_{n}.log"))
            rtt = pg[-1][1] if pg else None
            cells.append(f"{bg:6.0f}Mb {tcp if tcp is not None else 0:6.1f} "
                         f"{rtt if rtt is not None else 0:5.1f}ms {bar(tcp, max(cap - bg, 1))}")
        info(f"{sec:4d} | {cells[0]} | {cells[1]}\n")
    time.sleep(3)
    srv.cmd("pkill -9 iperf3")
    sta1.cmd("pkill -9 iperf3; pkill -9 ping")

    # ---- build the per-second time series ---------------------------------
    rows = []
    series = {}
    for n in NAMES:
        cap = PATHS[n]["bw"]
        steps = step_times(read(f"{outdir}/bg_{n}_times.txt"))
        start = step_times(read(f"{outdir}/fg_{n}_start.txt"))
        fg0 = start[0] if start else t0
        tcp = {round(fg0 + end - t0): v for end, v in
               iperf_intervals(read(f"{outdir}/fg_{n}.log"))}
        rtt = {}
        for ts, r in ping_samples(read(f"{outdir}/ping_{n}.log")):
            rtt.setdefault(int(ts - t0) + 1, []).append(r)
        bg = {}
        for sec in range(1, duration + 1):
            k = sum(1 for s in steps if s - t0 <= sec - 0.5) - 1
            k = max(0, min(k, nsteps - 1))
            bg[sec] = cap * sched[n][k]
        series[n] = (bg, tcp, rtt)
    for sec in range(1, duration + 1):
        row = {"t_s": sec}
        for n in NAMES:
            bg, tcp, rtt = series[n]
            row[f"{n}_bg_mbps"] = round(bg[sec], 1)
            row[f"{n}_tcp_mbps"] = round(tcp[sec], 2) if sec in tcp else ""
            row[f"{n}_rtt_ms"] = (round(sum(rtt[sec]) / len(rtt[sec]), 2)
                                  if sec in rtt else "")
        rows.append(row)
    csv_path = os.path.join(outdir, "timeseries.csv")
    with open(csv_path, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    info(f"\n*** Time series saved: {csv_path}\n")
    return csv_path


# ----------------------------------------------------------------- graphs ---
def plot(csv_path, outdir):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        info("*** matplotlib missing: sudo apt install -y python3-matplotlib\n")
        return
    with open(csv_path) as fh:
        rows = list(csv.DictReader(fh))
    color = {"wifi": "#2a78d6", "5g": "#eb6834"}
    bgcol, ink, muted, grid = "#c3c2b7", "#0b0b0b", "#52514e", "#e6e5e1"

    def num(v):
        return float(v) if v not in ("", None) else None

    def tidy(ax):
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(grid)
        ax.grid(axis="y", color=grid, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(colors=muted)
        ax.set_ylim(bottom=0)

    t = [int(r["t_s"]) for r in rows]
    fig, axes = plt.subplots(2, 1, figsize=(8, 6), dpi=150, sharex=True)
    for ax, n in zip(axes, NAMES):
        cap = PATHS[n]["bw"]
        bg = [num(r[f"{n}_bg_mbps"]) for r in rows]
        tcp = [num(r[f"{n}_tcp_mbps"]) for r in rows]
        ax.fill_between(t, bg, step="mid", color=bgcol, alpha=0.6,
                        label="Background UDP load")
        ax.plot(t, tcp, color=color[n], linewidth=2, label=f"{LABEL[n]} TCP")
        ax.axhline(cap, color=muted, linestyle="--", linewidth=1)
        ax.text(t[-1], cap, f" capacity {cap}", va="bottom", ha="right",
                color=muted, fontsize=8)
        ax.set_title(f"{LABEL[n]} path", loc="left", color=ink, fontsize=11)
        ax.set_ylabel("Mbit/s", color=muted)
        tidy(ax)
        ax.set_ylim(0, cap * 1.12)
        ax.legend(frameon=False, loc="upper left", fontsize=8, labelcolor=ink, ncol=2)
    axes[-1].set_xlabel("Time (s)", color=muted)
    fig.suptitle("TCP throughput as background traffic varies", x=0.02,
                 ha="left", color=ink, fontsize=13)
    fig.tight_layout()
    p1 = os.path.join(outdir, "vary_throughput.png")
    fig.savefig(p1)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=150)
    for n in NAMES:
        ax.plot(t, [num(r[f"{n}_rtt_ms"]) for r in rows], color=color[n],
                linewidth=2, label=LABEL[n])
    ax.set_title("Round-trip time as background traffic varies", loc="left",
                 color=ink, fontsize=13)
    ax.set_xlabel("Time (s)", color=muted)
    ax.set_ylabel("RTT (ms)", color=muted)
    tidy(ax)
    ax.legend(frameon=False, labelcolor=ink)
    fig.tight_layout()
    p2 = os.path.join(outdir, "vary_rtt.png")
    fig.savefig(p2)
    plt.close(fig)
    subprocess.run(["chmod", "a+rw", p1, p2])
    info(f"*** Graphs: {p1}\n            {p2}\n")


def main():
    ap = argparse.ArgumentParser(description="Wi-Fi + 5G with time-varying background traffic")
    ap.add_argument("--config", default=None, help="config file (default config.ini)")
    ap.add_argument("--duration", type=int, default=None, help="override duration_s")
    ap.add_argument("--step", type=int, default=None, help="override step_s")
    ap.add_argument("--min", type=float, default=None, help="override min_share")
    ap.add_argument("--max", type=float, default=None, help="override max_share")
    ap.add_argument("--seed", type=int, default=None, help="override seed")
    ap.add_argument("--out", default="vary_results")
    ap.add_argument("--plot-only", action="store_true",
                    help="only redraw graphs from an existing timeseries.csv")
    args = ap.parse_args()
    cfg = netconfig.load(args.config)
    topo.apply_config(cfg)
    for key, ck in (("duration", "duration"), ("step", "bg_step"), ("min", "bg_min"),
                    ("max", "bg_max"), ("seed", "seed")):
        if getattr(args, key) is None:
            setattr(args, key, cfg[ck])
    print(netconfig.summary(cfg) +
          f"  Background: varies {args.min*100:g}-{args.max*100:g} % every {args.step} s\n",
          flush=True)
    setLogLevel("info")
    outdir = os.path.abspath(args.out)
    if args.plot_only:
        plot(os.path.join(outdir, "timeseries.csv"), outdir)
        return
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = topo.build_topology()
    try:
        csv_path = run(net, args.duration, args.step, args.min, args.max,
                       args.seed, outdir)
    finally:
        info("*** Stopping network\n")
        net.stop()
    plot(csv_path, outdir)


if __name__ == "__main__":
    main()
