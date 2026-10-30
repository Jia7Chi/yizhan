"""The ledger: one git working tree that every machine clones.

Every file has exactly one writing machine, so commits from different machines
never touch the same file and git never has to merge content:

  yizhan.json, README.md, ...          shared; change only with --allow-shared
  machines/<machine>/...               written by <machine>
  seats/<name>@<machine>/...           written by <machine> (that seat's sessions)
  projects/<p>/project.json            written by the project's owner machine
  projects/<p>/README.md               same owner (the project charter)
  projects/<p>/decisions/<YYYYMMDD>_<name>@<machine>_<title>.md   written by <machine>
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
from pathlib import Path

from . import gitio, util
from .util import YizhanError

SHARED = "*shared*"
DECISION_RE = re.compile(r"^(\d{8})_([a-z0-9][a-z0-9-]{0,39})@([a-z0-9][a-z0-9-]{0,39})_(.+)\.md$")
AGENT_KINDS = ("claude", "codex")
WAKE_MODES = ("queue", "exec")

DEFAULTS = {
    "version": 1,
    "timezone": "+00:00",
    # Collaboration rules carried over from the original Mac/Linux bridge. They are
    # policies you choose, not tuned numbers; edit them in yizhan.json.
    "max_body_chars": 4000,        # a letter holds conclusions, asks and paths; details go into files
    "max_rounds": 2,               # after a request, two round trips still wake the other side
    "wake_kinds": ["request", "reply"],
    "wake_budget_per_day": 12,     # most wakes per seat per day: your quota decision
    "max_delivery_delay_s": 1800,  # longest you accept to wait when a sender never ends its turn
    "max_wake_run_s": 3600,        # longest one woken run may take
    "max_file_mb": 2,
}

LEDGER_README = """# 驿站账本

这是 yizhan（驿站）的共享账本，每台机器克隆一份。规矩只有一条：**每个文件只有一台机器写**。

| 位置 | 谁写 |
|---|---|
| `seats/<岗位>@<机器>/` | 那台机器（该岗位的会话） |
| `machines/<机器>/` | 那台机器 |
| `projects/<项目>/project.json`、`README.md` | 项目的主管机器 |
| `projects/<项目>/decisions/<日期>_<岗位>@<机器>_<标题>.md` | 文件名里那台机器 |
| `yizhan.json` 和根目录的其他文件 | 共用：改之前先打招呼，同步时加 `--allow-shared` |

`yizhan sync` 提交前会核对这条规矩，还会查疑似密钥和超大文件，查到就整批拦下。
原始数据不进账本，这里只放文字。
"""

CHARTER = """# {title}

{summary}

## 目标

## 分工原则

各岗位的职责写在各自的 `seats/<岗位>/profile.json` 里，`yizhan roster --project {name}` 能看到汇总。
这里只写分工原则，比如哪类事归哪个岗位、谁复核谁。

## 约定

