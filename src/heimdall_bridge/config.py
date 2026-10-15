import os
import re
import shutil
import tomllib
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    cwd: Path
    data_dir: Path
    schema_dir: Path
    codex_home: Path
    codex_bin: str = "codex"
    user_id: int = 0
    chat_id: int = 0
    token: str = field(default="", repr=False)
    max_queue: int = 30
    runtime: float = 600
    rpc_timeout: float = 30
    min_disk_bytes: int = 100 * 1024 * 1024
    write_roots: tuple[Path, ...] = ()
    approval_timeout: float = 120
    temporary_access_until: float = 0
    enable_network_profile: bool = False

    @classmethod
    def load(cls, path, *, live=True):
        path = Path(path).resolve(strict=True)
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        tracking = raw["session_tracking"]
        cwd = Path(tracking["cwd"])
        if tracking.get("match") != "exact" or not cwd.is_absolute() or not cwd.is_dir():
            raise ConfigError("session_tracking must be an existing absolute cwd with exact matching")
        cwd = cwd.resolve(strict=True)
        if cwd == Path("/"):
            raise ConfigError("Root workspace forbidden")
        data = Path(os.environ.get("HEIMDALL_DATA_DIR", Path.home() / ".local/share/heimdall"))
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        if not data.is_absolute() or not codex_home.is_absolute() or not codex_home.is_dir():
            raise ConfigError("Absolute data_dir and existing CODEX_HOME required")
        data = data.resolve()
        codex_home = codex_home.resolve()
        protected = [codex_home, Path.home() / "personal-agent", Path("/var/lib/kakao-radar"), Path("/etc/kakao-radar")]
        if data in (Path("/"), Path.home(), cwd) or any(data.is_relative_to(p.resolve()) for p in protected):
            raise ConfigError("Unsafe data directory")
        bridge = raw.get("bridge", {})
        if bridge.get("permission_mode", "readonly") != "readonly":
            raise ConfigError("P0 supports readonly only")
        try:
            user_id = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "0"))
            chat_id = int(os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "0"))
        except ValueError:
            raise ConfigError("Numeric owner IDs required") from None
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if live and (user_id <= 0 or chat_id != user_id or not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token)):
            raise ConfigError("Dedicated bot token and matching private owner user/chat IDs required")
        codex = os.environ.get("CODEX_BIN", "codex")
        if not shutil.which(codex):
            raise ConfigError("Codex executable unavailable")
        max_queue = int(bridge.get("max_queue_length", 30))
        runtime = float(bridge.get("max_job_runtime", 600))
        if not 1 <= max_queue <= 100 or not 10 <= runtime <= 3600:
            raise ConfigError("Queue/runtime bounds invalid")
        timeout = float(bridge.get("approval_timeout", 120))
        if not 15 <= timeout <= 600:
            raise ConfigError("Approval timeout must be 15~600 seconds")
        temporary_until = 0
        expiry = bridge.get('temporary_access_until')
        if expiry is not None and live:
            try:
                parsed = datetime.fromisoformat(expiry)
                if parsed.tzinfo is None:
                    raise ValueError()
                deadline = parsed.timestamp()
                remaining = deadline - time.time()
                if remaining > 1800:
                    raise ValueError()
                temporary_until = deadline if remaining > 0 else 0
            except (ValueError, TypeError, OverflowError):
                raise ConfigError('temporary_access_until must be timezone-aware and at most 30 minutes ahead') from None
        roots = []
        project = path.parent
        protected.extend([data, Path.home() / ".ssh", Path.home() / ".config",
                          project / "src", project / "scripts", project / "deploy",
                          project / "schemas", project / "config.toml", project / "api_bot_key"])
        for value in raw.get("security", {}).get("write_workspaces", []):
            root = Path(value)
            if not root.is_absolute() or not root.is_dir():
                raise ConfigError("Write workspace must be an existing absolute directory")
            root = root.resolve(strict=True)
            if root == cwd or not root.is_relative_to(cwd) or any(
                root.is_relative_to(p.resolve()) or p.resolve().is_relative_to(root) for p in protected):
                raise ConfigError("Write workspace overlaps protected paths")
            roots.append(root)
        return cls(cwd, data, path.parent / "schemas", codex_home, codex, user_id,
                   chat_id, token, max_queue, runtime, write_roots=tuple(roots), approval_timeout=timeout,
                   temporary_access_until=temporary_until)

    def temporary_active(self):
        return self.temporary_access_until > time.time()

    def temporary_protected(self):
        project = self.schema_dir.parent
        return (self.codex_home, self.data_dir, Path.home() / 'personal-agent',
            Path.home() / '.ssh', Path.home() / '.config', Path.home() / '.aws', Path.home() / '.gnupg',
            Path.home() / '.local/bin', Path.home() / '.local/share/router-browser-venv',
            Path.home() / '.cache/ms-playwright', project,
            Path('/etc'), Path('/var'), Path('/proc'),
            *(Path.home() / name for name in ('.env', '.env.local', '.netrc', '.npmrc', '.pypirc', '.git-credentials',
                                            '.bashrc', '.profile', '.bash_profile', '.zshrc', '.zprofile')))

    def writable(self, value):
        path = Path(value)
        if not path.is_absolute():
            return False
        try:
            resolved = path.resolve()
            if self.temporary_active():
                ordinary = resolved.is_relative_to(self.cwd) or resolved.is_relative_to(Path('/tmp'))
                if not ordinary:
                    return False
                # Only explicit workspaces can reopen our protected project tree.
                for protected in self.temporary_protected():
                    if resolved.is_relative_to(protected.resolve()):
                        return protected == self.schema_dir.parent and any(resolved.is_relative_to(r) for r in self.write_roots)
                return True
            return any(resolved.is_relative_to(root) for root in self.write_roots)
        except (OSError, RuntimeError):
            return False

    def check_thread(self, thread):
        raw = thread.get("cwd")
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise ConfigError("Thread cwd missing")
        if Path(raw).resolve(strict=True) != self.cwd:
            raise ConfigError("Thread outside tracked workspace")

