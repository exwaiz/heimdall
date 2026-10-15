"""Short-lived per-thread owners; metadata queries never retain thread leases.

Unsubscribe is NOT an immediate unlock in Codex 0.162.1. A terminal turn's
dedicated app-server must exit before another local client can resume it.
"""
import asyncio
import time
import math
from dataclasses import replace

from .codex import CodexAdapter, RpcRejected, TurnUncertain


class HandoffCodexAdapter:
    def __init__(self, config, *, adapter_factory=CodexAdapter, max_owners=8):
        self.config = config
        self.factory = adapter_factory
        self.metadata = adapter_factory(config)
        self.owners = {}
        self.executing = set()
        self.creating = 0
        self.max_owners = max_owners
        self.stopping = False
        self.approval_handler = None
        self.network_stop = None
        self.network_closed = None

    @property
    def ready(self):
        return self.metadata.ready

    @property
    def version(self):
        return self.metadata.version

    async def open(self):
        self.stopping = False
        # Reconnecting metadata must NOT terminate an uncertain/running owner.
        await self.metadata.open()

    async def close(self):
        self.stopping = True
        owners = list(self.owners.values())
        await asyncio.gather(*(owner.close() for owner in owners), self.metadata.close())
        self.owners.clear()

    def reader(self, thread_id):
        owner = self.owners.get(thread_id)
        return owner if owner and owner.ready else self.metadata

    async def list_threads(self, cursor=None, search=None):
        return await self.metadata.list_threads(cursor, search)

    async def memory_config(self):
        return await self.metadata.memory_config()

    async def read_thread(self, thread_id):
        return await self.reader(thread_id).read_thread(thread_id)

    async def history(self, thread_id, limit=10):
        return await self.reader(thread_id).history(thread_id, limit)

    async def rename_thread(self, thread_id, name):
        return await self.reader(thread_id).rename_thread(thread_id, name)

    async def thread_is_active(self, thread_id):
        if thread_id in self.executing:
            return True
        return await self.reader(thread_id).thread_is_active(thread_id)

    async def _create(self, source=None):
        if self.stopping or len(self.owners) + self.creating >= self.max_owners:
            raise ValueError("열린 브리지 세션 한도입니다. 새 세션에 첫 질문을 보내거나 기존 작업 상태를 확인하세요.")
        self.creating += 1
        owner = self.factory(replace(self.config, enable_network_profile=True))
        registered = False
        try:
            await owner.open()
            thread = await (owner.new_thread() if source is None else owner.fork_thread(source))
            if self.stopping:
                raise RpcRejected("bridge_stopping")
            self.owners[thread["id"]] = owner
            registered = True
            return thread
        finally:
            self.creating -= 1
            if not registered:
                await owner.close()

    async def new_thread(self):
        # Empty new threads are not resumable after their first server exits.
        # Keep only their dedicated owner until the first turn is persisted.
        return await self._create()

    async def fork_thread(self, thread_id):
        return await self._create(thread_id)

    async def _drop(self, thread_id, owner):
        # Wait for actual process exit (not unsubscribe) before advertising release.
        await owner.close()
        if self.owners.get(thread_id) is owner:
            del self.owners[thread_id]

    async def run_turn(self, thread_id, prompt, on_started, progress, **kwargs):
        if self.stopping or self.executing:
            raise RpcRejected("bridge_turn_already_running")
        self.executing.add(thread_id)
        deadline = kwargs.pop('network_until', 0)
        network = kwargs.get('permission_mode') == 'network'
        if network:
            self.network_stop = asyncio.Event()
            self.network_closed = asyncio.Event()
        owner = self.owners.get(thread_id)
        turn_task = stop_task = None
        started_turn = None
        def started(turn_id):
            nonlocal started_turn
            started_turn = turn_id
            on_started(turn_id)
        try:
            if network and (type(deadline) not in (float, int) or not math.isfinite(deadline)
                            or not time.time() < deadline <= time.time() + 1801):
                raise RpcRejected('network_window_expired')
            network_cutoff = asyncio.get_running_loop().time() + max(0, deadline - time.time()) if network else 0
            execution_config = replace(self.config, enable_network_profile=True,
                temporary_access_until=deadline if network else self.config.temporary_access_until)
            if owner is None:
                if len(self.owners) + self.creating >= self.max_owners:
                    raise RpcRejected("bridge_owner_limit")
                owner = self.factory(execution_config)
                self.owners[thread_id] = owner
            owner.config = execution_config
            if not owner.ready:
                await owner.open()
            owner.approval_handler = self.approval_handler
            if network:
                turn_task = asyncio.create_task(owner.run_turn(thread_id, prompt, started, progress, **kwargs))
                stop_task = asyncio.create_task(self.network_stop.wait())
                done, _ = await asyncio.wait((turn_task, stop_task), timeout=max(0, network_cutoff - asyncio.get_running_loop().time()),
                    return_when=asyncio.FIRST_COMPLETED)
                if turn_task not in done:
                    turn_task.cancel()
                    await asyncio.gather(turn_task, return_exceptions=True)
                    if started_turn:
                        try:
                            await asyncio.wait_for(owner.interrupt(thread_id, started_turn), 2)
                        except Exception:
                            pass
                    await self._drop(thread_id, owner)
                    reason = 'network_mode_revoked' if self.network_stop.is_set() else 'network_window_expired'
                    raise TurnUncertain(reason)
                result = await turn_task
            else:
                result = await owner.run_turn(thread_id, prompt, on_started, progress, **kwargs)
            if result.get("status") in {"completed", "failed", "interrupted"}:
                await self._drop(thread_id, owner)
            # Unknown results/exceptions deliberately retain their owner. They
            # must be reconciled, not killed/retried merely to free a lock.
            return result
        except RpcRejected:
            # A rejected resume never acquired this thread. Do not leak an idle
            # server for every attempt to access somebody else's live session.
            if owner is not None and (network or thread_id not in owner.loaded_threads):
                await self._drop(thread_id, owner)
            raise
        except BaseException:
            # A network-enabled owner cannot be left alive with an unattended
            # expiry after losing control. Stop it, but preserve uncertain job
            # state and never replay the possibly accepted operation.
            if network and owner is not None and self.owners.get(thread_id) is owner:
                await self._drop(thread_id, owner)
            raise
        finally:
            for task in (turn_task, stop_task):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if network and self.network_closed:
                self.network_closed.set()
                self.network_stop = None
            self.executing.discard(thread_id)

    async def revoke_network(self):
        if self.network_stop is None:
            return False
        self.network_stop.set()
        await asyncio.wait_for(self.network_closed.wait(), 20)
        return True

    async def interrupt(self, thread_id, turn_id):
        owner = self.owners.get(thread_id)
        if owner is None:
            raise RpcRejected("turn_not_owned_by_bridge")
        await owner.interrupt(thread_id, turn_id)

    async def reconcile(self, thread_id, turn_id):
        result = await self.reader(thread_id).reconcile(thread_id, turn_id)
        owner = self.owners.get(thread_id)
        if result and result.get("status") in {"completed", "failed", "interrupted"} and owner and thread_id not in self.executing:
            await self._drop(thread_id, owner)
        return result

    async def release_thread(self, thread_id):
        owner = self.owners.get(thread_id)
        if owner is None:
            return False
        if thread_id in self.executing or await owner.thread_is_active(thread_id):
            raise RpcRejected("cannot_release_active_turn")
        if thread_id in owner.new_threads and not await owner.history(thread_id, 1):
            raise ValueError("첫 질문이 없는 새 세션은 아직 보존 가능한 대화 기록이 없습니다. 먼저 첫 질문을 보내주세요.")
        await self._drop(thread_id, owner)
        return True
