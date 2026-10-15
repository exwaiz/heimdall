import asyncio
import copy

from .codex import RpcRejected, TurnUncertain


class FakeCodexAdapter:
    def __init__(self, cwd):
        self.cwd = str(cwd)
        self.threads = {}
        self.calls = []
        self.results = {}
        self.delay = 0
        self.fail = False
        self.disconnected = False
        self.interrupted = set()
        self.ready = True
        self.version = "fake"
        self.loaded_threads = set()

    async def open(self):
        self.ready = True

    async def close(self):
        self.ready = False

    async def memory_config(self):
        from .memory import sanitize_memory_config
        return sanitize_memory_config(None)

    async def revoke_network(self):
        return False

    async def new_thread(self):
        thread_id = f"fake-thread-{len(self.threads) + 1}"
        self.threads[thread_id] = {"id": thread_id, "cwd": self.cwd, "source": "appServer",
                                  "updatedAt": 1, "status": {"type": "idle"}}
        self.loaded_threads.add(thread_id)
        return copy.deepcopy(self.threads[thread_id])

    async def rename_thread(self, thread_id, name):
        self.threads[thread_id]["name"] = name

    async def history(self, thread_id, limit):
        return []

    async def fork_thread(self, thread_id):
        await self.read_thread(thread_id)
        return await self.new_thread()

    async def release_thread(self, thread_id):
        if thread_id not in self.loaded_threads:
            return False
        self.loaded_threads.remove(thread_id)
        return True

    async def list_threads(self, cursor=None, search=None):
        data = list(self.threads.values())
        if search:
            data = [t for t in data if search in t.get("name", "")]
        offset = int(cursor or 0)
        return {"data": copy.deepcopy(data[offset:offset + 20]),
                "nextCursor": str(offset + 20) if offset + 20 < len(data) else None}

    async def read_thread(self, thread_id):
        if thread_id not in self.threads:
            raise RpcRejected("not_found")
        return copy.deepcopy(self.threads[thread_id])

    async def thread_is_active(self, thread_id):
        return self.threads[thread_id]["status"]["type"] == "active"

    async def run_turn(self, thread_id, prompt, on_started, progress, *, permission_mode="readonly", image_paths=(), network_until=0):
        turn_id = f"turn-{len(self.calls) + 1}"
        self.calls.append((thread_id, prompt))
        on_started(turn_id)
        await asyncio.sleep(self.delay)
        if self.disconnected:
            raise TurnUncertain("connection_lost_during_turn")
        result = {"status": "interrupted" if turn_id in self.interrupted else ("failed" if self.fail else "completed"),
                  "text": "Codex: " + prompt, "turn_id": turn_id}
        self.results[turn_id] = result
        self.threads[thread_id]["updatedAt"] += 1
        return result

    async def interrupt(self, thread_id, turn_id):
        self.interrupted.add(turn_id)

    async def reconcile(self, thread_id, turn_id):
        return self.results.get(turn_id)

