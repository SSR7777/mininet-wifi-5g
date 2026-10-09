#!/usr/bin/env python3
"""
Print the video test results as detailed tables in the terminal.

  python3 show_results.py                  # reads video_results/
  python3 show_results.py --dir my_run     # another results folder

Tables:
  1. Settings used (config.ini)
  2. Changing conditions, every step, for each network
  3. Full results: time, path loss, packet loss, delay, background, quality
"""
import argparse
import csv
import os

LABEL = {"wifi": "Wi-Fi", "5g": "5G"}


def table(title, header, rows, align=None):
    """Print an ASCII table with a title."""
    cols = len(header)
    align = align or (["<"] + [">"] * (cols - 1))
    w = [len(str(h)) for h in header]
    for r in rows:
        if r is None:
            continue
        for i, c in enumerate(r):
            w[i] = max(w[i], len(str(c)))
    line = "+" + "+".join("-" * (x + 2) for x in w) + "+"
    total = len(line)
    print()
    print("=" * total)
    print(" " + title)
    print("=" * total)
    print(line)
    print("|" + "|".join(f" {str(h):^{w[i]}} " for i, h in enumerate(header)) + "|")
    print(line.replace("-", "="))
    for r in rows:
        if r is None:                      # section separator
            print(line)
            continue
        print("|" + "|".join(f" {str(c):{align[i]}{w[i]}} " for i, c in enumerate(r)) + "|")
    print(line)


def num(x, d=2, unit=""):
    try:
        return f"{float(x):.{d}f}{unit}"
    except (TypeError, ValueError):
        return "-"


def _num(x, d=2, unit=""):
    return num(x, d, unit)


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return list(csv.DictReader(fh))


def show(d):
    summ = {r["path"]: r for r in read_csv(os.path.join(d, "video_summary.csv"))}
    if not summ:
        raise SystemExit(f"No results in {d}/ - run: sudo python3 video_stream.py --video BBB.webm")
    nets = [n for n in ("wifi", "5g") if n in summ]
    cond = {n: read_csv(os.path.join(d, f"conditions_{n}.csv")) for n in nets}
    varied = any(cond.values())

    # ---------------- 1. settings ----------------
    rows = []
    for n in nets:
        r = summ[n]
        rows.append([LABEL[n], r["transport"].upper(), num(r["configured_loss_pct"], 2, " %"),
                     f"{r['configured_delay_ms']} ms", f"{r['configured_rtt_ms']} ms",
                     (f"{float(r['background_target_mbps']):.0f} Mbit/s"
                      + (" (avg)" if cond.get(n) else "")
                      if r["background_target_mbps"] not in ("", "0", "0.0") else "off"),
                     "random every step" if cond.get(n) else "fixed"])
    table("1. TEST SETTINGS",
          ["Network", "Transport", "Loss", "Delay 1-way", "RTT", "Background", "Conditions"], rows)

    # ---------------- 2. changing conditions ----------------
    if varied:
        for n in nets:
            if not cond[n]:
                continue
            rows = []
            st = cond[n]
            step = (int(float(st[1]["start_s"])) - int(float(st[0]["start_s"]))) if len(st) > 1 else 5
            for c in st:
                t0 = int(float(c["start_s"]))
                rows.append([f"{t0}-{t0 + step}s",
                             num(float(c["background_pct"] or 0) * 100, 0, " %"),
                             num(c["background_target_mbps"], 0),
                             num(c["background_achieved_mbps"], 1),
                             num(c["capacity_mbps"], 0),
                             num(c["delay_ms"], 1),
                             num(c["loss_pct"], 2),
                             num(c["measured_rtt_ms"], 1),
                             num(c["video_mbps"], 2),
                             c["video_packets_sent"], c["video_packets_received"],
                             num(c["video_loss_pct"], 2)])
            table(f"2. CHANGING CONDITIONS DURING THE VIDEO - {LABEL[n]}",
                  ["Time", "Bg share", "Bg target", "Bg real", "Capacity", "Delay",
                   "Loss set", "RTT", "Video", "Pkts sent", "Pkts rcvd", "Pkt loss"],
                  rows)
            print("  Bg target / Bg real / Capacity / Video in Mbit/s, Delay / RTT in ms, "
                  "Loss set / Pkt loss in %")

    # ---------------- 3. full results ----------------
    g = lambda k, d=2, u="": [num(summ[n].get(k), d, u) for n in nets]
    s = lambda k: [summ[n].get(k) or "-" for n in nets]
    rows = [
        ["TIME TO SEND", *[""] * len(nets)],
        ["  Video length", *g("video_length_s", 1, " s")],
        ["  Video size", *g("video_mb", 2, " MB")],
        ["  Transfer time (start -> last packet)", *g("transfer_time_s", 2, " s")],
        ["  First packet arrived after", *g("first_packet_s", 2, " s")],
        ["  Sender finished after", *g("sender_time_s", 2, " s")],
        ["  Throughput", *g("throughput_mbps", 2, " Mbit/s")],
        None,
        ["PATH LOSS (radio)", *[""] * len(nets)],
        ["  Distance to access point",
         *[num(summ[n]["radio_distance_m"], 0, " m") if summ[n]["radio_distance_m"] else "n/a"
           for n in nets]],
        ["  Received signal",
         *[num(summ[n]["radio_signal_dbm"], 1, " dBm") if summ[n]["radio_signal_dbm"] else "n/a"
           for n in nets]],
        ["  Path loss",
         *[num(summ[n]["radio_path_loss_db"], 1, " dB") if summ[n]["radio_path_loss_db"]
           else "n/a (wired)" for n in nets]],
        None,
        ["PACKET LOSS", *[""] * len(nets)],
        ["  Configured loss", *g("configured_loss_pct", 2, " %")],
        ["  Packets sent", *s("packets_sent")],
        ["  Packets received", *s("packets_received")],
        ["  Packets lost",
         *[str(int(summ[n]["packets_sent"]) - int(summ[n]["packets_received"]))
           if summ[n]["packets_sent"] and summ[n]["packets_received"] else "-" for n in nets]],
        ["  Measured packet loss", *g("packet_loss_pct", 2, " %")],
        ["  TCP retransmissions", *s("tcp_retransmissions")],
        None,
        ["DELAY", *[""] * len(nets)],
        ["  Configured one-way delay", *g("configured_delay_ms", 1, " ms")],
        ["  Configured RTT", *g("configured_rtt_ms", 1, " ms")],
        ["  Measured RTT (ping)", *g("measured_rtt_ms", 1, " ms")],
        ["  Measured one-way (RTT/2)", *g("measured_one_way_ms", 1, " ms")],
        None,
        ["BACKGROUND TRAFFIC", *[""] * len(nets)],
        ["  Target" + (" (average)" if varied else ""), *g("background_target_mbps", 1, " Mbit/s")],
        ["  Achieved" + (" (average)" if varied else ""), *g("background_achieved_mbps", 1, " Mbit/s")],
        ["  Background UDP loss", *g("background_udp_loss_pct", 2, " %")],
        None,
        ["PICTURE QUALITY", *[""] * len(nets)],
        ["  Frames received / sent",
         *[f"{summ[n]['frames_received']} / {summ[n]['frames_sent']}" for n in nets]],
        ["  Decoder errors", *s("decode_errors")],
        ["  PSNR (higher = better)", *g("psnr_db", 1, " dB")],
        ["  SSIM (1 = identical)", *g("ssim", 4)],
    ]
    table("3. VIDEO RESULTS - " + " vs ".join(LABEL[n] for n in nets),
          ["Measurement", *[LABEL[n] for n in nets]], rows)
    print(f"  Files: {os.path.abspath(d)}/video_summary.csv"
          + (", conditions_wifi.csv, conditions_5g.csv" if varied else ""))


