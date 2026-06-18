"""ffroute — Starlette-compatible URL route matcher (Rust + PyO3).

Two public classes:

* :class:`Router` — the low-level Rust trie. Build with a list of
  Starlette-style path patterns; returns the **index** of the matching pattern
  (or ``None``) for each probe. Zero Python dependencies; suitable as a
  building block for your own dispatch layer.

* :class:`FFRouter` — a drop-in ``starlette.routing.Router`` subclass that
  delegates the path-matching scan to ``Router`` while keeping Starlette's
  per-route ``Route.matches`` / method dispatch / ``Mount`` / ``Host`` /
  ``redirect_slashes`` logic intact. Requires Starlette
  (``pip install ffroute[starlette]``); imported lazily so the core stays
  dependency-free.
"""

from __future__ import annotations

from ._core import Router

__all__ = ['FFRouter', 'Router', 'speedup']

_LAZY_STARLETTE = {'FFRouter', 'speedup'}


def __getattr__(name: str):
    if name in _LAZY_STARLETTE:
        try:
            from . import _starlette
        except ImportError as e:
            # Only rewrite the message if Starlette itself is the missing module;
            # any other ImportError (e.g. a bug in _starlette.py, a broken env)
            # should surface unchanged so the user can see the real cause.
            if e.name and e.name.split('.', 1)[0] == 'starlette':
                raise ImportError(
                    f'ffroute.{name} requires Starlette. '
                    'Install with: pip install ffroute[starlette]'
                ) from e
            raise
        return getattr(_starlette, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_STARLETTE})
