#!/usr/bin/env python3
"""
Part 7 - Long video (15 min) with changing 5G and Wi-Fi signal:
         I frames on the BETTER path's QUIC stream, P/B frames on the WORSE path's datagrams.

  5G  : up to 700 Mbit/s          Wi-Fi: up to 500 Mbit/s

The video is sent live. While it plays, the adaptive sender checks the link rate of both
paths every second (--check) and switches AUTOMATICALLY - it does not know in advance
when a link will drop:

    5G stronger than Wi-Fi   ->  I frames on the 5G QUIC stream,    P/B on Wi-Fi datagrams
    5G drops below Wi-Fi     ->  I frames on the Wi-Fi QUIC stream, P/B on 5G datagrams
    5G strong again          ->  back to the 5G stream

    (I frames only ever travel on a QUIC stream - lost pieces are resent;
     P/B frames only ever travel as QUIC datagrams - fast, not resent)

While the video plays, the bandwidth of both paths changes like in the drawing
(the curves cross, and each path has times when its signal is weak):

    phase 1   5G strong 700,  Wi-Fi 500         -> I on 5G
    phase 2   5G weak,        Wi-Fi 500         -> I on Wi-Fi
    phase 3   5G strong 700,  Wi-Fi 400         -> I on 5G
    phase 4   5G 600,         Wi-Fi weak        -> I on 5G
    phase 5   5G weak,        Wi-Fi 500         -> I on Wi-Fi
    (changes are ramps of --ramp seconds, applied every 2 s)

Compared with:
    fixed     no switching: I always on the 5G stream, P/B always on Wi-Fi datagrams
    adaptive  the rule above, checked every second, switches automatically

Results: frames that arrived in time, GOPs lost, frames the viewer can see, picture
quality, delay - for the whole video, per phase and per segment; a graph of both
bandwidths with the path chosen for the I frames; a VLC video with a status bar.

Run:   sudo python3 long_video_test.py --video BBB.webm                  # 15 min video (about 40 min)
       sudo python3 long_video_test.py --video BBB.webm --length 120     # quick 2-minute test
       python3 show_results.py --long                                    # tables again
"""
import argparse
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

PATHS = topo.PATHS
TSP = 188
PORT0 = 4600
LABEL = {"wifi": "Wi-Fi", "5g": "5G"}
MAXBW = {"5g": 700.0, "wifi": 500.0}
MAIN = "5g"          # the main path of the scenario that is running (set in main())
WEAK_BELOW = 50.0    # the main path counts as weak below this bandwidth (Mbit/s)
PB_RULE = "better"   # P/B datagrams: "better" = on the better path, "other" = on the path without the I frames


def other(n):
    return "wifi" if n == "5g" else "5g"
# phases as (fraction of the video where it ends, 5G Mbit/s, Wi-Fi Mbit/s); "W" = weak
PHASES = [(0.2, 700, 500), (0.4, "W", 500), (0.6, 700, 400), (0.8, 600, "W"), (1.0, "W", 500)]


# ---------------------------------------------------------------------------
#  Bandwidth over time
# ---------------------------------------------------------------------------
def phase_table(length, weak):
    out, a = [], 0.0
    for end, g5, wf in PHASES:
        b = end * length
        out.append((a, b, weak if g5 == "W" else float(g5), weak if wf == "W" else float(wf)))
        a = b
    return out


def bandwidth(t, phases, ramp):
    """(5G, Wi-Fi) Mbit/s at time t: phase values with linear ramps of `ramp` s between phases."""
    for k, (a, b, g5, wf) in enumerate(phases):
        if t < b or k == len(phases) - 1:
            if k > 0 and t < a + ramp:
                _, _, pg, pw = phases[k - 1]
                f = max(0.0, (t - a) / ramp)
                return pg + (g5 - pg) * f, pw + (wf - pw) * f
            return g5, wf
    return phases[-1][2], phases[-1][3]


def phase_of(t, phases):
    for k, (a, b, *_ ) in enumerate(phases):
        if t < b:
            return k
    return len(phases) - 1


# ---------------------------------------------------------------------------
#  Encoding a long video (the source is looped if it is shorter)
# ---------------------------------------------------------------------------
def encode_long(src, out, codec, bitrate, length):
    inp = f"-stream_loop -1 -i '{src}' -t {length}" if src else \
        f"-f lavfi -i testsrc2=size=1280x720:rate=30 -t {length}"
    rate = f"-b:v {bitrate}M -maxrate {bitrate}M -bufsize {bitrate}M"
    if codec == "h264":
        enc = f"-c:v libx264 -preset veryfast {rate} -g 30 -keyint_min 30 -sc_threshold 0 -bf 3"
    else:
        enc = (f"-c:v libx265 -preset veryfast {rate} -tag:v hvc1 -x265-params "
               f"keyint=30:min-keyint=30:scenecut=0:bframes=3:open-gop=0:log-level=error")
    r = vs.sh(f"ffmpeg -y -loglevel error {inp} -an -vf scale=1280:720,fps=30 {enc} -pix_fmt yuv420p '{out}'")
    if r.returncode != 0:
        raise SystemExit(f"ffmpeg could not encode {codec}:\n{r.stderr}")


def packet_times_all(frames, owner):
    """Display time of every TS packet; non-video packets take the time of the video before them."""
    p0 = min(f["pts"] for f in frames)
    ft = [(f["pts"] - p0) / 90000.0 for f in frames]
    out, last = [], 0.0
    for o in owner:
        if o >= 0:
            last = ft[o]
        out.append(last)
    return out


# ---------------------------------------------------------------------------
#  Automatic switching: the sender checks both links while the video plays
# ---------------------------------------------------------------------------
def link_rate(net, n):
    """Current link rate of a path in Mbit/s, read from sta1's interface (what the radio reports,
    like the signal bars on a phone). The sender knows nothing about the future."""
    dev = "sta1-5g" if n == "5g" else "sta1-wlan0"
    out = net.get("sta1").cmd(f"tc qdisc show dev {dev}")
    import re
    m = re.search(r"rate (\d+(?:\.\d+)?)([KMG]?)bit", out)
    if not m:
        return None
    return float(m.group(1)) * {"K": 1e-3, "M": 1.0, "G": 1e3, "": 1e-6}[m.group(2)]


