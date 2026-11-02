# Part 9 - MoQ (MoQtail) layer on top of picoquic

Parts 3-8 sent the video with picoquic's test program (`picoquicdemo` / quicperf), one QUIC
connection per piece of video. Part 9 adds a real **MoQ layer** (Media over QUIC Transport):

| Layer | What it is |
|---|---|
| Video | H.264 in MPEG-TS, 30 frames/s, one I frame per second |
| **MoQ (new)** | MOQT **draft-16**, the version MoQtail uses (`moqt-16`): sessions, tracks, groups, objects |
| QUIC | picoquic (streams + datagrams) |
| Network | Mininet-WiFi: Wi-Fi 500 Mbit/s 10 ms 1 % / 5G 700 Mbit/s 15 ms 2 % |

The MoQ layer is written in C directly on picoquic (`moq/moqt.c`, `moq/moqt.h`). It speaks the same
wire format as MoQtail. It was tested against the real MoQtail relay and client (release 0.14.1,
see "Checking against MoQtail" below).

## How the video is carried

| MoQ term | Our video |
|---|---|
| Track | `moqpico/video` |
| Group | one GOP (a new group starts at every I frame, so 1 group per second) |
| Object | 940 bytes of video (5 MPEG-TS packets); every frame is cut into objects |
| Subgroup **stream** (reliable) | the objects of the **I frames**, on the **better** path |
| Object **datagram** (not resent) | the objects of the **P/B frames**, on the **worse** path |

The receiver (`srv`) opens **one MoQ session per path** (Wi-Fi and 5G) and subscribes to the same
track on both. The sender (`sta1`) decides, for every object, which session carries it.

The decision is the same as in Part 8:

1. Measure delay and loss of each path every 5 s.
2. Label each path good, degraded, lossy, high delay or congested (Td = 50 ms, TL = 5 %).
3. Put the I frames on the better path and the P/B frames on the other.

The controller writes `"<I path> <P/B path>"` (for example `5g wifi`) into a small file. The MoQ
publisher re-reads this file every 100 ms.

Messages used (draft-16):

- `CLIENT_SETUP` / `SERVER_SETUP`
- `SUBSCRIBE` → `SUBSCRIBE_OK`
- `PUBLISH_NAMESPACE` → `REQUEST_OK` (when publishing through a relay)
- `PUBLISH_DONE` at the end
- data: `SUBGROUP_HEADER` streams and `OBJECT_DATAGRAM`s

## Files

| File | What |
|---|---|
| `moq/moqt.h`, `moq/moqt.c` | the MoQ layer on picoquic (draft-16 messages, sessions, subgroup streams, datagrams) |
| `moq/moqpico.c` | `moqpico pub` (publisher) and `moqpico sub` (subscriber) |
| `moq/Makefile` | builds `moq/moqpico` against `~/picoquic` |
| `moq/moqtail_interop.sh` | optional: builds the real MoQtail relay and checks that they work together |
| `moq_test.py` | the Mininet test (same 3 scenarios as Part 8: congested, lossy, degraded; fixed vs adaptive) |

## Commands (on the VM)

```bash
cd ~/mininet-wifi-5g
make -C moq                     # build the MoQ layer (moq_test.py also does this by itself)

# 1. Self-test, no Mininet (about 1 min): 12 s video, I on one session, P/B on the other, switch at 6 s
sudo python3 moq_test.py --selftest

# 2. Quick Mininet check (about 6 min): congested scenario, 2 minutes
sudo pkill -9 -f picoquicdemo; sudo mn -c
sudo python3 moq_test.py --video BBB.webm --scenarios congested --bg-high 120 --length 120 --no-video

# 3. Full run (about 3 h): all 3 scenarios, fixed + adaptive, 14-min video
sudo python3 moq_test.py --video BBB.webm < /dev/null 2>&1 | tee moq_run.log

python3 moq_test.py --show       # the tables again
```

`--bg-high 120` makes the congested phases really congested (at 90 % the path still has free
capacity; see Part 8).

## Results (`moq_results/`)

