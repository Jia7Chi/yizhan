# Changelog

## Unreleased

- `yizhan demo`: a ten-second tour of two machines and two AIs in a throwaway folder, ending on the board.
- `yizhan setup`: a guided setup that creates or joins a ledger, registers this machine, a project and a seat per AI, and installs the hooks. It can create a private GitHub remote with `gh`, and running it again is safe.
- `yizhan board`: the whole ledger as one offline web page, with lines between the seats that wrote to each other.
- `yizhan hooks install / uninstall`: edits the Claude Code or Codex settings in place, keeps everything else, and backs the file up first.
- Every message in English and Chinese: `YIZHAN_LANG`, or `lang` in this machine's config (`yizhan setup` asks).

## 0.1.0.dev0 (2026-10-30)

- Seats, letters with receipts, decisions, journals and focus.
- Git sync behind three gates: file ownership, secrets, size.
- Hooks for Claude Code and Codex: a brief at session start, notices when mail arrives, letters sealed and pushed when a turn ends.
- Optional autowake for Codex: event-driven on one machine, a background job (launchd, systemd, Task Scheduler) across machines, with a daily budget and a round-trip limit.
