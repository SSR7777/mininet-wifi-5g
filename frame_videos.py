#!/usr/bin/env python3
"""
Part 5 videos for VLC that show the FRAMES (I / P / B).

Every tile shows the frame number and the frame type (I, P or B) of the picture on screen,
so you can watch what happens to the video when a frame is lost.

  frames_lost_one_<codec>.mp4   2 x 2:  Original | one I frame lost | one P frame lost | one B frame lost
                                -> a lost I frame breaks the whole GOP until the next I frame,
                                   a lost P frame the rest of the GOP, a lost B frame (H.264) only itself
  frames_<case>_<codec>.mp4     with --only <case>: Original next to one case, e.g. --only combo
                                (I frames on a 5G QUIC stream, P/B frames on Wi-Fi datagrams)
  frames_<case>_h264_hevc.mp4   the same, H.264 (top row) and HEVC (bottom row) side by side
  frames_cases_<codec>.mp4      Original + every case of iframe_test.py (what the viewer saw),
                                4 tiles per row; a frozen frame number = frames that could not be shown

Needs the results of:   sudo python3 iframe_test.py --video BBB.webm
Run:                    python3 frame_videos.py                 (both codecs)
                        python3 frame_videos.py --codecs h264
Watch on the Mac:       scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/iframe_results/frames_*.mp4' ~/Downloads/
                        open -a VLC ~/Downloads/frames_lost_one_h264.mp4
"""
import argparse
import os
import shutil
import subprocess
import tempfile

CODEC = {"h264": "H.264", "hevc": "HEVC"}
CASES = [("wifi", "Wi-Fi, no frame awareness"), ("wifi_combo", "Wi-Fi: I stream + P/B dgram"),
         ("5g", "5G, no frame awareness"), ("5g_combo", "5G: I stream + P/B dgram"),
         ("ref", "Wi-Fi+5G, no frame awareness"), ("fixed", "(1) I->5G, P/B->Wi-Fi"),
         ("combo", "(3) I->5G stream, P/B->Wi-Fi"), ("tiered", "(5) I 5G stream, P 5G, B Wi-Fi")]
TSP = 188
HOLD = 0.4                    # seconds an I-frame highlight stays on screen


def font():
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf"):
        if os.path.exists(f):
            return f
    r = subprocess.run("fc-match -f '%{file}' sans:bold", shell=True, capture_output=True, text=True)
    return r.stdout.strip()


