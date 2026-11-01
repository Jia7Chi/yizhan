"""`yizhan board`: the whole ledger on one page.

One HTML file with no outside resources, so it opens offline and straight from disk:
which machines and seats there are, what each seat is working on, the letters
between seats with their receipts, decisions and recent journal entries. It goes to
this machine's state folder, never into the ledger, and is rebuilt on every run.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import string
from collections import Counter, defaultdict
from pathlib import Path

from . import __version__, adapters, mail, records, state, util
from .i18n import lang, pick

PLATFORM = {"darwin": "macOS", "linux": "Linux", "win32": "Windows"}
KIND_CLASS = {"request": "req", "reply": "rep", "update": "upd", "decision": "dec"}
RECEIPT_ICON = {"done": "✓", "working": "◔", "received": "•", "deferred": "⏸"}
MAX_THREADS = 40
JOURNAL_DAYS = 14


def esc(text):
    return html.escape(str(text if text is not None else ""), quote=True)


def _ago(led, ts, fmt="%m-%d %H:%M"):
    """A time that the page's script turns into "3 min ago"; the absolute time is the
    fallback text and the tooltip."""
    t = util.parse_iso(ts)
    if not t:
        return f'<span class="ago">{esc(pick("还没有动静", "never"))}</span>'
    return (f'<span class="ago" data-ts="{esc(util.iso(t))}" title="{esc(led.fmt_time(ts, "%Y-%m-%d %H:%M"))}">'
            f'{esc(led.fmt_time(ts, fmt))}</span>')


# ---------- gathering ----------

def collect(led, now=None):
    now = now or util.utcnow()
    seats = led.seats()
    machines = list(led.machines())
    names = [m["machine"] for m in machines]
    for s in seats:
        if s["machine"] not in names:
            names.append(s["machine"])
            machines.append({"machine": s["machine"]})
    machines.sort(key=lambda m: (m["machine"] != led.machine, m["machine"]))   # this machine first
    letters = []
    for meta, body, _ in mail.all_letters(led):
        receipts = {r: mail.latest_receipt(led, r, meta["id"]) for r in meta["to"]}
        letters.append({"meta": meta, "body": body, "receipts": receipts})
    open_for = Counter()
    pairs, directed = Counter(), Counter()
    for item in letters:
        sender = item["meta"]["from"]
        for r, rec in item["receipts"].items():
            if not rec or rec.get("status") not in mail.TERMINAL:
                open_for[r] += 1
            pairs[tuple(sorted((sender, r)))] += 1
            directed[(sender, r)] += 1
    links = [{"a": a, "b": b, "n": n, "ab": directed[(a, b)], "ba": directed[(b, a)]}
             for (a, b), n in sorted(pairs.items())]
    decisions = []
    for d in records.decisions(led):
        try:
            _, body = util.read_frontmatter(records.find_decision(led, d["id"]))
        except (OSError, ValueError, util.YizhanError):
            body = ""
        decisions.append((d, body))
    since = now - dt.timedelta(days=JOURNAL_DAYS)
    journal = sorted(((t, s["address"], title) for s in seats
                      for t, title in records.journal_entries(led, s["address"], since=since)),
                     key=lambda x: x[0], reverse=True)
    return {"now": now, "seats": seats, "machines": machines, "projects": led.projects(),
            "letters": letters, "open_for": open_for, "links": links, "decisions": decisions,
            "journal": journal, "sync": state.load(led, "sync")}


def threads(letters):
    """Letters grouped by conversation, the most recently active conversation first.
    Inside one, a letter always comes after the one it answers: times have only
    seconds and ids are random, so neither alone says which came first."""
    by_id = {x["meta"]["id"]: x for x in letters}

    def when(item):
        return (item["meta"].get("created", ""), item["meta"]["id"])

    def root(item):
        seen = set()
        while item["meta"].get("reply_to") in by_id and item["meta"]["id"] not in seen:
            seen.add(item["meta"]["id"])
            item = by_id[item["meta"]["reply_to"]]
        return item["meta"]["id"]
    groups = defaultdict(list)
    for item in letters:
        groups[root(item)].append(item)
    out = []
    for rid, items in groups.items():
        answers = defaultdict(list)
        for item in items:
            if item["meta"]["id"] != rid:
                answers[item["meta"].get("reply_to")].append(item)
        ordered, seen, stack = [], set(), [by_id[rid]]
        while stack:                          # depth first: each answer right after its letter
            item = stack.pop()
            if item["meta"]["id"] in seen:
                continue
            seen.add(item["meta"]["id"])
            ordered.append(item)
            stack.extend(sorted(answers.get(item["meta"]["id"], []), key=when, reverse=True))
        ordered += sorted((x for x in items if x["meta"]["id"] not in seen), key=when)
        out.append(ordered)
    return sorted(out, key=lambda g: max(when(x) for x in g), reverse=True)


# ---------- pieces ----------

def _projects_attr(names):
    return esc(" ".join(names))


def seat_card(led, s, open_count):
    agent = s.get("agent", "")
    display = adapters.ADAPTERS[agent].display if agent in adapters.ADAPTERS else agent
    pres = s.get("presence") or {}
    rows = []
    if s.get("duty"):
        rows.append(f'<p class="duty">{esc(s["duty"])}</p>')
    if s.get("focus"):
        rows.append(f'<p class="focus"><span class="label">{esc(pick("在做", "Working on"))}</span>'
                    f'{esc(s["focus"])}</p>')
    if s.get("owns"):
        rows.append('<p class="owns">' + " ".join(f"<code>{esc(o)}</code>" for o in s["owns"]) + "</p>")
    pills = []
    if open_count:
        pills.append(f'<span class="pill open">{esc(pick(f"待处理 {open_count}", f"{open_count} open"))}</span>')
    if s.get("autowake"):
        pills.append(f'<span class="pill wake">⚡ {esc(pick("自动叫醒", "autowake"))}</span>')
    for p in s.get("projects", []):
        pills.append(f'<span class="pill proj">{esc(p)}</span>')
    return f"""
      <article class="seat {esc(agent)}" data-addr="{esc(s['address'])}" data-projects="{_projects_attr(s.get('projects', []))}">
        <header>
          <span class="dot" data-ts="{esc(pres.get('last_active') or '')}"></span>
          <div class="who"><div class="addr">{esc(s['address'])}</div>
            <div class="sub"><span class="agent">{esc(display)}</span>{(' · ' + esc(s['title'])) if s.get('title') else ''}</div></div>
        </header>
        {''.join(rows)}
        <footer>{''.join(pills)}<span class="seen">{_ago(led, pres.get('last_active'))}</span></footer>
      </article>"""


def machine_column(led, m, seats, open_for):
    mine = [s for s in seats if s["machine"] == m["machine"]]
    here = m["machine"] == led.machine
    plat = PLATFORM.get(m.get("platform", ""), m.get("platform", ""))
    host = m.get("hostname", "")
    cards = "".join(seat_card(led, s, open_for.get(s["address"], 0)) for s in mine) or \
        f'<p class="empty">{esc(pick("这台机器还没有岗位", "No seats on this machine yet"))}</p>'
    tag = f'<span class="here">{esc(pick("本机", "this machine"))}</span>' if here else ""
    return f"""
    <section class="machine">
      <h3><span class="mname">{esc(m['machine'])}</span>{tag}<span class="plat">{esc(plat)}{(' · ' + esc(host)) if host else ''}</span></h3>
      {cards}
    </section>"""


def _receipts(led, item):
    out = []
    for r, rec in item["receipts"].items():
        status = (rec or {}).get("status", "waiting")
        icon = RECEIPT_ICON.get(status, "○")
        label = mail.status_label(status) if rec else pick("未回执", "no receipt yet")
        note = (rec or {}).get("note", "")
        tip = f"{label} · {led.fmt_time(rec.get('at'))}" if rec else label
        out.append(f'<span class="rc {esc(status)}" title="{esc(tip)}">{icon} {esc(r)}'
                   + f'<span class="note">{esc(note or label)}</span></span>')
    return "".join(out)


def letter_block(led, item, first):
    m = item["meta"]
    kind = m.get("kind", "update")
    tags = []
    still_open = any(not rec or rec.get("status") not in mail.TERMINAL for rec in item["receipts"].values())
    if m.get("wake") and still_open:
        tags.append(f'<span class="tag act">{esc(pick("待办", "needs action"))}</span>')
    if m.get("artifacts"):
        tags.append(f'<span class="tag">{esc(pick("附件", "attachments"))} {len(m["artifacts"])}</span>')
    body = item["body"].strip()
    details = (f'<details><summary>{esc(pick("正文", "Body"))} · {len(body)}{esc(pick(" 字", " chars"))}</summary>'
               f'<div class="body">{esc(body)}</div></details>') if body else ""
    return f"""
        <div class="letter{'' if first else ' reply'}">
          <div class="lhead"><span class="kind {KIND_CLASS.get(kind, 'upd')}">{esc(mail.kind_label(kind))}</span>
            <span class="route"><b>{esc(m['from'])}</b> → {esc(pick('、', ', ').join(m['to']))}</span>
            {''.join(tags)}<span class="when">{_ago(led, m.get('created'))}</span></div>
          <div class="subject">{esc(m.get('subject', ''))}</div>
          <div class="receipts">{_receipts(led, item)}</div>
          {details}
        </div>"""


def thread_block(led, group, seats_by_addr):
    root = group[0]["meta"]
    projects = [root["project"]] if root.get("project") else \
        (seats_by_addr.get(root["from"]) or {}).get("projects", [])
    inner = "".join(letter_block(led, item, i == 0) for i, item in enumerate(group))
    return f'<article class="thread" data-projects="{_projects_attr(projects)}">{inner}</article>'


def decision_block(led, d, body, titles):
    status = d.get("status", "confirmed")
    newer = d.get("superseded_by")
    cls = "superseded" if newer else status
    label = pick("已被取代", "superseded") if newer else records.decision_label(status)
    replaced = (f'<div class="by">{esc(pick("改为：", "replaced by: "))}'
                f'<span title="{esc(newer)}">{esc(titles.get(newer, newer))}</span></div>') if newer else ""
    details = (f'<details><summary>{esc(pick("依据", "Reasoning"))}</summary><div class="body">{esc(body.strip())}</div>'
               + ("".join(f"<code>{esc(r)}</code>" for r in d.get("refs", [])) if d.get("refs") else "")
               + "</details>") if body.strip() else ""
    return f"""
      <article class="decision {esc(cls)}" data-projects="{esc(d.get('project', ''))}">
        <div class="dhead"><span class="dstatus">{esc(label)}</span>
          <span class="pill proj">{esc(d.get('project', ''))}</span><span class="when">{_ago(led, d.get('created'), '%m-%d')}</span></div>
        <div class="dtitle">{esc(d.get('title', ''))}</div>
        <div class="by">{esc(d.get('by', ''))}</div>
        {replaced}
        {details}
      </article>"""


def journal_block(led, entries, seats_by_addr):
    if not entries:
        return f'<p class="empty">{esc(pick(f"最近 {JOURNAL_DAYS} 天没有日志", f"No journal entries in the last {JOURNAL_DAYS} days"))}</p>'
    out, day = [], None
    for t, seat, title in entries[:80]:
        d = t.astimezone(led.tz).strftime("%m-%d")
        if d != day:
            out.append(f'<div class="jday">{esc(d)}</div>')
            day = d
        projects = (seats_by_addr.get(seat) or {}).get("projects", [])
        agent = (seats_by_addr.get(seat) or {}).get("agent", "")
        out.append(f'<div class="jrow" data-projects="{_projects_attr(projects)}"><span class="jtime">{t.astimezone(led.tz):%H:%M}</span>'
                   f'<span class="jseat {esc(agent)}">{esc(seat)}</span><span class="jtitle">{esc(title)}</span></div>')
    return "".join(out)


# ---------- the page ----------

def render(led, data=None):
    data = data or collect(led)
    seats, now = data["seats"], data["now"]
    by_addr = {s["address"]: s for s in seats}
    groups = threads(data["letters"])
    open_letters = sum(data["open_for"].values())
    st = data["sync"]
    stats = [
        pick(f"{len(data['machines'])} 台机器", f"{len(data['machines'])} machines"),
        pick(f"{len(seats)} 个岗位", f"{len(seats)} seats"),
        pick(f"{len(data['letters'])} 封信", f"{len(data['letters'])} letters"),
        pick(f"{open_letters} 封待处理", f"{open_letters} open"),
        pick(f"{len(data['decisions'])} 条定论", f"{len(data['decisions'])} decisions"),
    ]
    chips = "".join(f'<button class="chip" data-filter="{esc(p["name"])}" title="{esc(p.get("summary", ""))}">'
                    f'{esc(p.get("title") or p["name"])}</button>' for p in data["projects"])
    sync_line = ""
    if st.get("last_run"):
        sync_line = pick(f"本机最近同步 {led.fmt_time(st['last_run'])}", f"last sync here {led.fmt_time(st['last_run'])}")
        if st.get("last_error"):
            sync_line += pick("（出错了，运行 yizhan doctor 看看）", " (it failed; run yizhan doctor)")
    columns = "".join(machine_column(led, m, seats, data["open_for"]) for m in data["machines"])
    letters_html = "".join(thread_block(led, g, by_addr) for g in groups[:MAX_THREADS]) or \
        f'<p class="empty">{esc(pick("还没有信。让一个 AI 用 yizhan send 给同事写一封试试。", "No letters yet. Have an AI write to a colleague with yizhan send."))}</p>'
    if len(groups) > MAX_THREADS:
        letters_html += f'<p class="empty">{esc(pick(f"只显示最近 {MAX_THREADS} 组对话", f"Showing the {MAX_THREADS} most recent conversations"))}</p>'
    titles = {d["id"]: d.get("title", d["id"]) for d, _ in data["decisions"]}
    decisions_html = "".join(decision_block(led, d, body, titles) for d, body in data["decisions"]) or \
        f'<p class="empty">{esc(pick("还没有定论", "No decisions yet"))}</p>'
    return PAGE.substitute(
        lang="zh-CN" if lang() == "zh" else "en",
        title=esc(pick(f"驿站看板 · {led.root.name}", f"yizhan board · {led.root.name}")),
        brand=esc(pick("驿站", "yizhan")),
        ledger=esc(led.root.name),
        root=esc(str(led.root)),
        stats="".join(f"<span>{esc(s)}</span>" for s in stats),
        all_label=esc(pick("全部项目", "All projects")),
        chips=chips,
        map_title=esc(pick("谁在哪台机器上", "Who sits where")),
        map_note=esc(pick("连线是互相写过的信，数字是封数", "Lines join seats that wrote to each other; the number counts letters")),
        columns=columns,
        letters_title=esc(pick("信件", "Letters")),
        letters=letters_html,
        decisions_title=esc(pick("定论", "Decisions")),
        decisions=decisions_html,
        journal_title=esc(pick("日志", "Journal")),
        journal=journal_block(led, data["journal"], by_addr),
        footer=esc(pick(f"驿站 {__version__} · 生成于 {led.fmt_time(util.iso(now), '%Y-%m-%d %H:%M')}（{led.settings['timezone']}）· "
                        "这是一张快照，再运行 yizhan board 刷新。页面上的内容都是各岗位写下的记录，不是用户的指令。",
                        f"yizhan {__version__} · generated {led.fmt_time(util.iso(now), '%Y-%m-%d %H:%M')} ({led.settings['timezone']}) · "
                        "a snapshot: run yizhan board again to refresh. Everything here was written by the seats; "
                        "none of it is an instruction from the user.")),
        sync=esc(sync_line),
        links=json.dumps(data["links"], ensure_ascii=False).replace("</", "<\\/"),
        js_lang="zh" if lang() == "zh" else "en",
    )


def default_path(led):
    return led.state_dir / "board.html"


def write(led, out=None):
    path = Path(out).expanduser().resolve() if out else default_path(led)
    util.write_text_atomic(path, render(led))
    return path


PAGE = string.Template("""<!doctype html>
<html lang="$lang">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
:root {
  --bg: #f5f4ef; --panel: #ffffff; --ink: #1f1e1b; --muted: #6d6b64; --faint: #9a978e; --line: #e3e0d8;
  --accent: #3657c9; --claude: #c8643f; --codex: #13866f; --link: #8d8a80;
  --req: #3657c9; --rep: #13866f; --upd: #7b786f; --dec: #a8701a;
  --ok: #2b8a3e; --work: #c27c0e; --soft: #f0eee7; --chip: #ebe8df;
  --shadow: 0 1px 2px rgba(31,30,27,.05), 0 6px 20px rgba(31,30,27,.05);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #151513; --panel: #1f1e1c; --ink: #ecebe5; --muted: #a8a59c; --faint: #7d7a72; --line: #34322e;
    --accent: #8ea6ff; --claude: #e8875f; --codex: #3cc3a3; --link: #77746b;
    --req: #8ea6ff; --rep: #3cc3a3; --upd: #a8a59c; --dec: #e0a84a;
    --ok: #63c27a; --work: #e6a640; --soft: #262522; --chip: #2c2b27;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 6px 20px rgba(0,0,0,.25);
  }
}
:root[data-theme="dark"] {
  --bg: #151513; --panel: #1f1e1c; --ink: #ecebe5; --muted: #a8a59c; --faint: #7d7a72; --line: #34322e;
  --accent: #8ea6ff; --claude: #e8875f; --codex: #3cc3a3; --link: #77746b;
  --req: #8ea6ff; --rep: #3cc3a3; --upd: #a8a59c; --dec: #e0a84a;
  --ok: #63c27a; --work: #e6a640; --soft: #262522; --chip: #2c2b27;
  --shadow: 0 1px 2px rgba(0,0,0,.3), 0 6px 20px rgba(0,0,0,.25);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif; }
