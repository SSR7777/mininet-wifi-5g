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

**Mac** - watch in VLC
```bash
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/iframe_results/iframe_grid_*.mp4' ~/Downloads/
open -a VLC ~/Downloads/iframe_grid_h264.mp4
open -a VLC ~/Downloads/iframe_grid_hevc.mp4
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/iframe_results/h264/combo/live.mp4' ~/Downloads/iframe_combo_h264.mp4
open -a VLC ~/Downloads/iframe_combo_h264.mp4
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
