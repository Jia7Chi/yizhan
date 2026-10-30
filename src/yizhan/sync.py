"""Commit this machine's files, take in the other machines', push.

Three gates run before anything is committed, and any finding stops the whole batch:
1. ownership: every changed path must belong to this machine (see Ledger.owner_of);
2. secrets: nothing that looks like a key or token;
3. size: no file above max_file_mb; raw data stays out of the ledger.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import adapters, gitio, mail, state, util
from .ledger import SHARED
from .util import YizhanError

SECRET_PATTERNS = [re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\bghp_[A-Za-z0-9]{30,}",
    r"\bgho_[A-Za-z0-9]{30,}",
    r"\bgithub_pat_[A-Za-z0-9_]{30,}",
    r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    r"\bsk-proj-[A-Za-z0-9_-]{20,}",
    r"\bsk-[A-Za-z0-9]{32,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    r"\bAIza[0-9A-Za-z_-]{35}\b",
)]
MEMORY_SUFFIXES = {".md", ".txt", ".json"}


class SyncBlocked(YizhanError):
    def __init__(self, problems):
        self.problems = problems
        super().__init__("同步被拦下，什么都没提交：\n" + "\n".join("- " + p for p in problems))


def check(ledger, paths, allow_shared=False):
    problems = []
    max_mb = float(ledger.settings["max_file_mb"])
    for rel in paths:
        owner = ledger.owner_of(rel)
        if owner == SHARED and not allow_shared:
            problems.append(f"{rel} 是共用文件。改它要先跟其他机器打招呼，再加 --allow-shared 同步")
        elif owner not in (SHARED, ledger.machine):
            problems.append(f"{rel} 归 {owner} 写，本机（{ledger.machine}）不能改")
        f = ledger.root / rel
        if not f.is_file():
            continue                    # a deletion
        size = f.stat().st_size
        if size > max_mb * 1024 * 1024:
            problems.append(f"{rel} 有 {size / 1048576:.1f} MB，超过 {max_mb:g} MB 上限；原始数据别放进账本")
            continue
        try:
            text = f.read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            continue
        if any(rx.search(text) for rx in SECRET_PATTERNS):
            problems.append(f"{rel} 里像是有密钥或令牌，删掉再同步")
    return problems


def mirror_memory(ledger):
    """Copy each local seat's private memory into seats/<seat>/memory/, read-only for others."""
    notes = []
    max_bytes = float(ledger.settings["max_file_mb"]) * 1024 * 1024
    for prof in ledger.seats():
        if prof.get("machine") != ledger.machine:
            continue
        dest_root = ledger.seat_dir(prof["address"]) / "memory"
        wanted = {}
        for src in adapters.memory_dirs(prof):
            src = Path(src)
            if not src.is_dir():
                continue
            label = src.parent.name if src.name == "memory" else src.name
            for f in sorted(src.rglob("*")):
                if not f.is_file() or f.name.startswith(".") or f.suffix.lower() not in MEMORY_SUFFIXES:
                    continue
                if f.stat().st_size > max_bytes:
                    notes.append(f"记忆文件太大，没镜像：{f}")
                    continue
                wanted[f"{label}/{f.relative_to(src).as_posix()}"] = f
        existing = {}
        if dest_root.is_dir():
            existing = {p.relative_to(dest_root).as_posix(): p for p in dest_root.rglob("*") if p.is_file()}
        for rel, src in wanted.items():
            dst = dest_root / rel
            data = src.read_bytes()
            if not dst.is_file() or dst.read_bytes() != data:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)
        for rel, p in existing.items():
            if rel not in wanted:
                p.unlink()
    return notes


