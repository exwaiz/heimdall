import asyncio
from contextlib import closing
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

import httpx

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.fakes import FakeCodexAdapter
from heimdall_bridge.outgoing import prepare_images, read_photo, queue_images
from heimdall_bridge.knowledge_backup import backup_bundle, restore_bundle
from heimdall_bridge.transport import TelegramTransport, FakeTelegramTransport, DeliveryUncertain, TransportError, deliver
from test_assets import png, jpeg

ROOT = Path(__file__).resolve().parents[1]


class OutgoingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='heimdall-outgoing-')
        self.root = Path(self.temp.name)
        self.config = replace(Config.load(ROOT / 'config.toml', live=False), cwd=self.root,
            data_dir=self.root / 'private', chat_id=7, user_id=7)
        self.store = Store(self.config.data_dir / 'bridge.sqlite3')
        self.adapter = FakeCodexAdapter(self.config.cwd)
        self.transport = FakeTelegramTransport()
        self.bridge = Bridge(self.config, self.store, self.adapter, self.transport, conflict_check=lambda *_:False)
        await self.bridge.handle(update(1, '/new Images'))
        self.image = self.root / 'screen shot.png'
        self.image.write_bytes(png(20, 10))
        self.text = f'Screenshot: ![Dashboard](<{self.image}>)'

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_snapshot_and_content_validation(self):
        data, mime, digest = read_photo(self.image, self.config)
        self.assertEqual(data, self.image.read_bytes())
        self.assertEqual(mime, 'image/png')
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        bad = self.root / 'bad.png'
        bad.write_bytes(b'not an image')
        with self.assertRaises(ValueError): read_photo(bad, self.config)
        jpg = self.root / 'fixture.jpg'
        jpg.write_bytes(jpeg())
        self.assertEqual(read_photo(jpg, self.config)[1], 'image/jpeg')

    def test_protected_paths_symlink_hardlink_fifo_and_traversal_denied(self):
        targets = [self.config.data_dir / 'secret.png', self.root / '.ssh' / 'secret.png',
                   self.root / 'personal-agent' / 'secret.png']
        for target in targets:
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(png())
        link = self.root / 'linked.png'
        link.symlink_to(self.image)
        targets += [link, self.root / 'sub' / '..' / self.image.name, Path('/etc/fake.png')]
        folder = self.root / 'linked-dir'
        folder.symlink_to(self.root, target_is_directory=True)
        targets.append(folder / self.image.name)
        fifo = self.root / 'pipe.png'
        os.mkfifo(fifo)
        targets.append(fifo)
        for target in targets:
            with self.subTest(target=target), self.assertRaises((OSError, ValueError)):
                read_photo(target, self.config)
        hard = self.root / 'hard.png'
        os.link(self.image, hard)
        with self.assertRaises(ValueError): read_photo(hard, self.config)

    def test_markdown_only_dedupe_code_examples_and_three_image_limit(self):
        photos, rejected = prepare_images(self.text + '\n' + self.text, self.config)
        self.assertEqual((len(photos), rejected), (1, 0))
        self.assertEqual(prepare_images(f'```\n{self.text}\n```\n`{self.text}`', self.config), ([], 0))
        photos, rejected = prepare_images('![x](https://example.com/img.png)', self.config)
        self.assertEqual((len(photos), rejected), (0, 1))
        text = ''
        for i in range(5):
            image = self.root / f'{i}.png'
            image.write_bytes(png())
            text += f'![{i}]({image})\n'
        self.assertEqual(len(prepare_images(text, self.config)[0]), 3)

    def test_size_and_dimensions_guard(self):
        too_big = self.root / 'big.png'
        with too_big.open('wb') as out: out.truncate(10_000_001)
        with self.assertRaises(ValueError): read_photo(too_big, self.config)
        too_big.write_bytes(png(100, 1))
        with self.assertRaises(ValueError): read_photo(too_big, self.config)

    async def complete(self):
        await self.bridge.handle(update(2, 'show image'))
        original = self.adapter.run_turn
        async def run(*args, **kwargs):
            result = await original(*args, **kwargs)
            return {**result, 'text': self.text}
        self.adapter.run_turn = run
        await self.bridge.work_once()

    async def test_bridge_auto_photo_immutable_snapshot_and_no_duplicate_delivery(self):
        await self.complete()
        saved = self.image.read_bytes()
        self.image.write_bytes(png(5, 5))
        self.assertEqual(self.store.job(1)['response_text'], self.text)
        await deliver(self.store, self.transport)
        await deliver(self.store, self.transport)
        self.assertEqual(len(self.transport.photos), 1)
        self.assertEqual(self.transport.photos[0][1], saved)
        self.assertTrue(any('이미지 첨부' in text for _, text in self.transport.messages))
        self.assertEqual(self.store.conn.execute("SELECT state FROM outbox WHERE photo IS NOT NULL").fetchone()[0], 'sent')

    async def test_images_command_no_model_rerun_and_gateway_dedupe(self):
        await self.complete()
        calls = len(self.adapter.calls)
        command = update(10, '/images 1')
        await self.bridge.handle(command)
        await self.bridge.handle({**command, 'update_id': 11})
        self.assertEqual(len(self.adapter.calls), calls)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM outbox WHERE photo IS NOT NULL').fetchone()[0], 2)
        with self.assertRaises(ValueError): await self.bridge.command('/images 1', 8, 8, 12)

    async def test_quota_and_owner_check(self):
        await self.complete()
        photos, _ = prepare_images(self.text, self.config)
        with patch('heimdall_bridge.outgoing.MAX_STORED', 0):
            self.assertEqual(queue_images(self.store, self.config, self.store.job(1), photos, 'limit'), (0, 1))
        with self.assertRaises(ValueError):
            queue_images(self.store, self.config, {**self.store.job(1), 'user_id':8}, photos, 'wrong')

    async def test_ambiguous_photo_no_retry_recovery_and_hash_mismatch(self):
        await self.complete()
        class Uncertain(FakeTelegramTransport):
            async def send_photo(self, *args): raise DeliveryUncertain('uncertain')
        await deliver(self.store, Uncertain())
        self.assertEqual(self.store.conn.execute('SELECT state FROM outbox WHERE photo IS NOT NULL').fetchone()[0], 'uncertain')
        self.store.conn.execute("UPDATE outbox SET state='sending' WHERE photo IS NOT NULL")
        self.store.recover()
        self.assertEqual(self.store.conn.execute('SELECT state FROM outbox WHERE photo IS NOT NULL').fetchone()[0], 'uncertain')
        self.store.conn.execute("UPDATE outbox SET state='pending',photo_sha256='tampered' WHERE photo IS NOT NULL")
        await deliver(self.store, self.transport)
        self.assertFalse(self.transport.photos)
        self.assertEqual(self.store.conn.execute('SELECT state FROM outbox WHERE photo IS NOT NULL').fetchone()[0], 'failed')

    async def test_photo_snapshot_survives_backup_and_fresh_restore(self):
        await self.complete()
        directory = self.root / 'backups'
        directory.mkdir(mode=0o700)
        parent = self.root / 'restore-parent'
        parent.mkdir(mode=0o700)
        archive = backup_bundle(self.config, directory)
        restore_bundle(self.config, archive, parent / 'restored')
        with closing(sqlite3.connect(parent / 'restored' / 'bridge.sqlite3')) as db:
            row = db.execute('SELECT photo,photo_sha256 FROM outbox WHERE photo IS NOT NULL').fetchone()
            self.assertEqual(row[0], self.image.read_bytes())
            self.assertEqual(hashlib.sha256(row[0]).hexdigest(), row[1])

    async def test_manual_resend_requires_uncertain_confirmation_without_model_rerun(self):
        await self.complete()
        identifier = self.store.conn.execute('SELECT delivery_id FROM outbox WHERE photo IS NOT NULL').fetchone()[0]
        self.store.conn.execute("UPDATE outbox SET state='uncertain' WHERE delivery_id=?", (identifier,))
        calls = len(self.adapter.calls)
        with self.assertRaises(ValueError):
            self.bridge.commands.ops.resend(identifier, 7, 7, 100)
        self.bridge.commands.ops.resend(identifier, 7, 7, 101, confirm_uncertain=True)
        await deliver(self.store, self.transport)
        self.assertEqual(len(self.transport.photos), 1)
        self.assertEqual(len(self.adapter.calls), calls)


class PhotoHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = TelegramTransport('123456:SECRET_FIXTURE_NOT_FOR_LOGS')
        await self.transport.client.aclose()

    async def asyncTearDown(self): await self.transport.close()

    async def test_multipart_fixed_host_no_path_or_url_handoff(self):
        calls = []
        def handle(request):
            calls.append(request)
            self.assertEqual(request.url.host, 'api.telegram.org')
            self.assertTrue(request.url.path.endswith('/sendPhoto'))
            self.assertIn(b'name="photo"; filename="image.png"', request.content)
            self.assertIn(png(), request.content)
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 42}})
        self.transport.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.assertEqual(await self.transport.send_photo(7, png(), 'caption', 'image/png'), 42)
        self.assertEqual(len(calls), 1)

    async def test_429_then_success_but_network_and_5xx_are_uncertain(self):
        calls = []
        def handle(request):
            calls.append(request)
            if len(calls) == 1: return httpx.Response(429, json={'ok':False,'error_code':429,'parameters':{'retry_after':1}})
            return httpx.Response(200, json={'ok':True,'result':{'message_id':42}})
        self.transport.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with patch('heimdall_bridge.transport.asyncio.sleep', return_value=None):
            self.assertEqual(await self.transport.send_photo(7, png(), '', 'image/png'), 42)
        await self.transport.close()
        for payload in ({'ok':False,'error_code':500}, []):
            self.transport.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500,json=payload)))
            with self.assertRaises(DeliveryUncertain) as caught:
                await self.transport.send_photo(7, png(), '', 'image/png')
            self.assertNotIn(self.transport.token, str(caught.exception))
            await self.transport.close()
        def timeout(request): raise httpx.ReadTimeout('SECRET', request=request)
        self.transport.client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        with self.assertRaises(DeliveryUncertain): await self.transport.send_photo(7, png(), '', 'image/png')
