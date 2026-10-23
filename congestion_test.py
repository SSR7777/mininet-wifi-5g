#!/usr/bin/env python3
"""
Part 8 - Network condition aware sending: the sender MEASURES delay and loss on both paths every
5 seconds, decides the state of each path, and sends

        I frames   -> the BETTER path, on a QUIC stream   (lost pieces are resent)
        P/B frames -> the WORSE path, as QUIC datagrams   (fast, not resent)

  Wi-Fi : 500 Mbit/s, 10 ms, 1 % loss       5G : 700 Mbit/s, 15 ms, 2 % loss
  background UDP traffic (iPerf3): 10 % of each path normally

State of a path in each 5 s window (delay = one-way delay of the probe packets, loss = lost probes):

        good        delay < Td  and  loss < TL
        degraded    delay and loss both rising over the last 30 s, still under Td / TL
        lossy       delay < Td  and  loss > TL
        high delay  delay > Td  and  loss < TL
        congested   delay > Td  and  loss > TL
        (Td = 50 ms, TL = 5 % by default)

  Better path = the one with the better state (good < degraded < lossy / high delay < congested).
  Both good -> I frames on the 5G stream (the main path). Same problem on both -> the lower
  delay/Td + loss/TL.

Three scenarios, each a 14-minute video in 6 phases (the paths take turns being bad):
  congested : background traffic rises from 10 % to 90 % of the path (Wi-Fi, later 5G, at the end both)
  lossy     : loss jumps to 8 %, the delay stays the same
  degraded  : delay and loss creep up slowly during the phase (they cross Td / TL near the end)

Compared with:
  fixed     no switching: I frames always on the 5G stream, P/B always on Wi-Fi datagrams
  adaptive  measures every 5 s and switches automatically

Run:   sudo python3 congestion_test.py --video BBB.webm                              # all 3 (about 3 h)
       sudo python3 congestion_test.py --video BBB.webm --scenarios congested        # one (about 1 h)
       sudo python3 congestion_test.py --video BBB.webm --length 120 --no-video      # quick 2-min check
       python3 congestion_test.py --show                                             # tables again
"""
import argparse
import bisect
import csv
import os
import shutil
import subprocess
import time

from mininet.log import setLogLevel, info

import netconfig
import wifi_5g_topology as topo
import video_stream as vs
import players_test as pt
import iframe_test as it
import adaptive_test as at
import long_video_test as lv

PATHS = topo.PATHS
TSP = 188
PORT0 = 4600
PROBE_PORT = {"wifi": 5300, "5g": 5301}
BG_PORT0 = {"wifi": 5400, "5g": 5440}
LABEL = {"wifi": "Wi-Fi", "5g": "5G"}
BASE = {"wifi": {"bw": 500.0, "delay": 10.0, "loss": 1.0},
        "5g":   {"bw": 700.0, "delay": 15.0, "loss": 2.0}}
MAIN = "5g"                      # both paths good -> I frames on this path
RANK = {"good": 0, "degraded": 1, "lossy": 2, "high delay": 2, "congested": 3, "down": 4}
HERE = os.path.dirname(os.path.abspath(__file__))

# Each scenario: 6 phases of equal length; per phase what happens to each path
#   "bg" = background traffic high, "loss" = lossy, "deg" = getting worse, "deg2" = getting worse slowly
SCEN = {
    "congested": {"title": "Congested network (background UDP traffic 10 % -> high)",
                  "phases": [("both normal", {}),
                             ("Wi-Fi congested", {"wifi": "bg"}),
                             ("both normal", {}),
                             ("5G congested", {"5g": "bg"}),
                             ("both normal", {}),
                             ("both congested", {"wifi": "bg", "5g": "bg"})]},
    "lossy":     {"title": "Lossy network (loss jumps, delay stays the same)",
                  "phases": [("both normal", {}),
                             ("Wi-Fi lossy", {"wifi": "loss"}),
                             ("both normal", {}),
                             ("5G lossy", {"5g": "loss"}),
                             ("both normal", {}),
                             ("both lossy", {"wifi": "loss", "5g": "loss"})]},
    "degraded":  {"title": "Degraded network (delay and loss slowly getting worse)",
                  "phases": [("both normal", {}),
                             ("Wi-Fi degrading", {"wifi": "deg"}),
                             ("both normal", {}),
                             ("5G degrading", {"5g": "deg"}),
                             ("both normal", {}),
                             ("both degrading (Wi-Fi faster)", {"wifi": "deg", "5g": "deg2"})]},
}


def other(n):
    return "wifi" if n == "5g" else "5g"


# ---------------------------------------------------------------------------
#  Network conditions over time
# ---------------------------------------------------------------------------
def phase_table(scen, length):
    ph = SCEN[scen]["phases"]
    d = length / len(ph)
    return [(k * d, length if k == len(ph) - 1 else (k + 1) * d, name, what) for k, (name, what) in enumerate(ph)]


def phase_of(t, phases):
    for k, (a, b, *_ ) in enumerate(phases):
        if t < b:
            return k
    return len(phases) - 1


def conditions(t, phases, n, args):
    """(background %, delay ms, loss %) of path n at time t (seconds from the start of the video)."""
    k = phase_of(t, phases)
    a, b, _, what = phases[k]
    kind = what.get(n)
    prev = phases[k - 1][3].get(n) if k > 0 else None
    bg, dly, loss = args.bg_low, BASE[n]["delay"], BASE[n]["loss"]
    f_ramp = min(1.0, max(0.0, (t - a) / args.ramp)) if args.ramp > 0 else 1.0
    if kind == "bg":
        bg = (args.bg_low + (args.bg_high - args.bg_low) * f_ramp) if prev != "bg" else args.bg_high
    elif prev == "bg":                                          # traffic goes down again
        bg = args.bg_high - (args.bg_high - args.bg_low) * f_ramp
    if kind == "loss":
        loss = args.lossy_pct
    if kind in ("deg", "deg2"):
        f = (t - a) / (b - a) * (1.0 if kind == "deg" else 0.5)
        dly += args.deg_delay * f
        loss += args.deg_loss * f
    return bg, dly, loss


# ---------------------------------------------------------------------------
#  Background traffic: flows of bg_unit % of the path each (iPerf3 UDP, sta1 -> srv)
# ---------------------------------------------------------------------------
class Background:
    def __init__(self, net, unit):
        self.sta1, self.srv = net.get("sta1"), net.get("srv")
        self.unit = unit
        self.on = {"wifi": [], "5g": []}            # [(port, pid)]

    def _start(self, n, idx):
        port = BG_PORT0[n] + idx
        rate = BASE[n]["bw"] * self.unit / 100.0
        self.srv.cmd(f"pkill -9 -f 'iperf3 -s -p {port} -1' ; iperf3 -s -p {port} -1 > /dev/null 2>&1 &")
        time.sleep(0.2)
        out = self.sta1.cmd(f"iperf3 -c {PATHS[n]['server_ip']} -p {port} -u -b {rate:.1f}M -l 1400 -t 4000 "
                            f"> /dev/null 2>&1 & echo $!").split()
        self.on[n].append((port, out[-1] if out and out[-1].isdigit() else None))

    def _stop(self, n):
        port, pid = self.on[n].pop()
        if pid:
            self.sta1.cmd(f"kill -9 {pid} 2>/dev/null")
        self.srv.cmd(f"pkill -9 -f 'iperf3 -s -p {port} -1'")

    def set(self, n, pct):
        want = max(0, int(round(pct / self.unit)))
        while len(self.on[n]) < want:
            self._start(n, len(self.on[n]))
        while len(self.on[n]) > want:
            self._stop(n)

    def level(self, n):
        return len(self.on[n]) * self.unit

    def stop_all(self):
        for n in self.on:
            while self.on[n]:
                self._stop(n)
        self.sta1.cmd("pkill -9 iperf3")
        self.srv.cmd("pkill -9 iperf3")