- 定论写进 `decisions/`，一条一个文件，不改别人写的；有异议就另立一条，写明取代哪一条。
- 本文件由 {machine} 维护，别的机器要改，写信给它。
"""


class Ledger:
    def __init__(self, root, machine):
        self.root = Path(root).expanduser().resolve()
        self.machine = util.check_name(machine, "机器名")
        cfg = self.root / "yizhan.json"
        if not cfg.is_file():
            raise YizhanError(f"{self.root} 不是驿站账本：缺 yizhan.json")
        self.settings = dict(DEFAULTS)
        self.settings.update(util.read_json(cfg, {}) or {})
        self.tz = util.parse_offset(self.settings["timezone"])
        self.state_dir = util.state_home() / util.short_hash(util.norm_path(self.root))

    def lock(self):
        """A short lock around local writes; several sessions may share one machine."""
        return util.file_lock(self.state_dir / "ledger.lock")

    def local_date(self, t=None):
        return (t or util.utcnow()).astimezone(self.tz)

    def fmt_time(self, ts, fmt="%m-%d %H:%M"):
        t = util.parse_iso(ts)
        return t.astimezone(self.tz).strftime(fmt) if t else "?"

    # ---------- machines ----------

    def register_machine(self):
        path = self.root / "machines" / self.machine / "machine.json"
        with self.lock():
            old = util.read_json(path, {}) or {}
            data = {"machine": self.machine, "hostname": socket.gethostname(),
                    "platform": sys.platform, "joined": old.get("joined") or util.iso(util.utcnow())}
            util.write_json(path, data)
        return data

    def machines(self):
        base = self.root / "machines"
        out = []
        if base.is_dir():
            for d in sorted(base.iterdir()):
                data = util.read_json(d / "machine.json") if d.is_dir() else None
                if isinstance(data, dict):
                    out.append(data)
        return out

    # ---------- projects ----------

    def project_dir(self, name):
        util.check_name(name, "项目名")
        return self.root / "projects" / name

    def project(self, name):
        try:
            d = self.project_dir(name)
        except YizhanError:
            return None
        data = util.read_json(d / "project.json")
        return data if isinstance(data, dict) and data.get("name") == name else None

    def projects(self):
        base = self.root / "projects"
        if not base.is_dir():
            return []
        return [p for p in (self.project(d.name) for d in sorted(base.iterdir()) if d.is_dir()) if p]

    def add_project(self, name, title="", summary=""):
        d = self.project_dir(name)
        if (d / "project.json").exists():
            raise YizhanError(f"项目 {name} 已经有了")
        data = {"name": name, "title": title or name, "summary": summary,
                "owner_machine": self.machine, "created": util.iso(util.utcnow())}
        with self.lock():
            util.write_json(d / "project.json", data)
            util.write_text_atomic(d / "README.md", CHARTER.format(
                title=data["title"], name=name, machine=self.machine,
                summary=summary or "（写一两句：这个项目要做成什么。）"))
        return data

    # ---------- seats ----------

    def seat_dir(self, address):
        util.parse_address(address)
        return self.root / "seats" / address

    def _load_seat(self, d):
        prof = util.read_json(d / "profile.json")
        if not isinstance(prof, dict) or prof.get("address") != d.name:
            return None
        prof["presence"] = util.read_json(d / "presence.json", {}) or {}
        return prof

    def seats(self):
        base = self.root / "seats"
        if not base.is_dir():
            return []
        return [p for p in (self._load_seat(d) for d in sorted(base.iterdir()) if d.is_dir()) if p]

    def seat(self, address):
        d = self.seat_dir(address)
        return self._load_seat(d) if d.is_dir() else None

    def require_seat(self, address, local=False):
        _, machine = util.parse_address(address)
        prof = self.seat(address)
        if prof is None:
            raise YizhanError(f"驿站里还没有 {address} 这个岗位，先用 yizhan seat add 登记")
        if local and machine != self.machine:
            raise YizhanError(f"{address} 不在本机（本机是 {self.machine}），只能用本机岗位的身份写东西")
        return prof

    def resolve_seat(self, explicit=None, cwd=None, agent=None):
        """Who is acting: --as, then $YIZHAN_SEAT, then the local seat whose folder holds cwd."""
        address = explicit or os.environ.get("YIZHAN_SEAT")
        if address:
            return self.require_seat(address, local=True)
        cwd = cwd or os.getcwd()
        best, best_len, ties = None, -1, []
        for prof in self.seats():
            if prof.get("machine") != self.machine or (agent and prof.get("agent") != agent):
                continue
            for p in prof.get("paths", []):
                if util.is_within(cwd, p):
                    n = len(util.norm_path(p))
                    if n > best_len:
                        best, best_len, ties = prof, n, [prof]
                    elif n == best_len and prof["address"] != best["address"]:
                        ties.append(prof)
        if len(ties) > 1:
            names = "、".join(t["address"] for t in ties)
            raise YizhanError(f"这个文件夹同时登记给了 {names}，请用 --as 指明身份")
        return best

    def add_seat(self, name, agent, title="", duty="", projects=(), paths=(), owns=(),
                 wake=None, workdir=None, command=None):
        """Register or update a seat on this machine. Empty arguments keep old values."""
        util.check_name(name, "岗位名")
        if agent not in AGENT_KINDS:
            raise YizhanError(f"暂时只支持这几家 AI：{'、'.join(AGENT_KINDS)}")
        if wake is not None and wake not in WAKE_MODES:
            raise YizhanError(f"叫醒方式只能是 {'、'.join(WAKE_MODES)}")
        for p in projects:
            if self.project(p) is None:
                raise YizhanError(f"没有 {p} 这个项目，先用 yizhan project add 建")
        address = f"{name}@{self.machine}"
        path = self.seat_dir(address) / "profile.json"
        with self.lock():
            old = util.read_json(path, {}) or {}
            if old and old.get("agent") != agent:
                raise YizhanError(f"{address} 原来登记的是 {old.get('agent')}，不能改成 {agent}；换个岗位名")
            prof = dict(old)            # keep fields set elsewhere, such as autowake
            prof.update({
                "address": address, "name": name, "machine": self.machine, "agent": agent,
                "title": title or old.get("title", ""),
                "duty": duty or old.get("duty", ""),
                "projects": list(projects) or old.get("projects", []),
                "paths": [os.path.abspath(os.path.expanduser(p)) for p in paths] or old.get("paths", []),
                "owns": list(owns) or old.get("owns", []),
                "focus": old.get("focus", ""),
                "focus_at": old.get("focus_at"),
                "created": old.get("created") or util.iso(util.utcnow()),
            })
            for key, value in (("wake", wake), ("workdir", workdir), ("command", command)):
                if value is not None:
                    prof[key] = value
            util.write_json(path, prof)
        return prof

    def update_seat(self, address, **fields):
        """Set fields on a local seat's profile; a value of None removes the field."""
        self.require_seat(address, local=True)
        path = self.seat_dir(address) / "profile.json"
        with self.lock():
            prof = util.read_json(path, {}) or {}
            for key, value in fields.items():
                if value is None:
                    prof.pop(key, None)
                else:
                    prof[key] = value
            util.write_json(path, prof)
        return prof

    # ---------- who may write what ----------

    def owner_of(self, rel):
        """The machine allowed to write this ledger path, or SHARED."""
        parts = [p for p in rel.replace("\\", "/").split("/") if p]
        if len(parts) >= 3 and parts[0] == "seats":
            try:
                return util.parse_address(parts[1])[1]
            except YizhanError:
                return SHARED
        if len(parts) >= 3 and parts[0] == "machines":
            return parts[1] if util.NAME_RE.match(parts[1]) else SHARED
        if len(parts) >= 3 and parts[0] == "projects":
            if parts[2] == "decisions":
                m = DECISION_RE.match(parts[3]) if len(parts) == 4 else None
                return m.group(3) if m else SHARED
            return self._project_owner(parts[1]) or SHARED
        return SHARED

    def _project_owner(self, name):
        """Take the owner from the committed project.json, so nobody can claim a
        project by editing that file; a project not yet committed uses its working copy."""
        rel = f"projects/{name}/project.json"
        data = None
        text = gitio.show(self.root, f"HEAD:{rel}")
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        if data is None:
            try:
                data = util.read_json(self.root / rel)
            except ValueError:
                data = None
        return data.get("owner_machine") if isinstance(data, dict) else None


