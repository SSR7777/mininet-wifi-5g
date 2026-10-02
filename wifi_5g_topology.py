#!/usr/bin/env python3
"""
Dual-path Wi-Fi + emulated 5G topology in Mininet-WiFi.

                 Wi-Fi path: 130 Mbit/s, 10 ms, 1 % loss
   sta1-wlan0 ))))  ap1  ================================  srv-wifi
   10.0.1.1/24                                             10.0.1.100/24

   sta1-5g    =====  s5g  ================================  srv-5g
   10.0.2.1/24      5G path: 700 Mbit/s, 15 ms, 0.1 % loss   10.0.2.100/24

  * Each path is its own subnet, so traffic to 10.0.1.100 uses Wi-Fi and
    traffic to 10.0.2.100 uses 5G (no policy routing needed).
  * The 5G path is "emulated": a wired link shaped with tc (HTB + netem)
    to the 5G capacity / delay / loss values.
  * iPerf3 UDP background traffic loads each path to 60 % of capacity
    (78 Mbit/s on Wi-Fi, 420 Mbit/s on 5G) while TCP foreground flows and
    ping measure what is left.

Run:   sudo python3 wifi_5g_topology.py            # run experiment
       sudo python3 wifi_5g_topology.py --cli      # experiment, then CLI
       sudo python3 wifi_5g_topology.py --mptcp    # also test MPTCP over both paths
"""
import argparse
import csv
import json
import os
import re
import time

from mininet.log import setLogLevel, info
from mininet.link import TCLink
from mininet.node import OVSKernelSwitch
from mn_wifi.net import Mininet_wifi
from mn_wifi.node import OVSKernelAP
from mn_wifi.cli import CLI

# ---- Parameters from the assignment --------------------------------------
PATHS = {
    "wifi": {"bw": 130, "delay": "10ms", "loss": 1.0,
             "client_ip": "10.0.1.1", "server_ip": "10.0.1.100",
             "client_if": "sta1-wlan0", "bg_port": 5201, "fg_port": 5203},
    "5g":   {"bw": 700, "delay": "15ms", "loss": 0.1,
             "client_ip": "10.0.2.1", "server_ip": "10.0.2.100",
             "client_if": "sta1-5g", "bg_port": 5202, "fg_port": 5204},
}
BG_SHARE = 0.60          # iPerf background traffic = 60 % of path capacity
MPTCP_PORT = 5205


def build_topology():
    net = Mininet_wifi(controller=None, accessPoint=OVSKernelAP,
                       switch=OVSKernelSwitch)

    info("*** Creating nodes\n")
    sta1 = net.addStation("sta1", ip="10.0.1.1/24", position="10,10,0")
    srv = net.addHost("srv", ip="10.0.1.100/24")
    ap1 = net.addAccessPoint("ap1", ssid="wifi-path", mode="g", channel="1",
                             position="15,10,0", failMode="standalone")
    s5g = net.addSwitch("s5g", failMode="standalone")

    info("*** Configuring Wi-Fi nodes\n")
    if hasattr(net, "configureNodes"):
        net.configureNodes()          # newer Mininet-WiFi
    else:
        net.configureWifiNodes()      # older Mininet-WiFi

    w, f = PATHS["wifi"], PATHS["5g"]

    info("*** Creating links\n")
    # Wi-Fi association
    net.addLink(sta1, ap1)
    # Wi-Fi backhaul: shapes the Wi-Fi path (bw + delay both directions,
    # loss on the uplink direction ap1 -> srv, i.e. the data direction)
    net.addLink(ap1, srv, cls=TCLink, intfName2="srv-wifi",
                bw=w["bw"], delay=w["delay"], params1={"loss": w["loss"]})
    # Emulated 5G access link (shaped) + core link (unshaped)
    net.addLink(sta1, s5g, cls=TCLink, intfName1="sta1-5g",
                bw=f["bw"], delay=f["delay"], params1={"loss": f["loss"]})
    net.addLink(s5g, srv, intfName2="srv-5g")

    info("*** Starting network\n")
    net.build()
    net.start()
    time.sleep(5)
    # Mininet-WiFi puts its own (mode/distance based) rate limit on the radio
    # and overrides the TCLink shaping, so apply the Wi-Fi path limits here.
    sta1.cmd(f"tc qdisc replace dev sta1-wlan0 root netem rate {w['bw']}mbit "
             f"delay {w['delay']} loss {w['loss']}% limit 10000")
    ap1.cmd("tc qdisc del dev ap1-wlan1 root")
    srv.cmd(f"tc qdisc replace dev srv-wifi root netem rate {w['bw']}mbit "
            f"delay {w['delay']} limit 10000")

    # Addresses (one subnet per path)
    sta1.setIP("10.0.1.1/24", intf="sta1-wlan0")
    sta1.setIP("10.0.2.1/24", intf="sta1-5g")
    srv.setIP("10.0.1.100/24", intf="srv-wifi")
    srv.setIP("10.0.2.100/24", intf="srv-5g")

    wait_for_wifi(sta1, ap1)
    return net


