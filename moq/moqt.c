/*
 * moqt.c - MoQ Transport draft-16 on top of picoquic (see moqt.h).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <picoquic.h>
#include <picoquic_utils.h>
#include <picoquic_internal.h>   /* stream send/ack state for the delivery timeout */
#include "moqt.h"

#define LOG(ctx, ...) do { if ((ctx) && (ctx)->verbose) { fprintf(stderr, __VA_ARGS__); } } while (0)

/* ======================================================================== buffers */
void mbuf_free(mbuf_t* m)
{
    free(m->b);
    m->b = NULL;
    m->n = m->cap = 0;
}

static void mbuf_need(mbuf_t* m, size_t more)
{
    if (m->n + more > m->cap) {
        size_t c = m->cap ? m->cap : 256;
        while (c < m->n + more) {
            c *= 2;
        }
        uint8_t* nb = (uint8_t*)realloc(m->b, c);
        if (nb == NULL) {
            fprintf(stderr, "moqt: out of memory\n");
            exit(1);
        }
        m->b = nb;
        m->cap = c;
    }
}

void mbuf_put(mbuf_t* m, const void* p, size_t n)
{
    if (n == 0) {
        return;
    }
    mbuf_need(m, n);
    memcpy(m->b + m->n, p, n);
    m->n += n;
}

void mbuf_u8(mbuf_t* m, uint8_t v) { mbuf_put(m, &v, 1); }

void mbuf_u16(mbuf_t* m, uint16_t v)
{
    uint8_t b[2] = { (uint8_t)(v >> 8), (uint8_t)v };
    mbuf_put(m, b, 2);
}

size_t moqt_vi_len(uint64_t v)
{
    return v < 64 ? 1 : v < 16384 ? 2 : v < 1073741824ull ? 4 : 8;
}

uint8_t* moqt_vi_write(uint8_t* p, uint64_t v)
{
    size_t n = moqt_vi_len(v);
    uint8_t pfx = n == 1 ? 0x00 : n == 2 ? 0x40 : n == 4 ? 0x80 : 0xC0;
    for (size_t i = 0; i < n; i++) {
        p[n - 1 - i] = (uint8_t)(v >> (8 * i));
    }
    p[0] |= pfx;
    return p + n;
}

void mbuf_vi(mbuf_t* m, uint64_t v)
{
    uint8_t b[8];
    uint8_t* e = moqt_vi_write(b, v);
    mbuf_put(m, b, (size_t)(e - b));
}

void mbuf_bytes(mbuf_t* m, const void* p, size_t n)
{
    mbuf_vi(m, n);
    mbuf_put(m, p, n);
}

void mbuf_consume(mbuf_t* m, size_t n)
{
    if (n >= m->n) {
        m->n = 0;
    } else {
        memmove(m->b, m->b + n, m->n - n);
        m->n -= n;
    }
}

uint64_t rd_vi(mrd_t* r)
{
    if (r->err || r->p >= r->e) {
        r->err = 1;
        return 0;
    }
    size_t n = (size_t)1 << (r->p[0] >> 6);
    if ((size_t)(r->e - r->p) < n) {
        r->err = 1;
        return 0;
    }
    uint64_t v = r->p[0] & 0x3F;
    for (size_t i = 1; i < n; i++) {
        v = (v << 8) | r->p[i];
    }
    r->p += n;
    return v;
}

uint8_t rd_u8(mrd_t* r)
{
    if (r->err || r->p >= r->e) {
        r->err = 1;
        return 0;
    }
    return *r->p++;
}

uint16_t rd_u16(mrd_t* r)
{
    uint16_t a = rd_u8(r);
    uint16_t b = rd_u8(r);
    return (uint16_t)((a << 8) | b);
}

const uint8_t* rd_bytes(mrd_t* r, size_t n)
{
    if (r->err || (size_t)(r->e - r->p) < n) {
        r->err = 1;
        return NULL;
    }
    const uint8_t* p = r->p;
    r->p += n;
    return p;
}

/* ======================================================================== names */
void moqt_ns_parse(moqt_ns_t* ns, const char* s)
{
    memset(ns, 0, sizeof(*ns));
    const char* p = s;
    while (*p && ns->n < MOQT_MAX_NS) {
        const char* q = strchr(p, '/');
        size_t len = q ? (size_t)(q - p) : strlen(p);
        if (len > 0) {
            if (len >= MOQT_MAX_NAME) {
                len = MOQT_MAX_NAME - 1;
            }
            memcpy(ns->f[ns->n], p, len);
            ns->f[ns->n][len] = 0;
            ns->n++;
        }
        if (!q) {
            break;
        }
        p = q + 1;
    }
}

int moqt_ns_equal(const moqt_ns_t* a, const moqt_ns_t* b)
{
    if (a->n != b->n) {
        return 0;
    }
    for (int i = 0; i < a->n; i++) {
        if (strcmp(a->f[i], b->f[i]) != 0) {
            return 0;
        }
    }
    return 1;
}

void moqt_ns_str(const moqt_ns_t* ns, char* out, size_t len)
{
    size_t k = 0;
    out[0] = 0;
    for (int i = 0; i < ns->n && k + 1 < len; i++) {
        k += (size_t)snprintf(out + k, len - k, "%s%s", i ? "/" : "", ns->f[i]);
    }
}

static void put_ns(mbuf_t* m, const moqt_ns_t* ns)
{
    mbuf_vi(m, (uint64_t)ns->n);
    for (int i = 0; i < ns->n; i++) {
        mbuf_bytes(m, ns->f[i], strlen(ns->f[i]));
    }
}

