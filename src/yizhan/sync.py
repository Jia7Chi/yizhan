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
from .i18n import pick
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
        super().__init__(pick("同步被拦下，什么都没提交：\n", "Sync blocked, nothing was committed:\n")
                         + "\n".join("- " + p for p in problems))


def check(ledger, paths, allow_shared=False):
    problems = []
    max_mb = float(ledger.settings["max_file_mb"])
    for rel in paths:
        owner = ledger.owner_of(rel)
        if owner == SHARED and not allow_shared:
            problems.append(pick(f"{rel} 是共用文件。改它要先跟其他机器打招呼，再加 --allow-shared 同步",
                                 f"{rel} is a shared file. Tell the other machines before you change it, "
                                 "then sync with --allow-shared"))
        elif owner not in (SHARED, ledger.machine):
            problems.append(pick(f"{rel} 归 {owner} 写，本机（{ledger.machine}）不能改",
                                 f"{rel} is written by {owner}; this machine ({ledger.machine}) may not change it"))
        f = ledger.root / rel
        if not f.is_file():
            continue                    # a deletion
        size = f.stat().st_size
        if size > max_mb * 1024 * 1024:
            problems.append(pick(f"{rel} 有 {size / 1048576:.1f} MB，超过 {max_mb:g} MB 上限；原始数据别放进账本",
                                 f"{rel} is {size / 1048576:.1f} MB, over the {max_mb:g} MB limit; keep raw data "
                                 "out of the ledger"))
            continue
        try:
            text = f.read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            continue
        if any(rx.search(text) for rx in SECRET_PATTERNS):
            problems.append(pick(f"{rel} 里像是有密钥或令牌，删掉再同步",
                                 f"{rel} seems to contain a key or token; remove it, then sync again"))
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
                    notes.append(pick(f"记忆文件太大，没镜像：{f}", f"Memory file too large, not mirrored: {f}"))
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
        return pick("远端还没有这条分支", "The remote does not have this branch yet")
    if gitio.head(root) is None:                  # nothing committed here yet
        gitio.git(root, "checkout", "-q", "-B", branch, upstream)
        return pick("已拉取", "Pulled")
    if gitio.git(root, "merge", "-q", "--ff-only", upstream, check=False).returncode == 0:
        return pick("已拉取", "Pulled")
    # Replaying commits (and the autostash) writes new commits, so git needs an identity.
    who = gitio.identity_args(root, machine)
    r = gitio.git(root, *who, "rebase", "-q", "--autostash", upstream, check=False)
    if r.returncode != 0:
        gitio.git(root, "rebase", "--abort", check=False)
        said = " ".join((r.stderr or r.stdout).split())[:300]
        hint = (pick("多半是有机器改了不归它写的文件。", "Most likely a machine changed a file it does not own. ")
                if "CONFLICT" in (r.stdout or "") + (r.stderr or "") else "")
        raise gitio.GitError(pick(f"把本地提交接到远端之后失败，已撤回；本地提交还在，没推送。{hint}git 的原话：{said}",
                                  "Could not replay the local commits on top of the remote ones, so that was undone; "
                                  f"the local commits are still here and were not pushed. {hint}git said: {said}"))
    return pick("已拉取（本地提交接在远端之后）", "Pulled (local commits replayed on top of the remote ones)")


def pull_only(ledger, timeout=30):
    """Take in other machines' changes without committing anything (session start, waker)."""
    if not gitio.has_remote(ledger.root):
        return pick("没有远端", "No remote")
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
            _record(ledger, error=pick("；", "; ").join(problems))
            raise SyncBlocked(problems)
        if paths:
            gitio.git(ledger.root, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul",
                      input_text="\0".join(paths) + "\0")
            stamp = ledger.local_date().strftime("%Y-%m-%d %H:%M")
            what = summarize(paths)
            gitio.git(ledger.root, *gitio.identity_args(ledger.root, ledger.machine),
                      "commit", "-q", "--no-verify", "-m",
                      f"yizhan: {ledger.machine} {stamp}" + (f" · {what}" if what else ""))
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


_COUNTED = {"letter": "letters", "receipt": "receipts", "decision": "decisions",
            "seat": "seats", "project": "projects", "machine": "machines"}
_ORDER = ("letter", "receipt", "decision", "journal", "seat", "project", "machine", "memory", "status", "files")
_LABEL = {"files": "shared files"}       # root files such as yizhan.json belong to every machine


def summarize(paths):
    """What a commit carries, for `git log`: '2 letters, 1 receipt, journal'. Seats, projects
    and machines are counted by name, since one of them may touch several files."""
    seen = {}
    for p in paths:
        p = p.replace("\\", "/")
        if "/outbox/" in p:
            key = "letter"
        elif "/receipts/" in p:
            key = "receipt"
        elif "/decisions/" in p:
            key = "decision"
        elif "/journal/" in p:
            key = "journal"
        elif "/memory/" in p:
            key = "memory"
        elif p.endswith("/presence.json"):
            key = "status"
        elif p.endswith("/profile.json"):
            key = "seat"
        elif p.startswith("machines/"):
            key = "machine"
        elif p.startswith("projects/"):
            key = "project"
        else:
            key = "files"
        parts_of_path = p.split("/")
        name = parts_of_path[1] if key in ("seat", "project", "machine") and len(parts_of_path) > 1 else p
        seen.setdefault(key, set()).add(name)
    parts = []
    for key in _ORDER:
        n = len(seen.get(key, ()))
        if n:
            parts.append(f"{n} {key if n == 1 else _COUNTED[key]}" if key in _COUNTED else _LABEL.get(key, key))
    return ", ".join(parts)


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
