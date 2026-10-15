import asyncio
import re
import os
import stat
import hashlib
from pathlib import PurePosixPath

import httpx


class DeliveryUncertain(RuntimeError):
    pass


class TransportError(RuntimeError):
    pass


def sanitize(text, secrets=()):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}", "[redacted]", text)
    return re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}", "[redacted]", text)


def chunks(text, budget=3400):
    # UTF-16 units bound astral characters as Telegram clients count them.
    parts, current, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > budget:
            parts.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        parts.append("".join(current))
    if len(parts) <= 1:
        return parts or ["(빈 응답)"]
    return [f"({i}/{len(parts)}) {part}" for i, part in enumerate(parts, 1)]


class FakeTelegramTransport:
    def __init__(self):
        self.messages = []
        self.buttons = []
        self.callback_answers = []
        self.photos = []
        self.username = "HeimdallTestBot"
        self.connected = True

    async def send(self, chat, text, reply_markup=None):
        self.messages.append((chat, text))
        if reply_markup:
            self.buttons.append((chat, len(self.messages), reply_markup))
        return len(self.messages)

    async def answer_callback(self, callback_id, text):
        self.callback_answers.append((callback_id, text))

    async def send_photo(self, chat, data, caption, mime):
        self.photos.append((chat, data, caption, mime))
        return len(self.photos) + len(self.messages)

    async def close(self):
        pass


class TelegramTransport:
    def __init__(self, token):
        self.token = token
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(40), trust_env=False)
        self.username = None
        self.connected = False

    async def call(self, method, params):
        for attempt in range(4):
            try:
                response = await self.client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params)
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                self.connected = False
                if method == "sendMessage":
                    raise DeliveryUncertain("telegram_delivery_uncertain") from None
                raise TransportError("telegram_network_error") from None
            if payload.get("ok"):
                self.connected = True
                return payload["result"]
            if payload.get("error_code") == 429 and attempt < 3:
                delay = payload.get("parameters", {}).get("retry_after", 1)
                await asyncio.sleep(min(max(float(delay), 1), 60))
                continue
            self.connected = False
            raise TransportError(f"telegram_api_{payload.get('error_code', 'unknown')}")
        raise TransportError("telegram_retry_exhausted")

    async def initialize(self):
        result = await self.call("getMe", {})
        self.username = result["username"]
        webhook = await self.call("getWebhookInfo", {})
        if webhook.get("url"):
            raise TransportError("dedicated_bot_has_webhook; not modified")

    async def updates(self, offset):
        return await self.call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]})

    async def send(self, chat, text, reply_markup=None):
        params = {"chat_id": chat, "text": text, "link_preview_options": {"is_disabled": True}}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        result = await self.call("sendMessage", params)
        return result["message_id"]

    async def answer_callback(self, callback_id, text):
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def send_photo(self, chat, data, caption, mime):
        if mime not in {'image/png', 'image/jpeg'} or not 0 < len(data) <= 10_000_000:
            raise TransportError('invalid_outgoing_photo')
        filename = 'image.png' if mime == 'image/png' else 'image.jpg'
        for attempt in range(4):
            try:
                response = await self.client.post(f'https://api.telegram.org/bot{self.token}/sendPhoto',
                    data={'chat_id': str(chat), 'caption': caption},
                    files={'photo': (filename, data, mime)}, follow_redirects=False)
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                self.connected = False
                raise DeliveryUncertain('telegram_photo_delivery_uncertain') from None
            if not isinstance(payload, dict):
                raise DeliveryUncertain('telegram_photo_delivery_uncertain')
            if payload.get('ok'):
                if not isinstance(payload.get('result'), dict) or type(payload['result'].get('message_id')) is not int:
                    raise DeliveryUncertain('telegram_photo_delivery_uncertain')
                self.connected = True
                return payload['result']['message_id']
            if payload.get('error_code') == 429 and attempt < 3:
                delay = payload.get('parameters', {}).get('retry_after', 1)
                await asyncio.sleep(min(max(float(delay), 1), 60))
                continue
            self.connected = False
            # A proxy/server 5xx might follow an accepted upload. Do not retry.
            if response.status_code >= 500 or payload.get('error_code', 0) >= 500:
                raise DeliveryUncertain('telegram_photo_delivery_uncertain')
            raise TransportError(f"telegram_api_{payload.get('error_code', 'unknown')}")

    async def get_file(self, file_id):
        if not isinstance(file_id, str) or not file_id or len(file_id) > 1024:
            raise TransportError("invalid_file_id")
        return await self.call("getFile", {"file_id": file_id})

    async def download_file(self, file_path, destination, max_bytes, *, reserved_identity=None):
        # getFile supplies only a relative Telegram file path, never an arbitrary URL.
        if (not isinstance(file_path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,1024}", file_path)
            or PurePosixPath(file_path).is_absolute() or any(p in {"", ".", ".."} for p in file_path.split("/"))
            or type(max_bytes) is not int or not 1 <= max_bytes <= 20 * 1024 * 1024):
            raise TransportError("unsafe_file_download")
        created = False
        try:
            async with self.client.stream("GET", f"https://api.telegram.org/file/bot{self.token}/{file_path}",
                                          follow_redirects=False) as response:
                if response.status_code != 200:
                    raise TransportError("telegram_file_download_failed")
                declared = response.headers.get("content-length")
                if declared is not None and (not declared.isdecimal() or int(declared) > max_bytes):
                    raise TransportError("attachment_too_large")
                if reserved_identity is None:
                    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                else:
                    fd = os.open(destination, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                        or (info.st_dev, info.st_ino) != reserved_identity):
                        os.close(fd)
                        raise TransportError("unsafe_reserved_download")
                    os.ftruncate(fd, 0)
                with os.fdopen(fd, "wb") as output:
                    created = True
                    count = 0
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        count += len(chunk)
                        if count > max_bytes:
                            raise TransportError("attachment_too_large")
                        output.write(chunk)
                if count == 0:
                    raise TransportError("empty_attachment")
                return count
        except (httpx.HTTPError, OSError, ValueError, TransportError) as exc:
            if created:
                destination.unlink(missing_ok=True)
            code = str(exc) if isinstance(exc, TransportError) else "telegram_file_download_failed"
            raise TransportError(code) from None
        except asyncio.CancelledError:
            if created:
                destination.unlink(missing_ok=True)
            raise

    async def close(self):
        await self.client.aclose()