def tile(k, label, w, h, fnt, fps=30, itimes=()):
    """Filter for input k: label on top, frame number + frame type (I/P/B) at the bottom.
    The text is drawn on the decoded frames BEFORE fps duplicates frames, so a frame that
    could not be decoded shows up as a frozen number.
    An I frame is only 1 frame in 30 (1/30 s on screen), so at every I frame a red border and
    an "I FRAME" banner stay on screen for HOLD seconds, so it can be seen at normal speed."""
    lab = label.replace(":", "\\:").replace("'", "").replace(",", "\\,")
    fs = max(14, h // 16)
    big = max(18, h // 9)
    t = (f"[{k}:v]setpts=PTS-STARTPTS,scale={w}:{h},"
         f"drawtext=fontfile='{fnt}':text='{lab}':x=6:y=6:fontsize={fs}:fontcolor=white:"
         f"box=1:boxcolor=black@0.65:boxborderw=4,"
         f"drawtext=fontfile='{fnt}':text='frame %{{eif\\:t*{fps}\\:d}}   %{{pict_type}}':"
         f"x=6:y=h-th-8:fontsize={big}:fontcolor=yellow:box=1:boxcolor=black@0.7:boxborderw=6,"
         f"fps={fps}")
    itimes = list(itimes)
    gaps = [b - a for a, b in zip(itimes, itimes[1:])]
    if len(itimes) > 3 and itimes[0] < 0.05 and max(gaps) - min(gaps) < 0.01:
        # I frames at a fixed interval (one per GOP): one expression instead of one filter per I frame
        p = sum(gaps) / len(gaps)
        en = f"lt(mod(t+0.001\\,{p:.4f})\\,{HOLD})"
        t += (f",drawbox=x=0:y=0:w=iw:h=ih:color=red:t={max(4, h // 60)}:enable='{en}'"
              f",drawtext=fontfile='{fnt}':text='I FRAME  (GOP %{{eif\\:floor((t+0.001)/{p:.4f})+1\\:d}})':"
              f"x=w-tw-12:y={fs * 2 + 14}:fontsize={int(fs * 1.3)}:fontcolor=white:box=1:boxcolor=red@0.85:"
              f"boxborderw=6:enable='{en}'")
        return t + f"[t{k}];"
    for g, ti in enumerate(itimes, 1):                      # highlight every I frame (start of a GOP)
        en = f"between(t,{ti:.3f},{ti + HOLD:.3f})"
        t += (f",drawbox=x=0:y=0:w=iw:h=ih:color=red:t={max(4, h // 60)}:enable='{en}'"
              f",drawtext=fontfile='{fnt}':text='I FRAME  (GOP {g})':x=w-tw-12:y={fs * 2 + 14}:"
              f"fontsize={int(fs * 1.3)}:fontcolor=white:box=1:boxcolor=red@0.85:boxborderw=6:enable='{en}'")
    return t + f"[t{k}];"


def i_times(ts):
    """Times (s from the first picture) of the I frames of the original video."""
    import iframe_test as it
    fr = it.frame_list(ts)
    if not fr:
        return []
    p0 = min(f["pts"] for f in fr)
    return sorted((f["pts"] - p0) / 90000.0 for f in fr if f["type"] == "I")


def grid(inputs, out, w, h, per_row, fnt, itimes=()):
    """inputs = [(label, file)] -> one video, per_row tiles per row. itimes: one list per tile
    (or one list for all tiles) of I-frame times to highlight."""
    keep = [k for k, (lab, f) in enumerate(inputs) if os.path.exists(f) and os.path.getsize(f) > 0]
    inputs = [inputs[k] for k in keep]
    if itimes and isinstance(itimes[0], (list, tuple)):
        itimes = [itimes[k] for k in keep]
    else:
        itimes = [itimes] * len(inputs)
    if len(inputs) < 2:
        return None
    fc = "".join(tile(k, lab, w, h, fnt, itimes=itimes[k]) for k, (lab, _) in enumerate(inputs))
    lay = "|".join(f"{(k % per_row) * w}_{(k // per_row) * h}" for k in range(len(inputs)))
    fc += "".join(f"[t{k}]" for k in range(len(inputs))) + f"xstack=inputs={len(inputs)}:layout={lay}:fill=black"
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for _, f in inputs:
        cmd += ["-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err"] + \
               (["-f", "mpegts"] if f.endswith(".ts") else []) + ["-i", f]
    script = out + ".filter.txt"                  # the filter can be long: give it to ffmpeg as a file
    with open(script, "w") as fh:
        fh.write(fc)
    cmd += ["-filter_complex_script", script, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        os.remove(script)
    except OSError:
        pass
    if r.returncode != 0:
        print("ffmpeg failed:", r.stderr[-800:])
        return None
    return out


def lost_one_files(d, tmp):
    """Make video.ts with one I, one P and one B frame lost (as in the frame analysis)."""
    import iframe_test as it
    ts = os.path.join(d, "video.ts")
    frames = it.frame_list(ts)
    it.split_ts(ts, frames, os.path.join(tmp, "part_I.ts"), os.path.join(tmp, "part_PB.ts"))
    data = open(ts, "rb").read()
    ipos = [k for k, f in enumerate(frames) if f["type"] == "I"]
    g = ipos[len(ipos) // 2] if ipos else 0
    nxt = next((k for k in ipos if k > g), len(frames))
    pick = {"I": g,
            "P": next((k for k in range(g + 1, nxt) if frames[k]["type"] == "P"), None),
            "B": next((k for k in range(g + 1, nxt) if frames[k]["type"] == "B"), None)}
    out = []
    for t, k in pick.items():
        if k is None:
            continue
        buf = bytearray(data)
        for pkt in frames[k]["packets"]:
            buf[pkt * TSP:(pkt + 1) * TSP] = b"\xff" * TSP
        f = os.path.join(tmp, f"lost_{t}.ts")
        open(f, "wb").write(buf)
        shown = round(frames[k]["pts"] / 3000.0) - round(frames[0]["pts"] / 3000.0)   # 90 kHz / 30 fps
        out.append((f"one {t} frame lost (frame {max(0, shown)})", f))
    return out


def main():
    ap = argparse.ArgumentParser(description="Part 5 videos that show the I/P/B frames")
    ap.add_argument("--dir", default="iframe_results")
    ap.add_argument("--codecs", default="h264,hevc")
    ap.add_argument("--size", default="640x360", help="tile size (default 640x360)")
    ap.add_argument("--only", default=None,
                    help="only Original + these cases, side by side, e.g. --only combo "
                         "(I frames on a 5G stream, P/B frames on Wi-Fi datagrams); cases: "
                         + ", ".join(s for s, _ in CASES))
    a = ap.parse_args()
    w, h = (int(x) for x in a.size.split("x"))
    fnt = font()
    made = []
    both = []                    # --only: the tiles of every codec, for one video with all codecs
    itimes_all = []              # I-frame times of every tile in "both"
    for c in [x.strip() for x in a.codecs.split(",") if x.strip() in CODEC]:
        d = os.path.join(a.dir, c)
        ts = os.path.join(d, "video.ts")
        if not os.path.exists(ts):
            print(f"{CODEC[c]}: no results in {d}/ - run: sudo python3 iframe_test.py --video BBB.webm")
            continue
        if a.only:
            pick = [x.strip() for x in a.only.split(",")]
            names = dict(CASES)
            ins = [(f"Original {CODEC[c]}", ts)] + [(f"{CODEC[c]} {names.get(s, s)}", os.path.join(d, s, "live.ts"))
                                                    for s in pick]
            both.append(ins)
            missing = [s for s in pick if not os.path.exists(os.path.join(d, s, "live.ts"))]
            if missing:
                print(f"{CODEC[c]}: no result for {', '.join(missing)} - run: "
                      f"sudo python3 iframe_test.py --video BBB.webm --scenarios {','.join(missing)}")
            it_c = i_times(ts)
            itimes_all.append([it_c] * len(ins))
            out = grid(ins, os.path.join(a.dir, f"frames_{'_'.join(pick)}_{c}.mp4"), w, h, len(ins), fnt, it_c)
            made += [out] if out else []
            continue
        tmp = tempfile.mkdtemp(prefix="frames_")
        try:
            print(f"*** {CODEC[c]}: video with one I, one P and one B frame lost ...")
            ins = [(f"Original {CODEC[c]}", ts)] + lost_one_files(d, tmp)
            out = grid(ins, os.path.join(a.dir, f"frames_lost_one_{c}.mp4"), w, h, 2, fnt, i_times(ts))
            made += [out] if out else []
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print(f"*** {CODEC[c]}: every case, with frame numbers and types ...")
        ins = [(f"Original {CODEC[c]}", ts)] + [(lab, os.path.join(d, s, "live.ts")) for s, lab in CASES]
        out = grid(ins, os.path.join(a.dir, f"frames_cases_{c}.mp4"), w * 3 // 4, h * 3 // 4, 3, fnt, i_times(ts))
        made += [out] if out else []
    if a.only and len(both) > 1:                 # H.264 and HEVC side by side: one row per codec
        print("*** H.264 and HEVC side by side ...")
        pick = [x.strip() for x in a.only.split(",")]
        ins = [t for row in both for t in row]
        out = grid(ins, os.path.join(a.dir, f"frames_{'_'.join(pick)}_h264_hevc.mp4"), w, h, len(both[0]), fnt,
                   [t for row in itimes_all for t in row])
        made += [out] if out else []
    subprocess.run(["chmod", "-R", "a+rwX", a.dir])
    print()
    for m in made:
        print("Made:", os.path.abspath(m))
    if made:
        print("\nOn the Mac:\n  scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/iframe_results/frames_*.mp4' ~/Downloads/\n"
              f"  open -a VLC ~/Downloads/{os.path.basename(made[0])}\n"
              "In VLC: Playback -> Speed -> Slower, or press E to step one frame at a time.")


if __name__ == "__main__":
    main()
