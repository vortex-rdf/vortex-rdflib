"""The pushdown's tuning, and every environment switch the package reads.

:data:`SETTINGS` holds the numbers the planner decides by — the pair budget,
the probe fan-out, the batch and chunk sizes — and the switches that narrow
what the pushdown hook answers. It is one object, read when each decision is
made, so every module sees the same values; a test overrides one for its
duration with ``monkeypatch.setattr(SETTINGS, "pair_budget", 0)``.

The environment is read at four points, each named by a function here:

- on import: ``VORTEX_RDF_PAIR_BUDGET``, the pair budget's default;
- each time the pushdown is registered (every store construction):
  ``VORTEX_RDF_DISABLE_PUSHDOWN`` (:func:`pushdown_disabled`) and the switches
  :func:`apply_switches` reads, each of which narrows a setting and never
  widens it back;
- each time a store is constructed: ``VORTEX_RDF_IN_MEMORY``
  (:func:`in_memory_by_default`) and ``VORTEX_RDF_DISABLE_CODE_PATH``
  (:func:`code_path_enabled`);
- per call: ``VORTEX_RDF_TRACE_QUERY`` and ``VORTEX_RDF_TRACE_QUERY_ID``
  (:func:`trace_enabled`, :func:`trace_query_id`), for each algebra node the
  hook answers outside a traced query, and ``VORTEX_RDF_TRACE_TRIPLES``
  (:func:`trace_triples`), for each ``triples()`` call.
"""

import os
from dataclasses import dataclass, field

#: The algebra nodes the pushdown hook answers, by rdflib's name for them:
#: the values ``VORTEX_RDF_PUSHDOWN_OPS`` may list.
ALL_OPS = frozenset(
    {
        "BGP",
        "Filter",
        "Join",
        "LeftJoin",
        "Minus",
        "ToMultiSet",
        "Graph",
        "Project",
        "Slice",
        "Distinct",
        "OrderBy",
        "AggregateJoin",
        "AskQuery",
    }
)

PAIR_BUDGET_ENV = "VORTEX_RDF_PAIR_BUDGET"
PUSHDOWN_OPS_ENV = "VORTEX_RDF_PUSHDOWN_OPS"
TRACE_ENV = "VORTEX_RDF_TRACE_QUERY"


def _pair_budget_from_env() -> int:
    """``VORTEX_RDF_PAIR_BUDGET``, an integer of at least 0, or ``1 << 24``
    when it is unset."""
    value = os.environ.get(PAIR_BUDGET_ENV)
    if value is None:
        return 1 << 24
    try:
        budget = int(value)
    except ValueError:
        budget = None
    if budget is None or budget < 0:
        raise ValueError(f"{PAIR_BUDGET_ENV} must be an integer >= 0, got {value!r}")
    return budget


def _scan_lookahead() -> int:
    return min(os.cpu_count() or 1, 8)


@dataclass(slots=True)
class Settings:
    """What the pushdown decides by. See the module docstring for when the
    environment sets a field."""

    #: The algebra nodes the hook answers (``VORTEX_RDF_PUSHDOWN_OPS``).
    enabled_ops: frozenset[str] = ALL_OPS
    #: FILTER values go through the fast route over parsed spellings first
    #: (``VORTEX_RDF_FILTER_FAST=0`` sends every one to rdflib's evaluator;
    #: the equivalence tests run the matrix both ways).
    fast_filters: bool = True
    #: Conjuncts with a native shape are decided inside vortex-rdf: kind
    #: tests as code ranges, IRI equalities as code sets, the others over the
    #: codes a query produced (``VORTEX_RDF_NATIVE_FILTERS=0`` keeps them on
    #: the Python routes, for A/B measurements; turning the fast route off
    #: turns these off too).
    native_filters: bool = True

    #: The most row pairs one join step of a BGP may make. A step whose two
    #: sides' product is within it is built uncounted; past that, its pairs
    #: are counted natively before any is built, and a step that counts more
    #: is not built. A probe step, which has no pairs to count, stops once
    #: its rows pass a quarter of the budget; a keep-scan, once its match
    #: passes the budget. ``VORTEX_RDF_PAIR_BUDGET``, read on import.
    pair_budget: int = field(default_factory=_pair_budget_from_env)
    #: Probing beats hash-joining when the running relation is at least this
    #: many times smaller than the next pattern's match. The value trades the
    #: fixed cost of a native match, which does not depend on how selective
    #: it is, against the cost of materializing a matched row into Python:
    #: one probe is worth skipping that for this many rows.
    probe_fanout: int = 100
    #: The most rows of its fed side a streamed join step takes at a time: a
    #: slice of a side it holds, or a chunk of a match it reads in chunks. A
    #: piece over the pair budget is halved before it is gathered.
    piece_rows: int = 65_536

    #: Probes per native batch call: bounds the columns one batch holds.
    probe_batch: int = 4096
    #: A batch call (``count_quads_many``/``match_codes_many``) pays a fixed
    #: cost of native task scheduling before its probes run concurrently.
    #: That pays off for probes that scan the store, but not for a handful of
    #: point lookups, which plain calls in a loop answer in less time than
    #: the batch costs. Point lookups are batched only on a file-backed store
    #: and from this many on, where overlapping their reads wins.
    cheap_batch_min: int = 256
    #: Scanning probes batch from two on a file-backed store, whose scans
    #: overlap their reads.
    scan_batch_min: int = 2
    #: An in-memory scan is pure CPU, so overlapping two of them gains less
    #: than the batch costs; they batch from three on.
    memory_scan_batch_min: int = 3
    #: The ``lookahead`` of a batch call whose probes scan the store: a scan
    #: per core, eight at most. The native layer runs at most twice that many
    #: probes ahead of the caller's pulls. A batch of point lookups names
    #: none and runs under the binding's default.
    scan_lookahead: int = field(default_factory=_scan_lookahead)

    #: Rows are read and decoded in slices that grow geometrically, from
    #: ``chunk_start`` rows by four times up to ``chunk_max``: the first keeps
    #: a LIMIT (or any consumer that stops early) from decoding more than a
    #: few dozen rows, the cap keeps a full scan to a handful of
    #: ``decode_many`` calls.
    chunk_start: int = 64
    chunk_max: int = 4096
    #: Groups up to which COUNT(DISTINCT) gathers each group's rows natively
    #: (one ``join_indices`` over the key column per group); past it, one
    #: pass over the distinct (key, value) pairs.
    native_groups_max: int = 64
    #: The most rows ``VortexRdflibStore.triples`` reads as one match: a
    #: pattern that matches more is read through ``match_code_chunks`` in
    #: chunks of at most this many rows, so a scan of a large store holds one
    #: chunk of codes, not its whole match.
    triples_chunk_rows: int = 65_536


