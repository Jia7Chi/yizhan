"""Autowake CLI contracts, with all process/service operations replaced."""
import contextlib
import io
import os
import unittest
from unittest import mock

from helpers import Sandbox

from yizhan import adapters, cli, service, state, util


class AutowakeCLI(Sandbox):
    seat = "codex@mac"

    def setUp(self):
        super().setUp()
        self.led = self.make_single()
        self.use_ledger(self.led)
        self.led.add_seat("codex", "codex", paths=[str(self.led.root)],
                          command="fake-codex")
        self.led.add_seat("claude", "claude", paths=[str(self.folder("claude-work"))])
        self.install = self.enter_patch(mock.patch.object(service, "install"))
        self.uninstall = self.enter_patch(mock.patch.object(service, "uninstall"))
        self.installed = self.enter_patch(mock.patch.object(service, "installed", return_value=True))
        self.enter_patch(mock.patch.object(adapters, "check_command", return_value=(True, "test command")))
        self.enter_patch(mock.patch.object(cli.sys, "stdin", io.StringIO()))
        # plan() is real but may only describe paths inside this test's sandbox.
        self.enter_patch(mock.patch.object(service.Path, "home", return_value=self.folder("service-home")))
        # Sandbox already restores the environment in tearDown; a nested patch
        # cleaned up afterward would restore this test's temporary environment.
        os.environ["XDG_CONFIG_HOME"] = str(self.folder("xdg"))

    def enter_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def enable(self, *args):
        return self.run_cli("autowake", "on", "--as", self.seat, *args)

    def test_default_status_is_off_and_does_not_probe_service(self):
        code, output = self.run_cli("autowake", "status")
        self.assertEqual(code, 0, output)
        self.assertIn("codex@mac：自动叫醒关着", output)
        self.installed.assert_not_called()
        self.install.assert_not_called()

    def test_noninteractive_enable_needs_explicit_yes_without_mutation(self):
        before = self.led.seat(self.seat)
        code, output = self.enable()
        self.assertEqual(code, 2, output)
        self.assertIn("--yes", output)
        self.assertIn("账号的额度", output)
        self.assertEqual(self.led.seat(self.seat), before)
        self.install.assert_not_called()

    def test_interactive_decline_preserves_profile(self):
        before = self.led.seat(self.seat)
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="n"):
            code, output = self.enable()
        self.assertEqual(code, 1, output)
        self.assertEqual(self.led.seat(self.seat), before)
        self.install.assert_not_called()

    def test_claude_cannot_enable_even_with_yes(self):
        before = self.led.seat("claude@mac")
        code, output = self.run_cli("autowake", "on", "--as", "claude@mac", "--yes")
        self.assertEqual(code, 2, output)
        self.assertEqual(self.led.seat("claude@mac"), before)
        self.install.assert_not_called()

    def test_git_and_command_preconditions_leave_profile_unchanged(self):
        outside = self.folder("not-a-repository")
        self.led.update_seat(self.seat, workdir=str(outside))
        before = self.led.seat(self.seat)
        code, output = self.enable("--yes")
        self.assertEqual(code, 2, output)
        self.assertIn("不在 git 仓库", output)
        self.assertEqual(self.led.seat(self.seat), before)
        self.led.update_seat(self.seat, workdir=str(self.led.root))
        before = self.led.seat(self.seat)
        with mock.patch.object(adapters, "check_command", return_value=(False, "missing")):
            code, output = self.enable("--yes")
        self.assertEqual(code, 2, output)
        self.assertIn("missing", output)
        self.assertEqual(self.led.seat(self.seat), before)
        self.install.assert_not_called()

    def test_enable_accepts_git_subdirectory_and_installs_plan(self):
        subdir = self.led.root / "work-subdir"
        subdir.mkdir()
        self.led.update_seat(self.seat, workdir=str(subdir))
        code, output = self.enable("--yes", "--budget", "3", "--interval", "120")
        self.assertEqual(code, 0, output)
        prof = self.led.seat(self.seat)
        self.assertTrue(prof["autowake"])
        self.assertEqual(prof["wake"], "exec")
        self.assertEqual(prof["wake_budget_per_day"], 3)
        self.assertEqual(prof["autowake_interval"], 120)
        self.install.assert_called_once()
        self.assertIn("yizhan-wake-codex-mac", self.install.call_args.args[0]["name"])

    def test_no_service_enables_event_wakes_without_install(self):
        code, output = self.enable("--yes", "--no-service")
        self.assertEqual(code, 0, output)
        self.assertTrue(self.led.seat(self.seat)["autowake"])
        self.install.assert_not_called()
        self.assertIn("不装后台任务", output)

    def test_install_failure_reports_event_only_fallback(self):
        self.install.side_effect = util.YizhanError("test service failure")
        code, output = self.enable("--yes")
        self.assertEqual(code, 1, output)
        self.assertTrue(self.led.seat(self.seat)["autowake"])
        self.assertIn("后台任务没装上", output)
        self.assertIn("test service failure", output)

    def test_off_is_repeatable_and_preserves_unrelated_profile_fields(self):
        self.led.update_seat(self.seat, autowake=True, autowake_interval=120,
                             wake="exec", wake_budget_per_day=3, focus="review")
        for _ in range(2):
            code, output = self.run_cli("autowake", "off", "--as", self.seat)
            self.assertEqual(code, 0, output)
            prof = self.led.seat(self.seat)
            self.assertNotIn("autowake", prof)
            self.assertNotIn("autowake_interval", prof)
            self.assertEqual(prof["wake"], "queue")
            self.assertEqual(prof["focus"], "review")
            self.assertEqual(prof["wake_budget_per_day"], 3)
        self.assertEqual(self.uninstall.call_count, 2)

    def test_status_and_doctor_surface_failure_and_missing_service(self):
        self.led.update_seat(self.seat, autowake=True, wake="exec", wake_budget_per_day=3)
        today = self.led.local_date().date().isoformat()
        with state.edit(self.led, f"seat-{self.seat}") as st:
            st.update(wakes={today: 2}, last_wake_rc=-1, last_wake_error="run failed")
        self.installed.return_value = False
        code, output = self.run_cli("autowake", "status")
        self.assertEqual(code, 0, output)
        self.assertIn("2/3", output)
        self.assertIn("后台任务没在跑", output)
        self.assertIn("run failed", output)
        self.led.update_seat(self.seat, workdir=str(self.folder("no-git")))
        with mock.patch.object(adapters, "hooks_installed", return_value=True), \
                mock.patch.object(adapters, "memory_collisions", return_value={}):
            code, output = self.run_cli("doctor")
        self.assertEqual(code, 1, output)
        self.assertIn("run failed", output)
        self.assertIn("不在 git 仓库", output)

    def test_invalid_interval_is_rejected_before_enabling_profile(self):
        # Reject an invalid plan before enabling wakes in the seat profile.
        before = self.led.seat(self.seat)
        code, output = self.enable("--yes", "--interval", "59")
        self.assertEqual(code, 2, output)
        self.install.assert_not_called()
        self.assertEqual(self.led.seat(self.seat), before)

    def test_off_can_recover_from_invalid_saved_interval(self):
        # Turning wakes off must not depend on a valid installation interval.
        self.led.update_seat(self.seat, autowake=True, wake="exec", autowake_interval=59)
        code, output = self.run_cli("autowake", "off", "--as", self.seat)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.led.seat(self.seat)["wake"], "queue")
        self.assertFalse(self.led.seat(self.seat).get("autowake"))


