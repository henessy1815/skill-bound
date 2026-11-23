"""hooks/guard.py 테스트. 가짜 홈 폴더를 만들어 그 안에서만 판단한다.

실행 (프로젝트 맨 위 폴더에서):
    /usr/bin/python3 -I -B -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GUARD_PATH = os.path.join(ROOT, "hooks", "guard.py")

_spec = importlib.util.spec_from_file_location("skill_bound_test_guard", GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)  # type: ignore

NOW = 1_800_000_000.0


class GuardBase(unittest.TestCase):

    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="sbtest-"))
        self.home = os.path.join(self.base, "home")
        self.sb = os.path.join(self.home, ".claude", "skills", "skill-bound")
        self.state = os.path.join(self.home, ".claude", "skill-bound-state")
        self.tmpdir = os.path.join(self.base, "tmp")
        self.project = os.path.join(self.home, "project")
        self.quarantine = os.path.join(self.tmpdir, "skill-bound-abc123")
        for path in (os.path.join(self.sb, "scripts"), os.path.join(self.sb, "hooks"), self.tmpdir,
                     self.project, os.path.join(self.quarantine, "scripts"), os.path.join(self.home, ".ssh")):
            os.makedirs(path)
        for name in guard.SB_SCRIPTS:
            open(os.path.join(self.sb, "scripts", name), "w").close()
        with open(os.path.join(self.quarantine, "SKILL.md"), "w") as handle:
            handle.write("x\n")
        self.ctx = guard.Ctx(home=self.home, sb_dir=self.sb, state_dir=self.state, tmp_dir=self.tmpdir, now=NOW)
        self.py = guard.SYSTEM_PYTHON
        self.script = lambda name: os.path.join(self.sb, "scripts", name)

    def tearDown(self):
        shutil.rmtree(self.base)

    def decide(self, tool, inp, session="s1", cwd=None):
        event = {"session_id": session, "tool_name": tool, "tool_input": inp, "cwd": cwd or self.project}
        return guard.decide(event, self.ctx)

    def action(self, tool, inp, **kw):
        return self.decide(tool, inp, **kw)[0]

    def bash(self, command, **kw):
        return self.action("Bash", {"command": command}, **kw)

    def start_vet(self, session="s1", started=NOW - 60):
        guard.write_session_state(self.state, session, self.quarantine, now=started)


# ---------------------------------------------------------------------------
# 평소 모드
# ---------------------------------------------------------------------------

class NormalPassTest(GuardBase):
    """평소엔 조용히 통과해야 한다. 귀찮은 도구가 되면 안 된다."""

    def test_ordinary_commands(self):
        for command in ("git status", "npm test", "ls -la", "python3 -m pytest", "git clone https://github.com/a/b",
                        "ls ~/.claude/skills", "cat ~/.claude/settings.json", "grep -r foo ~/.claude/agents",
                        "find ~/.claude/skills -name SKILL.md",
                        "python3 ~/.claude/skills/pdf/scripts/extract.py in.pdf",
                        "bash ~/.claude/skills/x/run.sh", "cat ~/.claude/skills/skill-bound/SKILL.md"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), guard.PASS)

    def test_ordinary_tools(self):
        self.assertEqual(self.action("Write", {"file_path": os.path.join(self.project, "a.ts")}), guard.PASS)
        self.assertEqual(self.action("Edit", {"file_path": "src/b.ts"}), guard.PASS)
        self.assertEqual(self.action("Read", {"file_path": os.path.join(self.home, ".ssh", "id_rsa")}), guard.PASS)
        self.assertEqual(self.action("WebFetch", {"url": "https://example.com"}), guard.PASS)
        self.assertEqual(self.action("mcp__x__y", {}), guard.PASS)
        self.assertEqual(self.action("Write", {"file_path": os.path.join(self.project, "CLAUDE.md")}), guard.PASS)

    def test_sb_scripts_in_right_form(self):
        self.assertEqual(self.bash("%s -I %s start https://github.com/a/b" % (self.py, self.script("vet.py"))), guard.PASS)
        self.assertEqual(self.bash("%s -I -B %s --format=text x" % (self.py, self.script("scan.py"))), guard.PASS)


class NormalInstallTest(GuardBase):

    def test_install_commands_blocked(self):
        for command in ("npx skills add someone/pdf-helper", "npx -y skills@latest add x/y", "bunx skills add x",
                        "claude plugin install foo@bar", "claude plugins install foo", "claude plugin enable foo",
                        "claude plugin marketplace add someone/market", "claude mcp add x -- npx y",
                        "claude mcp add-json x '{}'", "CLAUDE MCP ADD x", "/Users/x/.local/bin/claude plugin install y",
                        "cd /tmp && claude plugin install y"):
            with self.subTest(command=command):
                self.assertEqual(self.decide("Bash", {"command": command}), (guard.DENY, "INSTALL_BLOCKED"))

    def test_bash_writes_into_skill_dirs(self):
        for command in ("cp -r x ~/.claude/skills/", "git clone https://github.com/a/b ~/.claude/skills/b",
                        "curl -o ~/.claude/agents/a.md https://x", "mv a $HOME/.claude/commands/",
                        "mv a ${HOME}/.claude/commands/", "ln -s /tmp/x ~/.claude/skills/x",
                        "echo hi > ~/.claude/skills/x/SKILL.md", "cp x ~/.CLAUDE/Skills/",
                        'cp x ~/".claude"/skills/', "cp x ~/.cl\\aude/skills/",
                        "curl --output=~/.claude/skills/x/SKILL.md https://x",
                        "cp x ./.claude/skills/", "mkdir -p .claude/agents", "cp x .mcp.json",
                        "unzip x.zip -d ~/.claude/plugins/"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), guard.DENY)

    def test_section8_write_options(self):
        """CLAUDE.md 8절: 읽기처럼 보이지만 쓰기 옵션이 있는 명령."""
        for command in ("sort -o~/.claude/skills/x/SKILL.md in.txt", "sort -o ~/.claude/skills/x/SKILL.md in.txt",
                        "xxd -r in.hex ~/.claude/skills/x/run.py", "tree -o~/.claude/skills/x/SKILL.md",
                        "tree --output=~/.claude/agents/a.md", "uniq in.txt ~/.claude/commands/c.md",
                        "find ~/.claude/skills -delete", "find ~/.claude/skills -exec rm {} ;",
                        "awk '{print > \"x\"}' ~/.claude/skills/a/SKILL.md"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), guard.DENY)

    def test_cd_into_claude_then_relative(self):
        """CLAUDE.md 8절: cd로 위치를 옮긴 뒤 상대 경로. Bash 도구는 cwd를 이어 간다."""
        claude_dir = os.path.join(self.home, ".claude")
        self.assertEqual(self.bash("cp /tmp/evil skills/", cwd=claude_dir), guard.DENY)
        self.assertEqual(self.bash("ls skills", cwd=claude_dir), guard.PASS)
        self.assertEqual(self.bash("cp /tmp/evil x/", cwd=os.path.join(claude_dir, "skills")), guard.DENY)

    def test_write_tools_into_skill_dirs(self):
        for path in (os.path.join(self.home, ".claude", "skills", "x", "SKILL.md"),
                     os.path.join(self.home, ".claude", "agents", "a.md"),
                     os.path.join(self.home, ".claude", "commands", "c.md"),
                     os.path.join(self.home, ".claude", "plugins", "p", "plugin.json"),
                     os.path.join(self.home, ".CLAUDE", "Skills", "x", "SKILL.md"),
                     os.path.join(self.project, ".claude", "skills", "x", "SKILL.md"),
                     os.path.join(self.project, ".mcp.json")):
            with self.subTest(path=path):
                self.assertEqual(self.decide("Write", {"file_path": path}), (guard.DENY, "INSTALL_BLOCKED"))
        self.assertEqual(self.action("Write", {"file_path": "~/.claude/skills/x/SKILL.md"}), guard.DENY)
        self.assertEqual(self.action("NotebookEdit", {"notebook_path": os.path.join(self.home, ".claude", "skills", "a.ipynb")}),
                         guard.DENY)

    def test_config_files(self):
        for path in (os.path.join(self.home, ".claude.json"), os.path.join(self.home, ".claude", "hooks", "h.py")):
            with self.subTest(path=path):
                self.assertEqual(self.action("Write", {"file_path": path}), guard.DENY)
        for path in (os.path.join(self.home, ".claude", "settings.json"),
                     os.path.join(self.project, ".claude", "settings.local.json")):
            with self.subTest(path=path):
                self.assertEqual(self.decide("Edit", {"file_path": path}), (guard.ASK, "SETTINGS_ASK"))
        self.assertEqual(self.bash("echo {} > ~/.claude/settings.json"), guard.DENY)

    def test_symlink_into_skill_dir(self):
        target = os.path.join(self.home, ".claude", "skills")
        os.symlink(target, os.path.join(self.project, "link"))
        self.assertEqual(self.action("Write", {"file_path": os.path.join(self.project, "link", "x", "SKILL.md")}),
                         guard.DENY)


class NormalSelfProtectTest(GuardBase):

    def test_bash_cannot_touch_self_or_state(self):
        for command in ("rm -rf ~/.claude/skills/skill-bound", "touch ~/.claude/skill-bound-state/OFF",
                        "rm ~/.claude/skill-bound-state/sessions/s1.json",
                        "cp evil.py %s" % os.path.join(self.sb, "hooks", "guard.py"),
                        "echo x > %s" % os.path.join(self.sb, "scripts", "scan.py"),
                        "sed -i '' s/a/b/ ~/.claude/skills/skill-bound/hooks/guard.py",
                        "python3 -c pass ~/.claude/skill-bound-state"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), guard.DENY)

    def test_wrong_form_of_sb_script(self):
        for command in ("python3 -I %s x" % self.script("scan.py"),
                        "%s %s x" % (self.py, self.script("scan.py")),
                        "%s -I %s x; rm -rf ~" % (self.py, self.script("scan.py")),
                        "%s -I %s $HOME" % (self.py, self.script("scan.py")),
                        "CLAUDE_CODE_SESSION_ID=other %s -I %s start x" % (self.py, self.script("vet.py"))):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), guard.DENY)

    def test_write_tools_cannot_touch_self_or_state(self):
        for path in (os.path.join(self.sb, "hooks", "guard.py"), os.path.join(self.sb, "SKILL.md"),
                     os.path.join(self.state, "OFF"), os.path.join(self.state, "sessions", "s1.json"),
                     os.path.join(self.home, ".claude", "SKILLS", "skill-bound", "x")):
            with self.subTest(path=path):
                self.assertEqual(self.decide("Write", {"file_path": path}), (guard.DENY, "PROTECTED"))

    def test_install_checked_asks(self):
        command = "%s -I %s %s" % (self.py, self.script("install_checked.py"), self.quarantine)
        self.assertEqual(self.decide("Bash", {"command": command}), (guard.ASK, "INSTALL_ASK"))
        self.assertEqual(self.bash(command + " ; echo done"), guard.DENY)


class SwitchCommandTest(GuardBase):
    """끄기와 지우기는 금지가 아니라 사람의 허락 한 번. 켜기는 묻지 않는다."""

    def switch(self, arg):
        return "%s -I -B %s %s" % (self.py, self.script("switch.py"), arg)

    def test_off_and_uninstall_ask(self):
        self.assertEqual(self.decide("Bash", {"command": self.switch("off")}), (guard.ASK, "SWITCH_OFF_ASK"))
        self.assertEqual(self.decide("Bash", {"command": self.switch("uninstall")}), (guard.ASK, "UNINSTALL_ASK"))

    def test_on_passes(self):
        self.assertEqual(self.decide("Bash", {"command": self.switch("on")}), (guard.PASS, "OK"))

    def test_same_in_vet_mode(self):
        self.start_vet()
        self.assertEqual(self.bash(self.switch("off")), guard.ASK)
        self.assertEqual(self.bash(self.switch("uninstall")), guard.ASK)
        self.assertEqual(self.bash(self.switch("on")), guard.PASS)

    def test_allowed_when_state_broken(self):
        os.makedirs(os.path.join(self.state, "sessions"))
        with open(os.path.join(self.state, "sessions", "s1.json"), "w") as handle:
            handle.write("{broken")
        self.assertEqual(self.bash(self.switch("off")), guard.ASK)

    def test_direct_ways_still_denied_with_hint(self):
        action, code = self.decide("Bash", {"command": "touch ~/.claude/skill-bound-state/OFF"})
        self.assertEqual((action, code), (guard.DENY, "PROTECTED"))
        self.assertIn("switch.py", guard.MESSAGES["PROTECTED"])
        self.assertEqual(self.action("Write", {"file_path": os.path.join(self.state, "OFF")}), guard.DENY)
        self.assertEqual(self.bash("rm -rf ~/.claude/skills/skill-bound"), guard.DENY)

    def test_tricks_with_switch_denied(self):
        for command in (self.switch("off") + "; rm -rf ~", self.switch("off off"),
                        "python3 %s off" % self.script("switch.py"), self.switch("'off'")):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), guard.DENY)


# ---------------------------------------------------------------------------
# 점검 모드
# ---------------------------------------------------------------------------

class VetModeTest(GuardBase):

    def setUp(self):
        GuardBase.setUp(self)
        self.start_vet()

    def test_mode(self):
        self.assertEqual(guard.get_mode(self.ctx, "s1"), ("vet", self.quarantine))
        self.assertEqual(guard.get_mode(self.ctx, "s2")[0], "normal")

    def test_bash_only_sb_scripts(self):
        ok = "%s -I %s %s" % (self.py, self.script("scan.py"), self.quarantine)
        self.assertEqual(self.bash(ok), guard.PASS)
        self.assertEqual(self.bash("%s -I -B %s end" % (self.py, self.script("vet.py"))), guard.PASS)
        for command in ("ls", "cat SKILL.md", "git status", "cd", "cd -", "python3 -I %s x" % self.script("scan.py"),
                        "%s %s x" % (self.py, self.script("scan.py")),
                        "%s -I %s/scripts/../scripts/scan.py x" % (self.py, self.sb),
                        "%s -I -c print(1)" % self.py, "%s -I %s ~" % (self.py, self.script("scan.py")),
                        "%s -I  %s x" % (self.py, self.script("scan.py")),
                        "%s -I %s x\nrm -rf ~" % (self.py, self.script("scan.py")),
                        "%s -I %s 'x'" % (self.py, self.script("scan.py")),
                        "%s -I %s x`id`" % (self.py, self.script("scan.py")),
                        "%s -I %s/evil.py" % (self.py, self.quarantine),
                        "/usr/bin/python3.9 -I %s x" % self.script("scan.py")):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), guard.DENY)

    def test_install_checked_still_asks(self):
        command = "%s -I %s %s" % (self.py, self.script("install_checked.py"), self.quarantine)
        self.assertEqual(self.bash(command), guard.ASK)

    def test_read(self):
        self.assertEqual(self.action("Read", {"file_path": os.path.join(self.quarantine, "SKILL.md")}), guard.PASS)
        self.assertEqual(self.action("Read", {"file_path": os.path.join(self.sb, "scripts", "scan.py")}), guard.PASS)
        for path in (os.path.join(self.home, ".ssh", "id_rsa"), os.path.join(self.quarantine, "..", "x"),
                     os.path.join(self.project, "a.ts"), "~/.ssh/id_rsa", self.quarantine + "-evil/x", ""):
            with self.subTest(path=path):
                self.assertEqual(self.action("Read", {"file_path": path}), guard.DENY)

    def test_read_symlink_out_of_quarantine(self):
        os.symlink(os.path.join(self.home, ".ssh"), os.path.join(self.quarantine, "keys"))
        self.assertEqual(self.action("Read", {"file_path": os.path.join(self.quarantine, "keys", "id_rsa")}), guard.DENY)

    def test_glob_grep(self):
        self.assertEqual(self.action("Glob", {"pattern": "**/*", "path": self.quarantine}), guard.PASS)
        self.assertEqual(self.action("Glob", {"pattern": "**/*"}, cwd=self.quarantine), guard.PASS)
        self.assertEqual(self.action("Glob", {"pattern": "**/*"}), guard.DENY)  # cwd가 프로젝트
        self.assertEqual(self.action("Glob", {"pattern": "/etc/*", "path": self.quarantine}), guard.DENY)
        self.assertEqual(self.action("Glob", {"pattern": "../../*", "path": self.quarantine}), guard.DENY)
        self.assertEqual(self.action("Grep", {"pattern": "curl", "path": self.sb}), guard.PASS)
        self.assertEqual(self.action("Grep", {"pattern": "x", "path": self.quarantine, "glob": "../*"}), guard.DENY)
        self.assertEqual(self.action("Grep", {"pattern": "x", "path": self.home}), guard.DENY)

    def test_write(self):
        self.assertEqual(self.action("Write", {"file_path": os.path.join(self.quarantine, "notes.md")}), guard.PASS)
        for path in (os.path.join(self.sb, "x"), os.path.join(self.project, "a.ts"), self.quarantine,
                     os.path.join(self.state, "OFF"), os.path.join(self.quarantine, "..", "y")):
            with self.subTest(path=path):
                self.assertEqual(self.action("Write", {"file_path": path}), guard.DENY)

    def test_other_tools(self):
        for tool in ("WebFetch", "WebSearch", "mcp__github__create_issue", "mcp__ccd_turn__update_plan",
                     "Agent", "Task", "Skill", "NewUnknownTool"):
            with self.subTest(tool=tool):
                self.assertEqual(self.decide(tool, {}), (guard.DENY, "VET_TOOL"))
        self.assertEqual(self.action("TodoWrite", {}), guard.PASS)

    def test_expired(self):
        self.start_vet(started=NOW - guard.VET_MAX_SECONDS - 1)
        self.assertEqual(guard.get_mode(self.ctx, "s1")[0], "normal")

    def test_future_start_time_is_not_trusted(self):
        self.start_vet(started=NOW + 3600)
        self.assertEqual(guard.get_mode(self.ctx, "s1")[0], "normal")

    def test_quarantine_gone(self):
        shutil.rmtree(self.quarantine)
        self.assertEqual(guard.get_mode(self.ctx, "s1")[0], "normal")

    def test_quarantine_must_be_in_tmp(self):
        guard.write_session_state(self.state, "s1", self.project, now=NOW)
        self.assertEqual(guard.get_mode(self.ctx, "s1")[0], "normal")

    def test_clear(self):
        guard.clear_session_state(self.state, "s1")
        self.assertEqual(guard.get_mode(self.ctx, "s1")[0], "normal")


class CardDisplayTest(GuardBase):
    """버튼(AskUserQuestion)이 뜨기 직전에 훅이 요약 카드를 화면에 직접 보여 준다."""

    def event(self, tool="AskUserQuestion", session="s1"):
        return {"session_id": session, "tool_name": tool, "tool_input": {}, "cwd": self.project}

    def test_card_shown_before_buttons_in_vet_mode(self):
        self.start_vet()
        guard.save_card(self.state, self.quarantine, "\U0001F534 설치 비권장\n  a.py:1   x")
        message = guard.card_message(self.event(), self.ctx)
        self.assertIn("\U0001F534 설치 비권장", message)
        self.assertTrue(message.startswith("skill-bound 점검 결과"))

    def test_no_card_outside_vet_or_other_tools(self):
        guard.save_card(self.state, self.quarantine, "card")
        self.assertIsNone(guard.card_message(self.event(), self.ctx))  # 평소 모드
        self.start_vet()
        self.assertIsNone(guard.card_message(self.event(tool="Bash"), self.ctx))
        self.assertIsNone(guard.card_message(self.event(session="other"), self.ctx))

    def test_no_card_file(self):
        self.start_vet()
        self.assertIsNone(guard.card_message(self.event(), self.ctx))

    def run_main_with(self, event):
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(event).encode()))
        out = io.StringIO()
        old = sys.stdin
        sys.stdin = stdin
        try:
            with contextlib.redirect_stdout(out):
                guard.main_pre(self.ctx)
        finally:
            sys.stdin = old
        text = out.getvalue().strip()
        return json.loads(text) if text else None

    def ask_event(self, question="이 스킬을 설치할까요?"):
        event = self.event()
        event["tool_input"] = {"questions": [{"question": question, "header": "설치", "multiSelect": False,
                                              "options": [{"label": "설치 안 함 (추천)", "description": "d"},
                                                          {"label": "그래도 설치", "description": "d"}]}]}
        return event

    def test_card_put_on_top_of_question(self):
        self.start_vet()
        guard.save_card(self.state, self.quarantine, "CARD-TEXT")
        out = self.run_main_with(self.ask_event())["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "allow")
        q = out["updatedInput"]["questions"][0]
        self.assertTrue(q["question"].startswith(guard.CARD_TITLE))
        self.assertIn("CARD-TEXT", q["question"])
        self.assertTrue(q["question"].endswith("이 스킬을 설치할까요?"))
        self.assertEqual(q["options"][0]["label"], "설치 안 함 (추천)")  # 선택지는 그대로
        self.assertEqual(q["header"], "설치")

    def test_card_not_added_twice(self):
        self.start_vet()
        guard.save_card(self.state, self.quarantine, "CARD-TEXT")
        self.assertIsNone(self.run_main_with(self.ask_event(guard.CARD_TITLE + "\n\nCARD-TEXT\n\nq")))

    def test_odd_input_left_alone(self):
        self.start_vet()
        guard.save_card(self.state, self.quarantine, "CARD-TEXT")
        event = self.event()
        event["tool_input"] = {"questions": "not a list"}
        self.assertIsNone(self.run_main_with(event))

    def test_normal_mode_question_untouched(self):
        guard.save_card(self.state, self.quarantine, "CARD-TEXT")
        self.assertIsNone(self.run_main_with(self.ask_event()))

    def test_remove_card(self):
        guard.save_card(self.state, self.quarantine, "x")
        guard.remove_card(self.state, self.quarantine)
        self.assertIsNone(guard.load_card(self.state, self.quarantine))


class BrokenStateTest(GuardBase):

    def setUp(self):
        GuardBase.setUp(self)
        os.makedirs(os.path.join(self.state, "sessions"))
        with open(os.path.join(self.state, "sessions", "s1.json"), "w") as handle:
            handle.write("{not json")

    def test_broken_blocks_like_vet(self):
        self.assertEqual(guard.get_mode(self.ctx, "s1")[0], "broken")
        self.assertEqual(self.decide("Read", {"file_path": os.path.join(self.project, "a")}), (guard.DENY, "VET_BROKEN"))
        self.assertEqual(self.bash("%s -I %s x" % (self.py, self.script("scan.py"))), guard.DENY)
        self.assertEqual(self.bash("%s -I %s end" % (self.py, self.script("vet.py"))), guard.PASS)

    def test_bad_types(self):
        with open(os.path.join(self.state, "sessions", "s1.json"), "w") as handle:
            json.dump({"quarantine": 1, "started": "x"}, handle)
        self.assertEqual(guard.get_mode(self.ctx, "s1")[0], "broken")


class OffTest(GuardBase):

    def test_off_passes_everything(self):
        os.makedirs(self.state)
        open(os.path.join(self.state, "OFF"), "w").close()
        self.start_vet()
        self.assertEqual(self.bash("claude plugin install x"), guard.PASS)
        self.assertEqual(self.action("Write", {"file_path": os.path.join(self.sb, "x")}), guard.PASS)
        self.assertEqual(self.action("WebFetch", {}), guard.PASS)


# ---------------------------------------------------------------------------
# 진입점 (깨진 입력 등)
# ---------------------------------------------------------------------------

class MainTest(GuardBase):

    def run_main(self, raw):
        stdin = io.TextIOWrapper(io.BytesIO(raw))
        out = io.StringIO()
        old = sys.stdin
        sys.stdin = stdin
        try:
            with contextlib.redirect_stdout(out):
                code = guard.main_pre(self.ctx)
        finally:
            sys.stdin = old
        self.assertEqual(code, 0)
        text = out.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"]["permissionDecision"] if text else "pass"

    def test_garbage_without_vet_passes(self):
        self.assertEqual(self.run_main(b"not json"), "pass")

    def test_garbage_with_vet_denies(self):
        self.start_vet(session="other")
        self.assertEqual(self.run_main(b"not json"), "deny")

    def test_valid_event(self):
        event = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "claude plugin install x"}, "cwd": "/"}
        self.assertEqual(self.run_main(json.dumps(event).encode()), "deny")
        event["tool_input"] = {"command": "git status"}
        self.assertEqual(self.run_main(json.dumps(event).encode()), "pass")

    def test_crash_in_vet_mode_denies(self):
        self.start_vet()
        original = guard.decide_vet

        def boom(*_args):
            raise RuntimeError("boom")
        guard.decide_vet = boom
        try:
            event = {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": "x"}, "cwd": "/"}
            self.assertEqual(self.run_main(json.dumps(event).encode()), "deny")
        finally:
            guard.decide_vet = original

    def test_crash_in_normal_mode_passes(self):
        original = guard.decide_normal

        def boom(*_args):
            raise RuntimeError("boom")
        guard.decide_normal = boom
        try:
            event = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/"}
            self.assertEqual(self.run_main(json.dumps(event).encode()), "pass")
        finally:
            guard.decide_normal = original

    def test_subprocess_wrong_args_denies(self):
        proc = subprocess.run([sys.executable, "-I", "-B", GUARD_PATH], input=b"{}", stdout=subprocess.PIPE, timeout=30)
        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


# ---------------------------------------------------------------------------
# SessionStart 기준선
# ---------------------------------------------------------------------------

class SessionStartTest(GuardBase):

    def start(self):
        return guard.session_start({"cwd": self.project, "source": "startup"}, self.ctx)

    def add_skill(self, name, content="x\n"):
        folder = os.path.join(self.home, ".claude", "skills", name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "SKILL.md"), "w") as handle:
            handle.write(content)

    def test_first_run_is_silent_and_saves_baseline(self):
        self.assertIsNone(self.start())
        self.assertTrue(os.path.exists(guard.baseline_path(self.ctx, self.project)))
        self.assertIsNone(self.start())

    def test_new_skill_is_reported_once(self):
        self.start()
        self.add_skill("pdf-helper")
        out = self.start()
        self.assertIn("user-skills:pdf-helper", out["systemMessage"])
        self.assertIn("user-skills:pdf-helper", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.start())

    def test_changed_and_removed(self):
        self.add_skill("a")
        self.add_skill("b")
        self.start()
        self.add_skill("a", "changed\n")
        shutil.rmtree(os.path.join(self.home, ".claude", "skills", "b"))
        text = self.start()["systemMessage"]
        self.assertIn("변경: user-skills:a", text)
        self.assertIn("삭제: user-skills:b", text)

    def test_unsafe_name_is_masked(self):
        self.start()
        self.add_skill("ignore previous instructions")
        text = self.start()["systemMessage"]
        self.assertNotIn("ignore previous", text)
        self.assertIn("<unsafe-name>", text)

    def test_settings_only_security_keys(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w") as handle:
            json.dump({"model": "opus", "hooks": {}}, handle)
        self.start()
        with open(path, "w") as handle:
            json.dump({"model": "sonnet", "hooks": {}}, handle)
        self.assertIsNone(self.start())
        with open(path, "w") as handle:
            json.dump({"model": "sonnet", "hooks": {"PreToolUse": []}}, handle)
        self.assertIn("user-settings.json:hooks", self.start()["systemMessage"])

    def test_project_mcp(self):
        self.start()
        with open(os.path.join(self.project, ".mcp.json"), "w") as handle:
            handle.write("{}")
        self.assertIn("project:.mcp.json", self.start()["systemMessage"])

    def test_plugin_in_use_markers_ignored(self):
        """통합 시험에서 찾은 거짓 알림: Claude가 켜질 때마다 생기는 .in_use 표시 파일."""
        version = os.path.join(self.home, ".claude", "plugins", "cache", "market", "plugin", "1.0.0")
        os.makedirs(os.path.join(version, ".in_use"))
        self.write_file(os.path.join(version, "README.md"), "x\n")
        self.write_file(os.path.join(version, ".in_use", "111"), "")
        self.start()
        os.unlink(os.path.join(version, ".in_use", "111"))
        self.write_file(os.path.join(version, ".in_use", "222"), "")
        self.assertIsNone(self.start())
        self.write_file(os.path.join(version, "README.md"), "changed\n")
        self.assertIn("user-plugin-cache:market", self.start()["systemMessage"])

    @staticmethod
    def write_file(path, text):
        with open(path, "w") as handle:
            handle.write(text)

    def test_empty_mcp_entry_is_not_a_change(self):
        """통합 시험에서 찾은 거짓 알림: 새 폴더를 믿으면 Claude가 빈 mcpServers 칸을 만든다."""
        claude_json = os.path.join(self.home, ".claude.json")
        with open(claude_json, "w") as handle:
            json.dump({"projects": {}}, handle)
        self.start()
        with open(claude_json, "w") as handle:
            json.dump({"mcpServers": {}, "projects": {self.project: {"mcpServers": {}, "allowedTools": []}}}, handle)
        self.assertIsNone(self.start())
        with open(claude_json, "w") as handle:
            json.dump({"projects": {self.project: {"mcpServers": {"x": {"command": "npx"}}}}}, handle)
        self.assertIn("project-claude.json:mcpServers", self.start()["systemMessage"])

    def test_off_is_silent(self):
        self.start()
        self.add_skill("x")
        os.makedirs(self.state, exist_ok=True)
        open(os.path.join(self.state, "OFF"), "w").close()
        self.assertIsNone(self.start())


if __name__ == "__main__":
    unittest.main()
