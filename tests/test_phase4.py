import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch, AsyncMock
import zipfile
from contextlib import closing

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.fakes import FakeCodexAdapter
from heimdall_bridge.knowledge import identifier
from heimdall_bridge.knowledge_backup import backup_bundle, restore_bundle
from heimdall_bridge.extract_worker import extract
from test_phase3 import FilesTransport
from test_assets import message, png

ROOT = Path(__file__).resolve().parents[1]


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="heimdall-knowledge-")
        self.root = Path(self.temp.name)
        self.config = replace(Config.load(ROOT / "config.toml", live=False), data_dir=self.root / "data", user_id=7, chat_id=7)
        self.store = Store(self.config.data_dir / "bridge.sqlite3")
        self.adapter = FakeCodexAdapter(self.config.cwd)
        self.transport = FilesTransport(png())
        self.bridge = Bridge(self.config, self.store, self.adapter, self.transport, conflict_check=lambda *_: False)
        self.k = self.bridge.knowledge
        await self.bridge.handle(update(1, "/new Knowledge"))

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def note(self, text="서울 출장 식비는 칠만삼천원으로 확정되었습니다.", mid=2):
        with self.store.transaction():
            return self.k.save(7, 7, mid, text)

    async def test_save_idempotent_and_korean_two_character_search(self):
        value = self.note()
        self.assertEqual(value, self.note("must not replace"))
        for query in ("출장", "칠만삼천", "서울 출장", value):
            self.assertEqual(self.k.search(query, 7, 7)[0]["id"], value)
        self.assertEqual(self.k.recent(7, 7)[0]["contents"][0]["text"], "서울 출장 식비는 칠만삼천원으로 확정되었습니다.")

    async def test_search_syntax_quoted_and_percent_not_sql(self):
        self.note()
        for query in ('" OR * --', "' UNION SELECT secret", "NEAR(출장,1)", "%", "_", "*", "notfound"):
            self.k.search(query, 7, 7)
        with self.assertRaises(ValueError):
            self.k.search("x" * 301, 7, 7)

    async def test_retrieval_selects_matching_late_document_passage(self):
        value = self.note()
        self.k.add_content(identifier(value), "document-v1", "일반 본문. " * 7000 + "특별검증코드는 ABC7319입니다.")
        sources = self.k.context("특별검증코드", 7, 7)
        self.assertIn("ABC7319", sources[0]["text"])
        self.assertLessEqual(len(sources[0]["text"]), 2200)

    async def test_owner_and_deleted_filter_before_retrieval(self):
        value = self.note()
        for op in (lambda: self.k.get(value, 8, 8), lambda: self.k.search("출장", 8, 8), lambda: self.k.save(8, 8, 20, "secret")):
            with self.assertRaises(ValueError):
                op()
        self.assertIn("제외", await self.bridge.command(f"/forget {value}", 7, 7))
        self.assertEqual(self.k.search("출장", 7, 7), [])
        with self.assertRaises(ValueError):
            self.k.get(value, 7, 7)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM contents").fetchone()[0], 1)

    async def test_tags_and_item_commands(self):
        value = self.note()
        await self.bridge.command(f"/tag {value} #가족 #출장", 7, 7)
        self.assertEqual(self.k.search("가족", 7, 7)[0]["tags"], ["가족", "출장"])
        self.assertIn(value, await self.bridge.command("/items", 7, 7))
        self.assertIn("original", await self.bridge.command(f"/item {value}", 7, 7))

    async def test_no_hits_never_calls_model(self):
        response = await self.bridge.command("/ask 없는자료", 7, 7, 90)
        self.assertIn("실행하지 않았습니다", response)
        self.assertFalse(self.store.jobs())
        self.assertFalse(self.adapter.calls)

    async def test_ask_freezes_context_readonly_and_citation_validation(self):
        value = self.note()
        await self.bridge.command("/mode write", 7, 7)
        await self.bridge.command("/ask 출장 식비", 7, 7, 90)
        job = self.store.job(1)
        self.assertEqual(job["permission_mode"], "readonly")
        self.assertEqual(job["prompt"], "출장 식비")
        self.k.add_content(identifier(value), "original", "나중에 변경된 다른 내용입니다.")
        self.assertIn("칠만삼천원", self.k.prompt(job))
        self.store.claim()
        self.store.transition(1, "running", codex_turn_id="fixture")
        output = {"answer": f"출장 식비는 칠만삼천원입니다. [{value}]", "evidence": [{"id": value, "quote": "서울 출장 식비는 칠만삼천원으로 확정되었습니다."}]}
        self.bridge.finish(job, {"status": "completed", "text": json.dumps(output, ensure_ascii=False)})
        self.assertEqual(self.store.job(1)["state"], "completed")
        self.assertIn("근거 자료", self.store.job(1)["response_text"])

    async def test_fabricated_citations_and_quotes_refused(self):
        value = self.note()
        await self.bridge.command("/ask 출장", 7, 7, 90)
        job = self.store.job(1)
        for proof in ({"id": "HK-999999", "quote": "서울 출장 식비는 칠만삼천원으로 확정되었습니다."},
                      {"id": value, "quote": "기록에 존재하지 않는 인용문입니다."}):
            with self.assertRaises(ValueError):
                self.k.finish(job, json.dumps({"answer": f"답변 [{value}]", "evidence": [proof]}))
        self.store.claim()
        self.store.transition(1, "running", codex_turn_id="fixture")
        self.bridge.finish(job, {"status": "completed", "text": "hallucinated raw output"})
        self.assertEqual(self.store.job(1)["error_code"], "knowledge_validation_failed")
        self.assertNotIn("hallucinated", self.store.job(1)["response_text"])

    async def test_forgotten_queued_context_blocked_before_model(self):
        value = self.note()
        await self.bridge.command("/ask 출장", 7, 7, 90)
        await self.bridge.command(f"/forget {value}", 7, 7)
        await self.bridge.work_once()
        self.assertEqual(self.store.job(1)["state"], "failed")
        self.assertFalse(self.adapter.calls)

    async def test_photo_auto_index_description_and_backfill_idempotent(self):
        await self.bridge.handle({"update_id": 2, "message": message(2, "photo")})
        self.assertEqual(self.k.recent(7, 7)[0]["asset_id"], 1)
        await self.bridge.work_once()
        self.assertTrue(any(c["extractor"] == "codex-description-v1" for c in self.k.get(1,7,7)["contents"]))
        self.assertEqual(self.k.backfill(), 0)

    async def test_image_extraction_uses_global_queue_and_dedup(self):
        await self.bridge.handle({"update_id": 2, "message": message(2, "photo")})
        await self.bridge.work_once()
        number = self.k.schedule("HK-000001", 7, 7, 90)
        self.assertEqual(self.k.schedule("HK-000001", 7, 7, 91), number)
        self.assertTrue(await self.bridge.process_knowledge_once())
        job = self.store.job(2)
        self.assertEqual(json.loads(job["image_assets"]), [1])
        self.assertEqual(job["permission_mode"], "readonly")
        self.assertEqual(self.store.binding(7), "fake-thread-1")
        self.assertIn("description", self.k.prompt(job))
        self.store.claim()
        self.store.transition(2, "running", codex_turn_id="extract-turn")
        self.bridge.finish(job, {"status":"completed", "text":json.dumps({"text":"영수증 금액 73000원", "description":"출장 식비 영수증 이미지"})})
        self.assertEqual(self.k.search("영수증",7,7)[0]["id"], "HK-000001")
        self.assertEqual(self.store.conn.execute("SELECT state FROM processing_jobs").fetchone()[0], "completed")

    async def test_document_prepare_local_only_indexes_without_model(self):
        self.transport.data = b"sample document"
        await self.bridge.handle({"update_id":2,"message":message(2,"document")})
        self.k.schedule("HK-000001",7,7,90)
        with patch("heimdall_bridge.extraction.prepare", new=AsyncMock(return_value={"text":"출장 영수증 총액은 칠만삼천원입니다.","images":[]})):
            await self.bridge.process_knowledge_once()
        self.assertFalse(self.adapter.calls)
        self.assertFalse(self.store.jobs())
        self.assertTrue(self.k.search("영수증",7,7))

    async def test_audio_unsupported_honestly_and_original_retained(self):
        await self.bridge.handle({"update_id":2,"message":message(2,"voice")})
        self.k.schedule("HK-000001",7,7,90)
        with patch("heimdall_bridge.extraction.prepare", new=AsyncMock(return_value={"unsupported":"audio_turn_input_unsupported"})):
            await self.bridge.process_knowledge_once()
        self.assertEqual(self.store.conn.execute("SELECT state FROM processing_jobs").fetchone()[0], "unsupported")
        self.assertTrue(Path(self.bridge.assets.get(1,7,7)["path"]).exists())
        self.assertFalse(self.store.jobs())

    async def test_generated_video_frames_owned_validated_bounded(self):
        await self.bridge.handle({"update_id":2,"message":message(2,"video")})
        frame = self.bridge.assets.import_image(1,1,png(),7,7)
        self.assertEqual(frame,self.bridge.assets.import_image(1,1,png(),7,7))
        self.assertEqual(self.bridge.assets.local_image(frame,7,7)["type"],"localImage")
        self.assertEqual(self.k.backfill(),0)
        with self.assertRaises(ValueError):
            self.bridge.assets.import_image(1,4,png(),7,7)

    async def test_video_model_prompt_discloses_partial_frames_and_no_audio(self):
        await self.bridge.handle({"update_id":2,"message":message(2,"video")})
        self.k.schedule("HK-000001",7,7,90)
        result={"text":"일부 프레임만 분석","images":[base64.b64encode(png()).decode()]}
        with patch("heimdall_bridge.extraction.prepare",new=AsyncMock(return_value=result)):
            await self.bridge.process_knowledge_once()
        job=self.store.job(1)
        self.assertEqual(json.loads(job["knowledge_context"])["source_kind"],"video")
        self.assertIn("소리를 분석한 것처럼 말하지",self.k.prompt(job))

    async def test_legacy_photo_answer_backfill_does_not_call_model(self):
        await self.bridge.handle({"update_id":2,"message":message(2,"photo")})
        await self.bridge.work_once()
        for table in ("knowledge_search","contents","sources","items"):
            self.store.conn.execute(f"DELETE FROM {table}")
        calls=len(self.adapter.calls)
        self.assertEqual(self.k.backfill(),1)
        self.assertEqual(self.k.backfill(),0)
        self.assertEqual(len(self.adapter.calls),calls)
        self.assertTrue(any(c["extractor"]=="codex-description-v1" for c in self.k.recent(7,7)[0]["contents"]))

    async def test_full_bundle_restore_originals_fts_and_paths(self):
        value=self.note()
        await self.bridge.handle({"update_id":2,"message":message(2,"photo")})
        backups=self.root/"backups"
        backups.mkdir(mode=0o700)
        archive=backup_bundle(self.config,backups)
        self.assertEqual(archive.stat().st_mode & 0o777,0o600)
        destination=self.root/"restored"
        restore_bundle(self.config,archive,destination)
        with closing(sqlite3.connect(destination/"bridge.sqlite3")) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            path,digest=conn.execute("SELECT path,sha256 FROM assets WHERE asset_id=1").fetchone()
            self.assertEqual(Path(path).parent,destination/"assets")
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(),digest)
            self.assertTrue(conn.execute("SELECT rowid FROM knowledge_search WHERE knowledge_search MATCH '\"출장\"'").fetchall() or conn.execute("SELECT count(*) FROM items").fetchone()[0])
        with self.assertRaises(ValueError):
            restore_bundle(self.config,archive,destination)
        with self.assertRaises(ValueError):
            restore_bundle(self.config,archive,self.config.data_dir/"unsafe")

    async def test_bundle_rejects_extra_traversal_and_bad_hash_without_destination(self):
        await self.bridge.handle({"update_id":2,"message":message(2,"photo")})
        backups=self.root/"backups"
        backups.mkdir(mode=0o700)
        archive=backup_bundle(self.config,backups)
        with zipfile.ZipFile(archive,"a") as z:
            z.writestr("../escape","malicious")
        with self.assertRaises(ValueError):
            restore_bundle(self.config,archive,self.root/"unsafe")
        self.assertFalse((self.root/"unsafe").exists())
        self.assertFalse((self.root.parent/"escape").exists())

    async def test_bundle_refuses_tampered_original_and_symlink_restore_parent(self):
        await self.bridge.handle({"update_id":2,"message":message(2,"photo")})
        backups=self.root/"backups"
        backups.mkdir(mode=0o700)
        archive=backup_bundle(self.config,backups)
        linked=self.root/"linked"
        linked.symlink_to(backups,target_is_directory=True)
        with self.assertRaises(ValueError):
            restore_bundle(self.config,archive,linked/"restored")
        path=Path(self.bridge.assets.get(1,7,7)["path"])
        path.write_bytes(b"tampered original")
        with self.assertRaises(ValueError):
            backup_bundle(self.config,backups)
        self.assertEqual(list(backups.iterdir()),[archive])

    async def test_extract_bad_json_fails_and_retry_requires_new_extract_request(self):
        await self.bridge.handle({"update_id":2,"message":message(2,"photo")})
        await self.bridge.work_once()
        self.k.schedule("HK-000001",7,7,90)
        await self.bridge.process_knowledge_once()
        job=self.store.claim()
        self.store.transition(job["job_id"],"running",codex_turn_id="extract")
        self.bridge.finish(job,{"status":"completed","text":'{"text": "a", "description": 123}'})
        self.assertEqual(self.store.job(job["job_id"])["state"],"failed")
        self.assertEqual(self.store.conn.execute("SELECT state FROM processing_jobs").fetchone()[0],"failed")
        with self.assertRaises(ValueError):
            self.bridge.commands.ops.retry(job["job_id"],7,7,91,job["baseline"],self.config.max_queue,confirm_repeat=True)

    async def test_processing_reconciliation_preserves_uncertain_without_replay(self):
        await self.bridge.handle({"update_id":2,"message":message(2,"photo")})
        await self.bridge.work_once()
        self.k.schedule("HK-000001",7,7,90)
        await self.bridge.process_knowledge_once()
        job=self.store.claim()
        self.store.transition(job["job_id"],"running",codex_turn_id="extract")
        self.store.recover()
        self.bridge.sync_processing(job["job_id"])
        self.assertEqual(self.store.conn.execute("SELECT state FROM processing_jobs").fetchone()[0],"uncertain")
        self.assertIsNone(self.store.claim())
        self.adapter.results["extract"]={"status":"completed","text":json.dumps({"text":"73000","description":"영수증 금액"})}
        await self.bridge.reconcile()
        self.assertEqual(self.store.conn.execute("SELECT state FROM processing_jobs").fetchone()[0],"completed")


