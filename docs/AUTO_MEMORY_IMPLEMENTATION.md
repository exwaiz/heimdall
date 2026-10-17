# Auto-Memory implementation status

The imported [v0.1 design](AUTO_MEMORY_DESIGN_v0.1.md) is preserved verbatim.
The owner subsequently changed the canonical knowledge prefix from HK to GB.
The imported design is historical; current output uses GB while legacy HK
inputs, frozen reference digests and strict citation snapshots remain compatible.
Development starts from `main` at `5aac828`, on `feature/auto-memory-foundation`.

## Current status — 2026-10-17

PR-A/B/C are deployed. The rollout default was OFF; the owner subsequently
enabled Auto-Retrieve and exercised it through Telegram. Automatic capture
remains unimplemented/OFF. Canonical IDs are GB, with legacy HK compatibility.
Schema 9 additionally supports explicit owner Knowledge replacement and revision
history. `/memory` without arguments remains diagnostic; natural-language
arguments perform bounded save/read/update operations, collection pagination and
duplicate-title suppression. See [current command behavior](MEMORY_REQUESTS.md).
The final regression suite passed **326 tests**, with fake model/transport
fixtures and temporary databases. Known natural-language limitations are recorded
there; passing tests are not a claim of unrestricted language understanding.

## Historical: initial PR-A/B/C deployment OFF

The owner approved rollout. [Deployment verification and manual test](AUTO_MEMORY_DEPLOYMENT.md)
record the idle backup, original-data/config preservation, v8 migration and ready
service. Auto-Retrieve remains OFF; no real model call or test thread was created.

PR-B adds normal-text integration. References are frozen in the same SQLite
transaction as the unchanged prompt, selected thread, permission and receipt.
Duplicates do not reselect sources. Retry copies the same frozen refs, including
NULL, rather than broadening the snapshot with newly added knowledge.

Immediately before adapter dispatch, refs are re-resolved with owner, deletion,
all version digests, opt-in and read-only checks. A currently writable binding
also suspends queued readonly background. Stale/missing data and local retrieval
exceptions fall back to the original question, without raw prompt/exception logs.

`CodexAdapter.run_turn(extra_context=...)` accepts separate keyword-only text after
the original user input and memory/policy guidance, before image inputs. Native
schema validation is tested with mocked RPCs; no actual Codex process is started.
The entire framed JSON input, including escaping, is bounded to 3,500 characters.
It identifies sources, dates and heuristic relevance as untrusted reference data,
not instructions/tool authorization or verified truth. Prompt framing is not a
complete defense against every model failure; sandbox/approvals remain unchanged.

`/ask`/`/extract` keep their independent `knowledge_context` and strict output
validation. Photo captions do not auto-retrieve. Ordinary image output and
dedicated-owner handoff remain enabled and have regression coverage.

PR-C provides private-owner controls:

- `/knowledge auto on|off`: default OFF, never enables capture. OFF suppresses
  queued background too. Native Codex memory settings are untouched.
- `/knowledge status` (also `/knowledge`, `/knowledge auto status`): setting,
  current permission gating, bounds and counts from up to 50 recent owned jobs.
  Selection/context preparation counts do not prove model use or factual truth.
- `/knowledge test <question>` (also `/knowledge auto test ...`): local preview
  while OFF or ON, showing IDs/scores/skip reason, not excerpts. No model call,
  new job, or preference mutation. Writable/temporary profiles stay blocked.
- `/memory` and `/memories`: dynamic setting/mode explanation at this stage;
  subsequent explicit Knowledge operations are described above.

