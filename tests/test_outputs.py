"""Conversion of a (synthetic) batch output into the EPISERVE output files, and the vaccination guard of run.py."""
from __future__ import annotations

import datetime

import pandas as pd
import pytest

import outputs
import run

START = datetime.date(2022, 9, 26)  # a Monday


def simulation(root, seed: int, factor: float, days: int = 16) -> None:
    folder = root / f"seed_{seed}-gamma_1.00"
    folder.mkdir(parents=True)
    rows = []
    for day in range(days):
        date = START + datetime.timedelta(days=day)
        for district, scale in (("Köln", 1.0), ("total", 10.0)):
            # one new case per day and metric in Köln, times the factor of the seed
            n = (day + 1) * factor * scale
            rows.append({"day": day + 1, "date": date.isoformat(), "district": district, "nInfectedCumulative": n,
                         "nShowingSymptomsCumulative": n / 2, "nSeriouslySickCumulative": n / 10,
                         "nCriticalCumulative": n / 100})
    pd.DataFrame(rows).to_csv(folder / f"seed{seed}.infections.txt", sep="\t", index=False)

    strains = pd.DataFrame({"day": range(1, days + 1),
                            "date": [(START + datetime.timedelta(days=d)).isoformat() for d in range(days)],
                            "H3N2": [factor] * days, "SARS_CoV_2": [0.0] * days, "OTHER": [7.0] * days})
    strains.to_csv(folder / f"seed{seed}.strains.tsv", sep="\t", index=False)
    (folder / f"seed{seed}.config.xml").write_text(
        '<config><module name="virusStrains" >'
        '<parameterset type="strain"><param name="strain" value="SARS_CoV_2" /></parameterset>'
        '<parameterset type="strain"><param name="strain" value="H3N2" /></parameterset>'
        '</module></config>', encoding="utf-8")


def test_write(tmp_path):
    batch = tmp_path / "output"
    simulation(batch, 1, 1.0)
    simulation(batch, 2, 3.0)
    summary = outputs.write(batch, "Köln", tmp_path / "out")
    # 16 days from a Monday: two complete weeks, the two days of the third are left out
    assert summary == {"seeds": 2, "weeks": 2}

    p = pd.read_csv(tmp_path / "out" / "predictions.tsv", sep="\t")
    assert list(p["week"]) == ["2022-W39", "2022-W40"]
    assert list(p["week_start"]) == ["2022-09-26", "2022-10-03"]
    # seed 1: 7 infections a week, seed 2: 21; mean 14, q10 and q90 interpolated
    assert list(p["infections"]) == [14.0, 14.0]
    assert p["infections_q10"][0] == pytest.approx(8.4)
    assert p["infections_q90"][0] == pytest.approx(19.6)
    assert p["symptomatic"][0] == pytest.approx(7.0)
    assert {f"{m}_{q}" for m in outputs.METRICS for q in outputs.QUANTILES} <= set(p.columns)

    strains = pd.read_parquet(tmp_path / "out" / "infections_by_strain.parquet")
    assert set(strains["strain"]) == {"H3N2"}
    assert sorted(strains[strains.seed == "2"]["infections"]) == [21.0, 21.0]


def test_write_without_simulations(tmp_path):
    with pytest.raises(RuntimeError, match="no \\*.infections.txt"):
        outputs.write(tmp_path, "Köln", tmp_path / "out")


@pytest.mark.parametrize("module, vaccination, ok", [
    (True, True, True), (False, False, True), (True, False, False), (False, True, False),
])
def test_check_vaccination(tmp_path, module, vaccination, ok):
    xml = tmp_path / "config.xml"
    xml.write_text('<config>' + ('<module name="seasonalVaccination" >' if module else '') + '</config>',
                   encoding="utf-8")
    if ok:
        run.check_vaccination(xml, vaccination)
    else:
        with pytest.raises(RuntimeError, match="seasonalVaccination"):
            run.check_vaccination(xml, vaccination)
