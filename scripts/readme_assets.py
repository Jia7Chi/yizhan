"""Regenerate the pictures in the README from yizhan's own output.

    python scripts/readme_assets.py

writes docs/assets/demo-en.svg and demo-zh.svg (an animated terminal playing
`yizhan demo`), and builds a sample ledger: three machines, four seats, one
afternoon of work on a small web shop. Its board is written as HTML for both
languages, and the script prints where; screenshot those pages at 1280 px wide to
refresh docs/assets/board-en.png and board-zh.png.

The sample ledger is made with yizhan's normal functions on a scripted clock, so the
letters, receipts and decisions are exactly what yizhan would record. Nothing here
touches your own ledger or settings.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yizhan import board, demo, i18n, mail, records, util  # noqa: E402
from yizhan import ledger as ledger_mod  # noqa: E402

ASSETS = ROOT / "docs" / "assets"


# ---------- animated terminal ----------

COLORS = {"1": "#e6edf3", "2": "#8b949e", "32": "#3fb950", "33": "#d29922", "36": "#39c5cf", "1;36": "#56d4dd"}
SGR = re.compile(r"\033\[([0-9;]*)m")


def ansi_runs(line):
    """[(text, sgr code or None)] for one line of terminal output."""
    runs, code, pos = [], None, 0
    for m in SGR.finditer(line):
        if m.start() > pos:
            runs.append((line[pos:m.start()], code))
        code = None if m.group(1) in ("", "0") else m.group(1)
        pos = m.end()
    if pos < len(line):
        runs.append((line[pos:], code))
    return runs


def xml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def terminal_svg(text, title, columns):
    lines = text.rstrip("\n").split("\n")
    char_w, line_h, pad, bar = 7.83, 18, 18, 34
    width = int(columns * char_w + 2 * pad)
    height = bar + pad + (len(lines) + 1) * line_h + pad
    rows, delay = [], 0.3
    rows.append(f'<text class="l" x="{pad}" y="{bar + pad + 12}" style="animation-delay:{delay:.2f}s">'
                f'<tspan fill="#3fb950">$</tspan> yizhan demo</text>')
    for i, line in enumerate(lines, 1):
        plain = SGR.sub("", line)
        if re.match(r"^\[\d/\d\]", plain):
            delay += 1.1                      # a pause before each step, as in the real tour
        elif plain.startswith(("就这些", "That's all")):
            delay += 0.9
        else:
            delay += 0.045
        spans = "".join(f'<tspan fill="{COLORS.get(code, "#c9d1d9")}"'
                        + (' font-weight="700"' if code and code.startswith("1") else "")
                        + f">{xml(part)}</tspan>" for part, code in ansi_runs(line))
        y = bar + pad + 12 + i * line_h
        rows.append(f'<text class="l" x="{pad}" y="{y}" style="animation-delay:{delay:.2f}s">{spans}</text>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{xml(title)}">
<title>{xml(title)}</title>
<style>
text {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", "Noto Sans Mono CJK SC", monospace; font-size: 13px; fill: #c9d1d9; white-space: pre; }}
.l {{ opacity: 0; animation: show 0.01s linear forwards; }}
@keyframes show {{ to {{ opacity: 1; }} }}
@media (prefers-reduced-motion: reduce) {{ .l {{ animation: none; opacity: 1; }} }}
</style>
<rect width="{width}" height="{height}" rx="10" fill="#0d1117"/>
<rect width="{width}" height="{bar}" rx="10" fill="#161b22"/>
<rect y="{bar - 10}" width="{width}" height="10" fill="#161b22"/>
<circle cx="20" cy="17" r="6" fill="#ff5f57"/><circle cx="40" cy="17" r="6" fill="#febc2e"/><circle cx="60" cy="17" r="6" fill="#28c840"/>
<text x="{width / 2}" y="22" text-anchor="middle" style="fill:#8b949e;font-size:12px">{xml(title)}</text>
<g xml:space="preserve">
{chr(10).join(rows)}
</g>
</svg>
"""


def demo_text(lang, columns):
    os.environ["YIZHAN_LANG"] = lang
    os.environ["COLUMNS"] = str(columns)
    i18n.reset()
    out = io.StringIO()
    screen = demo.Screen(out=out, pause=0, color=True)
    screen.width = columns
    tmp = demo.run(screen, keep=False)
    # The demo prints its temporary folder; show a stable path instead.
    return out.getvalue().replace(str(tmp), "/tmp/yizhan-demo-7k2f")


# ---------- a sample ledger on a scripted clock ----------

class Clock:
    def __init__(self, start):
        self.t = start

    def at(self, t):
        self.t = t
        return self

    def __call__(self):
        return self.t


MACHINES = {"mac": ("darwin", "studio-mac"), "linux": ("linux", "build-server"), "win": ("win32", "win-desktop")}


