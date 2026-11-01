"""Letters between seats.

A letter lives in its sender's outbox, seats/<sender>/outbox/<id>.md, and is never
edited. Each recipient keeps its receipts in seats/<recipient>/receipts/<id>.jsonl.
Both files have a single writer, so letters cross machines through plain git.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path

from . import util
from .i18n import pick
from .util import YizhanError

KINDS = ("request", "reply", "update", "decision")
KIND_ZH = {"request": "请求", "reply": "答复", "update": "进展", "decision": "定论"}
STATUSES = ("received", "working", "done", "deferred")
STATUS_ZH = {"received": "已收到", "working": "处理中", "done": "已处理", "deferred": "暂缓"}
TERMINAL = ("done", "deferred")
ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def kind_label(kind):
    return pick(KIND_ZH.get(kind, kind), kind)


def status_label(status):
    return pick(STATUS_ZH.get(status, status), status)


def new_id(now):
    return f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


def key_id(sender, recipients, key):
    raw = "\0".join([sender, ",".join(recipients), key])
    return "k-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def check_id(mid):
    if not ID_RE.match(mid or ""):
        raise YizhanError(pick(f"信件编号“{mid}”格式不对", f"\"{mid}\" is not a valid letter id"))
    return mid


def find(ledger, mid):
    check_id(mid)
    hits = sorted((ledger.root / "seats").glob(f"*/outbox/{mid}.md"))
    if not hits:
        raise YizhanError(pick(f"找不到编号为 {mid} 的信；对方机器上的信要等同步过来",
                               f"Cannot find letter {mid}; a letter from another machine shows up only after a sync"))
    if len(hits) > 1:
        raise YizhanError(pick(f"编号 {mid} 重复了：{', '.join(str(h) for h in hits)}",
                               f"Letter id {mid} is used more than once: {', '.join(str(h) for h in hits)}"))
    return hits[0]


def read(path):
    return util.read_frontmatter(path)


def all_letters(ledger):
    base = ledger.root / "seats"
    for path in sorted(base.glob("*/outbox/*.md")) if base.is_dir() else []:
        try:
            meta, body = read(path)
        except (OSError, ValueError):
            continue        # half-synced or foreign file: skip it, never crash a session
        if isinstance(meta.get("to"), list) and meta.get("from") and meta.get("id"):
            yield meta, body, path


def expand_recipients(ledger, targets, sender):
    """Addresses, or 'project:<name>' for every seat in that project."""
    out = []
    for t in targets:
        t = t.strip()
        if t.startswith("project:"):
            name = t.split(":", 1)[1]
            if ledger.project(name) is None:
                raise YizhanError(pick(f"没有 {name} 这个项目", f"There is no project called {name}"))
            out += [s["address"] for s in ledger.seats() if name in s.get("projects", [])]
        else:
            util.parse_address(t)
            out.append(t)
    seen, result = set(), []
    for a in out:
        if a != sender and a not in seen:
            seen.add(a)
            result.append(a)
    return result


def send(ledger, sender, to, kind, subject, body, reply_to=None, key=None,
         artifacts=(), wake=None, sealed=False, project=None):
    st = ledger.settings
    ledger.require_seat(sender, local=True)
    if kind not in KINDS:
        raise YizhanError(pick(f"信的类型只能是 {'、'.join(KINDS)}",
                               f"The letter kind must be one of {', '.join(KINDS)}"))
    subject, body = (subject or "").strip(), (body or "").strip()
    if not subject or not body:
        raise YizhanError(pick("主题和正文都不能空", "A letter needs both a subject and a body"))
    limit = int(st["max_body_chars"])
    if len(body) > limit:
        raise YizhanError(pick(f"正文 {len(body)} 字，超过 {limit} 字。正文只写结论、要对方做什么和文件路径，"
                               "细节写进文件，用 --artifact 附上",
                               f"The body has {len(body)} characters, more than the limit of {limit}. A body holds "
                               "only conclusions, what you ask for and file paths; put details in a file and "
                               "attach it with --artifact"))
    original = None
    if reply_to:
        original, _ = read(find(ledger, reply_to))
        if sender not in original.get("to", []):
            raise YizhanError(pick("只有原信的收信方才能回这封信",
                                   "Only a recipient of the original letter can reply to it"))
        to = list(to) or [original["from"]]
        project = project or original.get("project")
    recipients = expand_recipients(ledger, to, sender)
    if not recipients:
        raise YizhanError(pick("没有收信方", "The letter has no recipients"))
    for r in recipients:
        ledger.require_seat(r)
    if project and ledger.project(project) is None:
        raise YizhanError(pick(f"没有 {project} 这个项目", f"There is no project called {project}"))
    arts = []
    for a in artifacts:
        p = Path(a).expanduser()
        if not p.exists():
            raise YizhanError(pick(f"附件不存在：{a}", f"Attachment not found: {a}"))
        arts.append(str(p.resolve()))
    depth = int(original.get("depth", 0)) + 1 if original else 0
    want = kind in st["wake_kinds"] if wake is None else bool(wake)
    now = util.utcnow()
    mid = key_id(sender, recipients, key) if key else new_id(now)
    meta = {
        "id": mid, "from": sender, "to": recipients, "kind": kind, "subject": subject,
        "project": project, "reply_to": reply_to, "depth": depth,
        # Past the round limit nobody is woken any more; the human settles it.
        "wake": want and depth < 2 * int(st["max_rounds"]),
        "sealed": bool(sealed), "artifacts": arts, "created": util.iso(now), "key": key,
    }
    path = ledger.seat_dir(sender) / "outbox" / f"{mid}.md"
    with ledger.lock():
        if path.exists():
            old, old_body = read(path)
            stable = ("from", "to", "kind", "subject", "reply_to", "artifacts")
            if old_body != body or any(old.get(k) != meta[k] for k in stable):
                raise YizhanError(pick("同一个 --key 对应了不同的内容，拒绝覆盖。内容变了就换一个 key",
                                       "This --key was already used for different content, so nothing was "
                                       "overwritten. Use a new key when the content changes"))
            return old
        util.write_text_atomic(path, util.dump_frontmatter(meta, body))
    return meta


# ---------- receipts ----------

def receipt_file(ledger, seat, mid):
    return ledger.seat_dir(seat) / "receipts" / f"{check_id(mid)}.jsonl"


def receipts(ledger, seat, mid):
    out = []
    try:
        with open(receipt_file(ledger, seat, mid), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return out


def latest_receipt(ledger, seat, mid):
    items = receipts(ledger, seat, mid)
    return items[-1] if items else None


def ack(ledger, seat, mid, status, note):
    ledger.require_seat(seat, local=True)
    if status not in STATUSES:
        raise YizhanError(pick(f"回执状态只能是 {'、'.join(STATUSES)}",
                               f"The receipt status must be one of {', '.join(STATUSES)}"))
    if not (note or "").strip():
        raise YizhanError(pick("回执要写一句说明。“收到”不等于同意对方的结论",
                               "A receipt needs a one-line note. \"Received\" does not mean you agree with "
                               "the sender's conclusions"))
    meta, _ = read(find(ledger, mid))
    if seat not in meta.get("to", []):
        raise YizhanError(pick("只有收信方能登记回执", "Only a recipient can record a receipt"))
    rec = {"id": mid, "seat": seat, "status": status, "note": note.strip(), "at": util.iso(util.utcnow())}
    with ledger.lock():
        util.append_text(receipt_file(ledger, seat, mid), json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def inbox(ledger, seat, include_closed=False):
    items = []
    for meta, body, path in all_letters(ledger):
        if seat not in meta["to"]:
            continue
        rec = latest_receipt(ledger, seat, meta["id"])
        if rec and rec.get("status") in TERMINAL and not include_closed:
            continue
        items.append({"meta": meta, "chars": len(body), "receipt": rec, "path": path})
    items.sort(key=lambda x: (x["meta"].get("created", ""), x["meta"]["id"]))
    return items


# ---------- presence: when a seat was last active, when its turn last ended ----------

def touch(ledger, seat, seal=False):
    path = ledger.seat_dir(seat) / "presence.json"
    with ledger.lock():
        data = util.read_json(path, {}) or {}
        now = util.iso(util.utcnow())
        data["last_active"] = now
        if seal:
            data["sealed_at"] = now
        util.write_json(path, data)
    return data


def is_sealed(ledger, meta):
    """A letter is final once its sender's turn has ended (the Stop event)."""
    if meta.get("sealed"):
        return True
    try:
        pres = util.read_json(ledger.seat_dir(meta["from"]) / "presence.json", {}) or {}
    except (ValueError, YizhanError):
        return False
    sealed_at, created = util.parse_iso(pres.get("sealed_at")), util.parse_iso(meta.get("created"))
    return bool(sealed_at and created and sealed_at >= created)


