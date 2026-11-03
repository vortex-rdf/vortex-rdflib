"""What the query trace (:mod:`vortex_rdflib.trace`) reports beyond its
events' own fields: a native keep and a batch probe as JSON.
"""

from ..terms import is_range


def _keep_summary(keep) -> dict | None:
    """A native keep as JSON: per position, a code range or a set's size."""
    if not keep:
        return None
    return {
        str(position): ({"range": list(spec)} if is_range(spec) else {"codes": len(spec)})
        for position, spec in keep.items()
    }


def _probe_pattern(probe) -> list:
    """The ``(s, p, o, g)`` of a batch probe, tuple or dict."""
    if isinstance(probe, dict):
        return [probe.get(key) for key in ("s", "p", "o", "g")]
    return list(probe)
