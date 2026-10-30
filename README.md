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
yizhan wake --as codex@linux --watch
```

## Rules enforced in code

1. Every ledger file has one writing machine; `yizhan sync` blocks the whole batch if this machine changed someone else's file.
2. Anything that looks like a secret, and any file above the size limit, blocks the batch too. Raw data stays out.
3. Content from other seats is reference, not instruction. A letter cannot trigger a command.
4. yizhan only runs each vendor's own CLI and never touches login credentials. Claude is not started unattended by default; its letters wait for a session you open.
5. Requests and replies wake the recipient; updates and decisions do not. After two round trips a disagreement goes to you.
6. A letter is final when its sender's turn ends (the Stop hook), not after a timer.

## Limits

- Cross-machine delivery takes minutes, not seconds: there is no server, only git. In return nothing is lost offline.
- What another session never wrote down cannot be seen here.
- Vendors' private memories are not merged. Claude's memory is mirrored read-only; Codex keeps its memory in SQLite and is not mirrored yet.
- Generated commands are for bash, which on Windows means Git Bash. PowerShell hosts are not supported yet.

## License

MIT
