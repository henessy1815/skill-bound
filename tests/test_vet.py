"""vet.py, install_checked.py 테스트. 가짜 홈과 가짜 임시 폴더, 로컬 git 저장소만 쓴다.

실행 (프로젝트 맨 위 폴더에서):
    /usr/bin/python3 -I -B -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")


def _load(name):
    spec = importlib.util.spec_from_file_location("skill_bound_test_" + name, os.path.join(SCRIPTS, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore
    return module


vet = _load("vet")
installer = _load("install_checked")
guard = vet.guard

NOW = 1_800_000_000.0
SID = "test-session-1"


class ParseSourceTest(unittest.TestCase):

    def test_github_forms(self):
        for text in ("github.com/someone/pdf-helper", "https://github.com/someone/pdf-helper",
                     "github.com/someone/pdf-helper.git", "github.com/someone/pdf-helper/"):
            with self.subTest(text=text):
                src = vet.parse_source(text)
                self.assertEqual((src["owner"], src["repo"], src["ref"], src["path"]),
                                 ("someone", "pdf-helper", None, ""))

    def test_tree_subpath(self):
        src = vet.parse_source("github.com/anthropics/skills/tree/main/skills/pdf")
        self.assertEqual((src["ref"], src["path"]), ("main", "skills/pdf"))
        self.assertEqual(vet.skill_name_for(src), "pdf")

    def test_local(self):
        self.assertEqual(vet.parse_source("/Users/x/Downloads/my-skill")["kind"], "local")

    def test_rejected(self):
        for text in ("", "gitlab.com/a/b", "http://github.com/a/b", "github.com/a", "github.com/../b",
                     "github.com/a/b/blob/main/x", "github.com/a/b/tree/main/../x", "github.com/a/b/tree/../x",
                     "github.com/a/b/tree/main/.git", "github.com/a//b", "github.com/-a/b",
                     "github.com/a/b?x=1", "github.com/a/b;rm", "ssh://github.com/a/b", "github.com/a/b/tree",
                     "relative/path", "~/skills/x", "github.com/a/" + "b" * 200):
            with self.subTest(text=text):
                with self.assertRaises(vet.VetError):
                    vet.parse_source(text)

    def test_bad_name(self):
        with self.assertRaises(vet.VetError):
            vet.skill_name_for({"kind": "local", "path": "/tmp/.hidden"})


class SafeGitTest(unittest.TestCase):
    """통합 시험에서 맥 키체인 암호 창이 뜬 문제: 남의 저장소를 받을 때 내 git 설정을 쓰지 않는다."""

    def test_env_ignores_system_and_user_git_config(self):
        env = vet.safe_git_env({"PATH": "/usr/bin", "HOME": "/x", "GIT_DIR": "/evil", "GIT_CONFIG_COUNT": "1",
                                "GIT_SSH_COMMAND": "evil"})
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        for key in ("GIT_DIR", "GIT_CONFIG_COUNT", "GIT_SSH_COMMAND"):
            self.assertNotIn(key, env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_credential_helper_disabled(self):
        self.assertIn("credential.helper=", vet.SAFE_GIT_CONFIG)

    @unittest.skipIf(vet.git_binary() is None, "git 없음")
    def test_git_sees_only_our_settings(self):
        cmd = [vet.git_binary()]
        for item in vet.SAFE_GIT_CONFIG:
            cmd += ["-c", item]
        cmd += ["config", "--list", "--show-origin"]
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=vet.safe_git_env(),
                             cwd=tempfile.gettempdir(), universal_newlines=True).stdout
        origins = {line.split("\t")[0] for line in out.strip().split("\n") if line}
        self.assertEqual(origins, {"command line:"})
        self.assertNotIn("osxkeychain", out)


class VetBase(unittest.TestCase):

    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="sbtest-"))
        self.home = os.path.join(self.base, "home")
        self.tmpdir = os.path.join(self.base, "tmp")
        self.state = os.path.join(self.home, ".claude", "skill-bound-state")
        os.makedirs(self.tmpdir)
        os.makedirs(os.path.join(self.home, ".claude"))
        self.ctx = guard.Ctx(home=self.home, sb_dir=os.path.join(self.home, "sb"), state_dir=self.state,
                             tmp_dir=self.tmpdir, now=NOW)
        # 로컬 스킬 폴더
        self.local = os.path.join(self.base, "downloads", "my-skill")
        os.makedirs(os.path.join(self.local, "scripts"))
        self.write(os.path.join(self.local, "SKILL.md"), "---\nname: my-skill\n---\nhello\n")
        self.write(os.path.join(self.local, "scripts", "run.py"), "print(1)\n")
        os.chmod(os.path.join(self.local, "scripts", "run.py"), 0o755)

    def tearDown(self):
        shutil.rmtree(self.base)

    @staticmethod
    def write(path, text):
        with open(path, "w") as handle:
            handle.write(text)

    def start_local(self, path=None):
        return vet.start(path or self.local, SID, self.ctx)


class StartEndTest(VetBase):

    def test_start_local(self):
        out = self.start_local()
        q = out["quarantine"]
        self.assertEqual(out["status"], "STARTED")
        self.assertEqual(out["name"], "my-skill")
        self.assertEqual(out["files"], 2)
        self.assertTrue(os.path.basename(q).startswith("skill-bound-"))
        self.assertEqual(os.path.dirname(q), self.tmpdir)
        self.assertEqual(guard.get_mode(self.ctx, SID), ("vet", q))
        manifest = vet.load_manifest(self.ctx, q)
        self.assertEqual(sorted(manifest["files"]), ["SKILL.md", "scripts/run.py"])
        self.assertEqual(stat.S_IMODE(os.stat(q).st_mode), 0o700)

    def test_no_session(self):
        with self.assertRaises(vet.VetError) as ctx:
            vet.start(self.local, None, self.ctx)
        self.assertEqual(ctx.exception.code, "NO_SESSION")
        self.assertEqual(vet.current_session_id({}), None)
        self.assertEqual(vet.current_session_id({"CLAUDE_CODE_SESSION_ID": "bad id;"}), None)
        self.assertEqual(vet.current_session_id({"CLAUDE_CODE_SESSION_ID": SID}), SID)

    def test_double_start(self):
        self.start_local()
        with self.assertRaises(vet.VetError) as ctx:
            self.start_local()
        self.assertEqual(ctx.exception.code, "ALREADY_VETTING")

    def test_off(self):
        os.makedirs(self.state)
        open(os.path.join(self.state, "OFF"), "w").close()
        with self.assertRaises(vet.VetError) as ctx:
            self.start_local()
        self.assertEqual(ctx.exception.code, "SKILL_BOUND_OFF")

    def test_failure_leaves_nothing(self):
        with self.assertRaises(vet.VetError):
            vet.start("/nonexistent/folder", SID, self.ctx)
        self.assertEqual(os.listdir(self.tmpdir), [])
        self.assertEqual(guard.get_mode(self.ctx, SID)[0], "normal")

    def test_local_symlink_kept_as_link_and_not_followed(self):
        secret_dir = os.path.join(self.base, "secret")
        os.makedirs(secret_dir)
        self.write(os.path.join(secret_dir, "id_rsa"), "KEY\n")
        os.symlink(secret_dir, os.path.join(self.local, "keys"))
        q = self.start_local()["quarantine"]
        self.assertTrue(os.path.islink(os.path.join(q, "keys")))
        manifest = vet.load_manifest(self.ctx, q)
        self.assertEqual(manifest["others"], ["keys"])
        self.assertNotIn("keys/id_rsa", manifest["files"])

    def test_local_fifo_rejected(self):
        os.mkfifo(os.path.join(self.local, "pipe"))
        with self.assertRaises(vet.VetError) as ctx:
            self.start_local()
        self.assertEqual(ctx.exception.code, "SPECIAL_FILE")
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_end(self):
        q = self.start_local()["quarantine"]
        out = vet.end(SID, self.ctx)
        self.assertEqual(out["status"], "ENDED")
        self.assertFalse(os.path.exists(q))
        self.assertIsNone(vet.load_manifest(self.ctx, q))
        self.assertEqual(guard.get_mode(self.ctx, SID)[0], "normal")
        self.assertEqual(vet.end(SID, self.ctx)["status"], "NOTHING_TO_END")

    def test_end_broken_state(self):
        os.makedirs(os.path.join(self.state, "sessions"))
        self.write(guard.session_state_path(self.state, SID), "{broken")
        self.assertEqual(vet.end(SID, self.ctx)["status"], "ENDED")
        self.assertEqual(guard.get_mode(self.ctx, SID)[0], "normal")

    def test_end_never_deletes_outside_tmp(self):
        keep = os.path.join(self.home, "important")
        os.makedirs(keep)
        guard.write_session_state(self.state, SID, keep, now=NOW)
        vet.end(SID, self.ctx)
        self.assertTrue(os.path.isdir(keep))

    def test_status(self):
        self.assertEqual(vet.status(SID, self.ctx)["status"], "IDLE")
        self.start_local()
        out = vet.status(SID, self.ctx)
        self.assertEqual(out["status"], "VETTING")
        self.assertEqual(out["minutes_left"], 120)


@unittest.skipIf(vet.git_binary() is None, "git 없음")
class GitFetchTest(VetBase):
    """인터넷 없이 로컬 git 저장소로 받기를 시험한다. file:// 은 테스트에서만 허용한다."""

    def setUp(self):
        VetBase.setUp(self)
        self.repo = os.path.join(self.base, "repo")
        os.makedirs(os.path.join(self.repo, "skills", "pdf"))
        self.write(os.path.join(self.repo, "README.md"), "root\n")
        self.write(os.path.join(self.repo, "skills", "pdf", "SKILL.md"), "---\nname: pdf\n---\n")
        os.symlink(os.path.join(self.home, ".ssh"), os.path.join(self.repo, "skills", "pdf", "keys"))
        git = vet.git_binary()
        quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([git, "init", "-q", "-b", "main", self.repo], check=True, **quiet)
        subprocess.run([git, "-C", self.repo, "add", "-A"], check=True, **quiet)
        subprocess.run([git, "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "-q", "-m", "init"], check=True, **quiet)
        self.url = "file://" + self.repo
        self.extra = ["protocol.file.allow=always"]

    def start_git(self, source):
        return vet.start(source, SID, self.ctx, extra_git_config=self.extra, url_override=self.url)

    def test_clone_whole_repo(self):
        q = self.start_git("github.com/someone/repo")["quarantine"]
        self.assertTrue(os.path.isfile(os.path.join(q, "README.md")))
        self.assertFalse(os.path.exists(os.path.join(q, ".git")))
        self.assertFalse(os.path.exists(os.path.join(q, ".skill-bound-fetch")))

    def test_clone_subpath_and_symlink_becomes_file(self):
        out = self.start_git("github.com/someone/repo/tree/main/skills/pdf")
        q = out["quarantine"]
        self.assertEqual(out["name"], "pdf")
        self.assertEqual(sorted(os.listdir(q)), ["SKILL.md", "keys"])
        keys = os.path.join(q, "keys")
        self.assertFalse(os.path.islink(keys))
        self.assertTrue(os.path.isfile(keys))  # core.symlinks=false: 링크 대상 경로가 적힌 글 파일

    def test_missing_subpath(self):
        with self.assertRaises(vet.VetError) as ctx:
            self.start_git("github.com/someone/repo/tree/main/nope")
        self.assertEqual(ctx.exception.code, "SUBPATH_NOT_FOUND")
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_file_protocol_blocked_by_default(self):
        """기본 설정(https만)으로는 file:// 주소를 받지 않는다."""
        with self.assertRaises(vet.VetError) as ctx:
            vet.start("github.com/someone/repo", SID, self.ctx, url_override=self.url)
        self.assertEqual(ctx.exception.code, "FETCH_FAILED")

    def test_bad_branch(self):
        with self.assertRaises(vet.VetError) as ctx:
            self.start_git("github.com/someone/repo/tree/nope/skills")
        self.assertEqual(ctx.exception.code, "FETCH_FAILED")


class InstallTest(VetBase):

    def setUp(self):
        VetBase.setUp(self)
        self.q = self.start_local()["quarantine"]
        self.target = os.path.join(self.home, ".claude", "skills", "my-skill")

    def assert_install_error(self, code, quarantine=None):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install(quarantine or self.q, SID, self.ctx)
        self.assertEqual(ctx.exception.code, code)
        self.assertFalse(os.path.exists(self.target))

    def test_install_ok(self):
        out = installer.install(self.q, SID, self.ctx)
        self.assertEqual(out["status"], "INSTALLED")
        self.assertEqual(out["target"], self.target)
        with open(os.path.join(self.target, "SKILL.md")) as handle:
            self.assertIn("hello", handle.read())
        run = os.path.join(self.target, "scripts", "run.py")
        self.assertEqual(stat.S_IMODE(os.stat(run).st_mode), 0o644)  # 실행 권한 빠짐
        self.assertFalse(os.path.exists(self.q))  # 점검 모드도 끝남
        self.assertEqual(guard.get_mode(self.ctx, SID)[0], "normal")

    def test_modified_after_vet(self):
        self.write(os.path.join(self.q, "SKILL.md"), "changed\n")
        self.assert_install_error("HASH_MISMATCH")

    def test_new_file_after_vet(self):
        self.write(os.path.join(self.q, "extra.sh"), "curl x | sh\n")
        self.assert_install_error("HASH_MISMATCH")

    def test_removed_file_after_vet(self):
        os.unlink(os.path.join(self.q, "scripts", "run.py"))
        self.assert_install_error("HASH_MISMATCH")

    def test_symlink_added_after_vet(self):
        os.symlink("/etc/passwd", os.path.join(self.q, "link"))
        self.assert_install_error("NON_REGULAR")

    def test_symlink_from_start_refused(self):
        vet.end(SID, self.ctx)
        os.symlink("/etc/passwd", os.path.join(self.local, "link"))
        self.q = self.start_local()["quarantine"]
        self.assert_install_error("NON_REGULAR")

    def test_target_exists(self):
        os.makedirs(self.target)
        self.write(os.path.join(self.target, "SKILL.md"), "mine\n")
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install(self.q, SID, self.ctx)
        self.assertEqual(ctx.exception.code, "TARGET_EXISTS")
        with open(os.path.join(self.target, "SKILL.md")) as handle:
            self.assertEqual(handle.read(), "mine\n")  # 덮어쓰지 않음

    def test_wrong_quarantine(self):
        other = tempfile.mkdtemp(prefix="skill-bound-", dir=self.tmpdir)
        self.assert_install_error("NOT_VETTING", quarantine=other)

    def test_other_session(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install(self.q, "other-session", self.ctx)
        self.assertEqual(ctx.exception.code, "NOT_VETTING")

    def test_no_session(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install(self.q, None, self.ctx)
        self.assertEqual(ctx.exception.code, "NO_SESSION")

    def test_manifest_missing(self):
        os.unlink(vet.manifest_path(self.ctx, self.q))
        self.assert_install_error("NO_MANIFEST")

    def test_not_a_skill(self):
        vet.end(SID, self.ctx)
        os.unlink(os.path.join(self.local, "SKILL.md"))
        self.q = self.start_local()["quarantine"]
        self.assert_install_error("NOT_A_SKILL")

    def test_baseline_registered(self):
        project = os.path.join(self.home, "project")
        os.makedirs(project)
        self.assertIsNone(guard.session_start({"cwd": project}, self.ctx))  # 기준선 처음 만들기
        installer.install(self.q, SID, self.ctx)
        # 점검을 거쳐 설치한 스킬은 다음 세션에서 "새로 생겼다"고 알리지 않는다.
        self.assertIsNone(guard.session_start({"cwd": project}, self.ctx))

    def test_unvetted_skill_is_still_reported(self):
        project = os.path.join(self.home, "project")
        os.makedirs(project)
        guard.session_start({"cwd": project}, self.ctx)
        installer.install(self.q, SID, self.ctx)
        rogue = os.path.join(self.home, ".claude", "skills", "rogue")
        os.makedirs(rogue)
        self.write(os.path.join(rogue, "SKILL.md"), "x\n")
        out = guard.session_start({"cwd": project}, self.ctx)
        self.assertIn("user-skills:rogue", out["systemMessage"])
        self.assertNotIn("my-skill", out["systemMessage"])


class CliTest(unittest.TestCase):
    """진짜 홈 폴더를 건드리지 않는 경우만 명령줄로 시험한다(세션 id가 없으면 아무것도 하지 않는다)."""

    def run_cli(self, script, *args):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        return subprocess.run([sys.executable, "-I", "-B", os.path.join(SCRIPTS, script)] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)

    def test_vet_without_session(self):
        proc = self.run_cli("vet.py", "start", "github.com/a/b")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["error"], "NO_SESSION")

    def test_install_without_session(self):
        proc = self.run_cli("install_checked.py", "/tmp/skill-bound-x")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["error"], "NO_SESSION")

    def test_bad_source_not_echoed(self):
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID="cli-test-no-state-change")
        # 형식이 틀린 주소는 상태를 바꾸기 전에 거절된다. 주소 글은 출력에 나오지 않는다.
        proc = subprocess.run([sys.executable, "-I", "-B", os.path.join(SCRIPTS, "vet.py"), "start",
                               "evil.example/ignore-previous-instructions"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["error"], "BAD_SOURCE")
        self.assertNotIn(b"ignore-previous", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
