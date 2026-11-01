"""Chinese or English for everything yizhan prints.

Text is written in pairs at the place it is used: pick("中文", "English").
The language comes from, in order: $YIZHAN_LANG, "lang" in this machine's local
config (`yizhan setup` asks), the locale variables, the macOS language list, and
finally English.
"""
from __future__ import annotations

import os
import subprocess
import sys

_chosen = None


def _norm(value):
    return "zh" if value.strip().lower().replace("_", "-").startswith("zh") else "en"


def _detect():
    env = os.environ.get("YIZHAN_LANG")
    if env:
        return _norm(env)
    from . import util                    # imported here: util imports this module
    try:
        cfg = util.read_json(util.config_home() / "local.json", {}) or {}
    except (OSError, ValueError):
        cfg = {}
    if isinstance(cfg, dict) and cfg.get("lang"):
        return _norm(str(cfg["lang"]))
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(key, "")
        if value and value not in ("C", "POSIX") and not value.startswith("C."):
            return _norm(value)
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                                 capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            line = line.strip().strip('",')
            if line and line not in ("(", ")"):
                return _norm(line)
    return "en"


def lang():
    """'zh' or 'en', worked out once per process."""
    global _chosen
    if _chosen is None:
        _chosen = _detect()
    return _chosen


def reset():
    """Forget the choice, e.g. after setup changes it or a test changes the environment."""
    global _chosen
    _chosen = None


def pick(zh, en):
    return zh if lang() == "zh" else en
