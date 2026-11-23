#!/usr/bin/env python3
"""skill-bound 정적 스캔.

격리 폴더 안의 파일을 읽기만 하고(아무것도 실행하지 않는다), 위험해 보이는
패턴 후보를 넓게 찾아 낸다.

중요한 약속:
- 결과에는 파일 내용을 한 글자도 넣지 않는다. 규칙 이름, 분류, 위험도,
  미리 써 둔 고정 문장, 파일 경로, 줄 번호만 내보낸다. 이 결과는 본 세션의
  Claude가 읽기 때문에, 파일 안의 함정 문장이 결과를 타고 새어 나가면 안 된다.
- 파일 이름도 공격자가 정한 값이다. 안전한 문자만 쓴 이름이 아니면 가린다.
- 심볼릭 링크는 따라가지 않고, 일반 파일이 아닌 것(파이프, 장치 등)은 열지 않는다.

사용법:
    /usr/bin/python3 -I scan.py <폴더> [--format json|text]

파이썬 3.9 문법까지만 쓰고, 표준 라이브러리만 쓴다.
"""

import argparse
import json
import os
import re
import stat
import sys
import unicodedata
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 한도
# ---------------------------------------------------------------------------

MAX_FILES = 2000                  # 이보다 많으면 나머지는 점검하지 않고 "판단 불가"
MAX_FILE_BYTES = 1024 * 1024      # 파일 하나의 최대 크기 (1MB)
MAX_TOTAL_BYTES = 20 * 1024 * 1024  # 전체 읽기 한도 (20MB)
MAX_HITS_PER_RULE_FILE = 3        # 같은 파일에서 같은 규칙은 처음 3줄만 기록
MAX_FINDINGS = 300                # 결과에 담는 발견 개수 상한
LONG_LINE_CHARS = 5000            # 이보다 긴 줄은 숨긴 코드일 수 있어 표시
MAX_PATH_CHARS = 200

# 위험도 순서. 둘 중 더 나쁜 쪽을 고를 때 쓴다.
# "unknown"(판단 불가)은 초록이나 노랑보다 나쁘게 본다. 절대 초록으로 넘어가지 않게.
SEVERITY_RANK = {"green": 0, "yellow": 1, "unknown": 2, "red": 3}

SEVERITY_LABEL_KO = {
    "green": "초록 (통과)",
    "yellow": "노랑 (조건부)",
    "unknown": "판단 불가",
    "red": "빨강 (설치 비권장)",
}

SEVERITY_TAG_KO = {"yellow": "노랑", "unknown": "판단 불가", "red": "빨강"}

# ---------------------------------------------------------------------------
# 줄 단위 규칙
#
# 각 줄은 보이지 않는 문자를 지우고 NFKC 정규화(전각 글자를 보통 글자로 바꾸기 등)를
# 한 뒤에 검사한다. 규칙 형식: (규칙 이름, 분류, 위험도, 고정 문장, 정규식[, 플래그])
# ---------------------------------------------------------------------------

_I = re.IGNORECASE

