"""Board rendering and demo isolation, including untrusted ledger content."""
import contextlib
import datetime as dt
import html
import io
import json
import os
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from helpers import Sandbox

from yizhan import board, cli, demo, mail, records, util


class PageParser(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.scripts = []
        self._script = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag == "script":
            self._script = {"attrs": dict(attrs), "text": ""}
            self.scripts.append(self._script)

    def handle_endtag(self, tag):
        if tag == "script":
            self._script = None

    def handle_data(self, data):
        if self._script is not None:
            self._script["text"] += data


def files_under(root):
    """Include git/config/state files so an accidental write cannot hide there."""
    return {str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


class Board(Sandbox):
    def setUp(self):
        super().setUp()
        self.led = self.make_single("mac")
        self.led.add_project("paper")
        for name in ("claude", "codex"):
            self.led.add_seat(name, name, projects=["paper"])

    def test_ledger_text_is_escaped_in_every_board_section(self):
        fields = {}

        def attack(name):
            fields[name] = f'{name}<img src=x onerror="alert(1)">&\'quoted\''
            return fields[name]

        project = self.led.project("paper")
        project.update(title=attack("project-title"), summary=attack("project-summary"))
        util.write_json(self.led.project_dir("paper") / "project.json", project)
        self.led.add_seat("claude", "claude", title=attack("seat-title"),
                          duty=attack("seat-duty"), owns=[attack("owned-path")])
        records.set_focus(self.led, "claude@mac", attack("seat-focus"))
        profile_path = self.led.seat_dir("claude@mac") / "profile.json"
        profile = util.read_json(profile_path)
        profile["agent"] = attack("agent-class")
        util.write_json(profile_path, profile)
        machine_path = self.led.root / "machines" / "mac" / "machine.json"
        machine = util.read_json(machine_path)
        machine.update(hostname=attack("machine-host"), platform=attack("machine-platform"))
        util.write_json(machine_path, machine)
        letter = mail.send(self.led, "claude@mac", ["codex@mac"], "request",
                           attack("letter-subject"), attack("letter-body"), project="paper")
        mail.ack(self.led, "codex@mac", letter["id"], "done", attack("receipt-note"))
        records.decide(self.led, "codex@mac", "paper", attack("decision-title"),
                       attack("decision-body"), refs=[attack("decision-reference")])
        records.log(self.led, "claude@mac", attack("journal-title"))

        source = board.render(self.led)
        parsed = PageParser(source)
        for name, value in fields.items():
            with self.subTest(field=name):
                self.assertIn(html.escape(value, quote=True), source)
                self.assertNotIn(value, source)
        self.assertNotIn("img", [tag for tag, _ in parsed.elements])
        self.assertEqual(len(parsed.scripts), 2)
        self.assertFalse(any(attr.startswith("on") for _, attrs in parsed.elements for attr in attrs))

    def test_embedded_links_json_cannot_end_its_script_element(self):
        data = board.collect(self.led)
        for payload in ('</script><script>alert("x")</script>',
                        '</ScRiPt><img src=x onerror="alert(1)">',
                        'quote" slash\\ ampersand& 中文'):
            with self.subTest(payload=payload):
                data["links"] = [{"a": payload, "b": "codex@mac", "n": 1, "ab": 1, "ba": 0}]
                parsed = PageParser(board.render(self.led, data))
                self.assertEqual(len(parsed.scripts), 2)
                self.assertNotIn("img", [tag for tag, _ in parsed.elements])
                self.assertEqual(parsed.scripts[0]["attrs"]["id"], "links-data")
                self.assertEqual(json.loads(parsed.scripts[0]["text"]), data["links"])

    def test_decision_supersession_shows_replacement_title_and_preserves_both(self):
        old = records.decide(self.led, "claude@mac", "paper", "旧结论", "旧依据")
        new = records.decide(self.led, "codex@mac", "paper", "新结论 <2c>", "新依据",
                             supersedes=[old["id"]])
        data = board.collect(self.led)
        decisions = {d["id"]: (d, body) for d, body in data["decisions"]}
        self.assertEqual(decisions[old["id"]][0]["superseded_by"], new["id"])
        self.assertIsNone(decisions[new["id"]][0]["superseded_by"])
        source = board.render(self.led, data)
        self.assertIn('class="decision superseded"', source)
        self.assertIn("已被取代", source)
        self.assertIn(f'<span title="{html.escape(new["id"], quote=True)}">新结论 &lt;2c&gt;</span>', source)
        self.assertIn("旧依据", source)
        self.assertIn("新依据", source)

    def test_collect_counts_directions_receipts_and_groups_replies(self):
        now = util.utcnow()
        with mock.patch.object(util, "utcnow", return_value=now):
            request = mail.send(self.led, "claude@mac", ["codex@mac"], "request", "问题", "正文")
        with mock.patch.object(util, "utcnow", return_value=now + dt.timedelta(seconds=1)):
            reply = mail.send(self.led, "codex@mac", [], "reply", "答复", "回信",
                              reply_to=request["id"])
        mail.ack(self.led, "codex@mac", request["id"], "done", "已答复")
        data = board.collect(self.led)
        self.assertEqual(dict(data["open_for"]), {"claude@mac": 1})
        self.assertEqual(data["links"], [{"a": "claude@mac", "b": "codex@mac",
                                          "n": 2, "ab": 1, "ba": 1}])
        groups = board.threads(data["letters"])
        self.assertEqual(len(groups), 1)
        self.assertEqual([item["meta"]["id"] for item in groups[0]], [request["id"], reply["id"]])

    def test_same_second_reply_is_displayed_after_its_request(self):
        # IDs contain randomness, so their lexicographic order is not causality.
        now = util.utcnow()
        with mock.patch.object(util, "utcnow", return_value=now), \
                mock.patch.object(mail, "new_id", side_effect=["letter-ffffffff", "letter-00000000"]):
            request = mail.send(self.led, "claude@mac", ["codex@mac"], "request", "问题", "正文")
            reply = mail.send(self.led, "codex@mac", [], "reply", "答复", "回信",
                              reply_to=request["id"])
        groups = board.threads(board.collect(self.led)["letters"])
        self.assertEqual([item["meta"]["id"] for item in groups[0]], [request["id"], reply["id"]])

    def test_cli_out_writes_only_requested_file(self):
        out = self.tmp / "requested-board.html"
        before = files_under(self.tmp)
        with contextlib.redirect_stdout(io.StringIO()), mock.patch("webbrowser.open") as browser:
            result = cli.main(["board", "--ledger", str(self.led.root), "--machine", "mac",
                               "--out", str(out)])
        self.assertEqual(result, 0)
        browser.assert_not_called()
        self.assertIn("<!doctype html>", out.read_text(encoding="utf-8"))
        after = files_under(self.tmp)
        after.pop(str(out.relative_to(self.tmp)))
        self.assertEqual(after, before)
        self.assertFalse(board.default_path(self.led).exists())

    def test_default_output_is_in_local_state_outside_ledger(self):
        before = files_under(self.led.root)
        path = board.write(self.led)
        self.assertEqual(path, self.led.state_dir / "board.html")
        self.assertFalse(util.is_within(path, self.led.root))
        self.assertEqual(files_under(self.led.root), before)


class DemoIsolation(Sandbox):
    def screen(self):
        return demo.Screen(out=io.StringIO(), pause=0, color=False)

    def test_complete_demo_restores_environment_and_leaves_existing_files_untouched(self):
        led = self.make_single("real")
        self.use_ledger(led)
        os.environ["YIZHAN_SEAT"] = "codex@real"
        self.folder("home").joinpath("local.json").write_text('{"sentinel":"keep"}', encoding="utf-8")
        self.folder("state").joinpath("sentinel").write_text("private state", encoding="utf-8")
        self.folder("claude").joinpath("settings.json").write_text("{}", encoding="utf-8")
        tour = self.folder("tour")
        environment = dict(os.environ)
        before = files_under(self.tmp)
        screen = self.screen()
        with mock.patch.object(demo.tempfile, "mkdtemp", return_value=str(tour)):
            result = demo.run(screen=screen, keep=False)
        self.assertEqual(result, tour)
        self.assertFalse(tour.exists())
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(files_under(self.tmp), before)
        self.assertIn("[5/5]", screen.out.getvalue())
        self.assertIn("codex@linux", screen.out.getvalue())

    def test_failure_restores_present_and_absent_environment_variables(self):
        tour = self.folder("failed-tour")
        os.environ.pop("YIZHAN_SEAT", None)
        os.environ["YIZHAN_LEDGER"] = str(self.tmp / "existing-ledger")
        environment = dict(os.environ)

        def fail_inside_demo(screen, root):
            self.assertEqual(Path(os.environ["YIZHAN_HOME"]), tour / "config")
            self.assertEqual(Path(os.environ["YIZHAN_STATE"]), tour / "state")
            self.assertNotIn("YIZHAN_LEDGER", os.environ)
            self.assertNotIn("YIZHAN_SEAT", os.environ)
            raise RuntimeError("interrupted demonstration")

        with mock.patch.object(demo.tempfile, "mkdtemp", return_value=str(tour)), \
                mock.patch.object(demo, "_play", side_effect=fail_inside_demo):
            with self.assertRaisesRegex(RuntimeError, "interrupted demonstration"):
                demo.run(screen=self.screen(), keep=False)
        self.assertEqual(dict(os.environ), environment)
        self.assertFalse(tour.exists())

    def test_inherited_git_dir_cannot_change_existing_repository_config(self):
        # An inherited GIT_DIR must not redirect the demo to another repository.
        # Every path here is inside the sandbox.
        led = self.make_single("real")
        config = led.root / ".git" / "config"
        before = config.read_bytes()
        tour = self.folder("git-dir-tour")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(led.root / ".git")}), \
                mock.patch.object(demo.tempfile, "mkdtemp", return_value=str(tour)):
            try:
                demo.run(screen=self.screen(), keep=False)
            except util.YizhanError:
                pass  # Check the isolation guarantee even when the tour fails.
        self.assertEqual(config.read_bytes(), before)
