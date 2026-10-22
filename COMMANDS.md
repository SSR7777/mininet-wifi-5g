# All commands, in order

Every command used in this project, from setting up the VM to pushing to GitHub.
Explanations and results: [GUIDE.md](GUIDE.md) and [PART5_IFRAME.md](PART5_IFRAME.md).

**Where to type each command** - check the prompt first, and paste **one command at a time**:

| Prompt | Machine | Used for |
|---|---|---|
| `ssr@Siddharthas-MacBook-Air ~ %` | **Mac** | copying files (`scp`), logging in (`ssh`), watching videos (VLC) |
| `siddhartha@mininet-wifi:~/mininet-wifi-5g$` | **VM** (Ubuntu) | running the emulation (`sudo python3 ...`) |

---

## 0. Set up (once)

**VM**
```bash
git clone https://github.com/SSR7777/mininet-wifi-5g.git
cd mininet-wifi-5g
bash install.sh                                   # Mininet-WiFi, iperf3, mptcpd (10-20 min)
sudo apt install -y ffmpeg python3-matplotlib
bash install_picoquic.sh                          # picoquic + real-video patch (Parts 3-5)
```

If `apt` says "Release file ... is not valid yet" (wrong VM clock):
```bash
sudo date -s "$(curl -sI https://www.google.com | grep -i '^date:' | cut -d' ' -f2-)"
sudo timedatectl set-ntp true
```

If picoquic was built before the real-video patch existed:
```bash
python3 patch_quicperf_video.py ~/picoquic
(cd ~/picoquic && make -j"$(nproc)" picoquicdemo)
```

**Mac** - copy the video and changed files to the VM, then log in:
```bash
cd ~/Downloads/mininet-wifi-5g
scp ~/Downloads/BBB.webm siddhartha@192.168.252.4:~/mininet-wifi-5g/
scp *.py *.sh *.md config.ini siddhartha@192.168.252.4:~/mininet-wifi-5g/
ssh siddhartha@192.168.252.4
```

**VM** - go to the project folder (every time after logging in):
```bash
cd ~/mininet-wifi-5g
```

After a crash: `sudo mn -c`.  Leave the VM: `exit` (until the prompt is the Mac again).

---

## Part 1 - Wi-Fi vs 5G comparison  (branch `part1-wifi-5g-comparison`)

**VM**
```bash
sudo python3 wifi_5g_topology.py                  # constant 60 % background
sudo python3 vary_traffic.py                      # background changes every 5 s
sudo python3 sweep.py
python3 plot.py                                   # graphs
```

---

## Part 2 - Video streaming, live and not live  (branch `part2-video-streaming`)

**VM**
```bash
sudo python3 video_stream.py --video BBB.webm     # time to send, path loss, packet loss, delay, background
python3 show_results.py                           # tables again

sudo python3 players_test.py --video BBB.webm --mode both     # video player: live (UDP) and not live (HTTP/TCP)
sudo python3 players_test.py --video BBB.webm --mode both --players 1,2,4,8   # more players
python3 show_results.py --players
```

---

## Part 3 - QUIC with picoquic  (branch `part3-picoquic`)

**VM**
```bash
sudo python3 quic_test.py --video BBB.webm        # file over QUIC (HTTP/3) - does QUIC work?
sudo python3 quic_live.py                         # live stream: QUIC streams vs QUIC datagrams
python3 show_results.py --quic
python3 show_results.py --live
python3 show_results.py --compare                 # without vs with live stream
python3 show_results.py --one                     # Wi-Fi vs 5G, all QUIC tests in one table
sudo python3 players_test.py --video BBB.webm --mode quic     # video players over QUIC
python3 show_results.py --players
```

---

## Part 4 - Wi-Fi + 5G together, live real video  (branch `part4-multipath-live-video`)

