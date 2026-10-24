"""The string FILTER shapes vortex-rdf answers — REGEX, CONTAINS,
STRSTARTS and STRENDS over ?v and STR(?v), either under LCASE/UCASE —
checked against rdflib's evaluator and Python's re over texts that exercise
the exact-agreement rules: trailing newlines, case folding outside ASCII,
language tags, typed literals whose rdflib lexical form is not the stored
one, IRIs and blank nodes."""

import contextlib
import os
import random
import re
import signal

import pytest
from rdflib import Graph, Literal
from rdflib.term import Variable
from vortex_rdf import serialize_rdf

from test_filters import filter_expr, rdflib_answer
from test_native_filters import _outcome
from test_pushdown import both_ways
from vortex_rdflib import VortexRdflibStore, candidates, filters, register_sparql_pushdown
from vortex_rdflib.candidates import NativeShape
from vortex_rdflib.settings import SETTINGS
from vortex_rdflib.terms import canonical_spelling, parse_spelling

V = Variable("v")
XSD = "http://www.w3.org/2001/XMLSchema#"

TEXTS = [
    '"abc"', '"ABC"', '"aBc xyz"', '"abc\\n"', '"line one\\nline two\\n"', '"a.b*c+d?"',
    '"(x)[y]{z}|^$\\\\/-"', '"   "', '""', '"0123 456"', '"a_b-c"', '"école"',
    '"ÉCOLE"', '"straße"', '"ſ s S"', '"K k"', '"tab\\there"',
    '"a\\u0001b"', '"a\\\\x41"', '"abc"@en', '"ABC def"@en-gb', '"x\\n"@fr', f'"5"^^<{XSD}integer>',
    f'"042"^^<{XSD}integer>', f'"1e2"^^<{XSD}double>', '"x"^^<http://ex.org/dt>',
    # \s, \w and \d over characters Python's re and Unicode-property engines class
    # differently: no-break space, U+001C, superscript two, Arabic-Indic three,
    # Roman numeral four, a combining mark, dotted and dotless i
    '"a\xa0b"', '"a\\u001Cb"', '"\xb2"', '"\u0663"',
    '"\u2163"', '"e\u0301"', '"\u0130"', '"\u0131"',
    "<http://ex.org/abc>", "<urn:x:ABC>", "_:abc",
]  # fmt: skip

_PY_FLAGS = {"i": re.IGNORECASE, "s": re.DOTALL, "m": re.MULTILINE}


def _py_flags(flags: str) -> int:
    value = 0
    for flag in flags:
        value |= _PY_FLAGS[flag]
    return value


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    directory = tmp_path_factory.mktemp("native-strings")
    nt = directory / "texts.nt"
    nt.write_text(
        "".join(f"<http://ex.org/s{i}> <http://ex.org/p> {t} .\n" for i, t in enumerate(TEXTS)),
        encoding="utf-8",
    )
    out = directory / "texts.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexRdflibStore(str(out), in_memory=True)
    assert store._dict is not None
    codes = {store._dict.encode(text) for text in TEXTS}
    assert None not in codes
    return store, codes


def _rdflib_text(spelling: str, case: str | None, as_str: bool) -> str | None:
    """The text rdflib's string functions read: ``string()`` of the term, or
    ``STR()`` of it (an IRI's string, a literal's rdflib lexical form, a blank
    node's label), then LCASE/UCASE; ``None`` where rdflib raises."""
    term = VortexRdflibStore._from_n3_safe(spelling)
    view = parse_spelling(spelling)
    if as_str:
        text = str(term)
    elif view.kind == filters.LITERAL and view.dt in (None, XSD + "string"):
        text = str(term)
    else:
        return None
    if case == "lower":
        return text.lower()
    if case == "upper":
        return text.upper()
    return text


# --- random patterns from vortex-rdf's regex allow-list grammar

_LITERALS = "abcxyzABC019 -_"
_ESCAPED = [r"\.", r"\*", r"\+", r"\?", r"\(", r"\)", r"\[", r"\]", r"\{", r"\}", r"\|",
            r"\^", r"\$", r"\\", r"\/", r"\-"]  # fmt: skip
_SHORTHANDS = [r"\d", r"\w", r"\s", r"\D", r"\W", r"\S"]


def _class(rng: random.Random) -> str:
    items = []
    for _ in range(rng.randint(1, 3)):
        roll = rng.random()
        if roll < 0.4:
            items.append(rng.choice("abcxyz019"))
        elif roll < 0.6:
            start = rng.choice("abcx0")
            items.append(f"{start}-{chr(ord(start) + rng.randint(0, 3))}")
        elif roll < 0.8:
            items.append(rng.choice(_SHORTHANDS))
        else:
            items.append(rng.choice([r"\-", r"\]", r"\[", r"\\", r"\^"]))
    return "[" + ("^" if rng.random() < 0.3 else "") + "".join(items) + "]"


