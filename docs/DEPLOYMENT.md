# Phase 2 permanent deployment

Phase 4 updates only the reviewed Heimdall backup unit's ExecStart to add
`--include-assets`. The daily archive now includes the SQLite snapshot and
verified original/derived assets. See KNOWLEDGE.md for restoration. Earlier
DB-only backup statements below describe the original Phase 2 deployment.

The main operator completed the permanent deployment on 2026-10-15. Read-only
verification recorded the system bridge active with PID 738365 and enabled, the
backup timer active and enabled, the old user bridge inactive, and the backup
service successful (`Result=success`, `ExecMainStatus=0`). All three source units
matched their installed files exactly. These are observed deployment results;
rerun the checks below for current state. No reboot or boot configuration change
was performed as part of this documentation update.

The system bridge runs as `haaland:haaland`, from
`/home/haaland/heimdall`, using `.venv/bin/python scripts/run_bridge.py`.
It starts at boot through `multi-user.target`, independently of user login and
linger. `HOME=/home/haaland` and `CODEX_HOME=/home/haaland/.codex` retain the existing
Codex login. The launcher loads the existing private
`/home/haaland/.config/heimdall/bridge.env`; neither units nor installation
commands contain tokens. Do not print, source, or copy credential contents into
systemd configuration. Private configuration may override `CODEX_HOME`; the
operator must ensure its effective value still uses the intended existing login.

## Review and prerequisites

```sh
cd /home/haaland/heimdall
bash -n scripts/install_service.sh
bash scripts/install_service.sh --verify-only
.venv/bin/python -m unittest discover -s tests -p test_deployment.py -v
```

The installer defaults to verification only. Run it as `haaland` (UID 1000),
not with `sudo bash`. Verification uses `systemd-analyze verify` on all three
current checkout unit paths before any installation mutation. It does not prove
that the backup script, authentication, network, or Codex sandbox works.

The backup unit invokes the operations worker's CLI as:

```sh
/home/haaland/heimdall/.venv/bin/python /home/haaland/heimdall/scripts/backup_bridge.py backup --destination-dir /home/haaland/.local/share/heimdall/backups
```

The installer creates only the final `backups` directory, as `haaland:haaland`
with mode 0700, after successful preflight. Its parents must already exist.
Directory traversal rejects symlinks at every component; an existing destination
must already have the correct owner, group, and exact 0700 mode. Existing paths
are never chmodded or chowned. The preflight checks these conditions without
creating the directory; `--verify-only` never creates it either. Backup files and
the directory are preserved on rollback.

The CLI uses SQLite online backup and creates private files. The timer calls it
directly and does not load Telegram credentials through `run_bridge.py`. Both
services include `/home/haaland/.local/bin` in PATH so `Config.load(live=False)`
can find Codex. The backup uses the CLI's configured/default data path, independent
of the installer's `--database` argument. If the bridge uses a custom data path,
reconcile the backup service configuration before deployment. The installer
refuses installation while this script or the bridge interpreter is missing.
Backup/restore validation is owned by the operations worker. The current CLI
does not prune old snapshots: monitor disk space and establish retention before
long-term unattended operation.

Pause incoming Telegram commands and drain the queue before handoff. Supply the
effective bridge database path explicitly, especially if `HEIMDALL_DATA_DIR` was
configured. The installer does not read `bridge.env` to discover it. A wrong but
valid database cannot be detected automatically: the operator must confirm the
path matches the running launcher configuration. Default:
`/home/haaland/.local/share/heimdall/bridge.sqlite3`.

Read-only inspection on this target confirmed systemd 257.13, a cgroup v2
hierarchy, the user manager's `FreezeUnit`/`ThawUnit` D-Bus methods, and a
haaland-owned writable `cgroup.freeze` for the exact transient Heimdall service.
The live service reported `FreezerState=running` during read-only artifact
verification. During the main operator's subsequent deployment attempt, systemd
257 refused stopping it while frozen with
`Cannot perform operation on frozen unit heimdall-bridge.service.` The cleanup
trap restored the old bridge to running. The installed manual's automatic-thaw
description did not match that observed behavior; the installer now explicitly
thaws immediately before stopping.

