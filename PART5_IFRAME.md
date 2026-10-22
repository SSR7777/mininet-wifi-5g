# Part 5 - I-frame aware sending (every step and command)

Branch: `part5-iframe-aware` · Main script: `iframe_test.py` · Tables: `show_results.py --iframe` ·
Every command of the whole project: [COMMANDS.md](COMMANDS.md)

**Idea:** a compressed video has I, P and B frames. If an I frame is lost, every frame that depends on it
is lost too. So the important frames (I) are sent the safe way (**5G, QUIC stream: lost pieces are resent**)
and the rest (P, B) the fast way (**Wi-Fi, QUIC datagrams: not resent**).

```
            5G    ── I frames,   QUIC STREAM   (lost pieces resent) ──►
 Server                                                                  Client
 (sta1)    Wi-Fi  ── P/B frames, QUIC DATAGRAM (not resent, fast) ─────►  (srv)
```

Where each command runs:

| Prompt | Machine |
|---|---|
| `siddhartha@mininet-wifi:~/mininet-wifi-5g$` | Ubuntu VM (emulation) |
| `ssr@Siddharthas-MacBook-Air ~ %` | Mac (copying files, VLC) |

Paste one command at a time.

---

## 0. Before you start (once)

```bash
# VM: picoquic must be able to carry the real video (Part 4)
python3 patch_quicperf_video.py ~/picoquic
(cd ~/picoquic && make -j"$(nproc)" picoquicdemo)

# VM: the video in the project folder (copy from the Mac if needed)
ls BBB.webm
```

---

## Step 1 - Raw video -> encoded (compressed) video

A raw frame is a complete picture (1280x720 = about 2.7 MB; 30 per second = about 660 Mbit/s).
The **encoder** compresses it to 4 Mbit/s by storing only what changes, and in doing so **creates the
I, P and B frames**. Both codecs get the same rules: one I frame every 30 frames (GOP = 1 s), 3 B frames.

What `iframe_test.py` runs (you can also run these by hand in the VM):

```bash
mkdir -p demo && cd demo

# H.264
ffmpeg -y -i ../BBB.webm -t 20 -an -vf scale=1280:720,fps=30 \
  -c:v libx264 -preset veryfast -b:v 4M -maxrate 4M -bufsize 4M \
  -g 30 -keyint_min 30 -sc_threshold 0 -bf 3 -pix_fmt yuv420p h264.mp4

# HEVC (H.265)
ffmpeg -y -i ../BBB.webm -t 20 -an -vf scale=1280:720,fps=30 \
  -c:v libx265 -preset veryfast -b:v 4M -maxrate 4M -bufsize 4M -tag:v hvc1 \
  -x265-params keyint=30:min-keyint=30:scenecut=0:bframes=3:open-gop=0 -pix_fmt yuv420p hevc.mp4
```

## Step 2 - Extracting the frames from the encoded video (H.264 / HEVC)

Pack the video as MPEG-TS (188-byte packets, the live-TV format), then read every frame's type and size:

```bash
ffmpeg -y -i h264.mp4 -c copy -f mpegts h264.ts

# how many I / P / B frames
ffprobe -v error -select_streams v:0 -show_entries frame=pict_type -of csv=p=0 h264.ts | sort | uniq -c

# type and size of the first 40 frames (in the order they are shown)
ffprobe -v error -select_streams v:0 -show_entries frame=pict_type,pkt_size -of csv=p=0 h264.ts | head -40

cd ..
```

The script then gives every 188-byte TS packet to the **I part** or the **P/B part**
(`iframe_results/<codec>/part_I.ts`, `part_PB.ts`), measures what one lost frame does, and prints the analysis:

```bash
sudo python3 iframe_test.py --video BBB.webm --analyse-only     # steps 1 + 2 only, no network
```

## Step 3 - Sending I frames on 5G streams and P/B frames on Wi-Fi datagrams

