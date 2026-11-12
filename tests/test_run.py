"""main() of run.py end to end with a stand-in for the Java generator: the dry run, which skips the simulation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import prepare_inputs
import run


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """An image layout with one scenario folder, a /work folder, and a generator that writes config.xml."""
    app, work = tmp_path / "app", tmp_path / "work"
    city = prepare_inputs.CITIES["cologne"]
    scenario = app / "Scenarios" / city.folder
    scenario.mkdir(parents=True)
    (scenario / city.ifsg_file).write_text("week\tcases\n", encoding="utf-8")
    (scenario / "README.md").write_text("not copied", encoding="utf-8")
    (app / "BUILD_INFO").write_text("episimCommit=test\n", encoding="utf-8")
    (work / "input").mkdir(parents=True)
    monkeypatch.setattr(run, "APP", app)
    monkeypatch.setattr(run, "WORK", work)

    calls = []

    def java(*args, cwd, env=None):
        calls.append(args)
        if args[0] == run.GENERATORS["cologne"][0]:
            module = "" if "--without-vaccination" in args else '<module name="seasonalVaccination" >'
            (cwd / "Scenarios" / city.folder / "config.xml").write_text(f"<config>{module}</config>", encoding="utf-8")
        else:
            raise AssertionError(f"a dry run must not simulate: {args}")

    monkeypatch.setattr(run, "java", java)
    return work, calls


@pytest.mark.parametrize("vaccination", [True, False])
def test_dry_run(platform, vaccination):
    work, calls = platform
    config = {"city": "cologne", "prediction_length": 4, "seeds": 1, "dry_run": True, "vaccination": vaccination}
    (work / "input" / "config.json").write_text(json.dumps(config), encoding="utf-8")

    assert run.main() == 0
    assert len(calls) == 1
    assert ("--without-vaccination" in calls[0]) is not vaccination

    out = work / "output"
    assert sorted(p.name for p in out.iterdir()) == ["run_info.json", "scenario"]
    assert sorted(p.name for p in (out / "scenario").iterdir()) == ["config.xml", prepare_inputs.CITIES["cologne"].ifsg_file]
    info = json.loads((out / "run_info.json").read_text(encoding="utf-8"))
    assert info["dry_run"] is True
    assert info["config"]["prediction_length"] == 4
    assert info["simulation_end"] == "2022-10-23"
    assert info["build"] == "episimCommit=test\n"
    assert set(info["inputs"].values()) == {"baked into the image"}


def test_bad_config_fails_before_any_work(platform):
    work, calls = platform
    (work / "input" / "config.json").write_text('{"city": "hamburg", "dry_run": true}', encoding="utf-8")
    with pytest.raises(run.ConfigError):
        run.main()
    assert calls == []
    assert not (work / "output").exists()


def test_generator_ignoring_the_vaccination_switch(platform, monkeypatch):
    work, _ = platform
    city = prepare_inputs.CITIES["cologne"]

    def old_generator(*args, cwd, env=None):
        (cwd / "Scenarios" / city.folder / "config.xml").write_text('<module name="seasonalVaccination" >', encoding="utf-8")

    monkeypatch.setattr(run, "java", old_generator)
    (work / "input" / "config.json").write_text('{"city": "cologne", "vaccination": false, "dry_run": true}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="does the matsim-episim commit"):
        run.main()


def test_unknown_argument():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, str(Path(run.__file__)), "--fast"], capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.startswith("ERROR: unknown arguments ['--fast']")


def viewer_package(root: Path, metadata: str) -> Path:
    package = root / "output-vis-no-seeds"
    (package / "summaries").mkdir(parents=True)
    (package / "observed").mkdir()
    (package / "metadata.yaml").write_text(metadata, encoding="utf-8")
    (package / "_info.txt").write_text("RunId\tseed\n0\t4711\n", encoding="utf-8")
    (package / "notes.md").write_text("notes", encoding="utf-8")
    (package / "summaries" / "0.zip").write_bytes(b"PK")
    (package / "observed" / "weekly.tsv").write_text("week\tcases\n", encoding="utf-8")
    return package


def test_viewer_package_is_copied_unpacked(tmp_path):
    package = viewer_package(tmp_path / "batch", "zipFolder: summaries\ndefaultStartDate: 2022-09-26\ncity: Köln\n")
    out = tmp_path / "out"
    out.mkdir()
    (out / "predictions.tsv").write_text("week\n", encoding="utf-8")
    run.copy_viewer_package(package, out)

    assert sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()) == [
        "_info.txt", "metadata.yaml", "notes.md", "observed/weekly.tsv", "predictions.tsv", "summaries/0.zip"]
    metadata = (out / "metadata.yaml").read_text(encoding="utf-8")
    assert metadata.endswith("city: Köln\ngraphStartDate: 2022-09-26\n")


def test_viewer_package_keeps_an_existing_graph_start_date(tmp_path):
    text = "graphStartDate: 2022-10-01\ndefaultStartDate: 2022-09-26\n"
    package = viewer_package(tmp_path / "batch", text)
    run.copy_viewer_package(package, tmp_path)
    assert (tmp_path / "metadata.yaml").read_text(encoding="utf-8") == text


def test_viewer_package_must_not_overwrite_model_outputs(tmp_path):
    package = viewer_package(tmp_path / "batch", "defaultStartDate: 2022-09-26\n")
    (package / "predictions.tsv").write_text("not ours", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "predictions.tsv").write_text("week\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="collides"):
        run.copy_viewer_package(package, out)
    with pytest.raises(RuntimeError, match="no viewer package"):
        run.copy_viewer_package(tmp_path / "missing", out)
