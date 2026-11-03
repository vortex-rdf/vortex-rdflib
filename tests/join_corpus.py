"""The join tests' corpus (``tests/joins/``): the statements their files hold,
the queries they ask, and the helpers that run and trace them."""

from tests.corpus import EX_PREFIX
from tests.support import both_ways, run, trace_events

EX = "http://ex/"
_PREFIX = f"PREFIX : <{EX}> "

#: Ten subjects with one `p` and two `q` each: the `?s` step joins 10 rows
#: with 20, a product of 200 that makes 20 pairs.
STAR = _PREFIX + "SELECT ?s ?x ?y WHERE { ?s :p ?x . ?s :q ?y }"
#: The one anchor is probed into its two `r` keys (of 122 `r` rows); the `?k`
#: step then hash-joins those 2 rows with the 150 `t` rows, a product of 300
#: that makes 20 pairs.
CHAIN = _PREFIX + 'SELECT ?s ?k ?z WHERE { ?s :anchor "yes" . ?s :r ?k . ?k :t ?z }'
#: `c0` has nine keys, each referenced by one subject.
FAN = _PREFIX + "SELECT ?k ?f WHERE { :c0 :has ?k . ?f :ref ?k }"
#: The star's seed joined on `?s` and `?x` at once: its ten rows against the
#: 20 `pq` rows, counted on `?s`, the first shared variable, as 20 pairs.
TWIN = _PREFIX + "SELECT ?s ?x WHERE { ?s :p ?x . ?s :pq ?x }"
#: The chain's anchor probed into its two keys, then `?k :owner ?s` (170
#: rows, more than `r`'s 122, so joined after it) on both shared variables:
#: ten `owner` rows per key, counted on `?k` as 20 pairs.
LOOP = _PREFIX + 'SELECT ?s ?k WHERE { ?s :anchor "yes" . ?s :r ?k . ?k :owner ?s }'
#: Ten `ga` rows, two per key, against the 20 `gb` rows, four per key: 40
#: pairs, the relation the smaller side.
WIDE = _PREFIX + "SELECT ?g ?k ?z WHERE { ?g :ga ?k . ?k :gb ?z }"
#: Five keys fanned out into 50 rows over ten `pn` keys, then the 20 `fc`
#: rows of those keys: 100 pairs, the pattern the smaller side.
FANNED = _PREFIX + "SELECT ?m ?n ?z WHERE { :fa0 :fa ?m . ?m :fb ?n . ?n :fc ?z }"
#: Three `u` rows on one key against its four `w` rows.
ONE_KEY = _PREFIX + "SELECT ?h ?z WHERE { ?h :u ?k . ?k :w ?z }"
#: Four `ra` rows on two keys against `?k :loop ?k`: of the 110 `loop` rows,
#: the ten that loop a key onto itself pass, and 4 pairs are made.
SELF_LOOP = _PREFIX + "SELECT ?s ?k WHERE { ?s :ra ?k . ?k :loop ?k }"
#: 120 `ta` rows seed the relation. Both `tl` patterns count all 245 `tl`
#: rows, and the `?s` one comes first: its step makes 240 pairs, two `tl`
#: rows per seed subject. The `?x` one reads the match that step left in the
#: memo, and its step makes 5, the five seed objects that are `tl` subjects.
#: Ten answers.
_TIER_BODY = "?s :ta ?x . ?s :tl ?c . ?x :tl ?d"
REORDER = _PREFIX + f"SELECT ?s ?x ?c ?d WHERE {{ {_TIER_BODY} }}"
#: 120 `sa` rows bind `?k` to one literal, so the FILTER is a prefix test of
#: `?c` alone for the pattern that binds it. `?c :sl ?s` (pattern 0) comes
#: first: its 120 `spc` rows that pass make 120 pairs. `?s :sl ?c` (pattern
#: 1) makes 2 of the seed rows, `sp0`'s two `sl` rows, and one of them fails
#: the test.
SPECIALIZED = (
    _PREFIX + "SELECT ?s ?k ?c WHERE { ?s :sa ?k . ?s :sl ?c . ?c :sl ?s "
    "FILTER(STRSTARTS(STR(?c), ?k)) }"
)
#: `c0`'s nine keys against every statement that has one as its object: their
#: nine `has` and nine `ref` statements. Counted at its step up to the nine
#: rows x 100 + 1, `?f ?p ?k` reaches that cap (the store holds 1,348
#: statements), so the step joins it per key — with probes that would each
#: scan the store, binding no subject on a store with no index.
SCANNED = _PREFIX + "SELECT ?k ?f ?p WHERE { :c0 :has ?k . ?f ?p ?k }"
#: The anchor against the 170 `owner` statements, 152 of which name it.
OWNED = _PREFIX + 'SELECT ?n WHERE { ?s :anchor "yes" . ?n :owner ?s }'
#: The chain's anchor probed into its two `r` keys, then every statement that
#: has one as its object: `?x ?p ?k` counts the store's 1,348 when planned,
#: past the two rows x 100, and is joined per key.
ANCHORED_SCAN = _PREFIX + 'SELECT ?s ?k ?x ?p WHERE { ?s :anchor "yes" . ?s :r ?k . ?x ?p ?k }'
#: The four `ra` rows hold their two keys twice each. Counted at its step up
#: to four rows x 100 + 1, `?x ?p ?k` reaches that cap; each key is the object
#: of its two `ra` statements and of its own `loop`, so the keep-scan reads
#: six rows, which make twelve pairs with the four.
KEYED_TWICE = _PREFIX + "SELECT ?r ?k ?x ?p WHERE { ?r :ra ?k . ?x ?p ?k }"
#: The same keys against their own loops: `?k :loop ?k` repeats `?k`.
LOOPED = _PREFIX + "SELECT ?r ?k WHERE { ?r :ra ?k . ?k :loop ?k }"

