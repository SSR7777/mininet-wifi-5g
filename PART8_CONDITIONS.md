# Part 8 - Sending by measured network condition (congested / lossy / degraded)

The sender **measures delay and loss on both paths every 5 seconds**, decides the state of each path,
and sends

| Frames | Path | How |
|---|---|---|
| I frames | the **better** path | QUIC **stream** (lost pieces are resent) |
| P/B frames | the **worse** path | QUIC **datagrams** (fast, not resent) |

When both paths are good, the I frames stay on the **5G stream** (the main path).

## Network

| Path | Capacity | Delay (one way) | Loss | Background traffic (iPerf3 UDP) |
|---|---|---|---|---|
| Wi-Fi | 500 Mbit/s | 10 ms | 1 % | 10 % normally (50 Mbit/s) |
| 5G | 700 Mbit/s | 15 ms | 2 % | 10 % normally (70 Mbit/s) |

## How the state of a path is decided (every 5 s)

A small probe (200 packets/s, 100 bytes each, `probe.py`) runs on each path. The receiver records
when each probe was sent and when it arrived: **delay** = one-way delay, **loss** = probes that never
arrived. Thresholds: **Td = 50 ms**, **TL = 5 %**.

| State | Rule |
|---|---|
| good | delay < Td and loss < TL |
| degraded | delay **and** loss both rising over the last 30 s (still under Td / TL) |
| lossy | delay < Td and loss > TL |
| high delay | delay > Td and loss < TL |
| congested | delay > Td and loss > TL |

Better path = better state (good < degraded < lossy / high delay < congested). Same state on both:
the one with the lower `delay/Td + loss/TL` (it must be clearly better twice in a row before the
I frames move).

## The three scenarios (14-minute video, 6 phases of 2 min 20 s)

| Phase | congested | lossy | degraded |
|---|---|---|---|
| 1 | both normal | both normal | both normal |
| 2 | Wi-Fi background 10 % -> 90 % | Wi-Fi loss 8 % | Wi-Fi delay/loss slowly rising |
| 3 | both normal | both normal | both normal |
| 4 | 5G background 10 % -> 90 % | 5G loss 8 % | 5G delay/loss slowly rising |
| 5 | both normal | both normal | both normal |
| 6 | both 90 % | both 8 % | both rising (Wi-Fi faster) |

Each scenario is run twice: **fixed** (I always on the 5G stream, P/B always on Wi-Fi datagrams)
and **adaptive** (measured, switches automatically).

> Note: at 90 % background a path still has 10 % free (50-70 Mbit/s), far more than the 4 Mbit/s
> video, so the delay and loss may hardly change. To see real congestion, run the congested scenario
> again with `--bg-high 105`.

## Commands (on the VM)

```bash
cd ~/mininet-wifi-5g
sudo pkill -9 -f picoquicdemo; sudo mn -c
sudo python3 congestion_test.py --video BBB.webm < /dev/null 2>&1 | tee congestion_run.log   # about 3 h
```

Only one scenario (about 1 h): add `--scenarios congested` (or `lossy`, `degraded`).
Quick 2-minute check: add `--length 120 --no-video`.
More congestion: add `--bg-high 105`.

Tables again: `python3 congestion_test.py --show`

## Results (`congestion_results/`)

| File | What |
|---|---|
| `congestion_<scenario>.png` | delay and loss of both paths, state of each path, where the I frames went, frames shown |
| `congestion_<scenario>.mp4` | VLC video: original / fixed / adaptive, with the measured delay, loss and state |
| `<scenario>/summary.csv` | fixed vs adaptive for the whole video |
| `<scenario>/windows.csv` | every 5 s window: delay, loss, state of both paths, path of the I and P/B frames |
| `<scenario>/minutes.csv`, `phases.csv`, `segments.csv` | the same per minute, per phase, per 20 s |

Copy to the Mac:

```bash
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/congestion_results/congestion_*' ~/Downloads/
```