LINE_RULES_SPEC = [
    # --- REMOTE_EXEC: 받아서 바로 실행 ---
    ("RX_PIPE_TO_SHELL", "REMOTE_EXEC", "red",
     "인터넷에서 받은 내용을 바로 셸이나 인터프리터로 실행합니다.",
     r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(env\s+)?((ba|z|da|k|c|fi)?sh|python[0-9.]*|node|perl|ruby|php|osascript)\b"),
    ("RX_SUBSHELL_DOWNLOAD", "REMOTE_EXEC", "red",
     "인터넷에서 받은 내용을 셸 안에서 바로 실행합니다.",
     r"\b(ba|z|da|k)?sh\b[^\n]*(<\(|\$\()\s*(curl|wget)\b|\beval\b[^\n]*\$\(\s*(curl|wget)\b|\bsource\s+<\(\s*(curl|wget)\b"),
    ("RX_EXEC_DOWNLOAD", "REMOTE_EXEC", "red",
     "인터넷에서 받은 코드를 프로그램 안에서 바로 실행합니다.",
     r"(?<![\w.])(exec|eval)\s*\([^\n]*\b(urlopen|urllib|requests\.get|httpx\.get|fetch)\b"),
    ("RX_POWERSHELL_DOWNLOAD", "REMOTE_EXEC", "red",
     "인터넷에서 받은 내용을 PowerShell로 바로 실행합니다.",
     r"\b(iex|invoke-expression)\b[^\n]*\b(iwr|irm|invoke-webrequest|invoke-restmethod|downloadstring)\b"),

    # --- EXFIL: 정보 유출 ---
    ("EX_WEBHOOK_HOST", "EXFIL", "red",
     "몰래 정보를 받아 가는 데 자주 쓰이는 수신 주소가 있습니다.",
     r"discord(app)?\.com/api/webhooks|hooks\.slack\.com/services|webhook\.site|requestbin|pipedream\.net"
     r"|ngrok(-free)?\.(io|app|dev)|burpcollaborator\.net|oastify\.com|interact\.sh|\.oast\.(fun|me|live|site|online|pro)"
     r"|transfer\.sh|api\.telegram\.org/bot"),
    ("EX_UPLOAD_CMD", "EXFIL", "yellow",
     "명령으로 데이터를 외부에 올립니다. 무엇을 어디로 보내는지 확인하세요.",
     r"\bcurl\b[^\n]*\s(-d|--data(-binary|-raw|-urlencode)?|-F|--form|-T|--upload-file)(\s|=|$)|\bwget\b[^\n]*--post-(data|file)"),
    ("EX_RAW_SOCKET", "EXFIL", "red",
     "네트워크 연결을 직접 열어 데이터를 주고받습니다.",
     r"\b(nc|ncat|netcat|socat)\s+(-[a-zA-Z]+\s+)*[\w.-]+\s+\d{2,5}\b|/dev/tcp/"),

    # --- NETWORK: 인터넷 접속 (그 자체는 노랑) ---
    ("NET_CODE", "NETWORK", "yellow",
     "인터넷에 접속하는 코드가 있습니다. 접속 주소가 스킬 설명과 맞는지 확인하세요.",
     r"\b(urlopen|urllib\.request|urllib3|requests\.(get|post|put|patch|delete|request|Session)|http\.client|httpx|aiohttp"
     r"|socket\.(socket|create_connection)|XMLHttpRequest|axios|WebSocket)\b|(?<![\w.])fetch\s*\(|\bhttps?\.(get|request)\s*\("),
    ("NET_CLI", "NETWORK", "yellow",
     "인터넷에서 받거나 보내는 명령이 있습니다. 접속 주소가 스킬 설명과 맞는지 확인하세요.",
     r"\b(curl|wget|scp|rsync|sftp)\b"),

    # --- CREDENTIAL: 비밀번호, 키 접근 ---
    ("CR_SSH_KEY", "CREDENTIAL", "red",
     "SSH 키에 접근합니다.",
     r"\.ssh(/|\b)|\bid_(rsa|ed25519|ecdsa|dsa)\b|authorized_keys"),
    ("CR_CLOUD_CREDS", "CREDENTIAL", "red",
     "클라우드나 개발 도구의 로그인 정보 파일에 접근합니다.",
     r"\.aws/(credentials|config)|\.config/gcloud|\.azure/|\.kube/config|\.docker/config\.json|\.netrc\b|\.npmrc\b"
     r"|\.pypirc\b|\.config/gh/|\.git-credentials|\.terraformrc|\.vault-token"),
    ("CR_KEYCHAIN", "CREDENTIAL", "red",
     "macOS 키체인에 저장된 비밀번호에 접근합니다.",
     r"\bsecurity\s+(find-(generic|internet)-password|dump-keychain|export)\b|/Keychains/|login\.keychain"),
    ("CR_BROWSER_DATA", "CREDENTIAL", "red",
     "브라우저의 쿠키나 저장된 비밀번호에 접근합니다.",
     r"Login Data|Cookies\.binarycookies|/Cookies['\"\s]|key4\.db|logins\.json"
     r"|Application Support/(Google/Chrome|BraveSoftware|Firefox|Microsoft Edge)|Library/Safari"),
    ("CR_CLAUDE_CREDS", "CREDENTIAL", "red",
     "Claude 로그인 정보나 계정 설정 파일에 접근합니다.",
     r"\.claude\.json|\.claude/\.credentials|\.credentials\.json"),
    ("CR_ENV_FILE", "CREDENTIAL", "yellow",
     ".env 파일(비밀 값이 자주 들어 있는 파일)에 접근합니다.",
     r"(^|[\s'\"/~=(`])\.env(\.[A-Za-z]+)?(?=$|[\s'\")`])"),
    ("CR_SECRET_NAME", "CREDENTIAL", "yellow",
     "API 키나 토큰 같은 비밀 값의 이름이 나옵니다. 왜 필요한지 확인하세요.",
     r"\b[A-Z][A-Z0-9_]*(API_KEY|APIKEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY)\b", 0),

    # --- PERSISTENCE: 자동 실행 등록 ---
    ("PE_LAUNCHD", "PERSISTENCE", "red",
     "컴퓨터를 켜거나 로그인할 때마다 자동으로 실행되게 등록합니다(LaunchAgents).",
     r"LaunchAgents|LaunchDaemons|\blaunchctl\s+(load|bootstrap|submit|enable|kickstart)\b"),
    ("PE_CRON", "PERSISTENCE", "red",
     "정해진 시간마다 자동으로 실행되게 등록합니다(cron).",
     r"\bcrontab\b|/etc/cron|/var/at/tabs|\bperiodic\.conf\b"),
    ("PE_SHELL_RC", "PERSISTENCE", "red",
     "터미널을 열 때마다 실행되는 설정 파일을 건드립니다.",
     r"(\.zshrc|\.zprofile|\.zshenv|\.zlogin|\.bashrc|\.bash_profile|\.bash_login|(?<![\w.])\.profile|config\.fish)\b"),
    ("PE_CLAUDE_SETTINGS", "PERSISTENCE", "red",
     "Claude Code 설정이나 훅을 바꿔 자동 실행을 심을 수 있습니다.",
     r"\.claude/(settings(\.local)?\.json|hooks)\b|managed-settings\.json"),
    ("PE_CLAUDE_DIRS", "PERSISTENCE", "yellow",
     "Claude의 스킬, 에이전트, 커맨드, 플러그인 폴더나 MCP 설정을 다룹니다. 설치 안내인지 확인하세요.",
     r"\.claude/(skills|agents|commands|plugins)\b|\.mcp\.json"),
    ("PE_GIT_HOOKS", "PERSISTENCE", "red",
     "git 훅을 심어 git 명령 때마다 실행되게 합니다.",
     r"\.git/hooks\b|core\.hooksPath"),
    ("PE_LOGIN_ITEMS", "PERSISTENCE", "red",
     "로그인할 때 자동으로 실행되는 항목을 등록합니다.",
     r"\blogin\s*items?\b|\bloginwindow\b|SMLoginItemSetEnabled|BackgroundItems"),

    # --- DESTRUCTIVE: 파괴적 명령 ---
    ("DE_RM_BROAD", "DESTRUCTIVE", "red",
     "홈 폴더나 디스크 전체를 지우는 명령이 있습니다.",
     r"\brm\s+(-[A-Za-z-]+\s+)*(/|~|~/|\$HOME|\$\{HOME\}|\"\$HOME\"|\"\$\{HOME\}\")(/\*|\*)?(\s|;|&|\||\"|$)"
     r"|\brmtree\s*\(\s*(os\.path\.expanduser\(\s*['\"]~|['\"]/['\"]|Path\.home\(\))"
     r"|\bfind\s+(/|~|\$HOME)\s[^\n]*-delete\b"),
    ("DE_DISK", "DESTRUCTIVE", "red",
     "디스크를 지우거나 덮어쓰는 명령이 있습니다.",
     r"\bmkfs(\.\w+)?\b|\bdiskutil\s+(erase\w*|zeroDisk|randomDisk|secureErase|partitionDisk|reformat)\b|\bdd\s+[^\n]*\bof=/dev/"),
    ("DE_GIT_REWRITE", "DESTRUCTIVE", "yellow",
     "git 기록이나 작업 내용을 강제로 덮어쓰는 명령이 있습니다.",
     r"\bgit\s+push\b[^\n]*\s(--force|-f|--force-with-lease)\b|\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-[a-z]*f"),
    ("DE_PERM_WIPE", "DESTRUCTIVE", "red",
     "넓은 범위의 파일 권한이나 주인을 통째로 바꾸는 명령이 있습니다.",
     r"\bchmod\s+(-R\s+)?(0?777|a\+rwx)\s+(/|~|\$HOME)|\bchown\s+-R\s+\S+\s+(/|~|\$HOME)(\s|$)"),

    # --- OBFUSCATION: 난독화 ---
    ("OB_DECODE_EXEC", "OBFUSCATION", "red",
     "숨겨 둔(인코딩한) 코드를 풀어서 바로 실행합니다.",
     r"(base64\s+(-d|--decode|-D)|b64decode|atob\s*\(|fromCharCode|bytes\.fromhex|codecs\.decode|zlib\.decompress|marshal\.loads|xxd\s+-r)"
     r"[^\n]{0,120}(\|\s*(ba|z)?sh\b|\bexec\b|\beval\b)"
     r"|(?<![\w.])(exec|eval)\s*\([^\n]{0,120}(b64decode|atob|fromCharCode|fromhex|codecs\.decode|decompress|marshal\.loads|rot13|\[::-1\])"),
    ("OB_DYNAMIC_CODE", "OBFUSCATION", "yellow",
     "문자열을 코드로 바꿔 실행하는 부분이 있습니다(eval, exec 등).",
     r"(?<![\w.])(eval|exec)\s*\(|\bnew\s+Function\s*\(|(?<![\w.])compile\s*\([^\n]*['\"]exec['\"]"),
    ("OB_DYNAMIC_IMPORT", "OBFUSCATION", "yellow",
     "실행 중에 이름을 조립해서 모듈을 불러옵니다.",
     r"\b__import__\s*\(|\bimportlib\.import_module\s*\("),
    ("OB_LONG_BLOB", "OBFUSCATION", "yellow",
     "아주 긴 인코딩 문자열이 있습니다. 안에 무엇이 들어 있는지 확인하세요.",
     r"[A-Za-z0-9+/]{200,}={0,2}", 0),
    ("OB_ESCAPED_BYTES", "OBFUSCATION", "yellow",
     "읽기 어렵게 바꾼 문자들이 연달아 있습니다.",
     r"(\\x[0-9a-fA-F]{2}){8,}|(\\u[0-9a-fA-F]{4}){8,}|(chr\(\s*\d+\s*\)\s*\+\s*){4,}|fromCharCode\(\s*(\d+\s*,\s*){6,}"),

    # --- HIDDEN_INSTRUCTION: AI를 노린 숨은 지시 (영어) ---
    ("HI_IGNORE_RULES", "HIDDEN_INSTRUCTION", "red",
     "AI에게 원래 지시를 무시하라고 하는 문장이 있습니다.",
     r"\b(ignore|disregard|forget|override|bypass)\b[^\n]{0,40}\b(previous|prior|above|earlier|all|any|your|system|safety|security)\b"
     r"[^\n]{0,30}\b(instructions?|rules|prompts?|guidelines|directions|policies|constraints)\b"),
    ("HI_REPORT_SAFE", "HIDDEN_INSTRUCTION", "red",
     "점검 결과를 '안전'으로 보고하라고 유도하는 문장이 있습니다.",
     r"\b(report|mark|classify|rate|label|declare)\b[^\n]{0,60}\bas\s+(safe|benign|harmless|clean|trusted|green|secure|low[\s-]risk)\b"
     r"|\b(this|the)\s+(skill|plugin|file|code|repo|repository|package)\s+(is|has\s+been)\s+(completely\s+|totally\s+|100%\s+|already\s+)?"
     r"(safe|benign|harmless|trusted|verified|audited|approved)\b"),
    ("HI_HIDE_FROM_USER", "HIDDEN_INSTRUCTION", "red",
     "사용자에게 숨기라고 하는 문장이 있습니다.",
     r"\b(do\s*n[o']?t|never|without|avoid)\b[^\n]{0,25}\b(tell|telling|inform|informing|mention|mentioning|notify|notifying"
     r"|alert|alerting|show|showing|reveal|revealing)\b[^\n]{0,25}\b(the\s+)?(user|human|owner|operator)s?\b"),
    ("HI_SILENT_ACTION", "HIDDEN_INSTRUCTION", "yellow",
     "몰래 무언가를 실행하거나 보내라는 문장이 있습니다.",
     r"\b(silently|secretly|covertly)\b[^\n]{0,30}\b(run|execute|send|upload|install|copy|delete|post)\b"),
    ("HI_ROLE_OVERRIDE", "HIDDEN_INSTRUCTION", "red",
     "AI의 역할이나 시스템 지시를 바꾸려는 문장이 있습니다.",
     r"\byou\s+are\s+now\b|\bnew\s+(system\s+)?instructions?\s*:|\bsystem\s*(prompt|message|override|instruction)\s*:"
     r"|<\s*/?\s*(system|assistant|instructions?|im_start|im_end)\s*>|\[\s*/?\s*(system|INST)\s*\]"
     r"|\bfrom\s+now\s+on\b[^\n]{0,20}\byou\b|\bdeveloper\s+mode\b"),
    ("HI_OUTPUT_HIJACK", "HIDDEN_INSTRUCTION", "red",
     "정해진 출력 대신 다른 내용을 내라고 시키는 문장이 있습니다.",
     r"\b(instead\s+of|rather\s+than)\b[^\n]{0,30}\b(json|checklist|report|verdict|output|findings)\b"
     r"|\b(output|print|respond\s+with|reply\s+with|return)\s+(only\s+)?(exactly\s+)?the\s+following\b"
     r"|[\"']verdict[\"']\s*:\s*[\"']green[\"']"),
    ("HI_AI_ADDRESS", "HIDDEN_INSTRUCTION", "yellow",
     "AI나 점검 도구를 직접 부르는 문장이 있습니다.",
     r"\b(note|message|instructions?)\s+(to|for)\s+(the\s+|any\s+)?(ai|llm|assistant|model|agent|claude|reviewer|scanner|auditor)s?\b"
     r"|\b(dear|hey|attention)\s+(ai|llm|assistant|claude|model|reviewer|scanner)\b"
     r"|\bif\s+you\s+are\s+an?\s+(ai|llm|language\s+model|assistant)\b"),

    # --- HIDDEN_INSTRUCTION: 한국어 ---
    ("HI_KO_IGNORE_RULES", "HIDDEN_INSTRUCTION", "red",
     "AI에게 원래 지시를 무시하라고 하는 문장이 있습니다(한국어).",
     r"(이전|앞|위|기존|원래|모든|시스템)[^\n]{0,15}(지시|지침|명령|규칙|프롬프트)[^\n]{0,10}(무시|잊어|잊고|따르지)"),
    ("HI_KO_REPORT_SAFE", "HIDDEN_INSTRUCTION", "red",
     "점검 결과를 '안전'으로 보고하라고 유도하는 문장이 있습니다(한국어).",
     r"(안전|무해|문제\s*없|깨끗|초록)[^\n]{0,10}(하다고|다고|이라고|라고|으로|로)\s*(보고|판정|표시|분류|답|출력|말|적)"
     r"|(이|본)\s*(스킬|플러그인|코드|파일|저장소)\S{0,2}\s*(완전히\s*|매우\s*|이미\s*)?(안전|무해|검증)"),
    ("HI_KO_HIDE_FROM_USER", "HIDDEN_INSTRUCTION", "red",
     "사용자에게 숨기라고 하는 문장이 있습니다(한국어).",
     r"(사용자|유저)\S{0,4}[^\n]{0,10}(알리지|말하지|보여\s*주지)\s*(마|말|않)|몰래[^\n]{0,15}(실행|보내|전송|설치|복사|삭제)"),
    ("HI_KO_OUTPUT_HIJACK", "HIDDEN_INSTRUCTION", "red",
     "정해진 출력 대신 다른 내용을 내라고 시키거나 AI의 역할을 바꾸려는 문장이 있습니다(한국어).",
     r"(JSON|제이슨|체크리스트|양식|보고서|판정)\s*(대신|말고)|다음\s*(문장|내용|글)[^\n]{0,6}(출력|답|말)하"
     r"|너는\s*이제|지금부터\s*(너는|당신은)|새로운\s*(지시|지침)\s*:"),

    # --- PERMISSION: 과한 권한 ---
    ("PM_SKIP_PERMISSIONS", "PERMISSION", "red",
     "권한 확인을 건너뛰게 하는 설정이 있습니다.",
     r"dangerously[-_]?skip[-_]?permissions|bypassPermissions|skipDangerousModePermissionPrompt"
     r"|--permission-mode[\s=]+(bypassPermissions|auto)\b|allow-dangerously"),
    ("PM_BROAD_TOOLS", "PERMISSION", "yellow",
     "모든 셸 명령이나 모든 도구를 허용하는 설정이 있습니다.",
     r"^\s*[\"']?allowed[-_]?tools[\"']?\s*:[^\n]*(\bBash\b(?!\s*\()|Bash\(\s*\*\s*\)|[\"']\*[\"'])"),
    ("PM_SUDO", "PERMISSION", "yellow",
     "관리자 권한(sudo)을 씁니다.",
     r"\bsudo\b|\bdoas\b|with administrator privileges"),
    ("PM_SETUID", "PERMISSION", "red",
     "실행할 때 높은 권한을 얻게 하는 설정(setuid)이 있습니다.",
     r"\bchmod\s+([ugoa]*\+[rwx]*s|[2467][0-7]{3})\b"),
    ("PM_MACOS_SECURITY", "PERMISSION", "red",
     "macOS 보안 기능을 끄거나 우회합니다.",
     r"\btccutil\b|TCC\.db|\bspctl\s+--(master-disable|global-disable|add)\b|\bcsrutil\s+disable\b"
     r"|\bxattr\s+[^\n]*-[a-z]*d[a-z]*\s+com\.apple\.quarantine|\bxattr\s+-[a-z]*c"),

    # --- REMOTE_CONTENT: 실행 때마다 원격에서 받아옴 ---
    ("RC_RAW_URL", "REMOTE_CONTENT", "yellow",
     "원격 파일의 원본 주소가 있습니다. 실행 때 받아 오면 나중에 내용이 바뀔 수 있습니다.",
     r"raw\.githubusercontent\.com|gist\.githubusercontent\.com|gist\.github\.com|pastebin\.com/raw|hastebin"),
    ("RC_FETCH_INSTRUCTIONS", "REMOTE_CONTENT", "yellow",
     "지시나 규칙을 인터넷에서 받아 와 따르라고 합니다. 나중에 내용이 바뀔 수 있습니다.",
     r"\b(fetch|download|load|read|retrieve|pull|get)\b[^\n]{0,40}\b(instructions?|prompts?|rules|guidelines|latest\s+(version|skill)|skill\s+file|SKILL\.md)\b[^\n]{0,60}https?://"
     r"|https?://\S+[^\n]{0,30}\b(and\s+)?(follow|obey|execute)\b"
     r"|https?://\S+[^\n]{0,30}(받아|불러|가져)[^\n]{0,15}(따르|실행|적용)"),
    ("RC_PACKAGE_RUN", "REMOTE_CONTENT", "yellow",
     "실행할 때마다 패키지를 인터넷에서 받아 실행합니다. 나중에 내용이 바뀔 수 있습니다.",
     r"\bnpx\s+(-y\s+|--yes\s+)?[@\w]|\buvx\s+[\w@]|\bpipx\s+run\b|\bbunx\s+[\w@]|\bgo\s+run\s+\S+@|\bdeno\s+run\s+[^\n]*https?://"
     r"|[\"']command[\"']\s*:\s*[\"'](npx|uvx|bunx)[\"']"),
    ("RC_INSTALL_PACKAGE", "REMOTE_CONTENT", "yellow",
     "외부 패키지를 설치합니다.",
     r"\bpip3?\s+install\b|\bnpm\s+(i|install|add)\b|\byarn\s+add\b|\bpnpm\s+(add|install)\b|\bbrew\s+install\b|\bgem\s+install\b|\bcargo\s+install\b"),
]

# ---------------------------------------------------------------------------
# 파일 단위, 구조, 유니코드 규칙 (정규식 없이 코드로 판단)
# ---------------------------------------------------------------------------

OTHER_RULES_SPEC = [
    ("HI_HTML_COMMENT", "HIDDEN_INSTRUCTION", "yellow",
     "화면에 보이지 않는 주석 안에 AI나 점검과 관련된 말이 있습니다."),
    ("UC_TAG_CHARS", "HIDDEN_INSTRUCTION", "red",
     "눈에 보이지 않는 태그 문자로 숨긴 글이 있습니다."),
    ("UC_VARIATION_SMUGGLING", "HIDDEN_INSTRUCTION", "red",
     "눈에 보이지 않는 변형 선택 문자가 잔뜩 붙어 있습니다. 숨긴 데이터일 수 있습니다."),
    ("UC_BIDI", "OBFUSCATION", "red",
     "글자 방향을 뒤집는 보이지 않는 문자가 있습니다. 보이는 코드와 실제 코드가 다를 수 있습니다."),
    ("UC_INVISIBLE", "OBFUSCATION", "yellow",
     "폭이 0인 보이지 않는 문자가 있습니다."),
    ("EX_CRED_NETWORK", "EXFIL", "red",
     "같은 파일에서 비밀 정보에 접근하고 인터넷에도 접속합니다. 유출 가능성이 큽니다."),
    ("CT_CLAUDE_SETTINGS", "CONFIG_TRAP", "red",
     "Claude Code 설정 파일(.claude/settings.json)이 들어 있습니다. 이 폴더에서 Claude를 열면 안의 훅이 실행될 수 있습니다."),
    ("CT_CLAUDE_MD", "CONFIG_TRAP", "yellow",
     "CLAUDE.md가 들어 있습니다. 이 폴더에서 Claude를 열면 자동으로 읽혀 지시처럼 쓰입니다."),
    ("CT_MCP_JSON", "CONFIG_TRAP", "yellow",
     ".mcp.json이 들어 있습니다. MCP 서버를 등록해 실행합니다."),
    ("CT_PLUGIN_HOOKS", "CONFIG_TRAP", "yellow",
     "플러그인 훅 설정(hooks/hooks.json)이 들어 있습니다. 설치하면 자동으로 실행되는 명령이 생깁니다."),
    ("ST_SYMLINK", "STRUCTURE", "yellow",
     "폴더 안의 다른 파일을 가리키는 바로 가기(심볼릭 링크)가 있습니다. 따라가지 않았습니다."),
    ("ST_SYMLINK_ESCAPE", "STRUCTURE", "red",
     "폴더 밖을 가리키는 바로 가기(심볼릭 링크)가 있습니다."),
    ("ST_SPECIAL_FILE", "STRUCTURE", "red",
     "일반 파일이 아닌 특수 파일(파이프, 장치 등)이 있습니다. 열지 않았습니다."),
    ("ST_EXECUTABLE_BINARY", "STRUCTURE", "red",
     "컴파일된 실행 파일이 있습니다. 내용은 점검하지 못했습니다."),
    ("ST_ARCHIVE", "STRUCTURE", "yellow",
     "압축 파일이 있습니다. 안쪽은 점검하지 못했습니다."),
    ("ST_BINARY", "STRUCTURE", "yellow",
     "사람이 읽을 수 없는 바이너리 파일이 있습니다. 내용은 점검하지 못했습니다."),
    ("ST_NOT_UTF8", "STRUCTURE", "yellow",
     "UTF-8이 아닌 인코딩의 파일이 있습니다. 일부 글자를 바꿔 읽고 점검했습니다."),
    ("ST_LONG_LINE", "OBFUSCATION", "yellow",
     "한 줄이 아주 긴 부분이 있습니다. 압축하거나 숨긴 코드일 수 있습니다."),
    ("ST_UNSAFE_NAME", "OBFUSCATION", "yellow",
     "파일 이름에 허용하지 않는 문자가 있어 이름을 가렸습니다."),
    ("ST_TOO_LARGE", "UNSCANNED", "unknown",
     "파일이 너무 커서 점검하지 못했습니다."),
    ("ST_LIMIT_REACHED", "UNSCANNED", "unknown",
     "점검 한도(파일 수나 전체 크기)를 넘어 일부 파일을 점검하지 못했습니다."),
    ("ST_UNREADABLE", "UNSCANNED", "unknown",
     "읽을 수 없는 파일이나 폴더가 있어 점검하지 못했습니다."),
]


class Rule(object):
    __slots__ = ("rule_id", "category", "severity", "message", "regex")

    def __init__(self, rule_id, category, severity, message, regex=None):
        self.rule_id = rule_id
        self.category = category
        self.severity = severity
        self.message = message
        self.regex = regex


def _build_rules():
    # type: () -> Tuple[List[Rule], Dict[str, Rule]]
    line_rules = []  # type: List[Rule]
    by_id = {}  # type: Dict[str, Rule]
    for spec in LINE_RULES_SPEC:
        flags = spec[5] if len(spec) > 5 else _I
        rule = Rule(spec[0], spec[1], spec[2], spec[3], re.compile(spec[4], flags | re.MULTILINE))
        line_rules.append(rule)
        by_id[rule.rule_id] = rule
    for spec in OTHER_RULES_SPEC:
        rule = Rule(spec[0], spec[1], spec[2], spec[3])
        by_id[rule.rule_id] = rule
    if len(by_id) != len(LINE_RULES_SPEC) + len(OTHER_RULES_SPEC):
        raise RuntimeError("duplicate rule id")
    return line_rules, by_id


LINE_RULES, RULES_BY_ID = _build_rules()

# ---------------------------------------------------------------------------
# 유니코드
# ---------------------------------------------------------------------------

TAG_FIRST, TAG_LAST = 0xE0000, 0xE007F
VS_SUPP_FIRST, VS_SUPP_LAST = 0xE0100, 0xE01EF
VS_SMUGGLING_MIN = 4  # 한 줄에 이만큼 이상이면 숨긴 데이터로 본다

BIDI_CONTROLS = frozenset(list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A)))

