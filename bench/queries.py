"""The synthetic representative SPARQL query set.

Every query's constants (anchor terms, filter thresholds) are derived from the
same modular arithmetic that generates the dataset, so each query is
guaranteed non-empty by construction — no query can silently measure a
zero-row execution. The workers still record actual result counts, and the
orchestrator cross-checks them across stores: every store answers the same
query over the same data, so a count disagreement is a correctness bug in one
of them, not a benchmarking detail.

Four groups, mirroring how the dashboard panels are organized:

- ``lookups``  — single-pattern selectivity shapes (ASK, bound PO, bound O,
  predicate scan): what a store's raw ``triples()`` service costs; plus an
  ASK over a variable pattern and a ``LIMIT 10`` over the whole store, the
  two heads a store can answer without decoding a term.
- ``joins``    — anchored star-2/star-3, an unanchored 2-hop chain, an
  anchored OPTIONAL, a VALUES of 64 subjects joined to a predicate scan, a
  wide OPTIONAL (a whole predicate scan as the outer side), a NOT EXISTS
  over the same scan, and a MINUS of the filtered range
  against an object-kind filtered scan (heavy: rdflib's own MINUS is
  quadratic): where the join
  strategy (vortex-rdflib's code-space joins vs rdflib's per-binding
  nested loop) dominates.
- ``features`` — FILTER on a typed range, a term-kind FILTER (``isIRI``),
  DISTINCT over a predicate scan and over the whole store, ORDER BY + LIMIT,
  a full ORDER BY of a predicate scan, and full-scan aggregates (a GROUP BY
  count, a COUNT(*), a COUNT DISTINCT per group): rdflib operators layered
  over the BGP.
- ``graphs``   — the shapes that name a graph: a scan and a star inside one
  named graph, a chain that starts in one and continues wherever the object
  lives, and the three an unbound ``GRAPH ?g`` drives — a scan, the distinct
  graph names, and a count per graph. Marked ``quads``: the contenders that
  cannot serve named graphs through rdflib (HDT, COTTAS — see
  ``bench.adapters``) do not run them, and the dashboard leaves those cells
  empty rather than pretending.

  Every other query is asked over the **union** of the graphs, which is the
  triple set (``bench.dataset`` puts each statement in exactly one graph), so
  its row count is what it was before the dataset had graphs at all.

``heavy`` marks queries whose single execution touches the whole dataset (or
a whole predicate's bindings joined against the store); the harness gives
those a lower iteration budget, mirroring FULL_SCAN_OPTS in the JS bench.
"""

from collections import Counter
from dataclasses import dataclass
from textwrap import dedent

from .dataset import (
    DatasetConfig,
    Moduli,
    graph_iri,
    graph_of_subject,
    object_nt,
    predicate_iri,
    subject_iri,
)

XSD_PREFIX = "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"


@dataclass(frozen=True)
class Query:
    name: str
    group: str
    sparql: str
    heavy: bool = False
    is_ask: bool = False
    #: Names a graph, so only a store that serves named graphs can answer it.
    quads: bool = False
    #: One pattern, and an answer that is a cardinality rather than the rows:
    #: a store able to count a selection without materializing it answers
    #: these without decoding a term, so their columns compare an approach
    #: rather than an implementation's speed. See ``docs/pushdown.md``.
    countable: bool = False


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


def _partner_predicate(cfg: DatasetConfig, m: Moduli, subjects: set[int], exclude: int) -> int:
    """A predicate carried by roughly half of ``subjects`` — so a join of
    those subjects' rows against it has matched *and* unmatched rows by
    construction (OPTIONAL pads, MINUS and NOT EXISTS remove).

    Subject ``j`` carries predicates ``(j + k * n_subj) mod n_pred`` for the
    rows ``j + k * n_subj < n``; the walk counts them per predicate and picks
    the one (other than ``exclude``) closest to half, strictly between none
    and all.
    """
    counts: dict[int, int] = {}
    for j in subjects:
        for i in range(j, cfg.n, m.n_subj):
            p = i % m.n_pred
            counts[p] = counts.get(p, 0) + 1
    total = len(subjects)
    candidates = [p for p, c in counts.items() if p != exclude and 0 < c < total]
    if not candidates:
        raise ValueError("no predicate splits the subjects — dataset too small or ratios off")
    return min(candidates, key=lambda p: abs(counts[p] - total / 2))


