#!/usr/bin/env bash
# Install AI Camera into a target directory and register a systemd service.
#   sudo ./scripts/install.sh [/opt/ai-camera]
set -euo pipefail

DEST="${1:-/opt/ai-camera}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"

if [[ $EUID -ne 0 ]]; then
  echo "please run with sudo: sudo $0 $DEST" >&2
  exit 1
fi

echo "==> installing to $DEST (service user: $RUN_USER)"
mkdir -p "$DEST"
if command -v rsync >/dev/null; then
  rsync -a --delete \
    --exclude '.venv' --exclude 'data' --exclude 'models' --exclude '.git' \
    --exclude '__pycache__' --exclude 'config.json' \
    "$SRC"/ "$DEST"/
else
  tar -C "$SRC" --exclude=.venv --exclude=data --exclude=models --exclude=.git \
      --exclude=__pycache__ --exclude=config.json -cf - . | tar -C "$DEST" -xf -
fi
mkdir -p "$DEST/data" "$DEST/models"
chown -R "$RUN_USER":"$RUN_USER" "$DEST"

echo "==> python environment"
sudo -u "$RUN_USER" python3 -m venv "$DEST/.venv"
sudo -u "$RUN_USER" "$DEST/.venv/bin/pip" install --upgrade pip >/dev/null
sudo -u "$RUN_USER" "$DEST/.venv/bin/pip" install -r "$DEST/requirements.txt"

echo "==> offline models"
sudo -u "$RUN_USER" "$DEST/scripts/download_models.sh" "$DEST/models" || \
  echo "model download failed - they will be fetched on first start"

echo "==> systemd unit"
if getent group video >/dev/null && ! id -nG "$RUN_USER" | grep -qw video; then
  usermod -aG video "$RUN_USER"
  echo "    added $RUN_USER to the 'video' group (re-login required)"
fi
sed -e "s|__USER__|$RUN_USER|g" -e "s|__DIR__|$DEST|g" \
  "$DEST/deploy/aicam.service" > /etc/systemd/system/aicam.service
systemctl daemon-reload
systemctl enable --now aicam.service

IP=$(hostname -I | awk '{print $1}')
echo
echo "done. web UI: http://${IP:-localhost}:8080"
echo "logs:        journalctl -u aicam -f"
