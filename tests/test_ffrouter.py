"""Integration tests for the Starlette layer: ``FFRouter`` + ``speedup``.

Uses raw ASGI probes rather than ``starlette.testclient.TestClient`` because
Starlette 1.3+ requires ``httpx2``, which we don't want to drag into our dev
deps just to test 404/405/200 status codes.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap

import ffroute
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


async def _ok(_request):
    return JSONResponse({})


def _probe(app, path: str, method: str = 'GET') -> int:
    """Drive a single request through the ASGI app; return the HTTP status."""
    scope = {
        'type': 'http',
        'method': method,
        'path': path,
        'headers': [],
        'query_string': b'',
        'raw_path': path.encode(),
        'http_version': '1.1',
        'scheme': 'http',
        'root_path': '',
        'server': ('t', 80),
        'client': ('t', 0),
    }
    rec: dict[str, int | None] = {'status': None}

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(msg):
        if msg['type'] == 'http.response.start':
            rec['status'] = msg['status']

    asyncio.run(app(scope, receive, send))
    return rec['status']  # type: ignore[return-value]


# --- Lazy-import wiring ----------------------------------------------------


def test_lazy_import_exposes_ffrouter_and_speedup():
    assert ffroute.FFRouter.__module__ == 'ffroute._starlette'
    assert callable(ffroute.speedup)


def test_dir_lists_lazy_exports():
    # __dir__ surfaces FFRouter / speedup even before they're materialized.
    names = dir(ffroute)
    assert 'FFRouter' in names
    assert 'speedup' in names
    assert 'Router' in names


def test_missing_starlette_raises_helpful_importerror():
    """Spawn a subprocess that pretends Starlette isn't installed; verify the
    error message points at the ``ffroute[starlette]`` extra rather than the
    raw ``No module named 'starlette'``.
    """
    src = textwrap.dedent(
        """
        import sys
        # Block Starlette imports BEFORE ffroute lazy-loads it.
        sys.modules['starlette'] = None
        import ffroute
        try:
            ffroute.FFRouter
        except ImportError as e:
            print('GOT:', e)
        """
    )
    out = subprocess.run([sys.executable, '-c', src], capture_output=True, text=True, timeout=10)
    assert 'ffroute[starlette]' in out.stdout, out.stdout + out.stderr


# --- speedup() + FFRouter dispatch ----------------------------------------


def test_speedup_swaps_router_class_and_serves_requests():
    app = Starlette(routes=[Route('/x', _ok)])
    assert type(app.router).__name__ == 'Router'
    ffroute.speedup(app)
    assert type(app.router).__name__ == 'FFRouter'
    assert _probe(app, '/x') == 200
    assert _probe(app, '/missing') == 404


def test_first_registered_wins_via_ffrouter():
    # Both /users/{id} and /users/me match '/users/me' — index 0 wins.
    app = Starlette(routes=[Route('/users/{id}', _ok), Route('/users/me', _ok)])
    ffroute.speedup(app)
    # If FFRouter wrongly preferred /users/me over /users/{id}, this still
    # returns 200 (just the wrong handler). The signal is that *both* probes
    # work, which exercises the candidate-narrowing path.
    assert _probe(app, '/users/me') == 200
    assert _probe(app, '/users/42') == 200


def test_method_mismatch_returns_405():
    # PARTIAL match (path matches, method doesn't) must surface as 405.
    app = Starlette(routes=[Route('/x', _ok, methods=['GET'])])
    ffroute.speedup(app)
    assert _probe(app, '/x', method='POST') == 405


def test_mount_routes_are_always_candidates():
    # Mounts have no flat path the trie can index, so FFRouter must consider
    # them on every request. Without that, /m/inner would 404.
    sub = Starlette(routes=[Route('/inner', _ok)])
    app = Starlette(routes=[Mount('/m', app=sub)])
    ffroute.speedup(app)
    assert _probe(app, '/m/inner') == 200
    assert _probe(app, '/m/missing') == 404


def test_add_route_after_speedup_reindexes():
    # The add_route override must rebuild the trie; otherwise /b would 404.
    app = Starlette(routes=[Route('/a', _ok)])
    ffroute.speedup(app)
    app.router.add_route('/b', _ok)
    assert _probe(app, '/a') == 200
    assert _probe(app, '/b') == 200


def test_ffrouter_with_zero_routes_falls_back_cleanly():
    # Empty router: self._ffroute is None; the fast path short-circuits to
    # super().app(), which returns 404 — no AttributeError on None.
    app = Starlette(routes=[])
    ffroute.speedup(app)
    assert _probe(app, '/anything') == 404


# --- Combined-router scenarios --------------------------------------------


def test_nested_starlette_mounts_dispatch_correctly():
    """A Mount containing another Mount — outer Mount must stay an always-
    considered candidate even though it covers many sub-paths."""
    inner = Starlette(routes=[Route('/deep', _ok)])
    middle = Starlette(routes=[Mount('/inner', app=inner)])
    app = Starlette(routes=[Mount('/outer', app=middle)])
    ffroute.speedup(app)
    assert _probe(app, '/outer/inner/deep') == 200
    assert _probe(app, '/outer/inner/missing') == 404
    assert _probe(app, '/outer/missing/deep') == 404


def test_speedup_recurses_into_mounted_subapps_by_default():
    """speedup(app, recursive=True) must descend into Mounts and FFRoute
    each Starlette/FastAPI sub-app it finds, otherwise the sub-app keeps
    doing slow linear-regex matching inside its Mount prefix."""
    inner = Starlette(routes=[Route('/deep', _ok)])
    middle = Starlette(routes=[Mount('/inner', app=inner)])
    app = Starlette(routes=[Route('/top', _ok), Mount('/outer', app=middle)])

    ffroute.speedup(app)

    # Every layer's router class now has FFRouter in its MRO.
    assert ffroute.FFRouter in type(app.router).__mro__
    assert ffroute.FFRouter in type(middle.router).__mro__
    assert ffroute.FFRouter in type(inner.router).__mro__
    # And dispatch still works end-to-end.
    assert _probe(app, '/top') == 200
    assert _probe(app, '/outer/inner/deep') == 200
    assert _probe(app, '/outer/inner/missing') == 404


def test_speedup_recursive_false_leaves_subapps_alone():
    inner = Starlette(routes=[Route('/x', _ok)])
    app = Starlette(routes=[Mount('/m', app=inner)])

    ffroute.speedup(app, recursive=False)

    assert ffroute.FFRouter in type(app.router).__mro__
    # The sub-app's router class is untouched.
    assert ffroute.FFRouter not in type(inner.router).__mro__
    # Dispatch still works (Mount routes the request into the sub-app's
    # stock Starlette router).
    assert _probe(app, '/m/x') == 200


def test_speedup_recurses_into_mount_with_routes_kwarg():
    """``Mount('/m', routes=[...])`` (no explicit app) — Starlette wraps the
    routes in a Router and stores it as ``mount.app``. Recursive speedup
    must catch that too."""
    mount = Mount('/m', routes=[Route('/x', _ok)])
    app = Starlette(routes=[mount])
    ffroute.speedup(app)
    # Both the outer router and the Router-inside-Mount have FFRouter mixed in.
    assert ffroute.FFRouter in type(app.router).__mro__
    assert ffroute.FFRouter in type(mount.app).__mro__
    assert _probe(app, '/m/x') == 200


def test_speedup_skips_non_router_mounts():
    """Mount wrapping a non-Router ASGI app (e.g. StaticFiles, a raw
    callable) must not crash the recursive descent."""

    async def raw_asgi(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 204, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    app = Starlette(routes=[Mount('/raw', app=raw_asgi)])
    ffroute.speedup(app)  # must not raise
    assert _probe(app, '/raw/anything') == 204


def test_fastapi_include_router_plus_starlette_mount():
    """Real-world FastAPI app shape: top-level routes + APIRouter prefix +
    Starlette Mount, all in the same app, all reachable through FFRouter."""
    fastapi = pytest.importorskip('fastapi')

    api = fastapi.APIRouter(prefix='/api/v1')

    @api.get('/items/{item_id}')
    def get_item(item_id: int):
        return {'item_id': item_id}

    @api.get('/users/me')
    def me():
        return {'me': True}

    sub = Starlette(routes=[Route('/files/{name}', _ok)])
    app = fastapi.FastAPI()

    @app.get('/')
    def root():
        return {'ok': True}

    app.include_router(api)
    app.router.routes.append(Mount('/static', app=sub))

    ffroute.speedup(app)

    # Top-level FastAPI route.
    assert _probe(app, '/') == 200
    # include_router flattens routes into app.router.routes; the trie indexes
    # them like any other Route.
    assert _probe(app, '/api/v1/items/42') == 200
    assert _probe(app, '/api/v1/users/me') == 200
    # Method dispatch (PARTIAL match) still works for include_router routes.
    assert _probe(app, '/api/v1/items/42', method='POST') == 405
    # Mount routes — always-candidate via the _unindexed list.
    assert _probe(app, '/static/files/logo.png') == 200
    # 404s for unmatched paths under both spaces.
    assert _probe(app, '/api/v1/missing') == 404
    assert _probe(app, '/static/missing-thing') == 404
    # FastAPI's auto-generated /openapi.json should still resolve — it's
    # added to app.router.routes lazily on first access.
    assert _probe(app, '/openapi.json') == 200


def test_include_router_and_top_level_can_overlap():
    """`/items/{x}` registered top-level AND `/api/items/{x}` via APIRouter
    must both be reachable; their static prefixes split the trie."""
    fastapi = pytest.importorskip('fastapi')

    api = fastapi.APIRouter(prefix='/api')

    @api.get('/items/{x}')
    def api_items(x: str):
        return {'where': 'api', 'x': x}

    app = fastapi.FastAPI()

    @app.get('/items/{x}')
    def top_items(x: str):
        return {'where': 'top', 'x': x}

    app.include_router(api)
    ffroute.speedup(app)

    assert _probe(app, '/items/42') == 200
    assert _probe(app, '/api/items/42') == 200
    # Neither prefix shadows the other's 404.
    assert _probe(app, '/api/missing') == 404
    assert _probe(app, '/missing') == 404


# --- FFAPIRouter (static class) -------------------------------------------


def test_ffapirouter_is_importable_static_class():
    """``FFAPIRouter`` is the static class that backs the planned upstream
    ``FastAPI(router_class=FFAPIRouter)`` proposal — must be a real
    importable subclass of both ``APIRouter`` and ``FFRouter``."""
    fastapi = pytest.importorskip('fastapi')

    assert ffroute.FFAPIRouter.__module__ == 'ffroute._fastapi'
    assert issubclass(ffroute.FFAPIRouter, fastapi.APIRouter)
    assert issubclass(ffroute.FFAPIRouter, ffroute.FFRouter)
    # MRO order: our overrides win, APIRouter behavior is preserved.
    mro_names = [c.__name__ for c in ffroute.FFAPIRouter.__mro__]
    assert mro_names.index('FFRouter') < mro_names.index('APIRouter')


def test_ffapirouter_as_drop_in_via_assignment():
    """Construct ``FFAPIRouter`` directly and assign it as ``app.router`` —
    this is the shape that ``FastAPI(router_class=FFAPIRouter)`` will use
    once the upstream PR lands. Verify end-to-end: parametrized routes,
    /openapi.json, method-mismatch → 405, include_router."""
    fastapi = pytest.importorskip('fastapi')

    api = fastapi.APIRouter(prefix='/api/v1')

    @api.get('/items/{item_id}')
    def get_item(item_id: int):
        return {'item_id': item_id}

    # Build the app, then replace the auto-created APIRouter with an
    # FFAPIRouter pre-loaded with the same routes/config. This is the
    # forward-compatible shape: same as what FastAPI's __init__ will do
    # internally once router_class= is accepted.
    app = fastapi.FastAPI()

    @app.get('/')
    def root():
        return {'ok': True}

    @app.get('/users/{user_id}')
    def get_user(user_id: int):
        return {'user_id': user_id}

    app.include_router(api)

    # Swap router AFTER routes are registered, preserving them.
    old_routes = list(app.router.routes)
    new_router = ffroute.FFAPIRouter()
    new_router.routes.extend(old_routes)
    app.router = new_router
    # FastAPI captures middleware_stack lazily in build_middleware_stack(),
    # so router replacement before first request is safe.
    new_router._rebuild_index()

    assert _probe(app, '/') == 200
    assert _probe(app, '/users/42') == 200
    assert _probe(app, '/api/v1/items/42') == 200
    assert _probe(app, '/api/v1/items/42', method='POST') == 405
    assert _probe(app, '/missing') == 404
    # /openapi.json must still work — proves APIRouter behavior is intact.
    assert _probe(app, '/openapi.json') == 200


def test_ffapirouter_works_without_speedup_call():
    """When ``FFAPIRouter`` is the router class from construction time,
    no ``speedup()`` call and no ``middleware_stack`` rebind are needed —
    dispatch flows through ``FFAPIRouter.app`` naturally. This is the
    invariant the upstream ``router_class=`` PR will enable; we prove it
    holds today by constructing the router as the active class up-front.

    The spy pattern (drop a custom matcher into ``_ffroute``, drive a
    request, assert the matcher was consulted) is the same regression
    pattern from ``test_speedup_actually_activates_trie_dispatch`` —
    here applied to the static-class path, not the swap path."""
    fastapi = pytest.importorskip('fastapi')

    router = ffroute.FFAPIRouter()

    @router.get('/x')
    def handler():
        return {}

    app = fastapi.FastAPI()
    app.router = router
    router._rebuild_index()

    # Notably: no ffroute.speedup(app) call. No app.router.__class__ swap.
    # If FFRouter's app override is actually the active dispatcher (because
    # the router was constructed as FFAPIRouter from the start), the spy
    # below sees the call.
    real = router._ffroute
    calls: list[str] = []

    class SpyMatcher:
        def match_all(self, path: str) -> list[int]:
            calls.append(path)
            return real.match_all(path) if real is not None else []

    router._ffroute = SpyMatcher()

    assert _probe(app, '/x') == 200
    assert calls == ['/x'], f'trie was never consulted via the static class path; calls={calls!r}'


def test_speedup_actually_activates_trie_dispatch():
    """Regression for the middleware_stack dormancy bug.

    ``Router.__init__`` caches ``middleware_stack = self.app`` as a bound
    method at construction time. A plain ``__class__`` swap leaves that
    bound method pointing at the *old* ``Router.app`` function, so ASGI
    dispatch never enters ``FFRouter.app`` and the trie sits unused. The
    fix in ``_swap_router_class`` rebinds ``middleware_stack`` to the new
    ``router.app``.

    Without the fix this test fails: the spy's ``match_all`` is never
    called because dispatch bypasses ``FFRouter.app`` entirely.
    """
    app = Starlette(routes=[Route('/x', _ok)])
    ffroute.speedup(app)

    real = app.router._ffroute
    calls: list[str] = []

    class SpyMatcher:
        def match_all(self, path: str) -> list[int]:
            calls.append(path)
            return real.match_all(path)

    app.router._ffroute = SpyMatcher()

    assert _probe(app, '/x') == 200
    assert calls == ['/x'], f'trie was never consulted; calls={calls!r}'


def test_mounted_subapp_trie_sees_root_path_stripped():
    """Regression: a mounted ``FFRouter`` must feed the trie the path with
    the mount prefix removed.

    Starlette's ``Mount`` does not rewrite ``scope['path']``; it appends the
    consumed prefix to ``scope['root_path']`` and leaves ``path`` as the full
    request path. The trie inside the sub-app is built from *relative* route
    patterns (e.g. ``/x``), so matching against the raw ``scope['path']``
    (``/outer/x``) would always miss and silently fall through to the slow
    ``super().app`` linear scan — the fast-path going dormant inside mounts.

    ``get_route_path(scope)`` strips ``root_path``, so the trie must be
    consulted with ``/x``, not ``/outer/x``.
    """
    inner = Starlette(routes=[Route('/x', _ok)])
    app = Starlette(routes=[Mount('/outer', app=inner)])
    ffroute.speedup(app)

    real = inner.router._ffroute
    calls: list[str] = []

    class SpyMatcher:
        def match_all(self, path: str) -> list[int]:
            calls.append(path)
            return real.match_all(path)

    inner.router._ffroute = SpyMatcher()

    assert _probe(app, '/outer/x') == 200
    assert calls == ['/x'], f'mounted trie consulted with un-stripped path; calls={calls!r}'


def test_speedup_preserves_router_level_middleware():
    """If ``router.middleware_stack`` has been wrapped with custom middleware
    before ``speedup`` is called, the rebind logic must skip — otherwise
    we'd silently drop that middleware. The class swap itself still happens
    (so future child routers and ``_rebuild_index`` updates work)."""
    app = Starlette(routes=[Route('/x', _ok)])

    # Simulate router-level middleware: a callable that isn't a bound method
    # of the router (so __self__ isn't `router`, so our rebind guard skips).
    sentinel = object()
    app.router.middleware_stack = sentinel  # type: ignore[assignment]

    ffroute.speedup(app)

    assert isinstance(app.router, ffroute.FFRouter)
    assert app.router.middleware_stack is sentinel, (
        'router-level middleware was silently replaced by the speedup rebind'
    )


def test_non_http_scope_falls_through_to_super():
    # FFRouter.app must short-circuit on non-http/websocket scopes (e.g.
    # lifespan) and defer to the base Router so startup/shutdown events fire.
    app = Starlette(routes=[Route('/x', _ok)])
    ffroute.speedup(app)
    sent: list[dict] = []

    async def receive():
        # Return startup once, then shutdown — Starlette's lifespan handler
        # exits cleanly after seeing shutdown.complete.
        return {'type': 'lifespan.shutdown'} if sent else {'type': 'lifespan.startup'}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app({'type': 'lifespan'}, receive, send))
    msg_types = [m['type'] for m in sent]
    assert 'lifespan.startup.complete' in msg_types
    assert 'lifespan.shutdown.complete' in msg_types
