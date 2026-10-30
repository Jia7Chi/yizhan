"""Command line: yizhan <command> ..."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__, adapters, brief, gitio, hooks, mail, records, service, state, sync, util, wake
from . import ledger as ledger_mod
from .ledger import AGENT_KINDS, WAKE_MODES
from .util import YizhanError


# ---------- helpers ----------

def _body(ns):
    if getattr(ns, "body", None) is not None:
        return ns.body
    if getattr(ns, "body_file", None):
        if ns.body_file == "-":
            return sys.stdin.read()
        return Path(ns.body_file).expanduser().read_text(encoding="utf-8")
    return ""


def _me(led, ns, agent=None):
    prof = led.resolve_seat(explicit=getattr(ns, "as_", None), agent=agent)
    if prof is None:
        raise YizhanError("认不出你是哪个岗位：用 --as 名字@机器 指明，或者在登记过的项目文件夹里运行")
    return prof["address"]


def _print_sync(rep):
    print(f"已提交 {rep['committed'][:8]}" if rep.get("committed") else "没有新改动要提交")
    if rep.get("pulled"):
        print(rep["pulled"])
    if rep.get("error"):
        print("推送没成功，本地提交还在，下次同步再推：" + rep["error"])
    elif rep.get("pushed"):
        print("远端已是最新")
    for n in rep.get("notes", []):
        print("提示：" + n)


# ---------- commands ----------

def cmd_init(ns):
    led, created = ledger_mod.create_or_join(ns.dir, ns.machine, ns.timezone, ns.remote)
    old = ledger_mod.save_local_config(led.root, led.machine)
    rep = sync.sync(led, quick=True, allow_shared=created, push=not ns.no_push)
    print(("新建了驿站账本：" if created else "接入了已有的账本：") + str(led.root))
    print(f"本机名：{led.machine}；时区：{led.settings['timezone']}")
    if old:
        print(f"注意：本机原来接的是 {old.get('ledger')}（{old.get('machine')}），已改成这一个")
    _print_sync(rep)
    print("下一步：yizhan project add <项目名> --title …，再用 yizhan seat add 登记本机的岗位")


def cmd_project_add(ns):
    led = ledger_mod.open_local()
    p = led.add_project(ns.name, ns.title, ns.summary)
    print(f"建好了项目 {p['name']}（{p['title']}），章程在 {led.project_dir(p['name']) / 'README.md'}")


def cmd_project_list(ns):
    led = ledger_mod.open_local()
    seats = led.seats()
    projects = led.projects()
    if not projects:
        print("（还没有项目）")
    for p in projects:
        n = sum(1 for s in seats if p["name"] in s.get("projects", []))
        summary = f"：{p['summary']}" if p.get("summary") else ""
        print(f"{p['name']}　{p.get('title', '')}{summary}（{n} 个岗位，主管机器 {p.get('owner_machine')}）")


def cmd_seat_add(ns):
    led = ledger_mod.open_local()
    prof = led.add_seat(ns.name, ns.agent, ns.title, ns.duty, ns.project, ns.path, ns.owns,
                        ns.wake, ns.workdir, ns.command)
    print(f"登记好了岗位 {prof['address']}（{prof['agent']}）")
    if prof["paths"]:
        print("在这些文件夹里开的会话都算这个岗位：" + "；".join(prof["paths"]))
    else:
        print("还没给它指定文件夹：hook 认不出它，会话里要用 --as 指明身份。可以用 --path 补上")
    print(f"接上 hook：yizhan hooks --agent {prof['agent']}")


def cmd_roster(ns):
    led = ledger_mod.open_local()
    if ns.project:
        lines = brief.roster_lines(led, [ns.project])
        print("\n".join(lines) if lines else "（这个项目还没有岗位）")
        return
    seats = led.seats()
    if not seats:
        print("（还没有岗位）")
        return
    for p in led.projects():
        lines = brief.roster_lines(led, [p["name"]])
        if lines:
            print(f"【{p['name']}】{p.get('title', '')}")
            print("\n".join(lines))
    loose = [s["address"] for s in seats if not s.get("projects")]
    if loose:
        print("【没归进项目的岗位】" + "、".join(loose))


def cmd_whoami(ns):
    led = ledger_mod.open_local()
    prof = led.resolve_seat(explicit=ns.as_, agent=ns.agent)
    if prof is None:
        print(f"这个文件夹没登记给本机（{led.machine}）的任何岗位")
        return 1
    print(f"{prof['address']}（{prof.get('title') or prof['agent']}）")
    return 0


def cmd_focus(ns):
    led = ledger_mod.open_local()
    prof = records.set_focus(led, _me(led, ns), ns.text)
    print(f"已更新 {prof['address']} 手上在做的事：{prof['focus']}")


def cmd_send(ns):
    led = ledger_mod.open_local()
    meta = mail.send(led, _me(led, ns), ns.to, ns.kind, ns.subject, _body(ns), ns.reply_to,
                     ns.key, ns.artifact, ns.wake, ns.now, ns.project)
    if not meta["wake"]:
        how = "不叫醒对方，对方下次查信时看"
    else:
        auto = [t for t in meta["to"] if (led.seat(t) or {}).get("autowake")]
        if not auto:
            how = "待办；对方没开自动叫醒，下次开会话时看到"
        elif len(auto) == len(meta["to"]):
            how = "待办；对方开着自动叫醒，信写完就会自己启动处理"
        else:
            how = f"待办；{'、'.join(auto)} 开着自动叫醒，其余的下次开会话时看到"
    print(f"已发 {meta['id']} → {'、'.join(meta['to'])}（{how}）")
    if any(t.split("@", 1)[1] != led.machine for t in meta["to"]):
        print("收信方里有别的机器上的岗位：AI 会话这一轮结束时会自动推送；不在会话里发的，跑一下 yizhan sync")


def cmd_inbox(ns):
    led = ledger_mod.open_local()
    items = mail.inbox(led, _me(led, ns), include_closed=ns.all)
    if not items:
        print("（没有待处理的信）")
    for it in items:
        print(mail.line(led, it))


def cmd_show(ns):
    led = ledger_mod.open_local()
    meta, body = mail.read(mail.find(led, ns.id))
    if ns.json:
        print(json.dumps({"meta": meta, "body": body}, ensure_ascii=False, indent=2))
    else:
        print(mail.show_text(led, meta, body))


def cmd_ack(ns):
    led = ledger_mod.open_local()
    rec = mail.ack(led, _me(led, ns), ns.id, ns.status, ns.note)
    print(f"已登记 {rec['id']}：{mail.STATUS_ZH[rec['status']]}")


def cmd_decide(ns):
    led = ledger_mod.open_local()
    me = _me(led, ns)
    if ns.update:
        meta = records.set_status(led, me, ns.update, ns.status or "confirmed", ns.note or "")
        print(f"已把 {meta['id']} 改成{records.DECISION_ZH[meta['status']]}")
        return
    if not ns.project or not ns.title:
        raise YizhanError("新定论要有 --project 和 --title")
    meta = records.decide(led, me, ns.project, ns.title, _body(ns), ns.status or "confirmed",
                          ns.ref, ns.supersedes)
    print(f"记下了定论 {meta['id']}")


def cmd_decisions(ns):
    led = ledger_mod.open_local()
    items = records.decisions(led, ns.project)
    if not items:
        print("（还没有定论）")
    for d in items:
        status = records.DECISION_ZH.get(d.get("status"), d.get("status"))
        if d.get("superseded_by"):
            status += f"，已被 {d['superseded_by']} 取代"
        print(f"{d['id']}　[{d['project']}] {d['title']}（{d['by']}，{status}）")


def cmd_log(ns):
    led = ledger_mod.open_local()
    path = records.log(led, _me(led, ns), ns.title, _body(ns))
    print(f"记进了 {path}")


def cmd_sync(ns):
    led = ledger_mod.open_local()
    rep = sync.sync(led, quick=ns.quick, allow_shared=ns.allow_shared, push=not ns.no_push)
    _print_sync(rep)
    return 1 if rep.get("error") else 0


def cmd_brief(ns):
    led = ledger_mod.open_local()
    prof = led.require_seat(_me(led, ns))
    budget = adapters.get(ns.agent or prof["agent"]).context_budget
    print(brief.session_brief(led, prof, budget, remember=False))


def cmd_hook(ns):
    return hooks.run(ns.agent, event=ns.event)


def cmd_hooks(ns):
    cmd = brief.cli_prefix()
    print(f"把下面的 \"hooks\" 合并进 {adapters.hook_target(ns.agent)}。已有的 hook 留着，同一事件下并列加一条：")
    print(adapters.dumps(adapters.hook_config(ns.agent, cmd)))
    if ns.agent == "codex":
        print("Codex 要先审核才会运行新 hook：打开 Codex 输入 /hooks，确认这几条。")
    else:
        print("Claude Code 下次开会话时生效。Windows 上 hook 默认经 Git Bash 执行。")
    print("命令用的是绝对路径：从程序坞打开的应用跑 hook 时，不一定带着终端里的 PATH。")


def cmd_wake(ns):
    led = ledger_mod.open_local()
    me = _me(led, ns)
    if ns.watch:
        wake.watch(led, me, ns.interval, ns.dry_run)
        return 0
    r = wake.wake_once(led, me, dry_run=ns.dry_run)
    for n in r["notes"]:
        print("提示：" + n)
    print(wake.describe(r))
    return 0


def _service_plan(led, prof):
    """The installed job, found by its name alone: checking or removing it needs neither the
    interval nor the command line, so neither can block `off`, `status` or `doctor`."""
    return service.handle(led, prof["address"])


def cmd_autowake(ns):
    led = ledger_mod.open_local()
    if ns.action == "status":
        mine = [s for s in led.seats() if s["machine"] == led.machine]
        if not mine:
            print("（本机还没有岗位）")
        today = led.local_date().date().isoformat()
        for s in mine:
            if not s.get("autowake"):
                print(f"- {s['address']}：自动叫醒关着，信只排队，等有人开会话")
                continue
            st = state.load(led, f"seat-{s['address']}")
            used = int(st.get("wakes", {}).get(today, 0))
            budget = int(s.get("wake_budget_per_day") or led.settings["wake_budget_per_day"])
            job = ("后台任务在跑" if service.installed(_service_plan(led, s))
                   else "后台任务没在跑，别的机器来的信要等有人开会话")
            last = f"上次叫醒 {led.fmt_time(st['last_wake_at'])}" if st.get("last_wake_at") else "还没叫醒过"
            print(f"- {s['address']}：自动叫醒开着；今天叫醒 {used}/{budget} 次；{job}；{last}")
            if st.get("last_wake_rc"):
                print(f"  上次叫醒失败（返回码 {st['last_wake_rc']}），信已放回待办，下次再试：{st.get('last_wake_error', '')}")
        return 0

    seat = _me(led, ns)
    prof = led.require_seat(seat, local=True)
    if ns.action == "off":
        service.uninstall(_service_plan(led, prof))
        led.update_seat(seat, autowake=None, autowake_interval=None, wake="queue")
        print(f"已关掉 {seat} 的自动叫醒，后台任务也删了。之后它的信只排队，等你开会话。")
        return 0

    if prof["agent"] != "codex":
        raise YizhanError("暂时只有 Codex 能自动叫醒。Claude 的订阅登录按条款只用于本人正常使用，"
                          "没人在场时自动启动不合适；有新信时，你在 Claude 会话里说一句话就会收到提示。")
    exe = prof.get("command") or adapters.find_codex()
    good, text = adapters.check_command(exe) if exe else (False, "找不到 codex 命令")
    if not good:
        raise YizhanError(f"codex 跑不起来（{text}）。用 yizhan seat add {prof['name']} --agent codex "
                          "--command <codex 的完整路径> 指定一个能用的")
    workdir = prof.get("workdir") or (prof.get("paths") or [""])[0]
    if not workdir or not adapters.git_toplevel(workdir):
        raise YizhanError(f"被叫醒的 Codex 只肯在 git 仓库里干活，而 {workdir or '它的工作文件夹'} 不在 git 仓库里。"
                          f"用 yizhan seat add {prof['name']} --agent codex --workdir <一个 git 仓库> 指定，"
                          "或者在那个文件夹里 git init")
    # Check the job before anything changes; without a job there is nothing to check.
    p = None if ns.no_service else service.plan(led, seat, ns.interval)
    budget = int(ns.budget or prof.get("wake_budget_per_day") or led.settings["wake_budget_per_day"])
    rounds = led.settings["max_rounds"]
    print(f"打开以后，{seat} 收到要它动手的信（请求或答复）会自己启动一次 Codex，处理完就退出：")
    print("- 同一台机器上，发信方这一轮一结束就叫醒；" + (
        "这次不装后台任务，别的机器来的信要等有人开会话。" if ns.no_service
        else f"别的机器来的信，由后台任务每 {ns.interval} 秒查一次。"))
    print(f"- 每叫醒一次就是一次 Codex 运行，用你账号的额度；每天最多 {budget} 次，"
          f"一条请求往返 {rounds} 轮以后不再自动叫醒，分歧交你裁决。")
    print("- 被叫醒的 Codex 只能写它的工作文件夹和驿站账本。信里的内容仍然只当参考，"
          "权限、实机操作、改别人负责的文件只听你本人。")
    if not ns.yes:
        if not sys.stdin.isatty():
            raise YizhanError("确认要打开的话，加上 --yes 再运行一次")
        if input("确定打开吗？[y/N] ").strip().lower() not in ("y", "yes", "是"):
            print("没有打开。")
            return 1
    led.update_seat(seat, wake="exec", autowake=True, wake_budget_per_day=budget, command=exe,
                    autowake_interval=None if p is None else int(ns.interval))
    if p is not None:
        try:
            service.install(p)
        except YizhanError as e:
            print(f"自动叫醒已打开，但后台任务没装上：{e}。同一台机器上的信照样会立刻叫醒。")
            return 1
        print(f"装好了后台任务（{p['kind']}：{p['name']}）。")
    print(f"已打开 {seat} 的自动叫醒。关掉：yizhan autowake off --as {seat}")
    return 0


def cmd_doctor(ns):
    ok = True

    def say(good, text):
        nonlocal ok
        ok = ok and good
        print(("[好] " if good else "[查] ") + text)

    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip()
        nums = [int(x) for x in out.split()[2].split(".")[:2]]
        say(nums >= [2, 25], f"{out}（需要 2.25 以上）")
    except (OSError, ValueError, IndexError):
        say(False, "没找到 git")
    led = ledger_mod.open_local()
    say(True, f"账本 {led.root}，本机名 {led.machine}，时区 {led.settings['timezone']}")
    if gitio.has_remote(led.root):
        url = gitio.git(led.root, "remote", "get-url", "origin").stdout.strip()
        say(True, f"远端 {url}")
    else:
        say(True, "没有远端：只在本机用，别的机器看不到")
    say(True, "已接入的机器：" + "、".join(m["machine"] for m in led.machines()))
    mine = [s for s in led.seats() if s["machine"] == led.machine]
    if not mine:
        say(False, "本机还没有岗位：yizhan seat add <名字> --agent claude --path <项目文件夹>")
    for s in mine:
        missing = [p for p in s.get("paths", []) if not Path(p).is_dir()]
        say(not missing, f"岗位 {s['address']}（{s['agent']}）文件夹："
                         + ("；".join(s.get("paths", [])) or "未指定") + ("；不存在：" + "；".join(missing) if missing else ""))
        say(adapters.hooks_installed(s["agent"]),
            f"{adapters.get(s['agent']).display} 的 hook " + ("已接上" if adapters.hooks_installed(s["agent"])
                                                         else f"还没接：yizhan hooks --agent {s['agent']}"))
        mode = s.get("wake") or adapters.get(s["agent"]).default_wake
        if mode == "exec":
            exe = s.get("command") or (adapters.find_codex() if s["agent"] == "codex" else None)
            good, text = adapters.check_command(exe) if exe else (False, "找不到命令")
            say(good, f"{s['address']} 叫醒用的命令 {exe}：{text}")
        if s.get("autowake"):
            running = service.installed(_service_plan(led, s))
            say(running, f"{s['address']} 开着自动叫醒；后台任务" + (
                "在跑" if running else f"没在跑，别的机器来的信要等有人开会话（yizhan autowake on --as {s['address']} 重装）"))
            workdir = s.get("workdir") or (s.get("paths") or [""])[0]
            say(bool(adapters.git_toplevel(workdir)) if workdir else False,
                f"{s['address']} 被叫醒时在 {workdir} 干活" + ("" if workdir and adapters.git_toplevel(workdir)
                                                           else "，可它不在 git 仓库里，Codex 会拒绝运行"))
            st = state.load(led, f"seat-{s['address']}")
            if st.get("last_wake_rc"):
                say(False, f"{s['address']} 上次叫醒失败（返回码 {st['last_wake_rc']}）：{st.get('last_wake_error', '')}")
    for d, roots in adapters.memory_collisions(mine).items():
        say(False, f"这几个文件夹共用同一个 Claude 记忆目录 {d}：" + "；".join(roots))
        for root in roots:
            f, snippet = adapters.collision_fix(root)
            print(f"      可在 {f} 里写入 {json.dumps(snippet, ensure_ascii=False)}，让它单独记")
    st = state.load(led, "sync")
    if st.get("last_error"):
        say(False, f"最近一次同步出错（{led.fmt_time(st.get('last_error_at'))}）：{st['last_error'][:300]}")
    elif st.get("last_run"):
        say(True, f"最近一次同步 {led.fmt_time(st.get('last_run'))}，最近一次推送成功 {led.fmt_time(st.get('last_push_ok'))}")
    log = util.state_home() / "hook-errors.log"
    if log.is_file() and log.stat().st_size:
        tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
        say(False, "hook 出过错，最近几行：" + " | ".join(tail))
    return 0 if ok else 1


def cmd_status(ns):
    led = ledger_mod.open_local()
    seats, projects = led.seats(), led.projects()
    print(f"账本 {led.root}（本机 {led.machine}）：{len(led.machines())} 台机器，{len(projects)} 个项目，{len(seats)} 个岗位")
    for s in seats:
        if s["machine"] == led.machine:
            n = len(mail.inbox(led, s["address"]))
            print(f"- {s['address']}：待处理 {n} 封")
    st = state.load(led, "sync")
    if st:
        print(f"最近同步 {led.fmt_time(st.get('last_run'))}；最近推送成功 {led.fmt_time(st.get('last_push_ok'))}"
              + (f"；出错：{st['last_error'][:200]}" if st.get("last_error") else ""))


# ---------- parser ----------

def build_parser():
    p = argparse.ArgumentParser(prog="yizhan", description="驿站：让不同机器、不同 AI、不同会话一起做项目。")
    p.add_argument("--version", action="version", version=f"yizhan {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<命令>")
    sub.required = True

    def add(parent, name, help_text, func):
        sp = parent.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        return sp

    def with_as(sp):
        sp.add_argument("--as", dest="as_", metavar="名字@机器", help="用哪个岗位的身份；默认按当前文件夹认")
        return sp

    s = add(sub, "init", "新建账本，或让本机接入已有的账本克隆", cmd_init)
    s.add_argument("dir", help="账本目录（一个 git 仓库）")
    s.add_argument("--machine", required=True, help="本机名，如 mac、linux、win")
    s.add_argument("--remote", help="远端仓库地址")
    s.add_argument("--timezone", help="如 +08:00；默认取本机当前时区")
    s.add_argument("--no-push", action="store_true", help="先不推送")

    pr = sub.add_parser("project", help="项目：add 新建，list 列出")
    prs = pr.add_subparsers(dest="sub", metavar="<add|list>")
    prs.required = True
    s = add(prs, "add", "新建项目", cmd_project_add)
    s.add_argument("name")
    s.add_argument("--title", default="")
    s.add_argument("--summary", default="", help="一两句话：这个项目要做成什么")
    add(prs, "list", "列出项目", cmd_project_list)

    se = sub.add_parser("seat", help="岗位：add 登记或更新本机的岗位")
    ses = se.add_subparsers(dest="sub", metavar="<add>")
    ses.required = True
    s = add(ses, "add", "登记或更新本机的岗位（没给的项保留原值）", cmd_seat_add)
    s.add_argument("name", help="岗位名，如 claude、claude-paper、codex")
    s.add_argument("--agent", required=True, choices=AGENT_KINDS)
    s.add_argument("--title", default="", help="一句话称呼，如“论文主稿”")
    s.add_argument("--duty", default="", help="负责什么")
    s.add_argument("--project", action="append", default=[], help="参与的项目，可重复")
    s.add_argument("--path", action="append", default=[], help="这个岗位干活的文件夹，可重复")
    s.add_argument("--owns", action="append", default=[], help="归它管的文件或目录，如 paper/**，可重复")
    s.add_argument("--wake", choices=WAKE_MODES, help="queue 只排队；exec 有待办信时自动启动")
    s.add_argument("--workdir", help="被叫醒时在哪个文件夹里干活（默认第一个 --path）")
    s.add_argument("--command", help="叫醒用的命令完整路径，如 codex 的路径")

    s = add(sub, "roster", "分工表：每个岗位负责什么、在做什么", cmd_roster)
    s.add_argument("--project")
    s = with_as(add(sub, "whoami", "当前文件夹算哪个岗位", cmd_whoami))
    s.add_argument("--agent", choices=AGENT_KINDS)
    s = with_as(add(sub, "focus", "更新手上在做的事", cmd_focus))
    s.add_argument("text")

    s = with_as(add(sub, "send", "写信给别的岗位", cmd_send))
    s.add_argument("--to", action="append", default=[], metavar="名字@机器",
                   help="收信方，可重复；project:<项目> 表示这个项目的所有岗位；回信时默认回给原发信方")
    s.add_argument("--kind", default="request", choices=mail.KINDS,
                   help="request 请求、reply 答复（这两种会叫醒对方），update 进展、decision 定论（不叫醒）")
    s.add_argument("--subject", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--body")
    g.add_argument("--body-file", help="正文文件；- 表示标准输入")
    s.add_argument("--reply-to", metavar="编号")
    s.add_argument("--key", help="同一个 key 重发相同内容只留一封")
    s.add_argument("--artifact", action="append", default=[], help="附件路径，可重复")
    s.add_argument("--project")
    w = s.add_mutually_exclusive_group()
    w.add_argument("--wake", dest="wake", action="store_true", default=None, help="要求叫醒对方（过了往返上限仍不叫醒）")
    w.add_argument("--no-wake", dest="wake", action="store_false", help="不叫醒，对方下次查信时看")
    s.add_argument("--now", action="store_true", help="马上定稿；不在 AI 会话里发信时用")

    s = with_as(add(sub, "inbox", "看待处理的信", cmd_inbox))
    s.add_argument("--all", action="store_true", help="连已处理的也列出")
    s = add(sub, "show", "读一封信的正文", cmd_show)
    s.add_argument("id")
    s.add_argument("--json", action="store_true")
    s = with_as(add(sub, "ack", "登记回执", cmd_ack))
    s.add_argument("id")
    s.add_argument("--status", required=True, choices=mail.STATUSES,
                   help="received 收到、working 在做、done 处理完、deferred 暂缓")
    s.add_argument("--note", required=True)

    s = with_as(add(sub, "decide", "记一条定论；或用 --update 改自己那条的状态", cmd_decide))
    s.add_argument("--project")
    s.add_argument("--title")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--body")
    g.add_argument("--body-file")
    s.add_argument("--status", choices=records.DECISION_STATUSES)
    s.add_argument("--ref", action="append", default=[], help="依据：文档、数据、脚本路径，可重复")
    s.add_argument("--supersedes", action="append", default=[], metavar="编号", help="取代哪条定论，可重复")
    s.add_argument("--update", metavar="编号")
    s.add_argument("--note")
    s = add(sub, "decisions", "列出定论", cmd_decisions)
    s.add_argument("--project")

    s = with_as(add(sub, "log", "在本岗位的日志里记一条", cmd_log))
    s.add_argument("--title", required=True)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--body")
    g.add_argument("--body-file")

    s = add(sub, "sync", "过三道闸门，提交、拉取、推送", cmd_sync)
    s.add_argument("--quick", action="store_true", help="不刷新记忆镜像")
    s.add_argument("--allow-shared", action="store_true", help="允许提交共用文件")
    s.add_argument("--no-push", action="store_true")

    s = with_as(add(sub, "brief", "看会话开场时会收到的提醒（不记为已读）", cmd_brief))
    s.add_argument("--agent", choices=AGENT_KINDS)
    s = add(sub, "hook", "给 Claude Code / Codex 的 hook 调用", cmd_hook)
    s.add_argument("--agent", required=True, choices=AGENT_KINDS)
    s.add_argument("--event")
    s = add(sub, "hooks", "打印要接进 Claude Code / Codex 的 hook 配置", cmd_hooks)
    s.add_argument("--agent", required=True, choices=AGENT_KINDS)

    s = with_as(add(sub, "wake", "有定稿的待办信时叫醒这个岗位", cmd_wake))
    s.add_argument("--dry-run", action="store_true", help="只说会怎么做，不真的启动")
    s.add_argument("--watch", action="store_true", help="一直守着")
    s.add_argument("--interval", type=float, default=60,
                   help="--watch 时多少秒查一次：按你要的“别的机器来信多快被发现”定")

    s = with_as(add(sub, "autowake", "自动叫醒（默认关着）：on 打开，off 关掉，status 查看", cmd_autowake))
    s.add_argument("action", choices=("on", "off", "status"))
    s.add_argument("--interval", type=int, default=service.DEFAULT_INTERVAL_S,
                   help="后台任务隔多少秒查一次别的机器来的信，按你要的“多快发现”定，最少 60（默认 300）")
    s.add_argument("--budget", type=int, help="每天最多叫醒几次（默认 12）")
    s.add_argument("--yes", action="store_true", help="不再问一遍，直接打开")
    s.add_argument("--no-service", action="store_true",
                   help="不装后台任务：只在同一台机器上、发信方这一轮结束时叫醒")

    add(sub, "doctor", "体检：git、远端、岗位、hook、自动叫醒、记忆目录撞车、同步状态", cmd_doctor)
    add(sub, "status", "账本概况", cmd_status)
    return p


def main(argv=None):
    util.setup_console()
    ns = build_parser().parse_args(argv)
    try:
        return ns.func(ns) or 0
    except sync.SyncBlocked as e:
        print(str(e), file=sys.stderr)
        return 3
    except YizhanError as e:
        print("错误：" + str(e), file=sys.stderr)
        return 2
    except PermissionError as e:
        # Common when an agent runs yizhan inside its own sandbox.
        print(f"错误：没有权限写 {e.filename}。如果是在 AI 的沙箱里运行，"
              "请让它申请写这个目录，或者把账本和状态目录放到它能写的地方。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
