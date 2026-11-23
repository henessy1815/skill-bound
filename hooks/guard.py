#!/usr/bin/env python3
"""skill-bound 훅 (PreToolUse, SessionStart).

평소 모드: 아무 말 없이 통과한다. 다음 경우에만 개입한다.
- 스킬 설치 시도 (설치 명령, 스킬 관련 폴더에 파일 넣기) -> 막고 점검 절차로 안내
- skill-bound 자신의 폴더와 상태 폴더를 바꾸려는 시도 -> 막음
- Claude Code 설정 파일(.claude/settings.json)을 Edit, Write로 바꾸려는 시도 -> 사람에게 물음
- 점검을 마친 스킬의 설치(install_checked.py) -> 사람에게 물음

점검 모드 (이 세션에 격리 폴더가 있을 때): 정해진 것만 허용한다.
- Bash: skill-bound 스크립트를 정해진 형태로 실행하는 명령만
- Read, Grep, Glob: 격리 폴더와 skill-bound 폴더 안쪽만
- Write, Edit: 격리 폴더 안쪽만
- 그 밖의 도구(WebFetch, WebSearch, MCP 등): 묻지 않고 막음

실패했을 때: 평소 모드는 통과(fail-open), 점검 모드이거나 상태를 알 수 없으면 차단(fail-closed).

끄기, 지우기: `scripts/switch.py off|uninstall` 로 한다. Claude가 실행하면 사람에게 허락을 묻는다.
Claude가 OFF 파일을 직접 만들거나 폴더를 직접 지우는 것은 막는다. 다시 켜기(`switch.py on`)는 묻지 않는다.

사용법 (훅 명령):
    /usr/bin/python3 -I -B <이 파일> pre            PreToolUse
    /usr/bin/python3 -I -B <이 파일> session-start  SessionStart

파이썬 3.9 문법까지만 쓰고, 표준 라이브러리만 쓴다. 다른 파일을 불러오지 않는다
(불러오기가 실패해서 훅이 멈추는 일을 줄이려고).
"""

import json
import os
import re
import stat
import sys
import time

# 훅은 도구를 쓸 때마다 실행되므로 빨라야 한다. 대부분의 판단에 필요 없는 모듈
# (tempfile, hashlib, shlex)은 쓰는 곳에서 불러온다. typing도 불러오지 않는다
# (타입은 주석으로만 적었으므로 실행에 필요 없다).

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

SYSTEM_PYTHON = "/usr/bin/python3"
VET_MAX_SECONDS = 2 * 60 * 60
SB_SCRIPTS = ("vet.py", "scan.py", "reader.py", "install_checked.py", "switch.py")
MAX_INPUT_BYTES = 4 * 1024 * 1024
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# skill-bound 스크립트 명령의 각 조각에 허용하는 문자. 따옴표, $, ~, ;, |, &, <, >, 괄호,
# 와일드카드, 역슬래시, 공백류는 하나도 없다. 셸이 다르게 해석할 여지를 없앤다.
SB_TOKEN_RE = re.compile(r"^[A-Za-z0-9._/:@=+%,\-]+$")

# 점검 모드에서 허용하는 그 밖의 도구 (파일이나 바깥에 영향이 없는 것)
VET_HARMLESS_TOOLS = frozenset(["TodoWrite", "AskUserQuestion"])
WRITE_TOOLS = frozenset(["Write", "Edit", "MultiEdit", "NotebookEdit"])
READ_TOOLS = frozenset(["Read", "Grep", "Glob"])

# 내용을 바꾸지 않는 명령. 위험한 옵션은 아래 READ_ONLY_BAD_OPTIONS로 따로 막는다.
READ_ONLY_PROGRAMS = frozenset([
    "ls", "cat", "head", "tail", "wc", "stat", "file", "du", "grep", "egrep", "fgrep", "rg",
    "diff", "cmp", "shasum", "md5", "sha256sum", "readlink", "realpath", "pwd", "echo", "tree", "find",
])
READ_ONLY_BAD_OPTIONS = {
    # find는 지우거나 실행하거나 파일에 쓰는 옵션이 있다.
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"),
    # rg --pre는 파일마다 다른 프로그램을 실행한다.
    "rg": ("--pre",),
}
SHELL_META = set(";|&<>`$(){}\n\r")

