#!/usr/bin/env python3
"""
QUIC test with picoquic (https://github.com/private-octopus/picoquic).

QUIC is a transport protocol that runs over UDP (it is what HTTP/3 uses).
It adds what plain UDP does not have: encryption (TLS 1.3), reliable
delivery (lost packets are resent), congestion control (BBR by default)
and multiple streams in one connection.

This script checks that QUIC works over the emulated networks:
  sta1 (sender)   runs the picoquic server and sends the video
  srv  (receiver) runs the picoquic client and requests/receives it over QUIC/UDP
  (same direction as the video test, so the configured loss hits the data)
Each network is tested on its own: first Wi-Fi, then 5G.
For TEST seconds the client downloads the video again and again, while the
background traffic, capacity, delay and loss change every step (same random
changes as video_stream.py, from config.ini [background] and [vary]).

Run:   sudo python3 quic_test.py --video BBB.webm
       sudo python3 quic_test.py --fixed          # constant config.ini values
       sudo python3 quic_test.py --path wifi      # one network only
       python3 show_results.py --quic             # show the tables again
Needs: picoquic built in ~/picoquic   (see install_picoquic.sh)
"""
import argparse
import csv
import hashlib
import os
import re
import shutil
import subprocess
import time

from mininet.log import setLogLevel, info

import netconfig
import wifi_5g_topology as topo
import video_stream as vs

PATHS = topo.PATHS
LABEL = {"wifi": "Wi-Fi", "5g": "5G"}
QUIC_PORT = {"wifi": 4443, "5g": 4444}
CC_NAME = {"1": "NewReno", "2": "CUBIC", "3": "DCUBIC", "4": "FAST", "5": "BBR",
           "6": "Prague", "7": "BBRv1", "8": "C4"}


def find_picoquic(arg):
    """picoquicdemo location: --picoquic, else ~/picoquic of the user who ran sudo."""
    cands = [arg] if arg else []
    user = os.environ.get("SUDO_USER") or ""
    cands += [os.path.expanduser(f"~{user}/picoquic"), os.path.expanduser("~/picoquic")]
    for d in cands:
        if d and os.path.isfile(os.path.join(d, "picoquicdemo")):
            return os.path.abspath(d)
    raise SystemExit("picoquicdemo not found. Build it first:  bash install_picoquic.sh")


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def ipt_sport(node, chain, port):
    """(packets, bytes) of the iptables rule counting UDP from source port."""
    for line in node.cmd(f"iptables -w -L {chain} -v -x -n 2>/dev/null").splitlines():
        if f"spt:{port}" in line:
            f = line.split()
            try:
                return int(f[0]), int(f[1])
            except (ValueError, IndexError):
                pass
    return (0, 0)


def read_perf(path):
    """picoquic -F performance log: one row per connection."""
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]
    return [dict(zip(head, [c.strip() for c in r])) for r in rows[1:] if len(r) >= len(head) - 1]


def client_stats(log):
    """Bytes, seconds and Mbit/s from the client output."""
    try:
        txt = open(log).read()
    except OSError:
        return None
    m = re.search(r"Received (\d+) bytes in ([\d.]+) seconds, ([\d.]+) Mbps", txt)
    ok = "Client exit with code = 0" in txt
    return {"ok": ok, "bytes": int(m.group(1)) if m else 0,
            "secs": float(m.group(2)) if m else None,
            "mbps": float(m.group(3)) if m else None,
            "alpn": (re.search(r"Negotiated ALPN: (\S+)", txt) or [None, ""])[1]}