The installer requires `FreezerState=running` and an accessible cgroup freezer
before installing artifacts, rejects user drop-ins, and rechecks freezer state
immediately before freezing. It freezes only the exact transient Heimdall cgroup,
requires `FreezerState=frozen`, checks the SQLite states again, explicitly thaws,
then immediately stops it synchronously. After stop, it confirms the user unit is
inactive/failed and checks the database again before enabling or starting the
system consumer. This leaves a short, nonzero thaw/stop race: keep incoming input
quiet until shutdown and handoff finish. A queued, dispatching, or running job
found after stop blocks the system start and leaves the old bridge stopped for
operator review. If shutdown has already recorded an interrupted running job as
`uncertain`, that state is preserved and is never automatically replayed. The
idle checks do not prove that no request arrived during the thaw/stop interval.
A failed frozen check, thaw/freeze/stop failure, SIGINT, or SIGTERM attempts to thaw only the same exact
transient unit and does not start the system consumer. Thaw failure is reported
explicitly. SIGKILL/power loss cannot run shell cleanup; if an interrupted handoff
leaves the transient unit frozen, verify its exact fragment path and recover it
explicitly before retrying. The automated tests reproduce refusal to stop a
frozen unit, require the explicit thaw/stop sequence, and never freeze the live
service. Main performs the operational handoff validation.

`NoNewPrivileges`, namespace restrictions, and broad filesystem protections are
intentionally absent until the deploying operator tests compatibility with Codex
and bwrap user namespaces. Validate the existing read-only sandbox under the actual
system service identity before declaring deployment complete.

## Install and hand off (explicit deployment action)

```sh
cd /home/haaland/heimdall
bash scripts/install_service.sh --install --database /home/haaland/.local/share/heimdall/bridge.sqlite3
```

Only this command performs installation. It checks for conflicting system unit
fragments, any drop-ins, symlinks/masks, and differing target files. It accepts
identical reviewed files for reruns, but refuses to overwrite differing files,
including older Heimdall revisions. Concurrent installers from the same script
are rejected with `flock`. Leave unit administration and source changes idle during
installation. It reads only counts from SQLite using `mode=ro` and `query_only`;
missing databases, unreadable schemas, queued, dispatching, or running jobs block
handoff. Failed/uncertain jobs remain available for the normal recovery workflow.

Privilege preflight uses `sudo -n true`, which succeeds with the target's
noninteractive sudo permission. It does not use `sudo -v`: the actual deployment
attempt prompted for a password despite the reported `NOPASSWD:ALL` grant. Failure
of `sudo -n true` aborts before directory creation or unit installation; credentials
must never be supplied through this installer.

After preflight, `sudo install` creates only these missing paths as root:root 0644:

- `/etc/systemd/system/heimdall-bridge.service`
- `/etc/systemd/system/heimdall-backup.service`
- `/etc/systemd/system/heimdall-backup.timer`

The installer prints each newly installed path; preserve this list for rollback.
It reloads the system manager, stops only the user unit whose fragment is exactly
`/run/user/1000/systemd/transient/heimdall-bridge.service`, and waits for it to stop
before starting the system bridge. No other user unit is stopped. A system bridge
already active alongside an active user bridge blocks deployment. The runtime
bridge lock additionally rejects a second consumer of the same data directory;
do not manually launch another consumer or invoke `start_transient_service.sh`
while the system bridge is active.

Only `heimdall-bridge.service` and `heimdall-backup.timer` are enabled and started.
The backup service is triggered by its separate daily timer, at 03:00 local host
time plus up to one hour of random delay. `Persistent=true` catches up a missed
timer occurrence when the timer next starts, so installation can trigger a backup
immediately. It does not depend on or restart the bridge. No Kakao services or
global journald settings are changed. No reboot is performed.

Installation is deliberately not an automatic rollback transaction. A failure may
leave new unit files installed, or leave the user bridge stopped. Inspect the
reported error and current states; never start the transient bridge until the
system bridge is confirmed stopped. Reruns do not restart an active system bridge.
The main operator reported that the first attempt installed all three exact unit
files as root but did not enable them, and restored the old transient bridge
after the frozen-stop refusal. This records that attempt, not the current live
deployment status; inspect current state before retrying. Identical installed
files are accepted by the corrected installer without overwriting them.

## Verify the deployed service