static void rd_ns(mrd_t* r, moqt_ns_t* ns)
{
    memset(ns, 0, sizeof(*ns));
    uint64_t n = rd_vi(r);
    if (n == 0 || n > 32) {
        r->err = 1;
        return;
    }
    for (uint64_t i = 0; i < n && !r->err; i++) {
        uint64_t l = rd_vi(r);
        const uint8_t* p = rd_bytes(r, (size_t)l);
        if (p != NULL && ns->n < MOQT_MAX_NS) {
            size_t c = l < MOQT_MAX_NAME - 1 ? (size_t)l : MOQT_MAX_NAME - 1;
            memcpy(ns->f[ns->n], p, c);
            ns->f[ns->n][c] = 0;
            ns->n++;
        }
    }
}

/* ======================================================================== parameters (Key-Value-Pairs) */
typedef void (*kvp_fn)(void* arg, uint64_t type, uint64_t ival, const uint8_t* val, size_t len);

/* reads "count" KVPs; types are deltas from the previous type (draft-16) */
static void rd_kvps(mrd_t* r, uint64_t count, kvp_fn fn, void* arg)
{
    uint64_t type = 0;
    for (uint64_t i = 0; i < count && !r->err; i++) {
        type += rd_vi(r);
        if (type & 1) {
            uint64_t l = rd_vi(r);
            const uint8_t* p = rd_bytes(r, (size_t)l);
            if (p && fn) {
                fn(arg, type, 0, p, (size_t)l);
            }
        } else {
            uint64_t v = rd_vi(r);
            if (!r->err && fn) {
                fn(arg, type, v, NULL, 0);
            }
        }
    }
}

typedef struct {
    mbuf_t b;
    uint64_t last;
    uint64_t count;
} kvp_out_t;

static void kv_int(kvp_out_t* o, uint64_t type, uint64_t v)
{
    mbuf_vi(&o->b, type - o->last);
    mbuf_vi(&o->b, v);
    o->last = type;
    o->count++;
}

static void kv_bytes(kvp_out_t* o, uint64_t type, const void* p, size_t n)
{
    mbuf_vi(&o->b, type - o->last);
    mbuf_bytes(&o->b, p, n);
    o->last = type;
    o->count++;
}

/* ======================================================================== control stream */
static void send_ctrl(moqt_session_t* s, uint64_t type, const mbuf_t* payload)
{
    if (s->cnx == NULL || !s->has_ctrl || s->closed) {
        return;
    }
    mbuf_t m = { 0 };
    mbuf_vi(&m, type);
    mbuf_u16(&m, (uint16_t)payload->n);
    mbuf_put(&m, payload->b, payload->n);
    picoquic_add_to_stream(s->cnx, s->ctrl_stream, m.b, m.n, 0);
    mbuf_free(&m);
    LOG(s->ctx, "[moqt %s] -> ctrl 0x%llx (%zu bytes)\n", s->label, (unsigned long long)type, payload->n);
}

static const char IMPL[] = "moqpico (MOQT draft-16 on picoquic)";

static void send_setup(moqt_session_t* s, const char* authority)
{
    kvp_out_t o = { 0 };
    if (s->is_client) {
        kv_int(&o, MOQT_SETUP_MAX_REQUEST_ID, 1000);
        if (authority && *authority) {
            kv_bytes(&o, MOQT_SETUP_AUTHORITY, authority, strlen(authority));
        }
    } else {
        kv_int(&o, MOQT_SETUP_MAX_REQUEST_ID, 1000);
    }
    kv_bytes(&o, MOQT_SETUP_IMPLEMENTATION, IMPL, strlen(IMPL));
    mbuf_t p = { 0 };
    mbuf_vi(&p, o.count);
    mbuf_put(&p, o.b.b, o.b.n);
    send_ctrl(s, s->is_client ? MOQT_CLIENT_SETUP : MOQT_SERVER_SETUP, &p);
    mbuf_free(&p);
    mbuf_free(&o.b);
}

static void send_request_error(moqt_session_t* s, uint64_t req, uint64_t code, const char* why)
{
    mbuf_t p = { 0 };
    mbuf_vi(&p, req);
    mbuf_vi(&p, code);
    mbuf_vi(&p, 0);                     /* retry interval: do not retry */
    mbuf_bytes(&p, why, strlen(why));
    send_ctrl(s, MOQT_REQUEST_ERROR, &p);
    mbuf_free(&p);
}

static void setup_param(void* arg, uint64_t type, uint64_t v, const uint8_t* val, size_t len)
{
    moqt_session_t* s = (moqt_session_t*)arg;
    if (type == MOQT_SETUP_MAX_REQUEST_ID) {
        s->peer_max_request_id = v;
    } else if (type == MOQT_SETUP_IMPLEMENTATION && val) {
        size_t c = len < sizeof(s->peer_impl) - 1 ? len : sizeof(s->peer_impl) - 1;
        memcpy(s->peer_impl, val, c);
        s->peer_impl[c] = 0;
    } else if (type == MOQT_SETUP_PATH && val) {
        size_t c = len < sizeof(s->peer_path) - 1 ? len : sizeof(s->peer_path) - 1;
        memcpy(s->peer_path, val, c);
        s->peer_path[c] = 0;
    }
}

typedef struct {
    uint8_t priority;
    uint64_t filter;
    uint64_t forward;
} sub_params_t;

static void subscribe_param(void* arg, uint64_t type, uint64_t v, const uint8_t* val, size_t len)
{
    sub_params_t* sp = (sub_params_t*)arg;
    if (type == MOQT_PARAM_SUBSCRIBER_PRIORITY) {
        sp->priority = (uint8_t)v;
    } else if (type == MOQT_PARAM_FORWARD) {
        sp->forward = v;
    } else if (type == MOQT_PARAM_SUBSCRIPTION_FILTER && val) {
        mrd_t r = { val, val + len, 0 };
        sp->filter = rd_vi(&r);
    }
}

static moqt_pubsub_t* find_sub(moqt_session_t* s, uint64_t req)
{
    for (int i = 0; i < MOQT_MAX_SUBS; i++) {
        if (s->subs[i].used && s->subs[i].request_id == req) {
            return &s->subs[i];
        }
    }
    return NULL;
}

