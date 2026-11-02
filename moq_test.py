#!/usr/bin/env python3
"""
Part 9 - The video over MoQ (Media over QUIC Transport, draft-16 - the MoQtail version) on top of
picoquic, with the Part 8 network-condition switching.

    moq/moqt.c, moq/moqt.h   the MoQ layer (MOQT draft-16, ALPN "moqt-16") written on picoquic
    moq/moqpico              publisher ("pub") and subscriber ("sub") built on that layer

How the video is carried (one MoQ TRACK "moqpico/video"):

    GROUP   = one GOP (starts at every I frame, 1 per second)
    OBJECT  = 940 bytes of the video (5 MPEG-TS packets); every frame is cut into objects
    I frame objects   -> SUBGROUP stream (reliable, lost pieces are resent) on the BETTER path
    P/B frame objects -> OBJECT_DATAGRAMs (not resent) on the WORSE path

The receiver (srv) opens one MoQ session per path (Wi-Fi 10.0.1.x and 5G 10.0.2.x) and subscribes
to the same track on both. The sender (sta1) measures delay/loss of both paths every 5 s (exactly as
Part 8: probes, Td = 50 ms, TL = 5 %, good / degraded / lossy / congested) and tells the MoQ publisher
which session carries the I frames and which the P/B frames (it writes "<I path> <P/B path>" into a
small file the publisher re-reads every 100 ms).

Run (VM):
    sudo python3 moq_test.py --selftest                              # 1 min, no Mininet: MoQ works?
    sudo python3 moq_test.py --video BBB.webm --scenarios congested --bg-high 120 --length 120 --no-video
    sudo python3 moq_test.py --video BBB.webm                        # all 3 scenarios, fixed + adaptive
    python3 moq_test.py --show                                       # tables again
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MOQ_DIR = os.path.join(HERE, "moq")
BIN = os.path.join(MOQ_DIR, "moqpico")
PORT = 4443
TSP = 188


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def find_picoquic(arg):
    cands = [arg] if arg else []
    user = os.environ.get("SUDO_USER") or ""
    cands += [os.path.expanduser(f"~{user}/picoquic"), os.path.expanduser("~/picoquic")]
    for d in cands:
        if d and os.path.isfile(os.path.join(d, "libpicoquic-core.a")):
            return os.path.abspath(d)
    raise SystemExit("picoquic not found (need ~/picoquic with libpicoquic-core.a): bash install_picoquic.sh")


def build_moqpico(pq):
    """(Re)build moq/moqpico against the picoquic in pq when the sources are newer."""
    srcs = [os.path.join(MOQ_DIR, f) for f in ("moqt.c", "moqt.h", "moqpico.c", "Makefile")]
    if os.path.exists(BIN) and all(os.path.getmtime(BIN) >= os.path.getmtime(s) for s in srcs):
        return
    print("*** building the MoQ layer (moq/moqpico) against", pq)
    r = sh(f"make -C '{MOQ_DIR}' clean >/dev/null; make -C '{MOQ_DIR}' PICOQUIC='{pq}'")
    if r.returncode != 0 or not os.path.exists(BIN):
        print(r.stdout[-3000:], r.stderr[-3000:])
        raise SystemExit("building moqpico failed (see above)")
    user = os.environ.get("SUDO_USER")
    if user:                                     # keep the files owned by the normal user
        sh(f"chown -R {user}: '{MOQ_DIR}'")


def frames_of(ts):
    """Frames in decode (send) order: type, size, pts, TS position (same as iframe_test.frame_list)."""
    pk = json.loads(sh(f"ffprobe -v error -select_streams v:0 -show_entries packet=pts,dts,pos,size "
                       f"-of json '{ts}'").stdout or "{}").get("packets", [])
    fr = json.loads(sh(f"ffprobe -v error -select_streams v:0 -show_entries frame=pts,pict_type "
                       f"-of json '{ts}'").stdout or "{}").get("frames", [])
    ptype = {int(f["pts"]): f.get("pict_type", "?")[:1] for f in fr if "pts" in f}
    out = []
    for p in pk:
        if "pos" in p and "pts" in p:
            out.append({"pos": int(p["pos"]), "size": int(p["size"]), "pts": int(p["pts"]),
                        "type": ptype.get(int(p["pts"]), "?"), "packets": []})
    return out


def make_units(ts, frames, out, fps=30.0):
    """units.csv for the publisher: one line per frame = the bytes of the .ts from this frame's first
    TS packet up to the next frame (PAT/PMT packets go with the frame before them; the first frame
    starts at byte 0). A frame is sent at (its number in decode order) / fps seconds."""
    size = os.path.getsize(ts)
    fs = sorted(frames, key=lambda f: f["pos"])
    with open(out, "w") as fh:
        fh.write("frame,type,send_s,offset,length\n")
        for k, f in enumerate(fs):
            a = 0 if k == 0 else f["pos"]
            b = fs[k + 1]["pos"] if k + 1 < len(fs) else size
            t = f["type"] if f["type"] in ("I", "P", "B") else "P"
            fh.write(f"{k},{t},{k / fps:.6f},{a},{b - a}\n")
    return len(fs)


def write_path(path_file, ip, pb):
    tmp = path_file + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(f"{ip} {pb}\n")
    os.replace(tmp, path_file)


def read_objects(path):
    """objects.csv of the subscriber -> per (path, kind): objects, bytes; I-frame delays."""
    per, d_i = {}, []
    try:
        with open(path) as fh:
            for row in csv.DictReader(fh):
                k = (row["path"], row["kind"])
                n, b = per.get(k, (0, 0))
                per[k] = (n + 1, b + int(row["length"] or 0))
                if row["type"] == "I" and row["delay_ms"]:
                    d_i.append(float(row["delay_ms"]))
    except (OSError, KeyError, ValueError):
        pass
    return per, d_i


# ---------------------------------------------------------------------------
#  Self-test on this machine (no Mininet): 127.0.0.1 = "wifi", 127.0.0.2 = "5g"
# ---------------------------------------------------------------------------
def selftest(args):
    pq = find_picoquic(args.picoquic)
    build_moqpico(pq)
    d = os.path.abspath(os.path.join(args.out, "selftest"))
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    secs = 12
    print(f"*** encoding a {secs} s test video")
    r = sh(f"ffmpeg -y -loglevel error -f lavfi -i testsrc2=size=1280x720:rate=30 -t {secs} -an "
           f"-c:v libx264 -preset veryfast -b:v 3M -maxrate 3M -bufsize 3M -g 30 -keyint_min 30 -sc_threshold 0 "
           f"-bf 3 -pix_fmt yuv420p -f mpegts '{d}/video.ts'")
    if r.returncode != 0:
        raise SystemExit("ffmpeg failed: " + r.stderr[-500:])
    frames = frames_of(f"{d}/video.ts")
    n = make_units(f"{d}/video.ts", frames, f"{d}/units.csv")
    print(f"    {n} frames, {os.path.getsize(d + '/video.ts') / 1e6:.1f} MB")
    write_path(f"{d}/path.txt", "5g", "wifi")
    pub = subprocess.Popen(
        [BIN, "pub", "--ts", f"{d}/video.ts", "--units", f"{d}/units.csv", "--port", str(PORT),
         "--cert", f"{pq}/certs/cert.pem", "--key", f"{pq}/certs/key.pem",
         "--label", "wifi=127.0.0.1", "--label", "5g=127.0.0.2", "--wait-subs", "2", "--start-delay", "0.5",
         "--path-file", f"{d}/path.txt", "--started-file", f"{d}/started.txt", "--log", f"{d}/sent.csv"],
        stdout=open(f"{d}/pub.log", "w"), stderr=subprocess.STDOUT)
    time.sleep(0.5)
    sub = subprocess.Popen(
        [BIN, "sub", "--connect", f"wifi=127.0.0.1:{PORT}", "--connect", f"5g=127.0.0.2:{PORT}",
         "--out", f"{d}/received.ts", "--pieces", f"{d}/pieces.csv", "--log", f"{d}/objects.csv",
         "--duration", str(secs + 30)], stdout=open(f"{d}/sub.log", "w"), stderr=subprocess.STDOUT)
    t_end = time.time() + 15
    while not os.path.exists(f"{d}/started.txt") and time.time() < t_end:
        time.sleep(0.05)
    if not os.path.exists(f"{d}/started.txt"):
        pub.kill()
        sub.kill()
        print(open(f"{d}/pub.log").read(), open(f"{d}/sub.log").read())
        raise SystemExit("the MoQ sessions did not start")
    t0 = float(open(f"{d}/started.txt").read())
    print("*** video started; at 6 s the controller moves the I frames to Wi-Fi and P/B to 5G")
    while time.time() - t0 < 6:
        time.sleep(0.05)
    write_path(f"{d}/path.txt", "wifi", "5g")
    sub.wait(timeout=secs + 60)
    pub.wait(timeout=30)
    print("\n--- publisher (sta1 side)\n" + open(f"{d}/pub.log").read())
    print("--- subscriber (srv side)\n" + open(f"{d}/sub.log").read())
    a = open(f"{d}/video.ts", "rb").read()
    try:
        b = open(f"{d}/received.ts", "rb").read()
    except OSError:
        b = b""
    same = sum(1 for k in range(0, len(a), TSP) if a[k:k + TSP] == b[k:k + TSP])
    total = (len(a) + TSP - 1) // TSP
    rows = list(csv.DictReader(open(f"{d}/objects.csv")))
    print("--- where the objects travelled  (path, kind, frame type: objects)")
    for half, sel in (("first 6 s ", lambda t: t < 6), ("after 6 s ", lambda t: t >= 6)):
        cnt = {}
        for x in rows:
            if sel((int(x["arrival_us"]) / 1e6) - t0):
                k = (x["path"], x["kind"], x["type"])
                cnt[k] = cnt.get(k, 0) + 1
        print("   ", half, ", ".join(f"{p} {kd} {ty}: {c}" for (p, kd, ty), c in sorted(cnt.items())))
    dl = [float(x["delay_ms"]) for x in rows if x["delay_ms"]]
    print(f"--- {same}/{total} TS packets received identical; object delay avg "
          f"{sum(dl) / max(1, len(dl)):.2f} ms, max {max(dl) if dl else 0:.2f} ms")
    ok = same == total
    print("*** SELF-TEST " + ("PASSED: the video went over MoQ (draft-16) on picoquic, I frames on the "
                              "stream of one path, P/B as datagrams on the other, and the switch worked"
                              if ok else "FAILED - see the logs in " + d))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
#  One Mininet run (one scenario, one variant) - same timeline and decisions as Part 8
# ---------------------------------------------------------------------------
def run(net, scen, variant, pq, d, A, args):
    from mininet.log import info
    import congestion_test as ct
    import iframe_test as it
    import long_video_test as lv
    import adaptive_test as at
    import players_test as pt
    import video_stream as vs
    PATHS = ct.PATHS
    sta1, srv = net.get("sta1"), net.get("srv")
    sub = os.path.join(d, variant)
    shutil.rmtree(sub, ignore_errors=True)
    os.makedirs(sub)
    length = A["secs"]
    phases = ct.phase_table(scen, length)
    W = args.window
    nch = max(1, int(round(length / W)))
    bounds = [(k * W, length if k == nch - 1 else (k + 1) * W) for k in range(nch)]
    info(f"\n========== MoQ - {ct.SCEN[scen]['title']} - {length / 60:.1f} min - "
         + ("adaptive: measures every %g s, I objects on the better path's subgroup stream, "
            "P/B objects as datagrams on the worse path" % W if variant == "adaptive"
            else "no switching: I objects on the 5G stream, P/B datagrams on Wi-Fi") + " ==========\n")

    # ---- probes and background traffic (as Part 8)
    logs = {}
    for n in ("wifi", "5g"):
        f = f"{sub}/probe_{n}.log"
        srv.cmd(f"python3 {HERE}/probe.py recv {PATHS[n]['server_ip']} {ct.PROBE_PORT[n]} {f} > /dev/null 2>&1 &")
        logs[n] = ct.ProbeLog(f, args.probe_pps)
    time.sleep(0.5)
    for n in ("wifi", "5g"):
        sta1.cmd(f"python3 {HERE}/probe.py send {PATHS[n]['server_ip']} {ct.PROBE_PORT[n]} {args.probe_pps} "
                 f"{int(length) + 180} > /dev/null 2>&1 &")
    bg = ct.Background(net, args.bg_unit)
    applied = {}

    def set_cond(t):
        out = {}
        for n in ("wifi", "5g"):
            b_, dl, ls = ct.conditions(t, phases, n, args)
            if applied.get(n, (None,))[1:] != (round(dl, 1), round(ls, 2)):
                vs.shape(net, n, ct.BASE[n]["bw"], dl, ls)
            bg.set(n, b_)
            applied[n] = (bg.level(n), round(dl, 1), round(ls, 2))
            out[n] = applied[n]
        return out

    cond = set_cond(0.0)
    time.sleep(3.0)

    # ---- the MoQ publisher (sta1) and subscriber (srv)
    pathf, started = f"{sub}/path.txt", f"{sub}/started.txt"
    write_path(pathf, ct.MAIN, ct.other(ct.MAIN))
    cc = f"--cc {args.cc} " if args.cc else ""
    sta1.cmd(f"{BIN} pub --ts {A['ts']} --units {A['units']} --port {PORT} --cert {pq}/certs/cert.pem "
             f"--key {pq}/certs/key.pem --label wifi={PATHS['wifi']['client_ip']} "
             f"--label 5g={PATHS['5g']['client_ip']} --wait-subs 2 --start-delay 1 --path-file {pathf} "
             f"--started-file {started} --log {sub}/sent.csv --dgram-age {args.dgram_age} --stream-age {args.stream_age} --cc-floor {args.cc_floor} --linger 4 {cc}"
             f"> {sub}/pub.log 2>&1 &")
    time.sleep(1.0)
    srv.cmd(f"({BIN} sub --connect wifi={PATHS['wifi']['client_ip']}:{PORT} --connect 5g={PATHS['5g']['client_ip']}:{PORT} "
            f"--out {sub}/received.ts --pieces {sub}/pieces.csv --log {sub}/objects.csv --duration {int(length) + 120} "
            f"> {sub}/sub.log 2>&1; touch {sub}/sub.done) &")
    t_wait = time.time() + 40
    while not os.path.exists(started) and time.time() < t_wait:
        time.sleep(0.05)
    if not os.path.exists(started):
        info(open(f"{sub}/pub.log").read() + open(f"{sub}/sub.log").read())
        raise SystemExit("the MoQ sessions did not start (logs above)")
    t0 = float(open(started).read().strip())
    info(f"*** MoQ: both sessions subscribed, video started\n")

    chunks = [None] * nch
    windows = []
    hist = {"wifi": [], "5g": []}
    state_prev = {"wifi": "good", "5g": "good"}
    ip_prev = ct.MAIN
    memo = {}
    last_print = -60
    k, next_cond = 0, 1.0
    while k < nch or next_cond < length:
        now = time.time() - t0
        if k < nch and now >= bounds[k][0]:
            at_ = bounds[k][0]
            m = {}
            for n in ("wifi", "5g"):
                logs[n].poll()
                dl, ls, npr = logs[n].window(t0 + at_ - W - 0.5, t0 + at_ - 0.5)
                if at_ < W:
                    m[n] = {"d": None, "l": 0.0, "state": "good", "probes": 0}
                    continue
                hist[n].append((dl, ls))
                ds_, ls_ = ct.smooth(hist[n])
                m[n] = {"d": dl, "l": ls, "ds": ds_, "ls": ls_, "state": ct.classify(hist[n], state_prev[n], args),
                        "probes": npr}
            ip = ct.choose(ip_prev, m, args, memo) if variant == "adaptive" else ct.MAIN
            pb = ct.other(ip)
            if variant == "adaptive":
                write_path(pathf, ip, pb)
            chunks[k] = (bounds[k][0], bounds[k][1], ip, pb)
            windows.append({"t": at_, "ip": ip, "pb": pb,
                            **{f"{n}_{q}": m[n][q] for n in m for q in ("d", "l", "state")},
                            "bg_wifi": cond["wifi"][0], "bg_5g": cond["5g"][0],
                            "set_delay_wifi": cond["wifi"][1], "set_delay_5g": cond["5g"][1],
                            "set_loss_wifi": cond["wifi"][2], "set_loss_5g": cond["5g"][2]})
            ts_ = f"{int(at_ // 60):2d}:{int(at_ % 60):02d}"
            for n in ("5g", "wifi"):
                if m[n]["state"] != state_prev[n]:
                    info(f"*** {ts_}  {ct.LABEL[n]} is now {m[n]['state'].upper()} ({ct.mtxt(m[n])})\n")
                    state_prev[n] = m[n]["state"]
            if ip != ip_prev:
                info(f"*** {ts_}  SWITCH -> I objects on the {ct.LABEL[ip]} MoQ stream, P/B datagrams on "
                     f"{ct.LABEL[pb]}  [5G {ct.mtxt(m['5g'])} | Wi-Fi {ct.mtxt(m['wifi'])}]\n")
            ip_prev = ip
            if at_ - last_print >= 30:
                info(f"*** {ts_}  5G [{ct.mtxt(m['5g'])}, bg {cond['5g'][0]:.0f} %]  Wi-Fi [{ct.mtxt(m['wifi'])}, "
                     f"bg {cond['wifi'][0]:.0f} %]  -> I on {ct.LABEL[ip]}, P/B on {ct.LABEL[pb]}\n")
                last_print = at_
            k += 1
            continue
        if next_cond < length and now >= next_cond:
            cond = set_cond(next_cond)
            next_cond += 1.0
            continue
        time.sleep(0.01)

    t_wait = time.time() + 60
    while not os.path.exists(f"{sub}/sub.done") and time.time() < t_wait:
        time.sleep(0.5)
    time.sleep(1)
    sta1.cmd("pkill -9 -x moqpico ; pkill -9 -f probe.py")
    srv.cmd("pkill -9 -x moqpico ; pkill -9 -f probe.py")
    bg.stop_all()
    for n in ("wifi", "5g"):
        vs.shape(net, n, ct.BASE[n]["bw"], ct.BASE[n]["delay"], ct.BASE[n]["loss"])
    info("    " + "".join(l for l in open(f"{sub}/pub.log") if l.startswith("*** END")))
    info("    " + "".join(l for l in open(f"{sub}/sub.log") if l.startswith("*** END")))

    # ---- what the viewer got
    cov, st = it.rebuild([(f"{sub}/received.ts", f"{sub}/pieces.csv", None)], os.path.getsize(A["ts"]),
                         args.buffer, f"{sub}/live.ts")
    bad_pk, got_pk = lv.check_data(f"{sub}/live.ts", A["ts"], cov)
    if bad_pk:
        info(f"    !!! WARNING: {bad_pk} of {got_pk} received TS packets are not the packets that were sent\n")
    try:
        os.remove(f"{sub}/received.ts")
    except OSError:
        pass
    per, delays_i = read_objects(f"{sub}/objects.csv")
    frames = A["frames"]
    dep = it.dependency(frames, cov)
    flags = at.shown_flags(frames, cov)
    p0 = min(f["pts"] for f in frames)
    ftime = [(f["pts"] - p0) / 90000.0 for f in frames]
    alld = sorted(st[0]["delays"]) if st else []
    sw = [c[0] for p, c in zip(chunks, chunks[1:]) if p and c and p[2] != c[2]]
    mb = {n: sum(b for (p, _), (_, b) in per.items() if p == n) / 1e6 for n in ("wifi", "5g")}
    r = {"scenario": scen, "variant": variant, "dep": dep, "frames": len(frames), "switches": len(sw),
         "switch_times": sw, "delay_avg": pt.mean(alld), "delay_p95": pt.pctl(alld, 95) if alld else None,
         "delay_i": pt.mean(delays_i),
         "late_pct": 100.0 * sum(1 for x in alld if x > args.buffer) / len(alld) if alld else None,
         "mb_wifi": mb["wifi"], "mb_5g": mb["5g"], "bad_packets": bad_pk, "length": length,
         "chunks": [c for c in chunks if c], "windows": windows,
         "objects": {f"{p}_{kd}": n for (p, kd), (n, _) in per.items()}}
    fidx = [[] for _ in range(nch)]
    for j, tt in enumerate(ftime):
        fidx[min(nch - 1, int(tt // W))].append(j)

    def summary(a, b):
        ks = [q for q, c in enumerate(chunks) if c and a <= c[0] < b]
        idx = [j for q in ks for j in fidx[q]]
        t5 = sum(chunks[q][1] - chunks[q][0] for q in ks if chunks[q][2] == "5g")
        tw = sum(chunks[q][1] - chunks[q][0] for q in ks if chunks[q][2] == "wifi")
        ii = [j for j in idx if frames[j]["type"] == "I"]
        ws = [w for w in windows if a <= w["t"] < b]

        def avg(key):
            v = [w[key] for w in ws if w[key] is not None]
            return sum(v) / len(v) if v else None

        def most(key):
            v = [w[key] for w in ws]
            return max(set(v), key=v.count) if v else ""
        return {"i_path": "+".join(f"{ct.LABEL[p]}" + (f" {tm:.0f}s" if 0 < tm < (b - a) - 0.5 and t5 and tw else "")
                                   for p, tm in (("5g", t5), ("wifi", tw)) if tm > 0),
                "i_5g_s": t5, "i_wifi_s": tw, "shown": sum(flags[j] for j in idx), "frames": len(idx),
                "i_ok": sum(1 for j in ii if all(cov[q] >= TSP for q in frames[j]["packets"])), "i_total": len(ii),
                "d5": avg("5g_d"), "l5": avg("5g_l"), "dw": avg("wifi_d"), "lw": avg("wifi_l"),
                "s5": most("5g_state"), "sw": most("wifi_state"), "bg5": avg("bg_5g"), "bgw": avg("bg_wifi")}

    r["phases"] = [{"phase": q + 1, "start": a, "end": b, "name": nm, **summary(a, b)}
                   for q, (a, b, nm, _) in enumerate(phases)]
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
    info(f"    data check: {got_pk - bad_pk}/{got_pk} received packets correct; MoQ objects: "
         + ", ".join(f"{k} {v}" for k, v in sorted(r["objects"].items())) + "\n")
    info(f"    {len(sw)} switches; I {I[0]}/{I[1]}, P {P[0]}/{P[1]}, B {B[0]}/{B[1]}, GOPs lost {dep['gops_lost']}; "
         f"frames shown {dep['shown']}/{len(frames)}; PSNR {r['psnr'] or '-'} dB; "
         f"delay avg {vs.f1(r['delay_avg'], ' ms')}\n")
    return r


def save_objects(od, runs):
    keys = sorted({k for r in runs for k in r.get("objects", {})})
    with open(f"{od}/moq_objects.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["variant"] + keys)
        for r in runs:
            w.writerow([r["variant"]] + [r.get("objects", {}).get(k, 0) for k in keys])


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Part 9: the video over MoQ (MOQT draft-16) on picoquic, "
                                             "with the Part 8 measured path switching")
    ap.add_argument("--selftest", action="store_true", help="MoQ test on this machine without Mininet (1 min)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--video", default=None)
    ap.add_argument("--scenarios", default="congested,lossy,degraded")
    ap.add_argument("--variants", default="fixed,adaptive")
    ap.add_argument("--length", type=float, default=840.0, help="video length in s (default 840 = 14 min)")
    ap.add_argument("--segment", type=float, default=20.0)
    ap.add_argument("--window", type=float, default=5.0)
    ap.add_argument("--td", type=float, default=50.0)
    ap.add_argument("--tl", type=float, default=5.0)
    ap.add_argument("--deg-span", type=float, default=30.0)
    ap.add_argument("--deg-dd", type=float, default=0.1)
    ap.add_argument("--deg-dl", type=float, default=0.01)
    ap.add_argument("--bg-low", type=float, default=10.0)
    ap.add_argument("--bg-high", type=float, default=90.0)
    ap.add_argument("--bg-unit", type=float, default=10.0)
    ap.add_argument("--ramp", type=float, default=20.0)
    ap.add_argument("--lossy-pct", type=float, default=8.0)
    ap.add_argument("--deg-delay", type=float, default=60.0)
    ap.add_argument("--deg-loss", type=float, default=6.0)
    ap.add_argument("--probe-pps", type=float, default=200.0)
    ap.add_argument("--buffer", type=float, default=150.0)
    ap.add_argument("--dgram-age", type=int, default=1000, help="drop P/B datagrams waiting longer than ... ms")
    ap.add_argument("--stream-age", type=int, default=500,
                    help="give up (reset) an I-frame group stream not delivered after ... ms (0 = never)")
    ap.add_argument("--cc-floor", type=int, default=64, help="restart the I-frame session's congestion control when its window stays below ... KB on an unqueued path (0 = off)")
    ap.add_argument("--cc", default=None, help="picoquic congestion control: bbr (default, as picoquicdemo), bbr1, cubic, newreno")
    ap.add_argument("--picoquic", default=None)
    ap.add_argument("--no-video", action="store_true", help="skip the VLC videos")
    ap.add_argument("--out", default="moq_results")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    outdir = os.path.abspath(args.out)
    if args.selftest:
        sys.exit(selftest(args))

    import congestion_test as ct
    ct.PART_NAME = "Part 9 (MoQ on picoquic)"
    ct.FILE_PREFIX = "moq"
    scens = [s.strip() for s in args.scenarios.split(",") if s.strip() in ct.SCEN]
    if args.show:
        return ct.show(outdir, scens)

    from mininet.log import setLogLevel, info
    import netconfig
    import wifi_5g_topology as topo
    import video_stream as vs
    import iframe_test as it
    import long_video_test as lv
    cfg = netconfig.load(args.config)
    topo.apply_config(cfg)
    for n in ("wifi", "5g"):
        ct.PATHS[n]["bw"] = ct.BASE[n]["bw"]
        ct.PATHS[n]["delay"] = f"{ct.BASE[n]['delay']:g}ms"
        ct.PATHS[n]["loss"] = ct.BASE[n]["loss"]
    vc = vs.video_cfg(args.config)
    src = args.video if args.video is not None else vc["file"]
    variants = [v.strip() for v in args.variants.split(",") if v.strip() in ("fixed", "adaptive")]
    setLogLevel("info")
    os.makedirs(outdir, exist_ok=True)
    pq = find_picoquic(args.picoquic)
    build_moqpico(pq)
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
    units = f"{vd}/units.csv"
    nu = make_units(ts, frames, units)
    secs = float(vs.sh(f"ffprobe -v error -show_entries format=duration -of csv=p=0 '{ts}'").stdout.strip()
                 or args.length)
    info(f"    {nu} frames -> MoQ: 1 group per GOP, {os.path.getsize(ts) / 1e6:.0f} MB in objects of 940 bytes\n")
    A = {"ts": ts, "frames": frames, "owner": owner, "original": f"{vd}/original.mp4", "secs": secs, "units": units}
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = topo.build_topology()
    net.get("srv").cmd("for i in srv-wifi srv-5g; do ethtool -K $i gro off 2>/dev/null; done")
    for n in ("wifi", "5g"):
        vs.shape(net, n, ct.BASE[n]["bw"], ct.BASE[n]["delay"], ct.BASE[n]["loss"])
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
            info(f"\n################ MoQ SCENARIO: {ct.SCEN[scen]['title']} ################\n")
            for v in variants:
                r = run(net, scen, v, pq, d, A, args)
                results.setdefault(scen, []).append(r)
                ct.save(od, results[scen], args)
                save_objects(od, results[scen])
    finally:
        info("*** Stopping network\n")
        net.get("sta1").cmd("pkill -9 -x moqpico")
        net.get("srv").cmd("pkill -9 -x moqpico")
        net.stop()
    made = []
    for scen, runs in results.items():
        od = os.path.join(outdir, scen)
        g = ct.graph(od, scen, runs, args)
        if g:
            shutil.copy(g, os.path.join(outdir, os.path.basename(g)))
            made.append(os.path.join(outdir, os.path.basename(g)))
        if not args.no_video:
            info(f"*** {scen}: making the VLC video (10-20 min) ...\n")
            v = ct.video(od, scen, runs, args)
            if v:
                os.replace(v, os.path.join(outdir, os.path.basename(v)))
                made.append(os.path.join(outdir, os.path.basename(v)))
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    ct.show(outdir, scens)
    for x in made:
        print("Made:", x)
    print(f"\nOn the Mac:\n  scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/{args.out}/moq_*' ~/Downloads/")


if __name__ == "__main__":
    main()
