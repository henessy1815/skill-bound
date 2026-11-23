#!/usr/bin/env python3
"""읽기실 전용 PreToolUse 훅 (이중 잠금).

reader.py가 `claude -p --settings` 로 이 훅을 붙인다. `--restricted` 가 이미 파일 도구를
작업 폴더 안으로 가두지만, 한 번 더 막는다.

- 허용: Read, Grep, Glob (격리 폴더 안쪽만), StructuredOutput (최종 답 제출)
- 그 밖의 도구는 전부 차단
- 판단 중에 예외가 나면 차단 (fail-closed)

사용법 (훅 명령): python3 -I -B reader_guard.py <격리 폴더>
허용할 때는 아무것도 출력하지 않고 종료 코드 0으로 끝난다.
"""

import json
import os
import sys
from typing import Optional, Tuple

MAX_INPUT_BYTES = 1024 * 1024
FILE_TOOLS = ("Read", "Grep", "Glob")


def _inside(path, root_real):
    # type: (str, str) -> bool
    if not path or "\x00" in path:
        return False
    if path.startswith("~"):
        return False
    if not os.path.isabs(path):
        path = os.path.join(root_real, path)
    real = os.path.realpath(path)
    return real == root_real or real.startswith(root_real + os.sep)


def _safe_pattern(pattern):
    # type: (object) -> bool
    """Glob 패턴이나 Grep의 glob 값이 폴더 밖을 가리키지 않는지."""
    if not isinstance(pattern, str) or not pattern or "\x00" in pattern:
        return False
    if pattern.startswith("/") or pattern.startswith("~"):
        return False
    parts = pattern.replace("\\", "/").split("/")
    return ".." not in parts and "{" not in pattern


def decide(tool_name, tool_input, root_real):
    # type: (object, object, str) -> Tuple[bool, str]
    """(허용 여부, 이유 코드). 이유 코드는 고정된 낱말이다."""
    if tool_name == "StructuredOutput":
        return True, "OK"
    if tool_name not in FILE_TOOLS:
        return False, "TOOL_NOT_ALLOWED"
    if not isinstance(tool_input, dict):
        return False, "BAD_INPUT"
    if tool_name == "Read":
        path = tool_input.get("file_path")
        if not isinstance(path, str) or not _inside(path, root_real):
            return False, "OUTSIDE_FOLDER"
        return True, "OK"
    path = tool_input.get("path")  # type: Optional[object]
    if path is not None and (not isinstance(path, str) or not _inside(path, root_real)):
        return False, "OUTSIDE_FOLDER"
    if tool_name == "Glob":
        if not _safe_pattern(tool_input.get("pattern")):
            return False, "BAD_PATTERN"
    if tool_name == "Grep":
        glob_value = tool_input.get("glob")
        if glob_value is not None and not _safe_pattern(glob_value):
            return False, "BAD_PATTERN"
    return True, "OK"


def _deny(code):
    # type: (str) -> None
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "skill-bound reader: blocked (%s)" % code,
        }
    }))


def main(argv):
    # type: (list) -> int
    try:
        if len(argv) != 2:
            _deny("BAD_ARGS")
            return 0
        root_real = os.path.realpath(argv[1])
        if not os.path.isdir(root_real):
            _deny("BAD_ROOT")
            return 0
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            _deny("INPUT_TOO_LARGE")
            return 0
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            _deny("BAD_INPUT")
            return 0
        allowed, code = decide(data.get("tool_name"), data.get("tool_input"), root_real)
        if not allowed:
            _deny(code)
        return 0
    except Exception:  # noqa: 판단할 수 없으면 막는다
        _deny("GUARD_ERROR")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
