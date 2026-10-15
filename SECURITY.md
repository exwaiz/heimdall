# Security and deployment scope

Heimdall is a single-owner Telegram bridge to an existing local Codex
installation. It is not a multi-tenant service or a hardened remote shell.
Deployment paths and systemd units currently target the original Raspberry Pi
installation under `/home/haaland`; review and adapt them before another deployment.
The checked-in schemas pin the tested Codex CLI version in `schemas/VERSION`.

Never commit bot tokens, router credentials, Codex authentication/configuration,
SSH keys, conversation rollouts, knowledge databases, attachments, or backups.
`.env.example` contains placeholders only. Private runtime files and local
diagnostic snapshots are excluded through `.gitignore`; exclusion is not encryption.
Historical reports describe observations at the time, not current health checks.
Some reports refer to private diagnostic files intentionally absent from Git.

Read mode is the default. Write mode opens only configured workspace roots.
Network mode is a deliberate 30-minute grant of networking and ordinary home/tmp
writes; it is not safe for untrusted prompts or attachments. Existing credential
and bridge/runtime protections remain, but no path denylist protects every possible
future secret. Never place credentials in otherwise writable ordinary directories.
The OS account, administrators, filesystem layout, installed Codex runtime, and
Telegram owner account are trusted components of this deployment.

No uncertain job or uncertain delivery is automatically replayed. Test commands
that contact Telegram or run models are opt-in and may incur usage or send messages.
Voice transcription is currently unsupported. Memory cgroup limits are not enforced
on the original Pi kernel; configured values are not a resource isolation guarantee.

Do not put credentials or private conversation contents in GitHub issues. If a
credential is exposed, revoke/rotate it; removing it in a later commit is insufficient.
