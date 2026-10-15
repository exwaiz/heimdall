"""Metadata-only App Server smoke. Never resumes/creates a thread or starts a turn."""
import argparse
import asyncio
import json
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {
    "initialize": "v1/InitializeParams.json",
    "thread/list": "v2/ThreadListParams.json",
    "thread/read": "v2/ThreadReadParams.json",
}


def tracking_cwd(config_path=ROOT / "config.toml"):
    with config_path.open("rb") as handle:
        tracking = tomllib.load(handle)["session_tracking"]
    if tracking.get("match") != "exact":
        raise ValueError("Only exact workspace tracking is supported")
    path = Path(tracking["cwd"])
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("Tracking cwd must be an existing absolute directory")
    path = path.resolve(strict=True)
    if path == Path("/"):
        raise ValueError("Root cannot be the tracking workspace")
    return str(path)


def list_params(cwd):
    return {"limit": 50, "sortKey": "updated_at", "sortDirection": "desc",
            "sourceKinds": ["cli", "vscode", "exec", "appServer"],
            "archived": False, "useStateDbOnly": True, "cwd": cwd}


def check_tracked_thread(thread, cwd):
    if thread.get("cwd") != cwd:
        raise ValueError("Thread outside configured tracking workspace")


def check_params(method, params):
    # Deliberately restricted Phase 0 allowlist, including a strict field check.
    # This is not a general JSON Schema validator.
    schema = json.loads((ROOT / "schemas" / SCHEMAS[method]).read_text())
    if set(params) - set(schema["properties"]):
        raise ValueError("Unsupported request field")
    if set(schema.get("required", [])) - set(params):
        raise ValueError("Missing required request field")
    if method == "thread/list":
        if params.get("useStateDbOnly") is not True:
            raise ValueError("Phase 0 requires state-DB-only listing")
        definitions = schema["definitions"]
        if not set(params["sourceKinds"]) <= set(definitions["ThreadSourceKind"]["enum"]):
            raise ValueError("Unsupported source kind")
        if params["sortKey"] not in definitions["ThreadSortKey"]["enum"]:
            raise ValueError("Unsupported sort key")
    if method == "thread/read" and params.get("includeTurns") is not False:
        raise ValueError("Phase 0 requires metadata-only read")


class RpcError(RuntimeError):
    pass


class MetadataClient:
    def __init__(self, process, timeout=20):
        self.process = process
        self.timeout = timeout
        self.sequence = 0
        self.notifications = 0

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params):
        check_params(method, params)
        self.sequence += 1
        request_id = self.sequence
        await self.send({"id": request_id, "method": method, "params": params})

        async def receive():
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise RpcError("App Server stdout closed")
                message = json.loads(line)
                if "method" in message:
                    self.notifications += 1
                    if "id" in message:
                        # No approvals should arise during a metadata query.
                        # Do not leave unexpected server requests hanging.
                        await self.send({"id": message["id"], "error": {
                            "code": -32601, "message": "Unsupported during metadata-only diagnostic"}})
                        raise RpcError("Unexpected server request; diagnostic stopped")
                    continue
                if message.get("id") != request_id:
                    raise RpcError("Unexpected response id")
                if "error" in message:
                    raise RpcError(f"RPC rejected: {method}; code={message['error'].get('code')}")
                return message["result"]

        return await asyncio.wait_for(receive(), self.timeout)


def thread_metadata(thread):
    # No preview, title, message body, auth response, or rollout content in output.
    return {k: thread[k] for k in ("id", "cwd", "source", "createdAt", "updatedAt", "status") if k in thread}


async def smoke(codex_bin, thread_id=None, timeout=20, config_path=ROOT / "config.toml"):
    cwd = tracking_cwd(config_path)
    process = await asyncio.create_subprocess_exec(
        codex_bin, "app-server", "--strict-config", "--listen", "stdio://",
        "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="on-request"',
        "-c", 'approvals_reviewer="user"',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        # App Server logs can contain personal data; never echo raw stderr.
        stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024,
        cwd=ROOT,
    )
    client = MetadataClient(process, timeout)
    try:
        await client.request("initialize", {"clientInfo": {
            "name": "heimdall", "title": "Heimdall", "version": "0.0.1"}})
        await client.send({"method": "initialized", "params": {}})
        listing = await client.request("thread/list", list_params(cwd))
        threads = listing["data"]
        for thread in threads:
            check_tracked_thread(thread, cwd)
        cli = next((t for t in threads if t.get("source") == "cli"), None)
        selected = thread_id or (cli or (threads[0] if threads else {})).get("id")
        read = None
        if selected:
            response = await client.request("thread/read", {"threadId": selected, "includeTurns": False})
            if response["thread"]["id"] != selected:
                raise RpcError("Read response thread id mismatch")
            check_tracked_thread(response["thread"], cwd)
            read = thread_metadata(response["thread"])
        return {"initialize": "passed", "thread_list": "passed",
                "thread_read": "passed" if read else "not tested: empty listing",
                "codex_home": os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
                "tracking_cwd": cwd, "tracking_match": "exact",
                "listed_count": len(threads), "next_cursor_present": bool(listing.get("nextCursor")),
                "threads": [thread_metadata(t) for t in threads],
                "read_thread": read, "notifications_count": client.notifications,
                "turn_started": False, "thread_resumed": False}
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    parser.add_argument("--thread-id")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    args = parser.parse_args()
    try:
        result = asyncio.run(smoke(args.codex_bin, args.thread_id, args.timeout, args.config))
    except Exception as exc:
        # Raw subprocess/RPC errors may contain secrets. Emit category only.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
