"""Regression coverage for 4557c7a and absolute hook command generation."""
import ntpath
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import ROOT, Sandbox

from yizhan import adapters, brief, gitio, sync


class SyncIdentity(Sandbox):
    def setUp(self):
        super().setUp()
        self.a, self.b = self.make_pair()
        self.commit(self.a, "draft.txt", "base draft\n")
        self.commit(self.a, "shared.txt", "base shared\n")
        gitio.git(self.a.root, "push", "-q", "origin", "main")
        sync.pull_only(self.b)
        for led in (self.a, self.b):
            self.assertNotEqual(
                gitio.git(led.root, "var", "GIT_COMMITTER_IDENT", check=False).returncode, 0,
                "the regression must run without a usable git identity",
            )

    def commit(self, led, path, text):
        (led.root / path).write_text(text, encoding="utf-8")
        gitio.git(led.root, "add", "--", path)
        gitio.git(led.root, *gitio.identity_args(led.root, led.machine),
                  "commit", "-q", "--no-verify", "-m", path)

    def diverge(self):
        self.commit(self.a, "remote.txt", "remote work\n")
        gitio.git(self.a.root, "push", "-q", "origin", "main")
        self.commit(self.b, "local.txt", "local work\n")

    def dirty_draft(self):
        path = self.b.root / "draft.txt"
        path.write_text("uncommitted work\n", encoding="utf-8")
        return path

    def assert_draft_restored(self, path):
        self.assertEqual(path.read_text(encoding="utf-8"), "uncommitted work\n")
        self.assertEqual(gitio.git(self.b.root, "stash", "list").stdout, "")
        self.assertEqual(gitio.changed_paths(self.b.root), ["draft.txt"])

    def test_diverged_pull_rebases_with_fallback_identity_and_restores_autostash(self):
        self.diverge()
        old_head = gitio.head(self.b.root)
        author = gitio.git(self.b.root, "log", "-1", "--format=%an <%ae>").stdout.strip()
        draft = self.dirty_draft()
        sync.pull_only(self.b)
        self.assertNotEqual(gitio.head(self.b.root), old_head)
        self.assertEqual(gitio.git(self.b.root, "log", "-1", "--format=%an <%ae>").stdout.strip(), author)
        self.assertEqual(gitio.git(self.b.root, "log", "-1", "--format=%cn <%ce>").stdout.strip(),
                         "yizhan linux <linux@yizhan.invalid>")
        self.assertEqual((self.b.root / "remote.txt").read_text(encoding="utf-8"), "remote work\n")
        self.assertEqual((self.b.root / "local.txt").read_text(encoding="utf-8"), "local work\n")
        self.assert_draft_restored(draft)

    def test_rebase_preserves_configured_committer(self):
        self.diverge()
        old_head = gitio.head(self.b.root)
        gitio.git(self.b.root, "config", "user.name", "Configured User")
        gitio.git(self.b.root, "config", "user.email", "configured@example.invalid")
        sync.pull_only(self.b)
        self.assertNotEqual(gitio.head(self.b.root), old_head)
        self.assertEqual(gitio.git(self.b.root, "log", "-1", "--format=%cn <%ce>").stdout.strip(),
                         "Configured User <configured@example.invalid>")

    def test_conflicting_rebase_aborts_and_restores_dirty_work_without_identity(self):
        self.commit(self.a, "shared.txt", "remote version\n")
        gitio.git(self.a.root, "push", "-q", "origin", "main")
        self.commit(self.b, "shared.txt", "local version\n")
        old_head = gitio.head(self.b.root)
        draft = self.dirty_draft()
        with self.assertRaises(gitio.GitError):
            sync.pull_only(self.b)
        self.assertEqual(gitio.head(self.b.root), old_head)
        self.assertEqual((self.b.root / "shared.txt").read_text(encoding="utf-8"), "local version\n")
        self.assert_draft_restored(draft)
        for name in ("rebase-merge", "rebase-apply"):
            self.assertFalse((self.b.root / ".git" / name).exists())

    def test_fast_forward_with_merge_autostash_needs_no_identity(self):
        self.commit(self.a, "remote.txt", "remote work\n")
        gitio.git(self.a.root, "push", "-q", "origin", "main")
        gitio.git(self.b.root, "config", "merge.autoStash", "true")
        draft = self.dirty_draft()
        sync.pull_only(self.b)
        self.assertEqual(gitio.head(self.b.root), gitio.head(self.a.root))
        self.assert_draft_restored(draft)


class AbsoluteCommands(Sandbox):
    @unittest.skipUnless(shutil.which("bash"), "requires Bash to check Claude hook parsing")
    def test_windows_installed_command_survives_bash_parsing(self):
        # Parsing is tested with real Bash; this is not a Windows execution test.
        exe = r"C:\Tools\Scripts\yizhan.exe"
        with mock.patch.object(brief, "os", SimpleNamespace(name="nt", path=ntpath)), \
                mock.patch.object(brief.shutil, "which", return_value=exe):
            command = brief.cli_prefix()
        hook = adapters.hook_config("claude", command)["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        parsed = subprocess.run([shutil.which("bash"), "--noprofile", "--norc", "-c",
                                 "set -- " + hook + '; printf "%s\\n" "$@"'],
                                capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual([ntpath.normpath(parsed[0]), *parsed[1:]],
                         [exe, "hook", "--agent", "claude"])

    @unittest.skipUnless(os.name == "posix", "requires a POSIX source launcher")
    def test_source_launcher_command_runs_without_python_on_path(self):
        empty_path = str(self.folder("empty-path"))
        with mock.patch.dict(os.environ, {"PATH": str(ROOT / "bin")}):
            command = brief.cli_prefix()
        env = dict(os.environ, PATH=empty_path)
        result = subprocess.run(["/bin/sh", "-c", command + " --help"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_source_fallback_runs_without_python_on_path(self):
        empty_path = str(self.folder("empty-path"))
        with mock.patch.dict(os.environ, {"PATH": empty_path}):
            command = brief.cli_prefix()
        self.assertEqual(shlex.split(command), [sys.executable, str(ROOT / "bin" / "yizhan")])
        result = subprocess.run(["/bin/sh", "-c", command + " --help"],
                                env=dict(os.environ, PATH=empty_path), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