#: The settings every module reads.
SETTINGS = Settings()


def parse_ops(spec: str) -> frozenset[str]:
    """``VORTEX_RDF_PUSHDOWN_OPS``: ``bgp`` or a comma-separated node list."""
    if spec.strip().lower() == "bgp":
        return frozenset({"BGP"})
    names = frozenset(name.strip() for name in spec.split(",") if name.strip())
    unknown = names - ALL_OPS
    if unknown:
        raise ValueError(
            f"{PUSHDOWN_OPS_ENV} names unknown algebra nodes {sorted(unknown)}; "
            f"known: {sorted(ALL_OPS)}"
        )
    return names


def pushdown_disabled() -> bool:
    """``VORTEX_RDF_DISABLE_PUSHDOWN=1`` keeps rdflib's evaluator entirely
    (the equivalence tests' oracle)."""
    return os.environ.get("VORTEX_RDF_DISABLE_PUSHDOWN") == "1"


def apply_switches() -> None:
    """Narrow :data:`SETTINGS` by the switches set in the environment:
    ``VORTEX_RDF_PUSHDOWN_OPS`` (:func:`parse_ops`), ``VORTEX_RDF_FILTER_FAST=0``
    and ``VORTEX_RDF_NATIVE_FILTERS=0``. A switch left unset changes nothing,
    and a list the parser refuses raises before any setting changes."""
    spec = os.environ.get(PUSHDOWN_OPS_ENV)
    ops = None if spec is None else parse_ops(spec)
    if ops is not None:
        SETTINGS.enabled_ops = ops
    if os.environ.get("VORTEX_RDF_FILTER_FAST") == "0":
        SETTINGS.fast_filters = False
    if os.environ.get("VORTEX_RDF_NATIVE_FILTERS") == "0":
        SETTINGS.native_filters = False


def in_memory_by_default() -> bool:
    """``VORTEX_RDF_IN_MEMORY=1``: a store constructed without ``in_memory``
    loads the whole file up front (for benchmark sweeps)."""
    return os.environ.get("VORTEX_RDF_IN_MEMORY") == "1"


def code_path_enabled() -> bool:
    """Whether a store reads term codes (``VORTEX_RDF_DISABLE_CODE_PATH=1``
    keeps it on the N-Triples string columns)."""
    return os.environ.get("VORTEX_RDF_DISABLE_CODE_PATH") != "1"


def trace_enabled() -> bool:
    """``VORTEX_RDF_TRACE_QUERY``: ``1`` prints one JSON line per planning
    step to stderr, ``0`` or unset prints none; anything else is refused."""
    value = os.environ.get(TRACE_ENV)
    if value is None or value == "0":
        return False
    if value != "1":
        raise ValueError(f"{TRACE_ENV} must be 0 or 1, got {value!r}")
    return True


def trace_query_id() -> str | None:
    """``VORTEX_RDF_TRACE_QUERY_ID``: the ``query_id`` a trace's events carry."""
    return os.environ.get("VORTEX_RDF_TRACE_QUERY_ID")


def trace_triples() -> bool:
    """``VORTEX_RDF_TRACE_TRIPLES=1`` prints each ``triples()`` call's pattern
    to stdout."""
    return os.environ.get("VORTEX_RDF_TRACE_TRIPLES") == "1"
