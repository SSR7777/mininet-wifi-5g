#!/usr/bin/env python3
"""
Send a real video from the sender (sta1) to the receiver (srv), testing each
network individually: first over the Wi-Fi path only, then over the emulated
5G path only. Then compare what arrived with the original.

  sta1 (sender)  --Wi-Fi-->  srv (receiver)   -> received_wifi.ts
  sta1 (sender)  ---5G---->  srv (receiver)   -> received_5g.ts

Capacity, delay and loss come from config.ini (sections [wifi], [5g]);
video settings come from the [video] section.

Run:   sudo python3 video_stream.py                          # test video
       sudo python3 video_stream.py --path wifi              # Wi-Fi only
       sudo python3 video_stream.py --path 5g                # 5G only
       sudo python3 video_stream.py --video myvideo.mp4      # your own video
       sudo python3 video_stream.py --transport tcp          # reliable TCP
       sudo python3 video_stream.py --background             # + iPerf load
Output in video_results/:
  original.mp4          the video that was sent
  received_wifi.ts      what arrived over Wi-Fi
  received_5g.ts        what arrived over 5G
  side_by_side.mp4      Wi-Fi (left) and 5G (right) next to each other
  video_summary.csv     per path: transfer time, path loss, packet loss,
                        delay, background, frames, PSNR, SSIM
Needs: sudo apt install -y ffmpeg
"""
import argparse
import configparser
import os
import random
import re
import shutil
import subprocess
import time

from mininet.log import setLogLevel, info

import netconfig
import wifi_5g_topology as topo

PATHS = topo.PATHS
NAMES = ("wifi", "5g")
LABEL = {"wifi": "Wi-Fi", "5g": "5G"}
VIDEO_PORT = {"wifi": 6001, "5g": 6002}


def video_cfg(path):
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.read(path or netconfig.DEFAULT_FILE)
    get = lambda k, d: cp.get("video", k, fallback=d)
    return {"file": get("file", "").strip(),
            "bitrate_mbps": float(get("bitrate_mbps", "4")),
            "length_s": int(get("length_s", "20")),
            "transport": get("transport", "udp").strip().lower(),
            "background": get("background", "no").strip().lower() in ("yes", "true", "1"),
            "vary": cp.get("vary", "enabled", fallback="no").strip().lower() in ("yes", "true", "1"),
            "capacity_pct": cp.getfloat("vary", "capacity_pct", fallback=30),
            "delay_pct": cp.getfloat("vary", "delay_pct", fallback=50),
            "loss_pct": cp.getfloat("vary", "loss_pct", fallback=100)}


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def prepare_video(src, out, bitrate, length):
    """Re-encode to H.264 720p, fixed bitrate, a key frame every second."""
    if src:
        if not os.path.exists(src):
            raise SystemExit(f"Video not found: {src}")
        inp = f"-i '{src}' -t {length}"
        info(f"*** Preparing your video {src} ({length}s, {bitrate:g} Mbit/s)\n")
    else:
        inp = f"-f lavfi -i testsrc2=size=1280x720:rate=30 -t {length}"
        info(f"*** Creating a {length}s test video ({bitrate:g} Mbit/s)\n")
    r = sh(f"ffmpeg -y -loglevel error {inp} -an -vf scale=1280:720,fps=30 "
           f"-c:v libx264 -preset veryfast -b:v {bitrate}M -maxrate {bitrate}M "
           f"-bufsize {bitrate}M -g 30 -pix_fmt yuv420p '{out}'")
    if r.returncode != 0:
        raise SystemExit("ffmpeg failed:\n" + r.stderr)


def count_frames(path):
    r = sh(f"ffprobe -v error -select_streams v:0 -count_frames "
           f"-show_entries stream=nb_read_frames -of csv=p=0 '{path}'")
    nums = re.findall(r"\d+", r.stdout)
    return int(nums[0]) if nums else 0


def decode_errors(path):
    r = sh(f"ffmpeg -v error -i '{path}' -f null - 2>&1")
    return len([l for l in r.stdout.splitlines() if l.strip()])


def quality(received, original):
    """Average PSNR (dB) and SSIM (0-1) of received vs original."""
    r = sh(f"ffmpeg -i '{received}' -i '{original}' -lavfi "
           f"\"[0:v]setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];"
           f"[a][b]psnr;[0:v]setpts=PTS-STARTPTS[c];[1:v]setpts=PTS-STARTPTS[d];"
           f"[c][d]ssim\" -f null - 2>&1")
    psnr = re.search(r"PSNR .*average:([\d.]+|inf)", r.stdout)
    ssim = re.search(r"SSIM .*All:([\d.]+)", r.stdout)
    fmt = lambda m, d: ("" if not m else m.group(1) if m.group(1) == "inf"
                        else f"{float(m.group(1)):.{d}f}")
    return fmt(psnr, 1), fmt(ssim, 4)


