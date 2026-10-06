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
  video_summary.csv     frames, decode errors, PSNR, SSIM per path
Needs: sudo apt install -y ffmpeg
"""
import argparse
import configparser
import os
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
            "background": get("background", "no").strip().lower() in ("yes", "true", "1")}


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


def stream_one(net, n, video, length, transport, background, bg_share, outdir):
    """Send the video over ONE path (n) only, while the other path stays idle."""
    sta1, srv = net.get("sta1"), net.get("srv")
    p = PATHS[n]
    info(f"\n========== {LABEL[n]} test: capacity {p['bw']:g} Mbit/s, delay {p['delay']}, "
         f"loss {p['loss']:g} % ==========\n")

    if background:
        rate = max(1, int(p["bw"] * bg_share))
        info(f"*** Background UDP traffic on {LABEL[n]}: {rate} Mbit/s "
             f"({bg_share*100:g} % of capacity)\n")
        srv.cmd(f"iperf3 -s -D -p {p['bg_port']}")
        time.sleep(1)
        sta1.cmd(f"iperf3 -c {p['server_ip']} -p {p['bg_port']} -u -b {rate}M "
                 f"-t {int(length) + 15} > /dev/null 2>&1 &")
        time.sleep(2)

    port = VIDEO_PORT[n]
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

    info(f"*** Sender (sta1) streaming the video over {LABEL[n]} only "
         f"({length:.0f}s, real time)\n")
    start = time.time()
    sta1.cmd(f"(ffmpeg -loglevel error -re -i {video} -c copy -f mpegts '{dst}' "
             f"> {outdir}/send_{n}.log 2>&1; date +%s.%N > {outdir}/send_{n}.done) &")
    rx = f"{outdir}/received_{n}.ts"
    while time.time() < start + length + 120:
        el = time.time() - start
        mb = os.path.getsize(rx) / 1e6 if os.path.exists(rx) else 0.0
        info(f"    t={el:5.1f}s  received over {LABEL[n]}: {mb:5.1f} MB\n")
        if os.path.exists(f"{outdir}/send_{n}.done"):
            break
        time.sleep(2)
    done = f"{outdir}/send_{n}.done"
    st = float(open(done).read()) - start if os.path.exists(done) else None
    time.sleep(7 if transport == "udp" else 3)   # let UDP receiver time out
    srv.cmd("pkill -INT ffmpeg; sleep 1; pkill -9 ffmpeg; pkill -9 iperf3")
    sta1.cmd("pkill -9 ffmpeg; pkill -9 iperf3")
    time.sleep(2)
    return st


def run(net, video, transport, background, bg_share, outdir, names):
    length = float(sh(f"ffprobe -v error -show_entries format=duration "
                      f"-of csv=p=0 '{video}'").stdout.strip() or 20)
    send_time = {}
    for n in names:                      # one network at a time
        send_time[n] = stream_one(net, n, video, length, transport,
                                  background, bg_share, outdir)
    return send_time


def analyse(video, send_time, transport, outdir, names):
    info("\n*** Comparing received video with the original (this takes a moment)\n")
    sent_frames = count_frames(video)
    sent_mb = os.path.getsize(video) / 1e6
    rows = ["path,configured_loss_pct,delay_ms,transport,frames_sent,frames_received,"
            "decode_errors,psnr_db,ssim,mb_sent,mb_received,send_time_s"]
    info(f"\n*** VIDEO RESULTS ({transport.upper()})\n")
    info(f"  {'path':6} {'loss':>5} {'delay':>6} {'frames rx/tx':>14} {'errors':>7} "
         f"{'PSNR dB':>8} {'SSIM':>6} {'send time':>10}\n")
    for n in names:
        rx = f"{outdir}/received_{n}.ts"
        if not os.path.exists(rx) or os.path.getsize(rx) == 0:
            info(f"  {LABEL[n]:6} nothing received (see {outdir}/recv_{n}.log)\n")
            continue
        fr = count_frames(rx)
        err = decode_errors(rx)
        psnr, ssim = quality(rx, video)
        st = send_time[n]
        info(f"  {LABEL[n]:6} {PATHS[n]['loss']:4g}% {PATHS[n]['delay']:>6} "
             f"{fr:6d}/{sent_frames:<6d} {err:7d} {psnr:>8} {ssim[:6]:>6} "
             f"{(f'{st:.1f}s' if st else '-'):>10}\n")
        rows.append(f"{n},{PATHS[n]['loss']},{PATHS[n]['delay']},{transport},{sent_frames},"
                    f"{fr},{err},{psnr},{ssim},{sent_mb:.2f},{os.path.getsize(rx)/1e6:.2f},"
                    f"{st if st else ''}")
    with open(f"{outdir}/video_summary.csv", "w") as fh:
        fh.write("\n".join(rows) + "\n")
    info("  frames rx/tx : frames decoded at the receiver / frames sent\n"
         "  errors       : decoder errors caused by missing or damaged packets\n"
         "  PSNR / SSIM  : picture quality vs the original (higher = better;\n"
         "                 PSNR > 40 dB / SSIM > 0.98 looks identical)\n")

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
    background = args.background or vc["background"]
    print(netconfig.summary(cfg) +
          f"  Video: {video_src or 'test pattern'}, {bitrate:g} Mbit/s, {length}s, "
          f"{transport.upper()}, background {'on' if background else 'off'}\n"
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
        send_time = run(net, video, transport, background, cfg["bg_share"], outdir, names)
    finally:
        info("*** Stopping network\n")
        net.stop()
    analyse(video, send_time, transport, outdir, names)
    subprocess.run(["chmod", "-R", "a+rwX", outdir])
    info(f"\n*** All files in {outdir}/\n")


if __name__ == "__main__":
    main()
