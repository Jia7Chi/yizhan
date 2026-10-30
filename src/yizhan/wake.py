"""Wake an idle seat when letters that need it are final.

A letter becomes final ("sealed") when its sender's turn ends, which the Stop
hook records. So the batch of letters that one wake handles is decided by that
event, not by a timer. max_delivery_delay_s only covers a sender that never ends
its turn, and wake_budget_per_day is the quota you allow each seat.
"""
from __future__ import annotations

import subprocess
import time

from . import adapters, brief, gitio, mail, state, sync, util


def wake_once(ledger, seat, dry_run=False, pull=True, now=None, runner=None):
    prof = ledger.require_seat(seat, local=True)
    mode = prof.get("wake") or adapters.get(prof["agent"]).default_wake
    result = {"seat": seat, "mode": mode, "notes": []}
    if pull and gitio.has_remote(ledger.root):
        try:
            sync.pull_only(ledger, timeout=60)
        except util.YizhanError as e:
            result["notes"].append(f"拉取失败：{e}")
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
    argv, workdir = adapters.wake_argv(prof, adapters.wake_prompt(prof, brief.cli_prefix()),
                                       out_dir / f"{seat}.last.md")
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
    result["returncode"] = run(argv, workdir, out_dir / f"{seat}.log",
                               float(ledger.settings["max_wake_run_s"]))
    result["action"] = "woke"
    return result


def _run(argv, workdir, log_path, timeout):
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n===== {util.iso(util.utcnow())} {argv[0]} {argv[1] if len(argv) > 1 else ''}\n")
        log.flush()
        try:
            return subprocess.run(argv, cwd=workdir, stdout=log, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            log.write(f"超过 {timeout:g} 秒，已停止\n")
            return -1
        except OSError as e:
            log.write(f"启动失败：{e}\n")
            return -1


def watch(ledger, seat, interval, dry_run=False, say=print):
    """Check every `interval` seconds: set it from how soon an idle seat should notice
    letters from other machines."""
    while True:
        r = wake_once(ledger, seat, dry_run=dry_run)
        if r["action"] not in ("idle", "waiting"):
            say(describe(r))
        time.sleep(interval)


def describe(r):
    action = r["action"]
    if action == "idle":
        return f"{r['seat']}：没有要叫醒它的信"
    if action == "waiting":
        return f"{r['seat']}：有 {r['pending']} 封待办信，发信方这一轮还没结束，先等着"
    if action == "queued":
        return f"{r['seat']}：{r['ready']} 封待办信已经排队，下次有人开会话时提示（这个岗位只排队、不自动启动）"
    if action == "over_budget":
        return f"{r['seat']}：今天已叫醒 {r['used']} 次，到了每天 {r['budget']} 次的上限，信先排队"
    if action == "dry_run":
        return f"{r['seat']}：会叫醒它处理 {r['ready']} 封信，命令：{' '.join(r['argv'][:6])} …"
    return f"{r['seat']}：已叫醒，处理 {r['ready']} 封信，返回码 {r.get('returncode')}"
