"""Ablation switches, for measuring what each pushdown mechanism contributes. Each is an environment variable read
once, when this module is imported; with none set the evaluator is exactly the default one.

- ``VORTEX_RDF_CODE_PREDICATES=0``: kind tests (``isIRI``, ``isLiteral``, ``isBlank``) and IRI equality / ``IN`` /
  ``sameTerm`` with an IRI are not answered as code ranges or code sets; they go through decoded values.
- ``VORTEX_RDF_COUNT_ORDER=0``: no native counts for planning. Patterns are ordered by their bound positions (ties
  as :func:`pushdown._pattern_static_rank` breaks them) and every connected step probes, as rdflib's nested loop does.
- ``VORTEX_RDF_DECODE_EACH_STEP=1``: after every join step the relation's codes are decoded to spellings and encoded
  again, so each step pays the term round trip a term-space evaluator would.
- ``VORTEX_RDF_DECODE_CHUNK=<n>``: every decode chunk is ``n`` rows (default 64, growing to 4,096).
- ``VORTEX_RDF_PROBE_BATCH=<n>``: ``n`` probes per batch call (default 4,096), and every probe step batched.

``VORTEX_RDF_DISABLE_PUSHDOWN``, ``VORTEX_RDF_NATIVE_FILTERS``, ``VORTEX_RDF_FILTER_FAST`` and
``VORTEX_RDF_PAIR_BUDGET`` keep their own readers (pushdown.py, filters.py).
"""
import os

NAMES = ("VORTEX_RDF_CODE_PREDICATES", "VORTEX_RDF_COUNT_ORDER", "VORTEX_RDF_DECODE_EACH_STEP",
         "VORTEX_RDF_DECODE_CHUNK", "VORTEX_RDF_PROBE_BATCH")


def flag(name: str, default: bool, environ=os.environ) -> bool:
    """``0`` is off, ``1`` is on, unset is ``default``; anything else is refused."""
    value = environ.get(name)
    if value is None or value == "":
        return default
    if value not in ("0", "1"):
        raise ValueError(f"{name} must be 0 or 1, got {value!r}")
    return value == "1"


def positive_int(name: str, environ=os.environ) -> int | None:
    """A positive integer, or ``None`` when unset."""
    value = environ.get(name)
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return n


CODE_PREDICATES = flag("VORTEX_RDF_CODE_PREDICATES", True)
COUNT_ORDER = flag("VORTEX_RDF_COUNT_ORDER", True)
DECODE_EACH_STEP = flag("VORTEX_RDF_DECODE_EACH_STEP", False)
DECODE_CHUNK = positive_int("VORTEX_RDF_DECODE_CHUNK")
PROBE_BATCH = positive_int("VORTEX_RDF_PROBE_BATCH")


def settings() -> dict:
    """The switches as set in this process (for run records)."""
    return {name: os.environ.get(name) for name in NAMES}
