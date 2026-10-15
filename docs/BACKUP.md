# Phase 2 recovery and backups

`Operations(store, config)` contains synchronous SQLite-only helpers; async command
handlers may call them directly. They never call Codex or Telegram. The worker
performs normal preflight checks on newly queued jobs.

| Method | Result |
| --- | --- |
| `inspect(job_id, chat, user)` | Owner-scoped metadata dictionary |
| `status(chat, user, limit=20, *, before_job_id=None)` | Newest-first metadata list, limit 1–100; exclusive cursor |
| `retry(job_id, chat, user, message_id, new_baseline, maximum, *, confirm_repeat=False)` | New queued job ID |
| `resolve(job_id, decision, chat, user)` | Resolved job metadata; decision must be `failed` or `cancelled` |
| `resend(delivery_id, chat, user, message_id, confirm_uncertain=False)` | Existing delivery ID, requeued once |

All commands check configured identity and database row ownership. Mutation and
audit writes share one transaction. Inspection excludes prompts, responses, and
baseline content. Audit events contain IDs and fixed decisions, never prompts.
Helpers tolerate command IDs already claimed by `Bridge.handle`; operation audit
markers deduplicate the actual mutation and prevent command IDs being reused
across retry and resend. Call the helpers outside an existing Store transaction.

Retry accepts known preflight failures with no turn ID and no running event.
Completed or failed jobs with a recorded turn ID require `confirm_repeat=True`:
the original turn may already have performed actions, so repeating can duplicate
effects. Uncertain, running, dispatching, queued, and cancelled jobs cannot retry.
Any uncertain/running/dispatching job on the same thread blocks retry. Supply a
fresh baseline from the current thread snapshot. The source job remains intact.
The retry preserves its permission mode; workspace jobs require a current,
database-owned workspace binding. A readonly source stays readonly even if the
binding now allows workspace writes. Legacy schemas without mode columns work.

Manual resolution records an operator decision about an uncertain job. It does
not prove that execution never occurred, undo changes, or automatically enqueue
another turn. Resolve only after examining authoritative turn state. A resolved
failed job with a known turn still requires repeat confirmation; a manually
resolved failure without a turn is not treated as a safe preflight failure.

Resend requeues only failed/uncertain outbox rows. For uncertain delivery, show
the owner a warning that Telegram may already have accepted the message and
require `confirm_uncertain=True`. Sent/pending/sending/split rows cannot be newly
requeued. Repeating the same command message ID is a no-op, including after a
successful send. No new outbox row is made. Rows with non-null `reply_markup`,
including split descendants of such rows, are never replayed. Resend requires a
matching database chat binding; job-linked deliveries also require job ownership.

## Backup CLI

Use the installed project environment. No service changes or live writes are
needed for backup. Do not source secret environment files: this CLI calls
`Config.load(..., live=False)` and never prints config, tokens, or exception text.
`HEIMDALL_DATA_DIR` may be set explicitly to select the intended bridge data root.

Create an empty private destination directory (0700) before running:

```sh
.venv/bin/python scripts/backup_bridge.py --config config.toml backup \
  --destination-dir /absolute/private/backup-directory
```

`backup_database(config, destination_dir)` reads only
`config.data_dir/bridge.sqlite3` through SQLite `mode=ro` and uses the online
backup API, including committed WAL contents. It reserves a unique 0600
`bridge-backup-*.sqlite3` destination with exclusive creation, checks disk space
for the database plus `config.min_disk_bytes`, and verifies both source and
destination with `integrity_check` and `foreign_key_check`. It also requires bridge
schema tables. It does not copy directories, environment secrets, Codex histories,
credentials, or service configuration. Backups **do contain local prompts and
answers** already stored in the bridge DB; protect them like the original DB.
Concurrent source growth can still exhaust disk after the initial check; failure
removes only the fresh artifact reserved by that invocation.

## Restore to a fresh destination

```sh
.venv/bin/python scripts/backup_bridge.py --config config.toml restore \
  --backup /absolute/private/backup-directory/bridge-backup-example.sqlite3 \
  --destination /absolute/bridge-data/recovered.sqlite3

# Alternatively use a separate existing private directory:
.venv/bin/python scripts/backup_bridge.py --config config.toml restore \
  --backup /absolute/private/backup-directory/bridge-backup-example.sqlite3 \
  --safe-directory /absolute/private/recovery-directory \
  --destination /absolute/private/recovery-directory/recovered.sqlite3
```

`restore_database(config, backup_path, destination, *, safe_directory=None)`
validates and copies through the same SQLite API. The explicit destination must
be fresh, beneath `config.data_dir` or an explicitly supplied separate private
directory. Parent directories must already exist and be owner-private; the helper
does not chmod existing paths. Existing targets or SQLite sidecars, symlinks in
any path component, protected directories, and the live `bridge.sqlite3` path are
refused. Missing sources are not created. Restore never swaps the live database,
starts a service, migrates/reconciles jobs, or reruns queued work. Inspect the
restored data offline and plan any later service/database cutover separately;
the normal Store recovery process must mark interrupted work uncertain before
dispatch. This implementation performs no live cutover.
# Phase 4 전체 백업 안내

현재 `/backup` 및 일일 timer는 `--include-assets`로 DB+원본 ZIP을 만든다.
새 `restore-bundle` 명령과 제한은 [KNOWLEDGE.md](KNOWLEDGE.md)의 저장·복구 절차를 참조한다.
아래 내용은 기존 DB-only 백업/복원 인터페이스의 설명이며 그대로 사용할 수 있다.