async def deliver(store, transport, token=""):
    # Persist each chunk separately before any network side effect.
    rows = store.conn.execute("SELECT * FROM outbox WHERE state='pending' ORDER BY coalesce(parent_id,delivery_id),photo IS NOT NULL,delivery_id").fetchall()
    for row in rows:
        body = sanitize(row["body"], (token,))
        # Numbered chunks already include a small prefix; do not split them again.
        pieces = [body] if row['photo'] is not None or len(body.encode("utf-16-le")) // 2 <= 3500 else chunks(body)
        if len(pieces) > 1:
            with store.transaction():
                for i, piece in enumerate(pieces):
                    store.reply(row["chat_id"], piece, f"{row['dedupe']}:part:{i}", row["parent_id"] or row["delivery_id"])
                store.conn.execute("UPDATE outbox SET state='split' WHERE delivery_id=?", (row["delivery_id"],))
            return  # Flush this answer's new chunks before any later messages.
        store.conn.execute("UPDATE outbox SET state='sending' WHERE delivery_id=?", (row["delivery_id"],))
        try:
            if row['photo'] is not None:
                data = bytes(row['photo'])
                if hashlib.sha256(data).hexdigest() != row['photo_sha256']:
                    raise TransportError('outgoing_photo_hash_mismatch')
                message_id = await transport.send_photo(row['chat_id'], data, body[:450], row['photo_mime'])
            else:
                message_id = await transport.send(row["chat_id"], pieces[0])
        except DeliveryUncertain:
            store.conn.execute("UPDATE outbox SET state='uncertain' WHERE delivery_id=?", (row["delivery_id"],))
        except TransportError:
            store.conn.execute("UPDATE outbox SET state='failed' WHERE delivery_id=?", (row["delivery_id"],))
        else:
            store.conn.execute("UPDATE outbox SET state='sent',message_id=? WHERE delivery_id=?", (message_id, row["delivery_id"]))

