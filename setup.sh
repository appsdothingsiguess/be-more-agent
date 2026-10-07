#!/usr/bin/env bash
# BMO thin-client setup for Raspberry Pi OS Lite (64-bit).
#
# Architecture change from upstream: the original script installed and
# downloaded local AI runtimes and model files onto the Pi. In this fork ALL
# AI work (LLM, vision, speech-to-text, text-to-speech) runs on the home
# server. The Pi is a thin client: it captures audio and camera frames, talks
# to the server over HTTP, and plays back the result. This script therefore
# installs only a few system packages and Python libraries. It downloads
# nothing and the Pi holds no models.
#
# Run as the normal user. sudo is used only for apt and --install-service.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO/venv"
SKIP_APT=0
WITH_WAKEWORD=0
WITH_DEV=0
INSTALL_SERVICE=0
DRY_RUN=0

usage() {
    cat <<USAGE
Usage: ./setup.sh [options]

  --help              Show this help and exit
  --venv PATH         Virtualenv location (default: ./venv in the repo)
  --skip-apt          Do not install system packages
  --with-wakeword     Also install requirements-wakeword.txt
  --dev               Also install requirements-dev.txt (pytest)
  --install-service   Install /etc/systemd/system/bmo-agent.service
                      (does NOT enable or start it)
  --dry-run           Print each command instead of running it
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --venv)
            [ $# -ge 2 ] || { echo "--venv needs a path" >&2; exit 2; }
            VENV="$2"; shift ;;
        --skip-apt) SKIP_APT=1 ;;
        --with-wakeword) WITH_WAKEWORD=1 ;;
        --dev) WITH_DEV=1 ;;
        --install-service) INSTALL_SERVICE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] $*"
    else
        "$@"
    fi
}

warn() { echo "WARNING: $*" >&2; }

cd "$REPO"

# 1. Platform check (warn only)
echo "[1/6] Checking platform..."
[ "$(uname -m)" = "aarch64" ] || warn "not aarch64 ($(uname -m)); this is meant for a 64-bit Pi."
if ! grep -qa "Raspberry Pi" /proc/device-tree/model 2>/dev/null; then
    warn "this does not look like a Raspberry Pi."
fi

# 2. System packages
echo "[2/6] System packages..."
if [ "$SKIP_APT" -eq 1 ]; then
    echo "Skipping apt (--skip-apt)."
else
    run sudo apt-get install -y python3-venv python3-tk ffmpeg alsa-utils rpicam-apps
fi

# 3. Virtualenv and Python packages
echo "[3/6] Python environment at $VENV..."
if [ ! -x "$VENV/bin/python" ]; then
    run python3 -m venv "$VENV"
else
    echo "Reusing existing venv."
fi
PIP=("$VENV/bin/python" -m pip)
run "${PIP[@]}" install -r "$REPO/requirements.txt"
[ "$WITH_DEV" -eq 0 ] || run "${PIP[@]}" install -r "$REPO/requirements-dev.txt"
[ "$WITH_WAKEWORD" -eq 0 ] || run "${PIP[@]}" install -r "$REPO/requirements-wakeword.txt"

# 4. Runtime dir and config
echo "[4/6] Runtime directory and config..."
run mkdir -p "$REPO/runtime"
if [ ! -e "$REPO/config.json" ]; then
    run cp "$REPO/config.example.json" "$REPO/config.json"
else
    echo "config.json already exists; leaving it alone."
fi

# 5. Credential file presence check (never read or print contents)
echo "[5/6] Checking for the server credential file..."
FOUND=""
for f in "${BMO_TOKEN_FILE:-}" /etc/bmo/token "$HOME/.config/bmo/token"; do
    if [ -n "$f" ] && [ -r "$f" ]; then FOUND="$f"; break; fi
done
if [ -n "$FOUND" ]; then
    echo "Credential file found: $FOUND"
else
    warn "no readable credential file at \$BMO_TOKEN_FILE, /etc/bmo/token or ~/.config/bmo/token."
fi

# 6. Optional systemd service (installed, not enabled)
echo "[6/6] Service..."
if [ "$INSTALL_SERVICE" -eq 1 ]; then
    UNIT_TMP="$(mktemp)"
    trap 'rm -f "$UNIT_TMP"' EXIT
    sed -e "s|@USER@|$(id -un)|g" -e "s|@REPO@|$REPO|g" -e "s|@PYTHON@|$VENV/bin/python|g" \
        "$REPO/tools/bmo-agent.service" > "$UNIT_TMP"
    run sudo install -m 644 "$UNIT_TMP" /etc/systemd/system/bmo-agent.service
    run sudo systemctl daemon-reload
    echo "Service installed but NOT enabled or started."
    echo "Only after the acceptance tests pass, run:"
    echo "  sudo systemctl enable --now bmo-agent"
else
    echo "Not installing the service (use --install-service)."
fi

echo
echo "Done. Next steps:"
echo "  $VENV/bin/python -m app --self-test"
echo "  $VENV/bin/python -m app --headless"
