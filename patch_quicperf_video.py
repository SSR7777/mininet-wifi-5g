#!/usr/bin/env python3
"""Let picoquic's quicperf carry a REAL video file instead of filler bytes.

Sender (server) environment:
  QUICPERF_VIDEO_IN=<file.ts>     bytes to send (live, paced by the quicperf scenario)
Receiver (client) environment:
  QUICPERF_VIDEO_OUT=<file.ts>    every byte that arrived is written at its place in the file
  QUICPERF_VIDEO_LOG=<file.csv>   one line per piece: offset,length,send_time_us,arrival_time_us
Both:
  QUICPERF_VIDEO_SLOT=<bytes>     video bytes per datagram (default 1000)
  QUICPERF_VIDEO_NS=<n>           number of datagram streams in the scenario (default 2)

QUIC streams: frame k of the scenario carries video bytes [k*(F-8), (k+1)*(F-8)),
F = frame size (the first 8 bytes of a frame are quicperf's time stamp).
QUIC datagrams: datagram j of datagram-stream s carries video bytes of piece
j*NS + s, SLOT bytes per piece. A lost datagram leaves a hole in the received file,
exactly as a real live video receiver would see it.

Without these variables quicperf behaves exactly as before.
Usage: python3 patch_quicperf_video.py ~/picoquic
"""
import sys, os

root = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/picoquic")
path = os.path.join(root, "picohttp", "quicperf.c")
s = open(path).read()
if "QUICPERF_VIDEO_IN" in s:
    print("quicperf already patched for real video")
    sys.exit(0)

HELPERS = r'''
/* ---- real video payload (patch_quicperf_video.py) ---- */
#ifndef _WINDOWS
#include <fcntl.h>
#include <unistd.h>
static int qpv_fd_in = -2;
static int qpv_fd_out = -2;
static FILE* qpv_log = NULL;
static uint64_t qpv_slot = 1000;
static uint64_t qpv_ns = 2;
static void qpv_init(void)
{
    char* e;
    if (qpv_fd_in != -2) {
        return;
    }
    qpv_fd_in = ((e = getenv("QUICPERF_VIDEO_IN")) != NULL) ? open(e, O_RDONLY) : -1;
    qpv_fd_out = ((e = getenv("QUICPERF_VIDEO_OUT")) != NULL) ? open(e, O_WRONLY | O_CREAT | O_TRUNC, 0644) : -1;
    if ((e = getenv("QUICPERF_VIDEO_LOG")) != NULL) {
        qpv_log = fopen(e, "w");
    }
    if ((e = getenv("QUICPERF_VIDEO_SLOT")) != NULL && atoi(e) > 0) {
        qpv_slot = (uint64_t)atoi(e);
    }
    if ((e = getenv("QUICPERF_VIDEO_NS")) != NULL && atoi(e) > 0) {
        qpv_ns = (uint64_t)atoi(e);
    }
}
/* fill buf with video bytes from offset off (filler after the end of the file) */
static void qpv_fill(uint8_t* buf, size_t len, uint64_t off, uint8_t filler)
{
    size_t got = 0;
    qpv_init();
    if (qpv_fd_in >= 0) {
        ssize_t r = pread(qpv_fd_in, buf, len, (off_t)off);
        if (r > 0) {
            got = (size_t)r;
        }
    }
    if (got < len) {
        memset(buf + got, filler, len - got);
    }
}
/* write received video bytes at their place, and log when they arrived */
static void qpv_put(const uint8_t* buf, size_t len, uint64_t off, uint64_t sent_time, uint64_t arrival_time)
{
    qpv_init();
    if (qpv_fd_out >= 0 && len > 0) {
        (void)pwrite(qpv_fd_out, buf, len, (off_t)off);
    }
    if (qpv_log != NULL && len > 0) {
        fprintf(qpv_log, "%" PRIu64 ",%zu,%" PRIu64 ",%" PRIu64 "\n", off, len, sent_time, arrival_time);
        fflush(qpv_log);
    }
}
#define QPV_DG_INDEX(stream_id, frame_id) ((frame_id) * qpv_ns + (((stream_id) / 4) % qpv_ns))
#else
static uint64_t qpv_slot = 1000;
static uint64_t qpv_ns = 2;
static void qpv_init(void) {}
static void qpv_fill(uint8_t* buf, size_t len, uint64_t off, uint8_t filler) { (void)off; memset(buf, filler, len); }
static void qpv_put(const uint8_t* buf, size_t len, uint64_t off, uint64_t s, uint64_t a) { (void)buf; (void)len; (void)off; (void)s; (void)a; }
#define QPV_DG_INDEX(stream_id, frame_id) ((frame_id) * qpv_ns + (((stream_id) / 4) % qpv_ns))
#endif
/* ---- end real video payload ---- */
'''


