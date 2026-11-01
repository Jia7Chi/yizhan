"""`yizhan demo`: two machines, two AIs, one ledger, in about ten seconds.

Everything runs in a temporary folder with its own settings: your real ledger, your
config and your git identity are never touched, and no AI is called. The ledger is
left behind so you can look around afterwards.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unicodedata
import webbrowser
from pathlib import Path

from . import adapters, brief, gitio, mail, records, sync, util
from . import ledger as ledger_mod
from .i18n import pick


def display_width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def wrap(text, width):
    """Split one line into pieces no wider than `width` columns (Chinese counts double).
    English breaks at a space; Chinese may break between any two characters."""
    out = []
    while display_width(text) > width:
        used, cut = 0, 0
        for c in text:
            if used + display_width(c) > width:
                break
            used += display_width(c)
            cut += 1
        space = text.rfind(" ", 0, cut + 1)
        if space > cut // 2 and display_width(text[cut - 1]) == 1:
            out.append(text[:space].rstrip())
            text = text[space + 1:]
        else:
            out.append(text[:cut])
            text = text[cut:]
    out.append(text)
    return out


class Screen:
    """Prints the tour: colours and short pauses only when a person is watching."""

    def __init__(self, out=None, pause=None, color=None):
        self.out = out or sys.stdout
        tty = bool(getattr(self.out, "isatty", lambda: False)())
        self.color = (tty and not os.environ.get("NO_COLOR")) if color is None else color
        self.pause = (0.7 if tty else 0.0) if pause is None else pause
        self.width = max(60, min(100, shutil.get_terminal_size((90, 24)).columns))

    def paint(self, text, code):
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def line(self, text=""):
        self.out.write(text + "\n")
        self.out.flush()

    def step(self, n, total, title):
        if self.pause:
            time.sleep(self.pause)
        self.line()
        self.line(self.paint(f"[{n}/{total}] {title}", "1;36"))

    def item(self, text):
        self.line("    " + text)

    def note(self, text):
        for piece in wrap(text, self.width - 4):
            self.line("    " + self.paint(piece, "2"))

    def box(self, text):
        inner = self.width - 8
        self.line("    " + self.paint("┌" + "─" * (inner + 2), "33"))
        for raw in text.splitlines():
            for piece in wrap(raw, inner) if raw else [""]:
                self.line("    " + self.paint("│ ", "33") + piece)
        self.line("    " + self.paint("└" + "─" * (inner + 2), "33"))


def run(screen=None, keep=True):
    """Play the tour; return the temporary folder that holds the demo ledgers."""
    sc = screen or Screen()
    tmp = Path(tempfile.mkdtemp(prefix="yizhan-demo-")).resolve()
    saved = os.environ.copy()
    empty_git_config = tmp / "empty.gitconfig"
    empty_git_config.write_text("", encoding="utf-8")
    # The tour sees none of the caller's git or yizhan setup: no inherited repository
    # (GIT_DIR and friends), no injected or global config, no ledger or seat.
    for k in [k for k in os.environ if k.startswith("GIT_") or k in ("YIZHAN_LEDGER", "YIZHAN_MACHINE", "YIZHAN_SEAT")]:
        del os.environ[k]
    os.environ.update(YIZHAN_HOME=str(tmp / "config"), YIZHAN_STATE=str(tmp / "state"),
                      GIT_CONFIG_GLOBAL=str(empty_git_config), GIT_CONFIG_NOSYSTEM="1")
    try:
        _play(sc, tmp)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        if not keep:
            util.remove_tree(tmp)
    if keep:
        offer_board(sc, tmp)
    return tmp


def offer_board(sc, tmp):
    """When a person is watching, end the tour on the board of the Mac's demo ledger."""
    if not (sys.stdin.isatty() and getattr(sc.out, "isatty", lambda: False)()):
        return
    sc.line()
    try:
        answer = input(pick("在浏览器里打开演示账本的看板？[Y/n] ", "Open the demo ledger's board in your browser? [Y/n] "))
    except (EOFError, KeyboardInterrupt):
        sc.line()
        return
    if answer.strip().lower() not in ("", "y", "yes", "是", "好"):
        return
    from . import board                   # imported here: only this step needs it
    path = board.write(ledger_mod.Ledger(tmp / "mac", "mac"), tmp / "board.html")
    webbrowser.open(path.as_uri())
    sc.line(pick("看板：", "Board: ") + str(path))


def _sync(*ledgers):
    for led in ledgers:
        sync.sync(led, quick=True)


