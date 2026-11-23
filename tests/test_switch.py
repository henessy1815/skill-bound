"""scripts/switch.py 테스트. 가짜 홈 폴더 안에서만 돈다.

실행 (프로젝트 맨 위 폴더에서):
    /usr/bin/python3 -I -B -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_spec = importlib.util.spec_from_file_location("skill_bound_test_switch", os.path.join(ROOT, "scripts", "switch.py"))
switch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(switch)  # type: ignore
guard = switch.guard

OTHER_HOOK = {"type": "command", "command": "echo other-app-hook"}


class SwitchBase(unittest.TestCase):

    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="sbtest-"))
        self.home = os.path.join(self.base, "home")
        self.installed = os.path.join(self.home, ".claude", "skills", "skill-bound")
        self.state = os.path.join(self.home, ".claude", "skill-bound-state")
        self.tmpdir = os.path.join(self.base, "tmp")
        os.makedirs(os.path.join(self.installed, "hooks"))
        os.makedirs(self.tmpdir)
        self.settings_path = os.path.join(self.home, ".claude", "settings.json")

    def tearDown(self):
        shutil.rmtree(self.base)

    def ctx(self, sb_dir=None):
        return guard.Ctx(home=self.home, sb_dir=sb_dir or self.installed, state_dir=self.state, tmp_dir=self.tmpdir)

    def our_hook(self, arg):
        return {"type": "command", "command": "/usr/bin/python3 -I -B ~/.claude/skills/skill-bound/hooks/guard.py " + arg,
                "timeout": 5}

    def write_settings(self, data):
        with open(self.settings_path, "w") as handle:
            json.dump(data, handle)

    def read_settings(self):
        with open(self.settings_path) as handle:
            return json.load(handle)


class OffOnTest(SwitchBase):

    def test_off_then_on(self):
        ctx = self.ctx()
        self.assertEqual(switch.turn_off(ctx)["status"], "OFF")
        self.assertEqual(guard.get_mode(ctx, "s1")[0], "off")
        self.assertEqual(switch.turn_off(ctx)["status"], "ALREADY_OFF")
        self.assertEqual(switch.turn_on(ctx)["status"], "ON")
        self.assertEqual(guard.get_mode(ctx, "s1")[0], "normal")
        self.assertEqual(switch.turn_on(ctx)["status"], "ALREADY_ON")


class UninstallTest(SwitchBase):

    def test_removes_only_our_hooks(self):
        self.write_settings({
            "model": "opus",
            "hooks": {
                "PreToolUse": [
                    {"matcher": "*", "hooks": [OTHER_HOOK]},
                    {"matcher": "*", "hooks": [self.our_hook("pre")]},
                ],
                "SessionStart": [{"hooks": [self.our_hook("session-start"), OTHER_HOOK]}],
                "Stop": [{"hooks": [OTHER_HOOK]}],
            },
        })
        out = switch.uninstall(self.ctx())
        self.assertEqual(out["hooks_removed"], 2)
        settings = self.read_settings()
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["hooks"], {
            "PreToolUse": [{"matcher": "*", "hooks": [OTHER_HOOK]}],
            "SessionStart": [{"hooks": [OTHER_HOOK]}],
            "Stop": [{"hooks": [OTHER_HOOK]}],
        })
        self.assertTrue(os.path.exists(out["settings_backup"]))

    def test_hooks_key_removed_when_empty(self):
        self.write_settings({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [self.our_hook("pre")]}]}})
        switch.uninstall(self.ctx())
        self.assertNotIn("hooks", self.read_settings())

    def test_no_settings_file(self):
        out = switch.uninstall(self.ctx())
        self.assertEqual((out["status"], out["hooks_removed"], out["settings_backup"]), ("UNINSTALLED", 0, None))

    def test_invalid_settings_changes_nothing(self):
        with open(self.settings_path, "w") as handle:
            handle.write("{broken")
        os.makedirs(self.state)
        with self.assertRaises(switch.SwitchError) as ctx:
            switch.uninstall(self.ctx())
        self.assertEqual(ctx.exception.code, "SETTINGS_INVALID")
        self.assertTrue(os.path.isdir(self.state))
        self.assertTrue(os.path.isdir(self.installed))

    def test_removes_state_folder_and_installed_folder(self):
        os.makedirs(os.path.join(self.state, "baselines"))
        out = switch.uninstall(self.ctx())
        self.assertTrue(out["folder_removed"])
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(os.path.exists(self.installed))

    def test_development_folder_is_never_removed(self):
        dev = os.path.join(self.base, "vscodeprojects", "skill-bound")
        os.makedirs(dev)
        out = switch.uninstall(self.ctx(sb_dir=dev))
        self.assertFalse(out["folder_removed"])
        self.assertTrue(os.path.isdir(dev))
        self.assertTrue(os.path.isdir(self.installed))  # 실행한 곳이 설치본이 아니면 설치본도 건드리지 않는다

    def test_active_quarantine_cleaned_only_in_tmp(self):
        quarantine = tempfile.mkdtemp(prefix="skill-bound-", dir=self.tmpdir)
        guard.write_session_state(self.state, "s1", quarantine)
        keep = os.path.join(self.home, "important")
        os.makedirs(keep)
        guard.write_session_state(self.state, "s2", keep)
        switch.uninstall(self.ctx())
        self.assertFalse(os.path.exists(quarantine))
        self.assertTrue(os.path.isdir(keep))


class CliTest(unittest.TestCase):

    def test_usage(self):
        self.assertEqual(switch.main([]), 2)
        self.assertEqual(switch.main(["delete-everything"]), 2)


if __name__ == "__main__":
    unittest.main()
