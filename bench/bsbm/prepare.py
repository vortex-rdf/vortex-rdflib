"""Prepare a BSBM run with the official tools: data, parameter pools, streams.

usage: python -m bench.bsbm.prepare (--products N | --from DIR) --warmup-mixes W
           --mixes M [--seed 808080] [--only-query Q] --out DIR

Writes into DIR: ``dataset.nt`` and ``td_data/`` (``./generate -pc N -s nt``, no
``-fc``, deterministic per N); ``warmup.json`` and ``measured.json`` (what the
unmodified test driver sends for W warm-up and M measured mixes with this seed,
808080 being its own default, captured by ``bench.bsbm.capture``); and
``meta.json`` (tools commit, products, seed, mixes, mix order, triple count,
SHA-256, templates), written last so an interrupted run is never a cache hit.

``--only-query Q``: a use case of the official template Q alone (e.g. 6, the
regex query the official mix leaves out). ``--from DIR``: reuse DIR's dataset
and td_data (no regeneration, no copy: meta.json names them; symlinks where the
platform allows) and only capture streams; products come from DIR's meta.json.
The same parameters again are a cache hit; the same dataset with other stream
parameters reuses the dataset and captures again.
"""

import argparse
import contextlib
import hashlib
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import streams, tools
from .capture import CaptureServer
from .tools import ToolsError

DRIVER_SEED = 808080  # TestDriverDefaultValues.seed
SCHEMA = 1


def stream_params(
    products: int, seed: int, warmup_mixes: int, mixes: int, only_query: int | None
) -> dict:
    return {
        "tools": tools.TOOLS_COMMIT,
        "products": products,
        "seed": seed,
        "warmup_mixes": warmup_mixes,
        "mixes": mixes,
        "only_query": only_query,
    }


def scan(path: Path) -> dict:
    """Line (= triple) count, size and SHA-256 in one pass."""
    digest, lines, size = hashlib.sha256(), 0, 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 22):
            digest.update(chunk)
            lines += chunk.count(b"\n")
            size += len(chunk)
    return {"triples": lines, "bytes": size, "sha256": digest.hexdigest()}


