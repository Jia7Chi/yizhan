"""`yizhan setup`: from nothing to working seats in about a minute.

A few questions, each with a default that Enter accepts. The wizard does what the
separate commands would do: create or join a ledger, register this machine, a project
and a seat for each AI found here, and install the hooks. Running it again is safe:
whatever is already set up is kept, and new folders or projects are added to it.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

from . import adapters, brief, gitio, i18n, service, sync, util
from . import ledger as ledger_mod
from .i18n import pick
from .ledger import AGENT_KINDS
from .util import YizhanError

# A starting split of the work; setup shows it as the default answer, so it is a
# suggestion the user accepts or replaces, and `yizhan seat add` changes it later.
ROLES = {
    "claude": (("主笔", "lead"), ("写代码和文档", "writes code and docs")),
    "codex": (("复核", "reviewer"), ("复核改动、补测试", "reviews changes, adds tests")),
}


def name_slug(text):
    """A name yizhan accepts (lower-case letters, digits, '-'), or '' if nothing is left."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40].strip("-")
    return s if util.NAME_RE.match(s) else ""


def default_ledger_path():
    return Path.home() / "yizhan-ledger"


def known_machines(root):
    """{machine name: hostname} for the machines registered in a ledger folder."""
    out = {}
    base = Path(root) / "machines"
    for f in sorted(base.glob("*/machine.json")) if base.is_dir() else []:
        try:
            data = util.read_json(f) or {}
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("machine"):
            out[data["machine"]] = data.get("hostname", "")
    return out


def default_machine(root=None):
    """mac, linux or win; if another computer already uses that name in this ledger,
    a name taken from this computer's host name."""
    base = {"darwin": "mac", "win32": "win"}.get(sys.platform, "linux")
    host = socket.gethostname()
    taken = known_machines(root) if root else {}
    for candidate in (base, name_slug(host.split(".")[0])):
        if candidate and taken.get(candidate, host) == host:
            return candidate
    n = 2
    while taken.get(f"{base}-{n}", host) != host:
        n += 1
    return f"{base}-{n}"


def detect_agents():
    """{agent: where it was found} for the AIs that look installed here."""
    found = {}
    claude = shutil.which("claude")
    if claude or adapters.claude_home().is_dir():
        found["claude"] = claude or str(adapters.claude_home())
    codex = adapters.find_codex()
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if codex or codex_home.is_dir():
        found["codex"] = codex or str(codex_home)
    return found


def _remove_clone(path, made_folder):
    """Delete a clone this run made: the folder itself if setup created it, otherwise only
    what the clone put into the empty folder. Git marks some files read-only, which
    Windows refuses to delete until they are made writable again."""
    def force(func, p, _info):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    handler = {"onexc": force} if sys.version_info >= (3, 12) else {"onerror": force}
    for item in ([path] if made_folder else list(path.iterdir())):
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item, **handler)
        else:
            item.unlink()


