"""Deterministic synthetic dataset with realistic term cardinality.

A Python port of the generator in vortex-rdf's ``js/bench/shared.ts``.
Distinct terms scale with rows: near-unique subject IRIs (~10 triples per
subject), a small closed predicate vocabulary, and a mix of IRI and literal
objects, so each store's term handling (dictionaries, interning, string
storage) is actually exercised.

The dataset is a set of **quads**, partitioned by subject: every statement
about subject ``j`` goes into graph ``j mod n_graph``, graph 0 being the
default graph and the rest named. Two consequences the query set relies on:

- every statement is in exactly one graph, so the union of the graphs is the
  triple set — the query row counts are the same whether a store is asked
  over the quads or over the flattened N-Triples (``write_ntriples``), and
  the contenders that cannot serve named graphs through rdflib stay
  comparable on every query that does not name one;
- a subject's statements are never split, so a star join *inside* one graph
  has rows, while a chain leaves it (the object is another subject, in
  whatever graph that subject belongs to) — which is how a named graph
  usually partitions real data, by source rather than by statement.

Uniqueness: term indices are ``i % k`` per role, so triple ``i`` maps to the
residue tuple ``(i mod nSubj, i mod nPred, i mod nObj)``. The three moduli are
nudged until pairwise coprime, which by the CRT makes that map injective as
long as their product covers ``n`` — every generated triple is distinct with
no dedupe set. The graph is a function of the subject, so it adds no
coordinate and cannot break that.

Two deliberate divergences from the JS generator, both in ``object_term``:

- IRI objects with index below ``n_subj`` reuse the *subject* IRI space, so a
  share of objects link back into subjects and multi-hop (chain) queries have
  paths to traverse. Injectivity holds: the branches produce disjoint term
  spaces, each injective in the index.
- Literal objects rotate through three kinds — plain, ``xsd:integer`` typed
  (lexical value = the index, so numeric FILTER/ORDER BY thresholds can be
  computed), and language-tagged — so the query set can exercise datatype
  filters and ordering, not just string equality.
"""

import os
from dataclasses import dataclass
from math import gcd

BASE = "http://data.example.org"


@dataclass(frozen=True)
class DatasetConfig:
    n: int
    subject_ratio: float
    predicates: int
    object_ratio: float
    literal_frac: float
    graphs: int


def config_from_env() -> DatasetConfig:
    return DatasetConfig(
        n=int(os.environ.get("BENCH_TRIPLES", 250_000)),
        subject_ratio=float(os.environ.get("BENCH_SUBJ_RATIO", 0.1)),
        predicates=int(os.environ.get("BENCH_PREDICATES", 32)),
        object_ratio=float(os.environ.get("BENCH_OBJ_RATIO", 0.5)),
        literal_frac=float(os.environ.get("BENCH_LITERAL_FRAC", 0.4)),
        graphs=int(os.environ.get("BENCH_GRAPHS", 8)),
    )


@dataclass(frozen=True)
class Moduli:
    n_subj: int
    n_pred: int
    n_obj: int
    n_graph: int
    terms: int


def moduli(cfg: DatasetConfig) -> Moduli:
    """Per-role term counts: the requested values nudged up until pairwise coprime."""
    want = [
        max(1, round(cfg.n * cfg.subject_ratio)),
        max(1, cfg.predicates),
        max(1, round(cfg.n * cfg.object_ratio)),
    ]
    got: list[int] = []
    for w in want:
        k = w
        while any(gcd(g, k) != 1 for g in got):
            k += 1
        got.append(k)
    n_subj, n_pred, n_obj = got
    # The graph is a residue of the *subject*, not of the row index, so it
    # needs no coprimality: it partitions the subject space as asked.
    n_graph = max(1, cfg.graphs)
    if n_subj * n_pred * n_obj < cfg.n:
        raise ValueError(
            f"dataset cardinality too low for {cfg.n} distinct triples: "
            f"{n_subj}x{n_pred}x{n_obj} cannot cover it — raise a ratio"
        )
    # Object indices below n_subj that fall in the IRI branch reuse subject
    # IRIs, so they are not new terms. The graph column adds one term per
    # named graph plus the default graph's empty name, which the store's
    # dictionary holds like any other.
    reused = sum(1 for j in range(min(n_subj, n_obj)) if not _is_literal_index(j, cfg))
    terms = n_subj + n_pred + n_obj - reused + min(n_graph, n_subj)
    return Moduli(n_subj, n_pred, n_obj, n_graph, terms)


def subject_iri(i: int) -> str:
    return f"{BASE}/resource/subject/{i:09d}"


def predicate_iri(i: int) -> str:
    return f"{BASE}/ontology/property/{i:04d}"


def graph_iri(i: int) -> str:
    return f"{BASE}/graph/{i:04d}"


def graph_of_subject(j: int, m: Moduli) -> int:
    """The graph holding every statement about subject index ``j``."""
    return j % m.n_graph


def graph_nq(i: int, m: Moduli) -> str:
    """The graph of statement ``i`` in N-Quads syntax: empty for the default
    graph (index 0), a named-graph IRI otherwise."""
    g = graph_of_subject(i % m.n_subj, m)
    return "" if g == 0 else f"<{graph_iri(g)}>"


def _is_literal_index(j: int, cfg: DatasetConfig) -> bool:
    return j % 10 < round(cfg.literal_frac * 10)


def object_nt(j: int, cfg: DatasetConfig, m: Moduli) -> str:
    """Object term for index ``j``, in N-Triples syntax."""
    if _is_literal_index(j, cfg):
        kind = j % 3
        if kind == 0:
            return f'"{j}"^^<http://www.w3.org/2001/XMLSchema#integer>'
        if kind == 1:
            return f'"descriptive object value {j:09d}"'
        return f'"valeur descriptive {j:09d}"@fr'
    if j < m.n_subj:
        return f"<{subject_iri(j)}>"
    return f"<{BASE}/resource/object/{j:09d}>"


def statement_nt(i: int, cfg: DatasetConfig, m: Moduli) -> str:
    """Statement ``i`` without its graph — the ``s p o`` of the quad."""
    return (
        f"<{subject_iri(i % m.n_subj)}> "
        f"<{predicate_iri(i % m.n_pred)}> "
        f"{object_nt(i % m.n_obj, cfg, m)}"
    )


def triple_nt(i: int, cfg: DatasetConfig, m: Moduli) -> str:
    return f"{statement_nt(i, cfg, m)} ."


def quad_nq(i: int, cfg: DatasetConfig, m: Moduli) -> str:
    graph = graph_nq(i, m)
    return f"{statement_nt(i, cfg, m)} {graph} ." if graph else f"{statement_nt(i, cfg, m)} ."


def write_nquads(path: str, cfg: DatasetConfig) -> Moduli:
    """The dataset as quads — what every store that has named graphs loads."""
    m = moduli(cfg)
    with open(path, "w", encoding="utf-8") as f:
        for i in range(cfg.n):
            f.write(quad_nq(i, cfg, m))
            f.write("\n")
    return m


def write_ntriples(path: str, cfg: DatasetConfig) -> Moduli:
    """The same statements flattened into one graph.

    Each statement is in exactly one graph, so this is the union of the quad
    file's graphs, triple for triple: the stores that cannot serve named
    graphs answer every non-``GRAPH`` query over identical data.
    """
    m = moduli(cfg)
    with open(path, "w", encoding="utf-8") as f:
        for i in range(cfg.n):
            f.write(triple_nt(i, cfg, m))
            f.write("\n")
    return m
