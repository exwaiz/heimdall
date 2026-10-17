import json
import unittest
from unittest.mock import patch

import test_auto_memory as fixtures
from heimdall_bridge.memory_commands import parse, handle_memory


class MemoryRequestTests(unittest.TestCase):
    setUp = fixtures.AutoMemoryTests.setUp
    note = fixtures.AutoMemoryTests.note
    enabled = fixtures.AutoMemoryTests.enabled
    refs = fixtures.AutoMemoryTests.refs

    def request(self, text, message=100, chat=7, user=7):
        return handle_memory(self.k, text, chat, user, message)

    def test_save_leading_and_trailing(self):
        for request in ('저장해줘: 테스트 알림은 17분', '테스트 알림은 17분 기억해줘',
                        '기억해 주세요. 테스트 알림은 17분'):
            self.assertEqual(parse(request), ('save', None, '테스트 알림은 17분'))
        self.assertIn('GB-000001', self.request('테스트 알림은 17분 저장해줘'))
        self.assertEqual(len(self.k.recent(7, 7)), 1)
        self.request('테스트 알림은 17분 저장해줘')
        self.assertEqual(len(self.k.recent(7, 7)), 1)

    def test_read_id_alias_and_query(self):
        self.note('테스트 알림 주기는 17분')
        for request in ('GB-000001', 'GB-000001 읽어줘', 'HK-000001 내용을 보여줘',
                        '알림 검색해줘', '검색해줘: 알림', '목록 보여줘'):
            self.assertIn('GB-000001', self.request(request))

    def test_all_collection_requests_not_keyword_search(self):
        for i in range(3):
            self.note(f'테스트 항목 {i}')
        for text in ('저장된 모든 지식을 읽어줘', '모든 지식 읽어줘', '전체 읽어줘',
                     '지식 전부 보여줘', '저장된 자료를 보여줘', '모든지식을 읽어줘',
                     '읽어줘: 저장된 모든 지식'):
            with self.subTest(text=text), patch.object(self.k, 'search', side_effect=AssertionError('not search')):
                reply = self.request(text)
                self.assertIn('전체 지식 3개', reply)
                for i in range(1, 4):
                    self.assertIn(f'GB-{i:06d}', reply)

    def test_all_pagination_no_hidden_latest_ten_limit(self):
        for i in range(12):
            self.note(f'테스트 항목 {i}')
        self.request('GB-000002 수정해줘: 최신 설명', message=101)
        self.store.conn.execute("UPDATE items SET deleted_at='fixture' WHERE item_id=3")
        first = self.request('저장된 모든 지식을 읽어줘')
        self.assertIn('전체 지식 11개', first)
        self.assertIn('최신 설명', first)
        self.assertNotIn('GB-000003', first)
        self.assertIn('다음: /memory 전체 읽어줘 2페이지', first)
        last = self.request('전체 읽어줘 3페이지')
        self.assertIn('GB-000012', last)
        self.assertNotIn('다음:', last)
        self.assertIn('마지막 페이지는 3페이지', self.request('전체 읽어줘 4페이지'))
        self.assertEqual(parse('TST731 모든 기록 읽어줘')[0], 'read')

    def test_all_empty_and_truncation_disclosed(self):
        self.assertIn('저장된 지식이 없습니다', self.request('전체 읽어줘'))
        self.note('가' * 4000)
        self.assertIn('긴 본문 일부 표시', self.request('전체 읽어줘'))
        with self.assertRaises(ValueError):
            self.request('전체 읽어줘', user=8)

    def test_derived_title_not_repeated_in_read_search_and_collection(self):
        value = self.note('중복 표시 검증 테스트 문장')
        for text in ('전체 내용 다 보여줘', f'{value} 읽어줘', '검증 검색해줘'):
            self.assertEqual(self.request(text).count('중복 표시 검증 테스트 문장'), 1)
        self.request(f'{value} 수정해줘: 수정된 검증 문장', message=101)
        self.assertEqual(self.request('전체 읽어줘').count('수정된 검증 문장'), 1)
        self.assertEqual(self.k.get(value, 7, 7)['title'], '수정된 검증 문장')

    def test_distinct_title_and_intentional_body_repetition_preserved(self):
        value = self.note('본문 내용\n본문 내용')
        self.store.conn.execute('UPDATE items SET title=? WHERE item_id=1', ('별도 제목',))
        reply = self.request(f'{value} 읽어줘')
        self.assertIn('별도 제목', reply)
        self.assertEqual(reply.count('본문 내용'), 2)

    def test_long_auto_title_not_repeated(self):
        text = '긴메모검증' * 50
        self.note(text)
        reply = self.request('전체 읽어줘')
        self.assertIn(text, reply)
        self.assertEqual(reply.count('긴메모검증'), 50)

    def test_screenshot_shape_explicit_update_and_original_preservation(self):
        value = self.note('사진의 옛 설명 테스트ALPHA')
        self.k.add_content(1, 'codex-description-v1', '과거 설명 테스트BETA')
        before = self.k.get(value, 7, 7)
        result = self.request(f'{value} 내용을 업데이트해줘. 사용자 제공 새 설명 테스트GAMMA')
        self.assertIn('수정 1 완료', result)
        item = self.k.get(value, 7, 7)
        self.assertEqual(item['contents'][0]['text'], '사용자 제공 새 설명 테스트GAMMA')
        self.assertEqual(len(item['contents']), 1)
        self.assertEqual(self.k.search('테스트ALPHA', 7, 7), [])
        self.assertEqual(self.k.search('테스트BETA', 7, 7), [])
        self.assertEqual(self.k.search('테스트GAMMA', 7, 7)[0]['id'], value)
        original = self.store.conn.execute("SELECT text FROM contents WHERE item_id=1 AND extractor='original'").fetchone()[0]
        self.assertEqual(original, before['contents'][1]['text'])
        history = json.loads(self.store.conn.execute('SELECT old_snapshot FROM knowledge_edits').fetchone()[0])
        self.assertEqual(history['contents'], before['contents'])
        self.k.add_content(1, 'codex-extract-v1', '뒤늦은 과거 설명 테스트DELTA')
        self.assertEqual(self.k.search('테스트DELTA', 7, 7), [])

    def test_edit_idempotence_and_alias(self):
        self.note('기존 테스트 기록')
        self.request('HK-000001 수정해줘: 새 테스트 기록')
        self.request('GB-000001 수정해줘: 새 테스트 기록')
        self.assertEqual(self.k.get(1, 7, 7)['revision'], 1)
        self.request('GB-000001 수정해줘: 다른 테스트 기록', message=101)
        self.assertEqual(self.k.get(1, 7, 7)['revision'], 2)

    def test_ambiguous_and_negative_do_not_write(self):
        self.note('기존 테스트 기록')
        for request in ('on', 'off', 'reset', '수정해줘: 새 내용',
                        'GB-000001 GB-000002 수정해줘: 새 내용',
                        'GB-000001 수정해줘', 'GB-000001 저장해줘: 새 내용',
                        '저장하지 마', 'GB-000001 수정하지 말아줘', '삭제해줘',
                        '이걸 어떻게 해야 할까', 'GB-000001 주기를 17분에서 20분으로 수정해줘'):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.request(request)
        self.assertEqual(self.k.get(1, 7, 7)['revision'], 0)
        self.assertEqual(len(self.k.recent(7, 7)), 1)

    def test_owner_and_message_required(self):
        for chat, user in ((8, 7), (7, 8), (True, 7)):
            with self.assertRaises(ValueError):
                self.request('저장해줘: 테스트', chat=chat, user=user)
        for message in (None, 0, True):
            with self.assertRaises(ValueError):
                self.request('저장해줘: 테스트', message=message)

    def test_mentions_of_storage_are_not_save_intents(self):
        self.note('저장한 내용 테스트 기록')
        self.request('저장한 내용 읽어줘')
        self.request('저장된 내용 보여줘')
        self.assertEqual(len(self.k.recent(7, 7)), 1)
        for text in ('저장하지는 마', '안 저장해줘', '수정하지도 말아줘'):
            with self.assertRaises(ValueError):
                self.request(text)

    def test_atomic_rollback(self):
        self.note('기존 테스트 기록')
        with patch.object(self.k, 'add_content', side_effect=ValueError('fixture')):
            with self.assertRaises(ValueError):
                self.request('GB-000001 수정해줘: 새 테스트 기록')
        self.assertEqual(self.k.get(1, 7, 7)['revision'], 0)
        self.assertEqual(self.k.get(1, 7, 7)['title'], '기존 테스트 기록')

    def test_stale_auto_memory_refs_invalid_after_edit_even_if_restored(self):
        value = self.note('8DT1 외부 노이즈 기존 기록')
        refs = self.refs()
        self.request(f'{value} 수정해줘: 8DT1 외부 노이즈 새 기록')
        self.assertEqual(self.retriever.validate(refs, '8DT1 외부 노이즈', 7, 7), ())
        new_refs = self.refs()
        current = self.retriever.validate(new_refs, '8DT1 외부 노이즈', 7, 7)
        self.assertEqual(current[0].origin, 'user-correction')
        self.request(f'{value} 수정해줘: 8DT1 외부 노이즈 기존 기록', message=101)
        self.assertEqual(self.retriever.validate(new_refs, '8DT1 외부 노이즈', 7, 7), ())

    def test_queued_strict_ask_invalidated(self):
        value = self.note('테스트 알림 주기는 17분이고 중요합니다')
        sources = self.k.context(value, 7, 7)
        job = {'knowledge_context': json.dumps({'purpose': 'ask', 'sources': sources}),
               'prompt': value, 'chat_id': 7, 'user_id': 7}
        self.k.prompt(job)
        self.request(f'{value} 수정해줘: 테스트 알림 주기는 20분')
        with self.assertRaises(ValueError):
            self.k.prompt(job)
        answer = json.dumps({'answer': f'옛 내용 [{value}]', 'evidence': [
            {'id': value, 'quote': '테스트 알림 주기는 17분이고 중요합니다'}]})
        with self.assertRaises(ValueError):
            self.k.finish(job, answer)

    def test_schema_reopen_preserves_edit_history(self):
        self.note('기존 테스트 기록')
        self.request('GB-000001 수정해줘: 새 테스트 기록')
        from heimdall_bridge.knowledge import Knowledge
        reopened = Knowledge(self.store, self.config)
        self.assertEqual(reopened.get(1, 7, 7)['revision'], 1)