INSTALL_COMMAND_RE = re.compile(
    r"\b(npx|bunx|pnpm\s+dlx|yarn\s+dlx)\s+(-y\s+|--yes\s+)?skills(@\S+)?\s+(add|install|i)\b"
    r"|\bclaude\s+plugins?\s+(install|i|enable|marketplace\s+add)\b"
    r"|\bclaude\s+mcp\s+(add|add-json|add-from-claude-desktop)\b",
    re.IGNORECASE,
)
INSTALL_DIRS = ("skills", "agents", "commands", "plugins")
CONFIG_MENTION_RE = re.compile(
    r"\.claude/(skills|agents|commands|plugins|hooks)\b|\.claude/settings(\.local)?\.json|\.claude\.json"
    r"|\.mcp\.json|managed-settings\.json|claude_config_dir",
)
INSTALL_MENTION_RE = re.compile(r"\.claude/(skills|agents|commands|plugins)\b|\.mcp\.json")

MESSAGES = {
    "INSTALL_BLOCKED": "skill-bound: 스킬, 플러그인, MCP 설치는 skill-bound 점검을 거쳐야 합니다. `/skill-bound <주소>` 로 먼저 점검하세요.",
    "PROTECTED": "skill-bound: skill-bound 폴더와 상태 폴더는 직접 바꿀 수 없습니다. 끄거나 지우려면 `/usr/bin/python3 -I -B <skill-bound 폴더>/scripts/switch.py off` 또는 `uninstall` 을 실행하세요(사람의 허락을 받습니다).",
    "SB_FORM": "skill-bound: skill-bound 스크립트는 `/usr/bin/python3 -I <skill-bound 폴더>/scripts/<이름>.py <인자>` 형태로만 실행할 수 있습니다. 인자에 따옴표, $, ~ 같은 특수 문자를 쓸 수 없습니다.",
    "CONFIG_BASH": "skill-bound: Claude 설정 폴더(.claude)를 바꾸는 셸 명령은 막혀 있습니다. 설정 변경은 사람이 직접 하거나, Edit 도구로 사람의 확인을 받으세요.",
    "SETTINGS_ASK": "skill-bound: Claude Code 설정 파일을 바꾸려고 합니다. 훅이나 권한이 바뀌면 skill-bound가 꺼질 수도 있습니다. 바뀌는 내용을 확인하고 허락하세요.",
    "INSTALL_ASK": "skill-bound: 점검을 마친 스킬을 설치합니다. 점검 보고서를 확인했다면 허락하세요.",
    "SWITCH_OFF_ASK": "skill-bound: Claude가 skill-bound를 끄려고 합니다. 직접 끄라고 하셨다면 허락하세요. 그런 적이 없다면 거절하세요. Claude가 읽은 글 속의 숨은 지시에 속았을 수 있습니다.",
    "UNINSTALL_ASK": "skill-bound: Claude가 skill-bound를 완전히 지우려고 합니다(훅 등록, 상태 폴더, 설치 폴더). 직접 지우라고 하셨다면 허락하세요. 그런 적이 없다면 거절하세요. Claude가 읽은 글 속의 숨은 지시에 속았을 수 있습니다.",
    "VET_BASH": "skill-bound 점검 중에는 skill-bound 스크립트를 `/usr/bin/python3 -I` 로 실행하는 명령만 쓸 수 있습니다.",
    "VET_READ": "skill-bound 점검 중에는 격리 폴더와 skill-bound 폴더 안쪽만 읽을 수 있습니다.",
    "VET_WRITE": "skill-bound 점검 중에는 격리 폴더 안쪽에만 쓸 수 있습니다.",
    "VET_TOOL": "skill-bound 점검 중에는 이 도구를 쓸 수 없습니다.",
    "VET_BROKEN": "skill-bound: 점검 상태 파일을 읽을 수 없어 점검 모드로 막고 있습니다. `vet.py end` 로 점검을 끝내세요.",
    "GUARD_ERROR": "skill-bound: 훅이 판단하지 못해 안전을 위해 막았습니다.",
    "CARD_MISSING": "skill-bound: 선택 버튼의 질문 글에 점검 카드가 없습니다. 질문 글 맨 위에 아래 카드를 줄바꿈까지 그대로, 한 글자도 바꾸지 말고 넣은 뒤 빈 줄 하나, 그다음 \"이 스킬을 설치할까요?\" 를 적어 다시 띄우세요. 카드는 사용자가 판정을 직접 보기 위한 것입니다.",
}


# ---------------------------------------------------------------------------
# 환경 (테스트에서 바꿔 끼울 수 있게 한 곳에 모은다)
# ---------------------------------------------------------------------------

class Ctx(object):
    def __init__(self, home=None, sb_dir=None, state_dir=None, tmp_dir=None, now=None):
        # type: (Optional[str], Optional[str], Optional[str], Optional[str], Optional[float]) -> None
        self.home = os.path.realpath(home or os.path.expanduser("~"))
        here = os.path.dirname(os.path.abspath(__file__))
        self.sb_dir = os.path.realpath(sb_dir or os.path.dirname(here))
        self.state_dir = os.path.realpath(state_dir or os.path.join(self.home, ".claude", "skill-bound-state"))
        self._tmp_dir = os.path.realpath(tmp_dir) if tmp_dir else None
        self.now = time.time() if now is None else now

    @property
    def tmp_dir(self):
        # type: () -> str
        if self._tmp_dir is None:
            import tempfile
            self._tmp_dir = os.path.realpath(tempfile.gettempdir())
        return self._tmp_dir


