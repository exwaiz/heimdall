import asyncio
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

from .codex import CodexAdapter


async def prepare(config, record, data):
    """Decode only an owned verified snapshot in an offline restricted subprocess."""
    with tempfile.TemporaryDirectory(prefix="extract-", dir=config.data_dir) as directory:
        directory = Path(directory)
        source = directory / "input.bin"
        fd = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        project = Path(__file__).resolve().parents[2]
        rules = {":minimal": "read", str(project / ".venv"): "read",
                 str(project / "src"): "read", str(config.codex_home / "packages"): "read",
                 str(directory): "read"}
        filesystem = ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in rules.items())
        profile = "{filesystem={" + filesystem + "},network={enabled=false}}"
        env = CodexAdapter(config).child_env()
        env.update(PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
        process = await asyncio.create_subprocess_exec(config.codex_bin, "sandbox", "--permission-profile", "heimdall_extract",
            "-c", "permissions.heimdall_extract=" + profile, "--", sys.executable,
            "-m", "heimdall_bridge.extract_worker", record["kind"], record["mime_type"], str(source),
            cwd=config.cwd, env=env, start_new_session=True,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            async with asyncio.timeout(50):
                chunks, size = [], 0
                while chunk := await process.stdout.read(65536):
                    size += len(chunk)
                    if size > 4000000:
                        raise ValueError("extraction_output_limit")
                    chunks.append(chunk)
                await process.wait()
            if process.returncode:
                raise ValueError("extraction_failed")
            result = json.loads(b"".join(chunks))
            if not isinstance(result, dict) or result.get("error"):
                raise ValueError("extraction_failed")
            return result
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
