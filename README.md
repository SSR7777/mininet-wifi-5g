# Wi-Fi + Emulated 5G Dual-Path Emulation (Mininet-WiFi)

Emulates a client with two access paths, Wi-Fi and 5G, to one server. It uses
[Mininet-WiFi](https://github.com/intrig-unicamp/mininet-wifi), and iPerf3 adds background load to both paths.

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

## Setup (Ubuntu VM)

```bash
git clone <this-repo-url>
cd mininet-wifi-5g
bash install.sh          # installs Mininet-WiFi, iperf3, mptcpd (about 10–20 min)
```

## Run

```bash
sudo python3 wifi_5g_topology.py                 # 30 s experiment
sudo python3 wifi_5g_topology.py --duration 60   # longer run
sudo python3 wifi_5g_topology.py --cli           # experiment, then Mininet CLI
sudo python3 wifi_5g_topology.py --no-test       # only build the topology + CLI
sudo python3 wifi_5g_topology.py --mptcp         # also one MPTCP flow over both paths
sudo mn -c                                       # clean up after a crash
```

### What the script does

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
- `video_summary.csv` lists, per network, frames received, decoder errors, and PSNR and SSIM (picture quality compared with the original).

With UDP, lost packets show up as damaged or frozen frames, so 1 % loss on Wi-Fi is clearly visible while 0.1 % on 5G barely is. With TCP, lost packets are resent, so the picture is perfect but delivery can take longer than the video's length.

## Note on Wi-Fi shaping

Mininet-WiFi puts its own rate limit on the radio interface, based on mode and distance (about 10 Mbit/s in mode g), and that overrides `TCLink`. The script therefore applies the Wi-Fi path's rate, delay and loss with `tc netem` directly on `sta1-wlan0` and `srv-wifi` after the network starts.

## Notes

- A VM with at least 2 vCPUs is recommended. 420 Mbit/s of UDP is CPU-heavy, and if the VM is slow the 5G background may not reach its target. Check `bg_achieved_mbps` in `summary.csv`.
- The MPTCP test needs kernel 5.15 or newer and `mptcpize` from the `mptcpd` package. Ubuntu 22.04 and 24.04 have both.
