"""What the query trace (:mod:`vortex_rdflib.trace`) reports beyond its
events' own fields: a native keep and a batch probe as JSON, and how a
variable's conjuncts are decided.
"""


def _keep_summary(keep) -> dict | None:
    """A native keep as JSON: per position, a code range or a set's size."""
    if not keep:
        return None
    return {
        str(position): ({"range": list(spec)} if isinstance(spec, tuple) else {"codes": len(spec)})
        for position, spec in keep.items()
    }


def _probe_pattern(probe) -> list:
    """The ``(s, p, o, g)`` of a batch probe, tuple or dict."""
    if isinstance(probe, dict):
        return [probe.get(key) for key in ("s", "p", "o", "g")]
    return list(probe)


def _restriction_counts(conjuncts) -> dict:
    """The trace fields that say how a variable's conjuncts are decided: how
    many there are, and how many have a native predicate (sent to vortex-rdf),
    a fast route, a fast route over term kinds only, or rdflib's evaluator
    alone."""
    return {
        "predicate_count": len(conjuncts),
        "native_predicate_count": sum(c.native is not None and not c.kind_only for c in conjuncts),
        "fast_predicate_count": sum(c.fast is not None for c in conjuncts),
        "kind_only_predicate_count": sum(c.kind_only for c in conjuncts),
        "generic_only_predicate_count": sum(c.fast is None for c in conjuncts),
    }