# ---------------------------------------------------------------------------
#  Measurements
# ---------------------------------------------------------------------------
def ipt_counter(node, chain, proto, port):
    """(packets, bytes) counted by the iptables rule for the video port."""
    out = node.cmd(f"iptables -w -L {chain} -v -x -n 2>/dev/null")
    for line in out.splitlines():
        if f"dpt:{port}" in line:
            f = line.split()
            try:
                return int(f[0]), int(f[1])
            except (ValueError, IndexError):
                pass
    return None


def snmp(node, proto):
    """Counters from /proc/net/snmp (per network namespace)."""
    lines = [l.split() for l in node.cmd("cat /proc/net/snmp").splitlines()
             if l.startswith(proto + ":")]
    if len(lines) < 2:
        return {}
    return {k: int(v) for k, v in zip(lines[0][1:], lines[1][1:]) if v.lstrip("-").isdigit()}


def wifi_path_loss(net):
    """Radio path loss (dB) between ap1 and sta1 = Tx power + gains - RSSI."""
    sta1, ap1 = net.get("sta1"), net.get("ap1")
    dist = PATHS["wifi"].get("distance", 5)
    rssi = txp = None
    gains = 0.0
    try:
                                    # Mininet-WiFi's own propagation model
        si, ai = sta1.wintfs[0], ap1.wintfs[0]
        rssi = float(si.rssi)
        txp = float(ai.txpower)
        gains = float(getattr(ai, "antennaGain", 0)) + float(getattr(si, "antennaGain", 0))
    except Exception:
        pass
    if rssi is None or rssi == 0:
        m = re.search(r"signal:\s*(-?\d+)", sta1.cmd("iw dev sta1-wlan0 link"))
        if m:
            rssi = float(m.group(1))
    if txp is None:
        txp = 14.0                  # Mininet-WiFi default Tx power (dBm)
    if rssi is not None and rssi != 0:
        return {"distance_m": dist, "txpower_dbm": txp, "rssi_dbm": rssi,
                "path_loss_db": txp + gains - rssi, "source": "measured RSSI"}
    # fallback: free-space path loss at 2.412 GHz (channel 1)
    import math
    fspl = 20 * math.log10(max(dist, 0.1)) + 20 * math.log10(2412) - 27.55
    return {"distance_m": dist, "txpower_dbm": txp, "rssi_dbm": txp - fspl,
            "path_loss_db": fspl, "source": "free-space model"}


def ping_stats(path):
    try:
        txt = open(path).read()
    except OSError:
        return None, None
    m = re.search(r"= [\d.]+/([\d.]+)/", txt)
    l = re.search(r"([\d.]+)% packet loss", txt)
    if not m:                       # ping was stopped before its summary
        t = [float(x) for x in re.findall(r"time=([\d.]+)", txt)]
        seq = [int(x) for x in re.findall(r"icmp_seq=(\d+)", txt)]
        lost = (100.0 * (1 - len(set(seq)) / max(seq)) if seq and max(seq) > 0 else None)
        return (sum(t) / len(t) if t else None, lost)
    return (float(m.group(1)), float(l.group(1)) if l else None)


def iperf_bg(path):
    """Achieved background rate (Mbit/s) and its UDP loss (%) from iperf3 -J."""
    import json
    try:
        txt = open(path).read()
        s = json.loads(txt[txt.index("{"):]).get("end", {}).get("sum", {})
        return s.get("bits_per_second", 0) / 1e6, s.get("lost_percent")
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
#  Changing network conditions (random, every step_s seconds)
# ---------------------------------------------------------------------------
def make_schedule(vary, steps):
    """Random factors per step. The same pattern is used for Wi-Fi and 5G,
    so both networks face the same kind of disturbance."""
    rng = random.Random(vary["seed"])
    u = lambda pct: rng.uniform(1 - pct / 100.0, 1 + pct / 100.0)
    return [{"share": rng.uniform(vary["bg_min"], vary["bg_max"]),
             "cap_f": u(vary["capacity_pct"]),
             "dly_f": u(vary["delay_pct"]),
             "loss_f": max(0.0, u(vary["loss_pct"]))} for _ in range(steps)]


