"""Local image through real App Server; optional dedicated Telegram file round trip."""
import argparse
import asyncio
import json
import os
import struct
import tempfile
import zlib
from dataclasses import replace
from pathlib import Path

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge
from heimdall_bridge.codex import CodexAdapter
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.transport import FakeTelegramTransport, TelegramTransport

ROOT = Path(__file__).resolve().parents[1]


def red_png():
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 48, 48, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff\x00\x00" * 48) * 48)) + chunk(b"IEND", b""))


class FixtureTransport(FakeTelegramTransport):
    async def get_file(self, file_id):
        return {"file_id": file_id, "file_path": "photos/phase3_fixture.png", "file_size": len(red_png())}

    async def download_file(self, path, destination, limit, *, reserved_identity=None):
        # This is generated test content, not image editing or a real attachment.
        fd = os.open(destination, os.O_WRONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "wb") as output:
            info = os.fstat(output.fileno())
            if (info.st_dev, info.st_ino) != reserved_identity:
                raise ValueError("fixture_identity_mismatch")
            output.write(red_png())
        return len(red_png())


async def smoke(telegram=False):
    if telegram:
        from scripts.setup_telegram import existing_env
        os.environ.update(existing_env())
    config = Config.load(ROOT / "config.toml", live=telegram)
    owner = config.user_id if telegram else 7
    with tempfile.TemporaryDirectory(prefix="heimdall-phase3-", dir=ROOT / "artifacts") as directory:
        config = replace(config, data_dir=Path(directory), user_id=owner, chat_id=owner, runtime=90)
        store = Store(config.data_dir / "bridge.sqlite3")
        adapter = CodexAdapter(config)
        transport = TelegramTransport(config.token) if telegram else FixtureTransport()
        try:
            await adapter.open()
            bridge = Bridge(config, store, adapter, transport, conflict_check=lambda *_: False)
            await bridge.handle(update(1, "/new Heimdall Phase3 image smoke", owner))
            if telegram:
                # One clearly labelled, harmless test image to the configured owner only.
                response = await transport.client.post(f"https://api.telegram.org/bot{config.token}/sendPhoto",
                    data={"chat_id": str(owner), "caption": "Heimdall Phase 3 연결 테스트 · 테스트용 빨간 정사각형"},
                    files={"photo": ("phase3-fixture.png", red_png(), "image/png")})
                payload = response.json()
                if not payload.get("ok"):
                    raise RuntimeError("telegram_fixture_upload_failed")
                photo = payload["result"]["photo"]
            else:
                photo = [{"file_id": "fixture_1", "width": 48, "height": 48, "file_size": len(red_png())}]
            message = {"message_id": 2, "from": {"id": owner}, "chat": {"id": owner, "type": "private"},
                "photo": photo, "caption": "Name the dominant color in the attached image. Reply only RED or OTHER. Do not use tools."}
            await bridge.handle({"update_id": 2, "message": message})
            asset = bridge.assets.get(1, owner, owner)
            await bridge.work_once()
            job = store.job(1)
            result = {"download": asset["state"] == "stored", "hash": len(asset["sha256"] or "") == 64,
                "image_turn": bool(job and job["state"] == "completed" and "RED" in (job["response_text"] or "").upper()),
                "telegram_download": "real" if telegram else "fixture"}
            result["status"] = "passed" if result["download"] and result["hash"] and result["image_turn"] else "failed"
            return result
        finally:
            await adapter.close()
            await transport.close()
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telegram", action="store_true", help="Send one labelled fixture to configured owner and download it")
    args = parser.parse_args()
    try:
        result = asyncio.run(smoke(args.telegram))
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__}
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
