"""Fetches the snapshots of a previous EPISERVE run from the public DOIP repository, for resume_from.

The platform's input mechanism (input_data_files) is not used: the run named by resume_from is looked up by its QID, its
run_info.json is checked against this run (same city and matsim-episim build, enough seeds), and one snapshot per seed
folder is downloaded into the folder that RunInfluenzaGamma --snapshot-dir reads:

  https://doip.episerve.zib.de/doip/retrieve/<QID>                              component list (fdo:hasComponent)
  .../<QID>/output/run_info.json                                                 city, build and config of that run
  .../<QID>/output/snapshot/seed_<seed>-gamma_<gamma>/episim-snapshot-<day>-<date>.zip   -> <target>/seed_.../...

The snapshot of day snapshot_at, or the latest one of each seed folder if snapshot_at is omitted. The URL, size and
sha256 of every file go into run_info.json. A failed request fails the run.

DOIP (its envoy proxy) ends a response after about 15 seconds, a snapshot of 220 MB after a third of it, without an
error status; a download is therefore continued with Range requests until it has the size the server announced
(MAX_PARTS at most), and a file shorter than that fails the run instead of being resumed from.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from prepare_inputs import InputError

DOIP = "https://doip.episerve.zib.de/doip/retrieve"
TIMEOUT = 60
CHUNK = 1 << 20
MAX_PARTS = 20  # Range requests per file; a part brings about 70 MB
QID = re.compile(r"^Q[0-9]+$")
SNAPSHOT = re.compile(r"^components/output/snapshot/(seed_[^/]+)/(episim-snapshot-([0-9]+)-[^/]+\.zip)$")

# (url, first byte) -> a response with status, headers and read(n), usable as a context manager; first byte > 0 asks
# for the rest of the file (Range), which the server answers with 206 or, if it ignores the range, with 200 and all of it
Opener = Callable[[str, int], object]
ERRORS = (OSError, http.client.HTTPException)  # a cut stream raises IncompleteRead, which is not an OSError


def http_open(url: str, start: int = 0):
    headers = {"User-Agent": "model__prediction__influenza__matsim-episim"}
    if start:
        headers["Range"] = f"bytes={start}-"
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT)
    except ERRORS as e:
        raise InputError(f"resume_from: request to {url} failed: {e}") from e


@dataclass(frozen=True)
class Snapshot:
    folder: str  # seed_<seed>-gamma_<gamma>
    file: str
    day: int

    def url(self, qid: str) -> str:
        return f"{DOIP}/{qid}/output/snapshot/{self.folder}/{self.file}"


def read(url: str, opener: Opener) -> bytes:
    try:
        with opener(url, 0) as response:
            return response.read()
    except ERRORS as e:
        raise InputError(f"resume_from: request to {url} failed: {e}") from e


def get_json(url: str, opener: Opener):
    body = read(url, opener)
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise InputError(f"resume_from: {url} did not return JSON: {body[:200]!r}") from e


def build_value(build: str | None, key: str) -> str | None:
    """A key=value line of BUILD_INFO (gitCommit, episimCommit, ...)."""
    for line in (build or "").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


def list_snapshots(components: list[dict]) -> list[Snapshot]:
    found = []
    for c in components:
        m = SNAPSHOT.match(c.get("@id", ""))
        if m:
            found.append(Snapshot(m.group(1), m.group(2), int(m.group(3))))
    return found


def choose(snapshots: list[Snapshot], snapshot_at: int | None, qid: str) -> list[Snapshot]:
    """One snapshot per seed folder: the one of day snapshot_at, or the latest."""
    folders = sorted({s.folder for s in snapshots})
    if not folders:
        raise InputError(f"resume_from: run {qid} has no snapshot/ output; was it run with keep_snapshots true?")
    chosen = []
    for folder in folders:
        days = {s.day: s for s in snapshots if s.folder == folder}
        if snapshot_at is None:
            chosen.append(days[max(days)])
        elif snapshot_at in days:
            chosen.append(days[snapshot_at])
        else:
            raise InputError(f"resume_from: run {qid} has no snapshot of day {snapshot_at} for {folder}; "
                             f"it has days {sorted(days)}")
    return chosen


def check_source(info: dict, qid: str, config: dict, build: str | None) -> list[str]:
    """Fails on what cannot be resumed (other city or build, too few seeds); returns notes on what diverges on purpose."""
    source = info.get("config") or {}
    if source.get("city") != config["city"]:
        raise InputError(f"resume_from: run {qid} is of city {source.get('city')!r}, this run of {config['city']!r}")
    theirs, ours = build_value(info.get("build"), "episimCommit"), build_value(build, "episimCommit")
    if ours is None:
        notes = ["this image has no BUILD_INFO, the matsim-episim build of the snapshots is not checked"]
    elif theirs != ours:
        raise InputError(f"resume_from: run {qid} was simulated with matsim-episim {theirs}, this image has {ours}; "
                         "a snapshot can only be resumed by the same build")
    else:
        notes = []
    if not isinstance(source.get("seeds"), int) or source["seeds"] < config["seeds"]:
        raise InputError(f"resume_from: run {qid} has {source.get('seeds')} seed(s), this run asks for {config['seeds']}")
    for name in ("gamma", "vaccination"):
        if source.get(name) != config[name]:
            notes.append(f"{name} is {config[name]!r} here but was {source.get(name)!r} in {qid}: the resumed seeds "
                         "diverge from that run from the snapshot day on")
    return notes


def total_size(response, start: int) -> int | None:
    """The size of the whole file: Content-Range of a 206, else Content-Length of a 200."""
    if start and getattr(response, "status", 200) == 206:
        total = (response.headers.get("Content-Range") or "").rpartition("/")[2]
        return int(total) if total.isdigit() else None
    length = response.headers.get("Content-Length")
    return int(length) if length is not None else None


def download(url: str, target: Path, opener: Opener) -> tuple[int, str, int]:
    """Downloads url to target, continuing a cut response with Range requests; returns size, sha256 and parts."""
    target.parent.mkdir(parents=True, exist_ok=True)
    size, total, parts = 0, None, 0
    with open(target, "wb") as out:
        while parts < MAX_PARTS:
            parts += 1
            try:
                with opener(url, size) as response:
                    if size and getattr(response, "status", 200) != 206:
                        out.seek(0)  # the server ignored the range and sends the whole file again
                        out.truncate()
                        size = 0
                    announced = total_size(response, size)
                    if total is None:
                        total = announced
                    elif announced is not None and announced != total:
                        raise InputError(f"resume_from: {url} changed size during the download ({total} -> {announced})")
                    while chunk := response.read(CHUNK):
                        out.write(chunk)
                        size += len(chunk)
            except ERRORS:
                pass  # a cut stream: continue from size with the next part
            if total is None:
                break  # no size announced: nothing to continue to, take what came
            if size >= total:
                break
        else:
            raise InputError(f"resume_from: {url} still incomplete after {MAX_PARTS} requests ({size} of {total} bytes)")
    if total is not None and size != total:
        raise InputError(f"resume_from: {url} ended after {size} of {total} bytes")
    if size == 0:
        raise InputError(f"resume_from: {url} is empty")
    sha = hashlib.sha256()
    with open(target, "rb") as f:
        while chunk := f.read(CHUNK):
            sha.update(chunk)
    return size, sha.hexdigest(), parts


def fetch(config: dict, iterations: int, target: Path, build: str | None,
          opener: Opener | None = None, log: Callable[[str], None] = print) -> dict[str, str]:
    """Downloads the snapshots into target/<seed folder>/; returns snapshot path -> origin for the run report."""
    opener = opener or http_open
    qid = config["resume_from"]
    record = get_json(f"{DOIP}/{qid}", opener)
    components = (record.get("kernel") or {}).get("fdo:hasComponent") if isinstance(record, dict) else None
    if not isinstance(components, list):
        raise InputError(f"resume_from: {DOIP}/{qid} is not a run record: {str(record)[:200]}")
    info = get_json(f"{DOIP}/{qid}/output/run_info.json", opener)
    for note in check_source(info, qid, config, build):
        log(f"resume_from: {note}")

    chosen = choose(list_snapshots(components), config["snapshot_at"], qid)
    late = [s for s in chosen if s.day >= iterations]
    if late:
        raise InputError(f"resume_from: the snapshot of day {late[0].day} ({late[0].folder}) lies at or after the end of "
                         f"this run (prediction_length {config['prediction_length']} weeks = {iterations} days)")

    origin = {}
    for s in chosen:
        url = s.url(qid)
        size, sha, parts = download(url, target / s.folder / s.file, opener)
        log(f"resume_from: {s.folder}/{s.file} ({size:,} bytes in {parts} request(s))")
        origin[f"snapshot/{s.folder}/{s.file}"] = f"{url} (run {qid}, {size} bytes, sha256 {sha})"
    return origin