**VM**
```bash
sudo python3 players_test.py --video BBB.webm                 # Wi-Fi, 5G and Wi-Fi + 5G (multipath QUIC, MPTCP)
python3 show_results.py --three

sudo rm -rf players_results
sudo python3 players_test.py --video BBB.webm --mode video    # the real video sent live over picoquic
python3 show_results.py --video
python3 make_grid.py                                          # all videos in one grid
exit
```

**Mac** - watch in VLC
```bash
brew install --cask vlc                                       # once
scp "siddhartha@192.168.252.4:~/mininet-wifi-5g/players_results/livevideo_grid.mp4" ~/Downloads/
open -a VLC ~/Downloads/livevideo_grid.mp4
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/players_results/livevideo_*_compare.mp4' ~/Downloads/
open -a VLC ~/Downloads/livevideo_dgram_compare.mp4
for n in wifi 5g multi; do scp "siddhartha@192.168.252.4:~/mininet-wifi-5g/players_results/livevideo_dgram_$n/live.ts" ~/Downloads/received_$n.ts; done
open -na VLC ~/Downloads/received_wifi.ts
open -na VLC ~/Downloads/received_5g.ts
open -na VLC ~/Downloads/received_multi.ts
```

---

## Part 5 - I-frame aware sending  (branch `part5-iframe-aware`)

**VM** - run everything again from scratch (H.264 + HEVC, all 7 cases, about 20-25 min)
```bash
cd ~/mininet-wifi-5g
sudo rm -rf iframe_results
sudo python3 iframe_test.py --video BBB.webm
python3 show_results.py --iframe                  # tables again
```

Only some cases (results of the other cases are kept):
```bash
sudo python3 iframe_test.py --video BBB.webm --analyse-only       # only frame analysis (steps 1 + 2)
sudo python3 iframe_test.py --video BBB.webm --scenarios single   # Wi-Fi only + 5G only
sudo python3 iframe_test.py --video BBB.webm --scenarios multi    # Wi-Fi + 5G cases
sudo python3 iframe_test.py --video BBB.webm --scenarios combo    # I -> 5G stream, P/B -> Wi-Fi datagrams
sudo python3 iframe_test.py --video BBB.webm --scenarios tiered   # I -> 5G stream, P -> 5G dgram, B -> Wi-Fi dgram
sudo python3 iframe_test.py --video BBB.webm --codecs h264        # one codec
```

Look at the frames yourself (steps 1 and 2 by hand):
```bash
mkdir -p demo && cd demo
ffmpeg -y -i ../BBB.webm -t 20 -an -vf scale=1280:720,fps=30 -c:v libx264 -preset veryfast \
  -b:v 4M -maxrate 4M -bufsize 4M -g 30 -keyint_min 30 -sc_threshold 0 -bf 3 -pix_fmt yuv420p h264.mp4
ffmpeg -y -i h264.mp4 -c copy -f mpegts h264.ts
ffprobe -v error -select_streams v:0 -show_entries frame=pict_type -of csv=p=0 h264.ts | sort | uniq -c
ffprobe -v error -select_streams v:0 -show_entries frame=pict_type,pkt_size -of csv=p=0 h264.ts | head -40
cd ..
exit
```

Videos that show the frame number and frame type (I / P / B) on every tile:
```bash
python3 frame_videos.py                           # iframe_results/frames_lost_one_<codec>.mp4, frames_cases_<codec>.mp4
```

**Mac** - watch in VLC
```bash
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/iframe_results/frames_*.mp4' ~/Downloads/
open -a VLC ~/Downloads/frames_lost_one_h264.mp4    # Original | one I lost | one P lost | one B lost
open -a VLC ~/Downloads/frames_cases_h264.mp4       # Original + every case
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/iframe_results/iframe_grid_*.mp4' ~/Downloads/
open -a VLC ~/Downloads/iframe_grid_h264.mp4
open -a VLC ~/Downloads/iframe_grid_hevc.mp4
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/iframe_results/h264/combo/live.mp4' ~/Downloads/iframe_combo_h264.mp4
open -a VLC ~/Downloads/iframe_combo_h264.mp4
```

