#!/usr/bin/env python3
"""skill-bound 끄기, 켜기, 완전히 지우기.

    switch.py off        skill-bound를 끈다. (guard.py가 실행 전에 사람에게 허락을 묻는다)
    switch.py on         다시 켠다. (안전해지는 쪽이라 묻지 않는다)
    switch.py uninstall  완전히 지운다. (guard.py가 실행 전에 사람에게 허락을 묻는다)

왜 허락을 묻나:
Claude는 평소 웹 페이지나 남의 저장소 글을 읽는다. 그 안에 "skill-bound를 꺼라" 같은
문장이 숨어 있을 수 있다. Claude가 묻지 않고 끌 수 있으면 그 한 줄로 모든 보호가 사라진다.
그래서 끄기와 지우기는 사람이 허락 창에서 한 번 눌러야 된다. 사람이 요청하지 않았는데
허락 창이 뜨면, 그것 자체가 무언가 이상하다는 신호다.

uninstall이 하는 일:
1. ~/.claude/settings.json 에서 skill-bound 훅 항목만 지운다(다른 훅은 그대로). 고치기 전에 백업을 남긴다.
2. 진행 중인 점검의 격리 폴더와 상태 폴더(~/.claude/skill-bound-state)를 지운다.
3. skill-bound가 ~/.claude/skills/skill-bound 에 설치되어 있으면 그 폴더를 지운다.
   다른 곳(개발 중인 폴더 등)에서 실행했으면 폴더는 지우지 않는다.

파이썬 3.9 문법까지만 쓰고, 표준 라이브러리만 쓴다.
"""

import importlib.util
import json
import os
import shutil
import sys
import time
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True

_spec = importlib.util.spec_from_file_location("skill_bound_guard", os.path.join(os.path.dirname(HERE), "hooks", "guard.py"))
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)  # type: ignore

GUARD_MARK = "skill-bound/hooks/guard.py"

MESSAGES = {
    "OFF": "skill-bound를 껐습니다. 다시 켜려면 `switch.py on` 을 쓰거나, 터미널에서 OFF 파일을 지우세요.",
    "ALREADY_OFF": "이미 꺼져 있습니다.",
    "ON": "skill-bound를 다시 켰습니다.",
    "ALREADY_ON": "이미 켜져 있습니다.",
    "UNINSTALLED": "skill-bound를 지웠습니다. Claude Code를 다시 시작하면 훅이 완전히 빠집니다.",
    "SETTINGS_INVALID": "settings.json을 읽을 수 없어(JSON 형식 오류) 아무것도 지우지 않았습니다. 사람이 직접 고쳐야 합니다.",
}


class SwitchError(Exception):
    def __init__(self, code):
        # type: (str) -> None
        Exception.__init__(self, code)
        self.code = code


def off_path(ctx):
    # type: (object) -> str
    return os.path.join(ctx.state_dir, "OFF")  # type: ignore


def turn_off(ctx):
    # type: (object) -> Dict[str, object]
    path = off_path(ctx)
    if os.path.lexists(path):
        return {"status": "ALREADY_OFF", "message": MESSAGES["ALREADY_OFF"]}
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("turned off by switch.py at %d\n" % int(time.time()))
    return {"status": "OFF", "message": MESSAGES["OFF"]}


def turn_on(ctx):
    # type: (object) -> Dict[str, object]
    path = off_path(ctx)
    if not os.path.lexists(path):
        return {"status": "ALREADY_ON", "message": MESSAGES["ALREADY_ON"]}
    os.unlink(path)
    return {"status": "ON", "message": MESSAGES["ON"]}


def remove_our_hooks(settings):
    # type: (Dict[str, object]) -> int
    """settings 사전에서 skill-bound 훅 항목만 지운다. 지운 개수를 돌려준다. 다른 훅은 건드리지 않는다."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks.keys()):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []  # type: List[object]
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = []
            for handler in group["hooks"]:
                command = handler.get("command") if isinstance(handler, dict) else None
                if isinstance(command, str) and GUARD_MARK in command:
                    removed += 1
                else:
                    kept.append(handler)
            if kept:
                new_group = dict(group)
                new_group["hooks"] = kept
                kept_groups.append(new_group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return removed


def uninstall(ctx):
    # type: (object) -> Dict[str, object]
    home = ctx.home  # type: ignore
    settings_path = os.path.join(home, ".claude", "settings.json")
    hooks_removed = 0
    backup = None  # type: Optional[str]
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as handle:
                settings = json.load(handle)
        except (OSError, ValueError):
            raise SwitchError("SETTINGS_INVALID")
        if not isinstance(settings, dict):
            raise SwitchError("SETTINGS_INVALID")
        hooks_removed = remove_our_hooks(settings)
        if hooks_removed:
            backup = settings_path + ".before-skill-bound-uninstall"
            shutil.copy2(settings_path, backup)
            guard._atomic_write_json(settings_path, settings)

    # 진행 중인 점검의 격리 폴더 정리 (임시 폴더 바로 아래의 skill-bound- 폴더만)
    sessions = os.path.join(ctx.state_dir, "sessions")  # type: ignore
    if os.path.isdir(sessions):
        for name in os.listdir(sessions):
            try:
                with open(os.path.join(sessions, name), encoding="utf-8") as handle:
                    quarantine = json.load(handle).get("quarantine")
            except Exception:
                continue
            if isinstance(quarantine, str) and guard.is_quarantine(quarantine, ctx.tmp_dir):  # type: ignore
                shutil.rmtree(os.path.realpath(quarantine), ignore_errors=True)
    shutil.rmtree(ctx.state_dir, ignore_errors=True)  # type: ignore

    installed = os.path.realpath(os.path.join(home, ".claude", "skills", "skill-bound"))
    folder_removed = False
    if ctx.sb_dir == installed and os.path.isdir(installed):  # type: ignore
        shutil.rmtree(installed)
        folder_removed = True
    return {
        "status": "UNINSTALLED",
        "message": MESSAGES["UNINSTALLED"],
        "hooks_removed": hooks_removed,
        "settings_backup": backup,
        "folder_removed": folder_removed,
    }


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    args = sys.argv[1:] if argv is None else argv
    ctx = guard.Ctx()
    actions = {"off": turn_off, "on": turn_on, "uninstall": uninstall}
    if len(args) != 1 or args[0] not in actions:
        print(json.dumps({"status": "ERROR", "error": "USAGE", "message": "사용법: switch.py off | on | uninstall"},
                         ensure_ascii=False))
        return 2
    try:
        out = actions[args[0]](ctx)
    except SwitchError as exc:
        print(json.dumps({"status": "ERROR", "error": exc.code, "message": MESSAGES[exc.code]}, ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
