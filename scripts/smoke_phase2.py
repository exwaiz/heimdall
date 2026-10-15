"""Disposable, harmless actual sandbox and RPC checks. No Telegram credentials."""
import asyncio
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from heimdall_bridge.codex import CodexAdapter
from heimdall_bridge.config import Config
from heimdall_bridge.bridge import Bridge
from heimdall_bridge.db import Store
from heimdall_bridge.transport import FakeTelegramTransport
from heimdall_bridge.__main__ import update

ROOT = Path(__file__).resolve().parents[1]
PROBE = r'''
import errno,json,pathlib,sys
allowed,outside,secret=map(pathlib.Path,sys.argv[1:])
result={"workspace_write":False,"outside_write_blocked":False,"private_read_blocked":False}
try:
    allowed.write_text("harmless owned fixture")
    result["workspace_write"]=allowed.read_text()=="harmless owned fixture"
except OSError: pass
try: outside.write_text("harmless owned fixture")
except OSError as e: result["outside_write_blocked"]=e.errno in (errno.EACCES,errno.EPERM,errno.EROFS)
try: secret.read_text()
except OSError as e: result["private_read_blocked"]=e.errno in (errno.EACCES,errno.EPERM)
print(json.dumps(result))
'''


async def smoke():
    config = Config.load(ROOT / "config.toml", live=False)
    with tempfile.TemporaryDirectory(prefix="heimdall-phase2-", dir=ROOT / "artifacts") as private:
        with tempfile.TemporaryDirectory(prefix="probe-", dir=config.write_roots[0]) as workspace:
            config = replace(config, data_dir=Path(private))
            secret = config.data_dir / "secret-fixture.txt"
            secret.write_text("harmless fixture")
            adapter = CodexAdapter(config)
            process = await asyncio.create_subprocess_exec(config.codex_bin, "sandbox", "--permission-profile",
                "heimdall_workspace", *adapter.policy_args("workspace"), "--", "/usr/bin/python3", "-c", PROBE,
                str(Path(workspace) / "allowed.txt"), str(config.data_dir / "outside.txt"), str(secret),
                cwd=config.cwd, env=adapter.child_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            output, _ = await asyncio.wait_for(process.communicate(), 30)
            if process.returncode:
                raise RuntimeError("sandbox probe failed, stderr suppressed")
            results = json.loads(output)
            await adapter.open()
            try:
                thread = await adapter.new_thread()
                await adapter.rename_thread(thread["id"], "Heimdall Phase2 harmless smoke")
                result = await adapter.run_turn(thread["id"], "Reply exactly PHASE2_OK. Do not use any tools.", lambda _: None, progress)
                results["real_turn"] = result["status"] == "completed" and "PHASE2_OK" in result["text"]
                if result["status"] != "completed":
                    return {**results, "status": "failed", "model_error": result.get("error_code") or result["status"]}
                results["history"] = bool(await adapter.history(thread["id"], 2))
                fork = await adapter.fork_thread(thread["id"])
                results["fork"] = fork["id"] != thread["id"]
                results["release"] = await adapter.release_thread(fork["id"]) and await adapter.release_thread(thread["id"])
                await adapter.close()
                await adapter.open()
                results["reconnect"] = (await adapter.read_thread(thread["id"]))["id"] == thread["id"]
                # A scripted owner click is restricted to this disposable test job.
                test_config = replace(config, user_id=7, chat_id=7, runtime=90, approval_timeout=15)
                store = Store(config.data_dir / "bridge.sqlite3")
                transport = FakeTelegramTransport()
                bridge = Bridge(test_config, store, adapter, transport, conflict_check=lambda *_: False)
                diagnostics = []
                async def fixture_approval(message, connection, items):
                    params = message.get("params", {})
                    diagnostics.append({"method": message["method"], "keys": list(params),
                        "kind": params.get("kind"), "environmentId": params.get("environmentId"),
                        "cwd": params.get("cwd"), "availableDecisions": params.get("availableDecisions"),
                        "additionalPermissions": params.get("additionalPermissions")})
                    return await bridge.approvals.handle(message, connection, items)
                adapter.approval_handler = fixture_approval
                fixture = Path(workspace) / "approval-write.txt"
                try:
                    await bridge.handle(update(1, "/new Phase2 write approval fixture"))
                    await bridge.handle(update(2, "/mode write"))
                    code = f"import pathlib; pathlib.Path({str(fixture)!r}).write_text('PHASE2_WORKSPACE_OK')"
                    await bridge.handle(update(3, "Run exactly this harmless shell command once: /usr/bin/python3 -c "
                        + __import__('shlex').quote(code) + ". Do not request extra permissions, do not use apply_patch, "
                        "do not access any other files or network. Then reply PHASE2_WRITE_OK."))
                    task = asyncio.create_task(bridge.work_once())
                    clicked = 0
                    while not task.done():
                        while clicked < len(transport.buttons):
                            chat, mid, markup = transport.buttons[clicked]
                            clicked += 1
                            await bridge.approvals.callback({"id": f"fixture-{clicked}", "from": {"id": 7},
                                "message": {"message_id": mid, "chat": {"id": chat, "type": "private"}},
                                "data": markup["inline_keyboard"][0][0]["callback_data"]})
                        await asyncio.sleep(.05)
                    await task
                    job = store.job(1)
                    results["real_workspace_turn"] = job["state"] == "completed" and fixture.exists() and fixture.read_text() == "PHASE2_WORKSPACE_OK"
                    results["real_approval_rpc"] = clicked > 0 and store.conn.execute("SELECT count(*) FROM approvals WHERE state='resolved'").fetchone()[0] > 0
                    if results["real_workspace_turn"]:
                        await bridge.handle(update(4, "Reply exactly PHASE2_RESUME_OK. Do not use tools."))
                        await bridge.work_once()
                        resumed = store.job(2)
                        results["workspace_resume"] = resumed["state"] == "completed" and "PHASE2_RESUME_OK" in (resumed["response_text"] or "")
                        await bridge.handle(update(5, "/mode read"))
                        await bridge.handle(update(6, "Reply exactly PHASE2_READONLY_OK. Do not use tools."))
                        await bridge.work_once()
                        readonly = store.job(3)
                        results["readonly_reset"] = readonly["state"] == "completed" and "PHASE2_READONLY_OK" in (readonly["response_text"] or "")
                        if not results["readonly_reset"]:
                            results["readonly_error"] = readonly["error_code"] or "unexpected_response"
                            results["readonly_diagnostic"] = (readonly["response_text"] or "")[:1000]
                    if not results["real_workspace_turn"]:
                        results["write_error"] = job["error_code"] or "expected_fixture_not_created"
                        results["write_diagnostic"] = (job["response_text"] or "")[:2500]
                        results["approval_diagnostic"] = diagnostics
                finally:
                    store.close()
            finally:
                await adapter.close()
            results["status"] = "passed" if all(results.values()) else "failed"
            return results


async def progress(_):
    pass


if __name__ == "__main__":
    result = asyncio.run(smoke())
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
