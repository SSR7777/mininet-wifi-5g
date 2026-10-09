#!/usr/bin/env python3
"""
Video PLAYERS at the receiver: 1 player, then several players at the same time.

The sender (sta1) streams the live video (MPEG-TS over UDP, real time) and the
receiver (srv) runs N video players. Every player is a real ffmpeg player that
receives the stream, decodes every frame and records what it played
(player_<k>.ts). For every decoded frame we write the exact arrival time, so for
each player we can measure:

  start-up time    time from the start of the stream to the first picture
  frame delay      how late each frame is compared with a reference player
                   on the sender itself (= network + queueing delay)
  jitter           how much the frame delay changes from frame to frame
  lost frames      frames that never arrived
  late frames      frames that came after their play-out time, which the
                   viewer sees as a stall / freeze (play-out buffer --buffer)
  picture quality  PSNR of what the player played vs the original

The test is repeated for 1, 2, 4, 8 ... players (--players) on each network,
so you can see what happens to delay when more viewers share the network.
Conditions are kept constant (config.ini values + background share) so the
only thing that changes between runs is the number of players.

Two ways of watching are tested (--mode live / notlive / both):

  LIVE      the video is sent in real time over UDP while it is "happening"
            (live TV, video call). The player can only show what has arrived.
  NOT LIVE  video on demand (YouTube / Netflix style): each player downloads
            the stored video file over HTTP/TCP as fast as the network allows
            and starts playing once it has a small buffer (--vod-buffer).
            Measured: start-up time, download time, stalls (buffer ran empty),
            stall time, throughput, TCP retransmissions, identical file.

Run:   sudo python3 players_test.py --video BBB.webm
       sudo python3 players_test.py --mode live --players 1,2,4,8 --buffer 200
       sudo python3 players_test.py --path wifi --players 1
       python3 show_results.py --players          # show the tables again
"""
import argparse
import csv
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
LABEL = {"wifi": "Wi-Fi", "5g": "5G", "multi": "Wi-Fi + 5G"}


# ---- "multi" = Wi-Fi and 5G used together (multipath) ----
def ip_path(n):
    """Addresses to use: multipath starts on the Wi-Fi path and adds 5G."""
    return PATHS["wifi" if n == "multi" else n]


def cfg_loss(n):
    if n == "multi":
        return f"{PATHS['wifi']['loss']:g} / {PATHS['5g']['loss']:g}"
    return PATHS[n]["loss"]


def cfg_rtt(n):
    r = lambda k: 2 * float(PATHS[k]["delay"].rstrip("ms"))
    return f"{r('wifi'):g} / {r('5g'):g}" if n == "multi" else r(n)


def bg_start_all(sta1, srv, n, bg_share, sub):
    """Background on the path(s) used: both paths for multipath."""
    for k in (["wifi", "5g"] if n == "multi" else [n]):
        rate = max(1, int(PATHS[k]["bw"] * bg_share))
        start_bg(sta1, srv, PATHS[k], rate, sub, tag="" if n != "multi" else k)
        info(f"*** Background on {LABEL[k]}: {rate} Mbit/s ({bg_share*100:g} % of capacity)\n")


def ping_all(sta1, n, sub, secs):
    for k, f in ([("wifi", "ping.log"), ("5g", "ping2.log")] if n == "multi" else [(n, "ping.log")]):
        sta1.cmd(f"ping -D -i 0.2 -w {secs} {PATHS[k]['server_ip']} > {sub}/{f} 2>&1 &")


def iface_rules(srv, proto, port, add=True):
    """Count packets arriving on each interface of the receiver (share of each path)."""
    for dev in ("srv-wifi", "srv-5g"):
        rule = f"INPUT -i {dev} -p {proto} --sport {port}"
        srv.cmd(f"while iptables -w -D {rule} 2>/dev/null; do :; done")    # no old counters left
        if add:
            srv.cmd(f"iptables -w -I {rule}")


def iface_share(srv, port):
    """(bytes on Wi-Fi, bytes on 5G, % on 5G) from the iface_rules counters."""
    w = g = 0
    for line in srv.cmd("iptables -w -L INPUT -v -x -n 2>/dev/null").splitlines():
        if f"spt:{port}" not in line:
            continue
        f = line.split()
        try:
            b = int(f[1])
        except (ValueError, IndexError):
            continue
        if "srv-wifi" in line:
            w += b
        elif "srv-5g" in line:
            g += b
    return w, g, (100.0 * g / (w + g) if w + g else None)


def finish_share(srv, run, proto, port, sub, n):
    """Read and remove the per-interface counters; 5G RTT for multipath."""
    w, g, pct = iface_share(srv, port)
    iface_rules(srv, proto, port, add=False)
    run["mb_wifi"], run["mb_5g"] = w / 1e6, g / 1e6
    run["share_5g"] = pct
    run["rtt2"] = vs.ping_stats(f"{sub}/ping2.log")[0] if n == "multi" else None
    if n == "multi":
        info(f"    Video bytes over Wi-Fi {w/1e6:.1f} MB, over 5G {g/1e6:.1f} MB "
             f"({vs.f1(pct, ' %', 0)} on 5G)\n")
REF_PORT = 7000
PORT0 = 7001

STAMP = '''import sys, time
out = open(sys.argv[1], "w")
inp = sys.stdin.buffer
while True:
    line = inp.readline()
    if not line:
        break
    out.write("%.6f %s" % (time.time(), line.decode("utf-8", "replace")))
    out.flush()
'''
DL = '''import sys, time, socket, urllib.request
url, out, log = sys.argv[1:4]
if len(sys.argv) > 4 and sys.argv[4] == "mptcp":       # use MPTCP (IPPROTO_MPTCP = 262)
    def _mp(addr, timeout=None, source_address=None, **kw):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 262)
        if isinstance(timeout, (int, float)):
            s.settimeout(timeout)
        s.connect(addr)
        return s
    socket.create_connection = _mp
L = open(log, "w")
L.write("start %.6f\\n" % time.time())
n = 0
try:
    with urllib.request.urlopen(url) as r, open(out, "wb") as f:
        rd = getattr(r, "read1", r.read)
        while True:
            b = rd(65536)
            if not b:
                break
            f.write(b)
            n += len(b)
            L.write("%.6f %d\\n" % (time.time(), n))
except Exception as e:
    sys.stderr.write("download failed: %s\\n" % e)
L.write("done %.6f\\n" % time.time())
L.close()
'''
HTTP_PORT = 8080

HTTPD = '''import sys, socket, functools, http.server
port, folder, proto = int(sys.argv[1]), sys.argv[2], sys.argv[3]
H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=folder)
srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H, bind_and_activate=False)
if proto == "mptcp":                    # MPTCP listening socket (IPPROTO_MPTCP = 262)
    srv.socket.close()
    srv.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 262)
srv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.server_bind()
srv.server_activate()
srv.serve_forever()
'''


def mptcp_ok(net):
    """True if the kernel supports MPTCP (Linux 5.6 or newer with MPTCP built in)."""
    return net.get("sta1").cmd("cat /proc/sys/net/mptcp/enabled 2>/dev/null").strip() == "1"


def mptcp_setup(net):
    """Sender announces its 5G address; receiver accepts a second subflow."""
    sta1, srv = net.get("sta1"), net.get("srv")
    for h in (sta1, srv):
        h.cmd("ip mptcp endpoint flush; ip mptcp limits set subflow 2 add_addr_accepted 2")
    sta1.cmd(f"ip mptcp endpoint add {PATHS['5g']['client_ip']} dev sta1-5g signal")
    return sta1.cmd("ip mptcp endpoint show").strip()


def mp_quic_args(srv, n):
    """picoquic client options: multipath with a 2nd path over the receiver's 5G interface."""
    if n != "multi":
        return ""
    idx = srv.cmd("cat /sys/class/net/srv-5g/ifindex").strip()
    return f"-M -A {PATHS['5g']['server_ip']}/{idx}/{PATHS['5g']['client_ip']} "


