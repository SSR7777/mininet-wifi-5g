# Step-by-step guide: from the start to the results

This guide follows the project in the order it was built. Each part has its own **branch** on GitHub,
and every branch also contains the parts before it:

| Branch | What it adds | Builds on |
|---|---|---|
| `part1-wifi-5g-comparison` | Wi-Fi vs emulated 5G: topology, config file, background traffic, sweeps, graphs | - |
| `part2-video-streaming` | A real video sent to the receiver: time to send, path loss, packet loss, delay, background, picture quality; video players, live (UDP) and not live (HTTP/TCP) | part 1 |
| `part3-picoquic` | QUIC with picoquic: file download (HTTP/3) and live stream over QUIC streams / datagrams; video players over QUIC | part 2 |
| `part4-multipath-live-video` | Wi-Fi + 5G together (multipath QUIC, MPTCP); the real video sent live over picoquic; one grid video to compare everything (VLC) | part 3 |
| `part5-iframe-aware` | I/P/B frame analysis (H.264, HEVC); I frames over 5G, P/B frames over Wi-Fi | part 4 |
| `main` | Everything | |

```
part1 ──► part2 ──► part3 ──► part4 ──► part5 = main
```

To look at one part only:

```bash
git clone https://github.com/SSR7777/mininet-wifi-5g.git
cd mininet-wifi-5g
git checkout part2-video-streaming      # or part1-..., part3-..., part4-..., main
```

Where each command runs:

| Prompt | Machine | Used for |
|---|---|---|
| `siddhartha@mininet-wifi:~$` | Ubuntu VM | Running the emulation (`sudo python3 ...`) |
| `ssr@Siddharthas-MacBook-Air ~ %` | Mac | Copying files (`scp`) and watching videos (VLC) |

Paste one command at a time. If you paste several lines while `ssh` or `scp` asks for a password, the
remaining lines run on the wrong machine.


All commands in one list (no explanations): **[COMMANDS.md](COMMANDS.md)**.

---

## Step 0 - Set up the VM (once)

```bash
git clone https://github.com/SSR7777/mininet-wifi-5g.git
cd mininet-wifi-5g
bash install.sh                          # Mininet-WiFi, iperf3, mptcpd (10-20 min)
sudo apt install -y ffmpeg python3-matplotlib
```

If `apt` says *"Release file ... is not valid yet"*, the VM clock is wrong:

```bash
sudo date -s "$(curl -sI https://www.google.com | grep -i '^date:' | cut -d' ' -f2-)"
sudo timedatectl set-ntp true
```

Copy the video into the project folder (from the Mac):

```bash
scp ~/Downloads/BBB.webm siddhartha@192.168.252.4:~/mininet-wifi-5g/
```

After a crash: `sudo mn -c`.

---

## Step 1 - Wi-Fi vs 5G comparison (`part1-wifi-5g-comparison`)

The two paths (values in `config.ini`):

| Path | Capacity | Delay | Loss | Background |
|---|---|---|---|---|
| Wi-Fi | 130 Mbit/s | 10 ms | 1 % | 60 % (iPerf3) |
| 5G | 700 Mbit/s | 15 ms | 0.1 % | 60 % (iPerf3) |

```bash
sudo python3 wifi_5g_topology.py          # constant 60 % background
sudo python3 vary_traffic.py              # background changes every 5 s
sudo python3 sweep.py && python3 plot.py  # graphs
```

**Result:** throughput, RTT and loss of each path, with and without changing background.

---

## Step 2 - Video streaming (`part2-video-streaming`)

### 2a. One video over each network: the 5 measurements

```bash
sudo python3 video_stream.py --video BBB.webm
python3 show_results.py
```

Measured for Wi-Fi and 5G: **time to send**, **path loss** (radio), **packet loss**, **delay**,
**background traffic**, and picture quality (PSNR/SSIM). With `[vary] enabled = yes` in `config.ini`,
capacity, delay, loss and background change randomly every 5 s while the video is sent.

### 2b. Video players at the receiver: live and not live

```bash
sudo python3 players_test.py --video BBB.webm --mode both
python3 show_results.py --players
```

- **Live:** the video is streamed over UDP in real time; frames later than the buffer (150 ms) freeze the picture.
- **Not live:** the player downloads the video over HTTP/TCP and starts after 1 s is buffered.
- `--players 1,2,4,8` compares more players at the same receiver (default: 1 player).

---

## Step 3 - QUIC with picoquic (`part3-picoquic`)

Build picoquic once (in the VM):

```bash
bash install_picoquic.sh                  # builds ~/picoquic and runs a self-test
```

```bash
sudo python3 quic_test.py --video BBB.webm      # file over QUIC (HTTP/3)
sudo python3 quic_live.py                       # live stream: QUIC streams vs datagrams
python3 show_results.py --compare               # without vs with live stream
python3 show_results.py --one                   # Wi-Fi vs 5G, all QUIC tests in one table
sudo python3 players_test.py --video BBB.webm --mode quic   # video players over QUIC
python3 show_results.py --players
```

- **QUIC streams:** reliable, lost packets are resent (more delay, no lost frames).
- **QUIC datagrams:** unreliable, lost packets are not resent (less delay, some lost frames).

---

## Step 4 - Wi-Fi + 5G together and live real video (`part4-multipath-live-video`)

Let picoquic carry the real video (once, in the VM; `install_picoquic.sh` does this too):

