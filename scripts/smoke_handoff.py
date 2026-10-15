"""Actual Codex sequential handoff, disposable bridge DB; no Telegram secrets."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge
from heimdall_bridge.codex import CodexAdapter
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.handoff import HandoffCodexAdapter
from heimdall_bridge.transport import FakeTelegramTransport

ROOT = Path(__file__).resolve().parents[1]


async def smoke():
    with tempfile.TemporaryDirectory(prefix="heimdall-handoff-smoke-") as directory:
        config = replace(Config.load(ROOT / "config.toml", live=False),
            data_dir=Path(directory), user_id=7, chat_id=7, runtime=120)
        adapter, external = HandoffCodexAdapter(config), CodexAdapter(config)
        store = Store(config.data_dir / "bridge.sqlite3")
        bridge = Bridge(config, store, adapter, FakeTelegramTransport(), conflict_check=lambda *_: False)
        results = {}
        try:
            await adapter.open()
            await bridge.handle(update(1, "/new Sequential handoff verification"))
            thread_id = store.binding(7)
            pending = await adapter.new_thread()
            await bridge.handle(update(2, "Do not use tools. Reply exactly HANDOFF_FIRST_OK."))
            await bridge.work_once()
            job = store.job(1)
            assert job['state'] == 'completed', job['error_code']
            assert 'HANDOFF_FIRST_OK' in job['response_text']
            assert thread_id not in adapter.owners
            assert pending['id'] in adapter.owners
            results['terminal_owner_exited'] = True
            results['unstarted_other_thread_preserved'] = True

            await external.open()
            await external.resume_thread(thread_id)
            assert await external.history(thread_id, 1)
            results['independent_codex_resume_after_telegram'] = True
            await external.close()

            await bridge.handle(update(3, "Do not use tools. Reply exactly HANDOFF_SECOND_OK."))
            await bridge.work_once()
            job = store.job(2)
            assert job['state'] == 'completed', job['error_code']
            assert 'HANDOFF_SECOND_OK' in job['response_text']
            assert thread_id not in adapter.owners
            results['telegram_resumed_after_external_exit'] = True
            await external.open()
            await external.resume_thread(thread_id)
            assert len(await external.history(thread_id, 5)) >= 2
            assert store.binding(7) == thread_id
            results['second_external_resume_and_history_preserved'] = True
            results['status'] = 'passed'
            return results
        finally:
            await external.close()
            await adapter.close()
            store.close()


if __name__ == '__main__':
    print(json.dumps(asyncio.run(smoke())))
