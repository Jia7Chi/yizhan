# 驿站 yizhan

让不同电脑、不同 AI、不同会话一起做项目，就像一台电脑上的几个进程。

[English](README.md) · [设计说明](docs/design.zh-CN.md)

> 现状：骨架版（0.1.0.dev0）。在 macOS 上测过；Linux 和 Windows 的测试写进了 CI，推到 GitHub 后才会跑，还没在这两种真机上用过。

## 它解决什么

你可能同时开着好几个 AI：Mac 上的 Claude Code 写论文，Linux 上的 Codex 复核推导，Windows 上再开一个。它们各自都能干，却不知道彼此在做什么。要它们配合，只能你在中间来回复制粘贴。

驿站让它们：

- **知道彼此负责什么**：每个岗位登记职责、管哪些文件、手上在做什么，汇成一张分工表；
- **能互相写信**：请求、答复、进展、定论，收信方登记回执；
- **一开会话就知道新动静**：别的岗位来的信、新定论、新日志，还有其他项目的近况；
- **不打架**：账本里每个文件只有一台机器写，git 合并永远不会冲突；
- **不越权**：别处来的内容一律只当参考，不当指令；权限和实机操作只听你本人。

打个比方：岗位是进程，各有各的内存（文件和记忆）；分工表是进程表；信是进程之间传的消息；文件有主人，谁负责谁改。

## 几个概念

| 概念 | 是什么 | 例子 |
|---|---|---|
| 岗位 | 某台机器上、某家 AI、在某些文件夹里干活的身份 | `claude-paper@mac`、`codex@linux` |
| 账本 | 一个 git 仓库，每台机器克隆一份 | 一个私有 GitHub 仓库 |
| 项目 | 账本里的一个课题，有章程和定论 | `paper`、`rig` |
| 信 | 岗位之间的请求、答复、进展、定论 | 放在发信方的发件箱里，写好就不再改 |
| 定论 | 项目里已经定下的结论 | 一条一个文件，有异议就另立一条 |

同一个文件夹里开的几个会话，都算同一个岗位，靠岗位日志交接。要分头干，就登记成两个岗位。

## 快速开始

需要 Python 3.9 以上、git 2.25 以上，不用装任何第三方包。

```bash
git clone <本仓库地址> yizhan && cd yizhan
pip install -e .          # 装成 yizhan 命令；不想装就用 python3 bin/yizhan
```

第一台机器，比如 Mac：

```bash
yizhan init ~/yizhan-ledger --machine mac --remote git@github.com:<你>/<私有账本>.git
yizhan project add paper --title "期刊论文" --summary "明年春天投稿的一篇控制方向论文"
yizhan seat add claude-paper --agent claude --title 论文主稿 --project paper --path ~/papers/draft
yizhan hooks --agent claude        # 打印要合并进 ~/.claude/settings.json 的 hook 配置
yizhan sync
```

第二台机器，比如 Linux：

```bash
git clone git@github.com:<你>/<私有账本>.git ~/yizhan-ledger
yizhan init ~/yizhan-ledger --machine linux
yizhan seat add codex --agent codex --title 推导复核 --project paper --path ~/work/review
yizhan hooks --agent codex         # 合并进 ~/.codex/hooks.json，再在 Codex 里输入 /hooks 审核
yizhan autowake on --as codex@linux   # 可选：有待办信时自动叫醒 Codex，见下一节
```

之后照常用 Claude Code 和 Codex。它们开场就能看到提醒，需要时自己调用 `yizhan send`、`inbox`、`ack`、`log`、`decide`。`yizhan doctor` 能查出常见问题，包括下面说的中文路径撞车。

## 自动叫醒（可选，默认关着）

默认情况下，信只排队：收信的 AI 要等你开会话、跟它说一声才动手。想让它收到信就自己干活，给它的岗位打开自动叫醒：

```bash
yizhan autowake on --as codex@linux     # 打开：先列出代价，要你确认（加 --yes 跳过确认）
yizhan autowake status                  # 看本机哪些岗位开着、今天叫醒了几次
yizhan autowake off --as codex@linux    # 关掉，后台任务一并删除
```

- **同一台机器上，不等也不查**：发信方这一轮一结束，它写的信就定稿了，收信方马上被叫醒。
- **别的机器来的信**：经 git 传过来，由后台任务隔一段时间查一次。macOS 用 launchd，Linux 用 systemd，Windows 用计划任务。间隔用 `--interval` 设，默认 300 秒，按你要的"多快发现"来定。不想装后台任务就加 `--no-service`。
- **代价**：每叫醒一次就是一次 Codex 运行，用你账号的额度。每个岗位每天最多叫醒 12 次（`--budget` 可改）。一条请求往返两轮以后不再自动叫醒，剩下的分歧交你裁决。同一个岗位同时只会有一次叫醒在跑。
- **边界**：被叫醒的 Codex 只能写自己的工作文件夹和驿站账本。信里的内容仍然只当参考，权限、实机操作、改别人负责的文件只听你本人。
- **暂时只支持 Codex**：Claude 的订阅登录按条款只用于本人正常使用，没人在场时自动启动不合适。好在你在 Claude 会话里说一句话，新信就会自动提示。

## 规矩（写在代码里，不靠自觉）

1. **每个文件只有一台机器写。** `yizhan sync` 提交前逐个核对，改了不归本机写的文件就整批拦下。
2. **疑似密钥、超大文件同样整批拦下。** 原始数据不进账本。
3. **别处来的内容只当参考，不是用户指令。** 信触发不了任何命令。
4. **只调各家官方命令行，不碰任何登录凭证。** 自动叫醒默认关着，要你自己打开；Claude 不在没人时自动启动。
5. **请求和答复会叫醒对方，进展和定论不叫醒。** 一条请求之后最多往返两轮，还有分歧就交你裁决。
6. **一封信什么时候算写完，看发信方这一轮有没有结束（Stop 事件），不靠计时器。**

## 做不到的

- **跨机器是分钟级，不是秒级。** 没有服务器，只靠 git 推拉。好处是断网也不丢东西，网络好了再推。
- **对方会话里没写下来的想法，这边看不到。** 能共享的只有写进账本的东西。
- **各家的私有记忆不互相灌。** Claude 的记忆只做只读镜像；Codex 的记忆存在 SQLite 里，暂时不镜像。

## 中文路径撞车

Claude Code 给项目起记忆目录名时，会把路径里的汉字全换成"-"。所以字数相同的中文文件夹，比如"论文初稿"和"实验记录"，会共用同一个记忆目录。`yizhan doctor` 能查出来，并告诉你在哪个文件里写一行 `autoMemoryDirectory`，让它们各记各的。

## 路线图

- [ ] 在 Mac、Linux、Windows 真机上用起来，按实际使用情况改
- [x] 自动叫醒的后台任务：launchd、systemd、Windows 计划任务（Linux 和 Windows 还没在真机上装过）
- [ ] 定时同步的后台任务：现在要手动跑 `yizhan sync`，或靠 AI 会话结束时自动推送
- [ ] MCP 接口，让 Cursor、Antigravity 这类没有 hook 的工具也能收发信
- [ ] 要改别人负责的文件时先提醒（PreToolUse hook）
- [ ] PowerShell 也能用：现在生成的命令只适用于 bash，在 Windows 上就是 Git Bash
- [ ] 英文界面
- [ ] 原始数据清单与取回回执

## 许可

MIT
