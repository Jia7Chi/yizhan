import contextlib
import io
import json
import os

from helpers import Sandbox

from yizhan import adapters, cli, sync, util


class Util(Sandbox):
    def test_frontmatter_round_trip(self):
        meta = {"id": "x", "subject": "中文主题", "to": ["a@b"]}
        body = "第一段\n---\n不是文件头的分隔线\n"
        got_meta, got_body = util.load_frontmatter(util.dump_frontmatter(meta, body))
        self.assertEqual(got_meta, meta)
        self.assertEqual(got_body, body.rstrip())

    def test_title_slug_is_safe_everywhere(self):
        slug = util.title_slug('采样周期/定为:1ms?  "暂定"*')
        self.assertFalse(set(slug) & set('<>:"/\\|?*'))
        self.assertTrue(slug.startswith("采样周期"))

    def test_offsets_and_paths(self):
        self.assertEqual(util.parse_offset("+08:00").utcoffset(None).total_seconds(), 8 * 3600)
        self.assertRegex(util.system_offset(), r"^[+-]\d{2}:\d{2}$")
        a, ab = self.folder("a"), self.folder("ab")
        self.assertTrue(util.is_within(self.folder("a/sub"), a))
        self.assertFalse(util.is_within(ab, a))


class ClaudeMemory(Sandbox):
    def test_encoding_matches_claude_code(self):
        self.assertEqual(adapters.encode_claude_project("/Users/alice/Desktop/个人AI开源项目"),
                         "-Users-alice-Desktop---AI----")

    def test_chinese_folders_of_equal_length_collide(self):
        p1, p2 = self.folder("论文初稿"), self.folder("实验记录")
        seats = [{"agent": "claude", "paths": [str(p1)]}, {"agent": "claude", "paths": [str(p2)]}]
        groups = adapters.memory_collisions(seats)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(next(iter(groups.values()))), 2)
        f, snippet = adapters.collision_fix(str(p1))
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(snippet), encoding="utf-8")
        self.assertEqual(adapters.memory_collisions(seats), {})

    def test_memory_is_mirrored_and_pruned(self):
        led = self.make_single()
        proj = self.folder("proj")
        led.add_seat("claude", "claude", paths=[str(proj)])
        mem, _ = adapters.claude_memory_dir(proj)
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text("- 记一条\n", encoding="utf-8")
        sync.sync(led)
        mirrored = led.seat_dir("claude@mac") / "memory" / mem.parent.name / "MEMORY.md"
        self.assertEqual(mirrored.read_text(encoding="utf-8"), "- 记一条\n")
        (mem / "MEMORY.md").unlink()
        sync.sync(led)
        self.assertFalse(mirrored.exists())

    def test_hook_config_shape(self):
        for agent in ("claude", "codex"):
            cfg = adapters.hook_config(agent, "yizhan")["hooks"]
            self.assertEqual(set(cfg), {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"})
            handler = cfg["SessionStart"][0]["hooks"][0]
            self.assertEqual(handler["type"], "command")
            self.assertEqual(handler["command"], f"yizhan hook --agent {agent}")
            self.assertIsInstance(handler["timeout"], int)


class CommandLine(Sandbox):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def test_a_whole_day(self):
        led_dir, paper, ident = self.tmp / "ledger", self.folder("paper"), self.folder("ident")
        steps = [
            ("init", str(led_dir), "--machine", "mac", "--timezone", "+08:00"),
            ("project", "add", "demo", "--title", "示例课题", "--summary", "一篇论文"),
            ("seat", "add", "claude", "--agent", "claude", "--title", "论文主稿",
             "--project", "demo", "--path", str(paper)),
            ("seat", "add", "codex", "--agent", "codex", "--title", "推导复核",
             "--project", "demo", "--path", str(ident), "--command", "codex"),
            ("send", "--as", "claude@mac", "--to", "codex@mac", "--subject", "请复核",
             "--body", "看第三节", "--now"),
            ("inbox", "--as", "codex@mac"),
            ("focus", "--as", "codex@mac", "复核第三节"),
            ("log", "--as", "codex@mac", "--title", "复核开始"),
            ("decide", "--as", "claude@mac", "--project", "demo", "--title", "采样周期定为 1 ms",
             "--body", "控制回路按 1 kHz 运行"),
            ("decisions",),
            ("roster",),
            ("brief", "--as", "claude@mac"),
            ("hooks", "--agent", "codex"),
            ("wake", "--as", "codex@mac", "--dry-run"),
            ("sync",),
            ("status",),
        ]
        for step in steps:
            code, text = self.run_cli(*step)
            self.assertEqual(code, 0, f"{step} -> {code}\n{text}")
        code, text = self.run_cli("inbox", "--as", "codex@mac")
        mid = text.split()[0]
        self.assertEqual(self.run_cli("show", mid)[0], 0)
        self.assertEqual(self.run_cli("ack", "--as", "codex@mac", mid, "--status", "done", "--note", "已复核")[0], 0)
        self.assertIn("没有待处理", self.run_cli("inbox", "--as", "codex@mac")[1])
        code, text = self.run_cli("doctor")
        self.assertIn(code, (0, 1), text)

    def test_errors_are_plain_messages(self):
        os.environ.pop("YIZHAN_LEDGER", None)
        code, text = self.run_cli("inbox", "--as", "claude@mac")
        self.assertEqual(code, 2)
        self.assertIn("还没接入驿站", text)