def show_quic(d):
    summ = {r["path"]: r for r in read_csv(os.path.join(d, "quic_summary.csv"))}
    if not summ:
        raise SystemExit(f"No QUIC results in {d}/ - run: sudo python3 quic_test.py --video BBB.webm")
    nets = [n for n in ("wifi", "5g") if n in summ]

    for n in nets:
        rows = []
        for r in read_csv(os.path.join(d, f"quic_downloads_{n}.csv")):
            rows.append([r["download"], num(r["start_s"], 1, " s"),
                         num(r["background_pct"], 0, " %"), r["background_target_mbps"],
                         r["capacity_mbps"], num(r["delay_ms"], 1), num(r["loss_pct"], 2),
                         num(r["download_s"], 2), num(r["throughput_mbps"], 1),
                         r["packets_sent"], r["retransmissions"], num(r["quic_srtt_ms"], 1),
                         "OK" if r["identical"] == "yes" else "FAILED"])
        table(f"QUIC DOWNLOADS OVER {LABEL[n]} (one row per download)",
              ["#", "Start", "Bg share", "Bg Mbit/s", "Capacity", "Delay", "Loss set",
               "Time s", "Mbit/s", "Pkts sent", "Retrans", "QUIC RTT", "File"], rows)
        print("  Capacity in Mbit/s, Delay / QUIC RTT in ms, Loss set in %. "
              "File OK = received file is identical to the original")

    g = lambda k, dd=2, u="": [num(summ[n].get(k), dd, u) for n in nets]
    s = lambda k: [summ[n].get(k) or "-" for n in nets]
    rows = [
        ["DOES QUIC WORK?", *[""] * len(nets)],
        ["  Protocol / application", *[f"QUIC over UDP / {summ[n]['alpn'] or '-'}" for n in nets]],
        ["  Downloads completed", *[f"{summ[n]['downloads_ok']} of {summ[n]['downloads']}" for n in nets]],
        ["  Every file identical to original", *[summ[n]["all_identical"].upper() for n in nets]],
        ["  Congestion control", *s("congestion_control")],
        None,
        ["TIME TO SEND", *[""] * len(nets)],
        ["  File size", *g("file_mb", 2, " MB")],
        ["  Average download time", *g("avg_download_s", 2, " s")],
        ["  Fastest / slowest",
         *[f"{num(summ[n]['min_download_s'], 2)} / {num(summ[n]['max_download_s'], 2)} s" for n in nets]],
        ["  Average throughput", *g("avg_throughput_mbps", 1, " Mbit/s")],
        None,
        ["PATH LOSS (radio)", *[""] * len(nets)],
        ["  Path loss", *[num(summ[n]["radio_path_loss_db"], 1, " dB") if summ[n]["radio_path_loss_db"]
                          else "n/a (wired)" for n in nets]],
        None,
        ["PACKET LOSS", *[""] * len(nets)],
        ["  Configured loss", *g("configured_loss_pct", 2, " %")],
        ["  QUIC packets sent (server)", *s("quic_packets_sent")],
        ["  QUIC packets received (client)", *s("quic_packets_received")],
        ["  Packets lost in the network", *g("network_packet_loss_pct", 2, " %")],
        ["  Retransmitted by QUIC", *g("retransmission_pct", 2, " %")],
        ["  Retransmissions per download", *g("avg_retransmissions", 1)],
        None,
        ["DELAY", *[""] * len(nets)],
        ["  Configured RTT", *g("configured_rtt_ms", 1, " ms")],
        ["  Measured RTT (ping)", *g("ping_rtt_ms", 1, " ms")],
        ["  QUIC smoothed RTT", *g("quic_srtt_ms", 1, " ms")],
        ["  QUIC minimum RTT", *g("quic_min_rtt_ms", 1, " ms")],
        None,
        ["BACKGROUND TRAFFIC", *[""] * len(nets)],
        ["  Target (average)", *g("background_target_mbps", 1, " Mbit/s")],
        ["  Achieved (average)", *g("background_achieved_mbps", 1, " Mbit/s")],
        ["  Conditions", *s("conditions")],
    ]
    table("QUIC RESULTS - " + " vs ".join(LABEL[n] for n in nets),
          ["Measurement", *[LABEL[n] for n in nets]], rows)
    print(f"  Files: {os.path.abspath(d)}/quic_summary.csv, quic_downloads_*.csv")


