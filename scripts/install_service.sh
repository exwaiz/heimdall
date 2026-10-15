#!/usr/bin/env bash
# Run as haaland; sudo is used only for explicit system-manager operations.
set -euo pipefail
project_dir=/home/haaland/heimdall
unit_dir=/etc/systemd/system
transient_path=/run/user/1000/systemd/transient/heimdall-bridge.service
backup_dir=/home/haaland/.local/share/heimdall/backups
cgroup_root=/sys/fs/cgroup
units=(heimdall-bridge.service heimdall-backup.service heimdall-backup.timer)
mode=${1:---verify-only}
database=
fail() { printf '%s\n' "$*" >&2; exit 1; }
case "$mode" in
    --verify-only) [[ $# -le 1 ]] || fail 'Usage: install_service.sh --verify-only' ;;
    --install)
        [[ $# -eq 3 && $2 == --database && $3 == /* ]] ||
            fail 'Usage: install_service.sh --install --database ABSOLUTE_BRIDGE_SQLITE_PATH'
        database=$3 ;;
    *) fail 'Use --verify-only or --install --database ABSOLUTE_BRIDGE_SQLITE_PATH' ;;
esac
[[ $(id -un) == haaland && $(id -u) == 1000 ]] || fail 'Run as haaland (uid 1000), not under sudo.'
[[ $(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P) == "$project_dir" ]] ||
    fail 'Installer must run from the reviewed /home/haaland/heimdall checkout.'
exec 9<"${BASH_SOURCE[0]}"
flock -n 9 || fail 'Another installer is running from this checkout.'
for unit in "${units[@]}"; do
    [[ -f $project_dir/deploy/$unit && ! -L $project_dir/deploy/$unit ]] || fail "Invalid source: $unit"
done
systemd-analyze verify "${units[@]/#/$project_dir/deploy/}"
if [[ $mode == --verify-only ]]; then
    printf '%s\n' 'Unit syntax verified; no service or filesystem mutations performed.'
    exit 0
fi
[[ -x $project_dir/.venv/bin/python && -f $project_dir/scripts/run_bridge.py &&
   -f $project_dir/scripts/backup_bridge.py ]] || fail 'Bridge Python and backup entry point must exist before installation.'

# Reject masks, symlinks, differing files, vendor/runtime units, and all drop-ins.
# Identical reviewed units are accepted for idempotent reruns, never restarted.
for unit in "${units[@]}"; do
    target=$unit_dir/$unit
    [[ ! -L $target ]] || fail "Refusing symlink/mask: $target"
    if [[ -e $target ]]; then
        [[ -f $target ]] && cmp -s "$project_dir/deploy/$unit" "$target" ||
            fail "Refusing to overwrite differing unit: $target"
    fi
    fragment=$(systemctl show "$unit" --property=FragmentPath --value)
    [[ -z $fragment || $fragment == "$target" ]] || fail "Conflicting system unit: $unit"
    dropins=$(systemctl show "$unit" --property=DropInPaths --value)
    [[ -z $dropins ]] || fail "Refusing system unit with drop-ins: $unit"
done
user_fragment=$(systemctl --user show heimdall-bridge.service --property=FragmentPath --value)
user_state=$(systemctl --user show heimdall-bridge.service --property=ActiveState --value)
user_dropins=$(systemctl --user show heimdall-bridge.service --property=DropInPaths --value)
[[ -z $user_dropins ]] || fail 'Unexpected user Heimdall drop-ins; refusing handoff.'
[[ -z $user_fragment || $user_fragment == "$transient_path" ]] || fail 'Unexpected user Heimdall unit; refusing handoff.'
[[ $user_state == inactive || $user_state == failed || $user_state == active ]] || fail 'User Heimdall is transitioning; retry when stable.'
if [[ $user_state == active ]]; then
    [[ $user_fragment == "$transient_path" ]] || fail 'Active user unit is not the exact expected transient unit.'
    system_state=$(systemctl show heimdall-bridge.service --property=ActiveState --value)
    [[ $system_state == inactive || $system_state == failed ]] || fail 'System bridge is already active or transitioning; refusing overlap.'
fi
check_freezer() {
    [[ $(systemctl --user show heimdall-bridge.service --property=FreezerState --value) == running ]] ||
        fail 'Transient unit is already frozen or freezer support is unavailable.'
    cgroup=$(systemctl --user show heimdall-bridge.service --property=ControlGroup --value)
    [[ $cgroup == /* && $cgroup != *..* && -r $cgroup_root/$cgroup/cgroup.freeze &&
       -w $cgroup_root/$cgroup/cgroup.freeze ]] || fail 'Transient cgroup freezer is unavailable or inaccessible.'
}
if [[ $user_state == active ]]; then
    check_freezer
fi

# Traverse with directory descriptors and O_NOFOLLOW. Never chmod/chown an
# existing path or follow a symlink; only the final backups leaf may be created.
prepare_backup_dir() {
    "$project_dir/.venv/bin/python" - "$backup_dir" "$1" <<'PY'
import os
from pathlib import Path
import stat
import sys

fd = None
try:
    parts = Path(sys.argv[1]).parts
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    for part in parts[1:-1]:
        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        os.close(fd)
        fd = child
    try:
        child = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        if sys.argv[2] == '--check':
            sys.exit(0)
        os.mkdir(parts[-1], mode=0o700, dir_fd=fd)
        child = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    os.close(fd)
    fd = child
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_gid != os.getgid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError()
except Exception:
    sys.exit('Backup directory refused: parents must exist without symlinks; destination must be haaland-owned mode 0700.')
finally:
    if fd is not None:
        os.close(fd)
PY
}

# The operator supplies the effective data path without revealing bridge.env.
# Only job counts are read. Missing/invalid DB fails closed, never creates it.
check_idle() {
    "$project_dir/.venv/bin/python" - "$database" <<'PY'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

try:
    path = Path(sys.argv[1])
    if path.is_symlink() or not path.is_file():
        raise ValueError()
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        conn.execute('PRAGMA query_only=ON')
        count = conn.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','dispatching','running')").fetchone()[0]
    if count:
        sys.exit('Handoff refused: queued, dispatching, or running jobs exist.')
except Exception:
    sys.exit('Handoff refused: bridge database could not be checked read-only.')
PY
}
check_idle
prepare_backup_dir --check
# Validate non-interactively. `sudo -v` can demand a password despite NOPASSWD
# command grants; unattended deployment must never prompt or read credentials.
sudo -n true
prepare_backup_dir --create
# Install all artifacts before stopping the old consumer. Failure leaves it running.
for unit in "${units[@]}"; do
    target=$unit_dir/$unit
    if [[ ! -e $target ]]; then
        sudo install -o root -g root -m 0644 "$project_dir/deploy/$unit" "$target"
        printf 'Newly installed: %s\n' "$target"
    fi
done
sudo systemctl daemon-reload
frozen=0
cleanup() {
    if [[ $frozen == 1 ]]; then
        if [[ $(systemctl --user show heimdall-bridge.service --property=FragmentPath --value) == "$transient_path" ]]; then
            systemctl --user thaw heimdall-bridge.service ||
                printf '%s\n' 'WARNING: thaw failed; inspect the exact transient Heimdall unit before recovery.' >&2
        else
            printf '%s\n' 'WARNING: transient unit identity changed; refusing to thaw another unit.' >&2
        fi
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ $user_state == active ]]; then
    # Freeze the whole cgroup for the second check. This host's systemd refuses
    # stop while frozen; explicitly thaw immediately before synchronous stop.
    # Keep input quiet: this narrows but cannot eliminate the check/stop race.
    [[ $(systemctl --user show heimdall-bridge.service --property=FragmentPath --value) == "$transient_path" ]] ||
        fail 'Transient unit changed during preflight.'
    check_freezer
    check_idle
    frozen=1
    systemctl --user freeze heimdall-bridge.service
    [[ $(systemctl --user show heimdall-bridge.service --property=FreezerState --value) == frozen ]] ||
        fail 'Transient freeze was not confirmed; refusing stop.'
    check_idle
    systemctl --user thaw heimdall-bridge.service
    systemctl --user stop heimdall-bridge.service
    frozen=0
fi
user_state=$(systemctl --user show heimdall-bridge.service --property=ActiveState --value)
[[ $user_state == inactive || $user_state == failed ]] || fail 'User consumer has not stopped; refusing system start.'
check_idle
# stop waits for the complete old cgroup; the bridge's flock additionally
# prevents any manually launched consumer from sharing this database.
sudo systemctl enable heimdall-bridge.service heimdall-backup.timer
sudo systemctl start heimdall-bridge.service
sudo systemctl start heimdall-backup.timer
sudo systemctl is-active --quiet heimdall-bridge.service
sudo systemctl is-active --quiet heimdall-backup.timer
printf '%s\n' 'Heimdall system bridge and daily backup timer are active. Review journal for bridge_ready.'
