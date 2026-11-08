"""The pushdown's tuning, and every environment switch the package reads.

:data:`SETTINGS` holds the numbers the planner decides by — the pair budget,
the probe fan-out, the batch and chunk sizes — the switches that narrow what
the pushdown hook answers, and the ablation and debugging switches. It is one
object, read when each decision is made, so every module sees the same
values; a test overrides one for its duration with
``monkeypatch.setattr(SETTINGS, "pair_budget", 0)``.

A switch is read by one parser per kind of value: :func:`flag` for on and off
(``1``, ``true``, ``yes``, ``on``; ``0``, ``false``, ``no``, ``off``; any case),
:func:`positive_int` for a size. Nothing is read on import, so a bad value
fails where it is read, naming the variable:

- each time the pushdown is registered, which constructing a store does
  once: the switches :func:`apply_switches` reads, each of which moves a
  setting away from its default and never back, and
  ``VORTEX_RDF_DISABLE_PUSHDOWN`` (:func:`pushdown_disabled`);
  ``VORTEX_RDF_IN_MEMORY`` (:func:`in_memory_by_default`) and
  ``VORTEX_RDF_DISABLE_CODE_PATH`` (:func:`code_path_enabled`) on
  construction;
- once per query, as it starts: ``VORTEX_RDF_TRACE_QUERY`` and
  ``VORTEX_RDF_TRACE_QUERY_ID`` (:func:`trace_enabled`,
  :func:`trace_query_id`), so a query is traced from its start or not at
  all.

The ablation switches (``VORTEX_RDF_CODE_PREDICATES``,
``VORTEX_RDF_COUNT_ORDER``, ``VORTEX_RDF_DECODE_EACH_STEP``,
``VORTEX_RDF_DECODE_CHUNK``, ``VORTEX_RDF_PROBE_BATCH``) measure what each
pushdown mechanism contributes: each changes how the pushdown answers, never
what (``tests/test_ablation_switches.py``); the fields they set say what
each does, and the README lists them.
"""

import os
from collections.abc import Callable
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

_ON = frozenset({"1", "true", "yes", "on"})
_OFF = frozenset({"0", "false", "no", "off"})


def _environment_reader() -> Callable[[str], str | None]:
    """``os.environ.get``, through the mapping's own dictionary where it keeps
    one (CPython's and PyPy's ``os.environ`` do): constructing a store reads
    every switch, and ``os.environ.get`` of a name that is not set raises
    and catches a ``KeyError`` (about 550 ns, against a 40 ns probe). The
    dictionary is the one ``os.environ`` updates when a variable is set or
    removed through it, so a read sees the environment as it is then."""
    environ = os.environ
    data = getattr(environ, "_data", None)
    encodekey = getattr(environ, "encodekey", None)
    decodevalue = getattr(environ, "decodevalue", None)
    if not isinstance(data, dict) or encodekey is None or decodevalue is None:
        return environ.get

    def read(name: str) -> str | None:
        raw = data.get(encodekey(name))
        return None if raw is None else decodevalue(raw)

    return read


#: A variable of the environment as it is when read, ``None`` when unset.
getenv = _environment_reader()


def flag(name: str, default: bool) -> bool:
    """The environment variable ``name`` as on or off: ``1``, ``true``,
    ``yes`` or ``on`` is on, ``0``, ``false``, ``no`` or ``off`` off, in any
    case and with surrounding blanks; unset or empty is ``default``; anything
    else raises ``ValueError``, naming the variable."""
    value = (getenv(name) or "").strip().lower()
    if not value:
        return default
    if value in _ON:
        return True
    if value in _OFF:
        return False
    raise ValueError(f"{name} must be 1/true/yes/on or 0/false/no/off, got {value!r}")


def _int_at_least(name: str, minimum: int) -> int | None:
    """The environment variable ``name`` as an integer of at least
    ``minimum``, written in ASCII digits (surrounding blanks aside), or
    ``None`` when unset or empty; anything else — a sign, an underscore,
    another script's digits — raises ``ValueError``, naming the variable."""
    value = (getenv(name) or "").strip()
    if not value:
        return None
    if not (value.isascii() and value.isdigit()) or int(value) < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {value!r}")
    return int(value)


