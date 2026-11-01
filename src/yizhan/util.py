"""Small cross-platform helpers. Standard library only, Python 3.9+.

Two choices here exist because of Windows:
- Time zones are stored as fixed UTC offsets ("+08:00"). zoneinfo has no
  time-zone data on Windows unless the third-party tzdata package is installed.
- File locks use msvcrt on Windows and fcntl elsewhere.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

from .i18n import pick

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ADDRESS_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,39})@([a-z0-9][a-z0-9-]{0,39})$")
# Characters Windows refuses in file names, plus control characters.
UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
UTC = dt.timezone.utc


class YizhanError(Exception):
    """An expected, user-facing error. The CLI prints it without a traceback."""


# ---------- names ----------

def check_name(value, what):
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise YizhanError(pick(f"{what}“{value}”不合格：只能用小写英文字母、数字和连字符，最长 40 个字符",
                               f"Invalid {what} \"{value}\": use only lower-case letters, digits and hyphens, "
                               "at most 40 characters"))
    return value


def parse_address(address):
    """'claude@mac' -> ('claude', 'mac')."""
    m = ADDRESS_RE.match(address or "")
    if not m:
        raise YizhanError(pick(f"地址“{address}”不合格，格式是 名字@机器，例如 claude@mac",
                               f"Invalid address \"{address}\": the form is name@machine, for example claude@mac"))
    return m.group(1), m.group(2)


def title_slug(title, limit=40):
    """Turn a title (Chinese is fine) into a file-name part that every OS accepts."""
    s = UNSAFE_FILENAME.sub("", title or "")
    s = re.sub(r"\s+", "-", s.strip()).strip(". -")
    return s[:limit] or "untitled"


def short_hash(text, n=12):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


# ---------- time ----------

def utcnow():
    return dt.datetime.now(UTC).replace(microsecond=0)


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    if not text:
        return None
    try:
        t = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def parse_offset(text):
    """'+08:00' -> a fixed-offset timezone."""
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", (text or "").strip())
    if not m:
        raise YizhanError(pick(f"时区“{text}”不合格，请写成 +08:00 这样的 UTC 偏移",
                               f"Invalid time zone \"{text}\": write it as a UTC offset such as +08:00"))
    delta = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return dt.timezone(delta if m.group(1) == "+" else -delta)


def system_offset():
    """This machine's current UTC offset, e.g. '+08:00'."""
    off = dt.datetime.now().astimezone().utcoffset() or dt.timedelta(0)
    minutes = int(off.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def ago(t, now=None):
    if t is None:
        return pick("还没有动静", "never")
    secs = int(((now or utcnow()) - t).total_seconds())
    if secs < 60:
        return pick("刚刚", "just now")
    if secs < 3600:
        return pick(f"{secs // 60} 分钟前", f"{secs // 60} min ago")
    if secs < 86400:
        return pick(f"{secs // 3600} 小时前", f"{secs // 3600} h ago")
    return pick(f"{secs // 86400} 天前", f"{secs // 86400} d ago")


# ---------- files ----------

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_text_atomic(path, text):
    """Write to a temp file in the same folder, then rename over the target.

    Readers never see a half-written file. Temp names start with '.tmp-',
    which the ledger's .gitignore excludes.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: another process may hold the target open for a moment.
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def remove_tree(path):
    """Delete a folder and everything in it, quietly. Git marks its object files
    read-only, and Windows refuses to delete those until they are writable again;
    shutil.rmtree(ignore_errors=True) would silently leave them behind."""
    def force(func, p, _info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    handler = {"onexc": force} if sys.version_info >= (3, 12) else {"onerror": force}
    shutil.rmtree(path, **handler)


def write_json(path, value):
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def append_text(path, text):
    """Append under the caller's lock. Used for files only one machine writes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(text)


FENCE = "---"


def dump_frontmatter(meta, body):
    """A Markdown file whose header is a JSON object between two '---' lines."""
    head = json.dumps(meta, ensure_ascii=False, indent=2)
    return f"{FENCE}\n{head}\n{FENCE}\n{body.rstrip()}\n"


def load_frontmatter(text):
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != FENCE:
        raise ValueError(pick("缺少文件头", "Missing header"))
    for i in range(1, len(lines)):
        if lines[i].strip() == FENCE:
            meta = json.loads("\n".join(lines[1:i]))
            if not isinstance(meta, dict):
                raise ValueError(pick("文件头不是 JSON 对象", "The header is not a JSON object"))
            return meta, "\n".join(lines[i + 1:]).rstrip("\n")
    raise ValueError(pick("文件头没有结束标记", "The header has no closing --- line"))


def read_frontmatter(path):
    with open(path, encoding="utf-8") as f:
        return load_frontmatter(f.read())


# ---------- locks ----------

if os.name == "nt":
    import msvcrt

    def _try_lock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(path, timeout=30.0):
    """An exclusive lock between processes on this machine."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _try_lock(f)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise YizhanError(pick(f"等锁超时：{path}。可能另一个 yizhan 进程卡住了。",
                                           f"Timed out waiting for the lock {path}. Another yizhan process "
                                           "may be stuck."))
                time.sleep(0.1)
        try:
            yield
        finally:
            _unlock(f)
    finally:
        f.close()


@contextlib.contextmanager
def try_file_lock(path):
    """Like file_lock, but never waits: yields False if another process holds it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    try:
        try:
            _try_lock(f)
            got = True
        except OSError:
            got = False
        try:
            yield got
        finally:
            if got:
                _unlock(f)
    finally:
        f.close()


# ---------- places ----------

def config_home():
    env = os.environ.get("YIZHAN_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "yizhan"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "yizhan"


def state_home():
    env = os.environ.get("YIZHAN_STATE")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "yizhan" / "state"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "yizhan"


def norm_path(p):
    p = os.path.realpath(os.path.expanduser(str(p)))
    return os.path.normcase(unicodedata.normalize("NFC", p))


def is_within(child, parent):
    c, p = norm_path(child), norm_path(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:      # different drives on Windows
        return False


def setup_console():
    """Print UTF-8 even where the console default is GBK or cp1252."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
