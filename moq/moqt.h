/*
 * moqt.h - a small MoQ Transport (MOQT, draft-ietf-moq-transport-16) layer on top of picoquic.
 *
 * Wire compatible with MoQtail (relay/client 0.14.x, ALPN "moqt-16", raw QUIC).
 *
 *   - session set-up:   CLIENT_SETUP / SERVER_SETUP on the control stream (first bidi stream)
 *   - publishing:       PUBLISH_NAMESPACE -> REQUEST_OK, SUBSCRIBE -> SUBSCRIBE_OK, PUBLISH_DONE
 *   - subscribing:      SUBSCRIBE -> SUBSCRIBE_OK / REQUEST_ERROR, UNSUBSCRIBE
 *   - objects:          SUBGROUP_HEADER unidirectional streams (reliable) and
 *                       OBJECT_DATAGRAM (unreliable, never resent)
 *
 * One moqt_session_t = one QUIC connection = one MOQT session. An application can hold several
 * sessions at the same time (for example one per network path).
 */
#ifndef MOQT_H
#define MOQT_H

#include <stdint.h>
#include <stddef.h>
#include <picoquic.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MOQT_ALPN "moqt-16"

/* control message types (draft-16) */
#define MOQT_CLIENT_SETUP       0x20
#define MOQT_SERVER_SETUP       0x21
#define MOQT_GOAWAY             0x10
#define MOQT_MAX_REQUEST_ID     0x15
#define MOQT_REQUESTS_BLOCKED   0x1A
#define MOQT_REQUEST_OK         0x07
#define MOQT_REQUEST_ERROR      0x05
#define MOQT_SUBSCRIBE          0x03
#define MOQT_SUBSCRIBE_OK       0x04
#define MOQT_REQUEST_UPDATE     0x02
#define MOQT_UNSUBSCRIBE        0x0A
#define MOQT_PUBLISH            0x1D
#define MOQT_PUBLISH_OK         0x1E
#define MOQT_PUBLISH_DONE       0x0B
#define MOQT_FETCH              0x16
#define MOQT_FETCH_OK           0x18
#define MOQT_FETCH_CANCEL       0x17
#define MOQT_TRACK_STATUS       0x0D
#define MOQT_PUBLISH_NAMESPACE  0x06
#define MOQT_NAMESPACE          0x08
#define MOQT_PUBLISH_NAMESPACE_DONE   0x09
#define MOQT_NAMESPACE_DONE     0x0E
#define MOQT_PUBLISH_NAMESPACE_CANCEL 0x0C
#define MOQT_SUBSCRIBE_NAMESPACE 0x11

/* setup parameters */
#define MOQT_SETUP_PATH                 0x01
#define MOQT_SETUP_MAX_REQUEST_ID       0x02
#define MOQT_SETUP_AUTHORITY            0x05
#define MOQT_SETUP_IMPLEMENTATION       0x07

/* message parameters */
#define MOQT_PARAM_LARGEST_OBJECT       0x09
#define MOQT_PARAM_FORWARD              0x10
#define MOQT_PARAM_SUBSCRIBER_PRIORITY  0x20
#define MOQT_PARAM_SUBSCRIPTION_FILTER  0x21
#define MOQT_PARAM_GROUP_ORDER          0x22

/* subscription filter types */
#define MOQT_FILTER_NEXT_GROUP   0x1
#define MOQT_FILTER_LARGEST      0x2
#define MOQT_FILTER_ABS_START    0x3
#define MOQT_FILTER_ABS_RANGE    0x4

/* errors */
#define MOQT_ERR_NO_ERROR            0x0
#define MOQT_ERR_INTERNAL            0x1
#define MOQT_ERR_PROTOCOL_VIOLATION  0x3
/* RESET_STREAM code for a subgroup stream that was given up (draft-16: DELIVERY_TIMEOUT) */
#define MOQT_RESET_DELIVERY_TIMEOUT  0x2
#define MOQT_LIVE_STREAMS 64     /* subgroup streams watched for the delivery timeout */
#define MOQT_REQERR_DOES_NOT_EXIST   0x10
#define MOQT_DONE_TRACK_ENDED        0x2

#define MOQT_MAX_NS      8      /* namespace fields we keep */
#define MOQT_MAX_NAME    64
#define MOQT_MAX_SUBS    8      /* subscriptions per session */
#define MOQT_SLOTS       4      /* open subgroup streams per subscription */

/* ---------------------------------------------------------------- buffers / varints */
typedef struct {
    uint8_t* b;
    size_t n, cap;
} mbuf_t;

