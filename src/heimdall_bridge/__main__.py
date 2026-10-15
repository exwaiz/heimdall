import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from .bridge import Bridge
from .handoff import HandoffCodexAdapter
from .temporary import expiry_guard
from .config import Config, ConfigError
from .db import Store
from .fakes import FakeCodexAdapter
from .transport import FakeTelegramTransport, TelegramTransport, TransportError, deliver

LOGGER = logging.getLogger("heimdall_bridge")


def update(number, text, owner=7):
    return {"update_id": number, "message": {"message_id": number,
        "from": {"id": owner}, "chat": {"id": owner, "type": "private"}, "text": text}}


async def demo(path):
    config = Config.load(path, live=False)
    with tempfile.TemporaryDirectory(prefix="heimdall-demo-") as directory:
        config = replace(config, data_dir=Path(directory), user_id=7, chat_id=7)
        store = Store(config.data_dir / "bridge.sqlite3")
        adapter = FakeCodexAdapter(config.cwd)
        transport = FakeTelegramTransport()
        bridge = Bridge(config, store, adapter, transport, conflict_check=lambda *_: False)
        try:
            for number, text in enumerate(("/new 테스트", "/database", "첫 질문 그대로", "두 번째 질문"), 1):
                await bridge.handle(update(number, text))
            while await bridge.work_once():
                pass
            await deliver(store, transport)
            await deliver(store, transport)  # Flush any split chunks.
            print(json.dumps({"transport": "fake", "codex": "fake", "states": [dict(r) for r in
                store.conn.execute("SELECT job_id,state FROM jobs")], "messages": transport.messages}, ensure_ascii=False, indent=2))
        finally:
            store.close()


async def run(config):
    LOGGER.info("bridge_starting")
    os.umask(0o077)
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (config.data_dir / "bridge.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(config.data_dir / "bridge.sqlite3")
        adapter = HandoffCodexAdapter(config)
        transport = TelegramTransport(config.token)
        bridge = Bridge(config, store, adapter, transport)
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        def request_stop(sig):
            LOGGER.info("bridge_stop_requested signal=%s", sig.name)
            stopping.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, request_stop, sig)
        tasks = []
        try:
            store.recover()
            for recovered_job in store.jobs():
                bridge.sync_processing(recovered_job["job_id"])
            store.conn.execute("UPDATE processing_jobs SET state='queued' WHERE state='preparing'")
            while bridge.knowledge.backfill():
                await asyncio.sleep(0)
            await transport.initialize()
            await adapter.open()
            await bridge.reconcile()
            LOGGER.info("bridge_ready codex_version=%s", adapter.version)
            if "memory" not in Path("/sys/fs/cgroup/cgroup.controllers").read_text().split():
                LOGGER.warning("memory_cgroup_unavailable configured_memory_limits_not_enforced")

            async def worker():
                connection_failures = 0
                last_reconcile = time.monotonic()
                while not stopping.is_set():
                    try:
                        if not adapter.ready:
                            connection_failures += 1
                            await adapter.open()
                            await bridge.reconcile()
                        connection_failures = 0
                        if time.monotonic() - last_reconcile >= 15:
                            await bridge.reconcile()
                            last_reconcile = time.monotonic()
                        if not await bridge.work_once():
                            await asyncio.sleep(0.25)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        LOGGER.warning("worker_retry error_type=%s", type(exc).__name__)
                        if connection_failures >= 3:
                            raise
                        await asyncio.sleep(5)

            async def sender():
                while not stopping.is_set():
                    await deliver(store, transport, config.token)
                    await asyncio.sleep(0.25)

            async def knowledge_worker():
                while not stopping.is_set():
                    if adapter.ready and await bridge.process_knowledge_once():
                        continue
                    await asyncio.sleep(.5)

            async def poller():
                while not stopping.is_set():
                    try:
                        for item in await transport.updates(store.offset()):
                            await bridge.handle(item)
                    except TransportError as exc:
                        LOGGER.warning("poll_retry error_type=%s", type(exc).__name__)
                        if str(exc) in {"telegram_api_401", "telegram_api_409"}:
                            raise
                        await asyncio.sleep(5)

            async def mode_watcher():
                while not stopping.is_set():
                    bridge.expire_network_mode()
                    await asyncio.sleep(.5)

            tasks = [asyncio.create_task(worker()), asyncio.create_task(sender()), asyncio.create_task(poller()), asyncio.create_task(knowledge_worker()), asyncio.create_task(mode_watcher())]
            if config.temporary_access_until:
                def expired():
                    store.reply(config.chat_id,
                        '임시 권한 30분이 만료되어 기본 제한으로 복귀합니다. 진행 중 작업은 중단·상태 확인 처리하며 자동 재실행하지 않습니다.',
                        f'temporary-expired:{config.temporary_access_until}')
                    LOGGER.info('temporary_access_expired_restarting_with_defaults')
                tasks.append(asyncio.create_task(expiry_guard(config.temporary_access_until, expired)))
            stop_task = asyncio.create_task(stopping.wait())
            tasks.append(stop_task)
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                if task.done() and not task.cancelled() and task.exception():
                    raise task.exception()
            if not stopping.is_set():
                raise RuntimeError("background_task_stopped_without_shutdown_request")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await adapter.close()
            await transport.close()
            store.close()
            LOGGER.info("bridge_stopped")


def main():
    parser = argparse.ArgumentParser(description="Private Telegram ↔ Codex bridge")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[2] / "config.toml")
    parser.add_argument("--demo", action="store_true", help="isolated fake Telegram + fake Codex, no real turns")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        if args.demo:
            asyncio.run(demo(args.config))
        else:
            asyncio.run(run(Config.load(args.config)))
    except KeyboardInterrupt:
        return 0
    except ConfigError as exc:
        LOGGER.error("configuration_error: %s", exc)
        return 1
    except Exception as exc:
        LOGGER.error("bridge_stopped error_type=%s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
