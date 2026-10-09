#!/usr/bin/env python3
"""
LIVE video over QUIC (picoquic "quicperf" media test).

A live stream sends a new video frame every 1/30 s and each frame is only
useful if it arrives in time (before its play-out deadline). This script
sends a 4 Mbit/s, 30 frames/s live stream from sta1 (sender, QUIC server)
to srv (receiver, QUIC client) over each network, in two QUIC modes:

  1. QUIC STREAMS   (reliable)   - lost packets are resent, every frame
                                   arrives, but resent frames arrive late
  2. QUIC DATAGRAMS (unreliable) - RFC 9221, like plain UDP inside QUIC:
                                   lost packets are not resent, frames are
                                   on time but some are missing

For every frame the receiver records when it should have been sent and when
it arrived, so we get: frames received, frames lost, delay of each frame and
how many frames missed the live deadline (default 150 ms).

Background traffic, capacity, delay and loss change every step exactly as
in video_stream.py (config.ini [background] and [vary]).

Run:   sudo python3 quic_live.py
       sudo python3 quic_live.py --deadline 200 --time 30
       sudo python3 quic_live.py --fixed
       python3 show_results.py --live          # show the tables again
Needs: bash install_picoquic.sh  (builds picoquic with per-frame reports)
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
import quic_test as qt

PATHS = topo.PATHS
LABEL = {"wifi": "Wi-Fi", "5g": "5G"}
MODE_LABEL = {"stream": "QUIC streams (reliable)", "dgram": "QUIC datagrams (unreliable)"}
PORT = {"wifi": 4453, "5g": 4454}
FPS = 30
FRAME = 16667            # bytes per frame at 30 fps = 4 Mbit/s
DG_RATE = 209            # datagrams per second per stream (2 streams = 4 Mbit/s)
DG_SIZE = 1200


def scenario(mode, secs):
    if mode == "stream":
        return f"=video:s{FPS}:n{FPS * secs}:{FRAME};"
    n = DG_RATE * secs
    return f"=dgA:d{DG_RATE}:n{n}:{DG_SIZE};=dgB:d{DG_RATE}:n{n}:{DG_SIZE};"


def expected_frames(mode, secs):
    return FPS * secs if mode == "stream" else 2 * DG_RATE * secs


def read_frames(path):
    """Per-frame report: stream, repeat, group, frame, init_time, recv_time (us)."""
    out = []
    try:
        with open(path) as fh:
            next(fh, None)
            for line in fh:
                f = [x.strip() for x in line.split(",")]
                if len(f) >= 6 and f[4].isdigit() and f[5].isdigit():
                    out.append((int(f[4]), int(f[5])))
    except OSError:
        pass
    return out


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = min(len(sorted_vals) - 1, int(round(p / 100.0 * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def live_one(net, n, mode, pq, outdir, secs, background, bg_share, vary, deadline_ms):
    sta1, srv = net.get("sta1"), net.get("srv")
    p = PATHS[n]
    port = PORT[n]
    base_dly = float(p["delay"].rstrip("ms"))
    tag = f"{n}_{mode}"
    perf_srv = f"{outdir}/perf_server_{tag}.csv"
    rep = f"{outdir}/frames_{tag}.csv"
    m = {"mode": mode, "steps": []}
    info(f"\n========== LIVE over {LABEL[n]} - {MODE_LABEL[mode]} ==========\n")

    sta1.cmd(f"cd {pq} && ./picoquicdemo -0 -p {port} -c certs/cert.pem -k certs/key.pem "
             f"-F {perf_srv} > {outdir}/live_server_{tag}.log 2>&1 &")
    time.sleep(1)
    sta1.cmd(f"iptables -w -I OUTPUT -p udp --sport {port}")
    srv.cmd(f"iptables -w -I INPUT -p udp --sport {port}")

    step = vary["step"] if vary else 5
    sched = vs.make_schedule(vary, int(secs // step) + 6) if vary else []
    bg_ports = (p["bg_port"], p["bg_port"] + 10)
    if background:
        for bp in bg_ports:
            srv.cmd(f"iperf3 -s -D -p {bp}")
        time.sleep(1)

    def apply_step(i, extra=0):
        if vary:
            f = sched[i]
            cap, dly, los = p["bw"] * f["cap_f"], base_dly * f["dly_f"], p["loss"] * f["loss_f"]
            share = f["share"]
            vs.shape(net, n, cap, dly, los)
        else:
            cap, dly, los, share = p["bw"], base_dly, p["loss"], bg_share
        bg = max(1, int(cap * share)) if background else 0
        if background:
            sta1.cmd(f"iperf3 -c {p['server_ip']} -p {bg_ports[i % 2]} -u -b {bg}M "
                     f"-t {step + extra} -J > {outdir}/lbg_{tag}_{i:02d}.json 2>&1 &")
        return {"step": i, "t0": i * step, "share": share if background else 0.0,
                "bg_target": bg, "cap": cap, "delay": dly, "loss": los}

    # warm-up: set first conditions and start background before the stream
    cur = apply_step(0, extra=3)
    m["steps"].append(cur)
    time.sleep(3)
    sta1.cmd(f"ping -D -i 0.2 -w {secs + 15} {p['server_ip']} > {outdir}/lping_{tag}.log 2>&1 &")

    done = f"{outdir}/live_client_{tag}.done"
    srv.cmd(f"(cd {outdir} && QUICPERF_REPORT={rep} timeout {secs + 40} {pq}/picoquicdemo -0 "
            f"-a perf -n test -F {outdir}/perf_client_{tag}.csv {p['client_ip']} {port} "
            f"'{scenario(mode, secs)}' > {outdir}/live_client_{tag}.log 2>&1; touch {done}) &")
    start = time.time()
    info(f"    sending a live 4 Mbit/s, {FPS} fps stream for {secs}s, conditions change every {step}s\n")
    while time.time() - start < secs + 40:
        el = time.time() - start
        i = int(el // step)
        if i > cur["step"] and i < len(sched or [0] * 99) and el < secs + 2:
            cur = apply_step(i)
            m["steps"].append(cur)
            bgtxt = f"{cur['share']*100:3.0f}%={cur['bg_target']}M" if background else "off"
            info(f"    t={cur['t0']:3d}s  background {bgtxt:>10} | capacity {cur['cap']:5.0f} Mb/s | "
                 f"delay {cur['delay']:5.1f} ms | loss {cur['loss']:5.2f} %\n")
        if os.path.exists(done):
            break
        time.sleep(0.5)

    sta1.cmd("pkill -INT picoquicdemo")
    time.sleep(2)
    tx = qt.ipt_sport(sta1, "OUTPUT", port)
    rx = qt.ipt_sport(srv, "INPUT", port)
    m["pk_tx"], m["pk_rx"] = tx[0], rx[0]
    m["pk_loss"] = 100.0 * (tx[0] - rx[0]) / tx[0] if tx[0] else None

    # frames
    frames = read_frames(rep)
    exp = expected_frames(mode, secs)
    delays = sorted((r - s) / 1000.0 for s, r in frames)
    late = sum(1 for d in delays if d > deadline_ms)
    m.update({"expected": exp, "received": len(frames), "lost": max(0, exp - len(frames)),
              "late": late, "on_time": len(frames) - late,
              "d_min": delays[0] if delays else None, "d_avg": sum(delays) / len(delays) if delays else None,
              "d_p95": pct(delays, 95), "d_max": delays[-1] if delays else None})
    # per step: frames and delay
    if frames:
        t_first = min(s for s, _ in frames)
        per = {}
        for s, r in frames:
            k = int((s - t_first) / 1e6 // step)
            per.setdefault(k, []).append((r - s) / 1000.0)
        for st in m["steps"]:
            ds = per.get(st["step"], [])
            st["frames"] = len(ds)
            st["exp_frames"] = int(round(exp * max(0, min(step, secs - st["t0"])) / secs))
            st["avg_delay"] = sum(ds) / len(ds) if ds else None
            st["late"] = sum(1 for d in ds if d > deadline_ms)
    # QUIC stats from the sender (server) report
    perf = qt.read_perf(perf_srv)
    if perf:
        r = perf[-1]
        try:
            m["pkt_sent"], m["retrans"] = int(r.get("pkt_sent", 0)), int(r.get("retrans.", 0))
            m["srtt"], m["minrtt"] = int(r.get("srtt", 0)) / 1000.0, int(r.get("minrtt", 0)) / 1000.0
            m["cc"] = qt.CC_NAME.get(r.get("ccalgo", ""), r.get("ccalgo", ""))
        except ValueError:
            pass
    rtts = vs.ping_by_step(f"{outdir}/lping_{tag}.log", start, step, len(m["steps"]))
    for st, rt in zip(m["steps"], rtts):
        st["rtt"] = rt
        st["bg_mbps"] = vs.iperf_bg(f"{outdir}/lbg_{tag}_{st['step']:02d}.json")[0] if background else None
    rs = [x["rtt"] for x in m["steps"] if x.get("rtt")]
    m["rtt"] = sum(rs) / len(rs) if rs else None
    m["bg_target"] = qt.avg([s["bg_target"] for s in m["steps"]]) if background else 0
    m["bg_mbps"] = qt.avg([s.get("bg_mbps") for s in m["steps"]]) if background else None
    if n == "wifi":
        m["pl"] = vs.wifi_path_loss(net)

    if vary:
        vs.shape(net, n, p["bw"], base_dly, p["loss"])
    sta1.cmd(f"iptables -w -D OUTPUT -p udp --sport {port}")
    srv.cmd(f"iptables -w -D INPUT -p udp --sport {port}")
    srv.cmd("pkill -9 picoquicdemo; pkill -9 iperf3")
    sta1.cmd("pkill -9 picoquicdemo; pkill -9 iperf3; pkill -9 ping")
    time.sleep(2)
    info(f"    received {m['received']}/{exp} frames, {late} late (> {deadline_ms:g} ms), "
         f"average delay {vs.f1(m['d_avg'], ' ms')}\n")
    return m


def save(meas, outdir, deadline_ms, vary):
    f2 = lambda x, d=2: "" if x is None else f"{x:.{d}f}"
    with open(f"{outdir}/live_summary.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "mode", "frames_expected", "frames_received", "frames_lost",
                    "frames_late", "frames_on_time", "on_time_pct", "deadline_ms",
                    "delay_min_ms", "delay_avg_ms", "delay_p95_ms", "delay_max_ms",
                    "radio_path_loss_db", "configured_loss_pct", "packets_sent", "packets_received",
                    "network_packet_loss_pct", "quic_packets_sent", "quic_retransmissions",
                    "configured_rtt_ms", "ping_rtt_ms", "quic_srtt_ms", "quic_min_rtt_ms",
                    "background_target_mbps", "background_achieved_mbps", "congestion_control",
                    "conditions"])
        for (n, mode), m in meas.items():
            exp = m["expected"]
            w.writerow([n, mode, exp, m["received"], m["lost"], m["late"], m["on_time"],
                        f2(100.0 * m["on_time"] / exp if exp else None), deadline_ms,
                        f2(m["d_min"]), f2(m["d_avg"]), f2(m["d_p95"]), f2(m["d_max"]),
                        f2(m["pl"]["path_loss_db"], 1) if m.get("pl") else "",
                        PATHS[n]["loss"], m["pk_tx"], m["pk_rx"], f2(m["pk_loss"], 3),
                        m.get("pkt_sent", ""), m.get("retrans", ""),
                        2 * float(PATHS[n]["delay"].rstrip("ms")), f2(m["rtt"]),
                        f2(m.get("srtt")), f2(m.get("minrtt")), f2(m["bg_target"], 1),
                        f2(m["bg_mbps"], 1), m.get("cc", ""),
                        "random every step" if vary else "fixed"])
    for (n, mode), m in meas.items():
        with open(f"{outdir}/live_steps_{n}_{mode}.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["step", "start_s", "background_pct", "background_target_mbps",
                        "capacity_mbps", "delay_ms", "loss_pct", "ping_rtt_ms",
                        "frames_expected", "frames_received", "avg_frame_delay_ms", "late_frames"])
            for s in m["steps"]:
                w.writerow([s["step"], s["t0"], f2(s["share"] * 100, 0), s["bg_target"],
                            f2(s["cap"], 0), f2(s["delay"], 1), f2(s["loss"]), f2(s.get("rtt"), 1),
                            s.get("exp_frames", ""), s.get("frames", ""),
                            f2(s.get("avg_delay"), 1), s.get("late", "")])


def main():
    ap = argparse.ArgumentParser(description="Live video over QUIC (streams vs datagrams)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--path", choices=["wifi", "5g", "both"], default="both")
    ap.add_argument("--mode", choices=["stream", "dgram", "both"], default="both")
    ap.add_argument("--time", type=int, default=20, help="seconds of live stream per test")
    ap.add_argument("--deadline", type=float, default=150.0,
                    help="live deadline in ms: later frames are useless (default 150)")
    ap.add_argument("--fixed", action="store_true", help="constant config.ini values")
    ap.add_argument("--no-background", action="store_true")
    ap.add_argument("--picoquic", default=None)
    ap.add_argument("--out", default="live_results")
    args = ap.parse_args()

    pq = qt.find_picoquic(args.picoquic)
    src = open(os.path.join(pq, "picoquicfirst", "picoquicdemo.c")).read()
    if "QUICPERF_REPORT" not in src:
        raise SystemExit("picoquic needs the per-frame report patch: run  bash install_picoquic.sh  again")
    cfg = netconfig.load(args.config)
    topo.apply_config(cfg)
    vc = vs.video_cfg(args.config)
    background = not args.no_background
    vary = None if (args.fixed or not vc["vary"]) else {
        "step": cfg["bg_step"], "bg_min": cfg["bg_min"], "bg_max": cfg["bg_max"],
        "capacity_pct": vc["capacity_pct"], "delay_pct": vc["delay_pct"],
        "loss_pct": vc["loss_pct"], "seed": cfg["seed"]}
    names = ["wifi", "5g"] if args.path == "both" else [args.path]
    modes = ["stream", "dgram"] if args.mode == "both" else [args.mode]
    print(netconfig.summary(cfg) +
          f"  Live stream: 4 Mbit/s, {FPS} frames/s, {args.time}s per test, deadline {args.deadline:g} ms\n"
          f"  QUIC modes: {', '.join(MODE_LABEL[x] for x in modes)}\n"
          f"  Background: {'on' if background else 'off'}, conditions: "
          f"{'random every %ss' % cfg['bg_step'] if vary else 'fixed'}\n", flush=True)

    setLogLevel("info")
    outdir = os.path.abspath(args.out)
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = topo.build_topology()
    meas = {}
    try:
        for n in names:
            for mode in modes:
                meas[(n, mode)] = live_one(net, n, mode, pq, outdir, args.time, background,
                                           cfg["bg_share"], vary, args.deadline)
    finally:
        info("*** Stopping network\n")
        net.stop()
    save(meas, outdir, args.deadline, vary)
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    import show_results
    show_results.show_live(outdir)
    info(f"\n*** All files in {outdir}/\n*** Show the tables again:  python3 show_results.py --live\n")


if __name__ == "__main__":
    main()
