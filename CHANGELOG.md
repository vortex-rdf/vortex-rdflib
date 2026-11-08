# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0](https://github.com/vortex-rdf/vortex-rdflib/compare/v0.4.0...v0.5.0) - 2026-11-08

Requires vortex-rdf 0.15 (`vortex-rdf>=0.15.0,<0.16`, up from 0.13.1), and the
union default graph is a set of triples. A plain `Graph` over the store, and a
`Dataset` whose default graph is the union, yielded a triple that several graphs
hold once per graph; they now yield it once, with every graph that holds it as
its contexts, as rdflib's own `Dataset(default_union=True)` does and SPARQL 1.1
§13 defines the merge. `len()`, `COUNT(*)` and every other query over the union
count it once; `GRAPH ?g` and `quads()` still give one row per graph. A store
whose quads are in one graph, or whose graphs share no triple, reads the union
as before and pays nothing for it: whether a triple repeats is read once per
open file, from one pass over its triple codes when it holds several graphs.
The floor is 0.15.0 because a FILTER's numeric comparison takes vortex-rdf's
SPARQL 1.1 verdict in one native call, and a store without term codes streams
its reads through `iter_quads`, both new in vortex-rdf 0.15 (the union drops a
run of a triple's repeats with `U64Column.run_starts`, from 0.14). Ablation switches turn one
mechanism of the pushdown off at a time for measurement
(`VORTEX_RDF_CODE_PREDICATES=0`, `VORTEX_RDF_COUNT_ORDER=0`,
`VORTEX_RDF_DECODE_EACH_STEP=1`, `VORTEX_RDF_DECODE_CHUNK=<n>`,
`VORTEX_RDF_PROBE_BATCH=<n>`), and `VORTEX_RDF_COUNTERS=1` counts the native
calls, rows, decodes and page faults a run makes. Inside, the pushdown and the
FILTER code are packages of one module per concern, and the settings and the
cancellation have modules of their own.

Python 3.12 to 3.15 are supported; 3.11 is dropped. A FILTER the pushdown
evaluates gives SPARQL 1.1's answer where rdflib's evaluator differs — errors
through `||` and `&&`, `IN`, comparisons across types and of ill-typed literals,
NaN, numeric promotion, language-tag case, `REGEX` flags and patterns — checked
against Oxigraph; rdflib's semantics remain wherever rdflib evaluates
([docs/pushdown.md](docs/pushdown.md#sparql-semantics) lists every case). A
numeric comparison is one native call, where it made two to four, and an
`xsd:float` constant is decided natively: vortex-rdf 0.15 compares numbers as
SPARQL 1.1 does (vortex-rdf/vortex-rdf#141). On a store without term codes (a
layout other than `"dictionary"`, or `VORTEX_RDF_DISABLE_CODE_PATH=1`),
`triples()`, `contexts()` and the union's `len()` stream the match a chunk at a
time: a read holds about one chunk's terms, a cancel stops it at the next
chunk, and a consumer that stops early (`LIMIT`, `ASK`) stops the read (#27). A
triple pattern naming a term vortex-rdf's parser refuses (`<http://x/%zz>`, a
lone surrogate, a second `#`, a language tag BCP 47 refuses) matches nothing
instead of raising, in `triples()`, `contexts()`, `in` and every SPARQL pattern
the pushdown resolves, a context binding and a VALUES table included (#26). An `OPTIONAL` the
pushdown evaluates answers as SPARQL 1.1 does where rdflib's evaluator does not:
it keeps a `VALUES` row no right row matches, and its FILTER sees a left-side
variable an enclosing join binds (#24). An
`OPTIONAL`, a nested group join and an `EXISTS` plan a one-pattern right side
as a BGP's join step does: probed per row only where each probe is a point
lookup, matched once narrowed to the left side's codes where each would scan
the store; an `OPTIONAL` whose condition no row can pass leaves its right side
unread. A query is one trace, whose events are built only when tracing is on,
and the FILTER applied over rows rdflib produced keeps nothing per row. Inside,
the switches are one settings object read when a store is constructed, every
native call goes through one boundary, an open file's state is one record, and
the join planner, the FILTER model, `COUNT` and the block solvers take one
typed path per case.

### Breaking changes

- vortex-rdf 0.13 and 0.14 are no longer supported: the dependency is
  `vortex-rdf>=0.15.0,<0.16`. vortex-rdf 0.15 writes Dictionary-layout files in
  a new edition (`vortexrdf2026.11.0`) that 0.14 and earlier cannot load into
  memory; files written by 0.12 to 0.14 open and answer as before, and
  rebuilding one from its quads gives its reads 0.15's zone statistics.
- The union default graph is a set of triples: a triple in several graphs is
  one triple of the union in `triples()`, `len()` and every query over it. Code
  that counted it once per graph through the union counts it per graph with
  `GRAPH ?g` or `quads()`.
- Python 3.11 is no longer supported: `requires-python = ">=3.12"`, tested on
  3.12 to 3.15.
- A FILTER the pushdown evaluates follows SPARQL 1.1 where rdflib's evaluator
  does not — the FILTER over a block it hands back to rdflib (a join over the
  pair budget) included, so an answer does not depend on the join's size — and
  some answers change:
  - errors through the operators: `T || E` is true, `F && E` and `E && F` are
    false, `!` of an error is an error;
  - `IN` is `=` over its list (an error when nothing matches and a comparison
    errs), `NOT IN` its negation;
  - RDF-term equality: a literal of an unknown or ill-typed datatype equals
    only itself, is unequal to a language-tagged literal, and is otherwise an
    error;
  - orderings within one value space only: NaN orders against nothing, an
    ill-typed literal (in the data, or a constant such as `"300"^^xsd:byte`)
    has no value, `xsd:dateTime` and `xsd:date` follow XML Schema's partial
    order (a value without a timezone and one with it, within 14 hours, are
    neither equal nor unequal, as the W3C suite's open-world date tests
    expect);
  - effective boolean value: NaN and ill-typed numbers and booleans are false;
    unknown datatypes, dates and IRIs are errors;
  - `isNumeric` of an ill-typed literal is false; `sameTerm` and `STR` read a
    term as the data writes it (`"042"^^xsd:integer`); `STR` of a blank node
    is an error;
  - `+` and `-` over XPath's numeric tower, exact for decimals, `xsd:float` at
    single precision; any other operand is an error;
  - the string functions compare language tags case-insensitively;
    `langMatches` takes a simple-literal range;
  - `REGEX` reads XPath's flags (`s m i x q`); an invalid pattern, and a
    language-tagged pattern or flags, is an error;
  - impure functions (`RAND()`, `BNODE()`, ...) inside an `EXISTS` body are
    seen: the FILTER goes to rdflib and is drawn per row.
- An `OPTIONAL` the pushdown evaluates answers as SPARQL 1.1 does where rdflib's
  evaluator does not (#24), checked against Oxigraph. It keeps a `VALUES` row
  that no right row matches, unextended: `VALUES ?o { <http://ex/unknown> }
  OPTIONAL { ?s <p> ?o }` gives the row `?o = <http://ex/unknown>` with `?s`
  unbound, which rdflib dropped whenever the store held a `<p>` triple. Its
  FILTER sees a variable of its left side that an enclosing join binds too,
  which rdflib saw unbound, dropping the row that matched. rdflib still answers
  an `OPTIONAL` it evaluates itself (`LeftJoin` left out of
  `VORTEX_RDF_PUSHDOWN_OPS`, or one under a binding rdflib substituted), and one
  under `GRAPH ?g` whose right side alone binds `?g`, where its answer is the
  specification's.
- `VortexRdflibStore` takes `(configuration, identifier, path, *, in_memory)`
  and raises `TypeError` on any other argument, so a typo such as
  `in_memroy=True` no longer opens the file another way. The unused `layout`
  and `backend` parameters are removed; `store.layout` still reads the open
  file's layout.
- Every `VORTEX_RDF_*` switch is parsed one way: an on/off switch takes
  `1/true/yes/on` or `0/false/no/off` in any case (only `1` and `0` worked for
  some), a size is a whole number in ASCII digits (no sign, no underscore), and
  anything else raises a `ValueError` naming the variable — when a store is
  constructed or the pushdown registered, not on import.
  `VORTEX_RDF_TRACE_TRIPLES` is read when a store is constructed.
  `vortex_rdflib.ablation`, `counters.ON` and `settings.trace_triples()` are
  gone: the fields of `settings.SETTINGS` replace them.
- The query trace (`VORTEX_RDF_TRACE_QUERY=1`) is one trace per query, kept on
  the query's scope: a `UNION`'s branches no longer restart its numbering, and
  a result never read no longer traces later queries. The `order_start` and
  `order_key_ranked` events are gone, as are the `input_codes` field of
  `rank_codes_complete` and `limit_applied_during_order` of `order_complete`
  (each repeated another). `join_plan_complete` and `left_join_plan_complete`
  report a `keep_scan` strategy, and the latter reports `constant_false` for
  any right side. A FILTER applied over the rows of a pattern rdflib solved
  is traced as `filter_over_rdflib_rows`, naming that node (`algebra_node`).
  A traced result prints its lines a decode chunk at a time as it is read,
  rather than holding them until it ends.
- `vortex_rdflib.pushdown.__all__` lists only `register_sparql_pushdown` and
  `unregister_sparql_pushdown`; its modules are internal.

### Added

- Show the CPU that measured each history point (bench) ([`2407e54`](https://github.com/vortex-rdf/vortex-rdflib/commit/2407e54dc2e348e41ec5cddedb02ea390aad6b60) by @julianrojas87)
- Query built stores in a fresh process, and count page faults per query (bench) ([`bee91bb`](https://github.com/vortex-rdf/vortex-rdflib/commit/bee91bbf1e8a11b3284e85ae834e40b3f7f57bbd) by @julianrojas87)
- Compare two sides' synthetic runs round by round (bench) ([`8942239`](https://github.com/vortex-rdf/vortex-rdflib/commit/8942239891a81016c2b4566475bfed8fe56fef17) by @julianrojas87)
- A BSBM cell on a store with the by-copy index (bench) ([`265d0f2`](https://github.com/vortex-rdf/vortex-rdflib/commit/265d0f229582b5e0548601082790b539b2eafdc9) by @julianrojas87)
- Scripts/perf-gate.sh, a paired performance gate (bench) ([`8cd112e`](https://github.com/vortex-rdf/vortex-rdflib/commit/8cd112eeb07b9d89e10cc9fe12477eb51edf663e) by @julianrojas87)
- Keep each query's median page faults in history records (bench) ([`88fb893`](https://github.com/vortex-rdf/vortex-rdflib/commit/88fb893b21f129d8d4bdb912a32e8b803b220170) by @julianrojas87)
- Correct backfilled records for the runner's drift (bench) ([`0e917cb`](https://github.com/vortex-rdf/vortex-rdflib/commit/0e917cb48d7cd17ae0b2b95df19a945ed66ad74e) by @julianrojas87)
- Ablation switches and work counters (pushdown) ([`d7daf62`](https://github.com/vortex-rdf/vortex-rdflib/commit/d7daf62d7baec05dc55e3dbda7bde34b056fc690) by @fothot2)
- Name the dashboard's BSBM tab after its scale (BSBM_10K) (bench) ([`78a168e`](https://github.com/vortex-rdf/vortex-rdflib/commit/78a168ec798cbf5f3940f8a843a31f6adb867bec) by @julianrojas87)

### Changed

- Drop the Python frames around each scoped row and count (pushdown) ([`ebeff3d`](https://github.com/vortex-rdf/vortex-rdflib/commit/ebeff3d4fb3fa41bd91a6f23c931897fae35133e) by @julianrojas87)
- Settings and cancellation get modules of their own ([`5d9ae65`](https://github.com/vortex-rdf/vortex-rdflib/commit/5d9ae655aa82e542dfc928edd18a5e63c1939391) by @claude)
- A typed Pattern, and a Relation for the running join (pushdown) ([`32adfa7`](https://github.com/vortex-rdf/vortex-rdflib/commit/32adfa7665e78ee1127d271fb4e3b9b5f8ecded5) by @claude)
- Build trace events outside the planner (pushdown) ([`49922f7`](https://github.com/vortex-rdf/vortex-rdflib/commit/49922f7971ae6e32234fc9cb7bc697e44733c983) by @claude)
- A join step commits only once it completes (pushdown) ([`a5f6e9b`](https://github.com/vortex-rdf/vortex-rdflib/commit/a5f6e9b914769a92e55336e997aee3b2de7ce20d) by @claude)
- One module per concern (pushdown) ([`5494f18`](https://github.com/vortex-rdf/vortex-rdflib/commit/5494f1829223c91f58bc8ab4cb95c7bd46eca93b) by @claude)
- A package, and one home for the native shapes (filters) ([`2020e33`](https://github.com/vortex-rdf/vortex-rdflib/commit/2020e33844c5509df50ece0fe3852516cd57dcb8) by @claude)
- Drop repeats only where a triple is in two graphs (union) ([`7623786`](https://github.com/vortex-rdf/vortex-rdflib/commit/76237865497d884ab2969d5f299876e684fb0073) by @claude)
- Every switch in SETTINGS, one parser per kind (settings) ([`0993fc4`](https://github.com/vortex-rdf/vortex-rdflib/commit/0993fc4141aed37f6fa18f0a61374202af4f8506) by @claude)
- One native boundary for cancellation, tracing and counters ([`a966aa5`](https://github.com/vortex-rdf/vortex-rdflib/commit/a966aa5e85f845222e641e91dcb8730265f9631e) by @claude)
- Per-file state in one record; unknown keywords raise (store) ([`f6b28ea`](https://github.com/vortex-rdf/vortex-rdflib/commit/f6b28eac978cb21e26b1bceeea3e7a720171e879) by @claude)
- Read the switches once per store (settings) ([`3343807`](https://github.com/vortex-rdf/vortex-rdflib/commit/3343807ba659805b89fc713251c89149682a34e1) by @claude)
- Read the switches without a memo (settings) ([`e9ba9f1`](https://github.com/vortex-rdf/vortex-rdflib/commit/e9ba9f15b988d6fa8b7ce36e16d53838c4a39dcc) by @claude)
- One strategy per join tier, one hash-join kernel (planner) ([`1f254f7`](https://github.com/vortex-rdf/vortex-rdflib/commit/1f254f795eef7a8409806dab2ff9f48a4b45a37b) by @claude)
- One way to split a literal's spelling (terms) ([`5224e4d`](https://github.com/vortex-rdf/vortex-rdflib/commit/5224e4d9d045810429c3135143ce9143ec21d0e9) by @claude)
- Typed native decisions; kind tests decided per kind (filters) ([`57f391e`](https://github.com/vortex-rdf/vortex-rdflib/commit/57f391ec2958eb85cf3de524cfccf8ba6e81f0c0) by @claude)
- Walk an expression off one stack (filters) ([`bcb1afa`](https://github.com/vortex-rdf/vortex-rdflib/commit/bcb1afa61402492e65d6d1ef7cf74289d71675c5) by @claude)
- One place for the query's foreign terms (pushdown) ([`8b11e5a`](https://github.com/vortex-rdf/vortex-rdflib/commit/8b11e5a6b900faac030414abeed49f447efdbf71) by @claude)
- One plan for a one-pattern right side (pushdown) ([`b4de4f5`](https://github.com/vortex-rdf/vortex-rdflib/commit/b4de4f5d3b20b9fcb6064cb6f934f832a503ab91) by @claude)
- Build trace payloads only when the query is traced (trace) ([`df2f97c`](https://github.com/vortex-rdf/vortex-rdflib/commit/df2f97c740ff4ba319d5b91e18346a022f1ef9ff) by @claude)
- Three COUNT cases, resolved once (aggregate) ([`451af27`](https://github.com/vortex-rdf/vortex-rdflib/commit/451af278d32034a858badab6332347b41ea9d53b) by @claude)
- A frozen frame and pushed conjuncts, applied on the relation (pushdown) ([`baccfa2`](https://github.com/vortex-rdf/vortex-rdflib/commit/baccfa2bf4ed9f1713cf8dbbcc438ab770c95bd4) by @claude)
- Typed blocks, a solver per kind, handlers as functions (pushdown) ([`21e8f2e`](https://github.com/vortex-rdf/vortex-rdflib/commit/21e8f2e55da081571f7efcf9e73a1635a99299b1) by @claude)
- Read a VALUES table in one pass (algebra) ([`bf03d3e`](https://github.com/vortex-rdf/vortex-rdflib/commit/bf03d3ed8fe71b5876debf3b5e892b6ff67c749f) by @claude)
- One REGEX recognizer (filters) ([`b1b36ef`](https://github.com/vortex-rdf/vortex-rdflib/commit/b1b36ef863ef737f6570b4301096dcc731f4f593) by @claude)
- A query constant's view built directly (filters) ([`bce5ccc`](https://github.com/vortex-rdf/vortex-rdflib/commit/bce5ccc49b8d04c27d636b0d2bde1eba2e6adfba) by @claude)
- The operand protocol stated once (filters) ([`ac98114`](https://github.com/vortex-rdf/vortex-rdflib/commit/ac98114e303294eae7eb35583b1d11eeca2c0c98) by @claude)
- One table of value spaces (filters) ([`f6dcb2f`](https://github.com/vortex-rdf/vortex-rdflib/commit/f6dcb2fca285b1c5bfa62aff345c488bc85dfc7c) by @claude)
- One tagged decision per conjunct, held by identity (filters) ([`31695d8`](https://github.com/vortex-rdf/vortex-rdflib/commit/31695d8e9c06477bba6940d897be2d51e0cfd2ef) by @claude)
- Native numeric rules as one table (filters) ([`f20c758`](https://github.com/vortex-rdf/vortex-rdflib/commit/f20c75826c77aeeceeb2cef40cd963e452a6f9da) by @claude)
- The open file owns its handle and its codes (store) ([`3d59826`](https://github.com/vortex-rdf/vortex-rdflib/commit/3d59826aaa2fb52fad64a02422bd2f8b48990ee7) by @claude)
- Main's cost per call under a token, one token per query (boundary) ([`67febc2`](https://github.com/vortex-rdf/vortex-rdflib/commit/67febc2f71a2100e9cd27181b6e28079e8ec6449) by @claude)
- The switches as one table; sizes in digits only (settings) ([`3b93eab`](https://github.com/vortex-rdf/vortex-rdflib/commit/3b93eabe8590acffa57ac5173ca5d948809c7c29) by @claude)
- The trace's JSON helpers in the trace module; no ablation module ([`5ba0804`](https://github.com/vortex-rdf/vortex-rdflib/commit/5ba0804dfc3a00bca97e60caf8df59b175b8e127) by @claude)
- Triples() reads through one of two readers (store) ([`270c38b`](https://github.com/vortex-rdf/vortex-rdflib/commit/270c38ba5293884809c2be44478bcef44f043888) by @claude)
- One step ladder for BGP steps and block joins (pushdown) ([`e5fdf4e`](https://github.com/vortex-rdf/vortex-rdflib/commit/e5fdf4ee46e30673ffe3ff2db02ea1a89325e0db) by @claude)
- A probe join counts the probes it sent as it sends them (probes) ([`d81f2cd`](https://github.com/vortex-rdf/vortex-rdflib/commit/d81f2cda65508601e5ed6386066672cf6ec61428) by @claude)
- A step's state derived, its tiers functions, no surgery on remaining (bgp) ([`378dfb0`](https://github.com/vortex-rdf/vortex-rdflib/commit/378dfb0a5b2cb27215950fc58aaf4cb11e680198) by @claude)
- The planner, its trace and the pushed conjuncts in modules of their own (pushdown) ([`f2b697b`](https://github.com/vortex-rdf/vortex-rdflib/commit/f2b697b37d395253b78e244533ccda7849b6e644) by @claude)
- A frame always carries its scope (blocks) ([`3f34ddd`](https://github.com/vortex-rdf/vortex-rdflib/commit/3f34ddd6636f549b11f0fa5dfd0901b9cf922bc2) by @claude)
- A VALUES block's rows are tuples in variable order (values) ([`6c4437e`](https://github.com/vortex-rdf/vortex-rdflib/commit/6c4437eb5bf7fd4bbf70782ccfe78a7f0ffce5cd) by @claude)
- FILTER and OPTIONAL blocks carry their own expressions (blocks) ([`cfe1e05`](https://github.com/vortex-rdf/vortex-rdflib/commit/cfe1e05d0b8b5a3490c0ddc4d3b7a5f7a64f8f0a) by @claude)
- COUNT cases that carry their behaviour (aggregate) ([`1c96486`](https://github.com/vortex-rdf/vortex-rdflib/commit/1c96486ecc226996efd2c96c97bcaa1d0d5758d2) by @claude)
- Stream reads without term codes through iter_quads (#27) (store) ([`3feb77b`](https://github.com/vortex-rdf/vortex-rdflib/commit/3feb77b9561a6f5a6d7e48786309b470a36fb9d5) by @claude)
- Let a streamed quad's tuple go as soon as it is unpacked (store) ([`944787b`](https://github.com/vortex-rdf/vortex-rdflib/commit/944787b14851db2a23b0fe206ff8b80ad3e42ac8) by @claude)
- Decide non-ASCII IRIs and BCP 47 tags locally (terms) ([`c3e1b6e`](https://github.com/vortex-rdf/vortex-rdflib/commit/c3e1b6e122c5d6aae121f7427fea2a93d46839c3) by @claude)
- Stream the right side of an OPTIONAL over an UNDEF key (pushdown) ([`7e6b383`](https://github.com/vortex-rdf/vortex-rdflib/commit/7e6b3834f64f5b8492f041ee2bb6e6e57b81aaa9) by @claude)
- Refused is any_refused of one spelling (boundary) ([`8c1672f`](https://github.com/vortex-rdf/vortex-rdflib/commit/8c1672f779cf8d19b06a02de6e3c6eca30a02034) by @claude)

### Fixed

- Keep a commit's newest measurement wherever records are placed (bench) ([`bcd5a98`](https://github.com/vortex-rdf/vortex-rdflib/commit/bcd5a98d8c614ab8e89ced1747981b35494dc7bf) by @julianrojas87)
- Wrap() keeps the native types; ty-clean ablation test (counters) ([`ec67338`](https://github.com/vortex-rdf/vortex-rdflib/commit/ec67338f8f6a547d62c72dd8fcad338be6fef31c) by @fothot2)
- Import resource only where it exists (not on Windows) (counters) ([`c531eb7`](https://github.com/vortex-rdf/vortex-rdflib/commit/c531eb7d4643e42ab76fba5e14f21f094a95aadf) by @fothot2)
- The union default graph is a set of triples ([`33bde65`](https://github.com/vortex-rdf/vortex-rdflib/commit/33bde65b0c0fdf602008b83cb3e3bbccbecc1e50) by @fothot2)
- Follow SPARQL 1.1 where rdflib does not (filters) ([`d40c233`](https://github.com/vortex-rdf/vortex-rdflib/commit/d40c233e9a39007cc5eb6772bfd62c3b0e126f8e) by @claude)
- See impure functions inside EXISTS bodies (filters) ([`780ec63`](https://github.com/vortex-rdf/vortex-rdflib/commit/780ec631eac76378b77615c71b6f19b6dbe67983) by @claude)
- One trace per query, kept on its scope (trace) ([`77d19ba`](https://github.com/vortex-rdf/vortex-rdflib/commit/77d19bab0a75afed6e222e26a660a5774abdb9cc) by @claude)
- Resolve the graph once, and merge the union by code (store) ([`e7bda4b`](https://github.com/vortex-rdf/vortex-rdflib/commit/e7bda4b45341783c6d64431ec38861322fb049fc) by @claude)
- The FILTER over rdflib's rows keeps nothing per row (filters) ([`ab41173`](https://github.com/vortex-rdf/vortex-rdflib/commit/ab411738a64d80fe2178122024ef1903898adf67) by @claude)
- One constant evaluator and one REGEX check for both routes (filters) ([`e4d9153`](https://github.com/vortex-rdf/vortex-rdflib/commit/e4d91534052ec72adf6c7199f518b1d1f6229897) by @claude)
- One IRI check, the package's; a refused IRI names no term (filters) ([`ca48ea0`](https://github.com/vortex-rdf/vortex-rdflib/commit/ca48ea0ed4a8d6c4a340ec858b95d287beb47f7c) by @claude)
- The open file's record refers to no store (store) ([`a58dd11`](https://github.com/vortex-rdf/vortex-rdflib/commit/a58dd11d46f9857b6cc7ab6a02cf90f162c3467b) by @claude)
- A traced result flushes per chunk (trace) ([`9585528`](https://github.com/vortex-rdf/vortex-rdflib/commit/958552810b8b1dc5cd6d6c83cc05f671475e8963) by @claude)
- A pattern naming a term vortex-rdf refuses matches nothing (#26) (store) ([`1a1f444`](https://github.com/vortex-rdf/vortex-rdflib/commit/1a1f444de4464af338ab50254165587a233e97ef) by @claude)
- A constant vortex-rdf refuses matches nothing (#26) (pushdown) ([`05faf90`](https://github.com/vortex-rdf/vortex-rdflib/commit/05faf905b856f9d54ec8d9add4f94a411f33f125) by @claude)
- Answer OPTIONAL over VALUES, and a condition reading a left variable, per SPARQL 1.1 (#24) (pushdown) ([`b51c429`](https://github.com/vortex-rdf/vortex-rdflib/commit/b51c42973df4a4efc00ebe3660ddba3fadce9e11) by @claude)
- A VALUES term vortex-rdf refuses keeps its unmatched OPTIONAL row (pushdown) ([`6b8e82a`](https://github.com/vortex-rdf/vortex-rdflib/commit/6b8e82a51dbb75e93d1b828ad032f5675d881186) by @claude)

## [0.4.0](https://github.com/vortex-rdf/vortex-rdflib/compare/v0.3.0...v0.4.0) - 2026-10-24

Requires vortex-rdf 0.13.1 (`vortex-rdf>=0.13.1,<0.14`, up from 0.12.1): joins
are budgeted, long native calls can be cancelled, and large reads stream. A
join step of a basic graph pattern has a pair budget (`VORTEX_RDF_PAIR_BUDGET`,
16,777,216 row pairs by default), and a step over it is not built whole: the
last step streams its pairs a piece at a time as the rows are read, or another
pattern that provably fits is joined first, or rdflib finishes the remaining
patterns row by row. A probe join whose probes would each scan the store is a
keep-scan instead: the pattern is matched once, narrowed to the codes the
relation binds. `vortex_rdflib.cancel_scope(token)` gives the queries started
inside it a `vortex_rdf.CancelToken`, and cancelling it, from any thread,
raises `vortex_rdf.Cancelled` at the next point that checks it: a native call
that races the token, the check before every other one-shot call, a piece of a
streamed join, a row rdflib finishes, a per-row probe, or a slice or decode
chunk of the rows being read, so a result whose rows are decoded as they are
read stops within one decode chunk, at most 4,096 rows. `triples()` reads a
pattern of more than 65,536 rows in chunks of that many, so a scan of a large
store holds one chunk of codes at a time. The floor is 0.13.1 because that
first chunk is a limited match, and 0.13.1 reads a limited match that the limit
cannot cut as it reads the match without one (vortex-rdf
[#104](https://github.com/vortex-rdf/vortex-rdf/issues/104)).

### Breaking changes

- vortex-rdf 0.12 and 0.13.0 are no longer supported: the dependency is
  `vortex-rdf>=0.13.1,<0.14`.

### Added

- A pair budget guards every BGP join step (pushdown) ([`d34a54e`](https://github.com/vortex-rdf/vortex-rdflib/commit/d34a54e85db9b5ebbcf9f2624974eac244f51f36) by @julianrojas87)
- Stream an over-budget last join step through CodeGroups (pushdown) ([`e37db53`](https://github.com/vortex-rdf/vortex-rdflib/commit/e37db53e917aefd61a5a2b7f875c3041003a9924) by @julianrojas87)
- Reorder around an over-budget step, else let rdflib finish the rest per row (pushdown) ([`439d828`](https://github.com/vortex-rdf/vortex-rdflib/commit/439d8287fab91df1a01a9c77ac31e523a541c7ca) by @julianrojas87)
- A per-query cancel token for every long native call ([`b71d6ff`](https://github.com/vortex-rdf/vortex-rdflib/commit/b71d6ff6dba7280eba41697f4c4f31691f60481e) by @julianrojas87)
- Triples() reads a large pattern one chunk at a time (store) ([`a9ffe48`](https://github.com/vortex-rdf/vortex-rdflib/commit/a9ffe482b65288d21320adefd903f92e85fe36fe) by @julianrojas87)

### Changed

- One keep-scan instead of a scan per row, and an explicit probe lookahead (pushdown) ([`5f6457a`](https://github.com/vortex-rdf/vortex-rdflib/commit/5f6457ae42b7e7634f80f52a1bb37d9da926a717) by @julianrojas87)
- Triples() tells a large pattern by its first match, not a count (store) ([`d8f9104`](https://github.com/vortex-rdf/vortex-rdflib/commit/d8f91044deebcf73a7fd12ea2bf696d000a7064e) by @julianrojas87)
- Answer plain probes without the token plumbing (pushdown) ([`a25c846`](https://github.com/vortex-rdf/vortex-rdflib/commit/a25c846259cebb7cfd8b36ceb7a8916bdb96e273) by @julianrojas87)
- Count a hash step's pairs once (pushdown) ([`6c8af68`](https://github.com/vortex-rdf/vortex-rdflib/commit/6c8af680e400375a0bb698d78fa05c853ff3937e) by @julianrojas87)
- Point-lookup batches take the binding's default lookahead (pushdown) ([`e3eeab7`](https://github.com/vortex-rdf/vortex-rdflib/commit/e3eeab769535ed9b2b493913f48d16556d25c8b5) by @julianrojas87)

### Fixed

- Record and replay vortex-rdf 0.13 batch iterators (bench) ([`323c97c`](https://github.com/vortex-rdf/vortex-rdflib/commit/323c97c2b8bdd04843821ac7a8d2800f0be9a24d) by @julianrojas87)
- Undo a put-back step's specialized conjuncts (pushdown) ([`9b69fa9`](https://github.com/vortex-rdf/vortex-rdflib/commit/9b69fa919eec72d65470a1b8e51e1078a832ed66) by @julianrojas87)
- Count the probes a tripped step sent, and key the memo by the keep column (pushdown) ([`9868e2a`](https://github.com/vortex-rdf/vortex-rdflib/commit/9868e2aa5c318f3cd1f6c98acdf0ca6ebc24451c) by @julianrojas87)
- Check the cancel token before a string-fallback read (store) ([`4409609`](https://github.com/vortex-rdf/vortex-rdflib/commit/44096098ac1c8679e22bcc7e8f94c1cceb95ea55) by @julianrojas87)
- Triples() lets go of every column of a chunk before the next pull (store) ([`7452b04`](https://github.com/vortex-rdf/vortex-rdflib/commit/7452b04a264eb0b8af54f5eae0a75740d0b9ee1e) by @julianrojas87)
- Check the cancel token per slice and per decode chunk (pushdown) ([`49c062f`](https://github.com/vortex-rdf/vortex-rdflib/commit/49c062f7669777644b67334ef5ca77132079774d) by @julianrojas87)

## [0.3.0](https://github.com/vortex-rdf/vortex-rdflib/compare/v0.2.0...v0.3.0) - 2026-10-13

Requires vortex-rdf 0.12.1 (`vortex-rdf>=0.12.1,<0.13`, up from 0.11): file-backed
stores are memory-mapped, FILTER predicates are evaluated by vortex-rdf over
the codes each query produces — never over the whole dictionary — and nothing
a query learns about terms outlives it. Removed: the `max_resident_bytes`
argument (a `TypeError` now) and the `VORTEX_RDF_DICT_MAX_RESIDENT_BYTES`
handling. Stores written by vortex-rdf 0.11 or earlier cannot be opened;
rebuild them from their RDF source with `serialize_rdf`.

### Breaking changes

- The `max_resident_bytes` argument is removed: passing it raises `TypeError`.
  `VORTEX_RDF_DICT_MAX_RESIDENT_BYTES` is no longer read.
- `in_memory` is keyword-only: `VortexRdflibStore(path, *, in_memory=False)`.

### Added

- Fresh-constants mode and peak anonymous memory (bench) ([`95bb9dc`](https://github.com/vortex-rdf/vortex-rdflib/commit/95bb9dc96d88befe51e79e57b42b57a961234541) by @julianrojas87)
- CodSpeed fresh-constant FILTER benchmark (bench) ([`cba01e9`](https://github.com/vortex-rdf/vortex-rdflib/commit/cba01e91df747dc475f439de150768b7d512f05a) by @julianrojas87)
- Pinned official BSBM tools, capture endpoint and stream split (bench) ([`e3b5a55`](https://github.com/vortex-rdf/vortex-rdflib/commit/e3b5a55950529ed084b49c8b64833dde934f7724) by @julianrojas87)
- Prepare official BSBM data and driver streams, cached (bench) ([`8cd3068`](https://github.com/vortex-rdf/vortex-rdflib/commit/8cd3068b89622006af396b7720c72ea6d68189f0) by @julianrojas87)
- Run a BSBM stream with digests, timeouts, budget and RssAnon (bench) ([`e920a82`](https://github.com/vortex-rdf/vortex-rdflib/commit/e920a82d044d7adacf7cbcd0088d45ed85c61b09) by @julianrojas87)
- Paired per-template comparison of BSBM runs (bench) ([`a8834c2`](https://github.com/vortex-rdf/vortex-rdflib/commit/a8834c28cf35c996e8c8df103b42491dc6d298f1) by @julianrojas87)
- Record a BSBM stream's native calls and replay them (bench) ([`95b417c`](https://github.com/vortex-rdf/vortex-rdflib/commit/95b417ce025b33ba46f01ebf6097eb392c21683f) by @julianrojas87)
- BSBM dataset for the dashboard bench, answer checks and merge (bench) ([`5f8764f`](https://github.com/vortex-rdf/vortex-rdflib/commit/5f8764f80268ae75d01e21aa839b7ddf58cb8954) by @julianrojas87)
- History records of the BSBM dataset (bench) ([`850175d`](https://github.com/vortex-rdf/vortex-rdflib/commit/850175dc867c075c3a144b333f7d78c0c18ebc50) by @julianrojas87)
- Synthetic and BSBM tabs in every section, BSBM panels (dashboard) ([`393a65e`](https://github.com/vortex-rdf/vortex-rdflib/commit/393a65e7b15e5a2f0ac906522ba8af3e01b326b7) by @julianrojas87)
- Refresh.sh bsbm stage, render both datasets (bench) ([`0512f45`](https://github.com/vortex-rdf/vortex-rdflib/commit/0512f458dbeca5b9eb272f613103b96d27e2e884) by @julianrojas87)
- Open stores memory-mapped on vortex-rdf 0.12 (store) ([`c143aab`](https://github.com/vortex-rdf/vortex-rdflib/commit/c143aab4e319bcdc4b4a14df22ef0be422ed0594) by @julianrojas87)
- Evaluate native FILTER predicates over candidate codes (filters) ([`5ae9502`](https://github.com/vortex-rdf/vortex-rdflib/commit/5ae9502c9220d352b711c966cc40171abe1dfd1a) by @julianrojas87)
- Native REGEX, CONTAINS, STRSTARTS and STRENDS shapes (filters) ([`d4560d4`](https://github.com/vortex-rdf/vortex-rdflib/commit/d4560d4e3e51ea34d2ea05ad1b10d56f9feb2576) by @julianrojas87)
- IRI equality, IN and sameTerm as keeps (filters) ([`39b6a4a`](https://github.com/vortex-rdf/vortex-rdflib/commit/39b6a4a3319d1cd3c0ac9ee3813c327c6186837e) by @julianrojas87)
- Keep term caches for one query only (store) ([`d164a49`](https://github.com/vortex-rdf/vortex-rdflib/commit/d164a499d2c0cc38820f079020cc0efb54a809f6) by @julianrojas87)

### Changed

- Share /proc memory readings, add RssAnon (bench) ([`8208b30`](https://github.com/vortex-rdf/vortex-rdflib/commit/8208b305e2dec3e56ce229f96da8c2d9da113d30) by @julianrojas87)
- Compact restricted matches natively, specialize bands as candidates (pushdown) ([`3c49e89`](https://github.com/vortex-rdf/vortex-rdflib/commit/3c49e89389abc95434df7647bcf4741df13e7e02) by @julianrojas87)
- Share conjunct compilation and restriction counts; drop dead row paths (pushdown) ([`29f8e15`](https://github.com/vortex-rdf/vortex-rdflib/commit/29f8e15d0cb8dbac6686fc6e43e1c344c21fb475) by @julianrojas87)
- Rank IRIs by their spelling, without building terms (pushdown) ([`45ec675`](https://github.com/vortex-rdf/vortex-rdflib/commit/45ec675096531264b52019f7b4beacfd6d6140ed) by @julianrojas87)
- Skip counts next to small patterns and cap decision counts (pushdown) ([`de98f18`](https://github.com/vortex-rdf/vortex-rdflib/commit/de98f188dd4dba7f645975203711ebc2edc3a5a2) by @julianrojas87)
- Decode the values rdflib's evaluator reads in one batch (filters) ([`e04a8a4`](https://github.com/vortex-rdf/vortex-rdflib/commit/e04a8a43c5136a8b255f11fc29ddb166b5057fce) by @julianrojas87)
- Send mid-size code-set keeps natively on vortex-rdf 0.12 (pushdown) ([`a7f46af`](https://github.com/vortex-rdf/vortex-rdflib/commit/a7f46afda8aa89496d098a0881df6b9e092381bf) by @julianrojas87)
- Drop the unreachable split-keep plumbing (pushdown) ([`a0b07c0`](https://github.com/vortex-rdf/vortex-rdflib/commit/a0b07c0c2198a52128ba29651336f68b004ae0a3) by @julianrojas87)
- Classify DISTINCT aliases by spelling, building only non-canonical typed literals (pushdown) ([`7a4d7a5`](https://github.com/vortex-rdf/vortex-rdflib/commit/7a4d7a5a250d7046edd843b98504e3022523248b) by @julianrojas87)
- Build decoded IRIs and simple literals directly, checked against rdflib's constructors at import (store) ([`45b695c`](https://github.com/vortex-rdf/vortex-rdflib/commit/45b695cd47c0ca7f2b00b767252053cc90631182) by @julianrojas87)
- Decode triples() lazily, in growing chunks (store) ([`e2192b7`](https://github.com/vortex-rdf/vortex-rdflib/commit/e2192b78b77546cb2ff9f92d8a7b040ddfb89c37) by @julianrojas87)

### Fixed

- Fresh constants of paired queries step apart (bench) ([`0816ee3`](https://github.com/vortex-rdf/vortex-rdflib/commit/0816ee32b6eb525c9694b1bd862ed46097f44cc3) by @julianrojas87)
- BSBM tools download failures skip cleanly (bench) ([`f80f322`](https://github.com/vortex-rdf/vortex-rdflib/commit/f80f322d68a7140ab1889217a07ed22ec12bdcc2) by @julianrojas87)
- Prepare never writes through --from links (bench) ([`e1e3afe`](https://github.com/vortex-rdf/vortex-rdflib/commit/e1e3afe28ccfd621c96caf7c4c22d7322cf188e9) by @julianrojas87)
- Native ASK detection and zero limits in the BSBM stream (bench) ([`4c683ae`](https://github.com/vortex-rdf/vortex-rdflib/commit/4c683aed7cf8d23202f2e4232a2fb083f7873f3a) by @julianrojas87)
- Compare decides flatness per round and flags template mismatches (bench) ([`14e0d6b`](https://github.com/vortex-rdf/vortex-rdflib/commit/14e0d6b060c046a5c6d2edf93ac6457245ff6c34) by @julianrojas87)
- Replay counts every rejected native call; recorder reuses run_instance (bench) ([`4c5d383`](https://github.com/vortex-rdf/vortex-rdflib/commit/4c5d3838f1680462b6068101a92384e5f4b8de94) by @julianrojas87)
- BSBM merge names stores a cut-short job never reached (bench) ([`eff1415`](https://github.com/vortex-rdf/vortex-rdflib/commit/eff1415eaab39f0dd5841b0bcbe4d661d410db4a) by @julianrojas87)
- A configuration's BSBM timeout drops its template from history (bench) ([`177cf5d`](https://github.com/vortex-rdf/vortex-rdflib/commit/177cf5db9115780dc7c1b6f47847713527fd91b0) by @julianrojas87)
- UTF-8 test I/O, escaped failure text, limits-off and history defaults (dashboard) ([`c40fa52`](https://github.com/vortex-rdf/vortex-rdflib/commit/c40fa528e54486c6f627de6c76ab8b704df09c51) by @julianrojas87)
- Timeouts a store converts stay timeouts; work dir, limits, CLI guards (bench) ([`3a64557`](https://github.com/vortex-rdf/vortex-rdflib/commit/3a64557bc83e9c3ec23c6cb8837510d4a907de07) by @julianrojas87)
- Decode literals as rdflib's N-Triples parser reads them (store) ([`ace3157`](https://github.com/vortex-rdf/vortex-rdflib/commit/ace315700dfa4729628798450c5c4eaa56fb7c59) by @julianrojas87)
- Decode literals without rdflib's parser validation flag (store) ([`68383d4`](https://github.com/vortex-rdf/vortex-rdflib/commit/68383d4525c0c49ec546a86719d8a0e70aaa21d8) by @julianrojas87)
- Leave codes outside the term kinds undecided (filters) ([`bfadceb`](https://github.com/vortex-rdf/vortex-rdflib/commit/bfadceb7dc63c39f27f35528b6090180134e6678) by @julianrojas87)
- A FILTER sees only its own group's bindings, as rdflib does (pushdown) ([`fb82ddb`](https://github.com/vortex-rdf/vortex-rdflib/commit/fb82ddbe194d0b836ee1f451a9efc817a0350eb3) by @julianrojas87)
- GRAPH ?g, MINUS and EXISTS shapes answer as rdflib does (pushdown) ([`1b77548`](https://github.com/vortex-rdf/vortex-rdflib/commit/1b775481dd64941193431c695ba55c7803149e52) by @julianrojas87)
- An EXISTS with a disjoint MINUS evaluates as rdflib does (pushdown) ([`aaa63e1`](https://github.com/vortex-rdf/vortex-rdflib/commit/aaa63e106d9b7aae38a9b43e9171ea7842d072b9) by @julianrojas87)
- An EXISTS nested in an expression with a MINUS body evaluates as rdflib does (pushdown) ([`7be22f2`](https://github.com/vortex-rdf/vortex-rdflib/commit/7be22f25520b54f0db80f4652ae04af43c7991a9) by @julianrojas87)
- An EXISTS sees its enclosing row and graph, as rdflib does (pushdown) ([`7ce50ed`](https://github.com/vortex-rdf/vortex-rdflib/commit/7ce50ed65ecb10054636a409a853975e51770603) by @julianrojas87)
- An EXISTS re-entered with a row sees only its Filter's variables, as rdflib does (pushdown) ([`9b04d74`](https://github.com/vortex-rdf/vortex-rdflib/commit/9b04d74c3a4f850eb3267c524f393e640c045245) by @julianrojas87)
- Apply a FILTER on a variable that only a VALUES table binds (pushdown) ([`42ee36d`](https://github.com/vortex-rdf/vortex-rdflib/commit/42ee36dd7543f1688f97b6314134c9abc8566eb4) by @julianrojas87)
- A FILTER re-entered with a row forgets its VALUES variable, as rdflib does (pushdown) ([`07aba53`](https://github.com/vortex-rdf/vortex-rdflib/commit/07aba531167020d731fa6f1d3597a57f06b6049a) by @julianrojas87)
- A pattern re cannot compile declines as rdflib does; stronger string-test floors (filters) ([`0f9d793`](https://github.com/vortex-rdf/vortex-rdflib/commit/0f9d79344405bec01414110023a6679294d8a16a) by @julianrojas87)
- Read IN () as the empty list, as rdflib does (filters) ([`3307ad5`](https://github.com/vortex-rdf/vortex-rdflib/commit/3307ad50ac72dafa0c437968bf3687002aa0c624) by @julianrojas87)
- Offer the query node to later rdflib evaluators; pin scope release (pushdown) ([`75c3b6e`](https://github.com/vortex-rdf/vortex-rdflib/commit/75c3b6eaa21aafaa6b3d64b10509d4397f4a8d1c) by @julianrojas87)
- ORDER BY ranks IRIs by their bare string, as rdflib does (pushdown) ([`a6511c1`](https://github.com/vortex-rdf/vortex-rdflib/commit/a6511c10eebe71cfe3c1a7f6250ff134590a0564) by @julianrojas87)
- A probe join keeps the union default graph's multiplicity, as rdflib does (pushdown) ([`df569bb`](https://github.com/vortex-rdf/vortex-rdflib/commit/df569bb1d77d54930e8913b4f355cd0defcb0565) by @julianrojas87)
- Spell a probe's query constant as the native parser reads it (pushdown) ([`f0c099d`](https://github.com/vortex-rdf/vortex-rdflib/commit/f0c099da0611f296aa2553682c6965ace413f2df) by @julianrojas87)
- A literal graph name matches nothing (store) ([`767e2b7`](https://github.com/vortex-rdf/vortex-rdflib/commit/767e2b7371b20c7965877e9e9207fc4de8355292) by @julianrojas87)
- A graph name the native parser rejects matches nothing (pushdown) ([`ef3cf62`](https://github.com/vortex-rdf/vortex-rdflib/commit/ef3cf62a68d36b768344c22f1f7618728a036568) by @julianrojas87)
- A term the native parser rejects matches nothing (pushdown) ([`20102fe`](https://github.com/vortex-rdf/vortex-rdflib/commit/20102fee7d44920eb5f95b44559c64a7bd2d3031) by @julianrojas87)

## [0.2.0](https://github.com/vortex-rdf/vortex-rdflib/compare/v0.1.3...v0.2.0) - 2026-10-05

Requires vortex-rdf 0.11 (`vortex-rdf>=0.11,<0.12`, up from 0.10): most of the
SPARQL pushdown now runs on its native primitives.

### Added

- Report each query's evaluation apart from rdflib's parse (bench) ([`f7dca63`](https://github.com/vortex-rdf/vortex-rdflib/commit/f7dca630ba4279a9e231706df337ab47ff09c9b2) by @julianrojas87)
- Measure pyoxigraph directly, add file pushdown-off row (bench) ([`c281aed`](https://github.com/vortex-rdf/vortex-rdflib/commit/c281aed286e200ea010b96096a0e414891603012) by @julianrojas87)
- Save each store's row counts in results.json (bench) ([`09286ab`](https://github.com/vortex-rdf/vortex-rdflib/commit/09286abee7ed6b0b464198cbb7e21fb517d0c7fa) by @julianrojas87)
- History records of the vortex configurations against rdflib (bench) ([`af90135`](https://github.com/vortex-rdf/vortex-rdflib/commit/af901351f48751379cf4e18d46e1ad586261eae7) by @julianrojas87)
- Build the history series and embed them in the dashboard (bench) ([`6ace2a7`](https://github.com/vortex-rdf/vortex-rdflib/commit/6ace2a7abea2023a2a381c04e76d0dbf7917f60b) by @julianrojas87)
- Per-commit speedup chart over rdflib (dashboard) ([`12d588f`](https://github.com/vortex-rdf/vortex-rdflib/commit/12d588f1820a3a88851b537d110670c5fa849ee7) by @julianrojas87)
- Plot the working tree on the history chart with refresh.sh --history (bench) ([`16f213a`](https://github.com/vortex-rdf/vortex-rdflib/commit/16f213a186662fc083dd909e2dca4beef88a5194) by @julianrojas87)
- Adopt vortex-rdf 0.11 primitives (pushdown) ([`f61f84d`](https://github.com/vortex-rdf/vortex-rdflib/commit/f61f84d1d90e27a59fe435c4c78989e386f72e41) by @claude)

### Fixed

- Harden the history chart's page embedding and workflows (bench) ([`c45ff81`](https://github.com/vortex-rdf/vortex-rdflib/commit/c45ff81c9987a5c3adb697b679e2693a7f4509f5) by @julianrojas87)
- Reorganize dashboard entries (bench) ([`1c60928`](https://github.com/vortex-rdf/vortex-rdflib/commit/1c6092897e204678eb685f0408a1e14c6ce81846) by @julianrojas87)
- Minor text adjustments in the dashboard (bench) ([`54d6d57`](https://github.com/vortex-rdf/vortex-rdflib/commit/54d6d579d6f1fd684ee21879750c09da7aecafab) by @julianrojas87)
- Defer out-of-range typed constants to rdflib (filters) ([`367c9c6`](https://github.com/vortex-rdf/vortex-rdflib/commit/367c9c64be12eef7e51d2b15b0b121f8899e0183) by @claude)

## [0.1.3](https://github.com/vortex-rdf/vortex-rdflib/compare/v0.1.2...v0.1.3) - 2026-09-29

### Changed

- Optimize BSBM Q05 joins and filters ([`bf1dfac`](https://github.com/vortex-rdf/vortex-rdflib/commit/bf1dfac0fd0579bdff95f14909251d530cafbc51) by @fothot2)
- Keep file-backed dictionaries resident for pushdown (store) ([`c80b557`](https://github.com/vortex-rdf/vortex-rdflib/commit/c80b55724d71694dd9267d38a37cf1e58694bc98) by @fothot2)
- Prune mid-join only when a later pattern can be probed (pushdown) ([`0a7aa8c`](https://github.com/vortex-rdf/vortex-rdflib/commit/0a7aa8cbe04605237a672f415fdd1f601acda9f8) by @julianrojas87)

### Fixed

- Explicit validation of callable conjuct.fast ([`80b80a5`](https://github.com/vortex-rdf/vortex-rdflib/commit/80b80a5acbe5a9ea7367609e23d3030d11c49863) by @fothot2)
- Documentation links ([`291542a`](https://github.com/vortex-rdf/vortex-rdflib/commit/291542a9330e444a96f6e36794b5ec316db69ee1) by @fothot2)
- Remove dead code and fix latent review findings ([`00e077f`](https://github.com/vortex-rdf/vortex-rdflib/commit/00e077ff4d81fc9af012f9b551e6b4eebda90f22) by @julianrojas87)
- Scope the residency default and add filter-band-probe (store,bench) ([`4b96350`](https://github.com/vortex-rdf/vortex-rdflib/commit/4b963502cbb98c07a2594f91c0a1fa75401170cd) by @julianrojas87)
- Unify pushed filters, share decodes, reconcile the trace (pushdown) ([`12ecac1`](https://github.com/vortex-rdf/vortex-rdflib/commit/12ecac1f2f982c61d3c2c6cef59a6511ebd9da71) by @julianrojas87)

## [0.1.2](https://github.com/vortex-rdf/vortex-rdflib/compare/v0.1.1...v0.1.2) - 2026-09-08

### Added

- Add opt-in SPARQL pushdown diagnostics ([`f961b87`](https://github.com/vortex-rdf/vortex-rdflib/commit/f961b8788557391c48eef4bbafe0d25ebd5a1dc2) by @fothot2)
- Add pattern-level BGP query diagnostics ([`6c36662`](https://github.com/vortex-rdf/vortex-rdflib/commit/6c36662496b716efb178265ec6c3f0ceda3bfaa5) by @fothot2)

### Changed

- Defer BGP filters to selective probe joins ([`5092f73`](https://github.com/vortex-rdf/vortex-rdflib/commit/5092f73fdddf002a7daa2160c1f3410929f36057) by @fothot2)
- Plan BGP joins through incremental native probes ([`14940cb`](https://github.com/vortex-rdf/vortex-rdflib/commit/14940cbcfa9813476037f6c3b35d8d1fb971e087) by @fothot2)
- Plan optional probes before matching right patterns ([`9721a50`](https://github.com/vortex-rdf/vortex-rdflib/commit/9721a509dedef55939e4d766153a7c48a966be29) by @fothot2)
- Plan BGP and OPTIONAL joins from native counts (pushdown) ([`6871a2f`](https://github.com/vortex-rdf/vortex-rdflib/commit/6871a2f8568c9e0a39a02abd6035e914f6348415) by @julianrojas87)

## [0.1.1](https://github.com/vortex-rdf/vortex-rdflib/compare/v0.1.0...v0.1.1) - 2026-09-03

### Changed

- Compile integer arithmetic in FILTER pushdown ([`4a427a6`](https://github.com/vortex-rdf/vortex-rdflib/commit/4a427a6f37350ed797dd2afadaa2a9b181505d6f) by @fothot2)

### Fixed

- Defer a comparison whose operand rdflib may raise on (filters) ([`7aca867`](https://github.com/vortex-rdf/vortex-rdflib/commit/7aca8679c211328a5ed9fc9bb90d742c61d71972) by @julianrojas87)

## [0.1.0] - 2026-09-01

First standalone release. The rdflib integration previously lived in the
[vortex-rdf](https://github.com/vortex-rdf/vortex-rdf) repository under
`python/`; it is now a pure-Python package built on the published
[vortex-rdf](https://pypi.org/project/vortex-rdf/) binding layer (0.10.x).

### Added

- `VortexRdflibStore`: a read-only rdflib `Store` over `.vortex` files,
  file-backed and lazily opened by default, or loaded into memory with
  `in_memory=True`.
- Named graphs: the store is context-aware, so a `.vortex` file's quads can be
  read through an rdflib `Dataset` — `contexts()`, per-graph `len()` and
  `triples()`, `quads()` carrying each row's graph — and `GRAPH` works in
  SPARQL. A `Graph` with no identifier of its own stays the view over the
  whole file, so the triple-oriented usage is unchanged.
- `GRAPH` pushdown: a named graph is the fourth position of every native match
  below it, and `GRAPH ?g` binds the graph from the match's fourth column, so
  the graph is an ordinary variable of the code-space relation — one match
  instead of rdflib's walk over the dataset's graphs. At 250k quads over 8
  graphs: `DISTINCT ?g` 2,678 ms -> 117 ms, `COUNT(*)` per graph
  1,593 ms -> 148 ms, a predicate scan under `GRAPH ?g` 87 ms -> 28 ms.
- Dictionary-layout code path: matched rows arrive as zero-copy `u32`
  term-code columns and each distinct code is decoded to an rdflib term once,
  with a string-table fallback for other layouts.
- SPARQL pushdown: whole basic graph patterns are evaluated in one pass as
  hash joins over `u32` term codes; a `FILTER` over a pattern is evaluated
  once per distinct value — a whitelist of expression shapes as predicates
  over the stored spellings, everything else through rdflib's own evaluator —
  with single-variable conditions applied to the pattern scans before the
  join; `OPTIONAL`, `MINUS`, `FILTER (NOT) EXISTS`, nested groups and inline
  `VALUES` tables run as hash left joins, anti-joins, semi-joins and joins
  over code tuples; the
  projection, `DISTINCT`, `ORDER BY` on variables,
  `LIMIT`/`OFFSET`, `ASK` and `COUNT` aggregates (with or without
  `GROUP BY`) above a block are answered on the same code-space result, and
  solutions are decoded lazily in chunks, only for the rows and variables
  actually consumed. Registered
  automatically; disable with `VORTEX_RDF_DISABLE_PUSHDOWN=1`, narrow with
  `VORTEX_RDF_PUSHDOWN_OPS`, force the generic FILTER route with
  `VORTEX_RDF_FILTER_FAST=0`. Described in `docs/pushdown.md`.
- Benchmark: a "pushdown off" row and nine operator-shaped queries
  (`ask-var`, `limit-scan`, `filter-class`, `distinct-p`, `count-all`,
  `count-distinct`, `optional-wide`, `not-exists`, `minus`, `order-var`,
  `values-64`) on the dashboard.
- Benchmark: the dataset is quads, partitioned by subject over eight graphs
  (`BENCH_GRAPHS`), and the quad-capable stores are compared as rdflib
  `Dataset`s over their union — so every existing query keeps its row counts —
  with a `graphs` panel for the shapes that name one (`graph-scan`,
  `graph-star`, `graph-chain`, `graph-var`, `graph-names`, `graph-count`).
  HDT and COTTAS do not serve named graphs through rdflib, so they load the
  flattened N-Triples of the same statements and are not asked that group.
- `py.typed` marker.

### Fixed

- A `FILTER (NOT) EXISTS` in an `OPTIONAL`'s group was hoisted into the
  `LeftJoin` condition and then dropped, so the optional side could bind where
  it should have stayed unbound. That shape is now left to rdflib.
- Benchmark generator: the graph count is nudged coprime with the predicate
  count. A shared factor pinned a predicate's rows to one graph across a whole
  block, which left the graph-scoped chain query with no rows to derive its
  constants from at some dataset sizes — including the CodSpeed suite's
  default, whose query set is built at import.
