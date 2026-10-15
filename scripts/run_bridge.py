"""Start the bridge with private env-file loading, without echoing credentials."""
import os

from setup_telegram import existing_env
from heimdall_bridge.__main__ import main


if __name__ == "__main__":
    try:
        values = existing_env()
        allowed = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID", "TELEGRAM_ALLOWED_CHAT_ID",
                   "CODEX_BIN", "CODEX_HOME", "HEIMDALL_DATA_DIR"}
        if set(values) - allowed:
            raise ValueError("unsupported_env_key")
        os.environ.update(values)
    except Exception as exc:
        print("Private configuration load failed: " + type(exc).__name__)
        raise SystemExit(1)
    raise SystemExit(main())