```bash
cd ~/mininet-wifi-5g
sudo rm -rf iframe_results                                       # run again from scratch (new results)
sudo python3 iframe_test.py --video BBB.webm                      # every case, H.264 + HEVC (~20 min)
sudo python3 iframe_test.py --video BBB.webm --scenarios combo    # only: I -> 5G stream, P/B -> Wi-Fi dgram
sudo python3 iframe_test.py --video BBB.webm --scenarios combo,tiered   # (3) vs (5): I 5G stream, P 5G dgram, B Wi-Fi dgram
sudo python3 iframe_test.py --video BBB.webm --scenarios single   # only Wi-Fi only + 5G only
sudo python3 iframe_test.py --video BBB.webm --scenarios multi    # only the Wi-Fi + 5G cases
sudo python3 iframe_test.py --video BBB.webm --codecs h264        # one codec
python3 show_results.py --iframe                                  # show all tables again
```

Every case (results of cases that are not run again are kept):

| `--scenarios` | Network | I frames | P/B frames |
|---|---|---|---|
| `wifi` | Wi-Fi only | Wi-Fi datagrams | Wi-Fi datagrams |
| `wifi_combo` | Wi-Fi only | Wi-Fi **stream** (resent) | Wi-Fi datagrams |
| `5g` | 5G only | 5G datagrams | 5G datagrams |
| `5g_combo` | 5G only | 5G **stream** (resent) | 5G datagrams |
| `ref` | Wi-Fi + 5G | both paths, no frame awareness | both paths |
| `fixed` (1) | Wi-Fi + 5G | **5G** datagrams | **Wi-Fi** datagrams |
| `combo` (3) | Wi-Fi + 5G | **5G stream** (resent) | **Wi-Fi** datagrams |
| `tiered` (5) | Wi-Fi + 5G | **5G stream** (resent) | P: **5G** datagrams, B: **Wi-Fi** datagrams |

The receiver puts every packet back in its original place and only uses what arrived within 150 ms (live).
A frame can be shown only if all its packets arrived **and** every frame it depends on can be shown.

### Videos that show the frames (I / P / B)

`frame_videos.py` writes the frame number and the frame type of the picture on screen on every tile:

| File | What it shows |
|---|---|
| `frames_lost_one_<codec>.mp4` | Original, one I frame lost, one P frame lost, one B frame lost (2 x 2). The damage stays until the next I frame. |
| `frames_cases_<codec>.mp4` | Original + every case. A frozen frame number = frames the viewer could not see. |

```bash
python3 frame_videos.py                     # VM, after iframe_test.py
```

In VLC: **Playback - Speed - Slower**, or press **E** to step one frame at a time.

### Watch the videos (on the Mac)

```bash
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/iframe_results/frames_*.mp4' ~/Downloads/
open -a VLC ~/Downloads/frames_lost_one_h264.mp4
open -a VLC ~/Downloads/frames_cases_h264.mp4
```

```bash
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/iframe_results/iframe_grid_*.mp4' ~/Downloads/
open -a VLC ~/Downloads/iframe_grid_h264.mp4
open -a VLC ~/Downloads/iframe_grid_hevc.mp4
```

One case only, e.g. I -> 5G stream, P/B -> Wi-Fi datagram:

```bash
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/iframe_results/h264/combo/live.mp4' ~/Downloads/iframe_combo_h264.mp4
open -a VLC ~/Downloads/iframe_combo_h264.mp4
```

---

## Results (VM run)

### Frames of the encoded video

| | H.264 | HEVC |
|---|---|---|
| I frames | 20, 146.7 kB each, **33.5 % of all bytes** | 20, 129.4 kB, 28.7 % |
| P frames | 184, 23.9 kB, 50.1 % | 160, 31.8 kB, 56.3 % |
| B frames | 396, 3.6 kB, 16.4 % | 420, 3.2 kB, 15.1 % |
| I frame vs P frame | 6.1 x bigger | 4.1 x bigger |
| GOP | 30 frames (1 s) | 30 frames (1 s) |

### What one lost frame does

| Lost | H.264 | HEVC |
|---|---|---|
| One I frame | 30 frames (1.00 s) | 30 frames (1.00 s) |
| One P frame | 29 frames (0.97 s) | 29 frames (0.97 s) |
| One B frame | 2 frames (0.07 s) | 28 frames (0.93 s) |

### Wi-Fi + 5G (live, buffer 150 ms, 60 % background)

