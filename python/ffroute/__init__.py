"""ffroute — Starlette-compatible URL route matcher (Rust + PyO3).

Public classes:

* :class:`Router` — the low-level Rust trie. Build with a list of
  Starlette-style path patterns; returns the **index** of the matching pattern
  (or ``None``) for each probe. Zero Python dependencies; suitable as a
  building block for your own dispatch layer.

* :class:`FFRouter` — a drop-in ``starlette.routing.Router`` subclass that
  delegates the path-matching scan to ``Router`` while keeping Starlette's
  per-route ``Route.matches`` / method dispatch / ``Mount`` / ``Host`` /
  ``redirect_slashes`` logic intact. Requires Starlette
  (``pip install ffroute[starlette]``); imported lazily.

* :class:`FFAPIRouter` — same idea for FastAPI: a
  ``fastapi.routing.APIRouter`` subclass that mixes ``FFRouter`` in.
  Requires FastAPI (``pip install ffroute[fastapi]``); imported lazily.

* :func:`speedup` — convenience helper that swaps an existing app's router
  to use ``FFRouter`` / ``FFAPIRouter``. Recurses into ``Mount`` sub-apps
  by default.
"""

from __future__ import annotations

from ._core import Router

__all__ = ['FFAPIRouter', 'FFRouter', 'Router', 'speedup']

# Lazy attribute → (submodule, extras-package). Importing the submodule
# triggers the underlying framework dep; the second value is what to
# suggest in the rewritten ImportError if that dep is missing.
_LAZY: dict[str, tuple[str, str]] = {
    'FFRouter': ('._starlette', 'starlette'),
    'speedup': ('._starlette', 'starlette'),
    'FFAPIRouter': ('._fastapi', 'fastapi'),
}


def __getattr__(name: str):
    if name in _LAZY:
        submod, extra = _LAZY[name]
        try:
            from importlib import import_module

            mod = import_module(submod, __name__)
        except ImportError as e:
            # Only rewrite the message if the framework itself is the
            # missing module; any other ImportError (e.g. a bug in our
            # submodule, a broken env) should surface unchanged so the
            # user can see the real cause.
            if e.name and e.name.split('.', 1)[0] == extra:
                raise ImportError(
                    f'ffroute.{name} requires {extra.capitalize()}. '
                    f'Install with: pip install ffroute[{extra}]'
                ) from e
            raise
        return getattr(mod, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})
