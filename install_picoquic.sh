#!/bin/bash
# Build picoquic (QUIC over UDP) from https://github.com/private-octopus/picoquic
# into ~/picoquic and run a quick self-test.
#   bash install_picoquic.sh
set -e
cd ~
echo "*** Installing build tools"
sudo apt-get update -qq
sudo apt-get install -y build-essential cmake libssl-dev pkg-config git

if [ ! -d picoquic ]; then
  echo "*** Downloading picoquic"
  git clone --depth 1 https://github.com/private-octopus/picoquic.git
fi
cd picoquic

# The demo server normally listens on IPv4 and IPv6. Our networks only use
# IPv4, so listen on IPv4 only (avoids a start-up error where IPv6 is off).
sed -i 's/param.local_af = 0;/param.local_af = AF_INET;/' picoquicfirst/picoquicdemo.c

# Let the demo client write the arrival time of every live-video frame
# (quicperf report) to the file named in QUICPERF_REPORT (used by quic_live.py).
grep -q QUICPERF_REPORT picoquicfirst/picoquicdemo.c || sed -i \
  '/quicperf_ctx = quicperf_create_ctx(client_scenario_text, NULL);/a\            if (quicperf_ctx != NULL \&\& getenv("QUICPERF_REPORT") != NULL) { quicperf_ctx->report_file = fopen(getenv("QUICPERF_REPORT"), "w"); }' \
  picoquicfirst/picoquicdemo.c

echo "*** Building picoquic (picotls is downloaded automatically, takes a few minutes)"
cmake -DPICOQUIC_FETCH_PTLS=Y -DCMAKE_BUILD_TYPE=Release . > /dev/null
make -j"$(nproc)" picoquicdemo

echo "*** Self-test: send a 5 MB file over QUIC on this machine"
rm -rf /tmp/pq_www /tmp/pq_dl && mkdir -p /tmp/pq_www /tmp/pq_dl
head -c 5000000 /dev/urandom > /tmp/pq_www/test.bin
./picoquicdemo -p 4433 -c certs/cert.pem -k certs/key.pem -w /tmp/pq_www -1 > /tmp/pq_srv.log 2>&1 &
sleep 1
./picoquicdemo -o /tmp/pq_dl -n test 127.0.0.1 4433 '/test.bin' > /tmp/pq_cli.log 2>&1 || true
grep "Received .* bytes" /tmp/pq_cli.log || true
if cmp -s /tmp/pq_www/test.bin /tmp/pq_dl/test.bin; then
  echo "*** picoquic works: file received over QUIC is identical"
else
  echo "*** Self-test FAILED - see /tmp/pq_srv.log and /tmp/pq_cli.log"
  exit 1
fi
