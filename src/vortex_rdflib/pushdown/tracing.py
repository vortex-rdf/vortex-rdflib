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
    many there are, and how many vortex-rdf decides (a keep or a
    ``filter_codes`` predicate), have a fast route, are decided by term kinds
    alone, or by rdflib's evaluator alone."""
    return {
        "predicate_count": len(conjuncts),
        "native_predicate_count": sum(
            c.kinds is None and (c.keep is not None or c.codes is not None) for c in conjuncts
        ),
        "fast_predicate_count": sum(c.fast is not None for c in conjuncts),
        "kind_only_predicate_count": sum(c.kinds is not None for c in conjuncts),
        "generic_only_predicate_count": sum(c.fast is None for c in conjuncts),
    }
