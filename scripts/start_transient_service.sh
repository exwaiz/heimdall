#!/usr/bin/env bash
# Detached user service for connection testing; no persistent unit or enable operation.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if systemctl --user is-active --quiet heimdall-bridge.service; then
    echo 'Heimdall user service is already active.'
    exit 0
fi
exec systemd-run --user --unit=heimdall-bridge --description='Heimdall Telegram bridge (transient)' \
    --property=Type=exec \
    --property="WorkingDirectory=$project_dir" \
    --property=Restart=on-failure \
    --property=RestartSec=5 \
    --property=TimeoutStopSec=30 \
    --property=UMask=0077 \
    --property=StartLimitIntervalSec=60 \
    --property=StartLimitBurst=5 \
    "$project_dir/.venv/bin/python" "$project_dir/scripts/run_bridge.py"