def _play(sc, tmp):
    total = 5
    sc.line(sc.paint(pick("驿站 · 十秒演示", "yizhan · a ten-second tour"), "1"))
    sc.note(pick("全程在临时文件夹里进行：不碰你的账本和配置，也不调用任何 AI。",
                 "Everything happens in a temporary folder: your own ledger and settings stay untouched, "
                 "and no AI is called."))

    # 1. Two machines join one ledger.
    sc.step(1, total, pick("两台机器、两家 AI、一本账", "Two machines, two AIs, one ledger"))
    bare = tmp / "remote.git"
    gitio.git(tmp, "init", "-q", "--bare", "-b", "main", str(bare))
    mac, _ = ledger_mod.create_or_join(tmp / "mac", "mac", util.system_offset(), str(bare))
    sync.sync(mac, quick=True, allow_shared=True)
    gitio.git(tmp, "clone", "-q", str(bare), str(tmp / "linux"))
    linux, _ = ledger_mod.create_or_join(tmp / "linux", "linux")
    # Both "machines" live on this computer; give the simulated one its own identity.
    util.write_json(linux.root / "machines" / "linux" / "machine.json",
                    dict(util.read_json(linux.root / "machines" / "linux" / "machine.json"),
                         platform="linux", hostname="linux-box"))
    for d in ("mac-paper", "linux-review"):
        (tmp / d).mkdir()
    mac.add_project("paper", title=pick("期刊论文", "Journal paper"),
                    summary=pick("一篇控制方向的论文", "a control-theory paper"))
    mac.add_seat("claude", "claude", title=pick("论文主笔", "writes the paper"),
                 duty=pick("正文与推导", "text and derivations"), projects=["paper"],
                 paths=[str(tmp / "mac-paper")])
    _sync(mac, linux)
    linux.add_seat("codex", "codex", title=pick("推导复核", "checks the math"),
                   duty=pick("复核推导、补测试", "reviews proofs, adds tests"), projects=["paper"],
                   paths=[str(tmp / "linux-review")])
    records.set_focus(linux, "codex@linux", pick("等着复核", "waiting for something to check"))
    _sync(linux, mac)
    sc.item("mac    claude@mac    Claude Code   " + pick("论文主笔", "writes the paper"))
    sc.item("linux  codex@linux   Codex         " + pick("推导复核", "checks the math"))
    sc.note(pick("账本就是一个 git 仓库，两台机器各克隆一份。每个文件只有一台机器写，所以永远不会冲突。",
                 "The ledger is one git repository that both machines clone. Every file has exactly one "
                 "writing machine, so merges never conflict."))

    # 2. Claude asks for a review.
    sc.step(2, total, pick("Mac 上的 Claude 请 Codex 复核", "Claude on the Mac asks Codex for a review"))
    subject = pick("请复核第三节的稳定性证明", "Please check the stability proof in section 3")
    letter = mail.send(mac, "claude@mac", ["codex@linux"], "request", subject,
                       pick("第二步给李雅普诺夫函数的导数定了一个上界，我不确定常数取得对不对。",
                            "Step 2 bounds the derivative of the Lyapunov function; I'm not sure the constant is right."),
                       project="paper")
    mail.touch(mac, "claude@mac", seal=True)        # what the Stop hook does when the turn ends
    sync.sync(mac, quick=True)
    sc.item(f"claude@mac → codex@linux   [{mail.kind_label('request')}] {subject}")
    sc.note(pick("Claude 这一轮一结束，信就算写完了，随即推送出去。",
                 "When Claude's turn ends the letter is final, and it is pushed right away."))

    # 3. Codex opens a session on Linux and gets the brief.
    sc.step(3, total, pick("Linux 上开了一个 Codex 会话，它开口之前就看到：",
                           "A Codex session opens on Linux. Before it types a word, it sees:"))
    sync.pull_only(linux)
    text = brief.session_brief(linux, linux.seat("codex@linux"), adapters.get("codex").context_budget,
                               session="demo")
    shown = [ln for ln in text.splitlines()
             if not ln.startswith(("下文的 `yizhan`", "Below, `yizhan` means"))]
    sc.box("\n".join(shown))

    # 4. Codex answers, records a decision and writes its journal.
    sc.step(4, total, pick("Codex 回信、记下定论、写日志", "Codex replies, records a decision, writes its journal"))
    reply_subject = pick("第三节：第二步的常数要改", "Section 3: the constant in step 2 needs fixing")
    mail.send(linux, "codex@linux", [], "reply", reply_subject,
              pick("上界里漏了因子 2，改成 2c 以后结论照样成立。", "The bound misses a factor of 2; with 2c the result still holds."),
              reply_to=letter["id"])
    mail.ack(linux, "codex@linux", letter["id"], "done", pick("复核完了，见回信", "checked, see my reply"))
    decision = pick("第三节的上界用 2c", "Use 2c as the bound in section 3")
    records.decide(linux, "codex@linux", "paper", decision,
                   pick("复核时发现原推导漏了因子 2。", "A factor of 2 was missing in the original derivation."))
    records.log(linux, "codex@linux", pick("第三节复核完成", "Finished checking section 3"))
    records.set_focus(linux, "codex@linux", pick("等第四节", "waiting for section 4"))
    mail.touch(linux, "codex@linux", seal=True)
    sync.sync(linux, quick=True)
    sc.item(f"codex@linux → claude@mac   [{mail.kind_label('reply')}] {reply_subject}")
    sc.item(pick(f"定论：{decision}（已确认）", f"decision: {decision} (confirmed)"))

    # 5. Back on the Mac.
    sc.step(5, total, pick("回到 Mac：Claude 下一次被调用时，自动收到：",
                           "Back on the Mac, Claude's next prompt comes with:"))
    sync.pull_only(mac)
    notice = brief.new_mail_notice(mac, mac.seat("claude@mac"), "demo", adapters.get("claude").context_budget)
    sc.box((notice or "").replace(brief.cli_prefix(), "yizhan"))

    commits = gitio.git(mac.root, "log", "--format=%s").stdout.splitlines()
    sc.line()
    sc.line(sc.paint(pick("就这些。没有服务器，全是普通的 git 提交：", "That's all. No server, only plain git commits:"), "1"))
    for c in commits[:6]:
        sc.item(c)
    sc.line()
    # Paths and commands stay on one line each, so they can be copied.
    sc.line(pick("演示账本：", "Demo ledgers: ") + str(tmp))
    sc.line(pick("看看板：", "See the board: ") + f"yizhan board --ledger {tmp / 'mac'} --machine mac --open")
    sc.line(pick("接上你自己的 Claude Code 和 Codex：", "Connect your own Claude Code and Codex: ") + "yizhan setup")