def wait_for_wifi(sta1, ap1, timeout=20):
    """Make sure sta1 is associated with ap1 before measuring."""
    info("*** Waiting for Wi-Fi association\n")
    out = ""
    for i in range(timeout * 2):
        out = sta1.cmd("iw dev sta1-wlan0 link")
        if "Connected" in out:
            break
        if i == 6:  # nudge it once if it has not associated after ~3 s
            sta1.cmd("iw dev sta1-wlan0 connect wifi-path")
        time.sleep(0.5)
    info(out + "\n")
    if "Connected" not in out:
        info("*** WARNING: sta1 is NOT associated with ap1\n")
        info(ap1.cmd("ovs-vsctl show") + "\n")
        info(sta1.cmd("iw dev sta1-wlan0 scan | grep -E 'SSID|freq'") + "\n")
    # Warm up ARP on the Wi-Fi path
    sta1.cmd("ping -c 3 -W 1 10.0.1.100")


def parse_ping(out):
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", out)
    return {"ping_loss_pct": float(loss.group(1)) if loss else None,
            "rtt_avg_ms": float(rtt.group(2)) if rtt else None}


def load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def mbps(bps):
    return round(bps / 1e6, 2) if bps is not None else None


def run_experiment(net, duration, outdir, mptcp):
    sta1, srv = net.get("sta1"), net.get("srv")
    os.makedirs(outdir, exist_ok=True)
    results = {}

    # Save tc config as evidence that the shaping is applied
    with open(os.path.join(outdir, "tc_config.txt"), "w") as fh:
        for node in (sta1, srv, net.get("ap1"), net.get("s5g")):
            fh.write(f"==== {node.name} ====\n{node.cmd('tc qdisc show')}\n")

    info("*** Idle RTT check (no background traffic)\n")
    for p in PATHS.values():          # warm-up pings (ARP etc.), not counted
        sta1.cmd(f"ping -c 3 -i 0.2 {p['server_ip']}")
    for name, p in PATHS.items():
        out = sta1.cmd(f"ping -c 10 -i 0.2 {p['server_ip']}")
        results[name] = {"idle": parse_ping(out)}
        info(f"    {name}: {results[name]['idle']}\n")

    info("*** Starting iperf3 servers\n")
    srv.cmd("pkill -9 iperf3")
    for p in PATHS.values():
        srv.cmd(f"iperf3 -s -D -p {p['bg_port']}")
        srv.cmd(f"iperf3 -s -D -p {p['fg_port']}")
    time.sleep(1)

    info(f"*** Background UDP traffic at {int(BG_SHARE*100)} % of capacity\n")
    for name, p in PATHS.items():
        rate = int(p["bw"] * BG_SHARE)
        info(f"    {name}: {rate} Mbit/s\n")
        if rate <= 0:                 # iperf3 -b 0 would mean "unlimited"
            continue
        sta1.cmd(f"iperf3 -c {p['server_ip']} -p {p['bg_port']} -u -b {rate}M "
                 f"-t {duration + 6} -J > {outdir}/bg_{name}.json 2>&1 &")
    time.sleep(3)

    info(f"*** Foreground TCP flows + ping under load ({duration}s)\n")
    for name, p in PATHS.items():
        sta1.cmd(f"iperf3 -c {p['server_ip']} -p {p['fg_port']} -t {duration} "
                 f"-J > {outdir}/fg_{name}.json 2>&1 &")
        sta1.cmd(f"ping -c {duration * 2} -i 0.5 {p['server_ip']} "
                 f"> {outdir}/ping_{name}.txt 2>&1 &")
    time.sleep(duration + 6)

    for name, p in PATHS.items():
        bg = load_json(f"{outdir}/bg_{name}.json").get("end", {}).get("sum", {})
        fg = load_json(f"{outdir}/fg_{name}.json").get("end", {})
        with open(f"{outdir}/ping_{name}.txt") as fh:
            loaded = parse_ping(fh.read())
        results[name].update({
            "capacity_mbps": p["bw"], "delay": p["delay"], "loss_pct": p["loss"],
            "bg_target_mbps": int(p["bw"] * BG_SHARE),
            "bg_achieved_mbps": mbps(bg.get("bits_per_second")),
            "bg_udp_lost_pct": bg.get("lost_percent"),
            "fg_tcp_mbps": mbps(fg.get("sum_received", {}).get("bits_per_second")),
            "fg_tcp_retransmits": fg.get("sum_sent", {}).get("retransmits"),
            "loaded": loaded,
        })

    if mptcp:
        results["mptcp"] = run_mptcp(sta1, srv, duration, outdir)

    srv.cmd("pkill -9 iperf3")
    sta1.cmd("pkill -9 iperf3")

    # Summary
    with open(os.path.join(outdir, "summary.json"), "w") as fh:
        json.dump(results, fh, indent=2)
    with open(os.path.join(outdir, "summary.csv"), "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["path", "capacity_mbps", "delay", "loss_pct", "bg_target_mbps",
                     "bg_achieved_mbps", "bg_udp_lost_pct", "fg_tcp_mbps",
                     "fg_tcp_retransmits", "idle_rtt_ms", "loaded_rtt_ms",
                     "loaded_ping_loss_pct"])
        for name in PATHS:
            r = results[name]
            wr.writerow([name, r["capacity_mbps"], r["delay"], r["loss_pct"],
                         r["bg_target_mbps"], r["bg_achieved_mbps"],
                         r["bg_udp_lost_pct"], r["fg_tcp_mbps"],
                         r["fg_tcp_retransmits"], r["idle"]["rtt_avg_ms"],
                         r["loaded"]["rtt_avg_ms"], r["loaded"]["ping_loss_pct"]])

    info("\n*** RESULTS\n")
    for name in PATHS:
        r = results[name]
        info(f"  {name:>4}: cap {r['capacity_mbps']} Mb/s | bg {r['bg_achieved_mbps']} Mb/s"
             f" | TCP fg {r['fg_tcp_mbps']} Mb/s | RTT idle {r['idle']['rtt_avg_ms']} ms"
             f" -> loaded {r['loaded']['rtt_avg_ms']} ms\n")
    if mptcp:
        info(f"  MPTCP: {results['mptcp']}\n")
    info(f"*** Raw files in {outdir}/\n")


