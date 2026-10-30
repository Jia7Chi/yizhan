"""Thin wrapper around the git command line."""
from __future__ import annotations

import os
import subprocess

from .util import YizhanError


class GitError(YizhanError):
    pass


def _has_ssh_command(root):
    r = git(root, "config", "--get", "core.sshCommand", check=False)
    return r.returncode == 0 and bool(r.stdout.strip())


def _env(root, network):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"        # never hang on a password prompt
    if network and "GIT_SSH_COMMAND" not in env and not _has_ssh_command(root):
        # Fail fast instead of waiting for an interactive host-key or passphrase prompt.
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=15"
    return env


def git(root, *args, timeout=60, check=True, network=False, input_text=None):
    # --literal-pathspecs: a file named "a*.md" means that file, not a glob.
    cmd = ["git", "-c", "core.quotepath=off", "-C", str(root), "--literal-pathspecs", *args]
    try:
        r = subprocess.run(cmd, input=input_text, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=_env(root, network) if network else
                           dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except FileNotFoundError:
        raise GitError("没找到 git 命令，请先安装 git") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} 超过 {timeout} 秒没完成") from None
    if check and r.returncode != 0:
        msg = " ".join((r.stderr or r.stdout or "").split())[:400]
        raise GitError(f"git {' '.join(args[:2])} 失败：{msg}")
    return r


def is_repo(root):
    return git(root, "rev-parse", "--git-dir", check=False).returncode == 0


def init(root):
    if git(root, "init", "-q", "-b", "main", check=False).returncode != 0:
        git(root, "init", "-q")                       # git older than 2.28
        git(root, "symbolic-ref", "HEAD", "refs/heads/main")


def has_remote(root):
    return git(root, "remote", "get-url", "origin", check=False).returncode == 0


def branch(root):
    r = git(root, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    return r.stdout.strip() or "main"


def head(root):
    r = git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return r.stdout.strip() or None


def show(root, spec):
    r = git(root, "show", spec, check=False)
    return r.stdout if r.returncode == 0 else None


def identity_args(root, machine):
    """Commit as the user if git knows them, otherwise as 'yizhan <machine>'."""
    def known(key):
        return bool(git(root, "config", "--get", key, check=False).stdout.strip())
    args = []
    if not known("user.name"):
        args += ["-c", f"user.name=yizhan {machine}"]
    if not known("user.email"):
        args += ["-c", f"user.email={machine}@yizhan.invalid"]
    return args


def changed_paths(root):
    """Every path that differs from HEAD, including untracked and deleted files."""
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    items = out.split("\0")
    paths, i = [], 0
    while i < len(items):
        entry = items[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        paths.append(path)
        if xy[0] in "RC" and i < len(items):      # a rename also changes its source path
            paths.append(items[i])
            i += 1
    return paths


def ahead_of(root, upstream):
    r = git(root, "rev-list", "--count", f"{upstream}..HEAD", check=False)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 1
