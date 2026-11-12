"""Rebuilding the scenario data files from the EPISERVE parquet inputs, on small synthetic tables."""
from __future__ import annotations

import pandas as pd
import pytest

import prepare_inputs as pi

COLOGNE = pi.CITIES["cologne"]


def ifsg(region_id: int = 5, weeks: list[str] | None = None) -> pd.DataFrame:
    weeks = weeks if weeks is not None else pi.iso_weeks(pi.FIRST_WEEK, pi.LAST_WEEK)
    rows = [{"Meldewoche": w, "Region_Id": region_id, "Altersgruppe": "00+", "Fallzahl": i} for i, w in enumerate(weeks)]
    # rows of other ages and regions must be ignored
    rows += [{"Meldewoche": weeks[0], "Region_Id": region_id, "Altersgruppe": "A00..04", "Fallzahl": 999},
             {"Meldewoche": weeks[0], "Region_Id": 1, "Altersgruppe": "00+", "Fallzahl": 999}]
    return pd.DataFrame(rows)


def data_rows(path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]


def test_iso_weeks_cross_a_53_week_year():
    weeks = pi.iso_weeks("2026-W52", "2027-W02")
    assert weeks == ["2026-W52", "2026-W53", "2027-W01", "2027-W02"]


def test_write_ifsg(tmp_path):
    target = tmp_path / COLOGNE.ifsg_file
    pi.write_ifsg(ifsg(), COLOGNE, target, "test")
    rows = data_rows(target)
    weeks = pi.iso_weeks(pi.FIRST_WEEK, pi.LAST_WEEK)
    assert rows[0] == "week\tcases"
    assert rows[1] == f"{pi.FIRST_WEEK}\t0"
    assert rows[-1] == f"{pi.LAST_WEEK}\t{len(weeks) - 1}"
    assert len(rows) == len(weeks) + 1


def test_write_ifsg_missing_weeks(tmp_path):
    weeks = pi.iso_weeks(pi.FIRST_WEEK, pi.LAST_WEEK)
    with pytest.raises(pi.InputError, match="lacks the weeks"):
        pi.write_ifsg(ifsg(weeks=weeks[:-3]), COLOGNE, tmp_path / "x.tsv", "test")
    with pytest.raises(pi.InputError, match="column Fallzahl missing"):
        pi.write_ifsg(ifsg().drop(columns="Fallzahl"), COLOGNE, tmp_path / "x.tsv", "test")


def test_write_sentinel(tmp_path):
    df = pd.DataFrame([
        {"ORIGIN_SOURCE": "SENTINEL", "ISO_WEEKSTARTDATE": "2022-10-03", "AH3": 3, "AH1N12009": 1, "ANOTSUBTYPED": None,
         "ANOTSUBTYPABLE": 1, "INF_B": 5, "SPEC_PROCESSED_NB": 20},
        {"ORIGIN_SOURCE": "SENTINEL", "ISO_WEEKSTARTDATE": "2022-09-26", "AH3": 0, "AH1N12009": 0, "ANOTSUBTYPED": 0,
         "ANOTSUBTYPABLE": 0, "INF_B": 0, "SPEC_PROCESSED_NB": 0},
        {"ORIGIN_SOURCE": "NONSENTINEL", "ISO_WEEKSTARTDATE": "2022-10-03", "AH3": 99, "AH1N12009": 0, "ANOTSUBTYPED": 0,
         "ANOTSUBTYPABLE": 0, "INF_B": 0, "SPEC_PROCESSED_NB": 99},
        {"ORIGIN_SOURCE": "SENTINEL", "ISO_WEEKSTARTDATE": "2022-09-19", "AH3": 99, "AH1N12009": 0, "ANOTSUBTYPED": 0,
         "ANOTSUBTYPABLE": 0, "INF_B": 0, "SPEC_PROCESSED_NB": 99},
    ])
    target = tmp_path / "influenza-sentinel-weekly.csv"
    pi.write_sentinel(df, target, "test")
    rows = data_rows(target)
    assert rows[0].startswith("season,iso_year,iso_week,monday,samples")
    # sorted by week; week 39 belongs to the season before, week 40 starts 2022/23; no samples -> empty positivity
    assert rows[1] == "2021/22,2022,39,2022-09-26,0,0,0,0,0,0,"
    assert rows[2] == "2022/23,2022,40,2022-10-03,20,1,3,1,5,10,50.0"
    assert len(rows) == 3