# 지우고 표시할 보이지 않는 문자
INVISIBLE_FLAGGED = frozenset([
    0x200B, 0x200C, 0x200E, 0x200F, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064,
    0xFEFF, 0x00AD, 0x180E, 0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0,
])
# 지우기만 하고 표시하지 않는 문자. 200D(ZWJ)는 가족 이모지 같은 정상 글에 흔하다.
INVISIBLE_SILENT = frozenset([0x200D])

BOM = chr(0xFEFF)  # 파일 맨 앞의 BOM은 정상이라 지우고 넘어간다

AI_WORDS_RE = re.compile(
    r"\b(claude|assistant|ai|llm|gpt|agent|instructions?|ignore|system|prompt|reviewer|scanner|auditor|verdict)\b"
    r"|지시|무시|점검|검사|보고|안전",
    _I,
)
HTML_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)


def inspect_unicode(raw_line):
    # type: (str) -> Tuple[str, List[str]]
    """보이지 않는 문자를 찾아 지우고, NFKC 정규화한 줄과 발견한 규칙 이름을 돌려준다.

    태그 문자(U+E0020~E007E)는 ASCII 글자를 숨기는 데 쓰인다. 숨긴 글자를 풀어서
    줄 끝에 붙여 두면 숨은 지시도 같은 규칙으로 잡을 수 있다.
    """
    hits = []  # type: List[str]
    kept = []  # type: List[str]
    hidden_ascii = []  # type: List[str]
    vs_count = 0
    for ch in raw_line:
        cp = ord(ch)
        if TAG_FIRST <= cp <= TAG_LAST:
            if "UC_TAG_CHARS" not in hits:
                hits.append("UC_TAG_CHARS")
            inner = cp - TAG_FIRST
            if 0x20 <= inner <= 0x7E:
                hidden_ascii.append(chr(inner))
            continue
        if VS_SUPP_FIRST <= cp <= VS_SUPP_LAST:
            vs_count += 1
            continue
        if cp in BIDI_CONTROLS:
            if "UC_BIDI" not in hits:
                hits.append("UC_BIDI")
            continue
        if cp in INVISIBLE_FLAGGED:
            if "UC_INVISIBLE" not in hits:
                hits.append("UC_INVISIBLE")
            continue
        if cp in INVISIBLE_SILENT:
            continue
        kept.append(ch)
    if vs_count >= VS_SMUGGLING_MIN:
        hits.append("UC_VARIATION_SMUGGLING")
    text = unicodedata.normalize("NFKC", "".join(kept))
    if hidden_ascii:
        text = text + " " + "".join(hidden_ascii)
    return text, hits


