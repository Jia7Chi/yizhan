"""Install or remove the background job that runs `yizhan wake` for one seat.

On one machine a letter wakes its reader the moment the sender's turn ends
(wake.kick), with no job at all. This job covers letters that arrive from other
machines through git, and letters whose sender never ends its turn. Its interval is
how soon such a letter should be noticed: a requirement you choose with --interval.

macOS uses a launchd agent, Linux a systemd user timer, Windows a scheduled task.
handle() is all that checking or removing a job needs; plan() adds what installing
needs and checks it, so it can be shown before anything is installed.
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


def _launchd_target():
    return f"gui/{getattr(os, 'getuid', lambda: 0)()}"


def handle(ledger, seat, platform=None):
    """Enough to find, check or remove an installed job: its name, its files and the
    commands that stop it. It builds no command line and reads no interval, so a job can
    always be checked and removed, even when its saved settings would not pass plan()."""
    platform = platform or current_platform()
    name = job_name(seat)
    base = {"dirs": [], "before": [], "on": [], "after_off": []}
    if platform == "darwin":
        label = f"io.github.{name}"
        path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        job = f"{_launchd_target()}/{label}"
        return dict(base, kind="launchd", name=label, files={str(path): None},
                    off=[["launchctl", "bootout", job]], check=["launchctl", "print", job])
    if platform == "windows":
        return dict(base, kind="schtasks", name=name, files={},
                    off=[["schtasks", "/Delete", "/F", "/TN", name]],
                    check=["schtasks", "/Query", "/TN", name])
    unit = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / name
    return dict(base, kind="systemd", name=f"{name}.timer",
                files={f"{unit}.service": None, f"{unit}.timer": None},
                off=[["systemctl", "--user", "disable", "--now", f"{name}.timer"]],
                after_off=[["systemctl", "--user", "daemon-reload"]],
                check=["systemctl", "--user", "is-active", "--quiet", f"{name}.timer"])


def plan(ledger, seat, interval=DEFAULT_INTERVAL_S, platform=None):
    """Everything install() will do: handle() plus the file contents and the commands
    that load the job. The interval and the command line are checked here, and only here."""
    interval = int(interval)
    if interval < 60:
        raise YizhanError("间隔最少 60 秒：Windows 计划任务按整分钟算")
    p = handle(ledger, seat, platform)
    argv = job_argv(seat)
    if p["kind"] == "launchd":
        _fill_launchd(p, argv, interval, ledger.state_dir / "wake" / f"{seat}.service.log")
    elif p["kind"] == "schtasks":
        _fill_schtasks(p, argv, interval)
    else:
        _fill_systemd(p, seat, argv, interval)
    return p


def _fill_launchd(p, argv, interval, log):
    path = next(iter(p["files"]))
    p["files"][path] = plistlib.dumps({
        "Label": p["name"], "ProgramArguments": argv, "StartInterval": interval,
        "RunAtLoad": True, "ProcessType": "Background",
        "StandardOutPath": str(log), "StandardErrorPath": str(log),
    }).decode("utf-8")
    p["dirs"] = [str(log.parent)]
    p["before"] = [list(cmd) for cmd in p["off"]]       # unload an older copy first
    p["on"] = [["launchctl", "bootstrap", _launchd_target(), path]]


def _systemd_line(argv):
    """systemd's own quoting: double quotes, backslash escapes, %% for a literal % and
    $$ for a literal $, which systemd would otherwise expand as a variable."""
    out = []
    for a in argv:
        a = a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        out.append(f'"{a}"')
    return " ".join(out)


def _fill_systemd(p, seat, argv, interval):
    service_path, timer_path = list(p["files"])
    unit = Path(service_path).stem
    p["files"][service_path] = (f"[Unit]\nDescription=yizhan: wake {seat} when letters need it\n\n"
                                f"[Service]\nType=oneshot\nExecStart={_systemd_line(argv)}\n")
    p["files"][timer_path] = (f"[Unit]\nDescription=yizhan: check letters for {seat} every {interval} s\n\n"
                              f"[Timer]\nOnBootSec={interval}\nOnUnitActiveSec={interval}\nUnit={unit}.service\n\n"
                              "[Install]\nWantedBy=timers.target\n")
    p["on"] = [["systemctl", "--user", "daemon-reload"],
               ["systemctl", "--user", "enable", "--now", p["name"]]]


def _fill_schtasks(p, argv, interval):
    minutes = -(-interval // 60)            # round up: tasks repeat on whole minutes
    if minutes > SCHTASKS_MAX_MINUTES:
        raise YizhanError(f"Windows 计划任务按分钟重复，最长只能隔 {SCHTASKS_MAX_MINUTES} 分钟；"
                          f"把间隔设在 {SCHTASKS_MAX_MINUTES * 60} 秒以内")
    line = subprocess.list2cmdline(argv)
    if len(line) > SCHTASKS_MAX_TR:
        raise YizhanError(f"命令有 {len(line)} 个字符，Windows 计划任务最多放 {SCHTASKS_MAX_TR} 个；"
                          "把 yizhan 装到路径短一些的地方再试")
    p["on"] = [["schtasks", "/Create", "/F", "/TN", p["name"], "/SC", "MINUTE", "/MO", str(minutes),
                "/TR", line]]


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
