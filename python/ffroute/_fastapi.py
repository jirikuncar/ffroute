"""FastAPI integration: ``FFAPIRouter`` — the FFRouter × APIRouter mixin.

Only imported when the user references ``ffroute.FFAPIRouter``; not loaded
on ``import ffroute`` itself. Importing this module unconditionally requires
``fastapi`` (which in turn requires ``starlette``).
"""

from __future__ import annotations

from fastapi.routing import APIRouter

from ._starlette import FFRouter


class FFAPIRouter(FFRouter, APIRouter):
    """Drop-in ``fastapi.routing.APIRouter`` subclass with FFRouter narrowing.

    Brings FastAPI's APIRouter-specific behavior (``_get_routes_version``
    for OpenAPI cache invalidation, ``add_api_route``, ``include_router``,
    HTTP-method decorators) together with FFRouter's trie path-scan. MRO::

        FFAPIRouter → FFRouter → APIRouter → Router → object

    Equivalent to the dynamic mixin that :func:`ffroute.speedup` synthesizes
    at call time, but as a real importable type. Lets users construct the
    fast router directly::

        app = FastAPI()
        app.router = FFAPIRouter(...)            # today
        app = FastAPI(router_class=FFAPIRouter)  # once upstream PR lands

    No body required — the mix is purely structural: FFRouter's ``app`` /
    ``add_route`` / ``_rebuild_index`` overrides win via C3 linearization,
    while APIRouter's APIRouter-specific methods are inherited unchanged.
    """
