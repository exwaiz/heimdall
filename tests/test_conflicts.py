import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from heimdall_bridge.bridge import external_cli_busy


class SessionConflictTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="heimdall-proc-tests-")
        self.root = Path(self.directory.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.codex_home = self.root / "codex-home"
        (self.codex_home / "sessions").mkdir(parents=True)
        self.environment = patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def process(self, args, *, thread=None, parent=200, pid=123):
        proc = self.proc / str(pid)
        proc.mkdir()
        (proc / "comm").write_text("codex\n")
        (proc / "cmdline").write_bytes(b"\x00".join(a.encode() for a in args) + b"\x00")
        (proc / "stat").write_text(f"{pid} (codex) S {parent} 0 0")
        (proc / "cwd").symlink_to(Path("/home/haaland"))
        (proc / "fd").mkdir()
        if thread:
            rollout = self.codex_home / "sessions" / f"rollout-date-{thread}.jsonl"
            rollout.touch()
            (proc / "fd" / "7").symlink_to(rollout)
        return proc

    def busy(self):
        return external_cli_busy("target-thread", Path("/home/haaland"), proc_root=self.proc, own_pid=999)

    def test_unidentified_cli_in_same_directory_is_allowed(self):
        self.process(["codex"])
        self.assertFalse(self.busy())

    def test_other_thread_resume_in_same_directory_is_allowed(self):
        self.process(["codex", "resume", "other-thread"], thread="other-thread")
        self.assertFalse(self.busy())

    def test_selected_thread_explicit_resume_is_blocked(self):
        self.process(["codex", "resume", "target-thread"])
        self.assertTrue(self.busy())

    def test_selected_thread_exec_resume_is_blocked(self):
        self.process(["codex", "exec", "resume", "target-thread"])
        self.assertTrue(self.busy())

    def test_selected_thread_open_rollout_is_blocked(self):
        self.process(["codex", "resume", "--last"], thread="target-thread")
        self.assertTrue(self.busy())

    def test_own_app_server_open_rollout_is_allowed(self):
        self.process(["codex", "app-server"], thread="target-thread", parent=999)
        self.assertFalse(self.busy())

    def test_idle_external_app_server_handle_is_not_an_execution_lock(self):
        self.process(["codex", "app-server"], thread="target-thread", parent=200)
        self.assertFalse(self.busy())

    def test_exited_or_unreadable_process_does_not_block(self):
        proc = self.proc / "456"
        proc.mkdir()
        self.assertFalse(self.busy())


if __name__ == "__main__":
    unittest.main()
