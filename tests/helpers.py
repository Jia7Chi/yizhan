"""Shared test setup: every test gets fresh config and state folders, and a git that
ignores the developer's own git config (no identity, no signing, no hooks)."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yizhan import ledger as ledger_mod  # noqa: E402
from yizhan import sync  # noqa: E402


def _force_remove(func, path, _exc):
    # git marks object files read-only, which Windows refuses to delete.
    os.chmod(path, stat.S_IWRITE)
    func(path)


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="yizhan-test-")).resolve()
        env = {
            "YIZHAN_HOME": str(self.tmp / "home"),
            "YIZHAN_STATE": str(self.tmp / "state"),
            "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
            "CODEX_HOME": str(self.tmp / "codex"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            # Like a fresh CI machine: git may not guess a name and email from the OS.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "user.useConfigOnly",
            "GIT_CONFIG_VALUE_0": "true",
        }
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        for key in ("YIZHAN_LEDGER", "YIZHAN_MACHINE", "YIZHAN_SEAT"):
            os.environ.pop(key, None)

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, onerror=_force_remove)

    def folder(self, name):
        p = self.tmp / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def use_ledger(self, led):
        """Point open_local() (hooks, CLI) at this ledger."""
        os.environ["YIZHAN_LEDGER"] = str(led.root)
        os.environ["YIZHAN_MACHINE"] = led.machine

    def make_single(self, machine="mac"):
        led, _ = ledger_mod.create_or_join(self.tmp / f"ledger-{machine}", machine, "+08:00")
        sync.sync(led, quick=True, allow_shared=True)
        return led

    def make_pair(self):
        """A ledger on 'mac' and a clone on 'linux', joined through a bare repository."""
        bare = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        a, _ = ledger_mod.create_or_join(self.tmp / "a", "mac", "+08:00", str(bare))
        sync.sync(a, quick=True, allow_shared=True)
        subprocess.run(["git", "clone", "-q", str(bare), str(self.tmp / "b")], check=True)
        b, created = ledger_mod.create_or_join(self.tmp / "b", "linux")
        self.assertFalse(created)
        sync.sync(b, quick=True)
        sync.sync(a, quick=True)
        return a, b

    def both(self, *ledgers):
        """Sync each ledger in turn, twice, so every side has everything."""
        for _ in range(2):
            for led in ledgers:
                rep = sync.sync(led, quick=True)
                self.assertIsNone(rep["error"], rep["error"])