# ---------------------------------------------------------------------------
# 상태 파일 (vet.py, install_checked.py도 이 함수들을 불러 쓴다)
# ---------------------------------------------------------------------------

class StateError(Exception):
    pass


def valid_session_id(session_id):
    # type: (object) -> bool
    return isinstance(session_id, str) and bool(SESSION_ID_RE.match(session_id))


def session_state_path(state_dir, session_id):
    # type: (str, str) -> str
    if not valid_session_id(session_id):
        raise StateError("bad session id")
    return os.path.join(state_dir, "sessions", session_id + ".json")


def is_quarantine(path, tmp_dir):
    # type: (str, str) -> bool
    real = os.path.realpath(path)
    return (os.path.dirname(real) == os.path.realpath(tmp_dir)
            and os.path.basename(real).startswith("skill-bound-")
            and os.path.isdir(real) and not os.path.islink(path))


def _atomic_write_json(path, data):
    # type: (str, object) -> None
    import tempfile
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_session_state(state_dir, session_id, quarantine, now=None):
    # type: (str, str, str, Optional[float]) -> None
    _atomic_write_json(session_state_path(state_dir, session_id), {
        "quarantine": os.path.realpath(quarantine),
        "started": time.time() if now is None else now,
    })


def clear_session_state(state_dir, session_id):
    # type: (str, str) -> None
    path = session_state_path(state_dir, session_id)
    if os.path.lexists(path):
        os.unlink(path)


def any_vet_active(ctx):
    # type: (Ctx) -> bool
    """어느 세션이든 점검 모드가 켜져 있을 수 있는지. 입력이 깨져 세션을 모를 때 쓴다."""
    folder = os.path.join(ctx.state_dir, "sessions")
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return False
    for name in names:
        if name.endswith(".json") and get_mode(ctx, name[:-5])[0] in ("vet", "broken"):
            return True
    return False


def card_path(state_dir, quarantine):
    # type: (str, str) -> str
    """reader.py가 만든 요약 카드를 두는 곳. 상태 폴더 안이라 Claude가 바꿀 수 없다."""
    return os.path.join(state_dir, "reports", os.path.basename(os.path.realpath(quarantine)) + ".json")


def save_card(state_dir, quarantine, card):
    # type: (str, str, str) -> None
    _atomic_write_json(card_path(state_dir, quarantine), {"card": card})


def load_card(state_dir, quarantine):
    # type: (str, str) -> Optional[str]
    try:
        with open(card_path(state_dir, quarantine), encoding="utf-8") as handle:
            card = json.load(handle).get("card")
    except (OSError, ValueError, AttributeError):
        return None
    return card if isinstance(card, str) and len(card) < 20000 else None


def remove_card(state_dir, quarantine):
    # type: (str, str) -> None
    try:
        os.unlink(card_path(state_dir, quarantine))
    except OSError:
        pass


def get_mode(ctx, session_id):
    # type: (Ctx, object) -> Tuple[str, Optional[str]]
    """("off" | "normal" | "vet" | "broken", 격리 폴더).

    broken: 상태 파일이 있는데 읽을 수 없다. 점검 모드처럼 막는다.
    """
    if os.path.lexists(os.path.join(ctx.state_dir, "OFF")):
        return "off", None
    if not valid_session_id(session_id):
        return "normal", None
    path = session_state_path(ctx.state_dir, str(session_id))
    if not os.path.lexists(path):
        return "normal", None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        quarantine = data["quarantine"]
        started = data["started"]
        if not isinstance(quarantine, str) or type(started) not in (int, float):
            return "broken", None
    except Exception:
        return "broken", None
    if ctx.now - started > VET_MAX_SECONDS or started > ctx.now + 60:
        return "normal", None
    if not is_quarantine(quarantine, ctx.tmp_dir):
        return "normal", None
    return "vet", os.path.realpath(quarantine)


# ---------------------------------------------------------------------------
# 경로 도우미
# ---------------------------------------------------------------------------

def _expand_home(path, home):
    # type: (str, str) -> str
    if path == "~" or path.startswith("~/"):
        return home + path[1:]
    return path


def resolve_path(path, cwd, home):
    # type: (str, str, str) -> str
    """도구 입력의 경로를 실제 절대 경로로 바꾼다. 있는 부분의 심볼릭 링크는 따라간다."""
    path = _expand_home(path, home)
    if not os.path.isabs(path):
        path = os.path.join(cwd or home, path)
    return os.path.realpath(path)


