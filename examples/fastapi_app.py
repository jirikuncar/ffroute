# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "ffroute[starlette]",
#     "fastapi>=0.115",
#     "uvicorn>=0.30",
# ]
# ///
"""Minimal FastAPI app whose underlying Starlette router is swapped for ffroute.

Run directly — uv assembles a fresh environment from the PEP 723 metadata
above (no project setup needed)::

    uv run examples/fastapi_app.py

Or, from inside this repo (uses the local editable ffroute install)::

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


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host='127.0.0.1', port=8000)