def run_mptcp(sta1, srv, duration, outdir):
    """Optional: one MPTCP connection using both paths (Linux >= 5.15, upstream MPTCP)."""
    info("*** MPTCP test over Wi-Fi + 5G\n")
    for node in (sta1, srv):
        node.cmd("sysctl -w net.mptcp.enabled=1")
        node.cmd("ip mptcp limits set subflow 2 add_addr_accepted 2")
    # Server announces its 5G address (ADD_ADDR); the client then opens a
    # 2nd subflow to 10.0.2.100, which is routed over sta1-5g.
    srv.cmd("ip mptcp endpoint add 10.0.2.100 dev srv-5g signal")

    srv.cmd(f"mptcpize run iperf3 -s -D -p {MPTCP_PORT}")
    time.sleep(1)
    sta1.cmd(f"mptcpize run iperf3 -c 10.0.1.100 -p {MPTCP_PORT} -t {duration} "
             f"-J > {outdir}/mptcp.json 2>&1 &")
    time.sleep(duration / 2)
    with open(f"{outdir}/mptcp_subflows.txt", "w") as fh:   # snapshot mid-test
        fh.write(sta1.cmd(f"ss -tni '( dport = :{MPTCP_PORT} )'"))
    time.sleep(duration / 2 + 3)
    end = load_json(f"{outdir}/mptcp.json").get("end", {})
    return {"mptcp_tcp_mbps": mbps(end.get("sum_received", {}).get("bits_per_second"))}


def main():
    global BG_SHARE
    ap = argparse.ArgumentParser(description="Wi-Fi + 5G dual-path emulation")
    ap.add_argument("--duration", type=int, default=30, help="test length in seconds")
    ap.add_argument("--out", default="results", help="output directory")
    ap.add_argument("--cli", action="store_true", help="open CLI after the test")
    ap.add_argument("--no-test", action="store_true", help="only build topology + CLI")
    ap.add_argument("--mptcp", action="store_true", help="also run an MPTCP test")
    ap.add_argument("--bg-share", type=float, default=BG_SHARE,
                    help="background load as a fraction of capacity (default 0.6)")
    ap.add_argument("--wifi-loss", type=float, default=PATHS["wifi"]["loss"],
                    help="Wi-Fi loss in %% (default 1)")
    ap.add_argument("--g5-loss", type=float, default=PATHS["5g"]["loss"],
                    help="5G loss in %% (default 0.1)")
    args = ap.parse_args()

    BG_SHARE = args.bg_share
    PATHS["wifi"]["loss"] = args.wifi_loss
    PATHS["5g"]["loss"] = args.g5_loss

    setLogLevel("info")
    net = build_topology()
    try:
        if not args.no_test:
            run_experiment(net, args.duration, args.out, args.mptcp)
        if args.cli or args.no_test:
            CLI(net)
    finally:
        info("*** Stopping network\n")
        net.stop()


if __name__ == "__main__":
    main()
