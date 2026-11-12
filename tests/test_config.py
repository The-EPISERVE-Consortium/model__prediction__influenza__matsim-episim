"""config.json validation of run.py and the agreement of run.py with the model card (fdo.json)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import prepare_inputs
import run

ROOT = Path(__file__).resolve().parents[1]
CARD = json.loads((ROOT / "fdo.json").read_text(encoding="utf-8"))
PARAMETERS = CARD["episerve:parameters"]["properties"]


def config_file(tmp_path: Path, content: object) -> Path:
    path = tmp_path / "config.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def test_defaults_are_filled_in(tmp_path):
    config = run.read_config(config_file(tmp_path, {"city": "cologne"}))
    assert config == {"city": "cologne", **run.DEFAULTS}


def test_full_config(tmp_path):
    raw = {"city": "berlin", "prediction_length": 33, "seeds": 3, "gamma": 0.85, "vaccination": False, "tasks": 2}
    assert run.read_config(config_file(tmp_path, raw)) == raw


def test_gamma_is_rounded_to_the_grid(tmp_path):
    config = run.read_config(config_file(tmp_path, {"city": "cologne", "gamma": 0.15000000000000002}))
    assert config["gamma"] == 0.15
    assert run.read_config(config_file(tmp_path, {"city": "cologne", "gamma": 1}))["gamma"] == 1.0


@pytest.mark.parametrize("raw, message", [
    ({}, "city must be one of"),
    ({"city": "hamburg"}, "city must be one of"),
    ({"city": "cologne", "horizon": 3}, "unknown parameters ['horizon']"),
    ({"city": "cologne", "prediction_length": 0}, "prediction_length must be an integer from 1 to 190"),
    ({"city": "cologne", "prediction_length": 191}, "prediction_length must be an integer from 1 to 190"),
    ({"city": "cologne", "prediction_length": 4.0}, "prediction_length must be an integer"),
    ({"city": "cologne", "seeds": True}, "seeds must be an integer"),
    ({"city": "cologne", "seeds": 11}, "seeds must be an integer from 1 to 10"),
    ({"city": "cologne", "tasks": 0}, "tasks must be an integer"),
    ({"city": "cologne", "gamma": 0.07}, "gamma must be a multiple of 0.05"),
    ({"city": "cologne", "gamma": 1.55}, "gamma must be a multiple of 0.05"),
    ({"city": "cologne", "gamma": "1.0"}, "gamma must be a multiple of 0.05"),
    ({"city": "cologne", "vaccination": "no"}, "vaccination must be true or false"),
])
def test_bad_config(tmp_path, raw, message):
    with pytest.raises(run.ConfigError, match=None) as e:
        run.read_config(config_file(tmp_path, raw))
    assert message in str(e.value)


@pytest.mark.parametrize("content, message", [
    ("{not json", "not valid JSON"),
    ("[1, 2]", "must be a JSON object"),
])
def test_unreadable_config(tmp_path, content, message):
    with pytest.raises(run.ConfigError) as e:
        run.read_config(config_file(tmp_path, content))
    assert message in str(e.value)


def test_missing_config(tmp_path):
    with pytest.raises(run.ConfigError, match="not found"):
        run.read_config(tmp_path / "config.json")


def test_check_config_cli(tmp_path):
    good = config_file(tmp_path, {"city": "cologne", "prediction_length": 4})
    result = subprocess.run([sys.executable, str(ROOT / "src" / "run.py"), "--check-config", str(good)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["prediction_length"] == 4

    bad = config_file(tmp_path, {"city": "hamburg"})
    result = subprocess.run([sys.executable, str(ROOT / "src" / "run.py"), "--check-config", str(bad)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.startswith("ERROR: city must be one of")


# --- card and code agree --------------------------------------------------------------------------------------------

def test_card_parameters_match_the_code():
    assert set(PARAMETERS) == set(run.DEFAULTS) | {"city"}
    assert CARD["episerve:parameters"]["required"] == ["city"]
    for name, default in run.DEFAULTS.items():
        assert PARAMETERS[name]["default"] == default, name


def test_card_bounds_match_the_code():
    assert PARAMETERS["city"]["enum"] == sorted(prepare_inputs.CITIES, reverse=True)
    assert PARAMETERS["prediction_length"]["maximum"] == run.MAX_WEEKS == CARD["episerve:task"]["maxHorizon"]
    assert PARAMETERS["seeds"]["maximum"] == run.MAX_SEEDS == PARAMETERS["tasks"]["maximum"]
    assert PARAMETERS["gamma"]["minimum"] == run.GAMMA_GRID[0]
    assert PARAMETERS["gamma"]["maximum"] == run.GAMMA_GRID[-1]
    assert CARD["episerve:task"]["parameterRoles"] == {"horizon": "prediction_length"}


def test_card_inputs_match_the_code():
    files = [i["file"] for i in CARD["episerve:inputs"]]
    assert files == [prepare_inputs.IFSG_FILE, prepare_inputs.FLUNET_FILE, prepare_inputs.KVIS_FILE]
    assert all(not i["required"] for i in CARD["episerve:inputs"])
