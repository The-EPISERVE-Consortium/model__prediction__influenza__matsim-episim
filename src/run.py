"""Model run of the multi-season influenza scenarios (Cologne, Berlin) on the EPISERVE platform.

Contract of the platform's model-runner: inputs and config.json in /work/input, everything written to /work/output is
stored with the run. The model card is fdo.json in the root of the repository. The Java model (episim.jar) and the
scenarios come from the matsim-episim commit pinned in episim.ref.

    python src/run.py                      the run (what the model-runner starts)
    python src/run.py --check-config FILE  validates a config.json and prints it with the defaults, without running

The run is a forecast in the sense of the platform only: the simulation always starts on 2022-09-26 (Monday of ISO week
39/2022) and runs prediction_length weeks of the past four seasons (at most 190, up to 2026-05-17). The output is the
simulated weekly epidemic, one series per seed, summarised as mean and 10th/90th percentile over the seeds.

Steps: rebuild the scenario data from the EPISERVE inputs (prepare_inputs), write config, progression and policy
(GenerateInfluenzaConfig), simulate (RunInfluenzaGamma), convert (outputs). The mobility (events) and the population are
read from the public VSP SVN at run time, so the container needs internet access.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import outputs
import prepare_inputs

APP = Path(os.environ.get("EPISIM_HOME", "/opt/episim"))
WORK = Path(os.environ.get("EPISERVE_WORK", "/work"))
JAR = APP / "episim.jar"

START = datetime.date(2022, 9, 26)
MAX_WEEKS = 190  # 1,330 days, 2022-09-26 to 2026-05-17
MAX_SEEDS = 10   # @GenerateSeeds of RunInfluenzaGamma
GAMMA_GRID = [round(0.05 * i, 2) for i in range(1, 31)]

GENERATORS = {
    "cologne": ("org.matsim.episim.run.scenarios.cologne.GenerateInfluenzaConfig", "cologne-multiseason"),
    "berlin": ("org.matsim.episim.run.scenarios.berlin.GenerateInfluenzaConfig", "berlin-multiseason"),
}

DEFAULTS = {"prediction_length": MAX_WEEKS, "seeds": 2, "gamma": 1.0, "vaccination": True, "tasks": 1, "dry_run": False}


class ConfigError(Exception):
    pass


def log(message: str) -> None:
    print(f"[episerve] {message}", flush=True)


def read_config(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"{path} not found")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"config.json is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config.json must be a JSON object")

    unknown = sorted(set(raw) - set(DEFAULTS) - {"city"})
    if unknown:
        raise ConfigError(f"unknown parameters {unknown}; allowed: city, {', '.join(DEFAULTS)}")
    config = {**DEFAULTS, **raw}

    city = config.get("city")
    if city not in prepare_inputs.CITIES:
        raise ConfigError(f"city must be one of {sorted(prepare_inputs.CITIES)}, got {city!r}")

    def integer(name, low, high):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ConfigError(f"{name} must be an integer from {low} to {high}, got {value!r}")

    integer("prediction_length", 1, MAX_WEEKS)
    integer("seeds", 1, MAX_SEEDS)
    integer("tasks", 1, MAX_SEEDS)
    gamma = config["gamma"]
    if isinstance(gamma, bool) or not isinstance(gamma, (int, float)) \
            or not any(math.isclose(gamma, g, abs_tol=1e-9) for g in GAMMA_GRID):
        raise ConfigError(f"gamma must be a multiple of 0.05 from 0.05 to 1.50, got {gamma!r}")
    config["gamma"] = round(float(gamma), 2)
    for name in ("vaccination", "dry_run"):
        if not isinstance(config[name], bool):
            raise ConfigError(f"{name} must be true or false, got {config[name]!r}")
    return config


def check_vaccination(config_xml: Path, vaccination: bool) -> None:
    """Guards against a matsim-episim commit whose generator ignores --without-vaccination."""
    present = '<module name="seasonalVaccination"' in config_xml.read_text(encoding="utf-8")
    if present != vaccination:
        raise RuntimeError(f"{config_xml.name} {'has' if present else 'lacks'} the seasonalVaccination module although "
                           f"vaccination is {str(vaccination).lower()}; does the matsim-episim commit of episim.ref "
                           "support --without-vaccination?")


def java(*args: str, cwd: Path, env: dict | None = None) -> None:
    command = ["java", "-cp", str(JAR), *args]
    log("$ " + " ".join(command))
    result = subprocess.run(command, cwd=cwd, env={**os.environ, **(env or {})})
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} failed with exit code {result.returncode}")


def main() -> int:
    input_dir, output_dir, run_dir = WORK / "input", WORK / "output", WORK / "run"
    started = datetime.datetime.now(datetime.UTC)
    config = read_config(input_dir / "config.json")
    city = prepare_inputs.CITIES[config["city"]]
    iterations = config["prediction_length"] * 7
    log(f"config {config}: {iterations} days from {START} to {START + datetime.timedelta(days=iterations - 1)}")

    # a writable copy of the scenarios; the generator reads and writes relative to the working directory
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    shutil.copytree(APP / "Scenarios", run_dir / "Scenarios")
    scenario = run_dir / "Scenarios" / city.folder

    origin = prepare_inputs.prepare(input_dir, scenario, city)
    for file, source in origin.items():
        log(f"{file}: {source}")

    generator, scenario_arg = GENERATORS[city.key]
    java(generator, scenario_arg, *([] if config["vaccination"] else ["--without-vaccination"]), cwd=run_dir)
    check_vaccination(scenario / "config.xml", config["vaccination"])

    output_dir.mkdir(parents=True, exist_ok=True)
    if config["dry_run"]:
        # everything but the simulation: no events from the SVN, no network, seconds instead of hours
        log("dry_run: config generated, simulation skipped")
        copy_scenario(scenario, output_dir / "scenario")
        write_run_info(output_dir, config, city, iterations, origin, started, {"dry_run": True})
        log(f"done, output in {output_dir}")
        return 0

    batch_args = ["--scenario", f"Scenarios/{city.folder}", "--seeds", str(config["seeds"]),
                  "--gamma", f"{config['gamma']:.2f}", "--tasks", str(config["tasks"])]
    if config["prediction_length"] < MAX_WEEKS:
        batch_args += ["--iterations", str(iterations)]
    java("org.matsim.episim.run.batch.RunInfluenzaGamma", *batch_args, cwd=run_dir,
         env={"EPISIM_OUTPUT": str(run_dir / "batch")})

    batch = sorted(p for p in (run_dir / "batch").glob("*/*") if (p / "output").is_dir())
    if not batch:
        raise RuntimeError(f"no batch output below {run_dir / 'batch'}")
    batch = batch[-1]

    summary = outputs.write(batch / "output", city.district, output_dir)
    log(f"converted {summary['seeds']} seed(s), {summary['weeks']} weeks")

    # the covid-sim viewer package and the scenario as it was run, for inspection
    with tarfile.open(output_dir / "viewer.tar.gz", "w:gz") as tar:
        tar.add(batch / "output-vis-no-seeds", arcname="output-vis-no-seeds")
    copy_scenario(scenario, output_dir / "scenario")
    write_run_info(output_dir, config, city, iterations, origin, started, summary)
    log(f"done, output in {output_dir}")
    return 0


def copy_scenario(scenario: Path, target: Path) -> None:
    """The generated config, policy and progression and the data files, as they were run."""
    target.mkdir(parents=True, exist_ok=True)
    for f in sorted(scenario.iterdir()):
        if f.is_file() and f.suffix in (".xml", ".conf", ".yaml", ".tsv", ".csv"):
            shutil.copy2(f, target / f.name)


def write_run_info(output_dir: Path, config: dict, city: prepare_inputs.City, iterations: int, origin: dict[str, str],
                   started: datetime.datetime, summary: dict) -> None:
    (output_dir / "run_info.json").write_text(json.dumps({
        "model": "model__prediction__influenza__matsim-episim",
        "build": (APP / "BUILD_INFO").read_text(encoding="utf-8") if (APP / "BUILD_INFO").exists() else None,
        "config": config,
        "city": city.district,
        "simulation_start": START.isoformat(),
        "simulation_end": (START + datetime.timedelta(days=iterations - 1)).isoformat(),
        "inputs": origin,
        "started": started.isoformat(),
        "finished": datetime.datetime.now(datetime.UTC).isoformat(),
        **summary,
    }, indent=2, ensure_ascii=False), encoding="utf-8")


def check_config(path: Path) -> int:
    print(json.dumps(read_config(path), indent=2))
    return 0


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--check-config":
            sys.exit(check_config(Path(sys.argv[2])))
        if len(sys.argv) > 1:
            raise ConfigError(f"unknown arguments {sys.argv[1:]}; usage: run.py [--check-config FILE]")
        sys.exit(main())
    except (ConfigError, prepare_inputs.InputError, RuntimeError) as e:
        # the model-runner appends the first line starting with ERROR: to the failure message
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