def start_bg(sta1, srv, p, rate, sub, tag=""):
    """Background UDP traffic in 5 s pieces until stop_bg (each piece reports)."""
    srv.cmd(f"iperf3 -s -D -p {p['bg_port']}")
    time.sleep(1)
    sta1.cmd(f"(i=0; while [ ! -e {sub}/bg.stop ]; do iperf3 -c {p['server_ip']} -p {p['bg_port']} "
             f"-u -b {rate}M -t 5 -J > {sub}/bg{tag}_$i.json 2>&1; i=$((i+1)); done) &")


def stop_bg(sub):
    """Stop the background; returns the average Mbit/s (summed over both paths for multipath)."""
    open(f"{sub}/bg.stop", "w").close()
    time.sleep(6)                            # let the last piece finish and report
    import glob
    total = None
    for tag in ("", "wifi", "5g"):
        vals = [vs.iperf_bg(f)[0] for f in sorted(glob.glob(f"{sub}/bg{tag}_*.json"))]
        m = mean(vals)
        if m is not None:
            total = (total or 0.0) + m
    return total


FRAME_RE = re.compile(r"^([\d.]+) .*?\bn:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:")
ERR_RE = re.compile(r"error|corrupt|concealing|missing|invalid", re.I)


def player_cmd(port, rec, log, stamp):
    url = f"udp://0.0.0.0:{port}?timeout=5000000&fifo_size=1000000&overrun_nonfatal=1"
    return (f"ffmpeg -hide_banner -nostats -loglevel info -flags low_delay "
            f"-probesize 200000 -analyzeduration 200000 -i '{url}' "
            f"-map 0:v -c copy -f mpegts -y {rec} "
            f"-map 0:v -threads 1 -vf showinfo -f null - 2>&1 | python3 -u {stamp} {log} &")


def read_frames(log):
    """pts -> arrival time (first time each frame was decoded), and error lines."""
    frames, errors = {}, 0
    try:
        for line in open(log, errors="replace"):
            m = FRAME_RE.search(line)
            if m:
                frames.setdefault(int(m.group(2)), float(m.group(1)))
            elif ERR_RE.search(line) and "showinfo" not in line and "demux" not in line:
                # (demuxer "Input/output error" = normal end of the stream)
                errors += 1
    except OSError:
        pass
    return frames, errors


def cpu_times():
    f = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    idle = f[3] + (f[4] if len(f) > 4 else 0)
    return sum(f), idle


def pctl(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))]


def mean(v):
    v = [x for x in v if x is not None]
    return sum(v) / len(v) if v else None


def analyse_player(ref, log, t_start, buffer_ms):
    pl, errors = read_frames(log)
    # the decoder only releases the last few frames when the stream closes,
    # on every player including the reference: leave the last 0.2 s out of
    # the delay / stall figures (they still count as received)
    tail = set(sorted(ref)[-6:])
    allc = sorted(k for k in pl if k in ref)
    common = [k for k in allc if k not in tail]
    exp = len(ref)
    r = {"expected": exp, "received": len(allc), "lost": max(0, exp - len(allc)),
         "errors": errors}
    if not common:
        r.update(startup=None, d_avg=None, d_p95=None, d_max=None, jitter=None,
                 late=0, stalls=0, smooth=0.0)
        return r
    delays = [(pl[k] - ref[k]) * 1000.0 for k in common]
    first = min(pl[k] for k in common)
    r["startup"] = (first - t_start) * 1000.0
    r["d_avg"], r["d_p95"], r["d_max"] = mean(delays), pctl(delays, 95), max(delays)
    r["jitter"] = mean([abs(delays[i] - delays[i - 1]) for i in range(1, len(delays))])
    # play-out: playback starts buffer_ms after the first picture, then one frame
    # every frame interval; a frame that arrives after its slot = stall/freeze
    p0 = common[0]
    start = pl[p0] + buffer_ms / 1000.0
    late, stalls, prev_late = 0, 0, False
    for k in common:
        is_late = pl[k] > start + (k - p0) / 90000.0
        late += is_late
        stalls += is_late and not prev_late
        prev_late = is_late
    r["late"], r["stalls"] = late, stalls
    r["smooth"] = 100.0 * (len(allc) - late) / exp if exp else 0.0
    return r


def run_once(net, n, N, video, length, outdir, background, bg_share, buffer_ms, psnr):
    sta1, srv = net.get("sta1"), net.get("srv")
    p = PATHS[n]
    sub = os.path.join(outdir, f"live_{n}_{N}players")
    shutil.rmtree(sub, ignore_errors=True)
    os.makedirs(sub, exist_ok=True)
    stamp = os.path.join(outdir, "stamp.py")
    ports = [PORT0 + i for i in range(N)]
    info(f"\n========== LIVE - {LABEL[n]}: {N} video player(s) at the receiver ==========\n")

    for port in ports:
        sta1.cmd(f"iptables -w -I OUTPUT -p udp --dport {port}")
        srv.cmd(f"iptables -w -I INPUT -p udp --dport {port}")
    if background:
        rate = max(1, int(p["bw"] * bg_share))
        start_bg(sta1, srv, p, rate, sub)
        info(f"*** Background {rate} Mbit/s ({bg_share*100:g} % of capacity)\n")
    sta1.cmd(f"ping -D -i 0.2 -w {int(length) + 12} {p['server_ip']} > {sub}/ping.log 2>&1 &")

    # reference player on the sender (no network) + N players on the receiver
    sta1.cmd(player_cmd(REF_PORT, f"{sub}/reference.ts", f"{sub}/reference.log", stamp))
    for i, port in enumerate(ports, 1):
        srv.cmd(player_cmd(port, f"{sub}/player_{i}.ts", f"{sub}/player_{i}.log", stamp))
    time.sleep(2.5)

    outs = [f"[f=mpegts:onfail=ignore:flush_packets=1]udp://{p['server_ip']}:{port}?pkt_size=1316" for port in ports]
    outs.append(f"[f=mpegts:onfail=ignore:flush_packets=1]udp://127.0.0.1:{REF_PORT}?pkt_size=1316")
    info(f"*** Sender streaming the live video to {N} player(s) "
         f"({N * vs_bitrate:g} Mbit/s of video) for {length:.0f}s\n")
    c0 = cpu_times()
    t_start = time.time()
    sta1.cmd(f"ffmpeg -loglevel error -re -i {video} -map 0:v -c copy -f tee "
             f"'{'|'.join(outs)}' > {sub}/sender.log 2>&1")
    c1 = cpu_times()
    time.sleep(7)                                     # players stop 5 s after the stream
    bg_mbps = stop_bg(sub) if background else 0.0
    srv.cmd("pkill -INT ffmpeg; sleep 1; pkill -9 ffmpeg; pkill -9 iperf3")
    sta1.cmd("pkill -INT ffmpeg; sleep 1; pkill -9 ffmpeg; pkill -9 iperf3; pkill -9 ping")
    time.sleep(1)

    ref, _ = read_frames(f"{sub}/reference.log")
    run = {"mode": "live", "path": n, "players": N, "cpu": 100.0 * (1 - (c1[1] - c0[1]) / max(1, c1[0] - c0[0])),
           "ref_frames": len(ref), "list": []}
    for i, port in enumerate(ports, 1):
        r = analyse_player(ref, f"{sub}/player_{i}.log", t_start, buffer_ms)
        tx = vs.ipt_counter(sta1, "OUTPUT", "udp", port)
        rx = vs.ipt_counter(srv, "INPUT", "udp", port)
        r["pk_tx"], r["pk_rx"] = (tx[0] if tx else None), (rx[0] if rx else None)
        r["pk_loss"] = (100.0 * (tx[0] - rx[0]) / tx[0]) if tx and rx and tx[0] else None
        r["mbps"] = (rx[1] * 8 / 1e6 / length) if rx else None
        r["psnr"] = ""
        if psnr and os.path.exists(f"{sub}/player_{i}.ts") and os.path.getsize(f"{sub}/player_{i}.ts"):
            r["psnr"] = vs.quality(f"{sub}/player_{i}.ts", video)[0]
        r["player"] = i
        run["list"].append(r)
        sta1.cmd(f"iptables -w -D OUTPUT -p udp --dport {port}")
        srv.cmd(f"iptables -w -D INPUT -p udp --dport {port}")
        info(f"    player {i}: {r['received']}/{r['expected']} frames, start-up "
             f"{vs.f1(r['startup'], ' ms', 0)}, frame delay avg {vs.f1(r['d_avg'], ' ms')}, "
             f"late {r['late']}, PSNR {r['psnr'] or '-'}\n")
    run["rtt"] = vs.ping_stats(f"{sub}/ping.log")[0]
    run["bg_mbps"] = bg_mbps
    if n == "wifi":
        run["pl"] = vs.wifi_path_loss(net)
    info(f"    CPU busy during the stream: {run['cpu']:.0f} %\n")
    return run