def inside_strict(path, folder):
    # type: (str, str) -> bool
    """허용 검사용. 글자 그대로 비교한다."""
    return path == folder or path.startswith(folder + os.sep)


def inside_loose(path, folder):
    # type: (str, str) -> bool
    """차단 검사용. 맥은 대소문자를 구분하지 않으니 소문자로 비교한다."""
    p, f = path.lower(), folder.lower()
    return p == f or p.startswith(f + os.sep)


def classify_config_path(real_path):
    # type: (str) -> Optional[str]
    """쓰기 대상이 Claude 설정이나 설치 위치이면 이유 코드를 돌려준다."""
    parts = real_path.lower().split(os.sep)
    base = parts[-1] if parts else ""
    if base == ".mcp.json":
        return "INSTALL_BLOCKED"
    if base in (".claude.json", "managed-settings.json"):
        return "CONFIG_BASH"
    for i, part in enumerate(parts[:-1]):
        if part != ".claude":
            continue
        nxt = parts[i + 1]
        if nxt in INSTALL_DIRS:
            return "INSTALL_BLOCKED"
        if nxt == "hooks":
            return "CONFIG_BASH"
        if i + 1 == len(parts) - 1 and nxt in ("settings.json", "settings.local.json"):
            return "SETTINGS_ASK"
    return None


def safe_glob_pattern(pattern):
    # type: (object) -> bool
    if not isinstance(pattern, str) or not pattern or "\x00" in pattern:
        return False
    if pattern.startswith("/") or pattern.startswith("~"):
        return False
    return ".." not in pattern.replace("\\", "/").split("/") and "{" not in pattern


# ---------------------------------------------------------------------------
# Bash 명령 도우미
# ---------------------------------------------------------------------------

def parse_sb_command(command, ctx):
    # type: (str, Ctx) -> Optional[Tuple[str, List[str]]]
    """정해진 형태의 skill-bound 스크립트 실행이면 (스크립트 이름, 인자)를 돌려준다.

    형태: /usr/bin/python3 -I [-B] <skill-bound 폴더>/scripts/<이름>.py [인자...]
    조각 사이는 빈칸 하나. 조각마다 SB_TOKEN_RE 문자만.
    """
    if not isinstance(command, str):
        return None
    parts = command.strip().split(" ")
    if any(not SB_TOKEN_RE.match(p) for p in parts):
        return None
    if len(parts) < 3 or parts[0] != SYSTEM_PYTHON or parts[1] != "-I":
        return None
    index = 2
    if parts[index] == "-B":
        index += 1
    if index >= len(parts):
        return None
    script = parts[index]
    for name in SB_SCRIPTS:
        if script == os.path.join(ctx.sb_dir, "scripts", name):
            return name, parts[index + 1:]
    return None


def normalize_command(command, home):
    # type: (str, str) -> str
    """경로 언급을 찾기 쉽게 다듬는다. 판단(막기)에만 쓰고 실행에는 쓰지 않는다.

    - 따옴표와 역슬래시를 지운다 (`~/".claude"/skills`, `.cl\\aude` 같은 쪼개기 대비)
    - $HOME, ${HOME}, 낱말 앞의 ~ 를 홈 경로로 바꾼다 (`--output=~/...` 의 ~ 포함)
    - 소문자로 바꾼다 (맥은 대소문자를 구분하지 않는다)
    """
    text = re.sub(r"[\"'\\]", "", command)
    text = text.replace("${HOME}", home).replace("$HOME", home)
    text = re.sub(r"(^|[\s=:])~(?=/|\s|$)", lambda m: m.group(1) + home, text)
    return text.lower()


def is_read_only_command(command):
    # type: (str) -> bool
    """셸 연산자가 없는 단일 명령이고, 내용을 바꾸지 않는 프로그램이면 참."""
    if any(ch in SHELL_META for ch in command):
        return False
    import shlex
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    program = os.path.basename(tokens[0])
    if program not in READ_ONLY_PROGRAMS:
        return False
    for token in tokens[1:]:
        lowered = token.lower()
        # 출력 파일을 정하는 옵션. 값이 붙은 형태(-o파일, --output=파일)도 막는다.
        if lowered.startswith("--output") or lowered.startswith("--out="):
            return False
        if program == "tree" and lowered.startswith("-o"):
            return False
        for bad in READ_ONLY_BAD_OPTIONS.get(program, ()):
            if lowered == bad or lowered.startswith(bad + "="):
                return False
    return True


