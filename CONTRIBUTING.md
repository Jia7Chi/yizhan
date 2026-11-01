# Contributing

Thanks for looking. yizhan is small on purpose: standard-library Python 3.9+, git, and nothing else.

## Run it from a checkout

```bash
git clone https://github.com/Jia7Chi/yizhan && cd yizhan
python3 bin/yizhan demo            # no install needed
pip install -e .                   # or install the `yizhan` command
```

## Tests

```bash
YIZHAN_LANG=zh python3 -m unittest discover -s tests
```

The tests build real ledgers and bare git remotes in temporary folders; they never touch your own config. CI runs them on Linux, macOS and Windows.

## Ground rules for changes

- **No dependencies.** If something needs a package, it probably belongs in a separate tool.
- **One writer per file.** Every path in the ledger has exactly one machine that may write it (`Ledger.owner_of`). New kinds of records must keep that property; it is why git never has to merge.
- **Text in both languages.** User-facing strings are written in pairs where they are used: `pick("中文", "English")`.
- **No hand-tuned numbers.** Limits such as the wake budget or the autowake interval are user policies with documented defaults, not values tuned until a test passes.
- **Only official CLIs.** yizhan starts `claude` and `codex` as they are and never touches their login credentials.

## Adding another agent

An adapter lives in `src/yizhan/adapters.py`: the agent's hook format and where its config lives, how much text one hook may inject, how to start a headless run, and where its memory is. If the agent's CLI has hooks for session start, prompt submit and turn end, it can be a full seat; send a pull request with tests.

## The README pictures

`python3 scripts/readme_assets.py` regenerates the animated terminal and a sample ledger from real yizhan output; screenshot the printed board pages at 1280 px wide for `docs/assets/board-*.png`.
