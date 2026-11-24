#!/usr/bin/env python3
"""skill-bound 해시 대조 설치.

    install_checked.py <격리 폴더>

점검한 바로 그 파일들인지 해시로 확인한 뒤에만 `~/.claude/skills/<이름>` 에 복사한다.
guard.py가 이 명령을 실행하기 전에 사람에게 허락을 묻는다.

확인하는 것 (하나라도 어긋나면 아무것도 설치하지 않는다):
1. 지금 이 세션에서 점검 중인 격리 폴더인지
2. vet.py가 받을 때 기록한 파일 목록, SHA-256과 지금이 완전히 같은지 (바뀐 파일, 새 파일, 없어진 파일 모두 중단)
3. 심볼릭 링크나 특수 파일이 없는지
4. 맨 위에 SKILL.md가 있는지 (이번 범위는 스킬만 설치한다)
5. 설치할 위치에 이미 무언가 있지 않은지 (덮어쓰지 않는다)

설치한 뒤에는 실행 권한을 원래대로 두고, 복사본의 해시를 다시 확인하고, 승인 목록과 세션 시작 기준선에
등록하고, 점검 모드를 끈다.

파이썬 3.9 문법까지만 쓰고, 표준 라이브러리만 쓴다.
"""

import importlib.util
import json
import os
import shutil
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore
    return module


vet = _load(os.path.join(HERE, "vet.py"), "skill_bound_vet")
guard = vet.guard

MESSAGES = {
    "INSTALLED": "점검한 파일과 해시가 모두 같아 설치했습니다. 이 스킬 폴더 안의 준비 작업(패키지 설치 등)은 skill-bound가 막지 않습니다. 점검 모드는 껐습니다.",
    "NO_SESSION": "이 세션의 id를 알 수 없어 설치하지 않았습니다.",
    "NOT_VETTING": "이 세션에서 점검 중인 격리 폴더가 아니어서 설치하지 않았습니다.",
    "NO_MANIFEST": "받을 때 남긴 해시 기록이 없거나 읽을 수 없어 설치하지 않았습니다.",
    "BAD_NAME": "스킬 이름이 올바르지 않아 설치하지 않았습니다.",
    "HASH_MISMATCH": "점검한 뒤에 파일이 바뀌었거나, 생기거나, 없어졌습니다. 설치하지 않았습니다. 처음부터 다시 점검하세요.",
    "NON_REGULAR": "심볼릭 링크나 특수 파일이 있어 설치하지 않았습니다.",
    "NOT_A_SKILL": "맨 위에 SKILL.md가 없어 스킬로 설치할 수 없습니다(플러그인, MCP 설치는 아직 지원하지 않습니다).",
    "TARGET_EXISTS": "같은 이름의 스킬이 이미 설치되어 있어 덮어쓰지 않았습니다.",
    "COPY_FAILED": "복사하는 중에 문제가 생겨 설치를 되돌렸습니다.",
}


class InstallError(Exception):
    def __init__(self, code):
        # type: (str) -> None
        Exception.__init__(self, code)
        self.code = code


def _copy_exact(src_root, dest_root, files):
    # type: (str, str, Dict[str, str]) -> None
    """목록에 있는 파일만, 새로 만드는 방식(O_EXCL)으로 복사한다.

    실행 권한은 받은 파일 그대로 둔다(실행 파일은 0755, 나머지는 0644). 처음엔 모두 뺐는데,
    `./run.sh` 처럼 바로 실행하는 스킬이 "권한 없음"으로 멈춰 사용을 방해했다.
    """
    os.mkdir(dest_root, 0o755)
    for rel in sorted(files):
        parts = rel.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise InstallError("COPY_FAILED")
        target = os.path.join(dest_root, *parts)
        parent = os.path.dirname(target)
        if not os.path.isdir(parent):
            os.makedirs(parent, 0o755)
        src_path = os.path.join(src_root, *parts)
        mode = 0o755 if os.lstat(src_path).st_mode & 0o111 else 0o644
        src_fd = os.open(src_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        dst_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(src_fd, "rb") as src, os.fdopen(dst_fd, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.chmod(target, mode)


def _register_baselines(ctx, name, target):
    # type: (object, str, str) -> None
    """설치한 파일을 모든 프로젝트의 기준선에 넣는다. 다음 세션에서 "새로 생겼다"고 알리지 않게."""
    folder = os.path.join(ctx.state_dir, "baselines")  # type: ignore
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return
    entries = {}  # type: Dict[str, str]
    for dirpath, _dirs, filenames in os.walk(target):
        for file_name in filenames:
            full = os.path.join(dirpath, file_name)
            rel = os.path.relpath(full, os.path.dirname(target)).replace(os.sep, "/")
            entries["user-skills:" + rel] = guard._hash_file(full, os.stat(full))
    for file_name in names:
        if not file_name.endswith(".json"):
            continue
        path = os.path.join(folder, file_name)
        current = guard.load_baseline(path)
        if current is None:
            continue
        current.update(entries)
        guard.save_baseline(path, current)


def install(quarantine_arg, session_id, ctx):
    # type: (str, Optional[str], object) -> Dict[str, object]
    if session_id is None:
        raise InstallError("NO_SESSION")
    mode, quarantine = guard.get_mode(ctx, session_id)
    if mode != "vet" or quarantine is None or os.path.realpath(quarantine_arg) != quarantine:
        raise InstallError("NOT_VETTING")

    manifest = vet.load_manifest(ctx, quarantine)
    if manifest is None or not isinstance(manifest.get("files"), dict):
        raise InstallError("NO_MANIFEST")
    name = manifest.get("name")
    if not isinstance(name, str) or not vet.NAME_RE.match(name):
        raise InstallError("BAD_NAME")

    files, others = vet.hash_tree(quarantine)
    if others or manifest.get("others"):
        raise InstallError("NON_REGULAR")
    if files != manifest["files"]:
        raise InstallError("HASH_MISMATCH")
    if "SKILL.md" not in files:
        raise InstallError("NOT_A_SKILL")

    skills_dir = os.path.join(ctx.home, ".claude", "skills")  # type: ignore
    target = os.path.join(skills_dir, name)
    if os.path.lexists(target):
        raise InstallError("TARGET_EXISTS")
    os.makedirs(skills_dir, exist_ok=True)
    try:
        _copy_exact(quarantine, target, files)
        copied, copied_others = vet.hash_tree(target)
        if copied != files or copied_others:
            raise InstallError("COPY_FAILED")
    except FileExistsError:
        # 확인과 복사 사이에 누군가 같은 이름을 만들었다. 남의 것을 지우지 않는다.
        raise InstallError("TARGET_EXISTS")
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise InstallError("COPY_FAILED")

    _register_baselines(ctx, name, target)
    guard.add_approved(ctx.state_dir, name, target)  # type: ignore
    vet.end(session_id, ctx)
    return {"status": "INSTALLED", "message": MESSAGES["INSTALLED"], "target": target, "files": len(files)}


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(json.dumps({"status": "ERROR", "error": "USAGE", "message": "사용법: install_checked.py <격리 폴더>"},
                         ensure_ascii=False))
        return 2
    try:
        out = install(args[0], vet.current_session_id(), guard.Ctx())
    except InstallError as exc:
        print(json.dumps({"status": "ERROR", "error": exc.code, "message": MESSAGES.get(exc.code, exc.code)},
                         ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
