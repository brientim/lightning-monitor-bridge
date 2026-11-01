#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Installs Lightning Monitor Bridge to /usr/local/bin and starts it at boot (systemd).
#   sudo ./install.sh                 LD-350 on USB
#   sudo ./install.sh --ld250 /dev/ttyUSB0
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo ./install.sh" >&2
    exit 1
fi
here="$(cd "$(dirname "$0")" && pwd)"
user="${SUDO_USER:-root}"

install -m 0755 "$here/lightning-bridge" /usr/local/bin/lightning-bridge
install -m 0644 "$here/99-boltek-ld350.rules" /etc/udev/rules.d/99-boltek-ld350.rules
udevadm control --reload-rules || true
udevadm trigger || true

if [ "${1:-}" = "--ld250" ] && [ -n "${2:-}" ]; then
    sudo -u "$user" /usr/local/bin/lightning-bridge config set detector ld250
    sudo -u "$user" /usr/local/bin/lightning-bridge config set serial_port "$2"
fi
/usr/local/bin/lightning-bridge install-service
echo
echo "Installed. Settings: $(sudo -u "$user" /usr/local/bin/lightning-bridge config path)"
echo "Status:   systemctl status lightning-monitor-bridge"
echo "Log:      journalctl -u lightning-monitor-bridge -f"
echo "Token:    sudo -u $user lightning-bridge token --new   (then: sudo systemctl restart lightning-monitor-bridge)"
