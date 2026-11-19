"""Snapshots of a previous run from DOIP (resume_from), with a stand-in for the HTTP requests."""
from __future__ import annotations

import hashlib
import http.client
import io
import json

import pytest

import snapshots as sn
from prepare_inputs import InputError

QID = "Q1794860351655"
BUILD = "gitCommit=abc\nepisimCommit=cb0b8bb\n"
FOLDERS = ["seed_4711-gamma_0.5", "seed_7564655870752979346-gamma_0.5"]
DAYS = {100: "2023-01-03", 1000: "2025-06-21", 1300: "2026-04-17"}


def body(folder: str, day: int) -> bytes:
    return f"PK {folder} {day}".encode() * 1000


class Response(io.BytesIO):
    """A response that may be cut after `cut` bytes like DOIP's proxy does: IncompleteRead, or a silent end."""

    def __init__(self, data: bytes, headers: dict | None = None, status: int = 200, cut: int | None = None,
                 silent: bool = False):
        super().__init__(data if cut is None else data[:cut])
        self.headers, self.status = headers or {}, status
        self.cut, self.silent = cut is not None and cut < len(data), silent

    def read(self, n=-1):
        chunk = super().read(n)
        if not chunk and self.cut and not self.silent:
            raise http.client.IncompleteRead(b"")
        return chunk


def serve(data: bytes, start: int, cut: int | None, ranges: bool = True, silent: bool = False) -> Response:
    """The file from byte start (206, Content-Range) or all of it (200, Content-Length), cut after `cut` bytes."""
    if start and ranges:
        return Response(data[start:], {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"}, 206, cut, silent)
    return Response(data, {"Content-Length": str(len(data))}, 200, cut, silent)


def record(folders=FOLDERS) -> dict:
    components = [{"@id": "components/input/config.json"}, {"@id": "components/output/run_info.json"}]
    components += [{"@id": f"components/output/snapshot/{f}/episim-snapshot-{d}-{date}.zip"}
                   for f in folders for d, date in DAYS.items()]
    return {"@id": QID, "kernel": {"fdo:hasComponent": components}}


def run_info(city="berlin", seeds=2, gamma=0.5, vaccination=True, build=BUILD) -> dict:
    return {"build": build, "config": {"city": city, "seeds": seeds, "gamma": gamma, "vaccination": vaccination}}


def fake(rec=None, info=None, cut=None, ranges=True, silent=False):
    calls = []

    def opener(url: str, start: int = 0):
        calls.append(url)
        if url == f"{sn.DOIP}/{QID}":
            return Response(json.dumps(rec if rec is not None else record()).encode())
        if url == f"{sn.DOIP}/{QID}/output/run_info.json":
            return Response(json.dumps(info if info is not None else run_info()).encode())
        prefix = f"{sn.DOIP}/{QID}/output/snapshot/"
        if url.startswith(prefix):
            folder, file = url[len(prefix):].split("/")
            return serve(body(folder, int(file.split("-")[2])), start, cut, ranges, silent)
        raise OSError(f"404 {url}")

    return opener, calls


def config(**changes) -> dict:
    return {"city": "berlin", "seeds": 2, "gamma": 0.5, "vaccination": True, "prediction_length": 190,
            "resume": True, "resume_from": QID, "snapshot_at": None, **changes}


def test_latest_snapshot_of_every_seed(tmp_path):
    opener, calls = fake()
    logged = []
    origin = sn.fetch(config(), 1330, tmp_path, BUILD, opener, logged.append)

    assert len(calls) == 4
    for folder in FOLDERS:
        path = tmp_path / folder / "episim-snapshot-1300-2026-04-17.zip"
        assert path.read_bytes() == body(folder, 1300)
        assert sorted(p.name for p in (tmp_path / folder).iterdir()) == [path.name]
        entry = origin[f"snapshot/{folder}/{path.name}"]
        assert entry.startswith(f"{sn.DOIP}/{QID}/output/snapshot/{folder}/{path.name} (run {QID}, ")
        assert hashlib.sha256(body(folder, 1300)).hexdigest() in entry
    assert len(logged) == 2


def test_snapshot_at_picks_the_day(tmp_path):
    opener, _ = fake()
    origin = sn.fetch(config(snapshot_at=1000), 1330, tmp_path, BUILD, opener, print)
    assert sorted(origin) == [f"snapshot/{f}/episim-snapshot-1000-2025-06-21.zip" for f in FOLDERS]


def test_diverging_gamma_is_logged(tmp_path):
    opener, _ = fake(info=run_info(gamma=0.6))
    logged = []
    sn.fetch(config(), 1330, tmp_path, BUILD, opener, logged.append)
    assert any("gamma is 0.5 here but was 0.6" in line for line in logged)


@pytest.mark.parametrize("info, rec, changes, iterations, message", [
    (run_info(city="cologne"), None, {}, 1330, "is of city 'cologne'"),
    (run_info(build="episimCommit=other\n"), None, {}, 1330, "simulated with matsim-episim other"),
    (run_info(seeds=1), None, {}, 1330, "has 1 seed"),
    (None, record(folders=[]), {}, 1330, "no snapshot/ output"),
    (None, None, {"snapshot_at": 700}, 1330, "no snapshot of day 700"),
    (None, None, {}, 1300, "at or after the end of this run"),
    (None, {"detail": "not found"}, {}, 1330, "is not a run record"),
])
def test_source_that_cannot_be_resumed(tmp_path, info, rec, changes, iterations, message):
    opener, calls = fake(rec=rec, info=info)
    with pytest.raises(InputError, match=message):
        sn.fetch(config(**changes), iterations, tmp_path, BUILD, opener, print)
    assert not any("/output/snapshot/" in url for url in calls)  # nothing downloaded


@pytest.mark.parametrize("silent", [False, True])
def test_cut_download_is_continued_with_ranges(tmp_path, silent):
    opener, calls = fake(cut=7000, silent=silent)  # each part brings 7,000 bytes
    logged = []
    origin = sn.fetch(config(), 1330, tmp_path, BUILD, opener, logged.append)
    for folder in FOLDERS:
        path = tmp_path / folder / "episim-snapshot-1300-2026-04-17.zip"
        assert path.read_bytes() == body(folder, 1300)
        assert hashlib.sha256(body(folder, 1300)).hexdigest() in origin[f"snapshot/{folder}/{path.name}"]
    parts = [-(-len(body(folder, 1300)) // 7000) for folder in FOLDERS]
    assert parts == [4, 6]
    assert len(calls) == 2 + sum(parts)
    assert [f"in {n} request(s)" in line for n, line in zip(parts, logged, strict=True)] == [True, True]


def test_server_ignoring_the_range_restarts_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sn, "MAX_PARTS", 3)
    opener, _ = fake(cut=7000, ranges=False)
    with pytest.raises(InputError, match="still incomplete after 3 requests"):
        sn.fetch(config(), 1330, tmp_path, BUILD, opener, print)


def test_failed_request(tmp_path):
    def opener(url, start=0):
        raise OSError("connection refused")

    with pytest.raises(InputError, match="connection refused"):
        sn.fetch(config(), 1330, tmp_path, BUILD, opener, print)


def test_image_without_build_info_is_not_checked(tmp_path):
    opener, _ = fake()
    logged = []
    sn.fetch(config(), 1330, tmp_path, None, opener, logged.append)
    assert any("no BUILD_INFO" in line for line in logged)
