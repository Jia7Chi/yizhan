"""Per-machine state that never enters the ledger: what each seat has already seen,
how often it was woken, how the last sync went."""
from __future__ import annotations

import contextlib

from . import util


def _path(ledger, name):
    return ledger.state_dir / f"{name}.json"


def load(ledger, name):
    try:
        data = util.read_json(_path(ledger, name), {})
    except ValueError:          # a damaged state file must not stop anything
        data = {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def edit(ledger, name):
    # The lock is named after the state file, so it can never be a lock the caller already holds.
    with util.file_lock(ledger.state_dir / f"{name}.json.lock"):
        data = load(ledger, name)
        yield data
        util.write_json(_path(ledger, name), data)
