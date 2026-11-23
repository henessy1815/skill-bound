"""Open-Meteo에서 현재 기온을 받아 출력한다."""
import json
import sys
import urllib.parse
import urllib.request


def current_temperature(lat, lon):
    query = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "current": "temperature_2m"})
    with urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + query, timeout=10) as resp:
        data = json.load(resp)
    return data["current"]["temperature_2m"]


if __name__ == "__main__":
    print(current_temperature(sys.argv[1], sys.argv[2]))
