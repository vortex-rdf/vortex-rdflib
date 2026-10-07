"""Comparative SPARQL benchmark: VortexRdflibStore vs rdflib vs oxrdflib.

Not part of the published package — run from the repo root:

    uv run python -m bench.run_bench --out bench/results.json

and on the official BSBM data and query streams (``bench/bsbm``; Java):

    uv run python -m bench.bsbm.prepare --products 10000 --warmup-mixes 5 --mixes 20 --out DIR
    uv run python -m bench.run_bench --dataset bsbm --bsbm-dir DIR
"""