def show_live(d):
    rows_all = read_csv(os.path.join(d, "live_summary.csv"))
    if not rows_all:
        raise SystemExit(f"No live results in {d}/ - run: sudo python3 quic_live.py")
    ML = {"stream": "streams", "dgram": "datagrams"}
    cols = [(r["path"], r["mode"]) for r in rows_all]
    R = {(r["path"], r["mode"]): r for r in rows_all}
    head = [f"{LABEL[n]} {ML[m]}" for n, m in cols]
    dl = rows_all[0]["deadline_ms"]

    for n, m in cols:
        rows = []
        for r in read_csv(os.path.join(d, f"live_steps_{n}_{m}.csv")):
            if not r["frames_expected"] or r["frames_expected"] == "0":
                continue
            t0 = int(float(r["start_s"]))
            rows.append([f"{t0}-{t0 + 5}s", num(r["background_pct"], 0, " %"),
                         r["background_target_mbps"], r["capacity_mbps"], num(r["delay_ms"], 1),
                         num(r["loss_pct"], 2), num(r["ping_rtt_ms"], 1),
                         f"{r['frames_received']} / {r['frames_expected']}",
                         num(r["avg_frame_delay_ms"], 1), r["late_frames"] or "0"])
        table(f"LIVE STREAM OVER {LABEL[n]} - QUIC {ML[m].upper()} (every 5 s)",
              ["Time", "Bg share", "Bg Mbit/s", "Capacity", "Delay", "Loss set", "RTT",
               "Frames rcvd/sent", "Frame delay", f"Late >{float(dl):g}ms"], rows)
        print("  Capacity in Mbit/s, Delay / RTT / Frame delay in ms, Loss set in %")

    g = lambda k, dd=2, u="": [num(R[c].get(k), dd, u) for c in cols]
    s = lambda k: [R[c].get(k) or "-" for c in cols]
    rows = [
        ["LIVE VIDEO RESULT (4 Mbit/s, 30 fps)", *[""] * len(cols)],
        ["  Frames sent", *s("frames_expected")],
        ["  Frames received", *s("frames_received")],
        ["  Frames lost (never arrived)", *s("frames_lost")],
        [f"  Frames late (> {float(dl):g} ms)", *s("frames_late")],
        ["  Frames usable (on time)", *g("on_time_pct", 1, " %")],
        None,
        ["FRAME DELAY (sent -> received)", *[""] * len(cols)],
        ["  Minimum", *g("delay_min_ms", 1, " ms")],
        ["  Average", *g("delay_avg_ms", 1, " ms")],
        ["  95 % of frames below", *g("delay_p95_ms", 1, " ms")],
        ["  Maximum", *g("delay_max_ms", 1, " ms")],
        None,
        ["PATH LOSS (radio)", *[""] * len(cols)],
        ["  Path loss", *[num(R[c]["radio_path_loss_db"], 1, " dB") if R[c]["radio_path_loss_db"]
                          else "n/a (wired)" for c in cols]],
        None,
        ["PACKET LOSS", *[""] * len(cols)],
        ["  Configured loss", *g("configured_loss_pct", 2, " %")],
        ["  Packets sent / received", *[f"{R[c]['packets_sent']} / {R[c]['packets_received']}" for c in cols]],
        ["  Packets lost in the network", *g("network_packet_loss_pct", 2, " %")],
        ["  Packets resent by QUIC", *s("quic_retransmissions")],
        None,
        ["DELAY", *[""] * len(cols)],
        ["  Configured RTT", *g("configured_rtt_ms", 1, " ms")],
        ["  Measured RTT (ping)", *g("ping_rtt_ms", 1, " ms")],
        ["  QUIC minimum RTT", *g("quic_min_rtt_ms", 1, " ms")],
        None,
        ["BACKGROUND TRAFFIC", *[""] * len(cols)],
        ["  Target / achieved (avg)",
         *[f"{num(R[c]['background_target_mbps'], 0)} / {num(R[c]['background_achieved_mbps'], 0)} Mbit/s"
           for c in cols]],
        ["  Conditions", *s("conditions")],
    ]
    table("LIVE VIDEO OVER QUIC - streams (reliable) vs datagrams (unreliable)", ["Measurement", *head], rows)
    print(f"  Files: {os.path.abspath(d)}/live_summary.csv, live_steps_*.csv, frames_*.csv")


