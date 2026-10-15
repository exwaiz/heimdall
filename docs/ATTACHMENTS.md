# Phase 3 attachment storage

`AssetStore(store, config)` owns the `assets` and `asset_events` SQLite tables and
the private `config.data_dir/assets` directory. Construction creates only these
tables and directory; it does not alter jobs, start services, read credentials,
or contact Telegram. Existing directories must already be owned by the current
user and have mode 0700. Files have mode 0600. Symlink/protected paths are refused.

```python
assets = AssetStore(
    store, config,
    max_file_bytes=20_000_000,
    max_total_bytes=200_000_000,
    max_assets=1000,
    max_group_assets=10,
    max_image_pixels=20_000_000,
    download_timeout=60,
)
record = await assets.receive(message, transport, chat, user)
record = assets.get(asset_id, chat, user)
records = assets.list(chat, user, limit=20, before_asset_id=None)
image_input = assets.local_image(asset_id, chat, user)
image_input = await assets.local_image_async(asset_id, chat, user)
path = assets.validate_image_path(path, chat, user)
```

`receive` and `local_image_async` are async; the other methods are synchronous. Call outside a Store
transaction. `before_asset_id` is keyword-only. Listing is newest-first, bounded
to 1–100 records, with an exclusive cursor. All operations require the configured
private owner; received messages also require matching sender/chat IDs and reject
bots, forwarded messages, and anonymous/channel senders. Database row ownership is
checked independently of message authorization.

Records are dictionaries with `asset_id`, `chat_id`, `user_id`,
`telegram_message_id`, `media_group_id`, `kind`, `state`, `path`, `sha256`,
`size_bytes`, `mime_type`, `declared_mime`, `original_name`, `caption`,
`file_id`, `file_unique_id`, `image_width`, `image_height`, `error_code`,
timestamps, `can_local_image`, and `guidance`. Path/hash/MIME are null until stored.
`caption` is the original string, including whitespace and newlines; absent
captions become an empty string. Treat captions/names as untrusted private content
and do not log complete records. Original filenames are metadata only; all paths
use generated names. No caption, file body, filename, token, or raw transport
exception is written to operational audit events.

| Telegram input | Stored kind | Model handling |
| --- | --- | --- |
| Photo | `image` | Validated PNG/JPEG can become explicit `localImage` input |
| Document, including an image sent as a file | `document` | Stored only; no automatic injection or extraction |
| Audio or voice | `audio` | Stored only, with an unsupported transcription/input guide |
| Video, video note, animation | `video` | Stored only, with an unsupported extraction/input guide |

The largest photo variant is saved once. Each Telegram message creates at most
one asset. Albums retain `media_group_id` but are not accumulated into an
unbounded batch or automatically merged into jobs. The main bridge must freeze
the original selected thread before awaiting receipt, persist asset IDs on the
job, and enqueue an image only after `state == 'stored'` and
`kind == 'image'`. Receipt itself never creates a job. Documents/audio/video do
not become model input even when they have captions.

`local_image` returns `{"type": "localImage", "path": absolute_path}` after
checking ownership, stored-image status, the exact generated path under the
private asset directory, regular-file type, owner, 0600 permissions, single hard
link, byte count, SHA-256, and image format/dimensions. Run it again immediately
before dispatch for each frozen asset ID. `validate_image_path` provides the same
checks starting from an owned DB-recorded path. Neither helper changes sandbox
rules: localImage is read by the app server; model filesystem tools should keep
the bridge data directory denied. These checks do not defend against a malicious
process with the same OS identity replacing a file after validation.
`local_image_async` does the same validation with SQLite ownership lookup on the
event loop and file/hash/image work in a worker thread. Receive also runs file
reading/hash and format validation off the event loop; SQLite is never sent into
those worker threads. Prefer the async helper in the dispatch worker to keep
callbacks responsive for large images.

## Transport contract and bounds

Main owns the transport methods:

```python
metadata = await transport.get_file(file_id)
byte_count = await transport.download_file(
    file_path, destination, max_bytes,
    reserved_identity=(device, inode),
)
```

`get_file` calls Telegram `getFile` and returns its metadata dictionary.
`download_file` streams only from
`https://api.telegram.org/file/bot<TOKEN>/<validated-relative-file_path>`;
redirects and arbitrary origins/URLs must be rejected. Use the fixed HTTPS origin,
disable environment proxy inheritance, enforce the byte limit while streaming,
and suppress token-bearing request/error strings. The module independently
rejects absolute paths, URLs, query/fragment/percent encoding, backslashes, empty
components, and traversal in `file_path` before invoking download.

AssetStore reserves an empty 0600 temporary file. With `reserved_identity`, the
transport must open with `O_NOFOLLOW`, check regular-file type, owner, mode,
single-link status and exact device/inode, then truncate/write. It must not replace
the reserved inode. AssetStore rechecks inode/size and computes SHA-256 over the
actual saved bytes, independently of the transport's returned byte count and
both message/getFile size claims. The file is published under a random generated
name using exclusive hard-link creation; existing files are not overwritten.

The 20,000,000-byte cap is conservative relative to Telegram's documented
[20 MB cloud Bot API download limit](https://core.telegram.org/bots/api#file).
Larger limits/local Bot API servers are not supported here. Each pending receive
reserves the full per-file allowance against the total budget, including unknown
or dishonest announced sizes. Disk free space must cover that allowance plus
`config.min_disk_bytes`. Count limits include failed/interrupted records; group
limits count distinct claimed messages. This bounds metadata as well as file
storage. An overall download timeout bounds metadata and download waits.

Image validation is dependency-free and bounded by file size and pixel count:
non-interlaced 8-bit PNG checks chunk framing/CRC/order and the complete bounded
zlib raster stream/filter bytes. JPEG accepts baseline/progressive frames with
structural marker, dimensions, table and scan checks; it is not a full JPEG pixel
decoder. Unsupported image encodings (including SVG, GIF, WebP and animated PNG)
are refused as image input. Validation does not strip EXIF metadata or certify
files malware-free. Documents use content signatures for PDF, ZIP/Office archive,
OLE/DOC and RTF, or valid UTF-8 text; signatures do not prove full document
validity. Archives are never expanded, and no document, media parser, converter,
OCR engine, executable, or shell is launched. Audio/video are opaque stored bytes.

## Idempotency and recovery

Receipt claims `(chat_id, telegram_message_id)` in a short SQLite transaction
before any network operation. Repeated and concurrent updates return the existing
record without redownloading, changing the original caption, or creating another
file. A changed file ID/kind under the same message ID is refused. State moves
from `downloading` to `stored` or `failed`; audit events record fixed state labels.
Preflight errors raise `AssetError` with fixed codes. Post-claim download/content
failures return a `failed` record with a fixed code and guidance. Cancellation
records failure and propagates cancellation. A failed record is not automatically
retried: send a new Telegram message after correcting the cause.

Controlled failures remove only files owned by that invocation and release the
reserved byte budget. Process/power loss can leave `downloading` rows and private
partial/orphan files; their full reservations remain charged. They are not
automatically redownloaded, reclaimed or deleted. Stop intake and inspect these
offline before an explicit maintenance cleanup. Asset retention and deletion are
not automatic.

The Phase 2 SQLite backup includes asset metadata, not asset file bytes. A complete
attachment backup requires a separately coordinated copy of the private assets
directory plus the matching DB snapshot. Do not interpret a DB-only backup as a
backup of original attachments. URL text/captions are preserved but never fetched;
SSRF-safe URL ingestion and knowledge indexing remain future work.
