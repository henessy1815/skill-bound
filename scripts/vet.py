#!/usr/bin/env python3
"""skill-bound 격리 폴더 관리.

    vet.py start <주소>   격리 폴더를 만들고 스킬을 받는다. 파일별 해시를 기록하고 점검 모드를 켠다.
    vet.py end            격리 폴더와 기록을 지우고 점검 모드를 끈다(취소도 이것으로).
    vet.py status         지금 점검 중인지 알려 준다.

받을 수 있는 주소:
    github.com/<owner>/<repo>
    github.com/<owner>/<repo>/tree/<브랜치나 태그>/<폴더 경로>
    (앞의 https:// 와 뒤의 .git 은 있어도 된다)
    /절대/경로/폴더   (이미 받아 둔 로컬 폴더를 복사)

중요한 약속:
- 아무것도 실행하지 않는다. git clone은 훅, LFS 필터, 심볼릭 링크를 끄고 받는다.
- git이나 원격 서버가 보낸 글은 출력하지 않는다. 출력은 고정된 값과 문장뿐이다.
- 이 세션의 id는 Claude Code가 Bash에 넣어 주는 CLAUDE_CODE_SESSION_ID 로 안다.

파이썬 3.9 문법까지만 쓰고, 표준 라이브러리만 쓴다.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore
    return module


guard = _load(os.path.join(os.path.dirname(HERE), "hooks", "guard.py"), "skill_bound_guard")

GIT_TIMEOUT = 180
MAX_FETCH_FILES = 5000
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
PATH_PART_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")

MESSAGES = {
    "STARTED": "격리 폴더에 받았습니다. 점검 모드가 켜졌습니다. 아무것도 실행하지 않았습니다.",
    "ENDED": "점검을 끝냈습니다. 격리 폴더와 기록을 지우고 점검 모드를 껐습니다.",
    "NOTHING_TO_END": "진행 중인 점검이 없습니다.",
    "STATUS_IDLE": "지금 점검 중이 아닙니다.",
    "STATUS_VET": "점검 중입니다.",
    "STATUS_BROKEN": "점검 상태 파일이 깨져 있습니다. `vet.py end` 로 정리하세요.",
    "NO_SESSION": "이 세션의 id를 알 수 없어 점검을 시작하지 않았습니다(CLAUDE_CODE_SESSION_ID 없음).",
    "ALREADY_VETTING": "이 세션은 이미 점검 중입니다. 먼저 `vet.py end` 로 끝내세요.",
    "SKILL_BOUND_OFF": "skill-bound가 꺼져 있어(OFF 파일) 점검 모드를 켤 수 없습니다.",
    "BAD_SOURCE": "받을 수 있는 주소 형식이 아닙니다. github.com/<owner>/<repo> 또는 로컬 폴더의 절대 경로를 쓰세요.",
    "BAD_NAME": "스킬 이름으로 쓸 수 있는 글자가 아닙니다(영문, 숫자, . _ - 만).",
    "GIT_NOT_FOUND": "git을 찾지 못했습니다.",
    "FETCH_FAILED": "받기에 실패했습니다(주소, 브랜치, 네트워크를 확인하세요).",
    "FETCH_TIMEOUT": "받는 데 너무 오래 걸려 멈췄습니다.",
    "SUBPATH_NOT_FOUND": "저장소 안에 그 폴더가 없습니다.",
    "LOCAL_NOT_FOUND": "그 로컬 폴더가 없거나 폴더가 아닙니다.",
    "SPECIAL_FILE": "일반 파일이 아닌 특수 파일(파이프, 장치 등)이 있어 받지 않았습니다.",
    "TOO_MANY_FILES": "파일이 너무 많아 받지 않았습니다.",
    "EMPTY": "받은 폴더에 파일이 없습니다.",
}


class VetError(Exception):
    def __init__(self, code):
        # type: (str) -> None
        Exception.__init__(self, code)
        self.code = code


# ---------------------------------------------------------------------------
# 주소 읽기
# ---------------------------------------------------------------------------

def parse_source(source):
    # type: (str) -> Dict[str, object]
    """주소를 읽어 {"kind": "github", owner, repo, ref, path} 또는 {"kind": "local", path}."""
    if not isinstance(source, str) or not source or len(source) > 500:
        raise VetError("BAD_SOURCE")
    if source.startswith("/"):
        return {"kind": "local", "path": source}
    text = source
    if text.startswith("https://"):
        text = text[len("https://"):]
    if not text.startswith("github.com/"):
        raise VetError("BAD_SOURCE")
    parts = [p for p in text[len("github.com/"):].split("/")]
    if parts and parts[-1] == "":
        parts = parts[:-1]  # 끝의 / 하나는 봐준다
    if len(parts) < 2:
        raise VetError("BAD_SOURCE")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not OWNER_REPO_RE.match(owner) or not OWNER_REPO_RE.match(repo) or repo in (".", ".."):
        raise VetError("BAD_SOURCE")
    ref = None  # type: Optional[str]
    sub = []  # type: List[str]
    if len(parts) > 2:
        if len(parts) < 4 or parts[2] != "tree":
            raise VetError("BAD_SOURCE")
        ref = parts[3]
        sub = parts[4:]
        if not REF_RE.match(ref) or ".." in ref:
            raise VetError("BAD_SOURCE")
        for part in sub:
            if not PATH_PART_RE.match(part):
                raise VetError("BAD_SOURCE")
    return {"kind": "github", "owner": owner, "repo": repo, "ref": ref, "path": "/".join(sub)}


def skill_name_for(source):
    # type: (Dict[str, object]) -> str
    if source["kind"] == "github":
        name = str(source["path"]).split("/")[-1] if source["path"] else str(source["repo"])
    else:
        name = os.path.basename(os.path.normpath(str(source["path"])))
    if not NAME_RE.match(name):
        raise VetError("BAD_NAME")
    return name


# ---------------------------------------------------------------------------
# 받기
# ---------------------------------------------------------------------------

def git_binary():
    # type: () -> Optional[str]
    if os.path.isfile("/usr/bin/git"):
        return "/usr/bin/git"
    return shutil.which("git")


# 받는 동안 아무것도 실행되지 않게 하는 git 설정
SAFE_GIT_CONFIG = [
    "credential.helper=",            # 저장된 로그인 정보(맥 키체인 등)를 쓰지 않음. 남의 공개 저장소만 받는다
    "core.hooksPath=/dev/null",      # 훅 실행 안 함
    "core.symlinks=false",           # 심볼릭 링크를 링크가 아닌 글 파일로
    "core.fsmonitor=false",
    "filter.lfs.smudge=",            # LFS 필터 끔
    "filter.lfs.process=",
    "filter.lfs.required=false",
    "submodule.recurse=false",
    "protocol.allow=never",          # https만 허용
    "protocol.https.allow=always",
]


def safe_git_env(base=None):
    # type: (Optional[Dict[str, str]]) -> Dict[str, str]
    """git을 실행할 환경 변수. 시스템과 사용자의 git 설정 파일을 아예 읽지 않게 한다.

    통합 시험에서 없는 저장소를 받으려 하자, 시스템 git 설정의 키체인 도우미 때문에
    맥 키체인 암호를 묻는 창이 떴다. 남의 스킬을 받을 때 내 로그인 정보나 git 설정
    (키체인, 필터, 주소 바꾸기 등)이 끼어들 이유가 없으므로 전부 끊는다.
    """
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key.startswith("GIT_"):
            del env[key]  # 밖에서 들어온 GIT_ 설정(GIT_DIR, GIT_CONFIG_* 등)도 버린다
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",      # /etc/gitconfig, Homebrew gitconfig 등 무시
        "GIT_CONFIG_GLOBAL": "/dev/null",  # ~/.gitconfig 무시
        "GIT_TERMINAL_PROMPT": "0",      # 아이디, 암호를 묻지 않음
        "GIT_ASKPASS": "/usr/bin/false",
        "SSH_ASKPASS": "/usr/bin/false",
        "GIT_LFS_SKIP_SMUDGE": "1",
    })
    return env


def git_clone(url, dest, ref=None, extra_config=None, timeout=GIT_TIMEOUT):
    # type: (str, str, Optional[str], Optional[List[str]], int) -> None
    git = git_binary()
    if git is None:
        raise VetError("GIT_NOT_FOUND")
    cmd = [git]
    for item in SAFE_GIT_CONFIG + list(extra_config or []):
        cmd += ["-c", item]
    cmd += ["clone", "--depth", "1", "--single-branch", "--no-tags", "--quiet"]
    if ref:
        cmd += ["--branch", ref]
    cmd += ["--", url, dest]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                              env=safe_git_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise VetError("FETCH_TIMEOUT")
    except OSError:
        raise VetError("FETCH_FAILED")
    if proc.returncode != 0:
        raise VetError("FETCH_FAILED")


def copy_local(src, dest):
    # type: (str, str) -> None
    """로컬 폴더를 복사한다. 심볼릭 링크는 따라가지 않고 링크 그대로 복사한다(스캔에서 잡히게)."""
    try:
        st = os.lstat(src)
    except OSError:
        raise VetError("LOCAL_NOT_FOUND")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise VetError("LOCAL_NOT_FOUND")
    count = 0
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel_dir = os.path.relpath(dirpath, src)
        target_dir = dest if rel_dir == "." else os.path.join(dest, rel_dir)
        os.makedirs(target_dir, mode=0o700, exist_ok=True)
        for name in list(dirnames) + filenames:
            full = os.path.join(dirpath, name)
            mode = os.lstat(full).st_mode
            target = os.path.join(target_dir, name)
            if stat.S_ISLNK(mode):
                os.symlink(os.readlink(full), target)
                if name in dirnames:
                    dirnames.remove(name)  # 링크된 폴더 안으로는 들어가지 않는다
                count += 1
            elif stat.S_ISDIR(mode):
                continue
            elif stat.S_ISREG(mode):
                shutil.copyfile(full, target, follow_symlinks=False)
                os.chmod(target, 0o600)
                count += 1
            else:
                raise VetError("SPECIAL_FILE")
            if count > MAX_FETCH_FILES:
                raise VetError("TOO_MANY_FILES")


def move_contents(src_dir, dest_dir):
    # type: (str, str) -> None
    for name in os.listdir(src_dir):
        os.rename(os.path.join(src_dir, name), os.path.join(dest_dir, name))


def fetch(source, quarantine, extra_git_config=None, url_override=None):
    # type: (Dict[str, object], str, Optional[List[str]], Optional[str]) -> None
    """source를 quarantine 바로 아래에 펼친다. .git 폴더는 남기지 않는다."""
    if source["kind"] == "local":
        copy_local(str(source["path"]), quarantine)
        return
    work = os.path.join(quarantine, ".skill-bound-fetch")
    url = url_override or "https://github.com/%s/%s.git" % (source["owner"], source["repo"])
    git_clone(url, work, ref=source["ref"], extra_config=extra_git_config)  # type: ignore
    shutil.rmtree(os.path.join(work, ".git"), ignore_errors=True)
    picked = work
    if source["path"]:
        picked = os.path.join(work, *str(source["path"]).split("/"))
        st = os.lstat(picked) if os.path.lexists(picked) else None
        if st is None or not stat.S_ISDIR(st.st_mode):
            raise VetError("SUBPATH_NOT_FOUND")
    move_contents(picked, quarantine)
    shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 해시 기록 (install_checked.py도 쓴다)
# ---------------------------------------------------------------------------

def sha256_file(path):
    # type: (str) -> str
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(root):
    # type: (str) -> Tuple[Dict[str, str], List[str]]
    """({상대 경로: sha256}, [일반 파일이 아닌 것들의 상대 경로]). 링크는 따라가지 않는다."""
    files = {}  # type: Dict[str, str]
    others = []  # type: List[str]
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(list(dirnames)):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                others.append(os.path.relpath(full, root).replace(os.sep, "/"))
                dirnames.remove(name)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            mode = os.lstat(full).st_mode
            if stat.S_ISREG(mode):
                files[rel] = sha256_file(full)
            else:
                others.append(rel)
    return files, sorted(others)


def manifest_path(ctx, quarantine):
    # type: (object, str) -> str
    return os.path.join(ctx.state_dir, "manifests", os.path.basename(os.path.realpath(quarantine)) + ".json")  # type: ignore


def load_manifest(ctx, quarantine):
    # type: (object, str) -> Optional[Dict[str, object]]
    try:
        with open(manifest_path(ctx, quarantine), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------

def current_session_id(environ=None):
    # type: (Optional[Dict[str, str]]) -> Optional[str]
    value = (environ if environ is not None else os.environ).get("CLAUDE_CODE_SESSION_ID")
    return value if guard.valid_session_id(value) else None


def start(source_text, session_id, ctx, extra_git_config=None, url_override=None):
    # type: (str, Optional[str], object, Optional[List[str]], Optional[str]) -> Dict[str, object]
    if session_id is None:
        raise VetError("NO_SESSION")
    mode, _ = guard.get_mode(ctx, session_id)
    if mode == "off":
        raise VetError("SKILL_BOUND_OFF")
    if mode in ("vet", "broken"):
        raise VetError("ALREADY_VETTING")
    source = parse_source(source_text)
    name = skill_name_for(source)

    quarantine = os.path.realpath(tempfile.mkdtemp(prefix="skill-bound-", dir=ctx.tmp_dir))  # type: ignore
    try:
        fetch(source, quarantine, extra_git_config=extra_git_config, url_override=url_override)
        files, others = hash_tree(quarantine)
        if not files and not others:
            raise VetError("EMPTY")
        guard._atomic_write_json(manifest_path(ctx, quarantine), {
            "format_version": 1,
            "name": name,
            "source": source,
            "files": files,
            "others": others,
            "created": ctx.now,  # type: ignore
        })
        # 기록을 먼저 남기고 마지막에 점검 모드를 켠다.
        guard.write_session_state(ctx.state_dir, session_id, quarantine, now=ctx.now)  # type: ignore
    except BaseException:
        shutil.rmtree(quarantine, ignore_errors=True)
        try:
            os.unlink(manifest_path(ctx, quarantine))
        except OSError:
            pass
        raise
    return {
        "status": "STARTED",
        "message": MESSAGES["STARTED"],
        "quarantine": quarantine,
        "name": name,
        "files": len(files),
        "non_regular": len(others),
    }


def _session_quarantine(ctx, session_id):
    # type: (object, str) -> Optional[str]
    """상태 파일이 깨져 있어도 격리 폴더 경로만은 최대한 읽어 낸다(정리용)."""
    try:
        with open(guard.session_state_path(ctx.state_dir, session_id), encoding="utf-8") as handle:  # type: ignore
            value = json.load(handle).get("quarantine")
    except Exception:
        return None
    return value if isinstance(value, str) else None


def end(session_id, ctx):
    # type: (Optional[str], object) -> Dict[str, object]
    if session_id is None:
        raise VetError("NO_SESSION")
    path = guard.session_state_path(ctx.state_dir, session_id)  # type: ignore
    if not os.path.lexists(path):
        return {"status": "NOTHING_TO_END", "message": MESSAGES["NOTHING_TO_END"]}
    quarantine = _session_quarantine(ctx, session_id)
    if quarantine and guard.is_quarantine(quarantine, ctx.tmp_dir):  # type: ignore
        shutil.rmtree(os.path.realpath(quarantine), ignore_errors=True)
        try:
            os.unlink(manifest_path(ctx, quarantine))
        except OSError:
            pass
    guard.clear_session_state(ctx.state_dir, session_id)  # type: ignore
    return {"status": "ENDED", "message": MESSAGES["ENDED"]}


def status(session_id, ctx):
    # type: (Optional[str], object) -> Dict[str, object]
    if session_id is None:
        raise VetError("NO_SESSION")
    mode, quarantine = guard.get_mode(ctx, session_id)
    if mode == "vet":
        with open(guard.session_state_path(ctx.state_dir, session_id), encoding="utf-8") as handle:  # type: ignore
            started = json.load(handle)["started"]
        left = max(0, int((guard.VET_MAX_SECONDS - (ctx.now - started)) // 60))  # type: ignore
        return {"status": "VETTING", "message": MESSAGES["STATUS_VET"], "quarantine": quarantine, "minutes_left": left}
    if mode == "broken":
        return {"status": "BROKEN", "message": MESSAGES["STATUS_BROKEN"]}
    return {"status": "IDLE", "message": MESSAGES["STATUS_IDLE"]}


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    parser = argparse.ArgumentParser(description="skill-bound quarantine manager (runs nothing it fetches)")
    sub = parser.add_subparsers(dest="command")
    p_start = sub.add_parser("start")
    p_start.add_argument("source")
    sub.add_parser("end")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    ctx = guard.Ctx()
    session_id = current_session_id()
    try:
        if args.command == "start":
            out = start(args.source, session_id, ctx)
        elif args.command == "end":
            out = end(session_id, ctx)
        elif args.command == "status":
            out = status(session_id, ctx)
        else:
            parser.print_usage()
            return 2
    except VetError as exc:
        print(json.dumps({"status": "ERROR", "error": exc.code, "message": MESSAGES.get(exc.code, exc.code)},
                         ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