static void handle_subscribe(moqt_session_t* s, mrd_t* r)
{
    moqt_pubsub_t tmp;
    memset(&tmp, 0, sizeof(tmp));
    tmp.request_id = rd_vi(r);
    rd_ns(r, &tmp.ns);
    uint64_t nl = rd_vi(r);
    const uint8_t* np = rd_bytes(r, (size_t)nl);
    sub_params_t sp = { 128, 0, 1 };
    uint64_t np_count = rd_vi(r);
    rd_kvps(r, np_count, subscribe_param, &sp);
    if (r->err || np == NULL) {
        moqt_close(s, MOQT_ERR_PROTOCOL_VIOLATION);
        return;
    }
    size_t c = nl < MOQT_MAX_NAME - 1 ? (size_t)nl : MOQT_MAX_NAME - 1;
    memcpy(tmp.name, np, c);
    tmp.name[c] = 0;
    tmp.priority = sp.priority;

    char nss[256];
    moqt_ns_str(&tmp.ns, nss, sizeof(nss));
    fprintf(stderr, "[moqt %s] SUBSCRIBE request %llu for %s/%s (filter %llu, priority %u)\n", s->label,
        (unsigned long long)tmp.request_id, nss, tmp.name, (unsigned long long)sp.filter, sp.priority);

    moqt_pubsub_t* slot = NULL;
    for (int i = 0; i < MOQT_MAX_SUBS; i++) {
        if (!s->subs[i].used) {
            slot = &s->subs[i];
            break;
        }
    }
    if (slot == NULL) {
        send_request_error(s, tmp.request_id, MOQT_ERR_INTERNAL, "too many subscriptions");
        return;
    }
    *slot = tmp;
    slot->used = 1;
    slot->alias = s->ctx->next_alias++;
    if (s->ctx->cb.on_subscribe == NULL || s->ctx->cb.on_subscribe(s, slot) != 0) {
        slot->used = 0;
        send_request_error(s, tmp.request_id, MOQT_REQERR_DOES_NOT_EXIST, "no such track");
        return;
    }
    /* SUBSCRIBE_OK */
    kvp_out_t o = { 0 };
    if (s->ctx->has_largest) {
        mbuf_t loc = { 0 };
        mbuf_vi(&loc, s->ctx->largest_group);
        mbuf_vi(&loc, s->ctx->largest_object);
        kv_bytes(&o, MOQT_PARAM_LARGEST_OBJECT, loc.b, loc.n);
        mbuf_free(&loc);
    }
    mbuf_t p = { 0 };
    mbuf_vi(&p, slot->request_id);
    mbuf_vi(&p, slot->alias);
    mbuf_vi(&p, o.count);
    mbuf_put(&p, o.b.b, o.b.n);
    /* no track extensions */
    send_ctrl(s, MOQT_SUBSCRIBE_OK, &p);
    mbuf_free(&p);
    mbuf_free(&o.b);
}

