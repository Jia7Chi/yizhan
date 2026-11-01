"""Wake an idle seat when letters that need it are final.

Starting an agent on its own is opt-in per seat (`yizhan autowake on`); otherwise
letters wait for a session someone opens. Two things start a waker:

- On one machine, the sender's own Stop hook (`kick`). The sender's turn has just
  ended, so its letters are final and the reader can start at once. No polling.
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

from . import adapters, brief, gitio, mail, state, sync, util
from .i18n import pick


def wake_once(ledger, seat, dry_run=False, pull=True, now=None, runner=None):
    """Check once and start one headless run if letters are ready. Only one waker per
    seat runs at a time; another one started meanwhile returns action "busy"."""
    with util.try_file_lock(ledger.state_dir / f"wake-{seat}.lock") as got:
        if not got:
            return {"seat": seat, "action": "busy", "notes": []}
        return _wake_once(ledger, seat, dry_run, pull, now, runner)


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
    woken = set(state.load(ledger, st_name).get("woken", [])) & open_ids
    pending = [it for it in items if it["meta"].get("wake") and it["meta"]["id"] not in woken]
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
    result["returncode"] = rc = run(argv, workdir, log_path, float(ledger.settings["max_wake_run_s"]))
    result["action"] = "woke"
    with state.edit(ledger, st_name) as s:
        s["last_wake_rc"] = rc
        if rc == 0:
            s.pop("last_wake_error", None)
        else:
            # The run failed, so these letters were not handled: put them back so the next
            # check tries again. Every try still counts against the daily budget.
            s["woken"] = sorted(set(s.get("woken", [])) - set(result["ids"]))
            s["last_wake_error"] = _log_tail(log_path)
    return result


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
            return subprocess.run(argv, cwd=workdir, stdout=log, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            log.write(pick(f"超过 {timeout:g} 秒，已停止\n", f"Ran longer than {timeout:g} s, stopped\n"))
            return -1
        except OSError as e:
            log.write(pick(f"启动失败：{e}\n", f"Could not start: {e}\n"))
            return -1


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
    if action == "dry_run":
        return pick(f"{r['seat']}：会叫醒它处理 {r['ready']} 封信，命令：{' '.join(r['argv'][:6])} …",
                    f"{r['seat']}: would wake it for {_letters(r['ready'])}, command: {' '.join(r['argv'][:6])} …")
    return pick(f"{r['seat']}：已叫醒，处理 {r['ready']} 封信，返回码 {r.get('returncode')}",
                f"{r['seat']}: woken for {_letters(r['ready'])}, exit code {r.get('returncode')}")