def remote_url(root):
    r = gitio.git(root, "remote", "get-url", "origin", check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def gh_command():
    """The gh command if it is installed and signed in, else None."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        r = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return gh if r.returncode == 0 else None


class Asker:
    """Asks questions with defaults. With --yes every default is taken without asking,
    and a question whose answer was given as an option is not asked at all."""

    def __init__(self, assume_yes=False, inp=None, out=None):
        self.yes = assume_yes
        self.inp = inp or input
        self.out = out or sys.stdout
        tty = bool(getattr(self.out, "isatty", lambda: False)())
        self.color = tty and not os.environ.get("NO_COLOR")

    def paint(self, text, code):
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def say(self, text=""):
        print(text, file=self.out, flush=True)

    def done(self, text):
        self.say(self.paint("✓ ", "32") + text)

    def warn(self, text):
        self.say(self.paint("! ", "33") + text)

    def heading(self, text):
        self.say()
        self.say(self.paint(text, "1;36"))

    def _read(self, prompt):
        try:
            return self.inp(prompt)
        except EOFError:
            raise YizhanError(pick("没有更多输入了。不方便回答问题时，加 --yes 全用默认值。",
                                   "No more input. To take every default without questions, add --yes.")) from None

    def text(self, question, default="", check=None, given=None):
        """An answer; `check` turns it into the value or raises YizhanError to ask again."""
        if given is not None:
            return check(given) if check else given
        while True:
            if self.yes:
                answer = default
            else:
                shown = f" [{default}]" if default else ""
                answer = self._read(f"{question}{shown} ").strip() or default
            try:
                return check(answer) if check else answer
            except YizhanError as e:
                if self.yes:
                    raise
                self.warn(str(e))

    def confirm(self, question, default, given=None):
        if given is not None:
            return given
        if self.yes:
            return default
        hint = "[Y/n]" if default else "[y/N]"
        while True:
            a = self._read(f"{question} {hint} ").strip().lower()
            if not a:
                return default
            if a in ("y", "yes", "是", "好", "要"):
                return True
            if a in ("n", "no", "否", "不", "不要"):
                return False

    def choose(self, question, options, default, given=None):
        """options: [(value, label)]. The answer may be the number or the value."""
        if given is not None:
            return given
        if self.yes:
            return default
        self.say(question)
        for i, (_, label) in enumerate(options, 1):
            self.say(f"  {i}) {label}")
        number = next(i for i, (v, _) in enumerate(options, 1) if v == default)
        while True:
            a = self._read(f"[{number}] ").strip().lower()
            if not a:
                return default
            if a.isdigit() and 1 <= int(a) <= len(options):
                return options[int(a) - 1][0]
            for value, _ in options:
                if a == value:
                    return value


# ---------- steps ----------

def _need_text(answer):
    if not answer.strip():
        raise YizhanError(pick("这一项不能空着", "This one can't be empty"))
    return answer.strip()


def _machine_check(ask, root):
    taken = known_machines(root)
    host = socket.gethostname()

    def check(answer):
        name = util.check_name(answer.strip(), pick("机器名", "machine name"))
        other = taken.get(name)
        if other and other != host:
            same = ask.confirm(pick(f"账本里已经有一台叫 {name} 的机器（主机名 {other}）。两台机器用同一个名字会互相覆盖。"
                                    "确定就是那一台吗？",
                                    f"The ledger already has a machine called {name} (host name {other}). Two machines "
                                    "with one name overwrite each other. Is this that same machine?"), False)
            if not same:
                raise YizhanError(pick("换一个名字吧", "Pick another name"))
        return name
    return check


def ledger_step(opts, ask):
    ask.heading(pick("1. 账本", "1. The ledger"))
    ask.say(pick("账本是一个 git 仓库，存着岗位、信件和定论。每台机器克隆一份，靠它互相同步。",
                 "The ledger is a git repository that holds seats, letters and decisions. Every machine "
                 "keeps a clone of it and syncs through it."))
    current = ledger_mod.open_local(required=False)
    if current and not opts.ledger and not opts.remote:
        url = remote_url(current.root)
        where = str(current.root) + (f" ({url})" if url else "")
        if ask.confirm(pick(f"本机已接入账本 {where}，本机名 {current.machine}。接着用它？",
                            f"This machine already uses the ledger {where} as {current.machine}. Keep using it?"), True):
            ask.done(pick(f"账本 {current.root}", f"ledger {current.root}"))
            return current, False
    mode = "join" if opts.remote else "new"
    if not opts.remote and not opts.ledger:
        mode = ask.choose(pick("这是第一台机器，还是要接入别的机器上建好的账本？",
                               "Is this the first machine, or are you joining a ledger made on another one?"),
                          [("new", pick("第一台：新建一本账本", "First machine: create a new ledger")),
                           ("join", pick("接入：从远端仓库克隆已有的账本", "Join: clone an existing ledger from its remote"))],
                          "new")
    remote = opts.remote
    if mode == "join" and not remote:
        remote = ask.text(pick("账本的远端仓库地址：", "The ledger's remote URL:"), "", check=_need_text)

    def path_check(answer):
        p = Path(answer.strip()).expanduser().resolve()
        if (p / "yizhan.json").is_file():
            return p                                      # a ledger already: join it
        # A new ledger may go into an empty git repository; a clone needs an empty folder.
        allowed = set() if mode == "join" else {".git"}
        if p.exists() and (not p.is_dir() or any(x.name not in allowed for x in p.iterdir())):
            raise YizhanError(pick(f"{p} 里已经有别的东西了，换一个空文件夹", f"{p} already holds other files; pick an empty folder"))
        return p
    path = ask.text(pick("账本放在哪个文件夹？", "Which folder should hold the ledger?"),
                    str(default_ledger_path()), check=path_check, given=opts.ledger)
    if mode == "join" and (path / "yizhan.json").is_file():
        here = remote_url(path)
        if here and remote and here != remote:
            ask.warn(pick(f"{path} 里已经有一本账本，它的远端是 {here}，不是 {remote}；接着用这一本。",
                          f"{path} already holds a ledger whose remote is {here}, not {remote}; using that one."))
    elif mode == "join":
        ask.say(pick(f"正在克隆 {remote} …", f"Cloning {remote} …"))
        made_folder = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        gitio.git(path.parent, "clone", "-q", remote, str(path), network=True, timeout=300)
        if not (path / "yizhan.json").is_file() and \
                any(x.name not in ledger_mod.IGNORABLE for x in path.iterdir()):
            # Neither a ledger nor empty: take the clone back, so the remote and this
            # machine stay exactly as they were.
            _remove_clone(path, made_folder)
            raise YizhanError(pick(f"{remote} 不是驿站账本：里面已经有别的文件。请换一个空仓库，或者另一台机器建好的账本。"
                                   "远端和本机都没改。",
                                   f"{remote} is not a yizhan ledger: it already holds other files. Use an empty "
                                   "repository, or a ledger another machine created. Nothing was changed here or there."))

    old_machine = ledger_mod.local_config()[1]
    default = old_machine if old_machine and known_machines(path).get(old_machine, socket.gethostname()) \
        == socket.gethostname() else default_machine(path)
    machine = ask.text(pick("给本机起个名字（小写字母、数字、连字符）：", "A name for this machine (lower-case letters, digits, '-'):"),
                       default, check=_machine_check(ask, path), given=opts.machine)
    led, created = ledger_mod.create_or_join(path, machine)
    ask.done(pick(f"{'新建了' if created else '接入了'}账本 {led.root}，本机名 {led.machine}",
                  f"{'created' if created else 'joined'} the ledger {led.root} as {led.machine}"))
    if not gitio.has_remote(led.root):
        _remote_step(opts, ask, led)
    return led, created


def _remote_step(opts, ask, led):
    """A new ledger needs a remote before a second machine can join it."""
    gh = None if ask.yes else gh_command()
    if gh and ask.confirm(pick(f"用 gh 在你的 GitHub 账号下新建一个私有仓库 {led.root.name}，当作账本的远端？",
                               f"Create a private GitHub repository {led.root.name} with gh, as the ledger's remote?"), False):
        r = subprocess.run([gh, "repo", "create", led.root.name, "--private", "--source", str(led.root),
                            "--remote", "origin"], capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            ask.done(pick(f"建好了远端 {remote_url(led.root)}", f"remote created: {remote_url(led.root)}"))
            return
        ask.warn(pick("gh 没建成：", "gh could not create it: ") + " ".join((r.stderr or r.stdout).split())[:300])
    url = ask.text(pick("远端仓库地址（别的机器靠它同步；只在本机用就直接回车）：",
                        "Remote URL (other machines sync through it; press Enter to stay on this machine):"), "")
    if url:
        gitio.git(led.root, "remote", "add", "origin", url)
        ask.done(pick(f"远端 {url}", f"remote {url}"))


def folder_step(opts, ask, led):
    ask.heading(pick("2. 项目", "2. The project"))

    def check(answer):
        p = Path(answer.strip() or ".").expanduser().resolve()
        if not p.is_dir():
            raise YizhanError(pick(f"{p} 不是一个文件夹", f"{p} is not a folder"))
        if util.is_within(p, led.root):
            raise YizhanError(pick("干活的文件夹不能放在账本里面", "The work folder can't be inside the ledger"))
        return p
    folder = ask.text(pick("AI 在哪个文件夹里干活？在这里打开的会话会自动认出自己的身份。",
                           "Which folder do the AIs work in? Sessions opened there know who they are."),
                      os.getcwd(), check=check, given=opts.folder)
    if util.norm_path(folder) == util.norm_path(Path.home()):
        ask.warn(pick("你选的是用户主目录：在它下面任何地方开的会话都会算进来。换成具体的项目文件夹更好。",
                      "That is your home folder: every session opened anywhere under it would count. "
                      "A specific project folder works better."))
    return folder


def project_step(opts, ask, led, folder):
    existing = led.projects()
    names = [p["name"] for p in existing]
    if existing:
        ask.say(pick("账本里已有的项目：", "Projects in the ledger: ")
                + pick("、", ", ").join(f"{p['name']} ({p.get('title') or p['name']})" for p in existing))
    default = names[0] if len(names) == 1 else (name_slug(folder.name) or "main")
    name = ask.text(pick("项目名（小写字母、数字、连字符；填已有的就加入它）：",
                         "Project name (lower-case letters, digits, '-'; an existing name joins it):"),
                    default, check=lambda a: util.check_name(a.strip(), pick("项目名", "project name")),
                    given=opts.project)
    if led.project(name):
        ask.done(pick(f"加入项目 {name}", f"joined the project {name}"))
        return name
    title = ask.text(pick("项目的全称（显示用）：", "Its full title (for display):"), folder.name, given=opts.title)
    summary = ask.text(pick("一句话：这个项目要做成什么？（可以留空）", "In one sentence, what is it for? (optional)"), "",
                       given=opts.summary)
    led.add_project(name, title, summary)
    ask.done(pick(f"建好了项目 {name}", f"created the project {name}"))
    return name


def seats_step(opts, ask, led, project, folder):
    ask.heading(pick("3. 岗位", "3. Seats"))
    ask.say(pick("本机的每个 AI 占一个岗位，地址是“名字@本机名”。",
                 "Each AI on this machine gets a seat, addressed name@machine."))
    found = detect_agents()
    seats = []
    for agent in AGENT_KINDS:
        display = adapters.get(agent).display
        if opts.agent:
            use = agent in opts.agent
        else:
            where = pick(f"（找到了：{found[agent]}）", f" (found: {found[agent]})") if agent in found \
                else pick("（本机没找到）", " (not found here)")
            use = ask.confirm(pick(f"接上 {display}{where}？", f"Connect {display}{where}?"), agent in found)
        if not use:
            continue
        address = f"{agent}@{led.machine}"
        old = led.seat(address) or {}
        (title_zh, title_en), (duty_zh, duty_en) = ROLES[agent]
        duty = ask.text(pick(f"{address} 负责什么？", f"What does {address} do?"), old.get("duty") or pick(duty_zh, duty_en))
        paths = list(dict.fromkeys([*old.get("paths", []), str(folder)]))
        projects = list(dict.fromkeys([*old.get("projects", []), project]))
        prof = led.add_seat(agent, agent, old.get("title") or pick(title_zh, title_en), duty, projects, paths)
        seats.append(prof)
        ask.done(pick(f"岗位 {address}（{display}）", f"seat {address} ({display})"))
    if not seats:
        ask.warn(pick("没有接上任何 AI。装好 Claude Code 或 Codex 以后，再跑一次 yizhan setup。",
                      "No AI connected. Once Claude Code or Codex is installed, run yizhan setup again."))
    return seats


def hooks_step(opts, ask, seats, folder):
    if not seats:
        return []
    ask.heading(pick("4. hook", "4. Hooks"))
    ask.say(pick("hook 让会话开场就收到简报，有来信时收到提醒，一轮结束时把信送出去。",
                 "Hooks give each session a brief when it starts and a notice when mail arrives, and they "
                 "send its letters when a turn ends."))
    plan = []
    for prof in seats:
        target = adapters.hook_target(prof["agent"], folder if prof["agent"] == "claude" else None)
        plan.append((prof["agent"], target))
        ask.say(f"  - {adapters.get(prof['agent']).display}: {target}")
    if opts.no_hooks or not ask.confirm(pick("把 hook 写进这些文件？原文件会先备份。",
                                             "Write the hooks into these files? Each one is backed up first."), True):
        ask.say(pick("先不写。之后可以运行：", "Skipped. Later, run:"))
        for agent, _ in plan:
            ask.say(f"  yizhan hooks install --agent {agent}" + (f" --folder {folder}" if agent == "claude" else ""))
        return []
    cmd = brief.cli_prefix()
    done = []
    for agent, target in plan:
        r = adapters.install_hooks(agent, target, cmd)
        done.append((agent, r))
        if not r["changed"]:
            ask.done(pick(f"{target} 里的 hook 已经是最新的", f"the hooks in {target} are already up to date"))
            continue
        ask.done(pick(f"写进了 {target}", f"wrote {target}") + (pick(f"（备份 {r['backup'].name}）", f" (backup {r['backup'].name})")
                                                              if r["backup"] else ""))
        if agent == "claude" and adapters.keep_out_of_git(target):
            ask.done(pick("这个文件只对本机有效，已让 git 忽略它（写在 .git/info/exclude）",
                          "that file only works on this machine, so git now ignores it (via .git/info/exclude)"))
    return done


def autowake_step(opts, ask, led, seats, folder):
    codex = [s for s in seats if s["agent"] == "codex"]
    if not codex:
        return
    seat = codex[0]["address"]
    ask.heading(pick("5. 自动叫醒（可选，默认关着）", "5. Autowake (optional, off by default)"))
    budget = led.settings["wake_budget_per_day"]
    ask.say(pick(f"打开以后，Codex 收到请求或答复会自己启动处理，处理完就退出。每次叫醒都用你账号的额度，每天最多 {budget} 次。",
                 f"When it is on, Codex starts by itself to handle a request or a reply, then exits. Each wake "
                 f"uses your account's quota, at most {budget} a day."))
    if not ask.confirm(pick("给 Codex 打开自动叫醒？", "Turn on autowake for Codex?"), False, given=True if opts.autowake else None):
        ask.say(pick(f"没打开。想开的时候运行：yizhan autowake on --as {seat}",
                     f"Left off. To turn it on later: yizhan autowake on --as {seat}"))
        return
    if not adapters.git_toplevel(folder):
        ask.warn(pick(f"被叫醒的 Codex 只在 git 仓库里干活，而 {folder} 不在 git 仓库里。"
                      f"先在那里 git init，再运行 yizhan autowake on --as {seat}",
                      f"A woken Codex only works inside a git repository, and {folder} is not in one. "
                      f"Run git init there, then yizhan autowake on --as {seat}"))
        return
    from . import cli                     # imported here: cli imports this module
    ns = argparse.Namespace(action="on", as_=seat, interval=service.DEFAULT_INTERVAL_S, budget=None,
                            yes=True, no_service=False)
    try:
        cli.cmd_autowake(ns)
    except YizhanError as e:
        ask.warn(pick("自动叫醒没打开：", "Autowake is still off: ") + str(e))


def summary(ask, led, project, folder, seats, rep):
    url = remote_url(led.root)
    ask.heading(pick("好了", "All set"))
    ask.say(f"  {pick('账本', 'ledger ')}  {led.root}" + (f"  ({url})" if url else pick("（只在本机，没有远端）", "  (this machine only)")))
    ask.say(f"  {pick('本机', 'machine')}  {led.machine}")
    ask.say(f"  {pick('项目', 'project')}  {project}  ({folder})")
    for s in seats:
        ask.say(f"  {pick('岗位', 'seat   ')}  {s['address']}  {adapters.get(s['agent']).display} · {s.get('duty', '')}")
    if rep.get("error"):
        ask.warn(pick("推送没成功，本地提交还在，下次同步再推：", "The push failed; the commit is kept and the next sync retries: ")
                 + rep["error"])
    steps = []
    if seats:
        apps = pick(" 或 ", " or ").join(dict.fromkeys(adapters.get(s["agent"]).display for s in seats))
        steps.append(pick(f"在 {folder} 里打开 {apps}：它开口之前就知道自己是谁、同事在做什么。",
                          f"Open {apps} in {folder}: before it says a word, it knows who it is and what "
                          "its colleagues are doing."))
    if any(s["agent"] == "codex" for s in seats):
        steps.append(pick("Codex 第一次打开时输入 /hooks，确认驿站的 hook，它们才会运行。",
                          "In Codex, type /hooks once and trust yizhan's hooks; until then they don't run."))
    reviewer = next((s["address"] for s in seats if s["agent"] == "codex"), None)
    if len(seats) >= 2 and reviewer:
        steps.append(pick(f"让它们互相写信，比如对 Claude 说：“请 {reviewer} 复核一下刚才的改动。”",
                          f"Let them write to each other. Tell Claude, for example: \"Ask {reviewer} to review this change.\""))
    if url:
        steps.append(pick(f"另一台机器：装好 yizhan，运行 yizhan setup，选“接入”，地址填 {url}",
                          f"On another machine: install yizhan, run yizhan setup, choose Join and give {url}"))
    else:
        steps.append(pick(f"要和别的机器协作，先给账本加远端：git -C {led.root} remote add origin <地址>，再运行 yizhan sync",
                          f"To work with other machines, give the ledger a remote: git -C {led.root} remote add origin "
                          "<url>, then yizhan sync"))
    steps.append(pick("全局一览：yizhan board --open    体检：yizhan doctor",
                      "See everything at once: yizhan board --open    Check the setup: yizhan doctor"))
    ask.heading(pick("接下来", "Next"))
    for i, s in enumerate(steps, 1):
        ask.say(f"  {i}. {s}")


def run(opts, ask=None):
    ask = ask or Asker(opts.yes)
    lang = opts.lang
    if not lang and not ask.yes:
        lang = ask.choose("Language / 语言:", [("zh", "中文"), ("en", "English")], i18n.lang())
    if lang:
        os.environ["YIZHAN_LANG"] = lang
        i18n.reset()
    ask.say(ask.paint(pick("驿站 · 设置向导", "yizhan setup"), "1"))
    ask.say(pick("每个问题都有默认值，直接回车就用它；随时可以 Ctrl+C 退出，再跑一次也不会重复登记。",
                 "Every question has a default: press Enter to take it. Ctrl+C quits at any time, and running "
                 "setup again never registers anything twice."))
    led, created = ledger_step(opts, ask)
    ledger_mod.save_local_config(led.root, led.machine, lang=lang)
    folder = folder_step(opts, ask, led)
    project = project_step(opts, ask, led, folder)
    seats = seats_step(opts, ask, led, project, folder)
    hooks_step(opts, ask, seats, folder)
    autowake_step(opts, ask, led, seats, folder)
    ask.say()
    ask.say(pick("同步账本 …", "Syncing the ledger …"))
    try:
        rep = sync.sync(led, quick=True, allow_shared=created)
    except YizhanError as e:              # e.g. a gate stopped the commit: setup itself is done
        rep = {"error": str(e)}
    summary(ask, led, project, folder, seats, rep)
    return 0