def integrate(root, timeout, machine):
    """Fetch, then fast-forward, or replay local commits after the remote ones."""
    branch = gitio.branch(root)
    gitio.git(root, "fetch", "-q", "origin", timeout=timeout, network=True)
    upstream = f"origin/{branch}"
    if gitio.git(root, "rev-parse", "--verify", "-q", upstream, check=False).returncode != 0:
        return "远端还没有这条分支"
    if gitio.head(root) is None:                  # nothing committed here yet
        gitio.git(root, "checkout", "-q", "-B", branch, upstream)
        return "已拉取"
    if gitio.git(root, "merge", "-q", "--ff-only", upstream, check=False).returncode == 0:
        return "已拉取"
    # Replaying commits (and the autostash) writes new commits, so git needs an identity.
    who = gitio.identity_args(root, machine)
    r = gitio.git(root, *who, "rebase", "-q", "--autostash", upstream, check=False)
    if r.returncode != 0:
        gitio.git(root, "rebase", "--abort", check=False)
        said = " ".join((r.stderr or r.stdout).split())[:300]
        hint = "多半是有机器改了不归它写的文件。" if "CONFLICT" in (r.stdout or "") + (r.stderr or "") else ""
        raise gitio.GitError(f"把本地提交接到远端之后失败，已撤回；本地提交还在，没推送。{hint}git 的原话：{said}")
    return "已拉取（本地提交接在远端之后）"


def pull_only(ledger, timeout=30):
    """Take in other machines' changes without committing anything (session start, waker)."""
    if not gitio.has_remote(ledger.root):
        return "没有远端"
    with util.file_lock(ledger.state_dir / "git.lock", timeout=5):
        return integrate(ledger.root, timeout, ledger.machine)


def sync(ledger, quick=False, allow_shared=False, push=True, net_timeout=60):
    """Gate, commit, pull, push. Network trouble is reported, not raised: the local
    commit stays and the next sync pushes it."""
    report = {"committed": None, "pulled": None, "pushed": False, "error": None, "notes": []}
    with util.file_lock(ledger.state_dir / "git.lock", timeout=net_timeout * 3):
        if not quick:
            report["notes"] += mirror_memory(ledger)
        paths = gitio.changed_paths(ledger.root)
        problems = check(ledger, paths, allow_shared)
        if problems:
            _record(ledger, error="；".join(problems))
            raise SyncBlocked(problems)
        if paths:
            gitio.git(ledger.root, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul",
                      input_text="\0".join(paths) + "\0")
            stamp = ledger.local_date().strftime("%Y-%m-%d %H:%M")
            gitio.git(ledger.root, *gitio.identity_args(ledger.root, ledger.machine),
                      "commit", "-q", "--no-verify", "-m", f"yizhan: {ledger.machine} {stamp}")
            report["committed"] = gitio.head(ledger.root)
        if push and gitio.has_remote(ledger.root):
            try:
                report["pulled"] = integrate(ledger.root, net_timeout, ledger.machine)
                upstream = f"origin/{gitio.branch(ledger.root)}"
                if gitio.head(ledger.root) and gitio.ahead_of(ledger.root, upstream):
                    gitio.git(ledger.root, "push", "-q", "-u", "origin",
                              f"HEAD:{gitio.branch(ledger.root)}", timeout=net_timeout, network=True)
                report["pushed"] = True
            except gitio.GitError as e:
                report["error"] = str(e)
        _record(ledger, report)
    return report


def needs_push(ledger, seat):
    """Has this seat written a letter to another machine since the last good push?"""
    if not gitio.has_remote(ledger.root):
        return False
    last = util.parse_iso(state.load(ledger, "sync").get("last_push_ok"))
    for path in sorted((ledger.seat_dir(seat) / "outbox").glob("*.md")):
        try:
            meta, _ = mail.read(path)
        except (OSError, ValueError):
            continue
        created = util.parse_iso(meta.get("created"))
        if last and created and created < last:
            continue
        if any(t.split("@", 1)[-1] != ledger.machine for t in meta.get("to", [])):
            return True
    return False


def _record(ledger, report=None, error=None):
    with state.edit(ledger, "sync") as st:
        now = util.iso(util.utcnow())
        st["last_run"] = now
        err = error or (report or {}).get("error")
        if err:
            st["last_error"], st["last_error_at"] = err[:600], now
        else:
            st.pop("last_error", None)
            st.pop("last_error_at", None)
        if report and report.get("pushed"):
            st["last_push_ok"] = now