def shape(net, n, bw, delay_ms, loss):
    """Set capacity, delay and loss of one path right now (both directions)."""
    sta1 = net.get("sta1")
    if n == "wifi":
        sta1.cmd(f"tc qdisc replace dev sta1-wlan0 root netem rate {bw:.1f}mbit "
                 f"delay {delay_ms:.2f}ms loss {loss:.3f}% limit 10000")
        net.get("srv").cmd(f"tc qdisc replace dev srv-wifi root netem rate {bw:.1f}mbit "
                           f"delay {delay_ms:.2f}ms limit 10000")
    else:
        sta1.cmd(f"tc qdisc replace dev sta1-5g root netem rate {bw:.1f}mbit "
                 f"delay {delay_ms:.2f}ms loss {loss:.3f}% limit 10000")
        link = sta1.intf("sta1-5g").link
        other = link.intf2 if link.intf1.name == "sta1-5g" else link.intf1
        other.node.cmd(f"tc qdisc replace dev {other.name} root netem rate {bw:.1f}mbit "
                       f"delay {delay_ms:.2f}ms limit 10000")


def ping_by_step(path, t0, step, nsteps):
    """Average ping RTT in each step (ping -D gives a timestamp per reply)."""
    sums = [[0.0, 0] for _ in range(nsteps)]
    try:
        for line in open(path):
            mm = re.search(r"\[([\d.]+)\].*time=([\d.]+)", line)
            if mm:
                i = int((float(mm.group(1)) - t0) // step)
                if 0 <= i < nsteps:
                    sums[i][0] += float(mm.group(2))
                    sums[i][1] += 1
    except OSError:
        pass
    return [a / c if c else None for a, c in sums]


def stream_one(net, n, video, length, transport, background, bg_share, outdir, vary=None):
    """Send the video over ONE path (n) only, while the other path stays idle."""
    sta1, srv = net.get("sta1"), net.get("srv")
    p = PATHS[n]
    port = VIDEO_PORT[n]
    proto = "udp" if transport == "udp" else "tcp"
    base_dly = float(p["delay"].rstrip("ms"))
    m = {"bg_target": 0.0, "steps": []}
    info(f"\n========== {LABEL[n]} test: capacity {p['bw']:g} Mbit/s, delay {p['delay']}, "
         f"loss {p['loss']:g} % ==========\n")

    if n == "wifi":
        m["pl"] = wifi_path_loss(net)
        info(f"*** Wi-Fi radio: station {m['pl']['distance_m']:g} m from AP, "
             f"signal {m['pl']['rssi_dbm']:.1f} dBm, path loss "
             f"{m['pl']['path_loss_db']:.1f} dB ({m['pl']['source']})\n")

    # packet counters for the video only (sender OUT, receiver IN)
    sta1.cmd(f"iptables -w -I OUTPUT -p {proto} --dport {port}")
    srv.cmd(f"iptables -w -I INPUT -p {proto} --dport {port}")
    snmp_tx0, snmp_rx0 = snmp(sta1, proto.capitalize()), snmp(srv, proto.capitalize())

    step = vary["step"] if vary else None
    sched = make_schedule(vary, int((length + 40) // step) + 2) if vary else []
    bg_ports = (p["bg_port"], p["bg_port"] + 10)   # two servers: steps alternate

    if background:
        for bp in bg_ports if vary else bg_ports[:1]:
            srv.cmd(f"iperf3 -s -D -p {bp}")
        time.sleep(1)
        if not vary:
            rate = max(1, int(p["bw"] * bg_share))
            m["bg_target"] = rate
            info(f"*** Background UDP traffic on {LABEL[n]}: {rate} Mbit/s "
                 f"({bg_share*100:g} % of capacity)\n")
            sta1.cmd(f"iperf3 -c {p['server_ip']} -p {p['bg_port']} -u -b {rate}M "
                     f"-t {int(length) + 8} -J > {outdir}/bg_{n}.json 2>&1 &")
            time.sleep(2)

    if transport == "udp":
        url = f"udp://0.0.0.0:{port}?timeout=5000000&fifo_size=1000000&overrun_nonfatal=1"
        dst = f"udp://{p['server_ip']}:{port}?pkt_size=1316"
    else:
        url = f"tcp://0.0.0.0:{port}?listen=1"
        dst = f"tcp://{p['server_ip']}:{port}"
    info(f"*** Receiver (srv) listening on {LABEL[n]} ({transport.upper()})\n")
    srv.cmd(f"ffmpeg -y -loglevel error -i '{url}' -c copy -f mpegts "
            f"{outdir}/received_{n}.ts > {outdir}/recv_{n}.log 2>&1 &")
    time.sleep(2)

    # delay measured with ping on the same path while the video is sent
    sta1.cmd(f"ping -D -i 0.2 -w {int(length) + 30} {p['server_ip']} "
             f"> {outdir}/ping_{n}.log 2>&1 &")

    def apply_step(i, extra=0):
        f = sched[i]
        cap = p["bw"] * f["cap_f"]
        dly = base_dly * f["dly_f"]
        los = p["loss"] * f["loss_f"]
        shape(net, n, cap, dly, los)
        bg = max(1, int(cap * f["share"])) if background else 0
        if background:
            sta1.cmd(f"iperf3 -c {p['server_ip']} -p {bg_ports[i % 2]} -u -b {bg}M "
                     f"-t {step + extra} -J > {outdir}/bg_{n}_{i:02d}.json 2>&1 &")
        return {"step": i, "t0": i * step, "share": f["share"] if background else 0.0,
                "bg_target": bg, "cap": cap, "delay": dly, "loss": los}

    if vary:
        info(f"*** Conditions change at random every {step}s "
             f"(capacity ±{vary['capacity_pct']:g} %, delay ±{vary['delay_pct']:g} %, "
             f"loss ±{vary['loss_pct']:g} %, background "
             f"{vary['bg_min']*100:g}-{vary['bg_max']*100:g} %"
             f"{'' if background else ' (off)'})\n")
        info(f"    {'time':>9} | {'background':>15} | {'capacity':>9} | {'delay':>8} | "
             f"{'loss':>7} || {'video rx':>10} {'video lost':>10}\n")

    info(f"*** Sender (sta1) streaming the video over {LABEL[n]} only "
         f"({length:.0f}s, real time)\n")
    if vary:
        # warm-up: set the first conditions and start the background 3 s before
        # the video, so switching the shaping does not disturb the measurement
        cur = apply_step(0, extra=3)
        time.sleep(3)
        cur_tx = cur_rx = cur_b = 0
    start = time.time()
    sta1.cmd(f"(ffmpeg -loglevel error -re -i {video} -c copy -f mpegts '{dst}' "
             f"> {outdir}/send_{n}.log 2>&1; date +%s.%N > {outdir}/send_{n}.done) &")
    done = f"{outdir}/send_{n}.done"
    first = last = None
    prev = 0
    shown = 0.0
    sender_end = None

    def close_step(el):
        """Finish the current step: video rate and loss in that step."""
        nonlocal cur_tx, cur_rx, cur_b
        tx = ipt_counter(sta1, "OUTPUT", proto, port) or (0, 0)
        rx = ipt_counter(srv, "INPUT", proto, port) or (0, 0)
        dt = max(el - cur["t0"], 0.1)
        cur["video_mbps"] = (rx[1] - cur_b) * 8 / 1e6 / dt
        stx, srx = tx[0] - cur_tx, rx[0] - cur_rx
        cur["video_tx"], cur["video_rx"] = stx, srx
        # packets still in flight at the step boundary are counted in the next
        # step, so a step can look slightly negative; show 0 in that case
        cur["video_loss"] = (max(0.0, 100.0 * (stx - srx) / stx)
                             if (proto == "udp" and stx) else None)
        cur_tx, cur_rx, cur_b = tx[0], rx[0], rx[1]
        m["steps"].append(cur)
        if stx == 0 and cur["t0"] > 0:
            return                              # video already finished
        bgtxt = (f"{cur['share']*100:3.0f}% = {cur['bg_target']:4d} Mb/s"
                 if background else "off".rjust(15))
        info(f"    {cur['t0']:3d}-{el:4.0f}s | {bgtxt:>15} | {cur['cap']:5.0f} Mb/s | "
             f"{cur['delay']:5.1f} ms | {cur['loss']:5.2f} % || "
             f"{cur['video_mbps']:5.2f} Mb/s {f1(cur['video_loss'], ' %', 2):>10}\n")

    while time.time() < start + length + 120:
        el = time.time() - start
        c = ipt_counter(srv, "INPUT", proto, port)
        if c is None and transport == "udp" and not background:   # no iptables
            c = (snmp(srv, "Udp").get("InDatagrams", 0) - snmp_rx0.get("InDatagrams", 0), 0)
        pk = c[0] if c else 0
        if pk > prev:
            if first is None:
                first = el
            last = el
            prev = pk
        if vary:
            i = int(el // step)
            if i > cur["step"] and i < len(sched):
                close_step(el)
                cur = apply_step(i)
        elif el - shown >= 2:
            shown = el
            info(f"    t={el:5.1f}s  video packets received over {LABEL[n]}: {pk}\n")
        if sender_end is None and os.path.exists(done):
            sender_end = el
        # finished: sender done and nothing new arrived for 3 s
        if sender_end is not None and (last is None or el - last > 3):
            break
        time.sleep(0.5)
    st = float(open(done).read()) - start if os.path.exists(done) else None
    if vary:
        close_step(time.time() - start)
        while len(m["steps"]) > 1 and not m["steps"][-1].get("video_tx"):
            m["steps"].pop()                    # drop steps after the video ended
        for s_, r_ in zip(m["steps"], ping_by_step(f"{outdir}/ping_{n}.log", start,
                                                   step, len(m["steps"]))):
            s_["rtt"] = r_
        shape(net, n, p["bw"], base_dly, p["loss"])       # back to config.ini values

    if background:                          # let iperf finish and write its report
        last_bg = (f"{outdir}/bg_{n}_{m['steps'][-1]['step']:02d}.json" if vary
                   else f"{outdir}/bg_{n}.json")
        for _ in range(30):
            try:
                if '"end"' in open(last_bg).read():
                    break
            except OSError:
                pass
            time.sleep(0.5)
    time.sleep(3 if transport == "udp" else 1)   # let UDP receiver time out

    tx = ipt_counter(sta1, "OUTPUT", proto, port)
    rx = ipt_counter(srv, "INPUT", proto, port)
    snmp_tx1, snmp_rx1 = snmp(sta1, proto.capitalize()), snmp(srv, proto.capitalize())
    if transport == "udp":
        if tx is None and not background:        # fallback without iptables
            tx = (snmp_tx1.get("OutDatagrams", 0) - snmp_tx0.get("OutDatagrams", 0), 0)
            rx = (snmp_rx1.get("InDatagrams", 0) - snmp_rx0.get("InDatagrams", 0), 0)
        m["pk_tx"] = tx[0] if tx else None
        m["pk_rx"] = rx[0] if rx else None
        m["rcvbuf_err"] = snmp_rx1.get("RcvbufErrors", 0) - snmp_rx0.get("RcvbufErrors", 0)
        if m["pk_tx"]:
            m["pk_loss"] = 100.0 * (m["pk_tx"] - m["pk_rx"]) / m["pk_tx"]
    else:
        out = snmp_tx1.get("OutSegs", 0) - snmp_tx0.get("OutSegs", 0)
        ret = snmp_tx1.get("RetransSegs", 0) - snmp_tx0.get("RetransSegs", 0)
        m["pk_tx"], m["retrans"] = out, ret
        m["pk_rx"] = rx[0] if rx else None
        if out:
            m["pk_loss"] = 100.0 * ret / out
    m["bytes_rx"] = rx[1] if rx else None

    m["first"], m["last"], m["send_time"] = first, last, st
    m["rtt"], m["ping_loss"] = ping_stats(f"{outdir}/ping_{n}.log")
    if vary:                              # average RTT only while the video was sent
        rs = [x["rtt"] for x in m["steps"] if x.get("rtt") is not None]
        if rs:
            m["rtt"] = sum(rs) / len(rs)
    if background and not vary:
        m["bg_mbps"], m["bg_loss"] = iperf_bg(f"{outdir}/bg_{n}.json")
    if vary:
        for s_ in m["steps"]:
            s_["bg_mbps"], s_["bg_loss"] = (iperf_bg(f"{outdir}/bg_{n}_{s_['step']:02d}.json")
                                            if background else (None, None))
        st_ = m["steps"]
        avg = lambda k: (sum(x[k] for x in st_ if x.get(k) is not None) /
                         max(1, sum(1 for x in st_ if x.get(k) is not None)))
        m["bg_target"] = avg("bg_target")
        if background:
            m["bg_mbps"], m["bg_loss"] = avg("bg_mbps"), avg("bg_loss")
        with open(f"{outdir}/conditions_{n}.csv", "w") as fh:
            fh.write("step,start_s,background_pct,background_target_mbps,background_achieved_mbps,"
                     "capacity_mbps,delay_ms,loss_pct,measured_rtt_ms,video_mbps,"
                     "video_packets_sent,video_packets_received,video_loss_pct\n")
            v = lambda x: "" if x is None else f"{x:.3f}" if isinstance(x, float) else str(x)
            for x in st_:
                fh.write(",".join(v(x.get(k)) for k in (
                    "step", "t0", "share", "bg_target", "bg_mbps", "cap", "delay", "loss",
                    "rtt", "video_mbps", "video_tx", "video_rx", "video_loss")) + "\n")

    sta1.cmd(f"iptables -w -D OUTPUT -p {proto} --dport {port}")
    srv.cmd(f"iptables -w -D INPUT -p {proto} --dport {port}")
    srv.cmd("pkill -INT ffmpeg; sleep 1; pkill -9 ffmpeg; pkill -9 iperf3")
    sta1.cmd("pkill -9 ffmpeg; pkill -9 iperf3; pkill -9 ping")
    time.sleep(2)
    return m


def run(net, video, transport, background, bg_share, outdir, names, vary=None):
    length = float(sh(f"ffprobe -v error -show_entries format=duration "
                      f"-of csv=p=0 '{video}'").stdout.strip() or 20)
    meas = {}
    for n in names:                      # one network at a time
        meas[n] = stream_one(net, n, video, length, transport,
                             background, bg_share, outdir, vary)
    return meas


def f1(x, unit="", d=1):
    return "-" if x is None else f"{x:.{d}f}{unit}"


def analyse(video, meas, transport, background, outdir, names, vstep=5):
    info("\n*** Comparing received video with the original (this takes a moment)\n")
    sent_frames = count_frames(video)
    sent_mb = os.path.getsize(video) / 1e6
    length = float(sh(f"ffprobe -v error -show_entries format=duration "
                      f"-of csv=p=0 '{video}'").stdout.strip() or 0)
    rows = ["path,transport,video_length_s,video_mb,"
            "transfer_time_s,first_packet_s,sender_time_s,throughput_mbps,"
            "radio_distance_m,radio_signal_dbm,radio_path_loss_db,"
            "configured_loss_pct,packets_sent,packets_received,packet_loss_pct,tcp_retransmissions,"
            "configured_delay_ms,configured_rtt_ms,measured_rtt_ms,measured_one_way_ms,ping_loss_pct,"
            "background_target_mbps,background_achieved_mbps,background_udp_loss_pct,"
            "frames_sent,frames_received,decode_errors,psnr_db,ssim"]
    bar = "=" * 66
    summ = []
    for n in names:
        m = meas.get(n, {})
        p = PATHS[n]
        rx = f"{outdir}/received_{n}.ts"
        ok = os.path.exists(rx) and os.path.getsize(rx) > 0
        fr = count_frames(rx) if ok else 0
        err = decode_errors(rx) if ok else 0
        psnr, ssim = quality(rx, video) if ok else ("", "")
        dly = float(p["delay"].rstrip("ms"))
        first, last, st = m.get("first"), m.get("last"), m.get("send_time")
        xfer = last if last is not None else st
        rx_bytes = m.get("bytes_rx") or (os.path.getsize(rx) if ok else 0)
        thr = rx_bytes * 8 / 1e6 / (xfer - (first or 0)) if xfer and rx_bytes and xfer > (first or 0) else None
        rtt = m.get("rtt")
        pl = m.get("pl")

        info(f"\n{bar}\n  VIDEO RESULTS - {LABEL[n]} ({transport.upper()})\n{bar}\n")
        info(f"  Video sent            : {length:.1f} s of video, {sent_mb:.1f} MB, "
             f"{sent_frames} frames\n")
        info("  -- Time to send --\n")
        info(f"  Transfer time         : {f1(xfer, ' s')}  (start of sending -> last packet received)\n")
        info(f"  First packet arrived  : {f1(first, ' s', 2)} after start\n")
        info(f"  Sender finished after : {f1(st, ' s')}\n")
        info(f"  Throughput            : {f1(thr, ' Mbit/s', 2)}\n")
        info("  -- Path loss (radio signal) --\n")
        if pl:
            info(f"  Distance to AP        : {pl['distance_m']:g} m\n")
            info(f"  Tx power / signal     : {pl['txpower_dbm']:.0f} dBm / {pl['rssi_dbm']:.1f} dBm\n")
            info(f"  Path loss             : {pl['path_loss_db']:.1f} dB ({pl['source']})\n")
        elif n == "5g":
            info("  Path loss             : n/a (5G is an emulated wired link, no radio)\n")
        else:
            info("  Path loss             : - (not measured)\n")
        info("  -- Packet loss --\n")
        info(f"  Configured loss       : {p['loss']:g} %\n")
        if transport == "udp":
            info(f"  Video packets sent    : {m.get('pk_tx', '-')}\n")
            info(f"  Video packets received: {m.get('pk_rx', '-')}\n")
            info(f"  Measured packet loss  : {f1(m.get('pk_loss'), ' %', 2)}\n")
        else:
            info(f"  TCP segments sent     : {m.get('pk_tx', '-')}\n")
            info(f"  Retransmissions       : {m.get('retrans', '-')}  "
                 f"(= {f1(m.get('pk_loss'), ' %', 2)} lost and resent)\n")
        info("  -- Delay --\n")
        info(f"  Configured delay      : {dly:g} ms one-way ({2*dly:g} ms round trip)\n")
        info(f"  Measured RTT (ping)   : {f1(rtt, ' ms', 1)} average during the video\n")
        info(f"  Measured one-way      : {f1(rtt / 2 if rtt else None, ' ms', 1)} (RTT / 2)\n")
        steps = m.get("steps") or []
        if steps:
            rng = lambda k, u, d=1: (f"{min(x[k] for x in steps):.{d}f} - "
                                     f"{max(x[k] for x in steps):.{d}f}{u}")
            info("  -- Changing conditions (random every step) --\n")
            info(f"  Capacity              : {rng('cap', ' Mbit/s', 0)}  (config {p['bw']:g})\n")
            info(f"  Delay one-way         : {rng('delay', ' ms')}  (config {dly:g})\n")
            info(f"  Loss                  : {rng('loss', ' %', 2)}  (config {p['loss']:g})\n")
            if background:
                info(f"  Background share      : {rng('share', '', 2)}  "
                     f"(average {sum(x['share'] for x in steps)/len(steps)*100:.0f} %)\n")
            info(f"    {'time':>8} {'bg %':>5} {'bg target':>10} {'bg real':>8} {'capacity':>9} "
                 f"{'delay':>7} {'loss':>6} {'RTT':>7} {'video':>7} {'lost':>7}\n")
            for x in steps:
                info(f"    {x['t0']:3d}-{x['t0'] + vstep:<3d}s "
                     f"{(x['share']*100 if background else 0):5.0f} "
                     f"{x['bg_target']:7d} Mb {f1(x.get('bg_mbps'), '', 0):>6} Mb "
                     f"{x['cap']:6.0f} Mb {x['delay']:5.1f}ms {x['loss']:5.2f}% "
                     f"{f1(x.get('rtt'), 'ms'):>7} {f1(x.get('video_mbps'), 'Mb', 1):>7} "
                     f"{f1(x.get('video_loss'), '%', 2):>7}\n")
            info(f"    (saved in {outdir}/conditions_{n}.csv)\n")
        info("  -- Background traffic --\n")
        if background:
            info(f"  Target                : {m.get('bg_target', 0):.0f} Mbit/s UDP"
                 f"{' (average, changing)' if steps else ''}\n")
            info(f"  Achieved              : {f1(m.get('bg_mbps'), ' Mbit/s')}"
                 f"  (UDP loss {f1(m.get('bg_loss'), ' %', 2)})\n")
        else:
            info("  Background            : off (add --background to turn it on)\n")
        info("  -- Picture quality --\n")
        if ok:
            info(f"  Frames received       : {fr} / {sent_frames}\n")
            info(f"  Decoder errors        : {err}\n")
            info(f"  PSNR / SSIM           : {psnr} dB / {ssim}\n")
        else:
            info(f"  Nothing received (see {outdir}/recv_{n}.log)\n")

        v = lambda x, d=3: "" if x is None else (f"{x:.{d}f}" if isinstance(x, float) else str(x))
        rows.append(",".join([
            n, transport, f"{length:.1f}", f"{sent_mb:.2f}",
            v(xfer), v(first), v(st), v(thr),
            v(pl["distance_m"] if pl else None), v(pl["rssi_dbm"] if pl else None, 1),
            v(pl["path_loss_db"] if pl else None, 1),
            v(p["loss"]), v(m.get("pk_tx")), v(m.get("pk_rx")), v(m.get("pk_loss")),
            v(m.get("retrans")),
            f"{dly:g}", f"{2*dly:g}", v(rtt), v(rtt / 2 if rtt else None), v(m.get("ping_loss")),
            v(float(m.get("bg_target", 0)) if background else 0.0, 0),
            v(m.get("bg_mbps")), v(m.get("bg_loss")),
            str(sent_frames), str(fr), str(err), psnr, ssim]))
        summ.append((LABEL[n], f1(xfer, " s"),
                     f1(pl["path_loss_db"], " dB") if pl else "n/a",
                     f1(m.get("pk_loss"), " %", 2), f1(rtt, " ms"),
                     (f"{m.get('bg_target', 0):.0f}/{f1(m.get('bg_mbps'))} Mb/s"
                      + (" avg" if m.get("steps") else "")
                      if background else "off"),
                     f"{fr}/{sent_frames}", psnr or "-"))
    with open(f"{outdir}/video_summary.csv", "w") as fh:
        fh.write("\n".join(rows) + "\n")
    hdr = ("network", "time", "path loss", "pkt loss", "RTT", "background", "frames", "PSNR")
    w = (8, 9, 10, 9, 9, 18, 9, 6)
    info(f"\n{bar}\n  SUMMARY ({transport.upper()})\n{bar}\n")
    info("  " + " ".join(h.ljust(x) for h, x in zip(hdr, w)) + "\n")
    for r in summ:
        info("  " + " ".join(str(c).ljust(x) for c, x in zip(r, w)) + "\n")
    info(f"\n  Table saved to {outdir}/video_summary.csv\n")
    info("  PSNR / SSIM: picture quality vs the original (higher = better;\n"
         "               PSNR > 40 dB / SSIM > 0.98 looks identical)\n")

    a, b = f"{outdir}/received_wifi.ts", f"{outdir}/received_5g.ts"
    if len(names) == 2 and all(os.path.exists(x) and os.path.getsize(x) > 0 for x in (a, b)):
        r = sh(f"ffmpeg -y -loglevel error -i {a} -i {b} -filter_complex "
               f"\"[0:v]scale=640:360,setpts=PTS-STARTPTS[l];"
               f"[1:v]scale=640:360,setpts=PTS-STARTPTS[r];[l][r]hstack\" "
               f"-c:v libx264 -preset veryfast -crf 23 -an {outdir}/side_by_side.mp4")
        if r.returncode == 0:
            info(f"*** Side-by-side video (left Wi-Fi, right 5G): "
                 f"{outdir}/side_by_side.mp4\n")


def main():
    ap = argparse.ArgumentParser(description="Send a real video over Wi-Fi and 5G")
    ap.add_argument("--config", default=None, help="config file (default config.ini)")
    ap.add_argument("--video", default=None, help="video file to send (default: [video] file)")
    ap.add_argument("--bitrate", type=float, default=None, help="video bitrate in Mbit/s")
    ap.add_argument("--length", type=int, default=None, help="seconds of video to send")
    ap.add_argument("--transport", choices=["udp", "tcp"], default=None)
    ap.add_argument("--path", choices=["wifi", "5g", "both"], default="both",
                    help="network to test: wifi, 5g, or both one after the other")
    ap.add_argument("--background", action="store_true", help="add iPerf background traffic")
    ap.add_argument("--no-background", action="store_true", help="no background traffic")
    ap.add_argument("--vary", action="store_true",
                    help="change background, capacity, delay and loss at random during the video")
    ap.add_argument("--fixed", action="store_true", help="keep the config.ini values constant")
    ap.add_argument("--out", default="video_results")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is missing:  sudo apt install -y ffmpeg")
    cfg = netconfig.load(args.config)
    topo.apply_config(cfg)
    vc = video_cfg(args.config)
    video_src = args.video if args.video is not None else vc["file"]
    bitrate = args.bitrate or vc["bitrate_mbps"]
    length = args.length or vc["length_s"]
    transport = args.transport or vc["transport"]
    background = (args.background or vc["background"]) and not args.no_background
    vary = None
    if (args.vary or vc["vary"]) and not args.fixed:
        vary = {"step": cfg["bg_step"], "bg_min": cfg["bg_min"], "bg_max": cfg["bg_max"],
                "capacity_pct": vc["capacity_pct"], "delay_pct": vc["delay_pct"],
                "loss_pct": vc["loss_pct"], "seed": cfg["seed"]}
    print(netconfig.summary(cfg) +
          f"  Video: {video_src or 'test pattern'}, {bitrate:g} Mbit/s, {length}s, "
          f"{transport.upper()}, background {'on' if background else 'off'}\n"
          + (f"  Changing conditions: every {vary['step']}s at random - background "
             f"{vary['bg_min']*100:g}-{vary['bg_max']*100:g} %, capacity ±{vary['capacity_pct']:g} %, "
             f"delay ±{vary['delay_pct']:g} %, loss ±{vary['loss_pct']:g} % (seed {vary['seed']})\n"
             if vary else "  Conditions: fixed at the config.ini values\n") +
          f"  Networks tested one at a time: {', '.join(LABEL[n] for n in (NAMES if args.path == 'both' else [args.path]))}\n",
          flush=True)

    names = list(NAMES) if args.path == "both" else [args.path]
    setLogLevel("info")
    outdir = os.path.abspath(args.out)
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)
    video = os.path.join(outdir, "original.mp4")
    prepare_video(video_src, video, bitrate, length)

    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = topo.build_topology()
    try:
        meas = run(net, video, transport, background, cfg["bg_share"], outdir, names, vary)
    finally:
        info("*** Stopping network\n")
        net.stop()
    analyse(video, meas, transport, background, outdir, names, cfg["bg_step"])
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    try:                                   # detailed tables (show_results.py)
        import show_results
        show_results.show(outdir)
    except Exception as e:
        info(f"*** Could not print tables: {e}\n")
    info(f"\n*** All files in {outdir}/\n"
         f"*** Show the tables again any time:  python3 show_results.py\n")


if __name__ == "__main__":
    main()
