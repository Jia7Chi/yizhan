"""Hook installation and fresh-process language selection stay local and reversible."""
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import unittest
from unittest import mock

from helpers import ROOT, Sandbox

from yizhan import adapters, brief, util
from yizhan import ledger as ledger_mod


class HookInstallation(Sandbox):
    def settings(self, data=None, name="settings.json"):
        path = self.tmp / name
        if data is not None:
            path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_install_preserves_other_settings_and_mixed_handler_groups(self):
        other = {"type": "command", "command": "echo keep-me", "timeout": 7}
        old = {"type": "command", "command": "/old/yizhan hook --agent claude"}
        data = {"permissions": {"allow": ["Read"]}, "hooks": {
            "SessionStart": [{"matcher": "startup", "hooks": [other, old]}],
            "Notification": [{"hooks": [other]}],
        }}
        path = self.settings(data)
        before = path.read_bytes()
        result = adapters.install_hooks("claude", path, "/new/yizhan")
        current = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(current["permissions"], data["permissions"])
        self.assertEqual(current["hooks"]["Notification"], data["hooks"]["Notification"])
        self.assertEqual(current["hooks"]["SessionStart"][0],
                         {"matcher": "startup", "hooks": [other]})
        self.assertEqual(current["hooks"]["SessionStart"][1]["hooks"][0]["command"],
                         "/new/yizhan hook --agent claude")
        self.assertEqual(result["backup"].read_bytes(), before)

    def test_repeat_install_does_not_write_or_make_another_backup(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                path = self.settings({}, f"{agent}.json")
                adapters.install_hooks(agent, path, "yizhan")
                before = path.read_bytes(), path.stat().st_mtime_ns
                backups = list(self.tmp.glob(f"{agent}.json.bak-*"))
                result = adapters.install_hooks(agent, path, "yizhan")
                self.assertFalse(result["changed"])
                self.assertIsNone(result["backup"])
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
                self.assertEqual(list(self.tmp.glob(f"{agent}.json.bak-*")), backups)
                for groups in json.loads(path.read_text())["hooks"].values():
                    self.assertEqual(len(groups), 1)

    def test_uninstall_only_removes_matching_agent_and_keeps_other_handlers(self):
        other = {"type": "command", "command": "echo keep-me"}
        data = {"theme": "dark", "hooks": {
            "Notification": [{"matcher": "notice", "hooks": [other]}],
            "SessionStart": [{"hooks": [other]}],
        }}
        path = self.settings(data)
        adapters.install_hooks("codex", path, "yizhan")
        before_claude = json.loads(path.read_text())
        adapters.install_hooks("claude", path, "yizhan")
        removed = adapters.uninstall_hooks("claude", path)
        self.assertTrue(removed["changed"])
        self.assertEqual(json.loads(path.read_text()), before_claude)
        self.assertFalse(adapters.uninstall_hooks("claude", path)["changed"])
        adapters.uninstall_hooks("codex", path)
        self.assertEqual(json.loads(path.read_text()), data)

    def test_uninstall_removes_empty_hooks_block(self):
        path = self.settings({"theme": "dark"})
        adapters.install_hooks("codex", path, "yizhan")
        adapters.uninstall_hooks("codex", path)
        self.assertEqual(json.loads(path.read_text()), {"theme": "dark"})

    def test_agent_marker_in_unrelated_command_is_not_owned_by_yizhan(self):
        # Another tool may use the same hook arguments without belonging to yizhan.
        retained = {}
        for action in ("install", "uninstall"):
            with self.subTest(action=action):
                other = {"type": "command", "command": "other-tool hook --agent claude"}
                path = self.settings({"hooks": {"SessionStart": [{"hooks": [other]}]}},
                                     name=f"unrelated-{action}.json")
                if action == "install":
                    adapters.install_hooks("claude", path, "yizhan")
                else:
                    adapters.uninstall_hooks("claude", path)
                groups = json.loads(path.read_text()).get("hooks", {}).get("SessionStart", [])
                retained[action] = any(other in group["hooks"] for group in groups)
        self.assertEqual(retained, {"install": True, "uninstall": True})

    def test_yizhan_argument_to_logger_does_not_claim_unrelated_hook(self):
        retained = {}
        for action in ("install", "uninstall"):
            other = {"type": "command", "command": "/usr/bin/logger -- yizhan hook --agent claude"}
            path = self.settings({"hooks": {"SessionStart": [{"hooks": [other]}]}},
                                 name=f"logger-{action}.json")
            if action == "install":
                adapters.install_hooks("claude", path, "/local/bin/yizhan")
            else:
                adapters.uninstall_hooks("claude", path)
            groups = json.loads(path.read_text()).get("hooks", {}).get("SessionStart", [])
            retained[action] = any(other in group["hooks"] for group in groups)
        self.assertEqual(retained, {"install": True, "uninstall": True})

    def test_missing_and_empty_settings_are_supported(self):
        missing = self.settings(name="missing.json")
        self.assertIsNone(adapters.uninstall_hooks("claude", missing))
        self.assertFalse(missing.exists())
        self.assertIsNone(adapters.install_hooks("claude", missing, "yizhan")["backup"])
        for index, original in enumerate(("", " \n\t")):
            with self.subTest(original=original):
                path = self.settings(name=f"empty-{index}.json")
                path.write_text(original, encoding="utf-8")
                result = adapters.install_hooks("claude", path, "yizhan")
                self.assertEqual(result["backup"].read_text(), original)
                self.assertIn("SessionStart", json.loads(path.read_text())["hooks"])

    def test_invalid_json_and_non_objects_are_never_overwritten(self):
        for text in ('{"hooks":', "[]", '"text"', "null", "3"):
            for action in ("install", "uninstall"):
                with self.subTest(text=text, action=action):
                    path = self.settings(name="invalid.json")
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(util.YizhanError):
                        if action == "install":
                            adapters.install_hooks("claude", path, "yizhan")
                        else:
                            adapters.uninstall_hooks("claude", path)
                    self.assertEqual(path.read_text(), text)
                    self.assertEqual(list(self.tmp.glob("invalid.json.bak-*")), [])

    def test_backups_in_same_second_keep_every_previous_version(self):
        path = self.settings({"permissions": {"allow": []}})
        frozen = dt.datetime(2026, 10, 30, 8, 0, tzinfo=dt.timezone.utc)
        before = []
        backups = []
        with mock.patch("yizhan.adapters.util.utcnow", return_value=frozen):
            for command in ("first-yizhan", "second-yizhan", "third-yizhan"):
                before.append(path.read_bytes())
                backups.append(adapters.install_hooks("claude", path, command)["backup"])
        self.assertEqual(len(set(backups)), 3)
        self.assertEqual([p.read_bytes() for p in backups], before)
        self.assertEqual([p.name for p in backups], [
            "settings.json.bak-20261030080000", "settings.json.bak-20261030080000-2",
            "settings.json.bak-20261030080000-3",
        ])

    def test_dry_run_never_writes_or_creates_parent_or_backup(self):
        path = self.tmp / "missing" / "settings.json"
        result = adapters.install_hooks("claude", path, "yizhan", dry_run=True)
        self.assertTrue(result["changed"])
        self.assertFalse(path.parent.exists())
        adapters.install_hooks("claude", path, "yizhan")
        before = path.read_bytes()
        self.assertTrue(adapters.uninstall_hooks("claude", path, dry_run=True)["changed"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob("*.bak-*")), [])

    def test_folder_target_only_applies_to_claude(self):
        folder = self.folder("project")
        self.assertEqual(adapters.hook_target("claude", folder),
                         folder / ".claude" / "settings.local.json")
        self.assertEqual(adapters.hook_target("codex", folder), adapters.hook_target("codex"))


class HookGitExclusion(Sandbox):
    def repo_settings(self, name="project", subdir=""):
        root = self.folder(name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        path = root / subdir / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")
        return root, path

    def ignored(self, root, path):
        # --no-index: plain check-ignore reads its argument as a pattern and skips tracked
        # files it matches, so a tracked "demo1/..." would hide the rules for "demo[1]/...".
        return subprocess.run(["git", "-C", str(root), "check-ignore", "--no-index", "-q", str(path)],
                              capture_output=True).returncode == 0

    def test_new_exclusion_is_local_and_idempotent(self):
        root, path = self.repo_settings()
        exclude = root / ".git" / "info" / "exclude"
        exclude.write_text("# existing without final newline", encoding="utf-8")
        self.assertTrue(adapters.keep_out_of_git(path))
        self.assertTrue(self.ignored(root, path))
        after = exclude.read_bytes()
        self.assertTrue(after.startswith(b"# existing without final newline\n"))
        self.assertFalse(adapters.keep_out_of_git(path))
        self.assertEqual(exclude.read_bytes(), after)
        self.assertFalse((root / ".gitignore").exists())

    def test_already_ignored_settings_do_not_touch_exclude(self):
        root, path = self.repo_settings()
        (root / ".gitignore").write_text(".claude/\n", encoding="utf-8")
        exclude = root / ".git" / "info" / "exclude"
        before = exclude.read_bytes()
        self.assertFalse(adapters.keep_out_of_git(path))
        self.assertEqual(exclude.read_bytes(), before)

    def test_tracked_settings_do_not_touch_exclude_or_index(self):
        root, path = self.repo_settings()
        subprocess.run(["git", "-C", str(root), "add", str(path)], check=True)
        exclude, index = root / ".git/info/exclude", root / ".git/index"
        before = exclude.read_bytes(), index.read_bytes()
        self.assertFalse(adapters.keep_out_of_git(path))
        self.assertEqual((exclude.read_bytes(), index.read_bytes()), before)

    def test_non_repository_is_unchanged(self):
        folder = self.folder("not-a-repository")
        path = folder / "settings.local.json"
        path.write_text("{}", encoding="utf-8")
        self.assertFalse(adapters.keep_out_of_git(path))
        self.assertEqual(sorted(p.name for p in folder.iterdir()), [path.name])

    def test_literal_brackets_in_project_path_are_escaped_for_gitignore(self):
        root, path = self.repo_settings(subdir="demo[1]")
        self.assertTrue(adapters.keep_out_of_git(path))
        self.assertTrue(self.ignored(root, path))
        self.assertFalse(adapters.keep_out_of_git(path))

    def test_bracket_path_is_not_confused_with_a_tracked_sibling(self):
        root, path = self.repo_settings(subdir="demo[1]")
        tracked = root / "demo1" / ".claude" / "settings.local.json"
        tracked.parent.mkdir(parents=True)
        tracked.write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "--literal-pathspecs", "add", str(tracked)], check=True)
        index = root / ".git" / "index"
        before = index.read_bytes()
        backup = adapters.install_hooks("claude", path, "/local/bin/yizhan")["backup"]

        adapters.keep_out_of_git(path)

        self.assertEqual(index.read_bytes(), before)
        self.assertFalse(self.ignored(root, tracked))
        self.assertEqual((self.ignored(root, path), self.ignored(root, backup)), (True, True))
        exclude = root / ".git" / "info" / "exclude"
        after = exclude.read_bytes()
        self.assertFalse(adapters.keep_out_of_git(path))
        self.assertEqual(exclude.read_bytes(), after)

    def test_local_hook_backups_are_also_kept_out_of_git(self):
        root, path = self.repo_settings()
        adapters.install_hooks("claude", path, "/old/local/yizhan")
        adapters.keep_out_of_git(path)
        result = adapters.install_hooks("claude", path, "/new/local/yizhan")
        self.assertTrue(self.ignored(root, path))
        self.assertTrue(self.ignored(root, result["backup"]))


