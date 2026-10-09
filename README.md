# Wi-Fi + Emulated 5G Dual-Path Emulation (Mininet-WiFi)

A client (`sta1`) with two access paths, **Wi-Fi** and **emulated 5G**, to one server (`srv`), built with
[Mininet-WiFi](https://github.com/intrig-unicamp/mininet-wifi). All network values are in `config.ini`.

## The project in three parts

| Part | What it does | Main files |
|---|---|---|
| **1. Wi-Fi vs 5G comparison** | Emulates both networks with the assignment's values (capacity, delay, loss), adds 60 % iPerf3 background traffic that also varies over time, and compares throughput, RTT and loss | `config.ini`, `netconfig.py`, `wifi_5g_topology.py`, `sweep.py`, `plot.py`, `vary_traffic.py`, `install.sh` |
| **2. Video streaming, live and not live** | Sends a real video from the sender to the receiver over each network. It measures time to send, path loss, packet loss, delay, background and picture quality, connects 1 or many video players at the receiver, and compares a live stream (UDP) with video on demand (HTTP/TCP) | `video_stream.py`, `players_test.py`, `show_results.py` |
| **3. QUIC with picoquic** | Uses [picoquic](https://github.com/private-octopus/picoquic) (QUIC over UDP) for the video: a file download (HTTP/3 over QUIC streams) and a live stream over QUIC streams and QUIC datagrams | `install_picoquic.sh`, `quic_test.py`, `quic_live.py`, `show_results.py` |

## Setup (Ubuntu VM)

```bash
git clone https://github.com/SSR7777/mininet-wifi-5g.git
cd mininet-wifi-5g
bash install.sh                 # Mininet-WiFi, iperf3, mptcpd (about 10-20 min)
sudo apt install -y ffmpeg python3-matplotlib
bash install_picoquic.sh        # only for part 3 (builds ~/picoquic)
sudo mn -c                      # clean up after a crash
```

## Quick start: one command per part

```bash
# Part 1 - Wi-Fi vs 5G
sudo python3 wifi_5g_topology.py          # constant 60 % background
sudo python3 vary_traffic.py              # background changes every 5 s
sudo python3 sweep.py && python3 plot.py  # graphs

# Part 2 - video streaming
sudo python3 video_stream.py --video BBB.webm      # one video, Wi-Fi then 5G
sudo python3 players_test.py --video BBB.webm      # players at the receiver, live + not live
python3 show_results.py --players                  # show the tables again

# Part 3 - QUIC (picoquic)
sudo python3 quic_test.py --video BBB.webm         # file over QUIC
sudo python3 quic_live.py                          # live stream over QUIC
python3 show_results.py --one                      # Wi-Fi vs 5G, all QUIC tests in one table
```

---

# Part 1 - Wi-Fi vs 5G comparison

## Path parameters

| Path  | Capacity   | Delay (one-way) | Loss  | Background traffic (60 %) |
|-------|------------|-----------------|-------|---------------------------|
| Wi-Fi | 130 Mbit/s | 10 ms           | 1 %   | 78 Mbit/s UDP (iPerf3)    |
| 5G    | 700 Mbit/s | 15 ms           | 0.1 % | 420 Mbit/s UDP (iPerf3)   |

## Topology

```
                 Wi-Fi: 130 Mbit/s, 10 ms, 1 % loss
 sta1-wlan0 )))) ap1 ============================== srv-wifi
 10.0.1.1                                          10.0.1.100

 sta1-5g ======= s5g ============================== srv-5g
 10.0.2.1        5G: 700 Mbit/s, 15 ms, 0.1 % loss 10.0.2.100
```

- `sta1` is a Mininet-WiFi station. It has a real emulated 802.11 interface (mac80211_hwsim) and a second interface for 5G.
- The 5G path is emulated with a wired link shaped by `tc` (HTB for rate, netem for delay and loss).
- Each path is in its own subnet. Traffic to `10.0.1.100` goes over Wi-Fi and traffic to `10.0.2.100` goes over 5G.
- Shaping sits on `TCLink`. Rate and delay apply in both directions, so the base RTT is about 20 ms on Wi-Fi and 30 ms on 5G. Loss applies only in the client→server (data) direction.

## Configuration file (`config.ini`)

All network settings live in **`config.ini`**: capacity, delay, loss, background traffic and test length. To change the experiment, edit this file and run the script again. No code changes are needed.

```ini
[wifi]
capacity_mbps = 130
delay_ms      = 10      # one-way delay
loss_pct      = 1       # packet loss in %
distance_m    = 5       # station to access point (radio path loss)

[5g]
capacity_mbps = 700
delay_ms      = 15
loss_pct      = 0.1

[background]
share     = 0.60        # constant background (wifi_5g_topology.py, sweep.py)
min_share = 0.30        # varying background (vary_traffic.py)
max_share = 0.90
step_s    = 5           # seconds between changes

[test]
duration_s = 60
seed       = 1
```

Edit it in the VM with `nano config.ini`, then save with Ctrl+O and exit with Ctrl+X. Every script prints the values it loaded when it starts. To keep several setups, copy the file and pass it with `--config`, for example `sudo python3 vary_traffic.py --config high_loss.ini`.

## Run the basic experiment

```bash
sudo python3 wifi_5g_topology.py                 # 30 s experiment
sudo python3 wifi_5g_topology.py --duration 60   # longer run
sudo python3 wifi_5g_topology.py --cli           # experiment, then Mininet CLI
sudo python3 wifi_5g_topology.py --no-test       # only build the topology + CLI
sudo python3 wifi_5g_topology.py --mptcp         # also one MPTCP flow over both paths
sudo mn -c                                       # clean up after a crash
```

### What `wifi_5g_topology.py` does

1. Builds the topology and saves the `tc` configuration to `results/tc_config.txt` as proof of the shaping.
2. Pings each path without load to get the base RTT.
3. Starts iPerf3 UDP background traffic at 60 % of each path's capacity.
4. Under that load, runs a TCP iPerf3 flow on each path and measures RTT with ping.
5. Writes `results/summary.csv` and `results/summary.json`, plus the raw iPerf3 JSON and ping logs.

### Expected behaviour

- Idle RTT is about 20 ms on Wi-Fi and about 30 ms on 5G.
- Background throughput reaches about 78 Mbit/s on Wi-Fi and about 420 Mbit/s on 5G.
- The foreground TCP flow gets the remaining capacity. On Wi-Fi, the 1 % loss holds it well below the remaining 52 Mbit/s.
- Under load, RTT rises because of queueing.

### Useful CLI commands (with `--cli`)

```
mininet-wifi> sta1 ping -c 5 10.0.1.100        # Wi-Fi path
mininet-wifi> sta1 ping -c 5 10.0.2.100        # 5G path
mininet-wifi> sta1 iw dev sta1-wlan0 link      # Wi-Fi association
mininet-wifi> sta1 tc qdisc show
```

## Parameter sweeps and graphs

`sweep.py` runs the experiment repeatedly with different settings, and `plot.py` turns the results into graphs.

```bash
sudo apt install -y python3-matplotlib    # once
sudo python3 sweep.py                     # about 10 min (add --repeats 3 for averages)
python3 plot.py
```

The sweep runs two sets of tests:

- **Background load:** 0, 20, 40, 60 and 80 % of each path's capacity, with the assignment's loss values.
- **Wi-Fi loss:** 0, 0.5, 1 and 2 %, with background fixed at 60 %.

Output in `sweep_results/`:

- `all_runs.csv` holds every measurement.
- `1_tcp_vs_background.png` shows TCP throughput against background load.
- `2_rtt_vs_background.png` shows RTT under load against background load.
- `3_wifi_tcp_vs_loss.png` shows Wi-Fi TCP throughput against loss, compared with the Mathis model.

A single run can also be changed by hand:

```bash
sudo python3 wifi_5g_topology.py --bg-share 0.4 --wifi-loss 0.5 --g5-loss 0.1
```

## Time-varying background traffic

`vary_traffic.py` makes the background load change over time instead of staying at a constant 60 %. Every few seconds each path jumps to a new random level between 30 and 90 % of capacity, about 60 % on average, and Wi-Fi and 5G change independently. A TCP flow and ping run on both paths throughout, and a live table in the terminal shows each second how TCP and RTT react.

```bash
sudo python3 vary_traffic.py                                  # 60 s, new level every 5 s
sudo python3 vary_traffic.py --duration 120 --step 10 --seed 7
```

Output in `vary_results/`:

- `timeseries.csv` has background load, TCP throughput, TCP retransmissions and RTT for every second.
- `loss_summary.csv` compares the configured loss with the measured loss: ping loss, background UDP loss and TCP retransmissions. The same summary is printed at the end of the run.
- `vary_throughput.png` shows TCP throughput on each path, drawn over the changing background load.
- `vary_rtt.png` shows RTT over time on both paths.

The same `--seed` always produces the same load pattern, so runs can be compared.

## Note on Wi-Fi shaping

Mininet-WiFi puts its own rate limit on the radio interface, based on mode and distance (about 10 Mbit/s in mode g), and that overrides `TCLink`. The script therefore applies the Wi-Fi path's rate, delay and loss with `tc netem` directly on `sta1-wlan0` and `srv-wifi` after the network starts.

---

# Part 2 - Video streaming, live and not live

## Sending a real video

`video_stream.py` sends a real video from the sender (`sta1`) to the receiver (`srv`). It **tests each network individually**: first over Wi-Fi only, then over 5G only. The receiver saves what arrived, and the script compares it with the original.

```bash
sudo apt install -y ffmpeg                            # once
sudo python3 video_stream.py                          # Wi-Fi, then 5G (built-in test video)
sudo python3 video_stream.py --path wifi              # Wi-Fi only
sudo python3 video_stream.py --path 5g                # 5G only
sudo python3 video_stream.py --video myvideo.mp4      # your own video
sudo python3 video_stream.py --transport tcp          # reliable TCP instead of UDP streaming
sudo python3 video_stream.py --background             # add iPerf background traffic
```

The default settings are in the `[video]` section of `config.ini`: file, bitrate, length, transport and background.

Output in `video_results/`:

- `original.mp4` is the video that was sent.
- `received_wifi.ts` and `received_5g.ts` are what arrived over each network. Copy them to a computer and play them in VLC.
- `side_by_side.mp4` shows Wi-Fi on the left and 5G on the right, for a quick visual comparison.
- `video_summary.csv` holds every measurement below, one row per network.
- `ping_wifi.log`, `ping_5g.log` and `bg_*.json` are the raw delay and background measurements.

### What is measured for each network

At the end, the terminal shows a results block for each network and then a summary table:

| Measurement | How it is measured |
|---|---|
| **Time to send** | From when sending starts until the last video packet reaches the receiver. Also shows when the first packet arrived, when the sender finished, and the throughput. |
| **Path loss** | Wi-Fi radio path loss in dB, calculated as Tx power + antenna gains − received signal (RSSI) from Mininet-WiFi's propagation model. Change it with `distance_m` in `config.ini`. The 5G path is an emulated wired link, so it shows n/a. |
| **Packet loss** | UDP: video packets sent (counted at the sender) compared with video packets received (counted at the receiver), using iptables counters on the video port. TCP: retransmitted segments compared with segments sent. Shown next to the configured loss. |
| **Delay** | `ping` runs on the same path while the video is sent. Shows the average RTT and one-way delay (RTT / 2) next to the configured delay. |
| **Background** | With `--background`: the iPerf3 target rate, the rate actually achieved and its UDP loss. Otherwise it shows "off". |
| **Picture quality** | Frames received, decoder errors, PSNR and SSIM compared with the original. |

Example: `sudo python3 video_stream.py --video BBB.webm --background`

### Detailed tables

At the end of every video test, `show_results.py` prints three tables in the terminal:

1. **Test settings:** loss, delay, RTT and background for each network.
2. **Changing conditions:** for every 5 s step, the background level, capacity, delay and loss, plus the measured RTT, video rate and video packets sent, received and lost.
3. **Video results:** Wi-Fi and 5G side by side, covering time to send, path loss, packet loss, delay, background and picture quality.

To show the tables again without running the test:

```bash
python3 show_results.py                 # video_results/
python3 show_results.py --dir my_run    # another results folder
```

### Changing conditions during the video

In a real network, conditions don't stay constant. When `[vary] enabled = yes` in `config.ini` (the default), the network changes at random every `step_s` seconds while the video is being sent:

- **Background traffic:** a random level between `min_share` and `max_share`, about 60 % on average.
- **Capacity:** the config value ± `capacity_pct` %. For example, Wi-Fi varies between about 91 and 169 Mbit/s.
- **Delay:** the config value ± `delay_pct` %. For example, Wi-Fi varies between 5 and 15 ms.
- **Loss:** the config value ± `loss_pct` %. With 100, Wi-Fi loss varies between 0 and 2 %.

On average the values stay at the assignment's numbers. Both networks get the same random pattern (set by `seed`), so the comparison is fair.

The terminal shows a live line for every step: the background level, capacity, delay, loss, and how much video got through and was lost in that step. The results include a per-step table with the measured RTT, which is also saved as `conditions_wifi.csv` and `conditions_5g.csv`.

```bash
sudo python3 video_stream.py --video BBB.webm            # changing conditions (default)
sudo python3 video_stream.py --video BBB.webm --fixed    # constant config.ini values
```

With UDP, lost packets show up as damaged or frozen frames, so 1 % loss on Wi-Fi is clearly visible while 0.1 % on 5G barely is. With TCP, lost packets are resent, so the picture is perfect but delivery can take longer than the video's length.

## Video players at the receiver (1 player and many players)

`players_test.py` connects real **video players** to the receiver. The sender (sta1) streams the live video over UDP in real time. The receiver (srv) runs N players at the same time. Each player is an ffmpeg player that receives, decodes and records what it played.

A **reference player** on the sender itself (no network) receives the same stream. For every frame, the difference between a player's arrival time and the reference's is the delay the network added.

For each player the test measures:

- **Start-up time:** stream start to the first picture.
- **Frame delay:** average, 95th percentile and maximum.
- **Jitter:** how much the frame delay changes from frame to frame.
- **Lost frames.**
- **Late frames and stalls:** frames that arrive after their play-out time, given the player buffer (`--buffer`, default 150 ms).
- **Smooth playback %.**
- **Picture quality:** PSNR against the original.
- **Network figures:** packet loss, RTT, background traffic and path loss.

The test runs for 1, 2, 4 and 8 players (`--players`) on each network, with constant conditions, so the only thing that changes is how many viewers share the network.

It covers two ways of watching (`--mode live / notlive / both`, default both):

| | Live | Not live (video on demand) |
|---|---|---|
| Example | Live TV, video call | YouTube, Netflix |
| How it is sent | Real time over UDP | The stored file is downloaded over HTTP/TCP as fast as possible |
| Lost packets | Lost, so the picture breaks | Resent, so the file arrives identical |
| Measured | Start-up, frame delay, jitter, lost and late frames, PSNR | Start-up (playback starts after `--vod-buffer` s of video, default 1 s), download time, stalls and stall time, TCP retransmissions |

```bash
sudo python3 players_test.py --video BBB.webm
sudo python3 players_test.py --video BBB.webm --players 1,2,4,8,16 --buffer 200
sudo python3 players_test.py --video BBB.webm --mode live      # only live
python3 show_results.py --players
```

Output in `players_results/`:

- `players_summary.csv` and `players_detail.csv`: live results, per number of players and per player.
- `players_notlive_summary.csv` and `players_notlive_detail.csv`: not-live (video on demand) results.
- `live_<network>_<N>players/player_<k>.ts`: what each live player played.
- `live_<network>_<N>players/players_grid.mp4`: up to 4 players side by side.

Every player decodes video, so many players also load the VM's CPU. The CPU % column shows this. If it is close to 100 %, part of the extra delay comes from the VM rather than the network.

---

# Part 3 - QUIC with picoquic

## QUIC with picoquic

[picoquic](https://github.com/private-octopus/picoquic) implements **QUIC**, the transport protocol behind HTTP/3. QUIC runs **over UDP** and adds what plain UDP lacks: TLS 1.3 encryption, reliable delivery (lost packets are resent), congestion control (BBR by default) and several streams in one connection.

`quic_test.py` checks that QUIC works over both emulated networks:

- `sta1`, the sender, runs the picoquic **server** and sends the video file.
- `srv`, the receiver, runs the picoquic **client** and fetches the file over QUIC/UDP (HTTP/3). This is the same direction as the video test, so the configured loss hits the QUIC data and QUIC has to retransmit.
- picoquic runs with `-0` (no UDP GSO), so every packet is counted individually.
- Each network is tested on its own. For 20 s the client downloads the file again and again, while background, capacity, delay and loss change every 5 s, the same as in the video test.
- After every download, the received file is checked to be **identical** to the original (MD5).

```bash
bash install_picoquic.sh                  # once: builds ~/picoquic and runs a self-test
sudo python3 quic_test.py --video BBB.webm
sudo python3 quic_test.py --fixed         # constant config.ini values
python3 show_results.py --quic            # show the tables again
```

Output in `quic_results/`:

- `quic_summary.csv`: per network, whether QUIC worked (downloads completed and identical), download time, throughput, path loss, network packet loss, QUIC retransmissions, ping RTT and QUIC RTT, and background.
- `quic_downloads_wifi.csv` and `quic_downloads_5g.csv`: one row per download, with the conditions at that moment.
- `perf_server_*.csv`: picoquic's own statistics for each connection.

## Live video over QUIC

`quic_live.py` sends a **live** stream at 4 Mbit/s and 30 frames per second, a new frame every 33 ms, from sta1 to srv using picoquic's quicperf media test. In a live stream a frame is only useful if it arrives before its deadline (150 ms by default). Each network is tested in two QUIC modes:

| Mode | How it works | What to expect |
|---|---|---|
| **QUIC streams** (reliable) | Lost packets are resent | Every frame arrives, but resent frames arrive **late** |
| **QUIC datagrams** (RFC 9221, unreliable) | Lost packets are not resent, like UDP | Frames are **on time**, but some are **missing** |

For every frame the receiver records when it was sent and when it arrived. From that the script reports frames received, lost and late, and the frame delay (minimum, average, 95th percentile and maximum), together with path loss, packet loss, RTT and background. Conditions change every 5 s, as in the other tests.

```bash
bash install_picoquic.sh            # (again) adds the per-frame report to picoquic
sudo python3 quic_live.py
sudo python3 quic_live.py --deadline 200 --time 30
python3 show_results.py --live
```

## Comparing the QUIC results

```bash
python3 show_results.py --quic       # file over QUIC
python3 show_results.py --live       # live over QUIC: streams vs datagrams
python3 show_results.py --compare    # per network: without live vs with live
python3 show_results.py --one        # Wi-Fi vs 5G, all QUIC tests in one table
```

---

## Notes

- A VM with at least 2 vCPUs is recommended. 420 Mbit/s of UDP is CPU-heavy, and if the VM is slow the 5G background may not reach its target. Check `bg_achieved_mbps` in `summary.csv`.
- The MPTCP test needs kernel 5.15 or newer and `mptcpize` from the `mptcpd` package. Ubuntu 22.04 and 24.04 have both.
