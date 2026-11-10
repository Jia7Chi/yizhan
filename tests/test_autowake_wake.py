"""Autowake event delivery and retries, with every agent launch intercepted."""
import io
import json
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import Sandbox

from yizhan import brief, hooks, mail, state, util, wake


class WakeSandbox(Sandbox):
    def setUp(self):
        super().setUp()
        self.led = self.make_single()
        self.sender = "claude@mac"
        self.reader = "codex@mac"
        self.workdir = self.folder("work")
        self.led.add_seat("claude", "claude", paths=[str(self.workdir)])
        self.led.add_seat("codex", "codex", command=sys.executable,
                          workdir=str(self.workdir), wake="exec")
        self.led.update_seat(self.reader, autowake=True, wake_budget_per_day=3)
        self.use_ledger(self.led)
        self.now = util.parse_iso("2026-10-30T06:00:00Z")

    def letter(self, *, to=None, when=None, **kwargs):
        with mock.patch.object(util, "utcnow", return_value=when or self.now):
            return mail.send(self.led, self.sender, to or [self.reader],
                             "request", "请复核", "测试材料", **kwargs)

    def stop(self, when):
        payload = {"hook_event_name": "Stop", "cwd": str(self.workdir),
                   "session_id": "sender-session"}
        out = io.StringIO()
        with mock.patch.object(util, "utcnow", return_value=when), \
                mock.patch.object(hooks.sync, "needs_push", return_value=False):
            self.assertEqual(hooks.run("claude", stdin=io.StringIO(json.dumps(payload)),
                                      stdout=out), 0)
        self.assertEqual(out.getvalue(), "")


class FreshTargets(WakeSandbox):
    def test_previous_second_excluded_equal_second_included(self):
        # Timestamps have only second precision. Equality must remain eligible so a
        # new letter written immediately after the previous Stop is not lost.
        self.letter(when=self.now - util.dt.timedelta(seconds=1))
        self.assertEqual(wake.fresh_targets(self.led, self.sender, self.now), [])
        self.letter(when=self.now)
        self.assertEqual(wake.fresh_targets(self.led, self.sender, self.now), [self.reader])

    def test_disabled_remote_unknown_and_non_wake_targets_are_ignored(self):
        self.led.add_seat("off", "codex", wake="queue")
        self.letter(to=["off@mac"])
        self.letter(wake=False)
        # Other-machine profiles are valid ledger data but must never be launched
        # by this machine's Stop hook.
        remote = dict(self.led.seat(self.reader), address="codex@linux", machine="linux")
        util.write_json(self.led.seat_dir("codex@linux") / "profile.json", remote)
        self.letter(to=["codex@linux"])
        unknown = self.letter()
        path = mail.find(self.led, unknown["id"])
        unknown["to"] = ["missing@mac"]
        util.write_text_atomic(path, util.dump_frontmatter(unknown, "未知收信方"))
        self.assertEqual(wake.fresh_targets(self.led, self.sender), [])

    def test_terminal_receipts_close_only_their_own_recipient(self):
        self.led.add_seat("review", "codex")
        self.led.update_seat("review@mac", autowake=True)
        first = self.letter(to=[self.reader, "review@mac"])
        mail.ack(self.led, self.reader, first["id"], "done", "完成")
        mail.ack(self.led, "review@mac", first["id"], "working", "正在处理")
        self.assertEqual(wake.fresh_targets(self.led, self.sender), ["review@mac"])
        mail.ack(self.led, "review@mac", first["id"], "deferred", "等用户")
        self.assertEqual(wake.fresh_targets(self.led, self.sender), [])

    def test_kick_batches_letters_once_per_target_and_uses_absolute_cli(self):
        self.led.add_seat("another", "codex")
        self.led.update_seat("another@mac", autowake=True)
        self.letter(to=[self.reader, "another@mac"])
        self.letter()
        spawn = mock.Mock()
        argv = ["/tools/python", "/checkout with spaces/bin/yizhan"]
        with mock.patch.object(brief, "cli_argv", return_value=argv):
            self.assertEqual(wake.kick(self.led, self.sender, spawn=spawn),
                             ["another@mac", self.reader])
        self.assertEqual(spawn.call_args_list, [
            mock.call(argv + ["wake", "--as", seat],
                      self.led.state_dir / "wake" / f"{seat}.log")
            for seat in ("another@mac", self.reader)
        ])

    def test_stop_seals_before_spawning_and_uses_previous_turn_end(self):
        old = self.now - util.dt.timedelta(seconds=10)
        with mock.patch.object(util, "utcnow", return_value=old):
            mail.touch(self.led, self.sender, seal=True)
        letter = self.letter()
        observed = []

        def spawn(argv, log_path):
            observed.append((argv, mail.is_sealed(self.led, letter)))

        with mock.patch.object(wake, "spawn_detached", side_effect=spawn):
            self.stop(self.now + util.dt.timedelta(seconds=1))
            self.stop(self.now + util.dt.timedelta(seconds=2))
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0][1])
        self.assertEqual(observed[0][0][-3:], ["wake", "--as", self.reader])

    def test_stop_still_kicks_after_sync_failure(self):
        self.letter()
        payload = {"cwd": str(self.workdir), "hook_event_name": "Stop"}
        out = io.StringIO()
        with mock.patch.object(hooks.sync, "needs_push", return_value=True), \
                mock.patch.object(hooks.sync, "sync", side_effect=util.YizhanError("offline")), \
                mock.patch.object(wake, "spawn_detached") as spawn:
            self.assertEqual(hooks.run("claude", stdin=io.StringIO(json.dumps(payload)),
                                      stdout=out), 0)
        spawn.assert_called_once()
        self.assertEqual(out.getvalue(), "")

    def test_spawn_failure_does_not_break_stop(self):
        self.letter()
        with mock.patch.object(wake, "spawn_detached", side_effect=OSError("no process")):
            self.stop(self.now)
        self.assertIn("Stop kick", (util.state_home() / "hook-errors.log").read_text())