class HookSubprocessLanguage(Sandbox):
    def hook_process(self, env_language, config_language):
        led = self.make_single()
        folder = self.folder("project with spaces")
        led.add_seat("claude", "claude", paths=[str(folder)])
        ledger_mod.save_local_config(led.root, led.machine, lang=config_language)
        target = adapters.hook_target("claude", folder)
        command = shlex.join([sys.executable, str(ROOT / "bin" / "yizhan")])
        adapters.install_hooks("claude", target, command)
        generated = json.loads(target.read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        env = dict(os.environ, LC_ALL="C", LC_MESSAGES="C", LANG="C.UTF-8")
        env.pop("YIZHAN_LANG", None)
        env.pop("YIZHAN_LEDGER", None)
        env.pop("YIZHAN_MACHINE", None)
        if env_language is not None:
            env["YIZHAN_LANG"] = env_language
        result = subprocess.run(shlex.split(generated), input=json.dumps({
            "hook_event_name": "SessionStart", "cwd": str(folder), "session_id": "fresh-process",
        }), text=True, capture_output=True, env=env, cwd=str(folder), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_fresh_hook_reads_english_from_local_config_under_c_locale(self):
        text = self.hook_process(None, "en")
        self.assertIn(brief.BANNER_EN, text)
        self.assertNotIn(brief.BANNER, text)

    def test_explicit_chinese_overrides_english_local_config(self):
        text = self.hook_process("zh", "en")
        self.assertIn(brief.BANNER, text)
        self.assertNotIn(brief.BANNER_EN, text)

    def test_explicit_english_overrides_chinese_local_config(self):
        text = self.hook_process("en", "zh")
        self.assertIn(brief.BANNER_EN, text)
        self.assertNotIn(brief.BANNER, text)
