"""Language selection and public CLI output, independent of the host locale."""
import contextlib
import io
import os
import subprocess
from types import SimpleNamespace
from unittest import mock

from helpers import Sandbox

from yizhan import cli, i18n, mail, records, util


class LanguageDetection(Sandbox):
    def setUp(self):
        super().setUp()
        for key in ("YIZHAN_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
            os.environ.pop(key, None)
        i18n.reset()

    def config(self, value):
        util.write_json(util.config_home() / "local.json", value)

    def test_explicit_language_overrides_config_and_locale(self):
        self.config({"lang": "zh"})
        with mock.patch.dict(os.environ, {"YIZHAN_LANG": "en", "LC_ALL": "zh_CN.UTF-8"}):
            self.assertEqual(i18n.pick("中文", "English"), "English")

    def test_chinese_variants_are_normalized(self):
        for value in ("zh", "zh_CN.UTF-8", "zh-Hant", " ZH_tw "):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"YIZHAN_LANG": value}):
                i18n.reset()
                self.assertEqual(i18n.lang(), "zh")

    def test_saved_language_overrides_locale(self):
        self.config({"lang": "en"})
        with mock.patch.dict(os.environ, {"LC_ALL": "zh_CN.UTF-8"}):
            self.assertEqual(i18n.lang(), "en")

    def test_locale_precedence_and_c_posix_fallthrough(self):
        cases = [
            ({"LC_ALL": "en_US.UTF-8", "LC_MESSAGES": "zh_CN", "LANG": "zh_CN"}, "en"),
            ({"LC_ALL": "", "LC_MESSAGES": "zh_CN", "LANG": "en_US"}, "zh"),
            ({"LC_ALL": "C", "LC_MESSAGES": "POSIX", "LANG": "zh_CN"}, "zh"),
            ({"LC_ALL": "C.UTF-8", "LC_MESSAGES": "", "LANG": "zh_TW"}, "zh"),
        ]
        for values, expected in cases:
            with self.subTest(values=values), mock.patch.dict(os.environ, values):
                i18n.reset()
                self.assertEqual(i18n.lang(), expected)

    def test_malformed_or_nonobject_config_falls_back_to_locale(self):
        path = util.config_home() / "local.json"
        for value in ("{broken", "[]", "null", '"en"'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"LANG": "zh_CN.UTF-8"}):
                util.write_text_atomic(path, value)
                i18n.reset()
                self.assertEqual(i18n.lang(), "zh")

    def test_macos_uses_first_preferred_language(self):
        for output, expected in [('(\n    "zh-Hans-CN",\n    "en-US"\n)\n', "zh"),
                                 ('(\n    "en-US",\n    "zh-Hans"\n)\n', "en")]:
            with self.subTest(expected=expected), \
                    mock.patch.object(i18n.sys, "platform", "darwin"), \
                    mock.patch.object(i18n.subprocess, "run", return_value=SimpleNamespace(stdout=output)) as run:
                i18n.reset()
                self.assertEqual(i18n.lang(), expected)
                run.assert_called_once_with(["defaults", "read", "-g", "AppleLanguages"],
                                            capture_output=True, text=True, timeout=3)

    def test_unavailable_macos_defaults_falls_back_to_english(self):
        for error in (OSError("unavailable"), subprocess.TimeoutExpired("defaults", 3)):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(i18n.sys, "platform", "darwin"), \
                    mock.patch.object(i18n.subprocess, "run", side_effect=error):
                i18n.reset()
                self.assertEqual(i18n.lang(), "en")

    def test_portable_c_locale_falls_back_without_platform_command(self):
        with mock.patch.dict(os.environ, {"LANG": "C.UTF-8"}), \
                mock.patch.object(i18n.sys, "platform", "linux"), \
                mock.patch.object(i18n.subprocess, "run") as run:
            self.assertEqual(i18n.lang(), "en")
            run.assert_not_called()

    def test_reset_applies_a_new_language_in_same_process(self):
        os.environ["YIZHAN_LANG"] = "en"
        self.assertEqual(i18n.lang(), "en")
        os.environ["YIZHAN_LANG"] = "zh"
        self.assertEqual(i18n.lang(), "en")
        i18n.reset()
        self.assertEqual(i18n.lang(), "zh")