static void handle_ctrl_msg(moqt_session_t* s, uint64_t type, const uint8_t* pl, size_t len)
{
    mrd_t r = { pl, pl + len, 0 };
    moqt_ctx_t* ctx = s->ctx;
    LOG(ctx, "[moqt %s] <- ctrl 0x%llx (%zu bytes)\n", s->label, (unsigned long long)type, len);

    if (!s->setup_done && type != MOQT_CLIENT_SETUP && type != MOQT_SERVER_SETUP) {
        fprintf(stderr, "[moqt %s] message 0x%llx before SETUP\n", s->label, (unsigned long long)type);
        moqt_close(s, MOQT_ERR_PROTOCOL_VIOLATION);
        return;
    }
    switch (type) {
    case MOQT_CLIENT_SETUP:
    case MOQT_SERVER_SETUP: {
        if ((type == MOQT_CLIENT_SETUP) == (s->is_client != 0)) {
            moqt_close(s, MOQT_ERR_PROTOCOL_VIOLATION);
            return;
        }
        uint64_t n = rd_vi(&r);
        rd_kvps(&r, n, setup_param, s);
        if (r.err) {
            moqt_close(s, MOQT_ERR_PROTOCOL_VIOLATION);
            return;
        }
        if (!s->is_client) {
            send_setup(s, NULL);
        }
        s->setup_done = 1;
        fprintf(stderr, "[moqt %s] session set up (peer: %s)\n", s->label,
            s->peer_impl[0] ? s->peer_impl : "?");
        if (ctx->cb.on_setup) {
            ctx->cb.on_setup(s);
        }
        break;
    }
    case MOQT_SUBSCRIBE:
        handle_subscribe(s, &r);
        break;
    case MOQT_SUBSCRIBE_OK: {
        uint64_t req = rd_vi(&r);
        uint64_t alias = rd_vi(&r);
        uint64_t n = rd_vi(&r);
        rd_kvps(&r, n, NULL, NULL);       /* track extensions follow; not needed */
        if (r.err) {
            moqt_close(s, MOQT_ERR_PROTOCOL_VIOLATION);
            return;
        }
        if (req == s->sub_request_id && s->sub_state == 1) {
            s->sub_alias = alias;
            s->sub_state = 2;
            fprintf(stderr, "[moqt %s] SUBSCRIBE_OK, track alias %llu\n", s->label, (unsigned long long)alias);
            if (ctx->cb.on_subscribe_ok) {
                ctx->cb.on_subscribe_ok(s, alias);
            }
        }
        break;
    }
    case MOQT_REQUEST_OK: {
        uint64_t req = rd_vi(&r);
        if (req == s->ns_request_id && !s->ns_ok) {
            s->ns_ok = 1;
            fprintf(stderr, "[moqt %s] namespace accepted (REQUEST_OK)\n", s->label);
            if (ctx->cb.on_namespace_ok) {
                ctx->cb.on_namespace_ok(s);
            }
        }
        break;
    }
    case MOQT_REQUEST_ERROR: {
        uint64_t req = rd_vi(&r);
        uint64_t code = rd_vi(&r);
        (void)rd_vi(&r);
        uint64_t l = rd_vi(&r);
        const uint8_t* p = rd_bytes(&r, (size_t)l);
        char why[256] = "";
        if (p) {
            size_t c = l < sizeof(why) - 1 ? (size_t)l : sizeof(why) - 1;
            memcpy(why, p, c);
            why[c] = 0;
        }
        fprintf(stderr, "[moqt %s] REQUEST_ERROR request %llu code 0x%llx: %s\n", s->label,
            (unsigned long long)req, (unsigned long long)code, why);
        if (req == s->sub_request_id && s->sub_state == 1) {
            s->sub_state = 3;
        }
        if (ctx->cb.on_request_error) {
            ctx->cb.on_request_error(s, req, code, why);
        }
        break;
    }
    case MOQT_UNSUBSCRIBE: {
        uint64_t req = rd_vi(&r);
        moqt_pubsub_t* sub = find_sub(s, req);
        if (sub) {
            fprintf(stderr, "[moqt %s] UNSUBSCRIBE request %llu\n", s->label, (unsigned long long)req);
            if (ctx->cb.on_unsubscribe) {
                ctx->cb.on_unsubscribe(s, sub);
            }
            moqt_close_streams(s, sub);
            sub->used = 0;
        }
        break;
    }
    case MOQT_PUBLISH_DONE: {
        uint64_t req = rd_vi(&r);
        uint64_t status = rd_vi(&r);
        uint64_t count = rd_vi(&r);
        fprintf(stderr, "[moqt %s] PUBLISH_DONE request %llu status 0x%llx (%llu streams)\n", s->label,
            (unsigned long long)req, (unsigned long long)status, (unsigned long long)count);
        if (req == s->sub_request_id) {
            s->sub_state = 4;
            if (ctx->cb.on_publish_done) {
                ctx->cb.on_publish_done(s, status);
            }
        }
        break;
    }
    case MOQT_MAX_REQUEST_ID:
        s->peer_max_request_id = rd_vi(&r);
        break;
    case MOQT_GOAWAY:
        fprintf(stderr, "[moqt %s] GOAWAY received\n", s->label);
        break;
    case MOQT_REQUESTS_BLOCKED: {
        /* peer wants more request ids: give plenty */
        mbuf_t p = { 0 };
        mbuf_vi(&p, rd_vi(&r) + 1000);
        send_ctrl(s, MOQT_MAX_REQUEST_ID, &p);
        mbuf_free(&p);
        break;
    }
    case MOQT_FETCH:
    case MOQT_TRACK_STATUS:
    case MOQT_SUBSCRIBE_NAMESPACE:
    case MOQT_PUBLISH:
    case MOQT_PUBLISH_NAMESPACE:
    case MOQT_REQUEST_UPDATE: {
        uint64_t req = rd_vi(&r);
        fprintf(stderr, "[moqt %s] request type 0x%llx not supported, answering REQUEST_ERROR\n", s->label,
            (unsigned long long)type);
        send_request_error(s, req, 0x3, "not supported by moqpico");
        break;
    }
    case MOQT_NAMESPACE:
    case MOQT_NAMESPACE_DONE:
    case MOQT_PUBLISH_NAMESPACE_DONE:
    case MOQT_PUBLISH_NAMESPACE_CANCEL:
    case MOQT_FETCH_CANCEL:
    case MOQT_PUBLISH_OK:
    case MOQT_FETCH_OK:
        break;
    default:
        fprintf(stderr, "[moqt %s] unknown control message 0x%llx\n", s->label, (unsigned long long)type);
        moqt_close(s, MOQT_ERR_PROTOCOL_VIOLATION);
        break;
    }
}

static void parse_ctrl(moqt_session_t* s)
{
    while (!s->closed) {
        mrd_t r = { s->ctrl_in.b, s->ctrl_in.b + s->ctrl_in.n, 0 };
        uint64_t type = rd_vi(&r);
        uint16_t len = rd_u16(&r);
        const uint8_t* pl = rd_bytes(&r, len);
        if (r.err) {
            return;                       /* wait for more bytes */
        }
        size_t used = (size_t)(r.p - s->ctrl_in.b);
        uint8_t* copy = (uint8_t*)malloc(len ? len : 1);
        memcpy(copy, pl, len);
        mbuf_consume(&s->ctrl_in, used);
        handle_ctrl_msg(s, type, copy, len);
        free(copy);
    }
}

/* ======================================================================== data plane: receive */
static void deliver(moqt_session_t* s, uint64_t alias, uint64_t group, uint64_t subgroup, uint64_t object,
    int dgram, const uint8_t* p, size_t n)
{
    if (dgram) {
        s->rx_dgram_obj++;
    } else {
        s->rx_stream_obj++;
    }
    s->rx_bytes += n;
    if (s->ctx->cb.on_object) {
        s->ctx->cb.on_object(s, alias, group, subgroup, object, dgram, p, n);
    }
}

static void handle_datagram(moqt_session_t* s, const uint8_t* b, size_t len)
{
    mrd_t r = { b, b + len, 0 };
    uint64_t t = rd_vi(&r);
    int valid = (t <= 0x0F) || (t >= 0x20 && t <= 0x2F && !(t & 0x02));
    if (!valid) {
        fprintf(stderr, "[moqt %s] bad datagram type 0x%llx\n", s->label, (unsigned long long)t);
        return;
    }
    uint64_t alias = rd_vi(&r);
    uint64_t group = rd_vi(&r);
    uint64_t object = (t & 0x04) ? 0 : rd_vi(&r);
    if (!(t & 0x08)) {
        (void)rd_u8(&r);
    }
    if (t & 0x01) {
        uint64_t el = rd_vi(&r);
        (void)rd_bytes(&r, (size_t)el);
    }
    if (r.err) {
        return;
    }
    if (t & 0x20) {
        (void)rd_vi(&r);                  /* object status, no payload */
        return;
    }
    deliver(s, alias, group, UINT64_MAX, object, 1, r.p, (size_t)(r.e - r.p));
}