def frame_bytes(vod):
    """Bytes of the file needed before each frame (in play order) can be shown."""
    out = vs.sh(f"ffprobe -v error -select_streams v:0 -show_entries packet=pts_time,pos,size "
                f"-of csv=p=0 '{vod}'").stdout.split()
    pk = []
    for line in out:
        f = line.split(",")
        try:
            pk.append((float(f[0]), int(f[2]) + int(f[1])))
        except (ValueError, IndexError):
            pass
    pk.sort()
    need, m = [], 0
    for _, end in pk:
        m = max(m, end)
        need.append(m)
    return need


def analyse_vod(log, need, t0, vod_buffer_s, fps=30.0):
    pts = []
    done = None
    try:
        for line in open(log):
            f = line.split()
            if f[0] == "done":
                done = float(f[1])
            elif f[0] != "start" and len(f) == 2:
                pts.append((float(f[0]), int(f[1])))
    except OSError:
        pass
    r = {"downloaded": pts[-1][1] if pts else 0, "complete": done is not None}
    if not pts:
        r.update(startup=None, dl_s=None, mbps=None, stalls=0, stall_ms=None, smooth=0.0)
        return r

    def avail(b):                       # time when b bytes had arrived
        for t, n in pts:
            if n >= b:
                return t
        return None
    nb = min(len(need), max(1, int(vod_buffer_s * fps)))
    first = avail(need[nb - 1])
    if first is None:
        r.update(startup=None, dl_s=None, mbps=None, stalls=0, stall_ms=None, smooth=0.0)
        return r
    r["startup"] = (first - t0) * 1000.0
    r["dl_s"] = (pts[-1][0] - t0)
    r["mbps"] = pts[-1][1] * 8 / 1e6 / max(r["dl_s"], 1e-6)
    # play-out: one frame every 1/fps; if the next frame is not downloaded yet
    # the picture freezes (stall) until it is
    clock, stalls, stall_t, prev = first, 0, 0.0, False
    for k in range(len(need)):
        a = avail(need[k])
        if a is None:
            break
        if a > clock:
            stall_t += a - clock
            stalls += not prev
            clock, prev = a, True
        else:
            prev = False
        clock += 1.0 / fps
    r["stalls"], r["stall_ms"] = stalls, stall_t * 1000.0
    length = len(need) / fps
    r["smooth"] = 100.0 * length / (length + stall_t)
    return r


def run_vod(net, n, N, vod, need, outdir, background, bg_share, vod_buffer_s):
    """NOT LIVE: N players download the stored video over HTTP/TCP and play it
    (over both paths with MPTCP when n == "multi")."""
    sta1, srv = net.get("sta1"), net.get("srv")
    p = ip_path(n)
    sub = os.path.join(outdir, f"notlive_{n}_{N}players")
    shutil.rmtree(sub, ignore_errors=True)
    os.makedirs(sub, exist_ok=True)
    proto_name = "MPTCP" if n == "multi" else "HTTP/TCP"
    info(f"\n========== NOT LIVE (video on demand, {proto_name}) - {LABEL[n]}: {N} player(s) ==========\n")
    if background:
        bg_start_all(sta1, srv, n, bg_share, sub)
    ping_all(sta1, n, sub, 60)
    iface_rules(srv, "tcp", HTTP_PORT)
    time.sleep(2)
    tcp0 = vs.snmp(sta1, "Tcp")
    url = f"http://{p['client_ip']}:{HTTP_PORT}/{os.path.basename(vod)}"
    info(f"*** {N} player(s) download {os.path.getsize(vod)/1e6:.1f} MB each over {proto_name} "
         f"and start playing after {vod_buffer_s:g} s of video is buffered\n")
    c0 = cpu_times()
    t0 = time.time()
    for i in range(1, N + 1):
        srv.cmd(f"python3 {outdir}/dl.py {url} {sub}/player_{i}.mp4 {sub}/player_{i}.log "
                f"{'mptcp' if n == 'multi' else 'tcp'} &")
    while time.time() - t0 < 180:
        if all("done" in open(f"{sub}/player_{i}.log").read()
               for i in range(1, N + 1) if os.path.exists(f"{sub}/player_{i}.log")) and \
                all(os.path.exists(f"{sub}/player_{i}.log") for i in range(1, N + 1)):
            break
        time.sleep(0.5)
    c1 = cpu_times()
    tcp1 = vs.snmp(sta1, "Tcp")
    bg_mbps = stop_bg(sub) if background else 0.0
    srv.cmd(f"pkill -9 -f '{outdir}/dl.py'; pkill -9 iperf3")
    sta1.cmd("pkill -9 iperf3; pkill -9 ping")
    want = vs.sh(f"md5sum '{vod}'").stdout.split()[:1]
    run = {"mode": "notlive", "path": n, "players": N, "list": [],
           "cpu": 100.0 * (1 - (c1[1] - c0[1]) / max(1, c1[0] - c0[0]))}
    finish_share(srv, run, "tcp", HTTP_PORT, sub, n)
    out_seg = tcp1.get("OutSegs", 0) - tcp0.get("OutSegs", 0)
    ret = tcp1.get("RetransSegs", 0) - tcp0.get("RetransSegs", 0)
    run["tcp_retrans_pct"] = 100.0 * ret / out_seg if out_seg > 0 else None
    for i in range(1, N + 1):
        r = analyse_vod(f"{sub}/player_{i}.log", need, t0, vod_buffer_s)
        got = vs.sh(f"md5sum '{sub}/player_{i}.mp4'").stdout.split()[:1]
        r["identical"] = bool(want) and got == want
        r["player"] = i
        run["list"].append(r)
        info(f"    player {i}: start-up {vs.f1(r['startup'], ' ms', 0)}, downloaded in "
             f"{vs.f1(r['dl_s'], ' s', 2)} ({vs.f1(r['mbps'], ' Mbit/s', 1)}), stalls {r['stalls']}, "
             f"{'identical' if r['identical'] else 'NOT identical'}\n")
    run["rtt"] = vs.ping_stats(f"{sub}/ping.log")[0]
    run["bg_mbps"] = bg_mbps
    if n in ("wifi", "multi"):
        run["pl"] = vs.wifi_path_loss(net)
    info(f"    TCP retransmissions {vs.f1(run['tcp_retrans_pct'], ' %', 2)}, "
         f"CPU busy {run['cpu']:.0f} %\n")
    return run


# ---------------------------------------------------------------------------
#  QUIC (picoquic) players
# ---------------------------------------------------------------------------
Q_LIVE_PORT = 4460
Q_VOD_PORT = 4461


def start_pq_server(sta1, pq, port, sub, www=None, extra="", env=""):
    """picoquic server on the sender; returns its process id."""
    w = f"-w {www} " if www else ""
    out = sta1.cmd(f"{env}{pq}/picoquicdemo -0 {extra}-p {port} -c {pq}/certs/cert.pem -k {pq}/certs/key.pem "
                   f"{w}-F {sub}/perf_server.csv > {sub}/server.log 2>&1 & echo $!").split()
    time.sleep(1)
    return out[-1] if out and out[-1].isdigit() else None


def stop_pq_server(sta1, pid):
    if pid:
        sta1.cmd(f"kill -INT {pid}")          # the server writes its report when it stops
        time.sleep(2)
        sta1.cmd(f"kill -9 {pid} 2>/dev/null")


