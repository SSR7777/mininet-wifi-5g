/*
 * moqpico - MoQ (MOQT draft-16, MoQtail compatible) video publisher / subscriber on picoquic.
 *
 * PUBLISHER (sender, sta1)
 *   moqpico pub --ts video.ts --units units.csv [--port 4443] [--cert c.pem --key k.pem]
 *               [--label wifi=10.0.1.1 --label 5g=10.0.2.1] [--i-path 5g --pb-path wifi]
 *               [--path-file /tmp/moq_path] [--i-mode stream|dgram] [--pb-mode dgram|stream]
 *               [--wait-subs 2] [--log sent.csv]
 *   moqpico pub --relay 127.0.0.1:4433 ...          publish through a MoQtail relay
 *   moqpico pub --synthetic 50,10,1000,33 ...       no video: groups,objects,bytes,interval ms
 *
 *   Track "<ns>/<track>" (default moqpico/video). One GROUP = one GOP (starts at an I frame).
 *   Every frame is cut into objects of --chunk bytes (5 MPEG-TS packets = 940 B by default),
 *   each object payload starts with a 21-byte header: sent time (us), byte offset in the .ts,
 *   frame number, frame type. I-frame objects go on a SUBGROUP stream (subgroup 0) of the session
 *   named by --i-path, P/B objects go as OBJECT_DATAGRAMs on the session named by --pb-path.
 *   --path-file: a file holding "<I path> <P/B path>" (e.g. "5g wifi"); re-read every 100 ms,
 *   so a controller can move the frames while the video plays.
 *
 * SUBSCRIBER (receiver, srv)
 *   moqpico sub --connect wifi=10.0.1.1:4443 --connect 5g=10.0.2.1:4443
 *               --out received.ts --pieces pieces.csv --log objects.csv [--duration 900]
 *   One MOQT session per --connect, each subscribes to the same track. Objects are written
 *   back at their byte offset; pieces.csv = "offset,length,sent_us,arrival_us" per object.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include <time.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <picoquic.h>
#include <picoquic_utils.h>
#include <picoquic_packet_loop.h>
#include "moqt.h"

#define HDR 21                    /* app header in every object payload */
#define MAX_LABELS 8
#define MAX_CONNECT 8

typedef struct {
    uint32_t frame;
    char type;
    double t;
    uint64_t off;
    uint32_t len;
} unit_t;

typedef struct {
    char name[32];
    char ip[64];
} label_t;

static struct {
    int is_pub;
    int verbose;
    /* common */
    moqt_ns_t ns;
    char track[MOQT_MAX_NAME];
    char relay[128];
    /* publisher */
    int port;
    const char* cert;
    const char* key;
    const char* ts;
    const char* units_file;
    int synthetic;
    int syn_groups, syn_objects, syn_size, syn_interval_ms;
    label_t labels[MAX_LABELS];
    int nlabels;
    char i_path[32], pb_path[32];
    const char* path_file;
    int i_dgram, pb_dgram;
    int wait_subs;
    double start_delay;
    int chunk;
    int dgram_age_ms;
    int stream_age_ms;
    int cc_floor_kb;               /* watchdog: restart CC of the I-frame session when its window stays below this */
    const char* sent_log;
    double linger;
    const char* started_file;
    const char* cc;
    /* subscriber */
    char connect_label[MAX_CONNECT][32];
    char connect_addr[MAX_CONNECT][128];
    int nconnect;
    const char* out;
    const char* pieces;
    const char* obj_log;
    double duration;
    int raw;
    int gso;
} cfg;

static moqt_ctx_t mctx;

/* ================================================================ helpers */
/* wall clock (us since 1970) for the times written in the objects and logs: the same on the sender
 * and the receiver in Mininet, and comparable with Python's time.time() (picoquic's own clock is
 * CLOCK_MONOTONIC on Linux) */
static uint64_t wall_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (uint64_t)ts.tv_sec * 1000000ull + (uint64_t)ts.tv_nsec / 1000ull;
}

static int parse_hostport(const char* s, struct sockaddr_storage* ss)
{
    char host[128];
    const char* c = strrchr(s, ':');
    if (c == NULL) {
        return -1;
    }
    if (strncmp(s, "moqt://", 7) == 0) {
        s += 7;
    }
    size_t hl = (size_t)(c - s);
    if (hl >= sizeof(host)) {
        return -1;
    }
    memcpy(host, s, hl);
    host[hl] = 0;
    int port = atoi(c + 1);
    memset(ss, 0, sizeof(*ss));
    struct sockaddr_in* a = (struct sockaddr_in*)ss;
    a->sin_family = AF_INET;
    a->sin_port = htons((uint16_t)port);
    if (strcmp(host, "localhost") == 0) {
        strcpy(host, "127.0.0.1");
    }
    return inet_pton(AF_INET, host, &a->sin_addr) == 1 ? 0 : -1;
}

static void put_u64(uint8_t* p, uint64_t v)
{
    for (int i = 7; i >= 0; i--) {
        p[i] = (uint8_t)v;
        v >>= 8;
    }
}

