"""Differential conformance: ffroute.Router vs Starlette's own path matcher.

Oracle = Starlette's real ``compile_path`` / ``Route.path_regex`` walked in
registration order. Case tables are derived from upstream
``starlette/tests/test_routing.py`` and FastAPI's documented
fixed-before-param priority — all public material — plus a synthetic fuzz over
an algorithmically-generated route corpus (no proprietary tables shipped).
"""

from __future__ import annotations

import random

import ffroute
import pytest
from starlette.routing import Route


def _starlette_oracle(patterns: list[str]):
    """First route whose real Starlette ``path_regex`` matches — ground truth."""
    regexes = [Route(p, endpoint=lambda: None).path_regex for p in patterns]

    def match(path: str) -> int | None:
        for i, rx in enumerate(regexes):
            if rx.match(path):
                return i
        return None

    return match


# --- public case tables (lifted from Starlette/FastAPI tests) ---------------

STARLETTE_TYPED = (
    [
        '/',
        '/func',
        '/int/{param:int}',
        '/float/{param:float}',
        '/path/{param:path}',
        '/uuid/{param:uuid}',
        '/path-with-parentheses({param:int})',
    ],
    [
        '/',
        '/func',
        '/int/5',
        '/int/abc',
        '/int/',
        '/int/5/6',
        '/float/25.5',
        '/float/5',
        '/float/25.',
        '/float/abc',
        '/path/some/example',
        '/path/',
        '/path/a/b/c/d',
        '/uuid/ec38df32-ceda-4cfa-9b4a-1aeb94ad551a',
        '/uuid/not-a-uuid',
        '/path-with-parentheses(7)',
        '/path-with-parentheses(abc)',
        '/path-with-parentheses()',
    ],
)

# Starlette's /users sub-router: intra-segment `:disable` + str-param priority.
STARLETTE_USERS = (
    ['/', '/me', '/{username}', '/{username}:disable', '/nomatch'],
    [
        '/',
        '/me',
        '/tomchristie',
        '/tomchristie:disable',
        '/nomatch',
        '/tomchristie/',
        '/me:disable',
        '/nomatch:disable',
    ],
)

# FastAPI's documented rule: a fixed path declared before a param path wins.
FASTAPI_PRIORITY = (
    ['/users/me', '/users/{user_id}', '/items/{item_id:int}', '/items/{item_id}'],
    ['/users/me', '/users/123', '/users/me/', '/items/5', '/items/abc'],
)

# Trailing-slash and overlap nuances.
TRAILING_SLASH = (
    ['/users', '/users/', '/users/{id}', '/users/{id}/'],
    ['/users', '/users/', '/users/42', '/users/42/', '/usersa'],
)


@pytest.mark.parametrize(
    ('name', 'patterns', 'probes'),
    [
        ('starlette-typed', *STARLETTE_TYPED),
        ('starlette-users', *STARLETTE_USERS),
        ('fastapi-priority', *FASTAPI_PRIORITY),
        ('trailing-slash', *TRAILING_SLASH),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_conformance_table(name: str, patterns: list[str], probes: list[str]):
    oracle = _starlette_oracle(patterns)
    router = ffroute.Router(patterns)
    divergences: list[tuple[str, int | None, int | None]] = []
    for path in probes:
        exp = oracle(path)
        got = router.match_(path)
        if got != exp:
            divergences.append((path, exp, got))
    assert not divergences, f'[{name}] {len(divergences)} divergences vs Starlette: {divergences}'


_STATIC_SEGS = ['v1', 'v2', 'api', 'users', 'items', 'orders', 'projects', 'me', 'list', 'bulk']
_PARAM_NAMES = ['id', 'name', 'slug', 'key', 'tag']
_CONVERTORS = ['', ':str', ':int', ':float', ':uuid']


def _fresh_name(rng: random.Random, used: set[str]) -> str | None:
    available = [n for n in _PARAM_NAMES if n not in used]
    if not available:
        return None
    chosen = rng.choice(available)
    used.add(chosen)
    return chosen


def _synthetic_corpus(n_routes: int = 200, *, seed: int = 0) -> list[str]:
    """Generate a representative route table covering most supported segment kinds.

    Mixes static segments, plain ``{str}``, typed convertors, and intra-segment
    literals at varied depths. Each pattern uses unique param names (Starlette
    rejects duplicates at compile time). ``{path}`` catch-alls are covered in
    the explicit ``STARLETTE_TYPED`` table — they're omitted here so the fuzz
    surfaces structural divergences rather than the well-known empty-segment
    edge.
    """
    rng = random.Random(seed)
    patterns: set[str] = set()
    while len(patterns) < n_routes:
        depth = rng.randint(1, 6)
        parts: list[str] = []
        used: set[str] = set()
        for _ in range(depth):
            kind = rng.choices(['static', 'param', 'typed', 'compound'], weights=[5, 3, 2, 1])[0]
            name = _fresh_name(rng, used) if kind != 'static' else None
            if kind == 'static' or name is None:
                parts.append(rng.choice(_STATIC_SEGS))
            elif kind == 'param':
                parts.append('{' + name + '}')
            elif kind == 'typed':
                parts.append('{' + name + rng.choice(_CONVERTORS) + '}')
            else:
                parts.append('{' + name + '}:' + rng.choice(_STATIC_SEGS))
        suffix = '/' if rng.random() < 0.5 else ''
        patterns.add('/' + '/'.join(parts) + suffix)
    return sorted(patterns)


def test_fuzz_against_starlette():
    patterns = _synthetic_corpus(n_routes=200, seed=0)
    oracle = _starlette_oracle(patterns)
    router = ffroute.Router(patterns)

    rng = random.Random(5)
    segs_pool = [
        'v1',
        'v2',
        'api',
        'users',
        'items',
        'orders',
        'projects',
        'me',
        'list',
        'bulk',
        'abc',
        '123',
        '25.5',
        'ec38df32-ceda-4cfa-9b4a-1aeb94ad551a',
        'a-b-c',
    ]
    divergences: list[tuple[str, int | None, int | None]] = []
    for _ in range(5000):
        depth = rng.randint(1, 7)
        path = '/' + '/'.join(rng.choice(segs_pool) for _ in range(depth))
        if rng.random() < 0.5:
            path += '/'
        exp = oracle(path)
        got = router.match_(path)
        if got != exp:
            divergences.append((path, exp, got))
            if len(divergences) > 10:
                break
    assert not divergences, f'{len(divergences)} fuzz divergences: {divergences[:5]}'