# ---------------------------------------------------------------------------
#  Probes: one-way delay and loss of each path, read while the video plays
# ---------------------------------------------------------------------------
class ProbeLog:
    def __init__(self, path, pps):
        self.path, self.pps, self.off = path, pps, 0
        self.sent, self.rows = [], []                # rows sorted by send time: (sent, seq, delay ms)

    def poll(self):
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.off)
                data = fh.read()
        except OSError:
            return
        cut = data.rfind(b"\n")
        if cut < 0:
            return
        self.off += cut + 1
        for line in data[:cut].split(b"\n"):
            p = line.split()
            if len(p) != 3:
                continue
            try:
                seq, s, r = int(p[0]), float(p[1]), float(p[2])
            except ValueError:
                continue
            k = bisect.bisect(self.sent, s)
            self.sent.insert(k, s)
            self.rows.insert(k, (s, seq, (r - s) * 1000.0))

    def window(self, a, b):
        """(one-way delay ms, loss %, probes) of the probes SENT in [a, b) (absolute times)."""
        i, j = bisect.bisect_left(self.sent, a), bisect.bisect_left(self.sent, b)
        sel = self.rows[i:j]
        expect = int(round(self.pps * (b - a)))
        if not sel:
            return None, 100.0, expect
        seqs = sorted({x[1] for x in sel})
        expect = max(len(seqs), seqs[-1] - seqs[0] + 1, int(round(0.9 * expect)))
        d = sum(x[2] for x in sel) / len(sel)
        return d, 100.0 * (1 - len(seqs) / expect), expect


def slope(vals):
    """Least-squares slope of vals (per step)."""
    n = len(vals)
    mx, my = (n - 1) / 2.0, sum(vals) / n
    return sum((x - mx) * (y - my) for x, y in enumerate(vals)) / sum((x - mx) ** 2 for x in range(n))


def classify(hist, prev, args):
    """State of a path from its window history [(delay, loss), ...] (last = newest) and its last state."""
    d, l = hist[-1]
    if d is None:
        return "down"
    if d > args.td and l > args.tl:
        return "congested"
    if l > args.tl:
        return "lossy"
    if d > args.td:
        return "high delay"
    n = max(3, int(round(args.deg_span / args.window)))     # trend over the last deg_span seconds
    h = hist[-n:]
    if len(h) == n and all(x[0] is not None for x in h):
        sd = slope([x[0] for x in h]) / args.window          # ms per second
        sl = slope([x[1] for x in h]) / args.window          # % per second
        # delay AND loss rising; a path that was already not good stays degraded while the delay keeps rising
        if sd > args.deg_dd and (sl > args.deg_dl or prev != "good"):
            return "degraded"
    return "good"


def smooth(hist):
    """Average delay / loss of the last 2 windows (less noise for comparing two paths)."""
    h = [x for x in hist[-2:] if x[0] is not None]
    if not h:
        return None, 100.0
    return sum(x[0] for x in h) / len(h), sum(x[1] for x in h) / len(h)


def score(m, args):
    d, l = m.get("ds", m["d"]), m.get("ls", m["l"])
    return (d if d is not None else 1e3) / args.td + l / args.tl


def choose(prev_ip, m, args, memo):
    """Path for the I frames (stream). P/B go on the other path as datagrams.
    memo keeps how many windows in a row the other path was clearly better (same state on both)."""
    r5, rw = RANK[m["5g"]["state"]], RANK[m["wifi"]["state"]]
    if r5 != rw:
        memo["streak"] = 0
        return "5g" if r5 < rw else "wifi"
    if r5 == 0:
        memo["streak"] = 0
        return MAIN
    o = other(prev_ip)                           # same problem on both: move only if clearly better twice
    memo["streak"] = memo.get("streak", 0) + 1 if score(m[o], args) < 0.8 * score(m[prev_ip], args) else 0
    return o if memo["streak"] >= 2 else prev_ip


def describe(ip, pb):
    return f"I frames on the {LABEL[ip]} stream, P/B as {LABEL[pb]} datagrams"


def mtxt(x):
    d = "-" if x["d"] is None else f"{x['d']:.0f} ms"
    return f"{d}, {x['l']:.1f} %, {x['state']}"


def runs_of(chunks):
    out = []
    for a, b, ip, pb in chunks:
        if out and out[-1][2:] == (ip, pb) and abs(out[-1][1] - a) < 1e-6:
            out[-1] = (out[-1][0], b, ip, pb)
        else:
            out.append((a, b, ip, pb))
    return out