def write_meta(
    out: Path,
    params: dict,
    dataset: dict,
    mix: list[int],
    templates: dict[int, str],
    counts: tuple[int, int],
) -> dict:
    meta = {
        "schema": SCHEMA,
        "params": params,
        "tools": {
            "repo": tools.TOOLS_REPO,
            "commit": tools.TOOLS_COMMIT,
            "sha256": tools.TOOLS_SHA256,
        },
        "products": params["products"],
        "seed": params["seed"],
        "warmup_mixes": params["warmup_mixes"],
        "mixes": params["mixes"],
        "only_query": params["only_query"],
        "mix": mix,
        "dataset": dataset,
        "templates": {str(q): text for q, text in sorted(templates.items())},
        "queries": {"warmup": counts[0], "measured": counts[1]},
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return meta


def capture_streams(
    tools_dir: Path,
    td_data: Path,
    *,
    warmup_mixes: int,
    mixes: int,
    seed: int,
    only_query: int | None,
) -> tuple[list[str], list[int], dict[int, str]]:
    """The driver's queries as the capture endpoint received them, the mix they
    follow, and the official templates they fill."""
    with tempfile.TemporaryDirectory(prefix="bsbm-capture-") as tmp:
        work = Path(tmp)
        if only_query is None:
            usecase, mix_dir = tools.official_usecase(tools_dir)
        else:
            usecase, mix_dir = tools.single_query_usecase(tools_dir, only_query, work)
        mix = streams.read_mix(mix_dir)
        with CaptureServer() as server:
            tools.run_testdriver(
                tools_dir,
                server.url,
                td_data=td_data,
                warmup_mixes=warmup_mixes,
                mixes=mixes,
                seed=seed,
                usecase=usecase,
                work_dir=work,
            )
        if server.missing:
            raise ToolsError(f"{server.missing} requests of the test driver carried no query")
        texts = list(server.queries)
    explore = tools_dir / "queries" / "explore"
    templates = {
        q: (explore / f"query{q}.txt").read_text(encoding="utf-8") for q in sorted(set(mix))
    }
    return texts, mix, templates


def _read_meta(out: Path) -> dict | None:
    try:
        return streams.read_meta(out)
    except (OSError, ValueError):
        return None


def _dataset_ready(base: Path, meta: dict) -> bool:
    """Whether meta's dataset is on disk as it was when meta was written."""
    try:
        data = streams.dataset_path(base, meta)
        return (
            data.is_file()
            and data.stat().st_size == meta["dataset"]["bytes"]
            and streams.td_data_path(base, meta).is_dir()
        )
    except (KeyError, TypeError, OSError):
        return False


def _referenced_dataset(source: Path, source_meta: dict, out: Path) -> dict:
    """``--from``: the source's dataset by absolute path, never copied."""
    data = streams.dataset_path(source, source_meta).resolve()
    td_data = streams.td_data_path(source, source_meta).resolve()
    for link, target in ((out / "dataset.nt", data), (out / "td_data", td_data)):
        if link.is_symlink():
            link.unlink()
        if not link.exists():
            with contextlib.suppress(OSError):  # e.g. Windows: meta.json still names the source
                link.symlink_to(target, target_is_directory=target.is_dir())
    entry = source_meta["dataset"]
    return {
        "path": str(data),
        "td_data": str(td_data),
        "from": str(source.resolve()),
        "triples": entry["triples"],
        "bytes": entry["bytes"],
        "sha256": entry["sha256"],
    }


def prepare(
    out: Path,
    *,
    products: int,
    seed: int,
    warmup_mixes: int,
    mixes: int,
    only_query: int | None,
    source: Path | None = None,
    source_meta: dict | None = None,
) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    params = stream_params(products, seed, warmup_mixes, mixes, only_query)
    current = _read_meta(out)
    streams_ready = (out / "warmup.json").is_file() and (out / "measured.json").is_file()
    if (
        current is not None
        and current.get("schema") == SCHEMA
        and current.get("params") == params
        and streams_ready
        and _dataset_ready(out, current)
    ):
        print(f"cache hit: {out} already holds these streams", file=sys.stderr)
        return current
    dataset: dict | None = None
    if source is not None and source_meta is not None:
        same_tools = source_meta.get("params", {}).get("tools") == tools.TOOLS_COMMIT
        if not (same_tools and _dataset_ready(source, source_meta)):
            raise ToolsError(f"--from {source}: its dataset is missing or made by other tools")
        dataset = _referenced_dataset(source, source_meta, out)
    elif (
        current is not None
        and current.get("products") == products
        and current.get("params", {}).get("tools") == tools.TOOLS_COMMIT
        and _dataset_ready(out, current)
    ):
        dataset = current["dataset"]
        print(f"reusing the {products:,}-product dataset in {out}", file=sys.stderr)
    tools.require_runtime()
    tools_dir = tools.fetch_tools()
    if dataset is None:
        (out / "meta.json").unlink(missing_ok=True)  # the old dataset is about to be replaced
        print(f"generating {products:,} products (official generator) -> {out}", file=sys.stderr)
        for link in (out / "dataset.nt", out / "td_data"):
            if link.is_symlink():  # a --from run left links into another directory
                link.unlink()
        reported = tools.generate(tools_dir, products, out)
        dataset = {
            "path": "dataset.nt",
            "td_data": "td_data",
            "from": None,
            "reported_triples": reported,
            **scan(out / "dataset.nt"),
        }
    print(
        f"capturing {warmup_mixes} warm-up + {mixes} measured mixes, seed {seed} "
        "(official test driver)",
        file=sys.stderr,
    )
    texts, mix, templates = capture_streams(
        tools_dir,
        streams.td_data_path(out, {"dataset": dataset}),
        warmup_mixes=warmup_mixes,
        mixes=mixes,
        seed=seed,
        only_query=only_query,
    )
    try:
        warmup, measured = streams.split_streams(texts, mix, warmup_mixes, mixes)
    except ValueError as error:
        raise ToolsError(
            f"the driver's queries do not make the streams asked for: {error}"
        ) from error
    (out / "meta.json").unlink(missing_ok=True)  # no cache hit on streams half rewritten
    (out / "warmup.json").write_text(json.dumps(warmup), encoding="utf-8")
    (out / "measured.json").write_text(json.dumps(measured), encoding="utf-8")
    return write_meta(out, params, dataset, mix, templates, (len(warmup), len(measured)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--products", type=int, help="the generator's -pc")
    parser.add_argument(
        "--from", dest="source", type=Path, help="reuse this prepared directory's dataset"
    )
    parser.add_argument("--warmup-mixes", type=int, required=True)
    parser.add_argument("--mixes", type=int, required=True)
    parser.add_argument("--seed", type=int, default=DRIVER_SEED)
    parser.add_argument("--only-query", type=int, help="a mix of this official template alone")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    source_meta = None
    if args.source is not None:
        if args.source.resolve() == args.out.resolve():
            parser.error("--from and --out name the same directory")
        source_meta = _read_meta(args.source)
        if source_meta is None:
            parser.error(f"--from {args.source}: no readable meta.json")
        if args.products is not None and args.products != source_meta["products"]:
            parser.error(
                f"--products {args.products} disagrees with --from {args.source} "
                f"({source_meta['products']} products)"
            )
        products = int(source_meta["products"])
    elif args.products is None:
        parser.error("give --products N, or --from DIR to reuse a prepared dataset")
    else:
        products = args.products
    if products < 1 or args.mixes < 1 or args.warmup_mixes < 0:
        parser.error("--products and --mixes must be positive, --warmup-mixes not negative")
    try:
        meta = prepare(
            args.out,
            products=products,
            seed=args.seed,
            warmup_mixes=args.warmup_mixes,
            mixes=args.mixes,
            only_query=args.only_query,
            source=args.source,
            source_meta=source_meta,
        )
    except ToolsError as error:
        print(f"prepare: {error}", file=sys.stderr)
        return 1
    print(
        f"products={meta['products']} triples={meta['dataset']['triples']}; wrote "
        f"{meta['warmup_mixes']} warm-up and {meta['mixes']} measured mixes "
        f"({len(meta['mix'])} queries per mix, seed {meta['seed']}) -> {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