code, .body { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Noto Sans Mono CJK SC", monospace; }
.wrap { max-width: 1240px; margin: 0 auto; padding: 28px 16px 48px; }
.top { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 18px; }
.top h1 { margin: 0; font-size: 24px; letter-spacing: -.01em; }
.top h1 .brand { color: var(--accent); }
.top .root { color: var(--faint); font-size: 12px; word-break: break-all; }
.stats { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 0; }
.stats span { background: var(--chip); border-radius: 999px; padding: 2px 10px; font-size: 12.5px; color: var(--muted); }
.stats .sync { background: transparent; padding-left: 4px; }
.filters { display: flex; flex-wrap: wrap; gap: 6px; margin: 18px 0 6px; }
.chip { border: 1px solid var(--line); background: var(--panel); color: var(--ink); border-radius: 999px;
  padding: 4px 12px; font: inherit; font-size: 13px; cursor: pointer; }
.chip.on { background: var(--ink); color: var(--bg); border-color: var(--ink); }
h2 { font-size: 15px; margin: 30px 0 4px; letter-spacing: .02em; }
.hint { color: var(--faint); font-size: 12.5px; margin: 0 0 12px; }
.map { position: relative; display: flex; flex-wrap: wrap; align-items: flex-start; gap: 28px 88px; padding: 4px 0 8px; }
.links { position: absolute; inset: 0; pointer-events: none; overflow: visible; z-index: 0; }
.links path { fill: none; stroke: var(--link); stroke-opacity: .55; stroke-linecap: round; transition: stroke .15s, stroke-opacity .15s; }
.links path.hot { stroke: var(--accent); stroke-opacity: 1; }
.links circle { fill: var(--panel); stroke: var(--link); stroke-opacity: .6; }
.links circle.hot { stroke: var(--accent); stroke-opacity: 1; }
.links text { fill: var(--muted); font-size: 11px; font-weight: 600; text-anchor: middle; dominant-baseline: central; }
.machine { position: relative; z-index: 1; flex: 0 1 290px; min-width: 220px; display: flex; flex-direction: column; gap: 12px; }
.machine h3 { margin: 0; font-size: 13px; display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; }
.mname { font-size: 17px; letter-spacing: -.01em; }
.here { font-size: 11px; color: var(--accent); border: 1px solid var(--accent); border-radius: 999px; padding: 0 7px; }
.plat { color: var(--faint); font-weight: 400; font-size: 12px; }
.seat { background: var(--panel); border-radius: 12px; box-shadow: var(--shadow); padding: 12px 14px 10px;
  border-left: 4px solid var(--link); }
.seat.claude { border-left-color: var(--claude); }
.seat.codex { border-left-color: var(--codex); }
.seat header { display: flex; gap: 10px; align-items: flex-start; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--line); margin-top: 6px; flex: none; }
.dot.live { background: var(--ok); box-shadow: 0 0 0 4px color-mix(in srgb, var(--ok) 22%, transparent); }
.dot.recent { background: var(--work); }
.addr { font-weight: 650; font-size: 14.5px; }
.sub { color: var(--muted); font-size: 12.5px; }
.seat.claude .agent { color: var(--claude); font-weight: 600; }
.seat.codex .agent { color: var(--codex); font-weight: 600; }
.duty { margin: 8px 0 0; color: var(--ink); }
.focus { margin: 6px 0 0; background: var(--soft); border-radius: 8px; padding: 6px 9px; }
.label { display: block; font-size: 11px; color: var(--faint); text-transform: uppercase; letter-spacing: .04em; }
.owns { margin: 6px 0 0; display: flex; flex-wrap: wrap; gap: 4px; }
.owns code { font-size: 11.5px; background: var(--soft); border-radius: 5px; padding: 1px 6px; color: var(--muted); }
.seat footer { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; margin-top: 9px; }
.pill { font-size: 11.5px; border-radius: 999px; padding: 1px 8px; background: var(--chip); color: var(--muted); }
.pill.open { background: color-mix(in srgb, var(--req) 14%, transparent); color: var(--req); font-weight: 600; }
.pill.wake { background: color-mix(in srgb, var(--work) 16%, transparent); color: var(--work); }
.seen { margin-left: auto; font-size: 11.5px; color: var(--faint); }
.cols { display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(0, 1fr); gap: 0 32px; }
.thread, .decision { background: var(--panel); border-radius: 12px; box-shadow: var(--shadow); padding: 4px 14px; margin-bottom: 12px; }
.letter { padding: 10px 0; }
.letter.reply { border-top: 1px dashed var(--line); margin-left: 14px; padding-left: 12px; border-left: 2px solid var(--line); }
.lhead, .dhead { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; font-size: 12.5px; color: var(--muted); }
.kind { font-size: 11.5px; font-weight: 650; border-radius: 6px; padding: 0 7px; color: #fff; }
.kind.req { background: var(--req); } .kind.rep { background: var(--rep); } .kind.upd { background: var(--upd); } .kind.dec { background: var(--dec); }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) .kind { color: #151513; } }
:root[data-theme="dark"] .kind { color: #151513; }
.route b { color: var(--ink); font-weight: 600; }
.when { margin-left: auto; color: var(--faint); }
.tag { font-size: 11px; border: 1px solid var(--line); border-radius: 6px; padding: 0 6px; color: var(--muted); }
.tag.act { border-color: var(--req); color: var(--req); }
.subject { font-weight: 620; font-size: 14.5px; margin: 4px 0 2px; }
.receipts { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 5px; }
.rc { font-size: 12px; border-radius: 8px; padding: 2px 8px; background: var(--soft); color: var(--muted); }
.rc.done { color: var(--ok); } .rc.working { color: var(--work); }
.rc .note { color: var(--muted); margin-left: 6px; }
details { margin-top: 6px; }
summary { cursor: pointer; color: var(--faint); font-size: 12px; }
.body { white-space: pre-wrap; font-size: 12.5px; background: var(--soft); border-radius: 8px; padding: 8px 10px; margin-top: 6px; overflow-wrap: anywhere; }
.decision { padding: 10px 14px; }
.dstatus { font-weight: 650; color: var(--ok); }
.decision.pending .dstatus { color: var(--work); }
.decision.superseded .dstatus, .decision.retracted .dstatus { color: var(--faint); }
.decision.retracted, .decision.superseded { opacity: .62; }
.decision.retracted .dtitle, .decision.superseded .dtitle { text-decoration: line-through; }
.dtitle { font-weight: 620; font-size: 14.5px; margin-top: 3px; }
.by { color: var(--faint); font-size: 12px; }
.decision code { display: inline-block; margin: 6px 6px 0 0; font-size: 11.5px; color: var(--muted); }
.journal { background: var(--panel); border-radius: 12px; box-shadow: var(--shadow); padding: 6px 14px 10px; }
.jday { font-size: 11.5px; color: var(--faint); margin: 10px 0 2px; font-weight: 600; }
.jrow { display: grid; grid-template-columns: 44px auto 1fr; gap: 8px; padding: 3px 0; font-size: 13px; align-items: baseline; }
.jtime { color: var(--faint); font-variant-numeric: tabular-nums; }
.jseat { font-size: 12px; font-weight: 600; color: var(--muted); }
.jseat.claude { color: var(--claude); } .jseat.codex { color: var(--codex); }
.empty { color: var(--faint); font-size: 13px; }
.foot { margin-top: 40px; color: var(--faint); font-size: 12px; }
[hidden] { display: none !important; }
@media (max-width: 860px) {
  .cols { grid-template-columns: 1fr; }
  .map { gap: 20px; }
  .machine { flex-basis: 100%; }
  .links { display: none; }
}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1><span class="brand">$brand</span> · $ledger</h1>
    <span class="root">$root</span>
  </div>
  <div class="stats">$stats<span class="sync">$sync</span></div>
  <div class="filters"><button class="chip on" data-filter="">$all_label</button>$chips</div>

  <h2>$map_title</h2>
  <p class="hint">$map_note</p>
  <div class="map"><svg class="links" aria-hidden="true"></svg>$columns</div>

  <div class="cols">
    <div>
      <h2>$letters_title</h2>
      $letters
    </div>
    <div>
      <h2>$decisions_title</h2>
      $decisions
      <h2>$journal_title</h2>
      <div class="journal">$journal</div>
    </div>
  </div>
  <p class="foot">$footer</p>
</div>
<script type="application/json" id="links-data">$links</script>
<script>
(function () {
  var LANG = "$js_lang";
  var NS = "http://www.w3.org/2000/svg";
  function ago(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return LANG === "zh" ? "刚刚" : "just now";
    if (s < 3600) return LANG === "zh" ? Math.floor(s / 60) + " 分钟前" : Math.floor(s / 60) + " min ago";
    if (s < 86400) return LANG === "zh" ? Math.floor(s / 3600) + " 小时前" : Math.floor(s / 3600) + " h ago";
    return LANG === "zh" ? Math.floor(s / 86400) + " 天前" : Math.floor(s / 86400) + " d ago";
  }
  var now = Date.now();
  document.querySelectorAll(".ago[data-ts]").forEach(function (el) {
    var t = Date.parse(el.getAttribute("data-ts"));
    if (!isNaN(t)) el.textContent = ago(now - t);
  });
  document.querySelectorAll(".dot[data-ts]").forEach(function (el) {
    var t = Date.parse(el.getAttribute("data-ts"));
    if (isNaN(t)) return;
    if (now - t < 15 * 60 * 1000) el.classList.add("live");
    else if (now - t < 24 * 3600 * 1000) el.classList.add("recent");
  });

  var links = JSON.parse(document.getElementById("links-data").textContent || "[]");
  var map = document.querySelector(".map"), svg = document.querySelector(".links");
  function card(addr) {
    var el = map.querySelector('.seat[data-addr="' + addr + '"]');
    return el && el.offsetParent !== null ? el : null;
  }
  function make(tag, attrs) {
    var el = document.createElementNS(NS, tag);
    for (var k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }
  function rounded(pts, r) {
    // A path through the points with rounded corners.
    var d = "M" + pts[0][0] + "," + pts[0][1];
    for (var i = 1; i < pts.length - 1; i++) {
      var p = pts[i], a = pts[i - 1], b = pts[i + 1];
      var da = Math.hypot(p[0] - a[0], p[1] - a[1]) || 1, db = Math.hypot(b[0] - p[0], b[1] - p[1]) || 1;
      var ra = Math.min(r, da / 2), rb = Math.min(r, db / 2);
      d += " L" + (p[0] - (p[0] - a[0]) / da * ra) + "," + (p[1] - (p[1] - a[1]) / da * ra)
        + " Q" + p[0] + "," + p[1] + " " + (p[0] + (b[0] - p[0]) / db * rb) + "," + (p[1] + (b[1] - p[1]) / db * rb);
    }
    var last = pts[pts.length - 1];
    return d + " L" + last[0] + "," + last[1];
  }
  function draw() {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    map.style.paddingBottom = "";
    if (getComputedStyle(svg).display === "none") return;
    var box = map.getBoundingClientRect();
    function rel(el) {
      var r = el.getBoundingClientRect();
      return {l: r.left - box.left, r: r.right - box.left, t: r.top - box.top, b: r.bottom - box.top};
    }
    // Columns side by side form a row; a narrow window may wrap them into several rows.
    var info = new Map(), rows = {};
    [].slice.call(map.querySelectorAll(".machine")).forEach(function (c) {
      if (c.offsetParent === null) return;
      var r = rel(c), bottom = r.t;
      [].slice.call(c.querySelectorAll(".seat")).forEach(function (s) {
        if (s.offsetParent !== null) bottom = Math.max(bottom, rel(s).b);
      });
      var row = Math.round(r.t);
      info.set(c, {row: row, l: r.l, bottom: bottom});
      (rows[row] = rows[row] || []).push(c);
    });
    Object.keys(rows).forEach(function (k) {
      rows[k].sort(function (x, y) { return info.get(x).l - info.get(y).l; });
      rows[k].forEach(function (c, i) { info.get(c).index = i; });
    });
    var segs = [];
    links.forEach(function (l) {
      var a = card(l.a), b = card(l.b);
      if (!a || !b) return;
      var ia = info.get(a.closest(".machine")), ib = info.get(b.closest(".machine"));
      if (!ia || !ib || ia.row !== ib.row) return;          // columns on different rows: no line
      if (ia.index > ib.index) { var t = a; a = b; b = t; t = ia; ia = ib; ib = t; }
      var step = ib.index - ia.index;
      segs.push({l: l, a: a, b: b, ia: ia, ib: ib, ra: rel(a), rb: rel(b),
                 kind: step === 0 ? "same" : step === 1 ? "next" : "far"});
    });
    // Several lines meeting one side of a card get their own spots along it.
    var ends = {};
    function claim(s, end, side, toward) {
      var key = s[end].getAttribute("data-addr") + "|" + side;
      (ends[key] = ends[key] || []).push({s: s, end: end, toward: toward});
    }
    segs.forEach(function (s) {
      var ya = (s.ra.t + s.ra.b) / 2, yb = (s.rb.t + s.rb.b) / 2;
      claim(s, "a", "right", s.kind === "far" ? 1e9 : yb);
      claim(s, "b", s.kind === "same" ? "right" : "left", s.kind === "far" ? 1e9 : ya);
    });
    Object.keys(ends).forEach(function (key) {
      var list = ends[key].sort(function (x, y) { return x.toward - y.toward; });
      list.forEach(function (e, i) {
        var r = e.end === "a" ? e.s.ra : e.s.rb;
        e.s[e.end + "y"] = r.t + (r.b - r.t) * (0.3 + 0.4 * (i + 1) / (list.length + 1));
      });
    });
    var lanes = 0, need = 0;
    segs.forEach(function (s) {
      var l = s.l, ax = s.ra.r, ay = s.ay, by = s.by, bx, d, mx, my;
      if (s.kind === "same") {
        bx = s.rb.r;
        var bulge = 30 + Math.abs(ay - by) * 0.18;
        d = "M" + ax + "," + ay + " C" + (ax + bulge) + "," + ay + " " + (bx + bulge) + "," + by + " " + bx + "," + by;
        mx = (ax + bx) / 2 + bulge * 0.75; my = (ay + by) / 2;
      } else if (s.kind === "next") {
        bx = s.rb.l;
        var dx = (bx - ax) / 2;
        d = "M" + ax + "," + ay + " C" + (ax + dx) + "," + ay + " " + (bx - dx) + "," + by + " " + bx + "," + by;
        mx = (ax + bx) / 2; my = (ay + by) / 2;
      } else {
        // Seats with a machine between them: go down the gaps and under that machine's cards.
        bx = s.rb.l;
        var low = 0;
        info.forEach(function (i) {
          if (i.row === s.ia.row && i.index > s.ia.index && i.index < s.ib.index) low = Math.max(low, i.bottom);
        });
        var lane = low + 28 + lanes * 16, gx1 = ax + 26 + lanes * 8, gx2 = bx - 26 - lanes * 8;
        lanes++;
        d = rounded([[ax, ay], [gx1, ay], [gx1, lane], [gx2, lane], [gx2, by], [bx, by]], 14);
        mx = (gx1 + gx2) / 2; my = lane;
        need = Math.max(need, lane + 18);
      }
      var w = 1.4 + Math.log(l.n) / Math.LN2 * 1.1;
      var g = make("g", {"data-a": l.a, "data-b": l.b});
      var tip = make("title", {});
      tip.textContent = l.a + " → " + l.b + ": " + l.ab + "    " + l.b + " → " + l.a + ": " + l.ba;
      g.appendChild(tip);
      g.appendChild(make("path", {d: d, "stroke-width": w.toFixed(2)}));
      g.appendChild(make("circle", {cx: mx, cy: my, r: 10.5}));
      var label = make("text", {x: mx, y: my});
      label.textContent = String(l.n);
      g.appendChild(label);
      svg.appendChild(g);
    });
    var height = map.getBoundingClientRect().height;
    if (need > height - 8) map.style.paddingBottom = Math.ceil(need - height + 16) + "px";
  }
  function hot(addr, on) {
    svg.querySelectorAll("g").forEach(function (g) {
      var hit = g.getAttribute("data-a") === addr || g.getAttribute("data-b") === addr;
      g.querySelectorAll("path, circle").forEach(function (el) { el.classList.toggle("hot", on && hit); });
    });
  }
  map.querySelectorAll(".seat").forEach(function (el) {
    el.addEventListener("mouseenter", function () { hot(el.getAttribute("data-addr"), true); });
    el.addEventListener("mouseleave", function () { hot(el.getAttribute("data-addr"), false); });
  });
  document.querySelectorAll(".chip").forEach(function (chip) {
    chip.addEventListener("click", function () {
      var f = chip.getAttribute("data-filter");
      document.querySelectorAll(".chip").forEach(function (c) { c.classList.toggle("on", c === chip); });
      document.querySelectorAll("[data-projects]").forEach(function (el) {
        var list = (el.getAttribute("data-projects") || "").split(" ");
        el.hidden = !!f && list.indexOf(f) < 0;
      });
      draw();
    });
  });
  draw();
  window.addEventListener("resize", draw);
})();
</script>
</body>
</html>
""")