def _object_split_predicate(cfg: DatasetConfig, m: Moduli, subjects: set[int], exclude: int) -> int:
    """A predicate every one of ``subjects`` carries whose objects are IRIs for
    some of them and literals for the others — so ``MINUS { ?s <q> ?x
    FILTER(isIRI(?x)) }`` removes a proper subset by construction.

    The filter-range subjects all come from the first block of rows
    (``k = 0``), so they share one residue class modulo ``n_pred`` and
    carry the same predicates; a predicate cannot split them, an object
    kind can (the object index runs with the row index).
    """
    literal_cut = round(cfg.literal_frac * 10)
    per_pred: dict[int, list[bool]] = {}
    for j in subjects:
        for i in range(j, cfg.n, m.n_subj):
            per_pred.setdefault(i % m.n_pred, []).append((i % m.n_obj) % 10 >= literal_cut)
    total = len(subjects)
    for p in sorted(per_pred):
        kinds = per_pred[p]
        if p != exclude and len(kinds) == total and 0 < sum(kinds) < total:
            return p
    raise ValueError("no predicate splits the subjects by object kind — dataset too small")


def _busiest_named_graph(subjects, m: Moduli) -> int:
    """The named graph holding the most of ``subjects``.

    Never the default graph (index 0): no ``GRAPH`` clause can name it, so a
    query scoped there would measure an empty result.
    """
    counts = Counter(graph_of_subject(j, m) for j in subjects)
    counts.pop(0, None)
    if not counts:
        raise ValueError("no named graph holds any of these subjects — dataset too small")
    return max(counts, key=lambda g: (counts[g], -g))


def _chain_graph(cfg: DatasetConfig, m: Moduli, p_a: int) -> tuple[int, int]:
    """``(graph, p_b)`` for the graph-scoped chain: the named graph and
    second-leg predicate with the most continuations, picked *together*.

    The same walk as ``_chain_predicates``, but its ``p_b`` is chosen over
    every first-leg row, and the rows starting in a named graph are only a
    subset of those — a predicate that suits the whole need not suit them, and
    picking the graph afterwards can leave the query empty. Searching the pair
    keeps it non-empty by construction. The continuation is wherever the
    middle node's own graph is, which is the point of the query.
    """
    literal_cut = round(cfg.literal_frac * 10)
    counts: dict[tuple[int, int], int] = {}
    for i in range(p_a, cfg.n, m.n_pred):  # rows carrying predicate index p_a
        j = i % m.n_obj
        if j % 10 < literal_cut or j >= m.n_subj:
            continue  # object is a literal or a non-subject IRI: no hop
        g = graph_of_subject(i % m.n_subj, m)
        if g == 0:
            continue  # the default graph is not nameable
        for i2 in range(j, cfg.n, m.n_subj):  # rows with subject index j
            key = (g, i2 % m.n_pred)
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        raise ValueError("no named graph starts a chain — dataset too small or ratios off")
    return max(counts, key=lambda k: (counts[k], -k[0], -k[1]))