# ---------------------------------------------------------------------------
# 파일 종류 판단
# ---------------------------------------------------------------------------

EXECUTABLE_MAGIC = (
    b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",  # Mach-O
    b"\xca\xfe\xba\xbe",  # Mach-O 범용(universal)
    b"\x7fELF",  # 리눅스 실행 파일
    b"MZ",  # 윈도우 실행 파일
)
ARCHIVE_MAGIC = (
    b"PK\x03\x04", b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"7z\xbc\xaf\x27\x1c", b"Rar!",
)
IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"\x00\x00\x01\x00",
)


def classify_bytes(data):
    # type: (bytes) -> str
    """'text', 'image', 'executable', 'archive', 'binary' 중 하나."""
    head = data[:16]
    if head.startswith(EXECUTABLE_MAGIC):
        # "MZ"는 짧아서 글 파일 첫 두 글자와 겹칠 수 있으니 NUL 바이트가 있을 때만 실행 파일로 본다.
        if not head.startswith(b"MZ") or b"\x00" in data[:4096]:
            return "executable"
    if head.startswith(ARCHIVE_MAGIC) or data[257:262] == b"ustar":
        return "archive"
    if head.startswith(IMAGE_MAGIC) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    if b"\x00" in data[:8192]:
        return "binary"
    return "text"


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------

SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._\-]+(/[A-Za-z0-9._\-]+)*$")


class _Scan(object):
    """스캔 한 번의 상태를 담는다."""

    def __init__(self, root):
        # type: (str) -> None
        self.root = root
        self.root_real = os.path.realpath(root)
        self.findings = []  # type: List[Dict[str, object]]
        self.hit_counts = {}  # type: Dict[Tuple[str, str], int]
        self.worst = "green"
        self.truncated = False
        self.unsafe_names = {}  # type: Dict[str, int]
        self.files_seen = 0
        self.files_scanned = 0
        self.bytes_read = 0
        self.limit_reported = False

    # --- 기록 ---

    def add(self, rule_id, display, line):
        # type: (str, str, int) -> None
        rule = RULES_BY_ID[rule_id]
        if SEVERITY_RANK[rule.severity] > SEVERITY_RANK[self.worst]:
            self.worst = rule.severity
        key = (rule_id, display)
        count = self.hit_counts.get(key, 0)
        self.hit_counts[key] = count + 1
        if count >= MAX_HITS_PER_RULE_FILE:
            return
        if len(self.findings) >= MAX_FINDINGS:
            self.truncated = True
            return
        self.findings.append({
            "rule": rule.rule_id,
            "category": rule.category,
            "severity": rule.severity,
            "file": display,
            "line": line,
            "message": rule.message,
        })

    def display_path(self, rel):
        # type: (str) -> str
        """결과에 내보낼 경로. 안전한 문자만 쓴 경로가 아니면 가린다."""
        if is_safe_path(rel):
            return rel
        if rel not in self.unsafe_names:
            self.unsafe_names[rel] = len(self.unsafe_names) + 1
            masked = "<unsafe-name-%d>" % self.unsafe_names[rel]
            self.add("ST_UNSAFE_NAME", masked, 0)
        return "<unsafe-name-%d>" % self.unsafe_names[rel]

    # --- 폴더 돌기 ---

    def run(self):
        # type: () -> None
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            abs_dir = os.path.join(self.root, rel_dir) if rel_dir else self.root
            try:
                names = sorted(os.listdir(abs_dir))
            except OSError:
                self.add("ST_UNREADABLE", self.display_path(rel_dir) if rel_dir else ".", 0)
                continue
            subdirs = []  # type: List[str]
            for name in names:
                rel = name if not rel_dir else rel_dir + "/" + name
                full = os.path.join(abs_dir, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    self.add("ST_UNREADABLE", self.display_path(rel), 0)
                    continue
                mode = st.st_mode
                if stat.S_ISDIR(mode):
                    subdirs.append(rel)
                    continue
                self.files_seen += 1
                if self.files_seen > MAX_FILES:
                    self._limit_reached()
                    continue
                if stat.S_ISLNK(mode):
                    self._check_symlink(rel, full)
                elif stat.S_ISREG(mode):
                    self._check_path_rules(rel)
                    self._scan_file(rel, full, st.st_size)
                else:
                    self.add("ST_SPECIAL_FILE", self.display_path(rel), 0)
            # 이름 순서대로 처리되도록 거꾸로 넣는다.
            for sub in reversed(subdirs):
                stack.append(sub)

    def _limit_reached(self):
        # type: () -> None
        if not self.limit_reported:
            self.limit_reported = True
            self.add("ST_LIMIT_REACHED", ".", 0)

    def _check_symlink(self, rel, full):
        # type: (str, str) -> None
        display = self.display_path(rel)
        try:
            target = os.readlink(full)
            resolved = os.path.realpath(full)
        except OSError:
            self.add("ST_SYMLINK_ESCAPE", display, 0)
            return
        inside = resolved == self.root_real or resolved.startswith(self.root_real + os.sep)
        if os.path.isabs(target) or not inside:
            self.add("ST_SYMLINK_ESCAPE", display, 0)
        else:
            self.add("ST_SYMLINK", display, 0)

    def _check_path_rules(self, rel):
        # type: (str) -> None
        parts = rel.split("/")
        base = parts[-1]
        display = self.display_path(rel)
        if ".claude" in parts[:-1] and base in ("settings.json", "settings.local.json"):
            self.add("CT_CLAUDE_SETTINGS", display, 0)
        if base in ("CLAUDE.md", "CLAUDE.local.md"):
            self.add("CT_CLAUDE_MD", display, 0)
        if base == ".mcp.json":
            self.add("CT_MCP_JSON", display, 0)
        if len(parts) >= 2 and parts[-2] == "hooks" and base == "hooks.json":
            self.add("CT_PLUGIN_HOOKS", display, 0)

    # --- 파일 하나 ---

    def _scan_file(self, rel, full, size):
        # type: (str, str, int) -> None
        display = self.display_path(rel)
        if size > MAX_FILE_BYTES:
            self.add("ST_TOO_LARGE", display, 0)
            return
        if self.bytes_read + size > MAX_TOTAL_BYTES:
            self._limit_reached()
            return
        data = _read_regular_file(full, MAX_FILE_BYTES)
        if data is None:
            self.add("ST_UNREADABLE", display, 0)
            return
        if len(data) > MAX_FILE_BYTES:
            self.add("ST_TOO_LARGE", display, 0)
            return
        self.bytes_read += len(data)
        self.files_scanned += 1

        kind = classify_bytes(data)
        if kind == "image":
            return
        if kind == "executable":
            self.add("ST_EXECUTABLE_BINARY", display, 0)
            return
        if kind == "archive":
            self.add("ST_ARCHIVE", display, 0)
            return
        if kind == "binary":
            self.add("ST_BINARY", display, 0)
            return

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
            self.add("ST_NOT_UTF8", display, 0)
        if text.startswith(BOM):
            text = text[1:]
        self._scan_text(display, text)

    def _scan_text(self, display, text):
        # type: (str, str) -> None
        # 줄 번호는 "\n" 으로 나눈 순서다. reader.py의 줄 수 검사도 같은 기준을 쓴다.
        raw_lines = text.split("\n")
        norm_lines = []  # type: List[str]
        file_rule_ids = set()
        long_line_reported = False
        for index, raw in enumerate(raw_lines):
            line_no = index + 1
            norm, unicode_hits = inspect_unicode(raw)
            norm_lines.append(norm)
            for rule_id in unicode_hits:
                self.add(rule_id, display, line_no)
                file_rule_ids.add(rule_id)
            if len(raw) > LONG_LINE_CHARS and not long_line_reported:
                long_line_reported = True
                self.add("ST_LONG_LINE", display, line_no)
            for rule in LINE_RULES:
                if rule.regex.search(norm):
                    self.add(rule.rule_id, display, line_no)
                    file_rule_ids.add(rule.rule_id)

        # 주석 안에 AI에게 하는 말이 있는지 (여러 줄 주석도 잡는다)
        joined = "\n".join(norm_lines)
        for match in HTML_COMMENT_RE.finditer(joined):
            if AI_WORDS_RE.search(match.group(1)):
                line_no = joined.count("\n", 0, match.start()) + 1
                self.add("HI_HTML_COMMENT", display, line_no)

        # 같은 파일에서 비밀 정보 접근 + 인터넷 접속 = 유출 가능성
        cred_lines = []  # type: List[int]
        has_network = False
        for finding in self.findings:
            if finding["file"] != display:
                continue
            rule = RULES_BY_ID[str(finding["rule"])]
            if rule.category == "CREDENTIAL" and rule.severity == "red":
                cred_lines.append(int(finding["line"]))  # type: ignore
            if rule.category in ("NETWORK", "EXFIL"):
                has_network = True
        if cred_lines and has_network:
            self.add("EX_CRED_NETWORK", display, min(cred_lines))


def _read_regular_file(path, limit):
    # type: (str, int) -> Optional[bytes]
    """일반 파일만 읽는다. 심볼릭 링크는 따라가지 않고, 파이프 같은 것은 열지 않는다.

    limit 바이트보다 1바이트 더 읽어서, 읽는 사이에 파일이 커졌는지도 알 수 있게 한다.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        chunks = []  # type: List[bytes]
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# 바깥에서 쓰는 함수
# ---------------------------------------------------------------------------

class ScanError(Exception):
    pass


def is_safe_path(rel):
    # type: (str) -> bool
    """결과에 그대로 내보내도 되는 경로인지. reader.py도 같은 기준을 쓴다."""
    return len(rel) <= MAX_PATH_CHARS and bool(SAFE_PATH_RE.match(rel))


def count_lines(text):
    # type: (str) -> int
    """줄 수. 스캔의 줄 번호와 같은 기준("\\n" 으로 나눈 개수)이다."""
    return len(text.split("\n"))


def index_files(root):
    # type: (str) -> Optional[Dict[str, Dict[str, object]]]
    """폴더 안 일반 파일의 {상대 경로: {"lines": 줄 수, "text": 글 파일인지}}.

    읽기실 결과를 검사할 때 쓴다. "text" 가 참인 파일은 읽기실이 끝까지 읽어야 한다.

    심볼릭 링크와 특수 파일은 넣지 않는다. 한도를 넘거나 읽지 못한 파일이 있으면
    None을 돌려준다(어떤 파일이 있는지 확실히 모르면 검사도 할 수 없으니까).
    """
    try:
        st = os.lstat(root)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return None
    result = {}  # type: Dict[str, Dict[str, object]]
    total = 0
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        abs_dir = os.path.join(root, rel_dir) if rel_dir else root
        try:
            names = sorted(os.listdir(abs_dir))
        except OSError:
            return None
        for name in names:
            rel = name if not rel_dir else rel_dir + "/" + name
            full = os.path.join(abs_dir, name)
            try:
                st = os.lstat(full)
            except OSError:
                return None
            if stat.S_ISDIR(st.st_mode):
                stack.append(rel)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if len(result) >= MAX_FILES or st.st_size > MAX_FILE_BYTES:
                return None
            data = _read_regular_file(full, MAX_FILE_BYTES)
            if data is None or len(data) > MAX_FILE_BYTES:
                return None
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                return None
            text = data.decode("utf-8", errors="replace")
            if text.startswith(BOM):
                text = text[1:]
            result[rel] = {"lines": count_lines(text), "text": classify_bytes(data) == "text"}
    return result


def scan_folder(root):
    # type: (str) -> Dict[str, object]
    """폴더를 스캔하고 결과 사전을 돌려준다. 파일 내용은 결과에 들어가지 않는다."""
    try:
        st = os.lstat(root)
    except OSError:
        raise ScanError("folder not found")
    if stat.S_ISLNK(st.st_mode):
        raise ScanError("folder is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise ScanError("not a folder")

    scan = _Scan(root)
    scan.run()
    findings = sorted(
        scan.findings,
        key=lambda f: (-SEVERITY_RANK[str(f["severity"])], str(f["file"]), int(f["line"]), str(f["rule"])),  # type: ignore
    )
    return {
        "tool": "skill-bound-scan",
        "format_version": 1,
        "verdict": scan.worst,
        "stats": {
            "files_seen": scan.files_seen,
            "files_scanned": scan.files_scanned,
            "bytes_read": scan.bytes_read,
            "findings": len(findings),
            "truncated": scan.truncated,
        },
        "findings": findings,
    }


def format_text(result):
    # type: (Dict[str, object]) -> str
    """사람이 읽는 한국어 보고서. 고정 문장과 경로, 줄 번호만 쓴다."""
    stats = result["stats"]  # type: ignore
    lines = [
        "skill-bound 정적 스캔 결과: %s" % SEVERITY_LABEL_KO[str(result["verdict"])],
        "파일 %d개 중 %d개 점검, 발견 %d건" % (stats["files_seen"], stats["files_scanned"], stats["findings"]),
    ]
    if stats["truncated"]:
        lines.append("발견이 너무 많아 일부만 보여 줍니다.")
    if result["verdict"] == "green":
        lines.append("정적 스캔에서 걸린 것이 없습니다. 초록불은 안전 보증이 아니라 1차 검문입니다.")
    for finding in result["findings"]:  # type: ignore
        where = finding["file"] if not finding["line"] else "%s:%d" % (finding["file"], finding["line"])
        lines.append("")
        lines.append("[%s] %s  %s" % (SEVERITY_TAG_KO[finding["severity"]], finding["category"], where))
        lines.append("    %s" % finding["message"])
    return "\n".join(lines)


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    parser = argparse.ArgumentParser(description="skill-bound static scan (reads only, runs nothing)")
    parser.add_argument("folder")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)
    try:
        result = scan_folder(args.folder)
    except ScanError as exc:
        # 오류 문장도 고정된 것만 쓴다. 경로를 그대로 되돌려 주지 않는다.
        print(json.dumps({"tool": "skill-bound-scan", "error": str(exc)}))
        return 2
    if args.format == "text":
        print(format_text(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