def server_retrans(sub):
    import quic_test as qt
    rows = qt.read_perf(f"{sub}/perf_server.csv")
    sent = sum(int(r.get("pkt_sent", 0) or 0) for r in rows)
    ret = sum(int(r.get("retrans.", 0) or 0) for r in rows)
    return (100.0 * ret / sent if sent else None), sent, ret


def run_quic_live(net, n, N, qmode, pq, outdir, length, background, bg_share, buffer_ms):
    """LIVE over QUIC: N picoquic players on the receiver each get their own
    4 Mbit/s, 30 fps live stream (QUIC streams = reliable, datagrams = unreliable)."""
    import quic_live as ql
    sta1, srv = net.get("sta1"), net.get("srv")
    p = ip_path(n)
    secs = int(round(length))
    sub = os.path.join(outdir, f"quic{qmode}_{n}_{N}players")
    shutil.rmtree(sub, ignore_errors=True)
    os.makedirs(sub, exist_ok=True)
    label = "QUIC streams (reliable)" if qmode == "stream" else "QUIC datagrams (unreliable)"
    if n == "multi":
        label = "multipath " + label
    info(f"\n========== LIVE over {label} - {LABEL[n]}: {N} player(s) ==========\n")
    mp = "-M " if n == "multi" else ""
    pid = start_pq_server(sta1, pq, Q_LIVE_PORT, sub, extra=mp)
    iface_rules(srv, "udp", Q_LIVE_PORT)          # per interface (added first: the total rule stays on top)
    sta1.cmd(f"iptables -w -I OUTPUT -p udp --sport {Q_LIVE_PORT}")
    srv.cmd(f"iptables -w -I INPUT -p udp --sport {Q_LIVE_PORT}")
    if background:
        bg_start_all(sta1, srv, n, bg_share, sub)
    ping_all(sta1, n, sub, secs + 20)
    time.sleep(2)
    scen = ql.scenario(qmode, secs)
    c0 = cpu_times()
    t0 = time.time()
    for i in range(1, N + 1):
        srv.cmd(f"(QUICPERF_REPORT={sub}/frames_{i}.csv timeout {secs + 40} {pq}/picoquicdemo -0 "
                f"{mp_quic_args(srv, n)}-a perf -n test {p['client_ip']} {Q_LIVE_PORT} '{scen}' > {sub}/client_{i}.log 2>&1; "
                f"touch {sub}/done_{i}) &")
    info(f"*** {N} picoquic player(s) receiving a live 4 Mbit/s, 30 fps stream for {secs}s\n")
    while time.time() - t0 < secs + 45:
        if all(os.path.exists(f"{sub}/done_{i}") for i in range(1, N + 1)):
            break
        time.sleep(0.5)
    c1 = cpu_times()
    bg_mbps = stop_bg(sub) if background else 0.0
    stop_pq_server(sta1, pid)
    srv.cmd("pkill -9 iperf3")
    sta1.cmd("pkill -9 iperf3; pkill -9 ping")
    tx = qt_counter(sta1, "OUTPUT", Q_LIVE_PORT)
    rx = qt_counter(srv, "INPUT", Q_LIVE_PORT)
    sta1.cmd(f"iptables -w -D OUTPUT -p udp --sport {Q_LIVE_PORT}")
    srv.cmd(f"iptables -w -D INPUT -p udp --sport {Q_LIVE_PORT}")
    exp = ql.expected_frames(qmode, secs)
    run = {"mode": f"quic{qmode}", "path": n, "players": N, "list": [],
           "cpu": 100.0 * (1 - (c1[1] - c0[1]) / max(1, c1[0] - c0[0]))}
    finish_share(srv, run, "udp", Q_LIVE_PORT, sub, n)
    for i in range(1, N + 1):
        fr = sorted(ql.read_frames(f"{sub}/frames_{i}.csv"))
        d = [(r - s) / 1000.0 for s, r in fr]
        r = {"player": i, "expected": exp, "received": len(fr), "lost": max(0, exp - len(fr))}
        if fr:
            ds = sorted(d)
            r.update(startup=(min(x[1] for x in fr) - min(x[0] for x in fr)) / 1000.0,
                     d_avg=mean(d), d_p95=pctl(ds, 95), d_max=ds[-1], d_min=ds[0],
                     jitter=mean([abs(d[k] - d[k - 1]) for k in range(1, len(d))]))
            r["late"] = sum(1 for x in d if x > buffer_ms)
            r["smooth"] = 100.0 * (len(fr) - r["late"]) / exp if exp else 0.0
        else:
            r.update(startup=None, d_avg=None, d_p95=None, d_max=None, d_min=None, jitter=None,
                     late=0, smooth=0.0)
        run["list"].append(r)
        info(f"    player {i}: {r['received']}/{exp} {'frames' if qmode == 'stream' else 'pieces'}, "
             f"delay avg {vs.f1(r['d_avg'], ' ms')}, late {r['late']}\n")
    run["retrans_pct"], run["pkt_sent"], run["retrans"] = server_retrans(sub)
    run["pk_loss"] = 100.0 * (tx[0] - rx[0]) / tx[0] if tx[0] else None
    run["rtt"] = vs.ping_stats(f"{sub}/ping.log")[0]
    run["bg_mbps"] = bg_mbps
    info(f"    QUIC resent {vs.f1(run['retrans_pct'], ' %', 2)} of packets, "
         f"network loss {vs.f1(run['pk_loss'], ' %', 2)}, CPU {run['cpu']:.0f} %\n")
    return run


# ---------------------------------------------------------------------------
#  LIVE REAL VIDEO over picoquic (Wi-Fi, 5G, Wi-Fi + 5G)
# ---------------------------------------------------------------------------
Q_VIDEO_PORT = 4462
DG_SLOT = 1000          # video bytes per QUIC datagram
DG_MAX_RATE = 250       # datagrams per second per datagram stream (quicperf limit 255)


def video_scenario(qmode, size, secs, fps=30):
    """quicperf scenario that carries `size` bytes of video in `secs` seconds (live pace)."""
    import math
    if qmode == "stream":
        n = max(1, int(round(fps * secs)))
        return f"=video:s{fps}:n{n}:{math.ceil(size / n) + 8};", 1, n
    pieces = math.ceil(size / DG_SLOT)
    ns = max(1, math.ceil(pieces / secs / DG_MAX_RATE))
    per = math.ceil(pieces / ns)
    freq = math.ceil(per / secs)
    sc = "".join(f"=dg{chr(65 + k)}:d{freq}:n{per}:{DG_SLOT + 24};" for k in range(ns))
    return sc, ns, pieces


def live_cut(out_ts, log, size, buffer_ms, live_ts):
    """What a live player can use: only the bytes that arrived within the buffer.
    Returns (pieces, delays_ms, bytes_received, bytes_on_time, start-up ms)."""
    rows = []
    try:
        with open(log) as fh:
            for line in fh:
                f = line.strip().split(",")
                if len(f) == 4 and all(x.isdigit() for x in f):
                    rows.append(tuple(int(x) for x in f))
    except OSError:
        pass
    data = bytearray(size)
    try:
        with open(out_ts, "rb") as fh:
            got = fh.read(size)
    except OSError:
        got = b""
    live = bytearray(size)
    rec = ontime = 0
    seen = set()
    delays = []
    for off, ln, sent, arr in rows:
        if off >= size:
            continue
        ln = min(ln, size - off)
        if (off, ln) in seen:
            continue
        seen.add((off, ln))
        d = (arr - sent) / 1000.0
        delays.append(d)
        rec += ln
        if d <= buffer_ms:
            live[off:off + ln] = got[off:off + ln]
            ontime += ln
    with open(live_ts, "wb") as fh:
        fh.write(live)
    startup = (min(r[3] for r in rows) - min(r[2] for r in rows)) / 1000.0 if rows else None
    return len(rows), delays, rec, ontime, startup


