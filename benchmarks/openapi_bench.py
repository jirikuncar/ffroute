"""Profile ffroute vs Starlette against real OpenAPI specs from large APIs.

Downloads 3 public OpenAPI specs (Stripe, GitHub, Kubernetes), extracts the
path templates, converts them to Starlette syntax, then for **every route**
generates two probes:

* a **match** URL — the dynamic ``{param}`` placeholders filled with valid
  values, so the URL matches its source pattern.
* a **miss** URL — the dynamic section perturbed so the URL no longer matches.
  Each candidate miss is verified against Starlette's own matcher (the
  oracle); if a perturbation accidentally collides with another route in the
  table the script falls back to a guaranteed-unique magic prefix.

Both workloads are then looped to reach a stable iteration count and timed
best-of-N.

Run::

    uv sync --group benchmarks
    uv run --no-sync python benchmarks/openapi_bench.py

The first run downloads specs to ``benchmarks/.cache/``. Subsequent runs reuse
the cache.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import ffroute
from starlette.routing import Route

CACHE = Path(__file__).parent / '.cache'
CACHE.mkdir(exist_ok=True)

README = Path(__file__).parent.parent / 'README.md'
TABLE_START = '<!-- bench:openapi:start -->'
TABLE_END = '<!-- bench:openapi:end -->'

SPECS: dict[str, str] = {
    'stripe': 'https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json',
    'github': (
        'https://raw.githubusercontent.com/github/rest-api-description/main/'
        'descriptions/api.github.com/api.github.com.json'
    ),
    'kubernetes': (
        'https://raw.githubusercontent.com/kubernetes/kubernetes/master/'
        'api/openapi-spec/swagger.json'
    ),
}

# Magic prefix guaranteed not to appear as a static segment in any sane API
# spec — used as the universal-fallback miss probe.
NOMATCH_MARKER = '__ffroute_no_match__'


def fetch(name: str, url: str) -> dict:
    path = CACHE / f'{name}.json'
    if not path.exists():
        print(f'  downloading {name} ({url}) ...')
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 — trusted URLs only
            path.write_bytes(resp.read())
    with path.open() as f:
        return json.load(f)


_PARAM_RE = re.compile(r'\{([^{}/]+)\}')
_BAD_CHARS = re.compile(r'[\s,;@$()<>]')


def extract_paths(spec: dict) -> list[str]:
    """Extract path templates from an OpenAPI spec, normalized to Starlette syntax."""
    paths = spec.get('paths') or {}
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw.startswith('/'):
            continue
        if _BAD_CHARS.search(raw):
            continue  # skip OData-style /things({id}) — they break Starlette's compiler
        norm = re.sub(r'/+', '/', raw.strip())
        # Starlette wants ident-like param names; OpenAPI sometimes has dots/hyphens.
        norm = _PARAM_RE.sub(lambda m: '{' + re.sub(r'[^A-Za-z0-9_]', '_', m.group(1)) + '}', norm)
        if norm in seen:
            continue
        seen.add(norm)
        cleaned.append(norm)
    return cleaned


# OpenAPI path templates carry no type information, so every ``{param}`` is
# plain ``[^/]+``. A valid filler is any non-empty no-slash string.
_FILLER = 'abc42'


def make_match_url(pattern: str) -> str:
    """Fill every ``{param}`` in pattern with a valid value — yields a URL that
    matches this pattern (and possibly higher-priority patterns; that's fine,
    the oracle accounts for first-registered semantics)."""
    return _PARAM_RE.sub(lambda _m: _FILLER, pattern)


def make_miss_url(pattern: str, oracle: Callable[[str], int | None]) -> str:
    """Generate a URL whose **dynamic section** is mangled so no route matches.

    Strategies, tried in order, each verified against ``oracle`` (Starlette's
    real path-regex), so we never pollute the miss workload with accidental
    matches against another route:

    1. Empty out one dynamic param (forces non-match on ``[^/]+`` for that
       route; usually misses everything else too).
    2. Replace one dynamic param with a value containing the magic marker
       (e.g. ``/users/__ffroute_no_match__``) — keeps URL depth, varies the
       dynamic content with a string no spec contains as a literal.
    3. Universal fallback: prepend ``/<marker>/``. Guaranteed miss because no
       real route starts with this static segment.
    """
    params = list(_PARAM_RE.finditer(pattern))

    if params:
        # Strategy 1: empty out each dynamic param one at a time.
        for target in params:
            chunks: list[str] = []
            last = 0
            for m in params:
                chunks.append(pattern[last : m.start()])
                chunks.append('' if m is target else _FILLER)
                last = m.end()
            chunks.append(pattern[last:])
            candidate = ''.join(chunks)
            if oracle(candidate) is None:
                return candidate

        # Strategy 2: marker as the dynamic value.
        for target in params:
            chunks = []
            last = 0
            for m in params:
                chunks.append(pattern[last : m.start()])
                chunks.append(NOMATCH_MARKER if m is target else _FILLER)
                last = m.end()
            chunks.append(pattern[last:])
            candidate = ''.join(chunks)
            if oracle(candidate) is None:
                return candidate

    # Strategy 3: prepend marker — always misses.
    return f'/{NOMATCH_MARKER}' + make_match_url(pattern)


class StarletteMatcher:
    """Wrap Starlette's real ``Route.path_regex`` scan into a uniform API."""

    def __init__(self, patterns: list[str]) -> None:
        self._regexes = [Route(p, endpoint=lambda: None).path_regex for p in patterns]

    def match(self, path: str) -> int | None:
        for i, rx in enumerate(self._regexes):
            if rx.match(path):
                return i
        return None