static void put_u32(uint8_t* p, uint32_t v)
{
    for (int i = 3; i >= 0; i--) {
        p[i] = (uint8_t)v;
        v >>= 8;
    }
}

static uint64_t get_u64(const uint8_t* p)
{
    uint64_t v = 0;
    for (int i = 0; i < 8; i++) {
        v = (v << 8) | p[i];
    }
    return v;
}

static uint32_t get_u32(const uint8_t* p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

/* the name of a session: its --label name if the local IP matches one, else its own label */
static const char* sess_name(moqt_session_t* s)
{
    for (int i = 0; i < cfg.nlabels; i++) {
        if (strcmp(s->label, cfg.labels[i].ip) == 0) {
            return cfg.labels[i].name;
        }
    }
    return s->label;
}

/* ================================================================ publisher */
static struct {
    unit_t* u;
    size_t n, k;
    FILE* f;
    uint8_t* buf;
    size_t bufcap;
    uint64_t t0;
    int started;
    uint64_t group;
    uint64_t object;
    int have_group;
    uint64_t done_at;
    int done_sent;
    uint64_t last_path_check;
    time_t path_mtime;
    uint64_t last_status;
    uint64_t switches;
    FILE* log;
    uint64_t syn_off;
    uint64_t sent_units;
    uint64_t skipped_objects;
    int sent_now;
} pub;

static int load_units(const char* path)
{
    FILE* f = fopen(path, "r");
    if (f == NULL) {
        fprintf(stderr, "cannot open %s\n", path);
        return -1;
    }
    char line[256];
    size_t cap = 0;
    while (fgets(line, sizeof(line), f)) {
        unsigned fr;
        char ty;
        double t;
        unsigned long long off;
        unsigned len;
        if (sscanf(line, "%u,%c,%lf,%llu,%u", &fr, &ty, &t, &off, &len) != 5) {
            continue;                     /* header */
        }
        if (pub.n == cap) {
            cap = cap ? cap * 2 : 4096;
            pub.u = (unit_t*)realloc(pub.u, cap * sizeof(unit_t));
        }
        pub.u[pub.n].frame = fr;
        pub.u[pub.n].type = ty;
        pub.u[pub.n].t = t;
        pub.u[pub.n].off = off;
        pub.u[pub.n].len = len;
        pub.n++;
    }
    fclose(f);
    fprintf(stderr, "*** %zu frames to send (%.1f s of video)\n", pub.n, pub.n ? pub.u[pub.n - 1].t : 0.0);
    return pub.n ? 0 : -1;
}

static void read_path_file(uint64_t now)
{
    if (cfg.path_file == NULL || now - pub.last_path_check < 100000) {
        return;
    }
    pub.last_path_check = now;
    FILE* f = fopen(cfg.path_file, "r");
    if (f == NULL) {
        return;
    }
    char a[32] = "", b[32] = "";
    if (fscanf(f, "%31s %31s", a, b) == 2 && (strcmp(a, cfg.i_path) || strcmp(b, cfg.pb_path))) {
        double t = pub.started ? (now - pub.t0) / 1e6 : 0.0;
        fprintf(stderr, "*** %4.0f s  path change: I frames %s -> %s, P/B frames %s -> %s\n", t, cfg.i_path, a,
            cfg.pb_path, b);
        snprintf(cfg.i_path, sizeof(cfg.i_path), "%s", a);
        snprintf(cfg.pb_path, sizeof(cfg.pb_path), "%s", b);
        pub.switches++;
    }
    fclose(f);
}

/* the session + subscription that should carry objects for path "name" */
static moqt_session_t* target(const char* name, moqt_pubsub_t** psub)
{
    moqt_session_t* any = NULL;
    moqt_pubsub_t* any_sub = NULL;
    for (moqt_session_t* s = mctx.sessions; s; s = s->next) {
        if (s->closed) {
            continue;
        }
        for (int i = 0; i < MOQT_MAX_SUBS; i++) {
            moqt_pubsub_t* sub = &s->subs[i];
            if (!sub->used || sub->done_sent) {
                continue;
            }
            if (strcmp(sess_name(s), name) == 0) {
                *psub = sub;
                return s;
            }
            if (any == NULL) {
                any = s;
                any_sub = sub;
            }
        }
    }
    *psub = any_sub;
    return any;
}

static int active_subs(void)
{
    int n = 0;
    for (moqt_session_t* s = mctx.sessions; s; s = s->next) {
        if (!s->closed) {
            n += moqt_count_active_subs(s);
        }
    }
    return n;
}

static void send_unit(const unit_t* u, const uint8_t* data, uint64_t now)
{
    int is_i = (u->type == 'I');
    if (is_i || !pub.have_group) {
        if (pub.have_group) {
            pub.group++;
        }
        pub.have_group = 1;
        pub.object = 0;
    }
    moqt_pubsub_t* sub = NULL;
    moqt_session_t* s = target(is_i ? cfg.i_path : cfg.pb_path, &sub);
    int dgram = is_i ? cfg.i_dgram : cfg.pb_dgram;
    uint64_t subgroup = is_i ? 0 : 1;
    uint8_t prio = is_i ? 0 : (u->type == 'P' ? 64 : 128);
    uint8_t obuf[2048];
    for (uint32_t a = 0; a < u->len; a += (uint32_t)cfg.chunk) {
        uint32_t n = u->len - a < (uint32_t)cfg.chunk ? u->len - a : (uint32_t)cfg.chunk;
        put_u64(obuf, wall_us());
        put_u64(obuf + 8, u->off + a);
        put_u32(obuf + 16, u->frame);
        obuf[20] = (uint8_t)u->type;
        memcpy(obuf + HDR, data + a, n);
        uint64_t obj = pub.object++;
        int ok = (s != NULL) && moqt_send_object(s, sub, pub.group, subgroup, obj, prio, dgram, obuf, HDR + n) == 0;
        if (!ok) {
            pub.skipped_objects++;
        }
        moqt_note_published(&mctx, pub.group, obj);
        if (pub.log) {
            fprintf(pub.log, "%llu,%u,%c,%llu,%llu,%s,%s,%llu,%u\n", (unsigned long long)wall_us(), u->frame, u->type,
                (unsigned long long)pub.group, (unsigned long long)obj, s ? sess_name(s) : "-",
                ok ? (dgram ? "dgram" : "stream") : "none", (unsigned long long)(u->off + a), n);
        }
    }
    pub.sent_units++;
    pub.sent_now = 1;
}

static void pub_status(uint64_t now, int final)
{
    double t = pub.started ? (now - pub.t0) / 1e6 : 0.0;
    fprintf(stderr, "*** %s%5.0f s  frames %llu/%zu  I->%s  P/B->%s |", final ? "END " : "", t,
        (unsigned long long)pub.sent_units, cfg.synthetic ? (size_t)cfg.syn_groups * cfg.syn_objects : pub.n,
        cfg.i_path, cfg.pb_path);
    for (moqt_session_t* s = mctx.sessions; s; s = s->next) {
        fprintf(stderr, "  %s%s: %llu stream obj (%llu groups too late, %llu cc restarts), %llu dgram obj (%llu queued, %llu too old)",
            sess_name(s), s->closed ? "(closed)" : "", (unsigned long long)s->n_stream_obj,
            (unsigned long long)s->n_stream_reset, (unsigned long long)s->n_cc_restart, (unsigned long long)s->n_dgram_obj,
            (unsigned long long)s->dg_queued, (unsigned long long)s->n_dgram_stale);
        if (s->cnx && !s->closed) {
            picoquic_path_quality_t q;
            memset(&q, 0, sizeof(q));
            picoquic_get_default_path_quality(s->cnx, &q);
            fprintf(stderr, " [rtt %.0f ms, cwnd %.0f KB, pacing %.1f Mbit/s, lost %llu/%llu pkts]", q.rtt / 1000.0,
                q.cwin / 1024.0, q.pacing_rate * 8 / 1e6, (unsigned long long)q.lost, (unsigned long long)q.sent);
        }
    }
    fprintf(stderr, "\n");
}

/* Watchdog for the session that carries the I frames. BBR (v3) can keep a small window after a lossy
 * period when the traffic is bursty (one I frame per second): every round with a loss lowers its
 * short-term bound to what was in flight, and bursty traffic never fills the window. Then a 200 KB
 * I frame needs many round trips and misses the buffer although the path is fine. If the window stays
 * below cfg.cc_floor_kb for 2 s while the RTT is close to its minimum (no queue, so the path is not
 * congested) and less than 5 % of the packets were lost, the congestion controller of that session is started again (fresh BBR startup). */
static void cc_watchdog(uint64_t now)
{
    static picoquic_congestion_algorithm_t const* alg = NULL;
    if (cfg.cc_floor_kb <= 0 || !pub.started || now - pub.t0 < 10000000) {   /* not during the first startup */
        return;
    }
    if (alg == NULL) {
        alg = picoquic_get_congestion_algorithm(cfg.cc ? cfg.cc : "bbr");
        if (alg == NULL) {
            return;
        }
    }
    for (moqt_session_t* s = mctx.sessions; s; s = s->next) {
        if (s->closed || s->cnx == NULL || strcmp(sess_name(s), cfg.i_path) != 0) {
            s->cc_low_since = 0;
            continue;
        }
        picoquic_path_quality_t q;
        memset(&q, 0, sizeof(q));
        picoquic_get_default_path_quality(s->cnx, &q);
        int low = q.cwin < (uint64_t)cfg.cc_floor_kb * 1024 && q.rtt_min > 0 && q.rtt < 2 * q.rtt_min;
        if (!low) {
            s->cc_low_since = 0;
            continue;
        }
        if (s->cc_low_since == 0) {
            s->cc_low_since = now;
            s->cc_lost0 = q.lost;
            s->cc_sent0 = q.sent;
            continue;
        }
        if (now - s->cc_low_since < 2000000 || now - s->cc_last_restart < 10000000) {
            continue;
        }
        uint64_t sent = q.sent - s->cc_sent0, lost = q.lost - s->cc_lost0;
        if (sent < 20 || lost * 20 > sent) {           /* path really lossy (> 5 %) or idle: leave it alone */
            s->cc_low_since = 0;
            continue;
        }
        {
            double t = (now - pub.t0) / 1e6;
            fprintf(stderr, "*** %4.0f s  %s: window %.0f KB at rtt %.0f ms (min %.0f ms) for 2 s, loss %.1f %% - congestion control restarted\n",
                t, sess_name(s), q.cwin / 1024.0, q.rtt / 1000.0, q.rtt_min / 1000.0, 100.0 * lost / sent);
            picoquic_set_congestion_algorithm(s->cnx, alg);
            s->cc_last_restart = now;
            s->cc_low_since = 0;
            s->n_cc_restart++;
        }
    }
}

/* returns 1 when everything is finished */
static int pub_tick(uint64_t now, int64_t* next_delay)
{
    read_path_file(now);
    cc_watchdog(now);
    if (!pub.started) {
        static uint64_t ready_since = 0;
        if (active_subs() >= cfg.wait_subs) {
            if (ready_since == 0) {
                ready_since = now;
                fprintf(stderr, "*** %d subscription(s) - starting in %.1f s\n", active_subs(), cfg.start_delay);
            }
            if (now - ready_since >= (uint64_t)(cfg.start_delay * 1e6)) {
                pub.started = 1;
                pub.t0 = now;
                pub.last_status = now;
                if (cfg.started_file) {               /* tells a controller when the video started */
                    FILE* sf = fopen(cfg.started_file, "w");
                    if (sf) {
                        fprintf(sf, "%.6f\n", wall_us() / 1e6);
                        fclose(sf);
                    }
                }
            }
        } else {
            ready_since = 0;
        }
        if (!pub.started) {
            *next_delay = 50000;
            return 0;
        }
    }
    if (now - pub.last_status > 10000000) {
        pub_status(now, 0);
        pub.last_status = now;
    }
    size_t total = cfg.synthetic ? (size_t)cfg.syn_groups * cfg.syn_objects : pub.n;
    while (pub.k < total) {
        uint64_t due;
        if (cfg.synthetic) {
            due = pub.t0 + (uint64_t)pub.k * cfg.syn_interval_ms * 1000ull;
        } else {
            due = pub.t0 + (uint64_t)(pub.u[pub.k].t * 1e6);
        }
        if (due > now) {
            int64_t d = (int64_t)(due - now);
            if (d < *next_delay) {
                *next_delay = d;
            }
            break;
        }
        if (cfg.synthetic) {
            unit_t u;
            u.frame = (uint32_t)pub.k;
            u.type = (pub.k % cfg.syn_objects == 0) ? 'I' : 'P';
            u.t = 0;
            u.off = pub.syn_off;
            u.len = (uint32_t)cfg.syn_size;
            pub.syn_off += u.len;
            if (pub.bufcap < u.len) {
                pub.bufcap = u.len;
                pub.buf = (uint8_t*)realloc(pub.buf, pub.bufcap);
            }
            memset(pub.buf, (int)(pub.k & 0xFF), u.len);
            send_unit(&u, pub.buf, now);
        } else {
            unit_t* u = &pub.u[pub.k];
            if (pub.bufcap < u->len) {
                pub.bufcap = u->len;
                pub.buf = (uint8_t*)realloc(pub.buf, pub.bufcap);
            }
            if (fseeko(pub.f, (off_t)u->off, SEEK_SET) != 0 || fread(pub.buf, 1, u->len, pub.f) != u->len) {
                fprintf(stderr, "read error at frame %u\n", u->frame);
                return 1;
            }
            send_unit(u, pub.buf, now);
        }
        pub.k++;
    }
    if (pub.sent_now) {
        pub.sent_now = 0;
        *next_delay = 0;                  /* let picoquic send what was just queued */
    }
    if (pub.k >= total) {
        if (pub.done_at == 0) {
            pub.done_at = now;
        }
        if (!pub.done_sent && now - pub.done_at > 500000) {
            for (moqt_session_t* s = mctx.sessions; s; s = s->next) {
                for (int i = 0; i < MOQT_MAX_SUBS && !s->closed; i++) {
                    if (s->subs[i].used && !s->subs[i].done_sent) {
                        moqt_publish_done(s, &s->subs[i], MOQT_DONE_TRACK_ENDED, "end of video");
                    }
                }
            }
            pub.done_sent = 1;
            pub_status(now, 1);
        }
        if (pub.done_sent && now - pub.done_at > (uint64_t)(cfg.linger * 1e6)) {
            return 1;
        }
        *next_delay = 100000;
    }
    return 0;
}

static int pub_on_subscribe(moqt_session_t* s, moqt_pubsub_t* sub)
{
    if (!moqt_ns_equal(&sub->ns, &cfg.ns) || strcmp(sub->name, cfg.track) != 0) {
        return -1;
    }
    fprintf(stderr, "*** subscriber on path %s (session %s), track alias %llu\n", sess_name(s), s->label,
        (unsigned long long)sub->alias);
    return 0;
}

static void pub_on_setup(moqt_session_t* s)
{
    if (s->is_client) {                   /* publishing through a relay */
        moqt_publish_namespace(s, &cfg.ns);
    }
}

/* ================================================================ subscriber */
static struct {
    int fd;
    FILE* pieces;
    FILE* log;
    uint64_t first_us, last_us;
    uint64_t n_obj, n_bytes, n_dup;
    uint64_t done_at;
    uint64_t start;
    uint64_t last_status;
    moqt_session_t* sess[MAX_CONNECT];
} sub;

static void sub_on_setup(moqt_session_t* s)
{
    moqt_subscribe(s, &cfg.ns, cfg.track, 128);
}

static void sub_on_object(moqt_session_t* s, uint64_t alias, uint64_t group, uint64_t subgroup, uint64_t object,
    int is_dgram, const uint8_t* p, size_t len)
{
    uint64_t now = wall_us();
    (void)alias;
    if (sub.first_us == 0) {
        sub.first_us = now;
    }
    sub.last_us = now;
    sub.n_obj++;
    if (!cfg.raw && len >= HDR) {
        uint64_t sent = get_u64(p);
        uint64_t off = get_u64(p + 8);
        uint32_t frame = get_u32(p + 16);
        char type = (char)p[20];
        size_t n = len - HDR;
        sub.n_bytes += n;
        if (sub.fd >= 0 && pwrite(sub.fd, p + HDR, n, (off_t)off) != (ssize_t)n) {
            fprintf(stderr, "write error\n");
        }
        if (sub.pieces) {
            fprintf(sub.pieces, "%llu,%zu,%llu,%llu\n", (unsigned long long)off, n, (unsigned long long)sent,
                (unsigned long long)now);
        }
        if (sub.log) {
            fprintf(sub.log, "%llu,%s,%s,%llu,%lld,%llu,%u,%c,%llu,%zu,%.3f\n", (unsigned long long)now, s->label,
                is_dgram ? "dgram" : "stream", (unsigned long long)group,
                subgroup == UINT64_MAX ? -1LL : (long long)subgroup, (unsigned long long)object, frame,
                type, (unsigned long long)off, n, (now - sent) / 1000.0);
        }
    } else {
        sub.n_bytes += len;
        if (sub.log) {
            fprintf(sub.log, "%llu,%s,%s,%llu,%lld,%llu,,,,%zu,\n", (unsigned long long)now, s->label,
                is_dgram ? "dgram" : "stream", (unsigned long long)group,
                subgroup == UINT64_MAX ? -1LL : (long long)subgroup, (unsigned long long)object, len);
        }
        if (cfg.verbose) {
            fprintf(stderr, "[%s] object %llu/%llu %s %zu bytes\n", s->label, (unsigned long long)group,
                (unsigned long long)object, is_dgram ? "datagram" : "stream", len);
        }
    }
}

static void sub_status(uint64_t now, int final)
{
    fprintf(stderr, "*** %s%5.0f s  %llu objects, %.1f MB |", final ? "END " : "", (now - sub.start) / 1e6,
        (unsigned long long)sub.n_obj, sub.n_bytes / 1e6);
    for (int i = 0; i < cfg.nconnect; i++) {
        moqt_session_t* s = sub.sess[i];
        if (s) {
            fprintf(stderr, "  %s: %llu stream obj, %llu dgram obj%s", s->label, (unsigned long long)s->rx_stream_obj,
                (unsigned long long)s->rx_dgram_obj,
                s->sub_state == 4 ? " (done)" : s->sub_state == 3 ? " (refused)" : s->closed ? " (closed)" : "");
        }
    }
    fprintf(stderr, "\n");
}

static int sub_tick(uint64_t now, int64_t* next_delay)
{
    if (sub.start == 0) {
        sub.start = now;
        sub.last_status = now;
    }
    if (now - sub.last_status > 10000000) {
        sub_status(now, 0);
        sub.last_status = now;
        if (sub.pieces) {
            fflush(sub.pieces);
        }
        if (sub.log) {
            fflush(sub.log);
        }
    }
    int all_done = 1;
    for (int i = 0; i < cfg.nconnect; i++) {
        moqt_session_t* s = sub.sess[i];
        if (s && !s->closed && s->sub_state != 3 && s->sub_state != 4) {
            all_done = 0;
        }
    }
    if (all_done && sub.done_at == 0) {
        sub.done_at = now;
    }
    if (sub.done_at && now - sub.done_at > 2000000) {
        return 1;
    }
    if (cfg.duration > 0 && now - sub.start > (uint64_t)(cfg.duration * 1e6)) {
        fprintf(stderr, "*** --duration reached\n");
        return 1;
    }
    *next_delay = 100000;
    return 0;
}

/* ================================================================ packet loop */
static uint64_t closing_since = 0;

static int loop_cb(picoquic_quic_t* quic, picoquic_packet_loop_cb_enum mode, void* ctx, void* argp)
{
    (void)quic;
    (void)ctx;
    uint64_t now = picoquic_current_time();
    int64_t delay = 1000000;
    int finished = 0;
    switch (mode) {
    case picoquic_packet_loop_ready:
        if (argp != NULL) {
            ((picoquic_packet_loop_options_t*)argp)->do_time_check = 1;   /* we pace the video frames */
        }
        fprintf(stderr, "*** %s ready (MOQT draft-16, ALPN %s)\n", cfg.is_pub ? "publisher" : "subscriber", MOQT_ALPN);
        return 0;
    case picoquic_packet_loop_after_receive:
    case picoquic_packet_loop_after_send:
    case picoquic_packet_loop_time_check:
        if (closing_since == 0) {
            if (cfg.is_pub) {
                moqt_expire_streams(&mctx, now);     /* delivery timeout of late I-frame groups */
            }
            finished = cfg.is_pub ? pub_tick(now, &delay) : sub_tick(now, &delay);
            if (finished) {
                /* close every QUIC connection cleanly (CONNECTION_CLOSE), then leave the loop */
                closing_since = now;
                finished = 0;
                for (moqt_session_t* s = mctx.sessions; s; s = s->next) {
                    moqt_close(s, MOQT_ERR_NO_ERROR);
                }
            }
        }
        if (closing_since != 0) {
            int open = 0;
            for (moqt_session_t* s = mctx.sessions; s; s = s->next) {
                open += !s->closed;
            }
            finished = open == 0 || now - closing_since > 1000000;
            delay = 20000;
        }
        if (mode == picoquic_packet_loop_time_check && argp != NULL) {
            packet_loop_time_check_arg_t* tc = (packet_loop_time_check_arg_t*)argp;
            if (tc->delta_t > delay) {
                tc->delta_t = delay;
            }
        }
        return finished ? PICOQUIC_NO_ERROR_TERMINATE_PACKET_LOOP : 0;
    default:
        return 0;
    }
}

/* ================================================================ main */
static void usage(void)
{
    fprintf(stderr,
        "usage:\n"
        "  moqpico pub (--ts F --units F | --synthetic G,O,BYTES,MS) [--port 4443 --cert F --key F]\n"
        "              [--relay HOST:PORT] [--ns a/b] [--track video] [--label NAME=IP]...\n"
        "              [--i-path NAME] [--pb-path NAME] [--path-file F] [--i-mode stream|dgram]\n"
        "              [--pb-mode dgram|stream] [--wait-subs N] [--start-delay S] [--chunk BYTES]\n"
        "              [--dgram-age MS] [--stream-age MS (default 500, 0 = off)] [--cc-floor KB (default 64, 0 = off)] [--log sent.csv] [--linger S] [--started-file F] [--cc bbr|bbr1|cubic|newreno (default bbr)] [--gso] [-v]\n"
        "  moqpico sub --connect [NAME=]HOST:PORT ... [--ns a/b] [--track video] [--out F]\n"
        "              [--pieces F] [--log F] [--duration S] [--raw] [-v]\n");
    exit(2);
}

int main(int argc, char** argv)
{
    if (argc < 2) {
        usage();
    }
    memset(&cfg, 0, sizeof(cfg));
    cfg.is_pub = strcmp(argv[1], "pub") == 0;
    if (!cfg.is_pub && strcmp(argv[1], "sub") != 0) {
        usage();
    }
    moqt_ns_parse(&cfg.ns, "moqpico");
    strcpy(cfg.track, "video");
    cfg.port = 4443;
    strcpy(cfg.i_path, "5g");
    strcpy(cfg.pb_path, "wifi");
    cfg.pb_dgram = 1;
    cfg.wait_subs = 1;
    cfg.start_delay = 1.0;
    cfg.chunk = 940;
    cfg.dgram_age_ms = 1000;
    cfg.stream_age_ms = 500;
    cfg.cc_floor_kb = 64;
    cfg.linger = 3.0;
    cfg.cc = "bbr";                     /* same as picoquicdemo (Parts 3-8); picoquic's own default is newreno */

    for (int i = 2; i < argc; i++) {
        const char* a = argv[i];
        const char* v = (i + 1 < argc) ? argv[i + 1] : NULL;
#define ARG(name) (strcmp(a, name) == 0 && v && ++i)
        if (strcmp(a, "-v") == 0) {
            cfg.verbose = 1;
        } else if (strcmp(a, "--raw") == 0) {
            cfg.raw = 1;
        } else if (strcmp(a, "--gso") == 0) {
            /* UDP GSO is OFF by default (like picoquicdemo -0 in Parts 3-8): with GSO the kernel
             * hands up to ~40 packets to netem as ONE packet, so one emulated loss drops them all */
            cfg.gso = 1;
        } else if (ARG("--ns")) {
            moqt_ns_parse(&cfg.ns, v);
        } else if (ARG("--track")) {
            snprintf(cfg.track, sizeof(cfg.track), "%s", v);
        } else if (ARG("--relay")) {
            snprintf(cfg.relay, sizeof(cfg.relay), "%s", v);
        } else if (ARG("--port")) {
            cfg.port = atoi(v);
        } else if (ARG("--cert")) {
            cfg.cert = v;
        } else if (ARG("--key")) {
            cfg.key = v;
        } else if (ARG("--ts")) {
            cfg.ts = v;
        } else if (ARG("--units")) {
            cfg.units_file = v;
        } else if (ARG("--synthetic")) {
            cfg.synthetic = 1;
            if (sscanf(v, "%d,%d,%d,%d", &cfg.syn_groups, &cfg.syn_objects, &cfg.syn_size, &cfg.syn_interval_ms) != 4) {
                usage();
            }
        } else if (ARG("--label")) {
            const char* eq = strchr(v, '=');
            if (!eq || cfg.nlabels >= MAX_LABELS) {
                usage();
            }
            snprintf(cfg.labels[cfg.nlabels].name, 32, "%.*s", (int)(eq - v), v);
            snprintf(cfg.labels[cfg.nlabels].ip, 64, "%s", eq + 1);
            cfg.nlabels++;
        } else if (ARG("--i-path")) {
            snprintf(cfg.i_path, sizeof(cfg.i_path), "%s", v);
        } else if (ARG("--pb-path")) {
            snprintf(cfg.pb_path, sizeof(cfg.pb_path), "%s", v);
        } else if (ARG("--path-file")) {
            cfg.path_file = v;
        } else if (ARG("--i-mode")) {
            cfg.i_dgram = strcmp(v, "dgram") == 0;
        } else if (ARG("--pb-mode")) {
            cfg.pb_dgram = strcmp(v, "dgram") == 0;
        } else if (ARG("--wait-subs")) {
            cfg.wait_subs = atoi(v);
        } else if (ARG("--start-delay")) {
            cfg.start_delay = atof(v);
        } else if (ARG("--chunk")) {
            cfg.chunk = atoi(v);
        } else if (ARG("--dgram-age")) {
            cfg.dgram_age_ms = atoi(v);
        } else if (ARG("--stream-age")) {
            cfg.stream_age_ms = atoi(v);
        } else if (ARG("--cc-floor")) {
            cfg.cc_floor_kb = atoi(v);
        } else if (ARG("--log")) {
            if (cfg.is_pub) {
                cfg.sent_log = v;
            } else {
                cfg.obj_log = v;
            }
        } else if (ARG("--started-file")) {
            cfg.started_file = v;
        } else if (ARG("--cc")) {
            cfg.cc = v;
        } else if (ARG("--linger")) {
            cfg.linger = atof(v);
        } else if (ARG("--connect")) {
            if (cfg.nconnect >= MAX_CONNECT) {
                usage();
            }
            const char* eq = strchr(v, '=');
            int k = cfg.nconnect++;
            if (eq) {
                snprintf(cfg.connect_label[k], 32, "%.*s", (int)(eq - v), v);
                snprintf(cfg.connect_addr[k], 128, "%s", eq + 1);
            } else {
                snprintf(cfg.connect_label[k], 32, "s%d", k);
                snprintf(cfg.connect_addr[k], 128, "%s", v);
            }
        } else if (ARG("--out")) {
            cfg.out = v;
        } else if (ARG("--pieces")) {
            cfg.pieces = v;
        } else if (ARG("--duration")) {
            cfg.duration = atof(v);
        } else {
            fprintf(stderr, "unknown or incomplete option: %s\n", a);
            usage();
        }
#undef ARG
    }
    if (cfg.chunk < 1 || cfg.chunk > 1100) {
        fprintf(stderr, "--chunk must be 1..1100 (one object must fit in a QUIC datagram)\n");
        return 2;
    }

    uint64_t now = picoquic_current_time();
    int server = cfg.is_pub && cfg.relay[0] == 0;
    if (server && (cfg.cert == NULL || cfg.key == NULL)) {
        fprintf(stderr, "the publisher needs --cert and --key (picoquic/certs/cert.pem and key.pem)\n");
        return 2;
    }
    memset(&mctx, 0, sizeof(mctx));
    mctx.verbose = cfg.verbose;
    mctx.next_alias = 1;
    mctx.dgram_max_age_us = (uint64_t)cfg.dgram_age_ms * 1000ull;
    mctx.stream_max_age_us = cfg.is_pub ? (uint64_t)cfg.stream_age_ms * 1000ull : 0;
    if (cfg.is_pub) {
        mctx.cb.on_setup = pub_on_setup;
        mctx.cb.on_subscribe = pub_on_subscribe;
    } else {
        mctx.cb.on_setup = sub_on_setup;
        mctx.cb.on_object = sub_on_object;
    }
    mctx.quic = picoquic_create(16, server ? cfg.cert : NULL, server ? cfg.key : NULL, NULL, MOQT_ALPN,
        moqt_picoquic_callback, &mctx, NULL, NULL, NULL, now, NULL, NULL, NULL, 0);
    if (mctx.quic == NULL) {
        fprintf(stderr, "cannot create the QUIC context\n");
        return 1;
    }
    if (cfg.cc) {
        picoquic_register_all_congestion_control_algorithms();
        picoquic_congestion_algorithm_t const* alg = picoquic_get_congestion_algorithm(cfg.cc);
        if (alg == NULL) {
            fprintf(stderr, "unknown congestion control %s\n", cfg.cc);
            return 2;
        }
        picoquic_set_default_congestion_algorithm(mctx.quic, alg);
    }
    picoquic_set_null_verifier(mctx.quic);         /* test bed: self-signed certificates */
    moqt_configure_quic(mctx.quic);

    if (cfg.is_pub) {
        if (cfg.synthetic) {
            fprintf(stderr, "*** synthetic track: %d groups x %d objects of %d bytes, one every %d ms\n",
                cfg.syn_groups, cfg.syn_objects, cfg.syn_size, cfg.syn_interval_ms);
        } else {
            if (cfg.ts == NULL || cfg.units_file == NULL || load_units(cfg.units_file) != 0) {
                fprintf(stderr, "need --ts and --units (or --synthetic)\n");
                return 2;
            }
            pub.f = fopen(cfg.ts, "rb");
            if (pub.f == NULL) {
                fprintf(stderr, "cannot open %s\n", cfg.ts);
                return 1;
            }
        }
        if (cfg.sent_log) {
            pub.log = fopen(cfg.sent_log, "w");
            if (pub.log) {
                fprintf(pub.log, "time_us,frame,type,group,object,path,kind,offset,length\n");
            }
        }
        if (cfg.relay[0]) {
            struct sockaddr_storage ss;
            if (parse_hostport(cfg.relay, &ss) != 0 || moqt_connect(&mctx, (struct sockaddr*)&ss, "localhost",
                "relay", now) == NULL) {
                fprintf(stderr, "cannot connect to relay %s\n", cfg.relay);
                return 1;
            }
            fprintf(stderr, "*** publishing %s/%s through relay %s\n", cfg.ns.f[0], cfg.track, cfg.relay);
        } else {
            fprintf(stderr, "*** publishing on UDP port %d, I frames -> %s (%s), P/B frames -> %s (%s)\n", cfg.port,
                cfg.i_path, cfg.i_dgram ? "datagrams" : "stream", cfg.pb_path, cfg.pb_dgram ? "datagrams" : "stream");
        }
    } else {
        if (cfg.nconnect == 0) {
            usage();
        }
        sub.fd = -1;
        if (cfg.out) {
            sub.fd = open(cfg.out, O_WRONLY | O_CREAT | O_TRUNC, 0644);
            if (sub.fd < 0) {
                fprintf(stderr, "cannot write %s\n", cfg.out);
                return 1;
            }
        }
        if (cfg.pieces) {
            sub.pieces = fopen(cfg.pieces, "w");
        }
        if (cfg.obj_log) {
            sub.log = fopen(cfg.obj_log, "w");
            if (sub.log) {
                fprintf(sub.log, "arrival_us,path,kind,group,subgroup,object,frame,type,offset,length,delay_ms\n");
            }
        }
        for (int i = 0; i < cfg.nconnect; i++) {
            struct sockaddr_storage ss;
            if (parse_hostport(cfg.connect_addr[i], &ss) != 0) {
                fprintf(stderr, "bad address %s\n", cfg.connect_addr[i]);
                return 2;
            }
            sub.sess[i] = moqt_connect(&mctx, (struct sockaddr*)&ss, "localhost", cfg.connect_label[i], now);
            if (sub.sess[i] == NULL) {
                fprintf(stderr, "cannot connect to %s\n", cfg.connect_addr[i]);
                return 1;
            }
            fprintf(stderr, "*** subscribing to %s on path %s (%s)\n", cfg.track, cfg.connect_label[i],
                cfg.connect_addr[i]);
        }
    }

    int ret = picoquic_packet_loop(mctx.quic, server ? cfg.port : 0, AF_INET, 0, 4 * 1024 * 1024, !cfg.gso, loop_cb, NULL);
    if (ret == PICOQUIC_NO_ERROR_TERMINATE_PACKET_LOOP) {
        ret = 0;
    }
    now = picoquic_current_time();
    if (cfg.is_pub) {
        if (!pub.done_sent) {
            pub_status(now, 1);
        }
        fprintf(stderr, "*** path changes: %llu, objects with no subscriber: %llu\n",
            (unsigned long long)pub.switches, (unsigned long long)pub.skipped_objects);
        if (pub.log) {
            fclose(pub.log);
        }
        if (pub.f) {
            fclose(pub.f);
        }
    } else {
        sub_status(now, 1);
        if (sub.fd >= 0) {
            close(sub.fd);
        }
        if (sub.pieces) {
            fclose(sub.pieces);
        }
        if (sub.log) {
            fclose(sub.log);
        }
    }
    picoquic_free(mctx.quic);
    return ret;
}