class SeatAndArguments(Sandbox):
    def test_reregister_preserves_autowake_and_update_none_removes_only_requested_keys(self):
        led = self.make_single()
        led.add_seat("codex", "codex", paths=[str(led.root)], command="fake-codex")
        led.update_seat("codex@mac", autowake=True, autowake_interval=180,
                        wake="exec", wake_budget_per_day=7, focus="review")
        led.add_seat("codex", "codex", title="new title")
        prof = led.seat("codex@mac")
        self.assertEqual((prof["autowake"], prof["autowake_interval"], prof["wake_budget_per_day"]),
                         (True, 180, 7))
        self.assertEqual(prof["command"], "fake-codex")
        self.assertEqual(prof["focus"], "review")
        led.update_seat("codex@mac", autowake=None)
        self.assertNotIn("autowake", led.seat("codex@mac"))
        self.assertEqual(led.seat("codex@mac")["autowake_interval"], 180)
        with self.assertRaises(util.YizhanError):
            led.update_seat("codex@elsewhere", autowake=True)

    def test_writable_paths_with_spaces_remain_single_arguments(self):
        work = str(self.folder("工作 文件夹"))
        writable = (self.folder("账本 路径"), self.folder("状态 路径"))
        last = self.tmp / "last message.md"
        prof = {"agent": "codex", "wake": "exec", "command": "fake-codex", "workdir": work}
        argv, cwd = adapters.wake_argv(prof, "read and reply", last, writable=writable)
        self.assertEqual(cwd, work)
        self.assertEqual(argv[argv.index("--cd") + 1], work)
        self.assertEqual([argv[i + 1] for i, arg in enumerate(argv) if arg == "--add-dir"],
                         [str(path) for path in writable])
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertEqual(argv[-3:], ["-o", str(last), "read and reply"])
        prof["wake"] = "queue"
        self.assertEqual(adapters.wake_argv(prof, "read", last, writable=writable), (None, None))