```sh
systemctl --user show heimdall-bridge.service -p ActiveState -p FragmentPath
systemctl show heimdall-bridge.service -p User -p Group -p ActiveState -p MainPID -p FragmentPath -p MemoryCurrent -p MemoryHigh -p MemoryMax
systemctl is-enabled heimdall-bridge.service heimdall-backup.timer
systemctl list-timers heimdall-backup.timer --no-pager
systemctl show heimdall-backup.service -p Result -p ExecMainStatus
cat /sys/fs/cgroup/cgroup.controllers
# Follow the service's actual cgroup path; the inspection is read-only.
bridge_cgroup=$(systemctl show heimdall-bridge.service -p ControlGroup --value)
ls -l -- "/sys/fs/cgroup${bridge_cgroup}/memory.current" "/sys/fs/cgroup${bridge_cgroup}/memory.high" "/sys/fs/cgroup${bridge_cgroup}/memory.max"
journalctl -u heimdall-bridge.service -n 50 --no-pager
journalctl -u heimdall-backup.service -n 30 --no-pager
```

Journal access may require the operator's normal read permission. Confirm
`bridge_ready`, only one Telegram poller, owner-only `/status` response, sandbox
smoke results, and a successful backup/restore check. `is-active` alone does not
prove readiness for this `Type=exec` bridge. Follow the backup worker's documented
restore procedure; do not restore over a live bridge database. Boot enablement can
be verified without rebooting; an actual boot test remains a separate explicit
operator action.

The unit files configure bridge `MemoryHigh=1G` and `MemoryMax=1500M`, and backup
256MB/512MB thresholds. On this actual kernel these memory limits are **not
enforced**: `/sys/fs/cgroup/cgroup.controllers` reports `cpuset cpu io pids`, with
no `memory` controller, and the live bridge reports `MemoryCurrent=[not set]`.
`systemctl show` still displays the configured numeric `MemoryHigh`/`MemoryMax`
values; those values alone do not establish enforcement. The per-service
`memory.current`, `memory.high`, and `memory.max` files in the check above are
absent on this host, so that `ls` is expected to report missing files.

There is currently no cgroup memory ceiling protecting the 4GB Pi from bridge,
Codex-child, or backup memory growth. Monitor host memory and swap during real
jobs; host-wide memory pressure can affect other workloads. If a memory controller
is available on a future kernel, verify it is enabled down to each service cgroup,
check the actual `memory.high`/`memory.max` values and accounting there, and test
representative workloads before relying on the configured limits. Such limits
would cover the entire service cgroup, including the Codex child, and would not
reserve memory. Enabling kernel memory control or changing boot configuration is
outside the authorized deployment scope; no such change was made.

The bridge also configures low CPU/IO weights and 128 tasks; backup uses low
scheduling priority. Bridge failures restart after five seconds, with at most
five starts per minute; investigate failures before resetting the start limit.
A memory kill may leave a job uncertain rather than safely rerunnable.
Logs use the journal with per-unit rate limits; disk retention follows the host's
existing journald policy and is not changed here.

## Rollback (explicit operator action only)

First pause incoming commands and confirm no queued/dispatching/running jobs using
the same read-only database check in the installer. Record pre-install enabled and
active states if these units already existed. For a fresh installation, explicitly
stop and disable only the newly deployed bridge and timer:

```sh
sudo systemctl disable --now heimdall-backup.timer heimdall-bridge.service
sudo systemctl stop heimdall-backup.service
systemctl is-active heimdall-bridge.service
```

The final check must report inactive/failed before starting another consumer.
Stopping a backup mid-run may leave a partial artifact; follow the backup worker's
cleanup guidance. Disabling units is reversible and preserves their files. To
remove them, use the installer output to identify which files were newly created,
and verify they still match the reviewed sources with `cmp` first. Only after an
explicit decision to remove all three newly created, unchanged files:

```sh
cmp deploy/heimdall-bridge.service /etc/systemd/system/heimdall-bridge.service
cmp deploy/heimdall-backup.service /etc/systemd/system/heimdall-backup.service
cmp deploy/heimdall-backup.timer /etc/systemd/system/heimdall-backup.timer
# Run only if all three were newly installed and every comparison succeeded.
sudo rm -- /etc/systemd/system/heimdall-bridge.service /etc/systemd/system/heimdall-backup.service /etc/systemd/system/heimdall-backup.timer
sudo systemctl daemon-reload
```

If only some files were new, remove only those exact paths. Preserve any
pre-existing units and restore their recorded enablement/state instead. Unit
files can be recovered from `deploy/`; bridge data, backups, private configuration,
and Codex authentication are never removed by these steps. Once the system bridge
is stopped, the prior transient workflow can be restored explicitly:

```sh
bash scripts/start_transient_service.sh
systemctl --user status heimdall-bridge.service --no-pager
```

No rollback step requests a reboot, changes linger, alters protected projects, or
enables unrelated services.
