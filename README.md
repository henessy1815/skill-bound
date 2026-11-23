# skill-bound

GitHub에서 받은 Claude 스킬을 설치하기 전에, 아무것도 실행하지 않고 격리 폴더에 받아 보안 점검하는 Claude Code 스킬입니다. 결과를 🔴 설치 비권장 / 🟡 조건부 / 🟢 통과 / ⚪ 판단 불가로 보여 주고, 사람이 고른 경우에만 점검한 바로 그 파일을 설치합니다.

설치 뒤에는 훅이 지킵니다. 평소에는 조용히 있다가 스킬 설치 시도 때만 개입합니다.

- **점검하는 동안 (점검 모드)**: 받은 파일이 격리 폴더에 있는 동안, 그 세션은 skill-bound 스크립트 실행과 격리 폴더 읽기만 허용하고 웹 접속, MCP, 그 밖의 명령은 막습니다. 파일은 읽기 도구만 가진 별도의 Claude(밀폐 읽기실)가 읽고, 본 세션에는 양식 검사를 통과한 결과만 전달됩니다. 받은 파일 속 숨은 지시에 Claude가 속더라도 할 수 있는 일이 없습니다. 설치, 취소, 또는 2시간이 지나면 꺼집니다.
- **평소 작업 중**: 설치 명령(`npx skills add`, `claude plugin install`, `claude mcp add` 등)과 스킬 폴더에 파일을 넣는 시도만 막고 점검으로 안내합니다. 나머지 작업은 그대로 통과합니다. 허락 창은 점검한 스킬을 설치할 때, skill-bound를 끄거나 지울 때, Claude가 설정 파일을 고치려 할 때만 뜹니다.
- **세션 시작 때**: 스킬, 플러그인, 훅, MCP 설정이 지난번과 달라졌으면 알립니다. 바뀐 게 없으면 아무 말도 하지 않습니다.

## 이렇게 써요

```
/skill-bound github.com/<아이디>/<저장소>
/skill-bound github.com/<아이디>/<저장소>/tree/<브랜치>/<폴더>
/skill-bound /Users/me/Downloads/some-skill
```

"이 스킬 설치해 줘" 처럼 말로 해도 점검을 먼저 거칩니다. "skill-bound 꺼 줘", "다시 켜 줘", "완전히 지워 줘" 도 됩니다.

## 설치

요구 사항: macOS, 터미널에서 쓰는 Claude Code, `/usr/bin/python3` 3.9 이상(표준 라이브러리만), `git`(GitHub에서 받아 점검할 때만). 외부 패키지는 쓰지 않습니다. 밀폐 읽기실이 Claude를 한 번 더 부르므로 점검 한 번에 보통 몇 센트가 들고, 최대 1달러로 제한되어 있습니다.

**1. 받아서 복사합니다.** 스킬 폴더에 바로 받지 말고, 다른 곳에 받아서 필요한 파일만 복사합니다. 심볼릭 링크로 연결하면 동작하지 않습니다.

```bash
git clone https://github.com/henessy1815/skill-bound.git ~/skill-bound-src
```

```bash
mkdir -p ~/.claude/skills/skill-bound
```

```bash
rsync -a --exclude .git --exclude CLAUDE.md ~/skill-bound-src/ ~/.claude/skills/skill-bound/
```

**2. 훅을 등록합니다.** `~/.claude/settings.json` 을 백업한 뒤, `"hooks"` 안에 아래 두 항목을 추가합니다. 이미 다른 훅이 있으면 지우지 말고 같은 목록에 한 항목을 더 넣으세요.

```bash
cp ~/.claude/settings.json ~/.claude/settings.json.before-skill-bound
```

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/usr/bin/python3 -I -B ~/.claude/skills/skill-bound/hooks/guard.py pre",
            "timeout": 5,
            "onFailure": "block"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/usr/bin/python3 -I -B ~/.claude/skills/skill-bound/hooks/guard.py session-start",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

