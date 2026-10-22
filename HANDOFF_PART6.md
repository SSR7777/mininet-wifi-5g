# Part 6 hand-off: where things stand (2026-10-21)

Read this first to continue Part 6 in another chat. The full design, commands and output files are in
[PART6_ADAPTIVE.md](PART6_ADAPTIVE.md).

## The goal

Send a 15 min video over Wi-Fi + 5G while the bandwidth of both changes (5G 700, Wi-Fi 500 Mbit/s,
crossing over time, as in the hand-drawn sketch):

| Condition | I frames | P/B frames |
|---|---|---|
| 5G signal strong | 5G QUIC **stream** | Wi-Fi QUIC **datagrams** |
| 5G signal weak (Wi-Fi better) | Wi-Fi QUIC **stream** | 5G QUIC **datagrams** |

## What was built

- **Main script:** `adaptive_test.py`. It reuses the Part 5 `iframe_test.py` functions.
- **Video:** BBB is looped to 15 min and encoded as H.264 at 4 Mbit/s, GOP 30, 3 B frames. It is cut into 20 s segments, each starting with an I frame.
- **Capacity:** follows `[adaptive] timeline` in `config.ini`. Background traffic stays at 60 % of nominal (420 / 300 Mbit/s), so a path whose capacity drops below that background is congested.
- **Path choice:** before every segment, a 1 s UDP probe at 300 Mbit/s runs on both paths. The better path gets the I frames. The sender switches only if the other path delivered at least 15 % more.
- **Comparison cases:** `adaptive` (measured), `oracle` (decides from the true timeline) and `static` (never switches; the Part 5 case 3 setup).
- **Docs updated:** README, GUIDE, COMMANDS, `.gitignore` and `push_part6.sh` (branch `part6-adaptive-switching`).
- **Tested offline** on the Mac with Mininet stubbed out: segments, reassembly, frame dependency, the decisions, the tables and the plot.

## Results so far: 4 min quick test (VM, saved in `adaptive_results_4min/`)

| Case | I frames arrived | GOPs lost | Frames viewable | Switches |
|---|---|---|---|---|
| Adaptive (measured) | 235/240 | 5 | 1898/7200 (26.4 %) | 1 |
| Adaptive (oracle) | 239/240 | 1 | 2161/7200 (30.0 %) | 2 |
| Static | 137/240 | **103** | 1468/7200 (20.4 %) | 0 |

**Switching works for the I frames.** When 5G was congested (100-220 s), static lost every GOP, with I frames arriving 2-28 s late. Adaptive moved the I frames to Wi-Fi and kept them at about 25 ms.

Viewable frames stay low in every case, for two reasons:
- **P/B on Wi-Fi datagrams (1 % loss):** about 1 in 4 P frames is lost, so only about 35 % of frames are viewable even when both paths are good. Part 5 measured the same.
- **P/B on a congested 5G:** P/B delay is about 3 s, so only the I frames are viewable (a slideshow).

**Oracle segment 4 reached 574/600 viewable** with I on Wi-Fi and P/B on an *uncongested* 5G (0.1 % loss).

Weak points of the measured decision:
- It reacts one segment late. In segment 11 it stayed on Wi-Fi while Wi-Fi became congested, and lost 5 GOPs.
- The probe saturates at 300 Mbit/s, so two healthy paths look equal.
- One probe returned 0 under congestion; it should be treated as "unknown".

## Running now

The full 15 min run (adaptive, oracle, static) was started in the VM on 2026-10-21:
```bash
sudo nohup python3 adaptive_test.py --video BBB.webm > adaptive.log 2>&1 &
```
It takes about 1.5-2 h. To check on it: `jobs -l; tail -5 ~/mininet-wifi-5g/adaptive.log`. If it shows
`Stopped`, run `sudo -v` and then `bg %1`. The results go to `adaptive_results/`.

## Next steps

1. Paste the 15 min tables (`python3 adaptive_test.py --show`), explain them, and add them to the
   Results section of PART6_ADAPTIVE.md.
2. Open questions for the user:
   - Should the switch use a **fixed bandwidth threshold** ("switch when 5G < X Mbit/s") instead of the current relative rule?
   - Should there be a 4th case, **adaptive tiered**? It would keep P frames on the better path and send only B frames on the worse path when the worse path is congested.
3. Save to GitHub with `bash push_part6.sh` (in the VM).
