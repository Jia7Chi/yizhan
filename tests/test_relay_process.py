"""Process and command-template regressions from the relay review."""
import errno
import os
import shlex
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import Sandbox

from yizhan import relay, util


class RelayCommands(Sandbox):
    def test_windows_templates_keep_paths_and_quoted_arguments(self):
        cases = [
            (r'C:\tools\kimi.exe -p {prompt}',
             [r'C:\tools\kimi.exe', '-p', '{prompt}']),
            (r'"C:\Program Files\agent.exe" --file "C:\my work\prompt.md"',
             [r'C:\Program Files\agent.exe', '--file', r'C:\my work\prompt.md']),
            (r'agent.exe "" "say \"hello\"" "C:\work folder\\"',
             ['agent.exe', '', 'say "hello"', 'C:\\work folder\\']),
            (r'\\server\share\agent.exe --label="two words"',
             [r'\\server\share\agent.exe', '--label=two words']),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(relay.split_command(text, windows=True), expected)
                joined = relay.join_command(expected, windows=True)
                self.assertEqual(relay.split_command(joined, windows=True), expected)

    @unittest.skipUnless(os.name == 'posix', 'uses a POSIX executable script')
    def test_relative_program_and_path_are_pinned_before_workdir_changes(self):
        install = self.folder('install')
        workdir = self.folder('work')
        script = install / 'fake-agent'
        script.write_text('#!/bin/sh\nprintf "%s" "$1"\n', encoding='utf-8')
        script.chmod(0o755)
        old_cwd = os.getcwd()
        try:
            os.chdir(install)
            with mock.patch.dict(os.environ, {'PATH': '.'}):
                for command in ('./fake-agent', 'fake-agent'):
                    with self.subTest(command=command):
                        self.assertEqual(relay.program(command), str(script))
                        template = relay.absolute(command + ' "two words"')
                        result = subprocess.run(shlex.split(template), cwd=workdir,
                                                capture_output=True, text=True, check=True)
                        self.assertEqual(result.stdout, 'two words')
        finally:
            os.chdir(old_cwd)

    def test_argument_length_errors_pause_but_launch_errors_remain_retryable(self):
        win_error = OSError('Windows command line too long')
        win_error.winerror = 206
        for error in (OSError(errno.E2BIG, 'argument list too long'), win_error):
            with self.subTest(error=str(error)), \
                    mock.patch.object(util, 'run_tree', side_effect=error):
                with self.assertRaises(relay.PromptTooLong):
                    relay.run_cli([sys.executable], self.tmp, None,
                                  self.tmp / 'answer.md', self.tmp / 'run.log', 1)
        with mock.patch.object(util, 'run_tree', side_effect=FileNotFoundError('not installed')):
            self.assertEqual(relay.run_cli([sys.executable], self.tmp, None,
                                          self.tmp / 'answer.md', self.tmp / 'run.log', 1), -1)


@unittest.skipUnless(os.name == 'posix', 'checks POSIX process-group cleanup')
class ProcessGroups(Sandbox):
    def exercise_group_cleanup(self, parent_waits):
        # The child announces it is running, then waits for us to release it after
        # run_tree returns. A surviving child would then alter the work folder.
        child = self.tmp / 'child.py'
        child.write_text(
            'from pathlib import Path\n'
            'import time\n'
            'Path("ready").write_text("ready")\n'
            'deadline = time.monotonic() + 10\n'
            'while not Path("release").exists() and time.monotonic() < deadline:\n'
            '    time.sleep(.01)\n'
            'if Path("release").exists(): Path("late-write").write_text("changed")\n',
            encoding='utf-8',
        )
        parent = self.tmp / 'parent.py'
        parent.write_text(
            'from pathlib import Path\n'
            'import subprocess,sys,time\n'
            'subprocess.Popen([sys.executable, sys.argv[1]])\n'
            'deadline = time.monotonic() + 5\n'
            'while not Path("ready").exists() and time.monotonic() < deadline:\n'
            '    time.sleep(.01)\n'
            'if sys.argv[2] == "wait": time.sleep(10)\n',
            encoding='utf-8',
        )
        with (self.tmp / 'output.log').open('w', encoding='utf-8') as out:
            rc = util.run_tree([sys.executable, str(parent), str(child),
                                'wait' if parent_waits else 'exit'],
                               self.tmp, out, out, 2)
        self.assertTrue((self.tmp / 'ready').exists(), 'child must start before cleanup')
        (self.tmp / 'release').touch()
        time.sleep(.25)
        self.assertFalse((self.tmp / 'late-write').exists(), 'child outlived the command')
        self.assertEqual(rc, None if parent_waits else 0)

    def test_timeout_stops_child_processes(self):
        self.exercise_group_cleanup(parent_waits=True)

    def test_successful_exit_stops_remaining_child_processes(self):
        self.exercise_group_cleanup(parent_waits=False)

    def test_stdin_prompt_is_delivered_as_utf8(self):
        output = self.tmp / 'output.txt'
        with output.open('w', encoding='utf-8') as out:
            rc = util.run_tree([sys.executable, '-c',
                                'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'],
                               self.tmp, out, out, 2, input_text='信件内容\n')
        self.assertEqual(rc, 0)
        self.assertEqual(output.read_bytes(), '信件内容\n'.encode('utf-8'))