void mbuf_free(mbuf_t* m);
void mbuf_put(mbuf_t* m, const void* p, size_t n);
void mbuf_u8(mbuf_t* m, uint8_t v);
void mbuf_u16(mbuf_t* m, uint16_t v);
void mbuf_vi(mbuf_t* m, uint64_t v);
void mbuf_bytes(mbuf_t* m, const void* p, size_t n);      /* varint length + bytes */
void mbuf_consume(mbuf_t* m, size_t n);
size_t moqt_vi_len(uint64_t v);
uint8_t* moqt_vi_write(uint8_t* p, uint64_t v);

typedef struct {
    const uint8_t* p;
    const uint8_t* e;
    int err;                    /* set when the input is too short */
} mrd_t;

uint64_t rd_vi(mrd_t* r);
uint8_t rd_u8(mrd_t* r);
uint16_t rd_u16(mrd_t* r);
const uint8_t* rd_bytes(mrd_t* r, size_t n);

/* ---------------------------------------------------------------- names */
typedef struct {
    int n;
    char f[MOQT_MAX_NS][MOQT_MAX_NAME];
} moqt_ns_t;

void moqt_ns_parse(moqt_ns_t* ns, const char* s);           /* "a/b/c" -> ("a","b","c") */
int moqt_ns_equal(const moqt_ns_t* a, const moqt_ns_t* b);
void moqt_ns_str(const moqt_ns_t* ns, char* out, size_t len);

/* ---------------------------------------------------------------- sessions */
struct moqt_session;
struct moqt_ctx;

/* an open SUBGROUP stream (publisher side) */
typedef struct {
    int used;
    uint64_t stream_id;
    uint64_t group, subgroup, last_object;
    int has_object;
} moqt_slot_t;

/* a subscription the peer made to us (we publish) */
typedef struct {
    int used;
    uint64_t request_id;
    uint64_t alias;
    moqt_ns_t ns;
    char name[MOQT_MAX_NAME];
    uint8_t priority;           /* subscriber priority */
    moqt_slot_t slot[MOQT_SLOTS];
    uint64_t streams_opened;
    int done_sent;
} moqt_pubsub_t;

/* a queued datagram (sent when picoquic asks for one; stale ones are dropped) */
typedef struct moqt_dg {
    struct moqt_dg* next;
    uint64_t t_queued;
    size_t len;
    uint8_t data[];
} moqt_dg_t;

/* a subgroup stream we opened, watched until it is delivered or too old */
typedef struct {
    int used;
    uint64_t stream_id;
    uint64_t t_open;
} moqt_live_t;

/* incoming unidirectional data stream */
typedef struct {
    uint64_t stream_id;
    mbuf_t in;
    int header_done;
    uint64_t type, alias, group, subgroup;
    uint8_t priority;
    int has_object;
    uint64_t last_object;
    int bad;
} moqt_rx_stream_t;

typedef struct moqt_session {
    struct moqt_ctx* ctx;
    picoquic_cnx_t* cnx;
    struct moqt_session* next;
    char label[32];
    int is_client;
    int ready;                  /* QUIC connection ready */
    int setup_done;             /* SETUP exchanged */
    int closed;
    uint64_t ctrl_stream;
    int has_ctrl;
    mbuf_t ctrl_in;
    uint64_t next_request_id;   /* client: 0,2,4..  server: 1,3,5.. */
    uint64_t peer_max_request_id;
    char peer_impl[64];
    char peer_path[128];
    /* publisher side */
    moqt_pubsub_t subs[MOQT_MAX_SUBS];
    uint64_t ns_request_id;
    int ns_ok;
    /* subscriber side */
    uint64_t sub_request_id;
    uint64_t sub_alias;
    int sub_state;              /* 0 none, 1 sent, 2 ok, 3 error, 4 done */
    /* datagrams waiting to be sent */
    moqt_dg_t *dg_head, *dg_tail;
    size_t dg_queued;
    /* subgroup streams that may still be in flight (delivery timeout) */
    moqt_live_t live[MOQT_LIVE_STREAMS];
    /* statistics */
    uint64_t n_stream_obj, n_dgram_obj, n_dgram_stale, n_dgram_too_big;
    uint64_t n_stream_reset;    /* subgroup streams given up (older than stream_max_age_us) */
    uint64_t cc_low_since;      /* publisher watchdog: since when the I-frame path has a small window but no queue */
    uint64_t cc_last_restart;
    uint64_t cc_lost0, cc_sent0;  /* packet counters when cc_low_since was set */
    uint64_t n_cc_restart;      /* congestion controller restarted by the watchdog */
    uint64_t b_stream, b_dgram;
    uint64_t rx_stream_obj, rx_dgram_obj, rx_bytes;
    void* app;                  /* free for the application */
} moqt_session_t;

