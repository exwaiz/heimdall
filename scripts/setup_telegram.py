"""Import a dedicated Telegram token privately and inspect only safe bot/owner metadata."""
import argparse
import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path

from heimdall_bridge.transport import TelegramTransport

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = Path.home() / ".config/heimdall/bridge.env"


def existing_env():
    if DESTINATION.is_symlink() or DESTINATION.stat().st_mode & 0o077:
        raise ValueError("private_regular_env_file_required")
    values = {}
    for line in DESTINATION.read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return values


async def inspect_existing(owner_id=None):
    values = existing_env()
    transport = TelegramTransport(values["TELEGRAM_BOT_TOKEN"])
    try:
        await transport.initialize()
        updates = await transport.call("getUpdates", {"timeout": 0, "limit": 100, "allowed_updates": ["message"]})
        starts = {}
        users = set()
        for item in updates:
            message = item.get("message", {})
            user, chat = message.get("from", {}), message.get("chat", {})
            if (chat.get("type") != "private" or type(user.get("id")) is not int
                or user["id"] != chat.get("id") or user.get("is_bot")
                or any(k in message for k in ("forward_origin", "forward_from", "sender_chat"))):
                continue
            users.add(user["id"])
            text = message.get("text", "")
            age = time.time() - message.get("date", 0)
            if text in {"/start", "/start@" + transport.username} and -60 <= age <= 600:
                starts[user["id"]] = {"user_id": user["id"], "chat_id": chat["id"],
                    "update_id": item["update_id"], "age_seconds": round(age)}
        if owner_id is not None:
            # Operator must explicitly select the observed recent owner; never first-caller enrollment.
            if set(starts) != {owner_id} or users != {owner_id}:
                raise ValueError("owner_start_missing_or_ambiguous")
            old = (values.get("TELEGRAM_ALLOWED_USER_ID"), values.get("TELEGRAM_ALLOWED_CHAT_ID"))
            if old not in {("0", "0"), (str(owner_id), str(owner_id))}:
                raise ValueError("existing_owner_differs")
            values["TELEGRAM_ALLOWED_USER_ID"] = str(owner_id)
            values["TELEGRAM_ALLOWED_CHAT_ID"] = str(owner_id)
            fd, name = tempfile.mkstemp(prefix=".bridge-env-", dir=DESTINATION.parent)
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write("".join(k + "=" + v + "\n" for k, v in values.items()))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, DESTINATION)
            finally:
                if Path(name).exists():
                    Path(name).unlink()
        return {"bot_username": transport.username, "recent_start_candidates": list(starts.values()),
                "private_user_count": len(users), "owner_bound": owner_id is not None,
                "updates_acknowledged": False}
    finally:
        await transport.close()


async def setup(source):
    if source.is_symlink() or not source.is_file():
        raise ValueError("regular_token_file_required")
    os.chmod(source, 0o600)
    token = source.read_text().strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("invalid_token_format")
    transport = TelegramTransport(token)
    try:
        await transport.initialize()
        updates = await transport.call("getUpdates", {"timeout": 0, "limit": 100, "allowed_updates": ["message"]})
        candidates = set()
        for item in updates:
            message = item.get("message", {})
            user, chat = message.get("from", {}), message.get("chat", {})
            if (chat.get("type") == "private" and type(user.get("id")) is int
                and user["id"] == chat.get("id") and not user.get("is_bot")
                and not message.get("forward_origin")):
                candidates.add(user["id"])
        # Observed users are candidates only. Never auto-authorize the first caller.
        directory = DESTINATION.parent
        if directory.is_symlink():
            raise ValueError("symlink_config_directory_forbidden")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        contents = (f"TELEGRAM_BOT_TOKEN={token}\n"
            "TELEGRAM_ALLOWED_USER_ID=0\nTELEGRAM_ALLOWED_CHAT_ID=0\n"
            f"CODEX_BIN={Path.home()}/.local/bin/codex\nCODEX_HOME={Path.home()}/.codex\n"
            f"HEIMDALL_DATA_DIR={Path.home()}/.local/share/heimdall\n")
        fd = os.open(DESTINATION, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        return {"bot_username": transport.username, "token_verified": True,
            "config_path": str(DESTINATION), "config_mode": "0600",
            "owner_candidates": sorted(candidates), "owner_authorized": False,
            "updates_acknowledged": False, "message_sent": False}
    finally:
        await transport.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "api_bot_key")
    parser.add_argument("--inspect-existing", action="store_true")
    parser.add_argument("--owner-id", type=int)
    args = parser.parse_args()
    try:
        operation = inspect_existing(args.owner_id) if args.inspect_existing else setup(args.source)
        print(json.dumps(asyncio.run(operation), indent=2))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1)