def time_workload(fn, paths: list[str], *, target_iters: int = 20000, rounds: int = 3) -> float:
    """Loop the workload until ``target_iters`` reached; report best-of-N ns/req."""
    repeats = max(1, target_iters // len(paths))
    best = float('inf')
    for _ in range(rounds):
        t0 = time.perf_counter_ns()
        for _ in range(repeats):
            for p in paths:
                fn(p)
        elapsed = (time.perf_counter_ns() - t0) / (repeats * len(paths))
        best = min(best, elapsed)
    return best


@dataclass
class BenchResult:
    name: str
    routes: int
    ff_build_ms: float
    sl_build_ms: float
    ff_hit_ns: float
    sl_hit_ns: float
    ff_miss_ns: float
    sl_miss_ns: float


def benchmark(name: str, patterns: list[str]) -> BenchResult | None:
    n_routes = len(patterns)
    if n_routes < 100:
        print(f'  {name}: only {n_routes} routes — skipping')
        return None

    print(f'\n== {name}: {n_routes} routes ==')

    # Build both matchers (one-time, also a metric).
    t0 = time.perf_counter_ns()
    ff = ffroute.Router(patterns)
    ff_build_ms = (time.perf_counter_ns() - t0) / 1e6

    t0 = time.perf_counter_ns()
    sl = StarletteMatcher(patterns)
    sl_build_ms = (time.perf_counter_ns() - t0) / 1e6

    # Per-route workloads — every pattern generates one match + one miss probe.
    matches = [make_match_url(p) for p in patterns]
    print('   generating per-route misses (Starlette-verified) ...', flush=True)
    misses = [make_miss_url(p, sl.match) for p in patterns]

    # Sanity: how miss probes were constructed.
    n_collisions = sum(1 for m in misses if m.startswith(f'/{NOMATCH_MARKER}'))
    print(f'   build:  ffroute {ff_build_ms:7.2f} ms   starlette {sl_build_ms:7.2f} ms')
    print(
        f'   probes: {n_routes} matches / {n_routes} misses '
        f'({n_collisions} fell back to marker prefix)'
    )
    print(f'   match sample: {matches[:2]}')
    print(f'   miss sample:  {misses[:2]}')

    # Verify correctness ON THE WORKLOADS before timing — every match probe
    # must agree between ffroute and Starlette; every miss must miss both.
    mismatches = sum(1 for u in matches if ff.match_(u) != sl.match(u))
    miss_in_ff = sum(1 for u in misses if ff.match_(u) is not None)
    miss_in_sl = sum(1 for u in misses if sl.match(u) is not None)
    print(
        f'   correctness: {mismatches}/{n_routes} match-probe divergences, '
        f'{miss_in_ff}/{n_routes} ff-false-hits, {miss_in_sl}/{n_routes} sl-false-hits'
    )

    # Time both workloads.
    ff_hit = time_workload(ff.match_, matches)
    sl_hit = time_workload(sl.match, matches)
    ff_miss = time_workload(ff.match_, misses)
    sl_miss = time_workload(sl.match, misses)

    def fmt(ns: float) -> str:
        return f'{ns:7.0f} ns/req ({1e9 / ns / 1e6:5.2f} M req/s)'

    print(
        f'   hit:   ffroute {fmt(ff_hit)}   starlette {fmt(sl_hit)}   '
        f'speedup {sl_hit / ff_hit:5.1f}x'
    )
    print(
        f'   miss:  ffroute {fmt(ff_miss)}   starlette {fmt(sl_miss)}   '
        f'speedup {sl_miss / ff_miss:5.1f}x'
    )

    return BenchResult(
        name=name,
        routes=n_routes,
        ff_build_ms=ff_build_ms,
        sl_build_ms=sl_build_ms,
        ff_hit_ns=ff_hit,
        sl_hit_ns=sl_hit,
        ff_miss_ns=ff_miss,
        sl_miss_ns=sl_miss,
    )


_DISPLAY_NAME = {'stripe': 'Stripe', 'github': 'GitHub', 'kubernetes': 'Kubernetes'}


def render_table(results: list[BenchResult]) -> str:
    """Render the markdown table that the README sentinels wrap."""

    def _ns(v: float) -> str:
        return f'{v:,.0f} ns'.replace(',', ' ')

    lines = [
        '| spec | routes | ffroute hit | starlette hit | hit speedup '
        '| ffroute miss | starlette miss | miss speedup |',
        '|---|--:|--:|--:|--:|--:|--:|--:|',
    ]
    for r in results:
        display = _DISPLAY_NAME.get(r.name, r.name.capitalize())
        lines.append(
            f'| {display} | {r.routes} | '
            f'{_ns(r.ff_hit_ns)} | {_ns(r.sl_hit_ns)} | '
            f'**{r.sl_hit_ns / r.ff_hit_ns:.0f}×** | '
            f'{_ns(r.ff_miss_ns)} | {_ns(r.sl_miss_ns)} | '
            f'**{r.sl_miss_ns / r.ff_miss_ns:.0f}×** |'
        )
    return '\n'.join(lines)


def update_readme(results: list[BenchResult]) -> None:
    if not README.exists():
        print(f'README not found at {README}; skipping update', file=sys.stderr)
        return
    text = README.read_text()
    if TABLE_START not in text or TABLE_END not in text:
        print(
            f'sentinel markers ({TABLE_START} / {TABLE_END}) not found in README; '
            'add them around the table to enable --update-readme',
            file=sys.stderr,
        )
        sys.exit(1)
    new_block = f'{TABLE_START}\n{render_table(results)}\n{TABLE_END}'
    new_text = re.sub(
        re.escape(TABLE_START) + r'.*?' + re.escape(TABLE_END),
        new_block,
        text,
        count=1,
        flags=re.DOTALL,
    )
    if new_text == text:
        print('README already up to date')
        return
    README.write_text(new_text)
    print(f'  updated {README} ({len(results)} rows)')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--update-readme',
        action='store_true',
        help=f'rewrite the OpenAPI bench table in README.md between {TABLE_START} / {TABLE_END}',
    )
    args = parser.parse_args()

    print('Fetching OpenAPI specs (cached after first run) ...')
    results: list[BenchResult] = []
    for name, url in SPECS.items():
        try:
            spec = fetch(name, url)
        except Exception as e:  # noqa: BLE001
            print(f'  {name}: fetch failed ({e}); skipping')
            continue
        patterns = extract_paths(spec)
        r = benchmark(name, patterns)
        if r is not None:
            results.append(r)

    if args.update_readme and results:
        print()
        update_readme(results)


if __name__ == '__main__':
    main()
