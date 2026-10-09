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
       sudo python3 players_test.py --mode live --players 1,2,4,8,16 --buffer 200
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
LABEL = {"wifi": "Wi-Fi", "5g": "5G"}
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
DL = '''import sys, time, urllib.request
url, out, log = sys.argv[1:4]
L = open(log, "w")
L.write("start %.6f\\n" % time.time())
n = 0
with urllib.request.urlopen(url) as r, open(out, "wb") as f:
    rd = getattr(r, "read1", r.read)
    while True:
        b = rd(65536)
        if not b:
            break
        f.write(b)
        n += len(b)
        L.write("%.6f %d\\n" % (time.time(), n))
L.write("done %.6f\\n" % time.time())
L.close()
'''
HTTP_PORT = 8080


def start_bg(sta1, srv, p, rate, sub):
    """Background UDP traffic in 5 s pieces until stop_bg (each piece reports)."""
    srv.cmd(f"iperf3 -s -D -p {p['bg_port']}")
    time.sleep(1)
    sta1.cmd(f"(i=0; while [ ! -e {sub}/bg.stop ]; do iperf3 -c {p['server_ip']} -p {p['bg_port']} "
             f"-u -b {rate}M -t 5 -J > {sub}/bg_$i.json 2>&1; i=$((i+1)); done) &")


def stop_bg(sub):
    open(f"{sub}/bg.stop", "w").close()
    time.sleep(6)                            # let the last piece finish and report
    import glob
    vals = [vs.iperf_bg(f)[0] for f in sorted(glob.glob(f"{sub}/bg_*.json"))]
    return mean(vals)


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
    """NOT LIVE: N players download the stored video over HTTP/TCP and play it."""
    sta1, srv = net.get("sta1"), net.get("srv")
    p = PATHS[n]
    sub = os.path.join(outdir, f"notlive_{n}_{N}players")
    os.makedirs(sub, exist_ok=True)
    info(f"\n========== NOT LIVE (video on demand) - {LABEL[n]}: {N} player(s) ==========\n")
    if background:
        rate = max(1, int(p["bw"] * bg_share))
        start_bg(sta1, srv, p, rate, sub)
        info(f"*** Background {rate} Mbit/s ({bg_share*100:g} % of capacity)\n")
    sta1.cmd(f"ping -D -i 0.2 -w 60 {p['server_ip']} > {sub}/ping.log 2>&1 &")
    time.sleep(2)
    tcp0 = vs.snmp(sta1, "Tcp")
    url = f"http://{p['client_ip']}:{HTTP_PORT}/{os.path.basename(vod)}"
    info(f"*** {N} player(s) download {os.path.getsize(vod)/1e6:.1f} MB each over HTTP/TCP "
         f"and start playing after {vod_buffer_s:g} s of video is buffered\n")
    c0 = cpu_times()
    t0 = time.time()
    for i in range(1, N + 1):
        srv.cmd(f"python3 {outdir}/dl.py {url} {sub}/player_{i}.mp4 {sub}/player_{i}.log &")
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
    if n == "wifi":
        run["pl"] = vs.wifi_path_loss(net)
    info(f"    TCP retransmissions {vs.f1(run['tcp_retrans_pct'], ' %', 2)}, "
         f"CPU busy {run['cpu']:.0f} %\n")
    return run


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
                        PATHS[r["path"]]["loss"], 2 * float(PATHS[r["path"]]["delay"].rstrip("ms")),
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



