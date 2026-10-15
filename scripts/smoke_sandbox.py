"""Probe the exact bridge permissions profile using harmless disposable fixtures."""
import asyncio
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from heimdall_bridge.codex import CodexAdapter
from heimdall_bridge.config import Config

ROOT = Path(__file__).resolve().parents[1]

PROBE = r'''
import errno,json,os,pathlib,socket,sys
denied=pathlib.Path(sys.argv[1])
allowed=pathlib.Path(sys.argv[2])
result={"allowed_read":False,"denied_read":False,"write_blocked":False,"network_blocked":False}
try:
    result["allowed_read"]=bool(allowed.read_text())
except OSError:
    pass
try:
    denied.read_text()
except OSError as e:
    result["denied_read"]=e.errno in (errno.EACCES,errno.EPERM)
try:
    allowed.parent.joinpath("sandbox-write-must-fail.txt").write_text("harmless probe")
except OSError as e:
    result["write_blocked"]=e.errno in (errno.EACCES,errno.EPERM,errno.EROFS)
try:
    with socket.socket() as s:
        s.settimeout(1)
        result["network_blocked"]=s.connect_ex(("1.1.1.1",443)) in (errno.EACCES,errno.EPERM)
except OSError as e:
    result["network_blocked"]=e.errno in (errno.EACCES,errno.EPERM)
result["secret_env_absent"]="HEIMDALL_TEST_SECRET" not in os.environ
print(json.dumps(result))
'''


async def smoke():
    config = Config.load(ROOT / "config.toml", live=False)
    (ROOT / "artifacts").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sandbox-probe-", dir=ROOT / "artifacts") as directory:
        config = replace(config, data_dir=Path(directory))
        denied = config.data_dir / "denied-fixture.txt"
        denied.write_text("harmless disposable sentinel")
        adapter = CodexAdapter(config)
        env = adapter.child_env()
        proc = await asyncio.create_subprocess_exec(config.codex_bin, "sandbox", "--permission-profile",
            adapter.PERMISSION_PROFILE, *adapter.policy_args(), "--", "/usr/bin/python3", "-c", PROBE,
            str(denied), str(ROOT / "README.md"), cwd=config.cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await asyncio.wait_for(proc.communicate(), 30)
        if proc.returncode:
            return {"status": "failed", "exit_code": proc.returncode, "details": "stderr suppressed"}
        result = json.loads(output)
        result["status"] = "passed" if all(result.values()) else "failed"
        return result


if __name__ == "__main__":
    result = asyncio.run(smoke())
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