def range_rules(srv, lo, hi, add=True):
    """Count the video bytes arriving on each receiver interface (one rule per path for all lanes)."""
    for dev in ("srv-wifi", "srv-5g"):
        rule = f"INPUT -i {dev} -p udp --sport {lo}:{hi}"
        srv.cmd(f"while iptables -w -D {rule} 2>/dev/null; do :; done")
        if add:
            srv.cmd(f"iptables -w -I {rule}")


def range_bytes(srv, lo, hi):
    mb = {"wifi": 0.0, "5g": 0.0}
    for line in srv.cmd("iptables -w -L INPUT -v -x -n 2>/dev/null").splitlines():
        if f"spts:{lo}:{hi}" not in line:
            continue
        f = line.split()
        try:
            b = int(f[1])
        except (ValueError, IndexError):
            continue
        mb["wifi" if "srv-wifi" in line else "5g"] += b / 1e6
    return mb


def check_data(live, ts, cov):
    """Every TS packet the receiver got must be byte-identical to the packet that was sent there.
    Returns (wrong packets, received packets)."""
    bad = got = 0
    with open(live, "rb") as a, open(ts, "rb") as b:
        k = 0
        while True:
            x = a.read(TSP * 4096)
            y = b.read(TSP * 4096)
            if not x:
                break
            for j in range(0, len(x), TSP):
                if k < len(cov) and cov[k] >= TSP:
                    got += 1
                    if x[j:j + TSP] != y[j:j + TSP]:
                        bad += 1
                k += 1
    return bad, got


def decide(variant, r5, rw, pb_rule=None):
    """(path for I frames, path for P/B frames, how P/B go: "stream" or "dgram").
    The STREAM is the main way of sending; I frames always go on a stream:
      main path good -> I + P + B all on the main path's stream
      main path weak -> I frames switch automatically to the other path's stream,
                        P/B frames go as datagrams on the path that is better at that moment
    (weak = below WEAK_BELOW Mbit/s and below the other path).
    PB_RULE "better"/"other": P/B always datagrams, on the better path / on the path without the I frames."""
    rate = {"5g": r5, "wifi": rw}
    m, h = MAIN, other(MAIN)
    better = "5g" if rate["5g"] >= rate["wifi"] else "wifi"
    if variant == "fixed":                                            # no switching at all
        if PB_RULE == "streammain":
            return m, m, "stream"
        return m, (m if PB_RULE == "better" else h), "dgram"
    weak = rate[m] < WEAK_BELOW and rate[h] > rate[m]
    ip = h if weak else m
    if PB_RULE == "streammain":
        return (h, better, "dgram") if weak else (m, m, "stream")
    if PB_RULE == "better":
        return ip, better, "dgram"
    return ip, other(ip), "dgram"


def describe(ip, pb, pbm):
    if pbm == "stream" and ip == pb:
        return f"I + P/B on the {LABEL[ip]} stream"
    return (f"I frames on the {LABEL[ip]} stream, P/B as {LABEL[pb]} "
            + ("datagrams" if pbm == "dgram" else "stream"))


def runs_of(chunks):
    """Merge chunks with the same decision: [(start, end, i_path, pb_path)]."""
    out = []
    for c in chunks:
        a, b, ip, pb = c[:4]
        md = c[6] if len(c) > 6 else "dgram"
        if out and out[-1][2:] == (ip, pb, md) and abs(out[-1][1] - a) < 1e-6:
            out[-1] = (out[-1][0], b, ip, pb, md)
        else:
            out.append((a, b, ip, pb, md))
    return out


