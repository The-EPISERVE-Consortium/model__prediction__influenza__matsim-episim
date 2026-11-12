"""Rebuilds the data files of a multi-season influenza scenario from the EPISERVE datasets in /work/input.

Each input is optional; a missing one leaves the file that is baked into the image. The logic is the one of
Scenarios/Cologne-multiseason/build-reference-data.py and build-vaccination-data.py of matsim-episim, reading the parquet files of the
EPISERVE platform instead of the raw files:

  IfSG_Influenzafaelle.parquet        Q8399965930733  laboratory-confirmed notifications -> <region>-ifsg-influenza-2022-26.tsv
  VIW_FNT.parquet                     Q4242040001970  WHO FluNet sentinel, Germany       -> influenza-sentinel-weekly.csv
  KVIS_Impfquoten_Erwachsene.parquet  Q0602352424740  RKI KV-Impfsurveillance            -> vaccination-coverage-2022-26.tsv
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

IFSG_FILE = "IfSG_Influenzafaelle.parquet"
FLUNET_FILE = "VIW_FNT.parquet"
KVIS_FILE = "KVIS_Impfquoten_Erwachsene.parquet"

# the horizon of the scenarios plus one week on each side for the smoothing of the import
FIRST_WEEK, LAST_WEEK = "2022-W38", "2026-W21"
SENTINEL_START = "2022-09-26"

SEASONS = ["2022/23", "2023/24", "2024/25", "2025/26"]
BANDS = ["18-29", "30-39", "40-49", "50-59", "60-69", "70-79", "80+"]
BAND_AGES = {"18-29": (18, 29), "30-39": (30, 39), "40-49": (40, 49), "50-59": (50, 59),
             "60-69": (60, 69), "70-79": (70, 79), "80+": (80, 200)}
OLD = ["60-69", "70-79", "80+"]
STANDARD = "Personen ab 60 Jahren"
RISK = "Personen mit Grunderkrankung"


@dataclass(frozen=True)
class City:
    key: str            # value of the config parameter city
    folder: str         # scenario folder below Scenarios/
    district: str       # district attribute of the population, column district of infections.txt
    ifsg_file: str
    ifsg_region_id: int
    ifsg_region: str
    kvis_key: int       # Regionalschluessel_Kreis
    kvis_name: str


CITIES = {
    "cologne": City("cologne", "Cologne-multiseason", "Köln", "nrw-ifsg-influenza-2022-26.tsv", 5, "Nordrhein-Westfalen",
                    5315, "SK Köln"),
    "berlin": City("berlin", "Berlin-multiseason", "Berlin", "berlin-ifsg-influenza-2022-26.tsv", 11, "Berlin",
                   11000, "SK Berlin"),
}


class InputError(Exception):
    """An input file that cannot be used; the message is shown in the run log."""


def iso_weeks(first: str, last: str) -> list[str]:
    def monday(week: str) -> datetime.date:
        year, number = week.split("-W")
        return datetime.date.fromisocalendar(int(year), int(number), 1)

    weeks, day = [], monday(first)
    while day <= monday(last):
        iso = day.isocalendar()
        weeks.append(f"{iso.year}-W{iso.week:02d}")
        day += datetime.timedelta(days=7)
    return weeks


def write_ifsg(df: pd.DataFrame, city: City, target: Path, source: str) -> None:
    for column in ("Meldewoche", "Region_Id", "Altersgruppe", "Fallzahl"):
        if column not in df.columns:
            raise InputError(f"{IFSG_FILE}: column {column} missing")
    series = df[(df.Region_Id == city.ifsg_region_id) & (df.Altersgruppe == "00+")].set_index("Meldewoche").Fallzahl
    weeks = iso_weeks(FIRST_WEEK, LAST_WEEK)
    missing = [w for w in weeks if w not in series.index]
    if missing:
        raise InputError(f"{IFSG_FILE}: Region_Id {city.ifsg_region_id} lacks the weeks {missing[0]} to {missing[-1]} "
                         f"({len(missing)} of {len(weeks)})")
    with open(target, "w", encoding="utf-8") as out:
        out.write(f"# Weekly laboratory-confirmed influenza notifications (IfSG), {city.ifsg_region} (Region_Id "
                  f"{city.ifsg_region_id:02d}), all ages (00+).\n"
                  f"# Source: {source}, file {IFSG_FILE}, from Robert Koch-Institut, \"Laborbestätigte Influenzafälle in "
                  f"Deutschland\", CC BY 4.0.\n"
                  "# week = ISO reporting week (Meldewoche); cases = Fallzahl.\n"
                  f"# Drives the shape of the influenza import; weeks {FIRST_WEEK} and {LAST_WEEK} only serve the smoothing.\n"
                  "# Built by src/prepare_inputs.py (model__prediction__influenza__matsim-episim) from the input of the EPISERVE run.\n"
                  "week\tcases\n")
        for w in weeks:
            out.write(f"{w}\t{int(series[w])}\n")


def write_sentinel(df: pd.DataFrame, target: Path, source: str) -> None:
    columns = ["ORIGIN_SOURCE", "ISO_WEEKSTARTDATE", "AH3", "AH1N12009", "ANOTSUBTYPED", "ANOTSUBTYPABLE", "INF_B",
               "SPEC_PROCESSED_NB"]
    for column in columns:
        if column not in df.columns:
            raise InputError(f"{FLUNET_FILE}: column {column} missing")
    f = df[(df.ORIGIN_SOURCE == "SENTINEL") & (df.ISO_WEEKSTARTDATE >= SENTINEL_START)].copy()
    if f.empty:
        raise InputError(f"{FLUNET_FILE}: no SENTINEL rows from {SENTINEL_START}")
    f = f.sort_values("ISO_WEEKSTARTDATE").reset_index(drop=True)
    f["monday"] = pd.to_datetime(f.ISO_WEEKSTARTDATE)
    for c in columns[2:]:
        f[c] = f[c].fillna(0)
    with open(target, "w", encoding="utf-8") as out:
        out.write("# Weekly influenza detections of the sentinel specimens of Germany (WHO FluNet, ORIGIN_SOURCE = SENTINEL).\n"
                  f"# Source: {source}, file {FLUNET_FILE}, from the WHO FluMart API, CC BY 4.0.\n"
                  "# samples = SPEC_PROCESSED_NB; A_unsubtyped = ANOTSUBTYPED + ANOTSUBTYPABLE; A_H3N2 = AH3; A_H1N1pdm09 = AH1N12009;\n"
                  "#   B_Victoria = INF_B; influenza_positives = their sum; influenza_positivity_pct derived here.\n"
                  "# Season changes at ISO week 40. Used for the timing and for the subtype split of the import only.\n"
                  "# Built by src/prepare_inputs.py (model__prediction__influenza__matsim-episim) from the input of the EPISERVE run.\n"
                  "season,iso_year,iso_week,monday,samples,A_unsubtyped,A_H3N2,A_H1N1pdm09,B_Victoria,influenza_positives,"
                  "influenza_positivity_pct\n")
        for _, r in f.iterrows():
            iso = r.monday.isocalendar()
            start = iso.year if iso.week >= 40 else iso.year - 1
            h3, h1, au = int(r.AH3), int(r.AH1N12009), int(r.ANOTSUBTYPED + r.ANOTSUBTYPABLE)
            b = int(r.INF_B)
            n = int(r.SPEC_PROCESSED_NB)
            pos = h3 + h1 + au + b
            row = (f"{start}/{str(start + 1)[2:]}", iso.year, iso.week, r.monday.date(), n, au, h3, h1, b, pos,
                   round(100 * pos / n, 1) if n else "")
            out.write(",".join(str(x) for x in row) + "\n")


def read_ages(path: Path) -> dict[int, int]:
    ages = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if cols[0] == "age":
                continue
            ages[int(cols[0])] = int(cols[1])
    return ages


def write_vaccination(df: pd.DataFrame, city: City, scenario: Path, source: str) -> None:
    for column in ("Impfung", "Regionalschluessel_Kreis", "Berichtszeitpunkt", "Zielgruppe", "Altersgruppe",
                   "Impfquote", "Bevoelkerung_Gewicht"):
        if column not in df.columns:
            raise InputError(f"{KVIS_FILE}: column {column} missing")
    d = df[(df.Impfung == "Influenza") & (pd.to_numeric(df.Regionalschluessel_Kreis, errors="coerce") == city.kvis_key)]
    d = d.dropna(subset=["Impfquote", "Bevoelkerung_Gewicht"])
    rows = {(r.Berichtszeitpunkt, r.Zielgruppe, r.Altersgruppe): (float(r.Impfquote), float(r.Bevoelkerung_Gewicht))
            for r in d.itertuples()}
    if not rows:
        raise InputError(f"{KVIS_FILE}: no influenza rows for district {city.kvis_key} ({city.kvis_name})")

    # a season without data takes the coverage of the last season with data (assumption A42)
    with_data = [s for s in SEASONS if any(k[0] == s for k in rows)]
    if not with_data:
        raise InputError(f"{KVIS_FILE}: no influenza coverage of {city.kvis_name} for the seasons {SEASONS}")

    def need(season, group, band):
        if (season, group, band) not in rows:
            raise InputError(f"{KVIS_FILE}: {city.kvis_name} has no value for season {season}, group '{group}', band {band}")
        return rows[(season, group, band)]

    ages = read_ages(scenario / "population-ages.tsv")
    agents = {b: sum(n for a, n in ages.items() if BAND_AGES[b][0] <= a <= BAND_AGES[b][1]) for b in BANDS}
    data = {}
    for season in with_data:
        size60 = sum(need(season, STANDARD, b)[1] for b in OLD)
        scale = size60 / sum(agents[b] for b in OLD)
        for b in BANDS:
            cov_risk, size_risk = need(season, RISK, b)
            prevalence = size_risk / (scale * agents[b])
            cov_band = need(season, STANDARD, b)[0] if b in OLD else cov_risk * min(prevalence, 1.0)
            for v in (cov_risk, cov_band):
                if not 0.0 <= v <= 100.0:
                    raise InputError(f"{KVIS_FILE}: coverage {v} outside 0 to 100 ({season}, {b})")
            data[(season, b)] = (cov_risk, prevalence, cov_band)

    with open(scenario / "vaccination-coverage-2022-26.tsv", "w", encoding="utf-8") as out:
        out.write(
            f"# Seasonal influenza vaccination coverage by age band, {city.district} ({city.kvis_name}, key {city.kvis_key}), "
            "seasons 2022/23 to 2025/26.\n"
            "# Source: RKI KV-Impfsurveillance, claims data of the statutory insurance physicians (statutory insured only), 1 dose;\n"
            f"#   {source}, file {KVIS_FILE}, rows Impfung = Influenza, Regionalschluessel_Kreis = {city.kvis_key}.\n"
            "# coverage_band: percent of ALL persons of the band who are vaccinated in the season; this is the value the model uses.\n"
            "#   Bands 60-69, 70-79, 80+: the coverage of the group 'Personen ab 60 Jahren'.\n"
            "#   Bands 18-29 to 50-59: coverage_risk times prevalence_risk.\n"
            "# source_season: the season the values are taken from; a season without data takes the last one with data (A42).\n"
            "# Built by src/prepare_inputs.py (model__prediction__influenza__matsim-episim) from the input of the EPISERVE run.\n"
            "season\tband\tcoverage_band\tcoverage_risk\tprevalence_risk\tsource_season\n")
        for season in SEASONS:
            source_season = season if season in with_data else with_data[-1]
            for b in BANDS:
                cov_risk, prevalence, cov_band = data[(source_season, b)]
                out.write(f"{season}\t{b}\t{cov_band:.2f}\t{cov_risk:.2f}\t{prevalence:.3f}\t{source_season}\n")


def prepare(input_dir: Path, scenario: Path, city: City) -> dict[str, str]:
    """Rewrites the files of the scenario folder for which an input exists; returns file -> origin for the run report."""
    origin = {}
    source = "EPISERVE input of this run"

    path = input_dir / IFSG_FILE
    if path.is_file():
        write_ifsg(pd.read_parquet(path), city, scenario / city.ifsg_file, source)
        origin[city.ifsg_file] = f"/work/input/{IFSG_FILE}"
    else:
        origin[city.ifsg_file] = "baked into the image"

    path = input_dir / FLUNET_FILE
    if path.is_file():
        write_sentinel(pd.read_parquet(path), scenario / "influenza-sentinel-weekly.csv", source)
        origin["influenza-sentinel-weekly.csv"] = f"/work/input/{FLUNET_FILE}"
    else:
        origin["influenza-sentinel-weekly.csv"] = "baked into the image"

    path = input_dir / KVIS_FILE
    if path.is_file():
        write_vaccination(pd.read_parquet(path), city, scenario, source)
        origin["vaccination-coverage-2022-26.tsv"] = f"/work/input/{KVIS_FILE}"
    else:
        origin["vaccination-coverage-2022-26.tsv"] = "baked into the image"
    return origin
