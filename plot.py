#!/usr/bin/env python3
"""
Make graphs from sweep_results/all_runs.csv (created by sweep.py).

Run:  python3 plot.py
Needs matplotlib:  sudo apt install -y python3-matplotlib
Output (PNG) in sweep_results/:
  1_tcp_vs_background.png   TCP throughput vs background load
  2_rtt_vs_background.png   RTT under load vs background load
  3_wifi_tcp_vs_loss.png    Wi-Fi TCP throughput vs Wi-Fi loss (+ Mathis model)
"""
import csv
import math
import os
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sweep_results")
CSV = os.path.join(OUT, "all_runs.csv")

COLORS = {"wifi": "#2a78d6", "5g": "#eb6834"}   # categorical slots 1, 2
LABELS = {"wifi": "Wi-Fi", "5g": "5G"}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e6e5e1"


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load():
    with open(CSV) as fh:
        return list(csv.DictReader(fh))


def mean_by(rows, sweep, xkey, ykey):
    """{path: [(x, mean_y), ...]} averaged over repeats."""
    acc = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["sweep"] != sweep:
            continue
        x, y = num(r[xkey]), num(r[ykey])
        if x is not None and y is not None:
            acc[r["path"]][x].append(y)
    return {p: sorted((x, sum(v) / len(v)) for x, v in d.items())
            for p, d in acc.items()}


def style(ax, title, xlabel, ylabel):
    ax.set_title(title, loc="left", fontsize=13, color=INK, pad=12)
    ax.set_xlabel(xlabel, color=MUTED)
    ax.set_ylabel(ylabel, color=MUTED)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED)
    ax.set_ylim(bottom=0)


def line_chart(series, fname, title, xlabel, ylabel, extra=None):
    fig, ax = plt.subplots(figsize=(7, 4.2), dpi=150)
    for path in ("wifi", "5g"):
        pts = series.get(path)
        if not pts:
            continue
        xs, ys = zip(*pts)
        ax.plot(xs, ys, color=COLORS[path], linewidth=2, marker="o",
                markersize=6, markeredgecolor="white", markeredgewidth=1.5,
                label=LABELS[path])
        ax.annotate(f"{LABELS[path]}  {ys[-1]:.1f}", (xs[-1], ys[-1]),
                    xytext=(8, 0), textcoords="offset points",
                    va="center", color=INK, fontsize=9)
    if extra:
        extra(ax)
    style(ax, title, xlabel, ylabel)
    ax.legend(frameon=False, loc="upper right", labelcolor=INK)
    ax.margins(x=0.12)
    fig.tight_layout()
    path = os.path.join(OUT, fname)
    fig.savefig(path)
    plt.close(fig)
    print("saved", path)


def main():
    rows = load()

    line_chart(mean_by(rows, "bg", "bg_share_pct", "fg_tcp_mbps"),
               "1_tcp_vs_background.png",
               "TCP throughput falls as background load grows",
               "Background UDP load (% of path capacity)",
               "TCP throughput (Mbit/s)")

    line_chart(mean_by(rows, "bg", "bg_share_pct", "loaded_rtt_ms"),
               "2_rtt_vs_background.png",
               "Round-trip time under load",
               "Background UDP load (% of path capacity)",
               "Average RTT (ms)")

    wifi = {"wifi": mean_by(rows, "wifiloss", "wifi_loss_pct", "fg_tcp_mbps").get("wifi", [])}
    rtts = mean_by(rows, "wifiloss", "wifi_loss_pct", "loaded_rtt_ms").get("wifi", [])
    # Use the RTT of the lossy runs only: with 0 % loss TCP fills the queue
    # (bufferbloat) and that RTT is not representative.
    lossy = sorted(r for x, r in rtts if x > 0)
    rtt_s = (lossy[len(lossy) // 2] / 1000) if lossy else 0.02

    def mathis(ax):
        # Mathis et al.: throughput ~ (MSS / RTT) * 1.22 / sqrt(p)
        xs = [x / 10 for x in range(3, 21)]           # 0.3 % .. 2 %
        ys = [min(52, 1448 * 8 / rtt_s * 1.22 / math.sqrt(x / 100) / 1e6) for x in xs]
        ax.plot(xs, ys, color=MUTED, linestyle="--", linewidth=1.5,
                label=f"Mathis model, TCP Reno (RTT {rtt_s*1000:.0f} ms)")

    line_chart(wifi, "3_wifi_tcp_vs_loss.png",
               "Wi-Fi: packet loss limits TCP, not capacity",
               "Wi-Fi packet loss (%)", "TCP throughput (Mbit/s)", extra=mathis)


if __name__ == "__main__":
    main()
