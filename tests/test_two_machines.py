import json

from helpers import Sandbox

from yizhan import brief, gitio, mail, records, sync
from yizhan.util import YizhanError


class TwoMachines(Sandbox):
    def setUp(self):
        super().setUp()
        self.a, self.b = self.make_pair()
        self.a.add_project("demo", title="示例课题", summary="一篇论文")
        self.pa = self.folder("proj-mac")
        self.a.add_seat("claude", "claude", title="论文主稿", duty="写论文", projects=["demo"],
                        paths=[str(self.pa)], owns=["paper/**"])
        self.both(self.a, self.b)
        self.pb = self.folder("proj-linux")
        self.b.add_seat("codex", "codex", title="推导复核", projects=["demo"], paths=[str(self.pb)])
        self.both(self.b, self.a)

    def test_letter_round_trip(self):
        m = mail.send(self.a, "claude@mac", ["codex@linux"], "request", "请复核推导", "看第三节的推导。")
        self.assertTrue(m["wake"])
        self.both(self.a, self.b)
        self.assertEqual([it["meta"]["id"] for it in mail.inbox(self.b, "codex@linux")], [m["id"]])

        mail.ack(self.b, "codex@linux", m["id"], "done", "复核完了，见回信")
        r = mail.send(self.b, "codex@linux", [], "reply", "复核结果", "同意，第二式有笔误。", reply_to=m["id"])
        self.assertEqual(r["to"], ["claude@mac"])
        self.assertEqual(r["depth"], 1)
        self.both(self.b, self.a)

        self.assertEqual(mail.inbox(self.b, "codex@linux"), [])
        self.assertEqual([it["meta"]["id"] for it in mail.inbox(self.a, "claude@mac")], [r["id"]])
        meta, body = mail.read(mail.find(self.a, m["id"]))
        self.assertIn("已处理", mail.show_text(self.a, meta, body))

    def test_cannot_write_as_a_seat_on_another_machine(self):
        with self.assertRaises(YizhanError):
            mail.send(self.a, "codex@linux", ["claude@mac"], "request", "冒名", "不该成功")

    def test_ownership_gate_blocks_editing_another_machines_file(self):
        prof = self.b.seat_dir("claude@mac") / "profile.json"
        data = json.loads(prof.read_text(encoding="utf-8"))
        data["duty"] = "被别的机器改了"
        prof.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        head = gitio.head(self.b.root)
        with self.assertRaises(sync.SyncBlocked) as cm:
            sync.sync(self.b, quick=True)
        self.assertIn("seats/claude@mac/profile.json", str(cm.exception))
        self.assertEqual(gitio.head(self.b.root), head, "nothing may be committed")

    def test_project_owner_comes_from_the_committed_file(self):
        path = self.b.project_dir("demo") / "project.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["owner_machine"] = "linux"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(sync.SyncBlocked):
            sync.sync(self.b, quick=True)

    def test_shared_files_need_the_flag(self):
        cfg = self.b.root / "yizhan.json"
        data = json.loads(cfg.read_text(encoding="utf-8"))
        data["max_rounds"] = 3
        cfg.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(sync.SyncBlocked):
            sync.sync(self.b, quick=True)
        self.assertIsNone(sync.sync(self.b, quick=True, allow_shared=True)["error"])

    def test_secret_and_size_gates(self):
        records.log(self.a, "claude@mac", "误贴密钥", "sk-ant-" + "x" * 30)
        with self.assertRaises(sync.SyncBlocked) as cm:
            sync.sync(self.a, quick=True)
        self.assertIn("密钥", str(cm.exception))
        for p in (self.a.seat_dir("claude@mac") / "journal").glob("*.md"):
            p.unlink()
        big = self.a.seat_dir("claude@mac") / "notes.txt"
        big.write_bytes(b"0" * (3 * 1024 * 1024))
        with self.assertRaises(sync.SyncBlocked) as cm:
            sync.sync(self.a, quick=True)
        self.assertIn("MB", str(cm.exception))

    def test_decisions_belong_to_their_author(self):
        d = records.decide(self.b, "codex@linux", "demo", "采样周期定为 1 ms", "控制回路按 1 kHz 运行")
        self.both(self.b, self.a)
        with self.assertRaises(YizhanError):
            records.set_status(self.a, "claude@mac", d["id"], "retracted")
        newer = records.decide(self.a, "claude@mac", "demo", "[修订] 采样周期改为 2 ms", "实测 1 ms 时计算超时",
                               supersedes=[d["id"]])
        self.assertEqual(records.set_status(self.a, "claude@mac", newer["id"], "pending")["status"], "pending")
        found = {x["id"]: x for x in records.decisions(self.a, "demo")}
        self.assertEqual(found[d["id"]]["superseded_by"], newer["id"])
        self.both(self.a, self.b)

    def test_offline_keeps_the_local_commit(self):
        gitio.git(self.a.root, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        records.log(self.a, "claude@mac", "离线也照常记")
        rep = sync.sync(self.a, quick=True)
        self.assertIsNotNone(rep["committed"])
        self.assertIsNotNone(rep["error"])
        text = brief.session_brief(self.a, self.a.seat("claude@mac"), 10_000, remember=False)
        self.assertIn("同步出错", text)

    def test_brief_shows_what_the_other_machine_did(self):
        records.log(self.b, "codex@linux", "推导复核做完一半", "第三节没问题")
        records.set_focus(self.b, "codex@linux", "复核第三节推导")
        records.decide(self.b, "codex@linux", "demo", "仿真步长定为 0.1 ms", "与采样周期分开设")
        mail.send(self.b, "codex@linux", ["claude@mac"], "request", "请看定论", "见 decisions。")
        self.a.add_project("rig", title="实验平台", summary="实验台搭建与数据采集")
        self.both(self.b, self.a)

        prof = self.a.seat("claude@mac")
        text = brief.session_brief(self.a, prof, 10_000, session="s1")
        for needle in (brief.BANNER, "claude@mac", "请看定论", "仿真步长定为 0.1 ms", "复核第三节推导",
                       "推导复核做完一半", "rig", "（你）"):
            self.assertIn(needle, text)
        again = brief.session_brief(self.a, prof, 10_000, session="s2")
        self.assertNotIn("仿真步长定为 0.1 ms", again)        # already shown at the last session start
        self.assertIn("请看定论", again)                  # still unanswered, so still listed

    def test_brief_fits_the_budget(self):
        for i in range(30):
            mail.send(self.b, "codex@linux", ["claude@mac"], "update", f"进展 {i}", "一些进展。" * 20)
        self.both(self.b, self.a)
        text = brief.session_brief(self.a, self.a.seat("claude@mac"), 600, remember=False)
        self.assertLessEqual(len(text), 600)
        self.assertTrue(text.startswith("【驿站 · 会话开始】"))

    def test_project_broadcast(self):
        m = mail.send(self.a, "claude@mac", ["project:demo"], "update", "周报", "本周进展见日志。")
        self.assertEqual(m["to"], ["codex@linux"])
        self.assertFalse(m["wake"])
