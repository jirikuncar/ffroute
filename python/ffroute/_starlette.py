"""Starlette integration: ``FFRouter`` candidate-narrowing layer.

Only imported when the user references ``ffroute.FFRouter``; not loaded on
``import ffroute`` itself. Importing this module unconditionally requires
``starlette``.
"""

from __future__ import annotations

from typing import Any

from starlette.routing import Match, Mount, Router

from ._core import Router as _TrieRouter


class FFRouter(Router):
    """A ``starlette.routing.Router`` whose path-matching scan is replaced by
    the ffroute trie.

    Behaviour is identical to Starlette's stock ``Router``:

    * ``Route.matches`` still runs on each candidate, so method dispatch,
      param extraction and ``redirect_slashes`` are unchanged.
    * ``Mount`` and ``Host`` routes have no flat path the trie can index, so
      they are always considered candidates.
    * First-registered match wins, exactly like Starlette.

    Only difference: the linear regex scan over every route's ``path_regex``
    is replaced by a ``match_all`` lookup on the trie (~100× faster on
    realistic OpenAPI specs).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        unindexed: list[int] = []
        indexed: list[int] = []
        patterns: list[str] = []
        for i, route in enumerate(self.routes):
            if isinstance(route, Mount) or not hasattr(route, 'path'):
                unindexed.append(i)
            else:
                indexed.append(i)
                patterns.append(route.path)
        self._unindexed = unindexed
        self._indexed = indexed
        self._ffroute = _TrieRouter(patterns) if patterns else None

    def add_route(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        super().add_route(*args, **kwargs)
        self._rebuild_index()

    def add_websocket_route(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        super().add_websocket_route(*args, **kwargs)
        self._rebuild_index()

    async def app(self, scope, receive, send):  # type: ignore[override]
        if scope['type'] not in ('http', 'websocket') or self._ffroute is None:
            await super().app(scope, receive, send)
            return

        path = scope.get('path', '/')
        hits = self._ffroute.match_all(path)
        # De-dup + preserve registration order. Mounts/Hosts always considered.
        candidate_route_indices = sorted({self._indexed[h] for h in hits} | set(self._unindexed))

        partial = None
        partial_scope = None
        for ri in candidate_route_indices:
            route = self.routes[ri]
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                merged = {**scope, **child_scope}
                await route.handle(merged, receive, send)
                return
            elif match == Match.PARTIAL and partial is None:
                partial = route
                partial_scope = child_scope

        if partial is not None:
            merged = {**scope, **partial_scope}
            await partial.handle(merged, receive, send)
            return

        # No match — defer to base ``Router`` so default_app / redirects fire.
        await super().app(scope, receive, send)


def _swap_router_class(router: Router) -> None:
    """Replace ``router.__class__`` with an FFRouter mixin (idempotent), then
    rebind ``middleware_stack`` so the swap actually takes effect.

    ``Router.__init__`` does ``self.middleware_stack = self.app`` — a bound
    method captured at construction time. Plain ``router.__class__ = FFRouter``
    swaps the class but leaves the cached bound method pointing at the
    original ``Router.app`` function, so dispatch silently bypasses the trie
    (the swap is dormant). The rebind below re-resolves ``router.app`` via
    the new class so dispatch flows through ``FFRouter.app``.

    Guard: only rebind when ``middleware_stack`` is still the bare bound
    ``self.app`` — i.e. ``__self__`` points back at the router. If anything
    has wrapped it with middleware (router-level middleware stack), re-binding
    would silently drop that middleware. In that case the swap stays dormant
    *for that router*; the user keeps their middleware but doesn't get the
    trie fast-path until they call ``speedup`` before wrapping.
    """
    cls = type(router)
    if not issubclass(cls, FFRouter):
        new_cls = FFRouter if cls is Router else type(f'FF{cls.__name__}', (FFRouter, cls), {})
        router.__class__ = new_cls
    router._rebuild_index()
    ms = getattr(router, 'middleware_stack', None)
    if getattr(ms, '__self__', None) is router:
        router.middleware_stack = router.app


def speedup(app_or_router: Any, *, recursive: bool = True) -> None:
    """Apply ffroute to an app's (or router's) path-matching scan.

    Common pattern::

        from fastapi import FastAPI
        from ffroute import speedup
        app = FastAPI()
        # ... register routes ...
        speedup(app)

    Works for plain ``Starlette``, ``FastAPI`` (whose ``APIRouter`` adds
    methods like ``_get_routes_version`` for OpenAPI-schema cache
    invalidation that a flat class cast would discard), and bare
    ``starlette.routing.Router`` instances. For non-stock router subclasses
    a dynamic mixin is created that inherits from both ``FFRouter`` and the
    user's existing class — FFRouter's overrides win, everything else stays
    intact.

    With ``recursive=True`` (the default), also walks into ``Mount`` routes
    and speeds up any Starlette/FastAPI sub-app (or bare ``Router``) found
    there. Mounts wrapping non-router ASGI apps (``StaticFiles``, plain
    callables) are left alone. Pass ``recursive=False`` to only touch the
    top-level router.

    Idempotent: calling ``speedup`` on an already-speeded-up app just
    re-indexes; safe to call again after registering more routes.
    """
    router = app_or_router if isinstance(app_or_router, Router) else app_or_router.router
    _swap_router_class(router)
    if not recursive:
        return
    for route in router.routes:
        # Mount('/x', app=...) → route.app is the wrapped ASGI app or Router.
        # Mount('/x', routes=[...]) → Starlette wraps the routes in a Router
        # and stores it as route.app.
        sub = getattr(route, 'app', None)
        if sub is None:
            continue
        if isinstance(sub, Router):
            sub_router: Router | None = sub
        else:
            candidate = getattr(sub, 'router', None)
            sub_router = candidate if isinstance(candidate, Router) else None
        if sub_router is not None:
            speedup(sub_router, recursive=True)