def watchable(live_ts, mp4):
    """Decode what the live player got into a normal mp4 (a lost frame = the picture freezes)."""
    vs.sh(f"ffmpeg -y -loglevel error -err_detect ignore_err -fflags +genpts+discardcorrupt "
          f"-i '{live_ts}' -an -vsync cfr -r 30 -c:v libx264 -preset veryfast -crf 23 "
          f"-pix_fmt yuv420p '{mp4}'")


def run_quic_video(net, n, qmode, pq, outdir, ts, original, background, bg_share, buffer_ms):
    """Send the REAL video live from sta1 to the player on srv over picoquic
    (QUIC streams or QUIC datagrams; multipath QUIC for Wi-Fi + 5G)."""
    sta1, srv = net.get("sta1"), net.get("srv")
    p = ip_path(n)
    size = os.path.getsize(ts)
    secs = float(vs.sh(f"ffprobe -v error -show_entries format=duration -of csv=p=0 '{ts}'").stdout.strip() or 0)
    scen, ns, units = video_scenario(qmode, size, secs)
    sub = os.path.join(outdir, f"livevideo_{qmode}_{n}")
    shutil.rmtree(sub, ignore_errors=True)
    os.makedirs(sub, exist_ok=True)
    how = ("QUIC streams (reliable)" if qmode == "stream" else "QUIC datagrams (unreliable)")
    if n == "multi":
        how = "multipath " + how
    info(f"\n========== LIVE REAL VIDEO over {how} - {LABEL[n]} ==========\n")
    env = f"QUICPERF_VIDEO_IN={ts} QUICPERF_VIDEO_NS={ns} QUICPERF_VIDEO_SLOT={DG_SLOT} "
    pid = start_pq_server(sta1, pq, Q_VIDEO_PORT, sub, extra="-M " if n == "multi" else "", env=env)
    iface_rules(srv, "udp", Q_VIDEO_PORT)
    sta1.cmd(f"iptables -w -I OUTPUT -p udp --sport {Q_VIDEO_PORT}")
    srv.cmd(f"iptables -w -I INPUT -p udp --sport {Q_VIDEO_PORT}")
    if background:
        bg_start_all(sta1, srv, n, bg_share, sub)
    ping_all(sta1, n, sub, int(secs) + 20)
    time.sleep(2)
    info(f"*** Sending {size/1e6:.1f} MB of real video live ({secs:g} s, "
         f"{size*8/secs/1e6:.1f} Mbit/s) to the player on the receiver\n")
    c0 = cpu_times()
    t0 = time.time()
    srv.cmd(f"(QUICPERF_VIDEO_OUT={sub}/received_all.ts QUICPERF_VIDEO_LOG={sub}/pieces.csv "
            f"QUICPERF_VIDEO_NS={ns} QUICPERF_VIDEO_SLOT={DG_SLOT} QUICPERF_REPORT={sub}/frames.csv "
            f"timeout {int(secs) + 40} {pq}/picoquicdemo -0 {mp_quic_args(srv, n)}-a perf -n test "
            f"{p['client_ip']} {Q_VIDEO_PORT} '{scen}' > {sub}/client.log 2>&1; touch {sub}/done) &")
    while time.time() - t0 < secs + 45 and not os.path.exists(f"{sub}/done"):
        time.sleep(0.5)
    c1 = cpu_times()
    bg_mbps = stop_bg(sub) if background else 0.0
    stop_pq_server(sta1, pid)
    srv.cmd("pkill -9 iperf3")
    sta1.cmd("pkill -9 iperf3; pkill -9 ping")
    tx = qt_counter(sta1, "OUTPUT", Q_VIDEO_PORT)
    rx = qt_counter(srv, "INPUT", Q_VIDEO_PORT)
    sta1.cmd(f"iptables -w -D OUTPUT -p udp --sport {Q_VIDEO_PORT}")
    srv.cmd(f"iptables -w -D INPUT -p udp --sport {Q_VIDEO_PORT}")
    run = {"mode": qmode, "path": n, "players": 1, "list": [], "video_mb": size / 1e6,
           "cpu": 100.0 * (1 - (c1[1] - c0[1]) / max(1, c1[0] - c0[0]))}
    finish_share(srv, run, "udp", Q_VIDEO_PORT, sub, n)

    pieces, d, rec, ontime, startup = live_cut(f"{sub}/received_all.ts", f"{sub}/pieces.csv", size,
                                               buffer_ms, f"{sub}/live.ts")
    ds = sorted(d)
    run.update(startup=startup, d_avg=mean(d), d_p95=pctl(ds, 95) if ds else None,
               d_max=ds[-1] if ds else None,
               jitter=mean([abs(d[k] - d[k - 1]) for k in range(1, len(d))]) if len(d) > 1 else None,
               recv_pct=100.0 * rec / size, ontime_pct=100.0 * ontime / size)
    # what the viewer sees
    watchable(f"{sub}/live.ts", f"{sub}/live.mp4")
    total = vs.count_frames(original)
    shown = vs.count_frames(f"{sub}/live.ts")
    run["frames_total"], run["frames_shown"] = total, min(shown, total) if total else shown
    run["frames_shown_pct"] = 100.0 * run["frames_shown"] / total if total else None
    run["psnr"], run["ssim"] = vs.quality(f"{sub}/live.ts", original)
    run["errors"] = vs.decode_errors(f"{sub}/live.ts")
    run["retrans_pct"], run["pkt_sent"], run["retrans"] = server_retrans(sub)
    run["pk_loss"] = 100.0 * max(0, tx[0] - rx[0]) / tx[0] if tx[0] else None
    run["rtt"] = vs.ping_stats(f"{sub}/ping.log")[0]
    run["bg_mbps"] = bg_mbps
    info(f"    video bytes received {run['recv_pct']:.2f} %, in time for the player {run['ontime_pct']:.2f} %, "
         f"delay avg {vs.f1(run['d_avg'], ' ms')}\n"
         f"    frames shown {run['frames_shown']}/{total}, PSNR {run['psnr'] or '-'} dB, "
         f"QUIC resent {vs.f1(run['retrans_pct'], ' %', 2)}  ->  watch {sub}/live.mp4\n")
    return run


def compare_video(outdir, qmode, nets, original):
    """Original + every network side by side (2 x 2), with labels -> one video to watch."""
    tiles = [("Original (sent)", original)] + [(LABEL[n], f"{outdir}/livevideo_{qmode}_{n}/live.mp4")
                                               for n in nets]
    tiles = [(t, f) for t, f in tiles if os.path.exists(f) and os.path.getsize(f) > 0]
    if len(tiles) < 2:
        return None
    out = f"{outdir}/livevideo_{qmode}_compare.mp4"
    ins = " ".join(f"-i '{f}'" for _, f in tiles)
    while len(tiles) < 4:
        tiles.append(("", None))
    font = vs.sh("fc-match -f '%{file}' sans 2>/dev/null").stdout.strip()
    for use_text in (True, False):
        fc = ""
        k = 0
        for i, (t, f) in enumerate(tiles):
            if f is None:
                fc += f"color=black:s=640x360:r=30[v{i}];"
                continue
            txt = ""
            if use_text and font:
                tt = t.replace(":", "\\\\:")
                txt = (f",drawtext=fontfile='{font}':text='{tt}':x=10:y=10:fontsize=28:"
                       f"fontcolor=white:box=1:boxcolor=black@0.6")
            fc += f"[{k}:v]setpts=PTS-STARTPTS,fps=30,scale=640:360{txt}[v{i}];"
            k += 1
        fc += "[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0:shortest=1"
        r = vs.sh(f"ffmpeg -y -loglevel error {ins} -filter_complex \"{fc}\" -an -c:v libx264 "
                  f"-preset veryfast -crf 26 -pix_fmt yuv420p '{out}'")
        if r.returncode == 0:
            return out
    return None


