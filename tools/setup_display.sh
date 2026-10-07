#!/usr/bin/env bash
# Show BMO's face on the panel with the upstream Tk GUI (python -m app --gui).
#
# Pi OS Lite has no desktop, so this installs a bare X server (no desktop or
# window manager) and adds a systemd drop-in that starts bmo-agent inside it.
# Run as the normal user; sudo is used for apt, the X wrapper and the drop-in.
#
#   tools/setup_display.sh            install and switch the service to the GUI
#   tools/setup_display.sh --remove   go back to headless (packages are kept)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/venv/bin/python"
DROPIN_DIR=/etc/systemd/system/bmo-agent.service.d
DROPIN="$DROPIN_DIR/display.conf"

if [[ ${1:-} == --remove ]]; then
    sudo rm -f "$DROPIN"
    sudo systemctl daemon-reload
    sudo systemctl restart bmo-agent
    echo "bmo-agent is headless again."
    exit 0
fi

[[ $EUID -ne 0 ]] || { echo "Run as your normal user, not root." >&2; exit 1; }
[[ -x $PY ]] || { echo "Missing $PY; run ./setup.sh first." >&2; exit 1; }

# Bare X: server core (includes the KMS modesetting driver), input, xinit, xset.
sudo apt-get install -y --no-install-recommends \
    xserver-xorg-core xserver-xorg-legacy xserver-xorg-input-libinput xinit x11-xserver-utils

# The service is not a console login, so let the X wrapper start X for it.
sudo tee /etc/X11/Xwrapper.config >/dev/null <<'EOF'
allowed_users=anybody
needs_root_rights=yes
EOF

# X on VT7 with no cursor and no screen blanking; Tk is its only client.
sudo install -d -m 755 "$DROPIN_DIR"
sudo tee "$DROPIN" >/dev/null <<EOF
# Installed by tools/setup_display.sh; remove with: tools/setup_display.sh --remove
[Service]
ExecStart=
ExecStart=/usr/bin/xinit $PY -m app --gui -- :0 vt7 -nolisten tcp -nocursor -s 0 -dpms
EOF
sudo systemctl daemon-reload
sudo systemctl restart bmo-agent
echo "bmo-agent restarted with the face GUI. Logs: journalctl -u bmo-agent -f"