# ---------- opening and creating ----------

def local_config():
    cfg = util.read_json(util.config_home() / "local.json", {}) or {}
    return (os.environ.get("YIZHAN_LEDGER") or cfg.get("ledger"),
            os.environ.get("YIZHAN_MACHINE") or cfg.get("machine"))


def open_local(required=True):
    root, machine = local_config()
    if not root or not machine:
        if required:
            raise YizhanError("本机还没接入驿站。先运行：yizhan init <账本目录> --machine <本机名>")
        return None
    try:
        return Ledger(root, machine)
    except YizhanError:
        if required:
            raise
        return None


def save_local_config(root, machine):
    path = util.config_home() / "local.json"
    old = util.read_json(path, {}) or {}
    util.write_json(path, {"ledger": str(root), "machine": machine})
    return old if old and (old.get("ledger") != str(root) or old.get("machine") != machine) else None


def create_or_join(path, machine, timezone=None, remote=None):
    """Create a new ledger, or join an existing clone. Returns (ledger, created)."""
    util.check_name(machine, "机器名")
    root = Path(path).expanduser().resolve()
    created = False
    if not (root / "yizhan.json").is_file():
        root.mkdir(parents=True, exist_ok=True)
        if not gitio.is_repo(root):
            gitio.init(root)
        settings = dict(DEFAULTS)
        settings["timezone"] = timezone or util.system_offset()
        util.parse_offset(settings["timezone"])
        util.write_json(root / "yizhan.json", settings)
        util.write_text_atomic(root / ".gitattributes", "* text=auto eol=lf\n")
        util.write_text_atomic(root / ".gitignore", ".tmp-*\n.DS_Store\nThumbs.db\n")
        util.write_text_atomic(root / "README.md", LEDGER_README)
        created = True
    elif not gitio.is_repo(root):
        raise YizhanError(f"{root} 里有 yizhan.json，却不是 git 仓库")
    if remote and not gitio.has_remote(root):
        gitio.git(root, "remote", "add", "origin", remote)
    ledger = Ledger(root, machine)
    ledger.register_machine()
    return ledger, created
