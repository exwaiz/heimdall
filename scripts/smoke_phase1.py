"""Opt-in live smoke on a NEW isolated thread; never reuse a user's existing thread."""
import argparse
import asyncio
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge, external_cli_busy
from heimdall_bridge.codex import CodexAdapter
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.transport import FakeTelegramTransport, deliver

ROOT = Path(__file__).resolve().parents[1]


async def smoke():
    config = Config.load(ROOT / "config.toml", live=False)
    with tempfile.TemporaryDirectory(prefix="heimdall-live-smoke-") as directory:
        config = replace(config, data_dir=Path(directory), user_id=7, chat_id=7, runtime=120)
        adapter = CodexAdapter(config)
        store = Store(config.data_dir / "bridge.sqlite3")
        transport = FakeTelegramTransport()
        allowed_test_thread = None
        # This script exclusively creates and uses its own new test thread.
        # Unknown unrelated interactive CLIs must not cause this isolated smoke to stall.
        def conflict(thread_id, cwd):
            return False if thread_id == allowed_test_thread else external_cli_busy(thread_id, cwd)
        bridge = Bridge(config, store, adapter, transport, conflict_check=conflict)
        try:
            await adapter.open()
            await bridge.handle(update(1, "/new Heimdall Phase1 Smoke"))
            allowed_test_thread = store.binding(7)
            if not allowed_test_thread:
                print(json.dumps({"rpc_rejections": adapter.rejection_diagnostics}), flush=True)
                raise RuntimeError("new_thread_failed")
            print(json.dumps({"stage": "new_thread", "thread_id": allowed_test_thread}), flush=True)
            await bridge.handle(update(2, "도구나 파일을 사용하지 말고 HEIMDALL_P1_OK 라는 한 줄만 답해줘."))
            await bridge.work_once()
            first = store.job(1)
            if first is None:
                raise RuntimeError("first_prompt_not_enqueued")
            if first["state"] != "completed" or "HEIMDALL_P1_OK" not in (first["response_text"] or ""):
                print(json.dumps({"rpc_rejections": adapter.rejection_diagnostics}), flush=True)
                raise RuntimeError("first_turn_" + str(first["error_code"] or first["state"]))
            print(json.dumps({"stage": "bridge_turn", "state": first["state"], "final_verified": True}), flush=True)
            await adapter.close()
            # Same test thread, sequential CLI resume. Raw event output is never printed.
            proc = await asyncio.create_subprocess_exec(config.codex_bin, "exec", "--sandbox", "read-only",
                "-c", 'approval_policy="on-request"', "-c", 'approvals_reviewer="user"',
                "--skip-git-repo-check", "--json", "resume", allowed_test_thread, "-",
                cwd=config.cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            try:
                output, _ = await asyncio.wait_for(proc.communicate(
                    "도구를 사용하지 말고 HEIMDALL_CLI_OK 라는 한 줄만 답해줘.".encode()), 150)
            except asyncio.TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                raise RuntimeError("cli_resume_timeout") from None
            events = [json.loads(line) for line in output.splitlines() if line.strip()]
            cli_final = next((e.get("item", {}).get("text", "") for e in events
                if e.get("type") == "item.completed" and e.get("item", {}).get("type") == "agent_message"), "")
            if proc.returncode != 0 or "HEIMDALL_CLI_OK" not in cli_final:
                raise RuntimeError("cli_resume_failed")
            print(json.dumps({"stage": "cli_resume", "completed": True}), flush=True)
            await adapter.open()
            await bridge.handle(update(3, "도구를 사용하지 말고 앞서 답한 HEIMDALL_로 시작하는 두 테스트 문자열을 순서대로 한 줄에 적어줘."))
            await bridge.work_once()
            last = store.job(2)
            text = last["response_text"] or ""
            if last["state"] != "completed" or not all(s in text for s in ("HEIMDALL_P1_OK", "HEIMDALL_CLI_OK")):
                raise RuntimeError("reverse_context_failed")
            await deliver(store, transport)
            return {"status": "passed", "telegram": "fake", "codex": "real",
                "thread_id": allowed_test_thread, "bridge_cli_bridge_context": "passed",
                "first_turn": first["codex_turn_id"], "last_turn": last["codex_turn_id"],
                "final_delivery": "passed", "actual_telegram": "not configured"}
        finally:
            await adapter.close()
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True,
                        help="creates a new real test thread and makes three model turns")
    parser.parse_args()
    try:
        result = asyncio.run(smoke())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "stage_error": str(exc) if isinstance(exc, RuntimeError) else "details suppressed"}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