# ---------------------------------------------------------------------------
#  One run (one scenario, one variant)
# ---------------------------------------------------------------------------
def run(net, scen, variant, pq, d, A, args):
    sta1, srv = net.get("sta1"), net.get("srv")
    sub = os.path.join(d, variant)
    shutil.rmtree(sub, ignore_errors=True)
    os.makedirs(sub)
    length = A["secs"]
    phases = phase_table(scen, length)
    W = args.window
    nch = max(1, int(round(length / W)))
    bounds = [(k * W, length if k == nch - 1 else (k + 1) * W) for k in range(nch)]
    ptimes = lv.packet_times_all(A["frames"], A["owner"])
    is_i = [o < 0 or A["frames"][o]["type"] == "I" for o in A["owner"]]
    sel_i = [[] for _ in range(nch)]
    sel_pb = [[] for _ in range(nch)]
    for j, t in enumerate(ptimes):
        k = min(nch - 1, int(t // W))
        (sel_i if is_i[j] else sel_pb)[k].append(j)
    data = open(A["ts"], "rb").read()
    lanes = []                                   # (name, packets, chunk, start, length)
    for k, (a, b) in enumerate(bounds):
        for kind, pk in (("I", sel_i[k]), ("PB", sel_pb[k])):
            if pk:
                name = f"c{k:04d}{kind}"
                lanes.append((name, pk, k, a, b - a))
                at.write_part(data, pk, f"{sub}/part_{name}.ts")
    del data, sel_i, sel_pb
    info(f"\n========== {SCEN[scen]['title']} - {length / 60:.1f} min - "
         + ("adaptive: measures delay/loss every %g s, I frames on the better path's stream, "
            "P/B on the worse path's datagrams" % W if variant == "adaptive"
            else "no switching: " + describe("5g", "wifi")) + " ==========\n")
    ports = {}
    for i, (name, *_ ) in enumerate(lanes):
        ports[name] = PORT0 + i
    lo, hi = PORT0, PORT0 + len(lanes)
    lv.range_rules(srv, lo, hi)

    # ---- probes and background traffic
    logs = {}
    for n in ("wifi", "5g"):
        f = f"{sub}/probe_{n}.log"
        srv.cmd(f"python3 {HERE}/probe.py recv {PATHS[n]['server_ip']} {PROBE_PORT[n]} {f} > /dev/null 2>&1 &")
        logs[n] = ProbeLog(f, args.probe_pps)
    time.sleep(0.5)
    for n in ("wifi", "5g"):
        sta1.cmd(f"python3 {HERE}/probe.py send {PATHS[n]['server_ip']} {PROBE_PORT[n]} {args.probe_pps} "
                 f"{int(length) + 60} > /dev/null 2>&1 &")
    bg = Background(net, args.bg_unit)
    applied = {}

    def set_cond(t):
        out = {}
        for n in ("wifi", "5g"):
            b_, dl, ls = conditions(t, phases, n, args)
            if applied.get(n, (None,))[1:] != (round(dl, 1), round(ls, 2)):
                vs.shape(net, n, BASE[n]["bw"], dl, ls)
            bg.set(n, b_)
            applied[n] = (bg.level(n), round(dl, 1), round(ls, 2))
            out[n] = applied[n]
        return out

    cond = set_cond(0.0)
    events = []
    by_chunk = {}
    for name, pk, k, start, ln in lanes:
        events.append((max(0.0, start - 2.0), 1, "server", name))
        by_chunk.setdefault(k, []).append(name)
    for k, (a, b) in enumerate(bounds):
        events.append((a, 2, "decide", k))
    t = 0.0
    while t < length:
        events.append((t, 0, "cond", None))
        t += 1.0
    events.sort(key=lambda e: (e[0], e[1]))
    lane = {l[0]: l for l in lanes}
    pids, scen_, used = {}, {}, {}
    chunks = [None] * nch
    windows = []
    hist = {"wifi": [], "5g": []}
    state_prev = {"wifi": "good", "5g": "good"}
    ip_prev = MAIN
    memo = {}
    time.sleep(3.0)                               # probes and background traffic running before the video
    t0 = time.time() + 2.5
    last_print = -60
    for at_, _, what, x in events:
        while time.time() - t0 < at_:
            time.sleep(0.005)
        if what == "server":
            _, pk, k, start, ln = lane[x]
            os.makedirs(f"{sub}/{x}", exist_ok=True)
            md = "stream" if x.endswith("I") else "dgram"
            sc, ns, _ = pt.video_scenario(md, len(pk) * TSP, ln, slot=it.SLOT)
            scen_[x] = (sc, ns)
            env = f"QUICPERF_VIDEO_IN={sub}/part_{x}.ts QUICPERF_VIDEO_NS={ns} QUICPERF_VIDEO_SLOT={it.SLOT} "
            out = sta1.cmd(f"{env}{pq}/picoquicdemo -0 -p {ports[x]} -c {pq}/certs/cert.pem "
                           f"-k {pq}/certs/key.pem -F {sub}/{x}/perf_server.csv "
                           f"> {sub}/{x}/server.log 2>&1 & echo $!").split()
            pids[x] = out[-1] if out and out[-1].isdigit() else None
        elif what == "decide":
            k = x
            m = {}
            for n in ("wifi", "5g"):
                logs[n].poll()
                dl, ls, npr = logs[n].window(t0 + at_ - W - 0.5, t0 + at_ - 0.5)
                if at_ < W:                                    # nothing measured yet
                    dl, ls = None, 0.0
                if dl is None and at_ < W:
                    m[n] = {"d": None, "l": 0.0, "state": "good", "probes": 0}
                    continue
                hist[n].append((dl, ls))
                ds_, ls_ = smooth(hist[n])
                m[n] = {"d": dl, "l": ls, "ds": ds_, "ls": ls_, "state": classify(hist[n], state_prev[n], args),
                        "probes": npr}
            ip = choose(ip_prev, m, args, memo) if variant == "adaptive" else MAIN
            pb = other(ip)
            chunks[k] = (bounds[k][0], bounds[k][1], ip, pb)
            windows.append({"t": at_, "ip": ip, "pb": pb, **{f"{n}_{q}": m[n][q] for n in m for q in ("d", "l", "state")},
                            "bg_wifi": cond["wifi"][0], "bg_5g": cond["5g"][0],
                            "set_delay_wifi": cond["wifi"][1], "set_delay_5g": cond["5g"][1],
                            "set_loss_wifi": cond["wifi"][2], "set_loss_5g": cond["5g"][2]})
            ts_ = f"{int(at_ // 60):2d}:{int(at_ % 60):02d}"
            for n in ("5g", "wifi"):
                if m[n]["state"] != state_prev[n]:
                    info(f"*** {ts_}  {LABEL[n]} is now {m[n]['state'].upper()} ({mtxt(m[n])})\n")
                    state_prev[n] = m[n]["state"]
            if ip != ip_prev:
                info(f"*** {ts_}  SWITCH -> {describe(ip, pb)}  "
                     f"[5G {mtxt(m['5g'])} | Wi-Fi {mtxt(m['wifi'])}]\n")
            ip_prev = ip
            for name in by_chunk.get(k, []):
                _, pk, _, start, ln = lane[name]
                path = ip if name.endswith("I") else pb
                used[name] = path
                sc, ns = scen_[name]
                srv.cmd(f"(QUICPERF_VIDEO_OUT={sub}/{name}/received_all.ts QUICPERF_VIDEO_LOG={sub}/{name}/pieces.csv "
                        f"QUICPERF_VIDEO_NS={ns} QUICPERF_VIDEO_SLOT={it.SLOT} timeout {int(ln) + 30} "
                        f"{pq}/picoquicdemo -0 -a perf -n test {PATHS[path]['client_ip']} {ports[name]} '{sc}' "
                        f"> {sub}/{name}/client.log 2>&1; touch {sub}/{name}/done) &")
            if at_ - last_print >= 30:
                info(f"*** {ts_}  5G [{mtxt(m['5g'])}, bg {cond['5g'][0]:.0f} %]  "
                     f"Wi-Fi [{mtxt(m['wifi'])}, bg {cond['wifi'][0]:.0f} %]  -> {describe(ip, pb)}\n")
                last_print = at_
        else:
            cond = set_cond(at_)
        for n in list(pids):
            if pids[n] and os.path.exists(f"{sub}/{n}/done"):
                sta1.cmd(f"kill -9 {pids[n]} 2>/dev/null")
                pids[n] = None
    while time.time() - t0 < length + 45 and not all(os.path.exists(f"{sub}/{n}/done") for n in lane):
        time.sleep(0.5)
    for n, pid in pids.items():
        if pid:
            sta1.cmd(f"kill -9 {pid} 2>/dev/null")
    time.sleep(2)
    sta1.cmd(f"pkill -9 -f 'picoquicdemo -0 -p' ; pkill -9 -f probe.py")
    srv.cmd("pkill -9 -f probe.py")
    bg.stop_all()
    for n in ("wifi", "5g"):
        vs.shape(net, n, BASE[n]["bw"], BASE[n]["delay"], BASE[n]["loss"])
    mb = lv.range_bytes(srv, lo, hi)
    lv.range_rules(srv, lo, hi, add=False)

    # ---- what the viewer got
    parts = [(f"{sub}/{n}/received_all.ts", f"{sub}/{n}/pieces.csv", pk) for n, pk, *_ in lanes]
    cov, st = it.rebuild(parts, os.path.getsize(A["ts"]), args.buffer, f"{sub}/live.ts")
    bad_pk, got_pk = lv.check_data(f"{sub}/live.ts", A["ts"], cov)
    if bad_pk:
        info(f"    !!! WARNING: {bad_pk} of {got_pk} received TS packets are not the packets that were sent "
             f"- the results of this run are not valid\n")
    for n, *_ in lanes:
        for f in (f"{sub}/part_{n}.ts", f"{sub}/{n}/received_all.ts"):
            try:
                os.remove(f)
            except OSError:
                pass
    frames = A["frames"]
    dep = it.dependency(frames, cov)
    flags = at.shown_flags(frames, cov)
    p0 = min(f["pts"] for f in frames)
    ftime = [(f["pts"] - p0) / 90000.0 for f in frames]
    delays_i = [x for (n, *_), s in zip(lanes, st) if n.endswith("I") for x in s["delays"]]
    alld = sorted(x for s in st for x in s["delays"])
    sw = [c[0] for p, c in zip(chunks, chunks[1:]) if p and c and p[2] != c[2]]
    r = {"scenario": scen, "variant": variant, "dep": dep, "frames": len(frames), "switches": len(sw),
         "switch_times": sw, "delay_avg": pt.mean(alld), "delay_p95": pt.pctl(alld, 95) if alld else None,
         "delay_i": pt.mean(delays_i),
         "late_pct": 100.0 * sum(1 for x in alld if x > args.buffer) / len(alld) if alld else None,
         "mb_wifi": mb["wifi"], "mb_5g": mb["5g"], "bad_packets": bad_pk, "length": length,
         "chunks": [c for c in chunks if c], "windows": windows}
    fidx = [[] for _ in range(nch)]
    for j, tt in enumerate(ftime):
        fidx[min(nch - 1, int(tt // W))].append(j)

    def summary(a, b):
        ks = [k for k, c in enumerate(chunks) if c and a <= c[0] < b]
        idx = [j for k in ks for j in fidx[k]]
        t5 = sum(chunks[k][1] - chunks[k][0] for k in ks if chunks[k][2] == "5g")
        tw = sum(chunks[k][1] - chunks[k][0] for k in ks if chunks[k][2] == "wifi")
        ii = [j for j in idx if frames[j]["type"] == "I"]
        ws = [w for w in windows if a <= w["t"] < b]

        def avg(key):
            v = [w[key] for w in ws if w[key] is not None]
            return sum(v) / len(v) if v else None

        def most(key):
            v = [w[key] for w in ws]
            return max(set(v), key=v.count) if v else ""
        return {"i_path": "+".join(f"{LABEL[p]}" + (f" {tm:.0f}s" if 0 < tm < (b - a) - 0.5 and t5 and tw else "")
                                   for p, tm in (("5g", t5), ("wifi", tw)) if tm > 0),
                "i_5g_s": t5, "i_wifi_s": tw, "shown": sum(flags[j] for j in idx), "frames": len(idx),
                "i_ok": sum(1 for j in ii if all(cov[q] >= TSP for q in frames[j]["packets"])), "i_total": len(ii),
                "d5": avg("5g_d"), "l5": avg("5g_l"), "dw": avg("wifi_d"), "lw": avg("wifi_l"),
                "s5": most("5g_state"), "sw": most("wifi_state"),
                "bg5": avg("bg_5g"), "bgw": avg("bg_wifi")}

    r["phases"] = [{"phase": k + 1, "start": a, "end": b, "name": nm, **summary(a, b)}
                   for k, (a, b, nm, _) in enumerate(phases)]
    r["minutes"], a = [], 0.0
    while a < length - 0.01:
        b = min(length, a + 60.0)
        r["minutes"].append({"start": a, "end": b, **summary(a, b)})
        a = b
    r["segments"], a = [], 0.0
    while a < length - 0.01:
        b = min(length, a + args.segment)
        r["segments"].append({"start": a, "end": b, **summary(a, b)})
        a = b
    pt.watchable(f"{sub}/live.ts", f"{sub}/live.mp4")
    r["psnr"], r["ssim"], r["damaged"], r["total"] = lv.measure(sub, A["original"], A["ts"])
    I, P, B = dep["I"], dep["P"], dep["B"]
    info(f"    data check: {got_pk - bad_pk}/{got_pk} received packets correct\n")
    info(f"    {len(sw)} switches; I {I[0]}/{I[1]}, P {P[0]}/{P[1]}, B {B[0]}/{B[1]}, GOPs lost {dep['gops_lost']}; "
         f"frames shown {dep['shown']}/{len(frames)}; PSNR {r['psnr'] or '-'} dB; "
         f"delay avg {vs.f1(r['delay_avg'], ' ms')}\n")
    return r


# ---------------------------------------------------------------------------
#  Probe-only test (--no-stream): the network changes and the sender measures and decides,
#  but no video is sent - shows WHEN each state is detected and WHERE the I frames would go
# ---------------------------------------------------------------------------
def run_probe(net, scen, args, d):
    sta1, srv = net.get("sta1"), net.get("srv")
    os.makedirs(d, exist_ok=True)
    length = args.length
    phases = phase_table(scen, length)
    W = args.window
    nch = max(1, int(round(length / W)))
    info(f"\n========== {SCEN[scen]['title']} - {length / 60:.1f} min - NO VIDEO: measures delay/loss every "
         f"{W:g} s and decides where the I and P/B frames would go ==========\n")
    logs = {}
    for n in ("wifi", "5g"):
        f = f"{d}/probe_{n}.log"
        srv.cmd(f"python3 {HERE}/probe.py recv {PATHS[n]['server_ip']} {PROBE_PORT[n]} {f} > /dev/null 2>&1 &")
        logs[n] = ProbeLog(f, args.probe_pps)
    time.sleep(0.5)
    for n in ("wifi", "5g"):
        sta1.cmd(f"python3 {HERE}/probe.py send {PATHS[n]['server_ip']} {PROBE_PORT[n]} {args.probe_pps} "
                 f"{int(length) + 30} > /dev/null 2>&1 &")
    bg = Background(net, args.bg_unit)
    applied = {}

    def set_cond(t):
        out = {}
        for n in ("wifi", "5g"):
            b_, dl, ls = conditions(t, phases, n, args)
            if applied.get(n, (None,))[1:] != (round(dl, 1), round(ls, 2)):
                vs.shape(net, n, BASE[n]["bw"], dl, ls)
            bg.set(n, b_)
            applied[n] = (bg.level(n), round(dl, 1), round(ls, 2))
            out[n] = applied[n]
        return out

    cond = set_cond(0.0)
    events = [(k * W, 2, "decide") for k in range(nch)]
    t = 0.0
    while t < length:
        events.append((t, 0, "cond"))
        t += 1.0
    events.sort()
    hist = {"wifi": [], "5g": []}
    state_prev = {"wifi": "good", "5g": "good"}
    ip_prev, memo = MAIN, {}
    chunks, windows = [], []
    time.sleep(3.0)
    t0 = time.time() + 1.0
    last_print = -60
    try:
        for at_, _, what in events:
            while time.time() - t0 < at_:
                time.sleep(0.005)
            if what == "cond":
                cond = set_cond(at_)
                continue
            m = {}
            for n in ("wifi", "5g"):
                logs[n].poll()
                if at_ < W:
                    m[n] = {"d": None, "l": 0.0, "state": "good"}
                    continue
                dl, ls, _ = logs[n].window(t0 + at_ - W - 0.5, t0 + at_ - 0.5)
                hist[n].append((dl, ls))
                ds_, ls_ = smooth(hist[n])
                m[n] = {"d": dl, "l": ls, "ds": ds_, "ls": ls_, "state": classify(hist[n], state_prev[n], args)}
            ip = choose(ip_prev, m, args, memo)
            pb = other(ip)
            chunks.append((at_, min(length, at_ + W), ip, pb))
            windows.append({"t": at_, "ip": ip, "pb": pb,
                            **{f"{n}_{q}": m[n][q] for n in m for q in ("d", "l", "state")},
                            "bg_wifi": cond["wifi"][0], "bg_5g": cond["5g"][0],
                            "set_delay_wifi": cond["wifi"][1], "set_delay_5g": cond["5g"][1],
                            "set_loss_wifi": cond["wifi"][2], "set_loss_5g": cond["5g"][2]})
            ts_ = f"{int(at_ // 60):2d}:{int(at_ % 60):02d}"
            for n in ("5g", "wifi"):
                if m[n]["state"] != state_prev[n]:
                    info(f"*** {ts_}  {LABEL[n]} is now {m[n]['state'].upper()} ({mtxt(m[n])})\n")
                    state_prev[n] = m[n]["state"]
            if ip != ip_prev:
                info(f"*** {ts_}  SWITCH -> {describe(ip, pb)}  [5G {mtxt(m['5g'])} | Wi-Fi {mtxt(m['wifi'])}]\n")
            ip_prev = ip
            if at_ - last_print >= 30:
                info(f"*** {ts_}  5G [{mtxt(m['5g'])}, bg {cond['5g'][0]:.0f} %]  "
                     f"Wi-Fi [{mtxt(m['wifi'])}, bg {cond['wifi'][0]:.0f} %]  -> {describe(ip, pb)}\n")
                last_print = at_
    finally:
        sta1.cmd("pkill -9 -f probe.py")
        srv.cmd("pkill -9 -f probe.py")
        bg.stop_all()
        for n in ("wifi", "5g"):
            vs.shape(net, n, BASE[n]["bw"], BASE[n]["delay"], BASE[n]["loss"])
    sw = [c[0] for p, c in zip(chunks, chunks[1:]) if p[2] != c[2]]
    r = {"scenario": scen, "variant": "adaptive", "length": length, "chunks": chunks, "windows": windows,
         "switches": len(sw), "switch_times": sw, "segments": []}

    def summary(a, b):
        ws = [w for w in windows if a <= w["t"] < b]

        def avg(key):
            v = [w[key] for w in ws if w[key] is not None]
            return sum(v) / len(v) if v else None

        def most(key):
            v = [w[key] for w in ws]
            return max(set(v), key=v.count) if v else ""
        t5 = sum(W for w in ws if w["ip"] == "5g")
        tw = sum(W for w in ws if w["ip"] == "wifi")
        return {"i_path": "+".join(f"{LABEL[p]}" + (f" {tm:.0f}s" if t5 and tw else "")
                                   for p, tm in (("5g", t5), ("wifi", tw)) if tm > 0),
                "d5": avg("5g_d"), "l5": avg("5g_l"), "dw": avg("wifi_d"), "lw": avg("wifi_l"),
                "s5": most("5g_state"), "sw": most("wifi_state"), "bg5": avg("bg_5g"), "bgw": avg("bg_wifi")}

    r["phases"] = []
    for k, (a, b, nm, what) in enumerate(phases):
        det = []
        for n in what:                                   # how fast the bad path was noticed
            hit = next((w["t"] for w in windows if a <= w["t"] < b and w[f"{n}_state"] != "good"), None)
            det.append(f"{LABEL[n]} {hit - a:.0f} s" if hit is not None else f"{LABEL[n]} not seen")
        swk = [x for x in sw if a <= x < b + W]
        r["phases"].append({"phase": k + 1, "start": a, "end": b, "name": nm,
                            "detected": ", ".join(det) or "-",
                            "switch": ", ".join(f"{int(x // 60)}:{int(x % 60):02d}" for x in swk) or "-",
                            **summary(a, b)})
    r["minutes"], a = [], 0.0
    while a < length - 0.01:
        b = min(length, a + 60.0)
        r["minutes"].append({"start": a, "end": b, **summary(a, b)})
        a = b
    info(f"    {len(sw)} switches of the I frames" + (f" at {' '.join(f'{x:.0f}' for x in sw)} s" if sw else "") + "\n")
    return r


def save_probe(od, r):
    keys = ["t", "ip", "pb", "5g_d", "5g_l", "5g_state", "wifi_d", "wifi_l", "wifi_state", "bg_5g", "bg_wifi",
            "set_delay_5g", "set_delay_wifi", "set_loss_5g", "set_loss_wifi"]
    with open(f"{od}/windows.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["variant"] + keys)
        for x in r["windows"]:
            w.writerow(["probe"] + [fm(x[k], 2) if isinstance(x[k], float) else x[k] for k in keys])
    for name, cols in (("phases", ["phase", "start", "end", "name", "detected", "switch"]),
                       ("minutes", ["start", "end"])):
        cols = cols + ["i_path", "d5", "l5", "s5", "dw", "lw", "sw", "bg5", "bgw"]
        with open(f"{od}/probe_{name}.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for x in r[name]:
                w.writerow([fm(x[c], 2) if isinstance(x[c], float) else x[c] for c in cols])
    with open(f"{od}/probe_summary.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["scenario", "length_s", "switches", "switch_times_s"])
        w.writerow([r["scenario"], fm(r["length"], 0), r["switches"], " ".join(f"{x:.0f}" for x in r["switch_times"])])


def show_probe(base, scens=None):
    import show_results as sr
    rd, num = sr.read_csv, sr._pnum
    found = False
    for scen in (scens or list(SCEN)):
        od = os.path.join(base, scen)
        S = rd(f"{od}/probe_summary.csv")
        if not S:
            continue
        found = True
        dl = lambda x, a, b: f"{num(x[a], 0)} ms {num(x[b], 1)} %" if x.get(a) else "-"
        P = rd(f"{od}/probe_phases.csv")
        sr.table(f"PART 8 (NO VIDEO) - {SCEN[scen]['title'].upper()}  - {S[0]['switches']} switches of the I frames",
                 ["Phase", "Time (s)", "What happens", "5G measured (state)", "Wi-Fi measured (state)",
                  "Detected after", "Switch at", "I frames on"],
                 [[p["phase"], f"{float(p['start']):.0f}-{float(p['end']):.0f}", p["name"],
                   f"{dl(p, 'd5', 'l5')} {p['s5']}", f"{dl(p, 'dw', 'lw')} {p['sw']}", p["detected"], p["switch"],
                   p["i_path"]] for p in P], ["<", ">", "<", "<", "<", "<", "<", ">"])
        M = rd(f"{od}/probe_minutes.csv")
        rows = []
        for i, m in enumerate(M):
            a, b = float(m["start"]), float(m["end"])
            rows.append([f"{i + 1:2d}  ({int(a // 60)}:{int(a % 60):02d}-{int(b // 60)}:{int(b % 60):02d})",
                         f"{num(m['bg5'], 0)} / {num(m['bgw'], 0)}", f"{dl(m, 'd5', 'l5')} {m['s5']}",
                         f"{dl(m, 'dw', 'lw')} {m['sw']}", m["i_path"],
                         {"5G": "Wi-Fi datagrams", "Wi-Fi": "5G datagrams"}.get(m["i_path"], "switched")])
        sr.table(f"MINUTE BY MINUTE (NO VIDEO) - {scen}",
                 ["Minute", "BG % 5G / Wi-Fi", "5G measured (state)", "Wi-Fi measured (state)",
                  "I frames would go on", "P/B would go as"], rows, ["<", ">", "<", "<", ">", ">"])
    if not found:
        print(f"No no-video results in {base}/ - run: sudo python3 congestion_test.py --no-stream")


# ---------------------------------------------------------------------------
#  Results: CSV files, tables, graph, VLC video
# ---------------------------------------------------------------------------
def fm(x, n=1):
    return "" if x is None or x == "" else (f"{x:.{n}f}" if isinstance(x, float) else str(x))


def save(od, runs, args):
    with open(f"{od}/summary.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["scenario", "variant", "length_s", "switches", "switch_times_s", "i_complete", "i_total",
                    "p_complete", "p_total", "b_complete", "b_total", "gops_lost", "gops", "frames_shown", "frames",
                    "frames_damaged", "frames_total", "psnr_db", "ssim", "delay_avg_ms", "delay_p95_ms",
                    "delay_i_ms", "late_pct", "mb_wifi", "mb_5g", "bad_packets", "td_ms", "tl_pct",
                    "window_s", "bg_low", "bg_high"])
        for r in runs:
            dp = r["dep"]
            w.writerow([r["scenario"], r["variant"], fm(r["length"], 0), r["switches"],
                        " ".join(f"{x:.0f}" for x in r["switch_times"]), dp["I"][0], dp["I"][1], dp["P"][0],
                        dp["P"][1], dp["B"][0], dp["B"][1], dp["gops_lost"], dp["gops"], dp["shown"], r["frames"],
                        r["damaged"], r["total"], r["psnr"], r["ssim"], fm(r["delay_avg"]), fm(r["delay_p95"]),
                        fm(r["delay_i"]), fm(r["late_pct"], 2), fm(r["mb_wifi"]), fm(r["mb_5g"]),
                        r["bad_packets"], args.td, args.tl, args.window, args.bg_low, args.bg_high])
    keys = ["t", "ip", "pb", "5g_d", "5g_l", "5g_state", "wifi_d", "wifi_l", "wifi_state", "bg_5g", "bg_wifi",
            "set_delay_5g", "set_delay_wifi", "set_loss_5g", "set_loss_wifi"]
    with open(f"{od}/windows.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["variant"] + keys)
        for r in runs:
            for x in r["windows"]:
                w.writerow([r["variant"]] + [fm(x[k], 2) if isinstance(x[k], float) else x[k] for k in keys])
    for name, key in (("phases", "phases"), ("minutes", "minutes"), ("segments", "segments")):
        cols = ["start", "end"] + (["phase", "name"] if key == "phases" else []) + \
               ["i_path", "i_5g_s", "i_wifi_s", "i_ok", "i_total", "shown", "frames", "d5", "l5", "s5",
                "dw", "lw", "sw", "bg5", "bgw"]
        with open(f"{od}/{name}.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["variant"] + cols)
            for r in runs:
                for x in r[key]:
                    w.writerow([r["variant"]] + [fm(x[c], 2) if isinstance(x[c], float) else x[c] for c in cols])


def show(base, scens=None):
    import show_results as sr
    rd = sr.read_csv
    found = False
    for scen in (scens or list(SCEN)):
        od = os.path.join(base, scen)
        S = rd(f"{od}/summary.csv")
        if not S:
            continue
        found = True
        g = {r["variant"]: r for r in S}
        vs_ = [v for v in ("fixed", "adaptive") if v in g]
        r0 = g[vs_[0]]
        num = sr._pnum
        frac = lambda r, a, b: f"{r[a]} / {r[b]}"
        head = (f"PART 8 - {SCEN[scen]['title'].upper()}  ({float(r0['length_s']) / 60:.0f} min, measured every "
                f"{r0['window_s']} s, Td {r0['td_ms']} ms, TL {r0['tl_pct']} %, background {r0['bg_low']} % -> "
                f"{r0['bg_high']} %)")
        rows = [["I frames go on", *["5G stream always" if v == "fixed" else "better path's stream" for v in vs_]],
                ["P/B frames go on", *["Wi-Fi datagrams always" if v == "fixed" else "worse path's datagrams"
                                       for v in vs_]],
                ["Automatic switches", *[g[v]["switches"] for v in vs_]],
                ["  at (s)", *[(g[v]["switch_times_s"] or "-") for v in vs_]],
                None,
                ["FRAMES THAT ARRIVED IN TIME", *[""] * len(vs_)],
                ["  I frames", *[frac(g[v], "i_complete", "i_total") for v in vs_]],
                ["  P frames", *[frac(g[v], "p_complete", "p_total") for v in vs_]],
                ["  B frames", *[frac(g[v], "b_complete", "b_total") for v in vs_]],
                ["  GOPs lost (I frame missing)", *[frac(g[v], "gops_lost", "gops") for v in vs_]],
                None,
                ["WHAT THE VIEWER SEES", *[""] * len(vs_)],
                ["  Frames shown", *[frac(g[v], "frames_shown", "frames") for v in vs_]],
                ["  Frames damaged (measured)", *[frac(g[v], "frames_damaged", "frames_total") for v in vs_]],
                ["  Picture vs original (PSNR)", *[num(g[v]["psnr_db"], 1, " dB") for v in vs_]],
                ["  Picture similarity (SSIM)", *[num(g[v]["ssim"], 3) for v in vs_]],
                None,
                ["DELAY OF THE VIDEO", *[""] * len(vs_)],
                ["  All video: average / 95 %", *[f"{num(g[v]['delay_avg_ms'], 1)} / {num(g[v]['delay_p95_ms'], 1)} ms"
                                                   for v in vs_]],
                ["  I frames: average", *[num(g[v]["delay_i_ms"], 1, " ms") for v in vs_]],
                ["  Video pieces later than the buffer", *[num(g[v]["late_pct"], 2, " %") for v in vs_]],
                None,
                ["  Video over Wi-Fi / 5G", *[f"{num(g[v]['mb_wifi'], 1)} / {num(g[v]['mb_5g'], 1)} MB" for v in vs_]]]
        sr.table(head, ["Measurement", *[v.capitalize() for v in vs_]], rows)

        def dl(x, a, b):
            return f"{num(x[a], 0)} ms {num(x[b], 1)} %" if x.get(a) else "-"
        P = rd(f"{od}/phases.csv")
        pf = {(p["variant"], p["phase"]): p for p in P}
        prow = []
        for p in [p for p in P if p["variant"] == vs_[-1]]:
            f_ = pf.get(("fixed", p["phase"]))
            ad = pf.get(("adaptive", p["phase"]))
            prow.append([p["phase"], f"{float(p['start']):.0f}-{float(p['end']):.0f}", p["name"],
                         f"{dl(p, 'd5', 'l5')} {p['s5']}", f"{dl(p, 'dw', 'lw')} {p['sw']}",
                         f"{f_['shown']}/{f_['frames']}" if f_ else "-",
                         ad["i_path"] if ad else "-", f"{ad['shown']}/{ad['frames']}" if ad else "-"])
        sr.table(f"PER PHASE - {scen}", ["Phase", "Time (s)", "What happens", "5G measured (state)",
                                         "Wi-Fi measured (state)", "Fixed: shown", "Adaptive: I on",
                                         "Adaptive: shown"], prow, ["<", ">", "<", "<", "<", ">", ">", ">"])
        Mi = rd(f"{od}/minutes.csv")
        mf = {(m["variant"], m["start"]): m for m in Mi}
        mrow = []
        for i, m in enumerate([m for m in Mi if m["variant"] == vs_[-1]]):
            f_ = mf.get(("fixed", m["start"]))
            ad = mf.get(("adaptive", m["start"]))
            a, b = float(m["start"]), float(m["end"])
            pct = lambda x: f"{x['shown']}/{x['frames']} ({100 * int(x['shown']) / max(1, int(x['frames'])):.0f} %)"
            mrow.append([f"{i + 1:2d}  ({int(a // 60)}:{int(a % 60):02d}-{int(b // 60)}:{int(b % 60):02d})",
                         f"{num(m['bg5'], 0)} / {num(m['bgw'], 0)}",
                         f"{dl(m, 'd5', 'l5')} {m['s5']}", f"{dl(m, 'dw', 'lw')} {m['sw']}",
                         pct(f_) if f_ else "-", ad["i_path"] if ad else "-",
                         {"5G": "Wi-Fi datagrams", "Wi-Fi": "5G datagrams"}.get(ad["i_path"], "switched")
                         if ad else "-",
                         pct(ad) if ad else "-"])
        sr.table(f"MINUTE BY MINUTE - {scen}  (measured delay/loss = average of the 5 s windows; state = most often)",
                 ["Minute", "BG % 5G / Wi-Fi", "5G measured (state)", "Wi-Fi measured (state)",
                  "Fixed: shown", "Adaptive: I stream", "Adaptive: P/B", "Adaptive: shown"], mrow,
                 ["<", ">", "<", "<", ">", ">", ">", ">"])
    if not found:
        print(f"No results in {base}/ - run: sudo python3 congestion_test.py --video BBB.webm")


def graph(od, scen, runs, args):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(no graph: sudo apt install -y python3-matplotlib)")
        return None
    ad = next((r for r in runs if r["variant"] == "adaptive"), runs[0])
    ws = ad["windows"]
    t = [w["t"] / 60 for w in ws]
    C = {"5g": "#7b3fe4", "wifi": "#e4823f"}
    with_video = any(r.get("segments") for r in runs)
    fig, ax = plt.subplots(4 if with_video else 3, 1, figsize=(12, 10 if with_video else 8), sharex=True,
                           gridspec_kw={"height_ratios": [2, 2, 0.7, 1.6] if with_video else [2, 2, 0.7]})
    for n in ("5g", "wifi"):
        ax[0].plot(t, [float("nan") if w[f"{n}_d"] is None else w[f"{n}_d"] for w in ws], color=C[n], lw=1.6,
                   label=f"{LABEL[n]} delay")
        ax[1].plot(t, [w[f"{n}_l"] for w in ws], color=C[n], lw=1.6, label=f"{LABEL[n]} loss")
    ax[0].axhline(args.td, color="red", ls="--", lw=1)
    ax[0].text(0.01, args.td, f" Td = {args.td:g} ms", color="red", va="bottom", fontsize=9,
               transform=ax[0].get_yaxis_transform())
    ax[1].axhline(args.tl, color="red", ls="--", lw=1)
    ax[1].text(0.01, args.tl, f" TL = {args.tl:g} %", color="red", va="bottom", fontsize=9,
               transform=ax[1].get_yaxis_transform())
    ax[0].set_ylabel("one-way delay (ms)")
    ax[1].set_ylabel("loss (%)")
    for k, (a, b, nm, _) in enumerate(phase_table(scen, ad["length"])):
        for a_ in ax[:3]:
            a_.axvline(a / 60, color="grey", lw=0.6, ls=":")
        ax[0].text((a + b) / 120, 1.02, nm, ha="center", fontsize=8, color="grey",
                   transform=ax[0].get_xaxis_transform())
    SC = {"good": "#2ca02c", "degraded": "#f2c94c", "lossy": "#f2994a", "high delay": "#f2994a",
          "congested": "#d62728", "down": "black"}
    for w in ws:
        for y, n in ((1.5, "5g"), (0.5, "wifi")):
            ax[2].barh(y, args.window / 60, left=w["t"] / 60, height=0.8, color=SC.get(w[f"{n}_state"], "grey"), lw=0)
    for a, b, ip, pb in runs_of(ad["chunks"]):
        ax[2].barh(2.5, (b - a) / 60, left=a / 60, height=0.8, color=C[ip], lw=0)
    ax[2].set_yticks([0.5, 1.5, 2.5])
    ax[2].set_yticklabels(["Wi-Fi state", "5G state", "I frames on"], fontsize=8)
    ax[2].set_ylim(0, 3)
    for x in ad["switch_times"]:
        for a_ in ax[:3]:
            a_.axvline(x / 60, color="red", lw=0.8, ls="--", alpha=0.6)
    ax[2].text(1.0, -0.45, "state: green good, yellow degraded, orange lossy/high delay, red congested;  "
               "I frames bar: purple 5G, orange Wi-Fi;  red dashes = switch", transform=ax[2].transAxes,
               ha="right", fontsize=8)
    if not with_video:
        ax[2].set_xlabel("time (min)")
        for a_ in (ax[0], ax[1]):
            a_.grid(alpha=0.3)
            a_.legend(loc="upper right", fontsize=8)
        fig.suptitle(f"Part 8 (no video) - {SCEN[scen]['title']}\nmeasured every {args.window:g} s; where the "
                     f"I frames would go (better path's stream)", fontsize=11)
        fig.tight_layout()
        out = f"{od}/conditions_{scen}.png"
        fig.savefig(out, dpi=120)
        plt.close(fig)
        return out
    for r in runs:
        col = "#888888" if r["variant"] == "fixed" else "#1a9e55"
        ax[3].plot([(s["start"] + s["end"]) / 120 for s in r["segments"]],
                   [100.0 * s["shown"] / s["frames"] if s["frames"] else 0 for s in r["segments"]],
                   marker="o", ms=3, color=col,
                   label="fixed (I on 5G stream, P/B on Wi-Fi datagrams)" if r["variant"] == "fixed"
                   else "adaptive (I better path stream, P/B worse path datagrams)")
    ax[3].set_ylabel(f"frames shown %\n(per {args.segment:g} s)")
    ax[3].set_ylim(0, 105)
    ax[3].set_xlabel("time (min)")
    ax[3].legend(loc="lower left", fontsize=8)
    ax[0].legend(loc="upper right", fontsize=8)
    ax[1].legend(loc="upper right", fontsize=8)
    for a_ in (ax[0], ax[1], ax[3]):
        a_.grid(alpha=0.3)
    fig.suptitle(f"Part 8 - {SCEN[scen]['title']}\nmeasured every {args.window:g} s; I frames on the better "
                 f"path's stream, P/B frames on the worse path's datagrams", fontsize=11)
    fig.tight_layout()
    out = f"{od}/congestion_{scen}.png"
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


def video(od, scen, runs, args):
    """Original | fixed | adaptive, with a status bar: where the adaptive sender sends the frames, and the
    delay / loss / state of both paths it measured."""
    import frame_videos as fv
    d = os.path.join(od, "h264")
    ts = os.path.join(d, "video.ts")
    fnt = fv.font()
    ins = [("Original H.264", ts)] + [("H.264 " + ("no switching (I on 5G)" if r["variant"] == "fixed"
                                                   else "adaptive (measured)"),
                                       os.path.join(d, r["variant"], "live.ts")) for r in runs]
    tmp = os.path.join(d, "_grid.mp4")
    if not fv.grid(ins, tmp, 640, 360, len(ins), fnt, fv.i_times(ts)):
        return None
    ad = next((r for r in runs if r["variant"] == "adaptive"), runs[0])
    esc = lambda s: s.replace("\\", "\\\\").replace(":", "\\:").replace(",", "\\,").replace("'", "")
    dt = "drawtext=expansion=none:fontfile='" + fnt + "'"
    bars = []
    for n, (a, b, ip, pb) in enumerate(runs_of(ad["chunks"])):
        col = "0x7b3fe4" if ip == "5g" else "0xe4823f"
        bars.append(f"{dt}:text='{esc('adaptive: ' + describe(ip, pb))}':x=(w-tw)/2:y=h-74:fontsize=24:"
                    f"fontcolor=white:box=1:boxcolor={col}@0.95:boxborderw=8:enable='between(t,{a:.2f},{b - 0.01:.2f})'")
        if n > 0:
            bars.append(f"{dt}:text='{esc('SWITCH -> I frames on the ' + LABEL[ip] + ' stream')}':x=(w-tw)/2:y=150:"
                        f"fontsize=34:fontcolor=white:box=1:boxcolor=red@0.9:boxborderw=10:"
                        f"enable='between(t,{a:.2f},{a + 3:.2f})'")
    for w in ad["windows"]:
        s = lambda n: (f"{LABEL[n]} " + ("-" if w[f"{n}_d"] is None else f"{w[f'{n}_d']:.0f} ms") +
                       f" {w[f'{n}_l']:.1f} % {w[f'{n}_state']}")
        bars.append(f"{dt}:text='{esc(s('5g') + '      ' + s('wifi'))}':x=(w-tw)/2:y=h-32:fontsize=22:"
                    f"fontcolor=white:enable='between(t,{w['t']:.2f},{w['t'] + args.window - 0.01:.2f})'")
    with open(os.path.join(d, "_bars.txt"), "w") as fh:
        fh.write("pad=iw:ih+84:0:0:black," + ",".join(bars))
    out = f"{od}/congestion_{scen}.mp4"
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp, "-filter_script:v",
                        os.path.join(d, "_bars.txt"), "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                        "-pix_fmt", "yuv420p", out], capture_output=True, text=True)
    os.remove(tmp)
    if r.returncode != 0:
        print("ffmpeg failed:", r.stderr[-500:])
        return None
    return out


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Part 8: measured delay/loss decide the paths of the I and P/B frames")
    ap.add_argument("--config", default=None)
    ap.add_argument("--video", default=None)
    ap.add_argument("--scenarios", default="congested,lossy,degraded")
    ap.add_argument("--variants", default="fixed,adaptive")
    ap.add_argument("--length", type=float, default=840.0, help="video length in s (default 840 = 14 min)")
    ap.add_argument("--segment", type=float, default=20.0, help="segments for the graph (default 20 s)")
    ap.add_argument("--window", type=float, default=5.0, help="measure and decide every ... s (default 5)")
    ap.add_argument("--td", type=float, default=50.0, help="delay threshold Td in ms (default 50)")
    ap.add_argument("--tl", type=float, default=5.0, help="loss threshold TL in %% (default 5)")
    ap.add_argument("--deg-span", type=float, default=30.0, help="degraded: trend over the last ... s (default 30)")
    ap.add_argument("--deg-dd", type=float, default=0.1, help="degraded: delay rising faster than ... ms/s (0.1)")
    ap.add_argument("--deg-dl", type=float, default=0.01, help="degraded: loss rising faster than ... %%/s (0.01)")
    ap.add_argument("--bg-low", type=float, default=10.0, help="normal background traffic, %% of the path (10)")
    ap.add_argument("--bg-high", type=float, default=90.0, help="congestion background traffic, %% (default 90)")
    ap.add_argument("--bg-unit", type=float, default=10.0, help="one background flow = ... %% of the path (10)")
    ap.add_argument("--ramp", type=float, default=20.0, help="seconds the background traffic takes to rise (20)")
    ap.add_argument("--lossy-pct", type=float, default=8.0, help="loss of a lossy path in %% (default 8)")
    ap.add_argument("--deg-delay", type=float, default=60.0, help="degrading: extra delay at the end of the phase")
    ap.add_argument("--deg-loss", type=float, default=6.0, help="degrading: extra loss at the end of the phase")
    ap.add_argument("--probe-pps", type=float, default=200.0, help="probe packets per s on each path (200)")
    ap.add_argument("--buffer", type=float, default=150.0)
    ap.add_argument("--picoquic", default=None)
    ap.add_argument("--no-video", action="store_true", help="skip the VLC videos (saves about 15 min each)")
    ap.add_argument("--out", default="congestion_results")
    ap.add_argument("--show", action="store_true", help="only print the tables of the last run")
    ap.add_argument("--no-stream", action="store_true",
                    help="no video: only the network changes + measuring + deciding (results in conditions_results/)")
    args = ap.parse_args()
    if args.no_stream and args.out == "congestion_results":
        args.out = "conditions_results"
    outdir = os.path.abspath(args.out)
    scens = [s.strip() for s in args.scenarios.split(",") if s.strip() in SCEN]
    if args.show:
        return show_probe(outdir, scens) if args.no_stream else show(outdir, scens)
    if args.no_stream:
        return main_probe(args, outdir, scens)

    cfg = netconfig.load(args.config)
    topo.apply_config(cfg)
    for n in ("wifi", "5g"):
        PATHS[n]["bw"] = BASE[n]["bw"]
        PATHS[n]["delay"] = f"{BASE[n]['delay']:g}ms"
        PATHS[n]["loss"] = BASE[n]["loss"]
    vc = vs.video_cfg(args.config)
    src = args.video if args.video is not None else vc["file"]
    variants = [v.strip() for v in args.variants.split(",") if v.strip() in ("fixed", "adaptive")]
    setLogLevel("info")
    os.makedirs(outdir, exist_ok=True)
    import quic_test as qt
    pq = qt.find_picoquic(args.picoquic)
    if "QUICPERF_VIDEO_IN" not in open(os.path.join(pq, "picohttp", "quicperf.c")).read():
        raise SystemExit("picoquic cannot carry the real video yet: python3 patch_quicperf_video.py ~/picoquic "
                         "&& (cd ~/picoquic && make picoquicdemo)")
    if not shutil.which("iperf3"):
        raise SystemExit("iperf3 is missing: sudo apt install -y iperf3")
    vd = os.path.join(outdir, "h264")
    os.makedirs(vd, exist_ok=True)
    info(f"\n*** H.264: encoding a {args.length / 60:.1f}-minute video ({vc['bitrate_mbps']:g} Mbit/s, "
         f"one I frame per second) - this takes a few minutes\n")
    lv.encode_long(src, f"{vd}/original.mp4", "h264", vc["bitrate_mbps"], args.length)
    ts = f"{vd}/video.ts"
    vs.sh(f"ffmpeg -y -loglevel error -i '{vd}/original.mp4' -c copy -f mpegts '{ts}'")
    frames = it.frame_list(ts)
    _, owner = it.split_ts(ts, frames, f"{vd}/part_I.ts", f"{vd}/part_PB.ts")
    for f in ("part_I.ts", "part_PB.ts", "part_P.ts", "part_B.ts"):
        try:
            os.remove(f"{vd}/{f}")
        except OSError:
            pass
    secs = float(vs.sh(f"ffprobe -v error -show_entries format=duration -of csv=p=0 '{ts}'").stdout.strip()
                 or args.length)
    st = it.frame_stats(frames)
    info(f"    {st['frames']} frames: I {st['I']['count']}, P {st['P']['count']}, B {st['B']['count']}, "
         f"{os.path.getsize(ts) / 1e6:.0f} MB\n")
    A = {"ts": ts, "frames": frames, "owner": owner, "original": f"{vd}/original.mp4", "secs": secs}
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = topo.build_topology()
    net.get("srv").cmd("for i in srv-wifi srv-5g; do ethtool -K $i gro off 2>/dev/null; done")
    for n in ("wifi", "5g"):
        vs.shape(net, n, BASE[n]["bw"], BASE[n]["delay"], BASE[n]["loss"])
    results = {}
    try:
        for scen in scens:
            od = os.path.join(outdir, scen)
            d = os.path.join(od, "h264")
            os.makedirs(d, exist_ok=True)
            for f in ("video.ts", "original.mp4"):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
                os.symlink(os.path.join(vd, f), os.path.join(d, f))
            info(f"\n################ SCENARIO: {SCEN[scen]['title']} ################\n")
            for v in variants:
                r = run(net, scen, v, pq, d, A, args)
                results.setdefault(scen, []).append(r)
                save(od, results[scen], args)               # saved after every run (nothing lost on a crash)
    finally:
        info("*** Stopping network\n")
        net.stop()
    made = []
    for scen, runs in results.items():
        od = os.path.join(outdir, scen)
        g = graph(od, scen, runs, args)
        if g:
            shutil.copy(g, os.path.join(outdir, os.path.basename(g)))
            made.append(os.path.join(outdir, os.path.basename(g)))
        if not args.no_video:
            info(f"*** {scen}: making the VLC video (10-20 min) ...\n")
            v = video(od, scen, runs, args)
            if v:
                os.replace(v, os.path.join(outdir, os.path.basename(v)))
                made.append(os.path.join(outdir, os.path.basename(v)))
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    show(outdir, scens)
    for x in made:
        print("Made:", x)
    print("\nOn the Mac:\n  scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/congestion_results/congestion_*' ~/Downloads/")


def main_probe(args, outdir, scens):
    """--no-stream: no encoding, no picoquic, no video - only the measuring and deciding."""
    cfg = netconfig.load(args.config)
    topo.apply_config(cfg)
    for n in ("wifi", "5g"):
        PATHS[n]["bw"] = BASE[n]["bw"]
        PATHS[n]["delay"] = f"{BASE[n]['delay']:g}ms"
        PATHS[n]["loss"] = BASE[n]["loss"]
    if not shutil.which("iperf3"):
        raise SystemExit("iperf3 is missing: sudo apt install -y iperf3")
    setLogLevel("info")
    os.makedirs(outdir, exist_ok=True)
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = topo.build_topology()
    for n in ("wifi", "5g"):
        vs.shape(net, n, BASE[n]["bw"], BASE[n]["delay"], BASE[n]["loss"])
    results = {}
    try:
        for scen in scens:
            od = os.path.join(outdir, scen)
            os.makedirs(od, exist_ok=True)
            info(f"\n################ SCENARIO: {SCEN[scen]['title']} ################\n")
            r = run_probe(net, scen, args, od)
            results[scen] = r
            save_probe(od, r)
    finally:
        info("*** Stopping network\n")
        net.stop()
    made = []
    for scen, r in results.items():
        g = graph(os.path.join(outdir, scen), scen, [r], args)
        if g:
            shutil.copy(g, os.path.join(outdir, os.path.basename(g)))
            made.append(os.path.join(outdir, os.path.basename(g)))
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    show_probe(outdir, scens)
    for x in made:
        print("Made:", x)
    print(f"\nOn the Mac:\n  scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/{os.path.basename(outdir)}/conditions_*' ~/Downloads/")


if __name__ == "__main__":
    main()
