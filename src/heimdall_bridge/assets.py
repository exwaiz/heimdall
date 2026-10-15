"""Bounded Telegram attachment storage; no URL fetching or automatic job injection."""

import asyncio
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import uuid
import zlib

from .db import now


class AssetError(ValueError):
    """Fixed, public error codes only. Never include transport exceptions or tokens."""


def validate_telegram_file_path(value):
    """Validate getFile's relative path; the transport must pin Telegram's HTTPS origin."""
    if (not isinstance(value, str) or len(value) > 1024
            or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", value)
            or any(part in {".", ".."} for part in value.split("/"))):
        raise AssetError("invalid_telegram_file_path")
    return value


def _private_directory(path):
    path = Path(path)
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise AssetError("unsafe_asset_directory")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise AssetError("private_asset_directory_required")
    return path.resolve(strict=True)


def _dimensions(width, height, maximum):
    if not width or not height or width * height > maximum:
        raise AssetError("image_dimensions_exceeded")


def _png(data, maximum):
    """Validate non-interlaced 8-bit PNG chunks, CRCs and bounded raster stream."""
    offset, width, height, channels = 8, 0, 0, 0
    compressed, palette, ended, idat_ended = [], False, False, False
    while offset < len(data):
        if offset + 12 > len(data):
            raise AssetError("invalid_image_content")
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data) or not re.fullmatch(b"[A-Za-z]{4}", kind):
            raise AssetError("invalid_image_content")
        body = data[offset + 8:end - 4]
        if zlib.crc32(kind + body) & 0xffffffff != int.from_bytes(data[end - 4:end], "big"):
            raise AssetError("invalid_image_content")
        if offset == 8 and kind != b"IHDR":
            raise AssetError("invalid_image_content")
        if kind == b"IHDR":
            if width or length != 13:
                raise AssetError("invalid_image_content")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", body)
            _dimensions(width, height, maximum)
            if depth != 8 or color not in {0, 2, 3, 4, 6} or compression or filtering or interlace:
                raise AssetError("unsupported_image_format")
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
        elif kind == b"PLTE":
            if compressed or palette or not length or length % 3 or length > 768:
                raise AssetError("invalid_image_content")
            palette = True
        elif kind == b"IDAT":
            if idat_ended or (color == 3 and not palette):
                raise AssetError("invalid_image_content")
            compressed.append(body)
        elif kind == b"IEND":
            if length or not compressed or end != len(data):
                raise AssetError("invalid_image_content")
            ended = True
        elif kind in {b"acTL", b"fcTL", b"fdAT"}:
            raise AssetError("unsupported_image_format")
        elif kind[0] < 97:
            raise AssetError("unsupported_image_format")
        if compressed and kind != b"IDAT":
            idat_ended = True
        offset = end
    if not ended:
        raise AssetError("invalid_image_content")
    row_size, count = width * channels + 1, 0
    expected = row_size * height
    decoder = zlib.decompressobj()
    try:
        for chunk in compressed:
            pending = chunk
            while pending:
                output = decoder.decompress(pending, min(65536, expected - count + 1))
                if count + len(output) > expected:
                    raise AssetError("image_decompression_exceeded")
                first_filter = (-count) % row_size
                if any(output[index] > 4 for index in range(first_filter, len(output), row_size)):
                    raise AssetError("invalid_image_content")
                count += len(output)
                pending = decoder.unconsumed_tail
                if decoder.unused_data:
                    raise AssetError("invalid_image_content")
        if count != expected or not decoder.eof:
            raise AssetError("invalid_image_content")
    except zlib.error:
        raise AssetError("invalid_image_content") from None
    return "image/png", ".png", width, height