def _atom(rng: random.Random, depth: int) -> str:
    roll = rng.random()
    if roll < 0.35:
        return rng.choice(_LITERALS)
    if roll < 0.45:
        return rng.choice(_ESCAPED)
    if roll < 0.52:
        return "."
    if roll < 0.62:
        return rng.choice(_SHORTHANDS)
    if roll < 0.75:
        return _class(rng)
    if roll < 0.85 and depth < 2:
        return rng.choice(["(", "(?:"]) + _alternation(rng, depth + 1) + ")"
    return rng.choice(_LITERALS)


def _quantified(rng: random.Random, depth: int) -> str:
    atom = _atom(rng, depth)
    if rng.random() < 0.6:
        return atom
    low = rng.randint(0, 2)
    quantifier = rng.choice(
        ["*", "+", "?", f"{{{low}}}", f"{{{low},}}", f"{{{low},{low + rng.randint(0, 2)}}}"]
    )
    return atom + quantifier + ("?" if rng.random() < 0.2 else "")


def _sequence(rng: random.Random, depth: int) -> str:
    parts = [_quantified(rng, depth) for _ in range(rng.randint(1, 4))]
    if rng.random() < 0.15:
        parts.insert(rng.randint(0, len(parts)), rng.choice([r"\b", r"\B"]))
    return "".join(parts)


def _alternation(rng: random.Random, depth: int) -> str:
    return "|".join(_sequence(rng, depth) for _ in range(rng.randint(1, 2)))


def _pattern(rng: random.Random) -> str:
    body = _alternation(rng, 0)
    if rng.random() < 0.3:
        body = rng.choice(["^", r"\A"]) + body
    if rng.random() < 0.3:
        body += "$"
    return body


def _check_regex(store, codes, spellings, pattern, flags, case, as_str) -> int:
    try:
        compiled = re.compile(pattern, _py_flags(flags))
    except re.error:
        return 0  # native_shape never maps a pattern Python cannot compile
    shape = NativeShape("regex", pattern, flags, case, as_str)
    passed, undecided = candidates.evaluate(store, shape, codes)
    checked = 0
    for code in codes - set(undecided):
        text = _rdflib_text(spellings[code], case, as_str)
        expected = text is not None and compiled.search(text) is not None
        assert (code in passed) == expected, (pattern, flags, case, as_str, spellings[code])
        checked += 1
    return checked


@contextlib.contextmanager
def _time_limit(seconds: float):
    """``TimeoutError`` from inside the block once ``seconds`` have passed
    (SIGALRM, so POSIX only)."""

    def expire(signum, frame):
        raise TimeoutError

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize(
    ("case", "as_str"), [(None, False), (None, True), ("lower", False), ("upper", True)]
)
def test_random_allow_list_patterns_agree_with_python_re(corpus, seed, case, as_str):
    store, codes = corpus
    spellings = {code: store._dict.decode(code) for code in codes}
    rng = random.Random(seed)
    checked = 0
    for _ in range(40):
        flags = rng.choice(["", "i", "s", "m", "is", "im", "sm", "ism"])
        checked += _check_regex(store, codes, spellings, _pattern(rng), flags, case, as_str)
    assert checked > 400


FUNCTIONS = ("contains", "strstarts", "strends")
TEXT_FORMS = ("?v", "str(?v)", "lcase(?v)", "ucase(?v)", "lcase(str(?v))", "ucase(str(?v))")
CONSTANTS = (
    '"a"', '"A"', '"ab"', '"bc"', '"c\\n"', '"\\n"', '"é"', '"É"', '"ss"', '"SS"',
    '"ſ"', '"k"', '""', '"abc"@en', '"x"@fr', '"line"', '"5"', '"4"', '"+"', '"http"',
    '"abc"^^xsd:string',
)  # fmt: skip
STRING_EXPRESSIONS = [
    f"{function}({form}, {constant})"
    for function in FUNCTIONS
    for form in TEXT_FORMS
    for constant in CONSTANTS
]


@pytest.mark.parametrize("sparql_expr", STRING_EXPRESSIONS)
def test_string_functions_agree_with_rdflib(corpus, sparql_expr):
    store, codes = corpus
    expr = filter_expr(sparql_expr)
    shape = filters.native_shape(expr, V, {})
    if shape is None:
        # A tagged constant against STR(): an error for every value in rdflib.
        assert "str(?v)" in sparql_expr and '"@' in sparql_expr
        return
    passed, undecided = candidates.evaluate(store, shape, codes)
    decided = 0
    for code in codes - set(undecided):
        stored = store._dict.decode(code)
        assert (code in passed) == rdflib_answer(expr, stored), (sparql_expr, stored)
        decided += 1
    assert decided > 0, sparql_expr