/* parse what we can of an incoming SUBGROUP stream */
static void parse_rx_stream(moqt_session_t* s, moqt_rx_stream_t* rx)
{
    for (;;) {
        if (rx->bad) {
            rx->in.n = 0;
            return;
        }
        mrd_t r = { rx->in.b, rx->in.b + rx->in.n, 0 };
        if (!rx->header_done) {
            uint64_t t = rd_vi(&r);
            if (r.err) {
                return;
            }
            int ok = ((t >= 0x10 && t <= 0x1D) || (t >= 0x30 && t <= 0x3D)) && ((t & 0x06) != 0x06);
            if (!ok) {
                fprintf(stderr, "[moqt %s] stream %llu: unsupported stream type 0x%llx\n", s->label,
                    (unsigned long long)rx->stream_id, (unsigned long long)t);
                rx->bad = 1;
                continue;
            }
            uint64_t alias = rd_vi(&r);
            uint64_t group = rd_vi(&r);
            uint64_t mode = (t & 0x06) >> 1;
            uint64_t sg = (mode == 2) ? rd_vi(&r) : 0;
            uint8_t prio = (t & 0x20) ? 0 : rd_u8(&r);
            if (r.err) {
                return;
            }
            rx->type = t;
            rx->alias = alias;
            rx->group = group;
            rx->subgroup = sg;
            rx->priority = prio;
            rx->header_done = 1;
            rx->has_object = 0;
            mbuf_consume(&rx->in, (size_t)(r.p - rx->in.b));
            continue;
        }
        uint64_t delta = rd_vi(&r);
        if (rx->type & 0x01) {
            uint64_t el = rd_vi(&r);
            (void)rd_bytes(&r, (size_t)el);
        }
        uint64_t plen = rd_vi(&r);
        if (plen == 0) {
            (void)rd_vi(&r);              /* status */
        }
        const uint8_t* p = rd_bytes(&r, (size_t)plen);
        if (r.err) {
            return;                       /* incomplete object */
        }
        uint64_t obj = rx->has_object ? rx->last_object + delta + 1 : delta;
        if (!rx->has_object && ((rx->type & 0x06) >> 1) == 1) {
            rx->subgroup = obj;
        }
        rx->last_object = obj;
        rx->has_object = 1;
        if (plen > 0) {
            deliver(s, rx->alias, rx->group, rx->subgroup, obj, 0, p, (size_t)plen);
        }
        mbuf_consume(&rx->in, (size_t)(r.p - rx->in.b));
    }
}

/* ======================================================================== data plane: send */
static void dg_free_all(moqt_session_t* s)
{
    while (s->dg_head) {
        moqt_dg_t* d = s->dg_head;
        s->dg_head = d->next;
        free(d);
    }
    s->dg_tail = NULL;
    s->dg_queued = 0;
}

static int prepare_datagram(moqt_session_t* s, void* context, size_t length)
{
    uint64_t now = picoquic_current_time();
    while (s->dg_head) {
        moqt_dg_t* d = s->dg_head;
        int stale = s->ctx->dgram_max_age_us && now - d->t_queued > s->ctx->dgram_max_age_us;
        int too_big = d->len > length && length >= 1150;
        if (!stale && !too_big) {
            break;
        }
        if (stale) {
            s->n_dgram_stale++;
        } else {
            s->n_dgram_too_big++;
        }
        s->dg_head = d->next;
        s->dg_queued--;
        free(d);
    }
    if (s->dg_head == NULL) {
        s->dg_tail = NULL;
        picoquic_mark_datagram_ready(s->cnx, 0);
        return 0;
    }
    moqt_dg_t* d = s->dg_head;
    if (d->len > length) {
        return 0;                         /* does not fit in this packet, try in the next one */
    }
    uint8_t* buf = picoquic_provide_datagram_buffer(context, d->len);
    if (buf == NULL) {
        return 0;
    }
    memcpy(buf, d->data, d->len);
    s->dg_head = d->next;
    s->dg_queued--;
    if (s->dg_head == NULL) {
        s->dg_tail = NULL;
    }
    free(d);
    picoquic_mark_datagram_ready(s->cnx, s->dg_head != NULL);
    return 0;
}

static moqt_slot_t* get_slot(moqt_session_t* s, moqt_pubsub_t* sub, uint64_t group, uint64_t subgroup,
    uint8_t prio)
{
    moqt_slot_t* free_slot = NULL;
    for (int i = 0; i < MOQT_SLOTS; i++) {
        moqt_slot_t* sl = &sub->slot[i];
        if (sl->used && sl->subgroup == subgroup) {
            if (sl->group == group) {
                return sl;
            }
            picoquic_add_to_stream(s->cnx, sl->stream_id, NULL, 0, 1);   /* FIN: subgroup complete */
            sl->used = 0;
        }
        if (!sl->used && free_slot == NULL) {
            free_slot = sl;
        }
    }
    if (free_slot == NULL) {                                             /* close the oldest group */
        free_slot = &sub->slot[0];
        for (int i = 1; i < MOQT_SLOTS; i++) {
            if (sub->slot[i].group < free_slot->group) {
                free_slot = &sub->slot[i];
            }
        }
        picoquic_add_to_stream(s->cnx, free_slot->stream_id, NULL, 0, 1);
    }
    memset(free_slot, 0, sizeof(*free_slot));
    free_slot->used = 1;
    free_slot->group = group;
    free_slot->subgroup = subgroup;
    free_slot->stream_id = picoquic_get_next_local_stream_id(s->cnx, 1);
    /* SUBGROUP_HEADER type 0x14: subgroup id present, no extensions, priority present */
    mbuf_t h = { 0 };
    mbuf_vi(&h, 0x14);
    mbuf_vi(&h, sub->alias);
    mbuf_vi(&h, group);
    mbuf_vi(&h, subgroup);
    mbuf_u8(&h, prio);
    picoquic_add_to_stream(s->cnx, free_slot->stream_id, h.b, h.n, 0);
    mbuf_free(&h);
    sub->streams_opened++;
    if (s->ctx->stream_max_age_us > 0) {               /* watch it for the delivery timeout */
        moqt_live_t* lv = NULL;
        for (int i = 0; i < MOQT_LIVE_STREAMS; i++) {
            if (!s->live[i].used) {
                lv = &s->live[i];
                break;
            }
        }
        if (lv == NULL) {                              /* full: forget the oldest one */
            lv = &s->live[0];
            for (int i = 1; i < MOQT_LIVE_STREAMS; i++) {
                if (s->live[i].t_open < lv->t_open) {
                    lv = &s->live[i];
                }
            }
        }
        lv->used = 1;
        lv->stream_id = free_slot->stream_id;
        lv->t_open = picoquic_current_time();
    }
    return free_slot;
}