def show_compare(qdir, ldir):
    """QUIC without live stream (file download) vs with live stream."""
    Q = {r["path"]: r for r in read_csv(os.path.join(qdir, "quic_summary.csv"))}
    L = {(r["path"], r["mode"]): r for r in read_csv(os.path.join(ldir, "live_summary.csv"))}
    if not Q or not L:
        raise SystemExit("Need both results: run  sudo python3 quic_test.py --video BBB.webm  "
                         "and  sudo python3 quic_live.py  first")
    fx = lambda v, d=1, u="": num(v, d, u)
    for n in [x for x in ("wifi", "5g") if x in Q]:
        q = Q[n]
        ls, ld = L.get((n, "stream"), {}), L.get((n, "dgram"), {})
        rpct = lambda r: (fx(100.0 * float(r["quic_retransmissions"]) / float(r["quic_packets_sent"]), 2, " %")
                          if r.get("quic_retransmissions") and r.get("quic_packets_sent")
                          and float(r["quic_packets_sent"]) > 0 else "-")
        frames = lambda r: (f"{r['frames_received']} / {r['frames_expected']} frames" if r else "-")
        rows = [
            ["WHAT IS SENT", "", "", ""],
            ["  Data", "8.8 MB video file", "live video 4 Mbit/s", "live video 4 Mbit/s"],
            ["  How it is sent", "as fast as possible", "real time, 30 frames/s", "real time, 30 frames/s"],
            ["  QUIC mode", "streams (reliable)", "streams (reliable)", "datagrams (unreliable)"],
            None,
            ["DELIVERY", "", "", ""],
            ["  Delivered",
             f"{q['downloads_ok']} / {q['downloads']} files", frames(ls), frames(ld)],
            ["  Complete / identical",
             q["all_identical"].upper(),
             "YES" if ls and ls["frames_lost"] == "0" else "NO", "YES" if ld and ld["frames_lost"] == "0" else
             f"NO ({ld.get('frames_lost', '-')} lost)"],
            ["  Usable on time (live deadline)", "n/a (not live)",
             fx(ls.get("on_time_pct"), 1, " %"), fx(ld.get("on_time_pct"), 1, " %")],
            None,
            ["TIME / SPEED", "", "", ""],
            ["  Time to deliver", f"{fx(q['avg_download_s'], 2)} s per file",
             *[f"{int(float(ls['frames_expected']) / 30)} s (real time)" if ls else "-"] * 2],
            ["  Speed", fx(q["avg_throughput_mbps"], 1, " Mbit/s"), "4 Mbit/s (video rate)", "4 Mbit/s (video rate)"],
            ["  Frame delay average", "n/a", fx(ls.get("delay_avg_ms"), 1, " ms"), fx(ld.get("delay_avg_ms"), 1, " ms")],
            ["  Frame delay 95 % / max", "n/a",
             f"{fx(ls.get('delay_p95_ms'))} / {fx(ls.get('delay_max_ms'))} ms",
             f"{fx(ld.get('delay_p95_ms'))} / {fx(ld.get('delay_max_ms'))} ms"],
            None,
            ["PATH LOSS (radio)", "", "", ""],
            ["  Path loss", *([fx(q["radio_path_loss_db"], 1, " dB")] * 3 if q["radio_path_loss_db"]
                              else ["n/a (wired)"] * 3)],
            None,
            ["PACKET LOSS", "", "", ""],
            ["  Configured loss", *([fx(q["configured_loss_pct"], 2, " %")] * 3)],
            ["  Lost in the network", fx(q["network_packet_loss_pct"], 2, " %"),
             fx(ls.get("network_packet_loss_pct"), 2, " %"), fx(ld.get("network_packet_loss_pct"), 2, " %")],
            ["  Resent by QUIC (% of packets)", fx(q["retransmission_pct"], 2, " %"), rpct(ls), rpct(ld)],
            None,
            ["DELAY", "", "", ""],
            ["  Configured RTT", *([fx(q["configured_rtt_ms"], 1, " ms")] * 3)],
            ["  Measured RTT (ping)", fx(q["ping_rtt_ms"], 1, " ms"),
             fx(ls.get("ping_rtt_ms"), 1, " ms"), fx(ld.get("ping_rtt_ms"), 1, " ms")],
            ["  QUIC minimum RTT", fx(q["quic_min_rtt_ms"], 1, " ms"),
             fx(ls.get("quic_min_rtt_ms"), 1, " ms"), fx(ld.get("quic_min_rtt_ms"), 1, " ms")],
            None,
            ["BACKGROUND TRAFFIC", "", "", ""],
            ["  Target / achieved (avg)",
             f"{fx(q['background_target_mbps'], 0)} / {fx(q['background_achieved_mbps'], 0)} Mbit/s",
             f"{fx(ls.get('background_target_mbps'), 0)} / {fx(ls.get('background_achieved_mbps'), 0)} Mbit/s",
             f"{fx(ld.get('background_target_mbps'), 0)} / {fx(ld.get('background_achieved_mbps'), 0)} Mbit/s"],
        ]
        table(f"QUIC OVER {LABEL[n]}: WITHOUT LIVE STREAM vs WITH LIVE STREAM",
              ["Measurement", "Without live (file)", "Live - QUIC streams", "Live - QUIC datagrams"], rows)
    print(f"  From {os.path.abspath(qdir)}/quic_summary.csv and {os.path.abspath(ldir)}/live_summary.csv")