| File | What |
|---|---|
| `moq_<scenario>.png` | delay and loss of both paths, state of each path, where the I frames went, frames shown |
| `moq_<scenario>.mp4` | VLC video: original / fixed / adaptive, with the measured state |
| `<scenario>/summary.csv` | fixed vs adaptive for the whole video (same columns as Part 8, so they can be compared) |
| `<scenario>/moq_objects.csv` | how many MoQ objects went on each path as stream / datagram |
| `<scenario>/h264/<variant>/objects.csv` | every object received: path, stream/datagram, group, object, frame, delay |
| `<scenario>/h264/<variant>/pub.log`, `sub.log` | the MoQ publisher and subscriber logs |

```bash
scp 'siddhartha@192.168.252.4:~/mininet-wifi-5g/moq_results/moq_*' ~/Downloads/
```

## What was checked before delivery (in the cloud, without Mininet)

| Test | Result |
|---|---|
| Self-test: 12 s video over two MoQ sessions, switch at 6 s | 25 662 / 25 662 TS packets identical, switch done within 0.1 s |
| 8 % loss on the "Wi-Fi" session, P/B datagrams there | I 403/403 objects; P/B lose about 7 % (datagrams are not resent) |
| 8 % loss on the "Wi-Fi" session, I stream there | I 403/403 objects, but average delay 119 ms and maximum 1 s (resent pieces), so some arrive later than the 150 ms buffer |
| Same 4 Mbit/s video through an emulated Wi-Fi (10 ms, 1 %) and 5G (15 ms, 2 %), congestion control **newreno** | only 14/30 I frames in time, I delay 98 ms avg / 229 ms p95 |
| Same, congestion control **BBR** (the default now, as `picoquicdemo` in Parts 3-8) | 30/30 I frames in time, I delay 32 ms avg / 60 ms p95 |
| `moq_test.py` pipeline on loopback (30 s, fixed + adaptive) | 900/900 frames, PSNR identical, tables and graph made |
| MoQtail client publishes → MoQtail relay → moqpico | 20/20 objects |
| moqpico publishes → MoQtail relay → MoQtail client | 6/6 stream objects, errors 0, gaps 0 |
| moqpico → MoQtail relay → moqpico | 6/6 stream objects + 54/54 datagrams, PUBLISH_DONE forwarded |

The loss tests show the rule in action:

- A lossy path hurts **datagrams** by losing them.
- A lossy path hurts **streams** by delaying them.

So the I frames should go on the better path.

## Checking against MoQtail (optional, needs about 2 GB of disk)

```bash
bash moq/moqtail_interop.sh
```

This script does the following:

1. Installs Rust (if missing).
2. Downloads MoQtail `relay@0.14.1`, the last release with draft-16 over raw QUIC.
3. Applies a small patch (`moq/moqtail-0.14.1-ipv4.patch`). Without it, the relay does not start
   on machines with IPv6 turned off, like our VM.
4. Runs the three interop tests above.

Newer MoQtail versions use draft-18 and later, which are not compatible with draft-16.

## Transport settings (why the first Mininet runs were bad, and what was fixed)

The first two Mininet runs gave only 6/120 I frames in time (I-frame delay 320-440 ms), while Part 8
with `picoquicdemo` had 120/120 and 37 ms. Three settings of `moqpico` were different from
`picoquicdemo`; all three now match Parts 3-8:

| Setting | Before | Now | Why it matters |
|---|---|---|---|
| UDP GSO | on | **off** (like `picoquicdemo -0`) | with GSO the kernel hands up to ~40 QUIC packets to netem as ONE packet, so one emulated 2 % loss drops all of them and the I-frame stream keeps waiting for resends (`--gso` turns it back on) |
| Congestion control | newreno (picoquic's library default) | **BBR** (like `picoquicdemo`) | newreno halves its window at every random loss of our 1-2 % paths (`--cc` to change) |
| Flow-control credit per stream | 64 KB | **16 MB** | one I frame (100-200 KB) is one MoQ subgroup stream; with 64 KB it waits for MAX_STREAM_DATA several times |

Every 10 s the publisher now also prints, per session, the RTT, congestion window, pacing rate and
lost/sent packets that picoquic sees, so a slow path is easy to spot in `pub.log`.

## Delivery timeout for late I-frame groups (`--stream-age`, default 500 ms)

The first 14-minute run showed I-frame delays of 1-4 s in the congested scenario. The I frames go
on a reliable stream, and a congested path cannot keep up, so every late group queued up behind
the one before it.

MoQ's answer is the delivery timeout: the publisher gives up a group that is too old. `moqpico`
now does this: an I-frame group stream that is still not delivered 500 ms after it was opened is
reset (`RESET_STREAM`, code `DELIVERY_TIMEOUT` = 0x2). The connection then works on the newest
I frame instead of a backlog. Streams that already arrived are never reset.

- The publisher status line shows how many were given up: `5g: 1496 stream obj (23 groups too late)`.
- Change it with `sudo python3 moq_test.py ... --stream-age 300`, or switch it off with `--stream-age 0`.

Checked in the cloud with an emulated path (30 s video, I frames on the 5G stream):

| 5G path | Without the timeout | With the timeout (500 ms) |
|---|---|---|
| 15 ms, 2 % loss (normal) | 30/30 I frames in 150 ms, 0 groups reset | 30/30, 0 groups reset |
| 15 ms, 8 % loss (lossy) | 29/30, I delay 52 ms | 28/30, I delay 53 ms, 0 groups reset |
| 165 ms, 20 % loss (congested at 120 %) | I delay 2.4 s average, growing to 6.2 s | 0.76 s average, not growing; 23 of 30 groups given up |

On a congested path no I frame can arrive in time anyway. The point of the timeout is that the
delay stops growing, so when the path gets better again, or the adaptive controller moves the
I frames to the other path, the video recovers at once instead of first draining seconds of old
groups.

### Two fixes after the first 14-minute runs with the timeout

1. **Groups already sent but not acknowledged were never given up.** The check "was this stream
   delivered?" read picoquic's acknowledgement list the wrong way round, so only groups still
   waiting in the send queue were reset. A group sent just before a path went bad was resent for
   a long time instead. In the emulator, one I frame arrived 37 s late. Fixed: now every group
   that is not fully acknowledged after 500 ms is reset.
2. **Watchdog for a stuck congestion window (`--cc-floor`, default 64 KB).** In the Mininet run,
   Adaptive's 5G session kept a 34 KB window for a whole minute after switching back, on a path
   with 30 ms RTT. One I frame (about 210 KB) then needs about 7 round trips and misses the 150 ms
   buffer. Minute 12 showed 102 frames instead of 608 for Fixed. If the session carrying the I
   frames has a window below 64 KB for 2 s, while its RTT is less than 2× its minimum (no queue) and
   it loses less than 5 % of its packets, the session's BBR is restarted. This happens at most once
   every 10 s. Each restart is printed in `pub.log`. Use `--cc-floor 0` to switch it off.

| Emulator check (I frames on 5G; 5G bad from 31 to 71 s; I frames on Wi-Fi from 33 to 73 s) | Before | After |
|---|---|---|
| Largest I-object delay | 37 500 ms | 650 ms |
| Average I delay, 70-80 s | 3144 ms | 18 ms |
| Good path (15 ms, 2 %), 30 s: groups reset / watchdog restarts | 0 / - | 0 / 0 |
| Congested path (165 ms, 20 %): groups reset / watchdog restarts | 23 of 30 | 30 of 30 / 0 |

## Limits

- Only the parts of MoQ the video needs are implemented: setup, subscribe/unsubscribe, publish
  namespace, publish done, subgroup streams, datagrams.
  - FETCH, TRACK_STATUS and SUBSCRIBE_NAMESPACE are answered with `REQUEST_ERROR` (not supported).
- Sending each object on only one of the two sessions is our multipath extension; plain MoQ sends a
  track on one session.
  - Through a relay there is one session, so the I frames go on the stream and the P/B frames as
    datagrams on that same session.
- One object must fit in one QUIC datagram, which is why objects are 940 bytes (`--chunk`).