# ---------------------------------------------------------------------------
#  One run
# ---------------------------------------------------------------------------
def run(net, variant, pq, d, A, args):
    sta1, srv = net.get("sta1"), net.get("srv")
    sub = os.path.join(d, variant)
    shutil.rmtree(sub, ignore_errors=True)
    os.makedirs(sub)
    length = A["secs"]
    phases = phase_table(length, args.weak_mbps)
    nch = max(1, int(round(length / args.check)))
    bounds = [(k * args.check, length if k == nch - 1 else (k + 1) * args.check) for k in range(nch)]
    ptimes = packet_times_all(A["frames"], A["owner"])
    is_i = [o < 0 or A["frames"][o]["type"] == "I" for o in A["owner"]]
    sel_i = [[] for _ in range(nch)]
    sel_pb = [[] for _ in range(nch)]
    for j, t in enumerate(ptimes):                        # one pass over all TS packets
        k = min(nch - 1, int(t // args.check))
        (sel_i if is_i[j] else sel_pb)[k].append(j)
    data = open(A["ts"], "rb").read()
    lanes = []                      # (name, packets, mode, chunk, start, length); the path is chosen live
    for k, (a, b) in enumerate(bounds):
        for kind, pk, mode in (("I", sel_i[k], "stream"), ("PB", sel_pb[k], "dgram")):
            if pk:
                name = f"c{k:04d}{kind}"
                lanes.append((name, pk, mode, k, a, b - a))
                at.write_part(data, pk, f"{sub}/part_{name}.ts")
    del data, sel_i, sel_pb
    info(f"\n========== Long video, {length / 60:.1f} min - "
         f"{LABEL[MAIN]} main - "
         f"{'adaptive: checks the links every %g s and switches automatically when %s is weak' % (args.check, LABEL[MAIN]) if variant == 'adaptive' else 'no switching: ' + describe(*decide('fixed', 1, 1))}"
         f" ==========\n")
    # one port per lane (never reused: a reused port could reach an old sender with other data)
    ports, nxt = {}, PORT0
    for name, *_ in lanes:
        for md in (("stream",) if name.endswith("I") else ("stream", "dgram")):
            ports[(name, md)] = nxt
            nxt += 1
    lo, hi = PORT0, nxt
    range_rules(srv, lo, hi)
    dly = {n: float(str(PATHS[n]["delay"]).rstrip("ms")) for n in ("5g", "wifi")}

    def set_bw(t):
        g5, wf = bandwidth(t, phases, args.ramp)
        vs.shape(net, "5g", g5, dly["5g"], PATHS["5g"]["loss"])
        vs.shape(net, "wifi", wf, dly["wifi"], PATHS["wifi"]["loss"])
        return g5, wf

    cur = list(set_bw(0.0))
    pt.ping_all(sta1, "multi", sub, int(length) + 30)
    events = []
    by_chunk = {}
    for name, pk, mode, k, start, ln in lanes:
        events.append((max(0.0, start - 2.0), 1, "server", name))
        by_chunk.setdefault(k, []).append(name)
    for k, (a, b) in enumerate(bounds):
        events.append((a, 2, "decide", k))
    t = 0.0
    while t < length:
        events.append((t, 0, "bw", None))
        t += 1.0
    events.sort(key=lambda e: (e[0], e[1]))
    lane = {l[0]: l for l in lanes}
    pids, scen, used = {}, {}, {}
    chunks = [None] * nch          # (start, end, i_path, pb_path, 5G Mbit/s seen, Wi-Fi Mbit/s seen)
    t0 = time.time() + 2.5
    last_print = -60
    for at_, _, what, x in events:
        while time.time() - t0 < at_:
            time.sleep(0.005)
        if what == "server":
            # a sender for every way this second may be sent (P/B: stream or datagrams);
            # the one that is not chosen is stopped when the second starts
            _, pk, mode, k, start, ln = lane[x]
            os.makedirs(f"{sub}/{x}", exist_ok=True)
            for md in (("stream",) if x.endswith("I") else ("stream", "dgram")):
                sc, ns, _ = pt.video_scenario(md, len(pk) * TSP, ln, slot=it.SLOT)
                scen[(x, md)] = (sc, ns)
                env = f"QUICPERF_VIDEO_IN={sub}/part_{x}.ts QUICPERF_VIDEO_NS={ns} QUICPERF_VIDEO_SLOT={it.SLOT} "
                out = sta1.cmd(f"{env}{pq}/picoquicdemo -0 -p {ports[(x, md)]} -c {pq}/certs/cert.pem "
                               f"-k {pq}/certs/key.pem -F {sub}/{x}/perf_server_{md}.csv "
                               f"> {sub}/{x}/server_{md}.log 2>&1 & echo $!").split()
                pids[(x, md)] = out[-1] if out and out[-1].isdigit() else None
        elif what == "decide":
            k = x
            r5 = link_rate(net, "5g")
            rw = link_rate(net, "wifi")
            r5 = cur[0] if r5 is None else r5
            rw = cur[1] if rw is None else rw
            ip, pb, pbm = decide(variant, r5, rw)
            prev = chunks[k - 1] if k > 0 else None
            chunks[k] = (bounds[k][0], bounds[k][1], ip, pb, r5, rw, pbm)
            ts_ = f"{int(at_ // 60):2d}:{at_ % 60:04.1f}"
            if prev and prev[2] != ip:
                rm = r5 if MAIN == "5g" else rw
                why = (f"{LABEL[MAIN]} weak ({rm:.0f} Mbit/s)" if ip != MAIN
                       else f"{LABEL[MAIN]} good again ({rm:.0f} Mbit/s)")
                info(f"*** {ts_}  SWITCH: {why} -> {describe(ip, pb, pbm)}\n")
            elif prev and (prev[3], prev[6]) != (pb, pbm):
                info(f"*** {ts_}  5G {r5:.0f} / Wi-Fi {rw:.0f} Mbit/s -> {describe(ip, pb, pbm)}\n")
            for name in by_chunk.get(k, []):              # start sending this second of video
                _, pk, mode, _, start, ln = lane[name]
                is_i_lane = name.endswith("I")
                path = ip if is_i_lane else pb
                md = "stream" if is_i_lane else pbm
                if not is_i_lane:                          # stop the sender that is not used
                    unused = (name, "dgram" if md == "stream" else "stream")
                    if pids.get(unused):
                        sta1.cmd(f"kill -9 {pids[unused]} 2>/dev/null")
                        pids[unused] = None
                used[name] = (path, md)
                sc, ns = scen[(name, md)]
                srv.cmd(f"(QUICPERF_VIDEO_OUT={sub}/{name}/received_all.ts QUICPERF_VIDEO_LOG={sub}/{name}/pieces.csv "
                        f"QUICPERF_VIDEO_NS={ns} QUICPERF_VIDEO_SLOT={it.SLOT} timeout {int(ln) + 30} "
                        f"{pq}/picoquicdemo -0 -a perf -n test {PATHS[path]['client_ip']} {ports[(name, md)]} '{sc}' "
                        f"> {sub}/{name}/client.log 2>&1; touch {sub}/{name}/done) &")
        else:
            cur = list(set_bw(at_))
            if at_ - last_print >= 30:
                kk = min(nch - 1, int(at_ // args.check))
                c = chunks[kk] or (chunks[kk - 1] if kk > 0 else None)
                if c:
                    info(f"*** {int(at_ // 60):2d}:{int(at_ % 60):02d}    5G {cur[0]:6.1f} Mbit/s  Wi-Fi {cur[1]:6.1f} Mbit/s"
                         f"  -> {describe(c[2], c[3], c[6])}\n")
                last_print = at_
        for n in list(pids):                              # stop the senders of finished seconds
            if pids[n] and os.path.exists(f"{sub}/{n[0]}/done"):
                sta1.cmd(f"kill -9 {pids[n]} 2>/dev/null")
                pids[n] = None
    while time.time() - t0 < length + 45 and not all(os.path.exists(f"{sub}/{n}/done") for n in lane):
        time.sleep(0.5)
    for n, pid in pids.items():
        if pid:
            sta1.cmd(f"kill -9 {pid} 2>/dev/null")
    time.sleep(2)
    sta1.cmd(f"pkill -9 -f 'picoquicdemo -0 -p' ; pkill -9 ping")
    vs.shape(net, "5g", PATHS["5g"]["bw"], dly["5g"], PATHS["5g"]["loss"])
    vs.shape(net, "wifi", PATHS["wifi"]["bw"], dly["wifi"], PATHS["wifi"]["loss"])
    mb = range_bytes(srv, lo, hi)
    range_rules(srv, lo, hi, add=False)

    # ---- what the viewer got
    parts = [(f"{sub}/{n}/received_all.ts", f"{sub}/{n}/pieces.csv", pk) for n, pk, *_ in lanes]
    size = os.path.getsize(A["ts"])
    cov, st = it.rebuild(parts, size, args.buffer, f"{sub}/live.ts")
    bad_pk, got_pk = check_data(f"{sub}/live.ts", A["ts"], cov)
    if bad_pk:
        info(f"    !!! WARNING: {bad_pk} of {got_pk} received TS packets are not the packets that were sent "
             f"- the results of this run are not valid\n")
    for n, *_ in lanes:                                   # the parts are no longer needed (large)
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
    r = {"variant": variant, "dep": dep, "frames": len(frames), "switches": len(sw), "switch_times": sw,
         "delay_avg": pt.mean(alld), "delay_p95": pt.pctl(alld, 95) if alld else None,
         "delay_i": pt.mean(delays_i),
         "late_pct": 100.0 * sum(1 for x in alld if x > args.buffer) / len(alld) if alld else None,
         "mb_wifi": mb["wifi"], "mb_5g": mb["5g"], "bad_packets": bad_pk, "main": MAIN,
         "weak_below": WEAK_BELOW, "length": length, "segment": args.segment,
         "check": args.check, "weak_mbps": args.weak_mbps, "buffer_ms": args.buffer,
         "chunks": [c for c in chunks if c]}
    fidx = [[] for _ in range(nch)]
    for j, tt in enumerate(ftime):
        fidx[min(nch - 1, int(tt // args.check))].append(j)

    def summary(a, b):
        cs = [c for c in r["chunks"] if a <= c[0] < b]
        idx = [j for c in cs for j in fidx[min(nch - 1, int(round(c[0] / args.check)))]]
        t5 = sum(c[1] - c[0] for c in cs if c[2] == "5g")
        tw = sum(c[1] - c[0] for c in cs if c[2] == "wifi")
        p5 = sum(c[1] - c[0] for c in cs if c[3] == "5g")
        pw = sum(c[1] - c[0] for c in cs if c[3] == "wifi")
        pst = sum(c[1] - c[0] for c in cs if len(c) > 6 and c[6] == "stream")
        pdg = sum(c[1] - c[0] for c in cs if not (len(c) > 6 and c[6] == "stream"))
        ii = [j for j in idx if frames[j]["type"] == "I"]
        return {"i_path": "+".join(LABEL[p] for p, tm in (("5g", t5), ("wifi", tw)) if tm > 0),
                "i_main": "5g" if t5 >= tw else "wifi", "i_5g_s": t5, "i_wifi_s": tw, "pb_5g_s": p5, "pb_wifi_s": pw, "pb_stream_s": pst, "pb_dgram_s": pdg,
                "pb_main": "5g" if p5 >= pw else "wifi",
                "shown": sum(flags[j] for j in idx), "frames": len(idx),
                "i_ok": sum(1 for j in ii if all(cov[q] >= TSP for q in frames[j]["packets"])), "i_total": len(ii)}

    r["phases"] = []
    for k, (a, b, g5, wf) in enumerate(phases):
        r["phases"].append({"phase": k + 1, "start": a, "end": b, "bw5g": g5, "bwwifi": wf, **summary(a, b)})
    r["segments"] = []
    a, k = 0.0, 0
    while a < length - 0.01:
        b = min(length, a + args.segment)
        g5, wf = bandwidth((a + b) / 2, phases, args.ramp)
        s = summary(a, b)
        r["segments"].append({"seg": k + 1, "start": a, "end": b, "bw5g": g5, "bwwifi": wf,
                              "pb_path": s["pb_main"], **s})
        a, k = b, k + 1
    pt.watchable(f"{sub}/live.ts", f"{sub}/live.mp4")
    r["psnr"], r["ssim"], r["damaged"], r["total"] = measure(sub, A["original"], A["ts"])
    I, P, B = dep["I"], dep["P"], dep["B"]
    info(f"    data check: {got_pk - bad_pk}/{got_pk} received packets correct\n")
    info(f"    {len(sw)} switches; I {I[0]}/{I[1]}, P {P[0]}/{P[1]}, B {B[0]}/{B[1]}, GOPs lost {dep['gops_lost']}; "
         f"frames shown {dep['shown']}/{len(frames)}; PSNR {r['psnr'] or '-'} dB; "
         f"delay avg {vs.f1(r['delay_avg'], ' ms')}\n")
    return r, r["chunks"], phases


def measure(sub, original, ts, thr=35.0):
    """Picture quality of what the viewer saw, compared with the sent video AT THE SAME MOMENT.
    Both videos keep their real timestamps (-copyts) and are turned into one picture every 1/30 s
    from the start of the video (fps filter): a lost frame = the last picture is repeated, as on
    screen, and a frame of the received video is never compared with a different moment.
    Returns (PSNR dB, SSIM, damaged frames, frames)."""
    import re
    live = f"{sub}/live.ts"
    if not os.path.exists(live) or os.path.getsize(live) == 0:
        return "", "", None, None
    t0 = vs.sh(f"ffprobe -v error -select_streams v:0 -show_entries stream=start_time -of csv=p=0 '{ts}'").stdout
    dur = vs.sh(f"ffprobe -v error -show_entries format=duration -of csv=p=0 '{ts}'").stdout
    try:
        t0, dur = float(t0.strip().split()[0]), float(dur.strip().split()[0])
    except (ValueError, IndexError):
        return "", "", None, None
    log = f"{sub}/psnr_frames.log"
    norm = f"setpts=PTS-{t0:.6f}/TB,fps=30:start_time=0:round=near,trim=duration={dur:.3f},setpts=PTS-STARTPTS"
    r = vs.sh(f"ffmpeg -nostdin -copyts -fflags +genpts+discardcorrupt -err_detect ignore_err -f mpegts -i '{live}' "
              f"-copyts -i '{ts}' -lavfi \"[0:v]{norm},split[a1][a2];[1:v]{norm},split[b1][b2];"
              f"[a1][b1]psnr=stats_file='{log}';[a2][b2]ssim\" -f null - 2>&1")
    out = r.stdout + r.stderr
    mp = re.search(r"PSNR .*average:([\d.]+|inf)", out)
    ms = re.search(r"SSIM .*All:([\d.]+)", out)
    vals = []
    try:
        for line in open(log):
            for tok in line.split():
                if tok.startswith("psnr_avg:"):
                    v = tok.split(":")[1]
                    vals.append(float("inf") if v == "inf" else float(v))
        os.remove(log)
    except OSError:
        pass
    total = int(round(dur * 30))
    bad = sum(1 for v in vals if v < thr) + max(0, total - len(vals))
    psnr = "" if not mp else (mp.group(1) if mp.group(1) == "inf" else f"{float(mp.group(1)):.1f}")
    ssim = "" if not ms else f"{float(ms.group(1)):.4f}"
    return psnr, ssim, bad, total


# ---------------------------------------------------------------------------
#  Results: CSV, graph, VLC video
# ---------------------------------------------------------------------------
def save(outdir, c, runs):
    f = lambda x, n=2: "" if x is None or x == "" else (f"{x:.{n}f}" if isinstance(x, float) else str(x))
    with open(f"{outdir}/longvideo_summary_{c}.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["codec", "variant", "length_s", "segment_s", "check_s", "switches", "switch_times_s",
                    "i_complete", "i_total", "p_complete", "p_total", "b_complete", "b_total", "gops_lost", "gops",
                    "frames_shown", "frames", "frames_damaged", "frames_total", "psnr_db", "ssim", "delay_avg_ms",
                    "delay_p95_ms", "delay_i_ms", "late_pct", "mb_wifi", "mb_5g", "weak_mbps", "buffer_ms",
                    "main", "weak_below_mbps", "bad_packets", "pb_rule"])
        for r in runs:
            dp = r["dep"]
            w.writerow([c, r["variant"], f(r["length"], 0), f(float(r["segment"]), 0), f(float(r["check"]), 1),
                        r["switches"], " ".join(f"{x:.0f}" for x in r["switch_times"]), dp["I"][0],
                        dp["I"][1], dp["P"][0], dp["P"][1], dp["B"][0], dp["B"][1], dp["gops_lost"], dp["gops"],
                        dp["shown"], r["frames"], r["damaged"], r["total"], r["psnr"], r["ssim"],
                        f(r["delay_avg"], 1), f(r["delay_p95"], 1), f(r["delay_i"], 1), f(r["late_pct"]),
                        f(r["mb_wifi"], 1), f(r["mb_5g"], 1), f(float(r["weak_mbps"]), 1),
                        f(float(r["buffer_ms"]), 0), r.get("main", "5g"), f(float(r.get("weak_below", 0)), 0),
                        r.get("bad_packets", ""), PB_RULE])
    with open(f"{outdir}/longvideo_phases_{c}.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["codec", "variant", "phase", "start_s", "end_s", "bw_5g_mbps", "bw_wifi_mbps", "i_path",
                    "i_on_5g_s", "i_on_wifi_s", "i_complete", "i_total", "frames_shown", "frames"])
        for r in runs:
            for p in r["phases"]:
                w.writerow([c, r["variant"], p["phase"], f(p["start"], 0), f(p["end"], 0), f(p["bw5g"], 0),
                            f(p["bwwifi"], 0), p["i_path"], f(p["i_5g_s"], 0), f(p["i_wifi_s"], 0), p["i_ok"],
                            p["i_total"], p["shown"], p["frames"]])
    with open(f"{outdir}/longvideo_segments_{c}.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["codec", "variant", "segment", "start_s", "end_s", "bw_5g_mbps", "bw_wifi_mbps",
                    "i_path", "i_on_5g_s", "i_on_wifi_s", "frames_shown", "frames", "pb_on_5g_s", "pb_on_wifi_s",
                    "pb_stream_s", "pb_dgram_s"])
        for r in runs:
            for s in r["segments"]:
                w.writerow([c, r["variant"], s["seg"], f(s["start"], 0), f(s["end"], 0), f(s["bw5g"], 1),
                            f(s["bwwifi"], 1), s["i_path"], f(s["i_5g_s"], 0), f(s["i_wifi_s"], 0),
                            s["shown"], s["frames"], f(s["pb_5g_s"], 0), f(s["pb_wifi_s"], 0),
                            f(s["pb_stream_s"], 0), f(s["pb_dgram_s"], 0)])
    with open(f"{outdir}/longvideo_decisions_{c}.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["codec", "variant", "start_s", "end_s", "i_path", "pb_path", "seen_5g_mbps", "seen_wifi_mbps",
                    "pb_mode"])
        for r in runs:
            for ch in r["chunks"]:
                a, b, ip, pb, r5, rw = ch[:6]
                w.writerow([c, r["variant"], f(a, 2), f(b, 2), ip, pb, f(r5, 1), f(rw, 1),
                            ch[6] if len(ch) > 6 else "dgram"])


def graph(outdir, c, runs, phases, ramp):
    """Bandwidth of both paths over time, the path of the I frames (switches), frames shown per segment."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(no graph: sudo apt install -y python3-matplotlib)")
        return None
    length = runs[0]["length"]
    ts = [x * 1.0 for x in range(0, int(length) + 1)]
    bw = [bandwidth(t, phases, ramp) for t in ts]
    fig, ax = plt.subplots(2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [2, 1.4]})
    ax[0].plot([t / 60 for t in ts], [b[0] for b in bw], color="#7b3fe4", lw=2, label="5G bandwidth")
    ax[0].plot([t / 60 for t in ts], [b[1] for b in bw], color="#e4823f", lw=2, label="Wi-Fi bandwidth")
    ad = next((r for r in runs if r["variant"] == "adaptive"), None)
    if ad:
        for a, b, ip, pb, _md in runs_of(ad["chunks"]):
            ax[0].axvspan(a / 60, b / 60, ymin=0, ymax=0.05, color="#7b3fe4" if ip == "5g" else "#e4823f", lw=0)
        for x in ad.get("switch_times", []):
            ax[0].axvline(x / 60, color="red", lw=1, ls="--")
        ax[0].text(0.01, 0.07, "bar = I frames' stream (adaptive): purple 5G, orange Wi-Fi;  red dashes = automatic switch",
                   transform=ax[0].transAxes, fontsize=9)
    for k, (a, b, *_ ) in enumerate(phases):
        ax[0].axvline(a / 60, color="grey", lw=0.6, ls=":")
        ax[0].text((a + b) / 120, 735, f"phase {k + 1}", ha="center", fontsize=9, color="grey")
    ax[0].set_ylabel("Mbit/s")
    ax[0].set_ylim(0, 780)
    ax[0].legend(loc="upper right")
    m = runs[0].get("main", "5g")
    ax[0].set_title(f"Long video ({length / 60:.0f} min, {c.upper()}): whole video on the {LABEL[m]} stream; when "
                    f"{LABEL[m]} is weak the I frames switch automatically\nto the {LABEL[other(m)]} stream and the "
                    f"P/B frames go as datagrams on the better path", fontsize=11)
    for r in runs:
        col = "#888888" if r["variant"] == "fixed" else "#1a9e55"
        xs = [(s["start"] + s["end"]) / 120 for s in r["segments"]]
        ys = [100.0 * s["shown"] / s["frames"] if s["frames"] else 0 for s in r["segments"]]
        ax[1].plot(xs, ys, marker="o", ms=3, color=col,
                   label=f"no switching (all on the {LABEL[m]} stream)" if r["variant"] == "fixed" else "adaptive (automatic switch)")
    ax[1].set_ylabel("frames shown %")
    ax[1].set_ylim(0, 105)
    ax[1].set_xlabel("time (min)")
    ax[1].legend(loc="lower right")
    for a_ in ax:
        a_.grid(alpha=0.3)
    out = f"{outdir}/longvideo_{c}.png"
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


def video(outdir, c, runs, chunks_by_variant, phases=None, ramp=30.0):
    """Original | fixed | adaptive, with frame labels, I-frame highlight and a status bar:
    line 1 = where the adaptive sender sends the I and P/B frames (SWITCH shown for 3 s),
    line 2 = bandwidth of both links."""
    import frame_videos as fv
    d = os.path.join(outdir, c)
    ts = os.path.join(d, "video.ts")
    fnt = fv.font()
    ins = [(f"Original {fv.CODEC[c]}", ts)] + \
          [(f"{fv.CODEC[c]} " +
            (f"no switching (all on {LABEL[r.get('main', '5g')]} stream)" if r["variant"] == "fixed"
             else "automatic switch"),
            os.path.join(d, r["variant"], "live.ts")) for r in runs]
    tmp = os.path.join(d, "_grid.mp4")
    if not fv.grid(ins, tmp, 640, 360, len(ins), fnt, fv.i_times(ts)):
        return None
    chunks = chunks_by_variant.get("adaptive") or next(iter(chunks_by_variant.values()))
    length = runs[0]["length"]
    esc = lambda t: t.replace(":", "\\:").replace(",", "\\,")
    bars = []
    for n, (a, b, ip, pb, md) in enumerate(runs_of(chunks)):
        col = "0x7b3fe4" if ip == "5g" else "0xe4823f"
        txt = esc("adaptive: " + describe(ip, pb, md))
        bars.append(f"drawtext=fontfile='{fnt}':text='{txt}':x=(w-tw)/2:y=h-74:fontsize=24:fontcolor=white:"
                    f"box=1:boxcolor={col}@0.95:boxborderw=8:enable='between(t,{a:.2f},{b - 0.01:.2f})'")
        if n > 0:
            mm = runs[0].get("main", "5g")
            why = f"{LABEL[mm]} weak" if ip != mm else f"{LABEL[mm]} good again"
            sw = esc(f"SWITCH: {why} -> I frames on the {LABEL[ip]} stream")
            bars.append(f"drawtext=fontfile='{fnt}':text='{sw}':x=(w-tw)/2:y=150:fontsize=34:fontcolor=white:"
                        f"box=1:boxcolor=red@0.9:boxborderw=10:enable='between(t,{a:.2f},{a + 3:.2f})'")
    step = 5.0
    t = 0.0
    while t < length and phases:
        g5, wf = bandwidth(t, phases, ramp)
        txt = esc(f"5G {g5:.0f} Mbit/s     Wi-Fi {wf:.0f} Mbit/s")
        bars.append(f"drawtext=fontfile='{fnt}':text='{txt}':x=(w-tw)/2:y=h-32:fontsize=22:fontcolor=white:"
                    f"enable='between(t,{t:.2f},{t + step - 0.01:.2f})'")
        t += step
    with open(os.path.join(d, "_bars.txt"), "w") as fh:
        fh.write("pad=iw:ih+84:0:0:black," + ",".join(bars))
    out = f"{outdir}/longvideo_{c}.mp4"
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp, "-filter_script:v",
                        os.path.join(d, "_bars.txt"), "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                        "-pix_fmt", "yuv420p", out], capture_output=True, text=True)
    os.remove(tmp)
    if r.returncode != 0:
        print("ffmpeg failed:", r.stderr[-500:])
        return None
    return out


def scenario_dirs(base):
    """[(main, folder)] of the results: longvideo_results/main_5g, main_wifi (or the old single folder)."""
    import glob
    out = []
    for m in ("5g", "wifi"):
        d = os.path.join(base, f"main_{m}")
        if glob.glob(os.path.join(d, "longvideo_summary_*.csv")):
            out.append((m, d))
    if not out and glob.glob(os.path.join(base, "longvideo_summary_*.csv")):
        out.append(("5g", base))
    return out


def remeasure(args):
    """PSNR / SSIM / damaged frames again, frame by time, for the results in longvideo_results/."""
    setLogLevel("info")
    base = os.path.abspath(args.out)
    for _, outdir in scenario_dirs(base):
      for c in [x.strip() for x in args.codecs.split(",") if x.strip() in it.CODEC_LABEL]:
        fn = f"{outdir}/longvideo_summary_{c}.csv"
        rows = list(csv.DictReader(open(fn)))
        d = os.path.join(outdir, c)
        for r in rows:
            sub = os.path.join(d, r["variant"])
            info(f"*** {it.CODEC_LABEL[c]} {r['variant']}: measuring the picture (a few minutes) ...\n")
            if not os.path.exists(f"{sub}/live.mp4"):
                pt.watchable(f"{sub}/live.ts", f"{sub}/live.mp4")
            p, s_, bad, tot = measure(sub, f"{d}/original.mp4", f"{d}/video.ts")
            r["psnr_db"], r["ssim"] = p, s_
            r["frames_damaged"], r["frames_total"] = ("" if bad is None else bad), ("" if tot is None else tot)
            info(f"    PSNR {p} dB, SSIM {s_}, damaged {bad}/{tot}\n")
        with open(fn, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    subprocess.run(["chmod", "-R", "a+rwX", base])
    import show_results
    show_results.show_long(base)


def video_only(args):
    """Make graph + VLC video again from longvideo_results/ (the network test is not run again)."""
    global MAIN
    setLogLevel("info")
    base = os.path.abspath(args.out)
    for m, outdir in scenario_dirs(base):
        MAIN = m
        for c in [x.strip() for x in args.codecs.split(",") if x.strip() in it.CODEC_LABEL]:
            rows = list(csv.DictReader(open(f"{outdir}/longvideo_summary_{c}.csv")))
            segrows = list(csv.DictReader(open(f"{outdir}/longvideo_segments_{c}.csv")))
            decrows = list(csv.DictReader(open(f"{outdir}/longvideo_decisions_{c}.csv")))
            r0 = rows[0]
            length = float(r0["length_s"])
            phases = phase_table(length, float(r0["weak_mbps"]))
            runs, chunks_by = [], {}
            for r in rows:
                v = r["variant"]
                ch = [(float(x["start_s"]), float(x["end_s"]), x["i_path"], x["pb_path"],
                       float(x["seen_5g_mbps"]), float(x["seen_wifi_mbps"]), x.get("pb_mode") or "dgram")
                      for x in decrows if x["variant"] == v]
                runs.append({"variant": v, "length": length, "chunks": ch, "main": r.get("main") or m,
                             "switch_times": [float(x) for x in r["switch_times_s"].split()],
                             "segments": [{"start": float(s_["start_s"]), "end": float(s_["end_s"]),
                                           "shown": int(s_["frames_shown"]), "frames": int(s_["frames"])}
                                          for s_ in segrows if s_["variant"] == v]})
                chunks_by[v] = ch
            g = graph(outdir, c, runs, phases, args.ramp)
            if g:
                shutil.copy(g, os.path.join(base, f"longvideo_{c}_{m}main.png"))
                print("Made:", os.path.join(base, f"longvideo_{c}_{m}main.png"))
            info(f"*** {LABEL[m]} main, {it.CODEC_LABEL[c]}: making the VLC video (10-20 min) ...\n")
            v = video(outdir, c, runs, chunks_by, phases, args.ramp)
            if v:
                os.replace(v, os.path.join(base, f"longvideo_{c}_{m}main.mp4"))
                print("Made:", os.path.join(base, f"longvideo_{c}_{m}main.mp4"))
    subprocess.run(["chmod", "-R", "a+rwX", base])
    import show_results
    show_results.show_long(base)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Long video: I frames on the better path, P/B on the worse path")
    ap.add_argument("--config", default=None)
    ap.add_argument("--video", default=None)
    ap.add_argument("--codecs", default="h264")
    ap.add_argument("--length", type=float, default=900.0, help="video length in s (default 900 = 15 min)")
    ap.add_argument("--segment", type=float, default=20.0, help="video segments for the results (default 20 s)")
    ap.add_argument("--weak-mbps", type=float, default=5.0, help="bandwidth of a weak path (default 5)")
    ap.add_argument("--ramp", type=float, default=30.0, help="seconds a bandwidth change takes (default 30)")
    ap.add_argument("--check", type=float, default=1.0,
                    help="the adaptive sender checks both links every ... s and switches at once (default 1)")
    ap.add_argument("--main", default="5g",
                    help="the main path for the I frames: 5g (default), wifi, or both: 5g,wifi")
    ap.add_argument("--pb", choices=("better", "streammain", "other"), default="better",
                    help="streammain (default): whole video on the main stream, when it is weak I -> other stream "
                         "and P/B -> datagrams on the better path; better/other: P/B always datagrams")
    ap.add_argument("--weak-below", type=float, default=50.0,
                    help="the main path is weak below ... Mbit/s -> the I frames move (default 50)")
    ap.add_argument("--variants", default="fixed,adaptive")
    ap.add_argument("--buffer", type=float, default=150.0)
    ap.add_argument("--picoquic", default=None)
    ap.add_argument("--no-video", action="store_true", help="skip the VLC video (saves time)")
    ap.add_argument("--out", default="longvideo_results")
    ap.add_argument("--remeasure", action="store_true",
                    help="measure the picture quality again from the last run (no network test)")
    ap.add_argument("--video-only", action="store_true",
                    help="only make the VLC video again from the results of the last run (no network test)")
    args = ap.parse_args()
    global WEAK_BELOW, PB_RULE
    WEAK_BELOW = args.weak_below
    PB_RULE = args.pb
    if args.remeasure:
        return remeasure(args)
    if args.video_only:
        return video_only(args)

    cfg = netconfig.load(args.config)
    topo.apply_config(cfg)
    for n in ("5g", "wifi"):                       # the maximum bandwidths of this test
        PATHS[n]["bw"] = MAXBW[n]
    vc = vs.video_cfg(args.config)
    src = args.video if args.video is not None else vc["file"]
    codecs = [c.strip() for c in args.codecs.split(",") if c.strip() in it.CODEC_LABEL]
    variants = [v.strip() for v in args.variants.split(",") if v.strip() in ("fixed", "adaptive")]
    setLogLevel("info")
    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    import quic_test as qt
    pq = qt.find_picoquic(args.picoquic)
    if "QUICPERF_VIDEO_IN" not in open(os.path.join(pq, "picohttp", "quicperf.c")).read():
        raise SystemExit("picoquic cannot carry the real video yet: python3 patch_quicperf_video.py ~/picoquic "
                         "&& (cd ~/picoquic && make picoquicdemo)")
    analysis = {}
    for c in codecs:
        d = os.path.join(outdir, c)
        os.makedirs(d, exist_ok=True)
        info(f"\n*** {it.CODEC_LABEL[c]}: encoding a {args.length / 60:.1f}-minute video "
             f"({vc['bitrate_mbps']:g} Mbit/s, one I frame per second) - this takes a few minutes\n")
        encode_long(src, f"{d}/original.mp4", c, vc["bitrate_mbps"], args.length)
        ts = f"{d}/video.ts"
        vs.sh(f"ffmpeg -y -loglevel error -i '{d}/original.mp4' -c copy -f mpegts '{ts}'")
        frames = it.frame_list(ts)
        _, owner = it.split_ts(ts, frames, f"{d}/part_I.ts", f"{d}/part_PB.ts")
        for f in ("part_I.ts", "part_PB.ts", "part_P.ts", "part_B.ts"):
            try:
                os.remove(f"{d}/{f}")
            except OSError:
                pass
        secs = float(vs.sh(f"ffprobe -v error -show_entries format=duration -of csv=p=0 '{ts}'").stdout.strip()
                     or args.length)
        st = it.frame_stats(frames)
        info(f"    {st['frames']} frames: I {st['I']['count']}, P {st['P']['count']}, B {st['B']['count']}, "
             f"{os.path.getsize(ts) / 1e6:.0f} MB\n")
        analysis[c] = {"ts": ts, "frames": frames, "owner": owner, "original": f"{d}/original.mp4", "secs": secs}
    results = {}
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = topo.build_topology()
    net.get("srv").cmd("for i in srv-wifi srv-5g; do ethtool -K $i gro off 2>/dev/null; done")
    mains = [m.strip() for m in args.main.split(",") if m.strip() in ("5g", "wifi")]
    global MAIN
    try:
        for m in mains:
            MAIN = m
            od = os.path.join(outdir, f"main_{m}")
            info(f"\n################ SCENARIO: {LABEL[m]} MAIN ({LABEL[other(m)]} helper, "
                 f"{LABEL[m]} weak below {WEAK_BELOW:g} Mbit/s) ################\n")
            for c in codecs:
                d = os.path.join(od, c)
                os.makedirs(d, exist_ok=True)
                for f in ("video.ts", "original.mp4"):          # the same video for every scenario
                    try:
                        os.remove(os.path.join(d, f))
                    except OSError:
                        pass
                    os.symlink(os.path.join(outdir, c, f), os.path.join(d, f))
                info(f"\n######## {it.CODEC_LABEL[c]} ########\n")
                for v in variants:
                    r, segs, phases = run(net, v, pq, d, analysis[c], args)
                    results.setdefault((m, c), []).append((r, segs, phases))
    finally:
        info("*** Stopping network\n")
        net.stop()
    made = []
    import show_results
    for (m, c), lst in results.items():
        MAIN = m
        od = os.path.join(outdir, f"main_{m}")
        runs = [x[0] for x in lst]
        save(od, c, runs)
        g = graph(od, c, runs, lst[0][2], args.ramp)
        if g:
            shutil.copy(g, os.path.join(outdir, f"longvideo_{c}_{m}main.png"))
            made.append(os.path.join(outdir, f"longvideo_{c}_{m}main.png"))
        if not args.no_video:
            info(f"*** {LABEL[m]} main, {it.CODEC_LABEL[c]}: making the VLC video (10-20 min) ...\n")
            v = video(od, c, runs, {x[0]["variant"]: x[1] for x in lst}, lst[0][2], args.ramp)
            if v:
                os.replace(v, os.path.join(outdir, f"longvideo_{c}_{m}main.mp4"))
                made.append(os.path.join(outdir, f"longvideo_{c}_{m}main.mp4"))
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    show_results.show_long(outdir)
    for x in made:
        print("Made:", x)
    print("\nOn the Mac:\n  scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/longvideo_results/longvideo_*' ~/Downloads/\n"
          "  open ~/Downloads/longvideo_h264_5gmain.png ~/Downloads/longvideo_h264_wifimain.png\n"
          "  open -a VLC ~/Downloads/longvideo_h264_5gmain.mp4")


if __name__ == "__main__":
    main()