def _jpeg(data, maximum):
    """Validate JPEG marker framing, frame/scan dimensions and required tables.

    This is structural validation, not a native entropy/pixel decoder.
    """
    offset, width, height, scans = 2, 0, 0, 0
    quantization, huffman, components = set(), set(), set()
    component_tables, frame_type = set(), None
    while offset < len(data):
        if data[offset] != 0xff:
            raise AssetError("invalid_image_content")
        while offset < len(data) and data[offset] == 0xff:
            offset += 1
        if offset >= len(data):
            raise AssetError("invalid_image_content")
        marker = data[offset]
        offset += 1
        if marker == 0xd9:
            if not scans or offset != len(data):
                raise AssetError("invalid_image_content")
            return "image/jpeg", ".jpg", width, height
        if marker in {0, 0xd8, 1, *range(0xd0, 0xd8)} or offset + 2 > len(data):
            raise AssetError("invalid_image_content")
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            raise AssetError("invalid_image_content")
        body = data[offset + 2:offset + length]
        offset += length
        if marker in {0xc0, 0xc2}:
            if width or len(body) < 6 or body[0] != 8:
                raise AssetError("unsupported_image_format")
            height, width = struct.unpack(">HH", body[1:5])
            _dimensions(width, height, maximum)
            count = body[5]
            if count not in {1, 3, 4} or len(body) != 6 + count * 3:
                raise AssetError("invalid_image_content")
            for index in range(6, len(body), 3):
                identifier, sampling, table = body[index:index + 3]
                if identifier in components or not 1 <= sampling >> 4 <= 4 or not 1 <= sampling & 15 <= 4 or table > 3:
                    raise AssetError("invalid_image_content")
                components.add(identifier)
                component_tables.add(table)
            frame_type = marker
        elif marker == 0xdb:
            index = 0
            while index < len(body):
                precision, table = body[index] >> 4, body[index] & 15
                if precision > 1 or table > 3:
                    raise AssetError("invalid_image_content")
                index += 1 + 64 * (precision + 1)
                quantization.add(table)
            if not body or index != len(body):
                raise AssetError("invalid_image_content")
        elif marker == 0xc4:
            index = 0
            while index < len(body):
                if index + 17 > len(body) or body[index] >> 4 > 1 or body[index] & 15 > 3:
                    raise AssetError("invalid_image_content")
                counts = body[index + 1:index + 17]
                symbols = sum(counts)
                if not symbols or symbols > 256:
                    raise AssetError("invalid_image_content")
                available = 1
                for count in counts:
                    available = available * 2 - count
                    if available < 0:
                        raise AssetError("invalid_image_content")
                huffman.add(body[index])
                index += 17 + symbols
            if index != len(body):
                raise AssetError("invalid_image_content")
        elif marker == 0xda:
            if not width or not component_tables <= quantization or not huffman or not body:
                raise AssetError("invalid_image_content")
            count = body[0]
            if not 1 <= count <= len(components) or len(body) != 1 + count * 2 + 3:
                raise AssetError("invalid_image_content")
            scan_components = set()
            first, last, approximation = body[-3:]
            if frame_type == 0xc0 and (first, last, approximation) != (0, 63, 0):
                raise AssetError("invalid_image_content")
            if (first > last or last > 63 or (first == 0 and frame_type == 0xc2 and last != 0)
                    or (first > 0 and count != 1) or approximation >> 4 > 13 or approximation & 15 > 13
                    or (approximation >> 4 and approximation >> 4 != (approximation & 15) + 1)):
                raise AssetError("invalid_image_content")
            for index in range(1, 1 + count * 2, 2):
                identifier, tables = body[index:index + 2]
                if identifier not in components or identifier in scan_components or tables >> 4 > 3 or tables & 15 > 3:
                    raise AssetError("invalid_image_content")
                scan_components.add(identifier)
                if first == 0 and not approximation >> 4 and tables >> 4 not in huffman:
                    raise AssetError("invalid_image_content")
                if last > 0 and 0x10 + (tables & 15) not in huffman:
                    raise AssetError("invalid_image_content")
            start = offset
            while offset < len(data):
                if data[offset] != 0xff:
                    offset += 1
                    continue
                following = offset + 1
                while following < len(data) and data[following] == 0xff:
                    following += 1
                if following >= len(data):
                    raise AssetError("invalid_image_content")
                if data[following] == 0 or 0xd0 <= data[following] <= 0xd7:
                    offset = following + 1
                    continue
                break
            if offset == start:
                raise AssetError("invalid_image_content")
            scans += 1
        elif marker == 0xdd:
            if len(body) != 2:
                raise AssetError("invalid_image_content")
        elif not (0xe0 <= marker <= 0xef or marker == 0xfe):
            raise AssetError("unsupported_image_format")
    raise AssetError("invalid_image_content")


def _image(data, maximum):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png(data, maximum)
    if data.startswith(b"\xff\xd8"):
        return _jpeg(data, maximum)
    raise AssetError("unsupported_image_format")