def sample_ledger(base, lang):
    """One afternoon on a small web shop: Claude Code writes the payment code on a Mac,
    Codex reviews on a Linux server, a second Claude runs load tests there, and Codex on
    Windows looks after the installer."""
    os.environ["YIZHAN_LANG"] = lang
    i18n.reset()
    pick = i18n.pick
    now = util.utcnow().replace(microsecond=0)
    clock = Clock(now - dt.timedelta(hours=7))
    real_now = util.utcnow
    util.utcnow = clock
    try:
        root = base / f"shop-ledger-{lang}"
        mac, _ = ledger_mod.create_or_join(root, "mac", "+08:00")
        linux, win = ledger_mod.Ledger(root, "linux"), ledger_mod.Ledger(root, "win")
        for led in (linux, win):
            led.register_machine()
        for name, (plat, host) in MACHINES.items():
            path = root / "machines" / name / "machine.json"
            util.write_json(path, dict(util.read_json(path), platform=plat, hostname=host))

        def h(hours, minutes=0):
            return clock.at(now - dt.timedelta(hours=hours, minutes=minutes))

        mac.add_project("shop", pick("网店后端", "Web shop"), pick("下单、支付、退款", "orders, payments, refunds"))
        mac.add_seat("claude", "claude", pick("主笔", "lead"), pick("下单和支付流程", "orders and the payment flow"),
                     ["shop"], [str(base / "shop")], ["src/**"])
        linux.add_seat("codex", "codex", pick("复核", "reviewer"), pick("复核每次改动，补测试", "reviews every change, writes tests"),
                       ["shop"], [str(base / "shop")], ["tests/**"])
        linux.update_seat("codex@linux", autowake=True, wake="exec")
        linux.add_seat("claude", "claude", pick("运维", "ops"), pick("压测和部署脚本", "load tests and deploy scripts"),
                       ["shop"], [str(base / "shop-ops")], ["deploy/**"])
        win.add_seat("codex", "codex", "Windows", pick("Windows 构建和安装包", "Windows build and installer"),
                     ["shop"], [str(base / "shop-win")], ["installer/**"])

        h(6, 40)
        records.decide(linux, "claude@linux", "shop", pick("支付回调失败重试 3 次", "Retry failed payment webhooks 3 times"),
                       pick("先用固定次数，压测后再定。", "A fixed count for now; revisit after the load test."))
        h(6, 10)
        req = mail.send(mac, "claude@mac", ["codex@linux"], "request",
                        pick("请复核退款接口", "Review the refund endpoint"),
                        pick("分支 refund-endpoint。主要担心支付方重发回调时重复退款。测试在 tests/test_refund.py。",
                             "Branch refund-endpoint. My worry: a payment provider that retries its webhook could "
                             "trigger a second refund. Tests are in tests/test_refund.py."), project="shop")
        mail.touch(mac, "claude@mac", seal=True)
        h(6, 2)
        mail.ack(linux, "codex@linux", req["id"], "working", pick("开始复核", "on it"))
        h(5, 25)
        rep = mail.send(linux, "codex@linux", [], "reply", pick("退款接口：两处问题，补了 9 个测试", "Refund endpoint: 2 issues, 9 new tests"),
                        pick("1. [P1] 回调重发会退两次款：缺幂等键。\n2. [P2] 金额用了浮点数，0.1 + 0.2 会出错。\n"
                             "新增 9 个测试，其中 2 个在修好之前会失败。",
                             "1. [P1] A retried webhook refunds twice: there is no idempotency key.\n"
                             "2. [P2] Amounts are floats, so 0.1 + 0.2 goes wrong.\n"
                             "Added 9 tests; 2 of them fail until these are fixed."), reply_to=req["id"])
        mail.ack(linux, "codex@linux", req["id"], "done", pick("见回信", "see my reply"))
        records.log(linux, "codex@linux", pick("复核退款接口：2 处问题", "Reviewed the refund endpoint: 2 issues"))
        mail.touch(linux, "codex@linux", seal=True)
        h(5, 5)
        mail.ack(mac, "claude@mac", rep["id"], "working", pick("两处都在修", "fixing both"))
        h(4, 30)
        records.decide(mac, "claude@mac", "shop", pick("退款按订单幂等：每单一个幂等键", "Refunds are idempotent: one key per order"),
                       pick("Codex 复核发现回调重发会重复退款。", "Codex found that a retried webhook refunds twice."))
        records.decide(mac, "claude@mac", "shop", pick("金额一律用整数分存储", "Store money as integer cents"),
                       pick("浮点数在 0.1 + 0.2 上出错，见复核回信。", "Floats break on 0.1 + 0.2; see the review."))
        records.log(mac, "claude@mac", pick("修好幂等键和金额精度", "Fixed the idempotency key and money precision"))
        fix = mail.send(mac, "claude@mac", [], "reply", pick("两处已修，请再看一遍", "Both fixed, please re-check"),
                        pick("幂等键按订单号生成；金额改成整数分。", "Idempotency key per order id; money in integer cents."),
                        reply_to=rep["id"])
        mail.ack(mac, "claude@mac", rep["id"], "done", pick("已修", "fixed"))
        mail.touch(mac, "claude@mac", seal=True)
        h(4, 0)
        mail.send(linux, "codex@linux", [], "reply", pick("复核通过：47 个测试全过", "Re-checked: all 47 tests pass"),
                  pick("两处修复都成立。重试策略建议改成退避，另记一条定论。",
                       "Both fixes hold. I suggest backoff for retries; recorded as a decision."), reply_to=fix["id"])
        mail.ack(linux, "codex@linux", fix["id"], "done", pick("通过", "approved"))
        old = records.decisions(linux, "shop")[-1]["id"]
        records.decide(linux, "codex@linux", "shop", pick("回调重试改为指数退避，1 小时后放弃", "Retry webhooks with backoff; give up after 1 hour"),
                       pick("固定 3 次在支付方短暂故障时不够。", "Three fixed retries are not enough when the provider has a short outage."),
                       supersedes=[old])
        records.log(linux, "codex@linux", pick("复核修复：通过", "Re-checked the fixes: approved"))
        mail.touch(linux, "codex@linux", seal=True)
        h(3, 10)
        ask = mail.send(mac, "claude@mac", ["codex@win"], "request",
                        pick("改了数据目录以后，安装包还能用吗？", "Does the installer still work after the data folder move?"),
                        pick("数据目录从 %APPDATA%\\shop 挪到了 %LOCALAPPDATA%\\shop。", "The data folder moved from %APPDATA%\\shop to %LOCALAPPDATA%\\shop."),
                        project="shop")
        mail.touch(mac, "claude@mac", seal=True)
        h(2, 45)
        mail.ack(win, "codex@win", ask["id"], "working", pick("在打安装包", "building the installer"))
        h(1, 40)
        mail.send(win, "codex@win", [], "reply", pick("Windows 11 上安装正常，旧数据会自动迁移", "Installer works on Windows 11; old data migrates"),
                  pick("另有一个警告：卸载时没删日志目录，已提 issue。", "One warning: uninstall leaves the log folder; filed an issue."),
                  reply_to=ask["id"])
        mail.ack(win, "codex@win", ask["id"], "done", pick("见回信", "see my reply"))
        records.log(win, "codex@win", pick("安装包在 Windows 11 上验证通过", "Verified the installer on Windows 11"))
        records.set_focus(win, "codex@win", pick("卸载时清理日志目录", "Cleaning up the log folder on uninstall"))
        mail.touch(win, "codex@win", seal=True)
        h(1, 5)
        update = mail.send(linux, "claude@linux", ["project:shop"], "update",
                           pick("压测：下单 500 rps，p95 180 ms", "Load test: checkout holds 500 rps, p95 180 ms"),
                           pick("数据库连接池 40 足够；报告在 deploy/reports/load-1030.md。",
                                "A pool of 40 database connections is enough; report in deploy/reports/load-1030.md."),
                           project="shop")
        records.log(linux, "claude@linux", pick("压测下单接口", "Load-tested checkout"))
        records.set_focus(linux, "claude@linux", pick("写部署脚本", "Writing the deploy script"))
        mail.touch(linux, "claude@linux", seal=True)
        h(0, 50)
        mail.ack(mac, "claude@mac", update["id"], "received", pick("好的", "noted"))
        h(0, 35)
        records.set_focus(linux, "codex@linux", pick("等收据邮件的改动", "Waiting for the receipt e-mails change"))
        mail.touch(linux, "codex@linux")
        h(0, 12)
        mail.send(mac, "claude@mac", ["codex@linux"], "request", pick("请复核收据邮件", "Review the receipt e-mails"),
                  pick("新增退款收据模板，见 src/mail/receipt.py。", "New refund receipt template in src/mail/receipt.py."),
                  project="shop")
        records.set_focus(mac, "claude@mac", pick("退款收据邮件", "Refund receipt e-mails"))
        h(0, 2)
        mail.touch(mac, "claude@mac")
        return mac
    finally:
        util.utcnow = real_now


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="yizhan-assets-"))
    saved = {k: os.environ.get(k) for k in ("YIZHAN_HOME", "YIZHAN_STATE", "YIZHAN_LANG", "COLUMNS")}
    os.environ.update(YIZHAN_HOME=str(tmp / "config"), YIZHAN_STATE=str(tmp / "state"))
    try:
        for lang, title in (("en", "yizhan demo"), ("zh", "yizhan demo · 驿站")):
            columns = 92
            svg = terminal_svg(demo_text(lang, columns), title, columns)
            (ASSETS / f"demo-{lang}.svg").write_text(svg, encoding="utf-8")
            print("wrote", ASSETS / f"demo-{lang}.svg")
        for lang in ("en", "zh"):
            led = sample_ledger(tmp, lang)
            path = board.write(led, tmp / f"board-{lang}.html")
            # Show the sample where a real ledger would live, not in a temporary folder.
            page = path.read_text(encoding="utf-8")
            page = page.replace(str(led.root), "~/yizhan-ledger").replace(led.root.name, "yizhan-ledger")
            path.write_text(page, encoding="utf-8")
            print("board", path)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    main()
