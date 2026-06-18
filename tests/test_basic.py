"""Smoke tests for the Router public API."""

from __future__ import annotations

import ffroute


def test_module_exports_router():
    assert hasattr(ffroute, 'Router')


def test_empty_router_returns_none():
    r = ffroute.Router([])
    assert r.match_('/anything') is None


def test_static_match():
    r = ffroute.Router(['/users', '/items'])
    assert r.match_('/users') == 0
    assert r.match_('/items') == 1
    assert r.match_('/missing') is None


def test_str_param_match():
    r = ffroute.Router(['/users/{name}'])
    assert r.match_('/users/alice') == 0
    assert r.match_('/users/') is None  # empty segment doesn't match str param
    assert r.match_('/users/a/b') is None  # str doesn't span segments


def test_path_param_consumes_remainder():
    r = ffroute.Router(['/static/{file:path}'])
    assert r.match_('/static/a/b/c.txt') == 0
    assert r.match_('/static/') == 0  # `:path` matches the empty remainder
    # `/static` (no trailing slash) must NOT match: Starlette's regex
    # `^/static/(?P<file>.*)$` requires the literal `/` after `static`,
    # and ffroute mirrors that.
    assert r.match_('/static') is None


def test_first_registered_wins():
    # Starlette semantics: minimum registration index wins.
    r = ffroute.Router(['/users/{id}', '/users/me'])
    assert r.match_('/users/me') == 0  # the {id} route was registered first


def test_match_many_batch():
    r = ffroute.Router(['/a', '/b/{x}'])
    assert r.match_many(['/a', '/b/1', '/c']) == [0, 1, -1]


def test_match_all_collects_all_candidates():
    # Same path, two routes (e.g. GET vs POST in a real app) — both indices returned.
    r = ffroute.Router(['/items/', '/items/'])
    out = sorted(r.match_all('/items/'))
    assert out == [0, 1]