def save_livevideo(runs, outdir, buffer_ms):
    f = lambda x, d=2: "" if x is None or x == "" else (f"{x:.{d}f}" if isinstance(x, float) else str(x))
    with open(f"{outdir}/players_livevideo_summary.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "quic_mode", "video_mb", "startup_ms", "delay_avg_ms", "delay_p95_ms",
                    "delay_max_ms", "jitter_ms", "bytes_received_pct", "bytes_on_time_pct",
                    "frames_shown", "frames_total", "frames_shown_pct", "psnr_db", "ssim",
                    "decoder_errors", "quic_retrans_pct", "packet_loss_pct", "ping_rtt_ms",
                    "background_mbps", "configured_loss_pct", "configured_rtt_ms", "buffer_ms",
                    "cpu_busy_pct", "mb_over_wifi", "mb_over_5g", "share_5g_pct", "ping_rtt_5g_ms"])
        for r in runs:
            w.writerow([r["path"], r["mode"], f(r["video_mb"], 2), f(r["startup"], 0), f(r["d_avg"], 1),
                        f(r["d_p95"], 1), f(r["d_max"], 1), f(r["jitter"], 1), f(r["recv_pct"], 2),
                        f(r["ontime_pct"], 2), r["frames_shown"], r["frames_total"],
                        f(r["frames_shown_pct"], 1), r["psnr"], r["ssim"], r["errors"],
                        f(r["retrans_pct"], 2), f(r["pk_loss"], 2), f(r["rtt"], 1), f(r["bg_mbps"], 1),
                        cfg_loss(r["path"]), cfg_rtt(r["path"]), f(float(buffer_ms), 0), f(r["cpu"], 0)]
                       + share_cols(r, f))


def qt_counter(node, chain, port):
    import quic_test as qt
    return qt.ipt_sport(node, chain, port)


def run_quic_vod(net, n, N, vod, need, pq, outdir, background, bg_share, vod_buffer_s):
    """NOT LIVE over QUIC: N picoquic players download the video over HTTP/3 and play it."""
    sta1, srv = net.get("sta1"), net.get("srv")
    p = ip_path(n)
    sub = os.path.join(outdir, f"quicnotlive_{n}_{N}players")
    shutil.rmtree(sub, ignore_errors=True)
    os.makedirs(sub, exist_ok=True)
    vname = os.path.basename(vod)
    mpq = "multipath QUIC" if n == "multi" else "QUIC"
    info(f"\n========== NOT LIVE over {mpq} (HTTP/3) - {LABEL[n]}: {N} player(s) ==========\n")
    pid = start_pq_server(sta1, pq, Q_VOD_PORT, sub, www=os.path.dirname(vod),
                          extra="-M " if n == "multi" else "")
    iface_rules(srv, "udp", Q_VOD_PORT)
    if background:
        bg_start_all(sta1, srv, n, bg_share, sub)
    ping_all(sta1, n, sub, 60)
    time.sleep(2)
    files = []
    for i in range(1, N + 1):
        os.makedirs(f"{sub}/p{i}", exist_ok=True)
        files.append(f"{sub}/p{i}/{vname}")
    c0 = cpu_times()
    t0 = time.time()
    for i in range(1, N + 1):
        srv.cmd(f"(cd {sub}/p{i} && timeout 120 {pq}/picoquicdemo -0 {mp_quic_args(srv, n)}-o {sub}/p{i} -n test "
                f"{p['client_ip']} {Q_VOD_PORT} '/{vname}' > {sub}/client_{i}.log 2>&1; "
                f"touch {sub}/done_{i}) &")
    info(f"*** {N} picoquic player(s) download {os.path.getsize(vod)/1e6:.1f} MB each over HTTP/3 ({mpq})\n")
    prog = [[] for _ in range(N)]
    last = [-1] * N
    while time.time() - t0 < 150:
        now = time.time()
        for k, f in enumerate(files):
            try:
                sz = os.path.getsize(f)
            except OSError:
                continue
            if sz != last[k]:
                prog[k].append((now, sz))
                last[k] = sz
        if all(os.path.exists(f"{sub}/done_{i}") for i in range(1, N + 1)):
            break
        time.sleep(0.05)
    c1 = cpu_times()
    bg_mbps = stop_bg(sub) if background else 0.0
    stop_pq_server(sta1, pid)
    srv.cmd("pkill -9 iperf3")
    sta1.cmd("pkill -9 iperf3; pkill -9 ping")
    want = vs.sh(f"md5sum '{vod}'").stdout.split()[:1]
    run = {"mode": "quicnotlive", "path": n, "players": N, "list": [],
           "cpu": 100.0 * (1 - (c1[1] - c0[1]) / max(1, c1[0] - c0[0]))}
    finish_share(srv, run, "udp", Q_VOD_PORT, sub, n)
    for k in range(N):
        with open(f"{sub}/player_{k + 1}.log", "w") as fh:     # same format as the TCP player
            fh.write(f"start {t0:.6f}\n")
            for t, b in prog[k]:
                if b > 0:
                    fh.write(f"{t:.6f} {b}\n")
            fh.write(f"done {time.time():.6f}\n")
        r = analyse_vod(f"{sub}/player_{k + 1}.log", need, t0, vod_buffer_s)
        got = vs.sh(f"md5sum '{files[k]}'").stdout.split()[:1]
        r["identical"] = bool(want) and got == want
        r["player"] = k + 1
        run["list"].append(r)
        info(f"    player {k + 1}: start-up {vs.f1(r['startup'], ' ms', 0)}, downloaded in "
             f"{vs.f1(r['dl_s'], ' s', 2)} ({vs.f1(r['mbps'], ' Mbit/s', 1)}), stalls {r['stalls']}, "
             f"{'identical' if r['identical'] else 'NOT identical'}\n")
    run["tcp_retrans_pct"], run["pkt_sent"], run["retrans"] = server_retrans(sub)   # QUIC resends
    run["rtt"] = vs.ping_stats(f"{sub}/ping.log")[0]
    run["bg_mbps"] = bg_mbps
    if n in ("wifi", "multi"):
        run["pl"] = vs.wifi_path_loss(net)
    info(f"    QUIC resent {vs.f1(run['tcp_retrans_pct'], ' %', 2)} of packets, CPU {run['cpu']:.0f} %\n")
    return run


def save_quic_live(runs, outdir, buffer_ms):
    f = lambda x, d=2: "" if x is None or x == "" else (f"{x:.{d}f}" if isinstance(x, float) else str(x))
    with open(f"{outdir}/players_quic_live_summary.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "quic_mode", "players", "units_expected_per_player", "startup_ms",
                    "delay_min_ms", "delay_avg_ms", "delay_p95_ms", "delay_max_ms", "jitter_ms",
                    "lost_pct", "late_pct", "smooth_playback_pct", "smooth_worst_player_pct",
                    "quic_retrans_pct", "packet_loss_pct", "ping_rtt_ms", "background_mbps",
                    "configured_loss_pct", "configured_rtt_ms", "buffer_ms", "cpu_busy_pct",
                    "mb_over_wifi", "mb_over_5g", "share_5g_pct", "ping_rtt_5g_ms"])
        for r in runs:
            L = r["list"]
            exp = L[0]["expected"] if L else 0
            w.writerow([r["path"], r["mode"].replace("quic", ""), r["players"], exp,
                        f(mean([x["startup"] for x in L]), 0), f(mean([x.get("d_min") for x in L]), 1),
                        f(mean([x["d_avg"] for x in L]), 1), f(max([x["d_p95"] or 0 for x in L]), 1),
                        f(max([x["d_max"] or 0 for x in L]), 1), f(mean([x["jitter"] for x in L]), 1),
                        f(100.0 * mean([x["lost"] for x in L]) / exp if exp else None, 2),
                        f(100.0 * mean([x["late"] for x in L]) / exp if exp else None, 2),
                        f(mean([x["smooth"] for x in L]), 1), f(min([x["smooth"] for x in L]), 1),
                        f(r["retrans_pct"], 2), f(r["pk_loss"], 2), f(r["rtt"], 1), f(r["bg_mbps"], 1),
                        cfg_loss(r["path"]), cfg_rtt(r["path"]),
                        f(float(buffer_ms), 0), f(r["cpu"], 0)] + share_cols(r, f))
    with open(f"{outdir}/players_quic_live_detail.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "quic_mode", "players", "player", "received", "expected", "lost",
                    "startup_ms", "delay_avg_ms", "delay_p95_ms", "delay_max_ms", "jitter_ms",
                    "late", "smooth_playback_pct"])
        for r in runs:
            for x in r["list"]:
                w.writerow([r["path"], r["mode"].replace("quic", ""), r["players"], x["player"],
                            x["received"], x["expected"], x["lost"], f(x["startup"], 0), f(x["d_avg"], 1),
                            f(x["d_p95"], 1), f(x["d_max"], 1), f(x["jitter"], 1), x["late"],
                            f(x["smooth"], 1)])


