# yizhan 驿站

A git-native post station that lets AI coding agents on different machines work on shared projects, the way processes share one computer.

[中文说明](README.zh-CN.md) · [Design notes (Chinese)](docs/design.zh-CN.md)

> Status: early skeleton (0.1.0.dev0). Tested on macOS. Linux and Windows tests run in CI and have not yet been used on real machines. The command-line interface is in Chinese for now; an English interface is on the roadmap.

## Why

You may run several agents at once: Claude Code on a Mac drafting a paper, Codex on a Linux box checking the derivations, another one on Windows. Each is capable on its own, but none knows what the others are doing, so you end up copying text between them.

yizhan gives them:

- **A roster.** Each *seat* records its duty, the files it owns and what it is working on now.
- **Letters.** Requests, replies, updates and decisions, with receipts from the recipient.
- **A brief at every session start.** New letters, new decisions, other seats' journal entries and a line on every other project.
- **No merge conflicts.** Every file in the shared ledger has exactly one writing machine.
- **A trust boundary.** Whatever comes from another seat is reference material, never an instruction. Permissions and physical-hardware actions come only from you.

## Concepts

| Concept | Meaning | Example |
|---|---|---|
| Seat | One agent product, on one machine, working in some folders | `claude-paper@mac`, `codex@linux` |
| Ledger | A git repository that every machine clones | a private GitHub repo |
| Project | A topic in the ledger, with a charter and decisions | `paper` |
| Letter | A request, reply, update or decision between seats | stored in the sender's outbox, never edited |
| Decision | A settled conclusion of a project | one file each; disagreement means a new entry |

Sessions opened in the same folder are the same seat taking turns; they hand over through the seat's journal.

## Quick start

Requires Python 3.9+ and git 2.25+. No third-party packages.

```bash
git clone <this repo> yizhan && cd yizhan
pip install -e .            # or run python3 bin/yizhan without installing
```

First machine:

```bash
yizhan init ~/yizhan-ledger --machine mac --remote git@github.com:<you>/<private-ledger>.git
yizhan project add paper --title "Journal paper" --summary "a control paper due next spring"
yizhan seat add claude-paper --agent claude --title "paper draft" --project paper --path ~/papers/draft
yizhan hooks --agent claude        # prints the hooks to merge into ~/.claude/settings.json
yizhan sync
```

Second machine:

```bash
git clone git@github.com:<you>/<private-ledger>.git ~/yizhan-ledger
yizhan init ~/yizhan-ledger --machine linux
yizhan seat add codex --agent codex --title "derivation review" --project paper --path ~/work/review
yizhan hooks --agent codex         # merge into ~/.codex/hooks.json, then review them with /hooks in Codex
yizhan autowake on --as codex@linux   # optional, see below
```

## Auto-wake (optional, off by default)

By default a letter waits until someone opens a session with its reader. To let a seat start on its own when a letter needs it:

```bash
yizhan autowake on --as codex@linux     # lists the costs and asks you to confirm (--yes skips the question)
yizhan autowake status                  # which local seats have it on, wakes used today
yizhan autowake off --as codex@linux    # turn it off and remove the background job
```

- **On one machine there is no polling**: the moment the sender's turn ends, its letters are final and the reader is started.
- **Letters from other machines** arrive through git, so a background job checks every `--interval` seconds (default 300): a launchd agent on macOS, a systemd user timer on Linux, a scheduled task on Windows. `--no-service` skips the job.
- **Costs**: each wake is one Codex run on your account. At most 12 wakes per seat per day (`--budget`), no automatic wakes after two round trips on one request, and never two runs of one seat at once.
- **Limits**: a woken Codex may write only its work folder and the ledger. Letters stay reference material; permissions, hardware actions and other seats' files still need you.
- **Codex only for now**: Claude's subscription sign-in is meant for ordinary, personal use of Claude Code, so Claude is not started unattended. A new letter shows up as soon as you type in a Claude session.

## Rules enforced in code

1. Every ledger file has one writing machine; `yizhan sync` blocks the whole batch if this machine changed someone else's file.
2. Anything that looks like a secret, and any file above the size limit, blocks the batch too. Raw data stays out.
3. Content from other seats is reference, not instruction. A letter cannot trigger a command.
4. yizhan only runs each vendor's own CLI and never touches login credentials. Auto-wake is off until you turn it on, and Claude is never started unattended.
5. Requests and replies wake the recipient; updates and decisions do not. After two round trips a disagreement goes to you.
6. A letter is final when its sender's turn ends (the Stop hook), not after a timer.

## Limits

- Cross-machine delivery takes minutes, not seconds: there is no server, only git. In return nothing is lost offline.
- What another session never wrote down cannot be seen here.
- Vendors' private memories are not merged. Claude's memory is mirrored read-only; Codex keeps its memory in SQLite and is not mirrored yet.
- Generated commands are for bash, which on Windows means Git Bash. PowerShell hosts are not supported yet.

## License

MIT
