"""Turns the output of an influenza batch into the files of an EPISERVE model run (/work/output).

Every simulation of the batch (one per seed) has a folder below <run>/output with
  <id>.infections.txt          daily cumulative counts by district, scaled to the whole population
  <id>.strains.tsv             daily new infections by strain, scaled to the whole population
  <id>.post.incidenceByAge.tsv weekly incidence per 100,000 by age group (ExtractInfectionsByAge)
The weekly values are summed over ISO weeks (Monday to Sunday); a week the run does not cover completely is left out.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

# output name -> cumulative column of infections.txt
METRICS = {
    "infections": "nInfectedCumulative",
    "symptomatic": "nShowingSymptomsCumulative",
    "hospitalisations": "nSeriouslySickCumulative",
    "critical": "nCriticalCumulative",
}
QUANTILES = {"q10": 0.1, "q90": 0.9}


def _single(folder: Path, suffix: str) -> Path | None:
    files = sorted(folder.glob(f"*{suffix}"))
    return files[0] if files else None


def _seed_of(folder: Path) -> str:
    # folder names of RunParallel: seed_<seed>-gamma_<value>
    for part in folder.name.split("-"):
        if part.startswith("seed_"):
            return part.removeprefix("seed_")
    return folder.name


def _weekly(daily: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Sums daily values over complete ISO weeks; daily has a column date."""
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    d["week_start"] = d["date"] - pd.to_timedelta(d["date"].dt.weekday, unit="D")
    days = d.groupby("week_start")["date"].nunique()
    w = d.groupby("week_start")[columns].sum()
    w = w[days.reindex(w.index) == 7].reset_index()
    iso = w["week_start"].dt.isocalendar()
    w.insert(0, "week", [f"{y}-W{n:02d}" for y, n in zip(iso["year"], iso["week"], strict=True)])
    w["week_start"] = w["week_start"].dt.date
    return w


def configured_strains(folder: Path) -> list[str] | None:
    """Strains of the virusStrains module of the run's config.xml, without the default SARS_CoV_2 entry; None if unknown.

    strains.tsv has a column for every strain the library knows, most of which the scenario never uses.
    """
    config = _single(folder, ".config.xml")
    if config is None:
        return None
    text = config.read_text(encoding="utf-8")
    start = text.find('<module name="virusStrains"')
    if start < 0:
        return None
    end = text.find("</module>", start)
    strains = re.findall(r'<param name="strain" value="([^"]+)"', text[start:end])
    return [s for s in dict.fromkeys(strains) if s != "SARS_CoV_2"] or None


def simulation_folders(run_output: Path) -> list[Path]:
    return sorted(p.parent for p in run_output.rglob("*.infections.txt"))


def collect(run_output: Path, district: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Weekly totals, weekly infections by strain and weekly incidence by age, each long by seed."""
    totals, strains, ages = [], [], []
    for folder in simulation_folders(run_output):
        seed = _seed_of(folder)

        inf = pd.read_csv(_single(folder, ".infections.txt"), sep="\t")
        if "district" in inf.columns:
            selected = inf[inf["district"] == district]
            if selected.empty:
                selected = inf[inf["district"] == "total"]
            inf = selected
        inf = inf.sort_values("day")
        daily = pd.DataFrame({"date": inf["date"].values})
        for name, column in METRICS.items():
            cumulative = inf[column].to_numpy(dtype=float)
            daily[name] = np.diff(cumulative, prepend=0.0)
        w = _weekly(daily, list(METRICS))
        w.insert(0, "seed", seed)
        totals.append(w)

        strain_file = _single(folder, ".strains.tsv")
        if strain_file is not None:
            s = pd.read_csv(strain_file, sep="\t")
            columns = [c for c in s.columns if c not in ("day", "date")]
            wanted = configured_strains(folder)
            if wanted is not None:
                columns = [c for c in columns if c in wanted]
            sw = _weekly(s, columns).melt(id_vars=["week", "week_start"], var_name="strain", value_name="infections")
            sw.insert(0, "seed", seed)
            strains.append(sw)

        age_file = _single(folder, ".post.incidenceByAge.tsv")
        if age_file is not None:
            a = pd.read_csv(age_file, sep="\t").rename(columns={"date": "week_end"})
            a = a.melt(id_vars=["week_end"], var_name="age_group", value_name="incidence_per_100k")
            a.insert(0, "seed", seed)
            ages.append(a)

    if not totals:
        raise RuntimeError(f"no *.infections.txt below {run_output}")
    return (pd.concat(totals, ignore_index=True),
            pd.concat(strains, ignore_index=True) if strains else pd.DataFrame(),
            pd.concat(ages, ignore_index=True) if ages else pd.DataFrame())


def predictions(totals: pd.DataFrame) -> pd.DataFrame:
    """One row per week: the mean over the seeds and the 10th and 90th percentiles of each metric."""
    rows = []
    for (week, week_start), g in totals.groupby(["week", "week_start"], sort=True):
        row = {"week": week, "week_start": week_start}
        for name in METRICS:
            values = g[name].to_numpy(dtype=float)
            row[name] = float(values.mean())
            for suffix, level in QUANTILES.items():
                row[f"{name}_{suffix}"] = float(np.quantile(values, level))
        rows.append(row)
    return pd.DataFrame(rows)


def write(run_output: Path, district: str, target: Path) -> dict[str, int]:
    totals, strains, ages = collect(run_output, district)
    target.mkdir(parents=True, exist_ok=True)
    predictions(totals).to_csv(target / "predictions.tsv", sep="\t", index=False, float_format="%.2f")
    totals.to_parquet(target / "simulation_weekly.parquet", index=False)
    if not strains.empty:
        strains.to_parquet(target / "infections_by_strain.parquet", index=False)
    if not ages.empty:
        ages.to_parquet(target / "incidence_by_age.parquet", index=False)
    return {"seeds": int(totals["seed"].nunique()), "weeks": int(totals["week"].nunique())}