def share_cols(r, f):
    return [f(r.get("mb_wifi"), 2), f(r.get("mb_5g"), 2), f(r.get("share_5g"), 1), f(r.get("rtt2"), 1)]


def grid_video(sub, N):
    """Put up to 4 players side by side so you can watch them together."""
    k = min(N, 4)
    if k < 2:
        return
    ins = " ".join(f"-i {sub}/player_{i}.ts" for i in range(1, k + 1))
    sc = "".join(f"[{i}:v]scale=640:360,setpts=PTS-STARTPTS[v{i}];" for i in range(k))
    if k == 2:
        lay = "[v0][v1]hstack=inputs=2"
    else:
        sc += "".join(f"color=black:s=640x360:d=30[v{i}];" for i in range(k, 4))
        lay = "[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0:shortest=1"
    vs.sh(f"ffmpeg -y -loglevel error {ins} -filter_complex \"{sc}{lay}\" "
          f"-c:v libx264 -preset veryfast -crf 26 -an {sub}/players_grid.mp4")


def save_live(runs, outdir, buffer_ms, bitrate):
    f = lambda x, d=2: "" if x is None or x == "" else (f"{x:.{d}f}" if isinstance(x, float) else str(x))
    with open(f"{outdir}/players_summary.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "players", "video_load_mbps", "received_mbps_total", "startup_ms",
                    "frame_delay_avg_ms", "frame_delay_p95_ms", "frame_delay_max_ms", "jitter_ms",
                    "frames_expected_per_player", "frames_lost_pct", "frames_late_pct",
                    "stalls_per_player", "smooth_playback_pct", "smooth_worst_player_pct",
                    "psnr_avg_db", "psnr_worst_db", "decoder_errors_per_player", "packet_loss_pct",
                    "ping_rtt_ms", "background_mbps", "radio_path_loss_db", "configured_loss_pct",
                    "configured_rtt_ms", "buffer_ms", "cpu_busy_pct"])
        for r in runs:
            L = r["list"]
            exp = L[0]["expected"] if L else 0
            ps = [float(x["psnr"]) for x in L if x["psnr"] not in ("", "inf")]
            w.writerow([r["path"], r["players"], f(r["players"] * bitrate, 1),
                        f(sum(x["mbps"] or 0 for x in L), 1), f(mean([x["startup"] for x in L]), 0),
                        f(mean([x["d_avg"] for x in L]), 1), f(max([x["d_p95"] or 0 for x in L]), 1),
                        f(max([x["d_max"] or 0 for x in L]), 1), f(mean([x["jitter"] for x in L]), 1),
                        exp, f(100.0 * mean([x["lost"] for x in L]) / exp if exp else None, 2),
                        f(100.0 * mean([x["late"] for x in L]) / exp if exp else None, 2),
                        f(mean([float(x["stalls"]) for x in L]), 1),
                        f(mean([x["smooth"] for x in L]), 1), f(min([x["smooth"] for x in L]), 1),
                        f(mean(ps), 1) if ps else ("inf" if any(x["psnr"] == "inf" for x in L) else ""),
                        f(min(ps), 1) if ps else ("inf" if any(x["psnr"] == "inf" for x in L) else ""),
                        f(mean([float(x["errors"]) for x in L]), 1), f(mean([x["pk_loss"] for x in L]), 2),
                        f(r["rtt"], 1), f(r["bg_mbps"], 1),
                        f(r["pl"]["path_loss_db"], 1) if r.get("pl") else "",
                        cfg_loss(r["path"]), cfg_rtt(r["path"]),
                        f(float(buffer_ms), 0), f(r["cpu"], 0)])
    with open(f"{outdir}/players_detail.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "players", "player", "frames_received", "frames_expected", "frames_lost",
                    "startup_ms", "frame_delay_avg_ms", "frame_delay_p95_ms", "frame_delay_max_ms",
                    "jitter_ms", "late_frames", "stalls", "smooth_playback_pct", "psnr_db",
                    "decoder_errors", "packets_sent", "packets_received", "packet_loss_pct",
                    "received_mbps"])
        for r in runs:
            for x in r["list"]:
                w.writerow([r["path"], r["players"], x["player"], x["received"], x["expected"],
                            x["lost"], f(x["startup"], 0), f(x["d_avg"], 1), f(x["d_p95"], 1),
                            f(x["d_max"], 1), f(x["jitter"], 1), x["late"], x["stalls"],
                            f(x["smooth"], 1), x["psnr"], x["errors"], x["pk_tx"], x["pk_rx"],
                            f(x["pk_loss"], 2), f(x["mbps"], 2)])



def save_notlive(runs, outdir, vod_buffer_s, size_mb, prefix="players_notlive"):
    f = lambda x, d=2: "" if x is None or x == "" else (f"{x:.{d}f}" if isinstance(x, float) else str(x))
    with open(f"{outdir}/{prefix}_summary.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "players", "file_mb", "total_mb", "startup_ms", "download_avg_s",
                    "download_max_s", "throughput_per_player_mbps", "stalls_per_player",
                    "stall_time_ms", "smooth_playback_pct", "smooth_worst_player_pct",
                    "all_identical", "tcp_retrans_pct", "ping_rtt_ms", "background_mbps",
                    "radio_path_loss_db", "configured_loss_pct", "configured_rtt_ms",
                    "vod_buffer_s", "cpu_busy_pct",
                    "mb_over_wifi", "mb_over_5g", "share_5g_pct", "ping_rtt_5g_ms"])
        for r in runs:
            L = r["list"]
            w.writerow([r["path"], r["players"], f(size_mb, 2), f(size_mb * r["players"], 1),
                        f(mean([x["startup"] for x in L]), 0), f(mean([x["dl_s"] for x in L]), 2),
                        f(max([x["dl_s"] or 0 for x in L]), 2), f(mean([x["mbps"] for x in L]), 1),
                        f(mean([float(x["stalls"]) for x in L]), 1),
                        f(mean([x["stall_ms"] for x in L]), 0), f(mean([x["smooth"] for x in L]), 1),
                        f(min([x["smooth"] for x in L]), 1),
                        "yes" if L and all(x["identical"] for x in L) else "no",
                        f(r["tcp_retrans_pct"], 2), f(r["rtt"], 1), f(r["bg_mbps"], 1),
                        f(r["pl"]["path_loss_db"], 1) if r.get("pl") else "",
                        cfg_loss(r["path"]), cfg_rtt(r["path"]),
                        f(float(vod_buffer_s), 1), f(r["cpu"], 0)] + share_cols(r, f))
    with open(f"{outdir}/{prefix}_detail.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "players", "player", "startup_ms", "download_s", "throughput_mbps",
                    "stalls", "stall_time_ms", "smooth_playback_pct", "identical"])
        for r in runs:
            for x in r["list"]:
                w.writerow([r["path"], r["players"], x["player"], f(x["startup"], 0), f(x["dl_s"], 2),
                            f(x["mbps"], 1), x["stalls"], f(x["stall_ms"], 0), f(x["smooth"], 1),
                            "yes" if x["identical"] else "no"])

vs_bitrate = 4.0