def positive_int(name: str) -> int | None:
    """The environment variable ``name`` as a positive integer, or ``None``
    when unset or empty; anything else raises ``ValueError``, naming it."""
    return _int_at_least(name, 1)


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
    #: Kind tests (``isIRI``, ``isLiteral``, ``isBlank``) and IRI equality
    #: are answered as code ranges and code sets (``VORTEX_RDF_CODE_PREDICATES=0``
    #: sends them through decoded values, an ablation).
    code_predicates: bool = True
    #: Patterns are ordered by native counts (``VORTEX_RDF_COUNT_ORDER=0``
    #: orders them by their bound positions alone and probes every connected
    #: step, as rdflib's nested loop does: an ablation).
    count_order: bool = True
    #: After every join step the relation's codes are decoded to spellings and
    #: encoded again (``VORTEX_RDF_DECODE_EACH_STEP=1``: the term round trip a
    #: term-space evaluator pays, an ablation).
    decode_each_step: bool = False
    #: A store counts every call through its native handle and dictionary
    #: (``VORTEX_RDF_COUNTERS=1``, read when the store is opened; see
    #: :mod:`vortex_rdflib.counters`).
    counters: bool = False
    #: ``triples()`` prints each pattern it is asked (``VORTEX_RDF_TRACE_TRIPLES=1``,
    #: debugging).
    trace_triples: bool = False

    #: The most row pairs one join step of a BGP may make. A step whose two
    #: sides' product is within it is built uncounted; past that, its pairs
    #: are counted natively before any is built, and a step that counts more
    #: is not built. A probe step, which has no pairs to count, stops once
    #: its rows pass a quarter of the budget; a keep-scan, once its match
    #: passes the budget. ``VORTEX_RDF_PAIR_BUDGET``: an integer of at least 0.
    pair_budget: int = 1 << 24
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
    return flag("VORTEX_RDF_DISABLE_PUSHDOWN", False)


#: The on/off switches :func:`apply_switches` reads: each variable, the
#: :class:`Settings` field it sets and that field's default, which the switch
#: moves it away from.
_FLAGS = (
    ("VORTEX_RDF_FILTER_FAST", "fast_filters", True),
    ("VORTEX_RDF_NATIVE_FILTERS", "native_filters", True),
    ("VORTEX_RDF_CODE_PREDICATES", "code_predicates", True),
    ("VORTEX_RDF_COUNT_ORDER", "count_order", True),
    ("VORTEX_RDF_DECODE_EACH_STEP", "decode_each_step", False),
    ("VORTEX_RDF_COUNTERS", "counters", False),
    ("VORTEX_RDF_TRACE_TRIPLES", "trace_triples", False),
)


def apply_switches(target: Settings | None = None) -> None:
    """Set ``target`` (:data:`SETTINGS`) by the switches set in the
    environment: ``VORTEX_RDF_PUSHDOWN_OPS`` (:func:`parse_ops`), the on/off
    switches (:data:`_FLAGS`), the pair budget and the decode and probe
    batch sizes. A switch moves its settings away from their defaults and
    never back, and one left unset changes nothing. Every switch is read
    before any setting changes, so a value refused leaves them all as they
    were."""
    target = SETTINGS if target is None else target
    spec = getenv(PUSHDOWN_OPS_ENV)
    ops = None if spec is None else parse_ops(spec)
    flags = [(field, flag(env, default), default) for env, field, default in _FLAGS]
    pair_budget = _int_at_least(PAIR_BUDGET_ENV, 0)
    decode_chunk = positive_int("VORTEX_RDF_DECODE_CHUNK")
    probe_batch = positive_int("VORTEX_RDF_PROBE_BATCH")
    if ops is not None:
        target.enabled_ops = ops
    for field_name, value, default in flags:
        if value != default:
            setattr(target, field_name, value)
    if pair_budget is not None:
        target.pair_budget = pair_budget
    if decode_chunk is not None:  # one fixed chunk size
        target.chunk_start = target.chunk_max = decode_chunk
    if probe_batch is not None:  # this many probes a call, every step batched
        target.probe_batch = probe_batch
        target.cheap_batch_min = target.scan_batch_min = target.memory_scan_batch_min = 1


def in_memory_by_default() -> bool:
    """``VORTEX_RDF_IN_MEMORY=1``: a store constructed without ``in_memory``
    loads the whole file up front (for benchmark sweeps)."""
    return flag("VORTEX_RDF_IN_MEMORY", False)


def code_path_enabled() -> bool:
    """Whether a store reads term codes (``VORTEX_RDF_DISABLE_CODE_PATH=1``
    keeps it on the N-Triples string columns)."""
    return not flag("VORTEX_RDF_DISABLE_CODE_PATH", False)


def trace_enabled() -> bool:
    """``VORTEX_RDF_TRACE_QUERY=1`` prints one JSON line per planning step to
    stderr."""
    return flag(TRACE_ENV, False)


def trace_query_id() -> str | None:
    """``VORTEX_RDF_TRACE_QUERY_ID``: the ``query_id`` a trace's events carry."""
    return getenv("VORTEX_RDF_TRACE_QUERY_ID")
