"""Wake an idle seat when letters that need it are final.

Starting an agent on its own is opt-in per seat (`yizhan autowake on`); otherwise
letters wait for a session someone opens. Two things start a waker:

- On one machine, the moment the sender's turn ends (`kick`): its own Stop hook or,
  for a run yizhan woke, the waker as soon as that run exits. The sender's letters
  are final, so the reader can start at once. No polling.
- Letters that arrive from another machine through git, or whose sender never ends
  its turn, are picked up by a background job that runs `yizhan wake` every so often
  (service.py). Its interval is how soon such a letter should be noticed.

max_delivery_delay_s covers only a sender that never ends its turn, and
wake_budget_per_day is the quota you allow each seat.
"""
from __future__ import annotations

import os
import subprocess
import time

from . import adapters, brief, gitio, mail, relay, state, sync, util
from .i18n import pick


def wake_once(ledger, seat, dry_run=False, pull=True, now=None, runner=None):
    """Check once and start a headless run if letters are ready. Only one waker per seat
    runs at a time. One started meanwhile leaves a note and returns "busy"; the running
    waker looks for that note once it has let go of the lock and, if it is there, checks
    again. So a letter that arrives during a run (a fast reply, say) is handled when the
    run ends instead of waiting for the next trigger, and nothing polls."""
    lock, again = ledger.state_dir / f"wake-{seat}.lock", ledger.state_dir / f"wake-{seat}.again"
    first = None
    while True:
        with util.try_file_lock(lock) as got:
            if not got:
                if not dry_run:
                    _flag(again)
                return first or {"seat": seat, "action": "busy", "notes": []}
            _take(again)                            # a note left before this check is covered by it
            r = _wake_once(ledger, seat, dry_run, pull and first is None, now if first is None else None, runner)
        first = first or r
        if dry_run or not _take(again):
            return first