class BilingualCommands(Sandbox):
    def run_cli(self, language, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"YIZHAN_LANG": language}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            i18n.reset()
            try:
                code = cli.main(list(args))
            except SystemExit as exc:
                code = exc.code
            finally:
                i18n.reset()
        return code, out.getvalue(), err.getvalue()

    def test_no_argument_welcome_and_help_in_each_language(self):
        for language, welcome, description in (
                ("en", "New here?", "Write a letter to other seats"),
                ("zh", "第一次用？", "写信给别的岗位")):
            with self.subTest(language=language):
                code, output, error = self.run_cli(language)
                self.assertEqual((code, error), (0, ""))
                self.assertIn(welcome, output)
                for command in ("yizhan demo", "yizhan setup", "yizhan board"):
                    self.assertIn(command, output)
                code, output, error = self.run_cli(language, "--help")
                self.assertEqual((code, error), (0, ""))
                self.assertIn(description, output)

    def test_missing_config_error_is_localized_without_traceback(self):
        for language, prefix, message in (
                ("en", "Error: ", "not connected"),
                ("zh", "错误：", "还没接入驿站")):
            with self.subTest(language=language):
                code, output, error = self.run_cli(language, "inbox", "--as", "codex@mac")
                self.assertEqual((code, output), (2, ""))
                self.assertTrue(error.startswith(prefix), error)
                self.assertIn(message, error)
                self.assertNotIn("Traceback", error)

    def test_empty_inbox_keeps_original_chinese_and_has_english_translation(self):
        led = self.make_single()
        led.add_seat("codex", "codex")
        self.use_ledger(led)
        for language, expected in (("en", "(no letters waiting for you)\n"),
                                   ("zh", "（没有待处理的信）\n")):
            self.assertEqual(self.run_cli(language, "inbox", "--as", "codex@mac"), (0, expected, ""))

    def test_letter_output_translates_labels_without_translating_user_content(self):
        led = self.make_single()
        for name in ("claude", "codex"):
            led.add_seat(name, name)
        self.use_ledger(led)
        letter = mail.send(led, "claude@mac", ["codex@mac"], "request", "中文主题", "原始正文", sealed=True)
        for language, label in (("en", "Subject: 中文主题"), ("zh", "主题：中文主题")):
            with self.subTest(language=language):
                code, output, error = self.run_cli(language, "show", letter["id"])
                self.assertEqual((code, error), (0, ""))
                self.assertIn(label, output)
                self.assertIn("原始正文", output)
        self.assertEqual(self.run_cli("en", "show", letter["id"], "--json"),
                         self.run_cli("zh", "show", letter["id"], "--json"))

    def test_persisted_status_codes_are_language_independent(self):
        led = self.make_single()
        led.add_project("review")
        for name in ("claude", "codex"):
            led.add_seat(name, name, projects=["review"])
        self.use_ledger(led)
        letter = mail.send(led, "claude@mac", ["codex@mac"], "request", "Check", "Body", sealed=True)
        code, output, error = self.run_cli("en", "ack", "--as", "codex@mac", letter["id"],
                                           "--status", "done", "--note", "已复核")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Recorded a receipt", output)
        closed = mail.inbox(led, "codex@mac", include_closed=True)
        self.assertEqual(closed[0]["receipt"]["status"], "done")
        code, output, error = self.run_cli("en", "decide", "--as", "claude@mac", "--project", "review",
                                           "--title", "保留中文", "--body", "记录", "--status", "confirmed")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Recorded the decision", output)
        self.assertEqual(records.decisions(led)[0]["status"], "confirmed")
