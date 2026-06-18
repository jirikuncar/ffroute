# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "ffroute[starlette]",
#     "uvicorn>=0.30",
# ]
#
# # Local-checkout override (uv extension): resolves ffroute from this repo
# # instead of PyPI. Remove this block once ffroute is published on PyPI.
# [tool.uv.sources]
# ffroute = { path = "..", editable = true }
# ///
"""Minimal Starlette app whose router is replaced with ffroute-backed narrowing.

Run directly — uv assembles a fresh environment from the PEP 723 metadata
above (no project setup needed)::

    uv run examples/starlette_app.py

Or, from inside this repo (uses the local editable ffroute install)::

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


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host='127.0.0.1', port=8000)