#: Quads for Review Focus 2's shapes beyond `PROBE_MULTIPLICITY`'s, added to
#: `PROBE_QUADS`, whose cases they leave alone: `?s :q ?v` binds `?v` to `x`
#: four times and to `y` once, and the `r` statements repeat a code across
#: their subject and object — `x`'s in two graphs, `y`'s in one, and `z`'s,
#: which the relation does not bind — or do not (`x :r :y`, `y :r :x`).
KEEP_SCAN_QUADS = """\
<http://ex/s5> <http://ex/q> <http://ex/y> <http://ex/g1> .
<http://ex/x> <http://ex/r> <http://ex/x> <http://ex/g1> .
<http://ex/x> <http://ex/r> <http://ex/x> <http://ex/g2> .
<http://ex/x> <http://ex/r> <http://ex/y> <http://ex/g1> .
<http://ex/y> <http://ex/r> <http://ex/x> <http://ex/g2> .
<http://ex/y> <http://ex/r> <http://ex/y> <http://ex/g3> .
<http://ex/z> <http://ex/r> <http://ex/z> <http://ex/g1> .
"""

#: Per shape, as `PROBE_MULTIPLICITY` has it: whether the default graph is the
#: union of the graphs, the query and the rows rdflib answers.
KEEP_SCAN_MULTIPLICITY = {
    # The shared `?v` sits at both positions of `?v :r ?v`, the pattern a
    # step keep-scans: of the triples its keep selects, the two that repeat a
    # code join, `x`'s three subjects and `y`'s one, each triple once however
    # many graphs hold it.
    "shared-twice": (True, EX_PREFIX + "SELECT ?s ?v WHERE { ?s :q ?v . ?v :r ?v }", 4),
    # The FILTER's keep on `?s` is on the pattern before the keep-scan
    # narrows it to the relation's codes: `s1`, the one subject it keeps with
    # both triples.
    "keep-before": (
        True,
        EX_PREFIX + "SELECT ?s WHERE { ?s :q :x . ?s :p :o1 FILTER(?s IN (:s1, :s2, :s3)) }",
        1,
    ),
}

#: The shapes whose `p` or `r` pattern is a step of the BGP: forced to probe
#: with probes that scan, it keep-scans. The others join above a BGP, or
#: test an OPTIONAL or EXISTS, which keep their probes.
_KEEP_SCANNED = {
    "bgp-join",
    "graph-variable",
    "named-graph",
    "default-graph",
    "shared-twice",
    "keep-before",
}

#: The fields every trace event carries; the rest are the event's own.
_ENVELOPE = ("schema", "event", "sequence", "query_id")


