import tempfile
import unittest
from pathlib import Path

import httpx

from heimdall_bridge.transport import TelegramTransport, TransportError


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="heimdall-download-test-")
        self.target = Path(self.temp.name) / "fixture"
        self.transport = TelegramTransport("123456:DO_NOT_LOG_THIS_FIXTURE")
        await self.transport.client.aclose()

    async def asyncTearDown(self):
        await self.transport.close()
        self.temp.cleanup()

    def mock(self, status=200, body=b"harmless", headers=None):
        self.calls = []
        def handle(request):
            self.calls.append(request)
            return httpx.Response(status, content=body, headers=headers)
        self.transport.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))

    async def test_fixed_host_bounded_download(self):
        self.mock()
        count = await self.transport.download_file("documents/file_1.txt", self.target, 100)
        self.assertEqual(count, 8)
        self.assertEqual(self.target.read_bytes(), b"harmless")
        self.assertEqual(self.calls[0].url.host, "api.telegram.org")

    async def test_arbitrary_url_traversal_and_encoded_paths_blocked_before_network(self):
        self.mock()
        for path in ("https://evil.test/file", "//evil/file", "../private", "photos/../private", "photos/%2fetc", "photos\\etc", "photos/file?x=1", "/photos/a", "photos//a"):
            with self.assertRaises(TransportError):
                await self.transport.download_file(path, self.target, 100)
        self.assertFalse(self.calls)

    async def test_redirect_never_followed(self):
        self.mock(302, headers={"location": "http://127.0.0.1/private"})
        with self.assertRaises(TransportError):
            await self.transport.download_file("photos/a.jpg", self.target, 100)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.target.exists())

    async def test_declared_and_actual_size_limits_cleanup(self):
        self.mock(body=b"a" * 101)
        with self.assertRaises(TransportError):
            await self.transport.download_file("photos/a.jpg", self.target, 100)
        self.assertFalse(self.target.exists())
        await self.transport.close()
        self.mock(body=b"a" * 101, headers={"content-length": "1"})
        with self.assertRaises(TransportError):
            await self.transport.download_file("photos/a.jpg", self.target, 100)
        self.assertFalse(self.target.exists())

    async def test_existing_target_is_never_overwritten(self):
        self.mock()
        self.target.write_bytes(b"original")
        with self.assertRaises(TransportError):
            await self.transport.download_file("photos/a.jpg", self.target, 100)
        self.assertEqual(self.target.read_bytes(), b"original")

    async def test_failure_no_credential_in_exception(self):
        self.mock(500)
        with self.assertRaises(TransportError) as caught:
            await self.transport.download_file("photos/a.jpg", self.target, 100)
        self.assertNotIn(self.transport.token, str(caught.exception))
