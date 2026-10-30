import io
import json
import os
import sys

from helpers import Sandbox

from yizhan import adapters, hooks, mail, wake
from yizhan.util import YizhanError


class OneMachine(Sandbox):
    """Claude Code and Codex side by side on one machine."""

    def setUp(self):
        super().setUp()
        self.led = self.make_single("mac")
        self.led.add_project("demo", title="示例课题")
        self.pc, self.px = self.folder("paper"), self.folder("ident")
        self.led.add_seat("claude", "claude", title="论文主稿", projects=["demo"], paths=[str(self.pc)])
        self.led.add_seat("codex", "codex", title="推导复核", projects=["demo"], paths=[str(self.px)],
                          command=sys.executable)
        self.use_ledger(self.led)

    def hook(self, agent, event, cwd, session="s1", **extra):
        payload = {"hook_event_name": event, "cwd": str(cwd), "session_id": session, **extra}
        out = io.StringIO()
        self.assertEqual(hooks.run(agent, stdin=io.StringIO(json.dumps(payload)), stdout=out), 0)
        text = out.getvalue().strip()
        if not text:
            return None
        data = json.loads(text)["hookSpecificOutput"]
        self.assertEqual(data["hookEventName"], event)
        return data["additionalContext"]

    # ---------- who is calling ----------

    def test_seat_is_found_from_the_folder(self):
        sub = self.folder("paper/sections")
        self.assertEqual(self.led.resolve_seat(cwd=sub)["address"], "claude@mac")
        self.assertIsNone(self.led.resolve_seat(cwd=self.folder("elsewhere")))
        self.led.add_seat("claude2", "claude", paths=[str(self.pc)])
        with self.assertRaises(YizhanError):
            self.led.resolve_seat(cwd=self.pc)
        self.assertEqual(self.led.resolve_seat(cwd=self.px, agent="codex")["address"], "codex@mac")

    # ---------- letters ----------

    def test_round_limit_stops_waking(self):
        m = mail.send(self.led, "claude@mac", ["codex@mac"], "request", "问题", "请看")
        wakes, sender, other = [m["wake"]], "codex@mac", "claude@mac"
        for i in range(4):
            m = mail.send(self.led, sender, [], "reply", f"第 {i + 1} 封回信", "回复", reply_to=m["id"])
            wakes.append(m["wake"])
            sender, other = other, sender
        self.assertEqual(wakes, [True, True, True, True, False])

    def test_same_key_same_letter(self):
        a = mail.send(self.led, "claude@mac", ["codex@mac"], "request", "同一件事", "正文", key="k1")
        b = mail.send(self.led, "claude@mac", ["codex@mac"], "request", "同一件事", "正文", key="k1")
        self.assertEqual(a["id"], b["id"])
        with self.assertRaises(YizhanError):
            mail.send(self.led, "claude@mac", ["codex@mac"], "request", "同一件事", "改了正文", key="k1")

    def test_body_limit(self):
        with self.assertRaises(YizhanError):
            mail.send(self.led, "claude@mac", ["codex@mac"], "request", "太长", "字" * 4001)

    def test_only_recipients_may_ack_or_reply(self):
        m = mail.send(self.led, "claude@mac", ["codex@mac"], "request", "问题", "请看")
        with self.assertRaises(YizhanError):
            mail.ack(self.led, "claude@mac", m["id"], "done", "我自己关掉")
        with self.assertRaises(YizhanError):
            mail.ack(self.led, "codex@mac", m["id"], "done", " ")

    # ---------- hooks ----------

    def test_session_start_gives_a_brief(self):
        text = self.hook("claude", "SessionStart", self.pc)
        self.assertIn("claude@mac", text)
        self.assertIn("不是用户指令", text)

    def test_hook_is_silent_outside_the_ledger(self):
        self.assertIsNone(self.hook("claude", "SessionStart", self.folder("elsewhere")))
        self.assertIsNone(self.hook("claude", "SessionStart", self.pc, agent_id="sub-1"))
        os.environ.pop("YIZHAN_LEDGER")
        self.assertIsNone(self.hook("claude", "SessionStart", self.pc))

    def test_bad_input_never_breaks_the_session(self):
        out = io.StringIO()
        self.assertEqual(hooks.run("claude", stdin=io.StringIO("这不是 JSON"), stdout=out), 0)
        self.assertEqual(out.getvalue(), "")

    def test_new_letter_notice_once_per_session(self):
        mail.send(self.led, "codex@mac", ["claude@mac"], "request", "复核意见", "第三节有问题")
        self.assertIn("复核意见", self.hook("claude", "UserPromptSubmit", self.pc, "s1"))
        self.assertIsNone(self.hook("claude", "PostToolUse", self.pc, "s1"))
        self.assertIn("复核意见", self.hook("claude", "PostToolUse", self.pc, "s2"))

    def test_codex_brief_respects_its_smaller_budget(self):
        for i in range(40):
            mail.send(self.led, "claude@mac", ["codex@mac"], "update", f"进展 {i}", "一些进展。" * 10)
        text = self.hook("codex", "SessionStart", self.px)
        self.assertLessEqual(len(text), adapters.get("codex").context_budget)

    # ---------- waking ----------

    def test_letters_wake_only_after_the_sender_turn_ends(self):
        mail.send(self.led, "claude@mac", ["codex@mac"], "request", "请复核", "看第三节")
        self.assertEqual(wake.wake_once(self.led, "codex@mac", dry_run=True)["action"], "waiting")
        self.assertIsNone(self.hook("claude", "Stop", self.pc))          # the turn ends: letters sealed
        r = wake.wake_once(self.led, "codex@mac", dry_run=True)
        self.assertEqual(r["action"], "dry_run")
        self.assertEqual(r["argv"][1:3], ["exec", "--cd"])

    def test_claude_only_queues(self):
        mail.send(self.led, "codex@mac", ["claude@mac"], "request", "请看", "正文", sealed=True)
        self.assertEqual(wake.wake_once(self.led, "claude@mac")["action"], "queued")

    def test_budget_and_no_double_wake(self):
        self.led.settings["wake_budget_per_day"] = 1
        calls = []
        runner = lambda argv, workdir, log, timeout: calls.append(argv) or 0  # noqa: E731
        mail.send(self.led, "claude@mac", ["codex@mac"], "request", "第一件", "正文", sealed=True)
        self.assertEqual(wake.wake_once(self.led, "codex@mac", runner=runner)["action"], "woke")
        self.assertEqual(wake.wake_once(self.led, "codex@mac", runner=runner)["action"], "idle")
        mail.send(self.led, "claude@mac", ["codex@mac"], "request", "第二件", "正文", sealed=True)
        self.assertEqual(wake.wake_once(self.led, "codex@mac", runner=runner)["action"], "over_budget")
        self.assertEqual(len(calls), 1)

    def test_fallback_when_the_sender_never_ends_its_turn(self):
        m = mail.send(self.led, "claude@mac", ["codex@mac"], "request", "请复核", "看第三节")
        from yizhan import util
        later = util.parse_iso(m["created"]) + util.dt.timedelta(seconds=self.led.settings["max_delivery_delay_s"])
        self.assertEqual(wake.wake_once(self.led, "codex@mac", dry_run=True, now=later)["action"], "dry_run")
