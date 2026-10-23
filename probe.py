#!/usr/bin/env python3
"""
Small UDP probe that measures the one-way DELAY and the LOSS of one path (used by congestion_test.py).

  receiver (srv):  python3 probe.py recv <bind ip> <port> <log file>
  sender  (sta1):  python3 probe.py send <dst ip> <port> <packets per s> <seconds>

Every probe packet carries a sequence number and the time it was sent. The receiver writes one
line per packet:  "<seq> <sent time> <received time>". In Mininet all nodes share the same clock,
so received - sent is the real one-way delay (on real devices the clocks would have to be synced).
Missing sequence numbers = lost packets.
"""
import socket
import struct
import sys
import time

FMT = "!Id"                     # sequence number, send time
SIZE = 100                      # bytes per probe packet (tiny next to the video)


def send(dst, port, pps, secs):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    gap = 1.0 / pps
    pad = b"\0" * (SIZE - struct.calcsize(FMT))
    t0 = time.time()
    seq = 0
    while True:
        due = t0 + seq * gap
        now = time.time()
        if now - t0 > secs:
            break
        if due > now:
            time.sleep(due - now)
        try:
            s.sendto(struct.pack(FMT, seq, time.time()) + pad, (dst, port))
        except OSError:
            pass
        seq += 1


def recv(ip, port, log):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    s.bind((ip, port))
    s.settimeout(0.2)
    n = struct.calcsize(FMT)
    last = time.time()
    with open(log, "w") as fh:
        while True:
            try:
                data = s.recv(2048)
                now = time.time()
                if len(data) >= n:
                    seq, sent = struct.unpack(FMT, data[:n])
                    fh.write(f"{seq} {sent:.6f} {now:.6f}\n")
            except socket.timeout:
                pass
            if time.time() - last > 0.2:          # the controller reads the file while it grows
                fh.flush()
                last = time.time()


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "send":
        send(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5]))
    elif len(sys.argv) >= 5 and sys.argv[1] == "recv":
        recv(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    else:
        print(__doc__)
