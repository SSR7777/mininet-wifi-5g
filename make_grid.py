#!/usr/bin/env python3
"""Put every live video in ONE grid video, so they can be compared at the same time.

    Columns:  Original | Wi-Fi | 5G | Wi-Fi + 5G
    Rows:     QUIC datagrams (lost packets not resent)
              QUIC streams   (lost packets resent)

Uses the results of:  sudo python3 players_test.py --video BBB.webm --mode video
Usage:  python3 make_grid.py                 -> players_results/livevideo_grid.mp4
        python3 make_grid.py --dir DIR --out FILE --size 480x270
"""
import argparse, os, subprocess

NETS = [("wifi", "Wi-Fi"), ("5g", "5G"), ("multi", "Wi-Fi + 5G")]
MODES = [("dgram", "Datagrams"), ("stream", "Streams")]


def font():
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf"):
        if os.path.exists(f):
            return f
    r = subprocess.run("fc-match -f '%{file}' sans:bold", shell=True, capture_output=True, text=True)
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser(description="All live videos in one grid")
    ap.add_argument("--dir", default="players_results")
    ap.add_argument("--out", default=None)
    ap.add_argument("--size", default="480x270", help="size of each tile (default 480x270)")
    a = ap.parse_args()
    d = a.dir
    out = a.out or os.path.join(d, "livevideo_grid.mp4")
    w, h = (int(x) for x in a.size.split("x"))
    original = os.path.join(d, "original.mp4")
    if not os.path.exists(original):
        raise SystemExit(f"{original} not found - run: sudo python3 players_test.py --video BBB.webm --mode video")

    rows = []
    for m, mlabel in MODES:
        files = [(f"Original", original)] + [
            (f"{lab} - {mlabel}", os.path.join(d, f"livevideo_{m}_{n}", "live.mp4")) for n, lab in NETS]
        if all(os.path.exists(f) for _, f in files[1:]):
            rows.append(files)
        else:
            print(f"(skipping the {mlabel} row: not every network has a result)")
    if not rows:
        raise SystemExit("No live video results - run: sudo python3 players_test.py --video BBB.webm --mode video")

    fnt = font()
    inputs, fc, tiles = [], "", []
    for r, row in enumerate(rows):
        for c, (label, f) in enumerate(row):
            k = len(inputs)
            inputs.append(f)
            txt = label.replace(":", "\\:")
            draw = (f",drawtext=fontfile='{fnt}':text='{txt}':x=8:y=8:fontsize={max(14, h // 14)}:"
                    f"fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=4") if fnt else ""
            fc += f"[{k}:v]setpts=PTS-STARTPTS,fps=30,scale={w}:{h}{draw}[t{k}];"
            tiles.append((f"[t{k}]", c * w, r * h))
    layout = "|".join(f"{x}_{y}" for _, x, y in tiles)
    fc += "".join(t for t, _, _ in tiles) + f"xstack=inputs={len(tiles)}:layout={layout}:shortest=1"
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for f in inputs:
        cmd += ["-i", f]
    cmd += ["-filter_complex", fc, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and fnt:              # no drawtext in this ffmpeg: try without labels
        import re
        cmd[cmd.index("-filter_complex") + 1] = re.sub(r",drawtext=[^\[]*?(?=\[t\d+\];)", "", fc)
        r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("ffmpeg failed:\n" + r.stderr)
    print(f"Grid video: {os.path.abspath(out)}  ({len(rows)} row(s) x 4 columns, "
          f"{4 * w}x{len(rows) * h})")
    print("Columns: Original | Wi-Fi | 5G | Wi-Fi + 5G     Rows: "
          + " / ".join(m for (_, m), _ in zip(MODES, rows)))


if __name__ == "__main__":
    main()
