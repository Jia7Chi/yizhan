"""Install or remove the background job that runs `yizhan wake` for one seat.

On one machine a letter wakes its reader the moment the sender's turn ends
(wake.kick), with no job at all. This job covers letters that arrive from other
machines through git, and letters whose sender never ends its turn. Its interval is
how soon such a letter should be noticed: a requirement you choose with --interval.

macOS uses a launchd agent, Linux a systemd user timer, Windows a scheduled task.
plan() describes everything first, so it can be shown before anything is installed.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import brief, util
from .util import YizhanError

DEFAULT_INTERVAL_S = 300    # how soon a letter from another machine gets noticed
SCHTASKS_MAX_TR = 261       # Windows refuses longer /TR command lines
SCHTASKS_MAX_MINUTES = 1439  # /SC MINUTE accepts /MO 1 to 1439 (schtasks /create docs)


def job_name(seat):
    return "yizhan-wake-" + seat.replace("@", "-")


def job_argv(seat):
    argv = brief.cli_argv() + ["wake", "--as", seat]
    if os.name == "nt" and argv[0].lower().endswith("python.exe"):
        quiet = argv[0][: -len("python.exe")] + "pythonw.exe"
        if os.path.isfile(quiet):
            argv[0] = quiet          # no console window flashing up every few minutes
    return argv


def current_platform():
    return "windows" if os.name == "nt" else sys.platform


def plan(ledger, seat, interval=DEFAULT_INTERVAL_S, platform=None):
    """Everything install() and uninstall() will do for this seat on this platform."""
    interval = int(interval)
    if interval < 60:
        raise YizhanError("间隔最少 60 秒：Windows 计划任务按整分钟算")
    platform = platform or current_platform()
    argv = job_argv(seat)
    name = job_name(seat)
    if platform == "darwin":
        return _launchd(name, argv, interval, ledger.state_dir / "wake" / f"{seat}.service.log")
    if platform == "windows":
        return _schtasks(name, argv, interval)
    return _systemd(name, seat, argv, interval)


def _launchd(name, argv, interval, log):
    label = f"io.github.{name}"
    path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    body = plistlib.dumps({
        "Label": label, "ProgramArguments": argv, "StartInterval": interval,
        "RunAtLoad": True, "ProcessType": "Background",
        "StandardOutPath": str(log), "StandardErrorPath": str(log),
    }).decode("utf-8")
    target = f"gui/{getattr(os, 'getuid', lambda: 0)()}"
    return {"kind": "launchd", "name": label, "dirs": [str(log.parent)], "files": {str(path): body},
            "before": [["launchctl", "bootout", f"{target}/{label}"]],
            "on": [["launchctl", "bootstrap", target, str(path)]],
            "off": [["launchctl", "bootout", f"{target}/{label}"]],
            "after_off": [],
            "check": ["launchctl", "print", f"{target}/{label}"]}


def _systemd_line(argv):
    """systemd's own quoting: double quotes, backslash escapes, %% for a literal % and
    $$ for a literal $, which systemd would otherwise expand as a variable."""
    out = []
    for a in argv:
        a = a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        out.append(f'"{a}"')
    return " ".join(out)


def _systemd(name, seat, argv, interval):
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
    service = (f"[Unit]\nDescription=yizhan: wake {seat} when letters need it\n\n"
               f"[Service]\nType=oneshot\nExecStart={_systemd_line(argv)}\n")
    timer = (f"[Unit]\nDescription=yizhan: check letters for {seat} every {interval} s\n\n"
             f"[Timer]\nOnBootSec={interval}\nOnUnitActiveSec={interval}\nUnit={name}.service\n\n"
             "[Install]\nWantedBy=timers.target\n")
    return {"kind": "systemd", "name": f"{name}.timer", "dirs": [],
            "files": {str(unit_dir / f"{name}.service"): service, str(unit_dir / f"{name}.timer"): timer},
            "before": [],
            "on": [["systemctl", "--user", "daemon-reload"],
                   ["systemctl", "--user", "enable", "--now", f"{name}.timer"]],
            "off": [["systemctl", "--user", "disable", "--now", f"{name}.timer"]],
            "after_off": [["systemctl", "--user", "daemon-reload"]],
            "check": ["systemctl", "--user", "is-active", "--quiet", f"{name}.timer"]}


def _schtasks(name, argv, interval):
    minutes = -(-interval // 60)            # round up: tasks repeat on whole minutes
    if minutes > SCHTASKS_MAX_MINUTES:
        raise YizhanError(f"Windows 计划任务按分钟重复，最长只能隔 {SCHTASKS_MAX_MINUTES} 分钟；"
                          f"把间隔设在 {SCHTASKS_MAX_MINUTES * 60} 秒以内")
    line = subprocess.list2cmdline(argv)
    if len(line) > SCHTASKS_MAX_TR:
        raise YizhanError(f"命令有 {len(line)} 个字符，Windows 计划任务最多放 {SCHTASKS_MAX_TR} 个；"
                          "把 yizhan 装到路径短一些的地方再试")
    return {"kind": "schtasks", "name": name, "dirs": [], "files": {},
            "before": [],
            "on": [["schtasks", "/Create", "/F", "/TN", name, "/SC", "MINUTE", "/MO", str(minutes), "/TR", line]],
            "off": [["schtasks", "/Delete", "/F", "/TN", name]],
            "after_off": [],
            "check": ["schtasks", "/Query", "/TN", name]}


def _run(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, (r.stderr or r.stdout).strip()


def install(p, run=_run):
    for d in p.get("dirs", []):
        Path(d).mkdir(parents=True, exist_ok=True)
    for path, text in p["files"].items():
        util.write_text_atomic(path, text)
    for argv in p.get("before", []):
        run(argv)                            # unload an older copy; failing here is fine
    for argv in p["on"]:
        code, out = run(argv)
        if code != 0:
            raise YizhanError(f"{' '.join(argv[:3])} 失败：{out[:300]}")


def uninstall(p, run=_run):
    for argv in p["off"]:
        run(argv)                            # already gone is fine
    for path in p["files"]:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    for argv in p.get("after_off", []):
        run(argv)


def installed(p, run=_run):
    return run(p["check"])[0] == 0
