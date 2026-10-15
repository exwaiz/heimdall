import asyncio
import json
import unittest

from scripts.smoke_appserver import (
    MetadataClient, RpcError, check_params, check_tracked_thread,
    list_params, thread_metadata, tracking_cwd,
)


class Writer:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(json.loads(data))

    async def drain(self):
        pass


class Process:
    def __init__(self, messages, eof=True):
        self.stdin = Writer()
        self.stdout = asyncio.StreamReader()
        for message in messages:
            self.stdout.feed_data((json.dumps(message) + "\n").encode())
        if eof:
            self.stdout.feed_eof()


PARAMS = {"threadId": "opaque-id", "includeTurns": False}


class Phase0Tests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_before_response(self):
        process = Process([{"method": "unknown/event", "params": {}},
                           {"id": 1, "result": {"thread": {"id": "opaque-id"}}}])
        result = await MetadataClient(process).request("thread/read", PARAMS)
        self.assertEqual(result["thread"]["id"], "opaque-id")
        self.assertEqual(process.stdin.messages[0]["params"], PARAMS)

    async def test_response_id_mismatch(self):
        with self.assertRaises(RpcError):
            await MetadataClient(Process([{"id": 2, "result": {}}])).request("thread/read", PARAMS)

    async def test_server_request_is_rejected_and_stops(self):
        process = Process([{"method": "item/commandExecution/requestApproval", "id": "approval-1"}])
        with self.assertRaises(RpcError):
            await MetadataClient(process).request("thread/read", PARAMS)
        self.assertEqual(process.stdin.messages[-1]["id"], "approval-1")
        self.assertIn("error", process.stdin.messages[-1])

    async def test_timeout(self):
        with self.assertRaises(asyncio.TimeoutError):
            await MetadataClient(Process([], eof=False), timeout=0.01).request("thread/read", PARAMS)

    async def test_eof(self):
        with self.assertRaises(RpcError):
            await MetadataClient(Process([])).request("thread/read", PARAMS)

    async def test_rpc_error_does_not_leak_message(self):
        process = Process([{"id": 1, "error": {"code": -1, "message": "secret"}}])
        with self.assertRaises(RpcError) as caught:
            await MetadataClient(process).request("thread/read", PARAMS)
        self.assertNotIn("secret", str(caught.exception))

    def test_forbidden_calls_and_full_history(self):
        with self.assertRaises(KeyError):
            check_params("turn/start", {})
        with self.assertRaises(ValueError):
            check_params("thread/read", {**PARAMS, "includeTurns": True})
        with self.assertRaises(ValueError):
            check_params("thread/read", {**PARAMS, "unsupported": True})

    def test_read_only_listing_required(self):
        with self.assertRaises(ValueError):
            check_params("thread/list", {"limit": 50})

    def test_output_excludes_private_content(self):
        result = thread_metadata({"id": "opaque-id", "preview": "secret", "name": "secret",
                                  "turns": ["secret"], "cwd": "/workspace"})
        self.assertEqual(result, {"id": "opaque-id", "cwd": "/workspace"})

    def test_home_tracking_keeps_all_frontend_sources(self):
        params = list_params(tracking_cwd())
        self.assertEqual(params["cwd"], "/home/haaland")
        self.assertEqual(params["sourceKinds"], ["cli", "vscode", "exec", "appServer"])
        check_params("thread/list", params)

    def test_old_project_and_descendants_are_outside_tracking(self):
        for cwd in ("/home/haaland/personal-agent", "/home/haaland/heimdall",
                    "/home/haaland-other", None):
            with self.subTest(cwd=cwd), self.assertRaises(ValueError):
                check_tracked_thread({"cwd": cwd}, "/home/haaland")
        check_tracked_thread({"cwd": "/home/haaland"}, "/home/haaland")


if __name__ == "__main__":
    unittest.main()