class DecoderTests(unittest.TestCase):
    def test_pdf_text_extraction(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer=PdfWriter()
        page=writer.add_blank_page(width=300,height=200)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        stream=DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 10 100 Td (HEIMDALL DOCUMENT 73000) Tj ET')
        page[NameObject('/Contents')]=stream
        data=io.BytesIO()
        writer.write(data)
        self.assertIn('73000',extract('document','application/pdf',data.getvalue())['text'])

    def test_plaintext_docx_and_unsupported_archive(self):
        self.assertIn("text",extract("document","text/plain","출장 기록".encode()))
        output=io.BytesIO()
        with zipfile.ZipFile(output,"w") as z:
            z.writestr("word/document.xml",'<w:document xmlns:w="word"><w:t>출장 보고서</w:t></w:document>')
        self.assertIn("출장 보고서",extract("document","application/zip",output.getvalue())["text"])
        self.assertEqual(extract("audio","application/octet-stream",b"audio")["unsupported"],"audio_turn_input_unsupported")

    def test_xml_entity_and_archive_bomb_refused(self):
        for xml in ('<!DOCTYPE x [<!ENTITY xx SYSTEM "file:///etc/passwd">]><x>&xx;</x>',"a"*100000):
            output=io.BytesIO()
            with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr("word/document.xml",xml)
            with self.assertRaises(ValueError):
                extract("document","application/zip",output.getvalue())