/* 1 when every byte written to the stream so far was sent and acknowledged */
static int stream_delivered(picoquic_cnx_t* cnx, uint64_t stream_id)
{
    picoquic_stream_head_t* st = picoquic_find_stream(cnx, stream_id);
    if (st == NULL) {
        return 1;                                      /* already finished and freed */
    }
    if (st->send_queue != NULL || st->reset_requested) {
        return st->reset_requested;                    /* still data waiting to be sent */
    }
    if (st->sent_offset == 0) {
        return 1;
    }
    /* picoquic_check_sack_list() returns 0 when the range still has a hole (not all acknowledged)
     * and -1 when the whole range [0, sent_offset - 1] was acknowledged */
    return picoquic_check_sack_list(&st->sack_list, 0, st->sent_offset - 1) != 0;
}

int moqt_expire_streams(moqt_ctx_t* ctx, uint64_t now)
{
    int n = 0;
    if (ctx == NULL || ctx->stream_max_age_us == 0) {
        return 0;
    }
    for (moqt_session_t* s = ctx->sessions; s; s = s->next) {
        if (s->closed || s->cnx == NULL) {
            continue;
        }
        for (int i = 0; i < MOQT_LIVE_STREAMS; i++) {
            moqt_live_t* lv = &s->live[i];
            if (!lv->used || now < lv->t_open + ctx->stream_max_age_us) {
                continue;
            }
            lv->used = 0;
            if (stream_delivered(s->cnx, lv->stream_id)) {
                continue;                              /* everything arrived: nothing to give up */
            }
            if (picoquic_reset_stream(s->cnx, lv->stream_id, MOQT_RESET_DELIVERY_TIMEOUT) == 0) {
                s->n_stream_reset++;
                n++;
                if (ctx->verbose) {
                    fprintf(stderr, "[moqt %s] stream %llu not delivered after %llu ms: reset\n", s->label,
                        (unsigned long long)lv->stream_id, (unsigned long long)(ctx->stream_max_age_us / 1000));
                }
            }
            /* a slot still pointing at this stream must not be written to again */
            for (int k = 0; k < MOQT_MAX_SUBS; k++) {
                for (int j = 0; j < MOQT_SLOTS; j++) {
                    moqt_slot_t* sl = &s->subs[k].slot[j];
                    if (sl->used && sl->stream_id == lv->stream_id) {
                        sl->used = 0;
                    }
                }
            }
        }
    }
    return n;
}

int moqt_send_object(moqt_session_t* s, moqt_pubsub_t* sub, uint64_t group, uint64_t subgroup,
    uint64_t object, uint8_t prio, int as_datagram, const uint8_t* payload, size_t len)
{
    if (s == NULL || s->cnx == NULL || s->closed || sub == NULL || !sub->used) {
        return -1;
    }
    if (as_datagram) {
        mbuf_t h = { 0 };
        mbuf_vi(&h, 0x00);                /* object id present, priority present, no extensions */
        mbuf_vi(&h, sub->alias);
        mbuf_vi(&h, group);
        mbuf_vi(&h, object);
        mbuf_u8(&h, prio);
        moqt_dg_t* d = (moqt_dg_t*)malloc(sizeof(moqt_dg_t) + h.n + len);
        if (d == NULL) {
            mbuf_free(&h);
            return -1;
        }
        d->next = NULL;
        d->t_queued = picoquic_current_time();
        d->len = h.n + len;
        memcpy(d->data, h.b, h.n);
        memcpy(d->data + h.n, payload, len);
        mbuf_free(&h);
        if (s->dg_tail) {
            s->dg_tail->next = d;
        } else {
            s->dg_head = d;
        }
        s->dg_tail = d;
        s->dg_queued++;
        s->n_dgram_obj++;
        s->b_dgram += len;
        picoquic_mark_datagram_ready(s->cnx, 1);
        return 0;
    }
    moqt_slot_t* sl = get_slot(s, sub, group, subgroup, prio);
    mbuf_t o = { 0 };
    uint64_t delta = sl->has_object ? object - sl->last_object - 1 : object;
    if (sl->has_object && object <= sl->last_object) {
        mbuf_free(&o);
        return -1;                        /* objects of a subgroup must increase */
    }
    mbuf_vi(&o, delta);
    mbuf_vi(&o, len);
    if (len == 0) {
        mbuf_vi(&o, 0);                   /* status normal */
    }
    mbuf_put(&o, payload, len);
    picoquic_add_to_stream(s->cnx, sl->stream_id, o.b, o.n, 0);
    mbuf_free(&o);
    sl->last_object = object;
    sl->has_object = 1;
    s->n_stream_obj++;
    s->b_stream += len;
    return 0;
}

void moqt_close_streams(moqt_session_t* s, moqt_pubsub_t* sub)
{
    for (int i = 0; i < MOQT_SLOTS; i++) {
        if (sub->slot[i].used) {
            if (s->cnx && !s->closed) {
                picoquic_add_to_stream(s->cnx, sub->slot[i].stream_id, NULL, 0, 1);
            }
            sub->slot[i].used = 0;
        }
    }
}

