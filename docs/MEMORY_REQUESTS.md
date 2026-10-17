# Natural-language Knowledge commands — 2026-10-17

`/memory` and `/memories` retain their no-argument diagnostic report. Explicit
natural-language arguments now perform local owner-scoped Knowledge operations:

```text
/memory 저장해줘: TST731 알림 주기는 17분이야.
/memory GB-000001 읽어줘
/memory TST731 검색해줘
/memory GB-000001 내용을 업데이트해줘. TST731 알림 주기는 20분이야.
```

These are bounded explicit-intent rules, not an unrestricted language model.
Ambiguous or negated mutations fail without changes. Replacement requires exactly
one owned, active ID and the complete new text. Partial change instructions are
not silently treated as facts. HK aliases resolve to the same GB record. Deletion
remains the explicit `/forget` command. No model call, API credential, conversation
binding, native-memory setting change, or sandbox/network expansion is needed.

Schema 9 adds only `knowledge_edits`: atomic message receipts, per-item revision
numbers and previous active title/content snapshots. Original attachment files
and original/extracted content rows remain intact. `user-update-v1` becomes the
sole searchable active content after replacement; subsequent extraction cannot
override it. The total history text cap is 20 million characters; reaching it
rejects an edit rather than silently dropping history. History remains private
and is included in existing DB backup bundles, not application logs.

Auto-Retrieve references and queued/in-flight grounded `/ask` replies validate
the manual revision. Stale context is excluded/rejected; already-dispatched model
context and prior conversation cannot be recalled. Existing unedited GB/HK hashes
remain compatible. A general chat message without `/memory` is not silently
converted into a storage write.

## Runtime rollout

- Existing processing/model/approval queues were idle. One pre-existing uncertain
  delivery was retained unchanged, not retried or removed.
- Created and verified a private DB/asset ZIP and a separate previous-code archive.
- Applied six reviewed modules including the new intent handler, preserving runtime
  config, launcher, credentials, service unit and Codex sessions.
- Restarted the existing service; `bridge_ready` reported Codex CLI 0.162.1.
- Schema 9, integrity check `ok`, edit history empty. All pre-existing tables except
  the additive migration ledger had identical logical hashes. Auto-Retrieve ON
  remained ON; no actual knowledge item was modified for the rollout.
- Actual runtime handler read an existing owned item using a read-only connection.
  Mutation tests used temporary databases and fake transports/models only; no new
  real model/test conversation was created. Live Telegram interaction awaits the
  owner's test.

The existing Pi memory-cgroup warning remains unrelated and was not suppressed.

## Subsequent fixes and known limitations

Collection requests such as `/memory 저장된 모든 지식을 읽어줘` now list
all active owned items in ID order, five per page, with total counts and a next
page command. Long entries explicitly identify partial display. Read, search and
collection output suppress auto-derived titles that duplicate the first body
line; distinct titles, intentional body repetition and stored data remain intact.
Runtime read-only checks confirmed these fixes; the complete suite passed 326
tests before publication.

Intent recognition remains bounded lexical rules, not general natural-language
understanding. Extra playful suffixes such as `보여줘바바바` can still turn a
collection request into a keyword search and return only some matching items.
This is a known unresolved limitation; use `/memory 전체 읽어줘` for an
unambiguous collection request. No claim is made that arbitrary phrasing works.