`onFailure: "block"` 은 훅이 고장 나거나 5초 안에 끝나지 않으면 막는다는 뜻입니다. 고장 난 틈으로 위험한 명령이 지나가지 않게 하기 위해서입니다.

**3. Claude Code를 다시 시작합니다.** 훅 없이 점검 절차만 쓰고 싶으면 2번을 건너뛰어도 됩니다.

## 들어 있는 것

| 파일 | 하는 일 |
|---|---|
| `SKILL.md` | 점검 절차(받기, 정적 스캔, 밀폐 읽기실, 신호등 카드와 선택 버튼, 설치 또는 취소) |
| `hooks/guard.py` | 평소 감시, 점검 모드 차단, 세션 시작 때 변경 알림 |
| `scripts/vet.py` | 격리 폴더에 받기(git 훅, LFS, 심볼릭 링크를 끄고), 끝내기, 상태 보기 |
| `scripts/scan.py` | 정적 스캔: 원격 즉시 실행, 유출, 자동 실행 등록, 숨은 지시, 보이지 않는 유니코드 등 |
| `scripts/reader.py` | 밀폐 읽기실(`claude -p`, 읽기 도구만) 실행, 결과 양식 검사, 신호등 카드 |
| `scripts/reader_guard.py` | 밀폐 읽기실 전용 훅. 격리 폴더 밖 읽기와 다른 도구를 한 번 더 막음 |
| `scripts/install_checked.py` | 받을 때 기록한 해시와 같은 파일만 설치(덮어쓰기 안 함, 실행 권한 제거) |
| `scripts/switch.py` | 끄기, 켜기, 완전히 지우기 |

점검 기록(`~/.claude/skill-bound-state/`)은 각자 컴퓨터에서 처음 쓸 때 생기며 저장소에는 없습니다. `CLAUDE.md` 는 개발 메모라 설치하지 않습니다.

## 끄기와 지우기

Claude에게 "skill-bound 꺼 줘" 또는 "완전히 지워 줘" 라고 하면 허락 창이 한 번 뜹니다. 직접 부탁하지 않았는데 이 창이 뜨면 거절하세요. Claude가 읽은 글 속 숨은 지시에 속았다는 신호입니다. 켜기는 허락 창 없이 됩니다.

훅이 고장 나 Claude가 아무것도 못 할 때는 터미널에서 직접 끕니다.

```bash
mkdir -p ~/.claude/skill-bound-state && touch ~/.claude/skill-bound-state/OFF
```

## 알아 둘 한계

- 운영체제 샌드박스가 아닙니다. Claude의 도구 호출만 거릅니다.
- 평소 모드에서 `python3 -c "..."` 처럼 프로그램 안에서 쓰는 파일은 명령만 보고는 알 수 없어 실시간으로 막지 못하고, 다음 세션 시작 때 알립니다.
- Claude 밖 터미널에서 한 일도 다음 세션 시작 때 알립니다.
- 받을 수 있는 곳은 GitHub와 로컬 폴더, 설치할 수 있는 것은 스킬뿐입니다. 플러그인과 MCP는 설치 시도를 막기만 합니다.
- 점검 중에는 MCP 도구가 모두 막혀 MCP로 동작하는 다른 기능도 쓸 수 없습니다.
- 초록은 안전 보증이 아니라 설치 전 1차 검문입니다.

## 자기 점검 결과

skill-bound로 skill-bound를 점검하면 **빨강**입니다(2026-11-23, 설치되는 파일 9개). 정적 스캔은 규칙 안의 `curl ... | sh`, `LaunchAgents` 같은 찾으려는 말 때문에 80건을 표시합니다. 밀폐 읽기실은 노랑으로 4건을 표시했는데, 훅 등록 안내, 모든 도구 감시, `git clone` 으로 받기, 지우기 기능처럼 모두 skill-bound가 하는 일 그 자체입니다.

## 라이선스

MIT