---

## Part 6 - Adaptive: switch when the main path gets weak

Scenario A = 5G main + Wi-Fi helper, scenario B = Wi-Fi main + 5G helper. While the main path is good the
whole video goes over it; from 5 s to 15 s its bandwidth drops to 2 Mbit/s and the adaptive sender keeps only
the I frames on the main path's QUIC stream and moves the P/B frames to the helper path as datagrams.

**VM**
```bash
sudo python3 adaptive_test.py --video BBB.webm                    # A and B, H.264 + HEVC (~10 min)
sudo python3 adaptive_test.py --video BBB.webm --scenarios A --codecs h264
sudo python3 adaptive_test.py --video BBB.webm --weak-mbps 1 --drop-at 4 --recover-at 16
python3 show_results.py --adaptive                                # tables again
python3 adaptive_test.py --videos-only                            # VLC videos again
exit
```

**Mac** - watch in VLC (Original | no adaptation | adaptive, bar = main path GOOD / WEAK)
```bash
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/adaptive_results/adaptive_*.mp4' ~/Downloads/
open -a VLC ~/Downloads/adaptive_A_h264.mp4
open -a VLC ~/Downloads/adaptive_B_h264.mp4
```

---

## Part 7 - Long video: I frames on the better path, P/B on the worse path

A 15-minute video is sent live while the bandwidth of both paths changes (5G up to 700 Mbit/s,
Wi-Fi up to 500 Mbit/s, the curves cross like in the mentor's drawing). I frames only travel on a
QUIC **stream**, P/B frames only as QUIC **datagrams**. The adaptive sender checks both links every
second and switches **automatically**: 5G stronger -> I on the 5G stream, P/B on Wi-Fi datagrams;
5G drops below Wi-Fi -> I on the Wi-Fi stream, P/B on 5G datagrams; 5G strong again -> back.
"Fixed" never switches (I on 5G, P/B on Wi-Fi).

| Phase (each 1/5 of the video) | 5G | Wi-Fi | Adaptive: I frames on |
|---|---|---|---|
| 1 | 700 | 500 | 5G stream |
| 2 | weak | 500 | Wi-Fi stream |
| 3 | 700 | 400 | 5G stream |
| 4 | 600 | weak | 5G stream |
| 5 | weak | 500 | Wi-Fi stream |

**VM**
```bash
cd ~/mininet-wifi-5g
sudo python3 long_video_test.py --video BBB.webm --length 120 --segment 10 --ramp 10   # quick 2-minute test (~8 min)
sudo python3 long_video_test.py --video BBB.webm                  # full 15-minute video (about 40 min)
sudo python3 long_video_test.py --video BBB.webm --weak-mbps 2    # weaker "weak" signal
sudo python3 long_video_test.py --video BBB.webm --pb wifi        # P/B always on Wi-Fi datagrams
python3 show_results.py --long                                    # tables again
sudo python3 long_video_test.py --video-only                      # graph + VLC video again (no new test)
exit
```

**Mac** - graph and VLC video (Original | fixed | adaptive, bar = where the I and P/B frames go)
```bash
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/longvideo_results/longvideo_*' ~/Downloads/
open ~/Downloads/longvideo_h264.png
open -a VLC ~/Downloads/longvideo_h264.mp4
```

---

## Saving to GitHub

**VM**
```bash
bash organize_github.sh       # first time only: branches part1 ... part4 + main
bash push_part5.sh            # Part 5 (run again after every change, e.g. new results)
git log --oneline --graph --decorate -7 main      # check the order of the parts
```

Only pushing again (the commit is already made):
```bash
git push origin part5-iframe-aware main
```

When asked: **Username** = `SSR7777`, **Password** = a personal access token
(https://github.com/settings/tokens/new?scopes=repo, 7 days). Paste it only into the terminal
(Cmd+V, nothing is shown), never into a chat or screenshot, and delete it after the push.
