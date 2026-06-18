# ffroute

> **Experimental** — APIs may change.

A Starlette/FastAPI-compatible URL route matcher implemented as a native Python
extension (Rust + [PyO3](https://pyo3.rs/)). Replaces Starlette's
linear-regex-scan with a **min-index segment trie** — **100–200× faster** on
real-world OpenAPI specs (Stripe, GitHub, Kubernetes), while preserving
Starlette's exact first-registered-wins semantics, including its
static-vs-param shadowing behaviour.

[![PyPI](https://img.shields.io/pypi/v/ffroute.svg)](https://pypi.org/project/ffroute/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## When to use this

ffroute helps when route matching is actually slow enough to notice:

- **Hundreds of routes** — Starlette's linear regex scan shows up in flame
  graphs (the original 740-route measurement that motivated this package
  found routing was ~13 % of request time).
- **Lots of 404 traffic** — bots, scanners, deprecated endpoints. The trie
  fails fast at the first non-matching segment; the "miss" speedup is even
  larger than the hit speedup (see Performance).
- **App startup matters** — ffroute builds the routing table 50–200× faster
  than Starlette's regex compile (sub-millisecond vs 10–45 ms on the OpenAPI
  specs below). Useful for cold-start serverless and frequent restarts in CI.

Skip if you have fewer than ~50 routes (savings are real but small in
absolute terms), or if you use custom `starlette.convertors.Convertor`
subclasses — `FFRouter` still works correctly (Starlette's per-route check
filters), but loses the trie fast-path for those segments.

## Install

```bash
pip install ffroute               # core (zero Python deps)
pip install 'ffroute[starlette]'  # with FFRouter — the Starlette/FastAPI drop-in
# or
uv add ffroute
```

Pre-built wheels are published for CPython 3.10–3.14 on Linux (manylinux +
musllinux, x86_64 + aarch64), macOS (x86_64 + arm64) and Windows (x86_64).
For other platforms, `pip install` builds from sdist — needs a Rust
toolchain (`rustup default stable`).

## Usage

### Drop-in for Starlette / FastAPI

```python
from fastapi import FastAPI
from ffroute import speedup

app = FastAPI()

@app.get('/users/{user_id}')
def show_user(user_id: int) -> dict:
    return {'user_id': user_id}

# Swap the path-matching layer for ffroute. That's it — method dispatch,
# Mount, Host, redirect_slashes, /openapi.json, /docs all unchanged.
speedup(app)
```

`speedup(app)` rebinds `app.router` to a dynamically-built subclass that
mixes [`FFRouter`](python/ffroute/_starlette.py) into whatever router class
the app already uses (`starlette.routing.Router` or
`fastapi.routing.APIRouter`). FFRouter's path-scan overrides win; everything
else — including FastAPI's OpenAPI-schema cache hooks — stays intact. It's
idempotent: a second call just re-indexes.

By default `speedup` **recurses into `Mount` sub-apps** and applies the same
treatment to any Starlette/FastAPI app (or bare `Router`) it finds — so a
heavily-routed sub-app behind `Mount('/api', app=sub)` gets the trie too,
not just the top-level router. Mounts wrapping non-router ASGI apps
(`StaticFiles`, raw callables) are left alone. Pass `recursive=False` to
opt out:

```python
speedup(app, recursive=False)  # only the top-level router
```

Equivalent manual form, if you're on plain Starlette (and don't need to
preserve a custom router subclass):

```python
from ffroute import FFRouter
app.router.__class__ = FFRouter
app.router._rebuild_index()
```

### Advanced: low-level matcher

For a custom dispatch layer outside Starlette, use the raw trie matcher
directly — no framework dependency, integers in / integers out:

```python
import ffroute

router = ffroute.Router([
    '/users/me',                           # 0
    '/users/{user_id:int}',                # 1
    '/items/{item_id}',                    # 2
    '/static/{file:path}',                 # 3
])

router.match('/users/me')        # -> 0   (first-registered wins)
router.match('/users/42')        # -> 1
router.match('/items/abc')       # -> 2
router.match('/static/a/b.png')  # -> 3
router.match('/missing')         # -> None
```

## API reference

| symbol | needs | use case |
|---|---|---|
| `ffroute.Router` | — | low-level Rust trie; returns the integer **index** of the matching pattern |
| `ffroute.FFRouter` | `starlette` | `starlette.routing.Router` subclass; drop-in replacement |
| `ffroute.speedup(app)` | `starlette` | one-liner: rebind an existing app's router class |

`Router` methods:

| method | returns | use case |
|---|---|---|
| `match(path)` | `int \| None` — best (lowest-index) match | single-request dispatch |
| `match_many(paths)` | `list[int]` — `-1` for no match | batch matching |
| `match_all(path)` | `list[int]` — every candidate, in trie-walk order | candidate narrowing (then run the framework's own per-route check) |

## Compatibility

`speedup(app)` swaps **only the path-matching scan**. Everything else stays
the framework's job — `FFRouter` is a `starlette.routing.Router` subclass
that runs Starlette's per-route check on each candidate the trie surfaces.

| Feature | Status |
|---|---|
| FastAPI `@app.get` / `@app.post` / decorators | ✅ |
| FastAPI `APIRouter` + `include_router` (prefix, tags, dependencies) | ✅ |
| Starlette `Route` with `methods=[...]` (→ 405 on method mismatch) | ✅ |
| Starlette `Mount` / sub-apps (always considered as candidates) | ✅ |
| Nested `Mount` (Mount inside Mount inside Mount…) | ✅ |
| Speedup propagates **into** Mount sub-apps and `Mount(routes=[...])` | ✅ via `recursive=True` (default); opt out with `speedup(app, recursive=False)` |
| Starlette `Host` routing | ✅ |
| WebSocket routes | ✅ |
| `redirect_slashes` (trailing-slash redirect) | ✅ |
| FastAPI auto-generated `/openapi.json`, `/docs`, `/redoc` | ✅ |
| Built-in path convertors (`str`, `int`, `float`, `uuid`, `path`) | ✅ fast-path |
| Custom `starlette.convertors.Convertor` subclasses | ⚠️ works through `FFRouter` (Starlette filters); loses the trie fast-path |
| `app.router.add_route(...)` after `speedup()` | ✅ trie rebuilds; O(N), so register routes before `speedup()` when possible |
| Calling `speedup(app)` twice | ✅ idempotent — just re-indexes |

All cases above are covered by `tests/test_ffrouter.py`.

## Supported segment kinds

`ffroute` mirrors Starlette's
[`convertors.py`](https://github.com/encode/starlette/blob/master/starlette/convertors.py):

| segment | regex | note |
|---|---|---|
| static | exact match | HashMap fast path |
| `{name}` / `{name:str}` | `[^/]+` | regex-free fast path |
| `{name:int}` | `[0-9]+` | typed convertor |
| `{name:float}` | `[0-9]+(\.[0-9]+)?` | typed convertor |
| `{name:uuid}` | `[0-9a-f]{8}-[0-9a-f]{4}-…` | typed convertor |
| `{name:path}` | `.*` | consumes the remainder of the path |
| compound (e.g. `{user}:disable`, `({n:int})`) | anchored whole-segment regex | mixed literal + param |

**Semantics:** `match` returns the **minimum registration index** among all
patterns that match. This is exactly Starlette's "first-registered wins" rule,
including the FastAPI footgun where `/x/{id}/` registered before `/x/bulk/`
makes `GET /x/bulk/` match the param route with `id="bulk"`. Drop-in routers
from other ecosystems (`httprouter`, `matchit`, `find-my-way`) do **not**
preserve this — they use static > param > catch-all priority, which is why
they can't be used as a Starlette replacement without behaviour change.

## Performance

Measured against three public OpenAPI specs ([Stripe], [GitHub REST v3] and
[Kubernetes]). For every route a **match** probe (dynamic params filled with
valid values) and a **miss** probe (dynamic section perturbed; verified
against Starlette's own matcher so collisions don't pollute the workload)
is generated; the full workload is looped to ≥ 20 k iterations, best-of-3:

<!-- bench:openapi:start -->
| spec | routes | ffroute hit | starlette hit | hit speedup | ffroute miss | starlette miss | miss speedup |
|---|--:|--:|--:|--:|--:|--:|--:|
| Stripe | 414 | 95 ns | 11 418 ns | **120×** | 65 ns | 22 956 ns | **355×** |
| GitHub | 788 | 112 ns | 23 501 ns | **210×** | 42 ns | 43 039 ns | **1022×** |
| Kubernetes | 542 | 127 ns | 15 496 ns | **122×** | 80 ns | 31 723 ns | **398×** |
<!-- bench:openapi:end -->

The **miss** speedup is even larger than the hit speedup because a trie
fails fast at the first non-matching segment, while Starlette has to execute
every compiled regex before concluding "no match". One-time build is
**50–200×** faster too (Stripe: 0.13 ms vs 8.3 ms; GitHub: 0.22 ms vs
22.5 ms).

Reproduce — or regenerate the table above — with
[`benchmarks/openapi_bench.py`](./benchmarks/openapi_bench.py):

```bash
uv sync --group benchmarks
uv run --no-sync python benchmarks/openapi_bench.py                 # print
uv run --no-sync python benchmarks/openapi_bench.py --update-readme # rewrite the table above
```

[Stripe]: https://github.com/stripe/openapi
[GitHub REST v3]: https://github.com/github/rest-api-description
[Kubernetes]: https://github.com/kubernetes/kubernetes/tree/master/api/openapi-spec

### Why this is fast (vs other approaches)

On a synthetic 740-route table with 50 k weighted URLs (best-of-5,
Python 3.14.0rc2), ffroute is measured against the design space of routing
implementations — pure-Python tries, regex variants, batch vs single-call:

| implementation | build (ms) | ns/req | req/s | vs Starlette |
|---|--:|--:|--:|--:|
| `starlette.routing.Router` (linear regex scan) | 130 | 2971 | 0.34 M | 1.0× |
| linear regex, no scope dict | 59 | 953 | 1.05 M | 3.1× |
| pure-Python segment trie | 1.8 | 668 | 1.50 M | 4.4× |
| **`ffroute` (PyO3, batch)** | 0.7 | 115 | 8.7 M | **26×** |
| **`ffroute` (PyO3, single-call)** | **0.5** | **101** | **9.9 M** | **29×** |

The single-call number is the realistic one — ASGI calls the matcher once
per request, not in a batch. A pure-Python trie alone already wins 4.4×, so
most of the gap isn't "Rust vs Python" — it's "trie vs linear scan". The
Rust implementation is then ~7× on top of that. A trie also removes the
O(N) factor: the linear scan climbs 745 → 2731 ns/req as N goes 100 →
3000, while ffroute stays ~98 ns regardless of N.

The 740-route numbers are lower than the OpenAPI numbers above because the
synthetic corpus is dominated by short static prefixes (faster for both
matchers); the real OpenAPI tables have deeper paths and more dynamic
segments, which slow Starlette's per-route regex scan disproportionately.

## Why a custom router (vs `matchit` / `httprouter` / `find-my-way`)?

A native radix router from another ecosystem can't be dropped into FastAPI
without changing behaviour:

- **`httprouter` (Go)** *panics* when a static segment and a param share a
  position (`/x/bulk/` vs `/x/{id}/`) — the everyday FastAPI route model. It
  rejected 53 % of a 740-route table in our measurements.
- **`matchit` (axum)** and **`find-my-way` (Fastify)** use static > param >
  catch-all priority, returning a *different* match than Starlette for the
  shadowing cases above.

`ffroute` implements a min-index segment trie precisely to reproduce
Starlette's first-registered semantics byte-for-byte.

## Conformance

`tests/test_conformance.py` is a differential test whose oracle is Starlette's
real `compile_path` / `Route.path_regex`. Cases are lifted from
[`starlette/tests/test_routing.py`](https://github.com/encode/starlette/blob/master/tests/test_routing.py)
(typed convertors, `/path-with-parentheses({param:int})`, the
intra-segment `/{username}:disable`, str-param shadowing), FastAPI's
documented fixed-before-param priority, and trailing-slash edge cases, plus
a synthetic 5 000-path fuzz over an algorithmically-generated route corpus.

`tests/test_ffrouter.py` covers the `speedup` / `FFRouter` integration —
mount + APIRouter + include_router combinations, method dispatch (→ 405),
nested Mounts, FastAPI's `/openapi.json` round-trip, and the lazy-import
error message when Starlette isn't installed.

See [`examples/starlette_app.py`](./examples/starlette_app.py) and
[`examples/fastapi_app.py`](./examples/fastapi_app.py) for runnable apps
that enable `FFRouter` in one line.

## Development

```bash
uv sync --all-groups               # install dev deps incl. maturin
uv run maturin develop --release   # build the Rust extension into the venv
uv run pytest                      # run the test suite
```

See the [`Makefile`](./Makefile) for additional targets (`make help`).

## License

MIT — see [LICENSE](./LICENSE).
