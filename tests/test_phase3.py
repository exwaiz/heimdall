import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.fakes import FakeCodexAdapter
from heimdall_bridge.transport import FakeTelegramTransport
from test_assets import FakeFiles, message, png

ROOT = Path(__file__).resolve().parents[1]


class FilesTransport(FakeTelegramTransport, FakeFiles):
    def __init__(self, data):
        FakeTelegramTransport.__init__(self)
        FakeFiles.__init__(self, data)


class Phase3Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="heimdall-phase3-test-")
        self.config = replace(Config.load(ROOT / "config.toml", live=False),
            data_dir=Path(self.temp.name), user_id=7, chat_id=7)
        self.store = Store(self.config.data_dir / "bridge.sqlite3")
        self.adapter = FakeCodexAdapter(self.config.cwd)
        self.transport = FilesTransport(png())
        self.bridge = Bridge(self.config, self.store, self.adapter, self.transport, conflict_check=lambda *_: False)
        await self.bridge.handle(update(1, "/new Test"))

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_image_preserves_caption_once_and_freezes_assets(self):
        item = {"update_id": 2, "message": message(2, "photo")}
        await self.bridge.handle(item)
        await self.bridge.handle(item)
        await self.bridge.handle({**item, "update_id": 20})
        self.assertEqual(len(self.store.jobs()), 1)
        job = self.store.job(1)
        self.assertEqual(job["prompt"], item["message"]["caption"])
        self.assertEqual(json.loads(job["image_assets"]), [1])
        await self.bridge.handle(update(3, "/new Other"))
        seen = []
        original = self.adapter.run_turn
        async def run(*args, **kwargs):
            seen.append(kwargs)
            return await original(*args, **kwargs)
        self.adapter.run_turn = run
        await self.bridge.work_once()
        self.assertEqual(self.adapter.calls[0][0], "fake-thread-1")
        self.assertEqual(self.store.job(1)["state"], "completed")
        self.assertTrue(seen[0]["image_paths"][0].endswith(".png"))
        self.assertEqual(len(self.transport.download_calls), 1)

    async def test_document_audio_video_store_without_codex_job(self):
        for number, kind in enumerate(("document", "audio", "voice", "video"), 2):
            await self.bridge.handle({"update_id": number, "message": message(number, kind)})
        self.assertFalse(self.store.jobs())
        self.assertEqual(len(self.bridge.assets.list(7, 7)), 4)
        self.assertIn("#1", await self.bridge.command("/assets", 7, 7))
        self.assertIn("SHA-256", await self.bridge.command("/asset 1", 7, 7))

    async def test_mutated_file_is_blocked_before_turn(self):
        await self.bridge.handle({"update_id": 2, "message": message(2, "photo")})
        path = self.bridge.assets.get(1, 7, 7)["path"]
        Path(path).write_bytes(b"tampered fixture")
        await self.bridge.work_once()
        self.assertFalse(self.adapter.calls)
        self.assertEqual(self.store.job(1)["state"], "failed")
        self.assertEqual(self.store.job(1)["error_code"], "image_asset_unavailable_or_changed")

    async def test_unauthorized_attachment_never_downloaded(self):
        item = message(2, "photo")
        item["from"]["id"] = 8
        await self.bridge.handle({"update_id": 2, "message": item})
        self.assertFalse(self.transport.get_calls)
        self.assertFalse(self.bridge.assets.list(7, 7))

    async def test_selection_during_download_does_not_retarget_or_expand_mode(self):
        self.transport.blocked = asyncio.Event()
        self.transport.release = asyncio.Event()
        task = asyncio.create_task(self.bridge.handle({"update_id": 2, "message": message(2, "photo")}))
        await self.transport.blocked.wait()
        await self.bridge.handle(update(3, "/new B"))
        await self.bridge.handle(update(4, "/mode write"))
        self.transport.release.set()
        await task
        job = self.store.job(1)
        self.assertEqual(job["thread_id"], "fake-thread-1")
        self.assertEqual(job["permission_mode"], "readonly")

    async def test_image_repeat_retry_preserves_asset_ids(self):
        await self.bridge.handle({"update_id": 2, "message": message(2, "photo")})
        await self.bridge.work_once()
        await self.bridge.command("/retry 1 repeat", 7, 7, 90)
        self.assertEqual(self.store.job(2)["image_assets"], self.store.job(1)["image_assets"])

    async def test_duplicate_receipt_after_selection_does_not_mutate_original_job(self):
        item = {"update_id": 2, "message": message(2, "photo")}
        await self.bridge.handle(item)
        original = self.store.job(1)
        await self.bridge.handle(update(3, "/new B"))
        await self.bridge.handle(update(4, "/mode write"))
        await self.bridge.handle({**item, "update_id": 20})
        self.assertEqual(self.store.job(1), original)
