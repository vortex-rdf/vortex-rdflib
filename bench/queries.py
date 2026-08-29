"""The synthetic representative SPARQL query set.

Every query's constants (anchor terms, filter thresholds) are derived from the
same modular arithmetic that generates the dataset, so each query is
guaranteed non-empty by construction — no query can silently measure a
zero-row execution. The workers still record actual result counts, and the
orchestrator cross-checks them across stores: every store answers the same
query over the same data, so a count disagreement is a correctness bug in one
of them, not a benchmarking detail.

Three groups, mirroring how the dashboard panels are organized:

- ``lookups``  — single-pattern selectivity shapes (ASK, bound PO, bound O,
  predicate scan): what a store's raw ``triples()`` service costs; plus an
  ASK over a variable pattern and a ``LIMIT 10`` over the whole store, the
  two heads a store can answer without decoding a term.
- ``joins``    — anchored star-2/star-3, an unanchored 2-hop chain, and an
  OPTIONAL: where the BGP evaluation strategy (vortex-rdflib's whole-BGP
  pushdown vs rdflib's per-binding nested loop) dominates.
- ``features`` — FILTER on a typed range, DISTINCT, ORDER BY + LIMIT, and a
  full-scan GROUP BY aggregate: rdflib operators layered over the BGP.

``heavy`` marks queries whose single execution touches the whole dataset (or
a whole predicate's bindings joined against the store); the harness gives
those a lower iteration budget, mirroring FULL_SCAN_OPTS in the JS bench.
"""

from dataclasses import dataclass
from textwrap import dedent

from .dataset import DatasetConfig, Moduli, object_nt, predicate_iri, subject_iri

XSD_PREFIX = "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"


@dataclass(frozen=True)
class Query:
    name: str
    group: str
    sparql: str
    heavy: bool = False
    is_ask: bool = False


def _sparql(text: str) -> str:
    """Strip the Python source indentation off a query written inline.

    Queries are laid out one triple pattern per line: whitespace is
    insignificant to SPARQL, and the dashboard prints ``sparql`` verbatim in
    its query-set section, so the text a reader sees is exactly the text the
    stores were given.
    """
    return dedent(text).strip()


def _chain_predicates(cfg: DatasetConfig, m: Moduli) -> tuple[int, int]:
    """Pick ``(pA, pB)`` for the 2-hop chain so it has continuations.

    ``pA`` is fixed at 0; its object IRIs that fall in the subject space are
    the middle nodes. ``pB`` is the predicate the most middle nodes carry as
    subjects, computed by walking the same residue arithmetic the generator
    uses (cheap: ~n/n_pred iterations).
    """
    literal_cut = round(cfg.literal_frac * 10)
    counts: dict[int, int] = {}
    for i in range(0, cfg.n, m.n_pred):  # rows with predicate index 0
        j = i % m.n_obj
        if j % 10 < literal_cut or j >= m.n_subj:
            continue  # object is a literal or a non-subject IRI: no hop
        for i2 in range(j, cfg.n, m.n_subj):  # rows with subject index j
            p2 = i2 % m.n_pred
            counts[p2] = counts.get(p2, 0) + 1
    if not counts:
        raise ValueError("chain-2 has no continuations — dataset too small or ratios off")
    return 0, max(counts, key=lambda p: counts[p])


