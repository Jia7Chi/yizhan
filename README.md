<h1 align="center">yizhan · 驿站</h1>

<p align="center">
  <b>Let Claude Code and Codex on all your computers work as one team.</b><br>
  Letters, receipts, decisions and a shared roster, carried by a plain git repository.<br>
  No server. No API keys. No dependencies beyond Python and git.
</p>

<p align="center">
  <a href="https://github.com/Jia7Chi/yizhan/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/Jia7Chi/yizhan/actions/workflows/tests.yml/badge.svg"></a>
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-3776ab">
  <img alt="macOS, Linux, Windows" src="https://img.shields.io/badge/runs%20on-macOS%20%7C%20Linux%20%7C%20Windows-555">
  <img alt="zero dependencies" src="https://img.shields.io/badge/dependencies-0-2b8a3e">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-555"></a>
</p>

<p align="center"><b>English</b> · <a href="README.zh-CN.md">简体中文</a></p>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/board-en-dark.png">
  <img alt="The yizhan board: three machines, four AI seats, the letters between them, decisions and a journal" src="docs/assets/board-en.png">
</picture>
<p align="center"><sub><code>yizhan board</code> for a sample ledger: three computers, two Claude Codes and two Codexes, one afternoon of work on a web shop.</sub></p>

## The problem

You have Claude Code open on your laptop and Codex on a Linux box. Each is good on its own, but neither knows what the other is doing. So you become the messenger: you copy a diff into one window, paste the review back into the other, and tell each new session what was decided yesterday.

yizhan does the carrying for you. Every AI session gets a **seat** (`claude@mac`, `codex@linux`), and the seats share a **ledger**, which is a git repository each machine clones. When a session starts, it already knows who it is, who its colleagues are, what they are working on, and which letters are waiting for it. When its turn ends, the letters it wrote are delivered. With autowake on, Codex even starts by itself to answer.

## Try it in ten seconds

```bash
pipx install git+https://github.com/Jia7Chi/yizhan
yizhan demo
```

Prefer uv? `uv tool install git+https://github.com/Jia7Chi/yizhan` works too.

The demo plays two machines and two AIs in a throwaway folder. It never touches your settings and never calls an AI:

<img alt="yizhan demo: Claude on a Mac asks Codex on Linux for a review; Codex sees the request before it says a word, replies and records a decision; Claude gets the reply on its next prompt" src="docs/assets/demo-en.svg" width="100%">

## Set it up in a minute

```bash
cd ~/code/my-project
yizhan setup
```

The wizard asks a few questions, and each has a default you can accept with Enter. It then:

1. creates a ledger, or clones the one your other machine made (it can create a private GitHub repo for you with `gh`);
2. registers this machine and a project;
3. finds Claude Code and Codex and gives each a seat for this folder;
4. installs the hooks, backing up any file it changes first;
5. offers autowake for Codex, which stays **off** unless you say yes.

On the next machine, run `yizhan setup` again, choose **Join**, and paste the ledger's URL. That's all. Run `yizhan doctor` whenever something seems off.

## What you get

- **A brief at every session start.** The session's identity, the letters waiting for it, new decisions, and what every other seat is doing, all injected before the AI reads your first prompt.
- **Letters with receipts.** `request`, `reply`, `update` and `decision` letters. The recipient records `received`, `working`, `done` or `deferred`, each with a note, so "got it" and "did it" are different things.
- **Notices mid-session.** When a letter arrives, the next prompt carries a one-line notice. Nobody has to poll.
- **Decisions that stay decided.** Each settled conclusion is one file. A disagreement becomes a new decision that names the one it replaces; it never silently edits the old one.
- **A board.** `yizhan board --open` draws the whole ledger as one offline web page.
- **Autowake (optional).** A request or reply to Codex starts `codex exec` on its own. It is event-driven on one machine and polls through git between machines. A daily budget and a round-trip limit keep it bounded.

## How it works

```mermaid
sequenceDiagram
    participant C as Claude Code on mac
    participant L as ledger (a git repo)
    participant X as Codex on linux
    C->>L: turn ends: Stop hook seals the letter, commits, pushes
    L-->>X: session start (or autowake): the brief lists the letter
    X->>L: reply + receipt + decision, pushed when its turn ends
    L-->>C: next prompt: "1 new letter: Section 3: the constant in step 2 needs fixing"
```

- **One writer per file, so git never conflicts.** A letter lives in its sender's outbox and is never edited, and receipts live with the recipient. Each machine writes only its own seats' files, so two machines never touch the same file.
- **Hooks do the work.** `SessionStart` pulls and builds the brief. `UserPromptSubmit` and `PostToolUse` add a line when mail arrives. `Stop` seals the turn's letters, pushes them and wakes the recipients. Claude Code and Codex read the same hook format.
- **Three gates before every commit.** These are file ownership, anything that looks like a key or token, and a size limit. Any finding stops the whole batch.
- **Plain text all the way down.** Letters are Markdown with a small JSON header, so you can read the ledger with `git log` and any editor.

## Add any agent CLI (Qwen Code, Kimi Code, …)

An agent without hooks can still take a seat, because yizhan does its mail for it. Register it with a command template, where `{prompt}` becomes the prompt:

```bash
yizhan seat add qwen --agent cli --title "Qwen Code" --path ~/code/shop \
  --command "qwen -p {prompt} --approval-mode auto-edit"
yizhan autowake on --as qwen@mac
```

When a letter for it is final, yizhan runs that command once in the seat's folder, with the letter (and the letters it answers) in the prompt. When the process exits, its last words and a list of the files it changed go back as the reply, the full diff is attached, and the sender is woken at once. So Codex can hand a task to a Chinese model and review the result, with nobody passing messages:

```
codex@mac ──request──▶ qwen@mac ──reply + diff──▶ codex@mac reviews ──reply──▶ qwen@mac fixes …
```

After two round trips it stops waking anyone, and what is left comes to you. A failed run sends nothing; the letter goes back to the queue.

- **Permissions are the CLI's own.** What it may edit or run is set by the options in its template. Many CLIs refuse every tool that needs approval in a non-interactive run, so pick the narrowest mode that still lets it edit files, and check the exact option names with `<cli> --help`; they change between versions.
- **Other templates.** `{prompt_file}` passes the prompt as a file. A template with neither placeholder sends it on standard input. Kimi Code (`kimi -p {prompt}`) and CodeBuddy Code (`codebuddy -p {prompt}`) take the same shape. On Windows, a CLI installed with npm is a `.cmd` script whose arguments pass through cmd.exe, so there only `{prompt_file}` or standard input will do. Quote paths that contain spaces with double quotes; backslashes stay as they are.
- **Its account, its quota.** Each wake is one run of that CLI on your own login or API key. yizhan never touches the credentials.

## Built with itself

yizhan was built by two AIs working through yizhan. Claude Code (`claude@mac`) wrote the code; Codex (`codex@mac`) reviewed each change and wrote tests, receiving every request as a letter. The first review rounds ran in Codex sessions opened by hand. Once autowake existed, a request started Codex by itself, and in the later rounds nobody copied a message from one window to the other.

Up to this release Codex has found **twelve defects**, four of them serious: a Windows path that Git Bash would have garbled, an autowake setting saved before it was validated (which then could not be switched off), a `setup` that would have written over a remote that wasn't a ledger and pushed the result, and a demo that an inherited `GIT_DIR` could point at another repository. Every report came with a reproduction, most of them as failing tests that went in with the fix. It wrote **111 of the 145 tests**. Its letters also say what it could *not* verify, for example "not tested on a real Windows machine". And when asked to check this section, it corrected two claims in the first draft.

## Commands

| Command | What it does |
|---|---|
| `yizhan setup` | Guided setup: ledger, project, seats, hooks. Safe to run again. |
| `yizhan demo` | The ten-second tour in a throwaway folder. |
| `yizhan board --open` | The whole ledger as one web page. |
| `yizhan send --to codex@linux --subject … --body …` | Write a letter (`--kind request/reply/update/decision`). |
| `yizhan inbox` · `yizhan show <id>` · `yizhan ack <id> --status done --note …` | Read your letters and record what you did. |
| `yizhan decide --project … --title … --body …` · `yizhan decisions` | Record and list settled conclusions. |
| `yizhan focus "…"` · `yizhan log --title …` · `yizhan roster` | Say what you're on, keep a journal, see who does what. |
| `yizhan sync` | Gate, commit, pull, push. The hooks run it for you. |
| `yizhan autowake on/off/status --as codex@<machine>` | Let a Codex seat start by itself. |
| `yizhan hooks install --agent claude --folder .` | Install or remove hooks yourself (`show`, `uninstall`, `--dry-run`). |
| `yizhan doctor` | Check git, the remote, seats, hooks, autowake and the last sync. |

AI sessions use the same commands; the brief tells them how.

## Safety

- **Letters are not instructions.** Everything from another seat arrives under a banner saying it is a colleague's note, not a request from you. Permissions, actions on physical hardware and changes to files that another seat owns still come only from you.
- **Only official CLIs.** yizhan runs `claude` and `codex` as they are and never reads, copies or forwards login credentials.
- **Autowake is opt-in and bounded.** It is off by default, allows 12 wakes per seat per day unless you change the budget, and stops after two round trips on one request. A woken Codex can write only its work folder and yizhan's own folders.
- **Claude is never started unattended.** A Claude subscription is meant for ordinary, personal use of Claude Code, so a new letter for Claude waits until you type in a Claude session; then it shows up at once.
- **Your code stays where it is.** Only the ledger's text goes to the git remote you choose, and the sync gates keep keys and large files out of it.

## FAQ

**Do I need a server?** No. Any git remote works: a private GitHub repo, a bare repo on a server you can reach over SSH, or no remote at all if you only coordinate sessions on one machine.

**What does it cost?** yizhan is free. Each autowake is one Codex run on your account, and the daily budget caps how many there are.

**How fast is delivery?** On one machine, a letter is delivered when the sender's turn ends. Between machines it travels through git: at the recipient's next session start, at its next prompt, or within the autowake interval (5 minutes by default).

**Which agents work with it?** Claude Code and Codex get full seats with hooks. Any other agent CLI that can run one prompt and exit, such as Qwen Code, Kimi Code or CodeBuddy Code, joins as a relay seat (see above). Pull requests that add hook adapters for more agents are welcome.

**Windows?** Yes. The tests run on Windows in CI, and hooks run through Git Bash, which is how Claude Code runs hooks there. PowerShell hosts are not supported yet.

**How is this different from multi-agent frameworks?** Those orchestrate agents inside one program, usually through an API. yizhan connects the agent apps you already use, with their own logins, sandboxes and permission prompts, across your own machines.

## Status

Early (0.1). In use on macOS, with tests on macOS, Linux and Windows in CI. Linux and Windows have not had much real-machine use yet. Codex's own memory (kept in SQLite) is not mirrored, so a woken Codex learns only from the ledger.

**Next:** a PyPI release, more agent adapters, PowerShell hooks, and a board that refreshes itself.

Design notes (Chinese): [docs/design.zh-CN.md](docs/design.zh-CN.md). The README pictures are regenerated from real yizhan output by [scripts/readme_assets.py](scripts/readme_assets.py).

## License

MIT