class WakeRetries(WakeSandbox):
    def check(self, runner, **kwargs):
        return wake.wake_once(self.led, self.reader, pull=False,
                              now=kwargs.pop("now", self.now), runner=runner, **kwargs)

    def test_dry_run_does_not_change_state_or_consume_budget(self):
        self.letter(sealed=True)
        runner = mock.Mock()
        self.assertEqual(self.check(runner, dry_run=True)["action"], "dry_run")
        self.assertEqual(state.load(self.led, f"seat-{self.reader}"), {})
        runner.assert_not_called()

    def test_failure_retries_and_success_clears_error_without_duplicate_run(self):
        first = self.letter(sealed=True)
        runs = []

        def runner(argv, workdir, log_path, timeout):
            runs.append(argv)
            log_path.write_text("old line\nerror one\nerror two\nerror three\n", encoding="utf-8")
            return 17 if len(runs) == 1 else 0

        self.assertEqual(self.check(runner)["returncode"], 17)
        failed = state.load(self.led, f"seat-{self.reader}")
        self.assertNotIn(first["id"], failed["woken"])
        self.assertEqual(failed["last_wake_rc"], 17)
        self.assertEqual(failed["last_wake_error"], "error one | error two | error three")
        self.assertEqual(self.check(runner)["returncode"], 0)
        successful = state.load(self.led, f"seat-{self.reader}")
        self.assertIn(first["id"], successful["woken"])
        self.assertEqual(successful["last_wake_rc"], 0)
        self.assertNotIn("last_wake_error", successful)
        self.assertEqual(successful["wakes"], {"2026-10-30": 2})
        self.assertEqual(self.check(runner)["action"], "idle")
        self.assertEqual(len(runs), 2)

    def test_failed_attempts_exhaust_budget_and_new_day_can_retry(self):
        self.led.update_seat(self.reader, wake_budget_per_day=2)
        self.letter(sealed=True)
        runner = mock.Mock(return_value=1)
        self.assertEqual(self.check(runner)["action"], "woke")
        self.assertEqual(self.check(runner)["action"], "woke")
        denied = self.check(runner)
        self.assertEqual((denied["action"], denied["used"], denied["budget"]),
                         ("over_budget", 2, 2))
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(self.check(runner, now=self.now + util.dt.timedelta(days=1))["action"],
                         "woke")
        self.assertEqual(runner.call_count, 3)

    def test_partial_failure_does_not_reopen_acknowledged_letters(self):
        done = self.letter(sealed=True)
        pending = self.letter(sealed=True)

        def fail_after_ack(argv, workdir, log_path, timeout):
            mail.ack(self.led, self.reader, done["id"], "done", "这一封已处理")
            return 1

        self.assertEqual(set(self.check(fail_after_ack)["ids"]), {done["id"], pending["id"]})
        retry = self.check(mock.Mock(return_value=0))
        self.assertEqual(retry["ids"], [pending["id"]])
        self.assertEqual(mail.latest_receipt(self.led, self.reader, done["id"])["status"], "done")

    def test_in_flight_run_keeps_lock_until_runner_finishes(self):
        self.letter(sealed=True)
        other_runner = mock.Mock(return_value=0)

        def runner(argv, workdir, log_path, timeout):
            self.assertEqual(self.check(other_runner)["action"], "busy")
            return 0

        self.assertEqual(self.check(runner)["action"], "woke")
        other_runner.assert_not_called()
        self.assertEqual(self.check(other_runner)["action"], "idle")

    def test_runner_timeout_and_launch_error_are_retryable_failures(self):
        self.letter(sealed=True)
        for failure, outcome in (("timeout", None), ("launch_error", OSError("not installed"))):
            with self.subTest(failure=failure), \
                    mock.patch.object(wake.util, "run_tree", side_effect=[outcome]) as run:
                result = self.check(None)
                run.assert_called_once()
                self.assertEqual(result["returncode"], -1)
                self.assertEqual(state.load(self.led, f"seat-{self.reader}")["woken"], [])
        error = state.load(self.led, f"seat-{self.reader}")["last_wake_error"]
        self.assertIn("not installed", error)

    def test_unwritable_log_still_allows_failed_run_to_retry(self):
        first = self.letter(sealed=True)
        log = self.led.state_dir / "wake" / f"{self.reader}.log"
        log.mkdir(parents=True)
        with mock.patch.object(util, "run_tree") as process:
            result = self.check(None)
        process.assert_not_called()
        self.assertEqual(result["returncode"], -1)
        self.assertNotIn(first["id"], state.load(self.led, f"seat-{self.reader}")["woken"])
        log.rmdir()
        retry = mock.Mock(return_value=0)
        self.assertEqual(self.check(retry)["returncode"], 0)
        retry.assert_called_once()

    def test_completed_run_seals_its_letters_and_wakes_their_reader(self):
        self.letter(sealed=True)
        self.led.update_seat(self.sender, autowake=True)
        sent = []

        def runner(*args):
            sent.append(mail.send(self.led, self.reader, [self.sender],
                                  "request", "继续处理", "下一项工作"))
            return 0

        with mock.patch.object(wake, "spawn_detached") as spawn:
            result = self.check(runner)
        self.assertEqual(result["returncode"], 0)
        self.assertTrue(mail.is_sealed(self.led, sent[0]))
        self.assertEqual(result["kicked"], [self.sender])
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0][-3:], ["wake", "--as", self.sender])


