#!/usr/bin/env python3
"""
Part 6 - Adaptive frame-aware sending: switch when the main path's signal gets weak.

Two scenarios, each with a MAIN path and a HELPER path:

  A  5G main, Wi-Fi helper      B  Wi-Fi main, 5G helper

While the main path is good, the whole video goes over it
(I frames on a QUIC stream, P/B frames as QUIC datagrams).
When the main path's bandwidth drops below what the video needs (weak signal),
the sender keeps ONLY the I frames on the main path's stream and moves the
P/B frames to the helper path as datagrams. When the main path recovers,
the P/B frames come back.

The signal drop is emulated with bandwidth steps on the main path:

     good (configured capacity)  |  weak (--weak-mbps)  |  good again
     0 s .............. --drop-at  ..........  --recover-at ....... end

The sender notices a change after --detect seconds (like a real sender measuring
throughput). Each scenario is run twice so the switch can be compared:

  fixed      no adaptation: everything stays on the main path, also while it is weak
  adaptive   I frames stay on the main stream, P/B frames move to the helper path while weak

Measured as in Part 5 (iframe_test.py): I/P/B frames that arrived in time, GOPs lost,
frames the viewer can see (frame dependency), picture quality, delay - for the whole
video and for the weak period only.

Run:   sudo python3 adaptive_test.py --video BBB.webm
       sudo python3 adaptive_test.py --video BBB.webm --scenarios A --codecs h264
       python3 show_results.py --adaptive          # tables again
       python3 adaptive_test.py --videos-only      # only make the VLC videos again
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

PATHS = topo.PATHS
TSP = 188
PORT0 = 4480
LABEL = {"wifi": "Wi-Fi", "5g": "5G"}
SCEN = {"A": ("5g", "wifi"), "B": ("wifi", "5g")}          # (main, helper)
VARIANTS = ["fixed", "adaptive"]
VLABEL = {"fixed": "no adaptation (all on main path)",
          "adaptive": "adaptive (weak: I on main stream, P/B on helper)"}


# ---------------------------------------------------------------------------
#  Which packet belongs to which lane
# ---------------------------------------------------------------------------
def packet_times(frames, owner):
    """Display time (s) of every TS packet (from the frame it belongs to); -1 = not video."""
    p0 = min(f["pts"] for f in frames)
    ft = [(f["pts"] - p0) / 90000.0 for f in frames]
    return [ft[o] if o >= 0 else -1.0 for o in owner]


def lanes_for(variant, main, helper, secs, s1, s2, frames, owner):
    """A lane = (name, packet indices, QUIC mode, path, start s, length s).
    s1/s2 = when the sender moves the P/B frames to the helper / back."""
    pt_ = packet_times(frames, owner)
    is_i = [o < 0 or frames[o]["type"] == "I" for o in owner]
    n = len(owner)
    lanes = [("I", [k for k in range(n) if is_i[k]], "stream", main, 0.0, secs)]
    if variant == "fixed":
        lanes.append(("PB", [k for k in range(n) if not is_i[k]], "dgram", main, 0.0, secs))
        return lanes
    for name, a, b, path in (("PB1", 0.0, s1, main), ("PBweak", s1, s2, helper), ("PB2", s2, secs + 1, main)):
        pk = [k for k in range(n) if not is_i[k] and a <= pt_[k] < b]
        if pk:
            lanes.append((name, pk, "dgram", path, a, max(0.5, min(b, secs) - a)))
    return lanes


def write_part(ts_data, pk, path):
    with open(path, "wb") as fh:
        for k in pk:
            fh.write(ts_data[k * TSP:(k + 1) * TSP])


# ---------------------------------------------------------------------------
#  One run
# ---------------------------------------------------------------------------
def run(net, sc, variant, pq, d, A, secs, args, bg_share):
    sta1, srv = net.get("sta1"), net.get("srv")
    main, helper = SCEN[sc]
    sub = os.path.join(d, f"{sc}_{variant}")
    shutil.rmtree(sub, ignore_errors=True)
    os.makedirs(sub)
    s1, s2 = args.drop_at + args.detect, args.recover_at + args.detect
    lanes = lanes_for(variant, main, helper, secs, s1, s2, A["frames"], A["owner"])
    data = open(A["ts"], "rb").read()
    info(f"\n========== Scenario {sc} ({LABEL[main]} main, {LABEL[helper]} helper) - {VLABEL[variant]} ==========\n")
    for k, (name, pk, mode, path, start, length) in enumerate(lanes):
        write_part(data, pk, f"{sub}/part_{name}.ts")
        info(f"    lane {name:6s}: {len(pk) * TSP / 1e6:4.1f} MB on {LABEL[path]:5s} "
             f"{'QUIC stream ' if mode == 'stream' else 'datagrams   '} from {start:4.1f} s for {length:4.1f} s\n")
    ports = {name: PORT0 + k for k, (name, *_) in enumerate(lanes)}
    for port in ports.values():
        pt.iface_rules(srv, "udp", port)
    p = PATHS[main]
    dly = float(str(p["delay"]).rstrip("ms"))
    vs.shape(net, main, p["bw"], dly, p["loss"])                 # start: main path good
    if args.background:
        pt.bg_start_all(sta1, srv, helper, bg_share, sub)        # helper path carries other traffic
    pt.ping_all(sta1, "multi", sub, int(secs) + 20)
    pids, scen = {}, {}
    for name, pk, mode, path, start, length in lanes:
        ldir = f"{sub}/{name}"
        os.makedirs(ldir)
        size = len(pk) * TSP
        scen[name], ns, _ = pt.video_scenario(mode, size, length, slot=it.SLOT)
        env = f"QUICPERF_VIDEO_IN={sub}/part_{name}.ts QUICPERF_VIDEO_NS={ns} QUICPERF_VIDEO_SLOT={it.SLOT} "
        pids[name] = pt.start_pq_server(sta1, pq, ports[name], ldir, env=env)
        scen[name] = (scen[name], ns)
    time.sleep(1)
    t0 = time.time()
    events = sorted([(start, "lane", name) for name, _, _, _, start, _ in lanes] +
                    [(args.drop_at, "weak", None), (args.recover_at, "good", None)])
    for at, what, name in events:
        while time.time() - t0 < at:
            time.sleep(0.01)
        if what == "lane":
            ln = next(l for l in lanes if l[0] == name)
            path, length = ln[3], ln[5]
            sc_txt, ns = scen[name]
            srv.cmd(f"(QUICPERF_VIDEO_OUT={sub}/{name}/received_all.ts QUICPERF_VIDEO_LOG={sub}/{name}/pieces.csv "
                    f"QUICPERF_VIDEO_NS={ns} QUICPERF_VIDEO_SLOT={it.SLOT} timeout {int(length) + 40} "
                    f"{pq}/picoquicdemo -0 -a perf -n test {PATHS[path]['client_ip']} {ports[name]} '{sc_txt}' "
                    f"> {sub}/{name}/client.log 2>&1; touch {sub}/{name}/done) &")
        elif what == "weak":
            vs.shape(net, main, args.weak_mbps, dly, p["loss"])
            info(f"*** {time.time() - t0:5.1f} s: {LABEL[main]} signal weak -> {args.weak_mbps:g} Mbit/s\n")
        else:
            vs.shape(net, main, p["bw"], dly, p["loss"])
            info(f"*** {time.time() - t0:5.1f} s: {LABEL[main]} signal good again -> {p['bw']} Mbit/s\n")
    while time.time() - t0 < secs + 60 and not all(os.path.exists(f"{sub}/{n}/done") for n, *_ in lanes):
        time.sleep(0.5)
    vs.shape(net, main, p["bw"], dly, p["loss"])
    bg = pt.stop_bg(sub) if args.background else 0.0
    for pid in pids.values():
        pt.stop_pq_server(sta1, pid)
    srv.cmd("pkill -9 iperf3")
    sta1.cmd("pkill -9 iperf3; pkill -9 ping")
    mb = {"wifi": 0.0, "5g": 0.0}
    for port in ports.values():
        w, g, _ = pt.iface_share(srv, port)
        pt.iface_rules(srv, "udp", port, add=False)
        mb["wifi"] += w / 1e6
        mb["5g"] += g / 1e6
    resent = {n: pt.server_retrans(f"{sub}/{n}")[0] for n, *_ in lanes}

    # ---- what the viewer got
    parts = [(f"{sub}/{name}/received_all.ts", f"{sub}/{name}/pieces.csv", pk) for name, pk, *_ in lanes]
    size = os.path.getsize(A["ts"])
    cov, st = it.rebuild(parts, size, args.buffer, f"{sub}/live.ts")
    frames = A["frames"]
    dep = it.dependency(frames, cov)
    flags = shown_flags(frames, cov)
    p0 = min(f["pts"] for f in frames)
    weak = [k for k, f in enumerate(frames) if args.drop_at <= (f["pts"] - p0) / 90000.0 < args.recover_at]
    delays = {name: s["delays"] for (name, *_), s in zip(lanes, st)}
    alld = sorted(x for v in delays.values() for x in v)
    r = {"scenario": sc, "variant": variant, "main": main, "helper": helper, "dep": dep,
         "frames": len(frames), "weak_frames": len(weak), "weak_shown": sum(flags[k] for k in weak),
         "delay_avg": pt.mean(alld), "delay_p95": pt.pctl(alld, 95) if alld else None,
         "delay_i": pt.mean(delays.get("I", [])),
         "ontime_pct": 100.0 * sum(s["ontime"] for s in st) / max(1, sum(s["size"] for s in st)),
         "late_pct": 100.0 * sum(1 for x in alld if x > args.buffer) / len(alld) if alld else None,
         "mb_wifi": mb["wifi"], "mb_5g": mb["5g"], "resent_i": resent.get("I"),
         "bg_mbps": bg, "weak_mbps": args.weak_mbps, "drop_at": args.drop_at,
         "recover_at": args.recover_at, "detect": args.detect, "buffer_ms": args.buffer}
    pt.watchable(f"{sub}/live.ts", f"{sub}/live.mp4")
    r["psnr"], r["ssim"] = vs.quality(f"{sub}/live.ts", A["original"])
    r["damaged"], r["total"] = it.damaged_frames(f"{sub}/live.ts", A["ts"])
    I, P, B = dep["I"], dep["P"], dep["B"]
    info(f"    I {I[0]}/{I[1]}, P {P[0]}/{P[1]}, B {B[0]}/{B[1]}, GOPs lost {dep['gops_lost']}; "
         f"frames shown {dep['shown']}/{len(frames)} (weak period {r['weak_shown']}/{len(weak)}); "
         f"PSNR {r['psnr'] or '-'} dB; delay avg {vs.f1(r['delay_avg'], ' ms')}\n")
    return r


def shown_flags(frames, cov):
    """Like iframe_test.dependency, but one True/False per frame (decode order)."""
    ok_ref, out = [], []
    for f in frames:
        t = f["type"] if f["type"] in ("I", "P", "B") else "P"
        complete = bool(f["packets"]) and all(cov[k] >= TSP for k in f["packets"])
        if t == "I":
            ok_ref = [complete]
            dec = complete
        elif t == "P":
            dec = complete and bool(ok_ref) and ok_ref[-1]
            ok_ref.append(dec)
        else:
            dec = complete and len(ok_ref) >= 1 and all(ok_ref[-2:])
        out.append(dec)
    return out


# ---------------------------------------------------------------------------
#  Results and videos
# ---------------------------------------------------------------------------
def save(outdir, runs):
    f = lambda x, n=2: "" if x is None or x == "" else (f"{x:.{n}f}" if isinstance(x, float) else str(x))
    path = f"{outdir}/adaptive_summary.csv"
    hdr = ["codec", "scenario", "variant", "main", "helper", "i_complete", "i_total", "p_complete", "p_total",
           "b_complete", "b_total", "gops_lost", "gops", "frames_shown", "frames", "weak_shown", "weak_frames",
           "frames_damaged", "frames_total", "psnr_db", "ssim", "delay_avg_ms", "delay_p95_ms", "delay_i_ms",
           "late_pct", "bytes_on_time_pct", "mb_wifi", "mb_5g", "resent_i_pct", "background_mbps",
           "weak_mbps", "drop_at_s", "recover_at_s", "detect_s", "buffer_ms"]
    old = []
    try:
        done = {(r["codec"], r["scenario"], r["variant"]) for r in runs}
        with open(path) as fh:
            old = [r for r in csv.DictReader(fh)
                   if all(k in r for k in hdr)                       # skip rows of an older file format
                   and (r["codec"], r["scenario"], r["variant"]) not in done]
    except (OSError, KeyError, csv.Error):
        old = []
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(hdr)
        for r in old:
            w.writerow([r.get(k, "") for k in hdr])
        for r in runs:
            dp = r["dep"]
            w.writerow([r["codec"], r["scenario"], r["variant"], r["main"], r["helper"], dp["I"][0], dp["I"][1],
                        dp["P"][0], dp["P"][1], dp["B"][0], dp["B"][1], dp["gops_lost"], dp["gops"], dp["shown"],
                        r["frames"], r["weak_shown"], r["weak_frames"], r["damaged"], r["total"], r["psnr"],
                        r["ssim"], f(r["delay_avg"], 1), f(r["delay_p95"], 1), f(r["delay_i"], 1),
                        f(r["late_pct"]), f(r["ontime_pct"]), f(r["mb_wifi"]), f(r["mb_5g"]), f(r["resent_i"]),
                        f(r["bg_mbps"], 1), f(float(r["weak_mbps"]), 1), f(float(r["drop_at"]), 1),
                        f(float(r["recover_at"]), 1), f(float(r["detect"]), 1), f(float(r["buffer_ms"]), 0)])


def videos(outdir, codecs, scens, drop_at, recover_at, weak_mbps):
    """Per codec and scenario: Original | no adaptation | adaptive, with frame number + type,
    red I-frame highlight, and a bar that shows the main path's signal state."""
    import frame_videos as fv
    fnt = fv.font()
    made = []
    for c in codecs:
        d = os.path.join(outdir, c)
        ts = os.path.join(d, "video.ts")
        if not os.path.exists(ts):
            continue
        itimes = fv.i_times(ts)
        for sc in scens:
            main, helper = SCEN[sc]
            ins = [(f"Original {fv.CODEC[c]}", ts),
                   (f"{fv.CODEC[c]} no adaptation", os.path.join(d, f"{sc}_fixed", "live.ts")),
                   (f"{fv.CODEC[c]} adaptive", os.path.join(d, f"{sc}_adaptive", "live.ts"))]
            tmp = os.path.join(d, f"_grid_{sc}.mp4")
            if not fv.grid(ins, tmp, 640, 360, 3, fnt, itimes):
                continue
            out = os.path.join(outdir, f"adaptive_{sc}_{c}.mp4")
            M, H = LABEL[main], LABEL[helper]
            good = f"{M} signal GOOD - whole video on {M} (I stream + P/B datagrams)"
            weak = f"{M} signal WEAK ({weak_mbps:g} Mbit/s) - adaptive: I on {M} stream, P/B on {H} datagrams"
            esc = lambda t: t.replace(":", "\\:").replace(",", "\\,")
            bar = lambda txt, col, en: (f"drawtext=fontfile='{fnt}':text='{esc(txt)}':x=(w-tw)/2:y=h-th-6:fontsize=22:"
                                        f"fontcolor=white:box=1:boxcolor={col}@0.85:boxborderw=8:enable='{en}'")
            vf = (f"pad=iw:ih+44:0:0:black,"
                  + bar(good, "green", f"lt(t,{drop_at})+gte(t,{recover_at})") + ","
                  + bar(weak, "red", f"between(t,{drop_at},{recover_at})"))
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp, "-vf", vf, "-c:v", "libx264",
                                "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", out],
                               capture_output=True, text=True)
            os.remove(tmp)
            if r.returncode == 0:
                made.append(out)
            else:
                print("ffmpeg failed:", r.stderr[-500:])
    return made


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Adaptive frame-aware sending when the main path gets weak")
    ap.add_argument("--config", default=None)
    ap.add_argument("--video", default=None)
    ap.add_argument("--codecs", default="h264,hevc")
    ap.add_argument("--scenarios", default="A,B", help="A = 5G main + Wi-Fi helper, B = Wi-Fi main + 5G helper")
    ap.add_argument("--variants", default="fixed,adaptive")
    ap.add_argument("--weak-mbps", type=float, default=2.0, help="main path bandwidth while weak (default 2)")
    ap.add_argument("--drop-at", type=float, default=5.0, help="signal gets weak at (s, default 5)")
    ap.add_argument("--recover-at", type=float, default=15.0, help="signal good again at (s, default 15)")
    ap.add_argument("--detect", type=float, default=0.5, help="seconds until the sender notices (default 0.5)")
    ap.add_argument("--buffer", type=float, default=150.0, help="live player buffer, ms (default 150)")
    ap.add_argument("--no-background", dest="background", action="store_false")
    ap.add_argument("--picoquic", default=None)
    ap.add_argument("--out", default="adaptive_results")
    ap.add_argument("--videos-only", action="store_true", help="only make the VLC videos from saved results")
    args = ap.parse_args()

    cfg = netconfig.load(args.config)
    topo.apply_config(cfg)
    vc = vs.video_cfg(args.config)
    src = args.video if args.video is not None else vc["file"]
    codecs = [c.strip() for c in args.codecs.split(",") if c.strip() in it.CODEC_LABEL]
    scens = [s.strip().upper() for s in args.scenarios.split(",") if s.strip().upper() in SCEN]
    variants = [v.strip() for v in args.variants.split(",") if v.strip() in VARIANTS]
    setLogLevel("info")
    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)

    if not args.videos_only:
        import quic_test as qt
        pq = qt.find_picoquic(args.picoquic)
        if "QUICPERF_VIDEO_IN" not in open(os.path.join(pq, "picohttp", "quicperf.c")).read():
            raise SystemExit("picoquic cannot carry the real video yet: python3 patch_quicperf_video.py ~/picoquic "
                             "&& (cd ~/picoquic && make picoquicdemo)")
        analysis = {}
        for c in codecs:
            d = os.path.join(outdir, c)
            os.makedirs(d, exist_ok=True)
            info(f"\n*** {it.CODEC_LABEL[c]}: encoding {vc['length_s']} s at {vc['bitrate_mbps']:g} Mbit/s\n")
            it.encode(src, f"{d}/original.mp4", c, vc["bitrate_mbps"], vc["length_s"])
            ts = f"{d}/video.ts"
            vs.sh(f"ffmpeg -y -loglevel error -i '{d}/original.mp4' -c copy -f mpegts '{ts}'")
            frames = it.frame_list(ts)
            _, owner = it.split_ts(ts, frames, f"{d}/part_I.ts", f"{d}/part_PB.ts")
            secs = float(vs.sh(f"ffprobe -v error -show_entries format=duration -of csv=p=0 '{ts}'").stdout.strip()
                         or vc["length_s"])
            analysis[c] = {"ts": ts, "frames": frames, "owner": owner, "original": f"{d}/original.mp4", "secs": secs}
        runs = []
        subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        net = topo.build_topology()
        net.get("srv").cmd("for i in srv-wifi srv-5g; do ethtool -K $i gro off 2>/dev/null; done")
        try:
            for c in codecs:
                A = analysis[c]
                info(f"\n######## {it.CODEC_LABEL[c]} ########\n")
                for sc in scens:
                    for v in variants:
                        r = run(net, sc, v, pq, os.path.join(outdir, c), A, A["secs"], args, cfg["bg_share"])
                        r["codec"] = c
                        runs.append(r)
        finally:
            info("*** Stopping network\n")
            net.stop()
        save(outdir, runs)
    made = videos(outdir, codecs, scens, args.drop_at, args.recover_at, args.weak_mbps)
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    for m in made:
        print("Made:", m)
    import show_results
    show_results.show_adaptive(outdir)
    if made:
        print("\nOn the Mac:\n  scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/adaptive_results/adaptive_*.mp4' ~/Downloads/\n"
              "  open -a VLC ~/Downloads/adaptive_A_h264.mp4")


if __name__ == "__main__":
    main()
