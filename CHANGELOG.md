# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0](https://github.com/vortex-rdf/vortex-rdflib/compare/v0.3.0...v0.4.0) - 2026-10-22

Requires vortex-rdf 0.13 (`vortex-rdf>=0.13,<0.14`, up from 0.12.1): joins are
budgeted, long native calls can be cancelled, and large reads stream. A join
step of a basic graph pattern has a pair budget (`VORTEX_RDF_PAIR_BUDGET`,
16,777,216 row pairs by default), and a step over it is not built whole: the
last step streams its pairs a piece at a time as the rows are read, or another
pattern that provably fits is joined first, or rdflib finishes the remaining
patterns row by row. A probe join whose probes would each scan the store is a
keep-scan instead: the pattern is matched once, narrowed to the codes the
relation binds. `vortex_rdflib.cancel_scope(token)` gives the queries started
inside it a `vortex_rdf.CancelToken`, and cancelling it, from any thread,
raises `vortex_rdf.Cancelled` at the query's next call into vortex-rdf or its
next pull. `triples()` reads a pattern of more than 65,536 rows in chunks of
that many, so a scan of a large store holds one chunk of codes at a time.

### Breaking changes

- vortex-rdf 0.12 is no longer supported: the dependency is
  `vortex-rdf>=0.13,<0.14`.

### Added

- A pair budget guards every BGP join step (pushdown) ([`d34a54e`](https://github.com/vortex-rdf/vortex-rdflib/commit/d34a54e85db9b5ebbcf9f2624974eac244f51f36) by @julianrojas87)
- Stream an over-budget last join step through CodeGroups (pushdown) ([`e37db53`](https://github.com/vortex-rdf/vortex-rdflib/commit/e37db53e917aefd61a5a2b7f875c3041003a9924) by @julianrojas87)
- Reorder around an over-budget step, else let rdflib finish the rest per row (pushdown) ([`439d828`](https://github.com/vortex-rdf/vortex-rdflib/commit/439d8287fab91df1a01a9c77ac31e523a541c7ca) by @julianrojas87)
- A per-query cancel token for every long native call ([`b71d6ff`](https://github.com/vortex-rdf/vortex-rdflib/commit/b71d6ff6dba7280eba41697f4c4f31691f60481e) by @julianrojas87)
- Triples() reads a large pattern one chunk at a time (store) ([`a9ffe48`](https://github.com/vortex-rdf/vortex-rdflib/commit/a9ffe482b65288d21320adefd903f92e85fe36fe) by @julianrojas87)

### Changed

- One keep-scan instead of a scan per row, and an explicit probe lookahead (pushdown) ([`5f6457a`](https://github.com/vortex-rdf/vortex-rdflib/commit/5f6457ae42b7e7634f80f52a1bb37d9da926a717) by @julianrojas87)
- Triples() tells a large pattern by its first match, not a count (store) ([`d8f9104`](https://github.com/vortex-rdf/vortex-rdflib/commit/d8f91044deebcf73a7fd12ea2bf696d000a7064e) by @julianrojas87)

### Fixed

- Record and replay vortex-rdf 0.13 batch iterators (bench) ([`323c97c`](https://github.com/vortex-rdf/vortex-rdflib/commit/323c97c2b8bdd04843821ac7a8d2800f0be9a24d) by @julianrojas87)
- Undo a put-back step's specialized conjuncts (pushdown) ([`9b69fa9`](https://github.com/vortex-rdf/vortex-rdflib/commit/9b69fa919eec72d65470a1b8e51e1078a832ed66) by @julianrojas87)
- Count the probes a tripped step sent, and key the memo by the keep column (pushdown) ([`9868e2a`](https://github.com/vortex-rdf/vortex-rdflib/commit/9868e2aa5c318f3cd1f6c98acdf0ca6ebc24451c) by @julianrojas87)
- Check the cancel token before a string-fallback read (store) ([`4409609`](https://github.com/vortex-rdf/vortex-rdflib/commit/44096098ac1c8679e22bcc7e8f94c1cceb95ea55) by @julianrojas87)
- Triples() lets go of every column of a chunk before the next pull (store) ([`7452b04`](https://github.com/vortex-rdf/vortex-rdflib/commit/7452b04a264eb0b8af54f5eae0a75740d0b9ee1e) by @julianrojas87)

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
