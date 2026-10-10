#!/usr/bin/env python3
"""
Part 5 - I-frame aware sending over Wi-Fi + 5G (frame dependency -> path assignment).

A compressed video has three kinds of frames:
  I frame   a complete picture; needs no other frame
  P frame   only the changes from an earlier frame (needs it)
  B frame   changes from an earlier AND a later frame (needs both)
A GOP (group of pictures) starts with an I frame; every P/B frame of the GOP
depends on it. If the I frame is lost, the whole GOP cannot be shown.

This test
  1. encodes the video with H.264 and with HEVC (H.265) and ANALYSES THE FRAMES:
     how many I / P / B frames, their sizes, the GOP length;
  2. measures WHAT ONE LOST FRAME DOES: drops one I, one P and one B frame and
     counts how many frames of the decoded video are damaged;
  3. sends the video live over picoquic from sta1 to the player on srv:
        Wi-Fi only:
          wifi        whole video over Wi-Fi, QUIC datagrams (no frame awareness)
          wifi_combo  I frames as a QUIC stream (resent), P/B frames as datagrams, all on Wi-Fi
        5G only:
          5g          whole video over 5G, QUIC datagrams (no frame awareness)
          5g_combo    I frames as a QUIC stream (resent), P/B frames as datagrams, all on 5G
        Wi-Fi + 5G:
          ref         multipath QUIC datagrams over both paths (no frame awareness)
          fixed       (1) I frames -> 5G, P/B frames -> Wi-Fi, both QUIC datagrams
          combo       (3) I frames -> 5G with a QUIC stream, P/B frames -> Wi-Fi datagrams
     and measures which frames arrived in time, how many frames the viewer could
     see (frame dependency), the picture quality, delay and loss on each path.

The video is an MPEG transport stream: every 188-byte TS packet is given to the
I-frame part or the P/B part, the two parts are sent on their own picoquic
connection over their own path, and the receiver puts every byte back in its
original place (a live player only uses bytes that arrived within --buffer ms).

Run:   sudo python3 iframe_test.py --video BBB.webm
       sudo python3 iframe_test.py --video BBB.webm --scenarios single   # Wi-Fi only + 5G only
       python3 show_results.py --iframe           # show the tables again
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import time

from mininet.log import setLogLevel, info

import netconfig
import wifi_5g_topology as topo
import video_stream as vs
import players_test as pt

PATHS = topo.PATHS
TSP = 188                     # MPEG-TS packet size
SLOT = 5 * TSP                # video bytes per QUIC datagram (5 TS packets)
PORT_I, PORT_PB = 4470, 4471
CODEC_LABEL = {"h264": "H.264", "hevc": "HEVC (H.265)"}
SCEN = {
    # ---- Wi-Fi only
    "wifi":       {"group": "wifi", "label": "Wi-Fi only (no frame awareness)", "short": "Wi-Fi, no frame awareness",
                   "how": "whole video: Wi-Fi datagrams", "path": "wifi"},
    "wifi_combo": {"group": "wifi", "label": "Wi-Fi only: I stream + P/B datagram", "short": "Wi-Fi, I stream + P/B dgram",
                   "how": "I frames: Wi-Fi QUIC stream (resent) | P/B frames: Wi-Fi datagrams",
                   "i_mode": "stream", "pb_mode": "dgram", "i_path": "wifi", "pb_path": "wifi"},
    # ---- 5G only
    "5g":         {"group": "5g", "label": "5G only (no frame awareness)", "short": "5G, no frame awareness",
                   "how": "whole video: 5G datagrams", "path": "5g"},
    "5g_combo":   {"group": "5g", "label": "5G only: I stream + P/B datagram", "short": "5G, I stream + P/B dgram",
                   "how": "I frames: 5G QUIC stream (resent) | P/B frames: 5G datagrams",
                   "i_mode": "stream", "pb_mode": "dgram", "i_path": "5g", "pb_path": "5g"},
    # ---- Wi-Fi + 5G
    "ref":        {"group": "multi", "label": "Wi-Fi + 5G (no frame awareness)", "short": "Wi-Fi+5G, no frame awareness",
                   "how": "multipath QUIC datagrams, packets spread over both paths", "path": "multi"},
    "fixed":      {"group": "multi", "label": "(1) I -> 5G, P/B -> Wi-Fi", "short": "(1) I->5G, P/B->Wi-Fi",
                   "how": "I frames: 5G datagrams | P/B frames: Wi-Fi datagrams",
                   "i_mode": "dgram", "pb_mode": "dgram", "i_path": "5g", "pb_path": "wifi"},
    "combo":      {"group": "multi", "label": "(3) I -> 5G stream, P/B -> Wi-Fi datagram",
                   "short": "(3) I->5G stream, P/B->Wi-Fi dgram",
                   "how": "I frames: 5G QUIC stream (resent) | P/B frames: Wi-Fi datagrams",
                   "i_mode": "stream", "pb_mode": "dgram", "i_path": "5g", "pb_path": "wifi"},
}
ALL_SCEN = ["wifi", "wifi_combo", "5g", "5g_combo", "ref", "fixed", "combo"]
PATH_LABEL = {"wifi": "Wi-Fi", "5g": "5G", "multi": "Wi-Fi + 5G"}


# ---------------------------------------------------------------------------
#  1. Encode and analyse the frames
# ---------------------------------------------------------------------------
def encode(src, out, codec, bitrate, length):
    """Same picture, same bitrate, closed GOP of 30 frames (one I frame per second), 3 B frames."""
    inp = f"-i '{src}' -t {length}" if src else f"-f lavfi -i testsrc2=size=1280x720:rate=30 -t {length}"
    rate = f"-b:v {bitrate}M -maxrate {bitrate}M -bufsize {bitrate}M"
    if codec == "h264":
        enc = f"-c:v libx264 -preset veryfast {rate} -g 30 -keyint_min 30 -sc_threshold 0 -bf 3"
    else:
        enc = (f"-c:v libx265 -preset veryfast {rate} -tag:v hvc1 -x265-params "
               f"keyint=30:min-keyint=30:scenecut=0:bframes=3:open-gop=0:log-level=error")
    r = vs.sh(f"ffmpeg -y -loglevel error {inp} -an -vf scale=1280:720,fps=30 {enc} -pix_fmt yuv420p '{out}'")
    if r.returncode != 0:
        raise SystemExit(f"ffmpeg could not encode {codec}:\n{r.stderr}")


def frame_list(ts):
    """Frames in the order they are sent (decode order): type, size, pts, TS position."""
    pk = json.loads(vs.sh(f"ffprobe -v error -select_streams v:0 -show_entries packet=pts,dts,pos,size "
                          f"-of json '{ts}'").stdout or "{}").get("packets", [])
    fr = json.loads(vs.sh(f"ffprobe -v error -select_streams v:0 -show_entries frame=pts,pict_type "
                          f"-of json '{ts}'").stdout or "{}").get("frames", [])
    ptype = {int(f["pts"]): f.get("pict_type", "?")[:1] for f in fr if "pts" in f}
    out = []
    for p in pk:
        if "pos" not in p or "pts" not in p:
            continue
        out.append({"pos": int(p["pos"]), "size": int(p["size"]), "pts": int(p["pts"]),
                    "type": ptype.get(int(p["pts"]), "?"), "packets": []})
    return out


def split_ts(ts, frames, i_file, pb_file):
    """Give every TS packet to the I part or the P/B part. Returns (maps, owner of each packet)."""
    data = open(ts, "rb").read()
    n = len(data) // TSP
    start = {f["pos"]: k for k, f in enumerate(frames)}
    vpid = None
    if frames:
        b = data[frames[0]["pos"]:frames[0]["pos"] + 3]
        vpid = ((b[1] & 0x1F) << 8) | b[2]
    owner = [-1] * n                       # -1 = not video (PAT/PMT/...)
    cur = -1
    maps = {"I": [], "PB": []}
    with open(i_file, "wb") as fi, open(pb_file, "wb") as fp:
        for k in range(n):
            p = data[k * TSP:(k + 1) * TSP]
            pid = ((p[1] & 0x1F) << 8) | p[2]
            if pid == vpid:
                if (p[1] & 0x40) and k * TSP in start:
                    cur = start[k * TSP]
                owner[k] = cur
                if cur >= 0:
                    frames[cur]["packets"].append(k)
            part = "I" if owner[k] < 0 or frames[owner[k]]["type"] == "I" else "PB"
            maps[part].append(k)
            (fi if part == "I" else fp).write(p)
    return maps, owner


def frame_stats(frames, fps=30.0):
    st = {}
    total = sum(f["size"] for f in frames) or 1
    for t in ("I", "P", "B"):
        sz = [f["size"] for f in frames if f["type"] == t]
        st[t] = {"count": len(sz), "avg_kb": (sum(sz) / len(sz) / 1000.0) if sz else 0.0,
                 "max_kb": max(sz) / 1000.0 if sz else 0.0, "bytes_pct": 100.0 * sum(sz) / total}
    ipos = [k for k, f in enumerate(frames) if f["type"] == "I"]
    gaps = [b - a for a, b in zip(ipos, ipos[1:])]
    st["gop"] = (sum(gaps) / len(gaps)) if gaps else len(frames)
    st["frames"] = len(frames)
    st["i_vs_p"] = (st["I"]["avg_kb"] / st["P"]["avg_kb"]) if st["P"]["avg_kb"] else None
    return st


def damaged_frames(test, ref, thr=35.0):
    """Frames of `test` that differ visibly from `ref` (PSNR < thr dB). A frame the decoder
    could not show counts as damaged (the player repeats the last picture)."""
    log = test + ".psnr.log"
    vs.sh(f"ffmpeg -loglevel error -fflags +genpts -err_detect ignore_err -i '{test}' -i '{ref}' -lavfi "
          f"\"[0:v]setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];[a][b]psnr=stats_file='{log}'\" "
          f"-f null -")
    vals = []
    try:
        for line in open(log):
            for tok in line.split():
                if tok.startswith("psnr_avg:"):
                    v = tok.split(":")[1]
                    vals.append(float("inf") if v == "inf" else float(v))
    except OSError:
        pass
    try:
        os.remove(log)
    except OSError:
        pass
    total = vs.count_frames(ref)
    bad = sum(1 for v in vals if v < thr) + max(0, total - len(vals))
    return bad, total


def loss_impact(ts, frames, d, fps=30.0):
    """Drop ONE frame of a type (in the middle of the video) and count the damaged frames."""
    data = open(ts, "rb").read()
    ipos = [k for k, f in enumerate(frames) if f["type"] == "I"]
    g = ipos[len(ipos) // 2] if ipos else 0          # an I frame in the middle
    nxt = next((k for k in ipos if k > g), len(frames))
    pick = {"I": g,
            "P": next((k for k in range(g + 1, nxt) if frames[k]["type"] == "P"), None),
            "B": next((k for k in range(g + 1, nxt) if frames[k]["type"] == "B"), None)}
    out = {}
    for t, k in pick.items():
        if k is None:
            continue
        buf = bytearray(data)
        for pkt in frames[k]["packets"]:
            buf[pkt * TSP:(pkt + 1) * TSP] = b"\xff" * TSP      # packet lost
        f = os.path.join(d, f"lose_one_{t}.ts")
        open(f, "wb").write(buf)
        bad, total = damaged_frames(f, ts)
        out[t] = {"frame": k, "kb": frames[k]["size"] / 1000.0, "damaged": bad, "total": total,
                  "seconds": bad / fps}
        os.remove(f)
    return out


# ---------------------------------------------------------------------------
#  2. What arrived: put the parts back together, frame dependency
# ---------------------------------------------------------------------------
def read_pieces(path):
    rows, seen = [], set()
    try:
        for line in open(path):
            f = line.strip().split(",")
            if len(f) == 4 and all(x.isdigit() for x in f):
                key = (int(f[0]), int(f[1]))
                if key not in seen:
                    seen.add(key)
                    rows.append(tuple(int(x) for x in f))
    except OSError:
        pass
    return rows


def rebuild(parts, size, buffer_ms, live_ts):
    """parts = [(received_file, pieces.csv, map or None)]. Writes what a live player can use.
    Returns per-TS-packet coverage (on time), per-part delays and byte counts."""
    npk = size // TSP
    buf = bytearray(size)
    cov = [0] * npk
    stats = []
    for recv, log, mp in parts:
        try:
            got = open(recv, "rb").read()
        except OSError:
            got = b""
        part_size = (len(mp) * TSP) if mp is not None else size
        d, rec, ontime = [], 0, 0
        for off, ln, sent, arr in read_pieces(log):
            if off >= part_size:
                continue
            ln = min(ln, part_size - off)
            delay = (arr - sent) / 1000.0
            d.append(delay)
            rec += ln
            if delay > buffer_ms:
                continue
            ontime += ln
            a = off
            while a < off + ln:
                j = a // TSP
                b = min(off + ln, (j + 1) * TSP)
                k = mp[j] if mp is not None else j
                if k < npk:
                    o = k * TSP + (a - j * TSP)
                    buf[o:o + (b - a)] = got[a:b]
                    cov[k] += b - a
                a = b
        stats.append({"delays": d, "received": rec, "ontime": ontime, "size": part_size})
    with open(live_ts, "wb") as fh:
        fh.write(buf)
    return cov, stats


def dependency(frames, cov):
    """Which frames the viewer can see: complete (every TS packet in time) AND every frame it
    depends on can be shown. I: itself. P: the previous I/P. B: the two previous I/P (decode order)."""
    ok_ref = []                 # decodable flags of the reference frames (I/P) of this GOP
    res = {"I": [0, 0], "P": [0, 0], "B": [0, 0], "shown": 0, "gops": 0, "gops_lost": 0}
    for f in frames:
        t = f["type"] if f["type"] in ("I", "P", "B") else "P"
        complete = bool(f["packets"]) and all(cov[k] >= TSP for k in f["packets"])
        res[t][1] += 1
        res[t][0] += complete
        if t == "I":
            res["gops"] += 1
            res["gops_lost"] += not complete
            ok_ref = [complete]
            dec = complete
        elif t == "P":
            dec = complete and bool(ok_ref) and ok_ref[-1]
            ok_ref.append(dec)
        else:
            dec = complete and len(ok_ref) >= 1 and all(ok_ref[-2:])
        res["shown"] += dec
    return res


# ---------------------------------------------------------------------------
#  3. Sending with frame-aware path assignment
# ---------------------------------------------------------------------------
def send_part(sta1, srv, pq, sub, part_file, mode, path, port, secs):
    """Start one picoquic sender (sta1) + receiver (srv) for one part on one path."""
    os.makedirs(sub, exist_ok=True)
    size = os.path.getsize(part_file)
    scen, ns, _ = pt.video_scenario(mode, size, secs, slot=SLOT)
    env = f"QUICPERF_VIDEO_IN={part_file} QUICPERF_VIDEO_NS={ns} QUICPERF_VIDEO_SLOT={SLOT} "
    pid = pt.start_pq_server(sta1, pq, port, sub, env=env)
    srv.cmd(f"(QUICPERF_VIDEO_OUT={sub}/received_all.ts QUICPERF_VIDEO_LOG={sub}/pieces.csv "
            f"QUICPERF_VIDEO_NS={ns} QUICPERF_VIDEO_SLOT={SLOT} "
            f"timeout {int(secs) + 40} {pq}/picoquicdemo -0 -a perf -n test "
            f"{PATHS[path]['client_ip']} {port} '{scen}' > {sub}/client.log 2>&1; touch {sub}/done) &")
    return pid


def run_split(net, sc, pq, d, ts, frames, maps, original, secs, background, bg_share, buffer_ms):
    sta1, srv = net.get("sta1"), net.get("srv")
    sub = os.path.join(d, sc)
    shutil.rmtree(sub, ignore_errors=True)
    os.makedirs(sub)
    S = SCEN[sc]
    info(f"\n========== {S['label']}  ({S['how']}) ==========\n")
    for port in (PORT_I, PORT_PB):
        pt.iface_rules(srv, "udp", port)
        sta1.cmd(f"iptables -w -I OUTPUT -p udp --sport {port}")
        srv.cmd(f"iptables -w -I INPUT -p udp --sport {port}")
    net_n = S["i_path"] if S["i_path"] == S["pb_path"] else "multi"
    if background:
        pt.bg_start_all(sta1, srv, net_n, bg_share, sub)
    pt.ping_all(sta1, net_n, sub, int(secs) + 20)
    time.sleep(2)
    t0 = time.time()
    pid_i = send_part(sta1, srv, pq, f"{sub}/I", f"{d}/part_I.ts", S["i_mode"], S["i_path"], PORT_I, secs)
    pid_pb = send_part(sta1, srv, pq, f"{sub}/PB", f"{d}/part_PB.ts", S["pb_mode"], S["pb_path"], PORT_PB, secs)
    info(f"*** I frames ({os.path.getsize(d + '/part_I.ts')/1e6:.1f} MB) over {PATH_LABEL[S['i_path']]} "
         f"{'QUIC stream' if S['i_mode'] == 'stream' else 'QUIC datagrams'}, P/B frames "
         f"({os.path.getsize(d + '/part_PB.ts')/1e6:.1f} MB) over {PATH_LABEL[S['pb_path']]} "
         f"{'QUIC stream' if S['pb_mode'] == 'stream' else 'QUIC datagrams'}, live, {secs:g} s\n")
    while time.time() - t0 < secs + 45 and not (os.path.exists(f"{sub}/I/done") and os.path.exists(f"{sub}/PB/done")):
        time.sleep(0.5)
    bg = pt.stop_bg(sub) if background else 0.0
    pt.stop_pq_server(sta1, pid_i)
    pt.stop_pq_server(sta1, pid_pb)
    srv.cmd("pkill -9 iperf3")
    sta1.cmd("pkill -9 iperf3; pkill -9 ping")
    run = {"codec": None, "scenario": sc, "bg_mbps": bg, "frames": frames}
    for port, tag in ((PORT_I, "i"), (PORT_PB, "pb")):
        tx = pt.qt_counter(sta1, "OUTPUT", port)
        rx = pt.qt_counter(srv, "INPUT", port)
        sta1.cmd(f"iptables -w -D OUTPUT -p udp --sport {port}")
        srv.cmd(f"iptables -w -D INPUT -p udp --sport {port}")
        run[f"loss_{tag}"] = 100.0 * max(0, tx[0] - rx[0]) / tx[0] if tx[0] else None
        w, g, _ = pt.iface_share(srv, port)
        pt.iface_rules(srv, "udp", port, add=False)
        run[f"mb_wifi_{tag}"], run[f"mb_5g_{tag}"] = w / 1e6, g / 1e6
        run[f"resent_{tag}"] = pt.server_retrans(f"{sub}/{'I' if tag == 'i' else 'PB'}")[0]
    rtts(run, sub, net_n)
    parts = [(f"{sub}/I/received_all.ts", f"{sub}/I/pieces.csv", maps["I"]),
             (f"{sub}/PB/received_all.ts", f"{sub}/PB/pieces.csv", maps["PB"])]
    return finish(run, parts, ts, original, sub, buffer_ms)


def rtts(run, sub, n):
    p1 = vs.ping_stats(f"{sub}/ping.log")[0]
    p2 = vs.ping_stats(f"{sub}/ping2.log")[0]
    run["rtt_wifi"] = p1 if n in ("wifi", "multi") else None
    run["rtt_5g"] = p2 if n == "multi" else (p1 if n == "5g" else None)


def run_ref(net, sc, pq, d, ts, frames, original, background, bg_share, buffer_ms):
    """Whole video, no frame awareness: datagrams over one path, or multipath over both."""
    S = SCEN[sc]
    n = S["path"]
    info(f"\n========== {S['label']}  ({S['how']}) ==========\n")
    r = pt.run_quic_video(net, n, "dgram", pq, d, ts, original, background, bg_share, buffer_ms)
    sub = os.path.join(d, sc)
    shutil.rmtree(sub, ignore_errors=True)
    shutil.move(os.path.join(d, f"livevideo_dgram_{n}"), sub)
    run = {"scenario": sc, "frames": frames, "bg_mbps": r["bg_mbps"], "loss_i": r["pk_loss"], "loss_pb": None,
           "resent_i": r["retrans_pct"], "resent_pb": None,
           "mb_wifi_i": r.get("mb_wifi"), "mb_5g_i": r.get("mb_5g"), "mb_wifi_pb": 0.0, "mb_5g_pb": 0.0}
    rtts(run, sub, n)
    return finish(run, [(f"{sub}/received_all.ts", f"{sub}/pieces.csv", None)], ts, original, sub, buffer_ms)


def finish(run, parts, ts, original, sub, buffer_ms):
    size = os.path.getsize(ts)
    cov, st = rebuild(parts, size, buffer_ms, f"{sub}/live.ts")
    frames = run["frames"]
    dep = dependency(frames, cov)
    run.update(dep=dep)
    d_all = [x for s in st for x in s["delays"]]
    ds = sorted(d_all)
    run["delay_avg"] = pt.mean(d_all)
    run["delay_p95"] = pt.pctl(ds, 95) if ds else None
    run["delay_i"] = pt.mean(st[0]["delays"]) if len(st) > 1 else None
    run["delay_i_p95"] = pt.pctl(sorted(st[0]["delays"]), 95) if len(st) > 1 and st[0]["delays"] else None
    run["delay_pb"] = pt.mean(st[1]["delays"]) if len(st) > 1 else None
    run["ontime_pct"] = 100.0 * sum(s["ontime"] for s in st) / max(1, sum(s["size"] for s in st))
    pt.watchable(f"{sub}/live.ts", f"{sub}/live.mp4")
    run["psnr"], run["ssim"] = vs.quality(f"{sub}/live.ts", original)
    run["damaged"], run["total"] = damaged_frames(f"{sub}/live.ts", ts)
    I, P, B = dep["I"], dep["P"], dep["B"]
    info(f"    I frames complete {I[0]}/{I[1]}, P {P[0]}/{P[1]}, B {B[0]}/{B[1]}; "
         f"GOPs lost {dep['gops_lost']}/{dep['gops']}; frames the viewer can see {dep['shown']}/{len(frames)}\n"
         f"    measured: {run['damaged']}/{run['total']} frames damaged, PSNR {run['psnr'] or '-'} dB"
         f"  ->  {sub}/live.mp4\n")
    return run


# ---------------------------------------------------------------------------
#  Grid video and CSV files
# ---------------------------------------------------------------------------
def grid(d, c, out):
    """One codec: Original + every case that has a result, 4 tiles per row, a label on each tile."""
    font = ""
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf"):
        if os.path.exists(f):
            font = f
            break
    w, h, per_row = 480, 270, 4
    cols = [("Original " + CODEC_LABEL[c], f"{d}/{c}/original.mp4")] + \
           [(SCEN[s]["short"], f"{d}/{c}/{s}/live.mp4") for s in ALL_SCEN]
    cols = [(lab, f) for lab, f in cols if os.path.exists(f)]
    if len(cols) < 2:
        return None
    ins, fc, tiles = [], "", []
    for k, (lab, f) in enumerate(cols):
        ins.append(f)
        txt = lab.replace(":", "\\:").replace("'", "")
        draw = (f",drawtext=fontfile='{font}':text='{txt}':x=6:y=6:fontsize=16:fontcolor=white:"
                f"box=1:boxcolor=black@0.65:boxborderw=4") if font else ""
        fc += f"[{k}:v]setpts=PTS-STARTPTS,fps=30,scale={w}:{h}{draw}[t{k}];"
        tiles.append((f"[t{k}]", (k % per_row) * w, (k // per_row) * h))
    fc += "".join(t for t, _, _ in tiles) + \
        f"xstack=inputs={len(tiles)}:layout={'|'.join(f'{x}_{y}' for _, x, y in tiles)}:shortest=1:fill=black"
    r = vs.sh("ffmpeg -y -loglevel error " + " ".join(f"-i '{f}'" for f in ins) +
              f" -filter_complex \"{fc}\" -an -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p '{out}'")
    return out if r.returncode == 0 else None


def save(d, analysis, runs, buffer_ms):
    f = lambda x, n=2: "" if x is None or x == "" else (f"{x:.{n}f}" if isinstance(x, float) else str(x))
    with open(f"{d}/iframe_frames.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["codec", "frames", "gop_frames", "i_count", "i_avg_kb", "i_max_kb", "i_bytes_pct",
                    "p_count", "p_avg_kb", "p_bytes_pct", "b_count", "b_avg_kb", "b_bytes_pct", "i_vs_p",
                    "file_mb", "part_i_mb", "part_pb_mb",
                    "lose_i_damaged", "lose_p_damaged", "lose_b_damaged", "lose_i_s", "lose_p_s", "lose_b_s"])
        for c, a in analysis.items():
            st, im = a["stats"], a["impact"]
            g = lambda t, k: im.get(t, {}).get(k)
            w.writerow([c, st["frames"], f(st["gop"], 1), st["I"]["count"], f(st["I"]["avg_kb"], 1),
                        f(st["I"]["max_kb"], 1), f(st["I"]["bytes_pct"], 1), st["P"]["count"],
                        f(st["P"]["avg_kb"], 1), f(st["P"]["bytes_pct"], 1), st["B"]["count"],
                        f(st["B"]["avg_kb"], 1), f(st["B"]["bytes_pct"], 1), f(st["i_vs_p"], 1),
                        f(a["mb"], 2), f(a["mb_i"], 2), f(a["mb_pb"], 2),
                        f(g("I", "damaged")), f(g("P", "damaged")), f(g("B", "damaged")),
                        f(g("I", "seconds"), 2), f(g("P", "seconds"), 2), f(g("B", "seconds"), 2)])
    # keep the results of cases that were not run this time (e.g. run "single" after "multi")
    old = []
    try:
        with open(f"{d}/iframe_summary.csv") as fh:
            done = {(r["codec"], r["scenario"]) for r in runs}
            old = [r for r in csv.DictReader(fh) if (r.get("codec"), r.get("scenario")) not in done]
        for r in old:                          # column names of the first version
            r.setdefault("loss_i_pct", r.get("loss_5g_i_pct", ""))
            r.setdefault("loss_pb_pct", r.get("loss_wifi_pb_pct", ""))
    except OSError:
        pass
    with open(f"{d}/iframe_summary.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        hdr = ["codec", "scenario", "i_complete", "i_total", "p_complete", "p_total", "b_complete",
                    "b_total", "gops_lost", "gops", "frames_shown_model", "frames", "frames_damaged",
                    "frames_total", "psnr_db", "ssim", "bytes_on_time_pct", "delay_avg_ms", "delay_p95_ms",
                    "delay_i_ms", "delay_i_p95_ms", "delay_pb_ms", "loss_i_pct", "loss_pb_pct",
                    "resent_i_pct", "resent_pb_pct", "mb_wifi", "mb_5g", "rtt_wifi_ms", "rtt_5g_ms",
                    "background_mbps", "buffer_ms"]
        w.writerow(hdr)
        for r in old:
            w.writerow([r.get(k, "") for k in hdr])
        for r in runs:
            dp = r["dep"]
            w.writerow([r["codec"], r["scenario"], dp["I"][0], dp["I"][1], dp["P"][0], dp["P"][1], dp["B"][0],
                        dp["B"][1], dp["gops_lost"], dp["gops"], dp["shown"], len(r["frames"]), r["damaged"],
                        r["total"], r["psnr"], r["ssim"], f(r["ontime_pct"]), f(r["delay_avg"], 1),
                        f(r["delay_p95"], 1), f(r["delay_i"], 1), f(r["delay_i_p95"], 1), f(r["delay_pb"], 1),
                        f(r["loss_i"]), f(r["loss_pb"]), f(r["resent_i"]), f(r["resent_pb"]),
                        f((r.get("mb_wifi_i") or 0) + (r.get("mb_wifi_pb") or 0)),
                        f((r.get("mb_5g_i") or 0) + (r.get("mb_5g_pb") or 0)),
                        f(r["rtt_wifi"], 1), f(r["rtt_5g"], 1), f(r["bg_mbps"], 1), f(float(buffer_ms), 0)])


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="I-frame aware sending over Wi-Fi + 5G")
    ap.add_argument("--config", default=None)
    ap.add_argument("--video", default=None, help="video file (default: [video] file or test video)")
    ap.add_argument("--codecs", default="h264,hevc", help="h264, hevc or both (default)")
    ap.add_argument("--scenarios", default="all",
                    help="all, or a list of: wifi, wifi_combo (Wi-Fi only), 5g, 5g_combo (5G only), "
                         "ref, fixed, combo (Wi-Fi + 5G). Shortcuts: single = Wi-Fi only + 5G only, multi = Wi-Fi + 5G")
    ap.add_argument("--buffer", type=float, default=150.0, help="live player buffer in ms (default 150)")
    ap.add_argument("--picoquic", default=None)
    ap.add_argument("--no-background", action="store_true")
    ap.add_argument("--analyse-only", action="store_true", help="only the frame analysis (no network)")
    ap.add_argument("--out", default="iframe_results")
    a = ap.parse_args()

    cfg = netconfig.load(a.config)
    topo.apply_config(cfg)
    vc = vs.video_cfg(a.config)
    src = a.video if a.video is not None else vc["file"]
    codecs = [c.strip() for c in a.codecs.split(",") if c.strip() in CODEC_LABEL]
    want = []
    for x in a.scenarios.split(","):
        x = x.strip()
        want += {"all": ALL_SCEN, "single": ALL_SCEN[:4], "multi": ALL_SCEN[4:]}.get(x, [x])
    scens = [s for s in ALL_SCEN if s in want]
    background = not a.no_background
    setLogLevel("info")
    outdir = os.path.abspath(a.out)
    os.makedirs(outdir, exist_ok=True)

    pq = None
    if not a.analyse_only:
        import quic_test as qt
        pq = qt.find_picoquic(a.picoquic)
        if "QUICPERF_VIDEO_IN" not in open(os.path.join(pq, "picohttp", "quicperf.c")).read():
            raise SystemExit("picoquic cannot carry the real video yet: python3 patch_quicperf_video.py ~/picoquic "
                             "&& (cd ~/picoquic && make picoquicdemo)")

    # ---- 1 + 2: encode, analyse the frames, effect of one lost frame
    analysis = {}
    for c in codecs:
        d = os.path.join(outdir, c)
        os.makedirs(d, exist_ok=True)
        info(f"\n*** {CODEC_LABEL[c]}: encoding {vc['length_s']} s at {vc['bitrate_mbps']:g} Mbit/s "
             f"(one I frame per second, 3 B frames)\n")
        encode(src, f"{d}/original.mp4", c, vc["bitrate_mbps"], vc["length_s"])
        ts = f"{d}/video.ts"
        vs.sh(f"ffmpeg -y -loglevel error -i '{d}/original.mp4' -c copy -f mpegts '{ts}'")
        frames = frame_list(ts)
        maps, _ = split_ts(ts, frames, f"{d}/part_I.ts", f"{d}/part_PB.ts")
        st = frame_stats(frames)
        info(f"    {st['frames']} frames: I {st['I']['count']} (avg {st['I']['avg_kb']:.1f} kB, "
             f"{st['I']['bytes_pct']:.0f} % of the bytes), P {st['P']['count']} (avg {st['P']['avg_kb']:.1f} kB), "
             f"B {st['B']['count']} (avg {st['B']['avg_kb']:.1f} kB), GOP {st['gop']:.0f} frames\n")
        info("    Dropping one I, one P and one B frame to see how many frames are damaged ...\n")
        im = loss_impact(ts, frames, d)
        for t, v in im.items():
            info(f"      one {t} frame lost -> {v['damaged']} frames damaged ({v['seconds']:.2f} s of video)\n")
        analysis[c] = {"stats": st, "impact": im, "frames": frames, "maps": maps, "ts": ts,
                       "original": f"{d}/original.mp4", "mb": os.path.getsize(ts) / 1e6,
                       "mb_i": os.path.getsize(f"{d}/part_I.ts") / 1e6,
                       "mb_pb": os.path.getsize(f"{d}/part_PB.ts") / 1e6}

    runs = []
    if not a.analyse_only and scens:
        subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        net = topo.build_topology()
        net.get("srv").cmd("for i in srv-wifi srv-5g; do ethtool -K $i gro off 2>/dev/null; done")
        try:
            for c in codecs:
                A = analysis[c]
                d = os.path.join(outdir, c)
                secs = float(vs.sh(f"ffprobe -v error -show_entries format=duration -of csv=p=0 "
                                   f"'{A['ts']}'").stdout.strip() or vc["length_s"])
                info(f"\n######## {CODEC_LABEL[c]} ########\n")
                for s in scens:
                    if "path" in SCEN[s]:
                        run = run_ref(net, s, pq, d, A["ts"], A["frames"], A["original"], background,
                                      cfg["bg_share"], a.buffer)
                    else:
                        run = run_split(net, s, pq, d, A["ts"], A["frames"], A["maps"], A["original"], secs,
                                        background, cfg["bg_share"], a.buffer)
                    run["codec"] = c
                    runs.append(run)
        finally:
            info("*** Stopping network\n")
            net.stop()
    save(outdir, analysis, runs, a.buffer)
    if runs:
        for c in codecs:
            out = grid(outdir, c, f"{outdir}/iframe_grid_{c}.mp4")
            if out:
                info(f"\n*** {CODEC_LABEL[c]}: watch every case side by side: {out}\n")
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    import show_results
    show_results.show_iframe(outdir)


if __name__ == "__main__":
    main()
