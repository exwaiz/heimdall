# Auto-Memory rollout — 2026-10-17

The owner explicitly approved live deployment. At initial rollout Auto-Retrieve
remained **OFF** and capture was unimplemented. No real model smoke or artificial
Codex thread was run by the rollout. The owner later enabled Auto-Retrieve and
tested it in Telegram; this document preserves the initial deployment checks.

## Verified rollout

- Existing service was idle: no queued/running/uncertain jobs, pending approvals,
  extraction jobs or pending deliveries. Existing DB was version 7.
- Existing backup workflow produced a verified private DB+asset bundle. Previous
  affected Python modules were separately archived in private backup storage.
- Only reviewed application modules were applied to the actual runtime checkout.
  Its config, private-env launcher, environment loader, deployment location and
  service unit were retained; the public checkout was not started as a second bot.
  No credentials, unrelated services or original Codex session files were edited.
- Service restarted to active/running and logged `bridge_ready` with installed
  Codex CLI 0.162.1. Schema migrated to 8, integrity_check returned `ok`, settings
  table was empty (default OFF), and all old jobs had NULL auto-memory references.
- Logical hashes/counts of all existing columns in jobs, knowledge items/content,
  assets, sessions, bindings, Telegram update/command receipts, outbox and job
  events were unchanged. Fixed runtime config/launcher hashes were unchanged.
- Read-only checks against the actual operating DB exercised the deployed status
  and preview handlers: OFF confirmed, preview response valid, existing owned
  source matched. These checks did not create jobs, preferences or model calls.
- Pre-deployment automated suite: 304 tests passed. All model/transport fixtures
  used fake adapters, not the owner's real conversation list.
- Post-deployment suite imported the actual deployed modules: 304 tests passed.
  All eight deployed module files matched reviewed source byte-for-byte. The
  pre-deployment bundle's ZIP integrity and database manifest hash were verified.

## Owner's Telegram test

Select an idle conversation first (`/database`, `/select <ID>` if needed). Do not
select a thread currently running in SSH. Send these as separate Telegram messages:

```text
/mode read
/knowledge status
/save TST731 프로젝트의 알림 주기는 17분으로 확정했어.
/knowledge test TST731 알림 주기
/knowledge auto on
TST731 알림 주기가 어떻게 됐지?
```

`/save` returns the source's GB ID. Preview should list that ID without a Codex
call or settings change. Only the final ordinary question uses Codex. Check that
the answer uses **17분**, ideally citing the GB ID. Citation in ordinary chat is
guidance, not the strict verification provided by `/ask`.

`/knowledge status` afterwards reports selected/prepared counts, not proof the
model actually used the facts. `/memory` reflects ON and the current read mode.
Write/network/temporary modes suspend automatic background, as do OFF or changed
or forgotten sources. Photos and captions do not auto-retrieve in this release.

To turn it off again, send `/knowledge auto off`. To exclude the test note from
future retrieval, use `/forget <the GB ID returned by /save>`. Neither operation
erases already-dispatched model context, original files, chat history or backups.

## Recovery boundaries

On trouble, turn auto retrieval OFF first and inspect `/jobs`, `/knowledge status`
and service logs. Keep uncertain jobs without auto-repeat. Use the private code
archive/bundle only in a reviewed rollback; do not blindly replace a live DB or
drop the additive v8 schema. Real Telegram/model E2E and SSH sequential handoff
for this rollout await the owner's manual check; earlier fake tests are not a
claim that those live interactions have been observed.
