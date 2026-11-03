"""Ablation switches, for measuring what each pushdown mechanism contributes. Each is an
environment variable that sets a field of :data:`~vortex_rdflib.settings.SETTINGS` when a store is
constructed or the pushdown registered (:func:`~vortex_rdflib.settings.apply_switches`), parsed as
the other switches are; with none set the evaluator is exactly the default one.

- ``VORTEX_RDF_CODE_PREDICATES=0``: kind tests (``isIRI``, ``isLiteral``, ``isBlank``) and IRI
  equality / ``IN`` / ``sameTerm`` with an IRI are not answered as code ranges or code sets; they go
  through decoded values.
- ``VORTEX_RDF_COUNT_ORDER=0``: no native counts for planning. Patterns are ordered by their bound
  positions (ties as :func:`pushdown.matching._pattern_static_rank` breaks them) and every connected
  step probes, as rdflib's nested loop does.
- ``VORTEX_RDF_DECODE_EACH_STEP=1``: after every join step the relation's codes are decoded to
  spellings and encoded again, so each step pays the term round trip a term-space evaluator would.
- ``VORTEX_RDF_DECODE_CHUNK=<n>``: every decode chunk is ``n`` rows (default 64, growing to 4,096).
- ``VORTEX_RDF_PROBE_BATCH=<n>``: ``n`` probes per batch call (default 4,096), and every probe step
  batched.

``VORTEX_RDF_DISABLE_PUSHDOWN``, ``VORTEX_RDF_NATIVE_FILTERS`` and ``VORTEX_RDF_FILTER_FAST``
switch the pushdown and its FILTER routes the same way (:mod:`vortex_rdflib.settings`).
"""

from .settings import SETTINGS, Settings

NAMES = (
    "VORTEX_RDF_CODE_PREDICATES",
    "VORTEX_RDF_COUNT_ORDER",
    "VORTEX_RDF_DECODE_EACH_STEP",
    "VORTEX_RDF_DECODE_CHUNK",
    "VORTEX_RDF_PROBE_BATCH",
)


def settings() -> dict:
    """The ablation switches in effect in this process, as the environment spells them: ``None``
    where a setting has its default (for run records)."""
    current, default = SETTINGS, Settings()
    chunk = (current.chunk_start, current.chunk_max)
    return {
        "VORTEX_RDF_CODE_PREDICATES": None if current.code_predicates else "0",
        "VORTEX_RDF_COUNT_ORDER": None if current.count_order else "0",
        "VORTEX_RDF_DECODE_EACH_STEP": "1" if current.decode_each_step else None,
        "VORTEX_RDF_DECODE_CHUNK": (
            None if chunk == (default.chunk_start, default.chunk_max) else str(current.chunk_start)
        ),
        "VORTEX_RDF_PROBE_BATCH": (
            None if current.probe_batch == default.probe_batch else str(current.probe_batch)
        ),
    }