def _statements() -> str:
    lines = []
    for i in range(10):
        lines.append(f"<{EX}s{i}> <{EX}p> <{EX}x{i}> .")
        lines += [f"<{EX}s{i}> <{EX}q> <{EX}y{i}-{j}> ." for j in range(2)]
    lines.append(f'<{EX}a0> <{EX}anchor> "yes" .')
    lines += [f"<{EX}a0> <{EX}r> <{EX}k{i}> ." for i in range(2)]
    lines += [f"<{EX}b{i}> <{EX}r> <{EX}k{i + 2}> ." for i in range(120)]
    lines += [f"<{EX}k{i}> <{EX}t> <{EX}z{i}-{j}> ." for i in range(2) for j in range(10)]
    lines += [f"<{EX}k{i + 2}> <{EX}t> <{EX}w{i}> ." for i in range(130)]
    lines += [f"<{EX}c0> <{EX}has> <{EX}m{i}> ." for i in range(9)]
    lines += [f"<{EX}f{i}> <{EX}ref> <{EX}m{i}> ." for i in range(9)]
    lines += [f"<{EX}s{i}> <{EX}pq> <{EX}{o}{i}> ." for i in range(10) for o in ("x", "v")]
    owners = ["a0", *(f"o{j}" for j in range(9))]
    lines += [f"<{EX}k{i}> <{EX}owner> <{EX}{o}> ." for i in range(2) for o in owners]
    lines += [f"<{EX}n{i}> <{EX}owner> <{EX}a0> ." for i in range(150)]
    lines += [f"<{EX}g{i}> <{EX}ga> <{EX}gk{i // 2}> ." for i in range(10)]
    lines += [f"<{EX}gk{k}> <{EX}gb> <{EX}gz{k}-{t}> ." for k in range(5) for t in range(4)]
    lines += [f"<{EX}fa0> <{EX}fa> <{EX}pm{i}> ." for i in range(5)]
    lines += [f"<{EX}pm{i}> <{EX}fb> <{EX}pn{j}> ." for i in range(5) for j in range(10)]
    lines += [f"<{EX}pn{j}> <{EX}fc> <{EX}pz{j}-{t}> ." for j in range(10) for t in range(2)]
    lines += [f"<{EX}h{i}> <{EX}u> <{EX}hk> ." for i in range(3)]
    lines += [f"<{EX}hk> <{EX}w> <{EX}hz{i}> ." for i in range(4)]
    lines += [f"<{EX}rs{i}> <{EX}ra> <{EX}lk{i % 2}> ." for i in range(4)]
    lines += [f"<{EX}lk{j}> <{EX}loop> <{EX}lk{j}> ." for j in range(10)]
    lines += [f"<{EX}lk{j}> <{EX}loop> <{EX}lz{j}-{t}> ." for j in range(10) for t in range(10)]
    lines += _tier_statements(120, 5)
    lines += [f'<{EX}sp{i}> <{EX}sa> "{EX}spc" .' for i in range(120)]
    lines += [f"<{EX}spc{i}-0> <{EX}sl> <{EX}sp{i}> ." for i in range(120)]
    lines += [f"<{EX}spd0> <{EX}sl> <{EX}sp0> ."]
    lines += [f"<{EX}sp0> <{EX}sl> <{EX}{o}> ." for o in ("spc0-0", "spd0")]
    return "\n".join(lines) + "\n"


def _tier_statements(seeds: int, linked: int) -> list[str]:
    """``_TIER_BODY``'s data: ``seeds`` `ta` rows `ts{i} :ta tx{i}`, two `tl`
    rows per `ts{i}`, one `tl` row for each of the first ``linked`` `tx{i}`,
    and a `tw` row for each of the first three `ts{i}`."""
    lines = [f"<{EX}ts{i}> <{EX}ta> <{EX}tx{i}> ." for i in range(seeds)]
    lines += [f"<{EX}ts{i}> <{EX}tl> <{EX}tc{i}-{j}> ." for i in range(seeds) for j in range(2)]
    lines += [f"<{EX}tx{i}> <{EX}tl> <{EX}td{i}> ." for i in range(linked)]
    lines += [f"<{EX}ts{i}> <{EX}tw> <{EX}tw{i}> ." for i in range(3)]
    return lines


def _traced_both_ways(graph, monkeypatch, capsys, sparql, runner=run):
    """Both answers to ``sparql`` (see ``both_ways``) and the trace events of
    the pushdown's run."""
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    answers = both_ways(graph, sparql, runner)
    return answers, trace_events(capsys.readouterr().err)


def _fields(events, name) -> list[dict]:
    """The own fields of each ``name`` event."""
    return [
        {key: value for key, value in event.items() if key not in _ENVELOPE}
        for event in events
        if event["event"] == name
    ]


def _plans(events) -> list:
    """The patterns rdflib was left to finish (tier 3), per completed BGP
    plan: ``None`` where it was left none."""
    return [plan["rest_pattern_indices"] for plan in _fields(events, "bgp_plan_complete")]


def _streams(events) -> list[dict]:
    """The join steps that streamed (tier 1)."""
    return [
        step for step in _fields(events, "bgp_join_step_complete") if step["strategy"] == "stream"
    ]