def _carriers(cfg: DatasetConfig, m: Moduli, subjects: set[int], predicate: int) -> set[int]:
    """The members of ``subjects`` that carry ``predicate``."""
    return {
        j for j in subjects if any(i % m.n_pred == predicate for i in range(j, cfg.n, m.n_subj))
    }


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

    # Subjects of the p1 scan, and of the filter-range rows: the outer sides
    # of the OPTIONAL and MINUS shapes, paired with a predicate half of them carry.
    p1_subjects = {i % m.n_subj for i in range(1, cfg.n, m.n_pred)}
    opt_p = _partner_predicate(cfg, m, p1_subjects, exclude=1)
    q_opt = f"<{predicate_iri(opt_p)}>"
    filter_subjects = {
        i % m.n_subj
        for i in range(cfg.n)
        if i % m.n_pred == filter_p
        and (i % m.n_obj) % 10 < literal_cut
        and (i % m.n_obj) % 3 == 0
        and (i % m.n_obj) < int_cut
    }
    q_minus = (
        f"<{predicate_iri(_object_split_predicate(cfg, m, filter_subjects, exclude=filter_p))}>"
    )
    values_64 = " ".join(f"<{subject_iri(j)}>" for j in sorted(p1_subjects)[:64])

    # Graph constants. A subject's statements are never split across graphs,
    # so a graph is chosen by the subjects a query needs: the one holding most
    # of them, which makes every graph-scoped query non-empty by construction.
    g_scan = f"<{graph_iri(_busiest_named_graph(p1_subjects, m))}>"
    star_subjects = _carriers(cfg, m, p1_subjects, opt_p)
    g_star = f"<{graph_iri(_busiest_named_graph(star_subjects, m))}>"
    chain_graph, chain_graph_b = _chain_graph(cfg, m, chain_a)
    g_chain = f"<{graph_iri(chain_graph)}>"
    chain_g_pb = f"<{predicate_iri(chain_graph_b)}>"

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
            countable=True,
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
            countable=True,
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
            "values-64",
            "joins",
            _sparql(f"""
                SELECT ?s ?o WHERE {{
                  VALUES ?s {{ {values_64} }}
                  ?s {p1} ?o
                }}
            """),
        ),
        Query(
            "optional-wide",
            "joins",
            _sparql(f"""
                SELECT ?s ?o ?x WHERE {{
                  ?s {p1} ?o
                  OPTIONAL {{
                    ?s {q_opt} ?x
                  }}
                }}
            """),
        ),
        Query(
            "not-exists",
            "joins",
            _sparql(f"""
                SELECT ?s ?o WHERE {{
                  ?s {p1} ?o
                  FILTER NOT EXISTS {{
                    ?s {q_opt} ?x
                  }}
                }}
            """),
        ),
        Query(
            "minus",
            "joins",
            _sparql(f"""
                {XSD_PREFIX}
                SELECT ?s ?v WHERE {{
                  {{
                    ?s {pf} ?v .
                    FILTER(datatype(?v) = xsd:integer && ?v < {int_cut})
                  }}
                  MINUS {{
                    ?s {q_minus} ?x
                    FILTER(isIRI(?x))
                  }}
                }}
            """),
            heavy=True,
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
            "filter-class",
            "features",
            _sparql(f"""
                SELECT ?s ?o WHERE {{
                  ?s {p1} ?o
                  FILTER(isIRI(?o))
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
            "order-var",
            "features",
            _sparql(f"""
                SELECT ?s ?o WHERE {{
                  ?s {p1} ?o
                }}
                ORDER BY ?o
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
        Query(
            "distinct-p",
            "features",
            _sparql("""
                SELECT DISTINCT ?p WHERE {
                  ?s ?p ?o
                }
            """),
            heavy=True,
        ),
        Query(
            "count-all",
            "features",
            _sparql("""
                SELECT (COUNT(*) AS ?n) WHERE {
                  ?s ?p ?o
                }
            """),
            heavy=True,
            countable=True,
        ),
        Query(
            "count-distinct",
            "features",
            _sparql("""
                SELECT ?p (COUNT(DISTINCT ?o) AS ?n) WHERE {
                  ?s ?p ?o
                }
                GROUP BY ?p
            """),
            heavy=True,
        ),
        Query(
            "graph-scan",
            "graphs",
            _sparql(f"""
                SELECT ?s ?o WHERE {{
                  GRAPH {g_scan} {{
                    ?s {p1} ?o
                  }}
                }}
            """),
            quads=True,
        ),
        Query(
            "graph-star",
            "graphs",
            _sparql(f"""
                SELECT ?s ?o ?x WHERE {{
                  GRAPH {g_star} {{
                    ?s {p1} ?o .
                    ?s {q_opt} ?x
                  }}
                }}
            """),
            quads=True,
        ),
        Query(
            "graph-chain",
            "graphs",
            _sparql(f"""
                SELECT ?s ?o WHERE {{
                  GRAPH {g_chain} {{
                    ?s {chain_pa} ?m
                  }}
                  ?m {chain_g_pb} ?o
                }}
            """),
            heavy=True,
            quads=True,
        ),
        Query(
            "graph-var",
            "graphs",
            _sparql(f"""
                SELECT ?g ?s ?o WHERE {{
                  GRAPH ?g {{
                    ?s {p1} ?o
                  }}
                }}
            """),
            quads=True,
        ),
        Query(
            "graph-names",
            "graphs",
            _sparql("""
                SELECT DISTINCT ?g WHERE {
                  GRAPH ?g {
                    ?s ?p ?o
                  }
                }
            """),
            heavy=True,
            quads=True,
        ),
        Query(
            "graph-count",
            "graphs",
            _sparql("""
                SELECT ?g (COUNT(*) AS ?n) WHERE {
                  GRAPH ?g {
                    ?s ?p ?o
                  }
                }
                GROUP BY ?g
            """),
            heavy=True,
            quads=True,
        ),
    ]