| | H.264 ref | H.264 (1) | H.264 (3) | HEVC ref | HEVC (1) | HEVC (3) |
|---|---|---|---|---|---|---|
| I frames arrived | 10/20 | 19/20 | **20/20** | 7/20 | 17/20 | **20/20** |
| GOPs lost | 10 | 1 | **0** | 13 | 3 | **0** |
| Frames the viewer can see | 158/600 | 173/600 | **229/600** | 79/600 | 192/600 | **279/600** |
| PSNR | 23.4 dB | 24.3 dB | **26.6 dB** | 16.3 dB | 22.3 dB | **25.3 dB** |
| I-frame delay avg / 95 % | - | 30.5 / 30.8 ms | 33.7 / 38.9 ms | - | 30.5 / 30.8 ms | 35.1 / 43.6 ms |

ref = no frame awareness, (1) = I -> 5G, P/B -> Wi-Fi (datagrams), (3) = I -> 5G stream, P/B -> Wi-Fi datagrams.

---

## Improvement: (5) I -> 5G stream, P -> 5G datagram, B -> Wi-Fi datagram

Case (3) protects every I frame, but about 1 in 4 P frames is still lost on Wi-Fi, and every lost P
frame breaks the rest of its GOP. Case (5) also moves the P frames to 5G (fast datagrams, 0.1 % loss),
and leaves only the small B frames on Wi-Fi, where a loss damages little:

```
            5G    ── I frames, QUIC STREAM (resent)    ──►
 Server     5G    ── P frames, QUIC DATAGRAM (fast)    ──►   Client
           Wi-Fi  ── B frames, QUIC DATAGRAM (fast)    ──►
```

First test (H.264, test network with 1 % Wi-Fi / 0.1 % 5G loss, no delay):

| | (3) I 5G stream, P/B Wi-Fi | (5) I 5G stream, P 5G, B Wi-Fi |
|---|---|---|
| I frames arrived | 20/20 | 20/20 |
| P frames arrived | 168/220 | **218/220** |
| Frames the viewer can see | 105/600 | **512/600** |
| Frames damaged (measured) | 412/600 | **49/600** |
| PSNR / SSIM | 25.9 dB / 0.970 | **38.8 dB / 0.997** |

Run it in the VM for the real numbers: `sudo python3 iframe_test.py --video BBB.webm --scenarios combo,tiered`

## Questions

### 1. If an I frame is lost, does it affect the P and B frames?

Yes - all of them. The I frame is the only complete picture of its GOP; every P frame is built on it and
every B frame on those. One lost I frame = the whole GOP (30 frames, 1 s) until the next I frame.
It is also the frame most likely to be lost: about 150 packets that must **all** arrive
(with 0.6 % loss: 0.994^150 = 0.41, so about half are lost - measured 10/20 lost without frame awareness).
Sending I frames as a 5G stream: 20/20 arrived, 0 GOPs lost, for about 8 ms more delay.

### 2. If a P frame is lost?

The P frame and every frame after it in the GOP are damaged (later P frames and the B frames use it),
until the next I frame. Measured: 29 frames (the first P of a GOP). In case (3) P frames go over Wi-Fi
datagrams and about 1 in 4 is lost (25 packets each: 0.99^25 = 0.78), which is now the main limit.

### 3. If a B frame is lost?

H.264: usually only that frame (2 frames damaged) - almost nothing depends on it.
HEVC: B frames are also used as references (hierarchical B), so the damage spreads (28 frames).
B frames are small (about 4 packets), so they are rarely lost.

### 4. What if we skip similar frames?

- The encoder already skips similarity: P/B frames store only what changed (a B frame is 3.6 kB vs 147 kB).
- Dropping near-duplicate frames **before encoding** (ffmpeg `mpdecimate`) is safe: fewer frames and bytes,
  the player shows the previous picture a little longer; fast motion gets jerky if too many are skipped.
- Dropping frames **after encoding** is only safe for frames nothing depends on (H.264 B frames);
  skipping an I or P frame breaks the GOP (questions 1 and 2).

---

## Saving Part 5 to GitHub

```bash
bash push_part5.sh      # VM, in the project folder
```

Username = your GitHub username, password = a personal access token (GitHub - Settings -
Developer settings - Personal access tokens, with "repo"). Delete the token after the push.