@pytest.mark.skipif(
    not os.environ.get("VORTEX_RDFLIB_BSBM_STORE"),
    reason="set VORTEX_RDFLIB_BSBM_STORE to a BSBM .vortex store",
)
def test_bsbm_literals_agree_with_python_re():
    """The same differential over a real dictionary's literals: up to 20,000
    literal codes of a BSBM store, random allow-list patterns, and the first
    words of string literals from across those codes as
    CONTAINS/STRSTARTS/STRENDS constants."""
    store = VortexRdflibStore(os.environ["VORTEX_RDFLIB_BSBM_STORE"])
    assert store._dict is not None
    lo, hi = store._kind_ranges()[filters.LITERAL]
    ordered = list(range(lo, hi, max(1, (hi - lo) // 20000)))
    codes = set(ordered)
    spellings = {
        code: spelling
        for code, spelling in zip(ordered, store._dict.decode_many(ordered), strict=True)
        if spelling is not None
    }
    assert len(spellings) == len(ordered)
    rng = random.Random(20261007)
    checked = completed = 0
    for _ in range(60):
        flags = rng.choice(["", "i", "m", "s"])
        pattern = _pattern(rng)
        try:
            # The oracle, Python's backtracking `re`, is exponential on some
            # patterns over long texts: a pattern past the limit is skipped.
            with _time_limit(10):
                checked += _check_regex(store, codes, spellings, pattern, flags, None, False)
            completed += 1
        except TimeoutError:
            continue
    assert completed >= 50
    assert checked >= 20 * len(codes)
    words = []
    sample = list(spellings.values())
    for spelling in sample[:: max(1, len(sample) // 400)]:
        view = parse_spelling(spelling)
        if view.dt in (None, XSD + "string") and view.lex and view.lex.split():
            words.append(view.lex.split()[0])
    assert len(set(words)) >= 20
    tests = {
        "contains": lambda text, word: word in text,
        "strstarts": str.startswith,
        "strends": str.endswith,
    }
    for word in list(dict.fromkeys(words))[:50]:
        for function, test in tests.items():
            shape = NativeShape(function, canonical_spelling(Literal(word)))
            passed, undecided = candidates.evaluate(store, shape, codes)
            decided = 0
            for code in codes - set(undecided):
                view = parse_spelling(spellings[code])
                assert view.lex is not None
                string_like = view.dt in (None, XSD + "string")
                assert (code in passed) == (string_like and test(view.lex, word)), (word, view)
                decided += 1
            assert decided > 0, (function, word)


def test_stored_language_tags_are_lower_case(tmp_path):
    """The dictionary stores every language tag lower-cased, which the string
    shapes rely on: the pushdown and rdflib's evaluator over the store both
    read `"x"@EN-GB` back as `"x"@en-gb`."""
    nt = tmp_path / "tags.nt"
    nt.write_text('<http://ex.org/s> <http://ex.org/p> "x"@EN-GB .\n', encoding="utf-8")
    out = tmp_path / "tags.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    graph = Graph(store=VortexRdflibStore(str(out), in_memory=True))
    [(_, _, literal)] = list(graph.triples((None, None, None)))
    assert isinstance(literal, Literal)
    assert literal.language == "en-gb"
    register_sparql_pushdown()
    for condition in (
        'lang(?o) = "en-gb"',
        'contains(?o, "x"@en-gb)',
        'contains(?o, "x"@EN-GB)',
        'strstarts(?o, "x"@en-gb)',
        'langMatches(lang(?o), "EN")',
    ):
        query = f"SELECT ?s WHERE {{ ?s <http://ex.org/p> ?o FILTER({condition}) }}"
        with_pushdown, without_pushdown = both_ways(graph, query)
        assert with_pushdown == without_pushdown, condition


#: Patterns Python's `re` refuses with an exception other than `re.error`: a
#: repetition count past its limit, and nesting past the recursion limit.
UNCOMPILABLE = {
    "overflow": ("a{99999999999999999999}", OverflowError),
    "recursion": ("(" * 3000 + ")" * 3000, RecursionError),
}


@pytest.mark.parametrize("native", [True, False], ids=["native", "python"])
@pytest.mark.parametrize("string", [False, True], ids=["iris", "string"])
@pytest.mark.parametrize(("pattern", "error"), UNCOMPILABLE.values(), ids=list(UNCOMPILABLE))
def test_a_pattern_re_cannot_compile_answers_as_rdflib_does(
    tmp_path, monkeypatch, native, string, pattern, error
):
    """rdflib compiles a REGEX pattern only for a string text: over IRIs and
    blank nodes the FILTER fails every row, over a string it raises. The
    pushdown plans the FILTER whatever the pattern, through the native shape
    (``native``) or the fast route alone, and raises only where rdflib does."""
    objects = ["<http://ex.org/a>", "<http://ex.org/b>", "_:c", *(['"text"'] if string else [])]
    nt = tmp_path / "objects.nt"
    nt.write_text(
        "".join(f"<http://ex.org/s{i}> <http://ex.org/p> {o} .\n" for i, o in enumerate(objects)),
        encoding="utf-8",
    )
    out = tmp_path / "objects.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    graph = Graph(store=VortexRdflibStore(str(out), in_memory=True))
    monkeypatch.setattr(SETTINGS, "native_filters", native)
    query = f'SELECT ?s ?o WHERE {{ ?s <http://ex.org/p> ?o FILTER(regex(?o, "{pattern}")) }}'
    with_pushdown, without_pushdown = both_ways(graph, query, runner=_outcome)
    assert without_pushdown == (error if string else [])
    assert with_pushdown == without_pushdown
