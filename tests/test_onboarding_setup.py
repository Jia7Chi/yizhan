"""Setup exercises only disposable ledgers, remotes and AI settings."""
import contextlib
import io
import os
import socket
import subprocess
import sys
from unittest import mock

from helpers import Sandbox

from yizhan import adapters, cli, gitio, ledger, setup, util


class SetupOnboarding(Sandbox):
    def setUp(self):
        super().setUp()
        self.work = self.folder("work")
        self.remote = self.tmp / "remote.git"
        gitio.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.root = self.tmp / "ledger"

    def run_setup(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(setup, "detect_agents", return_value={}), \
                mock.patch.object(setup, "gh_command") as gh, \
                mock.patch.object(setup.service, "install") as install, \
                mock.patch("builtins.input", side_effect=AssertionError("--yes must not prompt")):
            result = cli.main(["setup", "--yes", "--folder", str(self.work), *args])
        gh.assert_not_called()
        install.assert_not_called()
        return result, out.getvalue() + err.getvalue()

    def first_machine(self, host="first-host"):
        with mock.patch.object(socket, "gethostname", return_value=host):
            code, output = self.run_setup("--remote", str(self.remote), "--ledger", str(self.root),
                                          "--machine", "first", "--agent", "claude", "--agent", "codex")
        self.assertEqual(code, 0, output)
        return ledger.Ledger(self.root, "first")

    def test_yes_initializes_empty_remote_and_installs_only_local_hooks(self):
        led = self.first_machine()
        self.assertEqual(ledger.local_config(), (str(self.root), "first"))
        self.assertEqual([p["name"] for p in led.projects()], ["work"])
        self.assertEqual({s["address"] for s in led.seats()}, {"claude@first", "codex@first"})
        self.assertEqual(gitio.head(led.root), gitio.head(self.remote))
        self.assertEqual(gitio.changed_paths(led.root), [])
        for agent in ("claude", "codex"):
            self.assertTrue(adapters.hook_target(agent, self.work if agent == "claude" else None).is_file())
        self.assertFalse(led.seat("codex@first").get("autowake"))

    def test_yes_creates_first_ledger_without_a_remote(self):
        code, output = self.run_setup("--ledger", str(self.root), "--machine", "first", "--agent", "codex")
        self.assertEqual(code, 0, output)
        self.assertTrue((self.root / "yizhan.json").is_file())
        self.assertIsNotNone(gitio.head(self.root))
        self.assertFalse(gitio.has_remote(self.root))

    def test_yes_joins_second_machine_and_preserves_first_machine_files(self):
        first = self.first_machine()
        original = (self.root / "machines/first/machine.json").read_bytes()
        second_root = self.tmp / "second-ledger"
        with mock.patch.dict(os.environ, {"YIZHAN_HOME": str(self.tmp / "second-home")}), \
                mock.patch.object(socket, "gethostname", return_value="second-host"):
            code, output = self.run_setup("--remote", str(self.remote), "--ledger", str(second_root),
                                          "--machine", "second", "--agent", "codex")
            self.assertEqual(code, 0, output)
            self.assertEqual(ledger.local_config(), (str(second_root), "second"))
        second = ledger.Ledger(second_root, "second")
        self.assertEqual({m["machine"] for m in second.machines()}, {"first", "second"})
        self.assertEqual((second_root / "machines/first/machine.json").read_bytes(), original)
        self.assertEqual(second.project("work"), first.project("work"))
        self.assertEqual({s["address"] for s in second.seats()},
                         {"claude@first", "codex@first", "codex@second"})
        self.assertEqual(gitio.head(second_root), gitio.head(self.remote))

    def test_repeat_setup_unions_paths_projects_and_preserves_seat_settings(self):
        led = self.first_machine()
        led.update_seat("codex@first", owns=["tests/**"], command="custom-codex", wake="exec", focus="reviewing")
        old_created = led.seat("codex@first")["created"]
        original_hook = adapters.hook_target("codex").read_bytes()
        original_backups = set(adapters.hook_target("codex").parent.glob("*.bak*"))
        next_work = self.folder("next-work")
        for _ in range(2):
            code, output = self.run_setup("--folder", str(next_work), "--project", "next", "--agent", "codex")
            self.assertEqual(code, 0, output)
        profile = led.seat("codex@first")
        self.assertEqual(profile["paths"], [str(self.work), str(next_work)])
        self.assertEqual(profile["projects"], ["work", "next"])
        self.assertEqual(profile["created"], old_created)
        self.assertEqual(profile["owns"], ["tests/**"])
        self.assertEqual(profile["command"], "custom-codex")
        self.assertEqual(profile["wake"], "exec")
        self.assertEqual(profile["focus"], "reviewing")
        self.assertEqual(len(led.seats()), 2)
        self.assertEqual(adapters.hook_target("codex").read_bytes(), original_hook)
        self.assertEqual(set(adapters.hook_target("codex").parent.glob("*.bak*")), original_backups)

    def test_yes_refuses_machine_name_owned_by_another_host(self):
        self.first_machine()
        original = (self.root / "machines/first/machine.json").read_bytes()
        original_config = (util.config_home() / "local.json").read_bytes()
        with mock.patch.object(socket, "gethostname", return_value="second-host"):
            code, output = self.run_setup("--ledger", str(self.root), "--machine", "first", "--agent", "codex")
        self.assertEqual(code, 2, output)
        self.assertEqual((self.root / "machines/first/machine.json").read_bytes(), original)
        self.assertEqual((util.config_home() / "local.json").read_bytes(), original_config)

    def test_default_machine_avoids_other_host_and_reuses_same_host(self):
        base = {"darwin": "mac", "win32": "win"}.get(sys.platform, "linux")
        root = self.folder("names")
        util.write_json(root / "machines" / base / "machine.json", {"machine": base, "hostname": "first-host"})
        with mock.patch.object(socket, "gethostname", return_value="second-host.example"):
            self.assertEqual(setup.default_machine(root), "second-host")
        with mock.patch.object(socket, "gethostname", return_value="first-host"):
            self.assertEqual(setup.default_machine(root), base)

    def test_clone_failure_preserves_config(self):
        self.first_machine()
        original = (util.config_home() / "local.json").read_bytes()
        code, output = self.run_setup("--remote", str(self.tmp / "missing.git"),
                                      "--ledger", str(self.tmp / "failed"))
        self.assertEqual(code, 2, output)
        self.assertEqual((util.config_home() / "local.json").read_bytes(), original)

    def test_existing_nonempty_destination_is_not_overwritten(self):
        self.root.mkdir()
        sentinel = self.root / "README.md"
        sentinel.write_text("Keep this", encoding="utf-8")
        code, output = self.run_setup("--remote", str(self.remote), "--ledger", str(self.root))
        self.assertEqual(code, 2, output)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "Keep this")
        self.assertFalse((util.config_home() / "local.json").exists())

    def test_join_rejects_nonempty_remote_that_is_not_a_ledger(self):
        source = self.folder("unrelated-project")
        gitio.init(source)
        (source / "README.md").write_text("An unrelated project's documentation\n", encoding="utf-8")
        gitio.git(source, "add", "README.md")
        gitio.git(source, *gitio.identity_args(source, "test"), "commit", "-qm", "project")
        gitio.git(source, "remote", "add", "origin", str(self.remote))
        gitio.git(source, "push", "-q", "origin", "main")
        before = gitio.head(self.remote)
        code, output = self.run_setup("--remote", str(self.remote), "--ledger", str(self.root), "--machine", "first")
        with self.subTest("must not push over an unrelated repository"):
            self.assertEqual(gitio.head(self.remote), before)
        with self.subTest("must reject a non-ledger remote"):
            self.assertEqual(code, 2, output)
        with self.subTest("must not adopt it in local configuration"):
            self.assertFalse((util.config_home() / "local.json").exists())

    def test_gh_creation_requires_opt_in_and_uses_private_repository(self):
        led = self.make_single()
        for consent in (False, True):
            with self.subTest(consent=consent):
                ask = mock.Mock(yes=False)
                ask.confirm.return_value = consent
                ask.text.return_value = ""
                with mock.patch.object(setup, "gh_command", return_value="fake-gh"), \
                        mock.patch.object(setup, "remote_url", return_value="example"), \
                        mock.patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                    setup._remote_step(None, ask, led)
                if consent:
                    run.assert_called_once_with(["fake-gh", "repo", "create", led.root.name, "--private", "--source",
                                                 str(led.root), "--remote", "origin"],
                                                capture_output=True, text=True, timeout=120)
                else:
                    run.assert_not_called()

    def test_gh_failure_reports_error_and_accepts_manual_remote(self):
        led = self.make_single()
        ask = mock.Mock(yes=False)
        ask.confirm.return_value = True
        ask.text.return_value = str(self.remote)
        with mock.patch.object(setup, "gh_command", return_value="fake-gh"), \
                mock.patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="creation failed")), \
                mock.patch.object(gitio, "git") as git:
            setup._remote_step(None, ask, led)
        self.assertIn("creation failed", ask.warn.call_args.args[0])
        git.assert_called_once_with(led.root, "remote", "add", "origin", str(self.remote))