def main():
    global vs_bitrate
    ap = argparse.ArgumentParser(description="Video players at the receiver: 1 and many")
    ap.add_argument("--config", default=None)
    ap.add_argument("--video", default=None, help="video file (default: [video] file or test video)")
    ap.add_argument("--path", choices=["wifi", "5g", "both", "multi", "all"], default="all",
                    help="wifi, 5g, both (= wifi and 5g), multi (= Wi-Fi + 5G together, multipath), "
                         "all (= wifi, 5g and multi; default)")
    ap.add_argument("--players", default="1",
                    help="number(s) of players at the receiver (default 1), e.g. 1,2,4,8 to compare more players")
    ap.add_argument("--buffer", type=float, default=150.0,
                    help="player buffer in ms: frames later than this cause a stall (default 150)")
    ap.add_argument("--mode", choices=["live", "notlive", "both", "quic", "video", "all"], default="all",
                    help="live = UDP live stream, notlive = HTTP/TCP download+play, both = live+notlive, "
                         "quic = picoquic players only (live over QUIC + HTTP/3 download), "
                         "video = the REAL video sent live over picoquic, all = everything")
    ap.add_argument("--quic-live", choices=["stream", "dgram", "both"], default="both",
                    help="QUIC live players: streams (reliable), datagrams (unreliable) or both")
    ap.add_argument("--picoquic", default=None, help="picoquic folder (default ~/picoquic)")
    ap.add_argument("--vod-buffer", type=float, default=1.0,
                    help="not live: seconds of video buffered before playback starts (default 1)")
    ap.add_argument("--no-background", action="store_true")
    ap.add_argument("--no-psnr", action="store_true", help="skip picture-quality check (faster)")
    ap.add_argument("--out", default="players_results")
    args = ap.parse_args()
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is missing:  sudo apt install -y ffmpeg")

    cfg = netconfig.load(args.config)
    topo.apply_config(cfg)
    vc = vs.video_cfg(args.config)
    vs_bitrate = vc["bitrate_mbps"]
    src = args.video if args.video is not None else vc["file"]
    counts = sorted({int(x) for x in args.players.split(",") if x.strip()})
    names = {"both": ["wifi", "5g"], "all": ["wifi", "5g", "multi"]}.get(args.path, [args.path])
    background = not args.no_background
    print(netconfig.summary(cfg) +
          f"  Video: {src or 'test video'}, {vs_bitrate:g} Mbit/s per player, {vc['length_s']}s\n"
          f"  Modes: {args.mode}  (live = UDP, notlive = HTTP/TCP, quic = picoquic live + HTTP/3)\n"
          f"  Players at the receiver: {', '.join(map(str, counts))}  (live buffer {args.buffer:g} ms, "
          f"not-live start buffer {args.vod_buffer:g} s)\n"
          f"  Background: {'%g %% of capacity (constant)' % (cfg['bg_share'] * 100) if background else 'off'}\n",
          flush=True)

    setLogLevel("info")
    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)        # keep results of modes that are not run now
    do_live = args.mode in ("live", "both", "all")
    do_notlive = args.mode in ("notlive", "both", "all")
    do_quic = args.mode in ("quic", "all")
    do_video = args.mode in ("video", "all")
    pq = None
    if do_quic or do_video:
        import quic_test as qt
        pq = qt.find_picoquic(args.picoquic)
        if "QUICPERF_REPORT" not in open(os.path.join(pq, "picoquicfirst", "picoquicdemo.c")).read():
            raise SystemExit("picoquic needs the per-frame report: run  bash install_picoquic.sh  again")
        if do_video and "QUICPERF_VIDEO_IN" not in open(os.path.join(pq, "picohttp", "quicperf.c")).read():
            raise SystemExit("picoquic cannot carry the real video yet: run  bash install_picoquic.sh  again")
    qmodes = ["stream", "dgram"] if args.quic_live == "both" else [args.quic_live]
    with open(os.path.join(outdir, "stamp.py"), "w") as fh:
        fh.write(STAMP)
    with open(os.path.join(outdir, "dl.py"), "w") as fh:
        fh.write(DL)
    with open(os.path.join(outdir, "httpd.py"), "w") as fh:
        fh.write(HTTPD)
    video = os.path.join(outdir, "original.mp4")
    vs.prepare_video(src, video, vs_bitrate, vc["length_s"])
    length = float(vs.sh(f"ffprobe -v error -show_entries format=duration -of csv=p=0 "
                         f"'{video}'").stdout.strip() or vc["length_s"])

    # not live: the stored file, with the index at the start so it can play
    # while it downloads (like a web video)
    vod = os.path.join(outdir, "video.mp4")
    vs.sh(f"ffmpeg -y -loglevel error -i '{video}' -c copy -movflags +faststart '{vod}'")
    need = frame_bytes(vod)
    # live: the same video as an MPEG transport stream (what live TV / IPTV sends)
    live_ts = os.path.join(outdir, "live.ts")
    vs.sh(f"ffmpeg -y -loglevel error -i '{video}' -c copy -f mpegts '{live_ts}'")

    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = topo.build_topology()
    # count every UDP packet one by one at the receiver (no GRO merging)
    net.get("srv").cmd("for i in srv-wifi srv-5g; do ethtool -K $i gro off 2>/dev/null; done")
    live, notlive, qlive, qnotlive, lvideo = [], [], [], [], []
    has_mptcp = False
    if "multi" in names:
        has_mptcp = mptcp_ok(net)
        if has_mptcp:
            info("*** MPTCP endpoints on the sender: " + (mptcp_setup(net) or "(none)") + "\n")
        else:
            info("*** This kernel has no MPTCP: the Wi-Fi + 5G TCP download is skipped "
                 "(multipath QUIC still runs)\n")
    try:
        for n in names:
            if do_live and n != "multi":       # plain UDP cannot use two paths at once
                for N in counts:
                    live.append(run_once(net, n, N, video, length, outdir, background,
                                         cfg["bg_share"], args.buffer, not args.no_psnr))
            if do_notlive and (n != "multi" or has_mptcp):
                proto = "mptcp" if n == "multi" else "tcp"
                pid = net.get("sta1").cmd(f"python3 {outdir}/httpd.py {HTTP_PORT} {outdir} {proto} "
                                          f"> {outdir}/http_{n}.log 2>&1 & echo $!").split()
                time.sleep(1)
                for N in counts:
                    notlive.append(run_vod(net, n, N, vod, need, outdir, background,
                                           cfg["bg_share"], args.vod_buffer))
                if pid and pid[-1].isdigit():
                    net.get("sta1").cmd(f"kill {pid[-1]}")
            if do_quic:
                for qm in qmodes:
                    for N in counts:
                        qlive.append(run_quic_live(net, n, N, qm, pq, outdir, length, background,
                                                   cfg["bg_share"], args.buffer))
                for N in counts:
                    qnotlive.append(run_quic_vod(net, n, N, vod, need, pq, outdir, background,
                                                 cfg["bg_share"], args.vod_buffer))
            if do_video:
                for qm in qmodes:
                    lvideo.append(run_quic_video(net, n, qm, pq, outdir, live_ts, video, background,
                                                 cfg["bg_share"], args.buffer))
    finally:
        info("*** Stopping network\n")
        net.stop()
    if live:
        for n in [x for x in names if x != "multi"]:
            big = max(counts)
            grid_video(os.path.join(outdir, f"live_{n}_{big}players"), big)
        save_live(live, outdir, args.buffer, vs_bitrate)
    if notlive:
        save_notlive(notlive, outdir, args.vod_buffer, os.path.getsize(vod) / 1e6)
    if qlive:
        save_quic_live(qlive, outdir, args.buffer)
    if lvideo:
        save_livevideo(lvideo, outdir, args.buffer)
        for qm in qmodes:
            out = compare_video(outdir, qm, names, video)
            if out:
                info(f"*** Watch all networks side by side: {out}\n")
    if qnotlive:
        save_notlive(qnotlive, outdir, args.vod_buffer, os.path.getsize(vod) / 1e6,
                     prefix="players_quic_notlive")
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    import show_results
    show_results.show_players(outdir)
    info(f"\n*** All files in {outdir}/  (player_<k>.ts = what each player played,"
         f" players_grid.mp4 = up to 4 players side by side)\n"
         f"*** Show the tables again:  python3 show_results.py --players\n")


if __name__ == "__main__":
    main()