WRITE_PROGRAM_RE = re.compile(
    r"(^|[\s;|&(`/])(cp|mv|ln|rsync|tee|install|ditto|rm|rmdir|mkdir|touch|chmod|chown|chflags|xattr|unzip|tar|bsdtar"
    r"|curl|wget|git|sed|perl|ruby|dd|truncate|patch|npm|npx|pnpm|yarn|bunx|pip|pip3|brew|claude"
    # 옵션으로 파일에 쓰는 명령들 (sort -o파일, xxd -r 입력 출력, uniq 입력 출력 등)
    r"|sort|xxd|tree|uniq|split|csplit|awk|gawk|base64|openssl|iconv|zip|gzip|gunzip|bzip2|xz|cpio|pax"
    r"|scp|sftp|defaults|plutil|hdiutil|osascript|vi|vim|nvim|nano|emacs|ed|ex"
    # 읽기 명령이지만 옵션에 따라 지우거나 실행하는 것 (find -delete, find -exec, rg --pre)
    r"|find|rg)(\s|$)"
)


def writes_something(command, norm):
    # type: (str, str) -> bool
    """파일을 만들거나 바꿀 수 있는 명령이 섞여 있는지. 넓게 잡는다(막는 쪽 판단이므로)."""
    return ">" in command or bool(WRITE_PROGRAM_RE.search(norm))


def _cwd_in_claude_dir(cwd):
    # type: (str) -> bool
    return ".claude" in os.path.realpath(cwd or "/").lower().split(os.sep)


# ---------------------------------------------------------------------------
# 판단
# ---------------------------------------------------------------------------

PASS, DENY, ASK = "pass", "deny", "ask"


def sb_script_decision(name, args):
    # type: (str, List[str]) -> Tuple[str, str]
    """정해진 형태로 실행하는 skill-bound 스크립트의 판단. 평소 모드, 점검 모드 모두 같다."""
    if name == "install_checked.py":
        return ASK, "INSTALL_ASK"
    if name == "switch.py":
        # 정확히 하나의 낱말만 받는다. 그 밖의 형태는 지금은 무해해도, 나중에 스크립트가
        # 인자를 다르게 읽게 되면 허락 없이 끄는 길이 될 수 있으니 막는다.
        if args == ["off"]:
            return ASK, "SWITCH_OFF_ASK"
        if args == ["uninstall"]:
            return ASK, "UNINSTALL_ASK"
        if args == ["on"]:
            return PASS, "OK"
        return DENY, "SB_FORM"
    return PASS, "OK"


def decide_normal(tool_name, tool_input, cwd, ctx):
    # type: (str, Dict[str, object], str, Ctx) -> Tuple[str, str]
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return PASS, "OK"
        parsed = parse_sb_command(command, ctx)
        if parsed is not None:
            return sb_script_decision(parsed[0], parsed[1])
        norm = normalize_command(command, ctx.home)
        if INSTALL_COMMAND_RE.search(norm):
            return DENY, "INSTALL_BLOCKED"
        read_only = is_read_only_command(command)
        mentions_self = (ctx.sb_dir.lower() in norm or ctx.state_dir.lower() in norm or "skill-bound-state" in norm)
        if mentions_self and not read_only:
            if "/scripts/" in norm and ".py" in norm and ctx.sb_dir.lower() in norm:
                return DENY, "SB_FORM"
            return DENY, "PROTECTED"
        # 설치된 스킬이 자기 스크립트를 실행하는 것(python3 ~/.claude/skills/x/run.py)은 평범한 사용이다.
        # 그래서 설정 경로를 언급하면서 "파일을 바꾸는 명령"까지 있을 때만 막는다.
        if not read_only and writes_something(command, norm):
            if INSTALL_MENTION_RE.search(norm):
                return DENY, "INSTALL_BLOCKED"
            if CONFIG_MENTION_RE.search(norm) or (ctx.home.lower() + "/.claude") in norm:
                return DENY, "CONFIG_BASH"
            if _cwd_in_claude_dir(cwd):
                return DENY, "CONFIG_BASH"
        return PASS, "OK"

    if tool_name in WRITE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(raw, str) or not raw:
            return PASS, "OK"
        target = resolve_path(raw, cwd, ctx.home)
        if inside_loose(target, ctx.sb_dir) or inside_loose(target, ctx.state_dir):
            return DENY, "PROTECTED"
        code = classify_config_path(target)
        if code == "SETTINGS_ASK":
            return ASK, code
        if code is not None:
            return DENY, code
        return PASS, "OK"

    return PASS, "OK"