def rep(old, new, count=1):
    global s
    if s.count(old) != count:
        raise SystemExit(f"patch failed: expected {count} match(es) of:\n{old}")
    s = s.replace(old, new)


# helpers after the last #include
i = s.rfind("#include")
j = s.index("\n", i) + 1
s = s[:j] + HELPERS + s[j:]

# datagrams, sender: video bytes after the datagram header
rep('''            if (bytes < bytes_max && encoded < data_size) {
                memset(bytes, 0xaa, (size_t)data_size - encoded);''',
    '''            if (bytes < bytes_max && encoded < data_size) {
                size_t qpv_len = (size_t)data_size - encoded;
                qpv_init();
                if (qpv_len > qpv_slot) {
                    memset(bytes + qpv_slot, 0xaa, qpv_len - (size_t)qpv_slot);
                    qpv_len = (size_t)qpv_slot;
                }
                qpv_fill(bytes, qpv_len, QPV_DG_INDEX(stream_ctx->stream_id, stream_ctx->nb_frames_sent) * qpv_slot, 0xaa);''')

# datagrams, receiver: store the video bytes
rep('''        report->nb_frames_received += 1;
        if (current_time <= expected_time) {
            rtt = 0;
        }''', '''        report->nb_frames_received += 1;
        {
            size_t qpv_len = (size_t)(bytes_max - bytes);
            qpv_init();
            if (qpv_len > qpv_slot) {
                qpv_len = (size_t)qpv_slot;
            }
            qpv_put(bytes, qpv_len, QPV_DG_INDEX(target_stream_ctx.stream_id, frame_id) * qpv_slot,
                expected_time, current_time);
        }
        if (current_time <= expected_time) {
            rtt = 0;
        }''')

# streams, sender: frame payload = video bytes
rep('''    int is_fin = 0;
    uint8_t* buffer;

    stream_ctx->is_activated = 1; /* default value, may be overwritten next. */''',
    '''    int is_fin = 0;
    uint8_t* buffer;
    uint64_t qpv_frame = stream_ctx->nb_frames_sent;   /* frame being sent now */

    stream_ctx->is_activated = 1; /* default value, may be overwritten next. */''')
rep('''        if (byte_index < available) {
            memset(buffer + byte_index, 0x30, available - byte_index);
        }''', '''        if (byte_index < available) {
            qpv_fill(buffer + byte_index, available - byte_index,
                qpv_frame * (send_limit - 8) + (stream_ctx->frame_bytes_sent - 8), 0x30);
        }''')

# streams, receiver: store the video bytes of each frame
rep('''                if (available > length - byte_index) {
                    available = length - byte_index;
                }
                stream_ctx->frames_bytes_received += available;''',
    '''                if (available > length - byte_index) {
                    available = length - byte_index;
                }
                if (ctx->is_client) {
                    uint64_t qpv_sent = stream_ctx->start_time;
                    if (stream_ctx->nb_frames_received > 0 && stream_ctx->frequency > 0) {
                        qpv_sent += (stream_ctx->nb_frames_received * 1000000) / stream_ctx->frequency;
                    }
                    qpv_put(bytes + byte_index, available,
                        stream_ctx->nb_frames_received * (expected_bytes - 8) + (stream_ctx->frames_bytes_received - 8),
                        qpv_sent, current_time);
                }
                stream_ctx->frames_bytes_received += available;''')

open(path, "w").write(s)
print("quicperf patched: it can now carry a real video (QUICPERF_VIDEO_IN / QUICPERF_VIDEO_OUT)")
