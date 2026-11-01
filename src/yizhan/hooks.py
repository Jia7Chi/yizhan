"""Entry point for Claude Code and Codex hooks.

A hook must never break the session it runs in: any failure is logged on this
machine, and the hook exits 0 without output. Both hosts read the same JSON:
{"hookSpecificOutput": {"hookEventName": ..., "additionalContext": ...}}.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

from . import adapters, brief, mail, sync, util, wake
from . import ledger as ledger_mod
from .i18n import pick

ERROR_LOG_MAX = 1_000_000      # bytes; keep the local error log from growing without end


def run(agent, stdin=None, stdout=None, event=None):
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    event = event or payload.get("hook_event_name") or ""
    try:
        text = _handle(agent, event, payload)
    except Exception:  # noqa: BLE001  a hook must not take the session down
        log_error(f"{agent} {event}")
        return 0
    if text:
        out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
        # ASCII only: \\u escapes carry the same text, and the JSON stays intact however the
        # host decodes our output (a Windows code page such as cp1252, for one).
        stdout.write(json.dumps(out) + "\n")
        stdout.flush()
    return 0


def _handle(agent, event, payload):
    led = ledger_mod.open_local(required=False)
    if led is None:
        return None
    if payload.get("agent_id") or "/subagents/" in str(payload.get("transcript_path") or ""):
        return None                                    # a subagent is not a seat of its own
    try:
        prof = led.resolve_seat(cwd=payload.get("cwd") or os.getcwd(), agent=agent)
    except util.YizhanError as e:
        return pick(f"【驿站】{e}", f"[yizhan] {e}") if event == "SessionStart" else None
    if prof is None:
        return None                                    # this folder is not part of the ledger
    seat, session = prof["address"], str(payload.get("session_id") or "")
    budget = adapters.get(agent).context_budget
    if event == "SessionStart":
        note = None
        try:
            sync.pull_only(led, timeout=10)
        except util.YizhanError as e:
            note = pick(f"拉取别的机器的新内容失败（{e}），下面按本机已有的内容。",
                        f"Could not pull new changes from the other machines ({e}); what follows is what this "
                        "machine already has.")
        mail.touch(led, seat)
        return brief.session_brief(led, prof, budget, session=session, note=note)
    if event in ("UserPromptSubmit", "PostToolUse"):
        return brief.new_mail_notice(led, prof, session, budget)
    if event == "Stop":
        # The turn is over, so this seat's letters are final ("sealed") and may wake others.
        previous_end = util.parse_iso(prof["presence"].get("sealed_at"))
        mail.touch(led, seat, seal=True)
        if sync.needs_push(led, seat):
            try:
                sync.sync(led, quick=True, net_timeout=20)
            except util.YizhanError:
                log_error(f"{agent} Stop sync")
        try:
            wake.kick(led, seat, since=previous_end)   # readers on this machine with autowake on
        except Exception:  # noqa: BLE001
            log_error(f"{agent} Stop kick")
        return None                                    # Stop takes no context in either host
    return None


def log_error(where):
    try:
        path = util.state_home() / "hook-errors.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > ERROR_LOG_MAX:
            path.write_text("", encoding="utf-8")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"===== {util.iso(util.utcnow())} {where}\n{traceback.format_exc()}\n")
    except OSError:
        pass
