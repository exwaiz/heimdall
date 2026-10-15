import asyncio
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from heimdall_bridge.assets import AssetError, AssetStore, validate_telegram_file_path
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store


def png(width=1, height=1, *, raster=None, corrupt_crc=False):
    def chunk(kind, body):
        crc = zlib.crc32(kind + body) & 0xffffffff
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    raster = raster if raster is not None else (b"\x00" + b"\x11\x22\x33" * width) * height
    data += chunk(b"IDAT", zlib.compress(raster)) + chunk(b"IEND", b"")
    return data[:-1] + bytes([data[-1] ^ 1]) if corrupt_crc else data


def jpeg():
    def segment(marker, body):
        return b"\xff" + bytes([marker]) + struct.pack(">H", len(body) + 2) + body

    # Valid 1x1 grayscale baseline JPEG: DC=0, EOB, then one-bit padding.
    counts = b"\x01" + b"\x00" * 15
    return (b"\xff\xd8" + segment(0xdb, b"\x00" + b"\x01" * 64)
            + segment(0xc0, b"\x08\x00\x01\x00\x01\x01\x01\x11\x00")
            + segment(0xc4, b"\x00" + counts + b"\x00" + b"\x10" + counts + b"\x00")
            + segment(0xda, b"\x01\x01\x00\x00\x3f\x00") + b"\x3f\xff\xd9")


def message(message_id=1, kind="document", *, caption="  원문\n`caption`  ", **metadata):
    file = {"file_id": f"file_{message_id}", "file_unique_id": f"unique_{message_id}", **metadata}
    if kind == "photo":
        file = [{"width": 1, "height": 1, **file}]
    return {"message_id": message_id, "chat": {"id": 7, "type": "private"},
            "from": {"id": 7, "is_bot": False}, "caption": caption, kind: file}


class FakeFiles:
    def __init__(self, data=b"plain document", *, path="documents/file_1.dat", announced=None):
        self.data, self.path, self.announced = data, path, announced
        self.get_calls, self.download_calls = [], []
        self.failure, self.ignore_limit = None, False
        self.fail_after_bytes = None
        self.reported = None
        self.blocked, self.release = None, None

    async def get_file(self, file_id):
        self.get_calls.append(file_id)
        if self.blocked:
            self.blocked.set()
            await self.release.wait()
        if self.failure:
            raise self.failure
        metadata = {"file_id": file_id, "file_path": self.path}
        if self.announced is not None:
            metadata["file_size"] = self.announced
        return metadata

    async def download_file(self, file_path, destination, max_bytes, *, reserved_identity=None):
        self.download_calls.append((file_path, destination, max_bytes))
        fd = os.open(destination, os.O_WRONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "wb") as handle:
            info = os.fstat(handle.fileno())
            if (info.st_dev, info.st_ino) != reserved_identity or info.st_mode & 0o777 != 0o600 or info.st_nlink != 1:
                raise ValueError("unsafe reservation")
            size = 0
            for offset in range(0, len(self.data), 7):
                chunk = self.data[offset:offset + 7]
                size += len(chunk)
                if not self.ignore_limit and size > max_bytes:
                    raise ValueError("too large")
                handle.write(chunk)
                if self.fail_after_bytes is not None and size >= self.fail_after_bytes:
                    raise RuntimeError("https://api.telegram.org/file/bot123:SECRET/partial")
                await asyncio.sleep(0)
        return len(self.data) if self.reported is None else self.reported


class AssetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="heimdall-assets-")
        self.root = Path(self.temp.name)
        self.config = Config(self.root, self.root / "data", self.root / "schemas", self.root / "codex",
                             user_id=7, chat_id=7, min_disk_bytes=0)
        self.store = Store(self.config.data_dir / "bridge.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.assets = AssetStore(self.store, self.config, max_file_bytes=1024, max_total_bytes=4096,
                                 max_assets=10, max_group_assets=2, max_image_pixels=1000)

    async def receive(self, msg=None, data=b"plain document", **transport_options):
        transport = FakeFiles(data, **transport_options)
        record = await self.assets.receive(msg or message(), transport, 7, 7)
        return record, transport

    async def test_streamed_storage_hash_permissions_caption_and_filename_not_used(self):
        data = b"%PDF-1.7\nopaque document"
        msg = message(file_name="../../api_bot_key", mime_type="image/svg+xml", file_size=len(data))
        # For document classification, an image MIME cannot smuggle unsupported image content.
        msg["document"]["mime_type"] = "application/pdf"
        record, transport = await self.receive(msg, data, announced=len(data))
        self.assertEqual(record["state"], "stored")
        self.assertEqual(record["caption"], msg["caption"])
        self.assertEqual(record["original_name"], "../../api_bot_key")
        self.assertEqual(record["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(record["size_bytes"], len(data))
        path = Path(record["path"])
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.parent, self.config.data_dir / "assets")
        self.assertTrue(path.name.startswith("asset-"))
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(record["can_local_image"])
        self.assertEqual(len(transport.download_calls), 1)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual([r[0] for r in self.store.conn.execute("SELECT event_type FROM asset_events")],
                         ["receive_claimed", "stored"])
        self.assertFalse(any(p.name.endswith(".part") for p in path.parent.iterdir()))

    async def test_png_and_jpeg_local_image_validation(self):
        for identifier, payload in enumerate((png(), jpeg()), 1):
            with self.subTest(format=identifier):
                record, _ = await self.receive(message(identifier, "photo"), payload)
                self.assertEqual(record["state"], "stored")
                self.assertEqual((record["kind"], record["image_width"], record["image_height"]), ("image", 1, 1))
                local = self.assets.local_image(record["asset_id"], 7, 7)
                self.assertEqual(local, {"type": "localImage", "path": record["path"]})
                self.assertEqual(await self.assets.local_image_async(record["asset_id"], 7, 7), local)
                self.assertEqual(self.assets.validate_image_path(record["path"], 7, 7), record["path"])

    async def test_largest_photo_variant_only(self):
        msg = message(kind="photo")
        msg["photo"].append({"width": 2, "height": 2, "file_id": "large"})
        record, transport = await self.receive(msg, png(2, 2))
        self.assertEqual(record["state"], "stored")
        self.assertEqual(transport.get_calls, ["large"])

    async def test_document_image_stays_store_only_and_audio_video_guidance(self):
        for identifier, (kind, payload) in enumerate((('document', png()), ('voice', b"opaque voice"),
                ('audio', b"opaque audio"), ('video', b"opaque video"), ('video_note', b"opaque video"),
                ('animation', b"opaque animation")), 1):
            record, _ = await self.receive(message(identifier, kind), payload)
            self.assertEqual(record["state"], "stored")
            self.assertFalse(record["can_local_image"])
            with self.assertRaisesRegex(AssetError, "asset_not_local_image"):
                self.assets.local_image(record["asset_id"], 7, 7)
            self.assertTrue(record["guidance"])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    async def test_duplicate_message_and_restart_do_not_download_again_or_change_caption(self):
        msg = message()
        record, transport = await self.receive(msg)
        edited = {**msg, "caption": "edited caption"}
        duplicate = await self.assets.receive(edited, transport, 7, 7)
        restarted = AssetStore(self.store, self.config)
        replay = await restarted.receive(msg, transport, 7, 7)
        self.assertEqual(duplicate, record)
        self.assertEqual(replay, record)
        self.assertEqual(transport.get_calls, ["file_1"])
        self.assertEqual(len(transport.download_calls), 1)
        self.assertEqual(len(list(self.assets.directory.iterdir())), 1)
        changed = message(file_id="different")
        with self.assertRaisesRegex(AssetError, "already_used"):
            await self.assets.receive(changed, transport, 7, 7)

    async def test_concurrent_duplicate_is_claimed_once(self):
        transport = FakeFiles()
        transport.blocked, transport.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.assets.receive(message(), transport, 7, 7))
        await transport.blocked.wait()
        duplicate = await self.assets.receive(message(), transport, 7, 7)
        self.assertEqual(duplicate["state"], "downloading")
        transport.release.set()
        result = await task
        self.assertEqual(result["state"], "stored")
        self.assertEqual(len(transport.get_calls), 1)
        self.assertEqual(len(transport.download_calls), 1)

    async def test_authorization_message_identity_and_database_owner(self):
        transport = FakeFiles()
        variants = [message(), message(), message(), message(), message()]
        variants[0]["from"]["id"] = 8
        variants[1]["chat"]["type"] = "group"
        variants[2]["forward_origin"] = {}
        variants[3]["sender_chat"] = {}
        variants[4]["from"]["is_bot"] = True
        for item in variants:
            with self.assertRaisesRegex(AssetError, "not_authorized"):
                await self.assets.receive(item, transport, 7, 7)
        self.assertEqual(transport.get_calls, [])
        record = await self.assets.receive(message(), transport, 7, 7)
        self.store.conn.execute("UPDATE assets SET user_id=8")
        with self.assertRaisesRegex(AssetError, "asset_not_owned"):
            await self.assets.receive(message(), transport, 7, 7)
        with self.assertRaisesRegex(AssetError, "asset_not_owned"):
            self.assets.get(record["asset_id"], 7, 7)
        self.assertEqual(self.assets.list(7, 7), [])
        for action in (lambda: self.assets.get(record["asset_id"], 7, 8),
                       lambda: self.assets.local_image(record["asset_id"], 7, 8)):
            with self.assertRaises(AssetError):
                action()

    async def test_file_paths_reject_urls_traversal_encoding_and_secrets(self):
        for value in ("https://example.com/a", "https://api.telegram.org/file/a", "//evil/a", "/photos/a",
                      "photos/../a", "../a", "photos/%2e%2e/a", "photos/a?token=secret", "photos/a#frag",
                      "photos\\a", "photos//a", "photos/./a", "photos/a\x00", "fileonly"):
            with self.subTest(path=value):
                with self.assertRaisesRegex(AssetError, "invalid_telegram_file_path"):
                    validate_telegram_file_path(value)
        transport = FakeFiles(path="http://127.0.0.1/auth")
        record = await self.assets.receive(message(), transport, 7, 7)
        self.assertEqual(record["state"], "failed")
        self.assertEqual(transport.download_calls, [])

    async def test_declared_getfile_and_actual_stream_limits(self):
        transport = FakeFiles()
        with self.assertRaisesRegex(AssetError, "asset_too_large"):
            await self.assets.receive(message(file_size=1025), transport, 7, 7)
        self.assertEqual(transport.get_calls, [])
        metadata_large = FakeFiles(announced=1025)
        record = await self.assets.receive(message(2), metadata_large, 7, 7)
        self.assertEqual((record["state"], record["error_code"]), ("failed", "asset_too_large"))
        self.assertEqual(metadata_large.download_calls, [])
        for identifier, dishonest in enumerate((False, True), 3):
            large = FakeFiles(b"x" * 1025)
            large.ignore_limit = dishonest
            result = await self.assets.receive(message(identifier), large, 7, 7)
            self.assertEqual(result["state"], "failed")
        self.assertEqual(list(self.assets.directory.iterdir()), [])
        self.assertEqual(self.store.conn.execute("SELECT sum(reserved_bytes) FROM assets").fetchone()[0], 0)

    async def test_size_mismatch_empty_and_invalid_image_fail_cleanly(self):
        cases = ((message(1, "photo"), b"not image", None),
                 (message(2, "photo"), png(corrupt_crc=True), None),
                 (message(3, "photo"), jpeg()[:-2], None),
                 (message(4, "photo"), png() + b"trailing", None),
                 (message(5), b"", None),
                 (message(6), b"actual", 7),
                 (message(7, file_size=7), b"actual", None),
                 (message(8, "photo"), png(1001, 1), None),
                 (message(9, "photo"), png(raster=b"\x00" + b"x" * 100), None))
        for msg, data, announced in cases:
            record, _ = await self.receive(msg, data, announced=announced)
            self.assertEqual(record["state"], "failed")
            self.assertIsNone(record["path"])
        self.assertEqual(list(self.assets.directory.iterdir()), [])

    async def test_transport_error_token_not_saved_or_replayed(self):
        transport = FakeFiles()
        transport.failure = RuntimeError("https://api.telegram.org/file/bot12345:SECRET_TOKEN/photos/a")
        record = await self.assets.receive(message(), transport, 7, 7)
        self.assertEqual(record["error_code"], "asset_download_failed")
        self.assertNotIn("SECRET_TOKEN", str(record))
        self.assertNotIn("SECRET_TOKEN", str(self.store.conn.execute("SELECT * FROM asset_events").fetchall()))
        await self.assets.receive(message(), transport, 7, 7)
        self.assertEqual(len(transport.get_calls), 1)

    async def test_partial_download_timeout_and_false_byte_count_fail_cleanly(self):
        partial = FakeFiles(b"partial download")
        partial.fail_after_bytes = 1
        record = await self.assets.receive(message(), partial, 7, 7)
        self.assertEqual(record["state"], "failed")
        self.assertNotIn("SECRET", str(record))
        false_count = FakeFiles()
        false_count.reported = 999
        record = await self.assets.receive(message(2), false_count, 7, 7)
        self.assertEqual(record["error_code"], "asset_size_mismatch")
        blocked = FakeFiles()
        blocked.blocked, blocked.release = asyncio.Event(), asyncio.Event()
        self.assets.download_timeout = 0.01
        record = await self.assets.receive(message(3), blocked, 7, 7)
        self.assertEqual(record["state"], "failed")
        self.assertEqual(list(self.assets.directory.iterdir()), [])

    async def test_large_validation_does_not_block_callbacks_or_use_sqlite_in_thread(self):
        entered, release = threading.Event(), threading.Event()
        original = self.assets._classify

        def slow_classification(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test callback failed to release validation")
            return original(*args)

        with patch.object(self.assets, "_classify", side_effect=slow_classification):
            task = asyncio.create_task(self.assets.receive(message(kind="photo"), FakeFiles(png()), 7, 7))
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(entered.is_set())
                self.assertFalse(task.done())
                # An owner query/callback still runs while validation waits off-loop.
                self.assertEqual(self.assets.list(7, 7)[0]["state"], "downloading")
            finally:
                release.set()
            record = await task
        self.assertEqual(record["state"], "stored")
        self.assertEqual(await self.assets.local_image_async(record["asset_id"], 7, 7),
                         {"type": "localImage", "path": record["path"]})

    async def test_publication_refuses_existing_file_without_overwrite(self):
        stem = "1" * 32
        existing = self.assets.directory / ("asset-" + stem + ".txt")
        existing.write_bytes(b"preserve this artifact")
        existing.chmod(0o600)
        with patch("heimdall_bridge.assets.uuid.uuid4") as random:
            random.return_value.hex = stem
            record, _ = await self.receive()
        self.assertEqual(record["state"], "failed")
        self.assertEqual(existing.read_bytes(), b"preserve this artifact")
        self.assertEqual(list(self.assets.directory.iterdir()), [existing])

    async def test_directory_swap_during_metadata_wait_does_not_write_outside_assets(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)

        class Swap(FakeFiles):
            async def get_file(inner, file_id):
                self.assets.directory.rmdir()
                self.assets.directory.symlink_to(outside, target_is_directory=True)
                return await super().get_file(file_id)

        transport = Swap()
        record = await self.assets.receive(message(), transport, 7, 7)
        self.assertEqual(record["state"], "failed")
        self.assertEqual(transport.download_calls, [])
        self.assertEqual(list(outside.iterdir()), [])

    async def test_main_transport_contract_with_mock_http_only(self):
        import httpx
        from heimdall_bridge.transport import TelegramTransport

        payload = png()
        calls = []

        def handler(request):
            self.assertEqual((request.url.scheme, request.url.host), ("https", "api.telegram.org"))
            calls.append(request.method)
            if request.method == "POST":
                return httpx.Response(200, json={"ok": True, "result": {
                    "file_id": "file_1", "file_path": "photos/file_1.png", "file_size": len(payload)}})
            return httpx.Response(200, content=payload)

        transport = TelegramTransport("fake-token-for-unit-test")
        await transport.client.aclose()
        transport.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        try:
            record = await self.assets.receive(message(kind="photo"), transport, 7, 7)
        finally:
            await transport.close()
        self.assertEqual(record["state"], "stored")
        self.assertEqual(calls, ["POST", "GET"])
        self.assertEqual(self.assets.local_image(record["asset_id"], 7, 7)["path"], record["path"])

    async def test_total_count_and_group_limits_and_disk_guard(self):
        assets = AssetStore(self.store, self.config, max_file_bytes=16, max_total_bytes=32, max_assets=2)
        for identifier in (1, 2):
            self.assertEqual((await assets.receive(message(identifier), FakeFiles(b"x" * 16), 7, 7))["state"], "stored")
        rejected = FakeFiles()
        with self.assertRaisesRegex(AssetError, "storage_limit"):
            await assets.receive(message(3), rejected, 7, 7)
        self.assertEqual(rejected.get_calls, [])
        # A distinct group is bounded independently of the global byte budget.
        for identifier in (3, 4):
            msg = message(identifier)
            msg["media_group_id"] = "group"
            await self.assets.receive(msg, FakeFiles(), 7, 7)
        msg = message(5)
        msg["media_group_id"] = "group"
        with self.assertRaisesRegex(AssetError, "group_limit"):
            await self.assets.receive(msg, rejected, 7, 7)
        with patch("heimdall_bridge.assets.shutil.disk_usage", return_value=type(shutil.disk_usage(self.root))(0, 0, 0)):
            with self.assertRaisesRegex(AssetError, "asset_disk_full"):
                await self.assets.receive(message(6), rejected, 7, 7)

    async def test_pending_reservation_counts_toward_total(self):
        assets = AssetStore(self.store, self.config, max_file_bytes=1024, max_total_bytes=1024)
        transport = FakeFiles()
        transport.blocked, transport.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(assets.receive(message(), transport, 7, 7))
        await transport.blocked.wait()
        with self.assertRaisesRegex(AssetError, "storage_limit"):
            await assets.receive(message(2), FakeFiles(), 7, 7)
        transport.release.set()
        await task

    async def test_cancellation_marks_failed_and_clears_reservation(self):
        transport = FakeFiles()
        transport.blocked, transport.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.assets.receive(message(), transport, 7, 7))
        await transport.blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        record = self.assets.list(7, 7)[0]
        self.assertEqual(record["state"], "failed")
        self.assertEqual(self.store.conn.execute("SELECT reserved_bytes FROM assets").fetchone()[0], 0)

    async def test_worker_rejects_tampering_symlinks_hardlinks_and_foreign_paths(self):
        record, _ = await self.receive(message(kind="photo"), png())
        path = Path(record["path"])
        path.write_bytes(png(2, 1))
        with self.assertRaisesRegex(AssetError, "hash_mismatch"):
            self.assets.local_image(record["asset_id"], 7, 7)
        path.write_bytes(png())
        path.chmod(0o644)
        with self.assertRaisesRegex(AssetError, "unsafe_asset_file"):
            self.assets.local_image(record["asset_id"], 7, 7)
        path.chmod(0o600)
        hardlink = self.root / "copy"
        os.link(path, hardlink)
        with self.assertRaisesRegex(AssetError, "unsafe_asset_file"):
            self.assets.local_image(record["asset_id"], 7, 7)
        hardlink.unlink()
        original = path.with_name("original")
        path.rename(original)
        path.symlink_to(original)
        with self.assertRaisesRegex(AssetError, "asset_file_unavailable"):
            self.assets.local_image(record["asset_id"], 7, 7)
        with self.assertRaisesRegex(AssetError, "asset_not_owned"):
            self.assets.validate_image_path(original, 7, 7)
        self.store.conn.execute("UPDATE assets SET path=?", (str(original),))
        with self.assertRaisesRegex(AssetError, "unsafe_asset_path"):
            self.assets.local_image(record["asset_id"], 7, 7)

    async def test_worker_cannot_use_temporary_or_missing_database_path(self):
        record, _ = await self.receive(message(kind="photo"), png())
        published = Path(record["path"])
        temporary = published.with_name(".receive-abc123.part")
        published.rename(temporary)
        self.store.conn.execute("UPDATE assets SET path=?", (str(temporary),))
        with self.assertRaisesRegex(AssetError, "unsafe_asset_path"):
            self.assets.local_image(record["asset_id"], 7, 7)
        self.store.conn.execute("UPDATE assets SET path=NULL")
        with self.assertRaisesRegex(AssetError, "unsafe_asset_path"):
            self.assets.local_image(record["asset_id"], 7, 7)

    async def test_list_is_bounded_and_owner_scoped(self):
        for identifier in (1, 2, 3):
            await self.receive(message(identifier))
        self.assertEqual([r["asset_id"] for r in self.assets.list(7, 7, 2)], [3, 2])
        self.assertEqual([r["asset_id"] for r in self.assets.list(7, 7, 2, before_asset_id=3)], [2, 1])
        for limit in (0, 101, True, "2"):
            with self.assertRaises(AssetError):
                self.assets.list(7, 7, limit)

    async def test_archive_not_extracted_and_unsupported_executable_rejected(self):
        archive, _ = await self.receive(message(1, file_name="../../evil.zip"), b"PK\x03\x04opaque archive")
        self.assertEqual(archive["mime_type"], "application/zip")
        rejected, _ = await self.receive(message(2, file_name="report.pdf"), b"MZ\x00\x00binary")
        self.assertEqual(rejected["state"], "failed")
        self.assertEqual(len(list(self.assets.directory.iterdir())), 1)

    def test_private_symlink_and_protected_directory_denials(self):
        self.assets.directory.chmod(0o755)
        with self.assertRaisesRegex(AssetError, "private_asset_directory"):
            AssetStore(self.store, self.config)
        self.assets.directory.chmod(0o700)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.assets.directory.rmdir()
        self.assets.directory.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(AssetError, "unsafe_asset_directory"):
            AssetStore(self.store, self.config)
        self.assertEqual(list(elsewhere.iterdir()), [])
        with self.assertRaisesRegex(AssetError, "unsafe_asset_directory"):
            AssetStore(self.store, replace(self.config, codex_home=self.config.data_dir))


if __name__ == "__main__":
    unittest.main()