def test_write_sentinel_without_rows(tmp_path):
    df = pd.DataFrame(columns=["ORIGIN_SOURCE", "ISO_WEEKSTARTDATE", "AH3", "AH1N12009", "ANOTSUBTYPED",
                               "ANOTSUBTYPABLE", "INF_B", "SPEC_PROCESSED_NB"])
    with pytest.raises(pi.InputError, match="no SENTINEL rows"):
        pi.write_sentinel(df, tmp_path / "x.csv", "test")


def kvis(seasons: list[str]) -> pd.DataFrame:
    rows = []
    for s in seasons:
        for b in pi.BANDS:
            rows.append({"Impfung": "Influenza", "Regionalschluessel_Kreis": "05315", "Berichtszeitpunkt": s,
                         "Zielgruppe": pi.RISK, "Altersgruppe": b, "Impfquote": 20.0, "Bevoelkerung_Gewicht": 200.0})
            rows.append({"Impfung": "Influenza", "Regionalschluessel_Kreis": "05315", "Berichtszeitpunkt": s,
                         "Zielgruppe": pi.STANDARD, "Altersgruppe": b, "Impfquote": 40.0, "Bevoelkerung_Gewicht": 1000.0})
    rows.append({"Impfung": "COVID-19", "Regionalschluessel_Kreis": "05315", "Berichtszeitpunkt": seasons[0],
                 "Zielgruppe": pi.STANDARD, "Altersgruppe": "60-69", "Impfquote": 99.0, "Bevoelkerung_Gewicht": 1.0})
    return pd.DataFrame(rows)


def scenario_with_ages(tmp_path):
    # 100 agents at every age from 0 to 99: 1,000 per ten-year band, 1,200 aged 18-29
    tmp_path.mkdir(parents=True, exist_ok=True)
    lines = ["# test", "age\tagents"] + [f"{a}\t100" for a in range(100)]
    (tmp_path / "population-ages.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_write_vaccination(tmp_path):
    scenario = scenario_with_ages(tmp_path)
    pi.write_vaccination(kvis(["2022/23", "2023/24"]), COLOGNE, scenario, "test")
    rows = [r.split("\t") for r in data_rows(scenario / "vaccination-coverage-2022-26.tsv")]
    assert rows[0] == ["season", "band", "coverage_band", "coverage_risk", "prevalence_risk", "source_season"]
    body = {(r[0], r[1]): r[2:] for r in rows[1:]}
    assert len(body) == len(pi.SEASONS) * len(pi.BANDS)
    # 60+: weight 3 x 1000 for agents 1000 + 1000 + 2000 (80-99) -> scale 0.75; 60-69 takes the 60+ coverage
    assert body[("2022/23", "60-69")] == ["40.00", "20.00", "0.267", "2022/23"]
    # 18-29: prevalence 200 / (0.75 x 1200); coverage_band = 20 x prevalence
    assert body[("2022/23", "18-29")] == ["4.44", "20.00", "0.222", "2022/23"]
    # seasons without data take the last season with data (A42)
    assert body[("2025/26", "60-69")][-1] == "2023/24"


def test_write_vaccination_errors(tmp_path):
    scenario = scenario_with_ages(tmp_path)
    berlin = pi.CITIES["berlin"]
    with pytest.raises(pi.InputError, match="no influenza rows for district 11000"):
        pi.write_vaccination(kvis(["2022/23"]), berlin, scenario, "test")
    incomplete = kvis(["2022/23"])
    incomplete = incomplete[~((incomplete.Zielgruppe == pi.RISK) & (incomplete.Altersgruppe == "30-39"))]
    with pytest.raises(pi.InputError, match="no value for season 2022/23, group 'Personen mit Grunderkrankung', band 30-39"):
        pi.write_vaccination(incomplete, COLOGNE, scenario, "test")


def test_prepare_uses_inputs_when_present(tmp_path):
    inputs, scenario = tmp_path / "input", scenario_with_ages(tmp_path / "scenario")
    inputs.mkdir()
    origin = pi.prepare(inputs, scenario, COLOGNE)
    assert set(origin.values()) == {"baked into the image"}

    ifsg().to_parquet(inputs / pi.IFSG_FILE)
    kvis(["2022/23"]).to_parquet(inputs / pi.KVIS_FILE)
    origin = pi.prepare(inputs, scenario, COLOGNE)
    assert origin == {COLOGNE.ifsg_file: f"/work/input/{pi.IFSG_FILE}",
                      "influenza-sentinel-weekly.csv": "baked into the image",
                      "vaccination-coverage-2022-26.tsv": f"/work/input/{pi.KVIS_FILE}"}
    assert (scenario / COLOGNE.ifsg_file).is_file()
