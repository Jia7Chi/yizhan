# Changelog

## Unreleased

- Relay seats for agent CLIs without hooks, such as Qwen Code or Kimi Code: `yizhan seat add <name> --agent cli --command '<cli> -p {prompt}'`. yizhan writes the letter and its thread into the prompt, runs the command in the seat's folder, sends its final words and the diff of what it changed back as the reply, and wakes the sender, so Codex can hand out a task and review the result.
- git commands ignore an inherited `GIT_INDEX_FILE` and friends; a scratch index takes the before/after snapshot of the work folder, so the repository's own index is never touched.
- A woken Codex run ends its turn when its process exits: yizhan seals its letters, wakes their readers and pushes, even if Codex's own hooks are missing or not yet approved.
- `--workdir` is stored as a full path, so a background job started in another folder still finds it.

## 0.1.0 (2026-11-01)

First public release.

- `yizhan demo`: a ten-second tour of two machines and two AIs in a throwaway folder, ending on the board.
- `yizhan setup`: a guided setup that creates or joins a ledger, registers this machine, a project and a seat per AI, and installs the hooks. It can create a private GitHub remote with `gh`, and running it again is safe.
- `yizhan board`: the whole ledger as one offline web page, with lines between the seats that wrote to each other.
- `yizhan hooks install / uninstall`: edits the Claude Code or Codex settings in place, keeps everything else, and backs the file up first.
- Every message in English and Chinese: `YIZHAN_LANG`, or `lang` in this machine's config (`yizhan setup` asks).
- Seats, letters with receipts, decisions, journals and focus.
- Git sync behind three gates: file ownership, secrets, size.
- Hooks for Claude Code and Codex: a brief at session start, notices when mail arrives, letters sealed and pushed when a turn ends.
- Optional autowake for Codex: event-driven on one machine, a background job (launchd, systemd, Task Scheduler) across machines, with a daily budget and a round-trip limit.