def quic_one(net, n, pq, video, outdir, test_s, background, bg_share, vary):
    sta1, srv = net.get("sta1"), net.get("srv")
    p = PATHS[n]
    port = QUIC_PORT[n]
    base_dly = float(p["delay"].rstrip("ms"))
    pdir = os.path.join(outdir, f"quic_{n}")
    os.makedirs(pdir, exist_ok=True)
    perf_srv = os.path.join(outdir, f"perf_server_{n}.csv")
    vname = os.path.basename(video)
    want = md5(video)
    m = {"downloads": [], "steps": []}
    info(f"\n========== QUIC over {LABEL[n]}: capacity {p['bw']:g} Mbit/s, delay {p['delay']}, "
         f"loss {p['loss']:g} % ==========\n")
    if n == "wifi":
        m["pl"] = vs.wifi_path_loss(net)

    # QUIC server on sta1 = the SENDER of the video (same direction as the video
    # test, so the configured loss applies to the data). -0 turns off UDP GSO so
    # every packet is counted one by one.
    sta1.cmd(f"cd {pq} && ./picoquicdemo -0 -p {port} -c certs/cert.pem -k certs/key.pem "
            f"-w {outdir} -F {perf_srv} > {outdir}/quic_server_{n}.log 2>&1 &")
    time.sleep(1)
    # count QUIC data packets: sender sta1 (OUTPUT) / receiver srv (INPUT)
    sta1.cmd(f"iptables -w -I OUTPUT -p udp --sport {port}")
    srv.cmd(f"iptables -w -I INPUT -p udp --sport {port}")

    step = vary["step"] if vary else 5
    sched = vs.make_schedule(vary, int(test_s // step) + 4) if vary else []
    bg_ports = (p["bg_port"], p["bg_port"] + 10)
    if background:
        for bp in bg_ports:
            srv.cmd(f"iperf3 -s -D -p {bp}")
        time.sleep(1)

    def apply_step(i):
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
                     f"-t {step + 3} -J > {outdir}/qbg_{n}_{i:02d}.json 2>&1 &")
        return {"step": i, "t0": i * step, "share": share if background else 0.0,
                "bg_target": bg, "cap": cap, "delay": dly, "loss": los}

    sta1.cmd(f"ping -D -i 0.2 -w {int(test_s) + 20} {p['server_ip']} > {outdir}/qping_{n}.log 2>&1 &")
    cur = apply_step(0)
    m["steps"].append(cur)
    time.sleep(2)                                   # warm-up
    info(f"    {'#':>3} {'start':>6} | {'bg':>11} | {'capacity':>9} | {'delay':>8} | {'loss':>7} || "
         f"{'time':>7} {'speed':>12} {'file':>9}\n")
    start = time.time()
    k = 0
    while time.time() - start < test_s:
        el = time.time() - start
        i = int(el // step)
        if i > cur["step"]:
            cur = apply_step(i)
            m["steps"].append(cur)
        k += 1
        log = f"{outdir}/quic_client_{n}_{k:02d}.log"
        t0 = time.time()
        srv.cmd(f"cd {pdir} && rm -f {vname} && timeout 60 {pq}/picoquicdemo -0 -o {pdir} "
                 f"-F {outdir}/perf_client_{n}_{k:02d}.csv "
                 f"-n test {p['client_ip']} {port} '/{vname}' > {log} 2>&1")
        dt = time.time() - t0
        st = client_stats(log) or {"ok": False}
        got = os.path.join(pdir, vname)
        same = os.path.exists(got) and md5(got) == want
        cr = read_perf(f"{outdir}/perf_client_{n}_{k:02d}.csv")
        d = {"cnx": cr[0].get("CNX_ID", "") if cr else "", "n": k, "start": el, "step": cur["step"], "wall_s": dt, "ok": st.get("ok") and same,
             "identical": same, "secs": st.get("secs"), "mbps": st.get("mbps"),
             "bytes": st.get("bytes", 0), "alpn": st.get("alpn", "")}
        m["downloads"].append(d)
        bgtxt = f"{cur['share']*100:3.0f}%={cur['bg_target']:4d}M" if background else "off"
        info(f"    {k:3d} {el:5.1f}s | {bgtxt:>11} | {cur['cap']:5.0f} Mb/s | {cur['delay']:5.1f} ms | "
             f"{cur['loss']:5.2f} % || {vs.f1(d['secs'], ' s', 2):>7} "
             f"{vs.f1(d['mbps'], ' Mbit/s', 1):>12} {'OK' if same else 'FAILED':>9}\n")
    m["test_s"] = time.time() - start

    time.sleep(2)
    sta1.cmd("pkill -INT picoquicdemo")    # server writes its last report when it stops
    time.sleep(2)
    tx = ipt_sport(sta1, "OUTPUT", port)
    rx = ipt_sport(srv, "INPUT", port)
    m["pk_tx"], m["pk_rx"] = tx[0], rx[0]
    m["pk_loss"] = 100.0 * (tx[0] - rx[0]) / tx[0] if tx[0] else None

    # per-connection server stats (packets sent, retransmissions, RTT)
    perf = read_perf(perf_srv)
    by_cnx = {r.get("CNX_ID", ""): r for r in perf}
    for d in m["downloads"]:
        r = by_cnx.get(d["cnx"])
        if not r:
            continue
        try:
            d["pkt_sent"] = int(r.get("pkt_sent", 0))
            d["retrans"] = int(r.get("retrans.", 0))
            d["srtt_ms"] = int(r.get("srtt", 0)) / 1000.0
            d["minrtt_ms"] = int(r.get("minrtt", 0)) / 1000.0
        except ValueError:
            pass
    cc = perf[0].get("ccalgo", "") if perf else ""
    m["ccalgo"] = CC_NAME.get(cc, cc)

    # RTT (ping) and background achieved per step
    rtts = vs.ping_by_step(f"{outdir}/qping_{n}.log", start, step, len(m["steps"]))
    for s_, r_ in zip(m["steps"], rtts):
        s_["rtt"] = r_
        s_["bg_mbps"], s_["bg_loss"] = (vs.iperf_bg(f"{outdir}/qbg_{n}_{s_['step']:02d}.json")
                                        if background else (None, None))
    rs = [x["rtt"] for x in m["steps"] if x.get("rtt")]
    m["rtt"] = sum(rs) / len(rs) if rs else None

    if vary:
        vs.shape(net, n, p["bw"], base_dly, p["loss"])
    sta1.cmd(f"iptables -w -D OUTPUT -p udp --sport {port}")
    srv.cmd(f"iptables -w -D INPUT -p udp --sport {port}")
    srv.cmd("pkill -INT picoquicdemo; sleep 1; pkill -9 picoquicdemo; pkill -9 iperf3")
    sta1.cmd("pkill -9 picoquicdemo; pkill -9 iperf3; pkill -9 ping")
    time.sleep(2)
    return m


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def save(meas, outdir, video, background, vary):
    size = os.path.getsize(video)
    cols = ["path", "file_mb", "downloads", "downloads_ok", "all_identical",
            "avg_download_s", "min_download_s", "max_download_s", "avg_throughput_mbps",
            "radio_path_loss_db", "configured_loss_pct", "quic_packets_sent",
            "quic_packets_received", "network_packet_loss_pct", "avg_retransmissions",
            "retransmission_pct", "configured_rtt_ms", "ping_rtt_ms", "quic_srtt_ms",
            "quic_min_rtt_ms", "background_target_mbps", "background_achieved_mbps",
            "congestion_control", "alpn", "conditions"]
    with open(f"{outdir}/quic_summary.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for n, m in meas.items():
            dl = m["downloads"]
            ok = [d for d in dl if d["ok"]]
            secs = [d["secs"] for d in ok if d.get("secs")]
            ps = sum(d.get("pkt_sent", 0) for d in dl)
            rt = sum(d.get("retrans", 0) for d in dl)
            st = m["steps"]
            w.writerow([n, f"{size/1e6:.2f}", len(dl), len(ok), "yes" if dl and len(ok) == len(dl) else "no",
                        f"{avg(secs):.3f}" if secs else "", f"{min(secs):.3f}" if secs else "",
                        f"{max(secs):.3f}" if secs else "",
                        f"{avg([d['mbps'] for d in ok]):.2f}" if ok else "",
                        f"{m['pl']['path_loss_db']:.1f}" if m.get("pl") else "",
                        PATHS[n]["loss"], m["pk_tx"], m["pk_rx"],
                        f"{m['pk_loss']:.3f}" if m["pk_loss"] is not None else "",
                        f"{rt/len(dl):.1f}" if dl else "", f"{100.0*rt/ps:.3f}" if ps else "",
                        2 * float(PATHS[n]["delay"].rstrip("ms")),
                        f"{m['rtt']:.2f}" if m["rtt"] else "",
                        f"{avg([d.get('srtt_ms') for d in dl]):.2f}" if dl and avg([d.get('srtt_ms') for d in dl]) else "",
                        f"{avg([d.get('minrtt_ms') for d in dl]):.2f}" if dl and avg([d.get('minrtt_ms') for d in dl]) else "",
                        f"{avg([s['bg_target'] for s in st]):.1f}" if background else "0",
                        f"{avg([s.get('bg_mbps') for s in st]):.1f}" if background and avg([s.get('bg_mbps') for s in st]) else "",
                        m.get("ccalgo", ""), (dl[0]["alpn"] if dl else ""),
                        "random every step" if vary else "fixed"])
        for n, m in meas.items():
            with open(f"{outdir}/quic_downloads_{n}.csv", "w", newline="") as f2:
                w2 = csv.writer(f2)
                w2.writerow(["download", "start_s", "step", "background_pct", "background_target_mbps",
                             "capacity_mbps", "delay_ms", "loss_pct", "download_s", "throughput_mbps",
                             "identical", "packets_sent", "retransmissions", "quic_srtt_ms", "ping_rtt_ms"])
                steps = {s["step"]: s for s in m["steps"]}
                for d in m["downloads"]:
                    s = steps.get(d["step"], {})
                    w2.writerow([d["n"], f"{d['start']:.1f}", d["step"],
                                 f"{s.get('share', 0)*100:.0f}", s.get("bg_target", 0),
                                 f"{s.get('cap', 0):.0f}", f"{s.get('delay', 0):.1f}",
                                 f"{s.get('loss', 0):.2f}",
                                 f"{d['secs']:.3f}" if d.get("secs") else "",
                                 f"{d['mbps']:.2f}" if d.get("mbps") else "",
                                 "yes" if d["identical"] else "no", d.get("pkt_sent", ""),
                                 d.get("retrans", ""), f"{d.get('srtt_ms', 0):.1f}",
                                 f"{s['rtt']:.1f}" if s.get("rtt") else ""])


def main():
    ap = argparse.ArgumentParser(description="QUIC (picoquic) over Wi-Fi and 5G")
    ap.add_argument("--config", default=None)
    ap.add_argument("--video", default=None, help="file to send (default: [video] file or test video)")
    ap.add_argument("--path", choices=["wifi", "5g", "both"], default="both")
    ap.add_argument("--time", type=int, default=20, help="seconds of repeated downloads per network")
    ap.add_argument("--fixed", action="store_true", help="constant config.ini values")
    ap.add_argument("--no-background", action="store_true")
    ap.add_argument("--picoquic", default=None, help="picoquic folder (default ~/picoquic)")
    ap.add_argument("--out", default="quic_results")
    args = ap.parse_args()

    pq = find_picoquic(args.picoquic)
    cfg = netconfig.load(args.config)
    topo.apply_config(cfg)
    vc = vs.video_cfg(args.config)
    src = args.video if args.video is not None else vc["file"]
    background = not args.no_background
    vary = None if (args.fixed or not vc["vary"]) else {
        "step": cfg["bg_step"], "bg_min": cfg["bg_min"], "bg_max": cfg["bg_max"],
        "capacity_pct": vc["capacity_pct"], "delay_pct": vc["delay_pct"],
        "loss_pct": vc["loss_pct"], "seed": cfg["seed"]}
    names = ["wifi", "5g"] if args.path == "both" else [args.path]
    print(netconfig.summary(cfg) +
          f"  Protocol: QUIC (picoquic, {pq}) over UDP\n"
          f"  File: {src or 'test video'}, downloads repeated for {args.time}s per network\n"
          f"  Background: {'on' if background else 'off'}, conditions: "
          f"{'random every %ss' % cfg['bg_step'] if vary else 'fixed'}\n", flush=True)

    setLogLevel("info")
    outdir = os.path.abspath(args.out)
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)
    video = os.path.join(outdir, "original.mp4")
    vs.prepare_video(src, video, vc["bitrate_mbps"], vc["length_s"])

    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = topo.build_topology()
    meas = {}
    try:
        for n in names:
            meas[n] = quic_one(net, n, pq, video, outdir, args.time, background,
                               cfg["bg_share"], vary)
    finally:
        info("*** Stopping network\n")
        net.stop()
    save(meas, outdir, video, background, vary)
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    import show_results
    show_results.show_quic(outdir)
    info(f"\n*** All files in {outdir}/\n*** Show the tables again:  python3 show_results.py --quic\n")


if __name__ == "__main__":
    main()