def show_one(qdir, ldir):
    """All QUIC results in ONE table: Wi-Fi vs 5G, with the better network."""
    Q = {r["path"]: r for r in read_csv(os.path.join(qdir, "quic_summary.csv"))}
    L = {(r["path"], r["mode"]): r for r in read_csv(os.path.join(ldir, "live_summary.csv"))}
    if not (Q.get("wifi") and Q.get("5g")) or not L:
        raise SystemExit("Need both networks in quic_results/ and live_results/: run quic_test.py and quic_live.py")
    fl = lambda v: float(v) if v not in (None, "", "-") else None

    def row(label, w, g, unit="", d=1, better="low"):
        a, b = fl(w), fl(g)
        win = "-"
        if a is not None and b is not None and better and abs(a - b) > 1e-9:
            win = "Wi-Fi" if (a < b) == (better == "low") else "5G"
        elif a is not None and b is not None and better:
            win = "same"
        return [label, num(w, d, unit), num(g, d, unit), win]

    def txt(label, w, g, win="-"):
        return [label, w, g, win]

    qw, qg = Q["wifi"], Q["5g"]
    sw, sg = L.get(("wifi", "stream"), {}), L.get(("5g", "stream"), {})
    dw, dg = L.get(("wifi", "dgram"), {}), L.get(("5g", "dgram"), {})
    rp = lambda r: (100.0 * float(r["quic_retransmissions"]) / float(r["quic_packets_sent"])
                    if r.get("quic_retransmissions") and fl(r.get("quic_packets_sent")) else None)
    rows = [
        ["NETWORK", "", "", ""],
        txt("  Capacity / delay / loss (config)", "130 Mbit/s, 10 ms, 1 %", "700 Mbit/s, 15 ms, 0.1 %"),
        txt("  Path loss (radio)", num(qw["radio_path_loss_db"], 1, " dB"), "n/a (wired)"),
        row("  QUIC minimum RTT", qw["quic_min_rtt_ms"], qg["quic_min_rtt_ms"], " ms"),
        row("  Background traffic (avg)", qw["background_achieved_mbps"], qg["background_achieved_mbps"],
            " Mbit/s", 0, None),
        None,
        ["1. WITHOUT LIVE - file over QUIC streams", "", "", ""],
        txt("  Files complete / identical", f"{qw['downloads_ok']}/{qw['downloads']} {qw['all_identical'].upper()}",
            f"{qg['downloads_ok']}/{qg['downloads']} {qg['all_identical'].upper()}",
            "both" if qw["all_identical"] == qg["all_identical"] == "yes" else "-"),
        row("  Time per 8.8 MB file", qw["avg_download_s"], qg["avg_download_s"], " s", 2),
        row("  Speed", qw["avg_throughput_mbps"], qg["avg_throughput_mbps"], " Mbit/s", 1, "high"),
        row("  Packets lost in network", qw["network_packet_loss_pct"], qg["network_packet_loss_pct"], " %", 2),
        row("  Packets resent by QUIC", qw["retransmission_pct"], qg["retransmission_pct"], " %", 2),
        row("  RTT under load (ping)", qw["ping_rtt_ms"], qg["ping_rtt_ms"], " ms"),
        None,
        ["2. LIVE - QUIC streams (reliable)", "", "", ""],
        txt("  Frames received", f"{sw.get('frames_received','-')} / {sw.get('frames_expected','-')}",
            f"{sg.get('frames_received','-')} / {sg.get('frames_expected','-')}"),
        row("  Frames on time", sw.get("on_time_pct"), sg.get("on_time_pct"), " %", 1, "high"),
        row("  Frame delay average", sw.get("delay_avg_ms"), sg.get("delay_avg_ms"), " ms"),
        row("  Frame delay 95 %", sw.get("delay_p95_ms"), sg.get("delay_p95_ms"), " ms"),
        row("  Frame delay max", sw.get("delay_max_ms"), sg.get("delay_max_ms"), " ms"),
        row("  Packets lost in network", sw.get("network_packet_loss_pct"), sg.get("network_packet_loss_pct"), " %", 2),
        row("  Packets resent by QUIC", rp(sw), rp(sg), " %", 2),
        row("  RTT (ping)", sw.get("ping_rtt_ms"), sg.get("ping_rtt_ms"), " ms"),
        None,
        ["3. LIVE - QUIC datagrams (unreliable)", "", "", ""],
        txt("  Frames received", f"{dw.get('frames_received','-')} / {dw.get('frames_expected','-')}",
            f"{dg.get('frames_received','-')} / {dg.get('frames_expected','-')}"),
        row("  Frames lost", dw.get("frames_lost"), dg.get("frames_lost"), "", 0),
        row("  Frames on time", dw.get("on_time_pct"), dg.get("on_time_pct"), " %", 1, "high"),
        row("  Frame delay average", dw.get("delay_avg_ms"), dg.get("delay_avg_ms"), " ms"),
        row("  Frame delay 95 %", dw.get("delay_p95_ms"), dg.get("delay_p95_ms"), " ms"),
        row("  Frame delay max", dw.get("delay_max_ms"), dg.get("delay_max_ms"), " ms"),
        row("  Packets lost in network", dw.get("network_packet_loss_pct"), dg.get("network_packet_loss_pct"), " %", 2),
        row("  RTT (ping)", dw.get("ping_rtt_ms"), dg.get("ping_rtt_ms"), " ms"),
    ]
    table("QUIC (picoquic) - Wi-Fi vs 5G - all tests in one table",
          ["Measurement", "Wi-Fi", "5G", "Better"], rows, align=["<", ">", ">", "^"])
    print("  Better = the network with the better value (lower delay/loss/time, higher speed/on-time)")


