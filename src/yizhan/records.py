"""Decisions (a project's settled conclusions), journals (each seat's session log)
and focus (what a seat is working on right now)."""
from __future__ import annotations

import datetime as dt
import glob
import re
import secrets

from . import util
from .i18n import pick
from .ledger import DECISION_RE
from .util import YizhanError

DECISION_STATUSES = ("confirmed", "pending", "retracted")
DECISION_ZH = {"confirmed": "已确认", "pending": "待复核", "retracted": "已撤回"}


def decision_label(status):
    return pick(DECISION_ZH.get(status, status), status)
ENTRY_RE = re.compile(r"^### (\d{2}):(\d{2}) (.+)$")


# ---------- decisions ----------

def decisions(ledger, project=None):
    base = ledger.root / "projects"
    items = []
    for path in sorted(base.glob("*/decisions/*.md")) if base.is_dir() else []:
        if not DECISION_RE.match(path.name):
            continue
        try:
            meta, _ = util.read_frontmatter(path)
        except (OSError, ValueError):
            continue
        if project and meta.get("project") != project:
            continue
        items.append(meta)
    replaced = {}
    for m in items:
        for old in m.get("supersedes", []):
            replaced[old] = m["id"]
    for m in items:
        m["superseded_by"] = replaced.get(m["id"])
    items.sort(key=lambda m: m.get("created", ""), reverse=True)
    return items


def find_decision(ledger, did):
    # A title may contain "[...]", which glob would read as a character class.
    hits = sorted((ledger.root / "projects").glob(f"*/decisions/{glob.escape(did)}.md"))
    if len(hits) != 1:
        raise YizhanError(pick(f"找不到定论 {did}", f"Cannot find decision {did}"))
    return hits[0]


def decide(ledger, seat, project, title, body, status="confirmed", refs=(), supersedes=()):
    ledger.require_seat(seat, local=True)
    if ledger.project(project) is None:
        raise YizhanError(pick(f"没有 {project} 这个项目", f"There is no project called {project}"))
    if status not in DECISION_STATUSES:
        raise YizhanError(pick(f"定论状态只能是 {'、'.join(DECISION_STATUSES)}",
                               f"A decision's status must be one of {', '.join(DECISION_STATUSES)}"))
    title, body = " ".join((title or "").split()), (body or "").strip()
    if not title or not body:
        raise YizhanError(pick("定论要有标题和正文", "A decision needs a title and a body"))
    known = {d["id"] for d in decisions(ledger)}
    for old in supersedes:
        if old not in known:
            raise YizhanError(pick(f"要取代的定论 {old} 不存在", f"There is no decision {old} to supersede"))
    now = util.utcnow()
    stem = f"{ledger.local_date(now):%Y%m%d}_{seat}_{util.title_slug(title)}"
    folder = ledger.project_dir(project) / "decisions"
    with ledger.lock():
        if (folder / f"{stem}.md").exists():
            stem = f"{stem}-{secrets.token_hex(2)}"
        meta = {"id": stem, "project": project, "title": title, "by": seat, "status": status,
                "refs": list(refs), "supersedes": list(supersedes), "created": util.iso(now)}
        util.write_text_atomic(folder / f"{stem}.md", util.dump_frontmatter(meta, body))
    return meta


def set_status(ledger, seat, did, status, note=""):
    ledger.require_seat(seat, local=True)
    if status not in DECISION_STATUSES:
        raise YizhanError(pick(f"定论状态只能是 {'、'.join(DECISION_STATUSES)}",
                               f"A decision's status must be one of {', '.join(DECISION_STATUSES)}"))
    path = find_decision(ledger, did)
    with ledger.lock():
        meta, body = util.read_frontmatter(path)
        if meta.get("by") != seat:
            raise YizhanError(pick("只有写这条定论的岗位能改它的状态。有异议请另立一条，用 --supersedes 写明取代哪条",
                                   "Only the seat that wrote this decision can change its status. To disagree, "
                                   "write a new one and name the one it replaces with --supersedes"))
        meta["status"] = status
        meta["updated"] = util.iso(util.utcnow())
        if note:
            meta["status_note"] = note.strip()
        util.write_text_atomic(path, util.dump_frontmatter(meta, body))
    return meta


# ---------- journal ----------

def log(ledger, seat, title, body=""):
    ledger.require_seat(seat, local=True)
    title = " ".join((title or "").split())
    if not title:
        raise YizhanError(pick("日志要有标题", "A journal entry needs a title"))
    local = ledger.local_date()
    path = ledger.seat_dir(seat) / "journal" / f"{local:%Y-%m-%d}.md"
    text = f"### {local:%H:%M} {title}\n\n"
    if body and body.strip():
        text += body.strip() + "\n\n"
    with ledger.lock():
        if not path.exists():
            util.append_text(path, f"# {seat} · {local:%Y-%m-%d}\n\n")
        util.append_text(path, text)
    return path


def journal_entries(ledger, seat, since=None):
    """(time, title) of entries newer than `since`. Entries carry minutes only, so an
    entry in the same minute as `since` counts as new: showing one twice beats missing it."""
    folder = ledger.seat_dir(seat) / "journal"
    if not folder.is_dir():
        return []
    floor = since.replace(second=0) if since else None
    since_day = floor.astimezone(ledger.tz).date() if floor else None
    out = []
    for path in sorted(folder.glob("*.md")):
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if since_day and day < since_day:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in lines:
            m = ENTRY_RE.match(ln)
            if not m:
                continue
            t = dt.datetime(day.year, day.month, day.day, int(m.group(1)), int(m.group(2)), tzinfo=ledger.tz)
            if floor is None or t >= floor:
                out.append((t, m.group(3).strip()))
    return out


# ---------- focus ----------

def set_focus(ledger, seat, text):
    ledger.require_seat(seat, local=True)
    path = ledger.seat_dir(seat) / "profile.json"
    with ledger.lock():
        prof = util.read_json(path, {}) or {}
        prof["focus"] = " ".join((text or "").split())
        prof["focus_at"] = util.iso(util.utcnow())
        util.write_json(path, prof)
    return prof