/* callbacks into the application (all optional) */
typedef struct {
    void (*on_setup)(moqt_session_t* s);                                   /* SETUP done */
    /* peer subscribed to one of our tracks; return 0 to accept */
    int (*on_subscribe)(moqt_session_t* s, moqt_pubsub_t* sub);
    void (*on_unsubscribe)(moqt_session_t* s, moqt_pubsub_t* sub);
    void (*on_subscribe_ok)(moqt_session_t* s, uint64_t alias);
    void (*on_request_error)(moqt_session_t* s, uint64_t request_id, uint64_t code, const char* reason);
    void (*on_namespace_ok)(moqt_session_t* s);
    void (*on_publish_done)(moqt_session_t* s, uint64_t status);
    /* an object arrived (subscriber side). subgroup is UINT64_MAX for datagrams */
    void (*on_object)(moqt_session_t* s, uint64_t alias, uint64_t group, uint64_t subgroup,
        uint64_t object, int is_datagram, const uint8_t* payload, size_t len);
    void (*on_close)(moqt_session_t* s);
} moqt_callbacks_t;

typedef struct moqt_ctx {
    picoquic_quic_t* quic;
    moqt_callbacks_t cb;
    moqt_session_t* sessions;
    uint64_t next_alias;
    uint64_t dgram_max_age_us;  /* drop queued datagrams older than this (0 = never) */
    uint64_t stream_max_age_us; /* reset subgroup streams not delivered after this (0 = never) */
    int verbose;
    /* largest location published on our track (for SUBSCRIBE_OK LARGEST_OBJECT) */
    int has_largest;
    uint64_t largest_group, largest_object;
    void* app;
} moqt_ctx_t;

/* picoquic stream callback - pass to picoquic_create() as the default callback with the
 * moqt_ctx_t as default context (server), or let moqt_connect() install it (client). */
int moqt_picoquic_callback(picoquic_cnx_t* cnx, uint64_t stream_id, uint8_t* bytes, size_t length,
    picoquic_call_back_event_t event, void* callback_ctx, void* v_stream_ctx);

/* set the transport parameters MOQT needs (datagrams) on a QUIC context */
void moqt_configure_quic(picoquic_quic_t* quic);

/* open a client session to addr (sends CLIENT_SETUP when the connection is ready) */
moqt_session_t* moqt_connect(moqt_ctx_t* ctx, const struct sockaddr* addr, const char* sni,
    const char* label, uint64_t now);

/* client requests */
int moqt_publish_namespace(moqt_session_t* s, const moqt_ns_t* ns);
int moqt_subscribe(moqt_session_t* s, const moqt_ns_t* ns, const char* name, uint8_t priority);
int moqt_unsubscribe(moqt_session_t* s);

/* publisher: send one object of a subscription.
 *   as_datagram = 1  -> OBJECT_DATAGRAM (payload must fit in one QUIC datagram)
 *   as_datagram = 0  -> object on the SUBGROUP stream (group, subgroup); a new stream is opened
 *                       when the group changes, the previous one of that subgroup is closed (FIN)
 * returns 0 on success */
int moqt_send_object(moqt_session_t* s, moqt_pubsub_t* sub, uint64_t group, uint64_t subgroup,
    uint64_t object, uint8_t pub_priority, int as_datagram, const uint8_t* payload, size_t len);
/* delivery timeout: reset (RESET_STREAM, DELIVERY_TIMEOUT) every subgroup stream opened more than
 * ctx->stream_max_age_us ago that is not fully delivered yet, so a late group does not hold up
 * the newer ones. Call it often (publisher loop). Returns the number of streams reset. */
int moqt_expire_streams(moqt_ctx_t* ctx, uint64_t now);
/* close (FIN) every open subgroup stream of a subscription */
void moqt_close_streams(moqt_session_t* s, moqt_pubsub_t* sub);
/* end of a subscription: close streams + PUBLISH_DONE */
int moqt_publish_done(moqt_session_t* s, moqt_pubsub_t* sub, uint64_t status, const char* reason);
/* record the largest location published (used in SUBSCRIBE_OK) */
void moqt_note_published(moqt_ctx_t* ctx, uint64_t group, uint64_t object);

/* close the QUIC connection of a session */
void moqt_close(moqt_session_t* s, uint64_t code);
int moqt_count_active_subs(moqt_session_t* s);

#ifdef __cplusplus
}
#endif
#endif
