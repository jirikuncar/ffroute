"""Minimal FastAPI app whose underlying Starlette router is swapped for ffroute.

Run:
    uv run uvicorn examples.fastapi_app:app --reload

Then::
    curl http://localhost:8000/users/42
    curl http://localhost:8000/openapi.json
"""

from __future__ import annotations

from fastapi import FastAPI
from ffroute import speedup

app = FastAPI(title='ffroute demo')


@app.get('/')
def homepage() -> dict:
    return {'hello': 'world', 'matcher': 'ffroute'}


@app.get('/users/{user_id}')
def show_user(user_id: int) -> dict:
    return {'user_id': user_id}


@app.get('/items/{item}')
def show_item(item: str) -> dict:
    return {'item': item}


# FastAPI inherits its router from Starlette; one call swaps in FFRouter for
# every route already registered (including FastAPI's own /openapi.json, /docs).
speedup(app)
