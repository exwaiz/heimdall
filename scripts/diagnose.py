"""Read-only inventory; never read credentials or process command lines."""
import hashlib
import json
import os
import platform
import pwd
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def run(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if args[1:] == ("login", "status"):
            combined = result.stdout + result.stderr
            known = next((s for s in ("Logged in using ChatGPT", "Not logged in") if s in combined), None)
            return {"exit_code": result.returncode,
                    "status": known or "unknown authentication method (details suppressed)"}
        return {"exit_code": result.returncode, "stdout": result.stdout.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error_type": type(exc).__name__}


def inventory():
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex")).resolve()
    codex = os.environ.get("CODEX_BIN", "codex")
    model = Path("/sys/firmware/devicetree/base/model")
    units = run("systemctl", "show", "kakao-radar-*.service", "--no-pager",
                "--property=Id,User,Group,WorkingDirectory,FragmentPath,ActiveState,SubState,MainPID,MemoryCurrent")
    fingerprints = {}
    for line in units.get("stdout", "").splitlines():
        if line.startswith("FragmentPath="):
            path = Path(line.split("=", 1)[1])
            if path.is_file():
                fingerprints[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    sessions = codex_home / "sessions"
    paths = list(sessions.rglob("*.jsonl")) if sessions.is_dir() else []
    disk = shutil.disk_usage(home)
    return {
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "user": pwd.getpwuid(os.getuid()).pw_name, "uid": os.getuid(),
        "gid": os.getgid(), "home": str(home), "cwd": os.getcwd(),
        "hostname": platform.node(), "uname": platform.uname()._asdict(),
        "hardware_model": model.read_bytes().rstrip(b"\x00").decode() if model.exists() else "unknown",
        "os_release": platform.freedesktop_os_release(),
        "python": platform.python_version(), "codex_bin": shutil.which(codex),
        "codex_version": run(codex, "--version"),
        "codex_login_status": run(codex, "login", "status"),
        "codex_home": str(codex_home), "codex_home_exists": codex_home.is_dir(),
        "codex_home_accessible": os.access(codex_home, os.R_OK | os.X_OK),
        "codex_home_mode": oct(codex_home.stat().st_mode & 0o777) if codex_home.exists() else None,
        "sessions_count": len(paths),
        "sessions_bytes": sum(p.stat().st_size for p in paths),
        "disk_total_bytes": disk.total, "disk_free_bytes": disk.free,
        "memory": run("free", "-h"), "kakao_units": units,
        "kakao_unit_sha256": fingerprints,
        "protected_paths": ["/home/haaland/personal-agent", "/etc/kakao-radar",
                            "/var/lib/kakao-radar", str(codex_home)],
        "backup": "not performed; sessions plus state DB require coordinated snapshot and separate authorization",
    }


if __name__ == "__main__":
    print(json.dumps(inventory(), ensure_ascii=False, indent=2))