2026-10-17 PR-B/C verification: **304 total tests passed**, including 57
Auto-Memory tests and 14 handoff tests. Compile, dependency and diff checks passed.
The input contract uses the pinned local schemas and the
[official App Server input documentation](https://learn.chatgpt.com/docs/app-server).

Fixed-format job events contain counts/reasons, not prompts, excerpts, extracted
facts or tokens. There is no new background worker or remote API. No automatic
capture is implemented. Already-dispatched references cannot be retrospectively
recalled from a Codex conversation by OFF or `/forget`.

### Local performance scope

`PYTHONPATH=src .venv/bin/python scripts/benchmark_auto_memory.py` uses 10,000
synthetic notes, 60 iterations and a warm cache. One Pi run measured search/rerank
p95 **197.23ms**, including validation/render p95 **199.43ms** (max 200.85ms).
This meets the approximate 200ms target only for that fixture/run, not a guarantee
for real data, cold cache, load or worst-case documents. It uses no live data,
credentials, Telegram or model calls.

### Live deployment/rollback checklist (basic rollout completed; model test pending)

1. Require explicit approval and an idle service: no queued/running/uncertain job,
   pending approval or extraction. Verify the actual deployment checkout/env/data;
   do not start this public checkout as a second bot or alter other services.
2. Run the existing bundle-backup workflow against the actual deployment and
   verify DB/assets integrity. Do not publish or copy authentication into Git.
3. Deploy reviewed code and restart the single service only with approval. Verify
   migration 8, original job/item counts, integrity, `bridge_ready`, no duplicate
   poller and settings still OFF. This basic rollout has been verified.
4. Check `/knowledge status` and `/knowledge test` before opting in. A real model
   smoke requires isolated test-session storage and cleanup so it cannot pollute
   the user's normal conversation list. Then check ordinary replies, `/ask`,
   images, deletion, mode suspension and SSH sequential handoff.
5. Disable behavior with `/knowledge auto off` first if needed. Do not blindly
   replace a live DB/drop migration columns: queues, refs and deliveries must stay
   consistent. Already-dispatched information cannot be retroactively recalled.

## Initial PR-A snapshot (historical; integration above supersedes unwired state)

- Transactional, repeatable schema migration 8 adds `memory_settings` and nullable
  `jobs.auto_memory_refs`. Existing jobs keep their prompts, permissions, states,
  and `/ask`/`/extract` `knowledge_context`. Both settings default OFF.
- `MemorySettings` is an owner-scoped repository API, not a Telegram command yet.
  Reading an absent setting does not create it. A different stored owner is not
  overwritten. `capture_mode='suggest'` is reserved in the schema; no capture
  pipeline or capture setter is implemented.
- `AutoMemoryRetriever` reuses `Knowledge.search` (FTS5 trigram plus literal LIKE).
  A greeting/single generic term is skipped. Exact HK IDs, distinctive identifiers
  such as `8DT1`/`F27`, or two meaningful matching terms are required. Title, tags,
  source type and deterministic newest-item tie-breaking affect ranking. This is
  lexical heuristic relevance, not semantic or factual verification.
- At most three references are returned. Each freezes canonical ID, extractor,
  selected content SHA-256 and whole-item version SHA-256. The latter covers all
  extractor digests plus owner, title, tags, kind, creation time and provenance.
  No question or excerpt is copied into the reference JSON.
- Revalidation requires the owner, current opt-in and read-only permission.
  Workspace/network modes and the legacy temporary-access override are refused.
  Deleted, changed, foreign, corrupt and malformed references are omitted; other
  valid sources survive. Direct content tampering is checked against the digest.
- Matching late passages are selected from their actual extractor. Each excerpt
  is at most 850 characters, with titles limited to 120. Three such sources leave
  room for a future framing header inside the design's 3,500-character limit;
  PR-B must enforce the full rendered-context limit, not only the excerpt limit.
- Obvious credential patterns are excluded. This is **not** a comprehensive
  sensitive-data classifier. Retrieved prose remains untrusted data; no text is
  executed here. Safe prompt framing and real injection are still PR-B work.

No changes to Bridge dispatch, Codex RPCs, fake adapter contracts, memory guidance,
handoff, image delivery or the live service are included in PR-A. Even setting
the internal flag ON does not yet inject anything into ordinary conversations.
`/knowledge auto ...` commands are not available yet. `/ask` stays strict grounded
mode, and `/memory` remains accurate for the currently unwired normal path.

## Validation scope

Initial PR-A result: 24 new tests and 270 total tests passed on the Pi, using
the existing dependency-equipped Python environment with this checkout's `src`
on `PYTHONPATH`. Compile checks and `git diff --check` passed. The imported
design file matches the supplied Markdown byte-for-byte.

Run the new tests and the full existing suite using the project's Python
environment with its dependencies installed:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_auto_memory.py -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_auto_memory*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
```

All new fixtures use fresh private temporary SQLite databases. The bridge boundary
test uses `FakeCodexAdapter` and `FakeTelegramTransport`, never a real Codex thread.
The tests cover migration from v7/reopening, defaults/authorization, relevance,
Korean terms and mixed identifiers, late document passages, reference bounds,
forget/owner/content/provenance changes, malformed refs, mode/OFF revocation,
backup/restore and unchanged ordinary/strict-ask behavior.

The initial PR-A/B/C development used no real model calls, Telegram messages,
operating DB migration, service restart, live settings activation, new credentials
or local AI models. The subsequently approved rollout is recorded above. The
10,000-item p95 target was unmeasured at PR-A; the later fixture result is above.

## Next increments

PR-B and PR-C code are now implemented as described above.

1. Real-device/model validation and live migration/restart require explicit user
   approval. No smoke script should create persistent tests in the user's normal
   conversation list again; design test-session isolation/cleanup first.
2. Phase B capture suggestions and Phase C automatic capture remain separate
   opt-in work. This patch does not store ordinary chat as new knowledge.
