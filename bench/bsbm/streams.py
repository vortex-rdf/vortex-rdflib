"""BSBM query streams: the official mix order and the driver's queries, split.

A stream is a JSON list of ``{"mix","pos","q","i","text"}`` in execution order:
``mix`` numbers its mixes from 0, ``pos`` is the place in the mix, ``q`` the
official template, ``i`` numbers its queries from 0. ``prepare`` writes
``warmup.json`` and ``measured.json``; the driver's warm-up mixes come first in
what it sends and are split off by count. The readers below are what every
consumer of a prepared directory uses: ``meta.json`` names the dataset, inside
the directory or (``prepare --from``) elsewhere by absolute path.
"""

import json
import re
from pathlib import Path

#: ``queries/explore/querymix.txt`` of the pinned tools, verbatim: Q6 is not in
#: it and Q5 runs twice. ``prepare`` reads the file; this copy is for tests.
EXPLORE_QUERYMIX = "1 2 2 3 2 2 4 2 2 5 7 7 5 7 7 8 9 9 8 9 9 10 10 11 12"
EXPLORE_MIX: tuple[int, ...] = tuple(int(q) for q in EXPLORE_QUERYMIX.split())
PLACEHOLDER = re.compile(r"%[A-Za-z][A-Za-z0-9_]*%")


def read_mix(querymix_dir: str | Path) -> list[int]:
    """The templates the driver sends per mix: ``querymix.txt`` less ``ignoreQueries.txt``."""
    root = Path(querymix_dir)
    order = [int(t) for t in (root / "querymix.txt").read_text(encoding="ascii").split()]
    ignore = root / "ignoreQueries.txt"
    ignored = (
        {int(t) for t in ignore.read_text(encoding="ascii").split()} if ignore.is_file() else set()
    )
    return [q for q in order if q not in ignored]


def split_streams(
    texts: list[str], mix: list[int], warmup_mixes: int, mixes: int
) -> tuple[list[dict], list[dict]]:
    """Warm-up and measured streams; refuses a capture of the wrong length or a
    query still holding a ``%placeholder%`` (named), which no store must receive."""
    per_mix = len(mix)
    expected = per_mix * (warmup_mixes + mixes)
    if len(texts) != expected:
        raise ValueError(
            f"captured {len(texts)} queries, expected {expected} "
            f"({warmup_mixes} warm-up + {mixes} measured mixes of {per_mix})"
        )
    for k, text in enumerate(texts):
        left = sorted(set(PLACEHOLDER.findall(text)))
        if left:
            raise ValueError(f"query {k} (Q{mix[k % per_mix]}) left {', '.join(left)} unfilled")
    cut = per_mix * warmup_mixes
    return _stream(texts[:cut], mix), _stream(texts[cut:], mix)


def _stream(texts: list[str], mix: list[int]) -> list[dict]:
    n = len(mix)
    return [
        {"mix": k // n, "pos": k % n, "q": mix[k % n], "i": k, "text": text}
        for k, text in enumerate(texts)
    ]


def read_meta(prepared: str | Path) -> dict:
    return json.loads((Path(prepared) / "meta.json").read_text(encoding="utf-8"))


def dataset_path(prepared: str | Path, meta: dict) -> Path:
    return Path(prepared) / meta["dataset"]["path"]  # an absolute path replaces the directory


def td_data_path(prepared: str | Path, meta: dict) -> Path:
    return Path(prepared) / meta["dataset"]["td_data"]


def load_streams(prepared: str | Path) -> tuple[list[dict], list[dict]]:
    root = Path(prepared)
    return (
        json.loads((root / "warmup.json").read_text(encoding="utf-8")),
        json.loads((root / "measured.json").read_text(encoding="utf-8")),
    )