def _flag(path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        pass


def _take(path):
    """Remove the note; True if it was there."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _wake_once(ledger, seat, dry_run, pull, now, runner):
    prof = ledger.require_seat(seat, local=True)
    mode = prof.get("wake") or adapters.get(prof["agent"]).default_wake
    result = {"seat": seat, "mode": mode, "notes": []}
    if pull and gitio.has_remote(ledger.root):
        try:
            sync.pull_only(ledger, timeout=60)
        except util.YizhanError as e:
            result["notes"].append(pick(f"拉取失败：{e}", f"Pull failed: {e}"))
    now = now or util.utcnow()
    items = mail.inbox(ledger, seat)
    open_ids = {it["meta"]["id"] for it in items}
    st_name = f"seat-{seat}"
    st0 = state.load(ledger, st_name)
    woken = set(st0.get("woken", [])) & open_ids
    # A letter whose prompt cannot fit this template's command line waits for a new template.
    paused = {k for k, v in (st0.get("paused") or {}).items() if v == prof.get("command")}
    pending = [it for it in items if it["meta"].get("wake") and it["meta"]["id"] not in woken | paused]
    limit = float(ledger.settings["max_delivery_delay_s"])
    ready = []
    for it in pending:
        created = util.parse_iso(it["meta"].get("created")) or now
        if mail.is_sealed(ledger, it["meta"]) or (now - created).total_seconds() >= limit:
            ready.append(it)
    result.update(pending=len(pending), ready=len(ready), ids=[it["meta"]["id"] for it in ready])
    if not ready:
        result["action"] = "waiting" if pending else "idle"
        return result
    if mode == "queue":
        result["action"] = "queued"          # the next session's brief lists them
        return result
    today = now.astimezone(ledger.tz).date().isoformat()
    st = state.load(ledger, st_name)
    used = int(st.get("wakes", {}).get(today, 0))
    budget = int(prof.get("wake_budget_per_day") or ledger.settings["wake_budget_per_day"])
    if used >= budget:
        result.update(action="over_budget", used=used, budget=budget)
        return result
    if prof["agent"] == "cli":
        return _relay(ledger, prof, ready, result, today, used, budget, open_ids, dry_run, runner)
    out_dir = ledger.state_dir / "wake"
    # The run must be able to answer: let it write the ledger and yizhan's state folder.
    argv, workdir = adapters.wake_argv(prof, adapters.wake_prompt(prof, brief.cli_prefix()),
                                       out_dir / f"{seat}.last.md",
                                       writable=(ledger.root, util.state_home()))
    result.update(argv=argv, workdir=workdir)
    if dry_run:
        result["action"] = "dry_run"
        return result
    # Record the wake before running, so a slow run is never started twice.
    with state.edit(ledger, st_name) as s:
        s["woken"] = sorted((set(s.get("woken", [])) & open_ids) | set(result["ids"]))
        s["wakes"] = {today: used + 1}
        s["last_wake_at"] = util.iso(now)
    out_dir.mkdir(parents=True, exist_ok=True)
    run = runner or _run
    log_path = out_dir / f"{seat}.log"
    started, error = util.utcnow(), None
    try:
        rc = run(argv, workdir, log_path, float(ledger.settings["max_wake_run_s"]))
    except Exception as e:  # noqa: BLE001 - an unwritable log, say: still a failed run, not a lost letter
        rc, error = -1, pick(f"叫醒失败：{e}", f"Wake failed: {e}")
        _note(log_path, error)
    result["returncode"] = rc
    result["action"] = "woke"
    with state.edit(ledger, st_name) as s:
        s["last_wake_rc"] = rc
        if rc == 0:
            s.pop("last_wake_error", None)
        else:
            # The run failed, so these letters were not handled: put them back so the next
            # check tries again. Every try still counts against the daily budget.
            s["woken"] = sorted(set(s.get("woken", [])) - set(result["ids"]))
            s["last_wake_error"] = error or _log_tail(log_path)
    _end_turn(ledger, seat, started, result)
    return result


def _end_turn(ledger, seat, started, result):
    """What the seat's own Stop hook does, done again once the woken run has exited: its
    letters are final, the readers on this machine wake, and the letters go out. The run
    is over when its process is, so the next step never waits on hooks that are missing
    or not yet approved; when the hooks did run, this finds nothing left to do."""
    mail.touch(ledger, seat, seal=True)
    _wake_readers(ledger, seat, started, result)
    _push(ledger, seat, result)


# The run's outcome is already recorded when these run. Like the Stop hook, a failure
# here is only noted: the letters are final, and a later check still finds them.

def _wake_readers(ledger, seat, since, result):
    try:
        new = kick(ledger, seat, since=since)
    except Exception as e:  # noqa: BLE001
        result["notes"].append(pick(f"没能叫醒收信方：{e}", f"Could not wake the readers: {e}"))
        return
    result["kicked"] = result.get("kicked", []) + [k for k in new if k not in result.get("kicked", [])]


def _push(ledger, seat, result):
    try:
        if gitio.has_remote(ledger.root) and sync.needs_push(ledger, seat):
            sync.sync(ledger, quick=True)
    except (util.YizhanError, OSError) as e:
        result["notes"].append(pick(f"推送没成功，下次同步再推：{e}", f"Push failed; the next sync retries: {e}"))


def _relay(ledger, prof, ready, result, today, used, budget, open_ids, dry_run, runner):
    """A seat without hooks (relay.py): yizhan hands it the letters one at a time and
    sends back what it did. Each reply is final when it is written, so its reader, such
    as a reviewer, is woken right then rather than after the whole batch."""
    seat, st_name = prof["address"], f"seat-{prof['address']}"
    out_dir = ledger.state_dir / "wake"
    if dry_run:
        result.update(action="dry_run", argv=relay.parse_template(relay.absolute(prof.get("command") or "")))
        return result
    out_dir.mkdir(parents=True, exist_ok=True)
    done = []
    for item in ready:
        if used >= budget:
            result.update(over_budget=True, used=used, budget=budget)
            break
        mid = item["meta"]["id"]
        with state.edit(ledger, st_name) as s:      # recorded first: a slow run is never started twice
            s["woken"] = sorted((set(s.get("woken", [])) & open_ids) | {mid})
            s["wakes"] = {today: used + 1}
            s["last_wake_at"] = util.iso(util.utcnow())
        used += 1
        started, log_path = util.utcnow(), out_dir / f"{seat}.log"
        try:
            r = relay.relay_one(ledger, prof, item, out_dir, float(ledger.settings["max_wake_run_s"]), run=runner)
        except Exception as e:  # noqa: BLE001 - nobody is watching; the letter must not be lost
            # A bad template, an unreadable letter, a reply that could not be written: the
            # letter was not answered, so it is a failed run like any other.
            error = pick(f"处理 {mid} 时出错：{e}", f"Error while handling {mid}: {e}")
            _note(log_path, error)
            r = {"letter": mid, "returncode": -1, "error": error}
        done.append(r)
        with state.edit(ledger, st_name) as s:
            s["paused"] = {k: v for k, v in (s.get("paused") or {}).items() if k in open_ids and k != mid}
            if r.get("paused"):
                # The command never started, so this try uses none of the day's runs; the
                # letter waits until the template changes, and the others go on.
                s["woken"] = sorted(set(s.get("woken", [])) - {mid})
                s["paused"][mid] = r["paused"]
                s["wakes"] = {today: used - 1}
            else:
                s["last_wake_rc"] = r["returncode"]
                if r["returncode"] == 0:
                    s.pop("last_wake_error", None)
                else:
                    # Not handled: back to the queue for the next check, as for any failed wake.
                    s["woken"] = sorted(set(s.get("woken", [])) - {mid})
                    s["last_wake_error"] = r.get("error") or _log_tail(log_path)
        if r.get("paused"):
            used -= 1
            continue
        if r["returncode"] != 0:
            break                                   # the command fails now; the next check tries again
        _wake_readers(ledger, seat, started, result)   # this reply's reader starts now
    if not done:
        result.update(action="over_budget", used=used, budget=budget)
        return result
    result.update(action="relayed", returncode=done[-1]["returncode"],
                  replies=[r["reply"] for r in done if r.get("reply")],
                  paused=[r["letter"] for r in done if r.get("paused")])
    mail.touch(ledger, seat, seal=True)             # the turn is over
    _push(ledger, seat, result)
    return result


def _note(path, text):
    """Add a line to a log if it can be written; a log is never worth losing a letter over."""
    try:
        with open(path, "a", encoding="utf-8") as log:
            log.write(text + "\n")
    except OSError:
        pass


def _log_tail(path, lines=3):
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return " | ".join(text[-lines:])[:300]


def _run(argv, workdir, log_path, timeout):
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n===== {util.iso(util.utcnow())} {argv[0]} {argv[1] if len(argv) > 1 else ''}\n")
        log.flush()
        try:
            rc = util.run_tree(argv, workdir, log, subprocess.STDOUT, timeout)
        except OSError as e:
            log.write(pick(f"启动失败：{e}\n", f"Could not start: {e}\n"))
            return -1
        if rc is None:
            log.write(pick(f"超过 {timeout:g} 秒，已停止\n", f"Ran longer than {timeout:g} s, stopped\n"))
            return -1
        return rc


# ---------- waking at the moment the sender's turn ends ----------

def fresh_targets(ledger, sender, since=None):
    """Local seats with autowake on that hold a wake-worthy letter from `sender`
    written after `since` (the sender's previous turn end) and not yet closed."""
    targets = set()
    outbox = ledger.seat_dir(sender) / "outbox"
    for path in sorted(outbox.glob("*.md")) if outbox.is_dir() else []:
        try:
            meta, _ = mail.read(path)
        except (OSError, ValueError):
            continue
        created = util.parse_iso(meta.get("created"))
        if not meta.get("wake") or (since and created and created < since):
            continue
        for to in meta.get("to", []):
            if to in targets or to.split("@", 1)[-1] != ledger.machine:
                continue
            prof = ledger.seat(to)
            if not prof or not prof.get("autowake"):
                continue
            rec = mail.latest_receipt(ledger, to, meta["id"])
            if not (rec and rec.get("status") in mail.TERMINAL):
                targets.add(to)
    return sorted(targets)


def kick(ledger, sender, since=None, spawn=None):
    """Start a waker now for each seat in fresh_targets. Called from the sender's Stop hook,
    so it must return at once: the wakers run on in the background."""
    started = []
    for seat in fresh_targets(ledger, sender, since):
        (spawn or spawn_detached)(brief.cli_argv() + ["wake", "--as", seat],
                                  ledger.state_dir / "wake" / f"{seat}.log")
        started.append(seat)
    return started


def spawn_detached(argv, log_path):
    """Start a process that keeps running after the hook that started it exits."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n===== {util.iso(util.utcnow())} "
                  + pick("发信方这一轮结束，开始叫醒", "the sender's turn ended; waking the reader") + "\n")
        log.flush()
        return subprocess.Popen(argv, stdout=log, **kwargs)


def watch(ledger, seat, interval, dry_run=False, say=print):
    """Check every `interval` seconds: set it from how soon an idle seat should notice
    letters from other machines."""
    while True:
        r = wake_once(ledger, seat, dry_run=dry_run)
        if r["action"] not in ("idle", "waiting", "busy"):
            say(describe(r))
        time.sleep(interval)


def _letters(n):
    return f"{n} letter" + ("" if n == 1 else "s")


def describe(r):
    action = r["action"]
    if action == "idle":
        return pick(f"{r['seat']}：没有要叫醒它的信", f"{r['seat']}: no letters to wake it for")
    if action == "busy":
        return pick(f"{r['seat']}：已经有一次叫醒在跑，这次跳过",
                    f"{r['seat']}: a wake is already running; skipping this one")
    if action == "waiting":
        return pick(f"{r['seat']}：有 {r['pending']} 封待办信，发信方这一轮还没结束，先等着",
                    f"{r['seat']}: {_letters(r['pending'])} needing action, but the sender's turn has not "
                    "ended yet; waiting")
    if action == "queued":
        return pick(f"{r['seat']}：{r['ready']} 封待办信已经排队，下次有人开会话时提示（这个岗位没开自动叫醒）",
                    f"{r['seat']}: {_letters(r['ready'])} needing action, queued for the next session "
                    "(autowake is off for this seat)")
    if action == "over_budget":
        return pick(f"{r['seat']}：今天已叫醒 {r['used']} 次，到了每天 {r['budget']} 次的上限，信先排队",
                    f"{r['seat']}: already woken {r['used']} time{'' if r['used'] == 1 else 's'} today, the "
                    f"daily limit is {r['budget']}; the letters stay queued")
    if action == "relayed":
        n = len(r.get("replies") or [])
        if r.get("returncode"):
            return pick(f"{r['seat']}：命令返回 {r['returncode']}，信放回待办，下次再试",
                        f"{r['seat']}: the command exited with {r['returncode']}; the letter is queued again")
        woke = r.get("kicked") or []
        return pick(f"{r['seat']}：处理了 {n} 封信，答复已发出" + (f"，叫醒了 {'、'.join(woke)}" if woke else ""),
                    f"{r['seat']}: handled {_letters(n)}, replies sent" + (f", woke {', '.join(woke)}" if woke else ""))
    if action == "dry_run":
        return pick(f"{r['seat']}：会叫醒它处理 {r['ready']} 封信，命令：{' '.join(r['argv'][:6])} …",
                    f"{r['seat']}: would wake it for {_letters(r['ready'])}, command: {' '.join(r['argv'][:6])} …")
    woke = r.get("kicked") or []
    return pick(f"{r['seat']}：已叫醒，处理 {r['ready']} 封信，返回码 {r.get('returncode')}"
                + (f"；跑完叫醒了 {'、'.join(woke)}" if woke else ""),
                f"{r['seat']}: woken for {_letters(r['ready'])}, exit code {r.get('returncode')}"
                + (f"; then woke {', '.join(woke)}" if woke else ""))