class AssetStore:
    def __init__(self, store, config, *, max_file_bytes=20_000_000,
                 max_total_bytes=200_000_000, max_assets=1000, max_group_assets=10,
                 max_image_pixels=20_000_000, download_timeout=60):
        values = (max_file_bytes, max_total_bytes, max_assets, max_group_assets, max_image_pixels)
        if any(type(value) is not int or value < 1 for value in values) or max_file_bytes > 20_000_000:
            raise AssetError("invalid_asset_limits")
        if max_total_bytes < max_file_bytes or not 1 <= download_timeout <= 300:
            raise AssetError("invalid_asset_limits")
        self.store, self.config = store, config
        self.max_file_bytes, self.max_total_bytes = max_file_bytes, max_total_bytes
        self.max_assets, self.max_group_assets = max_assets, max_group_assets
        self.max_image_pixels, self.download_timeout = max_image_pixels, download_timeout
        data = _private_directory(config.data_dir)
        protected = (config.codex_home, Path.home() / ".codex", Path.home() / ".config",
                     Path.home() / ".ssh", Path.home() / "personal-agent",
                     Path("/var/lib/kakao-radar"), Path("/etc/kakao-radar"))
        if data in {Path("/"), Path.home(), config.cwd} or any(data.is_relative_to(Path(p).resolve()) for p in protected):
            raise AssetError("unsafe_asset_directory")
        self.directory = data / "assets"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        _private_directory(self.directory)
        with store.transaction():
            store.conn.execute("""CREATE TABLE IF NOT EXISTS assets(
                asset_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL, media_group_id TEXT,
                kind TEXT NOT NULL CHECK(kind IN ('image','document','audio','video')),
                file_id TEXT NOT NULL, file_unique_id TEXT, original_name TEXT,
                declared_mime TEXT, caption TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('downloading','stored','failed')),
                path TEXT, sha256 TEXT, size_bytes INTEGER NOT NULL DEFAULT 0,
                reserved_bytes INTEGER NOT NULL DEFAULT 0, mime_type TEXT,
                image_width INTEGER, image_height INTEGER, error_code TEXT,
                created_at TEXT NOT NULL, finished_at TEXT,
                UNIQUE(chat_id,telegram_message_id))""")
            store.conn.execute("CREATE INDEX IF NOT EXISTS assets_owner ON assets(chat_id,user_id,asset_id)")
            store.conn.execute("""CREATE TABLE IF NOT EXISTS asset_events(
                event_id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(asset_id),
                event_type TEXT NOT NULL, created_at TEXT NOT NULL)""")

    def _authorize(self, chat, user):
        if (type(chat) is not int or type(user) is not int or user <= 0 or chat != user
                or chat != self.config.chat_id or user != self.config.user_id):
            raise AssetError("asset_not_authorized")

    def _record(self, row):
        record = dict(row)
        record.pop("reserved_bytes", None)
        record["can_local_image"] = record["state"] == "stored" and record["kind"] == "image"
        if record["state"] == "failed":
            record["guidance"] = "첨부 저장 실패. 파일 형식/크기와 저장 공간을 확인한 뒤 새 메시지로 보내주세요."
        elif record["state"] == "downloading":
            record["guidance"] = "첨부 저장 중이거나 이전 수신이 중단됐습니다. 자동으로 다시 다운로드하지 않습니다."
        elif record["kind"] == "image":
            record["guidance"] = "이미지 저장 완료. 선택한 세션에 캡션과 이미지를 전달할 수 있습니다."
        elif record["kind"] == "document":
            record["guidance"] = "문서 저장 완료. 자동으로 Codex에 전달하거나 내용을 추출하지 않습니다."
        else:
            record["guidance"] = "음성/영상 저장 완료. 전사·프레임 추출·Codex 직접 입력은 아직 지원하지 않습니다."
        return record

    def get(self, asset_id, chat, user):
        self._authorize(chat, user)
        row = self.store.conn.execute("SELECT * FROM assets WHERE asset_id=? AND chat_id=? AND user_id=?",
                                      (asset_id, chat, user)).fetchone()
        if not row:
            raise AssetError("asset_not_owned")
        return self._record(row)

    def list(self, chat, user, limit=20, *, before_asset_id=None):
        self._authorize(chat, user)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AssetError("invalid_asset_list_limit")
        if before_asset_id is not None and (type(before_asset_id) is not int or before_asset_id < 1):
            raise AssetError("invalid_asset_list_cursor")
        sql = "SELECT * FROM assets WHERE chat_id=? AND user_id=?"
        args = [chat, user]
        if before_asset_id is not None:
            sql += " AND asset_id<?"
            args.append(before_asset_id)
        rows = self.store.conn.execute(sql + " ORDER BY asset_id DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._record(row) for row in rows]

    def _event(self, asset_id, event):
        self.store.conn.execute("INSERT INTO asset_events(asset_id,event_type,created_at) VALUES(?,?,?)",
                                (asset_id, event, now()))

    def _message(self, message, chat, user):
        self._authorize(chat, user)
        if not isinstance(message, dict):
            raise AssetError("invalid_asset_message")
        sender, target = message.get("from", {}), message.get("chat", {})
        if (not isinstance(sender, dict) or not isinstance(target, dict)
                or type(sender.get("id")) is not int or sender["id"] != user
                or type(target.get("id")) is not int or target["id"] != chat
                or target.get("type") != "private" or sender.get("is_bot", False)
                or any(key in message for key in ("forward_origin", "forward_from", "forward_sender_name", "sender_chat"))):
            raise AssetError("asset_not_authorized")
        message_id = message.get("message_id")
        if type(message_id) is not int or message_id <= 0:
            raise AssetError("invalid_asset_message")
        kinds = [key for key in ("photo", "document", "audio", "voice", "video", "video_note", "animation") if key in message]
        if len(kinds) != 1:
            raise AssetError("unsupported_attachment")
        key = kinds[0]
        media = message[key]
        if key == "photo":
            if not isinstance(media, list) or not 1 <= len(media) <= 10:
                raise AssetError("invalid_asset_message")
            for item in media:
                if (not isinstance(item, dict) or any(type(item.get(field)) is not int or item[field] < 1 for field in ("width", "height"))):
                    raise AssetError("invalid_asset_message")
            media = max(media, key=lambda item: item["width"] * item["height"])
        if (not isinstance(media, dict) or not isinstance(media.get("file_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", media["file_id"])):
            raise AssetError("invalid_file_id")
        kind = {"photo": "image", "document": "document", "voice": "audio", "audio": "audio"}.get(key, "video")
        caption = message.get("caption", "")
        group = message.get("media_group_id")
        if not isinstance(caption, str) or len(caption) > 4096 or (group is not None and (not isinstance(group, str) or len(group) > 128)):
            raise AssetError("invalid_asset_metadata")
        for field in ("file_name", "mime_type", "file_unique_id"):
            value = media.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 1024):
                raise AssetError("invalid_asset_metadata")
        self._size(media.get("file_size"))
        return message_id, kind, media, caption, group

    def _size(self, value):
        if value is not None and (type(value) is not int or value < 0):
            raise AssetError("invalid_asset_size")
        if value is not None and value > self.max_file_bytes:
            raise AssetError("asset_too_large")
        return value

    def _classify(self, data, kind, declared_mime):
        if kind == "image":
            return (kind, *_image(data, self.max_image_pixels))
        # Image documents stay document-only, even when their pixels are valid.
        if kind == "document":
            if data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8")) or (declared_mime or "").startswith("image/"):
                mime, suffix, width, height = _image(data, self.max_image_pixels)
                return kind, mime, suffix, width, height
            for signature, mime, suffix in (
                (b"%PDF-", "application/pdf", ".pdf"),
                (b"PK\x03\x04", "application/zip", ".zip"),
                (b"PK\x05\x06", "application/zip", ".zip"),
                (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage", ".doc"),
                (b"{\\rtf", "application/rtf", ".rtf")):
                if data.startswith(signature):
                    return kind, mime, suffix, None, None
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                raise AssetError("unsupported_document_format") from None
            if any(byte < 32 and byte not in {9, 10, 13} for byte in data):
                raise AssetError("unsupported_document_format")
            return kind, "text/plain", ".txt", None, None
        return kind, "application/octet-stream", ".bin", None, None

    def _read_file(self, path, *, identity=None):
        _private_directory(self.directory)
        if not isinstance(path, (str, Path)):
            raise AssetError("unsafe_asset_path")
        path = Path(path)
        pattern = (r"\.receive-[A-Za-z0-9_-]+\.part" if identity is not None else
                   r"asset-[a-f0-9]{32}\.(?:png|jpg|pdf|zip|doc|rtf|txt|bin)")
        if (not path.is_absolute() or path.parent != self.directory
                or not re.fullmatch(pattern, path.name)):
            raise AssetError("unsafe_asset_path")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or info.st_size > self.max_file_bytes
                    or (identity is not None and (info.st_dev, info.st_ino) != identity)):
                raise AssetError("unsafe_asset_file")
            digest, pieces, size = hashlib.sha256(), [], 0
            while chunk := handle.read(min(65536, self.max_file_bytes - size + 1)):
                size += len(chunk)
                if size > self.max_file_bytes:
                    raise AssetError("asset_too_large")
                digest.update(chunk)
                pieces.append(chunk)
            if not size:
                raise AssetError("empty_asset")
            return b"".join(pieces), digest.hexdigest(), size

    async def receive(self, message, transport, chat, user):
        message_id, kind, media, caption, group = self._message(message, chat, user)
        with self.store.transaction():
            existing = self.store.conn.execute("SELECT * FROM assets WHERE chat_id=? AND telegram_message_id=?",
                                               (chat, message_id)).fetchone()
            if existing:
                if existing["user_id"] != user:
                    raise AssetError("asset_not_owned")
                if existing["file_id"] != media["file_id"] or existing["kind"] != kind:
                    raise AssetError("asset_message_already_used")
                return self._record(existing)
            count, used = self.store.conn.execute("SELECT count(*),coalesce(sum(size_bytes+reserved_bytes),0) FROM assets").fetchone()
            if count >= self.max_assets or used + self.max_file_bytes > self.max_total_bytes:
                raise AssetError("asset_storage_limit")
            if group is not None and self.store.conn.execute(
                    "SELECT count(*) FROM assets WHERE chat_id=? AND media_group_id=?", (chat, group)).fetchone()[0] >= self.max_group_assets:
                raise AssetError("asset_group_limit")
            _private_directory(self.directory)
            if shutil.disk_usage(self.directory).free < self.max_file_bytes + self.config.min_disk_bytes:
                raise AssetError("asset_disk_full")
            cursor = self.store.conn.execute("""INSERT INTO assets(
                chat_id,user_id,telegram_message_id,media_group_id,kind,file_id,file_unique_id,
                original_name,declared_mime,caption,state,reserved_bytes,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'downloading',?,?)""",
                (chat, user, message_id, group, kind, media["file_id"], media.get("file_unique_id"),
                 media.get("file_name"), media.get("mime_type"), caption, self.max_file_bytes, now()))
            asset_id = cursor.lastrowid
            self._event(asset_id, "receive_claimed")
        temporary, published, identity = None, None, None
        try:
            async with asyncio.timeout(self.download_timeout):
                metadata = await transport.get_file(media["file_id"])
                if not isinstance(metadata, dict) or metadata.get("file_id", media["file_id"]) != media["file_id"]:
                    raise AssetError("invalid_telegram_file_metadata")
                file_path = validate_telegram_file_path(metadata.get("file_path"))
                announced = self._size(metadata.get("file_size"))
                _private_directory(self.directory)
                fd, name = tempfile.mkstemp(prefix=".receive-", suffix=".part", dir=self.directory)
                temporary = Path(name)
                os.fchmod(fd, 0o600)
                info = os.fstat(fd)
                identity = (info.st_dev, info.st_ino)
                os.close(fd)
                downloaded = await transport.download_file(file_path, temporary, self.max_file_bytes,
                                                           reserved_identity=identity)
                data, digest, size = await asyncio.to_thread(self._read_file, temporary, identity=identity)
                if type(downloaded) is not int or downloaded != size or any(
                        value is not None and value != size for value in (announced, media.get("file_size"))):
                    raise AssetError("asset_size_mismatch")
                kind, mime, suffix, width, height = await asyncio.to_thread(
                    self._classify, data, kind, media.get("mime_type"))
                published = self.directory / ("asset-" + uuid.uuid4().hex + suffix)
                with temporary.open("rb") as handle:
                    os.fsync(handle.fileno())
                os.link(temporary, published, follow_symlinks=False)
                temporary.unlink()
                temporary = None
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                with self.store.transaction():
                    self.store.conn.execute("""UPDATE assets SET state='stored',path=?,sha256=?,
                        size_bytes=?,reserved_bytes=0,mime_type=?,image_width=?,image_height=?,finished_at=?
                        WHERE asset_id=? AND state='downloading'""",
                        (str(published), digest, size, mime, width, height, now(), asset_id))
                    self._event(asset_id, "stored")
        except BaseException as exc:
            for path in (temporary, published):
                if path is not None:
                    try:
                        info = path.lstat()
                        if not stat.S_ISLNK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                            path.unlink()
                    except OSError:
                        pass
            # No raw exception message is recorded: it may contain a bot-token URL.
            public = {"invalid_telegram_file_metadata", "invalid_telegram_file_path", "invalid_asset_size",
                      "asset_too_large", "unsafe_asset_directory", "private_asset_directory_required",
                      "unsafe_asset_path", "unsafe_asset_file", "empty_asset", "asset_size_mismatch",
                      "image_dimensions_exceeded", "unsupported_image_format", "invalid_image_content",
                      "image_decompression_exceeded", "unsupported_document_format"}
            code = str(exc) if isinstance(exc, AssetError) and str(exc) in public else "asset_download_failed"
            with self.store.transaction():
                self.store.conn.execute("UPDATE assets SET state='failed',reserved_bytes=0,error_code=?,finished_at=? WHERE asset_id=?",
                                        (code, now(), asset_id))
                self._event(asset_id, "receive_failed")
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
        return self.get(asset_id, chat, user)

    def local_image(self, asset_id, chat, user):
        return self._validate_image_record(self.get(asset_id, chat, user))

    async def local_image_async(self, asset_id, chat, user):
        """Read SQLite ownership on the caller thread; validate file content off-loop."""
        record = self.get(asset_id, chat, user)
        return await asyncio.to_thread(self._validate_image_record, record)

    async def validated_data(self, asset_id, chat, user):
        record = self.get(asset_id, chat, user)
        if record["state"] != "stored":
            raise AssetError("asset_not_stored")
        data, digest, size = await asyncio.to_thread(self._read_file, record["path"])
        if digest != record["sha256"] or size != record["size_bytes"]:
            raise AssetError("asset_hash_mismatch")
        return record, data

    def import_image(self, source_asset, slot, data, chat, user):
        """Publish a generated frame/image, preserving the original source asset."""
        self.get(source_asset, chat, user)
        if type(slot) is not int or not 1 <= slot <= 3 or not isinstance(data, bytes) or len(data) > 2000000:
            raise AssetError("invalid_derived_image")
        mime, suffix, width, height = _image(data, self.max_image_pixels)
        file_id = f"derived_{source_asset}_{slot}"
        existing = self.store.conn.execute("SELECT asset_id FROM assets WHERE chat_id=? AND user_id=? AND file_id=?", (chat, user, file_id)).fetchone()
        if existing:
            return existing[0]
        count, used = self.store.conn.execute("SELECT count(*),coalesce(sum(size_bytes+reserved_bytes),0) FROM assets").fetchone()
        if count >= self.max_assets or used + len(data) > self.max_total_bytes:
            raise AssetError("asset_storage_limit")
        _private_directory(self.directory)
        if shutil.disk_usage(self.directory).free < len(data) + self.config.min_disk_bytes:
            raise AssetError("asset_disk_full")
        path = self.directory / ("asset-" + uuid.uuid4().hex + suffix)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            cursor = self.store.conn.execute("""INSERT INTO assets(chat_id,user_id,telegram_message_id,kind,
                file_id,original_name,caption,state,path,sha256,size_bytes,mime_type,image_width,image_height,created_at,finished_at)
                VALUES(?,?,?,'image',?,?,'','stored',?,?,?,?,?,?,?,?)""",
                (chat, user, -(source_asset * 10 + slot), file_id, f"자료{source_asset} 이미지{slot}", str(path),
                 hashlib.sha256(data).hexdigest(), len(data), mime, width, height, now(), now()))
            return cursor.lastrowid
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def _validate_image_record(self, record):
        if not record["can_local_image"]:
            raise AssetError("asset_not_local_image")
        try:
            data, digest, size = self._read_file(record["path"])
        except OSError:
            raise AssetError("asset_file_unavailable") from None
        if digest != record["sha256"] or size != record["size_bytes"]:
            raise AssetError("asset_hash_mismatch")
        mime, _, width, height = _image(data, self.max_image_pixels)
        if (mime, width, height) != (record["mime_type"], record["image_width"], record["image_height"]):
            raise AssetError("invalid_image_content")
        return {"type": "localImage", "path": record["path"]}

    def validate_image_path(self, path, chat, user):
        """Validate an owned stored image path and return the absolute path string."""
        self._authorize(chat, user)
        row = self.store.conn.execute(
            "SELECT asset_id FROM assets WHERE path=? AND chat_id=? AND user_id=? AND state='stored'",
            (str(path), chat, user)).fetchone()
        if not row:
            raise AssetError("asset_not_owned")
        return self.local_image(row[0], chat, user)["path"]
