# model__prediction__influenza__matsim-episim

The multi-season influenza scenarios of [matsim-episim](https://github.com/matsim-org/matsim-episim) (Cologne and
Berlin, seasons 2022/23 to 2025/26) as a model of the EPISERVE platform.

This repository is a thin adapter. It holds no model code: the image builds matsim-episim from the commit pinned in
[`episim.ref`](episim.ref) and adds the platform side:

| Path | Role |
|---|---|
| [`episim.ref`](episim.ref) | matsim-episim repository and commit the image is built from |
| [`src/run.py`](src/run.py) | container contract: reads `/work/input/config.json`, runs the scenario, writes `/work/output` |
| [`src/prepare_inputs.py`](src/prepare_inputs.py) | rebuilds the scenario data files from the EPISERVE datasets passed as input |
| [`src/external.py`](src/external.py) | with `fetch_external`: school holidays and weather from their public APIs, raw responses kept |
| [`src/outputs.py`](src/outputs.py) | converts the batch output into the declared output files |
| [`fdo.json`](fdo.json) | model card (EPISERVE model-card v1) |
| [`.github/workflows/publish.yml`](.github/workflows/publish.yml) | card check, image build, tests, push to ghcr.io |

## What a run does

1. Reads `config.json` (see below) and copies the scenarios of the image into `/work/run`.
2. Rebuilds the data files of the scenario from the EPISERVE datasets in `/work/input`
   ([`prepare_inputs.py`](src/prepare_inputs.py)); a dataset that is not passed keeps the file of the pinned commit.
3. Writes config, progression and policy with `GenerateInfluenzaConfig` (`--without-vaccination` if `vaccination` is
   false) and checks that the vaccination module is present or absent accordingly.
4. Simulates with `RunInfluenzaGamma` from 2022-09-26 for `prediction_length` weeks.
5. Converts the output ([`outputs.py`](src/outputs.py)) into `/work/output`.

The mobility (events) and the population are read from the public VSP SVN during the run, so the pod needs internet access
(`network: true` in the card).

### A forecast in name only

Version 1 of the EPISERVE model card knows only the task `forecast`. The scenarios simulate the past: the start is always
2022-09-26 (Monday of ISO week 39/2022) and `prediction_length` (the horizon role) is the number of weeks simulated from
there, at most 190 (up to 2026-05-17). The "forecast interval" is the spread over the seeds.

## Parameters (`config.json`)

| Parameter | Default | Meaning |
|---|---|---|
| `city` | (required) | `cologne` (Scenarios/Cologne-multiseason) or `berlin` (Scenarios/Berlin-multiseason) |
| `prediction_length` | 190 | weeks from 2022-09-26; 33 is the first season |
| `seeds` | 2 | number of seeds (1 to 10), the first ones of the batch |
| `gamma` | 0.5 | common calibration factor, 0.05 to 1.50 in steps of 0.05; 0.5 is provisional (the calibration is not finished), 1.0 is the uncalibrated configuration and gives far more infections than observed |
| `vaccination` | true | false runs the scenario without the seasonal vaccination of the persons aged 60 and over |
| `tasks` | 1 | simulations in parallel; each needs about 8 GB (Cologne) to 14 GB (Berlin) |
| `dry_run` | false | true runs everything but the simulation (see below) |
| `resume` | false | true continues each seed from a snapshot instead of simulating it from 2022-09-26 (see "Resuming from a snapshot" below) |
| `snapshot_at` | null | with `resume`, the day (1 to 1329) to resume from; the latest snapshot of the seed if omitted |
| `keep_snapshots` | false | true writes every seed's snapshot files into this run's `snapshot/` output, for a later `resume` |
| `fetch_external` | false | true fetches the school holidays and the weather at run time instead of using the baked files (see "Holidays and weather from their APIs" below) |

### Dry run

With `"dry_run": true` a run reads and checks the config, rebuilds the scenario data from the inputs, generates config,
policy and progression with the Java generator, checks the vaccination switch, and writes `run_info.json` and `scenario/`.
It does not simulate, so it needs no network (the events are not read) and little memory, and takes a few seconds, like
the null model. `predictions.tsv` and the other outputs are not written. It is meant for testing the platform, the inputs
and a new image; CI runs it for both cities with `--network none --memory 1g`.

## Resuming from a snapshot

Optional, off by default: a run always simulates the whole requested period from 2022-09-26, same as before. To skip
years another run already covers:

1. Run A with `"keep_snapshots": true`. It writes `/work/output/snapshot/seed_<seed>-gamma_<gamma>/episim-snapshot-*.zip`
   for every seed, one file per 100 days of the run (the generator's fixed `snapshotInterval`); three of the thirteen
   fall in the gaps between seasons (days 300, 700 and 1000).
2. Give Run B's `/work/input/snapshot` the `snapshot/` output of Run A (the same folder layout, seed folders included),
   and set `"resume": true`. Run B then continues each of its seeds from the latest snapshot in its folder (or the one
   named by `snapshot_at`, if a specific day is wanted, such as 1000 for the start of 2025/26) instead of simulating the
   earlier days again; `prediction_length` (and `seeds`) still name the end (and the seeds) of Run B, now reached partly
   from the snapshot.

Needs the **same city, build (`episim.ref`) and seeds as Run A** (or a subset of its seeds) and, to continue the same
scenario rather than deliberately diverge from it, the same `gamma` and `vaccination`: the snapshot carries the
simulated state (infections, immunity, progression) but not the config, so the values of Run B apply from the resumed
day onward, same as matsim-episim's own `--snapshot-dir` of `RunInfluenzaGamma`. A mismatch (missing seed, wrong build,
resuming at or after `prediction_length`) fails before simulating, not after.

## Inputs (all optional)

| File in `/work/input` | EPISERVE dataset | Rebuilds |
|---|---|---|
| `IfSG_Influenzafaelle.parquet` | Q8399965930733, RKI laboratory-confirmed influenza | `nrw-ifsg-influenza-2022-26.tsv` (Region_Id 5) or `berlin-ifsg-influenza-2022-26.tsv` (Region_Id 11): shape of the import |
| `VIW_FNT.parquet` | Q4242040001970, WHO FluNet Germany | `influenza-sentinel-weekly.csv`: split of the import between the A subtype and B/Victoria |
| `KVIS_Impfquoten_Erwachsene.parquet` | Q0602352424740, RKI KV-Impfsurveillance | `vaccination-coverage-2022-26.tsv`: coverage of SK Köln (5315) or SK Berlin (11000) |
| `SARI-Hospitalisierungsinzidenz.parquet` | Q0522578154235, RKI ICOSARI | `germany-icosari-influenza-sari-2022-26.tsv`: influenza SARI incidence of Germany (00+), the observed series plotted next to the hospitalisations of the model; not an input of the simulation |

The rebuild follows `Scenarios/Cologne-multiseason/build-reference-data.py` and `build-vaccination-data.py` of
matsim-episim. Not taken from EPISERVE: the weather (Open-Meteo historical; the platform has Berlin only, without precipitation),
the school holidays and the age table of the population. The first two can be fetched from their APIs at run time instead.

### Holidays and weather from their APIs (`fetch_external`)

With `"fetch_external": true` [`src/external.py`](src/external.py) replaces two baked files of the scenario:

| File | API | Request |
|---|---|---|
| `nrw-school-holidays-2022-26.tsv` / `berlin-school-holidays-2022-26.tsv` | [OpenHolidays](https://openholidaysapi.org) `/SchoolHolidays` | subdivision `DE-NW` or `DE-BE`, 2022-08-01 to 2026-05-31 in pieces of at most 1,095 days (the limit of the API) |
| `cologne-weather-2022-26.csv` / `berlin-weather-2022-26.csv` | [Open-Meteo](https://open-meteo.com) Historical Weather `/v1/archive` | the request of the baked file: 50.94 N 6.96 E or 52.47 N 13.40 E, 2022-09-01 to 2026-05-31, `temperature_2m_max`, `precipitation_sum` |

The holiday names are mapped to those of the baked files (Herbstferien `autumn`, Weihnachtsferien `christmas`, Winterferien
`winter`, Osterferien `easter`, Sommerferien `summer`, Unterrichtsfreier Tag `bridge_day`; a one-day Pfingstferien is
`whit_day` in NRW and `bridge_day` in Berlin); an unknown name fails the run.

A live request has no dataset version on the platform, so every raw response is written to `external/` of the output and its
URL, time of retrieval and sha256 to the `inputs` of `run_info.json`. A failed request fails the run, also a dry run, which
then needs network; there is no silent fallback to the baked file. Once EPISERVE has datasets for holidays and weather they
become inputs like the others and take precedence.

Compared with the baked files (2026-11-16): the holidays of NRW are identical; Berlin has one more day, 2025-06-10
(Pfingstferien), missing in the baked file. The weather is the same grid cell, but Open-Meteo has revised the reanalysis
since the baked files were retrieved (2026-09-30 and 2026-10-01): tmax differs on about 16 % of the days (mostly by 0.1,
at most 2.0 deg C), precipitation on 22 to 29 % (at most 4.4 mm).

## Outputs

| File | Content |
|---|---|
| `predictions.tsv` | per ISO week: new infections, symptomatic, hospitalisations and critical cases in the city, scaled to the whole population; mean and `_q10`/`_q90` over the seeds |
| `simulation_weekly.parquet` | the same weekly values for every seed |
| `infections_by_strain.parquet` | weekly new infections by transmission (without imports) by strain and seed, strains of the scenario only |
| `incidence_by_age.parquet` | weekly incidence per 100,000 by age group and seed |
| `run_info.json` | config, image build (including the matsim-episim commit), period and the origin of every input file |
| `metadata.yaml`, `_info.txt`, `notes.md`, `summaries/`, `observed/` | the covid-sim viewer package (`output-vis-no-seeds` of the batch), unpacked at the root of the output so that episim-vis opens the run from EPISERVE; `graphStartDate` is added to `metadata.yaml` |
| `scenario/` | config, policy, progression and the data files as they were run |
| `snapshot/` | only with `keep_snapshots`: every seed's snapshot files, for `resume` by a later run (see "Resuming from a snapshot") |
| `external/` | only with `fetch_external`: the raw responses of OpenHolidays and Open-Meteo |

## Starting a run on EPISERVE

Token from `POST /auth/token`; the model does not have to be registered before its first run.

```bash
curl -X POST https://api.episerve.zib.de/model-runs \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "model_image": "ghcr.io/the-episerve-consortium/model__prediction__influenza__matsim-episim",
    "model_tag": "latest",
    "input_data_files": [
      ["https://doip.episerve.zib.de/doip/retrieve/Q8399965930733/IfSG_Influenzafaelle.parquet", "IfSG_Influenzafaelle.parquet"],
      ["https://doip.episerve.zib.de/doip/retrieve/Q4242040001970/VIW_FNT.parquet", "VIW_FNT.parquet"],
      ["https://doip.episerve.zib.de/doip/retrieve/Q0602352424740/KVIS_Impfquoten_Erwachsene.parquet", "KVIS_Impfquoten_Erwachsene.parquet"],
      ["https://doip.episerve.zib.de/doip/retrieve/Q0522578154235/SARI-Hospitalisierungsinzidenz.parquet", "SARI-Hospitalisierungsinzidenz.parquet"]
    ],
    "config": {"city": "cologne", "prediction_length": 4, "seeds": 1}
  }'
```

For a test of the platform alone, add `"dry_run": true` to the config: the run then ends in seconds.

`POST /model-runs` returns the id of the Prefect flow run; `GET /model-runs/{run_id}` looks the run up by its QID in the
catalogue, where it appears only after it has finished and has been synchronised, so the id of the POST answer is not found
there. Look for the run in `GET /model-runs` by `model_name` instead.

## Local development

Tests and lint (Python 3.12+):

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-test.txt
.venv/bin/ruff check src tests && .venv/bin/python -m pytest
```

Image from the pinned commit, or from a working copy of matsim-episim next to this repository (the named context
replaces the clone, nothing is fetched from GitHub):

```bash
podman build -t episim-model .
podman build -t episim-model --build-context episim-source=../matsim-episim .
```

A run with the same layout as on the platform:

```bash
mkdir -p work/input work/output && echo '{"city": "cologne", "prediction_length": 1, "seeds": 1}' > work/input/config.json
podman run --rm -v "$PWD/work:/work" episim-model
```

## Updating the model

1. Push the change to matsim-episim.
2. Put the full commit SHA into `ref=` of [`episim.ref`](episim.ref); a change of the scenario folders may also need
   changes in `src/` (file names in `prepare_inputs.CITIES`, generator arguments in `run.GENERATORS`).
3. Push to `main` (image `latest` and `sha-<commit>`) or tag `vX.Y.Z` for a pinned version.

The platform reads `fdo.json` from the default branch once an hour; keep the card and `src/run.py` in agreement
(`tests/test_config.py` checks it).

## License

AGPL-3.0-only, like matsim-episim.
