"""Thin wrapper around the git command line."""
from __future__ import annotations

import os
import subprocess

from .i18n import pick
from .util import YizhanError


class GitError(YizhanError):
    pass


# Variables that point git at some repository, index or object store. yizhan always
# names the repository with -C; a value inherited from the caller (a git hook sets
# GIT_DIR, for one) would otherwise send yizhan's commands to that other repository.
REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
             "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX")


def base_env():
    """The environment for every git yizhan runs: no inherited repository, no prompts."""
    env = {k: v for k, v in os.environ.items() if k not in REPO_VARS}
    env["GIT_TERMINAL_PROMPT"] = "0"        # never hang on a password prompt
    return env


def _has_ssh_command(root):
    r = git(root, "config", "--get", "core.sshCommand", check=False)
    return r.returncode == 0 and bool(r.stdout.strip())


def _env(root, network):
    env = base_env()
    if network and "GIT_SSH_COMMAND" not in env and not _has_ssh_command(root):
        # Fail fast instead of waiting for an interactive host-key or passphrase prompt.
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=15"
    return env


def git(root, *args, timeout=60, check=True, network=False, input_text=None, env_extra=None, raw=False):
    """Run git in `root`. `env_extra` sets variables on purpose, such as a scratch
    GIT_INDEX_FILE; inherited ones that would point git elsewhere are always dropped.
    With raw=True, stdout comes back as bytes, exactly as git wrote it (a patch, say)."""
    # --literal-pathspecs: a file named "a*.md" means that file, not a glob.
    cmd = ["git", "-c", "core.quotepath=off", "-C", str(root), "--literal-pathspecs", *args]
    env = _env(root, network) if network else base_env()
    env.update(env_extra or {})
    text = {} if raw else {"text": True, "encoding": "utf-8", "errors": "replace"}
    try:
        r = subprocess.run(cmd, input=input_text, capture_output=True, timeout=timeout, env=env, **text)
        if raw:
            r.stderr = r.stderr.decode("utf-8", errors="replace")
    except FileNotFoundError:
        raise GitError(pick("没找到 git 命令，请先安装 git",
                            "The git command was not found; install git first")) from None
    except subprocess.TimeoutExpired:
        raise GitError(pick(f"git {args[0]} 超过 {timeout} 秒没完成",
                            f"git {args[0]} did not finish within {timeout} s")) from None
    if check and r.returncode != 0:
        msg = " ".join((r.stderr or ("" if raw else r.stdout) or "").split())[:400]
        raise GitError(pick(f"git {' '.join(args[:2])} 失败：{msg}", f"git {' '.join(args[:2])} failed: {msg}"))
    return r


def toplevel(path):
    """The root of the git repository that holds `path`, or None."""
    try:
        r = git(path, "rev-parse", "--show-toplevel", check=False, timeout=10)
    except GitError:
        return None
    out = r.stdout.strip()
    return os.path.normpath(out) if r.returncode == 0 and out else None


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
