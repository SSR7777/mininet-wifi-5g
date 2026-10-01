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

## Notes

- A VM with at least 2 vCPUs is recommended. 420 Mbit/s of UDP is CPU-heavy, and if the VM is slow the 5G background may not reach its target. Check `bg_achieved_mbps` in `summary.csv`.
- The MPTCP test needs kernel 5.15 or newer and `mptcpize` from the `mptcpd` package. Ubuntu 22.04 and 24.04 have both.
