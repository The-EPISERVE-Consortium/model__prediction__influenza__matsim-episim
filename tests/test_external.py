"""Holidays and weather from their public APIs (fetch_external), with a stand-in for the HTTP requests."""
from __future__ import annotations

import datetime
import json
import urllib.parse

import pytest

import external as ex
import prepare_inputs as pi

COLOGNE, BERLIN = pi.CITIES["cologne"], pi.CITIES["berlin"]


def holiday(first: str, last: str, name: str) -> dict:
    return {"startDate": first, "endDate": last, "type": "School", "name": [{"language": "DE", "text": name}]}


HOLIDAYS = [
    holiday("2022-06-27", "2022-08-09", "Sommerferien"),        # 2021/22, dropped
    holiday("2022-10-04", "2022-10-15", "Herbstferien"),
    holiday("2022-12-23", "2023-01-06", "Weihnachtsferien"),
    holiday("2023-05-19", "2023-05-19", "Unterrichtsfreier Tag"),
    holiday("2023-05-30", "2023-05-30", "Pfingstferien"),
    holiday("2024-07-08", "2024-08-20", "Sommerferien"),        # on the border of the two requests: comes twice
    holiday("2026-07-20", "2026-09-01", "Sommerferien"),        # after the horizon, dropped
]


def weather(days: int | None = None, gap: bool = False) -> dict:
    first = datetime.date.fromisoformat(ex.WEATHER_FROM)
    n = days if days is not None else (datetime.date.fromisoformat(ex.WEATHER_TO) - first).days + 1
    time = [(first + datetime.timedelta(days=i)).isoformat() for i in range(n)]
    tmax = [10.0 + i % 7 for i in range(n)]
    if gap:
        tmax[5] = None
    return {"latitude": 50.93, "longitude": 6.91, "elevation": 62.0,
            "daily": {"time": time, "temperature_2m_max": tmax, "precipitation_sum": [0.5] * n}}


def fake(holidays=HOLIDAYS, meteo=None):
    calls = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        if url.startswith(ex.OPENHOLIDAYS):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            first, last = q["validFrom"][0], q["validTo"][0]
            return json.dumps([h for h in holidays if first <= h["startDate"] <= last or first <= h["endDate"] <= last]).encode()
        return json.dumps(meteo if meteo is not None else weather()).encode()

    return fetcher, calls


def rows(path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]


def test_date_ranges_respect_the_api_limit():
    ranges = ex.date_ranges(ex.HOLIDAYS_FROM, ex.HOLIDAYS_TO)
    assert ranges[0][0] == ex.HOLIDAYS_FROM and ranges[-1][1] == ex.HOLIDAYS_TO
    assert all((b - a).days + 1 <= ex.MAX_RANGE_DAYS for a, b in ranges)
    assert all(b + datetime.timedelta(days=1) == c for (_, b), (c, _) in zip(ranges, ranges[1:], strict=False))


def test_fetch_cologne(tmp_path):
    fetcher, calls = fake()
    origin = ex.fetch(tmp_path, COLOGNE, tmp_path / "external", fetcher)

    assert rows(tmp_path / "nrw-school-holidays-2022-26.tsv") == [
        "school_year\tholiday\tfirst_day\tlast_day",
        "2022/23\tautumn\t2022-10-04\t2022-10-15",
        "2022/23\tchristmas\t2022-12-23\t2023-01-06",
        "2022/23\tbridge_day\t2023-05-19\t2023-05-19",
        "2022/23\twhit_day\t2023-05-30\t2023-05-30",
        "2023/24\tsummer\t2024-07-08\t2024-08-20",
    ]
    weather_rows = rows(tmp_path / "cologne-weather-2022-26.csv")
    assert weather_rows[0] == "date,tmax,prcp"
    assert weather_rows[1] == f"{ex.WEATHER_FROM},10.0,0.5"
    assert weather_rows[-1].startswith(ex.WEATHER_TO)

    assert len(calls) == 3  # two holiday requests, one weather request
    raw = sorted(p.name for p in (tmp_path / "external").iterdir())
    assert len(raw) == 3
    assert set(origin) == {"nrw-school-holidays-2022-26.tsv", "cologne-weather-2022-26.csv"}
    assert origin["nrw-school-holidays-2022-26.tsv"].count("sha256") == 2
    assert "subdivisionCode=DE-NW" in origin["nrw-school-holidays-2022-26.tsv"]
    assert "latitude=50.94" in origin["cologne-weather-2022-26.csv"]


def test_berlin_names_a_one_day_whitsun_a_bridge_day(tmp_path):
    fetcher, _ = fake()
    ex.fetch(tmp_path, BERLIN, tmp_path / "external", fetcher)
    assert "2022/23\tbridge_day\t2023-05-30\t2023-05-30" in rows(tmp_path / "berlin-school-holidays-2022-26.tsv")
    assert (tmp_path / "berlin-weather-2022-26.csv").is_file()


def test_unknown_holiday_fails(tmp_path):
    fetcher, _ = fake(holidays=[holiday("2023-02-20", "2023-02-21", "Karnevalsferien")])
    with pytest.raises(pi.InputError, match="unknown OpenHolidays holiday 'Karnevalsferien'"):
        ex.fetch(tmp_path, COLOGNE, tmp_path / "external", fetcher)


def test_no_holidays_fails(tmp_path):
    fetcher, _ = fake(holidays=[])
    with pytest.raises(pi.InputError, match="no school holidays of DE-NW"):
        ex.fetch(tmp_path, COLOGNE, tmp_path / "external", fetcher)


@pytest.mark.parametrize("meteo, message", [
    (weather(days=100), "returned 100 days"),
    (weather(gap=True), "no value for 1 day"),
    ({"error": True, "reason": "x"}, "Open-Meteo answered"),
])
def test_bad_weather_fails(tmp_path, meteo, message):
    fetcher, _ = fake(meteo=meteo)
    with pytest.raises(pi.InputError, match=message):
        ex.fetch(tmp_path, COLOGNE, tmp_path / "external", fetcher)


def test_failed_request_fails(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(ex.urllib.request, "urlopen", refuse)
    with pytest.raises(pi.InputError, match="request to https://openholidaysapi.org.* failed: no network"):
        ex.fetch(tmp_path, COLOGNE, tmp_path / "external")