def save_notlive(runs, outdir, vod_buffer_s, size_mb):
    f = lambda x, d=2: "" if x is None or x == "" else (f"{x:.{d}f}" if isinstance(x, float) else str(x))
    with open(f"{outdir}/players_notlive_summary.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "players", "file_mb", "total_mb", "startup_ms", "download_avg_s",
                    "download_max_s", "throughput_per_player_mbps", "stalls_per_player",
                    "stall_time_ms", "smooth_playback_pct", "smooth_worst_player_pct",
                    "all_identical", "tcp_retrans_pct", "ping_rtt_ms", "background_mbps",
                    "radio_path_loss_db", "configured_loss_pct", "configured_rtt_ms",
                    "vod_buffer_s", "cpu_busy_pct"])
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
                        PATHS[r["path"]]["loss"], 2 * float(PATHS[r["path"]]["delay"].rstrip("ms")),
                        f(float(vod_buffer_s), 1), f(r["cpu"], 0)])
    with open(f"{outdir}/players_notlive_detail.csv", "w", newline="") as fh:
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
    ap.add_argument("--path", choices=["wifi", "5g", "both"], default="both")
    ap.add_argument("--players", default="1,2,4,8", help="numbers of players to test, e.g. 1,2,4,8,16")
    ap.add_argument("--buffer", type=float, default=150.0,
                    help="player buffer in ms: frames later than this cause a stall (default 150)")
    ap.add_argument("--mode", choices=["live", "notlive", "both"], default="both",
                    help="live stream (UDP, real time), not live (download + play, HTTP/TCP), or both")
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
    names = ["wifi", "5g"] if args.path == "both" else [args.path]
    background = not args.no_background
    print(netconfig.summary(cfg) +
          f"  Video: {src or 'test video'}, {vs_bitrate:g} Mbit/s per player, {vc['length_s']}s\n"
          f"  Modes: {'live (UDP) + not live (HTTP/TCP download)' if args.mode == 'both' else args.mode}\n"
          f"  Players at the receiver: {', '.join(map(str, counts))}  (live buffer {args.buffer:g} ms, "
          f"not-live start buffer {args.vod_buffer:g} s)\n"
          f"  Background: {'%g %% of capacity (constant)' % (cfg['bg_share'] * 100) if background else 'off'}\n",
          flush=True)

    setLogLevel("info")
    outdir = os.path.abspath(args.out)
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)
    with open(os.path.join(outdir, "stamp.py"), "w") as fh:
        fh.write(STAMP)
    with open(os.path.join(outdir, "dl.py"), "w") as fh:
        fh.write(DL)
    video = os.path.join(outdir, "original.mp4")
    vs.prepare_video(src, video, vs_bitrate, vc["length_s"])
    length = float(vs.sh(f"ffprobe -v error -show_entries format=duration -of csv=p=0 "
                         f"'{video}'").stdout.strip() or vc["length_s"])

    # not live: the stored file, with the index at the start so it can play
    # while it downloads (like a web video)
    vod = os.path.join(outdir, "video.mp4")
    vs.sh(f"ffmpeg -y -loglevel error -i '{video}' -c copy -movflags +faststart '{vod}'")
    need = frame_bytes(vod)

    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = topo.build_topology()
    # count every UDP packet one by one at the receiver (no GRO merging)
    net.get("srv").cmd("for i in srv-wifi srv-5g; do ethtool -K $i gro off 2>/dev/null; done")
    live, notlive = [], []
    try:
        for n in names:
            if args.mode in ("live", "both"):
                for N in counts:
                    live.append(run_once(net, n, N, video, length, outdir, background,
                                         cfg["bg_share"], args.buffer, not args.no_psnr))
            if args.mode in ("notlive", "both"):
                pid = net.get("sta1").cmd(f"python3 -m http.server {HTTP_PORT} --bind 0.0.0.0 "
                                          f"--directory {outdir} > {outdir}/http_{n}.log 2>&1 & echo $!").split()
                time.sleep(1)
                for N in counts:
                    notlive.append(run_vod(net, n, N, vod, need, outdir, background,
                                           cfg["bg_share"], args.vod_buffer))
                if pid and pid[-1].isdigit():
                    net.get("sta1").cmd(f"kill {pid[-1]}")
    finally:
        info("*** Stopping network\n")
        net.stop()
    if live:
        for n in names:
            big = max(counts)
            grid_video(os.path.join(outdir, f"live_{n}_{big}players"), big)
        save_live(live, outdir, args.buffer, vs_bitrate)
    if notlive:
        save_notlive(notlive, outdir, args.vod_buffer, os.path.getsize(vod) / 1e6)
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    import show_results
    show_results.show_players(outdir)
    info(f"\n*** All files in {outdir}/  (player_<k>.ts = what each player played,"
         f" players_grid.mp4 = up to 4 players side by side)\n"
         f"*** Show the tables again:  python3 show_results.py --players\n")


if __name__ == "__main__":
    main()
