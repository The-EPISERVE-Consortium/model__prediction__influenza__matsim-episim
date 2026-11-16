"""Fetches the school holidays (OpenHolidays API) and the weather (Open-Meteo Historical Weather API) of the scenario at run
time, for the files the EPISERVE platform has no dataset for yet. Only with the config parameter fetch_external; without it the
files baked into the image are used.

A live request has no dataset version on the platform, so every raw response is written to /work/output/external/ and its URL,
time of retrieval and sha256 go into run_info.json: the run can be repeated on the same data. A failed request fails the run;
it does not fall back silently to the baked file.

  OpenHolidays  /SchoolHolidays, subdivision DE-NW or DE-BE  -> <region>-school-holidays-2022-26.tsv
  Open-Meteo    /v1/archive, daily tmax and precipitation    -> <city>-weather-2022-26.csv
"""
from __future__ import annotations

import datetime
import hashlib
import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from prepare_inputs import City, InputError

OPENHOLIDAYS = "https://openholidaysapi.org/SchoolHolidays"
OPEN_METEO = "https://archive-api.open-meteo.com/v1/archive"
TIMEOUT = 60

# school years 2022/23 to 2025/26 up to the end of May 2026, as the baked files: the summer of 2026 lies after the horizon
HOLIDAYS_FROM, HOLIDAYS_TO = datetime.date(2022, 8, 1), datetime.date(2026, 5, 31)
MAX_RANGE_DAYS = 1095  # limit of the OpenHolidays API per request
# the weather file covers the horizon (2022-09-26 to 2026-05-17) with a margin
WEATHER_FROM, WEATHER_TO = "2022-09-01", "2026-05-31"

HOLIDAY_NAMES = {"Herbstferien": "autumn", "Weihnachtsferien": "christmas", "Winterferien": "winter",
                 "Osterferien": "easter", "Sommerferien": "summer", "Unterrichtsfreier Tag": "bridge_day"}


@dataclass(frozen=True)
class Place:
    subdivision: str      # ISO 3166-2 code of the federal state
    region: str
    holidays_file: str
    single_whit_day: str  # name of a one-day Pfingstferien in the baked file of the state
    latitude: str
    longitude: str
    weather_file: str


PLACES = {
    "cologne": Place("DE-NW", "Nordrhein-Westfalen", "nrw-school-holidays-2022-26.tsv", "whit_day", "50.94", "6.96",
                     "cologne-weather-2022-26.csv"),
    "berlin": Place("DE-BE", "Berlin", "berlin-school-holidays-2022-26.tsv", "bridge_day", "52.47", "13.40",
                    "berlin-weather-2022-26.csv"),
}

Fetcher = Callable[[str], bytes]


def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "model__prediction__influenza__matsim-episim"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except OSError as e:
        raise InputError(f"fetch_external: request to {url} failed: {e}") from e


class Recorder:
    """Fetches URLs, keeps every raw response in raw_dir and the provenance of each."""

    def __init__(self, raw_dir: Path, fetcher: Fetcher):
        self.raw_dir, self.fetcher, self.records = raw_dir, fetcher, []

    def get_json(self, url: str, name: str):
        body = self.fetcher(url)
        retrieved = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        (self.raw_dir / name).write_bytes(body)
        sha = hashlib.sha256(body).hexdigest()
        self.records.append({"url": url, "retrieved": retrieved, "sha256": sha, "file": f"external/{name}"})
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise InputError(f"fetch_external: {url} did not return JSON: {e}") from e

    def origin(self, names: list[str]) -> str:
        return "; ".join(f"{r['url']} (retrieved {r['retrieved']}, sha256 {r['sha256']}, raw response {r['file']})"
                         for r in self.records if r["file"].split("/")[-1] in names)


def date_ranges(first: datetime.date, last: datetime.date, days: int = MAX_RANGE_DAYS) -> list[tuple[datetime.date, datetime.date]]:
    ranges = []
    while first <= last:
        end = min(first + datetime.timedelta(days=days - 1), last)
        ranges.append((first, end))
        first = end + datetime.timedelta(days=1)
    return ranges


def school_year(day: datetime.date) -> str:
    start = day.year if day.month >= 8 else day.year - 1
    return f"{start}/{str(start + 1)[2:]}"


def holiday_rows(records: list[dict], place: Place) -> list[tuple[str, str, datetime.date, datetime.date]]:
    rows = set()
    for r in records:
        try:
            first, last = datetime.date.fromisoformat(r["startDate"]), datetime.date.fromisoformat(r["endDate"])
            german = next(n["text"] for n in r["name"] if n.get("language") == "DE")
        except (KeyError, StopIteration, TypeError, ValueError) as e:
            raise InputError(f"fetch_external: OpenHolidays record not understood: {r!r}") from e
        if first < HOLIDAYS_FROM or first > HOLIDAYS_TO:
            continue  # summer 2022 belongs to 2021/22, summer 2026 lies after the horizon
        if german == "Pfingstferien":
            name = place.single_whit_day if first == last else "whit"
        elif german in HOLIDAY_NAMES:
            name = HOLIDAY_NAMES[german]
        else:
            raise InputError(f"fetch_external: unknown OpenHolidays holiday {german!r} ({first} to {last}), "
                             f"known: {sorted(HOLIDAY_NAMES) + ['Pfingstferien']}")
        rows.add((school_year(first), name, first, last))  # a holiday on the border of two requests comes twice
    if not rows:
        raise InputError(f"fetch_external: OpenHolidays returned no school holidays of {place.subdivision}")
    return sorted(rows, key=lambda row: (row[2], row[3]))