# ---------- display ----------

def line(ledger, item):
    m, rec = item["meta"], item.get("receipt")
    chars = item.get("chars", 0)
    tags = [pick(f"{chars} 字", f"{chars} chars")]
    if m.get("reply_to"):
        tags.append(pick("回信", "reply"))
    if m.get("artifacts"):
        n = len(m["artifacts"])
        tags.append(pick(f"附件 {n}", f"{n} attachment" + ("s" if n > 1 else "")))
    if rec:
        tags.append(status_label(rec.get("status")))
    if m.get("wake"):
        tags.append(pick("待办", "needs action"))
    if int(m.get("depth", 0)) >= 2 * int(ledger.settings["max_rounds"]):
        tags.append(pick("已过往返上限，分歧请交用户裁决", "round limit reached: the user decides"))
    head = f"{m['id']} [{kind_label(m.get('kind'))}] {ledger.fmt_time(m.get('created'))} {m['from']}"
    return pick(f"{head}：{m['subject']}（{'，'.join(tags)}）", f"{head}: {m['subject']} ({', '.join(tags)})")


def show_text(ledger, meta, body):
    first = (f"{meta['id']} [{kind_label(meta.get('kind'))}] {meta['from']} → "
             + pick("、", ", ").join(meta["to"]) + f"  {ledger.fmt_time(meta.get('created'))}")
    if meta.get("reply_to"):
        first += pick(f"，回复 {meta['reply_to']}", f", replying to {meta['reply_to']}")
    head = [first, pick("主题：", "Subject: ") + meta["subject"]]
    if meta.get("project"):
        head.append(pick("项目：", "Project: ") + meta["project"])
    if meta.get("artifacts"):
        machine = meta["from"].split("@", 1)[-1]
        head.append(pick(f"附件（路径在 {machine} 上）：", f"Attachments (paths on {machine}): ")
                    + pick("；", "; ").join(meta["artifacts"]))
    for r in meta["to"]:
        rec = latest_receipt(ledger, r, meta["id"])
        if rec:
            when, note = ledger.fmt_time(rec.get("at")), rec.get("note", "")
            head.append(pick(f"回执：{r} {status_label(rec['status'])}（{when}）{note}",
                             f"Receipt: {r} {status_label(rec['status'])} ({when}) {note}"))
    return "\n".join(head) + "\n----\n" + body