def _filter_predicate(cfg: DatasetConfig, m: Moduli) -> tuple[int, int]:
    """Pick the predicate for the numeric FILTER/ORDER BY queries, plus a
    threshold selecting roughly an eighth of its integer bindings.

    The predicate index cannot be fixed: the literal-kind selector
    (``j % 3``) correlates with predicate stepping whenever 3 divides the
    nudged predicate modulus, leaving some predicates with *no* integer
    objects at all. Walk the residue arithmetic and take the predicate with
    the most integer bindings (their lexical value is the object index).
    """
    literal_cut = round(cfg.literal_frac * 10)
    per_pred: dict[int, list[int]] = {}
    for i in range(cfg.n):
        j = i % m.n_obj
        if j % 10 < literal_cut and j % 3 == 0:
            per_pred.setdefault(i % m.n_pred, []).append(j)
    if not per_pred:
        raise ValueError("no integer-literal objects generated — dataset too small")
    p = max(per_pred, key=lambda k: len(per_pred[k]))
    values = sorted(per_pred[p])
    return p, values[max(1, len(values) // 8)]


def build_queries(cfg: DatasetConfig, m: Moduli) -> list[Query]:
    s0 = f"<{subject_iri(0)}>"
    p0 = f"<{predicate_iri(0)}>"
    p1 = f"<{predicate_iri(1)}>"
    o0 = object_nt(0, cfg, m)

    # First object index in the IRI-branch that links back to a subject.
    literal_cut = round(cfg.literal_frac * 10)
    j_link = next(j for j in range(m.n_obj) if j % 10 >= literal_cut and j < m.n_subj)
    o_link = f"<{subject_iri(j_link)}>"

    # Star legs: subject(0)'s rows are i = k * n_subj, whose predicate indices
    # are (k * n_subj) mod n_pred — distinct while k < n_pred (coprime moduli).
    star_p2 = f"<{predicate_iri(m.n_subj % m.n_pred)}>"
    star_p3 = f"<{predicate_iri(2 * m.n_subj % m.n_pred)}>"

    chain_a, chain_b = _chain_predicates(cfg, m)
    chain_pa = f"<{predicate_iri(chain_a)}>"
    chain_pb = f"<{predicate_iri(chain_b)}>"

    filter_p, int_cut = _filter_predicate(cfg, m)
    pf = f"<{predicate_iri(filter_p)}>"

    return [
        Query(
            "ask-spo",
            "lookups",
            _sparql(f"""
                ASK {{
                  {s0} {p0} {o0}
                }}
            """),
            is_ask=True,
        ),
        Query(
            "po-lookup",
            "lookups",
            _sparql(f"""
                SELECT ?s WHERE {{
                  ?s {p0} {o0}
                }}
            """),
        ),
        Query(
            "o-scan",
            "lookups",
            _sparql(f"""
                SELECT ?s ?p WHERE {{
                  ?s ?p {o_link}
                }}
            """),
        ),
        Query(
            "p-scan",
            "lookups",
            _sparql(f"""
                SELECT ?s ?o WHERE {{
                  ?s {p1} ?o
                }}
            """),
        ),
        Query(
            "ask-var",
            "lookups",
            _sparql(f"""
                ASK {{
                  ?s {p1} ?o
                }}
            """),
            is_ask=True,
        ),
        Query(
            "limit-scan",
            "lookups",
            _sparql("""
                SELECT * WHERE {
                  ?s ?p ?o
                }
                LIMIT 10
            """),
        ),
        Query(
            "star-2",
            "joins",
            _sparql(f"""
                SELECT ?s ?o WHERE {{
                  ?s {p0} {o0} .
                  ?s {star_p2} ?o
                }}
            """),
        ),
        Query(
            "star-3",
            "joins",
            _sparql(f"""
                SELECT ?s ?o ?o2 WHERE {{
                  ?s {p0} {o0} .
                  ?s {star_p2} ?o .
                  ?s {star_p3} ?o2
                }}
            """),
        ),
        Query(
            "chain-2",
            "joins",
            _sparql(f"""
                SELECT ?s ?o WHERE {{
                  ?s {chain_pa} ?m .
                  ?m {chain_pb} ?o
                }}
            """),
            heavy=True,
        ),
        Query(
            "optional",
            "joins",
            _sparql(f"""
                SELECT ?s ?o ?x WHERE {{
                  ?s {p0} {o0} .
                  ?s {star_p2} ?o
                  OPTIONAL {{
                    ?s {star_p3} ?x
                  }}
                }}
            """),
        ),
        Query(
            "filter-range",
            "features",
            _sparql(f"""
                {XSD_PREFIX}
                SELECT ?s ?v WHERE {{
                  ?s {pf} ?v .
                  FILTER(datatype(?v) = xsd:integer && ?v < {int_cut})
                }}
            """),
        ),
        Query(
            "distinct",
            "features",
            _sparql(f"""
                SELECT DISTINCT ?o WHERE {{
                  ?s {p1} ?o
                }}
            """),
        ),
        Query(
            "order-limit",
            "features",
            _sparql(f"""
                {XSD_PREFIX}
                SELECT ?s ?v WHERE {{
                  ?s {pf} ?v .
                  FILTER(datatype(?v) = xsd:integer)
                }}
                ORDER BY DESC(?v)
                LIMIT 10
            """),
        ),
        Query(
            "agg-count",
            "features",
            _sparql("""
                SELECT ?p (COUNT(*) AS ?n) WHERE {
                  ?s ?p ?o
                }
                GROUP BY ?p
            """),
            heavy=True,
        ),
    ]