def _pnum(x, dd=2, unit=""):
    """Like num(), but PSNR "inf" (no difference at all) shows as 'identical'."""
    return "identical" if str(x).strip() == "inf" else _num(x, dd, unit)


def show_players(d):
    num = _pnum
    S = read_csv(os.path.join(d, "players_summary.csv"))            # live
    V = read_csv(os.path.join(d, "players_notlive_summary.csv"))    # not live
    D = read_csv(os.path.join(d, "players_detail.csv"))
    VD = read_csv(os.path.join(d, "players_notlive_detail.csv"))
    if not S and not V:
        raise SystemExit(f"No results in {d}/ - run: sudo python3 players_test.py --video BBB.webm")
    nets = [n for n in ("wifi", "5g") if any(r["path"] == n for r in S + V)]
    one_l = {r["path"]: r for r in S if r["players"] == "1"}
    one_v = {r["path"]: r for r in V if r["players"] == "1"}

    # 1. one player: live vs not live, Wi-Fi vs 5G
    cols = []
    for n in nets:
        if n in one_l:
            cols.append((n, "live", one_l[n]))
        if n in one_v:
            cols.append((n, "notlive", one_v[n]))
    if cols:
        def r_(label, lk=None, vk=None, unit="", dd=1, lfix="n/a", vfix="n/a"):
            out = [label]
            for _, m, r in cols:
                k, fix = (lk, lfix) if m == "live" else (vk, vfix)
                out.append(num(r.get(k), dd, unit) if k else fix)
            return out
        pl = lambda r: num(r.get("radio_path_loss_db"), 1, " dB") if r.get("radio_path_loss_db") else "n/a (wired)"
        rows = [
            ["HOW IT IS WATCHED", *["real time (UDP)" if m == "live" else "download+play (TCP)"
                                   for _, m, _ in cols]],
            None,
            ["PLAYBACK (what the viewer sees)", *[""] * len(cols)],
            r_("  Start-up time (first picture)", "startup_ms", "startup_ms", " ms", 0),
            r_("  Smooth playback", "smooth_playback_pct", "smooth_playback_pct", " %", 1),
            r_("  Stalls / freezes", "stalls_per_player", "stalls_per_player", "", 1),
            r_("  Stall time (frozen)", None, "stall_time_ms", " ms", 0, lfix="-"),
            r_("  Late frames", "frames_late_pct", None, " %", 2),
            r_("  Lost frames", "frames_lost_pct", None, " %", 2, vfix="0 (resent)"),
            ["  Picture vs original", *[num(r.get("psnr_avg_db"), 1, " dB PSNR") if m == "live" else
                                       ("identical" if r.get("all_identical") == "yes" else "NOT identical")
                                       for _, m, r in cols]],
            None,
            ["TIME / DELAY", *[""] * len(cols)],
            r_("  Frame delay average", "frame_delay_avg_ms", None, " ms", 1),
            ["  Frame delay 95 % / max", *[f"{num(r.get('frame_delay_p95_ms'), 1)} / "
                                          f"{num(r.get('frame_delay_max_ms'), 1)} ms" if m == "live" else "n/a"
                                          for _, m, r in cols]],
            r_("  Jitter", "jitter_ms", None, " ms", 1),
            r_("  Download time (whole video)", None, "download_avg_s", " s", 2, lfix="real time"),
            r_("  Speed per player", "received_mbps_total", "throughput_per_player_mbps", " Mbit/s", 1),
            r_("  RTT (ping)", "ping_rtt_ms", "ping_rtt_ms", " ms", 1),
            None,
            ["NETWORK", *[""] * len(cols)],
            r_("  Packet loss (UDP)", "packet_loss_pct", None, " %", 2),
            r_("  TCP retransmissions", None, "tcp_retrans_pct", " %", 2),
            r_("  Configured loss", "configured_loss_pct", "configured_loss_pct", " %", 2),
            ["  Path loss (radio)", *[pl(r) for _, _, r in cols]],
            r_("  Background traffic", "background_mbps", "background_mbps", " Mbit/s", 0),
        ]
        head = [f"{LABEL[n]} {'live' if m == 'live' else 'not live'}" for n, m, _ in cols]
        table("ONE VIDEO PLAYER - LIVE vs NOT LIVE - " + " vs ".join(LABEL[n] for n in nets),
              ["Measurement", *head], rows)
        print("  Live: frames must arrive in real time (buffer %s ms).  Not live: the player downloads the video\n"
              "  and starts after %s s is buffered; a stall = the buffer ran empty."
              % (num(S[0]["buffer_ms"], 0) if S else "-", num(V[0]["vod_buffer_s"], 1) if V else "-"))

    # 2. live vs not live with more players
    if S and V:
        rows = []
        for n in nets:
            for N in sorted({int(r["players"]) for r in S + V if r["path"] == n}):
                l = next((r for r in S if r["path"] == n and r["players"] == str(N)), {})
                v = next((r for r in V if r["path"] == n and r["players"] == str(N)), {})
                rows.append([LABEL[n], N, num(l.get("startup_ms"), 0), num(l.get("frame_delay_avg_ms"), 1),
                             num(l.get("frames_lost_pct"), 2), num(l.get("smooth_playback_pct"), 1),
                             num(l.get("psnr_avg_db"), 1), "|", num(v.get("startup_ms"), 0),
                             num(v.get("download_avg_s"), 2), num(v.get("stalls_per_player"), 1),
                             num(v.get("smooth_playback_pct"), 1), (v.get("all_identical") or "-").upper(),
                             num(v.get("tcp_retrans_pct"), 2)])
            rows.append(None)
        table("LIVE vs NOT LIVE - MORE PLAYERS AT THE SAME RECEIVER (average per player)",
              ["Network", "Players", "LIVE start-up", "Delay avg", "Lost %", "Smooth %", "PSNR", "|",
               "NOT LIVE start-up", "Download s", "Stalls", "Smooth %", "Identical", "TCP retr %"],
              rows[:-1])
        print("  Start-up / Delay in ms.  Live = UDP real time.  Not live = HTTP/TCP download, plays after the buffer.")

    # 3. live details
    if S:
        rows = []
        for n in nets:
            for r in [x for x in S if x["path"] == n]:
                rows.append([LABEL[n], r["players"], num(r["video_load_mbps"], 0), num(r["startup_ms"], 0),
                             num(r["frame_delay_avg_ms"], 1), num(r["frame_delay_p95_ms"], 1),
                             num(r["frame_delay_max_ms"], 1), num(r["jitter_ms"], 1),
                             num(r["frames_lost_pct"], 2), num(r["frames_late_pct"], 2),
                             num(r["stalls_per_player"], 1), num(r["smooth_playback_pct"], 1),
                             num(r["psnr_avg_db"], 1), num(r["packet_loss_pct"], 2),
                             num(r["ping_rtt_ms"], 1), num(r["background_mbps"], 0), num(r["cpu_busy_pct"], 0)])
            rows.append(None)
        table("LIVE - MORE VIDEO PLAYERS (average per player)",
              ["Network", "Players", "Video Mb/s", "Start-up", "Delay avg", "Delay 95%", "Delay max",
               "Jitter", "Lost %", "Late %", "Stalls", "Smooth %", "PSNR", "Pkt loss %", "RTT", "Bg Mb/s",
               "CPU %"], rows[:-1])
        print("  Start-up / Delay / Jitter / RTT in ms.  CPU % near 100 = extra delay may come from the VM.")

    # 4. not live details
    if V:
        rows = []
        for n in nets:
            for r in [x for x in V if x["path"] == n]:
                rows.append([LABEL[n], r["players"], num(r["total_mb"], 1), num(r["startup_ms"], 0),
                             num(r["download_avg_s"], 2), num(r["download_max_s"], 2),
                             num(r["throughput_per_player_mbps"], 1), num(r["stalls_per_player"], 1),
                             num(r["stall_time_ms"], 0), num(r["smooth_playback_pct"], 1),
                             num(r["smooth_worst_player_pct"], 1), r["all_identical"].upper(),
                             num(r["tcp_retrans_pct"], 2), num(r["ping_rtt_ms"], 1),
                             num(r["background_mbps"], 0), num(r["cpu_busy_pct"], 0)])
            rows.append(None)
        table("NOT LIVE (video on demand) - MORE VIDEO PLAYERS (average per player)",
              ["Network", "Players", "Total MB", "Start-up", "Download s", "Slowest s", "Mbit/s each",
               "Stalls", "Stall ms", "Smooth %", "Worst %", "Identical", "TCP retr %", "RTT",
               "Bg Mb/s", "CPU %"], rows[:-1])
        print("  Start-up / Stall / RTT in ms.  Stall = playback caught up with the download and froze.")

    # 5. each player in the biggest run
    for n in nets:
        Ls = [x for x in D if x["path"] == n]
        if Ls:
            big = max(int(x["players"]) for x in Ls)
            if big > 1:
                table(f"EACH PLAYER - LIVE - {LABEL[n]} with {big} players",
                      ["Player", "Frames", "Start-up", "Delay avg", "Delay 95%", "Delay max", "Jitter",
                       "Late", "Stalls", "Smooth %", "PSNR", "Pkt loss %"],
                      [[x["player"], f"{x['frames_received']} / {x['frames_expected']}", num(x["startup_ms"], 0),
                        num(x["frame_delay_avg_ms"], 1), num(x["frame_delay_p95_ms"], 1),
                        num(x["frame_delay_max_ms"], 1), num(x["jitter_ms"], 1), x["late_frames"],
                        x["stalls"], num(x["smooth_playback_pct"], 1), num(x["psnr_db"], 1),
                        num(x["packet_loss_pct"], 2)] for x in Ls if x["players"] == str(big)])
        Vs = [x for x in VD if x["path"] == n]
        if Vs:
            big = max(int(x["players"]) for x in Vs)
            if big > 1:
                table(f"EACH PLAYER - NOT LIVE - {LABEL[n]} with {big} players",
                      ["Player", "Start-up", "Download s", "Mbit/s", "Stalls", "Stall ms", "Smooth %", "Identical"],
                      [[x["player"], num(x["startup_ms"], 0), num(x["download_s"], 2),
                        num(x["throughput_mbps"], 1), x["stalls"], num(x["stall_time_ms"], 0),
                        num(x["smooth_playback_pct"], 1), x["identical"].upper()]
                       for x in Vs if x["players"] == str(big)])
    print(f"  Files: {os.path.abspath(d)}/players_summary.csv (live), players_notlive_summary.csv, "
          f"*_detail.csv, live_<net>_<N>players/player_<k>.ts, players_grid.mp4")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Show test results as tables")
    ap.add_argument("--dir", default=None, help="results folder")
    ap.add_argument("--quic", action="store_true", help="show the QUIC test (quic_results/)")
    ap.add_argument("--live", action="store_true", help="show the live QUIC test (live_results/)")
    ap.add_argument("--compare", action="store_true",
                    help="QUIC without live stream (quic_results/) vs with live stream (live_results/)")
    ap.add_argument("--one", action="store_true", help="Wi-Fi vs 5G, all QUIC tests in one table")
    ap.add_argument("--players", action="store_true", help="video players test (players_results/)")
    a = ap.parse_args()
    if a.players:
        show_players(a.dir or "players_results")
    elif a.one:
        show_one("quic_results", "live_results")
    elif a.compare:
        show_compare("quic_results", "live_results")
    elif a.live:
        show_live(a.dir or "live_results")
    elif a.quic:
        show_quic(a.dir or "quic_results")
    else:
        show(a.dir or "video_results")
