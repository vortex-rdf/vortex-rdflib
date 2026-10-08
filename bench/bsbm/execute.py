"""Run a BSBM stream against one store, as the official driver runs it.

Shared by ``run_stream`` (one vortex store, paired comparisons) and the
dashboard worker (``bench.worker --bsbm``, any store). The warm-up runs first,
untimed. Every measured instance runs once, timed as two adjacent spans like
``bench.worker.run_once``: ``prepareQuery``, then evaluate-and-consume; a store
taking query text (``native``: pyoxigraph) has one span. Answers are digested
outside the timing. RssAnon is sampled after every measured mix.

Limits, off unless given: a per-query timeout (SIGALRM via ``setitimer``,
raising ``QueryTimeout`` once Python regains control; POSIX only), and a store
budget over the measured mixes, past which the store finishes its mix and
stops (``partial``). An instance timed out when its deadline fired, whatever
the store made of the interrupt. Imports only the standard library at module
level.
"""

import gc
import hashlib
import math
import os
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import groupby
from time import perf_counter_ns

from ..procmem import rss_anon_mb

QUERY_TIMEOUT_ENV = "BSBM_QUERY_TIMEOUT_S"
STORE_BUDGET_ENV = "BSBM_STORE_BUDGET_S"
DEFAULT_QUERY_TIMEOUT_S = 5.0  # the dashboard's (CI, refresh.sh); run_stream has none by default
DEFAULT_STORE_BUDGET_S = 300.0


class QueryTimeout(BaseException):
    """A BaseException, like KeyboardInterrupt, so no store's or rdflib's
    ``except Exception`` swallows it."""


def limit(seconds: float | None) -> float | None:
    """A limit in seconds, or None for no limit: 0, a negative value, ``inf``
    and ``nan`` all turn it off."""
    return seconds if seconds is not None and math.isfinite(seconds) and seconds > 0 else None


def limit_from_env(name: str, default: float) -> float | None:
    value = os.environ.get(name, "").strip()
    return limit(float(value) if value else default)


class Deadline:
    """What ``deadline`` yields: ``expired`` once its time has run out."""

    def __init__(self) -> None:
        self.expired = False


@contextmanager
def deadline(seconds: float | None) -> Iterator[Deadline]:
    """Raise ``QueryTimeout`` in the block after ``seconds``, and mark the
    yielded ``Deadline`` expired: a store that catches the interrupt and raises
    an error of its own, or swallows it, cannot hide that the time ran out.
    Never expires when ``seconds`` is None or 0, off the main thread, and
    without ``setitimer`` (Windows). The previous SIGALRM handler is back, and
    the timer disarmed, however the block ends."""
    state = Deadline()
    if (
        not seconds
        or not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield state
        return

    def expire(signum: int, frame: object) -> None:
        state.expired = True
        raise QueryTimeout(f"over {seconds:g} s")

    previous = signal.signal(signal.SIGALRM, expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)  # in here: a refused value still restores
        yield state
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous if previous is not None else signal.SIG_DFL)


#: RDF 1.1 makes ``"lex"^^xsd:string`` and ``"lex"`` one term, and stores spell it
#: either way (rdflib Memory keeps an explicit datatype, vortex returns the simple
#: literal), so the digest drops that datatype.
_XSD_STRING = "^^<http://www.w3.org/2001/XMLSchema#string>"


def answer_digest(rows: list) -> str:
    """An answer's identity, row order aside: its terms in N3, ``xsd:string``
    literals as simple literals, sorted (rdflib terms)."""
    if rows and isinstance(rows[0], bool):
        return hashlib.sha1(repr(rows).encode()).hexdigest()[:16]
    canon = sorted(
        tuple("UNDEF" if term is None else term.n3().removesuffix(_XSD_STRING) for term in row)
        for row in rows
    )
    return hashlib.sha1(repr(canon).encode()).hexdigest()[:16]


