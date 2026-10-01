#!/usr/bin/env bash
# Installs Mininet-WiFi and the tools used by this experiment on Ubuntu (20.04 / 22.04 / 24.04).
# Usage:  bash install.sh
set -euo pipefail

echo "[1/3] Installing system packages..."
sudo apt-get update
sudo apt-get install -y git python3 python3-pip iperf3 iproute2 net-tools ethtool mptcpd || \
sudo apt-get install -y git python3 python3-pip iperf3 iproute2 net-tools ethtool

echo "[2/3] Cloning and installing Mininet-WiFi..."
cd "$HOME"
if [ ! -d mininet-wifi ]; then
  git clone https://github.com/intrig-unicamp/mininet-wifi
fi
cd mininet-wifi
# -W wireless deps, -l wmediumd, -n mininet-wifi, -f openflow, -v open vswitch
sudo util/install.sh -Wlnfv

echo "[3/3] Sanity check..."
sudo mn -c >/dev/null 2>&1 || true
python3 -c "import mn_wifi, mininet; print('mininet-wifi import OK')" || \
sudo python3 -c "import mn_wifi, mininet; print('mininet-wifi import OK (as root)')"
iperf3 --version | head -n1

echo
echo "Done. Quick test:  sudo mn --wifi --test pingall"
