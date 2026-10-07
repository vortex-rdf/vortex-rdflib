"""BSBM Explore harness on the official BSBM tools.

Data and query streams come from the official generator and the unmodified
official test driver (``Tpt/bsbm-tools``, pinned in ``bench.bsbm.tools``; Java
required), so every query instance carries the parameters the driver drew::

    python -m bench.bsbm.prepare --products 10000 --warmup-mixes 5 --mixes 20 --out DIR
    python -m bench.bsbm.run_stream store.vortex --file DIR/warmup.json DIR/measured.json run.json
    python -m bench.bsbm.compare baseline.json run.json
    python -m bench.run_bench --dataset bsbm --bsbm-dir DIR

Every module imports only the standard library at import time: the dashboard
worker loads them in isolated environments, and the pyoxigraph worker must
never load rdflib.
"""