class DetachedProcessAndLocks(Sandbox):
    def test_spawn_uses_platform_detachment_and_redirects_all_streams(self):
        for platform in ("posix", "nt"):
            with self.subTest(platform=platform):
                path = self.tmp / platform / "wake.log"
                argv = ["fake-python", "fake-yizhan", "wake"]
                captured = {}

                def popen(command, **kwargs):
                    captured.update(kwargs)
                    self.assertEqual(command, argv)
                    self.assertFalse(kwargs["stdout"].closed)
                    self.assertIn("开始叫醒", path.read_text(encoding="utf-8"))
                    return sentinel

                sentinel = object()
                # Replace only wake's os binding; changing global os.name would
                # change pathlib behavior while exercising the Windows branch.
                with mock.patch.object(wake, "os", SimpleNamespace(name=platform)), \
                        mock.patch.object(wake.subprocess, "Popen", side_effect=popen):
                    self.assertIs(wake.spawn_detached(argv, path), sentinel)
                self.assertEqual(captured["stdin"], subprocess.DEVNULL)
                self.assertEqual(captured["stderr"], subprocess.STDOUT)
                self.assertTrue(captured["close_fds"])
                self.assertTrue(captured["stdout"].closed)
                if platform == "nt":
                    self.assertEqual(captured["creationflags"], 0x00000008 | 0x00000200)
                    self.assertNotIn("start_new_session", captured)
                else:
                    self.assertTrue(captured["start_new_session"])
                    self.assertNotIn("creationflags", captured)

    def test_try_lock_is_nonblocking_and_releases_after_exception(self):
        path = self.tmp / "locks" / "seat.lock"
        with self.assertRaisesRegex(RuntimeError, "test release"):
            with util.try_file_lock(path) as acquired:
                self.assertTrue(acquired)
                with util.try_file_lock(path) as second:
                    self.assertFalse(second)
                raise RuntimeError("test release")
        with util.try_file_lock(path) as acquired:
            self.assertTrue(acquired)


if __name__ == "__main__":
    unittest.main()