def run_instance(
    graph,
    text: str,
    *,
    native: bool = False,
    query_kwargs: dict | None = None,
    init_ns: dict | None = None,
) -> tuple[list, int | None, int]:
    """One execution, consumed to the last value: (rows, prepare ns, evaluate ns)."""
    kwargs = query_kwargs or {}
    if native:
        t0 = perf_counter_ns()
        result = graph.query(text, **kwargs)
        try:
            items = iter(result)
        except TypeError:  # an ASK answer is a boolean
            rows = [bool(result)]
        else:
            rows = [tuple(item) for item in items]  # every value read, as rdflib rows hold them
        return rows, None, perf_counter_ns() - t0
    from rdflib.plugins.sparql import prepareQuery  # here: a native run never loads rdflib

    t0 = perf_counter_ns()
    prepared = prepareQuery(text, initNs=init_ns or {})
    t1 = perf_counter_ns()
    result = graph.query(prepared, **kwargs)
    rows = [result.askAnswer] if result.type == "ASK" else list(result)
    return rows, t1 - t0, perf_counter_ns() - t1


def _attempt(
    graph, text: str, native: bool, kwargs: dict, init_ns: dict, timeout_s: float | None
) -> tuple[dict, int]:
    """One instance, never raising: its record and the time it took.

    It timed out when its deadline fired, whatever the store did next: raised
    ``QueryTimeout``, raised an error of its own (DuckDB, under pycottas, turns
    the interrupt into ``RuntimeError: Query interrupted``), or swallowed it
    and answered (rdflib's bare ``except:`` blocks can). A timed-out or failed
    instance's time is its elapsed time, up to where it stopped. The digest is
    taken outside the timing, inside the guard."""
    t0 = perf_counter_ns()
    timer = Deadline()  # stays unexpired when the deadline cannot be armed
    error = None
    try:
        with deadline(timeout_s) as timer:
            rows, prep_ns, exec_ns = run_instance(
                graph, text, native=native, query_kwargs=kwargs, init_ns=init_ns
            )
        if not timer.expired:
            record = {
                "prep_ns": prep_ns,
                "exec_ns": exec_ns,
                "rows": len(rows),
                "digest": None if native else answer_digest(rows),
            }
            return record, (prep_ns or 0) + exec_ns
    except QueryTimeout:
        pass
    except Exception as failure:  # noqa: BLE001 — one instance must not sink the stream
        error = f"{type(failure).__name__}: {failure}"
    elapsed = perf_counter_ns() - t0
    if error is not None and not timer.expired:
        return {"error": error, "elapsed_ns": elapsed}, elapsed
    return {"timeout": True, "elapsed_ns": elapsed}, elapsed


def execute_stream(
    graph,
    warmup: list[dict],
    measured: list[dict],
    *,
    native: bool = False,
    query_kwargs: dict | None = None,
    query_timeout_s: float | None = None,
    store_budget_s: float | None = None,
    collect_each: bool = False,
) -> dict:
    """Run ``warmup`` untimed, then ``measured`` mix by mix (module docstring).
    A mix's time is every instance's elapsed time, a timed-out one's up to the
    abort, as the official driver counts it."""
    kwargs = query_kwargs or {}
    init_ns = {} if native else dict(graph.namespaces())
    warm_timeouts = warm_errors = 0
    t0 = perf_counter_ns()
    for q in warmup:
        record, _ns = _attempt(graph, q["text"], native, kwargs, init_ns, query_timeout_s)
        warm_timeouts += 1 if record.get("timeout") else 0
        warm_errors += 1 if "error" in record else 0
    warmup_ns = perf_counter_ns() - t0
    gc.collect()
    results: list[dict] = []
    mix_ns: list[int] = []
    rss: list[int | None] = []
    planned = len({q["mix"] for q in measured})
    started = perf_counter_ns()
    for _mix, group in groupby(measured, key=lambda q: q["mix"]):
        spent = 0
        for q in group:
            if collect_each:
                gc.collect()
            record, ns = _attempt(graph, q["text"], native, kwargs, init_ns, query_timeout_s)
            results.append({"q": q["q"], "i": q["i"], "mix": q["mix"], **record})
            spent += ns
        mix_ns.append(spent)
        rss.append(rss_anon_mb())
        if store_budget_s and perf_counter_ns() - started > store_budget_s * 1e9:
            break
    readings = [mb for mb in rss if mb is not None]
    return {
        "warmup_ns": warmup_ns,
        "warmup_timeouts": warm_timeouts,
        "warmup_errors": warm_errors,
        "results": results,
        "mixes_planned": planned,
        "mixes_done": len(mix_ns),
        "partial": len(mix_ns) < planned,
        "mix_ns": mix_ns,
        "rss_anon_per_mix_mb": rss,
        "peak_anon_mb": max(readings) if readings else None,
    }
