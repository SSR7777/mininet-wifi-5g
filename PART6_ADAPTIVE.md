# Part 6 - Adaptive path switching for a long video (every step and command)

Branch: `part6-adaptive-switching` · Main script: `adaptive_test.py` · Settings: `[adaptive]` in `config.ini` ·
Every command of the whole project: [COMMANDS.md](COMMANDS.md)

**Idea:** Part 5 always sent the I frames over 5G and the P/B frames over Wi-Fi. That is only right while
5G is the better path. In real life the signal of each network changes during a long video, so the
sender has to keep checking and **swap the roles** when the other path becomes better:

| | I frames | P/B frames |
|---|---|---|
| **better path** | **QUIC stream** (lost pieces are resent) | |
| **worse path** | | **QUIC datagrams** (not resent, fast) |

```
             5G strong                     5G weak                      5G strong again
  5G    ==== I frames, stream ===>    ---- P/B, datagrams ---->    ==== I frames, stream ===>
  Wi-Fi ---- P/B, datagrams ---->     ==== I frames, stream ===>   ---- P/B, datagrams ---->
        |<--- 20 s --->|<--- 20 s --->|  ...  segments of a 15 min video  ...
```

## How it works

1. **The video.** `BBB.webm` is looped to 15 minutes and encoded like in Part 5 (H.264, 4 Mbit/s,
   one I frame per second, 3 B frames). It is cut into **segments of 20 s**; every segment starts with an
   I frame, so a segment can go on different paths than the one before it.
2. **The network changes.** The capacity of each path follows the `[adaptive] timeline` in `config.ini`
   (default below). The background traffic stays at 60 % of the *nominal* capacity
   (5G 420, Wi-Fi 300 Mbit/s), so a path whose capacity drops below its background is **congested**
   (long queue, lost packets) - that is what makes it the worse path.

   | Video time | 5G | Wi-Fi | Better path |
   |---|---|---|---|
   | 0-80 s | 700 | 500 | 5G |
   | 120-200 s | **300** | 500 | Wi-Fi |
   | 240-320 s | 700 | **250** | 5G |
   | 360-440 s | 700 | 500 | 5G |
   | 480-560 s | **250** | 500 | Wi-Fi |
   | 600-680 s | 700 | **250** | 5G |
   | 720-900 s | 700 | 500 | 5G |

   (straight lines in between, so the two capacities cross like in the sketch)
3. **The sender measures.** Before each segment it sends a 1 s UDP burst of 300 Mbit/s on both paths at
   the same time and counts what got through next to the background. The path that delivered more gets
   the I frames (QUIC stream), the other one the P/B frames (QUIC datagrams). It only switches when the
   other path delivered **15 % more** than the current one, so it does not flip back and forth.
4. **Three ways of sending, same timeline:**

   | `--scenarios` | How the path is chosen |
   |---|---|
   | `adaptive` | measured before every segment (the proposed method) |
   | `oracle` | from the true capacity timeline - the best any measurement could do |
   | `static` | never switches: I -> 5G stream, P/B -> Wi-Fi datagrams (Part 5 case 3) |

5. **What the viewer gets.** As in Part 5, the receiver puts every piece back in its place and only uses
   what arrived within 150 ms. A frame can be shown only if all its packets arrived **and** every frame it
   depends on can be shown.

## Commands

Where each command runs:

| Prompt | Machine |
|---|---|
| `ssr@Siddharthas-MacBook-Air ~ %` | Mac (copying files, pictures) |
| `siddhartha@mininet-wifi:~/mininet-wifi-5g$` | Ubuntu VM (emulation) |

**Mac** - copy the new files to the VM:
```bash
cd ~/Downloads/mininet-wifi-5g
scp adaptive_test.py config.ini PART6_ADAPTIVE.md push_part6.sh README.md GUIDE.md COMMANDS.md .gitignore siddhartha@192.168.252.4:~/mininet-wifi-5g/
ssh siddhartha@192.168.252.4
```

**VM** - a quick test first (4 min video: one 5G drop, one Wi-Fi drop; about 20 min for the 3 cases):
```bash
cd ~/mininet-wifi-5g
sudo python3 adaptive_test.py --video BBB.webm --minutes 4 --no-quality
```

**VM** - the full 15 min video, all three cases (about 75-90 min, plus 10-20 min the first time to encode
the 15 min video, which is kept for later runs; with `nohup` it keeps running if the SSH window closes):
```bash
sudo rm -rf adaptive_results
sudo nohup python3 adaptive_test.py --video BBB.webm > adaptive.log 2>&1 &
tail -f adaptive.log                      # Ctrl+C only stops watching
```

Other options:
```bash
sudo python3 adaptive_test.py --video BBB.webm --scenarios adaptive,static   # only some cases
sudo python3 adaptive_test.py --video BBB.webm --scale 0.1                    # 70 / 50 Mbit/s if the VM is too slow
sudo python3 adaptive_test.py --video BBB.webm --codec hevc                   # HEVC instead of H.264
python3 adaptive_test.py --show                                               # tables again
```

If the VM cannot generate the background traffic (`adaptive.log` shows the background far below
420 / 300 Mbit/s), run with `--scale 0.1`: every capacity, background and probe rate is divided by 10,
so the paths are congested at the same moments.

**Mac** - get the timeline picture and the video:
```bash
scp siddhartha@192.168.252.4:~/mininet-wifi-5g/adaptive_results/adaptive_timeline.png ~/Downloads/
open ~/Downloads/adaptive_timeline.png
scp siddhartha@192.168.252.4:~/mininet-wifi-5g/adaptive_results/adaptive/live.mp4 ~/Downloads/adaptive_live.mp4
open -a VLC ~/Downloads/adaptive_live.mp4
```

## Output (`adaptive_results/`)

| File | What it is |
|---|---|
| `adaptive_timeline.png` | Top: capacity of 5G and Wi-Fi over the video and their backgrounds. Below, one row per case: the colour shows which path carried the I frames (blue 5G, orange Wi-Fi), the line how many frames the viewer could see in each segment |
| `adaptive_summary.csv` | One row per case: I frames that arrived, GOPs lost, frames the viewer could see, PSNR/SSIM, number of switches, % of segments on the same path as the oracle, I and P/B delay |
| `<case>/segments.csv` | One row per segment: capacities, probe results, the path chosen, I/P/B frames that arrived, frames shown, loss and delay of each part |
| `<case>/live.mp4` | What the viewer saw (whole video) |
| `<case>/segNNN/` | Raw files of one segment (picoquic logs, probe results) |

## What to look for

- **Static** loses many frames while 5G is congested (120-200 s, 480-560 s): its I frames sit in the
  congested 5G queue, arrive after the 150 ms buffer, and whole GOPs are lost.
- **Adaptive** should move the I frames to Wi-Fi in those periods and back to 5G afterwards. Compare its
  frames-viewable line with the **oracle**: the gap is the cost of measuring (it decides once per segment
  and needs a segment to notice a change).
- **Switches** = how often the roles were swapped; with the default timeline the oracle switches 4 times.

## Results

*Run the full test in the VM and paste the summary table here (`python3 adaptive_test.py --show`).*

## Saving Part 6 to GitHub

```bash
bash push_part6.sh      # VM, in the project folder
```
