"""Relay delivery regressions, with temporary ledgers and intercepted agent runs."""
import contextlib
import io
import sys
from pathlib import Path
from unittest import mock

from helpers import Sandbox

from yizhan import cli, gitio, mail, relay, state, util, wake


class RelaySandbox(Sandbox):
    sender = "reviewer@mac"
    worker = "worker@mac"

    def setUp(self):
        super().setUp()
        self.led = self.make_single()
        self.use_ledger(self.led)
        self.work = self.folder("work")
        gitio.init(self.work)
        self.target = self.work / "value.txt"
        self.target.write_text("before\n", encoding="utf-8")
        self.led.add_seat("reviewer", "codex", command=sys.executable)
        self.led.add_seat("worker", "cli", command=relay.join_command([sys.executable]),
                          workdir=str(self.work), wake="exec")
        self.led.update_seat(self.worker, autowake=True)
        self.sent = 0
        patcher = mock.patch.object(wake, "spawn_detached")
        self.spawn = patcher.start()
        self.addCleanup(patcher.stop)

    def letter(self, subject="task"):
        # Inbox ordering must not depend on the random suffix of same-second ids.
        now = util.utcnow() + util.dt.timedelta(seconds=self.sent)
        self.sent += 1
        with mock.patch.object(util, "utcnow", return_value=now):
            return mail.send(self.led, self.sender, [self.worker], "request", subject,
                             f"do {subject}", sealed=True)

    def run_ok(self, argv, workdir, stdin_text, out_path, log_path, timeout):
        out_path.write_text("done", encoding="utf-8")
        return 0

    def check(self, runner=None):
        return wake.wake_once(self.led, self.worker, pull=False, runner=runner or self.run_ok)

    def worker_state(self):
        return state.load(self.led, f"seat-{self.worker}")

    def replies(self):
        return mail.inbox(self.led, self.sender)