```bash
python3 patch_quicperf_video.py ~/picoquic
(cd ~/picoquic && make -j"$(nproc)" picoquicdemo)
```

### 4a. Wi-Fi vs 5G vs Wi-Fi + 5G

```bash
sudo python3 players_test.py --video BBB.webm            # every test on Wi-Fi, 5G and Wi-Fi + 5G
python3 show_results.py --three
```

| Way of sending | Wi-Fi + 5G uses |
|---|---|
| Live over QUIC (streams / datagrams) | multipath QUIC (picoquic `-M`) |
| Not live over HTTP/3 | multipath QUIC |
| Not live over TCP | MPTCP |
| Live over plain UDP | not possible (UDP has no multipath) |

### 4b. The real video sent live, and the grid video

```bash
sudo rm -rf players_results
sudo python3 players_test.py --video BBB.webm --mode video
python3 show_results.py --video          # table
python3 make_grid.py                     # players_results/livevideo_grid.mp4
exit                                     # back to the Mac
```

On the Mac:

```bash
scp "siddhartha@192.168.252.4:~/mininet-wifi-5g/players_results/livevideo_grid.mp4" ~/Downloads/
open -a VLC ~/Downloads/livevideo_grid.mp4
```

The grid (press **F** in VLC for full screen):

```
            Original | Wi-Fi | 5G | Wi-Fi + 5G
Datagrams:     ...   |  ...  | .. |    ...       <- lost packets not resent: damage / freezes
Streams:       ...   |  ...  | .. |    ...       <- lost packets resent: clean picture, more delay
```

Other files: `livevideo_dgram_compare.mp4`, `livevideo_stream_compare.mp4` (2 x 2 per mode) and
`livevideo_<mode>_<network>/live.ts` (exactly what the player received; plays in VLC).

---

## Step 5 - I-frame aware sending (`part5-iframe-aware`)

All commands, results and explanations of this part: **[PART5_IFRAME.md](PART5_IFRAME.md)**.

A compressed video has **I frames** (complete pictures), **P frames** (changes from an earlier frame)
and **B frames** (changes from an earlier and a later frame). A **GOP** starts with an I frame and every
P/B frame of the GOP depends on it, so **if the I frame is lost the whole GOP is lost**.

```
 GOP 1                 GOP 2                 GOP 3
[I B B P B B P ...]   [I B B P B B P ...]   [I B B P ...]
 ^ every frame of the GOP needs this I frame
```

```bash
sudo python3 iframe_test.py --video BBB.webm      # H.264 and HEVC (about 10 min)
python3 show_results.py --iframe
```

What it does:

1. **Frame analysis** - encodes the video with H.264 and HEVC (one I frame per second, 3 B frames) and
   counts the I / P / B frames, their sizes and the GOP length.
2. **What one lost frame does** - drops one I, one P and one B frame and counts the damaged frames.
3. **Frame-aware path assignment** (live, picoquic, both paths with 60 % background):

| Network | Case | I frames | P/B frames |
|---|---|---|---|
| Wi-Fi only | no frame awareness | Wi-Fi datagrams | Wi-Fi datagrams |
| Wi-Fi only | I stream + P/B dgram | Wi-Fi QUIC **stream** (resent) | Wi-Fi datagrams |
| 5G only | no frame awareness | 5G datagrams | 5G datagrams |
| 5G only | I stream + P/B dgram | 5G QUIC **stream** (resent) | 5G datagrams |
| Wi-Fi + 5G | reference, no frame awareness | both paths (multipath QUIC) | both paths |
| Wi-Fi + 5G | (1) fixed | **5G**, datagrams | **Wi-Fi**, datagrams |
| Wi-Fi + 5G | (3) combination | **5G**, QUIC **stream** | **Wi-Fi**, datagrams |

Only some cases: `--scenarios single` (Wi-Fi only + 5G only), `--scenarios multi` (Wi-Fi + 5G), or a list
such as `--scenarios wifi,5g`. Results of cases not run again are kept.

The video (MPEG-TS) is split packet by packet into an I-frame part and a P/B part, each part is sent on
its own picoquic connection over its own path, and the receiver puts every byte back in place. A frame
can be shown only if all its bytes arrived within the buffer **and** every frame it depends on can be shown.

Watch every case side by side (one grid video per codec):

```bash
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/iframe_results/iframe_grid_*.mp4' ~/Downloads/     # on the Mac
open -a VLC ~/Downloads/iframe_grid_h264.mp4
```

---

## Results in short

| | Wi-Fi | 5G | Wi-Fi + 5G |
|---|---|---|---|
| Live, QUIC datagrams | visible damage and freezes (1 % loss, not resent) | almost perfect (0.1 % loss) | less damage than Wi-Fi, short freezes at scene changes (two paths with different delays) |
| Live, QUIC streams | perfect picture, more delay (resends) | perfect, low delay | perfect, part of the video over 5G |
| Not live (TCP / HTTP/3) | identical file, slower download | identical file, fast | identical file, both paths used |

---

## Saving the work to GitHub

In the VM, inside the project folder:

```bash
bash organize_github.sh
```

It creates the four part branches and pushes them, and `main`. When asked, the username is your
GitHub username and the password is a **personal access token** (GitHub - Settings - Developer settings -
Personal access tokens). Do not paste the token in chats or screenshots; delete it on GitHub after the push.
