from dataclasses import replace
import json
import unittest

from heimdall_bridge.auto_memory import decode_refs, encode_refs
from heimdall_bridge.knowledge import canonical_id, identifier, label
import test_auto_memory as fixtures


class KnowledgeIdCompatibilityTests(unittest.TestCase):
    setUp = fixtures.AutoMemoryTests.setUp
    note = fixtures.AutoMemoryTests.note
    refs = fixtures.AutoMemoryTests.refs
    enabled = fixtures.AutoMemoryTests.enabled
    def test_gb_is_canonical_and_legacy_hk_is_an_alias(self):
        value = self.note('TST731 알림 주기는 17분으로 확정했습니다.')
        self.assertEqual(value, 'GB-000001')
        for alias in ('GB-000001', 'gb-1', 'HK-000001', 'hk-1'):
            self.assertEqual(identifier(alias), 1)
            self.assertEqual(canonical_id(alias), value)
            self.assertEqual(self.k.get(alias, 7, 7)['id'], value)
            self.assertEqual(self.k.search(alias, 7, 7)[0]['id'], value)
        self.assertEqual(len(self.k.search('HK-000001 GB-000001', 7, 7)), 1)
        self.assertEqual(self.k.save(7, 7, self.message, 'do not replace'), value)
        self.assertEqual(self.store.conn.execute('select count(*) from items').fetchone()[0], 1)

    def test_frozen_hk_refs_keep_original_version_and_render_gb(self):
        value = self.note('8DT1 외부 노이즈 차폐')
        refs = decode_refs(self.refs())
        item = self.k.get(value, 7, 7)
        legacy = replace(refs[0], id='HK-000001', item_sha256=self.retriever._version(item, legacy=True))
        frozen = encode_refs((legacy,))
        prepared = self.retriever.prepare(frozen, 'HK-000001', 7, 7)
        self.assertEqual(prepared.source_count, 1)
        self.assertIn('[GB-000001]', prepared.context)
        self.assertNotIn('[HK-000001]', prepared.context)
        mixed = json.loads(frozen) + json.loads(encode_refs(refs))
        self.assertEqual(decode_refs(json.dumps(mixed)), ())
        self.k.add_content(1, 'original', 'modified')
        self.assertEqual(self.retriever.validate(frozen, 'GB-000001', 7, 7), ())

    def test_queued_strict_ask_accepts_old_or_new_ids_without_rewriting_prompt(self):
        text = 'TST731 알림 주기는 17분으로 확정했습니다.'
        self.note(text)
        sources = self.k.context('HK-000001', 7, 7)
        sources[0]['id'] = 'HK-000001'
        job = {'chat_id':7, 'user_id':7, 'prompt':'HK-000001 알려줘',
               'knowledge_context':json.dumps({'purpose':'ask', 'sources':sources})}
        self.assertIn('"id": "GB-000001"', self.k.prompt(job))
        self.assertEqual(job['prompt'], 'HK-000001 알려줘')
        for key in ('HK-000001', 'GB-000001'):
            result = self.k.finish(job, json.dumps({'answer':f'17분입니다 [{key}]',
                                   'evidence':[{'id':key,'quote':text}]}))
            self.assertIn('[GB-000001]', result)
            self.assertNotIn('HK-', result)
