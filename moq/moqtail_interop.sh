#!/bin/bash
# Check that our MoQ layer (moqpico, C on picoquic) talks to the real MoQtail (Rust) - optional.
#
#   bash moq/moqtail_interop.sh
#
# Builds the MoQtail relay + test client, release relay@0.14.1 (the last one with MOQT draft-16,
# ALPN "moqt-16", raw QUIC), into ~/moqtail-0.14.1, then runs on this machine (no Mininet):
#   1. MoQtail client publishes   -> MoQtail relay -> moqpico subscribes
#   2. moqpico publishes          -> MoQtail relay -> MoQtail client subscribes
#   3. moqpico publishes          -> MoQtail relay -> moqpico subscribes (streams + datagrams)
# Needs internet (Rust + crates) and about 2 GB of disk; the build takes 5-15 min the first time.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
M="$HERE/moqpico"
DIR="$HOME/moqtail-0.14.1"
LOG="/tmp/moq_interop"
PORT=4433
mkdir -p "$LOG"

if [ ! -x "$M" ]; then
  echo "*** building moqpico"
  make -C "$HERE" PICOQUIC="${PICOQUIC:-$HOME/picoquic}"
fi
if ! command -v cargo >/dev/null; then
  [ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
fi
if ! command -v cargo >/dev/null; then
  echo "*** installing Rust (rustup, for this user only)"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
  . "$HOME/.cargo/env"
fi
if [ ! -d "$DIR" ]; then
  echo "*** downloading MoQtail relay@0.14.1"
  git clone -q https://github.com/moqtail/moqtail.git "$DIR"
  (cd "$DIR" && git checkout -q relay@0.14.1 && git apply "$HERE/moqtail-0.14.1-ipv4.patch")
fi
echo "*** building the MoQtail relay and client (first time 5-15 min)"
(cd "$DIR" && cargo build --release -q -p relay -p client)
R="$DIR/target/release/relay"
C="$DIR/target/release/client"

# self-signed certificate for the relay
if [ ! -f "$LOG/cert.pem" ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout "$LOG/key.pem" \
    -out "$LOG/cert.pem" -days 365 -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null
fi
pkill -x relay 2>/dev/null || true
"$R" --port $PORT --host 127.0.0.1 --cert-file "$LOG/cert.pem" --key-file "$LOG/key.pem" > "$LOG/relay.log" 2>&1 &
RELAY=$!
trap 'kill $RELAY 2>/dev/null' EXIT
sleep 2

pass=0
echo
echo "=== 1. MoQtail client publishes -> MoQtail relay -> moqpico subscribes"
"$C" -c publish-namespace -s moqt://127.0.0.1:$PORT --no-cert-validation -n moqtail -T demo \
  --group-count 5 --objects-per-group 4 --interval 200 --payload-size 500 > "$LOG/1_moqtail_pub.log" 2>&1 &
sleep 1.5
timeout 15 "$M" sub --connect relay=127.0.0.1:$PORT --ns moqtail --track demo --raw --log "$LOG/1_objects.csv" \
  --duration 8 2>&1 | grep -E "set up|SUBSCRIBE_OK|END" || true
n=$(($(wc -l < "$LOG/1_objects.csv") - 1))
echo "    moqpico received $n of 20 objects"; [ "$n" -eq 20 ] && pass=$((pass + 1))

echo
echo "=== 2. moqpico publishes -> MoQtail relay -> MoQtail client subscribes"
"$M" pub --relay 127.0.0.1:$PORT --ns moqpico --track t2 --synthetic 6,10,900,30 --start-delay 0.5 \
  > "$LOG/2_moqpico_pub.log" 2>&1 &
sleep 1
timeout 10 "$C" -c subscribe -s moqt://127.0.0.1:$PORT --no-cert-validation -n moqpico -T t2 --duration 5 \
  > "$LOG/2_moqtail_sub.log" 2>&1 || true
sed 's/\x1b\[[0-9;]*m//g' "$LOG/2_moqtail_sub.log" | grep -E "Subscribed|Reception stats|Subscriber finished" | sed 's/^.*INFO /    /'
if sed 's/\x1b\[[0-9;]*m//g' "$LOG/2_moqtail_sub.log" | grep -q "errors=0, gaps=0"; then pass=$((pass + 1)); fi
echo "    (the MoQtail test client reads only the subgroup streams: the 6 I objects; the 54 datagrams are"
echo "     checked in test 3)"
sleep 3

echo
echo "=== 3. moqpico publishes -> MoQtail relay -> moqpico subscribes"
"$M" pub --relay 127.0.0.1:$PORT --ns moqpico --track t3 --synthetic 6,10,900,30 --start-delay 0.5 \
  > "$LOG/3_moqpico_pub.log" 2>&1 &
sleep 1
timeout 15 "$M" sub --connect relay=127.0.0.1:$PORT --ns moqpico --track t3 --log "$LOG/3_objects.csv" \
  --out "$LOG/3.bin" 2>&1 | grep -E "set up|SUBSCRIBE_OK|PUBLISH_DONE|END" || true
s=$(grep -c ",stream," "$LOG/3_objects.csv" || true)
d=$(grep -c ",dgram," "$LOG/3_objects.csv" || true)
echo "    moqpico received $s stream objects (of 6) and $d datagram objects (of 54)"
[ "$s" -eq 6 ] && [ "$d" -ge 50 ] && pass=$((pass + 1))

echo
echo "*** $pass of 3 interop tests passed (logs in $LOG)"