def decide_vet(tool_name, tool_input, cwd, ctx, quarantine):
    # type: (str, Dict[str, object], str, Ctx, Optional[str]) -> Tuple[str, str]
    """점검 모드. quarantine이 None이면 상태 파일이 깨진 경우다."""
    if tool_name == "Bash":
        parsed = parse_sb_command(tool_input.get("command"), ctx)  # type: ignore
        if parsed is None:
            return DENY, ("VET_BROKEN" if quarantine is None else "VET_BASH")
        if quarantine is None and parsed[0] not in ("vet.py", "switch.py"):
            return DENY, "VET_BROKEN"
        return sb_script_decision(parsed[0], parsed[1])

    if quarantine is None:
        return DENY, "VET_BROKEN"

    if tool_name in READ_TOOLS:
        if tool_name == "Read":
            raw = tool_input.get("file_path")
        else:
            raw = tool_input.get("path", cwd)
        if not isinstance(raw, str) or not raw:
            return DENY, "VET_READ"
        target = resolve_path(raw, cwd, ctx.home)
        if not (inside_strict(target, quarantine) or inside_strict(target, ctx.sb_dir)):
            return DENY, "VET_READ"
        if tool_name == "Glob" and not safe_glob_pattern(tool_input.get("pattern")):
            return DENY, "VET_READ"
        if tool_name == "Grep" and tool_input.get("glob") is not None and not safe_glob_pattern(tool_input.get("glob")):
            return DENY, "VET_READ"
        return PASS, "OK"

    if tool_name in WRITE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(raw, str) or not raw:
            return DENY, "VET_WRITE"
        target = resolve_path(raw, cwd, ctx.home)
        if not inside_strict(target, quarantine) or target == quarantine:
            return DENY, "VET_WRITE"
        return PASS, "OK"

    if tool_name == "AskUserQuestion":
        # 점검 결과 카드가 있으면, 선택 버튼의 질문 글에 그 카드가 그대로 들어 있어야 띄울 수 있다.
        # 훅은 화면에 직접 글을 띄울 수 없어서(통합 시험에서 확인), Claude가 질문 글에 넣고 훅이 검사한다.
        card = load_card(ctx.state_dir, quarantine)
        if card and not question_has_card(tool_input, card):
            return DENY, "CARD_MISSING"
        return PASS, "OK"
    if tool_name in VET_HARMLESS_TOOLS:
        return PASS, "OK"
    return DENY, "VET_TOOL"


def _squash(text):
    # type: (str) -> str
    return " ".join(text.split())


def question_has_card(tool_input, card):
    # type: (object, str) -> bool
    """질문 글 중 하나에 카드가 들어 있는지. 빈칸과 줄바꿈의 차이는 봐주고, 글자는 그대로여야 한다."""
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("questions"), list):
        return False
    want = _squash(card)
    for item in tool_input["questions"]:
        if isinstance(item, dict) and isinstance(item.get("question"), str) and want in _squash(item["question"]):
            return True
    return False


def decide(event, ctx):
    # type: (Dict[str, object], Ctx) -> Tuple[str, str]
    """PreToolUse 판단. (pass | deny | ask, 이유 코드)."""
    mode, quarantine = get_mode(ctx, event.get("session_id"))
    if mode == "off":
        return PASS, "OK"
    tool_name = event.get("tool_name")
    tool_input = event.get("tool_input")
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = ctx.home
    if not isinstance(tool_input, dict):
        tool_input = {}
    if not isinstance(tool_name, str):
        return (PASS, "OK") if mode == "normal" else (DENY, "VET_TOOL")
    if mode == "normal":
        try:
            return decide_normal(tool_name, tool_input, cwd, ctx)
        except Exception:
            return PASS, "OK"
    return decide_vet(tool_name, tool_input, cwd, ctx, quarantine)


def hook_output(action, code, extra=None):
    # type: (str, str, Optional[str]) -> Optional[Dict[str, object]]
    """extra는 이유 문장 뒤에 붙이는 글(점검 카드). 카드는 skill-bound가 만든 고정 문장과 경로뿐이다."""
    if action == PASS:
        return None
    reason = MESSAGES.get(code, MESSAGES["GUARD_ERROR"])
    if extra:
        reason = reason + "\n\n" + extra
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny" if action == DENY else "ask",
            "permissionDecisionReason": reason,
        }
    }


# ---------------------------------------------------------------------------
# SessionStart: 설치된 스킬 등의 기준선 비교
# ---------------------------------------------------------------------------

BASELINE_MAX_FILES = 5000
BASELINE_HASH_MAX_BYTES = 1024 * 1024  # 이보다 큰 파일은 크기와 수정 시각으로만 비교
SETTINGS_KEYS = ("hooks", "enabledPlugins", "extraKnownMarketplaces", "mcpServers", "permissions",
                 "disableAllHooks", "apiKeyHelper", "statusLine")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._:/\-]{1,200}$")


