"""Temporary 10k-item lexical-retrieval benchmark. No model, auth or live DB."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import time

from heimdall_bridge.assets import AssetStore
from heimdall_bridge.auto_memory import AutoMemoryRetriever, encode_refs
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.knowledge import Knowledge


def benchmark():
    with tempfile.TemporaryDirectory(prefix='heimdall-retrieval-benchmark-') as directory:
        root = Path(directory)
        config = Config(root, root / 'private', root / 'schemas', root / 'codex', user_id=7, chat_id=7)
        with closing(Store(config.data_dir / 'bridge.sqlite3')) as store:
            AssetStore(store, config)
            knowledge = Knowledge(store, config)
            items, contents, sources, index = [], [], [], []
            stamp = '2026-10-17T00:00:00+00:00'
            for number in range(1, 10001):
                text = (f'8DT1 외부 노이즈 차폐 기준 {number}은 승인된 시험 기준입니다.' if number <= 3
                        else f'일반 보관 자료 {number}의 검증용 본문입니다.')
                items.append((number, 7, 7, f'fixture:{number}', 'note', text, stamp))
                contents.append((number, 'original', text, hashlib.sha256(text.encode()).hexdigest(), stamp))
                sources.append((number, 7, number, 'telegram'))
                index.append((number, text, text))
            with store.transaction():
                store.conn.executemany('INSERT INTO items(item_id,chat_id,user_id,source_key,kind,title,created_at) '
                                       'VALUES(?,?,?,?,?,?,?)', items)
                store.conn.executemany('INSERT INTO contents VALUES(?,?,?,?,?)', contents)
                store.conn.executemany('INSERT INTO sources VALUES(?,?,?,?)', sources)
                store.conn.executemany('INSERT INTO knowledge_search(rowid,title,body) VALUES(?,?,?)', index)
            retriever = AutoMemoryRetriever(knowledge)
            retriever.settings.set_auto_retrieve(7, 7, True)
            questions = ('8DT1 외부 노이즈', '외부 노이즈', 'F27 승인기록', 'GB-000001')
            for question in questions:
                retriever.retrieve(question, 7, 7)
            search_ms, pipeline_ms = [], []
            for n in range(60):
                question = questions[n % len(questions)]
                start = time.perf_counter()
                result = retriever.retrieve(question, 7, 7)
                selected = time.perf_counter()
                prepared = retriever.prepare(encode_refs(result.refs), question, 7, 7)
                end = time.perf_counter()
                assert prepared.source_count <= 3 and len(prepared.context or '') <= 3500
                search_ms.append((selected - start) * 1000)
                pipeline_ms.append((end - start) * 1000)
            def report(samples):
                return {'median_ms': round(statistics.median(samples), 2),
                        'p95_ms': round(sorted(samples)[56], 2), 'max_ms': round(max(samples), 2)}
            return {'fixture_items': 10000, 'iterations': 60, 'cache': 'warm, synthetic fixture',
                    'search_and_rerank': report(search_ms), 'including_validation_and_render': report(pipeline_ms),
                    'real_model_calls': 0, 'live_data_used': False}


if __name__ == '__main__':
    print(json.dumps(benchmark()))
