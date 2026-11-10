"""Command line: yizhan <command> ..."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__, adapters, brief, gitio, hooks, mail, records, relay, service, state, sync, util, wake
from . import ledger as ledger_mod
from .i18n import pick
from .ledger import AGENT_KINDS, HOOKED_KINDS, WAKE_MODES
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
        raise YizhanError(pick("认不出你是哪个岗位：用 --as 名字@机器 指明，或者在登记过的项目文件夹里运行",
                               "Cannot tell which seat you are: use --as name@machine, or run this in a "
                               "registered project folder"))
    return prof["address"]


def _print_sync(rep):
    print(pick(f"已提交 {rep['committed'][:8]}", f"Committed {rep['committed'][:8]}") if rep.get("committed")
          else pick("没有新改动要提交", "No new changes to commit"))
    if rep.get("pulled"):
        print(rep["pulled"])
    if rep.get("error"):
        print(pick("推送没成功，本地提交还在，下次同步再推：",
                   "The push failed; the local commit is kept and the next sync retries: ") + rep["error"])
    elif rep.get("pushed"):
        print(pick("远端已是最新", "The remote is up to date"))
    for n in rep.get("notes", []):
        print(pick("提示：", "Note: ") + n)


# ---------- commands ----------

def cmd_init(ns):
    led, created = ledger_mod.create_or_join(ns.dir, ns.machine, ns.timezone, ns.remote)
    old = ledger_mod.save_local_config(led.root, led.machine)
    rep = sync.sync(led, quick=True, allow_shared=created, push=not ns.no_push)
    print((pick("新建了驿站账本：", "Created a yizhan ledger: ") if created
           else pick("接入了已有的账本：", "Joined an existing ledger: ")) + str(led.root))
    print(pick(f"本机名：{led.machine}；时区：{led.settings['timezone']}",
               f"Machine name: {led.machine}; time zone: {led.settings['timezone']}"))
    if old:
        print(pick(f"注意：本机原来接的是 {old.get('ledger')}（{old.get('machine')}），已改成这一个",
                   f"Note: this machine was connected to {old.get('ledger')} ({old.get('machine')}); "
                   "it now uses this one"))
    _print_sync(rep)
    print(pick("下一步：yizhan project add <项目名> --title …，再用 yizhan seat add 登记本机的岗位",
               "Next: yizhan project add <name> --title …, then register this machine's seats with yizhan seat add"))


def cmd_project_add(ns):
    led = ledger_mod.open_local()
    p = led.add_project(ns.name, ns.title, ns.summary)
    print(pick(f"建好了项目 {p['name']}（{p['title']}），章程在 {led.project_dir(p['name']) / 'README.md'}",
               f"Created the project {p['name']} ({p['title']}); its charter is at "
               f"{led.project_dir(p['name']) / 'README.md'}"))


def cmd_project_list(ns):
    led = ledger_mod.open_local()
    seats = led.seats()
    projects = led.projects()
    if not projects:
        print(pick("（还没有项目）", "(no projects yet)"))
    for p in projects:
        n = sum(1 for s in seats if p["name"] in s.get("projects", []))
        summary = pick(f"：{p['summary']}", f": {p['summary']}") if p.get("summary") else ""
        print(pick(f"{p['name']}　{p.get('title', '')}{summary}（{n} 个岗位，主管机器 {p.get('owner_machine')}）",
                   f"{p['name']}  {p.get('title', '')}{summary} ({n} seat{'' if n == 1 else 's'}, "
                   f"owner machine {p.get('owner_machine')})"))


def cmd_seat_add(ns):
    led = ledger_mod.open_local()
    prof = led.add_seat(ns.name, ns.agent, ns.title, ns.duty, ns.project, ns.path, ns.owns,
                        ns.wake, ns.workdir, ns.command)
    print(pick(f"登记好了岗位 {prof['address']}（{prof['agent']}）",
               f"Registered the seat {prof['address']} ({prof['agent']})"))
    if prof["paths"]:
        print(pick("在这些文件夹里开的会话都算这个岗位：", "Sessions opened in these folders count as this seat: ")
              + pick("；", "; ").join(prof["paths"]))
    else:
        print(pick("还没给它指定文件夹：hook 认不出它，会话里要用 --as 指明身份。可以用 --path 补上",
                   "It has no folder yet, so the hooks cannot recognize it and its sessions have to name it "
                   "with --as. Add a folder with --path"))
    if prof["agent"] == "cli":
        print(pick(f"命令模板：{prof.get('command', '')}", f"Command template: {prof.get('command', '')}"))
        print(pick(f"它没有 hook，驿站替它收发信。让它收到信就动手：yizhan autowake on --as {prof['address']}",
                   f"It has no hooks, so yizhan relays its mail. To have it act on letters by itself: "
                   f"yizhan autowake on --as {prof['address']}"))
    else:
        print(pick(f"接上 hook：yizhan hooks --agent {prof['agent']}",
                   f"To connect the hooks: yizhan hooks --agent {prof['agent']}"))


def cmd_roster(ns):
    led = ledger_mod.open_local()
    if ns.project:
        lines = brief.roster_lines(led, [ns.project])
        print("\n".join(lines) if lines else pick("（这个项目还没有岗位）", "(this project has no seats yet)"))
        return
    seats = led.seats()
    if not seats:
        print(pick("（还没有岗位）", "(no seats yet)"))
        return
    for p in led.projects():
        lines = brief.roster_lines(led, [p["name"]])
        if lines:
            print(pick(f"【{p['name']}】{p.get('title', '')}", f"[{p['name']}] {p.get('title', '')}"))
            print("\n".join(lines))
    loose = [s["address"] for s in seats if not s.get("projects")]
    if loose:
        print(pick("【没归进项目的岗位】", "[Seats in no project] ") + pick("、", ", ").join(loose))


def cmd_whoami(ns):
    led = ledger_mod.open_local()
    prof = led.resolve_seat(explicit=ns.as_, agent=ns.agent)
    if prof is None:
        print(pick(f"这个文件夹没登记给本机（{led.machine}）的任何岗位",
                   f"This folder is not registered to any seat on this machine ({led.machine})"))
        return 1
    print(pick(f"{prof['address']}（{prof.get('title') or prof['agent']}）",
               f"{prof['address']} ({prof.get('title') or prof['agent']})"))
    return 0


def cmd_focus(ns):
    led = ledger_mod.open_local()
    prof = records.set_focus(led, _me(led, ns), ns.text)
    print(pick(f"已更新 {prof['address']} 手上在做的事：{prof['focus']}",
               f"Updated what {prof['address']} is working on: {prof['focus']}"))


def cmd_send(ns):
    led = ledger_mod.open_local()
    meta = mail.send(led, _me(led, ns), ns.to, ns.kind, ns.subject, _body(ns), ns.reply_to,
                     ns.key, ns.artifact, ns.wake, ns.now, ns.project)
    if not meta["wake"]:
        how = pick("不叫醒对方，对方下次查信时看", "no wake; they will see it the next time they check their letters")
    else:
        auto = [t for t in meta["to"] if (led.seat(t) or {}).get("autowake")]
        if not auto:
            how = pick("待办；对方没开自动叫醒，下次开会话时看到",
                       "needs action; autowake is off for them, so they will see it in their next session")
        elif len(auto) == len(meta["to"]):
            how = pick("待办；对方开着自动叫醒，信写完就会自己启动处理",
                       "needs action; autowake is on for them, so they will start on it by themselves once the "
                       "letter is finished")
        else:
            how = pick(f"待办；{'、'.join(auto)} 开着自动叫醒，其余的下次开会话时看到",
                       f"needs action; autowake is on for {', '.join(auto)}, and the others will see it in their "
                       "next session")
    print(pick(f"已发 {meta['id']} → {'、'.join(meta['to'])}（{how}）",
               f"Sent {meta['id']} → {', '.join(meta['to'])} ({how})"))
    if any(t.split("@", 1)[1] != led.machine for t in meta["to"]):
        print(pick("收信方里有别的机器上的岗位：AI 会话这一轮结束时会自动推送；不在会话里发的，跑一下 yizhan sync",
                   "Some recipients are on other machines: an AI session pushes the letter when its turn ends; "
                   "if you sent it outside a session, run yizhan sync"))


def cmd_inbox(ns):
    led = ledger_mod.open_local()
    items = mail.inbox(led, _me(led, ns), include_closed=ns.all)
    if not items:
        print(pick("（没有待处理的信）", "(no letters waiting for you)"))
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
    print(pick(f"已登记 {rec['id']}：{mail.status_label(rec['status'])}",
               f"Recorded a receipt for {rec['id']}: {mail.status_label(rec['status'])}"))


def cmd_decide(ns):
    led = ledger_mod.open_local()
    me = _me(led, ns)
    if ns.update:
        meta = records.set_status(led, me, ns.update, ns.status or "confirmed", ns.note or "")
        print(pick(f"已把 {meta['id']} 改成{records.decision_label(meta['status'])}",
                   f"Marked {meta['id']} as {records.decision_label(meta['status'])}"))
        return
    if not ns.project or not ns.title:
        raise YizhanError(pick("新定论要有 --project 和 --title", "A new decision needs --project and --title"))
    meta = records.decide(led, me, ns.project, ns.title, _body(ns), ns.status or "confirmed",
                          ns.ref, ns.supersedes)
    print(pick(f"记下了定论 {meta['id']}", f"Recorded the decision {meta['id']}"))


def cmd_decisions(ns):
    led = ledger_mod.open_local()
    items = records.decisions(led, ns.project)
    if not items:
        print(pick("（还没有定论）", "(no decisions yet)"))
    for d in items:
        status = records.decision_label(d.get("status"))
        if d.get("superseded_by"):
            status += pick(f"，已被 {d['superseded_by']} 取代", f", superseded by {d['superseded_by']}")
        print(pick(f"{d['id']}　[{d['project']}] {d['title']}（{d['by']}，{status}）",
                   f"{d['id']}  [{d['project']}] {d['title']} ({d['by']}, {status})"))


def cmd_log(ns):
    led = ledger_mod.open_local()
    path = records.log(led, _me(led, ns), ns.title, _body(ns))
    print(pick(f"记进了 {path}", f"Journal entry added to {path}"))


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


def _hook_follow_up(agent):
    if agent == "codex":
        return pick("Codex 要先审核才会运行新 hook：打开 Codex 输入 /hooks，确认这几条。",
                    "Codex runs new hooks only after you approve them: open Codex, type /hooks and trust them.")
    return pick("Claude Code 新开的会话会用上它们。Windows 上 hook 默认经 Git Bash 执行。",
                "New Claude Code sessions pick them up. On Windows, hooks run through Git Bash by default.")


def cmd_hooks(ns):
    cmd = brief.cli_prefix()
    folder = None if ns.user else ns.folder
    if ns.agent == "claude" and ns.action != "show" and not (ns.folder or ns.user):
        raise YizhanError(pick(
            "给 Claude Code 装 hook 要说清装在哪：--folder <项目文件夹>（只在那个文件夹里生效，推荐）"
            "或 --user（你所有的 Claude Code 会话都生效）",
            "Say where Claude Code's hooks go: --folder <project folder> (only sessions opened there; "
            "recommended) or --user (every Claude Code session)"))
    target = adapters.hook_target(ns.agent, folder)
    if ns.action == "show":
        print(pick(f"把下面的 \"hooks\" 合并进 {target}，已有的 hook 留着；也可以直接运行 yizhan hooks install：",
                   f"Merge the \"hooks\" below into {target}, keeping any hooks already there, or just run "
                   "yizhan hooks install:"))
        print(adapters.dumps(adapters.hook_config(ns.agent, cmd)))
        print(_hook_follow_up(ns.agent))
        print(pick("命令用的是绝对路径：从程序坞打开的应用跑 hook 时，不一定带着终端里的 PATH。",
                   "The command uses absolute paths: an app started from the Dock may run hooks without your PATH."))
        return 0
    if ns.action == "install":
        r = adapters.install_hooks(ns.agent, target, cmd, dry_run=ns.dry_run)
        if not r["changed"]:
            print(pick(f"{target} 里的 hook 已经是最新的，没有改动。", f"The hooks in {target} are already up to date."))
            return 0
    else:
        r = adapters.uninstall_hooks(ns.agent, target, dry_run=ns.dry_run)
        if r is None or not r["changed"]:
            print(pick(f"{target} 里没有驿站的 hook，不用拿。", f"There are no yizhan hooks in {target}."))
            return 0
    if ns.dry_run:
        print(pick(f"（只是预演，没有写入）{target} 会变成：", f"(Dry run, nothing written) {target} would become:"))
        print(r["text"].rstrip())
        return 0
    if ns.action == "install":
        print(pick(f"已把驿站的 hook 写进 {r['path']}", f"Installed yizhan's hooks in {r['path']}"))
        if folder and adapters.keep_out_of_git(r["path"]):
            print(pick("这个文件只对本机有效，已让 git 忽略它（写在 .git/info/exclude）。",
                       "That file only works on this machine, so git now ignores it (via .git/info/exclude)."))
    else:
        print(pick(f"已从 {r['path']} 拿掉驿站的 hook", f"Removed yizhan's hooks from {r['path']}"))
    if r["backup"]:
        print(pick(f"原文件备份在 {r['backup']}", f"The previous file was kept as {r['backup']}"))
    if ns.action == "install":
        print(_hook_follow_up(ns.agent))
    return 0


def cmd_wake(ns):
    led = ledger_mod.open_local()
    me = _me(led, ns)
    if ns.watch:
        wake.watch(led, me, ns.interval, ns.dry_run)
        return 0
    r = wake.wake_once(led, me, dry_run=ns.dry_run)
    for n in r["notes"]:
        print(pick("提示：", "Note: ") + n)
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
            print(pick("（本机还没有岗位）", "(no seats on this machine yet)"))
        today = led.local_date().date().isoformat()
        for s in mine:
            if not s.get("autowake"):
                print(pick(f"- {s['address']}：自动叫醒关着，信只排队，等有人开会话",
                           f"- {s['address']}: autowake is off; letters wait until someone opens a session"))
                continue
            st = state.load(led, f"seat-{s['address']}")
            used = int(st.get("wakes", {}).get(today, 0))
            budget = int(s.get("wake_budget_per_day") or led.settings["wake_budget_per_day"])
            job = (pick("后台任务在跑", "the background job is running") if service.installed(_service_plan(led, s))
                   else pick("后台任务没在跑，别的机器来的信要等有人开会话",
                             "the background job is not running, so letters from other machines wait until "
                             "someone opens a session"))
            last = (pick(f"上次叫醒 {led.fmt_time(st['last_wake_at'])}", f"last woken {led.fmt_time(st['last_wake_at'])}")
                    if st.get("last_wake_at") else pick("还没叫醒过", "not woken yet"))
            print(pick(f"- {s['address']}：自动叫醒开着；今天叫醒 {used}/{budget} 次；{job}；{last}",
                       f"- {s['address']}: autowake is on; woken {used}/{budget} times today; {job}; {last}"))
            if st.get("last_wake_rc"):
                print(pick(f"  上次叫醒失败（返回码 {st['last_wake_rc']}），信已放回待办，下次再试：{st.get('last_wake_error', '')}",
                           f"  The last wake failed (exit code {st['last_wake_rc']}); its letters are back in the "
                           f"queue for another try: {st.get('last_wake_error', '')}"))
        return 0

    seat = _me(led, ns)
    prof = led.require_seat(seat, local=True)
    if ns.action == "off":
        service.uninstall(_service_plan(led, prof))
        led.update_seat(seat, autowake=None, autowake_interval=None, wake="queue")
        print(pick(f"已关掉 {seat} 的自动叫醒，后台任务也删了。之后它的信只排队，等你开会话。",
                   f"Turned off autowake for {seat} and removed its background job. From now on its letters "
                   "wait until you open a session."))
        return 0

    if prof["agent"] == "claude":
        raise YizhanError(pick("Claude Code 不能自动叫醒。Claude 的订阅登录按条款只用于本人正常使用，"
                               "没人在场时自动启动不合适；有新信时，你在 Claude 会话里说一句话就会收到提示。",
                               "Claude Code cannot autowake. Under its terms, a Claude subscription is for your "
                               "own ordinary use, so starting Claude with nobody there is not appropriate; when a "
                               "new letter arrives, type anything in a Claude session and you will see the notice."))
    if prof["agent"] == "cli":
        exe = _check_cli_seat(prof)
        name = Path(relay.parse_template(exe)[0]).name
    else:
        exe = _check_codex_seat(prof)
        name = "Codex"
    # Check the job before anything changes; without a job there is nothing to check.
    p = None if ns.no_service else service.plan(led, seat, ns.interval)
    budget = int(ns.budget or prof.get("wake_budget_per_day") or led.settings["wake_budget_per_day"])
    rounds = led.settings["max_rounds"]
    _explain_autowake(seat, prof, name, ns, budget, rounds)
    if not ns.yes:
        if not sys.stdin.isatty():
            raise YizhanError(pick("确认要打开的话，加上 --yes 再运行一次", "To turn it on, run this again with --yes"))
        if input(pick("确定打开吗？[y/N] ", "Turn it on? [y/N] ")).strip().lower() not in ("y", "yes", "是"):
            print(pick("没有打开。", "Left off."))
            return 1
    return _enable_autowake(led, seat, ns, p, budget, exe)


def _check_codex_seat(prof):
    exe = prof.get("command") or adapters.find_codex()
    good, text = adapters.check_command(exe) if exe else (False, pick("找不到 codex 命令", "cannot find the codex command"))
    if not good:
        raise YizhanError(pick(f"codex 跑不起来（{text}）。用 yizhan seat add {prof['name']} --agent codex "
                               "--command <codex 的完整路径> 指定一个能用的",
                               f"codex does not run ({text}). Point to one that works with yizhan seat add "
                               f"{prof['name']} --agent codex --command <full path to codex>"))
    workdir = prof.get("workdir") or (prof.get("paths") or [""])[0]
    if not workdir or not adapters.git_toplevel(workdir):
        raise YizhanError(pick(f"被叫醒的 Codex 只肯在 git 仓库里干活，而 {workdir or '它的工作文件夹'} 不在 git 仓库里。"
                               f"用 yizhan seat add {prof['name']} --agent codex --workdir <一个 git 仓库> 指定，"
                               "或者在那个文件夹里 git init",
                               f"A woken Codex only works inside a git repository, and {workdir or 'its work folder'} "
                               f"is not in one. Choose one with yizhan seat add {prof['name']} --agent codex "
                               "--workdir <a git repository>, or run git init in that folder"))
    return exe


def _check_cli_seat(prof):
    """A relay seat (relay.py): its command template must parse and its program must run
    here. Returns the template with the program as a full path."""
    template = prof.get("command") or ""
    if not template:
        raise YizhanError(pick(f"{prof['address']} 还没有命令模板：yizhan seat add {prof['name']} --agent cli "
                               "--command 'qwen -p {prompt}'",
                               f"{prof['address']} has no command template yet: yizhan seat add {prof['name']} "
                               "--agent cli --command 'qwen -p {prompt}'"))
    if not relay.program(template):
        raise YizhanError(pick(f"找不到命令 {relay.parse_template(template)[0]}：先装好它并登录，或者在模板里写完整路径",
                               f"Cannot find the command {relay.parse_template(template)[0]}: install it and sign "
                               "in first, or give its full path in the template"))
    relay.check(template)
    workdir = prof.get("workdir") or (prof.get("paths") or [""])[0]
    if not workdir or not Path(workdir).is_dir():
        raise YizhanError(pick(f"{prof['address']} 的工作文件夹不存在：用 --path 或 --workdir 指一个",
                               f"{prof['address']} has no work folder: give one with --path or --workdir"))
    return relay.absolute(template)


def _explain_autowake(seat, prof, name, ns, budget, rounds):
    print(pick(f"打开以后，{seat} 收到要它动手的信（请求或答复）会自己启动一次 {name}，处理完就退出：",
               f"Once this is on, a letter that {seat} has to act on (a request or a reply) starts {name} by "
               "itself; it handles the letter and exits:"))
    print(pick("- 同一台机器上，发信方这一轮一结束就叫醒；",
               "- When the sender is on the same machine, it is woken as soon as the sender's turn ends; ") + (
        pick("这次不装后台任务，别的机器来的信要等有人开会话。",
             "no background job this time, so letters from other machines wait until someone opens a session.")
        if ns.no_service
        else pick(f"别的机器来的信，由后台任务每 {ns.interval} 秒查一次。",
                  f"a background job checks for letters from other machines every {ns.interval} seconds.")))
    print(pick(f"- 每叫醒一次就是一次 {name} 运行，用你账号的额度；每天最多 {budget} 次，"
               f"一条请求往返 {rounds} 轮以后不再自动叫醒，分歧交你裁决。",
               f"- Each wake is one {name} run on your account's quota, at most {budget} a day. After {rounds} "
               f"round trip{'' if rounds == 1 else 's'} on one request it stops waking, and the disagreement "
               "comes to you."))
    if prof["agent"] == "cli":
        print(pick(f"- 驿站把信写进提示词交给 {name}，它在工作文件夹里干活；它最后的说明和文件改动会作为答复寄回，"
                   "并立刻叫醒发信方。它能改哪些文件、能不能运行命令，由你在命令模板里给它的参数决定。",
                   f"- yizhan puts the letter into {name}'s prompt and runs it in the work folder; its final words "
                   "and the files it changed go back as the reply, and the sender is woken at once. What it may "
                   "change or run is set by the options you give it in the command template."))
        print(pick("- 信里的内容仍然只当参考：权限、实机操作、改别人负责的文件只听你本人。",
                   "- Letters are still only for reference: permissions, actions on physical hardware and changes "
                   "to files another seat owns come only from you."))
    else:
        print(pick("- 被叫醒的 Codex 只能写它的工作文件夹和驿站账本。信里的内容仍然只当参考，"
                   "权限、实机操作、改别人负责的文件只听你本人。",
                   "- A woken Codex can write only its work folder and the yizhan ledger. Letters are still only "
                   "for reference: permissions, actions on physical hardware and changes to files another seat "
                   "owns come only from you."))


def _enable_autowake(led, seat, ns, p, budget, exe):
    led.update_seat(seat, wake="exec", autowake=True, wake_budget_per_day=budget, command=exe,
                    autowake_interval=None if p is None else int(ns.interval))
    if p is not None:
        try:
            service.install(p)
        except YizhanError as e:
            print(pick(f"自动叫醒已打开，但后台任务没装上：{e}。同一台机器上的信照样会立刻叫醒。",
                       f"Autowake is on, but the background job could not be installed: {e}. Letters sent on "
                       "this machine still wake it at once."))
            return 1
        print(pick(f"装好了后台任务（{p['kind']}：{p['name']}）。", f"Installed the background job ({p['kind']}: {p['name']})."))
    print(pick(f"已打开 {seat} 的自动叫醒。关掉：yizhan autowake off --as {seat}",
               f"Autowake is on for {seat}. To turn it off: yizhan autowake off --as {seat}"))
    return 0


def cmd_doctor(ns):
    ok = True

    def say(good, text):
        nonlocal ok
        ok = ok and good
        print((pick("[好] ", "[ok] ") if good else pick("[查] ", "[check] ")) + text)

    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip()
        nums = [int(x) for x in out.split()[2].split(".")[:2]]
        say(nums >= [2, 25], pick(f"{out}（需要 2.25 以上）", f"{out} (2.25 or later needed)"))
    except (OSError, ValueError, IndexError):
        say(False, pick("没找到 git", "git not found"))
    led = ledger_mod.open_local()
    say(True, pick(f"账本 {led.root}，本机名 {led.machine}，时区 {led.settings['timezone']}",
                   f"Ledger {led.root}, machine name {led.machine}, time zone {led.settings['timezone']}"))
    if gitio.has_remote(led.root):
        url = gitio.git(led.root, "remote", "get-url", "origin").stdout.strip()
        say(True, pick(f"远端 {url}", f"Remote {url}"))
    else:
        say(True, pick("没有远端：只在本机用，别的机器看不到", "No remote: this machine only; other machines cannot see it"))
    say(True, pick("已接入的机器：", "Machines in the ledger: ") + pick("、", ", ").join(m["machine"] for m in led.machines()))
    mine = [s for s in led.seats() if s["machine"] == led.machine]
    if not mine:
        say(False, pick("本机还没有岗位：yizhan seat add <名字> --agent claude --path <项目文件夹>",
                        "No seats on this machine yet: yizhan seat add <name> --agent claude --path <project-folder>"))
    for s in mine:
        missing = [p for p in s.get("paths", []) if not Path(p).is_dir()]
        say(not missing, pick(f"岗位 {s['address']}（{s['agent']}）文件夹：", f"Seat {s['address']} ({s['agent']}) folders: ")
                         + (pick("；", "; ").join(s.get("paths", [])) or pick("未指定", "none given"))
                         + (pick("；不存在：", "; missing: ") + pick("；", "; ").join(missing) if missing else ""))
        if s["agent"] in HOOKED_KINDS:
            say(adapters.hooks_installed(s["agent"], s.get("paths", [])),
                pick(f"{adapters.get(s['agent']).display} 的 hook ", f"{adapters.get(s['agent']).display} hooks ")
                + (pick("已接上", "connected") if adapters.hooks_installed(s["agent"], s.get("paths", []))
                   else pick(f"还没接：yizhan hooks --agent {s['agent']}",
                             f"not connected yet: yizhan hooks --agent {s['agent']}")))
        else:
            say(True, pick(f"{s['address']} 没有 hook，由驿站代收代发：{s.get('command', '')}",
                           f"{s['address']} has no hooks; yizhan relays its mail: {s.get('command', '')}"))
        mode = s.get("wake") or adapters.get(s["agent"]).default_wake
        if mode == "exec":
            if s["agent"] == "cli":
                exe = relay.program(s.get("command") or "") if s.get("command") else None
            else:
                exe = s.get("command") or (adapters.find_codex() if s["agent"] == "codex" else None)
            good, text = adapters.check_command(exe) if exe else (False, pick("找不到命令", "command not found"))
            say(good, pick(f"{s['address']} 叫醒用的命令 {exe}：{text}", f"{s['address']} wakes with {exe}: {text}"))
        if s.get("autowake"):
            running = service.installed(_service_plan(led, s))
            say(running, pick(f"{s['address']} 开着自动叫醒；后台任务", f"{s['address']} has autowake on; the background job ") + (
                pick("在跑", "is running") if running
                else pick(f"没在跑，别的机器来的信要等有人开会话（yizhan autowake on --as {s['address']} 重装）",
                          "is not running, so letters from other machines wait until someone opens a session "
                          f"(reinstall it with yizhan autowake on --as {s['address']})")))
            workdir = s.get("workdir") or (s.get("paths") or [""])[0]
            if s["agent"] == "cli":
                say(bool(workdir) and Path(workdir).is_dir(),
                    pick(f"{s['address']} 被叫醒时在 {workdir or '（未指定）'} 干活",
                         f"When woken, {s['address']} works in {workdir or '(none given)'}")
                    + ("" if workdir and adapters.git_toplevel(workdir)
                       else pick("；不在 git 仓库里，答复里列不出改了哪些文件",
                                 "; not a git repository, so replies cannot list the changed files")))
            else:
                say(bool(adapters.git_toplevel(workdir)) if workdir else False,
                    pick(f"{s['address']} 被叫醒时在 {workdir} 干活", f"When woken, {s['address']} works in {workdir}")
                    + ("" if workdir and adapters.git_toplevel(workdir)
                       else pick("，可它不在 git 仓库里，Codex 会拒绝运行",
                                 ", but that folder is not in a git repository, so Codex will refuse to run")))
            st = state.load(led, f"seat-{s['address']}")
            if st.get("last_wake_rc"):
                say(False, pick(f"{s['address']} 上次叫醒失败（返回码 {st['last_wake_rc']}）：{st.get('last_wake_error', '')}",
                                f"The last wake of {s['address']} failed (exit code {st['last_wake_rc']}): "
                                f"{st.get('last_wake_error', '')}"))
    for d, roots in adapters.memory_collisions(mine).items():
        say(False, pick(f"这几个文件夹共用同一个 Claude 记忆目录 {d}：", f"These folders share one Claude memory folder, {d}: ")
            + pick("；", "; ").join(roots))
        for root in roots:
            f, snippet = adapters.collision_fix(root)
            print(pick(f"      可在 {f} 里写入 {json.dumps(snippet, ensure_ascii=False)}，让它单独记",
                       f"      To give it a memory of its own, write {json.dumps(snippet, ensure_ascii=False)} "
                       f"into {f}"))
    st = state.load(led, "sync")
    if st.get("last_error"):
        say(False, pick(f"最近一次同步出错（{led.fmt_time(st.get('last_error_at'))}）：{st['last_error'][:300]}",
                        f"The last sync failed ({led.fmt_time(st.get('last_error_at'))}): {st['last_error'][:300]}"))
    elif st.get("last_run"):
        say(True, pick(f"最近一次同步 {led.fmt_time(st.get('last_run'))}，最近一次推送成功 {led.fmt_time(st.get('last_push_ok'))}",
                       f"Last sync {led.fmt_time(st.get('last_run'))}, last successful push "
                       f"{led.fmt_time(st.get('last_push_ok'))}"))
    log = util.state_home() / "hook-errors.log"
    if log.is_file() and log.stat().st_size:
        tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
        say(False, pick("hook 出过错，最近几行：", "The hooks have failed before; the last lines: ") + " | ".join(tail))
    return 0 if ok else 1


def cmd_demo(ns):
    from . import demo
    demo.run(demo.Screen(pause=0 if ns.fast else None), keep=not ns.clean)
    return 0


def cmd_setup(ns):
    from . import setup
    if not ns.yes and not sys.stdin.isatty():
        raise YizhanError(pick("这里没有终端可以回答问题：加 --yes 全用默认值，或用参数给出各项（yizhan setup --help）",
                               "There is no terminal to answer questions: add --yes to take every default, or give "
                               "the answers as options (yizhan setup --help)"))
    return setup.run(ns)


def cmd_board(ns):
    import webbrowser
    from . import board
    if ns.ledger:
        local_root, local_machine = ledger_mod.local_config()
        same = local_root and util.norm_path(local_root) == util.norm_path(ns.ledger)
        led = ledger_mod.Ledger(ns.ledger, ns.machine or (local_machine if same else None) or "viewer")
    else:
        led = ledger_mod.open_local()
    path = board.write(led, ns.out)
    print(pick(f"看板写好了：{path}", f"Board written to {path}"))
    if ns.open:
        webbrowser.open(path.as_uri())
    else:
        print(pick("加 --open 直接在浏览器里打开。", "Add --open to open it in your browser."))
    return 0


def cmd_status(ns):
    led = ledger_mod.open_local()
    seats, projects = led.seats(), led.projects()
    machines = len(led.machines())
    print(pick(f"账本 {led.root}（本机 {led.machine}）：{machines} 台机器，{len(projects)} 个项目，{len(seats)} 个岗位",
               f"Ledger {led.root} (this machine: {led.machine}): {machines} machine{'' if machines == 1 else 's'}, "
               f"{len(projects)} project{'' if len(projects) == 1 else 's'}, "
               f"{len(seats)} seat{'' if len(seats) == 1 else 's'}"))
    for s in seats:
        if s["machine"] == led.machine:
            n = len(mail.inbox(led, s["address"]))
            print(pick(f"- {s['address']}：待处理 {n} 封", f"- {s['address']}: {n} letter{'' if n == 1 else 's'} waiting"))
    st = state.load(led, "sync")
    if st:
        print(pick(f"最近同步 {led.fmt_time(st.get('last_run'))}；最近推送成功 {led.fmt_time(st.get('last_push_ok'))}",
                   f"Last sync {led.fmt_time(st.get('last_run'))}; last successful push "
                   f"{led.fmt_time(st.get('last_push_ok'))}")
              + (pick(f"；出错：{st['last_error'][:200]}", f"; error: {st['last_error'][:200]}")
                 if st.get("last_error") else ""))


# ---------- parser ----------

def build_parser():
    p = argparse.ArgumentParser(prog="yizhan", description=pick(
        "驿站：让不同机器、不同 AI、不同会话一起做项目。",
        "yizhan: lets different machines, AIs and sessions work on projects together."))
    p.add_argument("--version", action="version", version=f"yizhan {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar=pick("<命令>", "<command>"))

    def add(parent, name, help_text, func):
        sp = parent.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        return sp

    def with_as(sp):
        sp.add_argument("--as", dest="as_", metavar=pick("名字@机器", "name@machine"),
                        help=pick("用哪个岗位的身份；默认按当前文件夹认",
                                  "the seat to act as; by default, the one registered for the current folder"))
        return sp

    s = add(sub, "init", pick("新建账本，或让本机接入已有的账本克隆",
                              "Create a ledger, or connect this machine to a clone of an existing one"), cmd_init)
    s.add_argument("dir", help=pick("账本目录（一个 git 仓库）", "the ledger folder (a git repository)"))
    s.add_argument("--machine", required=True, help=pick("本机名，如 mac、linux、win",
                                                         "this machine's name, e.g. mac, linux, win"))
    s.add_argument("--remote", help=pick("远端仓库地址", "URL of the remote repository"))
    s.add_argument("--timezone", help=pick("如 +08:00；默认取本机当前时区",
                                           "e.g. +08:00; default: this machine's current time zone"))
    s.add_argument("--no-push", action="store_true", help=pick("先不推送", "don't push yet"))

    pr = sub.add_parser("project", help=pick("项目：add 新建，list 列出", "Projects: add creates one, list shows them"))
    prs = pr.add_subparsers(dest="sub", metavar="<add|list>")
    prs.required = True
    s = add(prs, "add", pick("新建项目", "Create a project"), cmd_project_add)
    s.add_argument("name")
    s.add_argument("--title", default="")
    s.add_argument("--summary", default="", help=pick("一两句话：这个项目要做成什么",
                                                      "a sentence or two on what the project should achieve"))
    add(prs, "list", pick("列出项目", "List the projects"), cmd_project_list)

    se = sub.add_parser("seat", help=pick("岗位：add 登记或更新本机的岗位",
                                          "Seats: add registers or updates a seat on this machine"))
    ses = se.add_subparsers(dest="sub", metavar="<add>")
    ses.required = True
    s = add(ses, "add", pick("登记或更新本机的岗位（没给的项保留原值）",
                             "Register or update a seat on this machine (options left out keep their old values)"),
            cmd_seat_add)
    s.add_argument("name", help=pick("岗位名，如 claude、claude-paper、codex",
                                     "the seat name, e.g. claude, claude-paper, codex"))
    s.add_argument("--agent", required=True, choices=AGENT_KINDS)
    s.add_argument("--title", default="", help=pick("一句话称呼，如“论文主稿”", 'a short title, e.g. "paper lead"'))
    s.add_argument("--duty", default="", help=pick("负责什么", "what it is responsible for"))
    s.add_argument("--project", action="append", default=[],
                   help=pick("参与的项目，可重复", "a project it works on; repeatable"))
    s.add_argument("--path", action="append", default=[],
                   help=pick("这个岗位干活的文件夹，可重复", "a folder this seat works in; repeatable"))
    s.add_argument("--owns", action="append", default=[],
                   help=pick("归它管的文件或目录，如 paper/**，可重复", "a file or folder it owns, e.g. paper/**; repeatable"))
    s.add_argument("--wake", choices=WAKE_MODES, help=pick("queue 只排队；exec 有待办信时自动启动",
                                                           "queue: letters just wait; exec: start by itself when a "
                                                           "letter needs action"))
    s.add_argument("--workdir", help=pick("被叫醒时在哪个文件夹里干活（默认第一个 --path）",
                                          "the folder it works in when woken (default: the first --path)"))
    s.add_argument("--command", help=pick("codex：叫醒用的 codex 完整路径；cli：命令模板，{prompt} 换成提示词，"
                                          "如 'qwen -p {prompt}'",
                                          "codex: full path of codex; cli: a command template where {prompt} "
                                          "becomes the prompt, such as 'qwen -p {prompt}'"))

    s = add(sub, "roster", pick("分工表：每个岗位负责什么、在做什么",
                                "Who does what: each seat's duty and what it is working on"), cmd_roster)
    s.add_argument("--project")
    s = with_as(add(sub, "whoami", pick("当前文件夹算哪个岗位", "Show which seat the current folder belongs to"), cmd_whoami))
    s.add_argument("--agent", choices=AGENT_KINDS)
    s = with_as(add(sub, "focus", pick("更新手上在做的事", "Update what you are working on"), cmd_focus))
    s.add_argument("text")

    s = with_as(add(sub, "send", pick("写信给别的岗位", "Write a letter to other seats"), cmd_send))
    s.add_argument("--to", action="append", default=[], metavar=pick("名字@机器", "name@machine"),
                   help=pick("收信方，可重复；project:<项目> 表示这个项目的所有岗位；回信时默认回给原发信方",
                             "a recipient, repeatable; project:<name> means every seat in that project; a reply "
                             "goes to the original sender by default"))
    s.add_argument("--kind", default="request", choices=mail.KINDS,
                   help=pick("request 请求、reply 答复（这两种会叫醒对方），update 进展、decision 定论（不叫醒）",
                             "request or reply (these two wake the recipient), update or decision (no wake)"))
    s.add_argument("--subject", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--body")
    g.add_argument("--body-file", help=pick("正文文件；- 表示标准输入", "a file holding the body; - means standard input"))
    s.add_argument("--reply-to", metavar=pick("编号", "id"))
    s.add_argument("--key", help=pick("同一个 key 重发相同内容只留一封",
                                      "resending the same content with the same key keeps only one letter"))
    s.add_argument("--artifact", action="append", default=[],
                   help=pick("附件路径，可重复", "path of an attachment; repeatable"))
    s.add_argument("--project")
    w = s.add_mutually_exclusive_group()
    w.add_argument("--wake", dest="wake", action="store_true", default=None,
                   help=pick("要求叫醒对方（过了往返上限仍不叫醒）",
                             "wake the recipient (but never past the round-trip limit)"))
    w.add_argument("--no-wake", dest="wake", action="store_false",
                   help=pick("不叫醒，对方下次查信时看", "don't wake the recipient; they see it the next time they check"))
    s.add_argument("--now", action="store_true", help=pick("马上定稿；不在 AI 会话里发信时用",
                                                           "finalize at once; for sending from outside an AI session"))

    s = with_as(add(sub, "inbox", pick("看待处理的信", "List the letters waiting for you"), cmd_inbox))
    s.add_argument("--all", action="store_true", help=pick("连已处理的也列出", "include letters already handled"))
    s = add(sub, "show", pick("读一封信的正文", "Read a letter"), cmd_show)
    s.add_argument("id")
    s.add_argument("--json", action="store_true")
    s = with_as(add(sub, "ack", pick("登记回执", "Record a receipt"), cmd_ack))
    s.add_argument("id")
    s.add_argument("--status", required=True, choices=mail.STATUSES,
                   help=pick("received 收到、working 在做、done 处理完、deferred 暂缓",
                             "received, working, done (handled) or deferred (put off)"))
    s.add_argument("--note", required=True)

    s = with_as(add(sub, "decide", pick("记一条定论；或用 --update 改自己那条的状态",
                                        "Record a decision, or change the status of one you wrote with --update"),
                    cmd_decide))
    s.add_argument("--project")
    s.add_argument("--title")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--body")
    g.add_argument("--body-file")
    s.add_argument("--status", choices=records.DECISION_STATUSES)
    s.add_argument("--ref", action="append", default=[], help=pick("依据：文档、数据、脚本路径，可重复",
                                                                   "what it rests on: the path of a document, data "
                                                                   "or a script; repeatable"))
    s.add_argument("--supersedes", action="append", default=[], metavar=pick("编号", "id"),
                   help=pick("取代哪条定论，可重复", "a decision this one supersedes; repeatable"))
    s.add_argument("--update", metavar=pick("编号", "id"))
    s.add_argument("--note")
    s = add(sub, "decisions", pick("列出定论", "List the decisions"), cmd_decisions)
    s.add_argument("--project")

    s = with_as(add(sub, "log", pick("在本岗位的日志里记一条", "Add an entry to this seat's journal"), cmd_log))
    s.add_argument("--title", required=True)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--body")
    g.add_argument("--body-file")

    s = add(sub, "sync", pick("过三道闸门，提交、拉取、推送", "Pass the three gates, then commit, pull and push"), cmd_sync)
    s.add_argument("--quick", action="store_true", help=pick("不刷新记忆镜像", "don't refresh the memory mirror"))
    s.add_argument("--allow-shared", action="store_true", help=pick("允许提交共用文件", "allow committing shared files"))
    s.add_argument("--no-push", action="store_true")

    s = with_as(add(sub, "brief", pick("看会话开场时会收到的提醒（不记为已读）",
                                       "Show the brief a session gets when it starts (nothing is marked as read)"),
                    cmd_brief))
    s.add_argument("--agent", choices=AGENT_KINDS)
    s = add(sub, "hook", pick("给 Claude Code / Codex 的 hook 调用", "Called by the Claude Code and Codex hooks"), cmd_hook)
    s.add_argument("--agent", required=True, choices=HOOKED_KINDS)
    s.add_argument("--event")
    s = add(sub, "hooks", pick("接上或拿掉 Claude Code / Codex 的 hook：show 打印配置，install 写进去（先备份），uninstall 拿掉",
                               "Connect or disconnect Claude Code / Codex: show the hooks, install them (with a backup), "
                               "or uninstall them"), cmd_hooks)
    s.add_argument("action", nargs="?", default="show", choices=("show", "install", "uninstall"))
    s.add_argument("--agent", required=True, choices=HOOKED_KINDS)
    where = s.add_mutually_exclusive_group()
    where.add_argument("--folder", help=pick("Claude Code：只装进这个项目文件夹（推荐）",
                                             "Claude Code: only for this project folder (recommended)"))
    where.add_argument("--user", action="store_true", help=pick("Claude Code：装进用户级配置，所有会话都生效",
                                                                "Claude Code: user settings, every session"))
    s.add_argument("--dry-run", action="store_true", help=pick("只显示会写成什么，不写", "show the result, write nothing"))

    s = with_as(add(sub, "wake", pick("有定稿的待办信时叫醒这个岗位",
                                      "Wake this seat when finalized letters need its action"), cmd_wake))
    s.add_argument("--dry-run", action="store_true", help=pick("只说会怎么做，不真的启动", "say what would happen, start nothing"))
    s.add_argument("--watch", action="store_true", help=pick("一直守着", "keep watching"))
    s.add_argument("--interval", type=float, default=60,
                   help=pick("--watch 时多少秒查一次：按你要的“别的机器来信多快被发现”定",
                             "with --watch, seconds between checks: set it by how soon a letter from another "
                             "machine should be noticed"))

    s = with_as(add(sub, "autowake", pick("自动叫醒（默认关着）：on 打开，off 关掉，status 查看",
                                          "Autowake (off by default): on turns it on, off turns it off, status "
                                          "shows where it stands"), cmd_autowake))
    s.add_argument("action", choices=("on", "off", "status"))
    s.add_argument("--interval", type=int, default=service.DEFAULT_INTERVAL_S,
                   help=pick("后台任务隔多少秒查一次别的机器来的信，按你要的“多快发现”定，最少 60（默认 300）",
                             "seconds between the background job's checks for letters from other machines; set it "
                             "by how soon they should be noticed; at least 60 (default 300)"))
    s.add_argument("--budget", type=int, help=pick("每天最多叫醒几次（默认 12）", "at most this many wakes a day (default 12)"))
    s.add_argument("--yes", action="store_true", help=pick("不再问一遍，直接打开", "turn it on without asking first"))
    s.add_argument("--no-service", action="store_true",
                   help=pick("不装后台任务：只在同一台机器上、发信方这一轮结束时叫醒",
                             "install no background job: wake only when the sender is on the same machine, as "
                             "its turn ends"))

    add(sub, "doctor", pick("体检：git、远端、岗位、hook、自动叫醒、记忆目录撞车、同步状态",
                            "Check-up: git, the remote, seats, hooks, autowake, clashing memory folders and the "
                            "sync state"), cmd_doctor)
    add(sub, "status", pick("账本概况", "An overview of the ledger"), cmd_status)

    s = add(sub, "demo", pick("十秒演示：两台机器、两家 AI 在一本临时账本里协作（不碰你的配置，也不调用 AI）",
                              "A ten-second tour: two machines and two AIs working in a throwaway ledger "
                              "(your settings stay untouched and no AI is called)"), cmd_demo)
    s.add_argument("--fast", action="store_true", help=pick("不停顿", "no pauses"))
    s.add_argument("--clean", action="store_true", help=pick("演示完删掉临时账本", "delete the demo ledgers afterwards"))

    s = add(sub, "setup", pick("设置向导：一分钟接好账本、项目、岗位和 hook，可以反复运行",
                               "Guided setup: ledger, project, seats and hooks in about a minute; safe to run again"),
            cmd_setup)
    s.add_argument("-y", "--yes", action="store_true", help=pick("不提问，全用默认值", "ask nothing; take every default"))
    s.add_argument("--lang", choices=("zh", "en"), help=pick("界面语言，会记在本机", "language for yizhan on this machine"))
    s.add_argument("--ledger", help=pick("账本文件夹（默认 ~/yizhan-ledger）", "ledger folder (default ~/yizhan-ledger)"))
    s.add_argument("--remote", help=pick("从这个远端克隆账本（空仓库也行）", "clone the ledger from this remote (an empty repository works)"))
    s.add_argument("--machine", help=pick("本机名，如 mac、linux、win", "this machine's name, e.g. mac, linux, win"))
    s.add_argument("--folder", help=pick("AI 干活的项目文件夹（默认当前文件夹）", "the folder the AIs work in (default: here)"))
    s.add_argument("--project", help=pick("项目名", "project name"))
    s.add_argument("--title", help=pick("新项目的全称", "a new project's full title"))
    s.add_argument("--summary", help=pick("新项目的一句话简介", "a new project's one-line summary"))
    s.add_argument("--agent", action="append", choices=HOOKED_KINDS,
                   help=pick("要接上的 AI，可重复；默认本机找到的都接", "which AI to connect, repeatable; default: every one found"))
    s.add_argument("--no-hooks", action="store_true", help=pick("不写 hook", "don't install hooks"))
    s.add_argument("--autowake", action="store_true", help=pick("给 Codex 打开自动叫醒", "turn on autowake for Codex"))

    s = add(sub, "board", pick("看板：把整本账本画成一张网页（谁在哪、在做什么、信件、定论、日志）",
                               "The board: the whole ledger as one web page (who sits where, what they are doing, "
                               "letters, decisions, journal)"), cmd_board)
    s.add_argument("--open", action="store_true", help=pick("在浏览器里打开", "open it in your browser"))
    s.add_argument("--out", help=pick("写到这个文件（默认在本机状态目录）", "write to this file (default: this machine's state folder)"))
    s.add_argument("--ledger", help=pick("看另一本账本，比如演示账本", "show another ledger, such as the demo one"))
    s.add_argument("--machine", help=pick("配合 --ledger：以哪台机器的视角看", "with --ledger: whose machine to view it as"))
    return p


def welcome():
    """What plain `yizhan` prints: where a newcomer starts."""
    return pick(
        "驿站：让你每台电脑上的 Claude Code 和 Codex 像一个团队那样干活。\n\n"
        "第一次用？\n"
        "  yizhan demo     十秒演示，不碰你的任何配置\n"
        "  yizhan setup    一分钟接好这台机器\n"
        "  yizhan board    整本账本画成一张网页\n\n"
        "全部命令：yizhan --help",
        "yizhan: let Claude Code and Codex on all your computers work as one team.\n\n"
        "New here?\n"
        "  yizhan demo     a ten-second tour that touches none of your settings\n"
        "  yizhan setup    connect this machine in about a minute\n"
        "  yizhan board    the whole ledger as one web page\n\n"
        "Every command: yizhan --help")


def main(argv=None):
    util.setup_console()
    ns = build_parser().parse_args(argv)
    if not getattr(ns, "func", None):
        print(welcome())
        return 0
    try:
        return ns.func(ns) or 0
    except sync.SyncBlocked as e:
        print(str(e), file=sys.stderr)
        return 3
    except YizhanError as e:
        print(pick("错误：", "Error: ") + str(e), file=sys.stderr)
        return 2
    except PermissionError as e:
        # Common when an agent runs yizhan inside its own sandbox.
        print(pick(f"错误：没有权限写 {e.filename}。如果是在 AI 的沙箱里运行，"
                   "请让它申请写这个目录，或者把账本和状态目录放到它能写的地方。",
                   f"Error: no permission to write {e.filename}. If yizhan runs inside an AI's sandbox, have "
                   "the AI ask for write access to that folder, or move the ledger and the state folder somewhere "
                   "it can write."), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