int moqt_publish_done(moqt_session_t* s, moqt_pubsub_t* sub, uint64_t status, const char* reason)
{
    if (sub == NULL || !sub->used || sub->done_sent) {
        return -1;
    }
    moqt_close_streams(s, sub);
    mbuf_t p = { 0 };
    mbuf_vi(&p, sub->request_id);
    mbuf_vi(&p, status);
    mbuf_vi(&p, sub->streams_opened);
    mbuf_bytes(&p, reason ? reason : "", reason ? strlen(reason) : 0);
    send_ctrl(s, MOQT_PUBLISH_DONE, &p);
    mbuf_free(&p);
    sub->done_sent = 1;
    return 0;
}

void moqt_note_published(moqt_ctx_t* ctx, uint64_t group, uint64_t object)
{
    if (!ctx->has_largest || group > ctx->largest_group ||
        (group == ctx->largest_group && object > ctx->largest_object)) {
        ctx->has_largest = 1;
        ctx->largest_group = group;
        ctx->largest_object = object;
    }
}

int moqt_count_active_subs(moqt_session_t* s)
{
    int n = 0;
    for (int i = 0; i < MOQT_MAX_SUBS; i++) {
        n += s->subs[i].used && !s->subs[i].done_sent;
    }
    return n;
}

/* ======================================================================== requests */
static uint64_t next_req(moqt_session_t* s)
{
    uint64_t r = s->next_request_id;
    s->next_request_id += 2;
    return r;
}

int moqt_publish_namespace(moqt_session_t* s, const moqt_ns_t* ns)
{
    if (!s->setup_done) {
        return -1;
    }
    s->ns_request_id = next_req(s);
    mbuf_t p = { 0 };
    mbuf_vi(&p, s->ns_request_id);
    put_ns(&p, ns);
    mbuf_vi(&p, 0);                       /* no parameters */
    send_ctrl(s, MOQT_PUBLISH_NAMESPACE, &p);
    mbuf_free(&p);
    return 0;
}

int moqt_subscribe(moqt_session_t* s, const moqt_ns_t* ns, const char* name, uint8_t priority)
{
    if (!s->setup_done) {
        return -1;
    }
    s->sub_request_id = next_req(s);
    s->sub_state = 1;
    kvp_out_t o = { 0 };
    kv_int(&o, MOQT_PARAM_FORWARD, 1);
    kv_int(&o, MOQT_PARAM_SUBSCRIBER_PRIORITY, priority);
    mbuf_t f = { 0 };
    mbuf_vi(&f, MOQT_FILTER_LARGEST);
    kv_bytes(&o, MOQT_PARAM_SUBSCRIPTION_FILTER, f.b, f.n);
    kv_int(&o, MOQT_PARAM_GROUP_ORDER, 1);
    mbuf_free(&f);
    mbuf_t p = { 0 };
    mbuf_vi(&p, s->sub_request_id);
    put_ns(&p, ns);
    mbuf_bytes(&p, name, strlen(name));
    mbuf_vi(&p, o.count);
    mbuf_put(&p, o.b.b, o.b.n);
    send_ctrl(s, MOQT_SUBSCRIBE, &p);
    mbuf_free(&p);
    mbuf_free(&o.b);
    return 0;
}

int moqt_unsubscribe(moqt_session_t* s)
{
    if (s->sub_state != 2) {
        return -1;
    }
    mbuf_t p = { 0 };
    mbuf_vi(&p, s->sub_request_id);
    send_ctrl(s, MOQT_UNSUBSCRIBE, &p);
    mbuf_free(&p);
    s->sub_state = 4;
    return 0;
}

/* ======================================================================== sessions */
static moqt_session_t* new_session(moqt_ctx_t* ctx, picoquic_cnx_t* cnx, int is_client, const char* label)
{
    moqt_session_t* s = (moqt_session_t*)calloc(1, sizeof(moqt_session_t));
    if (s == NULL) {
        return NULL;
    }
    s->ctx = ctx;
    s->cnx = cnx;
    s->is_client = is_client;
    s->next_request_id = is_client ? 0 : 1;
    snprintf(s->label, sizeof(s->label), "%s", label ? label : "?");
    s->next = ctx->sessions;
    ctx->sessions = s;
    return s;
}

void moqt_close(moqt_session_t* s, uint64_t code)
{
    if (s->cnx && !s->closed) {
        picoquic_close(s->cnx, code);
    }
}

static void on_ready(moqt_session_t* s)
{
    if (s->ready) {
        return;
    }
    s->ready = 1;
    if (s->is_client) {
        s->ctrl_stream = picoquic_get_next_local_stream_id(s->cnx, 0);
        s->has_ctrl = 1;
        send_setup(s, s->peer_path);
    }
}

moqt_session_t* moqt_connect(moqt_ctx_t* ctx, const struct sockaddr* addr, const char* sni,
    const char* label, uint64_t now)
{
    picoquic_cnx_t* cnx = picoquic_create_cnx(ctx->quic, picoquic_null_connection_id,
        picoquic_null_connection_id, addr, now, 0, sni, MOQT_ALPN, 1);
    if (cnx == NULL) {
        return NULL;
    }
    moqt_session_t* s = new_session(ctx, cnx, 1, label);
    /* AUTHORITY = host:port of the server (raw QUIC moqt:// URI) */
    if (addr->sa_family == AF_INET) {
        const struct sockaddr_in* a = (const struct sockaddr_in*)addr;
        char ip[64];
        inet_ntop(AF_INET, &a->sin_addr, ip, sizeof(ip));
        snprintf(s->peer_path, sizeof(s->peer_path), "%s:%u", ip, ntohs(a->sin_port));
    }
    picoquic_set_callback(cnx, moqt_picoquic_callback, s);
    picoquic_enable_keep_alive(cnx, 1000000);
    if (picoquic_start_client_cnx(cnx) != 0) {
        picoquic_delete_cnx(cnx);
        s->cnx = NULL;
        s->closed = 1;
        return NULL;
    }
    return s;
}

