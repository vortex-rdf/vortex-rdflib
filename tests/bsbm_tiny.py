"""A BSBM-shaped dataset and query stream small enough for unit tests.

The real streams come from the official tools (Java); unit tests run without
them. The data uses BSBM's vocabulary; the queries follow the official Explore
templates' shapes (DESCRIBE for Q9, CONSTRUCT for Q12) with constants drawn
per instance, and streams are split by ``bench.bsbm.streams.split_streams``.
"""

import json
from pathlib import Path

from bench.bsbm import prepare, streams

VOCAB = "http://www4.wiwiss.fu-berlin.de/bizer/bsbm/v01/vocabulary/"
INST = "http://www4.wiwiss.fu-berlin.de/bizer/bsbm/v01/instances/"
TINY_PRODUCTS = 6
PREFIXES = (
    f"PREFIX bsbm: <{VOCAB}>\n"
    "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
    "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
    "PREFIX rev: <http://purl.org/stuff/rev#>\n"
)


def write_tiny_bsbm(path: Path) -> None:
    rdf_type = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
    label = "<http://www.w3.org/2000/01/rdf-schema#label>"
    xsd = "http://www.w3.org/2001/XMLSchema#"
    lines = [f"<{INST}ProductType{t}> {rdf_type} <{VOCAB}ProductType> ." for t in (1, 2)]
    for n in range(1, TINY_PRODUCTS + 1):
        prod = f"<{INST}dataFromProducer1/Product{n}>"
        offer = f"<{INST}dataFromVendor1/Offer{n}>"
        review = f"<{INST}dataFromRatingSite1/Review{n}>"
        lines += [
            f"{prod} {rdf_type} <{VOCAB}Product> .",
            f"{prod} {rdf_type} <{INST}ProductType{1 + n % 2}> .",
            f'{prod} {label} "alpha beta{n} gamma" .',
            f'{prod} <{VOCAB}productPropertyNumeric1> "{n * 100}"^^<{xsd}integer> .',
            f'{prod} <{VOCAB}productPropertyNumeric2> "{n * 50}"^^<{xsd}integer> .',
            f'{prod} <{VOCAB}productPropertyNumeric3> "{n * 10}"^^<{xsd}integer> .',
            f'{prod} <{VOCAB}productPropertyTextual1> "text {n}"^^<{xsd}string> .',
            *(f"{prod} <{VOCAB}productFeature> <{INST}ProductFeature{f}> ." for f in (1, 2, 3 + n)),
            f"{offer} <{VOCAB}product> {prod} .",
            f'{offer} <{VOCAB}price> "{n}.50"^^<{VOCAB}USD> .',
            f'{offer} <{VOCAB}validTo> "2008-09-1{n}T00:00:00"^^<{xsd}dateTime> .',
            f'{offer} <{VOCAB}deliveryDays> "{n % 4}"^^<{xsd}integer> .',
            f"{review} <{VOCAB}reviewFor> {prod} .",
            f'{review} <http://purl.org/stuff/rev#text> "nice {n}"@en .',
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def tiny_query(q: int, k: int) -> str:
    """Template ``q``'s tiny counterpart, its constants drawn from instance ``k``."""
    n, t, x = 1 + k % TINY_PRODUCTS, 1 + k % 2, (37 * k) % 500
    product = f"<{INST}dataFromProducer1/Product{n}>"
    offer = f"<{INST}dataFromVendor1/Offer{n}>"
    ptype, feature = f"<{INST}ProductType{t}>", f"<{INST}ProductFeature1>"
    after = '"2008-06-20T00:00:00"^^xsd:dateTime'
    bodies = {
        1: f"SELECT DISTINCT ?product ?label WHERE {{ ?product rdfs:label ?label . "
        f"?product a {ptype} . ?product bsbm:productFeature {feature} . "
        f"?product bsbm:productPropertyNumeric1 ?v1 . FILTER (?v1 > {x}) }} "
        "ORDER BY ?label LIMIT 10",
        2: f"SELECT ?label ?text WHERE {{ {product} rdfs:label ?label . "
        f"OPTIONAL {{ {product} bsbm:productPropertyTextual1 ?text }} }}",
        3: f"SELECT ?product ?label WHERE {{ ?product rdfs:label ?label . ?product a {ptype} . "
        f"?product bsbm:productPropertyNumeric1 ?p1 . FILTER (?p1 > {x}) "
        f"?product bsbm:productPropertyNumeric3 ?p3 . FILTER (?p3 < {x // 5 + 30}) "
        f"OPTIONAL {{ ?product bsbm:productFeature <{INST}ProductFeature{3 + n}> . "
        "?product rdfs:label ?testVar } FILTER (!bound(?testVar)) } ORDER BY ?label LIMIT 10",
        4: "SELECT DISTINCT ?product ?label WHERE { "
        f"{{ ?product rdfs:label ?label . ?product a {ptype} . "
        f"?product bsbm:productFeature {feature} . "
        f"?product bsbm:productPropertyNumeric1 ?p1 . FILTER (?p1 > {x}) }} UNION "
        f"{{ ?product rdfs:label ?label . ?product a {ptype} . "
        f"?product bsbm:productFeature <{INST}ProductFeature2> . "
        f"?product bsbm:productPropertyNumeric2 ?p2 . FILTER (?p2 > {x // 2}) }} }} "
        "ORDER BY ?label LIMIT 10",
        5: f"SELECT DISTINCT ?product ?label WHERE {{ ?product rdfs:label ?label . "
        f"FILTER ({product} != ?product) {product} bsbm:productFeature ?f . "
        f"?product bsbm:productFeature ?f . {product} bsbm:productPropertyNumeric1 ?o1 . "
        "?product bsbm:productPropertyNumeric1 ?s1 . "
        "FILTER (?s1 < (?o1 + 250) && ?s1 > (?o1 - 250)) } ORDER BY ?label LIMIT 5",
        6: "SELECT ?product ?label WHERE { ?product rdfs:label ?label . "
        f'?product rdf:type bsbm:Product . FILTER regex(?label, "beta{n}") }}',
        7: f"SELECT ?label ?offer ?price WHERE {{ {product} rdfs:label ?label . "
        f"OPTIONAL {{ ?offer bsbm:product {product} . ?offer bsbm:price ?price . "
        f"?offer bsbm:validTo ?date . FILTER (?date > {after}) }} }}",
        8: f"SELECT ?review ?text WHERE {{ ?review bsbm:reviewFor {product} . "
        '?review rev:text ?text . FILTER langMatches(lang(?text), "EN") } '
        "ORDER BY DESC(?review) LIMIT 20",
        9: f"DESCRIBE <{INST}dataFromRatingSite1/Review{n}>",
        10: f"SELECT DISTINCT ?offer ?price WHERE {{ ?offer bsbm:product {product} . "
        "?offer bsbm:deliveryDays ?days . FILTER (?days <= 3) "
        f"?offer bsbm:price ?price . ?offer bsbm:validTo ?date . FILTER (?date > {after}) }} "
        "ORDER BY xsd:double(str(?price)) LIMIT 10",
        11: f"SELECT ?property ?hasValue ?isValueOf WHERE {{ {{ {offer} ?property ?hasValue }} "
        f"UNION {{ ?isValueOf ?property {offer} }} }}",
        12: f"CONSTRUCT {{ {offer} bsbm:product ?p . {offer} bsbm:price ?price }} "
        f"WHERE {{ {offer} bsbm:product ?p . {offer} bsbm:price ?price }}",
    }
    return PREFIXES + bodies[q]


def tiny_texts(mix: tuple[int, ...] | list[int], mixes: int) -> list[str]:
    return [tiny_query(q, k) for k, q in enumerate(list(mix) * mixes)]


def tiny_streams(warmup_mixes: int, mixes: int) -> tuple[list[dict], list[dict]]:
    mix = list(streams.EXPLORE_MIX)
    return streams.split_streams(tiny_texts(mix, warmup_mixes + mixes), mix, warmup_mixes, mixes)


def write_prepared(out: Path, warmup_mixes: int, mixes: int) -> Path:
    """A directory as ``bench.bsbm.prepare`` writes one, from the tiny data."""
    out.mkdir(parents=True, exist_ok=True)
    write_tiny_bsbm(out / "dataset.nt")
    (out / "td_data").mkdir(exist_ok=True)
    warmup, measured = tiny_streams(warmup_mixes, mixes)
    (out / "warmup.json").write_text(json.dumps(warmup), encoding="utf-8")
    (out / "measured.json").write_text(json.dumps(measured), encoding="utf-8")
    params = prepare.stream_params(TINY_PRODUCTS, prepare.DRIVER_SEED, warmup_mixes, mixes, None)
    dataset = {
        "path": "dataset.nt",
        "td_data": "td_data",
        "from": None,
        **prepare.scan(out / "dataset.nt"),
    }
    templates = {q: tiny_query(q, 0) for q in sorted(set(streams.EXPLORE_MIX))}
    prepare.write_meta(
        out, params, dataset, list(streams.EXPLORE_MIX), templates, (len(warmup), len(measured))
    )
    return out