def _hash_bytes(data):
    # type: (bytes) -> str
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _hash_file(path, st):
    # type: (str, os.stat_result) -> str
    if st.st_size > BASELINE_HASH_MAX_BYTES:
        return "meta:%d:%d" % (st.st_size, int(st.st_mtime))
    with open(path, "rb") as handle:
        return _hash_bytes(handle.read())


def _walk_tree(prefix, folder, out, budget, skip_dirs=()):
    # type: (str, str, Dict[str, str], List[int], Tuple[str, ...]) -> None
    """폴더 안 파일을 {키: 해시}에 넣는다. 심볼릭 링크는 따라가지 않고 대상 경로를 기록한다.

    skip_dirs에 있는 이름의 폴더는 건너뛴다(내용과 상관없이 늘 바뀌는 폴더).
    """
    if not os.path.isdir(folder) or os.path.islink(folder):
        return
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        abs_dir = os.path.join(folder, rel_dir) if rel_dir else folder
        try:
            names = sorted(os.listdir(abs_dir))
        except OSError:
            continue
        for name in names:
            rel = name if not rel_dir else rel_dir + "/" + name
            full = os.path.join(abs_dir, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if name not in skip_dirs:
                    stack.append(rel)
                continue
            budget[0] -= 1
            if budget[0] < 0:
                out[prefix + ":<too-many-files>"] = "limit"
                return
            if stat.S_ISLNK(st.st_mode):
                try:
                    out[prefix + ":" + rel] = "link:" + os.readlink(full)
                except OSError:
                    pass
            elif stat.S_ISREG(st.st_mode):
                try:
                    out[prefix + ":" + rel] = _hash_file(full, st)
                except OSError:
                    pass


def _hash_json_keys(prefix, path, keys, out):
    # type: (str, str, Tuple[str, ...], Dict[str, str]) -> None
    """설정 파일에서 보안에 중요한 칸만 골라 해시한다(모델 설정 같은 잦은 변경은 무시)."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for key in keys:
        if key in data:
            out[prefix + ":" + key] = _hash_bytes(json.dumps(data[key], sort_keys=True).encode("utf-8"))


def collect_inventory(ctx, project_dir):
    # type: (Ctx, Optional[str]) -> Dict[str, str]
    out = {}  # type: Dict[str, str]
    budget = [BASELINE_MAX_FILES]
    claude_dir = os.path.join(ctx.home, ".claude")
    for sub in ("skills", "agents", "commands"):
        _walk_tree("user-" + sub, os.path.join(claude_dir, sub), out, budget)
    for name in ("installed_plugins.json", "known_marketplaces.json"):
        path = os.path.join(claude_dir, "plugins", name)
        try:
            out["user-plugins:" + name] = _hash_file(path, os.stat(path))
        except OSError:
            pass
    # .in_use/<프로세스 번호> 는 Claude가 켜질 때 생기고 꺼질 때 사라지는 표시 파일이다.
    # 플러그인 내용이 아니므로 비교에서 뺀다(넣으면 세션마다 "바뀌었다"는 거짓 알림이 뜬다).
    _walk_tree("user-plugin-cache", os.path.join(claude_dir, "plugins", "cache"), out, budget, skip_dirs=(".in_use",))
    for name in ("settings.json", "settings.local.json"):
        _hash_json_keys("user-" + name, os.path.join(claude_dir, name), SETTINGS_KEYS, out)
    # ~/.claude.json 에는 MCP 서버 설정이 들어 있다.
    try:
        with open(os.path.join(ctx.home, ".claude.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            # MCP 서버가 하나도 없으면(칸이 없거나 빈 값) 기록하지 않는다. 통합 시험에서, 새 폴더를
            # 처음 "믿겠다"고 하면 Claude가 빈 mcpServers 칸을 만들어 "MCP 설정이 생겼다"는 거짓 알림이 떴다.
            if data.get("mcpServers"):
                out["user-claude.json:mcpServers"] = _hash_bytes(json.dumps(data.get("mcpServers"), sort_keys=True).encode())
            projects = data.get("projects")
            if isinstance(projects, dict) and project_dir and isinstance(projects.get(project_dir), dict):
                servers = projects[project_dir].get("mcpServers")
                if servers:
                    out["project-claude.json:mcpServers"] = _hash_bytes(json.dumps(servers, sort_keys=True).encode())
    except (OSError, ValueError):
        pass
    if project_dir:
        pclaude = os.path.join(project_dir, ".claude")
        for sub in ("skills", "agents", "commands"):
            _walk_tree("project-" + sub, os.path.join(pclaude, sub), out, budget)
        for name in ("settings.json", "settings.local.json"):
            _hash_json_keys("project-" + name, os.path.join(pclaude, name), SETTINGS_KEYS, out)
        mcp = os.path.join(project_dir, ".mcp.json")
        try:
            out["project:.mcp.json"] = _hash_file(mcp, os.stat(mcp))
        except OSError:
            pass
    return out


def baseline_path(ctx, project_dir):
    # type: (Ctx, Optional[str]) -> str
    key = _hash_bytes((project_dir or "").encode("utf-8"))[:16]
    return os.path.join(ctx.state_dir, "baselines", key + ".json")


def load_baseline(path):
    # type: (str) -> Optional[Dict[str, str]]
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return None
    return {str(k): str(v) for k, v in entries.items()}


def save_baseline(path, entries):
    # type: (str, Dict[str, str]) -> None
    _atomic_write_json(path, {"format_version": 1, "entries": entries})


def _item_name(key):
    # type: (str) -> str
    """알림에 보여 줄 항목 이름. 경로 첫 부분(스킬 이름 등)까지만, 안전한 문자일 때만."""
    prefix, _, rest = key.partition(":")
    first = rest.split("/")[0] if "/" in rest else rest
    name = prefix + ":" + first
    return name if SAFE_NAME_RE.match(name) else prefix + ":<unsafe-name>"


def diff_baseline(old, new):
    # type: (Dict[str, str], Dict[str, str]) -> Tuple[List[str], List[str], List[str]]
    added = sorted({_item_name(k) for k in new if k not in old})
    removed = sorted({_item_name(k) for k in old if k not in new})
    changed = sorted({_item_name(k) for k in new if k in old and new[k] != old[k]})
    return added, changed, removed


def session_start(event, ctx):
    # type: (Dict[str, object], Ctx) -> Optional[Dict[str, object]]
    """바뀐 것이 없으면 None(아무것도 출력하지 않음). 바뀌었으면 알림 내용."""
    if os.path.lexists(os.path.join(ctx.state_dir, "OFF")):
        return None
    cwd = event.get("cwd")
    project_dir = os.path.realpath(cwd) if isinstance(cwd, str) and cwd else None
    if project_dir == ctx.home:
        project_dir = None
    path = baseline_path(ctx, project_dir)
    new = collect_inventory(ctx, project_dir)
    old = load_baseline(path)
    save_baseline(path, new)
    if old is None or old == new:
        return None
    added, changed, removed = diff_baseline(old, new)

    def show(items):
        # type: (List[str]) -> str
        if not items:
            return "없음"
        more = len(items) - 10
        return ", ".join(items[:10]) + (" 외 %d개" % more if more > 0 else "")

    text = ("skill-bound: 지난번 이후 설치된 스킬, 플러그인, 훅, MCP 설정이 바뀌었습니다. "
            "추가: %s. 변경: %s. 삭제: %s. skill-bound 점검을 거친 변경인지 확인하세요." % (
                show(added), show(changed), show(removed)))
    return {
        "systemMessage": text,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text},
    }


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def _read_event():
    # type: () -> Dict[str, object]
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input too large")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def main_pre(ctx):
    # type: (Ctx) -> int
    event = None  # type: Optional[Dict[str, object]]
    try:
        event = _read_event()
        action, code = decide(event, ctx)
    except Exception:
        # 상태를 알 수 있으면 평소 모드는 통과, 그 밖에는 막는다.
        action, code = DENY, "GUARD_ERROR"
        try:
            if os.path.lexists(os.path.join(ctx.state_dir, "OFF")):
                action, code = PASS, "OK"
            elif isinstance(event, dict) and valid_session_id(event.get("session_id")):
                if get_mode(ctx, event.get("session_id"))[0] == "normal":
                    action, code = PASS, "OK"
            elif not any_vet_active(ctx):
                # 입력이 깨져 세션을 알 수 없지만, 점검 중인 세션이 하나도 없으면 평소 모드로 본다.
                action, code = PASS, "OK"
        except Exception:
            pass
    extra = None
    if code == "CARD_MISSING":
        try:
            extra = load_card(ctx.state_dir, str(get_mode(ctx, event.get("session_id"))[1]))  # type: ignore
        except Exception:
            extra = None
    out = hook_output(action, code, extra)
    if out is not None:
        print(json.dumps(out, ensure_ascii=False))
    return 0


def main_session_start(ctx):
    # type: (Ctx) -> int
    try:
        out = session_start(_read_event(), ctx)
    except Exception:
        return 0  # 세션 시작은 절대 막지 않는다.
    if out is not None:
        print(json.dumps(out, ensure_ascii=False))
    return 0


def main(argv):
    # type: (List[str]) -> int
    ctx = Ctx()
    if len(argv) == 2 and argv[1] == "pre":
        return main_pre(ctx)
    if len(argv) == 2 and argv[1] == "session-start":
        return main_session_start(ctx)
    # 잘못 등록된 경우: 막는 쪽으로
    print(json.dumps(hook_output(DENY, "GUARD_ERROR"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