class RelayDelivery(RelaySandbox):
    def delivery_failure(self, operation):
        request = self.letter()
        runs = []

        def run(*args):
            runs.append(args)
            self.target.write_text(f"run {len(runs)}\n", encoding="utf-8")
            return self.run_ok(*args)

        with mock.patch.object(mail, operation, side_effect=OSError("delivery unavailable")):
            first = self.check(run)
        self.assertEqual(first["returncode"], -1)
        self.assertNotIn(request["id"], self.worker_state()["woken"])
        record_path = self.led.state_dir / "wake" / f"{self.worker}-{request['id']}.result.json"
        record = util.read_json(record_path)
        patch = Path(record["artifacts"][0])
        original_patch = patch.read_bytes()
        first_replies = self.replies()
        self.assertEqual(len(first_replies), 1 if operation == "ack" else 0)

        # A later retry must deliver the saved result even if the workspace changed.
        self.target.write_text("unrelated later change\n", encoding="utf-8")
        second = self.check(run)
        self.assertEqual(second["returncode"], 0)
        self.assertEqual(len(runs), 1)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "unrelated later change\n")
        self.assertEqual(patch.read_bytes(), original_patch)
        self.assertEqual(len(self.replies()), 1)
        if first_replies:
            self.assertEqual(second["replies"], [first_replies[0]["meta"]["id"]])
        self.assertEqual(mail.latest_receipt(self.led, self.worker, request["id"])["status"], "done")
        self.assertFalse(record_path.exists())
        self.assertEqual(self.check(run)["action"], "idle")
        self.assertEqual(len(runs), 1)

    def test_failed_send_retries_delivery_without_running_again(self):
        self.delivery_failure("send")

    def test_failed_receipt_keeps_one_reply_and_immutable_patch(self):
        self.delivery_failure("ack")

    def test_failed_run_remains_retryable_without_a_success_receipt(self):
        request = self.letter()
        self.assertEqual(self.check(mock.Mock(return_value=17))["returncode"], 17)
        self.assertNotIn(request["id"], self.worker_state()["woken"])
        self.assertIsNone(mail.latest_receipt(self.led, self.worker, request["id"]))
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.check()["returncode"], 0)
        self.assertEqual(len(self.replies()), 1)

    def test_unwritable_log_does_not_lose_the_request(self):
        request = self.letter()
        log = self.led.state_dir / "wake" / f"{self.worker}.log"
        log.mkdir(parents=True)
        with mock.patch.object(util, "run_tree") as process:
            first = self.check(relay.run_cli)
        process.assert_not_called()
        self.assertEqual(first["returncode"], -1)
        self.assertNotIn(request["id"], self.worker_state()["woken"])
        self.assertTrue(self.worker_state()["last_wake_error"])
        log.rmdir()
        self.assertEqual(self.check()["returncode"], 0)
        self.assertNotIn("last_wake_error", self.worker_state())

    def test_letter_arriving_during_run_is_handled_after_busy_wake(self):
        self.letter("first")
        prompts = []

        def run(*args):
            prompts.append(args[2])
            if len(prompts) == 1:
                self.letter("second")
                self.assertEqual(self.check(run)["action"], "busy")
            return self.run_ok(*args)

        self.assertEqual(self.check(run)["action"], "relayed")
        self.assertEqual(len(prompts), 2)
        self.assertIn("do second", prompts[1])
        self.assertEqual(len(self.replies()), 2)
        self.assertEqual(mail.inbox(self.led, self.worker), [])

    def test_each_reply_wakes_reader_before_next_task(self):
        self.led.update_seat(self.sender, autowake=True)
        self.letter("first")
        self.letter("second")
        observed = []

        def run(*args):
            observed.append(self.spawn.call_count)
            return self.run_ok(*args)

        result = self.check(run)
        self.assertEqual(observed, [0, 1])
        self.assertEqual(result["kicked"], [self.sender])
        self.assertEqual(self.spawn.call_count, 2)

    def test_long_prompt_pauses_without_budget_and_template_change_retries(self):
        long_one = self.letter("long")
        short_one = self.letter("short")
        attempted = []

        def run(*args):
            attempted.append(args[2])
            if "do long" in args[2]:
                raise relay.PromptTooLong("too long")
            return self.run_ok(*args)

        first = self.check(run)
        self.assertEqual(first["paused"], [long_one["id"]])
        self.assertEqual(len(first["replies"]), 1)
        self.assertEqual(len(attempted), 2)
        self.assertEqual(list(self.worker_state()["wakes"].values()), [1])
        self.assertIsNone(mail.latest_receipt(self.led, self.worker, long_one["id"]))
        self.assertEqual(mail.latest_receipt(self.led, self.worker, short_one["id"])["status"], "done")
        self.assertEqual(self.check(run)["action"], "idle")
        self.assertEqual(len(attempted), 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(cli.service, "installed", return_value=False):
            self.assertEqual(cli.main(["autowake", "status"]), 0)
        self.assertIn("1 封信的提示词放不进命令行", output.getvalue())

        self.led.update_seat(self.worker, command=relay.join_command([sys.executable, "{prompt_file}"]))
        second = self.check()
        self.assertEqual(second["ids"], [long_one["id"]])
        self.assertEqual(second["returncode"], 0)
        self.assertEqual(self.worker_state()["paused"], {})
        self.assertEqual(list(self.worker_state()["wakes"].values()), [2])


class RelaySnapshots(Sandbox):
    def test_patch_applies_with_original_crlf_and_non_utf8_bytes(self):
        for name, original, changed in (("crlf", b"before\r\n", b"after\r\n"),
                                        ("latin1", b"caf\xe9\n", b"caf\xe9 changed\n")):
            with self.subTest(name=name):
                work = self.folder(name)
                gitio.init(work)
                (work / ".gitattributes").write_text("*.txt -text\n", encoding="utf-8")
                target = work / "value.txt"
                target.write_bytes(original)
                before = relay.snapshot(work, self.tmp / f"{name}.index")
                target.write_bytes(changed)
                after = relay.snapshot(work, self.tmp / f"{name}.index")
                _, patch = relay.changes(before, after, self.tmp / f"{name}.patch")
                self.assertIsNotNone(patch)
                target.write_bytes(original)
                gitio.git(work, "apply", "--check", str(patch))
                gitio.git(work, "apply", str(patch))
                self.assertEqual(target.read_bytes(), changed)

    def test_assume_unchanged_edits_are_seen_without_touching_real_index(self):
        work = self.folder("assumed")
        gitio.init(work)
        target = work / "value.txt"
        target.write_text("before\n", encoding="utf-8")
        gitio.git(work, "add", "value.txt")
        gitio.git(work, "update-index", "--assume-unchanged", "value.txt")
        original_index = (work / ".git" / "index").read_bytes()
        before = relay.snapshot(work, self.tmp / "snapshot.index")
        target.write_text("after\n", encoding="utf-8")
        after = relay.snapshot(work, self.tmp / "snapshot.index")
        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertNotEqual(before, after)
        self.assertEqual((work / ".git" / "index").read_bytes(), original_index)
        self.assertTrue(gitio.git(work, "ls-files", "-v").stdout.startswith("h "))