void moqt_configure_quic(picoquic_quic_t* quic)
{
    picoquic_tp_t tp = *picoquic_get_default_tp(quic);
    tp.max_datagram_frame_size = 1400;
    /* picoquic's defaults allow only 64 KB per unidirectional stream before the receiver must send
     * MAX_STREAM_DATA. A video I frame (one subgroup stream per GOP) is often 100-200 KB, so with
     * the default it waits several round trips for flow-control credit. Give each stream and the
     * connection plenty of credit, and room for many streams (one per group). */
    tp.initial_max_stream_data_uni = 16 * 1024 * 1024;
    tp.initial_max_stream_data_bidi_local = 1024 * 1024;
    tp.initial_max_stream_data_bidi_remote = 1024 * 1024;
    tp.initial_max_data = 256 * 1024 * 1024;
    if (tp.initial_max_stream_id_unidir < 4096) {
        tp.initial_max_stream_id_unidir = 4096;
    }
    picoquic_set_default_tp(quic, &tp);
}

static void label_from_local_addr(moqt_session_t* s)
{
    struct sockaddr* la = NULL;
    picoquic_get_local_addr(s->cnx, &la);
    if (la && la->sa_family == AF_INET) {
        inet_ntop(AF_INET, &((struct sockaddr_in*)la)->sin_addr, s->label, sizeof(s->label));
    }
}

int moqt_picoquic_callback(picoquic_cnx_t* cnx, uint64_t stream_id, uint8_t* bytes, size_t length,
    picoquic_call_back_event_t event, void* callback_ctx, void* v_stream_ctx)
{
    moqt_session_t* s;
    if (callback_ctx == picoquic_get_default_callback_context(picoquic_get_quic_ctx(cnx))) {
        /* first event of a connection accepted by the server */
        moqt_ctx_t* ctx = (moqt_ctx_t*)callback_ctx;
        s = new_session(ctx, cnx, 0, "srv");
        if (s == NULL) {
            picoquic_close(cnx, MOQT_ERR_INTERNAL);
            return -1;
        }
        picoquic_set_callback(cnx, moqt_picoquic_callback, s);
        label_from_local_addr(s);
        picoquic_enable_keep_alive(cnx, 1000000);
    } else {
        s = (moqt_session_t*)callback_ctx;
    }
    if (s == NULL) {
        return 0;
    }

    switch (event) {
    case picoquic_callback_almost_ready:
    case picoquic_callback_ready:
        if (!s->is_client && strcmp(s->label, "srv") == 0) {
            label_from_local_addr(s);
        }
        on_ready(s);
        break;
    case picoquic_callback_stream_data:
    case picoquic_callback_stream_fin:
        if ((stream_id & 0x02) == 0) {
            /* bidirectional: the control stream (client initiated, first one) */
            if (!s->has_ctrl && !s->is_client && (stream_id & 0x01) == 0) {
                s->ctrl_stream = stream_id;
                s->has_ctrl = 1;
            }
            if (s->has_ctrl && stream_id == s->ctrl_stream) {
                mbuf_put(&s->ctrl_in, bytes, length);
                parse_ctrl(s);
                if (event == picoquic_callback_stream_fin) {
                    fprintf(stderr, "[moqt %s] control stream closed by peer\n", s->label);
                    moqt_close(s, MOQT_ERR_PROTOCOL_VIOLATION);
                }
            } else {
                picoquic_reset_stream(cnx, stream_id, MOQT_ERR_PROTOCOL_VIOLATION);
            }
        } else {
            /* unidirectional data stream */
            moqt_rx_stream_t* rx = (moqt_rx_stream_t*)v_stream_ctx;
            if (rx == NULL) {
                rx = (moqt_rx_stream_t*)calloc(1, sizeof(moqt_rx_stream_t));
                if (rx == NULL) {
                    return -1;
                }
                rx->stream_id = stream_id;
                picoquic_set_app_stream_ctx(cnx, stream_id, rx);
            }
            mbuf_put(&rx->in, bytes, length);
            parse_rx_stream(s, rx);
            if (event == picoquic_callback_stream_fin) {
                picoquic_unlink_app_stream_ctx(cnx, stream_id);
                mbuf_free(&rx->in);
                free(rx);
            }
        }
        break;
    case picoquic_callback_stream_reset:
        if (v_stream_ctx != NULL && (stream_id & 0x02)) {
            moqt_rx_stream_t* rx = (moqt_rx_stream_t*)v_stream_ctx;
            picoquic_unlink_app_stream_ctx(cnx, stream_id);
            mbuf_free(&rx->in);
            free(rx);
        }
        break;
    case picoquic_callback_stop_sending:
        picoquic_reset_stream(cnx, stream_id, 0);
        break;
    case picoquic_callback_datagram:
        handle_datagram(s, bytes, length);
        break;
    case picoquic_callback_prepare_datagram:
        return prepare_datagram(s, bytes, length);
    case picoquic_callback_close:
    case picoquic_callback_application_close:
    case picoquic_callback_stateless_reset:
        if (!s->closed) {
            s->closed = 1;
            fprintf(stderr, "[moqt %s] connection closed (error 0x%llx / app 0x%llx)\n", s->label,
                (unsigned long long)picoquic_get_remote_error(cnx),
                (unsigned long long)picoquic_get_application_error(cnx));
            if (s->ctx->cb.on_close) {
                s->ctx->cb.on_close(s);
            }
            dg_free_all(s);
            mbuf_free(&s->ctrl_in);
            picoquic_set_callback(cnx, NULL, NULL);
            s->cnx = NULL;
        }
        break;
    default:
        break;
    }
    return 0;
}
