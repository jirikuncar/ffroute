"""Minimal Starlette app whose router is replaced with ffroute-backed narrowing.

Run:
    uv run uvicorn examples.starlette_app:app --reload

Then::
    curl http://localhost:8000/users/42
    curl http://localhost:8000/items/abc
"""

from __future__ import annotations

from ffroute import speedup
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


async def homepage(request: Request) -> JSONResponse:
    return JSONResponse({'hello': 'world', 'matcher': 'ffroute'})


async def show_user(request: Request) -> JSONResponse:
    return JSONResponse({'user_id': int(request.path_params['user_id'])})


async def show_item(request: Request) -> JSONResponse:
    return JSONResponse({'item': request.path_params['item']})


routes = [
    Route('/', homepage),
    Route('/users/{user_id:int}', show_user),
    Route('/items/{item}', show_item),
]

app = Starlette(routes=routes)
speedup(app)  # swap app.router for FFRouter — that's it.
