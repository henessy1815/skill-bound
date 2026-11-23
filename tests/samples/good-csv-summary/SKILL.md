---
name: csv-summary
description: CSV 파일의 행 수와 열 이름을 요약해 표로 보여 줍니다.
allowed-tools: Read, Bash(python3 -I scripts/summarize.py *)
---

# CSV 요약

사용자가 CSV 파일 요약을 요청하면 다음 순서로 한다.

1. 파일 경로를 확인한다.
2. `python3 -I scripts/summarize.py <파일>` 을 실행한다.
3. 결과를 표로 정리해 보여 준다. 👍