def fetch_holidays(place: Place, recorder: Recorder, target: Path) -> list[str]:
    records, names = [], []
    for first, last in date_ranges(HOLIDAYS_FROM, HOLIDAYS_TO):
        query = urllib.parse.urlencode({"countryIsoCode": "DE", "subdivisionCode": place.subdivision, "languageIsoCode": "DE",
                                        "validFrom": first.isoformat(), "validTo": last.isoformat()})
        name = f"openholidays-{place.subdivision}-{first}-{last}.json"
        data = recorder.get_json(f"{OPENHOLIDAYS}?{query}", name)
        if not isinstance(data, list):
            raise InputError(f"fetch_external: OpenHolidays answered {str(data)[:200]}")
        records += data
        names.append(name)
    rows = holiday_rows(records, place)
    with open(target, "w", encoding="utf-8") as out:
        out.write(f"# School holidays of {place.region}, first and last day of each holiday (both days are holidays).\n"
                  f"# Source: OpenHolidays API (openholidaysapi.org), /SchoolHolidays, subdivision {place.subdivision}, "
                  f"{HOLIDAYS_FROM} to {HOLIDAYS_TO}; raw responses in external/ of the run output.\n"
                  f"# A one-day Pfingstferien is named {place.single_whit_day}, an 'Unterrichtsfreier Tag' bridge_day. "
                  "The 'bewegliche Ferientage' of single schools are not in the file.\n"
                  "# Built by src/external.py (model__prediction__influenza__matsim-episim) with fetch_external.\n"
                  "school_year\tholiday\tfirst_day\tlast_day\n")
        for year, name, first, last in rows:
            out.write(f"{year}\t{name}\t{first}\t{last}\n")
    return names


def fetch_weather(place: Place, recorder: Recorder, target: Path) -> list[str]:
    query = urllib.parse.urlencode({"latitude": place.latitude, "longitude": place.longitude, "start_date": WEATHER_FROM,
                                    "end_date": WEATHER_TO, "daily": "temperature_2m_max,precipitation_sum",
                                    "timezone": "Europe/Berlin"})
    url = f"{OPEN_METEO}?{query}"
    name = f"open-meteo-{place.weather_file.removesuffix('.csv')}.json"
    data = recorder.get_json(url, name)
    try:
        daily = data["daily"]
        days, tmax, prcp = daily["time"], daily["temperature_2m_max"], daily["precipitation_sum"]
    except (KeyError, TypeError) as e:
        raise InputError(f"fetch_external: Open-Meteo answered {str(data)[:200]}") from e
    if not days or days[0] != WEATHER_FROM or days[-1] != WEATHER_TO or not len(days) == len(tmax) == len(prcp):
        raise InputError(f"fetch_external: Open-Meteo returned {len(days)} days from {days[:1]} to {days[-1:]}, "
                         f"expected {WEATHER_FROM} to {WEATHER_TO}")
    gaps = [d for d, t, p in zip(days, tmax, prcp, strict=True) if t is None or p is None]
    if gaps:
        raise InputError(f"fetch_external: Open-Meteo has no value for {len(gaps)} day(s), first {gaps[0]}")
    with open(target, "w", encoding="utf-8") as out:
        out.write(f"# Daily weather, {place.region} grid cell {data.get('latitude')} N {data.get('longitude')} E (Open-Meteo "
                  f"Historical Weather API, ERA5 / ERA5-Land reanalysis; the request gave {place.latitude} N {place.longitude} E,\n"
                  f"#   elevation {data.get('elevation')} m), {url}, CC BY 4.0 (Open-Meteo, Copernicus ERA5); raw response in "
                  "external/ of the run output.\n"
                  "# tmax = daily maximum temperature 2 m in deg C, prcp = daily precipitation sum in mm.\n"
                  "# Built by src/external.py (model__prediction__influenza__matsim-episim) with fetch_external.\n"
                  "date,tmax,prcp\n")
        for d, t, p in zip(days, tmax, prcp, strict=True):
            out.write(f"{d},{t},{p}\n")
    return [name]


def fetch(scenario: Path, city: City, raw_dir: Path, fetcher: Fetcher | None = None) -> dict[str, str]:
    """Rewrites the holiday and the weather file of the scenario folder; returns file -> origin for the run report."""
    place = PLACES[city.key]
    recorder = Recorder(raw_dir, fetcher or http_get)
    holidays = fetch_holidays(place, recorder, scenario / place.holidays_file)
    weather = fetch_weather(place, recorder, scenario / place.weather_file)
    return {place.holidays_file: recorder.origin(holidays), place.weather_file: recorder.origin(weather)}
