"""Background-job plans and lifecycle, without installing any real OS jobs."""
import os
import plistlib
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import Sandbox

from yizhan import service, util


class BackgroundService(Sandbox):
    def setUp(self):
        super().setUp()
        self.led = SimpleNamespace(state_dir=self.tmp / "state" / "ledger")
        self.seat = "codex@mac"
        self.argv = ["/opt/Python Tools/python3", "/projects/中文 工具/bin/yizhan",
                     "wake", "--as", self.seat]
        self._home = mock.patch.object(Path, "home", return_value=self.tmp / "user")
        self._home.start()
        self.addCleanup(self._home.stop)
        # Let Sandbox restore the environment after the test.
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")

    def plan(self, platform, interval=300, argv=None):
        with mock.patch.object(service, "job_argv", return_value=self.argv if argv is None else argv):
            return service.plan(self.led, self.seat, interval, platform=platform)

    def test_launchd_plist_preserves_argument_boundaries_and_has_log_directory(self):
        p = self.plan("darwin", interval=61)
        self.assertEqual(p["kind"], "launchd")
        path, text = next(iter(p["files"].items()))
        self.assertEqual(Path(path).parent, self.tmp / "user" / "Library" / "LaunchAgents")
        body = plistlib.loads(text.encode("utf-8"))
        self.assertEqual(body["ProgramArguments"], self.argv)
        self.assertEqual(body["StartInterval"], 61)
        self.assertTrue(body["RunAtLoad"])
        self.assertEqual(body["StandardOutPath"], body["StandardErrorPath"])
        self.assertIn(str(Path(body["StandardOutPath"]).parent), p["dirs"])
        self.assertEqual(p["on"][0][0:2], ["launchctl", "bootstrap"])
        self.assertEqual(p["on"][0][-1], path)
        self.assertFalse(Path(path).exists(), "planning must not create a job")

    def test_systemd_plan_uses_xdg_config_and_escapes_literal_path_characters(self):
        argv = ["/opt/Python Tools/python3", '/projects/100%/a"b\\c/yizhan',
                "wake", "--as", self.seat]
        p = self.plan("linux", interval=61, argv=argv)
        self.assertEqual(p["kind"], "systemd")
        service_path = self.tmp / "config" / "systemd" / "user" / "yizhan-wake-codex-mac.service"
        body = p["files"][str(service_path)]
        self.assertIn('ExecStart="/opt/Python Tools/python3" ', body)
        self.assertIn(r'"/projects/100%%/a\"b\\c/yizhan"', body)
        self.assertIn(' "wake" "--as" "codex@mac"\n', body)
        timer = p["files"][str(service_path.with_suffix(".timer"))]
        self.assertIn("OnBootSec=61\n", timer)
        self.assertIn("OnUnitActiveSec=61\n", timer)
        self.assertIn("Unit=yizhan-wake-codex-mac.service\n", timer)
        self.assertFalse(service_path.exists(), "planning must not create a job")

    def test_systemd_preserves_dollar_in_source_launcher_path(self):
        # ${...} expands even inside a path. Escape dollars
        # as $$, or disable expansion with ExecStart=: (systemd.service(5)).
        # https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html
        p = self.plan("linux", argv=["/usr/bin/python3", "/projects/${USER}/bin/yizhan",
                                      "wake", "--as", self.seat])
        body = next(text for path, text in p["files"].items() if path.endswith(".service"))
        line = next(line for line in body.splitlines() if line.startswith("ExecStart="))
        self.assertTrue(line.startswith("ExecStart=:") or "/projects/$${USER}/bin/yizhan" in line,
                        "a literal source path must not be expanded as an environment variable")

    def test_windows_quotes_paths_and_rounds_partial_minutes_up(self):
        argv = [r"C:\Program Files\Python\python.exe", r"C:\我的 工具\yizhan",
                "wake", "--as", self.seat]
        for seconds, minutes in ((60, 1), (61, 2), (119, 2), (120, 2), (121, 3), (86340, 1439)):
            with self.subTest(seconds=seconds):
                p = self.plan("windows", interval=seconds, argv=argv)
                command = p["on"][0]
                self.assertEqual(command[command.index("/SC") + 1], "MINUTE")
                self.assertEqual(command[command.index("/MO") + 1], str(minutes))
                self.assertEqual(command[command.index("/TR") + 1],
                                 '"C:\\Program Files\\Python\\python.exe" '
                                 '"C:\\我的 工具\\yizhan" wake --as codex@mac')
                self.assertEqual(p["files"], {})

    def test_windows_rejects_interval_past_schtasks_minute_limit(self):
        # /SC MINUTE accepts /MO 1 through 1439 only.
        # https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create
        with self.assertRaises(util.YizhanError):
            self.plan("windows", interval=86341)

    def test_windows_long_command_fails_during_planning(self):
        with self.assertRaisesRegex(util.YizhanError, "Windows.*计划任务"):
            self.plan("windows", argv=["C:\\" + "a" * 300 + "\\yizhan.exe"])

    def test_all_platforms_reject_subminute_intervals_without_writing_files(self):
        for platform in ("darwin", "linux", "windows"):
            for interval in (-1, 0, 59):
                with self.subTest(platform=platform, interval=interval):
                    with self.assertRaisesRegex(util.YizhanError, "60"):
                        self.plan(platform, interval=interval)
        self.assertFalse((self.tmp / "user").exists())
        self.assertFalse((self.tmp / "config").exists())

    def test_install_creates_required_files_before_loading_each_platform(self):
        for platform in ("darwin", "linux", "windows"):
            with self.subTest(platform=platform):
                p = self.plan(platform)
                calls = []

                def run(argv):
                    calls.append(argv)
                    for path, expected in p["files"].items():
                        self.assertEqual(Path(path).read_text(encoding="utf-8"), expected)
                    for directory in p["dirs"]:
                        self.assertTrue(Path(directory).is_dir())
                    return (1, "not previously loaded") if argv in p["before"] else (0, "")

                service.install(p, run=run)
                self.assertEqual(calls, p["before"] + p["on"])

    def test_install_surfaces_loader_failure_and_stops(self):
        p = self.plan("linux")
        calls = []

        def run(argv):
            calls.append(argv)
            return 1, "user service manager unavailable"

        with self.assertRaisesRegex(util.YizhanError, "user service manager unavailable"):
            service.install(p, run=run)
        self.assertEqual(calls, [p["on"][0]])

    def test_uninstall_never_installed_is_quiet_on_every_platform(self):
        for platform in ("darwin", "linux", "windows"):
            with self.subTest(platform=platform):
                p = self.plan(platform)
                run = mock.Mock(return_value=(1, "no such job"))
                service.uninstall(p, run=run)
                self.assertEqual(run.call_args_list,
                                 [mock.call(argv) for argv in p["off"] + p["after_off"]])

    def test_uninstall_removes_job_files_before_systemd_reload(self):
        p = self.plan("linux")
        service.install(p, run=lambda argv: (0, ""))
        calls = []

        def run(argv):
            calls.append(argv)
            if argv in p["off"]:
                self.assertTrue(all(Path(path).exists() for path in p["files"]))
            if argv in p["after_off"]:
                self.assertTrue(all(not Path(path).exists() for path in p["files"]))
            return 0, ""

        service.uninstall(p, run=run)
        self.assertEqual(calls, p["off"] + p["after_off"])

    def test_installed_queries_job_manager_instead_of_presence_of_files(self):
        for platform in ("darwin", "linux", "windows"):
            p = self.plan(platform)
            for code, expected in ((0, True), (1, False), (3, False)):
                with self.subTest(platform=platform, returncode=code):
                    run = mock.Mock(return_value=(code, "status"))
                    self.assertIs(service.installed(p, run=run), expected)
                    run.assert_called_once_with(p["check"])

    def test_missing_job_manager_and_timeout_return_diagnostics(self):
        for exc in (FileNotFoundError("missing manager"),
                    subprocess.TimeoutExpired(["manager"], 60)):
            with self.subTest(error=type(exc).__name__):
                with mock.patch.object(service.subprocess, "run", side_effect=exc):
                    code, diagnostic = service._run(["manager"])
                self.assertNotEqual(code, 0)
                self.assertTrue(diagnostic)
