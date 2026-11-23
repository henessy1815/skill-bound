---
name: weather
description: Open-Meteo 공개 API로 도시의 현재 기온을 알려 줍니다. 인터넷 접속이 필요합니다.
allowed-tools: Bash(python3 -I scripts/weather.py *)
---

# 날씨

사용자가 날씨를 물으면 `python3 -I scripts/weather.py <위도> <경도>` 를 실행하고 기온을 알려 준다.
접속하는 곳은 api.open-meteo.com 한 곳뿐이다.
